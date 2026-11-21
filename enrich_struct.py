#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""enrich_struct.py —— 资源结构化增强层（接在爬取落盘之后，让资源自带可检索卡片）

解决的问题：
    过去爬虫爬下来的是一堆乱文本，无法筛选/调用，才靠标签、分类、翻译这些
    「事后取巧」去补救。真正的解法是：**在源头就让每条资源自带结构化摘要**，
    扫一眼就知道「这是什么、有什么用、关键词是什么」，直接可检索。

与 ai_extract.py 的关系：
    ai_extract 是通用的声明式抽取引擎；本模块是它在「资源结构化」这个场景的
    专用封装 —— 固定字段 schema + 元数据兜底 + 批量断点，供渲染流水线调用。

设计要点：
  · 结构化字段固定 4 个（kind / summary_zh / highlights / keywords），
    跨所有资源统一，检索站可直接按列筛选/搜索；
  · **元数据兜底**：og:description / jsonld / desc 直接可当摘要时用，不烧 LLM；
    LLM 只在需要归纳时上，失败静默降级，绝不让一条抽取失败拖垮整批；
  · **只取正文前段**（摘要类任务读开头就够），单块 1 次 LLM/条，不切块并行，
    省算力、避开 8099 单 slot 排队；
  · 增量：只处理还没 ai_struct 的记录，已有的不重抽（断点续跑）。

用法：
    from enrich_struct import structurize, enrich_records
    st, diag = structurize(rec)                    # 单条
    n = enrich_records(records, save=True)         # 批量 + 写回

CLI：
    python enrich_struct.py --selftest            # 自检（不联网）
    python enrich_struct.py --peek N              # 拿 records 里前 N 条真实记录试抽
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import ai_extract as _ax

KB = os.path.join(BASE, "_kb")
RECORDS = os.path.join(KB, "records")

# 跨所有资源统一的结构化字段（检索站按列存、按列筛）
FIELDS = {
    "kind": "资源类型：只能从 [文档, 数据集, 工具, 软件, 教程, 文章, 社区, 媒体, 其他] 里选一个",
    "summary_zh": "用一句中文说清这个资源是什么、有什么用，不超过40字；没有内容填 NA",
    "highlights": "这个资源最值得关注的2-4个要点，返回字符串数组",
    "keywords": "3-6个用于检索的关键词或主题词，返回字符串数组",
}

PROMPT = "把这个网页资源结构化成一张可检索的卡片：它是什么类型、讲什么、亮点、检索关键词。"

# 摘要类任务只需开头；控制长度以做到单块 1 次 LLM/条
_HEAD_LIMIT = 3500


# ---------------------------------------------------------------- 元数据兜底
def _og_map(rec):
    og = rec.get("og") or {}
    return og if isinstance(og, dict) else {}


def _jsonld_text(rec):
    ld = rec.get("jsonld") or []
    if not isinstance(ld, (list, tuple)):
        return ""
    out = []
    for x in ld[:3]:
        if isinstance(x, str):
            out.append(x[:600])
        elif isinstance(x, dict):
            # 只挑描述类字段，避免把整个 schema 塞进去
            for k in ("description", "headline", "name", "about"):
                v = x.get(k)
                if isinstance(v, str) and v:
                    out.append(v[:300])
    return " ".join(out)


def _usable_summary(s):
    return bool(s) and not any(x in s.lower() for x in _BAD_SUMMARY)


def _meta_summary(rec):
    """从现成元数据里能直接确定的摘要，不烧 LLM。"""
    og = _og_map(rec)
    for k in ("og:description", "description", "twitter:description"):
        v = og.get(k)
        if isinstance(v, str) and len(v.strip()) >= 15 and _usable_summary(v):
            return v.strip()[:200]
    d = (rec.get("desc") or "").strip()
    return d[:200] if len(d) >= 15 and _usable_summary(d) else ""


# 分类层已经给出的 forms 标签比文本词根可靠得多，优先直接映射
_FORM_KIND = (
    ("数据集", ("数据集", "数据")),
    ("教程", ("文档/手册", "电子书/档案", "教程")),
    ("工具", ("在线工具/API", "目录/索引")),
    ("软件", ("软件/安装包", "代码/仓库", "模型/权重", "镜像/网盘")),
    ("社区", ("社区/问答",)),
    ("媒体", ("影音/素材",)),
)


def _guess_kind(rec):
    """零成本的资源类型猜测，供 LLM 失败时兜底。"""
    tags = rec.get("tags") or {}
    forms = tags.get("forms", []) if isinstance(tags, dict) else []
    if not isinstance(forms, (list, tuple)):
        forms = [forms]
    for kind, keys in _FORM_KIND:
        if any(any(k == str(f) or k in str(f) for k in keys) for f in forms):
            return kind
    # 渲染补充库里的 forms 是平铺字段
    for kind, keys in _FORM_KIND:
        if any(any(k == str(f) or k in str(f) for k in keys)
               for f in (rec.get("forms") or [])):
            return kind
    blob = " ".join([
        str(rec.get("title") or ""), str(rec.get("desc") or ""),
        " ".join(str(x) for x in (rec.get("headings") or [])[:8]),
        " ".join(str(x) for x in forms),
    ]).lower()
    rules = [
        ("数据集", ("dataset", "data set", "数据集", "开放数据", "kaggle", "open data")),
        ("教程", ("tutorial", "docs", "documentation", "guide", "教程", "文档", "手册", "入门")),
        ("工具", ("tool", "generator", "converter", "在线工具", "工具箱", "generator")),
        ("软件", ("github", "download", "release", "软件", "开源", "repo")),
        ("社区", ("forum", "community", "reddit", "discuss", "论坛", "社区")),
        ("文章", ("blog", "article", "news", "post", "博客", "文章", "新闻")),
    ]
    for kind, kws in rules:
        if any(k in blob for k in kws):
            return kind
    return "其他"


# ---------------------------------------------------------------- 零成本卡片（渲染落盘即用）
_SENT_SPLIT = re.compile(r"[。；;!！?？\n]|(?<=[a-z])\.(?=\s|$)")
_STOP_KW = frozenset(
    "home about login sign up menu contact copyright all rights reserved cookie "
    "privacy terms 首页 导航 登录 更多 关于我们 搜索 下载 帮助 首页".split())
# 英文虚词不能当检索关键词：过去抽出来一堆 the / for / built，对 AI 检索毫无信息量
_STOP_EN = frozenset(
    "the a an and or of to in for on with from by at as is are was were be been "
    "that this these those it its our your their we you they he she not but if "
    "into about over under more most can will just use used using built make made "
    "get new one all any each which what when where who how why than then also".split())
_BAD_SUMMARY = ("accessdenied", "requestid", "当前位置：", "本站使用", "京icp证")
# 抓取文本常被截断，闭合标签不一定在：匹配到 </style>、下一个标签、或末尾，三者取先
_RE_STYLE = re.compile(r"<style\b[^>]*>[\s\S]*?(?:</style>|(?=<[a-zA-Z/])|$)", re.I)
_RE_SCRIPT = re.compile(r"<script\b[^>]*>[\s\S]*?(?:</script>|(?=<[a-zA-Z/])|$)", re.I)


def _clean_text(rec, limit=800):
    """正文去 HTML/样式/脚本后取前段，避免 CSS/JS 混进摘要和亮点。"""
    t = rec.get("text") or ""
    t = _RE_STYLE.sub(" ", t)
    t = _RE_SCRIPT.sub(" ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t)[:limit].strip()


def meta_card(rec):
    """纯规则从元数据生成完整 4 字段卡片，零 LLM、零延迟。

    渲染流水线在落盘那一刻调用：每条资源自带可检索卡片，这是
    「源头结构化」的主力（LLM 精炼是可选增强，不进渲染热路径）。
    失败绝不抛异常拖垮渲染。
    """
    st = {"kind": "其他", "summary_zh": "", "highlights": [], "keywords": []}
    try:
        st["kind"] = _guess_kind(rec)
        # 摘要：og:description / desc / 正文首句
        s = _meta_summary(rec)
        if not s:
            text = _clean_text(rec, 600)
            for seg in _SENT_SPLIT.split(text):
                seg = seg.strip()
                if len(seg) >= 15 and _usable_summary(seg):
                    s = seg[:200]
                    break
        if not _usable_summary(s):
            s = ""
        st["summary_zh"] = s or ((rec.get("title") or "")[:120])

        # 亮点：小标题优先，其次正文前几句
        hs = [str(x).strip() for x in (rec.get("headings") or []) if str(x).strip()]
        hs = [h for h in hs if h.lower() not in _STOP_KW and len(h) >= 3][:4]
        if not hs:
            text = _clean_text(rec, 800)
            for seg in _SENT_SPLIT.split(text):
                seg = seg.strip()
                if 12 <= len(seg) <= 120 and seg.lower() not in _STOP_KW:
                    hs.append(seg[:80])
                if len(hs) >= 3:
                    break
        st["highlights"] = hs

        # 关键词：meta keywords（逗号分隔）+ 标题/小标题里的实词
        kw = []
        mk = (rec.get("keywords") or "").strip()
        if mk:
            kw += [x.strip() for x in re.split(r"[,，;；]", mk) if x.strip()][:8]
        for h in ([rec.get("title") or ""] + (rec.get("headings") or [])[:5]):
            for w in re.findall(r"[A-Za-z][A-Za-z0-9+.#-]{2,}|[\u4e00-\u9fff]{2,6}",
                                str(h)):
                if w.lower() in _STOP_KW or w.lower() in _STOP_EN or w in kw:
                    continue
                kw.append(w)
        # 去重保序
        seen, uniq = set(), []
        for w in kw:
            if w.lower() not in seen:
                seen.add(w.lower())
                uniq.append(w)
        st["keywords"] = uniq[:6]
    except Exception:
        pass
    return st


# ---------------------------------------------------------------- 主抽取
def _build_source(rec):
    """把一条记录的元数据 + 正文前段拼成给 LLM 的紧凑文本。"""
    parts = []
    t = (rec.get("title") or "").strip()
    if t:
        parts.append("标题: " + t[:200])
    d = (rec.get("desc") or "").strip()
    if d:
        parts.append("描述: " + d[:300])
    og = _og_map(rec)
    if og.get("og:title"):
        parts.append("OG标题: " + str(og["og:title"])[:200])
    ld = _jsonld_text(rec)
    if ld:
        parts.append("结构化数据: " + ld[:500])
    hs = rec.get("headings") or []
    if hs:
        parts.append("小标题: " + " | ".join(str(x)[:60] for x in hs[:10]))
    text = (rec.get("text") or "").strip()
    if text:
        parts.append("正文: " + text[:_HEAD_LIMIT])
    return "\n".join(parts)


def structurize(rec, use_llm=True, window=9000):
    """给一条爬取记录生成结构化卡片。

    返回 (struct: dict, diag: dict)。struct 恒含 4 个字段，失败有兜底，
    不抛异常拖垮调用方。
    """
    diag = {"via": ["meta-card"], "text_len": len(rec.get("text") or "")}
    # 先拿一份完整的规则卡片打底：LLM 通了就被精炼覆盖，没通也照样可用
    struct = meta_card(rec)
    diag["via"].extend(["meta-summary", "guess-kind"])

    if not use_llm:
        return struct, diag

    # 2) LLM 抽取：归纳 highlights / keywords，并校正 kind / summary
    src = _build_source(rec)
    if len(src) < 40:
        diag["status"] = "thin-content"
        return struct, diag
    try:
        r = _ax.SmartExtract(
            source=src, prompt=PROMPT, fields=FIELDS,
            window=window, reattempt=False, merge="rule", workers=1,
        ).run()
        ans = r.get("answer") or {}
        diag["llm"] = r.get("diag", {}).get("stages", [])
        diag["via"].append("llm")
        # 用 LLM 结果覆盖（只收非空/非 NA）
        for k in ("kind", "summary_zh"):
            v = ans.get(k)
            if isinstance(v, str) and v.strip() and v.strip().upper() != "NA":
                struct[k] = v.strip()[:200]
        for k in ("highlights", "keywords"):
            v = ans.get(k)
            if isinstance(v, list):
                vals = [str(x).strip() for x in v
                        if x and str(x).strip().upper() != "NA"]
                if vals:
                    struct[k] = vals[:6]
            elif isinstance(v, str) and v.strip() and v.strip().upper() != "NA":
                struct[k] = [x.strip() for x in re.split(r"[;；,，、]", v) if x.strip()][:6]
    except Exception as e:
        diag["status"] = "llm-failed:" + repr(e)[:120]
        # 静默降级：元数据兜底结果照常返回
    struct["kind"] = struct["kind"] or "其他"
    diag.setdefault("status", "ok")
    return struct, diag


# ---------------------------------------------------------------- 批量（断点）
def enrich_records(records, save=False, use_llm=True):
    """给一批记录补 ai_struct 字段（只处理没有的），可选写回原分片。"""
    import glob
    changed = 0
    by_file = {}
    for f in glob.glob(os.path.join(RECORDS, "rec-*.jsonl")):
        by_file[f] = []
    for rec in records:
        if not isinstance(rec, dict) or rec.get("ai_struct"):
            continue
        if not (rec.get("ok") and len(rec.get("text") or "") > 200):
            continue
        st, diag = structurize(rec, use_llm=use_llm)
        st["_diag"] = diag
        rec["ai_struct"] = st
        changed += 1
        print("  [%s] %s | %s" % (st.get("kind"), (rec.get("domain") or "")[:40],
                                  (st.get("summary_zh") or "")[:50]), flush=True)
    if save and changed:
        _write_back(records, by_file)
    return changed


def _write_back(records, by_file):
    """按 domain 把更新后的 ai_struct 合并回各分片（整片原子重写）。"""
    import tempfile
    upd = {r.get("domain"): r for r in records if isinstance(r, dict) and r.get("ai_struct")}
    for f, _ in by_file.items():
        if not os.path.exists(f):
            continue
        rows = []
        for line in open(f, encoding="utf-8"):
            try:
                o = json.loads(line)
            except Exception:
                rows.append(line)
                continue
            d = o.get("domain")
            if d in upd:
                o["ai_struct"] = upd[d]["ai_struct"]
            rows.append(json.dumps(o, ensure_ascii=False) + "\n")
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(f), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.writelines(rows)
        os.replace(tmp, f)


# ---------------------------------------------------------------- selftest / CLI
def selftest():
    print("enrich_struct selftest（元数据兜底，不烧 LLM）")
    rec = {
        "domain": "example-data.org", "title": "Open Genome Dataset",
        "desc": "A curated dataset for protein structure research, free to download.",
        "og": {"og:description": "Curated protein-structure dataset for researchers."},
        "jsonld": [], "headings": ["Download", "Documentation"],
        "text": "We provide open datasets. " * 50, "ok": True,
        "tags": {"forms": ["数据集"]},
    }
    st, diag = structurize(rec, use_llm=False)
    print("  struct:", json.dumps(st, ensure_ascii=False))
    print("  diag:", diag)
    assert st["summary_zh"], "摘要兜底失败"
    assert st["kind"] in ("数据集", "其他", "教程"), "类型猜测异常: " + st["kind"]

    # 渲染热路径实际调用的是 meta_card：必须产出完整 4 字段，且不能把 CSS/JS 混进去
    card = meta_card(rec)
    print("  card:", json.dumps(card, ensure_ascii=False))
    assert card["summary_zh"] and card["highlights"] and card["keywords"], "零成本卡片字段不全"
    dirty = dict(rec, headings=[], desc="", og={},
                 text=("<style>.a{color:red} body{margin:0}</style>"
                       "<script>var x=1;</script>"
                       "Real content about protein folding appears here. More details follow."))
    clean_card = meta_card(dirty)
    blob = json.dumps(clean_card, ensure_ascii=False)
    print("  clean:", blob)
    assert "color:red" not in blob and "var x" not in blob, "样式/脚本污染未清干净"
    print("selftest DONE")


def _peek(n):
    import glob
    got = 0
    for f in sorted(glob.glob(os.path.join(RECORDS, "rec-*.jsonl"))):
        for line in open(f, encoding="utf-8"):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not (rec.get("ok") and len(rec.get("text") or "") > 300):
                continue
            print("=== %s ===" % rec.get("domain"))
            st, diag = structurize(rec, use_llm=True)
            print(json.dumps({"domain": rec.get("domain"), "struct": st, "diag": diag},
                             ensure_ascii=False, indent=1))
            got += 1
            if got >= n:
                return
    if not got:
        print("没有符合条件的记录")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
    elif "--peek" in args:
        _peek(int(args[args.index("--peek") + 1]) if len(args) > args.index("--peek") + 1 else 3)
    else:
        print(__doc__)
