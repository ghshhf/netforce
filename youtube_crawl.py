#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""YouTube 扩散器 —— 国外第一站，也是「related 边」质量最高的平台。

为什么值得先做
--------------
国内四个平台的 related 边都有病：
    小红书 explore → 随机流（116 个标签 115 个只用一次）
    红果 recommend_series → 全平台热门（题材从脑洞漂到都市日常）
    抖音 related → 只有这一条可扩，其余接口全要签名
YouTube 的 watch next 是**真的按内容相似度排的**：
    Rick Astley → Michael Jackson / Eagles / Eminem / Daft Punk
    同年代、同风格，不是「谁火推谁」。

三条边，全部纯 HTTP，零浏览器、零登录、零 API key
------------------------------------------------
    GET  /watch?v=<id>                    1.4MB  元数据 + channelId + handle
    POST /youtubei/v1/next?key=<KEY>      475KB  → 20 条相关视频（全唯一）
    GET  /channel/<id>/videos             1.3MB  → 30 个视频（首屏）

关键坑（实测 2026-11-05）
------------------------
✗ 直连 youtube.com HTTP 000，**必须走代理**（127.0.0.1:7890）
✗ 初始 HTML 的 ytInitialData 里 `secondaryResults` 是 **0 条**
  —— 相关视频是异步拉的，只抓 HTML 会以为「没有 related」
✗ 新版渲染器是 `lockupViewModel`，视频 ID 在 `contentId`
  老版才是 `videoRenderer` / `compactVideoRenderer` 的 `videoId`，两种都要认
✓ key / clientVersion / visitorData 全在 HTML 的 ytcfg 里，正则抠即可

用法
----
    python youtube_crawl.py --video dQw4w9WgXcQ
    python youtube_crawl.py --expand dQw4w9WgXcQ --rounds 2 --limit 10
    python youtube_crawl.py --channel UCuAXFkgsw1L7xaCfnd5JJOw
    python youtube_crawl.py --report
"""
import argparse
import collections
import json
import os
import re
import sqlite3
import time
import urllib.parse

import httpx

DB = (os.environ.get("NETFORCE_DB")
      or os.path.join(os.path.dirname(os.path.abspath(__file__)), "_kb", "seeds.db"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
H = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
PROXY = os.environ.get("NETFORCE_PROXY", "http://127.0.0.1:7890")
GAP = 0.6

RE_KEY = re.compile(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"')
RE_VER = re.compile(r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"]+)"')
RE_VIS = re.compile(r'"VISITOR_DATA"\s*:\s*"([^"]+)"')
RE_CH = re.compile(r'"channelId"\s*:\s*"(UC[\w-]{20,24})"')
RE_VID = re.compile(r'"videoId"\s*:\s*"([\w-]{11})"')


def grab(s, marker):
    """按括号配平抠 JS 对象。ytInitialData 后面接别的代码，正则 `\\{.*?\\}` 会截断。"""
    i = s.find(marker)
    if i < 0:
        return None
    j = i + len(marker)
    while j < len(s) and s[j] in " \t\r\n=":
        j += 1
    if j >= len(s) or s[j] not in "{[":
        return None
    op, cl = s[j], "}" if s[j] == "{" else "]"
    d = 0
    ins = esc = False
    for k in range(j, len(s)):
        ch = s[k]
        if ins:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                ins = False
            continue
        if ch == '"':
            ins = True
        elif ch == op:
            d += 1
        elif ch == cl:
            d -= 1
            if d == 0:
                return s[j: k + 1]
    return None


class YT:
    """一次会话：懒取 key / version / visitor，之后所有 innertube 调用复用。"""

    def __init__(self, proxy=None):
        self.cli = httpx.Client(proxy=proxy or PROXY, verify=False,
                                trust_env=False, timeout=35, headers=H,
                                follow_redirects=True)
        self.key = self.ver = self.vis = None

    def close(self):
        try:
            self.cli.close()
        except Exception:
            pass

    def bootstrap(self, seed_video="dQw4w9WgXcQ"):
        r = self.cli.get("https://www.youtube.com/watch?v=%s" % seed_video)
        t = r.text
        self.key = (RE_KEY.search(t) or [None, None])[1]
        self.ver = (RE_VER.search(t) or [None, None])[1]
        self.vis = (RE_VIS.search(t) or [None, None])[1]
        return t

    def _ctx(self):
        return {"client": {"clientName": "WEB", "clientVersion": self.ver or "2.20241105.00.00",
                           "hl": "en", "gl": "US", "visitorData": self.vis}}

    def watch(self, vid):
        """watch 页：元数据 + channelId。"""
        r = self.cli.get("https://www.youtube.com/watch?v=%s" % vid)
        return r.text

    def next_of(self, vid):
        """真正的相关视频边。只抓 watch 页会拿到 0 条。"""
        if not self.key:
            self.bootstrap()
        r = self.cli.post(
            "https://www.youtube.com/youtubei/v1/next?key=%s&prettyPrint=false" % self.key,
            json={"context": self._ctx(), "videoId": vid,
                  "racyCheckOk": True, "contentCheckOk": True})
        if r.status_code != 200:
            return None
        return r.json()

    def channel_videos(self, cid):
        r = self.cli.get("https://www.youtube.com/channel/%s/videos" % cid)
        return r.text


# ---------------------------------------------------------------- 解析

def parse_player(html_text):
    """元数据在金矿里：ytInitialPlayerResponse 一次给全。"""
    raw = grab(html_text, "ytInitialPlayerResponse")
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except Exception:
        return {}
    vd = d.get("videoDetails") or {}
    mf = ((d.get("microformat") or {}).get("playerMicroformatRenderer") or {})
    return {
        "video_id": vd.get("videoId"),
        "title": vd.get("title"),
        "channel": vd.get("author"),
        "channel_id": vd.get("channelId"),
        "length": vd.get("lengthSeconds"),
        "views": vd.get("viewCount"),
        "keywords": (vd.get("keywords") or [])[:15],
        "category": mf.get("category"),
        "publish": mf.get("publishDate"),
        "live": vd.get("isLiveContent"),
        "desc": (vd.get("shortDescription") or "")[:800],
    }


def _title_of(o):
    for f in ("title", "headline"):
        tt = o.get(f) or {}
        if tt.get("simpleText"):
            return tt["simpleText"]
        if tt.get("runs"):
            return "".join(x.get("text", "") for x in tt["runs"])
    md = ((o.get("metadata") or {}).get("lockupMetadataViewModel") or {})
    tt = md.get("title") or {}
    if tt.get("content"):
        return tt["content"]
    return ""


def _byline(o):
    for f in ("longBylineText", "shortBylineText"):
        runs = (o.get(f) or {}).get("runs") or []
        if runs:
            return runs[0].get("text")
    return ""


def walk_videos(o):
    """两种渲染器都要认。新版 lockupViewModel 的 ID 在 contentId，不是 videoId。"""
    if isinstance(o, dict):
        for k, v in o.items():
            if k in ("videoRenderer", "compactVideoRenderer", "gridVideoRenderer"):
                vid = v.get("videoId")
                if vid:
                    yield vid, _title_of(v)[:120], _byline(v)
            elif k == "lockupViewModel":
                cid = v.get("contentId")
                if cid and len(cid) == 11:
                    yield cid, _title_of(v)[:120], _byline(v)
            yield from walk_videos(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk_videos(v)


def parse_next(d):
    """从 next 响应里抽相关视频 + 「接下来播放」的播放列表。"""
    out = []
    seen = set()
    for vid, title, ch in walk_videos(d):
        if vid in seen:
            continue
        seen.add(vid)
        out.append({"video_id": vid, "title": title, "channel": ch})
    pls = []
    if isinstance(d, dict):
        txt = json.dumps(d, ensure_ascii=False)
        for m in re.finditer(r'"playlistId"\s*:\s*"(PL[\w-]{16,})"', txt):
            if m.group(1) not in pls:
                pls.append(m.group(1))
    return out, pls


# ---------------------------------------------------------------- 存储

def connect():
    c = sqlite3.connect(DB, timeout=30)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS entities(
            id TEXT PRIMARY KEY, platform TEXT, type TEXT, title TEXT,
            url TEXT, depth TEXT, payload TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS edges(
            src TEXT, dst TEXT, rel TEXT, ts REAL,
            PRIMARY KEY(src, dst, rel));
    """)
    c.commit()
    return c


def vid_id(x):
    return "youtube:video:%s" % x


def ch_id(x):
    return "youtube:channel:%s" % x


class Writer:
    def __init__(self, c):
        self.c = c
        self.new_ent = 0
        self.new_edge = 0

    def ent(self, eid, etype, title, url="", depth=0, payload=None, plat="youtube"):
        cur = self.c.execute(
            "INSERT OR IGNORE INTO entities(id,platform,type,title,url,depth,payload,ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (eid, plat, etype, title, url, str(depth),
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


def absorb_video(w, meta, depth=0):
    vid = meta.get("video_id")
    if not vid:
        return None
    payload = {k: meta.get(k) for k in
               ("title", "channel", "length", "views", "keywords", "category", "publish", "desc")}
    w.ent(vid_id(vid), "video", (meta.get("title") or vid)[:140],
          "https://www.youtube.com/watch?v=%s" % vid, depth, payload)
    cid = meta.get("channel_id")
    if cid:
        w.ent(ch_id(cid), "author", meta.get("channel") or cid,
              "https://www.youtube.com/channel/%s" % cid)
        w.edge(vid_id(vid), ch_id(cid), "by_author")
    for kw in (meta.get("keywords") or [])[:12]:
        k = str(kw).strip().lower()
        if not k or len(k) > 60:
            continue
        w.ent("youtube:kw:%s" % k, "hashtag", k)
        w.edge(vid_id(vid), "youtube:kw:%s" % k, "tagged")
    return vid


def absorb_related(w, src_vid, rows, depth=0):
    """related 边。这是 YouTube 最值钱的一条。"""
    n = 0
    for r in rows:
        v = r["video_id"]
        if v == src_vid:
            continue
        if w.ent(vid_id(v), "video", r.get("title") or v,
                 "https://www.youtube.com/watch?v=%s" % v, depth,
                 {"title": r.get("title"), "channel": r.get("channel")}):
            n += 1
        if r.get("channel"):
            w.ent("youtube:chname:%s" % r["channel"], "author", r["channel"])
            w.edge(vid_id(v), "youtube:chname:%s" % r["channel"], "by_author")
        w.edge(vid_id(src_vid), vid_id(v), "related")
    return n


# ---------------------------------------------------------------- 命令

def cmd_video(a):
    c = connect()
    w = Writer(c)
    yt = YT()
    try:
        for vid in a.video:
            t0 = time.time()
            html = yt.watch(vid)
            meta = parse_player(html)
            absorb_video(w, meta, 0)
            print("✓ %s  %s | %s | %ss | %s views"
                  % (vid, (meta.get("title") or "?")[:44], meta.get("channel"),
                     meta.get("length"), meta.get("views")))
            time.sleep(GAP)
            d = yt.next_of(vid)
            if d:
                rows, pls = parse_next(d)
                new = absorb_related(w, vid, rows, 1)
                print("   related %d 条（新增 %d）  播放列表 %d 个  %.1fs"
                      % (len(rows), new, len(pls), time.time() - t0))
                for r in rows[:5]:
                    print("     %s %s" % (r["video_id"], (r["title"] or "")[:44]))
            else:
                print("   related 拿不到")
            w.flush()
    finally:
        yt.close()
        c.close()
    print("\n新增实体 %d / 边 %d" % (w.new_ent, w.new_edge))


def cmd_expand(a):
    """多轮扩散：related 是分层走的，两轮就能看出题材会不会漂。"""
    c = connect()
    w = Writer(c)
    yt = YT()
    done = set()
    queue = list(a.expand)
    try:
        for rnd in range(a.rounds):
            batch = queue[:a.limit]
            queue = queue[a.limit:]
            if not batch:
                break
            print("\n=== 第 %d 轮：%d 个视频 ===" % (rnd + 1, len(batch)))
            for i, vid in enumerate(batch, 1):
                if vid in done:
                    continue
                done.add(vid)
                time.sleep(GAP)
                html = yt.watch(vid)
                meta = parse_player(html)
                absorb_video(w, meta, rnd)
                d = yt.next_of(vid)
                if not d:
                    print("  [%2d] %s 失败" % (i, vid))
                    continue
                rows, _ = parse_next(d)
                new = absorb_related(w, vid, rows, rnd + 1)
                if rnd + 1 < a.rounds:
                    for r in rows:
                        if r["video_id"] not in done:
                            queue.append(r["video_id"])
                print("  [%2d/%2d] %-34s %-16s rel %2d 新%2d 队列%3d"
                      % (i, len(batch), (meta.get("title") or vid)[:34],
                         (meta.get("channel") or "")[:16], len(rows), new, len(queue)))
                if i % 5 == 0:
                    w.flush()
            w.flush()
            queue = list(dict.fromkeys(queue))
    finally:
        yt.close()
        c.close()
    print("\n处理 %d 个视频 / 新增实体 %d / 边 %d" % (len(done), w.new_ent, w.new_edge))


def cmd_channel(a):
    c = connect()
    w = Writer(c)
    yt = YT()
    try:
        for cid in a.channel:
            time.sleep(GAP)
            html = yt.channel_videos(cid)
            vids = list(dict.fromkeys(RE_VID.findall(html)))
            print("频道 %s → %d 个视频" % (cid, len(vids)))
            w.ent(ch_id(cid), "author", cid, "https://www.youtube.com/channel/%s" % cid)
            for v in vids[:40]:
                w.ent(vid_id(v), "video", v, "https://www.youtube.com/watch?v=%s" % v, 1)
                w.edge(ch_id(cid), vid_id(v), "posted")
            w.flush()
    finally:
        yt.close()
        c.close()
    print("新增实体 %d / 边 %d" % (w.new_ent, w.new_edge))


def cmd_report(a):
    c = connect()
    print("=" * 68)
    print("YouTube · 数据盘点")
    print("=" * 68)
    for t, n in c.execute("SELECT type,count(*) FROM entities WHERE platform='youtube' "
                          "GROUP BY type ORDER BY 2 DESC"):
        print("  %-10s %d" % (t, n))
    for r, n in c.execute("SELECT rel,count(*) FROM edges WHERE src LIKE 'youtube:%' "
                          "GROUP BY rel ORDER BY 2 DESC"):
        print("  %-12s %d" % (r, n))
    print("\n--- 被推荐最多的视频 ---")
    for r in c.execute(
            "SELECT e.title, count(*) ct FROM edges g JOIN entities e ON e.id=g.dst "
            "WHERE g.rel='related' GROUP BY e.title ORDER BY ct DESC LIMIT 10"):
        print("  %-58s %d" % (str(r[0])[:58], r[1]))
    print("\n--- 播放量最高的 ---")
    for r in c.execute(
            "SELECT title, json_extract(payload,'$.views') v, "
            "json_extract(payload,'$.channel') ch FROM entities "
            "WHERE platform='youtube' AND type='video' "
            "AND json_extract(payload,'$.views') IS NOT NULL "
            "ORDER BY CAST(json_extract(payload,'$.views') AS INTEGER) DESC LIMIT 8"):
        print("  %-46s %12s  %s" % (str(r[0])[:46], r[1], r[2]))
    c.close()


def main():
    ap = argparse.ArgumentParser(description="YouTube 扩散器（纯 HTTP，零浏览器）")
    ap.add_argument("--video", action="append", default=[], help="视频 id，可多次")
    ap.add_argument("--expand", action="append", default=[], help="从这些视频多轮扩散")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--limit", type=int, default=8, help="每轮处理几个")
    ap.add_argument("--channel", action="append", default=[], help="频道 id（UC 开头）")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    if a.report:
        cmd_report(a)
    elif a.expand:
        cmd_expand(a)
    elif a.channel:
        cmd_channel(a)
    elif a.video:
        cmd_video(a)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
