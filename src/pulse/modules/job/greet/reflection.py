"""Reflexion-style keyword evolver for the greet trigger loop.

When the candidate pool comes back empty (or short of ``batch_size``), we do
NOT silently relax hard constraints — that would silently violate the user's
stated preferences. Instead we reflect on *why* the previous round produced
no shortlist, and let the LLM propose better search keywords for the next
scan. Hard constraints (city / experience_level / salary floor /
avoid_trait) stay constant across rounds.

This file owns one responsibility: turn a "round summary" into a small set
of fresh keywords. Service-layer is responsible for round budgeting,
audit emission and dedup.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pulse.core.llm.router import LLMRouter

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RoundSummary:
    """One round of greet trigger as fed back into reflection.

    ``skipped_examples`` should be a small (<= 8) list of ``{company,
    job_title, verdict, reason, hard_violations}`` dicts so the LLM can
    reason about *why* the previous scan produced nothing the user wanted.
    """

    keyword: str
    scanned_total: int
    shortlisted_total: int
    skipped_examples: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ReflectionPlan:
    """LLM's proposed next-round keywords + a short rationale."""

    next_keywords: list[str]
    rationale: str = ""


class ReflectionPlanner:
    """Evolve the search keyword based on the previous round's outcome.

    Failure policy:
        - LLM unavailable / invalid JSON → return empty plan; the caller
          stops the reflection loop fail-loud rather than guessing.
    """

    _ROUTE = "job_match"
    _MAX_KEYWORDS = 3

    def __init__(self, llm_router: LLMRouter) -> None:
        self._llm = llm_router

    def plan_next_keywords(
        self,
        *,
        original_user_intent: str,
        hard_constraints_md: str,
        round_history: list[RoundSummary],
        target_remaining: int,
        already_tried_keywords: list[str],
    ) -> ReflectionPlan:
        """Ask the LLM to reflect and propose next-round keywords.

        ``already_tried_keywords`` is fed back so the LLM does not loop on
        the same query string. ``hard_constraints_md`` is read-only for
        the LLM — it must NOT mutate them; we tell it that explicitly.
        """
        if not round_history:
            return ReflectionPlan(next_keywords=[])

        system_prompt = (
            "You are a reflexion planner for a job-search agent. The agent "
            "tried one or more search keywords against BOSS, but the LLM-as-Judge "
            "matcher did not shortlist enough JDs to meet the user's target.\n\n"
            "## Your job\n"
            "Read the previous round's outcome (skipped examples + their reasons) and "
            "propose 1-3 NEW search keywords likely to surface JDs that match the "
            "user's actual intent.\n\n"
            "## Hard rules\n"
            "1. The user's hard constraints (city, experience_level, salary floor, "
            "   avoid_trait) are FIXED. Do NOT propose keywords that try to widen "
            "   them. If the previous round was killed by 'avoid_trait:大厂', the "
            "   answer is NOT 'allow 大厂' — it is 'find more small/early-stage "
            "   keyword variants'.\n"
            "2. Do NOT repeat any keyword from `already_tried_keywords`.\n"
            "3. Each keyword must be a short search phrase a human would actually "
            "   type into BOSS (Chinese OK, mix OK, 2-12 chars). No punctuation, "
            "   no boolean operators, no quotes.\n"
            "4. Prefer keywords that change the *angle* (related role names, "
            "   adjacent tech stacks, alternative phrasings) over keywords that "
            "   only tighten or loosen the original.\n\n"
            "## Output schema (JSON only)\n"
            '{\n'
            '  "next_keywords": [<short search phrases, 1-3 items>],\n'
            '  "rationale": "<one short line: what was wrong with prev round, '
            'and what each new keyword targets>"\n'
            "}\n"
        )

        history_md = self._render_round_history(round_history)
        tried_md = self._render_tried(already_tried_keywords)

        user_prompt = (
            f"## Original user intent\n{original_user_intent or '(unspecified)'}\n\n"
            f"## Hard constraints (immutable)\n{hard_constraints_md or '(none)'}\n\n"
            f"## Round history\n{history_md}\n\n"
            f"## Already-tried keywords (DO NOT REPEAT)\n{tried_md}\n\n"
            f"## Target remaining\n{target_remaining}\n\n"
            "Return JSON only."
        )

        try:
            parsed = self._llm.invoke_json(
                [
                    _system(system_prompt),
                    _user(user_prompt),
                ],
                route=self._ROUTE,
            )
        except (RuntimeError, ValueError, TypeError) as exc:
            logger.warning("reflection planner LLM call failed: %s", exc)
            return ReflectionPlan(next_keywords=[])

        if not isinstance(parsed, dict):
            return ReflectionPlan(next_keywords=[])

        raw = parsed.get("next_keywords")
        if not isinstance(raw, list):
            return ReflectionPlan(next_keywords=[])

        tried_set = {kw.strip().casefold() for kw in already_tried_keywords if kw}
        out: list[str] = []
        for item in raw:
            text = str(item or "").strip()
            if not text:
                continue
            if text.casefold() in tried_set:
                continue
            if any(text.casefold() == kept.casefold() for kept in out):
                continue
            out.append(text[:60])
            if len(out) >= self._MAX_KEYWORDS:
                break

        return ReflectionPlan(
            next_keywords=out,
            rationale=str(parsed.get("rationale") or "").strip()[:240],
        )

    @staticmethod
    def _render_round_history(rounds: list[RoundSummary]) -> str:
        lines: list[str] = []
        for idx, summary in enumerate(rounds, start=1):
            lines.append(
                f"### Round {idx}\n"
                f"- keyword: {summary.keyword!r}\n"
                f"- scanned_total: {summary.scanned_total}\n"
                f"- shortlisted_total: {summary.shortlisted_total}"
            )
            if summary.skipped_examples:
                lines.append("- skipped_examples:")
                for row in summary.skipped_examples[:8]:
                    chunk = json.dumps(row, ensure_ascii=False)
                    if len(chunk) > 220:
                        chunk = chunk[:217] + "...]}"
                    lines.append(f"  - {chunk}")
        return "\n".join(lines) or "(empty)"

    @staticmethod
    def _render_tried(tried: list[str]) -> str:
        if not tried:
            return "(none)"
        return "\n".join(f"- {kw}" for kw in tried)


def _system(content: str) -> Any:
    from langchain_core.messages import SystemMessage
    return SystemMessage(content=content)


def _user(content: str) -> Any:
    from langchain_core.messages import HumanMessage
    return HumanMessage(content=content)


__all__ = ["ReflectionPlan", "ReflectionPlanner", "RoundSummary"]
