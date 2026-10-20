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

## 数据不随仓库分发

`resources.json`、`_visited.db`、各语料 `*.jsonl`、日志、状态文件都在 `.gitignore` 里，理由有两条：体积，以及抓下来的内容里含大量与项目无关的第三方站点，没必要也不适合公开分发。

## 许可

**禁止商用**（Netforce Non-Commercial License 1.0）。个人学习、研究、非营利使用与修改分发自由；任何以营利为目的的使用需另行取得书面许可。详见 [LICENSE](LICENSE)。

爬取他人站点时请自行遵守目标站的 robots.txt、服务条款与所在地法律。
