# -*- coding: utf-8 -*-
"""netforce / bulk_lock_watch.py —— 导入锁的自动解除哨兵

全量导入（parse_all.py --all）期间，autoloop 会因为 BULK_IMPORT 文件存在而
暂停写动作。这个哨兵盯着导入进程：它一结束就删掉锁文件，autoloop 下一轮
自动恢复全部工作 —— 不依赖当前对话还开着，也不依赖有人记得手动删。

用法：
    python bulk_lock_watch.py --spawn <导入进程PID>   # 无窗口后台启动哨兵
    python bulk_lock_watch.py <导入进程PID>            # 前台运行（调试）
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
LOCK = os.path.join(EV, "BULK_IMPORT")
LOG = os.path.join(EV, "bulk_watch.log")
MAX_WAIT = 3 * 3600


def say(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


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


def release(reason):
    say("导入结束（%s），先跑自动收尾 finalize.py" % reason)
    exe = sys.executable.replace("pythonw.exe", "python.exe")
    try:
        r = subprocess.run([exe, os.path.join(HERE, "finalize.py")], cwd=HERE,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=2400,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        say("finalize rc=%d：%s" % (r.returncode, (r.stdout or "")[-300:].strip()))
    except Exception as e:
        say("finalize 未能执行：%s（锁仍会解除）" % str(e)[:200])
    try:
        os.remove(LOCK)
        say("已解除 BULK_IMPORT 锁")
    except FileNotFoundError:
        say("锁已不存在")
    except Exception as e:
        say("解除锁失败：%s" % str(e)[:160])


def spawn_detached(pid):
    flags = 0
    for n in ("CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, n, 0)
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = os.path.join(os.path.dirname(exe), "pythonw.exe")
    p = subprocess.Popen([exe, os.path.abspath(__file__), str(pid)],
                          cwd=HERE, stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=flags, startupinfo=si)
    return p.pid


def main():
    if "--spawn" in sys.argv:
        pid = int(sys.argv[-1])
        say("哨兵启动，监控导入进程 %d" % pid)
        print("哨兵 pid=%d，监控导入进程 %d" % (spawn_detached(pid), pid))
        return
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    say("开始监控 pid=%d（最长 %d 小时）" % (pid, MAX_WAIT // 3600))
    t0 = time.time()
    while time.time() - t0 < MAX_WAIT:
        if not alive(pid):
            release("导入进程 %s 已结束" % pid)
            return
        time.sleep(30)
    release("超过 %d 小时上限，兜底解除（避免锁永久残留）" % (MAX_WAIT // 3600))


if __name__ == "__main__":
    main()
