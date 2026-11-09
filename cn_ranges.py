# -*- coding: utf-8 -*-
"""netforce / cn_ranges.py —— 「这是不是国内 IP」的判定表（离线、可刷新）

为什么需要：分流原则是
    **有国内的走国内，国外的才走加速。**
这要求每一跳都能回答「目标在国内还是国外」。域名后缀（.cn）只能覆盖一部分，
qq.com / 163.com / 大部分 CDN 都是 .com；所以真正的判据是 **IP 归属**。

数据源：APNIC 官方 delegated stats（`apnic|CN|ipv4|...`），只取 CN 的 IPv4 分配，
按起止整数区间排序合并，存成本地 `_kb/cn_ranges.tsv`。查询用二分，单次 ~1µs。

    from cn_ranges import is_cn
    is_cn("223.5.5.5")     # True
    is_cn("104.16.0.1")    # False

刷新：`python cn_ranges.py refresh`（默认 30 天过期；拿不到网就继续用旧表，绝不空表）
"""
from __future__ import annotations

import bisect
import os
import socket
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.join(BASE, "_kb")
TABLE = os.path.join(KB, "cn_ranges.tsv")
APNIC = "https://ftp.apnic.net/apnic/stats/apnic/delegated-apnic-latest"
TTL = 30 * 86400

# 兜底：APNIC 表缺失时也不至于把国内判成国外（只列最大的几段，够用不精确）
_FALLBACK = [
    ("1.0.1.0", "1.0.3.255"), ("14.0.0.0", "14.255.255.255"),
    ("27.0.0.0", "27.255.255.255"), ("36.0.0.0", "36.255.255.255"),
    ("39.0.0.0", "39.255.255.255"), ("42.0.0.0", "42.255.255.255"),
    ("49.0.0.0", "49.255.255.255"), ("58.0.0.0", "58.255.255.255"),
    ("59.0.0.0", "59.255.255.255"), ("60.0.0.0", "60.255.255.255"),
    ("61.0.0.0", "61.255.255.255"), ("101.0.0.0", "101.255.255.255"),
    ("106.0.0.0", "106.255.255.255"), ("110.0.0.0", "110.255.255.255"),
    ("111.0.0.0", "111.255.255.255"), ("112.0.0.0", "112.255.255.255"),
    ("113.0.0.0", "113.255.255.255"), ("114.0.0.0", "114.255.255.255"),
    ("115.0.0.0", "115.255.255.255"), ("116.0.0.0", "116.255.255.255"),
    ("117.0.0.0", "117.255.255.255"), ("118.0.0.0", "118.255.255.255"),
    ("119.0.0.0", "119.255.255.255"), ("120.0.0.0", "120.255.255.255"),
    ("121.0.0.0", "121.255.255.255"), ("122.0.0.0", "122.255.255.255"),
    ("123.0.0.0", "123.255.255.255"), ("124.0.0.0", "124.255.255.255"),
    ("125.0.0.0", "125.255.255.255"), ("171.0.0.0", "171.255.255.255"),
    ("175.0.0.0", "175.255.255.255"), ("180.0.0.0", "180.255.255.255"),
    ("182.0.0.0", "182.255.255.255"), ("183.0.0.0", "183.255.255.255"),
    ("202.96.0.0", "202.103.255.255"), ("203.0.0.0", "203.255.255.255"),
    ("210.0.0.0", "211.255.255.255"), ("218.0.0.0", "218.255.255.255"),
    ("219.0.0.0", "219.255.255.255"), ("220.0.0.0", "220.255.255.255"),
    ("221.0.0.0", "221.255.255.255"), ("222.0.0.0", "222.255.255.255"),
    ("223.0.0.0", "223.255.255.255"),
]

_starts = []
_ends = []
_loaded = None


def _ip2int(s):
    try:
        p = socket.inet_aton(s)
    except OSError:
        return None
    return (p[0] << 24) | (p[1] << 16) | (p[2] << 8) | p[3]


def _merge(ranges):
    """[(s,e)] → 排序合并成最小的不重叠区间集合。"""
    if not ranges:
        return []
    ranges.sort()
    out = [list(ranges[0])]
    for s, e in ranges[1:]:
        if s <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def build(text):
    """把 APNIC delegated stats 文本解析成 CN 的 [(start,end)] 整数区间（含 IPv6 跳过）。"""
    out = []
    for ln in text.splitlines():
        if not ln or ln.startswith("#"):
            continue
        p = ln.split("|")
        if len(p) < 5 or p[2] != "ipv4":
            continue
        # APNIC 只代表亚太，CN 的分配也可能出现在其它 RIR 表里 ⇒ 全部收
        if p[1] != "CN":
            continue
        s = _ip2int(p[3])
        try:
            cnt = int(p[4])
        except ValueError:
            continue
        if s is not None and cnt > 0:
            out.append((s, s + cnt - 1))
    return _merge(out)


def load(force=False):
    """装载区间表（内存里只装一次）。文件缺失/损坏 ⇒ 用兜底表，永不抛。"""
    global _starts, _ends, _loaded
    if _loaded is not None and not force:
        return _loaded
    ranges = []
    try:
        with open(TABLE, encoding="utf-8") as f:
            for ln in f:
                a, _, b = ln.strip().partition("\t")
                s, e = int(a), int(b)
                ranges.append((s, e))
    except Exception:
        ranges = []
    if not ranges:
        ranges = [(_ip2int(a), _ip2int(b)) for a, b in _FALLBACK]
    _starts = [r[0] for r in ranges]
    _ends = [r[1] for r in ranges]
    _loaded = len(ranges)
    return _loaded


def is_cn(ip):
    """IP 是否属于中国大陆分配。非 IPv4 / 解析失败 ⇒ False（宁可走加速，不误伤直连判断）。"""
    if not ip:
        return False
    v = _ip2int(ip) if not isinstance(ip, int) else ip
    if v is None:
        return False
    if _loaded is None:
        load()
    i = bisect.bisect_right(_starts, v) - 1
    return i >= 0 and v <= _ends[i]


def refresh(timeout=40):
    """从 APNIC 拉最新表并落盘。失败返回 False，旧表继续用。"""
    import urllib.request
    try:
        os.makedirs(KB, exist_ok=True)
        with urllib.request.urlopen(APNIC, timeout=timeout) as r:
            text = r.read().decode("utf-8", "ignore")
        ranges = build(text)
        if len(ranges) < 1000:                      # 明显不对就别覆盖
            return False
        tmp = TABLE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for s, e in ranges:
                f.write("%d\t%d\n" % (s, e))
        os.replace(tmp, TABLE)
        load(force=True)
        return True
    except Exception:
        return False


def age_days():
    try:
        return (time.time() - os.path.getmtime(TABLE)) / 86400.0
    except Exception:
        return 999.0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "refresh":
        ok = refresh()
        print("refresh:", "OK" if ok else "FAILED", "段数=%s" % load(force=True), "age=%.1f天" % age_days())
    else:
        print("段数=%s 表龄=%.1f天" % (load(), age_days()))
        for ip in ("223.5.5.5", "114.114.114.114", "104.16.0.1", "8.8.8.8", "140.82.112.3"):
            print("%-16s -> %s" % (ip, "国内" if is_cn(ip) else "国外"))
