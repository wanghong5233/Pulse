"""Score how well a scanned job fits the user's current preferences.

Pure policy component — owns no IO other than the :class:`LLMRouter`. Takes a
normalized scan item (whatever ``GreetService._normalize_scan_item`` produces)
+ the latest :class:`JobMemorySnapshot`, returns a structured
:class:`MatchResult`.

Design:

  * **LLM 主路径** (route=``classification``): 把 snapshot 渲染的 markdown
    片段拼进 system prompt, JD 拼进 user prompt, 通过 ``invoke_json`` 拿
    ``{score, verdict, matched_signals, concerns, reason}``。
  * **Heuristic 降级**: 当 LLM 不可用 / 返回非 JSON / 字段缺失时, 退回到
    keyword-substring 打分 + 硬性偏好检查 (城市 / 薪资下限)。保证 pipeline
    在离线/无 key 环境下仍能跑, 分数偏保守。

verdict 取值与下游行为:

    ``good``  → 强烈推荐打招呼; service 排在最前
    ``okay``  → 可以打招呼, 但提示用户确认
    ``poor``  → 不推荐, 低于 threshold 时直接丢弃
    ``skip``  → 命中用户黑名单或硬性偏好冲突, 必须丢弃

matcher 是否发射 stage 事件由调用方 (service 编排) 决定, matcher 本身不写
审计日志 — 只做 "输入→输出" 的纯函数, 方便单测。

见 ``docs/Pulse-DomainMemory与Tool模式.md`` §5.1 R2 / §5.2 性能边界。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pulse.core.llm.router import LLMRouter

from ..memory import JobMemorySnapshot

logger = logging.getLogger(__name__)


_VERDICTS: frozenset[str] = frozenset({"good", "okay", "poor", "skip"})


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Structured fit assessment for a single JD against user preferences."""

    score: float
    verdict: str  # one of _VERDICTS
    matched_signals: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "verdict": self.verdict,
            "matched_signals": list(self.matched_signals),
            "concerns": list(self.concerns),
            "reason": self.reason,
        }


class JobSnapshotMatcher:
    """LLM-backed fit scorer with a deterministic heuristic fallback."""

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
        """Score a single JD. Empty/invalid jobs get verdict='skip'."""
        title = str(job.get("title") or "").strip()
        if not title and not str(job.get("snippet") or "").strip():
            return MatchResult(
                score=0.0,
                verdict="skip",
                reason="empty job payload",
            )

        llm_result = self._match_with_llm(job=job, snapshot=snapshot, keyword=keyword)
        if llm_result is not None:
            return llm_result
        return self._match_with_heuristic(job=job, snapshot=snapshot, keyword=keyword)

    # ──────────────────────────────────────────────────────── LLM path

    def _match_with_llm(
        self,
        *,
        job: dict[str, Any],
        snapshot: JobMemorySnapshot | None,
        keyword: str,
    ) -> MatchResult | None:
        snapshot_md = snapshot.to_prompt_section() if snapshot is not None else "(no preferences set)"
        job_md = self._render_job(job, keyword=keyword)

        system_prompt = (
            "You are a job-fit scorer for an AI career assistant. "
            "Given the user's current preferences (as markdown) and a job posting, "
            "score how well the job matches.\n\n"
            "## Verdict policy (READ CAREFULLY — this is the #1 source of misjudgments)\n"
            "- **skip** ONLY when the JD text contains CLEAR, EXPLICIT EVIDENCE that a "
            "  hard constraint is violated. Examples that DO warrant skip:\n"
            "    * JD says 'base 北京', user prefers ['杭州','上海']  → skip (city mismatch).\n"
            "    * JD says '月薪 5-8K', user's salary_floor_monthly is 10K  → skip (ceiling < floor).\n"
            "    * JD explicitly targets '3 年以上工作经验 / 全职', user wants 'intern'  → skip.\n"
            "    * Company name appears on user's avoid_company list.\n"
            "- **DO NOT skip** when a field is merely absent / unknown ('salary: (not "
            "  provided)', snippet doesn't mention city). Missing != violating. In that case "
            "  use 'okay' (if other signals match) or 'poor' (if weak keyword fit), and put the "
            "  missing field into `concerns` so the user can decide. The user explicitly "
            "  wants breadth — filtering 5 out of 6 jobs because salary isn't disclosed "
            "  destroys the workflow.\n"
            "- **good / okay / poor** differ only in score & confidence; all three remain in "
            "  the candidate pool downstream.\n\n"
            "Respond with ONLY a JSON object. Schema:\n"
            '{"score": <int 0-100>, "verdict": "good|okay|poor|skip", '
            '"matched_signals": [<short strings>], '
            '"concerns": [<short strings>], '
            '"reason": "<one line>"}\n\n'
            f"## User preferences (current)\n{snapshot_md}"
        )
        user_prompt = f"## Job posting\n{job_md}\n\nReturn JSON only."

        parsed = self._llm.invoke_json(
            [
                _system(system_prompt),
                _user(user_prompt),
            ],
            route="classification",
        )
        if not isinstance(parsed, dict):
            return None

        try:
            score = float(parsed.get("score", 0))
        except (TypeError, ValueError):
            return None
        score = max(0.0, min(score, 100.0))

        verdict = str(parsed.get("verdict") or "").strip().lower()
        if verdict not in _VERDICTS:
            return None

        matched = _coerce_str_list(parsed.get("matched_signals"))
        concerns = _coerce_str_list(parsed.get("concerns"))
        reason = str(parsed.get("reason") or "").strip()[:400]
        return MatchResult(
            score=round(score, 1),
            verdict=verdict,
            matched_signals=matched,
            concerns=concerns,
            reason=reason or "llm_classification",
        )

    # ──────────────────────────────────────────────────────── heuristic path

    def _match_with_heuristic(
        self,
        *,
        job: dict[str, Any],
        snapshot: JobMemorySnapshot | None,
        keyword: str,
    ) -> MatchResult:
        """Substring-based keyword match + hard preference checks.

        降级路径, 打分保守: 最高 75 分, 避免 LLM 失败时误触发高置信动作。
        """
        title = str(job.get("title") or "")
        snippet = str(job.get("snippet") or "")
        haystack = f"{title}\n{snippet}"
        lowered = haystack.lower()

        score = 50.0
        matched: list[str] = []
        concerns: list[str] = []

        key = (keyword or "").strip().lower()
        if key and key in lowered:
            score += 15.0
            matched.append(f"keyword '{keyword}' in title/snippet")
        elif key:
            concerns.append(f"keyword '{keyword}' not found in JD")

        if snapshot is not None:
            locations = snapshot.hc_preferred_locations()
            if locations:
                city_hit = next(
                    (loc for loc in locations if loc and loc in haystack),
                    None,
                )
                if city_hit:
                    score += 8.0
                    matched.append(f"matches preferred_location {city_hit}")
                else:
                    concerns.append("preferred_location not confirmed from JD text")

            hit, which = snapshot.find_avoided_target_in(haystack)
            if hit:
                return MatchResult(
                    score=0.0,
                    verdict="skip",
                    matched_signals=matched,
                    concerns=[f"contains avoided target '{which}'"],
                    reason="heuristic: avoided target",
                )

        score = max(30.0, min(score, 75.0))
        verdict = "okay" if score >= 60 else "poor"
        return MatchResult(
            score=round(score, 1),
            verdict=verdict,
            matched_signals=matched,
            concerns=concerns,
            reason="heuristic_fallback",
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
            detail_md = f"\n- detail: |\n{_indent(detail_json[:1500], prefix='    ')}"

        return (
            f"- title: {title}\n"
            f"- company: {company}\n"
            f"- salary: {salary}\n"
            f"- user_searched_keyword: {keyword or '(none)'}\n"
            f"- snippet: {snippet[:1200]}"
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
