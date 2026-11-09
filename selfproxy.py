# -*- coding: utf-8 -*-
"""netforce / selfproxy.py —— 内建规则出口代理（不依赖外部代理）

设计目标：**出网能力应该是系统自带的一层，而不是外部前提。**
    本模块在本机 127.0.0.1 起一个轻量 HTTP/CONNECT 代理，所有抓取方
    （urllib / curl / 无头 Chrome）只要指到这个本地入口，就获得：

      · 统一出口       所有流量一个入口，规则、日志、限速都在这层做
      · 规则分流       国内/白名单域名直连，需要走节点的域名自动转上游
      · 失败自动降级   上游连不上 → 立刻回落直连，绝不因为代理没开就瘫痪
      · 外挂式上游     可按需接入自有节点，热加载配置即生效，
                       **不用改代码、不用重启浏览器**
      · 零外部依赖     没有上游时它就是一个纯直连转发器，功能不打折

复用天网既有能力（不重造轮子）：
    skynet/route.py::_ChainForwarder 的 CONNECT 隧道与双向 pump 写法、
    netpolicy.channels() 的「直连优先 / 按域名分流 / 存活探测」决策。
    更上层的 RelayFetcher（借自有节点 IP）由 netpolicy.fetch_any() 兜底接入。

明文 HTTP 中继重写：跳转死循环与速度下降，实测三个确定性缺陷（已用抓包与连接表复现）：
     ① 请求头重建多拼了一个空行，`Connection: close` 被写到了消息体里
        （抓到的原始字节：`...\r\n\r\n\r\nConnection: close\r\n\r\n`）⇒ 上游按
        keep-alive 处理，本层却在等 EOF ⇒ 响应读完也不释放；
     ② 判「响应结束」只靠 EOF、且上游 socket 无超时 ⇒ 上游 keep-alive 时
        `_do_plain` 线程永久阻塞，同时占着信号量槽与 socket。实测 3 个请求
        结束后仍有 6 条 ESTABLISHED 滞留（PID 13248）⇒ 槽位漏满 96 个后
        整个出口拒绝服务，客户端不断重试，表现就是「跳转越多越卡 → 死循环」；
     ③ 请求体只转发「和请求头同一个包到达的部分」⇒ 大 POST 后半截永远不发出，
        上游按 Content-Length 干等 ⇒ 登录/上传类跳转必然超时重试。

   改法（速度优先）：
     · 按 RFC 7230 正确重建报文；body 按 Content-Length / chunked 完整转发；
     · 响应按 Content-Length / chunked / 短连接三种定界精确收尾，绝不靠 EOF 猜；
     · 上游 socket 全部设超时，客户端中途断开立即回收线程与槽位；
     · **上游 keep-alive 连接池**：同 host:port 复用，跳转链少付 TCP 握手；
     · 槽位不足时短暂排队（3s）而不是直接掐断，减少无谓重试。

用法：
    from selfproxy import start, addr, stop, report
    a = start()          # → 'http://127.0.0.1:18080'
    stop()               # 进程退出时自动关（atexit）
"""
from __future__ import annotations

import atexit
import os
import socket
import sys
import threading
import time
import urllib.parse
from collections import Counter, defaultdict

import egress_route
import netpolicy

DEFAULT_PORT = int(os.environ.get("NETFORCE_SELF_PROXY_PORT", "18080"))
MAX_CONN = int(os.environ.get("NETFORCE_SELF_PROXY_MAX_CONN", "256"))
CONNECT_TIMEOUT = 15          # 建连（含上游 CONNECT 握手）
# 国外直连单独用短超时：跨境 TCP 握手正常 < 500ms，给 15s 是拿「可能建上」
# 换「一定慢」。实测 github 直连 15s 才超时，等于把一次失败放大成一次卡死。
FOREIGN_CONNECT_TIMEOUT = float(os.environ.get("NETFORCE_FOREIGN_CONNECT_TIMEOUT", "4"))
IO_TIMEOUT = 30               # 单次读写空闲上限，防上游挂死拖住线程
ACQUIRE_TIMEOUT = 3.0         # 槽位满时短暂排队，避免瞬时峰值直接掐断
POOL_MAX = 8                  # 每个 host:port 最多缓存的空闲连接数
POOL_TTL = 45.0               # 空闲连接最长复用时间

# 首包探活：跨境干扰的常见手法是「TCP 握手照常成功、数据一过就丢/RST」。
# 只看 TCP 层会把这种连接判成成功，于是每次先吃满一个超时才重试 —— 表现就是
# 「网页一直在转圈」。判据换成「客户端首个上行包必须换来上游回应」，超时即
# 判假连通，换出口重放同一段字节（此时客户端还没收到任何数据，重放无副作用）。
FIRST_BYTE_TIMEOUT = float(os.environ.get("NETFORCE_FIRST_BYTE_TIMEOUT", "1.6"))

# ---------------------------------------------------------------- 吞吐参数
# 隧道吞吐受「窗口 / RTT」约束：跨境链路 RTT 常在 150~250ms，窗口给不够就上不去。
# 默认 64KB 分片 + 系统默认缓冲，实测中继比直连慢 15%；放大缓冲与分片后折损
# 压到 5% 以内。这是**不改线路**就能拿到的唯一一块纯收益。
SOCK_BUF = int(os.environ.get("NETFORCE_SOCK_BUF", str(4 * 1024 * 1024)))
TUNNEL_CHUNK = int(os.environ.get("NETFORCE_TUNNEL_CHUNK", str(256 * 1024)))
HTTP_CHUNK = 256 * 1024

_IDENT = b"NETFORCE-LOCAL-EGRESS/1.1"
_PING_HOST = "__netforce_ping__"

# ---------------------------------------------------------------- 国外隧道
# 两端口架构：18080 统一出网，7810 只管 git。
# 隧道不再单独监听 18083，而是 selfproxy 进程内直接建连（cloudnode/wsrelay.py），
# 作为国外目标的一条候选出口，排在直连之后、由使用记录决定先后顺序。
_FLEET_CLOUDNODE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skynet-fleet", "tools", "cloudnode")
_WS_KEY_FILE = os.path.join(os.path.expanduser("~"), ".fleet", "wsrelay.key")
TUNNEL_CONNECT_TIMEOUT = float(os.environ.get("NETFORCE_TUNNEL_TIMEOUT", "12"))
TUNNEL_FIRST_BYTE_TIMEOUT = float(os.environ.get(
    "NETFORCE_TUNNEL_FIRST_BYTE_TIMEOUT", "6"))
_TUNNEL_READY = False
_TUNNEL_LOCK = threading.Lock()


def _open_tunnel(url, host, port):
    """进程内直连 Worker 隧道，返回可 recv/sendall 的 WsSock。

    密钥只在第一次调用时从 ~/.fleet/wsrelay.key 读进内存并放进环境变量
    （wsrelay._tunnel_auth_path 从 WSRELAY_SECRET 取 ?k=），代码里零默认值。
    """
    global _TUNNEL_READY
    with _TUNNEL_LOCK:
        if not _TUNNEL_READY:
            with open(_WS_KEY_FILE, encoding="utf-8") as f:
                secret = f.read().strip()
            if not secret:
                raise OSError("wsrelay.key is empty")
            os.environ["WSRELAY_SECRET"] = secret
            if _FLEET_CLOUDNODE not in sys.path:
                sys.path.insert(0, _FLEET_CLOUDNODE)
            _TUNNEL_READY = True
    from wsrelay import connect_via_ws
    if "NETFORCE_TUNNEL_TIMEOUT" in os.environ:
        tmo = TUNNEL_CONNECT_TIMEOUT
    else:
        # 配置热加载：改 _netpolicy.json 的 tunnel_connect_timeout 下一跳生效
        tmo = float(egress_route._cfg().get("tunnel_timeout") or 12.0)
    return connect_via_ws(url, host, port, timeout=tmo)


_INSTANCE = None
_LOCK = threading.Lock()


def _tune(sock, bufsize=None):
    """给 socket 上大缓冲 + 关 Nagle。失败一律忽略（只是优化，不是前置条件）。"""
    buf = SOCK_BUF if bufsize is None else bufsize
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, buf)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, buf)
    except Exception:
        pass
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass


# ================================================================ 小工具
def _pump(a, b, timeout=None):
    """双向隧道转发（CONNECT 用）。返回 (上行, 下行) 字节数，供使用记录统计。

    分片 64KB → 256KB：系统调用少 3/4，长肥管道上这点开销直接体现在吞吐。
    """
    counts = [0, 0]

    def pipe(src, dst, idx):
        try:
            while True:
                d = src.recv(TUNNEL_CHUNK)
                if not d:
                    break
                dst.sendall(d)
                counts[idx] += len(d)
        except Exception:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except Exception:
                pass

    _tune(a)
    _tune(b)
    t = threading.Thread(target=pipe, args=(b, a, 1), daemon=True)
    t.start()
    pipe(a, b, 0)
    t.join(timeout=timeout or 5)
    return counts[0], counts[1]


def _pump_probe(c, up, replay=b"", first_timeout=None):
    """带首包探活的双向转发。返回 (状态, 已消费的客户端首包, 上行, 下行)。

    状态：
        "ok"    探活通过，整条会话已跑完
        "stall" 上游收下了首包却一个字都没回 ⇒ 判假连通，调用方换出口并重放首包
        "gone"  客户端没发数据就断开，不必重试

    为什么必须缓存首包：探活发生在「已给客户端回 200」之后（否则客户端不会
    发 ClientHello），此时客户端认为隧道已通。换上游重放同一段字节，对 TLS
    完全透明 —— 因为到现在它还没收到过任何服务端字节。
    replay 非空表示这是换出口后的重放，不再从客户端读。
    """
    if replay:
        first = replay
    else:
        c.settimeout(IO_TIMEOUT)
        try:
            first = c.recv(TUNNEL_CHUNK)
        except Exception:
            return "gone", b"", 0, 0
        if not first:
            return "gone", b"", 0, 0

    try:
        up.sendall(first)
    except Exception:
        return "stall", first, len(first), 0

    up.settimeout(first_timeout or FIRST_BYTE_TIMEOUT)
    head = b""
    try:
        head = up.recv(TUNNEL_CHUNK)
    except socket.timeout:
        return "stall", first, len(first), 0
    except Exception:
        return "stall", first, len(first), 0
    if not head:
        return "stall", first, len(first), 0          # 对端直接关 = 也不可用

    up.settimeout(None)
    c.settimeout(None)
    try:
        c.sendall(head)
    except Exception:
        return "ok", first, len(first), len(head)
    u, d = _pump(c, up)
    return "ok", first, len(first) + u, len(head) + d


def _read_head(c, limit=256 * 1024):
    """读到完整报文头；顺带把多出来的 body 片段返回。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = c.recv(65536)
        if not chunk:
            return b"", b""
        buf += chunk
        if len(buf) > limit:
            return b"", b""
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head + b"\r\n\r\n", rest


def _parse_proxy(px):
    u = urllib.parse.urlsplit(px if "://" in px else "http://" + px)
    return u.hostname or "127.0.0.1", u.port or 80


def _header_map(lines):
    """[(b'Name: v')] → {b'name': [(b'Name: v'), ...]}（保留原始行以便原样转发）。"""
    out = defaultdict(list)
    for ln in lines:
        if not ln:
            continue
        name = ln.split(b":", 1)[0].strip().lower()
        out[name].append(ln)
    return out


class _Reader:
    """带缓冲的读端：保证「精确定长」转发时不多读、不丢字节。"""

    def __init__(self, sock, initial=b""):
        self.sock = sock
        self.buf = bytearray(initial)

    def read_until(self, marker, limit=256 * 1024):
        while marker not in self.buf:
            d = self.sock.recv(65536)
            if not d:
                break
            self.buf += d
            if len(self.buf) > limit:
                break
        i = self.buf.find(marker)
        if i < 0:
            head = bytes(self.buf)
            self.buf.clear()
            return head, False
        end = i + len(marker)
        head = bytes(self.buf[:end])
        del self.buf[:end]
        return head, True

    def read_some(self, n=HTTP_CHUNK):
        if self.buf:
            take = bytes(self.buf[:n])
            del self.buf[:n]
            return take
        return self.sock.recv(n)


# ================================================================ 上游连接池
class _Pool:
    """同 host:port 的空闲明文连接复用。跳转链里同一台服务器的多次请求少付握手。"""

    def __init__(self):
        self._idle = defaultdict(list)      # (host, port) → [(sock, ts)]
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.discards = 0

    def get(self, key):
        now = time.time()
        with self._lock:
            items = self._idle.get(key) or []
            while items:
                sock, ts = items.pop()
                if now - ts > POOL_TTL:
                    self.discards += 1
                    try:
                        sock.close()
                    except Exception:
                        pass
                    continue
                self.hits += 1
                return sock
        self.misses += 1
        return None

    def put(self, key, sock):
        with self._lock:
            items = self._idle[key]
            if len(items) >= POOL_MAX:
                try:
                    sock.close()
                except Exception:
                    pass
                self.discards += 1
                return
            items.append((sock, time.time()))

    def drop(self, key, sock):
        with self._lock:
            self.discards += 1
        try:
            sock.close()
        except Exception:
            pass

    def clear(self):
        with self._lock:
            for items in self._idle.values():
                for sock, _ in items:
                    try:
                        sock.close()
                    except Exception:
                        pass
            self._idle.clear()


POOL = _Pool()


class LocalEgress:
    def __init__(self, port=DEFAULT_PORT):
        self.port = port
        self.addr = ""
        self.sock = None
        self._stop = threading.Event()
        self._sem = threading.BoundedSemaphore(MAX_CONN)
        self.stats = Counter()
        self._lock = threading.Lock()
        self._ident = _IDENT

    # ------------------------------------------------------------ 生命周期
    def start(self):
        with _LOCK:
            if self.sock:
                return self.addr
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", self.port))
            except OSError:
                # 端口被占：可能已有另一个 netforce 出口在跑 ⇒ 先复用，不硬抢
                s.close()
                if _ping(self.port, self._ident):
                    self.addr = "http://127.0.0.1:%d" % self.port
                    return self.addr
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", 0))
                self.port = s.getsockname()[1]
            s.listen(128)
            s.settimeout(1.0)
            self.sock = s
            self.addr = "http://127.0.0.1:%d" % self.port
            threading.Thread(target=self._accept, daemon=True).start()
            atexit.register(self.stop)
            return self.addr

    def stop(self):
        self._stop.set()
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None
        POOL.clear()

    def report(self):
        with self._lock:
            data = dict(self.stats)
        data["pool_hit"] = POOL.hits
        data["pool_miss"] = POOL.misses
        data["pool_drop"] = POOL.discards
        data["egress"] = egress_route.summary_line()
        return data, self.addr

    def _bump(self, k, n=1):
        with self._lock:
            self.stats[k] += n

    # ------------------------------------------------------------ 路由
    def _routes(self, host, port):
        """有序候选出口（国内直连 / 国外按使用记录排）。

        每次请求都问一次 ⇒ 改 _netpolicy.json（cn_direct / accelerate_proxy）
        下一跳就生效，不用重启这个常驻进程。
        """
        try:
            return egress_route.plan(host, port)
        except Exception:
            return [egress_route.Route("direct")]

    # ------------------------------------------------------------ 连接处理
    def _accept(self):
        while not self._stop.is_set():
            try:
                c, _ = self.sock.accept()
            except Exception:
                continue
            if not self._sem.acquire(timeout=ACQUIRE_TIMEOUT):
                try:
                    c.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                              b"Content-Length: 0\r\nConnection: close\r\n\r\n")
                except Exception:
                    pass
                try:
                    c.close()
                except Exception:
                    pass
                self._bump("overload")
                continue
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            c.settimeout(IO_TIMEOUT)
            _tune(c)
            head, rest = _read_head(c)
            if not head:
                return
            first = head.split(b"\r\n", 1)[0].decode("latin-1", "ignore")
            parts = first.split()
            if len(parts) < 2:
                return
            method, target = parts[0].upper(), parts[1]
            if method == "CONNECT":
                host, _, port = target.rpartition(":")
                self._do_connect(c, head, host, int(port or 443))
                return
            # 兼容两种客户端写法：绝对形式（标准代理）与源站形式（透明入口）
            u = urllib.parse.urlsplit(target)
            if u.hostname:
                host, port = u.hostname, (u.port or 80)
                origin = (u.path or "/") + (("?" + u.query) if u.query else "")
            else:
                hm = _header_map(head.split(b"\r\n")[1:])
                hv = (hm.get(b"host") or [b""])[0].split(b":", 1)

                def _h(b):
                    return b.decode("latin-1", "ignore").strip()

                host = _h(hv[0]).strip("[]")
                port = int(_h(hv[1])) if len(hv) > 1 and _h(hv[1]).isdigit() else 80
                origin = target
                if not host:
                    c.sendall(b"HTTP/1.1 400 Bad Request\r\n"
                              b"Content-Length: 0\r\nConnection: close\r\n\r\n")
                    return
            if host == _PING_HOST:
                # 自报身份：让「端口是否已是我们自己」这件事可判定，
                # 不靠「回了个 HTTP/1.1 就算自己人」这种会误伤别人的猜测。
                c.sendall(b"HTTP/1.1 200 OK\r\nServer: " + self._ident
                          + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            self._do_plain(c, head, rest, host, port, method, target, origin)
        except Exception:
            self._bump("error")
        finally:
            try:
                c.close()
            except Exception:
                pass
            try:
                self._sem.release()
            except Exception:
                pass

    # ------------------------------------------------------------ CONNECT
    def _open_upstream(self, r, host, port, cls=""):
        """按候选出口建一条已打通的隧道。"""
        if r.kind == "tunnel" and r.addr:
            # Worker 已经替我们连上 host:port，返回的 WsSock 就是端到端字节管道，
            # 可直接交给 _pump()。不需要本地监听端口，也不需要额外的代理进程。
            up = _open_tunnel(r.addr, host, port)
            self._bump("tunnel_ws")
            return up
        if r.kind == "accel" and r.addr:
            ph, pp = _parse_proxy(r.addr)
            up = socket.create_connection((ph, pp), timeout=CONNECT_TIMEOUT)
            up.sendall(b"CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n"
                       % (host.encode(), port, host.encode(), port))
            resp = _read_head(up)[0]
            if b" 200 " not in resp.split(b"\r\n", 1)[0]:
                up.close()
                raise OSError("accel CONNECT refused")
            self._bump("tunnel_accel")
            return up
        ip = r.ip or egress_route.cached_ip(host, port) or host
        if r.kind == "direct6":
            fam = socket.AF_INET6
        else:
            fam = socket.AF_INET
        tmo = FOREIGN_CONNECT_TIMEOUT if (cls == "foreign"
                                          and r.kind.startswith("direct")) else CONNECT_TIMEOUT
        up = socket.socket(fam, socket.SOCK_STREAM)
        try:
            up.settimeout(tmo)
            up.connect((ip, port))
        except Exception:
            up.close()
            raise
        self._bump("tunnel_" + r.kind)
        return up

    def _do_connect(self, c, head, host, port):
        t0 = time.perf_counter()
        cls = egress_route.classify(host, port)
        replay = b""          # 上一出口收下但没换来回应的首包，下一个出口重放
        announced = False     # 已给客户端回过 200 ⇒ 不能再改状态行，只能断

        for r in self._routes(host, port):
            up = None
            try:
                up = self._open_upstream(r, host, port, cls)
                if not announced:
                    c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    announced = True

                # 国外直连 / Worker 隧道都要首包探活：隧道「连上却零字节」是
                # Worker 侧没连到目标，不探活会被误记成成功、污染出口排序，
                # 而且客户端只会看到一条永远不回数据的 200 隧道。
                # 隧道往返更高，单独给一段更长的首包超时。
                if cls == "foreign" and (r.kind.startswith("direct") or
                                         r.kind == "tunnel"):
                    tmo = TUNNEL_FIRST_BYTE_TIMEOUT if r.kind == "tunnel" else None
                    st, first, ub, db = _pump_probe(c, up, replay, tmo)
                    if st == "gone":
                        up.close()
                        return
                    if st == "stall":
                        up.close()
                        replay = first
                        self._bump("tunnel_stall")
                        egress_route.record(host, r.kind, False,
                                            (time.perf_counter() - t0) * 1000,
                                            0, 0, "stall:no-first-byte",
                                            cls, "tunnel")
                        continue                     # 换出口，重放同一段字节
                    up.close()
                    egress_route.record(host, r.kind, True,
                                        (time.perf_counter() - t0) * 1000, 0,
                                        db, "", cls, "tunnel")
                    return

                if replay:                           # 换到不探活的出口：先补发首包
                    up.sendall(replay)
                up.settimeout(None)
                c.settimeout(None)
                up_bytes, down_bytes = _pump(c, up)
                up.close()
                egress_route.record(host, r.kind, True,
                                    (time.perf_counter() - t0) * 1000, 0,
                                    down_bytes, "", cls, "tunnel")
                return
            except Exception as e:
                if up is not None:
                    try:
                        up.close()
                    except Exception:
                        pass
                egress_route.record(host, r.kind, False,
                                    (time.perf_counter() - t0) * 1000, 0, 0,
                                    "%s:%s" % (type(e).__name__, str(e)[:40]),
                                    cls, "tunnel")
                continue
        self._bump("fail")
        try:
            if not announced:
                c.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        except Exception:
            pass

    # ------------------------------------------------------------ 明文 HTTP
    def _do_plain(self, c, head, rest, host, port, method, target, origin):
        lines = head.split(b"\r\n")
        raw_headers = [ln for ln in lines[1:] if ln]
        hm = _header_map(raw_headers)

        # 重建请求头：去掉逐跳字段（由本层重新决定），Host 缺失时兜底
        drop = {b"proxy-connection", b"connection", b"keep-alive", b"te", b"upgrade"}
        fwd = [ln for ln in raw_headers
               if ln.split(b":", 1)[0].strip().lower() not in drop]
        if b"host" not in hm:
            fwd.append(b"Host: %s" % host.encode() if port == 80
                       else b"Host: %s:%d" % (host.encode(), port))
        try:
            clen = int((hm.get(b"content-length") or [b"0"])[0].split(b":", 1)[1])
        except Exception:
            clen = 0
        chunked_req = b"chunked" in b" ".join(
            hm.get(b"transfer-encoding") or []).lower()
        has_body = clen > 0 or chunked_req

        def build(where):
            return (b"%s %s HTTP/1.1\r\n" % (method.encode(), where.encode())
                    + b"\r\n".join(fwd)
                    + b"\r\nConnection: keep-alive\r\n\r\n")

        # 明文 HTTP 不能走 Worker 隧道：CF 的 connect() 禁连 80，
        # 且 _do_plain 的请求行语义也不适用于隧道端点 ⇒ 直接剔除。
        cands = [r for r in (self._routes(host, port) or
                             [egress_route.Route("direct")])
                 if r.kind != "tunnel"]
        if not cands:
            cands = [egress_route.Route("direct")]
        t0 = time.perf_counter()
        cls = egress_route.classify(host, port)
        last_err = ""
        for r in cands:
            px = r.addr if r.kind == "accel" else ""
            # 上游是代理时请求行必须用绝对形式；直连用源站形式
            req = build(target if px else origin)
            key = (host, port, r.kind, r.ip)
            for attempt in (1, 2):
                up, reused = None, False
                try:
                    if not px:                      # 直连才走连接池（代理有自己的连接管理）
                        up = POOL.get(key) if attempt == 1 else None
                        reused = up is not None
                    if up is None:
                        if px:
                            ph, pp = _parse_proxy(px)
                            up = socket.create_connection((ph, pp), timeout=CONNECT_TIMEOUT)
                            up.sendall(b"CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n"
                                       % (host.encode(), port, host.encode(), port))
                            resp, _ = _Reader(up).read_until(b"\r\n\r\n")
                            if b" 200 " not in resp.split(b"\r\n", 1)[0]:
                                raise OSError("accel CONNECT refused")
                            self._bump("plain_accel")
                        else:
                            ip = r.ip or egress_route.cached_ip(host, port) or host
                            up = socket.create_connection((ip, port),
                                                          timeout=CONNECT_TIMEOUT)
                            self._bump("plain_direct")
                        _tune(up)
                    up.settimeout(IO_TIMEOUT)
                    up.sendall(req)
                    if has_body:
                        self._send_body(c, up, rest, clen, chunked_req)
                    up.settimeout(IO_TIMEOUT)
                    ok, reusable, sent = self._relay_response(up, c, method)
                    spent = (time.perf_counter() - t0) * 1000
                    if ok and reusable and not px:
                        POOL.put(key, up)
                    else:
                        up.close()
                    self._bump("ok")
                    egress_route.record(host, r.kind, bool(ok), spent, spent,
                                        sent, "" if ok else "truncated", cls, "http")
                    return
                except Exception as e:               # noqa: BLE001
                    last_err = "%s: %s" % (type(e).__name__, str(e)[:60])
                    if up is not None:
                        try:
                            up.close()
                        except Exception:
                            pass
                    if reused:
                        continue                     # 池里的连接可能已被对端关掉 → 换新连接重试一次
                    break
            egress_route.record(host, r.kind, False, (time.perf_counter() - t0) * 1000,
                                0, 0, last_err, cls, "http")
        self._bump("fail")
        try:
            c.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        except Exception:
            pass

    # ---------------------------------------------------------- 请求体转发
    def _send_body(self, c, up, rest, clen, chunked):
        if chunked:
            r = _Reader(c, rest)
            while True:
                line, ok = r.read_until(b"\r\n", limit=8 * 1024)
                if not ok:
                    raise OSError("chunked body truncated")
                up.sendall(line)
                size_txt = line.split(b";", 1)[0].strip()
                try:
                    size = int(size_txt, 16)
                except ValueError:
                    raise OSError("bad chunk size")
                if size == 0:
                    # 尾部 trailer + 结束空行
                    while True:
                        t, ok = r.read_until(b"\r\n", limit=8 * 1024)
                        if not ok:
                            break
                        up.sendall(t)
                        if t == b"\r\n":
                            break
                    return
                left = size + 2                          # 含结尾 CRLF
                buf = rest if rest else b""
                rest = b""
                while left > 0:
                    if buf:
                        take, buf = buf[:left], buf[left:]
                    else:
                        take = r.read_some(min(65536, left))
                        if not take:
                            raise OSError("chunked body truncated")
                    up.sendall(take)
                    left -= len(take)
            return
        if clen > 0:
            left = clen
            buf = rest
            while left > 0:
                if buf:
                    take, buf = buf[:left], buf[left:]
                else:
                    take = c.recv(min(HTTP_CHUNK, left))
                    if not take:
                        raise OSError("request body truncated")
                up.sendall(take)
                left -= len(take)

    # ---------------------------------------------------------- 响应精确收尾
    def _relay_response(self, up, c, method):
        """按定界规则转发响应，返回 (成功, 连接可否复用, 发出字节数)。"""
        r = _Reader(up)
        sent = 0
        while True:
            head, ok = r.read_until(b"\r\n\r\n")
            if not ok or not head:
                raise OSError("empty response")
            first = head.split(b"\r\n", 1)[0]
            parts = first.split(None, 2)
            if len(parts) < 2 or not parts[1].isdigit():
                raise OSError("bad status line")
            status = int(parts[1])
            c.sendall(head)
            sent += len(head)
            hm = _header_map(head.split(b"\r\n")[1:])
            if 100 <= status < 200:
                continue                                  # 1xx 只转发，继续读正式响应
            break

        conn_hdr = b" ".join(hm.get(b"connection") or []).lower()
        server_close = b"close" in conn_hdr
        te = b" ".join(hm.get(b"transfer-encoding") or []).lower()
        clen_hdr = (hm.get(b"content-length") or [None])[0]

        if method == "HEAD" or status in (204, 304):
            return True, not server_close, sent
        if b"chunked" in te:
            while True:
                line, ok = r.read_until(b"\r\n", limit=8 * 1024)
                if not ok:
                    return False, False, sent
                c.sendall(line)
                sent += len(line)
                size_txt = line.split(b";", 1)[0].strip()
                try:
                    size = int(size_txt, 16)
                except ValueError:
                    return False, False, sent
                if size == 0:
                    while True:
                        t, ok = r.read_until(b"\r\n", limit=8 * 1024)
                        if not ok:
                            return False, False, sent
                        c.sendall(t)
                        sent += len(t)
                        if t == b"\r\n":
                            break
                    return True, not server_close, sent
                left = size + 2
                while left > 0:
                    take = r.read_some(min(HTTP_CHUNK, left))
                    if not take:
                        return False, False, sent
                    c.sendall(take)
                    sent += len(take)
                    left -= len(take)
        if clen_hdr:
            try:
                left = int(clen_hdr.split(b":", 1)[1])
            except Exception:
                left = -1
            if left >= 0:
                while left > 0:
                    take = r.read_some(min(HTTP_CHUNK, left))
                    if not take:
                        return False, False, sent
                    c.sendall(take)
                    sent += len(take)
                    left -= len(take)
                return True, not server_close, sent
        # 无定界（HTTP/1.0 风格 / 关连接收尾）：读到 EOF，但受 IO_TIMEOUT 约束
        while True:
            take = r.read_some(HTTP_CHUNK)
            if not take:
                return True, False, sent
            c.sendall(take)
            sent += len(take)


def _ping(port, ident):
    """探测该端口是不是自己的同伴实例（避免复用别人的服务）。

    只认自报的身份串 —— 以前只要回 HTTP/1.1 就算自己人，会把任意 HTTP 服务
    误判成本层（端口被别的程序占着时会静默把请求送进陌生人）。
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4) as s:
            s.sendall(b"GET http://%s/ HTTP/1.1\r\nHost: %s\r\n\r\n"
                      % (_PING_HOST, _PING_HOST))
            s.settimeout(0.6)
            data = s.recv(512)
            return ident in data
    except Exception:
        return False


def start(port=DEFAULT_PORT):
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = LocalEgress(port)
    return _INSTANCE.start()


def addr():
    """返回本地出口地址（不启动服务时也能用，供子进程读环境变量）。"""
    return os.environ.get("NETFORCE_SELF_PROXY") or (
        _INSTANCE.addr if _INSTANCE else "http://127.0.0.1:%d" % DEFAULT_PORT)


def stop():
    global _INSTANCE
    if _INSTANCE:
        _INSTANCE.stop()
        _INSTANCE = None


def report():
    return _INSTANCE.report() if _INSTANCE else ({}, "")


if __name__ == "__main__":
    import urllib.request
    a = start()
    print("本地出口:", a, flush=True)
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": a, "https": a}))
    try:
        r = op.open("http://example.com/", timeout=8)
        print("HTTP 明文:", r.status, len(r.read()), flush=True)
    except Exception as e:
        print("HTTP 明文失败:", e, flush=True)
    try:
        r = op.open("https://example.com/", timeout=8)
        print("HTTPS CONNECT:", r.status, len(r.read()), flush=True)
    except Exception as e:
        print("HTTPS CONNECT 失败:", e, flush=True)
    print("统计:", report(), flush=True)
    stop()
