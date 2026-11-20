# -*- coding: utf-8 -*-
"""netforce / kb_search.py —— 统一语料库的查询出口

解析完的数据在 _kb/corpus.db，没有查询入口等于没解析。这个脚本提供：
  * 全文检索（中文 trigram / 英文词，均可）
  * 按标签、来源、host、解析状态过滤
  * 输出命中摘要与可回溯的文件路径 / URL

用法：
    python kb_search.py "数字主权"
    python kb_search.py "quantization" --limit 20
    python kb_search.py --tag 哲学/人文理论 --limit 15
    python kb_search.py --host arxiv.org --limit 10
    python kb_search.py --source "records/%" --status ok
    python kb_search.py --stats
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "_kb", "corpus.db")


def jload(s):
    try:
        return json.loads(s or "[]")
    except Exception:
        return []


def conn():
    if not os.path.exists(DB):
        sys.exit("corpus.db 不存在，请先跑：python parse_all.py --all")
    return sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)


def stats(c):
    print("=== corpus.db ===")
    print("  总条数     %s" % format(c.execute("SELECT COUNT(*) FROM corpus").fetchone()[0], ","))
    print("  正文字符   %s" % format(
        c.execute("SELECT COALESCE(SUM(text_len),0) FROM corpus").fetchone()[0], ","))
    print("  FTS 可检索 %s" % format(c.execute("SELECT COUNT(*) FROM corpus_fts").fetchone()[0], ","))
    print("  空主题     %s" % c.execute(
        "SELECT COUNT(*) FROM corpus WHERE topics IN ('','[]')").fetchone()[0])
    print("\n=== 来源 TOP15 ===")
    for r in c.execute("SELECT source,COUNT(*) FROM corpus GROUP BY 1 ORDER BY 2 DESC LIMIT 15"):
        print("  %-36s %s" % (r[0], format(r[1], ",")))
    print("\n=== 主题 TOP20 ===")
    from collections import Counter
    ct = Counter()
    for (t,) in c.execute("SELECT topics FROM corpus"):
        ct.update(jload(t))
    for k, v in ct.most_common(20):
        print("  %-26s %s" % (k, format(v, ",")))
    print("\n=== 解析状态 ===")
    for r in c.execute("SELECT parse_status,COUNT(*) FROM corpus GROUP BY 1 ORDER BY 2 DESC"):
        print("  %-18s %s" % (r[0], format(r[1], ",")))


def search(c, q, a):
    # 纯全文查询统一走 kb_query（与 MCP 同一套逻辑）：多词自动放宽、
    # 2 字中文词正文回退、bm25 排序、空页过滤。带过滤条件的查询仍走下面
    # 的精确 SQL，保留 --tag/--host/--source/--status 的组合能力。
    if q and not (a.tag or a.host or a.source or a.status or a.min_quality):
        import kb_query
        print(kb_query.search({"query": q, "limit": a.limit, "scope": "all"}))
        return
    where, args = [], []
    if q:
        where.append("f.uid IN (SELECT uid FROM corpus_fts WHERE corpus_fts MATCH ?)")
        args.append(q)
    if a.tag:
        where.append("d.topics LIKE ?")
        args.append("%" + a.tag + "%")
    if a.host:
        where.append("d.host LIKE ?")
        args.append("%" + a.host + "%")
    if a.source:
        where.append("d.source LIKE ?")
        args.append(a.source)
    if a.status:
        where.append("d.parse_status=?")
        args.append(a.status)
    if a.min_quality:
        where.append("d.quality>=?")
        args.append(a.min_quality)
    # 直接用 corpus 主表 + 可选 FTS 子查询，避免虚表 JOIN 带来的行列对齐问题
    parts = []
    if q:
        parts.append("uid IN (SELECT uid FROM corpus_fts WHERE corpus_fts MATCH ?)")
    if a.tag:
        parts.append("topics LIKE ?")
    if a.host:
        parts.append("host LIKE ?")
    if a.source:
        parts.append("source LIKE ?")
    if a.status:
        parts.append("parse_status=?")
    if a.min_quality:
        parts.append("quality>=?")
    where_sql = (" WHERE " + " AND ".join(parts)) if parts else ""
    order = "quality DESC, text_len DESC" if q else "text_len DESC"
    sql = ("SELECT uid,host,title,topics,category,text_len,parse_status,url,meta,"
           "substr(text,1,260),quality FROM corpus%s ORDER BY %s LIMIT ?" %
           (where_sql, order))
    args.append(a.limit)
    try:
        rows = c.execute(sql, args).fetchall()
    except sqlite3.OperationalError as e:
        print("查询失败：%s" % e)
        print("提示：中文词组直接用双引号包起来可能触发 FTS 语法错误，试试不加引号的短词。")
        return
    print("命中 %d 条（查询：%s）\n" % (len(rows), q or " / ".join(
        x for x in [a.tag and "标签=" + a.tag, a.host and "域名=" + a.host,
                    a.source and "来源=" + a.source, a.status and "状态=" + a.status] if x)))
    for i, (uid, host, title, tp, cat, tlen, status, url, meta, snip, qv) in enumerate(rows, 1):
        print("%2d. %s" % (i, (title or host or "(无标题)")[:92]))
        print("    %s ｜ %s ｜ %s 字符 ｜ 质量 %s ｜ %s" %
              (host or "-", cat or "-", format(tlen or 0, ","), qv, status))
        print("    标签：%s" % "、".join(jload(tp))[:150])
        if snip:
            print("    摘录：%s" % " ".join(snip.split())[:180])
        if a.full or a.path:
            try:
                m = json.loads(meta or "{}")
                p = m.get("path") or m.get("full_path")
                if p:
                    print("    文件：%s" % p)
            except Exception:
                pass
        if a.url:
            print("    URL：%s" % (url or "")[:200])
        print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default="")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--tag")
    ap.add_argument("--host")
    ap.add_argument("--source")
    ap.add_argument("--status")
    ap.add_argument("--min-quality", type=int, default=0)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--full", action="store_true", help="显示原始文件路径")
    ap.add_argument("--url", action="store_true", help="显示 URL")
    ap.add_argument("--path", action="store_true")
    a = ap.parse_args()
    c = conn()
    if a.stats:
        return stats(c)
    return search(c, a.query.strip(), a)


if __name__ == "__main__":
    main()
