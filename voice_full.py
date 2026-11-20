#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
voice_full.py —— 多站角色台词 + 语音音频 一次性采集

定位（2026-10-21 用户定调）
    「你爬文本的时候随便把语音也爬一下，就这么简单。」
    ⇒ 文本和音频不是二选一，是同一趟抓取里一起拿走。

与既有脚本的关系
    resource_finder.py  : 通用爬虫，corpus 模式只认碧蓝 table-ShipWordsTable
    multi_voice.py      : 多站文本采集（第一版，角色名录靠爬一览页 -> 噪声大）
    voice_full.py       : 本脚本。名单走 MediaWiki API（干净），文本 + 音频一趟拿下
    三个脚本的关系：本脚本是 multi_voice.py 的替代，跑通后并入 resource_finder.py 子命令。

名单来源（实测可用，2026-10-21）
    api.php?action=query&list=categorymembers&cmtitle=Category:<分类>
    各站分类实测数量：
        arknights 干员=434   pcr 角色=294(+角色剧情206)   epic7 角色=290
        ys 角色=133   sr 角色=102(角色语音=95)   nikke 角色=93
        42lab 人形=94   umamusume 角色语音=80   zzz 角色=62
        bh3 角色=33   18trip 角色=30   datealive 精灵=27   breakmycase 角色=26

音频可获取性（实测）
    blhx  碧蓝航线  : ✅ tr 内直链 patchwiki.biligame.com/images/blhx/**.mp3，与台词一一对应
    sr    星穹铁道  : ❌ div.bikit-audio 的 data-src 为空，运行时才填，无浏览器拿不到
    其余站          : 能抓到直链就下，抓不到只记 URL 为空，不纠缠

输出
    G:/_train_corpus_raw/voice_<tag>.jsonl
        {"char":..,"wiki":..,"lines":[[key,label,text]],"audio":[[key,label,url,local]],..}
        保留 lines 字段与 voice_blhx.jsonl 完全兼容，新增 audio 旁挂，旧流程不用改。
    G:/_train_corpus_raw/audio/<tag>/<角色>/<序号>_<key>.mp3

用法
    python voice_full.py list                       # 只列各站名单数量
    python voice_full.py crawl --sites blhx,sr --audio
    python voice_full.py crawl --sites all --limit 20 --audio
"""
import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from skynet import net as snet, store as sstore   # noqa: E402

CORPUS_DIR = Path("G:/_train_corpus_raw")
AUDIO_DIR = CORPUS_DIR / "audio"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BASE = "https://wiki.biligame.com/"

NOISE = re.compile(r"WIKI|导航|编辑|刷新|本页|点击|加载|提交错误|欢迎来到|"
                   r"编辑权限|移动版|电脑版|免责声明|隐私政策|"
                   r"本条消息|全局导航|没有条目|尚未|待补充")

AUDIO_RE = re.compile(r"https?://[^\"'\s>]+\.(?:mp3|ogg|wav|m4a)")

KEY_HINTS = [
    ("登录", "login"), ("主界面", "main"), ("查看详情", "detail"),
    ("触摸", "touch"), ("摸头", "headtouch"), ("好感", "feeling"),
    ("誓约", "propose"), ("获得", "acquire"), ("自我介绍", "profile"),
    ("任务", "mission"), ("邮件", "mail"), ("回港", "home"),
    ("委托", "expedition"), ("强化", "upgrade"), ("出击", "battle"),
    ("胜利", "win"), ("失败", "lose"), ("技能", "skill"),
    ("战斗", "battle"), ("信赖", "feeling"), ("语音", "voice"),
    ("晋升", "upgrade"), ("交谈", "talk"), ("闲置", "idle"),
]


def key_of(label):
    for kw, slug in KEY_HINTS:
        if kw in label:
            return slug
    return "misc"


# ---------------------------------------------------------------- 站点配置
# vcats ：语音分类。**成员标题本身就是页面地址**（如 "阿兰/语音"、"12F/默认/中文-普通话"），
#         不用拼 path —— 比任何模板都稳，有就优先用。
# cats  ：角色分类，只用来出名单，页面地址靠 path 模板拼（模板是猜的）。
# mode  ：解析器。auto = 四套全跑一遍合并，不赌单一 mode。
SITES = {
    "blhx": {
        "name": "碧蓝航线",
        "cats": ["舰船"],
        "path": "/{char}",
        "mode": "shipwords",          # table.table-ShipWordsTable + data-key
        "audio": True,
    },
    "sr": {
        "name": "崩坏：星穹铁道",
        "cats": ["角色"],
        "vcats": ["角色语音"],         # 成员标题形如 "阿兰/语音"
        "vmode": "sr_voice",
        "path": "/{char}/语音",
        "mode": "sr_voice",
        "audio": False,
    },
    "arknights": {
        "name": "明日方舟",
        "cats": ["干员"],
        "vcats": ["语音"],            # 成员标题形如 "12F/默认/中文-普通话"
        "vmode": "cells",
        "path": "/{char}/默认/中文-普通话",
        "mode": "auto",
        "audio": True,
    },
    "pcr": {
        "name": "公主连结",
        "cats": ["角色"],
        "path": "/{char}",
        "mode": "auto",
        "audio": True,
    },
    "epic7": {
        "name": "第七史诗",
        "cats": ["角色"],
        "path": "/{char}",
        "mode": "auto",
        "audio": True,
    },
    "ys": {
        "name": "原神",
        "cats": ["角色"],
        "vcats": ["角色语音"],         # 成员标题形如 "阿贝多语音"
        "vmode": "sr_voice",
        "path": "/{char}语音",
        "mode": "auto",
        "audio": False,
    },
    "nikke": {
        "name": "胜利女神妮姬",
        "cats": ["角色"],
        "path": "/{char}",
        "mode": "auto",
        "audio": True,
    },
    "42lab": {
        "name": "少前:云图计划",
        "cats": ["人形"],
        "path": "/{char}",
        "mode": "auto",
        "audio": True,
    },
    "umamusume": {
        "name": "赛马娘",
        "cats": ["角色语音"],
        "vcats": ["角色语音"],         # 成员标题形如 "角色语音 爱丽数码"
        "vmode": "sr_voice",
        "path": "/{char}",
        "mode": "auto",
        "audio": True,
    },
    "zzz": {
        "name": "绝区零",
        "cats": ["角色"],
        "path": "/{char}",
        "mode": "auto",
        "audio": False,
    },
    "bh3": {
        "name": "崩坏3",
        "cats": ["角色"],
        "path": "/{char}",
        "mode": "auto",
        "audio": True,
    },
    "18trip": {
        "name": "18TRIP",
        "cats": ["角色"],
        "path": "/{char}",
        "mode": "auto",
        "audio": True,
    },
    "datealive": {
        "name": "约战精灵再临",
        "cats": ["精灵"],
        "path": "/{char}",
        "mode": "auto",
        "audio": True,
    },
    "breakmycase": {
        "name": "Break My Case",
        "cats": ["角色"],
        "path": "/{char}",
        "mode": "auto",
        "audio": True,
    },
}

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_HDR = {"User-Agent": UA, "Referer": BASE, "Accept-Language": "zh-CN,zh;q=0.9"}


def _record_fail(url, kind, err, path=""):
    """失败落账，供 `skynet.py retry` 补抓。失败不该是一件没人知道的事。"""
    try:
        con = sstore.connect()
        sstore.mark(con, url, urllib.parse.urlparse(url).netloc, kind=kind,
                    verdict="", err=str(err)[:120], path=path)
        con.close()
    except Exception:
        pass


def _record_ok(url, kind, size, path=""):
    try:
        con = sstore.connect()
        sstore.mark(con, url, urllib.parse.urlparse(url).netloc, kind=kind,
                    verdict="ok", size=size, path=path)
        con.close()
    except Exception:
        pass


def fetch(url, retry=2, binary=False):
    """统一走 skynet 下载层：会话复用 / 域限速 / 全局退避 / 通道熔断 / 真状态码校验。

    原实现是裸 urllib 直连：每个请求重做 TLS 握手、无同域限速、
    且完全不看状态码 —— WAF 返回的拦截页会被当成正文收下。
    """
    r = snet.fetch_text(url, timeout=25, gap=0.6, retry=retry, budget=45)
    if r.ok:
        return (r.data, True) if binary else (r.text, True)
    _record_fail(url, "text", r.error or f"HTTP {r.status}")
    return (b"", False) if binary else ("", False)


def api(tag, params):
    p = dict(params)
    p.setdefault("format", "json")
    u = f"{BASE}{tag}/api.php?" + urllib.parse.urlencode(p)
    h, ok = fetch(u)
    if not ok:
        # 别静默成 {}。静默的后果是「名单 0」被当成「这站没角色」，
        # 实际是 API 没通 —— 2026-10-21 那次 12 个站空跑就是这么瞒过去的。
        print(f"  ! {tag} api 失败：{u[:110]}", flush=True)
        return {}
    try:
        return json.loads(h)
    except Exception as e:
        print(f"  ! {tag} api 非 JSON（{type(e).__name__}）：{h[:80]!r}", flush=True)
        return {}


def category_members(tag, cat):
    out, cont = [], {}
    while True:
        p = {"action": "query", "list": "categorymembers",
             "cmtitle": "Category:" + cat, "cmlimit": "500"}
        p.update(cont)
        d = api(tag, p)
        if "query" not in d:
            break
        out += [m["title"] for m in d["query"]["categorymembers"] if m["ns"] == 0]
        if "continue" in d:
            cont = d["continue"]
        else:
            break
        time.sleep(0.3)
    return out


def clean(t):
    t = re.sub(r"[\u200b\u200e\u200f\ufeff]", "", t)
    return re.sub(r"\s+", "", t).strip()


# ---------------------------------------------------------------- 解析
def parse_page(html, mode):
    """返回 (lines, audios)。lines=[[key,label,text]]；audios=[[key,label,url]]"""
    from bs4 import BeautifulSoup
    s = BeautifulSoup(html, "lxml")
    for bad in s(["script", "style"]):
        bad.decompose()
    lines, audios, seen = [], [], set()

    def push(key, label, text, row_html=""):
        t = clean(text)
        if not (4 <= len(t) <= 90):
            return
        if not re.search(r"[\u4e00-\u9fff]", t):
            return
        if NOISE.search(t) or t in seen:
            return
        seen.add(t)
        lines.append([key, label[:16], t])
        if row_html:
            m = AUDIO_RE.search(row_html)
            if m:
                audios.append([key, label[:16], m.group(0)])

    if mode == "shipwords":
        for t in s.find_all("table", class_="table-ShipWordsTable"):
            for tr in t.find_all("tr"):
                key = tr.get("data-key") or ""
                if not key:
                    continue
                th = tr.find("th")
                label = th.get_text("", strip=True) if th else ""
                row_html = str(tr)
                for p in tr.find_all("p", class_="ship_word_line"):
                    if "舷号" in p.get_text() or "号舰" in p.get_text():
                        continue
                    push(key, label, p.get_text("", strip=True), row_html)

    elif mode == "sr_voice":
        for n in s.select("div.voice_text_chs"):
            label = ""
            p = n
            for _ in range(4):
                p = p.parent
                if p is None:
                    break
                h = p.find(["h1", "h2", "h3", "h4", "th", "dt"])
                if h:
                    label = h.get_text("", strip=True)
                    break
            push(key_of(label), label, n.get_text("", strip=True))

    elif mode == "cells":
        for n in s.select("div.operator-page-value-cell"):
            label = ""
            p = n
            for _ in range(4):
                p = p.parent
                if p is None:
                    break
                h = p.find(["h1", "h2", "h3", "h4", "th"])
                if h:
                    label = h.get_text("", strip=True)
                    break
            push(key_of(label), label, n.get_text("", strip=True), str(n.parent))

    else:  # tables
        for tr in s.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            ths = [c for c in cells if c.name == "th"]
            label = ths[0].get_text("", strip=True) if ths else ""
            row_html = str(tr)
            for c in cells:
                if c.name != "td":
                    continue
                for p in (c.find_all("p") or [c]):
                    push(key_of(label), label, p.get_text("", strip=True), row_html)

    return lines, audios


JUNK_WORDS = {"生命上限", "攻击力", "防御力", "法术抗性", "阻挡数", "部署费用",
              "再部署", "初始费用", "攻击间隔", "重量", "星级", "职业",
              "立绘", "默认立绘", "角色资料", "角色立绘默认", "普通攻击",
              "三维设定", "背景故事", "角色外号", "觉醒材料消耗", "受试人形档案"}
JUNK_RE = re.compile(r"^[\d.%+\-/×x\s()]+$")
DATE_RE = re.compile(r"^\d{1,2}月\d{1,2}日$|^\d{4}年\d{1,2}月")
FILE_RE = re.compile(r"(文件|檔案|File|Image):|\.(?:png|jpe?g|webp|gif|svg|mp3|ogg)\b",
                     re.I)
# 数值 / 技能 / 版本号 —— 档案页的重灾区。真台词几乎不会出现这些词。
MECH_RE = re.compile(r"■|●|◆|▶|【相当于|相当于|攻击力|防御力|技能等级|技能伤害|"
                     r"爆裂时间|装弹数|换弹时间|冷却时间|命中率|暴击率|成长[A-S]|"
                     r"版本）|限定召唤|普攻|魔法伤害|物理伤害|护盾】|\d+(?:\.\d+)?%")
DLG_PUNCT = re.compile(r"[。！？…～~!?]")


def is_dialogue(t):
    """这一行像不像「话说出口」的句子。

    只看行数会被骗：角色档案页的元数据（日期、标签、弹数、图标名）比台词还多。
    成句的特征是有停顿标点，或者本身够长。
    """
    t = (t or "").strip()
    if len(t) < 6:
        return False
    return bool(DLG_PUNCT.search(t)) or len(t) >= 14


def _junk(t, strict=False):
    """strict=True 用于 tables 通道（整页任意表格，噪声最重）。"""
    if len(t) > 160:
        return True
    if JUNK_RE.match(t):
        return True
    if t.count("·") >= 3:                     # "A·B·C·D·E" 名字串
        return True
    if t.strip("（）()[]【】：: ") in JUNK_WORDS:
        return True
    if not strict:
        return False
    if DATE_RE.match(t) or FILE_RE.search(t) or "｜" in t:
        return True
    if MECH_RE.search(t):
        return True
    return not is_dialogue(t)


def parse_auto(html):
    """四套解析器全跑一遍再合并 —— 不再赌单一 mode。

    赌 mode 的代价有两种，且都看不出来：
      · 200 但解析出 0 句（选择器不对）
      · 把游戏数值表当台词收了（选择器太宽）
    合并时按来源分严格度：前三套打的是语音专用容器，几乎不掺假；
    只有 tables 是「整页任意表格」，必须过严格闸门。
    """
    lines, audios, seen, aseen = [], [], set(), set()
    for mode in ("sr_voice", "shipwords", "cells", "tables"):
        strict = mode == "tables"
        try:
            ls, au = parse_page(html, mode)
        except Exception:                     # noqa: BLE001
            continue
        for k, lab, t in ls:
            if t in seen or _junk(t, strict):
                continue
            seen.add(t)
            lines.append([k, lab, t])
        for k, lab, u in au:
            if u in aseen:
                continue
            aseen.add(u)
            audios.append([k, lab, u])
    return lines, audios


# ---------------------------------------------------------------- 音频下载
def download_audio(url, dest, retry=1):
    """音频落盘走 skynet：Range 续传 + 内容指纹去重 + 失败落账。

    原来把整个文件 read() 进内存再 write_bytes，大文件既占内存又无法续传；
    中断就直接 return 0，下次还得从头下。
    """
    if dest.exists() and dest.stat().st_size > 800:
        return dest.stat().st_size
    r = snet.download(url, dest, timeout=45, gap=0.4, dedupe=True)
    if r.ok and dest.exists() and dest.stat().st_size > 800:
        return dest.stat().st_size
    _record_fail(url, "audio", r.error or "体积不足 800B", str(dest))
    return 0


# ---------------------------------------------------------------- 主流程
def do_list(sites):
    print(f"{'站':14s} {'来源':22s} {'数量':>6s}")
    total = 0
    for tag in sites:
        cfg = SITES[tag]
        vc = cfg.get("vcats") or []
        if vc:
            n = len(roster(tag))
            total += n
            print(f"{tag:14s} {'语音:' + ','.join(vc):22s} {n:6d}", flush=True)
        else:
            for cat in cfg["cats"]:
                m = category_members(tag, cat)
                total += len(m)
                print(f"{tag:14s} {'角色:' + cat:22s} {len(m):6d}", flush=True)
                time.sleep(0.3)
    print(f"{'合计':14s} {'':22s} {total:6d}")
    return 0


def char_of(title):
    """从页面标题还原角色名。

    语音分类的成员标题有四种形态，都得能剥干净：
      "阿兰/语音"            -> 阿兰      （sr）
      "12F/默认/中文-普通话"  -> 12F       （明日方舟）
      "阿贝多语音"            -> 阿贝多    （原神）
      "角色语音 爱丽数码"      -> 爱丽数码  （赛马娘）
    """
    t = title.split("/")[0]
    if t.startswith("角色语音 "):
        t = t[len("角色语音 "):]
    if t.endswith("语音") and len(t) > 2:
        t = t[:-2]
    return t.strip() or title


def roster(tag):
    """拿到该站的抓取任务：[(页面标题, 解析mode)]。

    有 vcats 语音分类就直取成员标题当页面地址 —— 最稳，不用猜模板；
    没有才退回「角色分类 + path 模板」，这种情况 mode 一律 auto，不赌选择器。
    返回的标题同时充当「这条记录属于哪个角色」，用于断点续抓。
    """
    cfg = SITES[tag]
    vc = cfg.get("vcats") or []
    if vc:
        out = []
        for c in vc:
            for t in category_members(tag, c):
                if t.startswith(("模板:", "Template:", "分类:", "Category:")):
                    continue
                out.append((t, cfg.get("vmode") or cfg["mode"]))
            time.sleep(0.25)
        if out:
            return out
        print(f"  ! {tag} 语音分类 {vc} 取到 0 条，退回角色分类", flush=True)
    out = []
    for c in cfg["cats"]:
        for n in category_members(tag, c):
            ch = n.split("/")[0] if n.endswith("/语音") else n
            out.append((cfg["path"].format(char=ch).lstrip("/"), cfg["mode"]))
        time.sleep(0.25)
    return list(dict.fromkeys(out))


def do_crawl(sites, limit=None, with_audio=True, delay=2.5):
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    grand_txt = grand_aud = 0
    for tag in sites:
        cfg = SITES[tag]
        outfile = CORPUS_DIR / f"voice_{tag}.jsonl"
        done = set()
        if outfile.exists():
            for ln in outfile.open(encoding="utf-8"):
                try:
                    d = json.loads(ln)
                    done.add(d.get("src") or d["char"])
                except Exception:
                    pass

        tasks = roster(tag)
        todo = [t for t in tasks if t[0] not in done]
        if limit:
            todo = todo[:limit]
        print(f"[{tag}] {cfg['name']}：名单 {len(tasks)}，已完成 {len(done)}，本次抓 {len(todo)}",
              flush=True)
        if not tasks:
            print(f"[{tag}] ⚠ 名单为空 —— 不是这站没角色，是名单没拿到（API/分类名问题）",
                  flush=True)
            continue

        n_char = n_txt = n_aud = 0
        for i, (title, mode) in enumerate(todo, 1):
            char = char_of(title)
            # 注意：标题里保留原始中文，最后统一 quote 一次。重复 quote 会变 %25XX 直接 404。
            url = BASE + tag + urllib.parse.quote("/" + title, safe="/")

            html, ok = fetch(url)
            if not ok:
                lines, audios = [], []
            elif mode == "auto":
                lines, audios = parse_auto(html)
            else:
                lines, audios = parse_page(html, mode)

            audio_rows = []
            if lines and with_audio and cfg.get("audio"):
                adir = AUDIO_DIR / tag / re.sub(r'[\\/:*?"<>|]', "_", char)
                for idx, (key, label, aurl) in enumerate(audios, 1):
                    ext = aurl.rsplit(".", 1)[-1][:4]
                    fn = f"{idx:03d}_{key}_{hashlib.md5(aurl.encode()).hexdigest()[:8]}.{ext}"
                    dest = adir / fn
                    sz = download_audio(aurl, dest)
                    if sz:
                        n_aud += 1
                        audio_rows.append([key, label, aurl, str(dest.relative_to(CORPUS_DIR))])

            if lines:
                rec = {"char": char, "wiki": tag, "src": title, "lines": lines}
                if audio_rows:
                    rec["audio"] = audio_rows
                with outfile.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_char += 1
                n_txt += len(lines)
                print(f"[{tag}] {i}/{len(todo)} ✓ {char} 文{len(lines)} 音{len(audio_rows)}",
                      flush=True)
            else:
                print(f"[{tag}] {i}/{len(todo)} · {char} 无", flush=True)
            time.sleep(delay)

        grand_txt += n_txt
        grand_aud += n_aud
        print(f"[{tag}] 完成：{n_char} 角色 / 文本 {n_txt} 句 / 音频 {n_aud} 个 -> {outfile}",
              flush=True)

    print(f"全部完成：文本 {grand_txt} 句，音频 {grand_aud} 个", flush=True)
    return 0


def do_audio_index(tag, limit=None, delay=1.5):
    """为已有 voice_<tag>.jsonl 补音频索引，输出 voice_<tag>_audio.jsonl（不动原文件）。"""
    cfg = SITES[tag]
    src = CORPUS_DIR / f"voice_{tag}.jsonl"
    if not src.exists():
        print(f"[{tag}] 缺 {src}，跳过")
        return 0
    chars, done = [], set()
    for ln in src.open(encoding="utf-8"):
        try:
            d = json.loads(ln)
            chars.append(d["char"])
        except Exception:
            pass
    dst = CORPUS_DIR / f"voice_{tag}_audio.jsonl"
    if dst.exists():
        for ln in dst.open(encoding="utf-8"):
            try:
                done.add(json.loads(ln)["char"])
            except Exception:
                pass
    todo = [c for c in chars if c not in done]
    if limit:
        todo = todo[:limit]
    print(f"[{tag}] {cfg['name']}：角色 {len(chars)}，已补音频 {len(done)}，本次 {len(todo)}", flush=True)

    n_char = n_aud = 0
    for i, char in enumerate(todo, 1):
        path = cfg["path"].format(char=char)
        url = BASE + tag + urllib.parse.quote(path, safe="/")
        html, ok = fetch(url)
        if not ok:
            print(f"[{tag}] {i}/{len(todo)} · {char} 页面失败", flush=True)
            time.sleep(delay)
            continue
        _, audios = parse_page(html, cfg["mode"])
        rows = []
        adir = AUDIO_DIR / tag / re.sub(r'[\\/:*?"<>|]', "_", char)
        for idx, (key, label, aurl) in enumerate(audios, 1):
            ext = aurl.rsplit(".", 1)[-1][:4]
            fn = f"{idx:03d}_{key}_{hashlib.md5(aurl.encode()).hexdigest()[:8]}.{ext}"
            dest = adir / fn
            sz = download_audio(aurl, dest)
            if sz:
                n_aud += 1
                rows.append([key, label, aurl, str(dest.relative_to(CORPUS_DIR))])
        with dst.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"char": char, "wiki": tag, "audio": rows},
                               ensure_ascii=False) + "\n")
        n_char += 1
        print(f"[{tag}] {i}/{len(todo)} ✓ {char} 音{len(rows)}", flush=True)
        time.sleep(delay)
    print(f"[{tag}] 音频补完：{n_char} 角色 / {n_aud} 个 -> {dst}", flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["list", "crawl", "audio-index"])
    ap.add_argument("--sites", default="all")
    ap.add_argument("--site", default=None, help="audio-index 单站")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--delay", type=float, default=2.5)
    ap.add_argument("--no-audio", action="store_true")
    a = ap.parse_args()

    if a.mode == "audio-index":
        if not a.site:
            print("audio-index 需要 --site")
            return 2
        return do_audio_index(a.site, limit=a.limit, delay=a.delay)

    tags = list(SITES) if a.sites == "all" else [x.strip() for x in a.sites.split(",") if x.strip()]
    bad = [t for t in tags if t not in SITES]
    if bad:
        print("未知站点:", bad)
        return 2
    if a.mode == "list":
        return do_list(tags)
    return do_crawl(tags, limit=a.limit, with_audio=not a.no_audio, delay=a.delay)


if __name__ == "__main__":
    sys.exit(main())
