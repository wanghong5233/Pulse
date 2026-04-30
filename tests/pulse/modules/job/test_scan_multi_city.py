"""P2-A regression guard: preferred_location multi-city fan-out.

Audit of trace_f3bda835ed94 observed ``preferred_location=[杭州, 上海]`` but
the 15 returned jobs were **all from Shanghai** because:
  1. ``BossPlatformConnector.scan_jobs`` did not accept a ``city`` argument,
     so the MCP browser scan always hit BOSS's nationwide feed (which
     happens to recommend Shanghai-heavy results for dev keywords).
  2. ``JobGreetService.run_scan`` never fanned-out the preference, so
     downstream ``_apply_hard_constraints`` just silently dropped every
     non-Shanghai JD.

Contract enforced here:
  * ``scan_jobs`` on the platform connector accepts an optional ``city``
    kwarg and forwards it to the underlying transport payload.
  * ``JobGreetService.run_scan`` reads ``JobMemory.preferred_location``
    and, when it contains N>1 cities, invokes the connector once per
    city and merges/dedupes the items.
  * MCP runtime's search-URL builder translates ``city='杭州'`` into the
    BOSS numeric city code (``101210100``).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from pulse.core.memory.workspace_memory import WorkspaceMemory
from pulse.modules.job._connectors.base import JobPlatformConnector
from pulse.modules.job.greet.repository import GreetRepository
from pulse.modules.job.greet.service import GreetPolicy, JobGreetService
from pulse.modules.job.memory import JobMemory

# import the runtime lazily inside the URL test, because it pulls patchright
# which is only available inside the pulse venv.


class _RecorderConnector(JobPlatformConnector):
    """Captures every ``scan_jobs`` invocation for assertion."""

    def __init__(self, *, results_by_city: dict[str | None, list[dict[str, Any]]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        # ``None`` key = no city specified (backwards-compat).
        self._results_by_city = dict(results_by_city or {})

    # identity --------------------------------------------------------------
    @property
    def provider_name(self) -> str:
        return "recorder"

    @property
    def execution_ready(self) -> bool:
        return True

    def health(self) -> dict[str, Any]:
        return {"ok": True}

    def check_login(self) -> dict[str, Any]:
        return {"ok": True, "status": "ready"}

    # scan/detail -----------------------------------------------------------
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
        self.calls.append(
            {
                "keyword": keyword,
                "max_items": max_items,
                "max_pages": max_pages,
                "target_count": target_count,
                "evaluation_cap": evaluation_cap,
                "scroll_plateau_rounds": scroll_plateau_rounds,
                "job_type": job_type,
                "city": city,
            }
        )
        default_items = [
            {
                "job_id": f"{city or 'all'}-1",
                "title": f"{keyword} 岗位 {city or '全国'}",
                "company": "demo",
                "source_url": f"https://boss/{city or 'all'}/1",
                "snippet": "",
                "source": "recorder",
                "collected_at": "",
            }
        ]
        items = self._results_by_city.get(city, default_items)
        return {
            "ok": True,
            "items": list(items),
            "pages_scanned": 1,
            "scroll_count": 0,
            "exhausted": True,
            "source": "recorder",
            "errors": [],
        }

    def fetch_job_detail(self, *, job_id: str, source_url: str) -> dict[str, Any]:
        return {"ok": True, "detail": {}, "source": "recorder"}

    def greet_job(self, *, job, greeting_text, run_id):  # type: ignore[override]
        return {"ok": False, "status": "not_implemented", "source": "recorder"}

    def pull_conversations(self, *, max_conversations, unread_only, fetch_latest_hr, chat_tab):  # type: ignore[override]
        return {"ok": True, "items": [], "source": "recorder"}

    def reply_conversation(self, *, conversation_id, reply_text, profile_id, conversation_hint):  # type: ignore[override]
        return {"ok": False, "source": "recorder"}

    def mark_processed(self, *, conversation_id, run_id, note):  # type: ignore[override]
        return {"ok": True, "status": "noop", "source": "recorder"}


class _FakeWorkspaceDB:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def execute(self, sql, params=None, *, fetch="none", commit=True):  # noqa: ANN001
        _ = commit, fetch
        norm = " ".join(str(sql).lower().split())
        p = tuple(params or ())
        if norm.startswith("create "):
            return None
        if norm.startswith("select value from workspace_facts"):
            ws, key = p
            for row in self.rows:
                if row["workspace_id"] == ws and row["key"] == key:
                    return (row["value"],)
            return None
        if norm.startswith("select id from workspace_facts"):
            ws, key = p
            for row in self.rows:
                if row["workspace_id"] == ws and row["key"] == key:
                    return (id(row),)
            return None
        if norm.startswith("update workspace_facts set value"):
            value, source, updated_at, ws, key = p
            for row in self.rows:
                if row["workspace_id"] == ws and row["key"] == key:
                    row["value"] = value
                    row["source"] = source
                    row["updated_at"] = updated_at
            return None
        if norm.startswith("insert into workspace_facts"):
            workspace_id, key, value, source, created_at, updated_at = p
            self.rows.append(
                {
                    "workspace_id": workspace_id,
                    "key": key,
                    "value": value,
                    "source": source,
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
            )
            return None
        if norm.startswith("select key, value, source, updated_at from workspace_facts"):
            ws, prefix_pattern = p
            prefix = prefix_pattern.rstrip("%")
            return [
                (r["key"], r["value"], r["source"], r["updated_at"])
                for r in sorted(self.rows, key=lambda r: r["key"])
                if r["workspace_id"] == ws and (not prefix or r["key"].startswith(prefix))
            ]
        if norm.startswith("select count(*) from workspace_facts"):
            ws, pat = p
            prefix = pat.rstrip("%")
            return (sum(1 for r in self.rows if r["workspace_id"] == ws and r["key"].startswith(prefix)),)
        if norm.startswith("delete from workspace_facts where workspace_id = %s and key = %s"):
            ws, key = p
            self.rows = [r for r in self.rows if not (r["workspace_id"] == ws and r["key"] == key)]
            return None
        if norm.startswith("delete from workspace_facts where workspace_id = %s and key like"):
            ws, pat = p
            prefix = pat.rstrip("%")
            self.rows = [r for r in self.rows if not (r["workspace_id"] == ws and r["key"].startswith(prefix))]
            return None
        raise AssertionError(f"unexpected SQL: {sql}")


@pytest.fixture()
def job_memory() -> JobMemory:
    return JobMemory(
        workspace_memory=WorkspaceMemory(db_engine=_FakeWorkspaceDB()),
        workspace_id="job.test",
    )


def _build_service(
    *,
    connector: JobPlatformConnector,
    repository: GreetRepository | None = None,
    preferences: JobMemory | None = None,
) -> JobGreetService:
    repo = repository or MagicMock(spec=GreetRepository)
    if repository is None:
        repo.today_greeted_urls.return_value = set()
        repo.all_greeted_urls.return_value = set()
        repo.append_greet_logs.return_value = None
    return JobGreetService(
        connector=connector,
        repository=repo,
        policy=GreetPolicy(
            batch_size=5,
            match_threshold=60.0,
            daily_limit=20,
            default_keyword="python",
            greeting_template="",
            hitl_required=True,
        ),
        notifier=MagicMock(),
        emit_stage_event=MagicMock(return_value="trace-x"),
        preferences=preferences,
        matcher=None,
        greeter=None,
    )


# ---------------------------------------------------------------------------
# 1) run_scan fans out per preferred_location city
# ---------------------------------------------------------------------------


def test_run_scan_fans_out_per_preferred_city(job_memory: JobMemory) -> None:
    job_memory.set_hard_constraint("preferred_location", ["杭州", "上海"])
    connector = _RecorderConnector()
    service = _build_service(connector=connector, preferences=job_memory)

    service.run_scan(keyword="python", max_items=10, max_pages=1, apply_filters=False)

    cities = [call["city"] for call in connector.calls]
    assert set(cities) == {"杭州", "上海"}, (
        f"run_scan must fan-out once per preferred city, got {cities}"
    )


def test_run_scan_single_city_forwards_city_param(job_memory: JobMemory) -> None:
    job_memory.set_hard_constraint("preferred_location", ["杭州"])
    connector = _RecorderConnector()
    service = _build_service(connector=connector, preferences=job_memory)

    service.run_scan(keyword="python", max_items=10, max_pages=1, apply_filters=False)

    assert len(connector.calls) == 1
    assert connector.calls[0]["city"] == "杭州"


def test_run_scan_no_preference_does_not_pass_city(job_memory: JobMemory) -> None:
    connector = _RecorderConnector()
    service = _build_service(connector=connector, preferences=job_memory)

    service.run_scan(keyword="python", max_items=10, max_pages=1, apply_filters=False)

    assert len(connector.calls) == 1
    assert connector.calls[0]["city"] is None, (
        "no preferred_location means nationwide scan; city must be None"
    )


def test_run_scan_fan_out_merges_and_dedupes_items(job_memory: JobMemory) -> None:
    job_memory.set_hard_constraint("preferred_location", ["杭州", "上海"])
    shared = {
        "job_id": "shared-1",
        "title": "python dev",
        "company": "shared",
        "source_url": "https://boss/shared/1",
        "snippet": "",
        "source": "recorder",
        "collected_at": "",
    }
    connector = _RecorderConnector(
        results_by_city={
            "杭州": [
                {**shared, "job_id": "hz-1", "source_url": "https://boss/hz/1"},
                shared,
            ],
            "上海": [
                {**shared, "job_id": "sh-1", "source_url": "https://boss/sh/1"},
                shared,
            ],
        }
    )
    service = _build_service(connector=connector, preferences=job_memory)

    result = service.run_scan(
        keyword="python", max_items=10, max_pages=1, apply_filters=False
    )
    job_ids = {item["job_id"] for item in result["items"]}
    assert job_ids == {"hz-1", "sh-1", "shared-1"}, (
        "items from multiple cities must be merged and deduped by (job_id, source_url)"
    )


# ---------------------------------------------------------------------------
# 2) BossPlatformConnector.scan_jobs accepts ``city`` kwarg
# ---------------------------------------------------------------------------


def test_boss_connector_scan_jobs_forwards_city(monkeypatch) -> None:
    from pulse.modules.job._connectors.boss.connector import BossPlatformConnector
    from pulse.modules.job._connectors.boss.settings import (
        BossConnectorSettings,
        BossMcpSettings,
        get_boss_connector_settings,
    )

    monkeypatch.setenv("PULSE_BOSS_MCP_BASE_URL", "http://127.0.0.1:8811")
    monkeypatch.setenv("PULSE_BOSS_PROVIDER", "mcp")
    get_boss_connector_settings.cache_clear()
    settings = get_boss_connector_settings()

    connector = BossPlatformConnector(settings)
    captured: dict[str, Any] = {}

    def fake_call_tool(server, tool, arguments):
        captured["server"] = server
        captured["tool"] = tool
        captured["arguments"] = dict(arguments)
        return {"ok": True, "items": [], "pages_scanned": 1, "source": "boss_mcp"}

    connector._mcp_transport.call_tool = fake_call_tool  # type: ignore[assignment]

    connector.scan_jobs(keyword="python", max_items=5, max_pages=1, city="杭州")

    assert captured["tool"] == "scan_jobs"
    assert captured["arguments"].get("city") == "杭州", (
        f"connector must forward city to MCP payload; got {captured['arguments']!r}"
    )


# ---------------------------------------------------------------------------
# 3) MCP runtime search-URL builder embeds BOSS city code
# ---------------------------------------------------------------------------


def test_boss_runtime_search_url_embeds_city_code_for_known_city() -> None:
    from pulse.mcp_servers import _boss_platform_runtime as rt

    url = rt._build_search_url(keyword="python", page=1, city="杭州")
    assert "city=101210100" in url, (
        f"杭州 BOSS city code 101210100 must appear in the search URL; got {url}"
    )


def test_boss_runtime_search_url_omits_city_for_unknown_city() -> None:
    from pulse.mcp_servers import _boss_platform_runtime as rt

    url = rt._build_search_url(keyword="python", page=1, city="火星殖民地")
    assert "city=" not in url, (
        "unknown city must not inject a bogus city= parameter; fall back to nationwide scan"
    )


def test_boss_runtime_search_url_without_city_unchanged() -> None:
    from pulse.mcp_servers import _boss_platform_runtime as rt

    url_no_city = rt._build_search_url(keyword="python", page=1)
    assert "city=" not in url_no_city
