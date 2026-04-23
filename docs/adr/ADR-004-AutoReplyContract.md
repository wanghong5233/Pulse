# ADR-004: BOSS 自动回复决策契约

| 字段 | 值 |
|---|---|
| 状态 | Proposed (Step A 落地,dry-run 验收后转 Accepted);§6.1 Patrol 对话式控制面 2026-04-22 追加并同步落地 |
| 日期 | 2026-04-22(§1-§6 原决策) / 2026-04-22(§6.1 追加决策 A-D + 代码 + 文档) / 2026-04-22 M9.2(trigger_now + chat-list 脚手架 snapshot wait/resilient + 可观察性降噪) |
| 作用域 | `src/pulse/mcp_servers/_boss_platform_runtime.py`、`src/pulse/mcp_servers/boss_platform_server.py`、`scripts/smoke_auto_reply.py`、`tests/pulse/mcp_servers/test_boss_autoreply_decision.py`;§6.1 额外覆盖 `src/pulse/core/scheduler/engine.py`、`src/pulse/core/runtime.py`、`src/pulse/core/server.py`、`src/pulse/modules/system/patrol/`、`scripts/smoke_patrol_control.py` |
| 关联 | `ADR-001-ToolUseContract.md` §6 P3e(幂等)、`ADR-003-ActionReport.md`、`docs/dom-specs/boss/chat-list/README.md`、`docs/dom-specs/boss/chat-detail/README.md`、`docs/code-review-checklist.md`、`docs/Pulse-AgentRuntime设计.md` §5.6、`docs/Pulse-DomainMemory与Tool模式.md` §6.3、`docs/Pulse-内核架构总览.md` §5.2 |

---

## 1. 现状

**已落地**:chat-list / chat-detail 两份 DOM 合同完成(`docs/dom-specs/boss/`),`_extract_conversations_from_page` 按新合同重写,`extract_chat_detail_state(page) -> ChatDetailState` 纯读 API 就位;会话列表未读信号、消息流发送方、`.respond-popover` 快捷响应条、"发简历" 按钮存在性都已结构化可读。

**缺口**:没有任何一条**决策 + 交互**的编排代码真的把"读到未读" → "决定做什么" → "真点击"串起来。老代码里有 `click_conversation_card` / `send_resume_attachment` 两个 tool,但:

1. 它们的底层 selector(`text=同意` / `.im-card:has-text('交换简历')`)是合同落地**之前**拍脑袋写的,和 `.message-tip-bar .respond-popover .btn.btn-agree` 等合同锚点**完全没对齐**。
2. 它们依赖 `_build_chat_url(conversation_id)` 直接 goto URL 定位会话,但我们的 `conversation_id` 是**内容 hash**(chat-list DOM 无 `data-*` 属性,这是物理事实),真直接 goto 会 404。
3. 没有"先 read ChatDetailState 再决定做什么"的链路,意味着即便入口 tool 被调用,决策层也是盲的。

同时体感层面的痛点:用户在托管模式下希望"HR 发来简历请求卡自动同意 + 发简历" / "HR 发纯文字招呼自动主动发简历"成为常态,**不需要**人再去点;但这个动作是**不可撤回**的,一点就真的把简历推到对方邮箱,必须有 dry-run 安全阀。

---

## 2. 根因:缺少「决策契约 + 编排层 + dry-run 安全阀」三件套

```text
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  chat-list    │ → │ chat-detail  │ → │   Actuator   │
│   Reader       │   │   Reader     │   │   (点按钮)   │
└──────────────┘   └──────────────┘   └──────────────┘
       ✅               ✅                  ❌ selector 不对齐, 且无链路
        ↑                                         ↑
        └── ❌ 谁在编排?(没人)────────────────────┘

        ❌ 谁在决策 "这一条该做什么"?(没人)
        ❌ dry-run 审计能力?(没有)
```

具体来看,**一条完整的"自动回复一次"操作**至少要:

1. `pull_conversations(chat_tab="未读")` → 拿到 N 条 `notice-badge` 存在的 item
2. 对每条 item:
   a. 点开(靠 content hint,不能靠 hash id)
   b. 等 `.chat-conversation` 渲染
   c. `extract_chat_detail_state(page)` → `ChatDetailState`
   d. **决策**:这条状态对应什么动作?(agree / refuse / send_resume / skip)
   e. 执行:真点 `.respond-popover` 浮动条 或 底部"发简历"按钮
   f. 审计:写 `boss_mcp_actions.jsonl`
   g. 幂等:同一条 HR 触发消息不重复响应
3. 汇总报告(配合 ADR-003 ActionReport)

每一步都没代码。老 `click_conversation_card` 只能"已知 conversation_id + 已知 card_type + 已知 action 的情况下点一次按钮",完全跳过了 2c / 2d 两步,**没有决策层**。

---

## 3. 第一性原理

| 维度 | 分析 | 结论 |
|---|---|---|
| **决策来源** | v1 要不要接 LLM 做决策? | **不接**。求职场景的默认动作极其收敛:HR 发简历请求卡几乎一定要同意(不同意约等于放弃该岗);HR 发纯文字招呼几乎一定要回一份简历(这是求职的最小 viable response)。规则足够覆盖 >80% 场景,LLM 只在 v2 的"个性化答复具体问题"时才需要。按宪法 §类型C,"将来可能用到"的 LLM 决策现在**不**提前铺路 |
| **未知情形处理** | 没采样过的 HR 动作卡(面试邀请/换电话/换微信)怎么办? | **保守 SKIP**。让用户人肉处理并补 dump。合同 §H 的 "未采样"列表就是 v2 决策层的 input queue |
| **不可撤回副作用** | 点"同意"就真发简历了,错了没回头路 | **dry-run 默认 True**,所有 cycle 入口强制要求显式 `dry_run=False` 才真点。dry_run 分支不写 audit 不改任何状态,纯粹 "scan + decide + print" |
| **幂等边界** | HR 同一条消息被脚本扫到两次会重复响应吗? | **不会**。幂等 key = `(conversation_id, decision_kind, trigger_mid)`;`trigger_mid` 是 `<li.message-item data-mid="..."`>` 里 BOSS 后端消息 id。哪怕 cycle 在 300s 内被重跑,同一 `trigger_mid` 的 agree/send_resume 不会再点第二次。复用 ADR-001 §6 P3e 幂等框架 |
| **可观测性** | 用户怎么知道脚本做了什么 / 没做什么? | 所有决策(含 SKIP)都落 `boss_mcp_actions.jsonl` 的 `auto_reply_decision` 条目;真执行的部分追加 `auto_reply_result` 条目。dry_run 只 return,不落盘(审计意义是"真动作"才有,dry_run 是讨论) |
| **Actuator 归属** | 在新合同的精确 selector 上直接改老 `_execute_browser_click_card` 吗? | **不改**。老接口有独立 tool 入口(`click_conversation_card`),可能被其他 caller 用着;改它风险传播面大。新路径(`_click_respond_popover_via_browser` / `_click_send_resume_via_browser`)按合同重写,**并存**。未来 refactor 专项清老的 |
| **端到端测试** | 怎么证明这套东西真的 work? | 两层:① 纯函数层:`decide_auto_reply_action` 用真实 chat-detail dump 切出 N 条 state fixture,断言决策 kind(宪法 §测试分层#2+#3)。② 真实浏览器层:`scripts/smoke_auto_reply.py` + `dry_run=True`,在登录过的 Chromium 里跑一轮,打印决策,人肉审核;审核通过后才允许 `dry_run=False`。**不写** Playwright mock 级集成测试(宪法明确反对) |

---

## 4. 接口契约

### 4.1 决策契约(`_boss_platform_runtime.py`)

```python
from dataclasses import dataclass
from typing import Literal

AutoReplyKind = Literal[
    "skip",                    # 不操作
    "click_respond_agree",     # 点 .respond-popover 的 .btn-agree
    "click_respond_refuse",    # 点 .respond-popover 的 .btn-refuse (v1 未使用)
    "click_send_resume",       # 点底部 .chat-controls 里 innerText=="发简历"
]

@dataclass(frozen=True)
class AutoReplyDecision:
    kind: AutoReplyKind
    reason: str              # ≤160 字的决策依据, 写 audit, 供 postmortem
    trigger_mid: str         # state.last_message.data_mid; 空串表示不依赖某条消息
```

### 4.2 决策规则(`decide_auto_reply_action`)

**纯函数**,输入 `ChatDetailState`,输出 `AutoReplyDecision`。规则树按合同 §E 归纳:

```text
IF state.pending_respond is not None:
    # .respond-popover 存在 ⇒ HR 发了需要响应的动作卡
    popover_text = state.pending_respond.text

    IF "简历" in popover_text AND state.pending_respond.has_agree:
        → CLICK_RESPOND_AGREE    (求职视角: 简历请求默认同意)

    ELSE:
        → SKIP  reason="未识别的动作卡类型"  (面试/电话/微信 v1 保守不动)

ELIF state.last_message is None:
    → SKIP  reason="无消息"

ELIF state.last_message.sender == "me":
    → SKIP  reason="最后是我方"

ELIF state.last_message.sender == "bot":
    → SKIP  reason="机器人卡,忽略"  (竞争者 PK 卡等运营内容)

ELIF state.last_message.sender == "friend" AND state.last_message.kind == "text":
    IF state.send_resume_button_present:
        → CLICK_SEND_RESUME  (HR 发纯文字招呼, 主动发简历)
    ELSE:
        → SKIP  reason="HR 发文字但底部无发简历按钮"

ELSE:
    → SKIP  reason="未识别形态"
```

**为什么 v1 不出 `CLICK_RESPOND_REFUSE`**:目前规则树没有任何分支触发它。字面保留是为合同完备性(Actuator 能点),但决策层**不**产出。等 v2 引入 LLM 或规则扩充(如"如果 HR 发了换微信卡 → refuse")时再用。

### 4.3 Actuator(新,按合同精确 selector)

| 函数 | 合同锚点 | 行为 | 返回 |
|---|---|---|---|
| `_click_respond_popover_via_browser(page, *, decision)` | chat-detail §C.4 | `decision="agree"` 点 `.message-tip-bar .respond-popover .btn.btn-agree`;`decision="refuse"` 点 `.btn.btn-refuse`。点后等 `.respond-popover` 从 DOM 消失(=成功信号) | `{ok, status, selector, screenshot_path, error?}` |
| `_click_send_resume_via_browser(page)` | chat-detail §D.1 | 在 `.chat-controls` 里找 `innerText === "发简历"` 的 `.toolbar-btn`(文本精确匹配,合同明示"无独立 class");点击后,若 `.sentence-popover, .pop-wrap` 等 confirm 浮层出现则再点 `确定` / `确认` 文本节点 | 同上 |

**不点 `.card-btn`**:合同明示"卡片内嵌的 `.card-btn` 是通用 class,要靠文本区分";`.respond-popover` 的 `.btn-agree / .btn-refuse` 才是独有且稳定 class。v1 **只**点 popover 路径。

### 4.4 编排层(`run_auto_reply_cycle`)

```python
def run_auto_reply_cycle(
    *,
    max_conversations: int = 5,
    chat_tab: str = "未读",
    dry_run: bool = True,     # ★ 默认 True, 显式改 False 才真点
    profile_id: str = "default",
    run_id: str = "",
) -> dict[str, Any]:
    """
    Pipeline (合同 §E):
      1. _pull_conversations_via_browser(unread_only=True, chat_tab=chat_tab)
      2. FOR EACH conv IN items[:max_conversations]:
         a. _try_click_conversation_by_hint(page, {hr_name, company, job_title})
            (chat-list li 无 data-conversation-id, 合同明示必须走 hint)
         b. page.wait_for_selector(".chat-conversation", timeout=...)
         c. state = extract_chat_detail_state(page)
         d. decision = decide_auto_reply_action(state)
         e. IF dry_run:
              collect (conv, state_snapshot, decision) into report; continue
            ELSE:
              IF _find_recent_successful_action(
                  operation="auto_reply",
                  match={conversation_id, decision_kind, trigger_mid},
                  within_sec=_idempotency_window_sec(),
              ):
                  mark as "idempotent_replay", skip
              ELSE:
                  execute decision via appropriate _click_*_via_browser
                  _append_action_log("auto_reply_result", ...)
      3. return {
             ok, status, dry_run, decisions:[...], executed:[...], skipped:[...], errors:[...]
         }
    """
```

**dry_run 行为契约**:

- `dry_run=True`:不调用任何 `_click_*` 函数;不写 `boss_mcp_actions.jsonl`;return 里 `executed` 恒空,`decisions` 列全所有会话 + 决策 + 原因
- `dry_run=False`:按正常幂等 + 审计路径,每个 decision 产出一条 `auto_reply_result`;失败不重试(交给下一轮 cycle)

**幂等 key**:`match = {"conversation_id": cid, "decision_kind": kind, "trigger_mid": mid}`。`trigger_mid` = `state.last_message.data_mid`;`pending_respond` 场景下用 popover 对应的那条 card `li` 的 `data-mid`(cycle 内部拼接)。若 `trigger_mid` 为空(极端情况 DOM 未带),默认跳过执行并记 `status="skipped_no_trigger_mid"`,避免盲点。

### 4.5 MCP tool 暴露

```python
# boss_platform_server.py
@_MCP.tool
def auto_reply_cycle(
    max_conversations: int = 5,
    chat_tab: str = "未读",
    dry_run: bool = True,
    profile_id: str = "default",
    run_id: str = "",
) -> dict:
    """Scan unread chat-list, decide per-conversation action, optionally execute.

    dry_run=True (default): returns decisions only; no DOM click, no audit.
    dry_run=False: executes agreed decisions with idempotency guard + audit.
    """
    return runtime.run_auto_reply_cycle(
        max_conversations=max_conversations,
        chat_tab=chat_tab,
        dry_run=dry_run,
        profile_id=profile_id,
        run_id=run_id,
    )
```

### 4.6 ActionReport 集成(复用 ADR-003)

cycle 结尾产出一份 `ActionReport`:

| 字段 | 值 |
|---|---|
| `action` | `"job.auto_reply"` |
| `status` | 聚合自 details:全 agree/send_resume 成功 ⇒ `succeeded`;混合 ⇒ `partial`;全 skip ⇒ `skipped`;有错 ⇒ `partial`/`failed`;`dry_run=True` ⇒ `preview` |
| `summary` | 例 `"扫描 3 条未读,2 条已同意简历请求,1 条跳过"` |
| `details[i].target` | `f"{hr_name} / {company} / {job_title}"` |
| `details[i].status` | `succeeded` / `skipped` / `failed` |
| `details[i].reason` | decision.reason(SKIP 原因人类可读) |
| `details[i].extras` | `{decision_kind, trigger_mid, popover_text?}` |
| `metrics` | `{scanned, agreed, sent_resume, skipped, failed}` |

`preview` 状态 + ADR-003 的 judge Meta-rule 保证:dry_run 产出的 ActionReport 不会被 LLM 当成"已执行"来汇报(验证器的 `inference_as_fact` 规则会兜)。

---

## 5. 可逆性与重评触发

### 5.1 开关

| 层 | 环境变量 | 关闭后行为 |
|---|---|---|
| 全局禁用 auto-reply | `PULSE_BOSS_AUTOREPLY=off` | cycle 入口直接返回 `{ok:false, status:"disabled"}`,不走任何 DOM |
| 强制 dry_run | `PULSE_BOSS_AUTOREPLY_FORCE_DRY_RUN=on` | 即便调用方传 `dry_run=False`,runtime 层 override 回 True;用于 ops 紧急刹车 |
| 幂等窗口 | `PULSE_BOSS_MCP_IDEMPOTENCY_WINDOW_SEC`(复用) | 0 即关 |

### 5.2 重评触发

1. dry_run 跑 3 次后,决策样本看齐用户心智,且未见假阴性("该回没回")/假阳性("不该回乱点") → 允许切 `dry_run=False`
2. `boss_mcp_actions.jsonl` 中 `auto_reply_result.status="idempotent_replay"` 出现 ≥5 次 → 证明幂等框架落地,可以把窗口拉长到 1h
3. 出现新的 HR 动作卡形态(合同 §H 未采样条目) → 补 dump → 扩充决策规则 → 本 ADR §4.2 追加分支
4. 用户反馈"Agent 同意了不该同意的卡" → 立即 `PULSE_BOSS_AUTOREPLY=off`,补合同采样,再用 fixture 驱动写 test 钉住 regression,再放开

---

## 6. 落地顺序

| 阶段 | 内容 | 状态 |
|---|---|---|
| **Step A.1** | 本 ADR 书面决策 | ✅ 本次 |
| **Step A.2** | `AutoReplyDecision` dataclass + `decide_auto_reply_action` 纯函数 | ✅ 本次 |
| **Step A.3** | `_click_respond_popover_via_browser` + `_click_send_resume_via_browser` Actuator | ✅ 本次 |
| **Step A.4** | `run_auto_reply_cycle` 编排(含 dry_run + 幂等) | ✅ 本次 |
| **Step A.5** | `auto_reply_cycle` MCP tool 暴露 | ✅ 本次 |
| **Step A.6** | `test_boss_autoreply_decision.py` 真实 dump 驱动纯函数守卫 | ✅ 本次 |
| **Step A.7** | `scripts/smoke_auto_reply.py` dry-run 端到端脚本 | ✅ 本次 |
| **Step B.1** | 用户在真实浏览器跑 dry_run,审 ≥3 条样本决策正确 | ⏳ 交用户 |
| **Step B.2** | 用户授权 `dry_run=False` 跑 1 条真实 agree/send_resume,观察 audit + 截图 | ⏳ 交用户 |
| **Step B.3** | 用 ADR-003 ActionReport 把 cycle 接进 Brain 的 JobChat/AutoReply module(如需要) | — (独立任务) |
| **Step C**(未来) | LLM 层:对 HR 的"具体问题"生成个性化答复 | — |
| **Step D**(未来) | 新 HR 动作卡形态(面试/换电话)采样补决策规则 | — |

Step A 是**完全新增且并存**的路径:不动任何老 `click_conversation_card` / `send_resume_attachment` / `_execute_browser_*` 代码;`boss_platform_server.py` 只加一个新 tool。老路径的消费者零影响。

---

## 6.1 Patrol 对话式控制面(2026-04-22 追加决策 + 同步落地)

### 6.1.0 背景

Step A 把 `run_auto_reply_cycle` 做成可被 MCP tool / 脚本调用的 rule-based 编排;真正跑起来还需要回答**一个独立问题**:**用户在 IM 里说"开启自动回复 / 自动回复先别跑了 / 现在就跑一次 / 最近跑得怎样 / 后台有什么任务",这些自然语言指令怎么映射到 AgentRuntime 的 patrol 生命周期?**

现状交叉检查(代码 + 文档 + 行业先进产品)得到的事实:

| 观测 | 位置 | 结论 |
|---|---|---|
| `AgentRuntime.register_patrol(enabled=True/False)` 是**启动期**开关 | `src/pulse/core/runtime.py` L228-L284 | 不支持 runtime 改 |
| `ScheduleTask.enabled` 是 `dataclass(slots=True)` 公开可变字段 | `src/pulse/core/scheduler/engine.py` L13-L24 | 软开关的原材料已就位 |
| 只有全局 `pause_patrols` / `request_takeover` / `release_takeover` | `src/pulse/core/runtime.py` L955-L1020 | 粗粒度,关不了单个 patrol |
| `/api/runtime/reset/{task_name}` 已有 per-task 心智 | `src/pulse/core/server.py` L1260-L1263 | per-patrol 粒度是既定方向 |
| `job_chat.patrol` handler 调 `JobChatService.run_process`(LLM-first) | `src/pulse/modules/job/chat/module.py` | 与 `run_auto_reply_cycle`(rule-based)是**两条独立路径** |
| ChatGPT Scheduled Tasks / LangGraph thread-cron 用"对话式 IntentSpec 控制后台任务"作为主流操作面 | 行业调研 | 验证方向正确 |

### 6.1.1 决策 A:per-patrol 软开关 = `ScheduleTask.enabled` 的内存状态

**决策(2026-04-22 修订)**:
- **注册与启用解耦**:`on_startup` 中 `register_patrol()` **无条件执行**(no env guard, no early return);`ScheduleTask.enabled` 初始恒为 `False`(`register_patrol` 默认值)
- **启停唯一来源 = IM 对话**:用户通过 `system.patrol.enable/disable` 翻转 `ScheduleTask.enabled` in-memory
- **重启语义**:重启 Pulse 后,所有 patrol 回到 `enabled=False`(默认安全);用户需要通过 IM 再次启用。这是有意为之 — 重启后不主动副作用,对齐 systemd 非 `wants=default.target` 的 unit 语义
- **业务层 killswitch(第二层护栏)仍保留**:`PULSE_BOSS_AUTOREPLY=off` 之类的 env 作用在 handler 内部,让 enable 后的 patrol 仍返回 `{status:"disabled"}`;这一层与 `ScheduleTask.enabled` 正交,属于"紧急阻断",不承担日常启停

**为什么从"env 控制初始 enabled"退化到"IM 独占"**:
- 前一版本(`patrol_chat_enabled` env)构成**双源真相**:既可以从 env 启,也可以从 IM 启 — 违反 ADR-001 单一认知路径
- `trace_0cf87040e0e5` 暴露:env 默认 `False` → `on_startup` early-return → patrol 根本没注册 → 控制面 `list_patrols=[]` → Brain 调 `system.patrol.enable` 必然 "not found",系统对用户呈现"无法支持此操作",根因是"被控制对象不存在",**非**控制面 bug
- env 只在"首次启动的一瞬间"决定状态,随后立刻被 IM 覆盖,存在即是"弱耦合而非无耦合"—— 对个人单机助手无价值,删除
- `JobSettings.patrol_*_enabled` 字段**同步删除**;保留的 `patrol_*_interval_peak/offpeak` 是性能 knob(调度节拍),与启停语义正交,不在本次调整

**为什么不持久化**:
- 宪法 §类型 C:持久化层是"将来可能用到"的基础设施,当前没有跨进程场景
- 持久化带来 storage schema / migration / 反序列化一致性 3 个额外 surface area,不值得
- Brain 对话 session 本身有 memory,用户可以问"我刚才关了哪个 patrol"

**未来演进方向(未承诺,记 TODO)**:
- 若用户反馈"每次重启都要喊一遍开启"成为真实痛点,通过新 ADR 引入轻量持久化(候选:`~/.pulse/patrol_state.json`,启动时读取作为 `register_patrol(enabled=...)` 的真值源)
- 在此之前,保持"重启=回到默认安全态"的语义最简单

### 6.1.2 决策 B:控制面 = in-process IntentSpec,**不**走 MCP

**决策**:
- 新增 `src/pulse/modules/system/patrol/module.py`,暴露 5 个 `IntentSpec`:`system.patrol.list / status / enable / disable / trigger`
- Handler 直接调 `runtime.enable_patrol(name)` 等 in-process API
- **不**把这些能力包成 MCP tool;MCP 用于跨进程副作用边界,patrol 控制面是 kernel 内部面

**为什么不走 MCP**:
- MCP tool 跨进程调用开销 + 序列化成本,换不来任何收益(runtime 和 BaseModule 在同一 FastAPI 进程)
- `system.patrol.enable` 的副作用**恰好**是修改同进程 runtime 状态,跨进程反而丢了引用

### 6.1.3 决策 C:`run_auto_reply_cycle`(规则)与 `run_process`(LLM)**共存**,不迁移

**决策**:
- `job_chat.patrol` handler 继续调 `JobChatService.run_process`(LLM-first 分类 + 回复 + 投简历)
- `run_auto_reply_cycle`(rule-based Reader → Decision → Actuator)通过 `auto_reply_cycle` MCP tool / `scripts/smoke_auto_reply.py` 独立消费
- 两条路径共用 DOM Reader(`extract_chat_detail_state`)和 audit log(`boss_mcp_actions.jsonl`),**不**共用决策层

**为什么共存**:

| 维度 | `run_process`(LLM-first) | `run_auto_reply_cycle`(rule) |
|---|---|---|
| 决策依据 | LLM classify(完整会话历史) | 规则树(当前 `ChatDetailState`) |
| 可覆盖形态 | 开放域(包括"具体问题答复") | 合同已采样形态(动作卡 / 纯文字招呼) |
| 成本 | LLM tokens / 延迟 | 纯 DOM |
| 适用场景 | 复杂 / 多轮 | 收敛 / 首次接触 |
| 可观测性 | 走 `pipeline_runs` + LLM trace | 走 `boss_mcp_actions.jsonl` + `AutoReplyDecision.reason` |

迁移决策(谁替代谁 / 是否合并)**交给**未来 ADR-005,基于真实跑一段时间后两路径的业务效果对比。当前不做。

### 6.1.4 决策 D:IntentSpec schema(每条必含 `when_to_use` / `when_not_to_use` — ADR-001 契约 A)

| Intent | 入参 | `mutates` | `risk` | `requires_confirmation` | 对应 runtime API |
|---|---|---|---|---|---|
| `system.patrol.list` | — | F | 0 | F | `runtime.list_patrols()` |
| `system.patrol.status` | `name: str` | F | 0 | F | `runtime.get_patrol_stats(name)` |
| `system.patrol.enable` | `name: str`, `trigger_now: bool = true` | T | 2 | **T** | `runtime.enable_patrol(name)` + (条件)`runtime.run_patrol_once(name)` |
| `system.patrol.disable` | `name: str` | T | 1 | F | `runtime.disable_patrol(name)` |
| `system.patrol.trigger` | `name: str` | T | 2 | **T** | `runtime.run_patrol_once(name)` |

职责边界(`when_not_to_use` 摘录):
- `list` vs `status`:没给 name 用 list,指定 name 用 status
- `enable`(默认 `trigger_now=true`) vs `trigger`:enable 翻 `ScheduleTask.enabled=true` **并**立刻跑一次(编排在 `PatrolControlModule._enable_handler`,非 runtime 内核契约);`trigger` 只跑一次,**不**改 `enabled` 标志。用户说"只跑这一次不要开" → `trigger`;用户说"开启 / 启动 / 托管 / 帮我持续监听" → `enable`
- `enable` + `job.chat.process` 的分流:"长程监听 / 持续处理新消息" → `enable(name="job_chat.patrol")`;"就扫一次当前未读,跑完别再跑" → `job.chat.process`(同步 tool)。`job.chat.process.when_not_to_use` 显式把前一种意图回流给 enable,避免 trace_7a4be7958c5b 类误路由
- `disable` vs `/api/runtime/pause`:前者关单条,后者关所有(升级 / 接管场景)
- `enable` 不绕过业务层 killswitch:`PULSE_BOSS_AUTOREPLY=off` 仍会让 handler return `{status:"disabled"}`;enable 只是把 patrol 放回调度队列,不改业务语义

**`trigger_now` 默认 `true` 的理由**(2026-04-22 M9.2):用户说"开启自动回复"的意思是"启动并让我看到它在工作",不是"仅置位,等一个 interval 再跑"。若默认 `false`,典型 peak_interval=180s 会让用户 UI 侧没有任何反馈,读作"没开起来"。`trigger_now=false` 仅保留给显式"挂起来先别跑"语义。enable 与 trigger 是两次独立 kernel 调用,中间存在极小窗口可能被下一 scheduler tick 抢先(最多多跑一次);业务侧 `JobChatService.run_process` 对 `conversation_id` 已做幂等去重,该风险可接受。

### 6.1.5 事件契约

| 事件名 | 何时发出 | payload |
|---|---|---|
| `runtime.patrol.lifecycle.enabled` | `enable_patrol(name)` 命中已注册 patrol | `{task_name}` |
| `runtime.patrol.lifecycle.disabled` | `disable_patrol(name)` 命中已注册 patrol | `{task_name}` |
| `runtime.patrol.lifecycle.triggered` | `run_patrol_once(name)` 进入 `_execute_patrol` 前 | `{task_name}` |

已有事件(`runtime.patrol.started / completed / degraded / failed / circuit_breaker`)在 `_execute_patrol` 中继续发出,**不重复**。

### 6.1.6 HTTP 对等面

| 路由 | 方法 | 对等 IntentSpec |
|---|---|---|
| `/api/runtime/patrols` | GET | `system.patrol.list` |
| `/api/runtime/patrols/{name}` | GET | `system.patrol.status` |
| `/api/runtime/patrols/{name}/enable` | POST | `system.patrol.enable` |
| `/api/runtime/patrols/{name}/disable` | POST | `system.patrol.disable` |
| `/api/runtime/patrols/{name}/trigger` | POST | `system.patrol.trigger` |

IntentSpec 和 HTTP 路由**语义完全对等**,共享同一底层 `AgentRuntime` API;前者给 LLM tool_use,后者给 CLI / 前端 / 运维脚本。

### 6.1.7 不变量(fail-loud)

1. 内部心跳 `__runtime_heartbeat__` **不可**通过对话面控制(enable / disable / trigger 对它 return `ok=False` + 错误原因),防止 runtime 自锁
2. `enable` / `disable` 对未注册 patrol return `ok=False, error="patrol not found: ..."`,**不**自动创建、**不**吞错
3. `trigger` 仍走 `_execute_patrol` 全流程,熔断开则 L0 skip,不绕过
4. 所有 handler 不吞异常;内部一致性违反 raise `RuntimeError`

### 6.1.8 落地状态(`一次性到位`)

| 项 | 位置 | 状态 |
|---|---|---|
| ADR-004 §6.1 决策 A-D + IntentSpec schema + 事件契约 | 本节 | ✅ 已落地(本 PR) |
| `SchedulerEngine.set_enabled` + `.get_task` | `core/scheduler/engine.py` | ✅ 已落地 |
| `AgentRuntime.enable_patrol / disable_patrol / run_patrol_once / get_patrol_stats / list_patrols` + `runtime.patrol.lifecycle.*` 事件 | `core/runtime.py` | ✅ 已落地 |
| per-patrol HTTP 路由 5 条 | `core/server.py` | ✅ 已落地 |
| `PatrolControlModule` 新模块(5 个 IntentSpec,契约 A 完整) | `modules/system/patrol/module.py` | ✅ 已落地 |
| Scheduler / Runtime / 模块单测 + 契约 test | `tests/pulse/core/`、`tests/pulse/modules/system/` | ✅ 已落地 |
| `smoke_patrol_control.py` 端到端 dry-run | `scripts/smoke_patrol_control.py` | ✅ 已落地 |
| `Pulse-AgentRuntime设计.md` §5.6 + §5.2 / §5.5 行追加 | `docs/Pulse-AgentRuntime设计.md` | ✅ 已落地 |
| `Pulse-DomainMemory与Tool模式.md` §6.3 系统控制类 Intent Tool 分类 | `docs/Pulse-DomainMemory与Tool模式.md` | ✅ 已落地 |
| `Pulse-内核架构总览.md` §5.2 契约行追加 | `docs/Pulse-内核架构总览.md` | ✅ 已落地 |
| `docs/README.md` ADR 清单 + `Pulse实施计划.md` M9 里程碑 | 两文件 | ✅ 已落地 |
| §6.1.1 修订(IM 独占启停 + 删 env killswitch + register 无条件)回归修复 trace_0cf87040e0e5 | `modules/job/config.py`、`modules/job/chat/module.py`、`modules/job/greet/module.py`、`core/runtime.py`(默认 `enabled=False`)、测试 1 条 | ✅ 已落地(2026-04-22 M9.1) |
| §6.1.4 `system.patrol.enable` 增 `trigger_now: bool = true`;`_enable_handler` 编排 enable + 立即 `run_patrol_once`;`job.chat.process.when_not_to_use` 加"长程监听 → `system.patrol.enable`"回流锚点。回归修复 trace_7a4be7958c5b(Brain 把"开启自动回复"误路由到 `job.chat.process`) | `modules/system/patrol/module.py`、`modules/job/chat/module.py`、`tests/pulse/modules/system/test_patrol_control_module.py` 新增 2 条 | ✅ 已落地(2026-04-22 M9.2) |
| §6.1 chat-list 脚手架:`_switch_chat_tab` 改 snapshot-change 判据(pre/post 首行签名对比 + `networkidle` + 3s poll budget,替换原 `wait_for_timeout(700)`);新增 `_resilient_extract_conversations_from_page`(3 次 attempt + `networkidle` + 递增 backoff,与 jobs 侧 `_resilient_extract_jobs_from_page` 对齐);`_pull_conversations_via_browser` 切换到 resilient 变体 + 新增 `pull_conversations_via_browser` 结构化 log 行。回归修复 trace_7a4be7958c5b 的"tab 切换但 list 仍是全部消息 → extract 返回 [] → LLM 说无未读"失败链 | `mcp_servers/_boss_platform_runtime.py`、`tests/pulse/mcp_servers/test_boss_chat_pull_timing.py` 新增 6 条 | ✅ 已落地(2026-04-22 M9.2) |
| §6.1 运行期可观察性:`boss_platform_gateway` 关闭 uvicorn 默认 access log(过滤 `/health` noise),`/call` 端点改手工 `logger.info("mcp_call tool=... status=... elapsed_ms=...")`,保留业务可观察性;`start.sh monitor_loop` 改 state-change-only(首次 baseline + 状态码变化时打印,不再每 20s `[SYS] idle`) | `mcp_servers/boss_platform_gateway.py`、`scripts/start.sh` | ✅ 已落地(2026-04-22 M9.2) |
| §6.1 B3(`fetch_latest_hr` 真逐条点会话抓详情)合同前置:需先在 `docs/dom-specs/boss/chat-detail/` 补一轮 tab-switched 后的 DOM dump 并锁合同,再开代码实现 | — | ⏳ 推迟到 PR-2(ADR-004 §6.1 追加决策 E,本轮范围外) |
| 日志系统专项(trace 聚合工具 + JSONL sidecar + ADR-003 `pipeline_runs / action_reports` 存储) | — | ⏳ 推迟到日志系统专项 ADR,本轮仅占位。旧日志已按开发阶段授权清空 |

---

## 7. 风险与取舍

1. **hint click 误点相似会话**:`_try_click_conversation_by_hint` 用 `get_by_text(value, exact=False).first`,如果 HR 姓名/公司在屏幕上出现两次(例如有两条会话都是"字节跳动"的 HR),会点到错的。mitigation:hint 优先级按 `hr_name + company + job_title` 三元复合搜,且打开后 `ChatDetailState` 的 `hr_company + position_name` 与预期对不上时 cycle 记 `wrong_conversation` 跳过。v1 容忍该假阳率,v2 如果变明显再引入 li 的 DOM index 定位。
2. **"发简历" 点击后的 confirm 浮层形态未采样**:合同 §H 明确"未采样"。v1 先假设无 confirm 或 `.btn-sure` / `text=确认` 兜住;如果真遇到 confirm 结构不同,`_click_send_resume_via_browser` 会 `status="confirm_selector_missing"` fail-loud,下一个 cycle 用户看了 audit 补 dump + 补 selector。不掩盖。
3. **dry_run 的误解**:有人可能以为 dry_run 会"在浏览器上演示",实际上 dry_run **也是真打开真扫**的,只是不点最后那一下按钮;因此 dry_run 仍会让 HR 侧看到"对方在看我的主页"(BOSS 有在线提示)。这点在 smoke 脚本开头的 banner 明示。
4. **规则驱动的假阳性**:HR 发"能看看你的作品集吗?" 走的是"`friend` + `text` + `send_resume_button_present`" 分支 → `CLICK_SEND_RESUME`,但用户可能希望发的是 GitHub 链接而非简历。v1 容忍该误差(发简历在求职场景里不算错,只是不是最精准答复);v2 的 LLM 层可以识别"作品集 ≠ 简历"并出 text reply。
5. **合同漂移**:BOSS 一旦改版(例如把 `.respond-popover` 换成 `.quick-respond`),本 ADR 的 Actuator 全部哑弹。mitigation:Actuator 在点前先跑 `extract_chat_detail_state` 验证 `pending_respond is not None`;如果状态里明明说有 popover 但 selector 点不到,立刻 `status="selector_drift"` 写 audit。这是合同和实现的一致性校验闸。

---

## 附录:与 ADR-001 / ADR-003 的关系

- **ADR-001 三契约**:auto_reply_cycle 作为 tool(契约 A)**有**返回 shape 规范、**有**幂等守卫(§6 P3e 复用)、**有**与 LLM 承诺对齐的 ActionReport(给契约 C verifier 消费)。
- **ADR-003 ActionReport**:cycle 产出 `action="job.auto_reply"` 的 ActionReport;`dry_run=True` 时 `status="preview"` 走 ADR-003 §4.4 的"preview 不算已完成"规则,防止 Brain 把"扫了没点"汇报成"已点"。
- **`docs/dom-specs/boss/chat-detail/README.md` §E 决策树**:本 ADR §4.2 规则是对 §E 的 Python 化机械映射;合同文档改一次,本 ADR §4.2 就得同步改一次,不允许两处漂移。
