# OpenClaw 调研报告 × OfferPilot 立项方案

> 整合时间：2026-03-14 | 目标：为研二在读生（苏大 AI 硕士）立一个高吸引力的 Agentic 项目，最大化大模型应用实习机会

---

## 一、OpenClaw 是什么

### 1.1 本质定义与核心架构

OpenClaw（中文社区昵称"龙虾 🦞"）是一个 **MIT 协议开源的本地化 AI Agent Runtime**，由 PSPDFKit 创始人 Peter Steinberger 于 2025 年 11 月以 Clawdbot 名称创建，经历三次改名后于 2026 年 1 月以 OpenClaw 正式公开。

它不是聊天机器人，而是一个**可常驻、可执行、可自治的 Agent 操作层**——你给它目标，它自己拆解任务、调用工具、一步步干完。

**核心架构六件套：**

| 组件 | 职责 |
|------|------|
| **Gateway** | 常驻后台的 Node.js 长进程，负责全局调度和会话管理 |
| **Brain** | LLM 驱动的规划与决策层（支持多模型路由） |
| **Hands** | 执行层：Shell、文件系统、浏览器自动化、HTTP |
| **Memory** | 本地 Markdown 文件存储的长期记忆（`~/.openclaw/memory/`） |
| **Heartbeat** | 每 30 分钟自主触发的定时任务守护进程 |
| **Channels + Skills** | 消息渠道（微信/飞书/Telegram/Slack 等 20+ 平台）和可扩展技能体系 |

与传统聊天机器人的关键差异：

| 能力维度 | 传统 Chatbot | OpenClaw |
|----------|-------------|---------|
| 执行方式 | 被动回答 | 主动行动（Heartbeat 驱动） |
| 操作范围 | 文本进、文本出 | Shell、文件、浏览器、API 全接管 |
| 记忆 | 会话级 | 本地持久化 Markdown |
| 集成 | 无 | 50+ 平台 + 13,000+ ClawHub Skills |
| 隐私 | 数据上云 | 全部本地存储 |

### 1.2 发展历程（三次改名）

| 时间 | 事件 |
|------|------|
| 2025 年 11 月 | Peter Steinberger 以 **Clawdbot** 名称创建 |
| 2026 年 1 月 27 日 | Anthropic 发商标警告（"Clawd"太接近"Claude"），改名 **Moltbot** |
| 2026 年 1 月 30 日 | 再次改名为 **OpenClaw**，完成史上最快三次开源项目改名 |
| 2026 年 1 月底 | 两天内新增 10 万+ Star，成为 GitHub 历史上最快破 10 万 Star 的项目 |
| 2026 年 2 月 | 阿里云、腾讯等完成适配，韩国 Kakao/Naver/Karrot 内部禁用 |
| 2026 年 3 月 | BAT 全线推出配套"龙虾"产品矩阵 |

> Steinberger 自称"ship code he doesn't read"，2026 年 1 月一个人提交了 6,600 次 commit（使用 AI 辅助编程），这既是 OpenClaw 快速迭代的动力，也是安全问题的根源。

### 1.3 当前热度（2026 年 3 月）

- **GitHub Stars**：31 万+，历史增速最快的开源项目
- **最新版本**：v2026.3.7-beta.1（89 项提交，200+ Bug 修复）
- **社区规模**：376+ 贡献者，ClawHub Skills 市场收录 13,000+ 个技能包
- **模型支持**：Claude、GPT、Grok、DeepSeek、Qwen、Ollama 本地模型全覆盖
- **企业采用**：百度、阿里云、腾讯、京东均已推出基于 OpenClaw 的配套部署产品

### 1.4 近期重要更新

1. **ContextEngine 插件接口**：上下文管理可自由插拔（bootstrap → ingest → assemble → compact → afterTurn 完整生命周期），从工具升级为平台
2. **模型双引擎 + 自动路由**：支持模型降级/重试/自动切换，多模型协同更稳健
3. **多渠道深度整合**：飞书/Telegram/Slack 等持久化绑定 + 断线自动恢复
4. **安全加固**：沙盒逃逸防范、系统命令白名单鉴权、VirusTotal Skills 扫描合作

### 1.5 价值与边界——为什么不能直接拿来做项目底座

**OpenClaw 的产品价值：**
- 代表了 `Computer Use / Browser Use / Always-On Agent` 这一波执行型 Agent 浪潮的开源标杆
- 生态闭环已初步形成：Skills 市场 + 部署方案 + 企业级扩展
- BAT 纷纷跟进，说明它的产品范式是对的

**但它有严重的安全问题（官方自己承认）：**

| 安全事件 | 时间 | 严重程度 |
|---------|------|---------|
| CVE-2026-25253：一键 RCE 漏洞 | 2026.1.30 | Critical（8.8） |
| Moltbook 数据泄露：150 万 API Token、3.5 万邮箱 | 2026.1.31 | Critical |
| 93.4% 公网暴露实例存在认证绕过 | 2026.2.9 | Critical |
| ClawHub 发现 341 个恶意 Skills | 2026.2.4 | High |
| Snyk：36% Skills 含安全缺陷 | 2026.2.5 | High |

Gartner 将其定性为"一款非企业软件，不承诺质量保证、无供应商支持，默认无强制认证"。

**对你的策略判断：**
- 不要 fork OpenClaw 内核，它安全风险太高且更新节奏混乱
- 正确姿势：**借它的产品范式（Always-On + Browser Use + Heartbeat），在垂直场景独立实现**
- 最后加一个 OpenClaw Skill 适配层，作为"兼容生态"的彩蛋加分项

---

## 二、中国 Agent 市场资方风向（2026 年 Q1）

### 2.1 融资热点赛道

| 赛道 | 代表公司 | 融资情况 | 核心业务 |
|------|----------|----------|----------|
| **企业数据智能 Agent** | 中数睿智 | 2 亿元 A+ 轮（鼎晖VGC + 北京市AI基金） | 企业级AI Agent"智能军团"，服务能源/军工/央企 |
| **金融 Agent** | 讯兔科技 | 超 1 亿元 Pre-A 轮（高瓴 + 红杉） | 金融领域垂直 Agent |
| **办公 Agent** | AutoAgents | 数千万元天使轮（创新工场） | 办公自动化多 Agent 协同 |
| **Agent 开发平台** | Dify | 5000 万元 A 轮（源码资本） | 中小企业 Agent 快速部署 |
| **Browser Use 工具** | Browser Use | 1700 万美元种子轮（Felicis + YC） | 让 AI Agent 能"读懂"网页的基础工具 |
| **AI 编程 Agent** | Anysphere (Cursor) | 估值 293 亿美元 | AI 编程助手 |

### 2.2 大厂"龙虾"产品矩阵

| 公司 | 产品 | 定位 |
|------|------|------|
| **百度** | DuClaw（百度智能云）+ 红手指Operator（手机版） | 云端零部署 + 手机龙虾，支持 DeepSeek/Kimi/GLM/MiniMax |
| **阿里云** | JVS Claw | 手机端一键接入，iOS/Android/Web/Pad 多端，云端+本地双模式 |
| **腾讯** | WorkBuddy + QClaw + 腾讯云 Lighthouse + ADP | 全系产品矩阵，马化腾亲自朋友圈发文 |
| **京东** | 京东云 OpenClaw 部署 | 免费安装+百万 Tokens 赠送 |
| **美团** | Tabbit AI 浏览器 | AI原生浏览器 + Browser Use，已公测 |
| **字节跳动** | Coze / 飞书 Aily | 2C 智能体平台 + 企业协同 |

### 2.3 投资人最看重什么

根据近 6 个月公开融资案例和行业分析，资方偏好从高到低：

1. **垂直场景闭环 > 通用框架**：资方已从"概念炒作"转向"务实落地"，AutoAgents 合同审核 Agent 帮某电网客户效率提升 80% 就是标准叙事
2. **可量化的业务价值**：效率提升 X%、成本降低 X%，必须能算 ROI
3. **安全与合规治理能力**：Human-in-the-loop、审计日志、权限沙箱，尤其是企业场景
4. **工程化交付能力**：不是能跑 demo，而是能上线、能监控、能运维
5. **Browser Use / Computer Use 执行能力**：2026 年最热的 Agent 能力层
6. **MCP 协议兼容**：正在成为行业标准协议

### 2.4 资方最关注的三大 Agent 范式

```
[范式1] 垂直业务 Agent = LLM + 领域知识 + 工具链 + 安全治理
         → 金融、法律、医疗、HR、营销 · · ·

[范式2] Browser/Computer Use Agent = LLM + 视觉/DOM 感知 + 自动执行
         → AI 浏览器（Tabbit/Fellou）、RPA 升级、网页自动化

[范式3] Multi-Agent 协作平台 = 编排层 + 通信协议 + 状态管理
         → 企业级工作流（AutoAgents/中数睿智/Coze）
```

OfferPilot 同时命中范式 1 和范式 2，这是它作为个人项目叙事最强的地方。

---

## 三、招聘市场需求分析

### 3.1 谁在招 Agent 实习生

| 公司 | 岗位 | 薪资 | 核心要求 |
|------|------|------|----------|
| **淘天集团** | AI Agent 应用开发（暑期实习） | 300–600 元/天 | Agent 系统开发、LangChain/LlamaIndex/AutoGen、MCP、Prompt/RAG/Tool Calling |
| **淘天集团** | AI Agent 算法工程师（日常实习） | — | SFT/RL、多步推理、Agent 评测体系 |
| **小红书** | AI Agent 工程师（智能体 + 上下文工程） | 社招 | Intent 识别、任务分解、规划执行、工具调用、记忆与状态管理、Agent Runtime 设计 |
| **字节 / 飞书 Aily** | 大模型/Agent 评测工程师 | 28–45K×12 | Agent 评测体系设计、GAIA/AgentBench、分布式评测框架、稳定性/一致性/安全性指标 |
| **百度 / 蚂蚁** | 大模型应用工程师 | 300–500 元/天 | Agent 框架、MCP、全栈能力 |

### 3.2 JD 高频必备关键词

淘天 JD 原文要求（节选）：具备 Agent 系统开发经验，掌握 LangChain/LlamaIndex/AutoGen 框架及实现原理，能够设计高可用、高扩展性的大模型/Agent 工程架构；掌握 Prompt 工程、RAG、Tool Calling、Context Engineering；掌握 OpenAPI、RPC、MCP 等协议实现方案。

### 3.3 你的能力地图 × JD 对照

**你已覆盖的（已有项目证明）：**

| JD 要求 | 你的对应项目 |
|---------|------------|
| Intent 识别 + 任务分解 + 规划执行 | ScholarMind DeepResearch（Planner/Manager/Research/Reporter 六层架构） |
| RAG 系统 | ScholarMind 六层混合 RAG（BM25 + 向量 + RRF + MMR + Cross-Encoder） |
| 工具调用 / Function Call / ReAct | ScholarMind Doc Studio（16 类工具 + 预算管理 + 失败恢复） |
| 记忆管理（STM/LTM） | ScholarMind 多层上下文记忆（Session KB + STM + LTM + rolling summary） |
| SFT / DPO 对齐 | Resona（QLoRA + SFT + DPO，人格漂移抑制） |
| 数据工程 | Resona（真实语料采集 + 三层漏斗清洗） |

**你尚未覆盖的（新项目要补上的）：**

| JD 要求 | 当前状态 | OfferPilot 如何补 |
|---------|---------|-----------------|
| Browser Use / Computer Use | ❌ 无 | Playwright 驱动的网申表单自动执行 |
| MCP 协议集成 | ❌ 无 | 工具层全部走 MCP 标准接口 |
| 长期自治任务执行（Always-On） | ❌ 无 | Heartbeat 定时巡检岗位/邮件/状态 |
| 安全审计 + 行为回放 | ❌ 无 | Action Timeline + 截图回放 + 审批令牌 |
| Agent 评测与稳定性指标 | ❌ 无 | 填表成功率、匹配评分漂移、失败恢复率 |
| 多渠道接入（邮件） | ❌ 无 | 邮件 MCP Server + 飞书/微信推送 |

---

## 3.5 小厂/初创公司 OpenClaw 岗位实况（对症下药）

大厂岗位竞争极其激烈，学历筛选严格。更务实的策略是：**先拿高质量初创/小厂的垂直实习做跳板，再向大厂渗透。** 而这些小厂正在直接把"OpenClaw"写进 JD，你的项目必须能直接命中他们的关键词。

以下是 2026 年 3 月公开可查的三个代表性真实 JD：

#### JD-1：V2EX 远程 AI 应用工程师（初创公司，底薪 20–40K + 年终奖）

> **任职要求原文（节选）：**
> - 熟悉 **OpenClaw 框架的使用与底层逻辑**
> - 有基于 **OpenClaw 构建 Agent 或工作流**的实际项目经验
> - 理解 **Prompt 结构设计、任务拆解与输出控制**
> - 熟悉大模型 API 调用与参数调优
> - Python 或 Node.js
> - 能独立完成从需求拆解到系统落地的全过程
> - 加分项：RAG 体系建设、内部效率工具或自动化系统建设经验
>
> **岗位职责原文（节选）：**
> - 基于 OpenClaw 框架设计并落地公司内部 AI 工具
> - 使用 OpenClaw 构建 **Agent 工作流与自动化执行链路**
> - 设计结构化 Prompt 与多轮任务执行逻辑
> - 对接内部系统，实现业务流程自动化
> - 优化模型稳定性、执行效率与成本控制
>
> 来源：[V2EX](https://v2ex.com/t/1195183)，2026-03-02 发布

#### JD-2：Varsity Holdings（新加坡 AI Fintech 初创）OpenClaw Multi-Agent 实习

> **核心要求原文（节选）：**
> - Python or JavaScript
> - LLM APIs (OpenAI, Claude, etc.)
> - Building AI tools, bots, or **automation workflows**
> - **"We care more about what you've built than academic credentials"**
> - 加分项：LangChain 框架、RAG pipelines、向量数据库、API 集成
>
> **工作内容原文（节选）：**
> - Build and improve our **OpenClaw multi-agent architecture**
> - Design agents that can **coordinate, delegate tasks, and collaborate**
> - Implement **agent communication and workflow orchestration**
> - Develop tools: APIs, databases, web search, financial data feeds
> - Help develop **agent memory & knowledge systems**
>
> 月薪 $800–2,100 SGD，3–6 个月，可能转正
> 来源：[InternSG](https://www.internsg.com/job/varsity-holdings-openclaw-multi-agent-systems-engineer-intern-ai-agents-openclaw/)，2026-03-13 发布

#### JD-3：国内多行业 OpenClaw 岗位汇总（红星资本局报道）

> 北京、上海、深圳多企业放出"**OpenClaw 开发工程师**""**AI 产品经理**""**OpenClaw 研究实习生**"等岗位。要求："**熟悉 OpenClaw 工具，有实际部署经验**""**有 OpenClaw 搭建智能体与自动化工作流的能力**"。覆盖 AI、计算机硬件、传媒影视、**医疗健康、私募基金**等行业。月薪 1–3 万元。
>
> 深圳某私募基金公司 IT 实习生 JD："深入研究并运用 AI 工具，如 **OpenClaw 小龙虾**、deepseek、豆包等，构建适用于私募基金业务的工作模板"。
>
> 来源：[川观新闻/红星资本局](https://cbgc.scol.com.cn/news/7359403)，2026-03-08 报道

#### 小厂 JD 的共性特征提炼

| JD 高频关键词 | 出现频率 | OfferPilot 是否覆盖 |
|--------------|---------|-------------------|
| **OpenClaw 框架使用与部署经验** | 几乎所有 JD | ✅ 项目直接跑在 OpenClaw 上 |
| **构建 Agent 工作流 / 自动化执行链路** | 几乎所有 JD | ✅ 求职全流程 Agent Loop |
| **结构化 Prompt 设计 + 任务拆解** | 大部分 JD | ✅ Planner Agent 的核心能力 |
| **大模型 API 调用与多模型切换** | 大部分 JD | ✅ DeepSeek/Qwen/Claude 混合用 |
| **Python 或 Node.js** | 所有 JD | ✅ Python (FastAPI) |
| **RAG / 向量数据库** | 加分项 | ✅ ChromaDB + 简历/JD 历史窄域检索 |
| **完整项目落地（非 Demo）** | 强调 | ✅ 自己真实求职在用 |
| **Skill 开发能力** | 部分 JD | ✅ 开发并发布 OpenClaw Skill |
| **多 Agent 协作架构** | 高级 JD | ✅ Planner-Executor-Evaluator |
| **内部工具 / 效率工具建设** | 加分项 | ✅ 求职效率工具 |

**关键洞察：小厂不像大厂那样看学历，他们更看重"你用 OpenClaw 做了什么"。** "We care more about what you've built than academic credentials" 这句话不只是 Varsity 一家的态度，而是初创公司招聘 OpenClaw 人才的普遍心态。你的学历劣势在这些公司面前可以被项目实力对冲。

---

## 四、立项方案：OfferPilot — 基于 OpenClaw 的求职运营 Agent

### 4.1 为什么选这个方向

四个维度全部对齐：

| 维度 | 分析 |
|------|------|
| **OpenClaw 热度红利** | 小厂 JD 直接写"OpenClaw 部署经验""OpenClaw 工作流能力"，你的项目名和技术栈必须能被搜到 |
| **技术覆盖** | 一个项目同时覆盖：OpenClaw Skill 开发 + Browser Use + 多 Agent 协作 + Heartbeat + RAG，精准命中小厂 JD 所有关键词 |
| **Dogfooding** | 你就是第一个用户，需求 100% 真实，小厂面试官最看重"你真的在用" |
| **学历对冲** | 初创看项目不看学历，OfferPilot 的实际落地深度 > 985 同学的课程 demo |
| **差异化** | 与 ScholarMind（研究型 Agent）互补，证明你不是只会做一种 Agent |
| **可扩展性** | MVP 做候选人侧，后期可扩成 Recruiting Agent（招聘团队侧），商业叙事完整 |

### 4.2 产品定义

**OfferPilot = 一个安全受控的求职运营 Agent 系统**

> 定位不是"简历润色工具"，而是"能替你执行求职全流程、但关键节点必须你审批"的垂直执行型 Agent。

**核心业务闭环：**

```
┌─────────────────────────────────────────────────────────────────┐
│                     OfferPilot Agent Loop                       │
│                                                                 │
│  [1] 岗位监控    自动爬取/监控招聘平台、目标公司官网              │
│       ↓                                                         │
│  [2] JD 理解     结构化抽取技能要求、业务方向、加分项             │
│       ↓                                                         │
│  [3] 匹配评分    简历 × JD 语义匹配 + Gap 分析报告               │
│       ↓                                                         │
│  [4] 材料定制    针对 JD 改写简历 bullet、生成求职信              │
│       ↓                                                         │
│  [5] Browser Use 自动打开网申页、填表、上传材料                  │
│       ↓           ⚠️ 提交前强制 Human-in-the-Loop 审批           │
│  [6] 长期跟踪    邮件解析、投递状态追踪、面试日历提醒             │
│       ↓                                                         │
│  [7] 情报生成    公司/团队/技术栈/潜在面试题自动汇总              │
│       ↓                                                         │
│  [8] 审计回放    完整 Action Timeline + 截图逐步回放              │
└─────────────────────────────────────────────────────────────────┘
```

### 4.3 系统架构

```
┌────────────────────────────────────────────────────┐
│                 Frontend (Next.js + Tailwind)        │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────┐  │
│  │ 投递看板  │  │ 审批面板 │  │ Action Timeline   │  │
│  └──────────┘  └──────────┘  └───────────────────┘  │
└──────────────────────┬─────────────────────────────┘
                       │ WebSocket / SSE
┌──────────────────────┴─────────────────────────────┐
│              Backend (FastAPI + Python)              │
│                                                      │
│  ┌──────────────────────────────────────────────┐   │
│  │         Agent Orchestration Layer (LangGraph) │   │
│  │  ┌──────────┐  ┌──────────┐  ┌────────────┐  │   │
│  │  │ Planner  │  │ Executor │  │ Evaluator  │  │   │
│  │  │  Agent   │  │  Agent   │  │  Agent     │  │   │
│  │  └──────────┘  └──────────┘  └────────────┘  │   │
│  └──────────────────────────────────────────────┘   │
│                                                      │
│  ┌──────────────────────────────────────────────┐   │
│  │          Tool Layer（MCP Protocol）           │   │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────────┐  │   │
│  │  │ Browser  │ │  Email   │ │  Web Search  │  │   │
│  │  │   Use    │ │  Reader  │ │  (情报采集)   │  │   │
│  │  │(Playwright)└──────────┘ └──────────────┘  │   │
│  │  └──────────┘   ··· 共 5 类 MCP Server ···   │   │
│  └──────────────────────────────────────────────┘   │
│                                                      │
│  ┌──────────────────────────────────────────────┐   │
│  │       Safety & Governance Layer               │   │
│  │  Human-in-the-Loop │ Action Budget │ Audit Log │  │
│  │  审批令牌 + 防重放   │ 工具调用预算  │ 行为审计  │  │
│  └──────────────────────────────────────────────┘   │
│                                                      │
│  ┌──────────────────────────────────────────────┐   │
│  │       Memory & Storage Layer                  │   │
│  │  PostgreSQL（关系数据）│ ChromaDB（向量数据）│ LTM    │   │
│  │  Heartbeat 定时守护进程（岗位/邮件/状态巡检）  │   │
│  └──────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────┘
```

### 4.4 实现策略：双轨架构（OpenClaw 原生 + 独立深度模块）

之前的方案把 OpenClaw 放在最后当"彩蛋"，这对大厂面试是对的，但**对小厂面试是错的**。小厂 JD 直接要求"OpenClaw 部署经验"和"Skill 开发能力"，所以 OpenClaw 必须是项目的**显性标签**，而不是隐性借鉴。

**调整后的双轨策略：**

**轨道一：OpenClaw 原生层（对小厂展示）**
1. 项目**直接运行在 OpenClaw 上**：安装 OpenClaw，配置 Gateway + Heartbeat + 飞书/微信 Channel
2. **开发 3–5 个 OpenClaw Skills** 并发布到 ClawHub：
   - `job-monitor`：招聘平台岗位监控 + JD 结构化解析
   - `resume-tailor`：针对 JD 自动改写简历 bullet
   - `company-intel`：公司/团队/技术栈情报自动汇总
   - `application-tracker`：投递状态跟踪 + 邮件解析
   - `interview-prep`：面试题库生成 + 模拟面试
3. **利用 Heartbeat** 实现每日自动岗位巡检 + 邮件状态更新
4. **接入飞书/微信 Channel**，让 Agent 主动推送消息给你

这条轨道让你面试时能说："我开发了 5 个 OpenClaw Skills，已经发布到 ClawHub，我自己每天在用 OpenClaw 跑求职流程。"——这比任何学历都能打动小厂。

**轨道二：独立深度模块（对大厂展示技术深度）**
1. **Browser Use 执行引擎**用 Playwright 独立实现（OpenClaw 的浏览器自动化不够稳定）
2. **多 Agent 编排**用 LangGraph 实现 Planner-Executor-Evaluator 状态机
3. **安全治理层**（审批令牌、Action Budget、审计回放）独立实现
4. **MCP 工具层**标准化接口
5. **RAG** 只围绕简历、项目文档、目标公司资料做窄域检索
6. 通过 **OpenClaw Skill 接口**把这些深度模块暴露给 OpenClaw 调用

两条轨道的交汇点：**OpenClaw 是入口和调度层，深度模块是执行和治理层**。面对不同公司，切换讲述重点：
- 面试小厂 → 重点讲轨道一（OpenClaw 使用/部署/Skill 开发经验）
- 面试大厂 → 重点讲轨道二（Agent 架构/安全治理/Browser Use/MCP 工程深度）

### 4.5 技术栈

| 层级 | 选型 | 理由 |
|------|------|------|
| **Agent Runtime** | **OpenClaw** | 直接命中小厂 JD 的"OpenClaw 部署经验"要求 |
| 前端 | **Next.js 14 + Tailwind CSS** | SSR + 现代 UI，vibe coding 友好 |
| 后端 | **FastAPI (Python)** | 与你技术栈一致，Agent 生态最完善 |
| Agent 编排 | **LangGraph** | 状态机驱动，比 AgentExecutor 更可控可审计 |
| Browser Use | **Playwright** | 支持 DOM 操作 + 截图 + Stealth 模式 |
| 工具协议 | **MCP (Model Context Protocol)** | 2026 年行业标准，JD 加分项 |
| OpenClaw Skills | **SKILL.md + Node.js 模块** | 发布到 ClawHub，展示 Skill 开发能力 |
| 关系存储 | **PostgreSQL** | 业务数据（jobs/applications/actions） |
| 向量存储 | **ChromaDB（嵌入模式）** | 简历段落 + JD 历史向量检索 |
| 模型 | **DeepSeek（规划层）+ Qwen（执行层）** | 混合使用降低成本 |
| 消息渠道 | **飞书 / 微信 Channel** | 通过 OpenClaw Channel 接入 IM 推送 |
| 部署 | **Docker Compose** | 一键部署，demo 方便 |

### 4.6 十天 MVP 开发计划（增加 OpenClaw 原生轨道）

```
Day 1: 环境搭建 + OpenClaw 部署
  - WSL2 + Node 22 安装 OpenClaw，配通 Gateway + Heartbeat
  - 接入飞书/微信 Channel，验证消息收发
  - 配置模型路由（DeepSeek 为主 + Qwen 备用）
  - Next.js + FastAPI 脚手架搭建
  - 数据库 schema 设计（Job / Application / Action 三张核心表）

Day 2: OpenClaw Skill 开发 —— 岗位监控
  - 开发 job-monitor Skill（SKILL.md + 爬虫逻辑）
  - 招聘平台监控 + JD 结构化解析（LLM 抽取技能、方向、加分项）
  - 配置 Heartbeat 每日自动触发岗位扫描
  - 前端看板骨架（岗位列表展示）

Day 3: 简历匹配 + Gap 分析
  - 简历 × JD 语义匹配评分
  - Gap 分析报告生成
  - 前端看板（匹配度排序 + Gap 可视化）

Day 4: OpenClaw Skill 开发 —— 材料定制
  - 开发 resume-tailor Skill
  - 针对 JD 自动改写简历 bullet
  - 求职信 / 邮件模板生成
  - Diff 对比展示（类似 ScholarMind Doc Studio 的审批逻辑）

Day 5: Browser Use 引擎（独立深度模块）
  - Playwright 浏览器代理层
  - 网申表单自动识别与填充
  - DOM 快照 + 截图 + 强制 Human-in-the-Loop 审批流程
  - 通过 OpenClaw Skill 接口暴露给 OpenClaw 调用

Day 6: 长期跟踪 + 邮件解析
  - 开发 application-tracker Skill
  - 邮件 MCP Server（解析面试邀请、拒信、进度更新）
  - 投递状态自动更新 + 飞书/微信推送提醒
  - Heartbeat 定时巡检（岗位更新 / 邮件变化 / 状态提醒）

Day 7: 面试情报 + OpenClaw Skill 发布
  - 开发 company-intel Skill + interview-prep Skill
  - 公司 / 团队 / 技术栈 / 潜在面试题自动调研报告
  - 将所有 Skills 整理、测试、发布到 ClawHub

Day 8: 安全治理 + 审计
  - Action Budget（工具调用预算上限）+ 审批令牌 + 审计日志
  - Action Timeline 可视化（时间轴 + 截图回放）
  - 安全加固：敏感操作白名单、凭证环境变量管理

Day 9: 多 Agent 编排 + 联调
  - LangGraph 状态机：Planner-Executor-Evaluator 三角色完整联调
  - OpenClaw Gateway 入口 ↔ LangGraph 深度模块 ↔ 前端看板全链路打通
  - 用真实求职数据跑一遍完整流程，修 bug

Day 10: 打磨 + Demo
  - UI 响应式适配 + 细节打磨
  - Docker Compose 一键部署配置（含 OpenClaw + FastAPI + PostgreSQL）
  - 录制 Demo 视频（完整真实求职流程 + OpenClaw 飞书推送演示）
  - 撰写 README（突出 OpenClaw Skills + 独立深度模块双轨架构）
  - 整理 ClawHub Skills 发布截图作为项目成果
```

---

## 五、竞品对比与差异化

| 竞品 | 定位 | OfferPilot 的差异化 |
|------|------|-------------------|
| **OpenClaw 通用使用** | 装完跑个 demo | 你不只是"用"，你**开发了 Skills 并发布到 ClawHub**，还在它之上做了独立的安全治理和 Agent 编排 |
| **JobClaw**（GitHub 开源） | 简单的投递自动化脚本 | 你有完整的 Planner-Executor-Evaluator 架构、审批机制、审计回放，工程深度不在一个量级 |
| **Coze / Dify** | Agent 开发平台 | 你不是做平台，而是做交付了业务价值的端到端产品 |
| **Tabbit / Fellou** | 通用 AI 浏览器 | 你聚焦求职场景，闭环更完整，可量化效果 |
| **Boss直聘 / 猎聘 AI** | 平台侧内置推荐 | 你是用户侧、跨平台、可审计、可扩展的 Agent |

**面试时可能被问到的问题 + 推荐答法（分小厂/大厂两套）：**

| 问题 | 面试小厂时的答法 | 面试大厂时的答法 |
|------|----------------|----------------|
| "你对 OpenClaw 有多熟？" | "我不只是部署过，我开发了 5 个 Skills 发布到 ClawHub，每天真实用 OpenClaw 跑求职工作流，踩过 Heartbeat 配置、Channel 断线恢复、Skill 安全审查等坑" | （大厂一般不问这个） |
| "你的项目和 OpenClaw 是什么关系？" | "OpenClaw 是我的 Agent Runtime，我在上面开发了求职场景的 Skills 套件" | "OpenClaw 验证了 Always-On Agent 的范式，但它的安全模型不够，所以我独立实现了安全治理、审计回放和多 Agent 编排，然后反向把深度模块通过 Skill 接口接回 OpenClaw 生态" |
| "Browser Use 怎么处理反爬？" | "Playwright Stealth 模式 + 请求限速 + 人工审批兜底，核心不是绕反爬，是辅助提效" | 同左 |
| "MCP 协议的价值？" | "标准化接口，方便接新工具和换模型" | "标准化工具调用接口，降低供应商锁定风险，一套 Agent 跨模型透明切换" |
| "你的 Agent 和 RPA 区别？" | "RPA 是固定流程，我的 Agent 基于 LLM 理解做自适应执行，能处理非标准化页面" | 同左，加："而且我做了失败恢复、偏差纠正和 Evaluator 质量门控" |

---

## 六、简历叙事设计

### 6.1 三项目递进叙事

```
[Resona]       模型层 → 证明你会 数据工程 + SFT/DPO 对齐
    ↓
[ScholarMind]  认知层 → 证明你会 Multi-Agent + RAG + 工程化交付
    ↓
[OfferPilot]   执行层 → 证明你会 OpenClaw + Browser Use + MCP + 安全治理 + 长期自治
```

三个项目合在一起，覆盖了 Agent 系统从**模型对齐 → 认知推理 → 自治执行**的完整链路。

### 6.2 简历项目描述模板——小厂版（突出 OpenClaw）

```
OfferPilot — 基于 OpenClaw 的求职运营 Agent    2026.03-至今
开源地址：https://github.com/wanghong5233/OfferPilot

项目描述：基于 OpenClaw 框架独立设计并实现面向求职场景的垂直执行型
Agent 系统，开发了 5 个 OpenClaw Skills 并发布到 ClawHub，利用
Heartbeat 机制实现 7×24 岗位监控与状态追踪，通过飞书 Channel 主动
推送求职进展。系统覆盖岗位监控、JD 理解、匹配评分、材料定制、
网申自动填充到面试情报生成的完整求职闭环。

核心技术：

1. OpenClaw Skills 套件开发与发布
   设计并实现 job-monitor / resume-tailor / company-intel /
   application-tracker / interview-prep 共 5 个 OpenClaw Skills，
   采用 SKILL.md + Node.js 模块架构，通过 ClawHub 发布供社区使用。
   配置 Heartbeat 定时巡检 + 飞书 Channel 主动推送，实现 Always-On
   求职状态追踪。

2. Browser Use 自动执行引擎（Playwright）
   构建浏览器代理层，支持招聘平台表单自动识别与填充。引入 DOM
   快照 + 截图双模态验证，提交前强制 Human-in-the-Loop 审批。
   通过 OpenClaw Skill 接口暴露给 OpenClaw 调度。

3. 多 Agent 编排与结构化 Prompt（LangGraph）
   Planner-Executor-Evaluator 三角色协作，基于 JD 语义理解做任务
   拆解与执行计划生成，支持失败重试与偏差纠正。

4. MCP 协议工具集成
   数据库、文件管理、浏览器操作、邮件解析、网页搜索共 5 类
   MCP Server 即插即用，支持 DeepSeek/Qwen/Claude 跨模型透明切换。

5. 安全治理与审计体系
   工具调用预算上限 + 审批令牌 + 防重放机制，完整 Action Timeline
   支持截图回放与历史审计。

项目成果：
· 5 个 OpenClaw Skills 已发布到 ClawHub
· 系统在个人真实求职中持续运行 X 周，累计监控 X 个岗位
· 自动生成 X 份定制简历，网申填充准确率 X%
· 已部署上线并开源，提供完整 Demo 演示
```

### 6.3 简历项目描述模板——大厂版（突出工程深度）

```
OfferPilot — 安全受控的求职运营 Agent（OpenClaw + LangGraph）    2026.03-至今
开源地址：https://github.com/wanghong5233/OfferPilot

项目描述：独立设计并实现面向求职场景的垂直执行型 Agent 系统，采用
OpenClaw 作为 Agent Runtime，自主开发安全治理层与多 Agent 编排引擎，
实现从岗位监控、JD 理解、匹配评分、材料定制、网申自动填充到面试
情报生成的完整求职闭环，强调安全受控的自治执行。

核心技术：

1. Browser Use 自动执行引擎（Playwright）
   构建可编程的浏览器代理层，支持招聘平台表单自动识别与填充、网页
   数据结构化提取。引入 DOM 快照 + 视觉截图双模态验证，提交前强制
   进入 Human-in-the-Loop 审批流程，防止错误投递不可撤回。

2. MCP 协议工具集成
   基于 Model Context Protocol 标准化工具调用接口，实现数据库操作、文件管理、
   浏览器自动化、邮件解析、网页搜索共 5 类 MCP Server 的即插即用，支持
   Claude/DeepSeek/Qwen 跨模型透明切换，降低供应商锁定风险。

3. Planner-Executor-Evaluator 多智能体编排（LangGraph）
   采用状态机驱动的三角色协作架构：Planner 基于 JD 语义理解生成执行
   计划，Executor 调用工具链逐步执行，Evaluator 对执行结果进行质量
   评估与偏差纠正，支持失败自动重试与恢复。

4. 安全治理与审计体系
   为每类工具设定独立调用预算上限，危险操作（提交申请、发送邮件）
   引入审批令牌 + 防重放机制。完整的 Action Timeline 记录所有
   Agent 行为，支持截图逐步回放与历史审计。

5. 长期记忆与自治调度（Always-On）
   基于 PostgreSQL 构建投递状态数据库、ChromaDB 构建向量知识库，OpenClaw Heartbeat
   守护进程定期扫描邮件更新、触发面试提醒、更新匹配评分，实现
   7×24 无人值守的求职状态追踪。

项目成果：
· 系统在个人真实求职中持续运行 X 周，累计监控 X 个岗位
· 自动生成 X 份定制简历材料，辅助完成 X 个职位的申请
· 网申表单自动填充准确率达 X%，审批通过率 X%
· 5 个 OpenClaw Skills 已发布到 ClawHub
· 已部署上线并开源，提供完整可复现的 Demo 演示
```

### 6.4 求职自我介绍——小厂版（突出 OpenClaw 实战）

> "我是苏州大学 AI 硕士，专注 Agent 系统。
>
> 最近我基于 OpenClaw 做了一个求职运营 Agent——OfferPilot。我自己开发了 5 个 OpenClaw Skills 发布到 ClawHub，用 Heartbeat 做 7×24 岗位监控，用 Playwright 做网申自动填充，用飞书 Channel 推送进度。我自己每天真的在用它找实习，系统一直在跑。
>
> 在此之前，我还做了 ScholarMind（学术研究 Agent，Multi-Agent + RAG）和 Resona（MBTI 人格对齐微调），三个项目覆盖了 Agent 从模型到认知到执行的完整链路。
>
> 我不只是会装 OpenClaw，我理解它的 Gateway、Brain、Hands、Memory 架构，也知道它的安全边界在哪里，所以我在上面做了额外的安全治理层。"

### 6.5 求职自我介绍——大厂版（突出工程深度）

> "我是苏州大学 AI 硕士，专注 Agent 系统工程。
>
> 我做了三个 Agent 项目，覆盖不同层次：Resona 解决**模型层**的人格对齐问题，ScholarMind 解决**认知层**的学术推理和 RAG 问题，OfferPilot 解决**执行层**的安全自治执行问题——它用 Playwright 做 Browser Use、用 LangGraph 做多 Agent 编排、用 MCP 标准化工具接口，所有提交操作都经过 Human-in-the-Loop 审批。
>
> 我的论文发表在 TVT（中科院二区 Top），研究安全多智能体强化学习。
>
> 我不只是会调 API——我理解 Agent 系统从对齐、到推理、到执行、到治理的完整链路。"

---

## 七、风险与注意事项

1. **法律合规**：网申自动化需注意各招聘平台 ToS，demo 时始终强调"辅助工具 + 人工审批"定位，不要以"全自动投递"为卖点
2. **隐私保护**：简历和个人信息纯本地存储，不上传第三方服务，demo 时这一点是加分项
3. **别过度工程化**：7 天 MVP 的目标是"能 demo + 能讲故事"，不是做生产级系统，先跑通再打磨
4. **保持真实数据**：用自己真实求职数据跑，面试时讲真实 case 比造假数据有说服力 100 倍
5. **OpenClaw 适配层是锦上添花**：核心链路先做好，最后再加 OpenClaw Skill 适配器，顺序不要搞反

---

## 八、总结

**你要同时吃到 OpenClaw 的热度红利和技术深度的长期价值。**

经过对小厂/初创公司真实 JD 的调研，策略已从"借势但不沾手"调整为**"双轨架构：OpenClaw 原生层吃热度 + 独立深度模块展技术"**。

- **面对小厂**：你有 OpenClaw 部署经验、5 个 Skills 发布在 ClawHub、Heartbeat + Channel 实战、真实项目落地——精准命中他们 JD 里"OpenClaw 搭建智能体与自动化工作流"的要求
- **面对大厂**：你有 LangGraph 多 Agent 编排、Playwright Browser Use、MCP 标准化工具层、安全治理与审计回放——这是他们 JD 里"高可用、高扩展性的 Agent 工程架构"要的东西
- **学历劣势的对冲**：小厂 JD 原文写"We care more about what you've built than academic credentials"，你的项目实战深度可以直接对冲学历差距
- **Dogfooding 优势**：你自己每天在用这个系统找实习，面试时讲的每一个 case 都是真的

> **下一步**：确认立项后，可以直接出 OfferPilot 的完整脚手架代码、数据库 Schema 设计、OpenClaw Skills 模板、LangGraph Agent 编排逻辑初稿，用 vibe coding 方式快速开干。
