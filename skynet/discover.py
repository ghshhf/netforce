"""天网 · 站点结构发现（discover）
==================================
搜索引擎告诉你「哪个站」，robots/sitemap 告诉你「这个站里有什么」。
强检索层必须两者都有 —— 只靠站外引擎，永远只能看到被收录的那一小部分。

能力：
    robots(base)    → 解析 /robots.txt：Sitemap 声明、Disallow 规则、Crawl-delay
    sitemaps(base)  → 递归取 sitemap.xml / sitemapindex，展开成 URL 列表（有上限）
    feed(base)      → 常见 RSS/Atom 入口探测
    structure(url)  → 一次拿全：robots + sitemap + 页面内部分类链接

设计取舍：
  · sitemapindex 会递归，但**限深度与总量**，避免巨型站把内存打满
  · 只读取，不构造任何会触发写操作的路径
  · 全程走 skynet.net，天然继承代理、限速、退避、隐藏窗口
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

from . import net

LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S | re.I)
SITEMAP_DECL = re.compile(r"(?im)^\s*sitemap:\s*(\S+)\s*$")
DISALLOW = re.compile(r"(?im)^\s*disallow:\s*(\S+)\s*$")
CRAWL_DELAY = re.compile(r"(?im)^\s*crawl-delay:\s*([\d.]+)\s*$")

FEED_PATHS = ["/feed", "/rss", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml"]


@dataclass
class Robots:
    base: str
    exists: bool = False
    sitemaps: list = field(default_factory=list)
    disallow: list = field(default_factory=list)
    crawl_delay: float = 0.0
    size: int = 0

    def allowed(self, path: str) -> bool:
        if not self.exists:
            return True
        for rule in self.disallow:
            if rule and rule != "/" and path.startswith(rule.rstrip("*")):
                return False
        return True

    def brief(self) -> str:
        return (f"{'✓' if self.exists else '✗'} robots {self.size}B "
                f"sitemap={len(self.sitemaps)} disallow={len(self.disallow)} "
                f"delay={self.crawl_delay or '-'}")


@dataclass
class Sitemap:
    urls: list = field(default_factory=list)
    files: int = 0
    truncated: bool = False

    def __len__(self):
        return len(self.urls)


def _root(base: str) -> str:
    p = urllib.parse.urlsplit(base if "//" in base else "https://" + base)
    return f"{p.scheme}://{p.netloc}"


def robots(base: str, timeout: float = 12.0) -> Robots:
    root = _root(base)
    r = net.fetch_text(root + "/robots.txt", timeout=timeout, budget=14, gap=0.4)
    rb = Robots(base=root, size=r.size)
    if not r.ok or r.size < 8:
        return rb
    rb.exists = True
    txt = r.text[:200_000]
    rb.sitemaps = [u for u in SITEMAP_DECL.findall(txt) if u.startswith("http")]
    rb.disallow = [u for u in DISALLOW.findall(txt) if u]
    m = CRAWL_DELAY.search(txt)
    if m:
        try:
            rb.crawl_delay = float(m.group(1))
        except ValueError:
            pass
    return rb


def _fetch_sitemap(url: str, out: list, depth: int, seen_files: set,
                   max_urls: int, max_files: int) -> int:
    """递归展开 sitemapindex。返回本文件贡献的 URL 数。"""
    if url in seen_files or len(seen_files) >= max_files or len(out) >= max_urls:
        return 0
    seen_files.add(url)
    r = net.fetch_text(url, timeout=20, budget=25, gap=0.4)
    if not r.ok:
        return 0
    locs = [x.strip() for x in LOC_RE.findall(r.text) if x.strip().startswith("http")]
    if not locs:
        return 0
    # sitemapindex 的 loc 指向子 sitemap；普通 sitemap 的 loc 就是页面
    is_index = "<sitemapindex" in r.text[:500].lower()
    added = 0
    if is_index and depth < 3:
        for sub in locs:
            if len(out) >= max_urls or len(seen_files) >= max_files:
                break
            _fetch_sitemap(sub, out, depth + 1, seen_files, max_urls, max_files)
    else:
        for u in locs:
            if len(out) >= max_urls:
                return 0
            out.append(u)
            added += 1
    return added


def sitemaps(base: str, max_urls: int = 3000, max_files: int = 12) -> Sitemap:
    """取站点的全部 sitemap 页面 URL。优先用 robots 里声明的入口，没有再猜常见路径。"""
    root = _root(base)
    sm = Sitemap()
    rb = robots(root)
    entries = rb.sitemaps or [root + "/sitemap.xml"]
    seen: set = set()
    for e in entries:
        _fetch_sitemap(e, sm.urls, 0, seen, max_urls, max_files)
        if len(sm.urls) >= max_urls:
            sm.truncated = True
            break
    sm.files = len(seen)
    # 去重保序
    sm.urls = list(dict.fromkeys(sm.urls))
    return sm


def feed(base: str) -> Optional[str]:
    """探测常见订阅入口，返回第一个可用的。"""
    root = _root(base)
    for p in FEED_PATHS:
        r = net.fetch_text(root + p, timeout=8, budget=10, gap=0.3)
        if r.ok and ("<rss" in r.text[:800].lower() or "<feed" in r.text[:800].lower()):
            return root + p
    return None


def structure(url: str, max_urls: int = 1500) -> dict:
    """一次拿全一个站的结构：robots 规则 + sitemap 样本 + feed。

    给检测层用：sitemap 里如果一堆 `.mp3`，那这站就是音频库；
    如果一堆 `/wiki/角色`，那它是 wiki 站。**看结构比看首页准得多。**
    """
    root = _root(url)
    rb = robots(root)
    sm = sitemaps(root, max_urls=max_urls)
    ext = {}
    for u in sm.urls[:800]:
        e = urllib.parse.urlsplit(u).path.rsplit(".", 1)
        if len(e) == 2 and len(e[1]) <= 5:
            ext[e[1].lower()] = ext.get(e[1].lower(), 0) + 1
    return {
        "root": root,
        "robots": rb.brief(),
        "crawl_delay": rb.crawl_delay,
        "sitemap_files": sm.files,
        "sitemap_urls": len(sm.urls),
        "truncated": sm.truncated,
        "ext": dict(sorted(ext.items(), key=lambda x: -x[1])[:8]),
        "sample": sm.urls[:5],
        "feed": feed(root),
    }
