"""netforce 爬虫守护：每 5 分钟检查一次，进程不在就重新拉起（多目标版）。

为什么要有它：
    2026-10-18 凌晨，爬虫在没人动的情况下静默死了两次——一次是长时间运行后自己没了，
    一次是被我"清理 chrome 进程"误杀（爬虫的无头浏览器也被一起杀了）。
    `while True` 只保证"自己不主动停"，扛不住被杀/崩溃 ⇒ 外面必须有个看门狗。

设计要点：
  · 只判断"在不在"，不在才拉起 —— 不做定时重启（那会打断正在跑的批次、丢内存里的队列）
  · **多目标**：主发现器 + 闭环流水线 pipeline.py，各自独立判活/卡死/拉起（一个守护，不许多开）
  · 拉起用 DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP，脱离本守护独立存活
  · stdout/stderr 追加进各自的日志，日志不打架
  · 每次检查都打一行心跳到 _watchdog.log，方便事后看它到底干没干活

🔴 热重启（2026-11-04 新增）：
    pipeline.py --loop 每轮检查相关 .py mtime，代码变了就**做完当前批次**再优雅退出
    并写 `_kb/RELOAD_REQUEST`。守护下一轮发现"不在" ⇒ 用新代码拉起；
    进度在 `_kb/state.db` / `kb_index.db` / `assets.db`（SQLite 断点续跑），不丢不重。
    ⇒ 改代码不用手工杀进程，也不会打断正在跑的一批。

用法（后台常驻）：
    nohup python -u watchdog.py >> _watchdog.log 2>&1 &
停止：先 kill watchdog，再 kill 各目标（顺序反了会被重新拉起）。
"""
import os
import sys
import time
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable or "python"
LOG_CRAWLER = os.path.join(HERE, "_crawler.log")
LOG_PIPELINE = os.path.join(HERE, "_kb", "pipeline.log")
INTERVAL = 300          # 5 分钟（用户指定）

# (目标脚本, 参数, 产出日志, 卡死阈值秒)
# 主爬虫负责发现域名；pipeline 一个进程跑完 渲染→分类→索引→资源落盘，
# 不再单独守护 render_parse --loop（两个渲染器会抢同一把单例锁）。
# 一批 200 域 + 资源下载可能跑十几分钟 ⇒ 阈值放宽到 1.5 小时，别误杀正在干活的批次。
TARGETS = [
    {"name": "resource_finder.py", "args": [], "log": LOG_CRAWLER, "stall": 900},
    {"name": "pipeline.py", "args": ["--loop"], "log": LOG_PIPELINE, "stall": 5400},
]

# 🔴 用户要求（2026-10-18）：**绝不弹窗**。三个来源全部堵死：
#   ① 判活：只用 psutil（纯内存，零进程、零窗口）。psutil 不可用就**跳过本次检查**等下轮，
#      ——不再退回 PowerShell（哪怕带 CREATE_NO_WINDOW，实测仍会闪一下）。
#   ② 拉起：用 **pythonw.exe**（Windows 无控制台版本的 Python），天生不开窗口；
#      python.exe 即使带 DETACHED_PROCESS 在某些宿主下仍会闪一下。
#   ③ 本守护自己也用 pythonw 启动 ⇒ 连自身都不需要控制台，日志一律写文件。
WATCHDOG_LOG = os.path.join(HERE, "_watchdog.log")
PYW = os.path.join(os.path.dirname(PY), "pythonw.exe")
if not os.path.exists(PYW):
    PYW = PY


def log(msg):
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(WATCHDOG_LOG, "a", encoding="utf-8", errors="ignore") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _alive_psutil(target):
    import psutil
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cl = p.info.get("cmdline") or []
            if any(target in str(c) for c in cl):
                return str(p.info["pid"])
        except Exception:
            continue
    return None


def alive(target):
    """目标在不在？只看命令行（按进程名扫射会误伤别的 python 任务）。
    查不了就返回 "unknown"，调用方据此跳过本轮——宁可不拉，也不弹窗。"""
    try:
        return _alive_psutil(target) or None
    except ImportError:
        log("警告：psutil 未安装，本轮跳过检查（不会退回 PowerShell，避免弹窗）")
        return "unknown"
    except Exception as e:
        log("psutil 判活失败，本轮跳过：%s" % str(e)[:80])
        return "unknown"


# 🔴 第二条血的教训（2026-10-18 04:35）：**"进程在" ≠ "在干活"**。
# 03:28 爬虫就静默卡死了（日志停更），但守护一直到 03:56 还在报"在跑"——
# 只查进程在不在，僵尸进程照样占着 PID。⇒ 判活必须看**产出**：日志 mtime 超时 = 卡死。
def stall_seconds(logpath):
    try:
        return time.time() - os.path.getmtime(logpath)
    except Exception:
        return 0.0


def kill_tree(pid):
    """杀掉目标**及其子进程**（无头 Chrome 是它的孩子，不一起杀会变孤儿占内存）。
    ⚠️ 只按这棵树杀，绝不按进程名扫射 —— 别的 Chrome（用户自己的浏览器）不动。"""
    killed = []
    try:
        import psutil
        p = psutil.Process(int(pid))
        kids = p.children(recursive=True)
        for k in kids:
            try:
                k.kill()
                killed.append(k.pid)
            except Exception:
                pass
        p.kill()
        killed.append(int(pid))
    except Exception as e:
        log("杀进程失败：%s" % str(e)[:80])
    return killed


def _hidden_startupinfo():
    """Windows 隐藏控制台窗口的标准组合（2026-10-18 联网核实后补齐）。

    之前只用了 CREATE_NO_WINDOW，**窗口还是会闪** —— 漏的是 STARTUPINFO：
      · 必须 `dwFlags |= STARTF_USESHOWWINDOW`，否则 wShowWindow 被忽略；
      · wShowWindow = SW_HIDE（0）才是真正把窗口藏起来的那个开关。
    ⚠️ 每次 Popen 都要**新建** STARTUPINFO：Python 3.7+ 会把传入对象的内部状态改掉，
       复用同一个实例第二次就失效（官方 subprocess 文档的已知坑）。
    """
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def launch(entry):
    """拉起目标。带 60 秒节流（按目标分开，避免一个目标挡住另一个）：

    🔴 防"两个守护同时判定不在、各起一个" —— 历史上跑出过 5 个爬虫。
    """
    name = entry["name"]
    stamp = os.path.join(HERE, "_last_launch_" + name.replace(".py", ""))
    try:
        if time.time() - os.path.getmtime(stamp) < 60:
            return
    except Exception:
        pass
    try:
        open(stamp, "w").close()
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(entry["log"]), exist_ok=True)
    except Exception:
        pass
    fh = open(entry["log"], "a", encoding="utf-8", errors="ignore")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"      # 日志重定向后控制台默认 GBK，中文会崩
    env["PYTHONUTF8"] = "1"
    try:
        subprocess.Popen([PYW, "-u", name] + entry["args"], cwd=HERE,
                         stdout=fh, stderr=fh, env=env,
                         startupinfo=_hidden_startupinfo(),
                         creationflags=subprocess.CREATE_NO_WINDOW
                         | subprocess.DETACHED_PROCESS
                         | subprocess.CREATE_NEW_PROCESS_GROUP)
        log("已用 %s 拉起 %s %s（隐藏窗口）"
            % (os.path.basename(PYW), name, " ".join(entry["args"])))
    except Exception as e:
        log("拉起失败：%s" % str(e)[:120])


def already_running():
    """单例自检：已经有一个看门狗在跑就直接退出。

    🔴 血的教训（2026-10-18）：我前后起过 3 个看门狗、最终跑出 **5 个爬虫实例**——
    每次想"重启守护"时只杀了匹配到的第一个，旧的全留着。⇒ 守护必须自带单例保护。
    """
    me = os.getpid()
    try:
        import psutil
        others = []
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                if p.info["pid"] == me:
                    continue
                cl = [str(c) for c in (p.info.get("cmdline") or [])][1:]
                if any(c.endswith("watchdog.py") for c in cl):
                    others.append(p.info["pid"])
            except Exception:
                continue
        return others
    except Exception:
        return []


def main():
    # --once：检查一次就退出（给 Windows 计划任务用）。
    # 🔴 常驻看门狗自己也会死，"守护的守护"是套娃 ⇒ 交给系统级计划任务才是根解。
    once = "--once" in sys.argv
    if not once:
        others = already_running()
        if others:
            log("已有看门狗在跑（PID %s）⇒ 本实例直接退出，防止多开拖垮机器"
                % ", ".join(str(x) for x in others))
            return
        log("看门狗启动（无窗口，单例）：每 %d 秒检查 %d 个目标，日志 → %s"
            % (INTERVAL, len(TARGETS), WATCHDOG_LOG))
    while True:
        # 渲染器自己写完当前批次后优雅退出留下的标志：清掉即可，下一跳用新代码拉起
        flag = os.path.join(HERE, "_kb", "RELOAD_REQUEST")
        if os.path.exists(flag):
            try:
                os.remove(flag)
                log("收到代码热重启标志 ⇒ 等待渲染器退出后用新代码拉起")
            except Exception:
                pass
        for entry in TARGETS:
            pid = alive(entry["name"])
            if pid == "unknown":
                continue
            if pid:
                age = stall_seconds(entry["log"])
                if age > entry["stall"]:
                    log("⚠️ %s 卡死：%d 分钟无产出（PID %s）⇒ 杀进程树后重拉"
                        % (entry["name"], int(age // 60), pid))
                    log("   已杀：%s" % kill_tree(pid))
                    time.sleep(3)
                    launch(entry)
                else:
                    others = already_running()
                    extra = "（⚠️ 另有 %d 个守护实例）" % len(others) if others else ""
                    log("心跳：%s 在跑（PID %s，最近产出 %d 秒前）%s"
                        % (entry["name"], pid, int(age), extra))
            else:
                log("心跳：%s 不在 ⇒ 拉起" % entry["name"])
                launch(entry)
        if once:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
