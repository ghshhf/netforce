# -*- coding: utf-8 -*-
"""
netforce / anime_diffuse.py —— 定向扩散小爬虫（二次元站群专用）

为什么单独起一个：
  主爬虫 resource_finder.py 是"从 awesome-list 锚点递归找资源站"的逻辑，出网默认走
  本机代理；而 wiki.biligame.com 这类站**套代理必被 567 风控页打回**（实测 7.3KB 空壳，
  正文一个字都没有）。改主爬虫的取页通道会牵动它的整套判重/并发/计分逻辑，风险大、
  收益小 ⇒ 按用户 00:25 的指示「不行的话就专门再起个小爬虫，不用管那个大爬虫」，
  这里单独一条单线程、慢速、可续跑的扩散线，只干一件事：把一个站群的目录页盘干净。

干什么（第一相）：
  读 wiki_siblings.txt 的 1,199 个子站代号 → 逐个抓子站首页 → 判活 → 抽站名 →
  从首页把「角色 / 语音 / 剧情 / 档案 图鉴」这类目录页挑出来存好。
  第二相（episode / story 深挖）留给语料模式去消费，本脚本不碰正文。

取页通道（实测三种走法的差别，2026-10-21 00:30）：
  ① 走代理                 → 567 / 7.3KB 空壳
  ② 直连但不带 Referer     → 567 / 7.3KB 空壳
  ③ 直连 + Referer + 中文  → 200 / 1.41MB 全量
用法：python anime_diffuse.py [--limit N]

🔴 待办（用户 00:33：「最后合并就行了」）：
   本脚本只是**临时单独起的扩散线**，跑完这一轮就并回主爬虫，不做长期两个脚本。
   合并方式（不重写，只搬）：
     ① `_hidden_startupinfo()` / `fetch_sibling()` 已有同款在 resource_finder.py（第 887 行起），
        本文件的 `get()` 等价于它的 `_curl_get` + `-L` + Referer，合并时直接去掉重复实现；
     ② `harvest()` 搬进 resource_finder.py，作为站群目录页的链接抽取器；
     ③ 入口改成子命令 `python resource_finder.py diffuse`，与现有 `corpus` 模式并列
        （同一文件、不同 mode，跟 2026-10-20 把 corpus_crawler.py 并回来的做法一致）。
   现在不并：它正在跑 1,191 个子站，动主文件会打断在跑的这一轮。
"""
import json
import random
import re
import subprocess
import headless
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
SIB_FILE = HERE / "wiki_siblings.txt"
OUT_DIR = Path("G:/_train_corpus_raw")
STATE = OUT_DIR / "anime_diffuse_state.json"
OUTPUT = OUT_DIR / "anime_sites.json"
LOG = OUT_DIR / "anime_diffuse.log"

BASE = "https://wiki.biligame.com/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

SHELL_BYTES = 20_000        # 567 空壳 7.3KB；真首页一般 100KB+ ⇒ 20KB 是安全线
DELAY = (2.5, 4.0)          # 站点已同时被两个语料爬虫在打，这里必须是慢速客
BACKOFF_MAX = 60.0

# 目录页分桶关键词（中文优先，二次元 wiki 的栏目名基本就这几个）
BUCKETS = {
    "角色": ["角色", "人物", "干员", "舰娘", "从者", "英灵", "character", "角色图鉴"],
    "语音": ["语音", "台词", "配音", "voice"],
    "剧情": ["剧情", "故事", "章节", "主线", "活动", "story", "episode"],
    "档案": ["档案", "资料", "图鉴", "数据", "属性"],
}


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    try:
        print(line, flush=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _hidden_startupinfo():
    """Windows 隐藏控制台窗口（照搬主爬虫 resource_finder.py 的现成写法，别自己另搞一套）。

    curl 是控制台程序。父进程是 pythonw（本身没有控制台）时，Windows 会**新建一个控制台**
    来跑它 ⇒ 每抓一个站闪一个黑框，1,199 个站就是 1,199 次弹窗。
    三件套缺一不可：
      · STARTUPINFO + dwFlags |= STARTF_USESHOWWINDOW + wShowWindow = SW_HIDE
        （漏掉 dwFlags 那行，wShowWindow 直接被忽略 —— 最容易错的一行）
      · CREATE_NO_WINDOW（不分配控制台）
      · stdin=DEVNULL（不继承句柄，逼不出控制台）
    ⚠️ 每次调用都要新建实例：Python 3.7+ 会改掉传入对象的内部状态，复用第二次就失效。
    """
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def get(url, timeout=45):
    """直连取页。返回 (html, size, final_url)；被风控/失败返回 (None, size, fin)。

    ⚠️ 必须带 -L：子站根路径 301 → /<sub>/首页，不跟跳转拿到的是 0 字节空体
    （实测 00:26 五个站全判"失败 size=0"，加上 -L 立刻 200 / 619KB）。"""
    try:
        p = headless.run(
            ["curl", "-sL", "--noproxy", "*", "--max-time", str(timeout),
             "-A", UA,
             "-H", "Referer: " + BASE,
             "-H", "Accept-Language: zh-CN,zh;q=0.9",
             "-w", "\n__U__%{url_effective}", url],
            capture_output=True, timeout=timeout + 15,
            stdin=subprocess.DEVNULL,
            startupinfo=_hidden_startupinfo(),
            creationflags=subprocess.CREATE_NO_WINDOW)
        body = p.stdout
    except Exception as e:
        log("  异常 %s %s" % (type(e).__name__, str(e)[:60]))
        return None, 0, ""
    fin = ""
    if b"__U__" in body:
        body, meta = body.rsplit(b"\n__U__", 1)
        fin = meta.decode("utf-8", "replace").strip()
    if len(body) < SHELL_BYTES:
        return None, len(body), fin
    return body.decode("utf-8", "replace"), len(body), fin


def site_name(html, sub):
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    if not m:
        return sub
    t = re.sub(r"\s+", " ", m.group(1)).strip()
    return t.replace("_BWIKI_哔哩哔哩", "").replace("WIKI_BWIKI_哔哩哔哩", "").strip() or sub


def harvest(html, sub):
    """从首页抽同子站目录页，按 BUCKETS 分桶。只看 /<sub>/ 前缀，不看跨站。"""
    links, seen = {k: [] for k in BUCKETS}, set()
    for u in re.findall(r'<a[^>]+href="([^"]+)"', html):
        if not u or u.startswith(("#", "javascript:", "mailto:")):
            continue
        u = urllib.parse.urljoin(BASE + sub + "/", u)
        if not u.startswith(BASE + sub + "/"):
            continue
        path = urllib.parse.unquote(urllib.parse.urlparse(u).path)
        if re.search(r"\.(png|jpg|jpeg|gif|svg|css|js|ico)$", path, re.I):
            continue
        if u in seen:
            continue
        seen.add(u)
        low = path.lower()
        for name, kws in BUCKETS.items():
            if any(k.lower() in low for k in kws):
                links[name].append(u)
                break
    return {k: v[:40] for k, v in links.items() if v}


def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done": {}, "blocked": 0}


def save_state(st):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE)


def main():
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    subs = [x.strip() for x in SIB_FILE.read_text(encoding="utf-8").splitlines() if x.strip()]
    st = load_state()
    todo = [s for s in subs if s not in st["done"]]
    if limit:
        todo = todo[:limit]
    log("站群盘点开始：白名单 %d 个子站，已完成 %d，本轮待抓 %d"
        % (len(subs), len(st["done"]), len(todo)))
    throttle = random.uniform(*DELAY)
    for i, sub in enumerate(todo, 1):
        url = BASE + sub + "/"
        html, size, fin = get(url)
        if not html:
            st["blocked"] += 1
            throttle = min(BACKOFF_MAX, throttle * 1.8)
            st["done"][sub] = {"status": "blocked", "size": size}
            log("(%d/%d) ✗ %s 风控/失败 size=%d，退避 → %.1fs"
                % (i, len(todo), sub, size, throttle))
        elif fin.startswith(BASE + "wiki/") or fin.rstrip("/") == BASE.rstrip("/"):
            # 子站不存在时 301 回主站首页（1.37MB 那页，标题"bilibili游戏中心 - WIKI"）
            # ⇒ 这类是空壳条目，别记成活站，否则 1,199 里一半是假货
            st["done"][sub] = {"status": "dead", "size": size, "final": fin}
            log("(%d/%d) - %s 已合并/不存在（跳回主站首页）" % (i, len(todo), sub))
        else:
            st["blocked"] = 0
            throttle = random.uniform(*DELAY)
            name = site_name(html, sub)
            pages = harvest(html, sub)
            st["done"][sub] = {"status": "alive", "size": size,
                               "name": name, "pages": pages}
            log("(%d/%d) ✓ %s「%s」%dKB 目录页 %d 个（%s）"
                % (i, len(todo), sub, name, size // 1024,
                   sum(len(v) for v in pages.values()),
                   "/".join("%s%d" % (k, len(v)) for k, v in pages.items()) or "-"))
        if i % 20 == 0:
            save_state(st)
        time.sleep(random.uniform(throttle * 0.85, throttle * 1.2))
    save_state(st)

    alive = [k for k, v in st["done"].items() if v.get("status") == "alive"]
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total": len(st["done"]),
        "alive": len(alive),
        "sites": {k: v for k, v in st["done"].items() if v.get("status") == "alive"},
    }
    tmp = OUTPUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(OUTPUT)
    log("本轮结束：累计盘点 %d 个，活站 %d → %s" % (len(st["done"]), len(alive), OUTPUT))


if __name__ == "__main__":
    main()
