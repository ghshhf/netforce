"""天网 · 下载层（fetch）
=========================
职责单一：**把任何一个 URL 变成字节**，并保证不把站点打爆、不弹窗、不丢进度。

对外只暴露三件事：
    fetch_text(url)  -> FetchResult(文本)
    fetch_bytes(url) -> FetchResult(字节)
    download(url, dest) -> FetchResult(落盘, 支持 Range 续传)

内部四层结构：
    ① 域名令牌桶      每个域独立限速，互不牵连（一个慢站不拖全局）
    ② 三级通道        urllib(代理) → urllib(直连) → curl_cffi(Chrome TLS 指纹) → 系统 curl
    ③ 退避策略        网络错指数退避；连续风控则全局降速（学 resource_finder 的 throttle）
    ④ 统一记录        每次请求的成功/失败/体积/通道/耗时，供检测层统计

🔴 后台脚本一律不许弹窗：curl 分支必须带 STARTUPINFO+SW_HIDE / CREATE_NO_WINDOW / stdin=DEVNULL，
   且 STARTUPINFO 每次新建（复用第二次失效）。
"""
from __future__ import annotations

import os
import io
import re
import ssl
import time
import json
import random
import gzip
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

PROXY = os.environ.get("NETFORCE_PROXY", "http://127.0.0.1:3067")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 指纹池。🔴 关键不是「多」，是「稳」：同一个域必须始终用同一套身份。
# 每个请求换 UA 比固定 UA 更像机器人 —— 真人不会每点一次链接就换台设备。
UA_POOL = [
    # (UA, Accept-Language, sec-ch-ua 平台段)
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", "zh-CN,zh;q=0.9,en;q=0.8", "Windows"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36", "zh-CN,zh;q=0.9", "Windows"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0", "zh-CN,zh;q=0.9,en;q=0.7", "Windows"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", "zh-CN,zh;q=0.9,en;q=0.8", "macOS"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
     "zh-CN,zh;q=0.8,en-US;q=0.5,en;q=0.3", ""),
]


def _fingerprint(host: str) -> tuple:
    """按域名粘一套指纹。和 egress_for 同样的思路：同域稳定，跨域分散。"""
    return UA_POOL[zlib.crc32((host or "").encode()) % len(UA_POOL)]


def ua_for(host: str) -> str:
    return _fingerprint(host)[0]


def headers_for(host: str, referer: str = "") -> dict:
    """给该域生成一套自洽的请求头。

    自洽 > 齐全：Chrome 的 UA 配 Firefox 的 header 顺序、
    或者没有 sec-ch-ua 的 UA 却带 sec-ch-ua，反而是明显的破绽。

    Referer 缺省用同域根：真实浏览里点站内链接就是这个形态，
    而且 wiki 这类站会校验 Referer，不带直接给你 567。
    """
    ua, lang, plat = _fingerprint(host)
    if not referer and host:
        referer = f"https://{host}/"
    h = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": lang,
        "Accept-Encoding": "gzip, deflate",
        "Upgrade-Insecure-Requests": "1",
    }
    if plat:                                   # 只有 Chromium 系才发这两个
        h["sec-ch-ua"] = ('"Chromium";v="131", "Not_A Brand";v="24", '
                          f'"Google Chrome";v="131"')
        h["sec-ch-ua-mobile"] = "?0"
        h["sec-ch-ua-platform"] = f'"{plat}"'
        h["Sec-Fetch-Dest"] = "document"
        h["Sec-Fetch-Mode"] = "navigate"
        h["Sec-Fetch-Site"] = "same-origin" if referer else "none"
        h["Sec-Fetch-User"] = "?1"
    if referer:
        h["Referer"] = referer
    return h

# 出口列表：默认单出口（本机只有 3067），加第二个代理时改环境变量即可，不用改代码
PROXIES = [p.strip() for p in
           os.environ.get("NETFORCE_PROXIES", PROXY).split(",") if p.strip()]

# 每域并发上限。抓 wiki 这类单站批量页时，6 个 worker 全打一个域 = 6 并发，容易被风控
HOST_CONCURRENCY = int(os.environ.get("NETFORCE_HOST_CONC", "3"))

# 风控状态码。567 是 wiki.biligame 这类站点的瞬时限流码（不是标准码），
# 412 是 bilibili 系的风控码 —— 这两个不认识的后果是「被拦了还当成功重试」。
RISK_CODES = (403, 412, 429, 503, 567)

# 服务端的「确定答复」：目标页面不存在 / 已失效 / 无权访问。
# 拿到这类码说明通道是通的，换通道重试没有意义，只会把 1 个请求放大成 10 个。
# 必须与 RISK_CODES 严格分开：混在一起会让通道熔断被正常 404 误触发。
DEFINITIVE = (404, 410)

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# cert 关闭后 requests/urllib3 会刷 InsecureRequestWarning，后台批量跑时污染日志
try:
    import warnings
    import urllib3
    urllib3.disable_warnings()
    warnings.filterwarnings("ignore", message=".*Unverified HTTPS.*")
except Exception:
    pass


# ---------------------------------------------------------------- 结果对象
@dataclass
class FetchResult:
    url: str
    ok: bool = False
    status: int = 0
    size: int = 0
    final_url: str = ""
    channel: str = ""          # proxy / direct / curl_cffi / curl
    elapsed: float = 0.0
    text: str = ""
    data: bytes = b""
    error: str = ""
    cached: bool = False
    hops: list = field(default_factory=list)   # 跳转链 [(code, url), ...]

    def __bool__(self):
        return self.ok

    def brief(self) -> str:
        h = f" hop{len(self.hops)}" if self.hops else ""
        return (f"{'✓' if self.ok else '✗'} {self.channel or '-'} "
                f"{self.status or '-'} {self.size}B {self.elapsed:.2f}s{h} {self.url[:60]}")


# ---------------------------------------------------------------- 令牌桶
class _Bucket:
    """按域名独立的令牌桶：同一域两次请求至少间隔 gap 秒。

    与全局 sleep 的区别：抓 8 个站时，慢站只会拖慢它自己，其余站照常并发。
    """

    def __init__(self):
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def take(self, host: str, gap: float) -> None:
        """占一个令牌，必要时等待。

        间隔带 0.85~1.3 倍抖动：严格等间隔的请求序列在网络侧看是标准方波，
        比随机间隔更容易被识别。抖动幅度刻意不大，保证总时长仍可预估。
        """
        with self._lock:
            now = time.monotonic()
            due = self._next.get(host, 0.0)
            wait = max(0.0, due - now)
            self._next[host] = max(now, due) + gap * random.uniform(0.85, 1.3)
        if wait > 0:
            time.sleep(wait)


_BUCKET = _Bucket()


EGRESS_COOLDOWN = 60.0      # 出口被判风控后冷却多久
EGRESS_TRIP = 3             # 连续几次风控才冷却（单次可能是偶发）


class _EgressPool:
    """出口健康度与选路。

    规则刻意保守：**同域粘一个出口，只在它被风控时才换**。
    逐请求轮换会让目标站看到「同一身份在多个 IP 间瞬移」，比固定 IP 更可疑。

    冷却到期自动恢复 —— 不把出口永久判死。一个出口可能只是被临时限流，
    永久拉黑等于把自己能用的路一条条封掉。
    """

    def __init__(self):
        self._stat: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _row(self, px: str) -> dict:
        return self._stat.setdefault(px, {"ok": 0, "block": 0, "until": 0.0})

    def pick(self, host: str) -> str:
        if not PROXIES:
            return ""
        now = time.monotonic()
        base = PROXIES[zlib.crc32((host or "").encode()) % len(PROXIES)]
        with self._lock:
            if self._row(base)["until"] < now:
                return base                       # 首选健康，保持粘性
            for px in PROXIES:                     # 首选冷却中，找替代
                if self._row(px)["until"] < now:
                    return px
        return base                                # 全在冷却，仍用首选

    def on_ok(self, px: str) -> None:
        if not px:
            return
        with self._lock:
            r = self._row(px)
            r["ok"] += 1
            r["block"] = 0

    def on_block(self, px: str) -> None:
        if not px:
            return
        with self._lock:
            r = self._row(px)
            r["block"] += 1
            if r["block"] >= EGRESS_TRIP:
                r["until"] = time.monotonic() + EGRESS_COOLDOWN
                r["block"] = 0

    def report(self) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            out = []
            for px in PROXIES:
                r = self._row(px)
                left = max(0.0, r["until"] - now)
                out.append({"proxy": px, "ok": r["ok"], "block": r["block"],
                            "cool_left": round(left, 1), "usable": left <= 0})
            return out

    def reset(self) -> None:
        with self._lock:
            self._stat.clear()


EGRESS = _EgressPool()


def egress_for(host: str) -> str:
    """给域名挑出口。委托 EGRESS.pick：健康时粘性，被风控时切到可用出口。"""
    return EGRESS.pick(host)


class _SessionPool:
    """按 (线程, 域) 缓存 HTTP 会话 —— 复用 TCP 连接与 TLS 握手。

    为什么要按线程分：curl_cffi 的 Session 不是线程安全的，多线程共用一个会串包。
    为什么要按域分：连接复用的前提是同一个 host；跨域复用反而多做一次握手。

    后端按能力分档（装了哪个用哪个，都不是硬依赖）：
      · curl_cffi.Session  → Chrome TLS 指纹 + 连接复用（最强，能过 JA3/JA4 识别）
      · requests.Session   → 连接池复用（够用，走 urllib3 的 keep-alive）
      · 都没有             → 返回 None，调用方退回 urllib 无复用通道

    实测意义：碧蓝 891 角色 × 每角色几十个 mp3 ≈ 数万次请求，
    不复用的话每次都要重做 TLS 握手（走代理更贵，握手要过两跳）。
    """

    TTL = 180.0          # 会话空闲 3 分钟就回收，避免长跑后句柄泄漏

    def __init__(self):
        self._pool: dict[tuple, tuple] = {}      # (tid, host) -> (kind, session, last_used)
        self._backend: Optional[str] = None      # 探测结果缓存，避免每次都 import
        self._lock = threading.Lock()

    def backend(self) -> Optional[str]:
        if self._backend is None:
            try:
                import curl_cffi  # noqa: F401
                self._backend = "curl_cffi"
            except Exception:
                try:
                    import requests  # noqa: F401
                    self._backend = "requests"
                except Exception:
                    self._backend = ""
        return self._backend or None

    def _new(self, host: str = ""):
        kind = self.backend()
        if kind == "curl_cffi":
            from curl_cffi import requests as cr
            return "curl_cffi", cr.Session(impersonate="chrome")
        if kind == "requests":
            import requests
            s = requests.Session()
            s.verify = False                      # 与 _CTX 的 CERT_NONE 保持一致
            s.headers.update(headers_for(host))   # 同域固定一套指纹，不逐请求换
            return "requests", s
        return None

    def get(self, host: str):
        """取一个本线程到该域的会话；没有可用后端时返回 None。"""
        key = (threading.get_ident(), host)
        now = time.monotonic()
        with self._lock:
            hit = self._pool.get(key)
            if hit and now - hit[2] < self.TTL:
                self._pool[key] = (hit[0], hit[1], now)
                return hit[0], hit[1]
            if hit:
                try:
                    hit[1].close()
                except Exception:
                    pass
                self._pool.pop(key, None)
        made = self._new(host)
        if made is None:
            return None
        with self._lock:
            self._pool[key] = (made[0], made[1], now)
        return made

    def drop(self, host: str) -> None:
        """丢掉本线程到该域的会话（连接坏了、被重置时用）。"""
        key = (threading.get_ident(), host)
        with self._lock:
            hit = self._pool.pop(key, None)
        if hit:
            try:
                hit[1].close()
            except Exception:
                pass

    def sweep(self) -> int:
        """清掉过期会话，返回清掉的个数。批量任务的收尾阶段调一次即可。"""
        now = time.monotonic()
        n = 0
        with self._lock:
            for k, (kind, s, t) in list(self._pool.items()):
                if now - t >= self.TTL:
                    try:
                        s.close()
                    except Exception:
                        pass
                    self._pool.pop(k, None)
                    n += 1
        return n

    def close_all(self) -> None:
        with self._lock:
            for _k, _s, _t in self._pool.values():
                try:
                    _s.close()
                except Exception:
                    pass
            self._pool.clear()


SESSIONS = _SessionPool()


class _HostGate:
    """每域并发闸门：同一个域同时最多 N 个在途请求。

    与令牌桶的分工：
      · 令牌桶管「两次请求之间隔多久」（频率）
      · 本闸门管「同时有几个在飞」（并发）
    只限频率不限并发时，6 个 worker 会在间隔到点的瞬间一起冲，瞬时并发仍是 6。
    """

    def __init__(self, n: int):
        self.n = max(1, n)
        self._sems: dict[str, threading.Semaphore] = {}
        self._lock = threading.Lock()

    def _sem(self, host: str) -> threading.Semaphore:
        with self._lock:
            s = self._sems.get(host)
            if s is None:
                s = threading.Semaphore(self.n)
                self._sems[host] = s
            return s

    def __call__(self, host: str):
        return self._sem(host)


HOSTGATE = _HostGate(HOST_CONCURRENCY)


class _ChanHealth:
    """通道级熔断，按「通道 @ 域」分桶。

    为什么需要：通道顺序是固定的，如果某个通道在本机环境下根本不可用
    （依赖没装、代理挂了），每个请求都要先在它身上白等一次超时。
    连续失败到阈值就暂时跳过，冷却后自动半开重试。

    两个吃过大亏的细节：
      · **只算通道自己的病**（连接异常 / 风控码）。服务器明确回了 404，说明
        这条通道完全通畅，那是目标页面不存在，不该记到通道头上。早期版本把
        404 也记一笔，三个不存在的页面就把 session 通道焊死，同进程后面 12 个
        站全部变 no channel —— 站没坏，是被自己的熔断连坐了。
      · **分域记账**：同一通道在 A 站被 WAF 挡，不代表在 B 站不可用。键是
        name@host；不传 host 时退化为全局键，兼容老调用。
    """

    COOLDOWN = 90.0

    def __init__(self, trip: int = 3, cooldown: float = COOLDOWN):
        self.trip = trip
        self.cooldown = cooldown
        self._fail: dict[str, int] = {}
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(name: str, host: str = "") -> str:
        return f"{name}@{host}" if host else name

    def ok(self, name: str, host: str = "") -> None:
        k = self._key(name, host)
        with self._lock:
            self._fail[k] = 0
            self._until.pop(k, None)

    def bad(self, name: str, host: str = "") -> None:
        k = self._key(name, host)
        with self._lock:
            n = self._fail.get(k, 0) + 1
            self._fail[k] = n
            if n >= self.trip:
                self._until[k] = time.monotonic() + self.cooldown

    def skip(self, name: str, host: str = "") -> bool:
        k = self._key(name, host)
        with self._lock:
            until = self._until.get(k)
            if not until:
                return False
            if time.monotonic() >= until:        # 冷却到期 ⇒ 半开，放一发试探
                self._until.pop(k, None)
                self._fail[k] = 0
                return False
            return True

    def reset(self) -> None:
        with self._lock:
            self._fail.clear()
            self._until.clear()

    def report(self) -> dict:
        now = time.monotonic()
        with self._lock:
            return {k: v for k, v in self._fail.items()
                    if v and self._until.get(k, 0) > now}


CHANS = _ChanHealth()


class _Throttle:
    """全局风控退避（跨域名）。

    单站限速解决「别把某个站打爆」，这里解决「整条线已经被盯上」：
      · 连续拿到 403/429/503 ⇒ 全局因子 ×1.7（上限 8 倍）
      · 连续成功           ⇒ 因子 ×0.92 缓慢回落（不瞬间放开，避免刚恢复就再撞墙）
    与 resource_finder 的 AutoThrottle 同思路：**只准调慢，快速调快会重新触发风控**。
    """

    MAX = 8.0

    def __init__(self):
        self.factor = 1.0
        self.streak = 0
        self.total_blocked = 0
        self._lock = threading.Lock()

    def on_block(self) -> float:
        with self._lock:
            self.streak += 1
            self.total_blocked += 1
            if self.streak >= 2:                      # 单次偶发不降速
                self.factor = min(self.MAX, self.factor * 1.7)
            return self.factor

    def on_ok(self) -> float:
        with self._lock:
            self.streak = 0
            self.factor = max(1.0, self.factor * 0.92)
            return self.factor

    def gap(self, base: float) -> float:
        return base * self.factor

    def report(self) -> dict:
        return {"factor": round(self.factor, 2), "streak": self.streak,
                "blocked": self.total_blocked}


THROTTLE = _Throttle()



# ---------------------------------------------------------------- 统计
class Stats:
    def __init__(self):
        self.n = self.ok = self.fail = self.blocked = 0
        self.bytes = 0
        self.by_channel: dict[str, int] = {}
        self.by_host: dict[str, list] = {}
        self._lock = threading.Lock()

    def add(self, r: FetchResult) -> None:
        with self._lock:
            self.n += 1
            if r.ok:
                self.ok += 1
                self.bytes += r.size
                self.by_channel[r.channel] = self.by_channel.get(r.channel, 0) + 1
                host = urllib.parse.urlparse(r.url).netloc
                row = self.by_host.setdefault(host, [0, 0])
                row[0] += 1
                row[1] += r.size
            else:
                self.fail += 1
                if r.status in RISK_CODES:
                    self.blocked += 1

    def report(self) -> dict:
        return {
            "req": self.n, "ok": self.ok, "fail": self.fail, "blocked": self.blocked,
            "MB": round(self.bytes / 1048576, 2),
            "channel": dict(self.by_channel),
            "hosts": len(self.by_host),
        }


STATS = Stats()


# ---------------------------------------------------------------- 通道实现
def _hidden_startupinfo():
    """Windows 隐藏控制台的三件套之一。每次必须新建实例（复用会失效）。"""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def _decompress(raw: bytes, enc: str) -> bytes:
    enc = (enc or "").lower()
    try:
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        pass
    return raw


class _RedirectRecorder(urllib.request.HTTPRedirectHandler):
    """记录跳转链。urllib 默认静默跟随 302，事后无法判断
    「这个子站是不是跳回主站了」——而软 404 恰恰长这样。"""

    def __init__(self):
        super().__init__()
        self.chain: list = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.chain.append((code, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _via_urllib(url: str, timeout: float, use_proxy: bool) -> FetchResult:
    rec = _RedirectRecorder()
    handlers = [rec, urllib.request.HTTPSHandler(context=_CTX)]
    if use_proxy:
        handlers.insert(0, urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    else:
        handlers.insert(0, urllib.request.ProxyHandler({}))
    op = urllib.request.build_opener(*handlers)
    host = urllib.parse.urlparse(url).netloc
    hdr = headers_for(host)
    hdr["Connection"] = "close"
    req = urllib.request.Request(url, headers=hdr)
    r = op.open(req, timeout=timeout)
    raw = r.read()
    raw = _decompress(raw, r.headers.get("Content-Encoding", ""))
    return FetchResult(
        url=url, ok=True, status=getattr(r, "status", 200), size=len(raw),
        final_url=r.geturl(), channel="proxy" if use_proxy else "direct",
        data=raw, text=raw.decode("utf-8", "ignore"), hops=rec.chain,
    )


def _via_session(url: str, timeout: float) -> FetchResult:
    """会话复用通道：能复用的连接就不重新握手。

    后端自动选（curl_cffi > requests）。有 curl_cffi 时顺带拿 Chrome TLS 指纹，
    过那些认 JA3 的 WAF；只有 requests 时纯做连接复用，对普通站足够。
    """
    host = urllib.parse.urlparse(url).netloc
    px = egress_for(host)
    proxies = {"http": px, "https": px} if px else None
    item = SESSIONS.get(host)
    if not item:
        return FetchResult(url=url, ok=False, error="no session backend", channel="session")
    kind, sess = item
    try:
        r = sess.get(url, proxies=proxies, timeout=timeout, headers=headers_for(host))
        status = getattr(r, "status_code", 0)
        body = r.content or b""
        if status < 400 and body:
            # requests / curl_cffi 都会把跳转链存在 history 里，转成统一格式
            hops = [(h.status_code, str(h.headers.get("Location", "") or h.url))
                    for h in getattr(r, "history", []) or []]
            return FetchResult(url=url, ok=True, status=status, size=len(body),
                               final_url=str(getattr(r, "url", url)),
                               channel=kind, data=body,
                               text=body.decode("utf-8", "ignore"), hops=hops)
        if status in (0, 502, 503, 504):
            SESSIONS.drop(host)                  # 连接坏了，下次重建
        return FetchResult(url=url, ok=False, status=status, error=f"HTTP {status}",
                           channel=kind)
    except Exception as e:                       # noqa: BLE001
        SESSIONS.drop(host)
        return FetchResult(url=url, ok=False, error=f"{type(e).__name__}: {str(e)[:60]}",
                           channel="session")


def _via_curl_cffi(url: str, timeout: float) -> FetchResult:
    """兼容旧调用名。现在实际由 _via_session 承担（curl_cffi 降为可选后端）。"""
    return _via_session(url, timeout)


def _via_curl(url: str, timeout: float) -> FetchResult:
    """子进程 curl 兜底通道。

    🔴 必须校验状态码：这行以前只看 returncode + stdout 非空，
    结果把 WAF 的 567 错误页（7KB 的拦截页）当成正文返回了 ——
    下游拿到一坨垃圾还以为抓成功。用 -o 落临时文件 + -w 取真实状态码。
    """
    import tempfile
    host = urllib.parse.urlparse(url).netloc
    ua = ua_for(host)
    last_status = 0
    for extra in (["-x", PROXY], []):
        path = None
        try:
            fd, path = tempfile.mkstemp(suffix=".bin")
            os.close(fd)
            p = subprocess.run(
                ["curl", "-s", "-L", "--compressed", "-A", ua,
                 "-H", f"Accept-Language: {_fingerprint(host)[1]}",
                 "-o", path, "-w", "%{http_code}",
                 "--max-time", str(max(1, int(timeout)))] + extra + [url],
                capture_output=True, timeout=timeout + 5,
                stdin=subprocess.DEVNULL,
                startupinfo=_hidden_startupinfo(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            code = int((p.stdout or b"").strip() or 0)
            last_status = code
            body = Path(path).read_bytes()
            if p.returncode == 0 and 200 <= code < 300 and body:
                return FetchResult(url=url, ok=True, status=code, size=len(body),
                                   final_url=url, channel="curl", data=body,
                                   text=body.decode("utf-8", "ignore"))
        except Exception:                        # noqa: BLE001
            continue
        finally:
            if path:
                try:
                    os.unlink(path)
                except Exception:
                    pass
    return FetchResult(url=url, ok=False, status=last_status,
                       error=f"curl http {last_status}", channel="curl")


def _via_doh(url: str, timeout: float) -> FetchResult:
    """DoH 兜底通道：用 DoH 解析出的 IP 直连，绕开被污染的系统 DNS。

    为什么不自己在 socket 层做：curl 原生支持 `--resolve host:port:ip`，
    按 IP 连、按域名发 SNI 与 Host 头，这正是我们要的；
    手写 TLS + Host 头容易在 SNI、证书、HTTP/1.1 细节上翻车。

    只在其他通道全失败时才试 —— 它必须直连（代理会自己解析域名，DoH 就没意义了），
    所以对墙外的站它反而不如代理通道，定位是「直连被 DNS 带偏」这一种故障的专用解。
    """
    from . import dns
    import tempfile
    rr = dns.resolve_for_curl(url, timeout=min(6.0, timeout))
    if not rr:
        return FetchResult(url=url, ok=False, error="doh 未解析到 IP", channel="doh")
    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".bin")
        os.close(fd)
        p = subprocess.run(
            ["curl", "-s", "-L", "--compressed", "-A", UA,
             "--resolve", rr, "-o", path, "-w", "%{http_code}",
             "--max-time", str(max(1, int(timeout))), url],
            capture_output=True, timeout=timeout + 5,
            stdin=subprocess.DEVNULL,
            startupinfo=_hidden_startupinfo(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        code = int((p.stdout or b"").strip() or 0)
        body = Path(path).read_bytes()
        if p.returncode == 0 and 200 <= code < 300 and body:
            return FetchResult(url=url, ok=True, status=code, size=len(body),
                               final_url=url, channel="doh", data=body,
                               text=body.decode("utf-8", "ignore"))
        return FetchResult(url=url, ok=False, status=code,
                           error=f"doh http {code}", channel="doh")
    except Exception as e:                       # noqa: BLE001
        return FetchResult(url=url, ok=False, channel="doh",
                           error=f"{type(e).__name__}: {str(e)[:50]}")
    finally:
        if path:
            try:
                os.unlink(path)
            except Exception:
                pass


# ---------------------------------------------------------------- GitHub 镜像
# 为什么单独做一层：raw.githubusercontent.com 在本机经常是**整段连接重置**
# （status=0，不是 404）。这种失败换 UA、换通道、换出口都没用——必须换主机名。
#
# 实测依据（2026-10-21 装 Nous Hermes 时踩出来的）：四次安装全部死在这一个域名上
# （os error 10054 / 超时），而同一时刻 api.github.com、codeload.github.com、
# cdn.jsdelivr.net 都是通的。也就是说「GitHub 不通」这个判断是错的，
# **只有 raw 那个域名不通**。抓 awesome-list 的 24 个种子全指向它，
# 一旦被墙，整条资源发现产线等于停摆——但表面上只会表现为「24 个种子全失败」。
#
# 只用纯 CDN（jsdelivr / statically）：它们直接读仓库内容、本身不做中转。
# 🔴 不挂 gh-proxy 这类反向代理——本机已有代理，再套一层只慢不快，而且反代
#    自身稳定性差（用户 2026-10-12 定的规矩）。
#
# 代价：CDN 有缓存（jsdelivr 默认约 12 小时），刚 push 的 README 可能拿到旧版。
# 所以镜像**只在原站彻底不通时才用**，绝不作为默认路径。
_GH_RAW_RE = re.compile(
    r"^https?://raw\.githubusercontent\.com/"
    r"(?P<user>[^/]+)/(?P<repo>[^/]+)/(?P<ref>[^/]+)/(?P<path>.+)$", re.I)

# 记住「raw 原站在本机已判定不通」。判定之后，后续 raw 请求直接先走镜像，
# 省掉每个 URL 都先等一次连接重置的代价（一次重置十几秒，24 个种子就是几分钟）。
_GH_RAW_BLOCKED = False
_GH_LOCK = threading.Lock()


def gh_mirror_urls(url: str) -> list:
    """把 GitHub raw 地址展开成候选表；非 raw 地址原样返回单元素。

    顺序 = 原站 → jsdelivr → statically。原站走本机代理通常最快，所以它始终
    排第一；镜像只承担兜底。调用方拿到的是**同一份内容的三条路径**。
    """
    m = _GH_RAW_RE.match(url)
    if not m:
        return [url]
    user, repo, ref = m.group("user"), m.group("repo"), m.group("ref")
    path = m.group("path")
    q = urllib.parse.urlsplit(url).query
    tail = ("?" + q) if q else ""
    return [
        url,
        f"https://cdn.jsdelivr.net/gh/{user}/{repo}@{ref}/{path}{tail}",
        f"https://cdn.statically.io/gh/{user}/{repo}/{ref}/{path}{tail}",
    ]


def _mirror_name(alt: str) -> str:
    host = urllib.parse.urlsplit(alt).netloc
    if host.startswith("cdn.jsdelivr.net"):
        return "mirror:jsdelivr"
    if host.startswith("cdn.statically.io"):
        return "mirror:statically"
    return "mirror"


def _mark_raw_blocked() -> None:
    """记录一次「raw 原站确认不通」。用函数封装而不是就地 global，
    是因为 global 必须出现在函数体内该名字的所有使用之前，就地写容易踩错位置。"""
    global _GH_RAW_BLOCKED
    with _GH_LOCK:
        _GH_RAW_BLOCKED = True


# ---------------------------------------------------------------- 主入口
def fetch_text(url: str, timeout: float = 15.0, gap: float = 0.8,
               retry: int = 2, force_channel: Optional[str] = None,
               budget: float = 25.0) -> FetchResult:
    """取文本。返回 FetchResult，永不大抛异常（调用方只看 .ok）。

    gap    ：同域名最小间隔（秒）。批量抓同一站时靠它自动限速。
    budget ：**单次抓取的总时间上限**。没有它的话最坏耗时 =
             通道数(4) × 重试(2) × timeout(15) = 120s，一个死链能卡死整批。
             超预算即返回，宁可判失败也不拖住流水线。

    GitHub raw 地址额外走一层镜像降级：原站整段不通（被墙）时自动换 CDN，
    换成功的结果仍以**原始 URL** 回报，调用方不需要知道镜像存在。
    """
    if not url.lower().startswith(("http://", "https://")):
        return FetchResult(url=url, error="bad scheme")
    host = urllib.parse.urlparse(url).netloc

    mirrors = gh_mirror_urls(url)
    if len(mirrors) == 1 or force_channel:
        with HOSTGATE(host):                          # 同域在途数封顶
            return _fetch_inner(url, host, timeout, gap, retry, force_channel, budget)

    # 已判定 raw 原站不通 → 先打镜像，省掉必然的等待；镜像也全挂再回原站试一把。
    if _GH_RAW_BLOCKED:
        mirrors = mirrors[1:] + mirrors[:1]

    first = None
    for i, alt in enumerate(mirrors):
        alt_host = urllib.parse.urlsplit(alt).netloc
        with HOSTGATE(alt_host):
            r = _fetch_inner(alt, alt_host, timeout, gap, retry, None,
                             budget if i == 0 else max(6.0, budget * 0.6))
        if first is None:
            first = r
        if r.ok and r.size > 0:
            if i > 0:                                 # 走到镜像才成功 = 原站确认不通
                _mark_raw_blocked()
                r.channel = _mirror_name(alt)
                r.url = url                            # 对外仍报原始地址
            return r
        if r.status in DEFINITIVE:                    # 404：内容真没了，换镜像同样没有
            r.url = url
            return r
        # status=0 且通道全挂 = 连接层就断了，这才是被墙的判据；403/429 只是风控，
        # 换镜像也没意义但也不会伤，继续试下一档即可。
    return first


def _fetch_inner(url: str, host: str, timeout: float, gap: float,
                 retry: int, force_channel: Optional[str],
                 budget: float) -> FetchResult:
    _BUCKET.take(host, THROTTLE.gap(gap))

    t0 = time.monotonic()
    deadline = t0 + budget
    last = FetchResult(url=url, error="no channel")
    # 顺序：复用通道打头（省握手），再走无复用的两档兜底，最后子进程 curl。
    channels = [force_channel] if force_channel else ["session", "urllib_proxy",
                                                     "urllib_direct", "curl", "doh"]
    stop = False
    for name in channels:
        if stop:
            break
        if CHANS.skip(name, host):               # 已熔断，本轮不再试
            continue
        for attempt in range(max(1, retry)):
            left = deadline - time.monotonic()
            if left < 1.0:
                stop = True
                break
            eff = min(timeout, left)
            try:
                if name == "session":
                    r = _via_session(url, eff)
                elif name == "urllib_proxy":
                    r = _via_urllib(url, eff, True)
                elif name == "urllib_direct":
                    r = _via_urllib(url, eff, False)
                elif name == "curl":
                    r = _via_curl(url, eff)
                elif name == "doh":
                    r = _via_doh(url, eff)
                elif name == "curl_cffi":
                    r = _via_session(url, eff)
                else:
                    continue
            except urllib.error.HTTPError as e:
                r = FetchResult(url=url, ok=False, status=e.code, error=f"HTTP {e.code}")
            except Exception as e:                   # noqa: BLE001
                r = FetchResult(url=url, ok=False, error=f"{type(e).__name__}: {str(e)[:60]}")
            last = r
            if r.ok and r.size > 0:
                r.elapsed = round(time.monotonic() - t0, 3)
                CHANS.ok(name, host)
                EGRESS.on_ok(egress_for(host))
                THROTTLE.on_ok()
                STATS.add(r)
                return r
            if r.status in DEFINITIVE:
                # 服务端明确答复「没有这个页面 / 已失效」。换通道换个指纹也是同样
                # 结果，死磕只是把 1 个请求放大成 10 个。通道本身是通的，记健康。
                r.elapsed = round(time.monotonic() - t0, 3)
                CHANS.ok(name, host)
                THROTTLE.on_ok()
                STATS.add(r)
                return r
            # 到这里才算通道可疑：连接异常（status=0）或风控码。
            CHANS.bad(name, host)
            if r.status in RISK_CODES:
                EGRESS.on_block(egress_for(host))    # 记到这个出口头上，够数就冷却
                THROTTLE.on_block()                  # 整条线降速，不只这个域
                break                                # 风控：换下一通道，别在同通道死磕
            time.sleep(min(6.0, 0.8 * (2 ** attempt)))
    last.elapsed = round(time.monotonic() - t0, 3)
    if last.status in RISK_CODES or last.status == 0:
        THROTTLE.on_block()
    STATS.add(last)
    return last


def fetch_json(url: str, **kw) -> dict:
    r = fetch_text(url, **kw)
    if not r.ok:
        return {}
    try:
        return json.loads(r.text)
    except Exception:
        return {}


def download(url: str, dest: Path, timeout: float = 60.0, gap: float = 0.3,
             max_bytes: int = 200 * 1024 * 1024, dedupe: bool = True) -> FetchResult:
    """流式下载并落盘，**真** Range 续传；已存在且非空则直接跳过（幂等）。

    续传细节：中断后 `.part` 保留不删，下次带 `Range: bytes=<已有字节>-` 续；
    服务端回 206 就追加，回 200 说明它不支持 Range 或文件变了，从头覆写。

    dedupe：落盘后按内容 sha256 查重，同内容只留一份实体，其余做成硬链接。
    音频这类资源不同 URL 常常指向同一份字节，按 URL 判重看不出来。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return FetchResult(url=url, ok=True, size=dest.stat().st_size,
                           channel="skip", cached=True)

    host = urllib.parse.urlparse(url).netloc
    tmp = dest.with_suffix(dest.suffix + ".part")
    have = tmp.stat().st_size if tmp.exists() else 0

    with HOSTGATE(host):
        _BUCKET.take(host, THROTTLE.gap(gap))
        res = _download_inner(url, host, tmp, dest, have, timeout, max_bytes)
    if res.ok and dedupe and res.channel != "skip":
        _dedupe_after(dest)
    return res


_FP_LOCK = threading.Lock()


def _dedupe_after(dest: Path) -> None:
    """按内容指纹去重：同字节只保留一份实体，别处改成硬链接。

    硬链接失败（跨盘/权限）就退回复制 —— 宁可多占盘，也不能让文件消失。
    整个过程不抛异常：去重是优化，不该让一次成功下载变成失败。
    """
    try:
        from . import store
        sha = store.sha256_file(dest)
        con = store.connect()
        try:
            with _FP_LOCK:
                exist = store.blob_find(con, sha)
                if exist and Path(exist).resolve() != dest.resolve():
                    try:
                        dest.unlink()
                        os.link(exist, dest)
                    except Exception:
                        try:
                            import shutil
                            shutil.copy2(exist, dest)
                        except Exception:
                            pass
                store.blob_add(con, sha, dest, dest.stat().st_size if dest.exists() else 0)
        finally:
            con.close()
    except Exception:
        pass


def _referer_for(url: str) -> str:
    """Referer 按目标站根域推，别硬编码 —— 下别站的图/音频时硬编码会被防盗链挡。"""
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}/" if p.netloc else ""


def _download_inner(url: str, host: str, tmp: Path, dest: Path, have: int,
                    timeout: float, max_bytes: int) -> FetchResult:
    t0 = time.monotonic()
    px = egress_for(host)
    headers = headers_for(host, referer=_referer_for(url))
    headers.update({"Accept": "*/*", "Connection": "close"})
    if have > 0:
        headers["Range"] = f"bytes={have}-"

    try:
        op = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": px, "https": px} if px else {}),
            urllib.request.HTTPSHandler(context=_CTX))
        req = urllib.request.Request(url, headers=headers)
        with op.open(req, timeout=timeout) as r:
            status = getattr(r, "status", 200)
            append = (status == 206 and have > 0)
            if status == 200:
                have = 0                      # 服务端不支持续传，从头来
            total = have
            mode = "ab" if append else "wb"
            with open(tmp, mode) as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("exceed max_bytes")
                    f.write(chunk)
        if tmp.stat().st_size == 0:
            raise ValueError("empty body")
        tmp.replace(dest)
        EGRESS.on_ok(px)
        res = FetchResult(url=url, ok=True, status=status, size=total, final_url=url,
                          channel="resume" if append else "download",
                          elapsed=round(time.monotonic() - t0, 3))
    except Exception as e:                           # noqa: BLE001
        # 🔴 不删 .part：留着下次续传。删了等于每次中断都从头下。
        if getattr(e, "code", 0) in RISK_CODES:
            EGRESS.on_block(px)
        res = FetchResult(url=url, ok=False, error=f"{type(e).__name__}: {str(e)[:50]}",
                          channel="download", elapsed=round(time.monotonic() - t0, 3))
    STATS.add(res)
    return res


def parallel(urls, fn, workers: int = 6):
    """极简并发池：保持顺序返回结果，单点异常不影响其他任务。"""
    from concurrent.futures import ThreadPoolExecutor
    out = [None] * len(urls)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, u): i for i, u in enumerate(urls)}
        for f, i in futs.items():
            try:
                out[i] = f.result()
            except Exception as e:                   # noqa: BLE001
                out[i] = e
    return out
