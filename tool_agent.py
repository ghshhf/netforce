# -*- coding: utf-8 -*-
"""
让智能体从「只能说话」变成「能动手」——工具层 + 工具循环。

背景（用户点破的局限）：黑板上那些 API 只能产出文本/代码文本，
彼此靠上下文交流，**既不能执行代码验证，也不能向另一个 API 发布任务**，
所以它们给出的协议头、伪代码从来没被真正跑过。

本模块补上这件事，五个工具：
    EXEC_PY   真跑一段 Python（子进程，限时，工作区内）→ 拿回 stdout/stderr
    WRITE     在工作区写文件
    READ      读工作区文件
    LIST      列工作区文件
    TASK      向「另一个 agent（另一个 API）」发布任务，真调用并拿回它的回答
    DONE      宣布完成

工具调用走**文本协议**而非 function calling——因为免费模型池参差不齐，
很多不支持 tools 参数；但任何能吐文本的模型都能遵守 [TOOL:...] 格式。
"""

import os
import re
import sys
import json
import time
import shutil
import hashlib
import subprocess
import headless

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "work")
os.makedirs(WORK, exist_ok=True)

sys.path.insert(0, HERE)
sys.path.insert(0, r"E:/xmanbian/tools_and_notes")

TOOL_RE = re.compile(r"\[(EXEC|TASK|DONE|WRITE|READ|LIST)(?::([^\]]*))?\](.*?)\[/\1\]", re.S)


# ------------------------------------------------------------------ 工具实现
def _safe_path(rel, work=None):
    """路径锁在工作区内（尊重"给一个文件夹"的设定，不是安全研究）。

    work 可传入每题自己的共享工作区（problems/<id>/work），
    这样所有智能体在同一题内写的文件彼此可见 → 文件级共享黑板。
    """
    base = os.path.normpath(work or WORK)
    p = os.path.normpath(os.path.join(base, rel))
    if not p.startswith(base):
        raise ValueError("越出工作区: %s" % rel)
    return p


# 危险命令黑名单：智能体产出的是不可信代码， destructive / 系统级操作一律拒执行。
# 🔴 用户铁律（2026-10-17）：**智能体一律不给删除能力**（删除不可逆，规则删会误杀真东西）。
#    所以 EXEC 里所有删除类动作整类封死——不只是危险的 rm -rf，任何删除都不跑。
DANGER = re.compile(
    r"(rm\s+-rf|rm\s+-r\b|rmdir\s+/s|del\s+/[fqs]|format\s+[a-z]:|shutdown|"
    r"taskkill|vssadmin|bcdedit|reg\s+delete|reg\s+add|net\s+user|"
    r"curl[^|]*\|\s*(bash|sh)|wget[^|]*\|\s*(bash|sh)|iex\b|Invoke-Expression|"
    r"shutil\.rmtree|os\.system\s*\(\s*['\"](rm|del|format|rd)|"
    r"subprocess\.(call|run|Popen)\s*\(\s*['\"](rm|del|format|rd)|"
    # ↓ 删除类整类封死（智能体没有删除权限）
    r"os\.(remove|unlink|rmdir|removedirs)\s*\(|\.unlink\s*\(|Remove-Item|"
    r"\brm\s+-|\brm\s+[^\s]+|\bdel\s+[^\s]+|\brd\s+/s|rmdir\s+/q)", re.I)


# 常见命令前缀（这些虽然不是 Python，但确实该当命令跑）
CMD_PREFIX = re.compile(r"^\s*(python|py|pip|node|npm|git|dir|ls|cat|type|echo|curl|"
                        r"nc|ping|netstat|where)\b", re.I)


def _looks_like_code(s: str) -> bool:
    """粗略判断这段文本是不是能跑的东西（挡掉 ASCII 表格/纯说明文字被当命令执行）。"""
    if CMD_PREFIX.search(s):
        return True
    if re.search(r"^\s*\+[-=+]+\+", s, re.M):        # ASCII 表格线
        return False
    return bool(re.search(r"(import |def |print\(|class |for |while |if .*:|=)", s))


def tool_exec_py(code, timeout=30, work=None):
    """执行一段 Python 代码；若不是合法 Python（如 `python x.py`、`pip ...`），当命令跑。"""
    import ast
    code = code.strip()
    if DANGER.search(code):
        return ("拒绝执行：命中危险命令黑名单（破坏性/系统级操作一律不跑）。"
                "请改用纯计算 / 文件写入 / 本地 socket 的方式验证你的设计。")
    try:
        ast.parse(code)
        is_py = True
    except SyntaxError:
        is_py = False
    base = os.path.abspath(work or WORK)
    if not is_py and not _looks_like_code(code):
        return ("未执行：这段内容不像可执行的东西（像是 ASCII 表格或说明文字）。"
                "请只提交能直接跑的纯 Python 代码；表格请写成注释或字符串。")

    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        if is_py:
            f = os.path.join(base, "_exec_%d_%d.py" % (os.getpid(), int(time.time() * 1000)))
            open(f, "w", encoding="utf-8").write(code)
            cmd = [sys.executable, f]
        else:
            f = None
            cmd = code           # shell 命令
        r = headless.run(cmd, cwd=base, timeout=timeout,
                           capture_output=True, shell=not is_py, env=env)
        out = (r.stdout or b"").decode("utf-8", "ignore")
        err = (r.stderr or b"").decode("utf-8", "ignore")
        s = ("exit=%d\n--- stdout ---\n%s" % (r.returncode, out or "(空)"))
        if err.strip():
            s += "\n--- stderr ---\n" + err[-1500:]
        if (not is_py) and r.returncode != 0:
            s += ("\n提示：你的片段没能作为 Python 解析，被当成命令行执行了。"
                  "请只输出纯代码——不要把括号里的自然语言说明、'实际应该是…'之类写进代码块。")
        return s[:4000]
    except subprocess.TimeoutExpired:
        return "执行超时（%ss）——把它拆小再试" % timeout
    except Exception as e:
        return "执行异常: %r" % e
    finally:
        try:
            if is_py and f:
                os.remove(f)
        except Exception:
            pass


# 合法文件名形态：一行、无空格、常见扩展名
FNAME_RE = re.compile(r"^[\w\-./]+\.(?:py|md|txt|json|js|ts|sh|csv|ya?ml|ini|html)$")


def _split_fname(body):
    """[WRITE] 的第一行本应是文件名。模型经常不守规矩（直接贴代码），
    这里做容错：认得出文件名就用，认不出就整段当内容 + 自动命名，绝不丢内容。"""
    first, _, rest = body.partition("\n")
    first = first.strip().strip("`").strip()
    if FNAME_RE.match(first):
        return first, rest
    # 第一行里捞一个像文件名的 token
    m = re.search(r"[\w\-]+\.(?:py|md|txt|json|js|ts|sh|csv|ya?ml|ini|html)", first)
    if m:
        extra = (first[:m.start()] + first[m.end():]).strip()
        return m.group(0), ((extra + "\n") if extra else "") + rest
    # 完全不像：整段是内容，自动命名（内容一行不丢）
    tag = hashlib.md5(first[:200].encode("utf-8")).hexdigest()[:6]
    return "untitled_%s.py" % tag, body


def tool_write(rel, content, work=None):
    rel, extra = _split_fname((rel or "") + "\n" + (content or ""))
    if extra:
        content = extra
    if len(content) > 200_000:
        return "文件过大（%d 字节 > 200KB），已拒写" % len(content)
    p = _safe_path(rel, work)
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    open(p, "w", encoding="utf-8").write(content)
    return "已写入 %s（%d 字节）%s" % (
        rel, len(content),
        "" if rel.startswith("untitled_") else "")


def tool_read(rel, work=None):
    p = _safe_path(rel, work)
    if not os.path.exists(p):
        return "文件不存在: %s" % rel
    t = open(p, encoding="utf-8", errors="ignore").read()
    return t[:6000] + ("\n…（截断）" if len(t) > 6000 else "")


def tool_list(work=None):
    out = []
    for root, _, fs in os.walk(os.path.abspath(work or WORK)):
        base = os.path.abspath(work or WORK)
        for f in fs:
            fp = os.path.join(root, f)
            out.append("%-46s %7d 字节" % (os.path.relpath(fp, base), os.path.getsize(fp)))
    return "\n".join(out) if out else "（工作区为空）"


def make_task_tool(agents, me):
    """生成一个「向另一个 API 派活」的工具：真调另一个模型，拿回它的回答。"""

    def tool_task(target, task_text, timeout=90):
        """向另一个 API 派活。模型经常臆造 agent 名、或派给自己（自己不在池里）→
        以前直接空手而归，互派永远发生不了。现在：找不到就**自动改派**给一个可用的，
        并如实告知改派给了谁，让协作真的发生。"""
        target = (target or "").strip()
        pool = [a for a in agents if a["name"] != me]
        if not pool:
            return "没有其他 agent 可派活。"
        note, ag = "", None
        if target:
            hit = [a for a in pool if target.lower() in a["name"].lower()]
            if hit:
                ag = hit[0]
            else:
                ag = pool[0]
                note = ("⚠️ 找不到 agent「%s」（也不能派给你自己 %s），已自动改派给 %s。"
                        "可派活的对象（部分）：%s\n\n"
                        % (target, me, ag["name"],
                           ", ".join(a["name"] for a in pool[:15])))
        else:
            ag = pool[0]
            note = "（未指定目标，自动派给 %s）\n\n" % ag["name"]
        try:
            r = ag["call"]([{"role": "user", "content": task_text}])
            return note + "【%s 的回答】\n%s" % (ag["name"], (r or "").strip()[:4000])
        except Exception as e:
            return note + "派给 %s 失败: %r" % (ag["name"], e)

    return tool_task


def run_tool(name, arg, body, task_fn, work=None, timeout=30):
    name = name.upper()
    if name == "EXEC":
        return tool_exec_py(body.strip(), timeout=timeout, work=work)
    if name == "WRITE":
        rel, _, content = body.partition("\n")
        return tool_write(rel.strip(), content, work=work)
    if name == "READ":
        return tool_read(body.strip(), work=work)
    if name == "LIST":
        return tool_list(work=work)
    if name == "DELETE":
        # 🔴 用户铁律：智能体不给删除能力（删除不可逆）。工具层不存在 DELETE。
        return "拒绝：你没有删除权限。删除一律由人来做——要清理什么，请在结论里点名说明，人会处理。"
    if name == "TASK":
        return task_fn(arg, body.strip())
    if name == "DONE":
        return None
    return "未知工具: %s（可用: EXEC / WRITE / READ / LIST / TASK / DONE）" % name


# ------------------------------------------------------------------ 工具循环
SYS = """你有真实工具，会被**真实执行**，你会拿到真实返回结果。严格按格式输出，不要解释、不要寒暄：

[EXEC]python代码[/EXEC]        执行代码 → 你拿到真实的 stdout/stderr
[TASK:可选目标名]给另一个AI的任务[/TASK]   派活给另一个AI → 你拿到它的真实回答
[WRITE]文件名\\n内容[/WRITE]     写文件（第一行是文件名）
[READ]文件名[/READ]             读文件
[LIST][/LIST]                  列工作区文件（能看到同伴写的东西，也可以读它）
[DONE]结论[/DONE]              宣布完成

规则：
1. 每次只发一个工具调用，等结果回来再决定下一步。
2. **必须真的用 [EXEC] 验证**你的设计，不许只写不跑；跑出错就改代码再跑，直到跑通。
3. 不要臆造输出——只相信工具返回给你的真实结果。
4. 写文件时用你自己的代号做前缀，避免和同伴互相覆盖。
5. **必须把能跑的实现 [WRITE] 落盘**：只写在对话里的代码对同伴没有留下任何东西，
   同伴也无法引用、复现或反驳你。写完再 [EXEC] 跑一次确认它真能跑。
6. 🔴 **工具调用没有次数预算**：你想跑多少次就跑多少次，想改多少遍就改多少遍，
   一直做到你自己认为跑通、可以交付为止——自主性全在你手里。
   唯一的例外是别卡在同一件事上空转（那会被判定为循环）。
7. 🔴 **你没有删除权限，也不要尝试删除任何东西**（没有 DELETE 工具；EXEC 里的
   rm / del / os.remove / shutil.rmtree 等一律被拒执行）。删除不可逆，清理由人来做。
   想去掉某个旧文件，就在结论里点名说明，人来处理；在此之前只管写新的、跑新的。"""


def run(agent, task, steps=0, agents=None, verbose=True, work=None, timeout=30,
        wall=1800):
    """驱动一个 agent 用工具完成任务。返回最终 DONE 的结论。

    steps=0（默认）= 不设步数预算，做到它自己 DONE 为止；
    wall 是唯一兜底：单个 agent 卡死/循环超过这个秒数才停。
    """
    agents = agents or [agent]
    task_fn = make_task_tool(agents, agent["name"])
    os.makedirs(work or WORK, exist_ok=True)
    # 关键：工具说明必须放进 user 消息（放 system 里会被多数免费模型忽略）
    msgs = [{"role": "system", "content": SYS},
            {"role": "user", "content": SYS + "\n\n你的代号：" + agent["name"]
                                       + "\n\n任务：\n" + task}]
    t0 = time.time()
    i = 0
    while True:
        i += 1
        if steps and i > steps:
            break
        if time.time() - t0 > wall:
            break
        try:
            out = agent["call"](msgs) or ""
        except Exception as e:
            out = "[调用失败: %r]" % e
        m = TOOL_RE.search(out)
        if not m:
            # 容错：没按格式调用工具，但输出了代码块 → 直接拿来执行（让它"能尝试"）
            cb = re.search(r"```(?:python|py|bash|sh)?\s*\n(.*?)```", out, re.S)
            if cb and cb.group(1).strip():
                res = tool_exec_py(cb.group(1), timeout=timeout, work=work)
                if verbose:
                    print("  [%d] (自动提取代码块执行) → %s" % (i, res.splitlines()[0][:80]))
                msgs.append({"role": "assistant", "content": out})
                msgs.append({"role": "user",
                             "content": "我已自动执行了你的代码块，真实返回：\n%s\n\n"
                                        "据此继续（用 [EXEC]/[TASK]/[DONE] 等工具格式）。" % res})
                continue
            msgs.append({"role": "assistant", "content": out})
            msgs.append({"role": "user",
                         "content": "请严格用 [TOOL:名]…[/TOOL] 格式调用工具。（你刚才没调用任何工具）"})
            if verbose:
                print("  (第%d步 未调用工具，已提醒)" % i)
            continue
        name, arg, body = m.group(1), m.group(2) or "", m.group(3)
        res = run_tool(name, arg, body, task_fn, work=work, timeout=timeout)
        if verbose:
            head = (body.strip().splitlines() or [""])[0][:60]
            print("  [%d] %s %s → %s" % (i, name, (arg or head),
                                         (res or "DONE").splitlines()[0][:80]))
        if name.upper() == "DONE":
            return body.strip()
        msgs.append({"role": "assistant", "content": out})
        msgs.append({"role": "user",
                     "content": "工具 %s 的真实返回结果：\n%s\n\n据此继续（继续用工具，或 DONE）。"
                                % (name, res)})
    return "（步数用尽，未 DONE）"


if __name__ == "__main__":
    import heartbeat_relay as H
    agents = H.collect_agents()
    print("可用 agent %d 个，挑前 3 个用于互派演示：%s"
          % (len(agents), ", ".join(a["name"] for a in agents[:3])))
    a = agents[0]
    task = sys.argv[1] if len(sys.argv) > 1 else (
        "用 Python 实现你正在讨论的 PCLI 协议头打包/解包函数："
        "字段 MAGIC(4B)/TYPE(1B)/FLAGS(1B)/STREAM_ID(2B)/SEQ(4B)，用 struct 打包，"
        "然后**必须真的用 EXEC_PY 跑一遍**，验证打包后长度正好是 12 字节、解包能还原字段。"
        "跑通后，用 TASK 把你的实现发给另一个 AI 让它审查有没有 bug，"
        "并根据它的反馈改代码再跑一次，最后 DONE 给出最终代码和验证结果。")
    print("\n=== 执行 agent: %s ===\n" % a["name"])
    print(run(a, task, agents=agents[:6]))
