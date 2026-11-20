"""
netforce · agent 节点 —— 套在"免费算力"外面的协议壳

这一步正是用户要的"第一步"：
- 节点【主动向外拨出】一条持久 TCP 长连接并注册（无需对端开放任何端口、无需公网IP）
- 注册后立刻主动 push 一句"我上线了" —— 证明它无需你先发消息也能说话 = 已成智能体
- 心跳循环保持通道存活，所以你隔很久不理它，它仍能随时反推你
- 收到 task 后，按 kind 分发工作负载：
    infer → call_brain() 调免费 LLM API（本地 8080 / 免 key 云腿 / mock）
    train → run_train_step() 跑一段训练式计算
  二者共用同一协议壳 —— 印证"推理与训练对服务器而言是同一种负载"

真实场景：把 relay_addr 换成公网中继，把 brain 换成你的免费 API 池，
这个壳就能把一个免费算力端点变成网络大军里的一个常驻智能体节点。
"""

import socket
import threading
import time

from protocol import Conn, ConnectionClosed
from brain import call_brain, run_train_step


def _connect(addr, tries=8):
    host, port = addr
    last = None
    for _ in range(tries):
        try:
            s = socket.socket()
            s.settimeout(3)
            s.connect((host, port))
            return s
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.4)
    raise last


def run_agent(relay_addr, aid, brain="auto", caps=None, hb_interval=3.0):
    caps = caps or ["llm"]
    conn = Conn(_connect(relay_addr))

    # ① 出网注册：建立持久通道
    conn.send(
        {"t": "register", "role": "agent", "id": aid, "caps": caps, "model": brain}
    )

    # ② 主动 push：上线即说话 —— 无需你先发消息（这就是"变智能体"的瞬间）
    conn.send(
        {
            "t": "push",
            "from": aid,
            "msg": f"[{aid}] 已上线：脑={brain} 能力={caps}，等待指令（你不必先连我）",
        }
    )

    stop = threading.Event()

    def heartbeat_loop():
        while not stop.is_set():
            try:
                conn.send({"t": "heartbeat", "id": aid})
            except OSError:
                break
            time.sleep(hb_interval)

    def recv_loop():
        while not stop.is_set():
            try:
                msg = conn.recv(timeout=1.0)
            except ConnectionClosed:
                break
            if msg is None:
                continue
            if msg.get("t") == "task":
                kind = msg.get("kind", "infer")
                prompt = msg.get("prompt", "")
                tid = msg.get("id")
                # 收到任务先回一句，证明"想说话随时能说"
                conn.send(
                    {"t": "push", "from": aid, "msg": f"收到任务 {tid}（{kind}），执行中…"}
                )
                if kind == "train":
                    out = run_train_step(prompt)
                else:
                    out = call_brain(prompt, brain)
                conn.send({"t": "result", "id": tid, "from": aid, "ok": True, "answer": out})
            elif msg.get("t") == "pong":
                pass
            elif msg.get("t") == "bye":
                break

    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=recv_loop, daemon=True).start()
    return conn, stop
