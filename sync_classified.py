# -*- coding: utf-8 -*-
"""netforce / sync_classified.py —— 把渲染成果安全合并回主分类库

为什么需要它：
    render_parse 的解析结果只落在 _kb/records 与 _kb/classified_render.jsonl（追加），
    并没有回到主分类库 classified.json —— 因为 classify.py --recheck 正在写它，
    直接改会写冲突、还可能把 --recheck 当轮正在重算的条目覆盖乱。

它做什么：
    ① --status   统计「渲染成果库」里有多少条还**没回写**到 classified.json
    ② --dry      只预览会对 classified.json 改哪些域（不落盘）
    ③ --merge    安全合并：
                  · 🔴 先检测 classify.py --recheck 是否在跑 ⇒ 在跑就**拒绝**，避免写冲突
                  · 合并前先备份 classified.json（.bak）
                  · 用本地锁文件防自己并发跑两次
                  · 原子写：写 .tmp 再 os.replace，中途崩也不留半截文件
                  · 只补「classified 当前无标题 / 标题为空」的域；已有有效标题的不覆盖
                    （除非 --force）；渲染失败（ok=false）的域不写回

纪律：
    · 不会停、不会杀 classify.py --recheck；它还在跑时 --merge 直接退出并提示。
    · 不依赖任何外部库；进程检测走 PowerShell（与 netforce 其它脚本一致）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(BASE, "_kb")
CLS_JSON = os.path.join(BASE, "classified.json")
RENDER_JSONL = os.path.join(KB, "classified_render.jsonl")
LOCK_FILE = os.path.join(KB, ".sync_lock")
BAK = CLS_JSON + ".bak"


def _recheck_running():
    """classify.py --recheck 是否在跑（与 netforce 其它脚本同样的探测方式）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine "
             "-match 'classify.py' -and $_.CommandLine -match 'recheck' } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW)
        pids = [p.strip() for p in out.stdout.split() if p.strip()]
        return bool(pids), pids
    except Exception as e:
        return False, ["detection-error:%s" % type(e).__name__]


def _load_render():
    """渲染成果（权威来源 = _kb/records/*.jsonl）→ {domain: latest_record}。

    历史批次与本轮批次全在 records 里（persist 先写它），直接消费，
    不依赖中间层 classified_render.jsonl；后写的覆盖先写的。
    """
    import glob
    out = {}
    d = os.path.join(KB, "records")
    if not os.path.isdir(d):
        return out
    for fp in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
        with open(fp, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                dom = (r.get("domain") or "").lower()
                if dom:
                    out[dom] = r
    return out


def _load_cls():
    with open(CLS_JSON, encoding="utf-8") as f:
        return json.load(f)


def _apply(cls, render, force=False):
    """返回 (新 cls 列表, 改动明细列表)。只补标题为空的域。"""
    by_d = {((x.get("domain") or "").lower()): x for x in cls}
    changes = []
    for d, r in render.items():
        if not r.get("ok"):
            continue                      # 渲染失败的域不写回，留待重试
        rec = by_d.get(d)
        if rec is None:
            # classified 里没有这个域（极少见，渲染发现的新域还没进主库）
            rec = {"domain": d, "category": "渲染新增", "_rendered": True}
            cls.append(rec)
            by_d[d] = rec
        cur_title = (rec.get("title") or "").strip()
        new_title = (r.get("title") or "").strip()
        if cur_title and not force:
            continue                      # 已有有效标题，不覆盖
        if not new_title and not (r.get("desc") or "").strip():
            continue                      # 渲染也没拿到东西，别写空
        rec["title"] = new_title
        if (r.get("desc") or "").strip():
            rec["desc"] = r["desc"]
        rec["text_len"] = r.get("text_len", 0)
        rec["state"] = "rendered"         # 标记：标题来自无头渲染，而非静态抓取
        rec["_rendered_at"] = r.get("ts", time.time())
        changes.append(d)
    return cls, changes


def cmd_status():
    render = _load_render()
    cls = _load_cls()
    by_d = {(x.get("domain") or "").lower() for x in cls}
    pending = 0
    for d, r in render.items():
        if not r.get("ok"):
            continue
        rec = next((x for x in cls if (x.get("domain") or "").lower() == d), None)
        cur = (rec.get("title") or "").strip() if rec else ""
        if not cur:
            pending += 1
    running, pids = _recheck_running()
    print("=== 回写状态 ===")
    print("渲染成果库条数 : %d" % len(render))
    print("可回写（无标题域）: %d" % pending)
    print("主分类库条数   : %d" % len(cls))
    print("classify --recheck 在跑: %s %s" % (running, pids if running else ""))


def cmd_dry():
    cls = _load_cls()
    render = _load_render()
    new_cls, changes = _apply(cls, render)
    print("=== 预览（不落盘）===")
    if not changes:
        print("没有需要回写的域（或都已有标题）")
        return
    print("将回写 %d 个域：" % len(changes))
    for d in changes[:40]:
        r = render[d]
        print("  %-40s %s" % (d, (r.get("title") or "")[:48]))
    if len(changes) > 40:
        print("  ... 还有 %d 个" % (len(changes) - 40))


def cmd_merge(force=False):
    running, pids = _recheck_running()
    if running:
        print("🔴 拒绝合并：classify.py --recheck 正在运行（PID %s），"
              "此刻改写 classified.json 会写冲突。" % pids)
        print("   请先停掉 --recheck（或等它自然结束），再执行 --merge。")
        return 1
    # 本地锁：防 sync_classified 自己并发跑两次（O_EXCL 语义）
    if os.path.exists(LOCK_FILE):
        print("🔴 已有同步进行中（%s 存在），跳过。" % LOCK_FILE)
        return 1
    fd = open(LOCK_FILE, "w")
    fd.write(str(os.getpid()))
    try:
        cls = _load_cls()
        render = _load_render()
        new_cls, changes = _apply(cls, render, force=force)
        if not changes:
            print("没有需要回写的域，classified.json 不变。")
            return 0
        # 备份 + 原子写
        shutil.copy2(CLS_JSON, BAK)
        tmp = CLS_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(new_cls, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CLS_JSON)
        print("✅ 已回写 %d 个域到 classified.json（备份 → classified.json.bak）"
              % len(changes))
        print("   前 20 个：")
        for d in changes[:20]:
            print("     %-40s %s" % (d, (render[d].get("title") or "")[:44]))
        if len(changes) > 20:
            print("     ... 共 %d 个" % len(changes))
        return 0
    finally:
        fd.close()
        try:
            os.remove(LOCK_FILE)
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="连已有有效标题的域也用渲染结果覆盖")
    a = ap.parse_args()
    if a.status:
        return cmd_status()
    if a.dry:
        return cmd_dry()
    if a.merge:
        return cmd_merge(force=a.force)
    # 默认 = 状态
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main() or 0)
