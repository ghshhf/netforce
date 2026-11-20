# -*- coding: utf-8 -*-
"""netforce / egress_route.py —— 出口选路 + 使用记录（国内直连 / 国外加速）

分流原则，落到代码就是三句话：
    1. **有国内的走国内** —— 目标 IP 属于中国大陆分配 ⇒ 一律直连，绝不经加速层；
    2. **国外的才走加速** —— 只能走加速的才走，且加速失败要能退回直连；
    3. **留使用记录** —— 每个域走了哪条路、多快、错在哪，都要能查。
       没有记录，bug 只能靠猜；有了记录，慢和错都变成可定位的事实。

为什么按 **IP** 判而不是按域名判：
    `.cn` 只覆盖一小部分，qq.com / 163.com / 绝大多数 CDN 都是 .com。
    唯一的硬事实是「这个 IP 分给了谁」—— 所以用 APNIC 的 CN 分配表（cn_ranges.py），
    按解析出来的 IP 判，命中即国内。

选路不是「猜」而是「量」：
    · 国外目标默认 **直连优先**（直连不经过任何中间层，永远是最短路径）；
    · 直连连续失败/明显更慢 ⇒ 才把加速通道提到前面（带迟滞，不来回抖）；
    · 同一台主机解析出多个 IP 时，后台探测 TCP 握手最快的那个，下次直接用。

对外接口：
    classify(host)                   -> "cn" | "foreign"
    classify_detail(host, port)      -> 可解释的复合判据 dict（标签系统/日志用）
    plan(host, port)                 -> [Route, ...] 有序候选（第一个是首选）
    record(host, route, ok, ...)     -> 记一条使用记录（JSONL，自动轮转）
    report(top)                      -> 汇总：走了哪些路、多快、错多少
    note_host(host, ...)             -> 手工标注（国内站被误判时用）
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
import time
from collections import defaultdict

import cn_ranges

BASE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(BASE, "_kb")
USAGE = os.path.join(KB, "egress_usage.jsonl")
USAGE_MAX = 8 * 1024 * 1024          # 单文件上限，超了轮转成 .1（各留一份就够回溯）
CONFIG = os.path.join(BASE, "_netpolicy.json")

DNS_TTL = 120.0                       # 域名解析缓存
IP_RANK_TTL = 900.0                   # 最优 IP 结果缓存 15 分钟
LOCK = threading.Lock()

# 国内域名（后缀/精确），用于「还没解析就先判」的快速路径。
# 注意：只放确定性高的，判错会把国外站锁死在直连上，反而更慢。
CN_SUFFIX = (
    ".cn", ".com.cn", ".net.cn", ".org.cn", ".gov.cn", ".edu.cn", ".ac.cn",
    ".中国", ".公司", ".网络",
)
CN_HOSTS = {
    "localhost", "127.0.0.1", "::1",
    "baidu.com", "qq.com", "weixin.qq.com", "tencent.com", "aliyun.com",
    "taobao.com", "tmall.com", "jd.com", "bilibili.com", "zhihu.com",
    "douyin.com", "163.com", "126.com", "sina.com.cn", "weibo.com",
    "csdn.net", "gitee.com", "huaweicloud.com", "xiaomi.com", "mi.com",
    "douban.com", "sohu.com", "ifeng.com", "iqiyi.com", "youku.com",
    "alipay.com", "dingtalk.com", "feishu.cn", "kuaishou.com", "ximalaya.com",
}

# 强制走加速的域名（即使 IP 判成国内也走）——默认空，留着手工兜底
FORCE_PROXY = set()

_dns = {}                             # host -> (ts, [ip])
_iprank = {}                          # host -> (ts, ip)
_note = {}                            # host -> "cn"/"foreign"  手工标注
_usage = defaultdict(lambda: {"n": 0, "ok": 0, "fail": 0,
                              "bytes": 0, "ms": 0.0, "ttfb": 0.0})
_cfg_cache = [0.0, {}]
_cfg_tunnel_seen = [""]        # 上一次配置里的隧道地址；变化即清熔断（见 _cfg）


# ------------------------------------------------------------------ 配置
def _cfg():
    """读 _netpolicy.json（热加载）。只关心出口相关的几个键，其余用默认。

    🔴 隧道地址变化时自动清掉旧域名攒下的失败状态（熔断计数 + tunnel 坏记录）。
    这是「换域名/回滚不用重启 selfproxy」的关键：旧域名被封时累计的
    `_TUNNEL_BREAK` 与 `tunnel_bad` 都是进程内存状态，配置热加载了它们却不会
    自动失效，会把新域名也连坐判死、只能靠重启清空。这里在读到新地址的瞬间
    主动清掉，于是改一处配置即可热生效——不停爬虫、不中断任何在跑的任务。
    """
    try:
        mt = os.path.getmtime(CONFIG)
    except Exception:
        mt = 0.0
    if _cfg_cache[0] == mt and _cfg_cache[1]:
        return _cfg_cache[1]
    cfg = {"accelerate": "", "tunnel": "", "tunnel_timeout": 12.0, "cn_direct": True,
           "foreign_prefer": "direct", "probe_ips": 4, "ip_probe_timeout": 0.8}
    try:
        with open(CONFIG, encoding="utf-8") as f:
            raw = json.load(f)
        # 显式留空 = 关闭通道。不能写 `raw.get(...) or 默认值`，
        # 否则空字符串会被默认值顶回来，通道永远关不掉（一键回滚就失效）。
        if "accelerate_proxy" in raw:
            cfg["accelerate"] = str(raw["accelerate_proxy"] or "").strip()
        cfg["tunnel"] = str(raw.get("foreign_tunnel") or "").strip()
        try:
            cfg["tunnel_timeout"] = float(raw.get("tunnel_connect_timeout") or 12.0)
        except (TypeError, ValueError):
            pass
        if "cn_direct" in raw:
            cfg["cn_direct"] = bool(raw["cn_direct"])
        cfg["foreign_prefer"] = (raw.get("foreign_prefer") or "direct").lower()
    except Exception:
        pass
    # 隧道地址变了（换域名/临时关闭）⇒ 旧地址的成功率/熔断史对新地址无意义，清掉。
    # 放在配置更新后、返回前；只在此处做，避免每个请求都无谓清状态。
    if cfg["tunnel"] != _cfg_tunnel_seen[0]:
        with LOCK:
            _TUNNEL_BREAK["fails"] = 0
            _TUNNEL_BREAK["until"] = 0.0
            for k in [k for k in _usage if k[1] == "tunnel"]:
                _usage.pop(k, None)
        _cfg_tunnel_seen[0] = cfg["tunnel"]
    _cfg_cache[0], _cfg_cache[1] = mt, cfg
    return cfg


# ------------------------------------------------------------------ 解析
def resolve(host, port=443, ttl=DNS_TTL):
    """域名 → IP 列表（带缓存）。只取 IPv4，失败返回 []。

    ⚠️ 只做一次解析、结果缓存：原来每条连接都调一次 getaddrinfo，
       跳转链里同一台主机会被反复解析，白付 40ms×N。
    """
    if not host:
        return []
    if _is_ipv4(host):
        return [host]
    now = time.time()
    with LOCK:
        hit = _dns.get(host)
    if hit and now - hit[0] < ttl:
        return hit[1]
    ips = []
    try:
        for fam, _t, _p, _c, sa in socket.getaddrinfo(host, port, socket.AF_INET,
                                                      socket.SOCK_STREAM):
            if sa[0] not in ips:
                ips.append(sa[0])
    except Exception:
        ips = []
    with LOCK:
        _dns[host] = (now, ips)
    return ips


def _is_ipv4(s):
    try:
        socket.inet_aton(s)
        return s.count(".") == 3
    except OSError:
        return False


def resolve6(host, port=443, ttl=DNS_TTL):
    """域名 → IPv6 列表（带缓存）。没有 AAAA 记录就返回 []。

    🔴 为什么要专门走 v6：现网封锁绝大多数只铺在
    IPv4 侧（DNS 污染、IP 段阻断、SNI 干扰都按 v4 落地）。IPv6 往往**整条路径
    没人管**：有 AAAA 就能直达，且不少海外站的 v6 互联比 v4 更直。这不是绕，
    是走一条当下没被设卡的路。链路没 v6 时这里永远是空表，绝不产生额外延迟。
    """
    if not host or _is_ipv4(host) or ":" in host:
        return [host] if ":" in (host or "") else []
    now = time.time()
    key = "6:" + host
    with LOCK:
        hit = _dns.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    ips = []
    try:
        for fam, _t, _p, _c, sa in socket.getaddrinfo(host, port, socket.AF_INET6,
                                                      socket.SOCK_STREAM):
            ip = sa[0]
            # 只认全局单播（2000::/3）：链路本地/U LA 出了本机就没意义
            if ip and not ip.startswith(("fe80", "fc", "fd")) and ip not in ips:
                ips.append(ip)
    except Exception:
        ips = []
    if not ips and has_v6():
        # Windows 在「本机没有全局 v6」时会**抑制 AAAA 查询结果**，哪怕上游有 AAAA。
        # 所以一旦确认本机有 v6 出口却解析不到地址，就绕开系统解析器用 DoH 问一次。
        ips = _doh_aaaa(host, port)
    with LOCK:
        _dns[key] = (now, ips)
    return ips


def _doh_aaaa(host, port=443):
    """DoH 取 AAAA（只在系统解析器给出空结果时兜底，正常路径不付这个钱）。"""
    try:
        import json as _json
        import urllib.request
        body = _json.dumps({"name": host, "type": "AAAA"}).encode()
        req = urllib.request.Request(
            "https://223.5.5.5/resolve", data=body,
            headers={"Content-Type": "application/json"})
        ctx = __import__("ssl")._create_unverified_context()
        with urllib.request.urlopen(req, timeout=3, context=ctx) as r:
            data = _json.loads(r.read().decode("utf-8", "ignore"))
        out = []
        for a in data.get("Answer") or []:
            v = a.get("data") or ""
            if a.get("type") == 28 and v and v not in out:
                out.append(v)
        return out
    except Exception:
        return []


_V6 = [0.0, None]


def has_v6(ttl=300.0):
    """本机当前有没有可用的 IPv6 出口（有默认路由才叫有）。

    判法：对公共 v6 地址做一次 UDP connect —— 它不发包，只让内核查路由表；
    成功即说明有 v6 默认路由。比「有没有 v6 地址」更准（有地址没路由很常见）。
    """
    now = time.time()
    if _V6[1] is not None and now - _V6[0] < ttl:
        return _V6[1]
    ok = False
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        try:
            s.connect(("2606:4700:4700::1111", 53))     # Cloudflare DNS 的 v6
            ok = True
        finally:
            s.close()
    except Exception:
        ok = False
    _V6[0], _V6[1] = now, ok
    return ok


# ------------------------------------------------------------------ 分类
# 🔴 2026-11-09 晚升级：判据从「任一 IP 命中即国内」改成复合判据。
#    两个真实坑：
#      ① DNS 污染会给 www.google.com 返回回环/私网/假 IP。拿这种 IP 查 APNIC，
#         可能判成国内 ⇒ 被 cn_direct 锁死在直连上，隧道回落永远轮不到。
#         所以明显 bogon 地址一律丢弃；已知被切断的域干脆不看本机解析。
#      ② 一个站同时解析出国内外节点（常见 CDN）时，旧规则直接判国内。
#         现在混合一律判国外，但选路仍是「直连优先」，只是保留回落机会。
KNOWN_CUT_LABELS = {
    "google", "youtube", "youtu", "gstatic", "googleapis", "googlevideo",
    "facebook", "fbcdn", "instagram", "twitter", "x", "t", "telegram",
    "wikipedia", "wikimedia", "reddit", "discord", "quora", "medium",
}
_DETAIL = {}                          # host -> (ts, dict)，复用 DNS_TTL 缓存


def _bogon(ip):
    """回环/私网/链路本地/保留/组播/未指定 = 不可能是真实公网目标。"""
    try:
        a = ipaddress.ip_address(ip)
        return bool(a.is_loopback or a.is_private or a.is_link_local
                    or a.is_reserved or a.is_multicast or a.is_unspecified)
    except Exception:
        return True


def _known_cut(h):
    """按域名标签判「已知被切断」，避免 t.cn / x.example.org 这类误伤。"""
    parts = h.split(".")
    labels = set(parts)
    if "t" in labels and "me" in labels:            # t.me
        return True
    if "x" in labels and ("com" in labels or len(parts) <= 3):
        return True
    return bool(labels & (KNOWN_CUT_LABELS - {"x", "t"}))


def _detail(host, region, reason, ips=(), cn_ratio=0.0, polluted=(), source=""):
    return {"host": host, "region": region, "reason": reason,
            "ips": list(ips)[:8], "cn_ratio": round(float(cn_ratio), 3),
            "polluted": list(polluted)[:8], "source": source,
            "ts": time.time()}


def classify_detail(host, port=443):
    """复合判据，返回可解释 dict：region / reason / ips / cn_ratio / polluted / source。

    顺序：手工标注 > IP 字面量 > 强制名单 > 已知被切断域 > 国内后缀/名单
          > 解析 IP 归属（bogon 过滤 + 全部命中才算国内）> 默认 foreign。
    纯本地、带缓存；给标签系统写 region 字段用，不额外发任何网络请求
    （resolve 本身有 DNS_TTL 缓存）。
    """
    h = (host or "").lower().rstrip(".")
    now = time.time()
    with LOCK:
        hit = _DETAIL.get(h)
    if hit and now - hit[0] < DNS_TTL:
        return hit[1]

    if not h:
        d = _detail(h, "foreign", "empty-host", source="none")
    elif h in _note:
        d = _detail(h, _note[h], "manual-note", source="manual")
    elif _is_ipv4(h):
        cn = cn_ranges.is_cn(h) and not _bogon(h)
        d = _detail(h, "cn" if cn else "foreign",
                    "ip-cn" if cn else "ip-not-cn", ips=[h],
                    cn_ratio=1.0 if cn else 0.0, source="literal")
    elif ":" in h:                    # IPv6 字面量：暂无 CN v6 分配表，按国外处理
        d = _detail(h, "foreign", "ipv6-literal", ips=[h], source="literal")
    elif h in FORCE_PROXY:
        d = _detail(h, "foreign", "force-proxy", source="manual")
    elif _known_cut(h):
        # 被 DNS 污染 / IP 段切断的域：本机解析结果不可信，直接按国外处理。
        d = _detail(h, "foreign", "known-cut-skip-dns", source="policy")
    elif h in CN_HOSTS or h.endswith(CN_SUFFIX):
        d = _detail(h, "cn", "cn-suffix-or-hostlist", source="policy")
    else:
        ips = resolve(h, port)
        good = [ip for ip in ips if not _bogon(ip)]
        bad = [ip for ip in ips if ip not in good]
        cn = [ip for ip in good if cn_ranges.is_cn(ip)]
        ratio = len(cn) / len(good) if good else 0.0
        if not good:
            region, reason = "foreign", "dns-empty-or-all-bogon"
        elif ratio >= 0.999:
            region, reason = "cn", "all-ip-cn"
        elif ratio > 0:
            region, reason = "foreign", "mixed-cdn-keep-fallback"
        else:
            region, reason = "foreign", "all-ip-foreign"
        d = _detail(h, region, reason, ips=good, cn_ratio=ratio,
                    polluted=bad, source="dns+apnic")

    with LOCK:
        _DETAIL[h] = (now, d)
    return d


def classify(host, port=443):
    """判断目标是国内还是国外。返回 "cn" / "foreign"（细节见 classify_detail）。"""
    return classify_detail(host, port)["region"]


def note_host(host, kind):
    """手工纠正一个域的判定（写进 _kb/egress_notes.json，永久生效）。"""
    path = os.path.join(KB, "egress_notes.json")
    data = {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        pass
    data[host] = kind
    try:
        os.makedirs(KB, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        _note[host] = kind
        return True
    except Exception:
        return False


def _load_notes():
    path = os.path.join(KB, "egress_notes.json")
    try:
        with open(path, encoding="utf-8") as f:
            _note.update({k: v for k, v in json.load(f).items()
                          if v in ("cn", "foreign")})
    except Exception:
        pass


# ------------------------------------------------------------------ 最优 IP
def _probe(ip, port, timeout):
    t0 = time.perf_counter()
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return (time.perf_counter() - t0) * 1000
    except Exception:
        return None


def best_ip(host, port=443, ips=None, timeout=None):
    """同域多 IP 时挑握手最快的一个（有缓存）。

    为什么值得：国外 CDN 在国内的可达性差异极大，同一域名不同 anycast/边缘 IP
    实测能差出一倍。这一步不改协议、不改线路，只改「先连谁」，是纯赚的。
    """
    ips = ips or resolve(host, port)
    if len(ips) <= 1:
        return ips[0] if ips else None
    now = time.time()
    with LOCK:
        hit = _iprank.get(host)
    if hit and now - hit[0] < IP_RANK_TTL and hit[1] in ips:
        return hit[1]
    cfg = _cfg()
    cand = ips[:max(1, int(cfg.get("probe_ips", 4)))]
    to = float(timeout or cfg.get("ip_probe_timeout", 0.8))
    res = []
    ths = []

    def work(ip):
        ms = _probe(ip, port, to)
        if ms is not None:
            res.append((ms, ip))

    for ip in cand:
        t = threading.Thread(target=work, args=(ip,), daemon=True)
        t.start()
        ths.append(t)
    for t in ths:
        t.join(to + 0.2)
    if not res:
        return ips[0]
    res.sort()
    best = res[0][1]
    with LOCK:
        _iprank[host] = (now, best)
    return best


# ------------------------------------------------------------------ 选路
class Route:
    """一条候选出口。kind: direct / proxy；addr 为空表示直连；ip 是钉住的直连地址。"""

    __slots__ = ("kind", "addr", "ip")

    def __init__(self, kind, addr="", ip=""):
        self.kind, self.addr, self.ip = kind, addr, ip

    def name(self):
        if self.kind.startswith("direct"):
            return "direct" + (":" + self.ip if self.ip else "")
        if self.kind == "tunnel":
            return "tunnel"
        return "accel"

    def key(self):
        return self.kind

    def __repr__(self):
        return "<Route %s %s>" % (self.name(), self.addr or self.ip)


# 隧道熔断：晚高峰 Worker 握手连续失败时，别让每个请求都白等十来秒。
# 到期自动重新放行一次，网络恢复（平峰）后能自己复活。
_TUNNEL_BREAK = {"fails": 0, "until": 0.0}
TUNNEL_BREAK_AFTER = 5
TUNNEL_BREAK_SECONDS = 90.0


def _score(host, kind):
    """用**该 host 自己**的使用记录给出口打分：越大越好。没有记录 ⇒ 0（等同）。

    🔴 不回落到 ("*", kind) 全局记录：历史里国外直连失败很多，若让全局分
    参与排序，一个新域名刚进来就会被别处的失败史推去先试慢隧道（晚高峰
    尤其糟）。全局记录只用于 report() 统计，不决定任何单个域名的路径。
    """
    st = _usage.get((host, kind))
    if not st or not st["n"]:
        return 0.0
    okr = st["ok"] / max(1, st["n"])
    speed = st["bytes"] / max(1.0, st["ms"])          # B/ms ≈ KB/s
    return okr * 100.0 + min(speed, 20000.0) / 200.0 - st["fail"] * 12.0


def cached_ip(host, port=443, warm=True):
    """零延迟取 IP：有最优结果就用它，否则用解析出的第一个（不等探测）。

    为什么不在建连前同步探测：探测 4 个 IP 最坏要 0.8s，把它塞进请求路径
    等于拿「可能更快」换「一定更慢」。所以这里只读缓存，探测丢到后台，
    等同一台主机下一次连接时享受成果。
    """
    now = time.time()
    with LOCK:
        hit = _iprank.get(host)
    if hit and now - hit[0] < IP_RANK_TTL:
        return hit[1]
    ips = resolve(host, port)
    if warm and len(ips) > 1:
        threading.Thread(target=best_ip, args=(host, port, ips), daemon=True).start()
    return ips[0] if ips else None


def plan(host, port=443):
    """给出有序候选出口列表。

    · 国内                   ⇒ 只有直连（绝不经加速层，加速层对国内站只会更慢）
    · 国外 + 有 v6 出口      ⇒ **v6 直连提到最前**（封锁大多只做在 v4 侧）
    · 国外 + 配了隧道        ⇒ 候选末尾追加 Worker 隧道（80 端口除外）
    · 直连连续失败 3 次以上  ⇒ 直接走隧道，不再每请求白付 4s 直连超时
    """
    cfg = _cfg()
    kind = classify(host, port)
    if kind == "cn" and cfg.get("cn_direct", True):
        return [Route("direct4")]

    accel = cfg.get("accelerate") or ""
    tunnel = cfg.get("tunnel") or ""
    out = [Route("direct4")]
    # v6 优先：只对国外目标启用；国内站走 v6 反而可能绕远
    if has_v6():
        try:
            v6 = resolve6(host, port)
        except Exception:
            v6 = []
        if v6:
            out.insert(0, Route("direct6", ip=v6[0]))
    if accel:
        out.append(Route("accel", accel))
    # Worker 的 connect() 禁连 80（明文 HTTP），隧道只对非 80 端口开放；
    # 排在直连之后：能直连的站别白付一次跨境握手。
    if (tunnel and port != 80 and
            time.monotonic() >= _TUNNEL_BREAK["until"]):
        out.append(Route("tunnel", tunnel))
    if len(out) == 1:
        return out

    # 用记录决定顺序（v6 有记录就按记录，没记录就信「v6 未被设卡」这条例外判断）
    scores = [(r, _score(host, r.kind)) for r in out]
    if all(s == 0.0 for _, s in scores):
        prefer = cfg.get("foreign_prefer", "direct")
        if prefer == "accel":
            out.sort(key=lambda r: 0 if r.kind == "accel" else 1)
        elif prefer == "tunnel":
            out.sort(key=lambda r: 0 if r.kind == "tunnel" else 1)
    else:
        best = max(scores, key=lambda kv: kv[1])
        if out[0] is not best[0] and best[1] - scores[0][1] >= 15.0:
            out.remove(best[0])
            out.insert(0, best[0])

    # 短路：这个 host 的直连已连续失败多次 ⇒ 别再每次先撞 4s 直连超时。
    # 隧道自己也连续失败时解除短路，留机会给直连重新试探（网络会恢复）。
    if tunnel and port != 80:
        with LOCK:
            d = _usage.get((host, "direct4"))
            t = _usage.get((host, "tunnel"))
            dead_direct = bool(d and d["n"] >= 3 and d["ok"] == 0)
            tunnel_bad = bool(t and t["fail"] >= 5 and t["ok"] == 0)
        if dead_direct and not tunnel_bad:
            out.sort(key=lambda r: 0 if r.kind == "tunnel" else 1)
    return out


# ------------------------------------------------------------------ 使用记录
def _rotate():
    try:
        if os.path.getsize(USAGE) > USAGE_MAX:
            bak = USAGE + ".1"
            try:
                os.replace(USAGE, bak)
            except Exception:
                pass
    except Exception:
        pass


def record(host, route, ok, ms, ttfb=0.0, nbytes=0, err="", cls="", via=""):
    """记一条使用记录。**这是「有使用记录才知道哪里出 bug」的落点。**

    同时更新内存汇总（用于即时选路），并追加到 JSONL（用于事后追查）。
    写盘失败绝不影响转发 —— 记录是配角，转发是主角。
    """
    kind = route or "direct"
    with LOCK:
        for k in ((host, kind), ("*", kind)):
            st = _usage[k]
            st["n"] += 1
            if ok:
                st["ok"] += 1
                st["bytes"] += int(nbytes)
                st["ms"] += float(ms)
                st["ttfb"] += float(ttfb)
            else:
                st["fail"] += 1
        if kind == "tunnel":
            if ok:
                _TUNNEL_BREAK["fails"] = 0
                _TUNNEL_BREAK["until"] = 0.0
            else:
                _TUNNEL_BREAK["fails"] += 1
                if _TUNNEL_BREAK["fails"] >= TUNNEL_BREAK_AFTER:
                    _TUNNEL_BREAK["until"] = (time.monotonic()
                                              + TUNNEL_BREAK_SECONDS)
    try:
        os.makedirs(KB, exist_ok=True)
        _rotate()
        rec = {"t": int(time.time()), "h": host, "r": kind, "ok": 1 if ok else 0,
               "ms": round(float(ms), 1), "ttfb": round(float(ttfb), 1),
               "b": int(nbytes), "c": cls, "v": via}
        if err:
            rec["e"] = str(err)[:120]
        with open(USAGE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def report(top=15):
    """汇总当前进程内的使用情况：哪条路快、哪条路错、哪些域最慢。"""
    with LOCK:
        snap = {k: dict(v) for k, v in _usage.items()}
    rows = []
    for (host, kind), st in snap.items():
        if host == "*" or not st["n"]:
            continue
        okr = st["ok"] / st["n"]
        avg = (st["ms"] / max(1, st["ok"]))
        mb = st["bytes"] / 1048576.0
        rows.append({"host": host, "route": kind, "n": st["n"], "ok": okr,
                     "avg_ms": avg, "mb": mb, "fail": st["fail"]})
    rows.sort(key=lambda r: (-r["n"]))
    return {
        "total": sum(r["n"] for r in rows),
        "by_route": {k: v for k, v in snap.items() if k[0] == "*"},
        "hosts": rows[:top],
        "usage_file": USAGE,
        "cn_ranges": cn_ranges.load(),
    }


def _replay(n=3000):
    """从使用记录尾部回灌评分（进程重启后不至于从零学）。只读最后 n 行。"""
    try:
        with open(USAGE, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n * 220))
            tail = f.read().decode("utf-8", "ignore").splitlines()[-n:]
    except Exception:
        return
    for ln in tail:
        try:
            r = json.loads(ln)
        except Exception:
            continue
        host, kind = r.get("h"), r.get("r")
        if not host or not kind:
            continue
        with LOCK:
            for k in ((host, kind), ("*", kind)):
                st = _usage[k]
                st["n"] += 1
                if r.get("ok"):
                    st["ok"] += 1
                    st["bytes"] += int(r.get("b") or 0)
                    st["ms"] += float(r.get("ms") or 0)
                    st["ttfb"] += float(r.get("ttfb") or 0)
                else:
                    st["fail"] += 1


_load_notes()
_replay()


def summary_line():
    """一行摘要，给 selfproxy 的日志/自检用。"""
    st = _usage.get(("*", "direct"))
    sa = _usage.get(("*", "accel"))
    def fmt(x):
        if not x or not x["n"]:
            return "0"
        return "%d/%d ok %.0f%%" % (x["ok"], x["n"], 100.0 * x["ok"] / x["n"])
    return "国内IP段=%d 直连[%s] 加速[%s]" % (cn_ranges.load(), fmt(st), fmt(sa))


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        r = report()
        print("总请求 %d  国内IP段 %d  记录文件 %s" % (r["total"], r["cn_ranges"], r["usage_file"]))
        for (host, kind), st in sorted(r["by_route"].items(), key=lambda kv: -kv[1]["n"]):
            if host != "*":
                continue
            print("  %-6s 次数=%d 成功=%d 失败=%d 流量=%.1fMB"
                  % (kind, st["n"], st["ok"], st["fail"], st["bytes"] / 1048576.0))
        print("--- 最活跃主机 ---")
        for h in r["hosts"]:
            print("  %-32s %-6s n=%-5d ok=%5.1f%% avg=%6.0fms %7.2fMB"
                  % (h["host"], h["route"], h["n"], h["ok"] * 100, h["avg_ms"], h["mb"]))
    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        for h in ("baidu.com", "www.qq.com", "github.com", "registry.npmjs.org",
                  "speed.cloudflare.com", "pypi.org"):
            print("%-24s %-8s %s" % (h, classify(h), plan(h)))
    else:
        print(summary_line())
