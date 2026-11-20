# -*- coding: utf-8 -*-
"""关系圈：不靠地理定位，只靠「关联强度」把种子聚成圈。

核心思想（主人原话）：
    「哪怕没有关系圈，通过两个作品关联程度之类的，然后多种子的关联程度，
      基本就可以知道个大概了」

所以这里不猜「他在哪个城市」，只算一件事：
    两个作品 / 两个作者，共享了哪些**别人不共享**的实体。

为什么必须 IDF 加权：
    #随拍 这种话题几十万条作品都挂，共现它等于没信息。
    只有冷门话题、冷门原声，才是真实的纽带 —— 那才是「同一批人」。

输出三类东西：
    1. 作品-作品 强关联对（附「为什么连上」的证据）
    2. 作者-作者 共现图
    3. 关系圈（阈值过滤后的连通分量），带规模与代表实体

用法：
    python relation_circle.py                # 全量算一遍
    python relation_circle.py --min-sim 0.18 --top 20
    python relation_circle.py --seed-report  # 只看种子之间的重叠
"""
import argparse
import collections
import itertools
import math
import sqlite3
import time

DB = "_kb/seeds.db"

# 实体类型权重：作者共现 > 话题共现 > 音乐共现
# 理由：
#   同作者   = 硬关系（同一个人的作品）
#   同话题   = 主题信号，冷门话题只有真关注这个领域的人才会挂
#   同音乐   = 弱信号。热门 BGM 谁都能用，一首原声被几十人共用是常态，
#              单靠它连边会把「都用了同一首网红歌」误判成「同一批人」。
#              实测教训：1.000 的强关联几乎全是「共用一首创作原声」，
#              这种圈没有信息量，必须靠话题/多实体共同支撑。
TYPE_W = {"author": 3.0, "hashtag": 1.5, "music": 1.0, "work": 0.5,
          # GitHub：同一上游的分支血统，比作者还硬（同一份代码）
          "fork": 4.0,
          # 红果/豆瓣：演员。比作者弱一点（一部剧十几个演员），
          # 但比题材强 —— 题材是剧的属性，演员是人的身份，
          # 同一个人出现在两部剧里，误连概率远低于「都挂逆袭标签」
          "actor": 2.5}


def connect():
    c = sqlite3.connect(DB)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def load_graph(c, platform=None, only=None):
    """返回 (作品→实体集, 实体→类型, 实体→标题, 作品→作者)。

    platform: 只算某一个平台的子图（douyin / github / ...）。
        必须能分开算 —— GitHub 一个仓库带 5~20 个 topic，
        抖音一个作品带 0~5 个话题，混在一起的 IDF 分母是错的。
    """
    if platform:
        rows = c.execute("select src, dst, rel from edges where src like ?",
                         (platform + ":%",)).fetchall()
    else:
        rows = c.execute("select src, dst, rel from edges").fetchall()
    w2e = collections.defaultdict(set)
    e2type = {}
    e2title = {}
    for src, dst, rel in rows:
        if rel in ("by_author", "by_owner"):
            # src=作品/仓库, dst=作者/组织 → 归属
            w2e[src].add(dst)
            e2type[dst] = "author"
        elif rel in ("tagged", "has_topic"):
            w2e[src].add(dst)
            e2type[dst] = "hashtag"
        elif rel == "uses_music":
            w2e[src].add(dst)
            e2type[dst] = "music"
        elif rel == "acted_in":
            # 演员：src=剧, dst=人。跟作者同构，但一部剧有十几个。
            w2e[src].add(dst)
            e2type[dst] = "actor"
        elif rel == "fork_of":
            # fork 血统：同一上游的所有分支共享一个「家族」实体。
            # 这是 GitHub 上最强的同类信号 —— 同一个东西的不同代。
            fam = "family:" + dst.split(":", 1)[-1]
            w2e[src].add(fam)
            e2type[fam] = "fork"

    # only：只保留某一类实体参与相似度计算。
    # 红果必须用这个 —— 一部剧只有 4 个题材标签，共享 3 个就是 1.000，
    # 跟抖音「共用 BGM」同一个病：标签越稀疏，重叠系数越虚高。
    # 题材的正确用法是「目录下钻」（channel / rank / tag 三层），不是算相似度。
    if only:
        keep = set(only.split(","))
        for k in list(w2e):
            w2e[k] = set(e for e in w2e[k] if e2type.get(e) in keep)
            if not w2e[k]:
                del w2e[k]
    for eid, title in c.execute("select id, title from entities"):
        e2title[eid] = title
    return w2e, e2type, e2title


def prune_hub(w2e, max_ratio=1.0):
    """砍掉「枢纽实体」——被太大比例的仓库共享的标签。

    为什么必须有这一步（实测 2026-11-05，GitHub 子图 276 个仓库）：
        ai 被 104 个仓库共享，于是所有沾 AI 的作者互相都是 1.000，
        连出两个假圈：130 人的圈公约数就是「ai×80」。
        这跟抖音那边「一首网红 BGM 被几十人共用」是同一个病，
        只不过换了个马甲：那边靠 min-shared=2 挡住了，
        这边 ai 和 mcp 组合就能凑满 2 个，照样连。
    真正的同类不需要枢纽标签来连：
        firecrawl ↔ crawl4ai 靠的是 scraper/web-crawler/crawler 三个小标签。
    """
    if max_ratio >= 1.0:
        return w2e
    df = collections.Counter()
    for ents in w2e.values():
        for e in ents:
            df[e] += 1
    n = max(1, len(w2e))
    bad = {e for e, c in df.items() if c > n * max_ratio}
    if bad:
        print("[剪枝] 丢弃 %d 个枢纽实体（被 >%.0f%% 的仓库共享）：%s"
              % (len(bad), max_ratio * 100,
                 "、".join(sorted(bad)[:8])))
    for k in list(w2e):
        w2e[k] -= bad
        if not w2e[k]:
            del w2e[k]
    return w2e


def idf_weights(w2e, e2type):
    """实体稀缺度。df 越大 → 信息量越小。"""
    df = collections.Counter()
    for ents in w2e.values():
        for e in ents:
            df[e] += 1
    n = max(1, len(w2e))
    w = {}
    for e, d in df.items():
        w[e] = math.log((n + 1.0) / (d + 0.5)) * TYPE_W.get(e2type.get(e, ""), 1.0)
    return w, df


def sim(a, b, wt):
    """加权重叠系数（Overlap coefficient），不用余弦。

    为什么换：
        作者画像很稀疏（平均 4 个实体）。余弦的分子只算交集，分母却把
        自己一堆冷门实体的平方和全算进去，结果「共享 2 个真纽带」被稀释成
        0.069，看上去什么都没连上 —— 信号被分母吃掉了。
        重叠系数只问一句：**两个集合里较小那个，有多少被共享了**。
        共享 2 / 只有 4 个 → 0.5，这才是人眼看图时的直觉。
    """
    if not a or not b:
        return 0.0
    inter = a & b
    if not inter:
        return 0.0
    num = sum(wt.get(e, 1.0) for e in inter)
    base = min(sum(wt.get(e, 1.0) for e in a), sum(wt.get(e, 1.0) for e in b))
    if base <= 0:
        return 0.0
    return num / base


def explain(a, b, wt, e2title, top=4):
    """为什么这两个连上了 —— 只挑最稀缺的共享实体作证据。"""
    inter = sorted(a & b, key=lambda e: -wt.get(e, 1.0))[:top]
    return ["%s(%s)" % (e2title.get(e, e[:18]), ("%.1f" % wt.get(e, 1.0))) for e in inter]


def author_profiles(w2e, e2type):
    """作者画像 = 他名下所有作品关联到的实体并集（含话题与音乐）。"""
    a2e = collections.defaultdict(set)
    for work, ents in w2e.items():
        for e in ents:
            if e2type.get(e) == "author":
                for x in ents:
                    if e2type.get(x) != "author":
                        a2e[e].add(x)
    return a2e


def union_find(n):
    p = list(range(n))

    def find(x):
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            p[rb] = ra
            return True
        return False

    return find, union


def build_clusters(nodes, pairs, min_sim):
    """按相似度构图，取连通分量。返回 cluster 列表（每个是节点索引列表）。"""
    idx = {n: i for i, n in enumerate(nodes)}
    find, union = union_find(len(nodes))
    for (a, b), s in pairs:
        if s >= min_sim:
            union(idx[a], idx[b])
    groups = collections.defaultdict(list)
    for n in nodes:
        groups[find(idx[n])].append(n)
    return [sorted(v) for v in groups.values()], idx


def load_seedmap(c):
    """每条原始种子曾经带出过哪些作品（用于种子级重叠分析）。

    现 schema 没记「种子归属」，这里用保守近似：
    种子 URL 本身就是一个 work 实体，取它的直接邻居。
    """
    seeds = [r[0] for r in c.execute("select url from seeds")]
    return seeds


def expand(db, kw, hops=2, top=15):
    """目录式下钻：给一个关键词，沿关系边往外走。

    这就是「知道某类资源 → 顺着关系再走一层」的入口：
        关键词 → 命中的实体（话题/作者/音乐）
              → 挂着它的作品
              → 作品的其他实体（作者、别的冷门话题）
              → 再走出去一跳

    和记忆树一个道理：不靠全量扫描，靠「顺着目录往下翻」。
    """
    c = sqlite3.connect(db)
    hits = c.execute(
        "select id,type,title from entities where title like ? limit 8",
        ("%" + kw + "%",)).fetchall()
    if not hits:
        print("没有匹配到实体：%s" % kw)
        c.close()
        return
    print("=" * 68)
    print("下钻起点「%s」→ 命中 %d 个实体" % (kw, len(hits)))
    print("=" * 68)

    seen_work, seen_ent = set(), set()
    frontier = [h[0] for h in hits]
    for hop in range(1, hops + 1):
        works = set()
        for eid in frontier:
            for (w,) in c.execute(
                    "select src from edges where dst=? and rel!='by_author'", (eid,)):
                works.add(w)
            for (w,) in c.execute(
                    "select dst from edges where src=? and rel='by_author'", (eid,)):
                works.add(w)
        works -= seen_work
        if not works:
            print("\n第 %d 跳：断（没有新作品，说明这条边到头了）" % hop)
            break

        ents, nxt_authors = {}, set()
        for w in works:
            for eid, title, etype in c.execute(
                    "select e.id, e.title, e.type from edges g "
                    "join entities e on e.id=g.dst where g.src=?", (w,)):
                if eid not in seen_ent:
                    ents.setdefault(eid, (title, etype, 0))
                    ents[eid] = (title, etype, ents[eid][2] + 1)
                    if etype == "author":
                        nxt_authors.add(eid)

        print("\n第 %d 跳：新作品 %d 条 → 新实体 %d 个（作者 %d）"
              % (hop, len(works), len(ents), len(nxt_authors)))
        # 按「被几条作品同时挂」排序 —— 越多人共享，越是这个圈的核心
        ranked = sorted(ents.items(), key=lambda kv: -kv[1][2])
        by_type = collections.defaultdict(list)
        for eid, (title, etype, n) in ranked:
            by_type[etype].append((title, n))
        for etype in ("author", "hashtag", "music"):
            lst = by_type.get(etype) or []
            if not lst:
                continue
            label = {"author": "作者", "hashtag": "话题", "music": "音乐"}[etype]
            print("  %s(%d)：%s" % (label, len(lst),
                  "、".join("%s×%d" % (str(t)[:12], n) for t, n in lst[:top])))

        seen_work |= works
        seen_ent |= set(ents)
        frontier = list(ents)

    c.close()
    print("\n合计触达：作品 %d / 实体 %d" % (len(seen_work), len(seen_ent)))


def main():
    global DB
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-sim", type=float, default=0.45,
                    help="连边阈值（默认 0.20）")
    ap.add_argument("--top", type=int, default=15, help="展示前 N 组")
    ap.add_argument("--min-size", type=int, default=3, help="只显示规模≥N 的圈")
    ap.add_argument("--min-shared", type=int, default=2,
                    help="至少共享几个实体才算关联（默认 2；设 1 会混入「都用了同一首 BGM」的假圈）")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--expand", default="", help="给一个关键词，沿关系边下钻")
    ap.add_argument("--hops", type=int, default=2, help="下钻跳数")
    ap.add_argument("--max-df-ratio", type=float, default=1.0,
                    help="枢纽剪枝：实体被超过这个比例的仓库共享就丢弃。"
                         "GitHub 建议 0.25（实测 ai 被 104/276 共享，不剪就是假圈）")
    ap.add_argument("--only", default="",
                    help="只让某类实体参与相似度计算，逗号分隔：actor / hashtag / author / music")
    ap.add_argument("--platform", default="",
                    help="只算某个平台的子图：douyin / github。混算会让 IDF 分母失真")
    a = ap.parse_args()

    DB = a.db
    if a.expand:
        expand(DB, a.expand, hops=a.hops)
        return
    c = connect()
    w2e, e2type, e2title = load_graph(c, a.platform or None, a.only or None)
    c.close()
    w2e = prune_hub(w2e, a.max_df_ratio)

    if not w2e:
        print("图为空，先跑 seed_crawl.py")
        return

    wt, df = idf_weights(w2e, e2type)
    print("=" * 68)
    print("关系圈分析  |  作品 %d / 实体 %d / 边 %d"
          % (len(w2e), len(e2type), sum(len(v) for v in w2e.values())))
    print("=" * 68)

    # ---------- 1. 作者层共现 ----------
    a2e = author_profiles(w2e, e2type)
    authors = sorted(a2e)
    print("\n作者画像 %d 个（平均关联 %.1f 个话题/音乐）"
          % (len(authors), sum(len(v) for v in a2e.values()) / max(1, len(authors))))

    apairs = []
    for x, y in itertools.combinations(authors, 2):
        inter = a2e[x] & a2e[y]
        # 🔴 只靠一个实体连起来的是弱关联，不算关系。
        #    实测：不加这道闸，冒出 17 对 1.000 的「强关联」，全是共用同一首
        #    创作原声 —— 那只能说明都用了同一首网红 BGM，不是同一批人。
        if len(inter) < a.min_shared:
            continue
        s = sim(a2e[x], a2e[y], wt)
        if s > 0.02:
            apairs.append(((x, y), s))
    apairs.sort(key=lambda t: -t[1])
    print("作者-作者 有重叠的对数 %d（阈值以上 %d）"
          % (len(apairs), sum(1 for _, s in apairs if s >= a.min_sim)))
    # 强度分布：阈值不能拍脑袋，得看真实分布的形状
    if apairs:
        buckets = collections.Counter()
        for _, s in apairs:
            b = ("0.9-1.0" if s >= 0.9 else "0.7-0.9" if s >= 0.7 else
                 "0.5-0.7" if s >= 0.5 else "0.3-0.5" if s >= 0.3 else
                 "0.15-0.3" if s >= 0.15 else "<0.15")
            buckets[b] += 1
        order = ["0.9-1.0", "0.7-0.9", "0.5-0.7", "0.3-0.5", "0.15-0.3", "<0.15"]
        print("  强度分布: " + "  ".join("%s:%d" % (k, buckets[k]) for k in order if buckets[k]))

    print("\n--- 作者关联 TOP%d ---" % a.top)
    for (x, y), s in apairs[:a.top]:
        ev = explain(a2e[x], a2e[y], wt, e2title)
        print("  %.3f  %-16s ↔ %-16s  %s"
              % (s, str(e2title.get(x, x))[:16], str(e2title.get(y, y))[:16],
                 " / ".join(ev)))

    # ---------- 2. 作者关系圈 ----------
    clusters, _ = build_clusters(authors, apairs, a.min_sim)
    clusters.sort(key=len, reverse=True)
    big = [cl for cl in clusters if len(cl) >= a.min_size]
    print("\n--- 作者关系圈：%d 个圈 / 最大 %d 人 ---"
          % (len(big), len(big[0]) if big else 0))
    for i, cl in enumerate(big[:6], 1):
        # 圈内代表性实体：出现频次最高的共享话题
        cnt = collections.Counter()
        for au in cl:
            for e in a2e[au]:
                cnt[e] += 1
        common = [(e2title.get(e, e), n) for e, n in cnt.most_common(30)
                  if n >= max(2, len(cl) * 0.5)][:5]
        print("  圈#%d  %d 人" % (i, len(cl)))
        print("       成员: %s" % ("、".join(str(e2title.get(x, x))[:10] for x in cl[:8])))
        if common:
            print("       公约数: %s" % (" / ".join("%s×%d" % (t[:14], n) for t, n in common)))
        else:
            print("       公约数: 无（靠链式相似连通）")

    # ---------- 3. 作品层强关联 ----------
    # 必须把「同源」和「真关联」分开：
    #   共享 author 实体 = 同一个人的两条作品，这是同源，不是关系圈。
    #   只有共享话题/音乐、但不共享作者，才是跨主体的真实纽带。
    works = sorted(w2e)
    same_owner, cross = [], []
    for x, y in itertools.combinations(works, 2):
        s = sim(w2e[x], w2e[y], wt)
        if s < a.min_sim:
            continue
        shared_auth = [e for e in (w2e[x] & w2e[y]) if e2type.get(e) == "author"]
        (same_owner if shared_auth else cross).append(((x, y), s))
    same_owner.sort(key=lambda t: -t[1])
    cross.sort(key=lambda t: -t[1])

    print("\n--- 作品关联：跨主体 %d 对 / 同源 %d 对（≥%.2f）---"
          % (len(cross), len(same_owner), a.min_sim))
    print("  【跨主体】共享话题但不同作者 —— 这才是关系圈的边")
    for (x, y), s in cross[:8]:
        ev = explain(w2e[x], w2e[y], wt, e2title, top=3)
        print("    %.3f  %s ↔ %s" % (s, str(e2title.get(x, x))[:22], str(e2title.get(y, y))[:22]))
        print("           共享: %s" % " / ".join(ev))
    print("  【同源】同一作者的另一条作品 —— 只说明是同一人，不算关系")
    for (x, y), s in same_owner[:4]:
        ev = explain(w2e[x], w2e[y], wt, e2title, top=2)
        print("    %.3f  %s ↔ %s  共享: %s"
              % (s, x[-18:], y[-18:], " / ".join(ev)))

    # ---------- 4. 稀缺实体（真实纽带的候选） ----------
    rare = [(e, d) for e, d in df.items() if d >= 3]
    rare.sort(key=lambda t: t[1])
    print("\n--- 最稀缺共享实体（纽带候选）---")
    for e, d in rare[:10]:
        print("  %-30s 出现 %d 次  [%s]"
              % (str(e2title.get(e, e))[:30], d, e2type.get(e, "?")))


if __name__ == "__main__":
    main()
