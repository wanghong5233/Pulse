"""Pulse SafetyPlane — Agent 授权边界与 Escalation 原语 (ADR-006).

本包实现 SafetyPlane 核心契约:

* :mod:`pulse.core.safety.intent` — Intent (要做什么)
* :mod:`pulse.core.safety.context` — PermissionContext (谁/在哪/带什么证据)
* :mod:`pulse.core.safety.decision` — Decision / AskRequest / ResumeHandle
  (Gate 的三值判决和 Ask-Resume 路由)

后续 Step A.2+ 还会加入:

* ``rule_engine`` — 规则加载与匹配
* ``gate`` — PermissionGate 默认实现
* ``suspended`` — SuspendedTaskStore (Ask-Resume 状态机)
* ``hooks`` — 注册到 ``HookPoint.before_tool_use`` 的适配层

此 __init__ 只做 re-export, 不含实现逻辑 —— 避免循环依赖, 也方便调用
方写成 ``from pulse.core.safety import Decision, Intent, ...``。
"""

from __future__ import annotations

from pulse.core.safety.context import PermissionContext
from pulse.core.safety.decision import (
    VALID_DECISION_KINDS,
    AskRequest,
    Decision,
    DecisionKind,
    ResumeHandle,
)
from pulse.core.safety.intent import VALID_INTENT_KINDS, Intent, IntentKind

__all__ = (
    "AskRequest",
    "Decision",
    "DecisionKind",
    "Intent",
    "IntentKind",
    "PermissionContext",
    "ResumeHandle",
    "VALID_DECISION_KINDS",
    "VALID_INTENT_KINDS",
)
