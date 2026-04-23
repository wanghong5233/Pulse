# Pulse Domain Memory 与 Tool 模式

> 定位：业务侧 **DomainMemory facade** 与 **Intent 级 Tool** 的架构规范。  
> 关联文档：`Pulse-MemoryRuntime设计.md`（§7 Layer×Scope、§8 Promotion）、`Pulse-内核架构总览.md`、`Pulse架构方案.md`。

---

## 配套阅读

内核定义「通用记忆机制」，本文档定义「业务如何落在唯一接合点上」。二者通过 **Workspace × workspace** 单元对接（见 `Pulse-MemoryRuntime设计.md` §7.4）。

| 内核章节 | 与本文档的关系 |
|----------|----------------|
| §7 Layer × Scope | DomainMemory 落点：Workspace 层 × workspace 作用域下的 **facts** |
| §8 Compaction / Promotion | 与 DomainMemory **写入路径** 的区分（§2.3） |

```
┌─────────────────────────────────────────────┐
│  Memory Runtime（内核）                      │
│  Layer×Scope / Envelope / Compaction /      │
│  Promotion / Hook                           │
│  ★ Workspace × workspace：通用 KV / summary  │
└────────────────────┬────────────────────────┘
                     │ 仅在此单元上建 facade
                     ▼
┌─────────────────────────────────────────────┐
│  本文档                                      │
│  DomainMemory · IntentSpec · Brain 写入路径  │
│  Snapshot · 多 LLM 调用点边界               │
└────────────────────┬────────────────────────┘
                     │
                     ▼
        JobMemory / MailMemory / …
```

**两条并存的写入路径**（不冲突）：

| 路径 | 数据来源 | 机制 | 落点 |
|------|----------|------|------|
| **DomainMemory** | 用户**显式陈述**的偏好与约束 | Brain → `tool_use` → 领域 facade | Workspace facts（如 `job.*`） |
| **Promotion** | 从对话/任务中**自动提取**的稳定事实 | Promotion Pipeline | Archival / Core（见内核 §8.3） |

---

## 1. 核心原则

### 1.1 「Profile」是视图，不是独立存储

求职等业务上的「画像」在存储上是一组 **带命名空间的 workspace 事实**（KV），与同一单元内的 summary、模块状态快照 **形态一致**，差别仅在 **key 前缀**（如 `job.*`、`mail.*`）。

- 领域层使用 **`JobMemory` 这类 facade** 表达语义；避免把「Profile」实现成独立存储类型或独立表（除非产品有单独强需求）。
- Facade **不持有存储**，构造时注入内核的 workspace 记忆接口，读写均委托给内核。

### 1.2 Memory 抽象与 Domain Facade

| 层级 | 职责 | 是否跨业务域 |
|------|------|----------------|
| 内核 workspace 接口 | 通用 KV / summary / 与 Prompt 的 essentials 契约 | 是 |
| `JobMemory` / `MailMemory` / … | 命名空间、领域方法、`snapshot` 渲染给 LLM | 否（每域一份） |

### 1.3 写入入口：自然语言统一进 Brain

偏好随对话变化，**静态前缀路由无法覆盖表达多样性**。架构约定：

- **用户可见输入统一进入 Task Runtime（Brain）**，由模型做意图与参数抽取。
- 持久化偏好通过 **`{domain}.{capability}.{action}` 形态的 Intent Tool** 写入 DomainMemory，而不是并行维护一套「规则管道」。

---

## 2. DomainMemory 在矩阵中的位置

### 2.1 Layer × Scope（摘录）

参照 `Pulse-MemoryRuntime设计.md` §7.4：

| Layer \ Scope | … | workspace |
|---------------|---|-----------|
| Workspace | … | **summary + facts（★）** |
| … | … | … |

**DomainMemory（以 Job 为例）主要操作 ★ 中的 facts 子集**（具体 key 约定由该域 facade 定义）。

**不是**：

- Operational：单次 run 内临时态，不承载「长期偏好」的主存。
- Core：跨 workspace 的用户级信息（身份、通用沟通偏好等）— 由 Core 路径管理，**可被 snapshot 合并展示**，但不属于 `job.*` 命名空间。
- Archival：经 Promotion 晋升的结构化长期事实；与「用户当场说的偏好」写入路径不同。

### 2.2 与 Core Memory 的关系

用户级、与具体业务 workspace 无关的字段（身份、通用风格、语言偏好等）落在 **Core**；业务域 snapshot 组装时可 **读取** Core 中相关切片，形成完整上下文，但 **不在业务 facade 内篡改 Core**。

```
Snapshot（面向 LLM）
├── workspace 侧 facts（如 job.*）
└── 可选：Core 侧只读切片（身份 / 通用偏好）
```

### 2.3 与 Promotion Pipeline 的关系

- **Promotion**：对话中**隐式**提炼的稳定事实，走检测 / 校验 / 审批 / 写入 Archival 或 Core（内核 §8）。
- **DomainMemory 写入**：用户**显式**意图经 Brain 解析后的 **tool 写入**，**不重复走 Promotion 的「再提取」**；仍应遵守 **evidence** 约定（可追溯本次写入对应的会话或 run）。

### 2.4 与 Operational（Task Memory）的区分

经 Brain 调用的领域写入工具，其效果是 **跨 run 持久** 的 workspace 事实，**不属于** Operational 层生命周期。Operational 只承载当前推理与工具链的中间态。

---

## 3. Workspace 侧存储契约（领域必须遵守）

- **不新增业务表**承载「某域画像」：与内核设计一致时，应复用 **workspace facts** 的通用模型。
- **值的编码**：与内核公开约定一致——持久层存 JSON，**面向 facade 的类型为解码后的结构化值**；避免同一列多种语义解读。
- **批量与前缀**：领域加载/重置/同步若需「整域替换」，依赖内核提供的 **按前缀列举、按前缀删除** 等能力，保证「全量替换」语义可实现。
- **reason / 审计**：若需原因说明，优先作为 **value 结构内的字段**（如 `reason`），与内核 Fact 模型对齐；避免与业务 payload 字段冲突。

### 3.1 三类存储形态

DomainMemory 内部按 **下游如何消费** 把 `{domain}.*` 命名空间再分三类。划分规则与内核无关，仅是领域 facade 对 key 空间的内部约定，**下游决定形态**，不是先验枚举。

| 形态 | 何时用 | Key 命名约定 | 典型消费者 |
|------|--------|--------------|-----------|
| **Hard Constraints**（结构化硬约束） | 下游 **Connector / 纯 IO 层** 能直接用作过滤表达式的字段；且字段集小、稳定 | 独立具名 key（如 `{domain}.preferred_location`） | 搜索 URL 构造、SQL `WHERE`、API filter 参数 |
| **Memory Items**（语义记忆池） | 下游由 **LLM** 读取并自由解释的偏好/事件/软约束；schema 开放，允许 LLM 自行决定 type | `{domain}.item:<uuid>`，value 为带 `type / target / content / raw_text / valid_until / superseded_by` 的结构 | 领域 LLM 组件（matcher / greeter / replier）注入 Prompt |
| **Domain Documents**（领域文档） | 领域内相对稳定、**整体替换式** 的长文本/半结构化文档（如简历、个人简介、项目库） | `{domain}.doc:<id>`（原文）+ `{domain}.doc:<id>.parsed`（LLM 解析缓存） | LLM 组件引用具体能力/事实 |

**设计信号**：

- 把「规整的结构化字段」推向 **Hard Constraints**——因为它直接参与 Connector 的 IO 过滤，必须稳定可计算。
- 把「说不尽的语言描述」留给 **Memory Items**——因为 LLM 能自如处理 schema 开放的偏好池，而预设 enum 一定追不上真实用户表达。
- 把「能独立成文的领域知识」分到 **Domain Documents**——因为它有自己的生命周期（用户显式更新 + 可能触发重解析），和单条偏好不是同一类实体。

> 边界判断：**「这个字段有没有下游 IO 想直接把它当 filter 用？」** 有 → Hard Constraints；没有 → Memory Items。

### 3.2 Memory Item 契约（领域可扩展）

每条 Memory Item 至少包含：

| 字段 | 含义 |
|------|------|
| `id` | 唯一 id（UUID 或等价） |
| `type` | 推荐 enum（如领域给的 `avoid_*` / `favor_*` / `event` / `claim` / `note`）；**允许 LLM 填枚举外的字符串，兜底 `other`** |
| `target` | 被谈论对象（公司/关键词/岗位/…）或 `null` |
| `content` | 一句话归纳，**直接喂下游 Prompt 的可读文本** |
| `raw_text` | 用户原话（审计与 fallback） |
| `valid_from / valid_until` | 生效区间；`valid_until` 由 Brain 在写入时估算（如「暂时」→ 若干天） |
| `superseded_by` | 被新 item 取代时指向新 id，表达偏好演化 |

**写入**：Brain 在解析用户输入后，调用领域 `{domain}.memory.record` Intent Tool 传入结构化 item（LLM 负责按 schema 归一化）。
**读取**：Snapshot 渲染时按 `type` 分组列出；下游 LLM 组件在 Prompt 中看到分组后的 markdown，不再自己做 `target` 子串匹配。

### 3.3 是否对偏好做语义检索

偏好规模可控时，**默认全量 snapshot 注入 Prompt** 即可。若单 workspace 的 Memory Items 极多，再按 `type` / `valid_until` / 时间窗做截断、或另加分页 list tool；**Archival 向量检索**服务于另一类「量大、模糊命中」的长期事实，与当前「活跃偏好列表」主路径分离。

---

## 4. Snapshot 与 Prompt 刷新策略

### 4.1 Snapshot 结构

DomainMemory 的 snapshot 按 §3.1 的三类存储形态**分节渲染**，领域内任意 LLM 组件看到的都是同一份结构：

```
## {Domain} Snapshot
### Hard Constraints
- <field>: <value>
- ...

### Memory Items — {type_group_1}
- [target] content — (valid until …)
- ...
### Memory Items — {type_group_2}
- ...

### Domain Documents
#### {doc_id}
<LLM 解析后的 summary，必要时附关键字段>
```

- **Hard Constraints** 总是渲染（即便为空，留位提示 LLM 去问用户）。
- **Memory Items** 按 `type` 分组，`valid_until` 已过期的自动滤掉；可依软上限截断并提示「(N 条已省略，调 `{domain}.memory.list` 查全量)」。
- **Domain Documents** 按文档 id 展开；渲染摘要而非原文，避免 Prompt 膨胀；LLM 可按需调 `{domain}.doc.get` 拿完整内容。

### 4.2 刷新时机

| 时机 | 建议 |
|------|------|
| 一轮 ReAct 开始前 | 注入当前 workspace（及可选 Core 切片）的 **baseline snapshot** |
| 调用 **mutates** 类 Intent Tool 之后 | 下一轮组装 Prompt 时 **刷新 snapshot**，避免模型看到陈旧偏好 |
| 纯读取类 tool | 可不因单次读取强制刷新（以性能与一致性权衡为准） |
| 新的用户消息进入 | 可按需刷新，使多轮对话中偏好可见 |

**mutates** 由 Intent 元数据声明，供 Task Runtime 在 tool 循环中决定是否触发刷新（与内核 Hook 可组合）。

### 4.3 Snapshot 版本

`snapshot_version` 建议覆盖三类存储的最大更新时间（或 hash），使下游缓存键（如 matcher 对「同一 JD + 同一 snapshot 版本」复用评分）在任何一类发生变化时都会自动失效。

---

## 5. 输入路由与「命令」形态

- **架构目标**：单一认知路径——文本（自然语言或极少系统说法）由 Brain 理解，**不并行维护业务级静态路由表**。
- 若产品仍需「看起来像命令」的输入，由 **system 约定 + Intent** 处理（例如帮助、取消当前任务），仍经 Brain，不拆第二套路由引擎。

---

## 6. 写入路径：Brain → Intent Tool → DomainMemory

### 6.1 数据流（概念）

```
用户消息
    → Brain（选 tool、抽参）
    → 领域模块 handler（结构化参数）
    → DomainMemory facade
    → 内核 Workspace（facts）
```

### 6.2 Intent 规格（概念）

每个 Intent 对外暴露为 **独立 tool**（含名称、说明、**参数 JSON Schema**、是否 **mutates**、可选 **风险等级 / 是否需确认**）。  
注册表将各模块的 Intent 列表展开为模型可见的 tool 列表，使 Brain 能进行 **结构化参数填充**，而不是向单一大工具塞无模式文本。

**设计要点**：

- **mutates**：声明是否会改记忆，用于 snapshot 刷新与治理。
- **描述与示例**：面向模型，减少意图误判；敏感操作可通过 **确认策略** 对齐未来的 SafetyPlane（⏳ 规划中，届时经 Observability Plane 订阅 `policy.*` 事件）。

### 6.3 三类存储对应的 Intent Tool 形态

| 存储形态 | 推荐 Intent Tool | 参数风格 |
|---------|------------------|---------|
| **Hard Constraints** | `{domain}.hard_constraint.set(field, value)` / `.unset(field)` | `field` 为封闭 enum（该域提前声明的字段集），`value` 按字段类型强校验 |
| **Memory Items** | `{domain}.memory.record(item)` / `.retire(id)` / `.supersede(id, new_item)` | `item` schema 与 §3.2 一致；`type` 推荐 enum 但不强制；由 Brain 在写入前用 LLM 组好完整对象 |
| **Domain Documents** | `{domain}.doc.update(id, raw_text)` / `.patch_parsed(id, patch)` | 整体替换原文 + 可选结构化补丁 |

**一条用户陈述可能产出多次 tool call**：例如「投递过 X，简历被锁，暂时不投 X」同时涉及事件记录与回避偏好，Brain 会在同一轮 ReAct 里连续调 `memory.record` 两次（一条 `type=event`，一条 `type=avoid_company`）。这是允许的，架构上不做「强合并」假设。

#### 6.3.1 系统控制类 Intent Tool（与 DomainMemory 写入正交）

除了领域 memory 写入，`IntentSpec` 另一条合法用途是**让 Brain 控制 Kernel 自身的状态**——它们既不属于某个业务域的 memory，也不产生跨进程副作用，而是直接调同进程 `AgentRuntime` / `WorkspaceMemory` / `RecallMemory` API。

| 系统域 | 典型 Tool | 副作用 | 代表模块 |
|-------|----------|-------|---------|
| **Memory 控制面** | `memory.search(query)` / `memory.pin(id)` / `memory.forget(id)` | 改 Recall/Archival 位 | `src/pulse/core/memory/memory_tools.py` |
| **Runtime 控制面** | `system.patrol.list` / `.status(name)` / `.enable(name)` / `.disable(name)` / `.trigger(name)` | 改 `ScheduleTask.enabled`、触发一次 `_execute_patrol` | `src/pulse/modules/system/patrol/module.py` (ADR-004 §6.1, ✅ 2026-04-22) |

**共同特征**:
- 都是 in-process（handler 直接拿到内核对象的引用），**不**走 MCP
- 都要对 ADR-001 契约 A 逐字段表态（`when_to_use` / `when_not_to_use` 区分与邻居工具边界）
- 高风险动作（enable/trigger 会真跑 handler → 真点按钮 / 真发简历）走 `requires_confirmation=True`
- HTTP 对等面共存（`/api/runtime/patrols/*` / `/api/memory/*`）——同语义两个出口，CLI / ops 走 HTTP，Brain 走 IntentSpec

**为什么独立成一类**:业务域 memory 写入的不变量是"写入即领域 fact",而系统控制 tool 的不变量是"执行即修改 kernel 状态"。两者的 verifier 规则、审计落点、HITL 阈值都不同(ADR-004 §6.1.4 风险矩阵),混在同一类讨论会掩盖边界。

### 6.3 人机协同（HITL）边界（原则）

| 情形 | 倾向 |
|------|------|
| 用户已明确说出约束 | 一般无需额外交互；模型应给出可验证的简短回执 |
| 模型从弱上下文**推断**可能伤人的写入 | 倾向 **确认** 或 **高风险门控** |
| 批量或不可逆操作 | 倾向 **确认** |
| 解除限制（unblock 等） | 按风险等级配置 |

具体门控与 Hook、Policy 的对接见内核 §5、§6。

---

## 7. 读取路径：多 LLM 调用点与 Snapshot

业务域内可有 **多个** 需要模型的组件（例如：路由与工具选择、匹配、生成、分类、回复）。架构约束：

| 角色 | 职责 |
|------|------|
| **Brain** | 通用自然语言理解、Intent 选择、参数抽取；不替代领域内聚的匹配/生成逻辑 |
| **领域组件** | 单职责：如 JD↔偏好匹配、招呼文案、HR 消息规划、回复生成等 |
| **Repository 等纯 IO 层** | **不调 LLM** |

**反模式**：

- 在 service 层散落大段 prompt，而不收敛到命名清晰的领域 LLM 组件。
- 用 Brain 做本应由领域模型批量的细粒度打分（或相反，用领域组件做全局意图路由）。

领域组件读取偏好时，优先依赖 **同构的 MemorySnapshot**（或等价结构化视图），并可用 **snapshot 版本** 参与缓存键设计（例如「同一 JD + 同一 snapshot 版本」可复用中间结果）。

---

## 8. 其他 Domain 复用步骤

新增 `mail` / `health` 等域时：

1. 定义 **`{domain}.*` 命名空间** 与 `XxxMemory` facade。
2. 在模块中声明 **IntentSpec 列表**，由注册表展开为 tools。
3. 按需增加 **领域 LLM 组件**（摘要、回复、分类等），边界遵守 §7。
4. 在 Prompt 契约中 **挂载该域 snapshot**（或按 active domain 动态挂载）。

**不要求**：为「画像」单独建表、修改内核 Layer 定义、或复制一套 Brain 主循环。

---

## 9. 待决设计（保留问题，不绑定实现）

### 9.1 多 workspace 下的「当前 workspace」

请求入口（渠道绑定）、显式上下文标记、或用户级默认 workspace 等方案可择一或组合；需在产品与多租户模型确定后收口。

### 9.2 是否默认跨域加载 snapshot

默认 **仅挂载当前活动域**；跨域需求通过 **额外 tool** 按需拉取，避免 Prompt 膨胀与权限模糊。

### 9.3 Snapshot 体积

事实条目过多时，采用 **截断 + 摘要 + list tool** 等策略；再考虑更强检索，而不是默认上向量检索。

### 9.4 偏好变更与历史

Workspace 层可 **直接覆盖/删除** 表达「当前生效偏好」；长期版本链走 Archival 的 `superseded_by / valid_from / valid_to`（见 `Pulse-MemoryRuntime设计.md` §8.5），系统级审计走 Observability Plane 的 `memory.*` 事件流（见 `Pulse-内核架构总览.md` §6），避免在 workspace 重复造时序库。

### 9.5 主动询问偏好

属于产品层行为（由 Prompt 与策略引导），不改变 §1–§7 的存储与工具边界。

---

## 10. 与其他文档的关系

| 文档 | 关系 |
|------|------|
| `Pulse-MemoryRuntime设计.md` | DomainMemory 所在单元与 Promotion 语义 |
| `Pulse-内核架构总览.md` | 三层运行时与 Memory 读写关系 |
| `Pulse架构方案.md` | 全局架构 |
| `modules/job/architecture.md` | Job 工程蓝图可与本文档「Job 为例」章节交叉引用 |

---

## 附录：术语

| 术语 | 含义 |
|------|------|
| DomainMemory | 某业务域在 Workspace × workspace 上的 facade |
| MemorySnapshot | 渲染给模型用的结构化快照（常含 workspace facts + 可选 Core 切片） |
| Intent Tool / IntentSpec | 暴露给 Brain 的细粒度工具及其元数据（含 schema、mutates 等） |
| Mutation Tool | 会修改 DomainMemory / workspace facts 的 Intent Tool |
