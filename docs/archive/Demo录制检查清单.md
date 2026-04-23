# OfferPilot Demo 录制检查清单

> 用途：录制前 3 分钟快速自检，保证面试演示稳定。

## 1) 环境与服务

- 后端健康检查通过：`GET /health`
- 前端可访问：`http://127.0.0.1:3000`
- 数据库可读写（最近岗位/动作时间线可正常刷新）
- 模型环境变量已配置（主备模型可用）

## 2) 演示链路最小回归

- `python backend/smoke_check.py` 通过（或至少关键接口通过）
- JD 分析链路：能输出 `match_score + resume_evidence`
- 材料审批链路：`generate -> approve/reject -> export` 正常
- BOSS 对话链路：可拉取会话，`process` 返回结构化决策
- 邮件链路：`heartbeat/trigger` 返回 `schedule_reminders`

## 3) 演示素材就绪

- 预置 1 条可讲的 JD 文本
- 预置 1 条待审批材料线程（避免现场等待）
- 预置 1 条邮件样本（面试邀请）
- 前端“未来 14 天日程看板”有可展示数据

## 4) 讲解重点（8 分钟）

- 架构分层：OpenClaw 调度 + LangGraph 编排 + MCP 工具化
- 安全机制：审批令牌、工具预算、自动发送双开关
- 稳定性：Heartbeat 异常兜底、审计时间线、可恢复执行
- 业务闭环：JD -> 材料 -> 沟通 -> 邮件/日程 -> 面试准备

## 5) 故障兜底

- BOSS/IMAP 不可用：切到已落库数据 + 时间线回放
- 模型波动：说明主备切换与结构化 fallback
- 外部依赖超时：展示本地可复现实验链路（`demo_walkthrough.py`）
