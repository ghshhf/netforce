# -*- coding: utf-8 -*-
"""netforce / tagger_daemon.py —— 语义补标常驻循环（自我恢复）

单轮跑 100 条就退，单并发，轮间留缓冲：这样任何一次异常（模型重启、写库锁、
网络抖动）最多损失一轮里没写完的条目，下一轮按 uid 自动跳过已完成的重跑。
低占用优先——本机 16GB 内存还要同时承载 5GB 模型与其他常驻进程，
禁止把并发和单轮条数调大。候选空了就歇 5 分钟再查，不空转。

用法：python tagger_daemon.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import headless

HERE = os.path.dirname(os.path.abspath(__file__))
BATCH = os.path.join(HERE, "_kb", "semantic_local_batch.py")
LIMIT = "100"
WORKERS = "1"
IDLE_SLEEP = 300
ROUND_GAP = 20


def log(msg: str) -> None:
    print("%s [tagger-daemon] %s" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def main() -> None:
    log("启动，单轮 %s 条 / 并发 %s" % (LIMIT, WORKERS))
    while True:
        t0 = time.time()
        rc = -1
        try:
            rc = headless.run(
                [sys.executable, "-u", BATCH, "--workers", WORKERS, "--limit", LIMIT],
                cwd=HERE, timeout=3600).returncode
        except subprocess.TimeoutExpired:
            log("单轮超时，续跑")
        except Exception as e:
            log("单轮异常 %s，续跑" % str(e)[:160])
        el = time.time() - t0
        log("一轮结束 rc=%s 用时 %.1f 分" % (rc, el / 60))
        time.sleep(IDLE_SLEEP if el < 15 else ROUND_GAP)


if __name__ == "__main__":
    main()
