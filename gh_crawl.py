# -*- coding: utf-8 -*-
"""GitHub 关系扩散器：不靠推荐页，靠边。

主人的判断（2026-11-05）：
    「GitHub 那个推荐页太垃圾了，所以说必须建立那个扩散关系还有那个关系图之类
      的，这样的话咱们查找相关资源的话才能有方向，要不然一拉全部，哎呀有没有用
      不知道，真的是，是不是类似的不知道」

推荐页为什么没用：它是黑盒算法，给的是"全网热"；我们要的是"跟我这一坨有关"。
所以这里只做一件事 —— 把 GitHub 上**客观存在的强关系**建成边，然后沿边走：

    fork_of    repo → repo    同一份代码的分支（几乎等于同一个东西）   权 4.0
    listed_in  repo → repo    被同一份 awesome 清单收录（专家标注）   权 3.5
    by_owner   repo → owner   同一个作者的其他作品                    权 2.5
    has_topic  repo → topic   共享官方标签（维护者自己打的，非算法）  权 2.0
    depends_on repo → pkg     依赖同一个包（技术栈锁定）              权 1.0

为什么这些边可信而推荐页不可信：
    推荐页 = 平台算给你看（果）；这些边 = 作者自己的行为留下的痕迹（因）。
    一个人 fork 了什么、打了什么标签、依赖了什么包、把项目收进了哪份清单，
    这些都是他**已经做过的事**，不是猜的。

配额盘（实测 2026-11-05）：
    core    5000/h   —— forks / owner repos / readme
    search    30/min —— topic 枚举，一次 100 条（实测 topic:llm stars:>500 = 2197 个）
    graphql 5000 点/h —— 批量拿元数据，一次请求可查 20 个仓库

铁律：只读。gh api 的 -f 会变 POST（POST /repos/{o}/{r}/forks = 真的建 fork），
     本文件所有读取一律 gh_get()，参数拼查询串。

用法：
    python gh_crawl.py --mine                    # 从主人自己的仓库出发
    python gh_crawl.py --seed owner/repo --depth 2
    python gh_crawl.py --topic llm --limit 200
    python gh_crawl.py --report
"""
import argparse
import collections
import json
import math
import random
import re
import sqlite3
import subprocess
import headless
import sys
import time

DB = "_kb/seeds.db"

# ---------- 边权：谁更说明"是一伙的" ----------
REL_W = {
    "fork_of": 4.0,      # 同源代码，最强
    "listed_in": 3.5,    # 同一份人工清单收录
    "by_owner": 2.5,     # 同作者
    "has_topic": 2.0,    # 同官方标签
    "depends_on": 1.0,   # 同依赖（太泛，只能当辅助）
}

# ---------- 节流 ----------
_last_search = [0.0]
SEARCH_GAP = 2.2          # search 30/min → 至少 2.0s
_last_call = [0.0]
CORE_GAP = 0.12


def _run(args, tmo=60):
    """通用 gh 调用。返回 (ok, data, 秒)。文本返回时 data 是 str。"""
    t0 = time.time()
    try:
        p = headless.run(["gh"] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=tmo, shell=False)
    except subprocess.TimeoutExpired:
        return False, "timeout", time.time() - t0
    dt = time.time() - t0
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or "").strip()[:300], dt
    out = p.stdout.strip()
    try:
        return True, json.loads(out), dt
    except Exception:
        return True, out, dt


def gh_get(path, tmo=60):
    """只读 GET。参数必须写在 path 的查询串里。"""
    gap = time.time() - _last_call[0]
    if gap < CORE_GAP:
        time.sleep(CORE_GAP - gap)
    _last_call[0] = time.time()
    return _run(["api", "-X", "GET", path], tmo)


def gh_search(path, tmo=60):
    """搜索接口单独节流（30/min）。"""
    gap = time.time() - _last_search[0]
    if gap < SEARCH_GAP:
        time.sleep(SEARCH_GAP - gap)
    _last_search[0] = time.time()
    return _run(["api", "-X", "GET", path], tmo)


def gh_graphql(q, tmo=90):
    gap = time.time() - _last_call[0]
    if gap < CORE_GAP:
        time.sleep(CORE_GAP - gap)
    _last_call[0] = time.time()
    return _run(["api", "graphql", "-f", "query=" + q], tmo)


# ---------- 库 ----------
def connect():
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=20000")
    return c


def ensure(c):
    c.executescript("""
        CREATE TABLE IF NOT EXISTS entities(
            id TEXT PRIMARY KEY, platform TEXT, type TEXT, title TEXT,
            url TEXT, depth TEXT, payload TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS edges(
            src TEXT, dst TEXT, rel TEXT, ts REAL,
            PRIMARY KEY(src, dst, rel));
        CREATE INDEX IF NOT EXISTS ix_ent_type ON entities(platform, type);
        CREATE INDEX IF NOT EXISTS ix_edges_rel ON edges(rel);
        -- 涨星榜的底账。GitHub 没有公开的「涨星」接口，任何第三方榜都是推算的。
        -- 唯一可靠的办法是自己按天存快照，两次相减就是真实增量。
        CREATE TABLE IF NOT EXISTS star_snap(
            full_name TEXT, day TEXT, stars INTEGER, ts REAL,
            PRIMARY KEY(full_name, day));
    """)
    c.commit()


def rid(full_name):
    return "github:repo:%s" % full_name.lower()


def oid(login):
    return "github:owner:%s" % login.lower()


def tid(name):
    return "github:topic:%s" % name.lower()


# ---------- 写 ----------
class Writer:
    def __init__(self, c):
        self.c = c
        self.new_ent = 0
        self.new_edge = 0
        self.buf_e = []
        self.buf_g = []

    def repo(self, full_name, meta=None, depth=0):
        m = meta or {}
        eid = rid(full_name)
        cur = self.c.execute(
            "INSERT OR IGNORE INTO entities(id,platform,type,title,url,depth,payload,ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (eid, "github", "repo", (m.get("desc") or full_name)[:300],
             "https://github.com/%s" % full_name, str(int(depth)),
             json.dumps({k: m.get(k) for k in
                         ("stars", "forks", "lang", "pushed", "archived", "topics", "license")
                         if m.get(k) is not None}, ensure_ascii=False)[:4000],
             time.time()))
        self.new_ent += cur.rowcount
        return eid

    def owner(self, login, kind="User", depth=0):
        cur = self.c.execute(
            "INSERT OR IGNORE INTO entities(id,platform,type,title,url,depth,payload,ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (oid(login), "github", "owner", login,
             "https://github.com/%s" % login, str(int(depth)),
             json.dumps({"kind": kind}, ensure_ascii=False), time.time()))
        self.new_ent += cur.rowcount
        return oid(login)

    def topic(self, name, depth=0, size=None):
        cur = self.c.execute(
            "INSERT OR IGNORE INTO entities(id,platform,type,title,url,depth,payload,ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (tid(name), "github", "topic", name,
             "https://github.com/topics/%s" % name, str(int(depth)),
            "{}", time.time()))
        self.new_ent += cur.rowcount
        # size = 这个 topic 底下有多少仓库（来自 search 的 total_count）。
        # 它就是免费的 IDF 分母：ai 底下几十万，flourishing 底下几十个。
        # 扩「ai」等于扩全网热门，扩「flourishing」才是扩同类。
        if size is not None:
            self.c.execute(
                "UPDATE entities SET payload=? WHERE id=? AND (payload IS NULL OR payload='{}')",
                (json.dumps({"size": int(size)}), tid(name)))
        return tid(name)

    def edge(self, s, d, rel):
        cur = self.c.execute(
            "INSERT OR IGNORE INTO edges(src,dst,rel,ts) VALUES(?,?,?,?)",
            (s, d, rel, time.time()))
        self.new_edge += cur.rowcount

    def flush(self):
        self.c.commit()


# ---------- 解析 ----------
def norm_repo(node):
    """把 REST 或 GraphQL 的仓库对象统一成扁平 dict。"""
    if "nameWithOwner" in node:  # GraphQL
        return {
            "full_name": node["nameWithOwner"],
            "desc": (node.get("description") or "")[:280],
            "stars": node.get("stargazerCount") or 0,
            "forks": node.get("forkCount") or 0,
            "lang": ((node.get("primaryLanguage") or {}) or {}).get("name"),
            "pushed": node.get("pushedAt"),
            "archived": node.get("isArchived"),
            "topics": [x["topic"]["name"] for x in
                       (node.get("repositoryTopics") or {}).get("nodes", [])],
            "owner": ((node.get("owner") or {}) or {}).get("login"),
            "owner_kind": ((node.get("owner") or {}) or {}).get("__typename"),
            "parent": ((node.get("parent") or {}) or {}).get("nameWithOwner"),
            "license": ((node.get("licenseInfo") or {}) or {}).get("spdxId"),
        }
    return {
        "full_name": node.get("full_name"),
        "desc": (node.get("description") or "")[:280],
        "stars": node.get("stargazers_count") or 0,
        "forks": node.get("forks_count") or 0,
        "lang": node.get("language"),
        "pushed": node.get("pushed_at"),
        "archived": node.get("archived"),
        "topics": node.get("topics") or [],
        "owner": ((node.get("owner") or {}) or {}).get("login"),
        "owner_kind": ((node.get("owner") or {}) or {}).get("type"),
        "parent": ((node.get("parent") or {}) or {}).get("full_name"),
        "license": ((node.get("license") or {}) or {}).get("spdx_id"),
    }


def absorb(w, full_name, meta, depth, size=None, via=None):
    """把一个仓库连同它的 topic/owner/parent 边一起落库。

    size/via：只在「这次扩散的起因 topic」上回填 total_count。
    search 回来的一整批仓库都带 via 这个标签，所以一个 total_count 能回填整批，零额外请求。
    """
    eid = w.repo(full_name, meta, depth)
    if meta.get("owner"):
        o = w.owner(meta["owner"], meta.get("owner_kind") or "User", depth)
        w.edge(eid, o, "by_owner")
    for t in meta.get("topics") or []:
        t_size = size if (size is not None and via and t.lower() == via.lower()) else None
        w.edge(eid, w.topic(t, depth, t_size), "has_topic")
    if meta.get("parent"):
        pe = w.repo(meta["parent"], {}, depth)
        w.edge(eid, pe, "fork_of")
    return eid


# ---------- 单仓库详情 ----------
GQL_REPO = """query{repository(owner:"%s",name:"%s"){
  nameWithOwner description stargazerCount forkCount isArchived pushedAt
  primaryLanguage{name}
  repositoryTopics(first:25){nodes{topic{name}}}
  parent{nameWithOwner}
  owner{login __typename}
  licenseInfo{spdxId}
}}"""


def fetch_repo(full_name):
    o, r = full_name.split("/", 1)
    ok, d, dt = gh_graphql(GQL_REPO % (o, r))
    if not ok:
        return None, d
    rp = (d.get("data") or {}).get("repository")
    if not rp:
        return None, "empty"
    return norm_repo(rp), dt


# ---------- 扩散三条边 ----------
def expand_owner(w, login, depth, cap=100):
    """同作者其他仓库。一次请求最多 100 个。"""
    got = []
    ok, d, dt = gh_get("users/%s/repos?per_page=%d&sort=pushed" % (login, cap))
    if not ok or not isinstance(d, list):
        return got, dt
    for node in d:
        if node.get("fork"):
            continue
        m = norm_repo(node)
        absorb(w, m["full_name"], m, depth)
        got.append(m)
    return got, dt


def expand_forks(w, full_name, depth, cap=50):
    """fork 亲缘：fork 出去还活着的，说明真有人在这上面干活。"""
    got = []
    ok, d, dt = gh_get("repos/%s/forks?per_page=%d&sort=stargazers" % (full_name, cap))
    if not ok or not isinstance(d, list):
        return got, dt
    for node in d:
        m = norm_repo(node)
        m["parent"] = full_name
        absorb(w, m["full_name"], m, depth)
        got.append(m)
    return got, dt


TOPIC_SIZE = {}       # topic → total_count，进程内缓存


def search_topic(topic, min_stars=50, cap=100):
    """只查不落库。返回 (items, total, sec)。

    为什么先查再决定要不要：total_count 是这个标签的"体积"。
    实测（2026-11-05，fabric 的 5 个标签）：
        flourishing   1 个仓库   → 指纹级，指到 100% 同类
        work         35 个       → 精准
        life         50 个       → 精准
        augmentation 64 个       → 精准
        ai         9484 个       → 前排是 ★430k 的 rea、★393k 的 openclaw，纯流量
    所以体积超过阈值的标签，一次都不该扩 —— 扩它就是主人说的"一拉全部"。
    先探规模、再决定扩不扩，代价只是每个大标签浪费 1 次请求。
    """
    import urllib.parse
    q = "topic:%s stars:>=%d" % (topic, min_stars)
    ok, d, dt = gh_search("search/repositories?q=%s&per_page=%d&sort=stars&order=desc"
                          % (urllib.parse.quote(q), cap))
    if not ok or not isinstance(d, dict):
        return [], None, dt
    total = d.get("total_count")
    if total is not None:
        TOPIC_SIZE[topic.lower()] = total
    return [norm_repo(n) for n in d.get("items") or []], total, dt


# ---------- 相关度打分 ----------
def score(seed_topics, seed_lang, cand, seed_stars=0):
    """候选跟种子有多像。只用于排序和门槛，不做硬性淘汰。

    为什么不用「重叠系数」了（和老关系圈不同）：
        抖音那边一个作品最多挂 5 个话题，重叠系数够用。
        GitHub 这边一个仓库能挂 20 个 topic，分母一大，单靠比例会把
        「共享 2 个真标签」压到 0.1，反而被「共享 1 个 ai」的 0.05 差不多。
        所以这里用「绝对个数为主 + 比例为辅」：
            共享 1 个  → 0.7 + 0.2 = 0.9   （大概率是 ai/llm 这类大标签，噪声）
            共享 2 个  → 1.4 + 0.4 = 1.8   （开始有方向感）
            共享 3 个  → 2.1 + 0.6 = 2.7   （基本就是一伙的）
    """
    ct = set(t.lower() for t in (cand.get("topics") or []))
    st = set(t.lower() for t in seed_topics)
    inter = len(ct & st)
    s = min(inter, 3) * 0.7 + (inter / max(1, len(st))) * 1.0
    if seed_lang and cand.get("lang") == seed_lang:
        s += 0.4
    if seed_stars and cand.get("stars"):
        ratio = math.log10(max(1, cand["stars"])) / max(1.0, math.log10(max(1, seed_stars)))
        s += 0.3 * (1.0 - min(1.0, abs(1.0 - ratio)))
    return round(s, 3)


def order_topics(topics):
    """按标签规模从小到大排 —— 先扩小众，后扩大众。

    这是整个文件最重要的一行判断：
        从 fabric 出发扩 topic:ai（十万级），捞回来的是全网最热的 AI 项目，
        跟 fabric 没关系 —— 那就是主人说的「一拉全部，有没有用不知道」。
        而 fabric 自己的 flourishing / augmentation 只有几十个仓库，
        扩这些才是「同类」。所以小众标签优先，大众标签连碰都少碰。
    """
    def k(t):
        s = TOPIC_SIZE.get(t.lower())
        return (1, s) if s is None else (0, s)   # 未知规模的排后面
    return sorted(topics, key=k)


def queue_seeds(c, limit=200, mark=True):
    """从共享队列里取 platform='github' 的种子，转成 owner/repo。

    这些种子大多来自 B 站简介（seed_crawl.py 的 Bilibili 适配器写进来的），
    特点是 ★个位数、没有 topic、走任何搜索都找不到 —— 只能从人嘴里捡。
    """
    rows = c.execute(
        "SELECT url FROM seeds WHERE platform='github' AND state='new' "
        "ORDER BY prio ASC, ts ASC LIMIT ?", (limit,)).fetchall()
    out = []
    for (u,) in rows:
        m = re.match(r"https?://(?:www\.)?github\.com/([^/?#]+)/([^/?#]+)", u or "")
        if not m:
            continue
        fn = "%s/%s" % (m.group(1), m.group(2))
        if fn.lower().endswith(".git"):
            fn = fn[:-4]
        if fn not in out:
            out.append(fn)
        if mark:
            # 标成跑过，避免下次重复取（重复拉同一个仓库只会 INSERT OR IGNORE，白花配额）
            c.execute("UPDATE seeds SET state='run' WHERE url=?", (u,))
    if mark:
        c.commit()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="append", default=[], help="owner/repo，可多次")
    ap.add_argument("--from-queue", action="store_true",
                    help="从共享队列取 github 种子（B 站简介挖来的低星项目都在这）")
    ap.add_argument("--mine", action="store_true", help="用主人自己的仓库当种子")
    ap.add_argument("--topic", action="append", default=[], help="直接按 topic 枚举")
    ap.add_argument("--depth", type=int, default=1)
    ap.add_argument("--rounds", type=int, default=1, help="扩散轮数")
    ap.add_argument("--limit", type=int, default=120, help="每轮最多处理多少个仓库")
    ap.add_argument("--min-stars", type=int, default=50)
    ap.add_argument("--profile", action="store_true", help="只看我的兴趣指纹，不扩散")
    ap.add_argument("--trend", action="store_true", help="出新冒尖榜 + 真实涨星榜")
    ap.add_argument("--tags", default="", help="按指定标签出榜，逗号分隔")
    ap.add_argument("--from-profile", action="store_true", help="用我的收藏算出的指纹标签出榜")
    ap.add_argument("--top-tags", type=int, default=6, help="指纹取前几个标签")
    ap.add_argument("--snap", action="store_true", help="把当前 star 数存档（涨星榜底账）")
    ap.add_argument("--days", type=int, default=7, help="涨星/新榜的时间窗口")
    ap.add_argument("--min-score", type=float, default=1.5,
                    help="候选入队门槛。1.5 ≈ 至少共享 2 个标签，或 1 个标签+同语言同量级")
    ap.add_argument("--topics-per-repo", type=int, default=3, help="每个仓库最多扩几个 topic")
    ap.add_argument("--topic-max-size", type=int, default=3000,
                    help="超过这个规模的标签不扩。实测 ai=9484 / llm=8069 全是流量怪物，"
                         "fabric 自己的标签只有 1~64 个")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    c = connect()
    ensure(c)

    if a.report:
        report(c)
        return

    if a.snap and not a.trend:
        snap(c)
        report(c)
        return

    if a.trend:
        # 出榜前先存档：榜二要的是「今天 vs 上次」，顺手把今天这一天记下来，
        # 下次跑就自动有增量了。不存就永远只有基线。
        snap(c)
        tags = [t.strip() for t in (a.tags or "").split(",") if t.strip()]
        if a.from_profile and not tags:
            # 从我的收藏里现算指纹，取出现 ≥2 次的前 N 个 —— 那才是"我的方向"
            ok, s2, dt = gh_get("user/starred?per_page=100&sort=created")
            fp = collections.Counter()
            for x in (s2 if (ok and isinstance(s2, list)) else []):
                for t in x.get("topics") or []:
                    fp[t.lower()] += 1
            tags = [t for t, n in fp.most_common(a.top_tags) if n >= 2]
            print("[指纹] 取前 %d 个方向标签：%s" % (len(tags), " ".join(tags)))
        if tags:
            trend_tags(c, tags, a.days, a.min_stars, 5)
            return
        trend(c, a.days, a.min_stars, a.limit)
        return

    w = Writer(c)
    seeds = list(a.seed)

    if a.from_queue:
        qs = queue_seeds(c, a.limit * 2)
        print("[queue] 队列里 %d 个 github 种子（多来自 B 站简介，★普遍很低）" % len(qs))
        seeds = list(dict.fromkeys(qs + seeds))
        if not seeds:
            print("[queue] 队列是空的。先跑 seed_crawl.py --bili-kw \"关键词\" 去挖。")
            return

    if a.mine or a.profile or not seeds:
        # 我的仓库
        ok, d, dt = gh_get("users/ghshhf/repos?per_page=100&sort=pushed")
        mine = []
        if ok and isinstance(d, list):
            mine = [x["full_name"] for x in d]
        # 我的收藏 —— 主人原话：「我自己的仓库和那个我自己的收藏，本质上就是最好的关联扩展点」
        # 为什么 star 比自己的仓库信号更强：自己的仓库大多没打 topic（29 个里只有 2 个有），
        # 而 star 是亲手投的票，被 star 的仓库带着别人打好的 topic。
        ok2, s2, dt2 = gh_get("user/starred?per_page=100&sort=created")
        starred = s2 if (ok2 and isinstance(s2, list)) else []
        print("[mine] 我的仓库 %d（fork %d）｜我的收藏 %d"
              % (len(mine), sum(1 for x in d if x.get("fork")) if ok else 0, len(starred)))

        # 兴趣指纹：我收藏的仓库里，哪些标签反复出现。
        # 出现 2 次以上的才是「我的方向」，出现 1 次的可能只是顺手 star。
        fp = collections.Counter()
        for x in starred:
            for t in x.get("topics") or []:
                fp[t.lower()] += 1
        if fp:
            print("[指纹] 我收藏里的标签频次（≥2 才算方向）:")
            for t, n in fp.most_common(18):
                if n >= 2:
                    print("    %-24s ×%d" % (t, n))
        if a.profile:
            return

        seeds = list(dict.fromkeys(seeds + [x["full_name"] for x in starred] + mine))

    # 直接 topic 枚举
    if a.topic:
        for t in a.topic:
            items, total, dt = search_topic(t, a.min_stars, a.limit)
            for m in items:
                absorb(w, m["full_name"], m, 0, size=total, via=t)
            print("[topic] %-20s 拿 %3d / 共 %s  (%.1fs)" % (t, len(items), total, dt))
        w.flush()
        print("新增实体 %d / 边 %d" % (w.new_ent, w.new_edge))
        report(c)
        return

    print("种子 %d 个，深度 %d，每轮上限 %d" % (len(seeds), a.depth, a.limit))

    # 第一层：种子仓库本身
    queue = []          # (full_name, depth, score)
    seen_seed = set()
    for s in seeds[:a.limit]:
        meta, dt = fetch_repo(s)
        if not meta:
            print("  ! %-40s %s" % (s, dt))
            continue
        absorb(w, meta["full_name"], meta, 0)
        seen_seed.add(meta["full_name"].lower())
        queue.append((meta["full_name"], 1, 9.9, meta))
        print("  + %-42s ★%-7d topics=%s"
              % (meta["full_name"], meta["stars"], ",".join(meta["topics"][:5]) or "-"))
    w.flush()
    print("第一层完成：实体 +%d 边 +%d" % (w.new_ent, w.new_edge))

    # 后续层：沿三条边扩散
    done = set()
    for rnd in range(1, a.rounds + 1):
        if not queue:
            break
        batch = queue[:a.limit]
        queue = queue[a.limit:]
        print("\n=== 第 %d 轮：处理 %d 个仓库 ===" % (rnd, len(batch)))
        cands = collections.defaultdict(lambda: [0.0, None])
        for i, (fn, dep, sc, meta) in enumerate(batch, 1):
            if fn.lower() in done:
                continue
            done.add(fn.lower())
            topics = meta.get("topics") or []
            lang = meta.get("lang")
            stars = meta.get("stars") or 0
            owner = meta.get("owner")
            n0e, n0g = w.new_ent, w.new_edge
            tused = []

            # 边 1：同作者（最省，1 次请求最多 100 个）
            if owner:
                got, dt = expand_owner(w, owner, dep)
                for m in got:
                    ks = score(topics, lang, m, stars)
                    k = m["full_name"].lower()
                    if ks > cands[k][0]:
                        cands[k] = [ks, m]

            # 边 2：同 topic —— 先探规模，只有小标签才扩
            n_expanded = 0
            for t in order_topics(topics):
                if n_expanded >= a.topics_per_repo:
                    break
                sz_cached = TOPIC_SIZE.get(t.lower())
                if sz_cached is not None and sz_cached > a.topic_max_size:
                    tused.append("%s(%d)✗" % (t, sz_cached))
                    continue
                items, total, dt = search_topic(t, a.min_stars, 100)
                if total is None:
                    continue
                if total > a.topic_max_size:
                    # 大众标签：样本一律不落库。它给的是全网热门，不是同类。
                    tused.append("%s(%d)✗" % (t, total))
                    continue
                for m in items:
                    absorb(w, m["full_name"], m, dep, size=total, via=t)
                    ks = score(topics, lang, m, stars)
                    k = m["full_name"].lower()
                    if ks > cands[k][0]:
                        cands[k] = [ks, m]
                n_expanded += 1
                tused.append("%s(%d)" % (t, total))

            print("  [%3d/%3d] %-38s +实体%-4d +边%-4d 候选%-4d  %s"
                  % (i, len(batch), fn[:38], w.new_ent - n0e, w.new_edge - n0g, len(cands),
                     " ".join(tused)[:40]))
            if i % 10 == 0:
                w.flush()

        w.flush()
        # 候选排序：相关度优先，同分看 star
        # 注意：defaultdict 的默认值是 [0.0, None]，分数没超过 0 的会留下 None，
        # 必须先剔掉再排序，否则 v[1]["full_name"] 直接 TypeError（实测踩过）。
        ranked = sorted(
            [(k, v) for k, v in cands.items() if v[1]],
            key=lambda kv: (-kv[1][0], -((kv[1][1] or {}).get("stars") or 0)))
        passed = [(k, v) for k, v in ranked if v[0] >= a.min_score]
        newq = [(v[1]["full_name"], dep + 1, k, v[1]) for k, v in passed
                if k not in done][:a.limit * 2]
        queue.extend(newq)
        print("  候选 %d → 过门槛 %d（>=%.1f）→ 入队 %d"
              % (len(ranked), len(passed), a.min_score, len(newq)))
        # 把过门槛的摊开看：这是"方向"本身，不看不放心
        for k, v in passed[:6]:
            m = v[1]
            print("     %.2f  %-40s ★%-7d %s"
                  % (v[0], m["full_name"][:40], m.get("stars") or 0,
                     ",".join((m.get("topics") or [])[:5])))

    w.flush()
    print("\n新增实体 %d / 边 %d" % (w.new_ent, w.new_edge))
    report(c)


def snap(c):
    """把当前 star 数按天存档。涨星榜的唯一底账。"""
    day = time.strftime("%Y-%m-%d")
    n = 0
    rows = c.execute(
        "select id, payload from entities where platform='github' and type='repo'").fetchall()
    for eid, pay in rows:
        try:
            p = json.loads(pay or "{}")
        except Exception:
            continue
        st = p.get("stars")
        if st is None:
            continue
        cur = c.execute(
            "INSERT OR REPLACE INTO star_snap(full_name,day,stars,ts) VALUES(?,?,?,?)",
            (eid.split("github:repo:", 1)[-1], day, int(st), time.time()))
        n += 1
    c.commit()
    days = c.execute("select count(distinct day) from star_snap").fetchone()[0]
    print("[snap] %s 存档 %d 个仓库，累计 %d 天快照" % (day, n, days))
    return days


GQL_ACTIVE = """query{%s}"""
GQL_NODE = """r%d: repository(owner:"%s",name:"%s"){
  nameWithOwner description stargazerCount forkCount pushedAt createdAt
  primaryLanguage{name}
  repositoryTopics(first:8){nodes{topic{name}}}
  openIssues: issues(states:OPEN){totalCount}
  defaultBranchRef{target{... on Commit{history(first:1,since:"%s"){totalCount}}}}
}"""


def commits_since(repos, since, batch=12):
    """批量查「窗口内提交数」—— 这才是"真在更新"的硬指标。

    为什么不用 star：刷星工厂能让上万个账号点 star，但没法让代码天天有新 commit。
    实测反例（2026-11-05）：
        deepseek-ai/dsh-superagent-prototype  ★18464  0 个 issue  ← 星是刷的
        storytold/craft-anything              ★11063  186 个 issue ← 真的有人在用
    fork 率分不开这两类（unslop 5.0% vs 真项目 firecrawl 5.3%），
    但「窗口内 commit 数」和「open issue 数」能。
    """
    out = []
    for i in range(0, len(repos), batch):
        chunk = repos[i:i + batch]
        parts = []
        for j, (o, r) in enumerate(chunk):
            parts.append(GQL_NODE % (j, o, r, since))
        ok, d, dt = gh_graphql(GQL_ACTIVE % " ".join(parts))
        if not ok:
            continue
        got = (d.get("data") or {})
        for j in range(len(chunk)):
            node = got.get("r%d" % j)
            if not node:
                continue
            m = norm_repo(node)
            try:
                m["commits"] = node["defaultBranchRef"]["target"]["history"]["totalCount"]
            except Exception:
                m["commits"] = 0
            m["issues"] = ((node.get("openIssues") or {}).get("totalCount")) or 0
            m["created"] = node.get("createdAt")
            out.append(m)
    return out


def trend_tags(c, tags, days=7, min_stars=20, per=5):
    """按「我的标签」逐一出榜 —— 这才是主人要的"有方向"。

    全网新榜 = 第二个推荐页（榜一实测就被 ★18464 零 issue 的仓库占了位置）。
    只有按我自己的指纹标签切开，每个标签底下那几行才真的跟我有关。

    每个标签两问：
        新的：topic:X created>=窗口      —— 我这条线上最近冒出来的
        活的：topic:X pushed>=窗口       —— 我这条线上现在还在长的
    两个都用窗口内 commit 数排，不看 star。
    """
    import datetime
    import urllib.parse

    today = datetime.date.today()
    s_created = (today - datetime.timedelta(days=days)).isoformat()
    s_push = (today - datetime.timedelta(days=max(2, days // 2))).isoformat()

    for t in tags:
        print("\n" + "-" * 74)
        print("◆ %s" % t)
        print("-" * 74)
        for label, qq, since in (
                ("新建", "topic:%s created:>=%s stars:>=%d" % (t, s_created, min_stars), s_created),
                ("在长", "topic:%s pushed:>=%s stars:>=%d" % (t, s_push, min_stars), s_push)):
            ok, d, dt = gh_search("search/repositories?q=%s&per_page=40&sort=stars&order=desc"
                                  % urllib.parse.quote(qq))
            if not ok or not isinstance(d, dict):
                print("  [%s] 查询失败 %s" % (label, str(d)[:60]))
                continue
            items = [norm_repo(x) for x in (d.get("items") or [])]
            total = d.get("total_count")
            if not items:
                print("  [%s] 0 个" % label)
                continue
            ms = commits_since([tuple(m["full_name"].split("/", 1)) for m in items],
                               since + "T00:00:00Z")
            ms = [m for m in ms if m.get("commits", 0) > 0]
            ms.sort(key=lambda m: (-m["commits"], -m["stars"]))
            print("  [%s] 共 %s 个，%d 个窗口内有提交" % (label, total, len(ms)))
            for i, m in enumerate(ms[:per], 1):
                flag = "" if m.get("issues") else "  ⚠零issue"
                print("      %d) %3d commits ★%-6d %-38s%s"
                      % (i, m["commits"], m["stars"], m["full_name"][:38], flag))
    return True


def trend(c, days=7, min_stars=100, limit=40):
    """两个榜，都不看 star 排名。

    榜一 · 新冒尖（created 在窗口内）
        老仓库不管多热都进不来 —— 这是推荐页永远给不了的东西。
    榜二 · 真在动（窗口内 commit 数排序）
        按「代码有没有在长」排，不按「有多少人点星」排。
        主人原话：「用更新替代那个刷」。

    为什么不直接用网上的周涨星榜（实测 2026-11-05）：
        ossinsight  v1/trends/repos 自报「事件捕获率跌到基线 0.3%，
                    自 2026-03-01 起 unavailable」，直接返回空表；
        github.com/trending  直连 HTTP 000。
        第三方要么断了，要么在拿旧数据推算 —— 只能自己算。
    """
    import datetime
    import urllib.parse

    today = datetime.date.today()
    since_created = (today - datetime.timedelta(days=days)).isoformat()
    since_push = (today - datetime.timedelta(days=max(2, days // 2))).isoformat()

    # ---------- 榜一：新建 + 还在动 ----------
    # 加 pushed 条件：建完就跑的刷星仓库直接筛掉
    q1 = urllib.parse.quote("created:>=%s pushed:>=%s stars:>=%d"
                            % (since_created, since_push, min_stars))
    ok, d, dt = gh_search("search/repositories?q=%s&per_page=100&sort=stars&order=desc" % q1)
    raw = [norm_repo(x) for x in (d.get("items") or [])] if (ok and isinstance(d, dict)) else []
    print("\n" + "=" * 74)
    print("榜一 · 最近 %d 天新建 + 还在推送（created>=%s, pushed>=%s）"
          % (days, since_created, since_push))
    print("=" * 74)
    if ok and isinstance(d, dict):
        print("  共 %s 个符合，取 %d 个查真实提交" % (d.get("total_count"), len(raw)))
    ranked = []
    if raw:
        ranked = commits_since([tuple(m["full_name"].split("/", 1)) for m in raw],
                              since_created + "T00:00:00Z")
        # 窗口内 0 提交 = 建完就躺，和刷星仓库一个特征，直接不要
        ranked = [m for m in ranked if m.get("commits", 0) > 0]
        ranked.sort(key=lambda m: (-m["commits"], -m["stars"]))
        for i, m in enumerate(ranked[:limit], 1):
            print("  %2d. %3d commits  ★%-6d %-40s %s"
                  % (i, m["commits"], m["stars"], m["full_name"][:40],
                     ",".join((m.get("topics") or [])[:4])[:40]))
        if not ranked:
            print("  （全部 0 提交，说明这批全是建完就躺的）")

    # ---------- 榜二：已有仓库里真正在长的 ----------
    q2 = urllib.parse.quote("pushed:>=%s stars:>=%d" % (since_push, min_stars))
    ok2, d2, dt2 = gh_search("search/repositories?q=%s&per_page=100&sort=stars&order=desc" % q2)
    raw2 = [norm_repo(x) for x in (d2.get("items") or [])] if (ok2 and isinstance(d2, dict)) else []
    print("\n" + "=" * 74)
    print("榜二 · 真在长（窗口内 commit 数排，不看 star 排名）")
    print("=" * 74)
    if ok2 and isinstance(d2, dict):
        print("  共 %s 个最近有推送，取 %d 个查真实提交" % (d2.get("total_count"), len(raw2)))
    if raw2:
        r2 = commits_since([tuple(m["full_name"].split("/", 1)) for m in raw2],
                           since_push + "T00:00:00Z")
        r2 = [m for m in r2 if m.get("commits", 0) > 0]
        # 提交/星 比：高说明代码在长而不是营销在长
        for m in r2:
            m["vitality"] = round(m["commits"] / max(1.0, math.log10(max(10, m["stars"]))), 2)
        r2.sort(key=lambda m: -m["vitality"])
        for i, m in enumerate(r2[:limit], 1):
            print("  %2d. %3d commits / ★%-6d 活力%-6.1f issues%-5d %-36s"
                  % (i, m["commits"], m["stars"], m["vitality"], m["issues"],
                     m["full_name"][:36]))
    return True


def report(c):
    print("\n" + "=" * 62)
    tot = c.execute("select count(*) from entities where platform='github'").fetchone()[0]
    print("GitHub 实体 %d" % tot)
    for t, n in c.execute("select type,count(*) from entities where platform='github' "
                          "group by type order by 2 desc"):
        print("   %-8s %6d" % (t, n))
    print("GitHub 边 %d" % c.execute("select count(*) from edges where src like 'github:%'").fetchone()[0])
    for r, n in c.execute("select rel,count(*) from edges where src like 'github:%' "
                          "group by rel order by 2 desc"):
        print("   %-12s %6d  (权 %.1f)" % (r, n, REL_W.get(r, 0)))
    print("\nTOP topic（被最多仓库共享）:")
    for t, n in c.execute(
            "select e.title,count(*) c from edges g join entities e on e.id=g.dst "
            "where g.rel='has_topic' group by e.title order by c desc limit 15"):
        print("   %-28s %5d" % (t, n))


if __name__ == "__main__":
    main()
