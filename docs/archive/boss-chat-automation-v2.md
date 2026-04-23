# BOSS 直聘自动对话方案 V2

> 日期：2026-03-17  
> 状态：**实施中**  
> 前置文档：[browser-agent-architecture-decision.md](./browser-agent-architecture-decision.md)  
> 核心变更：从「保守 Copilot」升级为「销售式 Agentic 自动化」

---

## 一、社区调研结论

### 1.1 调研范围

| 层次 | 代表项目 | 规模 | 技术栈 |
|------|---------|------|--------|
| 油猴脚本 | Boss Batch Push, JobPilot 海投助手, AI 工作猎手 | 6259+ 安装 | JS, DOM 操作, Protobuf WebSocket |
| Python RPA | auto_job__find__chatgpt__rpa | 1564 star | Selenium + ChatGPT + FAISS |
| Playwright 方案 | auto-zhipin (ufownl) | 13 star | Playwright + LLM 匹配 |
| Chrome 插件 | boss-auto, boss_copilot | 2~20 star | TypeScript Chrome Extension |
| 通用 Agentic 框架 | browser-use | 81k star | Python, LangChain, Playwright |
| 销售 Agentic 参考 | LangChain GTM Agent, Apollo.io AI | 商业级 | LangGraph + HITL |

### 1.2 关键结论

1. **所有成功的 BOSS 自动化项目都使用确定性 DOM 操作 + LLM 决策层**，无一采用视觉 LLM 驱动浏览器。
2. **简历发送不需要上传文件**——BOSS 平台已有附件简历，只需 DOM 操作点击「发简历」按钮 → 选择简历 → 确认即可。
3. **成熟项目的三阶段模式**：打招呼 → HR 回复后自动发简历 → 持续对话。
4. **LangChain GTM Agent 模式**：新线索 → 检查历史避免重复 → 上下文收集 → 个性化文案 → HITL 审批，转化率 +250%。

### 1.3 BOSS 简历发送机制（从 JobPilot 源码逆向）

```
1. 找到「发简历」按钮: .toolbar-btn (textContent === '发简历')
2. 检查可用性: button.classList.contains('unable') → HR 未回复，不可发送
3. 点击打开简历选择面板
4. 等待简历列表: ul.resume-list
5. 选择简历: li.list-item (按岗位匹配或选第一个)
6. 点击确认: button.btn-v2.btn-sure-v2.btn-confirm
```

---

## 二、V1 → V2 问题分析

### V1 存在的问题

| 问题 | 严重程度 | 根因 |
|------|---------|------|
| `reply_count_for_hr` 始终传 0，回复上限形同虚设 | 高 | `_decision_node` 硬编码 0 |
| 简历发送只生成文字"已附上简历"，无实际 DOM 操作 | 高 | `_try_send_message` 只支持文本 |
| `send_resume` action 被排除在自动执行之外 | 高 | `to_send` 过滤条件写死 `reply_from_profile` |
| HR 主动联系首轮只发招呼，不发简历 | 中 | `needs_send_resume = False` 硬编码 |
| 每次心跳间隔 10 分钟，响应延迟过高 | 中 | cron 调度粒度 |
| `express_interest` 时保守转人工，不主动回复 | 低 | V1 Copilot 策略设计 |

### V2 核心原则

> **像销售一样主动、快速、持续跟进。Agent 能回复就回复，不能回复就飞书通知人。没有人为的回复次数上限。**

---

## 三、巡检调度策略

### 设计原则

巡检（查看是否有新消息并回复）和主动打招呼是**两个独立的调度任务**，时间范围和频率不同。

### 推荐 cron 配置（工作日）

| 任务 | 时间段 | 频率 | 说明 |
|------|--------|------|------|
| 高频巡检 | 10:00-12:00, 14:00-18:00 | 每 3 分钟 | 主动打招呼时段，HR 活跃，预期回复快 |
| 普通巡检 | 08:00-10:00, 12:00-14:00, 18:00-22:00 | 每 10 分钟 | 非高峰但仍可能有消息 |
| 主动打招呼 | 10:00-12:00, 14:00-17:00 | 每 15 分钟 | 涓流式：每批 3 个 HR，模拟真人节奏 |

### 推荐 cron 配置（周末）

| 任务 | 时间段 | 频率 | 说明 |
|------|--------|------|------|
| 低频巡检 | 09:00-21:00 | 每 30 分钟 | 周末 HR 活跃度低 |
| 主动打招呼 | **不执行** | - | 周末打招呼效果差且容易被标记 |

### 主动打招呼策略（涓流式）

**核心原则：像真人一样——刷一会儿、找几个感兴趣的、打个招呼、过一会儿再来。**

| 参数 | 推荐值 | 环境变量 | 说明 |
|------|--------|---------|------|
| 每批打招呼数量 | 3 | `BOSS_GREET_BATCH_SIZE` | 每次触发搜索并打招呼的 HR 数量 |
| 单个招呼间延迟 | 30-90秒 | `BOSS_GREET_DELAY_MIN/MAX_MS` | 模拟阅读JD→思考→打招呼的过程 |
| 上午时段总量 | ~24 | 计算值 | 10:00-12:00, 每15分钟1批, 8批×3 |
| 下午时段总量 | ~36 | 计算值 | 14:00-17:00, 每15分钟1批, 12批×3 |
| 日上限 | 50 | `BOSS_GREET_DAILY_LIMIT` | 硬上限，超过当天不再打招呼 |
| JD 匹配阈值 | 60 | `BOSS_GREET_MATCH_THRESHOLD` | match_score >= 60 才打招呼 |

**为什么涓流式（每 15 分钟 3 个）而不是集中式（一次 20 个）：**
1. 反爬安全：短时间大量打招呼是典型机器人行为模式
2. 风险控制：每批只 3 个，发现异常可立即停止
3. 回复管理：打招呼后 HR 回复分散到来，巡检（每 3 分钟）可及时响应
4. 质量优化：可根据上一批匹配效果动态调整搜索关键词

**与巡检的交替时序（上午 10:00-12:00 示例）：**

```
10:00  巡检（查新消息，回复 HR）
10:03  巡检
10:06  巡检
10:10  打招呼（搜索 → JD匹配 → 3个HR打招呼）← 每个间隔30-90秒
10:12  巡检
10:15  巡检
10:18  巡检
10:25  打招呼（再搜索3个）
10:27  巡检
...以此类推
```

### cron 表达式示例

```bash
# ─── 巡检（查看新消息 + 自动回复） ───

# 工作日高频巡检（周一到周五，10-12/14-18 每3分钟）
*/3 10-11 * * 1-5  curl -s -X POST http://localhost:8000/api/boss/chat/heartbeat/trigger
*/3 14-17 * * 1-5  curl -s -X POST http://localhost:8000/api/boss/chat/heartbeat/trigger

# 工作日普通巡检（周一到周五，8-10/12-14/18-22 每10分钟）
*/10 8-9 * * 1-5   curl -s -X POST http://localhost:8000/api/boss/chat/heartbeat/trigger
*/10 12-13 * * 1-5  curl -s -X POST http://localhost:8000/api/boss/chat/heartbeat/trigger
*/10 18-21 * * 1-5  curl -s -X POST http://localhost:8000/api/boss/chat/heartbeat/trigger

# 周末低频巡检（每30分钟）
*/30 9-20 * * 0,6   curl -s -X POST http://localhost:8000/api/boss/chat/heartbeat/trigger

# ─── 主动打招呼（仅工作日，涓流式） ───

# 上午场（10:00-12:00 每15分钟，每批3个）
*/15 10-11 * * 1-5  curl -s -X POST http://localhost:8000/api/boss/greet/trigger
# 下午场（14:00-17:00 每15分钟，每批3个）
*/15 14-16 * * 1-5  curl -s -X POST http://localhost:8000/api/boss/greet/trigger
```

---

## 四、V2 目标流程

### 路径 A：HR 主动打招呼（Inbound）

```
心跳触发 → 打开 BOSS 聊天页 → 点击"未读"标签
  → 拉取未读会话列表
  → 对每个未读会话：
      → 按 hr_name 精确点击 .friend-content 打开对话
      → 提取消息 (.im-list > li.message-item, item-myself/item-friend)
      → has_candidate_messages=False? → HR 首轮联系
      → LLM JD 匹配度（基于 profile + notes 战略目标）
      → 不匹配 → ignore
      → 匹配 → LLM 生成回复 + 标记 needs_send_resume
      → 后续 HR 回复 → 检测简历是否已发 → 未发则自动发简历
      → 持续对话：LLM 意图分类 + 画像回复（无上限）
      → Agent 信息不足 → 飞书通知用户
  → 切回"全部"标签
```

### 路径 B：Agent 主动打招呼（Outbound）

```
定时搜索岗位（已有 scan 功能）
  → LLM JD 匹配
  → 匹配 → 打招呼（DOM 点击）
  → 等待 HR 回复
  → HR 回复后 → 自动发送简历
  → 后续对话（同路径 A）
```

### 对话状态机

```
                     ┌──────────┐
          打招呼发出  │ greeted  │
                     └────┬─────┘
                          │ HR 回复
                     ┌────▼─────┐
         自动发简历  │ resume   │
                     │ _sent    │
                     └────┬─────┘
                          │ 继续对话
                     ┌────▼─────┐
         持续 AI 回复 │ chatting │ ←→ LLM 回复 / 飞书通知
                     └────┬─────┘
                          │ HR 拒绝或沉默
                     ┌────▼─────┐
                     │  closed  │
                     └──────────┘
```

---

## 五、V2 代码改动清单

### 4.1 `boss_scan.py` — 新增 `_try_send_resume()`

Playwright 实现点击「发简历」按钮的完整流程：
- 找到 `.toolbar-btn` 文本为「发简历」
- 检查 `.unable` 状态
- 点击 → 等待 `ul.resume-list` → 选第一个 `li.list-item` → 点击确认

### 4.2 `boss_chat_service.py` — 移除人为上限

- 删除 `_max_auto_replies_per_hr()` 函数
- 删除 `preview_boss_chat_reply()` 中 `reply_count_for_hr >= max_replies` 的拦截逻辑
- `send_resume` action 不再生成虚假文本，而是标记 `needs_send_resume=True`

### 4.3 `boss_chat_workflow.py` — 打通自动执行

- `_decision_node`: 删除 `reply_count_for_hr=0` 的传参
- `auto_execute` 逻辑：将 `send_resume` 纳入自动执行范围
- `send_resume` 调用 `_try_send_resume()` 而非文本发送
- HR 主动联系且匹配：首轮发招呼 + 标记待发简历

---

## 六、安全与风控

| 措施 | 说明 |
|------|------|
| 随机延迟 | 所有 DOM 操作间保持随机延迟 (BOSS_ACTION_DELAY_MIN/MAX_MS) |
| 来源匹配拦截 | source_fit_passed=False 的会话不自动回复 |
| JD 匹配拦截 | proactive_match_passed=False 的 HR 主动联系不跟进 |
| 置信度阈值 | 意图分类 confidence < 0.7 时转人工 |
| reject 硬拦截 | HR 拒绝一律 ignore |
| escalate_topics | 技术问题/薪资谈判等强制转人工 |
| 发简历前置条件 | `.unable` 状态检查：HR 未回复时不强行发简历 |

---

## 七、面试要点补充

### 问题："你的求职 Agent 如何处理多轮对话？"

核心论点：
1. **状态机驱动**：每个 HR 会话有明确的生命周期（greeted → resume_sent → chatting → closed），状态持久化在数据库
2. **LLM 仅做决策，不做执行**：意图分类和回复生成用 LLM，DOM 操作用 Playwright 确定性执行
3. **Human-in-the-Loop**：Agent 信息不足时通过飞书通知用户，参考 LangChain GTM Agent 模式
4. **无人为上限**：类比销售场景，HR 问什么就回什么，直到对话自然结束或需要人工介入

### 问题："为什么不限制自动回复次数？"

参考 LangChain GTM Agent 的经验：限制回复次数是把「系统的不确定性风险」转嫁为「用户体验损失」。正确做法是：
1. 用 escalate_topics 白名单机制保证安全边界
2. 用置信度阈值过滤低质量回复
3. 用来源/JD 匹配在对话开始前就过滤掉不匹配的机会
4. 在这三层保护下，对话本身不需要人为上限

---

## 八、V2.1 补丁：审计修复记录（2026-03-17）

### 7.1 发现的漏洞

| # | 问题 | 严重度 | 根因 |
|---|------|--------|------|
| 1 | `express_interest` 回复 100% 失败回退为 notify_user | P0 | `_resolve_profile_answer()` 无对应分支 → 已删除整个模板体系 |
| 2 | 回复使用模板填充而非 LLM，不够 Agentic | P0 | 已彻底重构：删除 `_build_profile_reply` + `_resolve_profile_answer`，统一用 `generate_reply()` LLM 生成 |
| 3 | `classify_hr_message` prompt 缺少 6 个意图标签 | P1 | `ask_contact/ask_skills/ask_experience/ask_status/ask_overtime/ask_english` 未列入 system prompt |
| 4 | `_estimate_source_fit` 未传入 `notes`（核心战略目标） | P1 | prompt 只有 `target_positions + work_cities`，缺少求职意图上下文 |
| 5 | 心跳间隔 10 分钟，无实时消息监听 | P1 | 架构限制，cron 调度粒度 |

### 7.2 修复方案

1. **删除全部模板代码**：移除 `_build_profile_reply()`、`_resolve_profile_answer()`、`_first_non_empty()`
2. **新增 `generate_reply()` 作为唯一回复生成函数**：所有回复（reply_from_profile / send_resume）统一由 LLM 基于完整 profile 上下文生成。`_build_profile_text()` 将 profile 序列化为结构化文本供 LLM 使用。LLM 判断无法回复时才转人工
3. **`classify_hr_message` prompt 补全所有 15 个意图标签**：每个标签附带示例描述，确保 LLM 能准确分类
4. **`_estimate_source_fit` prompt 加入 `notes` 字段**：并在 system prompt 中强调 notes 是核心求职意图，地点权重降低
5. **监听频率优化方向（未来）**：
   - 短期：将 cron 间隔缩短至 3-5 分钟（`*/3` or `*/5`）
   - 中期：利用 BOSS WebSocket 长连接（Protobuf 编码）实时监听新消息事件
   - 长期：Chrome Extension 注入 MutationObserver 监听 DOM 变化，通过 native messaging 通知后端

### 7.3 回复生成架构（V2.1 最终版 — 全 LLM 驱动）

**设计原则：这是大模型应用项目，所有回复由 LLM 生成，不使用任何模板。**

已删除的旧代码：`_build_profile_reply()`（模板填充）、`_resolve_profile_answer()`（字段提取）、`_first_non_empty()`

```
HR 消息 → classify_hr_message() [LLM 意图分类，15 个标签]
       → _plan_hr_reply() [LLM 策略规划]
       → action = reply_from_profile / send_resume?
           → generate_reply() [LLM 生成自然语言回复]
               输入：HR 消息 + _build_profile_text(profile) 完整序列化
               输出：can_reply + reply_text
               ├── can_reply=true → 使用 LLM 生成的回复
               └── can_reply=false → notify_user（飞书通知用户人工处理）
```

为什么全用 LLM 而不用模板：
1. 求职对话短小（1-3句），token 消耗极低
2. LLM 能自然组合多个 profile 字段回答复合问题
3. 模板无法处理非标准问法，LLM 理解语义
4. 这是 Agent 项目的核心价值——智能回复，不是规则引擎

### 8.3 V2.2 补丁（2026-03-17 第二轮审计）

| # | 问题 | 修复 |
|---|------|------|
| 1 | 巡检时段固定 `*/10 10-12,15-17`，范围太窄且不区分频率 | 设计分时段分频率调度：高频(3min)/普通(10min)/周末(30min) |
| 2 | 首次HR联系靠LLM判断，浪费调用且不准确 | 结构化判断：`has_candidate_messages=False` 即为首次联系 |
| 3 | 只提取最后一条HR消息，丢失多条消息上下文 | 新增 `_extract_conversation_messages()` 提取完整对话 + `pending_hr_texts`（待回复消息列表） |
| 4 | 回复生成无对话历史，无法保持上下文连贯 | `generate_reply()` 接收完整 `conversation_messages`，格式化后输入 LLM |
| 5 | 状态日志缺少对话结构信息 | 日志增加 `conv_state/msg_count/pending_hr/has_candidate_msgs` |

消息提取架构变更：

```
旧: _extract_latest_hr_message() → 只返回 (latest_hr_message, latest_hr_time)

新: _extract_conversation_messages() → 返回完整结构:
  {
    messages: [{role:"hr"|"self"|"unknown", text, time, mid}, ...],  ← 完整对话
    has_candidate_messages: bool,           ← 结构化首次联系判断
    latest_hr_message: str,                 ← 兼容旧逻辑 + 去重签名
    latest_hr_time: str,
    pending_hr_texts: [str, ...],           ← 最后一条self之后的所有HR消息
  }

DOM 选择器（V2.4 实测确认）：
  消息列表: .im-list > li.message-item
  角色判断: item-myself → self, item-friend → hr
  文本提取: .message-content .text p span（跳过 .message-status）
  卡片消息: .message-content .message-card
  时间戳:   .item-time .time
  消息ID:   li[data-mid]
```

LLM 回复生成输入变更：

```
旧: generate_reply(hr_message=最后一条HR消息)
新: generate_reply(
      hr_message=pending_hr_texts合并,     ← 所有待回复消息
      conversation_messages=完整对话历史,   ← LLM可参考上下文
    )
```

---

## 十、环境变量配置汇总

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `BOSS_CHAT_AUTO_EXECUTE_ENABLED` | `true` | Agent 自动发送消息的总开关（设 false 可全局禁止） |
| `BOSS_GREET_BATCH_SIZE` | `3` | 每次打招呼批次数量（1-10） |
| `BOSS_GREET_DELAY_MIN_MS` | `30000` | 单个招呼间最小延迟（ms） |
| `BOSS_GREET_DELAY_MAX_MS` | `90000` | 单个招呼间最大延迟（ms） |
| `BOSS_GREET_DAILY_LIMIT` | `50` | 每日打招呼硬上限 |
| `BOSS_GREET_MATCH_THRESHOLD` | `60` | JD 匹配分阈值（≥ 此分数才打招呼） |
| `BOSS_ACTION_DELAY_MIN_MS` | `3000` | 页面操作最小延迟（ms） |
| `BOSS_ACTION_DELAY_MAX_MS` | `5000` | 页面操作最大延迟（ms） |
| `BOSS_HEADLESS` | `false` | 是否无头模式运行浏览器 |
| `BOSS_ENABLE_STEALTH` | `true` | 是否启用反检测 |
| `BOSS_FETCH_DETAIL` | `false` | 是否抓取职位详情页 |

### API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/boss/greet/trigger` | POST | 涓流式主动打招呼，由 cron 调用 |
| `/api/boss/chat/heartbeat/trigger` | POST | 巡检触发，默认 `chat_tab="未读"`，查看未读消息并自动回复 |
| `/api/boss/scan` | POST | 搜索+JD分析（不打招呼） |
| `/api/boss/chat/pull` | POST | 拉取聊天对话列表，支持 `chat_tab` 参数（全部/未读/新招呼） |

---

## 十一、V2.3 补丁：全面审计修复记录

> 日期：2026-03-14

### P0 修复

| # | 问题 | 修复方案 | 涉及文件 |
|---|------|---------|----------|
| 1 | heartbeat `auto_execute` 默认 `False`，cron 调用不自动发送 | `BossChatHeartbeatTriggerRequest.auto_execute` 默认改 `True`；`BOSS_CHAT_AUTO_EXECUTE_ENABLED` 默认改 `true` | `schemas.py`, `boss_chat_workflow.py` |
| 2 | 主动打招呼后 HR 回复，Agent 不会自动发简历 | `_decision_node` 新增 `_resume_already_sent()` 检测：回复时如对话中尚未发送简历，自动附加 `needs_send_resume=True`；`_try_send_resume` 已有按钮可用性检查作为安全兜底 | `boss_chat_workflow.py` |
| 3 | `execute_boss_chat_replies` 用索引 `nth(idx)` 点击，发送后列表重排导致消息发到错误对话 | 新增 `_click_conversation_by_id()`：V2.3 设计按 `d-c` 属性匹配（**实测不可行，d-c 是用户 ID，见 V2.4 补丁**），V2.4 改为按 `hr_name` 文本精确匹配并点击 `.friend-content`；若匹配失败跳过 | `boss_scan.py` |

### P1 修复

| # | 问题 | 修复方案 | 涉及文件 |
|---|------|---------|----------|
| 4 | 首轮 HR 主动联系且匹配时，`decision.action=reply_from_profile` 不会触发发简历 | 扩展条件 `{"notify_user", "ignore"}` → `{"notify_user", "ignore", "reply_from_profile"}`；并保留 LLM 生成的 reply_text | `boss_chat_workflow.py` |
| 5 | `unknown` 角色消息被计入 `pending_hr_texts`，可能将系统消息或误分类的自己消息当作 HR 待回复 | `_extract_conversation_messages` JS 中 `pending_hr_texts` 仅收录 `role==="hr"`；`latestHrMessage` 也不再 fallback 到 `unknown` | `boss_scan.py` |
| 6 | 日打招呼计数存内存 `_greet_state`，重启清零 | 改为从 DB `actions` 表查询 `SELECT COUNT(*) WHERE action_type='boss_greet' AND status='success' AND created_at >= CURRENT_DATE`；移除内存计数器 | `boss_scan.py` |
| 7 | `_enrich_latest_hr_messages` 也用 `nth(idx)` 索引点击，有漂移风险 | 优先用 `_click_conversation_by_id()` 按 ID 定位，仅 ID 失败时回退索引 | `boss_scan.py` |

### P2 修复

| # | 问题 | 修复方案 | 涉及文件 |
|---|------|---------|----------|
| 8 | `matched_details.success` 用 `i < greeted` 近似判断，不反映每个实际结果 | 使用 `greet_results` 列表逐条记录 `(item, score, success)`，返回时按真实结果填充 | `boss_scan.py` |

### 核心设计：简历发送时机

```
我方主动打招呼  →  HR回复  →  Agent自动回复 + 自动发简历
HR主动联系     →  JD匹配通过  →  Agent回复 + 发简历
后续对话       →  若简历已发送  →  正常LLM对话（不重复发简历）
```

`_resume_already_sent()` 通过扫描对话历史中的"简历"/"附件"关键词判断是否已发。
`_try_send_resume()` 的按钮 `.unable` 检查是最终安全兜底（BOSS 未开放时按钮灰色）。

### 核心设计：精确会话定位

```
旧方案（有风险）: nth(idx) → 发消息 → 列表重排 → 下一个 nth 错位
V2.3 方案（失败）: _click_conversation_by_id(cid) → 按 d-c 属性匹配（实测 d-c 是用户 ID，所有会话共享）
V2.4 方案（可用）: 用 hr_name 生成唯一 ID → 按 .name-box 文本精确匹配 → 点击 .friend-content
                   若匹配失败 → 跳过（不发送），emit WARNING 日志
```

---

## 十三、V2.4 补丁：实战测试修复记录

> 日期：2026-03-17  
> 触发：分阶段只读测试验证全链路，发现 3 个 P0 级实战 bug

### 关键发现：BOSS 直聘 Web 端 DOM 真实结构

通过实际诊断脚本逐层分析 BOSS 直聘 Web 端 DOM，得到以下**事实**（非假设）：

| 组件 | 选择器 | 说明 |
|------|--------|------|
| 会话列表 | `li[role="listitem"]` | 左侧 HR 列表 |
| 会话点击触发 | `.friend-content` | **必须点击此元素**，点击 `li` 不触发 Vue 事件 |
| HR 名称 | `.name-box .name` | 含 HR 姓名+公司+职位 |
| `d-c` 属性 | `.friend-content[d-c]` | **是登录用户自己的 ID（如 62001），所有会话相同，不可用作 conversation_id** |
| 右侧对话面板 | `.chat-conversation` | 选中 HR 后出现；未选中时显示 `.chat-no-data` |
| 对话面板子结构 | `.top-info-content` + `.message-content` + `.message-controls` | |
| 消息列表 | `.chat-record .chat-message .im-list` | `<ul>` 包含所有消息 `<li>` |
| 单条消息 | `li.message-item` | `data-mid` 属性为消息唯一 ID |
| 自己的消息 | `li.message-item.item-myself` | class 含 `item-myself` |
| HR 的消息 | `li.message-item.item-friend` | class 含 `item-friend` |
| 消息文本 | `.message-content .text p span` | 纯文本在 `<span>` 中 |
| 消息状态 | `.message-status` | "已读"/"送达" 标签，在 `.text` 内但需跳过 |
| 时间戳 | `.item-time .time` | 如 "昨天 22:00" |
| 卡片消息 | `.message-content .message-card` | 面试邀请、微信交换等 |
| 过滤标签栏 | `.label-list li` | "全部"/"未读"/"新招呼"/"更多" |

### P0 修复

| # | 问题 | 根因 | 修复方案 | 涉及文件 |
|---|------|------|---------|----------|
| 1 | `conversation_id` 全部相同 `"62001"` | BOSS `d-c` 属性是用户 ID，所有会话共享 | 改用 `hr_name` 生成唯一 ID，格式 `hr_<HR名称>`；`_extract_chat_items` JS 中不再读取 `d-c` | `boss_scan.py` |
| 2 | 点击 HR 会话不打开右侧对话面板 | JS `li.click()` 不触发 Vue 路由/事件 | `_click_conversation_by_id` 改为先按 `.name-box` 文本匹配，再点击 `.friend-content` 子元素 | `boss_scan.py` |
| 3 | 消息角色全部 `unknown`，提取到的是侧边栏预览文本 | 旧选择器 `.message-item` / `.chat-message` 等命中了侧边栏 `.last-msg`，而非右侧对话面板 | 完全重写 `_extract_conversation_messages`：选择器改为 `.im-list > li.message-item`；角色判断用 `item-myself`/`item-friend` CSS class；文本从 `.text p span` 提取，跳过状态标签 | `boss_scan.py` |

### 功能增强：BOSS 内置标签过滤

**关键简化**：BOSS 已内置 "全部/未读/新招呼" 过滤标签，直接利用比自己判断可靠得多。

| 标签 | 用途 | 说明 |
|------|------|------|
| 未读 | 心跳巡检的数据源 | BOSS 自动标记，Agent 只处理未读会话 |
| 新招呼 | HR 首次联系未回复 | 如果 JD 不匹配已忽略，会留在此标签下 |
| 全部 | 默认视图 / 手动检查 | 包含所有会话 |

代码变更：
- `pull_boss_chat_conversations` 新增 `chat_tab` 参数（默认 "全部"）
- `_click_chat_tab(page, tab_name)` 函数：点击标签切换过滤视图
- `BossChatPullRequest` / `BossChatHeartbeatTriggerRequest` 新增 `chat_tab` 字段
- **心跳默认 `chat_tab="未读"`**：只拉取未读会话，避免处理旧消息
- 操作完毕自动切回 "全部" 标签

### 验证结果

| 测试项 | 结果 | 详情 |
|--------|------|------|
| conversation_id 唯一性 | ✅ 8/8 唯一 | `hr_黄女士快商通招聘经理` 等 |
| 名称匹配点击 | ✅ 3/3 成功 | 点击后右侧正确加载对话 |
| 消息角色识别 | ✅ `self:5 hr:7` | 黄女士会话精确区分 |
| has_candidate_messages | ✅ | 黄女士=True（有我的消息），潘女士=False（仅HR消息） |
| pending_hr_texts | ✅ | 潘女士=2条待回复，黄女士=0条 |
| 多会话消息独立性 | ✅ | 3个会话内容完全独立（不再串线） |
| 未读标签切换 | ✅ | 点击"未读"正确过滤 |
| 新招呼标签切换 | ✅ | 显示10个HR首次联系 |

---

## 十四、参考资料

| 项目 | 价值 | 链接 |
|------|------|------|
| JobPilot 海投助手 | 发简历 DOM 选择器逆向 | greasyfork.org/scripts/556268 |
| Boss Batch Push | BOSS API (`friend/add.json`) + Protobuf WebSocket | greasyfork.org/scripts/468125 |
| auto_job__find__chatgpt__rpa | Selenium + ChatGPT 完整工作流 (1564 star) | github.com/Frrrrrrrrank/auto_job__find__chatgpt__rpa |
| auto-zhipin | Playwright + LLM 个性化沟通 | github.com/ufownl/auto-zhipin |
| LangChain GTM Agent | 销售场景 Agentic + HITL 最佳实践 | blog.langchain.com/how-we-built-langchains-gtm-agent |
| browser-use | Agentic 浏览器框架架构参考 (81k star) | github.com/browser-use/browser-use |
