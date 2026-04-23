# OfferPilot Demo 演示脚本（8 分钟版）

> 目标：面试时稳定演示“可落地 + 有工程深度 + 安全可控”的 Agent 项目。
> 版本：阶段 6 收口版（含公司情报、面试题库、安全治理）。

---

## 0) 演示前 60 秒自检

```bash
# 后端健康
curl -sS http://127.0.0.1:8010/health

# 快速回归（可选）
cd backend
source /root/.venvs/offerpilot/bin/activate
python smoke_check.py
```

如果要走“命令行一键演示”备用方案：

```bash
python demo_walkthrough.py
```

---

## 1) 开场（30 秒）

建议原话：

- “OfferPilot 是一个安全受控的求职运营 Agent。OpenClaw 负责消息入口和调度，LangGraph 负责复杂工作流编排，MCP 提供工具标准化接入。”
- “我重点展示 3 条链路：业务闭环、自动化可控、安全治理。”

---

## 2) 链路 A：业务闭环（3 分钟）

### A1. JD 分析 + 证据召回（阶段 1）

在看板：

- 填 JD -> 点击“JD 分析”
- 讲 `match_score` + `resume_evidence` + `similar_jobs`

建议强调：

- “不是纯 prompt 输出，评分依据来自简历向量检索片段。”

### A2. 材料生成审批（阶段 2）

在看板：

- 从最近岗位“一键生成材料”
- 依次演示 `regenerate -> approve -> export`

建议强调：

- “这里是 LangGraph interrupt + PostgreSQL checkpoint，可中断恢复。”

### A3. BOSS / 网申（阶段 3-4，可按时间选讲）

- 有时间：演示 BOSS 扫描或表单填充审批
- 时间紧：直接讲“有 HITL 守卫，不自动提交外部申请”

---

## 3) 链路 B：持续自动化（1.5 分钟）

### B1. 邮件巡检 + 心跳联动（阶段 5）

在看板：

- 点击“手动触发一次巡检”
- 展示 heartbeat 状态区和通知发送结果（含 `schedule_reminders`）
- 展示“未来 14 天日程看板”（由邮件自动提取并入库 `schedules`）

补充命令（可口头提）：

```bash
./scripts/openclaw_heartbeat_setup.sh status
./scripts/openclaw_heartbeat_setup.sh run-now offerpilot-email-daily-heartbeat
curl -sS "http://127.0.0.1:8010/api/schedules/upcoming?limit=10&days=14"
```

建议强调：

- “OpenClaw cron 与后端巡检走同一条链路，支持无人值守。”

---

## 4) 链路 C：阶段 6 深度（2 分钟）

### C1. 公司情报 + 面试题库

在看板：

- 输入公司名和岗位，点击“生成公司情报”
- 点击“生成面试题库”

建议强调：

- “公司情报输出结构化字段，不只是散文；面试题包含 intent 和答题提示。”

### C2. 安全治理

在看板：

- 签发一次性 token
- 连续消费两次，第二次应被拦截（防重放）
- 预算校验：先允许，再超限拒绝

建议强调：

- “自动化不越界：审批令牌 + 工具预算双保险。”

---

## 5) 收尾（30 秒）

建议原话：

- “这个项目我不是只做了功能点，而是做了可演示、可恢复、可审计的 Agent 工程化闭环。”
- “如果你们团队在做 Agent 业务，我可以直接承担工作流编排、工具接入、稳定性和安全治理这条线。”

---

## 6) 演示故障兜底（必须准备）

- **BOSS 扫描 503**：直接切到“已存最近岗位 + 材料审批 + 阶段 6 区块”
- **IMAP 未配置**：说明“邮件巡检链路已打通，当前环境缺少真实邮箱凭据”
- **模型波动**：切换到已生成结果区，讲 fallback 策略（`qwen3-max` 主、`qwen-plus` 备）

---

## 7) 面试追问高频答法（速记）

- 为什么 OpenClaw + LangGraph？
  - “OpenClaw 做入口和调度，LangGraph 做业务状态机，职责解耦。”
- 如何防止自动化误操作？
  - “外部危险动作必须审批；一次性令牌 + 预算限制 + 审计时间线。”
- 如何评估 Agent 效果？
  - “看一致性、准确率、通过率、延迟四类指标，已在 `/api/eval/metrics` 落地。”
