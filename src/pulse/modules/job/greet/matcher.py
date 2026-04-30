"""LLM-as-Judge fit classifier for scanned jobs.

Pure policy component — owns no IO other than the :class:`LLMRouter`. Takes a
normalized scan item (whatever ``GreetService._normalize_scan_item`` produces)
+ the latest :class:`JobMemorySnapshot`, returns a structured
:class:`MatchResult`.

Design contract:

  * **Ordinal verdict, not floating-point score** — LLM-as-Judge research
    (G-Eval / MT-Bench / Anthropic constitutional evals) consistently shows
    ordinal labels are far more stable than 0-100 numeric ratings. The LLM
    has no calibrated meaning of "70 vs 65"; it does have a calibrated
    meaning of "good vs okay vs poor". We let it classify, not score.

  * **Score is a code-side ranking projection** — a deterministic mapping
    over ``verdict + hard_violations + reason strength`` produces a stable
    score used **only for sorting** within the candidate pool. Filtering
    (whether a JD is shortlisted at all) is decided by **verdict
    membership**, not by score >= threshold.

  * **No heuristic fallback** — when the LLM is unavailable or returns
    invalid JSON, the JD is skipped (``verdict='skip'``). Auto-greet is a
    real outbound action; degrading to keyword heuristics is worse than
    failing loud.

verdict semantics:

    ``good``  → strong match; rank highest in candidate pool.
    ``okay``  → acceptable match; included in candidate pool below ``good``.
    ``poor``  → weak match; excluded from candidate pool but kept in audit.
    ``skip``  → constraint violation or empty payload; excluded.

Service layer reads ``verdict``; ``score`` is exposed for telemetry &
deterministic ordering only. See ``architecture.md`` for the cascading
filter pipeline this matcher sits inside.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from typing import Any

from pulse.core.llm.router import LLMRouter
from pulse.core.tokenizer import token_preview

from ..memory import JobMemorySnapshot

logger = logging.getLogger(__name__)

_VERDICTS: frozenset[str] = frozenset({"good", "okay", "poor", "skip"})

# Code-side projection of verdict → ranking score. The numbers are
# arbitrary monotone constants whose only job is to give a stable
# ordering when the candidate pool needs sorting (good > okay >
# poor > skip). The LLM never sees these numbers and never reasons
# about them.
_VERDICT_RANK_BASE: dict[str, float] = {
    "good": 80.0,
    "okay": 60.0,
    "poor": 30.0,
    "skip": 0.0,
}

# When ``hard_violations`` is non-empty, the JD must rank below any
# clean candidate of the same verdict. We subtract a small per-violation
# penalty so multi-violation rows sort even lower, but never cross into
# the next verdict tier.
_HARD_VIOLATION_PENALTY: float = 5.0


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Structured fit assessment for a single JD against user preferences.

    ``score`` is **derived** from ``verdict + hard_violations``; it is *not*
    an LLM output. Treat it as an opaque ranking handle, not a calibrated
    confidence signal.
    """

    score: float
    verdict: str  # one of _VERDICTS
    matched_signals: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    hard_violations: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "verdict": self.verdict,
            "matched_signals": list(self.matched_signals),
            "concerns": list(self.concerns),
            "hard_violations": list(self.hard_violations),
            "reason": self.reason,
        }


def _project_score(verdict: str, hard_violations: list[str]) -> float:
    """Map (verdict, hard_violations) → deterministic ranking score.

    Pure function — same inputs always produce the same number. Service
    layer must not mutate this; if a different ranking is needed, change
    the table here so the contract stays one-source-of-truth.
    """
    base = _VERDICT_RANK_BASE.get(verdict, 0.0)
    if not hard_violations:
        return base
    penalty = _HARD_VIOLATION_PENALTY * min(len(hard_violations), 5)
    # Floor at the next verdict tier so penalty cannot move a 'good'
    # below an 'okay'. _VERDICT_RANK_BASE is sorted descending, so the
    # adjacent floor is the next-lower tier value (or 0 for 'skip').
    tiers = sorted(_VERDICT_RANK_BASE.values(), reverse=True)
    floor = 0.0
    for value in tiers:
        if value < base:
            floor = value
            break
    return max(floor, base - penalty)


class JobSnapshotMatcher:
    """LLM-as-Judge ordinal classifier; invalid LLM output skips the JD."""

    def __init__(self, llm_router: LLMRouter) -> None:
        self._llm = llm_router

    # ──────────────────────────────────────────────────────── public

    def match(
        self,
        *,
        job: dict[str, Any],
        snapshot: JobMemorySnapshot | None,
        keyword: str = "",
    ) -> MatchResult:
        """Classify a single JD. Empty/invalid jobs get verdict='skip'."""
        title = str(job.get("title") or "").strip()
        if not title and not str(job.get("snippet") or "").strip():
            return MatchResult(
                score=_project_score("skip", []),
                verdict="skip",
                reason="empty job payload",
            )

        llm_result = self._classify_with_llm(job=job, snapshot=snapshot, keyword=keyword)
        if llm_result is not None:
            return llm_result
        return MatchResult(
            score=_project_score("skip", []),
            verdict="skip",
            concerns=["LLM matcher unavailable or returned invalid JSON"],
            reason="llm_required_no_heuristic_autosend",
        )

    # ──────────────────────────────────────────────────────── LLM path

    def _classify_with_llm(
        self,
        *,
        job: dict[str, Any],
        snapshot: JobMemorySnapshot | None,
        keyword: str,
    ) -> MatchResult | None:
        snapshot_md = snapshot.to_prompt_section() if snapshot is not None else "(no preferences set)"
        job_md = self._render_job(job, keyword=keyword)

        system_prompt = (
            "You are a job-fit classifier for an AI career assistant. "
            "Given the user's current preferences and one job posting, "
            "return an ordinal verdict — DO NOT produce a numeric score.\n\n"
            "## Why ordinal, not numeric\n"
            "Numeric scores like '72/100' are not stable across calls; you do not "
            "have a calibrated meaning for '72 vs 68'. You DO have a calibrated "
            "meaning for the four labels below. Pick one label honestly; the "
            "ranking is the host's job, not yours.\n\n"
            "## Label definitions\n"
            "- **good** — fits the user's intent on multiple dimensions (role, "
            "  seniority, location, salary band, company trait); no hard violations.\n"
            "- **okay** — acceptable; some signals match, some are unknown but not "
            "  contradicted. Use this when the JD is plausible but not exciting; "
            "  it stays in the candidate pool.\n"
            "- **poor** — weak fit; multiple signals are off (e.g. wrong direction, "
            "  vague description, uncertain seniority). Excluded from outreach but "
            "  kept in the audit log.\n"
            "- **skip** — clear evidence the JD violates a hard user constraint. "
            "  See §Evidence below for what counts as 'clear evidence'.\n\n"
            "## Evidence rules for skip\n"
            "Two flavors of evidence count. Use BOTH where applicable:\n"
            "\n"
            "  (a) **Literal field mismatch** — read directly off the JD fields:\n"
            "    * JD says 'base 北京', user prefers ['杭州','上海']  → skip (city mismatch).\n"
            "    * JD says '月薪 5-8K', user's salary_floor_monthly is 10K  → skip (ceiling < floor).\n"
            "    * JD explicitly targets '3 年以上工作经验 / 全职', user wants 'intern'  → skip.\n"
            "    * Company name appears on user's avoid_company list.\n"
            "\n"
            "  (b) **Trait judgement using your world knowledge** — when the user's "
            "preferences include `avoid_trait` / `favor_trait` items (free-text labels "
            "such as '大厂' / '外包' / '广告业务' / '小厂或初创' / '业务垂直匹配'), "
            "apply your world knowledge of the company to decide whether the JD "
            "obviously belongs to that trait, then act on the user's intent:\n"
            "    * If the company plainly belongs to an `avoid_trait` (e.g. user says "
            "      avoid_trait='大厂' and the company is 字节跳动 / 阿里巴巴 / 腾讯 / 美团 / "
            "      百度 / 京东 / 拼多多 / 华为 / 小米 / 网易 / 滴滴 / 快手 / 小红书 / B 站 …) "
            "      → skip with reason='avoid_trait:<trait>'.\n"
            "    * If the company plainly belongs to a `favor_trait` (e.g. user wants "
            "      '小厂或初创' and the company is a small / early-stage startup) → use "
            "      verdict='good' and add a matched_signal naming the trait.\n"
            "    * If you are NOT confident which side a company falls on, do NOT skip — "
            "      add the trait to `concerns` and pick okay / poor by other signals. "
            "      Skipping on uncertain trait inference is worse than letting the user "
            "      see it.\n"
            "\n"
            "## Do-not-skip rules\n"
            "- Missing != violating. Don't skip just because salary is '(not provided)' "
            "  or city isn't in the snippet — return okay or poor and put the missing "
            "  field into `concerns`. The user explicitly wants breadth.\n"
            "- Don't downgrade a literal-fit JD to skip on subjective dislike.\n"
            "\n"
            "## Output schema (JSON only, no commentary)\n"
            '{\n'
            '  "verdict": "good"|"okay"|"poor"|"skip",\n'
            '  "matched_signals": [<short strings, may name a favor_trait>],\n'
            '  "concerns": [<short strings, may name an uncertain trait>],\n'
            '  "hard_violations": [<machine-readable hard-constraint tags, e.g. '
            '"avoid_trait:大厂", "hc_location:北京!=杭州|上海", "hc_salary:6K<7K">],\n'
            '  "reason": "<one short line; if skipped on a trait, prefix with avoid_trait:<name>>"\n'
            "}\n\n"
            "If `hard_violations` is non-empty, your verdict MUST be `skip`.\n\n"
            f"## User preferences (current)\n{snapshot_md}"
        )
        user_prompt = f"## Job posting\n{job_md}\n\nReturn JSON only."

        parsed = self._llm.invoke_json(
            [
                _system(system_prompt),
                _user(user_prompt),
            ],
            route="job_match",
        )
        if not isinstance(parsed, dict):
            return None

        verdict = str(parsed.get("verdict") or "").strip().lower()
        if verdict not in _VERDICTS:
            return None

        matched = _coerce_str_list(parsed.get("matched_signals"))
        concerns = _coerce_str_list(parsed.get("concerns"))
        hard_violations = _coerce_str_list(parsed.get("hard_violations"))
        reason = str(parsed.get("reason") or "").strip()[:400]

        # Self-consistency guard: if the LLM emitted hard_violations but
        # forgot to flip verdict to skip, normalize. The contract is
        # "violations ⇒ skip" and we enforce it deterministically.
        if hard_violations and verdict != "skip":
            logger.info(
                "matcher: forcing verdict=skip due to hard_violations=%s "
                "(LLM said verdict=%s)",
                hard_violations,
                verdict,
            )
            verdict = "skip"

        return MatchResult(
            score=_project_score(verdict, hard_violations),
            verdict=verdict,
            matched_signals=matched,
            concerns=concerns,
            hard_violations=hard_violations,
            reason=reason or "llm_classification",
        )

    # ──────────────────────────────────────────────────────── helpers

    @staticmethod
    def _render_job(job: dict[str, Any], *, keyword: str) -> str:
        title = str(job.get("title") or "").strip()
        company = str(job.get("company") or "").strip()
        salary = str(job.get("salary") or "").strip() or "(not provided)"
        snippet = str(job.get("snippet") or "").strip()
        detail = job.get("detail") if isinstance(job.get("detail"), dict) else {}
        detail_md = ""
        if detail:
            try:
                detail_json = json.dumps(detail, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                detail_json = str(detail)
            detail_md = "\n- detail: |\n" + _indent(
                token_preview(detail_json, max_tokens=700),
                prefix="    ",
            )

        return (
            f"- title: {title}\n"
            f"- company: {company}\n"
            f"- salary: {salary}\n"
            f"- user_searched_keyword: {keyword or '(none)'}\n"
            f"- snippet: {token_preview(snippet, max_tokens=600)}"
            f"{detail_md}"
        )


def _coerce_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(text[:160])
    return out


def _indent(text: str, *, prefix: str) -> str:
    return "\n".join(f"{prefix}{line}" for line in text.splitlines())


def _system(content: str) -> Any:
    from langchain_core.messages import SystemMessage
    return SystemMessage(content=content)


def _user(content: str) -> Any:
    from langchain_core.messages import HumanMessage
    return HumanMessage(content=content)


__all__ = ["JobSnapshotMatcher", "MatchResult"]
