#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""netforce mobile · 基线同步

手机从零开始爬太慢，电脑上已经攒了十万实体。但整库 78MB 直接搬过去不明智 ——
里面有 6.6 万条 HN 评论，那些是计算 author 关系的中间产物，手机上根本用不到。

所以这里做的是「提炼后同步」：
  · 只带资源节点（repo / model / paper / story / series / video / domain …）
  · 只带资源之间的边
  · 评论、作者、hashtag 这类中间实体一律丢掉 —— 需要时手机上自己重新采

实测 103385 实体 / 78MB  →  提炼后约 1.5 万实体 / 6MB，手机完全吃得下。

用法
----
    python sync.py export                 # 生成 mobile/baseline.db
    python sync.py export --out /tmp/x.db  # 指定输出
    python sync.py report                 # 看提炼前后对比

导出的 baseline.db 拷到手机 mobile/ 目录，然后：
    python app.py --db baseline.db
或者直接覆盖 kb.db。
"""
import argparse
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.environ.get("NETFORCE_DB") or os.path.join(ROOT, "_kb", "seeds.db")

# 值得带走的：真正能当「资源」用的节点
KEEP_TYPES = (
    "repo", "owner", "model", "paper", "space", "dataset", "basekind",
    "story", "domain", "series", "work", "video", "music", "topic",
    "page", "site", "actor",
)
# 明确不要的：中间产物，体积大且没有独立价值
DROP_TYPES = ("comment", "hashtag", "author")

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities(
    id TEXT PRIMARY KEY, platform TEXT, type TEXT, title TEXT,
    url TEXT, depth TEXT, payload TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS edges(
    src TEXT, dst TEXT, rel TEXT, ts REAL,
    PRIMARY KEY(src, dst, rel));
"""


def cmd_export(a):
    if not os.path.exists(SRC):
        print("源库不存在：%s" % SRC)
        return 1
    out = a.out or os.path.join(HERE, "baseline.db")
    for p in (out, out + "-wal", out + "-shm"):
        if os.path.exists(p):
            os.remove(p)

    src = sqlite3.connect("file:%s?mode=ro" % SRC, uri=True, timeout=10)
    dst = sqlite3.connect(out)
    dst.executescript(SCHEMA)
    dst.execute("PRAGMA journal_mode=WAL")
    dst.execute("PRAGMA synchronous=OFF")

    ph = ",".join("?" * len(KEEP_TYPES))
    rows = src.execute(
        "SELECT id,platform,type,title,url,depth,payload,ts FROM entities "
        "WHERE type IN (%s)" % ph, KEEP_TYPES).fetchall()
    dst.executemany("INSERT OR IGNORE INTO entities VALUES(?,?,?,?,?,?,?,?)", rows)
    keep_ids = {r[0] for r in rows}
    print("  实体 %d 条（原库 %d）" % (
        len(rows), src.execute("SELECT count(*) FROM entities").fetchone()[0]))

    erows = []
    for s, d, r, ts in src.execute("SELECT src,dst,rel,ts FROM edges"):
        if s in keep_ids and d in keep_ids:
            erows.append((s, d, r, ts))
    dst.executemany("INSERT OR IGNORE INTO edges VALUES(?,?,?,?)", erows)
    print("  边   %d 条（原库 %d）" % (
        len(erows), src.execute("SELECT count(*) FROM edges").fetchone()[0]))

    dst.commit()
    dst.execute("VACUUM")
    dst.commit()
    dst.close()
    src.close()

    mb = os.path.getsize(out) / 1048576
    print("  → %s  %.1f MB" % (out, mb))
    print("\n拷到手机：")
    print("  adb push %s /sdcard/netforce/mobile/baseline.db" % out)
    return 0


def cmd_report(a):
    if not os.path.exists(SRC):
        print("源库不存在：%s" % SRC)
        return 1
    c = sqlite3.connect("file:%s?mode=ro" % SRC, uri=True)
    print("=" * 56)
    print("提炼预览：%s" % SRC)
    print("=" * 56)
    ph = ",".join("?" * len(KEEP_TYPES))
    keep_n = c.execute("SELECT count(*) FROM entities WHERE type IN (%s)" % ph,
                       KEEP_TYPES).fetchone()[0]
    all_n = c.execute("SELECT count(*) FROM entities").fetchone()[0]
    print("实体  保留 %6d / 共 %6d  (%.1f%%)" % (
        keep_n, all_n, 100.0 * keep_n / max(1, all_n)))
    print("\n--- 会被丢掉的 ---")
    for t, n in c.execute(
            "SELECT type,count(*) FROM entities WHERE type NOT IN (%s) "
            "GROUP BY 1 ORDER BY 2 DESC" % ph, KEEP_TYPES):
        print("  %-12s %6d" % (t, n))
    print("\n--- 会被保留的 ---")
    for t, n in c.execute(
            "SELECT type,count(*) FROM entities WHERE type IN (%s) "
            "GROUP BY 1 ORDER BY 2 DESC" % ph, KEEP_TYPES):
        print("  %-12s %6d" % (t, n))
    return 0


def main():
    ap = argparse.ArgumentParser(description="手机基线同步")
    sub = ap.add_subparsers(dest="cmd")
    e = sub.add_parser("export", help="生成 baseline.db")
    e.add_argument("--out", default="")
    r = sub.add_parser("report", help="看提炼前后对比")
    a = ap.parse_args()
    if a.cmd == "export":
        sys.exit(cmd_export(a))
    elif a.cmd == "report":
        sys.exit(cmd_report(a))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
