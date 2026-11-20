# -*- coding: utf-8 -*-
"""
probe_sites.py —— 实地勘察：抓不到标题/归不了类的站点，到底是个啥？

用途：分类器把一批站标成「待验证（未取到标题）」或「其他/待定」，
      不能靠猜，也不能因为"看着像广告"就不分类（用户：就算是广告也可以分类）。
      本脚本真去访问，把 status / title / 正文片段捞回来，供人工判类。

原则：**只勘察，不删除**。勘察结论用于新增/修正分类标签，绝不会拿它去过滤资源。

用法：
  python probe_sites.py 待验证（未取到标题） 15     # 勘察某一类，抽 15 个
  python probe_sites.py 其他/待定 20
"""
import sys
import json
import html
import random
import re
import ssl
import urllib.request
import netpolicy          # 统一出网：天网 fleet/net.py 自适应直连优先

BASE = "E:/xmanbian/netforce"
CLS = BASE + "/classified.json"
# 🔴 旧版写死 7890（代理没开即全灭）。统一委托 netpolicy：默认直连，
#    自有 7891 / 系统代理只在网络层失败时兜底，候选链由 fleet.net 现探。
PROXY = netpolicy.cfg().get("proxy", "")
CTX = netpolicy.CTX
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def openers(url=""):
    return [op for _n, op in netpolicy.openers(url or "https://example.com/")]


def fetch(url, timeout=8):
    txt, ch, st, err = netpolicy.fetch_text(url, timeout=timeout, max_bytes=200_000)
    if txt:
        return st, txt.encode("utf-8", "ignore")
    return None, (err or "不可达")


def probe(domain):
    for scheme in ("https://", "http://"):
        st, raw = fetch(scheme + domain)
        if st == 200 and isinstance(raw, bytes):
            try:
                t = raw.decode("utf-8", "ignore")
            except Exception:
                t = ""
            m = re.search(r"<title[^>]*>(.*?)</title>", t, re.S | re.I)
            title = html.unescape(m.group(1)).strip()[:90] if m else "（无 title 标签）"
            body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", t, flags=re.S | re.I)
            body = re.sub(r"<[^>]+>", " ", body)
            body = re.sub(r"\s+", " ", html.unescape(body)).strip()[:220]
            return "200", title, body
        if isinstance(raw, str) and raw:
            last = raw
        else:
            last = "HTTP %s" % st
    return last or "全部失败", "—", ""


def main():
    cat = sys.argv[1] if len(sys.argv) > 1 else "待验证（未取到标题）"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    data = json.load(open(CLS, encoding="utf-8"))
    pool = [x for x in data if x.get("category") == cat]
    print("类别「%s」共 %d 条，抽样勘察 %d 个：\n" % (cat, len(pool), min(n, len(pool))))
    random.seed(42)
    for x in random.sample(pool, min(n, len(pool))):
        d = x.get("domain")
        st, title, body = probe(d)
        print("● %s" % d)
        print("  状态: %s" % st)
        print("  标题: %s" % title)
        if body:
            print("  正文: %s" % body)
        print()


if __name__ == "__main__":
    main()
