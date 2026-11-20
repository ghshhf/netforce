#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Hacker News 扩散器 —— 国外第三站，也是最省事的一站。

为什么先写它
-----------
**直连可用、零密钥、零登录、不需要代理**。国内四个平台全要绕，
YouTube 要代理，HF 要镜像，只有 HN 是打开就通的。

实测边界（2026-11-05，由探针跑出来）
----------------------------------
✓ `hn.algolia.com/api/v1/search?tags=story&hitsPerPage=1000`
    → **一次请求 1000 条 story 全字段**（1.99MB / 1.85s），这是整个方案的基本盘
✓ `hn.algolia.com/api/v1/items/<id>` → 整棵评论树（实测 32 节点 / 深度 4 / 12KB）
✓ `hacker-news.firebaseio.com/v0/user/<name>.json` → 该号全历史发帖（pg 有 15,565 条）
✓ `restrictSearchableAttributes=url` → 域名反查精度 98%+
✗ **Algolia 的 `nbHits` 完全不可信**：`tags=comment` 报 298,712、`tags=story` 报 45,822,966
  只能当「有/无」用，真实数量必须靠"拿数据 + 精确过滤"
✗ `query=github.com` 不加 `restrictSearchableAttributes=url` 时精度只有 33.7%
  （因为 "com" 是通用 token）
✗ Algolia `paginationLimitedTo=1000`，翻第 11 页直接报错 —— 超量只能按时间切片

★ 核心判据：帖/作者比（p/a）
--------------------------
这是探针最有价值的发现，直接决定"哪个域名值得建边"：

    simonwillison.net  998 帖 / 410 人 = 2.43   ← 真社群，simonw 本人发 198 条
    jvns.ca            822 / 351      = 2.34
    lwn.net            1000 / 443     = 2.26
    github.com         999 / 875      = 1.14   ← **纯噪音，别给 mega-domain 建边**

p/a 高 = 一个域名背后是一群固定的人反复投稿 = 真实关系圈。
p/a ≈ 1 = 人人各发一条 = 只是"链接路过这里"。
所以：**mega-domain 只对具体路径建边**（实测 `github.com/astral-sh/ruff` 精确命中 8 条独立帖）。

用法
----
    python hn_crawl.py --stories 1000              # 拉 1000 条 story（1 次请求）
    python hn_crawl.py --domain simonwillison.net  # 域名反查 + p/a 判定
    python hn_crawl.py --trees 20                  # 给前 20 条拉整棵评论树
    python hn_crawl.py --user pg                   # 某用户全历史
    python hn_crawl.py --report
"""
import argparse
import collections
import json
import os
import re
import sqlite3
import time

import httpx

DB = (os.environ.get("NETFORCE_DB")
      or os.path.join(os.path.dirname(os.path.abspath(__file__)), "_kb", "seeds.db"))

UA = "netforce/1.0 (research crawler; contact via github)"
H = {"User-Agent": UA}
ALG = "https://hn.algolia.com/api/v1"
FB = "https://hacker-news.firebaseio.com/v0"
GAP = 0.35

# mega-domain：p/a 低，只对具体路径建边，不做整域聚合
MEGA = {"github.com", "twitter.com", "x.com", "youtube.com", "medium.com",
        "reddit.com", "nytimes.com", "arxiv.org", "wikipedia.org", "blogspot.com"}

RE_GH = re.compile(r"^(?:www\.)?github\.com/([^/?#]+)/([^/?#]+)", re.I)


def client():
    # HN 直连可用，明确 trust_env=False 免得被系统代理带走
    return httpx.Client(trust_env=False, timeout=40, headers=H, follow_redirects=True)


def host_of(u):
    m = re.match(r"https?://([^/?#]+)", u or "")
    if not m:
        return ""
    h = m.group(1).lower()
    return h[4:] if h.startswith("www.") else h


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


def sid(x):
    return "hn:story:%s" % x


def uid(x):
    return "hn:user:%s" % x


def did(x):
    return "hn:domain:%s" % x


def cid(x):
    return "hn:comment:%s" % x


class Writer:
    def __init__(self, c):
        self.c = c
        self.new_ent = 0
        self.new_edge = 0

    def ent(self, eid, etype, title, url="", depth=0, payload=None, plat="hn"):
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


# ---------------------------------------------------------------- 落库

def absorb_hit(w, h, depth=0):
    """Algolia 的 hit 结构：objectID / author / url / points / num_comments / created_at_i"""
    oid = h.get("objectID") or h.get("id")
    if not oid:
        return None
    title = (h.get("title") or h.get("story_title") or "")[:200]
    url = h.get("url") or h.get("story_url") or ""
    w.ent(sid(oid), "story", title,
          "https://news.ycombinator.com/item?id=%s" % oid, depth,
          {"story_id": oid, "points": h.get("points"),
           "comments": h.get("num_comments"), "ts": h.get("created_at_i"),
           "url": url, "type": h.get("_tags")})
    a = h.get("author")
    if a:
        w.ent(uid(a), "author", a,
              "https://news.ycombinator.com/user?id=%s" % a)
        w.edge(sid(oid), uid(a), "by_author")

    # ★ 跨平台桥一：外链域名
    host = host_of(url)
    if host:
        w.ent(did(host), "domain", host, "https://" + host)
        w.edge(sid(oid), did(host), "links_to")
        # ★ 跨平台桥二：GitHub 仓库，ID 与 gh_crawl.py 完全一致
        m = RE_GH.match(url.split("//", 1)[-1])
        if m:
            repo = "%s/%s" % (m.group(1), m.group(2))
            if repo.lower().endswith(".git"):
                repo = repo[:-4]
            w.ent("github:repo:%s" % repo, "repo", repo,
                  "https://github.com/%s" % repo, depth,
                  {"full_name": repo}, plat="github")
            w.edge(sid(oid), "github:repo:%s" % repo, "links_to_repo")

    # 评论节点（Algolia 平铺时也会带）
    for k in h.get("kids") or []:
        w.ent(cid(k), "comment", "", "https://news.ycombinator.com/item?id=%s" % k)
        w.edge(sid(oid), cid(k), "has_comment")
    return oid


def absorb_tree(w, tree, depth=1):
    """Algolia items/<id> 返回整棵树：children 嵌套。"""
    n, edges = 0, 0

    def walk(node, parent=None, d=1):
        nonlocal n, edges
        if not isinstance(node, dict):
            return
        nid = node.get("id")
        if nid:
            a = node.get("author")
            txt = re.sub(r"<[^>]+>", " ", node.get("text") or "")[:300]
            w.ent(cid(nid), "comment", txt,
                  "https://news.ycombinator.com/item?id=%s" % nid, d,
                  {"author": a, "parent": node.get("parent_id"),
                   "story": node.get("story_id"), "ts": node.get("created_at_i")})
            n += 1
            if a:
                w.ent(uid(a), "author", a,
                      "https://news.ycombinator.com/user?id=%s" % a)
                w.edge(cid(nid), uid(a), "by_author")
                edges += 1
            if parent:
                w.edge(cid(nid), cid(parent), "reply_to")
                edges += 1
        for ch in node.get("children") or []:
            walk(ch, nid, d + 1)

    walk(tree)
    return n, edges


# ---------------------------------------------------------------- 命令

def cmd_stories(a):
    c = connect()
    w = Writer(c)
    got = 0
    with client() as cli:
        pages = max(1, (a.stories + 999) // 1000)
        for pg in range(pages):
            time.sleep(GAP)
            r = cli.get(ALG + "/search", params={
                "tags": a.tags, "hitsPerPage": 1000, "page": pg})
            if r.status_code != 200:
                print("  第 %d 页 HTTP %s" % (pg + 1, r.status_code))
                break
            d = r.json()
            hits = d.get("hits") or []
            if not hits:
                break
            for h in hits:
                if absorb_hit(w, h):
                    got += 1
            print("  第 %d 页 %d 条 → 累计 %d（真实总数算法：拿数据，不信 nbHits=%s）"
                  % (pg + 1, len(hits), got, d.get("nbHits")))
            w.flush()
            if len(hits) < 1000:
                break
    print("\nstory %d 条 / 新增实体 %d / 边 %d" % (got, w.new_ent, w.new_edge))
    c.close()


def cmd_domain(a):
    """域名反查 + p/a 判定。这是探针那个发现落地的地方。"""
    c = connect()
    w = Writer(c)
    with client() as cli:
        for host in a.domain:
            time.sleep(GAP)
            r = cli.get(ALG + "/search", params={
                "query": host, "restrictSearchableAttributes": "url",
                "tags": "story", "hitsPerPage": 1000})
            if r.status_code != 200:
                print("  %-26s HTTP %s" % (host, r.status_code))
                continue
            hits = r.json().get("hits") or []
            # 精确过滤：Algolia 的模糊匹配会把 "com" 当通用 token
            exact = [h for h in hits if host_of(h.get("url") or "") == host]
            authors = collections.Counter(h.get("author") for h in exact if h.get("author"))
            pa = len(exact) / max(1, len(authors))
            verdict = ("真社群，建整域边" if pa >= 1.8 else
                       ("mega-domain，只对该路径建边" if host in MEGA else "噪音，跳过"))
            print("  %-26s 精确 %4d 帖 / %3d 人 = p/a %.2f  %s"
                  % (host, len(exact), len(authors), pa, verdict))
            if len(exact) < 3:
                continue
            if pa < 1.8:
                # 不建整域边，但把 top 作者记录下来
                for name, k in authors.most_common(3):
                    print("       top 作者 %-18s %d 条" % (name, k))
                continue
            for h in exact:
                absorb_hit(w, h, 1)
            w.flush()
    print("\n新增实体 %d / 边 %d" % (w.new_ent, w.new_edge))
    c.close()


def cmd_trees(a):
    """给库里排前的 story 拉整棵评论树。"""
    c = connect()
    w = Writer(c)
    rows = c.execute(
        "SELECT id FROM entities WHERE platform='hn' AND type='story' "
        "ORDER BY CAST(json_extract(payload,'$.comments') AS INTEGER) DESC LIMIT ?",
        (a.trees,)).fetchall()
    ids = [r[0].rsplit(":", 1)[-1] for r in rows]
    print("拉 %d 棵评论树" % len(ids))
    with client() as cli:
        for i, oid in enumerate(ids, 1):
            time.sleep(GAP)
            r = cli.get(ALG + "/items/%s" % oid)
            if r.status_code != 200:
                print("  [%d] %s HTTP %s" % (i, oid, r.status_code))
                continue
            n, e = absorb_tree(w, r.json())
            print("  [%d/%d] %s → %d 节点 / %d 边" % (i, len(ids), oid, n, e))
            if i % 5 == 0:
                w.flush()
        w.flush()
    print("\n新增实体 %d / 边 %d" % (w.new_ent, w.new_edge))
    c.close()


def cmd_user(a):
    c = connect()
    w = Writer(c)
    with client() as cli:
        for name in a.user:
            time.sleep(GAP)
            r = cli.get(FB + "/user/%s.json" % name)
            if r.status_code != 200 or not isinstance(r.json(), dict):
                print("  %s 拿不到" % name)
                continue
            d = r.json()
            subs = d.get("submitted") or []
            print("  %-18s karma %-7s 历史发帖 %d 条" % (name, d.get("karma"), len(subs)))
            w.ent(uid(name), "author", name,
                  "https://news.ycombinator.com/user?id=%s" % name,
                  0, {"karma": d.get("karma"), "created": d.get("created"),
                      "submitted": len(subs)})
            # 只对前 60 条拉详情，不然一个号就能打满配额
            for oid in subs[:60]:
                time.sleep(0.12)
                r2 = cli.get(ALG + "/items/%s" % oid)
                if r2.status_code != 200:
                    continue
                it = r2.json()
                if it.get("type") == "story":
                    absorb_hit(w, {"objectID": oid, "author": name,
                                   "title": it.get("title"), "url": it.get("url"),
                                   "points": it.get("points"),
                                   "num_comments": len(it.get("children") or []),
                                   "created_at_i": it.get("created_at_i")}, 1)
                else:
                    w.edge(uid(name), uid(name), "active_in")
            w.flush()
    print("\n新增实体 %d / 边 %d" % (w.new_ent, w.new_edge))
    c.close()


def cmd_author_graph(a):
    """把评论树折成 author → author 的回复边。

    为什么必须折
    -----------
    comment 节点对「发现资源」毫无价值 —— 它既不是入口也不是分类维度，
    但它是唯一能算出「谁在跟谁对话」的原始材料。
    评论树一拉就是几万节点（20 棵树 = 6.6 万 comment），留在图里纯属噪音；
    折成 author 边之后，20 棵树只剩几千条边，且每条都有社交含义：

        simonw --replied_to--> tosh        表示前者接过后者的话题

    这是 HN 唯一能贡献给关系图的东西，比 story 列表值钱。
    """
    c = connect()
    now = time.time()
    before = c.execute("SELECT count(*) FROM edges WHERE rel='replied_to'").fetchone()[0]

    # 一条 reply_to(comment→comment) + 两端 author = 一条 replied_to(author→author)
    # 自己回自己不算，SQL 里直接排掉
    c.execute("""
        INSERT OR IGNORE INTO edges(src, dst, rel, ts)
        SELECT 'hn:author:' || a1.title, 'hn:author:' || a2.title, 'replied_to', ?
        FROM edges e
        JOIN entities c1 ON c1.id = e.src AND c1.type = 'comment'
        JOIN entities c2 ON c2.id = e.dst AND c2.type = 'comment'
        JOIN edges b1 ON b1.src = c1.id AND b1.rel = 'by_author'
        JOIN edges b2 ON b2.src = c2.id AND b2.rel = 'by_author'
        JOIN entities a1 ON a1.id = b1.dst
        JOIN entities a2 ON a2.id = b2.dst
        WHERE e.rel = 'reply_to' AND a1.id != a2.id
    """, (now,))
    c.commit()

    after = c.execute("SELECT count(*) FROM edges WHERE rel='replied_to'").fetchone()[0]
    print("author→author 边：%d → %d（新增 %d）" % (before, after, after - before))

    print("\n--- 回复最密集的配对（双向都算）---")
    rows = c.execute("""
        SELECT a1.title, a2.title, count(*) FROM (
            SELECT src, dst FROM edges WHERE rel='replied_to'
            UNION ALL
            SELECT dst, src FROM edges WHERE rel='replied_to')
        m JOIN entities a1 ON a1.id = m.src JOIN entities a2 ON a2.id = m.dst
        GROUP BY m.src, m.dst HAVING count(*) >= 3
        ORDER BY 3 DESC LIMIT 12""").fetchall()
    for x, y, n in rows:
        print("  %-20s ↔ %-20s %d 次" % (x, y, n))

    print("\n--- 被回复最多的人（话题中心）---")
    for t, n in c.execute(
            "SELECT e.title, count(*) FROM edges g JOIN entities e ON e.id=g.dst "
            "WHERE g.rel='replied_to' GROUP BY e.title ORDER BY 2 DESC LIMIT 12"):
        print("  %-24s 被回 %d 次" % (t, n))


def cmd_report(a):
    c = connect()
    print("=" * 70)
    print("Hacker News · 数据盘点")
    print("=" * 70)
    for t, n in c.execute("SELECT type,count(*) FROM entities WHERE platform='hn' "
                          "GROUP BY type ORDER BY 2 DESC"):
        print("  %-10s %d" % (t, n))
    for r, n in c.execute("SELECT rel,count(*) FROM edges WHERE src LIKE 'hn:%' "
                          "GROUP BY rel ORDER BY 2 DESC"):
        print("  %-14s %d" % (r, n))

    print("\n--- 域名 p/a 排行（这是判据本身）---")
    rows = c.execute("""
        SELECT e.title, count(*) posts, count(DISTINCT a.dst) authors
        FROM edges g
        JOIN entities e ON e.id = g.dst AND e.type='domain'
        JOIN edges a ON a.src = g.src AND a.rel='by_author'
        WHERE g.rel='links_to' AND g.src LIKE 'hn:%'
        GROUP BY e.title HAVING posts >= 3
        ORDER BY (1.0*posts/MAX(1,authors)) DESC LIMIT 15""").fetchall()
    for t, posts, auth in rows:
        print("  %-28s %4d 帖 / %3d 人 = p/a %.2f" % (t, posts, auth, posts / max(1, auth)))

    print("\n--- 最活跃作者 ---")
    for r in c.execute(
            "SELECT e.title, count(*) ct FROM edges g JOIN entities e ON e.id=g.dst "
            "WHERE g.rel='by_author' AND g.src LIKE 'hn:%' "
            "GROUP BY e.title ORDER BY ct DESC LIMIT 10"):
        print("  %-24s %d" % r)

    print("\n--- 跨平台：链到 GitHub 仓库的 story ---")
    n = c.execute("SELECT count(*) FROM edges WHERE rel='links_to_repo'").fetchone()[0]
    print("  %d 条" % n)
    for r in c.execute("SELECT e.title, e.id FROM edges g JOIN entities e ON e.id=g.dst "
                       "WHERE g.rel='links_to_repo' LIMIT 6"):
        print("  %-44s %s" % (str(r[0])[:44], r[1]))
    c.close()


def main():
    ap = argparse.ArgumentParser(description="Hacker News 扩散器（直连，零密钥）")
    ap.add_argument("--stories", type=int, default=1000, help="拉多少条 story")
    ap.add_argument("--tags", default="story", help="story / show_hn / ask_hn / front_page")
    ap.add_argument("--domain", action="append", default=[], help="域名反查，可多次")
    ap.add_argument("--trees", type=int, default=0, help="给前 N 条拉评论树")
    ap.add_argument("--user", action="append", default=[], help="用户名，可多次")
    ap.add_argument("--author-graph", action="store_true",
                    help="把评论树折成 author→author 的 replied_to 边")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    if a.report:
        cmd_report(a)
    elif a.author_graph:
        cmd_author_graph(a)
    elif a.domain:
        cmd_domain(a)
    elif a.trees:
        cmd_trees(a)
    elif a.user:
        cmd_user(a)
    else:
        cmd_stories(a)


if __name__ == "__main__":
    main()
