#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""anyfetch —— 「爬虫叠加解析」的通用抗噪获取层（纯直连，零代理）

主人 2026-11-05 的判断
----------------------
    污染大部分情况下是「获取资源程度不够强」，解析能解析污染，
    就跟大模型的噪音一样。理论上这两层足够强，只要有网就能爬取世界。

把这句话落成工程命题：**墙不是一堵墙，是往信道里掺的噪音。**
噪声分四类，每一类都有对应的「解析」手段，没有一类需要代理：

| 噪音类型 | 表象 | 解析手段（本模块） |
|---|---|---|
| DNS 污染 | 域名解析到假 IP → 000 | DoH 拿真实 IP + curl --resolve |
| 边缘节点丢包 | 同一请求有的 IP 返回半个 JSON / HTML 壳 | 多 IP 并发 + 内容完整性校验，取有效解 |
| 参数/协议错配 | HTTP 400「Unknown query parameter」 | 读懂协议再构造，不把 400 当网络故障 |
| 主域协议层断链 | 主域 SNI/Host 被识别 → RST/403 | **换入口**：同一份数据在别处有副本
                                          （归档 API / oEmbed / 元数据中继 / 镜像） |

🔴 2026-11-06 修正定性：上表原写「主域物理断链」，是**错的**。
   同一个 Cloudflare 边缘 IP，SNI=x.com 被 RST、SNI=www.cloudflare.com
   握手 OK + HTTP 200 ⇒ 封锁在**协议层的明文语义识别**，不是物理断链。
   但伪装 SNI + Host: x.com 会被 CDN 403（SNI/Host 一致性校验），
   所以握手通 ≠ 拿得到内容：是层层解，不是一伪装全通。
   判别工具见 `snifftest.py`。**取样不足就下「物理断链」结论＝噪声。**

关键在最后一行：**「连不上那个域」不等于「拿不到那份数据」**。
Reddit 主域不通，但全量归档在 arctic-shift 上；YouTube/X 主域不通，
但元数据在 microlink / noembed 上。代理是换通道，换入口是换信息源——
后者不需要任何中间人，也不受单点封锁影响。

设计：不做「失败再试下一个」的串行降级（慢且会把截断当成功），
而是**一次性并发所有候选通道，对每个返回打信号质量分，取最高分**。
这和大模型从噪音里投票还原信号是同一个逻辑。

用法
----
    from anyfetch import get, get_json
    r = get("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    print(r.source, r.quality, r.title, r.json)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import netpolicy  # noqa: E402   （隐藏窗口三件套）
import direct_ip  # noqa: E402   （DoH 真实 IP / 纯直连环境）
from skynet import dns as _dns  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 「同一份数据的另一个入口」：都是公开服务，纯直连实测可达（2026-11-05）
RELAYS = [
    ("microlink", "https://api.microlink.io/?url={u}"),
    ("noembed", "https://noembed.com/embed?url={u}"),
]

TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")


@dataclass
class Result:
    ok: bool
    source: str = ""          # 哪条通道赢的：direct / doh:<ip> / relay:<name>
    status: str = ""
    body: bytes = b""
    quality: float = 0.0
    title: str = ""
    json: object = None
    diag: str = ""
    tried: list = field(default_factory=list)


def _env():
    """清掉全部 *_proxy —— 纯直连纪律，绝不偷偷用本机端口。"""
    return direct_ip._clean_env()


def _curl(url, host=None, ip=None, timeout=15):
    """单次纯直连 GET。body 写临时文件、stdout 只留状态码，避免截断误判。"""
    fd, tmp = tempfile.mkstemp(prefix="nf_any_", suffix=".bin")
    os.close(fd)
    args = ["curl", "-s", "--noproxy", "*", "-A", UA, "-L",
            "--max-time", str(timeout), "-o", tmp, "-w", "%{http_code}"]
    if host and ip:
        args += ["--resolve", "%s:443:%s" % (host, ip)]
    args.append(url)
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout + 5,
                           env=_env(), stdin=subprocess.DEVNULL,
                           startupinfo=netpolicy._hidden_startupinfo(),
                           creationflags=subprocess.CREATE_NO_WINDOW)
        code = (r.stdout or b"").strip().decode("ascii", "ignore")
        body = open(tmp, "rb").read() if os.path.exists(tmp) else b""
        return code, body
    except Exception as e:
        return "ERR:" + type(e).__name__, b""
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _score(code, body):
    """信号质量：能不能解析出结构，比 HTTP 码重要得多。

    HTTP 200 + 半个 JSON = 噪音；HTTP 200 + 完整可解析结构 = 信号。
    """
    if not code.isdigit() or not body:
        return 0.0, None, ""
    c = int(code)
    if not (200 <= c < 400):
        return 0.0, None, ""
    s = 0.5                                   # 有 2xx/3xx + 字节
    j = None
    try:
        j = json.loads(body)
        s = 3.0 if isinstance(j, dict) else 2.5
        # 带业务数据字段的才算真解析出来（空壳 {'data':null} 不算）
        if isinstance(j, dict):
            # 🔴 中继的「解析后摘要」字段不能与原始结构等价计分：
            #    曾出现 HN 直连 36KB 完整 JSON(3.18) 输给 microlink 705B
            #    元数据摘要(4.0)，把原件丢掉只留二手标题 —— 正好违背「还原信号」。
            #    原始结构化数据(data/results/items/hits)给满分，title 类弱加分。
            if any(j.get(k) for k in ("data", "results", "items", "hits", "html")):
                s += 1.0
            elif any(j.get(k) for k in ("title", "description", "author")):
                s += 0.2
            if j.get("error"):
                s -= 4.0                       # {"error":...} 本质是失败，不能算信号
            if j.get("status") == "fail":
                s -= 4.0
    except Exception:
        pass
    t = ""
    m = TITLE_RE.search(body.decode("utf-8", "ignore")[:200000])
    if m:
        t = re.sub(r"\s+", " ", m.group(1)).strip()[:200]
        s += 0.3
    s += min(1.0, len(body) / 200000.0)       # 字节量是弱信号，只加分不主导
    return s, j, t


def _channel_direct(url, timeout):
    code, body = _curl(url, timeout=timeout)
    s, j, t = _score(code, body)
    return Result(s > 0, "direct", code, body, s, t, j, "直连", [])


def _channel_doh(host, path, url, ip, timeout):
    code, body = _curl(url, host=host, ip=ip, timeout=timeout)
    s, j, t = _score(code, body)
    return Result(s > 0, "doh:%s" % ip, code, body, s, t, j,
                  "DoH 真实 IP %s" % ip, [])


def _channel_relay(name, tmpl, url, timeout):
    q = urllib.parse.quote(url, safe="")
    rurl = tmpl.format(u=q)
    host = urllib.parse.urlsplit(rurl).hostname
    ips = []
    try:
        ips = [x for x in _dns.resolve(host, timeout=5) if x][:1]
    except Exception:
        pass
    code, body = _curl(rurl, host=host, ip=ips[0] if ips else None,
                       timeout=timeout)
    s, j, t = _score(code, body)
    # 中继返回的是「解析后的元数据」，不是原页面，标注来源避免误当原站 HTML
    return Result(s > 0, "relay:%s" % name, code, body, s, t, j,
                  "元数据中继 %s" % name, [])


def get(url, timeout=15, allow_relay=True, extra_paths=None):
    """并发所有纯直连通道，返回信号质量最高的那个结果。

    extra_paths: 可选，同一份数据的其他入口（如 Arctic Shift 归档 API），
                 格式为 [(label, full_url), ...]，会一起并发抢答。
    """
    u = urllib.parse.urlsplit(url)
    host = u.hostname or ""
    path = u.path + (("?" + u.query) if u.query else "")

    jobs = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        jobs.append(("direct", ex.submit(_channel_direct, url, timeout)))
        try:
            for ip in [x for x in _dns.resolve(host, timeout=5) if x][:3]:
                jobs.append(("doh:%s" % ip,
                             ex.submit(_channel_doh, host, path, url, ip, timeout)))
        except Exception:
            pass
        if allow_relay:
            for name, tmpl in RELAYS:
                jobs.append(("relay:%s" % name,
                             ex.submit(_channel_relay, name, tmpl, url, timeout)))
        for label, full in (extra_paths or []):
            h = urllib.parse.urlsplit(full).hostname
            jobs.append((label, ex.submit(_channel_relay, label,
                                         full.replace("{u}", "{u}"),
                                         url, timeout)))

        results = []
        for label, f in jobs:
            try:
                r = f.result()
            except Exception as e:
                r = Result(False, label, diag=type(e).__name__)
            results.append(r)

    best = max(results, key=lambda r: r.quality)
    if not best.ok:
        best = Result(False, source="", diag="所有纯直连通道均未解析出有效信号")
    best.tried = [("%s" % r.source, r.status, len(r.body), round(r.quality, 2))
                  for r in results]
    return best


def get_json(url, **kw):
    return get(url, **kw).json


def probe(urls, timeout=12):
    """批量探测：给一批 URL，返回每个最好的纯直连解析结果。"""
    out = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(get, u, timeout): u for u in urls}
        for f in as_completed(futs):
            u = futs[f]
            try:
                out[u] = f.result()
            except Exception as e:
                out[u] = Result(False, diag=type(e).__name__)
    return out


if __name__ == "__main__":
    targets = sys.argv[1:] or [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://x.com/elonmusk/status/1587846802346147840",
        "https://www.reddit.com/r/python/top.json?limit=5",
        "https://hn.algolia.com/api/v1/search?query=rust&tags=story",
    ]
    print("anyfetch 纯直连抗噪获取（零代理）\n" + "=" * 64)
    for u in targets:
        t0 = time.time()
        r = get(u)
        print("\n%s" % u)
        print("  结果: %s  来源=%s  HTTP=%s  %d bytes  质量=%.2f  %.1fs"
              % ("✓" if r.ok else "✗", r.source or "-", r.status,
                 len(r.body), r.quality, time.time() - t0))
        if r.title:
            print("  标题:", r.title[:120])
        if isinstance(r.json, dict):
            d = r.json.get("data")
            if isinstance(d, dict):
                print("  字段:", list(d.keys())[:12])
            elif isinstance(d, list):
                print("  条数:", len(d))
            else:
                print("  字段:", list(r.json.keys())[:12])
        print("  通道:", r.tried)
