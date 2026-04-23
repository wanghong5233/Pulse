# Pulse 剩余验收关口清单

> 基准文档：`docs/Pulse架构方案.md`
> 形成时间：2026-03-29
> 目的：只保留真正影响“进入下一阶段”的少数关口，避免继续被历史清单和已关闭问题干扰。

---

## 0. 当前结论

- 当前代码已经具备 `Brain + Tool Registry + MCP Client + PG+Chroma Memory + Governance + Evolution + Skill Generator` 的核心骨架。
- 最近一轮真实 `PostgreSQL + Chroma` 回归已打通，相关集成/回归测试通过。
- 但是，**仍不能严格确认“已完全按架构方案收尾”**。
- 主要原因不是主干不可用，而是仍有少数 **会污染真实测试结论** 或 **与架构验收口径直接冲突** 的缺口尚未关闭。

---

## 1. 验收口径

### A. 可以开始“受控生产环境测试”

满足以下条件即可进入下一阶段：

- `G1` 完成
- 真实环境变量、凭据、外部依赖接入完成
- 明确规定：一旦进入 fallback/degraded/mock 路径，测试直接判失败

### B. 可以确认“本轮架构重构完成”

只有当 `G1-G4` 全部完成，才可以对外或对自己确认：

- Pulse 重构已按当前架构方案收口
- 可以把重点切到新增功能而不是继续补架构缺口

---

## 2. 剩余关口

## G1 关闭会污染真实结果的招聘 fallback

### 当前状态（2026-03-29 已关闭）

- `boss_greet` 默认不再从“未配置真实连接器”自动退化到 `web_search`；未就绪时返回明确失败源 `boss_unconfigured`。
- `boss_local_seed`、`boss_chat_inbox.jsonl`、MCP runtime 的 `browser_first/local_only/web_search_only` 等非真实路径，现已改为**显式 opt-in**；默认关闭。
- 测试基线也已同步到“严格默认值”，只有对应测试显式打开开关时，才允许进入本地联调路径。

### 关键位置

- `src/pulse/integrations/boss/connector.py`
- `src/pulse/modules/boss_chat/module.py`
- `backend/mcp_servers/_boss_platform_runtime.py`

### 完成标准

- 默认配置下，BOSS 相关链路只能使用真实平台连接器，不再使用种子职位或本地 inbox。
- 当真实连接器未就绪时，返回明确失败，而不是继续给出可执行结果。
- 所有“仅联调/本地调试”路径必须显式受环境变量控制，且默认关闭。

### 出关后意义

- 这是进入受控生产环境测试的首要前提。
- 不完成这一项，任何招聘/BOSS 测试结论都不可信。

---

## G2 完成标准 MCP Server 对外暴露

### 当前状态（2026-03-29 已关闭）

- 当前主进程已新增标准 MCP HTTP 端点 `/mcp`，支持标准 JSON-RPC `initialize / notifications/initialized / tools/list / tools/call`。
- `src/pulse/core/mcp_server.py` 已补齐可直接运行的 stdio 启动入口：`python -m pulse.core.mcp_server`。
- `server.py` 中多 transport 装配已修正，`streamable_http / http_sse / stdio` 配置都会正确实例化到 MCP Client 多 transport 图中。
- 原有 `/api/mcp/tools`、`/api/mcp/call` 继续保留为兼容 REST 包装层，但不再是唯一的 MCP 对外暴露方式。

### 关键位置

- `src/pulse/core/server.py`
- `src/pulse/core/mcp_server.py`
- `src/pulse/core/mcp_servers_config.py`

### 完成标准

- `Pulse` 可以被标准 MCP Client 作为 MCP Server 连接，而不是只能调用 REST 包装接口。
- stdio 启动入口可直接运行。
- HTTP 端支持文档要求的标准 MCP 暴露方式，或明确限定仅支持 stdio 并同步修正文档。
- `streamable_http` / `http_sse` / `stdio` 的配置行为与代码一致。

### 出关后意义

- 这是“架构方案 Phase 3 MCP 兼容性”收尾项。
- 不完成这一项，不影响继续开发一般功能，但影响“已按方案完成 MCP 双角色”的结论。

---

## G3 补齐 Event Bus / 可观测性验收面

### 当前状态（2026-03-29 已关闭）

- 已补齐面向客户端的 SSE 事件流出口：`/api/system/events/stream`，支持 replay 和有限事件消费。
- 已补齐事件导出能力：`/api/system/events/export`，可导出 JSON / JSONL 快照。
- 已在 `module` 基类和关键招聘模块中补入统一阶段事件：至少覆盖 `boss_greet.scan/trigger`、`boss_chat.inbox_load/ingest/process/pull/execute`，并补了通用 `module.intent.*` 事件。
- 当前事件留存策略为 **in-memory + export**，不是进程外持久化；但已满足“生产测试期可订阅、可导出、可回放最近事件”的验收需要。

### 关键位置

- `src/pulse/core/events.py`
- `src/pulse/core/server.py`
- `src/pulse/modules/*`

### 完成标准

- 至少补齐一个面向客户端的 SSE 事件流出口，或正式下调文档预期。
- 关键 Pipeline 阶段具备统一事件上报，而不是只有入口和 Brain 级事件。
- 明确事件留存策略：若仍为内存态，文档中必须降级声明；若按架构验收，则需补持久化或可导出机制。

### 出关后意义

- 不完成这一项，不利于正式生产测试和问题定位。
- 这是进入“生产可观测验证”前的关键质量门槛。

---

## G4 收拢架构与代码事实不一致项

### 当前状态

- `src/pulse/core/recruitment/` 已下沉到 `src/pulse/integrations/boss/`，`core/` 不再承载 BOSS 业务连接器。
- `feedback_loop` 已从“只写 corrections”收口为“写审计记录 + 触发 Evolution Engine 反思”，HTTP / intent 路径都会进入偏好学习闭环。
- `intel_interview` / `intel_techradar` 已将对外 `source` / `mode` 语义收紧为真实实现的 `web_search` pipeline；保留 `real -> web_search` 兼容别名，并已在架构文档中区分“当前实现”和“目标态”。

### 关键位置

- `src/pulse/integrations/boss/`
- `src/pulse/modules/feedback_loop/module.py`
- `src/pulse/modules/intel_interview/module.py`
- `src/pulse/modules/intel_techradar/module.py`
- `docs/Pulse架构方案.md`

### 完成标准

- 本轮按“代码 + 文档双收口”完成：
- 业务连接器迁出 `core/`；
- `feedback_loop` 接入真实偏好学习闭环；
- 情报模块对外语义与架构文档统一为“当前实现 / 目标态”双视角表述。

### 出关后意义

- 这一项更偏“架构验收一致性”，不是最先阻止受控生产测试的项。
- 但如果不处理，就不能说“当前实现已经与方案完全对齐”。

---

## 3. 建议执行顺序

1. `G1` 已完成：招聘链路默认已切到严格真实源模式。
2. `G3` 已完成：事件总线已具备 SSE、replay、export 和关键阶段事件。
3. `G2` 已完成：MCP 双角色能力已具备标准 HTTP + stdio 暴露面。
4. `G4` 已完成：架构文档与代码事实已经收拢，不再保留误导性的强表述。

---

## 4. 阶段建议

### 现在就可以做的事

- 可以开始下一阶段的受控生产环境测试：梳理真实环境变量、真实账号、真实外部依赖、测试 SOP，并按严格模式执行。
- 可以并行开发新的**领域无关能力**，例如新的 Tool、Memory、Governance、Channel、MCP 生态接入。
- 可以在生产测试中直接消费 `/api/system/events/stream` 和 `/api/system/events/export`，作为问题定位入口。

### 现在不建议直接宣告的事

- 不建议在未明确区分“严格模式”和“本地联调模式”时，把任何带 opt-in fallback 的演示结果当成正式生产结论。
- 不建议把文档中的“目标态采集器”（如面经站点级抓取、RSS / GitHub / 公众号）当成当前已经交付的实现能力。

---

## 5. 最终判断

- **是否可以开始下一阶段的受控生产环境测试：可以。`G1` 已完成，接下来应严格按真实依赖运行，并把任何 fallback 命中直接判失败。**
- **是否可以确认本轮“架构重构对齐收口”已经完成：可以。`G1-G4` 已全部关闭，当前代码事实、能力边界和验收文档已经一致。**
- **是否可以开始新增功能：可以，尤其是领域无关新能力；但不应再让新增功能继续建立在现有 fallback/文档不一致项之上。**
