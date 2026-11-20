"""天网 · GitHub 代码挖域名层（ghcode）
====================================
从 GitHub 全站代码里挖「自建站」域名 —— NAS / DDNS / 免费主机 / 个人站。

为什么这条路能挖到搜索引擎看不见的小众站：搜索引擎收录慢、甚至不收录，
但自建站的作者**会在代码 / README / 配置文件里留下域名痕迹**（nginx 的
server_name、Caddy 的站点地址、Docker 的 traefik Host 规则、README 里的
部署链接）。GitHub 是全世界最大的代码库，Code Search 全文搜代码 = 把
「谁在代码里写了哪个自建站域名」直接查出来。

这是「关键词 → 域名」的反向通道，专门针对搜索引擎看不见的小众站。

数据源：gh CLI 的 `gh search code`
  · 走 gh 子命令，token 由 gh 自己管理，代码里不碰明文 token
  · Code Search 必须认证（匿名 401），gh 已登录（token 带 repo scope）正好可用
  · 限速约 30 次/分 ⇒ 节流 + 磁盘缓存，别打爆

设计约束（与项目铁律一致）：
  · 子进程走隐藏窗口三件套（net._hidden_startupinfo），不弹窗
  · 只读查询，不构造任何写操作路径
  · 域名提取后过滤平台自身域名（github.com 等），只留真·自建站
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from . import net

# ---------------------------------------------------------------- 常量

# 缓存文件（与其它层共用一份磁盘缓存，TTL 各自控制）
_CACHE_PATH = Path(__file__).resolve().parent.parent / "_ghcode_cache.json"
_CACHE_TTL = 7 * 24 * 3600

# gh search code 认证后约 30 次/分 ⇒ 两次调用间隔 2.5s（留余量）
_MIN_GAP = 2.5
_last_call = 0.0

# 域名提取正则：匹配一个完整域名（可带 http:// 前缀和路径）
_DOMAIN_RE = re.compile(
    r"(?:https?://)?([a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}"
    r"(?::\d{1,5})?(?:/[^\s\"'<>)]*)?",
    re.IGNORECASE,
)

# 平台自身域名（不算「发现的资源」，过滤掉）
_PLATFORM_DOMS = {
    "github.com", "github.io", "githubusercontent.com", "gitlab.com",
    "gist.github.com", "raw.githubusercontent.com", "bitbucket.org",
    "example.com", "example.org", "localhost", "0.0.0.0", "127.0.0.1",
    "stackoverflow.com", "stackexchange.com", "npmjs.com", "pypi.org",
    "google.com", "microsoft.com", "w3.org", "ietf.org",
}

# ---------------------------------------------------------------- 缓存


def _load() -> dict:
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(c: dict) -> None:
    try:
        tmp = _CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
    except Exception:
        pass


def _cached(key: str, ttl: float = _CACHE_TTL):
    c = _load()
    rec = c.get(key)
    if rec and time.time() - rec.get("ts", 0) < ttl:
        return rec.get("v")
    return None


def _cache_put(key: str, v) -> None:
    c = _load()
    c[key] = {"ts": time.time(), "v": v}
    _save(c)


# ---------------------------------------------------------------- gh 子进程


def _throttle() -> None:
    global _last_call
    wait = _MIN_GAP - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def _gh_search_code(query: str, limit: int = 30) -> list[dict]:
    """调 `gh search code`，返回结果列表（每条含 path / repository / textMatches）。

    走 gh 子命令，token 由 gh 自己管。textMatches 里有 fragment（关键词附近的
    代码片段），从里面能提取出完整域名。
    """
    _throttle()
    cmd = ["gh", "search", "code", query, "--limit", str(limit),
           "--json", "path,repository,textMatches"]
    try:
        si = net._hidden_startupinfo()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                           startupinfo=si)
    except Exception:
        return []
    if r.returncode != 0 or not r.stdout.strip():
        return []
    try:
        return json.loads(r.stdout)
    except Exception:
        return []


# ---------------------------------------------------------------- 域名提取


def _clean_domain(raw: str) -> str | None:
    """把从代码里抠出来的域名片段洗干净，返回干净的 host，不合规则返回 None。"""
    d = raw.strip().lower()
    # 去协议前缀
    d = re.sub(r"^https?://", "", d)
    # 去端口和路径（只留 host 部分）
    d = re.split(r"[:/]", d)[0]
    d = d.strip(".")
    if not d or "." not in d:
        return None
    # 过滤平台自身域名
    for pd in _PLATFORM_DOMS:
        if d == pd or d.endswith("." + pd):
            return None
    # 只留像域名的（无空格/引号/特殊符号）
    if re.search(r"[\s\"'<>(){}]", d):
        return None
    # 后缀必须含字母（过滤纯数字/IP）
    tld = d.rsplit(".", 1)[-1]
    if not re.match(r"^[a-z]{2,}$", tld):
        return None
    return d


def _extract_domains(fragments: list[str], suffix: str = "") -> list[str]:
    """从代码片段里提取所有域名。suffix 非空时，只保留该后缀的域名（精准）。

    ⚠️ 用 group(0)（完整匹配）：group(1) 只捕获最后一段「标签+点」（如 `duckdns.`），
    不含 TLD，会洗成空。group(0) 才是完整域名，_clean_domain 再去协议/端口/路径。
    """
    out: list[str] = []
    for frag in fragments:
        for m in _DOMAIN_RE.finditer(frag or ""):
            d = _clean_domain(m.group(0))
            if not d:
                continue
            if suffix:
                s = suffix.lstrip("*.")
                # 过滤平台官网本身（裸域 + www），只留用户自定义子域 xxx.后缀
                if d == s or d == "www." + s:
                    continue
                if not d.endswith("." + s):
                    continue
            out.append(d)
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- 挖域名入口


def gh_code_domains(suffix: str, limit: int = 30, timeout: float = 60.0) -> list[str]:
    """按后缀挖自建站域名 —— 核心入口。

    suffix 是自建站的域名后缀指纹，例如：
      · `duckdns.org`（DuckDNS 免费 DDNS，NAS 用户最爱）
      · `synology.me`（群晖 NAS）
      · `myqnapcloud.com`（威联通）
      · `pages.dev`（Cloudflare Pages）
      · `onrender.com`（Render 免费主机）
    搜这个后缀在代码里的所有出现，从上下文片段里提取 `xxx.后缀` 完整域名。
    """
    suffix = (suffix or "").strip().lower().lstrip("*.")
    if not suffix or "." not in suffix:
        return []
    key = f"ghcode:{suffix}"
    hit = _cached(key)
    if hit is not None:
        return hit[:limit]

    results = _gh_search_code(suffix, limit=max(limit, 30))
    fragments = []
    for rec in results:
        for tm in (rec.get("textMatches") or []):
            frag = tm.get("fragment") or ""
            if frag:
                fragments.append(frag)
    doms = _extract_domains(fragments, suffix=suffix)
    _cache_put(key, doms)
    return doms[:limit]


def gh_code_server_names(limit: int = 30, timeout: float = 60.0) -> list[str]:
    """从 nginx/Caddy 配置里挖 server_name —— 挖「泛自建站」域名（不限后缀）。

    搜 `server_name`（nginx 配置指令），从片段里提取所有被代理的域名。
    这能挖到不用 DDNS、直接绑自己域名的自建站（VPS / 家庭宽带 / 云主机）。
    """
    key = "ghcode:server_name"
    hit = _cached(key)
    if hit is not None:
        return hit[:limit]

    results = _gh_search_code("server_name", limit=max(limit, 30))
    fragments = []
    for rec in results:
        for tm in (rec.get("textMatches") or []):
            frag = tm.get("fragment") or ""
            if frag:
                fragments.append(frag)
    doms = _extract_domains(fragments)
    _cache_put(key, doms)
    return doms[:limit]


# ---------------------------------------------------------------- 自测


def _selftest():
    ok = 0
    fail = 0

    def check(cond, label):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [ok] {label}")
        else:
            fail += 1
            print(f"  [FAIL] {label}")

    # 1. 域名清洗（离线）
    check(_clean_domain("https://mynas.duckdns.org/") == "mynas.duckdns.org",
          "带协议/路径的域名清洗")
    check(_clean_domain("github.com") is None, "平台域名被过滤")
    check(_clean_domain("127.0.0.1") is None, "IP 被过滤")

    # 2. 片段提取（离线）
    frags = ["upstream mynas.duckdns.org { server 192.168.1.5; }",
             "https://foo.synology.me:5001"]
    check("mynas.duckdns.org" in _extract_domains(frags), "片段提取域名")

    # 3. 联网：gh search code 按后缀挖（duckdns.org 是 DDNS 铁证）
    doms = gh_code_domains("duckdns.org", limit=20)
    print(f"      gh_code_domains(duckdns.org) → {len(doms)} 个：{doms[:6]}")
    check(isinstance(doms, list) and bool(doms), "gh search code 挖到域名")

    print(f"[ghcode] 自测：{ok} 通过 / {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
