# -*- coding: utf-8 -*-
"""netforce / external_seeds.py —— 第二层：给自举闭环开「外生信息口」。

为什么必须有这一层：
    evolve.bootstrap_keywords() 从自己的 classified.json 抽词，
    bootstrap_search() 把结果又写回自己的库 —— 输入 S、输出 S，
    中间还过一道 25.8 万指纹去重。**这是闭合映射 S→S，收敛不是 bug，
    是数学必然**：种子太少，系统只能反复在书房里照着已有书名找书。

破局 = 注入集合外的信息。全部选免费、无需 key、不绑卡的公开数据源：
    ① crt.sh          证书透明日志：按 SAN 关键词挖搜索引擎没收录的子域
    ② Common Crawl    历史网页索引：按 **URL 形态**（*.edu/dataset）而非主题词
    ③ Wayback CDX     时间维度：老站 / 已下线资源的 URL 与子域
    ④ Wikipedia API   知识图谱的「外部链接」属性，天然带主题、多语种
    ⑤ sitemap/robots/RSS  站级发现：一次拿全站 URL，比搜首页高一个量级
    ⑥ Tranco 榜单差分  抓「新出现的」，而不是历史存量（默认关闭，手动开）
    ⑦ 结构查询词      filetype: / inurl: / intitle: —— 搜形态而不是搜主题

调度：多臂老虎机的 ε-greedy（借鉴 Scrapy 的AutoThrottle 自适应调度 +
      强化学习里的探索/利用权衡）：82% 预算按「新域名产出率」加权选源，
      18% 完全随机探索，保证冷门/新源不会被饿死；连续失败的源自动
      指数冷却、到期复活，只降权不删源。

纪律：
    · 全程走 _netpolicy.json 的 crawler_proxy（统一出口 18080），
      绝不读系统代理 / 外部自开端口；
    · 新域名先落 _kb/external_seeds.jsonl 再分批吐给爬虫 ⇒ 断点续传、
      与爬虫解耦，爬虫重启也不丢种子；
    · 按主机配额 + 全局去重，防大站淹没队列；
    · 不建定时任务、不开新端口、不改默认出网行为。

用法：
    python external_seeds.py --selftest        # 离线验证解析器，不联网
    python external_seeds.py --run 3           # 跑 3 轮外生源抓取并打印产出
    python external_seeds.py --stats           # 各源产出率 / 队列深度
    python external_seeds.py --take 20         # 从种子池取 20 条（爬虫调用的接口）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import random
import re
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(BASE, "_kb")
sys.path.insert(0, BASE)

POLICY = os.path.join(BASE, "_netpolicy.json")
POOL = os.path.join(KB, "external_seeds.jsonl")
CURSOR = os.path.join(KB, "external_seeds_cursor.json")
STATS = os.path.join(KB, "external_seeds_stats.json")
KB_DB = os.path.join(KB, "kb_index.db")
RES_JSON = os.path.join(BASE, "resources.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
URL_RE = re.compile(r"https?://[^\s<>\"'`\)\]]+", re.I)
DOM_RE = re.compile(r"^[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?"
                    r"(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)+$")
# 基础设施 / 社交 / 统计噪声：这些域再新也不是「资源站」
NOISE = ("google.", "facebook.", "twitter.", "instagram.", "youtube.",
         "linkedin.", "cdn.", "cloudflare.", "amazonaws.com", "gstatic.",
         "googleapis.", "doubleclick.", "w3.org", "schema.org",
         "creativecommons.org", "archive.org", "wikimedia.org",
         "gravatar.com", "fonts.googleapis", "analytics.", "adservice.",
         "microsoftonline.", "windows.net", "azure.", "office.com",
         "live.com", "akamai.", "msecnd.net")

# 结构形态查询：搜「页面长什么样」，不是搜「讲什么主题」
PATTERN_QUERIES = [
    'filetype:csv site:*.edu dataset',
    'inurl:/dataset/ open data',
    'intitle:"index of" datasets',
    '"swagger" "api" open source',
    'inurl:/api/v1/ documentation public',
    'filetype:json site:*.gov opendata',
    '"open source" "self-hosted" tools list',
    'inurl:/models/ gguf huggingface',
    '"awesome-" resources github.io',
    'inurl:/docs/ api reference free',
]

# 关键词必须「垂直」。实测用 docs / api / tools 这类泛词，crt.sh 会吐回
# 一大堆 CI 预览证书（docs---pr-375...platform.sh），全是噪声站。
CRTSH_WORDS = ("dataset", "opendata", "open data", "corpus", "repository",
               "archive", "huggingface", "modelscope", "dataverse",
               "zenodo", "figshare", "geodata", "openstreetmap", "llm")
CC_PATTERNS = (
    "*.edu/*dataset*", "*.gov/*opendata*", "*.org/*datasets/*",
    "*.ac.uk/*data*", "*.edu.cn/*resource*", "*/*/corpus/*",
    "*/*/models/*gguf*", "*/*/api/v1/*",
)
# 形态/主题提示：Common Crawl 按 urlkey 字典序返回，不加这道过滤，
# limit=300 拿回来的全是 0-100.org 这种完全无关的站。
_TOPIC_HINT = re.compile(
    r"(dataset|opendata|corpus|archive|model|api|docs|data|research|"
    r"library|repository|open[-_]?source|mirror|resource)", re.I)
_BAD_HOST = re.compile(
    r"(^|[.\-])(pr-|preview|review|staging|dev-|test-|ci-|build-|"
    r"sandbox|localhost|status|whois|epp|nic|autodiscover|webmail|"
    r"lyncdiscover|sip|meet|dialin|owa|exchange|smtp|imap|pop|mail|"
    r"mx|vpn|remote|ns\d+|ftp|cdn|static|con|legacy|cloud|"
    r"test|dev|preview|demo)([.\-]|$)", re.I)
_INFRA_SUFFIX = (".platform.sh", ".porter.run", ".herokuapp.com",
                 ".vercel.app", ".netlify.app", ".compute.amazonaws.com",
                 ".pages.dev", ".workers.dev", ".onrender.com",
                 ".repl.co", ".glitch.me", ".azurewebsites.net",
                 ".code.run", ".addon.code.run")
WIKI_WORDS = ("open data", "dataset repository", "digital library",
              "open source software", "scientific database")
# 该源默认关闭：Top-1M zip 约 10MB，晚高峰不值得自动拉，手动 --source tranco 才跑
DISABLED_DEFAULT = {"tranco"}
SOURCES = ("crtsh", "cc", "wayback", "wikipedia", "sitemap", "tranco")


def log(*a, **kw):
    print(*a, flush=True)


def cfg():
    try:
        with open(POLICY, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _http(url, timeout=12, raw=False):
    """统一出口；不读系统代理（ProxyHandler({}) = 真直连兜底）。"""
    px = (cfg().get("crawler_proxy") or "").strip()
    hs = [urllib.request.ProxyHandler({"http": px, "https": px} if px else {}),
          urllib.request.HTTPSHandler(context=ssl._create_unverified_context())]
    op = urllib.request.build_opener(*hs)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json,text/xml,text/html,application/rss+xml,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    with op.open(req, timeout=timeout) as r:
        b = r.read(4 * 1024 * 1024)
    return b if raw else b.decode("utf-8", "replace")


def _dom(host):
    h = (host or "").lower().strip().rstrip(".").lstrip("*.")
    if h.startswith("www."):
        h = h[4:]
    if not h or not DOM_RE.match(h) or len(h) > 100:
        return ""
    if any(n in h for n in NOISE):
        return ""
    return h


def _quality_ok(d, u="", need_hint=True):
    """外生域名的质量闸门：证书日志 / 网页索引里混着大量非资源站。

    实测踩坑（2026-11-09）：crt.sh 泛词命中一堆 `docs---pr-...platform.sh`
    CI 预览证书；CC Index 按 urlkey 字典序返回 `0--0.org`、`0-100.org`。
    这些「库外域名」在统计上是新增，实际是把爬虫往沟里带 ⇒ 必须先过滤。
    """
    if not d:
        return False
    if re.search(r"-{2,}", d) or d.count("-") > 5 or len(d.split(".")) > 5:
        return False
    if any(d.endswith(s) for s in _INFRA_SUFFIX) or _BAD_HOST.search(d):
        return False
    if re.match(r"^[0-9\-]+", d):              # 0-100.org / 0--0.org
        return False
    labels = d.split(".")[:-1]
    if not any(re.search(r"[a-z]{3,}", lab) for lab in labels):
        return False                            # 至少要有一段 ≥3 连字母
    tld = d.rsplit(".", 1)[-1]
    if not (2 <= len(tld) <= 6 and tld.isalpha()):
        return False
    # Common Crawl / Wayback 的 URL 必须真的带形态词，否则只是字典序噪声。
    # crt.sh 的 SAN 已被 q 关键词筛过一遍，不必再要求域名含形态词。
    if need_hint and not _TOPIC_HINT.search(d + " " + (u or "")):
        return False
    return True


_2LD = {"co", "com", "net", "org", "gov", "edu", "ac"}


def _registrable(d):
    """粗粒度注册域：geodata.soton.ac.uk → soton.ac.uk；geodata.no → geodata.no。"""
    p = d.split(".")
    if len(p) >= 3 and p[-2] in _2LD and len(p[-1]) == 2:
        return ".".join(p[-3:])
    return ".".join(p[-2:])


def _cap(pairs, per_reg=2):
    """按注册域配额，防一家公司的几十个子域把种子池淹没。"""
    out, cnt = [], {}
    for d, u in pairs:
        r = _registrable(d)
        if cnt.get(r, 0) >= per_reg:
            continue
        cnt[r] = cnt.get(r, 0) + 1
        out.append((d, u))
    return out


def _pairs(urls, need_hint=True):
    """URL 列表 → [(domain,url)]，按主机配额去重（同域最多 3 条）。

    need_hint=True 用于 CC Index（返回字典序噪声，必须要求 URL 带形态词）；
    站级发现 / Wikipedia 外链的 URL 未必含关键词，传 False，只做域名质量过滤。
    """
    out, per = [], {}
    for u in urls:
        u = str(u).strip().rstrip(",)")
        try:
            h = urllib.parse.urlsplit(u).netloc.lower()
        except Exception:
            continue
        d = _dom(h)
        if not _quality_ok(d, u, need_hint) or per.get(d, 0) >= 3:
            continue
        per[d] = per.get(d, 0) + 1
        out.append((d, u if u.startswith("http") else "https://%s/" % d))
    return out


def _valid_pair(dom, url=""):
    d = _dom(dom)
    return (d, url or "https://%s/" % d) if d else None


# ------------------------------------------------------------------ 各外生源
def src_crtsh():
    """证书透明日志：SAN 关键词 → 子域。免费、无 key、含未收录站。

    一次并行查 3 个关键词：单个关键词的证书常集中在同一家公司
    （q=llm 曾只吐出 llmtv.xyz 的两个子域），换词比换配额更能拉开分散度。
    """
    def one(kw):
        try:
            u = "https://crt.sh/?q=" + urllib.parse.quote(kw) + "&output=json"
            data = json.loads(_http(u, 20))   # 晚高峰 CT 日志常要 15s+
            got = []
            for row in data[:500]:
                for name in str(row.get("name_value") or "").splitlines():
                    p = _valid_pair(name.strip().lstrip("*."))
                    if p and _quality_ok(p[0], "", need_hint=False):
                        got.append(p)
            return got
        except Exception:
            return []                       # 单个词超时不拖垮整源

    kws = random.sample(CRTSH_WORDS, k=min(3, len(CRTSH_WORDS)))
    out = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        for got in ex.map(one, kws):
            out.extend(got)
    random.shuffle(out)
    # 这里只做宽松的注册域配额（4），严格配额在 fetch_rounds 统一做。
    return _cap(out, 4)[:200]


def src_cc():
    """Common Crawl Index：按 URL 形态取，而不是按主题词搜首页。"""
    coll = json.loads(_http("https://index.commoncrawl.org/collinfo.json", 20))
    api = coll[0]["cdx-api"]
    pat = random.choice(CC_PATTERNS)
    u = (api + "?url=" + urllib.parse.quote(pat, safe="*./")
         + "&output=json&filter=statuscode:200&filter=mime:text/html"
         + "&collapse=urlkey&limit=300")
    urls = []
    for ln in _http(u, 20).splitlines():
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if d.get("url"):
            urls.append(d["url"])
    out = _pairs(urls)          # 质量闸门 + 主机配额
    random.shuffle(out)         # 别总取字典序最前那批
    return out[:120]


def src_wayback():
    """时间维度：从本地枢纽站的历史 CDX 里捞子域与老 URL。"""
    host = random.choice(_hub_hosts() or ["example.org"])
    u = ("https://web.archive.org/cdx/search/cdx?url="
         + urllib.parse.quote("*." + host + "/*", safe="*.")
         + "&output=json&filter=statuscode:200&fl=original"
         + "&collapse=urlkey&limit=300")
    data = json.loads(_http(u, 20))
    urls = [r[0] for r in data[1:] if isinstance(r, list) and r]
    return _pairs(urls, need_hint=False)[:120]


def src_wikipedia():
    """知识图谱：Wikipedia 条目的外部链接，天然带主题标注、多语种。"""
    kw = random.choice(WIKI_WORDS)
    u = ("https://en.wikipedia.org/w/api.php?action=query&generator=search"
         "&gsrsearch=" + urllib.parse.quote(kw) + "&gsrlimit=6"
         "&prop=externallinks&ellimit=100&format=json")
    data = json.loads(_http(u, 18))
    urls = []
    for page in (data.get("query") or {}).get("pages", {}).values():
        for link in page.get("externallinks") or []:
            urls.append(str(link.get("url") if isinstance(link, dict) else link))
    return _pairs(urls, need_hint=False)[:120]


def src_sitemap():
    """站级发现：sitemap / robots / RSS，一次拿全站 URL。"""
    out = []
    for host in random.sample(_hub_hosts(), min(3, len(_hub_hosts()))):
        for path in ("/sitemap.xml", "/robots.txt", "/feed", "/rss.xml"):
            try:
                txt = _http("https://" + host + path, 12)
            except Exception:
                continue
            out.extend(_pairs(URL_RE.findall(txt), need_hint=False)[:40])
            if len(out) >= 100:
                return out[:100]
    return out[:100]


def src_tranco():
    """榜单差分：拿最新 Top-1M，与现有库比对出「没见过的站」。默认手动开。"""
    z = zipfile.ZipFile(io.BytesIO(_http(
        "https://tranco-list.eu/top-1m.csv.zip", 40, raw=True)))
    name = next(n for n in z.namelist() if n.endswith(".csv"))
    lines = z.read(name).decode("utf-8", "replace").splitlines()[:60000]
    out = []
    for ln in lines[1:]:
        parts = ln.strip().split(",")
        if len(parts) >= 2:
            p = _valid_pair(parts[1])
            if p:
                out.append(p)
        if len(out) >= 400:
            break
    return out


_IMPL = {"crtsh": src_crtsh, "cc": src_cc, "wayback": src_wayback,
         "wikipedia": src_wikipedia, "sitemap": src_sitemap,
         "tranco": src_tranco}
_HUB_CACHE = {"ts": 0.0, "hosts": []}


def _hub_hosts():
    """枢纽站来自本地目录类记录，缓存 10 分钟（别每轮查 4.7 万行）。"""
    if _HUB_CACHE["hosts"] and time.time() - _HUB_CACHE["ts"] < 600:
        return _HUB_CACHE["hosts"]
    hosts = []
    try:
        import sqlite3
        c = sqlite3.connect(KB_DB, timeout=8)
        rows = c.execute("SELECT domain FROM docs WHERE forms LIKE '%目录/索引%' "
                         "OR category LIKE '%导航%' LIMIT 300").fetchall()
        c.close()
        hosts = [r[0] for r in rows if r[0] and "." in r[0]]
    except Exception:
        pass
    if not hosts:
        hosts = ["example.org", "wikipedia.org", "opendirectoryproject.org"]
    _HUB_CACHE.update(ts=time.time(), hosts=hosts)
    return hosts


# ------------------------------------------------------------------ 统计与调度
def _stats():
    try:
        with open(STATS, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    for s in SOURCES:
        d.setdefault(s, {"rounds": 0, "returned": 0, "new": 0,
                         "fails": 0, "cool_until": 0.0})
    return d


def _save_stats(d):
    os.makedirs(KB, exist_ok=True)
    with open(STATS, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def _pool():
    """返回 (所有行, 已见域名集合)。append-only 文件，重启可完整恢复。"""
    try:
        lines = open(POOL, encoding="utf-8", errors="replace").read().splitlines()
    except Exception:
        lines = []
    rows, seen = [], set()
    for ln in lines:
        try:
            r = json.loads(ln)
            rows.append(r)
            seen.add(r.get("domain") or "")
        except Exception:
            continue
    return rows, seen


def _cursor():
    try:
        with open(CURSOR, encoding="utf-8") as f:
            return int(json.load(f).get("offset", 0))
    except Exception:
        return 0


def _set_cursor(n):
    os.makedirs(KB, exist_ok=True)
    with open(CURSOR, "w", encoding="utf-8") as f:
        json.dump({"offset": n, "ts": time.time()}, f)


def _known_domains(seen_doms=None):
    if seen_doms is not None:
        return set(seen_doms)
    known = set()
    try:
        import sqlite3
        c = sqlite3.connect(KB_DB, timeout=8)
        known |= {r[0] for r in c.execute("SELECT domain FROM docs")}
        c.close()
    except Exception:
        pass
    try:
        with open(RES_JSON, encoding="utf-8") as f:
            known |= {(x or {}).get("domain", "") for x in json.load(f)}
    except Exception:
        pass
    return known


def _pick_sources(k=3, epsilon=0.18, force=None, st=None):
    """ε-greedy：82% 按历史「新域名产出率」加权，18% 纯探索。

    st 必须传调用方内存里的那份：冷却是在循环内即时写 st 的，若这里
    重新读盘（尚未 save），刚失败的源本轮仍会被选中 —— 冷却等于没生效。
    """
    if force:
        return [force] if force in _IMPL else []
    st = st if st is not None else _stats()
    now = time.time()
    alive = [s for s in SOURCES
             if s not in DISABLED_DEFAULT and st[s]["cool_until"] <= now]
    if not alive:
        return []
    out = []
    for _ in range(min(k, len(alive))):
        pool = [s for s in alive if s not in out]
        if not pool:
            break
        if random.random() < epsilon:
            out.append(random.choice(pool))
            continue
        ws = []
        for s in pool:
            v = st[s]
            rate = v["new"] / max(1, v["returned"])
            ws.append(0.35 + 3.0 * rate)       # 0.35 底权，冷门源不被饿死
        pick = random.choices(pool, weights=ws, k=1)[0]
        out.append(pick)
    return out


def fetch_rounds(rounds=1, force=None, epsilon=None):
    """跑若干轮外生源抓取，只把**库外新域名**追加进种子池。返回新增数。"""
    if epsilon is None:
        epsilon = float((cfg().get("task_layer") or {})
                        .get("external_epsilon", 0.18))
    os.makedirs(KB, exist_ok=True)
    st = _stats()
    _, pool_seen = _pool()
    known = _known_domains()
    added = 0
    for rd in range(rounds):
        picks = ([force] if force else
                 _pick_sources(k=3, epsilon=epsilon, st=st))
        if not picks:
            log("[ext] 无可用源（全部冷却中或已禁用）")
            break
        for s in picks:
            t0 = time.time()
            try:
                pairs = _cap(_IMPL[s]() or [], 3)   # 注册域配额，统一兜底
            except Exception as e:
                st[s]["fails"] += 1
                st[s]["rounds"] += 1
                cool = min(3600.0, 600.0 * (2 ** min(4, st[s]["fails"] - 1)))
                st[s]["cool_until"] = time.time() + cool
                log("  [ext:%-9s] 失败 %s → 冷却 %.0fs"
                    % (s, type(e).__name__ + ":" + str(e)[:50], cool))
                continue
            st[s]["rounds"] += 1
            st[s]["returned"] += len(pairs)
            fresh = []
            for d, u in pairs:
                if d in known or d in pool_seen:
                    continue
                pool_seen.add(d)
                fresh.append({"domain": d, "url": u, "src": s,
                              "ts": time.time()})
            if fresh:
                with open(POOL, "a", encoding="utf-8") as f:
                    for r in fresh:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
            st[s]["new"] += len(fresh)
            added += len(fresh)
            log("  [ext:%-9s] 返回 %-4d 新增 %-4d 命中率 %.0f%% (%.1fs)"
                % (s, len(pairs), len(fresh),
                   100.0 * len(fresh) / max(1, len(pairs)), time.time() - t0))
    _save_stats(st)
    return added


_FILL = {"thread": None}


def _async_fill():
    """池子见底时在**同一进程**里异步补种，绝不阻塞爬虫的一轮 pump。

    晚高峰单个外源可能要 90s；若同步补种，爬虫整轮 pump 会卡在那里，
    「提速」反而变「停摆」。种子是 append 落盘的，线程退出也不丢。
    """
    t = _FILL.get("thread")
    if t is not None and t.is_alive():
        return
    def job():
        try:
            fetch_rounds(rounds=1)
        except Exception as e:
            log("  [ext] 后台补种异常：%s" % str(e)[:80])
    th = threading.Thread(target=job, daemon=True, name="ext-seed-fill")
    _FILL["thread"] = th
    th.start()


def next_batch(n=40, seen_doms=None, log_fn=None):
    """爬虫调用的接口：从种子池取 n 条**库外新域名**的 URL。

    池子不够 → 后台补种，本次先把现有种子吐出去（可能少于 n），
    取完只移动游标，不删任何记录。
    """
    rows, _ = _pool()
    pos = _cursor()
    if len(rows) - pos < n:
        _async_fill()
    take = rows[pos:pos + n]
    known = _known_domains(seen_doms)
    out = [r["url"] for r in take
           if r.get("domain") and r["domain"] not in known]
    _set_cursor(pos + len(take))
    if log_fn:
        log_fn("[pump:external] 种子池 %d → 吐出 %d 条（游标 %d→%d）"
               % (len(rows), len(out), pos, pos + len(take)))
    return out


def pattern_queries():
    """给 evolve.bootstrap_keywords 混用的结构形态查询词。"""
    return list(PATTERN_QUERIES)


def stats():
    st = _stats()
    rows, seen = _pool()
    log("== 外生源 ==")
    for s in SOURCES:
        v = st[s]
        rate = 100.0 * v["new"] / max(1, v["returned"])
        log("   %-10s 轮次 %-4d 返回 %-6d 新增 %-6d 命中 %5.1f%% 失败 %-3d %s"
            % (s, v["rounds"], v["returned"], v["new"], rate, v["fails"],
               ("冷却中" if v["cool_until"] > time.time() else
                ("默认关闭" if s in DISABLED_DEFAULT else "可用"))))
    log("   种子池 %d 条 / %d 个域名｜游标 %d｜待消费 %d"
        % (len(rows), len(seen), _cursor(), len(rows) - _cursor()))


def selftest():
    """离线验证解析器与调度，不联网。"""
    global _IMPL
    fake = {
        "crtsh": lambda: [("a.example.org", "https://a.example.org/"),
                          ("cdn.cloudflare.com", "https://cdn.cloudflare.com/")],
        "cc": lambda: [("b.example.edu", "https://b.example.edu/dataset")],
    }
    old = dict(_IMPL)
    _IMPL.update(fake)
    log("== 解析器 ==")
    log("   _pairs 主机配额:", _pairs(
        ["https://x.org/a", "https://x.org/b", "https://x.org/c",
         "https://x.org/d", "https://y.org/"]))
    log("   _dom 噪声过滤:", _dom("cdn.cloudflare.com"), _dom("data.example.org"))
    log("   结构查询词:", len(pattern_queries()), "条")
    log("== 调度 ==")
    log("   ε-greedy 抽样:", _pick_sources(k=3, epsilon=1.0))
    log("== 注入模拟（不写盘）==")
    log("   新域名:", [p[0] for p in fake["crtsh"]() if _dom(p[0])])
    _IMPL.clear()
    _IMPL.update(old)
    log("SELFTEST_OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, default=0)
    ap.add_argument("--take", type=int, default=0)
    ap.add_argument("--source", default="")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--epsilon", type=float, default=0.18)
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return
    if a.run:
        log("[ext] 抓取 %d 轮（epsilon=%.2f%s）"
            % (a.run, a.epsilon, "，指定源 " + a.source if a.source else ""))
        n = fetch_rounds(rounds=a.run, force=a.source or None,
                         epsilon=a.epsilon)
        log("[ext] 累计新增库外域名 %d 个" % n)
    if a.take:
        urls = next_batch(a.take, log_fn=log)
        log("   取出 %d 条：" % len(urls))
        for u in urls[:10]:
            log("     ", u)
    if a.stats or not (a.run or a.take):
        stats()


if __name__ == "__main__":
    main()
