#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deep_crawl.py —— 第二/三档国外知名站采集器（纯直连）

设计原则（沿用本项目已验证的两条）：
  1. **不信任单一 IP**：DDoH 结果 + 边缘 IP 并发抢答，取「完整度最高」的那份。
  2. **原件优先**：结构化原始响应 > 二手摘要；评分按字节与字段齐备度算。

复用 twitter_crawl 的 direct_get / connect / Writer，不重造轮子。

用法：
    python deep_crawl.py --probe                    # 只探测各端点可达性
    python deep_crawl.py --only wiki --limit 200
    python deep_crawl.py --only arxiv,so
    python deep_crawl.py --all --limit 100
    python deep_crawl.py --report
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, quote_plus, urlsplit

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import netpolicy            # noqa: E402
import direct_ip            # noqa: E402
from skynet import dns as _dns   # noqa: E402
from twitter_crawl import connect, Writer, host_of   # noqa: E402

DB = os.environ.get("NETFORCE_DB") or os.path.join(BASE, "_kb", "seeds.db")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 通用 RSS/Atom 命名空间
NS = {"atom": "http://www.w3.org/2005/Atom",
      "rss": "", "dc": "http://purl.org/dc/elements/1.1/",
      "content": "http://purl.org/rss/1.0/modules/content/"}


# ---------------------------------------------------------------- 取数层

def plain_get(url, timeout=18, extra=None):
    """普通直连（不钉 IP）。多数公开 API 走这条就够。"""
    fd, tmp = tempfile.mkstemp(prefix="nf_dc_", suffix=".bin")
    os.close(fd)
    args = ["curl", "-sL", "--noproxy", "*", "-A", UA,
            "--compressed", "--max-time", str(timeout),
            "-o", tmp, "-w", "%{http_code}"]
    for h in (extra or []):
        args += ["-H", h]
    args.append(url)
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout + 8,
                           env=direct_ip._clean_env(),
                           stdin=subprocess.DEVNULL,
                           startupinfo=netpolicy._hidden_startupinfo(),
                           creationflags=subprocess.CREATE_NO_WINDOW)
        code = (r.stdout or b"").strip().decode("ascii", "ignore")
        body = open(tmp, "rb").read() if os.path.exists(tmp) else b""
        if code.startswith("2") and body:
            return body
    except Exception:
        pass
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return None


def smart_get(url, timeout=18, extra=None):
    """两级：普通直连 → DoH 真 IP 多落点。返回 bytes 或 None。"""
    b = plain_get(url, timeout, extra)
    if b:
        return b
    host = urlsplit(url).hostname or ""
    try:
        ips = [x for x in _dns.resolve(host, timeout=6) if x][:4]
    except Exception:
        ips = []
    if not ips:
        return None
    fd, tmp = tempfile.mkstemp(prefix="nf_dc2_", suffix=".bin")
    os.close(fd)

    def one(ip):
        t2 = tempfile.mkstemp(prefix="nf_dc3_", suffix=".bin")
        os.close(t2[0])
        p = t2[1]
        args = ["curl", "-sL", "--noproxy", "*", "-A", UA, "--compressed",
                "--resolve", "%s:443:%s" % (host, ip),
                "--max-time", str(timeout), "-o", p, "-w", "%{http_code}"]
        for h in (extra or []):
            args += ["-H", h]
        args.append(url)
        try:
            r = subprocess.run(args, capture_output=True, timeout=timeout + 8,
                               env=direct_ip._clean_env(), stdin=subprocess.DEVNULL,
                               startupinfo=netpolicy._hidden_startupinfo(),
                               creationflags=subprocess.CREATE_NO_WINDOW)
            c = (r.stdout or b"").strip().decode("ascii", "ignore")
            if c.startswith("2"):
                d = open(p, "rb").read()
                return d if d else None
        except Exception:
            pass
        finally:
            try:
                os.remove(p)
            except OSError:
                pass
        return None

    best = None
    with cf.ThreadPoolExecutor(max_workers=min(4, len(ips))) as ex:
        for d in ex.map(one, ips):
            if d and (best is None or len(d) > len(best)):
                best = d
    try:
        os.remove(tmp)
    except OSError:
        pass
    return best


def _jget(url, timeout=18, extra=None, retry=2):
    for i in range(retry):
        b = smart_get(url, timeout, extra)
        if b:
            try:
                return json.loads(b)
            except Exception:
                pass
        time.sleep(1.2 * (i + 1))
    return None


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


# ---------------------------------------------------------------- ① Wikipedia

WIKI_API = "https://en.wikipedia.org/w/api.php"


def wiki_collect(w, limit=200, topics=None):
    """MediaWiki 公开 API：先 search 拿标题，再 extracts 拿首段。

    两个请求都是标准 HTTP GET，无鉴权、无 key，纯直连即可。
    """
    topics = topics or [
        "large language model", "transformer neural network", "reinforcement learning",
        "distributed computing", "computer network", "operating system", "database",
        "cryptography", "compiler", "programming language", "machine learning",
        "information retrieval", "web crawler", "knowledge graph", "semantic web",
        "cloud computing", "container virtualization", "version control",
        "functional programming", "graph theory", "linear algebra", "statistics",
        "data structure", "algorithm", "computer security", "reverse engineering",
    ]
    got = 0
    seen = set()
    per = max(10, min(50, limit // max(1, len(topics)) + 5))
    for t in topics:
        if got >= limit:
            break
        q = ("%s?action=query&format=json&list=search&srsearch=%s"
             "&srlimit=%d&srnamespace=0" % (WIKI_API, quote_plus(t), per))
        j = _jget(q)
        if not j:
            print("  ✗ wiki search 失败: %s" % t, flush=True)
            continue
        titles = [x.get("title") for x in (j.get("query", {}).get("search") or []) if x.get("title")]
        if not titles:
            continue
        # 批量取首段 + 元数据（一次最多 20 个标题）
        for i in range(0, len(titles), 20):
            chunk = titles[i:i + 20]
            if got >= limit:
                break
            q2 = ("%s?action=query&format=json&prop=extracts|info|pageprops"
                  "&exintro=1&explaintext=1&inprop=url&redirects=1&titles=%s"
                  % (WIKI_API, quote("|".join(chunk))))
            j2 = _jget(q2)
            if not j2:
                continue
            pages = (j2.get("query", {}) or {}).get("pages") or {}
            for pid, p in pages.items():
                if got >= limit:
                    break
                ti = p.get("title") or ""
                if not ti or ti in seen or pid == "-1":
                    continue
                seen.add(ti)
                eid = "wiki:page:" + ti.lower().replace(" ", "_")
                url = p.get("fullurl") or ("https://en.wikipedia.org/wiki/" + quote(ti.replace(" ", "_")))
                extract = _norm(p.get("extract"))[:1500]
                w.ent(eid, "article", ti, url,
                      depth=0,
                      payload={"extract": extract, "pageid": pid,
                               "lang": "en", "topic": t},
                      plat="wikipedia")
                w.edge(eid, "wiki:topic:" + t.lower().replace(" ", "_"), "in_topic")
                got += 1
            w.flush()
        print("  wiki %-28s +%d（累计 %d）" % (t[:28], len(chunk), got), flush=True)
    return got


# ---------------------------------------------------------------- ② arXiv

ARXIV_API = "https://export.arxiv.org/api/query"


def arxiv_collect(w, limit=200, cats=None):
    """arXiv Atom 接口。返回 XML，字段：title/summary/authors/categories/id。"""
    cats = cats or [
        "cs.AI", "cs.CL", "cs.LG", "cs.IR", "cs.CV", "cs.DC", "cs.SE", "cs.CR",
        "cs.NI", "cs.DB", "stat.ML", "math.OC", "eess.AS", "eess.SP",
    ]
    got = 0
    per = max(10, min(100, limit // max(1, len(cats)) + 5))
    for cat in cats:
        if got >= limit:
            break
        url = ("%s?search_query=cat:%s&start=0&max_results=%d"
               "&sortBy=submittedDate&sortOrder=descending" % (ARXIV_API, cat, per))
        b = smart_get(url, timeout=25)
        if not b:
            print("  ✗ arxiv 失败: %s" % cat, flush=True)
            continue
        try:
            root = ET.fromstring(b)
        except Exception as e:
            print("  ✗ arxiv XML 解析失败 %s: %s" % (cat, e), flush=True)
            continue
        n = 0
        for e in root.findall("atom:entry", NS):
            if got >= limit:
                break
            aid = _norm((e.findtext("atom:id", "", NS) or ""))
            ti = _norm(e.findtext("atom:title", "", NS))
            if not aid or not ti:
                continue
            # id 形如 http://arxiv.org/abs/2411.01234v1
            m = re.search(r"abs/([^/]+)$", aid)
            key = m.group(1) if m else hashlib.md5(aid.encode()).hexdigest()[:12]
            summ = _norm(e.findtext("atom:summary", "", NS))[:1500]
            authors = [_norm(a.findtext("atom:name", "", NS))
                       for a in e.findall("atom:author", NS)]
            pub = e.findtext("atom:published", "", NS) or ""
            upd = e.findtext("atom:updated", "", NS) or ""
            eid = "arxiv:paper:" + key
            w.ent(eid, "paper", ti, aid, depth=0,
                  payload={"abstract": summ, "authors": authors[:12],
                           "published": pub, "updated": upd, "category": cat},
                  plat="arxiv")
            for a in authors[:8]:
                if not a:
                    continue
                au = "arxiv:author:" + a.lower().replace(" ", "_")
                w.ent(au, "author", a, plat="arxiv")
                w.edge(eid, au, "by_author")
            w.edge(eid, "arxiv:cat:" + cat, "in_category")
            got += 1
            n += 1
        w.flush()
        print("  arxiv %-10s +%d（累计 %d）" % (cat, n, got), flush=True)
        time.sleep(0.4)
    return got


# ---------------------------------------------------------------- ③ Stack Overflow

SE_API = "https://api.stackexchange.com/2.2"


def so_collect(w, limit=200, tags=None):
    """StackExchange 公开 API。注意：响应是 gzip，curl 必须带 --compressed。"""
    tags = tags or [
        "python", "javascript", "rust", "go", "java", "c++", "sql", "docker",
        "kubernetes", "react", "node.js", "postgresql", "linux", "git",
        "machine-learning", "pytorch", "tensorflow", "llm", "openai-api",
        "web-scraping", "http", "regex", "multiprocessing", "asyncio",
    ]
    got = 0
    per = max(10, min(100, limit // max(1, len(tags)) + 5))
    for tag in tags:
        if got >= limit:
            break
        url = ("%s/questions?order=desc&sort=votes&tagged=%s&site=stackoverflow"
               "&pagesize=%d&filter=withbody" % (SE_API, quote(tag), per))
        j = _jget(url, timeout=22)
        if not j:
            print("  ✗ SO 失败: %s" % tag, flush=True)
            continue
        n = 0
        for it in (j.get("items") or []):
            if got >= limit:
                break
            qid = it.get("question_id")
            if not qid:
                continue
            ti = _norm(it.get("title"))
            body = re.sub(r"<[^>]+>", " ", it.get("body") or "")
            body = _norm(body)[:1500]
            eid = "so:q:%s" % qid
            w.ent(eid, "question", ti, it.get("link") or "", depth=0,
                  payload={"body": body, "score": it.get("score"),
                           "answers": it.get("answer_count"),
                           "views": it.get("view_count"),
                           "accepted": it.get("is_answered"),
                           "created": it.get("creation_date")},
                  plat="stackoverflow")
            for t in (it.get("tags") or []):
                tid = "so:tag:" + t.lower()
                w.ent(tid, "tag", t, plat="stackoverflow")
                w.edge(eid, tid, "has_tag")
            ow = (it.get("owner") or {}).get("display_name")
            if ow:
                oid = "so:user:" + str((it.get("owner") or {}).get("account_id") or ow)
                w.ent(oid, "user", ow, plat="stackoverflow")
                w.edge(eid, oid, "by_author")
            got += 1
            n += 1
        w.flush()
        print("  SO %-16s +%d（累计 %d）" % (tag, n, got), flush=True)
        if j.get("backoff"):
            time.sleep(float(j["backoff"]))
        time.sleep(0.5)
    return got


# ---------------------------------------------------------------- ④ Wikidata（QLever）

# Wikimedia 全部官方域名（含 api.wikimedia.org / www.wikidata.org）
# 2026-11-06 实测：TCP 可连但 TLS 后零响应，整段封死。
# 改用弗莱堡大学的 QLever —— Wikidata 的完整第三方镜像，SPARQL 端点公开。
QLEVER = "https://qlever.cs.uni-freiburg.de/api/wikidata"


def _sparql(q, timeout=40, retry=2):
    url = QLEVER + "?query=" + quote(q)
    for i in range(retry):
        b = smart_get(url, timeout=timeout)
        if b:
            try:
                return json.loads(b)
            except Exception:
                pass
        time.sleep(1.5 * (i + 1))
    return None


def _rows(j):
    if not j:
        return []
    return ((j.get("results") or {}).get("bindings")) or []


def wikidata_collect(w, limit=200, topics=None):
    """按主题关键词在 Wikidata 里搜实体，拿标签 + 一句话描述。

    QLever 是学术公益服务器，务必限速（每次间隔 ≥1.2s）。
    """
    topics = topics or [
        "transformer", "language model", "machine learning", "neural network",
        "distributed computing", "operating system", "database", "cryptography",
        "compiler", "programming language", "web crawler", "knowledge graph",
        "cloud computing", "docker", "kubernetes", "version control",
        "reverse engineering", "computer security", "algorithm", "graph theory",
    ]
    got = 0
    per = max(10, min(100, limit // max(1, len(topics)) + 3))
    for t in topics:
        if got >= limit:
            break
        kw = t.lower().replace('"', "")
        q = (
            'PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n'
            'PREFIX schema: <http://schema.org/>\n'
            'SELECT ?item ?label ?desc WHERE {\n'
            '  ?item rdfs:label ?label .\n'
            '  FILTER(LANG(?label) = "en")\n'
            '  FILTER(CONTAINS(LCASE(?label), "%s"))\n'
            '  OPTIONAL { ?item schema:description ?desc . FILTER(LANG(?desc) = "en") }\n'
            '} LIMIT %d' % (kw, per)
        )
        j = _sparql(q)
        rows = _rows(j)
        if not rows:
            print("  ✗ wikidata 无结果: %s" % t, flush=True)
            time.sleep(1.2)
            continue
        n = 0
        for r in rows:
            if got >= limit:
                break
            uri = (r.get("item") or {}).get("value") or ""
            if "/entity/" not in uri:
                continue
            qid = uri.rsplit("/", 1)[-1]
            label = _norm((r.get("label") or {}).get("value"))
            desc = _norm((r.get("desc") or {}).get("value"))
            if not label:
                continue
            eid = "wd:" + qid
            if w.ent(eid, "concept", label,
                     "https://www.wikidata.org/wiki/" + qid,
                     depth=0,
                     payload={"description": desc, "qid": qid, "topic": t},
                     plat="wikidata"):
                got += 1
                n += 1
            w.edge(eid, "wd:topic:" + t.lower().replace(" ", "_"), "in_topic")
        w.flush()
        print("  wikidata %-22s +%d（累计 %d）" % (t[:22], n, got), flush=True)
        time.sleep(1.2)
    return got


# ---------------------------------------------------------------- ⑤ 通用 feed（新闻 / 博客）

# 全部走同一个解析器，**不做站内适配**。失败的自动跳过，不影响其它源。
FEEDS = [
    # 新闻（已实测可通）
    ("npr", "https://feeds.npr.org/1001/rss.xml", "news"),
    ("verge", "https://www.theverge.com/rss/index.xml", "tech"),
    ("arstechnica", "https://feeds.arstechnica.com/arstechnica/index", "tech"),
    # 新闻（候选，失败自动跳过）
    ("bbc", "https://feeds.bbci.co.uk/news/rss.xml", "news"),
    ("bbc-tech", "https://feeds.bbci.co.uk/news/technology/rss.xml", "tech"),
    ("guardian-world", "https://www.theguardian.com/world/rss", "news"),
    ("guardian-tech", "https://www.theguardian.com/uk/technology/rss", "tech"),
    ("aljazeera", "https://www.aljazeera.com/xml/rss/all.xml", "news"),
    ("reuters-tech", "https://www.reutersagency.com/feed/?best-topics=tech", "tech"),
    ("engadget", "https://www.engadget.com/rss.xml", "tech"),
    ("hn-front", "https://hnrss.org/frontpage", "tech"),
    ("lobsters", "https://lobste.rs/rss", "tech"),
    ("techmeme", "https://www.techmeme.com/feed.xml", "tech"),
    # 博客 / 通讯
    ("substack-pragmatic", "https://newsletter.pragmaticengineer.com/feed", "blog"),
    ("substack-platformer", "https://www.platformer.news/feed", "blog"),
    ("medium-tag-prog", "https://medium.com/feed/tag/programming", "blog"),
    ("medium-tag-ai", "https://medium.com/feed/tag/artificial-intelligence", "blog"),
]


def feed_collect(w, limit=300, feeds=None, platform="news"):
    """通用 RSS/Atom 采集：一个解析器打所有站点。

    每源最多 limit/源数个条目；正文优先用 feed 内的 content:encoded，
    没有就只用摘要（够做索引，不额外抓页）。
    """
    from generic_extract import parse_feed

    feeds = feeds or FEEDS
    per = max(8, limit // max(1, len(feeds)) + 3)
    got = 0
    for name, url, cat in feeds:
        if got >= limit:
            break
        b = smart_get(url, timeout=20)
        if not b:
            print("  ✗ feed 不可达: %-20s" % name, flush=True)
            continue
        items = parse_feed(b, url)
        if not items:
            print("  ✗ feed 解析空: %-20s (%dB)" % (name, len(b)), flush=True)
            continue
        n = 0
        for it in items[:per]:
            if got >= limit:
                break
            ti = it.get("title") or ""
            u = it.get("url") or ""
            if not ti:
                continue
            key = hashlib.sha1((u or ti).encode()).hexdigest()[:16]
            eid = "%s:item:%s" % (platform, key)
            body = (it.get("content") or it.get("summary") or "")[:2500]
            if w.ent(eid, "story", ti[:300], u, depth=0,
                     payload={"summary": (it.get("summary") or "")[:1200],
                              "body": body,
                              "author": it.get("author") or "",
                              "published": it.get("published") or "",
                              "feed": name, "category": cat},
                     plat=platform):
                got += 1
                n += 1
            src = "%s:feed:%s" % (platform, name)
            w.ent(src, "feed", name, url, plat=platform)
            w.edge(eid, src, "from_feed")
            if it.get("author"):
                aid = "%s:author:%s" % (platform, it["author"].lower()[:60])
                w.ent(aid, "author", it["author"][:80], plat=platform)
                w.edge(eid, aid, "by_author")
            for tg in (it.get("tags") or [])[:6]:
                tid = "%s:tag:%s" % (platform, str(tg).lower()[:40])
                w.ent(tid, "tag", str(tg)[:40], plat=platform)
                w.edge(eid, tid, "has_tag")
        w.flush()
        print("  %-22s +%d（累计 %d）" % (name, n, got), flush=True)
        time.sleep(0.4)
    return got


# ---------------------------------------------------------------- ⑥ 通用页面（含商品）

# 只放「需要抓正文/商品页」的目标。解析走 ld+json → og: → 正文算法。
PAGES = [
    # 商品（反爬强，能拿多少算多少）
    ("amazon-dp", "https://www.amazon.com/dp/B08N5WRWNW", "product"),
    ("amazon-dp2", "https://www.amazon.com/dp/B09B8V1LZ3", "product"),
]


def page_collect(w, limit=50, pages=None, platform="web"):
    """通用页面采集：任意 URL → ld+json / og: / 正文算法，三级瀑布。"""
    from generic_extract import parse_page

    pages = pages or PAGES
    got = 0
    for name, url, cat in pages:
        if got >= limit:
            break
        b = smart_get(url, timeout=20)
        if not b:
            print("  ✗ 页面不可达: %-18s" % name, flush=True)
            continue
        p = parse_page(b.decode("utf-8", "ignore"), url)
        if not p or (not p.get("title") and not p.get("body")):
            print("  ✗ 页面无可提取内容: %-18s (%dB)" % (name, len(b)), flush=True)
            continue
        key = hashlib.sha1(url.encode()).hexdigest()[:16]
        eid = "%s:page:%s" % (platform, key)
        if w.ent(eid, p.get("kind") or "article", (p.get("title") or name)[:300], url,
                 depth=0,
                 payload={k: v for k, v in p.items()
                          if k in ("description", "body", "author", "publisher",
                                   "published", "section", "price", "currency",
                                   "availability", "rating", "rating_count",
                                   "sku", "site", "source")},
                 plat=platform):
            got += 1
        site = "web:site:" + (p.get("site") or "unknown")
        w.ent(site, "domain", p.get("site") or "", "https://" + (p.get("site") or ""),
              plat=platform)
        w.edge(eid, site, "on_site")
        print("  %-18s %-6s %s" % (name, p.get("source"),
                                   (p.get("title") or "")[:50]), flush=True)
        w.flush()
        time.sleep(1.0)
    return got


# ---------------------------------------------------------------- ⑦ Crossref（论文 + OA 全文地址）

CROSSREF = "https://api.crossref.org/works"


def crossref_collect(w, limit=200, queries=None, platform="crossref"):
    """Crossref 公开 API：论文元数据 + 是否有开放获取全文。

    Crossref 的 license/link 字段能直接指出免费全文在哪 —— 这就是
    「同一份内容在别处有更低的门」的落地方式，不碰付费墙。
    """
    queries = queries or [
        "large language model", "retrieval augmented generation", "knowledge graph",
        "web crawler", "distributed system", "reinforcement learning",
        "vector database", "prompt engineering", "model compression",
        "federated learning",
    ]
    per = max(5, min(50, limit // max(1, len(queries)) + 2))
    got = 0
    for qq in queries:
        if got >= limit:
            break
        url = ("%s?query=%s&rows=%d&select=DOI,title,author,abstract,"
               "published,container-title,subject,is-referenced-by-count,link,license"
               % (CROSSREF, quote_plus(qq), per))
        j = _jget(url, timeout=25)
        if not j:
            print("  ✗ crossref 失败: %s" % qq, flush=True)
            continue
        items = ((j.get("message") or {}).get("items")) or []
        n = 0
        for it in items:
            if got >= limit:
                break
            doi = it.get("DOI") or ""
            ti = _norm((it.get("title") or [""])[0])
            if not doi or not ti:
                continue
            eid = "cr:" + doi.lower()
            authors = []
            for a in (it.get("author") or [])[:10]:
                nm = ("%s %s" % (a.get("given") or "", a.get("family") or "")).strip()
                if nm:
                    authors.append(nm)
            oa = ""
            for lk in (it.get("link") or []):
                u = lk.get("URL") or ""
                if u and ("pdf" in (lk.get("content-type") or "") or u.endswith(".pdf")):
                    oa = u
                    break
            pub = ((it.get("published") or {}).get("date-parts") or [[""]])[0]
            if w.ent(eid, "paper", ti[:300], "https://doi.org/" + doi, depth=0,
                     payload={"abstract": _norm(it.get("abstract"))[:1500],
                              "authors": authors,
                              "published": "-".join(str(x) for x in pub if x),
                              "journal": _norm((it.get("container-title") or [""])[0]),
                              "subject": (it.get("subject") or [])[:8],
                              "cited_by": it.get("is-referenced-by-count"),
                              "oa_pdf": oa,
                              "query": qq},
                     plat=platform):
                got += 1
                n += 1
            for a in authors[:5]:
                aid = "cr:author:" + a.lower().replace(" ", "_")[:60]
                w.ent(aid, "author", a, plat=platform)
                w.edge(eid, aid, "by_author")
            for sj in (it.get("subject") or [])[:5]:
                sid = "cr:field:" + str(sj).lower().replace(" ", "_")[:50]
                w.ent(sid, "field", str(sj)[:60], plat=platform)
                w.edge(eid, sid, "in_field")
        w.flush()
        print("  crossref %-24s +%d（累计 %d）" % (qq[:24], n, got), flush=True)
        time.sleep(0.6)
    return got


# ---------------------------------------------------------------- 报告

def report(c):
    print("=== 各平台资产 ===")
    for r in c.execute("select platform,count(*) c from entities group by 1 order by c desc"):
        print("  %-14s %d" % (r[0], r[1]))
    print("=== 总览 ===")
    print("  实体 %d / 边 %d" % (
        c.execute("select count(*) from entities").fetchone()[0],
        c.execute("select count(*) from edges").fetchone()[0]))


# ---------------------------------------------------------------- 探测

PROBES = [
    ("wikidata", QLEVER + "?query=" + quote(
        'PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> '
        'SELECT ?i ?l WHERE { ?i rdfs:label ?l . FILTER(LANG(?l)="en") } LIMIT 1')),
    ("arxiv", "https://export.arxiv.org/api/query?search_query=cat:cs.AI&max_results=2"),
    ("so", SE_API + "/questions?order=desc&sort=votes&site=stackoverflow&pagesize=2"),
    ("crossref", CROSSREF + "?rows=2"),
    ("feed-npr", "https://feeds.npr.org/1001/rss.xml"),
    ("feed-verge", "https://www.theverge.com/rss/index.xml"),
    ("feed-substack", "https://newsletter.pragmaticengineer.com/feed"),
]

MODS = ("wd", "arxiv", "so", "cr", "feed", "page")


def probe():
    def one(item):
        name, url = item
        t0 = time.time()
        b = smart_get(url, timeout=20)
        return name, (len(b) if b else 0), round(time.time() - t0, 1)
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        for name, size, dt in ex.map(one, PROBES):
            print("  %-8s %s  %6dB  %.1fs" % (name, "OK " if size else "FAIL", size, dt))


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="第二/三档知名站采集（纯直连）")
    ap.add_argument("--only", default="", help="逗号分隔：wiki,arxiv,so")
    ap.add_argument("--all", action="store_true", help="跑全部已实现模块")
    ap.add_argument("--limit", type=int, default=200, help="每模块总量上限")
    ap.add_argument("--probe", action="store_true", help="只探测可达性")
    ap.add_argument("--report", action="store_true", help="只输出统计")
    a = ap.parse_args()

    if a.probe:
        print("=== 端点探测（纯直连）")
        probe()
        return 0

    c = connect()
    if a.report:
        report(c)
        return 0

    mods = {k.strip() for k in a.only.split(",") if k.strip()}
    if a.all:
        mods = set(MODS)
    if not mods:
        print("请指定 --only（%s）或 --all" % ",".join(MODS)); return 1
    # 兼容旧名：wiki 已换入口为 wikidata（QLever）
    if "wiki" in mods:
        mods.discard("wiki"); mods.add("wd")
        print("⚠ en.wikipedia.org 整段封死，已改用 Wikidata/QLever 入口")

    w = Writer(c)
    t0 = time.time()
    print("→ 目标库 %s" % DB, flush=True)
    res = {}
    order = [k for k in MODS if k in mods]
    for i, m in enumerate(order, 1):
        print("[%d/%d] %s" % (i, len(order), m), flush=True)
        try:
            if m == "wd":
                res["wd"] = wikidata_collect(w, a.limit)
            elif m == "arxiv":
                res["arxiv"] = arxiv_collect(w, a.limit)
            elif m == "so":
                res["so"] = so_collect(w, a.limit)
            elif m == "cr":
                res["cr"] = crossref_collect(w, a.limit)
            elif m == "feed":
                res["feed"] = feed_collect(w, a.limit)
            elif m == "page":
                res["page"] = page_collect(w, min(50, a.limit))
        except Exception as e:
            print("  ✗ %s 异常: %s: %s" % (m, type(e).__name__, e), flush=True)
            res[m] = -1
    w.flush()
    print("\n完成 %s / 新增实体 %d / 边 %d / %.1fs"
          % (res, w.new_ent, w.new_edge, time.time() - t0), flush=True)
    report(c)
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
