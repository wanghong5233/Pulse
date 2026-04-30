"""AgentRuntime per-patrol control plane tests (ADR-004 §6.1).

Drives the real ``AgentRuntime`` with a real ``register_patrol`` call,
then exercises ``list_patrols / get_patrol_stats / enable_patrol /
disable_patrol / run_patrol_once`` through their actual code paths.
No mocking of internal dispatch — only an in-memory event collector,
which is the natural integration boundary for these APIs.

Why integration-level (testing constitution §test-layers #1):
the APIs are thin delegators over SchedulerEngine + _execute_patrol;
unit-mocking would just assert "delegator called delegate", which is
a shadow test.  Exercising the real pipeline covers:
  - SchedulerEngine.set_enabled flip
  - runtime.patrol.lifecycle.* event emission
  - stats mutation through _execute_patrol
  - heartbeat carve-out invariant
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pulse.core.runtime import AgentRuntime, PatrolOutcome, RuntimeConfig


class _EventSink:
    """Minimal event collector — real EventEmitter signature, zero logic."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, dict(payload)))

    def types(self) -> list[str]:
        return [t for t, _ in self.events]


@pytest.fixture
def runtime() -> tuple[AgentRuntime, _EventSink]:
    sink = _EventSink()
    rt = AgentRuntime(event_emitter=sink, config=RuntimeConfig())
    return rt, sink


def _register_noop_patrol(
    rt: AgentRuntime,
    name: str = "test.patrol",
    *,
    enabled: bool = True,
    behavior=None,
) -> list[str]:
    """Register a patrol whose handler records each call. Returns the
    mutable call log so tests can assert dispatch behavior.
    """
    calls: list[str] = []

    def _handler(ctx):
        calls.append(ctx.task_id)
        if behavior is not None:
            return behavior(ctx)
        return None

    rt.register_patrol(
        name=name,
        handler=_handler,
        peak_interval=60,
        offpeak_interval=120,
        enabled=enabled,
        active_hours_only=False,
        token_budget=1000,
    )
    return calls


def test_list_patrols_snapshots_registered_patrols_and_excludes_heartbeat(runtime) -> None:
    rt, _ = runtime
    _register_noop_patrol(rt, "alpha")
    _register_noop_patrol(rt, "beta", enabled=False)

    patrols = rt.list_patrols()
    names = sorted(p["name"] for p in patrols)

    assert names == ["alpha", "beta"], "internal __runtime_heartbeat__ must be hidden"
    alpha = next(p for p in patrols if p["name"] == "alpha")
    beta = next(p for p in patrols if p["name"] == "beta")
    assert alpha["enabled"] is True
    assert alpha["peak_interval_seconds"] == 60
    assert alpha["offpeak_interval_seconds"] == 120
    assert alpha["active_hours_only"] is False
    assert "stats" in alpha and isinstance(alpha["stats"], dict)
    assert beta["enabled"] is False


def test_enable_and_disable_patrol_flip_scheduler_flag_and_emit_lifecycle_events(runtime) -> None:
    """Behavior contract: enable/disable mutate SchedulerEngine.enabled
    AND emit exactly one lifecycle event per successful flip."""
    rt, sink = runtime
    _register_noop_patrol(rt, "alpha", enabled=False)

    assert rt.enable_patrol("alpha") is True
    assert rt.list_patrols()[0]["enabled"] is True

    assert rt.disable_patrol("alpha") is True
    assert rt.list_patrols()[0]["enabled"] is False

    lifecycle = [
        (etype, payload)
        for etype, payload in sink.events
        if etype.startswith("runtime.patrol.lifecycle.")
    ]
    assert [etype for etype, _ in lifecycle] == [
        "runtime.patrol.lifecycle.enabled",
        "runtime.patrol.lifecycle.disabled",
    ]
    assert all(payload.get("task_name") == "alpha" for _, payload in lifecycle)
    # Every lifecycle event carries an actor tag (defaulting to "system")
    # so post-mortem audits can answer "who flipped this".
    assert all("actor" in payload for _, payload in lifecycle)


def test_enable_disable_unknown_patrol_return_false_and_emit_no_event(runtime) -> None:
    rt, sink = runtime

    assert rt.enable_patrol("does_not_exist") is False
    assert rt.disable_patrol("does_not_exist") is False
    assert not any(t.startswith("runtime.patrol.lifecycle.") for t in sink.types())


def test_disarm_patrols_disables_all_enabled_without_running_handlers(runtime) -> None:
    rt, sink = runtime
    enabled_calls = _register_noop_patrol(rt, "alpha", enabled=True)
    _register_noop_patrol(rt, "beta", enabled=False)

    out = rt.disarm_patrols(actor="test")

    assert out["disabled"] == ["alpha"]
    assert out["already_disabled"] == ["beta"]
    assert out["failed"] == []
    assert rt.get_patrol_stats("alpha")["enabled"] is False
    assert rt.get_patrol_stats("beta")["enabled"] is False
    assert enabled_calls == [], "disarm is lifecycle-only, must not execute business handler"
    assert "runtime.patrols.disarmed" in sink.types()


def test_heartbeat_task_is_not_controllable(runtime) -> None:
    """ADR-004 §6.1.7 invariant #1: the internal heartbeat must not be
    reachable through list/get/enable/disable/trigger — prevents runtime
    self-lock."""
    rt, _ = runtime
    heartbeat_name = rt._heartbeat_task_name

    assert rt.get_patrol_stats(heartbeat_name) is None
    assert rt.enable_patrol(heartbeat_name) is False
    assert rt.disable_patrol(heartbeat_name) is False

    result = rt.run_patrol_once(heartbeat_name)
    assert result == {"ok": False, "error": "internal heartbeat is not controllable"}

    assert heartbeat_name not in [p["name"] for p in rt.list_patrols()]


def test_run_patrol_once_invokes_handler_and_records_stats(runtime) -> None:
    """trigger pipeline contract: handler runs once, stats.total_runs++,
    last_outcome=completed, last_trace_id populated."""
    rt, sink = runtime
    calls = _register_noop_patrol(rt, "alpha")

    result = rt.run_patrol_once("alpha")

    assert result["ok"] is True
    assert result["task_name"] == "alpha"
    assert result["last_outcome"] == PatrolOutcome.completed.value
    assert result["last_trace_id"]
    assert result["last_error"] is None
    assert len(calls) == 1

    stats_snapshot = rt.get_patrol_stats("alpha")
    assert stats_snapshot is not None
    assert stats_snapshot["stats"]["total_runs"] == 1
    assert stats_snapshot["stats"]["last_outcome"] == PatrolOutcome.completed.value

    assert "runtime.patrol.lifecycle.triggered" in sink.types()
    assert "runtime.patrol.started" in sink.types()
    assert "runtime.patrol.completed" in sink.types()


def test_run_patrol_once_supports_async_handler(runtime) -> None:
    rt, _ = runtime
    calls: list[str] = []

    async def _async_handler(ctx) -> dict[str, Any]:
        await asyncio.sleep(0)
        calls.append(ctx.task_id)
        return {"ok": True}

    rt.register_patrol(
        name="alpha_async",
        handler=_async_handler,
        peak_interval=60,
        offpeak_interval=120,
        enabled=True,
        active_hours_only=False,
        token_budget=1000,
    )

    result = rt.run_patrol_once("alpha_async")
    assert result["ok"] is True
    assert result["last_outcome"] == PatrolOutcome.completed.value
    assert len(calls) == 1


def test_run_patrol_once_propagates_handler_failure_into_stats(runtime) -> None:
    """When the handler raises, _execute_patrol catches it (L1 retry
    path); run_patrol_once should still return ok=True (trigger itself
    succeeded) but the last_outcome reflects error_recovered and
    last_error carries the message."""
    rt, _ = runtime

    def _failing(ctx):
        raise RuntimeError("simulated failure")

    rt.register_patrol(
        name="alpha",
        handler=_failing,
        peak_interval=60,
        offpeak_interval=120,
        enabled=True,
        active_hours_only=False,
        token_budget=1000,
    )

    result = rt.run_patrol_once("alpha")

    assert result["ok"] is True  # trigger itself succeeded
    assert result["last_outcome"] == PatrolOutcome.error_recovered.value
    assert "simulated failure" in (result["last_error"] or "")


def test_run_patrol_once_rejects_unknown_name(runtime) -> None:
    rt, sink = runtime

    result = rt.run_patrol_once("does_not_exist")
    assert result == {"ok": False, "error": "patrol not found: does_not_exist"}
    assert "runtime.patrol.lifecycle.triggered" not in sink.types()


def test_get_patrol_stats_returns_snapshot_or_none(runtime) -> None:
    rt, _ = runtime
    _register_noop_patrol(rt, "alpha")

    snapshot = rt.get_patrol_stats("alpha")
    assert snapshot is not None
    assert snapshot["name"] == "alpha"
    assert snapshot["enabled"] is True
    assert isinstance(snapshot["stats"], dict)

    assert rt.get_patrol_stats("missing") is None


def test_register_patrol_defaults_to_disabled_and_is_controllable(runtime) -> None:
    """ADR-004 §6.1.1 invariant: register_patrol() without explicit
    ``enabled=`` must produce a patrol that is **registered and visible**
    in list_patrols but **disabled** by default. This is the key property
    that makes "single cognitive path" work — boot-time env killswitches
    must not hide patrols from the conversational control plane.

    This test captures the regression fixed after trace_0cf87040e0e5,
    where ``patrol_chat_enabled=False`` caused on_startup to early-return
    before register_patrol, leaving list_patrols empty.
    """
    rt, _ = runtime

    def _handler(ctx):
        return None

    rt.register_patrol(
        name="default_disabled.patrol",
        handler=_handler,
        peak_interval=60,
        offpeak_interval=120,
        active_hours_only=False,
        token_budget=1000,
    )

    patrols = rt.list_patrols()
    names = [p["name"] for p in patrols]
    assert "default_disabled.patrol" in names, (
        "registered patrol must be visible in control plane even when disabled"
    )
    snapshot = next(p for p in patrols if p["name"] == "default_disabled.patrol")
    assert snapshot["enabled"] is False, (
        "register_patrol must default to enabled=False (ADR-004 §6.1.1)"
    )

    assert rt.enable_patrol("default_disabled.patrol") is True
    assert rt.get_patrol_stats("default_disabled.patrol")["enabled"] is True


def test_heartbeat_skips_disabled_patrol_and_runs_enabled_one(runtime, monkeypatch) -> None:
    """ADR-004 §6.1.1 invariant: heartbeat Stage 5 MUST respect
    ``ScheduleTask.enabled``. ``register_patrol(enabled=False)`` is the
    contract that "patrol is registered but dormant until the user flips
    it on via IM". If heartbeat quietly fires every registered patrol on
    every tick, the single cognitive path is broken.

    Reproduces the 2026-04-23 postmortem: user enabled only
    ``job_chat.patrol`` but the visible browser still kept navigating to
    the BOSS search page every ~60s because ``job_greet.patrol`` was
    ticked by heartbeat — scan+score, then no-op because
    ``confirm_execute=False`` — a ghost tick the user can see but
    cannot control.
    """
    rt, _ = runtime
    # Pin is_active to True so the assertion is independent of wall clock.
    monkeypatch.setattr(rt._config, "is_active", lambda _now: True)

    enabled_calls = _register_noop_patrol(rt, "enabled.patrol", enabled=True)
    disabled_calls = _register_noop_patrol(rt, "disabled.patrol", enabled=False)

    result = rt.heartbeat()

    assert result["triggered_patrols"] == ["enabled.patrol"], (
        "heartbeat Stage 5 must fire ONLY enabled patrols; "
        f"got {result['triggered_patrols']}"
    )
    assert len(enabled_calls) == 1
    assert disabled_calls == [], (
        "heartbeat must NOT fire a patrol that register_patrol left disabled "
        "— that would silently override system.patrol.enable's single "
        "cognitive path and produce ghost browser navigations."
    )


def test_heartbeat_respects_patrol_interval_after_first_due_run(runtime, monkeypatch) -> None:
    """A newly enabled patrol may run on the first tick, then must obey its
    own interval. Heartbeat must not turn every enabled patrol into a
    tick_seconds-frequency task.
    """
    rt, _ = runtime
    monkeypatch.setattr(rt._config, "is_active", lambda _now: True)
    calls = _register_noop_patrol(
        rt,
        "daily_game.patrol",
        enabled=True,
    )

    first = rt.heartbeat()
    second = rt.heartbeat()

    assert first["triggered_patrols"] == ["daily_game.patrol"]
    assert second["triggered_patrols"] == []
    assert len(calls) == 1


def test_heartbeat_respects_patrol_task_windows(runtime, monkeypatch) -> None:
    """Per-task windows are part of the patrol contract. A globally active
    heartbeat must not run a task whose own window is inactive.
    """
    rt, _ = runtime
    monkeypatch.setattr(rt._config, "is_active", lambda _now: True)
    calls: list[str] = []

    def _handler(ctx):
        calls.append(ctx.task_id)

    rt.register_patrol(
        name="windowed_game.patrol",
        handler=_handler,
        peak_interval=60,
        offpeak_interval=60,
        enabled=True,
        active_hours_only=True,
        weekday_windows=((25, 26),),
        weekend_windows=((25, 26),),
    )

    result = rt.heartbeat()

    assert result["triggered_patrols"] == []
    assert calls == []


def test_manual_wake_skips_disabled_patrol_and_runs_enabled_one(runtime, monkeypatch) -> None:
    """manual_wake shares the same contract as heartbeat: it is a
    convenience "force tick NOW" surface, not an authorization channel.
    Firing a disabled patrol here would bypass ``system.patrol.enable``
    and allow an out-of-band operator (e.g. ``POST /api/runtime/wake``)
    to silently run patrols the user has not consented to.
    """
    rt, _ = runtime
    monkeypatch.setattr(rt._config, "is_active", lambda _now: True)

    enabled_calls = _register_noop_patrol(rt, "enabled.patrol", enabled=True)
    disabled_calls = _register_noop_patrol(rt, "disabled.patrol", enabled=False)

    result = rt.manual_wake()

    # manual_wake intentionally calls heartbeat() first and then force-
    # ticks again — enabled patrols can therefore run more than once per
    # wake. The strict contract we enforce here is the negative one:
    # a disabled patrol must never fire through ANY path of manual_wake.
    assert result["triggered_patrols"] == ["enabled.patrol"]
    assert enabled_calls, "enabled patrol must fire at least once"
    assert all(tid == "patrol:enabled.patrol" for tid in enabled_calls)
    assert disabled_calls == [], (
        "manual_wake must NOT fire a disabled patrol — it is a convenience "
        "force-tick surface, not an authorization channel."
    )
