"""Hard contract: planner's first move on any real HR message = send_resume.

求职场景第一性原理: HR 主动发来的任何"真实消息"(寒暄/表达兴趣/简单自我介绍/
招人广告) 都等价于 "请递简历". IGNORE 只保留给 BOSS 系统 UI 噪音,
ESCALATE 只保留给真正敏感的谈判话题. 这里锁合同, 防止下次 prompt 或
heuristic 被无意翻回 "寒暄 → IGNORE" 的旧行为.

这些测试只针对 heuristic + initiator_policy, 不依赖 LLM 路由器, 保证
CI 稳定 (LLM 返回不可重现).
"""

from __future__ import annotations

import pytest

from pulse.modules.job.chat.planner import HrMessagePlanner, PlannedChatAction
from pulse.modules.job.shared.enums import ChatAction, ConversationInitiator


# ────────────────────────────────────────────────────────────────────
# 1. heuristic: the fallback when LLM is unavailable
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "你好，对你之前的经历非常感兴趣",
        "Hi，我们赞意正在寻找 AI 实习生，请问考虑吗？",
        "在吗",
        "你好",
        "我们这边在找大模型应用方向的同学",
    ],
)
def test_heuristic_defaults_hr_greetings_to_send_resume(message: str) -> None:
    plan = HrMessagePlanner._plan_with_heuristic(message)
    assert plan.action == ChatAction.SEND_RESUME, (
        f"HR greeting {message!r} must heuristically fall back to send_resume "
        f"(job-seeker default); got {plan.action.value}"
    )


@pytest.mark.parametrize(
    "message",
    [
        "您正在与Boss周晨业沟通",
        "您已收到招呼",
        "对方已暂停沟通",
    ],
)
def test_heuristic_keeps_ui_noise_as_ignore(message: str) -> None:
    plan = HrMessagePlanner._plan_with_heuristic(message)
    assert plan.action == ChatAction.IGNORE, (
        f"BOSS system UI text {message!r} is not a real HR utterance; must stay IGNORE"
    )


@pytest.mark.parametrize(
    "message",
    [
        "方便加个微信聊聊薪资吗",
        "下周能来线下面试吗",
        "你现在几个 offer 在手上",
        "具体的到岗时间是？",
    ],
)
def test_heuristic_escalates_sensitive_negotiation(message: str) -> None:
    plan = HrMessagePlanner._plan_with_heuristic(message)
    assert plan.action == ChatAction.ESCALATE, (
        f"Sensitive negotiation topic {message!r} must escalate to HITL; "
        f"got {plan.action.value}"
    )


def test_heuristic_empty_message_still_ignored() -> None:
    plan = HrMessagePlanner._plan_with_heuristic("   ")
    assert plan.action == ChatAction.IGNORE


def test_heuristic_explicit_resume_request_still_send_resume() -> None:
    plan = HrMessagePlanner._plan_with_heuristic("方便发一份简历吗")
    assert plan.action == ChatAction.SEND_RESUME


# ────────────────────────────────────────────────────────────────────
# 2. initiator policy: the "HR-initiated → force escalate" one-liner
#    has been explicitly retired. Contract locks that it is NOT back.
# ────────────────────────────────────────────────────────────────────


def test_initiator_policy_no_longer_force_escalates_hr_reply() -> None:
    """HR 主动发起的对话不再被一刀切升级成 ESCALATE.

    这是历史上 "3 未读全部静默 ESCALATE, 0 自动回复" 的根本原因之一,
    与用户诉求 ('HR 打招呼先发简历') 直接冲突, 故永久下线.
    """
    plan = PlannedChatAction(
        action=ChatAction.REPLY,
        reason="LLM says reply",
        reply_text="您好",
    )
    policed = HrMessagePlanner._apply_initiator_policy(
        plan, initiated_by=ConversationInitiator.HR
    )
    assert policed.action == ChatAction.REPLY
    assert policed.reply_text == "您好"


def test_initiator_policy_no_longer_force_escalates_hr_send_resume() -> None:
    plan = PlannedChatAction(
        action=ChatAction.SEND_RESUME, reason="planner says send_resume"
    )
    policed = HrMessagePlanner._apply_initiator_policy(
        plan, initiated_by=ConversationInitiator.HR
    )
    assert policed.action == ChatAction.SEND_RESUME


def test_initiator_policy_upgrades_ignore_on_real_message_to_send_resume() -> None:
    """LLM 偶尔会对寒暄也回 IGNORE; policy 层兜底翻为 SEND_RESUME."""
    plan = PlannedChatAction(
        action=ChatAction.IGNORE, reason="low-priority greeting"
    )
    policed = HrMessagePlanner._apply_initiator_policy(
        plan, initiated_by=ConversationInitiator.HR
    )
    assert policed.action == ChatAction.SEND_RESUME
    assert "upgrade_from_ignore" in policed.reason


def test_initiator_policy_keeps_ignore_on_ui_noise() -> None:
    """纯 UI 噪音 / empty 的 IGNORE 必须保持 IGNORE, 不要给噪音发简历."""
    for reason in (
        "BOSS 系统 UI 噪音, 非真实 HR 消息",
        "empty message",
        "LLM says system pure UI text",
    ):
        plan = PlannedChatAction(action=ChatAction.IGNORE, reason=reason)
        policed = HrMessagePlanner._apply_initiator_policy(
            plan, initiated_by=ConversationInitiator.HR
        )
        assert policed.action == ChatAction.IGNORE, (
            f"IGNORE with reason={reason!r} must stay IGNORE; got {policed.action.value}"
        )


def test_initiator_policy_keeps_escalate_for_sensitive_topics() -> None:
    plan = PlannedChatAction(action=ChatAction.ESCALATE, reason="薪资谈判")
    policed = HrMessagePlanner._apply_initiator_policy(
        plan, initiated_by=ConversationInitiator.HR
    )
    assert policed.action == ChatAction.ESCALATE
