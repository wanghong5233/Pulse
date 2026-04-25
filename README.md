<h1 align="center">Pulse</h1>

<p align="center">
  <strong>开源的长驻个人 AI 助手 · 持久记忆 · 主动执行 · 内核 + 技能包架构</strong>
</p>

<p align="center">
  <a href="./README.en.md">English</a>&nbsp;·&nbsp;
  <a href="#为什么是-pulse">Why</a>&nbsp;·&nbsp;
  <a href="#核心能力">核心能力</a>&nbsp;·&nbsp;
  <a href="#系统架构">架构</a>&nbsp;·&nbsp;
  <a href="#agent-内核四项核心设计">内核</a>&nbsp;·&nbsp;
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
  <img src="https://img.shields.io/badge/Status-Alpha%20%E2%80%A2%20usable-2ea44f" alt="Status" />
  <img src="https://img.shields.io/badge/PRs-welcome-ff69b4" alt="PRs welcome" />
  <img src="https://img.shields.io/badge/License-MIT-yellow" alt="License" />
</p>

<p align="center">
  关键词: <code>AI Agent</code> · <code>Personal AI Assistant</code> · <code>Self-Hosted AI</code> · <code>JARVIS</code> ·
  <code>Self-Evolving Agent</code> · <code>ReAct</code> · <code>Tool Use Contract</code> · <code>Agent Memory</code> ·
  <code>Model Context Protocol (MCP)</code> · <code>Agent Skill Pack</code> ·
  <code>Game Automation</code> · <code>Life Assistant</code> · <code>Patrol / Heartbeat</code>
</p>

---

## Pulse 是什么

Pulse 是一个**部署在你自己机器上的个人 AI 助手** —— 长期驻留、真正理解你、帮你承担日常琐事。目标是做一个"你自己的 JARVIS":不是一次性的对话机器人,而是一个会主动执行、持续学习、越用越懂你的长驻系统。

为了做到这一点,Pulse 把一个通用的 **Agent 内核**和按领域挂载的**业务技能包**彻底分离:

- **内核**(`core/`)领域无关 —— 长程调度、五层记忆、契约化工具调用、事件审计、人格进化一体;
- **技能包**(`modules/`)按领域组目录,新增场景只加目录不改框架;
- **当前已落地的第一个技能包是求职** —— BOSS 直聘扫 JD、主动打招呼、HR 对话自动回复、发简历、邮件追踪全闭环;
- **真正的技术深度**在三个地方 —— 三契约工具调用(反 Agent 幻觉)、Layer × Scope 五层记忆、AgentRuntime 长程内核(patrol + 熔断 + 事件总线)。

求职只是第一个场景。当内核稳定下来后,**每一个值得被自动化的生活场景都是 Pulse 的下一站** —— 代理游戏日常签到、端到端生成旅行攻略、跟踪邮箱与财务、联动智能家居,甚至自动生成新技能、沉淀出属于你个人的使用习惯与偏好。一个内核,承载无限技能包。

---

## 为什么是 Pulse

现在的 Agent 项目大概分三类,都有明显痛点:

| 类型 | 代表 | 痛点 |
|---|---|---|
| 垂直自动化脚本 | 各种 BOSS / 简历 / 电商自动化项目 | 换场景就重写,没有复用价值 |
| Agent Framework | LangChain / LangGraph / AutoGen | 偏"怎么编排一次请求",离"你自己的长驻 AI 助手"还差一层产品化胶水 |
| Chat 式助手 | ChatGPT / Claude Desktop | 无法长期驻留、无定时巡视、无持久化记忆与业务闭环 |

Pulse 想做的是"**把 Agent 从对话窗口中解耦出来,成为一个长期服务于你的助手**":

- **主动执行**:AgentRuntime 内核长驻后台,在工作时段自动巡检新消息、定时采集情报,无需手动刷新
- **持久化记忆**:Layer × Scope 双轴记忆,跨会话保留用户画像、偏好、上下文进度,不在每轮对话后 reset
- **承诺可验证**:三契约架构(ADR-001)把"LLM 口头承诺却未真正调用工具"这类 Agent 幻觉约束到可观测事件流中
- **生态兼容**:对外暴露 MCP Server,对内接入 MCP Client,与 Claude Desktop / Cursor / 任意 MCP 客户端即插即用
- **可自扩展**:Skill Generator 将自然语言需求转化为 AST 校验 + 沙箱测试过的新工具,热加载至工具注册表

> "Agent OS" 并非营销包装。Pulse 的 `core/` 中实装了调度内核(`core/runtime.py`)、进程隔离、事件总线、熔断机制、patrol 生命周期,且 `core/` 代码中**不包含任何业务词汇**(boss / 简历 / 求职),这是通过 lint 规则强制的架构约束。

---

## 核心能力

| 能力 | 说明 | 实装状态 |
|---|---|---|
| **Agent OS 内核** | `AgentRuntime` 长驻,patrol 任务自注册,active hours + 熔断 + 事件总线 | ✅ |
| **ReAct 推理 + 三环工具** | Brain 推理循环 · Ring1 内置工具 · Ring2 Module · Ring3 外部 MCP | ✅ |
| **三契约工具调用** | Description(`when_to_use`) + Call(`tool_choice`) + Execution Verifier(commitment 审查) | ✅ |
| **五层记忆系统** | Operational / Recall / Workspace / Archival / Core + Layer × Scope 双轴 | ✅ |
| **Observability Plane** | 独立事件总线 + 按天滚动 JSONL 审计 + in-memory 滑窗 + SSE 实时流 | ✅ |
| **MCP Server + Client** | 内置 Tool 即 MCP Tool,对外被 Claude Desktop / Cursor 等直连;对内也接入外部 MCP | ✅ |
| **Skill Generator** | 自然语言 → 代码 → AST 白名单 → 沙箱 → 热加载 | ✅ |
| **自进化引擎** | SOUL `[CORE]`/`[MUTABLE]` 分级 · Autonomous/Supervised/Gated 治理 · 偏好学习 Track A · DPO 采集 Track B | ✅ |
| **浏览器自动化** | Patchright(Playwright 分支,CDP 层反检测),BOSS 直聘实测通过 | ✅ |
| **多渠道接入** | HTTP / SSE / CLI / 企业微信 / 飞书 · 消息意图路由 exact → prefix → LLM | ✅ |
| **HITL 治理** | Policy Engine L0-L5 门控 · approval / rollback / 版本化规则 + 差异对比 | ✅ |
| **SafetyPlane** | Service 层 side-effect 闸门 · Suspend-Ask-Resume-Reexecute 四步 HITL 原语 · Ask 幂等去重 | ✅ |

---

## 系统架构

### 全景图

```
┌────────────────────────────────────────────────────────────────────────────┐
│  Pulse 主 Python 进程   (FastAPI / uvicorn · 单进程 + asyncio + 后台线程)  │
│                                                                            │
│  ┌──────────────────────── 接入层 ──────────────────────────┐             │
│  │  HTTP API │ SSE │ CLI │ 企业微信/飞书 Adapter │ MCP Server │           │
│  └───────────────────────────┬──────────────────────────────┘             │
│                              │                                             │
│                              ▼                                             │
│  ┌──────────────────── Agent OS 内核 ───────────────────────┐             │
│  │  AgentRuntime    ──→ 调度 / active hours / patrol 生命周期│             │
│  │    · 后台守护线程 `pulse-scheduler-runner`(tick≈15s)      │             │
│  │    · 每个 tick 以 asyncio 执行到期 patrol(chat/greet…)    │             │
│  │                                                          │             │
│  │  Task Runtime (Brain) ──→ ReAct 推理循环                 │             │
│  │                           + ToolUseContract (A/B/C)      │             │
│  │  Memory Runtime       ──→ Layer × Scope 五层记忆         │             │
│  │  Observability Plane  ──→ EventBus + JSONL + InMemStore  │             │
│  │  SafetyPlane          ──→ Service 闸门 + Suspend-Ask-    │             │
│  │                           Resume-Reexecute 四步 HITL 原语 │             │
│  └───────────────────────────┬──────────────────────────────┘             │
│                              │                                             │
│                              ▼                                             │
│  ┌──────────── 三环能力模型 (Brain 的 tool list) ───────────┐             │
│  │  Ring 1 Tool         轻量级内置函数 (alarm/weather/web…) │             │
│  │  Ring 2 Module       业务技能包 (job/intel/email/system) │             │
│  │  Ring 3 External MCP 任意外部 MCP Server(跨进程调用)     │             │
│  │  Meta   SkillGen     自然语言 → 新工具热加载             │             │
│  └──────────────────────────────────────────────────────────┘             │
│                                                                            │
│  ┌──────────── Capability Layer (领域无关) ─────────────────┐             │
│  │  LLM Router · Browser Pool · Storage · Notify · Scheduler│             │
│  │  Channel · Policy · EventBus · Cost · Config             │             │
│  └──────────────────────────────────────────────────────────┘             │
└───────────┬─────────────────────┬──────────────────────┬──────────────────┘
            │  stdio / subprocess │  CDP (WebSocket)     │  TCP/Unix socket
            ▼                     ▼                      ▼
   ┌────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐
   │ 外部 MCP Server│  │  Chromium OS 子进程  │  │  PostgreSQL          │
   │ (各自独立进程) │  │  (Patchright 派生,   │  │  业务数据 · 记忆      │
   │ GitHub/Notion..│  │   处理 BOSS / 网页)  │  │  审计(JSONL 落盘)    │
   └────────────────┘  └──────────────────────┘  └──────────────────────┘
```

> **并发模型一句话说明**：Pulse 的 Python 代码**全部在一个进程内**,通过 asyncio + 一个守护线程跑所有 patrol 任务(如 BOSS 自动回复 / 自动投递),**不会**为每个 patrol 新起 Python 进程;真正会被派生成独立 OS 子进程的,只有 Patchright 驱动的 **Chromium** 和通过 stdio 接入的 **外部 MCP Server**。这是 Python GIL 下的典型部署形态,也是 Claude Desktop / Cursor 等同类项目的通用做法。

### 内核五层(`docs/Pulse-内核架构总览.md`)

| 层级 | 职责 | 职责边界外 | 状态 |
|---|---|---|---|
| **Agent OS** | 长驻运行、patrol 调度、active hours、熔断、事件广播 | prompt 组装、事实晋升、用户偏好写入 | ✅ |
| **Task Runtime** | 单轮执行状态机、tool loop、hook、budget、stop reason、三契约 | cron 调度、长期事实 schema | ✅ |
| **Memory Runtime** | 五层记忆读写、压缩、晋升、evidence tracing | 任务调度、最终答案生成、审计落盘 | ✅ |
| **Observability Plane** | 事件总线 + append-only 审计落盘 + 订阅推送 | 业务数据存储、业务决策 | ✅ |
| **SafetyPlane** | Service 层 side-effect 闸门(policy)、Suspend-Ask-Resume-Reexecute 四步 HITL 原语、Ask 幂等去重 | 业务决策、Intent 构造 | ✅ |

---

## Agent 内核四项核心设计

这是 Pulse 区别于"又一个 LangChain Agent wrapper"的关键所在。四项设计均有独立 ADR / 设计文档背书,并非概念性承诺。

### 1. ToolUseContract —— 反 Agent 幻觉的三条正交契约

**问题**:Agent 最常见的 bug 并非工具实现错误,而是 LLM"口头承诺却未真正调用工具" —— 例如模型回复"已为你记录偏好",但 `tool_calls=[]`,记忆系统中并无任何写入。纯 prompt 约束在复杂多轮对话中必然失守。

**Pulse 的答案**([ADR-001](./docs/adr/ADR-001-ToolUseContract.md)):三条契约正交布防,任一失守由下一层兜住。

| 契约 | 落点 | 干什么 |
|---|---|---|
| **A. Description** | `ToolSpec.when_to_use / when_not_to_use` + PromptContract 三段式渲染 + 反例 few-shot | 从"让 LLM 猜工具选哪个"变成"工具自己声明前置条件" |
| **B. Call** | `LLMRouter.invoke_chat(tool_choice=...)` + 按 ReAct 步数结构化 escalation + hand-off | 看到"纯文本空 tool 轮"就 escalate 到 `tool_choice="required"`;工具间传 `scan_handle` 复用结果 |
| **C. Execution Verifier** | 回复前 LLM 自评 `commitment vs used_tools`,不一致则改写为坦诚说明 + 发出 `brain.commitment.unfulfilled` 事件落盘审计 | 兜底 A/B 均失守的边缘情况,任何未兑现的承诺都会被改写为诚实表述,杜绝对用户的误导 |

**不变式**:语义判断归 LLM,结构判断归 Python。host 侧**禁止**用关键词/正则匹配用户意图去强制调工具。

每一次契约的引入都由真实生产 trace 驱动(ADR 中登记了 `trace_e48a6be0c90e / 16e97afe3ffc / 4890841c2322` 等生产案例),这是**由真实生产问题沉淀而来的**防线。

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

**不使用 Embedding**:Pulse 当前全量采用 agentic search(LLM 生成关键词 → SQL ILIKE),实测在单用户 ≤ 10⁴ 行规模下 P95 < 10 ms,向量库带来的收益抵不过依赖体积与额外推理成本。决策依据与未来切换阈值见 [`Pulse-MemoryRuntime设计.md`](./docs/Pulse-MemoryRuntime设计.md) 附录 B。

### 3. AgentRuntime —— 操作系统级的长程驱动

让 Pulse 在后台持续运行的内核,远不止是简单的 cron + while True。

- **自注册机制**:任意模块在 `on_startup` 中调用 `register_patrol(...)` 即可接入,内核不 import 任何业务模块
- **active hours**:工作日 9-22、周末 10-20 自动加频巡检,深夜进入静默状态,避免凌晨访问 BOSS 触发风控
- **对话式控制面**([ADR-004](./docs/adr/ADR-004-AutoReplyContract.md) §6.1):通过 IM 发送"查询后台任务"、"停止 BOSS 自动回复"等指令即可控制,无需开启管理面板。`system.patrol.*` IntentSpec 暴露 `list / status / enable / disable / trigger` 五个动作。
- **熔断 + 恢复梯度**:`retry` / `degrade` / `skip` / `abort` / `rollback` / `circuitBreak` / `manualTakeover` 七级恢复策略
- **Observability Plane 独立**:任意层次的事件写入均通过 `EventBus.publish`,两个默认订阅者:`InMemoryEventStore`(滑窗 2000 条,供 WS/SSE 消费)+ `JsonlEventSink`(按天滚动,仅持久化 `llm./tool./memory./policy./promotion.` 前缀)

### 4. SafetyPlane —— Service 层授权闸门 + 四步 HITL 原语

**问题**:长驻 Agent 接入真实外部账号(求职 / 邮箱 / 银行)后,它发出去的每条消息、点击的每个按钮都会打到陌生人。常见的三步 Ask primitive(Suspend / Ask / Resume)在产品里会走到"假成功":Ask 挂起时已把消息从平台未读队列里踢出,下一轮 patrol 不会再拾起;Agent 侧视作"已发送",connector 侧实际从未调用。Brain 层做闸门又会漏掉 patrol 触发的 side-effect。

**Pulse 的答案**([ADR-006](./docs/adr/ADR-006-v2-SafetyPlane.md)):HITL 按四步原语落地,授权判决下沉到 Service 层 side-effect 入口。

| 原语 | 落点 | 不变式 |
|---|---|---|
| **Suspend** | `WorkspaceSuspendedTaskStore.create` | 以 `(workspace_id, module, trace_id, intent_name)` 为幂等键;二次 patrol 命中既有 `awaiting_user` 任务时跳过 `mark_processed` 与 `Notifier.send`,不重复骚扰用户 |
| **Ask** | `Notifier.send`(企业微信 / 飞书 webhook) | patrol 路径没有 IncomingMessage 上下文,Ask 通道独立于 channel adapter;幂等命中时不重发 |
| **Resume** | `server._dispatch_channel_message` 前置的 `try_resume_suspended_turn` | 用户答复分类为 `approve` / `decline` / `unknown`,`unknown` 保守视作拒绝 |
| **Reexecute** | 业务模块实现的 `ResumedTaskExecutor` 回调 | Resume 成功后立即把原 Intent 重跑到 connector,用 `run_id="resume-*"` 留审计;用户确认即显式授权,不再经 Brain / policy |

**闸门位置**:授权判决在 `JobChatService._execute_reply` / `_execute_send_resume` / `_execute_card` 三处 side-effect 入口,由 `safety.policies` 下的三条 Python 纯函数承载。Pulse 的触发面除 interactive 还有 patrol,Brain 层闸门必然漏 patrol,因此闸门下沉到 Service 层。

**不变式**:Brain 不参与授权判决;policy 纯函数不做 I/O、不调 LLM;`SuspendedTask.original_intent.args` 自包含,重跑不依赖 service 瞬时状态。

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
| **interview** | 面经情报采集 → LLM 提取考察点 → 按公司聚合 → IM 日报（企业微信 / 飞书） |
| **techradar** | 技术雷达(RSS / GitHub Trending / 公众号)→ LLM 相关度打分 → 摘要 → 日报 |
| **query** | 情报语义检索 + 分类过滤 |

### 邮件域 `modules/email/tracker/`

IMAP 只读接入 → 邮件 LLM 分类(面试邀请 / 拒信 / 补材料)→ 日程结构化抽取 → 状态同步 + IM 提醒（企业微信 / 飞书）。

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

# 重启后, AgentRuntime 自动进入 active hours,高频时段每 15 分钟执行一次巡检
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
│   │   ├── safety/              SafetyPlane (policies / suspended / resume / Reexecute)
│   │   ├── cost.py              日 LLM 预算 + 自动降级
│   │   ├── router.py            意图路由 (exact/prefix/LLM)
│   │   ├── channel/             多渠道 (HTTP/CLI/企业微信/飞书)
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
│   ├── adr/                     ADR-001 ~ 006
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
| 部署 | 单 Python 进程 · Docker Compose | 无 K8s 依赖,开箱即用 |

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
- SafetyPlane(HITL / 授权边界): [`docs/adr/ADR-006-v2-SafetyPlane.md`](./docs/adr/ADR-006-v2-SafetyPlane.md)
- 测试工程化: [`docs/engineering/testing-guide.md`](./docs/engineering/testing-guide.md)
- ADR 目录: [`docs/adr/`](./docs/adr/)
- 工程宪法(代码 / 测试 / 注释 / 系统形态): [`docs/code-review-checklist.md`](./docs/code-review-checklist.md)；Pulse 落地约定见 [`docs/engineering/pulse-conventions.md`](./docs/engineering/pulse-conventions.md)

---

## Roadmap

> Pulse 的第一站是求职,但架构预留的舞台远比求职大。下面是未来可插拔的技能包方向,每一条都是独立的 Module 目录,每一条都欢迎 PR。

### 已发布 · v0 基线

- [x] 通用 Agent OS 内核(AgentRuntime + 事件总线 + patrol 生命周期)
- [x] ReAct + 三环工具模型(Ring 1/2/3 + Meta SkillGen)
- [x] 三契约工具调用(ADR-001 A/B/C),反 Agent 幻觉
- [x] Layer × Scope 五层记忆 + Observability Plane 独立
- [x] SOUL 人格分级 + Evolution Engine + Gated/Supervised/Autonomous 治理
- [x] MCP Server + Client 双向互通
- [x] 求职技能包:BOSS 扫 JD / 主动打招呼 / HR 对话自动回复 / 发简历 / 邮件追踪
- [x] **SafetyPlane** —— Service 层 side-effect 闸门 + Suspend-Ask-Resume-Reexecute 四步 HITL 原语 + Ask 幂等去重(ADR-006)

### 进行中 · 内核加固

- [ ] **SafetyPlane 推广到第二个模块** —— 邮件 / 简历发送等同样产生外部可感知副作用的路径,把 `job_chat` 验证过的四步原语抽成共享 helper
- [ ] **多渠道接入** —— Discord · Telegram · 企业微信 WebSocket 长连 bot(HTTPS webhook 版本的企业微信 / 飞书已落地)
- [ ] **求职域情报二期** —— 面经真实站点(牛客/脉脉)、公司深度调研 Module
- [ ] **本地模型离线模式** —— Ollama + Qwen 本地版,断网也能跑

---

### 🎮 游戏自动化技能包

让 Pulse 变成**托管 SLG / MMO / 卡牌手游日常**的数字肝工。Patchright + DOM 脚手架已经在求职域跑通反爬链路,下一步把这套能力泛化给游戏场景。

- [ ] **通用游戏 Module 骨架** —— 多账号管理 · Cookie 持久化 · 窗口矩阵 · 验证码/滑块策略
- [ ] **日常任务编排 DSL** —— 用 YAML 描述一个游戏的"登录 → 签到 → 领取邮件 → 日常副本 → 抽卡保底 → 清理背包"流水线
- [ ] **跨服活动提醒** —— 订阅官方公告 + 企业微信 / 飞书推送"限时折扣"、"首充返利"、"限定卡池开启"
- [ ] **策略抽卡日志** —— 记录每张卡的概率分布 + 累计欧非值统计,给你做数据驱动的氪金决策
- [ ] **每款游戏一个子目录** —— `modules/game/<game_name>/`,PR 合并即上架。《原神》《明日方舟》《崩坏:星穹铁道》《FGO》《碧蓝档案》等任意 adapter 都在等人来写

### ✈️ 端到端生活助手

不是"问一句答一句"的 chatbot,而是**从一句意图到完整方案**的闭环。

- [ ] **端到端出行规划** —— 一句"十一想去趟大理",Pulse 自动规划:机票比价 · 酒店挑选 · 天气预警 · 签证/证件提醒 · 景点 + 小众路线 + 美食爬取 · 日程自动进日历 · 出发前 24h 动态复核
- [ ] **财务周报 Module** —— 银行/券商/支付宝 API · LLM 分析异常支出 · 周末推送结构化周报
- [ ] **健康守护** —— 睡眠/心率数据 · 体检报告结构化 · 异常指标主动问候
- [ ] **家居联动** —— Home Assistant / Matter MCP · 对话式开关灯/调空调/查电量
- [ ] **邮件邮箱收件助理** —— 面试邀请/账单/订阅自动分类 · 重要事件加日程 · 垃圾信息静音
- [ ] **学习陪伴** —— 订阅课程/论文/公众号 · 每日提炼要点 · 错题本式的主动复盘

### 🧑‍💻 扩展 & 自扩展

- [ ] **Skill Generator v2** —— 从"生成 Ring 1 Tool"演进至"**LLM 生成完整 Module 目录**(含 patrol / memory / intent)",实现 Agent 自主扩展能力
- [ ] **10k+ MCP 生态即插即用** —— GitHub / Notion / Slack / Linear / Sentry 等 Claude Desktop 能接的 MCP Server,Pulse 全部可以调用
- [ ] **跨设备同步** —— 手机端轻量 channel + 云端 Pulse 主进程,出行在外亦可实时响应
- [ ] **DPO Track B 闭环** —— 自动把真实对话 → 偏好对 → 喂给微调 pipeline,Agent 按你的风格越用越像你

### 🧬 前沿探索 · 通往真正的"贾维斯"

这部分不做时间承诺,是 Pulse 愿意为之持续投入研究的方向:

- [ ] **情商 / 共情层(Emotional Intelligence)** —— 不只是语气友好,而是识别你当下情绪并调节策略(低谷期不催你投简历,焦虑期主动屏蔽噪音信息)
- [ ] **人格进化(Personality Evolution)** —— SOUL `[MUTABLE]` 信念真正随时间推移、与用户共生演化,保留 `[CORE]` 兜底
- [ ] **睡眠记忆(Sleep-Time Consolidation)** —— 借鉴 Letta 的 Sleep-Time Compute,夜间自主整理白天的 Recall → Archival → Core,像真人睡眠般巩固长期记忆
- [ ] **具身能力(Embodied Agent)** —— 接入机器人控制/桌面控制(OpenAI Operator 风格),Agent 从"看得见"升级到"能动手"
- [ ] **多 Agent 协作(Agent Society)** —— 多个 Pulse 实例跨用户协作(求职 Agent ↔ 招聘 Agent / 出行 Agent ↔ 酒店 Agent),走 MCP + A2A 协议
- [ ] **记忆水印与遗忘权** —— 符合 GDPR 的"用户级一键遗忘",记忆回溯审计链

> 前沿探索并非空泛愿景。Pulse 的 SOUL 进化、五层记忆、EventBus 审计、治理门控等底座**已实装于当前代码库**,上述前沿方向是在这些底座之上增量演进,而非重新设计。

---

## 贡献

Pulse 正处于 v0 阶段,早期贡献者有机会参与到项目核心架构的塑造中。

不同背景的贡献者在 Pulse 中都能找到发力点:

- 🎮 **硬核手游玩家** —— 在 `modules/game/<game_name>/` 下提交游戏技能包,将签到 / 日常 / 抽卡自动化。Patchright 反爬脚手架已在求职域验证,可直接复用
- ✈️ **生活自动化爱好者** —— 从出行 / 健康 / 家居 / 财务中任选一个领域挂载 Module,实现"一句意图 → 完整方案"的闭环
- 🧑‍💻 **架构 / 内核贡献者** —— Brain / Memory / AgentRuntime / SafetyPlane / MCP 均有待深入的课题,每项均配有独立 ADR 作为上下文
- 🎨 **Prompt / LLM 研究者** —— SOUL 进化、偏好学习 Track A、DPO 采集 Track B、三契约 Commitment Verifier 是 LLM 工程化的前沿问题
- 📚 **文档 / 布道者** —— 中英双语 README、ADR 翻译、教程视频、示例项目,任意形式的贡献都欢迎

提交 PR 前请阅读 [`docs/code-review-checklist.md`](./docs/code-review-checklist.md) —— **工程宪法**:编码与可维护性、测试、注释、**系统可配/可观测/可关机**;仓库级落地项另见 [`docs/engineering/pulse-conventions.md`](./docs/engineering/pulse-conventions.md)。标准较高,适合重视代码质量的贡献者。

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
  <strong>如果 Pulse 对你有启发,欢迎 Star ⭐ —— 这是对开源项目最实在的支持</strong>
  <br/>
  <sub>Built for people who want an AI that stays, not one that forgets every conversation.</sub>
</p>
