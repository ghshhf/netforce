"""Kaggle GPU 探针：确认加速器型号、显存、CUDA 可用性，以及可用时长。"""
import json
import os
import subprocess
import sys


def sh(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return "ERR: %s" % e


print("=" * 55)
print("1) nvidia-smi")
print("=" * 55)
print(sh(["nvidia-smi"]))

print("=" * 55)
print("2) torch + cuda")
print("=" * 55)
try:
    import torch
    print("torch      :", torch.__version__)
    print("cuda avail :", torch.cuda.is_available())
    print("cuda ver   :", torch.version.cuda)
    print("device cnt :", torch.cuda.device_count())
    for i in range(torch.cuda.device_count()):
        p = torch.cuda.get_device_properties(i)
        print("  GPU%d: %s | %.1f GB | sm_%d%d" % (
            i, p.name, p.total_memory / 1024 ** 3, p.major, p.minor))
    if torch.cuda.is_available():
        a = torch.randn(2000, 2000, device="cuda")
        b = torch.randn(2000, 2000, device="cuda")
        c = (a @ b).sum().item()
        print("matmul 2000x2000 ok, checksum=%.3f" % c)
except Exception as e:
    print("torch 不可用:", e)

print("=" * 55)
print("3) 环境")
print("=" * 55)
print("python:", sys.version.split()[0])
print("cwd   :", os.getcwd())
print("disk  :", sh(["df", "-h", "/kaggle/working"])[:300])
print("cpu   :", sh(["nproc"]).strip())

print("=" * 55)
print("4) 软件包")
print("=" * 55)
for pkg in ("torch", "transformers", "datasets", "accelerate", "peft", "trl", "bitsandbytes"):
    try:
        import importlib.metadata as md
        print("  %-14s %s" % (pkg, md.version(pkg)))
    except Exception:
        print("  %-14s (未安装)" % pkg)

print("=" * 55)
print("5) 写入测试")
print("=" * 55)
with open("/kaggle/working/probe_result.json", "w", encoding="utf-8") as f:
    json.dump({"ok": True, "torch": getattr(sys.modules.get("torch"), "__version__", None)}, f)
print("写入 /kaggle/working/probe_result.json 成功")
print("DONE")
