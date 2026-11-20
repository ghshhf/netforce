# -*- coding: utf-8 -*-
"""netforce / browser_pick.py —— 无头浏览器「启动实测择优」（共享给三个抓取入口）

为什么必须有这一层（2026-11-04 实测）：
    jsprobe / render_parse / resource_finder 都把系统 Chrome 写在候选第一位，
    而当天系统 Chrome **启动后立刻自行退出**（Playwright 日志：
    `<launched> pid=…` → `<gracefully close start>` → `exitCode=0`），
    同一时刻 Edge 149 与 Playwright 自带 Chromium 都正常起得来、能打开 example.com。
    ⇒ 静态的候选顺序靠不住，浏览器一升级/一被占用就全批崩。

做法：**真起一次**再决定，结果缓存到 `_kb/browser_ok.json`（TTL 1 天）。
    · 候选顺序：Playwright 自带 → Edge → 系统 Chrome → 显式 ms-playwright 路径
    · 成功判据：launch + 打开 about:blank（不依赖外网，2 秒级）
    · 返回 "" 表示「用 Playwright 默认 chromium」，上层不要传 executable_path
    · 全部失败也不抛异常，返回第一个存在的路径让上层照旧降级

复用范围：render_parse.py / jsprobe.py / resource_finder.py 三处统一调用，
避免同一个坑在不同文件里各踩一遍。
"""
from __future__ import annotations

import json
import os
import time

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "_kb", "browser_ok.json")
TTL = 86400.0

DEFAULT_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright",
                 "chromium-1228", "chrome-win", "chrome.exe"),
    r"G:\_pw_chrome\chrome-win64\chrome.exe",
]

ARGS = ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
        "--no-proxy-server", "--disable-session-crashed-bubble",
        "--disable-infobars", "--no-first-run", "--no-default-browser-check",
        "--disable-restore-session-state"]


def _read_cache():
    try:
        d = json.load(open(CACHE, encoding="utf-8"))
        if time.time() - float(d.get("ts", 0)) < TTL:
            return d.get("exe", "")
    except Exception:
        pass
    return None


def _write_cache(exe):
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump({"exe": exe, "ts": time.time(),
                       "at": time.strftime("%Y-%m-%d %H:%M:%S")}, f,
                      ensure_ascii=False, indent=2)
    except Exception:
        pass


def pick(candidates=None, force=False, timeout=25000):
    """返回可用的 executable_path；"" = 用 Playwright 默认 chromium。"""
    if not force:
        hit = _read_cache()
        if hit is not None:
            return hit
    cands = [c for c in (candidates or DEFAULT_CANDIDATES)
             if c and os.path.exists(c)]
    order = [None] + cands          # 先试 Playwright 自带（本机实测最稳）
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            for exe in order:
                try:
                    kw = {"headless": True, "args": ARGS, "timeout": timeout}
                    if exe:
                        kw["executable_path"] = exe
                    b = p.chromium.launch(**kw)
                    try:
                        pg = b.new_page()
                        pg.goto("about:blank", timeout=5000)
                    finally:
                        b.close()
                    val = exe or ""
                    _write_cache(val)
                    return val
                except Exception:
                    continue
    except Exception:
        pass
    return cands[0] if cands else ""


def launch_kw(exe):
    """把 pick() 的返回值转成 chromium.launch 的关键字参数。"""
    return {"executable_path": exe} if exe else {}


PW_ROOT = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")


GPU_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_gpu_browser():
    """GPU 模式专用：返回「真起得来且有完整 GPU 进程」的浏览器，找不到返回 ""。

    2026-11-04 本机实测四种组合：
      Playwright 自带 chromium-1248 + 完整 chrome.exe  → **launch 即退，不可用**
      Edge 149 + --headless=new                        → OK，绑 GTX 1660 Ti
      Edge 149 不带 --headless=new                      → OK，同样绑独显
      chrome-headless-shell（Playwright 默认）           → 能用，但 GPU 进程是裁剪版
    ⇒ GPU 档优先系统 Edge；Edge 不在再退回 Playwright 默认（至少能干活）。
    """
    for p in GPU_CANDIDATES:
        if os.path.exists(p):
            return p
    return ""


def find_full_chromium():
    """返回 Playwright 装的**完整 Chromium**（chrome.exe）路径，找不到返回 ""。

    🔴 为什么不能只靠 pick()：pick() 的候选第一项是 None = Playwright 默认，
    而 Playwright 在 headless=True 下默认启动的是 `chrome-headless-shell`
    （精简无头壳，进程名就是 chrome-headless-shell）。那套壳的 GPU 进程被裁过，
    只适合纯软件渲染；要真吃独显、要走 --headless=new 的完整合成管线，
    必须显式指向 chromium-*/chrome-win64/chrome.exe 这个完整二进制。
    """
    import glob
    hits = []
    for pat in ("chromium-*/chrome-win64/chrome.exe",
                "chromium-*/chrome-win/chrome.exe"):
        hits.extend(glob.glob(os.path.join(PW_ROOT, pat)))
    hits = [h for h in hits if os.path.exists(h)]
    if not hits:
        return ""
    hits.sort(key=lambda h: os.path.getmtime(h), reverse=True)
    return hits[0]


if __name__ == "__main__":
    import sys
    e = pick(force="--force" in sys.argv)
    print("可用浏览器:", e or "(Playwright 自带 chromium)")
    print("缓存:", CACHE)
