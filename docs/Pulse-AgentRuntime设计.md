# Pulse Agent Runtime 设计文档

> 版本：v2.1 | 日期：2026-04-13
> 定位：Pulse 的 Agent OS / Runtime Kernel 基线文档，负责说明长驻 Runtime、调度、patrol、生命周期与模块接入边界
> 关联文档：`Pulse-MemoryRuntime设计.md`（Task Runtime / Memory Runtime 主设计文档）、`Pulse-内核架构总览.md`（审计与维护基线）

---

## 1. 设计背景

> 说明：本文档聚焦 **Agent OS 层**，不再承担 Task Runtime 与 Memory Runtime 的完整设计展开。后两者以 `Pulse-MemoryRuntime设计.md` 为主，以 `Pulse-内核架构总览.md` 为审计总索引。

### 1.1 Pulse 的定位

Pulse 是一个**通用 AI 助手**（General-Purpose AI Assistant），不是某个垂直领域的自动化脚本。它的核心能力是：

- **Brain**：基于 LLM 的 ReAct 推理 + 工具调用
- **Memory**：四层记忆系统（Core / Recall / Archival / Meta）
- **Modules**：可插拔业务模块（BOSS 招聘、情报采集、未来可扩展任意领域）
- **Runtime**：持久运行的 Agent 执行环境 ← **本文档**

### 1.2 问题

架构重构中删除了旧的 `production_guard.py`，新建了 `core/scheduler/` 调度基础设施，但**未将其提升为通用 Agent Runtime**。结果 Pulse 退化为纯 HTTP API Server——只能响应一次性请求，丧失了作为 Agent 持久运行、主动执行任务的核心能力。

### 1.3 目标

为 Pulse 构建一个**通用的、操作系统级别的 Agent Runtime**：

- 作为 Pulse 的"内核"，管理所有长时间运行的后台任务
- 任何业务模块都可以向 Runtime 注册自己的定时任务（Patrol Task）
- Runtime 不知道、也不关心具体模块做什么——它只提供调度、错误恢复、可观测性等基础设施
- 支持同时运行多种定时任务、多个子 Agent
- 未来可扩展：日历助手、研究 Agent、监控 Agent、任何新领域

### 1.4 设计原则

| 原则 | 说明 |
|------|------|
| **内核与应用分离** | Runtime 是操作系统内核，业务模块是运行在上面的应用程序 |
| **自注册** | 模块在 `on_startup` 中主动向 Runtime 注册任务，Runtime 不主动导入任何模块 |
| **业务无关** | Runtime 的代码中不出现任何模块名（BOSS、Intel 等） |
| **可插拔** | 新模块只需实现 handler + 在 `on_startup` 中注册，即接入 Runtime |
| **渐进增强** | Runtime 不强制所有模块接入——不注册就是普通 HTTP 模块 |

### 1.5 演进路径

```
OfferPilot                         Pulse
production_guard.py      ──→    core/scheduler/ (基础设施)
  │                                │
  │ 单文件 ~300 行                  │ ScheduleTask + SchedulerEngine
  │ 硬编码 BOSS 逻辑               │ BackgroundSchedulerRunner
  │ 时段感知                        │ windows.py (时间窗)
  │                                │
  └─ 已删除                        └─ 已实现，但未提升为通用 Runtime
                                          │
                                          ▼
                                   core/runtime.py (本文档)
                                     AgentRuntime ("OS 内核")
                                       ├── HeartbeatLoop (通用调度)
                                       ├── PatrolTask (通用任务抽象)
                                       ├── 错误恢复 + 熔断
                                       └── 可观测性 (EventBus)

                                   业务模块 (自注册):
                                       ├── boss_greet → register_patrol()
                                       ├── boss_chat  → register_patrol()
                                       ├── intel_*    → register_patrol()
                                       └── 未来模块   → register_patrol()
```

---

## 2. 竞品对标

### 2.1 OpenClaw Gateway / Heartbeat

| 概念 | OpenClaw 设计 | Pulse 对应 |
|------|--------------|------------|
| Gateway | 单进程长生命周期，管理通道、路由、调度 | FastAPI + AgentRuntime (进程内) |
| Heartbeat | 完整 Agent Turn（不是简单定时器） | PatrolTask（每次执行是完整的 Agent 级操作） |
| Heartbeat 周期 | 默认 30min，到期检查 tasks | ScheduleTask 峰/离峰间隔 |
| 时间戳持久化 | session state，重启不丢失 | SchedulerEngine._last_run_at (内存，可扩展 PG) |
| Cron 重试策略 | rate_limit / overloaded / network | 级联恢复 + circuit breaker |
| per-session 序列化 | 防并发竞争 | per-task 序列化（同一任务不重入） |
| HEARTBEAT.md | 用户可编辑的任务清单 | 模块自注册 + 环境变量 |

**取经**：Heartbeat = 完整 Agent Turn 的理念；时间戳持久化；重试策略分级。

### 2.2 Claude Code Agentic Loop

| 概念 | Claude Code 设计 | Pulse 对应 |
|------|-----------------|------------|
| 主循环 | async generator（query.ts） | BackgroundSchedulerRunner 守护线程 |
| Per-Turn Pipeline | 6 阶段管线 | 5 阶段：Guard → Execute → Recover → Record → Emit |
| 成本感知恢复 | 免费 → 低成本 → 高成本 | CostController 预算检查 + 级联降级 |
| 递减收益检测 | 连续 3+ 次无效则停止 | circuit breaker（consecutive_errors 阈值） |
| 终止原因枚举 | 9 种 StopReason | PatrolOutcome 枚举 |
| 错误恢复级联 | Prompt 过长三级、输出超限三级 | skip → retry → degrade → abort |

**取经**：结构化 Per-Turn Pipeline；成本感知；递减收益检测防死循环。

### 2.3 三者对比

| 维度 | OpenClaw | Claude Code | Pulse AgentRuntime |
|------|----------|-------------|-------------------|
| 本质 | 本地守护进程 | CLI 交互进程 | Web Server 内嵌 OS |
| 持久性 | 7×24 Gateway | 会话级 | 7×24 随 Server |
| 调度 | Heartbeat + Cron | 无（用户驱动） | ScheduleTask + HeartbeatLoop |
| 模块接入 | 内置 Agent | 内置 Tools | 模块自注册 patrol |
| LLM 集成 | Pi Agent | Claude API | Brain ReAct（可选） |
| 记忆 | SOUL.md / Session | Transcript | 四层记忆系统 |
| 工具 | Function Calling + MCP | Tool Use + MCP | 三环模型 + MCP |
| 治理 | Sandbox + 审批 | 8 层安全 | Policy Engine |
| 可观测性 | lifecycle 事件 | Transcript | EventBus + SSE |

---

## 3. 核心概念

### 3.1 AgentRuntime（OS 内核）

AgentRuntime 是 Pulse 的"操作系统内核"。它：

- **不知道任何业务模块**——不 import BOSS、Intel 或任何具体模块
- **只提供基础设施**——调度、错误恢复、熔断、可观测性、生命周期管理
- **通过 `register_patrol()` 接受任务注册**——这是模块与 Runtime 的唯一耦合点

类比：Linux 内核不知道 nginx、MySQL 的存在，它只提供进程调度、内存管理、文件系统。应用程序通过系统调用与内核交互。

### 3.2 HeartbeatLoop

**借鉴 OpenClaw Heartbeat 理念**：不是简单的 `setInterval` 定时器，每次心跳是一次完整的 **Agent Turn**。

每次心跳执行 5 阶段管线：

```
┌─────────────────────────────────────────────┐
│              HeartbeatLoop                   │
│                                             │
│  while running:                             │
│    sleep(tick_seconds)                       │
│    ┌─────────┐                              │
│    │  Guard   │ ── 非活跃时段 → skip         │
│    └────┬────┘                              │
│         ▼                                   │
│    ┌─────────┐                              │
│    │  Check  │ ── 遍历所有注册的任务         │
│    └────┬────┘                              │
│         ▼                                   │
│    ┌─────────┐                              │
│    │ Execute │ ── 到期任务执行 handler       │
│    └────┬────┘                              │
│         ▼                                   │
│    ┌─────────┐                              │
│    │ Recover │ ── 错误分级 + 熔断判断        │
│    └────┬────┘                              │
│         ▼                                   │
│    ┌─────────┐                              │
│    │  Emit   │ ── EventBus 事件流           │
│    └─────────┘                              │
└─────────────────────────────────────────────┘
```

### 3.3 PatrolTask（巡检任务）

任何模块都可以注册的**通用定时任务抽象**。

与底层 `ScheduleTask` 的区别：

| 维度 | ScheduleTask | PatrolTask (Runtime 封装) |
|------|-------------|--------------------------|
| 定位 | 底层定时器 | Agent 级任务（带恢复和可观测） |
| 错误处理 | 静默吞掉 | 级联恢复 + circuit breaker |
| 可观测性 | 无 | EventBus 全链路事件 |
| 熔断保护 | 无 | 连续 N 次失败自动停止 |
| 注册方式 | 直接调用 Engine | 模块通过 `runtime.register_patrol()` |

**实现**：PatrolTask 是概念抽象，底层仍复用 `ScheduleTask` + `BackgroundSchedulerRunner`，通过 `_execute_patrol()` 包装 handler 实现增强。

### 3.4 PatrolOutcome（终止原因枚举）

借鉴 Claude Code 的 StopReason，每次任务执行产生明确的结果枚举：

```python
class PatrolOutcome(str, Enum):
    completed = "completed"               # 正常完成
    skipped_inactive = "skipped_inactive"  # 非活跃时段
    skipped_disabled = "skipped_disabled"  # 任务被禁用
    skipped_budget = "skipped_budget"      # 预算不足
    skipped_not_ready = "skipped_not_ready"# 前置条件不满足
    error_recovered = "error_recovered"    # 出错但已恢复
    error_aborted = "error_aborted"        # 出错且熔断
    degraded = "degraded"                  # 降级执行
```

---

## 4. 架构设计

### 4.1 分层架构

```
┌──────────────────────────────────────────────────────────────────┐
│                       业务模块层 (Applications)                  │
│                                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐    │
│  │boss_greet│  │boss_chat │  │intel_*   │  │ 未来任意模块  │    │
│  │          │  │          │  │          │  │              │    │
│  │ patrol() │  │ patrol() │  │ patrol() │  │  patrol()    │    │
│  └─────┬────┘  └─────┬────┘  └─────┬────┘  └──────┬───────┘    │
│        │             │             │               │            │
│        └──────┬──────┴──────┬──────┘               │            │
│               │  register_patrol()  │               │            │
│               ▼                     ▼               ▼            │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              AgentRuntime ("OS 内核")                     │   │
│  │                                                          │   │
│  │  ┌────────────────────────────────────────────────────┐  │   │
│  │  │           BackgroundSchedulerRunner                 │  │   │
│  │  │  ┌────────────────────────────────────────────┐    │  │   │
│  │  │  │ ScheduleTask registry (业务无关)            │    │  │   │
│  │  │  └────────────────────────────────────────────┘    │  │   │
│  │  └────────────────────────────────────────────────────┘  │   │
│  │                                                          │   │
│  │  错误恢复 + 熔断 │ 可观测性 (EventBus) │ 生命周期管理   │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌────────┐  ┌────────┐  ┌────────┐  ┌──────────┐              │
│  │ Brain  │  │ Memory │  │EventBus│  │CostCtrl  │              │
│  └────────┘  └────────┘  └────────┘  └──────────┘              │
└──────────────────────────────────────────────────────────────────┘
```

关键：Runtime（内核层）与业务模块层之间的唯一接口是 `register_patrol()`。Runtime 从不向上引用任何模块。

### 4.2 模块注册流程

```
server.py lifespan
  │
  ├── 1. 创建 AgentRuntime 实例
  ├── 2. 对每个模块调用 module.bind_runtime(runtime)
  ├── 3. 对每个模块调用 module.on_startup()
  │       └── 模块内部（ADR-004 §6.1.1）：
  │           if runtime:
  │               runtime.register_patrol(name=..., handler=...)
  │               # 不读 env, 初始 enabled=False (runtime 默认值)
  │               # 启停由 IM 经 system.patrol.enable/disable 独占控制
  ├── 4. 如果 AGENT_RUNTIME_ENABLED=true:
  │       runtime.start()  ← 启动 HeartbeatLoop（心跳与 patrol 启停正交）
  └── 5. yield (FastAPI 运行中)
  └── 6. shutdown: runtime.stop() + module.on_shutdown()
```

### 4.3 状态机

```
              ┌──────────┐
   ┌──────────│  Stopped  │
   │          └─────┬────┘
   │                │ start()
   │                ▼
   │          ┌──────────┐
   │     ┌────│  Running  │◄───────────┐
   │     │    └─────┬────┘             │
   │     │          │ tick             │
   │     │          ▼                  │
 stop()  │    ┌──────────┐             │
   │     │    │ Checking │──(无到期)──→ sleep
   │     │    └─────┬────┘
   │     │          │ 有到期任务
   │     │          ▼
   │     │    ┌──────────┐
   │     │    │Executing │
   │     │    └─────┬────┘
   │     │          │
   │     │     ┌────┴────┐
   │     │     ▼         ▼
   │     │  success    error
   │     │     │         │
   │     │     ▼         ▼
   │     │  Record    Recover
   │     │     │         │
   │     │     └────┬────┘
   │     │          ▼
   │     │        Emit
   │     │          │
   │     └──────────┘
   │
   └────→ Stopped
```

---

## 5. 核心组件详解

### 5.1 RuntimeConfig

**纯基础设施配置**，不包含任何业务模块的字段：

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `AGENT_RUNTIME_ENABLED` | `false` | 是否启用 Runtime |
| `AGENT_RUNTIME_TICK_SECONDS` | `15` | 心跳间隔（秒） |
| `AGENT_RUNTIME_MAX_ERRORS` | `5` | 连续错误熔断阈值 |
| `GUARD_TIMEZONE` | `Asia/Shanghai` | 时间判断时区 |
| `GUARD_ACTIVE_START_HOUR` | `9` | 工作日活跃开始 |
| `GUARD_ACTIVE_END_HOUR` | `22` | 工作日活跃结束 |
| `GUARD_WEEKEND_START_HOUR` | `10` | 周末活跃开始 |
| `GUARD_WEEKEND_END_HOUR` | `20` | 周末活跃结束 |

业务模块的配置（如 `GUARD_GREET_INTERVAL_PEAK`）由模块自己读取，不经过 RuntimeConfig。

### 5.2 AgentRuntime 类

```python
class AgentRuntime:
    """Generic, long-lived Agent Runtime — the 'OS kernel' of Pulse."""

    def __init__(self, *, event_emitter, config):
        ...

    # 模块调用此方法注册任务（唯一耦合点）
    # ADR-004 §6.1.1: enabled 默认 False — 注册 ≠ 启用;启停由 IM 独占控制
    def register_patrol(self, *, name, handler, peak_interval,
                        offpeak_interval,
                        enabled: bool = False,
                        active_hours_only: bool = True,
                        workspace_id: str | None = None,
                        token_budget: int = 4000):
        ...

    # 生命周期
    def start(self) -> bool: ...
    def stop(self) -> bool: ...
    def status(self) -> dict: ...
    async def trigger_once(self) -> list[str]: ...

    # 熔断恢复
    def reset_circuit_breaker(self, task_name) -> bool: ...

    # Per-patrol 对话式控制面 (ADR-004 §6.1, ✅ 已落地 2026-04-22)
    def list_patrols(self) -> list[dict]: ...
    def get_patrol_stats(self, name) -> dict | None: ...
    def enable_patrol(self, name) -> bool: ...
    def disable_patrol(self, name) -> bool: ...
    def run_patrol_once(self, name) -> dict: ...
```

**per-patrol API 语义边界**(ADR-004 §6.1.7 不变量):

- 内部心跳 `__runtime_heartbeat__` 不可通过以上 API 操作,防止 runtime 自锁
- `enable/disable/trigger` 对未注册 name return fail-loud `False` / `{ok:false}`,不自动创建、不吞错
- `enable` 只把 `ScheduleTask.enabled` 翻为 `True`,不绕过业务层 killswitch(如 `PULSE_BOSS_AUTOREPLY=off` 仍会让 handler return `{status:"disabled"}`)
- `run_patrol_once` 走完整 5-stage 流水线(熔断开则 L0 skip),阻塞返回

**注册与启用的解耦**(ADR-004 §6.1.1):`register_patrol(enabled=False)` 是默认与推荐形态 — 模块在 `on_startup` 中**无条件注册**,启停独占交给 IM 对话面。这保证 `list_patrols` 对控制面可见,避免"env 默认关 → on_startup early return → 控制面看到空列表 → `enable` 必然 not found"的死路(见 `trace_0cf87040e0e5` 回归修复)。`enabled=True` 仅在测试夹具和内部心跳场景使用。

### 5.3 错误恢复框架（借鉴 Claude Code）

分四级，成本从低到高：

| 级别 | 策略 | 成本 | 触发条件 |
|------|------|------|----------|
| L0 | Skip | 零 | 非活跃时段、任务禁用 |
| L1 | Retry with backoff | 低 | 网络超时、API 429 |
| L2 | Degrade | 中 | handler 返回 `{ok: false}` |
| L3 | Abort + Circuit Break | 高 | 连续 N 次失败 |

**递减收益检测**（防死循环）：
- 连续 `AGENT_RUNTIME_MAX_ERRORS`（默认 5）次失败 → circuit breaker 打开
- 发射 `runtime.patrol.circuit_breaker` 事件
- 可通过 API `POST /api/runtime/reset/{task_name}` 手动恢复

### 5.4 EventBus 可观测性

Runtime 发射的**通用事件**（不含任何业务模块名称）：

| 事件类型 | 时机 | payload |
|----------|------|---------|
| `runtime.lifecycle.started` | Runtime 启动 | `{tasks, config}` |
| `runtime.lifecycle.stopped` | Runtime 停止 | `{reason, uptime_sec}` |
| `runtime.patrol.started` | 任一任务开始 | `{task_name, trace_id}` |
| `runtime.patrol.completed` | 任一任务成功 | `{task_name, outcome, elapsed_ms}` |
| `runtime.patrol.failed` | 任一任务失败 | `{task_name, outcome, error}` |
| `runtime.patrol.circuit_breaker` | 熔断触发/恢复 | `{task_name, action}` |
| `runtime.patrol.lifecycle.enabled` | `enable_patrol` 成功 (ADR-004 §6.1, ✅) | `{task_name}` |
| `runtime.patrol.lifecycle.disabled` | `disable_patrol` 成功 (ADR-004 §6.1, ✅) | `{task_name}` |
| `runtime.patrol.lifecycle.triggered` | `run_patrol_once` 进入 `_execute_patrol` 前 (ADR-004 §6.1, ✅) | `{task_name}` |

### 5.5 API 控制面

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/runtime/status` | 运行状态、所有任务列表、统计 |
| POST | `/api/runtime/start` | 手动启动 |
| POST | `/api/runtime/stop` | 手动停止 |
| POST | `/api/runtime/trigger` | 手动触发一次心跳 |
| POST | `/api/runtime/reset/{task}` | 重置某任务的熔断器 |
| GET | `/api/runtime/patrols` | 列出所有已注册 patrol(排除内部心跳) (ADR-004 §6.1, ✅) |
| GET | `/api/runtime/patrols/{name}` | 单条 patrol 状态快照 (ADR-004 §6.1, ✅) |
| POST | `/api/runtime/patrols/{name}/enable` | 开启某条 patrol (ADR-004 §6.1, ✅) |
| POST | `/api/runtime/patrols/{name}/disable` | 关闭某条 patrol (ADR-004 §6.1, ✅) |
| POST | `/api/runtime/patrols/{name}/trigger` | 立即跑一次 patrol (ADR-004 §6.1, ✅) |

### 5.6 对话式控制面(ADR-004 §6.1, ✅ 已落地 2026-04-22)

上面 §5.5 的 HTTP 路由给 CLI / 前端 / 运维脚本用;**同语义**的对话式控制面让 Brain 直接处理用户说的 "开启自动回复 / 后台在跑什么 / 现在就跑一次 job_chat" 一类自然语言指令。

**实现路径**:`src/pulse/modules/system/patrol/module.py` 作为 `BaseModule` 暴露 5 个 `IntentSpec`:

| IntentSpec | 对等 HTTP | mutates | risk | requires_confirmation |
|---|---|---|---|---|
| `system.patrol.list` | `GET /patrols` | F | 0 | F |
| `system.patrol.status(name)` | `GET /patrols/{name}` | F | 0 | F |
| `system.patrol.enable(name, trigger_now=true)` | `POST .../enable` | T | 2 | **T** |
| `system.patrol.disable(name)` | `POST .../disable` | T | 1 | F |
| `system.patrol.trigger(name)` | `POST .../trigger` | T | 2 | **T** |

**`enable` 的 `trigger_now` 编排**(ADR-004 §6.1.4, 2026-04-22 M9.2):runtime 内核 API `enable_patrol(name)` 签名保持 `-> bool` 不变;"enable 同时立刻跑一次"的编排由 `PatrolControlModule._enable_handler` 组合 `enable_patrol + run_patrol_once` 完成。默认 `trigger_now=true` —— 用户表达"开启自动回复"的语义本意是"启动并让我看到它工作",如果仅翻 `ScheduleTask.enabled=true` 并等下一个 `peak_interval_seconds`(典型 180s)用户会读成"没生效"。显式 `trigger_now=false` 用于"挂起来先别跑"场景。

**为什么不走 MCP**:Brain、`BaseModule`、`AgentRuntime` 同进程;MCP 跨进程序列化开销零收益,而且会丢失 runtime 内存引用。Kernel 内部控制面 → in-process `IntentSpec`;跨进程副作用 → MCP(如 `boss_platform_server`)。

**行业对标**:
- **ChatGPT Scheduled Tasks**:用户对话触发"每天早上 8 点帮我总结新闻"类定时任务,所有 start/stop/list/重新调度走对话
- **LangGraph thread-cron**:以 `thread_id` 为 key 挂载 cron 任务,通过 graph 的 `interrupt / resume` 机制在对话里控制生命周期

Pulse 的 `system.patrol.*` IntentSpec 是这两种思路在 in-process runtime 上的收敛:既保留 LLM 自由 tool_use,又复用已有的 5-stage patrol 执行管线。

**共存而非替代**:`job_chat.patrol` 现在仍然用 `JobChatService.run_process`(LLM-first),`run_auto_reply_cycle`(rule-based)通过独立 MCP tool 消费;两条路径的比较与迁移交给未来 ADR-005 依据真实使用数据决定(ADR-004 §6.1.3 决策 C)。

---

## 6. 应用案例：BOSS 直聘巡检

以下是 BOSS 模块如何作为"应用程序"接入 Runtime "操作系统"的具体案例。

### 6.1 job_greet 模块接入

模块在 `on_startup` 中**无条件**注册(ADR-004 §6.1.1):

```python
def on_startup(self) -> None:
    if not self._runtime:
        return
    # 无条件 register_patrol; 初始 enabled=False (runtime 默认值);
    # 启停由 IM 经 system.patrol.enable/disable 独占控制。
    self._runtime.register_patrol(
        name="job_greet.patrol",
        handler=self._patrol,
        peak_interval=int(self._settings.patrol_greet_interval_peak),
        offpeak_interval=int(self._settings.patrol_greet_interval_offpeak),
    )
```

**模块级配置**(`JobSettings`,不影响启停):

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `PULSE_JOB_PATROL_GREET_INTERVAL_PEAK` | `900` | 高峰间隔(秒,调度节拍) |
| `PULSE_JOB_PATROL_GREET_INTERVAL_OFFPEAK` | `1800` | 低峰间隔(秒,调度节拍) |

注意**没有**启停 env — 启停唯一来源是 IM(单一认知路径)。

### 6.2 job_chat 模块接入

```python
def on_startup(self) -> None:
    if not self._runtime:
        return
    self._runtime.register_patrol(
        name="job_chat.patrol",
        handler=self._patrol,
        peak_interval=int(self._settings.patrol_chat_interval_peak),
        offpeak_interval=int(self._settings.patrol_chat_interval_offpeak),
    )
```

**模块级配置**:

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `PULSE_JOB_PATROL_CHAT_INTERVAL_PEAK` | `180` | 高峰间隔(秒) |
| `PULSE_JOB_PATROL_CHAT_INTERVAL_OFFPEAK` | `600` | 低峰间隔(秒) |

业务层 killswitch(如 `PULSE_BOSS_MCP_REPLY_MODE=manual_required`)作用在 handler 内部,与 `ScheduleTask.enabled` 正交,属于"紧急阻断"层,不承担日常启停。

### 6.3 BOSS 巡检全流程

```
boss_greet.patrol:
  scan_jobs → match_threshold → greet_job → record → emit

boss_chat.patrol:
  pull_conversations → LLM_classify → auto_reply → record → emit
```

这些业务逻辑完全在模块内部，Runtime 只看到 handler() 返回成功或抛出异常。

---

## 7. 扩展性

### 7.1 如何新增一个定时任务

任何新模块只需三步接入 Runtime：

1. **实现 patrol handler**：
   ```python
   def _patrol(self) -> dict[str, Any]:
       # 你的业务逻辑
       return {"ok": True, "items_processed": 42}
   ```

2. **在 on_startup 中无条件注册**(ADR-004 §6.1.1):
   ```python
   def on_startup(self) -> None:
       if not self._runtime:
           return
       # 不读 env guard; 初始 enabled=False; 启停交给 IM
       self._runtime.register_patrol(
           name="my_module.patrol",
           handler=self._patrol,
           peak_interval=600,
           offpeak_interval=1800,
       )
   ```

3. **可选**:只在 `.env` 暴露**调度节拍**类配置(interval peak/offpeak),**不要**为启停新增 env — 启停走 IM。

无需修改 Runtime、server.py 或任何其他文件。用户通过 `system.patrol.enable my_module.patrol` 启用它。

### 7.2 未来可能的扩展场景

| 场景 | 模块 | patrol 行为 |
|------|------|------------|
| 日历/面试提醒 | calendar_agent | 定时检查日历事件，发送提醒 |
| 技术雷达 | intel_techradar | 已实现，定时采集技术趋势 |
| 简历投递跟踪 | application_tracker | 定时检查投递状态更新 |
| 邮件监控 | email_monitor | 定时拉取未读邮件并分类 |
| 自我进化 | evolution_engine | 定时收集偏好数据并微调 |

所有这些都通过相同的 `register_patrol()` 接口接入，Runtime 代码无需任何改动。

---

## 8. 与现有架构的关系

### 8.1 复用（不新建）

| 组件 | 角色 |
|------|------|
| `BackgroundSchedulerRunner` | Runtime 的底层执行引擎 |
| `ScheduleTask` + `SchedulerEngine` | 到期判断、峰/离峰感知 |
| `windows.py` | 时间窗口工具函数 |
| `BaseModule.on_startup/on_shutdown` | 模块生命周期钩子 |
| `server.py lifespan` | 统一触发模块启动/关闭 |
| `EventBus` | 可观测性基础设施 |
| `CostController` | 成本预算控制 |

### 8.2 新增

| 组件 | 说明 |
|------|------|
| `core/runtime.py` | AgentRuntime 类 + RuntimeConfig + PatrolOutcome |
| `BaseModule.bind_runtime()` | 模块获取 Runtime 引用 |
| `/api/runtime/*` 路由 | Runtime 控制面 |

### 8.3 不动

| 组件 | 说明 |
|------|------|
| `Brain` | 保持独立 |
| `Memory` | 四层不变 |
| `MCP / 连接器` | 保持独立 |

---

## 9. 配置架构

配置分两层，Runtime 内核与业务模块完全独立：

```
┌─────────────────────────────────────────────┐
│ Runtime 层（core/runtime.py 读取）           │
│                                             │
│  AGENT_RUNTIME_ENABLED=true                 │
│  AGENT_RUNTIME_TICK_SECONDS=15              │
│  AGENT_RUNTIME_MAX_ERRORS=5                 │
│  GUARD_ACTIVE_START_HOUR=9                  │
│  GUARD_ACTIVE_END_HOUR=22                   │
│  GUARD_TIMEZONE=Asia/Shanghai               │
├─────────────────────────────────────────────┤
│ 模块层（各模块自己读取）                      │
│                                             │
│  GUARD_GREET_ENABLED=true        ← boss_greet│
│  GUARD_GREET_INTERVAL_PEAK=900   ← boss_greet│
│  GUARD_CHAT_ENABLED=true         ← boss_chat │
│  GUARD_CHAT_INTERVAL_PEAK=180    ← boss_chat │
│  ...                                        │
│  MY_NEW_MODULE_PATROL_ENABLED=true ← 未来模块│
└─────────────────────────────────────────────┘
```
