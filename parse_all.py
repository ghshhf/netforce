# -*- coding: utf-8 -*-
"""netforce / parse_all.py —— 全量语料解析与分类引擎（统一库 corpus.db）

目标：把爬虫攒在盘上的**所有**数据一次性吃干净，以后只跑增量。
    parse_assets.py 只处理了 _kb/assets 里 526 个下载文件；
    真正的主体是 records/*.jsonl 里 3.7 万条抓取记录（2.3 万条带 1.5 亿字符正文），
    加上 kb_index / merged / seeds / phone_kb / reclaim / state / assets / 抖音 API /
    discovered / sitemap / crt 等十余个来源。此前这些要么只存了截断 snippet，
    要么完全没进过索引，主题标签大面积为空。

设计：
  * 统一表 corpus，一条记录一行；uid = sha1(source|key)，跨源去重靠 host+url 归档。
  * 结构化元数据（headings / keywords / og / jsonld / links / assets / 统计）全部拆出存 meta。
  * 标签 = fine_classify.tag() 基础层 + tags_ext.json 扩展层，merge_tags 收敛 Top-K。
  * 无正文的 URL 级记录按 host 画像继承标签；仍无命中则走兜底类目，保证零空标签。
  * 断点续跑：sources 表记每个源的处理行数；--force 可全量重来；--retag 只重打标签。
  * 未来加标签：改 tags_ext.json → --retag → --backfill，不重读原始文件。

用法：
    python parse_all.py --all              # 全量（自动跳过已完成的源）
    python parse_all.py --stage records   # 只跑某个源
    python parse_all.py --retag           # 全库重打标签
    python parse_all.py --backfill        # 回填 kb_index.db 的空标题/正文/标签
    python parse_all.py --verify          # 零遗留验收
    python parse_all.py --stats          # 统计
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(HERE, "_kb")
sys.path.insert(0, HERE)

from fine_classify import tag as base_tag                      # noqa: E402
from parse_assets import (load_taxonomy, extra_tags, merge_tags,  # noqa: E402
                          _lang, STORE_TEXT)

DB = os.path.join(KB, "corpus.db")
LEGACY_DB = os.path.join(KB, "kb_index.db")
ASSET_DB = os.path.join(KB, "asset_index.db")
PARSE_VER = 3
STORE = 30000            # 单条入库正文上限（1.5 亿字符全存会让 FTS 索引失控）
CJK = re.compile(r"[\u4e00-\u9fff]")

COLS = ["uid", "source", "kind", "url", "host", "title", "descr", "text",
        "text_len", "lang", "category", "forms", "topics", "subtopics",
        "extra_topics", "quality", "value", "parse_status", "evidence",
        "meta", "ts", "parse_ver"]


# ------------------------------------------------------------------ 基础设施
def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def uid_of(source, key):
    return hashlib.sha1((source + "|" + str(key)).encode("utf-8", "ignore")).hexdigest()


def jdump(x, cap=None):
    try:
        s = json.dumps(x, ensure_ascii=False, default=str)
    except Exception:
        s = "[]"
    return s[:cap] if cap else s


def jload(s, default=None):
    if isinstance(s, (list, dict)):
        return s
    try:
        return json.loads(s or "")
    except Exception:
        return default if default is not None else []


def host_of(url, fallback=""):
    m = re.match(r"^[a-z]+://([^/:?#]+)", str(url or ""), re.I)
    if m:
        return m.group(1).lower()
    s = str(url or "").split("/")[0].lower()
    return s[:120] or fallback


def clean_text(s):
    if not isinstance(s, str):
        return ""
    s = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def connect():
    os.makedirs(KB, exist_ok=True)
    c = sqlite3.connect(DB, timeout=120)
    c.execute("PRAGMA busy_timeout=120000")   # 常驻循环会并发写，默认 5s 不够
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA cache_size=-65536")
    c.execute("""CREATE TABLE IF NOT EXISTS corpus(
        uid TEXT PRIMARY KEY, source TEXT, kind TEXT, url TEXT, host TEXT,
        title TEXT, descr TEXT, text TEXT, text_len INTEGER, lang TEXT,
        category TEXT, forms TEXT, topics TEXT, subtopics TEXT, extra_topics TEXT,
        quality INTEGER, value TEXT, parse_status TEXT, evidence TEXT,
        meta TEXT, ts TEXT, parse_ver INTEGER)""")
    for idx in ("ix_corpus_source", "ix_corpus_host", "ix_corpus_status",
                "ix_corpus_cat", "ix_corpus_kind"):
        col = {"ix_corpus_source": "source", "ix_corpus_host": "host",
               "ix_corpus_status": "parse_status", "ix_corpus_cat": "category",
               "ix_corpus_kind": "kind"}[idx]
        c.execute("CREATE INDEX IF NOT EXISTS %s ON corpus(%s)" % (idx, col))
    try:
        c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS corpus_fts USING fts5(
            uid UNINDEXED, host, title, topics, text, tokenize='trigram')""")
        c.execute("SELECT 1 FROM corpus_fts LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS corpus_fts USING fts5(
            uid UNINDEXED, host, title, topics, text)""")
    c.execute("""CREATE TABLE IF NOT EXISTS sources(
        name TEXT PRIMARY KEY, rows INTEGER, done_at TEXT, secs REAL)""")
    c.commit()
    return c


def write_rows(c, rows, fts=True):
    if not rows:
        return
    ph = ",".join("?" for _ in COLS)
    sql = "INSERT OR REPLACE INTO corpus(%s) VALUES(%s)" % (",".join(COLS), ph)
    # 只有真正带正文的记录才进全文索引；几十万条纯 URL 元数据连 DELETE 都不该做
    # （FTS5 上逐条 DELETE 即使无匹配也要走索引，45 万条能拖慢一个数量级）
    long = [r for r in rows if (r.get("text_len") or 0) > 200] if fts else []
    for attempt in range(6):
        try:
            c.executemany(sql, [[r.get(k, "") for k in COLS] for r in rows])
            if long:
                c.executemany("DELETE FROM corpus_fts WHERE uid=?",
                             [(r["uid"],) for r in long])
                # FTS 只索引前 12,000 字符：正文全文仍完整存在 corpus.text，
                # 但 trigram 索引对超长文本是 CPU 黑洞（7,500 字符/条 × 数万条）。
                # 检索召回靠开头已足够，要读全文时按 uid 回主表取。
                c.executemany("INSERT INTO corpus_fts(uid,host,title,topics,text) "
                              "VALUES(?,?,?,?,?)",
                              [(r["uid"], r["host"], r["title"], r["topics"],
                                (r["text"] or "")[:12000]) for r in long])
            c.commit()
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == 5:
                raise
            try:
                c.rollback()
            except Exception:
                pass
            time.sleep(2 * (attempt + 1))
            log("  [write_rows] 数据库忙，重试 %d/6" % (attempt + 1))


# ------------------------------------------------------------------ 分类层
DOMAIN_RULES = [
    ("代码/仓库", r"github\.com|gitlab|gitee|bitbucket|sourceforge|npmjs|pypi\.org|crates\.io|maven|huggingface|stackoverflow"),
    ("AI/机器学习", r"arxiv\.org|huggingface|paperswithcode|openai|anthropic|machinelearning|kaggle|pytorch|tensorflow"),
    ("文档/手册", r"readthedocs|\.rtfd|docs\.|/docs/|developer\.mozilla|w3\.org|learn\.microsoft"),
    ("百科/知识库", r"wikipedia|baike\.baidu|wikia|fandom|britannica|zhihu\.com|baike"),
    ("学术/科研", r"\.edu|\.ac\.|researchgate|springer|sciencedirect|jstor|nature\.com|doi\.org|cnki|wanfang"),
    ("新闻/媒体", r"news|bbc|reuters|thepaper|cctv|xinhua|sina\.com|163\.com|qq\.com|sohu|caixin"),
    ("社交/社区", r"douyin|bilibili|weibo|xiaohongshu|reddit|twitter|x\.com|tieba|douban|discord"),
    ("政府/公共", r"\.gov|\.gov\.cn|\.mil|gov\.|europa\.eu|who\.int|un\.org"),
    ("电商/商业", r"taobao|jd\.com|amazon|ebay|alibaba|tmall|shopify|pinduoduo"),
    ("云/基础设施", r"cloudflare|aws\.amazon|azure|aliyun|tencentcloud|digitalocean|vercel|netlify"),
    ("安全/渗透", r"owasp|nvd\.nist|cve|exploit-db|hackerone|portswigger|sans\.org|offensive-security"),
    ("设计/创意", r"behance|dribbble|figma|design|artstation|deviantart|awwwards"),
    ("图书馆/档案", r"archive\.org|library|lib\.|archive|loc\.gov|gutenberg|jstor"),
    ("音视频/播客", r"youtube|bilibili|podcast|spotify|soundcloud|netflix|vimeo|douyin"),
    ("地理/地图", r"openstreetmap|maps\.|geonames|gis\b|mapbox"),
    ("生物/医学", r"pubmed|ncbi|nih\.gov|medical|health|who\.int|clinicaltrials"),
    ("金融/经济", r"bloomberg|reuters\.com/finance|investing|stock|finance|证券|基金"),
]
DOMAIN_RULES = [(n, re.compile(p, re.I)) for n, p in DOMAIN_RULES]

TLD_RULES = {
    ".edu": "学术/科研", ".ac.uk": "学术/科研", ".gov": "政府/公共",
    ".mil": "政府/公共", ".org": "组织/非营利", ".int": "国际组织",
    ".io": "技术/工具", ".dev": "技术/工具", ".app": "技术/工具",
    ".cn": "中文站点", ".jp": "日文站点", ".de": "德文站点", ".fr": "法文站点",
    ".ru": "俄文站点", ".uk": "英文站点", ".au": "英文站点",
}


SITE_LANG_TAGS = {"中文站点", "日文站点", "德文站点", "法文站点",
                  "俄文站点", "英文站点"}


def domain_guess(host, url):
    """域名直判：只给站点类型，不给内容主题。

    TLD_RULES 里 .cn/.de/.jp 那批是**站点语言属性**，不是内容主题；
    把它们塞进 topics 会让主题分布头部全是「中文站点/德文站点」，
    且与 classify 快速路径「没有正文就不猜内容」的口径冲突。
    """
    h = (host or "").lower()
    u = (url or "").lower()
    hits = []
    for name, rx in DOMAIN_RULES:
        if rx.search(h) or rx.search(u[:200]):
            hits.append(name)
            if len(hits) >= 2:
                break
    for suf, name in TLD_RULES.items():
        if h.endswith(suf) or ("/." + suf.lstrip(".")) in u[:40]:
            if name not in hits and name not in SITE_LANG_TAGS:
                hits.append(name)
            break
    return hits[:3]


def fallback_tags(rec, inherited=None):
    """保证每条记录都有非空 topics / category / forms。

    优先级：host 画像 > 基础分类器已判出的 category > 域名规则 > 语义复核兜底。
    之前直接用域名规则兜底，把 fine_classify 判出的「网络安全/信息安全」
    「目录/索引站」等真实类目丢了，导致近半数记录落到「未命中」。
    """
    host, url = rec["host"], rec["url"]
    cat = rec.get("category") or ""
    tp = list(inherited or [])
    if not tp and cat and cat not in ("其他/待定", "访问失败（保留待复查）", "未识别"):
        tp = [cat]
    if not tp:
        tp = domain_guess(host, url)
    if not tp:
        tp = ["未命中/待语义复核"]
    if not cat or cat in ("其他/待定", "访问失败（保留待复查）"):
        cat = tp[0]
    if rec.get("parse_status") in ("pending_fetch", "unreachable"):
        cat = cat if "访问" in cat else "待抓取/不可达"
    forms = jload(rec.get("forms"), [])
    if not forms:
        if rec.get("kind") == "social_post":
            forms = ["社交动态"]
        elif rec.get("text_len", 0) > 0:
            forms = ["网页/正文"]
        else:
            forms = ["URL/元数据"]
    return cat, jdump(tp), jdump(forms[:3])


def classify(rec, tax, host_profile=None):
    """对一条规范化记录打标签，返回可直接入库的字段。

    快速路径：无正文的纯 URL 记录若命中 host 画像，直接继承标签，
    不跑全文规则引擎（26 万条元数据记录因此从 ~20 分钟降到秒级）。
    """
    text = rec.get("text") or ""
    title = rec.get("title") or ""
    host, url = rec["host"], rec["url"]
    tlen = rec.get("text_len") or len(text)
    inherit = host_profile.get(host) if host_profile else None
    substantive = bool(text) or (title and title != host)

    if not substantive and not text:
        # 纯 URL / 域名级记录：不跑全文规则引擎，用 host 画像 + 域名规则直判。
        # 45 万条已访问 URL 因此从分钟级降到秒级，分类口径也更诚实（没有正文就不猜内容）。
        guess = list(inherit or [])[:3] or domain_guess(host, url) or ["未命中/待语义复核"]
        return {
            "category": guess[0], "forms": jdump(["URL/元数据"]),
            "topics": jdump(guess[:3]), "subtopics": "[]",
            "extra_topics": "[]", "quality": 0, "value": "低",
            "evidence": jdump({"_inherit": host} if inherit else {"_domain_guess": True}),
            "lang": rec.get("lang") or "无正文",
        }

    base = {}
    md = rec.get("meta")
    md = jload(md, {}) if isinstance(md, str) else (md or {})
    if not isinstance(md, dict):
        md = {}
    if substantive:
        probe = {
            "domain": host, "url": url, "ok": rec.get("ok", True),
            "status": rec.get("status", 200), "title": title,
            # 基础规则引擎与打分用的 hay 一致，都只看前 12,000 字符。
            # 之前传全文：7,500 字符/条要跑全量正则，长文阶段直接慢 40 倍；
            # 而标签判定本来就不读 12,000 字符之后的内容，多喂纯属浪费。
            "desc": rec.get("descr", ""), "snippet": text[:500], "text": text[:12000],
            "text_len": tlen, "headings": md.get("heading_text", []),
            "keywords": [], "links": [], "assets": [], "new_domains": [],
            "xhr_count": 0, "structured": False, "rendered": False,
            "lang": rec.get("lang", ""), "exts": [],
        }
        try:
            base = base_tag(probe)
        except Exception:
            base = {}
    hay = " ".join([title, host, url, text[:12000]]).lower()
    try:
        et, ef, ev = extra_tags(tax, hay, host, os.path.basename(url))
    except Exception:
        et, ef, ev = [], [], {}
    topics, subs, forms, ev_all = merge_tags(base, et, ef, ev, title, tlen)

    if not topics and inherit:
        topics = list(inherit)[:3]
        ev_all["_inherit"] = host
    q = int(base.get("quality") or 0)
    if tlen > 2000:
        q = min(100, q + 10)
    return {
        "category": base.get("category") or "",
        "forms": jdump(forms),
        "topics": jdump(topics),
        "subtopics": jdump(subs),
        "extra_topics": jdump(et),
        "quality": q,
        "value": "高" if q >= 55 else ("中" if q >= 28 else "低"),
        "evidence": jdump(ev_all, 4000),
        "lang": base.get("lang") or rec.get("lang") or _lang(text),
    }


def make_row(source, kind, key, url, host, title, descr, text, meta,
             status, tax, host_profile=None, extra=None):
    text = clean_text(text or "")[:STORE]
    uid = uid_of(source, key)
    rec = {
        "uid": uid, "source": source, "kind": kind, "url": str(url or "")[:500],
        "host": (host or host_of(url))[:160], "title": str(title or "")[:300],
        "descr": str(descr or "")[:600], "text": text, "text_len": len(text),
        "lang": _lang(text), "category": "", "forms": "[]", "topics": "[]",
        "subtopics": "[]", "extra_topics": "[]", "quality": 0, "value": "低",
        "parse_status": status, "evidence": "{}", "meta": jdump(meta or {}, 6000),
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "parse_ver": PARSE_VER,
    }
    rec.update(classify(rec, tax, host_profile))
    cat, tp, fm = fallback_tags(rec, host_profile.get(rec["host"]) if host_profile else None)
    rec["category"], rec["topics"], rec["forms"] = cat, tp, fm
    if extra:
        rec.update(extra)
    return rec


# ------------------------------------------------------------------ 源：抓取记录
def structured_meta(d):
    """把记录里所有结构化字段拆出来，不丢信息。"""
    m = {}
    for k in ("headings", "keywords", "og", "jsonld", "structured", "links",
              "new_domains", "site_urls", "assets", "xhr", "tags", "response_count",
              "xhr_count", "rendered", "degraded", "tier", "channel", "scheme",
              "final_url", "status", "text_density", "main_tag", "region",
              "region_basis", "egress", "last_modified", "ts"):
        v = d.get(k)
        if v in (None, "", [], {}, False, 0):
            continue
        m[k] = v
    # 结构化数据里的名称/描述/面包屑是最有信息量的部分
    jl = d.get("jsonld")
    if jl:
        try:
            items = jl if isinstance(jl, list) else [jl]
            names, types = [], []
            for o in items[:20]:
                if isinstance(o, dict):
                    if o.get("name"):
                        names.append(str(o["name"])[:120])
                    if o.get("@type"):
                        types.append(str(o["@type"])[:60])
            if names:
                m["jsonld_names"] = names[:12]
            if types:
                m["jsonld_types"] = list(dict.fromkeys(types))[:8]
        except Exception:
            pass
    hl = d.get("headings")
    if isinstance(hl, list):
        m["heading_text"] = [str(x)[:120] for x in hl[:20] if x]
    elif isinstance(hl, dict):
        m["heading_text"] = [str(x)[:120] for v in hl.values() for x in (v if isinstance(v, list) else [v])][:20]
    return m


def src_records(c, tax, force=False):
    files = [p for p in sorted(glob.glob(os.path.join(KB, "records", "rec-*.jsonl")))
             if not p.endswith(".bak") and ".bak" not in p]
    done = set()
    if not force:
        done = {r[0] for r in c.execute("SELECT name FROM sources WHERE name LIKE 'records/%'")}
    total = 0
    for p in files:
        name = "records/" + os.path.basename(p)
        if name in done:
            log("  skip %s (已完成)" % name)
            continue
        t0 = time.time()
        batch, n, withtext = [], 0, 0
        with open(p, encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                url = d.get("url") or d.get("final_url") or ""
                host = d.get("domain") or host_of(url)
                if not isinstance(host, str):
                    host = str(host)
                host = host.split("/")[0].lower()
                title = d.get("title") or ""
                descr = d.get("desc") or ""
                text = d.get("text") or ""
                tlen = len(text)
                if tlen > 200:
                    withtext += 1
                meta = structured_meta(d)
                if not title and meta.get("jsonld_names"):
                    title = meta["jsonld_names"][0]
                if not title and meta.get("heading_text"):
                    title = meta["heading_text"][0]
                if not title:
                    title = host
                status = "ok" if tlen > 200 else ("no_text" if d.get("ok") else "unreachable")
                r = make_row(name, "page", "%s:%d" % (os.path.basename(p), lineno),
                             url, host, title, descr, text, meta, status, tax)
                r["lang"] = d.get("lang") or r["lang"]
                batch.append(r)
                n += 1
                if len(batch) >= 200:
                    write_rows(c, batch)
                    batch = []
                if n % 5000 == 0:
                    log("  %s %d 行..." % (name, n))
        if batch:
            write_rows(c, batch)
        c.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?,?)",
                  (name, n, datetime.now().isoformat(timespec="seconds"), time.time() - t0))
        c.commit()
        total += n
        log("  %s: %d 行，带正文 %d，%.1fs" % (name, n, withtext, time.time() - t0))
    return total


# ------------------------------------------------------------------ 源：主索引
def src_kb_index(c, tax, force=False):
    if not force and c.execute("SELECT 1 FROM sources WHERE name='kb_index'").fetchone():
        log("  skip kb_index (已完成)")
        return 0
    lc = sqlite3.connect("file:%s?mode=ro" % LEGACY_DB.replace("\\", "/"), uri=True)
    t0, n, batch = time.time(), 0, []
    for (dom, url, title, descr, snippet, cat, forms, topics, exts,
         access, lang, quality, value, ok, channel, ts, src_file, src_line) in lc.execute(
            "SELECT domain,url,title,descr,snippet,category,forms,topics,exts,"
            "access,lang,quality,value,ok,channel,ts,src_file,src_line FROM docs"):
        text = snippet or descr or ""
        meta = {"access": access, "exts": jload(exts, []), "channel": channel,
                "src_file": src_file, "src_line": src_line,
                "quality_orig": quality, "value_orig": value,
                "category_orig": cat, "topics_orig": jload(topics, []),
                "forms_orig": jload(forms, []), "ts": ts}
        r = make_row("kb_index", "page", url or dom, url, dom,
                     title or dom, descr, text, meta,
                     "ok" if ok else "unreachable", tax)
        r["lang"] = lang or r["lang"]
        batch.append(r)
        n += 1
        if len(batch) >= 200:
            write_rows(c, batch)
            batch = []
    lc.close()
    if batch:
        write_rows(c, batch)
    c.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?,?)",
              ("kb_index", n, datetime.now().isoformat(timespec="seconds"), time.time() - t0))
    c.commit()
    log("  kb_index: %d 条，%.1fs" % (n, time.time() - t0))
    return n


# ------------------------------------------------------------------ 源：已解析文件
def src_asset_docs(c, tax, force=False):
    if not force and c.execute("SELECT 1 FROM sources WHERE name='asset_docs'").fetchone():
        log("  skip asset_docs (已完成)")
        return 0
    ac = sqlite3.connect("file:%s?mode=ro" % ASSET_DB.replace("\\", "/"), uri=True)
    t0, n, batch = time.time(), 0, []
    for (p, host, url, ext, size, sha, title, text, tlen, pages, lang,
         cat, forms, topics, subs, extra, ev, q, value, status, err,
         ms, scanned, ver, ts) in ac.execute(
            "SELECT path,host,url,ext,size,sha256,title,text,text_len,pages,lang,"
            "category,forms,topics,subtopics,extra_topics,evidence,quality,value,"
            "parse_status,parse_err,parse_ms,scanned,parse_ver,ts FROM asset_docs"):
        meta = {"path": p, "ext": ext, "size": size, "sha256": sha,
                "pages": pages, "parse_err": err, "scanned": scanned,
                "forms_orig": jload(forms, []), "topics_orig": jload(topics, []),
                "evidence_orig": ev, "quality_orig": q, "value_orig": value}
        r = make_row("asset_docs", "file", p, url, host, title, "", text, meta,
                     status, tax)
        r["lang"] = lang or r["lang"]
        batch.append(r)
        n += 1
        if len(batch) >= 100:
            write_rows(c, batch)
            batch = []
    ac.close()
    if batch:
        write_rows(c, batch)
    c.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?,?)",
              ("asset_docs", n, datetime.now().isoformat(timespec="seconds"), time.time() - t0))
    c.commit()
    log("  asset_docs: %d 条，%.1fs" % (n, time.time() - t0))
    return n


# ------------------------------------------------------------------ host 画像
def build_host_profile(c):
    """用已有正文记录的标签给每个 host 建画像，供纯 URL 记录继承。"""
    prof = defaultdict(Counter)
    for host, tp, et in c.execute(
            "SELECT host,topics,extra_topics FROM corpus WHERE text_len>200"):
        for t in jload(tp, []) + jload(et, []):
            if t and t != "未命中/待语义复核":
                prof[host][t] += 1
    return {h: [t for t, _ in cnt.most_common(4)] for h, cnt in prof.items()}


# ------------------------------------------------------------------ 源：各种 URL 级记录
def _url_rows(c, tax, prof, name, kind, items):
    """items: (key, url, host, title, descr, text, meta, status)"""
    t0, n, batch = time.time(), 0, []
    for key, url, host, title, descr, text, meta, status in items:
        r = make_row(name, kind, key, url, host, title, descr, text, meta,
                     status, tax, prof)
        batch.append(r)
        n += 1
        if len(batch) >= 400:
            write_rows(c, batch)
            batch = []
    if batch:
        write_rows(c, batch)
    c.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?,?)",
              (name, n, datetime.now().isoformat(timespec="seconds"), time.time() - t0))
    c.commit()
    log("  %s: %d 条，%.1fs" % (name, n, time.time() - t0))
    return n


def src_entities(c, tax, prof, force=False):
    total = 0
    for db in ("merged.db", "seeds.db", "phone_kb.db"):
        name = "entities/" + db
        if not force and c.execute("SELECT 1 FROM sources WHERE name=?", (name,)).fetchone():
            log("  skip %s (已完成)" % name)
            continue
        p = os.path.join(KB, db)
        if not os.path.exists(p):
            continue
        ec = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)

        def gen():
            for (eid, platform, typ, title, url, depth, payload, ts) in ec.execute(
                    "SELECT id,platform,type,title,url,depth,payload,ts FROM entities"):
                pl = jload(payload, {}) if isinstance(payload, str) else (payload or {})
                if not isinstance(pl, dict):
                    pl = {"raw": str(pl)[:300]}
                bits = []
                for k in ("desc", "description", "text", "signature", "nickname",
                          "author", "music", "tag", "category", "note"):
                    v = pl.get(k)
                    if v and isinstance(v, (str, int, float)):
                        bits.append("%s: %s" % (k, str(v)[:300]))
                text = " ".join(bits)
                meta = {"eid": eid, "platform": platform, "entity_type": typ,
                        "depth": depth, "payload": pl, "ts": ts}
                yield (eid, url, host_of(url, platform), title, "", text, meta, "ok")
        total += _url_rows(c, tax, prof, name, "entity", gen())
        ec.close()
    return total


def src_url_tables(c, tax, prof, force=False):
    total = 0
    specs = [
        ("seeds.db", "SELECT url,host,domain_class,topic,kind,quality,crawled,ts FROM site_index",
         "urls/site_index"),
        ("seeds.db", "SELECT url,platform,depth,state,note,ts,prio FROM seeds",
         "urls/seeds"),
        ("reclaim_state.db", "SELECT domain,url,reason,access,priority,status,attempts,last_err,ok,ts FROM targets",
         "urls/reclaim_targets"),
        ("assets.db", "SELECT url,key,host,ext,src,path,size,status,tries,err,kind FROM assets",
         "urls/assets"),
        ("state.db", "SELECT value,kind,ts FROM fed", "urls/fed"),
    ]
    for db, sql, name in specs:
        if not force and c.execute("SELECT 1 FROM sources WHERE name=?", (name,)).fetchone():
            log("  skip %s (已完成)" % name)
            continue
        p = os.path.join(KB, db)
        if not os.path.exists(p):
            continue
        ec = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)

        def gen(ec=ec, sql=sql, name=name):
            for row in ec.execute(sql):
                if name == "urls/site_index":
                    url, host, dclass, topic, kind, quality, crawled, ts = row
                    key, title, desc, status = url, "", "", ("ok" if crawled else "pending_fetch")
                    meta = {"domain_class": dclass, "topic_orig": topic, "kind_orig": kind,
                            "quality_orig": quality, "crawled": crawled, "ts": ts}
                    text = " ".join(str(x) for x in (topic, kind, dclass) if x)
                elif name == "urls/seeds":
                    url, platform, depth, state, note, ts, prio = row
                    key, host, title, desc, status = url, host_of(url, platform), "", str(note or ""), str(state or "pending_fetch")
                    meta = {"platform": platform, "depth": depth, "state": state,
                            "prio": prio, "ts": ts}
                    text = " ".join(str(x) for x in (platform, note) if x)
                elif name == "urls/reclaim_targets":
                    dom, url, reason, access, priority, status0, attempts, last_err, ok, ts = row
                    key, host, title, desc = url, dom, "", str(reason or "")
                    status = "ok" if ok else ("unreachable" if last_err else "pending_fetch")
                    meta = {"reason": reason, "access": access, "priority": priority,
                            "status_orig": status0, "attempts": attempts,
                            "last_err": last_err, "ts": ts}
                    text = " ".join(str(x) for x in (reason, access, last_err) if x)
                elif name == "urls/assets":
                    url, key0, host, ext, src0, path, size, status0, tries, err, kind0 = row
                    key, title, desc = url, os.path.basename(str(path or "")), ""
                    status = "ok" if status0 == "done" else ("unreachable" if err else "pending_fetch")
                    meta = {"asset_key": key0, "ext": ext, "src": src0, "path": path,
                            "size": size, "status_orig": status0, "tries": tries,
                            "err": err, "kind": kind0}
                    text = " ".join(str(x) for x in (src0, ext, kind0, err) if x)
                else:  # fed
                    value, kind0, ts = row
                    url = ("https://%s/" % value) if kind0 == "domain" else str(value)
                    key, host, title, desc, status = url, host_of(url, value), "", "", "pending_fetch"
                    meta = {"fed_kind": kind0, "ts": ts}
                    text = str(value)
                yield (key, url, host, title, desc, text, meta, status)
        total += _url_rows(c, tax, prof, name, "url", gen())
        ec.close()
    return total


def src_jsonl(c, tax, prof, force=False):
    total = 0
    specs = [
        ("discovered.jsonl", "urls/discovered"),
        ("classified_render.jsonl", "urls/classified_render"),
        ("external_seeds.jsonl", "urls/external_seeds"),
        ("egress_usage.jsonl", "urls/egress_usage"),
        ("seed_records.jsonl", "urls/seed_records"),
        ("assets/manifest.jsonl", "urls/asset_manifest"),
        ("kb_export.jsonl", "urls/kb_export"),
    ]
    for fn, name in specs:
        p = os.path.join(KB, fn)
        if not os.path.exists(p):
            continue
        if not force and c.execute("SELECT 1 FROM sources WHERE name=?", (name,)).fetchone():
            log("  skip %s (已完成)" % name)
            continue

        def gen(p=p, name=name):
            with open(p, encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    url = d.get("url") or d.get("value") or d.get("seed") or d.get("page") or ""
                    host = d.get("domain") or d.get("h") or host_of(url)
                    if isinstance(host, str) and "/" in host:
                        host = host.split("/")[0]
                    title = d.get("title") or ""
                    descr = d.get("desc") or d.get("note") or ""
                    text = " ".join(str(v)[:300] for k, v in d.items()
                                   if k not in ("url", "domain", "value", "ts", "t")
                                   and isinstance(v, (str, int, float)))
                    status = "ok" if d.get("ok") else ("unreachable" if d.get("status") == 0 else "pending_fetch")
                    if name == "urls/classified_render" and d.get("topics"):
                        meta = {"topics_orig": d.get("topics"), "forms_orig": d.get("forms"),
                                "value_orig": d.get("value"), "text_len_orig": d.get("text_len")}
                        text += " " + " ".join(jload(d.get("topics"), []))
                    else:
                        meta = {k: v for k, v in d.items() if k not in ("text",)}
                    yield (i, url, host, title, descr, text[:3000], meta, status)
        total += _url_rows(c, tax, prof, name, "url", gen())
    return total


def src_social(c, tax, prof, force=False):
    """抖音 API 抓取的 aweme_list：把每条作品拆成可检索文本。"""
    name = "social/douyin"
    if not force and c.execute("SELECT 1 FROM sources WHERE name=?", (name,)).fetchone():
        log("  skip %s (已完成)" % name)
        return 0
    files = [os.path.join(KB, "_dy_author_post.json")] + sorted(
        glob.glob(os.path.join(KB, "_dy_api", "*.json"))) + sorted(
        glob.glob(os.path.join(KB, "_dy_*.json")))
    seen = set()

    def gen():
        for p in files:
            if not os.path.exists(p) or p in seen:
                continue
            seen.add(p)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    d = json.load(f)
            except Exception:
                continue
            raw = d
            if isinstance(d, str):
                try:
                    raw = json.loads(d)
                except Exception:
                    continue
            lst = raw.get("aweme_list") if isinstance(raw, dict) else None
            if not isinstance(lst, list):
                continue
            for a in lst:
                if not isinstance(a, dict):
                    continue
                aid = a.get("aweme_id") or ""
                desc = a.get("desc") or ""
                author = a.get("author") or {}
                music = a.get("music") or {}
                st = a.get("statistics") or {}
                bits = [desc]
                for k, v in (("author", author.get("nickname")),
                            ("signature", author.get("signature")),
                            ("music", music.get("title")),
                            ("tags", " ".join(str(x.get("hashtag_name", "")) for x in (a.get("text_extra") or []) if isinstance(x, dict)))):
                    if v:
                        bits.append("%s: %s" % (k, str(v)[:300]))
                meta = {"aweme_id": aid, "author": author.get("nickname"),
                        "create_time": a.get("create_time"),
                        "statistics": st, "music_title": music.get("title"),
                        "images": len(a.get("images") or []), "src_file": os.path.basename(p)}
                url = "https://www.douyin.com/video/%s" % aid if aid else ""
                yield (aid or len(bits), url, "douyin.com", desc[:120], "",
                       " ".join(bits), meta, "ok")
    return _url_rows(c, tax, prof, name, "social_post", gen())


def src_misc_json(c, tax, prof, force=False):
    """crt 证书域名、sitemap 缓存、抖音页面的 URL 清单等。"""
    name = "urls/misc_json"
    if not force and c.execute("SELECT 1 FROM sources WHERE name=?", (name,)).fetchone():
        log("  skip %s (已完成)" % name)
        return 0
    files = (sorted(glob.glob(os.path.join(KB, "crt_*.json"))) +
             sorted(glob.glob(os.path.join(KB, "sitemap_cache", "*.json"))) +
             [os.path.join(KB, x) for x in ("direct_ip.json", "dnshe_domains.json",
                                            "cf_zones_now.json", "_dy_ids.txt")])

    def walk(o, out, depth=0):
        if depth > 6 or len(out) > 20000:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str) and re.match(r"^(https?://|[\w.-]+\.[a-z]{2,})", v):
                    out.append(v)
                else:
                    walk(v, out, depth + 1)
        elif isinstance(o, list):
            for v in o[:5000]:
                walk(v, out, depth + 1)
        elif isinstance(o, str) and re.match(r"^(https?://|[\w.-]+\.[a-z]{2,})", o):
            out.append(o)

    def gen():
        for p in files:
            if not os.path.exists(p):
                continue
            try:
                if p.endswith(".txt"):
                    with open(p, encoding="utf-8", errors="replace") as f:
                        vals = [x.strip() for x in f if x.strip()]
                else:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        vals = []
                        walk(json.load(f), vals)
            except Exception:
                continue
            for i, v in enumerate(vals):
                if not v or len(v) > 300:
                    continue
                url = v if v.startswith("http") else "https://%s/" % v.lstrip("/")
                yield ("%s:%d" % (os.path.basename(p), i), url, host_of(url),
                       "", "", os.path.basename(p),
                       {"src_file": os.path.basename(p), "raw": v[:200]},
                       "pending_fetch")
    return _url_rows(c, tax, prof, name, "domain", gen())


# ------------------------------------------------------------------ 源：netforce 根目录爬虫产物
ROOT = os.path.dirname(HERE)          # E:\xmanbian
CORPUS_DIR = os.path.join(ROOT, "corpus")


def emit(c, tax, prof, name, items, batch_size=400):
    """通用写入器：items 为 dict 生成器，支持 extra 覆盖字段。"""
    t0, n, batch = time.time(), 0, []
    for it in items:
        r = make_row(name, it.get("kind", "url"), it["key"],
                     it.get("url", ""), it.get("host", ""),
                     it.get("title", ""), it.get("descr", ""),
                     it.get("text", ""), it.get("meta", {}),
                     it.get("status", "pending_fetch"), tax, prof,
                     it.get("extra"))
        batch.append(r)
        n += 1
        if len(batch) >= batch_size:
            write_rows(c, batch)
            batch = []
    if batch:
        write_rows(c, batch)
    c.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?,?)",
              (name, n, datetime.now().isoformat(timespec="seconds"), time.time() - t0))
    c.commit()
    log("  %s: %d 条，%.1fs" % (name, n, time.time() - t0))
    return n


def _json_list(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        return []


def src_root(c, tax, prof, force=False):
    """netforce 根目录的爬虫产物：resources/classified/visited/skynet/mobile/外移目录。"""
    total = 0
    checked = set()
    if not force:
        checked = {r[0] for r in c.execute("SELECT name FROM sources WHERE name LIKE 'root/%'")}
    existing_entities = {r[0] for r in c.execute("SELECT url FROM corpus WHERE kind='entity'")}

    def spec(name, fn):
        if name in checked:
            log("  skip %s (已完成)" % name)
            return 0
        return emit(c, tax, prof, name, fn())

    def g_resources():
        for i, o in enumerate(_json_list(os.path.join(HERE, "resources.json"))):
            if not isinstance(o, dict):
                continue
            url = o.get("url", "")
            yield {"key": url or i, "url": url,
                   "host": o.get("domain") or host_of(url),
                   "meta": {"type": o.get("type"), "found_via": o.get("found_via")}}
    total += spec("root/resources", g_resources)

    def g_classified():
        for i, o in enumerate(_json_list(os.path.join(HERE, "classified.json"))):
            if not isinstance(o, dict):
                continue
            url = o.get("url", "")
            txt = " ".join(str(x) for x in
                           (o.get("title"), o.get("desc"), o.get("basis"),
                            o.get("category")) if x)
            yield {"key": url or i, "url": url, "host": o.get("domain") or host_of(url),
                   "title": o.get("title", ""), "descr": o.get("desc", ""),
                   "text": txt[:3000],
                   "status": "ok" if o.get("http_ok") else "unreachable",
                   "meta": {k: v for k, v in o.items() if k not in ("title", "desc", "url")}}
    total += spec("root/classified", g_classified)

    def g_visited_json():
        for i, u in enumerate(_json_list(os.path.join(HERE, "_visited.json"))):
            yield {"key": u, "url": u, "host": host_of(u)}
    total += spec("root/visited_json", g_visited_json)

    def g_visited_db():
        p = os.path.join(HERE, "_visited.db")
        if not os.path.exists(p):
            return
        cc = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
        for (u,) in cc.execute("SELECT k FROM seen"):
            yield {"key": u, "url": u, "host": host_of(u)}
        cc.close()
    total += spec("root/visited_db", g_visited_db)

    def g_harvested():
        for d in _json_list(os.path.join(HERE, "_harvested.json")):
            u = "https://%s/" % d if not str(d).startswith("http") else d
            yield {"key": d, "url": u, "host": str(d).split("/")[0]}
    total += spec("root/harvested", g_harvested)

    def g_skynet_seen():
        p = os.path.join(HERE, "_skynet.db")
        if not os.path.exists(p):
            return
        cc = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
        for (url, host, kind, verdict, score, size, tries, err, ts, path) in cc.execute(
                "SELECT url,host,kind,verdict,score,size,tries,err,ts,path FROM seen"):
            yield {"key": url, "url": url, "host": host, "kind": "probe",
                   "text": " ".join(str(x) for x in (verdict, err) if x),
                   "status": "ok" if verdict == "valuable" else "no_text",
                   "meta": {"verdict": verdict, "score": score, "size": size,
                            "tries": tries, "err": err, "ts": ts, "path": path}}
        cc.close()
    total += spec("root/skynet_seen", g_skynet_seen)

    def g_skynet_blob():
        p = os.path.join(HERE, "_skynet.db")
        if not os.path.exists(p):
            return
        cc = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
        for (sha, path, size, refs, ts) in cc.execute(
                "SELECT sha256,path,size,refs,ts FROM blob"):
            fn = os.path.basename(str(path))
            yield {"key": sha, "url": "file:///" + str(path).replace("\\", "/"),
                   "host": "local-blob", "kind": "file", "title": fn,
                   "meta": {"sha256": sha, "path": path, "size": size,
                            "refs": refs, "ts": ts}, "status": "no_text"}
        cc.close()
    total += spec("root/skynet_blob", g_skynet_blob)

    def g_mobile():
        for db in ("mobile/kb.db", "mobile/baseline.db"):
            p = os.path.join(HERE, db)
            if not os.path.exists(p):
                continue
            cc = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
            for (eid, platform, typ, title, url, depth, payload, ts) in cc.execute(
                    "SELECT id,platform,type,title,url,depth,payload,ts FROM entities"):
                if url in existing_entities:
                    continue
                pl = jload(payload, {}) if isinstance(payload, str) else (payload or {})
                bits = [str(v)[:300] for k, v in pl.items()
                        if isinstance(v, (str, int, float)) and k in
                        ("desc", "description", "text", "nickname", "author", "signature")]
                yield {"key": eid, "url": url, "host": host_of(url, platform),
                       "kind": "entity", "title": title,
                       "text": " ".join(bits)[:2000],
                       "meta": {"eid": eid, "platform": platform, "entity_type": typ,
                                "depth": depth, "payload": pl, "ts": ts},
                       "status": "ok"}
            cc.close()
    total += spec("root/mobile_entities", g_mobile)

    def g_misc():
        files = (sorted(glob.glob(os.path.join(HERE, "_tmp_*.json"))) +
                 sorted(glob.glob(os.path.join(HERE, "_dl", "*.json"))) +
                 sorted(glob.glob(os.path.join(HERE, "_*.json"))) +
                 sorted(glob.glob(os.path.join(HERE, "reverse_out", "*.json"))) +
                 sorted(glob.glob(os.path.join(ROOT, "_inbox", "**", "_local_out", "*.json*"),
                                  recursive=True)))
        seen = set()

        def walk(o, out, depth=0):
            if depth > 7 or len(out) > 8000:
                return
            if isinstance(o, dict):
                for k, v in o.items():
                    if isinstance(v, str) and re.match(r"^https?://|^([\w-]+\.)+[a-z]{2,}", v):
                        out.append(v)
                    else:
                        walk(v, out, depth + 1)
            elif isinstance(o, list):
                for v in o[:3000]:
                    walk(v, out, depth + 1)
            elif isinstance(o, str) and re.match(r"^https?://|^([\w-]+\.)+[a-z]{2,}", o):
                out.append(o)
        for p in files:
            if p in seen or not os.path.isfile(p):
                continue
            seen.add(p)
            try:
                if p.endswith(".jsonl"):
                    vals = []
                    with open(p, encoding="utf-8", errors="replace") as f:
                        for line in f:
                            try:
                                walk(json.loads(line), vals)
                            except Exception:
                                pass
                else:
                    vals = []
                    walk(json.load(open(p, encoding="utf-8", errors="replace")), vals)
            except Exception:
                continue
            for i, v in enumerate(vals[:8000]):
                v = str(v)[:300]
                url = v if v.startswith("http") else "https://%s/" % v.lstrip("/")
                yield {"key": "%s:%d" % (os.path.basename(p), i), "url": url,
                       "host": host_of(url), "meta": {"src_file": os.path.basename(p), "raw": v}}
    total += spec("root/misc_json", g_misc)

    def g_wiki():
        p = os.path.join(HERE, "wiki_siblings.txt")
        if not os.path.exists(p):
            return
        for i, w in enumerate(open(p, encoding="utf-8", errors="replace").read().split("|")):
            w = w.strip()
            if not w:
                continue
            url = "https://wiki.biligame.com/%s/" % w
            yield {"key": w, "url": url, "host": "wiki.biligame.com",
                   "meta": {"sibling": w, "src_file": "wiki_siblings.txt"}}
    total += spec("root/wiki_siblings", g_wiki)
    return total


# ------------------------------------------------------------------ 源：本地训练语料（文件级登记）
CORPUS_TAGS = {"category": "小说/训练语料",
               "topics": jdump(["小说/文学语料", "中文长文本"]),
               "forms": jdump(["语料/长文本"])}


def src_corpus_files(c, tax, prof, force=False):
    if not os.path.isdir(CORPUS_DIR):
        log("  corpus 目录不存在，跳过")
        return 0
    if not force and c.execute("SELECT 1 FROM sources WHERE name='corpus_files'").fetchone():
        log("  skip corpus_files (已完成)")
        return 0
    c.execute("""CREATE TABLE IF NOT EXISTS corpus_digest(
        file TEXT PRIMARY KEY, size INTEGER, offset INTEGER,
        done INTEGER, chunks INTEGER, ts TEXT)""")
    c.commit()
    t0, n, batch = time.time(), 0, []
    for p in sorted(glob.glob(os.path.join(CORPUS_DIR, "*.txt"))):
        fn = os.path.basename(p)
        size = os.path.getsize(p)
        head = ""
        for enc in ("utf-8", "gb18030", "latin-1"):
            try:
                with open(p, encoding=enc, errors="replace") as f:
                    head = f.read(30000)
                break
            except Exception:
                continue
        title = re.sub(r"\s+", " ", head[:120]).strip()[:200]
        extra = dict(CORPUS_TAGS)
        r = make_row("corpus_file", "corpus_file", fn,
                     "file:///" + p.replace("\\", "/"), "local-corpus",
                     title or fn, "", head,
                     {"full_path": p, "size": size, "head_only": True,
                      "note": "全文在磁盘；autoloop 按 corpus_digest 渐进分块消化"},
                     "sampled", tax, prof, extra)
        batch.append(r)
        c.execute("INSERT OR IGNORE INTO corpus_digest VALUES(?,?,?,?,?,?)",
                  (p, size, 0, 0, 0, datetime.now().isoformat(timespec="seconds")))
        n += 1
        if len(batch) >= 20:
            write_rows(c, batch)
            batch = []
    if batch:
        write_rows(c, batch)
    c.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?,?)",
              ("corpus_files", n, datetime.now().isoformat(timespec="seconds"), time.time() - t0))
    c.commit()
    log("  corpus_files: %d 个语料文件登记（共 %d 字节待渐进消化），%.1fs" %
        (n, sum(os.path.getsize(p) for p in glob.glob(os.path.join(CORPUS_DIR, "*.txt"))),
         time.time() - t0))
    return n


# ------------------------------------------------------------------ 回填主索引
def backfill_legacy(c):
    """用 records 里的完整正文/标题，把 kb_index.db 的空字段补齐并重打标签。"""
    if not os.path.exists(LEGACY_DB):
        return 0
    lc = sqlite3.connect(LEGACY_DB, timeout=120)
    lc.execute("PRAGMA busy_timeout=120000")
    lc.execute("PRAGMA journal_mode=WAL")
    lc.execute("PRAGMA synchronous=NORMAL")
    # 建 host+url -> 最佳正文记录
    best = {}
    for url, host, title, descr, text, topics, cat in c.execute(
            "SELECT url,host,title,descr,text,topics,category FROM corpus "
            "WHERE source LIKE 'records/%' AND text_len>300 ORDER BY text_len DESC"):
        k = (url or "").strip()
        if k and k not in best:
            best[k] = (title, descr, text, topics, cat)
    log("[backfill] records 正文映射 %d 条 URL" % len(best))
    n_title = n_snip = n_topic = n_cat = 0
    hit = 0
    cur = lc.cursor()
    rows = cur.execute("SELECT url,title,snippet,topics,category FROM docs").fetchall()
    for url, title, snip, topics, cat in rows:
        b = best.get((url or "").strip())
        if not b:
            continue
        nt, nd, ntext, ntp, ncat = b
        sets, vals = [], []
        if (not title) and nt:
            sets.append("title=?"); vals.append(nt[:300]); n_title += 1
        if (not snip or len(snip) < 120) and ntext:
            sets.append("snippet=?"); vals.append(ntext[:1500]); n_snip += 1
        if (not descr_empty(nd)) and not snip:
            pass
        if (not topics or topics in ("[]", "")) and ntp not in ("[]", ""):
            sets.append("topics=?"); vals.append(ntp); n_topic += 1
        if (not cat or cat in ("其他/待定", "访问失败（保留待复查）")) and ncat:
            sets.append("category=?"); vals.append(ncat); n_cat += 1
        if sets:
            vals.append(url)
            lc.execute("UPDATE docs SET %s WHERE url=?" % ",".join(sets), vals)
            hit += 1
            if hit % 500 == 0:
                lc.commit()      # 分批提交，避免长事务把主索引锁死几分钟
    lc.commit()
    lc.close()
    log("[backfill] kb_index 补齐 标题 %d / 正文 %d / 主题 %d / 类目 %d" %
        (n_title, n_snip, n_topic, n_cat))
    return n_title + n_snip + n_topic + n_cat


def descr_empty(x):
    return not x or x in ("", "[]", "{}")


# ------------------------------------------------------------------ 重打标签
def retag_all(c):
    tax = load_taxonomy()
    prof = build_host_profile(c)
    t0 = time.time()
    n, batch = 0, []
    cur = c.execute("SELECT uid,source,kind,url,host,title,descr,text,text_len,"
                    "lang,parse_status,meta FROM corpus")
    for (uid, source, kind, url, host, title, descr, text, tlen, lang, status, meta) in cur:
        rec = {"host": host, "url": url, "title": title, "descr": descr,
               "text": text, "text_len": tlen, "lang": lang, "meta": meta}
        upd = classify(rec, tax, prof)
        cat, tp, fm = fallback_tags(
            {"host": host, "url": url, "category": upd["category"], "forms": upd["forms"],
             "kind": kind, "text_len": tlen, "parse_status": status},
            prof.get(host))
        c.execute("UPDATE corpus SET category=?,topics=?,forms=?,subtopics=?,"
                  "extra_topics=?,quality=?,value=?,evidence=?,parse_ver=? WHERE uid=?",
                  (cat, tp, fm, upd["subtopics"], upd["extra_topics"],
                   upd["quality"], upd["value"], upd["evidence"], PARSE_VER, uid))
        n += 1
        if n % 5000 == 0:
            c.commit()
            log("  [retag] %d 条 %.1fs" % (n, time.time() - t0))
    c.commit()
    log("[retag] 完成 %d 条，%.1fs" % (n, time.time() - t0))


# ------------------------------------------------------------------ 验收与统计
def verify(c):
    log("\n=== 零遗留验收 ===")
    total = c.execute("SELECT COUNT(*) FROM corpus").fetchone()[0]
    checks = [
        ("空主题标签", "topics IS NULL OR topics IN ('','[]')"),
        ("空类目", "category IS NULL OR category=''"),
        ("空形态", "forms IS NULL OR forms IN ('','[]')"),
        ("空来源", "source IS NULL OR source=''"),
        ("有正文但未入 FTS", "text_len>200"),
    ]
    bad = 0
    for label, cond in checks[:-1]:
        n = c.execute("SELECT COUNT(*) FROM corpus WHERE " + cond).fetchone()[0]
        print("  %-18s %d" % (label, n))
        bad += n
    fts = c.execute("SELECT COUNT(*) FROM corpus_fts").fetchone()[0]
    withtext = c.execute("SELECT COUNT(*) FROM corpus WHERE text_len>200").fetchone()[0]
    print("  %-18s %d / FTS %d" % ("有正文记录", withtext, fts))
    print("  %-18s %s" % ("语料总条数", format(total, ",")))
    print("  %-18s %s" % ("入库正文字符",
                          format(c.execute("SELECT COALESCE(SUM(text_len),0) FROM corpus").fetchone()[0], ",")))
    print("  %-18s %s" % ("遗留项", bad))
    print("\n=== 按来源 ===")
    for r in c.execute("SELECT source,COUNT(*),SUM(text_len>200) FROM corpus GROUP BY source ORDER BY 2 DESC"):
        print("  %-34s %8d  带正文 %d" % (r[0], r[1], r[2] or 0))
    print("\n=== 解析状态 ===")
    for r in c.execute("SELECT parse_status,COUNT(*) FROM corpus GROUP BY 1 ORDER BY 2 DESC"):
        print("  %-18s %d" % r)
    print("\n=== 主题 TOP20 ===")
    tp = Counter()
    for (t,) in c.execute("SELECT topics FROM corpus"):
        tp.update(jload(t, []))
    for k, v in tp.most_common(20):
        print("  %-24s %d" % (k, v))
    print("\n=== 中文全文检索抽样 ===")
    for q in ("机器学习", "网络安全", "数字主权", "quantization"):
        try:
            n = c.execute("SELECT COUNT(*) FROM corpus_fts WHERE corpus_fts MATCH ?", (q,)).fetchone()[0]
            print("  %-12s %d hits" % (q, n))
        except Exception as e:
            print("  %-12s ERR %s" % (q, str(e)[:60]))
    return bad


def stats(c):
    total = c.execute("SELECT COUNT(*) FROM corpus").fetchone()[0]
    print("corpus.db 共 %s 条，%.1f MB" %
          (format(total, ","), os.path.getsize(DB) / 1048576))
    print("正文 %s 字符，FTS %d 条" %
          (format(c.execute("SELECT COALESCE(SUM(text_len),0) FROM corpus").fetchone()[0], ","),
           c.execute("SELECT COUNT(*) FROM corpus_fts").fetchone()[0]))


# ------------------------------------------------------------------ 主流程
# 🔴 11-20 P3：源解析器注册表。加新源 = 加一行 (名字, 函数, 是否依赖 host 画像)，
#    main() 调度和 --stage 选项自动跟上，不用再改 if/elif 两处。
#    为什么是「代码内注册表」而非外部 JSON：解析器是可执行函数，没法序列化进配置。
#    真正可配置的「标签规则」早在 _kb/tags_ext.json（extra_tags 增量层）+ fine_classify
#    基础规则里，那层已经可插拔，无需重造——这里收敛的是「有哪些源、怎么调度」。
#    need_prof=True 的源每个都现建 host 画像：前面 stage 写进去的语料要能被后面看到，
#    与旧 if/elif 逐条 build_host_profile 行为一致，不做跨 stage 缓存优化。
SOURCE_PARSERS = [
    ("records",      src_records,      False),
    ("kb_index",     src_kb_index,     False),
    ("asset_docs",   src_asset_docs,   False),
    ("entities",     src_entities,     True),
    ("url_tables",   src_url_tables,   True),
    ("jsonl",        src_jsonl,        True),
    ("social",       src_social,       True),
    ("misc_json",    src_misc_json,    True),
    ("root",         src_root,         True),
    ("corpus_files", src_corpus_files, True),
]
STAGES = tuple(name for name, _fn, _prof in SOURCE_PARSERS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--stage", choices=STAGES)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--retag", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()

    c = connect()
    if a.stats:
        return stats(c)
    if a.verify:
        return verify(c)
    if a.retag:
        retag_all(c)
        return verify(c)
    if a.backfill:
        return backfill_legacy(c)

    tax = load_taxonomy()
    t0 = time.time()
    stages = [a.stage] if a.stage else (STAGES if a.all else [])
    if not stages:
        print("指定 --all / --stage / --retag / --verify / --stats")
        return
    log("开始：%s" % ", ".join(stages))
    # 注册表分派：need_prof 的源才现建 host 画像，其余走两参签名。
    # 加新源只需在 SOURCE_PARSERS 加一行，这里一行都不用碰。
    registry = {name: (fn, need_prof) for name, fn, need_prof in SOURCE_PARSERS}
    for s in stages:
        fn, need_prof = registry[s]
        try:
            if need_prof:
                fn(c, tax, build_host_profile(c), a.force)
            else:
                fn(c, tax, a.force)
        except Exception as e:
            import traceback
            log("  [ERROR] %s -> %s" % (s, e))
            traceback.print_exc()
    log("全部阶段结束，耗时 %.1fs" % (time.time() - t0))

    # 全量跑完自动补齐兜底标签 + 回填 + 验收
    if a.all:
        backfill_legacy(c)
        verify(c)


if __name__ == "__main__":
    main()
