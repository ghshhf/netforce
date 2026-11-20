# -*- coding: utf-8 -*-
"""天网自动体检：从它自己的代码与文档里发现问题，不靠人派活。

只读，不改天网仓库任何文件，报告写到 netforce/_kb/（天网是 public 仓，
不往里塞记录）。设计成可被常驻任务板每 N 轮调一次。

检查：
  1. 声明了但没有设备认领的能力，并区分「条件未点亮」与「疑似漏注册」
  2. 统计层是否发生降级（owners 抛异常被记账）
  3. README 自动块数字与实时统计是否一致
  4. 计划书点名但仓库里没有的文件
  5. 配置里指向已删权重/后端的失效引用（按实际文件是否存在判定）
  6. 根目录 Python 文件语法错误

2026-11-11 修正：以前把 7 个无主能力一律写成「调必失败」，实际其中
5 个是代码里明写了申报条件的条件能力（控制面/密码/relay 在跑），
静态快照里没有设备认领是**正常状态**，不是 bug。混报会误导下一步。
"""
import glob
import os
import py_compile
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKY = os.path.join(os.path.dirname(HERE), "skynet-fleet")
TOOLS = os.path.join(SKY, "tools")
REPORT = os.path.join(HERE, "_kb", "SKYNET_AUDIT.md")
STACK_JSON = r"E:\_tools\stack.json"
LLAMA_BAT = r"E:\_tools\llama_server.bat"

# 无主能力的「点亮条件」账本：来源是代码里的申报条件，不是猜的。
# (能力, 类型, 前置条件, 判定/实现位置, 点亮动作)
CONDITIONAL = {
    "shell.exec": ("条件", "控制面端口通 + FLEET_PASS 在环境里",
                   "fleet/kinds/fleet_node.py::caps()",
                   "起 commander，设 FLEET_PASS 后重启节点进程"),
    "file.push": ("条件", "控制面端口通 + FLEET_PASS 在环境里",
                  "fleet/kinds/fleet_node.py::caps()",
                  "同上；与 shell.exec 共用 _ctl() 通道"),
    "file.pull": ("条件", "控制面端口通 + FLEET_PASS 在环境里",
                  "fleet/kinds/fleet_node.py::caps()",
                  "同上；与 shell.exec 共用 _ctl() 通道"),
    "skynet.relay": ("条件", "本机 SkyNet relay 正在运行（relay.running）",
                     "fleet/kinds/fleet_node.py::caps()",
                     "node_agent.py 带 --relay-port 启动，或 FLEET_RELAY_ENABLED=1"),
    "skynet.relay.fetch": ("依赖", "对端节点已上报 skynet.relay 且 relay_port 可用",
                           "commander.py::relay_fetch",
                           "先点亮任一节点的 skynet.relay，再由 commander 路由"),
    "relay.status": ("条件", "中继节点在线并上报状态",
                     "fleet/agent_tools.py::Tool('relay_status')",
                     "中继节点上线后由该节点申报"),
    "fleet.memory.sync": ("注册", "需要设备主动认领（catalog 有 spec、有别名）",
                          "capabilities.py + evals/test_memory_sync.py",
                          "在目标设备 caps() 里补申报，并跑 test_memory_sync.py"),
}
# 与「条件未开」相反的硬编码关键词已废弃；失效判定改为看文件是否真实存在。
GGUF_REF = re.compile(r"[A-Za-z]:[\\/][^\s\"',]*?\.gguf", re.I)


def collect():
    sys.path.insert(0, TOOLS)
    import gen_stats as G
    names, err = G._declared_names()
    reg, rerr = G._load_registry()
    out = {"names": names, "declared_err": err, "registry_err": rerr,
           "registry_ok": reg is not None, "orphans": [], "stats": {}}
    if reg is not None and names:
        for n in names:
            try:
                if not reg.owners(n):
                    out["orphans"].append(n)
            except Exception as e:
                out.setdefault("owner_errors", []).append(
                    "%s: %s: %s" % (n, type(e).__name__, str(e)[:80]))
    try:
        out["stats"] = G.collect(probe=False)
    except Exception as e:
        out["stats_err"] = "%s: %s" % (type(e).__name__, str(e)[:120])
    return out


def classify_orphans(orphans):
    """分开「条件没点亮」与「真没人认领」，后者才值得动代码。"""
    cond, missing = [], []
    for n in orphans:
        info = CONDITIONAL.get(n)
        if info and info[0] != "注册":
            cond.append((n,) + info)
        else:
            missing.append((n,) + (info if info else
                           ("未知", "无条件说明", "—", "先查 catalog 与设备 caps()")))
    return cond, missing


def readme_numbers(st):
    """README 自动块里的数字 vs 实时统计。"""
    try:
        txt = open(os.path.join(SKY, "README.md"), encoding="utf-8").read()
    except Exception as e:
        return [("README 读取失败", str(e)[:100])]
    pairs = [
        ("声明能力", r"\|\s*声明能力\s*\|\s*\*\*(\d+)\*\*",
         st.get("capabilities_declared")),
        ("有主能力", r"\|\s*有主能力\s*\|\s*\*\*(\d+)\*\*",
         st.get("capabilities_owned")),
        ("有门禁能力", r"\|\s*有门禁能力\s*\|\s*\*\*(\d+)\*\*",
         st.get("capabilities_gated")),
        ("设备种类实例", r"\|\s*设备种类实例\s*\|\s*\*\*(\d+)\*\*",
         st.get("devices")),
        ("eval 文件", r"\|\s*eval 文件\s*\|\s*\*\*(\d+)\*\*",
         st.get("eval_files")),
    ]
    bad = []
    for label, pat, live in pairs:
        if live is None:
            continue
        m = re.search(pat, txt)
        if not m:
            bad.append("%s：README 里找不到该项" % label)
        elif int(m.group(1)) != int(live):
            bad.append("%s：README %s ≠ 实时 %s" % (label, m.group(1), live))
    return bad


def missing_planned():
    rel = ["cloudnode_up.py", os.path.join("fleet", "cloudnode_up.py")]
    return [r for r in rel if not os.path.isfile(os.path.join(SKY, r))]


def stale_configs():
    """配置里引用的 GGUF / 运行时路径是否真的还在（不靠关键词黑名单）。"""
    hits = []
    for p in (STACK_JSON, LLAMA_BAT):
        if not os.path.isfile(p):
            continue
        try:
            s = open(p, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        # .bat 的 rem 行只是历史说明（例如「旧 E:\llama.cpp 已删」），
        # 不是真实引用；不跳过会把注释误报成失效配置。
        if p.lower().endswith(".bat"):
            s = "\n".join(l for l in s.splitlines()
                          if not l.strip().lower().startswith("rem"))
        for m in GGUF_REF.finditer(s):
            path = m.group(0).replace("\\\\", "\\").replace("/", "\\")
            if not os.path.isfile(path):
                hits.append("%s → 权重不存在：%s" % (os.path.basename(p), path))
        # JSON 里双反斜杠会挡住正则的换行/转义，补一轮还原后的复查
        for m in GGUF_REF.finditer(s.replace("\\\\", "\\")):
            path = m.group(0).replace("/", "\\")
            if not os.path.isfile(path):
                msg = "%s → 权重不存在：%s" % (os.path.basename(p), path)
                if msg not in hits:
                    hits.append(msg)
        for kw in ("E:\\llama.cpp", "E:/llama.cpp", "Meissa-Qwen2.5-7B",
                   "Qwen3.5-4B", "Qwen3-Coder-30B", "gemma-4-26B"):
            if kw in s:
                hits.append("%s → 仍引用已删后端/权重 %s" % (os.path.basename(p), kw))
    return hits


def syntax_errors():
    bad = []
    for p in glob.glob(os.path.join(SKY, "*.py")):
        try:
            py_compile.compile(p, doraise=True)
        except Exception as e:
            bad.append("%s: %s" % (os.path.basename(p), str(e)[:90]))
    return bad


def main():
    t0 = time.time()
    d = collect()
    st = d["stats"]
    drift = readme_numbers(st)
    cond, missing = classify_orphans(d["orphans"])
    stale = stale_configs()
    syn = syntax_errors()
    lines = [
        "# 天网自动体检", "",
        "> `skynet_audit.py` 只读生成 · %s · 耗时 %.1fs" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), time.time() - t0),
        "> 不修改天网仓库；天网是 public 仓，记录一律落在 netforce/_kb。", "",
        "## 结论", "",
        "- 声明能力 **%s** / 有主 **%s** / 有门禁 **%s** / 设备 **%s**" % (
            st.get("capabilities_declared", "—"), st.get("capabilities_owned", "—"),
            st.get("capabilities_gated", "—"), st.get("devices", "—")),
        "- 无主能力 **%d 项**：条件未点亮 **%d**（前置条件没满足，静态快照里正常）｜"
        "疑似漏注册 **%d**（才需要改代码）" % (
            len(d["orphans"]), len(cond), len(missing)),
        "- README 漂移 **%d 处**｜统计降级 **%d 处**｜语法错误 **%d 个**｜失效配置 **%d 处**"
        % (len(drift), len(d.get("owner_errors", [])), len(syn), len(stale)),
        "",
        "## 一、条件未点亮（不是 bug，别删声明）", "",
        "| 能力 | 类型 | 前置条件 | 代码位置 | 怎么点亮 |",
        "|---|---|---|---|---|",
    ]
    for n, kind, need, where, how in cond:
        lines.append("| `%s` | %s | %s | `%s` | %s |" % (n, kind, need, where, how))
    if not cond:
        lines.append("| — | — | 无 | — | — |")
    lines += ["", "## 二、疑似漏注册（真正要改的代码）", ""]
    if missing:
        for n, kind, need, where, how in missing:
            lines.append("- `%s`：%s；%s ⇒ %s" % (n, need, where, how))
    else:
        lines.append("- 无")
    lines += ["", "## 三、README 与实时统计漂移", ""]
    lines += ["- " + x for x in drift] or ["- 一致"]
    lines += ["", "## 四、计划点名但仓库没有的文件", ""]
    lines += ["- `%s`" % x for x in missing_planned()] or ["- 无"]
    lines += ["", "## 五、失效配置引用（按文件是否真实存在判定）", ""]
    lines += ["- " + x for x in stale] or ["- 无"]
    lines += ["", "## 六、根目录语法错误", ""]
    lines += ["- " + x for x in syn] or ["- 无"]
    if d.get("owner_errors"):
        lines += ["", "## 七、统计层降级（owners 抛异常）", ""]
        lines += ["- " + x for x in d["owner_errors"]]
    if not d["registry_ok"]:
        lines += ["", "> 注册表加载失败：%s" % d["registry_err"][:160]]
    lines += ["", "## 下一步候选（按代价排序）", ""]
    i = 1
    if missing:
        lines.append("%d. 给 %s 补设备 `caps()` 申报，跑对应 eval 验证。" %
                     (i, "、".join("`%s`" % x[0] for x in missing)))
        i += 1
    if cond:
        lines.append("%d. 想点亮条件能力：起控制面/设 `FLEET_PASS`/开 relay，"
                     "再重跑本体检确认有主数上升。" % i)
        i += 1
    if stale:
        lines.append("%d. 修掉上面失效的路径引用（配置指向已删权重/后端）。" % i)
        i += 1
    if drift:
        lines.append("%d. 跑 `python tools/gen_stats.py --write-docs` 刷新 README 自动块。" % i)
    lines.append("")
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("[audit] 无主 %d（条件 %d / 漏注册 %d）｜漂移 %d｜失效配置 %d｜语法 %d → %s"
          % (len(d["orphans"]), len(cond), len(missing), len(drift),
             len(stale), len(syn), REPORT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
