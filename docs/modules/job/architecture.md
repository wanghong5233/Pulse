# Job 领域架构设计

> 求职自动化（BOSS 直聘 / 猎聘 / 智联 / 前程无忧 …）在 Pulse 业务层的落地规范。
> 位置：`src/pulse/modules/job/`。配套阅读：`Pulse-内核架构总览.md`、`Pulse-MemoryRuntime设计.md`、`Pulse-DomainMemory与Tool模式.md`。

---

## 1. 设计目标

1. **多平台可插拔**：新增一个招聘平台 = 新增 `_connectors/<platform>/`，业务代码零改动。
2. **子能力高内聚**：`greet` / `chat` / `profile` 各自独立，通过领域共享层交换数据，互不侵入。
3. **业务与平台解耦**：业务层（service）**不知道** DOM、URL、Cookie、HTTP 细节；connector **不知道** 用户偏好、黑名单、每日配额。
4. **两种触发对等**：定时 patrol 与消息命令走**同一条 service 入口**；module 层只做 Controller。
5. **零 `os.getenv` 在业务层**：所有配置走 `Settings`，依赖走构造注入。
6. **领域记忆 LLM-first**：Brain 负责把用户自然语言写入 `JobMemory`；下游匹配/文案生成读 `JobMemorySnapshot` 过 LLM，不走预设枚举的静态规则。

---

## 2. 目录结构

```
modules/job/
├── __init__.py                [导出 SKILL_SCHEMA]
├── skill.py                   [领域级 schema（供 Brain 两级路由）]
├── config.py                  [JobSettings: 业务策略，PULSE_JOB_*]
│
├── memory.py                  [JobMemory / JobMemorySnapshot — 三类存储 facade]
│
├── shared/                    [领域内共享原语]
│   ├── enums.py               [ChatAction / CardType / CardAction / ConversationInitiator / PlatformProvider]
│   └── models.py              [跨子能力共用的 Pydantic DTO]
│
├── _connectors/               [平台 IO 驱动 — 领域内部实现]
│   ├── base.py                [JobPlatformConnector 抽象契约]
│   ├── registry.py            [多平台注册表 + 默认平台选择]
│   └── boss/
│       ├── settings.py        [BossConnectorSettings: PULSE_BOSS_*]
│       └── connector.py
│   └── liepin/  (预留)
│
├── greet/                     [子能力：扫岗 + 打招呼]
│   ├── module.py              [Controller / IntentSpec 注册]
│   ├── service.py             [JobGreetService + GreetPolicy]
│   ├── matcher.py             [JobSnapshotMatcher — LLM 打分组件]
│   ├── greeter.py             [LLM 文案组件]
│   └── repository.py          [actions 表 + 每日配额]
│
├── chat/                      [子能力：HR 对话处理]
│   ├── module.py              [Controller]
│   ├── service.py             [JobChatService + ChatPolicy]
│   ├── planner.py             [HrMessagePlanner — 分类/路由]
│   ├── replier.py             [LLM 回复组件]
│   └── repository.py          [boss_chat_events 表]
│
└── profile/                   [子能力：用户求职画像管理]
    ├── module.py              [Controller + IntentSpec]
    ├── schema.py              [JobProfileSchema — yaml 结构契约]
    └── manager.py             [JobProfileManager — yaml/md ↔ memory 同步]
```

---

## 3. 分层职责

### 3.1 Module（Controller）

- **做**：HTTP 路由注册、IntentSpec 声明、参数校验（Pydantic）、生命周期钩子（`on_startup` 注册 patrol）。
- **不做**：业务逻辑、DB 访问、平台通信。

### 3.2 Service（Business Orchestration）

- **做**：业务编排 — 调 connector 取数据、读 `JobMemorySnapshot` 过滤/匹配、调 repository 落地、触发通知。
- **不碰**：DOM、URL、Cookie（connector 的活）；`os.getenv`（Settings 注入）。

### 3.3 Repository（Data Access）

- **做**：对领域专属表（`actions` / `boss_chat_events`）的 CRUD，返回领域对象。
- **不做**：LLM 调用、`workspace_facts` 的直接读写（那是 `JobMemory` 的职责）。

### 3.4 Connector（Platform IO）

- **做**：和外部平台通信（OpenAPI / MCP / 浏览器），返回已清洗的标准化领域对象。
- **不做**：任何业务规则（是否打招呼、是否命中黑名单、是否合适投递）。
- **必须实现** `JobPlatformConnector` 契约（见 §5）。

### 3.5 领域 LLM 组件（matcher / greeter / replier / planner）

- **做**：把 `JobMemorySnapshot` + 当前上下文渲染进 Prompt，让 LLM 做**语义决策**（匹配分 / 打招呼话术 / 回复草稿 / 消息分类）。
- **共同契约**：
  - 输入统一包含 `JobMemorySnapshot`（保证「用户偏好对所有 LLM 组件可见」）。
  - 必须有 heuristic fallback，用于 LLM 不可用 / 超时 / JSON 解析失败时的降级。
  - 不直接写 `JobMemory`；对记忆的任何 mutation 都要回到 Brain 的 IntentSpec 路径。

### 3.6 JobMemory（领域记忆 facade）

- **做**：在 `WorkspaceMemory` 之上封装 `job.*` 命名空间，提供三类存储（见 §6）的 CRUD 与 snapshot 渲染。
- **不持有存储**，所有 IO 委托 `WorkspaceMemory`。
- 线程模型：每 task run 构造一次即可。

---

## 4. 多平台扩展点

**契约层**：`_connectors/base.py` 的 `JobPlatformConnector` 抽象类。

**新增平台（例：猎聘）**：

1. 创建目录 `_connectors/liepin/`。
2. 实现 `LiepinPlatformConnector(JobPlatformConnector)`。
3. 在 `_connectors/registry.py` 注册工厂。
4. 新增 `LiepinConnectorSettings(BaseSettings, env_prefix="PULSE_LIEPIN_")`。
5. 业务层零改动（matcher / greeter / service 都通过注册表拿 connector）。

**多平台并行**：`PlatformRegistry.list_enabled()` 返回开启的平台集合；service 对每个平台分别扫描、分别配额。

---

## 5. JobPlatformConnector 契约

```python
class JobPlatformConnector(ABC):
    # identity
    provider_name: str
    execution_ready: bool
    def health(self) -> dict: ...
    def check_login(self) -> dict: ...

    # scan
    def scan_jobs(*, keyword, max_items, max_pages, job_type) -> dict: ...
    def fetch_job_detail(*, job_id, source_url) -> dict: ...

    # greet
    def greet_job(*, job, greeting_text, run_id) -> dict: ...
    def initiate_conversation(*, job_id, company, hr_hint) -> dict: ...

    # conversations
    def pull_conversations(*, max_conversations, unread_only,
                            fetch_latest_hr, chat_tab) -> dict: ...
    def reply_conversation(*, conversation_id, reply_text,
                            profile_id, conversation_hint) -> dict: ...
    def send_resume_attachment(*, conversation_id,
                                resume_profile_id,
                                conversation_hint) -> dict: ...
    def click_conversation_card(*, conversation_id,
                                 card_id, card_type, action) -> dict: ...
    def mark_processed(*, conversation_id, run_id, note) -> dict: ...
```

**未实现能力规范**：返回 `{ok: False, status: "not_implemented", error: "...", source: provider_name}`；**严禁静默降级为文本回复**（例如「用文字假装发简历」）。

> 注意：`send_resume_attachment` 的 `resume_profile_id` **只是 BOSS 后台的附件模板 id**，不是 Pulse 侧的简历数据。Pulse 侧的简历内容由 `JobMemory` 的 Domain Documents（§6.3）承载，用于文案生成与匹配判定。

---

## 6. JobMemory：三类存储

`JobMemory` 遵循 `Pulse-DomainMemory与Tool模式.md` §3.1 的三类存储契约，内部对 `workspace_facts` 的 `job.*` 命名空间做如下划分：

### 6.1 Hard Constraints（结构化硬约束）

**判定原则**：只有「Connector 能直接当过滤表达式用」的字段进这里。字段集**小且稳定**，禁止随业务想法膨胀。

| 字段 | 类型 | 消费者 |
|------|------|--------|
| `preferred_location` | `list[str]` | BOSS 搜索 URL 的 city 参数 |
| `salary_floor_monthly` | `int`（月薪 K） | BOSS 搜索 URL 的 salary 参数 |
| `target_roles` | `list[str]` | BOSS 搜索 URL 的 query 参数 |
| `experience_level` | `str` | BOSS 搜索 URL 的 experience 参数 |

**Key 命名**：

```
job.hc.preferred_location   -> value: ["杭州", "上海"]
job.hc.salary_floor_monthly -> value: 25
job.hc.target_roles         -> value: ["后端", "AI Engineer"]
job.hc.experience_level     -> value: "3-5年"
```

**不会进 Hard Constraints 的典型例子**：
- 「不喜欢加班多的公司」— 无法用搜索 URL 过滤，进 Memory Items。
- 「暂时不投字节」— 带时效 + 公司名，搜索 URL 也做不了公司黑名单，进 Memory Items。
- 「技能：Python / Rust」— 是简历内容的一部分，不是搜索 filter，进 Domain Documents（简历）。

### 6.2 Memory Items（语义记忆池）

**Schema 开放**的半结构化偏好/事件/软约束池。由 Brain 用 LLM 归一化后写入。

**Item 结构**：

```python
@dataclass
class MemoryItem:
    id: str                 # UUID
    type: str               # 推荐 enum + 允许 other
    target: str | None      # 公司/关键词/岗位/null
    content: str            # LLM 一句话归纳（给下游 Prompt 用）
    raw_text: str           # 用户原话
    valid_from: str         # ISO-8601 UTC
    valid_until: str | None # ISO-8601 UTC 或 null（永久）
    superseded_by: str | None  # 被新 item 取代时指向新 id
    created_at: str
```

**Type 推荐 enum**（LLM 可填枚举外字符串，兜底 `other`）：

| Type | 含义 | `target` 典型值 |
|------|------|-----------------|
| `avoid_company` | 回避公司 | 公司名 |
| `favor_company` | 偏好公司 | 公司名 |
| `avoid_trait` | 回避特质（岗位/公司层面） | "加班多" / "晋升不透明" / null |
| `favor_trait` | 偏好特质 | "远程" / "技术栈 Rust" / null |
| `application_event` | 投递/面试事件 | 公司名 |
| `capability_claim` | 能力自评（但简历是 ground truth） | null 或 技能名 |
| `constraint_note` | 其他约束说明 | 自由 |
| `other` | 任何未归类 | 自由 |

**Key 命名**：

```
job.item:<uuid>   -> value: MemoryItem 的 JSON
```

**演化规则**：
- **新增** → `record(item)`：分配 UUID，写入一条。
- **失效** → `retire(id)`：保留 item 但设置 `valid_until = now`（审计可查）。
- **更新** → `supersede(old_id, new_item)`：写入新 id + 把旧 id 的 `superseded_by` 指向新 id。
- 查询默认过滤 `valid_until < now OR superseded_by is not null` 的条目。

**举例：「目前我投递过字节，简历被锁，暂时不要投递字节的简历」**：

Brain 会产出 2 条 item：

```json
[
  {
    "type": "application_event",
    "target": "字节跳动",
    "content": "曾投递字节，结果为简历被锁（平台侧拒绝）",
    "raw_text": "目前我投递过字节，简历被锁，暂时不要投递字节的简历",
    "valid_until": null
  },
  {
    "type": "avoid_company",
    "target": "字节跳动",
    "content": "简历被锁期间暂停投递字节",
    "raw_text": "目前我投递过字节，简历被锁，暂时不要投递字节的简历",
    "valid_until": "<now + 30d>"
  }
]
```

### 6.3 Domain Documents（领域文档）—— 简历

**唯一当前文档**：`resume`。

**结构**：

```python
@dataclass
class JobResume:
    raw_text: str           # 用户粘贴 / PDF 解析后的完整简历文本
    parsed: ResumeParsed | None  # LLM 解析缓存
    raw_hash: str           # raw_text 的 sha256，parsed.raw_hash != 时重算
    updated_at: str

@dataclass
class ResumeParsed:
    summary: str
    years_exp: int | None
    skills: list[str]
    experiences: list[ExperienceEntry]  # {company, role, period, highlights}
    projects: list[ProjectEntry]        # {name, stack, highlights}
    education: list[EducationEntry]
    raw_hash: str
```

**Key 命名**：

```
job.doc:resume          -> value: {raw_text, raw_hash, updated_at}
job.doc:resume.parsed   -> value: ResumeParsed 的 JSON（异步刷新）
```

**更新流**：
- `job.resume.update(raw_text)` → 写入 `job.doc:resume`，**异步**触发 LLM 解析，解析完成后写 `job.doc:resume.parsed`；parsed 缺失或 `raw_hash` 不一致时，snapshot 仅渲染原文前 N 字 + 标注「解析中」。
- `job.resume.patch_parsed(patch)` → 允许用户手工纠错个别字段（例如补一条 project），仅打补丁不重跑解析。

**为什么简历是 Domain Documents 而不是 Memory Items**：

| 维度 | Memory Items | Domain Documents（简历） |
|------|--------------|--------------------------|
| 变化频率 | 高（每次对话可能产生多条） | 低（周/月级更新） |
| 内部结构 | 单条一句话 + 元数据 | 成篇文本，有标准小节结构 |
| 消费方式 | Prompt 里按 type 分组列出 | Prompt 里引用摘要 + LLM 按需展开 |
| 更新语义 | 追加 / 失效 / 取代 | 整体替换 + 解析缓存 |

### 6.4 JobMemorySnapshot 渲染

Snapshot 按三类分节，渲染给 matcher / greeter / replier / Brain 系统 prompt：

```
## Job Snapshot (workspace: <id>)

### Hard Constraints
- preferred_location: ["杭州", "上海"]
- salary_floor_monthly: 25 K
- target_roles: ["后端", "AI Engineer"]
- experience_level: "3-5年"

### Memory Items — avoid_company
- [字节跳动] 简历被锁期间暂停投递字节（valid until 2026-05-19）
- [拼多多] 笔试挂过，暂不考虑

### Memory Items — favor_trait
- 偏好远程或混合办公
- 喜欢技术栈含 Rust 或 Go

### Memory Items — application_event
- [字节跳动] 曾投递字节，结果为简历被锁（2026-04-19）

### Resume (summary)
3 年后端经验，Python / Go 主力，RAG 系统实战。主要项目：…
(完整简历调 job.resume.get 获取)
```

**`snapshot_version`** 由三类存储的最大 `updated_at` 合并而成，下游缓存键可直接以此做失效。

---

## 7. 写入路径：Brain → IntentSpec → JobMemory

### 7.1 Intent 清单

| Intent Name | mutates | 作用 |
|-------------|---------|------|
| `job.memory.record` | ✅ | 追加 1~N 条 MemoryItem（由 Brain 的 LLM 产出结构化 item） |
| `job.memory.retire` | ✅ | 将某条 item 设为已过期 |
| `job.memory.supersede` | ✅ | 用新 item 替换旧 item，建立链接 |
| `job.memory.list` | ❌ | 按 type / target / 时间窗查询（Brain 在不确定时用） |
| `job.hard_constraint.set` | ✅ | 设置 Hard Constraint 某字段 |
| `job.hard_constraint.unset` | ✅ | 取消 Hard Constraint 某字段 |
| `job.resume.update` | ✅ | 整体替换简历原文，触发异步解析 |
| `job.resume.patch_parsed` | ✅ | 对解析后的简历结构化字段打补丁 |
| `job.resume.get` | ❌ | 读取完整简历（给 LLM 在需要全文时按需调用） |
| `job.snapshot.get` | ❌ | 诊断 / 调试用 |

**注意**：不再暴露 `job.profile.block_company` / `job.profile.block_keyword` 等「按预设分类」的细粒度 intent——这些都由 `job.memory.record(item)` 统一承载，`type` 字段由 LLM 决定。

### 7.2 数据流

```
用户自然语言
    → Brain（ReAct：选 IntentSpec、LLM 填充参数）
    → job.memory.record / hard_constraint.set / resume.update
    → JobMemory facade
    → WorkspaceMemory（job.* key）
    → snapshot 刷新（mutates=true 触发）
```

### 7.3 HITL 门控（原则）

| 情形 | 倾向 |
|------|------|
| 用户已明确说出偏好（"不投字节"） | 一般无需确认；回执「已记录」 |
| 从弱上下文推断的写入（LLM 自行归纳的 claim） | **需确认**，尤其 `capability_claim` |
| 批量失效（一次 retire 5 条以上） | 需确认 |
| `resume.update`（整体替换） | 需确认（不可逆） |

---

## 8. 读取路径：下游 LLM 组件如何消费

| 组件 | 读什么 | 为什么过 LLM |
|------|-------|--------------|
| `JobSnapshotMatcher` | JD 文本 + 完整 snapshot | Hard Constraints 做粗筛后，LLM 读 Memory Items 判"软匹配"（偏好公司/特质/简历技能契合度） |
| `JobGreeter` | JD 文本 + 简历摘要 + avoid/favor memory items | 生成个性化开场白（引用简历真实项目），避开已屏蔽方向 |
| `HrMessagePlanner` | HR 消息 + snapshot | 判断 reply / send_resume / accept_card / escalate |
| `HrReplier` | HR 消息 + snapshot + 简历 | 生成符合用户偏好 + 不出简历范围的回复 |

**批量优化**：`JobSnapshotMatcher` 支持对一批候选岗位一次 LLM 调用（JD 列表 + 同一 snapshot），避免每岗位一次 LLM 的成本爆炸。

---

## 9. 配置分层

```
core/config.py :: Settings                     [内核 + 接入层]
modules/job/config.py :: JobSettings           [Job 业务策略, PULSE_JOB_*]
modules/job/_connectors/boss/settings.py :: BossConnectorSettings   [PULSE_BOSS_*]
```

三者各自独立从 `.env` 加载，互不依赖。新增业务域或平台**不得**修改 `core/config.py`。

---

## 10. Profile 持久化：yaml + md 双文件投影

`JobMemory` 的持久化真相在 `workspace_facts`。YAML / Markdown 文件是**人类可编辑的投影**，由 `JobProfileManager` 双向同步：

| 文件 | 内容 | 源 |
|------|------|-----|
| `config/profile/job.yaml` | Hard Constraints 全量 + Memory Items 的**摘要**（只列 count/最近 N 条）+ Resume 的结构化摘要字段 | memory → yaml 单向 auto-sync |
| `config/profile/resume.md` | Resume `raw_text` 的原文 | 双向：用户编辑后 `pulse profile load` 导入；`job.resume.update` 也会反写这里 |

**同步规则**：

- `memory → file`：每次 mutates 类 Intent 触发后，由 `ProfileCoordinator` 重写文件（原子替换）。
- `file → memory`：**仅** `pulse profile load` 显式触发（避免用户半途编辑造成不一致）。
- Memory Items 在 yaml 里**只投影摘要**，完整列表不进 yaml（量大且含 UUID，不适合人眼编辑）。用户要批量调整 Memory Items 必须通过对话（走 Brain → IntentSpec）。

`config/profile/job.example.yaml` 与 `config/profile/resume.example.md` 作为模板提交到仓库；`config/profile/job.yaml` 与 `config/profile/resume.md` 由 `.gitignore` 排除。

---

## 11. 代码质量铁律（与 `docs/code-review-checklist.md` 互补）

1. **业务层禁用 `os.getenv`**。所有配置通过对应的 `XxxSettings` 传入构造函数。
2. **内核 Settings 禁止承载业务字段**。每个业务域/平台拥有自己的 `BaseSettings` 子类。
3. **Module 禁碰 DB/DOM/URL**。Module 是 Controller，数据访问一律走 Service → Repository / Connector。
4. **Connector 禁碰业务规则**。Connector 只管 IO，返回标准化领域对象。
5. **不允许「假实现」**。`send_resume` 实际只发文字 = 业务谎言，必须 `not_implemented`。
6. **单一数据源**。每日配额、会话事件各自只有一个权威表；领域记忆只住 `workspace_facts`，yaml/md 是投影。
7. **每个 capability 都有 logger**。关键路径 INFO，失败路径 WARNING/ERROR。
8. **重构不做兼容**。开发期不留 `_LEGACY_*` 分支，旧 key 直接清库迁走。
9. **LLM 组件必须有 heuristic fallback**。LLM 不可用 / 超时 / JSON 解析失败时降级到可用行为，绝不静默崩溃。

---

## 12. 验收入口

1. `GET /api/modules/job/greet/health` — 连接器状态、执行时段。
2. `GET /api/modules/job/chat/health` — 连接器状态、HITL 开关。
3. `GET /api/modules/job/profile/health` — workspace_id、DB 可用性。
4. `POST /api/modules/job/profile/snapshot` — 返回完整 `JobMemorySnapshot` JSON。
5. 自然语言测试：
   - "以后不要投拼多多了，笔试挂过" → 应产生 1 条 `avoid_company` item。
   - "投递过字节，简历被锁，暂时不要投递字节" → 应产生 2 条 item（`application_event` + `avoid_company`，后者 `valid_until ≠ null`）。
   - "我能力不行，暂时不要投大厂" → 应产生 1 条 `avoid_trait` item。
   - "三个月之后我应该能投大厂了" → 应对已有 `avoid_trait` 做 `supersede` 或设置 `valid_until`。
6. `pulse profile load` 与 `pulse profile dump` 往返等价性测试。
7. 编辑 `resume.md` → `pulse profile load` → `GET /profile/snapshot` 能看到更新后的简历摘要。
