# -*- coding: utf-8 -*-
"""netforce / websearch.py —— 意图驱动的联网搜索编排层
================================================================
主人 2026-11-20 定的爬虫进化方向：从「爬完资源 → 事后贴标签」升级为
「带着需求去搜 → 现场解析各源 → 整合成答案」。本质 = 类搜索引擎。

设计要点（与项目铁律一致）：
  · `skynet/search.py` 的意图校验/多引擎聚合/引擎健康是现成好代码，本层只接线、不重造
  · 出口不硬编码：优先 NETFORCE_PROXY 环境变量，其次 _netpolicy.json 的 crawler_proxy
  · 本地模型只做「判断意图」和「整合答案」，不给固定标签集——模型自己读、自己判
  · 模型没起（8080/8787 都不监听）时优雅降级：全引擎搜索 + 结构化摘要，绝不报错卡死
  · 无窗口、密钥不入库、只读取配置

三段能力（可单独调用，便于逐段验证）：
  search(query)       P0 联网搜索 —— 带相关性排序的命中列表
  route(query)        P1 意图选源 —— 判断该查哪些引擎/哪个站内，失败降级全引擎
  answer(query)       P2 整合答案 —— 多源结果喂本地模型整合成带出处的答案
  deep_fetch(hits)    可选：抓命中页正文再整合（慢，按需 --deep）

用法：
  python websearch.py "python asyncio 教程"          # 搜索 + 整合答案
  python websearch.py "碧蓝航线 语音" --route          # 显示意图选源决策
  python websearch.py "Rust 异步运行时" --no-llm      # 只用搜索，不调模型
  python websearch.py "xxx" --json                    # 机器可读输出
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
POLICY = os.path.join(HERE, "_netpolicy.json")

# 本地模型端点优先级：环境变量 > 8080(llama-server) > 8787(zen-gate)
LLM_BASES = [
    os.environ.get("NETFORCE_LLM_BASE", "").strip(),
    "http://127.0.0.1:8080/v1",
    "http://127.0.0.1:8787/v1",
]
LLM_MODEL = os.environ.get("NETFORCE_LLM_MODEL", "").strip()


# ------------------------------------------------------------------ 出口
def egress_proxy() -> str:
    """搜索层走统一出口（18080 = 国内直连/国外 CF 隧道），不硬编码。
    环境变量优先；配置留空则返回空串=纯直连（search.py 自身支持直连）。"""
    env = os.environ.get("NETFORCE_PROXY", "").strip()
    if env:
        return env
    try:
        with open(POLICY, encoding="utf-8") as f:
            cfg = json.load(f)
        return str(cfg.get("crawler_proxy") or "").strip()
    except Exception:
        return ""


def _bind_egress():
    """把 skynet.net 的出口指到 18080。search.py 的引擎函数运行时读 net.PROXY，
    所以这里设模块变量即可生效，不必改 search.py 一行代码。"""
    from skynet import net
    px = egress_proxy()
    net.PROXY = px
    net.PROXIES = [px] if px else []
    return px


# ------------------------------------------------------------------ P0 搜索
def search(query: str, engines: list[str] | None = None, wiki_tag: str | None = None,
           budget: float = 22.0, workers: int = 5) -> list:
    """联网搜索：多引擎聚合 + 意图校验 + 相关性排序。返回 list[Hit]。"""
    from skynet import search as S
    _bind_egress()
    kw = {"budget": budget, "workers": workers, "min_rel": S.MIN_REL}
    if engines:
        kw["only"] = engines
    if wiki_tag:
        kw["wiki_tag"] = wiki_tag
    hits = S.search(query, **kw)
    # 选源可能挑中此刻不通的引擎（连通性随时间变）。0 条就放开全引擎重试一次，
    # 让健康度自己筛——"宁可多查不要漏"。只重试一次，不叠多轮。
    if not hits and engines:
        retry_kw = {"budget": budget, "workers": workers, "min_rel": S.MIN_REL}
        if wiki_tag:
            retry_kw["wiki_tag"] = wiki_tag
        hits = S.search(query, **retry_kw)
    return hits


# ------------------------------------------------------------------ 本地模型通道
def _llm_ready() -> tuple[str, str] | None:
    """探测哪个本地模型端点活着，返回 (base, model)；都不活返回 None。
    超时很短（2s），模型没起时几乎无感降级。"""
    for base in LLM_BASES:
        if not base:
            continue
        try:
            url = base.rstrip("/") + "/models"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=2) as r:
                data = json.load(r)
            models = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
            if not models:
                continue
            model = LLM_MODEL or models[0]
            return base, model
        except Exception:
            continue
    return None


def _llm_key(base: str) -> str:
    k = os.environ.get("NETFORCE_LLM_KEY", "").strip()
    if k:
        return k
    if "8080" in base or "8090" in base:
        return ""
    try:
        return open(os.path.join(os.path.expanduser("~"), ".fleet", "zengate.key"),
                    encoding="utf-8").read().strip()
    except Exception:
        return ""


def _call_llm(base: str, model: str, prompt: str, max_tokens: int = 400,
              timeout: float = 60) -> str:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "chat_template_kwargs": {"enable_thinking": False},
        "max_tokens": max_tokens, "temperature": 0,
    }).encode()
    headers = {"Content-Type": "application/json"}
    key = _llm_key(base)
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.load(r)
    msg = out["choices"][0]["message"]
    return (msg.get("content") or "") + (msg.get("reasoning_content") or "")


# ------------------------------------------------------------------ P1 意图选源
ROUTE_PROMPT = """用户想找：「{query}」

天网有这些搜索渠道，每个各有所长：
- github：找开源项目、代码仓库、工具清单（命名/描述含关键词）
- ddg / searx / bing：通用网页搜索，找资料、教程、新闻、普通网站
- wiki：特定游戏/动漫的中文维基站内搜索（如碧蓝航线 wiki）

请判断这个需求该查哪些渠道。直接输出 JSON，不要解释：
{{"engines": ["渠道名1", "渠道名2"], "wiki_tag": "站点标签或null", "why": "一句话理由"}}

规则：
- 找项目/工具/清单 → 加 github
- 找资料/教程/一般信息 → 加 ddg、searx、bing（三个都列，哪个通靠引擎健康度筛）
- 明确是某游戏/动漫/作品的站内资料 → 加 wiki，并在 wiki_tag 填站点标签（碧蓝航线=blhx，其他按作品英文缩写猜）
- 拿不准就多列几个，宁可多查不要漏。不要因为"某引擎可能不准"就排除它——健康度会自动降权，但联通性随时间变，静态排除会误杀
- engines 只能从上面列的名字里选"""


def route(query: str, use_llm: bool = True) -> dict:
    """P1：用本地模型判断该查哪些源。失败/没模型 → 全引擎降级（不卡死）。"""
    # 降级不硬编码引擎白名单：交给 search.py 自带的引擎健康度自动筛选。
    # 教训（11-20 实测）：曾因注释里"bing 已知污染"就把 bing 排除，结果那个
    # 时机下 ddg/searx/github 全 0 条、唯一通的恰是 bing——静态黑名单会害死人。
    # engines=[] 表示不限制，search() 里就不会传 only 参数。
    fallback = {"engines": [], "wiki_tag": None,
                "why": "模型不可用，全引擎（健康度自动筛选）", "degraded": True}
    if not use_llm:
        fallback["why"] = "调用方禁用模型"
        return fallback
    ready = _llm_ready()
    if not ready:
        return fallback
    base, model = ready
    try:
        txt = _call_llm(base, model, ROUTE_PROMPT.format(query=query), max_tokens=200)
        m = re.search(r"\{[\s\S]*?\}", txt)
        if not m:
            return fallback
        d = json.loads(m.group(0))
        valid = {"ddg", "searx", "bing", "github", "wiki"}
        engines = [e for e in (d.get("engines") or []) if e in valid]
        if not engines:
            return fallback
        tag = d.get("wiki_tag")
        if tag in ("null", "None", "", None):
            tag = None
        if "wiki" in engines and not tag:
            engines = [e for e in engines if e != "wiki"] or ["ddg"]
        return {"engines": engines, "wiki_tag": tag,
                "why": str(d.get("why") or "")[:80], "degraded": False}
    except Exception as ex:
        fallback["why"] = "意图判断失败：%s" % str(ex)[:60]
        return fallback


# ------------------------------------------------------------------ P2 整合答案
ANSWER_PROMPT = """用户问题：{query}

联网搜到的资料（每条前有编号，数字即出处）：
{sources}

请综合上面资料回答用户问题。要求：
- 直接给答案，不要复述"根据搜索结果"这类废话
- 关键结论后面用 [编号] 标注出处（如 [1][3]），出处只能是上面出现的编号
- 资料之间有冲突就如实说，不要硬编
- 说出处之外的信息不要杜撰；资料不够就明说哪里不确定
- 简洁，控制在 300 字内"""


def _sources_block(hits: list) -> str:
    lines = []
    for i, h in enumerate(hits[:12], 1):
        title = re.sub(r"\s+", " ", (h.title or ""))[:120]
        desc = re.sub(r"\s+", " ", (h.desc or ""))[:240]
        lines.append(f"[{i}] {title}\n    {desc}\n    {h.url}")
    return "\n".join(lines)


def synthesize(query: str, hits: list, use_llm: bool = True) -> str:
    """P2：把多源结果整合带出处的答案。没模型 → 结构化摘要（标题+摘要+链接）。"""
    if not hits:
        return "（没搜到相关结果）"
    if not use_llm:
        return _plain_summary(hits)
    ready = _llm_ready()
    if not ready:
        return _plain_summary(hits)
    base, model = ready
    try:
        out = _call_llm(base, model,
                        ANSWER_PROMPT.format(query=query, sources=_sources_block(hits)),
                        max_tokens=500, timeout=90)
        return out.strip() or _plain_summary(hits)
    except Exception:
        return _plain_summary(hits)


def _plain_summary(hits: list) -> str:
    lines = [f"找到 {len(hits)} 条相关结果：", ""]
    for i, h in enumerate(hits[:10], 1):
        lines.append(f"[{i}] {h.title[:70]}")
        lines.append(f"    {h.desc[:110]}")
        lines.append(f"    {h.url}")
    return "\n".join(lines)


# ------------------------------------------------------------------ 可选：抓正文
def deep_fetch(hits: list, n: int = 5) -> list[dict]:
    """抓命中页正文再整合（慢，按需 --deep）。用 skynet.net 走同一出口，
    轻量去标签，供 P2 喂更实的料。失败的那条跳过，不影响其他。"""
    from skynet import net
    _bind_egress()
    out = []
    for h in hits[:n]:
        try:
            r = net.fetch_text(h.url, timeout=12, budget=14)
            if not r.ok:
                continue
            text = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", " ", r.text or "", flags=re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) >= 200:
                out.append({"url": h.url, "title": h.title, "text": text[:3000]})
        except Exception:
            continue
    return out


# ------------------------------------------------------------------ 一站式
def answer(query: str, use_llm: bool = True, deep: bool = False,
           budget: float = 22.0) -> dict:
    """给一个需求 → 判断意图 → 联网搜索 → 整合成答案。全链路。"""
    t0 = time.time()
    decision = route(query, use_llm=use_llm)
    hits = search(query, engines=decision["engines"],
                  wiki_tag=decision["wiki_tag"], budget=budget)
    fetched = deep_fetch(hits) if deep else []
    ans = synthesize(query, hits, use_llm=use_llm)
    return {"query": query, "decision": decision,
            "hits": [vars(h) for h in hits], "deep": fetched,
            "answer": ans, "seconds": round(time.time() - t0, 1)}


# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    query_parts, flags = [], set()
    for a in argv:
        if a in ("--no-llm", "--route", "--json", "--deep"):
            flags.add(a)
        else:
            query_parts.append(a)
    query = " ".join(query_parts).strip()
    if not query:
        print("用法: python websearch.py \"查询词\" [--route] [--no-llm] [--deep] [--json]")
        return 2

    if "--route" in flags:
        d = route(query, use_llm="--no-llm" not in flags)
        print("意图选源:", json.dumps(d, ensure_ascii=False))
        return 0

    r = answer(query, use_llm="--no-llm" not in flags, deep="--deep" in flags)
    if "--json" in flags:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    print("=" * 64)
    print("需求:", r["query"])
    print("选源:", r["decision"]["engines"], r["decision"]["wiki_tag"] or "",
          "|", r["decision"]["why"])
    print("-" * 64)
    print(r["answer"])
    print("-" * 64)
    print("耗时 %.1fs · %d 条结果%s" % (
        r["seconds"], len(r["hits"]), " · 已抓正文 %d 页" % len(r["deep"]) if r["deep"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
