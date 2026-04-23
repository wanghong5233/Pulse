# Pulse 差距审查与实施计划

> 基准文档：`docs/Pulse架构方案.md`
> 审查时间：2026-03-14
> **最后更新：2026-03-28 第三轮审查完成**
> 审查范围：`src/pulse/` 全量代码 vs 架构方案七大章节
> 注意：本文档反映的是 2026-03-28 时点的审查结论；最新阶段性验收口径请以 `docs/Pulse剩余验收关口清单.md` 为准。

---

## 一、逐组件差距审查

### 1. 三层结构（接入层 → Module → Capability）

| 组件 | 方案要求 | 实际状态 | 差距评级 |
|------|---------|---------|---------|
| Channel（CLI/飞书） | BaseChannel 接口 + 统一格式化 | ✅ `channel/base.py`, `cli.py`, `feishu.py` | 无 |
| IntentRouter | 三级降本路由（精确→前缀→LLM） | ✅ `router.py` + `router_config.py` | 无 |
| Channel → Brain 贯通 | 消息经 Channel → Router → Brain → 回复 | ✅ `server.py::_dispatch_channel_message` | 无 |
| ModuleRegistry | 启动扫描 + capability 声明 + `as_tools()` | ✅ `module.py` 发现 `pulse.modules` | 无 |
| Capability 层 | Module 通过 Capability 访问基础设施 | ✅ **已完成** — IntelKnowledgeStore 已迁移到 PG+Chroma | 无 |

### 2. Brain ReAct 推理引擎

| 组件 | 方案要求 | 实际状态 | 差距评级 |
|------|---------|---------|---------|
| ReAct 循环 | think → act → observe → respond，最多 20 步 | ✅ LLM function calling + messages 标准流 + max_steps=20 + consecutiveErrors=3 | 无 |
| 记忆加载到 system prompt | Core + Recall recent + Recall search + Archival → 注入 | ✅ 四层记忆全部注入 system prompt | 无 |
| Observation 追加上下文 | 工具结果追加到 messages，LLM 看到后决定下一步 | ✅ AIMessage + ToolMessage 标准消息流 | 无 |
| 记忆写入 | 推理后自主决定是否更新记忆 | ✅ `memory_archive` 工具让 Brain 自主写入 Archival | 无 |
| 纠正检测 | 推理后检测用户纠正 → DPO + PREFS 更新 | ✅ `CorrectionDetector` 集成到 Brain `_remember_interaction` | 无 |

### 3. 三环能力模型（Ring1 / Ring2 / Ring3）

| Ring | 方案要求 | 实际状态 | 差距评级 |
|------|---------|---------|---------|
| Ring1 Tool | 轻量内置工具，独立文件 | ✅ `tools/alarm.py`, `weather.py`, `flight.py`, `web.py` + `memory_tools.py` | 无 |
| Ring2 Module | 重型 Pipeline 作工具 | ✅ `ModuleRegistry.as_tools()` 标记 `ring2_module` + `pipeline_runs` 写入 | 无 |
| Ring3 MCP | 外部生态工具 | ✅ HTTP + stdio 双传输，`MCPClient` 管理多 transport | 无 |
| MCP Server 对外暴露 | Pulse 工具作为标准 MCP Server | ✅ `MCPServerAdapter.serve_stdio()` JSON-RPC over stdio | 无 |

### 4. 四层记忆

| 层 | 方案要求 | 实际状态 | 差距评级 |
|----|---------|---------|---------|
| Core Memory | SOUL/USER/PREFS/CONTEXT 四个 Block + `soul.yaml` 种子 | ✅ `core_memory.py` + `soul.yaml` 含 [CORE]/[MUTABLE] | 无 |
| Recall Memory | PostgreSQL `conversations` + `tool_calls` + Chroma 向量 | ✅ `recall_memory.py` — PG + Chroma + `record_tool_call` | 无 |
| Archival Memory | PostgreSQL `facts` + Chroma 向量 | ✅ `archival_memory.py` — PG + Chroma（完整 schema） | 无 |
| Meta Memory（DPO） | DPO 训练对收集 | ✅ `DPOCollector` 写入 PG `corrections` 表 | 无 |
| 向量检索质量 | 语义检索 | ✅ OpenAI/sentence-transformers 真实 embedding | 无 |
| 记忆工具 | `memory_read/update/search/archive` 与其他 Tool 同级 | ✅ `memory_tools.py` 已注册 | 无 |
| DDL 与运行时一致 | `tool_calls`/`pipeline_runs`/`corrections` 表 | ✅ Brain + ModuleRegistry 均有写入 | 无 |

### 5. Capability Layer 逐项

| 能力 | 方案阶段 | 实际状态 | 差距 |
|------|---------|---------|------|
| Browser Pool | Phase 2 | ✅ `core/browser/pool.py`（site_key 复用 + 健康检查） | 无 |
| LLM Router | Phase 2 | ✅ `core/llm/router.py` invoke_text/invoke_structured/invoke_chat | 无 |
| Storage Engine (PG+Chroma) | Phase 2 | ✅ `storage/engine.py` + `storage/vector.py` (真实 embedding) | 无 |
| Notifier | Phase 2 | ✅ `ConsoleNotifier` + `FeishuNotifier` + `MultiNotifier` | 无 |
| Scheduler | Phase 2 | ✅ `scheduler/engine.py` — 峰谷时段感知 + active_hours_only | 无 |
| Channel | Phase 2 | ✅ `core/channel/` (base + cli + feishu) | 无 |
| Router | Phase 2 | ✅ `core/router.py` 三级匹配 | 无 |
| EventBus | Phase 2 | ✅ `events.py` + `InMemoryEventStore` | 无 |
| Config | Phase 2 | ✅ `config.py` Pydantic Settings | 无 |
| Policy Engine | Phase 2 | ✅ `policy.py` safe/confirm/blocked 三级门控 | 无 |
| Observability | Phase 2 | ⚠️ 进程内事件存储，无外部持久化 | **低** |
| MCP Client | Phase 3 | ✅ HTTP + stdio 双传输 | 无 |
| Cost Controller | Phase 3 | ✅ `cost.py` — 日预算 + 自动降级推荐 + Brain 集成 | 无 |

### 6. Skill Generator

| 要求 | 实际状态 | 差距评级 |
|------|---------|---------|
| LLM 生成代码 | ✅ `_generate_code_with_llm` 调用 LLM，保留 fallback | 无 |
| AST 安全扫描 | ✅ `sandbox.py` | 无 |
| subprocess 沙箱 | ✅ `sandbox.py` | 无 |
| 热加载 | ✅ `importlib.util.spec_from_file_location` + `register_callable` | 无 |
| HITL 确认激活 | ✅ Policy Engine 门控 `skill.activate` | 无 |

### 7. Evolution Engine

| 要求 | 实际状态 | 差距评级 |
|------|---------|---------|
| 反思管线 | ✅ `soul/evolution.py` → `reflect_interaction` | 无 |
| SOUL [CORE] 不可变 / [MUTABLE] 可进化 | ✅ `soul/governance.py` + `soul.yaml` 含 [CORE]/[MUTABLE] | 无 |
| 治理门控 (autonomous/supervised/gated) | ✅ 含审批、回滚、审计、版本 | 无 |
| DPO 收集 | ✅ PG `corrections` 表 | 无 |
| 偏好学习 Track A | ✅ `PreferenceExtractor` LLM + `CorrectionDetector` → PREFS 更新 | 无 |
| 行为分析 | ✅ `BehaviorAnalyzer` LLM + heuristic | 无 |

### 8. Module 内部质量

| 问题 | 涉及模块 | 差距评级 |
|------|---------|---------|
| `IntelKnowledgeStore` | ✅ 已迁移到 PG + Chroma | 无 |
| `boss_chat`/`boss_greet` 审计日志 | ✅ 优先写入 PG `actions`/`boss_chat_events`，jsonl 仅作 fallback | 无 |
| init_db.sql | ✅ 已清理 V1 旧表 | 无 |
| `feedback_loop` 模块 | ✅ Phase 3 完整实现（API + corrections 写入） | 无 |
| `pipeline_runs` 记录 | ✅ `ModuleRegistry.as_tools()` handler 每次调用写入 PG | 无 |

---

## 二、差距汇总

### ✅ 第一轮修复（2026-03-14）

| 编号 | 差距 | 解决方案 |
|------|------|---------|
| G1 | Brain 不是真正的 ReAct | ✅ 完全重写为 LLM function calling ReAct 循环 |
| G2 | Archival Memory 未注入 Brain | ✅ system prompt 注入 Archival 查询结果 |
| G3 | MCP 非标准传输 | ✅ 新增 `StdioMCPTransport` |
| G4 | Skill Generator mock | ✅ 接入 LLM 代码生成 |
| G5 | 确定性哈希 embedding | ✅ 支持 OpenAI/sentence-transformers |
| G6 | PG 审计表无运行时写入 | ✅ Brain tool_calls 表写入 |
| G7 | IntelKnowledgeStore jsonl | ✅ 迁移到 PG + Chroma |
| G8 | 偏好学习硬编码 | ✅ PreferenceExtractor LLM 化 |
| G9 | Brain 不能自主写 Archival | ✅ memory_archive 工具 |
| G10 | DPO jsonl | ✅ PG corrections 表 |
| G13 | init_db.sql 旧表 | ✅ 已清理 |
| G14 | max_steps + consecutiveErrors | ✅ 20 + 3 |

### ✅ 第二轮修复（2026-03-28）

| 编号 | 差距 | 解决方案 |
|------|------|---------|
| G15 | `correction_detector.py` 缺失 | ✅ 新建，LLM + heuristic 纠正检测 |
| G16 | `behavior_analyzer.py` 缺失 | ✅ 新建，LLM + heuristic 行为分析 |
| G17 | `soul.yaml` 无 [CORE]/[MUTABLE] | ✅ 重写为方案 §6.5 标准结构 |
| G18 | boss_* jsonl 审计日志 | ✅ 优先写 PG，jsonl 降级 fallback |
| G19 | MCP Server 非标准协议 | ✅ `serve_stdio()` JSON-RPC 2.0 |
| G20 | tools/ 未拆分独立文件 | ✅ alarm.py / weather.py / flight.py / web.py |
| G21 | pipeline_runs 无写入 | ✅ ModuleRegistry handler 每次调用写入 PG |
| G22 | Correction → PREFS 链断裂 | ✅ CorrectionDetector 提取规则 → CoreMemory.update_preferences |
| G23 | feedback_loop 模块缺失 | ✅ 完整模块 + API + corrections 写入 |
| G24 | 无 FeishuNotifier | ✅ FeishuNotifier 实现 Notifier 协议 |
| G25 | CostController 无降级 | ✅ should_degrade + recommend_route + Brain 集成 |
| G26 | Scheduler 无峰谷内置 | ✅ peak/offpeak interval + active_hours_only |

### ✅ 第三轮修复（2026-03-28 深度审查）

| 编号 | 严重度 | 差距 | 解决方案 |
|------|--------|------|---------|
| G27 | **CRITICAL** | `server.py` `db_engine` 未定义 → 启动 NameError | ✅ 改为 `storage_engine` |
| G28 | **CRITICAL** | Brain 纠正检测 `recent(2)` 取到本轮而非上轮 | ✅ 在 `add_interaction` 前先获取上一轮 assistant |
| G29 | **CRITICAL** | `correction_detector` 调用 `update_preferences(key=, value=)` 签名不匹配 | ✅ 改为传 `dict` |
| G30 | **CRITICAL** | `cost.py` `status()` 嵌套 `Lock` 死锁 | ✅ 提取 `_should_degrade_unlocked` 无锁方法 |
| G31 | **CRITICAL** | `mcp_transport_stdio` 进程重启不重置 `_initialized` | ✅ 进程重建时先 `_initialized = False` |
| G32 | **CRITICAL** | `feedback_loop` INSERT 使用不存在的 `correction_type` 列 | ✅ 改用 `correction_json` 列 |
| G33 | ISSUE | Brain `tool_calls` 仅支持 dict 不兼容对象 | ✅ 兼容 dict `.get()` 和 object `getattr()` |
| G34 | ISSUE | `behavior_analyzer` 创建后未使用（死变量） | ✅ 挂到 `app.state` + 暴露 API 端点 |
| G35 | ISSUE | `recall_memory._ensure_schema` 缺 `tool_calls` 表 | ✅ 补齐 `CREATE TABLE IF NOT EXISTS tool_calls` |
| G36 | ISSUE | `boss_chat` `inserted` 计数 ON CONFLICT 时虚高 | ✅ 改用 `RETURNING id` + `fetch="one"` 判断 |

### 🟡 剩余可迭代项

| 编号 | 差距 | 优先级 |
|------|------|--------|
| G11 | 前端未对齐当前 API | 低（P0-2） |
| G12 | Observability 无外部持久化 | 低 |

---

## 三、当前状态

> **Pulse 架构方案的全部核心组件已完整实现，经三轮深度审查确认无 CRITICAL 残留：**
>
> - **Brain**：真正的 ReAct 推理引擎（LLM function calling + max_steps=20 + 纠正检测序列已修正）
> - **四层记忆**：Core(SOUL [CORE]/[MUTABLE]) / Recall(PG+Chroma+tool_calls 自建) / Archival(PG+Chroma) / Meta(PG corrections+DPO)
> - **三环工具**：Ring1 builtin(alarm/weather/flight/web+memory) + Ring2 module(pipeline_runs) + Ring3 MCP(http+stdio)
> - **MCP 双角色**：Client(http+stdio, 进程重连安全) + Server(JSON-RPC stdio)
> - **Skill Generator**：LLM 代码生成 + AST + 沙箱 + 热加载
> - **Evolution Engine**：反思 + SOUL 治理 + CorrectionDetector → PREFS(签名已修正) + BehaviorAnalyzer(已挂载 API) + DPO
> - **Capability Layer**：全部就绪，CostController 死锁已修复，Scheduler 峰谷已内置
> - **server.py**：启动链路 NameError 已修复，所有组件正确接线
>
> 剩余可迭代项仅为前端重写和 Observability 外部持久化。
