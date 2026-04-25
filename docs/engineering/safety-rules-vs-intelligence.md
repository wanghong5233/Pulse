# 规则与智能的边界:Pulse SafetyPlane 设计决策

> **代码权威**:`../adr/ADR-006-v2-SafetyPlane.md`。本文只承载**设计思路与横向调研留痕**。凡与 ADR-006-v2 代码实现冲突之处,以 ADR 为准;本文"4 族 / when 词汇表 / core.yaml 骨架"是概念分析,**v2 落地时取消了 YAML 规则引擎,改为 Python policy 函数**(见本文 §12)。
>
> 定位:Pulse SafetyPlane 的**设计决策过程留痕**。业界横向调研(5 产品) + Pulse 纵向验证(所有 module 动作) + 双重证据下的最终设计 + 反模式清单。
> 目标读者:(1) 未来维护 Pulse 的自己;(2) 写博客 / 技术面试时需要讲"Agent 授权如何落地"的自己;(3) 接手 Pulse 并准备接入新 module 的人。
> 关联:`../adr/ADR-006-v2-SafetyPlane.md`(落地决策,权威)、`../adr/ADR-006-SafetyPlane.md`(已 Deprecated)、`./adr-guide.md`(文档规范)、`./agent-concepts.md`(术语速查)、`../code-review-checklist.md`(工程宪法)。

---

## 1. 为什么有这份文档

SafetyPlane 是 Pulse 跨 8+ module 的授权层。这种架构级决策若只写进 ADR,后来者只看得到"写成什么样",看不到"为什么这么写",等接入第二个 module 时就会在"要不要新增一族""要不要放宽 fallback""要不要让 LLM 直接判"几类问题上**原地返工**。

本文补上 ADR-006 刻意不承载的内容(`adr-guide.md` §2 要求 ADR 不堆调研):

- **横向证据**:5 个已落地 Agent / 自动化产品的真实授权分类,跨产品收敛信号
- **纵向证据**:Pulse 所有 module(已落地 + 规划)的具体动作清单,对 4 族零溢出验证
- **反模式清单**:本项目实际走过的弯路(含一次被自己推翻的 6 族草案),未来遇到类似歧路直接查此表
- **对 ADR-006 的引用钩子**:每一条设计决定能锚回 §X.Y,避免 ADR 与本文漂移

---

## 2. 需求锚点:Pulse 的授权场景

照抄业界做法会出事,先把 Pulse 的独有约束锁定:

| 维度 | Pulse | 与主流框架差异 |
|---|---|---|
| 部署形态 | 单用户自部署,部署者=使用者 | 5 产品中 3 个是 SaaS(Operator / AgentCore / AGT) |
| 最高威胁 | 代表用户对外承诺 / 不可逆消费 / PII 外发 | Claude Code 主防 RCE,Pulse 不碰 shell |
| 用户打扰成本 | **极低**(企业微信 IM 常驻,已是消息常态) | 终端弹窗/浏览器 confirm 打断编码 |
| 运行时长 | **24/7 长驻**,多 patrol 后台运行 | Claude Code / Operator 会话式 |
| 触发方式 | 用户主动 + patrol 定时 + 外部事件(邮件到达) | 主流框架几乎只有"用户主动" |
| 模块组合度 | 8+ module 同进程,共享 Brain 与 Memory | 框架多数是单 domain |

由此导出 Pulse 的**特殊要求**(业界产品不一定提供):

1. **后台触发必须升级**(patrol / 邮件触发):用户不在线时误操作无法挽回,ADR-006 §5.1 必须显式规则
2. **跨 module 借权硬禁**:`job_chat` 不得调 `game` 工具,即使 `game.yaml` 有 allow —— 这是架构级不变量
3. **IM 通道决定 fallback 成本**:Pulse 走 `fail-to-ask` 成本 ≈ 0,企业级产品走 `default-deny`(成本更高)不适配

---

## 3. 业界调研:5 产品的真实授权分类

### 3.1 Claude Code(Anthropic 官方 CLI)

来源:<https://code.claude.com/docs/en/permissions> · <https://code.claude.com/docs/en/permission-modes>

**工具级权限 3 层**:Read-only(auto,不可改) · File Edit / Bash / Net(ask,session 或项目级持久) · Protected paths(deny,任何模式不可绕) —— 叠加 6 种 permission modes(default / acceptEdits / plan / auto / dontAsk / bypassPermissions)。

**规则评估顺序**(原文):

> "Rules are evaluated in order: **deny → ask → allow**. The first matching rule wins, so deny rules always take precedence."

**关键启示**:
- 评估顺序 forbid-wins,不是 simple overlay
- 默认模式 fallback = ask(读操作除外)
- `auto` 模式有 YOLO Classifier,但 classifier **失败时 fail-open 到用户询问**(不是自动 deny)

### 3.2 OpenAI Operator(Computer-use Agent)

来源:Operator System Card(2025-01-23, PDF)

**5 层敏感动作**:
- 完全 deny:银行转账 / 股票 / 高风险决策(proactive refusal 94%)
- Watch Mode:邮件/金融页面持续操作,用户离开页面自动暂停
- Confirmation:购买 / 发邮件 / 删日历 / **任何世界状态变更**(召回率 92%)
- Takeover Mode:输入登录凭据 / 支付信息 / CAPTCHA(人工接管,不截图不记录)
- Auto:浏览 / 搜索 / 阅读 / 非提交性交互

**关键启示**:
- "世界状态变更"是一级分类维度,不是"金额"或"邮件"这种业务标签
- Operator 没有任何用户可调项 —— 安全是系统强制

### 3.3 Home Assistant Assist + LLM API

来源:<https://www.home-assistant.io/voice_control/voice_remote_expose_devices/> · `homeassistant/helpers/llm.py`

**2 层 + 1 过滤**:未暴露(deny,LLM 不知道存在) · 已暴露(auto,包括锁/门/警报,**无二次确认**) · IGNORE_INTENTS(deprecated / trivial intents 从工具列表中剔除)。

**关键启示(反面教材)**:
- HA 缺失 ACT_IRREVERSIBLE 族的 ask 层 —— 已暴露的锁直接 auto 控制,**被社区视为已知缺陷**(per-pipeline 实体隔离仍是 open feature request)
- Pulse 必须补齐这个族,不能抄 HA 的"二值门"

### 3.4 Browser-use 类(Gemini CLI browser agent + Controller.im)

来源:<https://github.com/google-gemini/gemini-cli/issues/15963> · <https://docs.controller.im/features/auto-approving-actions>

**3 层执行**:
- Hard block(YOLO-proof):黑名单域名导航、文件上传硬拦,**任何 approval 模式无法绕**
- Policy-based confirmation(priority 999):fill_form / upload / evaluate_script
- Auto allow:take_snapshot / list_pages 等只读观察

**关键启示**:
- **YOLO-proof 分层**:hard block 层不受 YOLO 模式影响 —— 对应 Pulse 的"族 DENY 无论 session_approvals 如何均生效"
- 原文:"guardrails must be enforced at the **application level**, not solely via policy engine rules — because policy rules can be overridden by YOLO's catch-all allow"

### 3.5 Microsoft Agent Governance Toolkit + AWS Bedrock AgentCore

来源:<https://aka.ms/agent-governance-toolkit> · <https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html>

**Microsoft AGT 动作**:allow / deny / audit / block(Entra 额外 REQUIRE_APPROVAL / MASK)
**AWS AgentCore(Cedar)语义**:`permit(principal, action, resource) when {...}` + `forbid(...)`,forbid-wins,default-deny

**Cedar `when` 子句示例**(原文):

```cedar
permit (principal, action == Action::"process_refund", resource)
when {
  context.input.amount < 1500 &&
  context.input.paymentMethod == "credit-card" &&
  ["US","CA","MX"].contains(context.input.country)
};
```

**关键启示**:
- 业务属性(amount / paymentMethod / country)全部是 `when` 子句条件,不是一级分类
- 企业级 default-deny 适合"错杀优于错放"场景,Pulse 单用户不适用 —— 选 `default-ask`

### 3.6 横向对比表

| 维度 | Claude Code | Operator | Home Assistant | Browser Agent | AGT + AgentCore |
|---|---|---|---|---|---|
| 一级分类数 | 3(read / mutate / protected) | 5 层 | 2(exposed/not) | 3(hard/policy/auto) | 4 动作 |
| 默认策略 | ask | ask | deny / auto(二元) | ask | **deny** |
| 永远 deny 类 | protected paths | 银行/股票 | 管理任务 | 黑名单域 / upload | 任意 forbid |
| 永远 auto 类 | 内建只读 | 浏览/搜索 | 已暴露一切 | snapshot / list | allow + audit |
| 可逆性进分类 | 否 | **是**(显式) | 否 | 部分 | 否(靠 context) |
| 金额/频率阈值 | 否 | 否 | 否 | maxActions | **是**(when) |
| 会话 vs 持久 | **是** | 否 | 否 | 否 | 否 |
| YOLO / bypass 模式 | 有(容器隔离) | 无 | 无 | 有(hard block 不受影响) | 无 |

### 3.7 跨产品收敛信号

4 族一级分类,证据强度 ★★★★★:

| 族 | 含义 | 默认 | 产品共识 |
|---|---|---|---|
| **OBSERVE** | 只读观察 | auto | 5/5 |
| **ACT_REVERSIBLE** | 可撤销状态变更 | ask(首次) | 3/5 明确 + 5/5 隐含 |
| **ACT_IRREVERSIBLE** | 不可撤销 / 高影响 | ask(每次) | 4/5 明确 |
| **DENY** | 超授权边界 | deny(forbid-wins) | 5/5 |
| *(SUPERVISED)* | 监督模式 | step-up | 2/5(Operator / Claude Code auto) |

**最强不变量**(5/5 共识):

1. 只读操作 auto,不需要授权 —— 反例即无限骚扰用户
2. 永远 deny 类必须存在,且 forbid-wins —— 反例即 HA 已暴露锁可直接控制
3. 世界状态变更必须 ask —— 反例即 HA(被社区骂)
4. **业务属性以 `when` 条件表达,不是独立族** —— 反例即 6 族草案(见 §11 反模式)

---

## 4. Pulse 纵向验证:所有 module 动作 → 4 族零溢出

调研把 Pulse 当前与规划所有 module 的具体动作扫一遍(来源:`src/pulse/modules/` 代码 + README Roadmap)。按族归类后的结果:

### 4.1 已落地 module 动作

| 族 | 动作 |
|---|---|
| **OBSERVE** | `BossPlatformConnector.scan_jobs / fetch_job_detail / pull_conversations / check_login` · `JobSnapshotMatcher.match` · `JobGreeter.compose` · `IntelQueryModule.search` · `IntelInterviewModule.search_web` · `email_tracker._fetch_imap_emails(read-only)` · `PatrolControlModule._list_handler / _status_handler` |
| **ACT_REVERSIBLE** | `JobProfileService.{set_hard_constraint, update_resume, record_item, retire_item, clear_all}` · `JobChatService.run_ingest` · `IntelKnowledgeStore.append` · `email_tracker._persist_email_event` · `system.PatrolControlModule._enable_handler / _disable_handler` · `FeedbackLoopModule._process_feedback` |
| **ACT_IRREVERSIBLE** | `BossPlatformConnector.{greet_job, reply_conversation, send_resume_attachment, click_conversation_card, mark_processed}` · `IntelInterviewModule.post_webhook`(飞书/企业微信) · `email_tracker._mark_imap_seen`(+FLAGS \\Seen) |
| **DENY** | 尚无规则命中 —— 已落地 module 不触发 |

### 4.2 规划中 module 典型动作

| 族 | 动作(标注 `[planned]`) |
|---|---|
| **OBSERVE** | `travel.查机票 / 酒店 / 天气` · `finance.拉银行流水 / 券商资产` · `learn.订阅源抓取` · `health.同步睡眠心率` · `home.查电量 / 查设备状态` · `mail.读邮件` |
| **ACT_REVERSIBLE** | `home.开灯 / 调空调`(完全可逆) · `mail.加日历条目 / 写分类规则 / 垃圾静音规则` · `game.切换账号 / 切区服` · `learn.错题本写入 / 日更要点入库` |
| **ACT_IRREVERSIBLE** | `game.抽卡 / 付费 / 签到(日度单次) / 日常副本消耗体力` · `travel.机票下单 / 酒店下单 / 改签` · `mail.自动代回外部邮件` · `home.开锁 / 开车库门` · `finance.转账(若启用)` · `health.体检数据外发分析` |
| **DENY** | `home.同时开所有门锁` · `home.夜间静音时段开灯` · `finance.单笔转账 > 日上限` · 任意**跨 module 借权**(如 `job_chat` 调 `game` 工具) |

### 4.3 结论

- 已落地 + 规划所有动作**零例外**落入 4 族
- 业务属性(金额 / PII / 承诺 / 账户)以 `when` 条件出现,跨族可复用(同一个 `amount > N` 既可在 `game.yaml` 也可在 `travel.yaml` 用)
- SUPERVISED 族在 Pulse 暂无对应动作 —— Phase C 再说,MVP 不做

---

## 5. 业务属性的正确位置:`when` 条件词汇表

业界 5 产品全部把业务属性当作**规则谓词**,不是族。Pulse 沿用这个抽象,MVP 词汇表:

| 条件键 | 语义 | 来源 | 覆盖族 |
|---|---|---|---|
| `amount` | 消费金额(数值,单位 CNY) | Intent 参数 | REVERSIBLE / IRREVERSIBLE |
| `pii_level` | 涉及 PII 程度:`none / low / medium / high` | LLM label | 所有非 OBSERVE |
| `represents_user_commitment` | 是否代表用户做主观表态 | LLM label | IRREVERSIBLE |
| `evidence_coverage` | profile / memory 是否覆盖该问题 | RuleEngine 计算 | REVERSIBLE / IRREVERSIBLE |
| `cross_domain` | `action.domain != context.module` | 静态 | 所有族(触发 DENY) |
| `scope` | 动作作用域:`single / batch / all` | Intent 参数 | IRREVERSIBLE |
| `trigger` | 触发源:`user / patrol / schedule / external` | PermissionContext | 所有族 |
| `session_approved` | session 内是否已 approve 过同类 intent | PermissionContext | REVERSIBLE |

**扩展原则**:新 module 提 PR 时可贡献新键,但必须满足:
1. 在 `when` 上可形式化(不能是"这段话感觉危险"这种主观)
2. 如果需要 LLM 打标,值域必须是封闭枚举或数值,**不得**是自由文本
3. 在 `config/safety/<domain>.yaml` 的 schema 注释里定义用法

---

## 6. 规则与智能的边界(跨模块通用版)

Zylos 2026《Policy Engines for AI Agent Governance》的 4 类别模型,映射到 Pulse 的"族 + when"双轴:

| 类别 | 技术形态 | Pulse 跨域例子 |
|---|---|---|
| **A 永不 LLM** | 纯 `when` 条件(结构化字段比对 / 阈值 / 白名单) | `amount > day_cap` · `cross_domain == true` · `pii_field in blocklist` —— 覆盖 80%+ 决策 |
| **B 规则优先,LLM 选配** | `when: llm_label.X == ...`,LLM 出结构化 label 再进 rule | `mail.邮件分类` · `game.卡池类型识别` · `finance.支出品类识别` · `job.岗位匹配度打分` |
| **C 必须 LLM** | 规则无法形式化的纯语义 | `llm_label.represents_user_commitment`(跨 job/mail/travel/game) · `llm_label.command_is_semantically_dangerous`(home) · `llm_label.report_has_anomaly`(finance) |
| **D 混合** | prefilter → LLM → deterministic enforcement | 所有自由草稿场景:job 回 HR / mail 回外部 / game 跨服私聊 / travel 行程备注 |

**铁律**(5 产品 + Windley + Zylos 共识,跨 module 不可破):

1. **LLM 只出结构化 label**,不直接出 allow/deny/ask 字符串
2. **最终 ALLOW/DENY 只能从 deterministic rule function 来**(原文 Zylos:"LLM as advisor, not judge")
3. **LLM 失败/超时 → fail-to-ask**,不是 fail-open 也不是 fail-deny

---

## 7. Pulse 最终设计:双轴模型 + MVP core.yaml 骨架

### 7.1 双轴

```text
┌──────────────────────── 族 (一级分类, 架构轴) ────────────────────────┐
│                                                                      │
│  OBSERVE  ◀───────── ACT_REVERSIBLE ◀──── ACT_IRREVERSIBLE ─────▶ DENY│
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
     ▲ when                    ▲ when                   ▲ when
     │                         │                        │
   [业务属性词汇表: amount / pii_level / represents_commitment /
    evidence_coverage / cross_domain / scope / trigger / session_approved]

评估顺序:      DENY  >  ASK  >  ALLOW      (forbid-wins)
全局 fallback: ASK                         (fail-to-ask)
族默认:        OBSERVE=allow / 其余=ask
```

### 7.2 MVP `config/safety/core.yaml` 最小集(5 条)[历史草案,v2 未落地]

> **状态**:本节是 v1 ADR 时期的规则骨架。ADR-006-v2 判断单用户 + 3–10 条规则的规模不值得引入 YAML DSL,把规则改写成 Python policy 函数(`src/pulse/core/safety/policies.py`),同等语义、更好的 IDE 支持与类型安全。下面 YAML 作为"族 + when" 概念的可视化保留,**不是活体配置**。

```yaml
family_defaults:
  OBSERVE:            allow
  ACT_REVERSIBLE:     ask
  ACT_IRREVERSIBLE:   ask
  DENY:               deny

rules:
  - id: core.cross_domain_deny
    forbid:
      - action.domain != context.module
    reason_code: cross_domain_privilege_abuse

  - id: core.represents_commitment_ask
    match:
      action.family: ACT_IRREVERSIBLE
      when: intent.llm_label.represents_user_commitment == true
    decision: ask
    evidence_hint: "Agent 无资格替用户做主观表态,必须用户回答"

  - id: core.background_no_irreversible
    match:
      context.trigger in [patrol, schedule, external_event]
      action.family: ACT_IRREVERSIBLE
    decision: ask
    timeout_sec: 3600

  - id: core.pii_highrisk_ask
    match:
      when: intent.llm_label.pii_level == high
    decision: ask

fallback:
  decision: ask
```

5 条覆盖 Pulse 所有已落地 + 规划 module 的架构级约束;每个 domain(`job_chat.yaml` / `game.yaml` / ...)在此之上**只能收紧,不能放宽**。

### 7.3 评估顺序与 fallback 的产品级证据

| 选择 | Pulse 采用 | 反面证据 |
|---|---|---|
| **forbid-wins(deny > ask > allow)** | ✅ | Claude Code 官方 + AGT + AgentCore 一致 |
| **全局 fallback = ask** | ✅ | Claude Code default mode + Gemini CLI |
| **全局 fallback = allow** | ❌ | 无任何产品采用 —— 上轮 MVP 草案错提 |
| **全局 fallback = deny** | ❌ | AGT / AgentCore 采用,但需要企业级策略团队维护规则覆盖率;Pulse 单人维护不适配 |
| **LLM 直接出 decision** | ❌ | Windley《AI is not your policy engine》明确反对 |

---

## 8. 三阶段演进(按族 / 类别触发,不按 module 排)

### Phase A-B(当前 ADR-006 实施):纯规则 + ASK 兜底

- Rule Engine 4 族 + when 双轴
- core.yaml 5 条(§7.2)
- job_chat domain.yaml 贡献 4 条(对齐 ADR-006 §5.2)
- C/D 类别决策**全部走 ask**,不引入 LLM Enricher

### Phase C(第二个 module 上线,典型族出现 C 类决策时触发)

触发信号与首批 Enricher:

| 触发 module | 首撞类别 | 首批 Enricher label | 族 |
|---|---|---|---|
| `mail` | C(语义判断"邮件隐含要回复") | `commits_on_behalf_of_user` | IRREVERSIBLE |
| `game` | B(卡池识别) | `pool_type: up/permanent` | IRREVERSIBLE |
| `home` | C(语义危险指令) | `command_is_semantically_dangerous` | IRREVERSIBLE |
| `finance` | C(账单异常) | `report_has_anomaly` | REVERSIBLE |

**Enricher 契约**:输入 Intent,输出 structured label 列表;label 进入 rule engine `when` 子句;Enricher 超时/失败 → label 缺失 → `fail-to-ask` 自然触发。

### Phase D(长期):规则自举 + 信任退化

- `task.asked` / `task.denied` 事件累积 → 候选规则生成(仿 Claude Code Accept/Reject 日志反向喂规则)
- 同 task 内连续 3 次 deny → 降级为全局 ask 模式(仿 Claude Code auto 模式"剥离危险 allow")
- `AskRequest.timeout_sec` 到期 → 状态 `deferred`;`resume` 前**重新校验世界状态**(LangGraph stale-state 教训)

---

## 9. 反模式清单(别犯这些)

| # | 反模式 | 为什么错 | 本项目走过 |
|---|---|---|---|
| 1 | LLM 直接输出 allow/deny | LLM 不具备授权/撤销/后果的语义一致性(Windley);任何 prompt injection 绕过都等于越权 | 否 |
| 2 | 全局 fallback = allow | 5 产品均无此采用;任何未声明工具 = 默认可用 = 无闸门 | **是**(上轮 A.2 草案,已推翻) |
| 3 | 用业务属性(金额/PII/承诺)做一级族 | 导致规则交叉重叠(订机票既金额又 PII 归哪族?);业界正确做法是把业务属性当 when 条件 | **是**(上轮 6 族草案,已推翻) |
| 4 | 没有 cross_domain 硬禁 | job_chat 一旦能调 game 工具,安全假设全毁;必须 forbid-wins(§7.2 `core.cross_domain_deny`) | 否 |
| 5 | patrol 后台直接做 IRREVERSIBLE 不升级 | 用户不在线时 Agent 误发消息不可挽回;必须在 core 层强制升级(§7.2 `core.background_no_irreversible`) | 否 |
| 6 | resume 时不校验世界状态 | 用户 2h 后回答"同意",但 HR 早已换了时间/岗位已关;LangGraph 官方教训 | 待修(Phase A.4 `SuspendedTaskStore.resolve` 时落实) |
| 7 | Enricher 失败时 fail-open 到 allow | 让攻击者/异常能绕过 C 类判断;必须 fail-to-ask | 否(Phase C 写入 contract) |
| 8 | `ChatAction.ESCALATE` 仅发 warning 不阻塞 | 单向广播不是升级;ADR-006 §1 已记判例 | **是**(`job_chat` 现状,待 Phase B.3 改) |
| 9 | 会话内 approve 无限期持久化 | 长驻 Agent 场景下会逐步放松闸门;session_approvals 必须绑定 `(user_id, module, task_id)` + TTL | 否 |
| 10 | 用 `needs_hitl` / `needs_review` 的 bool 信号 | 下游消费点不一致,信号被静默吞掉(ADR-006 §1 五漏洞之一) | **是**(`job_chat` 现状,待 Phase B.3 改) |

---

## 10. 与 ADR-006-v2 的实际落地差异(权威)

> 原 "§10 与 ADR-006 的对齐点" 针对 v1 ADR,v1 已 Deprecated,本节取代它,直接对齐 v2 代码。

| 本文原设计 | v2 落地 | 原因 |
|---|---|---|
| YAML `core.yaml` + `domain.yaml` + `RuleEngine` | Python 函数 `reply_policy` / `send_resume_policy` / `card_policy`(`src/pulse/core/safety/policies.py`) | 规则数量少(MVP 3 条、可预见 ≤10 条),YAML DSL 的维护成本高于代码,且失去类型校验 |
| Brain `before_tool_use` hook 作为主闸门 | Service 层 `_execute_*` 方法直接调 policy | patrol 路径不经 Brain,hook 会被绕过 |
| `ActionResolver`(tool_call → intent 反向映射) | Service 层直接构造 Intent | 反向映射是一层多余抽象,错位会放过 Deny |
| `shadow` 档(评估不阻断) | 已移除,自动升级 `enforce` | 不阻断 = 没接入,只产审计噪声 |
| `when` 词汇表作为 YAML 谓词 | `Intent.args` + `PermissionContext.profile_view` + `session_approvals`,直接在 Python 里读 | 同一语义,落到静态类型而非字符串 DSL |
| `family_defaults.OBSERVE=allow` 等族默认 | policy 函数显式 `return Decision(kind="allow", ...)` | 单用户下动作穷举只有 3 条,不需要族维度的默认映射 |

本文"**4 族 + when 词汇表**"作为概念分层仍然成立,v2 的三条 policy 函数内部依然按"先 deny、再 ask、兜底 allow"顺序展开,只是从 YAML 规则解释器变成 Python 代码行。

---

## 10.bis 与 ADR-006(v1, Deprecated)的历史对齐点

本文给出的设计对 ADR-006(Proposed)的**修订请求**:

| ADR-006 § | 原文要点 | 修订后 |
|---|---|---|
| §2 分层职责 | Core Rules 泛指"跨模块通用规则" | 明确为 **4 族 + when 词汇表**;SafetyPlane core 包括 `family` 枚举与评估顺序 |
| §4.3 Rules schema | `otherwise.decision` ∈ {ask, deny},不得 allow | 保留;新增 `family_defaults` 段 + 引擎级 `fallback.decision = ask` |
| §5.1 Core Rules MVP | 3 条(job_chat 语境凑的) | **5 条**:family_defaults + cross_domain_deny + represents_commitment_ask + background_no_irreversible + pii_highrisk_ask(§7.2) |
| §5.3 规则贡献流程 | 泛泛描述 | 新 module PR 必须:(1)声明每个工具所属族;(2)贡献或复用 when 词汇表键;(3)禁止放宽 core |
| §6 演进路径 | 按 module 排 | **按族 × 类别触发排**(§8),与 module 上线顺序解耦 |
| §9 参考 | 仅列既有 Pulse 内部文档 | 追加本文 + 5 产品调研 5 条外部链接 |

---

## 11. 参考

### Pulse 内部

- `../adr/ADR-006-SafetyPlane.md` —— SafetyPlane 落地决策
- `./adr-guide.md` §2 —— ADR 不承载调研/对比/学习笔记
- `./agent-concepts.md` §2/§4 —— Autonomy Spectrum / 四术语辨析(HITL / Escalation / Interrupt / Elicitation)
- `../code-review-checklist.md` —— 工程宪法

### 业界调研(按出现顺序)

1. Claude Code — *Configure permissions*,<https://code.claude.com/docs/en/permissions>
2. Claude Code — *Choose a permission mode*,<https://code.claude.com/docs/en/permission-modes>
3. OpenAI — *Operator System Card*,<https://cdn.openai.com/operator_system_card.pdf>
4. Home Assistant — *Expose entities to Assist*,<https://www.home-assistant.io/voice_control/voice_remote_expose_devices/>
5. Google — *Gemini CLI issue #15963*(YOLO-proof hard block),<https://github.com/google-gemini/gemini-cli/issues/15963>
6. Controller.im — *Auto-Approving Actions*,<https://docs.controller.im/features/auto-approving-actions>
7. Microsoft — *Agent Governance Toolkit*,<https://aka.ms/agent-governance-toolkit>
8. AWS — *Bedrock AgentCore Policy(Cedar)*,<https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html>

### 理论基础

9. Phil Windley — *AI Is Not Your Policy Engine (And That's a Good Thing)*(2025-12),<https://windley.com/archives/2025/12/ai_is_not_your_policy_engine_and_thats_a_good_thing.shtml>
10. Phil Windley — *Why Authorization Is the Hard Problem in Agentic AI*(2026-02),<https://www.windley.com/archives/2026/02/why_authorization_is_the_hard_problem_in_agentic_ai.shtml>
11. Zylos Research — *Policy Engines for AI Agent Governance: Rule-Based and Hybrid Approaches*(2026-03-14),<https://zylos.ai/research/2026-03-14-policy-engines-ai-agent-governance>
12. DEV Community — *Why AI Agent Authorization Is Still Unsolved in 2026*,<https://dev.to/webpro255/why-ai-agent-authorization-is-still-unsolved-in-2026-5hdk>
13. LangGraph — *Human-in-the-Loop (interrupt / Command)*,<https://docs.langchain.com/oss/python/langgraph/human-in-the-loop>
