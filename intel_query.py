# -*- coding: utf-8 -*-
"""
intel_query.py — 免key安全情报查询 CLI（Marvis 可调度）
子命令:
  ipinfo <ip?>       IP 情报（ipinfo.io，免key，默认查本机出口IP）
  urlscan <domain>   URL/域名扫描搜索（urlscan.io，免key限流）
  feodotracker       Botnet C2 IP 黑名单（abuse.ch，免key CSV）
  cve <keyword>      预留：CVE 检索（后续接 key 或换源）
用法:
  python intel_query.py ipinfo 8.8.8.8
  python intel_query.py urlscan example.com
  python intel_query.py feodotracker
"""
import sys, json, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MarvisIntel/1.0"}

def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", errors="replace")

def cmd_ipinfo(ip=None):
    url = "https://ipinfo.io/json" if not ip else f"https://ipinfo.io/{ip}/json"
    st, body = http_get(url)
    data = json.loads(body)
    out = {k: data.get(k) for k in ("ip", "hostname", "city", "region", "country", "loc", "org", "postal", "timezone")}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return st

def cmd_urlscan(domain):
    import urllib.parse
    q = urllib.parse.quote(f"domain:{domain}")
    st, body = http_get(f"https://urlscan.io/api/v1/search/?q={q}&size=5")
    data = json.loads(body)
    results = data.get("results", [])
    print(f"urlscan 命中 {len(results)} 条（最近扫描）")
    for it in results[:5]:
        page = it.get("page", {})
        task = it.get("task", {})
        print(f"  - {page.get('url','')[:100]} | {page.get('ip','')} | {task.get('time','')}")
    return st

def cmd_feodotracker():
    st, body = http_get("https://feodotracker.abuse.ch/downloads/ipblocklist.csv")
    lines = [l for l in body.splitlines() if l and not l.startswith("#")]
    print(f"Feodo Tracker Botnet C2 IP 黑名单共 {len(lines)} 条，示例:")
    for l in lines[:10]:
        print("  ", l)
    print("  ...")
    return st

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1].lower()
    if cmd == "ipinfo":
        return cmd_ipinfo(sys.argv[2] if len(sys.argv) > 2 else None)
    if cmd == "urlscan":
        if len(sys.argv) < 3:
            print("用法: intel_query.py urlscan <domain>"); return 2
        return cmd_urlscan(sys.argv[2])
    if cmd == "feodotracker":
        return cmd_feodotracker()
    print("未知子命令:", cmd); print(__doc__); return 2

if __name__ == "__main__":
    sys.exit(main())
