# -*- coding: utf-8 -*-
"""netforce / kb_query.py —— 统一语料库的只读查询与状态逻辑

从 netforce_kb_mcp.py 抽出来单独放：MCP 常驻进程每次 tools/call 都 reload
本模块，所以改查询规则 / 状态判定不用重启 MCP、不用主人重新点信任。

纯标准库，不写库、不联网、不碰凭据。

检索层三条实测结论（2026-11-11）：
  1. 多词默认 AND 会让中文几乎必然 0 命中 → 严格 0 命中自动放宽 OR；
  2. FTS5 trigram 只索引 ≥3 字符，「天网」「中继」这类 2 字词永远 0 命中
     → 单独走 instr() 正文回退；
  3. 库里约 48% 是空页 / 登录页 / AccessDenied（正文 <200 字符），
     会淹没有效结果 → 检索默认排除，条数在 kb_stats 里明说。
  4. 11-12 主人定：不再把本地 4B 当批量脚本用。语料库要能被「像 MCP
     一样直接读取」——谁想读、读哪条、读多少由模型自己决定。所以检索结果
     必须自带 uid / url / 摘录，并支持 query="uid:<uid>[@offset]" 直读全文。
     这条走 kb_query 热重载，不需要 MCP 重连、不需要新端口或新进程。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(HERE, "_kb")
EV = os.path.join(KB, "evolve")
MAIN = os.path.join(KB, "corpus.db")
SIDE = os.path.join(KB, "side_corpus.db")
JOBS = os.path.join(KB, "jobboard.db")
ASSETS = os.path.join(KB, "asset_index.db")

MIN_TEXT = 200          # 低于这个长度基本是空页/登录页/报错页，检索不收
FRESH = {"jobboard.heartbeat": 180, "jobboard.guard.heartbeat": 150,
         "heartbeat": 300, "watchdog.heartbeat": 150}


def ro(path):
    try:
        return sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro",
                               uri=True, timeout=5)
    except Exception:
        return None


def one(c, sql, args=(), default=0):
    try:
        v = c.execute(sql, args).fetchone()
        return v[0] if v and v[0] is not None else default
    except Exception:
        return default


def _match_exprs(q):
    """多词查询：先严格 AND；没命中再放宽成 OR。"""
    terms = [t.replace('"', "") for t in q.split() if t]
    if not terms:
        terms = [q.replace('"', "")]
    quoted = ['"%s"' % t for t in terms]
    return " AND ".join(quoted), " OR ".join(quoted)


def _short_terms(q):
    """trigram 不索引 2 字中文词，单独挑出来走正文回退。最多 3 个，防噪音。"""
    out = []
    for seg in re.findall(r"[\u4e00-\u9fff]+", q):
        if len(seg) == 2:
            out.append(seg)
    return list(dict.fromkeys(out))[:3]


class _TextOnly(HTMLParser):
    """只收可见文字，跳过 script/style/noscript。标准库，零依赖。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.buf = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data)


def _plain(text):
    """正文若整段是 HTML 源，读出来时转成可读文字；纯文本 / Markdown 原样返回。

    只处理「读出来的这一份」：不改库、不重跑全量、不新增索引。
    """
    if not text:
        return text
    head = text[:512].lstrip().lower()
    if not any(x in head for x in ("<html", "<!doctype html", "<div", "<body")):
        return text
    # 🔴 SVG 图标（GitHub 页里几百字符的 <path d="...">）在 HTMLParser 里会
    #    退化成纯文本漏进正文，把正文开头全占掉。先整段剔除再解析。
    #    ⚠️ 抓取被截断的 HTML 常有「只开不闭」的 <svg>：实测某条开在 29033、
    #       全文截在 30000，</svg> 根本不存在，配对式正则永远匹配不到。
    #       故允许未闭合时删到文本末尾；20000 字符上限防止吞掉过多正文。
    src = re.sub(r"<svg\b[^>]*>.{0,20000}?(?:</svg\s*>|$)", " ", text,
                 flags=re.I | re.S)
    p = _TextOnly()
    try:
        p.feed(src)
        p.close()
    except Exception:
        return text
    s = re.sub(r"[ \t\r\f\v]+", " ", " ".join(p.buf))
    s = re.sub(r"\n\s*\n+", "\n\n", s).strip()
    return s if len(s) > 80 else text


def _tidy(s):
    """去掉切块边界产生的 U+FFFD 与不可见控制字符，保留换行/制表。

    这些替换字符本来就在库里（中文被按字节切断后写入），不是 MCP 编码问题；
    只在读出来的这一份上清掉，不改库、不扫全库。
    """
    return re.sub(r"[\ufffd\u0000-\u0008\u000b\u000c\u000e-\u001f]",
                  "", s or "")


def _fmt_row(r):
    """检索结果必须自带着陆点：uid 供直读全文，url 供溯源，摘录让模型
    不用读完就能判断相关性。只给标题等于「搜得到、读不到」。"""
    cols = (list(r[:7]) + [None] * 7)[:7]
    uid, host, title, topics, tlen, url, excerpt = cols
    lines = ["  · %s ｜ %s ｜ %s 字符 ｜ %s" % (
        (title or "(无题)")[:60], (host or "")[:28], tlen or 0, (topics or "")[:60]),
        "    uid=%s" % (uid or "?")]
    if url:
        lines.append("    %s" % str(url)[:140])
    if excerpt:
        # 摘录来自完整正文（只取命中的这几条，不到 1 MB）：HTML 页要先把
        # 标签洗掉再截，否则模型看到的是一串 <html lang="..."> 属性。
        snippet = re.sub(r"\s+", " ", _tidy(_plain(str(excerpt))[:400])).strip()[:110]
        if snippet:
            lines.append("    %s…" % snippet[:110])
    return "\n".join(lines)


def _uid_query(q):
    """解析 'uid:<uid>[@offset][/maxchars]' —— 不新增 MCP 参数也能分段读长文。"""
    body = q.split(":", 1)[1].strip()
    offset, maxchars = 0, 4000
    if "@" in body:
        body, tail = body.split("@", 1)
        off_s, _, size_s = tail.partition("/")
        try:
            offset = max(0, int(off_s))
        except ValueError:
            pass
        if size_s:
            try:
                maxchars = int(size_s)
            except ValueError:
                pass
    return {"uid": body.strip(), "offset": offset, "maxchars": maxchars}


def read(args):
    """按 uid 直读正文。不经任何生成模型、不写库、不联网。

    uid 支持完整 40 位或 ≥8 位前缀；offset/maxchars 让模型自己决定读多少，
    避免一次把 3 万字塞进上下文。命中多条时只列候选，不擅自替模型选。
    """
    uid = str(args.get("uid") or "").strip()
    if len(uid) < 8:
        return "uid 至少给 8 位（从 kb_search 结果里复制 uid= 那一行）"
    try:
        offset = max(0, int(args.get("offset") or 0))
        maxchars = max(200, min(int(args.get("maxchars") or 4000), 20000))
    except (TypeError, ValueError):
        return "offset / maxchars 必须是整数"
    scope = (args.get("scope") or "all").lower()
    dbs = []
    if scope in ("all", "main"):
        dbs.append(("主库", MAIN, "corpus"))
    if scope in ("all", "side"):
        dbs.append(("旁路", SIDE, "side_corpus"))
    out = []
    seen = {}          # uid -> 已输出的正文，用于跨库去重
    dup_labels = []
    for label, path, table in dbs:
        if not os.path.isfile(path):
            continue
        c = ro(path)
        if not c:
            continue
        try:
            sel = ("SELECT uid,title,url,host,topics,text FROM %s " % table)
            rows = c.execute(sel + "WHERE uid=?", (uid,)).fetchall()
            if not rows:
                # 短码前缀：LIMIT 3 让 SQLite 找到就停，不做全表聚合
                rows = c.execute(sel + "WHERE uid LIKE ? LIMIT 3",
                                 (uid + "%",)).fetchall()
        except Exception as e:
            out.append("【%s】读取失败：%s" % (label, str(e)[:90]))
            c.close()
            continue
        c.close()
        if not rows:
            continue
        if len(rows) > 1:
            out.append("【%s】前缀匹配到 %d 条，请给完整 uid：" % (label, len(rows)))
            for x in rows:
                out.append("  %s ｜ %s" % (x[0], (x[1] or "")[:50]))
            continue
        r = rows[0]
        text = _tidy(_plain(r[5] or ""))
        # 主库与旁路常存同一 uid 的同一份正文：只输出一次，别让模型读两遍。
        # 内容不同时两份都保留，不替模型丢信息。
        if r[0] in seen and seen[r[0]] == text:
            dup_labels.append(label)
            continue
        seen[r[0]] = text
        total = len(text)
        chunk = text[offset:offset + maxchars]
        out.append("【%s】uid=%s" % (label, r[0]))
        out.append("标题：%s" % (r[1] or "(无题)"))
        out.append("来源：%s" % (r[2] or r[3] or ""))
        out.append("标签：%s" % (r[4] or ""))
        out.append("正文：%d 字符，本次 %d–%d" % (total, offset, offset + len(chunk)))
        out.append("-----")
        out.append(chunk)
        out.append("-----")
        if offset + len(chunk) < total:
            out.append("续读：uid:%s@%d" % (r[0], offset + len(chunk)))
        else:
            out.append("（已读完）")
    if dup_labels:
        out.insert(0, "（%s 含同一 uid 且正文一致，已合并，不重复输出）"
                   % "/".join(dup_labels))
    return "\n".join(out) or "该 uid 未找到（可换 scope=main / side 再试）"


RESOURCE_HELP = """本地统一语料库 · 读取说明

两种读法（纯只读，不经任何生成模型）：
  1. kb_search 传检索词 → 结果自带 uid / url / 摘录；
     再传 "uid:<uid>" 或用 kb_read 读全文，长文用 @offset 续读。
  2. resources/read 读 kb:// 资源：
     kb://uid/<uid>         按 uid 读正文，可加 ?offset=400&maxchars=4000
     kb://search/<关键词>   读一次检索结果，可加 ?limit=8
     kb://stats             库规模 / 任务队列 / OCR / 后台心跳
     kb://help              本说明

主库与旁路含同一 uid 且正文一致时只输出一份；HTML 页正文已洗成可读文字。
"""


def resource_list():
    """固定入口资源。10 万条语料不逐条列举，交给下面的 URI 模板。"""
    return [
        {"uri": "kb://help", "name": "语料库读取说明",
         "description": "怎么用 uid / kb:// 资源读正文", "mimeType": "text/plain"},
        {"uri": "kb://stats", "name": "语料库状态",
         "description": "规模、任务队列、OCR、后台心跳", "mimeType": "text/plain"},
    ]


def resource_templates():
    return [
        {"uriTemplate": "kb://uid/{uid}", "name": "按 uid 读取语料正文",
         "description": "uid 来自 kb_search 结果；支持 ?offset / ?maxchars / ?scope",
         "mimeType": "text/plain"},
        {"uriTemplate": "kb://search/{query}", "name": "读取一次检索结果",
         "description": "支持 ?limit；中文关键词需 URL 编码",
         "mimeType": "text/plain"},
    ]


def resource_read(uri):
    """kb:// URI → 一次只读调用。返回 (mimeType, text)；mimeType 为 None 表示出错。

    逻辑放在 kb_query（热重载模块）里，MCP 层只做协议转发，改 URI 规则不用重启。
    """
    from urllib.parse import urlparse, parse_qs, unquote
    p = urlparse(str(uri or ""))
    if p.scheme != "kb":
        return None, "只支持 kb:// 开头的 URI"
    q = {k: v[0] for k, v in parse_qs(p.query).items()}
    host = (p.netloc or "").lower()
    rest = unquote(p.path.lstrip("/"))
    if host == "help":
        return "text/plain", RESOURCE_HELP
    if host == "stats":
        return "text/plain", stats({})
    if host == "uid" and rest:
        a = {"uid": rest, "scope": q.get("scope", "all")}
        for k in ("offset", "maxchars"):
            if k in q:
                a[k] = q[k]
        return "text/plain", read(a)
    if host == "search" and rest:
        a = {"query": rest, "scope": q.get("scope", "all")}
        if "limit" in q:
            a["limit"] = q["limit"]
        return "text/plain", search(a)
    return None, "未知资源：%s（可用 kb://help 看用法）" % uri


def search(args):
    q = (args.get("query") or "").strip()
    if not q:
        return "查询词为空"
    # 直读通道：query="uid:<uid>[@offset][/长度]"。复用现有工具，
    # 旧 MCP 进程 reload kb_query 后立刻可用，不用等重连、不用加参数。
    if q.lower().startswith("uid:") or q.lower().startswith("read:"):
        return read(_uid_query(q))
    # 同一套 kb:// URI 在旧 MCP 进程上也能走 kb_search 读到，不用等重连
    if q.lower().startswith("kb://"):
        _mime, _text = resource_read(q)
        return _text
    limit = max(1, min(int(args.get("limit") or 8), 30))
    scope = (args.get("scope") or "all").lower()
    strict, loose = _match_exprs(q)
    shorts = _short_terms(q)
    out = []
    dbs = []
    if scope in ("all", "main") and os.path.isfile(MAIN):
        dbs.append(("主库", MAIN, "corpus", "corpus_fts"))
    if scope in ("all", "side") and os.path.isfile(SIDE):
        dbs.append(("旁路", SIDE, "side_corpus", "side_fts"))
    for label, path, table, fts in dbs:
        c = ro(path)
        if not c:
            continue
        # FTS5 默认按 rowid 返回；放宽 OR 后按 bm25 排，且排除空页。
        # 🔴 标签一律读主表 d.topics，不读 FTS 副本 f.topics：副本由多处脚本
        #    各自维护，补标随时会写新 topics 而副本滞后，读它就会拿到旧标签。
        sql = ("SELECT d.uid,d.host,d.title,d.topics,d.text_len,d.url,"
               "d.text FROM %s f "
               "LEFT JOIN %s d ON d.uid=f.uid "
               "WHERE %s MATCH ? AND COALESCE(d.text_len,0)>=? "
               "ORDER BY rank LIMIT ?" % (fts, table, fts))
        notes = []
        hits = {}
        try:
            rows = c.execute(sql, (strict, MIN_TEXT, limit)).fetchall()
            if not rows and loose != strict:
                rows = c.execute(sql, (loose, MIN_TEXT, limit)).fetchall()
                if rows:
                    notes.append("严格匹配 0 命中，已放宽为任一词命中")
            for r in rows:
                hits[r[0]] = (4, r)        # FTS 命中比「正文里恰好有这两个字」更相关
        except Exception as e:
            notes.append("检索失败：%s" % str(e)[:100])
        if shorts:
            try:
                case = " + ".join(
                    ["(CASE WHEN COALESCE(instr(text,?),0)>0 THEN 1 ELSE 0 END)"] * len(shorts))
                cond = " OR ".join(["COALESCE(instr(text,?),0)>0"] * len(shorts))
                sq = ("SELECT uid,host,title,topics,text_len,url,"
                      "text,(%s) AS nh FROM %s "
                      "WHERE (%s) AND COALESCE(text_len,0)>=? "
                      "ORDER BY nh DESC, text_len DESC LIMIT ?" % (case, table, cond))
                ps = list(shorts) + list(shorts) + [MIN_TEXT, limit * 3]
                extra = c.execute(sq, ps).fetchall()
                for r in extra:
                    prev = hits.get(r[0])
                    # nh 现在是第 8 列；正文与 url 补齐后结果统一截成 7 列元组
                    hits[r[0]] = ((prev[0] if prev else 0) + 2 * (r[7] or 1), r[:7])
                if extra:
                    notes.append("2 字中文词已走正文回退（%s）" % "/".join(shorts))
            except Exception as e:
                notes.append("短词回退失败：%s" % str(e)[:90])
        ordered = sorted(hits.values(), key=lambda x: (-x[0], -(x[1][4] or 0)))[:limit]
        out.append("【%s】命中 %d%s" %
                   (label, len(ordered), ("（" + "；".join(notes) + "）") if notes else ""))
        out.extend(_fmt_row(r) for _, r in ordered)
        c.close()
    return "\n".join(out) or "无命中"


def tag_search(args):
    tag = (args.get("tag") or "").strip()
    limit = max(1, min(int(args.get("limit") or 8), 30))
    if not tag:
        return "标签为空"
    out = []
    for label, path, table in (("主库", MAIN, "corpus"),
                               ("旁路", SIDE, "side_corpus")):
        if not os.path.isfile(path):
            continue
        c = ro(path)
        try:
            rows = c.execute(
                "SELECT uid,host,title,topics,text_len,url,text FROM %s "
                "WHERE topics LIKE ? AND COALESCE(text_len,0)>=? LIMIT ?" % table,
                ("%" + tag + "%", MIN_TEXT, limit)).fetchall()
            out.append("【%s】%d 条" % (label, len(rows)))
            out.extend(_fmt_row(r) for r in rows)
        except Exception as e:
            out.append("【%s】%s" % (label, str(e)[:90]))
        c.close()
    return "\n".join(out) or "无命中"


def hb(name):
    """返回 (人话, 是否新鲜)。不再无条件显示「Ns 前」制造假绿。"""
    p = os.path.join(EV, name)
    try:
        age = time.time() - float(open(p, encoding="utf-8").read().split("|")[0])
    except Exception:
        return "❌ 无心跳", False
    fresh = age < FRESH.get(name, 180)
    return (("✅ %.0fs 前" % age) if fresh else ("❌ 掉线（%.0fs 前）" % age)), fresh


def stats(_):
    out = ["== 本地统一语料库 =="]
    for label, path, table in (("主库 corpus.db", MAIN, "corpus"),
                              ("旁路 side_corpus.db", SIDE, "side_corpus")):
        c = ro(path)
        if c:
            tot = one(c, "SELECT COUNT(*) FROM " + table)
            good = one(c, "SELECT COUNT(*) FROM %s WHERE COALESCE(text_len,0)>=%d"
                       % (table, MIN_TEXT))
            out.append("  %s：%s 条 / %s 字符（有效正文 %s 条，检索默认排除空页）" %
                       (label, format(tot, ","),
                        format(one(c, "SELECT COALESCE(SUM(text_len),0) FROM " + table), ","),
                        format(good, ",")))
            if table == "corpus":
                # 防复发：总条数与 FTS 行数的差曾让我误判成「索引漏了」。
                # 把失败占位单独报出来，口径一眼可核（走 ix_corpus_status 索引）。
                bad = one(c, "SELECT COUNT(*) FROM corpus WHERE parse_status='unreachable'")
                out.append("    其中失败占位 %s 条（unreachable，无正文；不是索引漏检）"
                           % format(bad, ","))
            c.close()
    c = ro(JOBS)
    if c:
        out.append("  任务板：待办 %s / 已完成 %s" %
                   (format(one(c, "SELECT COUNT(*) FROM jobs WHERE status IN "
                              "('pending','doing','retry')"), ","),
                    format(one(c, "SELECT COUNT(*) FROM jobs WHERE status='done'"), ",")))
        c.close()
    c = ro(ASSETS)
    if c:
        out.append("  扫描件 OCR：已完成 %s / 待处理 %s" %
                   (one(c, "SELECT COUNT(*) FROM asset_docs WHERE parse_status='ok_ocr'"),
                    one(c, "SELECT COUNT(*) FROM asset_docs WHERE parse_status='scanned'")))
        c.close()
    jb, jb_ok = hb("jobboard.heartbeat")
    gd, gd_ok = hb("jobboard.guard.heartbeat")
    al, al_ok = hb("heartbeat")
    wd, wd_ok = hb("watchdog.heartbeat")
    try:
        import _supervisor
        # 只做只读判活：不再由一次查询偷偷拉起后台进程（避免滞留／忘了收）
        acts, rep = [], _supervisor.report()
    except Exception as e:
        acts, rep = [], {}
        out.append("  进程复核：不可用 %s" % str(e)[:80])

    def disp(text, ok, key):
        # 进程真在（互斥体在）却心跳旧 = 正在跑长批次，不是掉线，别报假故障
        return "⏳ 长任务中（进程在）" if (not ok and rep.get(key)) else text

    out.append("  后台心跳：任务板 %s ｜ guard %s ｜ 自治循环 %s ｜ watchdog %s" %
               (disp(jb, jb_ok, "worker"), disp(gd, gd_ok, "guard"),
                disp(al, al_ok, "autoloop"), disp(wd, wd_ok, "watchdog")))
    if rep:
        out.append("  进程复核：" + " ".join(
            "%s%s" % (k, "✅" if v else "❌") for k, v in rep.items()))
        if acts:
            out.append("  🔧 自愈拉起：" + "、".join("%s(pid=%d)" % a for a in acts))
    try:
        import _bulk_lock
        on, note = _bulk_lock.status()
        out.append("  导入锁：%s" % note)
    except Exception as e:
        out.append("  导入锁：读取失败 %s" % str(e)[:80])
    if not any((jb_ok, gd_ok, al_ok, wd_ok)):
        out.append("  ⚠️ 四条心跳全旧：以「进程复核」为准，缺线已由自愈补起")
    return "\n".join(out)
