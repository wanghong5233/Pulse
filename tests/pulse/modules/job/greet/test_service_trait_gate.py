from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from pulse.core.action_report import ACTION_REPORT_KEY
from pulse.core.memory.workspace_memory import WorkspaceMemory
from pulse.modules.job._connectors.base import JobPlatformConnector
from pulse.modules.job.greet.matcher import MatchResult
from pulse.modules.job.greet.repository import GreetRepository
from pulse.modules.job.greet.service import GreetPolicy, JobGreetService
from pulse.modules.job.memory import JobMemory
from tests.pulse.modules.job.test_scan_multi_city import _FakeWorkspaceDB


class _Connector(JobPlatformConnector):
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = list(items)
        self.greet_attempts = 0
        self.scan_calls = 0

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
            keyword, max_items, max_pages, target_count,
            evaluation_cap, scroll_plateau_rounds, job_type, city,
        )
        self.scan_calls += 1
        return {
            "ok": True,
            "items": list(self._items),
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


class _StubTraitExpander:
    def __init__(self, mapping: dict[str, set[str]]) -> None:
        self.mapping = mapping

    def resolve_avoid_trait_companies(self, *, snapshot) -> dict[str, set[str]]:  # noqa: ANN001
        _ = snapshot
        return dict(self.mapping)


class _ViolationMatcher:
    def match(
        self,
        *,
        job: dict[str, Any],
        snapshot: object | None,
        keyword: str = "",
    ) -> MatchResult:
        _ = job, snapshot, keyword
        return MatchResult(
            score=93.0,
            verdict="good",
            matched_signals=["keyword_fit"],
            hard_violations=["avoid_trait:大厂"],
            reason="conflicts with hard trait",
        )


def _memory() -> JobMemory:
    return JobMemory(
        workspace_memory=WorkspaceMemory(db_engine=_FakeWorkspaceDB()),
        workspace_id="job.test.service.trait",
    )


def _service(
    *,
    connector: JobPlatformConnector,
    preferences: JobMemory | None,
    matcher: Any = None,
    trait_expander: Any = None,
    hitl_required: bool = True,
    today_greeted_urls: set[str] | None = None,
) -> JobGreetService:
    repo = MagicMock(spec=GreetRepository)
    repo.today_greeted_urls.return_value = set(today_greeted_urls or [])
    repo.all_greeted_urls.return_value = set()
    repo.append_greet_logs.return_value = None
    return JobGreetService(
        connector=connector,
        repository=repo,
        policy=GreetPolicy(
            batch_size=5,
            match_threshold=0.0,
            daily_limit=20,
            default_keyword="AI Agent 实习",
            greeting_template="",
            hitl_required=hitl_required,
        ),
        notifier=MagicMock(),
        emit_stage_event=MagicMock(return_value="trace_trait_gate"),
        preferences=preferences,
        matcher=matcher,
        greeter=None,
        trait_expander=trait_expander,
    )


def test_run_scan_pref_filter_drops_company_hit_by_expanded_avoid_trait() -> None:
    memory = _memory()
    memory.record_item(
        {
            "type": "avoid_trait",
            "target": "大厂",
            "content": "暂时战略性放弃大厂暑期实习",
        }
    )
    connector = _Connector(
        [
            {
                "job_id": "1",
                "title": "AI Agent 实习生",
                "company": "字节跳动",
                "source_url": "https://x/1",
                "snippet": "Agent 研发",
                "source": "test-connector",
                "collected_at": "",
            },
            {
                "job_id": "2",
                "title": "LLM Agent 实习生",
                "company": "某创业公司",
                "source_url": "https://x/2",
                "snippet": "早期创业团队",
                "source": "test-connector",
                "collected_at": "",
            },
        ]
    )
    service = _service(
        connector=connector,
        preferences=memory,
        matcher=None,
        trait_expander=_StubTraitExpander({"大厂": {"字节跳动", "阿里巴巴"}}),
    )

    result = service.run_scan(
        keyword="AI Agent 实习",
        max_items=10,
        max_pages=1,
        apply_filters=True,
    )

    companies = [item["company"] for item in result["items"]]
    assert companies == ["某创业公司"]
    assert any(
        "skip:avoid_trait_company_set" in str(err)
        for err in result.get("errors") or []
    )


def test_run_trigger_enforces_hard_violations_even_when_matcher_returns_good() -> None:
    connector = _Connector(
        [
            {
                "job_id": "1",
                "title": "AI Agent 实习生",
                "company": "某公司",
                "source_url": "https://x/1",
                "snippet": "Agent 研发",
                "source": "test-connector",
                "collected_at": "",
            }
        ]
    )
    service = _service(
        connector=connector,
        preferences=None,
        matcher=_ViolationMatcher(),
        trait_expander=None,
        hitl_required=False,
    )

    result = service.run_trigger(
        keyword="AI Agent 实习",
        confirm_execute=True,
        fetch_detail=False,
    )

    assert result["ok"] is True
    assert result["greeted"] == 0
    assert connector.greet_attempts == 0, (
        "hard_violations non-empty must block send even when verdict=good"
    )
    assert any(
        "skip:matcher_hard_violations" in str(err)
        for err in result.get("errors") or []
    )


def test_run_trigger_short_circuits_when_daily_limit_reached_before_scan() -> None:
    connector = _Connector(
        [
            {
                "job_id": "1",
                "title": "AI Agent 实习生",
                "company": "某公司",
                "source_url": "https://x/1",
                "snippet": "Agent 研发",
                "source": "test-connector",
                "collected_at": "",
            }
        ]
    )
    service = _service(
        connector=connector,
        preferences=None,
        matcher=None,
        trait_expander=None,
        hitl_required=False,
        today_greeted_urls={f"https://done/{i}" for i in range(20)},
    )

    result = service.run_trigger(
        keyword="AI Agent 实习",
        confirm_execute=True,
        fetch_detail=False,
    )

    assert result["ok"] is True
    assert result["greeted"] == 0
    assert connector.scan_calls == 0, "quota reached should skip scan/match/reflect entirely"
    assert connector.greet_attempts == 0
    assert "daily_limit_reached" in str(result.get("reason") or "")
    report = dict(result.get(ACTION_REPORT_KEY) or {})
    assert report.get("status") == "skipped"
    metrics = dict(report.get("metrics") or {})
    assert metrics.get("daily_count") == 20
    assert metrics.get("daily_limit") == 20


def test_run_scan_hc_experience_level_uses_title_signal_when_detail_missing() -> None:
    memory = _memory()
    memory.set_hard_constraint("experience_level", "intern")
    connector = _Connector(
        [
            {
                "job_id": "1",
                "title": "AI Agent开发工程师",
                "company": "某公司A",
                "source_url": "https://x/1",
                "snippet": "负责Agent应用研发",
                "source": "test-connector",
                "collected_at": "",
            },
            {
                "job_id": "2",
                "title": "AI Agent 实习生",
                "company": "某公司B",
                "source_url": "https://x/2",
                "snippet": "实习岗",
                "source": "test-connector",
                "collected_at": "",
            },
        ]
    )
    service = _service(
        connector=connector,
        preferences=memory,
        matcher=None,
        trait_expander=None,
    )

    result = service.run_scan(
        keyword="AI Agent",
        max_items=10,
        max_pages=1,
        apply_filters=True,
    )

    assert [it["company"] for it in result["items"]] == ["某公司B"]
    assert any(
        "skip:hc_experience_level" in str(err)
        for err in result.get("errors") or []
    )


def test_run_scan_hc_experience_level_uses_salary_unit_signal() -> None:
    memory = _memory()
    memory.set_hard_constraint("experience_level", "intern")
    connector = _Connector(
        [
            {
                "job_id": "1",
                "title": "AI Agent 研发",
                "company": "某公司A",
                "source_url": "https://x/1",
                "snippet": "负责模型应用落地",
                "salary": "25-35K",
                "source": "test-connector",
                "collected_at": "",
            },
            {
                "job_id": "2",
                "title": "AI Agent 研发",
                "company": "某公司B",
                "source_url": "https://x/2",
                "snippet": "参与Agent系统迭代",
                "salary": "300-450元/天",
                "source": "test-connector",
                "collected_at": "",
            },
        ]
    )
    service = _service(
        connector=connector,
        preferences=memory,
        matcher=None,
        trait_expander=None,
    )

    result = service.run_scan(
        keyword="AI Agent",
        max_items=10,
        max_pages=1,
        apply_filters=True,
    )

    assert [it["company"] for it in result["items"]] == ["某公司B"]
    assert any(
        "skip:hc_experience_level" in str(err)
        for err in result.get("errors") or []
    )

