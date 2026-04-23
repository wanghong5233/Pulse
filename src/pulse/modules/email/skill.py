"""Email domain skill schema.

Declares the *domain-level* capability surface for the email domain.
Consumed by the Brain / router to drive two-level routing.
"""

from __future__ import annotations

from typing import Any

SKILL_SCHEMA: dict[str, Any] = {
    "name": "email",
    "description": (
        "邮件域技能包：通过 IMAP 拉取邮件、基于 LLM 分类与事件抽取，"
        "将招聘/通知/面试类邮件结构化进入本地知识库。"
    ),
    "subcapabilities": [
        {
            "name": "tracker",
            "module": "email_tracker",
            "description": "IMAP 拉取 + LLM 分类 + DB 持久化的邮件追踪能力",
            "intents": [
                "email.tracker.fetch",
                "email.tracker.process",
            ],
            "examples": [
                "帮我同步一下邮箱里的面试邀约",
                "/email fetch",
            ],
        },
    ],
}
