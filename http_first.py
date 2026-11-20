"""HTTP-first 一级漏斗：轻量 HTTP 直取，只有「空壳 / 需要 JS」才交给浏览器。

实测依据（本机 200 域 / 24 并发 / 8s 超时）：
    HTTP 直取   78.0% 拿到 HTML，其中 58.5% 标题+正文齐全 → 完全不需要浏览器
                吞吐 5.05 域/秒，内存几十 MB
    浏览器渲染  成功率仅 17.8%（89/500），吞吐 3.77 域/秒，单实例 500 MB+
    ConnectTimeout/ConnectError 占 22% —— 这部分浏览器同样救不了，不该烧渲染预算

开源界的共识也是这个顺序（Scrapy/Crawlee 的 HTTP-first、scrapy-playwright 按需渲染、
Playwright 官方「静态页用 requests 快一倍」）。本模块只做第一级，判定为「空壳」的
域名原样退回给 render_parse 的浏览器链路，行为与之前完全一致。
"""

import asyncio
import json
import os
import re
import socket
import time
import urllib.parse

import httpx

BASE = os.path.dirname(os.path.abspath(__file__))
_AUTO = object()


def egress_proxy(spec=_AUTO):
    """一级漏斗的出网口。

    2026-11-09：老版本用 trust_env=False 纯直连，海外站直接 TCP 不通，
    历史 2.1 万条「不可达」里很大一部分就是这么误判的。现在默认读
    _netpolicy.json 的 crawler_proxy（统一出口 18080：国内直连 + 国外
    隧道回落），但**不读系统代理**，纪律不变。
    显式传 "" / 环境变量 NETFORCE_HTTP_EGRESS=off ⇒ 强制退回纯直连。
    """
    if spec is not _AUTO:
        return str(spec).strip() or None
    env = os.environ.get("NETFORCE_HTTP_EGRESS")
    if env is not None:
        return None if env.strip().lower() in ("", "off", "0", "direct") else env.strip()
    try:
        with open(os.path.join(BASE, "_netpolicy.json"), encoding="utf-8") as f:
            return (json.load(f).get("crawler_proxy") or "").strip() or None
    except Exception:
        return None


def _client_kw(spec=_AUTO, log=print):
    """出口在线才挂 proxy；出口掉线自动降级纯直连，别让整批任务陪葬。"""
    p = egress_proxy(spec)
    if not p:
        return {}
    try:
        u = urllib.parse.urlsplit(p if "://" in p else "http://" + p)
        with socket.create_connection((u.hostname or "127.0.0.1",
                                       u.port or 80), 0.5):
            pass
    except Exception:
        log("  [http] 统一出口 %s 不可达 ⇒ 本批退回纯直连" % p, flush=True)
        return {}
    return {"proxy": p}

try:
    from lxml import html as lhtml
except Exception:          # lxml 缺失时退回正则，功能不丢、精度略降
    lhtml = None

WS = re.compile(r"\s+")
TAG = re.compile(r"(?s)<[^>]+>")
STRIP = re.compile(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>")
TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
META_RE = re.compile(r"(?is)<meta\s+([^>]+?)/?>")
ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""")
A_RE = re.compile(r"""(?is)<a\s+[^>]*href\s*=\s*["']([^"']+)["']""")
ASSET_RE = re.compile(
    r"""(?is)<(?:img|script|link|video|audio|source)\s+[^>]*?"""
    r"""(?:src|href)\s*=\s*["']([^"']+)["']""")


def _clean(s):
    return WS.sub(" ", (s or "")).strip()


def _host(u):
    try:
        return (urllib.parse.urlsplit(u).hostname or "").lower()
    except Exception:
        return ""


def _meta_map(doc):
    """name/property → content，兼容大小写与 http-equiv。"""
    out = {}
    for m in doc.xpath("//meta"):
        key = (m.get("name") or m.get("property") or "").strip().lower()
        if key and key not in out:
            out[key] = _clean(m.get("content"))
    return out


_NOISE_TAGS = {"nav", "header", "footer", "aside", "form", "noscript",
               "script", "style", "svg", "template", "iframe"}
_NOISE_WORDS = {"nav", "navbar", "menu", "sidebar", "footer", "header",
                "breadcrumb", "pagination", "cookie", "advert", "ads",
                "comment", "related", "share", "social"}


def _region_fields(dom):
    """把 egress_route 的复合判据写进记录，供 fine_classify 打地域/路由标签。

    纯本地（classify_detail 自带 DNS_TTL 缓存），import 失败也不影响抓取。
    """
    try:
        import egress_route
        d = egress_route.classify_detail(dom, 443)
        return {"region": d["region"],
                "region_basis": d["source"] + ":" + d["reason"]}
    except Exception:
        return {"region": "", "region_basis": ""}


def _is_noise(el):
    try:
        tag = el.tag.lower() if isinstance(el.tag, str) else ""
        if tag in _NOISE_TAGS:
            return True
        if (el.get("role") or "").lower() in ("navigation", "banner",
                                             "contentinfo", "complementary"):
            return True
        blob = ((el.get("class") or "") + " " + (el.get("id") or "")).lower()
        return bool(set(re.split(r"[^a-z0-9]+", blob)) & _NOISE_WORDS)
    except Exception:
        return False


def _main_text(doc):
    """正文抽取（trafilatura / readability 的轻量版，纯本地零新依赖）。

    老做法是 `doc.text_content()` 一把梭：导航、页脚、侧栏全算进 text_len，
    一个纯链接目录页能拿到 3000+ 字符，被误判成「内容够厚、不必渲染」，
    标签质量分也跟着虚高。这里按块挑正文（article/main/section/div），
    剔除高链接密度的导航块，返回 (正文, 正文密度, 命中标签)。

    🔴 不删任何数据：密度只影响质量分与是否需要渲染，全文仍保留兜底。
    """
    import copy
    try:
        d = copy.deepcopy(doc)
    except Exception:
        d = doc
    full = _clean(d.text_content())
    try:
        for el in list(d.xpath("//*")):
            if _is_noise(el):
                # 不规则 HTML 里 <nav> 可能没闭合，把后面整页都吞进子树。
                # 整块删会连正文一起带走 ⇒ 含长正文/语义块的噪声节点不删整块。
                try:
                    if (len(_clean(el.text_content())) > 800
                            and el.xpath(".//article|.//main|.//section")):
                        continue
                except Exception:
                    pass
                p = el.getparent()
                if p is not None:
                    p.remove(el)
    except Exception:
        pass
    best, best_len, best_tag, best_score = "", 0, "", 0.0
    try:
        for el in list(d.xpath("//article|//main|//section|//div|//body"))[:400]:
            t = _clean(el.text_content())
            if len(t) < 120:
                continue
            link_chars = sum(len(_clean(a.text_content()))
                             for a in list(el.xpath(".//a"))[:80])
            if link_chars / max(1, len(t)) > 0.55:   # 链接目录，不是正文
                continue
            tag = el.tag if isinstance(el.tag, str) else ""
            # 语义标签优先：article/main 比包裹它的 body 更像真正的正文块
            score = len(t) * (1.35 if tag in ("article", "main") else 1.0)
            if score > best_score:
                best, best_len, best_tag, best_score = t, len(t), tag, score
    except Exception:
        pass
    text = best if best_len >= max(180, len(full) * 0.3) else (full or best)
    return text[:200000], round(len(text) / max(1, len(full)), 3), best_tag


def _extract_lxml(html):
    doc = lhtml.fromstring(html)
    for bad in doc.xpath("//script|//style|//noscript|//svg|//template"):
        try:
            bad.getparent().remove(bad)
        except Exception:
            pass
    meta = _meta_map(doc)
    og = {k: v for k, v in meta.items() if k.startswith("og:") or k.startswith("twitter:")}
    headings = []
    for h in doc.xpath("//h1|//h2|//h3"):
        t = _clean(h.text_content())
        if t:
            headings.append(t[:120])
        if len(headings) >= 25:
            break
    links = []
    for a in doc.xpath("//a[@href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        links.append({"url": href[:500], "text": _clean(a.text_content())[:80]})
        if len(links) >= 300:
            break
    assets = []
    for u in doc.xpath("//img/@src|//script/@src|//link/@href|//video/@src|//audio/@src"):
        if u and not u.startswith("data:"):
            assets.append({"url": u[:400], "type": "", "ctype": ""})
        if len(assets) >= 200:
            break
    # 与 JS_EXTRACT 严格对齐：jsonld 是「原始 JSON 文本」列表，不是解析后的 dict，
    # headings 是纯字符串列表（fine_classify._blob 会直接 " ".join(headings)）
    jsonld = []
    for s in doc.xpath('//script[@type="application/ld+json"]'):
        t = (s.text or "").strip()
        if t:
            jsonld.append(t[:3000])
        if len(jsonld) >= 5:
            break
    text, density, main_tag = _main_text(doc)
    return {
        "title": _clean(doc.findtext(".//title")),
        "desc": meta.get("description", ""),
        "keywords": meta.get("keywords", ""),
        "lang": (doc.get("lang") or "")[:16],
        "og": og,
        "jsonld": jsonld,
        "headings": headings,
        "text": text,
        "text_density": density,
        "main_tag": main_tag,
        "links": links,
        "assets": assets,
    }


def _extract_regex(html):
    """lxml 不可用/解析失败时的兜底，字段名与 lxml 版一致。"""
    m = TITLE_RE.search(html)
    body = _clean(TAG.sub(" ", STRIP.sub(" ", html)))
    meta = {}
    for raw in META_RE.findall(html):
        attrs = {}
        for k, _v, dq, sq, bare in ATTR_RE.findall(raw):
            attrs[k.lower()] = dq or sq or bare
        key = (attrs.get("name") or attrs.get("property") or "").lower()
        if key and key not in meta:
            meta[key] = _clean(attrs.get("content"))
    links = [{"url": u[:500], "text": ""} for u in A_RE.findall(html)[:300]]
    assets = [{"url": u[:400], "type": "", "ctype": ""} for u in ASSET_RE.findall(html)[:200]]
    return {
        "title": _clean(TAG.sub("", m.group(1))) if m else "",
        "desc": meta.get("description", ""),
        "keywords": meta.get("keywords", ""),
        "lang": "",
        "og": {k: v for k, v in meta.items() if k.startswith("og:") or k.startswith("twitter:")},
        "jsonld": [],
        "headings": [],
        "text": body,
        "text_density": None,       # 正则兜底算不了块密度，不瞎给数
        "main_tag": "",
        "links": links,
        "assets": assets,
    }


def _extract(html):
    if lhtml is not None:
        try:
            return _extract_lxml(html)
        except Exception:
            pass
    return _extract_regex(html)


def _build(dom, url, scheme, status, data, elapsed, last_modified=""):
    """组装记录，字段与 render_parse._build_rec 保持同构。

    rendered=False / tier="http" 明示「这是静态抓取，没跑过 JS」，
    下游统计不会把它误当成渲染成功。
    """
    import render_parse as rp          # 延迟导入：避免模块级循环依赖

    links = data.get("links") or []
    new_domains = []
    for l in links:
        h = _host(l.get("url", ""))
        if (h and h != dom and not h.startswith(dom + ".")
                and h not in rp.NOISE_HOSTS
                and not any(h.endswith("." + n) for n in rp.NOISE_HOSTS)):
            new_domains.append(h)
    new_domains = sorted(set(new_domains))[:120]
    text = data.get("text") or ""
    # 站内深挖：HTTP 一级就顺带把「下一层该抓什么」挖出来，零额外请求。
    # 只有内容够厚的页面才展开（导航页展开出来的还是导航页）。
    deep = 0
    try:
        deep = int(rp.netpolicy.cfg().get("deep_per_site", 8))
    except Exception:
        deep = 8
    su = rp.site_urls(url, links, limit=deep) if (deep and len(text) >= 1500) else []
    og = data.get("og") or {}
    jsonld = data.get("jsonld") or []
    reg = _region_fields(dom)
    rec = {
        "domain": dom, "url": url, "final_url": url,
        "ok": True, "status": status, "channel": "direct(dynamic)",
        "scheme": scheme, "rendered": False, "degraded": False, "tier": "http",
        "title": data.get("title", ""), "desc": data.get("desc", ""),
        "keywords": data.get("keywords", ""), "lang": data.get("lang", ""),
        "og": og, "jsonld": jsonld,
        "structured": bool(og or jsonld),
        "headings": data.get("headings", []),
        "text": text[:200000], "text_len": len(text),
        "text_density": data.get("text_density"),
        "main_tag": data.get("main_tag") or "",
        "region": reg["region"], "region_basis": reg["region_basis"],
        "egress": "direct", "last_modified": last_modified or "",
        "links": links[:300], "new_domains": new_domains,
        "site_urls": su,
        "assets": (data.get("assets") or [])[:200],
        "xhr_count": 0, "xhr": [], "response_count": 1,
        "fetch_ms": int(elapsed * 1000),
        "ts": time.time(),
    }
    rec["tags"] = rp.fine_classify.tag(rec)
    return rec


def _candidate_urls(dom, seed):
    """待试 URL 列表。

    ⚠️ 以前这里写死 "https://{dom}/"，于是队列里的站内页 URL 全被丢掉，
       再多的深度发现也抓不到 —— 深度等于 0。
    现在：页面级任务只抓那一页（换 scheme 重试），域名级才抓首页。
    """
    seed = (seed or "").strip()
    if seed.startswith("http://") or seed.startswith("https://"):
        try:
            p = urllib.parse.urlsplit(seed)
        except Exception:
            return ["https://%s/" % dom]
        if not p.netloc:
            return ["https://%s/" % dom]
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        alt = "http" if p.scheme == "https" else "https"
        return [seed, "%s://%s%s" % (alt, p.netloc, path)]
    return ["https://%s/" % dom, "http://%s/" % dom]


# 🔴 单个响应体的硬上限。踩过的坑（2026-11-05）：某个站的响应体在解压后
# 把内存吃光，`r.text` 抛 MemoryError，直接带走整个 pipeline 进程 ——
# 日志里只留下三行 "internal buffer error : Memory allocation failed"。
# 正常网页 100 KB 量级，4 MB 已经覆盖到 99.9%；超限的一律记下来等人工看，
# 绝不静默丢，也绝不交给浏览器（浏览器同样会被它撑死）。
MAX_BODY = 4 * 1024 * 1024

# ------------------------------------------------------------ 网页 / 文件 分流
# 踩过的坑（2026-11-05）：把 .msix / .exe / .zip 这类下载链接当网页读，
# 响应体几十上百 MB，解压进内存直接 MemoryError 带走整个进程。
# 只限长（MAX_BODY）只是「拦下不崩」，仍然是把文件当网页处理 —— 东西在门口没被接住。
# 正确做法是看响应头就分流：是文件 → 登记进下载队列（assets.db），一个字节都不读；
# 是网页 → 才进解析。这样「先有」才真正落到硬盘上。
_TEXTY_CT = (
    "text/html", "application/xhtml", "text/plain", "application/xml",
    "text/xml", "application/json", "application/ld+json", "application/rss",
    "application/atom", "text/css", "text/javascript", "application/javascript",
)
# 明确是「站点生成了但实质是文件流」的类型
_BINARY_CT = (
    "application/octet-stream", "binary/octet-stream", "application/zip",
    "application/pdf", "application/gzip", "application/x-tar",
    "application/x-7z", "application/x-rar", "application/x-zstd",
    "application/x-msdownload", "application/vnd.microsoft.portable-executable",
    "application/vnd.android.package-archive", "application/x-apple-diskimage",
    "application/x-deb", "application/x-rpm", "application/vnd.ms-cab",
    "application/epub", "audio/", "video/", "image/", "font/",
)


def _ctype_is_file(ctype):
    """响应头是不是「文件」而不是「网页」。

    判定顺序很关键：先看文本白名单，再看文件黑名单，最后默认当网页。
    默认当网页是有意的 —— 漏判一个网页只是白跑一次解析；
    误判一个网页为文件则会丢掉整站内容。
    """
    c = (ctype or "").split(";")[0].strip().lower()
    if not c:
        return False
    if any(t in c for t in _TEXTY_CT):
        return False
    if any(t in c for t in _BINARY_CT):
        return True
    # 其余 application/* 与未知类型：带这些前缀的一律按文件处理
    return c.startswith(("application/", "image/", "audio/", "video/", "font/"))



class _NoSem:
    """滚动调度模式下每个 worker 自己就是一个并发槽，不需要额外信号量。

    保留同一个 `async with` 写法，是为了让 `_one` 只有一份实现 ——
    两份拷贝迟早会走偏，而这里每个字段的判定都踩过坑。
    """
    __slots__ = ()

    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


async def _get_html(cli, url, req_tmo=None):
    """流式取页面并限长，返回 (status, html, err, info)。

    为什么不用 r.text：
        它一次性把整个响应解码进内存。遇到大文件/压缩炸弹就是 MemoryError，
        而 MemoryError 发生在 try 之外，会直接带走整个 pipeline 进程。
        改成逐块读取 + 累计上限：超过 MAX_BODY 立刻停手，连接正常关闭。

    为什么先看 Content-Type：
        限长只是「不被撑死」，文件仍然被当成网页解析、然后丢进 too-big 记一笔了事。
        看清类型后直接返回 err="asset:<ctype>"，调用方把它登记进下载队列 ——
        一个字节都不读，既省内存也把资源真正接住了。
    """
    kw = {"timeout": req_tmo} if req_tmo is not None else {}
    async with cli.stream("GET", url, **kw) as r:
        status = r.status_code
        ctype = (r.headers.get("content-type") or "").strip()
        info = {"ctype": ctype, "clen": 0,
                "disp": r.headers.get("content-disposition") or "",
                "last_modified": r.headers.get("last-modified") or ""}
        cl = (r.headers.get("content-length") or "").strip()
        if cl.isdigit():
            info["clen"] = int(cl)
        if _ctype_is_file(ctype):
            return status, "", "asset:%s" % ctype, info
        if info["clen"] > MAX_BODY:
            return status, "", "too-large:%d" % info["clen"], info
        buf = bytearray()
        async for chunk in r.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > MAX_BODY:
                info["clen"] = len(buf)
                return status, "", "too-large:stream>%d" % MAX_BODY, info
        enc = r.encoding or "utf-8"
        try:
            return status, bytes(buf).decode(enc, errors="replace"), "", info
        except LookupError:
            return status, bytes(buf).decode("utf-8", errors="replace"), "", info


def _file_rec(dom, base, url, status, gerr, info):
    """把「下载链接」记成一条**成功**记录，并登记进资源队列。

    为什么记 ok=True：这一条任务确实处理完了 —— 我们知道它是什么、把它送进了
    下载队列。记成失败会让它进冷却池被反复重试，而重试永远不会变成网页。
    channel 带 /asset 后缀，统计时能和普通网页区分开。
    """
    import render_parse as rp
    ctype = (info or {}).get("ctype", "")
    disp = (info or {}).get("disp", "")
    name = ""
    if "filename=" in disp.lower():
        name = disp.split("filename=", 1)[1].strip().strip('"; ')
    queued, canon, kind = False, url, ""
    try:
        import download_assets as da
        kind = da.asset_kind(url, ctype, name)
        queued, canon = da.enqueue_direct(url, src=dom, ctype=ctype, filename=name)
    except Exception as e:
        # 队列写失败不能让整批崩：留一条 err 让人看得见
        gerr = "%s|enqueue-fail:%s" % (gerr, str(e)[:50])
    title = name or urllib.parse.unquote(
        urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]) or dom
    rec = {"domain": dom, "url": url, "final_url": url, "ok": True,
           "status": status, "channel": "direct(dynamic)/asset",
           "scheme": "https" if url.startswith("https") else "http",
           "rendered": False, "degraded": False, "tier": "http",
           "title": title[:200], "desc": ctype, "keywords": "", "lang": "",
           "og": {}, "jsonld": [], "structured": False, "headings": [],
           "text": "", "text_len": 0, "links": [], "new_domains": [],
           "site_urls": [], "assets": [{"url": url, "type": "download",
                                        "ctype": ctype[:60]}],
           "xhr_count": 0, "xhr": [], "response_count": 1,
           "bytes": (info or {}).get("clen", 0), "asset_queued": queued,
           "asset_kind": kind, "tags": {},
           "err": gerr, "fetch_ms": 0, "ts": time.time()}
    # tags 的约定是 dict（fine_classify.tag 的返回值），mark_done 会读 tags["quality"]。
    # 🔴 绝不能把它换成 list：2026-11-05 我就是这么把一轮 33 条结果连进程一起搞没的。
    rec["tags"] = rp.fine_classify.tag(rec)
    if kind:
        rec["tags"]["kind"] = kind
    return rec


def _dead_rec(dom, base, err, channel="direct(dynamic)/dead"):
    """连不上 / 响应体超限 ⇒ 一条留档的失败记录（ok=False）。

    域名本体一个都不删：done 表里留着，冷却期过后 pending_domains 会再取到它。
    channel 决定冷却窗口长短（render_parse 按 channel 区分 /dead）。
    """
    import render_parse as rp
    rec = {"domain": dom, "url": base, "final_url": base, "ok": False,
           "status": 0, "channel": channel, "rendered": False,
           "degraded": False, "tier": "http", "title": "", "desc": "",
           "text": "", "text_len": 0, "headings": [], "links": [],
           "new_domains": [], "assets": [], "xhr_count": 0, "xhr": [],
           "structured": False, "err": err or "unreachable",
           "fetch_ms": 0, "ts": time.time()}
    rec["tags"] = rp.fine_classify.tag(rec)
    return rec


async def _one(cli, sem, item, timeout, min_text, allow_dead, connect_to=None):
    dom = item["domain"]
    seed = (item.get("url") or "").strip()
    base = seed if seed.startswith("http") else ("https://%s/" % dom)
    t0 = time.time()
    last_err = ""
    # 单次请求的连接超时（滚动模式下逐次放宽）；None = 用 client 的默认值。
    req_tmo = None
    if connect_to is not None:
        req_tmo = httpx.Timeout(connect=connect_to, read=timeout,
                                write=timeout, pool=max(30.0, timeout * 4))
    # 🔴 信号量必须真的包住请求：漏了它 1000 个请求会一股脑砸进连接池，
    # 全部 PoolTimeout 然后被误判成「死站」（已踩过一次，2962 条）。
    async with (sem if sem is not None else _NoSem()):
        for url in _candidate_urls(dom, seed):
            scheme = "https" if url.startswith("https") else "http"
            # 只有「网络层」异常才代表不可达；提取/组装出错必须交浏览器重跑，
            # 否则一个字段格式问题就会把整批正常域误判成死站（已踩过一次）。
            try:
                status, html, gerr, info = await _get_html(cli, url, req_tmo)
            except Exception as e:
                last_err = "%s: %s" % (type(e).__name__, str(e)[:70])
                if "Timeout" in type(e).__name__:
                    break
                continue
            if gerr:
                last_err = gerr
                # ① 响应头认定是文件 ⇒ 登记进下载队列，一个字节都不读。
                #    记 ok=True：任务确实解决了（识别 + 入队），不该进冷却池反复撞。
                if gerr.startswith("asset:"):
                    return _file_rec(dom, base, url, status, "", info), None
                # ② 声明是文档但体积超过上限：仍按失败留档（没解析成功），
                #    但如果它恰好是下载链接，顺手塞进队列，别白发现一次。
                if gerr.startswith("too-large"):
                    try:
                        import download_assets as da
                        da.enqueue(url, src=dom)
                    except Exception:
                        pass
                    return _dead_rec(dom, base, gerr, channel="direct(dynamic)/too-big"), None
                continue
            try:
                data = await asyncio.to_thread(_extract, html)
            except Exception as e:
                return None, {"domain": dom, "url": url,
                              "hard": bool(item.get("hard")),
                              "why": "extract-fail: %s" % str(e)[:60]}
            text = data.get("text") or ""
            title = (data.get("title") or "").strip()
            elapsed = time.time() - t0
            # 🔴 2026-11-09 修：老判定是 `title or text>=min_text`，SPA 页只要写了
            # <title>My App</title> 就被当成「抓到了」，body 里其实只有 <div id=root>
            # 空壳，永远轮不到浏览器。现在要求「有标题 + 正文≥120」或正文≥min_text，
            # 并显式识别 root/app/__next 容器 ⇒ 空壳更早、更确定地进渲染队列。
            spa_shell = (len(text) < 120 and re.search(
                r"<div[^>]+id=[\"']?(?:root|app|__next)[\"']?[^>]*>\s*</div>",
                html[:30000], re.I))
            if spa_shell:
                return None, {"domain": dom, "url": base,
                              "hard": bool(item.get("hard")),
                              "why": "spa-shell", "render_priority": 10}
            if (title and len(text) >= 120) or len(text) >= min_text:
                try:
                    return _build(dom, url, scheme, status, data, elapsed,
                                  info.get("last_modified", "")), None
                except Exception as e:
                    return None, {"domain": dom, "url": url,
                                  "hard": bool(item.get("hard")),
                                  "why": "build-fail: %s" % str(e)[:60]}
            return None, {"domain": dom, "url": base,
                          "hard": bool(item.get("hard")),
                          "why": "shell(t=%d,text=%d)" % (len(title), len(text)),
                          "render_priority": 6}
    if allow_dead:
        # 连不上 ⇒ 记一条失败（走 done 表的冷却重试），不占用浏览器预算。
        # channel 带 /dead 标记：pending_domains 据此给它更长的冷却窗口，
        # 避免每轮都重新付一次超时代价。域名本体一个都不删。
        return _dead_rec(dom, base, last_err), None
    # 网络层失败 ⇒ 标 retryable：外层会放宽超时二次/三次确认，不许一次判死。
    return None, {"domain": dom, "url": base, "hard": bool(item.get("hard")),
                  "why": last_err or "http-fail", "retryable": True}


def _raise_dns_threads(n=512):
    """🔴 决定成败的一行：anyio 默认线程池只有 40，而 getaddrinfo（DNS 解析）
    全在这个池子里排队。64 并发下几百个域名同时要解析，排队时间直接超过
    connect 超时 ⇒ 大批正常站被记成 ConnectTimeout 判死（实测误杀率一度 100%）。

    实测（300 个「已判死」样本，同样是 4 秒 connect 超时）：
        线程池 40（默认） → 成功率 44%
        线程池 512        → 成功率 80%，而且总耗时还更短（27.7s vs 28.9s）
    """
    try:
        import anyio.to_thread
        lim = anyio.to_thread.current_default_thread_limiter()
        if lim.total_tokens < n:
            lim.total_tokens = n
        return lim.total_tokens
    except Exception:
        return 0


# 判死前的多轮确认：第一轮短超时 + 高并发（快筛），失败才进下一轮（放宽超时）。
# 好站第一轮就走掉；慢站/抖动站靠二三轮捞回来；只有真死站才付满整条预算。
# 取值原则：**优先成功**，速度够用就行 ——
# 因为判死判错，将来重试要再花一遍时间，不如当场做对。
# 后两轮并发不降：待处理的都是「难啃的」，降并发只会让它们排更多批、白等更多超时。
_ROUNDS = ((64, 4.0), (64, 10.0), (96, 10.0))


def fetch_many(items, conc=64, timeout=10.0, min_text=500, allow_dead=True,
               log=print, rounds=None, proxy=_AUTO):
    """第一级漏斗：返回 (已解决记录, 需浏览器渲染的 items)。

    判死要过三关：4s → 10s → 18s（并发逐轮递减，超时逐轮放宽）。
    三关全败才产出 ok=False 记录；空壳（有页面但要 JS）不进重试，直接交浏览器。
    """
    if not items:
        return [], []
    base_conc = max(1, min(int(conc), 96))
    plan = [(min(int(c), base_conc), float(t)) for c, t in (rounds or _ROUNDS)]
    headers = {
        "User-Agent": __import__("netpolicy").UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    async def _pass(pending, pconc, connect_to, is_last):
        # 并发跟着待处理量走：第三轮往往只剩几十条，固定 24 并发会白排好几批，
        # 每批都要等满超时才轮到下一批 —— 小批量就该一把梭。
        pconc = max(1, min(pconc, len(pending)))
        lim = httpx.Limits(max_connections=pconc,
                           max_keepalive_connections=pconc)
        # trust_env=False ⇒ 绝不读系统代理，与产线「纯直连」纪律一致
        tmo = httpx.Timeout(connect=connect_to, read=timeout,
                            write=timeout, pool=max(30.0, timeout * 4))
        async with httpx.AsyncClient(
                verify=False, timeout=tmo, limits=lim,
                follow_redirects=True, trust_env=False,
                headers=headers, **_client_kw(proxy, log)) as cli:
            sem = asyncio.Semaphore(pconc)
            tasks = [_one(cli, sem, it, timeout, min_text,
                          allow_dead and is_last) for it in pending]
            return await asyncio.gather(*tasks)

    async def _main():
        _raise_dns_threads()
        solved, pending, browser = [], list(items), []
        for i, (pconc, connect_to) in enumerate(plan):
            if not pending:
                break
            is_last = (i == len(plan) - 1)
            pairs = await _pass(pending, pconc, connect_to, is_last)
            pending = []
            for rec, rest in pairs:
                if rec is not None:
                    solved.append(rec)
                elif rest is None:
                    continue
                elif rest.get("retryable") and not is_last:
                    pending.append(rest)
                else:
                    browser.append(rest)
            if pending:
                nxt = plan[i + 1][1] if not is_last else connect_to
                log("  [http] 第 %d 轮仍 %d 条未定 → 放宽到 %.0fs 二次确认"
                    % (i + 1, len(pending), nxt), flush=True)
        return solved, browser

    t0 = time.time()
    done, rest = asyncio.run(_main())
    dt = time.time() - t0
    ok_n = sum(1 for r in done if r.get("ok"))
    log("  [http] 直取 %d 域：解决 %d（成功 %d，死站 %d）→ 剩 %d 交浏览器 | %.1fs %.2f 域/秒"
        % (len(items), len(done), ok_n, len(done) - ok_n, len(rest), dt,
           len(items) / max(1e-9, dt)), flush=True)
    # 全灭时把真实错误打出来 —— 否则一个字段/环境问题会被看成「全网都挂了」
    if done and ok_n == 0:
        for r in [x for x in done if not x.get("ok")][:3]:
            log("  [http] 失败样本 %s → %s" % (r.get("domain"), r.get("err")),
                flush=True)
        for r in rest[:3]:
            log("  [http] 待渲染样本 %s → %s" % (r.get("domain"), r.get("why")),
                flush=True)
    return done, rest


# 滚动模式的超时阶梯：第 1 次 4s，第 2 次 10s，第 3 次 10s。
# 与 _ROUNDS 的区别是「谁等」——旧版让整批等，这里只让超时的那一条自己等。
_SLIDE_TO = (4.0, 10.0, 10.0)


def fetch_many_slide(items, conc=96, timeout=10.0, min_text=500,
                     allow_dead=True, log=print, ladder=None, proxy=_AUTO):
    """第一级漏斗 · 滚动窗口版：worker 空出来就立刻补位，慢站单独回队尾升级超时。

    为什么要换掉「三轮齐步走」：
        旧版每轮一个 asyncio.gather 屏障 —— 一批 64 条里只要有一条吃满 4s，
        其余 63 条哪怕 0.3s 就返回了，也必须等在屏障上一起进下一轮。
        实测 1000 域 108–172s，而真正的网络等待只占其中三成。

    现在：
        队列 + N 个常驻 worker，谁空谁取；超时那条带着「第几次尝试」回到队尾，
        下次给它更长的连接超时。好站全程不排队，慢站不拖累任何人。
        整批共用一个 AsyncClient ⇒ 连接池与 TLS 会话跨轮复用。
    语义与结果跟 fetch_many 完全一致（同样的判死三关、同样的空壳判定）。
    """
    if not items:
        return [], []
    conc = max(1, min(int(conc), 256))
    ladder = tuple(ladder or _SLIDE_TO)
    headers = {
        "User-Agent": __import__("netpolicy").UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    async def _main():
        _raise_dns_threads(768)
        q = asyncio.Queue()
        for it in items:
            q.put_nowait((dict(it), 0))
        solved, browser = [], []
        lock = asyncio.Lock()
        lim = httpx.Limits(max_connections=conc, max_keepalive_connections=conc)
        tmo = httpx.Timeout(connect=ladder[0], read=timeout,
                            write=timeout, pool=max(30.0, timeout * 4))
        async with httpx.AsyncClient(
                verify=False, timeout=tmo, limits=lim,
                follow_redirects=True, trust_env=False,
                headers=headers, **_client_kw(proxy, log)) as cli:

            async def worker():
                while True:
                    it, attempt = await q.get()
                    try:
                        cto = ladder[min(attempt, len(ladder) - 1)]
                        is_last = attempt >= len(ladder) - 1
                        rec, rest = await _one(
                            cli, None, it, timeout, min_text,
                            allow_dead and is_last, connect_to=cto)
                        if rec is not None:
                            async with lock:
                                solved.append(rec)
                        elif rest is None:
                            pass
                        elif rest.get("retryable") and not is_last:
                            q.put_nowait((it, attempt + 1))
                        else:
                            async with lock:
                                browser.append(rest)
                    except Exception as e:
                        # worker 不能被单条任务的异常打死 —— 那会让整批悄悄少几条，
                        # 而且没人报错。所有意外一律当成「交给浏览器」。
                        async with lock:
                            browser.append({
                                "domain": it.get("domain", ""),
                                "url": it.get("url") or it.get("domain", ""),
                                "why": "worker-err: %s" % str(e)[:60],
                                "hard": bool(it.get("hard"))})
                    finally:
                        q.task_done()

            ws = [asyncio.create_task(worker())
                  for _ in range(min(conc, len(items)))]
            await q.join()          # 重试任务 put 回去会延长 join，语义天然正确
            for w in ws:
                w.cancel()
            await asyncio.gather(*ws, return_exceptions=True)
        return solved, browser

    t0 = time.time()
    done, rest = asyncio.run(_main())
    dt = time.time() - t0
    ok_n = sum(1 for r in done if r.get("ok"))
    log("  [http] 滚动 %d 域：解决 %d（成功 %d，死站 %d）→ 剩 %d 交浏览器 | %.1fs %.2f 域/秒"
        % (len(items), len(done), ok_n, len(done) - ok_n, len(rest), dt,
           len(items) / max(1e-9, dt)), flush=True)
    if done and ok_n == 0:
        for r in [x for x in done if not x.get("ok")][:3]:
            log("  [http] 失败样本 %s → %s" % (r.get("domain"), r.get("err")),
                flush=True)
        for r in rest[:3]:
            log("  [http] 待渲染样本 %s → %s" % (r.get("domain"), r.get("why")),
                flush=True)
    return done, rest
