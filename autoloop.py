# -*- coding: utf-8 -*-
"""netforce / autoloop.py —— 自治升级循环（解析 / 分类 / 爬虫 / 网络）

不依赖对话、不等待指令、不主动停止。每轮自己找活干：

  观察  扫描 _kb 全部数据源的 (size,mtime) 指纹，发现新增或变化
  解析  有变化就增量跑 parse_assets.py / parse_all.py 对应阶段
  评估  算指标：空标签率、平均标签宽度、正文覆盖率、FTS 覆盖率、类目倾斜
  升级  指标触发动作：
          - 有「未命中/待语义复核」→ 网关语义补标，结果沉淀成标签
          - 高频主题缺规则 → 生成候选关键词，验证命中区间后写进 tags_ext.json
          - 标签过宽 → 移除命中率异常高的噪声词，全库重打
  爬虫  从 corpus 的 pending_fetch 队列取小批公开 URL，限速补抓，
        抓回的正文直接解析入库（不写回原 assets 目录，避免干扰采集进程）
  网络  探测直连出口 / 网关 / gitport 的 RTT 与成功率，写 egress_stats，
        后续补抓按历史成败选路；失败域名沉淀为可达性知识
  报告  每轮写 metrics.jsonl + EVOLVE_LOG.md（滚动） + heartbeat + 互保拉活

安全护栏（硬约束，不因「自治」放宽）：
  * 只追加 / 只更新自己建的表，绝不删除任何已有文件或目录
  * 不停止、不重启、不干扰 resource_finder 等任何正在跑的进程
  * 补抓只走系统直连（不带 -x、不碰用户自开的第三方代理）、只抓公开 http(s)，
    跳过 localhost / 私网 / 账号类域名，每轮限量、单请求超时
  * 网关调用限量并失败退避；密钥从 ~/.fleet/zengate.key 读，代码里零默认值

用法：
    pythonw autoloop.py            # 常驻（由 autoloop_watchdog.py 拉起 / 互保）
    python  autoloop.py --once     # 只跑一轮，便于诊断
    python  autoloop.py --stats    # 看最近指标
"""
from __future__ import annotations

import argparse
import ctypes
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.error

import headless
import urllib.request
from collections import Counter
from datetime import datetime

import _bulk_lock

HERE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(HERE, "_kb")
EV = os.path.join(KB, "evolve")
CORPUS = os.path.join(KB, "corpus.db")
TAXONOMY = os.path.join(KB, "tags_ext.json")
PARSE_ALL = os.path.join(HERE, "parse_all.py")
PARSE_ASSETS = os.path.join(HERE, "parse_assets.py")
STATE = os.path.join(EV, "state.json")
METRICS = os.path.join(EV, "metrics.jsonl")
LOGMD = os.path.join(EV, "EVOLVE_LOG.md")
HEARTBEAT = os.path.join(EV, "heartbeat")
WATCHDOG_HB = os.path.join(EV, "watchdog.heartbeat")
PIDFILE = os.path.join(EV, "evolve.pid")
BULK_LOCK = os.path.join(EV, "BULK_IMPORT")
WATCHDOG_SCRIPT = os.path.join(HERE, "autoloop_watchdog.py")
OCR_SCRIPT = os.path.join(HERE, "ocr_scanned.py")
# 默认走本地 llama-server（不限量、不排队、不花钱）；要回云端网关就设
# NETFORCE_LLM_BASE=http://127.0.0.1:8787/v1 并保留 keyfile。
GATEWAY = os.environ.get("NETFORCE_LLM_BASE", "http://127.0.0.1:8080/v1")
LLM_MODEL = os.environ.get("NETFORCE_LLM_MODEL", "gemma4-e4b")
# 本地模型不限流：单条 3 秒级，一轮可以吞几十条；云端网关是思考型模型
# （20~60s/条），一轮只能给 6 条，否则一轮跑不完。两者都可用环境变量覆盖。
LLM_LOCAL = any(h in GATEWAY for h in ("127.0.0.1:8080", "localhost:8080"))
KEYFILE = os.path.join(os.path.expanduser("~"), ".fleet", "zengate.key")

FETCH_MAX_PER_ROUND = 12
FETCH_INTERVAL = 1.2
FETCH_TIMEOUT = 8
FETCH_MAX_BYTES = 2 * 1024 * 1024
LLM_MAX_PER_ROUND = int(os.environ.get(
    "NETFORCE_LLM_PER_ROUND", "40" if LLM_LOCAL else "6"))
IDLE_SLEEP = 90
BUSY_SLEEP = 10

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) netforce-autoloop/1.0"
SKIP_HOST = re.compile(
    r"(^|\.)(localhost|local|internal|test|invalid)$|"
    r"^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)|"
    r"(douyin|weibo|weixin|qq\.com|tiktok|xiaohongshu|zhihu|bilibili)\.com$",
    re.I)
TEXT_EXT = re.compile(
    r"\.(pdf|md|txt|rst|json|jsonl|xml|csv|tsv|html?|srt|vtt|tex|bib|epub|yaml|yml)(\?|$)",
    re.I)

os.makedirs(EV, exist_ok=True)


# ------------------------------------------------------------------ 基础
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def say(msg):
    line = "[%s] %s" % (now(), msg)
    print(line, flush=True)
    try:
        with open(os.path.join(EV, "autoloop.stdout.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"round": 0, "fingerprints": {}, "gateway_fail": 0,
                "last_actions": [], "total_fetched": 0, "total_llm": 0,
                "new_tags": [], "started": now()}


def save_state(s):
    s["round"] = int(s.get("round", 0)) + 1
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    with open(HEARTBEAT, "w", encoding="utf-8") as f:
        f.write("%s|%s" % (str(time.time()), s["round"]))
    with open(PIDFILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def db():
    c = sqlite3.connect(CORPUS, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS evolve_actions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, round INTEGER,
        action TEXT, detail TEXT, n INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS egress_stats(
        host TEXT PRIMARY KEY, ok INTEGER, fail INTEGER,
        avg_ms REAL, last_ts TEXT, last_err TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS semantic_tags(
        uid TEXT PRIMARY KEY, ts TEXT, topics TEXT, raw TEXT)""")
    c.commit()
    return c


def action(c, name, detail, n=0):
    c.execute("INSERT INTO evolve_actions(ts,round,action,detail,n) VALUES(?,?,?,?,?)",
              (now(), load_state().get("round", 0), name, str(detail)[:600], n))
    c.commit()


# ------------------------------------------------------------------ 观察
DATA_GLOBS = [
    "records/rec-*.jsonl", "*.db", "*.jsonl", "assets/manifest.jsonl",
    "_dy_api/*.json", "sitemap_cache/*.json", "crt_*.json",
]


def observe(state):
    old = state.get("fingerprints", {})
    cur = {}
    for g in DATA_GLOBS:
        for p in glob.glob(os.path.join(KB, g)):
            if not os.path.isfile(p) or p.startswith(EV):
                continue
            if os.path.basename(p) == "corpus.db":
                continue          # 本循环自己写的库，不算外部新数据
            try:
                st = os.stat(p)
                cur[os.path.relpath(p, KB)] = "%d:%d" % (st.st_size, int(st.st_mtime))
            except OSError:
                pass
    changed = [k for k, v in cur.items() if old.get(k) != v]
    state["fingerprints"] = cur
    return changed


def run_py(script, args, timeout=600):
    cmd = [sys.executable, script] + args
    try:
        r = headless.run(cmd, cwd=HERE, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "")[-2000:], (r.stderr or "")[-800:]
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)[:200]


# ------------------------------------------------------------------ 解析（增量）
def ingest(c, state, changed):
    acts = []
    rec_changed = any(k.startswith("records/") for k in changed)
    asset_changed = any("manifest" in k or k.startswith("assets/") for k in changed)
    asset_db_changed = "asset_index.db" in changed
    new_assets = []
    adir = os.path.join(KB, "assets")
    if os.path.isdir(adir):
        for root, _, files in os.walk(adir):
            for fn in files:
                p = os.path.join(root, fn)
                try:
                    if os.path.getmtime(p) > time.time() - 3600:
                        new_assets.append(p)
                except OSError:
                    pass
    if asset_changed or new_assets:
        rc, out, err = run_py(PARSE_ASSETS, [], timeout=600)
        acts.append("parse_assets rc=%d %s" % (rc, err[:120]))
        say("  增量资源解析：%d 个近期文件，rc=%d" % (len(new_assets), rc))
    if asset_changed or new_assets or asset_db_changed:
        # OCR 写回 asset_index.db 时，把新正文同步进统一库
        run_py(PARSE_ALL, ["--stage", "asset_docs", "--force"], timeout=600)
        acts.append("parse_all --stage asset_docs（含 OCR 新正文）")
    if rec_changed:
        rc, out, err = run_py(PARSE_ALL, ["--stage", "records", "--force"], timeout=900)
        acts.append("parse_all --stage records rc=%d" % rc)
        say("  增量抓取记录解析 rc=%d %s" % (rc, err[:120]))
    for a in acts:
        action(c, "ingest", a)
    return acts


# ------------------------------------------------------------------ 评估
def metrics(c):
    q = lambda s: c.execute(s).fetchone()[0]
    total = q("SELECT COUNT(*) FROM corpus")
    if not total:
        return {"total": 0, "ts": now()}
    empty_tp = q("SELECT COUNT(*) FROM corpus WHERE topics IS NULL OR topics IN ('','[]')")
    fallback = q("SELECT COUNT(*) FROM corpus WHERE topics LIKE '%未命中/待语义复核%'")
    withtext = q("SELECT COUNT(*) FROM corpus WHERE text_len>200")
    fts = q("SELECT COUNT(*) FROM corpus_fts")
    pending = q("SELECT COUNT(*) FROM corpus WHERE parse_status='pending_fetch'")
    widths = [len(json.loads(t or "[]")) for (t,) in c.execute("SELECT topics FROM corpus")]
    cats = Counter(r[0] for r in c.execute("SELECT category FROM corpus"))
    top_share = (cats.most_common(1)[0][1] / total) if cats else 0
    chars = q("SELECT COALESCE(SUM(text_len),0) FROM corpus")
    return {
        "ts": now(), "total": total, "empty_topics": empty_tp,
        "fallback": fallback, "fallback_rate": round(fallback / total, 4),
        "with_text": withtext, "text_rate": round(withtext / total, 4),
        "fts": fts, "fts_gap": withtext - fts, "chars": chars,
        "avg_width": round(sum(widths) / max(1, len(widths)), 2),
        "wide_gt5": sum(1 for w in widths if w > 5),
        "top_category": cats.most_common(1)[0][0] if cats else "",
        "top_share": round(top_share, 4), "pending_fetch": pending,
    }


def record_metrics(m):
    with open(METRICS, "a", encoding="utf-8") as f:
        f.write(json.dumps(m, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ 网关
def gateway_key():
    try:
        with open(KEYFILE, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return os.environ.get("ZENGATE_KEY", "")


def llm(prompt, max_tokens=1200, timeout=60):
    key = gateway_key()
    local = any(h in GATEWAY for h in ("127.0.0.1:8080", "localhost:8080"))
    if not key and not local:
        return None, "no key"
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": "只输出 JSON，不要解释，不要思考过程。"},
                      {"role": "user", "content": prompt}],
        "max_tokens": max_tokens, "temperature": 0.1,
    }
    # 本地 Gemma 4 是思考模型，不开这个会把 token 全花在 reasoning 上、content 为空
    if LLM_LOCAL:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(
        GATEWAY + "/chat/completions", data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        msg = d["choices"][0]["message"]
        txt = (msg.get("content") or "").strip()
        if not txt:
            # aggregate/auto 是思考型模型：max_tokens 给少了会 finish_reason=length、
            # content 全空，答案埋在 reasoning_content 里。取思维链最后一段 JSON
            # （通常是它复述的最终答案），不要把整段思维过程当结果返回。
            rs = (msg.get("reasoning_content") or "").strip()
            found = re.findall(r"\[[\s\S]*?\]|\{[\s\S]*?\}", rs)
            txt = found[-1] if found else rs[-400:]
        return txt, None
    except Exception as e:
        return None, str(e)[:160]


# 🔴 11-12 主人定：不再有固定标签集。预设 27 类会让模型在跨领域内容上
#    一律退到「其他」，等于把判断力废掉；让它自己读内容、自己造具体短语。
GENERIC = {"其他", "综合", "杂项", "未分类", "未知", "资讯", "技术",
           "文章", "网页", "内容", "信息", "一般", "其它"}


def _llm_reachable():
    """3 秒探活：端点不在就别进补标循环空等 timeout。"""
    base = GATEWAY.rstrip("/")
    try:
        with urllib.request.urlopen(base + "/models", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def semantic_fill(c, state, m):
    """给「兜底标签」的少量样本做语义分类，结果直接写回 corpus。"""
    if m.get("fallback", 0) == 0:
        return 0
    # 🔴 gateway_fail 不能跨轮累积：旧逻辑把它存进 state 持久化，模型/网关
    #    临时不在就累到 >3，之后永远不再尝试补标（2026-11-12 修复）。
    #    改成每轮重新计数 + 先探活，不可达直接跳过，不浪费 timeout。
    state["gateway_fail"] = 0
    if not _llm_reachable():
        say("  LLM 端点不可达（%s），本轮跳过语义补标" % GATEWAY)
        return 0
    rows = c.execute(
        "SELECT uid,host,url,title,substr(text,1,1400) FROM corpus "
        "WHERE topics LIKE '%未命中/待语义复核%' AND text_len>400 "
        "AND source NOT IN ('corpus_chunk','corpus_file') "
        "AND uid NOT IN (SELECT uid FROM semantic_tags) "
        "ORDER BY text_len DESC LIMIT ?", (LLM_MAX_PER_ROUND,)).fetchall()
    if not rows:
        return 0
    done = 0
    for uid, host, url, title, text in rows:
        prompt = ("读下面这个网页，用 1-3 个最贴切的中文短语概括它的主题。\n"
                  "自己判断内容，不要从任何预设列表里挑，也不要用「其他」「综合」这类兜底词；"
                  "短语要具体、能区分这篇内容（例如「Rust 异步运行时调优」而不是「编程」）。\n\n"
                  "标题：%s\n来源：%s\n正文摘录：%s\n\n"
                  "只输出 JSON 数组，每个短语不超过 14 个字，不要解释。" %
                  ((title or "")[:120], (host or "")[:80],
                   re.sub(r"\s+", " ", text or "")[:900]))
        out, err = llm(prompt, 200 if LLM_LOCAL else 1200)
        if err:
            state["gateway_fail"] = state.get("gateway_fail", 0) + 1
            say("  网关语义补标失败：%s" % err)
            break
        state["gateway_fail"] = 0
        labels = []
        try:
            found = re.findall(r"\[[\s\S]*?\]", out or "")
            arr = json.loads(found[-1]) if found else []
            for x in arr:
                s = str(x).strip().strip("\"'、,，")
                if s and len(s) <= 24 and s not in GENERIC and s not in labels:
                    labels.append(s)
                if len(labels) >= 3:
                    break
        except Exception:
            pass
        c.execute("INSERT OR REPLACE INTO semantic_tags VALUES(?,?,?,?)",
                  (uid, now(), json.dumps(labels, ensure_ascii=False), (out or "")[:500]))
        if labels:
            # category 是旧的粗分类维度，保持不动；开放标签只写 topics
            c.execute("UPDATE corpus SET topics=? WHERE uid=?",
                      (json.dumps(labels, ensure_ascii=False), uid))
        done += 1
        state["total_llm"] = state.get("total_llm", 0) + 1
        c.commit()          # 每次网关调用后立即释放写锁，绝不把事务带进下一次网络等待
        if not LLM_LOCAL:
            time.sleep(0.4)     # 云端网关需要限速；本地 llama-server 不必
    c.commit()
    action(c, "semantic_fill", "网关补标 %d 条" % done, done)
    say("  网关语义补标 %d 条" % done)
    return done


def mine_tags(c, state, m):
    """从高价值正文里挖高频词组，验证命中区间后新增标签规则。"""
    try:
        with open(TAXONOMY, encoding="utf-8") as f:
            tax = json.load(f)
    except Exception:
        return 0
    existing = set()
    for group in ("extra_topics", "extra_forms"):
        for r in tax.get(group, []):
            existing.add(r["name"])
            existing.update(str(k).lower() for k in r.get("keywords", []))
    cnt = Counter()
    stop = set("的 了 和 是 在 有 与 及 中 为 对 从 这 那 我们 你们 他们 一个 可以 没有 什么 怎么 "
               "the and for with that this from are was were not you your our their into about".split())
    sample = c.execute(
        "SELECT substr(text,1,3000),title FROM corpus WHERE text_len>1500 "
        "AND source NOT IN ('corpus_chunk','corpus_file') "
        "AND topics LIKE '%未命中/待语义复核%' LIMIT 400").fetchall()
    for text, title in sample:
        s = ((title or "") + " " + (text or "")).lower()
        for w in re.findall(r"[\u4e00-\u9fff]{2,6}|[a-z][a-z0-9\-]{4,18}", s):
            if w not in stop and w not in existing and not w.isdigit():
                cnt[w] += 1
    cands = [(w, n) for w, n in cnt.most_common(60) if n >= 8]
    if not cands:
        return 0
    total = max(1, m.get("total", 1))
    accepted = []
    for w, n in cands[:20]:
        try:
            hit = c.execute("SELECT COUNT(*) FROM corpus_fts WHERE corpus_fts MATCH ?",
                            (w,)).fetchone()[0]
        except Exception:
            continue
        rate = hit / total
        if 0.005 <= rate <= 0.25 and hit >= 20:
            accepted.append((w, hit, round(rate, 4)))
    if not accepted:
        return 0
    prompt = ("下面是文档库中反复出现但尚无标签规则的词。判断哪些能作为「独立主题标签」，"
              "输出 JSON 数组，元素格式 {\"word\":\"词\",\"label\":\"主题名\"}；"
              "不能构成主题的不要输出。词：%s" %
              "、".join(w for w, _, _ in accepted[:10]))
    out, err = llm(prompt, 400)
    new = []
    if not err:
        try:
            mm = re.search(r"\[[\s\S]*\]", out or "")
            arr = json.loads(mm.group(0)) if mm else []
            for o in arr:
                if isinstance(o, dict) and o.get("word") and o.get("label"):
                    if o["label"] not in existing:
                        new.append({"name": o["label"], "keywords": [o["word"]],
                                   "domain_hint": [], "origin": "autoloop:%s" % now()[:10]})
        except Exception:
            pass
    if not new:
        with open(os.path.join(EV, "tag_candidates.jsonl"), "a", encoding="utf-8") as f:
            for w, hit, rate in accepted:
                f.write(json.dumps({"ts": now(), "word": w, "hits": hit,
                                    "rate": rate}, ensure_ascii=False) + "\n")
        action(c, "mine_tags", "候选 %d 个待确认" % len(accepted), len(accepted))
        return 0
    for r in new:
        tax.setdefault("extra_topics", []).append(r)
    with open(TAXONOMY, "w", encoding="utf-8") as f:
        json.dump(tax, f, ensure_ascii=False, indent=1)
    state["new_tags"] = (state.get("new_tags", []) + [r["name"] for r in new])[-50:]
    run_py(PARSE_ALL, ["--retag"], timeout=900)
    action(c, "mine_tags", "新增规则 %s，已全库重打" % [r["name"] for r in new], len(new))
    say("  自动扩标签：新增 %d 条规则并全库重打" % len(new))
    return len(new)


def tighten_tags(c, state, m):
    """标签过宽时，移除命中率异常高的噪声词，全库重打。"""
    if m.get("wide_gt5", 0) < max(50, m.get("total", 0) * 0.02):
        return 0
    try:
        with open(TAXONOMY, encoding="utf-8") as f:
            tax = json.load(f)
    except Exception:
        return 0
    total = max(1, m.get("total", 1))
    removed = 0
    for group in ("extra_topics", "extra_forms"):
        for rule in tax.get(group, []):
            keep = []
            for kw in rule.get("keywords", []):
                try:
                    hit = c.execute("SELECT COUNT(*) FROM corpus_fts WHERE corpus_fts MATCH ?",
                                    (str(kw),)).fetchone()[0]
                except Exception:
                    keep.append(kw)
                    continue
                if hit / total > 0.55 and len(str(kw)) < 4:
                    removed += 1
                else:
                    keep.append(kw)
            rule["keywords"] = keep
    if removed:
        with open(TAXONOMY, "w", encoding="utf-8") as f:
            json.dump(tax, f, ensure_ascii=False, indent=1)
        run_py(PARSE_ALL, ["--retag"], timeout=900)
        action(c, "tighten_tags", "移除 %d 个过宽噪声词并重打" % removed, removed)
        say("  标签收紧：移除 %d 个过宽噪声词" % removed)
    return removed


# ------------------------------------------------------------------ 爬虫：限速补抓
def fetchable(c, limit):
    return c.execute(
        "SELECT uid,url,host,source,kind FROM corpus "
        "WHERE parse_status='pending_fetch' AND url LIKE 'http%' "
        "AND source IN ('urls/assets','urls/reclaim_targets','urls/site_index',"
        "               'urls/external_seeds','urls/discovered',"
        "               'root/resources','root/classified','root/visited_db',"
        "               'root/visited_json','root/harvested','root/misc_json') "
        "AND uid NOT IN (SELECT uid FROM semantic_tags WHERE raw='fetch_failed') "
        "ORDER BY RANDOM() LIMIT ?", (limit * 3,)).fetchall()


def fetch_one(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "text/html,application/xhtml+xml,application/json,*/*"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        ctype = r.headers.get("Content-Type", "")
        if not re.search(r"text|json|xml|pdf|javascript|octet-stream", ctype, re.I):
            return None, "skip:%s" % ctype[:40]
        data = r.read(FETCH_MAX_BYTES + 1)
        if len(data) > FETCH_MAX_BYTES:
            data = data[:FETCH_MAX_BYTES]
        return {"text": data.decode("utf-8", "ignore"),
                "ctype": ctype, "ms": (time.time() - t0) * 1000}, None


def html_text(s):
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(s, "lxml")
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        return (soup.title.get_text(" ", strip=True) if soup.title else ""), \
            soup.get_text("\n", strip=True)
    except Exception:
        return "", re.sub(r"<[^>]+>", " ", s)


COLS = ["uid", "source", "kind", "url", "host", "title", "descr", "text",
        "text_len", "lang", "category", "forms", "topics", "subtopics",
        "extra_topics", "quality", "value", "parse_status", "evidence",
        "meta", "ts", "parse_ver"]


def refetch(c, state, m):
    jobs = [j for j in fetchable(c, FETCH_MAX_PER_ROUND) if j[3] in
            ("urls/assets", "urls/external_seeds", "urls/site_index",
             "urls/reclaim_targets", "urls/discovered")]
    chosen = []
    for j in jobs:
        uid, url, host = j[0], j[1], j[2]
        if SKIP_HOST.search(host or ""):
            continue
        if not re.match(r"^https?://", url or "", re.I):
            continue
        chosen.append((uid, url, host))
        if len(chosen) >= FETCH_MAX_PER_ROUND:
            break
    if not chosen:
        return 0, 0
    sys.path.insert(0, HERE)
    from parse_all import make_row, load_taxonomy, build_host_profile
    tax = load_taxonomy()
    prof = build_host_profile(c)
    ok = fail = 0
    for uid, url, host in chosen:
        try:
            res, err = fetch_one(url)
            if err or not res or not res["text"].strip():
                raise RuntimeError(err or "empty body")
            title, text = html_text(res["text"])
            text = re.sub(r"\s+", " ", text)[:30000]
            meta = {"autoloop_fetch": True, "ctype": res["ctype"],
                    "rtt_ms": round(res["ms"]), "ts": now()}
            row = make_row("autoloop_fetch", "page", uid, url, host,
                           title or host, "", text, meta, "ok", tax, prof)
            c.execute("INSERT OR REPLACE INTO corpus(%s) VALUES(%s)" %
                      (",".join(COLS), ",".join("?" for _ in COLS)),
                      [row.get(k, "") for k in COLS])
            c.execute("DELETE FROM corpus_fts WHERE uid=?", (uid,))
            c.execute("INSERT INTO corpus_fts(uid,host,title,topics,text) VALUES(?,?,?,?,?)",
                      (uid, host, row["title"], row["topics"], row["text"]))
            c.execute("INSERT INTO egress_stats VALUES(?,?,?,?,?,?) "
                      "ON CONFLICT(host) DO UPDATE SET ok=ok+1, "
                      "avg_ms=(COALESCE(avg_ms,0)*ok+?)/(ok+1), last_ts=excluded.last_ts",
                      (host, 1, 0, res["ms"], now(), "", res["ms"]))
            ok += 1
        except Exception as e:
            fail += 1
            try:
                c.execute("INSERT OR REPLACE INTO semantic_tags VALUES(?,?,?,?)",
                          (uid, now(), "[]", "fetch_failed"))
                c.execute("INSERT INTO egress_stats VALUES(?,?,?,?,?,?) "
                          "ON CONFLICT(host) DO UPDATE SET fail=fail+1, "
                          "last_ts=excluded.last_ts, last_err=excluded.last_err",
                          (host, 0, 1, 0.0, now(), str(e)[:120]))
            except Exception:
                pass
        c.commit()          # 每个请求后立即提交，下一个请求的网络等待期不持锁
        time.sleep(FETCH_INTERVAL)
    c.commit()
    state["total_fetched"] = state.get("total_fetched", 0) + ok
    action(c, "refetch", "补抓成功 %d / 失败 %d" % (ok, fail), ok)
    say("  限速补抓：成功 %d / 失败 %d" % (ok, fail))
    return ok, fail


# ------------------------------------------------------------------ 渐进消化语料
CHUNK_CHARS = 6000
CHUNKS_PER_ROUND = 600
MIN_FREE_GB = 4.0


def digest_corpus(c, state):
    """每轮吃掉一批语料分块，直到 674 MB 全部可检索。

    这是「停不下来」的真正工作量来源之一：即便没有新抓取数据，
    循环也会一轮一轮把这批长文本切成可检索块消化掉。
    """
    try:
        c.execute("SELECT COUNT(*) FROM corpus_digest").fetchone()
    except sqlite3.OperationalError:
        return 0
    free = shutil.disk_usage(KB).free / (1024 ** 3)
    if free < MIN_FREE_GB:
        say("  磁盘可用 %.1f GB < %.1f GB，本轮跳过语料消化" % (free, MIN_FREE_GB))
        return 0
    row = c.execute("SELECT file,size,offset,chunks FROM corpus_digest "
                    "WHERE done=0 ORDER BY chunks ASC LIMIT 1").fetchone()
    if not row:
        return 0
    path, size, off, done_chunks = row
    if not os.path.exists(path):
        c.execute("UPDATE corpus_digest SET done=1 WHERE file=?", (path,))
        c.commit()
        return 0
    sys.path.insert(0, HERE)
    from parse_all import make_row, write_rows, load_taxonomy
    tax = load_taxonomy()
    raw_bytes = CHUNK_CHARS * 3 * CHUNKS_PER_ROUND
    with open(path, "rb") as f:
        f.seek(int(off))
        buf = f.read(raw_bytes)
    new_off = int(off) + len(buf)
    finished = (new_off >= int(size)) or not buf
    text = buf.decode("utf-8", "ignore")
    if len(text) < 50:
        text = buf.decode("gb18030", "ignore")
    fn = os.path.basename(path)
    base_no = int(off) // (CHUNK_CHARS * 3)
    rows = []
    for i in range(0, len(text), CHUNK_CHARS):
        chunk = text[i:i + CHUNK_CHARS]
        if len(chunk) < 200:
            continue
        no = base_no + i // CHUNK_CHARS
        r = make_row("corpus_chunk", "corpus_chunk", "%s:%d" % (fn, no),
                     "file:///" + path.replace("\\", "/"), "local-corpus",
                     "%s 分块 %d" % (fn, no), "", chunk,
                     {"full_path": path, "chunk_no": no, "byte_offset": int(off) + i},
                     "ok", tax, None,
                     {"category": "小说/训练语料",
                      "topics": json.dumps(["小说/文学语料"], ensure_ascii=False),
                      "forms": json.dumps(["语料/长文本"], ensure_ascii=False)})
        rows.append(r)
    if rows:
        write_rows(c, rows)
    c.execute("UPDATE corpus_digest SET offset=?,done=?,chunks=chunks+?,ts=? WHERE file=?",
              (new_off, 1 if finished else 0, len(rows), now(), path))
    c.commit()
    left = c.execute("SELECT COUNT(*) FROM corpus_digest WHERE done=0").fetchone()[0]
    action(c, "digest_corpus", "%s 新增 %d 块（%.1f MB），剩余 %d 个文件" %
           (fn, len(rows), len(buf) / 1048576, left), len(rows))
    say("  语料消化：%s +%d 块（%.1f MB），剩余 %d 个文件" %
        (fn[:32], len(rows), len(buf) / 1048576, left))
    return len(rows)


def ocr_round(state):
    """每轮限时推进一批扫描页；OCR 写回 asset_index.db 后由下一轮 observe 同步进 corpus。"""
    if not os.path.exists(OCR_SCRIPT):
        return 0
    rc, out, err = run_py(OCR_SCRIPT, ["--pages", "40", "--max-seconds", "60"],
                          timeout=150)
    m = re.search(r"本轮\s+(\d+)\s+页", out or "")
    n = int(m.group(1)) if m else 0
    if n:
        state["total_ocr_pages"] = state.get("total_ocr_pages", 0) + n
        say("  扫描件 OCR +%d 页（累计 %d）" % (n, state["total_ocr_pages"]))
    elif rc != 0:
        say("  OCR 本轮无进展 rc=%d %s" % (rc, err[:120]))
    return n


# ------------------------------------------------------------------ 网络
def probe_network(c):
    targets = [
        ("gateway-8787", "http://127.0.0.1:8787/health"),
        ("gitport-7810", "http://127.0.0.1:7810/"),
        ("direct-github", "https://raw.githubusercontent.com/"),
        ("direct-example", "https://example.com/"),
    ]
    out = []
    for name, url in targets:
        t0 = time.time()
        code, err = 0, ""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=4) as r:
                code = r.status
                r.read(128)
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:
            err = type(e).__name__
        out.append({"name": name, "code": code, "ms": round((time.time() - t0) * 1000),
                    "err": err})
    # 探测期间不做任何数据库写：全部请求结束后一次性落库
    for p in out:
        c.execute("INSERT INTO egress_stats VALUES(?,?,?,?,?,?) "
                  "ON CONFLICT(host) DO UPDATE SET ok=ok+?, fail=fail+?, "
                  "last_ts=excluded.last_ts, last_err=excluded.last_err",
                  (p["name"], 1 if p["code"] else 0, 0 if p["code"] else 1,
                   p["ms"], now(), p["err"],
                   1 if p["code"] else 0, 0 if p["code"] else 1))
    c.commit()
    with open(os.path.join(EV, "network.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now(), "probes": out}, ensure_ascii=False) + "\n")
    return out


def route_hint(c):
    rows = c.execute("SELECT host,ok,fail,avg_ms,last_err FROM egress_stats "
                     "WHERE ok+fail>=1 ORDER BY (ok*1.0/(ok+fail)) ASC, fail DESC "
                     "LIMIT 60").fetchall()
    with open(os.path.join(EV, "ROUTE_HINT.md"), "w", encoding="utf-8") as f:
        f.write("# 出口路由知识（autoloop 自动生成）\n\n")
        f.write("> 由实际补抓与探测沉淀，供采集侧选路参考；不修改任何网络配置、不启用任何第三方代理。\n\n")
        f.write("| 目标 | 成功 | 失败 | 平均 ms | 最后错误 |\n|---|---|---|---|---|\n")
        for h, ok, fail, ms, err in rows:
            f.write("| %s | %s | %s | %s | %s |\n" %
                    (h, ok or 0, fail or 0, round(ms or 0), str(err or "")[:60].replace("|", "/")))
    return len(rows)


def probe_models():
    out = []
    for port in (8080, 8090, 11434, 8787):
        code = 0
        try:
            req = urllib.request.Request("http://127.0.0.1:%d/v1/models" % port,
                                         headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=2) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception:
            pass
        out.append((port, code))
    return out


# ------------------------------------------------------------------ 报告
def write_report(state, m, acts, net, models, n_route):
    lines = [
        "# autoloop 自治升级状态", "",
        "> 本文件由常驻进程自动重写，不需要人看；会话结束它也会继续跑。", "",
        "## 当前语料", "",
        "| 指标 | 值 |", "|---|---|",
        "| 总条数 | %s |" % format(m.get("total", 0), ","),
        "| 入库正文字符 | %s |" % format(m.get("chars", 0), ","),
        "| 有正文 / FTS | %s / %s |" % (format(m.get("with_text", 0), ","),
                                        format(m.get("fts", 0), ",")),
        "| 空主题 / 兜底标签 | %s / %s（%.2f%%） |" %
        (format(m.get("empty_topics", 0), ","), format(m.get("fallback", 0), ","),
         m.get("fallback_rate", 0) * 100),
        "| 平均标签宽度 | %s（>5 的 %s 条） |" %
        (m.get("avg_width", 0), format(m.get("wide_gt5", 0), ",")),
        "| 最大类目占比 | %s %s |" % (m.get("top_category", ""), m.get("top_share", 0)),
        "| 待抓取队列 | %s |" % format(m.get("pending_fetch", 0), ","),
        "| 累计补抓 / 网关调用 | %s / %s |" %
        (state.get("total_fetched", 0), state.get("total_llm", 0)),
        "| 自动新增标签 | %s |" % ("、".join(state.get("new_tags", [])[-8:]) or "（暂无）"),
        "", "## 出口探测", "",
        "| 目标 | 状态 | 耗时 ms |", "|---|---|---|",
    ]
    for p in net:
        lines.append("| %s | %s %s | %s |" %
                     (p["name"], p["code"], p["err"], p["ms"]))
    lines += ["", "## 模型服务", "",
              "| 端口 | 状态 |", "|---|---|"]
    for port, code in models:
        lines.append("| %s | %s |" % (port, code or "未监听"))
    lines += ["", "## 本轮动作", ""]
    for a in acts:
        lines.append("- %s" % a)
    lines += ["", "## 路由知识", "",
              "已沉淀 %d 个目标的出口成败，见 `ROUTE_HINT.md`。" % n_route,
              "", "---", "",
              "第 %s 轮 · %s · 下一轮自动继续：有新数据就解析、指标变差就调标签、"
              "队列有活就补抓。" % (state.get("round", 0), now()), ""]
    with open(LOGMD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ------------------------------------------------------------------ 互保
def spawn_detached(script):
    flags = 0
    for n in ("CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, n, 0)
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = os.path.join(os.path.dirname(exe), "pythonw.exe")
    subprocess.Popen([exe, script], cwd=HERE, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, startupinfo=si)


def ensure_watchdog(state):
    try:
        age = time.time() - float(open(WATCHDOG_HB, encoding="utf-8").read().strip())
    except Exception:
        age = 9999
    if age < 120:
        return
    try:
        spawn_detached(WATCHDOG_SCRIPT)
        say("  看门心跳停了 %.0fs，已重新拉起 autoloop_watchdog" % age)
    except Exception as e:
        say("  拉起看门失败：%s" % str(e)[:120])


# ------------------------------------------------------------------ 一轮
def one_round(state):
    t0 = time.time()
    acts = []
    c = None
    # 批量全量导入进行时，本循环只做只读观察，不抢主库写锁；
    # 导入方删掉 BULK_IMPORT 文件后，下一轮自动恢复全部写动作，无需重启。
    bulk = _bulk_lock.locked()      # 带属主判活：导入进程已死会自动清僵尸锁
    if bulk:
        acts.append("批量导入进行中：本轮只读观察，写动作自动顺延")
    # 每轮开头就刷新心跳：单轮可能跑数分钟，不能等结束才报活
    try:
        with open(HEARTBEAT, "w", encoding="utf-8") as f:
            f.write("%s|%s" % (str(time.time()), state.get("round", 0)))
        with open(PIDFILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    try:
        c = db()
        first = not state.get("fingerprints")
        changed = observe(state)
        if first:
            acts.append("首轮建立 %d 个数据源基线，不重复解析已有语料" % len(changed))
            say("  首轮：建立 %d 个数据源基线" % len(changed))
            changed = []
        if changed and not bulk:
            acts.append("发现 %d 个数据源变化：%s" % (len(changed), ", ".join(changed[:4])))
            say("  数据变化 %d 个：%s" % (len(changed), ", ".join(changed[:3])))
            acts += ingest(c, state, changed)
        m = metrics(c)
        record_metrics(m)
        if m.get("total", 0) and not bulk:
            acts.append("指标：%s 条 / 正文 %s 字符 / 兜底 %s / 均宽 %s" %
                        (format(m["total"], ","), format(m["chars"], ","),
                         m.get("fallback", 0), m.get("avg_width", 0)))
            n = semantic_fill(c, state, m)
            if n:
                acts.append("网关语义补标 %d 条" % n)
            n = tighten_tags(c, state, m)
            if n:
                acts.append("移除过宽噪声词 %d 个" % n)
            n = mine_tags(c, state, m)
            if n:
                acts.append("自动新增标签规则 %d 条" % n)
            ok, fail = refetch(c, state, m)
            if ok or fail:
                acts.append("限速补抓：成功 %d / 失败 %d" % (ok, fail))
            n = digest_corpus(c, state)
            if n:
                acts.append("语料渐进消化 +%d 块" % n)
            n = ocr_round(state)
            if n:
                acts.append("扫描件 OCR +%d 页" % n)
            if m.get("fts_gap", 0) > 0:
                gap = c.execute(
                    "SELECT uid,host,title,topics,text FROM corpus WHERE text_len>200 "
                    "AND uid NOT IN (SELECT uid FROM corpus_fts) LIMIT 2000").fetchall()
                if gap:
                    c.executemany("INSERT INTO corpus_fts(uid,host,title,topics,text) "
                                  "VALUES(?,?,?,?,?)", gap)
                    c.commit()
                    acts.append("补齐 FTS 缺口 %d 条" % len(gap))
        net = probe_network(c)
        models = probe_models()
        n_route = route_hint(c)
        ensure_watchdog(state)
        write_report(state, m, acts, net, models, n_route)
    except Exception:
        say("  本轮异常：\n%s" % traceback.format_exc()[-1400:])
    finally:
        try:
            if c:
                c.close()
        except Exception:
            pass
    secs = time.time() - t0
    state["last_actions"] = acts[-12:]
    state["last_secs"] = round(secs, 1)
    save_state(state)
    say("第 %d 轮完成，%.1fs，动作 %d 项" % (state["round"], secs, len(acts)))
    return acts, secs


_MUTEX = {}


def single_instance():
    """命名互斥体：误拉起的第二个自治循环立刻自行退出，不重复写库、不抢锁。"""
    try:
        _MUTEX["loop"] = ctypes.windll.kernel32.CreateMutexW(
            None, False, "Global\\netforce_autoloop_v1")
        return ctypes.windll.kernel32.GetLastError() != 183
    except Exception:
        return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    if a.stats:
        try:
            for l in open(METRICS, encoding="utf-8").read().strip().split("\n")[-5:]:
                print(l)
        except Exception as e:
            print("no metrics:", e)
        return
    state = load_state()
    if a.once:
        one_round(state)
        return
    if not single_instance():
        say("已有 autoloop 在跑，本进程自行退出 pid=%d" % os.getpid())
        return
    say("autoloop 常驻启动 pid=%d（永不主动停止）" % os.getpid())
    while True:
        try:
            acts, secs = one_round(state)
            time.sleep(BUSY_SLEEP if len(acts) > 3 else IDLE_SLEEP)
        except KeyboardInterrupt:
            say("  收到中断，按约定不退出，继续下一轮")
            time.sleep(5)
        except Exception:
            say("  顶层异常，10s 后继续：\n%s" % traceback.format_exc()[-800:])
            time.sleep(10)


if __name__ == "__main__":
    main()
