# -*- coding: utf-8 -*-
"""
netforce / discovery.py  v2
网络通用协议 —— 发现层（Discovery Layer）最小参考实现

【为什么不是"扫一遍互联网"】
  用户原话："总不至于扫一遍互联网吧，那逻辑不成立"。完全正确：
  ① 扫全网（42 亿 IPv4）成本爆炸、必被墙/限流；
  ② 国内未授权端口扫描本身踩合规红线；
  ③ 真正要的是"能发现资源站的能力"——而发现靠的是【让站自己亮相 + 查目录】，不是盲扫。
  所以本模块主机制是：资源站主动 register（报名）→ 发现方 lookup（按 tag 查）→ 直接连交互。
  "主动握手探测"（scan）只作为【已有线索时】的补充，绝不盲扫全网。

【四种发现方式，全部不依赖人工点浏览器/搜搜索框】
  1) 注册+目录查询（核心，替代扫全网）：资源站跑咱协议 → 启动即拨 rendezvous 注册(tags) →
     发现方按 tag 查 rendezvous → 拿到地址 → 直接连。这就是"直接在互联网上找到那个站"。
  2) gossip 蔓延：节点握手 ACK 互带 known_peers，连一个蔓延到全网。
  3) rendezvous 会合点：同上 register/lookup 的载体（轻量目录服务）。
  4) bootstrap 种子：rendezvous 自身靠少量硬编码/动态种子起步（少而精，不扫）。
  5) 线索探测 scan()：仅当你【已经知道某 IP/域名】时才探，验证它是否跑咱协议（magic 校验）。

所有节点共用 magic=PCLI + 固定端口，所以"谁回应 PCLI 握手谁就是自家人"——
这正是能被目录/同伴识别、又不会误认普通网站的先天指纹。
"""
import asyncio
import json
import struct
import time
from pathlib import Path

# ---- 协议指纹（所有 netforce 节点共用，才能互相识别） ----
MAGIC = b"PCLI"          # 0x50434C49
VERSION = 1
T_DISCOVER = 1           # 主动握手探测
T_ACK = 2                # 握手回应（带 known_peers = gossip）
T_REGISTER = 3           # 资源站向 rendezvous 报名
T_LOOKUP = 4             # 发现方按 tag 查 rendezvous
T_LOOKUP_RES = 5         # rendezvous 返回节点列表
T_MSG = 6                # 发现后直接交互的普通消息
DEFAULT_PORT = 8443      # 节点握手端口
RV_PORT = 8444           # rendezvous 目录服务端口
PEER_FILE = Path(__file__).with_name("known_peers.json")
SEED_FILE = Path(__file__).with_name("seeds.json")


def _pack(msg_type: int, body: dict) -> bytes:
    body_b = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return MAGIC + bytes([VERSION, msg_type]) + struct.pack(">I", len(body_b)) + body_b


def _unpack(data: bytes):
    if not data or len(data) < 10 or data[:4] != MAGIC:
        return None  # 非 PCLI 包 → 不是自家人（也是"扫站"时区分节点与普通服务的关键）
    ver, mtype = data[4], data[5]
    (blen,) = struct.unpack(">I", data[6:10])
    try:
        body = json.loads(data[10:10 + blen].decode("utf-8"))
    except Exception:
        return None
    return ver, mtype, body


# ============================ rendezvous 目录服务（资源站在此"亮相"） ============================
class Rendezvous:
    """轻量目录服务：资源站 register(tag,addr)，发现方 lookup(tag) 拿地址。
    真实部署时它是公网独立服务；本文件用同进程模拟以跑通实证。"""

    def __init__(self):
        self.registry = {}   # tag -> [(node_id, addr), ...]

    async def serve(self, port: int = RV_PORT):
        async def handler(reader, writer):
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=5)
                p = _unpack(data)
                if p and p[1] == T_REGISTER:
                    for tag in p[2].get("tags", []):
                        self.registry.setdefault(tag, [])
                        if (p[2]["id"], p[2]["addr"]) not in self.registry[tag]:
                            self.registry[tag].append((p[2]["id"], p[2]["addr"]))
                    writer.write(_pack(T_ACK, {"ok": True, "tags": p[2].get("tags", [])}))
                elif p and p[1] == T_LOOKUP:
                    tag = p[2]["tag"]
                    writer.write(_pack(T_LOOKUP_RES, {"tag": tag, "nodes": self.registry.get(tag, [])}))
                else:
                    writer.write(_pack(T_ACK, {"ok": False}))
                await writer.drain()
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                except Exception:
                    pass
        srv = await asyncio.start_server(handler, "0.0.0.0", port)
        async with srv:
            await srv.serve_forever()


async def register(rv_host: str, rv_port: int, node_id: str, addr: str, tags: list):
    """资源站主动亮相：把自己登记进 rendezvous 的 tag 目录。"""
    r, w = await asyncio.wait_for(asyncio.open_connection(rv_host, rv_port), timeout=3)
    w.write(_pack(T_REGISTER, {"id": node_id, "addr": addr, "tags": tags}))
    await w.drain()
    await r.read(4096)
    w.close()


async def lookup(rv_host: str, rv_port: int, tag: str):
    """发现方按 tag 查目录，直接拿到同组资源站地址——零扫描。"""
    r, w = await asyncio.wait_for(asyncio.open_connection(rv_host, rv_port), timeout=3)
    w.write(_pack(T_LOOKUP, {"tag": tag}))
    await w.drain()
    data = await r.read(4096)
    w.close()
    p = _unpack(data)
    return p[2]["nodes"] if p and p[1] == T_LOOKUP_RES else []


# ============================ 节点：监听 + gossip + 线索探测 ============================
class Discovery:
    def __init__(self, self_id: str, port: int = DEFAULT_PORT, peers_file=PEER_FILE):
        self.self_id = self_id
        self.port = port
        self.peers_file = Path(peers_file)
        self.known = self._load()
        self._server = None

    def _load(self) -> dict:
        try:
            return json.load(open(self.peers_file, encoding="utf-8"))
        except Exception:
            return {}

    def save(self):
        json.dump(self.known, open(self.peers_file, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

    def bootstrap_seeds(self, seed_file=SEED_FILE):
        """第一跳锚点：读那几个关键域名/节点（互联网的'灯塔'），作为发现的起步。
        之后靠 register/lookup + gossip 蔓延，不必扫全网、不必依赖 AI 搜索覆盖。
        用户原话：小众资源站'要么自建、要么国内、要么国外，就那几个域名'。"""
        try:
            seeds = json.load(open(seed_file, encoding="utf-8"))
        except Exception:
            seeds = []
        for s in seeds:
            addr = s.get("addr")
            if addr and addr not in self.known:
                self.known[addr] = {"id": s.get("name"), "seed": True,
                                    "type": s.get("type"), "seen": 0}
        self.save()
        return seeds

    async def serve(self):
        """被动被发现 + 被发现后直接交互：监听握手端口，收到 DISCOVER 回 ACK（带 known_peers
        = gossip 蔓延）；收到 T_MSG 业务消息则应答（这就是'发现后才能交互'的落地）。"""
        async def handler(reader, writer):
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=5)
                p = _unpack(data)
                if not p:
                    writer.close(); return
                mtype = p[1]
                if mtype == T_DISCOVER:
                    ack = _pack(T_ACK, {
                        "id": self.self_id, "port": self.port,
                        "peers": list(self.known.keys())[:50],
                    })
                    writer.write(ack)
                elif mtype == T_MSG:
                    # 发现后直接交互：回显业务消息（真实场景这里可路由给本地智能体/资源）
                    writer.write(_pack(T_MSG, {
                        "from": self.self_id,
                        "text": "【%s 已收到】%s" % (self.self_id, p[2].get("text", "")),
                    }))
                else:
                    writer.write(_pack(T_ACK, {"ok": False}))
                await writer.drain()
            except Exception:
                pass
            finally:
                try: writer.close()
                except Exception: pass
        self._server = await asyncio.start_server(handler, "0.0.0.0", self.port)
        async with self._server:
            await self._server.serve_forever()

    async def probe(self, host: str, port: int = None):
        """线索探测：仅当你【已知某 IP/域名】时，发 PCLI 握手验证它是否自家人。"""
        port = port or self.port
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=3)
            w.write(_pack(T_DISCOVER, {"id": self.self_id, "port": self.port}))
            await w.drain()
            data = await asyncio.wait_for(r.read(4096), timeout=3)
            p = _unpack(data)
            w.close()
            if p and p[1] == T_ACK:
                peer_id, peers = p[2].get("id"), p[2].get("peers", [])
                addr = f"{host}:{port}"
                self.known[addr] = {"id": peer_id, "seen": time.time()}
                for pp in peers:  # gossip 扩散
                    if pp not in self.known:
                        self.known[pp] = {"id": None, "seen": 0, "via_gossip": True}
                self.save()
                return addr, peer_id, peers
        except Exception:
            return None
        return None

    async def interact(self, host: str, port: int, text: str):
        """发现后直接交互：向资源站发一条普通消息，收回应。"""
        r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=3)
        w.write(_pack(T_MSG, {"from": self.self_id, "text": text}))
        await w.drain()
        data = await asyncio.wait_for(r.read(4096), timeout=3)
        w.close()
        p = _unpack(data)
        return p[2] if p and p[1] == T_MSG else None


# ============================ 实证：不扫全网，直接在互联网上找到资源站并交互 ============================
async def demo():
    print("== netforce 发现层 v2 实证：注册+目录查询+gossip（非扫全网）==\n")
    rv = Rendezvous()
    rv_task = asyncio.create_task(rv.serve(8484))
    await asyncio.sleep(0.2)

    # 1) 资源站（小众个人博客）启动，主动向 rendezvous 报名："我是 AI 资源站"
    res = Discovery("blog-ai-zhang", port=8490)
    res_task = asyncio.create_task(res.serve())
    await asyncio.sleep(0.2)
    await register("127.0.0.1", 8484, "blog-ai-zhang", "127.0.0.1:8490",
                   ["ai-resource", "blog", "free-llm"])
    print("[资源站] blog-ai-zhang 已在 rendezvous 报名 tags=[ai-resource,blog,free-llm]")

    # 2) 发现方：不扫全网，直接按 tag 查目录 → 拿到资源站地址
    nodes = await lookup("127.0.0.1", 8484, "ai-resource")
    print("[发现方] 按 tag=ai-resource 查目录，命中:", nodes)

    # 3) 直接连资源站交互（发消息 → 收回应），完成"发现后才能交互"
    finder = Discovery("finder")
    seeds = finder.bootstrap_seeds()
    print("[第一跳] 节点启动读种子锚点（那几个关键域名）:", [s.get("name") for s in seeds])
    if nodes:
        host, port = nodes[0][1].split(":")
        # 先握手（gossip 互相认识），再发业务消息
        await finder.probe(host, int(port))
        reply = await finder.interact(host, int(port), "你好，能给我一份免费 LLM 清单吗？")
        print("[发现方→资源站] 直接交互，资源站回应:", reply)

    # 4) gossip 演示：再起第二个资源站，注册后让 finder 探它，互相扩散同伴
    res2 = Discovery("wiki-crypto", port=8491)
    res2_task = asyncio.create_task(res2.serve())
    await asyncio.sleep(0.2)
    await register("127.0.0.1", 8484, "wiki-crypto", "127.0.0.1:8491", ["crypto", "wiki"])
    # finder 探 wiki-crypto，wiki 的 ACK 带它已知的 peers → finder 拿到更多
    r2 = await finder.probe("127.0.0.1", 8491)
    print("[gossip] finder 探 wiki-crypto，对方已知同伴:", r2[2] if r2 else None)
    print("\nfinder 当前 known_peers:", list(finder.known.keys()))

    finder.save()
    rv_task.cancel(); res_task.cancel(); res2_task.cancel()
    print("\n实证完成：发现靠【报名+查目录】，不是扫全网；发现后即可直接交互。")


if __name__ == "__main__":
    asyncio.run(demo())
