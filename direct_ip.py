# -*- coding: utf-8 -*-
"""netforce / direct_ip.py —— DoH 真实 IP 直连增强（不是代理！）

为什么要有它（2026-11-04 宿主机域实测）：
    普通直连（fleet.net / urllib）打不开 github.com 和 raw.githubusercontent.com，
    但 `skynet/dns.py` 的 DoH 拿到真实 IP 后，用 `curl --resolve host:443:ip`
    **纯直连**就能拿到 HTTP 206（1 秒内）。也就是主人说的「直连 GitHub 那扇门」：
    它不是代理、不经过任何中间端口，只是把「被污染的 DNS 解析」换成真实 IP。

    Google / HuggingFace 即使换了真实 IP 依然不通（IP/SNI 层拦截）⇒ 如实记为不通，
    不伪装成功，也不偷偷切换到主人本地端口。

纪律：
    · 🔴 这仍是**纯直连**，不属于「启用代理」，不需要 --set-proxy，不碰 7890/3067/karing；
    · 只对**已知直连失败的域**做验证（netprobe 矩阵 + direct_fail.txt），
      不对正常域瞎改解析（CDN 站乱 MAP 反而变慢）；
    · 验证必须真发一次请求拿到 2xx/3xx + 字节，DoH 给出 IP 不算数；
    · 结果带 TTL 缓存，过期自动重验；单个 IP 不通就换下一个候选，全不通记为失败。

对外三个函数：
    curl_resolve(url)             → "host:443:ip" 或 ""（给 curl --resolve）
    rules_for(hosts)              → Chromium --host-resolver-rules 串或 ""
    stats()                       → 缓存概况
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import netpolicy

BASE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(BASE, "_kb")
CACHE = os.path.join(KB, "direct_ip.json")
FAIL_BOOK = os.path.join(KB, "direct_fail.txt")
TTL_OK = 6 * 3600
# 失败只缓存 2 分钟：实测 Bash 沙箱域与宿主机域的直连结果会不一样
# （同一分钟里 raw.githubusercontent.com 一个通一个不通），长期记失败会
# 把本来能通的域关在门外。短 TTL ⇒ 换个网络域/网络恢复后自动重验。
TTL_FAIL = 120
MAX_PROBE_PER_BATCH = 12
_lock = threading.Lock()


def _dns():
    """复用 netforce/skynet/dns.py 的 DoH（不重造轮子）。"""
    try:
        from skynet import dns as d
        return d
    except Exception:
        return None


def _load():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data):
    try:
        os.makedirs(KB, exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


_fail_cache = {"mtime": -1.0, "set": set()}


def _needs(host):
    """只增强「已知普通直连失败」的域，别把正常站也拖去 DoH 绕路。

    direct_fail.txt 会有几万行，每次请求重读一遍会拖死分类 ⇒ 按 mtime 缓存。
    """
    try:
        import netprobe
        if netprobe.needs_egress(host) is True:
            return True
    except Exception:
        pass
    try:
        mt = os.path.getmtime(FAIL_BOOK)
        if mt != _fail_cache["mtime"]:
            with open(FAIL_BOOK, encoding="utf-8") as f:
                _fail_cache["set"] = {ln.strip().lower() for ln in f if ln.strip()}
            _fail_cache["mtime"] = mt
    except Exception:
        pass
    return host in _fail_cache["set"]


def _clean_env():
    """curl 默认继承 *_PROXY 环境变量 ⇒ 显式清掉，保证这是纯直连。"""
    env = dict(os.environ)
    for k in list(env):
        if k.lower().endswith("_proxy"):
            env.pop(k, None)
    return env


def _probe(host, ip, timeout=5):
    """真发一次请求验活：拿不到 2xx/3xx + 字节就不算通。"""
    # 与手工验证保持同一组最小参数：只取前 100 字节，不加 --compressed / UA，
    # 少一个变量多一分可信。国内线路对 GitHub 系是间歇可达 ⇒ 超时给足 8s。
    args = ["curl", "-s", "-L", "--noproxy", "*",
            "--resolve", "%s:443:%s" % (host, ip),
            "-r", "0-100", "--max-time", str(max(8, int(timeout))),
            "-o", "-", "-w", "\n%{http_code}", "https://%s/" % host]
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout + 4,
                           env=_clean_env(),
                           stdin=subprocess.DEVNULL,
                           startupinfo=netpolicy._hidden_startupinfo(),
                           creationflags=subprocess.CREATE_NO_WINDOW)
        body, _, code = (r.stdout or b"").rpartition(b"\n")
        c = code.strip()
        return bool(c.isdigit() and 200 <= int(c) < 400 and len(body) > 0)
    except Exception:
        return False


def ensure(host, timeout=5, force=False):
    """返回该域可用的真实 IP 或 ""。带 TTL 缓存，失败也缓存（短 TTL）避免过度重试。"""
    host = (host or "").lower().strip()
    if not host or "." not in host or " " in host:
        return ""
    with _lock:
        data = _load()
        hit = data.get(host)
        ttl = TTL_OK if (hit or {}).get("ip") else TTL_FAIL
        if hit and not force and time.time() - hit.get("ts", 0) < ttl:
            return hit.get("ip", "")
    d = _dns()
    if not d:
        return ""
    try:
        ips = [x for x in d.resolve(host, timeout=timeout) if x][:3]
    except Exception:
        ips = []
    found = ""
    for ip in ips:
        if _probe(host, ip, timeout):
            found = ip
            break
    with _lock:
        data = _load()
        data[host] = {"ip": found, "ts": time.time(),
                      "ok": bool(found), "tried": len(ips)}
        _save(data)
    return found


def curl_resolve(url, allow_probe=True):
    """给 netpolicy 的 curl 通道用：返回 "host:443:ip" 或 ""。"""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:
        return ""
    if not host:
        return ""
    data = _load()
    hit = data.get(host)
    if hit and time.time() - hit.get("ts", 0) < (TTL_OK if hit.get("ip") else TTL_FAIL):
        return "%s:443:%s" % (host, hit["ip"]) if hit.get("ip") else ""
    if not allow_probe or not _needs(host):
        return ""
    ip = ensure(host)
    return "%s:443:%s" % (host, ip) if ip else ""


def rules_for(hosts, max_probe=MAX_PROBE_PER_BATCH, log_fn=None):
    """给 Chromium 的 --host-resolver-rules 串。

    并发验证已知失败域，只有验活通过的才 MAP；正常域完全不动。
    """
    hosts = list(dict.fromkeys((h or "").lower().strip() for h in hosts))
    todo = [h for h in hosts if _needs(h)][:max_probe]
    cache = _load()
    rules = []
    for h in hosts:
        hit = cache.get(h)
        if hit and hit.get("ip") and time.time() - hit.get("ts", 0) < TTL_OK:
            rules.append("MAP %s %s" % (h, hit["ip"]))
    pending = [h for h in todo
               if not (cache.get(h) and time.time() - cache[h].get("ts", 0)
                       < (TTL_OK if cache[h].get("ip") else TTL_FAIL))]
    if pending:
        with ThreadPoolExecutor(max_workers=min(4, len(pending))) as ex:
            for h, ip in zip(pending, ex.map(ensure, pending)):
                if ip:
                    rules.append("MAP %s %s" % (h, ip))
        if log_fn:
            final = _load()
            n_ok = sum(1 for h in pending if (final.get(h) or {}).get("ip"))
            log_fn("  [direct-ip] DoH 真实 IP 直连：%d/%d 个域验活通过（仍是纯直连）"
                   % (n_ok, len(pending)))
    return ",".join(dict.fromkeys(rules))


def stats():
    data = _load()
    ok = {h: v["ip"] for h, v in data.items() if v.get("ip")}
    print("DoH 直连 IP 缓存 %d 条 | 验活通过 %d 条" % (len(data), len(ok)))
    for h, ip in list(ok.items())[:20]:
        print("  %-34s → %s" % (h, ip))


if __name__ == "__main__":
    import sys
    if "--stats" in sys.argv:
        stats()
    else:
        hosts = [x for x in sys.argv[1:] if not x.startswith("--")]
        for h in (hosts or ["github.com", "raw.githubusercontent.com",
                            "www.google.com", "huggingface.co"]):
            ip = ensure(h, force="--force" in sys.argv)
            print("%-34s %s" % (h, ip or "不通（如实记录，不切换出口）"))
        stats()
