"""Pulse SafetyPlane — Agent 授权边界与 Suspend-Ask-Resume 回路.

规约权威: ``docs/adr/ADR-006-v2-SafetyPlane.md`` (v1 已 Deprecated).

模块组成 (全部为 re-export, 不含实现逻辑; 各文件自己是实现):

* ``intent`` / ``decision`` / ``context`` —— 数据契约原语
* ``policies`` —— 三条 side-effect 动作的纯函数 policy
* ``suspended`` —— Ask 分支产生 SuspendedTask 的状态机 + 持久化
* ``resume`` —— 用户下一条 IM 文本接回 SuspendedTask 的 helpers
"""

from __future__ import annotations

# PULSE_SAFETY_PLANE 合法值 (仅两档, 见 ``docs/adr/ADR-006-v2-SafetyPlane.md``).
# 以模块级常量形式导出供 server / service 做 mode 比较, 避免散落的字面量.
SAFETY_PLANE_OFF = "off"
SAFETY_PLANE_ENFORCE = "enforce"

from pulse.core.safety.context import PermissionContext
from pulse.core.safety.decision import (
    VALID_DECISION_KINDS,
    AskRequest,
    Decision,
    DecisionKind,
    ResumeHandle,
)
from pulse.core.safety.intent import VALID_INTENT_KINDS, Intent, IntentKind
from pulse.core.safety.policies import (
    DEFAULT_ASK_TIMEOUT_SECONDS,
    DEFAULT_RESUME_INTENT,
    DEFAULT_RESUME_PAYLOAD_SCHEMA,
    card_policy,
    profile_covers,
    reply_policy,
    send_resume_policy,
    session_approved,
)
from pulse.core.safety.resume import (
    DEFAULT_PAYLOAD_SCHEMA,
    SUPPORTED_PAYLOAD_SCHEMAS,
    ResumedExecution,
    ResumedExecutionStatus,
    ResumedTaskExecutor,
    ResumeOutcome,
    ResumeOutcomeKind,
    build_resume_payload,
    render_ask_for_im,
    try_resume_suspended_turn,
)
from pulse.core.safety.suspended import (
    EVENT_TASK_ASK_TIMEOUT,
    EVENT_TASK_DENIED,
    EVENT_TASK_RESUMED,
    EVENT_TASK_SUSPENDED,
    FACT_KEY_PREFIX,
    EventPublisher,
    FactsStore,
    SuspendedTask,
    SuspendedTaskStatus,
    SuspendedTaskStore,
    TaskAlreadyTerminalError,
    TaskNotFoundError,
    WorkspaceSuspendedTaskStore,
)

__all__ = (
    "AskRequest",
    "SAFETY_PLANE_ENFORCE",
    "SAFETY_PLANE_OFF",
    "DEFAULT_ASK_TIMEOUT_SECONDS",
    "DEFAULT_PAYLOAD_SCHEMA",
    "DEFAULT_RESUME_INTENT",
    "DEFAULT_RESUME_PAYLOAD_SCHEMA",
    "Decision",
    "DecisionKind",
    "EVENT_TASK_ASK_TIMEOUT",
    "EVENT_TASK_DENIED",
    "EVENT_TASK_RESUMED",
    "EVENT_TASK_SUSPENDED",
    "EventPublisher",
    "FACT_KEY_PREFIX",
    "FactsStore",
    "Intent",
    "IntentKind",
    "PermissionContext",
    "ResumedExecution",
    "ResumedExecutionStatus",
    "ResumedTaskExecutor",
    "ResumeHandle",
    "ResumeOutcome",
    "ResumeOutcomeKind",
    "SUPPORTED_PAYLOAD_SCHEMAS",
    "SuspendedTask",
    "SuspendedTaskStatus",
    "SuspendedTaskStore",
    "TaskAlreadyTerminalError",
    "TaskNotFoundError",
    "VALID_DECISION_KINDS",
    "VALID_INTENT_KINDS",
    "WorkspaceSuspendedTaskStore",
    "build_resume_payload",
    "card_policy",
    "profile_covers",
    "render_ask_for_im",
    "reply_policy",
    "send_resume_policy",
    "session_approved",
    "try_resume_suspended_turn",
)
