# Source Types

每个 source type 实现 `SourceFetcher` Protocol(`async fetch(cfg) -> SourceFetchResult`),返回 `RawItem` 列表。Pipeline 在 `fetch.py` 里 `asyncio.gather` 并发跑所有 source。

## 1. 已实现

| type | 用途 | 必填字段 | 主要可选字段 | 约束 |
|---|---|---|---|---|
| `rss` | 标准 RSS 2.0 / Atom feed,亦覆盖 RSSHub 路由 | `url` | `label`, `max_results`, `weight` | 仅标准库 `xml.etree.ElementTree`;不引 `feedparser`;User-Agent 固定;timeout 上限 60s。`url` 以 `rsshub://<route>` 开头时走 RSSHub 解析(见 §2) |
| `web_search` | DuckDuckGo HTML 兜底搜索 | `query` | `label`, `max_results`, `weight` | 复用 `pulse.core.tools.web_search.search_web`;`max_results` 上限 20 |
| `github_trending` | GitHub Search API,按 `created:>=since stars:>1` 拟合 trending | `since`(`daily`/`weekly`/`monthly`) | `language`, `spoken_language`, `label`, `max_results`(≤100) | 用 `urllib`,无第三方依赖;读 `GITHUB_TOKEN` 走 5000 req/h,无 token 走 60 req/h;同时输出 `extra` 携带 `stars` / `forks` / `language` / `topics` 给后续打分参考 |

## 2. RSSHub-first 寻址(`rsshub://`)

RSS 源的 `url` 字段支持两种写法,主题 YAML 不需要写死实例域名:

| 形式 | 行为 |
|---|---|
| `https://...` | 直接拉,跟普通 RSS 一样 |
| `rsshub:///nowcoder/discuss/2` | 由 `IntelSettings.rsshub_instance_list` 解析,按顺序找第一个健康实例 |

实例列表由 `PULSE_INTEL_RSSHUB_INSTANCES` 控制,默认 `http://rsshub:1200,https://rsshub.app,https://rsshub.rssforever.com`:

- 自部署的 `docker compose --profile rsshub up` 把容器内 `http://rsshub:1200` 排第一,优先吃自己的实例。
- 公共镜像作为兜底,任何一个实例 down 了 fetcher 自动跳到下一个。
- 健康探针(`HEAD <base>`)结果缓存 `PULSE_INTEL_RSSHUB_HEALTH_TTL_SEC` 秒(默认 300s),既不每次请求重探,也不被永久故障实例拖死。
- 全部实例都不健康时返回 `SourceFetchResult(error="rsshub: all instances ...")`,patrol 事件埋点会标 `source_failed`,我们能看见。

## 3. RawItem 契约

| 字段 | 来源建议 |
|---|---|
| `url` | RSS `<link>`,Atom `<link rel="alternate">`,搜索 hit URL |
| `title` | `<title>` 文本 |
| `content_raw` | RSS `<description>`,Atom `<summary>`/`<content>`,搜索 snippet;尽力填,长度可任意 |
| `source_type` | 与 register key 一致(`"rss"` / `"web_search"` / ...) |
| `source_id` | 稳定连接器标识;有 `label` 时用 `label`,否则用 `host`(`domain_of(url)`) |
| `published_at` | RSS `<pubDate>` (RFC 822) / Atom `<published>` (ISO 8601);搜索结果用 `now` |
| `weight` | 直接透传 `cfg.weight`,diversify 同分排序用 |

非必填字段缺省即可;不要为占位写假数据,空字符串比 `"unknown"` 更诚实。

## 4. 添加新 type

1. 在 `topics/_schema.py` 的 `SourceType` Literal 里加一行(让 YAML 校验放行)。
2. 新建 `sources/<type>.py`,实现 class 暴露 `source_type: str` 与 `async def fetch(cfg) -> SourceFetchResult`,文件末尾调一次 `register_fetcher("<type>", <Class>)`。
3. 在 `sources/__init__.py` 里 `from .<type> import <Class>` 并加进 `__all__` —— 这是触发 register 的方式。
4. 本文件 §1 加表行,`adding-a-topic.md` 示例补一份 YAML 片段。
5. 加单测覆盖 `fetch(cfg)` 的成功 / 部分失败 / 网络错误三种路径。

## 5. 失败行为不变式

| 失败类型 | 期望表现 |
|---|---|
| 网络 / DNS 错误 | `SourceFetchResult(error="transport error: ...")`,items 为空 |
| 解析错误 | `SourceFetchResult(error="parse error: ...")`,items 为空 |
| 字段不全的单条 | `continue`,不往 RawItem 列表里塞 |
| 完全意外的 `Exception` | 抛出去由 `fetch_all_sources` 的 `gather` 捕获 → 上层标 source 异常 |

不要在 fetcher 里捕一切异常返回空列表;那样 patrol 会安静失败,我们要让事件埋点能看到 `source_failed` 计数。
