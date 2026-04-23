# ADR-003: ActionReport — 执行结果报告层

| 字段 | 值 |
|---|---|
| 状态 | Proposed(Scaffolding landed) |
| 日期 | 2026-04-21 |
| 作用域 | `src/pulse/core/action_report.py`、`src/pulse/core/brain.py`、`src/pulse/core/verifier.py`、各 module 的 tool handler |
| 关联 | `ADR-001-ToolUseContract.md`(契约 A/B/C)、`docs/code-review-checklist.md`(类型 B 补丁判例)、`docs/Pulse实施计划.md` M9.ActionReport |

---

## 1. 现状

`trace_34682759d5e7` 场景:用户说"投递 1 个合适的 JD" → Brain 正确走 `job.greet.scan` → `job.greet.trigger(confirm_execute=true, batch_size=1)` → 浏览器**真实投递了 1 个岗位**(MCP 日志 `status=sent, ok=true, greeted=1`) → Brain 的 raw_reply 说"已为你投递了 1 个合适岗位",内容与现实一致。

但 `CommitmentVerifier`(契约 C)的 judge LLM 返回 `fulfilled=false, hallucination_type=fabricated, reason="ledger 中没有投递 receipt"`,导致最终 reply 被改写为"我其实没能完成投递...没有实际投递",与现实相反。

**体感层面**:用户看着浏览器真投出去,却收到"没投递"的文字回复。

上一轮曾尝试在 `verifier.py` 里补一段 `_false_absence_guard_reason`:"如果 reply 含'投递/打招呼/发送'关键词且 receipts 里 `job.greet.trigger.greeted > 0`,就把 judge 的 unfulfilled **反向** override 为 verified"。该补丁**被 `code-review-checklist §15 类型 B`明确列为屎山代码**(靠关键词 + module-specific 白名单兜底,上游 projection 有 bug 却在下游打胶布),已回滚。

---

## 2. 根因:缺少「执行结果报告层」

Pulse 当前的 tool execution → final reply 数据流:

```text
                                    (LLM 自由翻译)
tool_handler ── observation(JSON) ─────────────────> raw_reply ── shaper ──> shaped_reply
                      │
                      │ extract_facts 白名单抽取
                      ▼
                 Receipt.extracted_facts ──> judge LLM ──> verified / unfulfilled
```

有两条并行的事实投影:

1. **给 LLM 的 projection**:observation(JSON 大杂烩) → LLM 自己"回忆"出自然语言
2. **给 judge 的 projection**:observation → `extract_facts` → `Receipt.extracted_facts`(散碎字段)

两条 projection **互相不对齐**。具体 `trace_34682759d5e7` 里:

- LLM 看到 `{"matched_details": [{"job_title":"AIGC视觉生成实习","status":"greeted",...}], "greeted":1, "failed":0, ...}`,正确地讲成"已为你投递了 1 个岗位"。
- judge 看到 `Receipt(name="job.greet.trigger", extracted_facts={"greeted":1, "needs_confirmation":false, ...})`,**但 judge 的 prompt 没有"`greeted=1` 就等于真实发送"的硬规则**,它自由推理后误判为"没证据"。

**一句话根因**:两个 LLM 对同一份副作用做了两次不同的 projection,然后我们再拿第二次 projection 去审第一次 projection 的产物——天然会漂移。

上一轮加的 guard 补丁只是在下游把一个特定漂移"掰回来",但:

- **按模块硬编码**:guard 写死了 `job.greet.trigger`,未来 `game.checkin.run` / `trip.plan.finalize` / `system.login` 出同样问题都要再写一条。
- **按关键词硬编码**:guard 靠 `"投递/发送/打招呼"` 关键词匹配 reply;新动词("签到"/"预订"/"转账")天然漏网。
- **事后补救**:判决已经错了才纠,没修上游的"两条 projection 不一致"问题。

属于 `code-review-checklist §15 Type B`:「不修源头,下游堆补丁」。

---

## 3. 第一性原理

| 维度 | 分析 | 结论 |
|---|---|---|
| **事实一致性** | 两条 projection 会漂移,就让两条 projection 共用同一份 ground-truth | 引入结构化 ActionReport,LLM 与 verifier 消费同一份,不再各自翻译 |
| **责任归属** | "我做了什么"应该由 module(最懂自己业务)说,不是 LLM(基于 observation 猜) | ActionReport 是 module handler 的返回契约,LLM 只负责自然语言化 |
| **通用性** | job.greet / game.checkin / trip.plan / system.login / notification.send 都面临同一问题 | ActionReport 放 `core/`,零业务词汇,action/status/details/metrics/next_steps/evidence 通用 schema |
| **向后兼容** | 存量 tool 不愿立刻迁移 | observation 里不挂 ActionReport 时 Brain / Verifier 走旧路径,迁移是增量的 |
| **可验证性** | 契约要可单测 | frozen dataclass + to_dict/from_dict 往返 + to_prompt_lines / to_receipt_facts 纯函数,全可单测 |
| **可逆性** | 万一 LLM 对注入的 SystemMessage 反应不好,要能回退 | 通过 env `PULSE_ACTION_REPORT_INJECT=off` 关闭 Brain 的 prompt 注入,降级为仅挂 Receipt |

---

## 4. 接口契约

### 4.1 核心 schema(`src/pulse/core/action_report.py`)

```text
ActionStatus = Literal["succeeded", "partial", "failed", "preview", "skipped"]

ActionDetail(
  target: str,                 # 用户可读 ("AIGC视觉生成实习" / "原神" / "D1 成都")
  status: ActionStatus,
  reason: str | None,          # 失败或 skipped 的用户层原因
  url: str | None,             # 可跳转
  extras: dict[str, Any],      # 业务附加字段
)

ActionReport(
  action: str,                 # "job.greet" / "game.checkin" / "trip.plan"
  status: ActionStatus,        # 整体结论 (build 自动聚合 details, preview/skipped 需显式指定)
  summary: str,                # 一句话用户可见结论
  details: tuple[ActionDetail, ...],
  metrics: dict[str, int|float],   # "attempted"/"succeeded"/"failed" 等
  next_steps: tuple[str, ...],     # 给 LLM 的后续提示
  evidence: dict[str, Any],         # trace_id / screenshot / source
)
```

不变式:

- `action` 用 `<domain>.<verb>` 记号,避免 module 之间命名冲突。
- `status` 的五态语义固定: **succeeded**(全部成功) / **partial**(至少一个成功+至少一个非成功) / **failed**(无 succeeded) / **preview**(生成了预览未实际执行) / **skipped**(被幂等/前置条件短路)。
- `summary` 是**用户可读结论**,不是技术日志。超过 80 字符应当截断。
- `details[*].target` 必须是**用户可读标识**,不允许塞技术 id(`item_3fa9c2` 这种)。
- `metrics` 里的值仅 int/float 会被 `to_receipt_facts()` 投影到 Receipt(避免字符串污染 judge prompt)。
- `ACTION_REPORT_KEY = "__action_report__"`——双下划线前缀明确"runtime 识别用",不要与业务字段名撞车。

### 4.2 Module handler 的返回契约

```python
# 所有 mutating / multi-step module 的 tool handler, 统一这样返回:
from pulse.core.action_report import ACTION_REPORT_KEY, ActionReport, ActionDetail

async def run_greet_trigger(...) -> dict[str, Any]:
    ...  # 执行副作用
    report = ActionReport.build(
        action="job.greet",
        summary="投递了 1/1 个合适岗位",
        details=[
            ActionDetail(
                target=row["job_title"],
                status="succeeded" if row["status"] == "greeted" else "failed",
                reason=row.get("error"),
                url=row.get("source_url"),
            )
            for row in matched_details
        ],
        metrics={
            "attempted": len(matched_details),
            "succeeded": greeted_count,
            "failed": failed_count,
        },
    )
    return {
        "ok": True,
        "greeted": greeted_count,
        # ...既有业务字段保留, 保证向后兼容...
        ACTION_REPORT_KEY: report,
    }
```

handler **不得**只返回 `ActionReport` 而丢弃业务字段;既有的 observation shape 是 audit / debug 的 ground truth。

### 4.3 Brain 集成(待 Step B 实现)

```text
_react_loop:
  ob = tool_registry.invoke(tool_name, args)
  report = extract_action_report(ob)
  if report is not None:
      messages.append(
          SystemMessage(content="\n".join(report.to_prompt_lines()))
      )
      step.action_report = report.to_dict()
  ... # 原有 ToolMessage 继续挂 observation 全文, 不变

_build_tool_receipts:
  # 如果 step 有 action_report, 把它的 to_receipt_facts 和 handler 自带的
  # extract_facts 合并进 Receipt.extracted_facts; 且把 report 原文挂到
  # Receipt.action_report 供 verifier judge prompt 单独渲染.
```

可逆性:`PULSE_ACTION_REPORT_INJECT=off` 时跳过 SystemMessage 注入(Receipt 依然挂,只降级 LLM prompt 侧)。

### 4.4 Verifier 集成(待 Step B 实现)

```text
Receipt 新增字段:
  action_report: dict[str, Any] | None = None

judge prompt 的 Meta-rule 增加一节:
  "若某条 Receipt.action_report.status in {succeeded, partial} 且
   metrics.succeeded >= 1, 则 raw_reply 中语义相符的动作类承诺判 fulfilled.
   若 reply 声明的 count > metrics.succeeded, 仍判 count_mismatch."
```

关键:judge 的 grounding 从"零散 extracted_facts"升级为"结构化 ActionReport",误判面从"每个 tool 的字段组合"收敛成"ActionReport 的五态 + metrics",可解释性大幅提升。

### 4.5 通用性清单

| module | action | details[*].target | 典型 metrics | 状态语义 |
|---|---|---|---|---|
| `job.greet` | `job.greet` | 岗位标题 | `attempted`/`succeeded`/`failed`/`unavailable` | succeeded=全投成功,failed=全部拒绝,partial=部分成功 |
| `game.checkin` | `game.checkin` | 游戏名 | `attempted`/`success`/`already_done`/`error` | `already_done` 用 `status=skipped, reason="今日已签"` |
| `trip.plan` | `trip.plan` | 日期 D1/D2/... | `days_planned`/`est_budget_cny` | 常用 `preview` — 生成草案不是 mutating |
| `system.login` | `system.login` | 平台名 | `attempted`/`succeeded` | 单步 action,details 常只 1 项 |
| `notification.send` | `notification.send` | 收件人 | `total`/`delivered`/`failed` | failed 时 `reason` 带原因给用户看 |

新 module 接入时,只需按这张表约定 `action` 名 + details 的 target 粒度,**不改 core 任何一行**。

---

## 5. 可逆性与重评触发

### 5.1 开关

| 层 | 环境变量 | 关闭后行为 |
|---|---|---|
| Brain 注入 | `PULSE_ACTION_REPORT_INJECT=off` | 仅挂 `Receipt.action_report`,不追加 SystemMessage |
| Verifier 消费 | `PULSE_ACTION_REPORT_JUDGE=off` | judge prompt 不渲染 action_report 字段,回退为原 extracted_facts 规则 |
| 全局 | `PULSE_ACTION_REPORT=off` | 上两条全关;handler 可照旧返回 ActionReport,但 Brain / Verifier 不消费 |

### 5.2 重评触发

1. 连续 7 天 `brain.commitment.unfulfilled` 中 `hallucination_type=false_absence` 占比 < 3% → 证明 ActionReport 有效,可考虑把 `extract_facts` 标为 deprecated。
2. 某 module 的 tool 返回 `ACTION_REPORT_KEY` 但 Brain 日志里 `action_report_injected=false` → 注入开关异常,看 env。
3. `brain.commitment.verified` 中 `action_report_consumed=true` 占比 > 80% → 存量 tool 迁移进度健康。
4. 用户继续报"我看到 Agent 做了 X,但回复说没做" → action_report 覆盖不全,上对应 module 补 handler。

---

## 6. 落地顺序

| 阶段 | 内容 | 状态 |
|---|---|---|
| **Step A.1** | `core/action_report.py` 契约定义 + 22 条契约单测 | ✅ 已完成(本次) |
| **Step A.2** | 本 ADR-003 书面决策 + `Pulse实施计划.md` M9 条目 | ✅ 已完成(本次) |
| **Step A.3** | 撤掉上一轮 `verifier.py` 的 `_false_absence_guard_reason` 补丁 + 对应 2 条单测 | ✅ 已完成(本次) |
| **Step B.1** | Brain 识别 `__action_report__` → 注入 SystemMessage + 挂 `Receipt.action_report` + 关联单测 | ✅ 已完成 |
| **Step B.2** | Verifier `Receipt` 加 `action_report` 字段 + judge prompt Meta-rule 追加一节 + `trace_34682759d5e7` 回归测试 | ✅ 已完成 |
| **Step B.3** | `job.greet.service.run_trigger` 结尾产出 `ActionReport` + 模块单测 | ✅ 已完成 |
| **Step B.4** | 全量回归 + 手动 trace 回放 | ✅ 单测回归通过;端到端 trace_fe19c3ab1e43 手动回放通过(verdict=verified, raw→shaped→answer 未 rewrite) |
| **Step B.5** | ActionReport.to_prompt_lines 渲染 details[*].extras(白名单 primitive) + 通用 PUA sanitize + `job.greet.service` 透传 salary + Verifier judge 补齐 `skipped` 状态规则 | ✅ 已完成(本次) |
| **Step C**(后续独立任务) | 把 `game.checkin` / `system.login` / `trip.plan` 等 module 迁移到 ActionReport,`extract_facts` 逐步 deprecate | — |
| **Step D**(后续独立任务) | BOSS Private Use Area 字体反爬**在线解码器**(运行时下载 .woff → cmap → glyph→数字映射),根治 salary 数值乱码 | — |
| **Step E**(后续独立任务) | `boss_mcp_actions.jsonl` 的 audit 条目补 `trace_id` 字段,支持 brain trace × mcp trace 双向关联 | — |

Step A 是**纯增量且独立可跑**的地基:契约类型定义 + 文档,Brain / Verifier / job.greet 的现有调用链一行未改,因此即便 Step B 被否决,Step A 不会留下任何半成品。

Step B 已整体落地(2026-04-21):Brain / Verifier / `job.greet` 三处一起打通 ActionReport 管道,双 kill-switch(`PULSE_ACTION_REPORT_INJECT` / `PULSE_ACTION_REPORT_JUDGE`)保证任一侧都能独立降级。新增覆盖:
- `tests/pulse/core/test_brain_action_report.py` — Brain 的 SystemMessage 注入 / 降级 / 兼容路径;
- `tests/pulse/core/test_commitment_verifier.py` §8 — judge prompt 含 `action_report`、kill-switch、details 截断、`trace_34682759d5e7` 回归、preview / **skipped** 幻觉反向用例;
- `tests/pulse/modules/job/greet/test_service_action_report.py` — `run_trigger` 四条 exit 路径 (scan_miss / not_ready / preview / run) 的 ActionReport 形态与 `_UNAVAILABLE_STATUSES → skipped` 映射、salary 透传 / 缺失 / PUA sanitize 三组回归。

### Step B.5 的 3 个薄改动(trace_fe19c3ab1e43 薪资缺失 follow-up)

1. **`ActionReport.to_prompt_lines` 渲染 details[*].extras**。此前 `extras` 字典只进 `to_dict`(给 audit 和 Verifier 看),**不**进 prompt_lines(给 LLM 看),导致 module 即使在 extras 里塞了 salary / match_score / company,LLM 也看不到。新增 `_iter_renderable_extras` 按白名单(str / int / float / bool)渲染,配 `_MAX_EXTRAS_PER_DETAIL=8` 上限防 prompt 爆炸。
2. **通用 PUA sanitize**(`_sanitize_prompt_str`)。招聘平台(BOSS / 拉勾 / 58)普遍用 Private Use Area (U+E000..U+F8FF) 私有码点做数字反爬,裸字符扔给 LLM 会被默默吞掉或乱写成占位符,表现为"reply 没报薪资"。所有经 ActionReport 流向 prompt 的字符串(`action` / `summary` / `target` / `reason` / `next_step` / str evidence / str extras 值)统一过 `_PUA_PATTERN.sub("«encoded»", text)`,让 LLM 看到明示 marker,诚实地告诉用户"该字段被平台加密",而不是静默漏报(fail-loud)。注意:`ActionDetail.extras` 里依然保存**原串**供 audit,只有渲染进 prompt 的那一份被 sanitize。
3. **Verifier judge 补齐 `skipped` 状态规则**。此前 Meta-rule 只列了 `succeeded / partial / preview / failed` 四态,`skipped`(被幂等/前置条件短路,未发生副作用)在通用 knowledge 区,可能被 judge LLM 放水。本次显式写入:`action_report.status=="skipped"` 且 reply 声明"已完成" → 判 `inference_as_fact`,和 preview 对偶。

同步业务层:`job.greet.service.run_trigger` 的 `matched_details[*]` 透传 scan item 的 `salary`(可能含 PUA,由 ActionReport 层 sanitize),`_build_trigger_action_report` 按"求职视角优先级"排 extras 键(`company → salary → match_score → match_verdict`),空串 / None 静默丢弃。

端到端验收(`trace_fe19c3ab1e43` 回放):scan → trigger → Action Report SystemMessage → verifier `verified` → answer 未 rewrite,`boss_mcp_actions.jsonl` 同步记 `ok=true, status=sent, screenshot_path=20260422_134506_greet_2fdc49499d58b25c.png`,全链路无假通过。

---

## 7. 风险与取舍

1. **SystemMessage 注入可能让 LLM 拘谨**:注入 `IMPORTANT: MUST be grounded` 后 LLM 对 summary/details 之外的细节可能省略,体感回复变"死板"。mitigation: 通过 `PULSE_ACTION_REPORT_INJECT=off` 可即时回退;且实际 summary 本就由 module 控制,可以在 summary 里带足够的人味。
2. **ActionReport 与 extract_facts 的权责划分短期有重叠**:两者都写 `Receipt.extracted_facts`。约定:合并策略为 `{**facts_from_extract_facts, **action_report.to_receipt_facts()}`(后者覆盖前者的同名键),避免双源冲突。长期:`extract_facts` 标记为 legacy,新 tool 只走 ActionReport。
3. **跨进程 MCP tool 的 ActionReport 要 JSON 往返**:MCP observation 走 JSON,因此 handler 返回 `ActionReport` 实例到 Brain 时会经过 `to_dict` → `from_dict` 两跳;`ActionReport.from_dict` 已在契约单测里覆盖 roundtrip 稳定性。
4. **status 五态有重叠灰区**(例如"点了按钮但对方拒绝"该算 failed 还是 partial):由 `infer_status` 给出默认(有一个成功就是 partial,否则 failed),module 可自行 override。规则在 §4.1。

---

## 附录:与 ADR-001 的关系

- ADR-001 定义了三契约(A 描述 / B 调用 / C 验证)。
- ADR-003 不是第四个契约,而是**契约 A 的延伸**:把 A 侧 tool handler 的返回 shape 从 "dict + 可选 extract_facts" 升级为 "dict + 可选 ActionReport",统一 LLM 与 judge 的 grounding 面。
- 升级后,契约 C 的 judge 面从"extract_facts 白名单" → "ActionReport 结构化报告",原 `_false_absence_guard_reason` 那种下游补丁不再必要。
