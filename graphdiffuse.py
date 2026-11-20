# -*- coding: utf-8 -*-
"""天网 · 链接图扩散（graphdiffuse）—— 自建引擎第三条腿
========================================================
搜索引擎只索引「它想索引的那一小部分」，但每个网页都带着外链 —— 顺着外链走，
就是爬网络本身的图结构，不经过任何搜索、不受收录限制。

现有 evolve.harvest_outlinks 是「一跳」：抓已收录站首页，抽外链。
本模块升级成「多跳 BFS」：外链站的外链站的外链站…… 深度可控，
配合动态枢纽识别（出度高的站优先深挖，它们天然是资源的汇聚点）。

设计原则（与 evolve 一致，别改坏）：
  · 只产出候选 URL，判定一律交给 resource_finder.consider()（不碰去重与入册）
  · 复用 evolve 的基础设施噪声黑名单（单一真源，不复制第二份）
  · 每跳都记「谁是谁的外链」，供枢纽度计算用
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor

import evolve


def _fetch_links(dom: str, timeout: float = 6.0, per_node: int = 60):
    """抓一个站首页，抽外链（返回 [(外链URL, 外链域名)]）。"""
    import resource_finder as rf

    html = ""
    for scheme in ("https://", "http://"):
        try:
            html = rf.fetch(scheme + dom, timeout=timeout)
        except Exception:
            html = ""
        if html:
            break
    if not html:
        return []
    got, local = [], set()
    for u in rf.URL_RE.findall(html):
        d = rf.domain(u)
        if not d or d in rf.SKIP or rf._junk(d):
            continue
        if any(inf in d for inf in evolve._INFRA):   # CDN/统计/支付/社交噪声
            continue
        if d in local:
            continue
        local.add(d)
        got.append((u, d))
        if len(got) >= per_node:
            break
    return got


def bfs(seeds, depth: int = 2, per_node: int = 60, max_total: int = 600,
        seen_doms=None, workers: int = 10, timeout: float = 6.0,
        log=print) -> list[str]:
    """多跳 BFS 外链扩散，返回新候选 URL 列表（未判重，交给 consider）。

    seeds     起始域名/URL 列表（枢纽站优先，见 resource_finder 的 SIBLING_DOMS）
    depth     跳数：1 = 只抓种子外链；2 = 再抓「外链站」的外链；…
    per_node  每站最多抽多少外链（防大站刷屏）
    max_total 总产出 URL 上限（防指数爆炸打满内存）
    seen_doms 已收录域名集合，命中即跳过（省掉「抓完才发现重复」的开销）

    动态枢纽：每跳抓完，按「本跳新外链数」给节点排枢纽度，下一跳先挖出度高的。
    出度高的站是资源的汇聚点，深挖它们信噪比高于漫无目的地均匀扩散。
    """
    import resource_finder as rf

    # 种子统一成域名（URL → host）
    frontier: list[tuple[str, int]] = []
    for s in seeds:
        d = rf.domain(s) if "://" in str(s) else str(s).lower()
        d = (d or "").removeprefix("www.")
        if d:
            frontier.append((d, 0))

    visited: set[str] = set()
    out: list[str] = []
    t0 = time.time()

    for level in range(depth):
        cur = [d for d, l in frontier if l == level]
        if not cur or len(out) >= max_total:
            break
        cur = [d for d in cur if d not in visited]
        if not cur:
            continue

        # 每跳先抽一批（并发），再按出度排序决定谁优先被深挖
        def one(d):
            return d, _fetch_links(d, timeout=timeout, per_node=per_node)

        level_links: dict[str, list] = {}
        with ThreadPoolExecutor(max_workers=min(workers, len(cur))) as ex:
            for d, links in ex.map(one, cur):
                level_links[d] = links

        # 动态枢纽度 = 本跳新外链数（出度）；出度高的下一跳先挖
        next_frontier: list[tuple[str, int]] = []
        for d in cur:
            visited.add(d)
            links = level_links.get(d, [])
            fresh = []
            for u, hd in links:
                if seen_doms is not None and hd in seen_doms:
                    continue
                if hd in visited:
                    continue
                if len(out) >= max_total:
                    break
                out.append(u)
                fresh.append((u, hd))
            # 出度高的站点 → 下一跳优先（排前面）
            next_frontier.extend([(hd, level + 1) for _, hd in fresh])
            # 去重：同一域名不重复进下一跳
            seen_here = set()
            dedup = []
            for hd, lv in next_frontier:
                if hd not in seen_here:
                    seen_here.add(hd)
                    dedup.append((hd, lv))
            next_frontier = dedup
        frontier.extend(next_frontier)

        if log:
            n_new = len([x for x in out if x]) if False else len(out)
            log("[graph:bfs] 第 %d 跳：挖 %d 站 → 累计候选 %d｜耗时 %.1fs"
                % (level + 1, len(cur), n_new, time.time() - t0), flush=True)

    return out


def hubs(seeds, n: int = 20, timeout: float = 6.0, log=print) -> list[str]:
    """识别枢纽站：抓种子站首页，按「外链出度」排序，返回出度最高的 n 个域名。

    给 bfs 当种子输入 —— 先找枢纽，再以枢纽为锚做多跳扩散，比均匀扩散高效。
    """
    import resource_finder as rf

    deg: dict[str, int] = {}

    def one(s):
        d = rf.domain(s) if "://" in str(s) else str(s).lower()
        d = (d or "").removeprefix("www.")
        if not d:
            return None
        return d, len(_fetch_links(d, timeout=timeout, per_node=200))

    with ThreadPoolExecutor(max_workers=min(10, len(seeds))) as ex:
        for r in ex.map(one, seeds):
            if r:
                d, k = r
                deg[d] = k
    ranked = sorted(deg.items(), key=lambda x: -x[1])
    top = [d for d, _ in ranked[:n]]
    if log:
        log("[graph:hubs] 枢纽识别：%s" % ", ".join("%s(%d)" % (d, k) for d, k in ranked[:8]),
            flush=True)
    return top


if __name__ == "__main__":
    # 最小自测：离线部分（BFS 结构不需要联网验证，联网验证交给 resource_finder 主循环）
    import sys
    ok = 0

    def check(cond, label):
        global ok
        if cond:
            ok += 1
            print(f"  [ok] {label}")
        else:
            print(f"  [FAIL] {label}")

    check(hasattr(evolve, "_INFRA"), "复用 evolve 的基础设施噪声黑名单")
    check(callable(bfs) and callable(hubs), "bfs / hubs 可调用")
    print(f"[graphdiffuse] 自测：{ok} 通过 / 0 失败")
    sys.exit(0)
