# -*- coding: utf-8 -*-
"""netforce / parse_assets.py —— 已下载资源的离线批量解析 + 可扩展标签层

为什么要另起这条线：
    render_parse.py 是「浏览器渲染 + 网络等待」的完整解析引擎，正确但慢；
    本地已经躺着 526 个下载完成的文件（349 个 PDF），它们从来没被解析过。
    这一步不联网、不起浏览器，只用现成解析库并行把本地文件读成正文，
    再打标签入库 —— 分钟级跑完，而不是按天计的渲染队列。

标签分两层：
    基础层 = fine_classify.tag()（形态/主题/二级主题/语言/可达性/质量…）
    扩展层 = _kb/tags_ext.json（可增量添加，加完跑 --retag 即可，不重解析文件）

用法：
    python parse_assets.py                 # 解析全部未解析文件（断点续跑）
    python parse_assets.py --retag         # 只重打标签，不读文件（标签词表改完跑这个）
    python parse_assets.py --backfill-docs # 把标签回填到 kb_index.db 的空标签记录
    python parse_assets.py --stats          # 只看统计
    python parse_assets.py --workers 8     # 指定并行进程数
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import zipfile
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(HERE, "_kb")
ASSETS = os.path.join(KB, "assets")
TAXONOMY = os.path.join(KB, "tags_ext.json")
DB = os.path.join(KB, "asset_index.db")
LEGACY_DB = os.path.join(KB, "kb_index.db")
ASSETS_DB = os.path.join(KB, "assets.db")

MAX_TEXT = 100000          # 单文件最多提取多少字符
STORE_TEXT = 60000         # 入库文本上限（够全文检索，避免库膨胀）
MAX_PAGES = 80             # PDF 最多解析页数（大文件只取前 40 页）
BIG_FILE = 20 * 1024 * 1024
PARSE_VER = 2

TEXT_EXT = {".txt", ".md", ".markdown", ".rst", ".adoc", ".json", ".jsonl",
            ".xml", ".csv", ".tsv", ".log", ".py", ".js", ".yaml", ".yml",
            ".html", ".htm", ".srt", ".vtt", ".tex", ".bib"}
ZIP_TEXT_EXT = {".docx", ".pptx", ".odt"}
BINARY_EXT = {".mp3", ".mp4", ".m4a", ".wav", ".flac", ".ogg", ".avi", ".mkv",
              ".webm", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico",
              ".zip", ".gz", ".tar", ".7z", ".rar", ".dmg", ".exe", ".msi",
              ".apk", ".deb", ".rpm", ".aria2", ".pdfa", ".woff", ".woff2"}
CJK = re.compile(r"[\u4e00-\u9fff]")
TAG_STRIP = re.compile(r"<[^>]+>")


# ------------------------------------------------------------------ 标签词表
def load_taxonomy():
    with open(TAXONOMY, encoding="utf-8") as f:
        return json.load(f)


def _word_hit(hay, kw):
    """英文按词边界，中文按子串；跟 fine_classify 的口径保持一致。"""
    k = str(kw).lower()
    if CJK.search(k):
        return k in hay
    parts = [p for p in re.split(r"[\s\-_/]+", k) if p]
    if not parts:
        return k in hay
    body = r"[\s\-_/]+".join(re.escape(p) for p in parts)
    return bool(re.search(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])", hay))


def extra_tags(tax, hay, host, fname):
    """扩展层：返回 (extra_topics, extra_forms, evidence)。"""
    ev = {}
    topics, forms = [], []
    for group, out in (("extra_topics", topics), ("extra_forms", forms)):
        for rule in tax.get(group, []):
            name = rule["name"]
            hits = [k for k in rule.get("keywords", []) if _word_hit(hay, k)]
            dh = rule.get("domain_hint", [])
            if hits or any(d in host for d in dh):
                out.append(name)
                if hits:
                    ev[name] = hits[:3]
    dm = tax.get("domain_topics", {})
    for dom, names in dm.items():
        if dom in host:
            for n in names:
                if n not in topics:
                    topics.append(n)
    return topics, forms, ev


def merge_tags(base, et, ef, extra_ev, title, tlen):
    """收敛标签宽度：长文档喂 12000 字符会让 21 个主题命中一半，全是噪声。

    按 evidence 里的命中词数打分（标题命中加权），取 Top-K；
    裁剪前的原始标签存进 evidence['_raw']，随时可回溯、不用重解析。
    """
    ev = dict(base.get("evidence", {}) or {})
    ev.update(extra_ev or {})
    raw = {"topics": base.get("topics", []),
           "forms": base.get("forms", []),
           "subtopics": base.get("subtopics", [])}
    tl = (title or "").lower()
    need = 2   # evidence 每主题最多只记 3 个命中词；要求「至少 1 个」
              # 再按命中数排序取 Top-4，既压掉长文噪声又不把正常主题砍光

    def score(name):
        kws = ev.get(name, []) or []
        s = 2 * len(kws)
        if any(str(k).lower() in tl for k in kws):
            s += 3
        return s

    base_tp = list(base.get("topics", []))
    tp = [n for n in base_tp if score(n) >= need]
    tp = sorted(tp, key=score, reverse=True)[:4]
    if not tp and base_tp:
        tp = [max(base_tp, key=score)]
    subs = sorted(base.get("subtopics", []), key=score, reverse=True)[:2]
    forms = [f for f in base.get("forms", []) if f != "未识别/待人工看一眼"][:3]
    if not forms:
        forms = ["未识别/待人工看一眼"]
    et_keep = sorted(et, key=lambda n: -len((extra_ev or {}).get(n, []) or []))[:3]
    ev["_raw"] = raw
    return (list(dict.fromkeys(tp + et_keep)), subs,
            list(dict.fromkeys(forms + ef[:2])), ev)


# ------------------------------------------------------------------ 文本提取
def _read_plain(path):
    with open(path, "rb") as f:
        raw = f.read(MAX_TEXT * 3)
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            s = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        s = raw.decode("utf-8", "ignore")
    return s


def _html_text(s):
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(s, "lxml")
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        title = (soup.title.get_text(" ", strip=True) if soup.title else "")
        return title, soup.get_text("\n", strip=True)
    except Exception:
        return "", TAG_STRIP.sub(" ", s)


def _zip_text(path, ext):
    out = []
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.endswith(".xml")]
            pat = "word/document" if ext == ".docx" else "ppt/slides/slide"
            names = [n for n in names if n.startswith(pat)] or names[:30]
            for n in names[:40]:
                out.append(TAG_STRIP.sub(" ", z.read(n).decode("utf-8", "ignore")))
    except Exception:
        pass
    return "\n".join(out)


def extract(path):
    """返回 (text, title, pages, scanned, err)"""
    ext = os.path.splitext(path)[1].lower()
    try:
        if os.path.getsize(path) < 32:
            return "", "", 0, False, "broken: empty/corrupt download"
    except OSError:
        return "", "", 0, False, "broken: unreadable"
    try:
        if ext == ".pdf":
            import pymupdf as fitz
            doc = fitz.open(path)
            pages = doc.page_count
            limit = 40 if os.path.getsize(path) > BIG_FILE else MAX_PAGES
            chunks, total = [], 0
            for i in range(min(pages, limit)):
                t = doc[i].get_text("text")
                total += len(t)
                chunks.append(t)
                if total >= MAX_TEXT:
                    break
            title = ""
            try:
                title = (doc.metadata or {}).get("title") or ""
            except Exception:
                pass
            doc.close()
            text = "\n".join(chunks)[:MAX_TEXT]
            return text, title, pages, total < max(100, pages * 40), ""
        if ext in ZIP_TEXT_EXT:
            t = _zip_text(path, ext)[:MAX_TEXT]
            return t, "", 0, False, ""
        if ext in (".html", ".htm"):
            raw = _read_plain(path)
            title, t = _html_text(raw)
            return t[:MAX_TEXT], title, 0, False, ""
        if ext in TEXT_EXT:
            raw = _read_plain(path)
            title = ""
            if ext in (".html", ".htm"):
                title, raw = _html_text(raw)
            head = raw[:2000]
            m = re.search(r"(?:^|\n)#\s+(.+)", head) or re.search(r"(?:^|\n)title:\s*(.+)", head, re.I)
            if m:
                title = m.group(1).strip()[:200]
            return raw[:MAX_TEXT], title, 0, False, ""
        if ext in BINARY_EXT:
            return "", "", 0, False, ""
        # 未知扩展名：当文本试一次
        raw = _read_plain(path)
        return raw[:MAX_TEXT], "", 0, False, ""
    except Exception as e:
        return "", "", 0, False, "%s: %s" % (type(e).__name__, str(e)[:120])


def _lang(text):
    if not text:
        return "无正文"
    n = len(CJK.findall(text[:4000]))
    r = n / max(1, len(text[:4000]))
    return "中文" if r > 0.12 else ("中英混排" if r > 0.02 else "英文/拉丁")


# ------------------------------------------------------------------ worker
def parse_one(job):
    path, host, url = job
    t0 = time.time()
    fname = os.path.basename(path)
    ext = os.path.splitext(path)[1].lower()
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        sha = h.hexdigest()
    except Exception:
        sha = ""

    text, title, pages, scanned, err = extract(path)
    text_len = len(text)
    if not title:
        title = re.sub(r"^[0-9a-f]{12,16}_", "", os.path.splitext(fname)[0])
        title = title.replace("_", " ").replace("-", " ")[:200]

    hay = " ".join([title, fname, host, url, text[:12000]]).lower()
    rec = {
        "domain": host, "url": url or "", "ok": True, "status": 200,
        "title": title, "desc": text[:300].replace("\n", " "), "text": text,
        "text_len": text_len, "headings": [], "keywords": [], "links": [],
        "assets": [{"url": url or path}] if ext not in BINARY_EXT else [],
        "new_domains": [], "xhr_count": 0, "structured": False,
        "rendered": False, "text_density": None, "lang": _lang(text),
    }
    try:
        sys.path.insert(0, HERE)
        from fine_classify import tag
        base = tag(rec)
    except Exception as e:
        base = {"category": "其他/待定", "forms": [], "topics": [],
                "subtopics": [], "evidence": {}, "lang": _lang(text),
                "quality": 0, "value": "低", "exts": []}
        err = (err + " | tag:" + str(e)[:80]).strip(" |")

    try:
        tax = load_taxonomy()
        et, ef, ev = extra_tags(tax, hay, host, fname)
    except Exception:
        et, ef, ev = [], [], {}

    topics, subs, tag_forms, ev_all = merge_tags(base, et, ef, ev, title, text_len)
    q = int(base.get("quality", 0))
    if text_len > 2000:
        q = min(100, q + 10)
    if scanned and pages > 1:
        q = max(0, q - 8)

    return {
        "path": path, "host": host, "url": url or "", "ext": ext,
        "size": size, "sha256": sha, "title": title,
        "text": text[:STORE_TEXT], "text_len": text_len, "pages": pages,
        "lang": base.get("lang") or _lang(text),
        "category": base.get("category", ""),
        "forms": json.dumps(tag_forms, ensure_ascii=False),
        "topics": json.dumps(topics, ensure_ascii=False),
        "subtopics": json.dumps(subs, ensure_ascii=False),
        "extra_topics": json.dumps(et, ensure_ascii=False),
        "evidence": json.dumps(ev_all, ensure_ascii=False)[:4000],
        "quality": q, "value": "高" if q >= 55 else ("中" if q >= 28 else "低"),
        "parse_status": ("broken" if err.startswith("broken:") else
                          "binary" if ext in BINARY_EXT else
                          "scanned" if scanned else
                          "error" if err else "ok"),
        "parse_err": err[:300], "parse_ms": int((time.time() - t0) * 1000),
        "scanned": int(scanned), "parse_ver": PARSE_VER,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ------------------------------------------------------------------ 存储
COLS = ["path", "host", "url", "ext", "size", "sha256", "title", "text",
        "text_len", "pages", "lang", "category", "forms", "topics",
        "subtopics", "extra_topics", "evidence", "quality", "value",
        "parse_status", "parse_err", "parse_ms", "scanned", "parse_ver", "ts"]


def connect():
    os.makedirs(KB, exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("""CREATE TABLE IF NOT EXISTS asset_docs(
        path TEXT PRIMARY KEY, host TEXT, url TEXT, ext TEXT, size INTEGER,
        sha256 TEXT, title TEXT, text TEXT, text_len INTEGER, pages INTEGER,
        lang TEXT, category TEXT, forms TEXT, topics TEXT, subtopics TEXT,
        extra_topics TEXT, evidence TEXT, quality INTEGER, value TEXT,
        parse_status TEXT, parse_err TEXT, parse_ms INTEGER, scanned INTEGER,
        parse_ver INTEGER, ts TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_assets_host ON asset_docs(host)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_assets_status ON asset_docs(parse_status)")
    c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS asset_fts USING fts5(
        path UNINDEXED, host, title, topics, text)""")
    c.commit()
    return c


def write_rows(c, rows):
    ph = ",".join("?" for _ in COLS)
    sql = "INSERT OR REPLACE INTO asset_docs(%s) VALUES(%s)" % (",".join(COLS), ph)
    fts_del = "DELETE FROM asset_fts WHERE path=?"
    fts_ins = "INSERT INTO asset_fts(path,host,title,topics,text) VALUES(?,?,?,?,?)"
    for r in rows:
        c.execute(sql, [r[k] for k in COLS])
        c.execute(fts_del, (r["path"],))
        c.execute(fts_ins, (r["path"], r["host"], r["title"], r["topics"], r["text"]))
    c.commit()


def asset_url_map():
    m = {}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % ASSETS_DB.replace("\\", "/"), uri=True)
        for p, u, h in c.execute("SELECT path,url,host FROM assets"):
            m[os.path.normcase(os.path.normpath(str(p)))] = (u, h)
        c.close()
    except Exception:
        pass
    return m


def scan_files():
    jobs, umap = [], asset_url_map()
    for root, _, files in os.walk(ASSETS):
        for fn in files:
            p = os.path.join(root, fn)
            key = os.path.normcase(os.path.normpath(p))
            u, h = umap.get(key, ("", ""))
            if not h:
                h = os.path.basename(root)
            jobs.append((p, h, u))
    return jobs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=max(2, min(8, (os.cpu_count() or 4) - 1)))
    ap.add_argument("--retag", action="store_true")
    ap.add_argument("--backfill-docs", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    c = connect()
    if a.stats:
        return stats(c)

    if a.retag:
        return retag(c)

    done = {}
    if not a.force:
        for p, sha in c.execute("SELECT path,sha256 FROM asset_docs"):
            done[os.path.normcase(os.path.normpath(p))] = sha
    jobs = scan_files()
    todo = [j for j in jobs if os.path.normcase(os.path.normpath(j[0])) not in done]
    print("[parse] 本地文件 %d 个，待解析 %d 个，%d 进程并行" % (len(jobs), len(todo), a.workers), flush=True)

    t0 = time.time()
    ok = fail = 0
    batch = []
    if todo:
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            futs = {ex.submit(parse_one, j): j for j in todo}
            for n, fu in enumerate(as_completed(futs), 1):
                try:
                    r = fu.result()
                    batch.append(r)
                    ok += 1
                except Exception as e:
                    fail += 1
                    if fail <= 5:
                        print("  [warn] %s -> %s" % (futs[fu][0][-70:], str(e)[:100]), flush=True)
                if len(batch) >= 40:
                    write_rows(c, batch)
                    batch = []
                if n % 100 == 0:
                    print("  [%d/%d] %.1fs" % (n, len(todo), time.time() - t0), flush=True)
    if batch:
        write_rows(c, batch)
    print("[parse] 完成 %d 条，失败 %d 条，耗时 %.1fs" % (ok, fail, time.time() - t0), flush=True)

    if a.backfill_docs:
        backfill(c)
    return stats(c)


def retag(c):
    """只重打标签：读库里已存的 text，不重新解析文件。"""
    t0 = time.time()
    rows = c.execute("SELECT path,host,url,ext,size,sha256,title,text,text_len,pages,"
                     "parse_status,parse_err,parse_ms,scanned FROM asset_docs").fetchall()
    print("[retag] 重打 %d 条（不读文件）" % len(rows), flush=True)
    tax = load_taxonomy()
    sys.path.insert(0, HERE)
    from fine_classify import tag
    out = []
    for (p, host, url, ext, size, sha, title, text, tlen, pages,
         status, perr, pms, scanned) in rows:
        fname = os.path.basename(p)
        hay = " ".join([title, fname, host, url, text[:12000]]).lower()
        rec = {"domain": host, "url": url, "ok": True, "status": 200,
               "title": title, "desc": text[:300].replace("\n", " "),
               "text": text, "text_len": tlen, "headings": [], "keywords": [],
               "links": [], "assets": [], "structured": False, "rendered": False}
        try:
            base = tag(rec)
        except Exception:
            base = {"category": "", "forms": [], "topics": [], "subtopics": [],
                    "evidence": {}, "lang": "", "quality": 0, "value": "低"}
        et, ef, ev = extra_tags(tax, hay, host, fname)
        topics, subs, tag_forms, ev_all = merge_tags(base, et, ef, ev, title, tlen)
        q = min(100, int(base.get("quality", 0)) + (10 if tlen > 2000 else 0))
        out.append({
            "path": p, "host": host, "url": url, "ext": ext, "size": size,
            "sha256": sha, "title": title, "text": text, "text_len": tlen,
            "pages": pages, "lang": base.get("lang") or _lang(text),
            "category": base.get("category", ""),
            "forms": json.dumps(tag_forms, ensure_ascii=False),
            "topics": json.dumps(topics, ensure_ascii=False),
            "subtopics": json.dumps(subs, ensure_ascii=False),
            "extra_topics": json.dumps(et, ensure_ascii=False),
            "evidence": json.dumps(ev_all, ensure_ascii=False)[:4000],
            "quality": q, "value": "高" if q >= 55 else ("中" if q >= 28 else "低"),
            "parse_status": status, "parse_err": perr, "parse_ms": pms,
            "scanned": scanned, "parse_ver": PARSE_VER,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        if len(out) >= 100:
            write_rows(c, out)
            out = []
    if out:
        write_rows(c, out)
    print("[retag] 完成，耗时 %.1fs" % (time.time() - t0), flush=True)
    return stats(c)


def backfill(c):
    """把 asset 标签按 host 回填到 kb_index.db 里 topics 为空的记录（只填空、不覆盖）。"""
    if not os.path.exists(LEGACY_DB):
        print("[backfill] kb_index.db 不存在，跳过")
        return
    host_topics = {}
    for h, t, s in c.execute("SELECT host,topics,subtopics FROM asset_docs WHERE topics NOT IN ('','[]')"):
        try:
            tp = json.loads(t or "[]")
        except Exception:
            continue
        if tp:
            host_topics.setdefault(h, set()).update(tp)
    if not host_topics:
        print("[backfill] 无可回填标签")
        return
    lc = sqlite3.connect(LEGACY_DB)
    lc.execute("PRAGMA journal_mode=WAL")
    n = 0
    for h, tps in host_topics.items():
        val = json.dumps(sorted(tps), ensure_ascii=False)
        cur = lc.execute("UPDATE docs SET topics=? WHERE domain=? AND (topics IS NULL OR topics IN ('','[]'))",
                         (val, h))
        n += cur.rowcount
    lc.commit()
    lc.close()
    print("[backfill] 回填 %d 条 docs 的空主题标签（来自 %d 个域名）" % (n, len(host_topics)), flush=True)


def stats(c):
    total = c.execute("SELECT COUNT(*) FROM asset_docs").fetchone()[0]
    print("\n=== asset_index.db 统计 ===")
    print("已解析文件 %d 条" % total)
    if not total:
        return
    for r in c.execute("SELECT parse_status,COUNT(*) FROM asset_docs GROUP BY parse_status ORDER BY 2 DESC"):
        print("  状态 %-10s %d" % r)
    print("  正文总字符 %s" % format(c.execute("SELECT COALESCE(SUM(text_len),0) FROM asset_docs").fetchone()[0], ","))
    print("  PDF 总页数 %s" % format(c.execute("SELECT COALESCE(SUM(pages),0) FROM asset_docs").fetchone()[0], ","))
    tp, fm, et, cat = Counter(), Counter(), Counter(), Counter()
    for t, f, e, ca in c.execute("SELECT topics,forms,extra_topics,category FROM asset_docs"):
        for col, ctr in ((t, tp), (f, fm), (e, et)):
            try:
                ctr.update(json.loads(col or "[]"))
            except Exception:
                pass
        cat[ca] += 1
    print("  主题 TOP12:", tp.most_common(12))
    print("  形态 TOP8:", fm.most_common(8))
    print("  扩展标签:", et.most_common())
    print("  类目 TOP8:", cat.most_common(8))
    print("  质量分 >=55（高价值）: %d" % c.execute("SELECT COUNT(*) FROM asset_docs WHERE quality>=55").fetchone()[0])
    print("  FTS 可检索: %d" % c.execute("SELECT COUNT(*) FROM asset_fts").fetchone()[0])


if __name__ == "__main__":
    main()
