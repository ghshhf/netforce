# -*- coding: utf-8 -*-
"""netforce / tool_audit.py —— 技能 / MCP 自盘点（只读，不自动删）

由 jobboard_worker 每 30 轮调用一次，是「自己给自己找活」的一条支线。
输出 _kb/TOOL_AUDIT.md：清单 + 失效候选 + 可启用建议。
删除动作不在本脚本里做：明确失效项由领航员按名点删并留日志。
"""
from __future__ import annotations

import os
import json
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(HERE, "_kb")
HOME = os.path.expanduser("~")
WB = os.path.join(HOME, ".workbuddy")
PROJ = os.path.dirname(HERE)
OUT = os.path.join(KB, "TOOL_AUDIT.md")

SKILL_DIRS = [
    ("用户级", os.path.join(WB, "skills")),
    ("项目级", os.path.join(PROJ, ".workbuddy", "skills")),
    ("插件缓存", os.path.join(WB, "plugins", "cache")),
]
MCP_FILES = [
    os.path.join(WB, "mcp.json"),
    os.path.join(PROJ, ".workbuddy", "mcp.json"),
    os.path.join(WB, "settings.json"),
]


def age_days(ts):
    return (time.time() - ts) / 86400 if ts else -1


def scan_skills():
    """名称优先取 SKILL.md frontmatter 的 name；插件缓存里目录名常是版本号，不可信。"""
    rows, seen = [], set()
    for scope, base in SKILL_DIRS:
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if d not in ("node_modules", "__pycache__", ".git")
                           and not d.startswith(".staging-")]
            if ".staging-" in dirpath:
                continue
            if "SKILL.md" not in filenames and "skill.md" not in filenames:
                continue
            sf = os.path.join(dirpath, "SKILL.md")
            if not os.path.isfile(sf):
                sf = os.path.join(dirpath, "skill.md")
            try:
                with open(sf, "r", encoding="utf-8", errors="replace") as f:
                    head = [f.readline() for _ in range(40)]
                name, fm_end = "", 0
                if head and head[0].strip() == "---":
                    for i in range(1, len(head)):
                        if head[i].strip() == "---":
                            fm_end = i + 1
                            break
                    for l in head[1:fm_end]:
                        if l.lower().startswith("name:"):
                            name = l.split(":", 1)[1].strip().strip("\"'")
                            break
                if not name:
                    name = os.path.basename(dirpath)
                body = os.path.getsize(sf)
                desc = " ".join(
                    l.strip() for l in head[fm_end:]
                    if l.strip() and not l.startswith(("#", "---", "`")))[:110]
                key = (scope, name, body)
                if key in seen:
                    continue
                seen.add(key)
                st = os.stat(sf)
                rows.append((scope, name, sf, body, age_days(st.st_mtime), desc))
            except Exception:
                pass
            dirnames[:] = []      # 找到 SKILL.md 就不再往里钻
    return rows


def scan_mcp():
    rows = []
    for p in MCP_FILES:
        if not os.path.isfile(p):
            continue
        try:
            d = json.load(open(p, "r", encoding="utf-8"))
        except Exception as e:
            rows.append((p, "PARSE_ERROR", str(e)[:100], "", ""));
            continue
        servers = d.get("mcpServers") or d.get("servers") or {}
        if isinstance(servers, dict):
            for name, cfg in servers.items():
                cmd = (cfg or {}).get("command", "") if isinstance(cfg, dict) else ""
                url = (cfg or {}).get("url", "") if isinstance(cfg, dict) else ""
                exists = ""
                if cmd:
                    exists = "已找到" if (os.path.isfile(cmd) or _which(cmd)) else "❌ 命令不存在"
                if url:
                    exists = "远程 URL"
                rows.append((p, name, cmd or url, str((cfg or {}).get("args", ""))[:90]
                             if isinstance(cfg, dict) else "", exists))
    return rows


def _which(cmd):
    import shutil
    return shutil.which(cmd) is not None


def main():
    skills = scan_skills()
    mcps = scan_mcp()
    out = ["# 技能 / MCP 自盘点", "",
           "> 由常驻任务板每 30 轮自动生成（只读扫描，不自动删）。",
           "> 生成时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""]
    out += ["## 一、技能清单（%d 个）" % len(skills), "",
            "| 层级 | 名称 | 大小 | 闲置天数 | 摘要 |", "|---|---|---|---|---|"]
    for scope, name, path, size, days, desc in sorted(skills, key=lambda r: (r[0], r[1])):
        out.append("| %s | %s | %s KB | %d | %s |" %
                   (scope, name.replace("|", "/"), round(size / 1024, 1), int(days),
                    desc.replace("|", "/")[:90]))
    out += ["", "## 二、MCP 配置（%d 项）" % len(mcps), "",
            "| 配置文件 | 名称 | 命令/URL | 状态 |", "|---|---|---|---|"]
    for p, name, cmd, args, status in mcps:
        out.append("| %s | %s | %s | %s |" %
                   (os.path.basename(p), name, str(cmd)[:80].replace("|", "/"), status))
    out += ["", "## 三、候选处置（只列，不自动执行）", ""]
    dead = [r for r in mcps if "❌" in r[4] or r[1] == "PARSE_ERROR"]
    stale = [r for r in skills if r[4] > 180]
    empty = [r for r in skills if r[3] < 200]
    out.append("- **失效 MCP**：%d 项（命令路径已不存在 / 配置解析失败）" % len(dead))
    for r in dead:
        out.append("  - `%s` → %s" % (r[1], r[2]))
    out.append("- **半年未动的技能**：%d 个（仅提示，不等于该删）" % len(stale))
    for r in stale[:20]:
        out.append("  - `%s`（%d 天）" % (r[1], int(r[4])))
    out.append("- **疑似空壳技能**：%d 个（SKILL.md < 200 字节）" % len(empty))
    for r in empty[:20]:
        out.append("  - `%s`" % r[1])
    out += ["", "> 删除动作需逐一点名并留日志；本脚本只产出候选。", ""]
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("[tool_audit] 技能 %d / MCP %d / 失效候选 %d → %s" %
          (len(skills), len(mcps), len(dead), OUT))


if __name__ == "__main__":
    main()
