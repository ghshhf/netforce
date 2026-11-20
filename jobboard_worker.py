# -*- coding: utf-8 -*-
"""netforce / jobboard_worker.py —— 自找活常驻进程（永不主动停止）

worker：扫盘 → 生成任务 → 限时消化 → 合并主库 → 再扫盘。队列空了就做
        自检与快照，绝不长时间空等。
guard ：只盯 worker 心跳，掉了就拉起来；worker 反向也拉 guard。
两进程互保，脱离当前会话运行，不弹窗。

    python jobboard_worker.py            # worker
    python jobboard_worker.py --guard    # guard
    python jobboard_worker.py --status   # 状态
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
EV = os.path.join(HERE, "_kb", "evolve")
os.makedirs(EV, exist_ok=True)
HB = os.path.join(EV, "jobboard.heartbeat")
GHB = os.path.join(EV, "jobboard.guard.heartbeat")
LOG = os.path.join(EV, "jobboard.log")
STATE = os.path.join(EV, "jobboard.state.json")
WORKER = os.path.abspath(__file__)
sys.path.insert(0, HERE)

RUN_SECONDS = 25
IDLE_SLEEP = 2
GUARD_STALE = 120
WORKER_STALE = 180


def say(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def beat(path, extra=""):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("%s|%s" % (str(time.time()), extra))
    except Exception:
        pass


def age(path):
    try:
        return time.time() - float(open(path, encoding="utf-8").read().split("|")[0])
    except Exception:
        return 99999


def spawn(script, arg=None):
    flags = 0
    for n in ("CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, n, 0)
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = os.path.join(os.path.dirname(exe), "pythonw.exe")
    cmd = [exe, script] + ([arg] if arg else [])
    p = subprocess.Popen(cmd, cwd=HERE, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, startupinfo=si)
    return p.pid


def alive(pid):
    try:
        if not pid:
            return False
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return False


def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {"round": 0, "chunks": 0, "files": 0, "merged": 0, "empty_rounds": 0}


def save_state(s):
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


MUTEX_NAME = {"worker": "Global\\netforce_jobboard_worker_v1",
              "guard": "Global\\netforce_jobboard_guard_v1",
              "autoloop": "Global\\netforce_autoloop_v1",
              "watchdog": "Global\\netforce_autoloop_watchdog_v1"}
_MUTEX = {}          # 必须留全局引用，句柄被 GC 关掉的话「进程判活」就失效


def single_instance(role="worker"):
    """Windows 命名互斥体：误拉起的第二个实例会自己退出，不重复消耗队列。"""
    try:
        _MUTEX[role] = ctypes.windll.kernel32.CreateMutexW(
            None, False, MUTEX_NAME.get(role, role))
        return ctypes.windll.kernel32.GetLastError() != 183   # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def mutex_held(role="worker"):
    """只读探测：互斥体在 = 那个进程真的活着（心跳文件会骗人，它不会）。"""
    try:
        h = ctypes.windll.kernel32.OpenMutexW(0x100000, False, MUTEX_NAME.get(role, role))
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def worker():
    import jobboard as jb
    if not single_instance("worker"):
        say("已有 worker 在跑，本进程自行退出 pid=%d" % os.getpid())
        return
    say("jobboard worker 常驻启动 pid=%d（永不主动停止）" % os.getpid())
    beat(HB, "start")
    st = load_state()
    while True:
        st["round"] = st.get("round", 0) + 1
        beat(HB, str(st["round"]))
        try:
            # 状态面板每 10 轮自刷新，主人打开就是最新的，不用问
            if st["round"] % 10 == 1:
                try:
                    import live_status
                    live_status.main()
                except Exception as e:
                    say("  状态面板刷新异常：%s" % str(e)[:160])
            # 支线：技能 / MCP 自盘点，每 30 轮自己给自己排一次
            if st["round"] % 30 == 1:
                try:
                    import tool_audit
                    tool_audit.main()
                    say("  技能/MCP 自盘点完成（只读，候选见 _kb/TOOL_AUDIT.md）")
                except Exception as e:
                    say("  技能/MCP 盘点异常：%s" % str(e)[:160])
                # 天网：从它自己的代码与文档里找问题，不用主人想方向
                try:
                    import skynet_audit
                    skynet_audit.main()
                    say("  天网体检完成（只读，结论见 _kb/SKYNET_AUDIT.md）")
                except Exception as e:
                    say("  天网体检异常：%s" % str(e)[:160])
            # 1) 自己找活：队列薄了或每 8 轮重扫一次盘
            c = jb.jobs_db()
            pend = c.execute("SELECT COUNT(*) FROM jobs WHERE status IN "
                             "('pending','doing','retry')").fetchone()[0]
            c.close()
            if pend < 25 or st["round"] % 8 == 1:
                jb.scan(verbose=False)
            # 2) 限时内持续取批干活：一批 60 个文件跑完就立刻取下一批，
            #    不因为「本批完成」提前收工，也不长时间空等。
            rt0 = time.time()
            n = 0
            while time.time() - rt0 < RUN_SECONDS:
                left = RUN_SECONDS - (time.time() - rt0)
                if left < 1:
                    break
                # 长批次里持续刷心跳：一轮可能跑 200s+，只在轮首写会被 guard 误判掉线
                beat(HB, str(st["round"]))
                k = jb.run(min(8, left), 500, verbose=False)
                n += k
                if k == 0:
                    break          # 队列真空，交给下面的自检分支
            st["chunks"] = st.get("chunks", 0) + n
            # 3) 导入锁解除后自动把旁路成果并进主库
            m = jb.merge(verbose=False, tick=lambda x: beat(HB, x))
            st["merged"] = st.get("merged", 0) + m
            # 4) 队列真空了也不摸鱼：做指标快照 + 触发一次增量自检
            if n == 0 and pend == 0:
                st["empty_rounds"] = st.get("empty_rounds", 0) + 1
                if st["empty_rounds"] % 10 == 1:
                    try:
                        jb.scan(verbose=False)
                        say("  队列暂空：重扫全盘找新活（第 %d 次自检）" % st["empty_rounds"])
                    except Exception:
                        pass
            else:
                st["empty_rounds"] = 0
            if st["round"] % 5 == 1 or n:
                say("  第 %d 轮：消化 %d 块 / 合并 %d 条 / 累计 %d 块"
                    % (st["round"], n, m, st["chunks"]))
            save_state(st)
            # 5) 反向保活 guard
            if age(GHB) > GUARD_STALE:
                p = spawn(WORKER, "--guard")
                say("  反向拉起 guard pid=%d" % p)
        except KeyboardInterrupt:
            say("  收到中断，按约定不退出，继续下一轮")
        except Exception:
            say("  本轮异常：\n%s" % traceback.format_exc()[-1200:])
        time.sleep(IDLE_SLEEP)


def guard():
    if not single_instance("guard"):
        say("已有 guard 在跑，本进程自行退出 pid=%d" % os.getpid())
        return
    say("jobboard guard 常驻启动 pid=%d" % os.getpid())
    beat(GHB, "start")
    born = time.time()
    while True:
        beat(GHB, "ok")
        # 启动宽限 + 进程复核：worker 真活着（持有互斥体）时，心跳再旧也不重复拉起
        if time.time() - born > 60 and age(HB) > WORKER_STALE \
                and not mutex_held("worker"):
            p = spawn(WORKER)
            say("  worker 心跳超时，已拉起 pid=%d" % p)
            beat(HB, "respawn")
        time.sleep(15)


def status():
    s = load_state()
    print("jobboard worker: %s (心跳 %.0fs)" %
          ("运行中" if age(HB) < WORKER_STALE else "未运行", age(HB)))
    print("jobboard guard : %s (心跳 %.0fs)" %
          ("运行中" if age(GHB) < GUARD_STALE else "未运行", age(GHB)))
    print("state:", json.dumps(s, ensure_ascii=False))


if __name__ == "__main__":
    if "--status" in sys.argv:
        status()
    elif "--guard" in sys.argv:
        guard()
    else:
        worker()
