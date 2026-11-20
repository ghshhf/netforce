# -*- coding: utf-8 -*-
"""netforce / autoloop_watchdog.py —— 与 autoloop.py 互保的看门进程

职责极小：每 20 秒写一次自己的心跳；若 autoloop 的心跳超过 180 秒没动，
且 PID 已不存在，就重新拉起它。autoloop 每轮也会检查本进程心跳并反向拉起，
于是两个进程谁挂了，另一个都会把对方救回来 —— 会话结束仍继续跑。

不弹窗：pythonw + CREATE_NO_WINDOW + DETACHED_PROCESS + stdin=DEVNULL。
不主动停止：收到中断也继续。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
EV = os.path.join(HERE, "_kb", "evolve")
HB = os.path.join(EV, "heartbeat")
WHB = os.path.join(EV, "watchdog.heartbeat")
PIDFILE = os.path.join(EV, "evolve.pid")
TARGET = os.path.join(HERE, "autoloop.py")
LOGF = os.path.join(EV, "watchdog.log")
CHECK_EVERY = 20
STALE = 600          # 一轮可能跑 200s+，判定窗口必须大于「最长一轮 + 间隔」

os.makedirs(EV, exist_ok=True)


def say(msg):
    with open(LOGF, "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


LOOP_MUTEX = "Global\\netforce_autoloop_v1"
WD_MUTEX = "Global\\netforce_autoloop_watchdog_v1"
_MUTEX = {}


def single_instance():
    """误拉起的第二个 watchdog 自行退出。"""
    try:
        _MUTEX["wd"] = ctypes.windll.kernel32.CreateMutexW(None, False, WD_MUTEX)
        return ctypes.windll.kernel32.GetLastError() != 183
    except Exception:
        return True


def loop_running():
    """进程复核：autoloop 真在跑，就别因为一轮太久、心跳旧而重复拉起。"""
    try:
        h = ctypes.windll.kernel32.OpenMutexW(0x100000, False, LOOP_MUTEX)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def pid_alive(pid):
    if not pid:
        return False
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return False


def spawn():
    flags = 0
    for n in ("CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, n, 0)
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = os.path.join(os.path.dirname(exe), "pythonw.exe")
    p = subprocess.Popen([exe, TARGET], cwd=HERE, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, startupinfo=si)
    return p.pid


def main():
    if not single_instance():
        say("已有 watchdog 在跑，本进程自行退出 pid=%d" % os.getpid())
        return
    say("watchdog 启动 pid=%d" % os.getpid())
    while True:
        try:
            with open(WHB, "w", encoding="utf-8") as f:
                f.write(str(time.time()))
            age, pid = 9999, 0
            try:
                age = time.time() - float(open(HB, encoding="utf-8").read().split("|")[0])
            except Exception:
                pass
            try:
                pid = int(open(PIDFILE, encoding="utf-8").read().strip())
            except Exception:
                pass
            if age > STALE and not loop_running() and not pid_alive(pid):
                new = spawn()
                say("autoloop 心跳停了 %.0fs 且 pid=%s 已不在，已重新拉起 pid=%d" %
                    (age, pid, new))
            time.sleep(CHECK_EVERY)
        except KeyboardInterrupt:
            say("收到中断，按约定不退出")
            time.sleep(5)
        except Exception:
            say("异常：%s" % traceback.format_exc()[-600:])
            time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
