"""
netforce · 中继(relay) —— 第一步的控制/会合节点

职责：
- 接受任意节点(agent)的【出网长连接】并登记能力指纹(caps)
- 维护心跳：超时未心跳的 agent 判死，通知操作员
- 路由任务：操作员发 task{to:agent_id} → 转发给对应 agent
- 转发回推：agent 的 result / push 原样转给所有在线操作员
- 能力无关注册：推理(llm) / 训练(train) 节点共用同一套协议，relay 不区分

一句话：relay 就是"两端都在 NAT/防火墙后、只敢出网时"的会合点。
真实跨互联网部署时，把本机的 127.0.0.1:9000 换成一台有公网地址的服务器即可，
协议逻辑一行不用改（这也是用户"只要能联网就能传数据"主张的落点）。
"""

import socket
import threading
import time

from protocol import Conn, ConnectionClosed


class Relay:
    def __init__(self, host="127.0.0.1", port=9000, hb_timeout=10):
        self.addr = (host, port)
        self.hb_timeout = hb_timeout
        self.agents = {}  # id -> {"conn","caps","model","last"}
        self.agents_lock = threading.Lock()
        self.operators = []  # list[Conn]
        self.ops_lock = threading.Lock()
        self.running = True
        self._log = []
        self.log_lock = threading.Lock()
        self.srv = None

    def log(self, s):
        line = f"[{time.strftime('%H:%M:%S')}] {s}"
        with self.log_lock:
            self._log.append(line)
        print(line)

    def start(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(self.addr)
        self.srv.listen(64)
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()
        self.log(f"RELAY 监听 {self.addr[0]}:{self.addr[1]}（会合点就绪）")

    def _accept_loop(self):
        while self.running:
            try:
                c, a = self.srv.accept()
            except OSError:
                break
            conn = Conn(c)
            threading.Thread(
                target=self._handle, args=(conn, a), daemon=True
            ).start()

    def _handle(self, conn, addr):
        peer = addr[0]
        try:
            while True:
                msg = conn.recv(timeout=1.0)
                if msg is None:
                    continue
                self._dispatch(conn, msg, peer)
        except ConnectionClosed:
            pass
        except Exception as e:  # noqa: BLE001
            self.log(f"连接异常 {peer}: {e}")
        finally:
            self._drop(conn)

    def _dispatch(self, conn, msg, peer):
        t = msg.get("t")

        if t == "register":
            role = msg.get("role", "agent")
            if role == "agent":
                aid = msg.get("id")
                with self.agents_lock:
                    self.agents[aid] = {
                        "conn": conn,
                        "caps": msg.get("caps", []),
                        "model": msg.get("model", ""),
                        "last": time.time(),
                    }
                self.log(
                    f"AGENT 上线: {aid} caps={msg.get('caps')} model={msg.get('model')}"
                )
                self._notify_ops(
                    {
                        "t": "event",
                        "event": "agent_online",
                        "id": aid,
                        "caps": msg.get("caps"),
                        "model": msg.get("model"),
                    }
                )
            else:  # operator
                with self.ops_lock:
                    self.operators.append(conn)
                self.log(f"OPERATOR 接入 {peer}")

        elif t == "heartbeat":
            aid = msg.get("id")
            with self.agents_lock:
                if aid in self.agents:
                    self.agents[aid]["last"] = time.time()
            try:
                conn.send({"t": "pong", "id": aid})
            except OSError:
                pass

        elif t == "task":
            to = msg.get("to")
            with self.agents_lock:
                ag = self.agents.get(to)
            if ag:
                ag["conn"].send(msg)
                self.log(f"TASK → {to}: {str(msg.get('prompt'))[:40]}")
            else:
                try:
                    conn.send({"t": "error", "msg": f"agent {to} 不存在"})
                except OSError:
                    pass

        elif t in ("result", "push"):
            src = msg.get("from") or msg.get("id")
            snippet = str(msg.get("answer") or msg.get("msg") or msg.get("workload"))
            tag = "RESULT" if t == "result" else "PUSH  "
            self.log(f"{tag} ← {src}: {snippet[:60]}")
            self._notify_ops(msg)

        elif t == "bye":
            self._drop(conn)

        else:
            self.log(f"未知消息类型 {t}")

    def _notify_ops(self, msg):
        with self.ops_lock:
            for op in list(self.operators):
                try:
                    op.send(msg)
                except OSError:
                    pass

    def _watchdog(self):
        while self.running:
            time.sleep(2)
            now = time.time()
            dead = []
            with self.agents_lock:
                for aid, a in list(self.agents.items()):
                    if now - a["last"] > self.hb_timeout:
                        dead.append(aid)
            for aid in dead:
                with self.agents_lock:
                    a = self.agents.pop(aid, None)
                if a:
                    try:
                        a["conn"].close()
                    except OSError:
                        pass
                self.log(f"AGENT 失联(心跳超时): {aid}")
                self._notify_ops(
                    {"t": "event", "event": "agent_offline", "id": aid}
                )

    def _drop(self, conn):
        with self.agents_lock:
            for aid, a in list(self.agents.items()):
                if a["conn"] is conn:
                    del self.agents[aid]
                    self.log(f"AGENT 下线: {aid}")
                    self._notify_ops(
                        {"t": "event", "event": "agent_offline", "id": aid}
                    )
        with self.ops_lock:
            if conn in self.operators:
                self.operators.remove(conn)

    def stop(self):
        self.running = False
        try:
            self.srv.close()
        except OSError:
            pass
