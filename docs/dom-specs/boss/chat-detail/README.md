# BOSS 会话详情页 DOM 合同

> SOP: `../../README.md` · 平台: `../README.md`
> 数据基准：[`20260422T073442Z.json`](./20260422T073442Z.json)
> 观察对象：刘女士（字节跳动 AIGC视觉生成实习生，240-260元/天，上海），会话内含 4 条消息（我方 1 / 机器人卡 1 / HR 文字 1 / HR 附件简历卡 1），**恰好**覆盖自动回复四大典型形态。

## 根节点结构

```
.chat-conversation
├── .top-info-content
│   ├── .user-info-wrap > .user-info > .base-info            ← HR 顶部身份
│   └── .chat-position-content > .position-main              ← 岗位摘要 (★真实薪资在这里)
├── .message-content
│   ├── .chat-record > .chat-message > ul.im-list > li.message-item   ← 消息流
│   └── .message-tip-bar > .respond-popover                  ← HR 卡片时的浮动快捷操作栏
└── .message-controls > .chat-im.chat-editor
    ├── .chat-controls                                        ← 底部工具栏 (表情/发简历/换电话/...)
    └── .chat-input + .btn-send                               ← 输入框 + 发送按钮
```

## A. HR 顶部身份

| 字段 | selector | DOM 证据 | 注意 |
|---|---|---|---|
| HR 姓名 | `.base-info .name-content .name-text` | `刘女士` | `.name-text` 是唯一可靠的姓名 class |
| 公司 | JS: `Array.from(node.querySelectorAll('.base-info > span')).filter(s => !s.classList.contains('base-title'))[0]` | `字节跳动` | **无 class**；排除 `.base-title` 后第 0 个 span。注意 `.base-info > span.name-content` 被 `.name-content` 的直接 span 包住——其实直接 `.base-info > span:not(.base-title)` 更直观 |
| HR 身份 title | `.base-info .base-title` | `招聘者` | 比 chat-list 更精确——chat-list 里这个字段是无 class 的 `<span>`，chat-detail 补上了 `.base-title` |
| 在线状态图标 | `.base-info .chat-online-stats` (img) | `img src=.../fdszc4v9eo1672210548439.png` | 在/不在线是 src 变化，不是 class 显隐 |

## B. 岗位摘要（解释了"真实薪资从哪抓"的谜）

| 字段 | selector | DOM 证据 | 注意 |
|---|---|---|---|
| 岗位名 | `.chat-position-content .position-name` | `AIGC视觉生成实习生（Agent方向）` | |
| 薪资（**未脱敏**） | `.chat-position-content .salary` | `240-260元/天` | ★ 这是**真实薪资**。search-list 里的 `.job-salary` 现在会被脱敏成 `-元/天`，greet 路径要从这里再取一次。合同上写死"薪资以 chat-detail 为准，search-list 仅作候选" |
| 城市 | `.chat-position-content .city` | `上海` | |
| "查看职位"按钮 | `.chat-position-content .right-content span` 文本=`查看职位` | — | 点击跳 JD 详情页；自动回复一般不需要 |

## C. 消息流（自动回复决策的主战场）

每条消息是 `ul.im-list > li.message-item`，**发送方靠 li 的 class 组合**区分：

| class | 含义 | 证据 |
|---|---|---|
| `message-item item-myself` | 我方发的 | `<li data-mid="334613440820744" class="message-item item-myself">您好，27 应届硕士...` |
| `message-item item-friend` | 对方（HR 或机器人）发的 | 其他 3 条 |

> `data-mid` 是后端消息 id，去重 / 幂等判断用。

### C.1 文字消息

| 字段 | selector | 证据 | 说明 |
|---|---|---|---|
| 文字内容 | `li.message-item .text p span` | `您好，27 应届硕士，可尽快到岗...` / `你好呀！方便发一份你的简历过来嘛` | 两层嵌套 `p > span` 都是 Vue 习惯，直接取 `innerText` 即可 |
| 时间戳 | `li.message-item .item-time .time` | `12:25` / `13:12` | 可能不是每条都有（连续同一时段的消息折叠不显示 time） |
| 我方送达状态 | `li.item-myself .text .message-status` | `<i class="message-status status-read">已读</i>` | class 变体：`status-read` / `status-delivery`（见 chat-list 合同），HR 消息没有这个节点 |

### C.2 机器人卡片（不是 HR 真人发的，**不要当 HR 发言处理**）

| 特征 | 值 / selector | 证据 |
|---|---|---|
| 根 | `li.item-friend .articles-center .message-card-wrap.blue` | 观察到"你与该职位竞争者PK情况"卡 |
| 颜色 class | `.blue` | ★ **这是机器人卡的判别信号**（HR 卡是 `.boss-green`，见下） |
| **没有头像** | `li > div.message-content` 直接子节点 `.figure` **缺失** | 对比 C.3 的 HR 卡，该 li 里有 `.figure > .image-circle` |
| 卡片标题 | `.message-card-top-title` | `你与该职位竞争者PK情况` |
| 按钮 | `.message-card-buttons .card-btn.one-btn` | `查看详细分析`（单按钮形态，`.one-btn` 表示单按钮） |

> **重要**：本次样本里观察到一个现象——机器人卡的 li 里**压根没有 `.figure` 头像节点**，而真 HR 卡的 li 里**有**。这是另一条辅助判别依据，但颜色 class（`blue` vs `boss-green`）是主信号。

### C.3 HR 附件简历卡（自动回复核心场景）

| 特征 | 值 / selector | 证据 |
|---|---|---|
| 根 | `li.item-friend .message-dialog-both.message-card-wrap.boss-green` | 观察到"我想要一份您的附件简历"卡 |
| 颜色 class | `.boss-green` | ★ **HR 真人动作卡判别信号** |
| 有 HR 头像 | `li > div.message-content > .figure > .image-circle` | 与机器人卡的关键 DOM 差异 |
| 卡片图标类型 | `.message-card-top-wrap .dialog-icon` 的二级 class | `.dialog-icon.resume` （此次是简历请求）；未来其他卡（面试邀请 / 换电话等）可能是 `.dialog-icon.phone` / `.interview` 等，**未采样，需扩充** |
| 卡片标题 | `.message-card-top-title.message-card-top-text` | `我想要一份您的附件简历，您是否同意` |
| 双按钮 | `.message-card-buttons .card-btn` × 2 | `拒绝` / `同意`（按 DOM 顺序：拒绝在前，同意在后） |

### C.4 浮动快捷操作栏（★自动回复应该优先点这里而非 C.3 的内嵌按钮）

**位置**：`.message-content` 外层、在 `.message-tip-bar` 里。BOSS 会在**最新一条 HR 消息是需要响应的动作卡**时，自动在消息区上方冒出一个 `.respond-popover`，里面复制了同样的`[拒绝][同意]`——但**class 更精确**：

| 字段 | selector | 证据 | 为什么优先这里 |
|---|---|---|---|
| 浮动条存在性 | `.message-tip-bar .respond-popover` | 本次存在 | ★ 存在 ⇒ "最后一条 HR 消息是需要回应的卡片"。业务层 strong signal，不需要再去遍历消息流 |
| 对应文案 | `.respond-popover .text` | `我想要一份您的附件简历，您是否同意` | 方便日志记录 Agent 究竟在回应什么 |
| 拒绝按钮 | `.respond-popover .op .btn.btn-refuse` | `拒绝` | class **稳定且独有**，相比 C.3 内嵌按钮的通用 `.card-btn` 好定位 |
| 同意按钮 | `.respond-popover .op .btn.btn-agree` | `同意` | 同上 |

> 判定逻辑："最后一条 HR 消息需不需要 Agent 操作" = `.respond-popover` 节点是否存在。纯文字消息、机器人卡都不会触发这个浮动条。

## D. 底部工具栏（HR 没发卡片时，Agent 主动发简历等）

| 按钮 | 稳定 selector | 证据 | 备注 |
|---|---|---|---|
| 表情 | `.chat-controls .btn-emotion` | `<div class="icon btn-emotion ...">` | 自动回复一般用不上 |
| 常用语 | `.chat-controls .btn-dict` | `<div class="icon btn-dict ...">` | 可能在未来用（预置话术） |
| 发送图片 | `.chat-controls .btn-sendimg` + `input[type=file]` | | 发简历图片用？ |
| **发简历** | ★ **无稳定独立 class**，必须靠文本 | `<div class="toolbar-btn tooltip tooltip-top">发简历</div>` | 推荐 selector: `xpath=//div[contains(@class,'toolbar-btn')][normalize-space(text())='发简历']`；Playwright: `page.get_by_text("发简历", exact=True)` |
| 换电话 | `.chat-controls .btn-contact` | `<div class="... btn-contact toolbar-btn ...">换电话</div>` | 触发后有 `.sentence-popover.panel-contact` 确认弹层（取消/确定） |
| 换微信 | `.chat-controls .btn-weixin` | `<div class="btn-weixin toolbar-btn ...">换微信</div>` | |

### D.2 输入框 + 发送

| 字段 | selector | 证据 |
|---|---|---|
| 输入框 | `#chat-input` 或 `.chat-input[contenteditable=true]` | `<div contenteditable="true" id="chat-input" class="chat-input"></div>` |
| 发送按钮 | `.btn-send` 或 `button[type=send]` | `<button type="send" class="btn-v2 btn-sure-v2 btn-send disabled">发送</button>` |
| 发送按钮禁用状态 | `.btn-send.disabled` 存在性 | 输入框为空时 disabled |
| 输入提示 | `.chat-op .tip` | `按Enter键发送，按Ctrl+Enter键换行` |

## E. 业务决策规则（自动回复的最小充分契约）

从 DOM 层归纳出的"自动回复该做什么"决策树——**纯 DOM 层**，不涉及 LLM 判断：

```
对一条 chat-list 里 .notice-badge 存在的会话:
    点进去 → wait .chat-conversation 渲染 → 检查:

    IF .respond-popover 存在:
        # HR 发了动作卡, BOSS 已经给出快捷按钮
        根据业务策略 click .btn-agree 或 .btn-refuse
        (需要 LLM 决策: 这个卡是关于什么的、该不该同意)

    ELIF 最后一条 .message-item.item-friend.text 是纯文字:
        # HR 发了纯文字, 没有动作卡
        根据文字内容决策:
            - 如果是招呼问候 → 发预置答复 / 主动点"发简历"
            - 如果是具体问题 (薪资/地点/经验) → LLM 答复
            - 如果是机器人卡 (.message-card-wrap.blue) → 忽略

    ELIF 最后一条是我方 (item-myself):
        # 我已经回过了, 无动作
        跳过
```

## F. 已知陷阱

| 陷阱 | 形态 | 防御 |
|---|---|---|
| 机器人卡伪装成 HR 消息 | `.item-friend` 但 `.message-card-wrap.blue` | 用颜色 class 区分；不要把它的文本（"共XX人投递"）当 HR 发言 |
| "发简历"按钮无独立 class | 必须靠 innerText 定位 | 用 Playwright `get_by_text("发简历", exact=True)` 或 xpath 文本谓词；**禁止**用通用 `.toolbar-btn`（会命中"换电话"/"换微信"） |
| 卡片类型识别不全 | 本次只采到"附件简历" + "PK情况"两种 | `.dialog-icon` 二级 class（`.resume` / 等）需要随遇到的新卡类型扩充；合同文档要**每次都回来更新** |
| `.respond-popover.op` 内的按钮 class 稳定，但卡片内嵌的 `.card-btn` 通用 | C.3 卡片里两个按钮都是 `.card-btn`，要靠文本区分；C.4 浮动条里是独有 `.btn-agree` / `.btn-refuse` | **自动回复优先点 C.4 浮动条**，备用 C.3 按内容文本精确定位 |
| 跨 tab 一致性（经验推断） | 用户经验："未读 tab 只是全部 tab 的子集，结构相同" | 合同里已接受此推断。若未来 BOSS 改版让两 tab 结构分叉，scan 就会在"未读"tab 下找不到 item，需要触发补 dump |

## G. 与代码的对应（Reader + Decision + Actuator 全落地）

**Reader（chat-detail 合同首轮落地）**：

- `src/pulse/mcp_servers/_boss_platform_runtime.py::extract_chat_detail_state(page) -> ChatDetailState | None`
- `ChatDetailState` / `ChatDetailLastMessage` / `ChatDetailPendingRespond` 三个 frozen dataclass，字段机械对应合同 §A~D 中"Reader 可靠拿到 + 自动回复会读"的子集
- 字段映射：
  - §A → `hr_name` / `hr_company` / `hr_title`
  - §B → `position_name` / `position_salary` / `position_city`（★ 薪资真实来源）
  - §C → `last_message`（`sender ∈ {me, friend, bot}` / `kind ∈ {text, card}` / `text` / `data_mid`）
  - §C.4 → `pending_respond`（`text` / `has_agree` / `has_refuse`）
  - §D.1 → `send_resume_button_present`（靠文本 `=== "发简历"` 定位）
- 返回 `None` 的唯一条件是 `.chat-conversation` 根节点不存在（页面未开任何会话）；其余 DOM 未暴露的字段一律给空串，**不伪造**

**Decision（ADR-004 Step A.2 已落地）**：

- `decide_auto_reply_action(state: ChatDetailState | None) -> AutoReplyDecision`：合同 §E 规则树的机械 Python 映射，**纯函数，不走 LLM**（v1 简历请求卡 → agree / HR 纯文字 → send_resume / 其他 → skip）
- `AutoReplyDecision` frozen dataclass：`kind ∈ {skip, click_respond_agree, click_respond_refuse, click_send_resume}` + `reason` + `trigger_mid`（= `state.last_message.data_mid`，幂等 key 一部分）
- 决策契约细节 + kill-switch / 重评触发见 `docs/adr/ADR-004-AutoReplyContract.md`

**Actuator（ADR-004 Step A.3 已落地）**：

- `_click_respond_popover_via_browser(page, *, decision, conversation_id)` — 按合同 §C.4 精确 selector `.message-tip-bar .respond-popover .btn.btn-agree|refuse`；点后等 popover 从 DOM 消失作为成功信号
- `_click_send_resume_via_browser(page, *, conversation_id)` — 按合同 §D.1 用 `page.locator(".chat-controls .toolbar").locator("div.toolbar-btn", has_text="发简历").first`；如后继有 `.pop-wrap .btn-sure` / `text=确认` 等 confirm 浮层则再点一下
- 两个函数都**假定调用前已在正确的 chat-detail panel**；不做会话切换（那是编排层 `run_auto_reply_cycle` 的职责）

**Orchestrator（ADR-004 Step A.4 已落地）**：

- `run_auto_reply_cycle(*, max_conversations, chat_tab="未读", dry_run=True, profile_id, run_id)` — 完整 pipeline：拉未读 → 逐条 hint click → wait chat-detail → extract_chat_detail_state → decide → (dry_run 或 执行 + 幂等守卫 + 审计)
- 幂等 key：`{conversation_id, decision_kind, trigger_mid}`，窗口复用 `_idempotency_window_sec()`（默认 300s）
- MCP tool：`boss_platform_server.py::auto_reply_cycle`

**测试**：

- 纯函数守卫：`tests/pulse/mcp_servers/test_boss_autoreply_decision.py` — 10 条 case 按合同 §C / §H 的真实 dump 样本重建 `ChatDetailState` 驱动，每条钉死一个决策分支
- Reader / Actuator / Orchestrator 的整段"打开真实 chat-detail → extract → decide → click → popover 消失"留给 `scripts/smoke_auto_reply.py` 做真实 trace 回放（宪法 §测试分层#3），不在 CI 跑

## H. 下一次 dump 应该补什么

本轮合同里所有"未采样" / "需扩充"条目：

- [ ] 其他类型的 HR 动作卡：面试邀请 / 换电话发起 / 换微信发起（看 `.dialog-icon` 二级 class 还有什么）
- [ ] 跨天消息的时间戳形态（`昨天`、`MM-DD`、`YYYY-MM-DD`）
- [ ] "未读 tab" 下 `.chat-conversation` 是否真的和"全部 tab" 相同（一次简单跨 tab 跑同一个会话验证即可）
- [ ] HR 发多媒体消息（图片 / 语音 / 文件）的 li.message-item 结构——未来可能需要忽略或转发
- [ ] "发简历" 点击后的 confirm 弹层（如果存在）的结构
