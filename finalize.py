# -*- coding: utf-8 -*-
"""netforce / finalize.py —— 全量导入结束后的自动收尾

由 bulk_lock_watch.py 在导入进程退出后自动调用，也可手动跑：
    python finalize.py

步骤：
    1. 全文索引瘦身：清掉早期版本误插入的短文本 URL 记录
    2. 全库重打标签：应用最新 tags_ext.json（23 主题 / 4 形态 / 14 域名直判）
       与「优先用基础类目兜底」的修正，压掉大面积「未命中」
    3. 回填 kb_index.db：用 records 完整正文补齐空标题 / 空 snippet / 空主题
    4. 零遗留验收 + 生成 _kb/PARSE_REPORT.md
    5. 解除 BULK_IMPORT 锁，autoloop 下一轮自动恢复全部写动作
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(HERE, "_kb")
EV = os.path.join(KB, "evolve")
DB = os.path.join(KB, "corpus.db")
LOCK = os.path.join(EV, "BULK_IMPORT")
REPORT = os.path.join(KB, "PARSE_REPORT.md")
LOG = os.path.join(EV, "finalize.log")
sys.path.insert(0, HERE)


def say(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(script, args, timeout=1800):
    cmd = [sys.executable, os.path.join(HERE, script)] + args
    try:
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        say("  %s %s rc=%d" % (script, " ".join(args), r.returncode))
        return (r.stdout or "")[-4000:]
    except Exception as e:
        say("  %s %s 失败：%s" % (script, " ".join(args), str(e)[:160]))
        return ""


def fts_slim():
    c = sqlite3.connect(DB, timeout=120)
    c.execute("PRAGMA busy_timeout=120000")
    try:
        n = c.execute("SELECT COUNT(*) FROM corpus_fts WHERE uid IN "
                      "(SELECT uid FROM corpus WHERE text_len<=200)").fetchone()[0]
        if n:
            c.execute("DELETE FROM corpus_fts WHERE uid IN "
                      "(SELECT uid FROM corpus WHERE text_len<=200)")
            c.commit()
        say("  索引瘦身：清除 %d 条无正文记录的冗余 FTS 行" % n)
    except Exception as e:
        say("  索引瘦身跳过：%s" % str(e)[:120])
    finally:
        c.close()


def snapshot():
    c = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    q = lambda s: c.execute(s).fetchone()[0]
    d = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": q("SELECT COUNT(*) FROM corpus"),
        "chars": q("SELECT COALESCE(SUM(text_len),0) FROM corpus"),
        "with_text": q("SELECT COUNT(*) FROM corpus WHERE text_len>200"),
        "fts": q("SELECT COUNT(*) FROM corpus_fts"),
        "empty_topics": q("SELECT COUNT(*) FROM corpus WHERE topics IN ('','[]')"),
        "fallback": q("SELECT COUNT(*) FROM corpus WHERE topics LIKE '%未命中/待语义复核%'"),
        "pending": q("SELECT COUNT(*) FROM corpus WHERE parse_status='pending_fetch'"),
        "sources": c.execute("SELECT source,COUNT(*) FROM corpus GROUP BY 1 "
                             "ORDER BY 2 DESC").fetchall(),
        "status": c.execute("SELECT parse_status,COUNT(*) FROM corpus GROUP BY 1 "
                            "ORDER BY 2 DESC").fetchall(),
    }
    tp = Counter()
    for (t,) in c.execute("SELECT topics FROM corpus"):
        try:
            tp.update(json.loads(t or "[]"))
        except Exception:
            pass
    d["topics"] = tp.most_common(25)
    try:
        d["ocr"] = c.execute("SELECT COUNT(*) FROM corpus_digest").fetchone()[0]
    except Exception:
        d["ocr"] = 0
    c.close()
    return d


def write_report(d, ocr_stat, loop_stat):
    lines = [
        "# netforce 本地语料全量解析报告", "",
        "> 本文件由 `finalize.py` 自动生成，最新一次：%s。" % d["ts"],
        "> 数据源 = 本机爬虫攒下的全部产物；远程只在注册表里，不联网抓取正文。", "",
        "## 一句话结论", "",
        "把 `_kb/` 与 netforce 根目录里**所有**爬虫产物统一解析、分类、建全文索引，",
        "并接入一个不依赖对话的自治循环持续消化剩余工作量。", "",
        "## 总览", "",
        "| 指标 | 值 |", "|---|---|",
        "| 统一语料条数 | %s |" % format(d["total"], ","),
        "| 入库正文字符 | %s |" % format(d["chars"], ","),
        "| 有正文记录 | %s |" % format(d["with_text"], ","),
        "| 全文可检索 | %s |" % format(d["fts"], ","),
        "| 空主题标签 | %s |" % format(d["empty_topics"], ","),
        "| 待语义复核 | %s（%.2f%%） |" %
        (format(d["fallback"], ","), 100.0 * d["fallback"] / max(1, d["total"])),
        "| 待抓取队列 | %s |" % format(d["pending"], ","),
        "", "## 覆盖了哪些源", "",
        "| 来源 | 条数 |", "|---|---|",
    ]
    for src, n in d["sources"][:25]:
        lines.append("| `%s` | %s |" % (src, format(n, ",")))
    lines += ["", "> 主要来源说明：",
              "> - `records/*`：`_kb/records` 里 444 MB 抓取记录，每条带完整网页正文（此前只进了截断 snippet）。",
              "> - `kb_index`：原主索引 4.8 万条，补齐标题/正文/空标签。",
              "> - `asset_docs`：`_kb/assets` 里 526 个已下载文件（含 OCR 补出的正文）。",
              "> - `root/*`：netforce 根目录 `resources.json` / `classified.json` / `_visited.db` /",
              ">   `_skynet.db` / `mobile/*.db` / 外移目录结果等。",
              "> - `urls/*`：`seeds` / `reclaim` / `assets` / `state.fed` / `discovered` 等登记表。",
              "> - `corpus_file`：`E:\\xmanbian\\corpus` 的 76 个训练语料文件（文件级登记，",
              ">   全文留在磁盘，由常驻循环分块渐进消化）。",
              "", "## 解析状态分布", "",
              "| 状态 | 条数 |", "|---|---|"]
    for st, n in d["status"]:
        lines.append("| %s | %s |" % (st, format(n, ",")))
    lines += ["", "## 主题分布 TOP25", "",
              "| 标签 | 条数 |", "|---|---|"]
    for t, n in d["topics"]:
        lines.append("| %s | %s |" % (t, format(n, ",")))
    lines += [
        "", "## 标签体系（可增量扩展）", "",
        "- 基础层 `fine_classify.tag()`：形态 / 主题 / 二级主题 / 语言 / 质量 / 价值。",
        "- 扩展层 `_kb/tags_ext.json`：23 个主题、4 个形态、14 条域名直判；",
        "  加标签只需往 JSON 加一条 `{\"name\":..., \"keywords\":[...]}`，",
        "  再跑 `python parse_all.py --retag`，不重读原始文件。",
        "- 长文标签过宽的问题用「命中证据打分取 Top-4」收敛，裁剪前原始标签存 `evidence._raw` 可回溯。",
        "- 纯 URL 记录用 host 画像继承 + 域名规则直判，不把无正文记录硬塞进语义分类。",
        "", "## 常驻循环（不依赖对话）", "",
        "| 进程 | 状态 |", "|---|---|",
        "| `autoloop.py` | %s |" % loop_stat,
        "| `autoloop_watchdog.py` | 与 autoloop 互保，谁挂了另一个拉活 |",
        "", "循环每轮自己找活：发现新数据→增量解析；指标变差→收紧/新增标签；",
        "有「未命中」→网关语义补标；待抓队列→限速补抓；扫描 PDF→渐进 OCR；",
        "训练语料→分块消化；出口成败→沉淀路由知识。",
        "详情 `_kb/evolve/EVOLVE_LOG.md`、`metrics.jsonl`、`ROUTE_HINT.md`。", "",
        "## 查询出口", "",
        "```bash",
        "python kb_search.py \"数字主权\"",
        "python kb_search.py --tag 哲学/人文理论 --limit 20",
        "python kb_search.py --host arxiv.org --url",
        "python kb_search.py --stats",
        "```", "",
        "## 扫描件 OCR", "",
        ocr_stat, "",
        "## 边界（明确没纳入的）", "",
        "- `E:\\xmanbian` 下 `books` / `kb_data` / `kb_cards` / `industry_research` 等目录属于",
        "  其他项目，不是 netforce 爬虫产物，未吞并。",
        "- `assets.db` 里登记但从未下载成功的 URL 不作为「已解析资料」，",
        "  由常驻循环按 `pending_fetch` 队列限速补抓，抓回后自动解析入库。",
        "- 21 个音视频/压缩包不做正文解析，按元数据与文件名分类。",
        "", "## 复现 / 维护", "",
        "```bash",
        "python parse_all.py --all         # 全量（自动跳过已完成源）",
        "python parse_all.py --retag       # 改完标签词表后全库重打",
        "python parse_all.py --backfill    # 回填 kb_index.db 空字段",
        "python parse_all.py --verify      # 零遗留验收",
        "python ocr_scanned.py --stats     # OCR 进度",
        "python start_autoloop.py --status # 常驻循环状态",
        "```", "",
    ]
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    say("  报告已写入 %s" % REPORT)


def main():
    say("=== 开始自动收尾 ===")
    fts_slim()
    run("parse_all.py", ["--retag"])
    # 重打标签之后做维度归一与查重：站点属性拆出 topics、状态标签纠正、
    # 重复正文挂 canonical，并同步 FTS 的 topics 列。--force 因为此时锁还没解。
    run("kb_hygiene.py", ["--force"])
    run("parse_all.py", ["--backfill"])
    verify = run("parse_all.py", ["--verify"])
    d = snapshot()
    say("  最终：%s 条 / %s 字符 / 空标签 %d / 待语义复核 %d" %
        (format(d["total"], ","), format(d["chars"], ","),
         d["empty_topics"], d["fallback"]))
    ocr_stat = "扫描件 OCR 进度见 `python ocr_scanned.py --stats`；"
    try:
        ac = sqlite3.connect("file:%s?mode=ro" % os.path.join(KB, "asset_index.db").replace("\\", "/"), uri=True)
        n_ok = ac.execute("SELECT COUNT(*) FROM asset_docs WHERE parse_status='ok_ocr'").fetchone()[0]
        n_left = ac.execute("SELECT COUNT(*) FROM asset_docs WHERE parse_status='scanned'").fetchone()[0]
        ocr_stat += "已完成 %d 份，剩余 %d 份由常驻循环继续跑。" % (n_ok, n_left)
        ac.close()
    except Exception:
        ocr_stat += "统计暂不可用。"
    loop_stat = "运行中（心跳见 `_kb/evolve/heartbeat`）"
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, "start_autoloop.py"), "--status"],
                           cwd=HERE, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        loop_stat = (r.stdout or "").strip().replace("\n", "；")
    except Exception:
        pass
    write_report(d, ocr_stat, loop_stat)
    try:
        os.remove(LOCK)
        say("  已解除 BULK_IMPORT 锁，autoloop 下一轮恢复全部动作")
    except FileNotFoundError:
        pass
    except Exception as e:
        say("  解锁失败：%s" % str(e)[:120])
    with open(os.path.join(EV, "finalize_done.json"), "w", encoding="utf-8") as f:
        json.dump({"ts": d["ts"], "total": d["total"], "chars": d["chars"],
                   "empty_topics": d["empty_topics"], "fallback": d["fallback"],
                   "verify_tail": verify[-600:]}, f, ensure_ascii=False, indent=1)
    say("=== 自动收尾完成 ===")


if __name__ == "__main__":
    main()
