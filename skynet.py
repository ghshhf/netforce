#!/usr/bin/env python
"""天网采集内核 · 命令行
========================
    python skynet.py search "关键词" [--engines bing,ddg] [--top 30]
    python skynet.py probe URL [URL ...]
    python skynet.py probe-file urls.txt [--workers 5]
    python skynet.py hosts [--limit 30]
    python skynet.py stats
    python skynet.py rules            # 打印当前检测阈值（升级时对着改 RULES）

输出一律纯文本，方便管道与日志落盘。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skynet import net, probe as pmod, search as smod, store  # noqa: E402


def cmd_search(a):
    only = [x.strip() for x in a.engines.split(",")] if a.engines else None
    hits = smod.search(a.query, wiki_tag=a.wiki_tag, only=only, min_rel=a.min_rel)
    if not hits:
        print("无相关结果（引擎失败，或全部被意图校验挡下）")
        print("引擎健康度:", {k: v for k, v in smod.HEALTH.items()})
        return 1
    con = store.connect()
    for h in hits[: a.top]:
        row = con.execute("SELECT verdict,score FROM seen WHERE url=?", (h.url,)).fetchone()
        tag = f"[{row[0]}:{row[1]}]" if row else "[new]"
        print(f"{h.score:5.2f} rel{h.rel:.2f} {tag:13s} x{h.cross} "
              f"{'/'.join(h.engines)[:22]:22s} {h.title[:30]:30s} {h.url[:58]}")
    print(f"\n合计 {len(hits)} 条，{len(smod.by_host(hits))} 个域")
    print("引擎健康度(in→out):", {k: f"{v['in']}→{v['out']}" for k, v in smod.HEALTH.items()})
    return 0


def cmd_probe(a):
    urls = a.urls
    con = store.connect()
    good = 0
    for u in urls:
        p = pmod.probe(u, keep_html=False)
        store.mark(con, p.url, smod.host_of(p.url) or "-", kind="probe",
                   verdict=p.verdict, score=p.score, size=p.size,
                   err=";".join(p.reasons)[:80])
        print(p.brief())
        if p.verdict == "valuable":
            good += 1
    print(f"\n{good}/{len(urls)} 判定为 valuable")
    return 0


def cmd_probe_file(a):
    urls = [x.strip() for x in Path(a.path).read_text(encoding="utf-8").splitlines()
            if x.strip() and x.strip().startswith("http")]
    if not urls:
        print("文件里没有 URL")
        return 1
    print(f"待测 {len(urls)} 条，并发 {a.workers}")
    con = store.connect()
    done = 0
    for u, p in zip(urls, pmod.batch_probe(urls, workers=a.workers)):
        if isinstance(p, Exception) or p is None:
            print(f"  ✗ 异常 {u[:70]}")
            continue
        store.mark(con, p.url, smod.host_of(p.url) or "-", kind="probe",
                   verdict=p.verdict, score=p.score, size=p.size)
        print(p.brief())
        done += 1
    print(f"\n完成 {done}/{len(urls)}；{store.summary(con)}")
    return 0


def cmd_hosts(a):
    con = store.connect()
    rows = store.host_rank(con, a.limit)
    if not rows:
        print("还没有域画像（先跑 probe）")
        return 0
    print(f"{'host':38s} {'探测':>4s} {'优质':>4s} {'最高':>4s} {'均分':>5s}")
    for h, n, g, b, av in rows:
        print(f"{h[:38]:38s} {n:4d} {g:4d} {b:4d} {av:5.1f}")
    return 0


def cmd_stats(a):
    con = store.connect()
    s = store.summary(con)
    print(f"记录 URL {s['urls']} 条 / {s['MB']} MB / 域 {s['hosts']} 个")
    print("判定分布:", s["verdict"] or "-")
    print("本次进程:", net.STATS.report())
    return 0


def cmd_netstat(a):
    """下载层实况：出口、会话复用、并发闸门、退避、通道熔断。"""
    from skynet import net
    st = net.STATS.report()
    print("下载层实况")
    print(f"  会话后端   {net.SESSIONS.backend() or '（未装 requests/curl_cffi，退回 urllib 无复用）'}")
    print(f"  每域并发   {net.HOST_CONCURRENCY}   活跃会话 {len(net.SESSIONS._pool)}")
    print(f"  风控码     {net.RISK_CODES}")
    print(f"  退避因子   {net.THROTTLE.report()}")
    print(f"  通道熔断   {net.CHANS.report() or '无'}")
    print(f"  出口       {len(net.PROXIES)} 个")
    for e in net.EGRESS.report():
        state = "可用" if e["usable"] else f"冷却中 {e['cool_left']}s"
        print(f"             {e['proxy']:32s} 成功{e['ok']:6d} 风控{e['block']:3d}  {state}")
    st = net.STATS.report()
    print(f"  本轮统计   {st['req']} 次 / 成功 {st['ok']} / 失败 {st['fail']}"
          f" / 风控 {st['blocked']} / {st['MB']}MB / 通道 {st['channel'] or '-'}")
    if a.url:
        r = net.fetch_text(a.url, timeout=15, budget=20)
        print(f"\n连通性     {r.brief()}")
        for code, u in r.hops:
            print(f"    ↳ [{code}] {u[:88]}")
    return 0


def cmd_dedupe(a):
    """扫描目录按内容指纹找重复；默认只报告，加 --apply 才做成硬链接。"""
    from skynet import store
    root = Path(a.path)
    if not root.exists():
        print(f"目录不存在: {root}")
        return 2
    exts = tuple(x.strip().lower() for x in a.ext.split(",")) if a.ext else (
        ".mp3", ".ogg", ".wav", ".m4a", ".flac")
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in exts]
    print(f"扫描 {root}")
    print(f"  候选文件 {len(files)} 个")
    groups: dict[str, list] = {}
    total = 0
    for i, p in enumerate(files, 1):
        try:
            h = store.sha256_file(p)
            total += p.stat().st_size
        except Exception:
            continue
        groups.setdefault(h, []).append(p)
        if i % 500 == 0:
            print(f"    ...{i}/{len(files)}")
    dup = {h: ps for h, ps in groups.items() if len(ps) > 1}
    saved = sum((len(ps) - 1) * ps[0].stat().st_size for ps in dup.values())
    print(f"  唯一内容 {len(groups)} 份 / 总体积 {total / 1048576:.1f}MB")
    print(f"  重复组 {len(dup)} 组，去重可省 {saved / 1048576:.1f}MB")
    for h, ps in list(dup.items())[:8]:
        print(f"    {h[:12]} ×{len(ps)}  {ps[0].name[:40]}")
    if not a.apply:
        if dup:
            print("\n（仅报告。加 --apply 才把重复项改成硬链接——路径都保留，只是共用实体）")
        return 0
    if not dup:
        return 0
    done = 0
    con = store.connect()
    try:
        for h, ps in dup.items():
            keep = ps[0]
            for other in ps[1:]:
                try:
                    other.unlink()
                    os.link(keep, other)
                    done += 1
                except Exception:
                    pass
            store.blob_add(con, h, keep, keep.stat().st_size)
    finally:
        con.close()
    print(f"\n已去重 {done} 个文件，路径全部保留")
    return 0


def cmd_retry(a):
    """补抓失败项：页面类重新探活，音频/文件类带续传重下。

    为什么必须分开：探活用 probe（要判质量四档），补文件用 download（只看拿没拿到）。
    混在一起会出现「音频拿去算台词密度」这种荒唐判定。
    """
    import urllib.parse
    con = store.connect()
    kinds = tuple(x.strip() for x in a.kind.split(",") if x.strip()) if a.kind else ()
    rows = store.pending_rows(con, max_tries=a.max_tries, limit=a.limit, kinds=kinds)
    if not rows:
        print("没有待重试项")
        return 0
    print(f"待重试 {len(rows)} 条 | kind={','.join(kinds) if kinds else '全部'} | 次数上限 {a.max_tries}")
    ok = fail = 0
    for i, (url, kind, path, err, tries) in enumerate(rows, 1):
        host = urllib.parse.urlparse(url).netloc
        if kind == "audio" and path:
            r = net.download(url, Path(path), dedupe=True)
            if r.ok:
                store.mark(con, url, host, kind=kind, verdict="ok", size=r.size, path=path)
                ok += 1
                mark = "✓"
            else:
                store.mark(con, url, host, kind=kind, verdict="", err=r.error, path=path)
                fail += 1
                mark = "✗"
        else:
            p = pmod.probe(url)
            store.mark(con, url, host, kind=kind or "probe", verdict=p.verdict,
                       score=p.score, size=p.size, err="" if p.alive else (err or "probe failed"))
            if p.verdict in store.DONE_VERDICTS:
                ok += 1
                mark = "✓"
            else:
                fail += 1
                mark = "✗"
        if i <= a.verbose or i % 20 == 0:
            print(f"  [{i}/{len(rows)}] {mark} try{tries} {url[:74]}")
    print(f"\n完成：成功 {ok} / 仍失败 {fail}")
    return 0


def cmd_rules(a):
    import json
    print(json.dumps(pmod.RULES, ensure_ascii=False, indent=1))
    return 0


def cmd_structure(a):
    from skynet import discover
    s = discover.structure(a.url, max_urls=a.max_urls)
    print(f"站点   {s['root']}")
    print(f"robots {s['robots']}")
    print(f"sitemap 文件 {s['sitemap_files']} 个 / URL {s['sitemap_urls']} 条"
          f"{'（已截断）' if s['truncated'] else ''}")
    print(f"扩展名分布 {s['ext'] or '-'}")
    print(f"feed   {s['feed'] or '-'}")
    print("样本:")
    for u in s["sample"]:
        print("   ", u[:100])
    return 0


def main():
    ap = argparse.ArgumentParser(prog="skynet", description="天网采集内核")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", help="多引擎聚合搜索")
    p.add_argument("query")
    p.add_argument("--engines", default="", help="ddg,searx,bing,github,wiki")
    p.add_argument("--wiki-tag", default=None, help="给站内搜索用的 wiki 代号，如 sr / blhx")
    p.add_argument("--min-rel", type=float, default=smod.MIN_REL, help="意图相关度下限")
    p.add_argument("--top", type=int, default=30)
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("probe", help="探测一批 URL 的质量分")
    p.add_argument("urls", nargs="+")
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("probe-file", help="从文件批量探测")
    p.add_argument("path")
    p.add_argument("--workers", type=int, default=5)
    p.set_defaults(fn=cmd_probe_file)

    p = sub.add_parser("hosts", help="域画像排行")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_hosts)

    p = sub.add_parser("stats", help="总览")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("rules", help="打印检测阈值")
    p.set_defaults(fn=cmd_rules)

    p = sub.add_parser("netstat", help="下载层实况：后端/出口/并发/熔断/退避")
    p.add_argument("--url", default="", help="顺手测一次连通性")
    p.set_defaults(fn=cmd_netstat)

    p = sub.add_parser("structure", help="站点结构发现：robots + sitemap")
    p.add_argument("url")
    p.add_argument("--max-urls", type=int, default=1500)
    p.set_defaults(fn=cmd_structure)

    p = sub.add_parser("dedupe", help="按内容指纹找重复（默认只报告）")
    p.add_argument("path")
    p.add_argument("--ext", default="", help="扩展名过滤，逗号分隔；默认音视频")
    p.add_argument("--apply", action="store_true", help="真做成硬链接（路径全保留）")
    p.set_defaults(fn=cmd_dedupe)

    p = sub.add_parser("retry", help="补抓失败项：页面重探 / 文件续传")
    p.add_argument("--kind", default="", help="audio,probe,text；默认全部")
    p.add_argument("--max-tries", type=int, default=3)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--verbose", type=int, default=10, help="前 N 条逐条打印")
    p.set_defaults(fn=cmd_retry)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
