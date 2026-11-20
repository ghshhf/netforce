# -*- coding: utf-8 -*-
"""主库体检归一：标签维度拆分 + 状态标签纠正 + 重复正文标记。

为什么要单独一层：
  1. TLD 规则曾把「中文站点/德文站点」这类**站点属性**写进 topics，
     和真正的内容主题混在一个字段里，主题统计因此失真。
  2. 纯 URL 记录明明是「还没抓」，却顶着「未命中/待语义复核」，
     把「待抓取」和「分类失败」两种完全不同的状态混成一个数。
  3. 同一份正文被多个来源重复登记，检索会吐重复结果。

本脚本只改**标签与标记列**，不动 text、不删任何行、不联网；
原始标签在 evidence 里留档，可随时 --retag 重算。
设计成幂等：重复跑不会重复计数、不会反复写盘。

用法：
    python kb_hygiene.py --dry-run   # 只看问题，不写盘
    python kb_hygiene.py              # 导入锁未解除时自动跳过
    python kb_hygiene.py --force      # 忽略锁（供 finalize 收尾调用）
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(HERE, "_kb")
DB = os.path.join(KB, "corpus.db")
LOCK = os.path.join(KB, "evolve", "BULK_IMPORT")
REPORT = os.path.join(KB, "HYGIENE_REPORT.md")

SITE_ATTRS = {
    "中文站点", "日文站点", "德文站点", "法文站点", "俄文站点", "英文站点",
    "政府/公共", "政府/公共机构", "组织/非营利", "国际组织",
    "高校/实验室", "学术/科研", "个人博客/研究者主页",
}
UNKNOWN = "未命中/待语义复核"
GENERIC_CATS = {"其他/待定", "未识别", UNKNOWN, "待抓取/不可达", "访问失败（保留待复查）"}


def log(s):
    print("[hygiene] %s" % s, flush=True)


def jload(s, default):
    try:
        v = json.loads(s or "")
        return v if isinstance(v, list) else default
    except Exception:
        return default


def ensure_columns(c):
    have = {r[1] for r in c.execute("PRAGMA table_info(corpus)")}
    for name, ddl in (("site_attrs", "TEXT DEFAULT ''"),
                      ("dup_uid", "TEXT DEFAULT ''")):
        if name not in have:
            c.execute("ALTER TABLE corpus ADD COLUMN %s %s" % (name, ddl))
            log("新增列 %s" % name)
    c.commit()


def normalize(c, dry=False):
    """把站点属性从 topics 拆到 site_attrs；纠正状态标签。"""
    rows = c.execute(
        "SELECT uid,topics,site_attrs,category,parse_status FROM corpus").fetchall()
    updates, moved, status_fixed, emptied = [], Counter(), 0, 0
    for uid, tp, old_site, cat, st in rows:
        tags = jload(tp, [])
        site = set(jload(old_site, []))
        keep = []
        for t in tags:
            if not isinstance(t, str):
                continue
            if t in SITE_ATTRS:
                site.add(t)
                moved[t] += 1
            else:
                keep.append(t)
        if st in ("pending_fetch", "unreachable") and UNKNOWN in keep:
            keep = [t for t in keep if t != UNKNOWN] + ["待抓取/不可达"]
            status_fixed += 1
        if not keep:
            if cat and cat not in GENERIC_CATS:
                keep = [cat]
            else:
                keep = [UNKNOWN]
            emptied += 1
        new_tp = json.dumps(list(dict.fromkeys(keep)), ensure_ascii=False)
        new_site = json.dumps(sorted(site), ensure_ascii=False) if site else ""
        if new_tp != (tp or "") or new_site != (old_site or ""):
            updates.append((new_tp, new_site, uid))
    log("标签归一：%d 行需更新｜剥离站点属性 %d 次｜状态纠正 %d｜空标签兜底 %d"
        % (len(updates), sum(moved.values()), status_fixed, emptied))
    if not dry and updates:
        c.executemany("UPDATE corpus SET topics=?,site_attrs=? WHERE uid=?", updates)
        c.commit()
    return {"changed": len(updates), "moved": moved.most_common(12),
            "status_fixed": status_fixed, "emptied": emptied}


def mark_dupes(c, dry=False):
    """同一份正文只保留一条 canonical，其余记 dup_uid（不删行）。"""
    rows = c.execute(
        "SELECT uid,substr(text,1,160) FROM corpus "
        "WHERE text_len>200 ORDER BY uid").fetchall()
    groups = {}
    for uid, head in rows:
        if head and len(head) > 80:
            groups.setdefault(head, []).append(uid)
    updates, affected = [], 0
    for head, uids in groups.items():
        if len(uids) < 2:
            continue
        canonical = uids[0]
        affected += len(uids) - 1
        updates.extend((canonical, u) for u in uids[1:])
    log("重复标记：%d 组重复正文，%d 条挂到 canonical" % (
        sum(1 for v in groups.values() if len(v) > 1), affected))
    if not dry and updates:
        c.executemany("UPDATE corpus SET dup_uid=? WHERE uid=?", updates)
        c.commit()
    return {"groups": sum(1 for v in groups.values() if len(v) > 1),
            "dupes": affected}


def sync_fts_topics(c, dry=False):
    """主表 topics 改了，FTS 的 topics 列要跟上，否则标签检索仍旧。"""
    if dry:
        return 0
    try:
        t0 = time.time()
        c.execute("UPDATE corpus_fts SET topics=COALESCE("
                  "(SELECT topics FROM corpus WHERE corpus.uid=corpus_fts.uid),topics)")
        c.commit()
        log("FTS topics 同步完成 %.1fs" % (time.time() - t0))
        return 1
    except Exception as e:
        log("FTS topics 同步跳过：%s" % str(e)[:140])
        return 0


def topic_top(c, n=15):
    cnt = Counter()
    for (tp,) in c.execute("SELECT topics FROM corpus"):
        cnt.update(t for t in jload(tp, []) if isinstance(t, str))
    return cnt.most_common(n)


def write_report(d):
    lines = [
        "# 主库体检归一报告", "",
        "> `kb_hygiene.py` 自动生成 · %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "> 只改标签与标记列，不删行、不动正文、不联网；可重复运行。", "",
        "## 结果", "",
        "| 项 | 数 |", "|---|---|",
        "| 标签更新行 | %d |" % d["norm"]["changed"],
        "| 剥离站点属性次数 | %d |" % sum(v for _, v in d["norm"]["moved"]),
        "| 状态标签纠正 | %d |" % d["norm"]["status_fixed"],
        "| 空标签兜底 | %d |" % d["norm"]["emptied"],
        "| 重复正文组 | %d |" % d["dup"]["groups"],
        "| 标记为重复的条数 | %d |" % d["dup"]["dupes"],
        "| FTS topics 同步 | %s |" % ("完成" if d["fts"] else "跳过"), "",
        "## 剥离最多的站点属性", "",
    ]
    for k, v in d["norm"]["moved"]:
        lines.append("- %s：%d" % (k, v))
    lines += ["", "## 归一后主题 TOP 15", ""]
    for k, v in d["topics"]:
        lines.append("- %s：%d" % (k, v))
    lines += ["", "## 查重用法", "",
              "`SELECT * FROM corpus WHERE dup_uid='' AND text_len>200` "
              "只看正文不重复的记录。", ""]
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log("报告 → %s" % REPORT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略 BULK_IMPORT 锁")
    a = ap.parse_args()
    if os.path.exists(LOCK) and not a.force:
        log("全量导入锁仍在，跳过（收尾时会自动调用）")
        return 0
    if not os.path.isfile(DB):
        log("主库不存在，跳过")
        return 0
    c = sqlite3.connect(DB, timeout=180)
    c.execute("PRAGMA busy_timeout=180000")
    ensure_columns(c)
    d = {
        "norm": normalize(c, a.dry_run),
        "dup": mark_dupes(c, a.dry_run),
        "fts": 0 if a.dry_run else sync_fts_topics(c),
        "topics": topic_top(c),
    }
    if not a.dry_run:
        write_report(d)
    c.close()
    log("完成%s" % ("（dry-run，未写盘）" if a.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
