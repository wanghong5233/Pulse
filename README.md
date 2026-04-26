<h1 align="center">Pulse</h1>

<p align="center">
  <strong>开源的长驻个人 AI 助手 · 持久记忆 · 主动执行 · 内核 + 技能包架构</strong>
</p>

<p align="center">
  <a href="./README.en.md">English</a>&nbsp;·&nbsp;
  <a href="#为什么是-pulse">Why</a>&nbsp;·&nbsp;
  <a href="#核心能力">核心能力</a>&nbsp;·&nbsp;
  <a href="#系统架构">架构</a>&nbsp;·&nbsp;
  <a href="#核心设计">核心设计</a>&nbsp;·&nbsp;
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
- **已落地技能包**:求职(BOSS 扫 JD / 主动打招呼 / HR 对话自动回复 / 发简历)、情报订阅(多主题 YAML 装配的六步流水线 + 跨主题检索)、邮件追踪(IMAP 分类 + 日程抽取);
- **真正的技术深度**在三个地方 —— 三契约工具调用(反 Agent 幻觉)、Layer × Scope 五层记忆、AgentRuntime 长程内核(patrol + 熔断 + 事件总线)。

这只是开端。**每一个值得被自动化的生活场景都是 Pulse 的下一站** —— 代理游戏日常签到、端到端生成旅行攻略、财务周报、联动智能家居,甚至自动生成新技能、沉淀出属于你个人的使用习惯与偏好。一个内核,承载无限技能包。

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
- **承诺可验证**:三契约工具调用约束 LLM "口头承诺却未真正调用工具" 这类 Agent 幻觉,所有不一致都进事件流
- **生态兼容**:对外暴露 MCP Server,对内接入 MCP Client,与 Claude Desktop / Cursor / 任意 MCP 客户端即插即用
- **可自扩展**:Skill Generator 把自然语言需求转化为校验过、沙箱跑过的新工具,热加载到工具表

---

## 核心能力

- **长期运行**:后台 patrol 调度、任务启停、熔断恢复、手动接管。
- **可靠工具调用**:三契约工具使用,降低"说做了但没做"的 Agent 幻觉。
- **长期记忆**:Layer × Scope 记忆模型,把短期上下文、用户偏好、领域事实分层管理。
- **安全治理**:SafetyPlane 在真实外部副作用前执行 Allow / Deny / Ask,支持用户确认后恢复执行。
- **可观测**:EventBus + JSONL 审计 + SSE,关键路径可回放。
- **MCP 生态**:既能作为 MCP Server 暴露工具,也能作为 MCP Client 接入外部服务。
- **浏览器自动化**:Patchright 驱动真实浏览器,当前已用于求职场景。

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
│  │  AgentRuntime         ──→ 调度 / active hours / 熔断    │             │
│  │  Task Runtime (Brain) ──→ ReAct 推理 + ToolUseContract  │             │
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

Python 主体单进程内运行(asyncio + 一个调度线程),Chromium 与外部 MCP Server 是被派生的独立子进程。

### 内核五层

| 层级 | 职责 |
|---|---|
| **Agent OS** | 长驻运行、patrol 调度、active hours、熔断、事件广播 |
| **Task Runtime** | 单轮执行状态机、工具调用循环、预算与停止条件、三契约 |
| **Memory Runtime** | 多层记忆读写、压缩、晋升、来源追踪 |
| **Observability Plane** | 事件总线 + append-only 审计 + 订阅推送 |
| **SafetyPlane** | side-effect 授权闸门 + Suspend / Ask / Resume / Reexecute 四步 HITL 原语 |

---

## Agent 内核核心设计

### 1. ToolUseContract —— 反 Agent 幻觉的三条正交契约

LLM 最常见的 bug 不是工具坏了,而是"嘴上说调用了但实际没调":回复"已为你记录偏好",但本轮根本没发出工具调用。纯 prompt 约束在多轮对话里必然失守。

Pulse 用三条正交契约兜底,任一层失守由下一层接住:

| 契约 | 做什么 |
|---|---|
| **A. Description** | 工具自己声明前置条件 / 反例 / 何时不该用,而不是让 LLM 猜该选哪个 |
| **B. Call** | 在调用层强制结构化:看到"纯文本但承诺了动作"的回合,自动升级到必须调工具 |
| **C. Execution Verifier** | 回复前自检"承诺 vs 实际工具记录",不一致则改写为坦诚说明,并落审计事件 |

不变式:语义判断归 LLM,结构判断归 Python;不允许用关键词正则去强制调工具。

详见 [ADR-001](./docs/adr/ADR-001-ToolUseContract.md)。

### 2. Layer × Scope 双轴记忆

主流 Agent 记忆是"Core + 向量检索"两层,所有东西塞同一个袋子。Pulse 把记忆按两个独立维度拆开:

- **Layer 轴(决定生命周期)**:Operational(本轮) / Recall(短期对话) / Workspace(领域事实) / Archival(长期事实) / Core(人格与偏好)。
- **Scope 轴(决定隔离粒度)**:turn / taskRun / session / workspace / global。

**Workspace × workspace** 是接入点:业务域(job / mail / 未来的 game / health)挂自己的领域记忆 facade,内核只提供通用读写,不感知业务语义。新增技能包不需要改内核。

当前不使用向量库:在个人单用户规模下,LLM 生成关键词 + SQL 关键词检索的命中率与延迟优于向量召回,且依赖更轻。判断依据与未来切换阈值见 [MemoryRuntime 设计](./docs/Pulse-MemoryRuntime设计.md)。

### 3. AgentRuntime —— 让 Agent 真正长期运行

cron + while True 不是长驻 Agent,只是定时器。AgentRuntime 把以下能力做成内核能力:

- **patrol 自注册**:模块声明自己的巡检任务,内核负责调度、启停、错误恢复,内核不感知业务。
- **active hours**:任务级时间窗口,可按工作日 / 周末分别配置,避开非工作时段的无意义触发与风控。
- **熔断与恢复**:多档恢复策略(重试 / 降级 / 跳过 / 回滚 / 熔断 / 人工接管),失败不会拖垮整个进程。
- **对话式控制面**:通过 IM 直接管理后台任务(查询、启停、手动触发),不需要单独管理后台。
- **统一事件流**:所有关键路径都通过事件总线广播,落审计文件,同时支持实时订阅。

详见 [AgentRuntime 设计](./docs/Pulse-AgentRuntime设计.md)。

### 4. SafetyPlane —— Service 层授权闸门 + 四步 HITL 原语

当 Agent 接入真实外部账号(求职、邮箱、消息平台)后,每一条发出去的消息、每一次点击都会到达真人。这一层要回答两个问题:**什么动作可以直接做、什么动作必须人确认后才能做**。

常见的三步 Ask(Suspend / Ask / Resume)在产品里会走到"假成功":Ask 挂起后,下一轮巡检以为已经处理过、不会再拾起;Agent 侧标记"已发送",外部连接器实际从未调用。把闸门放在 Brain 层,又会漏掉所有由后台巡检触发的副作用。

Pulse 的做法:

- **闸门下沉到 Service 层** —— 真实副作用入口处过 policy,覆盖交互触发与巡检触发两条路径。
- **policy 是纯函数** —— Allow / Deny / Ask 三态决策,不做 I/O、不调 LLM,可单测、可审计。
- **HITL 是四步原语**:Suspend(挂起任务,带幂等键避免重复打扰)→ Ask(独立通道发出确认请求)→ Resume(用户答复分类为同意 / 拒绝 / 未知,未知保守视作拒绝)→ **Reexecute**(同意后立即把原意图重跑一次,带审计标记)。

第四步的 Reexecute 是关键不变式:用户确认不是把状态改成"成功",而是真的把动作重新执行一次,确认流程与真实执行不脱节。

详见 [ADR-006](./docs/adr/ADR-006-v2-SafetyPlane.md)。

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

| 子能力 | 做什么 |
|---|---|
| **greet** | BOSS 直聘岗位扫描 → 两层 JD 过滤(规则 + LLM)→ 主动打招呼 |
| **chat** | 拉取未读消息 → 意图分类 → 画像匹配回复 / 发简历 / HR 卡片确认 → 发送结果二次校验 |
| **profile** | 求职画像、硬约束、简历原文与结构化摘要 |
| **connectors/boss** | Patchright 持久化登录态、风控识别、DOM 快照驱动选择器 |

### 情报域 `modules/intel/`

单一 Module + 主题 YAML 装配,新增主题(秋招、面经、大模型前沿等)只加配置文件不改代码。

| 能力 | 做什么 |
|---|---|
| **digest** | 按主题跑确定性六步流水线:fetch → dedup → score → summarize → diversify → publish,RSS / GitHub Trending / Web Search 多渠道,反信息茧房(信源配额 + serendipity + 反主流加权)作为 workflow 一等公民,高分条目自动沉淀到 ArchivalMemory |
| **search** | 跨主题关键词检索(LLM 抽词 → SQL ILIKE),Brain ReAct 与外部 MCP 客户端(Claude Desktop / Cursor)同步可用 |

### 邮件域 `modules/email/tracker/`

IMAP 只读接入 → 邮件分类(面试邀请 / 拒信 / 补材料)→ 日程结构化抽取 → 状态同步 + IM 提醒。

### 系统域 `modules/system/`

| 子能力 | 做什么 |
|---|---|
| **hello** | 健康探针 |
| **feedback** | 反馈闭环,驱动偏好学习 |
| **patrol** | 通过 IM 直接管理后台任务(查询 / 启停 / 手动触发) |

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
PULSE_SAFETY_PLANE=enforce
```

### 连到 Claude Desktop / Cursor(作为 MCP Server)

Pulse 内部工具自动注册为 MCP Tool,对外可用。连接配置示例(Claude Desktop `claude_desktop_config.json`):

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
src/pulse/
  core/          # AgentRuntime / Brain / Memory / Safety / MCP / EventBus
  modules/       # job / intel / email / system 等技能包
  tools/         # 轻量内置工具
docs/            # 设计文档与 ADR
tests/pulse/     # 单元、集成、合同测试
```

---

## 技术栈

Python 3.11 · FastAPI · PostgreSQL · asyncio · Patchright · MCP · OpenAI / Qwen3-Max / DeepSeek · Docker Compose

---

## 文档

- [文档入口](./docs/README.md)
- [内核架构](./docs/Pulse-内核架构总览.md)
- [MemoryRuntime](./docs/Pulse-MemoryRuntime设计.md)
- [SafetyPlane / ADR-006](./docs/adr/ADR-006-v2-SafetyPlane.md)
- [工程宪法](./docs/code-review-checklist.md)

## Roadmap

> Pulse 的第一站是求职,但架构预留的舞台远比求职大。下面是未来可插拔的技能包方向,每一条都是独立的 Module 目录,每一条都欢迎 PR。

### 已发布 · v0 基线

- [x] 长驻 Agent OS 内核:AgentRuntime + 事件总线 + 任务级时间窗口
- [x] 三环工具模型:内置工具 / 业务模块 / 外部 MCP,叠加自然语言生成新工具
- [x] 三契约工具调用,约束 Agent "口头承诺却未真正调用工具" 类幻觉
- [x] Layer × Scope 双轴记忆 + 独立 Observability Plane
- [x] SOUL 人格分级与进化引擎,治理门控分自治 / 监督 / 强同意三档
- [x] MCP Server + Client 双向互通
- [x] 求职技能包:BOSS 扫 JD / 主动打招呼 / HR 对话自动回复 / 发简历 / 邮件追踪
- [x] SafetyPlane:授权闸门 + Suspend / Ask / Resume / Reexecute 四步 HITL 原语

### 进行中 · 内核加固

- [ ] SafetyPlane 抽成跨模块共享原语,推广到邮件 / 简历发送等同样产生外部副作用的路径
- [ ] 更多接入渠道:Discord / Telegram / WebSocket 长连(HTTPS webhook 版本的企业微信、飞书已落地)
- [ ] 求职域情报二期:面经站点深度抓取、公司维度调研


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

- [ ] **Skill Generator v2** —— 从"生成单个工具"演进至"LLM 生成完整业务模块(含巡检 / 记忆 / 意图)",实现 Agent 自我扩展
- [ ] **MCP 生态即插即用** —— GitHub / Notion / Slack / Linear / Sentry 等任意 MCP Server,Pulse 全部可调
- [ ] **跨设备同步** —— 移动端轻量通道 + 云端 Pulse 主进程,出行在外亦可实时响应
- [ ] **偏好微调闭环** —— 真实对话 → 偏好对 → 微调 pipeline,Agent 按你的风格越用越像你

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
- 🎨 **Prompt / LLM 研究者** —— SOUL 人格进化、偏好学习与采集、三契约里的执行验证器,都是 LLM 工程化的前沿问题
- 📚 **文档 / 布道者** —— 中英双语 README、ADR 翻译、教程视频、示例项目,任意形式的贡献都欢迎

提交 PR 前请阅读 [`docs/code-review-checklist.md`](./docs/code-review-checklist.md) —— **工程宪法**:编码与可维护性、测试、注释、**系统可配/可观测/可关机**;仓库级落地项另见 [`docs/engineering/pulse-conventions.md`](./docs/engineering/pulse-conventions.md)。标准较高,适合重视代码质量的贡献者。

架构类改动请先开 RFC issue 讨论,ADR 机制见 [`docs/adr/`](./docs/adr/)。

---

## 致谢

设计过程中吸收了以下项目的经验:

- **Letta / MemGPT** —— Core Memory Block 与 self-edit 思路
- **Claude Code** —— 单轮 Agent pipeline 的工程化实践与停机条件分类
- **Anthropic MCP** —— 工具协议设计
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
