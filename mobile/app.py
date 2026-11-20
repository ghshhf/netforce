#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""netforce mobile —— 把爬虫能力装进手机，24 小时不断网地跑。

设计取舍（为什么不是 APK）
------------------------
打包 APK 需要 Linux 交叉编译链，而采集脚本**只依赖 httpx + 标准库**
（hn / hongguo / hf 三个都验证过）。既然如此，直接原地跑就是最优解：

  · 不用编译，改一行就生效
  · 日志真实可见，出问题能当场调
  · 手机浏览器「添加到主屏幕」后，与原生 App 无差别（全屏 PWA）

所以这个文件做的事只有三件：
  1. 起一个 HTTP 服务，给手机浏览器看板用（**纯标准库，零依赖**）
  2. 按策略轮转调用同目录的采集脚本（subprocess，脚本一行不改）
  3. 把结果读出来给看板：统计、日志、关系图

为什么用 subprocess 而不是 import
--------------------------------
采集脚本各自有 main() 解析 argv，import 进来要改结构；而且长期运行
最怕内存泄漏和卡死——子进程跑完就回收，一个挂了不影响主服务。
手机上内存只有几 GB，稳定性比优雅重要。

用法
----
    python app.py                 # 起服务，默认 0.0.0.0:8848
    python app.py --port 9000
    python app.py --once hn       # 只跑一个任务后退出（调试用）
    python app.py --no-sched      # 起服务但不自动轮转
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import collections
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                 # netforce 根目录

# 手机是独立节点，用自己的库 —— 不和电脑抢写锁，也不被电脑的 78MB 拖累。
# 想跟电脑共用一份，设 NETFORCE_DB 指过去即可（但两边别同时写）。
DB = os.environ.get("NETFORCE_DB") or os.path.join(HERE, "kb.db")
LOGDIR = os.path.join(HERE, "logs")
STATE = os.path.join(HERE, "state.json")
WEB = os.path.join(HERE, "web")

os.makedirs(LOGDIR, exist_ok=True)

# ---------------------------------------------------------------- 任务表
# 每个任务 = 一条命令行（或返回命令行的函数）+ 最小间隔（秒）
# 间隔是按各站实测风控定的，不是拍脑袋：
#   hn      直连零密钥，Algolia 宽松，10 分钟一次足够
#   hongguo 实测 0.45s 连跑 28 次无风控，但目录枚举 33 页要几分钟，30 分钟一轮
#   hf      走镜像，量大，1 小时
#   youtube 要代理，手机上代理常掉，2 小时
#   xhs     免签通道 0.3s/30 条，但产出偏随机，40 分钟

def _yt_seeds(n=3):
    """YouTube 必须给种子才动。从库里挑最新的几个视频继续扩散。

    不用频道入口的原因：`--channel` 只列视频不建 related 边，
    而 related 边才是这个平台唯一高价值的东西（1 跳以内可信）。
    """
    if not os.path.exists(DB):
        return []
    try:
        c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
        rows = c.execute(
            "SELECT id FROM entities WHERE platform='youtube' AND type='video' "
            "ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
        c.close()
        return [r[0].rsplit(":", 1)[-1] for r in rows]
    except Exception:
        return []


def _yt_cmd():
    seeds = _yt_seeds()
    if not seeds:
        return None                      # 库还没种子，这轮跳过
    cmd = [sys.executable, "youtube_crawl.py", "--expand"]
    cmd += seeds
    cmd += ["--rounds", "1", "--limit", "4"]
    return cmd


def _pick(seq, period):
    """按时间轮转取样。

    24 小时无人值守最难的是「覆盖」——写死一个关键词，跑一万遍还是那一个角度。
    用时间戳取模轮转，一天下来每个关键词都能轮到，不需要额外的调度状态。
    """
    return seq[int(time.time() // period) % len(seq)]


# B 站关键词 —— 按「人话」找资源，跟 GitHub 按 topic 找（机器分类）是两个方向。
# 这四个词是实测出来的：前三个链接密度 8/20、8/20、5/20，
# 第四个「自己写的 开源」几乎全是个人开发者，主页星数个位数 ——
# 这正是主人说的「B 站简介里给的一般都是低星项目」那批。
BILI_KWS = ["自己写的 开源", "开源项目 推荐", "github 项目", "个人开发者"]

DOUBAN_KINDS = ["movie", "tv", "book", "music"]


def _bili_cmd():
    return [sys.executable, "seed_crawl.py",
            "--bili-kw", _pick(BILI_KWS, 3600),
            "--bili-pages", "1", "--platform", "bilibili",
            "--max-pages", "12", "--max-depth", "1", "--budget", "200"]


def _douban_cmd():
    return [sys.executable, "seed_crawl.py",
            "--douban-coll", _pick(DOUBAN_KINDS, 7200),
            "--platform", "douban",
            "--max-pages", "14", "--max-depth", "1", "--budget", "200"]


def _has_playwright():
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def _pending(platform):
    """队列里还有多少这个平台的活没干。"""
    if not os.path.exists(DB):
        return 0
    try:
        c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
        n = c.execute("SELECT count(*) FROM seeds WHERE platform=? AND state='new'",
                      (platform,)).fetchone()[0]
        c.close()
        return n
    except Exception:
        return 0


def _douyin_cmd():
    """抖音只有一条路：真渲染。

    实测把四条通道全试死了 —— 分享页是空壳、iteminfo 返 129B、
    网页版 detail 直接 403（Argus）、frontierSign 只给 X-Bogus 而接口要 a_bogus。
    ⇒ 没有纯 HTTP 解法。手机上装不了 playwright，所以这个任务只能电脑跑。
    检测不到就返回 None，调度器跳过，不会一直报错刷日志。
    """
    if not _has_playwright():
        return None
    if _pending("douyin") == 0:
        return None                  # 队列空着，跑了也是空转
    return [sys.executable, "seed_crawl.py", "--platform", "douyin",
            "--max-pages", "6", "--max-depth", "1", "--budget", "200"]


TASKS = collections.OrderedDict([
    # ---------------- 国内 ----------------
    ("bili", dict(
        cmd=_bili_cmd, every=15 * 60,
        desc="B 站关键词搜视频（简介里的仓库自动进 GitHub 队列）",
        need_proxy=False, plat="bilibili", region="cn")),
    ("douban", dict(
        cmd=_douban_cmd, every=40 * 60,
        desc="豆瓣分类集合（电影/剧集/书/音乐，免种子）",
        need_proxy=False, plat="douban", region="cn")),
    ("hongguo", dict(
        cmd=[sys.executable, "hongguo_crawl.py", "--crawl", "--pages", "5"],
        every=30 * 60, desc="红果短剧目录（真人/AI/漫剧三频道）",
        need_proxy=False, plat="hongguo", region="cn")),
    ("xhs", dict(
        cmd=[sys.executable, "seed_crawl.py", "--xhs-explore", "3",
             "--platform", "xiaohongshu"],
        every=40 * 60, desc="小红书 explore 首页流",
        need_proxy=True, plat="xiaohongshu", region="cn")),
    ("douyin", dict(
        cmd=_douyin_cmd, every=60 * 60,
        desc="抖音作品（要浏览器渲染，手机端跑不了）",
        need_proxy=False, plat="douyin", need_browser=True, region="cn")),
    # ---------------- 国外 ----------------
    ("hn", dict(
        cmd=[sys.executable, "hn_crawl.py", "--stories", "1000"],
        every=10 * 60, desc="Hacker News 故事流", need_proxy=False, plat="hn", region="intl")),
    ("hf", dict(
        cmd=[sys.executable, "hf_crawl.py", "--top", "60"],
        every=60 * 60, desc="HF 热门模型及衍生树", need_proxy=False, plat="hf", region="intl")),
    ("yt", dict(
        cmd=_yt_cmd, every=120 * 60, desc="YouTube 相关视频扩散",
        need_proxy=True, plat="youtube", region="intl")),
])

_proc_lock = threading.Lock()
_running = {}          # task -> {"start": ts, "proc": Popen}


# ---------------------------------------------------------------- 状态
def load_state():
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


# ---------------------------------------------------------------- 采集
def log_path(task):
    return os.path.join(LOGDIR, "%s.log" % task)


def tail(path, n=80):
    """只读文件尾部，手机上日志可能很大，不能整读。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            data = f.read().decode("utf-8", "replace")
        return "\n".join(data.splitlines()[-n:])
    except Exception:
        return ""


def run_task(task, wait=False):
    """跑一个采集任务。已在跑就拒绝，避免打爆对方服务器。"""
    cfg = TASKS.get(task)
    if not cfg:
        return {"ok": False, "err": "unknown task"}
    # cmd 可以是固定列表，也可以是函数（要现算参数的，比如 YouTube 种子）
    cmd = cfg["cmd"]() if callable(cfg["cmd"]) else cfg["cmd"]
    if not cmd:
        return {"ok": False, "err": "没有可用种子，跳过"}
    with _proc_lock:
        cur = _running.get(task)
        if cur and cur["proc"].poll() is None:
            return {"ok": False, "err": "already running", "pid": cur["proc"].pid}

        # 日志按天切割，避免无限长
        lp = log_path(task)
        if os.path.exists(lp) and os.path.getsize(lp) > 8 * 1024 * 1024:
            try:
                os.replace(lp, lp + ".1")
            except Exception:
                pass

        fh = open(lp, "a", encoding="utf-8", buffering=1)
        fh.write("\n===== %s 启动 %s =====\n" % (
            task, time.strftime("%Y-%m-%d %H:%M:%S")))
        fh.flush()
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        # 指到手机自己的库，别去碰电脑那份
        env["NETFORCE_DB"] = DB
        # 手机上没有桌面浏览器的 DISPLAY，明确告诉脚本别起界面
        env.setdefault("NETFORCE_MOBILE", "1")
        try:
            p = subprocess.Popen(cmd, cwd=ROOT, stdout=fh,
                                 stderr=subprocess.STDOUT, env=env,
                                 stdin=subprocess.DEVNULL)
        except Exception as e:
            fh.write("启动失败: %r\n" % (e,))
            fh.close()
            return {"ok": False, "err": str(e)}
        _running[task] = {"start": time.time(), "proc": p, "fh": fh}

    if wait:
        p.wait()
        fh.close()
        return {"ok": True, "rc": p.returncode}

    # 后台等结束，回收句柄并记下用时
    def waiter():
        rc = p.wait()
        try:
            fh.write("===== %s 结束 rc=%s 用时 %.0fs =====\n" % (
                task, rc, time.time() - _running[task]["start"]))
            fh.close()
        except Exception:
            pass
        st = load_state()
        st.setdefault("last", {})[task] = {
            "at": time.time(), "rc": rc,
            "secs": round(time.time() - _running[task]["start"], 1)}
        save_state(st)
        with _proc_lock:
            _running.pop(task, None)

    threading.Thread(target=waiter, daemon=True).start()
    return {"ok": True, "pid": p.pid}


def scheduler():
    """轮转：一次只跑一个任务，跑完等间隔到点再跑下一个。

    手机电池和内存都有限，并发跑多个采集没有意义——
    瓶颈在网络不在 CPU，串行反而更稳。
    """
    time.sleep(20)                      # 开机先让服务起来，别一上来就打
    while True:
        st = load_state()
        last = st.get("last", {})
        now = time.time()
        # 挑「最久没跑且已过期」的那个
        due = []
        for t, cfg in TASKS.items():
            prev = last.get(t, {}).get("at", 0)
            wait_left = cfg["every"] - (now - prev)
            if wait_left <= 0:
                due.append((prev, t))
        if due:
            due.sort()
            run_task(due[0][1])
            time.sleep(60)              # 给刚起的任务留出跑的时间
        else:
            time.sleep(30)


# ---------------------------------------------------------------- 数据
SCHEMA = """
CREATE TABLE IF NOT EXISTS entities(
    id TEXT PRIMARY KEY, platform TEXT, type TEXT, title TEXT,
    url TEXT, depth TEXT, payload TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS edges(
    src TEXT, dst TEXT, rel TEXT, ts REAL,
    PRIMARY KEY(src, dst, rel));
CREATE INDEX IF NOT EXISTS ix_edges_rel ON edges(rel);
CREATE INDEX IF NOT EXISTS ix_edges_src ON edges(src, rel);
CREATE INDEX IF NOT EXISTS ix_edges_dst ON edges(dst, rel);
CREATE INDEX IF NOT EXISTS ix_ent_type ON entities(platform, type);
"""


def ensure_db():
    """空库先建表，免得看板第一次打开一片报错。"""
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    c = sqlite3.connect(DB, timeout=20)
    try:
        c.executescript(SCHEMA)
        c.commit()
    finally:
        c.close()


def db_stats():
    if not os.path.exists(DB):
        return {"empty": True}
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
    try:
        out = {"empty": False,
               "size_mb": round(os.path.getsize(DB) / 1048576, 1)}
        out["entities"] = c.execute("SELECT count(*) FROM entities").fetchone()[0]
        out["edges"] = c.execute("SELECT count(*) FROM edges").fetchone()[0]
        out["platforms"] = [
            {"name": p, "n": n} for p, n in c.execute(
                "SELECT platform,count(*) FROM entities "
                "GROUP BY 1 ORDER BY 2 DESC")]
        out["types"] = [
            {"name": t, "n": n} for t, n in c.execute(
                "SELECT type,count(*) FROM entities "
                "GROUP BY 1 ORDER BY 2 DESC LIMIT 12")]
        out["rels"] = [
            {"name": r, "n": n} for r, n in c.execute(
                "SELECT rel,count(*) FROM edges "
                "GROUP BY 1 ORDER BY 2 DESC LIMIT 14")]
        # 今天的增量，用来回答「它到底有没有在干活」
        cut = time.time() - 86400
        out["today_ent"] = c.execute(
            "SELECT count(*) FROM entities WHERE ts > ?", (cut,)).fetchone()[0]
        out["today_edge"] = c.execute(
            "SELECT count(*) FROM edges WHERE ts > ?", (cut,)).fetchone()[0]
        return out
    except sqlite3.Error as e:
        return {"empty": True, "err": str(e)}
    finally:
        c.close()


def db_graph(limit=120, platform=None):
    """给看板的关系图：从一个高连接度的起点做 BFS，保证画出来是连着的。

    踩过两个坑：
      1. 先取度数最高的节点再找边 → 全是 domain/author，彼此不相连，40 个孤点
      2. 从边池里按度数选节点 → story 度数为 1 全被挤掉，还是只剩孤点

    正解是 BFS：从枢纽出发扩 2 跳，扩到谁就画谁。这样图天然连通，
    而且能看出结构 —— 比如「一个域名下面挂着一堆故事」。
    """
    if not os.path.exists(DB):
        return {"nodes": [], "links": []}
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=8)
    try:
        pw, pargs = "", ()
        if platform:
            pw = "JOIN entities en ON en.id = t.id AND en.platform = ?"
            pargs = (platform,)

        # 起点：连接度最高的节点（一个真正的枢纽）
        seed = c.execute("""
            SELECT t.id FROM (
                SELECT src AS id, count(*) cnt FROM edges GROUP BY src
                UNION ALL
                SELECT dst AS id, count(*) cnt FROM edges GROUP BY dst
            ) t %s GROUP BY t.id ORDER BY sum(cnt) DESC LIMIT 1""" % pw,
            pargs).fetchone()
        if not seed:
            return {"nodes": [], "links": []}

        frontier = [seed[0]]
        seen = {seed[0]}
        got_links = []
        budget = limit * 2

        for _hop in range(2):
            if not frontier or len(seen) >= budget:
                break
            ph = ",".join("?" * len(frontier))
            rows = c.execute(
                "SELECT src,dst,rel FROM edges WHERE src IN (%s) OR dst IN (%s) LIMIT 3000"
                % (ph, ph), frontier + frontier).fetchall()
            nxt = []
            for s, d, r in rows:
                got_links.append((s, d, r))
                for x in (s, d):
                    if x not in seen and len(seen) < budget:
                        seen.add(x)
                        nxt.append(x)
            # 下一层优先挑连接度高的，别一头扎进死胡同。
            # 度数直接从已抓到的边里数 —— 少一次查询，也避开 edges 没有 id 列这个坑
            if nxt:
                deg = collections.Counter()
                for s, d, _r in got_links:
                    deg[s] += 1
                    deg[d] += 1
                nxt.sort(key=lambda x: -deg.get(x, 0))
            frontier = nxt[:max(4, limit // 6)]

        ids = list(seen)
        ph3 = ",".join("?" * len(ids))
        info = {r[0]: r for r in c.execute(
            "SELECT id,type,title,platform FROM entities WHERE id IN (%s)" % ph3, ids)}
        # 只留真有实体记录的，脏 ID 不画
        ids = [i for i in ids if i in info]
        keep = set(ids[:limit])
        links = [{"s": s, "t": d, "r": r} for s, d, r in got_links
                 if s in keep and d in keep]
        nodes = [{"id": i, "type": info[i][1],
                  "title": (info[i][2] or i)[:36],
                  "plat": info[i][3]} for i in ids if i in keep]
        return {"nodes": nodes, "links": links}
    except sqlite3.Error:
        return {"nodes": [], "links": []}
    finally:
        c.close()


# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    server_version = "netforce-mobile/1.0"

    def log_message(self, *a):
        pass                                  # 别刷屏

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path

        if p in ("/", "/index.html"):
            fp = os.path.join(WEB, "index.html")
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            return self._send(404, "no web/index.html", "text/plain; charset=utf-8")

        if p == "/api/stats":
            return self._send(200, db_stats())

        if p == "/api/tasks":
            st = load_state()
            last = st.get("last", {})
            out = []
            for t, cfg in TASKS.items():
                run = _running.get(t)
                alive = bool(run and run["proc"].poll() is None)
                # 能不能跑：cmd 是函数时要现算（可能要种子/要浏览器）
                try:
                    avail = cfg["cmd"]() is not None if callable(cfg["cmd"]) else True
                except Exception:
                    avail = False
                out.append({
                    "id": t, "desc": cfg["desc"], "every": cfg["every"],
                    "proxy": cfg["need_proxy"], "running": alive,
                    "available": avail,
                    "browser": cfg.get("need_browser", False),
                    "platform": cfg.get("plat"),
                    "region": cfg.get("region", "intl"),
                    "pid": run["proc"].pid if alive else None,
                    "last_at": last.get(t, {}).get("at"),
                    "last_rc": last.get(t, {}).get("rc"),
                    "last_secs": last.get(t, {}).get("secs"),
                })
            return self._send(200, out)

        if p == "/api/run":
            t = (q.get("task") or [""])[0]
            return self._send(200, run_task(t))

        if p == "/api/log":
            t = (q.get("task") or ["hn"])[0]
            n = int((q.get("n") or ["80"])[0])
            return self._send(200, {"task": t, "text": tail(log_path(t), n)})

        if p == "/api/graph":
            n = int((q.get("limit") or ["120"])[0])
            pl = (q.get("platform") or [None])[0]
            return self._send(200, db_graph(n, pl))

        if p == "/api/health":
            return self._send(200, {"ok": True, "ts": time.time()})

        return self._send(404, {"err": "not found"})


# ---------------------------------------------------------------- main
def main():
    global DB
    ap = argparse.ArgumentParser(description="netforce 手机端 24h 采集节点")
    ap.add_argument("--port", type=int, default=8848)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--once", default="", help="只跑一个任务后退出")
    ap.add_argument("--no-sched", action="store_true", help="不自动轮转")
    ap.add_argument("--db", default="", help="指定库路径（默认 mobile/kb.db）")
    a = ap.parse_args()

    if a.db:
        DB = os.path.abspath(a.db)

    if a.once:
        print(run_task(a.once, wait=True))
        return

    ensure_db()

    if not a.no_sched:
        threading.Thread(target=scheduler, daemon=True).start()

    srv = ThreadingHTTPServer((a.host, a.port), H)
    lan = "?"
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    print("=" * 52)
    print(" netforce mobile 已启动")
    print(" 手机浏览器打开:  http://%s:%d" % (lan, a.port))
    print(" 本机:            http://127.0.0.1:%d" % a.port)
    print(" 自动轮转:        %s" % ("关" if a.no_sched else "开"))
    print("=" * 52)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止。")


if __name__ == "__main__":
    main()
