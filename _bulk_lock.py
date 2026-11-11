# -*- coding: utf-8 -*-
"""netforce / _bulk_lock.py —— 带属主、能自愈的全量导入锁

事故（2026-11-11 04:25）：BULK_IMPORT 以前只是一个手工写上去的纯文本标记，
既没有属主 PID，也没有生命周期。导入进程和它的哨兵被系统清掉之后，锁
永久残留 → jobboard.merge() 每轮被挡 → 旁路库 17,653 条成果一直进不了主库，
状态板却因为只看心跳时间戳而报「运行中」。

现在锁文件是 JSON，带属主 PID；任何读取方调用 locked() 时顺手判活：
属主进程已经不在了就自动解锁并写日志，不依赖哨兵还活着、不依赖有人手动删。
老的纯文本锁按「文件年龄 + 上限」兜底，兼容存量。

命令行：
    python _bulk_lock.py acquire "parse_all --all"   # 导入方创建锁
    python _bulk_lock.py status                       # 看锁状态
    python _bulk_lock.py release                     # 手动解锁
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
EV = os.path.join(HERE, "_kb", "evolve")
LOCK = os.path.join(EV, "BULK_IMPORT")
LOG = os.path.join(EV, "bulk_lock.log")
MAX_AGE = 3 * 3600          # 与 bulk_lock_watch.py 的兜底上限一致


def say(msg):
    try:
        os.makedirs(EV, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


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


def read():
    """返回锁信息 dict；文件不存在返回 None；兼容旧纯文本格式。"""
    try:
        raw = open(LOCK, encoding="utf-8").read().strip()
    except OSError:
        return None
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return d
    except (ValueError, TypeError):
        pass
    return {"tag": raw[:100], "legacy": True}


def acquire(tag="", pid=None):
    os.makedirs(EV, exist_ok=True)
    d = {"pid": int(pid or os.getpid()),
         "started": time.time(),
         "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         "tag": tag}
    with open(LOCK, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    say("加锁：pid=%d tag=%s" % (d["pid"], tag))
    return d


def release(reason="手动解锁"):
    try:
        os.remove(LOCK)
        say("解锁：%s" % reason)
        return True
    except FileNotFoundError:
        return False
    except Exception as e:
        say("解锁失败：%s" % str(e)[:160])
        return False


def locked(autoheal=True):
    """True = 确有导入在跑（写动作应顺延）；False = 可以写主库。

    每次被读都会判活，僵尸锁当场自愈。autoheal=False 用于纯诊断。
    """
    d = read()
    if d is None:
        return False
    try:
        age = time.time() - os.path.getmtime(LOCK)
    except OSError:
        return False
    pid = d.get("pid")
    if pid and alive(pid) and age < MAX_AGE:
        return True
    if pid:
        why = "属主进程 pid=%s 已不存在" % pid
    elif age >= MAX_AGE:
        why = "旧格式无属主，且已残留 %.1f 小时" % (age / 3600)
    else:
        # 旧格式、还年轻：无法确认真假，保守按有效锁处理，等哨兵/超时收拾
        return True
    if autoheal:
        release("僵尸锁自愈：%s（tag=%s）" % (why, d.get("tag", "")))
    return False


def status():
    """给状态面板用：(是否锁着, 人话说明)。"""
    d = read()
    if d is None:
        return False, "已解除"
    if locked(autoheal=False):
        return True, "进行中（pid=%s，%s）" % (d.get("pid", "?"), d.get("tag", ""))
    return False, "僵尸锁待清理（%s）" % (d.get("tag", "")[:40])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "acquire":
        print(acquire(sys.argv[2] if len(sys.argv) > 2 else ""))
    elif cmd == "release":
        print("已解除" if release() else "锁不存在")
    else:
        on, note = status()
        print("BULK_IMPORT: %s ｜ %s" % ("锁着" if on else "未锁", note))
