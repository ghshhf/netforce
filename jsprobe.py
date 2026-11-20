"""JS 站标题补抓：无头浏览器池（多进程 × 每进程多 tab 并发）。

为什么要有这个模块：
    resource_finder.py 的 fetch() 第四级确实接了无头浏览器，但那只是**抓目录页链接**用的；
    判断「这个站是干什么的」用的是首页 title/meta description，这一步在 classify.py 里，
    而 classify.py 的 _get() 只有 urllib 两通道（代理/直连），**压根没有浏览器**。
    ⇒ 纯 JS 渲染的站被静态通道判成「无 title」，落进「JS渲染/无静态内容」这一类（实测 1383 条）。
    本模块就是补上这一级：静态抓不到 title ⇒ 用真浏览器渲染后再取。

并发设计（关键）：
    playwright 的 sync API 非线程安全，单实例串行跑 1383 条要 1.5 小时以上。
    ⇒ 用 async_api：每个进程起 1 个 browser，进程内多 tab（默认 6）asyncio.gather 并发；
      再用 ProcessPoolExecutor 起 N 个进程分片。1383 条 ≈ 2-4 分钟。

用法：
    from jsprobe import probe_many
    got = probe_many(['example.com', ...])      # → {'example.com': (title, desc), ...}
命令行自测：
    python jsprobe.py a.com b.com c.com
"""
import os
import sys
import asyncio

import netpolicy          # 统一出网：天网 fleet/net.py 自适应直连优先
import browser_pick       # 浏览器启动实测择优（系统 Chrome 会自退）

# ⚠️ 本机实测：新下载的 Chrome for Testing 156 在这台机器上启动即崩（STATUS_BREAKPOINT
#    0x80000003），系统自带 Chrome 155 / Edge 149 正常 ⇒ 候选顺序以系统浏览器优先。
BROWSER_CANDIDATES = [
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright",
                 "chromium-1228", "chrome-win", "chrome.exe"),
]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
# 🔴 旧版默认写死 7890；本机代理没开时，无头补抓 100% 走死代理 ⇒ 1383 条全灭。
#    现在默认直连，代理只在 netpolicy 验活后才作为第二条通道启用。
PROXY = netpolicy.cfg().get("proxy", "")

TITLE_MAXLEN = 300


def _pick_exe():
    """真起一次再决定（系统 Chrome 会自退）；"" = Playwright 自带 chromium。"""
    return browser_pick.pick(BROWSER_CANDIDATES)


async def _fetch_one(ctx, ctx_proxy, sem, dom, timeout, out):
    """单域名：**出口一次定死**（netpolicy 固定规则），失败不换通道。

    默认纯直连（直连国外）；只有远程显式指定代理的域才走代理 context。
    """
    async with sem:
        ch_px = ""
        try:
            ch_px = netpolicy.channels(dom)[0][1]
        except Exception:
            pass
        use_ctx = ctx_proxy if (ch_px and ctx_proxy is not None) else ctx
        chans = [(use_ctx, "proxy" if ch_px else "direct")]
        for use_ctx, _ch in chans:
            for scheme in ("https", "http"):
                url = "%s://%s/" % (scheme, dom)
                pg = None
                try:
                    pg = await use_ctx.new_page()
                    await pg.goto(url, timeout=timeout * 1000,
                                  wait_until="domcontentloaded")
                    await pg.wait_for_timeout(1500)  # 等 JS 把标题填上（经验值）
                    title = (await pg.title() or "").strip()
                    desc = await pg.evaluate(
                        "() => { const m = document.querySelector("
                        "'meta[name=description], meta[property=\"og:description\"]');"
                        " return m ? (m.content || '') : ''; }")
                    desc = (desc or "").strip()
                    if title or desc:
                        out[dom] = (title[:TITLE_MAXLEN], desc[:TITLE_MAXLEN])
                        return True
                except Exception:
                    continue
                finally:
                    try:
                        if pg:
                            await pg.close()
                    except Exception:
                        pass
    return False


def _worker(doms, ntabs, timeout, exe, batch=20):
    """一个进程 = 一个 browser + ntabs 个并发 tab。返回 {domain: (title, desc)}。

    ⚠️ 分批重启（batch）：本机 Chrome 长时间挂着会偶发崩溃（STATUS_BREAKPOINT），
    每处理 batch 个域名就关掉重开一个 browser，把爆炸半径限制在一小批内。
    """
    async def _main():
        from playwright.async_api import async_playwright
        out = {}
        exe_kw = browser_pick.launch_kw(exe)   # "" ⇒ 不传 executable_path
        sem = asyncio.Semaphore(ntabs)
        chunks = [doms[i:i + batch] for i in range(0, len(doms), batch)]
        for ci, chunk in enumerate(chunks):
            try:
                async with async_playwright() as p:
                    # 出口由 netpolicy 验活后决定；默认直连（--no-proxy-server），
                    # 只有真有可用代理时才额外起一个代理浏览器作第二条通道。
                    # 固定出口：默认 "" = 真直连；只有远程显式指定才非空。
                    # 不扫描端口、不读系统代理、不碰主人本地端口。
                    px = netpolicy.browser_proxy()
                    browser = await p.chromium.launch(
                        headless=True, **exe_kw, timeout=60000,
                        args=netpolicy.browser_launch_args(""))
                    browser_p = None
                    if px:
                        try:
                            browser_p = await p.chromium.launch(
                                headless=True, **exe_kw, timeout=60000,
                                args=netpolicy.browser_launch_args(px),
                                proxy={"server": px})
                        except Exception:
                            browser_p = None
                    try:
                        ctx = await browser.new_context(
                            user_agent=UA, viewport={"width": 1366, "height": 768})
                        ctx_p = None
                        if browser_p:
                            ctx_p = await browser_p.new_context(
                                user_agent=UA, viewport={"width": 1366, "height": 768})
                        await asyncio.gather(
                            *[_fetch_one(ctx, ctx_p, sem, d, timeout, out)
                              for d in chunk])
                    finally:
                        for b in (browser, browser_p):
                            try:
                                if b:
                                    await b.close()
                            except Exception:
                                pass
            except Exception as e:
                print("    [jsprobe] 批次 %d 浏览器异常：%s" % (ci + 1, str(e)[:80]),
                      flush=True)
        return out

    return asyncio.run(_main())


def _chunks(lst, n):
    n = max(1, n)
    size = (len(lst) + n - 1) // n
    return [lst[i:i + size] for i in range(0, len(lst), size)]


# ⚠️ 并发别贪：本机实测**同时起 6 个 Chrome 会把启动拖到 180s 超时**（每个 Chrome 自带
#    多个子进程，61 个进程打满机器）。3 进程 × 6 tab = 18 并发是本机甜点值。
def probe_many(doms, workers=3, ntabs=6, timeout=15, log=print):
    """批量补抓标题。返回 {domain: (title, desc)}（只含成功拿到内容的）。"""
    doms = list(dict.fromkeys(doms))          # 去重保序
    if not doms:
        return {}
    exe = _pick_exe()                 # "" = Playwright 自带 chromium
    parts = _chunks(doms, min(workers, len(doms)))
    log("  [jsprobe] 无头补抓 %d 个域名（%d 进程 × %d tab，浏览器 %s）"
        % (len(doms), len(parts), ntabs,
           os.path.basename(exe) if exe else "playwright-chromium"), flush=True)
    out, failed = {}, []
    try:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=len(parts)) as ex:
            futs = [(i, part, ex.submit(_worker, part, ntabs, timeout, exe))
                    for i, part in enumerate(parts)]
            for i, part, f in futs:
                try:
                    out.update(f.result())
                except Exception as e:
                    log("  [jsprobe] 分片 %d 异常：%s" % (i + 1, str(e)[:100]), flush=True)
                    failed.append(part)
                log("  [jsprobe] 进度 %d/%d 分片，已拿到 %d 个标题"
                    % (i + 1, len(parts), len(out)), flush=True)
    except Exception as e:
        log("  [jsprobe] 进程池不可用（%s），降级为单进程" % str(e)[:80], flush=True)
        failed = parts
    # 崩掉的分片串行重试一次（单进程 + 少 tab，稳但慢）：实测崩溃是偶发的，重试基本能过
    if failed:
        flat = [d for part in failed for d in part if d not in out]
        log("  [jsprobe] 重试 %d 个崩掉/未拿到的域名（单进程）" % len(flat), flush=True)
        try:
            out.update(_worker(flat, min(4, ntabs), timeout, exe))
        except Exception as e:
            log("  [jsprobe] 重试也失败：%s" % str(e)[:100], flush=True)
    log("  [jsprobe] 完成：%d/%d 拿到标题" % (len(out), len(doms)), flush=True)
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print("用法: python jsprobe.py <domain1> <domain2> ...")
        sys.exit(1)
    r = probe_many(args, workers=min(4, len(args)), ntabs=4)
    for d, (t, desc) in r.items():
        print("%-40s | %s" % (d, (t or "(无标题)")[:70]))
    print("未拿到:", [d for d in args if d not in r])
