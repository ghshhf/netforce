#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""site_backfill.py — 接通两个岛。

背景：主爬虫 resource_finder.py 只做「广度域名发现」，resources.json 里 5 万余条
记录只有 url/domain/type/found_via 四个字段，**标题率 0%**，且从不入 seeds.db。
另一个岛是 twitter/reddit/gh 等采集器，写进 seeds.db 有正文有边。
两边各跑各的 —— 这个脚本就是那座桥。

做法：旁路消费 resources.json → smart_get 抓取 → generic_extract 三层瀑布解析
→ classify 简单分类 → 写 seeds.db。不动主爬虫进程，主爬虫可以继续跑。

用法：
    python site_backfill.py --limit 500 --workers 8
    python site_backfill.py --stats
    python site_backfill.py --dry --limit 30          # 只看分类结果不入库
    python site_backfill.py --reclassify              # 给库里旧实体补分类
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from urllib.parse import urlsplit

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

RESOURCES = os.path.join(BASE, "resources.json")
STATE = os.path.join(BASE, "_kb", "backfill_state.json")
DB = os.environ.get("NETFORCE_DB") or os.path.join(BASE, "_kb", "seeds.db")

# ---------------------------------------------------------------- 分类规则
# 用户要求「简单分类」：不搞模型、不搞多层判断，就按后缀 + 路径 + 关键词打分。
# 目标是可筛可选，不是绝对准确。

GOV_SUFFIX = (".gov", ".gov.cn", ".gov.uk", ".go.jp", ".gov.tw", ".gov.hk",
              ".mil", ".europa.eu", ".gov.au", ".gov.ca", ".gov.sg")
EDU_SUFFIX = (".edu", ".edu.cn", ".ac.uk", ".ac.jp", ".ac.kr", ".edu.hk",
              ".edu.tw", ".edu.au", ".edu.sg", ".ac.at", ".ac.be", ".ac.ir")

# 主题关键词：命中即加分，取最高分那一类
TOPIC_WORDS = {
    "tech": ("github", "gitlab", "stackoverflow", "stackexchange", "devto", "dev.to",
             "hackernews", "sourceforge", "npmjs", "pypi", "crates.io", "docker",
             "kubernetes", "linux", "kernel", "python", "rust", "golang", "java",
             "javascript", "typescript", "php", "ruby", "perl", "haskell", "erlang",
             "mozilla", "w3.org", "ietf", "rfc", "openai", "huggingface", "llm",
             "paperswithcode", "lobste.rs", "osnews", "phoronix", "slashdot",
             "serverfault", "superuser", "askubuntu", "devops", "database", "sql",
             "nginx", "apache", "redis", "mysql", "postgres", "mongo", "git",
             "compiler", "algorithm", "encoding", "protocol", "network", "server",
             "hack", "coding", "program", "software", "developer", "code",
             "开源", "编程", "代码", "算法", "服务器", "数据库", "架构", "开发"),
    "academic": ("arxiv", "doi", "springer", "elsevier", "wiley", "ieee", "acm.org",
                 "nature.com", "science.org", "plos", "biorxiv", "medrxiv", "pubmed",
                 "scholar", "researchgate", "jstor", "semanticscholar", "openalex",
                 "university", "institute", "laboratory", "journal", "thesis",
                 "paper", "citation", "研究", "论文", "学报", "学院", "大学", "实验室"),
    "news": ("news", "times", "herald", "tribune", "daily", "weekly", "magazine",
             "reuters", "bloomberg", "bbc", "cnn", "npr", "guardian", "verge",
             "wired", "ars-technica", "arstechnica", "techcrunch", "engadget",
             "aljazeera", "apnews", "axios", "politico", "vox", "quartz",
             "新闻", "日报", "晚报", "时报", "周刊", "报道", "记者", "媒体"),
    "business": ("shop", "store", "buy", "cart", "price", "deal", "coupon",
                 "amazon", "ebay", "etsy", "alibaba", "aliexpress", "taobao",
                 "mall", "market", "trade", "invest", "finance", "bank",
                 "insurance", "crypto", "exchange", "company", "corp", "inc",
                 "商城", "购物", "价格", "优惠", "投资", "理财", "银行", "保险",
                 "公司", "集团", "企业", "有限", "股份"),
    "social": ("weibo", "twitter", "reddit", "facebook", "instagram", "tiktok",
               "douyin", "bilibili", "zhihu", "douban", "tieba", "mastodon",
               "telegram", "discord", "slack", "forum", "community", "bbs",
               "论坛", "社区", "贴吧", "微博", "公众号"),
    "gov": ("gov", "ministry", "department", "agency", "bureau", "authority",
            "council", "parliament", "senate", "municipal", "provincial",
            "政府", "政务", "委员会", "管理局", "办公厅", "人民政府", "党委"),
    "culture": ("blog", "diary", "personal", "photography", "travel", "food",
                "music", "art", "book", "movie", "game", "anime", "history",
                "博客", "日记", "摄影", "旅行", "美食", "音乐", "艺术", "读书",
                "电影", "游戏", "动漫", "历史", "文化"),
}

# 静态资源与非内容页：抓了也没意义，抓之前就跳
SKIP_EXT = (".js", ".mjs", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif",
            ".svg", ".webp", ".ico", ".bmp", ".woff", ".woff2", ".ttf", ".eot",
            ".json", ".xml", ".rss", ".atom", ".pdf", ".zip", ".gz", ".bz2",
            ".tar", ".7z", ".rar", ".mp4", ".webm", ".mkv", ".avi", ".mp3",
            ".wav", ".flac", ".ogg", ".exe", ".msi", ".dmg", ".apk", ".deb",
            ".rpm", ".iso", ".whl", ".jar")
SKIP_PATH = ("/login", "/signin", "/sign-in", "/logout", "/register", "/signup",
             "/sign-up", "/cart", "/checkout", "/wp-admin", "/wp-json",
             "/wp-content/", "/ajax/", "/session", "/oauth", "/auth/",
             "/captcha", "/cdn-cgi/", "/feed/", "/rss", "/sitemap")

# 内容型：看 URL 路径
KIND_PATHS = (
    ("blog", ("/blog/", "/blogs/", "/post/", "/posts/", "/article/", "/articles/",
              "/p/", "/story/", "/stories/", "/2020/", "/2021/", "/2022/",
              "/2023/", "/2024/", "/2025/", "/2026/")),
    ("news", ("/news/", "/press/", "/media/", "/headline")),
    ("docs", ("/docs/", "/doc/", "/documentation/", "/manual/", "/guide/",
              "/wiki/", "/reference/", "/api")),
    ("forum", ("/forum/", "/thread/", "/topic/", "/discuss", "/community/")),
    ("product", ("/product/", "/item/", "/dp/", "/goods/", "/shop/", "/store/")),
    ("list", ("/tag/", "/category/", "/archive/", "/index", "/page/")),
)

# 明显与知识库无关的域名（抽样实测中发现的）：直接标 offtopic，仍入库但可筛掉
OFFTOPIC_HOSTS = (
    "maps.apple.com", "maps.google.com", "bing.com/maps",
    "anjuke.com", "ke.com", "lianjia.com", "fang.com", "58.com",
    "zhaopin.com", "51job.com", "lagou.com", "boss.com",
    "meituan.com", "dianping.com", "ele.me", "ctrip.com", "qunar.com",
    "taobao.com", "tmall.com", "jd.com", "pinduoduo.com", "suning.com",
    "tommy.com.cn", "nike.com", "adidas.com",
)


def host_of(u: str) -> str:
    try:
        h = (urlsplit(u).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def domain_class(host: str) -> str:
    """域名性质：gov / edu / org / com / net / info"""
    h = host.lower()
    if any(h.endswith(s) or ("." + s.strip(".") + ".") in ("." + h + ".") for s in GOV_SUFFIX):
        return "gov"
    if any(h.endswith(s) for s in EDU_SUFFIX):
        return "edu"
    if h.endswith(".org") or h.endswith(".org.cn"):
        return "org"
    if h.endswith(".gov") or ".gov." in h:
        return "gov"
    if h.endswith(".com") or h.endswith(".com.cn") or h.endswith(".co"):
        return "com"
    if h.endswith(".net") or h.endswith(".net.cn"):
        return "net"
    if h.endswith(".io"):
        return "com"
    if h.endswith(".ai"):
        return "com"
    tld = h.rsplit(".", 1)[-1] if "." in h else ""
    if tld in ("cn", "hk", "tw", "jp", "kr", "uk", "de", "fr", "ru", "sg"):
        return "regional"
    return "other"


def content_kind(url: str, rec: dict | None = None) -> str:
    """内容型：先信解析器给的 kind，再按 URL 路径猜。"""
    if rec:
        k = (rec.get("kind") or "").strip().lower()
        if k in ("article", "news", "blog", "product", "forum", "docs"):
            return k
        if rec.get("price") or rec.get("sku"):
            return "product"
    path = (urlsplit(url).path or "/").lower()
    for name, pats in KIND_PATHS:
        if any(p in path for p in pats):
            return name
    if path in ("", "/"):
        return "homepage"
    return "page"


def classify_topic(host: str, url: str, title: str = "", body: str = "") -> str:
    """主题：域名关键词优先，命中不了就按域名后缀兜底，最后才落 other。

    纯 URL 场景下没有正文，关键词命中率天然很低，所以必须有后缀兜底，
    否则 5 万条里八成都进 other，等于没分类。
    """
    hay = ("%s %s %s %s" % (host, url, title or "", (body or "")[:800])).lower()
    scores = {}
    for topic, words in TOPIC_WORDS.items():
        s = 0
        for w in words:
            if w in hay:
                # 域名里命中权重更高
                s += 3 if w in host else 1
        scores[topic] = s
    best = max(scores, key=lambda k: scores[k])
    if scores[best] >= 2:
        return best

    # —— 兜底：后缀是最可靠的信号，比关键词稳 ——
    h = host.lower()
    if any(h.endswith(s) for s in GOV_SUFFIX) or ".gov." in h:
        return "gov"
    if any(h.endswith(s) for s in EDU_SUFFIX):
        return "academic"
    if h.endswith(".org") or h.endswith(".org.cn"):
        return "org"
    if h.endswith(".mil") or h.endswith(".int"):
        return "gov"
    return "other"


def detect_lang(title: str, body: str = "") -> str:
    """粗判语种：标题里中文字符占比。够用即可，不引模型。"""
    s = (title or "") + (body or "")[:200]
    if not s:
        return "unknown"
    zh = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    latin = sum(1 for ch in s if ch.isascii() and ch.isalpha())
    if zh == 0 and latin == 0:
        return "unknown"
    if zh >= 2 and zh * 4 >= latin:
        return "zh"
    if latin > 0:
        return "en"
    return "other"


def should_skip(url: str) -> bool:
    """抓之前就排除：静态资源、登录/购物车/接口端点。"""
    try:
        sp = urlsplit(url)
    except Exception:
        return True
    path = (sp.path or "/").lower()
    host = (sp.hostname or "").lower()
    if any(path.endswith(e) for e in SKIP_EXT):
        return True
    if any(p in path for p in SKIP_PATH):
        return True
    # API 端点：主机名以 api. / api- 开头，或路径里明确 /api/
    if host.startswith("api.") or host.startswith("api-") or "/api/" in path:
        return True
    if host.startswith("cdn.") or host.startswith("static.") or host.startswith("assets."):
        return True
    # 带大量查询参数的通常是跳转/追踪链接
    if len(sp.query or "") > 120:
        return True
    return False


def judge(url: str, rec: dict | None, fetch_ok: bool) -> dict:
    """一次性给出分类标签 + 质量判定。"""
    host = host_of(url)
    dcls = domain_class(host)
    kind = content_kind(url, rec)
    title = (rec or {}).get("title") or ""
    body = (rec or {}).get("body") or ""
    topic = classify_topic(host, url, title, body) if (title or body) else "other"
    lang = detect_lang(title, body)

    offtopic = any(host.endswith(h) or h in url.lower() for h in OFFTOPIC_HOSTS)

    if not fetch_ok:
        quality = "unreachable"
    elif offtopic:
        quality = "offtopic"
    elif title and len(body) >= 500:
        quality = "good"
    elif title or len(body) >= 200:
        quality = "thin"
    else:
        quality = "empty"

    return {"domain_class": dcls, "topic": topic, "kind": kind,
            "quality": quality, "lang": lang}


# ---------------------------------------------------------------- 抓取 + 解析

def fetch_one(item: dict, timeout: int = 12):
    """抓一个站 → 解析 → 返回 (url, 是否成功, 解析结果 dict, 字节数, 耗时)"""
    import deep_crawl as dc
    import generic_extract as g

    url = item.get("url") or ""
    if not url.startswith("http"):
        url = "https://" + (item.get("domain") or "")
    t0 = time.time()
    try:
        b = dc.smart_get(url, timeout=timeout)
    except Exception:
        b = None
    dt = round(time.time() - t0, 2)
    if not b:
        return url, False, None, 0, dt
    html = b.decode("utf-8", "ignore") if isinstance(b, bytes) else b
    try:
        rec = g.parse_page(html, url) or {}
    except Exception:
        rec = {}
    return url, True, rec, len(b), dt


# ---------------------------------------------------------------- 状态

def load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            d = json.load(f)
            d.setdefault("tries", {})
            return d
    except Exception:
        return {"done": [], "tries": {}, "last_ts": 0, "runs": 0}


def save_state(st: dict):
    st["done"] = st["done"][-120000:]     # 防无限膨胀
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE)


def ukey(u: str) -> str:
    return hashlib.sha1(u.encode("utf-8", "ignore")).hexdigest()[:16]


# ---------------------------------------------------------------- 全量 URL 分类（不联网）

def ensure_index(c: sqlite3.Connection):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS site_index(
        url TEXT PRIMARY KEY, host TEXT, domain_class TEXT, topic TEXT,
        kind TEXT, quality TEXT, crawled INTEGER DEFAULT 0, ts REAL);
    CREATE INDEX IF NOT EXISTS idx_site_index_host  ON site_index(host);
    CREATE INDEX IF NOT EXISTS idx_site_index_topic ON site_index(topic);
    CREATE INDEX IF NOT EXISTS idx_site_index_crawl ON site_index(crawled, quality);
    """)


def index_all():
    """把 resources.json 全量按 URL 分类写进 site_index —— 不联网，几秒完成。

    这是「简单分类」的主路径：5 万条域名清单，靠 URL 结构 + 域名后缀就能
    分出性质、内容型、主题倾向，先拿到全景，再决定抓哪些。
    """
    with open(RESOURCES, encoding="utf-8") as f:
        data = json.load(f)
    c = sqlite3.connect(DB, timeout=60)
    c.execute("PRAGMA busy_timeout=90000")
    c.execute("PRAGMA journal_mode=WAL")
    ensure_index(c)

    skip_n = offtopic_n = 0
    rows = []
    for it in data:
        u = (it.get("url") or "").strip()
        if not u.startswith("http"):
            continue
        host = host_of(u)
        if not host:
            continue
        if should_skip(u):
            skip_n += 1
            continue
        dcls = domain_class(host)
        kind = content_kind(u, None)
        topic = classify_topic(host, u)
        offtopic = any(host.endswith(h) or h in u.lower() for h in OFFTOPIC_HOSTS)
        if offtopic:
            quality = "offtopic"
            offtopic_n += 1
        elif kind == "homepage":
            quality = "homepage"
        else:
            quality = "page"
        rows.append((u, host, dcls, topic, kind, quality, time.time()))

    c.executemany(
        "INSERT OR REPLACE INTO site_index"
        "(url,host,domain_class,topic,kind,quality,crawled,ts) VALUES(?,?,?,?,?,?,0,?)",
        rows)
    c.commit()

    from collections import Counter
    print("=== 全量 URL 分类完成 ===")
    print("  resources.json : %d 条" % len(data))
    print("  入库分类       : %d 条" % len(rows))
    print("  预过滤跳过     : %d 条（静态资源/登录页/接口端点）" % skip_n)
    print("  判为无关       : %d 条" % offtopic_n)
    print("\n  域名性质:", dict(Counter(r[2] for r in rows).most_common()))
    print("  主题    :", dict(Counter(r[3] for r in rows).most_common()))
    print("  内容型  :", dict(Counter(r[4] for r in rows).most_common()))
    print("  质量    :", dict(Counter(r[5] for r in rows).most_common()))
    print("\n=== 各主题下值得抓的具体页面数 ===")
    for t, n in c.execute(
            "select topic,count(*) from site_index "
            "where quality='page' group by 1 order by 2 desc"):
        print("  %-10s %6d" % (t, n))
    print("\n=== other 那一桶到底是什么（按域名性质拆）===")
    for d, n in c.execute(
            "select domain_class,count(*) from site_index "
            "where topic='other' group by 1 order by 2 desc"):
        print("  %-10s %6d" % (d, n))
    c.close()


# ---------------------------------------------------------------- 入库

def ensure_schema(c: sqlite3.Connection):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS site_meta(
        eid TEXT PRIMARY KEY, host TEXT, domain_class TEXT, topic TEXT,
        kind TEXT, quality TEXT, lang TEXT, ts REAL);
    CREATE INDEX IF NOT EXISTS idx_site_meta_topic ON site_meta(topic, quality);
    CREATE INDEX IF NOT EXISTS idx_site_meta_host  ON site_meta(host);
    """)
    # 老表补列（已经建过的不带 lang）
    cols = [r[1] for r in c.execute("pragma table_info(site_meta)")]
    if "lang" not in cols:
        try:
            c.execute("ALTER TABLE site_meta ADD COLUMN lang TEXT")
        except Exception:
            pass


def write_one(w, c, url: str, rec: dict | None, ok: bool, meta: dict) -> bool:
    """写实体 + 站点 + 分类表。返回是否新增。"""
    host = host_of(url)
    eid = "site:%s" % hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()[:16]
    title = ((rec or {}).get("title") or host or url)[:300]
    body = ((rec or {}).get("body") or "")[:2000]

    payload = {
        "domain_class": meta["domain_class"],
        "topic": meta["topic"],
        "kind": meta["kind"],
        "quality": meta["quality"],
        "lang": meta.get("lang") or "unknown",
    }
    for k in ("description", "author", "published", "publisher",
              "site", "source", "section"):
        v = (rec or {}).get(k)
        if v:
            payload[k] = str(v)[:200] if isinstance(v, str) else v
    if body:
        payload["body"] = body

    n = w.ent(eid, meta["kind"], title, url, depth=0, payload=payload, plat="site")

    host_id = "site:host:%s" % host
    w.ent(host_id, "domain", host, "https://" + host if host else "",
          depth=0, payload={"domain_class": meta["domain_class"]}, plat="site")
    w.edge(eid, host_id, "on_host")

    if payload.get("author"):
        a_id = "site:author:%s" % hashlib.sha1(
            payload["author"].encode("utf-8", "ignore")).hexdigest()[:12]
        w.ent(a_id, "author", payload["author"][:120], plat="site")
        w.edge(eid, a_id, "written_by")

    c.execute(
        "INSERT OR REPLACE INTO site_meta"
        "(eid,host,domain_class,topic,kind,quality,lang,ts) VALUES(?,?,?,?,?,?,?,?)",
        (eid, host, meta["domain_class"], meta["topic"], meta["kind"],
         meta["quality"], meta.get("lang") or "unknown", time.time()))
    return bool(n)


# ---------------------------------------------------------------- 主流程

def pick_todo(limit: int, st: dict, only_kind: str | None = None):
    with open(RESOURCES, encoding="utf-8") as f:
        data = json.load(f)
    done = set(st.get("done") or [])
    tries = st.get("tries") or {}
    todo, seen, hosts = [], set(), set()
    skipped = given_up = 0
    # 倒序：优先最新的发现
    for it in reversed(data):
        u = (it.get("url") or "").strip()
        if not u.startswith("http"):
            continue
        if only_kind and it.get("type") != only_kind:
            continue
        k = ukey(u)
        if k in done or k in seen:
            continue
        # 抓过两次仍不可达的放弃，不再占名额
        if tries.get(k, 0) >= 2:
            done.add(k)
            given_up += 1
            continue
        # 静态资源 / 登录页 / 接口端点：抓了没意义，直接记为已跳过
        if should_skip(u):
            done.add(k)
            skipped += 1
            continue
        # 同主机只取一个页面：一次发现常常带出同一站的几十个子域，重复抓是浪费
        h = host_of(u)
        if h in hosts:
            continue
        hosts.add(h)
        seen.add(k)
        todo.append((it, k))
        if len(todo) >= limit * 3:
            break
    st["done"] = list(done)
    if skipped:
        print("  预过滤跳过静态资源/登录页/接口端点 %d 个" % skipped, flush=True)
    if given_up:
        print("  重试两次仍不可达、本轮放弃 %d 个" % given_up, flush=True)
    # 排序：带路径的优先（更可能是具体页面），首页靠后
    todo.sort(key=lambda x: 0 if urlsplit(x[0]["url"]).path.strip("/") else 1)
    return todo[:limit], len(data)


def pick_from_index(limit: int, st: dict):
    """从 site_index 里按价值排序取 URL —— 只抓有内容的页，不抓企业官网首页。"""
    c = sqlite3.connect(DB, timeout=60)
    c.execute("PRAGMA busy_timeout=90000")
    ensure_index(c)
    done = set(st.get("done") or [])
    tries = st.get("tries") or {}

    # 优先级：技术/学术 > 文档/博客 > 新闻/社区 > 其他具体页 > 首页
    q = """
    select url, host, topic, kind, quality from site_index
    where crawled=0 and quality in ('page','homepage')
    order by
      case
        when topic in ('tech','academic') and kind in ('blog','docs','page') then 0
        when kind in ('blog','docs') then 1
        when topic in ('news','social','culture') then 2
        when quality='page' then 3
        else 4
      end,
      length(url)
    limit ?
    """
    rows = []
    for url, host, topic, kind, quality in c.execute(q, (limit * 4,)):
        k = ukey(url)
        if k in done:
            continue
        if tries.get(k, 0) >= 2:
            continue
        rows.append({"url": url, "domain": host, "type": "site",
                     "topic": topic, "kind": kind})
        if len(rows) >= limit:
            break
    c.close()
    todo = [(it, ukey(it["url"])) for it in rows]
    return todo, len(rows)


def mark_crawled(urls):
    c = sqlite3.connect(DB, timeout=60)
    c.execute("PRAGMA busy_timeout=90000")
    ensure_index(c)
    c.executemany("update site_index set crawled=1 where url=?",
                  [(u,) for u in urls])
    c.commit()
    c.close()


def run_index(limit: int, workers: int, timeout: int):
    """定向抓取：从已分类的 site_index 里挑高价值页面。"""
    st = load_state()
    todo, n = pick_from_index(limit, st)
    print("→ 从分类索引取到 %d 个高价值页面待抓" % n, flush=True)
    if not todo:
        print("没有待抓项。先跑 --index 生成分类。")
        return

    t0 = time.time()
    results = []
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(fetch_one, it, timeout): (it, k) for it, k in todo}
        for fu in cf.as_completed(futs):
            it, k = futs[fu]
            try:
                url, ok, rec, size, dt = fu.result()
            except Exception:
                url, ok, rec, size, dt = it.get("url") or "", False, None, 0, 0
            results.append((url, ok, rec, size, dt, judge(url, rec, ok), k))

    from twitter_crawl import connect, Writer
    c = connect()
    ensure_schema(c)
    ensure_index(c)
    w = Writer(c)
    added = done_urls = 0
    from collections import Counter
    qc, tc = Counter(), Counter()
    tries = st.setdefault("tries", {})
    for url, ok, rec, size, dt, meta, k in results:
        try:
            if write_one(w, c, url, rec, ok, meta):
                added += 1
            qc[meta["quality"]] += 1
            tc[meta["topic"]] += 1
            if meta["quality"] == "unreachable":
                tries[k] = int(tries.get(k, 0)) + 1
                if tries[k] >= 2:
                    st.setdefault("done", []).append(k)
                    c.execute("update site_index set crawled=1 where url=?", (url,))
            else:
                st.setdefault("done", []).append(k)
                tries.pop(k, None)
                c.execute("update site_index set crawled=1 where url=?", (url,))
            done_urls += 1
        except Exception as e:
            print("  写入失败 %s: %s" % (url[:50], str(e)[:60]), flush=True)
    w.flush()
    c.close()

    st["last_ts"] = time.time()
    st["runs"] = int(st.get("runs") or 0) + 1
    save_state(st)

    print("\n=== 定向抓取完成 ===")
    print("  处理 %d 个，新增实体 %d，边 %d，耗时 %ss"
          % (len(results), added, w.new_edge, round(time.time() - t0, 1)))
    print("  质量 :", dict(qc.most_common()))
    print("  主题 :", dict(tc.most_common()))


def run(limit: int, workers: int, dry: bool, only_kind: str | None, timeout: int):
    """主路径：直接从 resources.json 顺序消费（不依赖分类索引）。"""
    st = load_state()
    todo, total = pick_todo(limit, st, only_kind)
    print("→ resources.json 共 %d 条；待处理本轮 %d 个（已处理过 %d 个）"
          % (total, len(todo), len(st.get("done") or [])), flush=True)
    if not todo:
        print("没有待处理项。主爬虫是否还在追加？")
        return

    t0 = time.time()
    results = []
    try:
        with cf.ThreadPoolExecutor(workers) as ex:
            futs = {ex.submit(fetch_one, it, timeout): (it, k) for it, k in todo}
            for fu in cf.as_completed(futs):
                it, k = futs[fu]
                try:
                    url, ok, rec, size, dt = fu.result()
                except Exception:
                    url, ok, rec, size, dt = (it.get("url") or ""), False, None, 0, 0
                meta = judge(url, rec, ok)
                results.append((url, ok, rec, size, dt, meta, k))
    except KeyboardInterrupt:
        print("\n中断，已完成的会先落盘", flush=True)

    if dry:
        from collections import Counter
        print("\n=== 干跑分类结果（不入库）===")
        for url, ok, rec, size, dt, meta, k in results[:40]:
            print("  %-11s %-9s %-9s %-9s %-3s %s"
                  % (meta["quality"], meta["domain_class"], meta["topic"],
                     meta["kind"], meta.get("lang", "?"), url[:52]))
        print("\n  quality:", dict(Counter(r[5]["quality"] for r in results)))
        print("  topic  :", dict(Counter(r[5]["topic"] for r in results)))
        print("  dclass :", dict(Counter(r[5]["domain_class"] for r in results)))
        print("  lang   :", dict(Counter(r[5].get("lang") for r in results)))
        return

    from twitter_crawl import connect, Writer
    c = connect()
    ensure_schema(c)
    w = Writer(c)
    added = 0
    from collections import Counter
    qc, tc = Counter(), Counter()
    tries = st.setdefault("tries", {})
    retry_later = 0
    for url, ok, rec, size, dt, meta, k in results:
        try:
            if write_one(w, c, url, rec, ok, meta):
                added += 1
            qc[meta["quality"]] += 1
            tc[meta["topic"]] += 1
            # 不可达不算「处理过」：网络抖动很常见，下轮再试，两次后才放弃
            if meta["quality"] == "unreachable":
                tries[k] = int(tries.get(k, 0)) + 1
                if tries[k] >= 2:
                    st.setdefault("done", []).append(k)
                else:
                    retry_later += 1
            else:
                st.setdefault("done", []).append(k)
                tries.pop(k, None)
        except Exception as e:
            print("  写入失败 %s: %s" % (url[:50], str(e)[:60]), flush=True)
    w.flush()
    c.close()

    st["last_ts"] = time.time()
    st["runs"] = int(st.get("runs") or 0) + 1
    save_state(st)

    el = round(time.time() - t0, 1)
    print("\n=== 本轮完成 ===")
    print("  处理 %d 个，新增实体 %d，实体+边 %d/%d，耗时 %ss"
          % (len(results), added, w.new_ent, w.new_edge, el))
    if retry_later:
        print("  其中 %d 个本轮抓取失败，已留下轮重试" % retry_later)
    print("  质量 :", dict(qc.most_common()))
    print("  主题 :", dict(tc.most_common()))
    print("  累计处理 %d 个" % len(st.get("done") or []))


def stats():
    if not os.path.exists(DB):
        print("库不存在:", DB)
        return
    c = sqlite3.connect(DB)
    try:
        ensure_schema(c)
        ensure_index(c)
    except Exception:
        pass
    print("=== 全库 ===")
    print("  实体:", c.execute("select count(*) from entities").fetchone()[0])
    print("  边  :", c.execute("select count(*) from edges").fetchone()[0])

    try:
        tot_idx = c.execute("select count(*) from site_index").fetchone()[0]
        crawled = c.execute("select count(*) from site_index where crawled=1").fetchone()[0]
        print("\n=== 域名分类索引（site_index）===")
        print("  已分类 URL : %d" % tot_idx)
        print("  已抓取     : %d  (%.1f%%)" % (crawled, crawled * 100 / max(tot_idx, 1)))
        print("  待抓取     : %d" % (tot_idx - crawled))
        print("\n  按主题（待抓）:")
        for t, n in c.execute(
                "select topic,count(*) from site_index where crawled=0 "
                "group by 1 order by 2 desc limit 12"):
            print("    %-10s %6d" % (t, n))
    except Exception as e:
        print("  site_index 未就绪:", e)

    print("\n=== site 平台质量分布（已抓）===")
    for q, n in c.execute("select quality,count(*) from site_meta group by 1 order by 2 desc"):
        print("  %-12s %6d" % (q, n))
    print("\n=== site 平台主题分布（已抓）===")
    for t, n in c.execute("select topic,count(*) from site_meta group by 1 order by 2 desc"):
        print("  %-12s %6d" % (t, n))
    print("\n=== site 平台域名性质 ===")
    for d, n in c.execute("select domain_class,count(*) from site_meta group by 1 order by 2 desc"):
        print("  %-12s %6d" % (d, n))
    print("\n=== site 平台语种 ===")
    for d, n in c.execute("select lang,count(*) from site_meta group by 1 order by 2 desc"):
        print("  %-12s %6d" % (d or "unknown", n))
    print("\n=== site 平台内容型 ===")
    for d, n in c.execute("select kind,count(*) from site_meta group by 1 order by 2 desc"):
        print("  %-12s %6d" % (d, n))
    print("\n=== 各平台实体数 ===")
    tot = c.execute("select count(*) from entities").fetchone()[0]
    for p, n in c.execute("select platform,count(*) from entities group by 1 order by 2 desc"):
        print("  %-14s %7d  %4.1f%%" % (p, n, n * 100 / max(tot, 1)))
    c.close()


def reclassify():
    """给库里已有的旧实体补分类（按 url 推断，不重抓）。"""
    c = sqlite3.connect(DB, timeout=60)
    c.execute("PRAGMA busy_timeout=90000")
    ensure_schema(c)
    rows = list(c.execute(
        "select id,url,title,platform from entities "
        "where id not in (select eid from site_meta)"))
    print("待补分类实体:", len(rows))
    n = 0
    for eid, url, title, plat in rows:
        if not url:
            continue
        meta = judge(url, {"title": title or ""}, True)
        c.execute(
            "INSERT OR REPLACE INTO site_meta"
            "(eid,host,domain_class,topic,kind,quality,lang,ts) VALUES(?,?,?,?,?,?,?,?)",
            (eid, host_of(url), meta["domain_class"], meta["topic"],
             meta["kind"], meta["quality"], meta["lang"], time.time()))
        n += 1
        if n % 5000 == 0:
            c.commit()
            print("  ...%d" % n, flush=True)
    c.commit()
    c.close()
    print("补分类完成:", n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=12)
    ap.add_argument("--dry", action="store_true", help="只分类不入库")
    ap.add_argument("--only-kind", default=None, help="只处理 type=site/community/model 等")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--index", action="store_true",
                    help="全量 URL 分类（不联网，几秒完成）")
    ap.add_argument("--from-index", action="store_true",
                    help="按分类优先级定向抓取高价值页面")
    ap.add_argument("--reclassify", action="store_true", help="给旧实体补分类")
    a = ap.parse_args()

    if a.stats:
        stats(); return 0
    if a.index:
        index_all(); return 0
    if a.from_index:
        run_index(a.limit, a.workers, a.timeout); return 0
    if a.reclassify:
        reclassify(); return 0
    run(a.limit, a.workers, a.dry, a.only_kind, a.timeout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
