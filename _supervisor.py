# -*- coding: utf-8 -*-
"""netforce / _supervisor.py —— 后台线自愈（MCP 每次查询顺带触发）

问题背景：脱离会话的 pythonw 常驻进程会被系统清掉，只靠「心跳时间戳」
判断会报假绿（文件还在、进程已死）；而只看心跳又会在长任务里误判超时、
反复拉起重复进程。

这里做两件事：
  1. 用**命名互斥体**判活（进程真在，互斥体才在），不再被陈旧心跳骗；
  2. 缺哪条线就补哪条线，节流 120 秒，重复拉起由各脚本自身互斥体挡掉。

不 print 到 stdout —— 本模块会被 MCP stdio 服务 import，stdout 是协议通道。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
EV = os.path.join(HERE, "_kb", "evolve")
STAMP = os.path.join(EV, "supervisor.ts")
LOG = os.path.join(EV, "supervisor.log")
MIN_INTERVAL = 120

MUTEX = {
    "worker": "Global\\netforce_jobboard_worker_v1",
    "guard": "Global\\netforce_jobboard_guard_v1",
    "autoloop": "Global\\netforce_autoloop_v1",
    "watchdog": "Global\\netforce_autoloop_watchdog_v1",
}

LINES = [
    ("worker", "jobboard_worker.py", ()),
    ("guard", "jobboard_worker.py", ("--guard",)),
    ("autoloop", "autoloop.py", ()),
    ("watchdog", "autoloop_watchdog.py", ()),
]


def note(msg):
    try:
        os.makedirs(EV, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def mutex_held(name):
    """命名互斥体存在 = 有活进程持有它。只读探测，不创建。"""
    try:
        # SYNCHRONIZE = 0x100000；OpenMutexW 打不开就是不存在的
        h = ctypes.windll.kernel32.OpenMutexW(0x100000, False, MUTEX.get(name, name))
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def spawn(script, args=()):
    flags = 0
    for n in ("CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, n, 0)
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = os.path.join(os.path.dirname(exe), "pythonw.exe")
    p = subprocess.Popen([exe, os.path.join(HERE, script)] + list(args),
                         cwd=HERE, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, startupinfo=si)
    return p.pid


def ensure(force=False):
    """补上缺失的后台线，返回 [(线名, pid)]。节流内直接返回 None。"""
    now = time.time()
    if not force:
        try:
            if now - float(open(STAMP, encoding="utf-8").read().strip()) < MIN_INTERVAL:
                return None
        except Exception:
            pass
    try:
        with open(STAMP, "w", encoding="utf-8") as f:
            f.write(str(now))
    except Exception:
        pass
    acts = []
    for name, script, args in LINES:
        if mutex_held(name):
            continue                         # 进程真在，不补
        try:
            pid = spawn(script, args)
            acts.append((name, pid))
            note("自愈拉起 %-9s pid=%d" % (name, pid))
        except Exception as e:
            note("拉起 %s 失败：%s" % (name, str(e)[:160]))
    return acts


def report():
    """状态板用：进程复核（互斥体）比心跳文件可信。"""
    return {name: mutex_held(name) for name, _, _ in LINES}


if __name__ == "__main__":
    print("进程复核:", report())
    r = ensure(force="--force" in sys.argv)
    print("自愈动作:", r)
