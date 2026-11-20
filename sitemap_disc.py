# -*- coding: utf-8 -*-
"""sitemap 发现层：把「站方自己交出来的完整目录」变成 URL 清单。

为什么单独做一层：
    主爬虫的发现靠「链接扩散」——首页有什么链接就爬什么，漏掉的永远漏掉。
    而 sitemap 是站方主动公开的全量目录，精确到每一篇文章，还带 lastmod。
    实测 20 个国内站：7 个有 sitemap，合计 37.9 万条 URL
    （thepaper 10.9 万、cnblogs 23 万、qbitai 3.6 万）。
    这是白捡的发现层，比猜路径强一个数量级。

三个必须处理的现实问题：
    1. 大站是 sitemapindex 嵌套（子图几千个）→ 只展开有限层，按需取
    2. 单图可能几十 MB → 必须流式扫，不能 r.text 一把梭
    3. .xml.gz 压缩 → 边下边解

用法：
    from sitemap_disc import discover
    urls = discover("https://www.thepaper.cn", max_urls=3000)
"""
import gzip
import io
import json
import os
import re
import time
import urllib.parse as up

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

# 缓存目录：同一站 12 小时内不重复扫。
# 代价对比：cnblogs 扫一次 20 秒，只为了爬 6 页 —— 不缓存就是纯浪费。
CACHE_DIR = os.path.join("_kb", "sitemap_cache")
# 缓存里固定存这么多条，返回时再按调用方要的量截断，
# 否则「第一次要 60 条，第二次要 3000 条」就只能重扫一遍。
CACHE_CAP = 5000

# 单次响应最多读这么多字节：sitemap 动辄几十 MB，全读进内存会重演 MemoryError
MAX_BYTES = 8 * 1024 * 1024
LOC_RE = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+)\s*(?:\]\]>)?\s*</loc>", re.I)
SITEMAP_TAG = re.compile(r"<sitemapindex", re.I)

# 不是内容页的路径，发现时先排掉，省得占了预算
_JUNK = re.compile(
    r"/(?:tag|tags|category|categories|author|authors|page|pages|search|login|"
    r"register|cart|user|users|feed|rss|sitemap)(?:/|$|[?._])", re.I)
_ASSET_EXT = re.compile(
    r"\.(?:jpg|jpeg|png|gif|webp|svg|ico|css|js|mp4|mp3|zip|rar|7z|exe|msi|apk|"
    r"pdf|docx?|xlsx?|pptx?|woff2?|ttf|eot)$", re.I)


def _get(cli, url, cap=MAX_BYTES):
    """流式取，超过 cap 字节就停手。返回 (status, bytes)。"""
    try:
        with cli.stream("GET", url) as r:
            if r.status_code >= 400:
                return r.status_code, b""
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf.extend(chunk)
                if len(buf) >= cap:
                    break
            return r.status_code, bytes(buf)
    except Exception:
        return 0, b""


def _decode(raw, url):
    """gzip 就解，然后按 xml 里的 encoding 解码。"""
    if url.lower().endswith(".gz") or raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            try:
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            except Exception:
                return ""
    head = raw[:200].decode("ascii", errors="ignore")
    enc = "utf-8"
    m = re.search(r'encoding=["\']([\w-]+)["\']', head, re.I)
    if m:
        enc = m.group(1)
    try:
        return raw.decode(enc, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _candidates(base):
    """候选 sitemap 地址：先 robots.txt 声明的，再常见路径。"""
    root = base.rstrip("/")
    out = []
    cli = httpx.Client(follow_redirects=True, trust_env=False, timeout=12,
                       headers=HEADERS, verify=False)
    try:
        st, raw = _get(cli, root + "/robots.txt", cap=256 * 1024)
        if st == 200 and raw:
            txt = raw.decode("utf-8", errors="replace")
            for m in re.finditer(r"(?im)^\s*sitemap:\s*(\S+)", txt):
                u = m.group(1).strip()
                if u.startswith("//"):
                    u = "https:" + u
                elif u.startswith("/"):
                    u = root + u
                out.append(u)
    finally:
        cli.close()
    for p in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap/index.xml",
              "/wp-sitemap.xml", "/sitemap.xml.gz"):
        u = root + p
        if u not in out:
            out.append(u)
    return out[:10]


def discover(base, max_urls=3000, max_maps=40, log=None, ttl=12 * 3600,
             budget=12.0, refresh=False):
    """给站点根地址，返回 sitemap 里的 URL 清单（已排序、已去噪）。

    返回 (urls, info)：
        urls  已经按「像内容页」排好序，最多 max_urls 条
        info  {"maps": 实际读的子图数, "total": 扫到的原始条数, "hit": 命中的 sitemap}

    两条必须有的约束（实测换来的）：

    1. **缓存**：cnblogs 有 5.4 万条 URL，扫 40 个子图要 20 秒，
       而真正要爬的只有 6 页 —— 探测比干活还贵。同一站 12 小时内直接读缓存。
    2. **时间预算**：budget 秒到了就停手，把已经扫到的先交出去。
       大站永远扫不完，拿到够用的就走比「扫全」现实。

    子图筛选规则：
        WordPress 的 wp-sitemap.xml 下面混着 posts / users / taxonomies /
        attachments 四类子图。全展开的话拿到的全是作者页和附件页
        （qbitai 第一次跑就是这结果）。所以按名字优先 posts，跳过 users。
    """
    say = log or (lambda *a: None)
    root = base if "//" in base else "https://" + base
    host = (up.urlsplit(root).hostname or "").lower()
    t_start = time.time()

    # ---- 缓存
    cache_p = os.path.join(CACHE_DIR, (host or "unknown") + ".json")
    if not refresh and os.path.exists(cache_p):
        try:
            c = json.load(open(cache_p, encoding="utf-8"))
            if time.time() - float(c.get("ts") or 0) < ttl:
                us = c.get("urls") or []
                info = dict(c.get("info") or {})
                info["cached"] = True
                # 缓存里存的是 CACHE_CAP 条；只有「够用」或「本来就没扫满」才直接用
                if us and (max_urls <= len(us) or info.get("complete")):
                    say("  [sitemap] %s ← 缓存命中 %d 条" % (host, len(us)))
                    return us[:max_urls], info
        except Exception:
            pass

    # 扫的时候按 CACHE_CAP 的量扫，别按调用方要的量扫，否则缓存永远只够一次
    scan_cap = max(int(max_urls), CACHE_CAP)

    urls, seen = [], set()
    maps_read = 0
    raw_total = 0
    hit = ""
    # 子图优先级：含这些词的先读，含那些词的不读
    want = re.compile(r"(post|article|news|content|detail|story|blog)", re.I)
    skip = re.compile(r"(user|author|taxonom|attachment|comment|tag-|category)", re.I)
    cli = httpx.Client(follow_redirects=True, trust_env=False, timeout=20,
                       headers=HEADERS, verify=False)
    try:
        queue = _candidates(root)
        pending_subs = []
        while (queue or pending_subs) and maps_read < max_maps and len(urls) < scan_cap:
            # 时间预算：拿到东西后按 budget 收手；一条都没有时放宽到 2.5 倍，
            # 否则像 cnblogs（4.7 万个子图的 index）会在读完索引、还没读子图时
            # 就被砍掉，白扫 14 秒拿到 0 条。
            spent = time.time() - t_start
            if (urls and spent > budget) or spent > budget * 2.5:
                break
            if not queue:
                # 子图按优先级取：像内容集的先读
                pending_subs.sort(key=lambda s: (0 if want.search(s) else
                                                 (2 if skip.search(s) else 1)))
                queue = [pending_subs.pop(0)]
            sm = queue.pop(0)
            st, raw = _get(cli, sm)
            if st != 200 or not raw:
                continue
            body = _decode(raw, sm)
            if not body:
                continue
            if SITEMAP_TAG.search(body):
                # sitemapindex：子图先排队，不立刻读，等筛完优先级再取
                subs = LOC_RE.findall(body)
                raw_total += len(subs)
                for s in subs:
                    if len(pending_subs) >= 300:
                        break
                    if s not in pending_subs and not skip.search(s):
                        pending_subs.append(s)
                maps_read += 1
                if not hit:
                    hit = sm
                continue
            locs = LOC_RE.findall(body)
            if not locs:
                continue
            maps_read += 1
            hit = hit or sm
            raw_total += len(locs)
            for u in locs:
                if u in seen:
                    continue
                seen.add(u)
                urls.append(u)
                if len(urls) >= scan_cap:
                    break
    finally:
        cli.close()

    # 排序：带日期路径的、长 slug 的优先（更像文章），垃圾路径往后
    def score(u):
        p = up.urlsplit(u).path or ""
        s = 0
        if re.search(r"/\d{4}/\d{2}/", p):
            s += 8
        if re.search(r"/\d{4}/", p):
            s += 4
        if re.search(r"[a-z0-9]+-[a-z0-9-]{8,}", p, re.I):
            s += 5
        if p.count("/") >= 3:
            s += 2
        if _JUNK.search(p):
            s -= 12
        if _ASSET_EXT.search(p):
            s -= 20
        if len(p) < 8:
            s -= 6
        return -s

    urls.sort(key=score)
    # complete = 「已经扫到了全站能扫的东西」，缓存命中判断要用它
    complete = (len(urls) < scan_cap)
    info = {"maps": maps_read, "total": raw_total, "hit": hit,
            "kept": len(urls), "complete": complete,
            "host": host, "scanned_at": time.time()}
    say("  [sitemap] %s ← %s 读 %d 图 / 扫到 %d 条 / 保留 %d%s"
        % (host, hit.split("/")[-1][:20] if hit else "无",
           maps_read, raw_total, len(urls),
           "（全量）" if complete else "（截断）"))

    if urls:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            json.dump({"ts": time.time(), "info": info, "urls": urls[:CACHE_CAP]},
                      open(cache_p, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass
    return urls[:max_urls], info


def feed_crawler(urls, host="", limit=5000):
    """把 sitemap 的 URL 写进主爬虫的 fed 队列，让它慢慢爬。

    分工：seed_crawl 深挖前 N 条（要质量），主爬虫扫全量（要覆盖）。

    fed 的实际结构是 (value, kind, ts) 三列 —— 没有 src 列。
    第一版按四列写，插入直接失败、静默返回 0，
    所以「喂给主爬虫」这个动作从来没生效过。
    """
    import os
    import sqlite3
    import time
    p = os.path.join("_kb", "state.db")
    if not os.path.exists(p):
        return 0
    n = 0
    c = sqlite3.connect(p, timeout=30)
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(fed)")}
        if not {"kind", "value"} <= cols:
            return 0
        now = time.time()
        for u in urls[:limit]:
            try:
                cur = c.execute(
                    "INSERT OR IGNORE INTO fed(kind,value,ts) VALUES('url',?,?)",
                    (u, now))
                n += cur.rowcount
            except Exception:
                break
        c.commit()
    finally:
        c.close()
    return n


if __name__ == "__main__":
    import sys
    targets = sys.argv[1:] or ["https://www.thepaper.cn", "https://www.qbitai.com"]
    for t in targets:
        us, info = discover(t, max_urls=20, log=print)
        print("  ", info)
        for u in us[:5]:
            print("     ", u[:110])
        print()
