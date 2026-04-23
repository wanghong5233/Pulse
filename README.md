<h1 align="center">Pulse</h1>

<p align="center">
  <strong>开源的"钢铁侠贾维斯" —— 一个会长期陪你、越用越懂你的个人 AI 助手</strong>
</p>

<p align="center">
  <a href="./README.en.md">English</a>&nbsp;·&nbsp;
  <a href="#为什么是-pulse">Why</a>&nbsp;·&nbsp;
  <a href="#核心能力">核心能力</a>&nbsp;·&nbsp;
  <a href="#系统架构">架构</a>&nbsp;·&nbsp;
  <a href="#agent-内核三大硬骨头">内核</a>&nbsp;·&nbsp;
  <a href="#快速开始">快速开始</a>&nbsp;·&nbsp;
  <a href="#功能清单">功能</a>&nbsp;·&nbsp;
  <a href="#roadmap">Roadmap</a>&nbsp;·&nbsp;
  <a href="./docs/README.md">文档</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/MCP-Server%20%26%20Client-8A2BE2" alt="MCP" />
  <img src="https://img.shields.io/badge/LLM-OpenAI%20%7C%20Qwen3--Max-orange" alt="LLM" />
  <img src="https://img.shields.io/badge/Browser-Patchright%20CDP%20Stealth-FF6A00" alt="Patchright" />
  <img src="https://img.shields.io/badge/Milestone-M0--M8%20%E2%9C%93-success" alt="Milestone" />
  <img src="https://img.shields.io/badge/License-MIT-yellow" alt="License" />
</p>

<p align="center">
  关键词: <code>AI Agent</code> · <code>Personal AI Assistant</code> · <code>JARVIS</code> · <code>Self-Evolving Agent</code> ·
  <code>ReAct</code> · <code>Tool Use Contract</code> · <code>Agent Memory</code> ·
  <code>Model Context Protocol (MCP)</code> · <code>Patrol / Heartbeat</code>
</p>

---

## Pulse 是什么

Pulse 是一个**跑在你自己机器上的个人 AI 助手** —— 目标是成为一个会长期驻留、真正理解你、帮你把各种琐事扛下来的贾维斯。

为了做到这一点,Pulse 把一个通用的 **Agent 内核**和按领域挂载的**业务技能包**彻底分离:

- **内核**(`core/`)领域无关 —— 长程调度、五层记忆、契约化工具调用、事件审计、人格进化一体;
- **技能包**(`modules/`)按领域组目录,新增场景只加目录不改框架;
- **当前已落地的第一个技能包是求职** —— BOSS 直聘扫 JD、主动打招呼、HR 对话自动回复、发简历、邮件追踪全闭环;
- **真正的技术深度**在三个地方 —— 三契约工具调用(反 Agent 幻觉)、Layer × Scope 五层记忆、AgentRuntime 长程内核(patrol + 熔断 + 事件总线)。

求职只是第一个场景。等内核稳下来,查天气、订行程、调研公司、自动签到、甚至让 Agent 自己生成新技能,都会在同一个内核上跑。

---

## 为什么是 Pulse

现在的 Agent 项目大概分三类,都有明显痛点:

| 类型 | 代表 | 痛点 |
|---|---|---|
| 垂直自动化脚本 | 各种 BOSS / 简历 / 电商自动化项目 | 换场景就重写,没有复用价值 |
| Agent Framework | LangChain / LangGraph / AutoGen | 工具箱而非产品,自己还得从 0 搭 |
| Chat 式助手 | ChatGPT / Claude desktop | 不能长期驻留,不做定时巡视,没有持久记忆与业务闭环 |

Pulse 想做的是"**把 Agent 从聊天窗口拽出来,变成一个会长期陪你的助手**":

- **会主动干活**:AgentRuntime 内核长驻后台,在工作时段自动巡逻新消息、定时采集情报,不需要你去点刷新
- **有真正的记忆**:Layer × Scope 双轴记忆,跨会话记住你是谁、你讨厌什么公司、上次谈到哪,而不是每轮 reset
- **不做假动作**:三契约架构(ADR-001)把"LLM 口头答应了却没真调工具"这类 Agent 幻觉钉死在可观测的事件流里
- **生态兼容**:对外暴露 MCP Server,对内接 MCP Client,跟 Claude Desktop / Cursor / 任意 MCP 客户端即插即用
- **自己长新能力**:Skill Generator 把自然语言需求变成 AST 校验 + 沙箱测试过的新工具,热加载进工具注册表

> "Agent OS" 不是营销词。Pulse 的 `core/` 里真的有调度内核(`core/runtime.py`)、进程隔离、事件总线、熔断、patrol 生命周期,且 `core/` 代码里**不会出现任何业务词汇**(boss / 简历 / 求职),这是架构硬约束。

---

## 核心能力

| 能力 | 说明 | 实装状态 |
|---|---|---|
| **Agent OS 内核** | `AgentRuntime` 长驻,patrol 任务自注册,active hours + 熔断 + 事件总线 | ✅ M0-M3 |
| **ReAct 推理 + 三环工具** | Brain 推理循环 · Ring1 内置工具 · Ring2 Module · Ring3 外部 MCP | ✅ M4 |
| **三契约工具调用** | Description(`when_to_use`) + Call(`tool_choice`) + Execution Verifier(commitment 审查) | ✅ A · ✅ B · ✅ C v2.1 |
| **五层记忆系统** | Operational / Recall / Workspace / Archival / Core + Layer × Scope 双轴 | ✅ M5 |
| **Observability Plane** | 独立事件总线 + 按天滚动 JSONL 审计 + in-memory 滑窗 + SSE 实时流 | ✅ |
| **MCP Server + Client** | 内置 Tool 即 MCP Tool,对外被 Claude Desktop / Cursor 等直连;对内也接入外部 MCP | ✅ |
| **Skill Generator** | 自然语言 → 代码 → AST 白名单 → 沙箱 → 热加载 | ✅ M6 |
| **自进化引擎** | SOUL `[CORE]`/`[MUTABLE]` 分级 · Autonomous/Supervised/Gated 治理 · 偏好学习 Track A · DPO 采集 Track B | ✅ M7 |
| **浏览器自动化** | Patchright(Playwright 分支,CDP 层反检测),BOSS 直聘实测通过 | ✅ |
| **多渠道接入** | HTTP / SSE / CLI / 飞书 · 消息意图路由 exact → prefix → LLM | ✅ |
| **HITL 治理** | Policy Engine L0-L5 门控 · approval / rollback / 版本化规则 + 差异对比 | ✅ M7 |

---

## 系统架构

### 全景图

```
┌──────────────────────────────────────────────────────────────────────────┐
│                     Pulse (单 Python 进程)                              │
│                                                                          │
│  ┌──────────────────────── 接入层 ────────────────────────────┐         │
│  │  HTTP API │ SSE 事件流 │ CLI │ 飞书 Adapter │ MCP Server   │         │
│  └──────────────────────────┬───────────────────────────────┘         │
│                              ↓                                           │
│  ┌──────────────────── Agent OS 内核 ────────────────────────┐         │
│  │                                                            │         │
│  │  AgentRuntime  ──→  调度 / active hours / patrol 生命周期 │         │
│  │       │                                                    │         │
│  │       ▼                                                    │         │
│  │  Task Runtime (Brain)  ──→  ReAct 推理循环                │         │
│  │       │                      + ToolUseContract (A/B/C)    │         │
│  │       ▼                                                    │         │
│  │  Memory Runtime        ──→  Layer × Scope 五层记忆        │         │
│  │       │                                                    │         │
│  │       ▼                                                    │         │
│  │  Observability Plane   ──→  EventBus + JSONL + InMemStore │         │
│  │       │                                                    │         │
│  │       ▼                                                    │         │
│  │  SafetyPlane (规划中)  ──→  订阅事件流做策略门控           │         │
│  │                                                            │         │
│  └────────────────────────────────────────────────────────────┘         │
│                              ↓                                           │
│  ┌──────────────── 三环能力模型 (Brain 的 tool list) ────────┐         │
│  │  Ring 1 Tool         轻量级内置函数 (alarm/weather/web…)  │         │
│  │  Ring 2 Module       业务技能包 (job/intel/email/system)  │         │
│  │  Ring 3 External MCP 任意外部 MCP Server (GitHub/Notion…) │         │
│  │  Meta   SkillGen     自然语言 → 新工具热加载              │         │
│  └────────────────────────────────────────────────────────────┘         │
│                              ↓                                           │
│  ┌──────────────── Capability Layer (领域无关) ─────────────┐         │
│  │  LLM Router · Browser Pool · Storage · Notify · Scheduler│         │
│  │  Channel · Policy · EventBus · Cost · Config             │         │
│  └──────────────────────────────────────────────────────────┘         │
│                                                                          │
├──────────────────────────────────────────────────────────────────────────┤
│  PostgreSQL (业务数据 + 记忆)  │  JSON(Core Memory)  │  外部 MCP Servers │
└──────────────────────────────────────────────────────────────────────────┘
```

### 内核五层(`docs/Pulse-内核架构总览.md`)

| 层级 | 负责 | 严禁混入 | 实装 |
|---|---|---|---|
| **Agent OS** | 长驻运行、patrol 调度、active hours、熔断、事件广播 | prompt 组装、事实晋升、用户偏好写入 | ✅ |
| **Task Runtime** | 单轮执行状态机、tool loop、hook、budget、stop reason、三契约 | cron 调度、长期事实 schema | ✅ |
| **Memory Runtime** | 五层记忆读写、压缩、晋升、evidence tracing | 任务调度、最终答案生成、审计落盘 | ✅ |
| **Observability Plane** | 事件总线 + append-only 审计落盘 + 订阅推送 | 存业务数据、做决策 | ✅ |
| **SafetyPlane** | policy gate、approval、rollback、manual takeover | 主动业务执行 | ⏳ 规划中 |

---

## Agent 内核三大硬骨头

这是 Pulse 区别于"又一个 LangChain Agent wrapper"的地方。三项都有独立 ADR / 设计文档,不是 vaporware。

### 1. ToolUseContract —— 反 Agent 幻觉的三条正交契约

**问题**:Agent 最常见的 bug 不是工具写错,而是 LLM "口头答应了却没真调工具" —— 比如告诉用户"已为你记录偏好",但 `tool_calls=[]`,记忆里啥都没写。纯 prompt 约束在复杂对话里一定会失守。

**Pulse 的答案**([ADR-001](./docs/adr/ADR-001-ToolUseContract.md)):三条契约正交布防,任一失守由下一层兜住。

| 契约 | 落点 | 干什么 |
|---|---|---|
| **A. Description** | `ToolSpec.when_to_use / when_not_to_use` + PromptContract 三段式渲染 + 反例 few-shot | 从"让 LLM 猜工具选哪个"变成"工具自己声明前置条件" |
| **B. Call** | `LLMRouter.invoke_chat(tool_choice=...)` + 按 ReAct 步数结构化 escalation + hand-off | 看到"纯文本空 tool 轮"就 escalate 到 `tool_choice="required"`;工具间传 `scan_handle` 复用结果 |
| **C. Execution Verifier** | 回复前 LLM 自评 `commitment vs used_tools`,不一致改写为坦诚说明 + 发 `brain.commitment.unfulfilled` 落盘审计 | 兜住 A/B 都失守的边缘情况,任何"假动作"都会被坦诚改写,不给用户糊弄 |

**不变式**:语义判断归 LLM,结构判断归 Python。host 侧**禁止**用关键词/正则匹配用户意图去强制调工具。

每一次契约失守都有真实 trace 驱动(ADR 里登记了 `trace_e48a6be0c90e / 16e97afe3ffc / 4890841c2322` 等生产 case),这是**被生产 bug 打磨出来的**防线。

### 2. Layer × Scope 双轴记忆

业界主流 Agent 记忆是"Core + 向量检索"两层,塞所有东西进一个"memory"袋子里。Pulse 用**双轴**拆分:

**Layer 轴**(存在哪一层,决定生命周期):

| Layer | 生命周期 | 存储 | 检索路径 |
|---|---|---|---|
| Operational | turn / run 内 | 内存 | 直接读 |
| Recall | 中期 | PostgreSQL | recent + agentic search (ILIKE) |
| Workspace | 中长期 | PostgreSQL | KV facts + summary 直接读 |
| Archival | 长期 | PostgreSQL | SPO 精确过滤 + agentic keyword |
| Core | 长期 | JSON | block 直接读(SOUL / USER / PREFS / CONTEXT) |

**Scope 轴**(属于哪个作用域,决定隔离):`turn` / `taskRun` / `session` / `workspace` / `global`

**关键单元** —— `Workspace × workspace`:业务域(job / mail / health...)在这里挂 DomainMemory facade,内核不感知业务语义,只提供 KV/summary CRUD。新增技能包 = 在这个单元上挂一个新 facade。

**不走 embedding**:Pulse 当前全量用 agentic search(LLM 生关键词 → SQL ILIKE),实测在单用户 ≤ 10⁴ 行规模下 P95 < 10 ms,向量库带来的收益抵不过依赖体积和训练/推理成本。决策依据 + 未来切换阈值见 [`Pulse-MemoryRuntime设计.md`](./docs/Pulse-MemoryRuntime设计.md) 附录 B。

### 3. AgentRuntime —— 操作系统级的长程驱动

让 Pulse 在后台"一直醒着"的内核。不是简单的 cron + while True。

- **自注册**:任意模块 `on_startup` 里 `register_patrol(...)` 即接入,内核不 import 任何业务模块
- **active hours**:工作日 9-22、周末 10-20 自动加频巡视,深夜沉睡,避免凌晨刷 BOSS 被风控
- **对话式控制面**([ADR-004](./docs/adr/ADR-004-AutoReplyContract.md) §6.1):通过 IM 发"查一下后台有哪些任务"、"关一下 BOSS 自动回复"即可控制,不用开后台面板。`system.patrol.*` IntentSpec 暴露 `list / status / enable / disable / trigger` 五个动作。
- **熔断 + 恢复梯度**:`retry` / `degrade` / `skip` / `abort` / `rollback` / `circuitBreak` / `manualTakeover` 七级策略
- **Observability Plane 独立**:任何层写事件都经 `EventBus.publish`,两个默认订阅者:`InMemoryEventStore`(滑窗 2000 条给 WS/SSE)+ `JsonlEventSink`(按天滚动,只持久化 `llm./tool./memory./policy./promotion.` 前缀)

---

## 记忆与进化

```
┌─────────────────────────────────────────────────────────────────┐
│  每轮 ReAct 推理前:                                             │
│    加载 Core (SOUL + USER + PREFS + CONTEXT)                    │
│      + Recall 最近 N 轮摘要 + Archival 相关事实                 │
│      + Workspace × workspace 的 DomainMemory facts              │
│    → 拼成 system prompt 注入 LLM                                │
│                                                                  │
│  推理结束后:                                                    │
│    Recall 追加对话 + 工具调用                                   │
│    Operational 清理 turn 级 scratchpad                          │
│    Workspace/Archival/Core 按 Promotion Pipeline 晋升           │
│    事件同步进 EventBus → InMemStore + JsonlSink                 │
│                                                                  │
│  用户纠正 ("以后别推游戏公司"):                                 │
│    → 偏好学习 Track A:纠正检测 → 规则提取 → PREFS 更新          │
│    → 治理门控 (Autonomous/Supervised/Gated) 决定是否立即生效    │
│    → 变更写审计日志,可回滚、可版本对比                          │
└─────────────────────────────────────────────────────────────────┘
```

**人格分级**:SOUL 里每条信念带标签

```yaml
values:
  - "[CORE]    用户利益优先,不做损害用户的事"
  - "[CORE]    诚实,不确定的事明确说不确定"
  - "[MUTABLE] 优先推荐远程工作机会"
```

`[CORE]` 信念永不被反思 Pipeline 修改,`[MUTABLE]` 可通过反馈与反思进化 —— 越用越懂你,同时兜住底线。

---

## 功能清单

Pulse 当前的业务技能包聚焦求职场景,但底层内核已经是通用的,新增领域只加目录。

### 求职域 `modules/job/`

| 子能力 | 做什么 | 入口 |
|---|---|---|
| **greet** | BOSS 直聘岗位扫描 → JD 两层漏斗过滤(规则硬过滤 + LLM 二元判断,反评分阈值困境) → 详情页完整 JD 抽取 → 主动打招呼 | `job.greet.scan` / `job.greet.trigger` |
| **chat** | 拉未读消息 → LLM 意图分类 → 画像匹配回复/发简历/HR 卡片同意/升级通知 → 回复后 DOM verify 反假送达 | `job.chat.run_process` / `system.patrol.*` |
| **profile** | JobMemory 三类存储: hard constraints / memory items / 简历原文 + parsed 摘要 | `job.memory.record` / `job.hard_constraint.set` / `job.resume.update` |
| **connectors/boss** | Patchright 持久化 context · Cookie 单点登录 · 风控识别 · DOM 快照驱动 selector | 仅领域内使用 |

### 情报域 `modules/intel/`

| 子能力 | 做什么 |
|---|---|
| **interview** | 面经情报采集 → LLM 提取考察点 → 按公司聚合 → 飞书日报 |
| **techradar** | 技术雷达(RSS / GitHub Trending / 公众号)→ LLM 相关度打分 → 摘要 → 日报 |
| **query** | 情报语义检索 + 分类过滤 |

### 邮件域 `modules/email/tracker/`

IMAP 只读接入 → 邮件 LLM 分类(面试邀请 / 拒信 / 补材料)→ 日程结构化抽取 → 状态同步 + 飞书提醒。

### 系统域 `modules/system/`

| 子能力 | 做什么 |
|---|---|
| **hello** | 健康探针 |
| **feedback** | 反馈闭环,驱动偏好学习 Track A |
| **patrol** | `system.patrol.*` 对话式 patrol 控制面(ADR-004 §6.1) |

---

## 快速开始

### 前置

- Python 3.11+
- PostgreSQL(本地或 Docker)
- OpenAI 或 Qwen3-Max API Key(至少一个)

### 30 秒启动(开发模式)

```bash
git clone https://github.com/<your-org>/pulse.git
cd pulse

cp .env.example .env
# 编辑 .env 填入 OPENAI_API_KEY 或 DASHSCOPE_API_KEY, 以及 DATABASE_URL

pip install -e .[dev]
pulse start
```

API 文档: <http://localhost:8010/docs>
健康探针: <http://localhost:8010/health>
实时事件流(SSE): <http://localhost:8010/api/agent/events>

### Docker 一键

```bash
docker compose up --build
```

### 开启求职自动化(可选)

```bash
# 首次登录 BOSS(浏览器扫码,Cookie 自动持久化到 ~/.pulse/boss_browser_profile)
./scripts/boss_login.sh

# 在 .env 打开 patrol
PULSE_JOB_PATROL_GREET_ENABLED=true
PULSE_JOB_PATROL_CHAT_ENABLED=true
AGENT_RUNTIME_ENABLED=true

# 重启后, AgentRuntime 自动进入 active hours,高峰 15min 一次巡检
```

### 连到 Claude Desktop / Cursor(作为 MCP Server)

Pulse 的所有 Ring 1 / Ring 2 工具都经 `@tool` 装饰器自动注册为 MCP Tool,对外可用。连接配置示例(Claude Desktop `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "pulse": {
      "command": "pulse",
      "args": ["mcp", "serve"]
    }
  }
}
```

连上后,Claude / Cursor 里可以直接调用 `job.greet.scan`、`memory.search` 等 Pulse 工具。

---

## 项目结构

```
Pulse/
├── src/pulse/
│   ├── core/                    ← 领域无关的内核
│   │   ├── runtime.py           AgentRuntime OS 内核
│   │   ├── brain.py             ReAct 推理循环
│   │   ├── task_context.py      Task Runtime 状态机
│   │   ├── prompt_contract.py   Prompt Contract 体系
│   │   ├── verifier.py          CommitmentVerifier (契约 C)
│   │   ├── memory/              五层记忆 (operational/recall/workspace/archival/core)
│   │   ├── soul/                人格 SOUL + Evolution Pipeline
│   │   ├── learning/            偏好学习 Track A + DPO 采集 Track B
│   │   ├── tool.py              ToolRegistry + @tool 装饰器
│   │   ├── mcp_client.py        MCP Client (对接外部 Server)
│   │   ├── mcp_server.py        MCP Server (对外暴露 Pulse 工具)
│   │   ├── events.py            EventBus + InMemoryEventStore
│   │   ├── event_sinks.py       JsonlEventSink (按天滚动审计)
│   │   ├── policy.py            Policy Engine (L0-L5 门控)
│   │   ├── cost.py              日 LLM 预算 + 自动降级
│   │   ├── router.py            意图路由 (exact/prefix/LLM)
│   │   ├── channel/             多渠道 (HTTP/CLI/飞书)
│   │   ├── browser/             Patchright 浏览器池
│   │   ├── llm/                 多模型路由 + 失败降级
│   │   ├── skill_generator.py   自然语言 → 工具热加载
│   │   └── sandbox.py           AST + subprocess 沙箱
│   │
│   ├── modules/                 ← 业务技能包 (可插拔)
│   │   ├── job/                 求职域: greet / chat / profile / _connectors/boss
│   │   ├── intel/               情报域: interview / techradar / query
│   │   ├── email/tracker/       邮件域
│   │   └── system/              系统域: hello / feedback / patrol
│   │
│   ├── tools/                   ← Ring 1 内置工具 (alarm/weather/web/flight…)
│   └── mcp_servers/             MCP Server runtime (BOSS 浏览器 runtime 等)
│
├── docs/
│   ├── README.md                文档导航入口
│   ├── Pulse-内核架构总览.md    ★ 顶层架构索引
│   ├── Pulse-AgentRuntime设计.md
│   ├── Pulse-MemoryRuntime设计.md
│   ├── Pulse-DomainMemory与Tool模式.md
│   ├── adr/                     ADR-001 ~ 005
│   ├── engineering/             工程化实践 (测试指南等)
│   ├── modules/                 模块级设计文档
│   └── dom-specs/               外部页面真实 DOM 快照 (selector fixture)
│
├── config/                      运行时配置 (router / policy / soul.yaml 等)
├── tests/pulse/                 单元 + 集成 + 合同测试
├── scripts/                     运维脚本 (start / boss_login 等)
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

---

## 技术栈

| 层级 | 选型 | 说明 |
|---|---|---|
| Agent 内核 | 自研 `core/runtime.py` | 不绑任何 Agent framework,完全可读可改 |
| 推理 | ReAct + 三环工具模型 | 契约化工具调用,非黑盒 chain |
| 模型 | OpenAI / Qwen3-Max / DeepSeek | LLMRouter 多模型主备降级,按任务类型路由 |
| 工具协议 | **MCP**(Model Context Protocol) | 对外暴露 + 对内接入 |
| 浏览器 | **Patchright** | Playwright 分支,CDP 层消除反爬指纹 |
| 后端 | **FastAPI** + async | HTTP API + SSE 事件流 |
| 存储 | **PostgreSQL** | 业务数据 + Recall + Workspace + Archival + 审计 |
| 人格 / 核心记忆 | YAML + JSON | 文本可读,便于版本管理与治理回滚 |
| 可观测 | EventBus + JSONL + InMemoryEventStore | 独立 Observability Plane,与记忆解耦 |
| 部署 | 单 Python 进程 · Docker Compose | 无 K8s 依赖,自举零门槛 |

---

## 实施里程碑

Pulse 按里程碑推进,**每个里程碑都是可运行的系统**。当前状态:

| 里程碑 | 范围 | 状态 |
|---|---|---|
| M0 | 项目骨架 · 模块系统 · EventBus | ✅ |
| M1 | Capability 抽取(LLM/Storage/Browser/Scheduler/Notify) | ✅ |
| M2 | Module 迁移 · V1 清理 | ✅ |
| M3 | 接入层 · 意图路由 · Policy Engine · Docker | ✅ |
| M4 | Brain ReAct · 三环工具 · MCP Client/Server · 成本控制 | ✅ |
| M5 | 五层记忆 · SOUL · Memory Tools | ✅ |
| M6 | Skill Generator · AST + 沙箱 | ✅ |
| M7 | 进化引擎 · 治理 · DPO 采集 · 版本化规则 | ✅ |
| M8 | ToolUseContract 三契约加固(A/B/C) | ✅ 除 M8.A 真实 trace 验证 |

详见 [`docs/Pulse实施计划.md`](./docs/Pulse实施计划.md)。

---

## 设计决策(为什么这么选)

| 决策 | 选择 | 原因 |
|---|---|---|
| Agent 记忆是否用向量库 | **不用,走 agentic search** | 单用户数据量小,LLM 语义理解 > 向量相似度,依赖体积更小 |
| 浏览器引擎 | **Patchright** 而非原生 Playwright | CDP 层反检测,BOSS 等反爬站点实测通过率最高 |
| 防 Agent 幻觉 | **三契约正交布防**(ADR-001) | 只靠 prompt 约束在生产里一定失守;结构化、可审计、可测试 |
| 记忆模型 | **Layer × Scope 双轴** | 一维"memory"袋子塞什么都混乱;双轴让生命周期与作用域正交 |
| 审计 | **Observability Plane 独立于 Memory** | 给 LLM 读的和给人/合规读的是两个系统,不应共用 schema |
| 工具协议 | **MCP-first** | 与 Claude Desktop / Cursor 生态对齐,未来可接入 10k+ 外部 Server |
| 长驻运行 | **自研 AgentRuntime 内核** 非 cron | cron 不懂 active hours / 熔断 / 事件 / 对话式控制 |
| 业务 vs 内核 | **`core/` 零业务词汇**(硬约束) | `.cursor/rules/pulse-architecture.mdc` 里作为 lint 规则执行 |

---

## 文档导航

- 项目总入口: [`docs/README.md`](./docs/README.md)
- 内核架构索引: [`docs/Pulse-内核架构总览.md`](./docs/Pulse-内核架构总览.md)
- AgentRuntime 设计: [`docs/Pulse-AgentRuntime设计.md`](./docs/Pulse-AgentRuntime设计.md)
- Memory 主设计: [`docs/Pulse-MemoryRuntime设计.md`](./docs/Pulse-MemoryRuntime设计.md)
- 业务 DomainMemory 规范: [`docs/Pulse-DomainMemory与Tool模式.md`](./docs/Pulse-DomainMemory与Tool模式.md)
- 测试工程化: [`docs/engineering/testing-guide.md`](./docs/engineering/testing-guide.md)
- ADR 目录: [`docs/adr/`](./docs/adr/)
- 编码 / 测试宪法: [`docs/code-review-checklist.md`](./docs/code-review-checklist.md)

---

## Roadmap

Pulse 的第一站是求职,但架构预留了远比求职大的舞台。

**已完成**

- [x] 通用 Agent OS 内核(AgentRuntime + 事件总线 + patrol 生命周期)
- [x] ReAct + 三环工具模型(Ring 1/2/3 + Meta SkillGen)
- [x] 三契约工具调用(ADR-001 A/B/C)
- [x] Layer × Scope 五层记忆 + Observability Plane 独立
- [x] SOUL 人格分级 + Evolution Engine + Gated/Supervised/Autonomous 治理
- [x] MCP Server + Client 双向
- [x] 求职技能包:BOSS 扫 JD / 主动打招呼 / HR 对话自动回复 / 发简历 / 邮件追踪

**进行中 / 近期**

- [ ] SafetyPlane 实装 —— 订阅 EventBus 做 policy gate + manual takeover
- [ ] 多渠道接入:企业微信长连 bot、Discord、Telegram
- [ ] 求职域情报:面经真实站点接入(牛客/脉脉)、公司深度调研 Module

**待落地的新技能包(架构已就绪)**

- [ ] 日历 / 闹钟 / 天气 / 行程助手(Ring 1 工具已在 `tools/`,组合场景待打磨)
- [ ] 智能家居 MCP 接入(Home Assistant / Matter)
- [ ] 财务周报 Module(银行/券商 API)
- [ ] 旅游攻略 Module(多源爬 + LLM 汇总)
- [ ] 游戏自动签到 / 日常任务 Module
- [ ] 本地模型离线模式(Ollama + Qwen 本地版)

**远期规划**

- [ ] Skill Generator 支持生成完整 Module(目前只支持生成 Ring 1 Tool)
- [ ] DPO Track B 实际接入训练 pipeline(Track A 偏好学习已落地)
- [ ] 跨设备同步(手机端轻量 channel + 云端 Pulse 主进程)

---

## 贡献

Pulse 正处在 v0 阶段,欢迎 Issue / PR,现在来正是时候。提交 PR 前请过一遍 [`docs/code-review-checklist.md`](./docs/code-review-checklist.md) —— 包含 Pulse 的编码宪法与测试宪法,对代码质量要求较高(反吞异常、反静默兜底、反虚假测试)。

架构类改动请先开 RFC issue 讨论,ADR 机制见 [`docs/adr/`](./docs/adr/)。

---

## 致谢

设计过程中吸收了以下项目的经验:

- **Letta / MemGPT** —— Core Memory Block 与 self-edit 思路
- **Claude Code Agentic Loop** —— per-turn pipeline + 9 种 StopReason + 成本感知恢复
- **OpenClaw** —— Heartbeat = 完整 Agent Turn + session 序列化
- **Anthropic MCP** —— 工具协议设计
- **NabaOS Tool Receipts** —— False Absence 幻觉检测
- **Bertrand Meyer《Design by Contract》** —— 契约化设计哲学

---

## License

[MIT](./LICENSE)

---

<p align="center">
  <strong>如果 Pulse 帮到了你,欢迎点一个 Star 支持,这是对开源作者最大的鼓励 ⭐</strong>
  <br/>
  <sub>Built for people who want an AI that stays, not one that forgets every conversation.</sub>
</p>
