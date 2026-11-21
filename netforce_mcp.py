# -*- coding: utf-8 -*-
"""
netforce_mcp.py —— 把 netforce 采集内核暴露为 stdio MCP server
==============================================================
封装能力：
  * netforce_search         skynet search（多引擎聚合 + 意图校验）
  * netforce_probe          探测 URL 价值（L1-L4 四级判定）
  * netforce_structure      站内结构：robots + sitemap + feed
  * netforce_stats          采集总览
  * netforce_hosts          域画像排行
  * netforce_netstat        下载层实况
  * netforce_reverse        逆向提取（web/bin/video 三档）
  * netforce_query_resources 查询本地资源索引 resources.json / classified.json
  * netforce_classify       本地离线重算分类（复用已有标题，不发请求）
  * netforce_kb_search      全文检索本地知识库（渲染正文 + 多维标签），冷门资源在这里
  * netforce_kb_stats       知识库/资源落盘规模统计
  * netforce_kb_assets      列出已续传落盘的真实资源文件（可按域名/扩展名过滤）

协议：MCP stdio transport（每行一个 JSON-RPC 2.0 消息）。
实现只用标准库，不依赖 mcp 包，避免缺依赖导致启动即退出。
"""
import json
import os
import subprocess
import sys

# 解释器随调用方，避免写死路径后换机/换盘找不到 Python
PY = sys.executable if sys.executable and os.path.isfile(sys.executable) else "python"
BASE = r"E:/xmanbian/netforce"
SKYNET = os.path.join(BASE, "skynet.py")
CLASSIFY = os.path.join(BASE, "classify.py")
RES = os.path.join(BASE, "resources.json")
CLS = os.path.join(BASE, "classified.json")

ENV = dict(os.environ)
ENV["PYTHONPATH"] = BASE
ENV.setdefault("NETFORCE_PROXY", "")

MAX_TEXT = 12000


def _run(args, timeout=180):
    """调用 netforce 脚本，返回 (rc, stdout, stderr)。"""
    try:
        p = subprocess.run(
            [PY] + args,
            cwd=BASE, env=ENV, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", "TIMEOUT"
    except Exception as e:
        return -1, "", "%s" % e


def _load_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------- tools
TOOLS = [
    {
        "name": "netforce_search",
        "description": "多引擎聚合搜索资源站/页面（含意图校验，垃圾结果自动过滤）。例：搜 'open source game voice lines dataset'",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "engines": {"type": "string", "description": "引擎逗号分隔，默认 ddg,searx,github", "default": "ddg,searx,github"},
                "top": {"type": "integer", "description": "返回条数上限", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "netforce_probe",
        "description": "探测一个或多个 URL 的价值：可达性/实质内容/密度/解析校验四级判定",
        "inputSchema": {
            "type": "object",
            "properties": {
                "urls": {"type": "string", "description": "URL 列表，空格分隔，如 'https://a.com https://b.org'"},
            },
            "required": ["urls"],
        },
    },
    {
        "name": "netforce_structure",
        "description": "解析站内结构：robots.txt + sitemapindex 递归 + feed，看站点提供什么内容入口",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "站点 URL，如 https://opengameasset.net/"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "netforce_extract",
        "description": "爬取并当场抽取：给 URL、要什么字段，直接返回干净结构化 JSON（不再是乱文本）。例：url=https://x.com，fields=title,price,author",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要爬取的页面 URL"},
                "prompt": {"type": "string", "description": "抽取要求，默认：提取标题和主要内容", "default": "提取标题和主要内容"},
                "fields": {"type": "string", "description": "字段列表，逗号分隔，如 title,price,author", "default": "title,summary"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "netforce_stats",
        "description": "netforce 采集总览：已记录 URL/MB/域、判定分布、进程状态",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "netforce_hosts",
        "description": "域画像排行：哪些站长期优质（host/探测次数/优质次数/最高分/均分）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "返回条数", "default": 30},
            },
        },
    },
    {
        "name": "netforce_netstat",
        "description": "下载层实况：会话后端/每域并发/风控码/通道熔断/出口健康度/连通性",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "netforce_reverse",
        "description": "逆向提取目标里的知识：web=抽API端点/内联状态/sourcemap；bin=抽字符串/URL/版本号；video=字幕/弹幕/抽帧",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "URL、文件路径或 b 站短链"},
                "kind": {"type": "string", "description": "web/bin/video", "default": "web", "enum": ["web", "bin", "video"]},
            },
            "required": ["target"],
        },
    },
    {
        "name": "netforce_query_resources",
        "description": "查询本地资源索引：按类别/关键词/域名过滤 resources.json 与 classified.json",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "类别名（如 网络安全/信息安全、数据集），留空则不限"},
                "keyword": {"type": "string", "description": "标题/域名关键词"},
                "limit": {"type": "integer", "description": "返回条数上限", "default": 50},
                "high_confidence_only": {"type": "boolean", "description": "只返回机器高置信归类（排除待复查桶）", "default": False},
            },
        },
    },
    {
        "name": "netforce_classify",
        "description": "本地离线重算分类标签（复用已抓标题，不发网络请求）。新增规则后用于快速刷新全部条目",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "description": "offline=离线全量重算（推荐）；all=复用标题重分类（同offline）", "default": "offline", "enum": ["offline", "all"]},
            },
        },
    },
    {
        "name": "netforce_kb_search",
        "description": "全文检索本地知识库（无头渲染后的正文/标题/结构化元数据 + 多维细标签）。外部搜索只会给热门结果，冷门资源要在这里查。支持中英文与标签过滤；context=true 输出可直接放进回答的紧凑格式",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词，如 '数据集'、'open source llm'；留空则按标签浏览", "default": ""},
                "forms": {"type": "string", "description": "资源形态：数据集/文档·手册/代码·仓库/电子书·档案 等", "default": ""},
                "topics": {"type": "string", "description": "主题：AI·机器学习/安全·渗透/生物·医学 等", "default": ""},
                "lang": {"type": "string", "description": "语种：中文 / 英文·拉丁 / 日文", "default": ""},
                "value": {"type": "string", "description": "价值档：高 / 中 / 低", "default": ""},
                "min_quality": {"type": "integer", "description": "最低质量分 0-100", "default": 0},
                "limit": {"type": "integer", "description": "返回条数", "default": 10},
                "context": {"type": "boolean", "description": "true=输出可直接喂给模型的紧凑上下文", "default": True},
            },
        },
    },
    {
        "name": "netforce_kb_stats",
        "description": "本地知识库规模：已解析文档、全文索引条数、主题/形态分布、已落盘资源数量与体积",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "netforce_kb_assets",
        "description": "列出已经续传落盘的真实资源文件（JSON/XML/PDF/数据集等），可按域名或扩展名过滤，返回本地路径供直接读取",
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "域名关键词，留空不限", "default": ""},
                "ext": {"type": "string", "description": "扩展名，如 .json / .pdf", "default": ""},
                "limit": {"type": "integer", "description": "返回条数", "default": 30},
            },
        },
    },
]


def _tool_call(name, args):
    args = args or {}
    try:
        if name == "netforce_search":
            q = args.get("query", "")
            eng = args.get("engines", "ddg,searx,github")
            top = int(args.get("top", 20))
            rc, out, err = _run(["skynet.py", "search", q, "--engines", eng, "--top", str(top)], 240)
            return rc, out, err
        if name == "netforce_probe":
            urls = args.get("urls", "")
            rc, out, err = _run(["skynet.py", "probe"] + urls.split(), 240)
            return rc, out, err
        if name == "netforce_structure":
            rc, out, err = _run(["skynet.py", "structure", args.get("url", "")], 240)
            return rc, out, err
        if name == "netforce_extract":
            rc, out, err = _run([
                "ai_extract.py",
                "--url", args.get("url", ""),
                "--prompt", args.get("prompt") or "提取标题和主要内容",
                "--fields", args.get("fields") or "title,summary",
            ], 240)
            return rc, out[:MAX_TEXT], err
        if name == "netforce_stats":
            rc, out, err = _run(["skynet.py", "stats"], 120)
            return rc, out, err
        if name == "netforce_hosts":
            lim = int(args.get("limit", 30))
            rc, out, err = _run(["skynet.py", "hosts", "--limit", str(lim)], 120)
            return rc, out, err
        if name == "netforce_netstat":
            rc, out, err = _run(["skynet.py", "netstat"], 120)
            return rc, out, err
        if name == "netforce_reverse":
            rc, out, err = _run(["skynet.py", "reverse", args.get("target", ""), "--kind", args.get("kind", "web")], 300)
            return rc, out, err
        if name == "netforce_query_resources":
            return _query_resources(args)
        if name == "netforce_classify":
            rc, out, err = _run(["classify.py", "--offline"], 300)
            return rc, out, err
        if name == "netforce_kb_search":
            return _kb_search(args)
        if name == "netforce_kb_stats":
            return _kb_stats()
        if name == "netforce_kb_assets":
            return _kb_assets(args)
        return 1, "", "unknown tool: %s" % name
    except Exception as e:
        return -1, "", "%s" % e


def _kb_search(args):
    """查本地知识库：走 kb_index.py 的 FTS5 + 标签过滤，不发起任何网络请求。"""
    q = (args.get("query") or "").strip()
    argv = ["kb_index.py", "--search", q, "--limit", str(int(args.get("limit", 10)))]
    for flag, key in (("--forms", "forms"), ("--topics", "topics"),
                      ("--lang", "lang"), ("--value", "value")):
        v = (args.get(key) or "").strip()
        if v:
            argv += [flag, v]
    mq = int(args.get("min_quality", 0) or 0)
    if mq:
        argv += ["--min-quality", str(mq)]
    if args.get("context", True):
        argv.append("--context")
    rc, out, err = _run(argv, 120)
    if not out.strip():
        return rc, "本地知识库无命中（可放宽标签/质量分，或先用 netforce_kb_stats 看规模）", err
    return rc, out, err


def _kb_stats():
    rc1, a, e1 = _run(["kb_index.py", "--stats"], 120)
    rc2, b, e2 = _run(["download_assets.py", "--status"], 120)
    text = "=== 知识库索引 ===\n%s\n=== 资源落盘 ===\n%s" % (a.strip(), b.strip())
    return 0 if rc1 == 0 and rc2 == 0 else 1, text, (e1 + e2)[:400]


def _kb_assets(args):
    host = (args.get("host") or "").strip().lower()
    ext = (args.get("ext") or "").strip().lower()
    lim = int(args.get("limit", 30))
    path = os.path.join(BASE, "_kb", "assets", "manifest.jsonl")
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if host and host not in (d.get("host") or "").lower():
                    continue
                if ext and not (d.get("url") or "").lower().split("?")[0].endswith(ext):
                    continue
                out.append({"path": d.get("path"), "url": d.get("url"),
                            "host": d.get("host"), "size": d.get("size"),
                            "sha256": d.get("sha256")})
    except Exception:
        return 0, "尚无落盘资源（跑 pipeline.py 后才会有）", ""
    out = out[-lim:]
    return 0, (json.dumps(out, ensure_ascii=False, indent=2)
               if out else "无匹配资源（manifest 存在但过滤后为空）"), ""


def _query_resources(args):
    cat = (args.get("category") or "").strip()
    kw = (args.get("keyword") or "").strip().lower()
    lim = int(args.get("limit", 50))
    hc = bool(args.get("high_confidence_only", False))
    tail = ("平台/大厂服务（非资源站）", "疑似失效（保留待复查）", "访问失败（保留待复查）",
            "JS渲染/无静态内容（保留待复查）", "待验证（未取到标题）", "其他/待定")
    cls = _load_json(CLS) or []
    res = _load_json(RES) or []
    if not cls and not res:
        return 0, "资源索引为空或不可读：%s / %s" % (CLS, RES), ""
    out = []
    for x in cls:
        c = x.get("category", "")
        if cat and c != cat:
            continue
        if hc and c in tail:
            continue
        blob = " ".join(str(x.get(k, "")) for k in ("domain", "title", "desc", "url"))
        if kw and kw not in blob.lower():
            continue
        out.append({"domain": x.get("domain", ""), "url": x.get("url", ""),
                    "title": x.get("title", ""), "category": c, "basis": x.get("basis", "")})
        if len(out) >= lim:
            break
    if not out and cat:
        # 回退：全量扫 resources.json 的域名，给类别空结果时提示
        return 0, "类别「%s」无已分类条目；已分类总 %d 条，可换类别或用 keyword 过滤" % (cat, len(cls)), ""
    return 0, json.dumps(out, ensure_ascii=False, indent=2), ""


# ---------------------------------------------------------------- MCP transport
def _resp(id, result=None, error=None):
    m = {"jsonrpc": "2.0", "id": id}
    if error is not None:
        m["error"] = {"code": -32000, "message": error}
    else:
        m["result"] = result
    return m


def main():
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mid = msg.get("id")
        method = msg.get("method", "")
        if method == "initialize":
            out = _resp(mid, {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-03-26"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "netforce-mcp", "version": "1.0.0"},
            })
        elif method == "notifications/initialized":
            continue
        elif method == "ping":
            out = _resp(mid, {})
        elif method == "tools/list":
            out = _resp(mid, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name", "")
            rc, out, err = _tool_call(name, params.get("arguments", {}))
            if rc != 0 and not out:
                out = err or "执行失败 rc=%s" % rc
            out = (out or "").strip()[:MAX_TEXT]
            text = out if out else ("[完成 rc=%s]" % rc)
            out = _resp(mid, {"content": [{"type": "text", "text": text}], "isError": False})
        else:
            out = _resp(mid, error="method not found: %s" % method)
        # 🔴 与 netforce_kb_mcp.py 同一个坑：stdio 是字节管道，宿主客户端不一定
        #    按 UTF-8 解码。ensure_ascii=True 让信封保持纯 ASCII，中文由客户端的
        #    JSON 解析器还原；内层 text 仍是正常中文字符串，不会双重转义。
        sys.stdout.write(json.dumps(out, ensure_ascii=True) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
