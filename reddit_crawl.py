#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Reddit 定向关联爬取 —— **纯直连，零代理**（2026-11-05 实测打通）

为什么不用代理
--------------
`reddit.com` 主域在国内是 IP/SNI 层拦截，换真实 IP 也不通；但
**Arctic Shift**（第三方全量归档，免 key）的域 `arctic-shift.photon-reddit.com`
走工程自带的「DoH 真实 IP + curl --resolve」纯直连即可拿到 HTTP 200 真实数据。
这不是代理：连接直接打到目标 IP，不经过 7890 / 3067 / karing，也不读系统代理。

    普通 curl（被污染的 DNS）  → 000
    DoH 真实 IP + --resolve   → 200 / 6.5KB 帖子 JSON   ← 本模块走这条

坑（实测 2026-11-05，别再踩）
----------------------------
✗ `/api/posts/search?q=xxx` → HTTP 400「Unknown query parameter: 'q'」
  ⇒ 这个 API **不支持全文关键词**，只支持 subreddit / author / after-before / limit
  ⇒ 400 是参数错误不是网络不通，别拿它当「必须开代理」的证据
✗ `sort=desc` / `after=2024-01-01` → 422「Timeout. Maybe slow down a bit」
  ⇒ 服务端自己慢，别加排序/大跨度时间；用最小参数 + 重试
✗ `/api/comments/search` 经常 000（服务端超时）⇒ 评论树只做 best-effort，失败不阻塞
✓ `subreddit=` + `limit=` 是最稳的组合，limit ≤ 100
✓ 不同时刻 DoH 会给出不同 IP（Cloudflare），每次请求前重新解析、失败换下一个 IP

产出（写进 _kb/seeds.db，与 hn/hf/bili 同构）
-------------------------------------------
实体：post / author / subreddit / domain / comment
边  ：by_author、in_subreddit、links_to、replied_to、crosspost_of（跨版桥）

用法
----
    python reddit_crawl.py --sub LocalLLaMA --limit 50
    python reddit_crawl.py --subs LocalLLaMA,MachineLearning,selfhosted --limit 30
    python reddit_crawl.py --author <name> --limit 50
    python reddit_crawl.py --report
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import headless
import sys
import time
import urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

DB = (os.environ.get("NETFORCE_DB")
      or os.path.join(BASE, "_kb", "seeds.db"))

HOST = "arctic-shift.photon-reddit.com"
UA = "netforce/1.0 (research crawler; pure-direct; no proxy)"

# 复用工程现成的纯直连层，不重造轮子
import netpolicy  # noqa: E402
import direct_ip  # noqa: E402
from skynet import dns as _dns  # noqa: E402

RE_URL = re.compile(r'https?://[^\s\)\]\>"\'`]+', re.I)
# 排除 Reddit 自身的链，只留站外资源（和 hn_crawl 的 MEGA 判据同理）
SELF_HOSTS = ("reddit.com", "redd.it", "redditstatic.com", "redditmedia.com",
              "i.redd.it", "v.redd.it", "preview.redd.it")


def _env():
    """清掉所有 *_proxy，保证 curl 是纯直连（direct_ip 纪律）。"""
    return direct_ip._clean_env()


def _ips(host, timeout=5):
    try:
        return [x for x in _dns.resolve(host, timeout=timeout) if x][:3]
    except Exception:
        return []


def _one(ip, path, timeout):
    """对单个真实 IP 发一次纯直连请求。返回 (结果dict|None, 诊断串)。"""
    import tempfile
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="nf_reddit_", suffix=".json")
        os.close(fd)
        # 🔴 body 落临时文件、stdout 只留状态码：避免 -o - 时 body 与 -w 混在同一
        #   管道，用换行切分会在「响应被截断」时把半个 JSON 当成完整结果。
        r = headless.run(
            ["curl", "-s", "--noproxy", "*", "--compressed",
             "--resolve", "%s:443:%s" % (HOST, ip),
             "-A", UA, "--max-time", str(timeout),
             "-o", tmp, "-w", "%{http_code}",
             "https://%s%s" % (HOST, path)],
            capture_output=True, timeout=timeout + 5, env=_env(),
            stdin=subprocess.DEVNULL,
            startupinfo=netpolicy._hidden_startupinfo(),
            creationflags=subprocess.CREATE_NO_WINDOW)
        c = (r.stdout or b"").strip().decode("ascii", "ignore")
        body = open(tmp, "rb").read() if os.path.exists(tmp) else b""
        diag = "HTTP %s via %s (%d bytes)" % (c, ip, len(body))
        if c == "400":
            return (None, diag + " " + body.decode("utf-8", "ignore")[:120], True)
        if c.isdigit() and 200 <= int(c) < 400 and body:
            try:
                j = json.loads(body)
                if isinstance(j, dict) and "data" in j:
                    return (j, diag, True)       # 真成功
                return (None, diag + " (无 data 字段)", False)
            except Exception:
                return (None, diag + " (响应不完整/非 JSON)", False)
        return (None, diag + (" (slow down)" if c == "422" else ""), False)
    except Exception as e:
        return (None, "超时/异常 %s via %s" % (type(e).__name__, ip), False)
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def api(path, timeout=20, retry=1):
    """纯直连 GET Arctic Shift API；返回 (dict|None, 诊断串)。

    Cloudflare 多 IP，实测同一请求不同 IP/时刻会分别返回：
      200+JSON（正常）、422「slow down」、200+HTML（前端页）、000（该 IP 丢包）。
    ⇒ 「门是间歇开的」：不做串行死等，改成**多 IP 并发抢答**，谁先拿到带
      data 字段的 JSON 就用谁（README 第三轮「通了就用、不通就如实记失败」）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    diag = "全部 IP 未返回有效数据"
    for round_no in range(retry + 1):
        ips = _ips(HOST)
        if not ips:
            diag = "DoH 无解析"
            time.sleep(0.5)
            continue
        # 🔴 不能用 as_completed「谁先返回用谁」：慢的边缘节点会先吐出截断的半个
        #   JSON（实测同请求 172.67.139.201 给 26752B 完整、104.21.8.166 给
        #   11523B 截断）。要等所有 IP 都试完，取其中**解析成功**的那个。
        with ThreadPoolExecutor(max_workers=len(ips)) as ex:
            results = [f.result() for f in as_completed(
                [ex.submit(_one, ip, path, timeout) for ip in ips])]
        for j, d, decisive in results:
            diag = d
            if decisive and j is None:       # 400 参数错，重试无意义
                return None, d
        for j, d, decisive in results:
            if j is not None:
                return j, d
        time.sleep(0.6 * (round_no + 1))
    return None, diag


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

    def ent(self, eid, etype, title, url="", depth=0, payload=None):
        cur = self.c.execute(
            "INSERT OR IGNORE INTO entities(id,platform,type,title,url,depth,payload,ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (eid, "reddit", etype, title or "", url, str(depth),
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


def pid(x):
    return "reddit:post:%s" % x


def aid(x):
    return "reddit:user:%s" % x


def sid(x):
    return "reddit:sub:%s" % x


def did(x):
    return "reddit:domain:%s" % x


def cid(x):
    return "reddit:comment:%s" % x


def host_of(u):
    m = re.match(r"https?://([^/?#]+)", u or "")
    if not m:
        return ""
    h = m.group(1).lower()
    return h[4:] if h.startswith("www.") else h


def out_links(text):
    """从 selftext / 标题里抠站外链接，去掉 Reddit 自家域名。"""
    out = []
    for u in RE_URL.findall(text or ""):
        h = host_of(u)
        if h and not any(h == s or h.endswith("." + s) for s in SELF_HOSTS):
            out.append((h, u.rstrip(".,;)")))
    return out


def absorb_post(w, p, depth=0):
    """一条 post → 作者 / 版块 / 站外域名 / 跨版桥，全是可关联的图结构。"""
    oid = p.get("id") or p.get("name", "").replace("t3_", "")
    if not oid:
        return 0
    title = p.get("title") or ""
    author = p.get("author") or ""
    sub = p.get("subreddit") or ""
    permalink = p.get("permalink") or ""
    url = p.get("url") or ""
    body = p.get("selftext") or ""
    created = p.get("created_utc")

    e = pid(oid)
    w.ent(e, "post", title,
          "https://www.reddit.com%s" % permalink if permalink else url,
          depth,
          {"score": p.get("score"), "num_comments": p.get("num_comments"),
           "created_utc": created, "upvote_ratio": p.get("upvote_ratio"),
           "flair": p.get("link_flair_text"), "nsfw": p.get("over_18"),
           "selftext": body[:1500]})

    if author and author not in ("[deleted]", "AutoModerator"):
        w.ent(aid(author), "user", author,
              "https://www.reddit.com/user/%s" % author, depth + 1)
        w.edge(aid(author), e, "by_author")
        w.edge(e, aid(author), "has_author")

    if sub:
        w.ent(sid(sub), "subreddit", sub,
              "https://www.reddit.com/r/%s" % sub, depth + 1)
        w.edge(e, sid(sub), "in_subreddit")
        w.edge(sid(sub), e, "has_post")

    # 站外资源：帖子正文 + 外链本身，都按域名建节点（真关联，不是堆 URL）
    for h, u in out_links(body) + ([(host_of(url), url)] if host_of(url) else []):
        if not h or any(h == s or h.endswith("." + s) for s in SELF_HOSTS):
            continue
        w.ent(did(h), "domain", h, "https://" + h, depth + 1)
        w.edge(e, did(h), "links_to")
        w.ent("reddit:url:%s" % _key(u), "url", u[:180], u, depth + 1)
        w.edge(e, "reddit:url:%s" % _key(u), "has_url")

    # 跨版桥：crosspost 列表把两个 subreddit 连起来，是最高价值的边
    for xp in (p.get("crosspost_parent_list") or [])[:3]:
        xsub = xp.get("subreddit")
        xid = xp.get("id")
        if xsub and xid:
            w.ent(sid(xsub), "subreddit", xsub,
                  "https://www.reddit.com/r/%s" % xsub, depth + 2)
            w.edge(e, sid(xsub), "crosspost_of")
    return 1


def _key(s):
    import hashlib
    return hashlib.md5((s or "").encode("utf-8", "ignore")).hexdigest()[:12]


def crawl_sub(w, sub, limit=50):
    path = "/api/posts/search?subreddit=%s&limit=%d" % (
        urllib.parse.quote(sub), max(1, min(100, limit)))
    j, diag = api(path)
    if not j:
        print("  r/%s → 失败：%s" % (sub, diag), flush=True)
        return 0
    posts = j.get("data") or []
    n = 0
    for p in posts:
        n += absorb_post(w, p)
    w.flush()
    print("  r/%-22s → %3d 帖 / 新增实体 %4d / 边 %4d   [%s]"
          % (sub, len(posts), w.new_ent, w.new_edge, diag), flush=True)
    return n


def crawl_author(w, author, limit=50):
    path = "/api/posts/search?author=%s&limit=%d" % (
        urllib.parse.quote(author), max(1, min(100, limit)))
    j, diag = api(path)
    if not j:
        print("  u/%s → 失败：%s" % (author, diag), flush=True)
        return 0
    posts = j.get("data") or []
    for p in posts:
        absorb_post(w, p)
    w.flush()
    print("  u/%-22s → %3d 帖 / 新增实体 %4d / 边 %4d   [%s]"
          % (author, len(posts), w.new_ent, w.new_edge, diag), flush=True)
    return len(posts)


def report(c):
    print("=== Reddit 直连采集现状（%s）===" % DB)
    for t in ("entities", "edges"):
        try:
            print("  全库 %s: %d" % (t, c.execute("select count(*) from %s" % t).fetchone()[0]))
        except Exception as e:
            print("  ", t, "ERR", e)
    print("  reddit 实体:", c.execute(
        "select count(*) from entities where platform='reddit'").fetchone()[0])
    for r in c.execute("select type,count(*) from entities where platform='reddit' "
                       "group by type order by 2 desc"):
        print("    %-12s %d" % (r[0], r[1]))
    for r in c.execute("select rel,count(*) from edges where src like 'reddit:%' "
                       "group by rel order by 2 desc"):
        print("    edge %-14s %d" % (r[0], r[1]))


def main():
    ap = argparse.ArgumentParser(description="Reddit 纯直连关联爬取（Arctic Shift + DoH）")
    ap.add_argument("--sub", help="单个 subreddit")
    ap.add_argument("--subs", help="逗号分隔多个 subreddit")
    ap.add_argument("--author", help="按作者拉帖")
    ap.add_argument("--limit", type=int, default=50, help="每个目标最多几条（≤100）")
    ap.add_argument("--report", action="store_true", help="只看统计")
    a = ap.parse_args()

    c = connect()
    if a.report:
        report(c)
        return
    w = Writer(c)
    t0 = time.time()
    subs = []
    if a.subs:
        subs = [x.strip() for x in a.subs.split(",") if x.strip()]
    elif a.sub:
        subs = [a.sub]
    if not subs and not a.author:
        subs = ["LocalLLaMA", "MachineLearning", "selfhosted", "datasets",
                "opensource", "DataHoarder", "homelab", "python"]
    print("Reddit 纯直连（DoH + --resolve，零代理）→ %s" % DB, flush=True)
    total = 0
    for s in subs:
        total += crawl_sub(w, s, a.limit)
        time.sleep(0.5)          # 服务端会 422「slow down」，留间隔
    if a.author:
        total += crawl_author(w, a.author, a.limit)
    w.flush()
    print("完成：%d 帖处理 / 新增实体 %d / 边 %d / %.1fs"
          % (total, w.new_ent, w.new_edge, time.time() - t0), flush=True)
    c.close()


if __name__ == "__main__":
    main()
