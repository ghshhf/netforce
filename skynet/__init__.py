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

设计约束（与项目铁律一致）：
    · 后台进程不弹窗（curl 分支带隐藏窗口三件套）
    · 不碰浏览器控制
    · 只读为主；任何对外写操作必须显式点名
"""
from . import net, probe, search, store, discover, dns   # noqa: F401

__all__ = ["net", "probe", "search", "store", "discover", "dns"]
__version__ = "0.5.0"
