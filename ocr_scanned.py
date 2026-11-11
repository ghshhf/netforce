# -*- coding: utf-8 -*-
"""netforce / ocr_scanned.py —— 扫描版 PDF 的渐进式 OCR（进度存盘、可无限续跑）

34 份扫描 PDF 共 6848 页，一次性 OCR 要按小时算。这里做成常驻循环的一个动作：
每轮限时跑一批页，进度落在 asset_index.db 的 ocr_page / ocr_state 两张表，
断点续跑；某文件全部页完成后自动合并正文、重打标签、更新全文索引。

写回 asset_index.db 后，autoloop 的指纹观察会发现该文件变化，
自动执行 parse_all --stage asset_docs --force 把新正文同步进 corpus.db。

用法：
    python ocr_scanned.py --pages 3             # 单轮，最多 3 页（冒烟用）
    python ocr_scanned.py --max-seconds 45     # 单轮，限时 45 秒（autoloop 用）
    python ocr_scanned.py --stats              # 看进度
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(HERE, "_kb")
DB = os.path.join(KB, "asset_index.db")
sys.path.insert(0, HERE)

DPI = 130
MAX_PAGES_PER_FILE = 600       # 单文件最多 OCR 600 页，防止超长刊物独占队列


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect():
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("""CREATE TABLE IF NOT EXISTS ocr_page(
        file TEXT, page INTEGER, text TEXT, chars INTEGER, ts TEXT,
        PRIMARY KEY(file,page))""")
    c.execute("""CREATE TABLE IF NOT EXISTS ocr_state(
        file TEXT PRIMARY KEY, pages INTEGER, done INTEGER, ts TEXT)""")
    c.commit()
    return c


def init_state(c):
    todo = c.execute("SELECT path,pages FROM asset_docs "
                     "WHERE parse_status='scanned'").fetchall()
    added = 0
    for path, pages in todo:
        if c.execute("SELECT 1 FROM ocr_state WHERE file=?", (path,)).fetchone():
            continue
        real = pages or 0
        try:
            import pymupdf
            with pymupdf.open(path) as d:
                real = d.page_count
        except Exception:
            pass
        if real <= 0:
            # 打不开的扫描件（加密/损坏）：标完成，避免死循环
            c.execute("INSERT OR REPLACE INTO ocr_state VALUES(?,?,?,?)",
                      (path, 0, 1, now()))
            c.execute("UPDATE asset_docs SET parse_status='broken',"
                      "parse_err='scan-only PDF with 0 pages / unreadable' WHERE path=?",
                      (path,))
            continue
        c.execute("INSERT OR REPLACE INTO ocr_state VALUES(?,?,?,?)",
                  (path, min(real, MAX_PAGES_PER_FILE), 0, now()))
        added += 1
    c.commit()
    return added


def engine():
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR()


def render(page):
    import numpy as np
    import cv2
    pix = page.get_pixmap(dpi=DPI, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    elif pix.n == 3:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    return img


def retag_one(c, path, text):
    """合并正文后，用现有标签引擎重打这一条，不重跑全库。"""
    from fine_classify import tag as base_tag
    from parse_assets import load_taxonomy, extra_tags, merge_tags, _lang
    row = c.execute("SELECT host,url,ext,size,sha256,title,pages FROM asset_docs "
                    "WHERE path=?", (path,)).fetchone()
    if not row:
        return
    host, url, ext, size, sha, old_title, pages = row
    fname = os.path.basename(path)
    title = old_title
    if not title or title.startswith(fname[:12]):
        title = fname.replace("_", " ")[:180]
    probe = {"domain": host, "url": url, "ok": True, "status": 200,
             "title": title, "desc": text[:300].replace("\n", " "),
             "text": text, "text_len": len(text), "headings": [],
             "keywords": [], "links": [], "assets": [], "structured": False,
             "rendered": False, "lang": _lang(text), "exts": []}
    try:
        base = base_tag(probe)
    except Exception:
        base = {"category": "", "forms": [], "topics": [], "subtopics": [],
                "evidence": {}, "lang": _lang(text), "quality": 0}
    tax = load_taxonomy()
    hay = " ".join([title, fname, host or "", url or "", text[:12000]]).lower()
    et, ef, ev = extra_tags(tax, hay, host or "", fname)
    tp, subs, forms, ev_all = merge_tags(base, et, ef, ev, title, len(text))
    q = min(100, int(base.get("quality", 0)) + (10 if len(text) > 2000 else 0))
    c.execute("UPDATE asset_docs SET text=?,text_len=?,parse_status='ok_ocr',"
              "parse_err='',scanned=0,category=?,forms=?,topics=?,subtopics=?,"
              "extra_topics=?,evidence=?,quality=?,value=?,lang=?,ts=? WHERE path=?",
              (text, len(text), base.get("category", ""),
               json.dumps(forms, ensure_ascii=False),
               json.dumps(tp, ensure_ascii=False),
               json.dumps(subs, ensure_ascii=False),
               json.dumps(et, ensure_ascii=False),
               json.dumps(ev_all, ensure_ascii=False)[:4000], q,
               "高" if q >= 55 else ("中" if q >= 28 else "低"),
               base.get("lang") or _lang(text), now(), path))
    c.execute("DELETE FROM asset_fts WHERE path=?", (path,))
    c.execute("INSERT INTO asset_fts(path,host,title,topics,text) VALUES(?,?,?,?,?)",
              (path, host, title, json.dumps(tp, ensure_ascii=False), text))


def finish_file(c, path):
    rows = c.execute("SELECT page,text FROM ocr_page WHERE file=? ORDER BY page",
                     (path,)).fetchall()
    text = "\n\n".join("[p%d] %s" % (p, t) for p, t in rows if t)
    text = text[:60000]
    if len(text.strip()) < 30:
        c.execute("UPDATE asset_docs SET parse_status='ocr_empty',"
                  "parse_err='OCR produced no text' WHERE path=?", (path,))
    else:
        retag_one(c, path, text)
    c.execute("UPDATE ocr_state SET done=1,ts=? WHERE file=?", (now(), path))
    c.commit()
    return len(text)


def run_round(c, max_pages, max_seconds):
    init_state(c)
    t0 = time.time()
    eng = None
    done_pages = 0
    finished_files = []
    files = c.execute("SELECT file,pages FROM ocr_state WHERE done=0 "
                      "ORDER BY pages ASC, file ASC").fetchall()
    for path, pages in files:
        if done_pages >= max_pages or (time.time() - t0) > max_seconds:
            break
        have = {r[0] for r in c.execute("SELECT page FROM ocr_page WHERE file=?", (path,))}
        todo = [i for i in range(pages) if i not in have]
        if not todo:
            n = finish_file(c, path)
            finished_files.append(os.path.basename(path))
            print("[ocr] 完成 %s，正文 %d 字符" % (os.path.basename(path)[:52], n), flush=True)
            continue
        if eng is None:
            try:
                eng = engine()
            except Exception as e:
                print("[ocr] 引擎加载失败：%s" % str(e)[:200], flush=True)
                return 0, []
        try:
            import pymupdf
            doc = pymupdf.open(path)
        except Exception as e:
            print("[ocr] 打不开 %s：%s" % (os.path.basename(path)[:50], str(e)[:80]), flush=True)
            c.execute("UPDATE ocr_state SET done=1 WHERE file=?", (path,))
            c.commit()
            continue
        for pno in todo:
            if done_pages >= max_pages or (time.time() - t0) > max_seconds:
                break
            try:
                img = render(doc[pno])
                res, _ = eng(img)
                txt = " ".join(x[1] for x in (res or []) if x and len(x) > 1)
                c.execute("INSERT OR REPLACE INTO ocr_page VALUES(?,?,?,?,?)",
                           (path, pno, txt, len(txt), now()))
                done_pages += 1
                if done_pages % 5 == 0:
                    c.commit()
            except Exception as e:
                c.execute("INSERT OR REPLACE INTO ocr_page VALUES(?,?,?,?,?)",
                          (path, pno, "", 0, now()))
                done_pages += 1
        doc.close()
        c.commit()
        still = c.execute("SELECT COUNT(*) FROM ocr_page WHERE file=?", (path,)).fetchone()[0]
        if still >= pages:
            n = finish_file(c, path)
            finished_files.append(os.path.basename(path))
            print("[ocr] 完成 %s，正文 %d 字符" % (os.path.basename(path)[:52], n), flush=True)
    c.commit()
    return done_pages, finished_files


def stats(c):
    init_state(c)
    total = c.execute("SELECT COALESCE(SUM(pages),0) FROM ocr_state").fetchone()[0]
    ocrd = c.execute("SELECT COUNT(*) FROM ocr_page").fetchone()[0]
    done = c.execute("SELECT COUNT(*) FROM ocr_state WHERE done=1").fetchone()[0]
    allf = c.execute("SELECT COUNT(*) FROM ocr_state").fetchone()[0]
    print("OCR 进度：文件 %d/%d 完成，页面 %d/%d（%.1f%%）" %
          (done, allf, ocrd, total, 100.0 * ocrd / max(1, total)))
    print("  已入库 OCR 正文 %s 字符" %
          format(c.execute("SELECT COALESCE(SUM(chars),0) FROM ocr_page").fetchone()[0], ","))
    for r in c.execute("SELECT parse_status,COUNT(*) FROM asset_docs GROUP BY 1 ORDER BY 2 DESC"):
        print("  asset_docs %-12s %d" % r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=20)
    ap.add_argument("--max-seconds", type=float, default=50)
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    # 🔴 单实例保护：autoloop 每轮会调一次本脚本，独立 OCR 常驻循环也会调，
    #    两个同时跑会重复 OCR 同一页并互相覆盖写（2026-11-12 加）。
    import ctypes
    global _OCR_MUTEX
    _OCR_MUTEX = ctypes.windll.kernel32.CreateMutexW(
        None, False, "Global\\netforce_ocr_scanned_v1")
    if ctypes.windll.kernel32.GetLastError() == 183:
        print("[ocr] 已有实例在跑，本轮跳过", flush=True)
        return
    c = connect()
    if a.stats:
        return stats(c)
    t0 = time.time()
    n, files = run_round(c, a.pages, a.max_seconds)
    print("[ocr] 本轮 %d 页 / %.1fs / 完成文件 %d 个" %
          (n, time.time() - t0, len(files)), flush=True)
    c.close()


if __name__ == "__main__":
    main()
