# Job Greet 约束级联设计

`job.greet` 采用“字面硬门 + trait 展开硬门 + LLM 语义评分 + 后置硬门”的级联链路，自动投递决策以显式用户约束优先，不以单次 LLM 综合评分覆盖硬指令。

```mermaid
flowchart LR
    A[scan candidates] --> B[pref literal gate<br/>avoid_company]
    B --> C[trait company gate<br/>TraitCompanyExpander + cache]
    C --> D[hard constraints gate<br/>location/salary/level]
    D --> E[dedup gate<br/>applied URLs]
    E --> F[LLM matcher<br/>score/verdict/hard_violations]
    F --> G[deterministic enforce<br/>hard_violations != [] => skip]
    G --> H[selected for greet]
```

## 分层职责

| 层 | 负责 | 不负责 |
|---|---|---|
| `JobMemory` (`job.*`) | 持久化用户偏好、硬约束、trait 展开缓存（`job.derived.*`） | LLM 判分、外部平台 IO |
| `TraitCompanyExpander` | 将 `avoid_trait` 展开为公司集合并缓存（TTL） | 直接发消息、评分排序 |
| `JobGreetService._filter_by_preferences` | 字面黑名单与 trait 公司集合的确定性 veto | 语义匹配打分 |
| `JobSnapshotMatcher` | 语义匹配评分，产出 `hard_violations` 结构字段 | 最终发送决策 |
| `JobGreetService._score_items` | 对 `hard_violations` 做确定性强制 skip | 解释用户偏好语义 |
| `JobPlatformConnector` | 扫岗/发送等平台交互 | 业务约束判断 |

## 第一性原理分析

| 维度 | 分析 | 结论 |
|---|---|---|
| 能力归属 | “字节是否大厂”依赖世界知识，规则表不可穷举；“若命中禁止则绝不投递”是确定性约束 | 语义展开交给 LLM，一旦得到结果由代码硬执行 |
| 奖励精度 | 单次 LLM 综合评分在强正向信号下会漏掉约束违例 | 约束判定与评分解耦，违例字段单独输出并后置强制门 |
| 故障域 | 外部 LLM 调用可能抖动，实时每条展开会放大故障 | 展开结果持久化缓存，优先复用缓存；无缓存且展开失败时 fail-loud |
| 可观测性 | “为何被跳过”需要可审计，不可只看最终 `good/skip` | 过滤层与 matcher 层都写结构化 skip reason / violations |
| 可逆性 | trait 粒度或市场结构会变化，写死表迁移成本高 | 缓存是派生态；撤销 trait 或 TTL 到期即可重算 |

## 接口契约

```text
TraitCompanyExpander.resolve_avoid_trait_companies(snapshot) -> dict[str, set[str]]
```

不变式：
- 输入只消费 active `avoid_trait`。
- 输出键是 trait 原文，值是公司名集合（字面匹配用）。
- 无缓存且 LLM 失败时抛出 `RuntimeError`，不返回伪空集合。

```text
JobMemory.get_trait_company_set(trait_type, trait) -> TraitCompanySet | None
JobMemory.set_trait_company_set(...) -> TraitCompanySet
```

不变式：
- `trait_type` 仅允许 `avoid_trait|favor_trait`。
- 存储键稳定映射到 `job.derived.trait_company_set:<sha1>`。
- `companies` 只做字面去重，不做语义别名扩展。

```text
JobSnapshotMatcher.match(job, snapshot, keyword) -> MatchResult
MatchResult = {score, verdict, matched_signals, concerns, hard_violations, reason}
```

不变式：
- `hard_violations` 表示“硬约束冲突证据”，与 `verdict` 分离。
- `JobGreetService` 对 `hard_violations` 非空执行强制 skip。

## 重评触发条件

1. `skip:avoid_trait_company_set` 与用户人工复核冲突率连续 7 天 > 5%。
2. `hard_violations` 为空但人工复核认定违例的案例累计 >= 5。
3. trait 展开缓存 miss 后的 LLM 失败率连续 24 小时 > 10%。
4. 单 trait 展开公司集合稳定超过 40 且误伤率上升。

