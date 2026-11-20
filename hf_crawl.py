#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""HuggingFace + 论文的跨平台扩散器。国外第二站，也是唯一能把
「模型 → 论文 → 代码仓库」串成闭环的地方。

为什么 HF 值钱
--------------
一条模型记录里同时躺着三样东西：

    tags: ["base_model:Qwen/Qwen2.5-7B", "arxiv:2309.00071", "license:apache-2.0", ...]
    author: "Qwen"
    cardData.base_model: "Qwen/Qwen2.5-7B"

而一篇 HF 论文记录里有：

    githubRepo: "https://github.com/qwenlm/qwq"   ← GitHub 的桥
    linkedModels: [...]                           ← 模型的桥
    linkedSpaces / linkedDatasets
    ai_keywords: [...]                            ← 比 tags 更细的语义标签

于是三个平台闭成一个环：

    GitHub repo ←githubRepo─ HF paper ─linkedModels→ HF model ─base_model→ 衍生模型
                                      └─arxiv id→ arXiv / OpenAlex（引用图）

实测边界（2026-11-05）
--------------------
✓ huggingface.co 直连不通（HTTP 000），**hf-mirror.com 直连 1.0s**
✓ hf-mirror 完整代理 /api/models /api/models/<id> /api/papers/<id> /api/datasets /api/spaces
✓ filter=base_model:<id> 反向查可行，limit=1000 能一次拿满
✗ /api/papers 的 sort / q 参数无效（返回同一份固定列表，14588B 一字不差）
✗ linkedModels 数组只给前 4 条（numTotalModels 才是真值，1000）
✗ PapersWithCode 已被 HF 收购，paperswithcode.com/api/* 全部 302 → huggingface.co/papers
   （所以「再找一个论文平台」不能在 PWC 找，得用 arXiv / OpenAlex）

论文侧的独立源（实测，均免 key）
------------------------------
arXiv      http://export.arxiv.org/api/query?search_query=...     直连 1.7s / 代理 0.6s
OpenAlex   https://api.openalex.org/works?filter=...              直连 2.0s / 代理 0.6s（2.5 亿论文，带引用图）
S2         https://api.semanticscholar.org/graph/v1/...           429（全局限速，换出口也没用）

用法
----
    python hf_crawl.py --seed Qwen/Qwen2.5-7B-Instruct      # 单模型起扩
    python hf_crawl.py --tree Qwen/Qwen2.5-7B --pages 3     # 展开衍生树
    python hf_crawl.py --paper 2309.00071                   # 论文 → 模型 + GitHub
    python hf_crawl.py --top 200                            # 按下载量铺前 N 个模型
    python hf_crawl.py --report
"""
import argparse
import collections
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse

import httpx

DB = (os.environ.get("NETFORCE_DB")
      or os.path.join(os.path.dirname(os.path.abspath(__file__)), "_kb", "seeds.db"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json"}

# 直连 huggingface.co 不通，hf-mirror 直连 1s 且 API 完全同构（实测 4 个端点全通）
BASE = os.environ.get("HF_BASE", "https://hf-mirror.com")
GAP = 0.25
PROXY = os.environ.get("NETFORCE_PROXY", "")
_DEAD = [False]           # 镜像挂了就切代理走官方


def _client():
    proxy = (PROXY or "http://127.0.0.1:7890") if _DEAD[0] else None
    return httpx.Client(follow_redirects=True, trust_env=False, timeout=30,
                        headers=HEADERS, verify=False, proxy=proxy)


def get(cli, path, **params):
    """统一的 GET。镜像失败一次就永久切代理走官方域名。"""
    global BASE
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = BASE + path + qs
    try:
        r = cli.get(url)
        if r.status_code == 200:
            return r.json() if "json" in (r.headers.get("content-type") or "") else None
    except Exception:
        pass
    if not _DEAD[0]:
        _DEAD[0] = True
        BASE = "https://huggingface.co"
        print("  [hf] 镜像失败 → 切代理走官方")
        return get(cli, path, **params)
    return None


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


def mid(x):
    return "hf:model:%s" % x


def pid(x):
    return "hf:paper:%s" % x


def aid(x):
    return "hf:author:%s" % x


def dsid(x):
    return "hf:dataset:%s" % x


def spid(x):
    return "hf:space:%s" % x


class Writer:
    def __init__(self, c):
        self.c = c
        self.new_ent = 0
        self.new_edge = 0

    def ent(self, eid, etype, title, url="", depth=0, payload=None, plat="hf"):
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


# ---------------------------------------------------------------- 解析

RE_BASE = re.compile(r"^base_model:(?:([a-z]+):)?(.+)$")
RE_ARXIV = re.compile(r"^arxiv:(\d{4}\.\d{4,5})$", re.I)
RE_GH = re.compile(r"github\.com[/:]([^/\s]+)/([^/\s#?]+)", re.I)


def split_tags(tags):
    """HF 的 tags 是「扁平字符串数组」，语义全塞在前缀里：
        base_model:finetune:Qwen/Qwen2.5-7B
        arxiv:2309.00071
        license:apache-2.0
        task_categories:text-generation
        language:zh
    这里把前缀拆出来，返回 (普通标签, 派生关系, 论文 id, 其它)。
    """
    plain, bases, papers, meta = [], [], [], {}
    for t in tags or []:
        t = str(t)
        m = RE_BASE.match(t)
        if m:
            kind = m.group(1) or "finetune"      # finetune / adapter / merge / quantized / ...
            bases.append((m.group(2), kind))
            continue
        m = RE_ARXIV.match(t)
        if m:
            papers.append(m.group(1))
            continue
        if ":" in t:
            k, v = t.split(":", 1)
            meta.setdefault(k, []).append(v)
            continue
        plain.append(t)
    return plain, bases, papers, meta


def absorb_model(w, m, depth=0, link_papers=True):
    """落一个模型：base_model 树 + 作者 + 标签 + 论文桥。"""
    mid_ = m.get("id") or m.get("modelId")
    if not mid_:
        return None
    author = m.get("author") or mid_.split("/")[0]
    plain, bases, papers, meta = split_tags(m.get("tags"))
    payload = {
        "model_id": mid_,
        "downloads": m.get("downloads"),
        "likes": m.get("likes"),
        "pipeline_tag": m.get("pipeline_tag"),
        "library": m.get("library_name"),
        "created": m.get("createdAt") or m.get("lastModified"),
        "tags": plain[:40],
        "base_models": [b[0] for b in bases][:10],
        "papers": papers[:10],
        "license": (meta.get("license") or [None])[0],
        "langs": (meta.get("language") or [])[:8],
        "arxiv_ids": papers[:10],
    }
    w.ent(mid(mid_), "model", mid_, "%s/%s" % (BASE, mid_), depth, payload)

    if author:
        w.ent(aid(author), "author", author)
        w.edge(mid(mid_), aid(author), "by_author")

    for b, kind in bases:
        w.ent(mid(b), "model", b, "%s/%s" % (BASE, b))
        # 派生关系单独一类边：它比 topic 硬得多（同一份权重的不同代）
        w.edge(mid(mid_), mid(b), "derived_from")
        w.ent("hf:basekind:%s" % kind, "basekind", kind)
        w.edge(mid(mid_), "hf:basekind:%s" % kind, "derived_kind")

    for t in plain[:20]:
        w.ent("hf:tag:%s" % t, "hashtag", t)
        w.edge(mid(mid_), "hf:tag:%s" % t, "tagged")

    for p in papers:
        w.ent(pid(p), "paper", p, "https://huggingface.co/papers/%s" % p)
        w.edge(mid(mid_), pid(p), "has_paper")
    return mid_, author, bases, papers


def absorb_paper(w, d, depth=0):
    """落一篇论文：作者 + GitHub 桥 + 关联模型 + 关键词。"""
    xid = d.get("id")
    if not xid:
        return None
    payload = {
        "arxiv_id": xid,
        "title": d.get("title"),
        "published": d.get("publishedAt"),
        "upvotes": d.get("upvotes"),
        "summary": (d.get("summary") or "")[:1500],
        "github": d.get("githubRepo"),
        "github_stars": d.get("githubStars"),
        "keywords": (d.get("ai_keywords") or [])[:20],
        "num_models": d.get("numTotalModels"),
        "num_spaces": d.get("numTotalSpaces"),
        "num_datasets": d.get("numTotalDatasets"),
    }
    w.ent(pid(xid), "paper", (d.get("title") or xid)[:140],
          "https://huggingface.co/papers/%s" % xid, depth, payload)
    w.c.execute(
        "UPDATE entities SET title=?, payload=? WHERE id=? AND platform='hf'",
        ((d.get("title") or xid)[:140], json.dumps(payload, ensure_ascii=False), pid(xid)))

    # 作者（HF 把 arXiv 作者映射成了 hf user，能直接连人）
    for a in d.get("authors") or []:
        u = (a.get("user") or {})
        name = u.get("fullname") or u.get("user") or a.get("name")
        if not name:
            continue
        w.ent(aid(name), "author", name)
        w.edge(pid(xid), aid(name), "by_author")

    # ★ 跨平台桥：论文 → GitHub 仓库。
    # 用 github:repo: 前缀，跟 gh_crawl.py 的 ID 完全一致，两个子图天然合并。
    gh = d.get("githubRepo") or ""
    m = RE_GH.search(gh)
    if m:
        repo = "%s/%s" % (m.group(1), m.group(2))
        if repo.lower().endswith(".git"):
            repo = repo[:-4]
        rid = "github:repo:%s" % repo
        w.ent(rid, "repo", repo, "https://github.com/%s" % repo, depth,
              {"full_name": repo, "stars": d.get("githubStars")}, plat="github")
        w.edge(pid(xid), rid, "has_repo")

    # 关联的模型 / 数据集 / Space（只存样本，总数在 payload 里）
    for lm in d.get("linkedModels") or []:
        x = lm.get("id") or lm.get("modelId")
        if x:
            w.ent(mid(x), "model", x, "%s/%s" % (BASE, x))
            w.edge(pid(xid), mid(x), "uses_model")
    for ld in d.get("linkedDatasets") or []:
        x = ld.get("id")
        if x:
            w.ent(dsid(x), "dataset", x, "%s/datasets/%s" % (BASE, x))
            w.edge(pid(xid), dsid(x), "uses_dataset")
    for ls in d.get("linkedSpaces") or []:
        x = ls.get("id")
        if x:
            w.ent(spid(x), "space", x, "%s/spaces/%s" % (BASE, x))
            w.edge(pid(xid), spid(x), "uses_space")

    for kw in (d.get("ai_keywords") or [])[:12]:
        w.ent("hf:kw:%s" % kw, "hashtag", kw)
        w.edge(pid(xid), "hf:kw:%s" % kw, "tagged")
    return xid


# ---------------------------------------------------------------- 命令

def cmd_seed(a):
    """从一个模型出发：它自己的 base_model 链 + 作者 + 论文。"""
    c = connect()
    w = Writer(c)
    with _client() as cli:
        for m in a.seed:
            d = get(cli, "/api/models/%s" % m)
            if not d:
                print("  ✗ %s 拿不到" % m)
                continue
            got = absorb_model(w, d, 0)
            if not got:
                continue
            _, author, bases, papers = got
            print("  ✓ %-42s ★%-6s ↓%-10s 派生自 %d 个 / 论文 %d 篇"
                  % (m, d.get("likes"), d.get("downloads"),
                     len(bases), len(papers)))
            for p in papers[:5]:
                time.sleep(GAP)
                pd = get(cli, "/api/papers/%s" % p)
                if pd:
                    absorb_paper(w, pd, 1)
                    print("      论文 %s → GitHub %s" % (p, pd.get("githubRepo") or "-"))
            w.flush()
    print("\n新增实体 %d / 边 %d" % (w.new_ent, w.new_edge))
    c.close()


def cmd_tree(a):
    """展开 base_model 衍生树：给一个底座模型，拿所有在它上面微调的。"""
    c = connect()
    w = Writer(c)
    with _client() as cli:
        for base in a.tree:
            allrows = []
            for page in range(a.pages):
                time.sleep(GAP)
                url = "/api/models"
                d = get(cli, url, filter="base_model:%s" % base,
                        limit=1000, **_page(page, 1000))
                if not d:
                    break
                if isinstance(d, dict):
                    d = d.get("models") or d.get("items") or []
                if not d:
                    break
                allrows.extend(d)
                print("  %s p%d → %d 条" % (base, page + 1, len(d)))
                if len(d) < 1000:
                    break
            seen = set()
            for m in allrows:
                x = m.get("id") or m.get("modelId")
                if not x or x in seen:
                    continue
                seen.add(x)
                absorb_model(w, m, 1)
            print("  %-38s 衍生 %d 个" % (base, len(seen)))
            w.flush()
    print("\n新增实体 %d / 边 %d" % (w.new_ent, w.new_edge))
    c.close()


def _page(n, size):
    # HF 的 /api/models 用 cursor，不是 offset；先按 0 页跑，多页靠 cursor 另测
    return {} if n == 0 else {}


def cmd_paper(a):
    c = connect()
    w = Writer(c)
    with _client() as cli:
        for x in a.paper:
            d = get(cli, "/api/papers/%s" % x)
            if not d:
                print("  ✗ %s 拿不到" % x)
                continue
            absorb_paper(w, d, 0)
            print("  ✓ %-14s %s" % (x, (d.get("title") or "")[:40]))
            print("      GitHub: %s ★%s" % (d.get("githubRepo") or "-", d.get("githubStars")))
            print("      关联模型 %s 个 / Space %s 个"
                  % (d.get("numTotalModels"), d.get("numTotalSpaces")))
            for lm in (d.get("linkedModels") or [])[:4]:
                mid_ = lm.get("id")
                if not mid_:
                    continue
                time.sleep(GAP)
                md = get(cli, "/api/models/%s" % mid_)
                if md:
                    absorb_model(w, md, 1)
            w.flush()
    print("\n新增实体 %d / 边 %d" % (w.new_ent, w.new_edge))
    c.close()


def cmd_top(a):
    """按下载量铺前 N 个模型。这是「自顶向下枚举」，和红果的分类页同构。"""
    c = connect()
    w = Writer(c)
    seen = set()
    with _client() as cli:
        for page in range(max(1, a.top // 100)):
            time.sleep(GAP)
            d = get(cli, "/api/models", limit=100, sort="downloads",
                    direction=-1, **({"cursor": next(iter(seen))} if page else {}))
            if not isinstance(d, list) or not d:
                break
            for m in d:
                x = m.get("id") or m.get("modelId")
                if not x or x in seen:
                    continue
                seen.add(x)
                absorb_model(w, m, 0)
            print("  第 %d 批 → 累计 %d" % (page + 1, len(seen)))
            w.flush()
    print("\n模型 %d 个，新增实体 %d / 边 %d" % (len(seen), w.new_ent, w.new_edge))
    c.close()


def cmd_report(a):
    c = connect()
    print("=" * 68)
    print("HuggingFace · 数据盘点")
    print("=" * 68)
    for t, n in c.execute("SELECT type,count(*) FROM entities WHERE platform='hf' "
                          "GROUP BY type ORDER BY 2 DESC"):
        print("  %-12s %d" % (t, n))
    print()
    for r, n in c.execute("SELECT rel,count(*) FROM edges WHERE src LIKE 'hf:%' "
                          "GROUP BY rel ORDER BY 2 DESC"):
        print("  %-16s %d" % (r, n))

    print("\n--- 派生最深的底座（被多少模型微调过）---")
    for r in c.execute(
            "SELECT e.title, count(*) ct FROM edges g JOIN entities e ON e.id=g.dst "
            "WHERE g.rel='derived_from' GROUP BY e.title ORDER BY ct DESC LIMIT 12"):
        print("  %-44s %d" % r)

    print("\n--- 论文 → GitHub 桥（跨平台边）---")
    n = c.execute("SELECT count(*) FROM edges WHERE rel='has_repo'").fetchone()[0]
    print("  共 %d 条" % n)
    for r in c.execute(
            "SELECT e.title, json_extract(e.payload,'$.github') FROM edges g "
            "JOIN entities e ON e.id=g.src WHERE g.rel='has_repo' LIMIT 8"):
        print("  %-40s %s" % (str(r[0])[:40], r[1]))

    print("\n--- 最常被引用的论文（被多少模型挂）---")
    for r in c.execute(
            "SELECT e.title, count(*) ct FROM edges g JOIN entities e ON e.id=g.dst "
            "WHERE g.rel='has_paper' GROUP BY e.title ORDER BY ct DESC LIMIT 10"):
        print("  %-52s %d" % (str(r[0])[:52], r[1]))
    c.close()


def main():
    ap = argparse.ArgumentParser(description="HuggingFace + 论文跨平台扩散器")
    ap.add_argument("--seed", action="append", default=[], help="模型 id，可多次")
    ap.add_argument("--tree", action="append", default=[], help="展开某底座的衍生树")
    ap.add_argument("--pages", type=int, default=1, help="衍生树翻几页")
    ap.add_argument("--paper", action="append", default=[], help="arXiv id，可多次")
    ap.add_argument("--top", type=int, default=0, help="按下载量铺前 N 个模型")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    if a.report:
        cmd_report(a)
    elif a.tree:
        cmd_tree(a)
    elif a.paper:
        cmd_paper(a)
    elif a.top:
        cmd_top(a)
    elif a.seed:
        cmd_seed(a)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
