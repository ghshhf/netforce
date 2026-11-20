# -*- coding: utf-8 -*-
"""netforce / autotag_loop.py —— 持续打标 + 站点自更新驱动（无头、幂等、自恢复）

产品定位：netforce 站点的「自迭代引擎」。
- 打标：把语料持续喂给本地模型打开放标签，按 uid 幂等跳过已标。
- 自更新：累计新打标 ≥ BUILD_EVERY 条（或待标池清空）就重建一次静态站，
  让站点随数据推进自动反映新内容——这就是「产品自我迭代」的闭环。
- 模型挂即停（不自动重启，防 Vulkan 反复崩叠 buff）。
- 状态全在 corpus.db，进程被回收零损失，下次调起续跑。

单实例：pid 文件 + tasklist 校验，防止被重复拉起导致双写。
"""
from __future__ import annotations

import os
import sys
import time
import sqlite3
import subprocess
import atexit

HERE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(HERE, "_kb")
BATCH = os.path.join(KB, "semantic_local_batch.py")
BG = os.path.join(KB, "bg")
LLM = "http://127.0.0.1:8080"
BATCH_SIZE = 120
BUILD_EVERY = 3000   # 累计新打标这么多条就重建一次站点（产品自更新）
LOCK = os.path.join(BG, ".autotag.lock")
LOG = os.path.join(BG, "autotag_loop.log")
PY = sys.executable

sys.path.insert(0, HERE)
import headless  # noqa: E402  统一无窗口约定


def log(msg):
    line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line)
    print(line, end="", flush=True)


def pid_alive(pid):
    try:
        out = subprocess.run("tasklist /fi \"pid eq %d\" /nh" % pid,
                             shell=True, capture_output=True, text=True)
        return str(pid) in out.stdout
    except Exception:
        return False


def acquire_lock():
    os.makedirs(BG, exist_ok=True)
    if os.path.exists(LOCK):
        try:
            with open(LOCK) as f:
                pid = int((f.read() or "0").strip() or 0)
        except Exception:
            pid = 0
        if pid and pid_alive(pid):
            log("已有 autotag_loop 实例在跑（pid %d），本进程退出" % pid)
            sys.exit(0)
        try:
            os.remove(LOCK)
        except Exception:
            pass
    with open(LOCK, "w") as f:
        f.write(str(os.getpid()))

    def release():
        try:
            if os.path.exists(LOCK) and open(LOCK).read().strip() == str(os.getpid()):
                os.remove(LOCK)
        except Exception:
            pass
    atexit.register(release)


def health() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(LLM + "/health", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def pending() -> int:
    db = os.path.join(KB, "corpus.db")
    c = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=8)
    n = c.execute(
        "SELECT COUNT(*) FROM corpus WHERE text_len>400 AND "
        "(topics IS NULL OR topics='' OR topics LIKE '%未命中/待语义复核%') "
        "AND uid NOT IN (SELECT uid FROM semantic_tags)").fetchone()[0]
    c.close()
    return n


def tags_count() -> int:
    db = os.path.join(KB, "corpus.db")
    c = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=8)
    n = c.execute("SELECT COUNT(*) FROM semantic_tags").fetchone()[0]
    c.close()
    return n


def run_batch(n):
    try:
        r = headless.run([PY, "-u", BATCH, "--workers", "1", "--limit", str(n)],
                         cwd=HERE, capture_output=True, text=True, errors="replace",
                         timeout=1200)
        return r.returncode
    except subprocess.TimeoutExpired:
        log("单批超时（已落盘不丢），继续下一轮")
        return -1
    except Exception as e:
        log("单批异常 %s" % str(e)[:160])
        return -1


def site_build():
    """站点随打标推进周期性重建（产品自更新）。只读 corpus，写静态文件，
    与打标写库轻微争锁但 SQLite busy_timeout 能协调。"""
    log("开始站点重建（--limit 0 --docs 3000）")
    try:
        r = headless.run([PY, "-u", os.path.join(HERE, "site_gen.py"),
                          "--limit", "0", "--docs", "3000"],
                         cwd=HERE, capture_output=True, text=True, errors="replace",
                         timeout=1800)
        log("站点重建 rc=%s" % r.returncode)
        return r.returncode
    except Exception as e:
        log("站点重建异常（不影响数据）: %s" % str(e)[:160])
        return -1


def main():
    acquire_lock()
    log("=== autotag_loop 启动 (pid %d) ===" % os.getpid())
    since_build = 0
    rounds = 0
    while True:
        if not health():
            log("模型不在（Vulkan 崩/未拉起）→ 退出循环，不自动重启（防叠 buff）")
            break
        n = pending()
        if n <= 0:
            log("待标池已清空 → 触发站点重建")
            site_build()
            break
        rounds += 1
        take = min(BATCH_SIZE, n)
        before = tags_count()
        log("第 %d 轮：待标 %d，取 %d 条" % (rounds, n, take))
        rc = run_batch(take)
        after = tags_count()
        new = max(0, after - before)
        since_build += new
        log("第 %d 轮结束 rc=%s，本轮新增 %d，距重建 %d" % (rounds, rc, new, BUILD_EVERY - since_build))
        if since_build >= BUILD_EVERY:
            log("累计新标 %d ≥ %d → 重建站点（产品自更新）" % (since_build, BUILD_EVERY))
            site_build()
            since_build = 0
        time.sleep(2)
    log("=== autotag_loop 退出 ===")


if __name__ == "__main__":
    main()
