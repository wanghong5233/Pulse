# Intel Architecture

资讯采集是确定性 workflow,工程上实现为分阶段 pipeline。控制流不依赖 LLM 自主推理;LLM 只被注入到 score / summarize 单步内。

## 1. 现状

```mermaid
flowchart LR
    Patrol[AgentRuntime patrol] --> O[Orchestrator]
    O --> F1[fetch]
    F1 --> F2[dedup]
    F2 --> F3[score]
    F3 --> F4[summarize]
    F4 --> F5[diversify]
    F5 --> F6[publish]
    F6 --> S[(intel_documents)]
    F6 --> N[Notifier]
    Brain -. intel.search .-> S
```

每个 topic YAML 装配一份完整 workflow 配置,Orchestrator 串联六步并向 EventBus 发射结构化事件。失败隔离:某个 source 抓取失败只影响那一个源,不阻塞 topic;某条记录 LLM 调用失败标记 `_error` 入审计,不污染日报。

## 2. 分层职责

| 层 | 负责 | 不负责 |
|---|---|---|
| `module.py` | 装配依赖、注册 patrol、暴露 HTTP / IntentSpec | 业务流程 |
| `pipeline/orchestrator.py` | 串联六步、发事件、聚合统计 | 单步业务 |
| `pipeline/{fetch,dedup,score,summarize,diversify,publish}.py` | 单步纯函数,输入输出明确 | 跨步状态 |
| `sources/<type>.py` | 实现 `SourceFetcher` 协议,纯抓取 | 调用 LLM、写库 |
| `topics/_schema.py` + YAML | 主题级配置(信源、rubric、阈值、调度) | 跨主题流程 |
| `store.py` | `intel_documents` DAL,append/search/list | 评分、摘要 |

## 3. 六步 workflow 契约

| 阶段 | 输入 | 输出 | 关键不变式 |
|---|---|---|---|
| fetch | `TopicConfig` | `list[SourceFetchResult]` | 单 source 失败不阻塞;每 source 内部限 `max_results` |
| dedup | `list[RawItem]` + 已存 canonical 集合 | `(list[RawItem], list[canonical_url])` | 输出列表与 canonical 列表 1:1 对齐 |
| score | `list[RawItem]` + topic | `list[ScoredItem]` | 评分失败 → `score=0`,带 `_error` 入 breakdown |
| summarize | `list[ScoredItem]`(已过 threshold) | `list[SummarizedItem]` | LLM 失败回退到 `content_raw[:240]`,不产空摘要 |
| diversify | `list[SummarizedItem]` + topic.diversity | reordered `list[SummarizedItem]` | 单 source ≤ `max_per_source`;contrarian 不被挤出 |
| serendipity 注入 | orchestrator 查 `IntelDocumentStore.serendipity_pool` | `list[dict]` 跨主题高分行 | 数量 ≤ `topic.diversity.serendipity_slots`;只注入 publish 文本,不重新落库 |
| publish | `list[SummarizedItem]` + canonical_urls (+ serendipity + archival_memory?) | `DigestPublishResult` | 写库 (canonical UNIQUE) → notifier;高分条目 add_fact 到 `facts`;`dry_run=True` 时只写库不推送也不晋升 |

## 4. 数据模型

### 4.1 `intel_documents`(PG)

| 列 | 类型 | 用途 |
|---|---|---|
| `id` | UUID PK | 内部主键 |
| `topic_id` | TEXT | YAML 主题 id |
| `source_id` / `source_type` | TEXT | 连接器标识(`domain` 或 RSSHub route)、连接器类型 |
| `url` / `canonical_url` | TEXT | 原始 URL / 去重键(canonical_url UNIQUE) |
| `title` / `content_raw` / `content_summary` | TEXT | 原标题 / 抓取正文 / LLM 摘要 |
| `score` | REAL | 0-10,LLM 评分 |
| `score_breakdown` | JSONB | `{dimensions, tags, is_contrarian?, _error?}` |
| `tags` | JSONB | 短标签数组 |
| `published_at` / `collected_at` | TIMESTAMPTZ | 原文时间 / 入库时间 |
| `promoted_to_archival` | BOOL | 是否已沉淀到 ArchivalMemory(PR3) |

索引:`(topic_id, collected_at DESC)` 用于 latest 列出;`score DESC` 用于检索排序。`canonical_url` UNIQUE 同时承担去重与 upsert key。

### 4.2 `RawItem` / `ScoredItem` / `SummarizedItem`

| dataclass | 关键字段 | 来源 |
|---|---|---|
| `RawItem` | `url, title, content_raw, source_type, source_id, weight, published_at` | `sources/<type>.py` |
| `ScoredItem` | `item: RawItem, score, score_breakdown, is_contrarian` | `pipeline/score.py` |
| `SummarizedItem` | `scored: ScoredItem, summary` | `pipeline/summarize.py` |

## 5. 去重策略

| 层 | 实现 | 触发条件 |
|---|---|---|
| URL 规范化 | 小写 scheme/host + 去 `utm_*` / `fbclid` / `spm` 等 + 排序 query + 去尾斜杠 | 每条 item |
| 批内标题 hash | `sha1(normalised_title)[:16]` | 同一批内重复标题 |
| 跨批已知集合 | `IntelDocumentStore.existing_canonical_urls(urls)` | 入 `dedup` 阶段 |

升级条件:`重复误判 case ≥ 5 起` 或 `单 topic 入选率 < 10%` 时引入 MinHash / LSH。

## 6. 检索策略

`IntelDocumentStore.search(keywords, topic_id?, top_k, match)` 做字面量 ILIKE,不维护 embedding。

| 维度 | 分析 | 结论 |
|---|---|---|
| 数据规模 | 单用户预计 `intel_documents` ≤ 5×10⁴ 行 | PG ILIKE P95 < 50 ms |
| 能力归属 | LLM 是最强语义理解器 | 关键词扩展归 Brain,store 只字面匹配 |
| 写入成本 | embedding 每条新增一次推理 | 触发一次 LLM,贵 |
| 可逆性 | 写入走统一 record + canonical_url | 未来加向量列即新增 retriever,旧路径不用动 |

升级条件:`总行数 > 5×10⁵` 或 `召回失败 ≥ 5 起` 同时出现。

## 7. LLM 调用契约

| 阶段 | 路由 | 方法 | 失败行为 |
|---|---|---|---|
| score | `classification` | `invoke_json` | 返回 `None` → `score=0`,breakdown 标 `_error`,不进入 publish 阈值 |
| summarize | `generation` | `invoke_text` | 抛异常 → 回退 `content_raw[:240]`,数字段不为空 |

不在 pipeline 控制流里做 LLM judge / planner。score 与 summarize 都是单 item per call,以失败粒度换 prompt 简单度;批量化推迟到 token 成本变敏感时。

## 8. 事件埋点

每个 stage 完成后,Orchestrator 调用 `module.emit_stage_event(stage="intel.<阶段>", status="completed", trace_id, payload)`。EventBus 自动落 JSONL,`tail -f pulse.log` 也会看到一行结构化日志。

`workflow` 伪 stage 标记整个 patrol 起止,字段含 `topic_id` / `elapsed_ms` / `published`。

## 9. 与内核的整合

| 内核能力 | 整合点 |
|---|---|
| `AgentRuntime` | 每 topic `register_patrol`,`peak_interval_seconds` / `offpeak_interval_seconds` 来自 `topic.publish` |
| `EventBus` | `module.emit_event` / `emit_stage_event` 由 BaseModule 提供;Orchestrator 只调上层 emitter |
| `LLMRouter` | `route="classification"` 评分;`route="generation"` 摘要;失败 fallback 链由 router 内部负责 |
| `Notifier` | `MultiNotifier([ConsoleNotifier, FeishuNotifier])` 默认装配;Feishu 走 `core/notify/webhook.py` 的卡片 |
| `IntentSpec` | 4 个 intent 全 IntentSpec 路径,无 `handle_intent`;经 `ModuleRegistry.as_tools()` → `ToolRegistry` → `MCPServerAdapter` 链路自动暴露给 Brain ReAct **以及** 外部 MCP 客户端 |
| `MemoryRuntime` / `ArchivalMemory` | publish 阶段对 `score ≥ topic.memory.promote_threshold` 的记录调用 `ArchivalMemory.add_fact`,subject=`intel:<topic>`,predicate=`high_score_signal`;`promoted_to_archival` 列与 `facts.evidence_refs` 双向追踪 |
| `IntelSettings` (`PULSE_INTEL_*`) | 业务域专属配置;承载 RSSHub 实例列表 / 健康探针 TTL / 探针超时,主题 YAML 用 `rsshub://route` 指向逻辑路由,实例切换是纯运维变更 |

## 10. 不变式速查

- `dedup_items` 输出的 items 与 canonical_urls 同长度,publish 阶段必须按下标对齐传入。
- `IntelDocumentStore.append` 通过 `ON CONFLICT (canonical_url) DO UPDATE` 实现幂等,score 取 GREATEST,旧分不会被低分覆盖。
- `ensure_schema` 检测到缺列直接抛 `RuntimeError`,要求人工 `DROP TABLE`;不做隐式 ALTER。
- 所有 LLM 路径只使用 `LLMRouter`;模块内不直接 import `langchain_openai`。
- 模块入口仅在 `module.py`;`pipeline/` / `sources/` / `topics/` 目录由 `ModuleRegistry.discover` 在递归时跳过(无 `module.py`),不会被注册成独立 module。
