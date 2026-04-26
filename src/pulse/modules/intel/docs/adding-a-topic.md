# Adding a Topic

新增主题 = 加一个 YAML 文件,不改代码。所有可调项见 `topics/_schema.py` 中的 `TopicConfig`。

## 5 步流程

### 1. 复制模板

把 `topics/_examples/` 下任一 YAML 复制到 `topics/<id>.yaml`。`<id>` 必须只含小写字母、数字与下划线(校验:`^[a-z0-9_]+$`),会作为 `intel_documents.topic_id` 与 patrol 名(`intel.digest.<id>`)。

### 2. 选信源

`sources` 至少一条,常用三种 type:

```yaml
sources:
  - type: rss
    url: https://example.com/feed.xml
    label: example                  # 可选, 用于 source_id 区分同 host 多路由
    weight: 1.0                     # diversify 排序时同分按 weight 高优
    max_results: 30

  - type: rss                       # RSSHub 路由用 rsshub:// 写
    url: rsshub:///openai/blog      # 实例由 PULSE_INTEL_RSSHUB_INSTANCES 解析
    label: openai_blog
    weight: 1.0

  - type: web_search
    query: "AI agent observability"
    weight: 0.6                     # 搜索类排序权重通常调低
    max_results: 10

  - type: github_trending           # GitHub Search API,可选 GITHUB_TOKEN
    language: python
    spoken_language: en
    since: weekly                   # daily / weekly / monthly
    weight: 0.8
    max_results: 30
```

`rsshub://` 形式只关心路由,基础地址由 `IntelSettings`(`PULSE_INTEL_RSSHUB_INSTANCES`)按顺序探活回填;不开自部署 profile 时也能用公共镜像兜底。

### 3. 写 rubric

```yaml
scoring:
  threshold: 6.5
  rubric_dimensions:
    - depth
    - novelty
    - impact
  rubric_prompt: |
    评分维度 (0-10):
    1. depth: 技术深度 vs 营销
    2. novelty: 是否引入新概念
    3. impact: 工程实用性 vs 纯学术
```

`threshold` 表示进入 publish 的分数下限;`rubric_dimensions` 每项都会向 LLM 索取一个 0-10 的子分。LLM 返回的总分缺失时 orchestrator 用维度均值兜底。

### 4. 调多样性 / 调度 / 长期记忆阈值

```yaml
diversity:
  max_per_source: 2          # 单 source 单期最多 N 条
  serendipity_slots: 1       # 每期附加 N 个跨主题高分条目(orchestrator 查 store 注入)
  contrarian_bonus: 0.5      # LLM 标 is_contrarian=true 时加分

publish:
  schedule_cron: "0 9 * * *"          # 仅作展示, 实际调度走下两项
  channel: feishu
  format: digest_zh
  peak_interval_seconds: 3600         # patrol 高峰时段间隔
  offpeak_interval_seconds: 21600     # 静默时段间隔(秒); 注意单位

memory:
  promote_threshold: 8.5     # >= 该分自动标 promoted_to_archival, PR3 接 MemoryRuntime
```

### 5. 重启 + 验证

服务重启后:

```bash
# 列出主题, 确认新主题在列
curl http://localhost:8000/api/modules/intel/digests

# 立即跑一遍 (dry_run 跳过飞书推送)
curl -X POST http://localhost:8000/api/modules/intel/digests/<id>/run \
  -H 'Content-Type: application/json' \
  -d '{"dry_run": true}'

# 看采到了什么
curl 'http://localhost:8000/api/modules/intel/digests/<id>/latest?limit=20'
```

或在 IM 里直接说 `/intel digest run <id>`。

## 常见坑

| 现象 | 原因 | 处置 |
|---|---|---|
| 启动报 `invalid topic config` | YAML 不符合 `TopicConfig` schema | 看异常里的 ValidationError 字段名,`fail loud` 是约定,不要绕 |
| RSSHub 链路全失败 | 公共镜像 + 自部署都不健康,error 含 `rsshub: all instances ...` | 改 `PULSE_INTEL_RSSHUB_INSTANCES` 顺序或 `docker compose --profile rsshub up -d` 起本地副本 |
| 跑了几次后 `published=0` | 已落库的 canonical_url 命中,被 dedup 跳掉 | 正常;查阈值是否过高用 `dry_run=true` + `score_breakdown` |
| Feishu 没收到 | `NOTIFY_WEBHOOK_URL` 未配 | `Notifier` 已记日志,看 `delivery.error`;不在模块内组装 webhook |
| LLM 全部 `_error` | API key / 路由模型不可达 | `LLMRouter` 有路由 fallback;事件 `llm.invoke.exhausted` 标记真死 |

## 把模板转正

`topics/_examples/` 下文件以 `_` 起首,不会被发现(`discover_topic_files` 跳过 `_*.yaml`)。把模板转正只需:

1. 改文件名去掉 `_` 前缀;
2. 调 `id` 字段(默认会 fallback 到 `path.stem`);
3. 重启服务。

> 已转正为活跃主题:`autumn_recruit.yaml` / `interview_prep.yaml` / `llm_frontier.yaml`。
