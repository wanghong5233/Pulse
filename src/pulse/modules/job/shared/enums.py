"""Cross-capability enums for the job domain.

Values are strings so they round-trip safely through JSON / DB / tool
payloads and through LLM-planned action dicts.
"""

from __future__ import annotations

from enum import StrEnum


class ConversationInitiator(StrEnum):
    """Who started the current HR <-> geek conversation."""

    SELF = "self"        # Agent or user greeted first
    HR = "hr"            # HR sent the first message
    UNKNOWN = "unknown"  # Platform did not expose this signal


class ChatAction(StrEnum):
    """Planner output for a single inbound HR message."""

    REPLY = "reply"                # Send a plain-text reply
    SEND_RESUME = "send_resume"    # Send resume as attachment (not text!)
    ACCEPT_CARD = "accept_card"    # Click an interactive card's accept button
    REJECT_CARD = "reject_card"    # Click an interactive card's reject button
    ESCALATE = "escalate"          # Notify user for manual handling
    IGNORE = "ignore"               # Skip (e.g. company blacklisted)


class CardType(StrEnum):
    """Well-known interactive card types on recruitment platforms.

    Platforms must map their native card semantics to one of these values so
    the business layer can reason about intent uniformly.
    """

    EXCHANGE_RESUME = "exchange_resume"      # "交换简历" card (BOSS)
    EXCHANGE_CONTACT = "exchange_contact"    # "交换联系方式" card
    INTERVIEW_INVITE = "interview_invite"    # "面试邀约" card
    JOB_RECOMMEND = "job_recommend"          # HR 推荐岗位卡片
    UNKNOWN = "unknown"


class CardAction(StrEnum):
    """What to do with a conversation card."""

    ACCEPT = "accept"
    REJECT = "reject"
    VIEW = "view"


class PlatformProvider(StrEnum):
    """Supported recruitment platforms."""

    BOSS = "boss"
    LIEPIN = "liepin"
    ZHILIAN = "zhilian"
    LAGOU = "lagou"
    QIANCHENG = "qiancheng"
