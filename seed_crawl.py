# -*- coding: utf-8 -*-
"""定向关联爬取（seed crawl）—— 给一个主链接，精确展开这一个站。

与主爬虫的分工（2026-11-05 主人定调）：
    主爬虫 pipeline.py  = 全网平推，负责「发现」：还有哪些站活着。便宜、广、精度低。
    本模块 seed_crawl   = 定向关联，负责「深挖」：一个站里到底有什么。窄、贵、精度高、账算得清。

两种形态：
    · 平台适配  douyin —— 分享链接 → 一次渲染 → 拦 API → 作品/作者/图片/音乐 + 关系边
    · 通用站    site   —— 任意主链接 → 站内 BFS → 内容页优先 → 正文与资源入账

用法：
    python seed_crawl.py --seed "https://v.douyin.com/xxxx/" --max-pages 20
    python seed_crawl.py --seed "https://example.com/" --platform site --max-pages 60
    python seed_crawl.py --seed-file seeds.txt --max-depth 2 --budget 600
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse as up

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

KB = os.path.join(BASE, "_kb")
DB = os.environ.get("NETFORCE_DB") or os.path.join(KB, "seeds.db")
REC = os.path.join(KB, "seed_records.jsonl")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_lock = None


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- 存储层
def connect():
    os.makedirs(KB, exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def ensure():
    c = connect()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS seeds(
            url TEXT PRIMARY KEY, platform TEXT, depth INTEGER DEFAULT 0,
            state TEXT DEFAULT 'new', note TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS entities(
            id TEXT PRIMARY KEY, platform TEXT, type TEXT, title TEXT,
            url TEXT, depth INTEGER DEFAULT 0, payload TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS edges(
            src TEXT, dst TEXT, rel TEXT, ts REAL,
            PRIMARY KEY(src, dst, rel));
        CREATE INDEX IF NOT EXISTS ix_seeds_state ON seeds(state, depth);
        CREATE INDEX IF NOT EXISTS ix_ent_type ON entities(platform, type);
    """)
    # prio：0=手动指定的种子（优先跑），1=自动发现的下游种子。
    # 没有它的时候，--seed 给的链接会被队列里上千个旧种子挤到最后
    # —— 同一个坑踩过两次（B 站、豆瓣），特此固化。
    cols = [r[1] for r in c.execute("PRAGMA table_info(seeds)")]
    if "prio" not in cols:
        c.execute("ALTER TABLE seeds ADD COLUMN prio INTEGER DEFAULT 1")
    c.commit()
    c.close()


def push_seed(url, platform, depth=0, note="", prio=1):
    u = (url or "").strip()
    if not u:
        return False
    c = connect()
    try:
        cur = c.execute(
            "INSERT OR IGNORE INTO seeds(url,platform,depth,state,note,ts,prio) "
            "VALUES(?,?,?,'new',?,?,?)",
            (u, platform, int(depth), note[:120], time.time(), int(prio)))
        n = cur.rowcount
        # 已存在的种子：手动指定的（prio=0）要能被抬上来，否则重跑 --seed 无效
        if not n and int(prio) == 0:
            # 手动指定 = 明确要重跑。上次是 error/done/skip 都得重来 ——
            # 原来这里带了 AND state='new'，导致「跑失败过一次的站再也跑不了」
            # （36kr 实测：第一次 asyncio 报错标了 error，之后 --seed 全部空转）。
            c.execute("UPDATE seeds SET prio=0, state='new', note='' WHERE url=?", (u,))
        c.commit()
        return bool(n)
    finally:
        c.close()


def pop_seed(exclude=(), platform=None):
    """取一个待跑种子。

    exclude：不要取的平台。主循环必须排除 github ——
        那些种子是 B 站简介挖出来的仓库地址，交给 gh_crawl.py 处理。
        不排除的话会被本循环 pop 走，然后因为 depth 超限直接标成 skip，
        gh_crawl 再也读不到（实测踩过：2 个刚挖到的仓库被标「超过深度上限」）。

    platform：只取这个平台的。原来 `--platform douban` 只强制了适配器不过滤队列，
        结果豆瓣任务 pop 到队列里的 B 站种子，拿 B 站 URL 喂 Douban.handle()，
        14 个种子 0.1 秒全"跑完"、实体零增长 —— 静默空转最坑。
    """
    c = connect()
    try:
        q = ("SELECT url,platform,depth FROM seeds WHERE state='new'")
        args = []
        if exclude:
            q += " AND platform NOT IN (%s)" % ",".join("?" * len(exclude))
            args += list(exclude)
        if platform and platform != "auto":
            q += " AND platform=?"
            args.append(platform)
        q += " ORDER BY prio ASC, depth ASC, ts ASC LIMIT 1"
        row = c.execute(q, args).fetchone()
        if not row:
            return None
        c.execute("UPDATE seeds SET state='run' WHERE url=?", (row[0],))
        c.commit()
        return {"url": row[0], "platform": row[1], "depth": row[2]}
    finally:
        c.close()


def finish_seed(url, state="done", note=""):
    c = connect()
    c.execute("UPDATE seeds SET state=?, note=? WHERE url=?",
              (state, (note or "")[:200], url))
    c.commit()
    c.close()


def save_entities(items, edges=()):
    """实体按 (platform,type,id) 去重；返回真正新增的条数。"""
    c = connect()
    new = 0
    try:
        for e in items:
            cur = c.execute(
                "INSERT OR IGNORE INTO entities(id,platform,type,title,url,depth,payload,ts) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (e["id"], e.get("platform", ""), e.get("type", ""),
                 (e.get("title") or "")[:300], (e.get("url") or "")[:600],
                 int(e.get("depth", 0)), json.dumps(e.get("payload") or {},
                                                   ensure_ascii=False)[:4000],
                 time.time()))
            new += cur.rowcount
        for s, d, rel in edges:
            c.execute("INSERT OR IGNORE INTO edges(src,dst,rel,ts) VALUES(?,?,?,?)",
                      (s, d, rel, time.time()))
        c.commit()
        return new
    finally:
        c.close()


def record(rec):
    os.makedirs(KB, exist_ok=True)
    with open(REC, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def enqueue_asset(url, src, ctype="", filename=""):
    """资源一律进资源队列（不丢弃、不阻挡）。"""
    try:
        import download_assets as da
        ok, _ = da.enqueue_direct(url, src=src, ctype=ctype, filename=filename)
        return ok
    except Exception:
        return False


# ---------------------------------------------------------------- 浏览器复用
class Browser:
    """一个进程只起一次浏览器，之后所有页面复用同一个 context。

    实测（5 个作品连续加载）：每条起浏览器 10–24s → 复用 3.82s（首条 6.22s）。
    这是定向爬取能不能算得过账的关键，不是可有可无的优化。
    """

    def __init__(self, headless=True):
        self._p = None
        self._b = None
        self._ctx = None
        self.headless = headless

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        import browser_pick
        exe = browser_pick.find_gpu_browser()
        self._p = sync_playwright().start()
        self._b = self._p.chromium.launch(
            executable_path=exe, headless=self.headless,
            args=["--headless=new", "--disable-blink-features=AutomationControlled",
                  "--use-angle=vulkan", "--enable-gpu-rasterization",
                  "--mute-audio"])
        self._ctx = self._b.new_context(user_agent=UA, locale="zh-CN",
                                        viewport={"width": 1440, "height": 900})
        return self

    def __exit__(self, *exc):
        try:
            if self._ctx:
                self._ctx.close()
            if self._b:
                self._b.close()
            if self._p:
                self._p.stop()
        except Exception:
            pass
        return False

    def open(self, url, keys, wait_ms=12000, scrolls=0, settle_ms=4000):
        """打开一页，收集命中 keys 的接口 JSON。返回 (载荷列表, 耗时)。

        🔴 不能「拿到第一个响应就关页」：抖音一次加载会先后吐出
           detail → post → related，第一个到的是 detail（只有 1 条作品），
           post 里才是作者全集和全部图片。提前关页等于把大头全丢了
           （实测：只等 1 个接口 → 3 实体 / 2 资源；等齐 → 39 作品 / 87 图）。
        """
        pg = self._ctx.new_page()
        hits = []

        def on_resp(r):
            if any(k in r.url for k in keys):
                try:
                    body = r.text()
                except Exception:
                    return
                if body and body.lstrip()[:1] in "{[":
                    hits.append({"url": r.url, "body": body})
        pg.on("response", on_resp)
        t0 = time.time()
        want = 3 if len(keys) >= 3 else len(keys)
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=45000)
            waited = 0
            while waited < wait_ms:
                pg.wait_for_timeout(300)
                waited += 300
                kinds = {k for k in keys if any(k in h["url"] for h in hits)}
                # 攒够三类且过了最短沉淀时间才走；否则继续等满
                if len(kinds) >= want and waited >= settle_ms:
                    break
            pg.wait_for_timeout(1200)
            for _ in range(scrolls):
                pg.mouse.wheel(0, 2000)
                pg.wait_for_timeout(1800)
        except Exception as e:
            log("    [browser] %s → %s %s" % (url[:60], type(e).__name__, str(e)[:60]))
        try:
            pg.close()
        except Exception:
            pass
        return hits, time.time() - t0


# ---------------------------------------------------------------- 抖音适配
class Douyin:
    """分享链接 → 作品/作者/图片/音乐 + 关系边。

    实测结论（2026-11-05，四条通道全部试过）：
      · 分享页 HTML 是空壳，_ROUTER_DATA 只有 1.5 KB 页面上下文，无 play_addr
      · 旧 iteminfo 返回 129 B 空体；aweme.snssdk.com 返回 0 B
      · 网页版 aweme/detail 直接 403（Argus）
      · 页面内 fetch / XHR 重放同一条已签名 URL，仍然拿回 830 KB 空壳
      · frontierSign 只给 X-Bogus，接口要的是 a_bogus，算不出
      ⇒ 渲染是唯一可靠路径。优化的空间在「一次渲染拿一片」和「复用上下文」，不在绕过渲染。
    """
    name = "douyin"
    API_KEYS = ("aweme/detail", "aweme/post", "aweme/related",
                "music/aweme", "challenge/aweme")
    HOSTS = ("douyin.com", "iesdouyin.com")

    @classmethod
    def match(cls, url):
        h = (up.urlsplit(url).hostname or "").lower()
        return any(h == d or h.endswith("." + d) for d in cls.HOSTS)

    @staticmethod
    def resolve_short(url):
        """短链走 302 拿真实 ID —— 这一步纯 HTTP，不需要浏览器。"""
        import httpx
        try:
            with httpx.Client(follow_redirects=False, trust_env=False,
                              timeout=12, verify=False,
                              headers={"User-Agent": UA}) as c:
                r = c.get(url)
                loc = r.headers.get("location") or ""
        except Exception:
            loc = ""
        m = re.search(r"/(video|note|share/video|share/note)/(\d+)", loc or url)
        return (m.group(2), m.group(1)) if m else (None, None)

    @classmethod
    def page_url(cls, vid, kind="video"):
        return "https://www.douyin.com/%s/%s" % (
            "note" if kind == "note" else "video", vid)

    @staticmethod
    def _imgs(it, host, depth, out, ed):
        n = 0
        for im in (it.get("images") or []):
            for u in (im.get("url_list") or [])[:1]:
                if u:
                    enqueue_asset(u, host, "image/jpeg",
                                  "%s.jpg" % str(it.get("aweme_id")))
                    n += 1
            if n >= 40:
                break
        # 视频直链：优先无水印通道
        v = it.get("video") or {}
        for key in ("play_addr", "download_addr"):
            pa = v.get(key) or {}
            for u in (pa.get("url_list") or [])[:1]:
                if u:
                    enqueue_asset(u, host, "video/mp4", "%s.mp4" % it.get("aweme_id"))
                    n += 1
        return n

    @classmethod
    def parse(cls, hits, depth=0):
        """把拦截到的 JSON 变成实体 + 边 + 资源。"""
        works, authors, tags, musics, edges = {}, {}, {}, {}, []
        assets = 0
        for h in hits:
            try:
                d = json.loads(h["body"])
            except Exception:
                continue
            items = []
            if d.get("aweme_detail"):
                items = [d["aweme_detail"]]
            elif isinstance(d.get("aweme_list"), list):
                items = d["aweme_list"]
            for it in items:
                wid = str(it.get("aweme_id") or "")
                if not wid:
                    continue
                a = it.get("author") or {}
                st = it.get("statistics") or {}
                works[wid] = {
                    "id": "douyin:work:%s" % wid, "platform": "douyin",
                    "type": "work", "title": (it.get("desc") or "")[:120],
                    "url": cls.page_url(wid), "depth": depth,
                    "payload": {"digg": st.get("digg_count"),
                                "comment": st.get("comment_count"),
                                "create_time": it.get("create_time"),
                                "images": len(it.get("images") or []),
                                "author": a.get("nickname")},
                }
                assets += cls._imgs(it, "douyin:" + wid, depth, works, edges)
                if a.get("sec_uid"):
                    aid = "douyin:author:%s" % a["sec_uid"]
                    authors[aid] = {
                        "id": aid, "platform": "douyin", "type": "author",
                        "title": a.get("nickname") or "",
                        "url": "https://www.douyin.com/user/%s" % a["sec_uid"],
                        "depth": depth + 1,
                        "payload": {"sec_uid": a["sec_uid"]},
                    }
                    edges.append(("douyin:work:%s" % wid, aid, "by_author"))
                for e in (it.get("text_extra") or []):
                    if e.get("hashtag_name") and e.get("hashtag_id"):
                        tid = "douyin:hashtag:%s" % e["hashtag_id"]
                        tags[tid] = {
                            "id": tid, "platform": "douyin", "type": "hashtag",
                            "title": e["hashtag_name"],
                            "url": "https://www.douyin.com/hashtag/%s" % e["hashtag_id"],
                            "depth": depth + 1, "payload": {},
                        }
                        edges.append(("douyin:work:%s" % wid, tid, "tagged"))
                m = it.get("music") or {}
                if m.get("id"):
                    mid = "douyin:music:%s" % m["id"]
                    musics[mid] = {
                        "id": mid, "platform": "douyin", "type": "music",
                        "title": m.get("title") or "",
                        "url": "https://www.douyin.com/music/%s" % m["id"],
                        "depth": depth + 1, "payload": {},
                    }
                    edges.append(("douyin:work:%s" % wid, mid, "uses_music"))
        ents = list(works.values()) + list(authors.values()) + \
            list(tags.values()) + list(musics.values())
        return ents, edges, assets

    @classmethod
    def run(cls, bw, seed, max_pages, budget_left):
        url = seed["url"]
        vid, kind = cls.resolve_short(url)
        if not vid:
            m = re.search(r"/(video|note)/(\d+)", url)
            vid, kind = (m.group(2), m.group(1)) if m else (None, None)
        if not vid:
            return 0, "无法从链接解析出作品 ID"
        pg = cls.page_url(vid, kind or "video")
        log("  [douyin] %s → %s" % (url[:56], pg))
        hits, dt = bw.open(pg, cls.API_KEYS, wait_ms=9000)
        if not hits:
            return 0, "接口未命中（可能被风控）"
        ents, edges, assets = cls.parse(hits, depth=seed["depth"])
        n = save_entities(ents, edges)
        works = sum(1 for e in ents if e["type"] == "work")
        log("  [douyin] 命中 %d 接口 / 实体 %d（新 %d）/ 关系边 %d / 资源入队 %d | %.1fs"
            % (len(hits), len(ents), n, len(edges), assets, dt))
        record({"ts": time.time(), "platform": "douyin", "seed": url,
                "page": pg, "hits": len(hits), "entities": len(ents),
                "new": n, "edges": len(edges), "assets": assets,
                "sec": round(dt, 2)})
        # 下游种子：相关作品（depth+1）
        nxt = 0
        for e in ents:
            if e["type"] == "work" and e["depth"] == seed["depth"] and e["url"] != pg:
                if push_seed(e["url"], "douyin", seed["depth"] + 1, "related"):
                    nxt += 1
        # 返回值是「消耗的页面预算」，不是接口命中数 —— 一次渲染只算一页
        return 1, "ok 新增实体 %d / 下游种子 %d" % (n, nxt)


# ---------------------------------------------------------------- 通用站定向
class Bilibili:
    """B 站：一个 BV 号 → 视频 / UP 主 / 标签 / 40 条相关推荐。

    实测（2026-11-05）：四个接口**全部免登录、免签名、纯 HTTP**。
    与抖音形成极端对比 —— 抖音每页要一次渲染（约 12s），B 站这里 0.3s。
    这就是「同一个需求，成本差 40 倍」，差别不在破解，在**选对通道**。

    已验通的接口（都不需要 wbi 签名）：
        x/web-interface/view?bvid=              视频详情（标题/UP/统计/分P）
        x/web-interface/archive/related?bvid=   40 条相关推荐
        x/tag/archive/tags?bvid=                标签（9 个）
        x/web-interface/card?mid=               UP 主（粉丝/签名）
    未用：space/wbi/arc/search（UP 主全部作品）要 wbi 签名，算法公开但要多两步
    （nav 取 img_key/sub_key → 混表 → md5），后续需要时再补，不影响主链路。
    """
    name = "bilibili"
    HOSTS = ("bilibili.com", "b23.tv")
    HEADERS = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36"),
        "Referer": "https://www.bilibili.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    VIEW = "https://api.bilibili.com/x/web-interface/view?bvid=%s"
    RELATED = "https://api.bilibili.com/x/web-interface/archive/related?bvid=%s"
    TAGS = "https://api.bilibili.com/x/tag/archive/tags?bvid=%s"
    CARD = "https://api.bilibili.com/x/web-interface/card?mid=%s"
    BV_RE = __import__("re").compile(r"(BV[0-9A-Za-z]{10})")

    # ---------- 简介里的外链 ----------
    # 主人原话（2026-11-05）：
    #   「过去我是通过哔哩哔哩刷视频刷到一些地址，因为哔哩哔哩那个简介里面有人会给…
    #     那些里面给的一般都是低星的，或者是一般不是很高的。因为很多是个人开发者。」
    #
    # 为什么这条通道必须单独做：GitHub 的 topic 搜索是按 star 排的，我默认 min_stars=50，
    # ★50 以下的个人项目**永远进不来**。而 B 站简介里的项目恰恰就是这一档 ——
    # 实测三个关键词（2026-11-05）：
    #   「开源项目 github」 8/20 视频带链接，41 条
    #   「GitHub 项目推荐」  8/20 视频带链接，63 条
    #   「自己写的 开源」    5/20 视频带链接，6 条  ← 全是个人开发者，star 个位数
    # 所以 B 站简介是「低星个人项目」唯一的入口，别的通道都给不了。
    CODE_RE = __import__("re").compile(
        r"(?:https?://)?(?:github\.com|gitee\.com|gitcode\.(?:com|net))"
        r"/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+(?:/[^\s，。；）)\]\"'、|]{0,60})?", re.I)
    PAN_RE = __import__("re").compile(
        r"(?:https?://)?(?:pan\.baidu\.com|pan\.quark\.cn|aliyundrive\.com|alipan\.com"
        r"|123pan\.com|pan\.xunlei\.com|drive\.uc\.cn)/[^\s，。；）)\]\"'、|]{0,70}", re.I)

    @classmethod
    def desc_links(cls, desc):
        """从简介里挑出代码托管地址和网盘地址。返回 (codes, pans)。"""
        d = desc or ""
        codes, pans = [], []
        for m in cls.CODE_RE.finditer(d):
            u = m.group(0)
            if not u.lower().startswith("http"):
                u = "https://" + u.lstrip("/")
            # 去掉尾部的 /releases /issues 之类，只留仓库根
            parts = u.split("/")
            if len(parts) >= 5:
                u = "/".join(parts[:5])
            u = u.rstrip("/")
            if u not in codes:
                codes.append(u)
        for m in cls.PAN_RE.finditer(d):
            u = m.group(0)
            if not u.lower().startswith("http"):
                u = "https://" + u
            if u not in pans:
                pans.append(u)
        return codes, pans

    @classmethod
    def match(cls, url):
        h = (up.urlsplit(url).hostname or "").lower()
        return any(h == d or h.endswith("." + d) for d in cls.HOSTS)

    @classmethod
    def resolve(cls, url):
        """URL 或 b23.tv 短链 → BV 号。短链要跟一次 302，纯 HTTP。"""
        import httpx
        m = cls.BV_RE.search(url or "")
        if m:
            return m.group(1)
        try:
            with httpx.Client(follow_redirects=True, trust_env=False,
                              timeout=12, headers=cls.HEADERS,
                              verify=False) as cli:
                r = cli.get(url)
            m = cls.BV_RE.search(str(r.url)) or cls.BV_RE.search(r.text[:4000])
            return m.group(1) if m else ""
        except Exception:
            return ""

    @staticmethod
    def _api(cli, tmpl, key):
        try:
            d = cli.get(tmpl % key).json()
        except Exception:
            return None
        return d.get("data") if d.get("code") == 0 else None

    @classmethod
    def handle(cls, url, seed, a):
        import httpx
        t0 = time.time()
        bv = cls.resolve(url)
        if not bv:
            return 1, "拿不到 BV 号"
        depth = seed["depth"]
        ents, edges, assets = [], [], 0
        try:
            with httpx.Client(follow_redirects=True, trust_env=False,
                              timeout=12, headers=cls.HEADERS,
                              verify=False) as cli:
                v = cls._api(cli, cls.VIEW, bv) or {}
                if not v:
                    return 1, "view 接口无数据（可能已下架）"
                mid = str((v.get("owner") or {}).get("mid") or "")
                up_name = (v.get("owner") or {}).get("name") or ""
                page_url = "https://www.bilibili.com/video/%s" % bv
                desc = v.get("desc") or ""
                codes, pans = cls.desc_links(desc)

                ents.append({
                    "id": "bilibili:work:%s" % bv, "platform": "bilibili",
                    "type": "work", "title": (v.get("title") or "")[:120],
                    "url": page_url, "depth": depth,
                    "payload": {"bvid": bv, "aid": v.get("aid"),
                                "duration": v.get("duration"),
                                "pubdate": v.get("pubdate"),
                                "stat": v.get("stat"), "tname": v.get("tname"),
                                "codes": codes[:12], "pans": pans[:6]},
                })
                if mid:
                    ents.append({
                        "id": "bilibili:author:%s" % mid, "platform": "bilibili",
                        "type": "author", "title": up_name,
                        "url": "https://space.bilibili.com/%s" % mid,
                        "depth": depth, "payload": {},
                    })
                    edges.append(("bilibili:work:%s" % bv,
                                  "bilibili:author:%s" % mid, "by_author"))
                    card = cls._api(cli, cls.CARD, mid) or {}
                    if card.get("follower") is not None:
                        ents[-1]["payload"] = {"follower": card.get("follower"),
                                               "sign": str((card.get("card") or {}).get("sign"))[:120]}

                for t in (cls._api(cli, cls.TAGS, bv) or []):
                    nm = t.get("tag_name")
                    if not nm:
                        continue
                    tid = "bilibili:tag:%s" % (t.get("tag_id") or nm)
                    ents.append({"id": tid, "platform": "bilibili", "type": "hashtag",
                                 "title": nm, "url": "", "depth": depth,
                                 "payload": {}})
                    edges.append(("bilibili:work:%s" % bv, tid, "tagged"))

                rel = cls._api(cli, cls.RELATED, bv) or []
                nxt = []
                for x in rel:
                    rbv = x.get("bvid")
                    if not rbv or rbv == bv:
                        continue
                    rid = "bilibili:work:%s" % rbv
                    ents.append({"id": rid, "platform": "bilibili", "type": "work",
                                 "title": str(x.get("title") or "")[:120],
                                 "url": "https://www.bilibili.com/video/%s" % rbv,
                                 "depth": depth + 1,
                                 "payload": {"bvid": rbv,
                                             "owner": (x.get("owner") or {}).get("name"),
                                             "view": (x.get("stat") or {}).get("view")}})
                    edges.append(("bilibili:work:%s" % bv, rid, "related"))
                    nxt.append(rbv)
                cover = v.get("pic")
                if cover:
                    assets = 1 if enqueue_asset(cover, "bilibili:cover", "image/jpeg") else 0
        except Exception as e:
            return 1, "%s: %s" % (type(e).__name__, str(e)[:70])

        n = save_entities(ents, edges)
        dt = time.time() - t0
        log("  [bilibili] %s → 实体 %d（新 %d）/ 边 %d / 相关 %d | %.2fs"
            % (bv, len(ents), n, len(edges), len(nxt), dt))
        record({"ts": time.time(), "platform": "bilibili", "seed": url,
                "page": bv, "entities": len(ents), "new": n,
                "edges": len(edges), "assets": assets, "sec": round(dt, 2)})
        pushed = 0
        if depth + 1 <= a.max_depth:
            for rbv in nxt[:12]:
                if push_seed("https://www.bilibili.com/video/%s" % rbv,
                             "bilibili", depth + 1, "related"):
                    pushed += 1
        # 简介里的代码托管地址 → 直接进 GitHub 平台的种子队列。
        # 这些是低星个人项目，走 topic 搜索永远进不来（按 star 排会被过滤），
        # 只能从人的嘴里捞 —— 这就是这条通道的全部价值。
        gh = 0
        for u in codes[:8]:
            if push_seed(u, "github", depth + 1, "bili-desc", prio=0):
                gh += 1
        return 1, "ok 新增实体 %d / 下游种子 %d / 代码仓库 %d" % (n, pushed, gh)


class Xiaohongshu:
    """小红书：纯 HTTP 免签，0.3s 一次。跟抖音（12s 渲染）是两个极端。

    实测（2026-11-05）：
      · explore 首页 SSR 直接给 30 条 feed（含 id + xsec_token），0.3s
        连拉 5 次 **零重合**，累计 151 条唯一 → 这是无限的种子源
      · 详情页 /explore/{id}?xsec_token=… 0.23s / 86KB，全字段
        title / desc / tagList / imageList / user / interactInfo
      · 作者主页 /user/profile/{uid} 免签 200，但 **noteId 被抹成空字符串**
        （`{"id":"", "noteCard":{"noteId":""}}`），渲染也 302 到 /login
        → work→author→其他作品 这条边断，除非登录
      · 搜索页 SSR 的 search.feeds 为空，真实结果走
        /api/sns/web/v1/search/notes（要 x-s 签名）→ 未登录拿不到
      · 页面里那个 24 位 hex 数组是 `disableLikeNotes`（禁赞黑名单），
        不是笔记清单 —— 曾误判成 ID 兜底源，记一笔

    风控边界（实测）：连续 25 次请求后 explore 退化成 36KB 空壳（无 feeds），
    换 UA、加 Referer、拉长到 12s 间隔都无效 → 是 **IP 级临时限流**。
    所以这里必须硬限速：详情 1.6s/条，首页 4s/次，宁慢不封。
    """

    name = "xiaohongshu"
    HOSTS = ("xiaohongshu.com", "xhslink.com")
    HEADERS = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36"),
        "Referer": "https://www.xiaohongshu.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "*/*;q=0.8"),
    }
    GAP_NOTE = 1.6      # 详情页最小间隔
    GAP_FEED = 4.0      # 首页最小间隔
    _last = [0.0]
    COOKIE_FILE = os.path.join(KB, "xhs_cookies.json")

    # 出口通道：直连优先，直连被限才换代理（实测代理下 232 条唯一 / 0.27s / 零失败）。
    # 🔴 关键区别（主人 2026-11-05 04:09 提的点）：
    #   `httpx.Client(proxy=...)` 是 **显式代理**，只看 7890 端口活不活，
    #   **跟 Windows 系统代理开关无关**。所以「关掉系统代理」不影响这条路；
    #   真正会断的是「关掉 clash/mihomo 进程」——那时端口消失，本类自动退回直连。
    #   这就是过去连 GitHub 的那套逻辑：不依赖系统设置，依赖自己指定的出口。
    PROXY = os.environ.get("NETFORCE_PROXY", "http://127.0.0.1:7890")
    _chan = [0]                 # 0=直连, 1=代理；失败就往后挪，全失败回直连重来
    _last_err = ""              # 最近一次失败原因（换通道时打日志用）

    @staticmethod
    def _channels():
        return [None] + ([Xiaohongshu.PROXY] if Xiaohongshu.PROXY else [])

    @classmethod
    def _cur_proxy(cls):
        ch = cls._channels()
        return ch[cls._chan[0] % len(ch)]

    @classmethod
    def _bump(cls, why=""):
        n = len(cls._channels())
        cls._chan[0] += 1
        if why:
            log("  [xhs] 出口失败（%s）→ 换通道 #%d" % (why[:28], cls._chan[0] % n))
        return cls._chan[0] % n

    @classmethod
    def match(cls, url):
        h = (up.urlsplit(url).hostname or "").lower()
        return any(h == d or h.endswith("." + d) for d in cls.HOSTS)

    # ---------- 基础设施 ----------
    @classmethod
    def _pace(cls, gap):
        left = cls._last[0] + gap - time.time()
        if left > 0:
            time.sleep(left)
        cls._last[0] = time.time()

    @classmethod
    def client(cls):
        """带 cookie 存盘的 client。小红书的风控看 cookie 连续性，每次新开
        client 等于把自己伪装成全新访客，反而更容易被降级。"""
        import httpx
        proxy = cls._cur_proxy()
        c = httpx.Client(follow_redirects=True, trust_env=False, timeout=20,
                         headers=cls.HEADERS, verify=False, proxy=proxy)
        try:
            if os.path.exists(cls.COOKIE_FILE):
                d = json.load(open(cls.COOKIE_FILE, encoding="utf-8"))
                for k, v in (d or {}).items():
                    c.cookies.set(k, v, domain=".xiaohongshu.com")
        except Exception:
            pass
        return c

    @classmethod
    def save_cookies(cls, c):
        try:
            os.makedirs(KB, exist_ok=True)
            d = {k: v for k, v in c.cookies.items()}
            json.dump(d, open(cls.COOKIE_FILE, "w", encoding="utf-8"))
        except Exception:
            pass

    @staticmethod
    def _state(html):
        m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})</script>",
                      html or "", re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(1).replace("undefined", "null"))
        except Exception:
            return None

    # ---------- 解析 ----------
    @classmethod
    def parse_url(cls, url):
        """从链接里取 (note_id, xsec_token)。短链走 302 拿。"""
        u = url or ""
        m = re.search(r"/(?:explore|discovery/item|item)/([0-9a-f]{24})", u)
        nid = m.group(1) if m else None
        tok = ""
        try:
            q = up.parse_qs(up.urlsplit(u).query)
            tok = (q.get("xsec_token") or [""])[0]
        except Exception:
            pass
        if nid:
            return nid, tok
        # 短链：xhslink.com/a/xxxx
        import httpx
        try:
            with httpx.Client(follow_redirects=False, trust_env=False,
                              timeout=12, verify=False,
                              headers=cls.HEADERS) as cli:
                r = cli.get(u)
                loc = r.headers.get("location") or ""
            if not loc:
                r = cli.get(u) if False else None
        except Exception:
            loc = ""
        m = re.search(r"/(?:explore|discovery/item|item)/([0-9a-f]{24})", loc)
        if m:
            q = up.parse_qs(up.urlsplit(loc).query)
            return m.group(1), (q.get("xsec_token") or [""])[0]
        return None, ""

    @staticmethod
    def note_url(nid, tok):
        base = "https://www.xiaohongshu.com/explore/%s" % nid
        if tok:
            return base + "?xsec_token=%s&xsec_source=pc_feed" % tok
        return base

    @classmethod
    def detail(cls, cli, nid, tok):
        """返回 (note dict, 说明)。note 为空说明 token 过期或被风控。"""
        cls._pace(cls.GAP_NOTE)
        r = cli.get(cls.note_url(nid, tok))
        st = cls._state(r.text)
        if not st:
            return {}, "无 INITIAL_STATE（%dB）" % len(r.text)
        nd = ((st.get("note") or {}).get("noteDetailMap") or {})
        if not nd:
            return {}, "noteDetailMap 为空（token 过期或需登录）"
        key = nid if nid in nd else list(nd)[0]
        return (nd[key].get("note") or {}), "ok"

    @classmethod
    def explore(cls, cli=None):
        """首页 feed。返回 [(id, token)]；空 = 这个出口被限，由调用方决定换不换通道。"""
        own = cli is None
        cli = cli or cls.client()
        try:
            cls._pace(cls.GAP_FEED)
            r = cli.get("https://www.xiaohongshu.com/explore")
            feeds = cls._feeds_of(r.text)
            if not feeds:
                cls._last_err = "无 feed（%dB）" % len(r.text)
            return feeds
        except Exception as e:
            cls._last_err = "%s: %s" % (type(e).__name__, str(e)[:40])
            return []
        finally:
            if own:
                try:
                    cls.save_cookies(cli)
                    cli.close()
                except Exception:
                    pass

    @staticmethod
    def _feeds_of(html):
        st = Xiaohongshu._state(html) or {}
        out = []
        for x in (st.get("feed") or {}).get("feeds") or []:
            if x.get("id"):
                out.append((x.get("id"), x.get("xsecToken") or ""))
        return out

    @classmethod
    def imgs(cls, n0, nid, depth):
        """图片/视频封面全部入资源队列（只登记，不下载）。"""
        n = 0
        for im in (n0.get("imageList") or [])[:18]:
            u = im.get("urlDefault") or im.get("urlPre") or ""
            if not u:
                for inf in (im.get("infoList") or []):
                    u = u or inf.get("url") or ""
            if u:
                if enqueue_asset(u, "xiaohongshu:%s" % nid, "image/jpeg",
                                 "%s_%d.jpg" % (nid, n)):
                    n += 1
        v = n0.get("video") or {}
        stream = ((v.get("media") or {}).get("stream") or {})
        for codec in ("h264", "h265", "av1"):
            for s in (stream.get(codec) or [])[:1]:
                u = s.get("masterUrl") or ""
                if u and enqueue_asset(u, "xiaohongshu:%s" % nid, "video/mp4",
                                       "%s.mp4" % nid):
                    n += 1
        return n

    @classmethod
    def handle(cls, url, seed, a):
        depth = seed["depth"]
        nid, tok = cls.parse_url(url)
        if not nid:
            return 1, "拿不到笔记 ID"
        cli = cls.client()
        try:
            try:
                n0, why = cls.detail(cli, nid, tok)
            except Exception as e:
                n0, why = {}, "%s: %s" % (type(e).__name__, str(e)[:40])
            if not n0 and len(cls._channels()) > 1 and cls._chan[0] == 0:
                # 先用另一个出口排除「是本机 IP 被限，还是笔记本身没了」
                cls._bump(why)
                cls.save_cookies(cli)
                cli.close()
                cli = cls.client()
                try:
                    n0, why = cls.detail(cli, nid, tok)
                except Exception as e:
                    n0, why = {}, "%s: %s" % (type(e).__name__, str(e)[:40])
            if not n0:
                # token 过期时回首页现捞一次：同一条笔记可能还在 feed 里
                if not tok:
                    for cid, ctok in cls.explore(cli):
                        if cid == nid:
                            n0, why = cls.detail(cli, nid, ctok)
                            break
                if not n0:
                    cls.save_cookies(cli)
                    return 1, why
            page_url = cls.note_url(nid, tok)
            desc = n0.get("desc") or ""
            title = (n0.get("title") or desc or "").strip()[:120]
            codes, pans = Bilibili.desc_links(desc)
            ii = n0.get("interactInfo") or {}
            u0 = n0.get("user") or {}
            ents, edges = [], []
            ents.append({
                "id": "xiaohongshu:work:%s" % nid, "platform": "xiaohongshu",
                "type": "work", "title": title, "url": page_url, "depth": depth,
                "payload": {"note_id": nid, "type": n0.get("type"),
                            "liked": ii.get("likedCount"),
                            "collected": ii.get("collectedCount"),
                            "comment": ii.get("commentCount"),
                            "ip": n0.get("ipLocation"),
                            "ts": n0.get("time"),
                            "imgs": len(n0.get("imageList") or []),
                            "codes": codes[:8], "pans": pans[:4]},
            })
            aid = str(u0.get("userId") or "")
            if aid:
                ents.append({
                    "id": "xiaohongshu:author:%s" % aid, "platform": "xiaohongshu",
                    "type": "author", "title": u0.get("nickname") or u0.get("nickName") or "",
                    "url": "https://www.xiaohongshu.com/user/profile/%s" % aid,
                    "depth": depth, "payload": {}})
                edges.append(("xiaohongshu:work:%s" % nid,
                              "xiaohongshu:author:%s" % aid, "by_author"))
            tags = []
            for t in (n0.get("tagList") or []):
                nm = (t.get("name") or "").strip()
                if not nm:
                    continue
                tags.append(nm)
                tid = "xiaohongshu:tag:%s" % nm
                ents.append({"id": tid, "platform": "xiaohongshu",
                             "type": "hashtag", "title": nm, "url": "",
                             "depth": depth, "payload": {"id": t.get("id")}})
                edges.append(("xiaohongshu:work:%s" % nid, tid, "tagged"))
            assets = cls.imgs(n0, nid, depth)
            at_users = [x.get("nickname") for x in (n0.get("atUserList") or [])
                        if x.get("nickname")]
        except Exception as e:
            return 1, "%s: %s" % (type(e).__name__, str(e)[:70])
        finally:
            try:
                cls.save_cookies(cli)
                cli.close()
            except Exception:
                pass

        n = save_entities(ents, edges)
        log("  [xhs] %s → 实体 %d（新 %d）/ 边 %d / 标签 %d / 资源 %d"
            % (nid, len(ents), n, len(edges), len(tags), assets))
        record({"ts": time.time(), "platform": "xiaohongshu", "seed": url,
                "page": nid, "entities": len(ents), "new": n,
                "edges": len(edges), "assets": assets})
        # 正文里的代码托管地址 → GitHub 队列（跟 B 站简介同一条价值：低星个人项目）
        gh = 0
        for u in codes[:6]:
            if push_seed(u, "github", depth + 1, "xhs-desc", prio=0):
                gh += 1
        return 1, "ok 新增 %d / 标签 %d / 仓库 %d%s" % (
            n, len(tags), gh, (" / @" + ",".join(at_users[:2])) if at_users else "")


class Douban:
    """豆瓣：一个条目链接 → 作品 / 人物 / 类型 / 20 条相关推荐 / 短评。

    实测（2026-11-05）全免登录、免签名、纯 HTTP：
        m.douban.com/rexxar/api/v2/{movie,book,music}/{id}                 详情
        m.douban.com/rexxar/api/v2/{type}/{id}/recommendations             20 条相关
        m.douban.com/rexxar/api/v2/{type}/{id}/interests                   短评（total 上万）
        m.douban.com/rexxar/api/v2/subject_collection/{coll}/items         现成分类集合
        movie.douban.com/j/search_subjects?type=&tag=                      电影搜索
        book.douban.com/j/subject_suggest?q=                               读书搜索

    为什么它比抖音值：抖音是「内容流」，看完就过；豆瓣是**书影音知识库**，
    每条自带 导演/演员/作者/类型/评分，人和作品天然构成二分图。
    「同好推荐」还不是「同导演」那种表面关系，是**基于用户行为的协同过滤**，
    扩散质量比标签共现高一档。

    边界（实测）：导演对象里只有名字、没有 id，拿不到「此人全部作品」；
    所以人物只作**汇聚点**（多人共演一部片就连起来了），不作下游种子。
    """
    name = "douban"
    HOSTS = ("douban.com", "doubanio.com")
    HEADERS = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36"),
        "Referer": "https://m.douban.com/",
        "Origin": "https://m.douban.com",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    API = "https://m.douban.com/rexxar/api/v2"
    SID_RE = __import__("re").compile(r"/subject/(\d{3,12})")
    # 三条分类集合：能直接拿「正在上映 / TOP250 / 高分剧集」当类别底座
    COLLECTIONS = ("movie_showing", "movie_top250")

    @classmethod
    def match(cls, url):
        h = (up.urlsplit(url).hostname or "").lower()
        return any(h == d or h.endswith("." + d) for d in cls.HOSTS)

    @classmethod
    def resolve(cls, url):
        """URL → (kind, subject_id)。kind ∈ movie/book/music。

        两种格式都要认：
            https://movie.douban.com/subject/1292052/          ← 标准
            https://www.douban.com/doubanapp/dispatch/book/35268282  ← 分类集合/App 分享
        只认第一种的话，book_latest 那 20 个种子会全部静默失败。
        """
        m = cls.SID_RE.search(url or "")
        if not m:
            m = re.search(r"/(?:movie|book|music)/(\d{3,12})", url or "")
        if not m:
            return "", ""
        h = (up.urlsplit(url).hostname or "").lower()
        kind = "movie"
        if "book" in h or "/book/" in (url or ""):
            kind = "book"
        elif "music" in h or "/music/" in (url or ""):
            kind = "music"
        return kind, m.group(1)

    @staticmethod
    def _api(cli, path, **params):
        try:
            r = cli.get("https://m.douban.com" + path, params=params or None)
            if "json" not in (r.headers.get("content-type") or ""):
                return None
            return r.json()
        except Exception:
            return None

    @classmethod
    def handle(cls, url, seed, a):
        import httpx
        t0 = time.time()
        kind, sid = cls.resolve(url)
        if not sid:
            return 1, "拿不到豆瓣条目 id"
        depth = seed["depth"]
        ents, edges, assets = [], [], 0
        nxt = []
        try:
            with httpx.Client(follow_redirects=True, trust_env=False, timeout=15,
                              headers=cls.HEADERS, verify=False) as cli:
                det = cls._api(cli, "/rexxar/api/v2/%s/%s" % (kind, sid)) or {}
                if not det:
                    return 1, "rexxar 详情无数据（可能被限流或条目不存在）"
                title = str(det.get("title") or "")[:120]
                page_url = "https://%s.douban.com/subject/%s/" % (kind, sid)
                wid = "douban:work:%s" % sid
                rating = (det.get("rating") or {})
                ents.append({
                    "id": wid, "platform": "douban", "type": "work", "title": title,
                    "url": page_url, "depth": depth,
                    "payload": {"kind": kind, "rating": rating.get("value"),
                                "votes": rating.get("count"), "year": det.get("year"),
                                "genres": det.get("genres"),
                                "card_subtitle": str(det.get("card_subtitle") or "")[:160]},
                })

                # 人物：导演/编剧/主演 → 汇聚点。没有 id 就用名字做键，
                # 目的是「同一个人出现在多部作品里」时能把作品连起来。
                for role, key in (("directed_by", "directors"), ("written_by", "writers"),
                                  ("acted_in", "actors")):
                    for p in (det.get(key) or [])[:8]:
                        nm = str(p.get("name") or "").strip()
                        if not nm:
                            continue
                        pid = "douban:person:%s" % nm
                        ents.append({"id": pid, "platform": "douban", "type": "author",
                                     "title": nm, "url": "", "depth": depth,
                                     "payload": {"role": key}})
                        edges.append((wid, pid, role))

                # 类型：现成的分类，不用猜
                for g in (det.get("genres") or [])[:8]:
                    gid = "douban:genre:%s" % g
                    ents.append({"id": gid, "platform": "douban", "type": "hashtag",
                                 "title": str(g), "url": "", "depth": depth, "payload": {}})
                    edges.append((wid, gid, "tagged"))

                # 相关推荐：协同过滤，这条是主力扩散边
                rel = cls._api(cli, "/rexxar/api/v2/%s/%s/recommendations" % (kind, sid))
                rel = rel if isinstance(rel, list) else ((rel or {}).get("recommendations") or [])
                for x in rel[:20]:
                    rid = str(x.get("id") or "")
                    if not rid or rid == sid:
                        continue
                    rwid = "douban:work:" + rid
                    ents.append({"id": rwid, "platform": "douban", "type": "work",
                                 "title": str(x.get("title") or "")[:120],
                                 "url": "https://%s.douban.com/subject/%s/" % (kind, rid),
                                 "depth": depth + 1,
                                 "payload": {"kind": kind,
                                             "rating": (x.get("rating") or {}).get("value")}})
                    edges.append((wid, rwid, "related"))
                    nxt.append(rid)

                # 短评：UGC 文本，喂给语义分类；不是资源，不进下载队列
                it = cls._api(cli, "/rexxar/api/v2/%s/%s/interests" % (kind, sid),
                              count=10, start=0, order_by="hot")
                comments = (it or {}).get("interests") or []
                if comments:
                    ents[0]["payload"]["comments_total"] = (it or {}).get("total")
                    ents[0]["payload"]["comment_samples"] = [
                        str(c.get("comment"))[:120] for c in comments[:5]
                        if c.get("comment")]

                cover = ((det.get("cover") or {}).get("url")
                         or (det.get("pic") or {}).get("large") or det.get("pic"))
                if cover:
                    assets = 1 if enqueue_asset(str(cover), "douban:cover", "image/jpeg") else 0
        except Exception as e:
            return 1, "%s: %s" % (type(e).__name__, str(e)[:70])

        n = save_entities(ents, edges)
        dt = time.time() - t0
        log("  [douban] %s %s → 实体 %d（新 %d）/ 边 %d / 相关 %d | %.2fs"
            % (kind, sid, len(ents), n, len(edges), len(nxt), dt))
        record({"ts": time.time(), "platform": "douban", "seed": url, "page": sid,
                "entities": len(ents), "new": n, "edges": len(edges),
                "assets": assets, "sec": round(dt, 2)})
        pushed = 0
        if depth + 1 <= a.max_depth:
            for rid in nxt[:12]:
                if push_seed("https://%s.douban.com/subject/%s/" % (kind, rid),
                             "douban", depth + 1, "related"):
                    pushed += 1
        return 1, "ok 新增实体 %d / 下游种子 %d" % (n, pushed)


class Site:
    """任意主链接 → 站内 BFS。

    做法刻意简单：HTTP 批量取（快、便宜），只有空壳才升级渲染。
    深度不做无限展开 —— 定向爬取的价值是「把这个站的内容收干净」，
    不是「顺着一万个外链爬全宇宙」（那是主爬虫的活）。
    """
    name = "site"

    @classmethod
    def match(cls, url):
        return True

    @classmethod
    def run(cls, bw, seed, max_pages, budget_left):
        import http_first as hf
        import render_parse as rp
        root = seed["url"]
        host = (up.urlsplit(root).hostname or "").lower()
        depth = seed["depth"]

        # ---- 路线 A：站方自己给的目录（sitemap）。
        # 为什么先走这条：首页链接扩散只能看到「编辑愿意放在首页的东西」，
        # 而且栏目页/分页里的文章永远看不到。sitemap 是全量目录，
        # 实测 thepaper/huxiu/qbitai/cnblogs 拿到的 300 条**全是文章页**。
        pages, cand = [], []
        rest = []
        sm_urls, sm_info = [], {}
        try:
            import sitemap_disc as sm
            sm_urls, sm_info = sm.discover("https://" + host,
                                           max_urls=max(60, max_pages * 3),
                                           log=lambda *a, **k: None)
        except Exception as e:
            log("  [site] sitemap 探测失败：%s" % str(e)[:60])

        # ---- 路线 A 的前置判断：sitemap 给的到底是不是文章
        # 36kr 实测缺陷（2026-11-05）：sitemap 命中 32 条，但全是 /newsflashes/
        # 这类列表页，抓回来还是列表，一篇正文都拿不到，而 routes B 又不触发
        # （因为 pages 从列表页拿到了东西）。结果整个站只收了几个栏目入口。
        # 判据用 URL 形态：正文页通常带日期或内容提示词，列表页不带。
        if sm_urls:
            probe = sm_urls[:60]
            like = sum(1 for u in probe
                       if rp._CONTENT_HINT.search(u) or re.search(r"/\d{4}/\d{2}/", u))
            if like < len(probe) * 0.3:
                log("  [site] sitemap %d 条里仅 %d 条像正文 → 判列表页，改走链接扩散"
                    % (len(sm_urls), like))
                sm_urls = []

        if sm_urls:
            cand = sm_urls[:max(1, max_pages)]
            log("  [site] %s → sitemap 命中 %s，取 %d 篇"
                % (host, str(sm_info.get("hit", "")).split("/")[-1][:18], len(cand)))
            # 顺手把全量清单喂给主爬虫：深挖要质量，扫全量是主爬虫的活
            try:
                import sitemap_disc as sm
                fed = sm.feed_crawler(sm_urls, host)
                if fed:
                    log("  [site] 同时喂主爬虫 %d 条 sitemap URL" % fed)
            except Exception:
                pass
            items2 = [{"domain": host, "url": u, "hard": False} for u in cand]
            s2, r2 = hf.fetch_many(items2, conc=12, timeout=12, log=lambda *a, **k: None)
            pages += list(s2)
            rest += list(r2)

        # ---- 路线 B：sitemap 没有（或拿不到东西）→ 首页链接扩散
        # 注意条件带 not rest：sitemap 命中但全是空壳时（huxiu），
        # rest 里有待渲染的 URL，此时该去渲染，不是回头重走首页扩散。
        if not pages and not rest:
            items = [{"domain": host, "url": root, "hard": False}]
            solved, rest_b = hf.fetch_many(items, conc=8, timeout=12, log=lambda *a, **k: None)
            pages = list(solved)
            rest += list(rest_b)
            for r in solved:
                for l in (r.get("links") or []):
                    u = l.get("url") or ""
                    if u.startswith("/"):
                        u = up.urljoin(root, u)
                    h = (up.urlsplit(u).hostname or "").lower()
                    if h != host and not h.endswith("." + host):
                        continue
                    if u in cand:
                        continue
                    cand.append(u)
                for a in (r.get("assets") or []):
                    enqueue_asset(a.get("url", ""), host)

            # 按「像内容页」排序（复用 render_parse 的判定口径）
            def score(u):
                s = 0
                if rp._CONTENT_HINT.search(u):
                    s += 10
                if re.search(r"/\d{4}/\d{2}/", u):
                    s += 6
                if re.search(r"[a-z0-9]+-[a-z0-9-]{6,}", u):
                    s += 4
                if rp._SKIP_PATH.search(u):
                    s -= 20
                return -s
            cand.sort(key=score)
            cand = cand[:max(0, max_pages - 1)]
            log("  [site] %s → 无 sitemap，站内候选 %d（取 %d）"
                % (host, len(cand), len(cand)))

            if cand:
                items2 = [{"domain": host, "url": u, "hard": False} for u in cand]
                solved_b, rest_b = hf.fetch_many(items2, conc=12, timeout=12,
                                                 log=lambda *a, **k: None)
                pages += list(solved_b)
                rest += list(rest_b)

        # ---- 路线 C：空壳站升级渲染。
        # 实测 huxiu 就是这一类：HTTP 返回 200、16 KB，但 <title> 是空的，
        # 正文全在 JS 里。不渲染的话，sitemap 拿到再准的 URL 也白费。
        # 只在「成功页太少」时才升级，避免把小站也拖进渲染的高成本。
        ok_http = sum(1 for r in pages if r.get("ok"))
        total_http = len(pages) + len(rest)
        # 条件要涵盖「一页都没成功、全在 rest 里」这种最坏情况 ——
        # huxiu 就是这样：sitemap 拿到 10 条准 URL，HTTP 全是空壳，
        # 如果只看 pages 就会误判成「没什么可渲染」，直接放弃整站。
        if total_http and ok_http <= max(1, total_http // 3):
            try:
                import render_parse as rp
                targets = []
                seen_t = set()
                for r in pages:
                    if r.get("ok"):
                        continue
                    u = r.get("url") or ""
                    if u and u not in seen_t:
                        seen_t.add(u)
                        targets.append({"domain": host, "url": u})
                for r in rest:
                    u = r.get("url") or ""
                    if u and u not in seen_t and len(targets) < max_pages:
                        seen_t.add(u)
                        targets.append({"domain": host, "url": u})
                targets = targets[:max_pages]
                if targets:
                    log("  [site] %s HTTP 成功仅 %d/%d → 升级渲染 %d 页"
                        % (host, ok_http, total_http, len(targets)))
                    # 必须放到独立线程里跑：render_stage 内部是 asyncio.run()，
                    # 而外层 with Browser() 已经有一个事件循环在跑，
                    # 原地调用会抛 "asyncio.run() cannot be called from a running event loop"。
                    import concurrent.futures as _cf
                    with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
                        got = _ex.submit(
                            rp.render_stage, targets,
                            workers=2, ntabs=4, timeout=18, batch=10, gpu=True,
                            log=lambda *a, **k: None).result()
                    pages = [r for r in pages if r.get("ok")] + list(got or [])
            except Exception as e:
                log("  [site] 渲染兜底失败：%s" % str(e)[:80])

        new = 0
        ents = []
        for r in pages:
            if not r.get("ok"):
                continue
            u = r.get("url") or ""
            ents.append({"id": "web:%s" % u, "platform": "site", "type": "page",
                         "title": (r.get("title") or "")[:200], "url": u,
                         "depth": depth,
                         "payload": {"text_len": r.get("text_len"),
                                      "quality": (r.get("tags") or {}).get("quality")
                                      if isinstance(r.get("tags"), dict) else None}})
            for a in (r.get("assets") or []):
                enqueue_asset(a.get("url", ""), host)
        if ents:
            new = save_entities(ents)
            # 页正文进知识库（复用现有索引器；upsert 会按当前规则重打标签）
            try:
                import kb_index
                c = kb_index.connect()
                kb_index.ensure()
                for i, r in enumerate(pages):
                    if r.get("ok"):
                        kb_index.upsert(c, r, REC, i)
                c.commit()
                c.close()
            except Exception as e:
                log("    [site] 入知识库失败：%s" % str(e)[:80])
        ok_n = sum(1 for r in pages if r.get("ok"))
        log("  [site] 解析 %d 页（成功 %d）/ 新实体 %d / 路线 %s"
            % (len(pages), ok_n, new, "sitemap" if sm_urls else "链接扩散"))
        record({"ts": time.time(), "platform": "site", "seed": root,
                "pages": len(pages), "ok": ok_n, "new": new,
                "route": "sitemap" if sm_urls else "links"})
        return ok_n, "ok 页 %d（%s）" % (ok_n, "sitemap" if sm_urls else "links")


ADAPTERS = [Douyin, Xiaohongshu, Bilibili, Douban, Site]


# ---------------------------------------------------------------- 主流程
def pick_adapter(url, forced="auto"):
    if forced and forced != "auto":
        for a in ADAPTERS:
            if a.name == forced:
                return a
        return Site
    for a in ADAPTERS:
        try:
            if a.match(url):
                return a
        except Exception:
            continue
    return Site


def bili_search(kw, pages=1):
    """B 站关键词搜索 → BV 号列表。

    为什么要这个入口：不用等种子，直接按「人在聊什么」去找。
    GitHub 那边按 topic 找 = 按**机器分类**找；B 站这边按关键词找 = 按**人话**找，
    两个方向完全不同，后者能捞到前者永远看不到的东西。

    实测（2026-11-05）：必须先 GET 首页拿 buvid3，否则 412 Precondition Failed；
    Referer 必须指向 search.bilibili.com，指向 www 也会 412。拿到这两样就免签可用。
    """
    import httpx
    out = []
    with httpx.Client(follow_redirects=True, trust_env=False, timeout=20,
                      headers=dict(Bilibili.HEADERS), verify=False) as cli:
        try:
            cli.get("https://www.bilibili.com/")       # 拿 buvid3，没它必 412
        except Exception:
            return out
        for pg in range(1, max(1, pages) + 1):
            q = up.quote(kw)
            try:
                d = cli.get(
                    "https://api.bilibili.com/x/web-interface/search/type"
                    "?search_type=video&keyword=%s&page=%d" % (q, pg),
                    headers={"Referer": "https://search.bilibili.com/all?keyword=%s" % q},
                ).json()
            except Exception:
                continue
            if d.get("code") != 0:
                break
            for x in (d.get("data") or {}).get("result") or []:
                bv = x.get("bvid")
                if bv and bv not in out:
                    out.append(bv)
            time.sleep(0.6)
    return out


def _note_seeded(nid):
    """这个笔记 ID 是否已经在队列里（token 每次都变，只能按 ID 判重）。"""
    c = connect()
    try:
        return c.execute("SELECT 1 FROM seeds WHERE url LIKE ? LIMIT 1",
                         ("%/" + nid + "%",)).fetchone() is not None
    finally:
        c.close()


def xhs_explore_into_seeds(times=3):
    """小红书首页灌种子。

    实测（2026-11-05）：每次约 30 条，**5 次零重合**（151 条唯一），0.3s/次。
    这是整个系统里性价比最高的种子源 —— 比抖音一次渲染 12s 拿 108 实体还便宜。
    但它只给「量」，不给关系深度：小红书没有 related 接口，
    work→author 那条边又被 noteId 抹空挡死，所以关系圈只能靠 tag 撑。
    """
    cli = Xiaohongshu.client()
    total = 0
    try:
        for i in range(max(1, times)):
            feeds = Xiaohongshu.explore(cli)
            if not feeds:
                # 这个出口被限：把其余出口都试一遍（直连↔代理），全不通就停
                for _ in range(len(Xiaohongshu._channels())):
                    Xiaohongshu._bump(Xiaohongshu._last_err)
                    cli.close()
                    cli = Xiaohongshu.client()
                    feeds = Xiaohongshu.explore(cli)
                    if feeds:
                        break
            if not feeds:
                log("  第 %d 次首页无数据（%s）→ 所有出口都不通，停手"
                    % (i + 1, Xiaohongshu._last_err))
                break
            added = 0
            for nid, tok in feeds:
                if _note_seeded(nid):
                    continue
                if push_seed(Xiaohongshu.note_url(nid, tok), "xiaohongshu",
                             0, "xhs-explore", prio=1):
                    added += 1
            total += added
            log("  第 %d 次首页 %d 条 → 新种子 %d（累计 %d）"
                % (i + 1, len(feeds), added, total))
    finally:
        try:
            Xiaohongshu.save_cookies(cli)
            cli.close()
        except Exception:
            pass
    return total


DOUBAN_COLLS = {
    "movie": ["movie_top250", "movie_showing", "movie_weekly_best",
              "movie_real_time_hotest"],
    "tv": ["tv_domestic", "tv_american", "tv_japanese", "tv_korean",
           "tv_chinese_best_weekly"],
    "book": ["book_latest"],
    "music": ["music_single"],
}


def douban_coll_into_seeds(kinds=None, pages=2, per=20):
    """豆瓣分类集合灌种子 —— 唯一一个完全不需要种子的豆瓣入口。

    为什么值得单独做：豆瓣其他入口都要求先有一条条目链接（`--seed`），
    手机 24 小时无人值守时没人给链接，那就永远空转。而分类集合是现成的：

        实测（2026-11-05）11 个集合共 545 个条目，全免登录免签名：
          movie_top250 250 / movie_showing 71 / tv_domestic 50 / tv_american 50
          tv_japanese 25 / tv_korean 25 / book_latest 20 / music_single 24
          movie_weekly_best 10 / movie_real_time_hotest 10 / tv_chinese_best_weekly 10
        返回体自带 `url` 字段，可直接当种子。

    踩雷记录：`book_bestsellers` / `music_toplist` / `movie_top_box` 都是 404，
    集合名不能靠猜，得实测。
    """
    kinds = kinds or list(DOUBAN_COLLS)
    total = 0
    import httpx
    with httpx.Client(follow_redirects=True, trust_env=False, timeout=20,
                      headers=dict(Douban.HEADERS)) as cli:
        for kind in kinds:
            for coll in DOUBAN_COLLS.get(kind, []):
                added = 0
                for pg in range(max(1, pages)):
                    try:
                        r = cli.get(
                            "%s/subject_collection/%s/items" % (Douban.API, coll),
                            params={"start": pg * per, "count": per})
                    except Exception as e:
                        log("  %s 第 %d 页异常 %s" % (coll, pg + 1, repr(e)[:60]))
                        break
                    if r.status_code != 200:
                        log("  %s 第 %d 页 HTTP %s" % (coll, pg + 1, r.status_code))
                        break
                    items = (r.json() or {}).get("subject_collection_items") or []
                    if not items:
                        break
                    for it in items:
                        # 分类集合里 movie 给的是标准 subject 链接，
                        # 但 book/music 给的是 doubanapp/dispatch/book/xxx ——
                        # resolve() 的 SID_RE 只认 /subject/(\d+)，直接灌进去必然解析失败。
                        # 统一用 id + type 重构成标准形式。
                        sid = str(it.get("id") or "").strip()
                        kind = str(it.get("type") or "movie").strip()
                        if kind not in ("movie", "book", "music"):
                            kind = "movie"
                        if sid.isdigit():
                            u = "https://%s.douban.com/subject/%s/" % (kind, sid)
                        else:
                            u = it.get("url") or ""
                        if not u:
                            continue
                        if push_seed(u, "douban", 0, "douban-coll:" + coll, prio=1):
                            added += 1
                    time.sleep(0.6)          # 豆瓣对密集请求敏感，慢一点
                total += added
                if added:
                    log("  %-24s 新种子 %d" % (coll, added))
    return total


def main():
    ap = argparse.ArgumentParser(description="定向关联爬取：给一个主链接，深挖这个站")
    ap.add_argument("--seed", action="append", default=[], help="种子链接，可多次给")
    ap.add_argument("--seed-file", help="每行一个种子链接")
    ap.add_argument("--bili-kw", action="append", default=[],
                    help="B 站关键词搜索，可多次。搜到的视频入队，简介里的仓库地址自动进 github 队列")
    ap.add_argument("--bili-pages", type=int, default=1, help="每个关键词搜几页（一页 20 条）")
    ap.add_argument("--xhs-explore", type=int, default=0,
                    help="拉 N 次小红书首页灌种子（每次约 30 条且零重合，实测 0.3s/次）")
    ap.add_argument("--douban-coll", default="",
                    help="豆瓣分类集合灌种子，逗号分隔：movie,tv,book,music；"
                         "给 all 走全部。这是唯一不需要种子链接的豆瓣入口")
    ap.add_argument("--platform", default="auto",
                    choices=["auto", "douyin", "bilibili", "douban",
                             "xiaohongshu", "site"])
    ap.add_argument("--max-pages", type=int, default=20, help="本次最多拉取页面数")
    ap.add_argument("--max-depth", type=int, default=1, help="关联展开深度")
    ap.add_argument("--budget", type=float, default=600, help="时间预算（秒）")
    ap.add_argument("--show", action="store_true", help="只看队列与统计，不爬")
    a = ap.parse_args()

    ensure()
    if a.show:
        c = connect()
        print("种子队列：", dict(c.execute(
            "SELECT state,COUNT(*) FROM seeds GROUP BY state").fetchall()))
        print("实体：", dict(c.execute(
            "SELECT type,COUNT(*) FROM entities GROUP BY type").fetchall()))
        print("关系边：", c.execute("SELECT COUNT(*) FROM edges").fetchone()[0])
        print("最近 5 个种子：")
        for r in c.execute("SELECT url,platform,depth,state FROM seeds "
                           "ORDER BY ts DESC LIMIT 5"):
            print("  ", r)
        c.close()
        return

    seeds = list(a.seed)
    if a.seed_file and os.path.exists(a.seed_file):
        seeds += [l.strip() for l in open(a.seed_file, encoding="utf-8")
                  if l.strip() and not l.startswith("#")]
    # B 站关键词 → 视频入队（走队列，不走 seeds 列表：它们不是手动指定的种子）
    if a.bili_kw:
        n_bv = 0
        for kw in a.bili_kw:
            bvs = bili_search(kw, a.bili_pages)
            log("B站搜索「%s」→ %d 个视频" % (kw, len(bvs)))
            for bv in bvs:
                if push_seed("https://www.bilibili.com/video/%s" % bv, "bilibili",
                             0, "bili-kw:" + kw[:24], prio=1):
                    n_bv += 1
        log("B站关键词共入队 %d 个视频（简介里的仓库地址会在抓取时自动进 github 队列）" % n_bv)
    # 小红书首页 → 作品入队（每次约 30 条全新，纯 HTTP 0.3s）
    if a.xhs_explore:
        log("小红书首页灌种子 × %d 次……" % a.xhs_explore)
        n_xhs = xhs_explore_into_seeds(a.xhs_explore)
        log("小红书共入队 %d 条笔记" % n_xhs)
    # 豆瓣分类集合 → 条目入队（免种子，11 个集合共 545 条）
    if a.douban_coll:
        kinds = (list(DOUBAN_COLLS) if a.douban_coll.strip() == "all"
                 else [k.strip() for k in a.douban_coll.split(",") if k.strip()])
        log("豆瓣分类集合灌种子：%s" % ",".join(kinds))
        n_db = douban_coll_into_seeds(kinds)
        log("豆瓣共入队 %d 个条目" % n_db)
    if not seeds:
        # 没给新种子不代表不能跑：队列里可能还有上一轮展开出来的下游种子。
        # 定向爬取是「越跑越宽」的，每次都要能从断点接着走。
        # 按平台数：`--platform douban` 时队列里只有 B 站种子不算有活干。
        c = connect()
        if a.platform and a.platform != "auto":
            pend = c.execute("SELECT COUNT(*) FROM seeds WHERE state='new' "
                             "AND platform=?", (a.platform,)).fetchone()[0]
        else:
            pend = c.execute("SELECT COUNT(*) FROM seeds WHERE state='new' "
                             "AND platform != 'github'").fetchone()[0]
        c.close()
        if pend == 0:
            log("队列里没有 %s 的待跑种子，本轮空转结束" % (a.platform or "任何"))
            return
        log("未指定种子，接着跑队列里剩余的 %d 个" % pend)

    for s in seeds:
        push_seed(s, pick_adapter(s, a.platform).name, 0, "manual", prio=0)
        log("入队 %s → %s（优先级 0，插队）" % (s[:70], pick_adapter(s, a.platform).name))

    t0 = time.time()
    pages = 0
    done = 0
    # 浏览器改成「按需启动」。
    # 原来是 with Browser() 一把罩住整个循环，而 Browser 用的 sync_playwright()
    # 会在当前线程占住一个事件循环；http_first.fetch_many 里的 asyncio.run()
    # 于是全部报 "cannot be called from a running event loop" ——
    # 36kr 实测就是这个死法（Site 明明一行浏览器代码都不用）。
    from contextlib import ExitStack
    with ExitStack() as stack:
        bw = None
        while pages < a.max_pages and (time.time() - t0) < a.budget:
            seed = pop_seed(exclude=("github",), platform=a.platform)
            if not seed:
                break
            ad = pick_adapter(seed["url"], a.platform)
            if seed["depth"] > a.max_depth:
                finish_seed(seed["url"], "skip", "超过深度上限")
                continue
            try:
                # 两种适配器形态：
                #   handle(url, seed, a)  —— 纯 HTTP 平台（B 站/豆瓣），不需要浏览器
                #   run(bw, seed, ...)    —— 需要渲染的（抖音）或通用站内 BFS
                # 之前主循环只认 run，导致 B 站/豆瓣写完了却一次没被执行过。
                if hasattr(ad, "handle"):
                    n, note = ad.handle(seed["url"], seed, a)
                else:
                    # Site 内部只走 HTTP，需要渲染时自己起浏览器（render_parse 管），
                    # 所以它绝不能拿到外层浏览器 —— 否则外层的事件循环会毒死它。
                    if ad is Site:
                        n, note = ad.run(None, seed, a.max_pages - pages,
                                         a.budget - (time.time() - t0))
                    else:
                        if bw is None:
                            bw = stack.enter_context(Browser())
                        n, note = ad.run(bw, seed, a.max_pages - pages,
                                         a.budget - (time.time() - t0))
                pages += max(1, n)
                done += 1
                finish_seed(seed["url"], "done", note)
            except Exception as e:
                log("  [%s] 失败 %s → %s" % (ad.name, seed["url"][:50], str(e)[:100]))
                finish_seed(seed["url"], "error", str(e)[:180])

    dt = time.time() - t0
    c = connect()
    ent = c.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    ed = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    pend = c.execute("SELECT COUNT(*) FROM seeds WHERE state='new'").fetchone()[0]
    c.close()
    log("")
    log("本次：种子 %d 个 / 拉取 %d 页 / %.1fs（%.2f 页/秒）"
        % (done, pages, dt, pages / max(1e-9, dt)))
    log("累计：实体 %d / 关系边 %d / 队列剩余 %d" % (ent, ed, pend))


if __name__ == "__main__":
    main()
