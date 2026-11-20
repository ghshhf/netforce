"""
netforce · 协议层（第一步最小实现）

核心思想（对齐用户构想）：
- 云端 API 背后 = 服务器进程；推理与训练对服务器而言都是"输入→计算→输出"的工作负载，
  协议壳不区分二者，只认对端上报的能力指纹(caps)。
- 节点(agent)主动向外拨出一条持久 TCP 长连接并注册；中继(relay)经此通道向它派活。
- 心跳(heartbeat)保持通道存活；对端借此可随时主动 push（无需你先发消息）→ 被动 API 变主动智能体。
- 传输用换行分隔的 JSON（line protocol），零第三方依赖，最直观地体现
  "只要能联网、能收发字节，就能传数据、能交互"这一条。

消息类型：
  register  {role:agent|operator, id, caps:[...], model}
  heartbeat {id}
  pong      {id}
  task      {id, to, kind:infer|train, prompt}
  result    {id, from, ok, answer|workload...}
  push      {from, msg}           # 对端主动说话 = 已成为智能体
  event     {event:agent_online|agent_offline, id, caps}
  error     {msg}
  bye
"""

import json
import socket
import threading


DELIM = b"\n"


class ConnectionClosed(Exception):
    pass


def send_msg(sock, msg: dict):
    data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
    sock.sendall(data)


class LineReader:
    """按换行切分的 JSON 行读取器，处理半包/粘包。"""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def read_msg(self, timeout=None):
        self.sock.settimeout(timeout)
        while True:
            if b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                if not line.strip():
                    continue
                return json.loads(line.decode("utf-8"))
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return None
            except OSError:
                raise ConnectionClosed()
            if not chunk:
                raise ConnectionClosed()
            self.buf += chunk


class Conn:
    """带写锁的连接包装，多线程可安全发送。"""

    def __init__(self, sock):
        self.sock = sock
        self.reader = LineReader(sock)
        self._wlock = threading.Lock()

    def send(self, msg: dict):
        with self._wlock:
            send_msg(self.sock, msg)

    def recv(self, timeout=None):
        return self.reader.read_msg(timeout)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
