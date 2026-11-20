"""Cookie 持久化层 —— 「登录一次，长期用」。

伪装登录的第一级「会话层」：登录态（Cookie）落盘、复用、保活。

为什么这是伪装登录的地基：
    · 正常登录一次之后，服务商给你的身份就是那串 Cookie。
    · 把 Cookie 存盘、下次建会话时自动带上 ⇒ 不用反复登录，就是「最正常的路」。
    · 强者走最正常的路 —— 正常登录、正常带 Cookie，服务商看到的就是一个正常用户。

与 netforce 现有 _SessionPool 的关系：
    · _SessionPool 只做「连接复用 + TLS 指纹」（进程内、内存态，重启即失）。
    · 本模块补上「登录态落盘」，让 Cookie 跨进程存活，成为真正的持久身份。

库目录：<netforce>/_cookies/<host>.json
格式：{"host": ..., "saved_at": unix, "cookies": {"name": "value", ...}}

设计约束（第一级，先走简单）：
    · 只存 name=value 对（大多数登录 Cookie 是 host-only 的，简单键值够用）。
    · domain/path/expires/SameSite 的完整语义留到第二级再补。
    · curl_cffi 与 requests 的 Cookie 对象都用统一导出/注入，不区分后端。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

COOKIE_DIR = Path(__file__).resolve().parent.parent / "_cookies"

# 登录态检测的判据：URL 或正文出现这些词，多半是被踢去登录页了。
_LOGIN_HINT_URL = re.compile(r"(login|signin|sign-in|log-in|auth|sso|oauth|callback)", re.I)
_LOGIN_HINT_TEXT = re.compile(
    r"(请先登录|立即登录|登录后|登录注册|sign in|sign-in|log in|log-in|"
    r"you need to sign in|please log in|login to continue)", re.I)

# 排除误判：很多站首页就带「登录」按钮，所以只把「跳转到登录页」当作失活信号，
# 不把「页面上有个登录链接」当作失活。
_AUTH_CODES = {401, 403}


# ---------------------------------------------------------------- 底层读写

def host_key(host: str) -> str:
    """把 host 洗成文件名安全的 key（去掉 scheme / 端口 / 路径）。"""
    h = (host or "").strip().lower()
    h = re.sub(r"^https?://", "", h)
    h = h.split("/", 1)[0].split(":", 1)[0].split("?", 1)[0]
    h = re.sub(r"[^a-z0-9._\-]", "_", h)
    return h or "unknown"


def _jar_path(host: str) -> Path:
    return COOKIE_DIR / f"{host_key(host)}.json"


def _dump_cookies(session) -> dict[str, str]:
    """从 curl_cffi / requests 的 Session 里导出 name=value 字典。"""
    cj = getattr(session, "cookies", None)
    if cj is None:
        return {}
    out: dict[str, str] = {}
    # 优先 items()（两库都实现 Mapping 协议）
    try:
        for k, v in cj.items():
            if k:
                out[str(k)] = str(v)
    except Exception:
        out = {}
    # 兜底：keys()/__iter__ + get()
    if not out:
        try:
            for k in cj.keys():
                if k:
                    out[str(k)] = str(cj.get(k))
        except Exception:
            pass
    return out


def _apply_cookies(session, cookies: dict[str, str]) -> int:
    """把 name=value 字典注入 Session。返回注入条数。"""
    if not cookies:
        return 0
    cj = getattr(session, "cookies", None)
    if cj is None:
        return 0
    n = 0
    try:
        cj.update(cookies)          # 两库都支持 dict update
        n = len(cookies)
    except Exception:
        for k, v in cookies.items():
            try:
                cj.set(k, v)
                n += 1
            except Exception:
                pass
    return n


# ---------------------------------------------------------------- CookieJar

class CookieJar:
    """按 host 落盘/复用 Cookie 的持久层。线程安全、无外部依赖。"""

    def save(self, host: str, session) -> int:
        """把 session 的 Cookie 落盘，返回落盘条数。"""
        cookies = _dump_cookies(session)
        if not cookies:
            return 0
        COOKIE_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"host": host_key(host), "saved_at": int(time.time()), "cookies": cookies}
        tmp = _jar_path(host).with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, _jar_path(host))          # 原子写，避免半截文件
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return 0
        return len(cookies)

    def load(self, host: str, session) -> tuple[int, Optional[int]]:
        """把落盘的 Cookie 注入 session。返回 (注入条数, saved_at)。"""
        p = _jar_path(host)
        if not p.exists():
            return 0, None
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return 0, None
        cookies = rec.get("cookies") or {}
        n = _apply_cookies(session, cookies)
        return n, rec.get("saved_at")

    def has(self, host: str) -> bool:
        return _jar_path(host).exists()

    def clear(self, host: str) -> bool:
        p = _jar_path(host)
        if p.exists():
            try:
                p.unlink()
                return True
            except Exception:
                return False
        return False

    def list_hosts(self) -> list[str]:
        if not COOKIE_DIR.exists():
            return []
        return sorted(p.stem for p in COOKIE_DIR.glob("*.json"))

    def peek(self, host: str) -> dict:
        """只读查看某 host 存了哪些 Cookie 名（不暴露值，安全）。"""
        p = _jar_path(host)
        if not p.exists():
            return {}
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return {
            "host": rec.get("host"),
            "saved_at": rec.get("saved_at"),
            "names": sorted((rec.get("cookies") or {}).keys()),
        }


JAR = CookieJar()


# ---------------------------------------------------------------- 便捷函数

def save_session(host: str, session) -> int:
    """登录后调一次：把当前 session 的登录态存盘。"""
    return JAR.save(host, session)


def load_session(host: str, session) -> tuple[int, Optional[int]]:
    """建会话时自动调：把已存的登录态带回来。"""
    return JAR.load(host, session)


# ---------------------------------------------------------------- 登录态检测

def login_state(url: str, session=None, timeout: float = 15.0) -> dict:
    """探测一个 URL 当前是否「需要登录 / 登录态失效」。

    判据（由弱到强）：
      ① HTTP 401/403           → 明确未授权
      ② 最终 URL 带登录关键词   → 被重定向去登录页了（最硬的失活信号）
      ③ 正文命中「请先登录」等   → 页面本身要求登录

    返回 {"logged_in": bool, "reason": str, "final_url": str, "status": int|None}
    """
    reason = "ok"
    final_url = url
    status = None

    try:
        if session is not None:
            r = session.get(url, timeout=timeout, allow_redirects=True)
            status = r.status_code
            final_url = str(r.url)
            text = r.text or ""
        else:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "netforce/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                final_url = resp.geturl()
                text = resp.read(200_000).decode("utf-8", "ignore")
    except Exception as e:
        return {"logged_in": False, "reason": f"fetch 失败: {e}",
                "final_url": final_url, "status": status}

    if status in _AUTH_CODES:
        return {"logged_in": False, "reason": f"HTTP {status} 未授权",
                "final_url": final_url, "status": status}

    # 最终 URL 跳去了登录页 ⇒ 登录态失效（最硬信号）
    if _LOGIN_HINT_URL.search(final_url) and _LOGIN_HINT_URL.search(final_url).group(0).lower() in \
            ("login", "signin", "sign-in", "log-in", "auth", "sso"):
        # 仅当最终 URL 的「路径段」明显是登录页才判失活，避免首页带 login 词误判
        path = re.sub(r"^https?://[^/]+", "", final_url).lower()
        if re.search(r"(^|/)(login|signin|sign-in|log-in|auth|sso)(/|$|\.|\?)", path):
            return {"logged_in": False, "reason": f"被重定向到登录页: {final_url}",
                    "final_url": final_url, "status": status}

    if _LOGIN_HINT_TEXT.search(text):
        return {"logged_in": False, "reason": "页面正文要求登录",
                "final_url": final_url, "status": status}

    return {"logged_in": True, "reason": "ok", "final_url": final_url, "status": status}


# ---------------------------------------------------------------- 离线自测

def _selftest() -> int:
    ok = fail = 0

    def check(cond, name):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"    [失败] {name}")

    # 1. host_key 清洗
    check(host_key("HTTPS://Example.COM:8080/path?q=1") == "example.com", "host_key 去 scheme/端口/路径")
    check(host_key("www.example.com") == "www.example.com", "host_key 保留子域")

    # 2. 离线 save/load（用 requests 模拟；没有则用假对象）
    try:
        import requests
        s = requests.Session()
        s.cookies.set("sid", "abc123", domain="example.com")
        s.cookies.set("token", "xyz", domain="example.com")
        backend = "requests"
    except Exception:
        class _Fake:
            def __init__(self):
                self.cookies = {"sid": "abc123", "token": "xyz"}
            def get(self, *a, **k):
                return None
        s = _Fake()
        backend = "fake"

    n = JAR.save("example.com", s)
    check(n >= 2, f"save 落盘 {n} 条（backend={backend}）")
    check(JAR.has("example.com"), "has() 识别已存 host")

    n2, saved_at = JAR.load("example.com", s)
    check(n2 >= 2 and saved_at is not None, "load 注入回 session")

    peek = JAR.peek("example.com")
    check("sid" in peek["names"] and "token" in peek["names"], "peek 只读列出 Cookie 名")

    # 3. list_hosts / clear
    check("example.com" in JAR.list_hosts(), "list_hosts 列出已存 host")
    check(JAR.clear("example.com"), "clear 删除落盘")
    check("example.com" not in JAR.list_hosts(), "clear 后 list 不再含该 host")

    # 4. 登录态检测（离线：纯函数判据）
    check(login_state("https://x.com/login")["logged_in"] is False, "login URL 判未登录")
    check(login_state("https://x.com/home")["logged_in"] is True, "普通 URL 判已登录")

    print(f"[cookie_jar] 自测：{ok} 通过 / {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
