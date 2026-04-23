# OfferPilot 项目实施方案（可行性审核版）

> 更新时间：2026-03-16
> 本文档与 `openclaw调研.md` 配套使用。调研文档回答"为什么做"，本文档回答"怎么做能真的落地"。

---

## -1、当前进度对齐（2026-03-16）

> 目的：防止执行与方案脱节。以下是当前真实状态，与后续开发的基线。

### 架构迁移（2026-03-16 完成）

- [x] **全 WSL 原生架构迁移**：PostgreSQL / Backend / Frontend 全部从 Docker 迁移到 WSL 原生运行
- [x] Docker 容器、镜像、卷全部清理（零残留），`docker-compose.dev.yml` 已删除
- [x] PostgreSQL 16 WSL 原生安装（端口 5432），数据库 + 12 张表已建
- [x] `DATABASE_URL` 更新为 `postgresql://offerpilot:offerpilot@127.0.0.1:5432/offerpilot`
- [x] `BOSS_HEADLESS=false`（浏览器可见，调试可直接观察 Agent 操作）
- [x] 一键启动脚本 `scripts/start.sh`（PG + Backend + Frontend，Ctrl+C 全部停止，`[SYS]` 心跳日志）
- [x] 一键初始化脚本 `scripts/setup.sh`（PG + Python deps + Playwright + Frontend deps）
- [x] BOSS 首次登录脚本 `scripts/boss-login.sh`（Playwright 打开浏览器扫码登录，Cookie 持久化）
- [x] Makefile 精简为纯 WSL 命令（`make setup` / `make boss-login` / `make start` / `make ps` / `make health`）
- [x] README 全文更新反映新架构

### Agent 可观测性架构升级（2026-03-14 完成）

- [x] **`agent_events.py` 事件总线**：线程安全 EventBus，支持订阅/取消订阅/历史回放（200 条滚动窗口）
- [x] **`GET /api/agent/events` SSE 端点**：实时推送 Agent 事件流到前端，自动 keepalive + 断线重连
- [x] **`GET /api/agent/events/history` REST 端点**：非 SSE 客户端历史查询
- [x] **`log_action()` 增强**：新增 `screenshot_path` 参数，写入 `actions` 表并自动触发事件推送
- [x] **`boss_scan.py` 步骤级事件**：浏览器启动/导航/提取/截图/关闭全程 emit 结构化事件
- [x] **`boss_chat_workflow.py` 全链路事件**：LLM 调用/意图分类/安全拦截/回复发送/工作流节点均 emit 事件
- [x] **`boss_workflow.py` 工作流事件**：扫描节点/分析持久化节点 emit 事件
- [x] **前端 Agent 实时监控面板**：暗色终端风格，SSE 订阅，事件类型过滤/自动滚动/清空，彩色标签区分
- [x] **前端审计时间线增强**：截图标识列（图标 + tooltip），筛选功能
- [x] **LangSmith 追踪配置**：`.env` 新增 `LANGCHAIN_TRACING_V2` / `LANGCHAIN_API_KEY` / `LANGCHAIN_PROJECT`
- [x] **OpenClaw 结构化日志配置脚本**：`scripts/openclaw-logging-setup.sh`，自动配置 JSON 日志 + OTEL 提示
- [x] README + 实施方案文档同步更新

### 飞书通知与告警体系升级（2026-03-14 完成）

- [x] **`email_notify.py` 重构**：支持 `feishu_text` / `feishu_card` / `generic` 三种模式，分级告警（info/warning/critical 对应蓝/橙/红卡片）
- [x] **`notify_alert()` 通用告警函数**：title + message + level + fields 结构化参数
- [x] **`notify_cookie_expired()` 专用函数**：Cookie 过期紧急告警，含操作指引
- [x] **`notify_daily_summary()` 每日摘要**：扫描/聊天/回复/介入/邮件统计 + 异常列表
- [x] **BOSS Cookie 过期检测**：`boss_scan.py` 中导航后检测 URL/DOM 登录标志，过期则截图 + 事件 + 飞书告警 + 提前返回
- [x] **`POST /api/notify/daily-summary` 端点**：查询当日 actions 统计并推送飞书摘要
- [x] `.env` 环境变量重命名 `EMAIL_NOTIFY_*` → `NOTIFY_*`，升级 `NOTIFY_MODE=feishu_card`，含飞书 Webhook 获取指引注释

### 已完成（对齐阶段 0）

- [x] WSL2 + Node 22 + OpenClaw 可用
- [x] OpenClaw 已接通阿里 Key（DashScope 兼容）
- [x] 模型主备已落地：`qwen3-max` 主 + `qwen-plus` 备
- [x] FastAPI 已运行在 `127.0.0.1:8010`
- [x] PostgreSQL 16 WSL 原生运行（端口 5432，已从 Docker 迁移）
- [x] `job-monitor` Skill 可触发并完成 OpenClaw -> FastAPI 调用

### 已完成（阶段 1 部分）

- [x] `/api/jd/analyze` 从 mock 升级为 LangGraph 第一版流程（Parser -> Matcher -> Gap）
- [x] 增加了结构化输出与模型主备失败降级（fallback）机制
- [x] LangGraph 结果已写入 PostgreSQL `jobs/actions`
- [x] `jd_history` 已落地到 ChromaDB，本地相似 JD 检索接入 `/api/jd/analyze`（`similar_jobs`）
- [x] 新增 `GET /api/jobs/recent` 供前端看板读取真实数据
- [x] 新增 `POST /api/resume/index`，可将简历文本切块入库 `resume_chunks`
- [x] Next.js + Tailwind 前端看板已创建并对接真实后端接口（分析 / 入库 / 列表）

### 下一步（现在继续做）

- [ ] 用你的真实简历文本执行一次 `/api/resume/index`，让评分真正使用个人经历证据
- [x] 为 `jd_history` 增加阈值过滤与去重策略（相似度门槛）
- [x] 前端增加“评分依据片段（resume_chunks 命中项）”展示
- [x] 进入阶段 2：材料生成与人工审批 MVP（后端接口 + 前端看板）
- [x] 将阶段 2 的审批流从 `pending_threads` 升级为 LangGraph `interrupt_before + checkpoint`（可重启恢复）
- [x] 阶段 2 收尾：审批通过后导出 PDF/TXT、复制话术、`resume-tailor` Skill 首版
- [x] 阶段 3 MVP 起步：`/api/boss/scan`（单关键词扫描 + 截图 + 入库）

---

## 〇、硬件与环境可行性审核

### 你的设备

| 项 | 配置 |
|---|---|
| 内存 | 32 GB |
| 显存 | 8 GB |
| 操作系统 | Windows 10/11 |

### 结论：完全够用

| 组件 | 硬件需求 | 你的情况 | 结论 |
|------|---------|---------|------|
| **OpenClaw 本体** | 最低 4 GB RAM，Node.js 22+ | 32 GB，远超要求 | ✅ 无压力 |
| **LLM** | 走 API（DeepSeek/Qwen/Claude），**不跑本地模型** | 不占显存 | ✅ 无需 GPU |
| **Playwright** | 约 300–500 MB 内存 | 32 GB | ✅ |
| **PostgreSQL** | 约 200–500 MB 内存 | 32 GB | ✅ |
| **ChromaDB**（嵌入模式） | 约 100–300 MB 内存 | 32 GB | ✅ |
| **Next.js 开发服务器** | 约 200–500 MB 内存 | 32 GB | ✅ |
| **FastAPI 开发服务器** | 约 100–200 MB 内存 | 32 GB | ✅ |
| **WSL2（如果用）** | 默认占 1–2 GB 内存 | 32 GB | ✅ |
| **全部同时运行** | 约 3–5 GB 总占用 | 32 GB | ✅ 非常宽裕 |

**关于你的 8 GB 显存：** 本项目完全不需要显存。所有 LLM 调用都走云端 API（DeepSeek API 极便宜，规划类请求约 ¥1/百万 token），不在本地跑模型。如果你后期想用 Ollama 跑 7B 量化模型做离线测试，8 GB 显存也刚好够 Qwen2.5-7B-Q4，但这是可选的，不影响核心功能。

**API 成本预估（轻度使用）：**

| 模型 | 用途 | 估算月成本 |
|------|------|----------|
| Qwen3-Max | 主模型（JD 解析、匹配评分、材料生成） | 视调用量而定（面试展示优先质量） |
| Qwen-Plus | 回退模型（超时/限流时兜底） | 通常低于 Max |
| DeepSeek | 可选备用 Provider（多模型切换演示） | 低成本 |
| OpenClaw 本身 | 免费（MIT 协议） | ¥0 |

**OpenClaw 安装路线：** 你是 Windows，两种路线都可以：
- **推荐：WSL2 + Ubuntu**（更稳定，社区教程多，32 GB 内存完全扛得住 WSL2 开销）
- 备选：原生 PowerShell（会遇到更多 PATH 和 C++ 编译工具的坑）

---

## 一、产品需求侧分析（基于真实求职场景）

### 1.1 BOSS 直聘平台机制调研

在设计需求之前，必须先搞清楚 BOSS 直聘的实际运行规则，否则做出来的东西根本用不了。

**BOSS 直聘 Web 端核心页面：**

| 页面 | URL | 功能 |
|------|-----|------|
| 岗位搜索 | `zhipin.com/web/geek/jobs?query=...&city=...&salary=...` | 搜索/筛选岗位，参数可通过 URL 传递（城市代码、薪资代码） |
| 聊天列表 | `zhipin.com/web/chat/index` | 所有会话的消息列表，可读取 HR 消息、发送回复、发送附件简历 |
| 岗位详情 | `zhipin.com/job_detail/xxx.html` | 岗位 JD 详情 + "立即沟通"按钮 |
| 登录 | `zhipin.com/web/user/?ka=header-login` | 微信扫码 / 手机号登录 |

**平台限制与反作弊机制：**

| 限制项 | 详情 | 对 Agent 的影响 |
|------|------|----------------|
| 打招呼次数 | 每天约 100 次（普通用户），VIP 更多 | 需要做配额管理，优先打匹配度高的岗位 |
| 同一 HR 消息频率 | 每天 ≤ 3 条消息，间隔 24 小时 | 回复策略需限速，不能连续发多条 |
| 动态令牌 | `__zp_stoken__` + 浏览器指纹检测 | 必须用真实浏览器 + 用户自己的登录态 |
| 默认招呼语 | BOSS 有内置默认招呼语，会覆盖自定义消息 | 需要在设置中禁用默认招呼语 |
| 沟通方式切换 | 初期"小窗沟通"，大量打招呼后切为"沟通列表" | DOM 选择器需要兼容两种模式 |
| 已读状态 | "已读"仅表示对话框打开，不代表 HR 真的看了 | 不能以已读作为"HR 感兴趣"的判断依据 |

**开源项目验证（Playwright 自动化可行性已被证实）：**

| 项目 | Stars | 能力 |
|------|-------|------|
| `geekgeekrun/geekgeekrun` | 1520★ | 最成熟的 BOSS 自动化项目 |
| `engvuchen/boss-zhipin-robot-web` | 63★ | Node.js + Puppeteer，支持筛选条件配置、批量打招呼、消息模板 |
| `ufownl/auto-zhipin` | 13★ | Playwright + LLM，自动拉取岗位 + 生成个性化文案 + 自动发起沟通 |
| `wensia/boss-zhipin-automation` | 23★ | FastAPI + React + Playwright，二维码登录 + 多账号 + 批量打招呼 |

> **关键结论：** BOSS 直聘 Web 端自动化已有大量开源实践，核心技术路线是 **Playwright + Cookie 持久化 + 限速 + Stealth**。自动搜索、打招呼、发消息、发附件简历均可实现。

### 1.2 用户真实行为分析

**日常求职行为拆解：**

```
[时间线] 工作日一天的求职流程：

  10:00 - 12:00  HR 在线高峰期①
    → 打开 BOSS → 搜索"AI Agent 实习" → 浏览岗位列表
    → 看 JD → 判断是否匹配 → 匹配的就打招呼 → 发默认消息
    → 如果 HR 回复了，发简历 → HR 问问题就回答
    → 重复 50-100 次（广撒网）

  12:00 - 15:00  午休 / 做项目
    → 偶尔看看邮箱有没有面试通知

  15:00 - 17:00  HR 在线高峰期②
    → 重复上午的操作
    → 回复上午积累的 HR 消息
    → 查看有没有 HR 主动打招呼

  晚上
    → 整理今天的投递情况
    → 看邮件有没有笔试/面试安排
    → 规划明天重点投哪些公司
```

**痛点分析：**

| 行为 | 痛点 | Agent 能做什么 |
|------|------|--------------|
| 搜索岗位 + 浏览 JD | 每天重复浏览几百个 JD，大量无关岗位浪费时间 | 自动搜索 + 匹配度评分 + 过滤不匹配的 |
| 打招呼 | 每天手动发 50-100 条打招呼消息，内容高度雷同 | 自动发送可配置的默认招呼语 |
| 回复 HR 常规问题 | "你期望日薪多少？""工作地点在哪？""能来多久？"——每天回答几十遍一样的话 | 从预配置的求职画像中自动回复 |
| 发简历 | HR 表示感兴趣后需要发附件简历——手动操作繁琐 | 识别 HR 意图后自动发送 |
| 超出能力范围的问题 | HR 问技术细节或需要判断的问题，Agent 不该乱答 | 通知用户介入 |
| 邮件管理 | 面试邀请、笔试通知散落在邮箱里，容易遗漏 | 自动分类 + 日程提醒 |

### 1.3 修正后的需求定义

**三个核心场景，按优先级排序：**

```
[场景 1] BOSS 直聘沟通 Agent（主战场，核心价值）
    真实痛点：每天要在 BOSS 上重复搜索→打招呼→回复→发简历 50-100 次
    Agent 定位：在 HR 在线高峰期（可配置），自动执行"搜→筛→招呼→对话"闭环

    子功能：
    ① 定时岗位扫描     在配置的时间段（如工作日 10-12 / 15-17）自动搜索
    ② 智能匹配筛选     简历 × JD 语义匹配，只处理匹配度达标的岗位
    ③ 自动打招呼       对匹配岗位发送可配置的默认招呼消息
                       例："您好，27应届硕士在读，可实习3~6个月，每周可实习5天"
    ④ 消息监听         持续监听聊天列表中 HR 的回复
    ⑤ 智能回复         HR 的回复分三种情况：
       a) HR 表达兴趣 / 要简历 → 自动发送附件简历
       b) HR 问常规问题（日薪、地点、到岗时间…）→ 从求职画像配置中回复
       c) HR 问超出范围的问题 → 不回复，通知用户介入
    ⑥ HR 主动联系处理  检查该 HR 的岗位是否匹配 → 匹配则走上述流程 → 不匹配则不予理睬

[场景 2] 邮件智能秘书（重要，第二优先级）
    真实痛点：面试/笔试通知散落在邮箱，容易遗漏或搞混时间
    Agent 定位：只读邮件秘书，帮你从邮件中提取日程并提醒

    子功能：
    ① 邮件拉取         IMAP 只读接入（严格控制：只读不删不改）
    ② 求职邮件分类     LLM 分类：面试邀请 / 笔试通知 / 拒信 / 补材料 / 无关
    ③ 日程提取         从邮件正文中提取时间、地点、面试形式等结构化信息
    ④ 智能提醒         按时间线排列待办事项 → 飞书/微信推送提醒
                       例："明天 14:00 字节跳动 AI 应用工程师一面（线上 Zoom）"
    ⑤ 状态看板同步     自动更新投递状态：已投递 → 笔试中 → 面试中 → 已 Offer / 已拒

[场景 3] 大厂官网监控（可选，低优先级）
    真实痛点：大厂校招/实习招聘页偶尔开放新岗位，但不会主动通知
    Agent 定位：定时检查配置的官网 URL，发现新岗位时通知用户

    子功能：
    ① URL 监控         配置一组大厂招聘页 URL，Heartbeat 定时访问
    ② 新岗位检测       对比上次快照，发现新增岗位时提取信息
    ③ 通知             通过飞书/微信推送新岗位概要

    限制说明：
    - 不做表单填充（官网表单填一次就完了，不值得自动化，且需要登录态）
    - 不做简历改写（简历是求职者自己精心打磨的，不应该交给 Agent）
    - 投递状态追踪受限于各官网登录态，暂不做
```

**明确砍掉或降级的功能（避免伪需求）：**

| 功能 | 决定 | 原因 |
|------|------|------|
| 网申表单自动填充 | **砍掉** | 每个官网只填一次，不是重复性工作，不适合 Agent |
| 简历自动改写 | **降为工具** | 简历是核心竞争力，必须人工把控；已有的材料生成保留为辅助工具，但不作为 Agent 核心能力 |
| 日历同步 | **砍掉** | 邮件秘书直接推送飞书提醒即可 |
| RSS 监控 | **砍掉** | BOSS 和官网都没有 RSS |
| "自动投递" | **绝不做** | 法律风险 + 投简历必须人工确认 |

### 1.4 BOSS 直聘 Agent 核心交互流程

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                 BOSS 直聘沟通 Agent 核心闭环                                 │
│                                                                              │
│  ── 主动搜索场景（HR 在线高峰期触发） ──                                      │
│                                                                              │
│  [1] 定时触发     Heartbeat 在配置时段（如 10:00-12:00, 15:00-17:00）启动     │
│       ↓                                                                      │
│  [2] 岗位搜索     Playwright 按配置的关键词/城市/薪资搜索 BOSS                 │
│       ↓           限速 3-5s + Stealth + 用户自己的登录态                      │
│  [3] JD 理解      LLM 结构化抽取技能要求、业务方向、匹配加分项                │
│       ↓                                                                      │
│  [4] 匹配评分     简历 × JD 语义匹配 → 匹配度 ≥ 阈值才继续                   │
│       ↓                                                                      │
│  [5] 自动打招呼   发送可配置的默认招呼消息（需先禁用 BOSS 默认招呼语）         │
│       ↓           配额管理：按日限额控制打招呼数量                            │
│  [6] 进入消息监听                                                            │
│                                                                              │
│  ── 消息监听与智能回复（持续运行） ──                                          │
│                                                                              │
│  [7] 消息轮询     定期检查聊天列表中的未读消息                                │
│       ↓                                                                      │
│  [8] 意图识别     LLM 分类 HR 消息意图：                                     │
│       │           ├── 表达兴趣 / 要简历 → [9a] 发送附件简历                   │
│       │           ├── 问常规问题（日薪/地点/到岗…）→ [9b] 从求职画像回复       │
│       │           ├── 问超出范围的问题 → [9c] 不回复 + 通知用户介入            │
│       │           └── 拒绝 / 无意义消息 → 标记状态 + 不再回复                 │
│       ↓                                                                      │
│  [9a] 发送简历    在聊天窗口中发送预配置的附件简历                            │
│  [9b] 画像回复    从 profile.yaml 中查找对应字段 → 生成礼貌的回复文案         │
│  [9c] 通知升级    飞书/微信推送："XX 公司 HR 问了一个我无法回答的问题，请介入"  │
│                                                                              │
│  ── HR 主动联系场景 ──                                                        │
│                                                                              │
│  [10] 收到 HR 主动打招呼                                                     │
│       ↓                                                                      │
│  [11] 岗位匹配检查  查看该 HR 关联的岗位 JD → 评分                           │
│       ↓                                                                      │
│       ├── 匹配度达标 → 回复招呼 + 进入 [7] 消息监听                          │
│       └── 匹配度不达标 → 不予理睬 + 记录日志                                 │
│                                                                              │
│  ── 审计层 ──                                                                │
│  [12] 所有操作写入 Action 日志 + 关键步骤截图                                │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 1.5 求职画像配置（Agent 回复的知识库）

Agent 回复 HR 常规问题时，不是凭空生成，而是严格依据用户预配置的求职画像：

```yaml
# profile.yaml — 求职画像配置文件
personal:
  name: "王宏"
  education: "211硕士在读（2027届）"
  major: "计算机科学与技术"
  graduation_year: 2027
  age: 24

job_preference:
  target_positions: ["AI Agent 工程师", "大模型应用工程师", "LLM 应用开发"]
  work_cities: ["深圳", "广州", "北京", "上海", "杭州"]
  expected_daily_salary: "200-300元/天"
  internship_duration: "3-6个月"
  available_days_per_week: 5
  earliest_start_date: "一周内到岗"
  is_remote_ok: true

default_greeting: "您好，27应届硕士在读，可实习3~6个月，每周可实习5天"

# Agent 回复边界控制
reply_policy:
  # 可自动回复的问题类型
  auto_reply_topics:
    - "expected_salary"        # 期望薪资
    - "work_location"          # 工作地点
    - "start_date"             # 到岗时间
    - "internship_duration"    # 实习时长
    - "weekly_availability"    # 每周可实习天数
    - "education_background"   # 学历背景
    - "graduation_year"        # 毕业年份
  # 绝对不自动回复的场景
  escalate_topics:
    - "technical_questions"    # 技术问题
    - "salary_negotiation"     # 薪资谈判
    - "project_details"        # 项目细节追问
    - "personal_questions"     # 个人隐私问题
    - "unknown"                # 无法归类的问题
  # 回复风格
  tone: "礼貌、简洁、专业"
  max_auto_replies_per_hr: 3  # 对同一 HR 自动回复上限，超过则通知用户
```

> **安全边界设计：** Agent 只在 `auto_reply_topics` 范围内自动回复。任何不在白名单中的问题一律升级通知用户。这既避免了 Agent 乱答话造成不良印象，又体现了 Human-in-the-Loop 的安全设计——面试时的加分点。

### 1.6 修正后的 Agent 核心闭环总览

```
┌──────────────────────────────────────────────────────────────────┐
│                 OfferPilot Agent Loop（修正版 v2）                │
│                                                                  │
│  ── 场景 1：BOSS 直聘沟通 Agent（核心）──                         │
│  [1] 定时岗位扫描   Heartbeat 在 HR 高峰期触发搜索               │
│       ↓             Playwright + 限速 + 用户登录态               │
│  [2] JD 理解        LLM 结构化抽取技能要求                       │
│       ↓                                                          │
│  [3] 匹配评分       简历 × JD 语义匹配 + Gap 分析                │
│       ↓                                                          │
│  [4] 自动打招呼     发送可配置默认消息 + 配额管理                 │
│       ↓                                                          │
│  [5] 消息监听       轮询聊天列表中 HR 的回复                     │
│       ↓                                                          │
│  [6] 智能回复       意图识别 → 发简历 / 画像回复 / 通知用户       │
│                                                                  │
│  ── 场景 2：邮件智能秘书 ──                                      │
│  [7] 邮件拉取       IMAP 只读 + 求职邮件筛选                     │
│       ↓                                                          │
│  [8] 智能分类       面试邀请/笔试通知/拒信/补材料/无关            │
│       ↓                                                          │
│  [9] 日程提醒       提取时间 → 排列待办 → 飞书推送               │
│                                                                  │
│  ── 场景 3：大厂官网监控（可选）──                                │
│  [10] URL 定时检查   Heartbeat 定时访问配置的招聘页               │
│        ↓                                                         │
│  [11] 新岗位检测     对比快照 → 有新增则通知用户                  │
│                                                                  │
│  ── 横切面 ──                                                    │
│  [12] 公司情报      目标公司/团队/技术栈/面试题自动调研           │
│  [13] Action 审计   完整行为日志 + 关键步骤截图回放               │
└──────────────────────────────────────────────────────────────────┘
```

---

## 二、BOSS 直聘自动化的可行性方案

这是整个项目最关键也最有风险的模块。不同于原方案只涉及"扫描岗位"，修正后的需求覆盖 **搜索 → 打招呼 → 消息监听 → 智能回复 → 简历发送** 的完整沟通闭环。

### 2.1 已有验证

GitHub 上已有多个开源项目证实 BOSS 直聘 Web 端自动化的可行性：

- **`geekgeekrun`（1520★）**：最成熟的项目，已大规模使用
- **`auto-zhipin`**：Playwright 自动拉取岗位 + LLM 生成文案 + 自动发起沟通。其 `boss_zhipin.py` 的 `HrDialog.send()` 方法验证了通过 `.input-area` + `.send-message` DOM 选择器发送消息的可行性
- **`boss-zhipin-robot-web`**：Puppeteer 实现，支持批量打招呼 + 筛选条件 + 消息模板 + 错误通知。明确记录了两种沟通 UI 模式（小窗 vs 沟通列表）的切换问题及解决方案

### 2.2 BOSS 直聘自动化的四层能力

```
┌──────────────────────────────────────────────────────────┐
│  Layer 1: 浏览器会话管理                                  │
│  · Playwright 启动 Chromium（headless 可选）               │
│  · 首次手动扫码登录 → Cookie 持久化到 cookies.json        │
│  · 后续自动加载 Cookie 免登录                             │
│  · Stealth: --disable-blink-features=AutomationControlled │
├──────────────────────────────────────────────────────────┤
│  Layer 2: 岗位搜索 + JD 抓取（已有实现 /api/boss/scan）   │
│  · 导航到搜索页面 → 滚动加载 → 解析岗位卡片              │
│  · 点击岗位 → 提取 JD 详情 → 过滤已见/不活跃 HR          │
│  · 限速 3-5s 随机延迟 → 截图存档                         │
├──────────────────────────────────────────────────────────┤
│  Layer 3: 打招呼 + 消息发送（新增）                       │
│  · 在岗位详情页点击"立即沟通"按钮                        │
│  · 等待对话框出现 → 填充输入框 → 点击发送                │
│  · 附件简历发送：点击聊天窗口的"附件"按钮 → 上传文件      │
│  · DOM 选择器：.input-area (输入框), .send-message (发送) │
│  · 配额追踪：每日打招呼计数 → 达到上限自动停止            │
├──────────────────────────────────────────────────────────┤
│  Layer 4: 消息监听 + 智能回复（新增，核心差异化能力）     │
│  · 定时导航到聊天列表页 zhipin.com/web/chat/index         │
│  · 提取未读消息列表 → 逐个读取 HR 最新消息               │
│  · LLM 意图识别 → 分类为"要简历/问问题/拒绝/其他"        │
│  · 根据分类执行对应动作（回复/发简历/通知/忽略）          │
│  · 同一 HR 回复上限控制（≤3 条/天）                      │
└──────────────────────────────────────────────────────────┘
```

### 2.3 消息意图识别与回复策略

```python
# HR 消息意图分类（LLM Structured Output）
class HRMessageIntent(BaseModel):
    intent: Literal[
        "request_resume",      # "发一下简历" / "简历发来看看"
        "ask_salary",          # "期望薪资多少" / "日薪什么要求"
        "ask_location",        # "在哪个城市" / "能来北京吗"
        "ask_availability",    # "什么时候能到岗" / "能实习多久"
        "ask_education",       # "什么学校" / "什么专业"
        "express_interest",    # "可以来面试" / "约个时间聊聊"
        "reject",              # "不太合适" / "暂时不需要"
        "technical_question",  # 技术相关问题
        "unknown"              # 无法归类
    ]
    confidence: float          # 0.0 - 1.0
    extracted_question: str    # 提取的核心问题

# 回复决策矩阵
REPLY_STRATEGY = {
    "request_resume":    Action.SEND_RESUME,      # 自动发送附件简历
    "ask_salary":        Action.REPLY_FROM_PROFILE, # 从 profile.yaml 回复
    "ask_location":      Action.REPLY_FROM_PROFILE,
    "ask_availability":  Action.REPLY_FROM_PROFILE,
    "ask_education":     Action.REPLY_FROM_PROFILE,
    "express_interest":  Action.NOTIFY_USER,       # 通知用户，这是好机会
    "reject":            Action.MARK_REJECTED,     # 标记状态，不再回复
    "technical_question": Action.NOTIFY_USER,      # 不回复 + 通知用户介入
    "unknown":           Action.NOTIFY_USER,       # 不回复 + 通知用户介入
}
```

**安全守卫：**
- `confidence < 0.7` 时一律不自动回复，升级通知用户
- 同一 HR 自动回复次数达上限后不再自动回复
- 所有自动回复的消息内容 + HR 原始消息都写入审计日志
- 首版 MVP 可设为"预览模式"：生成回复内容但不实际发送，需用户确认

### 2.4 技术风险与合规

| 风险 | 等级 | 应对 |
|------|------|------|
| BOSS 封号 | 中 | 用自己的真实登录态 + 限速 + 每天打招呼不超 100 次，行为模式接近真人操作频率 |
| 消息发错 | 高 | MVP 阶段先做"预览模式"（生成不发送）；正式模式有 confidence 阈值 + 回复上限 + 审计日志 |
| HR 察觉自动化 | 低 | 回复内容基于 LLM 生成，自然度高；限速模拟人工打字速度 |
| 法律合规 | 中 | 始终定位为"求职辅助工具"而非"无差别爬取"，用户使用自己的账号和登录态 |
| DOM 结构变化 | 中 | 选择器封装为配置项，不硬编码；核心选择器有 fallback 策略 |

### 2.5 备选方案（最保守）

如果 BOSS 反爬太严或你不想承担自动化风险：
- Layer 1-2：正常使用，岗位扫描已验证可行
- Layer 3：打招呼改为"生成文案 → 你手动复制粘贴"
- Layer 4：消息回复改为"你截图 HR 消息 → Agent 建议回复内容 → 你手动发送"

**即使退回到最保守方案，项目的技术含量不减**——LLM 意图识别、结构化输出、求职画像匹配、安全守卫设计、审计日志这些面试能讲的点都在。

---

## 三、OpenClaw Skill 开发的实际复杂度

你可能担心"开发 OpenClaw Skill"很复杂。实际上：

**最简单的 Skill 只需要一个 `SKILL.md` 文件：**

```
~/.openclaw/workspace/skills/
└── job-monitor/
    └── SKILL.md     ← 只要这一个文件
```

SKILL.md 的内容就是**自然语言指令**，告诉 OpenClaw 的 Brain 在什么场景下做什么事：

```markdown
---
name: job-monitor
description: 监控 BOSS 直聘上的 AI Agent 相关岗位，每日汇总新增职位
---

# 岗位监控 Skill

## 触发时机
当用户说"查岗位""看看有没有新职位""岗位更新"时激活本技能。
也可以通过 Heartbeat 每天早上 9:00 自动触发。

## 工作流程
1. 读取 ~/.openclaw/memory/target-keywords.md 获取目标关键词列表
2. 对每个关键词，使用浏览器工具打开 BOSS 直聘搜索页面
3. 提取搜索结果中的职位名称、公司、薪资、JD 摘要
4. 与 ~/.openclaw/memory/seen-jobs.md 对比，过滤已见过的
5. 将新岗位追加到 seen-jobs.md
6. 生成今日岗位简报，通过当前渠道发送给用户
```

这不是写代码，是**写 Prompt**——你已经很擅长了。复杂 Skill 可以加 `scripts/` 目录放 Node.js 脚本，但 MVP 阶段纯 SKILL.md 就够用。

---

## 四、技术架构决策（面试必问，提前想清楚）

实施方案不能只写"用什么工具"，必须回答"为什么选这个架构"——这是大厂和高质量小厂面试时必问的。

### 4.1 Agent 编排框架选型：为什么用 LangGraph

调研报告里淘天 JD 原文要求"掌握 **LangChain/LlamaIndex/AutoGen** 框架及实现原理"，小红书要求"规划执行、工具调用、记忆与状态管理、Agent Runtime 设计"。OfferPilot 的编排层必须用**业界主流框架**，不能只是裸写 Prompt 串调用。

| 框架 | 是否适合 OfferPilot | 原因 |
|------|-------------------|------|
| **LangGraph** | ✅ 最优选 | 状态机驱动，适合有审批节点的多步工作流；支持条件分支、人工中断、状态持久化；面试时能画出清晰的状态图 |
| LangChain AgentExecutor | ❌ 不选 | 黑盒式 ReAct 循环，不够可控，不适合需要人工审批的场景 |
| AutoGen | 可选但不优 | 更适合多 Agent 对话场景，OfferPilot 是工作流驱动而非对话驱动 |
| CrewAI | 可选但不优 | 角色定义方便，但底层不如 LangGraph 灵活 |
| 裸写 Python | ❌ 不选 | 面试时讲不出框架选型理由，简历关键词也缺失 |

### 4.2 OfferPilot 的 LangGraph 状态机设计

项目有两套核心工作流，分别覆盖"搜索投递"和"消息沟通"两个高频场景。面试时画这两张图就能讲 15 分钟。

**工作流 A：岗位搜索 + JD 分析（已实现）**

```
                    ┌─────────────┐
                    │   START     │
                    └──────┬──────┘
                           ↓
                    ┌─────────────┐
                    │  JD 解析    │ ← LLM 结构化抽取
                    │  (Parser)   │
                    └──────┬──────┘
                           ↓
                    ┌─────────────┐
                    │  匹配评分   │ ← 简历 × JD 语义匹配（RAG）
                    │ (Matcher)   │
                    └──────┬──────┘
                           ↓
                  ┌────────┴────────┐
                  ↓                 ↓
          match_score ≥ 阈值   match_score < 阈值
                  ↓                 ↓
          ┌─────────────┐   ┌─────────────┐
          │ 标记待沟通  │   │  归档低匹配  │
          │(ReadyToChat)│   │  (Archive)   │
          └──────┬──────┘   └─────────────┘
                 ↓
          ┌─────────────┐
          │ 自动打招呼  │ ← 发送可配置默认消息 + 配额检查
          │ (Greeter)   │
          └──────┬──────┘
                 ↓
          ┌─────────────┐
          │ 状态更新    │ ← 更新 jobs 状态 + 审计日志
          │  (Update)   │
          └──────┬──────┘
                 ↓
              ┌──────┐
              │ END  │
              └──────┘
```

**工作流 B：BOSS 消息监听 + 智能回复（新增核心能力）**

```
                    ┌─────────────┐
                    │   START     │ ← Heartbeat 定时触发 / 手动触发
                    └──────┬──────┘
                           ↓
                    ┌─────────────┐
                    │ 消息拉取    │ ← Playwright 读取聊天列表未读消息
                    │(MsgFetcher) │
                    └──────┬──────┘
                           ↓
                    ┌─────────────┐
                    │ 来源判断    │ ← 是"我主动打招呼后 HR 回复"
                    │(SourceCheck)│    还是"HR 主动联系我"？
                    └──────┬──────┘
                      ┌────┴─────┐
                      ↓          ↓
              HR回复我的招呼   HR主动联系
                      ↓          ↓
                      │    ┌─────────────┐
                      │    │ 岗位匹配检查│ ← 获取该 HR 岗位信息 → 评分
                      │    │(JobMatcher) │
                      │    └──────┬──────┘
                      │      ┌────┴─────┐
                      │      ↓          ↓
                      │  匹配达标    不匹配
                      │      ↓          ↓
                      │      │    ┌──────────┐
                      │      │    │ 忽略+日志│
                      │      │    └──────────┘
                      ↓      ↓
                    ┌─────────────┐
                    │ 意图识别    │ ← LLM 分类 HR 消息：
                    │(IntentClass)│    要简历/问薪资/问地点/感兴趣/
                    └──────┬──────┘    技术问题/拒绝/其他
                           ↓
              ┌────────────┼────────────┐
              ↓            ↓            ↓
        要简历/表达兴趣  问常规问题    超出范围/技术问题/未知
              ↓            ↓            ↓
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │发送简历  │ │画像回复  │ │通知用户  │
        │(SendCV)  │ │(Profile  │ │(Escalate)│
        │          │ │ Reply)   │ │          │
        └────┬─────┘ └────┬─────┘ └────┬─────┘
             ↓            ↓            ↓
             │     从 profile.yaml     │
             │     查找对应字段         │
             │     → LLM 生成礼貌回复  │
             ↓            ↓            ↓
        ┌─────────────────────────────────┐
        │          安全守卫检查            │
        │  · confidence ≥ 0.7?            │
        │  · 同一 HR 回复次数 ≤ 上限?     │
        │  · 回复内容是否在白名单范围?     │
        │  → 不通过 → 升级通知用户         │
        └──────────────┬──────────────────┘
                       ↓
                ┌─────────────┐
                │ 执行回复    │ ← Playwright 在聊天窗口输入并发送
                │ (MsgSender) │    或预览模式：仅生成不发送
                └──────┬──────┘
                       ↓
                ┌─────────────┐
                │ 审计记录    │ ← HR原始消息 + 意图分类 + 回复内容
                │ (AuditLog)  │    + 执行结果 全部写入 actions 表
                └──────┬──────┘
                       ↓
                    ┌──────┐
                    │ END  │
                    └──────┘
```

**这两张图的面试价值：**
- 展示你理解**有向图状态机**的 Agent 编排模式，且能设计多个子工作流
- 工作流 B 的**意图识别 → 分支决策 → 安全守卫**体现真正的 Agent 自主决策能力
- `profile.yaml` 驱动的回复策略体现**可配置的安全边界设计**
- `confidence 阈值` + `回复上限` + `白名单检查` 体现多层 **Human-in-the-Loop** 安全设计
- 失败兜底路径（一律升级通知用户）体现**容错与风险控制**
- 对标 JD 里"规划执行、工具调用、记忆与状态管理、Agent 安全"的所有关键词

### 4.3 MCP 工具层设计

MCP（Model Context Protocol）是 2026 年行业标准协议。OfferPilot 的每个外部能力都封装成 MCP Server：

| MCP Server | 功能 | 实现方式 | 对应阶段 |
|------------|------|---------|---------|
| `job-db` | 岗位/投递/行为数据库 CRUD | Python MCP Server，暴露 `add_job` / `update_status` / `query_jobs` / `log_action` 等工具 | 阶段 1 |
| `file-manager` | 简历/求职信文件读写导出 | Python MCP Server，暴露 `read_resume` / `write_cover_letter` / `export_pdf` 等工具 | 阶段 2 |
| `browser-use` | Playwright 浏览器操作 | Python MCP Server，暴露 `navigate` / `click` / `fill_form` / `screenshot` / `extract_text` 等工具 | 阶段 3 |
| `email-reader` | IMAP 邮件读取与分类 | Python MCP Server，暴露 `fetch_unread` / `classify` / `mark_read` 等工具 | 阶段 5 |
| `web-search` | 网页搜索（公司情报采集） | Python MCP Server，封装搜索引擎 API（SerpAPI / Tavily），暴露 `search` / `scrape_page` 等工具 | 阶段 6 |

> **总计 5 个自研 MCP Server**，按阶段递增开发。MVP（阶段 0–2）只需前 2 个。

**面试时讲 MCP 的价值：** 工具调用接口标准化后，换模型（DeepSeek → Qwen → Claude）不需要改工具代码，只需要切 LLM Provider。新增工具只需加一个 MCP Server，Agent 编排层零修改。

### 4.5 OpenClaw 与 LangGraph 的协作关系（面试必问：两个编排层为什么不冲突？）

这是整个架构最容易被面试官质疑的点："OpenClaw 有 Brain 做编排，LangGraph 也做编排，为什么要两层？"

**答案：两者分工不同，不是竞争关系。**

```
┌──────────────────────────────────────────────────────────┐
│               用户入口 ①：飞书/微信 Channel                │
│  用户发消息 → OpenClaw Gateway → Brain（意图识别）          │
│       → 路由到对应 Skill → Skill 内发 HTTP 请求             │
│       → FastAPI 后端 → LangGraph 工作流执行                │
│       → 结果返回 → OpenClaw 通过 Channel 回复用户           │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│               用户入口 ②：Web 前端（Next.js）              │
│  用户操作 → 前端 API 调用 → FastAPI 后端                   │
│       → LangGraph 工作流执行 → SSE 流式返回                │
│       → 前端展示结果 / 审批 UI                             │
└──────────────────────────────────────────────────────────┘
```

| 层级 | 组件 | 职责 | 类比 |
|------|------|------|------|
| **消息路由层** | OpenClaw（Gateway + Brain + Channel） | 接收用户消息、意图识别、路由到 Skill、定时调度（Heartbeat） | 类似"智能前台" |
| **业务编排层** | LangGraph（FastAPI 内） | JD 解析、匹配评分、材料生成、审批流程、Browser Use 等复杂工作流 | 类似"后端大脑" |
| **工具执行层** | MCP Server | 浏览器操作、邮件读取、文件管理、数据库操作 | 类似"手脚" |

**为什么不把所有逻辑都放在 OpenClaw Skill 里？**
- OpenClaw 的 Brain 擅长自然语言理解和简单任务编排，但**不支持** LangGraph 的状态持久化、条件路由、interrupt_before 人工审批等高级特性
- 复杂的多步工作流（如：解析 JD → 评分 → 条件判断 → 生成材料 → 暂停审批 → 恢复执行）需要 LangGraph 的状态机能力
- OpenClaw 做**轻量路由 + 定时调度**，LangGraph 做**重型业务逻辑**，各司其职

**OpenClaw Skill 的实际角色：薄桥接层**

每个 Skill 的核心逻辑只有一件事：把用户意图翻译成 FastAPI 接口调用。例如 `job-monitor` Skill 的 SKILL.md 核心逻辑是：

```
当用户说"分析一下这个JD"时：
1. 提取用户消息中的 JD 文本
2. 调用 HTTP POST http://localhost:8010/api/jd/analyze，body 为 JD 文本
3. 将返回的 JSON 格式化为易读的消息发送给用户
```

OpenClaw 的 Brain 看到这个 Skill 后，会用内置的 HTTP 工具去调用 FastAPI，FastAPI 内部用 LangGraph 跑完整工作流，结果返回给 OpenClaw 再转发给用户。

**面试时的表述：** "OpenClaw 负责消息入口和定时调度，LangGraph 负责业务编排，两者通过 HTTP 桥接。这样做的好处是：① 用户可以通过飞书和网页两种方式使用系统；② OpenClaw 的 Heartbeat 可以在无人值守时触发 LangGraph 工作流；③ 即使不用 OpenClaw，LangGraph 工作流也能独立运行和测试。"

#### 4.5.1 OpenClaw 社区成功经验（用于当前项目落地）

参考官方文档与社区 Showcase，总结出对 OfferPilot 最有价值的可复用模式：

1. **Heartbeat 做“轻巡检”，Cron 做“准点任务”**  
   - 官方建议：周期感知与批处理优先 Heartbeat，精确时点与隔离重任务用 Cron（isolated session）。  
   - 落地到 OfferPilot：BOSS 消息轮询走 Heartbeat/高频 Cron；日报和周报走 isolated Cron。

2. **Skill 只做薄桥接，复杂状态放 LangGraph**  
   - 社区高可维护项目普遍采用“Skill 路由 + 后端工作流编排”分层。  
   - 落地到 OfferPilot：`boss-chat-copilot` 仅触发 `/api/boss/chat/process`，状态机在后端子图维护。

3. **默认“保守可控”：LLM 负责理解，规则负责兜底**  
   - 官方安全文档明确：系统提示不是硬边界，真正边界来自工具策略、审批、沙箱、allowlist。  
   - 落地到 OfferPilot：LLM Structured Output 负责意图和策略规划，最终由白名单/阈值/回复次数/升级话题做硬拦截。

4. **防循环与防失控要工程化**  
   - 官方提供 loop-detection（同参数重复调用/无进展 ping-pong 检测）。  
   - 落地到 OfferPilot：后续在 OpenClaw 配置里开启 loopDetection，避免 BOSS 巡检链路异常重试导致 token 浪费和噪音。

5. **社区高分案例共同点：先跑通单链路，再扩多 Agent**  
   - Showcase 里稳定项目都先把一个高价值流程打磨到可持续运行，再加更多 Agent。  
   - 落地到 OfferPilot：坚持“BOSS 对话主链路优先”，邮件与官网监控是扩展层，不抢主线资源。

官方参考链接：  
- `https://docs.openclaw.ai/automation/cron-vs-heartbeat.md`  
- `https://docs.openclaw.ai/tools/loop-detection.md`  
- `https://docs.openclaw.ai/gateway/security/index.md`  
- `https://docs.openclaw.ai/start/showcase.md`

### 4.6 前后端通信协议

| 场景 | 协议 | 原因 |
|------|------|------|
| LLM 流式输出（JD 分析、材料生成） | **SSE（Server-Sent Events）** | 单向流式推送，实现简单，FastAPI 原生支持 `StreamingResponse` |
| 审批操作（approve / reject） | **REST API（POST）** | 一次性操作，不需要持久连接 |
| 看板数据刷新 | **轮询 / REST API** | MVP 阶段够用，后期可升级为 WebSocket |

> SSE 比 WebSocket 简单得多（不需要额外协议握手），对 MVP 来说完全够用。LangGraph 的 `interrupt_before` 审批通过 REST API 触发 `graph.invoke(Command(resume=...), config)` 即可恢复执行。

### 4.7 技术栈完整清单

| 层级 | 选型 | 面试关键词覆盖 |
|------|------|-------------|
| Agent Runtime | **OpenClaw** | "OpenClaw 部署经验""Skill 开发" |
| Agent 编排 | **LangGraph（langgraph Python 包）** | "LangChain 生态""状态机""Agent 框架" |
| LLM 调用 | **LangChain ChatModel 接口** | "LangChain 框架及实现原理" |
| 工具协议 | **MCP** | "MCP 协议实现方案" |
| Browser Use | **Playwright (playwright-stealth)** | "Browser Use / Computer Use" |
| 向量检索 | **ChromaDB + LangChain Retriever** | "RAG""向量数据库""ChromaDB" |
| Embedding | **text-embedding-v3 (阿里) 或 BGE** | "Embedding 模型" |
| 后端 | **FastAPI (Python)** | "Python""后端开发" |
| 前端 | **Next.js 14 + Tailwind CSS** | "全栈" |
| 关系存储 | **PostgreSQL（WSL 原生）** | "数据库" |
| 向量存储 | **ChromaDB（嵌入模式，零额外服务）** | "向量数据库""ChromaDB" |
| 消息渠道 | **OpenClaw Channel（飞书/微信）** | "多渠道接入" |
| 部署 | **全 WSL 原生（开发）/ Docker Compose（生产可选）** | "工程化交付" |
| 模型 | **Qwen3-Max（主）+ Qwen-Plus（备）+ DeepSeek（可选）** | "多模型适配""成本优化" |

---

## 五、分阶段实施计划

不按天数，按"每个阶段做完能得到什么"来组织。每个阶段结束都有**可演示的成果**。

每个阶段都标注了**涉及的技术深度点**，确保做出来的东西不只是"能用"，还能在面试中讲出技术含量。

### 阶段 0：环境搭建（预计 1 天）

**目标：** 所有工具能跑起来，你能和 OpenClaw 对话。

**具体操作：**
- [ ] WSL2 安装 Ubuntu（如果还没装）
- [ ] Node.js 22 安装（nvm）
- [ ] OpenClaw 安装 + Gateway 启动
- [ ] 配置 DashScope API Key，默认模型设为 `qwen3-max`（`qwen-plus` 作为 fallback）
- [ ] 接入微信或飞书 Channel，验证能收发消息
- [ ] Heartbeat 配置验证（设一个"每小时报时"测试）
- [ ] Python 虚拟环境，安装核心依赖：`langgraph` / `langchain` / `langchain-community` / `langchain-openai` / `fastapi` / `playwright` / `psycopg[binary]` / `chromadb` / `langchain-chroma`
- [x] **PostgreSQL（WSL 原生安装）**：`apt install postgresql`，端口 5432，`scripts/setup-pg.sh` 自动建库建表
- [ ] **ChromaDB 无需额外部署**：`pip install chromadb` 完成，数据持久化到本地 `./chroma_db/` 目录，嵌入模式运行，零额外服务
- [ ] FastAPI 骨架 + 数据库初始化脚本
- [ ] Next.js 脚手架 + Tailwind 初始化

> **存储职责划分：**
> - **PostgreSQL**：业务关系数据（jobs / applications / actions 三张表），支持 JOIN 查询和事务
> - **ChromaDB**：向量数据（简历段落 + JD 历史），嵌入模式无需独立服务，`from langchain_chroma import Chroma` 直接用

**阶段成果：** OpenClaw 在你笔记本上跑着，飞书/微信能和它聊天。LangGraph 和 FastAPI 的 hello world 能跑。PostgreSQL + ChromaDB 双存储就绪。

---

### 阶段 1：JD 理解 + 匹配引擎（预计 2–3 天）

**目标：** 给它一段 JD 文本，能返回结构化分析和你的匹配度。

**这是整个项目的"最小价值单元"——做完这一步就已经有用了。**

**技术深度点：LangGraph 工作流初建 + 结构化输出 + RAG 匹配**

**具体操作：**
- [ ] 数据库 Schema 设计（见下方）
- [ ] **LangGraph 第一版工作流**：`JD解析 → 匹配评分 → Gap分析`（三个节点的简单链）
- [ ] JD 结构化解析节点：用 LangChain `ChatModel` + **Structured Output**（Pydantic Schema 约束 LLM 输出 JSON），输出 `JDAnalysis(title, company, skills[], direction, bonus_skills[], salary_range)`
- [ ] **简历结构化预处理**：将你的简历按项目/技能/教育三维度拆分为 10–20 个语义段落，Embedding 后存入 **ChromaDB**（collection：`resume_chunks`）
- [ ] 匹配评分节点（**RAG 在此处发挥作用**）：对 JD 每个 skill 要求，用 ChromaDB 检索最相关的简历段落，再交给 LLM 做精细打分 0–100。这比直接把整份简历丢给 LLM 更精准——因为简历有 3 个项目 + 多段技能描述，全文塞进 Prompt 会导致 LLM 注意力分散
- [ ] Gap 分析节点：JD 要求中匹配度低于阈值的技能点 → 生成 Gap 报告
- [ ] **JD Embedding 存储**：每个解析过的 JD 也存入 ChromaDB（collection：`jd_history`）。当新 JD 进来时，自动检索历史相似 JD，展示"与你之前分析过的 XX 公司 YY 岗位相似度 92%，匹配度对比：..."——这是 RAG 的第二个价值点，随使用积累数据越多越有价值
- [ ] 开发 OpenClaw Skill：**`job-monitor`**（SKILL.md，用户发 JD 文本给 OpenClaw → 调用 FastAPI `/api/jd/analyze` 接口 → 返回结构化分析 + 匹配度。后续阶段 3 会扩展该 Skill 增加 BOSS 自动搜索能力）
- [ ] 前端看板骨架：岗位列表页（手动录入 JD → 展示分析结果 + 匹配度 + Gap + 相似历史 JD）

> **RAG 的两个真实用途（面试时要讲清楚）：**
> 1. **简历段落精准检索**：JD 技能要求 → ChromaDB 检索最相关的简历段落 → LLM 精细打分。比全文匹配更精准。
> 2. **JD 历史相似度检索**：新 JD → ChromaDB 检索历史相似 JD → 对比分析，帮助判断优先级。数据越积累越有价值。

**数据库核心表（PostgreSQL，仅存业务关系数据）：**

```sql
-- 岗位表
CREATE TABLE jobs (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    source      TEXT NOT NULL,       -- 'boss' / 'official_site' / 'manual'
    source_url  TEXT,
    jd_raw      TEXT NOT NULL,       -- JD 原文
    jd_parsed   JSONB,              -- LLM 结构化解析结果
    match_score REAL,               -- 匹配评分 0-100
    gap_analysis TEXT,              -- Gap 分析文本
    status      TEXT DEFAULT 'new', -- new/applied/interviewing/offered/rejected
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 投递记录表
CREATE TABLE applications (
    id          TEXT PRIMARY KEY,
    job_id      TEXT REFERENCES jobs(id),
    resume_version TEXT,            -- 用了哪个版本的简历
    cover_letter TEXT,              -- 生成的求职信
    applied_at  TIMESTAMP,
    channel     TEXT,               -- 'boss_chat' / 'official_form' / 'email'
    notes       TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Agent 行为日志表
CREATE TABLE actions (
    id          TEXT PRIMARY KEY,
    job_id      TEXT REFERENCES jobs(id),
    action_type TEXT NOT NULL,      -- 'jd_parse' / 'match' / 'generate' / 'browse' / 'approve'
    input_summary TEXT,
    output_summary TEXT,
    screenshot_path TEXT,           -- 截图路径（Browser Use 时）
    status      TEXT,               -- 'success' / 'failed' / 'pending_approval'
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

```

> **向量数据由 ChromaDB 独立管理（不在 PostgreSQL 里）：**
>
> ```python
> # ChromaDB 两个 Collection（persistent_client 持久化到本地目录）
> import chromadb
> client = chromadb.PersistentClient(path="./chroma_db")
>
> resume_chunks = client.get_or_create_collection(
>     name="resume_chunks",
>     metadata={"hnsw:space": "cosine"}
> )
> # chunk_type: 'project' / 'skill' / 'education'
>
> jd_history = client.get_or_create_collection(
>     name="jd_history",
>     metadata={"hnsw:space": "cosine"}
> )
> # 每次分析 JD 后存入，用于历史相似度检索
> ```
>
> **PostgreSQL 只存业务关系数据，ChromaDB 只存向量数据，两者职责严格分离。**

**阶段成果：** 你把 BOSS 上看到的 JD 复制粘贴进系统，它告诉你匹配度多少、缺什么技能。OpenClaw 通过飞书也能用（你发 JD 给它，它回复分析结果）。

**面试可讲的技术点：** LangGraph 状态图编排、Pydantic Structured Output、RAG 语义匹配、ChromaDB 向量检索、关系数据库与向量数据库的职责分离设计。

---

### 阶段 2：材料生成 Agent（预计 2–3 天）

**目标：** 针对某个 JD 自动改写简历 bullet 和生成求职信。

**技术深度点：LangGraph 多节点 + 条件路由 + Human-in-the-Loop 中断**

**具体操作：**
- [x] **扩展 LangGraph 工作流**：在阶段 1 的基础上，增加 `Generator → HumanReview → Finalize` 节点
- [x] `Generator` 节点：LangChain ChatModel 生成三类材料——简历 bullet 改写、求职信、BOSS 打招呼话术（150 字以内）
- [x] **条件路由**：`match_score ≥ 60` 才进入 Generator，否则直接归档并告知用户"匹配度偏低，建议跳过"
- [x] **HumanReview 中断节点**：LangGraph `interrupt_before` / `interrupt` 机制，生成材料后暂停，等用户在前端审批
- [x] Diff 对比展示：原始简历 vs 改写后版本，高亮差异（已实现 Regenerate 对比 + 原始简历行级 Diff）
- [x] 审批通过后 → 导出定制简历 PDF / 复制话术
- [x] 审批拒绝 → 回到 Generator 重新生成（LangGraph 条件路由 + `regenerate`）
- [x] 开发 OpenClaw Skill：`resume-tailor`（首版）
- [x] 前端：材料生成页面（选择岗位 → 生成 → 审批/拒绝/重生成）

**当前落地状态（2026-03-14）：**
- 后端已新增 `POST /api/material/generate`、`POST /api/material/review`、`GET /api/material/pending`
- 审批线程由 LangGraph checkpoint + PostgreSQL `material_threads` 管理，支持可恢复 `approve/reject/regenerate`
- 审批通过后写入 PostgreSQL `applications`，并记录 `actions` 审计日志
- 前端看板已接入一键生成材料、加载待审批线程、审批操作、复制话术、导出 PDF/TXT

**阶段成果：** 选一个 BOSS 上的岗位，系统一键生成定制简历和打招呼话术，你审批后导出。

**面试可讲的技术点：** LangGraph 条件边与路由、Human-in-the-Loop（interrupt_before）、Agent 可控性设计。

---

### 阶段 3：BOSS 直聘搜索 + 自动打招呼（预计 2–3 天）

**目标：** Playwright 在你的浏览器里辅助操作 BOSS 直聘：自动搜索岗位 + 匹配评分 + 对匹配岗位自动打招呼。

**技术深度点：Browser Use Agent + MCP 工具封装 + Playwright Stealth + 配额管理**

**具体操作：**
- [x] **构建 `browser-use` MCP Server**（Python）：封装 Playwright 为标准 MCP 工具，暴露 `navigate(url)` / `click(selector)` / `extract_text(selector)` / `screenshot()` / `wait(ms)` 接口（最小可运行版）
- [x] Playwright 持久化浏览器上下文（保留你的 BOSS 登录态）
- [x] **LangGraph 新增 `BossScanner` 子图**：`搜索 → 提取JD → 批量解析`（首版单关键词/单页）
- [x] 每个 JD 自动点进去 → 抓取完整 JD 文本 → 送入阶段 1 的解析引擎（`BOSS_FETCH_DETAIL=true` 时启用）
- [x] 操作限速：每次点击间隔 3–5 秒随机延迟（可配置）
- [x] Stealth 模式配置（可选开关，自动降级）
- [x] 关键步骤截图保存 → 写入 `actions` 表（审计日志）
- [x] **扩展 OpenClaw Skill `job-monitor`**：增加 BOSS 自动搜索能力（用户对 OpenClaw 说"搜一下深圳 AI Agent 实习" → Skill 调用 FastAPI `/api/boss/scan` → 触发 BossScanner 子图）
- [ ] **自动打招呼功能（新增）**：
  - [ ] 匹配评分 ≥ 阈值的岗位，自动点击"立即沟通" → 发送 `profile.yaml` 中配置的 `default_greeting`
  - [ ] 打招呼前检查：① BOSS 默认招呼语已禁用 ② 今日配额未用尽 ③ 该 HR 未沟通过
  - [ ] 打招呼配额管理：每日计数 → 达上限自动停止 → 日志记录
  - [ ] DOM 选择器兼容两种 UI：小窗沟通模式 vs 沟通列表模式

**当前落地状态（2026-03-15 凌晨）：**
- 已新增 `POST /api/boss/scan`（单关键词扫描 + 可配置 `max_items`）
- 若 Playwright 环境缺失会返回明确错误（安装指引已写入 README）
- 扫描结果支持写入 `jobs/actions` 与 `jd_history`，并保存截图路径用于审计
- 已新增 `backend/mcp/browser_use_server.py`，可独立运行 browser-use MCP 服务
- **自动打招呼功能尚未实现**，是本阶段下一步重点

**降级方案：** 如果 BOSS 反爬太严导致不稳定，退回到"手动复制 JD + 系统解析 + 生成打招呼话术你手动复制粘贴"。

**阶段成果：** 你对 OpenClaw 说"帮我搜一下深圳的 AI Agent 实习"，它自动在 BOSS 上搜索、抓取、分析，对匹配度高的岗位自动发送打招呼消息，推送汇总到飞书。

**面试可讲的技术点：** Playwright 浏览器自动化、MCP Server 封装、反反爬策略（stealth + 限速）、配额管理与安全设计、Browser Use Agent。

---

### 阶段 4：BOSS 消息监听 + 智能回复（预计 3–4 天）

**目标：** 持续监听 BOSS 聊天消息，根据求职画像自动回复 HR 常规问题，超出能力范围则通知用户。

这是整个项目**最核心的差异化能力**——从单向的"搜索+打招呼"进化为双向的"对话 Agent"。

**技术深度点：消息意图识别 + 多分支决策 + 可配置求职画像 + 多层安全守卫 + 审计**

**具体操作：**
- [x] **求职画像配置系统**：
  - [x] 定义 `profile.yaml` 结构（见 1.5 节），包含个人信息、求职偏好、回复策略
  - [x] 后端 API：`GET/PUT /api/profile` 读取和更新画像
  - [x] 前端配置面板：可编辑个人信息 + 求职偏好 + 可自动回复的话题白名单
- [x] **消息拉取模块**：
  - [x] Playwright 导航到 `zhipin.com/web/chat/index`（手动触发版：`/api/boss/chat/pull`）
  - [x] 提取聊天列表中有未读标记的会话（`unread_only` 过滤）
  - [x] 逐个进入会话 → 读取 HR 最新消息文本（`fetch_latest_hr=true`）
  - [x] 消息去重：已处理过的消息不重复分析（`boss_chat_events.message_signature`）
- [x] **HR 主动联系处理（MVP）**：
  - [x] LLM 检测聊天消息是否属于“HR 主动首轮联系”（`proactive_contact`）
  - [x] 基于 `company/job_title/latest_hr_message` 做主动联系匹配评分（`proactive_match_score`）
  - [x] 匹配达标且表达兴趣 → 回复默认招呼并进入正常沟通流程
  - [x] 匹配不达标 → 不予理睬 + 记录日志（`proactive_match_passed=false`）
  - [x] 增强版：用 `company+job_title+latest_hr_message` 构造伪 JD，复用阶段 1 `run_jd_analysis` Matcher 做精细评分（`BOSS_CHAT_PROACTIVE_JD_ENRICHMENT=true` 默认开启）
- [x] **意图识别模块**：
  - [x] LLM + Structured Output 分类 HR 消息意图（见 2.3 节 `HRMessageIntent`）
  - [x] 输出 intent 类型 + confidence 分数 + 提取的核心问题
- [x] **智能回复决策引擎**：
  - [x] 根据 intent 类型查 `REPLY_STRATEGY` 决策矩阵：
    - `request_resume` → 在聊天窗口发送附件简历
    - `ask_salary / ask_location / ask_availability / ask_education` → 从 `profile.yaml` 查字段 → LLM 生成礼貌回复
    - `express_interest` → 通知用户（好机会，请亲自跟进）
    - `reject` → 标记岗位状态为 rejected，不再回复
    - `technical_question / unknown` → 不回复 + 通知用户介入
  - [x] 新增 **LLM 策略规划层**：`intent + profile_policy -> action + policy_topic + reason`（Structured Output）
  - [x] **安全守卫层**：confidence < 0.7 → 升级通知；同一 HR 回复达上限 → 停止自动回复；回复内容不在白名单 → 升级通知
- [x] **预览模式（MVP 必须）**：
  - [x] 首版默认"预览模式"：生成回复内容但不实际发送，展示在前端等用户确认
  - [x] 用户可在前端切换"自动模式"（满足安全守卫条件则直接发送，需 BOSS_CHAT_AUTO_EXECUTE_ENABLED=true）
- [x] **LangGraph `BossChatCopilot` 工作流（预览模式子图）**：
  - [x] 复用阶段 3 的 Playwright 会话拉取能力与阶段 4 的回复决策引擎
  - [x] 已落地节点链路：LoadProfile → PullConversations → SourceCheck → ProactiveGate → Decision(去重+意图分类+回复决策+审计)
  - [x] 支持 Heartbeat 定时触发（每 5-10 分钟轮询一次消息）：`/api/boss/chat/heartbeat/trigger` + OpenClaw cron 模板
- [x] **通知机制**：
  - [x] 复用阶段 5 已有的 `send_channel_notification` 飞书 webhook
  - [x] 通知内容模板："{公司} HR 问了：{问题摘要}，请在 BOSS 上回复"
- [x] **审计日志**：
  - [x] 每条 HR 消息 + Agent 分类结果 + 回复内容 + 执行结果 全部写入 `actions` 表
  - [x] 前端 Action Timeline 可回溯所有自动回复记录
- [x] 开发 / 扩展 OpenClaw Skill：`boss-chat-copilot`（首版，调用 `/api/boss/chat/process`）

**当前落地状态（2026-03-15）：**
- 已新增后端 `GET/PUT /api/profile`（求职画像持久化，PostgreSQL `user_profiles`）
- 已新增后端 `POST /api/boss/chat/pull`（聊天列表拉取 + 未读过滤 + 审计截图）
- 已新增后端 `POST /api/boss/chat/process`（批量读取 HR 最新消息 + 去重 + 决策）
- 已新增后端 `POST /api/boss/chat/heartbeat/trigger`（定时巡检摘要 + 可选通道通知）
- 已新增 `backend/app/boss_chat_workflow.py`（LangGraph `BossChatCopilot` 预览模式子图）
- 已新增后端 `POST /api/boss/chat/reply-preview`（意图识别 + 决策矩阵 + 安全守卫 + 升级通知）
- 已新增 LLM SourceCheck（来源匹配评分）：低匹配会话自动阻断高风险动作（如发简历）
- 已新增 LLM ProactiveGate（HR 主动联系识别 + 匹配门禁）：低匹配主动联系自动忽略并记录
- 已新增 OpenClaw Skill：`skills/boss-chat-copilot/SKILL.md`（批量巡检与回复建议）
- 已更新 Heartbeat 脚本：`scripts/openclaw_heartbeat_setup.sh` 支持 `boss-chat-copilot` 定时巡检消息模板
- 已接入前端“BOSS 对话 Copilot”面板（画像编辑、聊天列表拉取、批量处理、单条预览决策）
- 已增强 Heartbeat 稳定性：异常时返回 200 + ok=false + error，不抛 500，便于 Cron 报告
- 已实现自动发送执行层：Playwright 输入框+发送按钮，前端预览/自动切换，环境变量总开关

**阶段成果：** Agent 在 HR 在线高峰期自动搜索、打招呼，然后持续监听 HR 回复。HR 说"发一下简历"→ 自动发；HR 问"在哪个城市"→ 自动回复"深圳，也可接受远程"；HR 问技术问题 → 飞书通知你介入。

**面试可讲的技术点：** LLM 意图识别 + Structured Output、多分支决策引擎、可配置安全策略（confidence 阈值 + 回复上限 + 白名单）、预览模式 vs 自动模式的渐进式信任设计、Agent 对话状态管理、Heartbeat 驱动的消息轮询。

---

### 阶段 5：邮件智能秘书 + 状态追踪 + Heartbeat（预计 2 天）

**目标：** 邮件自动解析 + 日程提醒 + 投递看板状态更新 + 每日自动巡检。

**技术深度点：MCP 邮件工具 + LangGraph 异步子图 + OpenClaw Heartbeat 自治调度 + 日程提取**

**具体操作：**
- [x] **构建 `email-reader` MCP Server**（Python）：用 `imaplib` 读邮件，暴露 `fetch_unread(folder, since)` / `classify(email_id)` / `mark_read(email_id)` MCP 工具
- [x] 邮件分类：LangChain ChatModel + Structured Output，输出 `EmailClassification(type: 面试邀请|笔试通知|拒信|补材料|无关, interview_time?, company?)`
- [x] **LangGraph `EmailTracker` 子图**：`拉取未读 → 分类 → 更新数据库 → 推送通知`
- [x] 自动更新 jobs 表的 status 字段
- [x] **日程提取与智能提醒（增强）**：
  - [x] 从邮件正文中提取结构化日程：面试时间、笔试时间、地点、面试形式（线上/线下）、联系方式
  - [x] 存入 `schedules` 表，按时间线排列
  - [x] 提前 N 小时（可配置）通过飞书推送提醒："明天 14:00 字节跳动 AI 应用工程师一面（线上 Zoom，链接：xxx）"
  - [x] 前端日程看板：按日期展示待办事项
- [x] **安全边界：IMAP 严格只读**，不删除、不修改、不转发任何邮件
- [x] OpenClaw Heartbeat 配置：
  - 每天 9:00 自动触发 `EmailTracker` 子图
  - 每天 9:00 检查 BOSS 有无新岗位（如果阶段 3 稳定的话）
  - 有更新时自动推送到飞书
- [x] 开发 OpenClaw Skill：`email-reader` + `application-tracker`
- [x] 前端：投递看板（看板视图：待投递 / 已投递 / 笔试中 / 面试中 / 已拒 / 已 Offer）

**阶段成果：** 每天早上飞书自动收到一条消息："昨日收到 1 封面试邀请（字节 AI 应用工程师，3月20日 14:00 线上 Zoom），1 封拒信。明天有 2 场面试待准备。"

**面试可讲的技术点：** MCP Server 开发实践、IMAP 协议集成（只读安全设计）、结构化日程提取、Heartbeat 自治调度、Always-On Agent 设计。

---

### 阶段 6：公司情报 + 安全治理 + 打磨（预计 2–3 天）

**目标：** 补全情报功能和审计体系，做出可 demo 的完整产品。

**技术深度点：安全预算机制 + Action Replay 审计 + Agent 评测指标 + 工程化交付**

**具体操作：**
- [x] 公司情报 Prompt：输入公司名 → 用搜索工具自动收集 → 输出结构化报告（业务方向、技术栈、团队规模、融资阶段、面试风格）
- [x] 面试题库：基于 JD + 公司情报 → 生成可能的面试问题 + 建议答法
- [x] 开发 OpenClaw Skill：`company-intel` + `interview-prep`
- [x] **安全治理层实现（首版）**：
  - [x] 工具调用预算管理：每类工具设定调用次数上限（如 browser 操作 ≤ 50 次/session）
  - [x] 审批令牌机制：危险操作（提交申请、发邮件）需要一次性令牌授权
  - [x] 防重放校验：相同操作不会因 LLM 幻觉被重复执行
- [x] **Action Timeline 页面（首版）**：按时间轴展示所有 Agent 行为 + 关键步骤截图回放
- [x] **Agent 评测指标（首版）**（对标字节 JD 的"Agent 评测体系"要求）：
  - 匹配评分一致性：同一 JD 多次评分的标准差
  - 表单填充准确率：自动填充字段 vs 人工修正率
  - 材料生成质量：人工评审通过率（approve vs reject）
  - 端到端延迟：从输入 JD 到完成材料生成的时间
- [x] UI 整体打磨 + 响应式适配
- [x] Docker Compose 配置首版（已完成后迁移为全 WSL 原生架构，Docker 文件保留供生产部署参考）
- [x] README 阶段性更新（新增阶段 6 接口与技能说明）
- [ ] 录制 Demo 视频
- [x] 全 WSL 原生架构迁移（PostgreSQL / Backend / Frontend 脱离 Docker）
- [x] BOSS 首次登录脚本 `scripts/boss-login.sh`（Cookie 持久化）
- [x] 一键启动 `scripts/start.sh` + `[SYS]` 心跳日志
- [ ] **Agent 可观测性架构升级**（SSE 实时事件流 + screenshot_path 补全 + LangSmith 集成，见第十二章）
- [x] 将 Skills 发布到 ClawHub

**阶段成果：** 完整可用的 OfferPilot 系统 + Demo 视频 + 开源 README + ClawHub 上的 Skills。

**面试可讲的技术点：** 安全预算与防重放、Action Replay 审计设计、Agent 评测体系设计（稳定性/一致性/安全性指标）、Docker Compose 工程化交付。

---

## 六、风险清单与应对

| 风险 | 概率 | 影响 | 应对方案 |
|------|------|------|---------|
| BOSS 直聘反爬封号 | 中 | 高 | 用限速 + 自己的登录态 + 配额管理；最坏情况退回手动粘贴 JD |
| BOSS 自动回复发错话 | 高 | 高 | MVP 先做预览模式（生成不发送）；正式模式有 confidence 阈值 + 回复上限 + 白名单三层守卫 |
| BOSS DOM 结构变更导致选择器失效 | 中 | 中 | 选择器封装为配置项 + fallback 策略 + 启动时校验关键选择器可用性 |
| 意图识别准确率不够 | 中 | 中 | confidence < 0.7 一律不自动回复；收集 HR 消息样本持续优化 Prompt |
| OpenClaw 在 Windows/WSL2 装不上 | 低 | 中 | 社区已有大量 WSL2 教程，32GB 内存无压力；实在不行用原生 PowerShell |
| OpenClaw 更新太快导致 Skill 不兼容 | 中 | 低 | 锁定一个稳定版本，不追最新 beta |
| LLM API 生成结果不稳定 | 中 | 中 | 结构化 Prompt + JSON Schema 约束输出格式 + 重试机制 |
| 面试官质疑"自动沟通"的合规性 | 中 | 高 | 强调"求职辅助工具 + 预览模式 + 多层安全守卫"定位，Agent 有明确能力边界 |
| 时间不够做完全部阶段 | 中 | 中 | **阶段 0+1+3 是最小可行版本**，加上阶段 4 是完整差异化能力；阶段 2/5/6 是增量 |

---

## 七、工程策略：错误处理与交互设计

### 7.1 LLM 调用的错误处理策略（贯穿所有阶段）

这一节不属于某个特定阶段，而是所有涉及 LLM 调用的节点都要遵循的工程规范。面试时这也是被拷打的高频点。

### 结构化输出校验

```python
# 每个 LLM 输出节点都用 Pydantic 做 Schema 校验
# LangChain 的 with_structured_output() 原生支持
llm_with_schema = llm.with_structured_output(JDAnalysis)

# 如果 LLM 输出不符合 Schema（字段缺失、类型错误），
# LangChain 会自动重试并把校验错误信息反馈给 LLM
```

### 重试策略

| 场景 | 重试次数 | 策略 | 降级方案 |
|------|---------|------|---------|
| LLM API 超时 / 网络错误 | 3 次 | 指数退避（2s → 4s → 8s） | 切换到备用模型（Qwen3-Max → Qwen-Plus，必要时切换 DeepSeek） |
| 结构化输出校验失败 | 2 次 | 把校验错误信息拼入 Prompt 让 LLM 修正 | 返回原始文本 + 手动提示 |
| BOSS 直聘反爬触发 | 1 次 | 等待 30 秒后重试 | 降级为手动粘贴模式 |
| Playwright 操作超时 | 2 次 | 刷新页面重试 | 截图记录失败现场，跳过当前操作 |

### 部分失败的处理

批量操作中（如批量解析 10 个 JD），单个失败不应阻断整体流程：

```
批量解析 10 个 JD：
  JD-1 ✅ → JD-2 ✅ → JD-3 ❌（LLM 超时）→ 标记 failed，继续
  → JD-4 ✅ → ... → JD-10 ✅
  → 结果：9/10 成功，1 个标记为 pending_retry
  → 下次 Heartbeat 自动重试 failed 的 JD
```

### LangGraph 状态持久化

```python
# 使用 PostgresSaver 作为 checkpointer
# 工作流在任何节点中断（包括服务器重启）都能恢复
from langgraph.checkpoint.postgres import PostgresSaver

checkpointer = PostgresSaver.from_conn_string(DATABASE_URL)
graph = workflow.compile(checkpointer=checkpointer)
```

这意味着：
- HumanReview 节点暂停后，即使 FastAPI 重启，工作流状态不丢失
- 用户可以隔天再审批，工作流从暂停点恢复
- 面试时这是"状态持久化"的加分点

---

### 7.2 OpenClaw 路径的 Human-in-the-Loop 交互设计

通过飞书/微信使用 OpenClaw 时，审批流程的多轮交互需要专门设计：

### 简单路径（阶段 1 的 JD 分析）：单轮，无需 HITL

```
用户 → 飞书发送 JD 文本
  → OpenClaw Skill `job-monitor` → HTTP POST /api/jd/analyze
  → FastAPI 内 LangGraph 跑完 Parser → Matcher → Gap
  → 返回结果 JSON → OpenClaw 格式化为消息
  → 飞书回复分析结果（单轮结束）
```

### 复杂路径（阶段 2 的材料生成 + 审批）：多轮

```
用户 → 飞书发送"帮我针对这个JD改简历"
  → OpenClaw Skill `resume-tailor`
  → HTTP POST /api/material/generate {jd_id, thread_id}
  → LangGraph 跑 Parser → Matcher → Generator → 到 HumanReview 暂停
  → FastAPI 返回 {status: "pending_approval", thread_id, preview: "..."}
  → OpenClaw 回复飞书：
    "已生成以下材料，请审批：
     [简历 bullet 预览...]
     回复「确认」采用 / 回复「重来」重新生成"

用户 → 飞书回复"确认"
  → OpenClaw Brain 识别为同一会话的审批回复（上下文记忆）
  → Skill 调用 HTTP POST /api/material/approve {thread_id, decision: "approve"}
  → LangGraph 从 HumanReview 节点恢复 → Finalize → 导出
  → FastAPI 返回 {status: "completed", download_url: "..."}
  → OpenClaw 回复飞书："材料已生成，下载链接：..."
```

**关键实现点：**
- `thread_id` 是 LangGraph 的 checkpoint 标识，贯穿整个工作流
- OpenClaw 的 Brain 自带上下文记忆，能理解"确认"是对上一条消息的回应
- Skill 需要维护一个简单的 `pending_threads` 映射，记录哪些 thread 在等待审批

**MVP 简化策略：**
- 阶段 0–2 的 OpenClaw 路径只做**单轮交互**（JD 分析、匹配评分）
- **多轮审批** 走 Web 前端（更直观，支持 Diff 对比展示）
- 阶段 3+ 再逐步给 OpenClaw 路径加审批能力
- 这样 OpenClaw Skills 的开发复杂度降到最低，同时两个入口各有分工

---

## 八、最小可行版本（如果时间紧）

如果你时间紧张，**阶段 0 + 1 + 3（扫描+打招呼）做完就已经有核心价值了**：

- OpenClaw 装好了，能通过飞书聊天
- **LangGraph 工作流跑通**（JD 解析 → 匹配评分 → 自动打招呼）
- BOSS 直聘自动搜索 + 匹配筛选 + 对达标岗位自动发送打招呼消息
- **MCP 工具层建立**（job-db + browser-use MCP Server 在用）
- 前端有一个基础看板

**进一步加上阶段 4（消息监听+智能回复），就是完整的 BOSS 沟通 Agent：**

- HR 回复后自动识别意图 → 发简历 / 回答常规问题 / 通知用户
- 求职画像驱动的可配置回复 + 多层安全守卫
- 这才是项目最核心的差异化能力

这已经足够在简历里写"基于 **OpenClaw + LangGraph** 开发了求职沟通 Agent，实现 **BOSS 直聘自动搜索→打招呼→对话管理** 闭环，用 **MCP 协议**标准化工具层，**LLM 意图识别 + 多层安全守卫** 保障自动回复可控性"，面试时也能 live demo。

后续的邮件秘书（阶段 5）、情报和审计（阶段 6）可以边找工作边迭代，每加一个模块简历就更丰满一层。

---

## 九、开发顺序优先级总结

```
必做（决定能不能讲故事）：
  阶段 0  环境搭建            → OpenClaw + LangGraph + MCP 能跑
  阶段 1  JD 理解 + 匹配      → LangGraph 状态图 + RAG 匹配 + Structured Output
  阶段 3  BOSS 搜索+打招呼    → Browser Use + MCP + Playwright + 配额管理

高价值（决定故事有多好，核心差异化）：
  阶段 4  BOSS 消息监听+回复  → 意图识别 + 求职画像 + 多层安全守卫（最核心能力）
  阶段 5  邮件秘书+Heartbeat  → IMAP + 日程提取 + Always-On 自治调度

锦上添花（决定深度印象）：
  阶段 2  材料生成辅助        → 条件路由 + Human-in-the-Loop（保留为工具能力）
  阶段 6  情报+审计+打磨      → 安全预算 + Agent 评测 + 工程化交付
```

> **说明：阶段 2（材料生成）的定位调整**
> 原方案中阶段 2 的"简历改写"作为核心 Agent 能力有需求偏差——简历是求职者精心打磨的核心竞争力，不适合全权交给 Agent。现将其降级为"辅助工具"：保留现有的材料生成/审批功能代码，但不再作为 Agent 核心闭环的一部分。面试时可以讲"我们评估后发现简历改写不适合自动化，体现了对 Agent 能力边界的认知"——这反而是加分项。

---

## 十、可持续开发执行计划（夜间冲刺版）

> 目标：在你休息期间保持“可连续推进 + 可回归验证 + 可随时接手”的节奏，直到方案完整落地。

### 10.1 执行原则（先计划，后落地）

- 每一轮开发都遵循：**计划 -> 实现 -> 自动回归 -> 文档对齐 -> 进入下一轮**
- 每轮必须通过最小质量门：
  - 后端：`python -m compileall backend/app`
  - 前端：`npx tsc --noEmit`
  - 接口：`/health`、`/api/jd/analyze`、`/api/material/*` 烟测
- 不做“大而全一次性重构”，采用小步快跑，保证随时可演示

### 10.2 当前已达成里程碑（截至今晚）

- 阶段 1：JD 分析 + RAG + 相似岗位检索完成，且已加入相似度阈值与去重
- 阶段 2：材料生成审批流完成 MVP，并升级为 **LangGraph interrupt + Postgres checkpoint**（可恢复）
- 阶段 2 收尾：审批通过后导出 PDF/TXT、复制话术、`resume-tailor` Skill 首版完成
- 阶段 3 起步：BOSS 扫描接口 `/api/boss/scan` 已落地（单关键词、截图、结果入库）
- 阶段 3 增强：BOSS 多页扫描（`max_pages`）与详情页文本提取已落地（可配置）
- 前端已支持：
  - 评分依据片段展示（`resume_evidence`）
  - 材料审批 `approve/reject/regenerate`
  - Regenerate 前后简版 Diff + 原始简历行级 Diff
- 阶段 4 起步：新增 `/api/form/autofill/preview` + `/api/form/autofill/preview-url` + `/api/form/autofill/fill-url`，支持 HTML 预览、URL 抓取预览与“不提交”的自动填充执行
- 阶段 4 进阶：新增 `/api/form/fill/start` + `/api/form/fill/review` HITL 审批流（LangGraph interrupt + Postgres 持久化）
- OpenClaw Skill 增量：`application-tracker` 首版，已可触发表单填充审批流
- 阶段 5 起步：新增 `/api/email/ingest` + `/api/email/recent`（邮件分类 + 岗位状态同步 + 事件持久化）
- 阶段 5 起步：新增 `/api/email/fetch`（IMAP 未读邮件拉取入口，依赖 IMAP 环境变量）
- 阶段 5 进阶：新增 `/api/email/heartbeat/*`（定时巡检调度 + 手动触发 + 状态观测）
- OpenClaw Skill 增量：`email-reader` 首版，已可触发邮件分类与状态同步
- 阶段 5 收口：新增 `scripts/openclaw_heartbeat_setup.sh`，可脚本化配置 OpenClaw cron，联动 `email-reader` 与后端 `/api/email/heartbeat/trigger`
- 阶段 6 起步：新增 `/api/actions/timeline` + `/api/eval/metrics`，打通审计时间线与评测指标基础面板数据
- 阶段 6 起步：新增 `/api/company/intel` + `/api/interview/prep` + `/api/security/*`（令牌与预算），并接入前端看板与 Smoke Check
- 阶段 6 起步：新增 `backend/mcp_servers/web_search_server.py`（`search` / `scrape_page`）
- 阶段 6 收口：新增 `backend/demo_walkthrough.py` 一键演示脚本
- 阶段 6 收口：`README` 与部署手册补充 fullstack compose 与演示命令
- 阶段 6 收口：新增 `Demo演示脚本.md`（8 分钟面试演示话术 + 故障兜底）
- 阶段 6 收口：新增 `Demo演示脚本-3分钟版.md`（高压面试场景极速版本）
- 阶段 6 收口：新增 `Demo录制检查清单.md` + `scripts/demo_record_prep.sh`（录制前一键预检）
- OpenClaw 工程化：新增 `scripts/sync_skills_to_openclaw_workspace.sh` + `scripts/skills_release_prep.sh` + `scripts/clawhub_sync.sh`，解决技能同步与发布前检查
- ClawHub 发布进展：6 个技能已全部发布/更新完成（`job-monitor`、`resume-tailor`、`application-tracker`、`email-reader`、`company-intel`、`interview-prep`）。冲突 slug 已通过前缀回退策略收敛

### 10.3 下一轮连续推进顺序（严格按优先级）

1. **阶段 2 收尾（高优先）**
   - [x] 审批通过后的“导出定制简历 PDF/可复制话术”能力
   - [x] `resume-tailor` OpenClaw Skill 打通（首版）
   - [x] 前端 Diff 升级为“原文 vs 改写”更完整对比（不仅 bullets）

2. **阶段 3 MVP（第二优先）**
   - [x] `browser-use` MCP Server 最小实现（navigate/click/extract/screenshot/wait）
   - [x] `BossScanner` 子图首版（单关键词、单页抓取）
   - [x] `BossScanner` 增强版（`max_pages` 翻页抓取 + 详情页提取）
   - [x] 限速与截图审计落库（`actions`）

3. **阶段 4–6 按价值递增推进**
   - [x] 网申表单自动填充 + HITL 复用（首版）
   - [x] 阶段 4 起步：Autofill Preview API（HTML 字段识别 + profile 映射）
   - [x] 阶段 4 起步：Autofill URL Preview（Playwright 抓取真实页面表单 + 截图）
   - [x] 阶段 4 起步：Autofill Fill URL（`confirm_fill` 守卫 + 仅填充不提交）
   - [x] 阶段 5 起步：邮件手动 ingest 分类 + 状态同步首版（`/api/email/*`）
   - [x] 阶段 5 起步：IMAP 拉取入口首版（`/api/email/fetch`）
   - [x] 邮件分类与投递状态自动更新首版（IMAP 拉取 + 后端定时巡检）
   - [x] 通道通知首版：heartbeat 结果支持 webhook 推送（飞书机器人可接入）
   - [x] OpenClaw Heartbeat 与邮件巡检联动（通道消息提醒）
   - [ ] 情报、评测、安全治理与 Demo 打磨（已完成阶段 6 起步能力，继续做 Demo 收口）
   - [x] 全 WSL 原生架构迁移（Docker 清理 + 一键启动 + boss-login）
   - [x] **Agent 可观测性架构升级**（SSE 事件流 + screenshot_path 补全 + LangSmith + 前端监控面板 + OpenClaw 日志配置）
   - [x] ClawHub 发布收口：已完成（`interview-prep` 已发布为 `wanghong5233-offerpilot-interview-prep`）

### 10.4 每轮完成定义（DoD）

- 新功能有可触发入口（API 或前端）
- 至少 1 条端到端验证路径打通
- `README` 与本实施方案同步更新（防止“代码已变，文档过期”）
- 不破坏已有能力（阶段 1/2 回归通过）

### 10.5 明早可见的“惊喜”标准

- 看板可直接演示：`分析 -> 证据 -> 生成 -> 审批 -> 导出`
- OpenClaw 至少新增 1 个可用 Skill（`resume-tailor` 或 `job-monitor` 增强）
- 阶段 3 至少完成可运行最小链路（即使只抓单页，也要可演示）

---

## 十一、面试关键词覆盖自查

确保你的项目能覆盖招聘 JD 中的高频关键词：

| JD 高频关键词 | OfferPilot 中的对应实现 | 在哪个阶段 |
|-------------|----------------------|-----------|
| **Agent 框架（LangChain/LangGraph）** | LangGraph 状态机编排所有工作流（搜索流 + 对话流 + 邮件流） | 阶段 1–5 |
| **Prompt Engineering** | JD 解析、HR 意图识别、邮件分类、画像回复的结构化 Prompt | 阶段 1–5 |
| **RAG** | 简历段落精准检索 + JD 历史相似度检索（ChromaDB 双 Collection） | 阶段 1 |
| **Tool Calling / Function Call** | LangGraph ToolNode 调用 MCP Server 暴露的工具 | 阶段 1–5 |
| **MCP 协议** | 5 个自研 MCP Server（db/file/browser/email/web-search） | 阶段 1–6 递增 |
| **Browser Use / Computer Use** | Playwright 自动操作 BOSS（搜索+打招呼+消息读写） | 阶段 3–4 |
| **多 Agent / Multi-Agent** | Scanner-ChatCopilot-EmailTracker 三工作流（LangGraph 子图） | 阶段 3–5 |
| **Human-in-the-Loop** | 预览模式 + 安全守卫升级 + LangGraph interrupt_before 审批 | 阶段 2、4 |
| **意图识别 / NLU** | HR 消息意图分类（Structured Output + confidence 阈值） | 阶段 4 |
| **可配置策略 / 安全守卫** | profile.yaml 驱动回复 + 白名单 + 回复上限 + confidence 门槛 | 阶段 4 |
| **Agent 评测** | 意图分类准确率/回复通过率/端到端延迟/一致性四维指标 | 阶段 6 |
| **安全/审计** | 预算管理 + 审批令牌 + 预览模式 + Action Timeline 回放 | 阶段 4、6 |
| **OpenClaw** | Agent Runtime + 7 个 Skills + ClawHub 发布 | 全阶段 |
| **长期记忆 / 状态管理** | ChromaDB 向量知识库 + LangGraph PostgresSaver 状态持久化 + 求职画像 | 阶段 1、4、5 |
| **Heartbeat / 自治调度** | OpenClaw Heartbeat 定时触发搜索 + 消息轮询 + 邮件巡检 | 阶段 3–5 |
| **Structured Output** | Pydantic Schema 约束 LLM 输出 + 校验重试 | 阶段 1–5 |
| **状态持久化 / Checkpoint** | LangGraph PostgresSaver，工作流中断可恢复 | 阶段 2+ |
| **全栈** | FastAPI + Next.js + PostgreSQL + WSL 原生部署 | 全阶段 |
| **Agent 可观测性** | SSE 实时事件流 + 截图审计 + LangSmith 追踪 + OpenClaw OTEL | 阶段 7（新增） |

---

## 十二、Agent 可观测性与审计追踪架构（调研成果 + 实施计划）

> 2026-03-16 调研结论。解决核心问题：**"Agent 在操作我的账号，我必须知道它在做什么。"**
>
> 这是面试中最可能被追问的安全设计点之一。

### 12.1 调研背景与问题定义

**开发阶段需求：** Agent 通过 Playwright 操控 BOSS 直聘账号，开发者必须：
1. 实时看到浏览器操作（已通过 `BOSS_HEADLESS=false` 解决）
2. 实时看到 Agent 每一步决策过程（意图识别→安全守卫→执行/拦截）
3. 出问题时能回溯完整操作链路

**生产阶段需求：** Agent 在无人值守模式下自动运行，必须：
1. 全量审计日志（谁、何时、做了什么、结果如何）
2. 截图与 action 记录结构化关联（可按 action 查截图）
3. 异常告警 + 行为指标监控
4. 合规留存（可导出、可追溯）

### 12.2 业界调研结论

#### OpenClaw 社区最佳实践

| 方案 | 说明 | 适用场景 |
|------|------|---------|
| `openclaw logs --follow --json` | CLI 实时 JSONL 日志，含 session_id / action_type / timestamps | 开发调试 |
| `diagnostics-otel` 插件 | OpenTelemetry 导出 traces / metrics / logs 到 Grafana / Jaeger / SigNoz | 生产监控 |
| `openclaw.json` logging 配置 | `level: debug`、`consoleStyle: json`、`redactSensitive: tools` | 日志精细控制 |
| SOC 2 审计要求 | 365 天 append-only 留存、加密存储、角色权限、可搜索日志 | 企业合规 |

**关键结论：** OpenClaw 本身的 Skill 只做薄桥接，真正的 Agent 行为发生在后端（FastAPI + LangGraph），因此**审计重心在后端**，OpenClaw 端做辅助追踪。

#### LangGraph / LangChain 追踪方案

| 方案 | 侵入性 | 能力 | 成本 |
|------|--------|------|------|
| **LangSmith** | 零代码（设环境变量即可） | 节点级追踪、token 统计、延迟分析、trace 可视化 | 免费额度 5k traces/月 |
| **Langfuse（开源）** | 低（callback handler） | 自托管、节点追踪、成本分析 | 免费（自托管） |
| **OpenTelemetry** | 中 | 与现有监控打通（Grafana/Jaeger） | 免费 |

**关键结论：** LangSmith 的零侵入接入（`LANGSMITH_TRACING=true`）是最快收益方案，可立即获得所有 LangGraph 节点的输入/输出/耗时追踪。

### 12.3 当前实现差距分析

| 维度 | 当前状态 | 目标状态 | 差距 |
|------|---------|---------|------|
| **Action Logging** | `log_action()` 写入 `actions` 表 | 完善 | `screenshot_path` 列存在但**从未写入**（永远 NULL） |
| **Screenshot Audit** | boss_scan / boss_chat_pull 截图到本地 | 截图与 action 关联 | 截图文件与 actions 记录无结构化关联 |
| **Real-time Events** | 无 | SSE 实时推送 Agent 步骤 | **完全缺失** |
| **LangGraph Tracing** | 无 | LangSmith 节点级追踪 | 未配置 |
| **OpenClaw Tracing** | 默认日志 | 结构化 JSON + OTEL | 未配置 |
| **前端审计 UI** | 表格展示 action_type/status | 截图预览+详情展开+筛选 | 功能不完整 |

### 12.4 实施计划（按优先级）

#### 阶段 A：开发调试可观测性（已完成 ✅ 2026-03-14）

**A1. 后端 SSE 实时事件流**

新增 `GET /api/agent/events` SSE 端点，Agent 每个关键动作实时推送：

```python
# 事件类型定义
class AgentEvent(BaseModel):
    timestamp: str
    event_type: str  # "browser_navigate" | "browser_click" | "screenshot" |
                     # "llm_call" | "llm_response" | "intent_classified" |
                     # "safety_check" | "reply_sent" | "action_logged"
    detail: str      # 人类可读描述
    metadata: dict   # 结构化数据（URL、selector、confidence 等）
```

实现方式：
- 后端维护一个 `asyncio.Queue`，Agent 操作中每个步骤 push 事件
- `boss_scan.py` 中的 `page.goto()` / `page.click()` / `page.screenshot()` 前后各 push 一个事件
- `boss_chat_service.py` 中意图识别结果、安全守卫判定、回复生成均 push 事件
- SSE 端点消费 Queue 并 yield 给前端

**A2. 前端 Agent 实时监控面板**

新增"Agent 活动"标签页：
- `EventSource` 订阅 `/api/agent/events`
- 实时滚动显示 Agent 当前操作（带时间戳和颜色区分）
- 浏览器操作显示为蓝色、LLM 调用显示为绿色、安全拦截显示为红色
- 截图事件可内联预览

**A3. `log_action()` 补全 `screenshot_path`**

修改 `storage.py` 的 `log_action()` 函数：
- 新增 `screenshot_path` 参数
- INSERT 语句写入 `screenshot_path` 列
- 所有截图调用处传入路径

**A4. BOSS 操作步骤级日志**

在 `boss_scan.py` 的每个关键操作点增加结构化日志 + SSE 事件推送：
- `[BOSS] Navigating to chat page...`
- `[BOSS] Extracting 15 conversations (3 unread)...`
- `[BOSS] Reading HR message from 字节跳动-张经理...`
- `[BOSS] Intent classified: ask_salary (confidence=0.92)`
- `[BOSS] Safety check PASSED, generating reply...`
- `[BOSS] Screenshot saved: boss_chat_pull_20260316_143022.png`

#### 阶段 B：LangGraph 追踪集成（零代码改动）

在 `.env` 中添加：

```
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_PROJECT=OfferPilot
```

即可在 LangSmith 平台上看到：
- 每个 LangGraph 工作流的完整执行轨迹
- 每个节点的输入/输出/耗时
- LLM 调用的 token 消耗和成本
- 失败节点的错误堆栈

> 面试价值：展示 LangSmith trace 截图，说明"我们在生产中监控每个 Agent 节点的执行状况，包括延迟分析和异常追踪"。

#### 阶段 C：OpenClaw 可观测性配置

配置 `~/.openclaw/openclaw.json`：

```json
{
  "logging": {
    "level": "debug",
    "consoleStyle": "json",
    "redactSensitive": "tools"
  }
}
```

进阶（可选）：启用 `diagnostics-otel` 插件导出到 Grafana。

#### 阶段 D：生产级审计增强（后续迭代）

| 改进 | 具体内容 |
|------|---------|
| `actions` 表增强 | 新增 `session_id`、`duration_ms`、`token_usage`、`metadata_json` 列 |
| 审计时间线 UI 增强 | 截图缩略图预览、input/output 展开、action_type 筛选、时间范围过滤 |
| 审计日志导出 | CSV / JSON 导出，用于合规留存和面试展示 |
| 异常告警 | 连续 N 次安全守卫拦截时自动通知 |

### 12.5 目标架构图

```
┌──────────────────────────────────────────────────────────────────┐
│                    Agent 可观测性架构                              │
│                                                                   │
│  ┌──────────────┐                    ┌───────────────────────┐   │
│  │ Chromium      │  BOSS_HEADLESS    │ Frontend              │   │
│  │ (可视化浏览器) │  = false          │ ┌───────────────────┐ │   │
│  │ 开发者直接     │                   │ │ Agent 实时监控面板 │ │   │
│  │ 观察操作      │                   │ │ (SSE EventSource) │ │   │
│  └──────────────┘                   │ ├───────────────────┤ │   │
│                                      │ │ 审计时间线(增强)  │ │   │
│                                      │ │ (截图+筛选+详情) │ │   │
│                                      │ └────────┬──────────┘ │   │
│                                      └──────────┼────────────┘   │
│                                                  │ SSE            │
│  ┌───────────────────────────────────────────────┴────────────┐  │
│  │                FastAPI + LangGraph                          │  │
│  │                                                             │  │
│  │  每个 Agent 动作同时触发:                                    │  │
│  │   ① logger.info("[BOSS] ...")     → 终端日志（实时可见）     │  │
│  │   ② event_queue.put(AgentEvent)   → SSE 推送前端实时面板     │  │
│  │   ③ log_action(screenshot_path=)  → PostgreSQL actions 表   │  │
│  │   ④ page.screenshot()             → 本地文件 + actions 关联 │  │
│  │   ⑤ LangSmith auto-trace          → 节点级追踪（可选）      │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  OpenClaw Gateway                                          │  │
│  │  · openclaw logs --follow --json （结构化实时日志）          │  │
│  │  · diagnostics-otel → Grafana/Jaeger （可选）               │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  PostgreSQL: actions 表 (审计日志持久化)                     │  │
│  │  · id, job_id, action_type, input_summary, output_summary  │  │
│  │  · screenshot_path（已补全关联）                             │  │
│  │  · status, created_at                                       │  │
│  │  · 后续: session_id, duration_ms, token_usage, metadata     │  │
│  └────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

### 12.6 面试回答模板

**面试官："你怎么保证 Agent 不会乱操作你的 BOSS 账号？"**

> "四个层面。第一，安全守卫层：LLM confidence < 0.7 一律不自动回复、同一 HR 回复次数上限、话题白名单外自动升级通知用户。第二，预览模式：默认只生成不发送，需要 `auto_execute` 环境变量和请求参数双开关同时打开。第三，实时可观测：开发阶段浏览器可见 + SSE 实时事件流推送到前端监控面板，生产阶段全量审计日志 + 截图关联。第四，可追溯：所有 Agent 行为写入 PostgreSQL actions 表，LangSmith 提供节点级追踪，支持 Action Timeline 回放和审计导出。"

**面试官："出了问题怎么排查？"**

> "三条路径并行。第一，SSE 实时事件流可以看到 Agent 正在做什么（导航到哪个页面、识别了什么意图、安全守卫是否通过）。第二，LangSmith trace 可以看到每个 LangGraph 节点的输入输出和耗时，精确定位哪个节点出了问题。第三，actions 表 + 截图审计可以事后回溯完整操作链路。"
