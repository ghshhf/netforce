# -*- coding: utf-8 -*-
"""netforce / fleet_tick.py —— 一次推进（不常驻、幂等、无头）

为什么不是守护进程：本环境下由工具调用拉起的后台进程会在会话边界被整棵
进程树回收；而全部状态（corpus.db / records / site / semantic_tags）都在盘上，
补标按 uid 幂等跳过。所以自恢复的是「状态」不是「进程」——每轮调用一次 tick，
服务不在就拉起、补标推进一批、标签副本对齐、打印摘要；进程死了零损失。

用法：
    python fleet_tick.py --status      只读快照
    python fleet_tick.py              体检 + 拉服务 + 推进一批(默认 40 条)
    python fleet_tick.py --batch 100  指定本轮条数
    python fleet_tick.py --site       全量重建静态站
    python fleet_tick.py --check      全文索引体检
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "_kb", "corpus.db")
BG = os.path.join(HERE, "_kb", "bg")
LLM = "http://127.0.0.1:8080"
MODEL = r"F:/models/gemma-4-E4B_q4_0-it.gguf"
SERVER = r"D:/tools/llamacpp-b12284/llama-server.exe"
# 官方 llama-fit-params 给出的固定参数，不叠 --fit on / --cpu-moe / -np
LLM_ARGV = [SERVER, "-m", MODEL, "-c", "16384", "-ngl", "-1",
            "--host", "127.0.0.1", "--port", "8080", "--jinja"]
BATCH = os.path.join(HERE, "_kb", "semantic_local_batch.py")

sys.path.insert(0, HERE)
import headless  # noqa: E402  统一无窗口约定


def db():
    c = sqlite3.connect(CORPUS, timeout=60)
    c.execute("PRAGMA busy_timeout=60000")
    return c


def health() -> bool:
    try:
        with urllib.request.urlopen(LLM + "/health", timeout=4) as r:
            return b"ok" in r.read(200).lower() or r.status == 200
    except Exception:
        return False


def ensure_llm() -> tuple[bool, str]:
    if health():
        return True, "服务已在跑"
    if not os.path.isfile(MODEL) or not os.path.isfile(SERVER):
        return False, "模型或引擎缺失，跳过补标"
    subprocess.run([sys.executable, os.path.join(HERE, "spawn_bg.py"),
                    "llm", "--"] + LLM_ARGV, cwd=HERE, capture_output=True,
                   **headless.kw())
    for _ in range(24):          # 最多等 60 秒（模型加载约 35 秒）
        time.sleep(2.5)
        if health():
            return True, "服务已拉起"
    return False, "服务拉起失败，本轮跳过补标"


def snapshot() -> dict:
    c = db()
    q = lambda s: c.execute(s).fetchone()[0]
    has_fail = q("SELECT COUNT(*) FROM sqlite_master WHERE name='semantic_fail'")
    d = {
        "corpus": q("SELECT COUNT(*) FROM corpus"),
        "withtext": q("SELECT COUNT(*) FROM corpus WHERE text IS NOT NULL AND text<>''"),
        "fts": q("SELECT COUNT(*) FROM corpus_fts"),
        "tags": q("SELECT COUNT(*) FROM semantic_tags"),
        "fail": q("SELECT COUNT(*) FROM semantic_fail") if has_fail else 0,
        "truncated": q("SELECT COUNT(*) FROM corpus WHERE text_len=20000"),
        "llm": "ok" if health() else "down",
    }
    c.close()
    return d


def sync_fts_topics() -> int:
    """把 FTS 的 topics 副本对齐主表。一次 IN 只扫一遍虚表，不做逐条子查询。

    topics 是 FTS5 的索引列，副本滞后会让「按新标签词 MATCH」搜不到；
    而五处写入者各自维护、语义不一，所以放在每轮收尾统一对齐。
    """
    c = db()
    f = {u: (t or "") for u, t in c.execute("SELECT uid,topics FROM corpus_fts")}
    k = {u: (t or "") for u, t in c.execute(
        "SELECT uid,topics FROM corpus WHERE text IS NOT NULL AND text<>''")}
    diff = [u for u in (set(f) & set(k)) if f[u] != k[u]]
    if not diff:
        c.close()
        return 0
    total = 0
    for i in range(0, len(diff), 500):
        chunk = diff[i:i + 500]
        ph = ",".join("?" * len(chunk))
        for attempt in range(6):
            try:
                c.execute("BEGIN IMMEDIATE")
                c.execute(
                    "UPDATE corpus_fts SET topics=COALESCE("
                    "(SELECT topics FROM corpus WHERE corpus.uid=corpus_fts.uid),topics) "
                    "WHERE uid IN (%s)" % ph, chunk)
                c.commit()
                total += len(chunk)
                break
            except sqlite3.OperationalError as e:
                c.rollback()
                if "locked" not in str(e).lower() or attempt == 5:
                    raise
                time.sleep(1.5)
    c.close()
    return total


def visual_check() -> str:
    """无头浏览器自检站点。本机 Chrome 的 --dump-dom 在 CREATE_NO_WINDOW 下
    恒返回空 stdout，--screenshot 也必须 Popen + 轮询文件（Chrome 主进程把
    渲染交给子进程后自己先退，同步 run 返回时截图还没落盘）。
    """
    chrome = r"C:/Program Files/Google/Chrome/Application/chrome.exe"
    site = Path(os.path.join(HERE, "_kb", "site", "index.html"))
    if not os.path.isfile(chrome) or not site.is_file():
        return "Chrome 或站点不存在，跳过视觉自检"
    out = os.path.join(BG, "site_check.png")
    for mode in ("--headless=new", "--headless"):
        prof = tempfile.mkdtemp(prefix="nf_vis_")
        shot = os.path.join(prof, "s.png")
        try:
            p = headless.popen(
                [chrome, mode, "--disable-gpu", "--no-first-run", "--no-sandbox",
                 "--user-data-dir=" + prof, "--window-size=1280,900",
                 "--virtual-time-budget=9000", "--screenshot=" + shot, site.as_uri()])
            for _ in range(20):
                time.sleep(1)
                if os.path.exists(shot) and os.path.getsize(shot) > 1000:
                    break
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()
            if os.path.exists(shot) and os.path.getsize(shot) > 1000:
                shutil.copy(shot, out)
                return "视觉自检 OK：%s（%d 字节，%s）" % (
                    out, os.path.getsize(out), mode)
        except Exception as e:
            print("[visual] %s 失败 %s" % (mode, str(e)[:100]), flush=True)
        finally:
            shutil.rmtree(prof, ignore_errors=True)
    return "视觉自检失败（两种 headless 模式都没出图）"


def run_batch(n: int) -> str:
    log = os.path.join(BG, "tick_batch.log")
    with open(log, "ab", buffering=0) as f:
        f.write(("\n===== %s tick %d 条 =====\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), n)).encode("utf-8"))
        try:
            r = headless.run(
                [sys.executable, "-u", BATCH, "--workers", "1", "--limit", str(n)],
                cwd=HERE, stdout=f, stderr=subprocess.STDOUT, timeout=900)
            return "补标 rc=%d" % r.returncode
        except subprocess.TimeoutExpired:
            return "补标本轮超时（已落盘的条目不丢）"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--batch", type=int, default=40)
    ap.add_argument("--site", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--visual", action="store_true")
    a = ap.parse_args()

    if a.status:
        for k, v in snapshot().items():
            print("%-12s %s" % (k, v))
        return

    if a.check:
        # CREATE_NO_WINDOW 下子进程 stdout 会丢（新控制台无窗口），
        # 必须走管道捕获再自己打印，否则 --check 看起来像什么都没干。
        r = headless.run([sys.executable, "-u", os.path.join(HERE, "fts_rebuild.py"),
                          "--check"], cwd=HERE, capture_output=True,
                         text=True, errors="replace")
        print((r.stdout or "").strip(), flush=True)
        if r.returncode:
            print((r.stderr or "").strip()[-500:], flush=True)
        return

    if a.visual:
        print(visual_check(), flush=True)
        return

    if a.site:
        r = headless.run([sys.executable, "-u", os.path.join(HERE, "site_gen.py"),
                          "--limit", "0"], cwd=HERE, capture_output=True,
                         text=True, errors="replace")
        print((r.stdout or "")[-600:])
        return

    t0 = time.time()
    before = snapshot()
    print("[tick] 起手 %s" % json.dumps(before, ensure_ascii=False), flush=True)

    ok, msg = ensure_llm()
    print("[tick] %s" % msg, flush=True)
    if ok:
        print("[tick] %s" % run_batch(a.batch), flush=True)
    try:
        n = sync_fts_topics()
        print("[tick] FTS 标签副本对齐 %d 条" % n, flush=True)
    except Exception as e:
        print("[tick] 标签副本对齐失败（不影响数据）: %s" % str(e)[:120], flush=True)

    after = snapshot()
    print("[tick] 收尾 %s" % json.dumps(after, ensure_ascii=False), flush=True)
    print("[tick] 本轮新增标签 %d 条 / 耗时 %.1f 分" %
          (after["tags"] - before["tags"], (time.time() - t0) / 60), flush=True)


if __name__ == "__main__":
    main()
