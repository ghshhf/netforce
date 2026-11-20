#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测每个站「能出文本的路径 + 解析模式」组合。

为什么需要：SITES 里的 path/mode 是猜的。猜错的代价不是抓不到，
而是抓到了 200 但解析出 0 句 —— 全站空跑还看不出来。
每个站抽 2 个样本角色，把候选 (路径模板, mode) 全试一遍，谁出句子多就用谁。
"""
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from voice_full import SITES, BASE, category_members, fetch, parse_page  # noqa: E402

PATHS = ["/{char}/语音", "/{char}", "/{char}/默认/中文-普通话", "/{char}/台词",
         "/{char}/语音记录", "/{char}/语音台词"]
MODES = ["sr_voice", "shipwords", "cells", "tables"]


def is_dialogue(t):
    """判断一行是不是「台词」，而不是数值表里的标签。

    只看行数会踩坑：角色主页的数值表（生命上限 / 法术抗性 / 白垩之子）
    动辄几十行，比真正的语音子页还多，按行数打分必然选错。
    台词的特征是「成句」—— 有停顿标点，或者本身够长。
    """
    t = (t or "").strip()
    if len(t) < 6:
        return False
    if re.search(r"[。！？…～~!?]", t):
        return True
    return len(t) >= 14


def probe(tag, samples=2):
    cfg = SITES[tag]
    names = []
    for cat in cfg["cats"]:
        names += category_members(tag, cat)
        time.sleep(0.2)
    names = [n.split("/")[0] for n in dict.fromkeys(names)]
    if not names:
        return {"ok": False, "why": "名单为空"}
    picks = [names[0], names[len(names) // 2]][:samples]
    tried = []
    for ch in picks:
        for pt in PATHS:
            url = BASE + tag + urllib.parse.quote(pt.format(char=ch), safe="/")
            html, ok = fetch(url, retry=1)
            if not ok:
                tried.append({"char": ch, "path": pt, "mode": "-", "why": "fetch fail"})
                continue
            for mode in MODES:
                try:
                    lines, audios = parse_page(html, mode)
                except Exception as e:                       # noqa: BLE001
                    tried.append({"char": ch, "path": pt, "mode": mode,
                                  "why": f"parse err {e}"})
                    continue
                dlg = [l for l in lines if is_dialogue(l[2])]
                if dlg:
                    tried.append({"char": ch, "path": pt, "mode": mode,
                                  "lines": len(lines), "dlg": len(dlg),
                                  "audios": len(audios),
                                  "sample": [l[2] for l in dlg[:3]]})
            time.sleep(0.2)
    # 按「像台词的行数」排，不按总行数
    tried.sort(key=lambda x: (-x.get("dlg", 0), -x.get("audios", 0)))
    best = tried[0] if tried and tried[0].get("dlg") else None
    return {"ok": bool(best), "best": best, "top": tried[:5],
            "why": None if best else "所有组合均为 0 句像台词的文本"}


def main():
    tags = sys.argv[1:] or list(SITES)
    rep = {}
    for tag in tags:
        print(f"\n===== {tag} ({SITES[tag]['name']})", flush=True)
        r = probe(tag)
        rep[tag] = r
        if r.get("ok"):
            for b in r["top"][:3]:
                print(f"  {b['dlg']:4d}句/共{b['lines']:4d}行/音频{b['audios']:3d}  "
                      f"{b['path']:24s} mode={b['mode']}", flush=True)
                print(f"        {b['sample'][:2]}", flush=True)
        else:
            print(f"  × {r.get('why')}", flush=True)
            for t in r.get("top", [])[:5]:
                print(f"    {t}", flush=True)
    out = Path("G:/_train_corpus_raw/_page_probe.json")
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()
