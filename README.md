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

## 运行

```bash
pip install curl_cffi          # 可选：TLS 指纹伪装，能过部分 WAF
python resource_finder.py                      # 资源发现，无限跑
python resource_finder.py corpus blhx --until 06:00
python resource_finder.py diffuse --limit 20   # 先小样本试跑
```

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `NETFORCE_PROXY` | `http://127.0.0.1:3067` | 出网代理；置空字符串 = 全程直连 |
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

## 数据不随仓库分发

`resources.json`、`_visited.db`、各语料 `*.jsonl`、日志、状态文件都在 `.gitignore` 里，理由有两条：体积，以及抓下来的内容里含大量与项目无关的第三方站点，没必要也不适合公开分发。

## 许可

**禁止商用**（Netforce Non-Commercial License 1.0）。个人学习、研究、非营利使用与修改分发自由；任何以营利为目的的使用需另行取得书面许可。详见 [LICENSE](LICENSE)。

爬取他人站点时请自行遵守目标站的 robots.txt、服务条款与所在地法律。
