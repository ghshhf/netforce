# -*- coding: utf-8 -*-
"""netforce / ocr_daemon.py —— 扫描 PDF 的 OCR 常驻循环。

为什么要单独一个循环：
  ocr_scanned.py 是「跑一批页就退」的批处理，原本靠 autoloop 每轮捎带一次
  （--pages 40 --max-seconds 60）。实测一轮只能推进 3~13 页，而总量 1855 页，
  按 autoloop 8~12 分钟一轮的节奏要跑几十小时。这里把它拆出来独立推进：
  单轮给 80 页 / 15 分钟，做完立刻开下一批，跑完（连续 3 轮无页可做）就自行退出，
  不留空转进程。

互斥：ocr_scanned.py 内已有命名互斥体（Global\\netforce_ocr_scanned_v1），
即使 autoloop 同时调它也不会重复 OCR 同一页。
"""
import os
import re
import subprocess
import sys
import time

import headless

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
SCRIPT = os.path.join(HERE, "ocr_scanned.py")
LOG = os.path.join(HERE, "_kb", "evolve", "ocr_daemon.log")


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def main():
    log("ocr_daemon 启动 pid=%d" % os.getpid())
    idle = 0
    total = 0
    while True:
        try:
            r = headless.run(
                [PY, SCRIPT, "--pages", "80", "--max-seconds", "900"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=1200)
            out = (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            log("单轮超时（>20 分钟），继续下一批")
            continue
        except Exception as e:  # noqa: BLE001
            log("调用失败：%s" % e)
            time.sleep(30)
            continue

        # 输出是 "[ocr] 本轮 N 页 / Xs / 完成文件 M 个"；中文在 GBK 控制台下可能
        # 显示成乱码，所以只锚定 ASCII 前缀 [ocr] 和紧随其后的第一个整数。
        m = re.search(r"\[ocr\][^\d]*(\d+)", out)
        n = int(m.group(1)) if m else 0
        if "已有实例在跑" in out:
            time.sleep(20)
            continue
        total += n
        log("本轮 %d 页（累计 %d）rc=%d" % (n, total, r.returncode))
        if n == 0:
            idle += 1
            if idle >= 3:
                log("连续 3 轮无页可做，OCR 全部完成，daemon 退出（累计 %d 页）" % total)
                return
            time.sleep(30)
        else:
            idle = 0


if __name__ == "__main__":
    main()
