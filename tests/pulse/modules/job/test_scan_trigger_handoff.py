"""Contract B · Phase 2 guard tests — scan → trigger hand-off.

Audit of `trace_f3bda835ed94` flagged a redundant-scan pattern: the LLM
called ``job.greet.scan`` to preview, then ``job.greet.trigger`` to fire,
and trigger internally ran a *second* full ``run_scan`` — doubling MCP
browser cost and causing timeouts when ``fetch_detail=True``.

Contract pinned down here:

1. ``run_scan`` returns ``scan_handle: str`` (short token, tied to
   trace_id) and caches its normalized items in memory with a TTL.
2. ``run_trigger(scan_handle=X)`` with a **known, fresh** handle reuses
   the cached items and MUST NOT invoke the connector again.
3. ``run_trigger(scan_handle=X)`` with an **unknown or expired** handle
   is **fail-loud** — returns ``{"ok": False, "error":
   "scan_handle_unknown_or_expired", ...}`` and does NOT silently
   fall back to a fresh scan. Fallback would make the contract a lie
   (caller thinks it's reusing, actually pays scan cost again).
4. ``run_trigger()`` without ``scan_handle`` keeps legacy behavior (runs
   its own scan) — backward compatible for patrols/CLI that don't know
   about handles yet. A warning is logged so audit can detect agents
   that never opt in to hand-off.
5. The cache has a max size (eviction policy is LRU-ish by insertion
   order) — a runaway session can't OOM the service.

NB: connector is always injected as a fake that records calls; we assert
on the **number and args** of ``scan_jobs`` invocations, which is the
objective signal for "did we actually re-scan?".
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

from tests.pulse.modules.job.test_scan_multi_city import (
    _FakeWorkspaceDB,
    _RecorderConnector,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def job_memory() -> JobMemory:
    return JobMemory(
        workspace_memory=WorkspaceMemory(db_engine=_FakeWorkspaceDB()),
        workspace_id="job.handoff",
    )


def _build_service(
    *,
    connector: JobPlatformConnector,
    preferences: JobMemory | None = None,
    repository: GreetRepository | None = None,
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
            match_threshold=0.0,  # so every scored item passes
            daily_limit=20,
            default_keyword="python",
            greeting_template="",
            hitl_required=True,
        ),
        notifier=MagicMock(),
        emit_stage_event=MagicMock(return_value="tr_handoff"),
        preferences=preferences,
        matcher=None,
        greeter=None,
    )


def _fake_items(n: int, *, city: str | None = None) -> list[dict[str, Any]]:
    tag = city or "all"
    return [
        {
            "job_id": f"{tag}-{i}",
            "title": f"python dev {tag} #{i}",
            "company": f"co{i}",
            "source_url": f"https://boss/{tag}/{i}",
            "snippet": "",
            "source": "recorder",
            "collected_at": "",
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# 1. run_scan emits a scan_handle
# ---------------------------------------------------------------------------


def test_run_scan_returns_scan_handle(job_memory: JobMemory) -> None:
    connector = _RecorderConnector(results_by_city={None: _fake_items(3)})
    service = _build_service(connector=connector, preferences=job_memory)

    result = service.run_scan(
        keyword="python", max_items=5, max_pages=1, apply_filters=False
    )

    assert "scan_handle" in result, (
        "run_scan must surface scan_handle so LLM can pass it to trigger"
    )
    assert isinstance(result["scan_handle"], str)
    assert result["scan_handle"].startswith("sh_"), (
        f"scan_handle must use 'sh_' prefix for audit grep; got {result['scan_handle']!r}"
    )
    # trace_id already stable; handle should be distinct-but-linked
    assert result["scan_handle"] != result["trace_id"]


# ---------------------------------------------------------------------------
# 2. run_trigger(scan_handle=...) reuses cached items
# ---------------------------------------------------------------------------


def test_run_trigger_with_valid_scan_handle_skips_second_scan(
    job_memory: JobMemory,
) -> None:
    connector = _RecorderConnector(results_by_city={None: _fake_items(2)})
    service = _build_service(connector=connector, preferences=job_memory)

    scan = service.run_scan(
        keyword="python", max_items=5, max_pages=1, apply_filters=False
    )
    handle = scan["scan_handle"]
    calls_after_scan = len(connector.calls)
    assert calls_after_scan == 1

    service.run_trigger(
        keyword="python",
        confirm_execute=False,
        fetch_detail=False,
        scan_handle=handle,
    )

    assert len(connector.calls) == calls_after_scan, (
        f"trigger with a valid scan_handle must NOT re-invoke scan_jobs; "
        f"calls went {calls_after_scan} → {len(connector.calls)}"
    )


def test_run_trigger_with_valid_scan_handle_preserves_trace_id(
    job_memory: JobMemory,
) -> None:
    """Reusing a handle should reuse the trace_id too, so scan stage
    events and trigger stage events land under the same audit trail."""
    connector = _RecorderConnector(results_by_city={None: _fake_items(2)})
    service = _build_service(connector=connector, preferences=job_memory)

    scan = service.run_scan(
        keyword="python", max_items=5, max_pages=1, apply_filters=False
    )
    trig = service.run_trigger(
        keyword="python",
        confirm_execute=False,
        fetch_detail=False,
        scan_handle=scan["scan_handle"],
    )

    assert trig.get("trace_id") == scan["trace_id"], (
        f"trigger reuse must share trace_id with the scan; "
        f"scan trace={scan['trace_id']!r}, trigger trace={trig.get('trace_id')!r}"
    )


# ---------------------------------------------------------------------------
# 3. Unknown/expired handle is fail-loud (no silent re-scan)
# ---------------------------------------------------------------------------


def test_run_trigger_with_unknown_scan_handle_fail_loud(
    job_memory: JobMemory,
) -> None:
    connector = _RecorderConnector(results_by_city={None: _fake_items(2)})
    service = _build_service(connector=connector, preferences=job_memory)

    result = service.run_trigger(
        keyword="python",
        confirm_execute=False,
        fetch_detail=False,
        scan_handle="sh_nonexistent_handle",
    )

    assert result.get("ok") is False, (
        "unknown scan_handle MUST return ok=False; silent fallback would "
        "make the contract a lie (caller thinks reuse, actually pays cost)"
    )
    assert result.get("error") == "scan_handle_unknown_or_expired", (
        f"fail-loud error code must be stable for Brain-side handling; "
        f"got {result.get('error')!r}"
    )
    # And critically — no scan was run
    assert connector.calls == [], (
        "fail-loud path must not silently run scan_jobs; "
        f"got {len(connector.calls)} calls"
    )


def test_run_trigger_with_expired_scan_handle_fail_loud(
    job_memory: JobMemory, monkeypatch
) -> None:
    connector = _RecorderConnector(results_by_city={None: _fake_items(2)})
    service = _build_service(connector=connector, preferences=job_memory)

    # 0) sanity: TTL default used below is fine, but we force expiry by
    #    patching the service's internal clock rather than time.sleep().
    now = [1_000_000.0]
    monkeypatch.setattr(service, "_now_monotonic", lambda: now[0])

    scan = service.run_scan(
        keyword="python", max_items=5, max_pages=1, apply_filters=False
    )
    handle = scan["scan_handle"]
    # jump clock forward beyond TTL
    now[0] += service._SCAN_HANDLE_TTL_SEC + 1.0

    result = service.run_trigger(
        keyword="python",
        confirm_execute=False,
        fetch_detail=False,
        scan_handle=handle,
    )

    assert result.get("ok") is False
    assert result.get("error") == "scan_handle_unknown_or_expired"


# ---------------------------------------------------------------------------
# 4. Omitting scan_handle = legacy behavior (still runs its own scan)
# ---------------------------------------------------------------------------


def test_run_trigger_without_scan_handle_runs_own_scan(
    job_memory: JobMemory,
) -> None:
    """Backwards-compat: patrol / CLI paths that don't know about handles
    keep working. (Contract A's when_to_use text will encourage LLMs to
    pass the handle, but the API must not hard-break legacy callers.)"""
    connector = _RecorderConnector(results_by_city={None: _fake_items(2)})
    service = _build_service(connector=connector, preferences=job_memory)

    service.run_trigger(
        keyword="python",
        confirm_execute=False,
        fetch_detail=False,
    )

    assert len(connector.calls) == 1, (
        "legacy (no handle) trigger must still run its own scan; "
        f"got {len(connector.calls)} calls"
    )


# ---------------------------------------------------------------------------
# 5. Cache cap: runaway producers can't OOM the service
# ---------------------------------------------------------------------------


def test_scan_handle_cache_evicts_oldest_past_max(job_memory: JobMemory) -> None:
    connector = _RecorderConnector(results_by_city={None: _fake_items(1)})
    service = _build_service(connector=connector, preferences=job_memory)

    # Fire more scans than the max; the first handle should be evicted.
    cap = service._SCAN_HANDLE_MAX_ENTRIES
    first = service.run_scan(
        keyword="python", max_items=1, max_pages=1, apply_filters=False
    )["scan_handle"]
    for _ in range(cap):
        service.run_scan(
            keyword="python", max_items=1, max_pages=1, apply_filters=False
        )

    result = service.run_trigger(
        keyword="python",
        confirm_execute=False,
        fetch_detail=False,
        scan_handle=first,
    )
    assert result.get("ok") is False
    assert result.get("error") == "scan_handle_unknown_or_expired", (
        "oldest handle must be evicted once cache exceeds cap"
    )


# ---------------------------------------------------------------------------
# 6. Handles are per-service isolated (no global state bleed)
# ---------------------------------------------------------------------------


def test_scan_handle_is_not_global_across_services(
    job_memory: JobMemory,
) -> None:
    connector_a = _RecorderConnector(results_by_city={None: _fake_items(2)})
    service_a = _build_service(connector=connector_a, preferences=job_memory)
    handle_from_a = service_a.run_scan(
        keyword="python", max_items=5, max_pages=1, apply_filters=False
    )["scan_handle"]

    connector_b = _RecorderConnector(results_by_city={None: _fake_items(2)})
    service_b = _build_service(connector=connector_b, preferences=job_memory)
    result = service_b.run_trigger(
        keyword="python",
        confirm_execute=False,
        fetch_detail=False,
        scan_handle=handle_from_a,
    )

    assert result.get("ok") is False, (
        "a handle minted by service A must not be honoured by service B"
    )
    assert result.get("error") == "scan_handle_unknown_or_expired"


# ---------------------------------------------------------------------------
# 7. P0-b · unavailable classification
#    (mode_not_configured / manual_required / dry_run) ≠ failed
# ---------------------------------------------------------------------------


class _ExecutorNotConfiguredConnector(_RecorderConnector):
    """Simulates a live BOSS MCP with GREET_MODE misconfigured: scan works
    but ``greet_job`` returns a fail-loud stub without attempting the real
    browser send. trace_16e97afe3ffc root-cause reproduction.
    """

    def greet_job(self, *, job, greeting_text, run_id):  # type: ignore[override]
        _ = job, greeting_text, run_id
        return {
            "ok": False,
            "status": "mode_not_configured",
            "source": "boss_mcp",
            "error": (
                "PULSE_BOSS_MCP_GREET_MODE='manual_required' not recognised; "
                "expected one of: browser / playwright / log_only / dry_run_ok"
            ),
        }


def test_run_trigger_classifies_mode_not_configured_as_unavailable(
    job_memory: JobMemory,
) -> None:
    """P0-b regression: the executor refused to try (infra gap) — this is
    NOT the same as "tried-and-the-platform-said-no". Contract C judges
    grounding off ``greeted`` / ``failed`` / ``unavailable``; blending
    the two makes the trace indistinguishable from a real send-failure.
    """
    connector = _ExecutorNotConfiguredConnector(
        results_by_city={None: _fake_items(3)}
    )
    repo = MagicMock(spec=GreetRepository)
    repo.today_greeted_urls.return_value = set()
    repo.all_greeted_urls.return_value = set()
    repo.append_greet_logs.return_value = None
    notifier = MagicMock()
    service = JobGreetService(
        connector=connector,
        repository=repo,
        policy=GreetPolicy(
            batch_size=5,
            match_threshold=0.0,
            daily_limit=20,
            default_keyword="python",
            greeting_template="hi",
            hitl_required=True,
        ),
        notifier=notifier,
        emit_stage_event=MagicMock(return_value="tr_unavail"),
        preferences=job_memory,
        matcher=None,
        greeter=None,
    )

    result = service.run_trigger(
        keyword="python", confirm_execute=True, fetch_detail=False,
    )

    assert result["ok"] is True
    assert result["greeted"] == 0
    assert result["failed"] == 0, (
        "mode_not_configured MUST NOT count as failed; it's an infra gap, "
        "not a per-JD send failure"
    )
    assert result["unavailable"] == 3, (
        f"all 3 attempted sends got refused by the executor; "
        f"expected unavailable=3, got result={result}"
    )

    # Notifier level MUST be 'warning' — operator needs to know the
    # executor is dark; silent INFO would look like a normal empty run.
    assert notifier.send.called
    notif = notifier.send.call_args.args[0]
    assert notif.level == "warning", (
        f"unavailable>0 MUST lift notification level to warning; got {notif.level!r}"
    )
    assert "unavailable=3" in notif.content


def test_run_trigger_extract_facts_trigger_includes_unavailable(
    job_memory: JobMemory,
) -> None:
    """The ``unavailable`` counter MUST flow into the Contract C receipt
    ledger via ``_extract_facts_trigger`` so the judge sees grounding."""
    from pulse.modules.job.greet.module import _extract_facts_trigger

    connector = _ExecutorNotConfiguredConnector(
        results_by_city={None: _fake_items(2)}
    )
    repo = MagicMock(spec=GreetRepository)
    repo.today_greeted_urls.return_value = set()
    repo.all_greeted_urls.return_value = set()
    repo.append_greet_logs.return_value = None
    service = JobGreetService(
        connector=connector,
        repository=repo,
        policy=GreetPolicy(
            batch_size=5, match_threshold=0.0, daily_limit=20,
            default_keyword="python", greeting_template="hi", hitl_required=True,
        ),
        notifier=MagicMock(),
        emit_stage_event=MagicMock(return_value="tr_unavail2"),
        preferences=job_memory,
        matcher=None,
        greeter=None,
    )
    observation = service.run_trigger(
        keyword="python", confirm_execute=True, fetch_detail=False,
    )

    facts = _extract_facts_trigger(observation)
    assert facts.get("unavailable") == 2, (
        "extract_facts_trigger MUST expose 'unavailable' so the judge can "
        "distinguish 'infra refused' from 'send failed' when grading "
        "'已投递 N 家' commitments"
    )
    assert facts.get("greeted") == 0
    assert facts.get("failed") == 0
