#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai_extract.py —— 声明式 AI 结构化抽取层（吸收 ScrapeGraphAI 图编排设计）

参考 ScrapeGraphAI（github.com/ScrapeGraphAI/Scrapegraph-ai）的图编排设计
吸收它的三个核心设计，**不搬它的重依赖**（langchain / playwright / 一堆 LLM SDK）：

  1. 声明式抽取：给 prompt + 字段清单，LLM 直接吐结构化 JSON —— 不为每个站写解析规则
  2. 分块→并行→合并（map-reduce）：大页面按窗口切块，各块并行抽，再合并去重
  3. 解析后确定性自检：LLM-free 检查「用户要的字段在正文里有没有痕迹」，
     防止 200 空壳页 / JS 未渲染 / 字段被 parser 丢掉 骗过整条链，静默返回 NA

与 ScrapeGraphAI 的差异：
  · FetchNode 用 playwright + 自己管 proxy/anti-bot —— 这里直接复用 netforce.anyfetch
    （纯直连抗噪：DoH 真 IP + 多通道并发 + 信号质量投票 + 换入口），获取层更强且零外部 daemon。
  · LLM 通道复用本机 8099（gemma-4 CUDA，translate_local.py 同款），离线可跑，
    也可降级到 brain.call_brain 的免费 API 池；不绑死任何云厂商。

数据流（一条链，可解释）：
  source → _get_doc(anyfetch/HTML/JSON) → _html_to_text
        → _evidence_check(确定性) → [_split → 每块并行 _extract_one] → _merge → answer
        → answer 为空则 reattempt 一次（吸收 ConditionalNode + regen）

用法：
    from ai_extract import SmartExtract
    r = SmartExtract(
        source="https://example.com/article",
        prompt="提取这篇文章的标题、作者、发布日期",
        fields={"title": "文章标题", "author": "作者", "date": "发布日期"},
    ).run()
    print(r["answer"], r["diag"])

    # 本地 HTML 字符串也能抽（不联网）
    r = SmartExtract(source=html_text, prompt=..., fields=...).run()

CLI：
    python ai_extract.py --selftest
    python ai_extract.py --url URL --prompt "提取标题和价格" --fields "title,price"
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
except Exception:
    pass

import generic_extract as _ge  # noqa: E402  （正文密度 / 正则工具，同目录复用）

LLM_URL = os.environ.get("NF_AI_LLM_URL", "http://127.0.0.1:8099/v1/chat/completions")
LLM_MODEL = os.environ.get("NF_AI_LLM_MODEL", "local")
DEFAULT_WINDOW = 6000       # 每块字符数（中文≈字，英文≈4字符/token，保守取值）
DEFAULT_OVERLAP = 200
MAX_WORKERS = 4

SYS = (
    "你是一个网页数据抽取器。从给定文本中按用户要求抽取字段，只输出一个 JSON 对象。\n"
    "规则：只输出 JSON，不要 markdown 代码块，不要 ```json，不要任何解释；"
    "找不到的字段值用字符串 \"NA\"；值用原文，不要编造。"
)

# 与 ScrapeGraphAI ParseNode.prompt_stopwords 同源：判断「正文是否含用户要的信息」时丢掉这些无判别词
STOPWORDS = frozenset(
    "about after all also and any are been both but can content data does each every "
    "extract find for from get give has have here how html info information into its "
    "json list many more much must not only out page please provide retrieve return "
    "scrape site some such text that the their them there these they this those url was "
    "web webpage website were what when where which who why will with would you your 的 了 和 是 在 有 把 我 请 帮我 提取 列出 这个 那些 一下 内容 信息 网页 页面".split()
)
WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9]{2,}")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


# ---------------------------------------------------------------- LLM 通道
def _llm(prompt, system=SYS, max_tokens=600, timeout=90, url=None, model=None):
    """调本机 OpenAI 兼容端点（8099）。失败向上抛，由调用方决定是否降级。"""
    payload = {
        "model": model or LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "stream": False,
    }
    req = urllib.request.Request(
        (url or LLM_URL),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
    return json.loads(raw)["choices"][0]["message"]["content"].strip()


def _llm_any(prompt, **kw):
    """本机 8099 优先，不可达则降级到 netforce.brain 的免费 API 池。"""
    try:
        return _llm(prompt, **kw), "local:8099"
    except Exception:
        try:
            import brain
            return brain.call_brain(prompt), "brain:free-pool"
        except Exception as e:
            raise RuntimeError("LLM 全部通道不可用: %r" % (e,))


# ---------------------------------------------------------------- JSON 容错
def tolerant_json(text):
    """吸收 ScrapeGraphAI TolerantJsonOutputParser：扛 ```包裹 / 双花括号 / 前后碎话。"""
    s = (text or "").strip()
    if not s:
        raise ValueError("empty-llm-output")
    # 去掉 markdown 代码围栏
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    # 只取最外层 JSON 对象/数组
    m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", s)
    if m:
        s = m.group(1)
    # DeepSeek 类模型会复述转义后的双花括号
    if s.startswith("{{") and s.endswith("}}"):
        s = s[1:-1]
    try:
        return json.loads(s)
    except Exception:
        # 最后手段：去掉双花括号再来一次
        s2 = s.replace("{{", "{").replace("}}", "}")
        return json.loads(s2)


# ---------------------------------------------------------------- 获取 / 解析
def _get_doc(source):
    """source → (text, meta)。URL 走 anyfetch；否则当 HTML/JSON 字符串。"""
    if isinstance(source, (dict, list)):
        return json.dumps(source, ensure_ascii=False), {"via": "json-object"}
    s = str(source or "").strip()
    if s.startswith("http://") or s.startswith("https://"):
        import anyfetch
        r = anyfetch.get(s)
        if not r.ok:
            return "", {"via": "anyfetch", "ok": False, "diag": r.diag,
                        "tried": r.tried, "source": r.source}
        if isinstance(r.json, (dict, list)):
            text = json.dumps(r.json, ensure_ascii=False)
        else:
            text = r.body.decode("utf-8", "replace")
        return text, {"via": "anyfetch", "ok": True, "source": r.source,
                      "status": r.status, "quality": r.quality, "tried": r.tried}
    return s, {"via": "literal"}


def _html_to_text(html):
    """HTML → 干净文本。优先正文密度（复用 generic_extract），保留 meta/结构化字段。"""
    if not html or not re.search(r"<[a-zA-Z/]", html):
        return html or ""
    d = _ge.RE_COMMENT.sub(" ", html)
    d = _ge.RE_SCRIPT.sub(" ", d)
    # meta 里的 title/description 常是抽取目标，拼到最前面
    meta = _ge._meta_map(html)
    head = " ".join(
        "%s: %s" % (k, v) for k, v in meta.items()
        if k in ("og:title", "og:description", "description", "author", "title")
        and v
    )
    body = _ge.extract_article(d, min_len=20)
    # 正文算法没抽到时，退化为「去标签保留全部文本」
    if len(body) < 60:
        txt = re.sub(r"<(br|/p|/div|/li|/h[1-6])[^>]*>", "\n", d, flags=re.I)
        txt = _ge.RE_TAG.sub(" ", txt)
        txt = _ge._html.unescape(txt)
        txt = re.sub(r"[ \t]+", " ", txt)
        txt = re.sub(r"\n\s*\n+", "\n", txt).strip()
        body = txt
    return (head + "\n" + body).strip()


# ---------------------------------------------------------------- 确定性自检
def _expected_terms(fields, prompt):
    """从字段名 + prompt 提取判别词（吸收 ParseNode._collect_expected_terms）。"""
    terms = set()
    for name, desc in (fields.items() if isinstance(fields, dict) else
                       [(f, "") for f in fields]):
        for w in re.split(r"[_\s]+", str(name)):
            if w and w.lower() not in STOPWORDS:
                terms.add(w.lower())
        for w in WORD_RE.findall(str(desc)):
            if w.lower() not in STOPWORDS:
                terms.add(w.lower())
    if prompt:
        for w in WORD_RE.findall(prompt):
            if w.lower() not in STOPWORDS:
                terms.add(w.lower())
        # 中文 prompt 按连续汉字块切（2-6 字），只留较长的
        for w in re.findall(r"[\u4e00-\u9fff]{2,6}", prompt):
            if w not in STOPWORDS:
                terms.add(w)
    return terms


def evidence_check(text, fields, prompt):
    """LLM-free 预检：用户要的词在正文里一个都没有 → 大概率空壳/JS 未渲染。

    返回 (ok: bool, diag: str)。零命中才报警（保守，避免误伤措辞不同的正常页面）。
    """
    if not text or not text.strip():
        return False, "parsed-content-empty"
    terms = _expected_terms(fields, prompt)
    if not terms:
        return True, "no-discriminative-terms"
    low = text.lower()
    hit = [t for t in terms if t in low]
    if not hit:
        return False, "no-evidence: 期望词 %s 在正文里零命中（疑似空壳/JS未渲染/被parser丢弃）" % (
            sorted(terms)[:12],)
    return True, "evidence:%d/%d" % (len(hit), len(terms))


# ---------------------------------------------------------------- 分块
def split(text, window=DEFAULT_WINDOW, overlap=DEFAULT_OVERLAP):
    """按字符切块，带重叠（避免字段被切口切开）。短文本单块。"""
    text = text or ""
    if len(text) <= window:
        return [text]
    out, start, step = [], 0, window - overlap
    while start < len(text):
        out.append(text[start:start + window])
        if start + window >= len(text):
            break
        start += step
    return out


# ---------------------------------------------------------------- 抽取 / 合并
def _format_instructions(fields):
    shape = {name: (desc or "字段值，找不到填 NA") for name, desc in
             (fields.items() if isinstance(fields, dict) else [(f, "") for f in fields])}
    return ("只输出一个 JSON 对象，键为：%s；缺失用 \"NA\"。不要代码块、不要解释。\n"
            "JSON 形状示例：%s" % (
                ", ".join(shape.keys()),
                json.dumps(shape, ensure_ascii=False)))


def _extract_one(chunk, prompt, fields, extra=""):
    p = (
        _format_instructions(fields) + "\n\n" + extra + "\n"
        + "USER QUESTION: %s\n\nWEB CONTENT:\n%s" % (prompt, chunk)
    )
    out, via = _llm_any(p, max_tokens=600)
    try:
        obj = tolerant_json(out)
    except Exception:
        return {"_error": "bad-json", "_raw": out[:300]}, via
    if not isinstance(obj, dict):
        obj = {"content": obj}
    return obj, via


def _merge_rule(results):
    """确定性合并（默认）：list 拼接去重，str 取首个非 NA，dict 逐键合并。"""
    keys = set()
    for r in results:
        keys |= set(k for k in r if not k.startswith("_"))
    out = {}
    for k in keys:
        vals = [r[k] for r in results if k in r and r[k] not in (None, "", "NA")]
        if not vals:
            out[k] = "NA"
            continue
        if isinstance(vals[0], list):
            merged = []
            for v in vals:
                if isinstance(v, list):
                    for x in v:
                        if x not in merged:
                            merged.append(x)
            out[k] = merged
        elif isinstance(vals[0], dict):
            d = {}
            for v in vals:
                if isinstance(v, dict):
                    d.update(v)
            out[k] = d
        else:
            out[k] = vals[0]
    return out


def _merge_llm(results, prompt, fields):
    """吸收 ScrapeGraphAI TEMPLATE_MERGE：让 LLM 把各块结果合一个、去重。"""
    p = (
        _format_instructions(fields) + "\n\n"
        + "下面是从网页多个分块分别抽出的结果，请合并成单一答案，去重、不超限、结构一致。\n"
        + "USER QUESTION: %s\nCHUNKS RESULT: %s" % (
            prompt, json.dumps(results, ensure_ascii=False))
    )
    out, via = _llm_any(p, max_tokens=800)
    try:
        return tolerant_json(out), via
    except Exception:
        return _merge_rule(results), via + "+fallback-rule"


# ---------------------------------------------------------------- 主入口
class SmartExtract:
    """声明式抽取：source + prompt + fields → 结构化答案（带自检/分块/重试诊断）。"""

    def __init__(self, source, prompt, fields=None, window=DEFAULT_WINDOW,
                 overlap=DEFAULT_OVERLAP, workers=MAX_WORKERS, merge="rule",
                 reattempt=True, use_llm_merge=False, extra=""):
        self.source = source
        self.prompt = prompt
        self.fields = fields or {"answer": "从文本中提取的答案"}
        if not isinstance(self.fields, dict):
            self.fields = {str(f): "" for f in self.fields}
        self.window, self.overlap = window, overlap
        self.workers = workers
        self.merge = merge
        self.reattempt = reattempt
        self.use_llm_merge = use_llm_merge
        self.extra = extra

    def run(self):
        t0 = time.time()
        diag = {"stages": [], "via": []}

        # 1. Fetch（anyfetch 抗噪获取）
        doc, meta = _get_doc(self.source)
        diag["fetch"] = meta
        diag["stages"].append("fetch:%s" % meta.get("via"))
        if not doc:
            return self._result("NA", diag, "fetch-failed", t0)

        # 2. Parse（HTML→文本）
        text = _html_to_text(doc)
        diag["text_len"] = len(text)
        diag["stages"].append("parse")
        if not text.strip():
            return self._result("NA", diag, "empty-text", t0)

        # 3. Evidence 自检（LLM-free）
        ok, ev = evidence_check(text, self.fields, self.prompt)
        diag["evidence"] = ev
        diag["stages"].append("evidence:%s" % ("ok" if ok else "warn"))
        # 不硬失败：继续抽，让诊断说话（保守）

        # 4. Split → 并行抽取（map）
        chunks = split(text, self.window, self.overlap)
        diag["chunks"] = len(chunks)
        diag["stages"].append("split:%d" % len(chunks))
        results = []
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futs = {ex.submit(_extract_one, c, self.prompt, self.fields, self.extra): i
                    for i, c in enumerate(chunks)}
            for f in as_completed(futs):
                i = futs[f]
                try:
                    obj, via = f.result()
                    results.append((i, obj, via))
                    diag["via"].append(via)
                except Exception as e:
                    results.append((i, {"_error": repr(e)}, "error"))
        results.sort(key=lambda x: x[0])
        objs = [o for _, o, _ in results]
        diag["stages"].append("extract:%d" % len(objs))

        # 5. Merge（reduce）
        if self.use_llm_merge:
            answer, via = _merge_llm(objs, self.prompt, self.fields)
            diag["via"].append(via)
        else:
            answer = _merge_rule(objs)
        diag["stages"].append("merge:%s" % self.merge)

        # 6. Reattempt：答案全 NA 且有自检警告 → 再抽一次（吸收 regen）
        if self.reattempt and self._all_na(answer) and not ok:
            regen_extra = ("上一次抽取失败，正文中未发现期望字段。请重新仔细查找，"
                           "仍找不到才填 NA。\n" + self.extra)
            retry, _ = _extract_one(text[:self.window * 2], self.prompt,
                                    self.fields, regen_extra)
            if not self._all_na(retry):
                answer = retry
            diag["stages"].append("reattempt")

        diag["elapsed"] = round(time.time() - t0, 2)
        return self._result(answer, diag, "ok", t0, already_timed=True)

    def _all_na(self, obj):
        if not isinstance(obj, dict):
            return not obj
        vals = [v for k, v in obj.items() if not k.startswith("_")]
        return not vals or all(v in (None, "", "NA") for v in vals)

    def _result(self, answer, diag, status, t0, already_timed=False):
        if not already_timed:
            diag["elapsed"] = round(time.time() - t0, 2)
        return {"answer": answer, "status": status, "diag": diag}


# ---------------------------------------------------------------- selftest / CLI
def selftest():
    print("ai_extract selftest（本地 HTML，不联网）")
    html = """
    <html><head>
      <title>Free Tools Hub — 最好的在线工具箱</title>
      <meta property="og:title" content="Free Tools Hub">
      <meta name="description" content="A collection of free online tools for developers.">
      <meta name="author" content="Jane Doe">
    </head><body>
      <h1>Free Tools Hub</h1>
      <p>A collection of free online tools for developers, founded by Jane Doe in 2021.
      The flagship product is PDF Ninja Pro, priced at 29.99 USD per month.</p>
      <p>Contact: hello@freetoolshub.example. Located in Berlin, Germany.</p>
    </body></html>
    """
    fields = {"title": "网站/文章标题", "author": "作者姓名", "price": "产品价格", "founder_year": "创立年份"}
    # ① 确定性自检（不依赖 LLM）
    text = _html_to_text(html)
    ok, ev = evidence_check(text, fields, "提取标题、作者、价格和创立年份")
    print("  parse+evidence: ok=%s  diag=%s  text_len=%d" % (ok, ev, len(text)))
    assert len(text) > 40, "html_to_text 抽太少"
    # ② JSON 容错（吸收 TolerantJsonOutputParser）
    cases = [
        ('```json\n{"title":"A","price":"9.9"}\n```', {"title": "A", "price": "9.9"}),
        ('{{"title": "A"}}', {"title": "A"}),
        ('前面废话 {"author":"Jane"} 后面废话', {"author": "Jane"}),
    ]
    for raw, want in cases:
        got = tolerant_json(raw)
        assert got == want, "tolerant_json 失败: %r -> %r" % (raw, got)
    print("  tolerant_json: 3/3 OK")
    # ③ 分块 + 规则合并（不依赖 LLM）
    chunks = split("abcdefghij" * 100, window=120, overlap=20)
    assert len(chunks) > 1 and chunks[0][-20:] == chunks[1][:20], "分块重叠错误"
    merged = _merge_rule([{"tags": ["a", "b"], "title": "NA"},
                          {"tags": ["b", "c"], "title": "Real"}])
    assert merged["tags"] == ["a", "b", "c"] and merged["title"] == "Real", merged
    print("  split+merge: OK (%d chunks)" % len(chunks))
    # ④ 端到端（需要 8099 本地 LLM；不通则只报不失败）
    print("  --- 端到端抽取（本地 8099）---")
    r = SmartExtract(source=html, prompt="提取标题、作者、价格和创立年份",
                     fields=fields, reattempt=False).run()
    print("  answer:", json.dumps(r["answer"], ensure_ascii=False))
    print("  status:", r["status"], "| diag:", r["diag"])
    print("selftest DONE")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
    elif "--url" in args:
        url = args[args.index("--url") + 1]
        prompt = args[args.index("--prompt") + 1] if "--prompt" in args else "提取页面标题和主要内容"
        fields_raw = args[args.index("--fields") + 1] if "--fields" in args else "title,summary"
        fields = {f.strip(): "" for f in fields_raw.split(",") if f.strip()}
        r = SmartExtract(source=url, prompt=prompt, fields=fields).run()
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(__doc__)
