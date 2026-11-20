#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地小模型批量补标：给「未命中/待语义复核」的条目打主题标签。

设计要点
- 断点续跑：已写入 semantic_tags 的 uid 自动跳过
- 并发只到 HTTP 层：N 个线程各发请求，写库仍由主线程单线程完成（无 sqlite 锁竞争）
- 单条异常不重试、不猜原因；未处理的留到下一轮（按 uid 幂等，状态全在盘上）
- 每轮结束立即 commit，绝不把写事务带进网络等待
- backend 可切：默认打本地 llama-server 的 OpenAI 兼容端点
"""
import argparse, json, os, re, sqlite3, time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "corpus.db")

# 🔴 11-12 主人定：不再有固定标签集，也不再从 autoloop 导入 LABELS。
#    预设 27 类曾让模型在跨领域内容上一律退到「其他」，等于把判断力废掉。
#    历史已标条目保留原样（存量不追溯整改），新标的走开放标签。

# 🔴 11-12 主人定：不给固定标签集。模型是模型，不是分类器脚本；
#    预设 27 类（以及后来的 20 类锚点）都会逼它在跨领域内容上硬塞「其他」，
#    等于把判断力废掉。让它自己读内容、自己造具体短语，只约束输出格式（JSON 数组）与粒度。
# 🔴 11-20 统一口径：与 autoloop.semantic_fill() 完全对齐——开放标签、零锚点、零兜底词。
#    历史已标条目保留原样（存量不追溯整改），新标的走开放标签。
PROMPT_TPL = (
    "读下面这个网页，用 1-3 个最贴切的中文短语概括它的主题。\n"
    "自己判断内容，不要从任何预设列表里挑，也不要用「其他」「综合」这类兜底词；"
    "短语要具体、能区分这篇内容（例如「Rust 异步运行时调优」而不是「编程」）。\n\n"
    "标题：%s\n来源：%s\n正文摘录：%s\n\n"
    "只输出 JSON 数组，每个短语不超过 14 个字，不要解释。"
)


def _llm_key(base):
    """本地 llama-server 不用鉴权；云端网关（8787 zen-gate）必须带 Bearer。

    优先环境变量，其次 ~/.fleet/zengate.key —— 后者让开机自启的常驻栈也能直接用，
    不依赖启动时的 shell 环境。密钥本身不写进代码、也不写进仓库配置。
    """
    k = os.environ.get("NETFORCE_LLM_KEY", "").strip()
    if k:
        return k
    if "127.0.0.1:8080" in base:
        return ""
    p = os.path.join(os.path.expanduser("~"), ".fleet", "zengate.key")
    try:
        return open(p, encoding="utf-8").read().strip()
    except Exception:
        return ""


def call_llm(base, model, prompt, timeout=45):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        # Gemma 4 是思考模型：不关思考会先把 token 花在 reasoning 上，短上限下正文为空
        "chat_template_kwargs": {"enable_thinking": False},
        "max_tokens": 200, "temperature": 0,
    }).encode()
    headers = {"Content-Type": "application/json"}
    key = _llm_key(base)
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body,
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.load(r)
    msg = out["choices"][0]["message"]
    return (msg.get("content") or "") + (msg.get("reasoning_content") or "")


GENERIC = {"其他", "综合", "杂项", "未分类", "未知", "资讯", "技术",
           "文章", "网页", "内容", "信息", "一般", "其它"}


def parse_labels(text):
    """只做格式清洗，不做白名单校验：标签由模型自己造。"""
    found = re.findall(r"\[[\s\S]*?\]", text or "")
    if not found:
        return []
    try:
        arr = json.loads(found[-1])
    except Exception:
        return []
    out = []
    for x in arr:
        if not isinstance(x, str):
            continue
        s = x.strip().strip("\"'、,，")
        if not s or len(s) > 24 or s in GENERIC:
            continue
        if s not in out:
            out.append(s)
        if len(out) >= 3:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("NETFORCE_LLM_BASE", "http://127.0.0.1:8080/v1"))
    ap.add_argument("--model", default="gemma4-e4b")
    ap.add_argument("--workers", type=int, default=1,
                    help="HTTP 并发数；写库恒为主线程单线程。实测本机 4B+6G 显存下并发反而更慢（前缀缓存被多 slot 分食），默认串行")
    ap.add_argument("--limit", type=int, default=200, help="本轮最多处理多少条，0=不限")
    ap.add_argument("--minlen", type=int, default=400)
    args = ap.parse_args()

    con = sqlite3.connect(CORPUS, timeout=180, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=180000")
    # 候选按 text_len 倒序，没索引时 10 万行排序要几十秒
    con.execute("CREATE INDEX IF NOT EXISTS idx_corpus_textlen ON corpus(text_len DESC)")
    # 失败单独记账，不写 semantic_tags：写占位会让这条被打上「已处理」永久跳过。
    # 连续失败 3 次才放弃，服务重启后仍会重试其余条目。
    con.execute("CREATE TABLE IF NOT EXISTS semantic_fail("
                "uid TEXT PRIMARY KEY, n INTEGER, ts TEXT, err TEXT)")
    con.execute("DELETE FROM semantic_tags WHERE topics='[]' AND raw LIKE 'ERR%'")
    con.commit()
    rows = con.execute(
        "SELECT uid,host,url,title,substr(text,1,1200) FROM corpus "
        "WHERE (topics LIKE '%未命中/待语义复核%' OR topics IS NULL OR topics='') "
        "AND text_len>? "
        "AND source NOT IN ('corpus_chunk','corpus_file') "
        "AND uid NOT IN (SELECT uid FROM semantic_tags) "
        "AND uid NOT IN (SELECT uid FROM semantic_fail WHERE n>=3) "
        "ORDER BY text_len DESC " + ("LIMIT %d" % args.limit if args.limit else ""),
        (args.minlen,)).fetchall()
    print("本轮候选 %d 条，并发 %d，后端 %s" % (len(rows), max(1, args.workers), args.base), flush=True)
    if not rows:
        return

    # 两段式：线程池只负责「发请求、拿原文」，绝不碰 sqlite；
    # 解析与写库全部回到主线程，所以并发再高也只有一个写者。
    def fetch(row):
        uid, host, url, title, text = row
        prompt = PROMPT_TPL % ((title or "")[:150], (host or "")[:80],
                               re.sub(r"\s+", " ", text or "")[:900])
        return uid, call_llm(args.base, args.model, prompt)

    def commit(uid, out):
        labels = parse_labels(out)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if labels:
            con.execute("INSERT OR REPLACE INTO semantic_tags VALUES(?,?,?,?)",
                        (uid, now, json.dumps(labels, ensure_ascii=False), (out or "")[:400]))
            # category 是旧的粗分类维度（站点只当 chip 显示），保持不动；开放标签只写 topics
            con.execute("UPDATE corpus SET topics=? WHERE uid=?",
                        (json.dumps(labels, ensure_ascii=False), uid))
            con.execute("DELETE FROM semantic_fail WHERE uid=?", (uid,))
        else:
            # 走到这里说明服务正常回了话，只是内容没解析出标签 —— 才算内容失败
            con.execute(
                "INSERT INTO semantic_fail VALUES(?,?,?,?) "
                "ON CONFLICT(uid) DO UPDATE SET n=n+1, ts=excluded.ts, err=excluded.err",
                (uid, 1, now, (out or "")[:300]))
        con.commit()
        return bool(labels)

    ok = bad = done = 0
    t0 = time.time()
    workers = max(1, args.workers)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(fetch, row): row for row in rows}
        for fut in as_completed(futs):
            row = futs[fut]
            uid = row[0]
            try:
                uid, out = fut.result()
            except Exception as e:
                # 单条失败只记账，不停整轮（并发下停下会浪费已完成的请求）
                now = time.strftime("%Y-%m-%d %H:%M:%S")
                con.execute(
                    "INSERT INTO semantic_fail VALUES(?,?,?,?) "
                    "ON CONFLICT(uid) DO UPDATE SET n=n+1, ts=excluded.ts, err=excluded.err",
                    (uid, 1, now, ("HTTP_ERR " + str(e))[:300]))
                con.commit()
                bad += 1
                done += 1
                continue
            try:
                if commit(uid, out):
                    ok += 1
                else:
                    bad += 1
            except Exception as e:
                print("[停] 写库失败 uid=%s：%s" % (uid, str(e)[:180]), flush=True)
                con.rollback()
                break
            done += 1
            if done % 50 == 0:
                print("  [进度] %d/%d，成功 %d，用时 %.1f 分"
                      % (done, len(rows), ok, (time.time() - t0) / 60), flush=True)
    print("完成：成功 %d / 解析失败 %d / 已处理 %d / 共 %d，耗时 %.1f 分"
          % (ok, bad, done, len(rows), (time.time() - t0) / 60), flush=True)


if __name__ == "__main__":
    main()
