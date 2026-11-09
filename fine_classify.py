# -*- coding: utf-8 -*-
"""netforce / fine_classify.py —— 完整解析后的多维细分类（发现即打标签）

为什么要另起一层：
    老 classify.py 只有「域名 + title + desc」三个信号，把 41320 条里的 38608 条
    一股脑塞进「JS渲染/无静态内容」。那不是分类，是「没抓到」的垃圾桶。
    完整渲染后我们手里有：正文、标题层级、结构化元数据、全部链接、
    JS 触发的 XHR/资源 URL、状态码、走的通道 ⇒ 足够做**多维标签**。

设计原则（沿用 README）：**只打标签，绝不删除任何条目。**
    价值分只用来排序和挑重点复核，不做自动丢弃——冷门资源正是本系统的意义所在，
    误杀一个冷门站的代价远大于多留十个噪声站。

对外一个函数：tag(record) -> dict，纯本地规则、零网络、可批量。
"""
from __future__ import annotations

import functools
import re
import time
from urllib.parse import urlsplit

# 复用既有 40+ 类目体系，不重造轮子；import 失败也不影响本模块工作
try:
    from classify import classify as _legacy_classify
except Exception:                                    # pragma: no cover
    _legacy_classify = None


# ---------------------------------------------------------------- 资源形态
# (标签, 域名/URL 特征, 文本特征, 资源扩展名特征)
FORM_RULES = [
    ("数据集", ["dataset", "datasets", "kaggle", "zenodo", "figshare", "openml",
                "data.gov", "datahub", "opendata", "huggingface.co/datasets",
                "commoncrawl", "archive.ics.uci.edu"],
     ["dataset", "data set", "数据集", "语料", "corpus", "benchmark data", "标注数据"],
     [".csv", ".tsv", ".parquet", ".jsonl", ".arrow", ".hdf5", ".npy", ".npz"]),
    ("模型/权重", ["huggingface.co/models", "modelscope", "civitai", "ollama.com/library"],
     ["checkpoint", "weights", "模型权重", "pretrained", "fine-tune", "gguf", "safetensors"],
     [".gguf", ".safetensors", ".ckpt", ".onnx", ".pth", ".pt", ".bin"]),
    ("代码/仓库", ["github.com", "gitlab.com", "codeberg.org", "gitea", "bitbucket",
                 "sourcehut", "sr.ht", "gitee.com"],
     ["repository", "source code", "开源仓库", "clone", "commit", "pull request"],
     [".git", ".patch", ".diff"]),
    ("文档/手册", ["docs.", "/docs/", "readthedocs", "gitbook", "wiki.", "manual",
                 "developer.mozilla", "spec.", "standard"],
     ["documentation", "用户手册", "api reference", "specification", "tutorial", "教程"],
     [".pdf", ".md", ".rst", ".adoc", ".epub"]),
    ("在线工具/API", ["api.", "/api/", "swagger", "openapi", "playground", "tool.",
                    "tools.", "converter", "generator", "sandbox", "regex101"],
     ["api endpoint", "在线工具", "try it", "free api", "rest api", "graphql"],
     [".yaml", ".yml", ".json"]),
    ("电子书/档案", ["archive.org", "gutenberg", "libgen", "openlibrary", "book.",
                  "library", "ebooks", "z-lib"],
     ["ebook", "电子书", "full text", "scanned", "馆藏", "digital library"],
     [".epub", ".mobi", ".djvu", ".pdf"]),
    # ⚠️ 别把 .png/.jpg/.svg 当判据：几乎每个站都有图标/样式图，会把所有站都
    #    误标成「影音/素材」（2026-11-04 小批量实测命中 3/5 全是这个噪声）。
    ("影音/素材", ["pixabay", "unsplash", "freesound", "pexels", "soundcloud",
                "youtube", "bilibili", "vimeo", "podcast", "radio"],
     ["stock photo", "image gallery", "audio library", "素材", "图库", "播客",
      "音效", "download mp3"],
     [".mp3", ".mp4", ".wav", ".flac", ".mkv", ".avi", ".webm"]),
    ("软件/安装包", ["download.", "releases", "sourceforge", "fosshub", "app.",
                  "soft.", "mirror."],
     ["download for windows", "installer", "安装包", "portable", "release notes"],
     [".exe", ".msi", ".apk", ".dmg", ".deb", ".rpm", ".zip", ".7z", ".tar.gz"]),
    ("目录/索引", ["awesome", "directory", "index.", "list.", "catalog", "导航",
                 "hub.", "portal", "links."],
     ["curated list", "资源索引", "目录", "a list of", "collection of links"],
     []),
    ("社区/问答", ["forum", "discourse", "reddit", "stackexchange", "stackoverflow",
                 "community", "bbs", "知乎", "v2ex"],
     ["forum", "社区", "问答", "thread", "discussion board"], []),
    ("镜像/网盘", ["mirror", "pan.", "drive.", "index of /", "webdav", "alist",
                "nextcloud", "seafile"],
     ["index of", "镜像站", "网盘", "directory listing", "parent directory"], []),
]

# ---------------------------------------------------------------- 主题标签
TOPIC_RULES = [
    ("AI/机器学习", ["ai", "llm", "machine learning", "deep learning", "neural",
                  "transformer", "gpt", "diffusion", "nlp", "computer vision", "强化学习"]),
    ("数据科学/统计", ["data science", "statistics", "visualization", "pandas",
                  "jupyter", "biostatistics", "统计分析"]),
    ("安全/渗透", ["security", "pentest", "malware", "vulnerability", "ctf", "osint",
                "forensics", "exploit", "reverse engineering", "安全", "漏洞"]),
    ("隐私/匿名", ["privacy", "tor", "vpn", "proxy", "anonymous", "encryption", "加密流量"]),
    ("Web/前端", ["frontend", "javascript", "react", "vue", "css", "webgl", "typescript"]),
    ("系统/运维", ["linux", "kernel", "devops", "kubernetes", "docker", "network",
                "sysadmin", "embedded", "操作系统"]),
    ("数据库/存储", ["database", "postgres", "sql", "redis", "storage", "文件系统"]),
    ("生物/医学", ["bio", "genome", "protein", "medical", "clinical", "drug", "基因", "蛋白"]),
    ("金融/经济", ["finance", "economy", "trading", "market", "crypto", "区块链", "基金"]),
    ("法律/政策", ["law", "legal", "policy", "regulation", "patent", "法规", "判决"]),
    ("教育/课程", ["course", "lecture", "mooc", "textbook", "tutorial", "课程", "教材"]),
    ("学术/出版", ["journal", "arxiv", "preprint", "citation", "peer review", "论文", "预印本"]),
    ("图书馆/档案", ["archive", "library", "museum", "digital preservation", "档案", "馆藏"]),
    ("游戏/模组", ["game", "mod", "minecraft", "steam", "speedrun", "游戏"]),
    ("硬件/电子", ["hardware", "fpga", "circuit", "arduino", "raspberry", "电路"]),
    ("设计/创意", ["design", "typography", "illustration", "3d model", "font", "字体"]),
    ("语言/翻译", ["dictionary", "translation", "corpus linguistics", "词典", "语料库"]),
    ("地理/地图", ["gis", "geospatial", "map", "satellite", "地图", "遥感"]),
    ("机器人/控制", ["robotics", "ros", "control systems", "无人机", "机器人"]),
    ("天文/物理/材料", ["astronomy", "physics", "materials", "chemistry", "量子", "材料"]),
    ("农业/环境/能源", ["agriculture", "climate", "energy", "environment", "气候", "能源"]),
]

# ---------------------------------------------------------------- 二级主题
# 一级主题回答「哪一领域」，二级回答「领域里具体是哪一块」。检索时先按一级收敛、
# 再按二级挑，比一层 20 个平行标签好用（分类粒度更细）。
SUBTOPIC_RULES = [
    ("大模型/LLM", ["llm", "gpt", "qwen", "llama", "transformer", "大模型",
                  "语言模型", "chatgpt", "inference", "quantization", "gguf"]),
    ("扩散/生图", ["diffusion", "stable diffusion", "flux", "生图", "text to image",
                "lora", "comfyui"]),
    ("视觉/CV", ["computer vision", "object detection", "segmentation", "opencv",
              "图像识别", "目标检测"]),
    ("强化学习/推理", ["reinforcement learning", "rlhf", "reasoning", "cot",
                 "强化学习", "推理模型"]),
    ("Web 安全", ["xss", "sql injection", "owasp", "ctf", "web security", "渗透"]),
    ("二进制/逆向", ["reverse engineering", "malware", "binary", "exploit",
                 "pwn", "逆向", "漏洞利用"]),
    ("隐私/网络", ["tor", "vpn", "proxy", "shadowsocks", "wireguard", "翻墙",
               "加密流量", "匿名网络"]),
    ("数据库/检索", ["postgres", "elasticsearch", "vector database", "sqlite",
                 "全文检索", "向量库"]),
    ("云原生/部署", ["kubernetes", "docker", "serverless", "ci/cd", "helm",
                 "容器", "部署"]),
    ("区块链/代币", ["bitcoin", "ethereum", "solana", "defi", "token", "onchain",
                 "区块链", "智能合约"]),
]

BLOCK_WORDS = ["just a moment", "cloudflare", "checking your browser", "人机验证",
               "access denied", "captcha", "verify you are human", "请开启 javascript"]
LOGIN_WORDS = ["sign in", "log in", "login", "登录后才", "authentication required",
               "please sign in", "请先登录"]
PAYWALL_WORDS = ["subscribe to continue", "paywall", "premium content", "付费解锁",
                 "subscription required"]

_EXT_RE = re.compile(r"(?:\.([a-z0-9]{1,8}))(?:\?|#|$)", re.I)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 图标/样式/字体不算「资源形态」证据，否则几乎每个站都会被误标
_IGNORE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".ico",
               ".bmp", ".css", ".js", ".mjs", ".map", ".woff", ".woff2",
               ".ttf", ".eot", ".html", ".htm", ".php", ".aspx"}


def _blob(rec):
    """把所有文本信号拼成一个小写检索串（给 _lang / _access 用）。"""
    parts = [rec.get("domain", ""), rec.get("url", ""), rec.get("title", ""),
             rec.get("desc", ""), rec.get("text", "")[:6000],
             " ".join(rec.get("headings", [])[:20]),
             " ".join(rec.get("keywords", [])[:20])]
    return " ".join(str(p or "") for p in parts).lower()


def _hay(rec):
    """**正文语料**：不含 domain/url，避免 "ai" 撞上 domain、"tor" 撞上 repository。"""
    parts = [rec.get("title", ""), rec.get("desc", ""),
             rec.get("text", "")[:6000],
             " ".join(str(h) for h in (rec.get("headings") or [])[:20]),
             " ".join(str(k) for k in (rec.get("keywords") or [])[:20])]
    return " ".join(str(p or "") for p in parts).lower()


def _site(rec):
    """站点特征：域名 + URL，用子串匹配（域名本来就是短串，噪声低）。"""
    return (str(rec.get("domain", "")) + " " + str(rec.get("url", ""))).lower()


@functools.lru_cache(maxsize=4096)
def _kw_re(k):
    """英文/数字关键词按**词边界**匹配；返回 None 表示中文关键词（走子串）。"""
    if _CJK_RE.search(k):
        return None
    parts = [p for p in re.split(r"[\s\-_/]+", k.strip().lower()) if p]
    if not parts:
        return None
    body = r"[\s\-_/]+".join(re.escape(p) for p in parts)
    return re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])")


def _word_hit(hay, kws):
    """中文子串 + 英文词边界。2026-11-04 实测修掉的坑：
    裸子串会让 ai→main/available、tor→repository/directory、map→example
    之类满屏假标签，知识库检索结果全被污染。"""
    for k in kws:
        kl = str(k).lower()
        r = _kw_re(kl)
        if r is None:
            if kl in hay:
                return True
        elif r.search(hay):
            return True
    return False


def _exts(rec):
    out = set()
    for a in (rec.get("assets") or []):
        u = a if isinstance(a, str) else a.get("url", "")
        m = _EXT_RE.search(urlsplit(u).path)
        if m:
            out.add("." + m.group(1).lower())
    for l in (rec.get("links") or [])[:400]:
        u = l if isinstance(l, str) else l.get("url", "")
        m = _EXT_RE.search(urlsplit(u).path)
        if m:
            out.add("." + m.group(1).lower())
    return {e for e in out if e not in _IGNORE_EXT}


def _valuable_assets(rec):
    """只有真资料才算质量分证据，图标/logo 不算。"""
    n = 0
    for a in (rec.get("assets") or []):
        u = a if isinstance(a, str) else a.get("url", "")
        m = _EXT_RE.search(urlsplit(u).path)
        if m and ("." + m.group(1).lower()) not in _IGNORE_EXT:
            n += 1
    return n


def _forms(rec, hay, site, exts):
    out = []
    for name, dom_kw, txt_kw, ext_kw in FORM_RULES:
        hit = (any(str(k).lower() in site for k in dom_kw)
               or _word_hit(hay, txt_kw)
               or any(e in exts for e in ext_kw))
        if hit:
            out.append(name)
    # JS 触发的 XHR 端点本身就是「可查询的数据服务」信号
    if rec.get("xhr_count", 0) >= 3 and "在线工具/API" not in out:
        out.append("在线工具/API")
    return out or ["未识别/待人工看一眼"]


def _site_words(site):
    """域名/URL 标点转空格，让词边界规则生效。

    🔴 2026-11-09 修掉的真错：老代码 `any(k in site ...)` 是裸子串，
    "ai" 会命中 domain/mail、"tor" 命中 repository/directory、"map" 命中
    example —— 知识库里一片假「AI / 隐私 / 地理」标签。现在统一走词边界。
    """
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", str(site).lower())


def _topics(hay, site):
    sw = _site_words(site)
    return [name for name, kws in TOPIC_RULES
            if _word_hit(sw, kws) or _word_hit(hay, kws)]


def _subtopics(hay, site_words):
    return [name for name, kws in SUBTOPIC_RULES
            if _word_hit(hay, kws) or _word_hit(site_words, kws)]


def _hits(hay, kws, limit=3):
    """命中的具体关键词，留作复核证据（标签不是黑箱，要能回答「凭什么」）。"""
    out = []
    for k in kws:
        if _word_hit(hay, [k]):
            out.append(str(k))
            if len(out) >= limit:
                break
    return out


def _evidence(hay, site_words, topics, subs):
    ev = {}
    for name, kws in TOPIC_RULES:
        if name in topics:
            ev[name] = _hits(hay, kws) or _hits(site_words, kws)
    for name, kws in SUBTOPIC_RULES:
        if name in subs:
            ev[name] = _hits(hay, kws) or _hits(site_words, kws)
    return ev


def _access(rec, blob):
    st = rec.get("status", 0)
    if not rec.get("ok") or st == 0:
        return "不可达"
    if st in (401, 403, 412, 429, 503, 567) or any(w in blob for w in BLOCK_WORDS):
        return "风控/需验证"
    if any(w in blob for w in LOGIN_WORDS):
        return "需登录"
    if any(w in blob for w in PAYWALL_WORDS):
        return "付费墙"
    return "开放直取"


_LANG_MAP = (("zh", "中文"), ("ja", "日文"), ("ko", "韩文"), ("ru", "俄文"),
             ("de", "德文"), ("fr", "法文"), ("es", "西文"), ("pt", "葡文"),
             ("it", "意文"), ("ar", "阿文"), ("hi", "印地文"))


def _lang(rec, blob):
    lg = (rec.get("lang") or "").lower()
    if "chinese" in lg:
        return "中文"
    for code, name in _LANG_MAP:
        if lg.startswith(code) or lg == code:
            return name
    text = (rec.get("title", "") + " " + rec.get("text", "")[:1500])
    if text:
        cjk = len(_CJK_RE.findall(text)) / max(1, len(text))
        if cjk > 0.08:
            return "中文"
        # 假名/谚文：即使页面没写 lang 也能分开，不再一律「其他语种」
        if re.search(r"[\u3040-\u30ff]", text):
            return "日文"
        if re.search(r"[\uac00-\ud7af]", text):
            return "韩文"
        if re.search(r"[\u0400-\u04ff]", text):
            return "俄文"
    return "英文/拉丁"


def _region(rec):
    """地域标签只读采集层已判好的 region，**本模块零网络**（设计原则）。"""
    r = str(rec.get("region") or "").lower()
    if r == "cn":
        return "国内"
    if r == "foreign":
        return "国外/全球"
    return "未标注"


def _egress(rec):
    """这条记录是经哪条通道拿到的：静态直取 / TLS 指纹 / 浏览器 / 隧道 / 重抓。

    2026-11-09 修：旧版把 `tier=="http"` 放在最前面，导致重抓记录里
    egress="fingerprint" / "browser" 全被压成「静态直取」，新维度等于白加。
    现在先认采集层写明的**具体通道**，最后才用 tier 兜底。
    """
    ch = str(rec.get("channel") or "").lower()
    e = str(rec.get("egress") or "").lower()
    if "tunnel" in ch or e == "tunnel":
        return "隧道回落"
    if "fingerprint" in ch or e in ("fingerprint", "curl_cffi"):
        return "TLS指纹伪装"
    if "browser" in ch or e == "browser" or rec.get("rendered"):
        return "无头浏览器"
    if "reclaim" in ch:
        return "统一出口重抓"
    if rec.get("tier") == "http" or e == "direct":
        return "静态直取"
    return "直连"


def _tld(rec):
    d = str(rec.get("domain") or "").lower().rstrip(".")
    parts = d.split(".")
    if not parts or not d:
        return ""
    if (len(parts) >= 3 and parts[-2] in ("com", "net", "org", "gov", "edu", "ac", "co")
            and len(parts[-1]) == 2):
        return ".".join(parts[-2:])
    return parts[-1]


def _freshness(rec):
    """时效标签：优先页面声明的 last_modified，其次采集年份。"""
    lm = str(rec.get("last_modified") or "")
    m = re.search(r"(20\d{2})", lm)
    if m:
        return m.group(1)
    ts = rec.get("ts")
    if isinstance(ts, (int, float)) and ts > 0:
        return time.strftime("%Y", time.localtime(ts))
    return "未知"


def _quality(rec, forms, topics, access, subs=()):
    """0-100 的价值分。只用于排序/挑复核，不用于删除。"""
    q = 0
    tlen = rec.get("text_len", 0)
    q += min(30, tlen // 120)                       # 正文实质内容
    q += min(15, len(rec.get("links") or []) // 4)  # 可继续发现的链接
    q += min(15, _valuable_assets(rec) * 3)          # 真资料才算，图标不算
    q += 8 if (rec.get("title") or "").strip() else 0
    q += 6 if (rec.get("desc") or "").strip() else 0
    q += 6 if rec.get("structured") else 0          # JSON-LD / OG 等结构化元数据
    q += 5 if rec.get("rendered") else 0
    q += min(10, len(topics) * 3)
    q += min(6, 2 * len(subs))                      # 二级主题越具体越可检索
    q += 5 if forms != ["未识别/待人工看一眼"] else 0
    # 正文密度：导航页 text_content() 也很长，但正文占比低 ⇒ 别给它虚高分。
    dens = rec.get("text_density")
    if isinstance(dens, (int, float)):
        q += 6 if dens >= 0.55 else (-8 if dens < 0.25 else 0)
    if access in ("风控/需验证", "付费墙"):
        q -= 10
    if access == "不可达":
        q -= 25
    return max(0, min(100, q))


def tag(rec):
    """给一条完整解析记录打多维标签。返回 dict，直接写回记录的 tags 字段。"""
    blob = _blob(rec)
    hay, site = _hay(rec), _site(rec)
    sw = _site_words(site)
    exts = _exts(rec)
    forms = _forms(rec, hay, site, exts)
    topics = _topics(hay, site)
    subs = _subtopics(hay, sw)
    evidence = _evidence(hay, sw, topics, subs)
    access = _access(rec, blob)
    q = _quality(rec, forms, topics, access, subs)
    if _legacy_classify:
        try:
            cat, basis = _legacy_classify(
                rec.get("domain", ""), rec.get("title", ""), rec.get("desc", ""),
                "" if rec.get("ok") else "unreachable", bool(rec.get("ok")))
        except Exception:
            cat, basis = "其他/待定", ""
    else:
        cat, basis = "其他/待定", ""
    # 老类目里「JS渲染/无静态内容」是「没抓到」的占位；渲染成功后不再沿用
    if "JS渲染" in cat and rec.get("ok"):
        cat = (topics[0] if topics else "其他/待定")
        basis = "渲染后重判"
    return {
        "category": cat,
        "legacy_basis": basis,
        "forms": forms,
        "topics": topics,
        "subtopics": subs,
        "evidence": evidence,
        "access": access,
        "lang": _lang(rec, blob),
        # 2026-11-09 新增：地域 / 出口 / 形态 / 时效，四条轴把「国内外正确识别」
        # 和「走了哪条路」一起固化进标签，检索时能直接筛选，不必回查日志。
        "region": _region(rec),
        "region_basis": rec.get("region_basis") or "",
        "egress": _egress(rec),
        "tld": _tld(rec),
        "freshness": _freshness(rec),
        "text_density": rec.get("text_density"),
        "quality": q,
        "value": "高" if q >= 55 else ("中" if q >= 28 else "低"),
        "exts": sorted(exts)[:30],
        "discoverable": len(rec.get("new_domains") or []),
        "js_resources": len(rec.get("assets") or []) + rec.get("xhr_count", 0),
    }


if __name__ == "__main__":
    demo = {
        "domain": "example-data.org", "url": "https://example-data.org/", "ok": True,
        "status": 200, "title": "Open Genome Dataset", "desc": "free download",
        "text": "We provide a curated dataset for protein structure research. " * 60,
        "text_len": 3800, "headings": ["Datasets", "Download"], "keywords": [],
        "links": [{"url": "https://example-data.org/a.csv", "text": "csv"}] * 12,
        "assets": [{"url": "https://example-data.org/a.csv", "type": "xhr"}],
        "new_domains": [], "xhr_count": 4, "structured": True, "rendered": True,
        "lang": "en",
    }
    import pprint
    pprint.pprint(tag(demo))
