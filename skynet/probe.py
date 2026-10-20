"""天网 · 检测层（probe）
=========================
三层里最容易做浅的一层。绝大多数爬虫只判「HTTP 200 就算活」，
结果一半是跳回主站的死子站、软 404、纯导航壳页 —— 白抓一遍还污染语料。

本模块把「检测」拆成四级，逐级淘汰：

    L1 可达性   status / 最终 URL / 是否被跳转、是否被风控
    L2 实质内容  体积、文本密度、正文字数（壳页在这一级死）
    L3 目标密度  目标物计数（台词行 / 表格行 / 音频直链 / 结构块）—— 决定「值不值得下」
    L4 解析校验  抽出的条目数 vs 预期下限（页面在、但解析器失配 ⇒ 静默零产出，最难发现）

对外：
    probe(url)                  → Probe (L1+L2+L3)
    analyze(html, base_url)     → Content  (纯函数，可离线跑)
    verify(extracted, expect)   → 解析校验结果
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

from . import net

# ---------------------------------------------------------------- 可调规则
# 全部集中在这里，方便后续「不断升级」时只改数据不改逻辑。
RULES = {
    # L2：低于这些值 ⇒ 壳页 / 软 404
    "min_bytes": 3_000,
    "min_text_chars": 600,
    "min_text_density": 0.015,        # 正文字数 / HTML 字节
    # L3：目标物
    "min_tables_for_list": 1,
    "min_lines_for_corpus": 8,        # 台词类页面至少 8 条才值得入语料
    "min_audio_for_voice": 3,         # 语音类页面至少 3 个直链
    # 噪声惩罚
    "junk_markers": [
        "404", "not found", "页面不存在", "已删除", "无权访问", "验证码",
        "访问受限", "cloudflare", "just a moment", "请开启javascript",
    ],
    "nav_noise": [
        "首页", "登录", "注册", "编辑", "导航", "关于我们", "隐私政策",
        "版权所有", "关注我们", "返回顶部", "分享到",
    ],
    # 判定阈值
    "score_valuable": 55,
    "score_portal": 35,
}

AUDIO_RE = re.compile(r"https?://[^\"'\s>]+\.(?:mp3|ogg|wav|m4a|flac)", re.I)
SENT_RE = re.compile(r"[，。？！…~♪、]")
HUMAN_RE = re.compile(r"[我你您他她]")
TAG_RE = re.compile(r"<(script|style|nav|footer|header)[^>]*>.*?</\1>", re.S | re.I)
STRIP_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


@dataclass
class Content:
    bytes: int = 0
    title: str = ""
    text_chars: int = 0
    text_density: float = 0.0
    tables: int = 0
    rows: int = 0
    candidates: int = 0          # 像台词的短句数
    with_pronoun: int = 0        # 其中含人称代词的（强信号）
    audio_links: int = 0
    junk_hit: str = ""           # 命中的垃圾标记
    headers: list = field(default_factory=list)


@dataclass
class Probe:
    url: str
    alive: bool = False
    status: int = 0
    final_url: str = ""
    size: int = 0
    title: str = ""
    content: Optional[Content] = None
    score: int = 0
    verdict: str = "unknown"     # valuable / portal / thin / dead / blocked / junk
    reasons: list = field(default_factory=list)

    def __bool__(self):
        return self.alive

    def brief(self) -> str:
        return (f"[{self.verdict:8s}] {self.score:3d}分 {self.size//1024:5d}KB "
                f"台词{self.content.candidates if self.content else 0:4d} "
                f"音频{self.content.audio_links if self.content else 0:3d}  "
                f"{self.url[:56]}")


def _text_of(html: str) -> str:
    t = TAG_RE.sub(" ", html)
    t = STRIP_RE.sub(" ", t)
    return WS_RE.sub(" ", t).strip()


def analyze(html: str, base_url: str = "") -> Content:
    """纯函数：HTML → 内容特征。可离线批量跑，不依赖网络。"""
    c = Content(bytes=len(html or ""))
    if not html:
        return c
    low = html.lower()

    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        c.title = WS_RE.sub(" ", STRIP_RE.sub("", m.group(1))).strip()[:120]

    for mk in RULES["junk_markers"]:
        if mk in low:
            c.junk_hit = mk
            break

    c.tables = len(re.findall(r"<table", low))
    c.rows = len(re.findall(r"<tr", low))
    c.audio_links = len(set(AUDIO_RE.findall(html)))

    txt = _text_of(html)
    c.text_chars = len(txt)
    c.text_density = round(c.text_chars / max(1, c.bytes), 4)

    # 台词候选：短、带标点、含人称
    cand = 0
    pron = 0
    for seg in re.split(r"[\n。！？]", txt):
        seg = seg.strip()
        if 4 <= len(seg) <= 60 and SENT_RE.search(seg):
            if any(n in seg for n in RULES["nav_noise"]):
                continue
            cand += 1
            if HUMAN_RE.search(seg):
                pron += 1
    c.candidates = cand
    c.with_pronoun = pron
    return c


def _score(c: Content) -> tuple[int, list]:
    """0-100 质量分。分档给分，避免某一维独大。"""
    s = 0
    why = []

    size_pt = min(20, c.bytes / 25_000 * 20)          # 25KB 满分
    s += size_pt

    dens = min(20, c.text_density / 0.05 * 20)         # 5% 满分
    s += dens
    if c.text_density < RULES["min_text_density"]:
        why.append(f"文本密度低 {c.text_density:.3f}")

    struct = min(20, (c.tables * 6) + (c.rows / 20 * 10))
    s += struct

    line_pt = min(25, c.with_pronoun / RULES["min_lines_for_corpus"] * 25)
    s += line_pt

    audio_pt = min(15, c.audio_links / RULES["min_audio_for_voice"] * 15)
    s += audio_pt

    return int(round(s)), why


def verdict_of(c: Content, score: int, status: int = 200) -> str:
    if status in net.RISK_CODES:
        return "blocked"
    if c.junk_hit and c.text_chars < RULES["min_text_chars"]:
        return "dead"
    if c.bytes < RULES["min_bytes"] or c.text_chars < RULES["min_text_chars"]:
        return "thin"
    if score >= RULES["score_valuable"]:
        return "valuable"
    if score >= RULES["score_portal"]:
        return "portal"
    return "thin"


def probe(url: str, timeout: float = 15.0, gap: float = 0.6,
          keep_html: bool = False) -> Probe:
    """L1+L2+L3 一体探活。返回 Probe，不抛异常。"""
    r = net.fetch_text(url, timeout=timeout, gap=gap)
    p = Probe(url=url, status=r.status, final_url=r.final_url or url,
              size=r.size, alive=r.ok)
    if not r.ok:
        # 风控 ≠ 死亡。判成 dead 会被 is_done() 当成「已处理完」，好站从此不再重试。
        # 风控码统一取 net.RISK_CODES，别在这里再抄一份（567 就是漏抄的）。
        p.verdict = "blocked" if r.status in net.RISK_CODES else "dead"
        p.reasons.append(r.error or f"HTTP {r.status}")
        return p

    # 软跳转：子站不存在时 301 回主站首页
    if p.final_url and p.final_url.rstrip("/") != url.rstrip("/"):
        p.reasons.append(f"跳转→{p.final_url[:50]}")
        if _is_root(p.final_url) and not _is_root(url):
            p.verdict = "dead"
            p.reasons.append("子站已不存在（跳回主站根）")
            return p

    c = analyze(r.text)
    p.content = c
    p.title = c.title
    p.score, why = _score(c)
    p.reasons.extend(why)
    p.verdict = verdict_of(c, p.score, r.status)
    if keep_html:
        p.__dict__["_html"] = r.text
    return p


def _is_root(u: str) -> bool:
    path = urllib.parse.urlparse(u).path.strip("/")
    return path == "" or path.lower().startswith("wiki")


def verify(extracted: int, expect: int = None, kind: str = "corpus") -> dict:
    """L4 解析校验：页面活着，但解析器可能一条都抽不出来。

    这是最隐蔽的失效 —— 抓取成功率 100%，语料却零增长。
    """
    floor = expect if expect is not None else (
        RULES["min_lines_for_corpus"] if kind == "corpus" else RULES["min_audio_for_voice"])
    return {
        "ok": extracted >= floor,
        "extracted": extracted,
        "floor": floor,
        "kind": kind,
        "hint": "" if extracted >= floor else f"低于下限 {floor}：解析器可能失配或页面改版",
    }


def batch_probe(urls, workers: int = 5, gap: float = 0.8):
    return net.parallel(urls, lambda u: probe(u, gap=gap), workers=workers)
