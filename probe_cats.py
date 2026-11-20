#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测每个 biligame wiki 的真实分类名与成员数。

SITES 里的 cats 是手填的，12/13 站点名单为空就是这个原因：
  · 有的 wiki 分类名不是「角色」（明日方舟是「干员」，可能还有别的）
  · 有的 wiki 被 WAF 挡（nikke 返回拦截页）
不猜，直接问 API：allcategories + 关键词过滤，再对候选逐个验成员数。
"""
import json
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from voice_full import SITES, api   # noqa: E402

# 可能承载角色名单的分类名关键词
PREFIXES = ["角色", "干员", "人形", "精灵", "舰船", "人物", "构造体", "英雄",
            "偶像", "学生", "从者", "干员", "战姬", "誓灵", "少女"]


def allcats(tag, prefix, limit=500):
    out, cont = [], {}
    while True:
        p = {"action": "query", "list": "allcategories",
             "acprefix": prefix, "aclimit": str(limit)}
        p.update(cont)
        d = api(tag, p)
        if "query" not in d:
            return out, d.get("error", {})
        out += [c["*"] for c in d["query"].get("allcategories", [])]
        if "continue" in d and len(out) < 1200:
            cont = d["continue"]
            time.sleep(0.2)
        else:
            return out, None


def members(tag, cat, cap=5000):
    n, cont = 0, {}
    while True:
        p = {"action": "query", "list": "categorymembers",
             "cmtitle": "Category:" + cat, "cmlimit": "500"}
        p.update(cont)
        d = api(tag, p)
        if "query" not in d:
            break
        n += len([m for m in d["query"]["categorymembers"] if m["ns"] == 0])
        if "continue" in d and n < cap:
            cont = d["continue"]
            time.sleep(0.2)
        else:
            break
    return n


def main():
    tags = sys.argv[1:] or list(SITES)
    report = {}
    for tag in tags:
        print(f"\n===== {tag} ({SITES[tag]['name']}) 现行cats={SITES[tag]['cats']}", flush=True)
        hits = {}
        for pfx in PREFIXES:
            cats, err = allcats(tag, pfx)
            if err:
                print(f"  ! {pfx}: API error {err}", flush=True)
                continue
            for c in cats:
                if len(c) > 24:
                    continue
                hits[c] = pfx
        if not hits:
            print("  × 无候选分类", flush=True)
            report[tag] = {"ok": False}
            continue
        # 只验名字带「角色/人物/干员/人形/精灵/舰船/构造体/英雄/学生/从者」的分类
        cand = [c for c in hits if any(k in c for k in
                ("角色", "人物", "干员", "人形", "精灵", "舰船", "构造体",
                 "英雄", "学生", "从者", "偶像", "战姬"))]
        cand = sorted(set(cand), key=len)[:14]
        got = []
        for c in cand:
            n = members(tag, c)
            if n:
                got.append([c, n])
                print(f"  {n:5d}  {c}", flush=True)
            time.sleep(0.15)
        got.sort(key=lambda x: -x[1])
        report[tag] = {"ok": bool(got), "cats": got}
    out = Path("G:/_train_corpus_raw/_cat_probe.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()
