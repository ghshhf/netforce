# -*- coding: utf-8 -*-
"""netforce / fts_rebuild.py —— 全文索引按 uid 全量重建（幂等、分批、低内存）

为什么需要：corpus_fts 由 autoloop / jobboard / parse_all / finalize / kb_hygiene
五处各自维护，语义不统一（有的只 INSERT 不 DELETE，有的只 UPDATE topics），
长期累积出「有正文未入索引」与「正文已空仍留在索引」的漂移。

设计约束（11-12 主人：不叠 buff / 低占用优先）
- 分批小事务，每批 1000 行，绝不一次提交 7 万行撑爆 WAL
- 只插入有正文的行；无正文的旧索引一律清掉
- 幂等：任何时候中断，重跑即可，不会留下半套索引的错误状态
- 不改动其它五处写入者，保持单一职责

用法：
    python fts_rebuild.py             # 重建 + 深检
    python fts_rebuild.py --check     # 快检：只读 uid 两列，秒级
    python fts_rebuild.py --check --full  # 深检：含正文漂移比对，约 30 秒

11-12 主人：每加一点新知识就全量重跑一遍，收益与时间不对等。
根因定位：深检为了比 length(text) 会把 FTS 里 7 万行正文全读出来（约 30 秒 IO），
而日常只需要「有正文的 uid 是否都在索引里」。故默认快检只读 uid，
深检保留为 --full；重建仍走深检，正确性不打折。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "_kb", "corpus.db")
BATCH = 1000
COLS = "uid,host,title,topics,text"


def connect(readonly: bool = False) -> sqlite3.Connection:
    # 只读体检不许开可写连接、更不许执行 journal_mode=WAL（写 pragma）：
    # 实测后者在 2.8 GB 库上会触发 WAL checkpoint，把 0.8 秒的快检拖到 10.3 秒。
    if readonly:
        c = sqlite3.connect("file:" + CORPUS.replace("\\", "/") + "?mode=ro",
                            uri=True, timeout=120)
    else:
        c = sqlite3.connect(CORPUS, timeout=120)
        c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=120000")
    return c


def report_fast(c: sqlite3.Connection) -> tuple[int, int, int]:
    """只读 uid 两列做集合差集；不碰正文，不会把 7 万行 text 拉进内存。
    代价：查不出「uid 没变但正文被改写」的漂移，那种交给 --full。
    """
    t0 = time.time()
    fts_uids = {u for (u,) in c.execute("SELECT uid FROM corpus_fts")}
    corpus_uids = {u for (u,) in c.execute(
        "SELECT uid FROM corpus WHERE text IS NOT NULL AND text<>''")}
    missing = len(corpus_uids - fts_uids)
    stale = len(fts_uids - corpus_uids)
    print("[check] 快检 有正文 %d / FTS %d / 漏检 %d / 陈旧 %d（%.1fs；"
          "正文漂移请用 --check --full）"
          % (len(corpus_uids), len(fts_uids), missing, stale, time.time() - t0),
          flush=True)
    return missing, stale, 0


def report(c: sqlite3.Connection) -> tuple[int, int, int]:
    """⚠️ uid 是 FTS5 的 UNINDEXED 列，绝不能用 SQL 的 NOT EXISTS / JOIN 做差集：
    那会退化成 10 万 × 8 万 的全表嵌套扫描（实测卡死 20 分钟零输出）。
    这里改为流式取 uid 与长度摘要，在 Python 侧做 O(n) 集合运算。"""
    t0 = time.time()
    fts_meta = {u: (n or 0, t or "") for u, n, t in c.execute(
        "SELECT uid, length(text), topics FROM corpus_fts")}
    # 必须用 length(text) 而不是 text_len 字段：后者是历史冗余列，
    # 正文被清空/截断时有 52 条没同步，拿它比对会误报「索引漂移」。
    corpus_meta = {u: (n or 0, t or "") for u, n, t in c.execute(
        "SELECT uid, length(text), topics FROM corpus "
        "WHERE text IS NOT NULL AND text<>''")}
    fts_uids, corpus_uids = set(fts_meta), set(corpus_meta)
    missing = len(corpus_uids - fts_uids)
    stale = len(fts_uids - corpus_uids)
    shared = fts_uids & corpus_uids
    # 正文才是全文索引的核心，只有 uid 集合与正文不一致才算索引损坏。
    # topics 是补标高频写入的派生标签，FTS 里的副本允许滞后：
    # 它没有 UNINDEXED 加速，逐条 UPDATE 会全表扫 7 万行，得不偿失。
    # 标签检索以站点分片为准（site_gen.py 直接读 corpus.topics）。
    drift = sum(1 for u in shared if fts_meta[u][0] != corpus_meta[u][0])
    topic_lag = sum(1 for u in shared if fts_meta[u][1] != corpus_meta[u][1])
    print("[check] 有正文 %d / FTS %d / 漏检 %d / 陈旧 %d / 正文漂移 %d "
          "/ 标签副本滞后 %d（信息项）（%.1fs）"
          % (len(corpus_uids), len(fts_uids), missing, stale, drift,
             topic_lag, time.time() - t0), flush=True)
    return missing, stale, drift


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只读校验")
    ap.add_argument("--full", action="store_true",
                    help="深检：读正文比对漂移（约 30 秒）；重建时自动启用")
    ap.add_argument("--batch", type=int, default=BATCH)
    args = ap.parse_args()

    c = connect(readonly=args.check)
    if args.check and not args.full:
        report_fast(c)
        return 0
    missing, stale, drift = report(c)
    if args.check:
        return 0
    if missing == 0 and stale == 0 and drift == 0:
        print("索引已一致，无需重建。", flush=True)
        return 0

    t0 = time.time()
    print("[rebuild] 清空旧索引 …", flush=True)
    c.execute("DELETE FROM corpus_fts")
    c.commit()

    total = c.execute(
        "SELECT COUNT(*) FROM corpus WHERE text IS NOT NULL AND text<>''").fetchone()[0]
    print("[rebuild] 待写入 %d 条，每批 %d 条" % (total, args.batch), flush=True)

    # 用 rowid 游标分批读取，避免一次性把 7 万行正文读进内存
    last = 0
    done = 0
    sql_ins = "INSERT INTO corpus_fts(%s) VALUES(?,?,?,?,?)" % COLS
    while True:
        rows = c.execute(
            "SELECT rowid,uid,host,title,topics,text FROM corpus "
            "WHERE rowid>? AND text IS NOT NULL AND text<>'' "
            "ORDER BY rowid LIMIT ?", (last, args.batch)).fetchall()
        if not rows:
            break
        last = rows[-1][0]
        payload = [(r[1], r[2] or "", r[3] or "", r[4] or "", r[5] or "") for r in rows]
        c.executemany(sql_ins, payload)
        c.commit()
        done += len(rows)
        if done % 10000 < args.batch:
            print("  已写 %d / %d（%.0f 秒）" % (done, total, time.time() - t0), flush=True)

    print("[rebuild] 完成 %d 条，耗时 %.1f 秒" % (done, time.time() - t0), flush=True)
    m, s, d = report(c)
    if m or s or d:
        print("[warn] 校验仍有差异：漏检 %d / 陈旧 %d / 漂移 %d" % (m, s, d), flush=True)
        return 1
    print("[ok] 索引与正文完全一致。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
