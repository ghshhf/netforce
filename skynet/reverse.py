"""天网 · 逆向层（reverse）
===========================
前七层解决的是「去哪儿找 / 怎么拿 / 值不值得 / 记过什么 / 从哪儿出去 / 站里有什么 / 解析对不对」。
本层解决最后一件：**拿到的东西里还藏着什么**。

用户定调（2026-10-23，二次修正）：
    「逆向本质上是获得相关知识罢了，只要你不去用它那个东西赚钱，理论上没人去找你。」
    「不解密、不破授权、不绕付费墙、不撞登录态 —— 这条视情况而定，
      因为一般情况下大部分知识根本不需要去这样搞。」
    「不为盈利是真的 —— 真的没有人找我盈利，我也赚不到钱。」
    「与其说是无道德，不如说是**真正的工具**：想到什么就去得到什么。」
    ⇒ 一句话：**想要什么就去得到什么，去提取什么。**

所以边界的语言整个换了 —— **不做道德判断，只报代价**：

    · 工具不回答「该不该」，只回答「做了会怎样、代价多少、会不会把工具本身搞废」
    · 原来的 `REFUSE` 禁止清单拆成两张表：
        `COST_TAGS`  命中**不拦**，只在结果里标注这一步的真实代价（封号/风控/不可逆）
        `HARD_REFUSE` 几乎空 —— 因为唯一真硬的那条发生在代码之外（见下）
    · 唯一真硬的一条：**不为营利**。它不是道德，是**风险模型的前提** ——
      「不赚钱」正是「没人来找你」的充要条件，破了这条整套模型失效。
      而它形不成目标字符串匹配（它是「你拿结果去干嘛」），所以留在这里当宣言，不进代码。
    · 云 AI 有风控是事实，用户的选择是：**在最大范围内执行，撞了再说，不为风控预缴税**。

凭据的默认档（注意：这是**默认安全档**，不是禁令）：
    疑似密钥/token 默认只报「几处、什么类型」不给值 —— 理由是代价不可逆：
    一旦落盘就进了产物/快照/同步链路，收不回来。开关在人手里（`--secrets`）。

三档提取器（按 netforce 铁律，全部纯命令行，**不碰浏览器**）：

    web     从 HTML/JS 抽：API 端点、内联状态 JSON、sourcemap、隐藏路径
    bin     从二进制/文本抽：可打印字符串、URL、版本号、路径骨架
    video   从视频抽：字幕/弹幕（yt-dlp）+ 关键帧时间轴（ffmpeg）

与「记忆不压缩」定案一致（重要）：
    抽帧/转录只是**加载层**的有损裁剪，**原文一律留盘**（`keep_raw` 默认开）。
    对照 bilibili-ai-bot 的 `download → 抽 3 帧 → delete_video`：那是压存储层，
    正是我们判死刑的做法，本层**反着来** —— 抽完的帧和文本进 `reverse_out/`，原片不动。

--------------------------------------------------------------------------
2026-10-23 融合：从开源逆向项目里提取的知识（只取知识，不搬代码）
--------------------------------------------------------------------------
搜了一圈开源逆向项目，挑了三个真正有东西的，把它们**最硬的那条规矩**搬过来：

  1. morluto/rea（本周 GitHub 第一，周增 11.6 万星 · MIT）
     它自己不造反编译引擎，只是把 Ghidra / IDA / Hopper 编排给 AI agent，
     **但每个结论都带 Evidence 记录：SHA-256、artifact 身份、authority 分级、
     limitations 显式列出未知项**。
     → 我们搬的就是这条：**逆向最容易出的错不是「解不出来」，是「解不出来但编了一个」**。
       所以本层所有产出改为带证据：实物读出来的 / 自己跑一遍看到的 / 外部已知的 /
       我们猜的 —— 四级分开，猜的只当线索不当结论。
     它还有两条顺手可用的：`--snapshot`（引擎启动慢，成功结果缓存复用）、
     `investigate-versions`（两个版本做结构差分 —— 找变更最快的一招）。都接了。

  2. zhaoxuya520/reverse-skill（3.8 万星 · MIT）
     本体不是工具包，是**路由引擎**：44 条规则按「目标类型 × 用户意图 × 工具链」
     三轴消歧，命中才允许读对应 SKILL.md；**未命中就提议新技能，不硬塞**。
     外加三条我们直接照抄的：
       · `tool-index.md` 自动生成 —— "永远查索引，不许猜路径"（我们改成 tool_index()）
       · 只读默认 + 授权门：读不构成执行授权，有副作用前先列计划
       · field-journal 自进化：5 类触发才写回，写回必须同步更新 `_index.md`（检索层）
     最后一条和我们记忆树的「写前 --dup / 流水随便记、常驻必须精」是同一套思路，
     算是互相印证 —— 所以接进来了，位置在 `reverse_out/field_journal/`。

  3. MG1937/ASC + i12bp8/decomp.wtf
     ASC 给 AI agent 用，特点是**按需抽取不预索引**（毫秒级，别一次 dump 全集）；
     decomp.wtf 是「重建后编译回原字节」验证。
     → ASC 那条直接变成我们的 `limit` 默认收紧 + 按需抽；
       decomp.wtf 那条变成 `diff_versions()`：差分即验证，解不出来时至少能说清变了什么。

能力边界（36 氪对 REA 的 51 次公开记录统计，写在这里免得自己骗自己）：
    29 达成 / 16 部分 / 6 失败；扣掉作者自己做的 9 件，42 件独立记录里只有 20 件成功，
    其中 9 件是小程序、6 件是中型应用的单一功能。
    失效点：强虚拟化壳（VMP）、缺动态调试、服务器端逻辑够不着、输出吃上下文、复杂目标卡顿。
    **在已有积累的领域是加速器，在完全陌生的领域只是探索工具。**

对外：
    from_html(html, base_url="")      → WebIntel
    from_blob(path, max_strings=400)  → BlobIntel
    from_video(target, out_dir=None)  → VideoIntel
    scan(target, kind="auto")         → 自动判档 + 提取 + 落盘（返回带 evidence）
    route(target, intent="overview")  → 三轴路由（不干活，只说该走哪条路）
    tool_index()                      → 本机工具台账（不许猜路径）
    diff_versions(a, b)               → 两版本结构差分
    journal_write(...) / journal_search(kw)  → 经验写回与检索
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import net

# ---------------------------------------------------------------- 边界：代价标注，不是禁止清单
# 2026-10-23 二次定调后改的。原来那张 REFUSE 是「命中即拒」，
# 用户说：不绕授权这条**视情况而定** —— 大部分知识根本不需要这样搞，
# 为了极少数情况立一堆死规矩，等于给工具自己上锁。
#
# 所以语言换了：**不判对错，只报代价。** 命中不拦，只在结果里写清楚
# 「这一步会付出什么」，让用的人自己决定值不值。
#
# 这跟用户既有硬约束本来就是同一套东西 —— 那些约束从来不是道德，是成本核算：
#   · 他的账号只读、量全走 bot（曾因一天上万条被封）
#   · 高价值账号不得用于低价值批量（先算「封了亏多少」）
#   · 后台脚本不许弹窗（不是不许起，是代价是打扰）
COST_TAGS = {
    "撞登录态 / 付费墙": {
        "keys": ["paywall", "bypass-paywall", "绕过付费", "破解会员", "login-required",
                 "sessdata", "付费墙"],
        "cost": "会撞对方风控：IP/账号封禁、风控升级、以后更难拿。",
        "rule": "先算「封了亏多少」（用户既有硬约束）—— 亏得起就干，不是不许干。",
    },
    "主机 / 商用固件": {
        "keys": ["ps5", "ps4", "psvita", "xbox", "nintendo", "switch", "nx-", ".pup", ".pkg"],
        "cost": "法律风险显著高于其他目标（DMCA 反规避条款是专门的），且厂商取证积极。",
        "rule": "代价是法律不是技术 —— 知会一声即可，代码不拦。",
    },
    "凭据 / 身份材料": {
        "keys": ["sessdata", "bili_jct", "password", "private_key", "begin rsa private",
                 "seed phrase", "助记词"],
        "cost": "凭据一旦落盘就进产物/快照/同步链路，不可逆；且它是对方定损的依据。",
        "rule": "默认只报类型不给值（`--secrets` 可开）—— 默认档是安全档，不是禁令。",
    },
}

# 唯一真硬的一条写在这里，不进匹配表：
#   **不为营利。** 它不是道德，是风险模型的前提 ——「不赚钱」正是「没人来找你」的充要条件。
#   而它形不成目标字符串匹配（它是「你拿结果去干嘛」，发生在代码之外），
#   所以留作宣言；代码拦不住也不该拦，只有人自己拦得住。
HARD_REFUSE: dict = {}

# 凭据默认档：只计数、不取值。开关在人手里（--secrets）。
EXPOSE_CRED_VALUES = False

# 凭据类：本层唯一会「看见了但默认不看值」的东西。
CRED_HINT_RE = re.compile(
    r"(?:api[_-]?key|secret|token|passwd|password|Bearer\s+[A-Za-z0-9._-]{12,}"
    r"|sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|SESSDATA)", re.I)


def cost_tags(target: str) -> list:
    """返回目标命中的代价标注（**不拒绝**，只是把代价说清楚）。

    为什么是标注而不是拦截：工具该做的是把信息给全，不是替人决定。
    用户原话：「不绕授权这条视情况而定」「想到什么就去得到什么」。
    """
    low = str(target).lower()
    hits = []
    for tag, spec in COST_TAGS.items():
        for k in spec["keys"]:
            if k in low:
                hits.append({"tag": tag, "cost": spec["cost"], "rule": spec["rule"],
                             "matched": k})
                break
    return hits


# ================================================================ 证据层
# 从 REA 的 Evidence v2 搬来的最硬一条规矩：**结论必须带来源和「有多硬」**。
# 逆向的失败模式从来不是「读不懂」，是「读不懂但顺手编了一个，还写进了报告」。
AUTHORITY = {
    "shipped-artifact": "目标实物里直接读到的（字节/字符串/端点）——最硬，可复核",
    "controlled-replay": "我们自己跑一遍观察到的（抽帧/抓包/重放）——可复现，但依赖当时环境",
    "historical-reference": "外部已知信息（版本号对应上游发布）——需复核，可能已过时",
    "analyst-inference": "我们推断的（看着像 API、大概是个入口）——最软，只当线索不当结论",
}


@dataclass
class Evidence:
    """一条带来源的结论。

    `authority` 是这个类的灵魂：它把「我读到的」和「我猜的」强制分开。
    agent 拿到的结果里只要混进了 inference 却当 shipped 用，就是这个字段没起作用。
    """
    id: str                       # sha256 前 16 位，便于引用与去重
    facet: str                    # 观察面：endpoints / strings / frames / ...
    claim: str                    # 结论（人读的）
    value: str                    # 值（截断到 400 字符，避免吃上下文）
    authority: str                # 见 AUTHORITY
    subject: str = ""             # 目标标识（路径或 URL）
    limitations: list = field(default_factory=list)   # 显式列出「我没看到什么」

    def to_dict(self) -> dict:
        return {"id": self.id, "facet": self.facet, "claim": self.claim,
                "value": self.value, "authority": self.authority,
                "subject": self.subject, "limitations": self.limitations}


def mk_evidence(facet: str, claim: str, value, authority: str,
                subject: str = "", limitations=None) -> Evidence:
    """造一条证据。id 对 (facet, claim, value, authority) 取 sha256 —— 同一结论同一 id。

    `limitations` 是 REA 里最值得学的一格：它逼你写「我没看到什么」。
    逆向报告里最贵的谎言不是假话，是**省略**。
    """
    v = str(value)[:400]
    eid = hashlib.sha256(
        f"{facet}|{claim}|{v}|{authority}".encode("utf-8", "replace")).hexdigest()[:16]
    return Evidence(id=eid, facet=facet, claim=claim, value=v, authority=authority,
                    subject=subject, limitations=list(limitations or []))


# ================================================================ 工具台账
# 从 reverse-skill 的 tool-index.md 照搬的思路：**永远查索引，不许猜路径**。
# 它把 tool-index.md 设为 gitignore、每台机器单独生成 —— 于是「路由逻辑全机器通用、
# 工具路径本机自适应」。我们一样：探测结果落 `reverse_out/_toolindex.json`，不进仓库。
TOOL_SPECS = [
    # (命令, 名字, 能干什么, 是否有副作用, 版本参数)
    ("r2", "radare2", "反汇编 / 字符串 / 符号 / 交叉引用（命令行）", False, "-v"),
    ("rizin", "Rizin", "radare2 分支，命令行逆向", False, "-v"),
    ("objdump", "GNU objdump", "ELF/PE 节表与反汇编", False, "--version"),
    ("ghidra", "Ghidra", "反编译（headless 模式，启动慢）", True, "-version"),
    ("jadx", "JADX", "DEX/APK → Java", False, "--version"),
    ("apktool", "Apktool", "APK 解包（资源 + smali）", False, "--version"),
    ("ffmpeg", "FFmpeg", "视频抽帧 / 转码", False, "-version"),
    ("yt-dlp", "yt-dlp", "视频元数据 / 字幕 / 弹幕", False, "--version"),
    ("tshark", "TShark", "pcap 摘要（端点 / DNS / TLS SNI）", False, "-v"),
    ("mitmdump", "mitmproxy", "HTTP 流量录制", True, "--version"),
    ("binwalk", "Binwalk", "固件分区与提取", False, "--version"),
    ("uncompyle6", "uncompyle6", "Python 字节码 → 源码", False, "--version"),
]


def _which_version(cmd: str, ver_arg: str) -> tuple:
    """查命令在不在、版本多少。返回 (路径, 版本串)。"""
    p = shutil.which(cmd)
    if not p:
        return "", ""
    rc, out = _run([cmd, ver_arg], timeout=20)
    line = ""
    if rc == 0 and out.strip():
        line = out.strip().splitlines()[0][:60]
    return p, line


def register_tool(out_dir, cmd: str, path: str, name: str = "", capability: str = "") -> dict:
    """人工登记一个不在 PATH 里的工具（绿色版 Ghidra / 便携版 jadx 就是这种情况）。

    台账的原则是「不许猜路径」，但 PATH 之外装了确实是装了 ——
    这种事只能由人明确给路径，机器不替人猜。登记后刷新索引时会保留。
    """
    if not cmd or not path:
        return {"ok": False, "note": "用法：--register ghidra=D:/tools/ghidra/.../analyzeHeadless.bat"}
    out = Path(out_dir) if out_dir else DEFAULT_OUT
    cache = out / "_toolindex.json"
    manual = {}
    if cache.exists():
        try:
            manual = json.loads(cache.read_text(encoding="utf-8")).get("manual", {})
        except Exception:
            manual = {}
    spec = next((s for s in TOOL_SPECS if s[0] == cmd), None)
    manual[cmd] = {"path": str(path), "registered": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                   "name": name or (spec[1] if spec else cmd),
                   "capability": capability or (spec[2] if spec else "")}
    out.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"manual": manual}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "registered": manual[cmd], "note": "已登记，下次 --tools 会带上它"}


def tool_index(out_dir=None, refresh: bool = True) -> dict:
    """生成本机工具台账。没有的工具记 missing，**不猜路径**。

    为什么值得单独做：agent 最典型的翻车是「上来就调 IDA，而这台机器上没装」。
    reverse-skill 为此写了 163 条回归用例守着路由表；我们用不着那么多，
    但「先查索引再决定走哪条路」这条是一样的。

    PATH 之外装的工具走 `register_tool()` 人工登记 —— 机器探测 + 人工登记两路合并。
    """
    out = Path(out_dir) if out_dir else DEFAULT_OUT
    cache = out / "_toolindex.json"
    manual = {}
    if cache.exists():
        try:
            manual = json.loads(cache.read_text(encoding="utf-8")).get("manual", {})
        except Exception:
            manual = {}
    if not refresh and cache.exists():
        try:
            old = json.loads(cache.read_text(encoding="utf-8"))
            if "have" in old:
                return old
        except Exception:
            pass

    have, missing = {}, []
    for cmd, name, cap, side, varg in TOOL_SPECS:
        path, ver = _which_version(cmd, varg)
        if path:
            have[cmd] = {"name": name, "path": path, "version": ver,
                         "capability": cap, "side_effect": side, "source": "PATH"}
        elif cmd in manual:
            m = manual[cmd]
            have[cmd] = {"name": m.get("name", name), "path": m["path"], "version": "",
                         "capability": m.get("capability", cap), "side_effect": side,
                         "source": "manual", "registered": m.get("registered", "")}
        else:
            missing.append({"cmd": cmd, "name": name, "capability": cap})
    idx = {"generated": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "have": have, "missing": missing, "manual": manual,
           "note": "本文件是本机探测结果，不进仓库（与 reverse-skill 的 tool-index.md 同思路）"}
    try:
        out.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return idx


# ================================================================ 目标嗅探
# 扩展名会说谎（.bin 可能是 ELF，.dat 可能是 SQLite）。所以判类型看 magic bytes。
def sniff(path) -> str:
    """读前 16 字节判真实类型。比扩展名硬。"""
    try:
        p = Path(path)
        if p.exists() and p.is_file():
            head = p.open("rb").read(16)
        else:
            return "unknown"
    except Exception:
        return "unknown"
    if head[:2] == b"MZ":
        return "pe"
    if head[:4] == b"\x7fELF":
        return "elf"
    if head[:4] in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe"):
        return "macho"
    if head[:4] == b"PK\x03\x04":
        return "zip"          # apk / jar / whl 都是它，再看后缀细分
    if head[:3] == b"dex":
        return "dex"
    if head[:6] == b"\x00asm\x01\x00":
        return "wasm"
    if head[:15] == b"SQLite format 3":
        return "sqlite"
    if head[:4] in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4"):
        return "pcap"
    low = head.decode("latin-1", errors="replace").lstrip().lower()
    if low.startswith("<!doctype") or low.startswith("<html") or low.startswith("<?xml"):
        return "html"
    if low.startswith("{") or low.startswith("["):
        return "json"
    return "unknown"


def target_kind(target: str) -> str:
    """综合判定目标类型：URL → web；本地文件 → magic bytes；视频后缀 → video。"""
    s = str(target)
    if s.startswith("http://") or s.startswith("https://"):
        return "web"
    p = Path(s)
    if p.exists() and p.is_file():
        if p.suffix.lower() in VIDEO_EXT:
            return "video"
        k = sniff(p)
        if k == "zip":
            return "apk" if p.suffix.lower() == ".apk" else "jar"
        if k == "unknown":
            # 二进制里读不出 magic，就当文本兜底（至少能抽字符串）
            return "text" if p.suffix.lower() in {".txt", ".md", ".py", ".json", ".log"} else "bin"
        return k
    return "unknown"


# ================================================================ 三轴路由
# 从 reverse-skill 搬的最值钱的一块：**目标类型 × 用户意图 × 工具链** 三轴消歧。
# 它的原话是「同一个『分析这个文件』，可能通向完全不同的方法」——
# 拿到 APK 时，「理解本地算法」和「测线上接口」要走的路是两回事。
# 还有一条我们原样保留：**未命中就提议新技能，不硬塞**。
# 为了省事把任务塞进不合适的流程，比不干活更糟 —— 那会产生一堆带证据的错结论。
SKILLS = {
    "web-surface": {
        "facets": "endpoints / inline_state / sourcemap / graphql",
        "tools": [], "side_effect": False,
        "steps": ["取页面", "抽端点（/api/ 优先）", "看内联状态块", "找 sourcemap", "抽 GraphQL 操作名"],
    },
    "blob-surface": {
        "facets": "strings / urls / versions / paths / symbols",
        "tools": [], "side_effect": False,
        "steps": ["读可打印串", "抽 URL", "抽版本号", "抽路径骨架", "抽符号名"],
    },
    "native-deep": {
        "facets": "反汇编 / 交叉引用 / 伪代码",
        "tools": ["r2", "rizin", "objdump", "ghidra"], "side_effect": True,
        "steps": ["先扫字符串定位入口（便宜）", "r2 反汇编入口函数", "要语义才上 Ghidra（贵、慢）"],
    },
    "android-apk": {
        "facets": "manifest / classes / methods",
        "tools": ["apktool", "jadx"], "side_effect": False,
        "steps": ["apktool 解包看 manifest 与资源", "jadx 反编译关键类", "核心在 .so 就切 native-deep"],
    },
    "video-timeline": {
        "facets": "subtitle / danmaku / frames",
        "tools": ["yt-dlp", "ffmpeg"], "side_effect": True,
        "steps": ["取元数据", "字幕 → 弹幕（免费，先试）", "都没有才抽帧（烧存储）"],
    },
    "capture-flow": {
        "facets": "endpoints / dns / tls-sni",
        "tools": ["tshark", "mitmdump"], "side_effect": False,
        "steps": ["读 pcap 摘要", "抽端点与 SNI", "HTTP 层再进 mitmproxy"],
    },
    "firmware": {
        "facets": "分区 / 文件清单",
        "tools": ["binwalk"], "side_effect": True,
        "steps": ["binwalk 扫描分区", "提取后每件单独走 blob-surface"],
    },
    "pyc-source": {
        "facets": "python 源码",
        "tools": ["uncompyle6"], "side_effect": False,
        "steps": ["uncompyle6 还原", "还原失败就退回 blob-surface 读常量"],
    },
}

# 轴1（目标类型）→ 默认技能。注意这不是一一对应：zip 可能是 APK 也可能是 jar。
KIND2SKILL = {
    "web": "web-surface", "html": "web-surface", "text": "blob-surface",
    "json": "blob-surface", "bin": "blob-surface", "sqlite": "blob-surface",
    "pe": "native-deep", "elf": "native-deep", "macho": "native-deep", "wasm": "native-deep",
    "apk": "android-apk", "dex": "android-apk", "jar": "android-apk",
    "video": "video-timeline",
    "pcap": "capture-flow",
    "pyc": "pyc-source",
}

# 轴2（用户意图）→ 覆盖默认技能。同一目标，问法不同走的路不同。
INTENT2SKILL = {
    "endpoints": "web-surface",
    "strings": "blob-surface",
    "symbols": "native-deep",
    "diff": "blob-surface",
    "timeline": "video-timeline",
    "flow": "capture-flow",
    "source": "pyc-source",
}


def route(target: str, intent: str = "overview", out_dir=None,
          tools: Optional[dict] = None) -> dict:
    """三轴消歧：目标类型 × 用户意图 × 本机工具链 → 一条路。

    只回答「该走哪条」，**不干活** —— 先路由后动手是 reverse-skill 的立身之本：
    AI agent 的经典失败是拿到 APK 直接上 Frida，而核心逻辑其实藏在 .so 里，
    该走的是 IDA。选错工具，整条链路作废，还带着一串假证据。

    未命中就返回 `propose-new-skill`，**不硬塞**一个相近技能。
    """
    why = refuse_reason(target)
    if why:
        return {"route": "refused", "target": target, "reason": why}

    k = target_kind(target)
    skill = INTENT2SKILL.get(intent) or KIND2SKILL.get(k)
    if skill is None:
        return {"route": "propose-new-skill", "kind": k, "intent": intent,
                "target": target,
                "note": f"路由表没覆盖 kind={k} × intent={intent}。"
                        f"宁可说没覆盖，也不塞进相近流程 —— 那会产生带证据的错结论。"}

    spec = SKILLS[skill]
    idx = tools if tools is not None else tool_index(out_dir)
    have = set(idx.get("have", {})) if isinstance(idx, dict) else set()
    usable = [t for t in spec["tools"] if t in have]
    missing = [t for t in spec["tools"] if t not in have]

    return {
        "route": skill, "kind": k, "intent": intent, "target": target,
        "facets": spec["facets"], "steps": spec["steps"],
        "tools_usable": usable, "tools_missing": missing,
        "side_effect": spec["side_effect"],
        "degraded": bool(missing) and not usable,
        "cost": cost_tags(target) or None,
        "note": ("工具齐全" if not missing else
                 (f"缺 {missing}：降级为只读表面扫描（先扫字符串看看值不值得装）"
                  if usable or not spec["tools"] else
                  f"缺 {missing} 且无替代：只能做表面扫描，深挖需先装工具")),
    }


# ================================================================ 快照
# REA 用 --snapshot 缓存不可变分析结果：Ghidra 启动一次要几十秒，
# 同一个查询没道理重启引擎。我们同理 —— 扫同一个大文件不该扫第二遍。
def _snap_dir(out_dir=None) -> Path:
    return (Path(out_dir) if out_dir else DEFAULT_OUT) / "_snapshots"


def snapshot_key(target: str, kind: str, **kw) -> str:
    sig = f"{target}|{kind}|" + "|".join(f"{k}={v}" for k, v in sorted(kw.items()))
    return hashlib.sha256(sig.encode("utf-8", "replace")).hexdigest()[:16]


def snapshot_get(out_dir, key: str):
    f = _snap_dir(out_dir) / f"{key}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def snapshot_put(out_dir, key: str, payload: dict) -> str:
    d = _snap_dir(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{key}.json"
    try:
        f.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(f)
    except Exception:
        return ""


# ================================================================ 经验写回
# 从 reverse-skill 的 field-journal 搬的：**5 类触发才写，写回必须更新索引**。
# 它把「给系统自己看的经验」和「给人看的正式报告」严格分开 ——
# 混了，经验库就退化成会议纪要。这条和我们记忆树「流水随便记 / 常驻必须精」同源。
JOURNAL_TRIGGERS = (
    "任务完成并产出最终输出",
    "发现新工具坑位",
    "流程缺陷被修掉",
    "新场景未覆盖",
    "失败但有参考价值",
)


def journal_write(out_dir, category: str, scenario: str, summary: str,
                  pitfall: str = "", commands: str = "", reusable: str = "",
                  target: str = "") -> dict:
    """写一条经验。**未命中 5 类触发条件就不写** —— 否则日志会退化成流水噪声。

    写回同时更新 `_index.md`（场景分类 + 关键词 + 一句话），
    下次同类任务先查索引再动手。索引是检索层，不是堆日志的地方。
    """
    if category not in JOURNAL_TRIGGERS:
        return {"ok": False, "note": f"未命中触发条件，不写。允许的是：{list(JOURNAL_TRIGGERS)}"}
    d = (Path(out_dir) if out_dir else DEFAULT_OUT) / "field_journal"
    d.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today().isoformat()
    f = d / f"{today}.md"

    entry = [f"\n## {category} · {scenario}  ",
             f"- 目标：{target or '-'}  ",
             f"- 摘要：{summary}  "]
    if pitfall:
        entry.append(f"- 坑位：{pitfall}  ")
    if commands:
        entry.append(f"- 关键命令：`{commands}`  ")
    if reusable:
        entry.append(f"- 可复用：{reusable}  ")
    try:
        with f.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(entry) + "\n")
    except Exception:
        return {"ok": False, "note": f"写文件失败 {f}"}

    # 索引：一行一条，检索层
    idx = d / "_index.md"
    line = f"| {today} | {category} | {scenario} | {summary[:60]} | {target[:40]} |"
    try:
        if not idx.exists():
            idx.write_text("| 日期 | 触发类别 | 场景 | 一句话 | 目标 |\n|---|---|---|---|---|\n",
                           encoding="utf-8")
        with idx.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    return {"ok": True, "file": str(f), "index": str(idx)}


def journal_search(out_dir, keyword: str, limit: int = 10) -> list:
    """先查索引再动手 —— 与 `mem_tree.py --dup`（写前查重）同一条思路。"""
    idx = (Path(out_dir) if out_dir else DEFAULT_OUT) / "field_journal" / "_index.md"
    if not idx.exists():
        return []
    kw = str(keyword).strip().lower()
    if not kw:
        return []
    out = []
    for ln in idx.read_text(encoding="utf-8", errors="replace").splitlines():
        if kw in ln.lower():
            out.append(ln)
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------- web 规则
# 端点：带路径特征的 API 串。刻意放宽——宁可多收再由人筛，也别漏。
ENDPOINT_RE = re.compile(
    r"""(?:["'`]|\()\s*                        # 前引号或开括号
        ((?:/|https?://)[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{4,})  # 路径
        \s*(?:["'`]|\))""", re.X)             # 后引号或闭括号
API_HINT_RE = re.compile(r"/(?:api|v\d|graphql|rest|rpc|gateway|ajax|json)", re.I)
INLINE_STATE_RE = re.compile(
    r"(?:window\.__|__)(NEXT_DATA|NUXT__|INITIAL_STATE|INITIAL_|SSR_DATA|DATA__|PRELOADED_STATE)"
    r"[A-Z_]*\s*=\s*", re.I)
SOURCEMAP_RE = re.compile(r"//[#@]\s*sourceMappingURL\s*=\s*([^\s\"']+)", re.I)
HIDDEN_PATH_RE = re.compile(
    r"""(?:href|src|action|url)\s*=\s*["']([^"']{2,200})["']""", re.I)
GRAPHQL_RE = re.compile(r"""(?:query|mutation)\s+(\w+)\s*(?:\(|\{)""")
JS_BUNDLE_RE = re.compile(r"""["']([^"']+\.(?:js|mjs)(?:\?[^"']*)?)["']""", re.I)

# ---------------------------------------------------------------- bin 规则
PRINTABLE_RE = re.compile(rb"[\x20-\x7e]{4,}")
URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{6,}")
SEMVER_RE = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.]+)?\b")
WINPATH_RE = re.compile(r"[A-Za-z]:\\\\[^\s\"';]{3,80}")
POSIXPATH_RE = re.compile(r"(?<![\w/])/(?:usr|opt|var|etc|home|app|data)/[A-Za-z0-9._/-]{2,80}")
SYMBOL_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{3,40}\(")

# ---------------------------------------------------------------- video 规则
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".flv", ".avi", ".m4v"}
DEFAULT_OUT = Path(os.environ.get("NETFORCE_REVERSE_OUT", "reverse_out"))


@dataclass
class WebIntel:
    """网页里藏着的接口知识。"""
    base: str
    endpoints: list = field(default_factory=list)      # 疑似 API 端点（去重、按 API 特征排序）
    inline_state: list = field(default_factory=list)   # 内联状态块的 key 名
    bundles: list = field(default_factory=list)        # JS bundle 地址
    sourcemaps: list = field(default_factory=list)     # sourcemap 地址（有它 ≈ 能看原始源码）
    graphql: list = field(default_factory=list)        # GraphQL 操作名
    paths: list = field(default_factory=list)          # 页面里的相对路径
    cred_hits: int = 0                                 # 疑似凭据出现次数（默认不给值）
    cred_values: list = field(default_factory=list)    # 仅 --secrets 时填

    def summary(self) -> dict:
        d = {"kind": "web", "base": self.base, "endpoints": len(self.endpoints),
             "inline_state": len(self.inline_state), "bundles": len(self.bundles),
             "sourcemaps": len(self.sourcemaps), "graphql": len(self.graphql),
             "paths": len(self.paths), "cred_hits": self.cred_hits}
        if self.cred_values:
            d["cred_values"] = len(self.cred_values)
        return d


@dataclass
class BlobIntel:
    """二进制/文本里能读出来的结构知识。"""
    path: str
    size: int
    strings: list = field(default_factory=list)
    urls: list = field(default_factory=list)
    versions: list = field(default_factory=list)
    win_paths: list = field(default_factory=list)
    posix_paths: list = field(default_factory=list)
    symbols: list = field(default_factory=list)
    cred_hits: int = 0
    cred_values: list = field(default_factory=list)    # 仅 --secrets 时填

    def summary(self) -> dict:
        d = {"kind": "bin", "path": self.path, "size": self.size,
             "strings": len(self.strings), "urls": len(self.urls),
             "versions": len(self.versions), "win_paths": len(self.win_paths),
             "posix_paths": len(self.posix_paths), "symbols": len(self.symbols),
             "cred_hits": self.cred_hits}
        if self.cred_values:
            d["cred_values"] = len(self.cred_values)
        return d


@dataclass
class VideoIntel:
    """视频里能读出来的：字幕/弹幕 + 关键帧时间轴。"""
    target: str
    meta: dict = field(default_factory=dict)
    subtitle: str = ""            # 字幕/弹幕文本（原文，不摘要）
    frames: list = field(default_factory=list)   # 抽出的帧文件路径
    out_dir: str = ""
    keep_raw: bool = True
    note: str = ""

    def summary(self) -> dict:
        return {"kind": "video", "target": self.target, "title": self.meta.get("title", ""),
                "duration": self.meta.get("duration"), "subtitle_chars": len(self.subtitle),
                "frames": len(self.frames), "out_dir": self.out_dir,
                "keep_raw": self.keep_raw, "note": self.note}


# ---------------------------------------------------------------- 闸门
def refuse_reason(target: str) -> str:
    """命中硬清单则返回理由，否则返回空串（**默认永远返回空串**）。

    2026-10-23 二次定调后，这个函数从「第一道闸」降级成「一个几乎不会响的警报」：
    边界改成代价标注了 —— 该知道的是代价，不是准许。`HARD_REFUSE` 默认空，
    所以默认放行；真要拦，往 `HARD_REFUSE` 里加一条即可，仍然是「一句话能改回」。

    保留这个函数名是为了兼容三处调用点（scan / route / diff_versions）和老脚本。
    代价标注走 `cost_tags()`，那才是现在真正干活的那个。
    """
    low = str(target).lower()
    for why, keys in HARD_REFUSE.items():
        for k in keys:
            if k in low:
                return why
    return ""


def _dedup(seq, limit: int):
    seen, out = set(), []
    for s in seq:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 提取器 1：web
def from_html(html: str, base_url: str = "", limit: int = 60,
              expose_creds: bool = EXPOSE_CRED_VALUES) -> WebIntel:
    """从 HTML/JS 里抽接口知识。纯函数，离线可跑。

    为什么值得单独做一层：页面给看的是渲染结果，而**接口才是这个站真正的数据形状**。
    知道它有哪些端点、有没有内联状态、有没有 sourcemap，等于知道这个站能被怎么读。

    `expose_creds`：疑似凭据**给不给值**。默认 False —— 这是**默认安全档不是禁令**：
    凭据一旦落盘就进产物/快照/同步链路，收不回来；开关在人手里。
    """
    intel = WebIntel(base=base_url or "")

    raw_paths = [m.group(1) for m in ENDPOINT_RE.finditer(html)]
    # 有 API 特征的排前面：一个站几百个路径，/api/ 开头的才是接口
    api = [p for p in raw_paths if API_HINT_RE.search(p)]
    rest = [p for p in raw_paths if not API_HINT_RE.search(p)]
    intel.endpoints = _dedup(api, limit) + _dedup(rest, max(0, limit - len(api)))

    intel.inline_state = _dedup([m.group(1) for m in INLINE_STATE_RE.finditer(html)], 10)
    intel.bundles = _dedup([m.group(1) for m in JS_BUNDLE_RE.finditer(html)], 20)
    intel.sourcemaps = _dedup([m.group(1) for m in SOURCEMAP_RE.finditer(html)], 10)
    intel.graphql = _dedup([m.group(1) for m in GRAPHQL_RE.finditer(html)], 20)
    intel.paths = _dedup([m.group(1) for m in HIDDEN_PATH_RE.finditer(html)], limit)
    hits = CRED_HINT_RE.findall(html)
    intel.cred_hits = len(hits)
    if expose_creds:
        intel.cred_values = _dedup([str(h) for h in hits], 20)
    return intel


# ---------------------------------------------------------------- 提取器 2：bin
def from_blob(path, max_strings: int = 400,
              expose_creds: bool = EXPOSE_CRED_VALUES) -> BlobIntel:
    """从文件里抽可打印知识与结构骨架。

    刻意不接 radare2 / Ghidra：那些是**深度逆向工具**，本层的定位是「先读一遍字符串，
    看看值不值得交给专业工具」，不是替代它们。真要深挖再上 Ghidra（需人工登记路径）。

    `expose_creds`：疑似凭据**给不给值**，默认 False（安全档，非禁令）。
    """
    p = Path(path)
    data = p.read_bytes()
    intel = BlobIntel(path=str(p), size=len(data))

    strs = []
    for m in PRINTABLE_RE.finditer(data):
        s = m.group().decode("ascii", errors="replace").strip()
        if len(s) >= 4:
            strs.append(s)
    intel.strings = _dedup(strs, max_strings)
    blob_text = "\n".join(intel.strings)

    intel.urls = _dedup(URL_RE.findall(blob_text), 60)
    intel.versions = _dedup(SEMVER_RE.findall(blob_text), 40)
    intel.win_paths = _dedup(WINPATH_RE.findall(blob_text), 40)
    intel.posix_paths = _dedup(POSIXPATH_RE.findall(blob_text), 40)
    intel.symbols = _dedup([s[:-1] for s in SYMBOL_RE.findall(blob_text)], 60)
    hits = CRED_HINT_RE.findall(blob_text)
    intel.cred_hits = len(hits)
    if expose_creds:
        intel.cred_values = _dedup([str(h) for h in hits], 20)
    return intel


# ---------------------------------------------------------------- 提取器 3：video
def _run(cmd: list, timeout: int = 120) -> tuple:
    """跑外部命令。继承项目铁律：后台不弹窗的三件套一个都不能少。"""
    try:
        r = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            startupinfo=net._hidden_startupinfo(),                       # noqa: SLF001
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdin=subprocess.DEVNULL,
        )
        return r.returncode, (r.stdout or b"").decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""


def from_video(target: str, out_dir=None, frames: int = 6, keep_raw: bool = True) -> VideoIntel:
    """从视频抽「字幕/弹幕 + 关键帧时间轴」。

    对照 claude-video 的思路：**不是让模型长眼睛，是把视频翻译成模型本来就能读的三样东西
    —— 帧图 + 文字 + 时间轴。** 但有一处我们改了：它抽完就 `delete_video`，
    我们 `keep_raw=True` 默认留盘 —— 抽帧是加载层的有损裁剪，原文不该跟着一起消失。
    """
    out = Path(out_dir) if out_dir else DEFAULT_OUT
    out.mkdir(parents=True, exist_ok=True)
    intel = VideoIntel(target=target, out_dir=str(out), keep_raw=keep_raw)
    is_local = Path(target).suffix.lower() in VIDEO_EXT and os.path.exists(target)

    rc, info = _run(["yt-dlp", "--simulate", "--no-warnings", "--print",
                     "%(title)s\t%(duration)s\t%(uploader)s", target])
    if rc == 127:
        intel.note = "yt-dlp 未安装（pip install -U yt-dlp）"
        return intel
    if rc == 0 and info.strip():
        parts = info.strip().split("\t")
        intel.meta = {"title": parts[0] if len(parts) > 0 else "",
                      "duration": parts[1] if len(parts) > 1 else "",
                      "uploader": parts[2] if len(parts) > 2 else ""}

    # 1) 字幕优先：免费、不下载、信息密度最高。
    #    B 站实测常只有 danmaku（弹幕），弹幕虽不等于画面，但中文视频里信息密度很高，可用。
    # 两个开关同时开：有的站只有 UP 主上传字幕，有的只有平台自动生成的。
    # `danmaku` 是 B 站专属：实测多数 B 站视频没有正文字幕轨道，弹幕是唯一免费文本。
    _run(["yt-dlp", "--write-subs", "--write-auto-subs", "--skip-download",
          "--sub-langs", "zh.*,en.*,danmaku,ai-zh",
          "-o", str(out / "%(title)s.%(ext)s"), target], timeout=180)
    subs = sorted(out.glob("*.vtt")) + sorted(out.glob("*.srt")) + sorted(out.glob("*.xml"))
    if subs:
        try:
            intel.subtitle = subs[0].read_text(encoding="utf-8", errors="replace")[:200_000]
        except Exception:
            intel.subtitle = ""

    # 2) 没字幕才抽帧：这一步开始烧存储/算力，所以只在必要时做。
    if not intel.subtitle and frames > 0:
        local = target if is_local else str(out / "src.mp4")
        if not is_local:
            rc, _ = _run(["yt-dlp", "-f", "worst[ext=mp4]/worst", "-o", local, target], timeout=600)
            if rc != 0:
                intel.note = "下载失败（可能需要 cookie 或该站不支持）"
                return intel
        rc, _ = _run(["ffmpeg", "-y", "-i", local, "-vf",
                      f"select='gt(scene\\,0.4)',scale=640:-1", "-vsync", "vfr",
                      "-frames:v", str(frames), str(out / "f_%02d.jpg")], timeout=600)
        intel.frames = [str(f) for f in sorted(out.glob("f_*.jpg"))]
        if not keep_raw and not is_local:
            # 默认不开。留着这个分支是为了说明「我们不是不会删，是选择不删」。
            try:
                Path(local).unlink()
            except Exception:
                pass
    return intel


LIMITATIONS = {
    "web": ["未执行页面 JS：动态拼接 / 懒加载的接口抓不到",
            "无登录态：登录后才出现的接口不在范围内",
            "只看单页：SPA 的其他路由未遍历"],
    "bin": ["只扫可打印 ASCII：UTF-16 宽字符、压缩段、加密段未覆盖",
            "未反汇编：控制流与调用关系未知",
            "疑似凭据默认只计数不取值（`--secrets` 才给值），无法确认是否真凭据"],
    "video": ["弹幕/字幕不等于画面内容：镜头信息未覆盖",
              "未做 ASR：画面里的口播未转录",
              "抽帧是有损采样：两帧之间发生了什么不知道"],
}


def _evidence_of(obj, kind: str, target: str, limit: int = 12) -> list:
    """把提取结果打包成证据链。

    这一步是把 REA 那套搬进来的落点：以前返回的是一堆裸字符串，
    agent 拿到就当事实；现在每条都标了「从哪来的、有多硬、我没看到什么」。
    """
    ev = []

    def add(facet, claim, value, authority):
        ev.append(mk_evidence(facet, claim, value, authority, target,
                              LIMITATIONS.get(kind, [])).to_dict())

    if kind == "web":
        for e in obj.endpoints[:limit]:
            hit = bool(API_HINT_RE.search(e))
            add("endpoint", "页面里出现的路径" + ("（含 /api|/v1|graphql 特征，疑似接口）" if hit else ""),
                e, "analyst-inference" if hit else "shipped-artifact")
        for s in obj.inline_state[:6]:
            add("inline_state", "内联状态块（SSR/水合数据，常含完整数据形状）", s, "shipped-artifact")
        for s in obj.sourcemaps[:4]:
            add("sourcemap", "存在 sourcemap：可还原打包前源码", s, "shipped-artifact")
        for g in obj.graphql[:6]:
            add("graphql", "GraphQL 操作名", g, "shipped-artifact")
    elif kind == "bin":
        for u in obj.urls[:8]:
            add("url", "目标里硬编码的 URL", u, "shipped-artifact")
        for v in obj.versions[:8]:
            add("version", "版本号（可对照上游发布定位代码）", v, "historical-reference")
        for s in obj.symbols[:8]:
            add("symbol", "正则匹配到疑似函数名", s, "analyst-inference")
        for s in obj.strings[:6]:
            add("string", "可打印串", s, "shipped-artifact")
    elif kind == "video":
        if getattr(obj, "meta", None):
            add("meta", "视频元数据", obj.meta, "controlled-replay")
        if getattr(obj, "subtitle", ""):
            add("subtitle", f"字幕/弹幕文本，{len(obj.subtitle)} 字符",
                obj.subtitle[:200], "controlled-replay")
        for f in getattr(obj, "frames", [])[:4]:
            add("frame", "关键时刻抽帧", Path(f).name, "controlled-replay")
    return ev


def diff_versions(a, b, max_strings: int = 800, out_dir=None, **kw) -> dict:
    """两个版本做结构差分 —— 逆向里性价比最高的一招。

    思路来自 REA 的 `investigate-versions` 和 decomp.wtf 的「字节级验证」：
    **解不出一个东西是什么，往往能先说清它变了什么**。
    而 diff 出来的增删集合是实物比对（shipped-artifact），不是推断 —— 这是最硬的一类证据。
    """
    for t in (a, b):
        w = refuse_reason(t)
        if w:
            return {"kind": "refused", "target": t, "reason": w}
    try:
        ia = from_blob(a, max_strings=max_strings)
        ib = from_blob(b, max_strings=max_strings)
    except Exception as e:
        return {"kind": "error", "note": f"读取失败：{e}"}

    out = {"kind": "diff", "a": str(a), "b": str(b),
           "size_a": ia.size, "size_b": ib.size,
           "size_delta": ib.size - ia.size}
    for facet in ("urls", "versions", "symbols", "strings"):
        sa, sb = set(getattr(ia, facet)), set(getattr(ib, facet))
        out[f"{facet}_added"] = sorted(sb - sa)[:40]
        out[f"{facet}_removed"] = sorted(sa - sb)[:40]
    ev = []
    for facet in ("urls", "versions", "symbols"):
        for x in out[f"{facet}_added"][:5]:
            ev.append(mk_evidence(f"diff.{facet}", "新版本新增", x, "shipped-artifact",
                                  str(b), ["差分基于字符串集合，重排/重命名会被判为增删"]).to_dict())
        for x in out[f"{facet}_removed"][:5]:
            ev.append(mk_evidence(f"diff.{facet}", "新版本移除", x, "shipped-artifact",
                                  str(b), ["差分基于字符串集合，重排/重命名会被判为增删"]).to_dict())
    out["evidence"] = ev
    out["note"] = ("只看增删，不看语义：同一个功能换了个函数名会同时出现在两侧。"
                   "想确认因果还得回到代码 —— diff 是定位器，不是结论。")
    return out


# ---------------------------------------------------------------- 统一入口
def scan(target: str, kind: str = "auto", out_dir=None,
         intent: str = "overview", use_snapshot: bool = True, **kw) -> dict:
    """先路由，再提取，结论带证据。返回 dict（便于直接 json 落盘 / 打印）。

    与上一版的区别：`kind="auto"` 不再只看扩展名（.dat 可能是 SQLite），
    改走 `target_kind()` 的 magic bytes；返回里多了 `route` 与 `evidence`。
    """
    why = refuse_reason(target)
    if why:
        return {"kind": "refused", "target": target, "reason": why,
                "note": "硬清单命中。这是可改的规则，不是硬编码的禁止 —— 要开就说一声。"}

    k = kind
    if k == "auto":
        tk = target_kind(target)
        if tk == "unknown":
            return {"kind": "unknown", "target": target, "note": "判不出类型，用 --kind 指定"}
        # 目前只有三档提取器；其余类型（pe/apk/pcap…）先按 bin 做表面扫描，
        # 路由层会另行说明「深挖需要哪些工具」。
        k = {"web": "web", "html": "web", "video": "video"}.get(tk, "bin")

    rt = route(target, intent, out_dir)

    # 快照：同一目标同一组参数只扫一次（学 REA 的 --snapshot）
    if use_snapshot:
        key = snapshot_key(target, k, intent=intent, **kw)
        hit = snapshot_get(out_dir, key)
        if hit:
            hit["from_snapshot"] = True
            hit["route"] = rt
            return hit
        snap_key = key
    else:
        snap_key = ""

    if k == "web":
        fr = net.fetch_text(target, timeout=kw.get("timeout", 15.0))
        if not getattr(fr, "text", ""):
            return {"kind": "web", "target": target, "note": "取不到页面", "route": rt}
        intel = from_html(fr.text, target, limit=kw.get("limit", 60),
                          expose_creds=kw.get("expose_creds", EXPOSE_CRED_VALUES))
        res = intel.summary() | {"endpoints": intel.endpoints[:20],
                                 "inline_state": intel.inline_state,
                                 "sourcemaps": intel.sourcemaps[:5],
                                 "graphql": intel.graphql[:10]}
    elif k == "bin":
        intel = from_blob(target, max_strings=kw.get("max_strings", 400),
                          expose_creds=kw.get("expose_creds", EXPOSE_CRED_VALUES))
        res = intel.summary() | {"urls": intel.urls[:20], "versions": intel.versions[:15],
                                 "symbols": intel.symbols[:20],
                                 "strings": intel.strings[:40]}
    elif k == "video":
        intel = from_video(target, out_dir, frames=kw.get("frames", 6),
                           keep_raw=kw.get("keep_raw", True))
        res = intel.summary() | {"subtitle_head": intel.subtitle[:800]}
    else:
        return {"kind": "unknown", "target": target, "note": f"未支持的类型 {k}", "route": rt}

    res["route"] = rt
    res["evidence"] = _evidence_of(intel, k, target, limit=kw.get("ev_limit", 12))
    res["authority_legend"] = {a: d for a, d in AUTHORITY.items()
                               if a in {e["authority"] for e in res["evidence"]}}
    costs = cost_tags(target)
    if costs:
        # 不拦，只报代价：工具该给的是信息，不是准许。
        res["cost"] = costs
        res["cost_note"] = "以上为代价标注，不是拦截 —— 决定权在使用者。"
    if getattr(intel, "cred_values", None):
        res["cred_values"] = intel.cred_values
    if snap_key:
        res["snapshot"] = snapshot_put(out_dir, snap_key, res)
    return res


# ---------------------------------------------------------------- CLI
def _print(r: dict, show: int = 12) -> None:
    for k, v in r.items():
        if isinstance(v, list):
            if v and isinstance(v[0], dict):        # evidence / cost 等 dict 列表
                print(f"{k:14s} ({len(v)})")
                for e in v[:show]:
                    if "authority" in e:             # evidence
                        print(f"    [{e.get('authority','-')}] {e.get('facet','')}: "
                              f"{str(e.get('value',''))[:70]}")
                    else:                            # cost 标注：把代价说清楚
                        head = e.get("tag") or e.get("matched") or "?"
                        body = e.get("cost") or e.get("rule") or str(e)
                        print(f"    ⚠ {head}: {body}")
                        if e.get("rule"):
                            print(f"       → {e['rule']}")
            else:
                print(f"{k:14s} ({len(v)})")
                for x in v[:show]:
                    print(f"    {x}")
        elif isinstance(v, dict):
            print(f"{k:14s} {json.dumps(v, ensure_ascii=False)[:120]}")
        else:
            print(f"{k:14s} {v}")


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="天网 · 逆向层：拿到的东西里还藏着什么（先路由，后动手，结论带证据）")
    ap.add_argument("target", nargs="?", help="URL / 文件路径 / 视频链接")
    ap.add_argument("--kind", default="auto", choices=["auto", "web", "bin", "video"])
    ap.add_argument("--intent", default="overview",
                    help="意图轴：overview/endpoints/strings/symbols/diff/timeline/flow/source")
    ap.add_argument("--out", default=None, help="产物目录（默认 reverse_out/）")
    ap.add_argument("--frames", type=int, default=6)
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--max-strings", type=int, default=400)
    ap.add_argument("--show", type=int, default=12)
    ap.add_argument("--route-only", action="store_true", help="只路由不干活（看该走哪条路）")
    ap.add_argument("--secrets", action="store_true",
                    help="疑似凭据**给值**（默认只报几处、什么类型 —— 默认档是安全档，不是禁令）")
    ap.add_argument("--tools", action="store_true", help="打印本机工具台账并退出")
    ap.add_argument("--register", default=None,
                    help="登记 PATH 外的工具：cmd=完整路径，如 ghidra=D:/tools/ghidra/analyzeHeadless.bat")
    ap.add_argument("--diff", default=None, help="与 target 做结构差分：传另一个版本路径")
    ap.add_argument("--no-snapshot", action="store_true", help="不用结果缓存，重跑一遍")
    ap.add_argument("--journal", default=None,
                    help="写一条经验：格式 '触发类别|场景|摘要'（类别见 JOURNAL_TRIGGERS）")
    ap.add_argument("--journal-find", default=None, help="先查经验索引再动手：传关键词")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.register:
        if "=" not in a.register:
            print("格式：`--register 命令=完整路径`")
            return 2
        cmd, path = a.register.split("=", 1)
        r = register_tool(a.out, cmd.strip(), path.strip())
        print(json.dumps(r, ensure_ascii=False, indent=2) if a.json else r)
        return 0 if r.get("ok") else 2

    if a.tools:
        idx = tool_index(a.out, refresh=True)
        if a.json:
            print(json.dumps(idx, ensure_ascii=False, indent=2))
        else:
            print(f"生成于 {idx['generated']}")
            for c, m in idx["have"].items():
                tag = "手工登记" if m.get("source") == "manual" else m["version"][:28]
                print(f"  ✅ {c:12s} {tag:28s} {m['capability']}")
            for m in idx["missing"]:
                print(f"  ❌ {m['cmd']:12s} 未安装 —— {m['capability']}")
            if idx["missing"]:
                print("  提示：装了但不在 PATH 的，用 --register 命令=路径 登记，机器不替人猜路径")
        return 0

    if a.journal_find:
        hits = journal_search(a.out, a.journal_find)
        print(f"索引命中 {len(hits)} 条：")
        for h in hits:
            print("   ", h)
        return 0

    if a.journal:
        parts = [x.strip() for x in a.journal.split("|")]
        if len(parts) < 3:
            print("格式：`触发类别|场景|摘要`，例如：发现新工具坑位|yt-dlp|弹幕要显式加 danmaku")
            return 2
        r = journal_write(a.out, parts[0], parts[1], parts[2], target=a.target or "")
        print(json.dumps(r, ensure_ascii=False, indent=2) if a.json else r)
        return 0 if r.get("ok") else 2

    if not a.target:
        ap.error("缺少 target（--tools / --journal-find 除外）")

    if a.diff:
        r = diff_versions(a.target, a.diff, out_dir=a.out)
    elif a.route_only:
        r = route(a.target, a.intent, a.out)
        r["tools_on_box"] = sorted(tool_index(a.out).get("have", {}))
    else:
        r = scan(a.target, a.kind, a.out, intent=a.intent,
                 use_snapshot=not a.no_snapshot, frames=a.frames,
                 limit=a.limit, max_strings=a.max_strings,
                 expose_creds=a.secrets)

    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        _print(r, a.show)
    return 0 if r.get("kind") != "refused" else 2


if __name__ == "__main__":
    sys.exit(main())
