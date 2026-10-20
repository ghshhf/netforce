# -*- coding: utf-8 -*-
"""
netforce / resource_finder.py
资源站发现器（锚点递归版）—— 不是扫全网，是从种子 awesome-list 递归爬 URL。

为什么不是"扫全网"：网络太大，扫不动也不现实。正确做法 = 从已知锚点
（GitHub awesome lists / 学术目录）出发，递归发现链接，验证可达，收集成资源站清单。
用户原话："扫描全网...你感觉可能吗？网络多大呀" / "就那几个域名，相当于这种情况下才能掌握真正的互联网资源"。

运行：后台跑到早上 6:00（或集满 100 个即停），结果落 resources.json。
出网走本机代理 127.0.0.1:3067。
"""
import urllib.request
import urllib.parse
import re
import json
import time
import datetime
import ssl
import subprocess
import os
import threading
from concurrent.futures import ThreadPoolExecutor

PROXY = os.environ.get("NETFORCE_PROXY", "http://127.0.0.1:3067")   # 置空字符串 = 全程直连
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
# 浏览器UA（短链/落地页常有 UA 校验，必须用像样的 UA 才给重定向）
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ---- 去重（借鉴 Scrapy 的 dupefilter：请求指纹）----
# 同一个页面会因为一堆无意义参数变成"不同URL"，不规范化就会重复抓、重复入库。
# 这里把 host 小写、去 fragment、去跟踪参数（utm_*/fbclid/gclid…）、去默认端口、
# 去末尾斜杠、统一协议 http/https 等价，生成请求指纹 key。
_TRACKING = re.compile(
    r"^(utm_[a-z_]+|fbclid|gclid|msclkid|igshid|mc_[ce]id|ref|ref_|referrer|source|src|"
    r"spm|share_|_hsenc|_hsmi|yclid|ttclid|si)$", re.I)


def canon(u):
    """把 URL 规范化成去重指纹（Scrapy dupefilter 的等价物）。"""
    try:
        p = urllib.parse.urlsplit(u.strip())
    except Exception:
        return u.strip().lower()
    scheme = "http"                     # http/https 视为同一资源
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = p.port
    if port and port not in (80, 443):
        host = "%s:%d" % (host, port)
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    # GitHub 的 raw/README 与 README、blob/x 与 blob/X 是同一资源；实测 gh_pump 返回的
    # 200 条里约一半是这种大小写重复，不归一就是白跑一半。不做全站小写是因为部分服务器
    # 路径大小写敏感，全局小写会误合并。
    if host.endswith("githubusercontent.com") or host == "github.com":
        path = path.lower()
    qs = urllib.parse.urlencode(
        [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
         if not _TRACKING.match(k)])
    return urllib.parse.urlunsplit((scheme, host, path, qs, ""))


_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
VISITED_FILE = os.path.join(_HERE_DIR, '_visited.json')   # 旧版指纹（迁移种子用）
QUEUE_INJECT_MAX = 60     # 队列到这个深度就暂停注入新种子（只是节流，不是停止爬取）
# ↑ 原值 800。实测 2026-10-21 00:2x：队列长期停在 85（泵只入册、不产目录页种子），
#   永远够不到阈值 ⇒ `queue.pop(0)` 那条分支形同死代码，队列成了只进不出的死水，
#   SEEDS / 定向站群种子永远轮不到。降到 60 低于当前水位，让它真正开始出队。
VISITED_DB = os.path.join(_HERE_DIR, '_visited.db')       # Scrapy dupefilter 同款 SQLite

# 噪声域名识别：只用于**调度评分**（不算有效产出），条目照收不删（零删除铁律）。
# 成熟爬虫统计的是 yield（下游过滤后的有效产出）而不是原始命中数——本轮实测 wiki 泵
# 一次"命中 199"里 30 条是 vox/latimes/npr 这类新闻媒体，不扣掉就会被调度器当成高产源。
# ⚠️ 别把 mail/spam/security 之类真资源词写进来：会误伤邮箱/安全站。
NOISE_RE = re.compile(
    r"(news|times|post|press|guardian|cnn|bbc|reuters|msn\.com|vox|buzzfeed|vice|"
    r"npr|gizmodo|wired|theverge|mashable|huffpost|politico|axios|sphere|mic\.com|"
    r"skymark|magazine|shopping|deal|market|coupon|hosting|hostinger|udemy|coursera|"
    r"edx\.org|skillshare|pluralsight|udacity|zendesk|intercom|hubspot|shopify|"
    r"twitter|facebook|instagram|linkedin|tiktok|medium\.com|substack\.com)",
    re.I)


class SeenStore:
    """去重指纹存储（借鉴 Scrapy 的 RFPDupeFilter：SQLite + 主键唯一）。
    比 json 强在：① 插入不用全量重写（几万条后 json 每轮重写就是灾难）；
    ② 天然去重；③ 崩溃不损坏。代价：指纹库丢了只会重爬一遍，**不会丢资源站**
    （资源站本体在 resources.json 里）。"""

    def __init__(self, path=VISITED_DB):
        import sqlite3
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA synchronous=OFF")     # 仅影响去重缓存，丢了下轮重算
        self.c.execute("CREATE TABLE IF NOT EXISTS seen(k TEXT PRIMARY KEY)")
        self.c.commit()
        self.lock = threading.Lock()
        self._n = self.c.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    def __contains__(self, k):
        with self.lock:
            return bool(self._n) and self.c.execute(
                "SELECT 1 FROM seen WHERE k=?", (k,)).fetchone() is not None

    def add(self, k):
        """True = 首次见到（调用方据此决定要不要处理）。"""
        with self.lock:
            cur = self.c.execute("INSERT OR IGNORE INTO seen VALUES(?)", (k,))
            if cur.rowcount:
                self._n += 1
                self.c.commit()
                return True
            return False

    def __len__(self):
        return self._n


def load_visited(seen_doms=None):
    """断点续爬（借鉴 Scrapy JOBDIR）：指纹落盘，重启不重爬。"""
    st = SeenStore()
    if len(st) < 10:      # 空库 → 从历史 json / resources.json 播种一次
        try:
            for k in json.load(open(VISITED_FILE, encoding='utf-8')):
                st.add(k)
        except Exception:
            try:
                for x in json.load(open(os.path.join(_HERE_DIR, 'resources.json'),
                                        encoding='utf-8')):
                    st.add(canon(x.get('url', '') or x.get('domain', '')))
            except Exception:
                pass
    return st


def save_visited(v):
    """指纹已由 SQLite 自动持久化，这里仅为兼容旧调用点（no-op）。"""
    return

# ---- 种子锚点：GitHub raw awesome lists（递归发现从这里开始） ----
SEEDS = [
    # 🔴 定向扩散入口（二次元站群）：wiki.biligame.com 首页挂着全站 1,199 个子站，
    #    抓这一页 → 兄弟站被 SIBLING_DOMS 放行 → 按 wiki_siblings.txt 白名单扩到全站。
    #    加 ys（原神）角色图鉴作为方向锚点：它是已验证 200 的二次元页面。
    "https://wiki.biligame.com/wiki/%E9%A6%96%E9%A1%B5",
    "https://wiki.biligame.com/ys/%E8%A7%92%E8%89%B2%E5%9B%BE%E9%89%B4",
    "https://raw.githubusercontent.com/josephmisiti/awesome-machine-learning/master/readme.md",
    "https://raw.githubusercontent.com/ChristosChristofidis/awesome-deep-learning/master/readme.md",
    "https://raw.githubusercontent.com/awesomedata/awesome-public-datasets/master/readme.md",
    "https://raw.githubusercontent.com/andremann/awesome-scholarly-data-analysis/master/readme.md",
    "https://raw.githubusercontent.com/brandonhimpfen/awesome-ai-research-tools/master/readme.md",
    "https://raw.githubusercontent.com/husthuke/awesome-knowledge-graph/master/readme.md",
    "https://raw.githubusercontent.com/kjw0612/awesome-deep-vision/master/readme.md",
    "https://raw.githubusercontent.com/terryum/awesome-deep-learning-papers/master/readme.md",
    "https://raw.githubusercontent.com/eugeneyan/awesome-llm.md/master/readme.md",
    "https://raw.githubusercontent.com/ZuzooVn/machine-learning-for-software-engineers/master/README.md",
    "https://raw.githubusercontent.com/keon/awesome-nlp/master/README.md",
    "https://raw.githubusercontent.com/jbhuang0604/awesome-computer-vision/master/README.md",
    "https://raw.githubusercontent.com/academic/awesome-datascience/master/README.md",
    "https://raw.githubusercontent.com/OpenTalker/awesome-rl/master/README.md",
    "https://raw.githubusercontent.com/eginhard/awesome-ai/master/README.md",
    # —— 开源 / 工具箱方向种子（工具箱常带短链）——
    "https://raw.githubusercontent.com/awesome-selfhosted/awesome-selfhosted/master/README.md",
    "https://raw.githubusercontent.com/ianSimpson/awesome-ops/master/README.md",
    # —— 大学站方向种子：从官网出发，顺着链接往外挖论文/实验室/课程页（本质是看论文）——
    "https://www.mit.edu/", "https://www.stanford.edu/", "https://www.harvard.edu/",
    "https://www.berkeley.edu/", "https://www.cmu.edu/", "https://www.caltech.edu/",
    "https://www.ox.ac.uk/", "https://www.cam.ac.uk/", "https://www.ethz.ch/",
    "https://www.epfl.ch/", "https://www.toronto.edu/", "https://www.ubc.ca/",
    "https://www.utoronto.ca/", "https://www.ucl.ac.uk/", "https://www.imperial.ac.uk/",
    "https://www.tsinghua.edu.cn/", "https://www.pku.edu.cn/", "https://www.ustc.edu.cn/",
    "https://www.sjtu.edu.cn/", "https://www.zju.edu.cn/", "https://www.fudan.edu.cn/",
    "https://www.nus.edu.sg/", "https://www.kyoto-u.ac.jp/", "https://www.titech.ac.jp/",
    "https://www.umich.edu/", "https://www.washington.edu/", "https://www.illinois.edu/",
    "https://www.gatech.edu/", "https://www.uw.edu/", "https://www.ucla.edu/",
    # —— 加密 / Web3 方向种子 ——
    "https://raw.githubusercontent.com/igorbarinov/awesome-blockchain/master/README.md",
    "https://raw.githubusercontent.com/despiegk/awesome-web3/master/README.md",
    "https://raw.githubusercontent.com/sobolevn/awesome-cryptography/master/README.md",
    # —— 虚拟卡 / 加密支付方向种子（注册钱包送虚拟卡、充虚拟币走正常支付；
    #     要的是"卡"本身——能过某些平台绑卡/支付验证从而解锁更多资源，不是微信支付功能）——
    "https://redotpay.com/", "https://dupay.com/", "https://www.noones.com/",
    "https://paxful.com/", "https://wirexapp.com/", "https://utorg.com/",
    # —— 应用/软件库方向种子（起因：抖音 @林挽夏「爱语」App 的第三方分发站；
    #     应用库本身就是资源站，里面大量 AI/工具类 App 可继续外挖）——
    "https://www.beihangsoft.cn/",       # 北行软件库（爱语收录页 /app/8327.html）
    "http://www.baobeiy.com/",           # 宝贝游戏网·安卓软件库
    "https://www.5577.com/",             # 5577 安卓网
    "https://www.appinn.com/",           # 小众软件（老牌软件推荐库）
    "https://www.iplaysoft.com/",        # 异次元软件世界
    # —— 提速轮新增种子（2026-10-18）：目录站 + 软件站 + 数据集站 + 更多大学 ——
    # 选型理由：这些站本身就是"资源聚合体"，顺着它们的外链挖，新域名密度远高于搜索结果页。
    "https://curlie.org/Computers/Software/",   # 人工分类目录（种子即目录）
    "https://alternativeto.net/",               # 开源/商业软件替代库
    "https://paperswithcode.com/",              # 论文+代码+数据集三位一体
    "https://www.openml.org/",                  # 机器学习数据集库
    "https://zenodo.org/",                      # 欧盟开放仓储（论文/数据/软件）
    "https://figshare.com/",                    # 数据集/图表仓储
    "https://sourceforge.net/",                 # 老牌开源软件库
    "https://www.52pojie.cn/",                  # 吾爱破解（中文软件资源）
    "https://www.423down.com/",                 # 423down（中文软件下载）
    "https://www.onlinedown.net/",              # 华军软件园
    # 大学（补齐亚洲/欧洲/澳洲，大学的院系页是论文/课程/数据集的富矿）
    "https://www.u-tokyo.ac.jp/", "https://www.anu.edu.au/",
    "https://www.unimelb.edu.au/", "https://www.kaist.ac.kr/",
    "https://www.tum.de/", "https://www.mcgill.ca/",
    "https://www.hku.hk/", "https://www.ntu.edu.tw/",
    "https://www.sorbonne-universite.fr/", "https://www.uni-heidelberg.de/",
    "https://www.kcl.ac.uk/", "https://www.aalto.fi/",
]

# 种子即资源的站点：除了当源页面爬链接，自身域名也要注册进 resources.json
REGISTER_SEEDS = [
    "https://www.beihangsoft.cn/",
    "http://www.baobeiy.com/",
    "https://www.5577.com/",
    "https://www.appinn.com/",
    "https://www.iplaysoft.com/",
    # 提速轮新增：这些站本身就是资源站（不只是爬链接的跳板），要登记进清单
    "https://alternativeto.net/", "https://paperswithcode.com/",
    "https://www.openml.org/", "https://zenodo.org/", "https://figshare.com/",
    "https://sourceforge.net/", "https://www.52pojie.cn/",
    "https://www.423down.com/", "https://www.onlinedown.net/",
]

# ---- GitHub 搜索泵：让种子队列永不枯竭（"无限扫无限扩"）----
# 轮换多条查询 + 翻页，每次拉 100 个 awesome 仓库的 README 作为新种子。
GH_QUERIES = [
    "awesome+in:name",
    "awesome+in:name,description",
    "topic:awesome",
    "awesome-list+in:name",
    "curated+list+in:description",
    "resources+in:name+language:markdown",
    "free+in:name+stars:%3E200",
    "cheat+sheet+in:name",
    "guide+in:name+language:markdown",
    "papers+in:name+language:markdown",
]


def gh_pump(query=None, page=1):
    """从 GitHub 搜索 API 拉一页 awesome 仓库 → raw README 地址列表。"""
    url = ("https://api.github.com/search/repositories"
           "?q=%s&sort=stars&order=desc&per_page=100&page=%d" % (query, page))
    try:
        raw = fetch(url, 20)
        data = json.loads(raw)
        out = []
        for it in data.get("items", []):
            fn, br = it.get("full_name"), it.get("default_branch") or "master"
            if not fn:
                continue
            out.append("https://raw.githubusercontent.com/%s/%s/README.md" % (fn, br))
            out.append("https://raw.githubusercontent.com/%s/%s/readme.md" % (fn, br))
        return out
    except Exception as e:
        print("  [pump] %s p%d 失败: %s" % (query, page, repr(e)[:80]), flush=True)
        return []


# ---- 多引擎搜索泵：不能只盯 GitHub（用户原话）----
# 轮换 DuckDuckGo / Bing / Mojeek / Hacker News / Wikipedia 外链 / Reddit，
# 配多方向多语言关键词池，覆盖 AI 之外的各种领域。
KEYWORDS = [
    # AI / 大模型
    "LLM 资源站", "免费大模型 API", "开源大模型 列表", "AI agent 工具", "prompt 资源库",
    "local LLM inference", "free AI API", "open source LLM", "AI agent directory",
    "vector database", "RAG 工具", "embedding model",
    # 学术 / 论文
    "论文预印本 网站", "开放获取 期刊", "学术数据集 下载", "arxiv 替代", "open access journal",
    "preprint server", "research paper repository", "citation database",
    # 数据
    "公开数据集", "开放数据 平台", "government open data", "public dataset repository",
    "金融数据 API", "地理数据 下载", "气象数据 开放", "statistics portal",
    # 开发
    "开源工具 合集", "free developer tools", "API 目录", "self-hosted software",
    "开源软件 镜像", "documentation search", "code search engine",
    # 学习 / 图书
    "免费电子书", "公开课 平台", "free ebook library", "online course free",
    "教材 PDF 下载", "MOOC 平台", "lecture notes",
    # 媒体 / 社区
    "技术博客 推荐", "个人博客 独立", "podcast tech", "forum community developer",
    "newsletter 技术", "独立开发者 社区",
    # 其他方向（多域名 ≠ 只有 AI）
    "生物信息 数据库", "化学 数据库 开放", "医学文献 检索", "astronomy data archive",
    "古籍 数字化", "博物馆 数字馆藏", "地图 数据 开放", "专利 检索 免费",
    "标准 文档 下载", "纪录片 资源", "乐谱 免费 下载", "3D 模型 下载",
    "字体 免费 下载", "图标 资源站", "摄影 图库 免费", "设计素材 免费",
    "游戏 资源 开源", "模拟器 ROM 合法", "棋盘 game AI", "机器人 开源项目",
    "物联网 平台 开源", "区块链 浏览器", "密码学 工具", "安全 工具 开源",
    "前端 组件库 合集", "后端 框架 对比", "数据库 引擎 开源", "消息队列 对比",
    "运维 工具 开源", "监控 系统 开源", "测试 工具 开源", "CI CD 平台",
    "数据可视化 工具", "笔记软件 开源", "知识库 开源", "wiki 引擎",
    "翻译 工具 开源", "OCR 工具 开源", "语音识别 开源", "视频 工具 开源",
    # —— 论文 / 学术（用户指定方向）——
    "论文 预印本 平台", "学位论文 数据库", "会议论文 检索", "期刊 开放获取",
    "thesis repository", "dissertation database", "conference proceedings archive",
    "paper with code", "literature review tool", "reference manager 开源",
    "sci hub 替代", "学术搜索引擎", "citation graph", "research paper API",
    "stanford 论文", "mit 论文 开放", "arxiv mirror", "semantic scholar 替代",
    # —— 学校 / 高校机构（用户指定方向）——
    "计算机学院 实验室 数据集 site:.edu", "university research dataset site:.edu",
    "lab homepage computer vision site:.edu", "faculty page machine learning site:.edu",
    "高校 开放课程 课件", "university open courseware site:.edu",
    "research group publications site:.ac.uk", "institute technical report site:.edu.cn",
    "大学 图书馆 电子资源", "university digital repository site:.edu",
    # —— 服务器（用户指定方向）——
    "自建服务器 教程", "家用服务器 方案", "homelab 项目", "self hosted server 开源",
    "服务器 监控 开源", "serverless 平台 开源", "免费 服务器 试用", "free tier 云服务",
    "VPS 评测 网站", "服务器 硬件 数据库", "server benchmark tool",
    "reverse proxy 开源", " NAS 系统 开源", "私有云 开源", "kubernetes 工具 合集",
    "分布式存储 开源", "内网穿透 开源", "节点 探针 开源", "edge computing 平台 开源",
    "免费 GPU 算力", "免费 云计算 额度", "免费 容器 托管", "免费 静态托管",
    # —— 开源 / 工具箱（用户指定方向，工具箱常带短链）——
    "开源工具箱", "开源工具集", "自托管工具箱", "开发工具箱", "运维工具箱",
    "开发者工具箱", "homelab 工具箱", "开源工具 推荐", "免费开源 工具",
    "open source toolbox", "developer toolkit", "self-hosted toolbox",
    "dev toolbox", "open source toolkit collection", "awesome tools",
    "open source utilities", "homelab apps", "self-hosted apps list",
    # —— 贴吧（用户指定方向：吧内资源/分享可引出更多方向）——
    "贴吧 资源分享", "百度贴吧 开源", "贴吧 工具箱", "贴吧 黑科技 工具",
    "贴吧 自学 资源", "贴吧 软件 分享", "tieba 资源", "贴吧 网盘 资源",
]

SUBREDDITS = ["MachineLearning", "LocalLLaMA", "datasets", "opensource", "selfhosted",
              "datascience", "programming", "learnmachinelearning", "OpensourceWriters",
              "DataHoarder", "freeculture", "OpenData",
              "academia", "AskAcademia", "gradschool", "Physics", "math",
              "homelab", "sysadmin", "servers", "devops", "kubernetes", "cloudcomputing",
              "HomelabSales", "DataCenter", "networking"]


def _soup_urls(html, excl):
    """从搜索结果页抽出站外链接。"""
    out = []
    for u in URL_RE.findall(html):
        u = u.rstrip('.,);]}>"\'`').replace("&amp;", "&")
        if not u.startswith("http"):
            continue
        d = domain(u)
        if not d or _junk(d) or d in SKIP:
            continue
        if any(e in d for e in excl):
            continue
        out.append(u)
    # 去重保序
    seen, res = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            res.append(u)
    return res


def ddg_pump(kw):
    """DuckDuckGo html 端，必须 POST 才有结果。"""
    u = "https://html.duckduckgo.com/html/"
    try:
        data = urllib.parse.urlencode({"q": kw}).encode()
        req = urllib.request.Request(u, data=data, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "Chrome/124.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded"})
        h = _opener().open(req, timeout=20).read().decode("utf-8", "ignore")
        return _soup_urls(h, ["duckduckgo.com"])
    except Exception:
        return []


def bing_pump(kw):
    """Bing 的 RSS 输出（避免 JS 化页面）。"""
    u = "https://www.bing.com/search?q=" + urllib.parse.quote(kw) + "&format=rss&count=50"
    h = fetch(u)
    out = []
    for m in re.findall(r"<link>(.*?)</link>", h, re.I | re.S):
        m = m.strip()
        if m.startswith("http") and "bing.com" not in m:
            out.append(m)
    return out or _soup_urls(h, ["bing.com", "microsoft.com", "msn.com", "live.com"])


def google_pump(kw):
    """Google 直搜（走本机代理出口）。"""
    u = "https://www.google.com/search?num=50&hl=en&q=" + urllib.parse.quote(kw)
    try:
        req = urllib.request.Request(u, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "Chrome/124.0 Safari/537.36"})
        h = _opener().open(req, timeout=20).read().decode("utf-8", "ignore")
        out = []
        for m in re.findall(r'href="/url\?q=(https?://[^&"]+)', h):
            out.append(urllib.parse.unquote(m))
        return out or _soup_urls(h, ["google.com", "gstatic.com", "googleusercontent.com"])
    except Exception:
        return []


def mojeek_pump(kw):
    u = "https://www.mojeek.com/search?q=" + urllib.parse.quote(kw)
    return _soup_urls(fetch(u), ["mojeek.com"])


# ============================================================================
# 新增三个搜索源（2026-10-18 提速轮）
# 判断依据：去重指纹已 2.4 万，老泵关键词反复命中同批结果（日志大量"去重后 0 待查"），
# 入册率掉到 7% ⇒ 瓶颈是**新 URL 供给**，不是并发（并发已自适应到 54）。
# 选型标准：能产出**老泵没见过的独立小站**。实测产出（单次查询的链接数）：
#   old-search.marginalia.nu 130 ★ Marginalia 专索引"非主流小站"，天然是我们要的
#   curlie.org 深类目       113 ★ DMOZ 正统继任者，人工审过的分类目录
#   searxng.site            105 ★ SearXNG 公共实例，一次聚合多引擎
#   已排除：baidu（反爬，返回 1438 字节拦截页）、searx.be/inetol/opnx（0 结果）
# ============================================================================

MARGINALIA_HOSTS = ["https://old-search.marginalia.nu",
                    "https://search.marginalia.nu"]


def marginalia_pump(kw):
    """Marginalia：索引的是"搜索引擎看不见的小站"，域名新鲜度远高于 Bing/Google。"""
    out = []
    for host in MARGINALIA_HOSTS:
        try:
            u = host + "/search?query=" + urllib.parse.quote(kw)
            got = _soup_urls(fetch(u, 12), ["marginalia.nu", "kagi.com"])
            if got:
                out.extend(got)
                break                      # 主站够用就不打备用站（礼貌 + 省时）
        except Exception:
            continue
    return out


# curlie 的"关键词"就是类目路径：这样自适应调度会自动轮换类目、自动降权空类目。
CURLIE_PATHS = [
    "/Computers/Software/", "/Computers/Software/Freeware/",
    "/Computers/Software/Open_Source/", "/Computers/Artificial_Intelligence/",
    "/Computers/Programming/", "/Computers/Internet/", "/Computers/Security/",
    "/Computers/Multimedia/", "/Computers/Data_Formats/", "/Computers/Systems/",
    "/Science/", "/Science/Mathematics/", "/Science/Physics/",
    "/Science/Biology/", "/Science/Astronomy/", "/Reference/Directories/",
    "/Reference/Libraries/", "/Games/", "/Business/", "/Education/",
]


def curlie_pump(path):
    """curlie.org 类目页：不是搜索，是**直接掀人工分类目录**，一页上百个独立站点。"""
    u = "https://curlie.org" + (path if path.startswith("/") else "/" + path)
    return _soup_urls(fetch(u, 12), ["curlie.org", "dmoztools.net"])


SEARX_HOSTS = ["https://searxng.site", "https://searx.tiekoetter.com",
               "https://baresearch.org"]


def searx_pump(kw):
    """SearXNG 公共实例：一个查询同时拿多家引擎的结果，且不用 key。
    实例会挂 ⇒ 逐个试，谁先出结果用谁。"""
    for host in SEARX_HOSTS:
        try:
            u = host + "/search?q=" + urllib.parse.quote(kw)
            got = _soup_urls(fetch(u, 12), ["searx", "github.com", "wikipedia.org"])
            if got:
                return got
        except Exception:
            continue
    return []


def wiby_pump(kw):
    """wiby.me：专索引"老式个人主页/小站"，与主流引擎的命中几乎不重叠。"""
    u = "https://wiby.me/json/?q=" + urllib.parse.quote(kw)
    return _soup_urls(fetch(u, 12), ["wiby.me"])


WIBY_KEYWORDS = ["software", "archive", "research", "tools", "library",
                 "download", "index", "database", "linux", "science"]


def hn_pump(kw):
    u = ("https://hn.algolia.com/api/v1/search?query=%s&tags=story&hitsPerPage=100"
         % urllib.parse.quote(kw))
    h = fetch(u)
    try:
        data = json.loads(h)
        return [x.get("url") for x in data.get("hits", []) if x.get("url")]
    except Exception:
        return []


def wiki_pump(kw):
    """维基条目外部链接 = 一手资源站。"""
    s = ("https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=%s"
         "&srlimit=5&format=json" % urllib.parse.quote(kw))
    try:
        data = json.loads(fetch(s))
        titles = [x["title"] for x in data["query"]["search"]]
    except Exception:
        return []
    out = []
    for t in titles[:3]:
        e = ("https://en.wikipedia.org/w/api.php?action=query&prop=extlinks&titles=%s"
             "&ellimit=500&format=json" % urllib.parse.quote(t))
        try:
            d2 = json.loads(fetch(e))
            for pid, pg in d2.get("query", {}).get("pages", {}).items():
                for l in pg.get("extlinks", []):
                    uu = l.get("*")
                    if uu and uu.startswith("http"):
                        out.append(uu)
        except Exception:
            continue
    return out


def reddit_pump(sub):
    u = "https://www.reddit.com/r/%s/top.json?limit=100&t=all" % sub
    try:
        return [c["data"]["url"] for c in json.loads(fetch(u, 15)).get("data", {})
                .get("children", []) if c.get("data", {}).get("url")]
    except Exception:
        return []


def tieba_pump(kw):
    """百度贴吧帖子搜索：吧内常有人分享工具/资源外链，可引出更多方向。

    只留站外链接（排除贴吧/百度全系，含 pan.baidu.com 网盘噪声）。
    """
    u = "https://tieba.baidu.com/s/f?ie=utf-8&kw=" + urllib.parse.quote(kw)
    try:
        h = fetch(u)
        return _soup_urls(h, ["tieba.baidu.com", "baidu.com"])
    except Exception:
        return []


def openalex_pump(kw):
    """OpenAlex 学术 API：论文落地页 → 出版社/预印本/机构站。"""
    u = "https://api.openalex.org/works?search=%s&per-page=100" % urllib.parse.quote(kw)
    try:
        data = json.loads(fetch(u))
        out = []
        for w in data.get("results", []):
            loc = (w.get("primary_location") or {}).get("landing_page_url")
            if loc:
                out.append(loc)
            for a in w.get("authorships", [])[:2]:
                for inst in a.get("institutions", [])[:1]:
                    hp = inst.get("homepage_url")
                    if hp:
                        out.append(hp)
        return out
    except Exception:
        return []


def crossref_pump(kw):
    """Crossref：论文 DOI 落地页 → 期刊/出版社站。"""
    u = ("https://api.crossref.org/works?query=%s&rows=100&select=URL,title"
         % urllib.parse.quote(kw))
    try:
        data = json.loads(fetch(u))
        return [it.get("URL") for it in data.get("message", {}).get("items", [])
                if it.get("URL")]
    except Exception:
        return []


# 英文源（HN/Crossref）必须用英文关键词，中文词命中 0
KEYWORDS_EN = [k for k in KEYWORDS if not re.search(r"[\u4e00-\u9fff]", k)]
# 大学站方向关键词：搜"大学官网 + 论文/实验室/开放获取"，命中页再交给爬虫顺着官网往外挖
UNIV_KEYWORDS = [
    "university machine learning lab publications",
    "cs department faculty publications site:.edu",
    "university open access thesis repository",
    "course lecture notes site:.edu machine learning",
    "research group publications deep learning site:.edu",
    "university dataset benchmark release site:.edu",
    "professor homepage publications computer science",
    "graduate thesis dissertation repository site:.edu",
    "university arxiv preprint mirror",
    "conference proceedings papers university site:.edu",
    "大学 开放获取 论文 库",
    "高校 学术论文 主页 实验室",
    "大学 课程 讲义 机器学习",
    "高校 数据集 发布",
    "大学 实验室 论文 列表",
]
# 加密 / Web3 方向关键词
CRYPTO_KEYWORDS = [
    "crypto+in:name,description", "web3+in:name", "blockchain+in:name",
    "wallet+crypto+in:description", "defi+in:name", "airdrop+in:name",
    "ethereum+tools+in:description", "on-chain+analytics+in:name",
    "token+analytics+in:name", "mixnet+in:name",
]
# 虚拟卡 / 加密支付方向关键词（GitHub 搜索式）：要的是"卡"本身（过平台绑卡/支付验证解锁资源），不是支付功能
VCARD_KEYWORDS = [
    "virtual+card+in:name", "crypto+card+in:name,description",
    "usdt+card+in:name", "prepaid+card+crypto+in:description",
    "anonymous+card+in:name", "privacy+card+in:name",
    "card+no+kyc+in:description", "crypto+debit+in:name",
]
# 应用/软件库方向关键词（走 bing+google 双通道，同 univ_pump）：应用下载站/软件库本身就是资源站，
# 库内大量 AI App/工具可顺链接外挖（起因：「爱语」第三方分发站）
APP_KEYWORDS = [
    "安卓 软件下载站 应用库",
    "AI 应用 安卓 下载站",
    "免费软件 下载站 收录 库",
    "android apk download site collection",
    "free software download directory site",
]

# ---- 查询词分桶（成熟爬虫的做法：查询词必须匹配来源特性，别拿中文长尾词喂学术 API）----
# GitHub 搜索对精准英文词命中率最高（它匹配仓库名/描述），所以安全/OSINT/代理/邮箱/
# 账号/大模型这些"找项目"的词全部走 gh_pump。
GH_TOOL_KEYWORDS = [
    "awesome-infosec", "awesome-osint", "awesome-cybersecurity", "awesome-selfhosted",
    "awesome-linux", "awesome-cli-apps", "awesome-mac", "awesome-windows",
    "pentest toolkit", "ctf resources", "reverse engineering tools", "malware lab",
    "osint tools", "threat intelligence platform", "vulnerability database",
    "free proxy list", "clash proxy config", "socks proxy list", "wireguard config",
    "temporary email service", "self-hosted mail server", "email verification api",
    "username availability api", "account verification service",
    "awesome-llm", "local llm server", "gguf model", "llm inference server",
    "awesome-cryptography", "awesome-blockchain", "crypto toolkit",
    "game asset library", "open source game engine", "open source games",
    "apk mirror", "android app repository", "self-hosted file browser",
    "awesome-datasets", "awesome-public-datasets", "huggingface datasets",
]

# 学术 API（Crossref/OpenAlex）只吃"学术语汇"，之前喂 "local LLM inference" 这类工程词
# 返回的全是论文落地页（既不是资源站、又极易重复），是低产出泵的主因。
ACADEMIC_KEYWORDS = [
    "dataset", "benchmark", "knowledge graph", "information retrieval",
    "computer vision dataset", "speech recognition corpus", "robotics dataset",
    "materials database", "protein structure", "climate dataset", "genome",
    "network traffic dataset", "time series archive", "annotation corpus",
    "evaluation benchmark", "survey dataset", "multimodal dataset",
]

# 中文资源向（搜索引擎/贴吧吃长尾词最合适）
ZH_RESOURCE_KEYWORDS = [
    "开源项目 导航站", "免费开源 工具 合集", "自托管 服务 列表",
    "学术资源 分享 站", "数据集 下载 站", "免费 电子书 站点",
    "公开课 平台 汇总", "开源镜像 站", "免费 API 集合",
    "网盘资源 索引", "开源软件 下载 站", "开源应用 商店",
    # AI 前沿方向（用户 2026-10-17 补充："过去没给 AI 关键词，可能爬的时候很多找不到"）
    "AI 智能体 开源 项目", "自进化 AI 研究", "AGI 论文 博客",
    "大模型 压缩 部署 优化", "机器人 开源 框架", "具身智能 学习",
    "MCP 协议 教程", "大模型 技能 插件", "论文 精读 工具",
    "上下文 压缩 技巧", "提示词 工程 工具",
]

# ---- 2026-10-17 用户新增方向（"过去没给AI 关键词，可能找不到"）----
# AGI / 自进化 / 自适应：这批走 GitHub 搜索（仓库名+描述匹配最准）
GH_AGI_KEYWORDS = [
    "agi", "artificial general intelligence", "self-improving ai",
    "ai self-evolution", "recursive self-improvement", "autonomous agent",
    "self-adaptive ai", "agent evolution", "emergent behavior llm",
    "ai research agent", "automated research", "ai scientist",
    "autonomous llm agent", "continual learning agent", "meta-learning agent",
    "agent memory", "long-horizon agent", "multi-agent framework",
    "openagi", "agi research", "llm reasoning agent",
]

# 机器人开发 / 具身智能
GH_ROBOT_KEYWORDS = [
    "robot learning", "robotics simulation", "embodied ai", "sim2real",
    "humanoid robot", "legged robot", "robot arm control", "ros2",
    "robot navigation", "manipulation policy", "vision-language-action",
    "robot dataset", "reinforcement learning robotics", "drone control",
    "autonomous driving framework", "slam", "robot operating system",
    "dexhand", "open source robot", "robotics simulation benchmark",
]

# Skills / MCP（Agent 技能与工具协议生态）
GH_SKILL_MCP_KEYWORDS = [
    "model context protocol", "mcp server", "mcp client", "mcp tools",
    "agent skills", "claude skills", "agent skill library", "skills marketplace",
    "tool use protocol", "function calling server", "agent toolkit",
    "mcp registry", "agent framework", "agent toolkit github",
    "claude plugin", "agent workspace", "tool integration llm",
    "mcp gateway", "agent middleware",
]

# 极限压缩 / 上下文压缩 / KV cache
GH_COMPRESS_KEYWORDS = [
    "context compression", "kv cache compression", "kv cache",
    "prompt compression", "token compression", "context window",
    "long context llm", "memory compression agent", "llm pruning",
    "quantization llm", "attention compression", "context management llm",
    "llm cache", "sparse attention", "context extension",
    "llm.gguf", "quantized model", "speculative decoding",
]

# 论文相关（配 GitHub 的 papers-with-code 类仓库）
GH_PAPER_KEYWORDS = [
    "paper list", "reading list", "paper tracker", "arxiv tracker",
    "paper reproduction", "papers with code", "awesome papers",
    "research reading group", "literature review tool", "citation manager",
    "paper-to-code", "science agent", "experiment reproduction",
]

# ---- 双语并行（用户 21:43："要么中文要么英文，增加成中文和英文共同搜索，
#      有时候中文、英文搜索出来的不一样"）----
# 实证：GitHub 搜同一个概念，中文词命中中文项目/中文社区资源，英文词命中国际项目，
# **两批站基本不重叠** ⇒ 每个方向中英各来一遍。中文桶同样走 gh_pump（GitHub 支持中文检索）。
ZH_AGI_KEYWORDS = [
    "agi", "通用人工智能", "自进化 智能体", "自我进化 ai",
    "自主智能体 开源", "多智能体 框架", "大模型 智能体 开源",
    "agi 研究 论文", "自动化 科研 ai", "持续学习 智能体",
]
ZH_ROBOT_KEYWORDS = [
    "机器人 开源 框架", "具身智能", "机器人 仿真", "强化学习 机器人",
    "机器人 导航", "机械臂 控制", "ros2", "无人机 开源",
    "自动驾驶 框架", "slam 开源", "人形机器人",
]
ZH_SKILL_MCP_KEYWORDS = [
    "mcp 协议", "mcp 服务器", "模型上下文协议", "mcp 客户端",
    "agent 技能", "智能体 技能 库", "大模型 工具 插件", "工具调用 协议",
    "技能市场", "智能体 中间件", "claude 插件",
]
ZH_COMPRESS_KEYWORDS = [
    "上下文 压缩", "kv cache 压缩", "提示词 压缩", "长上下文 优化",
    "大模型 量化 部署", "显存 优化 推理", "模型 压缩 加速", "推理 加速 技巧",
    "注意力 稀疏 优化", "本地部署 大模型",
]
ZH_PAPER_KEYWORDS = [
    "论文 清单", "论文 精读", "arxiv 追踪", "论文 复现",
    "开源 论文 项目", "文献管理 工具", "论文 笔记 工具", "科研 助手",
]


def univ_pump(kw):
    """大学站方向：Bing + Google 搜"大学+论文"类词，命中页再交给爬虫顺着官网往外挖。"""
    out = []
    for fn in (bing_pump, google_pump):
        try:
            got = fn(kw)
            if got:
                out.extend(got)
        except Exception:
            continue
    seen, res = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            res.append(u)
    return res[:200]


# 顺序 = 轮换次序（初版权重；运行后由 PUMP_STATS 自动降权零产出的泵）。
# 🔴 查询词必须配对来源特性：学术 API 只吃学术词、GitHub 搜索只吃精准英文词、
#     搜索引擎吃中文长尾 —— 这是本轮实测"13/15 泵产出 0"的根因。
PUMPS = [
    ("gh_tool", gh_pump, GH_TOOL_KEYWORDS),   # GitHub 搜索：项目/清单类命中率最高
    ("gh_agi", gh_pump, GH_AGI_KEYWORDS),    # AGI / 自进化 / 自适应（用户新增方向）
    ("gh_robot", gh_pump, GH_ROBOT_KEYWORDS),  # 机器人开发 / 具身智能
    ("gh_skill", gh_pump, GH_SKILL_MCP_KEYWORDS),  # Skills / MCP 生态
    ("gh_compress", gh_pump, GH_COMPRESS_KEYWORDS),  # 极限压缩 / KV cache / 量化
    ("gh_paper", gh_pump, GH_PAPER_KEYWORDS), # 论文清单 / 复现 / 追踪
    # —— 双语并行：同一方向中英各一泵（用户 21:43）。中英命中的站基本不重叠，
    #    哪个产出差，自适应调度会自动把它降权。
    ("zh_agi", gh_pump, ZH_AGI_KEYWORDS),
    ("zh_robot", gh_pump, ZH_ROBOT_KEYWORDS),
    ("zh_skill", gh_pump, ZH_SKILL_MCP_KEYWORDS),
    ("zh_compress", gh_pump, ZH_COMPRESS_KEYWORDS),
    ("zh_paper", gh_pump, ZH_PAPER_KEYWORDS),
    ("gh_app", gh_pump, APP_KEYWORDS + ["apk mirror site", "android app mirror"]),
    ("github", gh_pump, None),                # awesome-list 目录页注入（队列深度来源）
    ("zh_web", bing_pump, ZH_RESOURCE_KEYWORDS + KEYWORDS),
    ("univ", univ_pump, UNIV_KEYWORDS),       # 实测高产（2/10）
    ("app", univ_pump, APP_KEYWORDS),
    ("academic_x", crossref_pump, ACADEMIC_KEYWORDS),   # 学术 API 喂学术词
    ("academic_o", openalex_pump, ACADEMIC_KEYWORDS),
    ("hn", hn_pump, GH_TOOL_KEYWORDS[:8]),    # HN 只对"项目/工具"类故事有产出
    ("ddg", ddg_pump, KEYWORDS),
    ("tieba", tieba_pump, ZH_RESOURCE_KEYWORDS),
    ("google", google_pump, KEYWORDS[:20]),
    ("mojeek", mojeek_pump, KEYWORDS[:20]),
    ("wiki", wiki_pump, ["awesome list", "open source", "software directory"]),
    ("reddit", reddit_pump, SUBREDDITS),
    ("bing", bing_pump, KEYWORDS[:20]),
    ("crypto", gh_pump, CRYPTO_KEYWORDS),
    ("vcard", gh_pump, VCARD_KEYWORDS),
    # —— 提速轮新增三源（2026-10-18）：产出的域名与老泵几乎不重叠 ——
    ("marginalia", marginalia_pump, KEYWORDS_EN[:40]),   # 小众独立站（实测单次 130 链接）
    ("curlie", curlie_pump, CURLIE_PATHS),               # 人工分类目录（实测 113 链接/类目）
    ("searx", searx_pump, KEYWORDS_EN[:40]),             # 多引擎聚合（实测 105 链接）
    ("wiby", wiby_pump, WIBY_KEYWORDS),                  # 老式小站索引
]


URL_RE = re.compile(r'https?://[^\s\)\]\>"\'`]+', re.I)

# 非资源站域名（排除掉，不算"发现的资源"）
SKIP = {
    'github.com', 'githubusercontent.com', 'twitter.com', 'x.com', 'youtube.com',
    'facebook.com', 'linkedin.com', 'reddit.com', 'wikipedia.org', 'google.com',
    'bing.com', 'duckduckgo.com', 'medium.com', 'stackoverflow.com', 'andrew.cmu.edu',
    'doi.org', 'localhost', 'example.com', 'gist.github.com',
    'shields.io', 'img.shields.io', 'badge.fury.io', 'travis-ci.org', 'circleci.com',
    'guides.github.com', 'lifehacker.com', 'techcrunch.com', 'wired.com', 'cnet.com',
    'theverge.com', 'arstechnica.com', 'engadget.com', 'bbc.com', 'cnn.com',
    'w3.org', 'schema.org', 'w3c.org', 'ns.adobe.com', 'purl.org', 'xmlns.',
}

# 跳板域名：本身不是资源站，但会重定向到真实落地页（如 DOI → 出版社）
REDIRECTORS = {"doi.org", "dx.doi.org", "doi.org/10"}


def _resolve(u):
    """跟随跳板链接，拿真实落地页 URL（才能拿到出版社/机构域名）。"""
    for op in _openers():
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            r = op.open(req, timeout=4)
            real = r.geturl()
            try:
                r.read(64)
            except Exception:
                pass
            if real and real != u:
                return real
        except Exception:
            continue
    return ""


# 短链服务：本身不是资源，但常指向真实工具/资源页（工具箱尤其爱带短链）。
# 不再直接丢弃，而是跟跳到落地页再判定（用户指定方向）。
SHORTENERS = {
    't.co', 'bit.ly', 'tinyurl.com', 'goo.gl', 'ow.ly', 'is.gd', 'buff.ly',
    'cutt.ly', 'rebrand.ly', 'dwz.cn', 'url.cn', 't.cn', 'suo.im', '0x0.st',
    'v.gd', 'tiny.cc', 'rb.gy', 'short.io', 'lnk.to', 's.id', 'mrw.so',
    'youtu.be', 'g.co', 'fb.me', 'amzn.to', 'tinyuri.ca', 'r.be', 'ly',
}

META_RE = re.compile(
    r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+content=["\']?[0-9]*\s*;?\s*'
    r'url=([^"\']+?)(?:["\']?\s*/?>)', re.I)


def _follow_short(u, max_hops=6):
    """跟跳短链到真实落地页：先吃 30x（urllib 自动跟随），再补 meta refresh。

    返回落地 URL；若死链/无法解析/原地打转则返回空串（调用方跳过，绝不收录短链本身）。
    """
    cur = u
    for _ in range(max_hops):
        got = None
        for op in _openers():
            try:
                req = urllib.request.Request(cur, headers={"User-Agent": UA_BROWSER})
                r = op.open(req, timeout=5)
                got = (r.geturl(), r.read(8192))
                break
            except Exception:
                continue
        if got is None:
            return ""
        final, body = got
        if final and final.rstrip('/') != cur.rstrip('/'):
            return final                      # 30x 链已解析到落地页
        m = META_RE.search(body.decode('utf-8', 'ignore'))
        if not m:
            return ""                         # 200 且无可跟跳 → 死链/落地本身，不收
        nxt = m.group(1).strip().strip('"\'')
        cur = urllib.parse.urljoin(cur, nxt) if not nxt.startswith('http') else nxt
    return cur if cur.rstrip('/') != u.rstrip('/') else ""


def _junk(dom):
    """明显非资源站（图片/CDN/纯新闻/纯社媒）"""
    return any(k in dom for k in [
        'img.', 'cdn.', 'assets.', 'static.', 'badge', 'shields', '.s3.', 'cloudfront',
        'wp-content', 'googleusercontent', 'amazonaws.com', 'jsdelivr', 'unpkg', 'font',
    ])


def _hidden_startupinfo():
    """Windows 隐藏控制台窗口的标准组合（GitHub 上主流开源项目都是这套写法）。

    必须 `dwFlags |= STARTF_USESHOWWINDOW`，否则 wShowWindow 会被忽略 —— 这是最容易漏的一行。
    ⚠️ 每次调用都要**新建** STARTUPINFO：Python 3.7+ 会改掉传入对象的内部状态，
       复用同一个实例第二次就失效。
    """
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def _openers():
    """双通道：先走本机代理，失败自动回退直连（有的站走代理被限流，直连反而通）。"""
    p = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    return [urllib.request.build_opener(p, urllib.request.HTTPSHandler(context=CTX)),
            urllib.request.build_opener(urllib.request.HTTPSHandler(context=CTX))]


def _curl_get(url, timeout=6):
    """WAF/TLS 指纹兜底通道。**三级**，按成熟爬虫的做法（Scrapling 用浏览器 TLS 指纹
    绕过 JA3/JA4 识别，底层就是 curl_cffi 这类库）：
      ① curl_cffi + impersonate='chrome'：**模拟 Chrome 的 TLS ClientHello 指纹**
         （实测 beihangsoft：urllib 恒 403 → 系统 curl 200 但只有 12KB → curl_cffi 200 且 245KB）
      ② 系统 curl：进程级兜底
      ③ 无
    每级都先走代理再直连。"""
    try:                                       # ① curl_cffi（TLS 指纹伪装）
        from curl_cffi import requests as _cr
        for px in ({'http': PROXY, 'https': PROXY}, None):
            r = _cr.get(url, impersonate='chrome', proxies=px, timeout=timeout)
            if r.status_code < 400 and r.text:
                return r.text
    except Exception:
        pass
    for extra in (["-x", PROXY], []):           # ② 系统 curl
        try:
            # 🔴 隐藏窗口三件套（缺一不可，GitHub 上 1204 处实现都是这套）：
            #    curl 是控制台程序，父进程（pythonw）没有控制台时，Windows 会**新建
            #    一个控制台**来跑它 ⇒ 每抓一个站闪一个黑框。这是"高频弹窗"的真凶。
            #    · STARTUPINFO + STARTF_USESHOWWINDOW + SW_HIDE（真正的隐藏开关）
            #    · CREATE_NO_WINDOW（不分配控制台）
            #    · stdin=DEVNULL（不继承句柄，逼不出来控制台）
            r = subprocess.run(
                ["curl", "-s", "-L", "--compressed", "-A",
                 "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                 "--max-time", str(timeout)] + extra + [url],
                capture_output=True, timeout=timeout + 5,
                stdin=subprocess.DEVNULL,
                startupinfo=_hidden_startupinfo(),
                creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode == 0 and r.stdout:
                return r.stdout.decode("utf-8", "ignore")
        except Exception:
            continue
    return ""


def fetch(url, timeout=4):
    for op in _openers():
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 netforce-finder"})
            return op.open(req, timeout=timeout).read().decode("utf-8", "ignore")
        except Exception:
            continue
    h = _curl_get(url, timeout)           # urllib 全灭 → curl_cffi 伪装 → 系统 curl
    if h:
        return h
    return _browser_get(url)              # 第四级兜底：无头浏览器渲染（JS 站/人机校验）


# ---- 第四级兜底：无头浏览器（借鉴 Scrapling 的 StealthyFetcher / Scrapy 的渲染中间件）----
# 解决静态通道拿不到的两类站：① 纯 JS 渲染（标题在 DOM 里但 HTML 里没有）；
#                              ② 人机校验/指纹拦截（Cloudflare 这类）。
# ⚠️ 关键经验：**优先用系统自带浏览器**（C:\Program Files\Google\Chrome\…），
#   本机实测新下载的 Chrome for Testing 156 在这台机器上直接崩（STATUS_BREAKPOINT
#   0x80000003 + crashpad 管道报错），而系统自带 155 / Edge 149 一启动就好。
# Playwright 的 sync API **不是线程安全的**，而爬虫是多线程 ⇒ 用全局锁串行化。
BROWSER_EXE_CANDIDATES = [
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    r'C:\Users\21393\AppData\Local\ms-playwright\chromium-1228\chrome-win\chrome.exe',
    r'G:\_pw_chrome\chrome-win64\chrome.exe',
]

_BW = {'pw': None, 'browser': None, 'ctx': None, 'tried': False}
_BW_LOCK = threading.Lock()


def _browser_init():
    """懒加载无头浏览器（第一次用到才启动，约 1-2 秒；用完常驻，进程退出时自动关）。"""
    if _BW['tried']:
        return _BW['ctx']
    _BW['tried'] = True
    try:
        from playwright.sync_api import sync_playwright
        exe = next((p for p in BROWSER_EXE_CANDIDATES if os.path.exists(p)), None)
        if not exe:
            return None
        _BW['pw'] = sync_playwright().start()
        _BW['browser'] = _BW['pw'].chromium.launch(
            headless=True, executable_path=exe,
            # 后几个 flag 是专门压窗口的：Chrome 被强杀过一次后，下次启动会弹
            #「未正常关闭 / 恢复页面」的气泡窗口，无头模式也可能冒出来。
            args=['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
                  '--disable-blink-features=AutomationControlled',
                  '--disable-session-crashed-bubble', '--disable-infobars',
                  '--no-first-run', '--no-default-browser-check',
                  '--disable-restore-session-state'])
        _BW['ctx'] = _BW['browser'].new_context(
            user_agent=UA_BROWSER,
            proxy={'server': PROXY} if PROXY else None,
            viewport={'width': 1366, 'height': 900})
        print("  [browser] 无头浏览器已就绪: %s" % exe, flush=True)
    except Exception as e:
        print("  [browser] 无头浏览器不可用（%s），跳过第四级兜底" % str(e)[:80], flush=True)
        _BW['ctx'] = None
    return _BW['ctx']


def _browser_get(url, timeout=20):
    """用无头浏览器渲染后取 HTML。失败返回空串（调用方照旧降级，不会崩）。"""
    with _BW_LOCK:                       # Playwright sync API 非线程安全 ⇒ 全局串行
        ctx = _browser_init()
        if not ctx:
            return ""
        pg = None
        try:
            pg = ctx.new_page()
            pg.goto(url, timeout=timeout * 1000, wait_until='domcontentloaded')
            pg.wait_for_timeout(1800)    # 等 JS 把标题/DOM 填上（经验值，够静态站用）
            html = pg.content()
            return html if html else ""
        except Exception:
            return ""
        finally:
            try:
                if pg:
                    pg.close()
            except Exception:
                pass


def domain(u):
    try:
        d = urllib.parse.urlparse(u).netloc.lower().replace('www.', '')
        return d
    except Exception:
        return ''


# 站群型域名：同一域名下挂多个独立 wiki/子站（如 wiki.biligame.com 有 1,206 个子站）。
# 这类站不能按域名判重，要按「域名/路径首段」区分，否则收了第一个就再也进不来。
# 首段必须在 wiki_siblings.txt 白名单里——否则 staticwiki/patchwiki/resources/tools
# 这类基础设施路径会冒充成"新站点"（实测裸放行 1,217 条，一半是噪声）。
SIBLING_DOMS = {'wiki.biligame.com'}
_SIBLING = {}


def _sibling_prefixes():
    if 'p' not in _SIBLING:
        try:
            f = Path(__file__).parent / 'wiki_siblings.txt'
            _SIBLING['p'] = {x.strip().lower()
                             for x in f.read_text(encoding='utf-8').splitlines() if x.strip()}
        except Exception:
            _SIBLING['p'] = set()
    return _SIBLING['p']


def site_key(u):
    """站点指纹：普通站=域名；站群型=域名/路径首段（仅限白名单子站）。"""
    d = domain(u)
    if d in SIBLING_DOMS:
        seg = urllib.parse.urlparse(u).path.strip('/').split('/')
        first = (seg[0] if seg and seg[0] else '').lower()
        if first in _sibling_prefixes():
            return d + '/' + first
    return d


import urllib.parse


def reachable(u):
    # 两段式：先 HEAD（省流量），失败再 GET 兜底——有的站拒绝 HEAD（beihangsoft 实测 591）
    for op in _openers():
        for method in ("HEAD", "GET"):
            try:
                req = urllib.request.Request(u, method=method,
                                             headers={"User-Agent": "Mozilla/5.0"})
                code = op.open(req, timeout=2.5).getcode()
                if code and code < 400:
                    return True
            except Exception:
                continue
    return bool(_curl_get(u, 8))             # urllib 被 TLS 指纹拦 → curl 兜底


def classify(u, dom):
    if any(k in dom for k in ['arxiv', 'biorxiv', 'ssrn', 'preprint', 'papers']):
        return 'paper'
    if any(k in dom for k in ['dataset', 'kaggle', 'huggingface', 'openml', 'uci.', 'data.gov']):
        return 'dataset'
    if 'github.com' in dom or 'gitlab' in dom or 'sourceforge' in dom:
        return 'tool'
    if 'awesome' in dom or dom.endswith('wiki') or 'fmhy' in dom:
        return 'directory'
    if any(k in dom for k in ['coursera', 'udacity', 'fast.ai', 'academy', 'kadenze',
                              'scaler', '.edu', 'course', 'school', 'mlss', 'tutorial']):
        return 'education'
    if any(k in dom for k in ['blog', 'wordpress', 'github.io', 'dev.to', 'hackernoon',
                              'blogspot', '.me']):
        return 'blog'
    if any(k in dom for k in ['podcast', 'talkingmachines', 'lineardigressions', 'dataskeptic',
                              'partiallyderivative', 'datastori', 'ocdevel', '.fm']):
        return 'podcast'
    if any(k in dom for k in ['virtualcard', 'virtual-card', 'crypto-card', 'cryptocard',
                              'redotpay', 'dupay', 'byswe', 'noones', 'paxful',
                              'wirex', 'utorg', 'coinspaid', 'cryptopay', 'paysafe']):
        return 'vcard'
    if any(k in dom for k in ['crypto', 'blockchain', 'ethereum', 'bitcoin', 'wallet',
                              'web3', 'defi', 'chain', 'token', 'solana', 'tron', 'bsc',
                              'opensea', 'airdrop', 'metamask', 'coinbase', 'binance',
                              'onchain', 'node', 'zk']):
        return 'crypto'
    if any(k in dom for k in ['game', 'repack', 'rom', 'emulator', 'torrent']):
        return 'game'
    if any(k in dom for k in ['modelscope', 'civitai', 'huggingface.co/models',
                              'modelzoo', 'hf-mirror', 'liblib', 'wan21', 'tripo']):
        return 'model'
    if any(k in dom for k in ['competition', 'codingame', 'tournament', 'contest']):
        return 'competition'
    if any(k in dom for k in ['forum', 'community', 'hackr', 'flipboard', 'speakerdeck',
                              'datasciencecentral', 'news']):
        return 'community'
    if any(k in dom for k in ['deeplearning4j', 'tensorflow', 'pytorch', 'openai', 'framework']):
        return 'framework'
    return 'site'


def main():
    collected = []
    seen_dom = set()
    out = os.path.join(_HERE_DIR, 'resources.json')
    # 断点续扫：启动前加载已有记录，避免重启后重复收集相同域名
    try:
        prev = json.load(open(out, encoding='utf-8'))
        for x in prev:
            seen_dom.add(x['domain'])
        collected = list(prev)
    except Exception:
        pass
    queue = list(SEEDS)
    visited = load_visited(seen_dom)      # 断点续爬：重启不重爬（Scrapy JOBDIR 等价）
    lock = threading.Lock()               # 并发下保护 queue/visited/collected/落盘
    dom_locks = {}                     # 单域名限流（防把单站打成 WAF 黑名单）
    dirty = {'t': time.time(), 'n': 0}  # 脏标记：定时/定量批量落盘，避免 O(N²) 写盘

    def flush_now():
        tmp = out + '.tmp'
        with lock:
            json.dump(collected, open(tmp, 'w', encoding='utf-8'),
                      ensure_ascii=False, indent=2)
            os.replace(tmp, out)       # 原子替换，防中断留半截文件
            dirty['t'], dirty['n'] = time.time(), 0

    def maybe_flush(_n=None):
        # 每 20 条或每 5 秒落一次盘（崩溃最多丢几秒内的数据，换来数量级的 IO 收益）
        with lock:
            due = dirty['n'] >= 20 or (time.time() - dirty['t']) >= 5
        if due:
            flush_now()
    now = datetime.datetime.now()
    deadline = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if deadline < now:
        deadline += datetime.timedelta(days=1)
    print("发现器启动（无上限），deadline=%s，已续扫 %d 个，去重指纹 %d 条"
          % (deadline.strftime("%Y-%m-%d %H:%M"), len(collected), len(visited)))

    qi, pg, pi = 0, 1, 0
    visited_lock = set()

    # —— 🔴 进化模块（2026-10-18 加）：外链矿 + 关键词自举 ——
    # 为什么加它（实测数据）：近 60 轮引擎返回 1547 条，**去重后只剩 437 条新的**
    # （重复率 71.8%）⇒ 瓶颈是"查询空间穷尽"，不是带宽也不是并发。
    # 两个新泵都从**已收录的语料**里长出新查询，让供给随规模增长：
    #   link = 挖已收录站点的外链（资源站互链是同质网络，信噪比高于搜索结果）
    #   boot = 从已收录站 title 抽高频短语造新词再搜（中文走"分类名×后缀"模板）
    # 它们不是替掉老泵，而是**加进同一个加权调度**：好用自然多跑，不好用自动饿死。
    import evolve

    def _evo_log(m, **_):
        # evolve 内部统一用 log(msg, flush=True) 调用；这里吞掉 flush 关键字，
        # 否则 bootstrap_keywords 等会抛 TypeError 把整个爬虫带崩。
        print(m, flush=True)

    def link_pump(_arg=None):
        return evolve.harvest_outlinks(seen_doms=seen_dom, n_sites=10,
                                       log=_evo_log)

    def boot_pump(_arg=None):
        return evolve.bootstrap_search(log=_evo_log)

    LOCAL_PUMPS = PUMPS + [("link", link_pump, ["-"]),
                           ("boot", boot_pump, ["-"])]

    # —— 泵的自适应调度（借鉴 Scrapy 调度器/AutoThrottle 的自适应思想）——
    # 死板轮换会让零产出的源一直占着轮次（本轮实测 13/15 泵产出 0）。
    # 做法：按"历史命中率"给每个源算权重 → 加权随机抽（保留 0.35 底权，冷门源不被饿死）。
    PUMP_STATS = {p[0]: {'rounds': 0, 'hits': 0, 'noise': 0} for p in LOCAL_PUMPS}

    def pump_weight(nm):
        s = PUMP_STATS[nm]
        if s['rounds'] < 2:                 # 样本不足时给中等权重，先观察
            return 1.0
        rate = s['hits'] / max(1, s['rounds'])
        return 0.35 + 3.0 * min(1.0, rate * 5)   # 0 有效产出 → 0.35（低但不死）；高产 → 上限

    def pick_pump():
        import random
        ws = [pump_weight(p[0]) for p in LOCAL_PUMPS]
        pick = random.choices(range(len(LOCAL_PUMPS)), weights=ws, k=1)[0]
        return LOCAL_PUMPS[pick]

    def note_pump(nm, hits, noise=0):
        """记分：**只有有效产出计hits**，噪声只记noise**不加分**（Scrapy 调度器按 yield 计分）。"""
        s = PUMP_STATS[nm]
        s['rounds'] += 1
        s['hits'] += hits
        s['noise'] += noise

    def pump_note(nm):
        s = PUMP_STATS[nm]
        return "｜累计 %d 轮 / 有效 %d / 噪声 %d" % (s['rounds'], s['hits'], s['noise'])

    # —— 自适应并发（借鉴 Scrapy AutoThrottle 的核心洞察）——
    # Scrapy 的 AutoThrottle 不是"固定延迟+硬并发"，而是按**实测响应延迟**调延迟：
    #   目标延迟 = latency / 目标并发；下一个延迟 = 上次延迟与目标延迟的均值；
    #   **非 200 响应只允许把延迟调大、不允许调小**（错误多说明我们打太狠了，要减速）。
    # 我们没有 per-domain slot，就落成全局自适应：有效率低 → 降并发；有效率高 → 升并发。
    workers = {'n': 16}   # 初始并发下调（原 32）：机器内存吃紧

    def tune_workers(hits, noise, tried):
        if tried < 5:
            return workers['n']
        eff = (hits - noise) / max(1, tried)          # 有效产出率
        if eff < 0.05:
            workers['n'] = max(8, workers['n'] // 2)  # 打不动就减速，别硬撞
        elif eff > 0.25:
            # ⚠️ 上限从 64 降到 24（2026-10-18）：用户实测机器被拖卡（内存 12.5/15.4 GB）。
            # 并发 48-64 时每个线程各持一个 socket + HTML 缓冲，内存和 CPU 都吃紧，
            # 而产出并没有等比增长（瓶颈是 URL 供给不是并发）⇒ 24 是本机甜点值。
            workers['n'] = min(24, int(workers['n'] * 1.5))   # 好使就加速
        return workers['n']

    def consider(u, via):
        """评估一个 URL：是目录页就入队再爬，是资源站就验证可达后入册。
        并发安全：先按规范化指纹查重（Scrapy dupefilter 思路），所有共享状态改动都在锁内。"""
        u = u.rstrip('.,);]}>"\'`')
        key = canon(u)
        # SeenStore.add 原子判重（SQLite 主键），返回 False=已见过，直接跳过
        if not visited.add(key):
            return False
        dom = domain(u)
        # 跳板（DOI 等）→ 先解析到真实落地页，拿出版社/机构域名
        if dom in REDIRECTORS:
            real = _resolve(u)
            if not real:
                return False
            u, dom = real, domain(real)
        # 短链（t.co/bit.ly/dwz.cn…）→ 跟跳到真实落地页再判定（工具箱常带短链）
        elif dom in SHORTENERS:
            real = _follow_short(u)
            if not real:
                return False
            u, dom = real, domain(real)
            if dom in SHORTENERS:           # 仍落在短链（死链/循环）→ 不收
                return False
        if not dom or dom in SKIP or _junk(dom):
            return False
        # github awesome 仓库页 → 转 raw README 当目录页再爬一层
        if ('github.com' in dom and '/awesome' in u.lower()
                and '/blob/' not in u):
            raw = (u.replace('github.com', 'raw.githubusercontent.com')
                     .replace('/tree/', '/').rstrip('/') + '/README.md')
            with lock:
                rk = canon(raw)
                if visited.add(rk):
                    queue.append(raw)
            return False
        skey = site_key(u) or dom       # 站群型按「域名/路径首段」区分，普通站仍是域名
        with lock:
            if skey in seen_dom:
                return False
            seen_dom.add(skey)
        # 单域名限速（借鉴 Scrapy CONCURRENT_REQUESTS_PER_DOMAIN=1）：同一站点并发打太多
        # 会把自己打进对方 WAF黑名单（我们已有 beihangsoft 被 403 的先例）⇒ 同一域名串行。
        dl = dom_locks.get(dom)
        if dl is None:
            with lock:
                dl = dom_locks.setdefault(dom, threading.Lock())
        with dl:
            if not reachable(u):
                return False
        # 落盘也放锁内：并发写同一个 json 会写坏文件。
        # ⚠️ 别每条都全量重写（2800 条 ≈700KB，收 N 条就是 O(N²) 的 IO —— 实测这是
        #    并发版的真实瓶颈之一）。改成"脏标记 + 定时/定量批量落盘"，仍保证不丢数据。
        with lock:
            collected.append({'url': u, 'domain': dom,
                              'type': classify(u, dom), 'found_via': via,
                              'noise': bool(NOISE_RE.search(dom))})
            n = len(collected)
            dirty['n'] += 1
        print('[%d] %s  via %s' % (n, dom, via), flush=True)
        maybe_flush(n)
        return True

    # 种子站点自身注册入库：主循环只 consider 页面里的链接，种子页面本身
    # 从不走 consider ⇒ 应用库这类"种子即资源"的站永远进不了清单，这里补上
    for _su in REGISTER_SEEDS:
        try:
            consider(_su, "seed:self")
        except Exception:
            pass

    # —— 🔴 无限爬取（用户 23:19："爬取无限，不要说因为满了就不进行爬了"）——
    #   原逻辑到早6:00 就退出；现在到点**自动续期到次日**，永远不停。
    #   每轮开始打点播报总入册数 + 队列深度 + 已运行时长，便于随时判断"它还在动没"。
    started = time.time()
    cycles = 0
    while True:
        if datetime.datetime.now() >= deadline:
            cycles += 1
            deadline += datetime.timedelta(days=1)
            flush_now()
            save_visited(visited)
            print("[cycle #%d] 已连续运行 %.1f 小时，累计入册 %d，去重指纹 %d；"
                  "已自动续期至 %s 继续爬（无限模式）"
                  % (cycles, (time.time() - started) / 3600, len(collected),
                     len(visited), deadline.strftime('%Y-%m-%d %H:%M')), flush=True)
        # —— 种子泵 vs 队列收割：这是**两相轮转**，不是"满了就停"。
        #   队列 ≥ 阈值时只是**暂停注入新种子**（避免无限堆积占内存），
        #   队列分支照常爬目录页、抽外链；队列回落到阈值以下，泵立刻恢复注入。
        #   实测：队列 384 时仍持续入册（[4080]…[4087]），爬取从未停过。
        if len(queue) < QUEUE_INJECT_MAX:
            # 🔴 泵选择= 加权轮转（借鉴 Scrapy 调度器的自适应思想）：命中率低的源自动降权，
            #    但保留最低出现概率（防止把冷门但独活的源饿死）。这是"命中率优化"的自动化手段。
            name, fn, pool = pick_pump()
            if fn is gh_pump:
                # 🔴 GitHub 类泵（github / gh_agi / gh_skill / zh_robot …）的产出是
                #   "raw.githubusercontent.com/.../README.md" 这类**目录页**，而 raw 域名在
                #   SKIP 黑名单里 ⇒ 走 consider() 会被整批丢弃（实测每轮 200 条命中 → 0 产出）。
                #   正确姿势：直接注入队列当种子，等队列分支去爬它、顺藤摸出里面的仓库站。
                if name == 'github':
                    q = GH_QUERIES[qi % len(GH_QUERIES)]
                    got = fn(q, pg)
                    pg += 1
                    if pg > 10:                   # GitHub 搜索最多 1000 条 = 10 页
                        pg, qi = 1, qi + 1
                    label = "%s p%d" % (q, pg - 1)
                else:
                    q = pool[qi % len(pool)]
                    qi += 1
                    got = fn(q, 1)
                    label = str(q)[:40]
                fresh_seed = []
                with lock:
                    for u in got:
                        if u not in fresh_seed and visited.add(canon(u)):
                            queue.append(u)
                            fresh_seed.append(u)
                if fresh_seed:
                    print("[pump:%s] 「%s」→ 注入 %d 个目录页种子｜队列 %d"
                          % (name, label, len(fresh_seed), len(queue)), flush=True)
                note_pump(name, len(fresh_seed))
                pi += 1
                if not fresh_seed:
                    time.sleep(1)
                continue
            else:
                arg = pool[qi % len(pool)]
                qi += 1
                got = fn(arg)
                links = (got or [])[:200]
                q0 = len(queue)          # 队列深度快照：目录页型产出的价值在这里
                # 预过滤：规范化指纹已在 visited 里的不必进线程（Scrapy dupefilter 同理）
                fresh = [u for u in links if canon(u) not in visited]
                tname = name + ':' + str(arg)[:40]
                if fresh:
                    w = tune_workers(0, 0, 0)   # 先按上一轮调好的并发跑这轮
                    with ThreadPoolExecutor(max_workers=w) as ex:
                        res = list(ex.map(lambda u: consider(u, tname), fresh))
                    n = sum(1 for r in res if r)
                    noise = sum(1 for x in collected[-200:]
                                if x.get('found_via') == tname and x.get('noise'))
                    w = tune_workers(n, noise, len(fresh))
                else:
                    n, noise = 0, 0
                # ⚠️ 计分修正（借鉴 Scrapy 的 item pipeline 思路：**不同产物类型分开算**）：
                #   gh_* 泵的产出是"往队列注入目录页种子"，不是直接入册的资源站。
                #   早期只按"入册数"计分 → 这些泵永远0 分 → 自适应把并发压到 8，
                #   等于掐掉最有价值的种子来源。现在把新增队列深度也计入有效产出。
                qadded = max(0, len(queue) - q0)
                save_visited(visited)          # 每轮落盘去重指纹 → 重启不重爬
                flush_now()                    # 每轮结束强制落盘（防中断丢数据）
                note_pump(name, max(0, n - noise) + qadded, noise)
                print("[pump:%s] 关键词「%s」→ 命中 %d 条（去重后 %d 待查），入册 %d（噪声 %d）"
                      "注入种子 %d｜队列 %d｜并发 %d%s"
                      % (name, str(arg)[:40], len(got or []), len(fresh), n, noise,
                         qadded, len(queue), workers['n'], pump_note(name)), flush=True)
                pi += 1
                if not got:
                    time.sleep(1)
                continue
        with lock:
            url = queue.pop(0) if queue else None
        if not url:
            time.sleep(1)
            continue
        with lock:
            k = canon(url)
            if not visited.add(k):
                continue
        html = fetch(url)
        if not html:
            continue
        links = URL_RE.findall(html)
        # 目录页里的链接同样并发评估（Scrapy CONCURRENT_REQUESTS 的简化版：12 并发）
        tname = 'queue:' + domain(url)
        with ThreadPoolExecutor(max_workers=12) as ex:   # 目录页链接评估（原 32，减轻卡顿）
            list(ex.map(lambda u: consider(u, tname), links))

    save_visited(visited)
    flush_now()
    print('DONE: 收集 %d 个资源站（去重指纹 %d） → %s'
          % (len(collected), len(visited), out))




# ============================================================================
# 语料模式（corpus）—— 角色语音语料爬取
# 原为独立的 corpus_crawler.py，2026-10-20 按用户要求并回本文件：
#   「两个爬虫可以合并为一个，不用非要建两个」。
# 与发现模式共享：deadline 时间控制、日志风格、断点续跑思想、自适应限速。
# 差异（不得不分开的）：本模式的 fetch 必须【直连】，国内 wiki 站套代理必挂；
#   抽取目标是页面正文台词表，不是页面链接。
# 用法：python resource_finder.py corpus blhx --until 06:00
# ============================================================================

import random
import subprocess
import sys
from datetime import datetime as _DT, timedelta as _TD
from pathlib import Path

HERE = Path(__file__).parent
CORPUS_DIR = Path(os.environ.get("NETFORCE_DATA", "G:/_train_corpus_raw"))
CORPUS_STATE = CORPUS_DIR / "crawler_state.json"
CORPUS_LOG = CORPUS_DIR / "crawler.corpus_log"

CORPUS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

CORPUS_MIN_BYTES = 100_000      # 低于此体积视为风控降级页


class CorpThrottle:
    """AutoThrottle：按实测耗时调延迟，风控时只准调慢不准调快。"""

    def __init__(self, start=6.0, floor=4.0, ceil=45.0, target=1.0):
        self.wait = start
        self.floor = floor
        self.ceil = ceil
        self.target = target          # 目标并发（这里 1 = 串行）
        self.ok = 0
        self.blocked = 0
        self.fails = 0

    def note_ok(self, latency):
        target = max(self.floor, latency / self.target)
        self.wait = (self.wait + target) / 2      # 向目标均值收敛（Scrapy 原式）
        self.wait = max(self.floor, min(self.ceil, self.wait))
        self.ok += 1

    def note_block(self):
        # 只允许调大：被拦说明打太狠
        self.wait = min(self.ceil, self.wait * 1.8)
        self.blocked += 1

    def note_fail(self):
        self.wait = min(self.ceil, self.wait * 1.5)
        self.fails += 1

    def sleep(self):
        time.sleep(random.uniform(self.wait * 0.8, self.wait * 1.25))

    def report(self):
        total = self.ok + self.blocked + self.fails
        rate = self.ok / max(1, total)
        return (f"成功 {self.ok} / 风控 {self.blocked} / 失败 {self.fails} "
                f"（有效率 {rate:.0%}），当前延迟 {self.wait:.1f}s")


# 各 Wiki 入口种子。⚠️ 高价值角色单独列出，先爬
CORPUS_WIKIS = {
    "blhx": {
        "name": "碧蓝航线",
        "base": "https://wiki.biligame.com/blhx/",
        # 用户点名/语料里已有的角色底色，优先
        "vip": ["天狼星", "柴郡", "贝尔法斯特", "谢菲尔德", "黛朵", "赫敏", "逸仙",
                "拉菲", "企业", "大凤", "赤城", "加贺", "光辉", "欧根亲王",
                "高雄", "爱宕", "明石", "山城", "扶桑", "Z23", "独角兽"],
        "seeds": ["天狼星"],
    },
    "bh3": {
        "name": "崩坏3",
        "base": "https://wiki.biligame.com/bh3/",
        "vip": ["琪亚娜·卡斯兰娜", "布洛妮娅·扎伊切克", "雷电芽衣", "德丽莎·阿波卡利斯"],
        "seeds": ["琪亚娜·卡斯兰娜"],
    },
    "ys": {
        "name": "原神",
        "base": "https://wiki.biligame.com/ys/",
        "vip": ["诺艾尔", "甘雨", "胡桃", "神里绫华", "宵宫", "八重神子", "雷电将军"],
        "seeds": ["诺艾尔"],
    },
}

# 触发条件 data-key → 用户可能的说法（把台词变成可训练问答的关键）
CORPUS_KEY2USER = {
    "login": ["我回来了", "在吗", "我上线了"],
    "main": ["我在待着，没干什么", "陪我一会儿", "你在干嘛"],
    "home": ["我在待着，没干什么", "陪我一会儿"],
    "detail": ["让我看看你", "今天状态怎么样"],
    "touch": ["我拍拍你", "碰到你了", "过来一下"],
    "headpat": ["摸摸你的头", "乖"],
    "special": ["我摸你一下"],
    "couple_encourage": ["一起上吧", "给我打气"],
    "expedition": ["委托回来了吗"],
    "mission": ["有什么要做的吗", "今天要干什么"],
    "mission_complete": ["任务做完了"],
    "mail": ["有我的信吗"],
    "love": ["我回来了，想我了吗"],
    "gift": ["给你个东西", "给你的礼物"],
    "battle": ["要出发了"],
    "win": ["赢了"],
    "lose": ["输了"],
    "skill": ["用技能"],
    "hp_warning": ["我快撑不住了"],
    "feeling1": ["我们才刚认识"],
    "feeling2": ["最近怎么样"],
    "feeling3": ["今天过得还行"],
    "feeling4": ["有你在挺好的"],
    "feeling5": ["我好像有点喜欢你"],
    "propose": ["我想跟你过一辈子"],
    "profile": ["介绍一下你自己"],
    "unlock": ["你来了"],
    "drop_descrip": ["你是什么人"],
}

# 只留这些 key —— 情人节/皮肤描述这类特定场景先不要，噪声太高
CORPUS_KEEP_KEYS = {
    "login", "main", "home", "detail", "touch", "headpat", "special",
    "couple_encourage", "expedition", "mission", "mission_complete", "mail",
    "love", "gift", "battle", "win", "lose", "skill", "hp_warning",
    "feeling1", "feeling2", "feeling3", "feeling4", "feeling5",
    "propose", "profile", "unlock", "drop_descrip", "backyard",
}

CORPUS_JP = ("ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすずせぜそぞただちぢっつづてでとど"
      "なにぬねのはばぱひびぴふぶぷへべぺほぼぽまみむめもゃやゅゆょよらりるれろゎわ"
      "ゐゑをんゔァアィイゥウェエォオカガキギクグケゲコゴサザシジスズセゼソゾタダチヂッ"
      "ツヅテデトドナニヌネノハバパヒビピフブプヘベペホボポマミムメモャヤュユョヨラリルレロヮワヲンヴ")


def corpus_log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(CORPUS_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def corpus_fetch(url, throttle, referer=None, tries=3):
    """curl 抓页面。返回 (html, ok)。风控降级页判为失败并触发降速。"""
    for i in range(tries):
        t0 = time.time()
        cmd = [
            "curl", "-s", "--noproxy", "*", "--max-time", "45",
            "-A", CORPUS_UA,
            "-H", f"Referer: {referer or 'https://wiki.biligame.com/'}",
            "-H", "Accept-Language: zh-CN,zh;q=0.9",
            "-w", "\n__META__%{http_code}|%{size_download}",
            url,
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=60)
            raw = p.stdout
            if b"__META__" not in raw:
                throttle.note_fail()
                corpus_log(f"    无 meta，重试 {i+1}/{tries}")
                continue
            body, meta = raw.rsplit(b"__META__", 1)
            code, size = meta.decode().split("|")
            code, size = int(code), int(size)
            if code == 200 and size >= CORPUS_MIN_BYTES:
                throttle.note_ok(time.time() - t0)
                return body.decode("utf-8", "replace"), True
            throttle.note_block()
            corpus_log(f"    风控 code={code} size={size}，退避 → 延迟 {throttle.wait:.1f}s")
            throttle.sleep()
        except Exception as e:
            throttle.note_fail()
            corpus_log(f"    异常 {type(e).__name__} {str(e)[:60]}")
            throttle.sleep()
    return "", False


def corpus_clean(tx):
    """单句清洗：去日文、去符号、去过长/过短。"""
    if not tx or len(tx) < 2:
        return None
    # 日文假名占比超 10% 直接丢（贝尔法斯特的 profile 是全日文）
    jp = sum(1 for c in tx if c in CORPUS_JP)
    if jp / max(1, len(tx)) > 0.10:
        return None
    # 去表情符号/波浪号/装饰字符
    for ch in "♡♥～~★☆♪•·「」『』【】…":
        tx = tx.replace(ch, "" if ch in "♡♥～~★☆♪•·" else ch)
    tx = tx.replace("…", "。")
    tx = tx.strip(" 。，、！？,.!?")
    if len(tx) < 2 or len(tx) > 200:
        return None
    return tx


def corpus_parse_lines(html, char):
    """抽出 [(data_key, 触发条件中文, 台词)]。"""
    from bs4 import BeautifulSoup
    s = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for t in s.find_all("table", class_="table-ShipWordsTable"):
        for tr in t.find_all("tr"):
            key = tr.get("data-key") or ""
            if key not in CORPUS_KEEP_KEYS:
                continue
            th = tr.find("th")
            label = th.get_text(strip=True) if th else ""
            for p in tr.find_all("p", class_="ship_word_line"):
                tx = corpus_clean(p.get_text("", strip=True))
                if not tx or tx in seen:
                    continue
                # 自我介绍里大量出现角色队名，去掉含"号舰""舷号"这类硬设定句
                if "舷号" in tx or "号舰" in tx:
                    continue
                seen.add(tx)
                out.append((key, label, tx))
    return out


def corpus_parse_links(html, prefix):
    """从导航表关联出同站其他角色名。"""
    from bs4 import BeautifulSoup
    s = BeautifulSoup(html, "lxml")
    names = set()
    for t in s.find_all("table", class_="table-ShowShipClassNavPannel"):
        for a in t.find_all("a", href=True):
            h = a["href"]
            if not h.startswith(prefix):
                continue
            tail = urllib.parse.unquote(h[len(prefix):])
            if not tail or any(c in tail for c in ":#?"):
                continue
            if len(tail) > 24:
                continue
            names.add(tail)
    return names


def corpus_load_state():
    if CORPUS_STATE.exists():
        try:
            return json.loads(CORPUS_STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done": {}, "queue": {}}


def corpus_save_state(st):
    CORPUS_STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def corpus_crawl(tag, limit=None, until=None, max_minutes=None, follow=True):
    cfg = CORPUS_WIKIS[tag]
    base, prefix = cfg["base"], "/" + tag + "/"
    st = corpus_load_state()
    done = st.setdefault("done", {}).setdefault(tag, {})
    q = st.setdefault("queue", {}).setdefault(tag, [])

    # 队列编排：VIP 优先 → 已有队列 → 关联发现
    vip = [n for n in cfg.get("vip", []) if n not in done]
    for n in reversed(vip):
        if n in q:
            q.remove(n)
        q.insert(0, n)
    if not q:
        q.extend(cfg["seeds"])

    outfile = CORPUS_DIR / f"voice_{tag}.jsonl"
    throttle = CorpThrottle()
    t_start = time.time()
    deadline = None
    if until:
        hh, mm = until.split(":")
        deadline = _DT.now().replace(hour=int(hh), minute=int(mm), second=0)
        if deadline <= _DT.now():
            deadline += _TD(days=1)
    if max_minutes:
        d2 = _DT.now() + _TD(minutes=max_minutes)
        deadline = min(deadline, d2) if deadline else d2
    corpus_log(f"[{tag}] 启动：队列 {len(q)}，已done {len(done)}，"
        f"截止 {deadline.strftime('%H:%M') if deadline else '无'}")

    n_char = 0
    while q:
        if deadline and _DT.now() >= deadline:
            corpus_log(f"[{tag}] 到点收工（{throttle.report()}）")
            break
        name = q.pop(0)
        if name in done:
            continue
        if limit and n_char >= limit:
            corpus_log(f"[{tag}] 达到上限 {limit}")
            break
        url = base + urllib.parse.quote(name)
        corpus_log(f"[{tag}] 抓 {name}（队列 {len(q)}）")
        html, ok = corpus_fetch(url, throttle, referer=base)
        if not ok:
            done[name] = -1
            corpus_save_state(st)
            throttle.sleep()
            continue

        lines = corpus_parse_lines(html, name)
        if lines:
            with open(outfile, "a", encoding="utf-8") as f:
                f.write(json.dumps({"char": name, "wiki": tag, "lines": lines},
                                   ensure_ascii=False) + "\n")
            n_char += 1
            corpus_log(f"    ✓ {len(lines)} 句")
        else:
            corpus_log("    · 无台词表（模板不同/非角色页）")
        done[name] = len(lines)
        corpus_save_state(st)

        if follow:
            new = corpus_parse_links(html, prefix) - set(done) - set(q)
            if new:
                # VIP 插到队首
                add = sorted(new)
                for n in reversed([x for x in add if x in cfg.get("vip", [])]):
                    add.remove(n); add.insert(0, n)
                q[:0] = add
                corpus_log(f"    → 关联新增 {len(new)}，队列 {len(q)}")
        throttle.sleep()

    corpus_save_state(st)
    corpus_log(f"[{tag}] 结束：{n_char} 角色 → {outfile} | {throttle.report()}")
    return outfile

# ============================================================================
# 扩散模式（diffuse）—— 二次元站群定向扩散
# 原为独立的 anime_diffuse.py，2026-10-21 按用户要求合并回本文件：
#   「跑完这一轮和主爬虫合并一下，相当于主爬虫也有了爬取动漫的能力」。
#   与 2026-10-20 把 corpus_crawler.py 并回来是同一个做法：一个文件、多个 mode。
# 与发现模式的差异（两处，别混）：
#   ① 取页必须【真直连 + Referer + Accept-Language】：实测走代理 567、裸直连也 567，
#      只有带 Referer 才 200（首页 1.41MB），且子站根路径 301 → /<sub>/首页，必须 -L。
#   ② 目标不是"找没见过的域名"，而是"把一个已知站群的子站逐个盘活 + 抽出目录页"。
# 用法：python resource_finder.py diffuse [--limit N]
# ============================================================================

ANIME_BASE = "https://wiki.biligame.com/"
ANIME_SIB_FILE = HERE / "wiki_siblings.txt"
ANIME_SHELL = 20_000            # 567 空壳 7.3KB；真首页一般 100KB+ ⇒ 20KB 是安全线
ANIME_DELAY = (2.5, 4.0)        # 站点常被别的爬虫同时打，这里必须是慢速客
ANIME_LOG = CORPUS_DIR / "anime_diffuse.log"
ANIME_STATE = CORPUS_DIR / "anime_diffuse_state.json"
ANIME_OUT = CORPUS_DIR / "anime_sites.json"

# 目录页分桶关键词（中文优先，二次元 wiki 的栏目名基本就这几个）
ANIME_BUCKETS = {
    "角色": ["角色", "人物", "干员", "舰娘", "从者", "英灵", "character", "角色图鉴"],
    "语音": ["语音", "台词", "配音", "voice"],
    "剧情": ["剧情", "故事", "章节", "主线", "活动", "story", "episode"],
    "档案": ["档案", "资料", "图鉴", "数据", "属性"],
}


def anime_log(msg):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg)
    try:
        print(line, flush=True)
        with open(ANIME_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def anime_get(url, timeout=45):
    """站群直连取页 → (html, size, final_url)；风控/失败返回 (None, size, fin)。"""
    try:
        p = subprocess.run(
            ["curl", "-sL", "--noproxy", "*", "--max-time", str(timeout),
             "-A", UA_BROWSER,
             "-H", "Referer: " + ANIME_BASE,
             "-H", "Accept-Language: zh-CN,zh;q=0.9",
             "-w", "\n__U__%{url_effective}", url],
            capture_output=True, timeout=timeout + 15,
            stdin=subprocess.DEVNULL,
            startupinfo=_hidden_startupinfo(),
            creationflags=subprocess.CREATE_NO_WINDOW)
        body = p.stdout
    except Exception as e:
        anime_log("  异常 %s %s" % (type(e).__name__, str(e)[:60]))
        return None, 0, ""
    fin = ""
    if b"__U__" in body:
        body, meta = body.rsplit(b"\n__U__", 1)
        fin = meta.decode("utf-8", "replace").strip()
    if len(body) < ANIME_SHELL:
        return None, len(body), fin
    return body.decode("utf-8", "replace"), len(body), fin


def anime_site_name(html, sub):
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    if not m:
        return sub
    t = re.sub(r"\s+", " ", m.group(1)).strip()
    return (t.replace("_BWIKI_哔哩哔哩", "")
             .replace("WIKI_BWIKI_哔哩哔哩", "").strip() or sub)


def anime_harvest(html, sub):
    """从子站首页抽同子站目录页，按 ANIME_BUCKETS 分桶。只看 /<sub>/ 前缀。"""
    links, seen = {k: [] for k in ANIME_BUCKETS}, set()
    for u in re.findall(r'<a[^>]+href="([^"]+)"', html):
        if not u or u.startswith(("#", "javascript:", "mailto:")):
            continue
        u = urllib.parse.urljoin(ANIME_BASE + sub + "/", u)
        if not u.startswith(ANIME_BASE + sub + "/"):
            continue
        path = urllib.parse.unquote(urllib.parse.urlparse(u).path)
        if re.search(r"\.(png|jpg|jpeg|gif|svg|css|js|ico)$", path, re.I):
            continue
        if u in seen:
            continue
        seen.add(u)
        low = path.lower()
        for name, kws in ANIME_BUCKETS.items():
            if any(k.lower() in low for k in kws):
                links[name].append(u)
                break
    return {k: v[:40] for k, v in links.items() if v}


def diffuse_crawl(limit=None):
    """站群盘点：逐个抓子站首页 → 判活 → 抽站名 + 四桶目录页。可续跑。"""
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    if not ANIME_SIB_FILE.exists():
        anime_log("缺 %s：先放一份子站代号清单（一行一个，如 ys / blhx）再跑" % ANIME_SIB_FILE)
        return None
    subs = [x.strip() for x in ANIME_SIB_FILE.read_text(encoding="utf-8").splitlines()
            if x.strip()]
    st = {"done": {}, "blocked": 0}
    if ANIME_STATE.exists():
        try:
            st = json.loads(ANIME_STATE.read_text(encoding="utf-8"))
        except Exception:
            pass

    def _save():
        tmp = ANIME_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        tmp.replace(ANIME_STATE)

    todo = [s for s in subs if s not in st["done"]]
    if limit:
        todo = todo[:limit]
    anime_log("站群盘点开始：白名单 %d 个子站，已完成 %d，本轮待抓 %d"
              % (len(subs), len(st["done"]), len(todo)))
    throttle = random.uniform(*ANIME_DELAY)
    for i, sub in enumerate(todo, 1):
        html, size, fin = anime_get(ANIME_BASE + sub + "/")
        if not html:
            st["blocked"] += 1
            throttle = min(60.0, throttle * 1.8)
            st["done"][sub] = {"status": "blocked", "size": size}
            anime_log("(%d/%d) ✗ %s 风控/失败 size=%d，退避 → %.1fs"
                      % (i, len(todo), sub, size, throttle))
        elif fin.startswith(ANIME_BASE + "wiki/") or fin.rstrip("/") == ANIME_BASE.rstrip("/"):
            # 不存在的子站 301 回主站首页（1.37MB，标题「bilibili游戏中心 - WIKI」）
            # ⇒ 必须判 dead，否则整份清单里一半是把主站记成活站
            st["done"][sub] = {"status": "dead", "size": size, "final": fin}
            anime_log("(%d/%d) - %s 已合并/不存在（跳回主站首页）" % (i, len(todo), sub))
        else:
            st["blocked"] = 0
            throttle = random.uniform(*ANIME_DELAY)
            name = anime_site_name(html, sub)
            pages = anime_harvest(html, sub)
            st["done"][sub] = {"status": "alive", "size": size,
                               "name": name, "pages": pages}
            anime_log("(%d/%d) ✓ %s「%s」%dKB 目录页 %d 个（%s）"
                      % (i, len(todo), sub, name, size // 1024,
                         sum(len(v) for v in pages.values()),
                         "/".join("%s%d" % (k, len(v)) for k, v in pages.items()) or "-"))
        if i % 20 == 0:
            _save()
        time.sleep(random.uniform(throttle * 0.85, throttle * 1.2))
    _save()

    alive = [k for k, v in st["done"].items() if v.get("status") == "alive"]
    payload = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total": len(st["done"]),
        "alive": len(alive),
        "sites": {k: v for k, v in st["done"].items() if v.get("status") == "alive"},
    }
    tmp = ANIME_OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ANIME_OUT)
    anime_log("本轮结束：累计盘点 %d 个，活站 %d → %s" % (len(st["done"]), len(alive), ANIME_OUT))
    return ANIME_OUT


if __name__ == '__main__':
    # 三模式：默认 discover（资源发现）；corpus 角色语料；diffuse 站群定向扩散
    if len(sys.argv) > 1 and sys.argv[1] == 'diffuse':
        _lim = None
        if '--limit' in sys.argv:
            _lim = int(sys.argv[sys.argv.index('--limit') + 1])
        diffuse_crawl(limit=_lim)
    elif len(sys.argv) > 1 and sys.argv[1] == 'corpus':
        tag = sys.argv[2] if len(sys.argv) > 2 else 'blhx'
        _limit = None
        _until = None
        _maxm = None
        _a = sys.argv[3:]
        _i = 0
        while _i < len(_a):
            if _a[_i] == '--until':
                _until = _a[_i + 1]; _i += 2
            elif _a[_i] == '--max-minutes':
                _maxm = int(_a[_i + 1]); _i += 2
            elif _a[_i].isdigit():
                _limit = int(_a[_i]); _i += 1
            else:
                _i += 1
        CORPUS_DIR.mkdir(parents=True, exist_ok=True)
        corpus_crawl(tag, limit=_limit, until=_until, max_minutes=_maxm)
    else:
        main()
