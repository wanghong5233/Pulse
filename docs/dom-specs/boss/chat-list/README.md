# BOSS 会话列表页 DOM 合同

> SOP: `../../README.md` · 平台: `../README.md`
> 数据基准：[`20260422T072555Z.json`](./20260422T072555Z.json)（6 条真实会话，landed URL `/web/geek/chat`，观察 tab = "全部"）

## 外层与列表

| 用途 | selector | 说明 |
|---|---|---|
| 列表容器（wait 用） | `.user-list` | 脚本 `wait_selectors` 命中此项即判渲染就绪 |
| 会话 item | `.user-list li` | 真实 DOM 里 `<li role="listitem">` 嵌在 `.user-list` 下的某个 wrapper 里（非 direct child）；必须用后代选择器。本次 hit_selector 与 trace_24ecd22aa795 diagnose 双向印证 (`> li` 计 0 / `  li` 计 2 ⟷ "未读(2)") |

> ⚠️ **跨 tab 未验证**：本次 dump 在"全部" tab 下采集；"未读"/"新招呼"/"更多" tab 切换后 item class 是否一致 **尚未验证**，改版前要补一轮 tab 切换后的 dump。
>
> **运行时补偿**（ADR-004 §6.1 M9.2）：由于 BOSS tab 切换是客户端过滤且无稳定 "active tab" 合同锚点，`_switch_chat_tab` 采用 **snapshot-change 判据**——click 前后分别抓首行签名（HR 姓名 + 公司 + notice-badge innerText + time,由 `_chat_list_first_row_signature` 计算）,在 `PULSE_BOSS_CHAT_TAB_WAIT_MS`(默认 3000ms)内轮询签名变化或 `networkidle`,两者任一达成即判切换完成。签名一直不变 → 视为"本来就在目标 tab",交由下游 `_resilient_extract_conversations_from_page`(3 次 attempt + 递增 backoff + `networkidle`)兜底,不 fail-loud、不伪造。这套策略不改本合同字段定义,仅把"DOM 是否就绪"从硬编码时延改成内容可观测信号,在补 cross-tab dump 之前先消除 trace_7a4be7958c5b 类"tab 切换但 list 是全部消息"的时序竞态。

## 字段合同

| 字段 | selector | DOM 证据 | 注意 |
|---|---|---|---|
| 未读红点（存在即未读） | `li .figure .notice-badge` | #1 周晟业存在 (`"2"`)、#3 胡女士存在 (`"1"`)；#2/#4/#5/#6 **缺失此节点** | **节点存在性本身就是未读信号**，不是 class 显隐。托管自动回复的触发条件 = `notice-badge` 节点存在 |
| 未读数字 | `li .figure .notice-badge` 的 `innerText` | `"2"` / `"1"` | 超过 99 的话 BOSS 通常写 `"99+"`，本次样本未出现；做计数要支持字符串 |
| HR 姓名 | `li .name-box > .name-text` | `周晟业`/`罗雨洁`/`胡女士`/`刘女士`/`杨女士`/`冯雅暄` | **唯一有 class 的姓名节点**。稳定锚点 |
| HR 公司 | JS: `Array.from(node.querySelectorAll('.name-box > span')).filter(s => !s.classList.contains('name-text'))[0]` | `阿里巴巴集团`/`字节跳动`/`赞意`/`字节跳动`/`维智`/`字节跳动` | ★ **公司与岗位的 span 都没有任何 class**。只能靠"排除 `.name-text` 后的 DOM 顺序"取。注意 `.name-box` 里还有 `<i class="vline">` 分隔符，查 `> span` 已经排除它 |
| HR 岗位 | JS: 同上 filter 后 `[1]` | `后端开发工程师`/`招聘HR`/`招聘者`/`招聘者`/`人力资源主管`/`HR` | 同上 |
| 时间戳 | `li .time` | `14:36` / `13:45` / `13:36` / `13:12` / `00:47` / `00:44` | ⚠️ 本次样本全是今天的 `HH:MM`；跨天会变成 `昨天`/`MM-DD`/其他形态，**未验证** |
| 最后消息预览 | `li .last-msg-text` | `您正在与Boss周晟业沟通`/`您好，27应届硕士...`/等 | 占位文本 `您正在与Boss<HR姓名>沟通` 是 BOSS 的系统提示，不是真实消息内容，下游要识别这种 pattern |
| 我方消息送达状态 | `li .message-status.status-read` / `.message-status.status-delivery` | #2 罗雨洁 `status-read`（我回的消息已读）、#5 杨女士 `status-delivery`（我发的还没读）、#6 冯雅暄 `status-read` | **节点存在即说明最后一条是我方发的**（HR 发给我的不会有这个 `<i>`）。`notice-badge` 存在性 + 此节点缺失 = "HR 有新消息未读" |
| 头像 URL | `li .image-circle` 的 `src` | CDN 链接 | 默认头像路径 `boss/avatar/avatar_N.png`，自定义头像路径 `beijin/upload/avatar/...` |

## 业务规则（供托管自动回复消费）

"需要触发自动回复"的 item 判定——从真实 DOM 归纳的最小充分条件：

```
li.has(.figure .notice-badge)
```

**只用一个条件就够**。原因：`notice-badge` 节点存在即代表"对方发了我还没读"。本次 6 条样本中：

| # | HR | 有 notice-badge? | 最后 msg 状态 | 是否需要自动回复 |
|---|---|---|---|---|
| 1 | 周晟业 / 阿里巴巴集团 | ✓ (2) | — | **需要** |
| 2 | 罗雨洁 / 字节跳动 | ✗ | 我方[已读] | 不需要 |
| 3 | 胡女士 / 赞意 | ✓ (1) | — | **需要** |
| 4 | 刘女士 / 字节跳动 | ✗ | — | 不需要（最后是系统提示） |
| 5 | 杨女士 / 维智 | ✗ | 我方[送达] | 不需要 |
| 6 | 冯雅暄 / 字节跳动 | ✗ | 我方[已读] | 不需要 |

2 + 1 = **3 条未读** ⟷ 用户图 1 左上角 tab "未读(3)"。合同闭环。

## 已知陷阱

| 陷阱 | 形态 | 防御 |
|---|---|---|
| 公司/岗位**无 class** | 只能靠 DOM 位置，`[class*='company']` / `.company` / `.sub-title` 全部不命中 | selector 禁止再靠 class 猜；用"排除 `.name-text` 后的 span 顺序" |
| 最后消息是系统占位 `您正在与Boss<X>沟通` | 这不是真实对话内容，是 BOSS 给新会话/空会话的默认文案 | 下游 matcher 不能把这个当"HR 说了什么" |
| 跨 tab / 跨天形态未采 | 只 dump 了"全部" tab 当天时间段 | 等下次真实遇到"昨天"/未读 tab 需求时补 dump |

## 与代码/测试的对应

- **代码**：`src/pulse/mcp_servers/_boss_platform_runtime.py::_extract_conversations_from_page`(单次抽取) + `_resilient_extract_conversations_from_page`(M9.2 新增,3 次 attempt 包装器) + `_switch_chat_tab` / `_chat_list_first_row_signature`(M9.2 snapshot-based tab wait)
- **状态**：✅ 已按本合同重写（本轮 C-路径 Reader 落地）。关键修复点：
  - selector 由 `.company,.company-name,.sub-title,[class*='company']` 等**全部哑弹链**换成"排除 `.name-text` 后的 `.name-box > span` 顺序"
  - 删除下游 `"Unknown HR"` / `"Unknown"` / `"Unknown Job"` / `"刚刚"` 等占位伪造（宪法 §类型A），改为空字符串向上抛；`pull_conversations` 消费端 L2529 的 4-field 非空守卫得以重新生效
  - 新增 `my_last_sent_status` 字段（`status-read` / `status-delivery` / `""`），供下游决策"HR 有没有读我的上一条消息"使用
  - **M9.2**:`_switch_chat_tab` 换 snapshot-change 判据 + `_resilient_extract_conversations_from_page` 3 次 attempt(对齐 jobs 侧 `_resilient_extract_jobs_from_page`)修复 tab 切换后 DOM 异步回填期间被单次抽取拿到 [] 的 trace_7a4be7958c5b 类时序问题
- **Actuator(未落地,ADR-004)**:点击第 N 条会话进入 chat-detail 这类交互属 Actuator 层,本轮不做(fetch_latest_hr 真逐条点会话延后到 PR-2,依赖 `docs/dom-specs/boss/chat-detail/` tab-switched 后的补 dump)
- **测试**：`tests/pulse/mcp_servers/test_boss_chat_pull_timing.py`(M9.2 新增 6 条)用 FakePage 驱动 `_resilient_extract_conversations_from_page` + `_chat_list_first_row_signature`,守"tab 切后单次抽空不伪造空结果"与"持久空合约仍返回 fail-loud 空列表"两条不变量
