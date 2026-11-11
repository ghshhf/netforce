# -*- coding: utf-8 -*-
"""netforce / kb_index.py —— 本地知识库全文索引（把「爬到了」变成「查得到」）

为什么要有这一层：
    定位是「本地知识库索引」。分片 jsonl 只是原始语料，
    没有索引就等于一堆散落的文本 —— 我（或其他 AI）搜索时依旧只会看到热门资源，
    冷门站照样找不到。这一层把完整解析结果灌进 SQLite FTS5，按关键词 +
    多维标签检索，并能导出成别的模型能直接读的紧凑格式。

设计要点：
    · 增量灌入：按 (文件, 行数) 记进度，records 分片越滚越大也只读新增行；
    · 中文可搜：FTS5 unicode61 会把连续汉字当成一个 token ⇒ 写索引和查询时
      都把 CJK 逐字切开（"数据集" → "数 据 集" 短语匹配），中英文混查都成立；
    · 多维过滤：形态 / 主题 / 语种 / 价值 / 质量分 / 可访问性，正好复用
      fine_classify.py 打好的标签，不重新分类；
    · 只增不删：失败/不可达的域也留在索引里（标记 access），避免下次反复重爬；
    · 三种消费方式：人看的表格、AI 读的 --json、直接塞进 prompt 的 --context。

用法：
    python kb_index.py --ingest            # 增量灌入（默认动作）
    python kb_index.py --retag             # 只重打标签：清已读行数→全量重灌（不删 docs）
    python kb_index.py --rebuild           # 全量重建（会清空 docs，慎用）
    python kb_index.py --stats
    python kb_index.py --search 数据集 --forms 数据集 --min-quality 40
    python kb_index.py --search "open source llm" --json
    python kb_index.py --search 语料 --context       # 输出可直接喂给模型的上下文
    python kb_index.py --export _kb/kb_export.jsonl
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(BASE, "_kb")
RECORDS = os.path.join(KB, "records")
DB = os.path.join(KB, "kb_index.db")

FTS_COLS = "domain, title, desc, tags, body"   # body 列序号 = 4（snippet 用）
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# 分类规则一变，历史文档必须自动重打标签（不必重爬、不必人工 --rebuild）
RULE_FILES = ("fine_classify.py", "classify.py")

# 2026-11-09 第一层升级新增的 8 个筛选轴：二级主题 / 命中证据 / 地域 /
# 判定依据 / 出口通道 / 顶级域 / 时效 / 正文密度。ensure() 与 upsert() 共用一份。
NEW_TAG_COLS = ("subtopics", "evidence", "region", "region_basis",
                "egress", "tld", "freshness", "text_density")


def rule_signature():
    out = []
    for n in RULE_FILES:
        p = os.path.join(BASE, n)
        try:
            out.append("%s:%d" % (n, int(os.path.getmtime(p))))
        except Exception:
            out.append(n + ":0")
    return "|".join(out)


def connect():
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def ensure(rebuild=False):
    os.makedirs(KB, exist_ok=True)
    c = connect()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS docs(
            domain TEXT UNIQUE, url TEXT, title TEXT, descr TEXT, snippet TEXT,
            category TEXT, forms TEXT, topics TEXT, exts TEXT,
            access TEXT, lang TEXT, quality INTEGER, value TEXT,
            ok INTEGER, channel TEXT, ts REAL, src_file TEXT, src_line INTEGER);
        CREATE INDEX IF NOT EXISTS ix_docs_quality ON docs(quality);
        CREATE INDEX IF NOT EXISTS ix_docs_value ON docs(value);
        CREATE TABLE IF NOT EXISTS ingested(file TEXT PRIMARY KEY, nlines INTEGER);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    """)
    try:
        c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5("
                  "domain, title, desc, tags, body, "
                  "tokenize='unicode61 remove_diacritics 2')")
    except sqlite3.OperationalError as e:
        raise RuntimeError("本机 SQLite 不支持 FTS5：%s" % e)
    # 2026-11-09 第一层升级：fine_classify 新增的 8 个维度要真正落库。
    # 用幂等 ALTER，不重建表、不删一条历史数据（--rebuild 会清空 docs，别在那儿加列）。
    for col in NEW_TAG_COLS:
        have = {r[1] for r in c.execute("PRAGMA table_info(docs)")}
        if col not in have:
            c.execute("ALTER TABLE docs ADD COLUMN %s TEXT DEFAULT ''" % col)
    if rebuild:
        c.execute("DELETE FROM docs_fts")
        c.execute("DELETE FROM docs")
        c.execute("DELETE FROM ingested")
    c.commit()
    c.close()


def expand(s):
    """CJK 逐字切开，英文/数字原样保留。索引与查询必须用同一个函数。"""
    if not s:
        return ""
    return "".join((" " + ch + " ") if _CJK.match(ch) else ch for ch in str(s))


def fts_query(q):
    """把自然语言查询变成 FTS5 MATCH 串：中文按短语、英文按词 AND。"""
    toks = [t.strip() for t in re.split(r"\s+", expand(q).strip()) if t.strip()]
    toks = [re.sub(r'["\*\(\):]', "", t)[:32] for t in toks][:24]
    toks = [t for t in toks if t]
    if not toks:
        return ""
    # 中文单字连着查用短语，避免「数」「据」各自命中一堆无关页
    if len(toks) > 1 and all(_CJK.match(t) and len(t) == 1 for t in toks):
        return '"%s"' % " ".join(toks)
    return " AND ".join('"%s"' % t for t in toks)


def _j(v):
    return json.dumps(v or [], ensure_ascii=False)


def upsert(c, rec, src_file, src_line):
    # 用**当前**分类规则重打标签：以后优化了 fine_classify，历史记录不用重爬
    # 也能立刻享受新标签（原始 jsonl 保持原样，不磨损原文）。
    try:
        import fine_classify
        tags = fine_classify.tag(rec)
    except Exception:
        tags = rec.get("tags") or {}
    domain = (rec.get("domain") or "").lower().strip()
    if not domain or domain == "?":
        return False
    title = (rec.get("title") or "").strip()[:300]
    desc = (rec.get("desc") or "").strip()[:600]
    text = (rec.get("text") or "")[:200000]
    snippet = text[:700]
    forms = tags.get("forms") or []
    topics = tags.get("topics") or []
    exts = tags.get("exts") or []
    subs = tags.get("subtopics") or []
    extra = (_j(subs), json.dumps(tags.get("evidence") or {}, ensure_ascii=False),
             str(tags.get("region") or ""), str(tags.get("region_basis") or ""),
             str(tags.get("egress") or ""), str(tags.get("tld") or ""),
             str(tags.get("freshness") or ""), str(tags.get("text_density") or 0))
    vals = (domain, rec.get("url") or "", title, desc, snippet,
            tags.get("category") or "", _j(forms), _j(topics), _j(exts),
            tags.get("access") or "", tags.get("lang") or "",
            int(tags.get("quality") or 0), tags.get("value") or "",
            1 if rec.get("ok") else 0, rec.get("channel") or "",
            float(rec.get("ts") or time.time()), src_file, src_line) + extra
    base_cols = ("domain", "url", "title", "descr", "snippet", "category",
                 "forms", "topics", "exts", "access", "lang", "quality",
                 "value", "ok", "channel", "ts", "src_file", "src_line")
    cols = base_cols + NEW_TAG_COLS
    sets = ",".join("%s=excluded.%s" % (x, x) for x in cols[1:])
    c.execute("INSERT INTO docs(%s) VALUES(%s) "
              "ON CONFLICT(domain) DO UPDATE SET %s"
              % (",".join(cols), ",".join("?" * len(cols)), sets), vals)
    rowid = c.execute("SELECT rowid FROM docs WHERE domain=?", (domain,)).fetchone()[0]
    body = expand(" ".join([
        domain, title, desc,
        " ".join(str(x) for x in (rec.get("headings") or [])[:25]),
        " ".join(forms), " ".join(topics), " ".join(str(x) for x in subs),
        text[:12000],
    ]))
    c.execute("DELETE FROM docs_fts WHERE rowid=?", (rowid,))
    # tags 列把二级主题 / 地域 / 出口也写进去 ⇒ --search 能直接检索新维度
    c.execute("INSERT INTO docs_fts(rowid,domain,title,desc,tags,body) "
              "VALUES(?,?,?,?,?,?)",
              (rowid, expand(domain), expand(title), expand(desc),
               expand(" ".join([str(x) for x in forms + topics + list(subs)]
                               + [extra[2], extra[4]])), body))
    return True


def check_rules(c, log_fn=None):
    """规则文件 mtime 变了 ⇒ 清空 ingested，让下一轮全量重灌并用新规则重打标签。

    只清「已读行数」，不动 docs / docs_fts / 原始 jsonl；upsert 会覆盖同域记录。
    """
    sig = rule_signature()
    row = c.execute("SELECT v FROM meta WHERE k='rule_sig'").fetchone()
    if row and row[0] != sig:
        n = c.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        c.execute("DELETE FROM ingested")
        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('rule_sig',?)", (sig,))
        c.commit()
        if log_fn:
            log_fn("  [index] 分类规则已更新 ⇒ 自动重打 %d 条历史标签（不用重爬）" % n)
    elif not row:
        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('rule_sig',?)", (sig,))
        c.commit()


def ingest(rebuild=False, log_fn=None):
    """增量把 records/*.jsonl 灌进索引。返回 (新增条数, 扫描文件数)。"""
    ensure(rebuild=rebuild)
    c = connect()
    check_rules(c, log_fn)
    ingested = dict(c.execute("SELECT file,nlines FROM ingested").fetchall())
    files = sorted(glob.glob(os.path.join(RECORDS, "*.jsonl")))
    added, bad = 0, 0
    for path in files:
        seen = ingested.get(path, 0)
        n = 0
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f):
                    n = i + 1
                    if i < seen:
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        bad += 1
                        continue
                    added += int(upsert(c, rec, os.path.basename(path), n))
            c.execute("INSERT OR REPLACE INTO ingested(file,nlines) VALUES(?,?)",
                      (path, n))
            c.commit()
        except Exception as e:
            if log_fn:
                log_fn("  [index] %s 灌入失败：%s" % (os.path.basename(path), str(e)[:80]))
    c.close()
    if log_fn:
        log_fn("  [index] 新增/更新 %d 条，坏行 %d，扫描 %d 个分片"
               % (added, bad, len(files)))
    return added, len(files)


def _filters(args):
    sql, par = [], []
    for key, col in (("forms", "forms"), ("topics", "topics"),
                     ("subtopics", "subtopics"), ("lang", "lang"),
                     ("value", "value"), ("access", "access"),
                     ("category", "category"), ("region", "region"),
                     ("egress", "egress"), ("tld", "tld"),
                     ("freshness", "freshness")):
        v = getattr(args, key, "") if hasattr(args, key) else ""
        if v:
            sql.append("d.%s LIKE ?" % col)
            par.append('%"' + v + '"%'
                       if col in ("forms", "topics", "subtopics", "exts")
                       else "%" + v + "%")
    if getattr(args, "min_quality", 0):
        sql.append("d.quality >= ?")
        par.append(int(args.min_quality))
    if getattr(args, "only_ok", False):
        sql.append("d.ok=1")
    return sql, par


def search(args):
    """返回精简结果列表（AI 可直接消费）。"""
    ensure()
    c = connect()
    where, par = _filters(args)
    q = fts_query(getattr(args, "query", "") or "")
    limit = int(getattr(args, "limit", 10) or 10)
    if q:
        sql = ("SELECT d.domain,d.url,d.title,d.descr,d.snippet,d.category,"
               "d.forms,d.topics,d.exts,d.access,d.lang,d.quality,d.value,"
               "d.subtopics,d.region,d.egress,d.freshness,"
               "snippet(docs_fts,4,'<b>','</b>','…',18) AS hit "
               "FROM docs_fts JOIN docs d ON d.rowid=docs_fts.rowid "
               "WHERE docs_fts MATCH ?")
        params = [q] + par
        if where:
            sql += " AND " + " AND ".join(where)
        sql += " ORDER BY bm25(docs_fts), d.quality DESC LIMIT ?"
    else:
        sql = ("SELECT d.domain,d.url,d.title,d.descr,d.snippet,d.category,"
               "d.forms,d.topics,d.exts,d.access,d.lang,d.quality,d.value,"
               "d.subtopics,d.region,d.egress,d.freshness,"
               "'' FROM docs d WHERE 1=1")
        params = list(par)
        if where:
            sql += " AND " + " AND ".join(where)
        sql += " ORDER BY d.quality DESC, d.ts DESC LIMIT ?"
    params.append(limit)
    try:
        rows = c.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        c.close()
        raise RuntimeError("检索失败（查询串：%s）：%s" % (q, e))
    c.close()
    keys = ["domain", "url", "title", "desc", "snippet", "category", "forms",
            "topics", "exts", "access", "lang", "quality", "value",
            "subtopics", "region", "egress", "freshness", "hit"]
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        for k in ("forms", "topics", "exts", "subtopics"):
            try:
                d[k] = json.loads(d[k] or "[]")
            except Exception:
                d[k] = []
        d["hit"] = re.sub(r"<[^>]+>", "", d["hit"] or "")
        out.append(d)
    return out


def stats():
    if not os.path.exists(DB):
        print("索引尚未建立（跑一次 --ingest）")
        return
    c = connect()
    total = c.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    ok = c.execute("SELECT COUNT(*) FROM docs WHERE ok=1").fetchone()[0]
    chars = c.execute("SELECT COALESCE(SUM(LENGTH(snippet)),0) FROM docs").fetchone()[0]
    fts = c.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0]
    ing = c.execute("SELECT COUNT(*),COALESCE(SUM(nlines),0) FROM ingested").fetchone()
    from collections import Counter
    forms = Counter()
    topics = Counter()
    for f, t in c.execute("SELECT forms,topics FROM docs"):
        try:
            forms.update(json.loads(f or "[]"))
        except Exception:
            pass
        try:
            topics.update(json.loads(t or "[]"))
        except Exception:
            pass
    c.close()
    print("知识库文档 %d 条 | 渲染成功 %d | 全文索引 %d | 摘要字符 %d"
          % (total, ok, fts, chars))
    print("已灌分片 %d 个 / %d 行" % (ing[0], ing[1]))
    print("形态 TOP:", forms.most_common(10))
    print("主题 TOP:", topics.most_common(10))
    c2 = connect()
    for col in NEW_TAG_COLS:
        try:
            n = c2.execute("SELECT COUNT(*) FROM docs WHERE \"%s\" IS NOT NULL "
                           "AND \"%s\" NOT IN ('','[]','{}','0','0.0')" % (col, col)).fetchone()[0]
            print("  新维度 %-14s 非空 %d" % (col, n))
        except sqlite3.OperationalError:
            print("  新维度 %-14s 尚未建列" % col)
    c2.close()


def retag(log_fn=None):
    """只重打标签：清「已读行数」⇒ 全量重灌，用当前规则覆盖同域记录。

    与 --rebuild 的区别：**不删 docs、不删 docs_fts、不动原始 jsonl**。
    规则或新增标签维度改完后跑一次即可让 4.7 万条历史记录吃上新标签。
    """
    ensure()
    c = connect()
    n = c.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    c.execute("DELETE FROM ingested")
    c.commit()
    c.close()
    if log_fn:
        log_fn("  [index] 重打标签：清已读行数，准备覆盖 %d 条历史记录（不删数据）" % n)
    return ingest(log_fn=log_fn)


def export(path):
    ensure()
    c = connect()
    rows = c.execute("SELECT domain,url,title,descr,snippet,category,forms,topics,"
                     "exts,access,lang,quality,value FROM docs "
                     "ORDER BY quality DESC").fetchall()
    c.close()
    keys = ["domain", "url", "title", "desc", "snippet", "category", "forms",
            "topics", "exts", "access", "lang", "quality", "value"]
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            d = dict(zip(keys, r))
            for k in ("forms", "topics", "exts"):
                try:
                    d[k] = json.loads(d[k] or "[]")
                except Exception:
                    d[k] = []
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print("导出 %d 条 → %s" % (len(rows), path))
    return len(rows)


def _print_results(rows, context=False, as_json=False):
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if context:
        for i, d in enumerate(rows, 1):
            print("[%d] %s — %s" % (i, d["title"] or d["domain"], d["url"]))
            print("    标签: %s / %s | %s | 质量 %s | %s"
                  % ("/".join(d["forms"]), "/".join(d["topics"]),
                     d["lang"], d["quality"], d["access"]))
            body = (d["hit"] or d["snippet"] or "")[:400].replace("\n", " ")
            if body:
                print("    摘要: %s" % body)
        return
    for i, d in enumerate(rows, 1):
        print("%2d. [%s] %-28s q=%-3s %s"
              % (i, d["value"] or "-", d["domain"][:28], d["quality"],
                 (d["title"] or "")[:52]))
        print("    %s | %s | %s" % (d["url"][:100],
                                    "/".join(d["topics"][:3]),
                                    "/".join(d["forms"][:2])))
        if d["hit"]:
            print("    …%s…" % d["hit"][:150])


class _Args:
    """供其他模块直接调 search()，不必拼 argparse。"""
    def __init__(self, **kw):
        self.query = ""
        self.forms = ""
        self.topics = ""
        self.lang = ""
        self.value = ""
        self.access = ""
        self.category = ""
        self.min_quality = 0
        self.only_ok = False
        self.limit = 10
        self.__dict__.update(kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--retag", action="store_true",
                    help="只重打标签：全量重灌但不删 docs / 原始 jsonl")
    for _k in ("subtopics", "region", "egress", "tld", "freshness"):
        ap.add_argument("--" + _k, default="")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--export", default="")
    ap.add_argument("--search", dest="query", default="")
    ap.add_argument("--forms", default="")
    ap.add_argument("--topics", default="")
    ap.add_argument("--lang", default="")
    ap.add_argument("--value", default="")
    ap.add_argument("--access", default="")
    ap.add_argument("--category", default="")
    ap.add_argument("--min-quality", type=int, default=0)
    ap.add_argument("--only-ok", action="store_true")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--context", action="store_true")
    a = ap.parse_args()

    if a.rebuild:
        n, f = ingest(rebuild=True)
        print("全量重建完成：%d 条 / %d 分片" % (n, f))
        stats()
        return
    if a.retag:
        n, f = retag(log_fn=print)
        print("重打标签完成：覆盖 %d 条 / %d 分片" % (n, f))
        stats()
        return
    if a.stats:
        stats()
        return
    if a.export:
        export(a.export)
        return
    if any((a.query, a.forms, a.topics, a.min_quality, a.access, a.category,
            a.lang, a.value, a.subtopics, a.region, a.egress, a.tld,
            a.freshness, a.only_ok)):
        rows = search(a)
        _print_results(rows, context=a.context, as_json=a.json)
        if not a.json and not a.context:
            print("\n命中 %d 条（--context 输出可直接喂给模型）" % len(rows))
        return
    n, f = ingest(log_fn=print)
    print("增量灌入 %d 条 / %d 分片" % (n, f))
    stats()


if __name__ == "__main__":
    main()
