# -*- coding: utf-8 -*-
"""
netforce / classify.py —— 增量分类器（扫一批，分类一批）

设计要点（用户原话："难道你不应该扫了一批，然后分类一批吗？"）：
  * 扫描器 resource_finder.py 在后台持续往 resources.json 追加；
  * 本脚本每次运行只处理「classified.json 里还没有的新域名」，增量分类，不重复劳动；
  * 分类依据 = 首页 title / meta description（真去抓，不是靠域名瞎猜）+ 域名关键词；
  * 结果落 classified.json（机器读）+ resources_by_category.md（人读，按类分组）；
  * 不回写 resources.json —— 避免和后台扫描器互相覆盖。

用法：
  python classify.py            # 增量：只分类新增的
  python classify.py --all      # 全量重分类（含已分类的）
"""
import json
import os
import re
import sys
import ssl
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import netpolicy          # 统一出网：天网 fleet/net.py 自适应直连优先

BASE = "E:/xmanbian/netforce"
RES = os.path.join(BASE, "resources.json")
CLS = os.path.join(BASE, "classified.json")
MD = os.path.join(BASE, "resources_by_category.md")

# 🔴 旧版硬编码 7890：代理没开时每条请求先撞一次死端口，无头补抓更是 100% 走死代理。
#    现在统一委托 netpolicy（默认直连；自有 7891 / 系统代理只在网络层失败时兜底）。
PROXY = netpolicy.cfg().get("proxy", "")
CTX = netpolicy.CTX

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) netforce-classifier/1.0"


def _opener():
    """兼容旧调用：给一个真直连 opener。"""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=CTX))


# ----------------------------------------------------------- 分类规则
# 每条规则 = (类别, 域名强特征, 域名弱特征, 标题语义词)
# 判定顺序：强域名 → 标题语义 → 弱域名（顺序即优先级）
RULES = [
    # —— 平台/大厂服务放最前：docs.google.com 这类是被引用的平台，不是资源站，
    #     但不删（用户：就算是广告也是可以分类的），单独归一类避免污染其他类别。
    ("平台/大厂服务（非资源站）",
     ["docs.google.com", "play.google.com", "drive.google.com", "youtube.com", "youtu.be",
      "notion.so", "figma.com", "airtable.com", "zapier.com", "slack.com", "zoom.us",
      "t.me", "telegram.me", "twitter.com", "x.com", "facebook.com", "linkedin.com",
      "instagram.com", "wikipedia.org", "amazon.com", "ebay.com", "microsoft.com",
      "apple.com", "caspio.com", "sharepoint", "atlassian", "wixsite", "squarespace"],
     ["sharepoint", "atlassian", "caspio", "wixsite", "squarespace", "webflow"],
     ["sign in to continue", "log in to continue", "workspace", "collaborate with"]),

    ("论文预印本/学术检索",
     ["arxiv", "biorxiv", "ssrn", "preprint", "semanticscholar", "aclanthology", "aclweb",
      "dblp", "pubmed", "sciencedirect", "springer", "ieee.", "researchgate", "paperwithcode",
      "papers.", "scholar", "citations"],
     ["scholar", "paper", "preprint", "proceedings", "journal", "abstract", "archive"],
     ["preprint", "paper", "论文", "research paper", "abstract", "proceedings", "journal",
      "citations", "publications", "e-print", "archive", "journal of"]),

    ("数据集",
     ["dataset", "datasets", "kaggle", "huggingface.co/datasets", "openml", "uci.edu/ml",
      "data.gov", "imagenet", "image-net", "cocodataset", "cityscapes", "kitti", "sintel",
      "opendata", "datahub", "zenodo", "figshare", "commoncrawl", "opensurfaces", "places.csail",
      "sun3d", "nyc3d", "middlebury", "lib.", "lsun", "visualgenome",
      "data.europa.eu", "data.worldbank.org", "data.un.org", "ourworldindata", "worldbank",
      "data.gov.uk", "data.gov.au", "huggingface.co", "paperswithcode"],
     ["dataset", "data set", "image-net", "imagenet", "corpus", "benchmark", "download",
      "open data", "data commons", "data portal"],
     ["dataset", "data set", "数据集", "benchmark data", "download data", "corpus", "语料",
      "database for", "database of", "annotated", "annotations", "开放数据", "数据集下载"]),

# —— 网络安全/信息安全（第 27 类，20:5x 手动普查 774 条「其他/待定」后立）：
    #     这批之前全掉进兜底桶，实测 security/threat/malware/osint 聚成一簇 120 条。
    #     ⚠️ 刻意不用裸 "cyber"（会误伤 CyberChef 这类密码学工具站），只认 cybersecurity/cyber security。
    ("网络安全/信息安全",
     ["abuse.ch", "bazaar.abuse.ch", "malware.bazaar", "hybrid-analysis", "virustotal",
      "any.run", "cuckoo", "yara", "maldetect", "binary.ninja", "ida", "ghidra", "radare",
      "0x44.cc", "blackhillsinfosec", "assetnote", "arnica.io", "atredis", "alephsecurity",
      "8ksec.io", "0ffset.net", "horizon3.ai", "securelist", "kaspersky", "avast", "malwarebytes",
      "pentest", "hackthebox", "tryhackme", "ctftime", "paloaltonetworks", "sans.org",
      "owasp.org", "portswigger", "burpsuite", "nmap", "metasploit", "wireshark", "ghidra",
      "virustotal.com", "crowdstrike", "rapid7", "qualys", "tenable", "splunk", "snyk.io"],
     ["infosec", "information security", "cybersecurity", "cyber security",
      "reverse engineering", "malware analysis", "threat intelligence", "vulnerability"],
     ["infosec", "security research", "security training", "malware", "ransomware",
      "threat intelligence", "threat hunting", "penetration testing", "pentest",
      "vulnerability database", "exploit", "phishing", "siem", "soc ", "dfir",
      "reverse engineering", "reverse engineering", "ctf", "bug bounty", "fuzzing",
      "安全", "逆向", "渗透", "恶意软件", "威胁情报", "漏洞", "攻防"]),

    # —— 邮件/邮箱服务（第 28 类）：这条线直接对接"给每个 AI 配邮箱"的身份层战略——
    #     匿名转发(addy.io)、一次性邮箱(maildrop)、邮箱验证(mailtester/neverbounce)、
    #     自托管邮件营销(buttondown/sendbroadcast)都在这里，实测 19 条。
    ("邮件/邮箱服务",
     ["addy.io", "anonaddy", "maildrop", "guerrillamail", "10minutemail", "temp-mail",
      "mailinator", "yopmail", "dispostable", "mailnesia", "mytemp.email", "emailrep.io",
      "mailtester", "neverbounce", "no2bounce", "proofy.io", "kickbox", "verifier",
      "buttondown.email", "sendbroadcast", "mailcoach", "mailchimp", "sendy", "listmonk",
      "smtp2go", "mailgun", "postmark", "sparkpost", "emailrep", "mailbox.org"],
     ["email service", "mail server", "smtp"],
     ["email verifier", "verify email", "email validation", "email deliverability",
      "disposable email", "temporary email", "anonymous email", "email forwarding",
      "newsletter", "email marketing", "self-hosted email", "catch-all",
      "邮箱验证", "临时邮箱", "一次性邮箱", "匿名邮箱", "邮件转发", "邮件营销", "邮件服务"]),

    ("竞赛/游戏AI",
     ["codingame", "tournament", "contest", "competition", "kaggle.com/c", "aibirds",
      "vindinium", "codecup", "sscai", "challenge", "leaderboard", "hackerrank", "topcoder",
      "robocode", "gvgai", "votchallenge", "davischallenge"],
     ["challenge", "contest", "leaderboard", "tournament"],
     ["competition", "challenge", "leaderboard", "tournament", "contest", "竞赛",
      "game ai", "bot vs", "submit your", "evaluation server"]),

    # —— 游戏资源站（用户给 byrutgame.org 后新增；与"竞赛/游戏AI"区分：
    #     这里是游戏下载/破解/ROM 资源站，不是 AI 竞赛）
    ("游戏/游戏资源",
     ["byrutgame", "repack", "fitgirl", "steamrip", "empress", "dodi", "gog.com",
      "torrent", "rom", "emulator", "gameresource", "gamestorrent", "rg-mechanics",
      "skidrow", "codex", "cpul", "elamigos"],
     ["repack", "rom", "emulator", "torrent"],
     ["скачать игры", "download game", "游戏下载", "torrent game", "repack",
      "rom download", "pc game download", "free game download", "emulator download",
      "游戏资源", "破解游戏"]),

    # —— 大模型 / 模型库（基础模型、权重、checkpoint、模型 hub；与"AI工具/平台"区分：
    #     这里是模型本体/权重分发，不是调用 API 的平台）
    ("大模型/模型库",
     ["modelscope", "modelscope.cn", "civitai", "huggingface.co/models", "hf-mirror",
      "modelzoo", "model-zoo", "liblib", "liblibai", "wan21", "tripo", "fal.ai"],
     ["weights", "checkpoint"],
     ["基础模型", "大模型", "foundation model", "model weights", "预训练模型",
      "pretrained model", "模型下载", "开源模型", "模型库", "模型权重", "model zoo",
      "base model", "checkpoint", "多模态大模型", "llm model", "权重下载", "模型分发"]),

    # —— 加密 / Web3（用户指定方向：钱包/节点/链上/空投/DeFi 等资源与工具，
    #     不是炒币，是给 netforce 扩账号池/身份层用的加密侧资源）——
    ("加密/Web3",
     ["crypto", "blockchain", "ethereum", "bitcoin", "coinbase", "binance", "wallet",
      "metamask", "web3", "defi", "solana", "tron", "bsc", "opensea", "onchain",
      "explorer", "rpc", "airdrop", "zk", "chain.", "token", "node"],
     ["crypto", "blockchain", "wallet", "defi", "web3", "token", "链", "钱包", "节点"],
     ["crypto", "blockchain", "比特币", "以太坊", "钱包", "defi", "web3", "加密货币",
      "区块链", "空投", "节点", "链上", "token", "coin", "数字资产", "on-chain",
      "加密", "虚拟币", "跨链", "混币"]),

    # —— 虚拟卡 / 加密支付（用户指定方向：注册钱包送虚拟卡、充虚拟币走正常支付；
    #     要的是"卡"本身——能过某些平台绑卡/支付验证从而解锁更多资源，不是微信支付功能）——
    ("虚拟卡/加密支付",
     ["virtualcard", "virtual-card", "crypto-card", "cryptocard", "redotpay", "dupay",
      "byswe", "noones", "paxful", "wirex", "utorg", "coinspaid", "cryptopay", "paysafe",
      "prepaid"],
     ["virtual card", "虚拟卡", "crypto card", "usdt card", "加密卡"],
     ["virtual card", "虚拟卡", "crypto card", "usdt card", "加密卡", "充币",
      "充值虚拟币", "数字卡", "prepaid card", "匿名卡", "隐私卡", "注册送卡",
      "wallet get card", "crypto debit"]),

    ("播客",
     ["podcast", "talkingmachines", "lineardigressions", "dataskeptic", "partiallyderivative",
      "datastori", "ocdevel", "twiml", "changelog", "talkpython", "softwareengineeringdaily"],
     [".fm", "radio"],
     ["podcast", "episode", "listen", "audio", "播客", "subscribe on", "itunes", "spotify"]),

    ("框架/库文档",
     ["readthedocs", "pytorch.org", "tensorflow.org", "keras", "scikit-learn", "numpy",
      "pandas.pydata", "nltk", "opennlp", "uima", "deeplearning4j", "spacy", "gensim",
      "allennlp", "flair", "transformers", "scipy", "matplotlib", "seaborn", "opencv",
      "pointclouds", "ceres-solver", "mxnet", "jax", "dlib", "shogun"],
     ["docs.", "documentation", "api.", "pypi", "crates.io"],
     ["documentation", "api reference", "library", "framework", "package", "user guide",
      "release notes", "pip install", "how to install", "tutorial"]),

    ("数据服务/查询工具",
     ["vesselfinder", "marinetraffic", "flightradar", "builtwith", "wappalyzer",
      "similarweb", "crunchbase", "opencorporates", "shodan", "censys", "zoomeye",
      "versiondb", "whois", "ipinfo", "ip2location", "social-searcher", "thatsthem",
      "haveibeenpwned", "urlscan", "virustotal"],
     ["track", "lookup", "finder", "explorer", "analytics", "monitor", "export",
      "searcher", "radar", "scan"],
     ["tracking", "search engine", "lookup", "reverse lookup", "search for", "find",
      "explore", "real-time data", "real time", "tracking data", "analytics",
      "dashboard", "people search", "导出", "查询", "检索", "追踪"]),

    ("素材/图库",
     ["cartoonstock", "shutterstock", "gettyimages", "unsplash", "pexels", "flaticon",
      "fontawesome", "thenounproject", "pixabay", "freepik", "iconfinder", "vecteezy"],
     ["stock", "icons", "fonts", "clipart", "photos", "vectors"],
     ["stock photo", "royalty-free", "royalty free", "icons", "fonts", "clipart",
      "vector art", "素材", "图库", "图片素材"]),

    ("标准/联盟/公益组织",
     ["aibom.org", "w3.org", "ietf", "iso.org", "nist.gov", "owasp", "linuxfoundation",
      "alliance", "consortium", "foundation", "institute"],
     [".org/", "institute", "council", "society"],
     ["standard", "alliance", "consortium", "foundation", "non-profit", "nonprofit",
      "transparency", "accountability", "bill of materials", "open standard",
      "标准", "联盟", "基金会", "公益"]),

    ("目录/索引站",
     ["telegram-group", "directory", "catalog", "listing", "awesome-", "links",
      "nav.", "hao.", "sites."],
     ["directory", "catalog", "list", "index"],
     ["directory", "catalog", "list of", "index of", "curated list", "collection of",
      "best of", "top 100", "目录", "导航", "收录"]),

    # —— 应用库/软件下载站（起因：「爱语」第三方分发站 beihangsoft 等；
    #     与"在线工具/服务"区分：这里是提供 App/软件安装包下载的库站）——
    # ⚠️ 弱域名必须收紧：app./apps./soft. 会误伤 web 应用子域与 emsisoft/whatsapp 类
    ("应用库/软件下载站",
     ["beihangsoft", "baobeiy", "5577.com", "appinn", "iplaysoft", "nirsoft",
      "apkcombo", "apkpure", "apkhere", "apk.", "apkdownload", "softpedia",
      "appgallery", "myapp.com", "coolapk", "pgyer", "appjmz", "ishowme"],
     ["apk", "download.com", "software download", "软件库", "下载站"],
     ["软件下载", "应用下载", "软件库", "应用库", "下载站", "安卓下载", "apk download",
      "app download", "download apk", "软件下载站", "免费下载", "最新版本下载",
      "安装包下载", "app 安卓版下载", "下载站"]),

    ("在线工具/服务",
     ["colab", "jupyter", "playground", "huggingface.co/spaces", "repl.it", "kaggle.com/code",
      "codepen", "observablehq", "streamlit", "gradio", "wandb", "neptune.ai"],
     ["tool", "app", "demo", "sandbox", "ide", "compiler", "editor", "visual"],
     ["online tool", "try it", "playground", "demo", "interactive", "no signup", "free tool",
      "generator", "converter", "visualizer", "一站式", "api for"]),

    ("AI工具/平台",
     ["openai.com", "anthropic", "huggingface.co", "together.ai", "replicate", "groq",
      "fireworks.ai", "ollama", "lmstudio", "openrouter", "perplexity", "mistral", "cohere",
      "deepseek", "qwen", "siliconflow", "zen", "venice.ai", "pollinations", "lmarena"],
     [".ai", "ai.", "-ai", "llm", "gpt", "chat"],
     ["ai agent", "ai tool", "ai platform", "ai-powered", "llm", "large language model",
      "language model", "gpt-", "chatgpt", "chatbot", "inference", "大模型", "生成式",
      "prompt", "rag ", "embedding", "fine-tun", "ai api", "ai assistant", "ai compute",
      "neural network as a service", "ai infrastructure"]),

    ("代码仓库/开源项目",
     ["github.com", "gitlab", "sourceforge", "bitbucket", "codeberg", "gitee", "npmjs"],
     ["github.io", "githubusercontent"],
     ["repository", "source code", "open source", "contribute", "star this", "readme",
      "clone", "fork"]),

    ("会议/学术组织",
     ["nips.cc", "neurips", "icml", "iclr", "ijcai", "emnlp", "naacl", "aaai", "cvpr",
      "iccv", "eccv", "siggraph", "acm.org", "ieee.org", "acl2017", "kdd"],
     ["society", "association", "sig"],
     ["conference", "workshop", "symposium", "call for papers", "会议", "registration",
      "program committee", "annual meeting"]),

    ("技术媒体/实战教程站",
     ["machinelearningmastery", "analyticsvidhya", "hackernoon", "dev.to", "towardsdatascience",
      "distill.pub", "heartbeat", "realpython", "kdnuggets", "computervisionblog",
      "visiondummy", "aishack", "colah.github.io", "jalammar", "bair.berkeley"],
     ["blog.", "blogs.", "medium", "news."],
     ["tutorial", "how to", "guide", "practical", "tips", "explained", "explainer",
      "walkthrough", "cheat sheet", "best practices", "教程", "实战", "hands-on", "articles on"]),

    ("个人博客/研究者主页",
     ["homepages.", "people.", "users.", "staff.", "web.stanford", "csail", "personal",
      "homepage", "~", "researcher", "about.me"],
     ["github.io", "wordpress.com", "blogspot", ".me/", "weebly", "notion.site", "substack"],
     ["blog", "personal", "homepage", "researcher", "my thoughts", "notes on", "随笔",
      "i write", "phd", "research interests", "about me", "curriculum vitae", "cv",
      "postdoctoral", "my research"]),

    ("课程/教程",
     ["coursera", "udacity", "fast.ai", "academy", "kadenze", "scaler", "udemy", "edx",
      "mlss", "introtodeeplearning", "dataschool", "ocw.", "mooc", "bootcamp", "d2l"],
     ["course", "tutorial", "class", "lecture", "school", "syllabus", "curriculum", "learn"],
     ["course", "lecture", "课程", "tutorial", "curriculum", "syllabus", "class notes",
      "learn", "study", "workshop", "summer school", "bootcamp", "teaching", "lecture notes"]),

    ("高校/实验室",
     ["lab", "laboratory", "institute", "mila", "inria", "maxplanck", "ethz", "tsinghua"],
     [".edu", ".ac.", "university", "cs.", "mmlab", "vision.", "cse.", "cuhk", "mit.edu",
      "stanford.edu", "berkeley.edu", "ox.ac.uk", "cam.ac.uk"],
     ["university", "laboratory", "research group", "department of", "institute", "faculty",
      "大学", "实验室", "课题组", "research lab", "computer vision lab", "graphics lab"]),

    ("社区/论坛",
     ["forum", "community", "discourse", "hackr", "flipboard", "speakerdeck",
      "datasciencecentral", "news.ycombinator", "slashdot", "discord", "meetup", "quora",
      "crossvalidated", "datascience.stackexchange"],
     ["community", "hub", "network"],
     ["forum", "community", "discussion", "问答", "community of", "join us", "members",
      "ask and answer"]),

    ("电子书/图书",
     ["book", "oreilly", "manning", "gutendex", "textbook", "handbook", "leanpub",
      "nlpprogress"],
     ["books.", "pdf"],
     ["book", "textbook", "chapter", "ebook", "read online", "handbook", "图书", "free ebook"]),

    # —— OSINT/情报调查（第 29 类）：与"网络安全"互补而非重复——前者是"攻击面/防御"，
    #     这里是人肉调查、开源情报核实（bellingcat、OSINT Consultants 这一系）。
    ("OSINT/情报调查",
     ["bellingcat", "theosintconsultants", "osint", "deaddrop", "effectgroup",
      "tracers", "ziplevel", "maltego", "shodan", "crt.sh", "leak-lookup", "epicfourx",
      "sherlock", "whoisxmlapi", "pipl", "hunchly"],
     ["osint", "open-source intelligence", "open source intelligence"],
     ["osint", "open-source intelligence", "investigations", "investigation",
      "due diligence", "people search", "background check", "digital footprint",
      "skip tracing", "reverse phone", "公开情报", "背景调查", "尽职调查", "调查核实"]),

    # —— 代理/匿名网络（第 30 类）：直接对应"免流转发层/虚拟号转发"那条线，
    #     Web 代理/VPN/反审查客户端是一类实物资源，和"虚拟卡/加密支付"同属身份层基建。
    ("代理/匿名网络",
     ["4everproxy", "adguard-vpn", "protonvpn", "mullvad", "windscribe", "hide.me",
      "psiphon", "lantern", "outline", "shadowsocks", "v2ray", "sing-box", "torproject",
      "proxysite", "kproxy", "croxyproxy", "hide.me", "w3proxy", "spidervm", "croxy"],
     ["web proxy", "proxy server", "vpn", "socks proxy"],
     ["web proxy", "free proxy", "proxy list", "vpn", "anonymous browsing",
      "anonymizer", "ip address changer", "hide my ip", "censorship circumvention",
      "socks5", "代理", "匿名网络", "免费代理", "翻墙工具"]),

    # —— 账号/用户名可用性（第 31 类）：账号注册机与身份层的"前置查询"工具，
    #     跟"邮件/邮箱服务"是同一条链上的两端（先查号可用 → 再收验证码）。
    ("账号/用户名可用性",
     ["checkuser.org", "namechk", "usernamesearch", "knowem", "namecheck", "handle.ws",
      "usernamecity", "hdl.handle.net"],
     ["username checker", "username availability", "handle availability"],
     ["username checker", "username availability", "handle availability",
      "name availability", "is this name available", "account availability",
      "用户名", "账号可用", "账号检测", "用户名可用"]),

    # —— 商业/营销也归一类（用户：就算是广告也是可以分类的，不能因为像广告就丢进待定）
    ("商业服务/营销推广",
     ["cobalt.io", "elliptic.co", "hubspot", "salesforce", "marketo", "mailchimp",
      "segment", "intercom", "drift", "getdemo"],
     ["pricing", "demo", "enterprise", "solutions", "saas", "crm"],
     ["request a demo", "book a demo", "get a demo", "pricing", "enterprise",
      "compliance", "solutions for", "trusted by", "our customers", "our clients",
      "get started free", "free trial", "contact sales", "book a call", "roi",
      "lead", "leads", "subscribe now", "buy now", "营销", "获客"]),

    # —— 新增细分（2026-11-04 主 Agent 扩展）：针对「其他/待定」716 条与未分类 37169 条，
    #     增加日常常见站点类别，只捞未命中，绝不抢已有类别。
    ("新闻媒体/资讯",
     ["reuters.com", "bbc.com", "bbc.co.uk", "cnn.com", "nytimes", "theguardian",
      "bloomberg", "ft.com", "apnews", "npr.org", "aljazeera", "washingtonpost",
      "wsj.com", "techcrunch", "theverge", "wired.com", "arstechnica", "engadget",
      "36kr.com", "ithome.com", "solidot.org", "cnbeta", "oschina", "caixin.com",
      "jiemian.com", "huxiu.com", "globaltimes", "chinanews", "people.com.cn",
      "xinhuanet", "sina.com.cn", "163.com", "qq.com", "sohu.com"],
     ["news", "media", "newspaper", "press", "journal"],
     ["news", "breaking news", "headlines", "latest news", "top stories",
      "新闻", "资讯", "快讯", "报道", "头条", "今日要闻", "时事"]),

    ("电商购物/交易",
     ["shopify", "aliexpress", "taobao.com", "tmall", "jd.com", "etsy.com",
      "shein.com", "temu.com", "dhgate", "banggood", "wish.com", "paypal",
      "stripe.com", "shop.com", "store.", "mercari", "poshmark", "depop",
      "ebay", "amazon", "lazada", "shopee"],
     ["shop", "store", "mall", "cart", "checkout", "ecommerce"],
     ["shop now", "add to cart", "buy now", "online store", "shopping cart",
      "checkout", "pricing", "best price", "free shipping", "购物", "商城",
      "购买", "下单", "电商", "优惠", "折扣价", "现货"]),

    ("视频/直播",
     ["bilibili", "twitch.tv", "vimeo", "dailymotion", "kuaishou", "douyin",
      "iqiyi", "youku.com", "netflix", "hulu.com", "disneyplus", "crunchyroll",
      "niconico", "afreecatv", "huya.com", "douyu.com", "kick.com", "rumble.com",
      "odysee", "lbry", "ted.com", "coursera", "kanzhun"],
     ["video", "live", "stream", "tv", "movie"],
     ["watch videos", "live stream", "video platform", "streaming",
      "movie", "tv show", "episode", "视频", "直播", "看视频", "追剧",
      "弹幕", "影视", "短视频", "在线观看"]),

    ("音乐/音频",
     ["spotify", "soundcloud", "bandcamp", "last.fm", "audiomack", "deezer",
      "tidal.com", "music.apple", "audius", "mixcloud", "hearthis.at",
      "musixmatch", "genius.com", "whosampled", "applepodcasts", "podbean",
      "castbox", "overcast.fm", "pocketcasts"],
     ["music", "audio", "song", "album", "playlist"],
     ["music streaming", "listen to music", "album", "playlist", "lyrics",
      "music", "podcast", "音乐", "在线听歌", "歌曲", "歌词", "专辑", "歌单"]),

    ("社交网络/内容社区",
     ["reddit.com", "tumblr", "pinterest", "mastodon", "bluesky", "threads.net",
      "weibo.com", "douban.com", "vk.com", "ok.ru", "deviantart", "artstation",
      "behance", "dribbble", "500px", "flickr.com", "imgur", "steemit",
      "lemmy", "misskey", "xiaohongshu", "zhi hu", "zhihu.com"],
     ["social", "community", "profile", "feed", "network"],
     ["social network", "share photos", "follow me", "social media",
      "discussion", "community", "社交", "社区", "图文", "分享", "关注",
      "动态", "帖子", "小组"]),

    ("设计/视觉素材",
     ["dribbble", "behance", "artstation", "deviantart", "wallhaven", "wallpaper",
      "awwwards", "cssdesignawards", "500px", "flickr", "pexels", "unsplash",
      "pixabay", "freepik", "flaticon", "iconfinder", "vecteezy", "figma",
      "canva", "remove.bg", "colorhunt", "coolors"],
     ["design", "wallpaper", "inspiration", "ui", "ux"],
     ["design inspiration", "ui design", "ux design", "wallpaper",
      "design resources", "设计", "灵感", "壁纸", "UI", "视觉", "配色",
      "设计素材", "海报"]),

    ("政府/公共机构",
     ["gov.", "gov.cn", "gouv.", ".gov", "parliament", "senate", "congress",
      "europa.eu", "who.int", "un.org", "oecd.org", "worldbank", "imf.org",
      "state.gov", "mod.gov", "stats.gov", "customs.gov", "nhc.gov",
      "mof.gov", "moe.gov", "gov.uk", "gov.au", "gov.sg", "gov.hk",
      "data.gov", "usa.gov", "whitehouse"],
     ["government", "official", "ministry", "department of state"],
     ["government", "official website", "public service", "govt",
      "政府", "政务", "官方网站", "国务院", "外交部", "统计局",
      "公共服务", "政策", "法律法规"]),

    ("招聘/远程工作/自由职业",
     ["indeed.com", "glassdoor", "remoteok", "weworkremotely", "remotemode",
      "flexjobs", "upwork", "fiverr", "freelancer.com", "toptal", "turing.com",
      "hired.com", "angel.co", "wellfound", "zhaopin.com", "liepin.com",
      "lagou.com", "51job.com", "boss.zhipin", "kanzhun", "career", "jobs"],
     ["jobs", "career", "hiring", "recruit", "freelance", "remote"],
     ["job board", "remote jobs", "hire", "apply now", "career",
      "jobs", "freelance", "招聘", "求职", "找工作", "远程办公", "远程工作",
      "自由职业", "简历", "岗位", "薪资"]),

    ("成人内容（保留标记）",
     ["onlyfans", "pornhub", "xvideos", "xhamster", "chaturbate", "stripchat",
      "redtube", "youporn", "hentai", "nsfw", "f95zone", "rule34", "e-hentai",
      "nhentai", "jav", "avgle", "91porn", "sex.com"],
     ["nsfw", "adult", "porn", "xxx"],
     ["adult content", "nsfw", "18+", "explicit", "成人", "色情", "涩涩",
      "av 在线", "porn", "xxx"]),

    ("域名/云/托管/CDN",
     ["cloudflare", "vercel", "netlify", "heroku", "render.com", "fly.io",
      "railway.app", "namecheap", "namesilo", "godaddy", "cloudinary",
      "digitalocean", "aliyun", "tencent cloud", "qcloud", "huaweicloud",
      "serverless", "supabase", "firebase", "aws.amazon", "azure.microsoft",
      "cloud.google", "fastly", "cdnjs", "jsdelivr", "unpkg", "nginx.org"],
     ["hosting", "domain", "server", "cdn", "cloud", "deploy"],
     ["hosting", "deploy", "domain registration", "cloud platform",
      "serverless", "cdn", "dns", "ssl", "域名", "云服务", "托管",
      "服务器", "虚拟主机", "对象存储", "cdn 加速", "部署"]),

    ("编程/开发者资源",
     ["devdocs", "regex101", "leetcode", "codecademy", "freecodecamp",
      "exercism", "rosettacode", "geeksforgeeks", "codewars", "jsfiddle",
      "stackblitz", "codesandbox", "glitch.com", "cheatsheets", "devhints",
      "quickref", "overapi", "mdn.", "developer.mozilla", "w3schools",
      "runoob", "bilibili-ai-bot", "rust-lang", "golang.org", "nodejs.org",
      "python.org", "ruby-lang", "php.net", "dotnet.microsoft", "spring.io",
      "react.dev", "vuejs.org", "angular.io", "svelte.dev", "nextjs.org",
      "typescriptlang", "json.org", "learnxinyminutes"],
     ["code", "dev", "programming", "developer", "cheatsheet"],
     ["programming", "code", "developer", "api docs", "cheat sheet",
      "learn to code", "coding", "编程", "开发", "代码", "程序员",
      "开发者", "api 文档", "速查表", "教程代码"]),

    ("3D/游戏开发资源",
     ["blender.org", "unity", "unrealengine", "sketchfab", "cgtrader",
      "turbosquid", "opengameart", "kenney.nl", "itch.io", "freesound",
      "mixamo", "quixel", "poly.pizza", "threejs", "babylonjs",
      "ambientcg", "polyhaven", "textures.com", "game-icons"],
     ["3d", "game asset", "texture", "model", "rig", "animation"],
     ["3d model", "game assets", "3d assets", "game dev", "texture",
      "model download", "3d", "建模", "游戏素材", "模型下载", "贴图",
      "引擎资源", "blender", "unity", "ue4", "ue5"]),

    ("数据可视化/图表",
     ["tableau", "powerbi", "plotly", "chart.js", "d3js", "echarts",
      "flourish.studio", "datawrapper", "infogram", "observable", "kepler.gl",
      "grafana", "kibana", "metabase", "superset", "rawgraphs"],
     ["dashboard", "visualization", "chart", "graph", "analytics"],
     ["dashboard", "visualization", "charts", "interactive chart",
      "data viz", "数据可视化", "图表", "仪表盘", "可视化分析", "大屏"]),

    ("漫画/小说/文学",
     ["mangadex", "mangakakalot", "komiic", "webnovel", "qidian", "sfacg",
      "archiveofourown", "fanfiction", "wattpad", "royalroad", "ao3.org",
      "novelupdates", "wuxiaworld", "lightnovel", "webtoons", "comic",
      "readcomiconline", "manga", "baozoumanhua", "dmzj", "manhua"],
     ["manga", "novel", "comic", "fiction", "story"],
     ["manga", "read manga", "light novel", "web novel", "comic",
      "fanfiction", "小说", "漫画", "轻小说", "同人", "网文", "阅读",
      "连载", "章节"]),
]


# 需要重新实地勘察的类别（只重抓换标签，绝不删条目）
RECHECK_CATS = ("待验证（未取到标题）", "访问失败（保留待复查）",
                "JS渲染/无静态内容（保留待复查）")

# 手动重验时打过的临时标签（curl/直连/http 救回）也应纳入重勘，
# 否则它们会永远停在"待验证"，规则再也碰不到（--recheck 是唯一能救回它们的入口）。
RECHECK_PREFIX = ("待验证", "疑似失效（保留待复查）")


def _needs_recheck(cat):
    return cat in RECHECK_CATS or cat.startswith(RECHECK_PREFIX)


def _norm(s):
    return (s or "").lower()


def classify(dom, title, desc, err="", http_ok=False):
    """三级优先级分类：
       P1 域名强特征（arxiv / imagenet / kaggle / github.com 等明确标识）
       P2 首页标题语义（最可靠：它是自己写的"我是干什么的"）
       P3 域名弱特征（.edu / .ac. / github.io / blog 等后缀类线索）
    """
    d, t = _norm(dom), _norm(title) + " " + _norm(desc)
    # ⚠️ 只打标签，绝不删除（用户原话："过度自动化，有可能把真正的资源过滤掉"）。
    # 抓不到标题 / 疑似被广告盘占用，都只是「标记」，条目本身永远留在清单里待人工复核。
    if re.search(r"slot|gambling|casino|\bbetting\b|poker|judi|togel|domain for sale|"
                 r"parked|buy this domain|网站已失效|404 not found|account suspended", t):
        return "疑似失效（保留待复查）", "标题命中垃圾词"
    if not title.strip() and not desc.strip():
        # 🔴 细分"抓不到"：站没了 ≠ 我们抓不到。两者都不删，但标签要诚实
        if not http_ok:
            return "访问失败（保留待复查）", "抓取失败:" + (err or "不可达")
        return "JS渲染/无静态内容（保留待复查）", "能连上但无静态 title（可能是纯 JS 站）"
    for cat, sdom, weak, tks in RULES:
        for k in sdom:
            if k in d:
                return cat, "域名强特征:" + k
    for cat, sdom, weak, tks in RULES:
        for k in tks:
            if k in t:
                return cat, "标题语义:" + k
    for cat, sdom, weak, tks in RULES:
        for k in weak:
            if k in d:
                return cat, "域名弱特征:" + k
    return "其他/待定", "未识别"
    for cat, sdom, weak, tks in RULES:
        for k in sdom:
            if k in d:
                return cat
    for cat, sdom, weak, tks in RULES:
        if any(k in t for k in tks):
            return cat
    for cat, sdom, weak, tks in RULES:
        for k in weak:
            if k in d:
                return cat
    return "其他/待定"


# ----------------------------------------------------------- 抓首页元信息
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
DESC_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', re.I | re.S)
DESC_RE2 = re.compile(
    r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']', re.I | re.S)


def _clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()[:300]


def _get(url, timeout=10):
    """统一出网：fleet 自适应（直连优先、仅网络层失败才回退代理），再走候选链 + curl。

    返回 (html|None, 错误串)，保持旧签名，probe() 无需改。
    """
    txt, ch, st, err = netpolicy.fetch_text(url, timeout=timeout, max_bytes=200000)
    if txt:
        return txt, ""
    return None, (err or "不可达")


def probe(item):
    """抓首页 title/description，用于判断这个站是干什么的。

    返回 http_ok / err，用于把"抓不到"细分成两类（都不删，只是标签）：
      · 访问失败（超时/DNS/拒绝）  → 可能是站真没了，也可能是我们这边网络不通
      · JS渲染/无静态内容          → 站活着，只是标题靠 JS 渲染，抓不到静态 title
    """
    dom = item["domain"]
    err = ""
    for scheme in ("https", "http"):
        url = "%s://%s/" % (scheme, dom)
        raw, e = _get(url)
        if raw:
            # meta refresh 跳转：标题其实在目标页（勘察发现 papers.mathyvanhoef.com 就是这种）
            if not TITLE_RE.search(raw):
                mr = re.search(
                    r'http-equiv=["\']?refresh["\']?[^>]*?url=(["\']?)([^"\'>]+)', raw, re.I)
                if mr:
                    tgt = mr.group(2)
                    if tgt.startswith("/"):
                        tgt = scheme + "://" + dom + tgt
                    raw2, _ = _get(tgt)
                    if raw2:
                        raw = raw2
            m = TITLE_RE.search(raw)
            title = _clean(m.group(1)) if m else ""
            m2 = DESC_RE.search(raw) or DESC_RE2.search(raw)
            desc = _clean(m2.group(1)) if m2 else ""
            return {"domain": dom, "url": item["url"], "found_via": item.get("found_via", ""),
                    "title": title, "desc": desc, "http_ok": True, "err": ""}
        err = e or "不可达"
    return {"domain": dom, "url": item["url"], "found_via": item.get("found_via", ""),
            "title": "", "desc": "", "http_ok": False, "err": err}


def load_json(p, default):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default


def main():
    full = "--all" in sys.argv
    recheck = "--recheck" in sys.argv      # 重新实地勘察"抓不到/未验证"的那些
    offline = "--offline" in sys.argv      # 改了规则后离线全量重算（复用已有标题，不联网）
    js = "--js" in sys.argv                # 静态抓不到 title ⇒ 无头浏览器补抓（见 jsprobe.py）
    jsonly = "--jsonly" in sys.argv        # 跳过静态重抓，只对"还没标题"的走浏览器（省一轮静态）
    res = load_json(RES, [])
    prev = {x["domain"]: x for x in load_json(CLS, [])}
    known = {} if full else dict(prev)
    todo = [x for x in res if x["domain"] not in known]
    if recheck:
        redom = {d for d, x in known.items() if _needs_recheck(x.get("category", ""))}
        todo = todo + [x for x in res if x["domain"] in redom]
        print("重新勘察 %d 个「抓不到/未验证」的站点（只换标签，不删条目）" % len(redom))
    print("resources.json 共 %d 个 | 已分类 %d | 本次待分类 %d 个%s"
          % (len(res), len(known), len(todo), "（全量重分类）" if full else ""))

    if offline:
        # 改完规则只想重算标签：一条网络请求都不发，纯本地 classify()。
        # 新域名（还没探过、没标题）会按"无标题"进"其他/待定"，等下次增量联网补。
        known = {}
        for x in res:
            o = prev.get(x["domain"], {})
            cat, basis = classify(x["domain"], o.get("title", ""), o.get("desc", ""),
                                  o.get("err", ""), o.get("http_ok", True))
            known[x["domain"]] = {
                "domain": x["domain"], "url": x.get("url", ""),
                "found_via": x.get("found_via", ""),
                "title": o.get("title", ""), "desc": o.get("desc", ""),
                "http_ok": o.get("http_ok", True), "err": "",
                "category": cat, "basis": basis}
        json.dump(sorted(known.values(), key=lambda x: x["domain"]),
                  open(CLS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("离线全量重算完成（未联网）：累计 %d 个" % len(known))
    elif todo:
        if jsonly:
            # 静态阶段（每条最多 2 scheme × 10s）跑 1546 条要 20+ 分钟，而"还没标题"的
            # 绝大多数是纯 JS 站，静态再抓一遍也是白等 ⇒ 直接复用已有记录，只补浏览器。
            metas = [{"domain": x["domain"], "url": x.get("url", ""),
                      "found_via": x.get("found_via", ""),
                      "title": prev.get(x["domain"], {}).get("title", ""),
                      "desc": prev.get(x["domain"], {}).get("desc", ""),
                      "http_ok": True, "err": ""} for x in todo]
        else:
            with ThreadPoolExecutor(max_workers=24) as ex:
                metas = list(ex.map(probe, todo))
        # 静态通道拿不到 title 的（纯 JS 站 / 人机校验）⇒ 真浏览器渲染后再取。
        # 这一步是"JS渲染/无静态内容"那一大类（实测 1383 条）唯一的解法，改规则没用。
        if js:
            need = [m for m in metas if not m["title"]]
            if need:
                from jsprobe import probe_many
                got = probe_many([m["domain"] for m in need])
                for m in need:
                    g = got.get(m["domain"])
                    if g and (g[0] or g[1]):
                        m["title"], m["desc"] = g
                        m["http_ok"], m["err"] = True, ""
        for m in metas:
            old = prev.get(m["domain"], {})
            if not m["title"] and old.get("title"):
                m["title"], m["desc"] = old.get("title", ""), old.get("desc", "")
                m["http_ok"], m["err"] = True, ""
            m["category"], m["basis"] = classify(
                m["domain"], m["title"], m["desc"],
                m.get("err", ""), m.get("http_ok", False))
            known[m["domain"]] = m
            print("  [%s] %-38s %s" % (m["category"], m["domain"], m["title"][:60]))
        json.dump(sorted(known.values(), key=lambda x: x["domain"]),
                  open(CLS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("已落盘 classified.json（累计 %d 个）" % len(known))
    elif full:
        for d, x in prev.items():
            x["category"], x["basis"] = classify(
                x["domain"], x.get("title", ""), x.get("desc", ""),
                x.get("err", ""), x.get("http_ok", False))
            known[d] = x
        json.dump(sorted(known.values(), key=lambda x: x["domain"]),
                  open(CLS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("全量重分类完成（复用已有标题，未重复联网）")
    else:
        print("无新增，无需分类")

    # 生成人读的分类报告（垃圾/待定排最后）
    allc = sorted(known.values(), key=lambda x: (x["category"], x["domain"]))
    from collections import Counter
    c = Counter(x["category"] for x in allc)
    tail = ("平台/大厂服务（非资源站）", "疑似失效（保留待复查）",
            "访问失败（保留待复查）", "JS渲染/无静态内容（保留待复查）",
            "待验证（未取到标题）", "其他/待定",
            "CDN/资源子域（非独立站，保留）", "WAF/人机验证拦截（站活，保留）")
    order = [k for k, _ in c.most_common() if k not in tail] + \
            [k for k in tail if k in c]
    useful = sum(n for k, n in c.items() if k not in tail)
    lines = ["# 已发现资源站分类清单（边扫边分类，持续追加）", "",
             "后台扫描器持续发现 → 每次 `python classify.py` 增量分类并刷新本文件。", "",
             "> ⚠️ **只打标签，绝不删除任何条目。** 分类是机器初判，"
             "「疑似失效 / 待验证 / 其他待定」三类一律保留在清单中等人工复核——"
             "过度自动化会把真资源误杀，宁可多留、不可自动丢弃。",
             "",
             "**累计 %d 个，机器高置信归类 %d 个，需人工复核 %d 个**"
             % (len(allc), useful, len(allc) - useful),
             "",
             "| 类别 | 数量 | 说明 |", "|---|---|---|"]
    note = {
        "其他/待定": "机器未识别，需人工看一眼",
        "待验证（未取到标题）": "抓取时没拿到标题，站可能仍有效",
        "疑似失效（保留待复查）": "标题像被广告盘占用，但可能只是暂时改版",
    }
    for cat in order:
        lines.append("| %s | %d | %s |" % (cat, c[cat], note.get(cat, "")))
    lines.append("")
    for cat in order:
        lines.append("## %s（%d）" % (cat, c[cat]))
        lines.append("")
        for x in allc:
            if x["category"] != cat:
                continue
            t = x["title"] or "（无标题）"
            d = ("　—　" + x["desc"]) if x["desc"] else ""
            b = x.get("basis", "")
            lines.append("- **%s**　%s%s　`依据:%s`" % (x["domain"], t, d, b))
        lines.append("")
    open(MD, "w", encoding="utf-8").write("\n".join(lines))
    print("分类报告 → %s" % MD)


if __name__ == "__main__":
    main()
