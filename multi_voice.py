#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
multi_voice.py —— 多站点二次元角色语音/台词采集（临时采集线）

定位
    北极星 v12 的语料扩源工具。与 resource_finder.py 的 corpus 模式互补：
      - resource_finder.py  : 只认碧蓝航线的 table-ShipWordsTable 模板
      - multi_voice.py      : 按站分派解析规则，覆盖碧蓝以外的 wiki
    跑完即并入主爬虫（同 2026-10-20 用户定的一贯要求：不长期维护两个脚本）。

已验通的站（2026-10-21 实测）
    sr         崩坏：星穹铁道   /{角色}/语音        -> div.voice_text_chs        约 58 条/角色
    arknights  明日方舟         /{角色}/默认/中文-普通话 -> div.operator-page-value-cell  约 17-33 条
    pcr        公主连结         角色页内表格                                    约 20 条（台词直接称「主人」）
    42lab      少前:云图计划    角色页内表格                                    约 7-11 条
    nikke      胜利女神妮姬     角色页                                        待实测

抓取纪律（都是踩过的坑，别改）
    - 必须直连（不走 7890 代理）：走代理对国内 wiki 返回 HTTP 567 空壳
    - 必须带 Referer: https://wiki.biligame.com/ 与浏览器 UA，否则同样 567
    - 必须 -L 跟跳转（urllib 默认跟，保持默认即可）
    - 限速：默认 4 秒/页，遇风控翻倍

输出
    G:/_train_corpus_raw/voice_<tag>.jsonl
    行格式与 voice_blhx.jsonl 一致：{"char":名字,"wiki":站点,"lines":[[key,label,text],...]}
    其他站没有 data-key，key 用页面所在区块标题的小写 slug 代替，label 用中文区块名。

用法
    python multi_voice.py probe              # 每站抽 3 角色，只报产量不落盘
    python multi_voice.py crawl --sites sr,arknights
    python multi_voice.py crawl --sites all --limit 50
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CORPUS_DIR = Path("G:/_train_corpus_raw")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BASE = "https://wiki.biligame.com/"

# 噪声词：出现在候选句里就丢掉（wiki 自家提示、导航、加载失败提示）
NOISE = re.compile(r"WIKI|导航|编辑|刷新|本页|点击|加载|提交错误|欢迎来到|"
                   r"编辑权限|移动版|电脑版|免责声明|隐私政策|"
                   r"本条消息|全局导航|没有条目|尚未|待补充")

# 站点配置
SITES = {
    "sr": {
        "name": "崩坏：星穹铁道",
        "list_page": "角色一览",
        "voice_path": "/{char}/语音",
        "text_sel": "div.voice_text_chs",
        "expect": 58,
    },
    "arknights": {
        "name": "明日方舟",
        "list_page": "干员一览",
        "voice_path": "/{char}/默认/中文-普通话",
        "text_sel": "div.operator-page-value-cell",
        "expect": 25,
    },
    "pcr": {
        "name": "公主连结",
        "list_page": None,          # 无统一名录页，靠角色页内关联
        "voice_path": "/{char}",
        "text_sel": None,           # 表格行抽取
        "expect": 20,
    },
    "42lab": {
        "name": "少前:云图计划",
        "list_page": None,
        "voice_path": "/{char}",
        "text_sel": None,
        "expect": 10,
    },
    "nikke": {
        "name": "胜利女神妮姬",
        "list_page": "角色一览",
        "voice_path": "/{char}",
        "text_sel": None,
        "expect": 15,
    },
    "umamusume": {
        "name": "赛马娘",
        "list_page": None,
        "voice_path": "/{char}",
        "text_sel": None,
        "expect": 20,
    },
}

# 情境关键词 -> slug（没有 data-key 时按区块标题反推）
KEY_HINTS = [
    ("登录", "login"), ("主界面", "main"), ("查看详情", "detail"),
    ("触摸", "touch"), ("摸头", "headtouch"), ("好感", "feeling"),
    ("誓约", "propose"), ("获得", "unlock"), ("自我介绍", "profile"),
    ("任务", "mission"), ("邮件", "mail"), ("回港", "home"),
    ("委托", "expedition"), ("强化", "upgrade"), ("出击", "battle"),
    ("胜利", "win"), ("失败", "lose"), ("技能", "skill"),
    ("战斗", "battle"), ("信赖", "feeling"), ("语音", "voice"),
]


def key_of(label):
    for kw, slug in KEY_HINTS:
        if kw in label:
            return slug
    return "misc"


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(url, retry=2):
    """直连抓取。返回 (html, ok)。"""
    hdr = {
        "User-Agent": UA,
        "Referer": BASE,
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    for i in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers=hdr)
            with _opener.open(req, timeout=20) as r:
                raw = r.read()
            return raw.decode("utf-8", "ignore"), True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "", False
            time.sleep(2 * (i + 1))
        except Exception:
            time.sleep(2 * (i + 1))
    return "", False


def clean(t):
    t = re.sub(r"[\u200b\u200e\u200f\ufeff]", "", t)
    t = re.sub(r"\s+", "", t)
    return t.strip()


def parse_voice(html, cfg):
    """按站解析台词。返回 [(key,label,text)]。"""
    from bs4 import BeautifulSoup
    s = BeautifulSoup(html, "lxml")
    for bad in s(["script", "style"]):
        bad.decompose()

    out, seen = [], set()

    def push(label, text):
        t = clean(text)
        if not (6 <= len(t) <= 80):
            return
        if not re.search(r"[，。？！…~♪]", t):
            return
        if not re.search(r"[\u4e00-\u9fff]", t):
            return
        if NOISE.search(t) or t in seen:
            return
        seen.add(t)
        out.append((key_of(label), label[:16], t))

    sel = cfg.get("text_sel")
    if sel:
        nodes = s.select(sel)
        # 每个台词节点向上找最近的、带标题的容器，取出区块名
        for n in nodes:
            label = ""
            p = n
            for _ in range(4):
                p = p.parent
                if p is None:
                    break
                h = p.find(["h1", "h2", "h3", "h4", "th", "dt"])
                if h:
                    label = h.get_text("", strip=True)[:16]
                    break
            push(label, n.get_text("", strip=True))
    else:
        # 表格行抽取：逐行取单元格，标题取行内 th
        for tr in s.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            label = ""
            body = [c for c in cells if c.name == "td"]
            ths = [c for c in cells if c.name == "th"]
            if ths:
                label = ths[0].get_text("", strip=True)[:16]
            for c in body:
                for p in (c.find_all("p") or [c]):
                    push(label, p.get_text("", strip=True))

    return out


def list_chars(html, tag):
    """从名录页抽角色名。"""
    from bs4 import BeautifulSoup
    s = BeautifulSoup(html, "lxml")
    pre = "/" + tag + "/"
    names = []
    for a in s.find_all("a", href=True):
        h = a["href"]
        if not h.startswith(pre):
            continue
        tail = urllib.parse.unquote(h[len(pre):])
        if not tail or any(c in tail for c in ":#?/"):
            continue
        if not (1 < len(tail) <= 12):
            continue
        if NOISE.search(tail):
            continue
        if not re.search(r"[\u4e00-\u9fff]", tail):
            continue
        names.append(tail)
    # 去重保序
    return list(dict.fromkeys(names))


def char_url(tag, cfg, char):
    path = cfg["voice_path"].format(char=urllib.parse.quote(char))
    return BASE + tag + urllib.parse.quote(path)


def probe(per_site=3):
    print(f"{'站':12s} {'角色':14s} {'条数':>5s}  样例")
    for tag, cfg in SITES.items():
        if cfg.get("list_page"):
            html, ok = fetch(BASE + tag + "/" + urllib.parse.quote(cfg["list_page"]))
            chars = list_chars(html, tag)[:per_site] if ok else []
        else:
            chars = []
        if not chars:
            # 无名录页的站，用已知代表角色探
            fallback = {"pcr": ["可可萝", "凯露", "佩可莉姆"],
                        "42lab": ["克罗琦", "帕斯卡", "赫波"],
                        "umamusume": ["特别周", "无声铃鹿", "东海帝王"]}
            chars = fallback.get(tag, [])
        got = 0
        for ch in chars:
            html, ok = fetch(char_url(tag, cfg, ch))
            lines = parse_voice(html, cfg) if ok else []
            got += len(lines)
            samp = lines[0][2][:34] if lines else "-"
            print(f"{tag:12s} {ch:14s} {len(lines):5d}  {samp}")
            time.sleep(0.5)
        print(f"{'-'*12} 小计 {got} 条")
    return 0


def crawl(sites, limit=None, delay=4.0, until_chars=None):
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    for tag in sites:
        cfg = SITES[tag]
        outfile = CORPUS_DIR / f"voice_{tag}.jsonl"
        done = set()
        if outfile.exists():
            for ln in outfile.open(encoding="utf-8"):
                try:
                    done.add(json.loads(ln)["char"])
                except Exception:
                    pass
        chars = []
        if cfg.get("list_page"):
            html, ok = fetch(BASE + tag + "/" + urllib.parse.quote(cfg["list_page"]))
            if ok:
                chars = list_chars(html, tag)
        if not chars:
            chars = until_chars or []
        chars = [c for c in chars if c not in done]
        if limit:
            chars = chars[:limit]
        print(f"[{tag}] {cfg['name']}：待抓 {len(chars)}（已有 {len(done)}）", flush=True)

        n_ok = n_line = 0
        for i, ch in enumerate(chars, 1):
            html, ok = fetch(char_url(tag, cfg, ch))
            lines = parse_voice(html, cfg) if ok else []
            if lines:
                with outfile.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"char": ch, "wiki": tag, "lines": lines},
                                       ensure_ascii=False) + "\n")
                n_ok += 1
                n_line += len(lines)
                print(f"[{tag}] {i}/{len(chars)} ✓ {ch} {len(lines)} 句", flush=True)
            else:
                print(f"[{tag}] {i}/{len(chars)} · {ch} 无台词", flush=True)
            time.sleep(delay)
        print(f"[{tag}] 完成：{n_ok} 角色 / {n_line} 句 -> {outfile}", flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["probe", "crawl"])
    ap.add_argument("--sites", default="all")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--delay", type=float, default=4.0)
    a = ap.parse_args()

    if a.mode == "probe":
        return probe()
    tags = list(SITES) if a.sites == "all" else [x.strip() for x in a.sites.split(",") if x.strip()]
    bad = [t for t in tags if t not in SITES]
    if bad:
        print("未知站点:", bad, "可用:", list(SITES))
        return 2
    return crawl(tags, limit=a.limit, delay=a.delay)


if __name__ == "__main__":
    sys.exit(main())
