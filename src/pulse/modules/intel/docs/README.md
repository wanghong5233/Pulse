# Intel Module

资讯订阅 / 情报聚合域。单一 `IntelModule` 跑确定性六步 workflow,按主题 YAML 订阅多渠道信号,产出日报推送 + 高分条目沉淀到长期记忆。

## 当前实现

| 维度 | 现状 |
|---|---|
| 业务形状 | 一个 `IntelModule`,多个 topic YAML;新增主题 = 加 `topics/<id>.yaml`,不改代码 |
| 工作流 | 固定六步 pipeline:`fetch → dedup → score → summarize → diversify → publish`,每步独立可单测 |
| 信源 | `rss`(`rsshub://route` 由 `IntelSettings` 解析到第一个健康实例)、`web_search`(DuckDuckGo 兜底)、`github_trending`(GitHub Search API + 可选 `GITHUB_TOKEN`) |
| 评分 / 摘要 | `LLMRouter.invoke_json` (classification 路由) + `invoke_text` (generation 路由),按主题 rubric 给 0-10 分 |
| 去重 | URL canonicalize(去 utm/排序 query)+ 标题归一化 hash,store 端 `canonical_url UNIQUE` |
| 反茧房 | `pipeline/diversify.py`:source quota + contrarian-keep + serendipity slot |
| 推送 | `Notifier` 抽象,默认 Console + Feishu(走 webhook 卡片),不在模块内组装 webhook 细节 |
| 检索 | `intel.search` 走 `IntelDocumentStore.search`(ILIKE 字面量),不引入向量库 |
| 调度 | `AgentRuntime.register_patrol`,每个 topic 一个 patrol(`intel.digest.<id>`);peak/offpeak 间隔来自 topic config |

## 入口契约

| Intent | Trigger | 行为 |
|---|---|---|
| `intel.digest.list` | IM / Brain | 列出所有主题 + 上次采集元数据 |
| `intel.digest.latest` | IM / Brain | 取某主题最新 N 条已落库内容 |
| `intel.digest.run` | IM / Brain / patrol | 立即跑一遍 workflow,`dry_run` 跳过推送 |
| `intel.search` | Brain ReAct | 跨主题关键词 ILIKE |

HTTP 同步暴露在 `/api/modules/intel/{health,digests,digests/{id}/latest,digests/{id}/run,search}`。

## 子文档

- [`architecture.md`](architecture.md) — workflow 六步契约 + 数据模型 + `SourceFetcher` Protocol
- [`adding-a-topic.md`](adding-a-topic.md) — 新主题接入 5 步教程
- [`source-types.md`](source-types.md) — 各 source_type 配置 + 新增 type 流程

## 决策记录

- **workflow 而非 sub-agent** — 控制流由代码而不是 LLM 决定;LLM 只在 score / summarize 单步内被调用,降低不确定性。Brain 唯一 agentic 入口是 `intel.search`。
- **single module, multi topic** — "面经""技术雷达"是用户视角主题,不是不同能力。能力只有 1 套,主题用 YAML 装配,新增主题不触发代码 review。
- **PR1 不上向量库** — 单用户语料 < 10⁴ 行,ILIKE P95 < 10 ms;触发升级条件:`总行数 > 5×10⁵` 或 `召回失败 ≥ 5 起`。
- **PR1 RSS 用标准库** — `xml.etree.ElementTree` 已覆盖 RSS 2.0 / Atom / RSSHub;`feedparser` 是新依赖且行为不可控,触发升级条件:`真实站点 RSS 解析失败 ≥ 3 起`。
- **schema 不自动迁移** — 旧 `intel_documents` 占位表 column set 不兼容新设计;`ensure_schema` 检测到缺列时直接抛错,要求人工 `DROP TABLE`,避免静默丢数据。
- **RSSHub 软依赖** — 主题 YAML 用 `rsshub://route` 写路由,基础地址由 `PULSE_INTEL_RSSHUB_INSTANCES` 控制(默认自部署优先 + 公共镜像兜底)。`docker-compose --profile rsshub up` 起自托管副本;不开 profile 时公共镜像仍工作,模块本身不会因为 RSSHub 缺席启动失败。
