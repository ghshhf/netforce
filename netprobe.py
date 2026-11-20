# -*- coding: utf-8 -*-
"""netforce / netprobe.py —— 只读出网能力诊断（默认纯直连）

回答「怎么不用开代理就拥有外网能力」：
    **不是去造一个代理，而是复用天网 fleet/net.py 已经打通的真直连出向层。**
    本机确实存在各种代理端口，但那是主人自己的东西，长期自主运行的爬虫
    不许自己发现、自己拿去用、更不许失败时偷偷切换过去。

它做四件事（**全部只读，绝不自动写配置**）：
    ① 直连能力矩阵    逐域实测「这个站裸直连能不能通」，结果落盘缓存（不靠猜、不靠记忆）
    ② 出口盘点        系统代理设置 / 环境变量 / 本地端口 / 代理程序，逐个验活，
                       **只列在报告里，不启用**（记忆纪律：connect_ex 不算数，要有 HTTP 应答）
    ③ 协议识别        HTTP 与 SOCKS5 分开认（karing 3067 是 socks5，urllib 吃不下，
                       交给 curl / Playwright）
    ④ 显式指定入口    `--set-proxy URL` 是**唯一**启用代理的方式，空字符串撤销。
                      一旦指定就固定使用，不自动切换、不因失败回退直连。

沿用的既有能力（不重造轮子）：
    skynet/dns.py   DoH 拿真实 IP 绕 DNS 污染
    skynet/route.py ProxyChain / RelayFetcher 的「节点 → 链 → 直连」降级顺序
    记忆 2026-10-31  默认全直连；只有国外站才考虑出口；401/403 也算「网络通」

用法：
    python netprobe.py                        # 探测 + 打印只读能力报告
    python netprobe.py --json                 # 只输出 JSON
    python netprobe.py --set-proxy http://... # 远程显式指定固定出口
    python netprobe.py --set-proxy ""         # 撤销，回到纯直连
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(BASE, "_kb")
CAP_FILE = os.path.join(KB, "egress_capability.json")
POLICY_FILE = os.path.join(BASE, "_netpolicy.json")

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 本机常见代理软件端口（karing / mihomo / clash / v2ray / sing-box / 系统代理）
CANDIDATE_PORTS = [3067, 7890, 7891, 7892, 7897, 7898, 1080, 10801, 10808,
                   10809, 20171, 2080, 33210, 8080, 8888, 8889, 1086, 7078]
PROXY_PROCESS_NAMES = ("karingService", "mihomo", "clash", "sing-box", "xray",
                       "v2ray", "hysteria", "nekoray", "verge", "shadow", "warp")

# 直连能力矩阵：国内基准 + 典型国外站。判定「拿到任何 HTTP 应答 = 通」
DIRECT_TARGETS = [
    ("https://www.baidu.com/", "国内基准"),
    ("https://github.com/", "GitHub"),
    ("https://pypi.org/simple/", "PyPI"),
    ("https://registry.npmjs.org/", "npm"),
    ("https://www.google.com/", "Google"),
    ("https://www.youtube.com/", "YouTube"),
    ("https://huggingface.co/", "HuggingFace"),
    ("https://raw.githubusercontent.com/github/gitignore/main/Python.gitignore", "GitHub raw"),
    ("https://en.wikipedia.org/", "Wikipedia"),
    ("https://api.telegram.org/", "Telegram API"),
    ("https://cdn.jsdelivr.net/", "jsDelivr"),
    ("https://www.cloudflare.com/", "Cloudflare"),
]

# 验代理用的目标：优先选「裸直连不通」的站，才能证明这个出口真带来了外网能力
VERIFY_TARGETS = ["https://www.google.com/", "https://github.com/",
                  "https://www.youtube.com/"]


def _opener(px=None, timeout=6):
    ph = urllib.request.ProxyHandler(
        {"http": px, "https": px} if px else {})       # {} = 显式禁用系统/环境代理
    return urllib.request.build_opener(ph, urllib.request.HTTPSHandler(context=CTX))


def _http_probe(px, url, timeout=6):
    """真发一次请求。返回 (ok, status, elapsed, err)。401/403 也算网络通。"""
    t0 = time.time()
    try:
        r = _opener(px, timeout).open(
            urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout)
        r.read(512)
        return True, r.status, round(time.time() - t0, 2), ""
    except urllib.error.HTTPError as e:
        return True, e.code, round(time.time() - t0, 2), ""
    except Exception as e:
        return False, 0, round(time.time() - t0, 2), type(e).__name__


def direct_matrix(timeout=6):
    """逐域实测裸直连能力。这是「哪里能直连」的唯一权威来源（不读旧结论）。"""
    out = {}
    for url, label in DIRECT_TARGETS:
        host = urllib.request.urlsplit if False else None
        from urllib.parse import urlsplit
        h = urlsplit(url).hostname
        ok, st, el, err = _http_probe(None, url, timeout)
        out[h] = {"label": label, "direct_ok": ok, "status": st,
                  "latency": el, "err": err}
        print("  直连 %-28s %-14s %s %s %.2fs"
              % (h, label, "OK" if ok else "NO", st or err, el), flush=True)
    return out


def system_proxy():
    """读 Windows 系统代理设置（HKCU Internet Settings）+ 环境变量。"""
    found = []
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            enable, _ = winreg.QueryValueEx(k, "ProxyEnable")
            server, _ = winreg.QueryValueEx(k, "ProxyServer")
        finally:
            winreg.CloseKey(k)
        if enable and server:
            if "://" not in server:
                server = "http://" + server
            found.append({"addr": server, "source": "系统代理(ProxyEnable=1)"})
    except Exception:
        pass
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                "https_proxy", "all_proxy"):
        v = os.environ.get(var, "").strip()
        if v and v not in [x["addr"] for x in found]:
            found.append({"addr": v, "source": "环境变量 " + var})
    return found


def _socks5_greeting(port, timeout=1.0):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall(b"\x05\x01\x00")
            return s.recv(2) == b"\x05\x00"
    except Exception:
        return False


def _port_open(port, timeout=0.25):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def running_proxy_processes():
    out = []
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name"]):
            n = (p.info.get("name") or "").lower()
            if any(k in n for k in PROXY_PROCESS_NAMES):
                out.append("%s(pid=%s)" % (p.info["name"], p.info["pid"]))
    except Exception:
        pass
    return out


def discover_egress(matrix, timeout=6):
    """找所有真能带来外网能力的出口，按协议分开标注。"""
    found = []
    # ① 系统代理 / 环境变量
    for item in system_proxy():
        px = item["addr"]
        ok, st, el, err = _http_probe(px, VERIFY_TARGETS[0], timeout)
        found.append({**item, "scheme": "http", "alive": ok,
                      "verify_status": st, "latency": el})
    # ② 常见本地端口：先认 socks5，再当 http 验
    for port in CANDIDATE_PORTS:
        if not _port_open(port):
            continue
        if _socks5_greeting(port):
            # socks5 用 curl 验（urllib 不支持 socks）；这里只确认握手可用
            found.append({"addr": "socks5h://127.0.0.1:%d" % port,
                          "source": "本地 SOCKS5 端口", "scheme": "socks5",
                          "alive": True, "verify_status": "socks5-handshake",
                          "latency": 0.0})
            continue
        px = "http://127.0.0.1:%d" % port
        ok, st, el, err = _http_probe(px, VERIFY_TARGETS[0], timeout)
        if ok:
            found.append({"addr": px, "source": "本地 HTTP 代理端口",
                          "scheme": "http", "alive": True,
                          "verify_status": st, "latency": el})
    # ③ 已装/在跑的代理程序（只作提示，不假设端口）
    procs = running_proxy_processes()
    return found, procs


def doh_capability():
    """复用 skynet/dns.py 的思路：DoH 拿真实 IP，绕开被污染的系统 DNS。"""
    try:
        sys.path.insert(0, BASE)
        from skynet import dns as _dns
        ips = _dns.resolve("www.youtube.com", timeout=5)
        return {"available": bool(ips), "sample": ips[:3]}
    except Exception as e:
        return {"available": False, "err": type(e).__name__}


def build_report(timeout=6):
    print("[netprobe] ① 实测直连能力矩阵", flush=True)
    matrix = direct_matrix(timeout)
    print("[netprobe] ② 自动发现本机出口", flush=True)
    egress, procs = discover_egress(matrix, timeout)
    print("[netprobe] ③ DoH 通道", flush=True)
    doh = doh_capability()
    rep = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "direct": matrix,
        "egress": egress,
        "proxy_processes": procs,
        "doh": doh,
        "summary": {
            "direct_ok_hosts": [h for h, v in matrix.items() if v["direct_ok"]],
            "direct_fail_hosts": [h for h, v in matrix.items() if not v["direct_ok"]],
            "alive_egress": [e["addr"] for e in egress if e["alive"]],
        },
    }
    os.makedirs(KB, exist_ok=True)
    with open(CAP_FILE, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    return rep


def set_proxy(url):
    """**远程显式指定**代理（唯一的启用入口）。传空字符串 = 撤销，回到纯直连。

    🔴 绝不从本机扫描结果里自动挑一个写进来 —— 那些多半是主人自己的端口，
       长期自主运行的爬虫不许自己拿去用，也不许自己切换。
    """
    url = (url or "").strip()
    cfg = {}
    if os.path.exists(POLICY_FILE):
        try:
            cfg = json.load(open(POLICY_FILE, encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg["proxy"] = url
    cfg["proxies"] = [url] if url else []
    cfg["_updated_by"] = "explicit %s" % time.strftime("%Y-%m-%d %H:%M:%S")
    with open(POLICY_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return url


def load_capability():
    try:
        return json.load(open(CAP_FILE, encoding="utf-8"))
    except Exception:
        return {}


def needs_egress(host):
    """给 netpolicy 用：这个域名裸直连不通吗？（以最近一次实测为准）"""
    rep = load_capability()
    row = (rep.get("direct") or {}).get((host or "").lower())
    if row is None:
        return None                 # 没测过 ⇒ 不臆断
    return not row["direct_ok"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set-proxy", default=None,
                    help="远程显式指定代理 URL；传空字符串撤销（回到纯直连）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--timeout", type=int, default=6)
    a = ap.parse_args()
    if a.set_proxy is not None:
        url = set_proxy(a.set_proxy)
        print("已显式设定固定出口：%s（热加载，无需重启）"
              % (url or "空 ⇒ 纯直连"))
        return
    rep = build_report(a.timeout)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return
    print("\n=== 出口能力报告（**只读诊断，不会自动启用任何出口**）===")
    print("默认出网 : 纯直连（fleet.net ProxyHandler({})，直连国外）")
    print("直连可达 : %s" % (", ".join(rep["summary"]["direct_ok_hosts"]) or "(无)"))
    print("直连不通 : %s" % (", ".join(rep["summary"]["direct_fail_hosts"]) or "(无)"))
    print("本机探到的出口（仅报告，不启用）: %s"
          % (", ".join(rep["summary"]["alive_egress"]) or "（无）"))
    print("代理程序（仅报告）: %s"
          % (", ".join(rep["proxy_processes"]) or "（未发现）"))
    print("DoH      : %s" % rep["doh"])
    print("要加出口 ⇒ python netprobe.py --set-proxy http://... ；撤销 ⇒ --set-proxy ''")


if __name__ == "__main__":
    main()
