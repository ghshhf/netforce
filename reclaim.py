# -*- coding: utf-8 -*-
"""netforce / reclaim.py —— 第一层：把「已知但没吃下」的欠账重抓回来。

为什么单开这一层：
    自举发现端已经饱和（25.8 万去重指纹 / 6.38 万域名），但知识库里
    47,793 篇文档中有 21,293 篇是「访问失败（保留待复查）」、8,324 篇
    「其他/待定」，另有约 1.6 万个域名压根没访问过。这批索引的 channel
    全是 direct，是隧道回落 / TLS 指纹 / 无头浏览器 / 正文抽取上来之前采的。
    ⇒ 它们是**已知目标**，不需要再赌发现端，用新通道按失败原因重抓即可。

分流依据（access 标签就是重抓调度表）：
    风控/需验证  → curl_cffi Chrome TLS 指纹（先），不行再无头浏览器
    不可达        → 统一出口 18080（国内直连 / 国外失败自动回落 Worker 隧道）
    需登录        → 静态直取公开部分（拿得到就算救回）
    其他/待定     → 静态漏斗 + SPA 空壳转浏览器
    从未访问      → 静态漏斗

纪律：
    · 不删任何历史记录，不动原始 records/*.jsonl，只 append 新分片；
    · 断点续传：进度在 _kb/reclaim_state.db，失败按指数退避、到期自动复活；
    · 按主机分散、晚高峰自动降并发；出口掉线由 http_first 自动退回直连；
    · 不建定时任务、不碰系统代理、不改开机自启脚本。

用法：
    python reclaim.py --build                 # 建立/补充目标队列（幂等）
    python reclaim.py --stats                 # 欠账与进度
    python reclaim.py --run 100               # 重抓 100 条（默认晚高峰 20 并发）
    python reclaim.py --run 200 --batch 3     # 连跑 3 批，每批 200 条
    python reclaim.py --run 100 --browser 10  # 最多 10 条交给无头浏览器
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(BASE, "_kb")
sys.path.insert(0, BASE)

STATE_DB = os.path.join(KB, "reclaim_state.db")
KB_DB = os.path.join(KB, "kb_index.db")
RES_JSON = os.path.join(BASE, "resources.json")
RECORDS_DIR = os.path.join(KB, "records")
POLICY = os.path.join(BASE, "_netpolicy.json")
EGRESS_LOG = os.path.join(KB, "egress_usage.jsonl")

# 明显可达却被旧版判成「不可达」的大站，优先重抓（收益最确定）
TRUSTED_REACHABLE = {
    "github.com", "aws.amazon.com", "docs.python.org", "pypi.org",
    "en.wikipedia.org", "developer.mozilla.org", "stackoverflow.com",
    "huggingface.co", "gitlab.com", "npmjs.com", "crates.io",
    "archive.org", "openstreetmap.org", "nature.com", "arxiv.org",
}
PRIORITY = {"waf": 90, "unreachable": 70, "login": 60,
            "pending": 45, "unvisited": 30}
MAX_ATTEMPTS = 3
BACKOFF = (0, 30 * 60, 6 * 3600, 24 * 3600)   # 第 1/2/3 次失败后的冷却


def log(*a, **kw):
    # http_first / render_parse 内部一律用 log(msg, flush=True) 调用，
    # 这里必须吞掉关键字，否则整批结果会在最后一行打印时抛 TypeError 丢光。
    print(*a, flush=True)


def cfg():
    try:
        with open(POLICY, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def conc_now():
    """晚高峰错峰：19:00–23:00 压到 peak_cap。"""
    t = cfg().get("task_layer") or {}
    peak, off = int(t.get("peak_cap", 20)), int(t.get("offpeak_cap", 40))
    return peak if 19 <= time.localtime().tm_hour < 23 else off


def connect():
    os.makedirs(KB, exist_ok=True)
    c = sqlite3.connect(STATE_DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS targets(
        domain TEXT PRIMARY KEY, url TEXT, reason TEXT, access TEXT,
        priority INTEGER, status TEXT DEFAULT 'pending',
        attempts INTEGER DEFAULT 0, next_ts REAL DEFAULT 0,
        last_err TEXT DEFAULT '', ok INTEGER DEFAULT 0, ts REAL)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_targets_pick "
              "ON targets(status, next_ts, priority)")
    return c


def _reason(access, ok, category):
    a = str(access or "")
    if "风控" in a:
        return "waf"
    if not ok or "不可达" in a:
        return "unreachable"
    if "需登录" in a:
        return "login"
    return "pending"


def _valid_domain(d):
    d = (d or "").lower().strip().rstrip(".")
    if not d or d == "?" or " " in d or "/" in d or len(d) > 120:
        return ""
    return d


def build():
    """幂等建队列：INSERT OR IGNORE，已跑过/冷却中的目标状态不重置。"""
    c = connect()
    before = c.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
    added = Counter()

    try:
        src = sqlite3.connect(KB_DB, timeout=30)
        rows = src.execute(
            "SELECT domain,url,access,ok,category FROM docs "
            "WHERE ok=0 OR category IN ('其他/待定') "
            "OR access IN ('风控/需验证','需登录','付费墙')").fetchall()
        indexed = {r[0] for r in src.execute("SELECT domain FROM docs")}
        src.close()
    except Exception as e:
        log("[build] 知识库读取失败：%s" % e)
        c.close()
        return
    for dom, url, access, ok, category in rows:
        d = _valid_domain(dom)
        if not d:
            continue
        rs = _reason(access, ok, category)
        pri = PRIORITY[rs] + (15 if rs == "unreachable" and d in TRUSTED_REACHABLE else 0)
        u = (url or "").strip() or ("https://%s/" % d)
        if not u.startswith("http"):
            u = "https://%s/" % d
        cur = c.execute("INSERT OR IGNORE INTO targets"
                        "(domain,url,reason,access,priority,ts) "
                        "VALUES(?,?,?,?,?,?)", (d, u, rs, access or "", pri, time.time()))
        if cur.rowcount:
            added["docs:" + rs] += 1
    try:
        with open(RES_JSON, encoding="utf-8") as f:
            res = json.load(f)
    except Exception:
        res = []
    for x in res:
        d = _valid_domain((x or {}).get("domain"))
        if not d or d in indexed:
            continue
        cur = c.execute("INSERT OR IGNORE INTO targets"
                        "(domain,url,reason,access,priority,ts) "
                        "VALUES(?,?,?,?,?,?)",
                        (d, "https://%s/" % d, "unvisited", "",
                         PRIORITY["unvisited"], time.time()))
        if cur.rowcount:
            added["resources:unvisited"] += 1
    c.commit()
    total = c.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
    c.close()
    log("[build] 目标 %d → %d（新增 %d）" % (before, total, total - before))
    for k, v in sorted(added.items(), key=lambda kv: -kv[1]):
        log("   + %-24s %d" % (k, v))


def stats():
    c = connect()
    log("== 重抓队列 ==")
    for r in c.execute("SELECT reason,status,COUNT(*) FROM targets "
                       "GROUP BY reason,status ORDER BY reason,status"):
        log("   %-14s %-9s %d" % r)
    log("   合计 %d｜已救回 %d｜待办 %d" % (
        c.execute("SELECT COUNT(*) FROM targets").fetchone()[0],
        c.execute("SELECT COUNT(*) FROM targets WHERE ok=1").fetchone()[0],
        c.execute("SELECT COUNT(*) FROM targets WHERE status='pending'").fetchone()[0]))
    c.close()
    try:
        k = sqlite3.connect(KB_DB, timeout=10)
        log("== 知识库 ==")
        log("   ok 分布 %s" % (k.execute(
            "SELECT ok,COUNT(*) FROM docs GROUP BY ok").fetchall(),))
        log("   access %s" % (k.execute(
            "SELECT access,COUNT(*) FROM docs GROUP BY access "
            "ORDER BY COUNT(*) DESC LIMIT 8").fetchall(),))
        k.close()
    except Exception as e:
        log("   知识库读取失败：%s" % e)


def _recent_routes(n=3000):
    """从统一出口的使用记录反查「这一批到底走了哪条路」，让 egress 标签是真的。"""
    out = {}
    try:
        with open(EGRESS_LOG, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()[-n:]
    except Exception:
        return out
    for ln in lines:
        try:
            d = json.loads(ln)
        except Exception:
            continue
        # egress_route.record 落盘字段是缩写：h=host, r=route kind, ok=成功,
        # v=via(上层通道名，CONNECT 隧道也叫 "tunnel"，别拿它判 Worker 隧道)。
        h, rt, ok = d.get("h"), d.get("r"), d.get("ok")
        if h and rt and ok:
            out[str(h).lower()] = str(rt)
    return out


def _rec_from_html(dom, url, html, channel, egress="direct", status=200):
    """把任一通道拿到的 HTML 组装成与 records/*.jsonl 同构的记录。"""
    import http_first as hf
    import fine_classify as fc
    t0 = time.time()
    data = hf._extract(html)
    scheme = "https" if url.startswith("https") else "http"
    rec = hf._build(dom, url, scheme, status, data, time.time() - t0)
    rec["channel"] = channel
    rec["egress"] = egress
    rec["tags"] = fc.tag(rec)          # 覆盖后重打，出口/地域标签才准
    return rec


# 拦截页的强特征（不放 "cloudflare" 这种泛词，避免误伤介绍它的文章）
_BLOCK_STRONG = ("just a moment", "attention required", "checking your browser",
                 "verify you are human", "enable javascript and cookies",
                 "cf-browser-verification", "人机验证", "请完成验证",
                 "访问被拒绝", "access denied")


def _ok_rec(rec):
    """「救回」的门槛：只拿到 <title> 的拦截页 / 空壳不算数。

    试点教训：curl_cffi 能把风控降级页也取成 200，若只看标题就会把
    "Just a moment..." 这类空壳记成有效资源。正文至少 120 字才算，
    或标题 + 40 字以上（短页如文档首页）也可接受。下载链接单独算接住。
    """
    if not rec or not rec.get("ok"):
        return False
    n = int(rec.get("text_len") or 0)
    # 2026-11-14 补：光看长度会把「拦截页」当正文。
    # 实测 30 条 waf 队列，标成救回 14 条，其中 10 条是
    # "Attention Required! | Cloudflare"（516 字）和 "Just a moment..."（16 字），
    # 真内容只有 3 条。⇒ 先认拦截页特征，再谈长度；长文里顺带提到这些词的不算。
    title = (rec.get("title") or "").lower()
    head = (rec.get("text") or "")[:600].lower()
    for w in _BLOCK_STRONG:
        if (w in title or w in head) and n < 1500:
            return False
    if n >= 120:
        return True
    if n >= 40 and (rec.get("title") or "").strip() and rec.get("status") == 200:
        return True
    return str(rec.get("channel") or "").endswith("/asset")


def _normalize(rec, routes):
    """给静态漏斗的记录补上真实出口通道。"""
    import fine_classify as fc
    d = (rec.get("domain") or "").lower()
    rt = routes.get(d, "")
    if "tunnel" in rt:
        rec["channel"], rec["egress"] = "reclaim/tunnel", "tunnel"
    else:
        rec["channel"], rec["egress"] = "reclaim/egress", "direct"
    rec["tags"] = fc.tag(rec)
    return rec


def _persist(recs):
    """append 新分片 + 覆盖式 upsert 进索引（不删旧记录、不碰原始分片）。"""
    import kb_index
    os.makedirs(RECORDS_DIR, exist_ok=True)
    path = os.path.join(RECORDS_DIR,
                        "rec-reclaim-%s.jsonl" % time.strftime("%Y%m%d"))
    with open(path, "a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    c = kb_index.connect()
    kb_index.ensure()
    for r in recs:
        try:
            kb_index.upsert(c, r, os.path.basename(path), 0)
        except Exception as e:
            log("   [index] upsert 失败 %s：%s" % (r.get("domain"), str(e)[:70]))
    c.commit()
    c.close()
    return os.path.basename(path)


def _curl_batch(items, timeout):
    """风控通道：curl_cffi 的 Chrome TLS 指纹，按线程复用 CONNECT 隧道。"""
    import resource_finder as rf
    out, errs = [], {}

    def one(it):
        try:
            html = rf._curl_get(it["url"], timeout=timeout)
            if html and len(html) > 200:
                rec = _rec_from_html(it["domain"], it["url"], html,
                                     "reclaim/fingerprint", "fingerprint")
                return it["domain"], rec
        except Exception as e:
            return it["domain"], {"err": "%s:%s" % (type(e).__name__, str(e)[:60])}
        return it["domain"], {"err": "fingerprint:empty"}

    with ThreadPoolExecutor(max_workers=min(12, len(items))) as ex:
        for dom, r in ex.map(one, items):
            if r.get("err"):
                errs[dom] = r["err"]
            else:
                out.append(r)
    return out, errs


def _browser_batch(items, timeout, cap):
    """SPA 空壳 / 风控漏网：Playwright sync 非线程安全 ⇒ 全局串行、限量。"""
    import resource_finder as rf
    out, errs = [], {}
    for it in items[:cap]:
        try:
            html = rf._browser_get(it["url"], timeout=timeout)
            if html and len(html) > 200:
                rec = _rec_from_html(it["domain"], it["url"], html,
                                     "reclaim/browser", "browser")
                rec["rendered"] = True
                rec["tier"] = "browser"
                import fine_classify as fc
                rec["tags"] = fc.tag(rec)
                out.append(rec)
            else:
                errs[it["domain"]] = "browser:empty"
        except Exception as e:
            errs[it["domain"]] = "%s:%s" % (type(e).__name__, str(e)[:60])
    return out, errs


def run(limit=100, conc=None, timeout=9.0, batches=1, browser=0, reason="",
        domain_glob=""):
    conc = int(conc or conc_now())
    c = connect()
    total_ok, total_try = 0, 0
    for bi in range(batches):
        now = time.time()
        sql = ("SELECT domain,url,reason,attempts FROM targets "
               "WHERE status='pending' AND next_ts<=? ")
        par = [now]
        if reason:
            sql += "AND reason=? "
            par.append(reason)
        if domain_glob:
            # 2026-11-14：按域名后缀分流。国内站直连可通、国际站要隧道，
            # 混在一批里跑会让「通道判断」失真，必须能分开跑。
            sql += "AND domain LIKE ? "
            par.append(domain_glob)
        sql += "ORDER BY priority DESC, attempts ASC, RANDOM() LIMIT ?"
        par.append(limit)
        rows = c.execute(sql, par).fetchall()
        if not rows:
            log("[run] 第 %d 批：没有到期目标" % (bi + 1))
            break
        items = [{"domain": d, "url": u or "https://%s/" % d,
                  "reason": rs} for d, u, rs, _ in rows]
        waf = [x for x in items if x["reason"] == "waf"]
        static = [x for x in items if x["reason"] != "waf"]
        log("[run] 第 %d/%d 批：%d 条（静态 %d / 指纹 %d），并发 %d，超时 %.0fs"
            % (bi + 1, batches, len(items), len(static), len(waf), conc, timeout))
        t0 = time.time()
        solved, errs = [], {}

        # ① 风控 → TLS 指纹
        if waf:
            got, e2 = _curl_batch(waf, timeout)
            solved.extend(got)
            errs.update(e2)

        # ② 其余 → 统一出口的静态滚动漏斗（allow_dead=False：失败回队列重试）
        browser_items = []
        if static:
            import http_first as hf
            try:
                done, rest = hf.fetch_many_slide(
                    static, conc=conc, timeout=timeout, min_text=180,
                    allow_dead=False, ladder=(2.0, 5.0, 9.0), log=log)
                routes = _recent_routes()
                for r in done:
                    if _ok_rec(r):
                        solved.append(_normalize(r, routes))
                    else:
                        errs[r.get("domain", "?")] = "static:empty"
                for r in rest:
                    why = str(r.get("why") or "").lower()
                    if ("shell" in why or "spa" in why or "403" in why
                            or "challenge" in why or "cloudflare" in why):
                        browser_items.append({"domain": r.get("domain"),
                                              "url": r.get("url")})
                    else:
                        errs[r.get("domain", "?")] = why[:80] or "static:fail"
            except Exception as e:
                log("   [static] 漏斗异常：%s" % str(e)[:100])
                for x in static:
                    errs[x["domain"]] = "static-exception:" + type(e).__name__

        # ③ 空壳 / 风控 → 无头浏览器（限量、串行）
        if browser and browser_items:
            got, e3 = _browser_batch(browser_items, timeout + 6, browser)
            solved.extend(got)
            errs.update(e3)

        # ④ 落盘 + 覆盖索引
        if solved:
            shard = _persist(solved)
            log("   [persist] 救回 %d 条 → %s" % (len(solved), shard))

        # ⑤ 进度与退避
        okmap = {r["domain"]: r for r in solved}
        upd = []
        for d, u, rs, att in rows:
            if d in okmap:
                upd.append(("done", 1, att + 1, 0, "", time.time(), d))
            else:
                err = str(errs.get(d, "no-channel"))[:110]
                # 通道自身异常（不是这个站不可达）不该消耗重试次数、不该进冷却
                if err.startswith("static-exception"):
                    upd.append(("pending", 0, att, 0, err, time.time(), d))
                    continue
                n = att + 1
                st = "dead" if n >= MAX_ATTEMPTS else "pending"
                cool = BACKOFF[min(n, len(BACKOFF) - 1)]
                upd.append((st, 0, n, now + cool if st == "pending" else 0,
                            err, time.time(), d))
        c.executemany("UPDATE targets SET status=?,ok=?,attempts=?,next_ts=?,"
                      "last_err=?,ts=? WHERE domain=?", upd)
        c.commit()
        total_ok += len(solved)
        total_try += len(rows)
        log("   [done] %d 条中救回 %d（%.1f%%），耗时 %.1fs，失败样例：%s"
            % (len(rows), len(solved), 100.0 * len(solved) / max(1, len(rows)),
               time.time() - t0, list(errs.items())[:3]))
    c.close()
    log("[run] 合计处理 %d 条，救回 %d 条" % (total_try, total_ok))
    return total_ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--run", type=int, default=0)
    ap.add_argument("--conc", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=9.0)
    ap.add_argument("--batch", type=int, default=1)
    ap.add_argument("--browser", type=int, default=0)
    ap.add_argument("--reason", default="",
                    choices=["", "waf", "unreachable", "login", "pending",
                             "unvisited"], help="只跑某一类欠账（试点用）")
    ap.add_argument("--domain-glob", default="",
                    help="只跑匹配的域名，如 '%%.cn'（分流国内/国际目标）")
    a = ap.parse_args()
    if a.build:
        build()
    if a.run:
        run(limit=a.run, conc=a.conc or None, timeout=a.timeout,
            batches=a.batch, browser=a.browser, reason=a.reason,
            domain_glob=a.domain_glob)
    if a.stats or not (a.build or a.run):
        stats()


if __name__ == "__main__":
    main()
