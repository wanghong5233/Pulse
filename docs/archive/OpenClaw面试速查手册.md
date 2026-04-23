# OpenClaw 面试速查手册

> 30 分钟速记版 — 覆盖架构、Skills、Agent Loop、安全、部署等高频考点

---

## 一、OpenClaw 是什么？（一句话）

OpenClaw 是一个**开源的、可自托管的 AI Agent 运行时平台**。  
它把 LLM 的推理能力包装成**完整的基础设施层**：一个 Agent 同时接入 WhatsApp / Telegram / Slack / Discord / iMessage 等 7+ 渠道，在你自己的机器上运行，数据全部本地存储。  
GitHub 8 周破 18 万星，2 个月破 24.7 万星，是 2026 年增速最快的开源项目之一。

---

## 二、核心三大架构概念

### 1. Gateway 网关架构（Hub-and-Spoke）

| 要点 | 说明 |
|------|------|
| 定位 | 中枢控制面，WebSocket 服务器（默认 `127.0.0.1:18789`） |
| 核心职责 | 消息路由、会话隔离、健康监控、多渠道接入 |
| 关键设计 | **接口层与智能层分离** — 渠道只是投递机制，Agent 人格/记忆/工具统一管理 |
| 为什么重要 | 1 个 Agent 覆盖所有渠道；用户从 Telegram 切到 Slack，记忆连续 |
| 面试话术 | "Gateway 实现了接口层和 Agent Runtime 的解耦，所有渠道消息归一化后路由到同一个 Agent 实例，保证了状态一致性和运维简洁性" |

### 2. Skills 系统（可插拔能力模块）

| 要点 | 说明 |
|------|------|
| 定义方式 | **Markdown 文件**（`SKILL.md`），YAML Frontmatter 声明元数据 + 正文写行为指令 |
| 内置 52 个 Skill | 音频转写（Whisper）、浏览器自动化、图像生成、Notion/GitHub/Trello 集成、Web 搜索等 |
| 社区市场 ClawHub | 10,700+ 社区 Skill |
| 按需加载 | 不是把所有 Skill 塞进 Prompt，而是**运行时按上下文选择性加载**，节省 Token |
| vs LangChain/CrewAI | LangChain 需写 Python 代码定义 Tool；CrewAI 需程序式定义 Agent Crew。OpenClaw **配置优先**（Markdown + 目录），非工程师也能编辑 |
| 面试话术 | "Skills 是 OpenClaw 的扩展机制，用 Markdown 声明而非编译代码。运行时按需加载避免 Prompt 膨胀。我在 OfferPilot 里用了自定义 Skill（`jd-filter`）来声明 JD 匹配规则，Agent 运行时热加载这些配置" |

**Skill 文件结构示例：**
```yaml
---
name: jd-filter
version: 1.0
description: JD 匹配过滤策略
parameters:
  - name: min_daily_salary
    default: 200
    description: 日薪下限
---
## LLM Decision Rules
### Accept Rules
- 岗位与大模型 Agent 应用方向高度相关
### Reject Rules
- 岗位日薪明确低于200元/天
```

### 3. Local-First 本地优先（记忆 & 隐私）

| 要点 | 说明 |
|------|------|
| 存储格式 | 会话 → JSONL；记忆 → Markdown（`MEMORY.md`、`memory/YYYY-MM-DD.md`）；人格 → `SOUL.md` |
| 数据归属 | 全部明文本地文件，无需"导出"，直接 cp/git |
| 模型无关 | 支持 Claude / GPT-4o / Gemini / Grok / Groq / Mistral / OpenRouter，自带 Key |
| 合规优势 | 数据不经第三方平台，满足医疗/金融/法律场景合规需求 |
| 面试话术 | "Local-First 意味着零厂商锁定。所有状态用开放格式（Markdown、JSONL、SQLite）存储在用户自有基础设施上" |

---

## 三、Agent Loop（核心循环）— 必考

这是 OpenClaw 的 Agent 执行引擎，理解这个等于理解整个框架：

```
Load → Call → Parse → Execute → Append → Loop
  ↑                                         |
  └─────────────────────────────────────────┘
```

| 步骤 | 做了什么 |
|------|----------|
| **Load** | 加载会话历史 + 记忆文件（SOUL.md、MEMORY.md）+ 系统 Prompt |
| **Call** | 带 Tools 列表调用 LLM |
| **Parse** | 解析 LLM 响应 → 纯文本 or `tool_call`（name, parameters） |
| **Execute** | 如果是 tool_call，执行对应 Skill/Tool，拿到结果 |
| **Append** | 把执行结果追加到上下文 |
| **Loop** | 重复，直到 LLM 输出最终文本（不再调用工具） |

**多步任务示例：**
> 用户："帮我搜索 OpenClaw 最新文章并总结"
> - 迭代1: LLM → `search_web("OpenClaw")` → 返回 URL 列表
> - 迭代2: LLM → `fetch_url(url1)`, `fetch_url(url2)` → 返回页面内容
> - 迭代3: LLM → 生成摘要 → 最终文本回复

**关键机制：**
- **Session Lane**：每个会话串行执行，防止并发竞态
- **流式事件**：`stream: "assistant"` 文本流、`stream: "tool"` 工具执行流
- **Hook 点**：`before_tool_call` / `after_tool_call` / `agent:bootstrap` 可拦截

**面试话术：** "Agent Loop 是一个 Load-Call-Parse-Execute-Append 循环，核心是让 LLM 在多轮中积累上下文并自主选择工具，直到任务完成。这与简单 Chatbot 的本质区别是：Agent 能跨多步推理并执行副作用。"

---

## 四、TOOLS.md 与 SKILLS.md 的区别

| | TOOLS.md | SKILLS.md |
|--|----------|-----------|
| 粒度 | 低级原子工具（`execute_shell`、`read_file`、`send_email`） | 高级 Skill 包（Calendar = get_events + create_event + ...） |
| 作用 | 列出 Agent **可调用**的所有工具，Agent 只能用菜单上的东西 | 列出要加载的 Skill 模块路径（ClawHub 或本地） |
| 安全 | 每个工具标注 safety level | Skill 内部可声明权限需求 |

**面试话术：** "TOOLS.md 是细粒度能力清单，控制 Agent 的权限边界。SKILLS.md 是高级模块注册表，一个 Skill 可以捆绑多个 Tool。这种分层设计兼顾了安全控制和功能扩展。"

---

## 五、安全架构（五层纵深防御）— 加分项

| 层 | 机制 |
|----|------|
| **网络安全** | 默认绑定 `127.0.0.1`，远程访问走 SSH 隧道 / Tailscale |
| **认证 & 设备配对** | Token 认证 + 设备密钥对挑战签名 + 人工审批 |
| **渠道访问控制** | DM Pairing（默认陌生人不处理，需审批）、Allowlist、群组 Mention 门控 |
| **工具沙箱** | Docker 隔离 per-session，文件系统/网络/资源可配；工具访问策略链：Tool Profile → Provider → Global → Agent → Group → Sandbox |
| **Prompt 注入防御** | 上下文隔离（用户消息 / 系统指令 / 工具结果分区标记），推荐顶级模型 + 限制工具权限降低爆炸半径 |

---

## 六、四种部署模式

| 模式 | 适用场景 | 特点 |
|------|----------|------|
| **本地开发** | 开发调试 | `pnpm dev` 热重载，绑回环，无需认证 |
| **生产 macOS** | 个人/小团队 | 菜单栏 App，LaunchAgent 后台运行，支持 iMessage |
| **Linux/VM** | 服务器 7×24 | systemd 服务 + SSH 隧道暴露 |
| **Fly.io 容器** | 云部署 | Docker + 持久卷 + 托管 HTTPS |

---

## 七、插件系统（Extension Points）

| 扩展类型 | 用途 |
|----------|------|
| **Provider Plugin** | 接入自定义/私有 LLM |
| **Tool Plugin** | 注册自定义工具 |
| **Memory Plugin** | 替换存储后端（向量库、知识图谱替代默认 SQLite） |
| **Channel Plugin** | 新增消息平台（Matrix、Mattermost 等） |

插件放 `extensions/` 目录，自动发现、热加载。

---

## 八、OpenClaw vs 竞品对比（高频问）

| 维度 | OpenClaw | LangChain | CrewAI |
|------|----------|-----------|--------|
| 定位 | Agent 运行时平台（产品级） | LLM 应用开发框架 | 多 Agent 编排框架 |
| Agent 模式 | 单 Agent Runtime | 框架，需自建 | 多 Agent Crew 协作 |
| 配置方式 | Markdown / YAML 文件 | Python 代码 | Python 代码 |
| 渠道接入 | 7+ 消息平台内置 | 无（需自建） | 无 |
| 适合场景 | 80% 业务场景（客服、运维、内容） | 复杂 RAG / Chain 编排 | 复杂多 Agent 编排 |
| 你项目用了什么 | OpenClaw 做 Agent Runtime + Skill 配置热加载 | — | — |

---

## 九、结合 OfferPilot 回答实战问题

### Q: "你在项目中怎么用 OpenClaw 的？"

> "OfferPilot 用 OpenClaw 作为 Agent Runtime 基础设施。具体来说：
> 1. **Skills 系统**：我写了 `jd-filter` Skill（Markdown + YAML），声明 JD 匹配规则（Accept/Reject 关键词、日薪下限等），Agent 运行时通过 `skill_loader.py` 热加载这些配置，无需重启服务。
> 2. **ProductionGuard**：借鉴 OpenClaw 的 Gateway 守护理念，实现了一个 7×24 守护线程，自动调度打招呼/聊天巡检，时段感知（高峰加密、夜间休眠），资源治理（标签页清理、孤儿进程回收）。
> 3. **安全门控**：四层安全架构 — 规则预过滤 → 方向门控 → LLM 二元判断 → 人工升级，类似 OpenClaw 的分层策略链思想。"

### Q: "OpenClaw 的 Skill 和 LangChain 的 Tool 有什么区别？"

> "最大区别是**声明方式**和**加载策略**。LangChain 的 Tool 需要写 Python 装饰器/类来定义；OpenClaw 的 Skill 是 Markdown 文件，配置优先，非工程师也能修改。  
> 另外 OpenClaw **按需加载**——不是把所有 Skill 塞 Prompt，而是根据对话上下文选择性注入相关 Skill，这对 Token 效率很重要。"

### Q: "为什么选 OpenClaw 而不是纯用 LangGraph？"

> "它们解决不同层面的问题。LangGraph 是状态机编排框架，负责 Workflow 流转逻辑——我在 OfferPilot 的聊天巡检流程就用了 LangGraph 做状态机。  
> OpenClaw 是 Agent Runtime 平台，负责更上层的事：Skill 声明、多渠道接入、本地记忆管理、安全策略。  
> 两者是互补关系：LangGraph 做 Workflow 编排，OpenClaw 做 Runtime 基础设施和 Skill 管理。"

### Q: "Agent Loop 和普通 Chat Completion 有什么区别？"

> "Chat Completion 是单轮请求-响应。Agent Loop 是**多轮循环**：LLM 在每轮可以选择调用工具，工具结果追加到上下文，LLM 基于累积上下文继续推理，直到任务完成。  
> 本质区别：Agent 具有**自主决策和执行副作用**的能力，而非只是生成文本。"

---

## 十、容易被追问的细节

| 问题 | 答案要点 |
|------|----------|
| Gateway 为什么默认绑 127.0.0.1？ | 安全。避免暴露公网，远程访问走 SSH 隧道或 Tailscale |
| Skill 热加载怎么实现的？ | 文件监视器（File Watcher）+ debounce，检测到 SKILL.md 变更自动重新解析 |
| 记忆检索用什么算法？ | SQLite + 向量嵌入，**混合检索**：向量相似度 + BM25 关键词匹配 |
| 工具沙箱怎么做的？ | Docker 容器 per-session 隔离，文件系统/网络独立，执行完销毁 |
| 怎么防 Prompt 注入？ | 上下文分区隔离（用户 / 系统 / 工具结果加 source 标签），用强模型 + 限制工具权限 |
| OpenClaw 有什么缺点？ | 单 Agent 设计（不适合复杂多 Agent 协作）、有安全漏洞历史（2026/01 XSS）、需要一定技术基础 |
| SOUL.md 是什么？ | Agent 的"人格文件"，定义性格、行为准则、回复风格 |

---

**最后提醒：面试时多往你的 OfferPilot 实战经验上引，展示你不只是知道概念，而是真正用 OpenClaw 的理念构建了生产级系统。**
