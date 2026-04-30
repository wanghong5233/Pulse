"""Reflection planner contract tests.

Pins the prompt + output contract:
  * The planner must NOT propose keywords that violate hard constraints
    (the LLM is told they are immutable).
  * It must dedup against ``already_tried_keywords``.
  * Invalid LLM output → empty plan; the caller stops the loop, never
    silently fabricates keywords.
"""
from __future__ import annotations

from typing import Any

from pulse.modules.job.greet.reflection import (
    ReflectionPlanner,
    RoundSummary,
)


class _CapturingLLM:
    def __init__(self, response: Any) -> None:
        self.last_messages: list[Any] = []
        self.last_route: str = ""
        self._response = response

    def invoke_json(
        self,
        prompt_value: Any,
        *,
        route: str = "default",
        default: Any = None,
    ) -> Any:
        self.last_messages = list(prompt_value)
        self.last_route = route
        return self._response if self._response is not None else default

    @property
    def system_prompt_text(self) -> str:
        for msg in self.last_messages:
            if type(msg).__name__ == "SystemMessage":
                return str(getattr(msg, "content", "") or "")
        return ""

    @property
    def user_prompt_text(self) -> str:
        for msg in self.last_messages:
            if type(msg).__name__ == "HumanMessage":
                return str(getattr(msg, "content", "") or "")
        return ""


def _round(keyword: str, scanned: int = 10, shortlisted: int = 0,
           skipped: list[dict[str, Any]] | None = None) -> RoundSummary:
    return RoundSummary(
        keyword=keyword,
        scanned_total=scanned,
        shortlisted_total=shortlisted,
        skipped_examples=skipped or [],
    )


def test_planner_returns_dedup_keywords_and_caps_at_three() -> None:
    llm = _CapturingLLM(response={
        "next_keywords": [
            "AI Agent 实习",          # dup of already-tried, must drop
            "大模型应用工程师 实习",   # keep
            "LangChain 实习",          # keep
            "Agent 工程师 实习",       # keep, fills cap
            "向量检索 实习",           # over cap, must drop
        ],
        "rationale": "previous round was killed by 大厂 trait; widen along role",
    })
    planner = ReflectionPlanner(llm)

    plan = planner.plan_next_keywords(
        original_user_intent="大模型应用开发 agent 实习",
        hard_constraints_md="(immutable hard constraints…)",
        round_history=[_round("AI Agent 实习", scanned=10, shortlisted=0)],
        target_remaining=2,
        already_tried_keywords=["AI Agent 实习"],
    )

    assert plan.next_keywords == [
        "大模型应用工程师 实习",
        "LangChain 实习",
        "Agent 工程师 实习",
    ]
    assert plan.rationale.startswith("previous round was killed")


def test_planner_returns_empty_plan_on_invalid_llm_response() -> None:
    """Fail-loud: invalid JSON → empty plan; caller stops the loop and
    speaks the truth, never substitutes a guessed keyword."""
    llm = _CapturingLLM(response="not-a-dict")
    planner = ReflectionPlanner(llm)

    plan = planner.plan_next_keywords(
        original_user_intent="x",
        hard_constraints_md="",
        round_history=[_round("x")],
        target_remaining=2,
        already_tried_keywords=[],
    )

    assert plan.next_keywords == []


def test_planner_returns_empty_plan_on_empty_round_history() -> None:
    llm = _CapturingLLM(response={"next_keywords": ["foo"]})
    planner = ReflectionPlanner(llm)

    plan = planner.plan_next_keywords(
        original_user_intent="x",
        hard_constraints_md="",
        round_history=[],
        target_remaining=2,
        already_tried_keywords=[],
    )

    assert plan.next_keywords == []


def test_planner_prompt_pins_hard_constraint_immutability() -> None:
    """Hard rule: the LLM must be explicitly told it cannot relax the
    user's hard constraints. Without this pin, the planner sometimes
    suggests '允许大厂' / '北京 fallback' which silently violates the
    user's stated preferences."""
    llm = _CapturingLLM(response={"next_keywords": ["foo"]})
    planner = ReflectionPlanner(llm)

    planner.plan_next_keywords(
        original_user_intent="大模型应用开发 agent 实习",
        hard_constraints_md="city: 杭州/上海; experience_level: intern",
        round_history=[_round("AI Agent 实习", skipped=[
            {"company": "字节跳动", "verdict": "skip",
             "reason": "avoid_trait:大厂"}
        ])],
        target_remaining=2,
        already_tried_keywords=["AI Agent 实习"],
    )

    sys = llm.system_prompt_text
    assert "FIXED" in sys.upper() or "immutable" in sys.lower(), (
        "system prompt must declare hard constraints as fixed; otherwise "
        "the planner sometimes suggests relaxing them."
    )
    assert "DO NOT REPEAT" in llm.user_prompt_text.upper() or \
           "already-tried" in llm.user_prompt_text.lower()


def test_planner_uses_job_match_route() -> None:
    llm = _CapturingLLM(response={"next_keywords": ["foo"]})
    planner = ReflectionPlanner(llm)

    planner.plan_next_keywords(
        original_user_intent="x",
        hard_constraints_md="",
        round_history=[_round("x")],
        target_remaining=1,
        already_tried_keywords=[],
    )

    assert llm.last_route == "job_match"
