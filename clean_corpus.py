#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按当前过滤规则清洗已有 voice_*.jsonl，原文件先备份到 _bak/。

为什么需要：早期 mode 猜错、auto 太宽，档案页的元数据（日期 / 标签 / 弹数 /
图标名）混进了 lines。训练侧要的是成句的台词与设定，这些必须摘掉。

严格度与抓取时保持一致：
  mode=auto 的站（整页任意表格）走严格闸门；其余站（语音专用容器）只过基础闸门。
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from voice_full import _junk, SITES, CORPUS_DIR   # noqa: E402


def pick(tag):
    """只有在「没有语音专用容器、只能啃整页表格」时才上严格闸门。

    有 vcats 的站（sr / 明日方舟 / 原神 / 赛马娘）抓的是语音专用容器，
    vmode 已经保证干净，再套严格闸门只会把短句 "嗯。" 这类真台词误杀。
    """
    cfg = SITES.get(tag) or {}
    return (not cfg.get("vcats")) and cfg.get("mode") == "auto"


def clean(path, strict):
    """返回 (保留记录列表, 原始记录数, 丢掉的行数)。"""
    keep, drop, n0 = [], 0, 0
    for ln in path.open(encoding="utf-8"):
        try:
            d = json.loads(ln)
        except Exception:                          # noqa: BLE001
            continue
        n0 += 1
        old = d.get("lines") or []
        ls = [x for x in old if len(x) >= 3 and not _junk(x[2], strict)]
        drop += len(old) - len(ls)
        if ls:
            d["lines"] = ls
            keep.append(d)
    return keep, n0, drop


def main(apply=False):
    bak = CORPUS_DIR / "_bak"
    total_in = total_out = total_drop = 0
    plan = []
    for f in sorted(CORPUS_DIR.glob("voice_*.jsonl")):
        if f.name.endswith("_audio.jsonl"):
            continue
        tag = f.stem.split("_", 1)[1]
        if tag not in SITES:
            print(f"{f.name}: 无站点配置，跳过")
            continue
        strict = pick(tag)
        keep, n0, drop = clean(f, strict)
        total_in += n0
        total_out += len(keep)
        total_drop += drop
        plan.append((f, keep, drop))
        print(f"{f.name:28s} {'严格' if strict else '基础'}  "
              f"记录 {n0:5d} -> {len(keep):5d}   丢行 {drop:6d}")

    print(f"\n合计：记录 {total_in} -> {total_out}，丢 {total_drop} 行")
    if not apply:
        print("（预演模式，未写盘；加 --apply 执行）")
        return 0

    bak.mkdir(parents=True, exist_ok=True)
    for f, keep, _d in plan:
        if not keep:
            print(f"! {f.name} 会被清空，保留原文件不动")
            continue
        shutil.copy2(f, bak / f.name)
        tmp = f.with_suffix(f.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for d in keep:
                fh.write(json.dumps(d, ensure_ascii=False) + "\n")
        tmp.replace(f)
        print(f"✔ {f.name}（备份在 _bak/）")
    return 0


if __name__ == "__main__":
    main("--apply" in sys.argv)
