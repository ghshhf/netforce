# -*- coding: utf-8 -*-
"""netforce / nf.py —— 统一入口（不用记六个脚本名）

    python nf.py status                        四层状态一屏看完
    python nf.py run --rounds 1                跑一轮完整闭环
    python nf.py run --loop                    长期自主闭环（前台）
    python nf.py render --domains example.com  只渲染指定域
    python nf.py download --limit 30           只跑资源续传下载
    python nf.py index --rebuild               重建知识库索引
    python nf.py search 数据集 --min-quality 40
    python nf.py search llm --context --limit 5 （可直接喂给模型）
    python nf.py probe                         只读出网能力报告
    python nf.py set-proxy http://1.2.3.4:8080 远程显式指定固定出口
    python nf.py set-proxy ""                  撤销，回纯直连
    python nf.py export                        导出紧凑知识库 jsonl

所有命令**前台执行**，不改配置、不自动起后台进程（守护由 watchdog.py 负责）。
"""
from __future__ import annotations

import os
import subprocess
import sys

import headless

BASE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

MAP = {
    "status": ("pipeline.py", ["--status"]),
    "run": ("pipeline.py", []),
    "pipeline": ("pipeline.py", []),
    "render": ("render_parse.py", []),
    "download": ("download_assets.py", []),
    "assets": ("download_assets.py", []),
    "index": ("kb_index.py", []),
    "search": ("kb_index.py", ["--search"]),
    "probe": ("netprobe.py", []),
    "policy": ("netpolicy.py", []),
    "export": ("kb_index.py", ["--export", os.path.join(BASE, "_kb", "kb_export.jsonl")]),
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "set-proxy":
        # 唯一允许启用代理的入口：远程显式指定，固定不切换
        if not rest:
            print('用法: nf.py set-proxy URL  或  nf.py set-proxy ""（撤销）')
            return 2
        return headless.run([PY, os.path.join(BASE, "netprobe.py"),
                             "--set-proxy", rest[0]], cwd=BASE).returncode
    if cmd not in MAP:
        print("未知命令：%s\n" % cmd)
        print(__doc__)
        return 2
    script, prefix = MAP[cmd]
    return headless.run([PY, os.path.join(BASE, script)] + prefix + rest, cwd=BASE).returncode


if __name__ == "__main__":
    raise SystemExit(main())
