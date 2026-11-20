"""
netforce · 极简本地智能体（用户要的本质单元）

逻辑就一句：给它一个文件夹，把问题丢给它，它就在文件夹里干活、给你结果。

- 命令行进程（agent），用免费 LLM 当脑（brain.call_brain）
- 工作区 = 一个文件夹（读写都限制在这亩地里，这是你定的"给它一个文件夹"）
- 你给任务 → 它自己规划、读写文件、反复迭代 → 汇报结果
- 不搞 relay / 互联网信标 / 沙箱逃逸那些花活

用法：
  python agent_cli.py --folder ./work --task "把 notes.txt 摘要成 3 条"
  （不传 --task 则读 folder/task.txt）
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from brain import call_brain

SYSTEM = """你是一个本地智能体，工作区是一个文件夹。你通过调用工具来完成任务，最后输出结果。

可用工具（每条单独成行，严格这种格式）：
  LIST                      列出工作区当前文件
  READ|<相对路径>            读取文件内容
  WRITE|<相对路径>|<内容>     写入/覆盖文件（内容可跨多行，直到下一个工具行）
  DONE|<你的回答>            任务完成，给出最终回答/结论

规则：
- 相对路径相对于工作区文件夹，不要写绝对路径、不要跳出文件夹。
- 一次回复可以包含多个工具调用（每行一个），也可以直接 DONE。
- 先想清楚再动手：必要时多次 LIST/READ 了解情况，再 WRITE 产出，最后 DONE。
- 用中文思考与回答。
"""


def safe_path(folder, rel):
    # 极简边界：只允许落在 folder 内（尊重"给它一个文件夹"的设计，不是沙箱逃逸研究）
    p = os.path.normpath(os.path.join(folder, rel))
    if not (p == folder or p.startswith(folder + os.sep)):
        raise ValueError(f"路径越界: {rel}")
    return p


def run_tool(folder, line):
    if line.startswith("LIST"):
        try:
            items = sorted(os.listdir(folder))
        except FileNotFoundError:
            items = []
        return "FILES: " + (", ".join(items) if items else "(空)")
    if line.startswith("READ|"):
        rel = line[5:].strip()
        try:
            with open(safe_path(folder, rel), "r", encoding="utf-8") as f:
                return f"CONTENT of {rel}:\n" + f.read()
        except Exception as e:  # noqa: BLE001
            return f"READ ERR {rel}: {e}"
    if line.startswith("WRITE|"):
        rest = line[6:]
        if "|" in rest:
            rel, first = rest.split("|", 1)
            rel = rel.strip()
        else:
            rel, first = rest.strip(), ""
        try:
            p = safe_path(folder, rel)
            os.makedirs(os.path.dirname(p) or folder, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(first)
            return f"WROTE {rel} ({len(first)} chars)"
        except Exception as e:  # noqa: BLE001
            return f"WRITE ERR {rel}: {e}"
    return f"UNKNOWN TOOL: {line}"


def parse_and_exec(folder, out, history):
    """顺序解析回复里的工具调用，WRITE 内容跨多行累积到下一个工具行为止。"""
    buf = None  # (rel, content)
    done = None

    def flush():
        nonlocal buf
        if buf:
            rel, content = buf
            res = run_tool(folder, f"WRITE|{rel}|{content}")
            history.append(f"[工具结果] {res}")
            buf = None

    for raw in out.splitlines():
        s = raw.strip()
        if s.startswith("WRITE|"):
            flush()
            rest = s[6:]
            if "|" in rest:
                rel, first = rest.split("|", 1)
                buf = (rel.strip(), first)
            else:
                buf = (rest.strip(), "")
        elif s.startswith("READ|") or s.startswith("LIST"):
            flush()
            res = run_tool(folder, s)
            history.append(f"[工具结果] {res}")
        elif s.startswith("DONE|"):
            flush()
            done = s[5:].strip()
        else:
            if buf is not None:
                buf = (buf[0], buf[1] + "\n" + raw)
    flush()
    return done


def solve(folder, task, rounds=8):
    history = [f"任务：{task}"]
    for i in range(rounds):
        prompt = SYSTEM + "\n\n" + "\n".join(history) + "\n\n（现在请继续，给出工具调用或 DONE）"
        out = call_brain(prompt)
        history.append(f"[第{i + 1}轮回复]\n{out}")
        done = parse_and_exec(folder, out, history)
        if done is not None:
            return done, history
    return "(达到轮次上限仍未 DONE，最后状态见日志)", history


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", required=True, help="工作区文件夹")
    ap.add_argument("--task", default=None, help="任务描述；省略则读 folder/task.txt")
    ap.add_argument("--rounds", type=int, default=8)
    args = ap.parse_args()

    folder = os.path.abspath(args.folder)
    os.makedirs(folder, exist_ok=True)

    task = args.task
    if not task:
        tp = os.path.join(folder, "task.txt")
        if os.path.exists(tp):
            task = open(tp, encoding="utf-8").read().strip()
    if not task:
        print("没给任务：传 --task 或在文件夹放 task.txt")
        return

    print(f"== 智能体启动 ==\n工作区: {folder}\n任务: {task}\n")
    answer, log = solve(folder, task, args.rounds)
    print("== 结果 ==\n" + answer)
    log_path = os.path.join(folder, "_agent_log.txt")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(log))
    print(f"\n(过程日志已存 {log_path})")


if __name__ == "__main__":
    main()
