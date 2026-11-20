"""
netforce · 脑/算力层（直接对接免费 API 池）

按用户要求：第一步绝不能只用本机 mock，必须真去调"免费算力"。
call_brain 按优先级打真实免费端点（详见 TOPIC-free-compute-apis.md §十二/§十四）：
  1) 本机 8080 本地模型（Qwen3.5-4B-UD-Q4，无 key，直连）
  2) 免 key 四家（直连免代理）：pollinations / llm7-anon / ovh / kilo
只有【全部不可达】才落 mock，并明确标注"未连通真实算力"——绝不悄悄假装。
run_train_step 仍是训练负载示例（证明推理/训练共用同一协议壳）。

⚠️ 架构事实：免费 LLM API 端点本身是无状态请求-应答，不能保持心跳/主动反推；
它在这里的定位是"脑"，由 agent 进程（壳）包裹调用。agent 进程跑在哪台免费算力宿主上，
哪台就是"节点"。localhost 只是证明协议用的占位宿主。
"""

import json
import urllib.request

LOCAL_URL = "http://127.0.0.1:8080/v1/chat/completions"

# (url, model, auth_style, auth_val)  —— 免 key 四家，直连免代理（见免费算力清单 §十二）
ANON = [
    ("https://text.pollinations.ai/v1/chat/completions", "openai", "none", None),
    ("https://api.llm7.io/v1/chat/completions", "mistral-Nemo-Instruct-2407", "none", None),
    ("https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions", "Mistral-Nemo-Instruct-2407", "none", None),
    ("https://api.kilo.ai/api/gateway/chat/completions", "kilo-auto/free", "x-api-key", "netforce"),
]


def _chat(url, model, auth_style, auth_val, prompt, timeout=8):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if auth_style == "x-api-key":
        headers["X-Api-Key"] = auth_val
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        js = json.loads(r.read().decode("utf-8"))
    return js["choices"][0]["message"]["content"]


def call_brain(prompt: str, mode: str = "auto") -> str:
    """按 free-pool 优先级真打免费端点；全不可达才 mock。"""
    tried = []
    # 1) 本机 8080 本地模型（你的免费本地算力，§十四）
    try:
        return _chat(LOCAL_URL, "local", "none", None, prompt, timeout=5)
    except Exception as e:  # noqa: BLE001
        tried.append(f"local8080:{e!r}")
    # 2) 免 key 云端四家（直连免代理，§十二）
    for url, model, style, val in ANON:
        try:
            return _chat(url, model, style, val, prompt, timeout=6)
        except Exception as e:  # noqa: BLE001
            tried.append(f"{model}:{e!r}")
    return (
        f"[mock-brain] 真实免费算力全部不可达（{';'.join(tried)}）"
        "—— 在能联网 / 8080 起着的机器上即变真脑，此 mock 仅为协议演示兜底"
    )


def run_train_step(prompt: str, steps: int = 5) -> dict:
    """训练负载示例：把 w 往 0.7 收敛，回报 loss 变化。纯计算，无外部依赖。

    印证用户判断：推理与训练对服务器而言是同一种负载——只是算的东西不同，
    二者共用 netforce 同一协议壳，relay 按 caps 派活、不区分。"""
    import random

    w = random.random()
    loss0 = abs(w - 0.7)
    for _ in range(steps):
        w -= 0.1 * (w - 0.7)
    loss1 = abs(w - 0.7)
    return {
        "ok": True,
        "workload": "train",
        "loss_start": round(loss0, 4),
        "loss_end": round(loss1, 4),
        "steps": steps,
        "note": prompt[:30],
    }
