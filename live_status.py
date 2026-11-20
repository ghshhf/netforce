# -*- coding: utf-8 -*-
"""netforce / live_status.py —— 自刷新状态面板

由 jobboard_worker 每 10 轮重写一次 _kb/LIVE_STATUS.md。
打开就能看到所有后台线在干什么，不需要问、不需要记命令。

2026-11-11 修正「假绿」：只看心跳时间戳会把「进程已被系统清掉、但心跳
刚写过不久」误报成运行中。现在额外复核 autoloop 的 PID 是否真的在，
并且四条线全超时时直接在顶部打出告警，不再让人对着历史心跳发呆。
"""
from __future__ import annotations

import ctypes
import os
import sqlite3
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(HERE, "_kb")
EV = os.path.join(KB, "evolve")
OUT = os.path.join(KB, "LIVE_STATUS.md")


def ro(path):
    try:
        c = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro",
                             uri=True, timeout=5)
        return c
    except Exception:
        return None


def one(c, sql, default=0):
    try:
        return c.execute(sql).fetchone()[0]
    except Exception:
        return default


def age(path):
    try:
        return time.time() - float(open(path, encoding="utf-8").read().split("|")[0])
    except Exception:
        return 99999


def pid_alive(pid):
    if not pid:
        return False
    try:
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return False


def tail(path, n=8):
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().strip().split("\n")
        return lines[-n:]
    except Exception:
        return ["(暂无日志)"]


def main():
    L = []
    A = L.append
    A("# 天网·自治任务板 实时状态")
    A("")
    A("> 本文件由常驻进程自动刷新，不需要人工维护。")
    A("> 刷新时间：**%s**" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    A("")
    hb = age(os.path.join(EV, "jobboard.heartbeat"))
    ghb = age(os.path.join(EV, "jobboard.guard.heartbeat"))
    ahb = age(os.path.join(EV, "heartbeat"))
    whb = age(os.path.join(EV, "watchdog.heartbeat"))
    try:
        apid = int(open(os.path.join(EV, "evolve.pid"), encoding="utf-8").read().strip())
    except Exception:
        apid = 0
    # 心跳新鲜 + 进程真的在，才算活着
    jb_ok, gd_ok = hb < 180, ghb < 150
    al_ok, wd_ok = (ahb < 300 and pid_alive(apid)), whb < 150
    import _bulk_lock
    bulk, bulk_note = _bulk_lock.status()
    if not any((jb_ok, gd_ok, al_ok, wd_ok)):
        A("> ⚠️ **后台线全灭**：四条心跳全部超时或进程已退出，下面表格里的 ✅ 只是历史残留。")
        A("> 重新拉起：`python start_autoloop.py --force` 和 "
          "`python -c \"import jobboard_worker as w; w.spawn(w.WORKER); "
          "w.spawn(w.WORKER,'--guard')\"`")
        A("")
    A("## 后台进程（脱离对话运行，不弹窗）")
    A("")
    A("| 线 | 判活 | 在做什么 |")
    A("|---|---|---|")
    A("| 任务板 worker | %s | 扫盘→生成任务→限时消化→合并主库→再扫盘 |" %
      ("✅ 心跳 %.0fs" % hb if jb_ok else "❌ 无心跳"))
    A("| 任务板 guard | %s | 盯 worker，掉了拉起来 |" %
      ("✅ 心跳 %.0fs" % ghb if gd_ok else "❌ 无心跳"))
    A("| 自治循环 autoloop | %s | 增量解析 / 语义补标 / 自动扩标签 / 限速补抓 / 出口路由 |" %
      ("✅ 心跳 %.0fs" % ahb if al_ok else "❌ 心跳超时或 pid=%s 已退出" % apid))
    A("| 循环 watchdog | %s | 反向保活 |" %
      ("✅ 心跳 %.0fs" % whb if wd_ok else "❌ 无心跳"))
    # 4B 语义补标栈（独立于 autoloop，自带 llama-server 保活）
    s4_pid = 0
    try:
        s4_pid = int(open(os.path.join(KB, "logs", "stack_4b.pid"), encoding="utf-8").read().strip())
    except Exception:
        pass
    s4_lag = 99999
    try:
        s4_lag = time.time() - os.path.getmtime(os.path.join(KB, "logs", "stack_4b.log"))
    except Exception:
        pass
    s4_ok = s4_pid and pid_alive(s4_pid) and s4_lag < 1800
    A("| 4B 语义补标栈 | %s | 保住 127.0.0.1:8080 的 4B 服务，串行给「未命中/待复核」打主题标签 |" %
      ("✅ pid=%d" % s4_pid if s4_ok else "❌ 未运行"))
    A("| 全量导入 | %s | %s |" % ("🔒 锁着" if bulk else "✅ 未锁", bulk_note))
    A("")
    A("## 数据与进度")
    A("")
    A("| 库 | 条数 | 正文字符 |")
    A("|---|---|---|")
    c = ro(os.path.join(KB, "corpus.db"))
    if c:
        A("| `corpus.db` 统一主库 | %s | %s |" %
          (format(one(c, "SELECT COUNT(*) FROM corpus"), ","),
           format(one(c, "SELECT COALESCE(SUM(text_len),0) FROM corpus"), ",")))
        total = one(c, "SELECT COUNT(*) FROM corpus") or 1
        tagd = one(c, "SELECT COUNT(*) FROM semantic_tags")
        left = one(c, """SELECT COUNT(*) FROM corpus
           WHERE (category IN ('其他/待定','未命中/待语义复核') OR topics IS NULL OR topics=''
                  OR topics LIKE '%未命中/待语义复核%')
             AND text_len>400 AND source NOT IN ('corpus_chunk','corpus_file')
             AND uid NOT IN (SELECT uid FROM semantic_tags)
             AND uid NOT IN (SELECT uid FROM semantic_fail WHERE n>=3)""")
        A("| 语义主题补标 | 已标 %s（%.1f%%） | 队列剩余 %s |" %
          (format(tagd, ","), 100.0 * tagd / total, format(left, ",")))
        c.close()
    c = ro(os.path.join(KB, "side_corpus.db"))
    if c:
        A("| `side_corpus.db` 旁路成果 | %s | %s |" %
          (format(one(c, "SELECT COUNT(*) FROM side_corpus"), ","),
           format(one(c, "SELECT COALESCE(SUM(text_len),0) FROM side_corpus"), ",")))
        c.close()
    c = ro(os.path.join(KB, "jobboard.db"))
    if c:
        pend = one(c, "SELECT COUNT(*) FROM jobs WHERE status IN ('pending','doing','retry')")
        done = one(c, "SELECT COUNT(*) FROM jobs WHERE status='done'")
        mb = one(c, "SELECT COALESCE(SUM(size),0) FROM jobs WHERE status IN "
                 "('pending','doing','retry')") / 1048576.0
        A("| `jobboard.db` 任务队列 | 待办 %s / 已完 %s | 剩余 %.1f MB |" %
          (format(pend, ","), format(done, ","), mb))
        c.close()
    c = ro(os.path.join(KB, "asset_index.db"))
    if c:
        A("| 扫描件 OCR | 已 OCR %s / 仍待 %s | — |" %
          (one(c, "SELECT COUNT(*) FROM asset_docs WHERE parse_status='ok_ocr'"),
           one(c, "SELECT COUNT(*) FROM asset_docs WHERE parse_status='scanned'")))
        c.close()
    A("")
    A("## 最近动作")
    A("")
    A("```")
    for l in tail(os.path.join(EV, "jobboard.log"), 6):
        A(l)
    for l in tail(os.path.join(EV, "autoloop.stdout.log"), 4):
        A(l)
    A("```")
    A("")
    A("## 想查东西时")
    A("")
    A("```bash")
    A("cd /e/xmanbian/netforce")
    A("python kb_search.py \"关键词\" --limit 10      # 主库全文检索")
    A("python jobboard.py --stats                    # 任务板队列")
    A("python jobboard_worker.py --status            # 后台进程判活")
    A("python start_autoloop.py --status             # 自治循环判活")
    A("python ocr_scanned.py --stats                 # 扫描件 OCR 进度")
    A("```")
    A("")
    A("> 加新标签：改 `_kb/tags_ext.json` → `python parse_assets.py --retag`。")
    A("> 任务板会自动发现盘上新增文件并排进队列，不需要手工派活。")
    A("")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[live_status] → %s" % OUT)


if __name__ == "__main__":
    main()
