"""Service-layer reflexion loop contract tests.

When the first scan+match round produces fewer shortlisted JDs than the
caller's batch_size target, the service must:

* call the reflection planner with the prior round's skip reasons,
* re-scan with the LLM-proposed keywords (NOT a heuristic relaxation
  of hard constraints),
* merge the new candidates while deduping by ``source_url``,
* emit a ``module.job_greet.trigger.reflection`` event each iteration,
* stop after at most ``_REFLECTION_MAX_ROUNDS`` reflection rounds even
  if still under target — fail-loud, never an infinite loop,
* emit ``module.job_greet.match.candidate`` events for every JD that
  reaches the matcher (shortlisted or not), so post-mortem can answer
  "really nothing matched" vs "rule misfire".
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from pulse.modules.job._connectors.base import JobPlatformConnector
from pulse.modules.job.greet.matcher import MatchResult
from pulse.modules.job.greet.reflection import ReflectionPlan
from pulse.modules.job.greet.repository import GreetRepository
from pulse.modules.job.greet.service import GreetPolicy, JobGreetService


# ──────────────────────────── fakes


class _MultiKeywordConnector(JobPlatformConnector):
    """Returns a different fixed list per scanned keyword.

    Lets us pin "what did the service ask BOSS for" — first round vs
    reflection round — without coupling to BOSS markup.
    """

    def __init__(self, keyword_to_items: dict[str, list[dict[str, Any]]]) -> None:
        self._keyword_to_items = keyword_to_items
        self.greet_attempts = 0
        self.scan_keyword_log: list[str] = []

    @property
    def provider_name(self) -> str:
        return "test-connector"

    @property
    def execution_ready(self) -> bool:
        return True

    def health(self) -> dict[str, Any]:
        return {"ok": True}

    def check_login(self) -> dict[str, Any]:
        return {"ok": True}

    def scan_jobs(
        self,
        *,
        keyword: str,
        max_items: int | None = None,
        max_pages: int | None = None,
        target_count: int | None = None,
        evaluation_cap: int | None = None,
        scroll_plateau_rounds: int | None = None,
        job_type: str = "all",
        city: str | None = None,
    ) -> dict[str, Any]:
        _ = (
            max_items, max_pages, target_count,
            evaluation_cap, scroll_plateau_rounds, job_type, city,
        )
        self.scan_keyword_log.append(keyword)
        items = list(self._keyword_to_items.get(keyword, []))
        # Sidebar fully consumed in one shot — every test stub
        # returns its full corpus per call, so reflection (gated on
        # exhausted=True) can run if the test wants it to.
        return {
            "ok": True,
            "items": items,
            "pages_scanned": 1,
            "scroll_count": 0,
            "exhausted": True,
            "source": "test-connector",
            "errors": [],
        }

    def fetch_job_detail(self, *, job_id: str, source_url: str) -> dict[str, Any]:
        _ = job_id, source_url
        return {"ok": True, "detail": {}, "source": "test-connector"}

    def greet_job(self, *, job, greeting_text, run_id):  # type: ignore[override]
        _ = job, greeting_text, run_id
        self.greet_attempts += 1
        return {"ok": True, "status": "greeted", "source": "test-connector"}

    def pull_conversations(self, *, max_conversations, unread_only, fetch_latest_hr, chat_tab):  # type: ignore[override]
        _ = max_conversations, unread_only, fetch_latest_hr, chat_tab
        return {"ok": True, "items": [], "source": "test-connector"}

    def reply_conversation(self, *, conversation_id, reply_text, profile_id, conversation_hint):  # type: ignore[override]
        _ = conversation_id, reply_text, profile_id, conversation_hint
        return {"ok": False, "source": "test-connector"}

    def mark_processed(self, *, conversation_id, run_id, note):  # type: ignore[override]
        _ = conversation_id, run_id, note
        return {"ok": True, "status": "noop", "source": "test-connector"}


class _PolicyMatcher:
    """Verdict comes from a per-company table; falls back to ``poor``."""

    def __init__(self, table: dict[str, MatchResult]) -> None:
        self._table = table

    def match(self, *, job: dict[str, Any], snapshot, keyword: str = "") -> MatchResult:  # noqa: ANN001
        _ = snapshot, keyword
        company = str(job.get("company") or "").strip()
        return self._table.get(
            company,
            MatchResult(score=30.0, verdict="poor", reason=f"unmatched-{company}"),
        )


class _ScriptedReflectionPlanner:
    """Returns a sequence of plans on each call; raises if over-called."""

    def __init__(self, plans: list[ReflectionPlan]) -> None:
        self._plans = list(plans)
        self.call_count = 0
        self.last_call_kwargs: dict[str, Any] = {}

    def plan_next_keywords(
        self,
        *,
        original_user_intent: str,
        hard_constraints_md: str,
        round_history,  # noqa: ANN001
        target_remaining: int,
        already_tried_keywords,  # noqa: ANN001
    ) -> ReflectionPlan:
        self.call_count += 1
        self.last_call_kwargs = {
            "original_user_intent": original_user_intent,
            "hard_constraints_md": hard_constraints_md,
            "round_history_len": len(list(round_history)),
            "target_remaining": target_remaining,
            "already_tried_keywords": list(already_tried_keywords),
        }
        if not self._plans:
            return ReflectionPlan(next_keywords=[])
        return self._plans.pop(0)


# ──────────────────────────── helpers


def _job(job_id: str, company: str, title: str = "AI Agent 实习生") -> dict[str, Any]:
    return {
        "job_id": job_id,
        "title": title,
        "company": company,
        "source_url": f"https://example/{job_id}",
        "snippet": f"{title} @ {company}",
        "source": "test-connector",
        "collected_at": "",
    }


def _service(
    *,
    connector: JobPlatformConnector,
    matcher,
    reflection_planner,
    batch_size: int = 2,
    captured_events: list[tuple[str, str, dict[str, Any]]] | None = None,
) -> JobGreetService:
    repo = MagicMock(spec=GreetRepository)
    repo.today_greeted_urls.return_value = set()
    repo.all_greeted_urls.return_value = set()
    repo.append_greet_logs.return_value = None

    def _emit(*, stage: str, status: str, trace_id: str | None = None,
              payload: dict[str, Any] | None = None) -> str:
        if captured_events is not None:
            captured_events.append((stage, status, dict(payload or {})))
        return trace_id or "trace_test_reflection"

    return JobGreetService(
        connector=connector,
        repository=repo,
        policy=GreetPolicy(
            batch_size=batch_size,
            match_threshold=0.0,
            daily_limit=20,
            default_keyword="AI Agent 实习",
            greeting_template="",
            hitl_required=True,
        ),
        notifier=MagicMock(),
        emit_stage_event=_emit,
        preferences=None,
        matcher=matcher,
        greeter=None,
        trait_expander=None,
        reflection_planner=reflection_planner,
    )


# ──────────────────────────── tests


def test_reflection_runs_when_first_round_short_and_merges_new_keyword_results() -> None:
    """Round 1 produces 0 shortlisted (matcher says poor for everyone);
    reflection proposes a new keyword that yields 2 ``good`` candidates;
    final selected pool is 2."""
    connector = _MultiKeywordConnector({
        "AI Agent 实习": [_job("a1", "字节跳动")],
        "LLM 应用开发实习": [
            _job("b1", "小厂Alpha"),
            _job("b2", "小厂Beta"),
        ],
    })
    matcher = _PolicyMatcher({
        "字节跳动": MatchResult(
            score=0.0, verdict="skip",
            hard_violations=["avoid_trait:大厂"],
            reason="avoid_trait:大厂",
        ),
        "小厂Alpha": MatchResult(score=80.0, verdict="good", reason="ok"),
        "小厂Beta": MatchResult(score=60.0, verdict="okay", reason="ok"),
    })
    planner = _ScriptedReflectionPlanner([
        ReflectionPlan(
            next_keywords=["LLM 应用开发实习"],
            rationale="round1 killed by 大厂 trait; widen the role angle",
        )
    ])
    events: list[tuple[str, str, dict[str, Any]]] = []

    service = _service(
        connector=connector,
        matcher=matcher,
        reflection_planner=planner,
        batch_size=2,
        captured_events=events,
    )

    result = service.run_trigger(
        keyword="AI Agent 实习",
        batch_size=2,
        confirm_execute=False,  # preview path is enough to exercise the loop
        fetch_detail=False,
    )

    assert result["ok"] is True
    assert result["needs_confirmation"] is True
    matched_companies = [d.get("company") for d in result.get("matched_details") or []]
    assert sorted(matched_companies) == ["小厂Alpha", "小厂Beta"]

    assert planner.call_count == 1, "planner should be called exactly once"
    assert "AI Agent 实习" in connector.scan_keyword_log
    assert "LLM 应用开发实习" in connector.scan_keyword_log

    reflection_events = [
        payload for stage, status, payload in events
        if stage == "trigger" and status == "reflection"
    ]
    assert len(reflection_events) == 1
    assert reflection_events[0]["next_keywords"] == ["LLM 应用开发实习"]
    assert reflection_events[0]["missing"] == 2

    candidate_events = [
        payload for stage, status, payload in events
        if stage == "match" and status == "candidate"
    ]
    # Two-stage match emits one event per (JD, stage). Stage-A (list)
    # always fires; stage-B (detail) only fires for the survivors of A:
    #   Round1 keyword=AI Agent: 1 list event (字节跳动 → skip, no detail pass)
    #   Round2 keyword=LLM 应用开发实习: 2 list events + 2 detail events
    # → 5 candidate events total.
    assert len(candidate_events) == 5
    list_events = [c for c in candidate_events if c.get("match_stage") == "list"]
    detail_events = [c for c in candidate_events if c.get("match_stage") == "detail"]
    assert len(list_events) == 3
    assert len(detail_events) == 2
    skipped = [c for c in candidate_events if not c["shortlisted"]]
    assert len(skipped) == 1
    assert skipped[0]["company"] == "字节跳动"
    assert "avoid_trait:大厂" in (skipped[0]["hard_violations"] or [""])[0]


def test_reflection_stops_at_budget_when_still_short() -> None:
    """Even with a planner that keeps returning new keywords and a
    pipeline that never produces a shortlist, the service must stop
    after _REFLECTION_MAX_ROUNDS reflection rounds (currently 2)."""
    connector = _MultiKeywordConnector({
        "kw0": [_job("k0", "字节跳动")],
        "kw1": [_job("k1", "字节跳动")],
        "kw2": [_job("k2", "字节跳动")],
    })
    matcher = _PolicyMatcher({
        "字节跳动": MatchResult(
            score=0.0, verdict="skip",
            hard_violations=["avoid_trait:大厂"],
            reason="x",
        ),
    })
    planner = _ScriptedReflectionPlanner([
        ReflectionPlan(next_keywords=["kw1"]),
        ReflectionPlan(next_keywords=["kw2"]),
        ReflectionPlan(next_keywords=["kw3"]),  # would loop forever if no budget
    ])

    service = _service(
        connector=connector,
        matcher=matcher,
        reflection_planner=planner,
        batch_size=3,
    )

    result = service.run_trigger(
        keyword="kw0",
        batch_size=3,
        confirm_execute=False,
        fetch_detail=False,
    )

    assert result["needs_confirmation"] is True
    assert (result.get("matched_details") or []) == []
    assert planner.call_count == 2, (
        "service must stop reflecting after the configured budget; observed "
        f"call_count={planner.call_count}"
    )


def test_reflection_skipped_when_first_round_meets_target() -> None:
    """Happy path: if round 1 already yields >= batch_size candidates,
    the planner must not be invoked at all (no wasted LLM calls)."""
    connector = _MultiKeywordConnector({
        "AI Agent 实习": [
            _job("a1", "小厂Alpha"),
            _job("a2", "小厂Beta"),
        ],
    })
    matcher = _PolicyMatcher({
        "小厂Alpha": MatchResult(score=80.0, verdict="good", reason="ok"),
        "小厂Beta": MatchResult(score=80.0, verdict="good", reason="ok"),
    })
    planner = _ScriptedReflectionPlanner([
        ReflectionPlan(next_keywords=["should-never-be-called"]),
    ])

    service = _service(
        connector=connector,
        matcher=matcher,
        reflection_planner=planner,
        batch_size=2,
    )

    service.run_trigger(
        keyword="AI Agent 实习",
        batch_size=2,
        confirm_execute=False,
        fetch_detail=False,
    )

    assert planner.call_count == 0
    assert connector.scan_keyword_log == ["AI Agent 实习"]
