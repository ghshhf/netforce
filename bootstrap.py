#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""bootstrap.py —— 换电脑 clone 后的一键自检（只读，不改任何东西）

对齐项目北极星：clone 下来就能用。本脚本逐层检查「模块 / 配置 / 依赖 / 出口」，
缺什么报什么 + 给修复命令，不用再翻记忆日志手工重建。

用法：
    python bootstrap.py              # 全量自检
    python bootstrap.py --json       # 机器可读输出

纪律：只读、无网络、无窗口、不碰密钥内容（只提示文件在不在）。
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)

# 收编后必须存在的真源码（clone 后最容易缺的就是这些）
CORE_MODULES = [
    "skynet", "skynet.net", "skynet.search",
    "websearch", "parse_all", "parse_assets", "fine_classify",
    "netpolicy", "egress_route", "kb_index", "kb_query",
]
# 第三方依赖：import 名 -> pip 包名
DEPS = {"httpx": "httpx", "lxml": "lxml"}
# 配置文件：缺了就从 example 复制
CONFIGS = [("_netpolicy.json", "_netpolicy.json.example")]
# 可选文件：缺了不算错，只提示
OPTIONAL = [("skynet-fleet", "同级目录，出网层（缺则 websearch 走内置直连）"),
            ("requirements.txt", "依赖清单")]


def chk(name, ok, fix="", detail=""):
    return {"name": name, "ok": bool(ok), "fix": fix, "detail": detail}


def check_modules():
    out = []
    for m in CORE_MODULES:
        try:
            importlib.import_module(m)
            out.append(chk("module:" + m, True))
        except Exception as e:
            out.append(chk("module:" + m, False,
                           "确认该文件已随仓库 clone；缺则检查 git ls-files",
                           "%s: %s" % (type(e).__name__, str(e)[:100])))
    return out


def check_deps():
    out = []
    for mod, pipname in DEPS.items():
        try:
            importlib.import_module(mod)
            out.append(chk("dep:" + mod, True))
        except Exception:
            out.append(chk("dep:" + mod, False, "pip install " + pipname))
    return out


def check_configs():
    out = []
    for real, tmpl in CONFIGS:
        rp, tp = os.path.join(HERE, real), os.path.join(HERE, tmpl)
        if os.path.exists(rp):
            out.append(chk("config:" + real, True, detail="已存在"))
        elif os.path.exists(tp):
            out.append(chk("config:" + real, False,
                           "copy %s %s 然后按本机改" % (tmpl, real),
                           "只有模板，真实配置未建"))
        else:
            out.append(chk("config:" + real, False,
                           "从另一台机器取回，或按 README 重建", "模板也没有"))
    return out


def check_optional():
    out = []
    for name, desc in OPTIONAL:
        p = os.path.join(PARENT, name) if name == "skynet-fleet" else os.path.join(HERE, name)
        out.append(chk("optional:" + name, os.path.exists(p), detail=desc))
    return out


def check_tunnel_key():
    """只报在不在，不读内容。"""
    p = os.path.join(os.path.expanduser("~"), ".fleet", "wsrelay.key")
    return [chk("key:wsrelay", os.path.exists(p),
                "隧道国外站要用；不用隧道可忽略", "~/.fleet/wsrelay.key")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    results = []
    results += check_modules()
    results += check_deps()
    results += check_configs()
    results += check_optional()
    results += check_tunnel_key()
    # 只有「模块缺失」才算硬伤：那是 clone 漏了代码，最致命。
    # 依赖/config 都能一键修（pip install / copy 模板），归到修复项别混为一谈。
    bad = [r for r in results if not r["ok"] and r["name"].startswith("module:")]
    if a.json:
        print(json.dumps({"ok": not bad, "results": results}, ensure_ascii=False, indent=2))
    else:
        print("=" * 60)
        print("天网 clone 自检：%d 项，硬伤 %d 处" % (len(results), len(bad)))
        print("=" * 60)
        for r in results:
            mark = "OK  " if r["ok"] else (
                "MISS" if r["name"].startswith("module:") else "WARN")
            line = " [%s] %-28s" % (mark, r["name"])
            if r["detail"]:
                line += "  " + r["detail"][:50]
            print(line)
            if not r["ok"] and r["fix"]:
                print("       -> " + r["fix"])
        print("-" * 60)
        fixable = [r for r in results if not r["ok"] and not r["name"].startswith(("optional:", "key:", "module:"))]
        if bad:
            print("结论：%d 个模块缺失（clone 漏代码），按上面 -> 提示补。" % len(bad))
        elif fixable:
            print("结论：代码齐了；%d 项环境待装（依赖/配置），按 -> 提示一键修即可跑。" % len(fixable))
        else:
            print("结论：全部就绪，可以直接跑。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
