"""
netforce · 演示（第一步闭环证明）

同时拉起：1 个中继 + 2 个 agent（一个推理节点 / 一个训练节点）+ 1 个操作员。
证明用户要的那条链路：
  你(操作员)发指令 → agent 心跳保活 → agent 调免费算力(脑) → 它反推结果 → 还能主动 push
推理节点与训练节点共用同一协议壳，印证"推理/训练对服务器是同一种负载"。
结束后自动关闭所有常驻连接。
"""

import socket
import threading
import time
import uuid

from protocol import Conn, ConnectionClosed
from relay import Relay
from agent import run_agent


def op_connect(addr, tries=8):
    host, port = addr
    for _ in range(tries):
        try:
            s = socket.socket()
            s.settimeout(3)
            s.connect((host, port))
            return Conn(s)
        except OSError:
            time.sleep(0.3)
    raise RuntimeError("连不上中继")


def main():
    relay = Relay(port=9000, hb_timeout=12)
    relay.start()
    time.sleep(0.4)

    # 两个 agent：推理节点 + 训练节点，共用同一协议壳
    a_infer, s_infer = run_agent(
        ("127.0.0.1", 9000), "agent-α(推理)", brain="auto", caps=["llm"]
    )
    a_train, s_train = run_agent(
        ("127.0.0.1", 9000), "agent-β(训练)", brain="auto", caps=["train", "llm"]
    )
    time.sleep(0.6)

    op = op_connect(("127.0.0.1", 9000))
    op.send({"t": "register", "role": "operator"})
    time.sleep(0.3)

    print("\n========== 操作员发任务 ==========")
    tasks = [
        ("agent-α(推理)", "infer", "用一句话证明你是一个联网保活、能主动反推的智能体节点"),
        ("agent-β(训练)", "train", "对一小批样本做一轮收敛，回报 loss 变化"),
    ]
    for to, kind, prompt in tasks:
        tid = str(uuid.uuid4())[:8]
        op.send({"t": "task", "id": tid, "to": to, "kind": kind, "prompt": prompt})

    # 收结果（result 算数，push 顺手打印）
    got = 0
    deadline = time.time() + 60
    while got < 2 and time.time() < deadline:
        try:
            m = op.recv(timeout=2.0)
        except ConnectionClosed:
            break
        if not m:
            continue
        if m.get("t") == "result":
            got += 1
            print(f"\n【RESULT】任务 {m.get('id')} ← {m.get('from')}")
            print(f"   答案: {m.get('answer')}")
        elif m.get("t") in ("push", "event"):
            print(f"【{m.get('t').upper()}】 {m.get('from') or m.get('id')}: {m.get('msg') or m.get('event')}")

    print("\n========== 演示结束，关闭全部连接 ==========")
    s_infer.set()
    s_train.set()
    a_infer.close()
    a_train.close()
    op.close()
    relay.stop()
    print("已关闭。后台常驻服务：无。")


if __name__ == "__main__":
    main()
