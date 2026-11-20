# -*- coding: utf-8 -*-
"""netforce / start_autoloop.py —— 一键拉起（或确认）自治常驻循环

不弹窗：pythonw + CREATE_NO_WINDOW + DETACHED_PROCESS + stdin=DEVNULL。
幂等：已在跑就只报告状态，不重复起进程。
两个进程互保：watchdog 拉 autoloop，autoloop 反向拉 watchdog。

用法：
    python start_autoloop.py          # 启动 / 报告状态
    python start_autoloop.py --status
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EV = os.path.join(HERE, "_kb", "evolve")
HB = os.path.join(EV, "heartbeat")
WHB = os.path.join(EV, "watchdog.heartbeat")
PIDFILE = os.path.join(EV, "evolve.pid")
WATCHDOG = os.path.join(HERE, "autoloop_watchdog.py")
AUTOLOOP = os.path.join(HERE, "autoloop.py")
os.makedirs(EV, exist_ok=True)


def alive(pid):
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


def age_of(path):
    try:
        return time.time() - float(open(path, encoding="utf-8").read().split("|")[0])
    except Exception:
        return 99999


def spawn(script):
    flags = 0
    for n in ("CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, n, 0)
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = os.path.join(os.path.dirname(exe), "pythonw.exe")
    p = subprocess.Popen([exe, script], cwd=HERE, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=flags, startupinfo=si)
    return p.pid


def main():
    pid = 0
    try:
        pid = int(open(PIDFILE, encoding="utf-8").read().strip())
    except Exception:
        pass
    # 以心跳为准：PID 文件可能在首轮未结束时还留着上一次进程的旧值
    force = "--force" in sys.argv      # 热更新代码后强制替换循环进程
    loop_ok = (not force) and (age_of(HB) < 300 or alive(pid))
    wd_ok = age_of(WHB) < 150
    if "--status" in sys.argv:
        print("autoloop: %s (pid=%s, 心跳 %.0fs)" %
              ("运行中" if loop_ok else "未运行", pid, age_of(HB)))
        print("watchdog: %s (心跳 %.0fs)" % ("运行中" if wd_ok else "未运行", age_of(WHB)))
        return
    if loop_ok and wd_ok:
        print("两个进程都在跑，无需启动。autoloop pid=%d" % pid)
        return
    if not wd_ok:
        w = spawn(WATCHDOG)
        print("已启动 watchdog pid=%d" % w)
    if not loop_ok:
        a = spawn(AUTOLOOP)
        print("已启动 autoloop pid=%d" % a)
    print("完成。状态可查：python start_autoloop.py --status")
    print("日志：_kb/evolve/EVOLVE_LOG.md、autoloop.stdout.log、watchdog.log")


if __name__ == "__main__":
    main()
