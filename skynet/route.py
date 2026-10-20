"""天网 · 路由层（route）—— 多重跳转，三层分开做
================================================
「多重跳转」其实是三件不同的事，混在一起实现必然出错，所以分层：

    ① 跳转链穿透 trace()      —— HTTP 层的多级 302/短链/跳板页，目标是「扒出真实地址」
    ② 多级代理链 ProxyChain   —— 网络层的出口串联 A→B→C，目标是「换出口、分散风控」
    ③ 节点中转 RelayFetcher   —— 自有节点的出口转发（netforce relay 协议），目标是「借别人的 IP 出去」

三者可以叠加：trace 负责「看清路径」，ProxyChain/RelayFetcher 负责「从哪儿出去」。

一、跳转链穿透为什么不能用 urllib 的自动跟随
    urllib 跟完 302 只给你终点，中间跳了几次、有没有 meta refresh 跳板、
    是不是跨了域（短链劫持的典型特征）全都看不见。而这几件事恰恰是判
    「这个地址值不值得抓」的关键。所以这里显式逐跳，每跳都留痕。

二、多级代理链为什么要本地起转发器
    HTTP 代理协议本身不支持「链式声明」（不能在 CONNECT 里写 A→B→C）。
    真正的多级只能靠本地串接：本机转发器接住 CONNECT，再向自己的上游发 CONNECT，
    上游再向它的上游发……每一级只知道自己下一跳，链路自然成立。
"""
from __future__ import annotations

import base64
import json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from . import net

SHORT_DOMAINS = {
    "bit.ly", "t.co", "t.cn", "dwz.cn", "tinyurl.com", "goo.gl", "is.gd", "v.gd",
    "ow.ly", "buff.ly", "rb.gy", "cutt.ly", "shorturl.at", "s.id", "u.nu",
    "suo.im", "url.cn", "mrw.so", "sourl.cn", "188web.com",
}
META_REFRESH = re.compile(
    r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]+content=["']?\s*\d+\s*;\s*url=([^"'>]+)""", re.I)
JS_LOCATION = re.compile(r"""(?:window\.|document\.)?location(?:\.href)?\s*=\s*["']([^"']{8,300})["']""", re.I)


# ================================================================ ① 跳转链穿透
@dataclass
class Hop:
    n: int
    url: str
    status: int
    location: str = ""
    kind: str = "http"          # http / meta / js / end
    host: str = ""
    set_cookie: int = 0

    def brief(self) -> str:
        return (f"{self.n}. [{self.status}] {self.kind:4s} {self.host:28s}"
                + (f" → {self.location[:60]}" if self.location else ""))


@dataclass
class Trace:
    start: str
    final: str = ""
    hops: list = field(default_factory=list)
    cross_domain: bool = False
    short: bool = False
    error: str = ""

    def brief(self) -> str:
        return (f"{len(self.hops)} 跳 → {self.final[:64]}"
                + ("（短链）" if self.short else "")
                + ("（跨域）" if self.cross_domain else "")
                + (f" ERR {self.error}" if self.error else ""))

    def table(self) -> str:
        return "\n".join(h.brief() for h in self.hops)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """让 3xx 变成异常而不是被静默跟随 —— 这样才拿得到每一跳的 Location。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _one_hop(url: str, timeout: float, use_proxy: bool = True):
    handlers = [_NoRedirect(), urllib.request.HTTPSHandler(context=net._CTX)]
    handlers.insert(0, urllib.request.ProxyHandler(
        {"http": net.PROXY, "https": net.PROXY} if use_proxy else {}))
    op = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={
        "User-Agent": net.UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    try:
        r = op.open(req, timeout=timeout)
        return r.status, dict(r.headers), r.read()[:200_000]
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), b""
    except Exception as e:                              # noqa: BLE001
        return 0, {}, str(e).encode()


def trace(url: str, max_hops: int = 10, timeout: float = 10.0,
          use_proxy: bool = True) -> Trace:
    """逐跳穿透，直到终点 / 达到上限 / 无更多跳转。"""
    t = Trace(start=url, final=url)
    t.short = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.") in SHORT_DOMAINS
    cur = url
    start_host = urllib.parse.urlparse(url).netloc.lower()
    seen = {cur}
    for n in range(1, max_hops + 1):
        status, headers, body = _one_hop(cur, timeout, use_proxy)
        host = urllib.parse.urlparse(cur).netloc.lower()
        loc = headers.get("Location") or headers.get("location") or ""
        kind = "http"
        if not loc and body and status < 400:
            # 跳板页：meta refresh / JS location 也算跳转
            txt = body.decode("utf-8", "ignore")[:20_000]
            m = META_REFRESH.search(txt)
            if m:
                loc, kind = m.group(1).strip(), "meta"
            else:
                m = JS_LOCATION.search(txt)
                if m:
                    loc, kind = m.group(1).strip(), "js"
        sc = len([k for k in headers if k.lower() == "set-cookie"])
        t.hops.append(Hop(n=n, url=cur, status=status, location=loc, kind=kind,
                          host=host, set_cookie=sc))
        if status == 0:
            t.error = body.decode("utf-8", "ignore")[:80]
            break
        if not loc:
            t.final = cur
            t.hops[-1].kind = "end"
            break
        nxt = urllib.parse.urljoin(cur, loc)
        if nxt in seen:                                 # 环路保护
            t.error = "redirect loop"
            t.final = nxt
            break
        seen.add(nxt)
        if urllib.parse.urlparse(nxt).netloc.lower() != start_host:
            t.cross_domain = True
        cur = nxt
        t.final = cur
    return t


# ================================================================ ② 多级代理链
class _ChainForwarder:
    """本地 CONNECT 转发器：接住 SOCKS 风格的 CONNECT，向自己的上游再发 CONNECT。

    每一级只认识自己的下一跳 —— 这就是链式代理不依赖协议扩展也能成立的原因。
    """

    def __init__(self, upstream_host: str, upstream_port: int):
        self.uh = upstream_host
        self.up = upstream_port
        self.sock: Optional[socket.socket] = None
        self.port = 0
        self._stop = threading.Event()
        self._threads: list = []

    def start(self) -> int:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        th = threading.Thread(target=self._accept_loop, daemon=True)
        th.start()
        self._threads.append(th)
        return self.port

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                client, _ = self.sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket):
        try:
            head = client.recv(65536)
            if not head:
                return
            first = head.split(b"\r\n", 1)[0].decode("latin-1", "ignore")
            if not first.upper().startswith("CONNECT "):
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                return
            upstream = socket.create_connection((self.uh, self.up), timeout=20)
            upstream.sendall(head)
            resp = upstream.recv(65536)
            client.sendall(resp)
            if b" 200 " not in resp.split(b"\r\n", 1)[0]:
                upstream.close()
                return
            self._pump(client, upstream)
        except Exception:
            pass
        finally:
            try:
                client.close()
            except Exception:
                pass

    @staticmethod
    def _pump(a: socket.socket, b: socket.socket):
        def pipe(src, dst):
            try:
                while True:
                    d = src.recv(65536)
                    if not d:
                        break
                    dst.sendall(d)
            except Exception:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass

        t = threading.Thread(target=pipe, args=(b, a), daemon=True)
        t.start()
        pipe(a, b)
        t.join(timeout=5)

    def stop(self):
        self._stop.set()
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass


class ProxyChain:
    """把 [代理1, 代理2, ...] 串成一条链路，返回可以喂给 urllib 的本地入口。

    用法：
        with ProxyChain(["http://127.0.0.1:3067"]) as pc:
            opener 走 pc.addr → 3067 → 目标

    单级时等价于直连代理；多级时自建转发器逐级串接（A→B→C）。
    """

    def __init__(self, proxies: list[str]):
        self.proxies = [p for p in proxies if p]
        self.entry = ""
        self._nodes: list = []

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    def start(self) -> str:
        if not self.proxies:
            self.entry = ""
            return ""
        # 从最后一跳往前串：最后一级的上游是真正的远端代理
        upstream = self.proxies[-1]
        self.entry = upstream
        for p in reversed(self.proxies[:-1]):
            hp = urllib.parse.urlsplit(p)
            fw = _ChainForwarder(hp.hostname or "127.0.0.1", hp.port or 8080)
            port = fw.start()
            self._nodes.append(fw)
            self.entry = f"http://127.0.0.1:{port}"
        return self.entry

    def stop(self):
        for n in self._nodes:
            n.stop()
        self._nodes.clear()

    def fetch(self, url: str, timeout: float = 12.0) -> net.FetchResult:
        entry = self.entry
        if not entry:
            return net.fetch_text(url, timeout=timeout, budget=timeout + 5)
        handlers = [_NoRedirect(), urllib.request.HTTPSHandler(context=net._CTX),
                    urllib.request.ProxyHandler({"http": entry, "https": entry})]
        op = urllib.request.build_opener(*handlers)
        t0 = time.monotonic()
        try:
            r = op.open(urllib.request.Request(url, headers={"User-Agent": net.UA}), timeout=timeout)
            raw = net._decompress(r.read(), r.headers.get("Content-Encoding", ""))
            return net.FetchResult(url=url, ok=True, status=r.status, size=len(raw),
                                   final_url=r.geturl(), channel=f"chain{len(self.proxies)}",
                                   data=raw, text=raw.decode("utf-8", "ignore"),
                                   elapsed=round(time.monotonic() - t0, 3))
        except Exception as e:                          # noqa: BLE001
            return net.FetchResult(url=url, ok=False, channel=f"chain{len(self.proxies)}",
                                   elapsed=round(time.monotonic() - t0, 3),
                                   error=f"{type(e).__name__}: {str(e)[:60]}")


# ================================================================ ③ 节点中转
@dataclass
class RelayRequest:
    """发给 relay/agent 节点的取页任务，复用 netforce 的 line-JSON 协议。

    agent 端只需实现 kind == "fetch"：
        r = skynet.net.fetch_text(url)
        回 result{id, from, ok, status, body_b64}
    """
    rid: str
    url: str

    def to_msg(self) -> dict:
        return {"type": "task", "id": self.rid, "to": "any",
                "kind": "fetch", "url": self.url}


class RelayFetcher:
    """经自有节点出口取页。

    与 ProxyChain 的区别：这里借的是**别人机器的 IP**，本机不需要连得上目标站；
    适合目标站按地区/ASN 封锁、或本机出口已被打上标记的情况。

    未接节点时 can_relay() 返回 False，调用方自动回落到直连/代理链 —— 不阻塞主流程。
    """

    def __init__(self, relay_host: str = "127.0.0.1", relay_port: int = 9000,
                 agent_id: str = "any", timeout: float = 25.0):
        self.addr = (relay_host, relay_port)
        self.agent_id = agent_id
        self.timeout = timeout

    def available(self) -> bool:
        try:
            with socket.create_connection(self.addr, timeout=2):
                return True
        except Exception:
            return False

    def fetch(self, url: str) -> net.FetchResult:
        """走节点取页。协议不可达就返回失败，由上层降级。"""
        if not self.available():
            return net.FetchResult(url=url, channel="relay", ok=False, error="relay offline")
        rid = f"f{int(time.time() * 1000)}"
        try:
            sock = socket.create_connection(self.addr, timeout=8)
            sock.settimeout(self.timeout)
            send = json.dumps(RelayRequest(rid, url).to_msg(), ensure_ascii=False) + "\n"
            sock.sendall(send.encode("utf-8"))
            buf = b""
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    msg = json.loads(line.decode("utf-8", "ignore"))
                    if msg.get("type") == "result" and msg.get("id") == rid:
                        body = base64.b64decode(msg.get("body_b64") or b"")
                        return net.FetchResult(url=url, ok=bool(msg.get("ok")),
                                               status=msg.get("status", 0), size=len(body),
                                               channel="relay", data=body,
                                               text=body.decode("utf-8", "ignore"))
            sock.close()
            return net.FetchResult(url=url, channel="relay", ok=False, error="relay timeout")
        except Exception as e:                          # noqa: BLE001
            return net.FetchResult(url=url, channel="relay", ok=False,
                                   error=f"{type(e).__name__}: {str(e)[:50]}")


# ================================================================ 统一入口
def fetch_any(url: str, proxies: Optional[list[str]] = None,
              relay: Optional[RelayFetcher] = None, timeout: float = 12.0) -> net.FetchResult:
    """按「节点 → 代理链 → 直连」的顺序择优，前一级失败自动降级。"""
    if relay is not None and relay.available():
        r = relay.fetch(url)
        if r.ok:
            return r
    if proxies:
        with ProxyChain(proxies) as pc:
            r = pc.fetch(url, timeout=timeout)
            if r.ok:
                return r
    return net.fetch_text(url, timeout=timeout, budget=timeout + 8)
