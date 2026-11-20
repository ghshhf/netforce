#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""generic_extract.py —— 通用内容提取层（不针对任何具体站点）

三级瀑布，覆盖率优先于精确度：
  ① feed       RSS 2.0 / Atom / RDF —— 站点自带的结构化输出，零适配
  ② ld+json    schema.org 结构化数据（NewsArticle / Article / BlogPosting / Product）
  ③ 正文算法   文本密度兜底，不需要知道版式

设计要点：
  - **不写 per-site 规则**。任何站点进来都走同一条链。
  - 原件优先：feed 的原文 > ld+json 的字段 > og: 标签 > 正文算法抽出来的段落。
  - 输出统一成 dict，交给 Writer 入库。

用法（被 deep_crawl.py 调用）：
    from generic_extract import parse_feed, parse_page, extract_article
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, urljoin

# ---------------------------------------------------------------- 基础

RE_TAG = re.compile(r"<[^>]+>")
RE_WS = re.compile(r"\s+")
RE_SCRIPT = re.compile(r"<(script|style|noscript|svg|iframe)[^>]*>.*?</\1>", re.I | re.S)
RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
RE_LDJSON = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)
RE_META = re.compile(
    r'<meta[^>]+(?:property|name)=["\']([^"\']+)["\'][^>]+content=["\']([^"\']*)["\']', re.I)
RE_META_R = re.compile(
    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']([^"\']+)["\']', re.I)
RE_P = re.compile(r"<p\b[^>]*>(.*?)</p>", re.I | re.S)
RE_H1 = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.I | re.S)
RE_TITLE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.I | re.S)
RE_PRICE = re.compile(r"(?:[$£€¥]|USD|EUR|GBP)\s?([0-9][0-9,]*\.?[0-9]{0,2})")


def _clean(s):
    if not s:
        return ""
    s = _html.unescape(str(s))
    s = RE_TAG.sub(" ", s)
    return RE_WS.sub(" ", s).strip()


def _meta_map(doc):
    """og: / twitter: / description 全部拍平成一个 dict。"""
    out = {}
    for k, v in RE_META.findall(doc):
        out.setdefault(k.lower(), _html.unescape(v or ""))
    for v, k in RE_META_R.findall(doc):
        out.setdefault(k.lower(), _html.unescape(v or ""))
    return out


def _host(u):
    try:
        h = (urlsplit(u).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _uid(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts if p).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- ① feed

def _walk_text(node, names, ns=None):
    for n in names:
        v = node.findtext(n, None, ns) if ns else node.findtext(n)
        if v:
            return _clean(v)
    return ""


def parse_feed(data, base_url=""):
    """解析 RSS 2.0 / Atom / RDF。返回 [{title,url,summary,author,published,tags,content}]"""
    out = []
    if not data:
        return out
    # 去掉可能的前置 BOM / 空白
    t = data.lstrip(b"\xef\xbb\xbf \r\n\t")
    try:
        root = ET.fromstring(t)
    except Exception:
        # 有些 feed 前面带 XHTML/DOCTYPE 噪声，从第一个 < 开始重试
        i = t.find(b"<")
        if i < 0:
            return out
        try:
            root = ET.fromstring(t[i:])
        except Exception:
            return out

    ATOM = "{http://www.w3.org/2005/Atom}"
    RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
    DC = "{http://purl.org/dc/elements/1.1/}"
    CONTENT = "{http://purl.org/rss/1.0/modules/content/}"

    # Atom: <entry>
    for e in root.iter(ATOM + "entry"):
        link = ""
        for ln in e.findall(ATOM + "link"):
            if ln.get("rel") in (None, "alternate"):
                link = ln.get("href") or ""
                if link:
                    break
        ti = _clean(e.findtext(ATOM + "title", "", None))
        summ = _clean(e.findtext(ATOM + "summary", "", None)) or \
            _clean(e.findtext(ATOM + "content", "", None))
        auth = e.findtext(ATOM + "author/" + ATOM + "name", "", None) or \
            e.findtext(ATOM + "author", "", None)
        pub = (e.findtext(ATOM + "published", "", None)
               or e.findtext(ATOM + "updated", "", None))
        tags = [c.get("term") for c in e.findall(ATOM + "category") if c.get("term")]
        if ti or link:
            out.append(dict(title=ti, url=urljoin(base_url, link) if link else "",
                            summary=summ, author=_clean(auth), published=pub,
                            tags=tags, content=""))

    # RSS 2.0 / RDF: <item>
    for it in root.iter("item"):
        link = _clean(it.findtext("link", "")) or ""
        if not link:
            g = it.findtext("{http://www.w3.org/2005/Atom}link")
            link = g or ""
        ti = _clean(it.findtext("title", ""))
        summ = _clean(it.findtext("description", ""))
        body = _clean(it.findtext(CONTENT + "encoded", ""))
        auth = _clean(it.findtext("author", "") or it.findtext(DC + "creator", ""))
        pub = _clean(it.findtext("pubDate", "") or it.findtext(DC + "date", ""))
        tags = [_clean(c.text) for c in it.findall("category") if _clean(c.text)]
        if ti or link:
            out.append(dict(title=ti, url=urljoin(base_url, link) if link else "",
                            summary=summ, author=auth, published=pub,
                            tags=tags, content=body))
    return out


# ---------------------------------------------------------------- ② ld+json

def _jsonld_objs(doc):
    objs = []
    for m in RE_LDJSON.findall(doc):
        s = m.strip()
        if not s:
            continue
        try:
            j = json.loads(s)
        except Exception:
            # 有些站塞了注释或尾逗号，退一步只找第一个平衡的 {}
            try:
                i, depth, start = 0, 0, -1
                for i, ch in enumerate(s):
                    if ch == "{":
                        if depth == 0:
                            start = i
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0 and start >= 0:
                            objs.append(json.loads(s[start:i + 1]))
                            break
            except Exception:
                continue
            continue
        if isinstance(j, list):
            objs.extend([x for x in j if isinstance(x, dict)])
        elif isinstance(j, dict):
            objs.append(j)
    return objs


def _types(o):
    t = o.get("@type") or ""
    if isinstance(t, list):
        return [str(x).lower() for x in t]
    return [str(t).lower()]


ARTICLE_TYPES = {
    "newsarticle", "article", "blogposting", "report", "analysisnewsarticle",
    "opinionnewsarticle", "reviewnewsarticle", "techarticle", "scholarlyarticle",
    "socialmediaposting", "liveblogposting", "backgroundnewsarticle",
}
PRODUCT_TYPES = {"product", "book", "movie", "creativework", "offer"}


def _first(seq):
    if isinstance(seq, list):
        return seq[0] if seq else None
    return seq


def _name_of(v):
    if isinstance(v, str):
        return _clean(v)
    if isinstance(v, dict):
        return _clean(v.get("name") or v.get("headline") or "")
    if isinstance(v, list):
        return _name_of(_first(v))
    return ""


def from_jsonld(doc):
    """从 ld+json 里抽出 article / product 的结构化字段。找不到返回 None。"""
    best = None
    best_score = 0
    for o in _jsonld_objs(doc):
        ty = set(_types(o))
        if ty & ARTICLE_TYPES:
            score = 3
            if o.get("articleBody"):
                score += 3
            if o.get("headline"):
                score += 1
        elif ty & PRODUCT_TYPES:
            score = 2
            if o.get("offers"):
                score += 1
        else:
            continue
        if score > best_score:
            best, best_score = o, score
    if not best:
        return None
    ty = set(_types(best))
    is_prod = bool(ty & PRODUCT_TYPES) and not (ty & ARTICLE_TYPES)
    d = dict(
        kind="product" if is_prod else "article",
        title=_clean(best.get("headline") or best.get("name") or ""),
        description=_clean(best.get("description") or "")[:1200],
        body=_clean(best.get("articleBody") or "")[:4000],
        author=_name_of(best.get("author")),
        publisher=_name_of(best.get("publisher") or best.get("brand")),
        published=_clean(best.get("datePublished") or best.get("dateModified") or ""),
        section=_clean(_name_of(best.get("articleSection"))),
        keywords=[_clean(k) for k in (best.get("keywords") or []) if _clean(k)]
        if isinstance(best.get("keywords"), list)
        else [_clean(k) for k in re.split(r"[,;]", str(best.get("keywords") or "")) if _clean(k)],
    )
    if is_prod:
        off = _first(best.get("offers")) or {}
        if isinstance(off, dict):
            d["price"] = off.get("price") or off.get("lowPrice")
            d["currency"] = off.get("priceCurrency")
            d["availability"] = _clean(off.get("availability") or "")
        agg = _first(best.get("aggregateRating")) or {}
        if isinstance(agg, dict):
            d["rating"] = agg.get("ratingValue")
            d["rating_count"] = agg.get("reviewCount") or agg.get("ratingCount")
        d["sku"] = _clean(best.get("sku") or best.get("productID") or "")
    return d


# ---------------------------------------------------------------- ③ 正文算法

def extract_article(doc, min_len=80):
    """文本密度兜底：把所有 <p> 聚合，选最长的连续块。

    不依赖任何站点版式——只看「哪一段文字最多、最长、最不像导航」。
    """
    if not doc:
        return ""
    d = RE_COMMENT.sub(" ", doc)
    d = RE_SCRIPT.sub(" ", d)
    paras = []
    for m in RE_P.findall(d):
        t = _clean(m)
        if len(t) >= min_len:
            paras.append(t)
    if not paras:
        # 有些站全用 <div> 与 <br>，退回按 <br><br> 切
        d2 = re.sub(r"<br\s*/?>", "\n", d)
        chunks = [_clean(x) for x in d2.split("\n")]
        paras = [c for c in chunks if len(c) >= min_len]
    if not paras:
        return ""
    # 过滤噪声段：短、全是链接式短语、重复站名
    good = []
    for p in paras:
        if len(p) < 40:
            continue
        if p.count("|") > 6:
            continue
        good.append(p)
    return _clean(" ".join(good or paras))[:5000]


# ---------------------------------------------------------------- 统一页面解析

def parse_page(doc, url=""):
    """任意网页 → 统一 dict。三级瀑布按信噪比降级。"""
    if not doc:
        return None
    meta = _meta_map(doc)
    jl = from_jsonld(doc)

    title = (jl or {}).get("title") or \
        meta.get("og:title") or meta.get("twitter:title") or \
        _clean(RE_TITLE.search(doc).group(1)) if RE_TITLE.search(doc) else \
        (jl or {}).get("title") or meta.get("og:title") or meta.get("twitter:title") or ""
    title = _clean(title)

    desc = (jl or {}).get("description") or \
        meta.get("og:description") or meta.get("description") or \
        meta.get("twitter:description") or ""

    body = (jl or {}).get("body") or ""
    src = "ld+json" if (jl and (jl.get("body") or jl.get("title"))) else ""
    if len(body) < 200:
        alt = extract_article(doc)
        if len(alt) > len(body):
            body, src = alt, (src + "+text-density") if src else "text-density"
    if not src:
        src = "og-meta" if title else "none"

    out = dict(
        url=url, title=title, description=_clean(desc)[:1200],
        body=body[:5000], source=src,
        author=(jl or {}).get("author", "") or _clean(meta.get("author", "")),
        publisher=(jl or {}).get("publisher", "") or _host(url),
        published=(jl or {}).get("published", "")
        or meta.get("article:published_time", "")
        or meta.get("datepublished", ""),
        section=(jl or {}).get("section", "") or meta.get("article:section", ""),
        site=_host(url),
        image=meta.get("og:image", ""),
        kind=(jl or {}).get("kind", "article"),
    )
    for k in ("price", "currency", "availability", "rating", "rating_count", "sku"):
        if (jl or {}).get(k) is not None:
            out[k] = jl[k]
    out["keywords"] = (jl or {}).get("keywords") or []
    # 价格兜底：正文里找一个货币数字
    if out.get("kind") == "product" and not out.get("price"):
        m = RE_PRICE.search(doc)
        if m:
            out["price"] = m.group(1)
    return out
