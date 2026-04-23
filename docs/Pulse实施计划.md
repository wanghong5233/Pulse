# Pulse 分阶段实施计划

> 本文档只定义里程碑节点和验收条件。每个里程碑的具体实施方案在进入该阶段时再详细设计。  
> 说明：`M0-M7` 标记完成，表示 **架构重构与最小可运行闭环完成**，不等于 **完整产品化已全部收尾**。  
> 当前剩余产品化差距见：`docs/Pulse最终收尾清单.md`。

---

## 里程碑总览

```
M0 项目骨架
 │
M1 Capability 层抽取
 │
M2 Module 迁移 + Phase 1 功能验证
 │
M3 接入层 + Phase 2 新功能
 │
M4 Brain + 三环能力模型 (Phase 3)
 │
M5 记忆系统 (Phase 3)
 │
M6 Skill Generator (Phase 4)
 │
M7 进化引擎 (Phase 5)
```

每个里程碑产出一个**可运行的系统**，不存在中间破损状态。

---

## M0：项目骨架

**目标**：建立新项目结构，空壳可启动。

**交付物**：
- `pyproject.toml` + `src/pulse/` 标准 Python 包结构
- `core/module.py`：BaseModule 抽象 + ModuleRegistry 自动发现
- `core/config.py`：Pydantic Settings 统一配置
- `core/events.py`：EventBus 进程内事件
- `core/server.py`：FastAPI 工厂，启动时扫描注册 Module
- `cli.py` + `__main__.py`：`pulse start` 入口

**验收条件**：
- `pip install -e .` 成功
- `pulse start` 启动 FastAPI 空壳，健康检查 200
- 放一个 `modules/hello/` 示例 Module，启动后自动注册路由

**依赖**：无（干净起点）

---

## M1：Capability 层抽取

**目标**：从 V1 代码中抽取共享基础设施到 `core/`，每个 Capability 独立可测。

**交付物**：
- `core/browser/`：Browser Pool（从 `boss_scan.py` 抽取浏览器管理、Cookie 检测、会话复用）
- `core/llm/`：LLM Router + Structured Output（从 `workflow.py` 抽取）
- `core/storage/`：PostgreSQL 连接池 + ChromaDB 向量存储（从 `storage.py` + `vector_store.py` 抽取）
- `core/notify/`：通知抽象（从 `email_notify.py` 中飞书通知部分抽取）
- `core/scheduler/`：时段感知调度器（从 `production_guard.py` 抽取）

**验收条件**：
- 每个 Capability 有独立的单元测试
- Capability 之间无循环依赖
- `core/` 中不出现任何业务术语（boss、求职、简历等）

**依赖**：M0

**关键约束**：
- 此阶段只抽取，不改业务逻辑
- V1 旧代码此时仍保留，尚未删除

---

## M2：Module 迁移 + Phase 1 功能验证

**目标**：将 Phase 1 的三个核心业务迁入 Module 架构，删除垃圾代码，新架构上跑通全部 Phase 1 功能。

**交付物**：
- `modules/boss_greet/`：岗位扫描 + JD 分析 + 主动打招呼（从 `boss_scan.py` + `boss_workflow.py`）
- `modules/boss_chat/`：聊天巡检 + 智能回复（从 `boss_chat_workflow.py` + `boss_chat_service.py`）
- `modules/email_tracker/`：邮件追踪（从 `email_*.py`）
- 删除：`material_*.py`、`form_*.py`、`interview_prep_service.py`、`company_intel_service.py`、截屏逻辑、`exports/` 目录
- 删除 V1 `backend/app/` 旧结构

**验收条件**：
- boss_greet：定时扫描 → JD 解析 → 打招呼 → 飞书通知，全流程跑通
- boss_chat：巡检拉消息 → 意图识别 → 四层门控 → 自动回复，全流程跑通
- email_tracker：IMAP 拉取 → LLM 分类 → 状态更新 → 飞书通知，全流程跑通
- V1 旧代码完全删除，项目中不存在 `backend/app/` 目录

**依赖**：M1

**关键约束**：
- 迁移顺序建议：boss_greet → boss_chat → email_tracker（前两个共享 Browser Pool，验证 Capability 的正确性）
- 迁完一个模块，立即端到端验证，再迁下一个，避免批量迁移导致 debug 困难
- 重型框架清理采用两步：先在 Phase 1 运行链路中去除 `LangGraph/OpenClaw` 运行依赖（仅保留兼容层），再在 M2 收尾统一删除旧文件和相关依赖项，避免中途回归

**M2.5 收尾（进入 M3 前）**：
- 增加 Phase 1 运行模式开关：`PULSE_PHASE1_RUNTIME_MODE=mock|real`
- `real` 模式通过 `PULSE_PHASE1_REAL_BASE_URL` 桥接真实 Phase1 服务，避免在本仓库重新引入 V1 代码
- 前端下线已删除能力入口（`material/form/intel/prep`），保留模块化入口（`/api/modules/*`）
- `browser_use_server` 默认不再落盘截图到 `exports/`，仅在显式开关下写盘

---

## M3：接入层 + Phase 2 新功能

**目标**：完成 Phase 2 架构全部功能。

**交付物**：
- `core/channel/`：消息接入抽象 + 飞书实现 + CLI 实现
- `core/router.py`：意图路由（精确匹配 → 前缀匹配 → LLM 兜底）
- `core/policy.py`：Policy Engine（safe / confirm / blocked）
- `modules/intel_interview/`：面经情报采集 + 日报推送
- `modules/intel_techradar/`：技术雷达采集 + 日报推送
- `modules/intel_query/`：情报语义检索
- Dockerfile + docker-compose.yml：一键部署

**验收条件**：
- 通过飞书/CLI 发消息 → Router 正确路由到对应 Module
- 面经情报 + 技术雷达定时采集 + 飞书推送正常
- 语义检索返回相关结果
- `docker compose up` 一键启动

**依赖**：M2

**M3 当前进度（持续更新）**：
- [x] `core/channel/` 基础抽象完成，CLI/飞书 Adapter 完成
- [x] 新增真实接入入口：`/api/channel/cli/ingest`、`/api/channel/feishu/events`
- [x] `core/router.py` 已落地（exact/prefix/LLM fallback）
- [x] `core/router` 配置化：支持 `config/router_rules.json` + `PULSE_ROUTER_RULES_PATH`
- [x] `core/policy.py` 已落地（safe/confirm/blocked）
- [x] `core/policy` 配置化：支持 `config/policy_rules.json` + 环境变量覆盖
- [x] `modules/intel_interview/` 已完成（collect/report/push + channel intent + schedule start/stop/status/trigger）
- [x] `modules/intel_techradar/` 已完成（collect/report/push + channel intent + schedule start/stop/status/trigger）
- [x] `modules/intel_query/` 已完成语义检索版本（向量召回 + category 过滤 + channel intent）
- [x] 新增根目录 `Dockerfile` + `docker-compose.yml`（可直接启动 `pulse-api`）
- [x] M3 验收闭环：channel 路由分发、情报定时触发与推送、语义检索、`docker compose up` 均可运行

---

## M4：Brain + 三环能力模型（Phase 3 核心）

**目标**：Router 升级为 Brain，实现 ReAct 多步推理，建立三环能力体系。

**交付物**：
- `core/brain.py`：ReAct 推理循环（Think → Act → Observe → ...）
- `core/tool.py`：Tool Registry + `@tool` 装饰器
- `tools/`：首批内置轻量工具（alarm、weather、web_search 等）
- `core/mcp_client.py`：MCP Client，连接外部 MCP Server
- `core/mcp_server.py`：将内置 Tool 暴露为 MCP Server
- `core/cost.py`：LLM 成本控制（日消费上限 + 自动降级）

**验收条件**：
- Brain 能串联多个 Tool 完成组合任务（如：查天气 → 查航班 → 建日程）
- 已有 Module 通过 `module_registry.as_tools()` 作为 Ring 2 被 Brain 调度
- 外部 MCP Server 工具可被发现并调用
- Pulse 自身作为 MCP Server 可被外部客户端连接

**依赖**：M3

**M4 当前进度（持续更新）**：
- [x] 新增 `core/tool.py`：`ToolRegistry` + `@tool` 装饰器
- [x] 新增 `tools/` 内置工具：`alarm.create`、`weather.current`、`flight.search`、`web.search`
- [x] 新增 `core/brain.py`：支持多步 ReAct（可串联多工具后统一总结）
- [x] `module_registry.as_tools()` 已实现，Module 以 Ring2 Tool 形式注册
- [x] 新增 `core/mcp_client.py`、`core/mcp_server.py`、`core/mcp_transport_http.py`，支持本地 MCP 与 HTTP 外部 MCP 发现/调用
- [x] 新增 `core/cost.py`，支持日预算估算与消费控制
- [x] 新增 API：`/api/brain/tools`、`/api/brain/run`、`/api/brain/cost/status`、`/api/mcp/tools`、`/api/mcp/call`
- [x] Brain 多步组合任务（>=2 工具链路）与外部 MCP 接入能力已落地（真实外部服务联调可按环境继续）
- [x] M4 验收闭环：多工具推理、Ring2 Module 调度、MCP 本地/外部 list+call、成本控制均已可运行

---

## M5：记忆系统（Phase 3 完成）

**目标**：为 Brain 增加跨会话记忆，实现"有状态的 Pulse"。

**交付物**：
- `core/memory/core_memory.py`：Core Memory（SOUL / USER / PREFS / CONTEXT Block）
- `core/memory/recall_memory.py`：Recall Memory（对话历史 + 工具调用记录，时间 + 语义双索引）
- `core/memory/memory_tools.py`：记忆工具（memory_read / memory_update / memory_search）
- Brain 推理循环集成记忆加载与写入
- `config/soul.yaml`：初始人格配置

**验收条件**：
- Brain 推理时自动加载 Core Memory 注入 system prompt
- 用户偏好跨会话保持（如"我不喜欢游戏公司" → 下次自动过滤）
- `memory_search` 能检索到历史对话中的相关信息
- SOUL 人格风格在对话中一致体现

**依赖**：M4

**M5 当前进度（持续更新）**：
- [x] 新增 `core/memory/core_memory.py`：支持 SOUL/USER/PREFS/CONTEXT 持久化与更新
- [x] 新增 `core/memory/recall_memory.py`：支持对话时间线写入 + 语义检索（向量召回）
- [x] 新增 `core/memory/memory_tools.py`：`memory_read` / `memory_update` / `memory_search`
- [x] Brain 已集成记忆读写：运行前加载 core+recall，上下文注入规划，结束后写回 recall
- [x] 新增 `config/soul.yaml` 并在 Brain 回复中注入 SOUL 风格前缀
- [x] 新增 Memory API：`/api/memory/core`、`/api/memory/core/update`、`/api/memory/recall/recent`、`/api/memory/search`
- [x] M5 验收闭环：system prompt 注入、偏好跨会话、memory_search、SOUL 风格均可运行

---

## M6：Skill Generator（Phase 4）

**目标**：用户自然语言描述需求 → 系统自动生成新 Tool/Module 并热加载。

**交付物**：
- `core/skill_generator.py`：Meta-Tool（需求分析 → 代码生成 → 安全检查 → 热加载）
- `core/sandbox.py`：代码沙箱（AST 扫描 + import 白名单 + subprocess 隔离）
- `generated/`：自动生成代码存放目录
- Policy Engine 扩展：生成代码需用户确认激活

**验收条件**：
- "我需要监控 BTC 价格" → 自动生成 `btc_monitor` Tool → 热加载 → 立即可用
- 恶意代码（如 `os.system`、`import subprocess`）被 AST 扫描拦截
- 沙箱超时和内存限制生效

**依赖**：M5

**M6 当前进度（持续更新）**：
- [x] 新增 `core/skill_generator.py`：完成最小闭环（需求描述 -> 代码生成 -> 沙箱扫描 -> 记录索引 -> 热加载注册）
- [x] 新增 `core/sandbox.py`：支持 AST 拦截（`import os`、`subprocess`、`os.system` 等），并增加源码大小/AST 节点上限与 subprocess 超时检查
- [x] 新增 `generated/` 目录（含 `generated/skills/`）用于生成技能代码与索引持久化
- [x] `server` 新增技能 API：`/api/skills/list`、`/api/skills/generate`、`/api/skills/activate`
- [x] Policy Engine 扩展落地：`skill.activate` 默认 `confirm`，必须显式确认后才激活生成技能
- [x] 验收闭环通过：BTC 监控技能可自动生成并立即可通过 Tool/MCP 调用；恶意代码可被拦截

---

## M7：进化引擎（Phase 5）

**目标**：实现人格进化、偏好学习、时序事实图，Pulse 越用越聪明。

**交付物**：
- `core/soul/evolution.py`：SOUL Block 反思 Pipeline（经验分级 → 反思 → 治理门控）
- `core/soul/governance.py`：CORE / MUTABLE 治理（Autonomous / Supervised / Gated）
- `core/learning/preference_extractor.py`：Track A 偏好学习（纠正检测 → 规则提取）
- `core/learning/dpo_collector.py`：Track B DPO 训练对收集（可选）
- `core/memory/archival_memory.py`：时序事实图（PostgreSQL facts 表）

**验收条件**：
- 用户纠正 → 自动提取偏好规则 → 后续行为改变
- MUTABLE 信念可通过反思 Pipeline 进化，CORE 信念不可修改
- 变更有审计日志，可追溯、可回滚

**依赖**：M6

**M7 当前进度（持续更新）**：
- [x] 新增 `core/soul/evolution.py`：实现最小反思闭环（分类 -> 偏好提取 -> 治理执行 -> 归档写入）
- [x] 新增 `core/soul/governance.py`：支持变更审计、CORE 键保护、MUTABLE 更新、变更回滚
- [x] 治理分级已落地：`Autonomous / Supervised / Gated`，支持待审批变更与审批执行
- [x] 新增 `core/learning/preference_extractor.py`：从自然语言中提取偏好/风格更新信号
- [x] 新增 `core/memory/archival_memory.py`：时序事实归档（append-only JSONL）与查询接口
- [x] Brain 已接入进化引擎：每轮交互后自动触发反思与治理
- [x] 新增 Evolution API：`/api/evolution/status`、`/api/evolution/audits`、`/api/evolution/reflect`、`/api/evolution/rollback`
- [x] 新增治理 API：`/api/evolution/governance/mode`、`/api/evolution/governance/approve`
- [x] 新增 Archival API：`/api/memory/archival/recent`、`/api/memory/archival/query`
- [x] 新增 DPO 采样闭环：`core/learning/dpo_collector.py` + `/api/learning/dpo/status|recent|collect` + 纠正场景自动采样
- [x] 新增规则化治理配置：`config/evolution_rules.json`（按变更类型 + 风险等级映射治理模式）
- [x] 新增审计统计视图：`/api/evolution/audits/stats`（按状态/类型/模式/风险聚合）
- [x] 新增治理规则热更新：`/api/evolution/governance/reload`（无需重启即可重载规则文件）
- [x] 新增审计导出接口：`/api/evolution/audits/export`（支持 `json/csv`）
- [x] 新增管理观测接口：`/api/evolution/dashboard`（治理状态 + 审计趋势 + 记忆规模）
- [x] 新增治理规则版本化：`governance_rules_versions` 持久化版本历史，支持按版本回滚
- [x] 新增版本 API：`/api/evolution/governance/versions`、`/api/evolution/governance/versions/rollback`
- [x] 审计导出增强：支持 `start_at/end_at` 时间范围过滤 + `cursor` 游标分页
- [x] dashboard 增强：新增 `hourly/daily` 时间序列趋势
- [x] 新增版本差异对比：`/api/evolution/governance/versions/diff`（规则快照差异）
- [x] 新增 dashboard 异常告警：`pending/gated/rejected` 激增检测
- [x] 治理配置变更支持持久化写回规则文件，避免重启丢失
- [x] 新增发布前检查文档：`docs/Pulse发布前检查清单.md`（API/配置/回滚演练/上线验收）
- [x] 验收闭环通过：用户纠正可触发偏好更新并影响后续行为；CORE 关键人格字段不可改；审计可追溯且支持回滚
- [x] **M7 结项状态：完成（功能实现 + 回归通过 + 发布前检查清单就绪）**

---

## M8：ToolUseContract（推理 ↔ 动作一致性加固）

设计依据：[ADR-001-ToolUseContract](./adr/ADR-001-ToolUseContract.md)、`Pulse-内核架构总览.md` §7。

**目标**：消除「LLM 口头承诺动作但未下发 tool_call」类幻觉，以三条正交契约取代 host 侧关键词守卫。

**分期**：

| 子里程碑 | 范围 | 验收 |
|---|---|---|
| **M8.A DescriptionContract** | `ToolSpec.when_to_use / when_not_to_use` 字段、PromptContract 三段式渲染、反例 few-shot、Ring1/Ring2 工具补齐 | Ring1/Ring2 全量工具声明 `when_to_use`; `_section_tools` 三段式单测覆盖 |
| **M8.B CallContract** | `LLMRouter.invoke_chat(tool_choice=...)` 参数、`Brain._decide_tool_choice` 决策矩阵、`llm.invoke.ok` 新字段 `tool_choice_applied` | 纯文本空 tool 轮出现时, 下一轮实际下发 `tool_choice="required"`; 事件字段在审计 jsonl 中可查 |
| **M8.C ExecutionVerifier** | `core/verifier.py` `CommitmentVerifier`、事件 `brain.commitment.verified/unfulfilled/degraded`、Brain `_verify_commitment` stage | unfulfilled 场景事件落盘且回复不伪装成功; verifier 自身失败走 degraded 不抛异常 |

**M8 当前进度**：
- [x] ADR-001 落地、ToolSpec / @tool / ToolRegistry 扩字段
- [x] PromptContract 三段式渲染 + 反例 few-shot
- [x] Ring1 (alarm / weather / flight / web) + memory_tools + job.greet / job.chat / job.profile 全量补 `when_to_use / when_not_to_use`
- [x] M8.A 回归：pytest tool / prompt_contract / brain 三套绿
- [ ] M8.A 真实 trace 验证：首轮 tool_call 率比基线 +20% 或 `brain.commitment.unfulfilled` 无新增
- [x] **M8.B Phase 1a**：`invoke_chat(tool_choice=...)` 参数 + `llm.invoke.ok.tool_choice_applied` 事件字段（5 guard tests）
- [x] **M8.B Phase 1b**：`Brain._decide_tool_choice` 结构信号决策 + 循环内 prev_text_only escalation 梯度（非交互模式, 12 guard tests）
- [x] **M8.B Phase 1c**：interactive_turn 也允许 escalation（去掉 `mode != interactive_turn` 守卫, 由 trace_e48a6be0c90e 驱动; 2 新 guard tests, 总 14 条）
- [x] **M8.B Phase 2**：scan → trigger 工具间 hand-off — `run_scan` 返 `scan_handle`、`run_trigger(scan_handle=...)` 复用缓存并复用 trace_id、miss 走 fail-loud `error=scan_handle_unknown_or_expired`（8 guard tests, ADR-001 §8）
- [x] **M8.C 契约 C v1**：`core/verifier.py::CommitmentVerifier` + Brain `_verify_commitment` stage + 3 新事件常量 + `brain.commitment.` 持久化前缀 + server.py 装配点（12 verifier tests + 6 Brain 集成 tests, ADR-001 §4.4）
- [x] **M8.C 契约 C v2**（trace_89690fb72ff8 触发）：`TurnEvidence` 替代 `observations_digest`、`ToolSpec.extract_facts` 钩子、judge prompt 补 rubric + hallucination_type taxonomy、`raw_reply` / `shaped_reply` 分离（17 verifier tests + 6 Brain integration tests, ADR-001 §4.4.2/3/5）
- [x] **M8.C 契约 C v2.1**（trace_16e97afe3ffc 触发）：① `unfulfilled` rewrite 绕过 `_apply_soul_style`；② judge prompt evidence per-receipt whitelist + 显式 truncation marker；③ `job.greet.trigger` 暴露 `unavailable` 计数（MCP mode 未配置不再混入 `failed`）；④ `PULSE_BOSS_MCP_GREET_MODE` 默认值 `manual_required` → `browser` + 未知值 fail-loud `mode_not_configured`（新增 3 brain integration + 3 verifier + 2 greet service tests，全绿；ADR-001 §4.4.3/4 + §6 P3c）
- [x] **M8.C 运行环境修复**（trace_16e97afe3ffc 收尾）：① `runtime.health()` 漏改的 `PULSE_BOSS_MCP_GREET_MODE` 默认值对齐 `browser`（`_boss_platform_runtime.py:2655`，与 `greet_job()` 的 reader 保持 lock-step）；② `.env` `BOSS_BROWSER_PROFILE_DIR` 从 `./backend/.playwright/boss`（NTFS 空目录，MCP 每次都进未登录状态）改为 `/root/.pulse/boss_browser_profile`，与 `scripts/boss_login.py` 写入路径对齐——这是用户原话"浏览器几乎观察不到 Agent 在执行任何操作"的真实根因；③ 新增 `scripts/start_boss_mcp.sh`（前台，对齐 `start_backend.sh`）+ `scripts/boss_mcpctl.sh`（daemon：start/stop/restart/status/logs，含 patchright/chrome 孤儿清理）；④ `scripts/start.sh` `all` 模式并行拉起 PG + BOSS MCP + backend，暴露 `boss_mcp` 单项 action；monitor_loop 额外汇报 `boss_mcp=code` 以便一眼发现 gateway 挂掉。
- [x] **M8.A 契约 A 补强 P3d**（trace_4890841c2322 触发）：① `job.greet.trigger` 的 `confirm_execute` schema description 从"Set true only after user confirmation."重写为 **IMPERATIVE vs EXPLORATORY 语义分类规则**（按本轮 utterance 的 sentential mood 决策，不列关键词；内嵌 3 条 worked examples 覆盖"先给我看看"/"帮我投 5 个"/"基于约束+祈使句"三种形态）；② `when_to_use` 里"两阶段 HITL"段落改写为"唯一判据是本轮用户话语，细则见参数 description"，避免 when_to_use 与 schema description 互相冲突；③ `requires_confirmation=True` **保留**（ToolSpec 级"高风险副作用"元数据，与 per-invocation 的 `confirm_execute` 职责正交）；④ `examples` 字段扩为 4 条并补 IMPERATIVE 用例——⚠ 当前 `PromptContractBuilder._section_tools` 未渲染 `IntentSpec.examples`，实际契约靠 schema description 承担；⑤ IntentSpec 与 schema description 均显式标注"过渡态，P4 落地后职责移交 reflection 域"。ADR-001 §6 P3d。
- [ ] **M8.B Phase 3 P4**（契约 B 终态 · action_intent pre_capture）：`soul.reflection:pre_turn` 结构化输出新增 `action_intent={is_imperative, reason, target_tool}`，Brain 据此**预填** `job.greet.trigger` / `job.chat.process` 的 `confirm_execute` 参数，LLM 在 ReAct 循环内看不到该参数 —— 把语义判决从 planning 域上移到 reflection 域；附带决定 `PromptContractBuilder._section_tools` 是否启用 `IntentSpec.examples` 渲染（若 P4 覆盖此职责则不启用，保留为开发文档；否则另起补丁接通）。新增事件 `reflection.pre_turn.action_intent_captured` + `llm.invoke.ok.confirm_execute_prefilled`，regression 覆盖 imperative / exploratory 两向。ADR-001 §6 P4。
- [x] **M8.Browser P3f**（trace 2026-04-21 触发 · BOSS 浏览器执行层两刀修复）：症状双发 —— ① 一个挂起的 `zhipin.com/web/geek/jobs?_security_check=...` chromium 窗口挡住可见浏览器无法关闭（图证）；② HR 聊天显示 **两条** "[送达] 您好，我是..."，第一条是 BOSS 平台代发的 APP 预设话术，第二条是 Pulse 追加的 `greeting_text`（图证）。两条根因独立、共在 MCP 浏览器执行层：① `_ensure_browser_page()` 启动时 chromium 从 `user_data_dir/Sessions` 自动恢复上次所有 tab，但只有 `pages[0]` 被 CDP 绑定，其余变成"WSLg 渲染 × 不受驱动"的僵尸窗口；② `_execute_browser_greet()` 点完"立即沟通"后无条件 `if safe_text: fill+send`，与 BOSS 平台代发预设话术冲突产生重复自我介绍。正交两刀：A. 新增纯函数 `_close_orphan_tabs(context, main_page)` 在 `_ensure_browser_page()` 初始化主 `_PAGE` 后主动 `close()` 所有非主 tab；close 失败落 audit `browser_orphan_tab_close_failed`（窄异常 + fail-loud）；B. 新增 env `PULSE_BOSS_MCP_GREET_FOLLOWUP ∈ {off(默认), on}`（未知值 fail-loud），`_execute_browser_greet()` 把 `if safe_text:` 收敛为 `if followup_enabled and safe_text:`；`off` 时只点"立即沟通"让 BOSS 平台代发预设话术，status 记 `sent` + `greet_strategy=button_only`；`on` 保留旧 "button + followup" 行为供"没设 APP 预设话术"的账号使用；audit `greet_job_result` 新增 `greet_strategy` 字段 + `/health` 新增 `greet_followup` 字段便于复盘。为什么用 env 不用 per-call 参数：账号级环境状态与单次 call 无关；让 Agent/Intent/ToolSpec/service/connector 五环全零改动，`greeting_text` 仍落 audit 便于切 `on` 时回归。新增 8 条单测绿：3 条 `_close_orphan_tabs`（keeps_main+closes_others / is_idempotent / survives_close_exception）+ 3 条 env parse（default_off / env_on / invalid_fails_loud）+ 2 条 `_execute_browser_greet`（button_only_skips_followup / followup_on_fills_and_sends）+ 1 条 health。ADR-001 §6 P3f。
- [x] **M8.Connector P3e**（trace_a9bbc29a245c 触发 · MUTATING 工具副作用屏障）：症状是"一次投递请求在 BOSS 真实外发 4 条消息，但 backend/LLM/Verifier 全报失败"，audit `/root/.pulse/boss_mcp_actions.jsonl` 同 `run_id` 下有 4 行 `greet_job_result/status=sent/ok=true`。三叠根因：① backend `mcp.timeout_sec=45s` < MCP 浏览器侧单次 `_execute_browser_greet` 实测 35–70s，HTTP 先断服务端继续点 send；② connector `retry_count=2` 全域通用，对 MUTATING ops 同样 retry → 每次 timeout 都重发 POST /call 造成重复 click；③ MCP 侧 `reply_conversation` / `send_resume_attachment` 早已接入 `_find_recent_successful_action` 幂等屏障，但 `greet_job()` 漏接这条线。三刀正交：A. `BossPlatformConnector._effective_retry_count` 把 `{greet_job, reply_conversation, send_resume_attachment}` 纳入 MUTATING 白名单 → `retry_count=0`，READ/幂等 op 沿用 `retry_count`；B. `BossMcpSettings` / `BossOpenApiSettings` `timeout_sec` 默认 `45.0 → 90.0`（基于实测 p95 + 余量，env 可降），regression `>= 60.0` 防回退；C. `greet_job()` 开头接入 `_find_recent_successful_action(match={run_id, job_id})` 幂等屏障，命中回放 `idempotent_replay=True` 且不调 `_execute_browser_greet`。新增 11 条单测绿：`test_mutating_operations_skip_retry` × 3 + `test_read_operations_still_retry` × 5 + 3 条 runtime 幂等回放正负用例。ADR-001 §6 P3e。
- [x] **M8.ActionReport P3g**（trace_34682759d5e7 触发 · 执行结果报告层，ADR-003）：症状是「Agent 浏览器里真实投递了 1 个岗位（`greeted=1, status=sent`），但 CommitmentVerifier judge 误判 `unfulfilled/fabricated`，最终 reply 被改写成"我其实没能完成投递"」。根因不是契约 C 失守，而是 **缺少执行结果报告层**：tool observation 经过两条 projection—— 给 LLM 的自然语言翻译 vs 给 judge 的 `extract_facts` 白名单抽取——天然会漂移；上一轮在 `verifier.py` 加的 `_false_absence_guard_reason`（基于"投递/打招呼/发送"关键词 + `job.greet.trigger` 白名单的下游 override）被 `code-review-checklist §15 类型 B`（补丁式兼容，不修源头）明确禁止，已回滚。根治方案是在 `core/` 引入**通用 ActionReport 契约**：所有 mutating / multi-step module 的 tool handler 返回结构化报告（`action`/`status`/`summary`/`details`/`metrics`/`next_steps`/`evidence`），Brain 把它注入 SystemMessage 作为 LLM grounding，Verifier 把它挂到 `Receipt.action_report` 作为 judge 首选证据，两条 projection 共用同一份事实。通用性：`job.greet` / `game.checkin` / `trip.plan` / `system.login` / `notification.send` 全部共享同一 schema，新 module 零改动 core 即可接入。**Step A（已完成）**—— ① 撤掉 `verifier.py` 上一轮 `_false_absence_guard_reason` 补丁 + 对应 2 条单测；② `core/action_report.py` 契约定义（`ActionStatus` 五态 / `ActionDetail` / `ActionReport.build` 自动聚合 status / `to_prompt_lines` 渲染 / `to_receipt_facts` 投影 / `extract_action_report` 三形态兼容），22 条契约单测绿；③ `docs/adr/ADR-003-ActionReport.md` 书面决策。**Step B（本次完成 2026-04-21）**—— ① Brain `_react_loop` 识别 `__action_report__` 后追加 SystemMessage + `BrainStep.action_report` 快照 + `_build_tool_receipts` 投影 `Receipt.action_report`；② Verifier `Receipt` 加 `action_report` 字段，`_compact_action_report` 裁剪 details 并 emit `details_truncated`，`_JUDGE_SYSTEM` Meta-rule 追加 ActionReport 首选证据段（`succeeded/partial` → fulfilled；`preview` → inference_as_fact；`failed` → false_absence；数量不符 → count_mismatch）；③ `job.greet.service.run_trigger` 新增 `_build_trigger_action_report(outcome=scan_miss|not_ready|preview|run)` 纯函数，四条 return 路径统一挂 `__action_report__`；④ 新增 `tests/pulse/core/test_brain_action_report.py` + `tests/pulse/core/test_commitment_verifier.py` §8（ActionReport 首选证据 + trace_34682759d5e7 回归 + preview 反向 + kill-switch）+ `tests/pulse/modules/job/greet/test_service_action_report.py`（四路径 + `_UNAVAILABLE_STATUSES → skipped` 映射）；⑤ 全套回归 `core/test_action_report + test_brain_action_report + test_commitment_verifier + test_brain{.,_commitment,_tool_choice,_endpoints} + modules/job/{test_scan_trigger_handoff,test_scan_multi_city,test_job_memory_dedup,test_memory_salary_spec}` 均绿（profile_manager 6 条 pre-existing fail 无关 P3g）。可逆性：`PULSE_ACTION_REPORT_INJECT=off` / `PULSE_ACTION_REPORT_JUDGE=off` 两级开关，handler 侧永远可以返回 ActionReport，Brain/Verifier 消费端独立降级。待一轮真实 trace 回放验收（用户端到端跑"投 1 个岗位"场景，确认 reply 不再被误改写为"我其实没能完成投递"）。ADR-003 §4–§7 + §6 Step A/B。
- [x] **M8.ActionReport.B5 P3g-followup**（trace_fe19c3ab1e43 触发 · ActionReport Step B.5 薪资缺失 + 后端全链路核查）：症状是「Step B 端到端回放通过（verdict=verified, 未 rewrite, `boss_mcp_actions.jsonl` 记录 `ok=true, status=sent, screenshot`),但 shaped_reply 漏报薪资」。用户担心其它层还有"假通过"。第一性追链后定位三个正交漏洞——① **`ActionReport.to_prompt_lines` 不渲染 details[*].extras**：`extras` 里的 salary/match_score/company 只进 `to_dict`（audit + Verifier 可见）却不进 prompt_lines（LLM 可见），Brain 注入的 SystemMessage 缺业务字段 → LLM 在 `IMPORTANT: MUST be grounded, Do NOT invent` 约束下照实没报；② **招聘平台 PUA 字体反爬未 sanitize**：BOSS/拉勾/58 用 Private Use Area (U+E000..U+F8FF) 私有码点做数字反爬，裸字符流到 prompt 会被 LLM 默默吞掉或乱写（表现为"回复没有薪资"），需要一道通用防线；③ **Verifier judge prompt 缺 `skipped` 状态规则**：Meta-rule 只列了 succeeded/partial/preview/failed 四态，`skipped`（被幂等/前置短路，未发生副作用）在通用 knowledge 区，judge LLM 可能放水让"已完成"类承诺假通过。**本次修复**—— ① `core/action_report.py` 新增 `_sanitize_prompt_str(value)` + `_PUA_PATTERN = r"[\\ue000-\\uf8ff]+" → «encoded»` marker（fail-loud 而非静默吞），`_iter_renderable_extras` 按白名单 primitive (`str / int / float / bool`) 渲染 `extras`，配 `_MAX_EXTRAS_PER_DETAIL=8` 上限；所有流向 prompt 的字符串（`action`/`summary`/`target`/`reason`/`next_step`/str evidence/str extras 值）统一过 sanitize，原串仍保存在 `to_dict` 里供 audit；② `job.greet.service.py` 的 `matched_details[*]` 透传 scan item 的 salary（原串，含 PUA 由 ActionReport 层统一 sanitize），`_build_trigger_action_report` 按求职视角优先级排 extras 键（`company → salary → match_score → match_verdict`），空串 / None 静默丢弃；③ `core/verifier.py` 的 `_JUDGE_SYSTEM` Meta-rule 追加 `skipped` 规则：`action_report.status=="skipped"` 且 reply 声明"已完成" → 判 `inference_as_fact`（和 preview 对偶，覆盖 ActionStatus 五态）；④ 补单测：`test_action_report` §4 加 5 条（extras primitive 渲染 + 插入顺序保序 + PUA sanitize + 非 primitive 丢弃 + `_MAX_EXTRAS_PER_DETAIL` 上限），`test_service_action_report` 加 3 条（salary 透传 + 缺失静默 + PUA sanitize 端到端），`test_commitment_verifier` §8 加 1 条 `skipped` 对偶。回归 68/68 绿（`test_action_report + test_brain_action_report + test_commitment_verifier + test_service_action_report`）。**后端全链路审计结果**：`trace_fe19c3ab1e43` 在 `pulse.log` L128-196 完整；`boss_mcp_actions.jsonl` 末尾 3 条（`greet_job / greet_job_attempt_summary status=sent / greet_job_result ok=true, screenshot=20260422_134506_greet_2fdc49499d58b25c.png`）坐实真实投递；ActionReport SystemMessage（L177 `[Action Report — ground-truth facts ...] action=job.greet, status=succeeded, summary=已投递 1 个岗位`）成功注入 GPT-4.1；Verifier L191 judge evidence 完整携带 `action_report.status=succeeded / succeeded=1`，`out_chars=174 reason=completed` 未 rewrite；`pre_capture_receipts` 两条 `preference.domain.applied` 均 `status=ok`——全链路真实通过，无假通过。发现两个可观测性 debt 独立立项：**ADR-003 Step D** BOSS PUA 字体在线解码器（根治 salary 数值乱码）+ **ADR-003 Step E** `boss_mcp_actions.jsonl` audit 条目补 `trace_id` 支持双向关联。ADR-003 §6 Step B.5。
- [ ] M8.B 真实 trace 验证：`tool_choice_applied` 字段在审计 jsonl 中可查 + `scan_handle_reused=true` 消除 trigger 内重扫
- [ ] M8.C 真实 trace 验证：trace_e48a6be0c90e 类"已记录但 used_tools=[]"失败场景触发 `brain.commitment.unfulfilled` 事件 + 用户看到坦诚改写

**依赖**：M4 ReAct 循环、M5 Prompt Contract、M7 事件总线。
**回退**：契约 A 不填字段即退化为原 description 单段；B 通过 `PULSE_TOOL_CHOICE_POLICY=off` 关闭；C 通过 `PULSE_COMMITMENT_VERIFIER=off` 关闭。三者独立降级。

---

## M9：Patrol 对话式控制面（per-patrol lifecycle via IntentSpec）

设计依据：[ADR-004-AutoReplyContract](./adr/ADR-004-AutoReplyContract.md) §6.1、`Pulse-AgentRuntime设计.md` §5.6、`Pulse-DomainMemory与Tool模式.md` §6.3.1。

**目标**：把 AgentRuntime 的 per-patrol 生命周期控制面从"启动期 env-flag 一次性拍板"升级到"对话式可交互" —— 用户在 IM 里说"开启自动回复 / 后台在跑什么 / 现在就跑一次 job_chat"能直接映射到 `AgentRuntime.{enable,disable,run_once}_patrol`。与 ChatGPT Scheduled Tasks / LangGraph thread-cron 的心智一致。

**M9 当前进度**（一次性到位，2026-04-22 全部落地）：
- [x] ADR-004 §6.1 决策 A-D（per-patrol 软开关内存态 / IntentSpec-not-MCP / 与 `run_process` 共存 / 5 个 IntentSpec schema + 事件契约）
- [x] `SchedulerEngine.set_enabled(name, enabled)` + `.get_task(name)`（fail-loud unknown，不自动创建）
- [x] `AgentRuntime` 新增 `list_patrols / get_patrol_stats / enable_patrol / disable_patrol / run_patrol_once`；发 `runtime.patrol.lifecycle.{enabled,disabled,triggered}`；heartbeat carve-out
- [x] `/api/runtime/patrols`、`/api/runtime/patrols/{name}`、`/{name}/enable|disable|trigger` 五条 HTTP 路由
- [x] `modules/system/patrol/module.py` 新 `PatrolControlModule` 暴露 5 个 `IntentSpec`（`system.patrol.list / status / enable / disable / trigger`），严格按 ADR-001 契约 A 填 `when_to_use` / `when_not_to_use` / `parameters_schema` / `examples`；risk_level + requires_confirmation 与 §6.1.4 风险矩阵对齐
- [x] 单测全绿：`test_scheduler_engine`（+3 条 set_enabled / get_task）、`test_agent_runtime_patrol_control`（8 条：list 排除心跳 / enable-disable lifecycle 事件 / unknown fail-loud / heartbeat carve-out / trigger 成功路径 / trigger 异常路径 / trigger 未知 / snapshot）、`test_patrol_control_module`（7 条：契约 A 聚合 shape + 5 条 handler 委托端到端 + 未绑定 runtime raise RuntimeError）
- [x] `scripts/smoke_patrol_control.py` 端到端 dry-run 脚本：新鲜 runtime + noop patrol，跑完 list → status → enable → trigger → status → disable → list + heartbeat 不变量探针；stdout 打印每步返回 + 事件流便于肉眼审
- [x] 文档同步：`Pulse-AgentRuntime设计.md` §5.2 类签名、§5.4 事件表、§5.5 HTTP 表、§5.6 对话式控制面章节；`Pulse-DomainMemory与Tool模式.md` §6.3.1 系统控制类 Intent Tool；`Pulse-内核架构总览.md` §5.2 `PatrolLifecycleRequest` 行

**验收**：一条 noop patrol 走 `list → enable → trigger → status → disable` 五动作能观测到 stats 变化 + 3 种 lifecycle 事件 + heartbeat 名不被列出/不可控，smoke 输出和 §6.1.8 状态表一致。

**回退**：不改 env 即不影响任何现有 patrol（`register_patrol` 参数未变，初始 enabled 语义未变）；如不希望暴露对话式控制面，从 `modules/system/patrol/__init__.py` 移除即可（HTTP 路由也可在 `core/server.py` 局部 feature flag，但当前没必要）。

**依赖**：M4 ReAct 循环 + M8.A 契约 A（`when_to_use` / `when_not_to_use` 渲染）+ 既有 `AgentRuntime` / `SchedulerEngine`。

---

## 依赖关系与回归风险控制

```
M0 ──→ M1 ──→ M2 ──→ M3 ──→ M4 ──→ M5 ──→ M6 ──→ M7 ──→ M8
骨架    基础设施  业务迁移  新功能   Brain   记忆    自扩展   进化   工具使用合约
                  ▲
                  │
          Phase 1 功能验证关卡
          （通过后才删除 V1 代码）
```

**防回归原则**：
- M0-M1 阶段 V1 代码保留不动，新旧并存
- M2 逐个 Module 迁移，迁一个验一个，全部通过后才删除 V1
- M3 及之后每个里程碑都是对已有系统的**纯增量扩展**，不修改已有接口
- 依赖方向严格单向：`modules/ → core/`，`core/` 内部 `brain → tool/module/mcp/memory`，绝不反向
