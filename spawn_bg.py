# -*- coding: utf-8 -*-
"""netforce / spawn_bg.py —— 脱离会话的后台启动器（幂等、不弹窗）

为什么要这个：用 `nohup ... &` 起的进程属于当前会话的进程组，会话轮次切换
或工具调用结束时会被连带终止；autoloop 用的是 pythonw + DETACHED_PROCESS，
所以能活下来。这里把同一套机制做成通用入口，服务和批处理一律走这里。

用法：
    python spawn_bg.py <标签> -- <命令> [参数...]
    python spawn_bg.py <标签> --status

日志固定写 _kb/bg/<标签>.log，PID 写 _kb/bg/<标签>.pid。
重复调用时先按标签查活进程，活着就只报告，不再拉第二份。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import headless

HERE = os.path.dirname(os.path.abspath(__file__))
BG = os.path.join(HERE, "_kb", "bg")
os.makedirs(BG, exist_ok=True)


# PID 会被系统复用：只查「这个数字还活着」会误判成服务在跑，
# 结果真的服务永远起不来。按标签核对命令行身份才算判活。
TAG_KEYS = {
    "llm": "llama-server.exe",
    "tagger": "tagger_daemon.py",
    "ocr": "ocr_daemon.py",
    "jobboard": "jobboard_worker.py",
    "autoloop": "autoloop.py",
}


def alive(tag: str) -> int:
    pidfile = os.path.join(BG, tag + ".pid")
    try:
        pid = int(open(pidfile, encoding="utf-8").read().strip())
    except Exception:
        return 0
    out = headless.run(
        ["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
        capture_output=True, text=True, errors="ignore").stdout or ""
    if str(pid) not in out:
        return 0
    key = TAG_KEYS.get(tag)
    if not key:
        return pid
    cmd = headless.run(
        ["wmic", "process", "where", "ProcessId=%d" % pid, "get", "CommandLine"],
        capture_output=True, text=True, errors="ignore").stdout or ""
    return pid if key.lower() in cmd.lower() else 0


def spawn(tag: str, argv: list[str]) -> int:
    log = open(os.path.join(BG, tag + ".log"), "ab", buffering=0)
    # 日志是追加的，没有分隔行就会把上一轮的历史报错误读成当前故障
    log.write(("\n===== %s 启动 %s | %s =====\n" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), tag,
        " ".join(argv)[:180])).encode("utf-8", "replace"))
    p = headless.popen(
        argv, detached=True, cwd=HERE, stdin=subprocess.DEVNULL,
        stdout=log, stderr=log, close_fds=True)
    with open(os.path.join(BG, tag + ".pid"), "w", encoding="utf-8") as f:
        f.write(str(p.pid))
    return p.pid


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("用法: python spawn_bg.py <标签> -- <命令> [参数...]")
        return
    tag = args[0]
    if len(args) > 1 and args[1] == "--status":
        pid = alive(tag)
        print("%s: %s" % (tag, ("存活 pid=%d" % pid) if pid else "未运行"))
        return
    if "--" not in args:
        print("缺少 -- 分隔符")
        return
    argv = args[args.index("--") + 1:]
    pid = alive(tag)
    if pid:
        print("%s: 已在跑 pid=%d，不重复启动" % (tag, pid))
        return
    pid = spawn(tag, argv)
    print("%s: 已启动 pid=%d  日志 _kb/bg/%s.log" % (tag, pid, tag))


if __name__ == "__main__":
    main()
