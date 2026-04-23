"""System domain skill schema.

Declares the *domain-level* capability surface for runtime / platform-level
utilities that are not tied to any business vertical (hello ping, feedback
loop, etc.).
"""

from __future__ import annotations

from typing import Any

SKILL_SCHEMA: dict[str, Any] = {
    "name": "system",
    "description": (
        "系统域技能包：面向 Agent Runtime 本身的平台级能力，例如健康探针、"
        "用户反馈回路等，不承载具体业务流程。"
    ),
    "subcapabilities": [
        {
            "name": "hello",
            "module": "hello",
            "description": "健康探针 / pingpong，用于链路连通性与默认回复",
            "intents": ["system.hello"],
            "examples": ["你好", "ping", "/hello"],
        },
        {
            "name": "feedback",
            "module": "feedback_loop",
            "description": "收集用户反馈并回写知识库，驱动行为进化",
            "intents": ["system.feedback.record", "system.feedback.list"],
            "examples": ["/feedback 这条回答不够准确", "看一下最近的反馈"],
        },
    ],
}
