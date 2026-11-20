"""天网 · 云厂商资产测绘层（cloud）
====================================
思路：现在绝大多数网站都部署在云厂商（阿里云 / 腾讯云 / AWS / Cloudflare / 华为云）
上。云厂商和资产测绘引擎早就把全网暴露的服务扫了个遍、公开可查 —— 这就是
「云厂商的公开账本」。直接读账本 = 一条搜索挖出这个云厂商名下的一大片网站，
完全不经过搜索引擎、也不爆破（爆破是「猜」，这是「抄已经扫好的答案」）。

核心数据源（全部免 key）：

  1. URLScan.io 搜索 API
     https://urlscan.io/api/v1/search/?q=...&size=100
     沙箱浏览器扫全网，每个页面结果记录了：
       · page.asnname  —— 托管它的云厂商 ASN（"ALIBABA-CN-NET - Hangzhou Alibaba"）
       · page.server   —— 服务器指纹（Tengine=阿里、nginx、cloudflare）
       · task.domain   —— 被扫的域名；page.domain —— 它联系的域名
       · page.ip       —— 它的 IP
     匿名额度：约 500 次搜索/天、30 次/分 ⇒ 必须限速 + 缓存，别打爆。

  2. Shodan InternetDB
     https://internetdb.shodan.io/{ip}
     按 IP 反查开放端口 + hostnames + cpe（技术栈）+ tags + vulns。
     这是「IP → 域名」的反向通道：拿到一个云 IP，反查它背后的 hostname。

设计约束（与项目铁律一致）：
  · 全程走 skynet.net（继承代理 / 限速 / 退避 / 隐藏窗口三件套）
  · 免 key 源必须节流（URLScan 匿名 30 次/分）—— 模块级节流 + 结果缓存
  · 只读查询，不构造任何写操作路径
"""
from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path

from . import net
from .selfindex import clean_names, _cached, _cache_put

# ---------------------------------------------------------------- 常量

URLSCAN_SEARCH = "https://urlscan.io/api/v1/search/"
INTERNETDB = "https://internetdb.shodan.io/{ip}"

# 缓存文件（与 selfindex 共用一份磁盘缓存，TTL 各自控制）
_CACHE_PATH = Path(__file__).resolve().parent.parent / "_cloud_cache.json"
_CACHE_TTL = 7 * 24 * 3600

# URLScan 匿名限速：30 次/分 ⇒ 两次调用至少间隔 2.2s（留余量）
_URLSCAN_MIN_GAP = 2.2
_last_urlscan = 0.0

# IP 地址（站点直接绑 IP 时，URLScan 的 domain 字段就是 IP，必须滤掉）
_IP_RE = __import__("re").compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _throttle_urlscan() -> None:
    """节流：保证两次 URLScan 调用间隔 ≥ _URLSCAN_MIN_GAP，不触发匿名限速。"""
    global _last_urlscan
    wait = _URLSCAN_MIN_GAP - (time.time() - _last_urlscan)
    if wait > 0:
        time.sleep(wait)
    _last_urlscan = time.time()


# ---------------------------------------------------------------- 缓存（云层专用，磁盘隔离）

def _load() -> dict:
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(c: dict) -> None:
    try:
        tmp = _CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
    except Exception:
        pass


def _cached2(key: str, ttl: float = _CACHE_TTL):
    c = _load()
    rec = c.get(key)
    if rec and time.time() - rec.get("ts", 0) < ttl:
        return rec.get("v")
    return None


def _cache_put2(key: str, v) -> None:
    c = _load()
    c[key] = {"ts": time.time(), "v": v}
    _save(c)


# ---------------------------------------------------------------- ① URLScan 搜索


def _urlscan(q: str, size: int = 200, timeout: float = 30.0) -> list[dict]:
    """URLScan 搜索底层。返回 results 列表（每条是一个扫描记录 dict）。"""
    _throttle_urlscan()
    u = URLSCAN_SEARCH + "?" + urllib.parse.urlencode({"q": q, "size": str(size)})
    r = net.fetch_text(u, timeout=timeout, budget=40, gap=0.0, retry=1)
    if not r.ok or not r.text.strip():
        return []
    try:
        data = json.loads(r.text)
        return data.get("results") or []
    except Exception:
        return []


def _domains_of(results: list[dict]) -> list[str]:
    """从 URLScan 扫描记录里抽取所有域名（被扫域 + apex 域 + 联系域）。

    ⚠️ 必须滤 IP：站点直接绑 IP 时，URLScan 的 domain 字段就是 IP（实测 asn:Tencent
    里混进了 43.156.0.130），不能当域名返回。
    """
    out: list[str] = []
    for rec in results:
        task = rec.get("task") or {}
        page = rec.get("page") or {}
        for name in (task.get("domain"), task.get("apexDomain"),
                     page.get("domain"), page.get("apexDomain")):
            if not name:
                continue
            n = str(name).strip().lower().rstrip(".")
            if _IP_RE.match(n):
                continue
            # 只保留像域名的（至少一个点，且无路径/端口/邮箱/空格）
            if "." not in n or "/" in n or ":" in n or " " in n or "@" in n:
                continue
            out.append(n)
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- 按云厂商反查


def cloud_asn_domains(asnname: str, limit: int = 300, timeout: float = 30.0) -> list[str]:
    """按云厂商 ASN 名反查域名 —— 「云厂商公开账本」的核心入口。

    asnname 是 ASN 描述里的子串，例如：
      · "Alibaba"    → 阿里云（ALIBABA-CN-NET - Hangzhou Alibaba）
      · "Tencent"    → 腾讯云
      · "Huawei"     → 华为云
      · "Cloudflare" → Cloudflare
      · "Amazon"     → AWS
      · "Microsoft"  → Azure
      · "Google"     → GCP
      · "DigitalOcean" / "Vercel" / "Netlify" / "GitHub"

    URLScan 的 page.asnname 用 Elasticsearch 查询字符串做子串匹配，
    值带空格用引号包起来。一条搜索 = 挖出该云厂商名下的一批真实网站。
    """
    asnname = (asnname or "").strip()
    if not asnname:
        return []
    key = f"cloud:asn:{asnname.lower()}"
    hit = _cached2(key)
    if hit is not None:
        return hit[:limit]
    q = f'page.asnname:"{asnname}"'
    results = _urlscan(q, size=max(limit, 200), timeout=timeout)
    doms = _domains_of(results)
    _cache_put2(key, doms)
    return doms[:limit]


def urlscan_subdomains(domain: str, limit: int = 300, timeout: float = 30.0) -> list[str]:
    """按域名挖子域（URLScan 层，比 CT 更全 —— 含纯 HTTP、没证书的站）。"""
    domain = (domain or "").strip().lower().removeprefix("www.")
    if not domain or "." not in domain:
        return []
    key = f"cloud:sub:{domain}"
    hit = _cached2(key)
    if hit is not None:
        return hit[:limit]
    results = _urlscan(f"page.domain:{domain}", size=max(limit, 200), timeout=timeout)
    doms = [d for d in _domains_of(results) if d == domain or d.endswith("." + domain)]
    _cache_put2(key, doms)
    return doms[:limit]


def urlscan_server_domains(server: str, limit: int = 300, timeout: float = 30.0) -> list[str]:
    """按服务器指纹反查域名（server 字段：Tengine=阿里、nginx、cloudflare、openresty…）。

    服务器指纹是「技术栈」级别的云厂商特征：Tengine 是阿里的开源 nginx 分支，
    能扫出一堆 Tengine 的基本都落在阿里云上。这是比 ASN 更细的一层。
    """
    server = (server or "").strip()
    if not server:
        return []
    key = f"cloud:srv:{server.lower()}"
    hit = _cached2(key)
    if hit is not None:
        return hit[:limit]
    results = _urlscan(f"page.server:{server}", size=max(limit, 200), timeout=timeout)
    doms = _domains_of(results)
    _cache_put2(key, doms)
    return doms[:limit]


# ---------------------------------------------------------------- ② Shodan InternetDB


def internetdb(ip: str, timeout: float = 20.0) -> dict:
    """按 IP 反查 Shodan InternetDB：开放端口 / hostnames / cpe / tags / vulns。

    这是「IP → 域名」的反向通道。返回原始 dict（hostnames 字段尤其有用）。
    """
    ip = (ip or "").strip()
    if not ip:
        return {}
    key = f"idb:{ip}"
    hit = _cached2(key, ttl=30 * 24 * 3600)   # 互联网测绘结果变化慢，缓存可久
    if hit is not None:
        return hit
    r = net.fetch_text(INTERNETDB.format(ip=ip), timeout=timeout, budget=30,
                       gap=0.0, retry=1)
    data = {}
    if r.ok and r.text.strip():
        try:
            data = json.loads(r.text)
        except Exception:
            data = {}
    _cache_put2(key, data)
    return data


def ip_hostnames(ip: str, timeout: float = 20.0) -> list[str]:
    """按 IP 反查它背后的 hostname（干净域名列表）。"""
    data = internetdb(ip, timeout=timeout)
    return clean_names("\n".join(data.get("hostnames") or []))


# ---------------------------------------------------------------- 聚合入口


def discover(asnname: str = "", domain: str = "", server: str = "",
             limit: int = 300, timeout: float = 30.0) -> dict:
    """一次把云厂商测绘三路都跑一遍，返回结构化结果。"""
    out = {"cloud_asn": [], "subdomains": [], "server": []}
    if asnname:
        out["cloud_asn"] = cloud_asn_domains(asnname, limit=limit, timeout=timeout)
    if domain:
        out["subdomains"] = urlscan_subdomains(domain, limit=limit, timeout=timeout)
    if server:
        out["server"] = urlscan_server_domains(server, limit=limit, timeout=timeout)
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

    # 1. 域名抽取（离线）
    recs = [{"task": {"domain": "a.example.com", "apexDomain": "example.com"},
             "page": {"domain": "cdn.example.com", "apexDomain": "example.com"}}]
    doms = _domains_of(recs)
    check("a.example.com" in doms and "cdn.example.com" in doms, "域名抽取正确")

    # 2. 联网：Shodan InternetDB（1.1.1.1 稳定）
    data = internetdb("1.1.1.1", timeout=20)
    print(f"      InternetDB 1.1.1.1 → hostnames={data.get('hostnames')}, cpe={data.get('cpes')}")
    check(isinstance(data, dict) and "ip" in data, "internetdb 反查成功")

    # 3. 联网：URLScan 按云厂商 ASN 反查（Alibaba）
    doms = cloud_asn_domains("Alibaba", limit=30, timeout=30)
    print(f"      cloud_asn_domains(Alibaba) → {len(doms)} 个域名：{doms[:6]}")
    check(isinstance(doms, list) and bool(doms), "URLScan 按云厂商反查成功")

    # 4. 联网：URLScan 按域名挖子域
    subs = urlscan_subdomains("alibaba.com", limit=30, timeout=30)
    print(f"      urlscan_subdomains(alibaba.com) → {len(subs)} 个：{subs[:6]}")
    check(isinstance(subs, list), "urlscan_subdomains 返回列表")

    print(f"[cloud] 自测：{ok} 通过 / {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
