# netforce

自建索引的发现层：从已知锚点出发，把"网上有哪些资源站 / 哪些子站还活着 / 哪些页面能产出语料"盘成可维护的本地索引。

不是通用爬虫框架，也不追求抓得多。它只解决一件事：**在不依赖别人搜索 API 的前提下，自己维护一份能用、能溯源、能增量更新的索引。**

## 三种模式

| 模式 | 命令 | 干什么 |
|---|---|---|
| 资源发现（默认） | `python resource_finder.py` | 从 awesome-list / 大学 / 软件库等种子递归扩散，验证可达后登记资源站，产出 `resources.json` |
| 角色语料 | `python resource_finder.py corpus blhx` | 抓游戏 wiki 的角色台词表，按情境键（`data-key`）切成"情境 → 台词"样本 |
| 站群扩散 | `python resource_finder.py diffuse` | 把一个站群（如 `wiki.biligame.com` 下 1,199 个子站）逐个判活，抽出角色/语音/剧情/档案四类目录页 |

三种模式共用一个文件、一套去重账本与自适应限速，通过子命令区分——刻意不拆成三个脚本。

## 采集内核 `skynet/`（搜索 · 下载 · 检测 · 记录 + 路由/结构/DNS）

`resource_finder.py` 是"跑起来的那条产线"；`skynet/` 是它下面可复用的内核，也是后续所有产线（语料、模型、数据）的公共供料口。老代码不删，新逻辑在新层里长。

| 层 | 模块 | 解决什么 |
|---|---|---|
| 搜索层 | `skynet/search.py` | 去哪儿找：多引擎聚合 + **意图校验** + 引擎健康度自动淘汰 |
| 下载层 | `skynet/net.py` | 怎么拿：**会话复用** + 域令牌桶（带抖动）+ **域并发闸门** + **通道熔断** + **出口健康度** + **真 Range 续传** + **内容指纹去重** + **GitHub raw 镜像降级** |
| 检测层 | `skynet/probe.py` | 值不值得：可达 / 实质 / 密度 / **解析校验** 四级判定 |
| 记录层 | `skynet/store.py` | 记过什么：去重 + 域画像 + **失败重试队列** + **内容指纹账** |
| 路由层 | `skynet/route.py` | 从哪儿出去：跳转链穿透 / 多级代理链（节点中转已设计，暂缓） |
| 结构层 | `skynet/discover.py` | 站里有什么：robots + sitemapindex 递归 + feed |
| DNS 层 | `skynet/dns.py` | 解析对不对：DoH 解析 + 缓存 + **污染判据**，喂给 `curl --resolve` |
| 逆向层 | `skynet/reverse.py` | 藏着什么：端点 / 字符串 / 时间轴 —— **只提取知识，不做绕过**（见下） |

```bash
python skynet.py search "game voice lines dataset" --engines ddg,searx,github
python skynet.py search "语音" --wiki-tag sr          # 站内搜索，找页面最准
python skynet.py probe https://wiki.biligame.com/blhx/天狼星
python skynet.py probe-file urls.txt --workers 5
python skynet.py hosts        # 域画像排行（哪些站长期优质）
python skynet.py structure https://opengameasset.net/   # robots + sitemap
python skynet.py netstat --url http://github.com        # 下载层实况 + 连通性自检
python skynet.py dedupe G:/_train_corpus_raw/audio      # 内容指纹查重（默认只报告）
python skynet.py retry --kind audio --limit 200         # 补抓失败项（带续传）
python skynet.py stats        # 总览
python skynet.py reverse https://wiki.biligame.com/blhx/天狼星   # 抽端点 / 内联状态 / sourcemap
python skynet.py reverse ./some.bin --kind bin       # 字符串 / URL / 版本号 / 路径骨架
python skynet.py reverse "https://b23.tv/xxxx" --kind video --frames 6
python skynet.py reverse ./app.exe --route-only      # 只说该走哪条路，不干活
python skynet.py reverse --tools                     # 本机工具台账（不许猜路径）
python skynet.py reverse old.bin --diff new.bin      # 两版本结构差分（找变更最快的一招）
python skynet.py reverse --journal-find yt-dlp       # 先查经验索引再动手
```

**下载层的六道闸门**（都不需要手工调，自动生效）：

| 闸门 | 作用 | 为什么需要 |
|---|---|---|
| 会话复用 | 按 (线程, 域) 缓存连接，同域不再重复握手 | 批量抓单站时数万次请求，握手占大头。实测同一页 7s → **0.32s** |
| 域令牌桶 | 同域两次请求至少隔 `gap` 秒，带 0.85~1.3 抖动 | 别把站点打爆；严格等间隔在网络侧是标准方波，更好识别 |
| 域并发闸门 | 同域在途请求 ≤ `NETFORCE_HOST_CONC` | 只限频率不限并发时，worker 会在间隔到点瞬间一起冲 |
| 通道熔断 | 某通道连续失败 3 次本轮跳过 | 依赖没装/代理挂了时，别每个请求都白等一次超时 |
| 出口健康度 | 出口连续 3 次风控则冷却 60s，自动切备用 | 单点出口被封 = 全线停；冷却到期自动恢复，不永久拉黑 |
| 指纹粘性 | 同域固定一套 UA/语言/sec-ch-ua，不逐请求换 | **逐请求换 UA 比固定 UA 更像机器人**——真人不会每点一次就换设备 |

**第七道：换域名（`gh_mirror_urls`）**

前面六道都在调「怎么发这个请求」，这道调的是「发给谁」。

`raw.githubusercontent.com` 在国内经常是**整段连接重置**（`status=0`，不是 404）。这种失败
换 UA、换通道、换出口全部无效——因为断在 TCP/TLS，请求根本没出去。而种子表里 24 条
awesome-list 全指向它，一旦被墙，表面现象是「24 个种子全部失效」，看着像链接过期，
实际是域名不通，很容易把排查方向带偏。

所以 raw 地址会被展开成三条等价路径：

```
raw.githubusercontent.com/{user}/{repo}/{ref}/{path}        ← 原站（走本机代理，通常最快）
cdn.jsdelivr.net/gh/{user}/{repo}@{ref}/{path}             ← 第一镜像
cdn.statically.io/gh/{user}/{repo}/{ref}/{path}            ← 第二镜像
```

原站整段不通时自动换下一档，换成功的结果仍以**原始 URL** 回报，调用方不需要知道镜像存在。
一旦某个 raw 地址是靠镜像救回来的，进程内会记下「原站已不通」，后续 raw 请求直接先打镜像，
省掉每次都先等一次连接重置的时间。

只用纯 CDN，**不套 gh-proxy 这类反向代理**：本机已有代理，再套一层只是双重中转，更慢；
反代自身也不稳定。代价是 CDN 有缓存（jsdelivr 约 12h），刚 push 的内容可能滞后，所以镜像
只做兜底，绝不替代原站。

**检测层的四级判定**（`probe.py`）：L1 可达 → L2 实质内容（体积/文本密度，壳页在这死）→ L3 目标密度（台词行/表格行/音频直链）→ L4 解析校验（`verify()`：页面活着但解析器失配会静默零产出，这是最隐蔽的失效）。

**搜索层的意图校验**（`relevance()`）：查询 token 在「标题(权重2) + 解码URL(权重1)」里的覆盖率，低于 0.18 直接丢。中文按 2-gram，不依赖分词。

## 逆向层：藏着什么（`skynet/reverse.py`）

前七层回答「去哪儿找 / 怎么拿 / 值不值得 / 记过什么 / 从哪儿出去 / 站里有什么 / 解析对不对」。
第八层回答最后一件：**拿到手的东西里，还藏着什么**。

> 「不解密、不破授权、不绕付费墙、不撞登录态 —— 这条**视情况而定**，
>   因为一般情况下大部分知识根本不需要去这样搞。」
> 「不为盈利是真的 —— 真的没有人找我盈利，我也赚不到钱。」
> 「与其说是无道德，不如说是**真正的工具**：想到什么就去得到什么。」
> ⇒ **想要什么就去得到什么，去提取什么。**（2026-10-23 二次定调）

### 边界：不判对错，只报代价

原来那张 `REFUSE`（命中即拒）**作废了**。为了极少数情况立一堆死规矩，等于给工具自己上锁。
现在的做法是：**命中不拦，只在结果里把代价说清楚**，让用的人自己决定值不值。

| 标注 | 代价 | 处置 |
|---|---|---|
| 撞登录态 / 付费墙 | 会撞对方风控：IP/账号封禁、风控升级、以后更难拿 | 先算「封了亏多少」—— 亏得起就干，**不是不许干** |
| 主机 / 商用固件 | 法律风险显著高于其他目标，且厂商取证积极 | 代价是法律不是技术 —— 知会一声，代码不拦 |
| 凭据 / 身份材料 | 一旦落盘就进产物/快照/同步链路，不可逆 | 默认只报类型不给值，`--secrets` 才给值 |

- `HARD_REFUSE` **默认是空表** —— 边界改成标注了，默认一路放行。
- 唯一真硬的一条：**不为营利**。它不是道德，是**风险模型的前提** —— 「不赚钱」
  正是「没人来找你」的充要条件，破了这条整套模型失效。而它形不成目标字符串匹配
  （它是「你拿结果去干嘛」，发生在代码之外），所以留作宣言，不进代码。
- 云 AI 有风控是事实。选择是：**在最大范围内执行，撞了再说，不为风控预缴税。**

这套语言跟项目既有硬约束本来就是同源的 —— 那些约束从来不是道德，是成本核算：
他的账号只读、量全走 bot（曾因一天上万条被封）；高价值账号不得用于低价值批量；
后台脚本不许弹窗（不是不许起，是代价是打扰）。

### 三档提取器（全部纯命令行，不碰浏览器）

| 档 | 抽什么 | 实测 |
|---|---|---|
| `web` | API 端点、内联状态块（`__NEXT_DATA__` 等）、sourcemap、GraphQL 操作名 | 抽到 `api.biligame.com/user/info` —— 页面上看不见、但接口才是数据真形状 |
| `bin` | 可打印串、URL、版本号、Win/POSIX 路径、符号名 | 扫自身 17KB → 40 串 / 2 版本 / 20 符号 |
| `video` | 字幕 → 弹幕 → 关键时刻抽帧（yt-dlp + ffmpeg） | B 站实测：**正文轨道 0，弹幕 106,148 字符、每条带秒级时间戳** |

`bin` 档刻意不接 Ghidra / radare2：本层是「先读一遍字符串，看看值不值得交给专业工具」，
不是替代它们。真要深挖再上 Ghidra（本机已装）。

### 一处刻意反着来的设计

对照 `bilibili-ai-bot` 的 `download → 抽 3 帧 → delete_video`：那是**压存储层**，
与我们「记忆/原文不压缩」的定案相反。

所以本层 `keep_raw=True` 默认开 —— 抽帧和转录只是**加载层**的有损裁剪，**原片留在盘上不动**。
代码里留着 `keep_raw=False` 的分支，是为了说明「我们不是不会删，是选择不删」。

### 2026-10-23 融合：从开源逆向项目里提取的知识

搜了一圈开源逆向项目，挑了三个有真东西的，把**最硬的那条规矩**搬过来（只取知识，不搬代码）：

| 来源 | 它是什么 | 我们搬了什么 | 落点 |
|---|---|---|---|
| `morluto/rea`（本周 GitHub 第一，周增 11.6 万星 · MIT） | 不造引擎，只把 Ghidra / IDA / Hopper 编排给 AI agent | **结论必须带 Evidence：SHA-256、authority 分级、limitations 显式列出未知项**；`--snapshot` 缓存；版本差分 | `Evidence` / `AUTHORITY` / `_evidence_of()` / `snapshot_*` / `diff_versions()` |
| `zhaoxuya520/reverse-skill`（3.8 万星 · MIT） | 不是工具包，是**路由引擎**：44 条规则三轴消歧 | 目标类型 × 意图 × 工具链路由、**未命中不硬塞**、工具台账不许猜路径、经验五类触发写回 | `route()` / `tool_index()` / `journal_*()` |
| `MG1937/ASC` + `i12bp8/decomp.wtf` | 给 agent 用的按需反编译器 / 重建后编译回原字节验证 | **按需抽取不预索引**（别一次 dump 全集）；**差分即验证**（解不出是什么，至少能说清变了什么） | `limit` 收紧 / `diff_versions()` |

**证据分级 `authority`**（这是本轮最值钱的一格）：

| 级别 | 含义 | 例子 |
|---|---|---|
| `shipped-artifact` | 目标实物里直接读到的 —— 最硬，可复核 | 页面里确实存在这个字符串 |
| `controlled-replay` | 我们自己跑一遍观察到的 —— 可复现，依赖当时环境 | yt-dlp 抓来的弹幕、ffmpeg 抽的帧 |
| `historical-reference` | 外部已知信息 —— 需复核，可能过时 | 版本号对应上游某次发布 |
| `analyst-inference` | 我们推断的 —— **只当线索不当结论** | 「这个路径看着像个 API 接口」 |

分级的意义在实测里很直白：扫 wiki 页面时 `api.biligame.com/user/info` 被标成 `analyst-inference`
（值确实在页面上，但「它是接口」是我们的推断），而 CSS 图片链接是 `shipped-artifact`。
**逆向最容易出的错不是「解不出来」，是「解不出来但编了一个，还写进了报告」。**

**三轴路由**（`--route-only` 只看路不干活）：

```
python skynet.py reverse G:/Python312/python.exe --route-only
  route          native-deep
  tools_usable   ['objdump']        tools_missing  ['r2', 'rizin', 'ghidra']
  note           缺 ['r2','rizin','ghidra']：降级为只读表面扫描（先扫字符串看看值不值得装）
```

轴 1 目标类型走 `sniff()` 的 magic bytes（不看扩展名 —— `.dat` 可能是 SQLite），
轴 2 是用户意图（问「端点」和问「符号」走不同的路），轴 3 是本机有什么工具。
**路由未命中就提议新技能，不硬塞一个相近流程** —— 那会产生一堆带证据的错结论。

**工具台账**（`--tools`）：本机实测有 `objdump / ffmpeg / yt-dlp`；`r2 / ghidra / jadx / apktool`
没装。装了但不在 PATH 的用 `--register ghidra=D:/某路径/analyzeHeadless.bat` 人工登记 ——
**机器不替人猜路径**。

**能力边界**（36 氪对 REA 的 51 次公开记录统计，写在这儿免得自己骗自己）：
29 达成 / 16 部分 / 6 失败；扣掉作者自己做的 9 件，42 件独立记录里只有 20 件成功，
其中 9 件是小程序、6 件是中型应用的单一功能。失效点：强虚拟化壳（VMP）、缺动态调试、
服务器端逻辑够不着、输出吃上下文、复杂目标卡顿。
**在已有积累的领域是加速器，在完全陌生的领域只是探索工具。**

## 运行

```bash
pip install curl_cffi          # 可选：装了自动升到 Chrome TLS 指纹；不装用 requests 复用
python resource_finder.py                      # 资源发现，无限跑
python resource_finder.py corpus blhx --until 06:00
python resource_finder.py diffuse --limit 20   # 先小样本试跑
```

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `NETFORCE_PROXY` | `http://127.0.0.1:3067` | 出网代理；置空字符串 = 全程直连 |
| `NETFORCE_PROXIES` | 同 `NETFORCE_PROXY` | 多出口，逗号分隔。**同域按 hash 粘一个出口**，不逐请求轮换（IP 瞬移比固定 IP 更像机器人） |
| `NETFORCE_HOST_CONC` | `3` | 每域最大在途请求数 |
| `NETFORCE_DATA` | `G:/_train_corpus_raw` | 语料/状态/日志落盘目录 |

其余路径（`resources.json`、`_visited.db`）都在脚本同目录，跟着脚本走。

## 设计要点

- **去重账本落 SQLite**（`_visited.db`），主键即规范化指纹，重启不重爬——等价于 Scrapy 的 dupefilter + JOBDIR。
- **两相轮转调度**：种子泵注入目录页 ↔ 队列消化目录页。队列到阈值只**暂停注入**，不停止爬取。
- **泵的加权轮转**：按命中率自适应降权，但保留最低出现概率，防止冷门但独活的源被饿死。
- **自适应限速（AutoThrottle）**：成功向实测耗时收敛；被风控只准调慢，不准调快。
- **站群指纹**：站群型域名（同域多子站）按「域名/路径首段」判重，而不是按域名——否则 1,199 个子站会被当成一个站。

## 踩过的坑（这几条比代码值钱）

1. **国内 wiki 套代理必挂。** 同一页三种走法：走代理 → HTTP 567 / 7.3KB 空壳；直连但不带 `Referer` → 同样 567；**直连 + 浏览器 UA + `Referer` + `Accept-Language`** → 200 / 1.41MB 全量。所以站群取页不复用发现模式的取页链。
2. **必须 `curl -L`。** 子站根路径 `/<sub>/` 是 301 → `/<sub>/首页`，不跟跳转拿到 0 字节，会被误判成"风控"。
3. **空壳要判死。** 不存在的子站会 301 回主站首页（1.41MB，标题是主站名），体积够大、长得像活站 ⇒ 判 `dead`，否则清单里一大半是把主站记成活站。
4. **后台跑不许弹窗。** Windows 上 `pythonw` 父进程没有控制台，`subprocess` 起 `curl` 会**每次新建一个黑框**。三件套缺一不可：`STARTUPINFO` + `dwFlags |= STARTF_USESHOWWINDOW` + `SW_HIDE`、`creationflags=CREATE_NO_WINDOW`、`stdin=DEVNULL`；且每次都要新建 `STARTUPINFO` 实例（复用第二次失效）。见 `_hidden_startupinfo()`。
5. **队列判重别和入队共用一本账。** 入队时就写 `visited`、出队时又用它判重 ⇒ 每个种子一 `pop` 就撞自己的记录被跳过，页面永远不被抓。队列页要另记一本账。
6. **搜索端点会被污染，必须校验意图。** 实测 Bing 的 RSS 端：查「碧蓝航线 语音 台词 wiki」返回**香港清水湾楼盘**，查「open source game voice lines dataset」返回**剑桥词典的 open 词条**——它按第一个词 + 地区偏好返回，与查询意图无关。只做「抽链接 + 去垃圾域」会把这类垃圾全收进来。解法是 `skynet/search.py::relevance()`：查询 token 在结果标题与 URL 里的覆盖率，不过关直接丢；同时给每个引擎记健康度，长期 0 相关的自动降权（Bing 实测 10 提交 0 存活，权重自动归零）。
7. **子进程 `curl` 通道必须校验状态码，否则会把风控页当正文。** 只看 `returncode == 0` + `stdout` 非空时，WAF 返回的 567 拦截页（7.3KB）被当成「抓取成功」，下游拿到一坨垃圾还以为数据是好的。现在改成 `-o 临时文件 + -w %{http_code}`，只有 `2xx` 才收。**凡是「跑通了」的通道，都要能说出它拿到的状态码是多少。**
8. **代码分支写著 ≠ 已生效，依赖不在等于没有。** 三级通道里的 `curl_cffi` 分支写了很久，实际这个 venv 里根本没装 `curl_cffi`，一直在静默走 urllib。现在会探测后端（curl_cffi > requests > urllib）并把结果打在 `netstat` 里，通道级还有熔断：连续失败 3 次本轮跳过，不再每个请求白等一次超时。
9. **单次抓取必须有总预算。** 最坏耗时 = 通道数 × 重试 × 单次超时，4×2×15 = 120s，一条死链就能拖死整批。加 `budget` 后全引擎一轮从 55.2s 降到 12.9s。
10. **进程数翻倍不等于重复启动。** Windows 下 venv 的 `python.exe` 是 launcher，会再起一个真实解释器，形成 `bash → python(launcher) → python(real)` 的父子链。看到 2N 个进程先查 `ParentProcessId`，别当成"跑了两套"直接杀——会白中断一次任务。
11. **CDN 站的 DNS 差异不是污染。** 判污染的判据必须是「系统解析与 DoH **完全没有交集**」，不能是「系统多出 DoH 没有的 IP」——`wiki.biligame.com` 这类站有十几个 IP，两个解析器按线路给出不同子集完全正常，按后者判会把所有 CDN 站误报。

## 2026-11-04 升级：出网纪律 / 完整解析 / 直接细分类 / 常驻可靠性

### 出网（唯一纪律）

- **默认纯直连**：委托天网 `fleet/net.py` 的 `ProxyHandler({})` 真直连层，也就是上个会话打通的「直连 GitHub / 直连国外」那扇门。
- **不碰主人本地端口**：不读系统代理、不扫端口、不自动启用 7890 / 3067 / karing。`netprobe.py` 探测到的本机出口**只报告、不启用**。
- **代理能力一直在、默认关**：`_netpolicy.json` 支持热改（下一跳生效，不用重启）；`python netprobe.py --set-proxy URL` 是**唯一**启用入口，`--set-proxy ''` 撤销。一旦指定就**固定使用，不自动切换、不回退**；`self_kernel=true` 可用天网自有 7891 内核（项目自己的，不是主人端口）。
- 修掉的实病：`classify.py` / `jsprobe.py` / `probe_sites.py` 旧版硬编码 7890；代理没开时静态分类每条先撞死端口、无头补抓 100% 全灭。`classified.json` 曾有 38608/41320 条（93.4%）堆在「JS渲染/无静态内容」。

### 新增模块

| 文件 | 职责 |
|---|---|
| `netpolicy.py` | 统一出网、热加载配置、固定出口、浏览器通道参数 |
| `netprobe.py` | 直连能力实测矩阵 + 只读出口诊断 + 显式指定入口 |
| `browser_pick.py` | 浏览器启动实测择优（系统 Chrome 会自退 ⇒ 自动落到 Edge / Playwright 自带） |
| `render_parse.py` | 无头完整解析：DOM/正文/OG/JSON-LD/链接/network 响应/懒加载滚动，解析即打标签 |
| `fine_classify.py` | 多维细标签：资源形态 / 主题 / 可获取性 / 语种 / 质量分 / 价值 |
| `selfproxy.py` | 可选统一本地出口（默认不用，仅供特殊场景） |
| `watchdog.py` | 多目标守护：主爬虫 + 闭环流水线 `pipeline.py`；卡死重拉、代码热重启 |

### 数据与闭环

- 记录 `_kb/records/*.jsonl`（分片 append-only），断点账本 `_kb/state.db`（SQLite，重启不重解析）。
- 新域名/资源写 `_kb/discovered.jsonl` → 主爬虫启动与队列空时回灌，形成「发现 → 渲染解析 → 再发现」闭环。
- 失败账本 `_kb/direct_fail.txt`：直连不通的域下一轮排后 + 短超时，不白等。
- 渲染器 `--loop` 每轮查代码 mtime，变了就做完当前批次优雅退出，由 watchdog 用新代码拉起；进度在 SQLite，不丢不重。

### 首轮实测（8 域 / 40 s / 纯直连）

5 个渲染成功拿到标题；59 个新域名、43 个 JS 触发资源、7 个 XHR 端点；3 条高价值；通道 8/8 全 `direct`，零代理。系统 Chrome 启动自退被自动绕过，实际用 Playwright 自带 chromium。

## 2026-11-04 第二轮：闭环 / 资源落盘 / 知识库可检索

**结论：从「爬到了」变成「查得到、拿得到、别的 AI 也能直接用」。** 出网纪律不变，仍是默认纯直连。

### 统一入口（不用记六个脚本名）

```bash
python nf.py status                              # 四层状态一屏看完
python nf.py run --rounds 1 --render-limit 8     # 跑一轮完整闭环
python nf.py run --loop                          # 长期自主闭环（前台）
python nf.py search 数据集 --min-quality 40 --context
python nf.py download --limit 30                 # 只跑资源续传
python nf.py index --rebuild                     # 重建知识库索引
python nf.py probe                               # 只读出网诊断（不写配置）
python nf.py set-proxy http://...                # 远程显式指定固定出口
python nf.py set-proxy ""                        # 撤销，回纯直连
python -m py_compile *.py                        # 改动后自检
```

`render_parse.py --loop` 仍可单独跑，但它与 `pipeline.py` 共用同一把单例锁，**不要同时开**。

### 新增模块

| 文件 | 职责 |
|---|---|
| `pipeline.py` | 一轮串完 发现 → 无头渲染 → 解析即细分类 → FTS5 索引 → 资源续传落盘；`--loop` 常驻；代码变更做完当前批次再优雅热重启 |
| `download_assets.py` | 资源下载三层：aria2c `-c -x8 -s8`（.aria2 断点）→ 自研 Range 分段（每段独立落盘、断点可接、拼完校验总大小）→ curl `-C -` 兜底；附 sha256、SQLite 断点、大小上限、价值过滤 |
| `kb_index.py` | SQLite FTS5 全文索引；中文逐字切分解决 unicode61 的中文分词问题；按形态/主题/语种/价值/质量分过滤；`--context` 输出可直接喂模型；增量灌入；分类规则变更自动重打历史标签；导出紧凑 jsonl |
| `nf.py` | 统一命令入口，全部前台执行 |
| `netforce_mcp.py` | 新增 `netforce_kb_search` / `netforce_kb_stats` / `netforce_kb_assets` 三个 MCP 工具，让别的 AI 直接查本地知识库和已落盘文件 |
| `_crtest/test_download.py` | 续传 / 分段拼接 / 大小校验的改动相关回归测试（本地 Range 服务器，不依赖外网） |

### 修掉的四个真问题

1. **闭环断口**：渲染器此前压根不消费自己写进 `discovered.jsonl` 的新域名。现在发现队列里的域名**优先解析**，主爬虫存量次之；失败域不再永久跳过，超过 6 小时按短超时有限重试并排在最后。
2. **发现队列膨胀**：同一域名/图标每次重解析都重写一遍。`state.db` 新增 `fed` 幂等表，且回灌时先用下载层的价值过滤。实测本轮 21 个新域名 + 17 个资源只新增 12 行。
3. **分类全是噪声**：`fine_classify` 原用裸子串匹配，`ai` 撞 `main`、`tor` 撞 `repository`，连 example.com 都被标成「AI/机器学习 / 隐私/匿名」。改成英文词边界 + 中文子串，域名信号与正文信号分开；图标/样式扩展名不再参与形态判定和质量分。
4. **JS 资源全是图标**：首轮 43 个 asset 里没有一个真资料，全是 logo / 导航图 / status.json。下载层按扩展名白名单 + 路径噪声过滤，并从页面 `links` 补捞真正的 pdf/csv/json/xml —— 冷门资料多藏在链接里，不在 network 面板里。

原始记录仍然保留完整 network 观测数据（**原文不删不缩**），价值过滤只发生在消费端。

### 数据落点

| 路径 | 内容 |
|---|---|
| `_kb/records/*.jsonl` | 完整解析记录，分片 append-only |
| `_kb/state.db` | `done` 断点 / `fed` 发现去重 / `direct_fail` 相关账本 |
| `_kb/kb_index.db` | `docs` + `docs_fts` 全文索引 + `ingested` 增量进度 + `meta` 规则签名 |
| `_kb/assets.db` | 下载队列与断点状态（含 `scanned` 增量扫描进度） |
| `_kb/assets/<host>/` | 成品文件；`parts/` 是永不自动删的断点半成品 |
| `_kb/assets/manifest.jsonl` | 落盘清单：URL / 本地路径 / 大小 / sha256 |
| `_kb/kb_export.jsonl` | 紧凑导出，给本地模型或其他 AI 直接读 |

### 出网纪律在下载层同样生效

`aria2c` / `curl` 默认会继承环境变量里的 `*_PROXY`，那等于偷偷用上主人本地端口。`download_assets.py` 每次调用前清空这些变量；出口取 `netpolicy.channels(host)` 的固定值，默认空 = 纯直连，显式指定才传 `--all-proxy` / `-x`，**不因下载失败切换通道**。

### 第二轮实测（一轮 6 域 / 87.8 s / 全 `direct`）

- 渲染成功 3/6，拿到 21 个新域名、17 个 JS 资源、11 个 XHR 端点；如实记 3 个不可达，未伪装成功。
- 索引从 8 条增至 14 条；4 个真实资源落盘共 0.39 MB（百科热词 / 历史事件 JSON、MDN RSS XML），`--verify` 复核 0 个损坏。
- 续传回归测试 6/6 通过；MCP 三个新工具经 stdio JSON-RPC 实测可用；16 个文件全量 `py_compile` 通过。
- 全程零代理、零端口扫描；未重启主爬虫、未启动常驻守护、未推远程。

## 2026-11-04 第三轮：DoH 真实 IP 直连增强（不夸大，如实记录）

**结论：找到了「直连国外」那扇门的真身，但它是间歇开的；系统现在会自动去试，通了就用、不通就如实记失败，绝不偷偷切到主人本地端口。**

### 实测（宿主机真实网络域，未开任何代理）

| 通道 | github.com | raw.githubusercontent.com | pypi / npm / cloudflare | google / HF / wikipedia / telegram |
|---|---|---|---|---|
| 普通直连 `fleet.net` | 不通 | 间歇可达 | 通 | 不通 |
| DoH 真实 IP + `curl --resolve` | **曾返回 206（1.0 s）** | **曾返回 206（0.8 s）** | 不需要 | 仍不通 |

`skynet/dns.py` 早就有「DoH 拿真实 IP + `curl --resolve host:443:ip` 直连」的实现。它**不是代理**：连接直接打到目标 IP，不经过 7890 / 3067 / karing，也不需要 `--set-proxy`。这解释了一件事：普通 `ProxyHandler({})` 直连打不开 GitHub 系，是 DNS 解析被污染，不是外网能力不存在。

几分钟后复验同一组 IP 又超时（国内线路间歇可达），Google / HuggingFace 即使换真实 IP 也始终不通（IP / SNI 层拦截）⇒ **不把一次成功写成稳定能力**。

### 落地方式

| 文件 | 作用 |
|---|---|
| `direct_ip.py` | 只对「已知普通直连失败」的域做 DoH 解析 + `curl --resolve` 真发请求验活；成功 IP 缓存 6 h，失败只缓存 2 min（网络域差异 / 线路恢复后自动重验）；生成 curl 的 `--resolve` 与 Chromium 的 `--host-resolver-rules` |
| `netpolicy.py` | 静态抓取命中缓存就走 `direct+doh`，不再去撞注定超时的普通直连；未命中照常走原链路 |
| `render_parse.py` | 无头浏览器按批次注入 `--host-resolver-rules`；实测带该参数启动正常（`Example Domain` 渲染成功） |

纪律复核：`nf.py probe` 只读、配置文件 md5 前后一致；`channels()` 对 github.com 仍返回 `[('direct','')]`；`system_proxy` 仍为「禁用」。另外修掉一个误伤：netprobe 矩阵可能测自 Bash 沙箱域（与宿主机域结果不一致），**不再拿它缩短渲染超时**，只作诊断；真正打通交给 `direct_ip`。

### 下一轮可优化清单（本次未做，留底）

1. **存量回填**：`classified.json` 38608 条「JS渲染」可在渲染成功后回写 title/category，但 `classify.py --recheck` 正在写同一文件，需文件锁或错峰，本次没碰。
2. **门户导航词噪声**：中文短词（网盘 / 图库 / 素材）出现在导航里仍会误标形态；可剔除导航块或给短词加出现次数阈值。
3. **发现优先级**：目前「新发现 > 存量垃圾 > 其余」，可改成按已解析质量分传播（高价值站的外链优先）。
4. **正文质量**：`JS_EXTRACT` 已取 main/article，可再加正文密度评分，对过短正文自动重取或标「需复核」。
5. **中文检索召回**：逐字切分 + 短语匹配准确率够、召回偏窄；可试 trigram tokenizer 或加同义词表。
6. **下载层双通道**：主人定的「大文件双通道」目前因「不自动切换出口」纪律只用固定单出口；若要并发镜像直连 + 官方通道，需**远程显式指定第二个出口**后再由下载器调度，未获授权不动。
7. **礼貌与配额**：下载器可加每域并发上限 / 请求间隔 / robots 开关；目前靠主爬虫已有的单域限流与 64 MB 单文件上限兜着。
8. **存量消化速度**：4.2 万待解析域，可按机器负载调 `--workers/--tabs`；本次未擅自提并发（主爬虫还在跑）。
9. **未重启 / 未常驻**：`resource_finder.py` 的浏览器择优与 `feed_discovered()` 要等下次重启才加载；watchdog 与 `pipeline.py --loop` 都没起。要起就一句话：`pythonw -u watchdog.py`（隐藏窗口、单例守护）。

## 双模式并发：保守默认 / 堆积自动激进（含 GPU 加速）

**结论：平时省资源、和正在跑的主爬虫和平共处；待解析域名堆过阈值就自动把整机多核 + 独显全拉满。** 模式由 `netpolicy.concurrency_plan()` 决定，下一跳生效、不用改代码。

### 自动切换逻辑（`netpolicy.py`）

`pending_count()` 数出「`resources.json` 里还剩多少域没被解析过」，跟 `_netpolicy.json` 的 `concurrency.auto_threshold` 比：

```
mode = auto 且 pending_count() > auto_threshold  →  aggressive
否则                                     →  conservative
手动 --mode conservative/aggressive 始终覆盖 auto
```

| 模式 | workers（进程） | tabs（每进程页签） | GPU | 适用 |
|---|---|---|---|---|
| 保守 `conservative` | 2 | 3 | 关 | 默认 / 常驻，不抢主爬虫的 CPU |
| 激进 `aggressive` | 8 | 3 | 开（Vulkan 独显） | 待解析 > 3000 时自动切 |

阈值与参数都在 `_netpolicy.json` 里热改，改完下一轮 `pipeline` 直接生效。

### GPU 加速无头渲染（四种组合实测，2026-11-04 联网核实 + 本机验证）

先说结论：**不是加个 `--enable-gpu` 就有 GPU**。本机四种组合逐一实跑，只有一种真吃到独显：

| 浏览器 / 参数 | launch | WebGL 渲染器 | 判定 |
|---|---|---|---|
| Playwright 默认（`chrome-headless-shell`）+ `--enable-gpu` | OK | 无 GPU 进程 | ❌ 恒 0% |
| Playwright 自带 `chromium-1248` + 完整 `chrome.exe` | **launch 即退** | — | ❌ 不可用 |
| 完整 Chromium + `--use-angle=d3d11` | OK | **AMD Radeon（核显）** | ❌ 绑错卡 |
| **系统 Edge + `--use-angle=vulkan`** | OK | **NVIDIA GTX 1660 Ti** | ✅ 采用 |

四条硬事实（都是踩过才知道的）：

1. **Playwright 在 `headless=True` 下默认启动的是 `chrome-headless-shell`**，不是完整 Chromium。那套壳的 GPU 进程被裁过，给再多 `--enable-gpu` 也是 0%（实测显存恒 122 MiB）。要真 GPU 必须换完整浏览器二进制。
2. **双显卡笔记本不能用 D3D11 判据**。网上通说「Windows 首选 `--use-angle=d3d11`」，但本机 d3d11 的 ANGLE 报的是 **AMD Radeon 核显**，`nvidia-smi` 自然恒 0%；只有 `vulkan` 才落到 GTX 1660 Ti。**换机器先用探针读 `UNMASKED_RENDERER_WEBGL`，别照抄通用建议。**
3. **光栅化要显式开**：`--enable-gpu-rasterization` / `--enable-zero-copy` / `--enable-accelerated-2d-canvas`，否则像素活仍可能落回 CPU 软光栅。
4. **GPU 对普通文本页几乎不出活**。实测跑 40 个文档站，独显利用率 0–34% 跳动；只有 WebGL / canvas / 视频 / 复杂合成才持续吃显卡。**决定吞吐的是并发数和网络等待，不是显卡** —— 这点必须说清楚，别把 GPU 当万能加速键。

落地：`browser_pick.find_gpu_browser()` 返回系统 Edge（其次 Chrome），`netpolicy.browser_launch_args(gpu=True)` 给 `--use-angle=<gpu_angle>` + 新无头 + 光栅化开关，`gpu_angle` 在 `_netpolicy.json` 里可改（本机固定 `vulkan`）。GPU 起不来按 `d3d11 → opengl → 纯 CPU` 逐级回退，不中断任务。

### 内存边界：并发数是唯一变量，跟总量无关

4.2 万条域名**不会同时进内存**——每个页签解析完就关，内存只跟「同一时刻开几个页签」有关。真正的固定开销是**每个浏览器实例自带的 browser 进程 + GPU 进程（各 100~400 MB）**，所以：

| 配置 | 并发 | 提交内存峰值 | 结果 |
|---|---|---|---|
| 10 实例 × 4 页签 | 40 | **30374 / 30789 MB（98.6%）** | 开始换页，吞吐崩到 0.1 域/秒 |
| **5 实例 × 6 页签** | 30 | 28.9 GB | 稳定，可用内存 1.7~3.9 GB |

**结论：要拉满整机就「减实例、加页签」，不能无脑堆实例数**——10 个实例等于白交 10 份浏览器/GPU 进程开销。本机 15.4 GB 内存的安全线是 30 并发左右。

```bash
python pipeline.py --rounds 1 --render-limit 40          # 自动判：堆太多→激进
python pipeline.py --mode aggressive --gpu               # 手动激进 + 强制 GPU
python pipeline.py --mode conservative                   # 手动保守
```

### 踩过的两个真阻塞（这轮修掉的）

1. **单进程 OOM（V8 堆爆）。** 激进初版按 `workers=8 × tabs=7 = 56` 并发，单个 Chromium 进程要同时撑 7 个 tab，触发 `FATAL ERROR: AlignedAlloc Allocation failed - process out of memory`。**改成 `tabs=3`**（8×3=24 并发，8 核全占、单进程内存安全），实测无 OOM。并发甜点：保守 `2×3`、激进 `8×3`。
2. **DNS 解析永久阻塞（整批卡死 7 分钟）。** `socket.create_connection(timeout=...)` 的超时**只管 connect、不管 `getaddrinfo`**；被污染/无响应域名会让 DNS 解析永久挂起，`asyncio.to_thread` 丢进线程池后整批不动。修复双保险：① `_reachable()` 里 `socket.setdefaulttimeout(timeout)` 同时约束 DNS；② `job` 层 `asyncio.wait_for(_render_one(...), timeout=cap)` 总超时（`cap = max(45, timeout+30)`）防内部 hang。修复后含死站在内的多域批次秒级判定、顺畅跑完。

### 第四轮实测（激进 40 域 / 39.0 s / 纯直连 / GPU on）

```
[pipeline] 并发模式=aggressive → workers=8 tabs=3 gpu=on
完成 40 条 | 渲染成功 36 | 拿到标题 34 | 通道 {'direct': 40}
新发现域名 130 | JS/静态资源 442 | XHR 端点 156
资源形态 TOP: 在线工具/API 18 · 未识别/待人工 14 · 文档/手册 3 · 社区/问答 3
主题标签 TOP: AI/机器学习 26 · 安全/渗透 11 · 系统/运维 6 · 教育/课程 4
高价值记录 10 条 | 本轮新落盘 40 条
```

无 OOM、无卡死、零代理、零端口扫描；主爬虫未重启、未推远程。

### 回写纪律（`sync_classified.py`，错峰/锁）

渲染成果落 `_kb/records/*.jsonl`（append-only，不碰 `classified.json`）。存量 38608 条「JS渲染/无标题」域的 title/category 回填，由 `sync_classified.py --merge` 安全合并：

- 检测到 `classify.py --recheck` 在跑 → **拒绝合并**（避免两个进程写同一个 `classified.json`）；
- 只补当前无标题的域，渲染失败（`ok=false`）不写回；
- 写前备份 `.bak`、本地锁 `.sync_lock`、原子写（`os.replace`）。

```bash
python sync_classified.py --status   # 看渲染成果库条数 / 可回写数 / recheck 是否在跑
python sync_classified.py --dry      # 预览将回写哪些域（不落盘）
python sync_classified.py --merge    # 安全合并（recheck 在跑时会被拒）
```

> 当前 `classify --recheck` 仍在跑（如 PID 22372 / 30088），按设计暂不回写；等它结束再 `--merge`。

## 2026-11-05 架构修正：HTTP-first 三级漏斗

### 为什么改：方向本来是错的

之前所有域名一律送进无头浏览器。200 域对照实测：

| 方式 | 吞吐 | 标题+正文齐全 | 内存 |
|---|---|---|---|
| 浏览器渲染 | 3.77 域/秒 | **17.8%**（89/500） | 530 MB/实例 |
| **HTTP 直取** | **5.05 域/秒** | **58.5%**（117/200） | 几十 MB |

更狠的是失败结构：`done` 表 1320 条里 946 条失败，其中 **673 条（71%）是 `unreachable(tcp)`**——浏览器连启动都没启动，光 TCP 预筛每条 2.5 秒就白烧了 28 分钟。

结论：**用 530 MB 的浏览器去取一段静态 HTML，等于开卡车送快递**。开源界（Scrapy / Crawlee / scrapy-playwright）的共识也是 HTTP 优先、浏览器按需渲染。

### 三级漏斗

```
① httpx 直取  ── 有标题或正文≥500字 ⇒ 直接结案（约 30–58%）
    │ 失败
    ▼
② 无头渲染    ── 只处理「空壳」：DOM 里没内容、必须跑 JS 的（约 15%）
    │ 连不上
    ▼
③ 死站长冷却  ── 域名保留，24 小时后再试，绝不删除
```

### 踩过的三个坑（都是自己的 bug，不是网络问题）

1. **`headings` / `jsonld` 结构不对**：`JS_EXTRACT` 返回字符串列表，`http_first` 给了 dict 列表 ⇒ `fine_classify` 里 `" ".join()` 直接抛 `TypeError`。
2. **提取异常被当成连接失败**：一个字段格式问题就让整批正常域被判死站。现在 `_one` 里网络异常与解析异常彻底分开，解析失败一律交浏览器重跑。
3. **信号量没生效**：`_one()` 收下了 `sem` 参数却漏了 `async with sem`，1000 个请求一股脑砸进 64 个连接的池子 ⇒ **2962 条 `PoolTimeout`**，全被记成死站。

第 3 条最阴：日志看起来像「全网都挂了」，实际是自己把连接池打爆。**所以 `fetch_many` 现在只要 ok=0 就一定打印失败样本的真实错误**，不再让人瞎猜。

### 参数与实测

- `--http-conc 64`（异步很轻，可到 96）、`--http-timeout 10`
- **连接超时单独压到 4 秒**：TCP 都建不起来的域就是死站，让它占满 10 秒纯属浪费并发槽
- 死站写入 `channel='direct(dynamic)/dead'`，`pending_domains` 据此给 **24 小时冷却**（普通失败仍是 6 小时）
- 误判修复只重置冷却时间（`ts=0`），**不删任何域名**

| 轮次 | HTTP 阶段 | 交浏览器 | 总耗时 | 对比纯浏览器 |
|---|---|---|---|---|
| 第 1 轮 | 96.4s / 834 解决 | 166 域 | **156.3s / 1000 域** | 265s ⇒ **提速 41%** |

### 关于「是不是用了落后的工具」

无头浏览器的本质是 **完整浏览器内核 + CDP 协议**，成本在「必须把多进程架构整个拉起来」（browser 进程 + 每 tab 一个 renderer + GPU + network），**跟页面大小无关**——所以你开 10 个页签和开 100 个，内存差不了多少。

开源界确实有更轻的路：

| 方案 | 25 并发内存 | 933 页耗时 | 对咱们的可行性 |
|---|---|---|---|
| Chrome / headless | 2 GB | 46 s | 现状 |
| **Lightpanda**（Zig 重写，兼容 CDP） | **123 MB** | **5 s** | ❌ **无原生 Windows 版**，只能 WSL2/Docker |
| 裸 CDP（跳过 Playwright 封装） | — | 快 **1.6×** | 可行，但要重写 500+ 行 |

判断：**暂不换引擎**。理由不是懒——浏览器阶段现在只占总耗时 **32%**（50s/156s），换裸 CDP 最多省 20 秒；换 Lightpanda 要引入 WSL 这层新依赖，收益和风险不成比例。真正的大头（HTTP 阶段 96s）里的死站，已经用「4 秒连接超时 + 24 小时冷却」处理。

等队列质量提上来、浏览器占比回升到 60% 以上时，再考虑上裸 CDP 或 Lightpanda 不迟。

## 2026-11-05 第二轮：三个真问题 —— 误杀、假快、只抓首页

第一轮跑通后，队列越跑越怪：第 1 轮 279 成功，第 6 轮只剩 **109 成功 / 884 死站**。查下去发现三处都不是「网络变差」，是自己的问题。

### 问题一：DNS 线程池只有 40，把活站大批判死（最严重）

抽查 15 个已被判死的域，**15 个全部能连上**，最快的 0.1 秒。

根因：`anyio` 默认线程池只有 **40**，而 `getaddrinfo`（DNS 解析）全在这个池子里排队。64 并发时几百个域名同时要解析，**排队时间本身超过 4 秒 connect 超时** ⇒ 站点还没开始连就已经"超时"了。

实测（300 个已判死样本，同样是 4 秒 connect 超时）：

| DNS 线程池 | 耗时 | 成功率 |
|---|---|---|
| 40（默认） | 28.9s | **44%** |
| **512** | **27.7s** | **80%** |

**同一个超时，只放开线程池，成功率翻倍，而且更快。** 修法一行：

```python
import anyio.to_thread
anyio.to_thread.current_default_thread_limiter().total_tokens = 512
```

这跟之前 `_reachable` 踩的 DNS 阻塞是同一个根：**`socket` 的 timeout 从不管 `getaddrinfo`**。

### 问题二：一次判死太粗暴 —— 改成三轮确认

第一轮用 4 秒快速筛（快），判死的**不许直接埋**，放宽超时再确认两次：

```
第 1 轮  64 并发 / 4s    → 好站当场走掉
第 2 轮  64 并发 / 10s   → 捞回抖动/慢站
第 3 轮  96 并发 / 18s   → 最后一次机会
三关全败 ⇒ 才记 channel='.../dead'（24 小时冷却，域名一个不删）
```

实测（同样 500 域）：

| 策略 | 成功率 | 判死 | 名义速度 | **真抓到内容的速度** |
|---|---|---|---|---|
| 单轮 4s | 26% | 360 | 15.1 域/秒 | 3.9 个/秒 |
| **三轮确认** | **88%** | **38** | 4.3 域/秒 | **3.8 个/秒** |

**名义速度慢了，但真正抓到内容的速度几乎一样**——因为单轮版本那 15 域/秒里有 74% 是在快速丢东西。判死判错，将来重试要再花一遍时间，不如当场做对。

> 后两轮并发不降：待处理的都是难啃的，降并发只会让它们排更多批、白等更多超时。

### 问题三：每个域只抓首页，深度是 0

之前 `http_first._one` 和 `render_parse._render_one` 都写死拼 `https://{domain}/`，**队列里就算有文章 URL 也用不上**。而首页往往只是导航页，真正的知识在文章页。

两处一起补：

1. **`_candidate_urls()`**：队列给的 URL 优先，页面级任务只抓那一页（换 scheme 重试）。
2. **`site_urls()`**：从页面链接里挑站内「像文章」的 URL 入队 —— **零额外请求**，白捡。

筛选规则（宁缺毋滥）：

- 只取同域（含子域），跨域交给 `new_domains` 走域名发现
- 排除导航/分页/标签/登录/作者/RSS/静态文件
- 加权：路径含 `blog|docs|article|wiki…` +3、带日期 +2、slug 带连字符 +2
- 阈值 ≥2 分才入队，每站上限 `deep_per_site`（默认 8，`_netpolicy.json` 热改，0=关闭）
- 只有正文 ≥1500 字的页面才展开（导航页展开出来还是导航页）

实测（4 个真实站点，全部命中真文章，无一例外）：

```
cloudflare     → /typescript-rust-port, /full-transaction-waf
阮一峰博客     → /blog/2026/10/weekly-issue-417.html
simonwillison  → /2026/Oct/29/api-credits
lwn.net        → /Articles/1105130
```

入队 key 用 `host/path`，与 `done` 表主键同形 ⇒ **天然去重、天然断点续跑**，不需要新建表。站内页最多占每轮 40%，否则会挤掉域名广度的份额。

### 这轮的数字

```
单轮 4s（误杀版）     500 域  33s   成功 130 (26%)  死站 360
DNS 线程池修复后      500 域  33s   成功 380 (76%)  死站 120
＋三轮确认            500 域 116s   成功 438 (88%)  死站  38
端到端 200 域         55.2s       成功 171 (85.5%)  站内 URL 产出 187 条 / 32 站
```

### 问题四：整机一半时间在空转 —— 两个阶段串行

主人原话：「**电脑总感觉就跟没有运行一样**」。实测坐实了：

```
HTTP 直取阶段    CPU 12–29%   GPU 0%（显存恒 122MB，Edge 进程 0 个）
渲染阶段        CPU 峰值 90%  GPU 14%（显存 431MB，47 个 Edge 进程）
```

原因是 `render_many` 里两个阶段**串行**：先跑完 1000 域的 HTTP（226 秒纯等网络），才轮到浏览器那几十个域。而 HTTP 阶段是 **IO 等待**，CPU/GPU 天生吃不上——这不是我们写得差，是所有爬虫的共性。

改法：**流水线**，把渲染挂到后台线程，跟下一轮 HTTP 重叠。

```
轮 N    [HTTP_N ................. 140s] → [提交渲染_N]
轮 N+1  [HTTP_N+1 ................. ]  ← 此刻渲染_N 正在后台跑
        → [收集渲染_N，已落盘] → [提交渲染_N+1]
```

- `render_parse.render_stage()` — 从 `render_many` 里拆出来的纯渲染入口
- `pipeline._start_render()` / `_collect_render()` — 后台线程提交与回收
- `persist()` 加 `@_serialized` 锁 —— 主线程和后台线程都会写盘，必须串起来
- 后台渲染**完成即落盘**，否则 done 表更新不及时，下一轮会把同一批域再取一次
- 进程退出时 `finally` 会 `_collect_render()` 等它跑完，不会丢数据

实测（1000 域）：

| | HTTP 阶段 | 成功率 | 轮次总耗时 |
|---|---|---|---|
| 串行 64 并发 | 226.0s | 88% | 226 + 渲染 |
| **流水线 96 并发** | **140.0s** | **87.2%** | **150.1s（渲染已在后台）** |

第二轮的日志会直接打出：`[render] 上一轮后台渲染 41 条已落盘（与本轮 HTTP 并行完成）`。

> 诚实话：**CPU/GPU 用不满不是 bug，是爬虫的物理属性**。1 个域名的真实处理成本里，绝大部分是「等对方服务器回话」。想让显卡满转只有一条路——把更多域塞给浏览器，而那条路已被实测证否（3.77 域/秒、成功率 17.8%）。

## 2026-11-05 第三轮：滚动调度与响应体上限（一次真实的进程崩溃换来的）

### 一、`fetch_many_slide()`：把「齐步走」换成「滚动窗口」

旧实现的致命结构不是并发不够，而是 **barrier**：每轮 `asyncio.gather` 把整批
（64 域）绑在一起，一批里只要有一条吃满 4s 超时，其余 63 条哪怕 0.3s 就返回了，
也必须等在屏障上一起进下一轮。日志里「第 1 轮仍 715 条未定」就是它的长相——
715 条不是慢，是被那几十条慢站按在原地。

新版给每个 worker 一个常驻循环，处理完立刻从队列补位；超时那条带着「第几次尝试」
回到**队尾**，下次单独拿更长的连接超时（4s → 10s → 18s）。判死三关的语义、
空壳判定、`done` 表写法全部不变，只换「谁等」。

| 同一批 150 个真实待爬域 | 耗时 | 吞吐 | 成功 |
|---|---|---|---|
| 三轮齐步走 `fetch_many` | 79.3s | 1.89 域/秒 | 120 |
| **滚动窗口 `fetch_many_slide`** | **53.3s** | **2.82 域/秒** | **124** |

**1.49 倍，成功数还多 4 条**。批次越大差距越明显（60 域时只有 1.19 倍）——
因为屏障的浪费量正比于批大小。

`pipeline.py` 默认走 `--http-mode slide`，保留 `rounds` 只为对照。
实现上整批复用一个 `AsyncClient`，连接池与 TLS 会话跨重试复用。

### 二、`MAX_BODY`：`r.text` 会把整个进程带走

跑批在第 7 轮直接死掉，日志只有三行：

```
internal buffer error : Memory allocation failed : growing buffer
```

栈顶指向 `httpx/_models.py:649` 的 `self._text = "".join(...)`。原因：
一级漏斗把**文件下载链接当网页读**。这一批里撞上的是 Blender 的 `.msix` 安装包
（`cdn.builder.blender.org/download/daily/*.msix`）和 TeamViewer 下载页，
响应体几十上百 MB，`r.text` 一次性解码进内存 ⇒ `MemoryError` ⇒ 进程没了。
更糟的是它发生在 `try` 之外，连兜底都轮不到。

现在改成 `_get_html()`：`cli.stream()` 逐块读，累计超过 `MAX_BODY = 4 MB`
立刻停手，连接正常关闭；先看 `Content-Length` 能更早拦下。超限的**不丢**，
记一条 `channel="direct(dynamic)/too-big"` 留档，`err` 里写清实际大小。

### 三、顺带暴露的真问题：文件不该进网页解析器

那 9 条 `too-big` 全是可下载的安装包——**它们不是「爬失败」，是「资源被当成了网页」**。
这解释了为什么 `download_assets` 每轮「落盘 0 个资源」：真正的文件在入口就被
网页解析器吃掉了，从来没进过下载队列。下一层要做的就是按 `Content-Type` 分流：
`text/html` 走解析，`application/octet-stream`、`*.msix/*.exe/*.zip/*.gguf`
直接登记进 `assets` 队列。

## 2026-11-05 第四轮：资源分流 —— 不阻挡，先认清是什么

上一轮把大文件「拦下不崩」了，但那只是防崩溃，不是处理。主人的要求很明确：
**下载资源该分流、该分类，阻挡是错误的。** 于是这一轮把入口改成三岔口。

### 判定发生在读正文之前

`_get_html()` 拿到响应头就先分流，**一个字节都不读**：

| 响应头 | 处置 | 记录 |
|---|---|---|
| `text/html` / `application/xhtml` / `json` / `xml` / `rss` | 进解析器 | `tier=http` |
| 其它 `application/*`、`image/*`、`audio/*`、`video/*`、`font/*` | **登记下载队列** | `channel=direct(dynamic)/asset`，`ok=True` |
| 无 `Content-Type` | 按网页处理（漏判网页的代价比漏判文件大） | — |

实测：TeamViewer 的 `TeamViewer_Setup_x64.exe`（86,387,776 B）
返回 `application/octet-stream` ⇒ 直接判 `asset`，读取字节数 **0**。
改动前这条会把 86 MB 读进内存，轻则慢，重则 MemoryError 带走进程。

`ok=True` 是有意的：这一条任务确实处理完了——识别出它是什么、送进了下载队列。
记成失败会让它进冷却池被反复重试，而重试永远不会把 exe 变成网页。

### 类型标签：看后缀，也看响应头

`download_assets.asset_kind()` 按「站点说明书 → 扩展名 → Content-Type」判定：

| kind | 含义 | 典型后缀 |
|---|---|---|
| `installer` | 安装包 | msix/msi/exe/dmg/pkg/apk/deb/rpm/iso |
| `model` | 模型权重 | gguf/ggml/safetensors/onnx/ckpt/pth |
| `dataset` | 数据集 | csv/tsv/parquet/arrow/npy/sqlite/jsonl |
| `doc` | 文档 | pdf/epub/djvu/docx/pptx/md/txt |
| `media` | 音视频图片 | mp3/mp4/mkv/wav/srt/jpg/png |
| `archive` | 压缩包 | zip/7z/rar/tar/gz/zst |
| `code` | 代码与配置 | py/ipynb/patch/diff/sql/yml/json |
| `feed` | **站点说明书**（不是资料） | sitemap/rss/atom/opensearch/osd/rsd/robots |

`feed` 这一类是被数据逼出来的：829 条 `.xml` 抽样 400 条，
feed 188 / index.xml 51 / osd.xml 46 / opensearch 41 / wlwmanifest 12 / sitemap 8——
九成是站点自己的说明书。它们该喂给**发现层**（xml 里全是 URL），
不该当文件囤起来，更不能算进「资源产出」。

### 首次盘点（3962 条存量线索）

```
doc       1691      feed      618      code      588      media     548
archive    400      installer 114      dataset     2      model       1
```

`assets` 表新增 `kind` 列（`ALTER TABLE` 补列，不重建，已下好的文件不丢），
`reclassify(force=True)` 可以在规则升级后全量回刷——否则旧账永远停在旧分类上。

### 还缺的一块

`enqueue_direct()` 跳过了扩展名白名单（`/download?id=123` 这类没有后缀的链接，
`worth()` 会一律拒收，而它们恰恰是最常见的真实入口），但仍会排除
favicon / 埋点 / 样式脚本。队列建好了，**下一步才是真的去下**。

## 2026-11-05 第五轮：定向关联爬取（seed_crawl.py）

主爬虫负责**发现**（全网平推，便宜、广、精度低），`seed_crawl.py` 负责**深挖**
（给一个主链接，精确展开这个站，窄、贵、精度高、账算得清）。两者并行，互不干扰。

目标很直白：**随处可看、随处可查**。靠平推做不到——你指一个站，它得能钻进去。

### 两种形态

| 形态 | 输入 | 做法 |
|---|---|---|
| `douyin` | 分享短链 / 作品链接 | 短链 302 取 ID（纯 HTTP）→ 渲染一次 → 拦 `detail`/`post`/`related` |
| `site` | 任意站的主链接 | 首页 HTTP 直取 → 站内链接按「像内容页」排序 → 批量直取 → 资源入队 |

`--platform auto` 按域名自动选；`site` 是兜底，任何站都能用。

### 抖音：渲染绕不过，但可以只做一次

四条免登录通道全部实测过：

| 通道 | 结果 |
|---|---|
| 分享页 HTML | 45 KB 空壳，`_ROUTER_DATA` 仅 1.5 KB 页面上下文，**无 `play_addr`** |
| 加 `?region=CN&mid=0` | 同上 |
| 旧 `iteminfo` | 200，body **129 B** 空体 |
| `aweme.snssdk.com` 移动端 | 200，**0 字节** |
| 网页版 `aweme/detail` | **403 Blocked by ArgusSecurityPlugin** |
| 爬虫 UA（Baiduspider/Googlebot） | 更差，0–27 KB |

页面内也不行：**原样重放已签名 URL（带有效 `a_bogus`）依然拿回 830 KB 空壳**，
换 XHR 一样；`window.byted_acrawler.frontierSign()` 只能算 `X-Bogus`，接口要的是
`a_bogus`，算不出。⇒ **`a_bogus` 与时间+环境绑定，绕不过渲染。**

优化的空间在两处，都在代码里落实了：

1. **一次渲染拿一片**：一次加载先后吐出 `detail`（1 条）+ `post`（作者全集 18 条）
   + `related`（20 条 / 20 个不同作者）。实测 **108 实体 / 105 关系边 / 131 资源**。
   🔴 不能「拿到第一个响应就关页」——第一个到的是 detail，大头全丢
   （实测只等 1 个接口：3 实体 / 2 资源）。
2. **复用浏览器上下文**：每条起浏览器 10–24s → 同 context 连续加载 **3.82s**
   （首条 6.22s，后续 2.63–3.90s）。

### 账本

`_kb/seeds.db` 三张表：`seeds`（待展开队列）、`entities`（作品/作者/话题/音乐，按
`platform:type:id` 去重）、`edges`（`by_author` / `tagged` / `uses_music`）。
资源一律走 `download_assets.enqueue_direct()` 进资源队列，不丢不挡。

首次实测（1 个种子 + 1 跳）：

```
种子 5 个已处理 / 63 个待展开
实体 337（work 68 / hashtag 156 / music 68 / author 45）
关系边 334
抖音资源入队 218（image + mp4 + 原声 mp3）
成本：约 12s/页（含等齐三类接口的沉淀时间）
```

### 用法

```bash
python seed_crawl.py --seed "https://v.douyin.com/xxxx/" --max-pages 20 --max-depth 2
python seed_crawl.py --seed "https://example.com/" --platform site --max-pages 60
python seed_crawl.py --show          # 只看队列与统计
python seed_crawl.py --max-pages 4   # 不给种子 = 接着跑队列里剩下的
```

`--budget` 是时间上限（秒），`--max-pages` 是页面预算，`--max-depth` 限制关联层数。
三个闸门一起卡，避免「顺着关系边爬全宇宙」——那是主爬虫的活。

## 2026-11-05 第六轮：先测再说 —— 国内平台实测矩阵与三个隐蔽 bug

这一轮没有加新功能，全部时间花在**实测国内平台到底长什么样**，然后按实测结果改。
结论先行：**统一类型不能靠设计推，只能从样本里长出来**。这一批测下来，
五个平台有四种完全不同的形态，任何「先设计抽象层」的做法都会设计错一半。

### 一、国内平台实测矩阵（全部亲自跑过，不是查文档）

| 平台 | 通道 | 免登录 | 单次产出 | 成本 | 结论 |
|---|---|---|---|---|---|
| **B 站** | `x/web-interface/view` `archive/related` `tag/archive/tags` `web-interface/card` | ✅ 全通、免签名 | 51 实体 / 40 相关 | 3.3s | 最优，纯 HTTP |
| **豆瓣** | `m.douban.com/rexxar/api/v2/{movie,book,music}/*` `recommendations` `interests` `subject_collection` | ✅ 全通 | 34 实体 / 20 相关 / 短评 | 2.4s（影）1.2s（书） | 书影音知识库，人物×作品二分图 |
| **抖音** | 渲染后拦 `detail`+`post`+`related` | ⚠️ 要渲染 | 108 实体 / 131 资源 | 11–13s | 内容密度最高，成本也最高 |
| **微博** | `weibo.com/ajax/statuses/hot_band` | ⚠️ **只有热搜** | 51 热点（含分类/热度/上榜时间） | 0.3s | 移动端全要登录，**桌面端反而开着** |
| **知乎** | — | ❌ `error.code=40362` 风控 | 0 | — | 直接拒绝 |
| **贴吧** | — | ❌ 返回 11 KB 反爬页 | 0 | — | 直接拒绝 |

三条反直觉的实测结果，写在代码注释里备查：

1. **微博是反的**：`m.weibo.cn` 所有接口 `ok=-100`（跳 passport 登录），
   但 `weibo.com/ajax/statuses/hot_band` 返回 `ok=1` 21 KB 真实数据。
   「移动端更宽松」这个常识在这里是错的。
2. **豆瓣人物没有 id**：详情里 `directors[].id` 和 `.url` 都是 `null`，只有名字。
   所以人物只能当**汇聚点**（同一人出现在多部片就连起来），不能当扩散种子。
3. **B 站和抖音差 40 倍**，差别不在破解难度，在**选对通道**：
   同一个「拿相关推荐」的需求，3.3 秒 vs 11 秒。

### 二、sitemap 发现层（新增 `sitemap_disc.py`）

主爬虫的发现靠「首页有什么链接就爬什么」，栏目页、分页里的文章永远看不到。
而 sitemap 是站方主动公开的全量目录，精确到每一篇。

实测 20 个国内站：**7 个有 sitemap，合计 37.9 万条 URL**
（thepaper 10.9 万 / cnblogs 23 万 / qbitai 3.6 万 / leiphone 3 千 / huxiu、ifanr、36kr 数百）。

实现要点（都是踩出来的）：

- **流式扫 + 8 MB 上限**：sitemap 动辄几十 MB，`r.text` 一把梭会重演 MemoryError
- **子图筛选**：WordPress 的 `wp-sitemap.xml` 下混着 posts/users/taxonomies/attachments
  四类子图。第一版全展开，结果拿到的全是 `/author/admin` 这种作者页。
  改成「含 post/article/news 优先，含 user/author/taxonom 跳过」之后，
  四个站的 300 条**全是文章页**，一条作者页都没有
- **双出口**：前 N 条给 `seed_crawl` 深挖（要质量），全量喂主爬虫 `fed` 队列（要覆盖）

实测 URL 质量：thepaper `newsDetail_forward_*`、huxiu `/article/*.html`、
cnblogs `/{user}/p/{id}`、qbitai `/2019/03/275.html` —— 清洗后干净。

### 三、Site 适配器改成三条路线

```
路线 A  sitemap 命中  → HTTP 批量取（thepaper 8/8，2.1s）
路线 B  无 sitemap     → 首页链接扩散 + 按「像内容页」排序
路线 C  空壳站         → 自动升级渲染（huxiu 8/8，12.5s）
```

路线 C 的触发条件是「成功页 ≤ 总数三分之一」，**且必须把 `rest`（空壳队列）也算进分母**。
第一版只算 `pages`，导致 huxiu 这种「一页都没成功、全在 rest 里」的情况
被误判成「没什么可渲染」，整站直接放弃。

### 四、三个隐蔽 bug（都是「写了但没生效」）

**bug 1：渲染永远渲染首页。** `render_parse._render_one` 里写死
`url = "%s://%s/" % (scheme, dom)`，完全忽略 `item["url"]`。
后果不只是定向抓取——**主爬虫的站内深挖也一直无效**：
2000 条/半小时的 url 类记录，渲染回来的全是首页，被去重成同一个实体。
实测 huxiu 8 个文章 URL → 8 条记录全叫「虎嗅网」4509 字；
修复后 → 「宇树用利润换时间…」7154 字、「好想来三万家门店…」5537 字。

**bug 2：适配器写了却从不执行。** 主循环只调 `ad.run(bw, seed, ...)`，
而 `Bilibili` 和 `Douban` 暴露的是 `handle(url, seed, a)`。
两个纯 HTTP 适配器写完之后一次都没跑过，日志里自然「一次都没被调用到」。
改成 `hasattr(ad, "handle")` 双形态分发。

**bug 3：`else: rest = []` 把空壳队列抹了。** 这个 `else` 本意是
「不走路线 B 时保持 rest 为空」，实际配对的却是 `if not pages and not rest:`，
于是 sitemap 路线攒下的 8 条空壳记录被清空，渲染兜底永远不触发。
代价是 huxiu 从「8/8 成功」变成「0/8，耗时 2 秒」。

**共同的教训**：这三个 bug 都不会报错，只会**安静地不生效**。
系统看着在跑、日志看着正常、数字还在涨，但做的是错的事。
所以「改了要立刻用真实数据验证输出内容」比「改了要跑通」重要一个量级——
跑通只证明没崩，验证输出才证明做对了。

### 五、种子优先级

`--seed` 给的新种子原来会被队列里上千个旧种子挤到最后（B 站、豆瓣各踩一次）。
`seeds` 表补 `prio` 列（ALTER 加列，不重建表）：0=手动指定（插队），1=自动发现。
`pop_seed` 按 `prio ASC, depth ASC, ts ASC` 取。

## 数据不随仓库分发

`resources.json`、`_visited.db`、各语料 `*.jsonl`、日志、状态文件都在 `.gitignore` 里，理由有两条：体积，以及抓下来的内容里含大量与项目无关的第三方站点，没必要也不适合公开分发。

## 许可

**禁止商用**（Netforce Non-Commercial License 1.0）。个人学习、研究、非营利使用与修改分发自由；任何以营利为目的的使用需另行取得书面许可。详见 [LICENSE](LICENSE)。

爬取他人站点时请自行遵守目标站的 robots.txt、服务条款与所在地法律。
