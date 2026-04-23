# Pulse 架构设计方案

> 基于 V2 架构调研与决策文档提取，仅保留最终架构设计，去除调研对比过程。
> 完整调研过程见 `V2架构设计.md`。

---

## 一、需求分析

### 1.1 产品定位

Pulse 的终极形态是一个**通用个人智能体（Personal Agent）**——类似钢铁侠的 Pulse。当前阶段的任务是找工作，但这只是第一个"技能包"。底层架构必须是**领域无关、能力可插拔、自我进化**的通用 Agent 平台。

**两类能力并存**（这是与"纯 Pipeline 自动化平台"的根本区别）：

| 能力类型 | 核心模式 | 典型场景 | 触发方式 |
|---------|---------|---------|---------|
| **Pipeline（重型自动化）** | 定时/手动触发 → 多源采集 → LLM 处理 → 执行/通知 | 投递、面经采集、技术雷达、深度调研 | Scheduler 定时 / 用户指令 |
| **Tool Call（轻量级交互）** | 用户提问 → Brain 推理 → 调用工具 → 返回结果 | 设闹钟、查天气、播放音乐、创建日历事件、查航班 | 实时对话 |

**场景矩阵**：

| 场景 | 类型 | 触发 | 数据源/工具 | 处理 | 输出 |
|------|------|------|-----------|------|------|
| 求职：自动投递 | Pipeline | 定时 | Patchright → BOSS | LLM 匹配评分 | 打招呼 → 飞书通知 |
| 求职：面经情报 | Pipeline | 每日 08:00 | 当前实现：Web Search；目标态：Patchright → 牛客/脉脉 | LLM 提取考点 | 飞书日报推送 |
| 技术雷达 | Pipeline | 每日 09:00 | 当前实现：Web Search；目标态：RSS / GitHub / 公众号 | LLM 筛选摘要 | 飞书日报推送 |
| 深度调研 | Pipeline | 手动 | Web 搜索 + 爬取 | LLM 汇总分析 | 结构化报告 |
| 设置闹钟 | Tool Call | 对话 | `set_alarm(time, label)` | — | 确认回复 |
| 查询天气 | Tool Call | 对话 | `get_weather(city)` API | — | 天气信息 |
| 安排行程 | Tool Call | 对话 | `calendar.create_event(...)` | — | 日历已创建 |
| 播放音乐 | Tool Call | 对话 | MCP → Spotify Server | — | 播放中 |
| 组合任务 | Brain 多步 | 对话 | 查天气 → 查航班 → 建日程 | Brain 推理串联 | 出行方案 |
| 旅游攻略 | Pipeline | 手动 | 旅游网站 + 点评 | LLM 筛选推荐 | 攻略文档 |
| 财务管理 | Pipeline | 每周 | 银行/券商 API | LLM 分析趋势 | 周报推送 |
| 自扩展 | Meta | 对话 | "我需要监控 BTC 价格" | LLM 生成代码 | 新 Tool/Module 热加载 |

**架构要求**：
- 当前求职功能定义的架构底座，未来不需要重建
- 新增 Pipeline 场景（如旅游攻略）→ 新建一个 Module 目录
- 新增 Tool Call 场景（如查天气）→ 新建一个 Tool 函数（或接入 MCP Server）
- 新增组合能力 → Brain 自动编排已有 Tool + Module，无需新代码

### 1.2 阶段演进

| 阶段 | 定位 | 核心架构升级 | 核心用户价值 |
|------|------|------------|------------|
| Phase 1（已完成） | 求职执行 | 单体脚本 | BOSS 自动投递 + 自动回复 + 邮件追踪 |
| Phase 2（本次目标） | 求职 + 情报 | **Module + Capability + Router** | 面经采集 + 技术雷达 + 语义检索 |
| Phase 3（近期） | 通用助手 | **Router → Brain + Tool Registry + MCP Client + 基础记忆（Core/Recall Memory）** | 轻量级工具调用 + 多步推理 + 外部生态 + 跨会话记忆 |
| Phase 4（远期） | 自进化助手 | **Skill Generator（LLM 生成代码热加载）** | 用户自然语言创建新能力 |
| Phase 5（长期） | 自主进化 | **Evolution Engine（人格进化 + 偏好学习 + 时序事实图 + 可选 DPO 微调）** | 反思式自主进化、越用越聪明、个性化适应 |

> Phase 2 架构是 Phase 3/4/5 的**严格子集**。Router 是 Brain 的退化形式（单步推理 = ReAct 循环只执行一轮）。Module 不变。Capability Layer 只做增量新增。每次升级都是**增量式**的，不需要重写。

### 1.3 Phase 2 功能清单（已完成基线）

| 功能 | 触发方式 | 数据源 | 执行方式 | 输出 |
|------|---------|--------|---------|------|
| **岗位搜索 + JD 分析** | Guard 定时 / 用户手动 | BOSS 直聘搜索页 | Patchright 抓取 + LLM 解析评分 | 岗位列表入库 + 向量化 |
| **主动打招呼** | Guard 高峰期定时 | BOSS 直聘详情页 | Patchright 点击"立即沟通" + LLM 精筛 | 打招呼 + 审计记录 |
| **聊天智能回复** | Guard 3min 巡检 | BOSS 直聘聊天页 | Patchright 拉消息 + LLM 意图识别 + 四层门控 + 自动回复 | 回复/通知用户 |
| **邮件追踪** | EmailHeartbeat 独立线程 | IMAP 邮箱 | 拉取 + LLM 分类 + 日程提取 | 状态更新 + 飞书通知 |

### 1.4 Phase 2 新增需求

#### F1：面经情报流

| 项 | 内容 |
|----|------|
| **用户故事** | 作为求职者，我希望每天自动收到目标公司的面经热点 |
| **数据源** | 当前实现：通用 Web Search；目标态：牛客网、脉脉、小红书（反爬站点，需要 Patchright） |
| **采集频率** | 每天 1 次（08:00），可手动触发 |
| **处理逻辑** | URL 去重 → 内容相似度去重 → LLM 提取（轮次/考察点/编程题/难度）→ 按公司聚合 |
| **输出** | 每日面经简报 → 飞书卡片推送 |
| **存储** | 结构化面经条目入 PostgreSQL + 向量化入 ChromaDB |

#### F2：技术雷达流

| 项 | 内容 |
|----|------|
| **用户故事** | 作为开发者，我希望每天收到我关注领域的技术动态精选 |
| **数据源** | 当前实现：通用 Web Search；目标态：RSS 源、GitHub Trending、微信公众号 |
| **处理逻辑** | 拉取更新 → LLM 相关度评分 → 阈值过滤 → LLM 摘要 + 标签 → 聚合日报 |

#### F3：情报语义检索

| 项 | 内容 |
|----|------|
| **实现** | 面经 + 技术情报向量化入 ChromaDB → 用户提问 → 向量检索 + LLM 回答 |

### 1.5 Phase 3-5 需求

#### F6：Brain 推理引擎（Phase 3）

| 项 | 内容 |
|----|------|
| **核心变化** | Router（一次 LLM 分类）升级为 Brain（ReAct 推理循环：Think → Act → Observe → ...） |
| **推理循环** | ① 接收消息 + 加载记忆 → ② 看到所有能力 → ③ LLM 决定下一步 → ④ 执行工具 → ⑤ 结果追加上下文 → ⑥ 循环直到最终回复 |
| **终止条件** | LLM 返回纯文本 → 结束；安全阀：maxToolCalls = 20 / maxConsecutiveErrors = 3 |
| **记忆集成** | Brain 每轮推理前加载 Core Memory + 检索 Recall Memory，推理后自主决定是否更新记忆 |

#### F7：Tool Registry（Phase 3）

| 项 | 内容 |
|----|------|
| **与 Module 的区别** | Module = 重型 Pipeline；Tool = 轻量级单函数（无调度、无状态、毫秒级） |
| **工具格式** | MCP 标准：函数名 + 描述 + JSON Schema。`@mcp.tool` 装饰器自动生成 schema |
| **双重身份** | 既是 Brain 的 Tool，又是对外的 MCP Server Tool |

#### F8：MCP Client（Phase 3）

| 项 | 内容 |
|----|------|
| **实现方式** | Pulse 作为 MCP Client，连接外部 MCP Server；运行时自动发现外部工具并加入 Brain 能力菜单 |
| **配置** | `mcp_servers.yaml` 列出 MCP Server 地址，支持 stdio/HTTP+SSE/Streamable HTTP |
| **安全** | Policy Engine 门控：外部工具首次调用需用户确认 |

#### F9：Skill Generator（Phase 4）

| 项 | 内容 |
|----|------|
| **实现方式** | Meta-Tool：LLM 分析需求 → 生成代码 → AST 安全扫描 → 沙箱测试 → 热加载 |
| **安全五层防线** | ① AST 语法检查 → ② import 白名单 → ③ 模式检测 → ④ subprocess 沙箱（5s 超时 / 256MB）→ ⑤ 用户确认激活 |

#### F10：基础记忆系统（Phase 3）

| 项 | 内容 |
|----|------|
| **Core Memory** | 每轮推理加载的工作记忆：SOUL（人格）、USER（画像）、PREFS（偏好）、CONTEXT（任务） |
| **Recall Memory** | 全量对话历史 + 工具调用记录，按时间和语义双索引 |
| **记忆工具** | Brain 通过 `memory_read/memory_update/memory_search` 自主管理，与其他 Tool 同级 |
| **详细设计** | 见第六节 |

#### F11：人格进化与偏好学习（Phase 5）

| 项 | 内容 |
|----|------|
| **人格进化** | SOUL Block 中 `[MUTABLE]` 信念通过反思 Pipeline 进化；`[CORE]` 不可修改 |
| **偏好学习 Track A** | 纠正检测 → 规则提取 → PREFS Block 更新。无需 GPU |
| **偏好学习 Track B（可选）** | DPO 训练对收集。需 GPU 时接入微调管线 |
| **治理门控** | Autonomous / Supervised / Gated 三级 |
| **详细设计** | 见第六节 |

### 1.6 非功能需求

| 类别 | 需求 | 优先级 | 阶段 |
|------|------|--------|------|
| **通用可扩展性** | 新增场景加 Module/Tool/MCP，不改框架代码 | **P0** | Phase 2 |
| **领域无关性** | 核心框架不含任何求职业务逻辑 | **P0** | Phase 2 |
| **功能独立性** | Module/Tool 自包含，低耦合 | P0 | Phase 2 |
| **浏览器按需** | 不需要浏览器的功能不依赖浏览器 | P0 | Phase 2 |
| **安全** | 门控/审核机制 | P0 | Phase 2 |
| **MCP 兼容性** | 对外暴露 MCP Server + 对内接入 MCP Client | **P0** | Phase 3 |
| **多步推理** | Brain ReAct 循环串联多个 Tool/Module | **P0** | Phase 3 |
| **跨会话记忆** | 长期记忆，跨会话保持偏好和历史 | **P0** | Phase 3 |
| **人格一致性** | 可配置人格，跨会话保持一致 | P1 | Phase 3 |
| **自扩展能力** | 自然语言生成新 Tool/Module 并热加载 | P1 | Phase 4 |
| **偏好学习** | 从纠正/反馈/行为中学习 | P1 | Phase 5 |
| **部署简单** | 跨平台单机 + Docker 一键部署 | P1 | Phase 2 |
| **成本可控** | LLM 日消费硬上限 + 自动降级 | P1 | Phase 3 |

### 1.7 技术约束

**真约束（不可协商）**：

| 约束 | 为什么 |
|------|--------|
| **Python 运行时** | Patchright 是 Python 库 |
| **Patchright 用于 BOSS 直聘** | binary-level CDP 反检测是唯一可靠方案 |
| **浏览器会话进程内共享** | persistent_context 保持登录态，page 对象不可序列化 |

### 1.8 需求与架构映射

| 核心问题 | 来源需求 | 架构含义 |
|---------|---------|---------|
| **领域无关的框架** | 通用可扩展性 P0 | core/ 不出现求职术语 |
| **模块自包含** | 功能独立性 P0 | 一个模块 = 一个目录 |
| **多源异构采集** | F1/F2 | 可插拔采集器，有的要浏览器有的只要 HTTP |
| **浏览器按需池化** | Phase1 + F1 + F2 | 需要的从 Pool 获取，不需要的不碰 |
| **统一 LLM 调用** | 所有功能用 LLM | 统一 LLM 路由（按任务选模型、降级、预算） |
| **一次定义不重建** | 未来新场景 | 框架只定义接口，业务全在模块内 |

---

## 二、Phase 2 架构设计

### 2.1 设计原则

| 原则 | 含义 |
|------|------|
| **内核零业务** | `core/` 不出现任何领域词汇 |
| **Module 自包含** | 一个目录 = 一个功能的全部 |
| **Capability 按需** | Module 按需使用共享能力，不用的不碰 |
| **单进程单栈** | 一个 Python 进程，无外部运行时依赖 |
| **配置驱动** | 所有阈值/频率/开关通过配置文件控制 |
| **一次定义不重建** | 接口稳定后新增功能只加目录 |
| **不提前引入不需要的** | 当前不用的抽象一律不加 |

### 2.2 架构总览

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         Pulse (单 Python 进程)                              │
│                                                                              │
│  ┌────────────────────────── 接入层 ──────────────────────────┐              │
│  │                                                            │              │
│  │  ┌──────────┐  ┌────────────┐  ┌─────┐                    │              │
│  │  │ FastAPI   │  │ 飞书 SDK   │  │ CLI │                    │              │
│  │  │ (HTTP)    │  │ (WebSocket)│  │     │                    │              │
│  │  └─────┬────┘  └─────┬──────┘  └──┬──┘                    │              │
│  │        └──────────────┴────────────┘                       │              │
│  │                       ↓                                    │              │
│  │          Channel 统一格式化 → Router 意图路由 → Module     │              │
│  └────────────────────────────────────────────────────────────┘              │
│                                                                              │
│  ┌────────────────────── Module Registry ─────────────────────┐              │
│  │       启动时扫描 modules/ → 注册路由、任务、能力声明        │              │
│  └────────────────────────────────────────────────────────────┘              │
│                                                                              │
│  ┌───────────────── Capability Layer (共享能力) ──────────────┐              │
│  │                                                            │              │
│  │  ┌──────────┐ ┌────────┐ ┌─────────┐ ┌────────┐          │              │
│  │  │ Browser  │ │  LLM   │ │ Storage │ │Notifier│          │              │
│  │  │  Pool    │ │ Router │ │ Engine  │ │        │          │              │
│  │  │(Patchright│ │(多模型 │ │(PG+     │ │(飞书+  │          │              │
│  │  │ 反检测)  │ │ 降级)  │ │ Chroma) │ │Webhook)│          │              │
│  │  └──────────┘ └────────┘ └─────────┘ └────────┘          │              │
│  │                                                            │              │
│  │  ┌──────────┐ ┌──────────┐ ┌─────────┐ ┌───────────────┐ │              │
│  │  │Scheduler │ │ Channel  │ │ Router  │ │ Policy Engine │ │              │
│  │  │(时段感知 │ │(多渠道   │ │(LLM 意图│ │(safe/confirm/ │ │              │
│  │  │ 自注册)  │ │ 抽象)   │ │ 路由)   │ │ blocked 门控) │ │              │
│  │  └──────────┘ └──────────┘ └─────────┘ └───────────────┘ │              │
│  │                                                            │              │
│  │  ┌─────────────────┐  ┌──────────┐  ┌──────────────────┐ │              │
│  │  │   Event Bus     │  │ Config   │  │  Observability   │ │              │
│  │  │(SSE + pub/sub)  │  │(Pydantic │  │(trace + metrics  │ │              │
│  │  │                 │  │ Settings)│  │ + audit log)     │ │              │
│  │  └─────────────────┘  └──────────┘  └──────────────────┘ │              │
│  └────────────────────────────────────────────────────────────┘              │
│                                                                              │
│  ┌──────────────────── Modules (业务技能包) ──────────────────┐              │
│  │                                                            │              │
│  │  ┌─── job 域 ───┐ ┌── intel 域 ──┐ ┌─ email 域 ─┐         │              │
│  │  │ greet        │ │ interview    │ │ tracker    │         │              │
│  │  │ chat         │ │ techradar    │ │            │         │              │
│  │  │ _connectors/ │ │ query        │ │            │         │              │
│  │  │  └─ boss/    │ │              │ │            │         │              │
│  │  └──────────────┘ └──────────────┘ └────────────┘         │              │
│  │  ┌── system 域 ─┐ ┌──(未来)──────┐                        │              │
│  │  │ hello        │ │ finance /... │                        │              │
│  │  │ feedback     │ │              │                        │              │
│  │  └──────────────┘ └──────────────┘                        │              │
│  └────────────────────────────────────────────────────────────┘              │
│                                                                              │
├──────────────────────────────────────────────────────────────────────────────┤
│          PostgreSQL              │           ChromaDB                         │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 2.3 三层职责边界

| 层 | 目录 | 职责 | 不做什么 |
|----|------|------|---------|
| **接入层** | `core/channel/` | 接收外部消息（企业微信/飞书/CLI），统一格式化，路由到 Brain | 不含业务逻辑 |
| **Modules 层（业务技能包）** | `modules/<domain>/<capability>/` | 业务调度逻辑（何时扫描、匹配策略、对话管理） | 不直接操作平台 IO，通过领域内 `_connectors/` 接口调用 |
| **Capability 层** | `core/` | 共享基础设施（LLM、存储、调度、通知、安全、Agent/Task/Memory Runtime） | 不含业务逻辑，不知道 Module 的存在 |

> 平台 IO 驱动 (`_connectors/`) **不是独立的架构层**，它是某个领域 (`job` / `email` / ...) 内部的实现细节，与该领域的子能力共同构成一个"技能包"。

**Modules 层的内部组织（领域 → 子能力 → 平台驱动）**：

```
modules/
├── job/                         求职域（技能包）
│   ├── skill.py                 领域级 SKILL_SCHEMA（供 Brain 两级路由）
│   ├── shared/                  领域内共享 schema / 模型
│   ├── _connectors/             领域内平台驱动
│   │   ├── base.py              JobPlatformConnector 接口
│   │   └── boss/                Boss 直聘驱动；未来可增 liepin/ zhilian/
│   ├── greet/module.py          JobGreetModule   (name = job_greet)
│   └── chat/module.py           JobChatModule    (name = job_chat)
├── intel/                       情报域
│   ├── skill.py
│   ├── interview/ techradar/ query/
├── email/                       邮件域
│   ├── skill.py
│   └── tracker/module.py        EmailTrackerModule (name = email_tracker)
└── system/                      平台级系统域
    ├── skill.py
    ├── hello/module.py          HelloModule         (name = hello)
    └── feedback/module.py       FeedbackLoopModule  (name = feedback_loop)
```

**核心约束**

- Brain 先做领域级路由（读 `modules/<domain>/skill.py::SKILL_SCHEMA`），再做子能力路由。
- 子能力 module 只依赖同领域的 `_connectors/base.py` 契约，不 import 具体平台实现。
- 同一个子能力可以驱动多个平台（如 `job/greet` 未来可同时驱动 Boss + 猎聘），新增平台 = 在 `<domain>/_connectors/` 下新增一个实现，子能力代码不变。
- `ModuleRegistry.discover()` 会自动跳过以 `_` 开头的子包（`_connectors` 等）与名为 `shared` 的子包，保证 IO 驱动不会被误注册为业务模块。

### 2.4 Module 设计

**Module 声明项**：

| 声明项 | 含义 | 是否必须 |
|--------|------|---------|
| `name` | 唯一标识 | 必须 |
| `capability` | 给 LLM 看的能力描述 | 可选（有消息交互的必须提供） |
| `trigger_examples` | 触发示例 | 可选 |
| `router` | FastAPI 路由 | 可选 |
| `scheduled_tasks` | 定时任务列表 | 可选 |
| `message_handler` | 消息处理方法 | 可选 |
| `db_schemas` | 建表 SQL | 可选 |
| `on_startup` / `on_shutdown` | 生命周期回调 | 可选 |

**Pipeline 统一范式**：

```
触发（Scheduler 定时 / API 请求 / 飞书消息）
  → 采集（Patchright 爬取 / HTTP API / RSS）
  → 处理（LLM 分析 / 去重 / 过滤 / 聚合）
  → 执行（浏览器操作 / API 调用 / 写入存储）
  → 通知（飞书推送 / SSE 事件 / 日志）
```

**Module 间关系**：低耦合、数据约定。跨模块数据共享通过共享存储间接访问，以约定的表结构和向量 collection 名称为契约。

**Module 实现阶段**：

| 领域 / 子能力（Module） | 路径 | 阶段 |
|------------------------|------|------|
| `job` / greet（`job_greet`）   | `modules/job/greet/`          | Phase 2（从 V1 `boss_greet` 迁移）|
| `job` / chat（`job_chat`）     | `modules/job/chat/`           | Phase 2（从 V1 `boss_chat` 迁移）|
| `email` / tracker（`email_tracker`） | `modules/email/tracker/` | Phase 2（从 V1 迁移）|
| `intel` / interview（`intel_interview`） | `modules/intel/interview/` | Phase 2（新建）|
| `intel` / techradar（`intel_techradar`） | `modules/intel/techradar/` | Phase 2（新建）|
| `intel` / query（`intel_query`）         | `modules/intel/query/`     | Phase 2（新建）|
| `system` / hello（`hello`）              | `modules/system/hello/`    | Phase 1（健康探针）|
| `system` / feedback（`feedback_loop`）   | `modules/system/feedback/` | Phase 3（已落地基础反馈闭环）|

### 2.5 Capability Layer

| Capability | 职责 | 关键设计点 | 阶段 |
|-----------|------|-----------|------|
| **Browser Pool** | Patchright 浏览器实例管理 | 按 site_key 复用 persistent_context；Cookie 过期检测；健康检查与自动恢复 | Phase 2 必须 |
| **LLM Router** | 统一 LLM 调用 | 任务类型 → (主模型, 备模型)；Pydantic Structured Output；token 预算；重试降级 | Phase 2 必须 |
| **Storage Engine** | PostgreSQL + ChromaDB | 异步连接池；Module 声明表结构，启动时自动迁移 | Phase 2 必须 |
| **Notifier** | 主动推送通知 | send(level, content)；飞书卡片分级模板；与 Channel 的区别：Notifier 是单向推送 | Phase 2 必须 |
| **Scheduler** | 时段感知定时调度 | Module 自注册；interval/daily/manual；高峰/低峰自适应 | Phase 2 必须 |
| **Agent Runtime** | 通用 Agent 运行时（OS 内核） | 基于 Scheduler 的 HeartbeatLoop；模块通过 `register_patrol()` 自注册定时任务；熔断 + 错误恢复 + EventBus 可观测；详见 [Pulse-AgentRuntime设计.md](Pulse-AgentRuntime设计.md) | Phase 2 必须 |
| **Channel** | 多渠道消息收发 | BaseChannel 接口；飞书默认实现（3秒 ACK + 异步执行） | Phase 2 必须 |
| **Router** | 消息意图路由 | Module capability → LLM 分类 → 路由 | Phase 2 必须 |
| **Event Bus** | 进程内事件分发 + SSE | Pipeline 阶段事件 emit | Phase 2 必须 |
| **Config** | 统一配置管理 | Pydantic Settings | Phase 2 必须 |
| **Policy Engine** | 动作风险分级与审批 | safe/confirm/blocked 三级 + 审计日志 | Phase 2 简版 |
| **Observability** | 指标、追踪、审计 | Phase 2 仅结构化日志 + trace_id | Phase 3 预留 |
| **Template Registry** | 模板分发与复用 | — | Phase 3 预留 |

### 2.6 消息接入与意图路由

**消息路由流程**：

```
用户消息（飞书/CLI/API）
  ↓
Channel 层接收，统一格式化
  ↓
消息路由器（core/router.py）
  ├── 1. 收集所有 Module 的 capability + trigger_examples
  ├── 2. 构建 prompt：可用功能菜单 + 用户消息
  ├── 3. LLM 返回 module_name + 参数
  └── 4. 调用对应 Module 的 message_handler(params)
  ↓
Module 处理 → 通过原渠道回复用户
```

**路由降本策略**（三级）：
1. **精确匹配**（零成本）：卡片按钮 / API 指定 module_name
2. **前缀匹配**（零成本）：trigger_examples 编译为关键词 trie
3. **LLM 推理**（兜底）：前两级未命中时调用 LLM 分类

**路由失败处理**：返回友好提示列出可用 Module。Phase 3 升级为 Brain 后可通过 Tool Call 或 MCP 处理。

**Channel 抽象接口**：

| 接口方法 | 职责 |
|---------|------|
| `start()` | 启动渠道连接 |
| `on_message(callback)` | 注册消息回调 |
| `send_text(target, text)` | 发送文本 |
| `send_card(target, card)` | 发送卡片 |
| `stop()` | 关闭连接 |

### 2.7 编排策略

所有 Pipeline 用**纯 Python async 函数**。`async def run()` 作为入口，Scheduler 和 API 都调用同一个函数。每个 Stage 完成后 emit 事件。单条失败不阻断批量。

### 2.8 项目目录结构

```
pulse/
├── pyproject.toml
├── README.md
├── Dockerfile
├── docker-compose.yml
│
├── src/pulse/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   │
│   ├── core/
│   │   ├── module.py                # BaseModule + ModuleRegistry + 自动发现
│   │   ├── router.py                # 消息意图路由
│   │   ├── config.py                # Pydantic Settings
│   │   ├── server.py                # FastAPI 工厂 + Module 注册
│   │   ├── policy.py                # Policy Engine
│   │   ├── observability/           # Phase 2: 结构化日志; Phase 3: 全栈
│   │   ├── templates/               # Phase 3: Template Registry
│   │   ├── browser/                 # Patchright 浏览器池
│   │   ├── llm/                     # LLM 路由 + Structured Output
│   │   ├── storage/                 # PostgreSQL + ChromaDB
│   │   ├── notify/                  # 通知
│   │   ├── channel/                 # 多渠道消息接入
│   │   │   ├── base.py              # BaseChannel 抽象接口
│   │   │   ├── feishu.py            # 飞书
│   │   │   └── cli.py               # CLI 终端
│   │   ├── scheduler/               # 时段感知调度
│   │   └── events.py                # Event Bus
│   │
│   └── modules/                     # 业务技能包（领域分组 → 子能力 → 平台驱动）
│       ├── job/                     # 求职域
│       │   ├── skill.py             #   领域级 SKILL_SCHEMA
│       │   ├── shared/              #   领域内共享 schema
│       │   ├── _connectors/         #   领域内平台驱动
│       │   │   ├── base.py          #     JobPlatformConnector 接口
│       │   │   └── boss/            #     BOSS 直聘驱动
│       │   ├── greet/module.py      #   job_greet
│       │   └── chat/module.py       #   job_chat
│       ├── intel/                   # 情报域
│       │   ├── skill.py
│       │   ├── interview/module.py
│       │   ├── techradar/module.py
│       │   └── query/module.py
│       ├── email/                   # 邮件域
│       │   ├── skill.py
│       │   └── tracker/module.py
│       └── system/                  # 平台级系统域
│           ├── skill.py
│           ├── hello/module.py
│           └── feedback/module.py   # Phase 3 基础反馈闭环
│
├── tests/
└── docs/
    ├── architecture.md
    └── module-guide.md
```

### 2.9 需求覆盖校验

| 需求 | 覆盖状态 | 说明 |
|------|---------|------|
| F1 面经情报 | 部分完成 | `intel_interview` 已实现 web_search pipeline；站点级采集为目标态 |
| F2 技术雷达 | 部分完成 | `intel_techradar` 已实现 web_search pipeline；RSS / GitHub / 公众号为目标态 |
| F3 语义检索 | ✅ | `intel_query` 模块 |
| F4 内容发布 | ⚠️ Phase 3 预留 | — |
| F5 反馈闭环 | 基础完成 | `feedback_loop` 已接入 Evolution Engine；更深策略持续增强 |
| P0 安全门控 | ✅ | Policy Engine |

---

## 三、技术选型

| 选型点 | 选择 | 理由 |
|--------|------|------|
| **语言** | Python 3.11+ | Patchright 唯一运行时 |
| **API 框架** | FastAPI | async 原生、Pydantic 集成 |
| **浏览器自动化** | Patchright | binary-level CDP 反检测 |
| **LLM 调用** | 自建 LLM Router + Pydantic Structured Output | 多 Provider 降级，类型安全 |
| **关系数据库** | PostgreSQL | JSONB + 并发写入 |
| **向量数据库** | ChromaDB / 可选 pgvector | 零额外服务 / 复用 PG |
| **调度** | 自建 Scheduler | 时段感知 + Module 自注册 |
| **消息接入** | 飞书 Python SDK（lark_oapi WebSocket） | 同进程、无需公网 IP |
| **工作流编排** | 纯 Python async 函数 | 当前需求无需框架 |
| **包管理** | pyproject.toml + Hatchling | 2026 Python 标准 |
| **代码质量** | Ruff + mypy(strict) + pytest | 开源标配 |
| **部署** | pip install + pulse start / Docker Compose | 全平台原生 + 容器 |

---

## 四、代码迁移映射

| 当前文件 | 行数 | V2 去向 |
|---------|------|--------|
| `boss_scan.py` | 3172 | → `core/browser/` + `modules/job/greet/` + `modules/job/chat/` + `modules/job/_connectors/boss/` |
| `boss_workflow.py` | ~200 | → `modules/job/greet/` |
| `boss_chat_workflow.py` + `boss_chat_service.py` | ~700 | → `modules/job/chat/` |
| `workflow.py` | ~500 | → `core/llm/` |
| `storage.py` | ~800 | → `core/storage/` + 各 Module models |
| `production_guard.py` | ~300 | → `core/scheduler/` + `core/runtime.py`（AgentRuntime） |
| `agent_events.py` | ~100 | → `core/events.py` |
| `email_*.py` (4 个文件) | ~400 | → `modules/email/tracker/` |
| `main.py` | ~500 | → `core/server.py` |
| `schemas.py` | ~200 | → 公共部分 `core/`，领域部分各 Module |
| `vector_store.py` | ~200 | → `core/storage/vector.py` |
| **砍掉** | ~1500 | `material_*.py`, `interview_prep_service.py`, `company_intel_service.py`, `form_*.py` |

**重构清理备忘**：
- 删除 `backend/exports/` 目录（V1 运行时垃圾数据，已从 git 移除）
- 清除所有文件中的**截屏逻辑**（`_screenshot_dir()`、`page.screenshot()`、`screenshot_path` 字段）：涉及 `boss_scan.py`、`form_autofill.py`、`browser_use_server.py`、`schemas.py`、`storage.py` 等。V1 为前端展示浏览器状态而往磁盘疯狂存 PNG，无清理机制。重构后若需浏览器可视化应改用流式方案或 CDP 直连

---

## 五、Phase 3-4 架构：Brain + 三环能力模型

### 5.1 架构总览

```
┌────────────────────────────────────────────────────────────────────────────┐
│                      Pulse (单 Python 进程)                               │
│                                                                            │
│  ┌──────────────────────── 接入层 ────────────────────────────┐            │
│  │  ┌──────────┐  ┌────────────┐  ┌─────┐  ┌──────────────┐  │            │
│  │  │ FastAPI   │  │ 飞书 SDK   │  │ CLI │  │ MCP Server   │  │            │
│  │  │ (HTTP)    │  │ (WebSocket)│  │     │  │ (对外暴露)   │  │            │
│  │  └─────┬────┘  └─────┬──────┘  └──┬──┘  └──────┬───────┘  │            │
│  │        └──────────────┴────────────┴────────────┘          │            │
│  │                        ↓                                    │            │
│  │       Channel 统一格式化 → Brain 推理引擎                   │            │
│  └────────────────────────────────────────────────────────────┘            │
│                                                                            │
│  ┌──────────────── Brain (ReAct 推理循环) ────────────────────┐            │
│  │                                                            │            │
│  │  用户消息 + Memory Context                                 │            │
│  │       ↓                                                    │            │
│  │  ┌──────────────────────────────────┐                      │            │
│  │  │ while not final_text_response:   │                      │            │
│  │  │   ① See: all capabilities below  │                      │            │
│  │  │   ② Think: what to do next?      │                      │            │
│  │  │   ③ Act: call tool / module / mcp│                      │            │
│  │  │   ④ Observe: get result          │                      │            │
│  │  │   ⑤ Append to context            │                      │            │
│  │  └──────────────────────────────────┘                      │            │
│  │       ↓ final response                                     │            │
│  │  回复用户（原渠道）                                         │            │
│  └────────────────────────────────────────────────────────────┘            │
│                                                                            │
│  ┌─────────── 三环能力模型 (Brain 的 tool list) ─────────────┐            │
│  │                                                            │            │
│  │  ┌─── Ring 1: Tool (轻量级内置工具) ──────────────────┐   │            │
│  │  │ set_alarm │ get_weather │ search_web │ run_python │  │   │            │
│  │  │ read_url  │ create_note │ list_tasks │ set_timer  │  │   │            │
│  │  └────────────────────────────────────────────────────┘   │            │
│  │                                                            │            │
│  │  ┌─── Ring 2: Module (技能包, domain → capability) ─┐   │            │
│  │  │ job/greet │ job/chat │ intel/interview            │   │            │
│  │  │ intel/techradar │ intel/query │ email/tracker      │   │            │
│  │  │ system/hello │ system/feedback                     │   │            │
│  │  └────────────────────────────────────────────────────┘   │            │
│  │                                                            │            │
│  │  ┌─── Ring 3: MCP (外部工具生态) ─────────────────────┐   │            │
│  │  │ google.calendar │ spotify.play │ smarthome.toggle   │   │            │
│  │  │ github.search   │ notion.query │ (10,000+ servers)  │   │            │
│  │  └────────────────────────────────────────────────────┘   │            │
│  │                                                            │            │
│  │  ┌─── Meta: Skill Generator (Phase 4 自扩展) ─────────┐   │            │
│  │  │ 需求分析 → 代码生成 → AST安全 → 沙箱测试 → 热加载  │   │            │
│  │  └────────────────────────────────────────────────────┘   │            │
│  └────────────────────────────────────────────────────────────┘            │
│                                                                            │
│  ┌────────────── Capability Layer (共享能力，不变) ───────────┐            │
│  │ Browser Pool │ LLM Router │ Storage │ Notifier │ Scheduler │            │
│  │ Channel │ Policy Engine │ Event Bus │ Config │ Observability│           │
│  │ MCP Client (新增) │ Memory (新增) │ Cost Controller (新增) │            │
│  └────────────────────────────────────────────────────────────┘            │
│                                                                            │
├────────────────────────────────────────────────────────────────────────────┤
│       PostgreSQL          │        ChromaDB / pgvector                      │
└────────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Brain 伪代码

```python
async def brain_loop(message: str, context: BrainContext) -> str:
    # === 记忆加载阶段 ===
    core = core_memory.load_all()                     # SOUL + USER + PREFS + CONTEXT
    recent = recall_memory.recent(n=10)               # 最近 10 轮对话摘要
    relevant = recall_memory.search(message, top_k=5) # 语义检索相关历史
    
    system_prompt = build_system_prompt(
        soul=core["SOUL"], user=core["USER"],
        prefs=core["PREFS"], task_context=core["CONTEXT"],
        recall=merge_deduplicate(recent, relevant),
    )
    
    tools = (
        tool_registry.list_tools()       # Ring 1: Built-in Tools
        + module_registry.as_tools()     # Ring 2: Modules
        + mcp_client.list_tools()        # Ring 3: MCP
        + memory_tools()                 # 记忆工具
    )
    
    context.set_system(system_prompt)
    context.append({"role": "user", "content": message})
    
    # === ReAct 推理循环 ===
    for step in range(MAX_TOOL_CALLS):
        response = await llm_router.call(messages=context.messages, tools=tools)
        
        if response.is_text():
            break
        
        for tool_call in response.tool_calls:
            result = await execute_tool(tool_call, context)
            context.append(tool_call_result(tool_call, result))
    
    # === 记忆写入阶段 ===
    recall_memory.save_turn(message, response.text, context.tool_calls)
    correction_detector.check(message, context)
    
    return response.text
```

### 5.3 三环能力模型

**Ring 1: Tool（轻量级内置工具）**

| 特性 | 说明 |
|------|------|
| 定义 | 单个 Python async 函数 + type hints + docstring |
| 格式 | MCP 标准，`@mcp.tool` 装饰器自动生成 JSON Schema |
| 执行 | 直接函数调用，毫秒级 |
| 双重身份 | 既是 Brain 的 Tool，又是对外 MCP Server Tool |

**Ring 2: Module（重型 Pipeline，不变）**

Module 通过 `module_registry.as_tools()` 暴露为"高级工具"，Brain 统一调度。

**Ring 3: MCP（外部工具生态）**

| 特性 | 说明 |
|------|------|
| 角色 | MCP Client 连接外部 MCP Server |
| 发现 | 启动时从 `mcp_servers.yaml` 读取配置并获取工具列表 |
| 安全 | Policy Engine 门控：外部工具首次调用需用户确认 |

**Meta: Skill Generator（Phase 4）**

```
用户："我需要一个每天监控 BTC 价格的功能"
  → Brain 调用 Skill Generator
  → ① 分析需求 → ② LLM 生成代码
  → ③ AST 安全扫描 → ④ subprocess 沙箱测试
  → ⑤ Policy Engine 确认 → ⑥ 热加载
  → 立即可用
```

**ToolUseContract（工具使用合约）**

三环工具经三条正交契约接入 Brain, 详见 [ADR-001-ToolUseContract](./adr/ADR-001-ToolUseContract.md) 与 `Pulse-内核架构总览.md` §7。

| 契约 | 落点 | 实装 |
|---|---|---|
| A. Description | `ToolSpec.when_to_use / when_not_to_use` + PromptContract 三段式渲染 | ✅ |
| B. Call | `LLMRouter.invoke_chat(tool_choice=...)` + Brain 结构化 escalation | ⏳ |
| C. Execution Verifier | 终回复前 LLM 自评 commitment vs used_tools | ⏳ |

不变式: 语义判断归 LLM, 结构判断归 Python; host 侧不得用关键词 / 正则匹配用户意图强制调工具。`when_*` 字段只陈述**代码事实** (schema 约束 / 副作用 / 与邻居工具职责划分), 不列用户口语示例。

### 5.4 Phase 2 → Phase 3 增量升级路径

| 组件 | Phase 2 | Phase 3 | 变化 |
|------|---------|---------|------|
| **接入层** | Channel + FastAPI + CLI | + MCP Server 端点 | 新增 |
| **推理引擎** | Router（一次 LLM 分类） | Brain（ReAct 循环） | 升级 |
| **Module** | BaseModule + Pipeline | 不变 | 不变 |
| **Tool** | 不存在 | Tool Registry + `tools/` | 新增 |
| **MCP** | 不存在 | MCP Client | 新增 |
| **Capability** | 11 个 | + MCP Client + Memory Engine + Cost Controller | 新增 3 个 |

### 5.5 Phase 3 新增目录

```
pulse/
├── src/pulse/
│   ├── core/
│   │   ├── brain.py               # ReAct 推理循环
│   │   ├── router.py              # 保留，Brain 内部降级路径
│   │   ├── tool.py                # Tool 注册器 + @tool 装饰器
│   │   ├── mcp_client.py          # MCP Client
│   │   ├── mcp_server.py          # 将内置 Tool 暴露为 MCP Server
│   │   ├── memory/                # 分层记忆引擎
│   │   │   ├── core_memory.py     #   Core Memory Block 管理
│   │   │   ├── recall_memory.py   #   对话历史 + 语义检索
│   │   │   └── memory_tools.py    #   Brain 可用记忆工具
│   │   ├── cost.py                # LLM 成本控制
│   │   ├── sandbox.py             # Phase 4: 代码沙箱
│   │   └── skill_generator.py     # Phase 4: Skill Generator
│   │
│   ├── tools/                     # 轻量级工具
│   │   ├── alarm.py
│   │   ├── weather.py
│   │   ├── web.py
│   │   ├── notes.py
│   │   ├── tasks.py
│   │   └── code.py
│   │
│   └── generated/                 # Phase 4: 自动生成代码
│       ├── tools/
│       └── modules/
│
├── mcp_servers.yaml
└── ...
```

### 5.6 安全架构

| 安全层 | Phase 2 | Phase 3 | Phase 4 |
|--------|---------|---------|---------|
| **动作门控** | safe/confirm/blocked | 扩展到 MCP 外部工具 | 扩展到生成代码 |
| **成本控制** | — | 每日 LLM 硬消费上限 + 自动降级 | 同上 |
| **工具审计** | Module 审计日志 | + Tool/MCP 调用审计 | + 生成代码审计 |
| **代码安全** | — | — | AST + 白名单 + 模式检测 + 沙箱 |
| **用户确认** | 高风险二次确认 | MCP 首次确认 | 生成代码激活确认 |

---

## 六、记忆与进化架构

### 6.1 四层记忆 + 双轨进化

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Pulse Memory & Evolution                       │
│                                                                     │
│  ┌────────── Layer 1: Core Memory (每轮加载) ──────────────┐       │
│  │  ┌──────────────┐  ┌─────────────┐  ┌───────────────┐  │       │
│  │  │  SOUL Block  │  │  USER Block │  │ PREFS Block   │  │       │
│  │  │  (Agent 人格)│  │  (用户画像) │  │ (偏好规则)    │  │       │
│  │  └──────────────┘  └─────────────┘  └───────────────┘  │       │
│  │  Brain 每轮推理前加载 → 注入 system prompt              │       │
│  │  总容量限制: < 50K 字符                                  │       │
│  └──────────────────────────────────────────────────────────┘       │
│                                                                     │
│  ┌────────── Layer 2: Recall Memory (按需检索) ────────────┐       │
│  │  对话历史（全量，语义+时间双索引）                        │       │
│  │  工具执行记录（Tool/Module/MCP 输入输出）                 │       │
│  └──────────────────────────────────────────────────────────┘       │
│                                                                     │
│  ┌────────── Layer 3: Archival Memory (长期存储) ───────────┐       │
│  │  结构化知识（PostgreSQL）                                │       │
│  │  向量知识（ChromaDB / pgvector）                         │       │
│  │  时序事实图（PostgreSQL JSONB，轻量版时序图谱）          │       │
│  └──────────────────────────────────────────────────────────┘       │
│                                                                     │
│  ┌────────── Layer 4: Meta Memory (进化层) ─────────────────┐       │
│  │  Track A: 偏好学习（无需 GPU）                            │       │
│  │    纠正检测 → 规则提取 → PREFS/USER/SOUL Block 更新      │       │
│  │  Track B: DPO 权重微调（Phase 5 进阶可选，需 GPU）       │       │
│  │    训练对收集 → 自动微调 → A/B 评估                      │       │
│  └──────────────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────────┘
```

### 6.2 Core Memory

| Block | 内容 | 更新方式 |
|-------|------|---------|
| **SOUL** | Agent 人格（语气、风格、价值观） | 初始化 + 反思 Pipeline（Phase 5） |
| **USER** | 用户画像（姓名、技术栈、目标） | Brain 通过 `memory_update` 工具 |
| **PREFS** | 偏好规则（排除条件、通知偏好） | 偏好学习 Pipeline |
| **CONTEXT** | 当前任务上下文 | Module 回调 + Brain 更新 |

**记忆工具**（与 set_alarm / get_weather 同级）：

```python
@tool
async def memory_read(block: str) -> str: ...
@tool
async def memory_update(block: str, key: str, value: str) -> str: ...
@tool
async def memory_search(query: str, limit: int = 5) -> list[dict]: ...
```

### 6.3 Recall Memory

| 数据类型 | 存储 | 索引方式 |
|---------|------|---------|
| 对话消息 | PostgreSQL `conversations` 表 | 时间 + 向量 |
| 工具调用记录 | PostgreSQL `tool_calls` 表 | 时间 + 工具名 |
| Module 执行记录 | PostgreSQL `pipeline_runs` 表 | 时间 + Module 名 |
| 纠正记录 | PostgreSQL `corrections` 表 | 时间 |

**检索策略**：时间优先（最近 N 轮摘要）+ 语义检索（Top-5）→ 合并去重 → 注入上下文。

### 6.4 Archival Memory

复用 Phase 2 已有的 PostgreSQL + ChromaDB。新增**时序事实图**：

```sql
CREATE TABLE facts (
    id          SERIAL PRIMARY KEY,
    subject     TEXT NOT NULL,
    predicate   TEXT NOT NULL,
    object      TEXT NOT NULL,
    valid_from  TIMESTAMP NOT NULL,
    valid_to    TIMESTAMP,               -- NULL = 当前有效
    confidence  FLOAT DEFAULT 1.0,
    source      TEXT,
    superseded_by INTEGER REFERENCES facts(id),
    created_at  TIMESTAMP DEFAULT NOW()
);
```

### 6.5 人格机制：SOUL Block

```yaml
identity:
  name: "Pulse"
  role: "个人智能助手"

style:
  tone: "简洁直接，偏技术"
  language: "中文为主，代码/术语用英文"
  emoji: false

values:
  - "[CORE] 用户利益优先，不做损害用户的事"
  - "[CORE] 诚实，不确定的事明确说不确定"
  - "[CORE] 高风险操作必须用户确认"
  - "[MUTABLE] 优先推荐远程工作机会"
  - "[MUTABLE] 技术日报控制在5条以内"

boundaries:
  - "不代替用户做最终决定（如接受 offer）"
  - "不发送用户未审核的公开内容"
```

- `[CORE]`：不可修改的基础信念，反思 Pipeline 也不能改
- `[MUTABLE]`：可通过反思和反馈进化

**人格进化流程（Phase 5）**：

```
日常交互 → 经验分级（Routine / Notable / Pivotal）
  → Notable/Pivotal 触发反思 Pipeline
  → LLM 分析 → 建议更新 SOUL
  → 治理门控：
    MUTABLE + Supervised → 用户审核
    MUTABLE + Autonomous → 自动应用
    CORE → 永远不修改
  → 记录变更日志（可追溯、可回滚）
```

### 6.6 偏好学习

**Track A（无需 GPU）**：

| 学习信号 | 检测方式 | 转化为 |
|---------|---------|--------|
| 用户纠正 | "以后别推游戏公司" | PREFS 规则 |
| 用户反馈 | 日报标记"无用" | 降低来源权重 |
| 隐式行为 | 用户总是忽略 XX 类推送 | 推断偏好 → 确认 → PREFS |

**Track B（Phase 5 进阶可选，需 GPU）**：

收集 DPO 训练对 `{query, chosen, rejected, timestamp}`，积累足够数据后可选接入微调管线。

### 6.7 记忆阶段演进

| 阶段 | 记忆能力 | 新增组件 |
|------|---------|---------|
| **Phase 2** | 审计日志 + PG/ChromaDB | 无（数据格式为未来兼容） |
| **Phase 3** | + Core Memory + Recall Memory + 记忆工具 | `core/memory/` 目录 |
| **Phase 5** | + 人格进化 + 偏好学习 + 时序事实图 | `core/soul/` + `core/learning/` |
| **Phase 5 进阶** | + DPO 训练管线（可选） | `training/` 目录 |

### 6.8 Phase 5 新增目录

```
pulse/
├── src/pulse/
│   ├── core/
│   │   ├── memory/                      # Phase 3
│   │   │   ├── core_memory.py
│   │   │   ├── recall_memory.py
│   │   │   ├── archival_memory.py
│   │   │   ├── memory_tools.py
│   │   │   └── correction_detector.py
│   │   │
│   │   ├── soul/                        # Phase 5
│   │   │   ├── soul_config.py
│   │   │   ├── evolution.py
│   │   │   └── governance.py
│   │   │
│   │   └── learning/                    # Phase 5
│   │       ├── preference_extractor.py
│   │       ├── behavior_analyzer.py
│   │       └── dpo_collector.py
│   │
│   ├── config/
│   │   └── soul.yaml
│   └── ...
│
├── data/
│   └── training_pairs/                  # Phase 5 进阶
└── ...
```

### 6.9 与现有架构兼容性

| 现有组件 | 记忆架构中的角色 | 改动量 |
|---------|----------------|--------|
| **PostgreSQL** | Recall + Archival + Meta 数据表 | 增量新增表 |
| **ChromaDB / pgvector** | Archival 向量检索 + Recall 语义检索 | 增量新增 collection |
| **Brain 推理循环** | 前后增加记忆加载/写入阶段 | 增量 |
| **Policy Engine** | 人格进化和偏好修改的审批 | 扩展策略规则 |
| **Module Pipeline** | 执行完毕后更新 CONTEXT Block | 增加回调 |
| **Event Bus** | 记忆变更事件 | 增加事件类型 |

**结论：记忆架构是对现有架构的纯增量扩展，不需要修改任何已有组件的接口。**

---

## 七、方案核心优势总结

| 维度 | 说明 |
|------|------|
| **Pulse 远景** | Phase 2 → Phase 3 → Phase 4 → Phase 5，架构底座一次定义，增量升级 |
| **三环能力模型** | Ring 1 Tool + Ring 2 Module + Ring 3 MCP，Brain 统一调度 |
| **四层记忆引擎** | Core/Recall/Archival/Meta + 人格机制 + 偏好学习 |
| **MCP 生态接入** | 对外暴露 Tool + 对内接入 10,000+ 外部工具 |
| **完全独立** | 不依赖 OpenClaw/LangGraph/LangChain，纯 Python 自主可控 |
| **单进程单栈** | pip install 一键启动，全平台原生 + Docker |
| **自进化** | Skill Generator：自然语言 → 代码 → 安全检查 → 热加载 |
| **领域无关** | core/ 不含业务词汇，求职只是一个 Module |
| **新增功能零成本** | Pipeline → 新目录；Tool → 新函数；外部服务 → 配置 yaml |
| **编排务实** | Pipeline 纯 async；Brain < 200 行。零框架开销 |
