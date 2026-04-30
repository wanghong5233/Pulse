"""Matcher prompt-contract tests: trait judgement is delegated to the LLM.

Background (post-mortem 2026-04-28 trace_753fecf70cc5): user said
"暂时战略性放弃大厂暑期实习" but greet patrol kept investing in 字节跳动
/ 阿里巴巴 / 阿里云. Root cause analysis (see commit message + matcher.py
Verdict policy doc) split the failure into two layers:

1. The snapshot data layer must NOT decide whether 字节跳动 == 大厂.
   That's now pinned by ``test_snapshot_avoid_company.py``.
2. The matcher LLM stage MUST be told it is allowed (in fact, required)
   to use its world knowledge to expand mass-noun avoid_trait /
   favor_trait labels onto specific companies. Otherwise the trait
   floats in the prompt as a soft hint and the LLM sometimes picks 字节
   anyway.

This file pins layer 2 — the **prompt contract** — without coupling to the
LLM's actual judgement (which would be a same-source rehearsal test, see
testing constitution §虚假测试). Concretely:

* The system prompt must explicitly delegate trait judgement to the LLM,
  using language the LLM can act on (we check for the key directives, not
  any single sentence).
* The user's avoid_trait / favor_trait items must be visible in the prompt
  the LLM actually receives. If the trait never reaches the LLM, no amount
  of policy text saves us.
"""
from __future__ import annotations

from typing import Any

from pulse.modules.job.greet.matcher import JobSnapshotMatcher
from pulse.modules.job.memory import (
    HardConstraints,
    JobMemorySnapshot,
    MemoryItem,
)


class _CapturingLLM:
    """Captures the messages we send to the matcher LLM, returns canned JSON.

    We intentionally do not assert on what the LLM "would decide" — that's
    LLM-vendor territory. We only assert on (a) the prompt text we send,
    and (b) that the matcher correctly maps a structured LLM response back
    into ``MatchResult`` fields.
    """

    def __init__(self, response: dict[str, Any] | None = None) -> None:
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
            cls_name = type(msg).__name__
            if cls_name == "SystemMessage":
                return str(getattr(msg, "content", "") or "")
        return ""

    @property
    def user_prompt_text(self) -> str:
        for msg in self.last_messages:
            cls_name = type(msg).__name__
            if cls_name == "HumanMessage":
                return str(getattr(msg, "content", "") or "")
        return ""


def _avoid_trait(target: str, content: str = "") -> MemoryItem:
    return MemoryItem(
        id=f"item-avoid-trait-{target}",
        type="avoid_trait",
        target=target,
        content=content or f"avoid trait {target}",
        raw_text=content or f"avoid trait {target}",
        valid_from="2026-04-28T00:00:00+00:00",
        valid_until=None,
        superseded_by=None,
        created_at="2026-04-28T00:00:00+00:00",
    )


def _favor_trait(target: str, content: str = "") -> MemoryItem:
    return MemoryItem(
        id=f"item-favor-trait-{target}",
        type="favor_trait",
        target=target,
        content=content or f"favor trait {target}",
        raw_text=content or f"favor trait {target}",
        valid_from="2026-04-28T00:00:00+00:00",
        valid_until=None,
        superseded_by=None,
        created_at="2026-04-28T00:00:00+00:00",
    )


def _snapshot_with_traits() -> JobMemorySnapshot:
    return JobMemorySnapshot(
        workspace_id="job.test",
        hard_constraints=HardConstraints(
            preferred_location=["杭州", "上海"],
            target_roles=["大模型应用开发"],
            experience_level="intern",
        ),
        memory_items=[
            _avoid_trait("大厂", "暂时战略性放弃大厂暑期实习"),
            _favor_trait("小厂或初创", "想找小厂或初创做深度垂直实习"),
        ],
    )


def _job(*, company: str, title: str = "AI Agent 实习生") -> dict[str, Any]:
    return {
        "title": title,
        "company": company,
        "salary": "200-400元/天",
        "snippet": "做大模型应用方向的研发实习, 杭州办公.",
    }


# ───────────────────────────── prompt contract


def test_system_prompt_explicitly_delegates_trait_judgement_to_llm_world_knowledge() -> None:
    """The prompt must tell the LLM to *use world knowledge* on
    avoid_trait / favor_trait. Without this directive, observed behavior
    was: trait floats as a soft hint, LLM keeps approving 字节 / 阿里."""
    llm = _CapturingLLM(response=None)  # forces matcher to its skip fallback
    matcher = JobSnapshotMatcher(llm)

    matcher.match(
        job=_job(company="字节跳动"),
        snapshot=_snapshot_with_traits(),
        keyword="大模型应用开发",
    )

    sys_prompt = llm.system_prompt_text
    assert sys_prompt, "matcher must invoke LLM with a non-empty system prompt"
    assert "world knowledge" in sys_prompt.lower(), (
        "system prompt must explicitly invite the LLM to use world knowledge "
        "for trait expansion (we observed soft-hint failures otherwise)."
    )
    assert "avoid_trait" in sys_prompt and "favor_trait" in sys_prompt, (
        "Verdict policy must name the two trait item types so the LLM knows "
        "which snapshot fields to act on."
    )
    assert "hard_violations" in sys_prompt, (
        "system prompt schema must expose hard_violations so downstream can "
        "apply deterministic veto on explicit constraint breaks."
    )
    # The prompt's few-shot examples seed the LLM toward "skip when company "
    # plainly belongs to an avoid_trait" — at least one canonical example
    # of a 大厂 must be present so the model learns the pattern.
    assert "大厂" in sys_prompt, "trait few-shot anchor missing"


def test_user_avoid_and_favor_traits_are_visible_in_prompt_to_llm() -> None:
    """The trait CONTENT itself (not just policy text) must reach the LLM,
    otherwise the world-knowledge stage has nothing to act on."""
    llm = _CapturingLLM(response=None)
    matcher = JobSnapshotMatcher(llm)

    matcher.match(
        job=_job(company="字节跳动"),
        snapshot=_snapshot_with_traits(),
        keyword="大模型应用开发",
    )

    sys_prompt = llm.system_prompt_text
    assert "暂时战略性放弃大厂" in sys_prompt or "大厂" in sys_prompt
    assert "小厂或初创" in sys_prompt
    assert "杭州" in sys_prompt and "上海" in sys_prompt


def test_matcher_uses_job_match_route() -> None:
    """Routing pin: matcher must use the job_match LLM route (cost / model
    selection lives there). Regression caught when route was accidentally
    renamed to 'classification' during a refactor."""
    llm = _CapturingLLM(response=None)
    matcher = JobSnapshotMatcher(llm)

    matcher.match(
        job=_job(company="字节跳动"),
        snapshot=_snapshot_with_traits(),
        keyword="大模型应用开发",
    )

    assert llm.last_route == "job_match"


# ───────────────────────────── output mapping


def test_matcher_propagates_llm_skip_verdict_into_match_result() -> None:
    """Output contract: when the LLM does decide to skip on a trait, the
    structured ``MatchResult`` carries the verdict / reason / concerns so
    the upstream service can audit which trait killed the candidate.

    Score is **derived** from (verdict, hard_violations) — LLM never
    produces it. Skip + any hard_violations → floor at 0.0.
    """
    llm = _CapturingLLM(response={
        "verdict": "skip",
        "matched_signals": [],
        "concerns": ["avoid_trait:大厂"],
        "hard_violations": ["avoid_trait:大厂"],
        "reason": "avoid_trait:大厂 — 字节跳动 是大厂",
    })
    matcher = JobSnapshotMatcher(llm)

    result = matcher.match(
        job=_job(company="字节跳动"),
        snapshot=_snapshot_with_traits(),
        keyword="大模型应用开发",
    )

    assert result.verdict == "skip"
    assert result.score == 0.0  # code-projected, not from LLM
    assert result.concerns == ["avoid_trait:大厂"]
    assert result.hard_violations == ["avoid_trait:大厂"]
    assert "avoid_trait" in result.reason


def test_matcher_propagates_llm_okay_verdict_with_favor_trait_signal() -> None:
    llm = _CapturingLLM(response={
        "verdict": "okay",
        "matched_signals": ["favor_trait:小厂或初创"],
        "concerns": [],
        "hard_violations": [],
        "reason": "early-stage AI startup matches favor_trait",
    })
    matcher = JobSnapshotMatcher(llm)

    result = matcher.match(
        job=_job(company="某创业公司", title="LLM Agent 研发实习生"),
        snapshot=_snapshot_with_traits(),
        keyword="大模型应用开发",
    )

    assert result.verdict == "okay"
    assert "favor_trait:小厂或初创" in result.matched_signals
    # Score is the code-side ranking projection of verdict='okay'.
    assert result.score == 60.0


def test_matcher_score_is_code_projection_not_llm_output() -> None:
    """Behavioral contract (NOT same-source constant rehearsal).

    The behavior under test is: a stray LLM-supplied ``score`` MUST NOT
    leak into ``MatchResult``. We construct a wrong score (99) on the
    LLM side, then assert the result's score does not equal 99 — anything
    else would be the regression we're guarding against. The exact
    numeric value (80.0) is incidental; if the projection table changes,
    update this assertion alongside the matcher.
    """
    llm = _CapturingLLM(response={
        "score": 99,  # deliberately wrong, MUST be ignored by matcher
        "verdict": "good",
        "matched_signals": ["agent role"],
        "concerns": [],
        "hard_violations": [],
        "reason": "ok",
    })
    matcher = JobSnapshotMatcher(llm)

    result = matcher.match(
        job=_job(company="某小厂"),
        snapshot=_snapshot_with_traits(),
        keyword="大模型应用开发",
    )

    assert result.verdict == "good"
    assert result.score != 99.0, (
        "matcher leaked the LLM-side score; verdict→score projection broken"
    )
    assert 0.0 < result.score <= 100.0


def test_matcher_forces_skip_when_llm_emits_hard_violations_but_keeps_other_verdict() -> None:
    """Self-consistency guard: if the LLM produces hard_violations but
    forgets to flip verdict to skip, code must normalize. The contract
    is 'violations ⇒ skip' — no soft compromise."""
    llm = _CapturingLLM(response={
        "verdict": "okay",
        "matched_signals": [],
        "concerns": [],
        "hard_violations": ["hc_location:北京!=杭州|上海"],
        "reason": "city mismatch",
    })
    matcher = JobSnapshotMatcher(llm)

    result = matcher.match(
        job=_job(company="某公司"),
        snapshot=_snapshot_with_traits(),
        keyword="大模型应用开发",
    )

    assert result.verdict == "skip", (
        "verdict MUST be normalized to skip when hard_violations is "
        "non-empty; otherwise downstream selection might still admit it."
    )
    assert result.score == 0.0
    assert result.hard_violations == ["hc_location:北京!=杭州|上海"]


def test_matcher_falls_back_to_skip_when_llm_returns_garbage() -> None:
    """No-heuristic guarantee: if the LLM is unreachable / returns
    non-JSON, matcher must NOT silently approve. ``skip`` is the safe
    fallback so we never autosend a greet on a guess."""
    llm = _CapturingLLM(response=None)
    matcher = JobSnapshotMatcher(llm)

    result = matcher.match(
        job=_job(company="字节跳动"),
        snapshot=_snapshot_with_traits(),
        keyword="大模型应用开发",
    )

    assert result.verdict == "skip"
    assert "llm" in result.reason.lower() or "unavailable" in (
        " ".join(result.concerns).lower()
    )
