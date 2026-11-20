# -*- coding: utf-8 -*-
"""netforce / netpolicy.py —— 统一出网策略（把天网 skynet/net.py 的做法抽成公共层）

一句话：**默认直连；代理只是可选的一条兜底通道，不是前提。**

为什么要有这一层（2026-11-04 实测）：
    主爬虫 resource_finder.py 与 skynet/net.py 早就改成「NETFORCE_PROXY 为空 = 全程直连」，
    但 classify.py / jsprobe.py / probe_sites.py 还硬编码 http://127.0.0.1:7890。
    而当天 7890、3067 两个端口都没在监听 ⇒ 静态分类每条先撞一次死代理，
    无头补抓更是 100% 走死代理 ⇒ 38608/41320 条卡在「JS渲染/无静态内容」。

本模块对外只暴露几件小事，所有抓取方共用同一套判断：
    channels(host)      按域名给出有序出口：直连 → 活着的代理（可配置反过来）
    openers(host)       urllib opener 列表（**ProxyHandler({}) 显式禁用系统代理 = 真直连**）
    fetch_text(url)     静态多通道抓取，返回 (text, channel, status, err)
    browser_*           给 Playwright 的直连/代理两套启动参数
    reload()            配置热加载：改 _netpolicy.json 不用重启进程

设计沿用天网的三条经验：
    ① 代理挂了就别每个请求都白试一轮 ⇒ 端口存活探测 + TTL 缓存 + 自动摘除/恢复；
    ② 同域按 hash 粘一个出口，不逐请求轮换 IP（IP 瞬移比固定 IP 更像机器人）；
    ③ 国内站套代理必挂（README 坑 1）⇒ 域名白名单强制直连，且请求带
       浏览器 UA + Referer + Accept-Language。
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE, "_netpolicy.json")

# 🔴 出向层不自己造：直接委托天网 fleet/net.py（主人 2026-11-03「融会贯通」那条）。
#    它是上个会话把 github.com 直连打通的地方：`_DIRECT = ProxyHandler({})` 真直连、
#    resolve_proxy() = 自有 7891 → 系统代理 → 直连、urlopen() 默认直连优先且
#    **只在网络层失败时**才回退代理（HTTP 4xx/5xx 不回退，那是业务结论）。
# 换机自举：默认从自身位置推导同级 skynet-fleet，没有就只靠内置实现，不留死路径。
FLEET_ROOT = os.environ.get("NETFORCE_FLEET_ROOT") or os.path.join(
    os.path.dirname(os.path.dirname(BASE)), "skynet-fleet")
_FLEET = None


def fleet():
    """延迟加载天网出向层。拿不到就返回 None，本模块退回内置实现（不阻塞主流程）。"""
    global _FLEET
    if _FLEET is not None:
        return _FLEET
    try:
        if FLEET_ROOT not in sys.path:
            sys.path.insert(0, FLEET_ROOT)
        from fleet import net as fnet
        _FLEET = fnet
    except Exception:
        _FLEET = False
    return _FLEET or None

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
UA_CURL = UA

DEFAULT = {
    # direct = 直连优先、代理兜底；proxy = 代理优先、直连兜底
    "prefer": "direct",
    "proxy": "",                       # 单出口；空字符串 = 不启用代理
    "proxies": [],                     # 多出口（逗号分隔或 JSON 数组），空则回落到 proxy
    # 强制直连的域名后缀/关键字：国内站走代理常吃 567 空壳
    "direct_domains": [
        ".cn", ".com.cn", ".gov.cn", ".edu.cn", ".org.cn", ".net.cn",
        "localhost", "127.0.0.1",
        "bilibili.com", "wiki.biligame.com", "baidu.com", "qq.com", "weixin.qq.com",
        "aliyun.com", "gitee.com", "huaweicloud.com", "tencent.com", "zhihu.com",
        "douyin.com", "csdn.net", "163.com", "sina.com.cn", "jd.com", "taobao.com",
        "douban.com", "sogou.com", "360.cn", "mi.com", "huawei.com",
    ],
    "proxy_domains": [],               # 可选：这些域名强制走代理（填了且代理活着才生效）
    # 域专用代理：proxy_domains 命中的域名走这里，探测活着才生效，死了回落直连。
    # 2026-11-20 Steam 场景新增（主人临时代理，过期自动回落，不留死配置）。
    "proxy_domains_proxy": "",
    "probe_ttl": 30.0,                # 代理存活探测结果缓存秒数
    "probe_timeout": 0.35,            # 单次 connect 超时（别让死代理拖住整批）
    "timeout": 8.0,                   # 静态抓取单通道超时
    "browser_timeout": 15.0,          # 无头渲染单页超时
    "use_fleet": True,                # 出向选路委托 fleet/net.py（关掉才用内置兜底）
    "self_kernel": False,             # 是否按需拉起天网自有 sing-box 内核（7891）
    "use_direct_matrix": True,        # 用 netprobe 实测的直连矩阵跳过已知不通的域
    # 并发模式：保守=常驻省资源、和主爬虫和平共处；堆积超阈值自动切激进，
    # 把整机多核 CPU + 独显 GPU 全拉满（workers=浏览器进程数=真多核并行）。
    "concurrency": {
        "auto_threshold": 3000,        # 待解析域名 > 此值 ⇒ 自动激进
        "conservative": {"workers": 2, "tabs": 3, "gpu": False},
        "aggressive":   {"workers": 5, "tabs": 6, "gpu": True},
    },
    # 每个域抓完首页后，再往下挖几个站内文章页（0 = 关闭，退回只抓首页）。
    # 只有「内容够厚」的页面才展开，且只挑像文章的 URL（见 render_parse.site_urls）。
    "deep_per_site": 8,
}

_state = {"mtime": -1.0, "cfg": dict(DEFAULT)}
_alive_cache = {}                      # px -> (bool, checked_at)
_lock = threading.Lock()
_stats = {"direct_ok": 0, "proxy_ok": 0, "fail": 0, "probe_skip": 0}
_pending_cache = [0.0, 0]            # [resources.json mtime, pending_count] 估算缓存


# ---------------------------------------------------------------- 配置热加载
def _file_cfg():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        return {**DEFAULT, **{k: v for k, v in raw.items() if k in DEFAULT}}
    except Exception:
        return dict(DEFAULT)


def cfg():
    """读当前生效配置。配置文件 mtime 变了就自动热加载，环境变量优先级最高。

    热加载的意义：爬虫/渲染器是长驻进程，改代理地址、改分流名单不该要求重启
    （重启会丢内存队列）。改 _netpolicy.json 后下一轮请求自动生效。
    """
    try:
        mt = os.path.getmtime(CONFIG_FILE)
    except Exception:
        mt = -1.0
    with _lock:
        if mt != _state["mtime"]:
            _state["cfg"] = _file_cfg()
            _state["mtime"] = mt
        c = dict(_state["cfg"])
    # 环境变量覆盖（含「显式置空」的语义）
    if "NETFORCE_PROXY" in os.environ:
        c["proxy"] = os.environ["NETFORCE_PROXY"].strip()
    if "NETFORCE_PROXIES" in os.environ:
        c["proxies"] = [p.strip() for p in
                        os.environ["NETFORCE_PROXIES"].split(",") if p.strip()]
    if "NETFORCE_PREFER" in os.environ:
        c["prefer"] = os.environ["NETFORCE_PREFER"].strip().lower()
    if "NETFORCE_DIRECT_DOMAINS" in os.environ:
        c["direct_domains"] = list(c["direct_domains"]) + [
            p.strip() for p in os.environ["NETFORCE_DIRECT_DOMAINS"].split(",") if p.strip()]
    if "NETFORCE_PROXY_DOMAINS" in os.environ:
        c["proxy_domains"] = list(c["proxy_domains"]) + [
            p.strip() for p in os.environ["NETFORCE_PROXY_DOMAINS"].split(",") if p.strip()]
    return c


def reload():
    """强制下一轮重新读配置文件（也可由外部显式调用）。"""
    with _lock:
        _state["mtime"] = -1.0
        _alive_cache.clear()
    return cfg()


def describe():
    c = cfg()
    return {
        "prefer": c["prefer"],
        "configured_proxies": _all_proxies(c),
        "alive_proxies": available_proxies(),
        "direct_domains": len(c["direct_domains"]),
        "proxy_domains": len(c["proxy_domains"]),
        "config_file": CONFIG_FILE if os.path.exists(CONFIG_FILE) else "(内置默认)",
        "fleet_root": FLEET_ROOT if fleet() else "(未接天网出向层)",
        "default_egress": "direct (真直连 / 直连国外)" if not egress_for() else egress_for(),
        "self_kernel_candidates": self_candidates(),   # 可选能力，默认不启用
        "system_proxy": "禁用：不读取、不启用（不碰主人本地端口）",
        "stats": dict(_stats),
    }


# ---------------------------------------------------------------- 代理存活探测
def _all_proxies(c=None):
    c = c or cfg()
    out = [p.strip() for p in (c.get("proxies") or []) if p and p.strip()]
    if not out and c.get("proxy", "").strip():
        out = [c["proxy"].strip()]
    return list(dict.fromkeys(out))


def proxy_alive(px):
    """端口级存活探测 + TTL 缓存。死了的出口不进入通道列表，避免每请求白等超时。"""
    if not px:
        return False
    now = time.monotonic()
    with _lock:
        hit = _alive_cache.get(px)
    if hit and now - hit[1] < float(cfg().get("probe_ttl", 30.0)):
        return hit[0]
    ok = False
    try:
        u = urllib.parse.urlsplit(px if "://" in px else "http://" + px)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme in ("https", "socks5s") else 80)
        with socket.create_connection((host, port), timeout=float(cfg().get("probe_timeout", .35))):
            ok = True
    except Exception:
        ok = False
    with _lock:
        _alive_cache[px] = (ok, now)
    if not ok:
        _stats["probe_skip"] += 1
    return ok


def available_proxies():
    return [p for p in _all_proxies() if proxy_alive(p)]


def _match(host, rules):
    """后缀匹配，不做裸子串匹配（否则 ".cn" 会把 cnn.com 也判成国内站）。"""
    h = (host or "").lower().rstrip(".")
    for r in rules:
        r = r.strip().lower()
        if not r:
            continue
        if r.startswith("."):
            if h.endswith(r) or h == r[1:]:
                return True
        elif h == r or h.endswith("." + r):
            return True
    return False


def sticky(host, proxies):
    """同域粘一个出口（crc32 hash），不逐请求轮换。"""
    return proxies[zlib.crc32((host or "").encode()) % len(proxies)]


def self_candidates():
    """**只**认天网自有内核（proxycore 起的 sing-box，7891），绝不读系统代理。

    主人 2026-11-04：「本地直连本身就有代理功能……只是不让他们用咱们本地这个端口」。
    ⇒ `fleet.net.resolve_proxy(allow_system=False)` 恰好就是这条纪律的现成实现：
      自有 7891 活着 ⇒ 用项目自己的；没起 ⇒ 纯直连。**系统代理/用户端口一律不看。**
    """
    f = fleet()
    if not f:
        return []
    try:
        u, _why = f.resolve_proxy(allow_system=False)
        return [u] if u else []
    except Exception:
        return []


def browser_proxy():
    """给无头浏览器的**固定**出口：默认空 = 真直连（直连国外，就是那扇门）。

    · 默认            → ""，浏览器带 --no-proxy-server，纯直连
    · 远程显式指定    → 用配置里的 proxy，固定不切换
    · self_kernel:true → 用天网自有内核 7891（项目自己的，不是主人本地端口）
    绝不返回系统代理 / karing 3067 / 7890。
    """
    c = cfg()
    cand = [p for p in _all_proxies(c) if p]
    if cand:
        return cand[0]
    if c.get("self_kernel"):
        sc = self_candidates()
        return sc[0] if sc else ""
    return ""


def direct_matrix_flag(host):
    """netprobe 实测结论：True=该域普通直连不通、False=通、None=没测过。

    ⚠️ **只作诊断参考**：矩阵可能测自别的网络域（Bash 沙箱 ≠ 宿主机），
       不拿它决定出网路径、也不拿它缩短超时（会误杀能通的国外站）。
       真正要打通这类域走 direct_ip.py 的 DoH 真实 IP 直连增强。"""
    if not cfg().get("use_direct_matrix"):
        return None
    try:
        import netprobe
        return netprobe.needs_egress(host)
    except Exception:
        return None


def channels(host):
    """按域名返回**固定**出口 [(通道名, 代理地址或空串)]。确定性优先，绝不运行时自动切换。

    🔴 主人 2026-11-04 定纪律：爬虫是长期自主运行的东西 ——
       · 默认不碰主人本地代理（7890/3067/karing），纯直连出网；
       · 代理能力代码里一直有，但只有**远程显式指定**才启用；
       · 一旦指定就固定用它，不因某次失败偷偷切回直连、也不自动换出口；
       · 不扫描端口、不读系统代理、不自动发现并启用任何本地出口。

    · 未显式配置代理（默认）  → 只有直连一条
    · 命中 direct_domains     → 直连（国内站套代理必挂，固定规则，不算切换）
    · 显式配置代理            → 固定返回那一个（多出口时同域按 hash 粘住，不轮换）
    · 命中 proxy_domains      → 走域专用代理（proxy_domains_proxy），机器活着才生效，
                                 死了回落直连（主人 2026-11-20 Steam 场景新增，见下）

    🔴 2026-11-20 主人新指令（覆盖「不碰 7890」旧纪律的例外，仅白名单域）：
       Steam 社区域名直连被污染 + IP 段切断，唯有走主人正在用的本地代理可达
       （实测 community 直连 000 / 经 7890 200）。代理是临时的（剩 9 天），
       故按域名定点、代理存活探测、过期自动回落直连——不留死配置。
       proxy_domains 为空时本节完全不介入，原行为不变。
    """
    c = cfg()
    host = (host or "").lower()
    # -- Steam 等「按域名定点走代理」白名单：代理活着才生效，死了自动回落直连 --
    pdom = c.get("proxy_domains") or []
    if pdom and _match(host, pdom):
        dpx = (c.get("proxy_domains_proxy") or "").strip() or (c.get("proxy") or "").strip()
        if dpx and proxy_alive(dpx):
            return [("proxy:" + dpx.split("//")[-1].split("/")[0], dpx)]
        return [("direct", "")]

    cand = [p for p in _all_proxies(c) if p]        # 只认显式配置，不自动发现
    if not cand or _match(host, c["direct_domains"]):
        return [("direct", "")]
    px = sticky(host, cand)
    return [("proxy:" + px.split("//")[-1].split("/")[0], px)]


def egress_for(host=None):
    """当前生效出口：'' = 纯直连；非空 = 远程显式指定的固定出口。"""
    return channels(host or "example.com")[0][1]


def host_of(url):
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


# ---------------------------------------------------------------- 静态抓取
def headers_for(url, referer=""):
    """README 坑 1：国内站不带 Referer / Accept-Language 会吃 567 空壳。"""
    h = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        h["Referer"] = referer
    elif host_of(url):
        h["Referer"] = "https://%s/" % host_of(url)
    return h


def openers(url):
    """生成 [(通道名, opener)]。直连用 ProxyHandler({}) 显式禁用系统代理。"""
    out = []
    for name, px in channels(host_of(url)):
        if px.startswith("socks"):        # urllib 吃不下 socks，交给 curl / Playwright
            continue
        ph = urllib.request.ProxyHandler({"http": px, "https": px} if px else {})
        out.append((name, urllib.request.build_opener(
            ph, urllib.request.HTTPSHandler(context=CTX))))
    return out


def _hidden_startupinfo():
    """后台脚本不许弹窗：STARTUPINFO 每次新建（复用第二次失效）。"""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def _curl_fallback(url, timeout, resolve_arg=""):
    """TLS 指纹兜底：系统 curl，带状态码校验（README 坑 7：只看 returncode 会收风控页）。

    resolve_arg 非空时加 --resolve host:443:ip：DoH 拿到的真实 IP，**仍是纯直连**
    （直连 GitHub 那扇门），不经过任何代理端口。
    """
    for name, px in channels(host_of(url)):
        extra = ["-x", px] if px else ["--noproxy", "*"]
        if resolve_arg and not px:
            extra += ["--resolve", resolve_arg]
        try:
            r = subprocess.run(
                ["curl", "-s", "-L", "--compressed", "-A", UA_CURL,
                 "--max-time", str(int(timeout)), "-o", "-", "-w", "\n%{http_code}"]
                + extra + [url],
                capture_output=True, timeout=timeout + 5,
                stdin=subprocess.DEVNULL,
                startupinfo=_hidden_startupinfo(),
                creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode != 0 or not r.stdout:
                continue
            body, _, code = r.stdout.rpartition(b"\n")
            if code.strip().isdigit() and 200 <= int(code.strip()) < 300 and body:
                return body.decode("utf-8", "ignore"), name + "+curl", int(code.strip())
        except Exception:
            continue
    return None, "", 0


def fetch_text(url, timeout=None, max_bytes=400_000, use_curl=True):
    """静态抓取 → (text|None, channel, status, err)。**出口固定，不自动切换**。

    · 未显式指定代理 ⇒ 走 fleet.net 的 proxy="direct"（真直连），不带 fallback；
    · 显式指定代理   ⇒ 严格走它，同样不带 fallback，失败就如实报错；
    · 只有「没有显式代理」时才允许直连类的 urllib/curl 兜底（不算换出口）。
    """
    timeout = float(timeout or cfg()["timeout"])
    px = channels(host_of(url))[0][1]
    tag = "fixed-proxy" if px else "direct"
    last = ""
    # DoH 真实 IP 直连增强：只对已验活缓存的域生效，仍是纯直连、不换出口。
    # 命中就别再去撞注定失败的普通直连（github/raw 实测普通直连 12s 超时，
    # --resolve 1s 内 206）。缓存未命中时照常走下面的常规链路。
    if not px:
        try:
            import direct_ip
            r = direct_ip.curl_resolve(url)
            if r:
                txt, ch, st = _curl_fallback(url, timeout, r)
                if txt:
                    _stats["direct_ok"] += 1
                    return txt, "direct+doh", st, ""
        except Exception:
            pass
    f = fleet() if cfg().get("use_fleet") else None
    if f:
        try:
            kw = {"proxy": px} if px else {"proxy": "direct"}
            with f.urlopen(url, headers=headers_for(url), timeout=timeout, **kw) as r:
                raw = r.read(max_bytes)
                if raw:
                    _stats["proxy_ok" if px else "direct_ok"] += 1
                    return raw.decode("utf-8", "ignore"), tag, r.status, ""
        except urllib.error.HTTPError as e:
            _stats["fail"] += 1
            return None, tag, e.code, "HTTP %s" % e.code
        except Exception as e:
            last = "fleet: %s" % type(e).__name__
    if not px:                       # 显式代理时不许偷偷降级到直连兜底
        for name, op in openers(url):
            try:
                req = urllib.request.Request(url, headers=headers_for(url))
                with op.open(req, timeout=timeout) as r:
                    raw = r.read(max_bytes)
                    if raw:
                        _stats["direct_ok"] += 1
                        return raw.decode("utf-8", "ignore"), name, r.status, ""
            except Exception as e:
                last = "%s: %s" % (e.__class__.__name__, str(e)[:60])
        if use_curl:
            try:
                import direct_ip
                r = direct_ip.curl_resolve(url, allow_probe=False)
            except Exception:
                r = ""
            txt, ch, st = _curl_fallback(url, timeout, r)
            if txt:
                _stats["direct_ok"] += 1
                return txt, ("direct+doh" if r else ch), st, ""
    _stats["fail"] += 1
    return None, tag, 0, last or "all channels failed"


# ---------------------------------------------------------------- 浏览器通道
def browser_launch_args(px="", gpu=False, angle=None):
    """直连浏览器必须显式 --no-proxy-server：Windows 系统代理若指向死端口，
    Chrome 会继承它并把每个请求都送进黑洞（这次事故的根因之一）。

    gpu=True ⇒ 用独显（GTX 1660Ti）做光栅化/合成。三个必须同时满足的条件
    （2026-11-04 联网核实 + 本机实测）：

      ① **必须新无头**：Playwright 默认 headless 用的是独立的 headless_shell，
         那套二进制没有 GPU 进程，给再多 --enable-gpu 也是 0%。加 --headless=new
         才切到真 Chromium 的新无头模式（等同 JS 端 channel:'chromium'）。
      ② **双显卡机器必须选对后端**：网上通说「Windows 首选 d3d11」，但本机实测
         d3d11 绑到的是 **AMD 核显**（ANGLE 报 AMD Radeon），nvidia-smi 恒为 0%；
         只有 `--use-angle=vulkan` 才落到 **GTX 1660 Ti 独显**。所以后端不能照抄
         通用建议，要用探针看 UNMASKED_RENDERER_WEBGL 实际绑了谁。
      ③ **光栅化要显式开**：只给 --enable-gpu 时页面合成仍可能落回 CPU 软光栅，
         需 --enable-gpu-rasterization / --enable-zero-copy 才真把像素活交给 GPU。

    ⚠️ 内存代价：每个 Chromium 实例自带 browser 进程 + GPU 进程（各 100~400 MB），
    这部分是**固定开销**，跟页签数无关。所以「整机拉满」要**减实例、加页签**，
    而不是无脑堆实例数 —— 10 个实例 = 白交 10 份固定开销，实测把提交内存顶到 30 GB。
    """
    if gpu and angle is None:
        try:
            angle = str(cfg().get("gpu_angle") or "vulkan")
        except Exception:
            angle = "vulkan"
    if gpu:
        base = [
            "--no-sandbox", "--disable-dev-shm-usage",
            "--use-angle=" + angle, "--enable-gpu", "--ignore-gpu-blocklist",
            "--enable-gpu-rasterization", "--enable-zero-copy",
            "--enable-accelerated-2d-canvas", "--disable-software-rasterizer",
            # ① 新无头：没有它就没有 GPU 进程
            "--headless=new",
            "--disable-blink-features=AutomationControlled",
            "--disable-session-crashed-bubble", "--disable-infobars",
            "--no-first-run", "--no-default-browser-check",
            "--disable-restore-session-state", "--disable-background-networking",
        ]
    else:
        base = [
            "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-session-crashed-bubble", "--disable-infobars",
            "--no-first-run", "--no-default-browser-check",
            "--disable-restore-session-state", "--disable-background-networking",
        ]
    return base + ([] if px else ["--no-proxy-server"])


def pending_count():
    """估算待解析域名数（resources.json 总量 − state.db 已做）。带 mtime 缓存，
    不每次重读大文件。"""
    try:
        import json as _json
        import sqlite3
        rj = os.path.join(BASE, "resources.json")
        mt = os.path.getmtime(rj)
        if _pending_cache[0] == mt:
            return _pending_cache[1]
        n = len(_json.load(open(rj, encoding="utf-8")))
        db = os.path.join(BASE, "_kb", "state.db")
        done = 0
        if os.path.exists(db):
            c = sqlite3.connect(db)
            done = c.execute("SELECT count(*) FROM done").fetchone()[0]
            c.close()
        v = max(0, n - done)
        _pending_cache[:] = [mt, v]
        return v
    except Exception:
        return 0


def concurrency_plan(mode=None):
    """返回 (workers, tabs, gpu, mode_used)。
    mode 为 None/'auto' ⇒ 待解析 > auto_threshold 自动激进，否则保守。
    手动 --mode conservative/aggressive 始终优先。"""
    c = cfg().get("concurrency", {})
    cons = c.get("conservative", {"workers": 2, "tabs": 3, "gpu": False})
    agg = c.get("aggressive", {"workers": 8, "tabs": 7, "gpu": True})
    thr = c.get("auto_threshold", 3000)
    if mode in (None, "auto", ""):
        n = pending_count()
        used = "aggressive" if n > thr else "conservative"
    else:
        used = "aggressive" if str(mode).lower() == "aggressive" else "conservative"
    plan = agg if used == "aggressive" else cons
    return (plan.get("workers", 2), plan.get("tabs", 3),
            bool(plan.get("gpu")), used)


def browser_context_kw(px=""):
    """new_context 的 proxy 参数：直连不传，代理按出口传。"""
    return {"proxy": {"server": px}} if px else {}


def browser_channels(host):
    """无头渲染用的出口顺序（复用 channels，保持与静态层同一套判断）。"""
    return channels(host)


def stats():
    return dict(_stats)


if __name__ == "__main__":
    import pprint
    pprint.pprint(describe())
    for h in ("example.com", "wiki.biligame.com", "raw.githubusercontent.com", "www.baidu.com"):
        print("%-30s -> %s" % (h, [n for n, _ in channels(h)]))
    u = "https://example.com/"
    t, ch, st, err = fetch_text(u, timeout=6)
    print("selftest %s: channel=%s status=%s bytes=%s err=%s"
          % (u, ch, st, len(t) if t else 0, err[:80]))
