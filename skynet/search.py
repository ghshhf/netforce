"""天网 · 搜索层（search）
==========================
目标不是「能发出查询」，而是「结果的每一条都跟意图有关」。

实地教训（2026-10-21 实测）：Bing 的 RSS 端在中国区已被污染 —— 查
「碧蓝航线 语音 台词 wiki」返回**香港清水湾楼盘**，查「open source game voice
lines dataset」返回**剑桥词典的 open 词条**。原 resource_finder 的 `_soup_urls`
只做「抽链接 + 去垃圾域」，完全不校验意图，于是这些垃圾照单全收。

所以本层有三道闸：

    ① 严格抽取  只从结果容器里取（RSS item / DDG .result__a / SearX JSON），
                每条都带 **标题** —— 标题是判相关的唯一可靠依据
    ② 意图校验  relevance(query, url, title) 按 token 覆盖率打分，不过关直接丢
    ③ 引擎健康  每个引擎记录「提交数 / 存活数」，长期 0 相关的引擎自动降权直至停用

两类通道分工明确：
    · 通用网搜（找「哪个站」）      ddg / searx / marginalia / bing / github
    · 站内搜索（找「这个站里哪页」）MediaWiki list=search —— 对 wiki 站命中率最高，
      实测在 sr 站搜「语音」直接给出「刃/语音」「青雀/语音」这类页面标题
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import net

# ---------------------------------------------------------------- 归一化
_TRACK = re.compile(
    r"(?:^|&)(utm_[a-z]+|ref|ref_src|spm|from|share_source|share_medium|fbclid|gclid)=[^&]*", re.I)
JUNK_HOST = re.compile(
    r"(^|\.)((img|cdn|static|assets|font|badge|shields|analytics|ads?|track)\b)|"
    r"(googleusercontent|amazonaws\.com|jsdelivr|unpkg|cloudfront|githubusercontent|"
    r"dictionary\.|cambridge|openrice|centanet)", re.I)
BAD_EXT = re.compile(r"\.(png|jpe?g|gif|svg|ico|css|js|woff2?|ttf|zip|exe|dmg|mp3|mp4)$", re.I)


def canon(u: str) -> str:
    try:
        p = urllib.parse.urlsplit(u.strip())
    except Exception:
        return ""
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    q = _TRACK.sub("", p.query).strip("&")
    path = p.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(("https", host, path, q, ""))


def host_of(u: str) -> str:
    return urllib.parse.urlparse(u).netloc.lower().removeprefix("www.")


# ---------------------------------------------------------------- 意图校验
_STOP = {"the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "with", "how", "what",
         "的", "了", "是", "和", "与", "在", "我", "要", "找", "一个", "一些", "怎么", "什么"}
_WORD = re.compile(r"[a-z0-9]{2,}")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def tokens(text: str) -> list[str]:
    """英文按词，中文按 2-gram —— 中文不分词也能抓到「语音 / 台词」这类核心词。"""
    t = (text or "").lower()
    out = [w for w in _WORD.findall(t) if w not in _STOP and len(w) >= 2]
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", t):
        out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return list(dict.fromkeys(out))


def relevance(query: str, url: str, title: str = "") -> float:
    """0~1：查询 token 在 (标题 + 解码后的 URL) 里的覆盖率。

    标题权重 2、URL 权重 1 —— 域名里带关键词但内容无关的情况很常见。
    """
    qt = tokens(query)
    if not qt:
        return 0.0
    hay_t = tokens(title)
    hay_u = tokens(urllib.parse.unquote(url))
    score = 0.0
    for t in qt:
        if t in hay_t:
            score += 1.0
        elif t in hay_u:
            score += 0.5
    return round(min(1.0, score / len(qt)), 3)


MIN_REL = 0.18          # 低于此判无关（实测：香港楼盘/剑桥词典均为 0）


# ---------------------------------------------------------------- 结果结构
@dataclass
class Hit:
    url: str
    host: str
    title: str = ""
    engines: list = field(default_factory=list)
    rel: float = 0.0
    score: float = 0.0

    @property
    def cross(self) -> int:
        return len(self.engines)

    def brief(self) -> str:
        return (f"{self.score:5.2f} rel{self.rel:.2f} x{len(self.engines)} "
                f"{'/'.join(self.engines)[:26]:26s} {self.title[:34]:34s} {self.url[:60]}")


# ---------------------------------------------------------------- 引擎实现
def _rss_items(html: str) -> list[tuple[str, str]]:
    """RSS/Atom → [(url, title)]。严格只取 item 内 link+title，不碰页面其他链接。"""
    out = []
    for it in re.findall(r"<item[^>]*>(.*?)</item>", html, re.S | re.I):
        m = re.search(r"<link[^>]*>(.*?)</link>", it, re.S | re.I)
        t = re.search(r"<title[^>]*>(.*?)</title>", it, re.S | re.I)
        if not m:
            continue
        u = re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1)).strip()
        ti = re.sub(r"<!\[CDATA\[|\]\]>", "", t.group(1)).strip() if t else ""
        if u.startswith("http"):
            out.append((u, _unesc(ti)))
    return out


def _unesc(s: str) -> str:
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def engine_bing(q: str) -> list[tuple[str, str]]:
    u = "https://www.bing.com/search?q=" + urllib.parse.quote(q) + "&format=rss&count=50"
    r = net.fetch_text(u, timeout=15, budget=15)
    return _rss_items(r.text) if r.ok else []


def engine_ddg(q: str) -> list[tuple[str, str]]:
    """DDG html 端必须 POST。链接在 result__a 的 href，实际地址藏在 uddg 参数里。"""
    body = urllib.parse.urlencode({"q": q}).encode()
    req = net.urllib.request.Request(
        "https://html.duckduckgo.com/html/", data=body,
        headers={"User-Agent": net.UA,
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        import ssl
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        op = net.urllib.request.build_opener(
            net.urllib.request.ProxyHandler({"http": net.PROXY, "https": net.PROXY}),
            net.urllib.request.HTTPSHandler(context=ctx))
        html = op.open(req, timeout=25).read().decode("utf-8", "ignore")
    except Exception:
        return []
    out = []
    for m in re.finditer(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                         html, re.S | re.I):
        href, txt = m.group(1), m.group(2)
        if "uddg=" in href:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            href = (qs.get("uddg") or [href])[0]
        href = _unesc(href)
        if href.startswith("http"):
            out.append((href, _unesc(re.sub(r"<[^>]+>", "", txt))))
    return out


SEARX_HOSTS = ["https://searx.be", "https://searxng.site", "https://search.bus-hit.me",
               "https://priv.au", "https://searx.tiekoetter.com"]
_SEARX_OK: Optional[str] = None


def engine_searx(q: str) -> list[tuple[str, str]]:
    """SearXNG JSON 端：结构化最干净，有 title+url，天然适配意图校验。

    5 个实例**并发**探测（串行要 70s，并发最慢也只有一个 timeout），
    探到能用的就记住，这轮之后都不再重复试错。
    """
    global _SEARX_OK
    hosts = [_SEARX_OK] if _SEARX_OK else SEARX_HOSTS

    def try_host(h: str):
        u = f"{h}/search?" + urllib.parse.urlencode(
            {"q": q, "format": "json", "safesearch": "0", "language": "all"})
        d = net.fetch_json(u, timeout=10, budget=12, gap=0.2)
        res = [(it["url"], it.get("title") or "") for it in (d.get("results") or [])[:40]
               if it.get("url")]
        return h, res

    for item in net.parallel(hosts, try_host, workers=len(hosts)):
        if isinstance(item, Exception) or not item:
            continue
        h, res = item
        if res:
            _SEARX_OK = h
            return res
    return []


def engine_github(q: str) -> list[tuple[str, str]]:
    """GitHub 仓库搜索 API —— 结构化、稳定，找「项目/清单」类命中率最高。"""
    u = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
        {"q": q, "sort": "stars", "per_page": "20"})
    d = net.fetch_json(u, timeout=10, budget=12, gap=0.2)
    return [(it["html_url"], f"{it.get('full_name','')} {it.get('description') or ''}")
            for it in (d.get("items") or [])]


def engine_mediawiki(q: str, tag: str = "blhx") -> list[tuple[str, str]]:
    """站内搜索：MediaWiki list=search。找「某站里的哪一页」用它，别用网搜。"""
    base = f"https://wiki.biligame.com/{tag}/"
    u = base + "api.php?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": q, "srlimit": "50",
         "srwhat": "text", "format": "json"})
    d = net.fetch_json(u, timeout=12, budget=14, gap=0.5)
    out = []
    for it in d.get("query", {}).get("search", []):
        title = it.get("title", "")
        if title:
            out.append((base + urllib.parse.quote(title.replace(" ", "_")),
                        title.replace("<", " ").replace(">", " ")))
    return out


# ---------------------------------------------------------------- 引擎注册表
@dataclass
class Engine:
    name: str
    fn: Callable
    weight: float = 1.0
    needs_tag: bool = False
    generic: bool = True


ENGINES: list[Engine] = [
    Engine("ddg", engine_ddg, 1.3),
    Engine("searx", engine_searx, 1.2),
    Engine("bing", engine_bing, 0.8),      # 已知污染，权重压低，健康度会继续压
    Engine("github", engine_github, 1.2),
    Engine("wiki", engine_mediawiki, 1.6, needs_tag=True, generic=False),
]

# 引擎健康度（进程内累积 + 落库，跨进程学习）
HEALTH: dict[str, dict] = {}
_LOADED = False


def bootstrap_health() -> None:
    """从库载入历史表现。搜索端点是会废掉的（Bing 已经废过一次），
    所以「哪个引擎还值得问」必须是可学习的状态。"""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        from . import store
        con = store.connect()
        for n, d in store.load_engines(con).items():
            HEALTH[n] = {"in": d["in"], "out": d["out"], "calls": d["calls"]}
        con.close()
    except Exception:
        pass


def _lang(q: str) -> str:
    """查询语言分桶。中文站内搜索对英文查询天然命中低，
    和中英混在一起记会把「wiki 很准」这条正确结论污染掉。"""
    return "zh" if _CJK.search(q or "") else "en"


def _bump(name: str, submitted: int, alive: int, lang: str = "") -> None:
    key = f"{name}|{lang}" if lang else name
    h = HEALTH.setdefault(key, {"in": 0, "out": 0, "calls": 0})
    h["in"] += submitted
    h["out"] += alive
    h["calls"] += 1
    try:
        from . import store
        con = store.connect()
        store.save_engine(con, key, h["calls"], h["in"], h["out"])
        con.close()
    except Exception:
        pass


def health_weight(name: str, lang: str = "") -> float:
    """拉普拉斯平滑，避免引擎被一次坏结果永久钉死，也避免新引擎被高估。

    (out+1)/(in+4) 先验 0.25 ⇒ 新引擎权重 0.75；
    实测 ddg 10→3 ⇒ 0.86；bing 10→0 ⇒ 0.21（自动沉底但不判死，以后恢复了能爬回来）。
    """
    h = HEALTH.get(f"{name}|{lang}") or HEALTH.get(name)
    if not h:
        return 1.0
    rate = (h["out"] + 1) / (h["in"] + 4)
    return round(min(1.5, rate * 3), 2)


# ---------------------------------------------------------------- 聚合
def search(query: str, wiki_tag: Optional[str] = None,
           only: Optional[list[str]] = None, min_rel: float = MIN_REL,
           workers: int = 5, budget: float = 18.0) -> list[Hit]:
    """budget：整轮搜索的时间预算。超时的引擎按「没有结果」处理，
    且不写健康度（超时不是它的错，不该被扣分）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    bootstrap_health()
    lang = _lang(query)
    pool = []
    for e in ENGINES:
        if only and e.name not in only:
            continue
        if e.needs_tag and not wiki_tag:
            continue
        pool.append(e)
    if not pool:
        return []

    raw: dict[str, dict] = {}
    def run(e: Engine):
        try:
            return e, (e.fn(query, wiki_tag) if e.needs_tag else e.fn(query))
        except Exception:
            return e, []

    ex = ThreadPoolExecutor(max_workers=workers)
    futs = [ex.submit(run, e) for e in pool]
    results = []
    try:
        for f in as_completed(futs, timeout=budget):
            try:
                results.append(f.result())
            except Exception:
                pass
    except Exception:                    # 预算耗尽
        pass
    ex.shutdown(wait=False, cancel_futures=True)

    for e, items in results:
        seen = 0
        kept = 0
        for url, title in items:
            k = canon(url)
            if not k:
                continue
            seen += 1
            host = host_of(k)
            if not host or JUNK_HOST.search(host) or BAD_EXT.search(k):
                continue
            rel = relevance(query, k, title)
            if rel < min_rel:
                continue
            kept += 1
            d = raw.setdefault(k, {"title": title, "engines": [], "rel": rel})
            if len(title) > len(d["title"]):
                d["title"] = title
            d["rel"] = max(d["rel"], rel)
            if e not in d["engines"]:
                d["engines"].append(e)
        _bump(e.name, seen, kept, lang)

    hits = []
    for k, d in raw.items():
        base = sum(e.weight * health_weight(e.name, lang) for e in d["engines"])
        cross = 0.4 * (len(d["engines"]) - 1) if len(d["engines"]) > 1 else 0.0
        hits.append(Hit(url=k, host=host_of(k), title=d["title"], engines=[e.name for e in d["engines"]],
                        rel=d["rel"], score=round(base * (0.5 + d["rel"]) + cross, 2)))
    hits.sort(key=lambda h: -h.score)
    return hits


def search_queries(queries: list[str], wiki_tag: Optional[str] = None,
                   cap: int = 300, **kw) -> list[Hit]:
    merged: dict[str, Hit] = {}
    for q in queries:
        for h in search(q, wiki_tag=wiki_tag, **kw):
            cur = merged.get(h.url)
            if cur is None or h.score > cur.score:
                merged[h.url] = h
        if len(merged) >= cap * 2:
            break
        time.sleep(0.2)
    return sorted(merged.values(), key=lambda h: -h.score)[:cap]


def by_host(hits: list[Hit]) -> dict[str, int]:
    cnt: dict[str, int] = {}
    for h in hits:
        cnt[h.host] = cnt.get(h.host, 0) + 1
    return dict(sorted(cnt.items(), key=lambda x: -x[1]))
