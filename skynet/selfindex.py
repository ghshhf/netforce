"""天网 · 自建发现层（selfindex）
====================================
不依赖搜索引擎的资源发现 —— 网络本身有三条公开数据源，都不经过任何搜索公司：

  1. 证书透明度日志（CT Log，crt.sh / certspotter）
     浏览器安全机制的副产物：每张 HTTPS 证书签发都被公开记录（公开审计账本）。
     问它就能拿到海量域名，包括搜索引擎没收录的新站、子站。
     —— 这是网络基础设施自己吐出来的数据，不是任何一家搜索公司的索引。

  2. Common Crawl 全网快照索引（index.commoncrawl.org）
     非营利组织每月爬全网存档，URL 索引完全公开。问它「某域名有哪些 URL」，
     绕开所有商业搜索引擎。

  3. 链接图扩散（graphdiffuse.py）
     每个网页都有外链，顺着外链 BFS 就是爬网络本身的图结构，不经过任何搜索。

搜索引擎在这套体系里降级为「冷启动种子源」：只负责给出第一批锚点，
之后一切靠 CT + CC + 图扩散自己长 —— 供给不再受限于别人索引的那一小部分。

设计约束（与项目铁律一致）：
  · 全程走 skynet.net（继承代理 / 限速 / 退避 / 隐藏窗口三件套）
  · 慢源（crt.sh）带重试 + 退避 + 结果缓存，不重复砸同一个慢查询
  · 只读查询，不构造任何写操作路径
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from pathlib import Path

from . import net

# ---------------------------------------------------------------- 常量

# CT 日志查询端点（crt.sh 主站 + certspotter 备用）。
# crt.sh 是 CT 日志的公开 SQL 前端，响应偏慢、偶发 502 ⇒ 必须重试 + 多源。
CT_HOST = "https://crt.sh"
CERTSPOTTER = "https://api.certspotter.com/v1/issuances"

# Common Crawl 索引端点
CC_COLLINFO = "https://index.commoncrawl.org/collinfo.json"
CC_INDEX = "https://index.commoncrawl.org/{crawl}-index"

# 缓存文件：CT/CC 都是慢源，结果按查询指纹缓存，TTL 到期才重查。
# 域名 / 证书 / 快照变化慢，7 天足够；缓存丢了只是重查一次，不丢资源。
_CACHE_PATH = Path(__file__).resolve().parent.parent / "_selfindex_cache.json"
_CACHE_TTL = 7 * 24 * 3600

# name_value 里要过滤的：IP 地址、通配符、空串
_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_BAD_NAME = re.compile(r"[\s,;:>]")     # 证书名里不该有这些，有了就是脏数据

# ---------------------------------------------------------------- 缓存


def _load_cache() -> dict:
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(c: dict) -> None:
    try:
        tmp = _CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
    except Exception:
        pass


def _cached(key: str, ttl: float = _CACHE_TTL):
    """命中且未过期 ⇒ 返回结果；否则返回 None。"""
    c = _load_cache()
    rec = c.get(key)
    if rec and time.time() - rec.get("ts", 0) < ttl:
        return rec.get("v")
    return None


def _cache_put(key: str, v) -> None:
    c = _load_cache()
    c[key] = {"ts": time.time(), "v": v}
    _save_cache(c)


# ---------------------------------------------------------------- 域名清洗


def clean_names(raw: str) -> list[str]:
    """把证书的 name_value（可能含通配符 / 多行 / 大小写混杂）洗成干净域名集合。

    name_value 真实形态（实测 crt.sh）：`*.biligame.com\\nbiligame.com\\nabc.biligame.com`
    —— 一个证书可以同时覆盖通配符、裸域和具体子域，要拆开逐条判。
    实测还有邮箱混进来（tongji.edu.cn 的证书里有 `10zhouyusheng@tongji.edu.cn`），
    必须滤掉 @。
    """
    out = []
    for name in (raw or "").split("\n"):
        n = name.strip().lower().lstrip("*.").strip(".")
        if not n or _IP_RE.match(n) or _BAD_NAME.search(n):
            continue
        # 只保留像域名的（至少一个点，且无路径/端口/邮箱）
        if "." not in n or "/" in n or ":" in n or " " in n or "@" in n:
            continue
        if len(n) > 255:
            continue
        out.append(n)
    return list(dict.fromkeys(out))


def host_of(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")


# ---------------------------------------------------------------- ① 证书透明度日志


def _ct_query(params: str, timeout: float = 30.0, budget: float = 50.0) -> list:
    """查 crt.sh 的 SQL JSON 端。慢源：单次 budget 放宽到 50s，再交给缓存兜底。"""
    url = CT_HOST + "/?" + params + "&output=json"
    r = net.fetch_text(url, timeout=timeout, budget=budget, gap=0.0, retry=2)
    if not r.ok or not r.text.strip():
        return []
    try:
        return json.loads(r.text)
    except Exception:
        return []


def _certspotter(domain: str, timeout: float = 20.0) -> list[str]:
    """certspotter 备用源：返回 dns_names 展开的域名。"""
    u = CERTSPOTTER + "?" + urllib.parse.urlencode(
        {"domain": domain, "include_subdomains": "true", "expand": "dns_names"})
    r = net.fetch_text(u, timeout=timeout, budget=30, gap=0.0, retry=1)
    if not r.ok or not r.text.strip():
        return []
    try:
        data = json.loads(r.text)
    except Exception:
        return []
    names = []
    for it in data:
        names.extend(it.get("dns_names") or [])
    return clean_names("\n".join(names))


def ct_subdomains(domain: str, limit: int = 300, timeout: float = 30.0) -> list[str]:
    """挖某域名的所有子域名（含通配符证书展开）。—— 定向站群扩散的完美补充。

    现有 SIBLING_DOMS 靠爬首页拿兄弟站（要能爬到首页）；CT 直接给全量子域，
    包括那些首页爬不到、搜索引擎没收录的冷门子站。
    """
    domain = (domain or "").strip().lower().removeprefix("www.")
    if not domain or "." not in domain:
        return []
    key = f"ct:sub:{domain}"
    hit = _cached(key)
    if hit is not None:
        return hit[:limit]

    # %25 是 URL 编码的 %；% 前缀匹配 = 挖所有子域
    recs = _ct_query(f"q=%25.{urllib.parse.quote(domain, safe='')}", timeout=timeout)
    names: list[str] = []
    for rec in recs:
        names.extend(clean_names(rec.get("name_value", "")))
    # 备用源补一轮（crt.sh 偶发空/慢时不至于空手）
    if not names:
        names = _certspotter(domain, timeout)
    # 只保留该域名的子域（CT 按 % 前缀查基本不会串，但防御一下）
    names = [n for n in names if n == domain or n.endswith("." + domain)]
    names = list(dict.fromkeys(names))
    _cache_put(key, names)
    return names[:limit]


def ct_keyword(keyword: str, limit: int = 300, timeout: float = 30.0) -> list[str]:
    """按关键词挖域名（例如 'dataset'、'wiki'、'download'、'awesome'）。

    CT 的 q 参数对 name_value 做子串匹配 —— 证书里带某个词的域名，多半是
    该词相关的服务（dataset.example.com、wiki.xxx.org）。这是关键词→域名的
    反向通道，完全不经过搜索引擎。
    """
    keyword = (keyword or "").strip().lower()
    if not keyword:
        return []
    key = f"ct:kw:{keyword}"
    hit = _cached(key)
    if hit is not None:
        return hit[:limit]

    recs = _ct_query(f"q={urllib.parse.quote(keyword, safe='')}", timeout=timeout)
    names: list[str] = []
    for rec in recs:
        names.extend(clean_names(rec.get("name_value", "")))
    # 只保留「真正包含关键词」的域名（子串匹配，避免 q 的模糊匹配带进无关项）
    names = [n for n in names if keyword in n]
    names = list(dict.fromkeys(names))
    _cache_put(key, names)
    return names[:limit]


def ct_suffix(suffix: str, limit: int = 500, timeout: float = 40.0) -> list[str]:
    """CT 按后缀挖域名（例如 `edu.cn`、`ac.cn`、`gov.cn`、`org`）。

    实测挖 `%.edu.cn` 一次出 **1836 个**真实域名 —— 这是「自建领域清单」的通用入口，
    不经过搜索引擎、不受收录限制。Common Crawl 的 `*.后缀` 通配查询会因结果量过大
    被后端 504（实测），所以按后缀挖域名改走 CT 日志这条腿。
    """
    suffix = (suffix or "").strip().lower().lstrip("*.")
    if not suffix or "." not in suffix:
        return []
    key = f"ct:sfx:{suffix}"
    hit = _cached(key)
    if hit is not None:
        return hit[:limit]

    recs = _ct_query(f"q=%25.{urllib.parse.quote(suffix, safe='')}", timeout=timeout)
    names: list[str] = []
    for rec in recs:
        names.extend(clean_names(rec.get("name_value", "")))
    names = [n for n in names if n.endswith("." + suffix)]
    names = list(dict.fromkeys(names))
    _cache_put(key, names)
    return names[:limit]


# ---------------------------------------------------------------- ② Common Crawl 索引


def cc_crawl_ids(timeout: float = 20.0) -> list[str]:
    """取 Common Crawl 有效 crawl id（最新在前）。"""
    key = "cc:collinfo"
    hit = _cached(key, ttl=24 * 3600)      # 快照列表一天查一次就够
    if hit is not None:
        return hit
    r = net.fetch_text(CC_COLLINFO, timeout=timeout, budget=30, gap=0.0, retry=1)
    ids = []
    if r.ok:
        try:
            ids = [c["id"] for c in json.loads(r.text) if c.get("id")]
        except Exception:
            pass
    _cache_put(key, ids)
    return ids


def _cc_query(crawl: str, pattern: str, limit: int = 200,
              timeout: float = 25.0, match_type: str = "domain") -> list[str]:
    """问 CC 索引，返回 URL 列表。CC 返回 NDJSON（每行一条），不是标准 JSON 数组。

    match_type：CC 的 url 参数是前缀匹配，**必须**配 matchType 才稳：
      · `matchType=domain` 匹配「该域名及其全部子域」（实测缺了它会 504）
      · `matchType=exact` 只匹配该 URL 本身
    实测：`url=tsinghua.edu.cn` 不带 matchType → 504；带 `matchType=domain` → 正常返回。
    """
    params = {"url": pattern, "output": "json", "limit": str(limit)}
    if match_type:
        params["matchType"] = match_type
    u = CC_INDEX.format(crawl=crawl) + "?" + urllib.parse.urlencode(params)
    r = net.fetch_text(u, timeout=timeout, budget=40, gap=0.0, retry=1)
    if not r.ok or not r.text.strip():
        return []
    urls = []
    for line in r.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if rec.get("url"):
                urls.append(rec["url"])
        except Exception:
            continue
    return urls


def cc_urls(pattern: str, crawl: str = "", limit: int = 200,
            timeout: float = 25.0, match_type: str = "domain") -> list[str]:
    """问 CC 索引：pattern 匹配的 URL 列表。

    pattern 用 `*.domain.com`（全部子域）或具体域名；match_type 默认 domain。
    这是「网络本身的存档」——快照里真实存在过的 URL，不是搜索引擎的收录判断。
    """
    pattern = (pattern or "").strip()
    if not pattern:
        return []
    if not crawl:
        ids = cc_crawl_ids(timeout=timeout)
        crawl = ids[0] if ids else ""
    if not crawl:
        return []
    key = f"cc:urls:{crawl}:{pattern}:{match_type}"
    hit = _cached(key, ttl=30 * 24 * 3600)    # 快照是历史存档，永不更新 ⇒ 缓存可久
    if hit is not None:
        return hit[:limit]
    urls = _cc_query(crawl, pattern, limit, timeout, match_type)
    _cache_put(key, urls)
    return urls[:limit]


def cc_hosts(domain: str, crawl: str = "", limit: int = 300,
             timeout: float = 25.0) -> list[str]:
    """某域名（含子域）在 CC 快照里出现过的所有主机。"""
    domain = (domain or "").strip().lower().removeprefix("www.")
    if not domain:
        return []
    urls = cc_urls(domain, crawl=crawl, limit=max(limit * 2, 500),
                   timeout=timeout, match_type="domain")
    hosts = list(dict.fromkeys(host_of(u) for u in urls if host_of(u)))
    return hosts[:limit]


def cc_domains(suffix: str, crawl: str = "", limit: int = 500,
               timeout: float = 40.0) -> list[str]:
    """按后缀挖域名（例如 `edu.cn`、`ac.cn`、`gov.cn`、`org`）。

    🔴 注意：CC 的 `*.后缀` 通配查询会因结果量过大被后端 504（实测），
    所以这里**改走 CT 日志**（`ct_suffix`），效果相同且稳定（实测 edu.cn 出 1836 个）。
    保留本函数名只为调用方便，本质是 CT 这条腿。
    """
    return ct_suffix(suffix, limit=limit, timeout=timeout)


# ---------------------------------------------------------------- ③ 互联网档案馆（Wayback）

# Wayback CDX API：互联网档案馆从 1996 年起备份全网网页，CDX 是它的公开索引，
# 直接问它「你备份过哪些 URL」—— 这是最典型的「公开宣告域名」平台，不经过搜索引擎。
# ⚠️ 本机实测：直连被墙（status=0），必须走代理；且 archive.org 偶发 Temporarily Offline，
#    所以要带重试 + 缓存，别一两次失败就判死。
ARCHIVE_CDX = "https://web.archive.org/cdx/search/cdx"


def archive_urls(domain: str, limit: int = 300, timeout: float = 35.0) -> list[str]:
    """互联网档案馆（Wayback Machine）：某域名被存档过的所有 URL（collapse 去重）。"""
    domain = (domain or "").strip().lower().removeprefix("www.")
    if not domain:
        return []
    key = f"ia:urls:{domain}"
    hit = _cached(key, ttl=30 * 24 * 3600)
    if hit is not None:
        return hit[:limit]

    u = ARCHIVE_CDX + "?" + urllib.parse.urlencode(
        {"url": domain, "matchType": "domain", "output": "json",
         "collapse": "urlkey", "fl": "original", "limit": str(limit)})
    r = net.fetch_text(u, timeout=timeout, budget=50, gap=0.0, retry=2)
    if not r.ok or not r.text.strip():
        return []
    try:
        rows = json.loads(r.text)
    except Exception:
        return []
    # 第一行是表头（fl=original 时是 ["original"]），后面才是数据行
    urls = []
    for row in rows[1:]:
        if isinstance(row, list) and row and str(row[0]).startswith("http"):
            urls.append(row[0])
    urls = list(dict.fromkeys(urls))
    _cache_put(key, urls)
    return urls[:limit]


def archive_hosts(domain: str, limit: int = 300, timeout: float = 35.0) -> list[str]:
    """某域名（含子域）被互联网档案馆备份过的所有主机。"""
    domain = (domain or "").strip().lower().removeprefix("www.")
    if not domain:
        return []
    urls = archive_urls(domain, limit=max(limit * 2, 600), timeout=timeout)
    hosts = list(dict.fromkeys(host_of(u) for u in urls if host_of(u)))
    return hosts[:limit]


# ---------------------------------------------------------------- 聚合入口


def discover(domain: str = "", keyword: str = "", suffix: str = "",
             limit: int = 400, timeout: float = 30.0) -> dict:
    """一次把三路自建发现都跑一遍，返回结构化结果（供泵直接消费）。"""
    out = {"ct_subdomains": [], "ct_keyword": [], "ct_suffix": [], "cc_hosts": []}
    if domain:
        out["ct_subdomains"] = ct_subdomains(domain, limit=limit, timeout=timeout)
        out["cc_hosts"] = cc_hosts(domain, limit=limit, timeout=timeout)
    if keyword:
        out["ct_keyword"] = ct_keyword(keyword, limit=limit, timeout=timeout)
    if suffix:
        out["ct_suffix"] = ct_suffix(suffix, limit=limit, timeout=timeout)
    return out


# ---------------------------------------------------------------- 自测


def _selftest():
    ok = 0
    fail = 0

    def check(cond, label):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [ok] {label}")
        else:
            fail += 1
            print(f"  [FAIL] {label}")

    # 1. 域名清洗（离线，不联网，必然通过）
    names = clean_names("*.biligame.com\nbiligame.com\nYYS.biligame.com\n192.168.1.1\n10zhou@tongji.edu.cn\n")
    check("biligame.com" in names and "yys.biligame.com" in names, "通配符/裸域/子域正确拆解")
    check(all("192.168" not in n for n in names), "IP 地址被过滤")
    check(all("@" not in n for n in names), "邮箱地址被过滤")

    # 2. 缓存读写（离线）
    _cache_put("__test__", ["a.com", "b.com"])
    check(_cached("__test__") == ["a.com", "b.com"], "缓存写入/读取一致")

    # 3. 联网：CT 日志挖子域（crt.sh）
    subs = ct_subdomains("biligame.com", limit=50, timeout=30)
    print(f"      CT 子域命中 {len(subs)} 个：{subs[:5]}")
    check(isinstance(subs, list), "ct_subdomains 返回列表")

    # 4. 联网：CT 按后缀挖域名（edu.cn，量大稳定）
    doms = ct_suffix("edu.cn", limit=20, timeout=40)
    print(f"      CT 挖 edu.cn 域名 {len(doms)} 个：{doms[:5]}")
    check(isinstance(doms, list) and bool(doms), "ct_suffix 挖到域名")

    # 5. 联网：Common Crawl 快照列表
    ids = cc_crawl_ids()
    print(f"      CC 快照列表 {len(ids)} 个：{ids[:3]}")
    check(bool(ids), "cc_crawl_ids 拿到有效 crawl id")

    # 6. 联网：CC 具体域名（matchType=domain）
    if ids:
        hosts = cc_hosts("tsinghua.edu.cn", limit=10, timeout=30)
        print(f"      CC 挖 tsinghua.edu.cn 主机 {len(hosts)} 个：{hosts[:5]}")
        check(isinstance(hosts, list), "cc_hosts 返回列表")

    print(f"[selfindex] 自测：{ok} 通过 / {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
