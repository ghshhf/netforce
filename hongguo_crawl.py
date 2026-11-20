#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""红果短剧扩散器 —— 国内平台最后一块，也是唯一带「有限分类体系」的站。

为什么值得单独写一个模块
--------------------------------
小红书 explore 是随机推荐流：116 个标签里 115 个只用过一次，聚不成圈。
红果正好相反，它有一套**平台固定的题材分类**（脑洞/穿越/系统/古代/都市日常/
逆袭/年代/种田/古风世情…），一部剧挂 4~5 个，几百部剧下来必然形成密集重叠。
这正是关系圈算法要的东西。

三条入口，全部免登录、免签名、纯 HTTP（实测 0.4s/页）：
    /category/{real-drama,ai-drama,comic,comic-drama}?page=N   每页 24 部
    /rank/hot-{drama,real-drama,ai-drama,comic-drama}?page=N   每页 20 部
    /detail?series_id=<sid>                                    单剧完整数据

测过但**不能用**的边（记下来省得以后重测）
------------------------------------------
  ✗ recommend_series（分享页里的 12 部推荐）：是「全平台热门」，不是相似。
     实测从「脑洞/穿越/系统/古代」出发，扩出来的是「都市日常/剧情/逆袭/种田」，
     题材完全漂移 —— 和抖音热门 BGM、GitHub 的 ai 标签同一个病。
  ✗ series-group（JSON-LD 里的系列组）：「第六季」「第十五季」这种明显多季的剧，
     containsSeason 也都是 1。红果的「季」是独立剧集，不共享 group。零信息。

能用的是自顶向下的**目录枚举**（和 sitemap_disc 同构）：先按频道/榜单铺开，
再用题材标签做聚类。推荐边一条都不要。

用法
----
    python hongguo_crawl.py --crawl --pages 3          # 枚举分类+榜单，落轻量记录
    python hongguo_crawl.py --detail-limit 60          # 给已登记的剧补详情
    python hongguo_crawl.py --detail 7701272620355832857
    python hongguo_crawl.py --with-episodes --detail 7701272620355832857
    python hongguo_crawl.py --from-queue               # 接别的平台送进来的种子
    python hongguo_crawl.py --report                   # 题材圈 + 榜单
"""
import argparse
import collections
import html as _h
import json
import os
import re
import sqlite3
import sys
import time

import httpx

DB = (os.environ.get("NETFORCE_DB")
      or os.path.join(os.path.dirname(os.path.abspath(__file__)), "_kb", "seeds.db"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}

# novelquickapp 和 hongguoduanju 是同一后端（实测同一 series_id 返回字节数完全一致），
# 用前者，它 SSR 更完整。
BASE = "https://novelquickapp.com"

CHANNELS = ["real-drama", "ai-drama", "comic-drama"]
RANKS = ["hot-drama", "hot-real-drama", "hot-ai-drama", "hot-comic-drama"]

GAP = 0.4               # 页间隔。实测 0.45s 连跑 28 次无任何风控
RE_SID = re.compile(r"series_id=(\d{15,25})")
RE_PLAYER = re.compile(r"/player/(\d{15,25})(?:/(\d{15,25}))?")


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
        CREATE INDEX IF NOT EXISTS ix_ent_type ON entities(platform, type);
        CREATE INDEX IF NOT EXISTS ix_edges_rel ON edges(rel);
    """)
    c.commit()
    return c


def sid_of(x):
    return "hongguo:series:%s" % x


def cat_id(name):
    return "hongguo:cat:%s" % name


def ep_id(sid, n):
    return "hongguo:episode:%s:%s" % (sid, n)


class Writer:
    def __init__(self, c):
        self.c = c
        self.new_ent = 0
        self.new_edge = 0

    def ent(self, eid, etype, title, url="", depth=0, payload=None, plat="hongguo"):
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

    def exists(self, eid):
        return self.c.execute("SELECT 1 FROM entities WHERE id=?", (eid,)).fetchone() is not None

    def flush(self):
        self.c.commit()


# ---------------------------------------------------------------- 抓取

def _client():
    return httpx.Client(follow_redirects=True, trust_env=False, timeout=25,
                        headers=HEADERS, verify=False)


def _visible(html_text):
    s = re.sub(r"<script.*?</script>", " ", html_text, flags=re.S)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return _h.unescape(re.sub(r"\s+", " ", s))


def parse_list(html_text, kind="category"):
    """列表页：拿 series_id + 标题/集数/题材。

    两种排版完全不同，必须分开处理 ——
      分类页：「全272集 剧名 题材1 题材2」
      榜单页：「1 剧名 9009万热度 新剧 评分9.5 272.2万收藏 700.6万点赞 题材… 简介 播放正片」

    列表页没有结构化 JSON，一次 detail 又是 687KB（1000 部剧 = 687MB），
    所以先靠列表页落轻量记录，题材边的权威版本留给 detail 覆盖。
    """
    ids = list(dict.fromkeys(RE_SID.findall(html_text)))
    txt = _visible(html_text)
    cards = []
    if kind == "rank":
        # 以「播放正片」为分块尾标记，块首是「名次 剧名」
        parts = txt.split("播放正片")
        for p in parts[:-1]:
            m = re.search(r"(?:^|\s)(\d{1,3})\s+(\S{2,40})\s+([\d.]+万?)热度", p)
            if not m:
                continue
            tail = p[m.end():]
            rk = int(m.group(1))
            cats = []
            # 题材段夹在点赞和简介之间；用「X万收藏 / X万点赞」定位
            mm = re.search(r"[\d.]+万?点赞\s+([^。！？]{2,40}?)\s", tail)
            if mm:
                cats = [x for x in re.split(r"[ ,，、]", mm.group(1).strip()) if x][:6]
            cards.append({"name": m.group(2), "eps": None, "cats": cats,
                          "rank_no": rk, "heat": m.group(3)})
    else:
        for m in re.finditer(r"全(\d+)集\s+([^\s]{2,40})\s*([^全]{0,40})", txt):
            cats = [x for x in re.split(r"[ ,，、]", m.group(3).strip()) if x]
            cards.append({"name": m.group(2), "eps": int(m.group(1)),
                          "cats": cats[:6]})
    return ids, cards


def fetch_list(c, kind, slug, page=1):
    if kind == "category":
        u = "%s/category/%s?page=%d" % (BASE, slug, page)
    else:
        u = "%s/rank/%s?page=%d" % (BASE, slug, page)
    r = c.get(u)
    return r.text, u


def parse_detail(html_text):
    """详情页解析。可见文本的格式极其规整，比 JSON-LD 更全：

        <剧名> 5457万热度 评分 8.5 84.8万 次点赞 38.1万 收藏
        真人剧热播榜 No.4 爱情 都市爱情 日久生情 权力斗争
        演员： 高铭屿 方思畅 小冰 … 简介： <正文>

    JSON-LD 有两种形态，都要收：
        TVSeason + episode[]  —— 分享落地页那种
        TVSeries + actor[]    —— 官网详情页那种
    只认 TVSeason 会把一半的剧判成「无数据」（实测踩过：25 部里 19 部误判）。
    """
    out = {}
    for b in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                        html_text, re.S):
        try:
            d = json.loads(b)
        except Exception:
            continue
        flat = []
        for it in (d if isinstance(d, list) else [d]):
            flat.extend(it.get("@graph") or [it])
        for it in flat:
            t = it.get("@type")
            if t in ("TVSeason", "TVSeries"):
                out.setdefault("_ld", it)

    txt = _visible(html_text)
    meta = {}
    mm = re.search(r"([\d.]+万?)热度", txt)
    if mm:
        meta["heat"] = mm.group(1)
    mm = re.search(r"评分\s*([\d.]+)", txt)
    if mm:
        meta["rating"] = float(mm.group(1))
    mm = re.search(r"([\d.]+万?)\s*次点赞", txt)
    if mm:
        meta["like"] = mm.group(1)
    mm = re.search(r"([\d.]+万?)\s*收藏", txt)
    if mm:
        meta["fav"] = mm.group(1)
    meta["ranks"] = re.findall(
        r"(红果热播榜|真人剧热播榜|AI剧热播榜|漫剧热播榜)\s*No\.(\d+)", txt)

    # 题材：夹在「收藏 [榜单 No.X]」和「演员：/简介：」之间
    tags = []
    mm = re.search(r"收藏\s+(?:[^ ]+热播榜\s+No\.\d+\s+)?(.+?)\s+(?:演员：|简介：)", txt)
    if mm:
        tags = [x for x in mm.group(1).split() if 1 < len(x) <= 12][:12]
    meta["tags"] = tags

    # 演员：JSON-LD 与可见文本互为备份（文本版更全，JSON-LD 有时缺）
    actors = []
    mm = re.search(r"演员：\s*(.+?)\s+简介：", txt)
    if mm:
        actors = [x for x in mm.group(1).split() if 1 < len(x) <= 12]
    ld = out.get("_ld") or {}
    if not actors:
        actors = [p.get("name") for p in (ld.get("actor") or []) if p.get("name")]
    meta["actors"] = actors[:30]

    mm = re.search(r"简介：\s*(.{10,1500}?)\s*(?:简介：|播放正片|$)", txt)
    if mm:
        meta["intro"] = mm.group(1)

    meta["title"] = ld.get("name") or ""
    meta["eps"] = ld.get("numberOfEpisodes")
    meta["cover"] = ld.get("image") or ""
    meta["episodes"] = ld.get("episode") or []
    out["_meta"] = meta
    return out


# ---------------------------------------------------------------- 落库

def absorb_series(w, sid, row=None, depth=0):
    """列表页给的轻量记录：只有 id / 标题 / 集数 / 题材。"""
    if row:
        w.ent(sid_of(sid), "series", row["name"],
              "%s/detail?series_id=%s" % (BASE, sid), depth,
              {"series_id": sid, "eps": row["eps"]})
        for t in row["cats"]:
            w.ent(cat_id(t), "hashtag", t)
            w.edge(sid_of(sid), cat_id(t), "tagged")
    else:
        w.ent(sid_of(sid), "series", "", "%s/detail?series_id=%s" % (BASE, sid), depth,
              {"series_id": sid})


def absorb_detail(w, sid, d, with_eps=False):
    """详情页给的完整数据。

    这里最重要的不是题材，是 **actor**。
    题材是「剧的属性」，演员是「人的身份」—— 同一个人演多部剧，
    这条边跨作品、密度适中、且几乎不会误连（跟 GitHub 的 by_owner 同构）。
    红果一部剧 4~12 个题材标签，但 15 个左右的演员，后者的辨识度高得多。
    """
    meta = d.get("_meta") or {}
    title = meta.get("title") or ""
    payload = {
        "series_id": sid,
        "eps": meta.get("eps"),
        "intro": (meta.get("intro") or "")[:1200],
        "cover": meta.get("cover") or "",
        "rating": meta.get("rating"),
        "heat": meta.get("heat"),
        "like": meta.get("like"),
        "fav": meta.get("fav"),
        "ranks": meta.get("ranks") or [],
        "tags": meta.get("tags") or [],
        "actors": meta.get("actors") or [],
    }
    w.ent(sid_of(sid), "series", title or sid,
          "%s/detail?series_id=%s" % (BASE, sid), 0, payload)
    # INSERT OR IGNORE 不会更新旧记录，payload 要强制刷
    w.c.execute(
        "UPDATE entities SET title=?, payload=? WHERE id=? AND platform='hongguo'",
        (title or sid, json.dumps(payload, ensure_ascii=False), sid_of(sid)))

    for t in meta.get("tags") or []:
        w.ent(cat_id(t), "hashtag", t)
        w.edge(sid_of(sid), cat_id(t), "tagged")

    for a in meta.get("actors") or []:
        aid = "hongguo:actor:%s" % a
        w.ent(aid, "actor", a)
        w.edge(sid_of(sid), aid, "acted_in")

    for name, no in (meta.get("ranks") or []):
        rid = "hongguo:rank:%s" % name
        w.ent(rid, "rank", name)
        w.edge(sid_of(sid), rid, "listed_in")

    epl = meta.get("episodes") or []
    if with_eps:
        for e in epl:
            n = e.get("episodeNumber")
            m = RE_PLAYER.search(e.get("url") or "")
            vid = (m.group(2) or m.group(1)) if m else ""
            w.ent(ep_id(sid, n), "episode", (e.get("name") or "")[:120],
                  e.get("url") or "", 1,
                  {"series_id": sid, "n": n, "vid": vid})
            w.edge(sid_of(sid), ep_id(sid, n), "has_episode")
    return title, meta.get("eps") or len(epl)


# ---------------------------------------------------------------- 命令

def cmd_crawl(a):
    c = connect()
    w = Writer(c)
    t0 = time.time()
    pages = max(1, a.pages)
    total_ids = set()
    miss_title = 0

    with _client() as cli:
        cli.get(BASE + "/")
        chans = [a.channel] if a.channel else CHANNELS
        ranks = [] if a.channel else RANKS
        for kind, slugs in (("category", chans), ("rank", ranks)):
            for slug in slugs:
                ch_id = "hongguo:channel:%s" % slug
                if kind == "category":
                    w.ent(ch_id, "channel", slug)
                got_here = 0
                for pg in range(1, pages + 1):
                    time.sleep(GAP)
                    try:
                        txt, u = fetch_list(cli, kind, slug, pg)
                    except Exception as e:
                        print("  %-22s p%d ERR %s" % (slug, pg, str(e)[:40]))
                        break
                    ids, cards = parse_list(txt, kind)
                    if not ids:
                        break
                    fresh = [x for x in ids if x not in total_ids]
                    if not fresh and pg > 1:
                        print("  %-22s p%d 全是重复 → 到底了" % (slug, pg))
                        break
                    # 卡片顺序与 ID 顺序一致，按位配对；数量对不上时宁可不配
                    paired = len(cards) == len(ids)
                    for k, sid in enumerate(ids):
                        total_ids.add(sid)
                        row = cards[k] if paired else None
                        absorb_series(w, sid, row, 0)
                        if kind == "category":
                            w.edge(ch_id, sid_of(sid), "contains")
                        else:
                            rid = "hongguo:rank:%s" % slug
                            w.ent(rid, "rank", slug)
                            w.edge(sid_of(sid), rid, "listed_in")
                    got_here += len(ids)
                    print("  %-6s %-16s p%-2d 拿 %-3d 配标题 %s 累计唯一 %d"
                          % (kind, slug, pg, len(ids),
                             "是(%d)" % len(cards) if paired else "否(%d/%d)"
                             % (len(cards), len(ids)), len(total_ids)))
                    if len(ids) < 10:
                        break
                w.flush()
    dt = time.time() - t0
    print("\n枚举完成：唯一剧 %d / %.1fs / 新增实体 %d / 边 %d"
          % (len(total_ids), dt, w.new_ent, w.new_edge))
    c.close()


def cmd_detail(a):
    c = connect()
    w = Writer(c)
    if a.detail:
        targets = [a.detail]
    else:
        # 只补「还没抓过详情」的（payload 里没有 intro 的）
        rows = c.execute(
            "SELECT id FROM entities WHERE platform='hongguo' AND type='series' "
            "AND payload NOT LIKE '%intro%' LIMIT ?", (a.detail_limit,)).fetchall()
        targets = [r[0].rsplit(":", 1)[-1] for r in rows]
    print("待补详情 %d 部" % len(targets))
    ok = 0
    with _client() as cli:
        cli.get(BASE + "/")
        for i, sid in enumerate(targets, 1):
            time.sleep(GAP)
            try:
                r = cli.get("%s/detail?series_id=%s" % (BASE, sid))
                d = parse_detail(r.text)
            except Exception as e:
                print("  %s ERR %s" % (sid, str(e)[:40]))
                continue
            if not (d.get("_ld")):
                print("  [%d/%d] %s 无 JSON-LD（可能下架）" % (i, len(targets), sid))
                continue
            title, ne = absorb_detail(w, sid, d, a.with_episodes)
            ok += 1
            mt = d["_meta"]
            print("  [%d/%d] %-26s %4s集 题材%-2d 演员%-2d %s"
                  % (i, len(targets), (title or sid)[:26], ne,
                     len(mt.get("tags") or []), len(mt.get("actors") or []),
                     ",".join("%s#%s" % t for t in (mt.get("ranks") or [])) or ""))
            if i % 20 == 0:
                w.flush()
        w.flush()
    print("\n详情完成 %d/%d，新增实体 %d / 边 %d" % (ok, len(targets), w.new_ent, w.new_edge))
    c.close()


def cmd_from_queue(a):
    """把别的平台送进来的红果链接转成种子。

    B 站简介、小红书正文里出现的 hongguoduanju / novelquickapp 链接都在这。
    """
    c = connect()
    w = Writer(c)
    rows = c.execute(
        "SELECT url FROM seeds WHERE state='new' AND (url LIKE '%hongguoduanju%' "
        "OR url LIKE '%novelquickapp%' OR url LIKE '%hainanyuyue%') LIMIT ?",
        (a.limit,)).fetchall()
    ids = []
    for (u,) in rows:
        m = re.search(r"series_id=(\d{15,25})", u or "") or \
            re.search(r"video_id=(\d{15,25})", u or "")
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    print("队列里的红果链接 → 剧 %d 部" % len(ids))
    for sid in ids:
        absorb_series(w, sid, None, 0)
        c.execute("UPDATE seeds SET state='done' WHERE url LIKE ?", ("%" + sid + "%",))
    w.flush()
    print("新增实体 %d / 边 %d" % (w.new_ent, w.new_edge))
    c.close()


def cmd_report(a):
    c = connect()
    print("=" * 66)
    print("红果短剧 · 数据盘点")
    print("=" * 66)
    for t, n in c.execute("SELECT type,count(*) FROM entities WHERE platform='hongguo' "
                          "GROUP BY type ORDER BY 2 DESC"):
        print("  %-10s %d" % (t, n))
    ne = c.execute("SELECT count(*) FROM edges WHERE src LIKE 'hongguo:%'").fetchone()[0]
    print("  边          %d" % ne)

    print("\n--- 题材分布（一部剧挂 4~5 个，重叠就是圈）---")
    for r in c.execute(
            "SELECT e.title, count(*) ct FROM edges g JOIN entities e ON e.id=g.dst "
            "WHERE g.rel='tagged' AND g.src LIKE 'hongguo:%' "
            "GROUP BY e.title ORDER BY ct DESC LIMIT 20"):
        print("  %-14s %d" % r)

    print("\n--- 单题材只用一次的（信息量为零的）---")
    n1, ntot = c.execute(
        "SELECT sum(CASE WHEN ct=1 THEN 1 ELSE 0 END), count(*) FROM ("
        " SELECT dst, count(*) ct FROM edges WHERE rel='tagged'"
        " AND src LIKE 'hongguo:%' GROUP BY dst)").fetchone()
    print("  %s / %s（%.0f%%）" % (n1 or 0, ntot or 0,
                                   100.0 * (n1 or 0) / max(1, ntot or 1)))

    print("\n--- 频道 ---")
    for r in c.execute(
            "SELECT e.title, count(*) FROM edges g JOIN entities e ON e.id=g.dst "
            "WHERE g.rel='contains' AND g.src LIKE 'hongguo:channel:%' GROUP BY e.title"):
        print("  %-14s %d 部" % r)

    print("\n--- 榜单 ---")
    for r in c.execute(
            "SELECT e.title, count(*) FROM edges g JOIN entities e ON e.id=g.dst "
            "WHERE g.rel='listed_in' AND g.src LIKE 'hongguo:%' GROUP BY e.title"):
        print("  %-16s %d 部" % r)

    print("\n--- 集数最多的 10 部 ---")
    for r in c.execute(
            "SELECT title, json_extract(payload,'$.eps') e, json_extract(payload,'$.rating') r "
            "FROM entities WHERE platform='hongguo' AND type='series' "
            "AND json_extract(payload,'$.eps') IS NOT NULL "
            "ORDER BY e DESC LIMIT 10"):
        print("  %-30s %5s集 评分%s" % (r[0][:30], r[1], r[2]))
    c.close()


def main():
    ap = argparse.ArgumentParser(description="红果短剧扩散器")
    ap.add_argument("--crawl", action="store_true", help="枚举分类页+榜单页")
    ap.add_argument("--pages", type=int, default=3, help="每个分类/榜单翻几页")
    ap.add_argument("--channel", default="",
                    help="只跑某个频道（real-drama / ai-drama / comic-drama），"
                         "留空跑全部。实测分页上限：real-drama 约 33 页")
    ap.add_argument("--detail", default="", help="抓单部剧的完整详情")
    ap.add_argument("--detail-limit", type=int, default=0,
                    help="给已登记但没详情的剧补 N 部")
    ap.add_argument("--with-episodes", action="store_true",
                    help="连每集一起落库（102 集 = 102 条实体，慎用）")
    ap.add_argument("--from-queue", action="store_true",
                    help="接队列里别的平台送进来的红果链接")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--report", action="store_true", help="出统计")
    a = ap.parse_args()

    if a.report:
        cmd_report(a)
    elif a.crawl:
        cmd_crawl(a)
    elif a.detail or a.detail_limit:
        cmd_detail(a)
    elif a.from_queue:
        cmd_from_queue(a)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
