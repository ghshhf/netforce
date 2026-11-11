# -*- coding: utf-8 -*-
"""netforce / netforce_kb_mcp.py —— 本地统一语料库的 MCP 服务（零依赖 stdio）

不需要 pip 装 mcp：MCP stdio 传输就是逐行 JSON-RPC 2.0。
只暴露只读查询，不写库、不联网、不碰凭据。

本文件只做协议转发；真正的查询/状态逻辑在 kb_query.py，
每次 tools/call 都 reload 一次 —— 改检索规则或状态判定后**立即生效**，
不用重启 MCP 服务、不用主人重新点信任。

工具：
    kb_search       全文检索（主库 + 旁路库，trigram 支持中文，多词自动放宽）
                    结果带 uid / url / 摘录；query="uid:<uid>" 可直接读全文
    kb_read         按 uid 直读正文，可分段；模型自己决定读哪条、读多少
    kb_tag_search   按主题标签筛选
    kb_stats        库规模 / 任务队列 / OCR / 后台心跳 / 导入锁

Resources（MCP 标准「读」语义，11-12 补）：
    kb://help · kb://stats          固定入口资源
    kb://uid/{uid} · kb://search/{query}    URI 模板，10 万条语料不逐条列举

11-12 主人定：不再把 4B 当批量脚本跑，语料库要能被「像 MCP 一样直接读取」。
kb_query.py 每次 tools/call 热重载，所以 uid: / kb:// 直读在旧进程上也能立即生效；
kb_read 工具与 resources/* 方法要等宿主重连 MCP 后才可见，不擅自重启。
"""
from __future__ import annotations

import importlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import kb_query  # noqa: E402

TOOLS = [
    {"name": "kb_search",
     "description": "检索本机统一语料库（网页正文、PDF、书籍、技能卡等），支持中文；多词先精确匹配，0 命中自动放宽为任一词命中。结果自带 uid / url / 正文摘录；想读全文把 query 写成 uid:<uid>（可加 @偏移量 续读），由模型自己决定读哪条。",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "检索词；或 uid:<uid>[@offset][/长度] 直读全文"},
         "limit": {"type": "integer", "description": "返回条数，默认 8", "default": 8},
         "scope": {"type": "string", "description": "main / side / all，默认 all",
                   "default": "all"}}, "required": ["query"]}},
    {"name": "kb_read",
     "description": "按 uid 直接读取一条语料的正文（不经生成模型）。支持完整 uid 或 ≥8 位前缀；offset / maxchars 分段读长文。纯只读。",
     "inputSchema": {"type": "object", "properties": {
         "uid": {"type": "string", "description": "语料 uid，来自 kb_search 结果的 uid= 行"},
         "offset": {"type": "integer", "description": "起始字符偏移，默认 0", "default": 0},
         "maxchars": {"type": "integer", "description": "本次读取长度，默认 4000，上限 20000", "default": 4000},
         "scope": {"type": "string", "description": "main / side / all，默认 all",
                   "default": "all"}}, "required": ["uid"]}},
    {"name": "kb_tag_search",
     "description": "按主题标签筛选语料，标签如 AI/机器学习、法律/政策、哲学/人文理论。",
     "inputSchema": {"type": "object", "properties": {
         "tag": {"type": "string", "description": "标签名"},
         "limit": {"type": "integer", "default": 8}}, "required": ["tag"]}},
    {"name": "kb_stats",
     "description": "查看统一语料库规模、任务队列、OCR 进度、后台心跳与导入锁状态。",
     "inputSchema": {"type": "object", "properties": {}}},
]

IMPL = {"kb_search": "search", "kb_read": "read",
        "kb_tag_search": "tag_search", "kb_stats": "stats"}


def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error:
        msg["error"] = {"code": -32603, "message": str(error)[:300]}
    else:
        msg["result"] = result
    # 🔴 必须 ensure_ascii=True：MCP stdio 是字节管道，宿主客户端不一定按 UTF-8
    #    解码（Windows 上常按 cp936/GBK）。早先这里写 ensure_ascii=False，直接把
    #    中文 UTF-8 字节塞进管道，客户端按本地编码解就变成 "?????" 乱码；而绕开
    #    MCP 直接调 kb_query.search() 一直正常，正是这个差别。
    #    纯 ASCII JSON + \uXXXX 转义后，任何 JSON 解析器都会还原出正确中文。
    sys.stdout.write(json.dumps(msg, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def handle(req):
    if not isinstance(req, dict):
        return
    method = req.get("method")
    mid = req.get("id")
    if method == "notifications/initialized" or (mid is None and method):
        return                                  # 通知类不回包
    params = req.get("params") or {}
    if method == "initialize":
        reply(mid, {"protocolVersion": params.get("protocolVersion") or "2024-11-05",
                    "capabilities": {"tools": {"listChanged": False},
                                     "resources": {"subscribe": False,
                                                   "listChanged": False}},
                    "serverInfo": {"name": "netforce-kb", "version": "1.2"}})
    elif method == "ping":
        reply(mid, {})
    elif method == "tools/list":
        reply(mid, {"tools": TOOLS})
    elif method == "resources/list":
        try:
            importlib.reload(kb_query)
            reply(mid, {"resources": kb_query.resource_list()})
        except Exception as e:
            reply(mid, error="资源列表失败：%s" % e)
    elif method == "resources/templates/list":
        try:
            importlib.reload(kb_query)
            reply(mid, {"resourceTemplates": kb_query.resource_templates()})
        except Exception as e:
            reply(mid, error="资源模板失败：%s" % e)
    elif method == "resources/read":
        try:
            importlib.reload(kb_query)
            uri = params.get("uri")
            mime, text = kb_query.resource_read(uri)
            if mime is None:
                reply(mid, error=text)
            else:
                reply(mid, {"contents": [{"uri": uri, "mimeType": mime,
                                          "text": text}]})
        except Exception as e:
            reply(mid, error="资源读取失败：%s" % e)
    elif method == "tools/call":
        name = params.get("name")
        fn = IMPL.get(name)
        if not fn:
            reply(mid, error="未知工具：%s" % name)
            return
        try:
            importlib.reload(kb_query)         # 热更新：改 kb_query.py 不用重启服务
            text = getattr(kb_query, fn)(params.get("arguments") or {})
            reply(mid, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as e:
            reply(mid, {"content": [{"type": "text", "text": "工具异常：%s" % e}],
                        "isError": True})
    elif mid is not None:
        reply(mid, error="不支持的方法：%s" % method)


def main():
    # 同样绕开 locale：按字节读、显式按 UTF-8 解，不依赖 PYTHONIOENCODING 是否生效。
    # MCP 规范要求 UTF-8；客户端若发 \uXXXX 转义的纯 ASCII 也天然兼容。
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    for line in stream:
        raw = line.strip() if isinstance(line, bytes) else line.strip().encode("utf-8")
        if not raw:
            continue
        try:
            handle(json.loads(raw.decode("utf-8")))
        except Exception as e:
            sys.stderr.write("bad request: %s\n" % str(e)[:200])
            sys.stderr.flush()


if __name__ == "__main__":
    main()
