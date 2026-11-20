#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""X / Twitter 定向关联爬取 —— **纯直连，零代理**（2026-11-06 打通）

主人的判据（写进设计里，别再绕回去）
------------------------------------
> 「通过直接看那个推特可能进不去，但但凡通过一个分享链接的话，就能直接解析它全部了。
>   分享链接都要分享出去的，网站不会对分享链接设防……基本上所有视频都有分享。」
> 「网络理论上没有封锁，除非物理封锁。」

所以本模块**不去硬连 x.com 主域**，走两段：

    ① 找分享链接：HN Algolia 的 URL 索引里存着 614,971 条 twitter.com 分享链接，
       433 条 x.com 链接，每条带标题/点数/讨论数 —— 纯直连 200，是现成种子池。
    ② 解析分享链接：把 `<user>/status/<id>` 丢给「为分享而存在」的公开端点，
       并发多通道，谁给的信号最完整用谁：

       api.fxtwitter.com/<u>/status/<id>  -> 完整推文 JSON（正文/作者/指标/媒体/t.co）
       noembed.com/embed?url=<分享链接>    -> 推文正文 blockquote + 作者 + 日期
       api.microlink.io/?url=<分享链接>    -> 作者/标题元数据（弱）
       api.allorigins.win/raw?url=...     -> X 原始页（间歇可用，best-effort）

实测坑（2026-11-06）
--------------------
✗ `x.com` 形式的链接明显比 `twitter.com` 难解析：microlink 对 x.com 全 000、对
  twitter.com 200。**统一把分享链接规范化成 twitter.com 再送出去。**
✗ 上一轮把 microlink 的 9.5KB「@elonmusk on X」当成推文元数据 —— 那是**通用页壳**，
  没有正文。判成功的唯一标准是**解析出 text / 推文正文**，不能只看 HTTP 码和字节数。
✗ DoH 结果是脏的：publish.twitter.com 解析到 Facebook 段、nitter.poast 解析到
  Twitter 段、t.co 解析到 Facebook 段。⇒ 每个域都取多个 IP + 补 Cloudflare 公共
  边缘 IP 并发抢答；单个 IP 000 不等于死链（门是间歇开的）。
✓ 同一条推文，fxtwitter 有的 IP 200 / 2471B 完整 JSON，有的 000 ⇒ 等所有 IP 试完
  取有效解，不用 as_completed「谁先回用谁」。

产出（写进 _kb/seeds.db，与 hn / reddit 同构）
---------------------------------------------
实体：tweet / user / domain / hashtag / hn story
边  ：by_author、links_to、mentions、has_tag、discussed_on_hn、has_media

泛化点
------
「找分享链接 → 交给他站解析 → 回填关联图」这套与平台无关。
YouTube / B站 / 抖音的分享链接同样是对外入口，换掉种子来源和解析端点即可复用。

用法
----
    python twitter_crawl.py --seeds 30              # Algolia 抽 30 条种子并解析
    python twitter_crawl.py --ids 1219644556003565568,935578694541770752
    python twitter_crawl.py --seeds 100 --pages 3   # 多页翻种子
    python twitter_crawl.py --report                # 只看库内统计
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import html as html_mod
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

DB = (os.environ.get("NETFORCE_DB")
      or os.path.join(BASE, "_kb", "seeds.db"))

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"

# Cloudflare 公共边缘 IP：只用于「同一 CDN 上换个落点」，不是代理、不是第三方节点
CF_IPS = ["172.66.43.190", "104.16.0.1", "104.16.1.1",
          "162.159.152.4", "104.21.87.14", "104.26.0.84"]

RE_SHARE = re.compile(
    r"https?://(?:www\.)?(?:twitter\.com|x\.com)/([A-Za-z0-9_]+)/status(?:es)?/(\d+)", re.I)
RE_URL = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+", re.I)
RE_MENTION = re.compile(r"(?<!\w)@([A-Za-z0-9_]{1,15})")
RE_TAG = re.compile(r"(?<!\w)#([A-Za-z0-9_\u4e00-\u9fff]{1,40})")

import netpolicy  # noqa: E402
import direct_ip  # noqa: E402
from skynet import dns as _dns  # noqa: E402


# ---------------------------------------------------------------- 纯直连取数

def _env():
    return direct_ip._clean_env()


def _ips(host, forced=None, cap=5):
    """DoH 结果 + 补充边缘 IP；DoH 常被污染，必须多取样。"""
    out = []
    try:
        out = [x for x in _dns.resolve(host, timeout=5) if x][:3]
    except Exception:
        pass
    for x in (forced or CF_IPS):
        if x not in out:
            out.append(x)
    return out[:cap]


def _one(ip, url, timeout=10):
    """单 IP 直取：body 落临时文件、stdout 只留状态码（避免截断混淆）。"""
    h = urlsplit(url).hostname
    fd, tmp = tempfile.mkstemp(prefix="nf_tw_", suffix=".bin")
    os.close(fd)
    try:
        r = subprocess.run(
            ["curl", "-sL", "--noproxy", "*",
             "--resolve", "%s:443:%s" % (h, ip), "-A", UA,
             "--max-time", str(timeout), "-o", tmp, "-w", "%{http_code}", url],
            capture_output=True, timeout=timeout + 6, env=_env(),
            stdin=subprocess.DEVNULL,
            startupinfo=netpolicy._hidden_startupinfo(),
            creationflags=subprocess.CREATE_NO_WINDOW)
        code = (r.stdout or b"").strip().decode("ascii", "ignore")
        body = open(tmp, "rb").read() if os.path.exists(tmp) else b""
        if code.startswith("2") and body:
            return body
    except Exception:
        pass
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return None


def direct_get(url, validator, ips=None, timeout=10):
    """多 IP 并发抢答，**等所有 IP 试完**再取有效解（截断的会先到，不能信先到者）。"""
    h = urlsplit(url).hostname
    cand = ips or _ips(h)
    with cf.ThreadPoolExecutor(max_workers=min(6, len(cand))) as ex:
        bodies = [f.result() for f in cf.as_completed(
            [ex.submit(_one, ip, url, timeout) for ip in cand])]
    best = None
    for b in bodies:
        if not b:
            continue
        try:
            v = validator(b)
        except Exception:
            v = None
        if v:
            # 保留字节最多的一份（同通道不同边缘节点完整度不一样）
            if best is None or len(b) > len(best[0]):
                best = (b, v)
    return best


# ---------------------------------------------------------------- 种子：Algolia

def algolia(path, timeout=15):
    b, v = None, None
    r = direct_get("https://hn.algolia.com" + path,
                   lambda x: (json.loads(x).get("hits")
                              if isinstance(json.loads(x), dict) else None),
                   ips=_ips("hn.algolia.com", [], cap=3), timeout=timeout)
    return (r[1] or []) if r else []


def seeds_from_algolia(pages=1, per_page=50):
    """按 URL 反查被引用的推特分享链接。同一份信息在别人那儿有副本。"""
    seeds, seen = [], set()
    for p in range(pages):
        path = ("/api/v1/search?query=twitter.com"
                "&restrictSearchableAttributes=url&tags=story"
                "&hitsPerPage=%d&page=%d" % (per_page, p))
        for h in algolia(path):
            u = h.get("url") or ""
            m = RE_SHARE.search(u)
            if not m:
                continue
            user, tid = m.group(1), m.group(2)
            if tid in seen:
                continue
            seen.add(tid)
            seeds.append(dict(user=user, tid=tid,
                              hn_title=h.get("title") or "",
                              hn_id=h.get("objectID") or "",
                              points=h.get("points"), comments=h.get("num_comments")))
    return seeds


# ---------------------------------------------------------------- 解析：分享链接

def share_url(user, tid):
    # 🔴 统一 twitter.com 形式：x.com 明显更难被中继解析
    return "https://twitter.com/%s/status/%s" % (user, tid)


def _v_fx(b):
    j = json.loads(b)
    t = j.get("tweet") or {}
    return t if isinstance(t, dict) and t.get("text") else None


def _v_noembed(b):
    j = json.loads(b)
    return j if isinstance(j, dict) and "twitter-tweet" in (j.get("html") or "") else None


def _v_microlink(b):
    j = json.loads(b)
    d = j.get("data") or {}
    return d if isinstance(d, dict) and d.get("title") else None


def _v_html(b):
    s = b.decode("utf-8", "ignore")
    return s if ("og:description" in s or "full_text" in s) and len(s) > 3000 else None


def resolve_tweet(user, tid, timeout=10, deep=False):
    """把分享链接交给多个公开端点，按信号完整度取最优。返回统一 dict。

    deep=False：主通道 fxtwitter 给出完整结构化对象就立即收工（实测 22s→数秒），
    不空等 best-effort 的原始页通道。deep=True 才补跑 allorigins 原始页。
    """
    u = share_url(user, tid)
    out = dict(user=user, tid=tid, url=u, source="", text="", author="",
               created="", likes=None, retweets=None, replies=None,
               urls=[], media=[], mentions=[], tags=[], raw={})

    # ① 主通道：fxtwitter 完整结构化对象
    fx = direct_get("https://api.fxtwitter.com/%s/status/%s" % (user, tid), _v_fx,
                    ips=_ips("api.fxtwitter.com"), timeout=timeout)
    if fx:
        t = fx[1]
        out.update(
            source="fxtwitter",
            text=t.get("text") or "",
            author=((t.get("author") or {}).get("name") or user),
            created=t.get("created_at") or "",
            likes=t.get("likes"), retweets=t.get("retweets"), replies=t.get("replies"),
            raw=t)
        au = t.get("author") or {}
        out["followers"] = au.get("followers")
        out["bio"] = au.get("description") or ""
        # --ids 模式传的是占位 user，用服务端返回的真实 handle 纠正 URL
        real_user = au.get("screen_name") or user
        out["user"] = real_user
        out["url"] = share_url(real_user, tid)
        med = (t.get("media") or {}).get("all") or []
        out["media"] = [m.get("url") for m in med if m.get("url")]
        raw = t.get("raw_text") or {}
        if isinstance(raw, dict):
            out["urls"] = RE_URL.findall(raw.get("text") or "")
        out["urls"] += RE_URL.findall(out["text"])
    if not fx:
        # ② 备通道：noembed 的推文正文 blockquote
        ne = direct_get("https://noembed.com/embed?url=" + quote(u, safe=""),
                        _v_noembed, ips=_ips("noembed.com"), timeout=timeout)
        if ne:
            j = ne[1]
            h = j.get("html") or ""
            txt = re.sub(r"<[^>]+>", " ", h)
            txt = html_mod.unescape(" ".join(txt.split()))
            out.update(source="noembed", text=txt[:1000],
                       author=j.get("author_name") or user,
                       created=j.get("title") or "", raw=j)
            out["urls"] = RE_URL.findall(h)
        else:
            # ③ 弱通道：microlink 元数据（只有壳，不算解析出正文）
            ml = direct_get("https://api.microlink.io/?url=" + quote(u, safe=""),
                            _v_microlink, ips=_ips("api.microlink.io"), timeout=timeout)
            if ml:
                d = ml[1]
                out.update(source="microlink", text=d.get("description") or "",
                           author=d.get("author") or user, raw=d)

    # ④ 原始页 best-effort：默认不跑（实测每条要空等 12s，且主通道已给完整对象）
    if deep:
        ao = direct_get("https://api.allorigins.win/raw?url=" + quote(out["url"], safe=""),
                        _v_html, ips=_ips("api.allorigins.win"), timeout=12)
        if ao:
            m = re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"', ao[1], re.I)
            if m and not out["text"]:
                out["text"] = html_mod.unescape(m.group(1))
            out["raw_page_bytes"] = len(ao[0])
            out["urls"] += RE_URL.findall(ao[1])[:30]

    text = out["text"]
    out["mentions"] = list(dict.fromkeys(RE_MENTION.findall(text)))
    out["tags"] = list(dict.fromkeys(RE_TAG.findall(text)))
    out["urls"] = list(dict.fromkeys(x.rstrip(".,);]") for x in out["urls"]))
    return out


# ---------------------------------------------------------------- 存储

def connect():
    # 多个采集器 + 主爬虫会并发写同一个库 ⇒ 必须给足等待时间，
    # 否则 "database is locked" 会直接把整轮采集打断（2026-11-06 实测踩到）。
    c = sqlite3.connect(DB, timeout=90)
    for pragma in ("PRAGMA busy_timeout=90000",
                   "PRAGMA journal_mode=WAL",
                   "PRAGMA synchronous=NORMAL"):
        try:
            c.execute(pragma)
        except Exception:
            pass
    c.executescript("""
        CREATE TABLE IF NOT EXISTS entities(
            id TEXT PRIMARY KEY, platform TEXT, type TEXT, title TEXT,
            url TEXT, depth TEXT, payload TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS edges(
            src TEXT, dst TEXT, rel TEXT, ts REAL, PRIMARY KEY(src,dst,rel));
        CREATE INDEX IF NOT EXISTS ix_e_platform ON entities(platform);
        CREATE INDEX IF NOT EXISTS ix_edge_src ON edges(src);
        CREATE INDEX IF NOT EXISTS ix_edge_dst ON edges(dst);
    """)
    c.commit()
    return c


class Writer:
    def __init__(self, c):
        self.c = c
        self.new_ent = 0
        self.new_edge = 0

    def ent(self, eid, etype, title, url="", depth=0, payload=None, plat="twitter"):
        cur = self.c.execute(
            "INSERT OR IGNORE INTO entities(id,platform,type,title,url,depth,payload,ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (eid, plat, etype, title or "", url, str(depth),
             json.dumps(payload or {}, ensure_ascii=False), time.time()))
        self.new_ent += cur.rowcount
        return cur.rowcount

    def edge(self, src, dst, rel):
        cur = self.c.execute(
            "INSERT OR IGNORE INTO edges(src,dst,rel,ts) VALUES(?,?,?,?)",
            (src, dst, rel, time.time()))
        self.new_edge += cur.rowcount
        return cur.rowcount

    def flush(self):
        self.c.commit()


def host_of(u):
    try:
        h = (urlsplit(u).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def tid_id(x):
    return "x:tweet:%s" % x


def uid_id(x):
    return "x:user:%s" % x.lower()


def did_id(x):
    return "x:domain:%s" % x


def tag_id(x):
    return "x:tag:%s" % x.lower()


def absorb(w, seed, tw):
    tid, user = tw["tid"], tw["user"]
    t = tid_id(tid)
    payload = {k: v for k, v in tw.items()
               if k in ("likes", "retweets", "replies", "source", "created",
                        "followers", "bio", "raw_page_bytes")}
    if tw["hn_title"]:
        payload["hn_title"] = tw["hn_title"]
    w.ent(t, "tweet", tw["text"][:300] or tw["hn_title"][:300],
          tw["url"], payload=payload)

    a = uid_id(user)
    w.ent(a, "user", tw["author"] or user,
          "https://x.com/%s" % user,
          payload={"followers": tw.get("followers"), "bio": tw.get("bio")})
    w.edge(t, a, "by_author")

    # HN 故事：谁在讨论这条推文（分享链接来自它的索引）
    if seed.get("hn_id"):
        s = "hn:story:%s" % seed["hn_id"]
        w.ent(s, "story", seed.get("hn_title") or "", plat="hn",
              payload={"points": seed.get("points"), "comments": seed.get("comments")})
        w.edge(t, s, "discussed_on_hn")
        w.edge(s, t, "links_to")

    # 外链域名 / 媒体域名 —— 关联图的骨架
    for u in tw["urls"]:
        h = host_of(u)
        if not h or h in ("twitter.com", "x.com"):
            continue
        d = did_id(h)
        w.ent(d, "domain", h, "https://" + h)
        w.edge(t, d, "links_to")
    for u in tw["media"]:
        h = host_of(u)
        if h:
            d = did_id(h)
            w.ent(d, "domain", h, "https://" + h)
            w.edge(t, d, "has_media")
    for m in tw["mentions"]:
        if m.lower() == user.lower():
            continue
        w.ent(uid_id(m), "user", m, "https://x.com/%s" % m)
        w.edge(t, uid_id(m), "mentions")
    for g in tw["tags"]:
        w.ent(tag_id(g), "hashtag", g)
        w.edge(t, tag_id(g), "has_tag")


def report(c):
    print("=== 库内 X/Twitter 资产 ===")
    for r in c.execute("select type,count(*) from entities where platform='twitter' group by type order by 2 desc"):
        print("  实体 %-12s %d" % (r[0], r[1]))
    for r in c.execute("select rel,count(*) from edges where src like 'x:%' group by rel order by 2 desc"):
        print("  边   %-18s %d" % (r[0], r[1]))
    print("  总实体 %d / 总边 %d" % (
        c.execute("select count(*) from entities").fetchone()[0],
        c.execute("select count(*) from edges").fetchone()[0]))


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="X/Twitter 分享链接关联爬取（纯直连）")
    ap.add_argument("--seeds", type=int, default=20, help="从 Algolia 抽多少条分享链接种子")
    ap.add_argument("--pages", type=int, default=1, help="Algolia 翻页数")
    ap.add_argument("--ids", default="", help="直接给 tweet id（逗号分隔，跳过种子）")
    ap.add_argument("--workers", type=int, default=4, help="并行解析数")
    ap.add_argument("--deep", action="store_true", help="额外补跑 allorigins 原始页（慢，默认关）")
    ap.add_argument("--report", action="store_true", help="只输出统计")
    a = ap.parse_args()

    c = connect()
    if a.report:
        report(c)
        return 0

    if a.ids:
        seeds = [dict(user="", tid=x.strip(), hn_title="", hn_id="", points=None, comments=None)
                 for x in a.ids.split(",") if x.strip()]
    else:
        seeds = seeds_from_algolia(pages=a.pages, per_page=min(100, a.seeds))
        seeds = seeds[:a.seeds]
        print("Algolia 抽到分享链接种子: %d 条" % len(seeds))

    w = Writer(c)
    ok = 0
    t0 = time.time()

    def work(s):
        user = s.get("user") or ""   # fxtwitter 可按 id 查，user 留空时用 i 占位
        u = user or "i"
        try:
            tw = resolve_tweet(u, s["tid"], deep=a.deep)
            tw["hn_title"] = s.get("hn_title", "")
            tw["user"] = user or tw.get("raw", {}).get("author", {}).get("screen_name", "") or u
            return s, tw
        except Exception as e:
            return s, None

    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for s, tw in ex.map(work, seeds):
            if not tw:
                print("  ✗ %s 解析失败" % s["tid"])
                continue
            absorb(w, s, tw)
            if tw["text"]:
                ok += 1
            print("  ✓ @%-16s %-12s %-10s %s" % (
                tw["user"][:16], s["tid"][:12], tw["source"],
                tw["text"][:60].replace("\n", " ")))
    w.flush()
    print("\n解析出正文 %d/%d 条；新增实体 %d / 新增边 %d；耗时 %.1fs"
          % (ok, len(seeds), w.new_ent, w.new_edge, time.time() - t0))
    report(c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
