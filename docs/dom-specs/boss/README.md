# BOSS 直聘 DOM Specs

> SOP 方法论见 [`../README.md`](../README.md)。
> 采集器：[`scripts/dump_boss_dom.py`](../../../scripts/dump_boss_dom.py)。
> 代码归宿：[`src/pulse/mcp_servers/_boss_platform_runtime.py`](../../../src/pulse/mcp_servers/_boss_platform_runtime.py)。
> 形态守卫：
> - [`tests/pulse/mcp_servers/test_boss_helpers.py`](../../../tests/pulse/mcp_servers/test_boss_helpers.py) — search-list / chat-list 形态启发式
> - [`tests/pulse/mcp_servers/test_boss_autoreply_decision.py`](../../../tests/pulse/mcp_servers/test_boss_autoreply_decision.py) — ADR-004 规则决策
>
> 决策契约：[`docs/adr/ADR-004-AutoReplyContract.md`](../../adr/ADR-004-AutoReplyContract.md)（auto-reply Reader + Decision + Actuator + Orchestrator 全落地）
> 端到端 dry-run 脚本：[`scripts/smoke_auto_reply.py`](../../../scripts/smoke_auto_reply.py)

## 反爬注意事项

| 项 | 观察 | 应对 |
|---|---|---|
| DevTools 检测 | F12 打开立即 `debugger;` 循环 / 强制跳转 | **不要**右键"检查"。Playwright 走 CDP 不触发 |
| 源代码壳 | 右键"查看网页源代码"只有 `<div id='app'>加载中</div>` | 必须等 SPA 渲染完再 dump |
| 登录态 | 未登录时搜索/聊天页为登录弹窗，dump 出来没有业务节点 | 复用 `~/.pulse/boss_browser_profile` |
| SingletonLock | backend 在跑 scan 时会霸占 profile 锁 | dump 前先停 backend，或等一个 scan 周期结束 |
| 薪资字段脱敏 | 搜索卡片 `.job-salary` 在未登录/某些账号下全部显示 `"-元/天"` | 合同约定：薪资以 `chat-detail/.chat-position-content .salary` 为准，search-list 侧仅作候选。**当前实现**：chat-detail Reader 已落地（`extract_chat_detail_state.position_salary`），但 greet 路径仍**没有**在发消息前打开 chat-detail 二次取薪资——"greet 时二次打开取真薪资"属独立任务，留给 ADR-004 follow-up |
| 地址节点伪装 | `.company-location` 字面含 "company" 却是地址 | 禁用 `[class*='company']`，公司精确走 `.boss-name` |

## page_type 索引

| page_type | 用途 | 合同文档 | 最新 dump |
|---|---|---|---|
| [`search-list`](./search-list/) | scan / greet 的岗位列表解析 | [search-list/README.md](./search-list/README.md) | `20260422T065914Z` |
| [`chat-list`](./chat-list/) | 托管模式监听"未读"/"新招呼"tab 新消息 | [chat-list/README.md](./chat-list/README.md) | `20260422T072555Z` |
| [`chat-detail`](./chat-detail/) | 自动回复：HR 卡片识别、主动发简历等 | [chat-detail/README.md](./chat-detail/README.md) | `20260422T073442Z` |

## 一次典型排障流程（以 `trace_fe19c3ab1e43` 为例）

1. 用户反馈 reply 里 `公司：杭州·余杭区·仓前`（形态明显是地址）。
2. 查 `logs/boss_mcp_actions.jsonl` 最近 26 条 `greet_job` record，发现 100% 形如 `xx·xx·xx`，锁定 page_type = `search-list`。
3. `python scripts/dump_boss_dom.py search-list "Agent开发实习生"`，落到 `search-list/<ts>.json`。
4. 肉眼扫 class tree：`.company-location` 是地址，`.boss-name` 才是公司；老代码用 `[class*='company']` 必然命中前者。根因定位。
5. 更新 [search-list/README.md](./search-list/README.md) 合同表。
6. 改 `_extract_jobs_from_page` 里的 JS selector + 加 `_looks_like_address` 形态守卫。
7. 给 `test_boss_helpers.py` 追加来自本次 dump 的真实样本 parametrize。
8. 用户下一次真实投递验收 audit log 里 company 是否回到公司名。

## 已回答的问题（合同落地后回填）

- **chat-list**
  - 未读红点：`.figure .notice-badge` 节点存在即未读；`innerText` 是数字或 `99+`。
  - 会话 item：`.user-list > li`，公司/岗位**无 class**，只能靠"排除 `.name-text` 后的 `.name-box > span` 顺序"。
  - 我方送达状态：`.message-status.status-read` / `.status-delivery` 节点存在性。
- **chat-detail**
  - HR 卡 vs 机器人卡：颜色 class `.boss-green` vs `.blue`（后者无头像 `.figure`）。
  - 动作卡"同意/拒绝"：卡片内嵌按钮是通用 `.card-btn`，但 BOSS 会在 `.message-tip-bar .respond-popover` 浮动条里复制一份 **独有 class** 的 `.btn-agree` / `.btn-refuse`，Actuator 层优先点浮动条。
  - 发简历按钮：**无独立 class**，必须靠 `innerText === "发简历"` 文本定位。
  - 真实薪资来源：`chat-detail/.chat-position-content .salary`（未脱敏）。

## 仍未回答的问题（待下次 dump 补采）

- 跨 tab 一致性：chat-list "未读" / "新招呼" tab 的 item DOM 是否真的与 "全部" tab 相同（用户经验推断一致，未机械验证）
- 列表虚拟滚动：会话超过视口时是否懒加载（本次样本 6 条未触发）
- 跨天时间戳形态：`昨天` / `MM-DD` / `YYYY-MM-DD`（本次全是当天 `HH:MM`）
- 其他 HR 动作卡类型：面试邀请 / 换电话发起 / 换微信发起（看 `.dialog-icon` 二级 class 还能出什么）
- 多媒体消息：HR 发图片 / 语音 / 文件的 `li.message-item` 结构
- "发简历" 点击后是否弹 confirm 浮层
