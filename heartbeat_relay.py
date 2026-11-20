#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
heartbeat_relay.py —— 服务器级共享黑板 · 多智能体讨论引擎（按用户方法实现）

方法来源（用户原话定调，2026-10-16）：
  1. 每个问题给一个文件夹：problems/<id>/，所有智能体往同一文件夹写。
  2. 他们能共同写一个文件夹 → 服务器级共享黑板 + 多轮交流：智能体自己读同伴写的、
     再回应，主进程不转发。
  3. 不限制智能体数量：运行时动态枚举免费模型池（几十个），不写死。
  4. 免费层优先：本地模型（离线·无密钥）+ pollinations 免 key 云腿 + 已配 key 的公益站。
  不设限制铁律：循环无轮数上限，只在「连续 6 轮黑板无新内容（真收敛）」才收口。
  隐私闸门：含权重路径/真实密钥/宪法原文等私有资产的文本，不外投给云端 agent。

queue.json 是任务队列：每题 {id, prompt, kind, status, folder}；
心跳每 interval 秒重读一遍，捡 pending 的执行；外部把 status 改成 paused 即干净暂停（不杀进程）。

用法：
  python heartbeat_relay.py serve --interval 60        # 常驻心跳（捡 pending 跑）
  python heartbeat_relay.py run <id>                   # 直接跑某一题（测试用）
  python heartbeat_relay.py run <id> --no-tools        # 关掉工具层（纯文本讨论）
  python heartbeat_relay.py enqueue "你的问题"         # 入队一道题
  python heartbeat_relay.py enqueue --batch problems_batch.md
  python heartbeat_relay.py agents                     # 列出当前可用的免费 agent 池
  python heartbeat_relay.py status                     # 看队列状态

工具层（2026-10-17 并入）：每个 agent 每轮都能真的动手——
    [EXEC]python代码[/EXEC]  真跑（子进程·限时·锁在本题工作区·危险命令黑名单）
    [TASK:目标]任务[/TASK]    真向另一个 API（另一个 agent）派活，拿回它的真实回答
    [WRITE]/[READ]/[LIST]     在本题共享工作区写/读/列文件 → 文件级共享黑板
    [DONE]结论[/DONE]
工作区 = problems/<id>/work，全题所有 agent 共用：你写的文件同伴下一轮能读到、能改、能跑。
"""
from __future__ import annotations
import os, sys, re, json, time, glob, hashlib, random, urllib.request
from concurrent.futures import (ThreadPoolExecutor, TimeoutError as FuturesTimeout,
                                wait, FIRST_COMPLETED)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
PROBLEMS_DIR = os.path.join(SCRIPT_DIR, "problems")
QUEUE_PATH = os.path.join(SCRIPT_DIR, "queue.json")
# 免费算力池统一接入层（用户提供）：本地 4B + 免 key 云腿 + 已配 key 公益站
FREE_LLM_DIR = "E:/xmanbian/tools_and_notes"
if FREE_LLM_DIR not in sys.path:
    sys.path.insert(0, FREE_LLM_DIR)

os.makedirs(PROBLEMS_DIR, exist_ok=True)

SYS_PROMPT = ("你是一个严谨、独立的研究讨论参与者，用中文回答，观点明确、有依据、"
              "不空话。你在一个共享黑板上与其他智能体协作：你们读写同一个文件夹，"
              "各自读同伴的发言再回应。")

# 连续多少轮黑板无新增内容即判定真收敛收口
MAX_IDLE_ROUNDS = int(os.environ.get("NETFORCE_MAX_IDLE", "6"))

# 前序讨论基础文件（放在问题文件夹里，作为每轮 agent 的起点，不重复已有结论）
BASIS_NAME = "basis.md"

# ----------------------------------------------------------- 工具层（让 agent 能动手）
# 默认开：所有 agent 每轮都能 EXEC 真跑 / TASK 真向另一个 API 派活 / 读写共享工作区。
# 关掉：NETFORCE_TOOLS=0 或命令行 --no-tools（退化为纯文本讨论）。
TOOLS_ON = os.environ.get("NETFORCE_TOOLS", "1") == "1"
# 每轮每个 agent 的工具步数：默认 **0 = 不设预算**（用户定调：想改多久就改多久，
# 最大自主性）。想实验性收紧时用 NETFORCE_TOOL_STEPS=N。
TOOL_STEPS = int(os.environ.get("NETFORCE_TOOL_STEPS", "0"))
# 🔴 唯一硬闸（用户定的）：单个 agent 卡死/循环 30 分钟才掐，而且只在它自己走不出来时。
# 这不是"预算"，是防止一个线程永远占着 worker 拖垮全场的兜底。
AGENT_WALL_DEFAULT = int(os.environ.get("NETFORCE_AGENT_WALL", "1800"))
# 单次 EXEC 的子进程墙钟（秒）
EXEC_TIMEOUT = int(os.environ.get("NETFORCE_EXEC_TIMEOUT", "25"))

try:
    import tool_agent as TA
except Exception as _e:      # 工具层缺失则自动退化为纯文本，不影响主链路
    TA = None
    print("[tools] tool_agent 不可用，退化为纯文本讨论: %s" % _e)


# ----------------------------------------------------------------- 队列读写
def load_queue(path: str = QUEUE_PATH) -> list:
    if not os.path.exists(path):
        return []
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return []


def save_queue(q: list, path: str = QUEUE_PATH) -> None:
    tmp = path + ".tmp"
    json.dump(q, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ----------------------------------------------------------- 隐私闸门
SENSITIVE_PAT = [
    r"\.gguf", r"/models/", r"E:\\models", r"sk-[A-Za-z0-9]{10,}",
    r"AKIA[0-9A-Z]{16}", r"api[_-]?key\s*[:=]\s*\S+", r"宪法原文",
    r"token\s*[:=]\s*[\"']?[A-Za-z0-9\-_]{16,}",
]


def is_sensitive(text: str) -> bool:
    for pat in SENSITIVE_PAT:
        if re.search(pat, text, re.I):
            return True
    return False


def _redact(text: str) -> str:
    out = text
    for pat in SENSITIVE_PAT:
        out = re.sub(pat, "[REDACTED]", out)
    return out


def _is_error(text: str) -> bool:
    """调用失败的串（如 '[xxx 调用失败: ...]' / '[agent xxx 异常: ...]'）不是内容。"""
    t = (text or "").strip()
    return bool(t) and t.startswith("[") and ("失败" in t or "异常" in t)


# 限流/配额类失败：外部配额问题（并发一高就爆发），不算这个 agent 坏了
RATE_PAT = re.compile(r"429|rate.?limit|too many requests|quota|限流|403|overload|"
                      r"busy|temporarily|try again|并发", re.I)


# ----------------------------------------------------------- 免费 agent 池
def _mk_local(fl):
    def f(messages):
        prompt = messages[-1]["content"]
        try:
            r = fl.call_local(prompt, n=600, timeout=600)
            return r.get("content", "")
        except Exception as e:
            return "[local 调用失败: %s]" % e
    return {"name": "local", "tier": "local", "external": False, "call": f}


def _mk_keyed(fl, name):
    def f(messages):
        try:
            r = fl.call(name, messages, timeout=180)
            return r.get("content", "")
        except Exception as e:
            return "[%s 调用失败: %s]" % (name, e)
    return {"name": "keyed_%s" % name, "tier": "keyed", "external": True, "call": f}


def _mk_anon(FP, i):
    def f(messages):
        prompt = messages[-1]["content"]
        try:
            r = FP.ask_any(prompt, tier="anon", max_tokens=640)
            if isinstance(r, dict):
                return r.get("content", "")
            return str(r)
        except Exception as e:
            return "[anon 调用失败: %s]" % e
    return {"name": "anon%d" % i, "tier": "anon", "external": True, "call": f}


def _mk_mock():
    def f(messages):
        prompt = messages[-1]["content"]
        return ("(mock) 已收到讨论上下文，占位回应：问题的核心是「%s…」。"
                "本环境无真实免费 agent，未消耗额度——仅验证黑板+收敛协议链路。"
                % prompt[:40])
    return {"name": "mock", "tier": "mock", "external": False, "call": f}


def collect_agents() -> list:
    """动态枚举免费模型池，不写死数量。顺序：本地(离线) → 免key云腿 → 已配key公益站。"""
    agents = []
    fl = None
    try:
        import free_llm as fl
    except Exception as e:
        print("[collect] free_llm 不可用: %s" % e)

    # 1) 本地模型（离线·无密钥·抗数据中断）
    if fl is not None and fl.local_available():
        agents.append(_mk_local(fl))

    # 2) 免 key 云腿（pollinations 等）—— 单一真源 free_pool.POOL 的 anon 层
    try:
        import free_pool as FP
        anon = [c for c in getattr(FP, "POOL", []) if c.get("tier") == "anon"]
        for i in range(min(len(anon), 12) if anon else 0):
            agents.append(_mk_anon(FP, i))
    except Exception:
        pass

    # 3) 已配 key 的公益站（动态，有几把 key 就几个）
    if fl is not None:
        for name, p in fl.PROVIDERS.items():
            if fl._key(p):
                agents.append(_mk_keyed(fl, name))

    # 4) zen-gate 本地网关：把 OpenCode Zen 免费车道（及可选已配 key 上游）包成 agent
    #    只拿网关自己的 mainKey 打本地 8787，上游密钥由 zen-gate 托管 → 代码零硬编码
    try:
        zen = collect_zen_agents(
            include_keyed=os.environ.get("NETFORCE_ZEN_KEYED") == "1")
        agents.extend(zen)
    except Exception as e:
        print("[zen] 并入失败（跳过）: %s" % e)

    # 兜底：若一个真实 agent 都没有，放一个明确标注的 mock，仅用于验证协议链路
    if not agents:
        agents.append(_mk_mock())
        print("[collect] ⚠️ 当前无任何真实免费 agent 可用（本机未配 key / 沙箱无网络），"
              "已启用 mock 占位 agent 以验证黑板+收敛协议；在你的机器上会真调免费 API。")
    return agents


# ----------------------------------------------------------- zen-gate 本地网关（OpenCode Zen 免费车道）
def _find_zen_config():
    cands = [
        os.environ.get("ZEN_GATE_CONFIG"),
        os.path.join(os.environ.get("APPDATA", ""), "zen-gate", "config.json"),
        os.path.expanduser("~/.zen-gate/config.json"),
    ]
    for c in cands:
        if c and os.path.exists(c):
            try:
                return json.load(open(c, encoding="utf-8"))
            except Exception:
                return None
    return None


def _mk_zen(zport, zkey, mid):
    def f(messages):
        body = json.dumps({
            "model": mid,
            "messages": messages,
            "max_tokens": 800,
            "temperature": 0.7,
        }).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/v1/chat/completions" % zport,
            data=body,
            headers={"Authorization": "Bearer " + zkey,
                     "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.load(r)
            return d["choices"][0]["message"]["content"]
        except Exception as e:
            return "[zen %s 调用失败: %s]" % (mid, e)
    return {"name": "zen_" + re.sub(r"[^A-Za-z0-9]+", "_", mid)[:48],
            "tier": "zen", "external": True, "call": f}


def collect_zen_agents(include_keyed=False, max_agents=400):
    """动态并入 zen-gate 暴露的模型（默认只吃免费车道，零密钥硬编码）。

    zen-gate 把 OpenCode Zen 免费车道 + 已配 key 的上游都封装成本地 OpenAI 兼容网关；
    我们只拿它网关自己的 mainKey 打 127.0.0.1:port，上游密钥由 zen-gate 托管，
    代码里不碰任何上游 key。调用失败/超时就返回错误串，由外层"慢的丢弃"闸统一处理。
    """
    cfg = _find_zen_config()
    if not cfg:
        return []
    zport = cfg.get("port", 8787)
    zkey = cfg.get("mainKey", "")
    if not zkey:
        return []
    try:
        data = json.load(urllib.request.urlopen(
            urllib.request.Request("http://127.0.0.1:%d/v1/models" % zport,
                                  headers={"Authorization": "Bearer " + zkey}),
            timeout=10))
        msgs = data.get("data", [])
    except Exception as e:
        print("[zen] 拉模型清单失败: %s" % e)
        return []
    NONTX = re.compile(r"embed|rerank|image|asr|tts|audio|voice|ocr|diffusion|"
                       r"captioner|kolors|wan2|bge|moss|cosy|sensevoice|"
                       r"paddleocr|seed-oss|omni", re.I)
    seen, out = set(), []
    for m in msgs:
        mid = m.get("id", "")
        if not mid or NONTX.search(mid):
            continue
        base = re.sub(r"\s*\((light|deep)\)\s*$", "", mid)   # 折叠 light/deep 变体
        isfree = bool(re.search(r"(-free|/free|:free)", mid)) or \
                 mid in ("kilo-auto/free", "openrouter/free",
                         "ll-alpha-free", "space-bunny-free")
        if not isfree and not include_keyed:
            continue
        if base in seen:
            continue
        seen.add(base)
        out.append(_mk_zen(zport, zkey, base))
        if len(out) >= max_agents:
            break
    print("[zen] 从 zen-gate 并入 %d 个 agent（免费车道为主，零密钥硬编码）" % len(out))
    return out


# ----------------------------------------------------------- 黑板读写
def read_blackboard(folder: str) -> list:
    out = []
    for fn in sorted(glob.glob(os.path.join(folder, "*_r*.md"))):
        try:
            c = open(fn, encoding="utf-8").read()
        except Exception:
            continue
        out.append((os.path.basename(fn), c))
    return out


def _fp_of_all(folder: str) -> set:
    """收敛指纹：**只看正文**，剥离末尾的「本轮工具实证」段。

    否则每轮的 trace 必然不同 → 指纹永远在变 → 永远判定"有新内容" → 永不收敛。
    """
    s = set()
    for fn in sorted(glob.glob(os.path.join(folder, "*_r*.md"))):
        try:
            c = open(fn, encoding="utf-8").read()
        except Exception:
            continue
        c = c.split("\n\n### 本轮工具实证")[0]
        norm = re.sub(r"\s+", "", c)[:400]
        s.add(hashlib.md5(norm.encode("utf-8")).hexdigest())
    return s


def _work_listing(folder: str, limit: int = 40) -> str:
    """本题共享工作区的文件清单（让 agent 看得见同伴写了什么、能去读去跑）。"""
    wd = os.path.join(folder, "work")
    if not os.path.exists(wd):
        return ""
    rows = []
    for root, _, fs in os.walk(wd):
        for f in fs:
            if f.startswith("_exec_") or f.startswith("_trace_"):
                continue
            fp = os.path.join(root, f)
            try:
                rows.append((os.path.getmtime(fp),
                             "%-46s %7d 字节" % (os.path.relpath(fp, wd),
                                                 os.path.getsize(fp))))
            except Exception:
                pass
    if not rows:
        return "\n【共享工作区】（空）"
    rows.sort(reverse=True)
    body = "\n".join(r[1] for r in rows[:limit])
    more = "\n…（共 %d 个文件）" % len(rows) if len(rows) > limit else ""
    return ("\n【共享工作区 problems/work 现有文件（这是你们共用的真文件系统，"
            "可以 [READ] 别人的代码、[EXEC] 跑它、改完再 [WRITE] 回去）】\n" + body + more)


def build_context(prompt: str, bb: list, folder: str = None) -> str:
    lines = ["【问题】\n" + prompt]
    # 前序讨论基础：已有的真内容，作为本轮起点（用户要求"以那些为基础继续烧"）
    if folder:
        bp = os.path.join(folder, BASIS_NAME)
        if os.path.exists(bp):
            try:
                basis = open(bp, encoding="utf-8").read().strip()
            except Exception:
                basis = ""
            if basis:
                lines.append("\n【前序讨论基础（已有的一小时真内容；请在此基础上继续深化，"
                             "不要重复已有结论，要找漏洞 / 推进 / 给可落地的协议头与伪代码）】\n"
                             + basis)
    if folder:
        lines.append(_work_listing(folder))
    lines.append("\n【已有黑板发言（按轮次，全量不截断）】")
    if bb:
        for nm, c in bb:
            lines.append("\n--- %s ---\n%s" % (nm, c))
    else:
        lines.append("（暂无新发言，请先给出你的独立见解）")
    lines.append("\n【你的任务】你是参与共享黑板讨论的智能体之一。"
                 "请基于问题、前序基础与他人的发言，产出你的独立贡献：新角度、反驳、"
                 "补充证据或综合。只输出你要写下的内容，不要复述问题。")
    return "\n".join(lines)


# ----------------------------------------------------------- 单题执行
# ----------------------------------------------------------- 一次发言（含工具循环）
TAG_STRIP = re.compile(r"\[/?(?:EXEC|TASK|DONE|WRITE|READ|LIST)(?::[^\]]*)?\]")


def _trim_msgs(msgs, limit=80000):
    """工具轮次无限后，上下文会膨胀到模型吃不下。这里只**压缩**不丢弃结论：
    早期的长工具返回压成摘要，实在太长才丢最早的中间轮（system 与任务始终保留）。"""
    if sum(len(m.get("content", "")) for m in msgs) <= limit:
        return msgs
    out = [msgs[0]]
    for m in msgs[1:]:
        c = m.get("content", "")
        if m.get("role") == "user" and len(c) > 3000:
            c = c[:300] + "\n…（早期工具返回已压缩，细节见工作区文件）"
        out.append({"role": m["role"], "content": c})
    while sum(len(m["content"]) for m in out) > limit and len(out) > 4:
        out.pop(1)
    return out


REVIEW_REQ = os.environ.get("NETFORCE_REVIEW", "1") == "1"   # 同伴验收（默认开）
# 验收者池里坏模型很多（401/402/429），随机挑常挑到坏的 → 记下"验成过的"优先再挑
_GOOD_REVIEWERS = set()


def _review_by_peer(ag, conclusion, trace, workdir, agents, log=None, tries=3):
    """🔴 用户铁律：不能自己说自己好了。

    任何 [DONE] 都必须交给**另一个 agent（另一个 API）**独立复核：
    它说 PASS 才写入黑板；说 FAIL 就把意见原样打回，让它继续改。
    编排器只负责传递，**不做任何主观判断**——合格与否由同伴说了算。

    返回 (ok, verdict, verified)：
      verified=False 表示**没人真的验过**（连换 tries 个验收者都没响应），
      这种情况只能标"免检"，绝不能冒充"已复核通过"。
    """
    pool = [a for a in agents if a["name"] != ag["name"]]
    if not pool:
        return True, "（无其他 agent 可验收，本次免检）", False
    rv = random.choice(pool)
    ev = "\n".join("[%s] %s → %s" % (n, b[:120], r[:240]) for n, b, r in trace[-8:]) or "（无）"
    try:
        files = TA.tool_list(work=workdir)[:1500]
    except Exception:
        files = "（读不到）"
    prompt = (
        "你是**验收者**。另一位智能体提交了他的结论，请你独立判断它是否真的成立。\n\n"
        "【他的结论】\n%s\n\n"
        "【他声称的工具实证（真实执行记录）】\n%s\n\n"
        "【共享工作区里他写的文件】\n%s\n\n"
        "判定规则（严格，不许客气）：\n"
        "1. 他声称'跑通了/验证过了'——必须有真实输出（exit=0 且 stdout 有内容）。"
        "只有代码、没有运行结果 = 不成立。\n"
        "2. 结论里的每个事实都必须能从实证或文件内容对上；对不上 = 不成立。\n"
        "3. 存在明确 bug、逻辑漏洞、或未验证的关键假设 = 不成立。\n"
        "4. 只写伪代码/设想而没有实际验证 = 不成立。\n\n"
        "只输出两行，不要多余内容：\n"
        "第一行：PASS 或 FAIL\n"
        "第二行：具体理由（指出哪一条不成立、问题在哪、要他改什么）"
        % (conclusion[:3000], ev, files))
    # 连换几个验收者，确保真的**有人**判过——优先挑历史上验成过的，坏模型太多
    good = [a for a in pool if a["name"] in _GOOD_REVIEWERS]
    v, rv, tried = "", None, []
    for _ in range(max(1, tries)):
        cands = [a for a in (good or pool) if a["name"] not in tried] or pool
        cand = random.choice(cands)
        tried.append(cand["name"])
        try:
            v = (cand["call"]([{"role": "user", "content": prompt}]) or "").strip()
        except Exception as e:
            v = "[%s 调用失败: %s]" % (cand["name"], e)
        rv = cand
        if v and not _is_error(v):
            _GOOD_REVIEWERS.add(cand["name"])
            break
    if not v or _is_error(v):
        note = "（连换 %d 个验收者均未响应：%s，本次免检）" % (len(tried), v[:160])
        if log is not None:
            print("[review] %s → 无人可验（免检）" % ag["name"], file=log, flush=True)
        return True, note, False
    head = v.splitlines()[0][:60] if v else ""
    # 严格判据：只有明确 PASS 才算过；含 FAIL/不通过 或说不清，一律打回
    ok = bool(re.search(r"PASS|通过", head, re.I)) and \
         not bool(re.search(r"FAIL|不通过|不成立|未通过", head, re.I))
    verdict = v[:1200] + "\n（验收者：%s）" % rv["name"]
    if log is not None:
        print("[review] %s → %s 判定 %s" % (ag["name"], rv["name"],
              "PASS" if ok else "FAIL"), file=log, flush=True)
    return ok, verdict, True


def _attach_review(ag, text, trace, workdir, agents, log=None):
    """堵漏：很多 agent 不提交 [DONE]、直接甩一段文本就结束 —— 那样就绕过了验收。

    凡是**用过工具（有实证）**的发言，一律送同伴复核并把结论附在黑板上；
    纯观点（没动过手）不强制送验，观点本来就不是交付物。
    """
    if not REVIEW_REQ or not trace:
        return text, trace
    ok, verdict, verified = _review_by_peer(ag, text, trace, workdir, agents, log)
    trace.append(("REVIEW", "本轮发言送验（未提交 DONE）", verdict[:600]))
    if not verified:
        mark = "⚠️ **本次免检**——没有验收者响应"
    elif ok:
        mark = "**✅ 同伴复核通过**"
    else:
        mark = "⚠️ **同伴复核未认可**"
    return text + "\n\n" + mark + "\n" + verdict, trace


def agent_turn(ag, ctx, workdir, agents, steps=TOOL_STEPS, log=None,
               wall=AGENT_WALL_DEFAULT):
    """一次发言 = 工具循环（**不给步数预算**）+ 最终文本结论。

    自主性全给 agent：它想跑多少次、改多少遍都行，直到它自己 [DONE]。
    唯一的兜底是单 agent 墙钟（默认 30 分钟，用户定的"卡死/循环才掐"），
    而且到点也只是**把已做完的工作作为本轮发言**交出去，不是判它错。
    """
    plain = [{"role": "system", "content": SYS_PROMPT}, {"role": "user", "content": ctx}]
    if not TOOLS_ON or TA is None:
        return ag["call"](plain), []

    task_fn = TA.make_task_tool(agents, ag["name"])
    head = (TA.SYS + "\n\n你的代号：" + ag["name"]
            + "（写文件请用这个代号做前缀，避免覆盖同伴的文件）\n\n")
    msgs = [{"role": "system", "content": SYS_PROMPT + "\n" + TA.SYS},
            {"role": "user", "content": head + ctx}]
    trace, out, n = [], "", 0
    passed, last_verdict = False, None
    t0 = time.time()
    while True:
        n += 1
        if steps and n > steps:          # steps=0（默认）即不设预算
            break
        if time.time() - t0 > wall:      # 唯一硬闸：卡死/循环才停
            trace.append(("WALL", "单 agent 墙钟 %ds 到点" % wall,
                          "已把做完的工作作为本轮发言交出（不是判错）"))
            break
        try:
            out = ag["call"](msgs) or ""
        except Exception as e:
            return "[agent %s 异常: %s]" % (ag["name"], e), trace
        m = TA.TOOL_RE.search(out)
        if not m:
            # 容错：没按格式调工具但贴了代码块 → 直接真跑（让它"能尝试"）
            cb = re.search(r"```(?:python|py|bash|sh)?\s*\n(.*?)```", out, re.S)
            if cb and cb.group(1).strip():
                res = TA.tool_exec_py(cb.group(1), timeout=EXEC_TIMEOUT, work=workdir)
                trace.append(("AUTOEXEC", cb.group(1)[:200], res[:600]))
                msgs.append({"role": "assistant", "content": out})
                msgs.append({"role": "user",
                             "content": "我已自动执行你的代码块，真实返回：\n%s\n\n"
                                        "据此继续（用 [EXEC]/[TASK]/[DONE] 等工具格式）。" % res})
                msgs = _trim_msgs(msgs)
                continue
            # 没调工具、也没 DONE：纯文本发言。用过工具的要送验，别让它绕过。
            return _attach_review(ag, TAG_STRIP.sub("", out).strip(), trace,
                                  workdir, agents, log)
        name, arg, body = m.group(1), (m.group(2) or ""), m.group(3)
        res = TA.run_tool(name, arg, body, task_fn, work=workdir, timeout=EXEC_TIMEOUT)
        trace.append((name.upper(), body.strip()[:200], (res or "DONE")[:600]))
        if name.upper() == "DONE":
            if not REVIEW_REQ:
                return body.strip(), trace
            # 🔴 自己说好不算：交给另一个 AI 独立验收
            ok, verdict, verified = _review_by_peer(ag, body.strip(), trace,
                                                    workdir, agents, log)
            trace.append(("REVIEW", "提交验收：%s" % body.strip()[:120], verdict[:600]))
            if verified and ok:
                passed = True
                return (body.strip() + "\n\n**✅ 验收：已由另一位智能体独立复核通过**\n"
                        + verdict), trace
            if not verified:      # 没人能验 ≠ 他错了：接受，但如实标注免检
                return (body.strip() + "\n\n⚠️ **本次免检**——没有验收者响应，"
                        + verdict), trace
            last_verdict = verdict
            # 打回：把同伴意见原样回传，让它继续改（自主性不变，只是没过关）
            msgs.append({"role": "assistant", "content": out})
            msgs.append({"role": "user",
                         "content": "你的 [DONE] **未被验收通过**（不能自己说自己好了）。"
                                    "验收者的意见：\n%s\n\n请据此用工具实际修改/验证，"
                                    "然后重新提交 [DONE]。不要只复述理由，要真的改到成立。"
                                    % verdict})
            msgs = _trim_msgs(msgs)
            continue
        msgs.append({"role": "assistant", "content": out})
        msgs.append({"role": "user",
                     "content": "工具 %s 的真实返回：\n%s\n\n据此继续"
                                "（继续用工具，或用 [DONE] 给出你本轮的结论）。"
                                % (name.upper(), res)})
        msgs = _trim_msgs(msgs)
    tail = TAG_STRIP.sub("", out).strip() or "（本轮未给出结论，实证见下方轨迹）"
    # 墙钟到点 / 步数用尽而验收还没过 → 如实标注，绝不能冒充"已验收"
    if REVIEW_REQ and not passed and last_verdict:
        tail += ("\n\n⚠️ **本轮结束时仍未通过同伴验收**（不能自己说自己好了）"
                 "\n最后一次验收意见：\n" + last_verdict)
    return _attach_review(ag, tail, trace, workdir, agents, log) \
        if not passed else (tail, trace)


def _write_trace(workdir, agent_name, round_no, trace):
    if not trace:
        return ""
    td = os.path.join(workdir, "_traces")
    os.makedirs(td, exist_ok=True)
    full = "\n\n".join("[%s] %s\n→ %s" % (n, b, r) for n, b, r in trace)
    open(os.path.join(td, "%s_r%d.txt" % (agent_name, round_no)),
         "w", encoding="utf-8").write(full)
    brief = "\n".join("- %s %s → %s" % (n, b.replace("\n", " ")[:60],
                                        r.splitlines()[0][:70] if r else "")
                      for n, b, r in trace[:6])
    return "\n\n### 本轮工具实证（真实跑过，非声称）\n" + brief


def _external_status(pid: str, queue_path: str = QUEUE_PATH) -> str:
    for x in load_queue(queue_path):
        if x["id"] == pid:
            return x.get("status", "pending")
    return "missing"


def synthesize(folder: str, agents: list, prompt: str) -> str:
    bb = read_blackboard(folder)
    if not bb:
        return "# 综述\n\n（无发言，未产出）\n"
    ctx = "【问题】\n%s\n\n【各智能体发言】\n" % prompt
    for nm, c in bb:
        ctx += "\n--- %s ---\n%s" % (nm, c)
    ctx += "\n\n请综合上述所有发言，产出一份结构化的综述（要点 + 分歧 + 共识 + 建议）。"
    real = [a for a in agents if a["tier"] != "mock"]
    if real:
        try:
            text = real[0]["call"]([
                {"role": "system", "content": "你是讨论主持人，负责写综述。"},
                {"role": "user", "content": ctx},
            ])
            if text and text.strip():
                return "# 综述（由 %s 生成）\n\n%s" % (real[0]["name"], text)
        except Exception:
            pass
    body = "\n\n".join("%s:\n%s" % (nm, c) for nm, c in bb)
    return "# 综述（自动拼接，未调用模型）\n\n" + body


def run_problem(pid: str, agents: list, queue_path: str = QUEUE_PATH,
                max_idle_rounds: int = MAX_IDLE_ROUNDS) -> str:
    q = load_queue(queue_path)
    prob = next((x for x in q if x["id"] == pid), None)
    if not prob:
        print("[run] 队列里没有 id=%s" % pid)
        return "missing"
    folder = prob["folder"]
    os.makedirs(folder, exist_ok=True)
    # 本题共享工作区：所有 agent 的代码/数据都落在这里，彼此可读可改可跑
    workdir = os.path.join(folder, "work")
    os.makedirs(workdir, exist_ok=True)
    with open(os.path.join(folder, "problem.md"), "w", encoding="utf-8") as f:
        f.write(prob.get("prompt", ""))
    log = open(os.path.join(folder, "log.txt"), "a", encoding="utf-8")
    print("[run] 开始问题 %s（%d 个 agent，工具层=%s，每轮每 agent 最多 %d 步）"
          % (pid, len(agents), "开" if (TOOLS_ON and TA) else "关", TOOL_STEPS),
          file=log, flush=True)

    round_no = 0
    no_new_streak = 0
    # —— 唯一的「掐一下」硬闸：单个智能体卡死/循环才掐，绝不限制讨论本身 ——
    # ① 单 agent 调用墙钟上限（默认 30 分钟）：超时即掐，丢给其他 agent 自己干
    AGENT_WALL = int(os.environ.get("NETFORCE_AGENT_WALL", "1800"))
    # ② 循环检测（同伴/自我监督的客观判据：连续重复 = 卡循环 = 掐）
    REPEAT_CUT = int(os.environ.get("NETFORCE_REPEAT_CUT", "3"))
    dropped = set()
    last_fp, repeat, err_streak = {}, {}, {}
    # 并发度：默认 16 路——一轮里所有 agent **同时读同一份黑板快照、同时思考、同时动手**，
    # 谁来谁的结果就先落盘，慢的照样按 30 分钟闸丢弃。可用 NETFORCE_WORKERS 调；设 1 即纯串行。
    WORKERS = int(os.environ.get("NETFORCE_WORKERS", "12"))
    ex = ThreadPoolExecutor(max_workers=WORKERS)

    while True:
        # 外部暂停 / 取消闸门：每轮重读队列，paused/cancelled 即干净退出（不杀进程）
        st = _external_status(pid, queue_path)
        if st in ("paused", "cancelled"):
            print("[run] 检测到外部状态=%s，干净退出（不杀进程）" % st, file=log, flush=True)
            ex.shutdown(wait=False, cancel_futures=True)
            log.close()
            return st

        round_no += 1
        print("[run] 第 %d 轮 / 当前空闲连击 %d / 已掐 %d 个"
              % (round_no, no_new_streak, len(dropped)), file=log, flush=True)
        bb = read_blackboard(folder)
        # 同一轮：所有人读的是**同一份**黑板快照（同时读），然后各自同时思考、同时动手
        ctx = build_context(prob["prompt"], bb, folder)
        before = _fp_of_all(folder)
        # —— 全部同时发车：谁先想完谁先落盘，慢的按 30 分钟闸单独掐，不拖累别人 ——
        futs, started, launched = {}, {}, 0
        for ag in agents:
            if ag["name"] in dropped:
                continue
            # 外部 agent 遇敏感内容则脱敏，本地/mock 用全文
            c = _redact(ctx) if (ag["external"] and is_sensitive(ctx)) else ctx
            f = ex.submit(agent_turn, ag, c, workdir, agents, TOOL_STEPS, log, AGENT_WALL)
            futs[f] = ag
            started[f] = time.time()
            launched += 1
        print("[run] 第 %d 轮同时发车 %d 个（并发 %d 路）"
              % (round_no, launched, WORKERS), file=log, flush=True)
        pending = set(futs)
        while pending:
            done, pending = wait(pending, timeout=10, return_when=FIRST_COMPLETED)
            for f in done:
                ag = futs[f]
                try:
                    text, trace = f.result(timeout=0)
                except Exception as e:
                    text, trace = "[agent %s 异常: %s]" % (ag["name"], e), []
                text = (text or "").strip()
                if not text:
                    continue
                # 调用失败的串不是内容：不写黑板、不重置收敛计数；连续 3 次失败则掐。
                # 🔴 限流/配额类失败（429/403/quota）是外部配额问题，不是这个 agent 坏了
                #    ——不计入失败连击、不掐，下轮它还能回来。否则并发一高就把 154 掐成 36。
                if _is_error(text):
                    if RATE_PAT.search(text):
                        continue
                    err_streak[ag["name"]] = err_streak.get(ag["name"], 0) + 1
                    if err_streak[ag["name"]] >= 3:
                        print("[run] agent %s 连续 3 次调用失败，已掐（丢给其他 agent）"
                              % ag["name"], file=log, flush=True)
                        dropped.add(ag["name"])
                    continue
                # —— 同伴/自我监督：连续重复输出 = 卡循环，掐（编排器不做任何主观判断）——
                fp = hashlib.md5(re.sub(r"\s+", "", text)[:400].encode()).hexdigest()
                repeat[ag["name"]] = (repeat[ag["name"]] + 1) if last_fp.get(ag["name"]) == fp else 0
                last_fp[ag["name"]] = fp
                if repeat[ag["name"]] >= REPEAT_CUT:
                    print("[run] agent %s 连续 %d 轮输出重复（被同伴判定为循环），已掐"
                          % (ag["name"], REPEAT_CUT), file=log, flush=True)
                    dropped.add(ag["name"]); continue
                body = text + _write_trace(workdir, ag["name"], round_no, trace)
                open(os.path.join(folder, "%s_r%d.md" % (ag["name"], round_no)),
                     "w", encoding="utf-8").write(body)
            # —— 掐一下：仍在跑但已超 AGENT_WALL 的**单个** agent（其他人不受影响）——
            for f in list(pending):
                if time.time() - started[f] > AGENT_WALL:
                    f.cancel()
                    pending.discard(f)
                    ag = futs[f]
                    print("[run] agent %s 超 %ds（卡死/循环），已掐，其他人继续"
                          % (ag["name"], AGENT_WALL), file=log, flush=True)
                    dropped.add(ag["name"])
        after = _fp_of_all(folder)
        if (after - before):
            no_new_streak = 0
        else:
            no_new_streak += 1
        if no_new_streak >= max_idle_rounds:
            print("[run] 连续 %d 轮无新内容 → 真收敛，收口" % max_idle_rounds,
                  file=log, flush=True)
            break

    ex.shutdown(wait=False, cancel_futures=True)
    final = synthesize(folder, agents, prob["prompt"])
    open(os.path.join(folder, "FINAL.md"), "w", encoding="utf-8").write(final)
    print("[run] 已收口 FINAL.md", file=log, flush=True)
    log.close()
    return "done"


# ----------------------------------------------------------- 常驻心跳
def serve(interval: int = 60, queue_path: str = QUEUE_PATH) -> None:
    print("== heartbeat_relay 常驻心跳（interval=%ds）==" % interval)
    while True:
        try:
            q = load_queue(queue_path)
            agents = collect_agents()
            for item in q:
                if item.get("status") != "pending":
                    continue
                pid = item["id"]
                item["status"] = "running"
                save_queue(q, queue_path)
                st = run_problem(pid, agents, queue_path)
                q = load_queue(queue_path)
                for it in q:
                    if it["id"] == pid:
                        it["status"] = {"paused": "paused", "cancelled": "cancelled"}.get(st, "done")
                save_queue(q, queue_path)
                print("[serve] %s → %s" % (pid, st))
        except Exception as e:
            print("[serve] 异常: %s" % e)
        time.sleep(interval)


# ----------------------------------------------------------- 入队
def enqueue(prompt: str, kind: str = "discuss", queue_path: str = QUEUE_PATH) -> str:
    q = load_queue(queue_path)
    pid = "p%03d" % (len(q) + 1)
    q.append({"id": pid, "prompt": prompt, "kind": kind,
              "status": "pending", "folder": os.path.join(PROBLEMS_DIR, pid)})
    save_queue(q, queue_path)
    print("已入队 %s：%s" % (pid, prompt[:50]))
    return pid


def enqueue_batch(path: str, queue_path: str = QUEUE_PATH) -> None:
    text = open(path, encoding="utf-8").read()
    for b in re.split(r"\n-{3,}\n", text):
        b = b.strip()
        if b:
            enqueue(b[:2000], queue_path=queue_path)


# ----------------------------------------------------------- CLI
def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 0
    cmd = args[0]
    if cmd == "serve":
        iv = 60
        if "--interval" in args:
            iv = int(args[args.index("--interval") + 1])
        serve(iv)
        return 0
    if cmd == "run":
        pid = args[1]
        if "--no-tools" in args:
            globals()["TOOLS_ON"] = False
        agents = collect_agents()
        if "--limit" in args:
            agents = agents[:int(args[args.index("--limit") + 1])]
        print("可用 agent：%d 个（%s）" % (len(agents),
              "、".join(a["name"] for a in agents[:6]) + ("…" if len(agents) > 6 else "")))
        st = run_problem(pid, agents)
        print("结果状态：%s" % st)
        return 0
    if cmd == "enqueue":
        if "--batch" in args:
            enqueue_batch(args[args.index("--batch") + 1])
        else:
            enqueue(" ".join(args[1:]))
        return 0
    if cmd == "agents":
        ags = collect_agents()
        print("当前免费 agent 池（%d 个）：" % len(ags))
        for a in ags:
            print("  - %s  [tier=%s, external=%s]" % (a["name"], a["tier"], a["external"]))
        return 0
    if cmd == "status":
        for x in load_queue():
            print("%s  %-9s %s" % (x["id"], x["status"], x["prompt"][:40]))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
