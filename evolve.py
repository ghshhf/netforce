# -*- coding: utf-8 -*-
"""netforce 爬虫的「进化模块」——让 URL 供给随收录规模增长，而不是靠手写关键词。

## 为什么要它（2026-10-18 实测数据）

近 60 轮漏斗：

    引擎返回 1547 条
      ↓ 去重（-71.8%）
    新 URL 437 条        ← 瓶颈在这，不在带宽也不在并发
      ↓ 判定入册（-69.3%）
    入册 134 条

并发早已自适应到 24，再调没有任何用——**重复率 71.8% 说明查询空间被穷尽了**：
泵用的关键词全是我手写的固定集合，跑得越久，搜索引擎返回的越是同一批结果。
也就是说，它现在的形态是「关键词驱动的枚举器」，上限 = 我写的词的数量。

## 两个能力（同一个原则：从"已经收进来的东西"里长出新的查询）

① **外链矿** `harvest_outlinks()`
   已收录 5453 个站 = 5453 个矿点，每个站首页的外链都是候选。
   关键洞察：资源站之间互相引用是**同质网络**——一个数据集站的外链大概率还是
   资源站，信噪比远高于搜索引擎结果。这是目前最大的未开采供给。

② **关键词自举** `bootstrap_keywords()` / `bootstrap_search()`
   从已收录站点的 title 里抽高频短语 → 造新查询。
   ⇒ 收录越多，能造出的查询越多，查询空间**随规模增长**（越跑越宽而不是越跑越窄）。
   中文没有分词器（没装 jieba），走「分类名 × 后缀模板」，质量可控；
   英文走语料 n-gram，是真正的自举。

## 设计原则（别改坏）

· **不替代原有泵**，而是作为两个新泵加进调度，让已有的加权轮转（命中率低自动降权）
  决定它们值不值得跑——好用自然多跑，不好用自动饿死，不需要我手工调参。
· **不碰去重与入册流程**，只负责"产出候选 URL"，判定一律交给 `consider()`。
· 外链挖过的站点记在 `_harvested.json`，不重复挖同一个站（否则每轮挖出一样的东西）。
"""
import os
import re
import json
import time
import random
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
RESOURCES = os.path.join(HERE, "resources.json")
CLASSIFIED = os.path.join(HERE, "classified.json")
HARVESTED = os.path.join(HERE, "_harvested.json")

# 抽词用的停用词（太泛，不构成"查询意图"）
_STOP = set("""
a an the and or of for to in on at by with from as is are was were be been being
this that these those it its into your you our we they them their his her
home page site web website official welcome about contact login sign more all
new latest news blog post posts archive archives tag tags category categories
free best top 2023 2024 2025 2026 http https www com net org io html htm
""".split())

# 泛词：两个词都是泛词时，这个短语没有检索价值（"free online" 之类）
_GENERIC = set("""
free online best top new latest home welcome official site website web page
tool tools service services platform app apps software system online
""".split())

# 英文后缀：与自举出的短语组合，制造查询多样性（避免只会搜原短语）
_SUFFIX_EN = ["directory", "resources", "dataset", "collection", "index",
              "archive", "tools", "list", "repository", "links"]

# 中文后缀（中文没有分词器 ⇒ 用「分类名 × 后缀」造词，质量可控）
_SUFFIX_ZH = ["网站", "资源", "工具", "导航", "大全", "索引", "合集",
              "下载", "站点", "集合"]

# ① 外链矿专用黑名单：**基础设施工艺噪声**。
# 这些是"页面上被引用"的东西（统计脚本/CDN/字体/支付/社交），不是"被推荐的资源"。
# 实测第一版挖 5 个站出 30 个候选，其中 googletagmanager / typekit / tailwindcss /
# stripe / discord 占了一半——不加这层，外链矿就是噪声矿。
# 用子串匹配（域名形态太多，精确列举列不完）。
_INFRA = (
    # 统计 / 广告
    "googletagmanager", "google-analytics", "googleapis", "gstatic", "doubleclick",
    "googlesyndication", "analytics.", "segment.com", "hotjar", "matomo", "statcounter",
    # CDN / 静态资源 / 字体
    "cloudflare", "jsdelivr", "unpkg", "cdnjs", "cdn.", ".cdn", "akamai", "cloudfront",
    "fontawesome", "typekit", "fonts.googleapis", "fonts.gstatic", "gravatar", "wp.com",
    # 社交 / 通讯 / 支付
    "discord", "t.me", "telegram.me", "whatsapp", "facebook", "instagram",
    "twitter", "linkedin", "youtube", "bsky.app", "bsky.social",
    "stripe", "paypal", "checkout", "patreon", "ko-fi",
    # 建站框架 / 前端库（被引用 ≠ 资源站）
    "tailwindcss", "bootstrapcdn", "jquery", "sentry", "wix.com", "squarespace",
    "godaddy", "weebly", "shopify", "hubspot", "mailchimp",
    # 规范 / 通用
    "w3.org", "schema.org", "creativecommons", "opensource.google", "policies.google",
)

# 枢纽站关键词：**优先挖这些站的外链**。
# 一个"资源目录/导航站"的外链，天然就是一串资源站；而普通企业官网的外链
# 多半是社交账号和 CDN ⇒ 挖谁决定了外链矿的品位。
_HUB_HINT = ("目录", "导航", "索引", "资源集合", "链接", "清单", "大全",
             "合集", "在线工具", "awesome", "数据服务", "代码仓库", "论文",
             "文档", "课程", "社区", "论坛")

# ② 自举词黑名单：拦截页 / 错误页的标题产生的短语（"unsupported browser" 之类）
_BAD_PHRASE = ("unsupported", "browser", "error", "denied", "404", "not found",
               "redirect", "cookie", "javascript", "enable", "attention",
               "moment", "captcha", "robot", "blocked", "forbidden", "timeout",
               "loading", "please wait", "access denied", "just a")


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    """原子写：先写临时文件再替换，避免中途崩溃写坏（和主程序同一套路）。"""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


# ---------------------------------------------------------------- ① 外链矿

def _candidate_sites(n, log=print):
    """挑 n 个**还没挖过**外链的已收录站点，**优先枢纽站**。

    ⚠️ 必须记住挖过谁：否则每轮都从同一批站抓，得到的还是同一批外链，
    等于把"供给增长"又变回固定集合——那就白做了。

    ⚠️ 为什么优先枢纽站（实测教训）：第一版纯随机挖，挖出的外链一半是
    CDN/统计/支付域名。目录站、导航站、awesome 清单的外链天然就是一串资源站，
    挖它们信噪比高一个量级 ⇒ 先挖枢纽，不够再用普通站补。
    """
    data = _load(CLASSIFIED, [])
    pairs = [(x.get("domain"), str(x.get("category") or "")) for x in data
             if isinstance(x, dict) and x.get("domain")]
    if not pairs:                       # classified.json 还没生成 ⇒ 退回资源清单
        pairs = [(x.get("domain"), "") for x in _load(RESOURCES, [])
                 if isinstance(x, dict) and x.get("domain")]
    harvested = set(_load(HARVESTED, []))
    fresh = [(d, c) for d, c in pairs if d not in harvested]
    if len(fresh) < n:                  # 挖过一轮了 ⇒ 清空重挖（站点会更新）
        harvested, fresh = set(), pairs
    hub = [d for d, c in fresh if any(h in c for h in _HUB_HINT)]
    hubset = set(hub)
    rest = [d for d, _ in fresh if d not in hubset]
    random.shuffle(hub)
    random.shuffle(rest)
    picked = (hub + rest)[:n]
    harvested.update(picked)
    _save(HARVESTED, sorted(harvested))
    return picked


def harvest_outlinks(seen_doms=None, n_sites=10, timeout=6, log=print):
    """从已收录站点抓外链，返回候选 URL 列表（未判重，交给 consider）。

    seen_doms：已收录域名集合，传进来可以提前排除，省掉"抓完才发现重复"的开销。
    """
    import resource_finder as rf      # 延迟导入：主程序会导入本模块，避免循环

    picked = _candidate_sites(n_sites, log)
    if not picked:
        return []

    def one(dom):
        html = ""
        for scheme in ("https://", "http://"):
            try:
                html = rf.fetch(scheme + dom, timeout=timeout)
            except Exception:
                html = ""
            if html:
                break
        if not html:
            return []
        got, local = [], set()
        for u in rf.URL_RE.findall(html):
            d = rf.domain(u)
            same = (not d) or d == dom or d.endswith("." + dom)
            if same:
                # 站群型（同域多子站）：路径首段不同即为兄弟站，放行
                if not (d and d in getattr(rf, "SIBLING_DOMS", ())):
                    continue                  # 普通站：站内链接不要，只要外链
                sk = rf.site_key(u) or d
            else:
                sk = d
            if d in rf.SKIP or rf._junk(d):
                continue
            if any(inf in d for inf in _INFRA):   # 🔴 基础设施噪声（CDN/统计/支付/社交）
                continue                            # 实测第一版挖 5 站出 30 候选，一半是这类
            if seen_doms is not None and sk in seen_doms:
                continue                      # 已收录
            if sk in local:
                continue                      # 同一页面同一站点只取一次
            local.add(sk)
            got.append(u)
            if len(got) >= 40:                # 单站上限，防大站刷屏
                break
        return got

    out = []
    with ThreadPoolExecutor(max_workers=min(8, len(picked))) as ex:
        for got in ex.map(one, picked):
            out.extend(got)
    if out:
        sample = sorted(set(rf.domain(u) for u in out))[:3]
        log("[evolve:link] 挖 %d 个站外链 → %d 个新候选（例：%s）"
            % (len(picked), len(out), "、".join(sample)), flush=True)
    return out


# ------------------------------------------------------------ ② 关键词自举

_KW_CACHE = {"ts": 0.0, "kw": []}


def bootstrap_keywords(limit=80, ttl=600, log=print):
    """从已收录站点的 title 抽高频短语，造新查询词（中英双语）。

    缓存 ttl 秒：每轮都重算 5000+ 条标题没必要，10 分钟刷新一次足够
    （收录增长速度远慢于这个频率）。
    """
    now = time.time()
    if _KW_CACHE["kw"] and now - _KW_CACHE["ts"] < ttl:
        return _KW_CACHE["kw"]

    data = _load(CLASSIFIED, [])
    titles = [(x.get("title") or "") for x in data
              if isinstance(x, dict) and x.get("title")]
    cats = sorted({x.get("category") for x in data
                   if isinstance(x, dict) and x.get("category")
                   and "其他" not in str(x.get("category"))
                   and "待定" not in str(x.get("category"))
                   and "JS" not in str(x.get("category"))})

    # —— 英文：语料 2-gram，频次>=2 的才要（出现一次可能是噪声） ——
    phrases = Counter()
    for t in titles:
        tl = t.lower()
        if any(b in tl for b in _BAD_PHRASE):   # 🔴 拦截页/错误页标题（"unsupported browser"等）
            continue                            # 第一版自举出 "unsupported browser" 这种垃圾词
        ws = re.findall(r"[a-zA-Z][a-zA-Z0-9\-]{2,}", tl)
        ws = [w for w in ws if w not in _STOP and len(w) > 3]
        for i in range(len(ws) - 1):
            a, b = ws[i], ws[i + 1]
            if a in _GENERIC and b in _GENERIC:
                continue
            phrases[a + " " + b] += 1
    solid = [(p, c) for p, c in phrases.items() if c >= 2]
    solid.sort(key=lambda x: (-x[1], x[0]))
    en = [p for p, _ in solid[:limit]]
    # 不足就用频次 1 的补齐（早期收录少时会有这种情况）
    if len(en) < limit // 2:
        rest = [p for p, _ in phrases.most_common()
                if p not in en][:limit - len(en)]
        en += rest

    # 短语 + 领域后缀：制造多样性，避免只会搜原短语撞回同一批结果
    if en:
        extra = ["%s %s" % (random.choice(en), random.choice(_SUFFIX_EN))
                 for _ in range(min(20, len(en)))]
        en += extra

    # —— 中文：分类名 × 后缀模板（无分词器，走模板保证质量） ——
    zh = []
    for c in cats:
        core = re.split(r"[/（(]", str(c))[0].strip()
        if not core:
            continue
        zh.append(core + " " + random.choice(_SUFFIX_ZH))

    kw = en + zh
    random.shuffle(kw)
    _KW_CACHE.update(ts=now, kw=kw)
    log("[evolve:boot] 自举出 %d 个查询词（英文短语 %d / 中文 %d），例：%s"
        % (len(kw), len(en), len(zh), "、".join(kw[:3])), flush=True)
    return kw


def bootstrap_search(per_round=4, log=print):
    """用自举出来的关键词跑搜索（引擎轮换，不吊死在一个源上）。"""
    import resource_finder as rf

    kws = bootstrap_keywords(log=log)
    if not kws:
        return []
    engines = [rf.bing_pump, rf.marginalia_pump, rf.searx_pump,
               rf.wiby_pump, rf.mojeek_pump]
    picked = random.sample(kws, min(per_round, len(kws)))
    out = []
    for kw in picked:
        try:
            out.extend(random.choice(engines)(kw) or [])
        except Exception:
            continue
    if out:
        log("[evolve:boot] 本轮用「%s」等 %d 个自举词 → 引擎返回 %d 条"
            % (picked[0], len(picked), len(out)), flush=True)
    return out
