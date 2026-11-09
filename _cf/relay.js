// Netforce Relay —— Cloudflare Worker 版 TCP-over-WebSocket 出口
// 与本地 skynet-fleet/tools/cloudnode/wsrelay.py 的 /tunnel 协议逐字节对齐：
//   1. 连接 wss://<host>/tunnel?k=<hmac>&host=&port=（k 可为空则首帧给目标）
//   2. 客户端发文本帧 {"host": "...", "port": 443}
//   3. 服务端回文本帧 {"ok": true} 或 {"ok": false, "err": "..."}
//   4. 之后双向二进制帧，即原始 TCP 字节流
// 鉴权：k = HMAC-SHA256(key=RELAY_SECRET, msg="tunnel").hex()[:32]
// Secret：Worker 环境变量 RELAY_SECRET（与本地 WSRELAY_SECRET 相同）

import { connect } from 'cloudflare:sockets';

const ROOM = 'tunnel';

function toHex(buf) {
  const b = new Uint8Array(buf);
  let s = '';
  for (let i = 0; i < b.length; i++) s += b[i].toString(16).padStart(2, '0');
  return s;
}

async function pairToken(secret, room) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw', enc.encode(String(secret)),
    { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']
  );
  const sig = await crypto.subtle.sign('HMAC', key, enc.encode(String(room)));
  return toHex(sig).slice(0, 32);
}

// 定长比较，避免按字节提前返回泄露时序
function safeEqual(a, b) {
  const x = String(a || '');
  const y = String(b || '');
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x.charCodeAt(i) ^ y.charCodeAt(i);
  return diff === 0;
}

// 注意：Workers 里 send(string) 才是文本帧，send(Uint8Array/ArrayBuffer) 是二进制帧。
// 本地客户端 wsrelay.py 要求握手应答必须是文本帧，这里只能给字符串。
function textFrame(obj) {
  return JSON.stringify(obj);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === '/' || url.pathname === '/health') {
      return new Response('NETFORCE-CF-RELAY/1.1', { status: 200 });
    }

    if (url.pathname !== '/tunnel') {
      return new Response('not found', { status: 404 });
    }

    const secret = (env && env.RELAY_SECRET) || '';
    if (!secret) {
      return new Response('relay has no secret', { status: 503 });
    }

    const expected = await pairToken(secret, ROOM);
    if (!safeEqual(url.searchParams.get('k'), expected)) {
      return new Response('forbidden', { status: 403 });
    }

    if ((request.headers.get('Upgrade') || '').toLowerCase() !== 'websocket') {
      return new Response('need websocket', { status: 426 });
    }

    // 目标可以从查询串预置，也可以等首帧
    let targets = {
      host: url.searchParams.get('host') || '',
      port: Number(url.searchParams.get('port') || 0) || 0,
    };

    const pair = new WebSocketPair();
    const client = pair[0];
    const server = pair[1];
    server.accept();

    let socket = null;
    let writer = null;
    let ready = false;
    let closing = false;

    const shutdown = () => {
      if (closing) return;
      closing = true;
      try { server.close(1000, 'bye'); } catch (e) {}
      try { if (socket) socket.close(); } catch (e) {}
    };

    const openTo = async (host, port) => {
      socket = connect({ hostname: host, port: port });
      await socket.opened;
      writer = socket.writable.getWriter();
      ready = true;

      // 远端 -> 客户端
      (async () => {
        try {
          const reader = socket.readable.getReader();
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            if (value && value.byteLength) server.send(value);
          }
        } catch (e) {
          // 远端异常断开
        } finally {
          shutdown();
        }
      })();

      server.send(textFrame({ ok: true }));
    };

    server.addEventListener('message', async (ev) => {
      try {
        if (!ready) {
          if (!targets.host) {
            const text = typeof ev.data === 'string'
              ? ev.data
              : new TextDecoder().decode(ev.data);
            const cfg = JSON.parse(text);
            targets = {
              host: String(cfg.host || ''),
              port: Number(cfg.port || 443) || 443,
            };
            if (!targets.host) throw new Error('missing host');
          }
          await openTo(targets.host, targets.port);
          return;
        }

        if (writer && ev.data) {
          const buf = typeof ev.data === 'string'
            ? new TextEncoder().encode(ev.data)
            : ev.data;
          await writer.write(new Uint8Array(buf));
        }
      } catch (e) {
        try { server.send(textFrame({ ok: false, err: String((e && e.message) || e) })); } catch (_) {}
        shutdown();
      }
    });

    server.addEventListener('close', shutdown);
    server.addEventListener('error', shutdown);

    return new Response(null, { status: 101, webSocket: client });
  }
};
