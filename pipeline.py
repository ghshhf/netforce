# -*- coding: utf-8 -*-
"""netforce / pipeline.py —— 一条命令跑完「发现 → 渲染 → 细分类 → 索引 → 资源落盘」

为什么要有它：
    之前每个阶段都是独立脚本，得人手工按顺序敲；而且 render_parse 的 --loop
    只管解析，新发现的域名、可下载资源、知识库索引都堆积在文件里没人消费。
    这一层把闭环串成一个进程，watchdog 只需守护「主爬虫 + pipeline」两个目标，
    不会出现多个渲染器抢同一把锁。

一轮做四件事（顺序固定）：
    ① 从 pending_domains() 取一批域（含渲染发现的新域名 + 失败重试）
    ② 无头完整解析 → 解析即打多维标签 → 分片 append-only 落盘 + 发现回灌
    ③ 增量灌进 FTS5 知识库索引（顺带用最新分类规则重打历史标签）
    ④ 把 JS/链接里真正的文档数据资源续传落盘（aria2c 优先）

🔴 纪律沿用：
    · 默认纯直连出网，代理能力只接受远程显式指定且固定不切换；
    · 全部阶段 append-only / SQLite 断点，随时可停、重启不重来；
    · 代码文件改了 ⇒ 做完当前批次后写 RELOAD_REQUEST 优雅退出，守护用新代码拉起；
    · 不改正在运行的 resource_finder.py 进程，只从它写的 resources.json 取料。

用法：
    python pipeline.py --rounds 1 --render-limit 8       # 跑一轮小批量
    python pipeline.py --loop                            # 长期自主闭环
    python pipeline.py --status                          # 四层状态一屏看完
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time

import netpolicy
import render_parse
import kb_index
import download_assets

BASE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(BASE, "_kb")
EXPORT = os.path.join(KB, "kb_export.jsonl")
RELOAD_FLAG = os.path.join(KB, "RELOAD_REQUEST")

WATCHED = ["netpolicy.py", "render_parse.py", "fine_classify.py",
           "download_assets.py", "kb_index.py", "pipeline.py"]


def code_signature():
    return {n: (os.path.getmtime(os.path.join(BASE, n))
                if os.path.exists(os.path.join(BASE, n)) else 0) for n in WATCHED}


def status():
    print("=== 出网策略 ===")
    d = netpolicy.describe()
    print("默认出口:", d["default_egress"])
    print("显式代理:", d["configured_proxies"] or "（无，纯直连）")
    print("系统代理:", d["system_proxy"])
    print("\n=== 解析层 ===")
    render_parse.status()
    print("\n=== 知识库索引 ===")
    kb_index.stats()
    print("\n=== 资源落盘 ===")
    download_assets.status()


# ---------------------------------------------------------------- 渲染流水线
# 为什么要有它：HTTP 直取和浏览器渲染是两种完全不同的负载 ——
#   HTTP 阶段：纯网络等待（实测 1000 域 226s，CPU 20%、GPU 0%、显存 122MB 不动）
#   渲染阶段：吃满 CPU + 独显（60s，CPU 60%+、GPU 20%+）
# 串行跑等于让整机一半时间空转（主人原话：「电脑总感觉就跟没有运行一样」）。
# 现在把「本轮渲染」挂到后台线程，让它跟「下一轮 HTTP」重叠 ——
# 两个阶段几乎不抢资源，总时间省掉整个渲染段的耗时。
_RENDER = {"th": None, "out": [], "err": None, "n": 0}


def _start_render(rest, w, t, timeout, batch, gpu):
    """把本轮「空壳」交给后台线程渲染，主线程立刻去跑下一轮 HTTP。"""
    if not rest:
        return
    if _RENDER["th"] is not None:
        _collect_render()          # 上一批还没收就先收掉，绝不并行两批抢内存

    def work():
        try:
            out = render_parse.render_stage(rest, w, t, timeout, batch, gpu)
            _RENDER["out"] = out
            # 渲染一完成就落盘：否则 done 表更新不及时，下一轮会把同一批域再取一次
            if out:
                render_parse.persist(out)
                _RENDER["n"] = len(out)
        except Exception as e:
            _RENDER["err"] = "%s: %s" % (type(e).__name__, str(e)[:140])
            _RENDER["out"] = []

    th = threading.Thread(target=work, daemon=True, name="render-bg")
    _RENDER["th"] = th
    _RENDER["out"] = []
    _RENDER["err"] = None
    _RENDER["n"] = 0
    th.start()


def _collect_render(log=print):
    """收上一轮的后台渲染结果（此时它已随本轮 HTTP 阶段跑完了）。"""
    th = _RENDER["th"]
    if th is None:
        return []
    th.join()
    out = _RENDER["out"] or []
    err, n = _RENDER["err"], _RENDER["n"]
    _RENDER.update({"th": None, "out": [], "err": None, "n": 0})
    if err:
        log("  [render] 后台渲染异常：%s" % err, flush=True)
    elif n:
        log("  [render] 上一轮后台渲染 %d 条已落盘（与本轮 HTTP 并行完成）" % n,
            flush=True)
    return out


def one_round(a, rnd):
    netpolicy.reload()                       # 改 _netpolicy.json 下一轮即生效
    t0 = time.time()
    items = render_parse.pending_domains(a.render_limit)
    print("\n[pipeline] 第 %d 轮：待解析 %d 域 | 出口 %s"
          % (rnd, len(items), netpolicy.egress_for() or "纯直连(固定)"), flush=True)
    if not items and not a.download_only:
        print("[pipeline] 待解析队列为空（发现队列/资源队列仍照常消费）", flush=True)

    recs = []
    if items and not a.download_only:
        w, t, gpu, mode_used = netpolicy.concurrency_plan(a.mode)
        if a.workers:
            w = a.workers
        if a.tabs:
            t = a.tabs
        if a.gpu:
            gpu = True
        print("[pipeline] 并发模式=%s → workers=%d tabs=%d gpu=%s"
              % (mode_used, w, t, "on" if gpu else "off"), flush=True)
        if a.no_http_first:
            # 关掉漏斗 = 没有 HTTP 阶段可重叠，老实串行
            recs = render_parse.render_stage(items, w, t, a.timeout, a.batch, gpu)
        else:
            import http_first as _hf
            # 主线程跑 HTTP；同一时间后台线程在渲染「上一轮的空壳」
            # 默认走滚动调度：worker 空出来立刻补位，慢站单独回队尾升级超时。
            # 对比「三轮齐步走」，150 域实测 79.3s → 53.3s（1.49×），成功数还多 4 条。
            _fetch = _hf.fetch_many if a.http_mode == "rounds" else _hf.fetch_many_slide
            solved, rest = _fetch(items, conc=a.http_conc,
                                  timeout=a.http_timeout, log=print)
            prev = _collect_render()      # 上一轮渲染已随本轮 HTTP 并行跑完
            recs = solved + prev
            _start_render(rest, w, t, a.timeout, a.batch, gpu)
            if rest:
                print("  [pipeline] %d 条空壳交后台渲染，主线程继续跑下一轮 HTTP"
                      % len(rest), flush=True)
        n = render_parse.persist(recs)
        render_parse.summary(recs)
        print("  本轮新落盘 %d 条" % n, flush=True)

    # 索引：哪怕本轮没解析，也把别的进程新写的分片灌进来
    added, _files = kb_index.ingest(log_fn=print)

    # 资源：扫描新线索 + 续传下载（失败不清零进度）
    dl = {"done": 0, "failed": 0, "bytes": 0}
    if not a.no_download:
        dl = download_assets.run_once(
            limit=a.download_limit, workers=a.dl_workers,
            max_total_mb=a.max_total_mb, scan_first=True, log_fn=print)

    # 导出紧凑知识库（给本地模型 / 其他 AI 直接读），按轮次节流避免反复写大文件
    if a.export_every and rnd % a.export_every == 0:
        try:
            kb_index.export(EXPORT)
        except Exception as e:
            print("  [pipeline] 导出失败：%s" % str(e)[:80], flush=True)

    print("[pipeline] 第 %d 轮完成：解析 %d / 索引 +%d / 落盘 %d 个资源 / %.1fs"
          % (rnd, len(recs), added, dl.get("done", 0), time.time() - t0), flush=True)
    return len(items)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="长期闭环常驻")
    ap.add_argument("--rounds", type=int, default=0,
                    help="跑几轮；0=不限（--loop 时一直跑），非 loop 固定 1 轮")
    ap.add_argument("--render-limit", type=int, default=200)
    ap.add_argument("--download-limit", type=int, default=40)
    ap.add_argument("--workers", type=int, default=None,
                    help="手动覆盖进程数；不传则按 --mode 自动取")
    ap.add_argument("--tabs", type=int, default=None,
                    help="手动覆盖每进程 tab 数；不传则按 --mode 自动取")
    ap.add_argument("--mode", default="auto",
                    choices=["auto", "conservative", "aggressive"],
                    help="auto=按堆积量自动切（默认）；conservative=省资源；"
                         "aggressive=整机 CPU+GPU 拉满")
    ap.add_argument("--gpu", action="store_true", help="强制开 GPU 加速")
    ap.add_argument("--no-http-first", action="store_true",
                    help="关掉 HTTP 一级漏斗，所有域都走浏览器（不推荐，仅用于对照）")
    ap.add_argument("--http-conc", type=int, default=48,
                    help="HTTP 一级漏斗并发数（默认 48，异步很轻，可到 96）")
    ap.add_argument("--http-timeout", type=float, default=8.0,
                    help="HTTP 一级漏斗单域超时秒数（默认 8）")
    ap.add_argument("--http-mode", choices=("slide", "rounds"), default="slide",
                    help="一级漏斗调度：slide=滚动窗口（默认，快 1.5 倍）；"
                         "rounds=旧的三轮齐步走（仅用于对照）")
    ap.add_argument("--timeout", type=int, default=12)
    ap.add_argument("--batch", type=int, default=15)
    ap.add_argument("--dl-workers", type=int, default=3)
    ap.add_argument("--max-total-mb", type=int, default=1024)
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--download-only", action="store_true")
    ap.add_argument("--export-every", type=int, default=10)
    ap.add_argument("--sleep", type=int, default=5)
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()

    render_parse.ensure_dirs()
    download_assets.ensure()
    if a.status:
        status()
        return
    if not render_parse.acquire_lock():
        return
    sig = code_signature()
    rnd = 0
    try:
        while True:
            rnd += 1
            idle = one_round(a, rnd) == 0
            if not a.loop or (a.rounds and rnd >= a.rounds):
                break
            if code_signature() != sig:
                with open(RELOAD_FLAG, "w", encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S")
                            + " pipeline code changed\n")
                print("[pipeline] 检测到代码更新 ⇒ 完成本批后优雅退出，"
                      "等待守护用新代码拉起", flush=True)
                break
            time.sleep(60 if idle else a.sleep)
    finally:
        try:
            _collect_render()      # 等后台渲染跑完（它完成后自己会落盘）
        except Exception:
            pass
        render_parse.release_lock()


if __name__ == "__main__":
    main()
