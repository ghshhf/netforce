"""天网 · DNS 层（dns）
========================
解决一个具体问题：**直连通道可能被污染的 DNS 带到一个假 IP**，
而且这种失败长得像「站点挂了」—— 连接超时或返回一个陌生页面。

三条原则：
  1. **不改全局 socket。** monkeypatch `socket.getaddrinfo` 会影响代理通道
     （代理本来由代理端解析域名），一刀切下去会把好路径也弄坏。
  2. **DoH 只负责给出正确 IP**，实际连接交给 `curl --resolve host:443:ip`——
     curl 原生支持按 IP 连、按域名发 SNI 和 Host 头，不用自己拼 TLS。
  3. **必须能对比。** 单看 DoH 的结果无法判断有没有被污染，
     要拿系统解析结果和 DoH 结果对照，不一致才叫污染。

DoH 端点用 IP 直连，避免「查 DNS 之前又要先解析 DNS」的死循环。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
import ssl
from typing import Optional

# 用 IP 直连的 DoH 端点：不需要先解析域名，天然绕开本地 DNS
DOH_ENDPOINTS = [
    ("https://223.5.5.5/resolve", {"name": "", "type": "A"}, "ali"),
    ("https://1.1.1.1/dns-query", {"name": "", "type": "A"}, "cf"),
]

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

CACHE_TTL = 600.0
_cache: dict[str, tuple[list[str], float]] = {}
_lock = threading.Lock()


def _doh_query(host: str, timeout: float = 6.0) -> list[str]:
    """按顺序试各 DoH 端点，返回 A 记录 IP 列表（失败返回空）。"""
    for base, params, _tag in DOH_ENDPOINTS:
        p = dict(params)
        p["name"] = host
        u = base + "?" + urllib.parse.urlencode(p)
        try:
            req = urllib.request.Request(u, headers={
                "Accept": "application/dns-json",
                "User-Agent": "netforce-dns/1.0",
            })
            op = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),          # DoH 必须直连 IP
                urllib.request.HTTPSHandler(context=_CTX))
            raw = op.open(req, timeout=timeout).read()
            d = json.loads(raw.decode("utf-8", "ignore"))
            ips = [a["data"] for a in (d.get("Answer") or [])
                   if a.get("type") == 1 and _looks_ip(a.get("data", ""))]
            if ips:
                return list(dict.fromkeys(ips))
        except Exception:
            continue
    return []


def _looks_ip(s: str) -> bool:
    parts = s.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(x) <= 255 for x in parts)
    except Exception:
        return False


def _system_query(host: str) -> list[str]:
    """系统解析（可能被污染）。拿来跟 DoH 对比，不是用来连接的。"""
    import socket
    try:
        infos = socket.getaddrinfo(host, None)
        ips = []
        for fam, _t, _p, _c, addr in infos:
            if fam == socket.AF_INET and addr:
                ips.append(addr[0])
        return list(dict.fromkeys(ips))
    except Exception:
        return []


def resolve(host: str, timeout: float = 6.0, use_cache: bool = True) -> list[str]:
    """DoH 解析，带缓存。失败返回空列表（调用方自行决定要不要退系统解析）。"""
    host = (host or "").strip().lower()
    if not host or _looks_ip(host):
        return [host] if _looks_ip(host) else []
    now = time.monotonic()
    with _lock:
        hit = _cache.get(host)
        if use_cache and hit and now - hit[1] < CACHE_TTL:
            return hit[0]
    ips = _doh_query(host, timeout=timeout)
    if ips:
        with _lock:
            _cache[host] = (ips, now)
    return ips


def check(host: str, timeout: float = 6.0) -> dict:
    """对比系统解析与 DoH，判断是否疑似被污染。

    🔴 判据不能是「系统有 DoH 没有的 IP」—— CDN 站（wiki.biligame 这类）
    本来就有十几个 IP，两个解析器按地域/线路给出不同子集完全正常，
    那样判会把所有 CDN 站都误报成投毒。

    真正的污染特征是**两边完全没有交集**（系统被带到了一个假 IP），
    所以以交集为准；无交集只报 suspicious，不下死结论。
    """
    sys_ips = _system_query(host)
    doh_ips = resolve(host, timeout=timeout, use_cache=False)
    if not doh_ips:
        return {"host": host, "system": sys_ips, "doh": [], "verdict": "doh_failed"}
    if not sys_ips:
        return {"host": host, "system": [], "doh": doh_ips,
                "verdict": "system_failed", "common": []}
    common = [x for x in sys_ips if x in doh_ips]
    if common:
        verdict = "ok"
    else:
        # 无交集：可能是污染，也可能只是 CDN 地域调度差异，标可疑不下结论
        verdict = "suspicious"
    return {"host": host, "system": sys_ips, "doh": doh_ips, "verdict": verdict,
            "common": common,
            "only_system": [x for x in sys_ips if x not in doh_ips]}


def resolve_for_curl(url: str, timeout: float = 6.0) -> Optional[str]:
    """给 curl 的 `--resolve` 拼参数：'host:443:ip'。拿不到返回 None。

    用第一个 DoH 结果。多 IP 时首选手动选，不做轮换 ——
    curl 自己会处理连接失败重连下一个，我们多给反而干扰。
    """
    p = urllib.parse.urlparse(url)
    host = p.hostname
    if not host:
        return None
    ips = resolve(host, timeout=timeout)
    if not ips:
        return None
    port = p.port or (443 if p.scheme == "https" else 80)
    return f"{host}:{port}:{ips[0]}"


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def cache_info() -> dict:
    with _lock:
        return {"entries": len(_cache), "hosts": list(_cache.keys())[:10]}
