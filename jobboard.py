# -*- coding: utf-8 -*-
"""netforce / jobboard.py —— 自找活任务板

不是固定动作轮询：它**扫描本机磁盘现状 → 自己生成任务 → 自己消化 → 再扫描**。
只要盘上还有没进过统一检索的东西，队列就不会空；队列空了就再扫一遍
（新下载、新抓取、OCR 新产出都会自动变成新任务）。

旁路库 side_corpus.db 与批量导入解耦：全量导入持有主库写锁期间，
本任务板照常消化，等锁一解除自动把结果合进 corpus.db。

安全边界：疑似凭据的整块文本不复制进索引/报告（按路径调用即可，无需副本）。
这不是拒绝使用，是避免把一份密钥扩散成 N 份可检索副本。

用法：
    python jobboard.py --scan                 # 重新扫盘生成任务
    python jobboard.py --run --seconds 20    # 消化一批（可断点续跑）
    python jobboard.py --merge               # 合并旁路库到主库（有导入锁则跳过）
    python jobboard.py --stats               # 队列与成果统计
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
import zipfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(HERE, "_kb")
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from parse_assets import load_taxonomy, extra_tags, merge_tags, _lang  # noqa: E402
from fine_classify import tag as base_tag                              # noqa: E402
import _bulk_lock                                                      # noqa: E402

JOBS_DB = os.path.join(KB, "jobboard.db")
SIDE_DB = os.path.join(KB, "side_corpus.db")
MAIN_DB = os.path.join(KB, "corpus.db")
BULK_LOCK = os.path.join(KB, "evolve", "BULK_IMPORT")

CHUNK = 4200            # 每块目标字符数
MAX_TEXT = 30000
TAG_PAT = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)
WS = re.compile(r"\s+")
# 只拦「把长串凭据复制进索引」，不拦读取与调用
SECRET_TEXT = re.compile(
    r"(sk-[A-Za-z0-9_\-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|"
    r"Bearer\s+[A-Za-z0-9_\-\.]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")
SENSITIVE_NAME = re.compile(
    r"(^|[\\/._-])(secret|secrets|password|passwd|credential|credentials|"
    r"api[_-]?key|token|\.env|private[_-]?key|id_rsa|keystore|cookie|auth|"
    r"login|\.fleet)([\\/._-]|$)", re.I)

# 扫哪些盘上已有的东西（路径为 Windows 原生，避免 sqlite/zipfile 认不出 POSIX 路径）
ROOTS = [
    (os.path.join(ROOT, "books"), 3),
    (os.path.join(ROOT, "kb_cards"), 2),
    (os.path.join(ROOT, "kb_build"), 2),
    (os.path.join(ROOT, "kb_viz"), 2),
    (os.path.join(ROOT, "corpus"), 2),
    (os.path.join(ROOT, "tools_and_notes"), 2),
    (os.path.join(ROOT, "archives"), 3),
    (os.path.join(ROOT, "_dl"), 2),
    (os.path.join(ROOT, "_models"), 2),
    (os.path.join(HERE, "_dl"), 2),
    # netforce 根目录与 _kb/records 已由 parse_all.py 全量覆盖，此处不重复登记
]
SPECIAL_DB = [
    os.path.join(ROOT, "kb_data", "chroma", "chroma.sqlite3"),
]
SKIP_DIR = {"node_modules", ".git", "__pycache__", ".workbuddy", "evolve",
            ".venv", "_venv", ".idea", "site-packages", "build", "dist",
            "ollama_store", "blobs", "weights", "checkpoints"}
BINARY_MARK = re.compile(rb"[\x00-\x08\x0e-\x1f]")


def looks_binary(path):
    """无扩展名的大文件（GGUF / safetensors blob 等）先探头部，别把权重当语料切。"""
    try:
        with open(path, "rb") as f:
            head = f.read(2048)
        if not head:
            return True
        bad = sum(1 for b in head if b < 9 or (14 <= b <= 31) or b == 127)
        return bad > len(head) * 0.02 or b"\x00" in head[:256]
    except Exception:
        return True
TEXT_EXT = {".txt", ".md", ".markdown", ".jsonl", ".json", ".csv", ".tsv",
            ".epub", ".html", ".htm", ".xml", ".log", ".yaml", ".yml", ".rst"}
BINARY_EXT = {".exe", ".apk", ".zip", ".7z", ".rar", ".dll", ".so", ".bin",
              ".gguf", ".safetensors", ".onnx", ".pt", ".pth", ".db",
              ".sqlite", ".sqlite3", ".png", ".jpg", ".jpeg", ".gif", ".pdf",
              ".mp3", ".mp4", ".wav", ".ttf", ".woff", ".parquet", ".pyc"}


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path, timeout=30):
    c = sqlite3.connect(path, timeout=timeout)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def jobs_db():
    c = connect(JOBS_DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS jobs(
      key TEXT PRIMARY KEY, path TEXT, size INTEGER, mtime REAL, kind TEXT,
      priority INTEGER DEFAULT 5, status TEXT DEFAULT 'pending',
      progress INTEGER DEFAULT 0, chunks INTEGER DEFAULT 0,
      chars INTEGER DEFAULT 0, err TEXT DEFAULT '', ts TEXT);
    CREATE INDEX IF NOT EXISTS j_status ON jobs(status, priority, size);
    """)
    return c


def side_db():
    c = connect(SIDE_DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS side_corpus(
      uid TEXT PRIMARY KEY, source TEXT, kind TEXT, url TEXT, host TEXT,
      title TEXT, descr TEXT, text TEXT, text_len INTEGER, lang TEXT,
      category TEXT, forms TEXT, topics TEXT, subtopics TEXT,
      extra_topics TEXT, quality INTEGER, value TEXT, parse_status TEXT,
      evidence TEXT, meta TEXT, ts TEXT, parse_ver INTEGER);
    CREATE VIRTUAL TABLE IF NOT EXISTS side_fts USING fts5(
      uid UNINDEXED, host, title, topics, text, tokenize='trigram');
    CREATE TABLE IF NOT EXISTS counters(k TEXT PRIMARY KEY, v INTEGER);
    """)
    return c


def bump(c, k, n=1):
    c.execute("INSERT INTO counters VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=v+?", (k, n, n))


def kind_of(path):
    e = os.path.splitext(path)[1].lower()
    if e == ".epub":
        return "epub"
    if e == ".jsonl":
        return "jsonl"
    if e == ".json":
        return "json"
    if e in (".html", ".htm", ".xml"):
        return "html"
    if e in (".md", ".markdown"):
        return "md"
    if e in (".txt", ".log", ".rst"):
        return "text"
    if e in (".csv", ".tsv"):
        return "csv"
    if e in (".yaml", ".yml"):
        return "yaml"
    return "text"


def priority_of(path, kind):
    p = 5
    if "kb_cards" in path:
        p = 9
    if "tools_and_notes" in path and "corpus" in path:
        p = 9
    if "clean_books" in path:
        p = 8
    if kind in ("epub",):
        p = 7
    if os.path.getsize(path) > 50 * 1048576:
        p -= 1        # 超大件往后排，先清小的，避免单任务长期占着
    return max(1, p)


# ------------------------------------------------------------------ 扫盘 → 任务
def scan(verbose=True):
    c = jobs_db()
    added = skipped = 0
    seen = set()
    for base, depth in ROOTS:
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            rel = os.path.relpath(dirpath, base)
            if rel.count(os.sep) >= depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR]
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                ext = os.path.splitext(fn)[1].lower()
                if ext in BINARY_EXT or (ext not in TEXT_EXT and ext != ""):
                    skipped += 1
                    continue
                if SENSITIVE_NAME.search(p):
                    skipped += 1
                    continue
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if st.st_size < 128:
                    skipped += 1
                    continue
                # 无扩展名 / 超大件：模型权重常被存成无后缀 blob，必须探头部
                if (ext == "" or st.st_size > 80 * 1048576) and looks_binary(p):
                    skipped += 1
                    continue
                key = hashlib.sha1(("%s|%d" % (p, st.st_size)).encode("utf-8", "replace")).hexdigest()
                if key in seen:
                    continue
                seen.add(key)
                if c.execute("SELECT 1 FROM jobs WHERE key=?", (key,)).fetchone():
                    continue
                kind = kind_of(p)
                c.execute("INSERT OR IGNORE INTO jobs(key,path,size,mtime,kind,priority,ts) "
                          "VALUES(?,?,?,?,?,?,?)",
                          (key, p, st.st_size, st.st_mtime, kind, priority_of(p, kind), now()))
                added += 1
    for p in SPECIAL_DB:
        if not os.path.isfile(p):
            continue
        key = hashlib.sha1(("db|" + p).encode("utf-8", "replace")).hexdigest()
        if c.execute("SELECT 1 FROM jobs WHERE key=?", (key,)).fetchone():
            continue
        c.execute("INSERT OR IGNORE INTO jobs(key,path,size,mtime,kind,priority,ts) "
                  "VALUES(?,?,?,?,?,?,?)",
                  (key, p, os.path.getsize(p), os.stat(p).st_mtime, "sqlite", 9, now()))
        added += 1
    c.commit()
    tot = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    pend = c.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('pending','doing')").fetchone()[0]
    if verbose:
        print("[scan] 新增任务 %d，跳过 %d（二进制/小文件/疑似凭据）；队列合计 %d，待办 %d"
              % (added, skipped, tot, pend))
    c.close()
    return added


# ------------------------------------------------------------------ 块生成器
def clean(s):
    s = html.unescape(s or "")
    s = TAG_PAT.sub(" ", s)
    return WS.sub(" ", s).strip()


def chunks_text(path, start):
    with open(path, "rb") as f:
        f.seek(start * CHUNK)
        i = start
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            yield i, clean(b.decode("utf-8", "replace"))
            i += 1


def chunks_epub(path, start):
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist()
                 if n.lower().endswith((".xhtml", ".html", ".htm", ".txt"))]
        for i in range(start, len(names)):
            try:
                raw = z.read(names[i]).decode("utf-8", "replace")
            except Exception:
                continue
            yield i, clean(raw)


def chunks_jsonl(path, start):
    """progress 语义 = 已消费的行号；每 30 行拼一块，可精确断点续跑。"""
    import itertools
    keys = ("question_content", "answer", "text", "content", "body", "title",
            "question", "instruction", "output", "snippet", "descr", "paragraph")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        buf, n = [], start
        for line in itertools.islice(f, start, None):
            n += 1
            try:
                d = json.loads(line)
            except Exception:
                continue
            if isinstance(d, dict):
                s = " ".join(str(d[k]) for k in keys if d.get(k))
                if not s:
                    s = json.dumps(d, ensure_ascii=False)[:CHUNK]
            else:
                s = str(d)
            buf.append(s[:CHUNK])
            if len(buf) >= 30:
                yield n, clean(" \n ".join(buf))[:CHUNK]
                buf = []
        if buf:
            yield n, clean(" \n ".join(buf))[:CHUNK]


def chunks_json(path, start):
    try:
        d = json.load(open(path, "r", encoding="utf-8", errors="replace"))
    except Exception:
        return
    def walk(o, out):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str) and len(v) > 20:
                    out.append(v)
                else:
                    walk(v, out)
        elif isinstance(o, list):
            for v in o[:3000]:
                walk(v, out)
        elif isinstance(o, str) and len(o) > 20:
            out.append(o)
    out = []
    walk(d, out)
    s = clean(" \n ".join(out))
    for i in range(start, (len(s) + CHUNK - 1) // CHUNK):
        yield i, s[i * CHUNK:(i + 1) * CHUNK]


def chunks_sqlite(path, start):
    """只吃 chroma 那类全文影子表；progress 语义 = OFFSET 行号，稳定可恢复。"""
    uri = "file:" + path.replace("\\", "/") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        target = next((t for t in tables if "fulltext" in t and "content" in t), None)
        if not target:
            return
        cur = start
        while True:
            rows = con.execute('SELECT * FROM "%s" LIMIT 200 OFFSET ?' % target,
                               (cur,)).fetchall()
            if not rows:
                break
            for r in rows:
                txt = clean(" ".join(str(x) for x in r if isinstance(x, str)))
                cur += 1
                if len(txt) > 30:
                    yield cur, txt[:CHUNK]
    finally:
        con.close()


GEN = {"text": chunks_text, "md": chunks_text, "csv": chunks_text,
       "yaml": chunks_text, "html": chunks_text, "epub": chunks_epub,
       "jsonl": chunks_jsonl, "json": chunks_json, "sqlite": chunks_sqlite}


# ------------------------------------------------------------------ 打标签
def classify(text, title, host, path, tax):
    probe = {"domain": host, "url": "file:///" + os.path.basename(path),
             "ok": True, "status": 200, "title": title, "desc": "",
             "snippet": text[:500], "text": text, "text_len": len(text),
             "headings": [], "keywords": [], "links": [], "assets": [],
             "new_domains": [], "xhr_count": 0, "structured": False,
             "rendered": False, "lang": "", "exts": []}
    try:
        base = base_tag(probe) or {}
    except Exception:
        base = {}
    hay = " ".join([title, host, os.path.basename(path), text[:12000]]).lower()
    try:
        et, ef, ev = extra_tags(tax, hay, host, os.path.basename(path))
    except Exception:
        et, ef, ev = [], [], {}
    try:
        tp, subs, forms, ev_all = merge_tags(base, et, ef, ev, title, len(text))
    except Exception:
        tp, subs, forms, ev_all = (list(base.get("topics", [])) + et)[:4], [], \
                                  list(base.get("forms", [])), ev
    if not tp:
        tp = ["未命中/待语义复核"]
    # merge_tags 是「基础 Top-4 + 扩展 Top-3」，短文本命中词密集时会叠到 7 个；
    # 这里再收一次：基础最多 3 条 + 扩展最多 1 条，稳在 4 个以内。
    if len(tp) > 4:
        core = [t for t in tp if t not in et][:3]
        ext = [t for t in tp if t in et][:1]
        tp = (core + ext) or tp[:4]
    q = min(100, int(base.get("quality") or 0) + (10 if len(text) > 2000 else 0))
    return (base.get("category") or "其他/待定", json.dumps(forms, ensure_ascii=False),
            json.dumps(tp, ensure_ascii=False), json.dumps(subs, ensure_ascii=False),
            json.dumps(et, ensure_ascii=False), q,
            "高" if q >= 55 else ("中" if q >= 28 else "低"),
            json.dumps(ev_all, ensure_ascii=False)[:2000],
            base.get("lang") or _lang(text))


# ------------------------------------------------------------------ 消化一批
def run(seconds=20, max_chunks=400, verbose=True):
    j = jobs_db()
    s = side_db()
    tax = load_taxonomy()
    t0 = time.time()
    done_chunks = done_jobs = secret_hits = errors = 0
    batch = j.execute(
        "SELECT key,path,kind,progress,size FROM jobs "
        "WHERE status IN ('pending','doing','retry') "
        "ORDER BY priority DESC, size ASC LIMIT 60").fetchall()
    j.execute("UPDATE jobs SET status='doing' WHERE key IN (%s)" %
              (",".join("?" * len(batch)) or "''"), [b[0] for b in batch]) if batch else None
    j.commit()
    for key, path, kind, prog, size in batch:
        if time.time() - t0 > seconds or done_chunks >= max_chunks:
            j.execute("UPDATE jobs SET status='pending' WHERE key=? AND status='doing'", (key,))
            continue
        if not os.path.isfile(path):
            j.execute("UPDATE jobs SET status='gone' WHERE key=?", (key,))
            continue
        # 二次保险：扫盘时没挡住的历史误登记，开跑前再探一次头部
        if kind in ("text", "md", "html") and os.path.splitext(path)[1] == "" \
                and looks_binary(path):
            j.execute("UPDATE jobs SET status='skip_binary' WHERE key=?", (key,))
            continue
        gen = GEN.get(kind, chunks_text)
        title = os.path.splitext(os.path.basename(path))[0][:120]
        host = os.path.basename(os.path.dirname(path)) or "local"
        got = 0
        step = 0 if kind in ("jsonl", "sqlite") else 1   # 这两类 yield 的已是消费后行号
        last_idx = prog or 0
        finished = True
        try:
            for idx, text in gen(path, prog or 0):
                if time.time() - t0 > seconds or done_chunks >= max_chunks:
                    finished = False
                    break
                last_idx = idx + step
                got += 1
                if not text or len(text) < 40:
                    continue
                if SECRET_TEXT.search(text):
                    secret_hits += 1
                    bump(s, "secret_blocks")
                    continue                  # 不复制明文，只记数
                uid = hashlib.sha1(("%s|%d" % (key, idx)).encode("utf-8", "replace")).hexdigest()
                cat, forms, tp, subs, ext, q, val, ev, lang = classify(text, title, host, path, tax)
                s.execute(
                    "INSERT OR REPLACE INTO side_corpus VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uid, "jobboard/" + kind, kind, "file:///" + path.replace("\\", "/"),
                     host, title, "", text[:MAX_TEXT], len(text), lang,
                     cat, forms, tp, subs, ext, q, val, "ok", ev,
                     json.dumps({"job": key, "chunk": idx}, ensure_ascii=False), now(), 1))
                try:
                    s.execute("DELETE FROM side_fts WHERE uid=?", (uid,))
                    s.execute("INSERT INTO side_fts(uid,host,title,topics,text) VALUES(?,?,?,?,?)",
                             (uid, host, title, tp, text[:MAX_TEXT]))
                except Exception:
                    pass
                done_chunks += 1
                if done_chunks % 25 == 0:
                    j.execute("UPDATE jobs SET progress=? WHERE key=?", (last_idx, key))
                    j.commit()
                    s.commit()
                if idx > 200000:
                    finished = False
                    break
            if finished:
                j.execute("UPDATE jobs SET status='done',progress=?,chunks=chunks+?,ts=? WHERE key=?",
                          (last_idx, got, now(), key))
                done_jobs += 1
            else:
                j.execute("UPDATE jobs SET status='pending',progress=? WHERE key=?",
                          (last_idx, key))
        except Exception as e:
            errors += 1
            j.execute("UPDATE jobs SET status='retry',err=?,ts=? WHERE key=?",
                      (str(e)[:300], now(), key))
        j.commit()
        s.commit()
    # 本轮取了但没轮到的任务一律放回待办，不留 doing 悬挂
    j.execute("UPDATE jobs SET status='pending' WHERE status='doing'")
    j.commit()
    bump(s, "chunks", done_chunks)
    bump(s, "files_done", done_jobs)
    s.commit()
    pend = j.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('pending','doing','retry')").fetchone()[0]
    if verbose:
        print("[run] %.1fs 消化 %d 块 / 完结 %d 文件；凭据块跳过 %d；错误 %d；待办 %d"
              % (time.time() - t0, done_chunks, done_jobs, secret_hits, errors, pend))
    j.close()
    s.close()
    return done_chunks


# ------------------------------------------------------------------ 合并进主库
def merge(verbose=True, tick=None):
    # 用带属主的锁：导入进程若已死，locked() 会当场清掉僵尸锁再返回 False，
    # 不会再出现「锁残留 → 旁路成果永远合不进主库」。
    if _bulk_lock.locked():
        if verbose:
            print("[merge] 全量导入锁仍在，跳过合并（旁路库继续累积）")
        return 0
    if not os.path.isfile(SIDE_DB) or not os.path.isfile(MAIN_DB):
        return 0
    s = connect(SIDE_DB)
    s.execute("CREATE TABLE IF NOT EXISTS merged(uid TEXT PRIMARY KEY, ts TEXT)")
    s.commit()
    rows = s.execute(
        "SELECT c.uid,c.source,c.kind,c.url,c.host,c.title,c.descr,c.text,c.text_len,"
        "c.lang,c.category,c.forms,c.topics,c.subtopics,c.extra_topics,c.quality,c.value,"
        "c.parse_status,c.evidence,c.meta,c.ts,c.parse_ver FROM side_corpus c "
        "LEFT JOIN merged m ON m.uid=c.uid WHERE m.uid IS NULL LIMIT 4000").fetchall()
    if not rows:
        s.close()
        return 0
    m = connect(MAIN_DB, timeout=60)
    cols = {r[1] for r in m.execute("PRAGMA table_info(corpus)")}
    use = [k for k in ("uid", "source", "kind", "url", "host", "title", "descr", "text",
                       "text_len", "lang", "category", "forms", "topics", "subtopics",
                       "extra_topics", "quality", "value", "parse_status", "evidence",
                       "meta", "ts", "parse_ver") if k in cols]
    idx = [("uid", "source", "kind", "url", "host", "title", "descr", "text", "text_len",
            "lang", "category", "forms", "topics", "subtopics", "extra_topics", "quality",
            "value", "parse_status", "evidence", "meta", "ts", "parse_ver").index(k) for k in use]
    ph = ",".join("?" for _ in use)
    # corpus_fts 的 uid 是 UNINDEXED 列：逐条 DELETE 会让每批 4000 条各做一次
    # 全表扫描（trigram 大库上一次几秒），worker 因此几分钟不刷心跳。
    # 改成按批 IN 一次删，整批只扫 10 次；循环里就不再逐条删。
    uids = [r[0] for r in rows]
    for i in range(0, len(uids), 400):
        chunk = uids[i:i + 400]
        try:
            m.execute("DELETE FROM corpus_fts WHERE uid IN (%s)" %
                      ",".join("?" * len(chunk)), chunk)
        except Exception:
            pass
    m.commit()
    n = 0
    for r in rows:
        try:
            m.execute("INSERT OR REPLACE INTO corpus(%s) VALUES(%s)" % (",".join(use), ph),
                      [r[i] for i in idx])
            try:
                m.execute("INSERT INTO corpus_fts(uid,host,title,topics,text) VALUES(?,?,?,?,?)",
                          (r[0], r[4], r[5], r[12], r[7]))
            except Exception:
                pass
            s.execute("INSERT OR IGNORE INTO merged VALUES(?,?)", (r[0], now()))
            n += 1
        except Exception:
            pass
        if n and n % 500 == 0:
            m.commit()
            s.commit()
            if tick:
                try:
                    tick("merge %d" % n)
                except Exception:
                    pass
    m.commit()
    s.commit()
    m.close()
    s.close()
    if verbose:
        print("[merge] 合入主库 %d 条" % n)
    return n


def stats():
    j = jobs_db()
    print("=== 任务板 ===")
    for st, n in j.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status ORDER BY 2 DESC"):
        print("  %-10s %d" % (st, n))
    print("  待办体积 %.1f MB" % (j.execute(
        "SELECT COALESCE(SUM(size),0) FROM jobs WHERE status IN ('pending','doing','retry')"
    ).fetchone()[0] / 1048576))
    j.close()
    s = side_db()
    print("=== 旁路成果 ===")
    print("  正文块 %d / 字符 %s" % (
        s.execute("SELECT COUNT(*) FROM side_corpus").fetchone()[0],
        format(s.execute("SELECT COALESCE(SUM(text_len),0) FROM side_corpus").fetchone()[0], ",")))
    for k, v in s.execute("SELECT k,v FROM counters"):
        print("  %-14s %d" % (k, v))
    print("  FTS 可检索 %d" % s.execute("SELECT COUNT(*) FROM side_fts").fetchone()[0])
    s.close()
    print("=== 导入锁 ===", "导入中" if os.path.exists(BULK_LOCK) else "已解除")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--max-chunks", type=int, default=400)
    a = ap.parse_args()
    if a.scan:
        scan()
    if a.run:
        run(a.seconds, a.max_chunks)
    if a.merge:
        merge()
    if a.stats or not (a.scan or a.run or a.merge):
        stats()


if __name__ == "__main__":
    main()
