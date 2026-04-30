"""标准事件类型与 payload 助手.

Pulse 的观测平面按 Event Sourcing 的简化思想组织:
- 所有跨模块可观测信号(记忆写入/LLM 调用/工具调用/连接器访问/策略决策)都
  以"事件"的形式通过 ``EventBus.publish(event_type, payload)`` 发出.
- 内存总线上挂 ``InMemoryEventStore``(滑动窗口, 供 WS/SSE 推送)和
  ``JsonlEventSink``(append-only 落盘, 供审计/回放).
- 事件不承担"给 LLM 读"的职责(那是 MemoryRuntime 的职责);
  记忆也不承担"不可变审计"的职责(那是 EventLog 的职责).

此模块只定义**字符串常量**和**payload 构造助手**, 不引入新的基础设施.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4


class EventTypes:
    """事件类型 catalog. 新增事件请在此处登记, 避免拼写漂移.

    只登记**当前代码真实发射**或**明确有 Owner 的规划事件**; 没有发射侧
    且没有实装计划的"幻想事件"不进 catalog, 避免文档和代码漂移.
    """

    # ── 接入层 / Runtime(server.py 实际发射) ──
    CHANNEL_MESSAGE_RECEIVED = "channel.message.received"
    CHANNEL_MESSAGE_DISPATCHED = "channel.message.dispatched"
    CHANNEL_MESSAGE_FAILED = "channel.message.failed"
    CHANNEL_MESSAGE_COMPLETED = "channel.message.completed"

    # ── Brain ReAct(server.py 实际发射) ──
    BRAIN_STEP = "brain.step"
    BRAIN_TOOL_INVOKED = "brain.tool.invoked"
    # Brain.reply_shaper 发射: 追踪"给用户的最终回复是否经过二次改写"
    # mode=skip_clean / rewrote. 不改写也会发 skip_clean, 用于后续审计确认没绕.
    BRAIN_REPLY_SHAPED = "brain.reply.shaped"
    # reply_shaper 降级(无 router / LLM error / 空输出); 仍用 raw_answer 回用户,
    # 但这条事件会落盘让事后能找到"为什么这条回复没被润色".
    BRAIN_REPLY_SHAPE_DEGRADED = "brain.reply.shape.degraded"

    # ── ToolUseContract C · CommitmentVerifier(ADR-001 §4.4) ──
    # 每轮 ReAct 末尾发射一次, 审计 reply 里声明的动作承诺是否真的由
    # used_tools 兑现. verified 高频但持久化(用于全局审计断言"N% turns
    # pass contract C"), unfulfilled / degraded 必须持久化以便复盘.
    BRAIN_COMMITMENT_VERIFIED = "brain.commitment.verified"
    BRAIN_COMMITMENT_UNFULFILLED = "brain.commitment.unfulfilled"
    BRAIN_COMMITMENT_DEGRADED = "brain.commitment.degraded"

    # ── LLM 调用(LLMRouter 实际发射) ──
    LLM_INVOKE_OK = "llm.invoke.ok"
    LLM_INVOKE_EXHAUSTED = "llm.invoke.exhausted"

    # ── 记忆写入(Brain / CoreMemory 实际发射) ──
    MEMORY_WRITE = "memory.write"  # Brain._route_envelope 成功写入
    MEMORY_CORE_UPDATED = "memory.core.updated"  # CoreMemory.update_block 产生实际变化

    # ── 偏好派发(DomainPreferenceDispatcher 实际发射) ──
    # 把"用户自然语言偏好 → 业务域记忆"的通路从"靠 LLM 调对工具"改成
    # "架构强制 dispatch". 每条 domain_pref 都会发且仅发一条:
    #   * applied   – Applier 成功写入 DomainMemory (e.g. JobMemory)
    #   * skipped   – 置信度不够 / domain 未注册 / 其它非异常原因跳过
    #   * rejected  – Applier 显式拒绝(非法 args / 越界字段)
    #   * error     – Applier 执行异常(记忆写失败等)
    PREFERENCE_DOMAIN_APPLIED = "preference.domain.applied"
    PREFERENCE_DOMAIN_SKIPPED = "preference.domain.skipped"
    PREFERENCE_DOMAIN_REJECTED = "preference.domain.rejected"
    PREFERENCE_DOMAIN_ERROR = "preference.domain.error"

    # ── job_greet 决策审计(per-JD verdict + reflection round) ──
    # 高价值低频事件: 一次 trigger 触发一组 candidate 事件(每个进 matcher
    # 的 JD 一条)和最多 _REFLECTION_MAX_ROUNDS 条 reflection 事件. 这两条
    # 是回答"agent 为什么没投递成功"的唯一真相来源, 必须可回放.
    MODULE_JOB_GREET_MATCH_CANDIDATE = "module.job_greet.match.candidate"
    MODULE_JOB_GREET_TRIGGER_REFLECTION = "module.job_greet.trigger.reflection"

    # ── 治理平面(SafetyPlane, ⏳ 规划中; 常量预留, 现阶段不会被发射) ──
    # 实装时机: SafetyPlane 作为 EventBus 订阅者接入, 同时作为发射者回写决策.
    # 详见 ``docs/Pulse-内核架构总览.md`` §6.6.
    POLICY_DECISION = "policy.decision"
    PROMOTION_REQUEST = "promotion.request"
    MEMORY_PROMOTED = "memory.promoted"      # ⏳ 规划: PromotionEngine 实装时发射
    MEMORY_SUPERSEDED = "memory.superseded"  # ⏳ 规划: archival 版本链触发时发射


# 只有审计价值高、量可控的事件才落 JsonlEventSink;
# channel.* / brain.* 每轮 ReAct 都高频发射, 从 logs 可还原, 不持久化.
_PERSISTED_PREFIXES: tuple[str, ...] = (
    "llm.",
    "memory.",
    "policy.",
    "promotion.",
    # brain.reply.* 是低频审计事件(每次任务最多 1-2 条), 必须可回放;
    # brain.step / brain.tool.invoked 高频, 不在白名单.
    "brain.reply.",
    # brain.commitment.* 是契约 C 的三分类结果(verified/unfulfilled/degraded),
    # 频率和 brain.reply.shaped 相当(每轮最多 1 条), 审计价值高 — 要查"为什么
    # agent 承诺'已记录'却没调 memory.record", 必须能从 jsonl 回放到这一条.
    "brain.commitment.",
    # preference.domain.* 是"用户自然语言偏好 → 业务域记忆"的落地证据,
    # 每次 reflection 少则 0 条多则若干条, 必须可回放以便事后解释为什么
    # 某条偏好没有被持久化(低 confidence / applier 缺失 / Applier 报错).
    "preference.",
    # job_greet 的 per-JD verdict 与 reflection 决策. module.* 默认不落盘
    # (高频、可从 runtime 日志还原), 但这两条是 agent 反思链路的唯一真相
    # 来源 — "为什么没投递" 在事后只能从这里复原, 必须可回放.
    "module.job_greet.match.candidate",
    "module.job_greet.trigger.reflection",
)


def should_persist(event_type: str) -> bool:
    """是否是需要 append-only 持久化的"审计级"事件.

    内存态事件(如 ``channel.*`` 会话流)数量大且可从 runtime 日志还原, 默认不落盘;
    ``llm/tool/memory/policy`` 事件涉及合规与复盘, 必须落盘.
    """
    safe = str(event_type or "").strip().lower()
    return any(safe.startswith(prefix) for prefix in _PERSISTED_PREFIXES)


def make_payload(
    *,
    trace_id: str | None = None,
    actor: str,
    session_id: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
    workspace_id: str | None = None,
    causation_id: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """构造事件 payload. 强制出现的字段是 ``actor``; 其它为可选上下文.

    - ``causation_id``: 触发当前事件的上游事件 id(构建因果链, 替代
      ``MemoryEnvelope.evidence_refs / promoted_from`` 等可选字段).
    - ``event_id``: 给发射侧一个稳定句柄, 用作下游事件的 causation_id 引用.
    """
    payload: dict[str, Any] = {
        "event_id": f"evt_{uuid4().hex[:12]}",
        "actor": str(actor),
    }
    if trace_id:
        payload["trace_id"] = str(trace_id)
    if session_id:
        payload["session_id"] = str(session_id)
    if task_id:
        payload["task_id"] = str(task_id)
    if run_id:
        payload["run_id"] = str(run_id)
    if workspace_id:
        payload["workspace_id"] = str(workspace_id)
    if causation_id:
        payload["causation_id"] = str(causation_id)
    for key, value in fields.items():
        if value is None:
            continue
        payload[key] = value
    return payload


__all__ = ["EventTypes", "make_payload", "should_persist"]
