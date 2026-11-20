# -*- coding: utf-8 -*-
"""netforce / download_assets.py —— 资源落盘层（可续传，禁止「一断全废」）

为什么要有这一层：
    render_parse.py 把 JS 触发的资源 URL 记进了 _kb/discovered.jsonl，但那些只是
    **线索**，不是资源本身。本地知识库要真正拿到冷门资料，必须把文件下回来。

🔴 主人 2026-10-30 定的下载铁律，逐条落实：
    ① 每段独立落盘、断点可接，不许无状态一次性脚本
       → 首选 aria2c -c -x8 -s16（自带 .aria2 控制文件，中断可续）；
         没有 aria2c 时自研 Range 分段器：每段一个文件、已完整的段跳过、
         未完成的段接着写，全部完成后才拼接。
    ② 下完再拼接并校验总大小 → 拼完比对 Content-Length，再算 sha256。
    ③ 双通道 → 出向选路复用 netpolicy.channels(host) 的固定出口；
       默认纯直连（不碰主人本地端口），只有远程显式指定才走固定代理，
       **不因某次失败切换出口**。
    ④ 判断进度只看真实字节 → --file-allocation=none，不看预分配尺寸。
    ⑤ 工具优先级：aria2c > aria2c 多区间 > 自研分段；别每次重造轮子。

另外两条：
    · 出网纪律（2026-11-04）：清掉环境里的 *_PROXY，默认 --all-proxy 不传；
      aria2c / curl / urllib 都不许偷偷继承系统代理或 7890/3067。
    · 价值过滤：实测首轮 43 个 asset 全是 logo/svg/导航图标/status.json。
      只下「文档 / 数据 / 模型 / 代码 / 电子书 / 媒体 / 安装包」白名单扩展名，
      并从页面 links 里补捞真正的 .pdf/.csv/.zip —— 冷门资源多藏在链接里。

用法：
    python download_assets.py --scan                 # 只把线索入队
    python download_assets.py --limit 30            # 下载一批（默认 --scan 自动先跑）
    python download_assets.py --status              # 看队列/落盘统计
    python download_assets.py --url https://x/a.pdf # 单个直下
    python download_assets.py --retry-failed --limit 50
    python download_assets.py --verify              # 复核已下文件，丢了的重排队
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import netpolicy

BASE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(BASE, "_kb")
OUT = os.path.join(KB, "assets")             # 成品
PARTS = os.path.join(KB, "assets", "parts")  # 断点半成品（永不自动删）
DB = os.path.join(KB, "assets.db")
MANIFEST = os.path.join(KB, "assets", "manifest.jsonl")
DISCOVERED = os.path.join(KB, "discovered.jsonl")
RECORDS = os.path.join(KB, "records")

# 值得落盘的资源形态；图标/样式/字体一律不进来（--include-images 才放行图片）
WANT_EXT = {
    ".pdf", ".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz",
    ".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".parquet", ".arrow",
    ".feather", ".hdf5", ".npy", ".npz", ".xlsx", ".xls", ".ods", ".sqlite",
    ".epub", ".mobi", ".djvu", ".mp3", ".wav", ".flac", ".ogg", ".m4a",
    ".mp4", ".mkv", ".avi", ".webm", ".srt", ".vtt",
    ".gguf", ".safetensors", ".onnx", ".ckpt", ".pth", ".pt", ".bin",
    ".md", ".rst", ".adoc", ".txt", ".docx", ".pptx", ".py", ".ipynb",
    ".yaml", ".yml", ".xml", ".sql", ".patch", ".diff", ".tex", ".bib",
    ".exe", ".msi", ".apk", ".dmg", ".pkg", ".deb", ".rpm", ".iso",
    # 2026-11-05：Blender 的 .msix 把流水线打崩过一次 —— 它本来就不在白名单里，
    # 于是既没进下载队列、又被当网页读，最后 MemoryError。缺的形态一并补上。
    ".msix", ".msixbundle", ".appx", ".msi", ".whl", ".jar", ".zst",
    ".ggml", ".orc", ".lz4", ".zstd", ".tgz", ".tbz2",
}

# Content-Type → 落盘扩展名。没有扩展名的下载链接（/download?id=123）靠它命名，
# 否则 worth() 因「无扩展名」直接拒收，资源就在眼皮底下漏掉。
CTYPE_EXT = {
    "application/pdf": ".pdf", "application/zip": ".zip",
    "application/x-7z-compressed": ".7z", "application/x-rar-compressed": ".rar",
    "application/gzip": ".gz", "application/x-tar": ".tar",
    "application/x-zstd": ".zst", "application/vnd.rar": ".rar",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/epub+zip": ".epub", "application/x-msdownload": ".exe",
    "application/vnd.microsoft.portable-executable": ".exe",
    "application/vnd.android.package-archive": ".apk",
    "application/x-apple-diskimage": ".dmg", "application/x-deb": ".deb",
    "application/x-rpm": ".rpm", "application/json": ".json",
    "text/csv": ".csv", "text/plain": ".txt", "text/markdown": ".md",
    "application/octet-stream": "", "binary/octet-stream": "",
}
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".ico", ".bmp"}
SKIP_EXT = {".css", ".js", ".mjs", ".map", ".woff", ".woff2", ".ttf", ".eot"}
NOISE_PATH = re.compile(
    r"(favicon|/logo|[-_]logo|/icon|[-_]icon|sprite|avatar|placeholder|"
    r"pixel|beacon|tracking|analytics|gtag|gtm\.js|/ads?/|doubleclick|"
    r"status\.json|/health|/metrics|1x1|/fonts?/)", re.I)
BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_EXT_RE = re.compile(r"(\.[a-z0-9]{1,8})$", re.I)

# ------------------------------------------------------------ 资源类型分类
# 主人 2026-11-05 定调：**下载资源要分流、要分类，不能阻挡**。
# 所以这里不是「挡掉大文件」，而是先认清它是什么，再按类归位：
# 安装包归安装包、数据集归数据集、模型权重归模型 —— 各类的下载策略、价值判断都不同。
_KIND = {
    "installer": {".msix", ".msixbundle", ".appx", ".msi", ".exe", ".dmg",
                  ".pkg", ".apk", ".deb", ".rpm", ".iso", ".cab"},
    "model": {".gguf", ".ggml", ".safetensors", ".onnx", ".ckpt", ".pth",
              ".pt", ".bin", ".h5", ".tflite", ".msgpack"},
    "dataset": {".csv", ".tsv", ".parquet", ".arrow", ".feather", ".hdf5",
                ".npy", ".npz", ".sqlite", ".db", ".jsonl", ".ndjson", ".orc"},
    "doc": {".pdf", ".epub", ".mobi", ".djvu", ".docx", ".doc", ".pptx",
            ".ppt", ".xlsx", ".xls", ".ods", ".md", ".rst", ".adoc", ".txt",
            ".tex", ".bib"},
    "media": {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".mp4", ".mkv",
              ".avi", ".webm", ".srt", ".vtt", ".jpg", ".jpeg", ".png",
              ".webp", ".gif", ".svg"},
    "archive": {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz",
                ".zst", ".zstd", ".lz4", ".tbz2"},
    "code": {".py", ".ipynb", ".js", ".ts", ".java", ".rs", ".go", ".c",
             ".cpp", ".h", ".patch", ".diff", ".sql", ".sh", ".yml", ".yaml",
             ".toml", ".json", ".xml", ".whl", ".jar"},
}
_EXT_KIND = {e: k for k, s in _KIND.items() for e in s}
# 站点自身的说明书，不是「资料」：sitemap / RSS / opensearch / RSD / manifest。
# 它们该被喂给发现层（xml 里全是 URL），而不是当文件囤起来。
# 实测依据（2026-11-05，829 条 xml 抽样 400）：feed 188、index.xml 51、
# osd.xml 46、opensearch 41、wlwmanifest 12、sitemap 8 —— 九成是这类。
_SITE_META = re.compile(
    r"(^|/)(sitemap[^/]*\.xml|[\w.-]*feed[\w.-]*\.xml|rss\.xml|atom\.xml|"
    r"opensearch\.xml|osd\.xml|rsd\.xml|wlwmanifest\.xml|manifest\.xml|"
    r"robots\.txt|humans\.txt|security\.txt|blog-feed\.xml|all\.atom\.xml)$", re.I)
_CT_KIND = (
    ("image/", "media"), ("audio/", "media"), ("video/", "media"),
    ("font/", "font"), ("text/csv", "dataset"), ("application/pdf", "doc"),
    ("application/zip", "archive"), ("application/gzip", "archive"),
    ("application/x-tar", "archive"), ("application/x-7z", "archive"),
    ("application/x-rar", "archive"), ("application/x-msdownload", "installer"),
    ("application/vnd.android.package-archive", "installer"),
    ("application/octet-stream", "binary"),
)


def asset_kind(url="", ctype="", filename=""):
    """认清这是什么资源。宁可给 unknown，也不猜错 —— 分错类比不分类更麻烦。

    判定顺序：站点说明书 → 扩展名 → Content-Type。
    说明书放最前，是因为它们也常带 .xml/.txt 后缀，先按后缀判会全被归进 code。
    """
    try:
        path = urllib.parse.urlsplit(url).path if "://" in (url or "") else (url or "")
    except Exception:
        path = url or ""
    for cand in (path, filename or ""):
        if cand and _SITE_META.search(cand.lower()):
            return "feed"
    for cand in (url, filename):
        if not cand:
            continue
        try:
            ext = (_EXT_RE.search(urllib.parse.urlsplit(cand).path).group(1).lower()
                   if "://" in cand else
                   (_EXT_RE.search(cand).group(1).lower()
                    if _EXT_RE.search(cand) else ""))
        except Exception:
            ext = ""
        if ext and ext in _EXT_KIND:
            return _EXT_KIND[ext]
        if ext and ext in WANT_EXT:
            return "other"
    c = (ctype or "").split(";")[0].strip().lower()
    for pre, k in _CT_KIND:
        if c.startswith(pre):
            return k
    if c in CTYPE_EXT and CTYPE_EXT[c] in _EXT_KIND:
        return _EXT_KIND[CTYPE_EXT[c]]
    return "unknown"


DEFAULT_MAX_SIZE = 64 * 1024 * 1024       # 单文件 64 MB 上限
DEFAULT_CHUNK = 512 * 1024                # 自研分段器每段 512 KB
_print_lock = threading.Lock()
_manifest_lock = threading.Lock()
_db_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(msg, flush=True)


# ---------------------------------------------------------------- 状态层
def connect():
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure():
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(PARTS, exist_ok=True)
    c = connect()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS assets(
            url TEXT PRIMARY KEY, key TEXT, host TEXT, ext TEXT, src TEXT,
            path TEXT, size INTEGER DEFAULT 0, expected INTEGER DEFAULT 0,
            sha256 TEXT, status TEXT DEFAULT 'pending', tries INTEGER DEFAULT 0,
            err TEXT, ts REAL, done_ts REAL);
        CREATE INDEX IF NOT EXISTS ix_assets_status ON assets(status);
        CREATE TABLE IF NOT EXISTS scanned(file TEXT PRIMARY KEY, nlines INTEGER);
    """)
    # 类型列是后加的：老库直接 ALTER 补上，不重建表（已经下过的文件不能丢）
    cols = [r[1] for r in c.execute("PRAGMA table_info(assets)")]
    if "kind" not in cols:
        c.execute("ALTER TABLE assets ADD COLUMN kind TEXT DEFAULT ''")
        c.execute("CREATE INDEX IF NOT EXISTS ix_assets_kind ON assets(kind)")
        # 老行回填：按 URL 扩展名重新认一遍类型
        rows = c.execute("SELECT url, ext FROM assets WHERE kind IS NULL OR kind=''").fetchall()
        for u, _e in rows:
            c.execute("UPDATE assets SET kind=? WHERE url=?", (asset_kind(u), u))
    c.commit()
    c.close()


def reclassify(force=False):
    """按当前规则重认一遍类型。规则细化后必须能回刷，否则旧账永远停在旧分类上。

    默认只补空的；force=True 全量重算（规则升级后用）。
    """
    ensure()
    c = connect()
    sql = ("SELECT url,ext,kind FROM assets" if force
           else "SELECT url,ext,kind FROM assets WHERE kind IS NULL OR kind=''")
    rows = c.execute(sql).fetchall()
    n = 0
    for u, _e, k in rows:
        nk = asset_kind(u)
        if nk != k:
            c.execute("UPDATE assets SET kind=? WHERE url=?", (nk, u))
            n += 1
    c.commit()
    c.close()
    return n


def _norm(url):
    u = (url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return ""
    try:
        p = urllib.parse.urlsplit(u)
        if not p.hostname:
            return ""
        return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(),
                                        p.path, p.query, ""))  # 去 fragment
    except Exception:
        return ""


def worth(url, include_images=False):
    """价值过滤：只放行可能成为知识库资料的文件，排除每页都有的图标噪声。"""
    u = _norm(url)
    if not u:
        return False
    try:
        p = urllib.parse.urlsplit(u)
        m = _EXT_RE.search(p.path)
        ext = m.group(1).lower() if m else ""
    except Exception:
        return False
    if ext in SKIP_EXT or (ext in IMG_EXT and not include_images):
        return False
    if ext not in WANT_EXT and not (include_images and ext in IMG_EXT):
        return False
    if NOISE_PATH.search(p.path):
        return False
    return True


def _key(url):
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def enqueue(url, src="", include_images=False):
    """入队一条线索。返回是否新增（URL 主键天然去重，重复出现不重置已完成状态）。"""
    u = _norm(url)
    if not u or not worth(u, include_images):
        return False
    p = urllib.parse.urlsplit(u)
    ext = (_EXT_RE.search(p.path).group(1).lower()
           if _EXT_RE.search(p.path) else "")
    with _db_lock:
        c = connect()
        try:
            cur = c.execute(
                "INSERT OR IGNORE INTO assets(url,key,host,ext,src,status,ts,kind) "
                "VALUES(?,?,?,?,?,'pending',?,?)",
                (u, _key(u), p.hostname or "", ext, src[:120], time.time(),
                 asset_kind(u)))
            n = cur.rowcount
            c.commit()
            return bool(n)
        finally:
            c.close()


def enqueue_direct(url, src="", ctype="", filename=""):
    """响应头已确认「这是文件」时直接入队，不靠扩展名猜。

    与 enqueue() 的区别只有一处：**跳过 worth() 的扩展名白名单**。
    原因：`/download?id=123`、`/getfile?fid=abc` 这类链接没有扩展名，
    worth() 会一律拒收，但它们恰恰是最常见的真实资源入口
    （踩过的实例：Blender 下载页、TeamViewer 下载页）。
    噪声路径与样式脚本仍被排除，避免把 favicon / 埋点灌进来。

    返回 (是否新增, 补全后的 url 或 ""）。
    """
    u = _norm(url)
    if not u:
        return False, ""
    try:
        p = urllib.parse.urlsplit(u)
    except Exception:
        return False, ""
    if NOISE_PATH.search(p.path or ""):
        return False, ""
    m = _EXT_RE.search(p.path or "")
    ext = m.group(1).lower() if m else ""
    if not ext and filename:
        m2 = _EXT_RE.search(filename)
        ext = m2.group(1).lower() if m2 else ""
    if not ext:
        ext = CTYPE_EXT.get((ctype or "").split(";")[0].strip().lower(), "")
    if ext in SKIP_EXT:
        return False, u
    with _db_lock:
        c = connect()
        try:
            cur = c.execute(
                "INSERT OR IGNORE INTO assets(url,key,host,ext,src,status,ts,kind) "
                "VALUES(?,?,?,?,?,'pending',?,?)",
                (u, _key(u), p.hostname or "", ext,
                 ("%s|%s" % (src[:100], ctype[:40])).strip("|"), time.time(),
                 asset_kind(u, ctype, filename)))
            n = cur.rowcount
            c.commit()
            return bool(n), u
        finally:
            c.close()


def _scan_file(path, kind, include_images=False):
    """按行数增量扫描，避免 records 分片越滚越大时每轮全量重读。"""
    added = 0
    try:
        c = connect()
        row = c.execute("SELECT nlines FROM scanned WHERE file=?", (path,)).fetchone()
        seen = row[0] if row else 0
        c.close()
    except Exception:
        seen = 0
    n = 0
    with open(path, encoding="utf-8", errors="ignore") as f:
        for i, line in enumerate(f):
            n = i + 1
            if i < seen:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if kind == "discovered":
                if obj.get("type") == "asset":
                    added += int(enqueue(obj.get("value", ""),
                                         "discovered:" + str(obj.get("from", "")),
                                         include_images))
            else:
                src = "records:" + str(obj.get("domain", ""))
                for a in (obj.get("assets") or [])[:200]:
                    added += int(enqueue(a.get("url", "") if isinstance(a, dict) else a,
                                         src, include_images))
                # 冷门资料更多是页面里的下载链接，而不是 network 面板里的图标
                for l in (obj.get("links") or [])[:400]:
                    u = l.get("url", "") if isinstance(l, dict) else l
                    if not isinstance(u, str):
                        continue
                    try:
                        hit = _EXT_RE.search(urllib.parse.urlsplit(u).path)
                    except Exception:
                        continue
                    if hit:
                        added += int(enqueue(u, src, include_images))
    with _db_lock:
        c = connect()
        c.execute("INSERT OR REPLACE INTO scanned(file,nlines) VALUES(?,?)", (path, n))
        c.commit()
        c.close()
    return added


def scan(include_images=False, quick=False):
    """把 discovered + records 的新线索入队。返回新增条数。"""
    ensure()
    added = 0
    files = []
    if os.path.exists(DISCOVERED):
        files.append((DISCOVERED, "discovered"))
    files += [(p, "records") for p in sorted(glob.glob(os.path.join(RECORDS, "*.jsonl")))]
    for path, kind in files:
        try:
            added += _scan_file(path, kind, include_images)
        except Exception as e:
            log("  [assets] 扫描 %s 失败：%s" % (os.path.basename(path), str(e)[:80]))
    return added


# 🔴 只把「内容本身就是信息」的类型搬回硬盘（2026-11-05 主人定的原则）：
#     文章、报告、文档、代码、数据集 —— 读的就是文字本身，必须能检索、能分析。
#     视频、音频、安装包、压缩包、模型权重 —— 文字只是一个地址，知道就行。
#   为什么不下载：字节只是拷贝，拷贝不产生新信息；真正值钱的是
#   「知道它存在、在哪、多大、什么类型、和谁有关」。BT 种子几 KB 指向几百 GB，
#   图书馆的目录比书库更常被翻 —— 天网要做目录，不是做书库。
#   需要实物时用 --fetch <url> 单条按需取，不入库、不占常驻空间。
KEEP_LOCAL = {"doc", "code", "dataset"}

# 扩展名级白名单：光看 kind 还不够。
#   .json / .xml / .yml 在 code 类里占了大半，但它们多是被抓到的 API 响应和
#   站点配置文件，不是「文章」也不是「源码」—— 读它们不产生信息，只占地方。
# 反过来 doc 里的 pdf/md/txt/djvu/epub 是真正值得存的正文。
KEEP_EXT = {
    ".pdf", ".md", ".markdown", ".txt", ".rst", ".adoc", ".epub", ".mobi",
    ".djvu", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".odt",
    ".tex", ".bib",
    ".py", ".ipynb", ".patch", ".diff", ".sql", ".sh",
    ".c", ".h", ".cpp", ".hpp", ".rs", ".go", ".java", ".js", ".ts",
    ".csv", ".tsv", ".parquet",
}


def mark_known():
    """把不该占硬盘的从 pending 转成 known：仍在账本里，只是不再排队下载。

    known 不是失败、也不是跳过 —— 它是「已登记」。查询、检索、关系推理全都照用，
    只是不把字节搬回家。账本一条不少，磁盘一个字节不多花。
    判断依据两道闸：先看类型（KEEP_LOCAL），再看扩展名（KEEP_EXT）。
    没见过扩展名的（URL 无后缀）一律按 known 处理 —— 不知道是什么就先别搬，
    但账要留着，随时能按需取。
    """
    ensure()
    c = connect()
    ks = sorted(KEEP_LOCAL)
    es = sorted(KEEP_EXT)
    ph1, ph2 = ",".join("?" * len(ks)), ",".join("?" * len(es))
    n = c.execute(
        "UPDATE assets SET status='known', "
        "err=COALESCE(NULLIF(err,''),'known-only: 只登记不下载') "
        "WHERE status='pending' AND ("
        "  COALESCE(NULLIF(kind,''),'unknown') NOT IN (%s)"
        "  OR lower(COALESCE(ext,'')) NOT IN (%s))" % (ph1, ph2),
        ks + es).rowcount
    c.commit()
    c.close()
    return n


def pending(limit, retry_failed=False, kinds=None, exts=None):
    c = connect()
    if retry_failed:
        c.execute("UPDATE assets SET status='pending' WHERE status='failed' AND tries<5")
        c.commit()
    # 两道闸都要过：类型 + 扩展名。mark_known 已经把不该下的转走了，
    # 这里再筛一次是防「手动改回 pending」或新扫描进来的漏网。
    ks = sorted(kinds if kinds is not None else KEEP_LOCAL)
    es = sorted(exts if exts is not None else KEEP_EXT)
    ph1, ph2 = ",".join("?" * len(ks)), ",".join("?" * len(es))
    rows = c.execute(
        "SELECT url,key,host,ext,src,status,tries,expected,path FROM assets "
        "WHERE status='pending' AND COALESCE(NULLIF(kind,''),'unknown') IN (%s) "
        "AND lower(COALESCE(ext,'')) IN (%s) "
        "ORDER BY tries ASC, ts DESC LIMIT ?" % (ph1, ph2),
        ks + es + [int(limit)]).fetchall()
    c.close()
    return rows


def fetch_now(url, out_dir=None, name=None):
    """按需单条取用：给一个 URL，立刻取回来。

    这是「知道它在哪」和「现在就要用」之间的那一步。
    不排队、不进 pending、不参与 mark_known 判定 —— 取完就落在 _kb/fetch/，
    下次要同一个文件直接命中，不再走网络。
    """
    root = os.path.dirname(os.path.abspath(__file__))
    out_dir = out_dir or os.path.join(root, "_kb", "fetch")
    os.makedirs(out_dir, exist_ok=True)
    p = urllib.parse.urlsplit(url)
    nm = name or os.path.basename(p.path) or "index"
    nm = BAD_NAME.sub("_", nm)[:120] or "index"
    if "." not in nm:
        m = _EXT_RE.search(p.path)
        nm += (m.group(1) if m else ".bin")
    dest = os.path.join(out_dir, nm)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return True, dest, os.path.getsize(dest), "已在本地，直接用"
    cmd = ["aria2c", "-c", "-x8", "-s8", "--max-tries=3", "--timeout=20",
           "--console-log-level=warn", "--summary-interval=0",
           "-d", out_dir, "-o", nm, url]
    try:
        subprocess.run(cmd, capture_output=True, timeout=1800,
                       stdin=subprocess.DEVNULL,
                       startupinfo=netpolicy._hidden_startupinfo(),
                       creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:
        return False, dest, 0, "%s: %s" % (type(e).__name__, str(e)[:80])
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return True, dest, os.path.getsize(dest), ""
    return False, dest, 0, "未取到（断点已保留，可重试续传）"


def status():
    if not os.path.exists(DB):
        print("资源队列尚未初始化（跑一次 --scan）")
        return
    c = connect()
    by = dict(c.execute("SELECT status,COUNT(*) FROM assets GROUP BY status").fetchall())
    total = c.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    done = c.execute("SELECT COUNT(*),COALESCE(SUM(size),0) FROM assets "
                     "WHERE status='done'").fetchone()
    top = c.execute("SELECT ext,COUNT(*) FROM assets GROUP BY ext "
                    "ORDER BY COUNT(*) DESC LIMIT 10").fetchall()
    kinds = c.execute("SELECT COALESCE(NULLIF(kind,''),'unknown'),COUNT(*) "
                      "FROM assets GROUP BY 1 ORDER BY 2 DESC").fetchall()
    c.close()
    print("资源队列 %d 条 | 状态 %s" % (total, by))
    print("已落盘 %d 个文件 / %.2f MB" % (done[0], done[1] / 1048576.0))
    known = by.get("known", 0)
    if known:
        print("只登记 %d 条（视频/音频/安装包/压缩包/模型/站点文件）—— 知道在哪就够了，不占硬盘"
              % known)
    print("扩展名 TOP:", top)
    print("类型分布:", kinds)
    print("成品目录:", OUT)


def verify():
    """复核成品：文件丢了/大小变了就重排队（.part 断点仍在，不会从头下）。"""
    ensure()
    c = connect()
    rows = c.execute("SELECT url,path,size FROM assets WHERE status='done'").fetchall()
    bad = 0
    for u, p, sz in rows:
        if not p or not os.path.exists(p) or os.path.getsize(p) != sz:
            c.execute("UPDATE assets SET status='pending',size=0 WHERE url=?", (u,))
            bad += 1
    c.commit()
    c.close()
    print("复核 %d 个成品，需重下 %d 个（断点保留）" % (len(rows), bad))
    return bad


# ---------------------------------------------------------------- 出网
def _clean_env():
    """🔴 清掉所有 *_PROXY：aria2c/curl 默认会继承环境变量，
    那等于偷偷用上主人本地端口。默认纯直连，只有 netpolicy 显式指定才走固定出口。"""
    env = dict(os.environ)
    for k in list(env):
        if k.lower().endswith("_proxy"):
            env.pop(k, None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _egress(host):
    """固定出口：'' = 纯直连；非空 = 远程显式指定，不自动切换。"""
    try:
        return netpolicy.channels(host)[0][1]
    except Exception:
        return ""


def _head(url, timeout=6):
    """拿 Content-Length / Accept-Ranges。HEAD 常被禁，拿不到不阻断下载。"""
    try:
        ops = netpolicy.openers(url)
        if not ops:
            return 0, False
        req = urllib.request.Request(url, method="HEAD",
                                     headers=netpolicy.headers_for(url))
        with ops[0][1].open(req, timeout=timeout) as r:
            h = r.headers or {}
            return int(h.get("Content-Length") or 0), \
                "bytes" in str(h.get("Accept-Ranges") or "").lower()
    except Exception:
        return 0, False


def _safe_name(url, key):
    try:
        name = os.path.basename(urllib.parse.urlsplit(url).path) or "file"
    except Exception:
        name = "file"
    name = BAD_NAME.sub("_", urllib.parse.unquote(name))[:90]
    return "%s_%s" % (key, name)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------- 下载通道
def _aria2(url, part_dir, name, px, timeout=180):
    """首选通道：aria2c 自带 .aria2 控制文件，中断后 -c 可续，分段独立落盘。"""
    exe = shutil.which("aria2c")
    if not exe:
        return False, "aria2c 未安装"
    args = [exe, "-c", "-x8", "-s8", "--min-split-size=1M",
            "--max-tries=2", "--retry-wait=2", "--timeout=20",
            "--connect-timeout=8", "--file-allocation=none",
            "--auto-file-renaming=false", "--allow-overwrite=true",
            "--check-certificate=false", "--summary-interval=0", "--quiet",
            "--user-agent=" + netpolicy.UA,
            "--referer=" + (netpolicy.headers_for(url).get("Referer") or ""),
            "--header=Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
            "-d", part_dir, "-o", name]
    if px:
        args.append("--all-proxy=" + px)   # 远程显式指定的固定出口
    args.append(url)
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout,
                           env=_clean_env(), stdin=subprocess.DEVNULL,
                           startupinfo=netpolicy._hidden_startupinfo(),
                           creationflags=subprocess.CREATE_NO_WINDOW)
        p = os.path.join(part_dir, name)
        if r.returncode == 0 and os.path.exists(p) and os.path.getsize(p) > 0:
            return True, ""
        return False, "aria2c rc=%d %s" % (
            r.returncode, (r.stderr or b"").decode("utf-8", "ignore")[-140:])
    except Exception as e:
        return False, "aria2c: %s %s" % (type(e).__name__, str(e)[:80])


def _range_download(url, part_dir, name, expected, chunk=DEFAULT_CHUNK):
    """自研分段器（aria2c 不可用/不支持 Range 时的第二层）。

    每段一个独立文件 seg-00000 ……；已完整段跳过，半截段用 Range 接着追加。
    全部完成后才拼接成成品，并校验总大小 —— 一断绝不废掉前面所有段。
    """
    segdir = os.path.join(part_dir, name + ".segments")
    os.makedirs(segdir, exist_ok=True)
    n = max(1, (expected + chunk - 1) // chunk)
    ops = netpolicy.openers(url)
    if not ops:
        return False, "无可用 opener"
    op = ops[0][1]
    for i in range(n):
        start = i * chunk
        end = min(expected, start + chunk) - 1
        want = end - start + 1
        sp = os.path.join(segdir, "seg-%05d" % i)
        have = os.path.getsize(sp) if os.path.exists(sp) else 0
        if have == want:
            continue
        if have > want:               # 半截段异常膨胀，重下这一段
            os.remove(sp)
            have = 0
        hdr = netpolicy.headers_for(url)
        hdr["Range"] = "bytes=%d-%d" % (start + have, end)
        req = urllib.request.Request(url, headers=hdr)
        try:
            with op.open(req, timeout=25) as r:
                # 服务器忽略 Range 回 200 时，写进第一段会变成永远对不上的坏段
                if getattr(r, "status", 206) != 206:
                    return False, "服务器不支持 Range（返回 %s），交给 curl 续传兜底" \
                        % getattr(r, "status", "?")
                with open(sp, "ab" if have else "wb") as f:
                    while True:
                        b = r.read(1 << 16)
                        if not b:
                            break
                        f.write(b)
        except Exception as e:
            return False, "段 %d 未完（已保留 %d 字节断点）：%s" % (
                i, os.path.getsize(sp) if os.path.exists(sp) else 0,
                type(e).__name__)
        if os.path.getsize(sp) != want:
            return False, "段 %d 大小不符，保留断点待续" % i
    final = os.path.join(part_dir, name)
    with open(final, "wb") as out:
        for i in range(n):
            with open(os.path.join(segdir, "seg-%05d" % i), "rb") as f:
                shutil.copyfileobj(f, out, 1 << 20)
    real = os.path.getsize(final)
    if real != expected:
        return False, "拼接总大小 %d != 预期 %d" % (real, expected)
    shutil.rmtree(segdir, ignore_errors=True)
    return True, ""


def _curl_resume(url, part, px, timeout=180):
    """最后兜底：服务器不支持 Range 时用 curl -C - 续传（同样不是一断全废）。"""
    # 不加 --compressed：解压后的字节数和 Content-Length 对不上，会误判下载不完整
    args = ["curl", "-s", "-L", "-C", "-", "-A", netpolicy.UA,
            "--max-time", str(int(timeout)), "-o", part]
    args += (["-x", px] if px else ["--noproxy", "*"])
    args.append(url)
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout + 10,
                           env=_clean_env(), stdin=subprocess.DEVNULL,
                           startupinfo=netpolicy._hidden_startupinfo(),
                           creationflags=subprocess.CREATE_NO_WINDOW)
        return (os.path.exists(part) and os.path.getsize(part) > 0,
                (r.stderr or b"").decode("utf-8", "ignore")[-100:])
    except Exception as e:
        return False, str(e)[:100]


def _record_final(rec, path, size, digest):
    with _manifest_lock:
        with open(MANIFEST, "a", encoding="utf-8") as f:
            f.write(json.dumps({"url": rec["url"], "host": rec["host"],
                                "ext": rec["ext"], "src": rec["src"],
                                "path": path, "size": size, "sha256": digest,
                                "ts": time.time()}, ensure_ascii=False) + "\n")


def download_one(row, max_size=DEFAULT_MAX_SIZE, use_aria2=True,
                 chunk=DEFAULT_CHUNK, include_images=False):
    """下载一条。任何失败都保留 part/.aria2/分段文件，下次接着续。"""
    (url, key, host, ext, src, _st, tries, _expected, _path) = row
    ensure()
    px = _egress(host)
    length, ranges = _head(url)
    if length and length > max_size:
        _update(url, "skipped_big", err="Content-Length %d > %d" % (length, max_size),
                expected=length, tries=tries + 1)
        return "skipped_big", 0

    name = _safe_name(url, key)
    part_dir = os.path.join(PARTS, key)
    os.makedirs(part_dir, exist_ok=True)
    part = os.path.join(part_dir, name)
    host_dir = os.path.join(OUT, BAD_NAME.sub("_", host)[:60])
    os.makedirs(host_dir, exist_ok=True)
    final = os.path.join(host_dir, name)

    if os.path.exists(final) and os.path.getsize(final) > 0 and (
            not length or os.path.getsize(final) == length):
        _update(url, "done", path=final, size=os.path.getsize(final),
                expected=length, sha=_sha256(final), tries=tries, done=True)
        return "done", os.path.getsize(final)

    err = ""
    ok = False
    if use_aria2:
        ok, err = _aria2(url, part_dir, name, px)
    if not ok and length and ranges:
        ok, e2 = _range_download(url, part_dir, name, length, chunk)
        err = (err + " | " + e2).strip(" |")
    if not ok:
        ok, e3 = _curl_resume(url, part, px)
        err = (err + " | " + e3).strip(" |")

    if not ok or not os.path.exists(part) or os.path.getsize(part) == 0:
        _update(url, "failed", err=err[:200], expected=length, tries=tries + 1)
        return "failed", 0
    size = os.path.getsize(part)
    if length and size != length:
        # 长度对不上就当没下完：保留 part，下一轮 -c 续，绝不冒充成功
        _update(url, "failed", err="大小 %d != 预期 %d（保留断点）" % (size, length),
                expected=length, tries=tries + 1)
        return "failed", 0
    if size > max_size:
        _update(url, "skipped_big", err="实际 %d > 上限" % size,
                expected=length, tries=tries + 1)
        return "skipped_big", 0
    os.replace(part, final)
    # 清掉同批的 aria2 控制文件（成品已完成，留着会让下次误判为未完成）
    for suffix in (".aria2", ".segments"):
        p = part + suffix
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
    digest = _sha256(final)
    _update(url, "done", path=final, size=size, expected=length,
            sha=digest, tries=tries, done=True)
    _record_final({"url": url, "host": host, "ext": ext, "src": src},
                  final, size, digest)
    return "done", size


def _update(url, status_, path="", size=0, expected=0, sha="", err="",
            tries=0, done=False):
    with _db_lock:
        c = connect()
        c.execute("UPDATE assets SET status=?,path=?,size=?,expected=?,sha256=?,"
                  "err=?,tries=?,done_ts=? WHERE url=?",
                  (status_, path, size, expected, sha, err[:200], tries,
                   time.time() if done else None, url))
        c.commit()
        c.close()


# ---------------------------------------------------------------- 批次
def run_once(limit=30, workers=3, max_size=DEFAULT_MAX_SIZE,
             max_total_mb=1024, use_aria2=True, retry_failed=False,
             include_images=False, scan_first=True, log_fn=log):
    """一轮下载。返回统计 dict（供 pipeline.py 直接调用，不另起进程）。"""
    ensure()
    added = scan(include_images) if scan_first else 0
    # 先把「只登记不下载」的类型归位，再来挑真正要搬的
    known_n = mark_known()
    rows = pending(limit, retry_failed=retry_failed)
    if log_fn and known_n:
        log_fn("  [assets] %d 条转为「只登记不下载」（视频/安装包/压缩包等）" % known_n)
    if not rows:
        return {"scanned": added, "picked": 0, "done": 0, "failed": 0, "bytes": 0}
    if log_fn:
        log_fn("  [assets] 入队 +%d，本轮挑 %d 条（并发 %d，出口 %s）"
               % (added, len(rows), workers,
                  _egress(rows[0][2]) or "纯直连(固定)"))
    got = {"done": 0, "failed": 0, "skipped_big": 0, "skipped": 0}
    total = [0]
    tlock = threading.Lock()

    def job(r):
        with tlock:
            if total[0] >= max_total_mb * 1048576:
                return ("skipped", 0)
        st, sz = download_one(r, max_size=max_size, use_aria2=use_aria2,
                              include_images=include_images)
        with tlock:
            total[0] += sz
        return (st, sz)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for st, sz in ex.map(job, rows):
            got[st] = got.get(st, 0) + 1
    if log_fn:
        log_fn("  [assets] 落盘 %d 个 / %.2f MB，失败 %d（断点保留），超限 %d"
               % (got["done"], total[0] / 1048576.0,
                  got["failed"], got["skipped_big"]))
    return {"scanned": added, "picked": len(rows), "done": got["done"],
            "failed": got["failed"], "skipped_big": got["skipped_big"],
            "bytes": total[0]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="只入队不下载")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--max-size", type=int, default=DEFAULT_MAX_SIZE // 1048576,
                    help="单文件上限 MB")
    ap.add_argument("--max-total-mb", type=int, default=1024)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--no-aria2", action="store_true", help="强制走自研分段器")
    ap.add_argument("--include-images", action="store_true")
    ap.add_argument("--url", nargs="*", default=[])
    ap.add_argument("--fetch", nargs="+", default=[],
                    help="按需单条取用：给 URL 立刻取回，不排队、不占常驻空间")
    a = ap.parse_args()

    ensure()
    if a.fetch:
        for u in a.fetch:
            ok, p, sz, msg = fetch_now(u)
            print("  %s %-40s %8.1f KB  %s"
                  % ("取到" if ok else "未取", os.path.basename(p)[:40],
                     sz / 1024.0, msg))
        return
    if a.status:
        status()
        return
    if a.verify:
        verify()
        return
    if a.scan:
        print("入队新增 %d 条" % scan(a.include_images))
        status()
        return
    if a.url:
        for u in a.url:
            enqueue(u, "manual", a.include_images)
        print("手动入队 %d 条（不合价值过滤的会被跳过）" % len(a.url))
    st = run_once(limit=a.limit, workers=a.workers,
                  max_size=a.max_size * 1048576,
                  max_total_mb=a.max_total_mb, use_aria2=not a.no_aria2,
                  retry_failed=a.retry_failed, include_images=a.include_images)
    print("本轮结果:", st)
    status()


if __name__ == "__main__":
    main()
