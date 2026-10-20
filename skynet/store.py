"""天网 · 记录层（store）
========================
一个 SQLite 文件解决三件事，避免每轮重复劳动：

  1. **去重**     同一个 URL 永不重复 probe / download（断点续跑的基础）
  2. **域画像**   每个 host 的历史命中率、平均分、最好成绩 ⇒ 搜索层拿它做提权
  3. **失败重试** 记录失败原因与次数，下一轮只捞没到上限的

表只有两张，字段刻意少，方便以后换库（DuckDB / ClickHouse）时不改调用方。
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent / "_skynet.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    url      TEXT PRIMARY KEY,
    host     TEXT NOT NULL,
    kind     TEXT DEFAULT '',        -- probe / text / audio / corpus
    verdict  TEXT DEFAULT '',
    score    INTEGER DEFAULT 0,
    size     INTEGER DEFAULT 0,
    tries    INTEGER DEFAULT 0,
    err      TEXT DEFAULT '',
    path     TEXT DEFAULT '',        -- 落盘路径（audio 重试要知道存到哪）
    ts       REAL
);
CREATE INDEX IF NOT EXISTS idx_seen_host ON seen(host);
CREATE INDEX IF NOT EXISTS idx_seen_verdict ON seen(verdict);

CREATE TABLE IF NOT EXISTS host (
    host     TEXT PRIMARY KEY,
    probes   INTEGER DEFAULT 0,
    good     INTEGER DEFAULT 0,
    best     INTEGER DEFAULT 0,
    avg      REAL DEFAULT 0,
    last     REAL
);

-- 引擎健康度：跨进程累积。搜索端点会随时间变化（Bing 就废过一次），
-- 所以「哪个引擎还值得问」必须是可学习、可衰减的状态，不能写死在代码里。
CREATE TABLE IF NOT EXISTS engine (
    name      TEXT PRIMARY KEY,
    calls     INTEGER DEFAULT 0,
    submitted INTEGER DEFAULT 0,
    alive     INTEGER DEFAULT 0,
    ts        REAL
);

-- 内容指纹：同一份字节只存一份。
-- 为什么要它：音频按 URL 落盘，同一个角色语音在不同站/不同皮肤页会重复提供，
-- 按 URL 判重看不出来，按内容 hash 才看得出来。
CREATE TABLE IF NOT EXISTS blob (
    sha256  TEXT PRIMARY KEY,
    path    TEXT NOT NULL,
    size    INTEGER DEFAULT 0,
    refs    INTEGER DEFAULT 1,
    ts      REAL
);
CREATE INDEX IF NOT EXISTS idx_blob_size ON blob(size);
"""


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    con = sqlite3.connect(str(path or DB_PATH), timeout=15)
    con.executescript(_SCHEMA)
    con.commit()
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """老库补列。`CREATE TABLE IF NOT EXISTS` 不会给已有表加字段，
    升级时要显式 ALTER，否则老库一写新字段就报错。"""
    cols = {r[1] for r in con.execute("PRAGMA table_info(seen)").fetchall()}
    if "path" not in cols:
        con.execute("ALTER TABLE seen ADD COLUMN path TEXT DEFAULT ''")
        con.commit()


def mark(con: sqlite3.Connection, url: str, host: str, *, kind: str = "",
         verdict: str = "", score: int = 0, size: int = 0, err: str = "",
         path: str = "") -> None:
    """写入/更新一条记录，并同步维护域画像。"""
    now = time.time()
    con.execute(
        "INSERT INTO seen(url,host,kind,verdict,score,size,tries,err,path,ts) "
        "VALUES(?,?,?,?,?,?,1,?,?,?) "
        "ON CONFLICT(url) DO UPDATE SET verdict=excluded.verdict, score=excluded.score, "
        "size=excluded.size, tries=seen.tries+1, err=excluded.err, "
        "path=CASE WHEN excluded.path<>'' THEN excluded.path ELSE seen.path END, "
        "ts=excluded.ts",
        (url, host, kind, verdict, score, size, err, path, now))
    if verdict:
        row = con.execute("SELECT probes,good,best,avg FROM host WHERE host=?", (host,)).fetchone()
        p, g, b, a = row if row else (0, 0, 0, 0.0)
        p += 1
        if verdict == "valuable":
            g += 1
        b = max(b, score)
        a = (a * (p - 1) + score) / p
        con.execute(
            "INSERT INTO host(host,probes,good,best,avg,last) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(host) DO UPDATE SET probes=?,good=?,best=?,avg=?,last=?",
            (host, p, g, b, a, now, p, g, b, a, now))
    con.commit()


def known(con: sqlite3.Connection, url: str) -> Optional[tuple]:
    return con.execute("SELECT verdict,score,ts FROM seen WHERE url=?", (url,)).fetchone()


DONE_VERDICTS = ("valuable", "portal", "dead", "ok")
"""判定为「有结论」的集合。不在其中的（blocked / thin / 空）都要进重试队列。

'ok' 是给下载类任务用的：音频抓到就算完成，不必套页面质量的四档判定。
"""


def is_done(con: sqlite3.Connection, url: str, kinds: tuple = ()) -> bool:
    """已成功处理过（有结论且不是 blocked/thin）则跳过。"""
    row = con.execute("SELECT verdict,kind FROM seen WHERE url=?", (url,)).fetchone()
    if not row:
        return False
    verdict, kind = row
    if kinds and kind not in kinds:
        return False
    return verdict in DONE_VERDICTS


def pending(con: sqlite3.Connection, max_tries: int = 3, limit: int = 200,
            kinds: tuple = ()) -> list[str]:
    """捞出还没到重试上限的失败项。

    kinds 用来分开补：探活失败（kind='probe'）和下文件失败（kind='audio'）
    的重试策略完全不同 —— 探活失败多试几轮无妨，下大文件失败要尽快续传。
    """
    q = ("SELECT url FROM seen WHERE verdict NOT IN (%s) AND tries<? "
         % ",".join("?" * len(DONE_VERDICTS)))
    args: list = list(DONE_VERDICTS) + [max_tries]
    if kinds:
        q += "AND kind IN (%s) " % ",".join("?" * len(kinds))
        args.extend(kinds)
    q += "ORDER BY tries ASC, score DESC LIMIT ?"
    args.append(limit)
    return [r[0] for r in con.execute(q, args).fetchall()]


def pending_rows(con: sqlite3.Connection, max_tries: int = 3, limit: int = 200,
                 kinds: tuple = ()) -> list[tuple]:
    """同 pending，但带出 kind/path/err/tries —— 重试时要知道「怎么补」。"""
    q = ("SELECT url,kind,path,err,tries FROM seen WHERE verdict NOT IN (%s) AND tries<? "
         % ",".join("?" * len(DONE_VERDICTS)))
    args: list = list(DONE_VERDICTS) + [max_tries]
    if kinds:
        q += "AND kind IN (%s) " % ",".join("?" * len(kinds))
        args.extend(kinds)
    q += "ORDER BY tries ASC, score DESC LIMIT ?"
    args.append(limit)
    return con.execute(q, args).fetchall()


def host_rank(con: sqlite3.Connection, limit: int = 30) -> list[tuple]:
    return con.execute(
        "SELECT host,probes,good,best,ROUND(avg,1) FROM host "
        "ORDER BY good DESC, avg DESC LIMIT ?", (limit,)).fetchall()


# ---------------------------------------------------------------- 内容指纹
def sha256_file(path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def blob_find(con: sqlite3.Connection, sha: str) -> Optional[str]:
    """已知指纹的落盘路径；记录指向的文件已被删则顺手清账。"""
    row = con.execute("SELECT path FROM blob WHERE sha256=?", (sha,)).fetchone()
    if not row:
        return None
    p = Path(row[0])
    if not p.exists():
        con.execute("DELETE FROM blob WHERE sha256=?", (sha,))
        con.commit()
        return None
    return row[0]


def blob_add(con: sqlite3.Connection, sha: str, path, size: int) -> None:
    now = time.time()
    con.execute(
        "INSERT INTO blob(sha256,path,size,refs,ts) VALUES(?,?,?,1,?) "
        "ON CONFLICT(sha256) DO UPDATE SET refs=blob.refs+1, ts=?",
        (sha, str(path), size, now, now))
    con.commit()


def blob_stats(con: sqlite3.Connection) -> dict:
    r = con.execute("SELECT COUNT(*), SUM(size) FROM blob").fetchone()
    dup = con.execute("SELECT COUNT(*) FROM blob WHERE refs>1").fetchone()[0]
    return {"unique": r[0] or 0, "MB": round((r[1] or 0) / 1048576, 1), "shared": dup}


def summary(con: sqlite3.Connection) -> dict:
    r = con.execute("SELECT COUNT(*), SUM(size) FROM seen").fetchone()
    v = dict(con.execute("SELECT verdict,COUNT(*) FROM seen GROUP BY verdict").fetchall())
    h = con.execute("SELECT COUNT(*) FROM host").fetchone()[0]
    return {"urls": r[0] or 0, "MB": round((r[1] or 0) / 1048576, 1), "hosts": h, "verdict": v}


# ---------------------------------------------------------------- 引擎健康度
def load_engines(con: sqlite3.Connection) -> dict:
    """读出各引擎的累计表现：{name: {calls, submitted, alive}}。"""
    rows = con.execute("SELECT name,calls,submitted,alive FROM engine").fetchall()
    return {n: {"calls": c, "in": s, "out": a} for n, c, s, a in rows}


def save_engine(con: sqlite3.Connection, name: str, calls: int, submitted: int,
                alive: int) -> None:
    con.execute(
        "INSERT INTO engine(name,calls,submitted,alive,ts) VALUES(?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET calls=?, submitted=?, alive=?, ts=?",
        (name, calls, submitted, alive, time.time(), calls, submitted, alive, time.time()))
    con.commit()
