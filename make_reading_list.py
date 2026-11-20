# -*- coding: utf-8 -*-
"""从 asset_index.db 挑一份「今晚可以翻的东西」清单。

按 质量分 + 稀有主题加权 + 篇幅 排序；摘录取原文开头，不编造简介。
用法：python make_reading_list.py [收录条数]
"""
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "_kb", "asset_index.db")
OUT = os.path.join(HERE, "_kb", "READING_LIST.md")
RARE = {"哲学/人文理论", "艺术/媒介研究", "社会学/人类学", "科幻/幻想文学",
        "播客/有声书", "图书馆/档案", "天文/物理/材料", "生物/医学",
        "语言/翻译", "学术/出版"}

n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
c = sqlite3.connect(DB)
rows = []
for host, title, path, tlen, pages, quality, topics, extra, text in c.execute(
        "SELECT host,title,path,text_len,pages,quality,topics,extra_topics,text "
        "FROM asset_docs WHERE text_len>4000 AND quality>=50"):
    try:
        tp = json.loads(topics or "[]")
        ex = json.loads(extra or "[]")
    except Exception:
        tp, ex = [], []
    tags = list(dict.fromkeys(tp + ex))
    rare = sum(1 for t in tags if t in RARE)
    # 标题像代码/日志/模板导出的一律降权，别让它们占掉「值得翻」的位置
    looks_like_doc = not re.match(
        r"^(#|Microsoft Word -|manifest$|book$|units database|\{|\<)", (title or "").strip(), re.I)
    score = quality + rare * 6 + min(20, tlen // 5000) + (25 if looks_like_doc else -40)
    snip = re.sub(r"\s+", " ", (text or "")[:260]).strip()
    rows.append((score, host, title, path, tlen, pages, quality, tags, [], snip))
rows.sort(reverse=True)

out = ["# 今晚可以翻的东西", "",
       "> 从刚解析完的 526 份本地资料里，按「质量分 + 稀有主题 + 篇幅」挑出的 %d 份。" % min(n, len(rows)),
       "> 摘录是原文开头，不是我写的简介；文件路径可直接打开。",
       "> 生成 2026-11-11 · 数据源 `_kb/asset_index.db`", ""]
for i, (sc, h, t, p, tl, pg, q, tp, ex, snip) in enumerate(rows[:n], 1):
    out.append("## %d. %s" % (i, (t or h)[:90]))
    out.append("")
    out.append("- 来源：`%s`" % h)
    out.append("- 文件：`%s`" % p)
    out.append("- 规模：%s 字符 / %s 页 ｜ 质量分 %d" % (format(tl, ","), pg, q))
    out.append("- 标签：%s" % ("、".join(tp + ex) if (tp + ex) else "（规则未命中，待复核）"))
    out.append("")
    out.append("> %s" % snip[:260])
    out.append("")
with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(out))
c.close()
print("生成 %s，候选 %d 份，收录 %d 份" % (OUT, len(rows), min(n, len(rows))))
for r in rows[:8]:
    print("  %-28s | %-50s | %s chars | %s"
          % (r[1][:28], r[2][:50], r[4], "、".join(r[7] + r[8])[:48]))
