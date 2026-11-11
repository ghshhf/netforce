# -*- coding: utf-8 -*-
"""netforce / headless.py —— 后台干活的默认无窗口约定

窗口是给人看的成品，不是给过程看的。项目里任何 subprocess 调用一律走这里：
CREATE_NO_WINDOW + STARTF_USESHOWWINDOW/SW_HIDE；浏览器一律 --headless=new。

⚠️ STARTUPINFO 必须每次新建：Python 3.7+ 会改写传入对象的内部状态，
   复用同一个实例第二次就失效（10-21 踩过）。
"""
from __future__ import annotations

import subprocess

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DETACHED = NO_WINDOW
for _n in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
    DETACHED |= getattr(subprocess, _n, 0)


def startupinfo():
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def kw(detached=False):
    """给 subprocess.run/Popen 的关键字参数补上无窗口设置。"""
    return {"startupinfo": startupinfo(),
            "creationflags": DETACHED if detached else NO_WINDOW}


def run(argv, **kwargs):
    """同步调用：不弹窗、可 capture_output。"""
    base = kw()
    base.update(kwargs)
    base["creationflags"] |= NO_WINDOW
    return subprocess.run(argv, **base)


def popen(argv, detached=False, **kwargs):
    """后台调用：默认脱离会话，stdout/stderr 由调用方重定向到日志。"""
    base = kw(detached)
    base.update(kwargs)
    base["creationflags"] |= NO_WINDOW
    return subprocess.Popen(argv, **base)


CHROME_HEADLESS = ["--headless=new", "--disable-gpu", "--no-first-run",
                   "--no-default-browser-check", "--disable-extensions"]
