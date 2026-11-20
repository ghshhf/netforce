#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""snifftest —— 封锁层判别 / 伪装 SNI 探测（纯直连，零代理，零第三方依赖）

工程背景（2026-11-06 实测修正，别再犯）
--------------------------------------
11-05 曾据**单个 IP** 判定「x.com 是连接层丢包、伪装 SNI 救不了」——错的。
换到 Cloudflare 真实边缘 IP 后，同一个 IP 上：

    SNI = x.com                -> ConnectionResetError（协议层按关键词 RST）
    SNI = www.cloudflare.com   -> TLSv1.3 握手 OK + HTTP 200

⇒ 封锁在**协议层的明文语义识别**，不是物理断链。只要电还通、包还能进出，
  就有表达方式可换；伪装确实能把 TLS 这一层打通。

但再往里推一层：伪装 SNI + Host: x.com（域名前置）-> Cloudflare 403。
CDN 会校验 SNI/Host 一致性，握手通 ≠ 拿得到目标内容。
⇒ 是「一层一层解」，不是「一伪装全通」；下一层要么 ECH（加密 ClientHello），
  要么继续换入口（归档 / 中继 / 镜像）。

⚠️ 取样陷阱：DoH 返回的 IP 本身也可能是污染 IP（实测 youtube 解析到
   Facebook 段、reddit 解析到 Twitter 段）。拿这种 IP 测只会得到「TCP 丢包」
   的假结论。**取样不够就下判断，本身就是噪声。**

用法
----
    python snifftest.py x.com
    python snifftest.py x.com --ip 172.66.43.190,104.16.0.1 --http
    python snifftest.py x.com --json
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import socket
import ssl
import sys

# 只在显式指定时才用；这些是 Cloudflare 公共边缘 IP，不是任何人的私有节点
DEFAULT_EXTRA_IPS = ["172.66.0.227", "172.66.43.190",
                     "104.16.0.1", "104.16.1.1", "162.159.152.4"]
DEFAULT_FAKE_SNIS = ["www.cloudflare.com", "cdnjs.cloudflare.com"]


def resolve(host, extra=None):
    """DoH 真实 IP + 手工补充 IP（去重）。DoH 不可用时也不报错，继续用手填的。"""
    ips = []
    try:
        from skynet import dns as _dns
        ips = [x for x in _dns.resolve(host, timeout=5) if x]
    except Exception:
        pass
    for x in (extra or []):
        if x and x not in ips:
            ips.append(x)
    return ips


def probe(ip, sni, host, want_http=False, timeout=5):
    """对 (IP, SNI) 做 TCP -> TLS -> 可选 HTTP(Host 独立) 三段判别。"""
    out = dict(ip=ip, sni=sni, host=host, tcp="X", tls="-", http="-")
    try:
        s = socket.create_connection((ip, 443), timeout=timeout)
        s.close()
        out["tcp"] = "OK"
    except Exception as e:
        out["tcp"] = type(e).__name__
        return out
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kw = {"server_hostname": sni} if sni else {}
        with socket.create_connection((ip, 443), timeout=timeout) as s:
            with ctx.wrap_socket(s, **kw) as ss:
                out["tls"] = "OK " + (ss.version() or "")
                if want_http:
                    ss.settimeout(timeout)
                    req = ("GET / HTTP/1.1\r\nHost: %s\r\nUser-Agent: Mozilla/5.0\r\n"
                           "Accept: */*\r\nConnection: close\r\n\r\n" % host).encode()
                    ss.sendall(req)
                    buf = b""
                    try:
                        while len(buf) < 1024:
                            ch = ss.recv(512)
                            if not ch:
                                break
                            buf += ch
                    except Exception:
                        pass
                    line = buf.split(b"\r\n")[0].decode("latin1", "ignore") if buf else "-"
                    out["http"] = line
    except Exception as e:
        out["tls"] = type(e).__name__
    return out


def diagnose(rows):
    """把多组结果收敛成一句封锁层判定。"""
    real = [r for r in rows if r["sni"] == r["host"]]
    fake = [r for r in rows if r["sni"] != r["host"]]
    tcp_ok = any(r["tcp"] == "OK" for r in rows)
    real_ok = any(r["tls"].startswith("OK") for r in real)
    fake_ok = any(r["tls"].startswith("OK") for r in fake)
    front_403 = any(r["http"].startswith("HTTP/1.1 403") for r in fake)

    if not tcp_ok:
        return "IP 层全丢包 / IP 取样被污染 —— 不能据此判定封锁层，先换 IP 段重测"
    if real_ok:
        return "完全可达"
    if fake_ok and not real_ok:
        v = "SNI 关键词拦截（伪装 SNI 可握手 ⇒ 不是物理断链）"
        if front_403:
            v += "；但 CDN 层 SNI/Host 一致性校验 403，域名前置拿不到内容 ⇒ 需 ECH 或换入口"
        return v
    return "TLS 层超时/重置（换 IP、换 SNI 或换协议族再判）"


def run(host, ips, fake_snis, want_http):
    jobs = [(ip, h) for ip in ips for h in [host] + fake_snis]
    with cf.ThreadPoolExecutor(max_workers=min(16, len(jobs))) as ex:
        rows = list(ex.map(lambda a: probe(a[0], a[1], host, want_http), jobs))
    return rows, diagnose(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description="封锁层判别 / 伪装 SNI 探测（纯直连）")
    ap.add_argument("host", help="目标域名，如 x.com")
    ap.add_argument("--ip", default="", help="逗号分隔的补充 IP（绕过被污染的 DoH 结果）")
    ap.add_argument("--fake", default=",".join(DEFAULT_FAKE_SNIS), help="伪装 SNI 列表")
    ap.add_argument("--http", action="store_true", help="握手成功后发真实 Host 的 HTTP GET")
    ap.add_argument("--no-extra", action="store_true", help="不自动补 Cloudflare 公共边缘 IP")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args(argv)

    extra = [x.strip() for x in a.ip.split(",") if x.strip()]
    if not a.no_extra:
        extra += DEFAULT_EXTRA_IPS
    ips = resolve(a.host, extra)
    if not ips:
        print("无可用 IP（DoH 无解析且未指定 --ip）", file=sys.stderr)
        return 2
    rows, verdict = run(a.host, ips,
                        [x.strip() for x in a.fake.split(",") if x.strip()], a.http)
    if a.json:
        print(json.dumps(dict(host=a.host, verdict=verdict, rows=rows),
                         ensure_ascii=False, indent=2))
    else:
        print("目标: %s   候选 IP: %d   （纯直连，零代理）" % (a.host, len(ips)))
        print("%-18s %-24s %-8s %-22s %s" % ("IP", "SNI", "TCP", "TLS", "HTTP"))
        for r in rows:
            print("%-18s %-24s %-8s %-22s %s" %
                  (r["ip"], r["sni"], r["tcp"], r["tls"], r["http"]))
        print("\n判定: %s" % verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
