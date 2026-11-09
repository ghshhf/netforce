"""天网（Skynet）采集内核
==========================
各层一体，供北极星语料产线与后续项目共用：

    skynet.search   搜索层 —— 去哪儿找（多引擎聚合 + 意图校验 + 引擎健康度）
    skynet.net      下载层 —— 怎么拿（会话复用 + 域限速 + 并发闸门 + 熔断 + 续传）
    skynet.probe    检测层 —— 值不值得（四级判定：可达 / 实质 / 密度 / 解析校验）
    skynet.store    记录层 —— 记过什么（去重 + 域画像 + 失败重试 + 内容指纹）
    skynet.discover 结构层 —— 站里有什么（robots + sitemap + feed）
    skynet.route    路由层 —— 从哪儿出去（跳转链穿透 / 多级代理链）
    skynet.dns      DNS 层 —— 解析对不对（DoH + 污染检测，喂给 curl --resolve）
    skynet.reverse  逆向层 —— 藏着什么（端点 / 字符串 / 时间轴 · 只提取知识，不做绕过）
                    2026-10-23 融合：三轴路由 + 证据分级 + 工具台账 + 版本差分 +
                    经验写回（取自 rea / reverse-skill / ASC，只取规矩不搬代码）
    skynet.selfindex 自建发现层 —— 不依赖搜索引擎的资源发现（2026-11-02 新增）
                    ① 证书透明度日志（crt.sh / certspotter）：挖域名/子域，搜索引擎不收录的也能挖
                    ② Common Crawl 全网快照索引：按域名/后缀查真实存在过的 URL
                    ③ 链接图扩散（graphdiffuse）：多跳 BFS 爬外链图，不经过任何搜索
                    搜索引擎降级为「冷启动种子源」，供给不再受限于别人索引的那一小部分
    skynet.cloud    云厂商资产测绘层 —— 读「云厂商的公开账本」（2026-11-02 新增）
                    ① URLScan.io 搜索 API（免 key）：按云厂商 ASN 反查域名、按服务器指纹反查
                    ② Shodan InternetDB（免 key）：按 IP 反查 hostname / 技术栈 / 端口
                    现在绝大多数网站都在云厂商上，按 ASN 一条搜索挖出一片网站，比爆破快几个量级
    skynet.ghcode   GitHub 代码挖域名层 —— 从全站代码里挖「自建站」域名（2026-11-02 新增）
                    走 gh CLI 的 Code Search（token 由 gh 自管），按 NAS/DDNS/免费主机后缀
                    搜代码里留下的域名痕迹，专门挖搜索引擎看不见的小众站 / NAS 站 / 免费服务器站

设计约束（与项目铁律一致）：
    · 后台进程不弹窗（curl 分支带隐藏窗口三件套）
    · 不碰浏览器控制
    · 只读为主；任何对外写操作必须显式点名
    · 逆向层只做知识提取：不解密、不破授权、不绕付费墙；凭据只计数不取值
"""
from . import net, probe, search, store, discover, dns, reverse, selfindex, cloud, ghcode   # noqa: F401

__all__ = ["net", "probe", "search", "store", "discover", "dns", "reverse",
           "selfindex", "cloud", "ghcode"]
__version__ = "0.10.0"
