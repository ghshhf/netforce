# -*- coding: utf-8 -*-
"""netforce / render_parse.py —— 无头完整解析引擎（发现 → 渲染 → 解析 → 细分类 → 回灌）

解决的核心问题：
    老链路是「静态抓 title → 抓不到就贴 JS渲染 标签」，41320 条里 38608 条堆在那一类。
    本引擎把「抓不到」变成「抓全」：真浏览器渲染，把 JS 执行后才有的一切都收下来。

一页收什么（完整解析）：
    · 直连优先 / 代理兜底两套浏览器通道（netpolicy，代理死了自动摘除）
    · DOM：title / description / OG / JSON-LD / html lang / H1-H3 / 正文全文
    · 链接：全部 a[href]，抽出新域名回灌发现队列（更多资源的闭环）
    · 网络：page.on("response") 收全部响应 ⇒ JS 触发的 XHR、懒加载图片、
      脚本里才拼出来的下载链接，都不会再漏
    · 懒加载：滚到底 3 轮触发 IntersectionObserver，再等 networkidle（有上限）
    · 解析完立刻 fine_classify.tag()，多维标签随记录一起落盘（直接分类）

可靠性 buff（全部叠上）：
    · SQLite 断点续跑：`_kb/state.db`，已解析域名重启不重来（等价 Scrapy JOBDIR）
    · 分片 append-only jsonl：写一半崩了也不毁已有记录
    · 分批重启浏览器：本机 Chrome 久挂偶发 STATUS_BREAKPOINT，每 batch 重开
    · 崩掉的分片单进程串行重试一次
    · 并发用本机实测甜点值 3 进程 × 5 tab（进浏览器前先 TCP 连接预筛，死站秒级判定，
      不浪费浏览器超时；DoH 映射域跳过预筛，交给浏览器直连真实 IP）
    · 单例锁：不会跑出两个渲染器互相抢
    · 配置热加载：改 `_netpolicy.json` 不用重启
    · **代码热重启**：--loop 每轮查 .py mtime，变了就做完当前批次优雅退出，
      由 watchdog 用新代码拉起；进度在 SQLite，不丢不重

用法：
    python render_parse.py --limit 300                # 一轮 300 条
    python render_parse.py --loop --limit 200         # 常驻伴跑（推荐）
    python render_parse.py --domains a.com b.com      # 指定域名自测
    python render_parse.py --status                   # 只看进度，不联网
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.parse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

import netpolicy
import fine_classify
import browser_pick     # 无头浏览器启动实测择优（系统 Chrome 会自退，别写死）

BASE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(BASE, "_kb")
RECORDS = os.path.join(KB, "records")
STATE_DB = os.path.join(KB, "state.db")
DISCOVERED = os.path.join(KB, "discovered.jsonl")
LOCK_FILE = os.path.join(KB, "render.lock")
RELOAD_FLAG = os.path.join(KB, "RELOAD_REQUEST")
RES_JSON = os.path.join(BASE, "resources.json")
CLS_JSON = os.path.join(BASE, "classified.json")

WATCHED_CODE = ["netpolicy.py", "fine_classify.py", "render_parse.py"]

BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Users\21393\AppData\Local\ms-playwright\chromium-1228\chrome-win\chrome.exe",
]

# 这些域不是「新资源」，是每页都有的公共噪声，回灌前先滤掉
NOISE_HOSTS = {
    "w3.org", "schema.org", "google.com", "www.google.com", "googletagmanager.com",
    "googleapis.com", "gstatic.com", "facebook.com", "twitter.com", "x.com",
    "linkedin.com", "instagram.com", "youtube.com", "doubleclick.net",
    "cloudflare.com", "jquery.com", "gravatar.com", "fonts.googleapis.com",
    "creativecommons.org", "archive.org", "about.me", "bit.ly", "t.co",
    "example.com", "localhost", "127.0.0.1", "javascript", "mailto",
}
ASSET_EXT = re.compile(
    r"\.(pdf|zip|7z|rar|tar|gz|tgz|csv|tsv|json|jsonl|parquet|arrow|xlsx|xls|"
    r"epub|mobi|djvu|mp3|wav|flac|ogg|mp4|mkv|avi|webm|png|jpe?g|webp|svg|gif|"
    r"gguf|safetensors|onnx|ckpt|pth|pt|bin|exe|msi|apk|dmg|deb|rpm|iso|"
    r"md|rst|docx|pptx|py|ipynb|srt|vtt)(\?|#|$)", re.I)

JS_EXTRACT = r"""() => {
  const q = (s) => document.querySelector(s);
  const txt = (e) => (e ? (e.textContent || '').replace(/\s+/g, ' ').trim() : '');
  const meta = (sel, attr) => { const m = q(sel); return m ? (m.getAttribute(attr) || '') : ''; };
  const og = {};
  document.querySelectorAll('meta[property^="og:"],meta[name^="twitter:"]').forEach(m => {
    const k = m.getAttribute('property') || m.getAttribute('name');
    if (k) og[k] = (m.content || '').slice(0, 300);
  });
  let ld = [];
  try {
    ld = [...document.querySelectorAll('script[type="application/ld+json"]')]
      .map(s => (s.textContent || '').slice(0, 3000)).slice(0, 5);
  } catch (e) {}
  const main = q('main') || q('article') || q('[role=main]') || q('#content') || document.body;
  let text = '';
  try {
    const c = main.cloneNode(true);
    c.querySelectorAll('script,style,noscript,svg,nav,footer,header,aside,form')
      .forEach(e => e.remove());
    text = (c.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 30000);
  } catch (e) { text = (document.body.innerText || '').slice(0, 30000); }
  const links = [...document.querySelectorAll('a[href]')].slice(0, 400).map(a => ({
    url: a.href, text: (a.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80)
  }));
  const headings = [...document.querySelectorAll('h1,h2,h3')].slice(0, 25)
    .map(h => txt(h).slice(0, 120)).filter(Boolean);
  return {
    title: (document.title || '').trim().slice(0, 300),
    desc: meta('meta[name=description]', 'content') ||
          meta('meta[property="og:description"]', 'content'),
    keywords: meta('meta[name=keywords]', 'content'),
    lang: document.documentElement.lang || '',
    og: og, jsonld: ld, text: text, links: links, headings: headings
  };
}"""


# ---------------------------------------------------------------- 状态层
def ensure_dirs():
    os.makedirs(RECORDS, exist_ok=True)
    conn = sqlite3.connect(STATE_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS done (
        domain TEXT PRIMARY KEY, ts REAL, ok INTEGER, channel TEXT, quality INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fed (
        value TEXT PRIMARY KEY, kind TEXT, ts REAL)""")
    # 一次性把「升级前已经写进 discovered.jsonl」的行认作已回灌，
    # 否则第一次去重判断会把老行又写一遍。
    seeded = conn.execute("SELECT v FROM meta WHERE k='fed_seeded'").fetchone()
    if not seeded and os.path.exists(DISCOVERED):
        try:
            with open(DISCOVERED, encoding="utf-8") as f:
                for line in f:
                    try:
                        o = json.loads(line)
                        conn.execute("INSERT OR IGNORE INTO fed(value,kind,ts) "
                                     "VALUES(?,?,?)",
                                     (o.get("value", ""), o.get("type", ""),
                                      o.get("ts", 0)))
                    except Exception:
                        continue
            conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('fed_seeded','1')")
        except Exception:
            pass
    conn.commit()
    conn.close()


def done_set():
    conn = sqlite3.connect(STATE_DB)
    rows = conn.execute("SELECT domain FROM done").fetchall()
    conn.close()
    return {r[0] for r in rows}


def _tag_quality(t):
    """从 tags 里取质量分，容错。

    tags 的约定是 dict（fine_classify.tag 的返回值）。2026-11-05 我把一条资源记录
    的 tags 写成了 list，mark_done 里 .get 直接抛 AttributeError，
    整轮 33 条已渲染结果连带进程一起没了 —— 一条脏记录不该有这种杀伤力。
    这里不猜类型，取不到就按 0 记，坏数据留在记录里由下游统计暴露。
    """
    if isinstance(t, dict):
        try:
            return int(t.get("quality") or 0)
        except Exception:
            return 0
    return 0


def mark_done(records):
    conn = sqlite3.connect(STATE_DB)
    conn.executemany(
        "INSERT OR REPLACE INTO done(domain,ts,ok,channel,quality) VALUES(?,?,?,?,?)",
        [(r.get("domain") or "", time.time(), 1 if r.get("ok") else 0,
          r.get("channel", ""), _tag_quality(r.get("tags")))
         for r in records])
    conn.commit()
    conn.close()


def status():
    if not os.path.exists(STATE_DB):
        print("尚未初始化（没跑过解析）")
        return
    conn = sqlite3.connect(STATE_DB)
    total = conn.execute("SELECT COUNT(*) FROM done").fetchone()[0]
    ok = conn.execute("SELECT COUNT(*) FROM done WHERE ok=1").fetchone()[0]
    by_ch = conn.execute("SELECT channel,COUNT(*) FROM done GROUP BY channel").fetchall()
    by_q = conn.execute("SELECT COUNT(*) FROM done WHERE quality>=55").fetchone()[0]
    conn.close()
    files = glob.glob(os.path.join(RECORDS, "*.jsonl"))
    print("已解析 %d 个域名 | 成功 %d | 高价值 %d | 记录分片 %d"
          % (total, ok, by_q, len(files)))
    print("通道分布:", dict(by_ch))
    print("待解析域名:", len(pending_domains(limit=10 ** 9)))


def _load_json(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _done_rows():
    conn = sqlite3.connect(STATE_DB)
    rows = conn.execute("SELECT domain,ts,ok FROM done").fetchall()
    conn.close()
    return rows


def discovered_domains():
    """渲染出的新域名回灌（闭环的真正入口）。只读 domain 行，asset 行交给下载器。"""
    out = []
    if not os.path.exists(DISCOVERED):
        return out
    try:
        with open(DISCOVERED, encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") != "domain":
                    continue
                d = (o.get("value") or "").strip().lower()
                if d and "." in d and " " not in d:
                    out.append(d)
    except Exception:
        pass
    return out


def discovered_urls():
    """站内内容页回灌：value 形如 "host/path"，与 done 表主键同形 ⇒ 天然去重。

    这是「深度」的唯一入口：没有它，每个域永远只抓得到首页一张导航页。
    """
    out = []
    if not os.path.exists(DISCOVERED):
        return out
    try:
        with open(DISCOVERED, encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") != "url":
                    continue
                v = (o.get("value") or "").strip().lower()
                if not v or "/" not in v or " " in v:
                    continue
                host = v.split("/", 1)[0]
                if "." in host and not host.endswith("."):
                    out.append(v)
    except Exception:
        pass
    return out


def pending_domains(limit, retry_after=6 * 3600, dead_retry=24 * 3600):
    """待解析队列，优先级从高到低：

      0  渲染发现的新域名（冷门资源的富矿，先解析）
      1  存量里「无标题 / JS渲染」的域（老分类的垃圾桶）
      2  resources.json 里其余未解析的域
      3  曾经渲染失败的域，超过 retry_after 秒后**有限重试**（排在最后 + 短超时）

    done 表里 ok=0 的记录以前被永久跳过 ⇒ 一次网络抖动就永远失去这个域。
    现在按时间窗重试，但保持「失败优先靠后、短超时」以免拖垮整批。
    """
    now = time.time()
    done = _done_rows()
    done_map = {d: (ts, ok) for d, ts, ok in done}
    done_set_ = set(done_map)
    res = _load_json(RES_JSON, [])
    cls = _load_json(CLS_JSON, [])
    bad = {(x.get("domain") or "").lower() for x in cls
           if (not x.get("title")) or "JS渲染" in x.get("category", "")}
    hard = set()
    try:
        with open(os.path.join(KB, "direct_fail.txt"), encoding="utf-8") as f:
            hard = {ln.strip() for ln in f if ln.strip()}
    except Exception:
        pass

    scored, seen = [], set()

    def add(d, u, prio, is_hard):
        d = (d or "").lower().strip()
        if not d or "." not in d or " " in d or d in seen:
            return False
        seen.add(d)
        scored.append((prio, bool(is_hard), d, u or ("https://" + d + "/")))
        return True

    # 0) 发现队列（去重，且已经解析过的不再进）
    for d in discovered_domains():
        if d not in done_set_:
            add(d, "", 0, d in hard)
    # 0.5) 站内内容页（深度）：value 形如 host/path，与 done 主键同形，天然断点续跑。
    #      限量：最多占本轮 40%，否则一次灌满会让「域名广度」永远轮不到。
    deep_cap = max(1, int(limit * 0.4))
    deep_n = 0
    if deep_cap > 0:
        for k in discovered_urls():
            if deep_n >= deep_cap:
                break
            if k in done_set_:
                continue
            host = k.split("/", 1)[0]
            # prio=-1：站内页绝对优先（已知站点里的文章，价值远高于未知新域），
            # 但条数被上面的 deep_cap 卡死（默认本轮 40%），不会挤掉域名广度。
            if add(k, "https://" + k, -1, host in hard):
                deep_n += 1
    # 1) 主爬虫的存量
    for x in res:
        d = (x.get("domain") or "").lower().strip()
        if d in done_set_:
            continue
        add(d, x.get("url") or "", 2 if d in bad else 3, d in hard)
    # 2) 失败重试：只在时间窗到了之后，且标注 hard 走短超时
    #    死站（HTTP 连不上）冷却更长：不删域，只是别每轮都重复付一次超时，
    #    窗口过后照样进队列重试——「能爬到就还是资源」。
    dead_set = set()
    try:
        conn = sqlite3.connect(STATE_DB)
        for (d,) in conn.execute(
                "SELECT domain FROM done WHERE ok=0 AND channel LIKE '%/dead'"):
            if d:
                dead_set.add(d.lower())
        conn.close()
    except Exception:
        pass
    for d, (ts, ok) in done_map.items():
        if ok:
            continue
        need = dead_retry if (d or "").lower() in dead_set else retry_after
        if now - ts < need:
            continue
        add(d, "", 4, True)
    scored.sort()
    return [{"domain": d, "url": u, "hard": h}
            for _p, h, d, u in scored[:limit]]


def code_signature():
    out = {}
    for n in WATCHED_CODE:
        try:
            out[n] = os.path.getmtime(os.path.join(BASE, n))
        except Exception:
            out[n] = 0
    return out


# ---------------------------------------------------------------- 单例锁
def acquire_lock():
    os.makedirs(KB, exist_ok=True)
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, encoding="utf-8") as f:
                old = json.load(f)
            pid, ts = int(old.get("pid", 0)), float(old.get("ts", 0))
            import psutil
            if psutil.pid_exists(pid) and time.time() - ts < 6 * 3600:
                alive = False
                try:
                    p = psutil.Process(pid)
                    alive = any("render_parse.py" in str(c) for c in (p.cmdline() or []))
                except Exception:
                    pass
                if alive:
                    print("已有渲染器在跑（PID %d），本实例退出" % pid)
                    return False
        except Exception:
            pass
        try:
            os.remove(LOCK_FILE)
        except Exception:
            pass
    with open(LOCK_FILE, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "ts": time.time()}, f)
    return True


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except Exception:
        pass


# ---------------------------------------------------------------- 渲染 worker
def _pick_exe(gpu=False):
    """选浏览器。gpu=True 时**必须用完整 Chromium**（chrome.exe），
    不能用 Playwright 默认的 chrome-headless-shell —— 那个壳的 GPU 进程是裁剪版。
    gpu=False 走原来的实测择优（哪个起得来用哪个）。"""
    if gpu:
        exe = browser_pick.find_gpu_browser()
        if exe:
            return exe
    return browser_pick.pick(BROWSER_CANDIDATES)


def _host(url):
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _is_asset(url, ctype=""):
    if ASSET_EXT.search(urllib.parse.urlsplit(url).path + "?"):
        return True
    c = (ctype or "").lower()
    return any(t in c for t in ("pdf", "zip", "octet-stream", "csv", "audio/", "video/"))


# https 失败后值得再试 http 的错误：协议/端口层面可能确实不同。
# 超时不在其中 —— 服务端在应答只是慢，换 scheme 等于把同一份预算再烧一遍。
_SCHEME_RETRY_HINTS = (
    "ERR_CONNECTION_REFUSED", "ERR_SSL", "ERR_CERT",
    "ERR_EMPTY_RESPONSE", "ERR_HTTP_RESPONSE_CODE_FAILURE",
    "ERR_ADDRESS_UNREACHABLE", "ERR_CONNECTION_RESET",
)


# ---------------------------------------------------------------- 站内深挖
# 以前每个域只抓首页 `/`，站内几百篇文章一条都不入队 —— 队列的「深度」是 0。
# 首页通常只是导航页，真正的知识在文章页；不往站内走，资源量永远只有域的数量级。
# 下面这套规则负责从页面链接里挑出「像内容页」的 URL，宁缺毋滥：
#   · 只取同域（含子域），跨域交给 new_domains 走域名发现
#   · 排除导航/分页/标签/登录/静态文件，这些抓了也不产知识
#   · 加权：路径含 blog/docs 等 + 带日期 + slug 含连字符 ⇒ 越像文章越靠前
_CONTENT_HINT = re.compile(
    r"/(blog|post|posts|article|articles|news|docs?|wiki|tutorial|tutorials|"
    r"guide|guides|learn|note|notes|story|stories|archive|read|"
    r"tech|dev|program|programming|code|coding|handbook|manual|reference)(/|$)",
    re.I)
_SKIP_PATH = re.compile(
    r"/(tag|tags|category|categories|page|paged|search|login|signin|signup|logout|"
    r"register|cart|checkout|user|users|account|profile|settings|admin|"
    r"author|authors|people|member|members|contributor|contributors|staff|team|"
    r"wp-admin|wp-login|wp-content|feed|rss|atom|rssfeed|atomfeed|newsfeed|"
    r"print|share|comment|comments|"
    r"reply|trackback|cdn-cgi|amp|sitemap)(/|$|\?|\.)", re.I)
_DATE_SEG = re.compile(r"/(19|20)\d{2}/(0?[1-9]|1[0-2])?")
_BAD_EXT = re.compile(
    r"\.(jpg|jpeg|png|gif|webp|svg|ico|css|js|json|xml|pdf|zip|rar|7z|"
    r"mp4|mp3|avi|mov|flv|exe|msi|dmg|apk|woff2?|ttf|eot)(\?|$)", re.I)


def site_urls(base_url, links, limit=8):
    """从页面链接里挑站内「值得深挖」的内容页。

    返回 [{"key": "host/path", "url": "https://host/path"}, ...]。
    key 同时是 done 表的主键形式 ⇒ 天然去重、天然断点续跑，不需要新表。
    """
    try:
        base = urllib.parse.urlsplit(base_url)
        host = (base.hostname or "").lower()
    except Exception:
        return []
    if not host:
        return []
    cand, seen = [], set()
    for l in (links or [])[:400]:
        raw = (l.get("url") if isinstance(l, dict) else l) or ""
        raw = raw.strip()
        if not raw or raw.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        try:
            u = urllib.parse.urljoin(base_url, raw)
            p = urllib.parse.urlsplit(u)
        except Exception:
            continue
        if p.scheme not in ("http", "https"):
            continue
        h = (p.hostname or "").lower()
        if not h or (h != host and not h.endswith("." + host)):
            continue
        path = p.path or "/"
        if path in ("", "/") or _BAD_EXT.search(path) or _SKIP_PATH.search(path):
            continue
        segs = [s for s in path.split("/") if s]
        if not segs:
            continue
        # 单段路径也可能是文章（/welcome-to-the-cloudflare-blog/），
        # 靠 slug 特征放行；否则一级目录（/about、/docs）一律当栏目页跳过。
        if len(segs) < 2 and not ("-" in segs[0] and len(segs[0]) >= 12):
            continue
        key = h + path.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        score = 0
        if _CONTENT_HINT.search(path):
            score += 3
        if _DATE_SEG.search(path):
            score += 2
        last = segs[-1]
        if "-" in last and len(last) >= 10:
            score += 2
        elif len(last) >= 20:
            score += 1
        if len(segs) == 2:
            score += 1
        if len(segs) > 5:
            score -= 1
        if score >= 2:                    # 阈值：至少两个「像文章」的信号
            cand.append((score, key, "https://" + h + path))
    cand.sort(key=lambda x: (-x[0], len(x[1])))
    out, dedup = [], set()
    for _s, key, u in cand:
        if key in dedup:
            continue
        dedup.add(key)
        out.append({"key": key, "url": u})
        if len(out) >= limit:
            break
    return out


def _scheme_retry_worth(exc):
    """https 失败后要不要改用 http 再试一次。"""
    if "Timeout" in type(exc).__name__:
        return False
    msg = str(exc)
    if "Timeout" in msg or "timeout" in msg:
        return False
    return any(h in msg for h in _SCHEME_RETRY_HINTS)


def _text_density(text):
    """浏览器 innerText 的正文密度启发式：短行 / 导航行占比越高越像壳。

    trafilatura 那类库靠 DOM 树打分；浏览器端我们只有 innerText，
    所以按行做轻量版：短且命中导航词的行不计入正文。只用于质量分与排序，
    绝不删正文。
    """
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return 0.0
    nav = ("home", "about", "login", "sign up", "menu", "contact", "copyright",
           "all rights reserved", "cookie", "privacy", "terms", "首页", "导航",
           "登录", "更多", "关于我们")
    keep = [l for l in lines if len(l) >= 40
            or (len(l) >= 15 and not any(w in l.lower() for w in nav))]
    return round(sum(len(l) for l in keep) / max(1, sum(len(l) for l in lines)), 3)


def _build_rec(dom, url, scheme, browser_ch, data, responses, main_resp,
               final_url="", degraded=False, err=""):
    """把一次提取结果组装成记录；正常路径与降级抢救共用，避免两套逻辑漂移。

    degraded=True 表示「goto 没走完，但 DOM 已有可用内容」，质量上要可追溯，
    不许当成完整成功来统计。
    """
    status_code = main_resp.status if main_resp else 0
    assets, xhr = [], []
    for u, st, ct, rt in responses:
        if rt in ("xhr", "fetch"):
            xhr.append({"url": u[:400], "status": st})
        if _is_asset(u, ct):
            assets.append({"url": u[:400], "type": rt, "ctype": ct[:60]})
    links = data.get("links") or []
    new_domains = []
    for l in links:
        h = _host(l.get("url", ""))
        if (h and h != dom and not h.startswith(dom + ".")
                and h not in NOISE_HOSTS
                and not any(h.endswith("." + n) for n in NOISE_HOSTS)):
            new_domains.append(h)
    new_domains = sorted(set(new_domains))[:120]
    text = data.get("text") or ""
    try:                       # 地域判据与 HTTP 一级漏斗共用一套，避免两套口径
        import http_first as _hf
        reg = _hf._region_fields(dom)
    except Exception:
        reg = {"region": "", "region_basis": ""}
    try:
        lm = ((main_resp.headers or {}).get("last-modified", "") if main_resp else "")
    except Exception:
        lm = ""
    # 站内深挖：只有内容够厚的页面才值得往下走（纯导航页展开的也还是导航页）
    deep = 0
    try:
        deep = int(netpolicy.cfg().get("deep_per_site", 8))
    except Exception:
        deep = 8
    su = site_urls(url, links, limit=deep) if (deep and len(text) >= 1500) else []
    rec = {
        "domain": dom, "url": url, "final_url": final_url or url,
        "ok": True, "status": status_code, "channel": browser_ch,
        "scheme": scheme, "rendered": True, "degraded": bool(degraded),
        "title": data.get("title", ""), "desc": data.get("desc", ""),
        "keywords": data.get("keywords", ""), "lang": data.get("lang", ""),
        "og": data.get("og", {}), "jsonld": data.get("jsonld", []),
        "structured": bool(data.get("og") or data.get("jsonld")),
        "headings": data.get("headings", []),
        "text": text[:200000], "text_len": len(text[:200000]),
        "text_density": _text_density(text),
        "region": reg["region"], "region_basis": reg["region_basis"],
        "egress": "tunnel" if "tunnel" in str(browser_ch).lower() else "direct",
        "last_modified": lm,
        "links": links[:300], "new_domains": new_domains,
        "site_urls": su,
        "assets": assets[:200], "xhr_count": len(xhr),
        "xhr": xhr[:60], "response_count": len(responses),
        "ts": time.time(),
    }
    if err:
        rec["err"] = err
    rec["tags"] = fine_classify.tag(rec)
    return rec


async def _salvage(pg, dom, url, scheme, browser_ch, responses, err):
    """goto 失败后抢救：只要页面已经导航且有标题/正文，就降级提取。

    返回 None 表示确实空白页，不值得记（交给上层判失败）。
    """
    try:
        cur = pg.url or ""
    except Exception:
        return None
    if (not cur) or cur == "about:blank" or cur.startswith("data:"):
        return None
    try:
        data = await pg.evaluate(JS_EXTRACT)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    title = (data.get("title") or "").strip()
    text = data.get("text") or ""
    if not title and len(text) < 80:
        return None
    return _build_rec(dom, url, scheme, browser_ch, data, responses, None,
                      final_url=cur, degraded=True, err=err)


async def _render_one(ctx, sem, item, timeout, browser_ch):
    """渲染单页：直连/代理由调用方选好的 ctx 决定。返回一条完整记录。"""
    dom = item["domain"]
    last_err = ""
    # 只信**真实渲染失败账本**（item["hard"]）；netprobe 矩阵是诊断参考，
    # 可能测自另一个网络域（Bash 沙箱 vs 宿主机），拿它缩短超时会误杀
    # 本来能直连的国外站。DoH 真实 IP 增强由 browser_args 负责，不在这里。
    eff_timeout = min(timeout, 6) if bool(item.get("hard")) else timeout
    async with sem:
        # 🔴 渲染目标必须尊重 item["url"]。
        # 原实现写死 "%s://%s/" % (scheme, dom)，等于「不管你要抓哪一页，
        # 我都渲染这个域的首页」。对主爬虫的域种子无害（本来就是首页），
        # 但把站内深挖（domain/路径 的 url 类种子）和定向抓取一起坑了：
        # 实测 huxiu 8 个文章 URL 渲染回来全是首页，8 条记录去重成 1 条。
        seed_url = (item.get("url") or "").strip()
        cands = []
        if seed_url.startswith("http"):
            cands.append(seed_url)
            # 只有当种子本身就是站点根时，才补另一个协议兜底
            # （保持"https 不通就试 http"的原有行为）
            tail = seed_url.split("://", 1)[-1].rstrip("/")
            if tail in ("", dom):
                other = "http" if seed_url.startswith("https") else "https"
                cands.append("%s://%s/" % (other, dom))
        else:
            cands = ["https://%s/" % dom, "http://%s/" % dom]
        for url in cands:
            scheme = "https" if url.startswith("https") else "http"
            pg = None
            responses = []

            def _on_resp(r):
                # 回调里不能抛异常，否则 Playwright 会把整页判失败
                try:
                    responses.append((r.url, r.status,
                                      (r.headers or {}).get("content-type", ""),
                                      r.request.resource_type))
                except Exception:
                    try:
                        responses.append((r.url, 0, "", ""))
                    except Exception:
                        pass

            try:
                pg = await ctx.new_page()
                pg.on("response", _on_resp)
                main_resp = await pg.goto(url, timeout=eff_timeout * 1000,
                                          wait_until="domcontentloaded")
                # networkidle 有上限：SPA 会挂着长连接，等死不划算
                try:
                    await pg.wait_for_load_state("networkidle", timeout=2500)
                except Exception:
                    pass
                await pg.wait_for_timeout(700)
                # 懒加载：滚到底 3 轮，触发 IntersectionObserver 才出现的图片/列表
                for _ in range(3):
                    try:
                        await pg.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                        await pg.wait_for_timeout(350)
                    except Exception:
                        break
                data = await pg.evaluate(JS_EXTRACT)
                return _build_rec(dom, url, scheme, browser_ch, data, responses,
                                  main_resp, final_url=pg.url)
            except Exception as e:
                last_err = "%s: %s" % (type(e).__name__, str(e)[:90])
                # 🔴 降级抢救：goto 超时/被中断时页面往往已经渲染出内容，
                # 直接 continue 等于白烧一次完整预算（实测 500 域里 290 条
                # 渲染失败，其中相当一部分是"慢站但 DOM 已就绪"）。
                if pg is not None:
                    try:
                        rec = await asyncio.wait_for(
                            _salvage(pg, dom, url, scheme, browser_ch,
                                     responses, last_err),
                            timeout=8)
                        if rec:
                            return rec
                    except Exception:
                        pass
                if not _scheme_retry_worth(e):
                    break
                continue
            finally:
                try:
                    if pg:
                        await pg.close()
                except Exception:
                    pass
    fail = {"domain": dom, "url": item.get("url", ""), "ok": False,
            "status": 0, "channel": browser_ch, "rendered": False,
            "title": "", "desc": "", "text": "", "text_len": 0,
            "headings": [], "links": [], "new_domains": [], "assets": [],
            "xhr_count": 0, "structured": False,
            "err": last_err or "all channels failed", "ts": time.time()}
    fail["tags"] = fine_classify.tag(fail)
    return fail


def _reachable(dom, timeout=2.5):
    """TCP 连接预筛：443/80 任一能连即视为可达。DoH 映射域由调用方跳过，
    交给浏览器用 --host-resolver-rules 直连真实 IP。

    🔴 关键坑：socket.create_connection 的 timeout 只管 connect，**不管 getaddrinfo
    （DNS 解析）**。被污染/无响应的域名会让 DNS 解析永久阻塞，而 asyncio.to_thread
    把它丢进线程池 → 整批卡死（实测 7 分钟不动）。setdefaulttimeout 同时约束 DNS
    解析，把挂死限制在 timeout 内。本函数在独立 worker 子进程里执行，不影响主进程。
    """
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    deadline = time.time() + timeout          # 总预算，不是每个端口各一份
    try:
        for port in (443, 80):
            left = deadline - time.time()
            if left <= 0.2:
                break
            try:
                s = socket.create_connection((dom, port), timeout=left)
                s.close()
                return True
            except Exception:
                continue
    finally:
        socket.setdefaulttimeout(old)
    return False


async def _one_batch(browser_direct, browser_proxy, ctxs, chunk, ntabs, timeout, out):
    sem = asyncio.Semaphore(ntabs)
    ctx_direct, ctx_proxy = ctxs
    # DoH 映射缓存：有映射的域跳过 TCP 预筛，交给浏览器直连真实 IP
    dip_cache = {}
    try:
        import direct_ip
        dip_cache = direct_ip._load()
    except Exception:
        dip_cache = {}

    async def job(item):
        dom = item["domain"]
        # 连接预筛：死站秒级判定，省掉昂贵的浏览器超时。DoH 映射域跳过预筛，
        # 交给浏览器用 --host-resolver-rules 直连真实 IP（dip_cache 顶层已加载）。
        try:
            if (dom not in dip_cache) and not await asyncio.to_thread(_reachable, dom, 2.5):
                rec = {"domain": dom, "url": item.get("url", ""), "ok": False,
                       "status": 0, "channel": "direct", "rendered": False,
                       "title": "", "desc": "", "text": "", "text_len": 0,
                       "headings": [], "links": [], "new_domains": [],
                       "assets": [], "xhr_count": 0, "structured": False,
                       "err": "unreachable(tcp)", "ts": time.time()}
                rec["tags"] = fine_classify.tag(rec)
                return rec
        except Exception:
            pass
        # 🔴 固定规则选路、一次定死，不做运行时切换：
        #    失败就如实记失败，不偷偷换出口（长期自主运行要的是确定性）。
        ch_px = ""
        try:
            ch_px = netpolicy.channels(item["domain"])[0][1]
        except Exception:
            pass
        cap = max(45, int(timeout) + 30)
        if ch_px and ctx_proxy is not None:
            return await asyncio.wait_for(
                _render_one(ctx_proxy, sem, item, timeout, "proxy"), timeout=cap)
        return await asyncio.wait_for(
            _render_one(ctx_direct, sem, item, timeout, "direct"), timeout=cap)

    res = await asyncio.gather(*[job(it) for it in chunk], return_exceptions=True)
    for r in res:
        if isinstance(r, dict):
            out.append(r)
        elif isinstance(r, BaseException):
            out.append({"domain": "?", "ok": False, "err": str(r)[:100], "tags": {}})


def _worker(items, ntabs, timeout, exe, batch, gpu=False):
    """一个进程 = 直连 browser（+ 可选代理 browser）+ ntabs 并发 tab。

    分批重启：本机 Chrome 长时间挂着会偶发 STATUS_BREAKPOINT，每 batch 关掉重开，
    把爆炸半径限制在一小批内。worker 只返回记录，落盘统一由主进程做（避免多写竞争）。
    """
    async def _main():
        from playwright.async_api import async_playwright
        from concurrent.futures import ThreadPoolExecutor
        # _reachable 是同步 DNS/TCP 预筛，走 asyncio.to_thread；默认线程池只有
        # min(32, cpu+4)=20 条，而一个 chunk 有几十个域同时在预筛 ⇒ 死站把线程占满，
        # 活站排在后面干等。这里放大到 64，让预筛不再成为吞吐瓶颈。
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=64))
        out = []
        exe_kw = browser_pick.launch_kw(exe)   # "" ⇒ 不传 executable_path
        # 固定出口：默认 "" = 真直连（直连国外）。只有远程显式指定 / self_kernel
        # 才非空；绝不自动发现系统代理或本机其它端口，也不在批次间切换。
        px = netpolicy.browser_proxy()
        # 纯直连增强：对「已知普通直连失败」的域，用 DoH 真实 IP 给 Chromium 下
        # host-resolver-rules（直连 GitHub 那扇门）。不是代理，不碰本地端口。
        rules = ""
        if not px:
            try:
                import direct_ip
                rules = direct_ip.rules_for([it.get("domain", "") for it in items],
                                            log_fn=print)
            except Exception:
                rules = ""
        browser_args = netpolicy.browser_launch_args("", gpu=gpu)
        if rules:
            browser_args = browser_args + ["--host-resolver-rules=" + rules]
            print("  [render] DoH 真实 IP 直连映射 %d 条" % rules.count("MAP"), flush=True)
        chunks = [items[i:i + batch] for i in range(0, len(items), batch)]
        for ci, chunk in enumerate(chunks):
            try:
                async with async_playwright() as p:
                    try:
                        bd = await p.chromium.launch(
                            headless=True, **exe_kw, timeout=60000,
                            args=browser_args)
                    except Exception:
                        if gpu:
                            # 独显起不来先换后端（d3d11 → vulkan），两个都不行
                            # 才退回纯 CPU；顺序不能反：Windows 原生是 DirectX。
                            bd = None
                            for angle in ("d3d11", "opengl"):
                                try:
                                    browser_args = netpolicy.browser_launch_args(
                                        "", gpu=True, angle=angle)
                                    if rules:
                                        browser_args = (browser_args
                                                        + ["--host-resolver-rules=" + rules])
                                    bd = await p.chromium.launch(
                                        headless=True, **exe_kw, timeout=60000,
                                        args=browser_args)
                                    print("  [render] GPU 后端回退到 %s" % angle, flush=True)
                                    break
                                except Exception:
                                    continue
                            if bd is None:
                                browser_args = netpolicy.browser_launch_args("", gpu=False)
                                if rules:
                                    browser_args = (browser_args
                                                    + ["--host-resolver-rules=" + rules])
                                print("  [render] GPU 启动失败 ⇒ 回退 disable-gpu（CPU 高并发）",
                                      flush=True)
                                bd = await p.chromium.launch(
                                    headless=True, **exe_kw, timeout=60000,
                                    args=browser_args)
                        else:
                            raise
                    bp = None
                    if px:
                        try:
                            bp = await p.chromium.launch(
                                headless=True, **exe_kw, timeout=60000,
                                args=netpolicy.browser_launch_args(px),
                                proxy={"server": px})
                        except Exception:
                            bp = None
                    try:
                        cd = await bd.new_context(
                            user_agent=netpolicy.UA,
                            viewport={"width": 1366, "height": 900},
                            **netpolicy.browser_context_kw(""))
                        cp = None
                        if bp:
                            # launch 级已设 proxy，context 不再重复传（避免两套配置打架）
                            cp = await bp.new_context(
                                user_agent=netpolicy.UA,
                                viewport={"width": 1366, "height": 900})
                        await _one_batch(bd, bp, (cd, cp), chunk, ntabs, timeout, out)
                    finally:
                        for b in (bp, bd):
                            try:
                                if b:
                                    await b.close()
                            except Exception:
                                pass
            except Exception as e:
                print("  [render] 批次 %d 浏览器异常：%s" % (ci + 1, str(e)[:100]),
                      flush=True)
                # 崩掉的分片降级：单 tab 重试一次，尽量把这一批捞回来
                try:
                    async with async_playwright() as p:
                        b = await p.chromium.launch(
                            headless=True, **exe_kw, timeout=60000,
                            args=browser_args)
                        try:
                            c = await b.new_context(user_agent=netpolicy.UA)
                            sem = asyncio.Semaphore(2)
                            for it in chunk:
                                try:
                                    out.append(await _render_one(c, sem, it, timeout, "direct"))
                                except Exception:
                                    pass
                        finally:
                            await b.close()
                except Exception as e2:
                    print("  [render] 批次重试也失败：%s" % str(e2)[:80], flush=True)
        return out

    return asyncio.run(_main())


def _chunks(lst, n):
    n = max(1, n)
    size = (len(lst) + n - 1) // n
    return [lst[i:i + size] for i in range(0, len(lst), size)]


def render_stage(items, workers=3, ntabs=6, timeout=15, batch=20, gpu=False,
                 log=print):
    """只做浏览器渲染（不碰 HTTP 漏斗）。

    单独拆出来的意义：编排层可以让「本轮渲染」和「下一轮 HTTP 直取」并行跑。
    实测这两个阶段几乎没有资源竞争 —— HTTP 阶段是纯网络等待（CPU 20%、GPU 0%），
    渲染阶段才吃 CPU/独显。串行跑等于让整机一半时间空转。
    """
    if not items:
        return []
    out = []
    exe = _pick_exe(gpu)              # gpu ⇒ 完整 Chromium；否则 Playwright 默认壳
    px = netpolicy.browser_proxy()    # "" = 真直连（直连国外）
    log("  [render] 解析 %d 域（%d 进程 × %d tab，GPU=%s，浏览器 %s，出口 %s）"
        % (len(items), min(workers, len(items)), ntabs, "on" if gpu else "off",
           os.path.basename(exe) if exe else "playwright-chromium",
           px or "直连(固定，不切换)"), flush=True)
    parts = _chunks(items, min(workers, len(items)))
    failed = []
    try:
        with ProcessPoolExecutor(max_workers=len(parts)) as ex:
            futs = [(i, part, ex.submit(_worker, part, ntabs, timeout, exe, batch, gpu))
                    for i, part in enumerate(parts)]
            for i, part, f in futs:
                try:
                    out.extend(f.result())
                except Exception as e:
                    log("  [render] 分片 %d 异常：%s" % (i + 1, str(e)[:110]), flush=True)
                    failed.append(part)
                log("  [render] 进度 %d/%d 分片，已回 %d 条"
                    % (i + 1, len(parts), len(out)), flush=True)
    except Exception as e:
        log("  [render] 进程池不可用（%s），降级单进程" % str(e)[:80], flush=True)
        failed = parts
    if failed:
        flat = [x for part in failed for x in part]
        log("  [render] 单进程重试 %d 条" % len(flat), flush=True)
        try:
            out.extend(_worker(flat, 2, timeout, exe, batch, gpu))
        except Exception as e:
            log("  [render] 重试失败：%s" % str(e)[:80], flush=True)
    return out


def render_many(items, workers=3, ntabs=6, timeout=15, batch=20, gpu=False, log=print,
                http_first=True, http_conc=48, http_timeout=8.0):
    if not items:
        return []
    out = []
    # ---- 一级漏斗：轻量 HTTP 直取 ----
    # 实测 58.5% 的域名静态 HTML 里标题+正文就已经齐全，开浏览器纯属浪费；
    # 另有约 22% 连 TCP 都不通，浏览器同样救不了。这两类在这里就结案，
    # 只有「空壳（需要跑 JS）」才进下面那条昂贵链路。
    if http_first:
        try:
            import http_first as _hf
            solved, items = _hf.fetch_many(items, conc=http_conc,
                                           timeout=http_timeout, log=log)
            out.extend(solved)
        except Exception as e:
            log("  [http] 一级漏斗异常，全部交浏览器：%s" % str(e)[:90], flush=True)
        if not items:
            return out
        # 🔴 2026-11-09：静态没解决的别按发现顺序排队，按「浏览器能救回来的概率」
        # 排序 —— SPA 空壳 / 风控挑战最值得先烧渲染预算，连 TCP 都不通的
        # （retryable / timeout）排最后甚至不必开浏览器。借鉴 scrapy-playwright
        # 的按需渲染 + 优先级队列，只是这里用纯本地分数，不引入调度依赖。
        def _prio(it):
            try:
                p = it.get("render_priority")
            except AttributeError:
                p = None
            if p is None:
                why = str(it.get("why") or "").lower() if hasattr(it, "get") else ""
                p = 3
                if "spa" in why or "shell" in why:
                    p = 8
                if "403" in why or "cloudflare" in why or "challenge" in why:
                    p = 9
                if it.get("retryable") or "timeout" in why or "connect" in why:
                    p = 0
            return -int(p)
        try:
            items = sorted(items, key=_prio)
            log("  [render] 按渲染优先级重排 %d 条（空壳/风控优先）" % len(items), flush=True)
        except Exception as e:
            log("  [render] 优先级排序跳过：%s" % str(e)[:80], flush=True)
    return out + render_stage(items, workers, ntabs, timeout, batch, gpu, log)


# ---------------------------------------------------------------- 落盘 / 闭环
def _serialized(fn):
    """给会写盘/写库的函数加进程内互斥。

    流水线化之后，主线程（HTTP 结果）和后台渲染线程会同时调用 persist，
    两边都要写 records 分片 + discovered.jsonl + SQLite，必须串起来，
    否则 SQLite 会 "database is locked"、分片行也可能交错。
    """
    import functools
    lock = threading.Lock()

    @functools.wraps(fn)
    def wrap(*args, **kwargs):
        with lock:
            return fn(*args, **kwargs)
    return wrap


@_serialized
def persist(records):
    """分片 append-only 落盘 + 断点标记 + 新域名回灌发现队列。"""
    if not records:
        return 0
    stamp = time.strftime("%Y%m%d-%H")
    path = os.path.join(RECORDS, "rec-%s.jsonl" % stamp)
    known = done_set()
    fresh = [r for r in records if r.get("domain") and r["domain"] not in known]
    with open(path, "a", encoding="utf-8") as f:
        for r in fresh:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    # 失败账本：直连渲染失败的域记下来，后续批次用短超时 / 优先走出口
    with open(os.path.join(KB, "direct_fail.txt"), "a", encoding="utf-8") as f:
        for r in fresh:
            if not r.get("ok") and r.get("channel") == "direct":
                f.write(r["domain"] + "\n")
    mark_done(fresh)
    # 渲染成果补充库（append-only，不碰 classified.json）：
    # classify.py --recheck 正在写 classified.json，直接改会写冲突；这里先落独立补充库，
    # 等 --recheck 不在跑时再用 sync_classified.py 安全合并回去（带备份+锁+原子写）。
    cr_path = os.path.join(KB, "classified_render.jsonl")
    with open(cr_path, "a", encoding="utf-8") as f:
        for r in fresh:
            t = r.get("tags") or {}
            f.write(json.dumps({
                "domain": r["domain"], "title": r.get("title", ""),
                "desc": r.get("desc", ""), "text_len": r.get("text_len", 0),
                "status": r.get("status", 0), "ok": bool(r.get("ok")),
                "topics": t.get("topics", []), "forms": t.get("forms", []),
                "value": t.get("value", ""), "ts": r.get("ts", time.time()),
            }, ensure_ascii=False) + "\n")
    # 发现闭环：新域名 + 真有价值的资源 URL 进队列，供后续批次消费。
    # 🔴 去重：以前每次重解析同一站都会把同样的域名/图标再写一遍，队列会无限膨胀；
    #    fed 表做幂等，且消费端（download_assets.worth）的价值过滤在这里先做一遍，
    #    原始记录里的完整 network 观测数据仍原样保留，不磨损。
    try:
        import download_assets
        worthy = download_assets.worth
    except Exception:
        worthy = lambda u: bool(u)        # 拿不到过滤器时退回只按去重处理
    conn = sqlite3.connect(STATE_DB)
    lines = []
    for r in fresh:
        ts = r.get("ts", time.time())
        for d in r.get("new_domains") or []:
            lines.append(("domain", d, r["domain"], ts))
        # 站内内容页：kind=url，value 用 host/path（与 done 表主键同形）
        for s in r.get("site_urls") or []:
            k = (s.get("key") if isinstance(s, dict) else s) or ""
            if k and "/" in k:
                lines.append(("url", k, r["domain"], ts))
        for a in (r.get("assets") or [])[:40]:
            u = a.get("url") if isinstance(a, dict) else a
            if u and worthy(u):
                lines.append(("asset", u, r["domain"], ts))
    written = 0
    with open(DISCOVERED, "a", encoding="utf-8") as f:
        for kind, value, src, ts in lines:
            cur = conn.execute(
                "INSERT OR IGNORE INTO fed(value,kind,ts) VALUES(?,?,?)",
                (value, kind, ts))
            if cur.rowcount:
                f.write(json.dumps({"type": kind, "value": value,
                                    "from": src, "ts": ts},
                                   ensure_ascii=False) + "\n")
                written += 1
    conn.commit()
    conn.close()
    return len(fresh)


def summary(records):
    ok = [r for r in records if r.get("ok")]
    tags = [r.get("tags") or {} for r in ok]
    ch = Counter(r.get("channel", "") for r in records)
    forms = Counter(f for t in tags for f in t.get("forms", []))
    topics = Counter(t2 for t in tags for t2 in t.get("topics", []))
    newd = sum(len(r.get("new_domains") or []) for r in ok)
    assets = sum(len(r.get("assets") or []) for r in ok)
    xhr = sum(r.get("xhr_count", 0) for r in ok)
    titled = sum(1 for r in ok if (r.get("title") or "").strip())
    print("  完成 %d 条 | 渲染成功 %d | 拿到标题 %d | 通道 %s"
          % (len(records), len(ok), titled, dict(ch)))
    print("  新发现域名 %d | JS/静态资源 %d | XHR 端点 %d" % (newd, assets, xhr))
    print("  资源形态 TOP: %s" % (forms.most_common(8),))
    print("  主题标签 TOP: %s" % (topics.most_common(8),))
    print("  高价值记录 %d 条" % sum(1 for t in tags if t.get("value") == "高"))


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--workers", type=int, default=None,
                    help="手动覆盖进程数；不传则按 --mode 自动取")
    ap.add_argument("--tabs", type=int, default=None,
                    help="手动覆盖每进程 tab 数；不传则按 --mode 自动取")
    ap.add_argument("--mode", default="auto",
                    choices=["auto", "conservative", "aggressive"],
                    help="auto=按堆积量自动切（默认）；conservative=省资源；"
                         "aggressive=整机 CPU+GPU 拉满")
    ap.add_argument("--gpu", action="store_true",
                    help="强制开 GPU 加速（覆盖模式默认）")
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--domains", nargs="*", default=[])
    a = ap.parse_args()

    ensure_dirs()
    if a.status:
        status()
        return
    if not acquire_lock():
        return
    sig = code_signature()
    rounds = 0
    try:
        while True:
            rounds += 1
            netpolicy.reload()                     # 配置热加载：改文件不用重启
            if a.domains and rounds == 1:
                items = [{"domain": d.lower(), "url": "https://" + d.lower() + "/"}
                         for d in a.domains]
            else:
                items = pending_domains(a.limit)
            if not items:
                print("[render] 待解析队列已空；%s"
                      % ("休眠 60s 等主爬虫发现新域名" if a.loop else "退出"), flush=True)
                if not a.loop:
                    break
                time.sleep(60)
                continue
            print("[render] 第 %d 轮：%d 域 | %s"
                  % (rounds, len(items), netpolicy.describe()["config_file"]), flush=True)
            w, t, gpu, mode_used = netpolicy.concurrency_plan(a.mode)
            if a.workers:
                w = a.workers
            if a.tabs:
                t = a.tabs
            if a.gpu:
                gpu = True
            print("  [render] 并发模式=%s → workers=%d tabs=%d gpu=%s"
                  % (mode_used, w, t, "on" if gpu else "off"), flush=True)
            t0 = time.time()
            recs = render_many(items, w, t, a.timeout, a.batch, gpu=gpu)
            n = persist(recs)
            summary(recs)
            print("  本轮新落盘 %d 条，耗时 %.1fs" % (n, time.time() - t0), flush=True)
            if not a.loop:
                break
            # 代码热重启：改完当前批次再优雅退出，watchdog 用新代码拉起；进度在 SQLite
            if code_signature() != sig:
                with open(RELOAD_FLAG, "w", encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " code changed\n")
                print("[render] 检测到代码更新 ⇒ 完成本批后优雅退出，等待守护用新代码拉起",
                      flush=True)
                break
            time.sleep(3)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
