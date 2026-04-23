from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from pulse.core.scheduler.engine import ScheduleTask, SchedulerEngine


def test_schedule_task_validates_interval() -> None:
    try:
        ScheduleTask(name="x", interval_seconds=0, handler=lambda: None)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_scheduler_runs_due_task_with_interval() -> None:
    called: list[str] = []
    engine = SchedulerEngine()
    engine.register(
        ScheduleTask(
            name="job",
            interval_seconds=60,
            run_immediately=True,
            handler=lambda: called.append("run"),
        )
    )

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ran_first = asyncio.run(engine.run_pending(now=t0))
    assert ran_first == ["job"]
    assert called == ["run"]

    ran_second = asyncio.run(engine.run_pending(now=t0 + timedelta(seconds=30)))
    assert ran_second == []

    ran_third = asyncio.run(engine.run_pending(now=t0 + timedelta(seconds=60)))
    assert ran_third == ["job"]
    assert called == ["run", "run"]


def test_set_enabled_stops_and_resumes_dispatch() -> None:
    """ADR-004 §6.1 decision A: flipping ``enabled`` in place must make the
    next ``run_pending`` tick skip the task, and flipping it back must
    resume dispatch without needing re-registration. Drives the real
    is_due → run_pending path (no mocks) — covers the behavior Brain relies
    on when toggling a patrol via ``system.patrol.enable/disable``.
    """
    runs: list[str] = []
    engine = SchedulerEngine()
    engine.register(
        ScheduleTask(
            name="job",
            interval_seconds=60,
            run_immediately=True,
            handler=lambda: runs.append("run"),
        )
    )

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert asyncio.run(engine.run_pending(now=t0)) == ["job"]

    assert engine.set_enabled("job", False) is True
    ran_while_disabled = asyncio.run(engine.run_pending(now=t0 + timedelta(seconds=120)))
    assert ran_while_disabled == []
    assert runs == ["run"]

    assert engine.set_enabled("job", True) is True
    ran_after_enable = asyncio.run(engine.run_pending(now=t0 + timedelta(seconds=180)))
    assert ran_after_enable == ["job"]
    assert runs == ["run", "run"]


def test_set_enabled_returns_false_for_unknown_task_and_does_not_auto_create() -> None:
    """Fail-loud unknown-task contract (ADR-004 §6.1.7 invariant #2):
    ``set_enabled`` on an unregistered name returns False and leaves the
    registry untouched — no auto-create, no swallowed error.
    """
    engine = SchedulerEngine()
    assert engine.set_enabled("never_registered", True) is False
    assert engine.list_tasks() == []
    assert engine.get_task("never_registered") is None


def test_get_task_returns_registered_instance() -> None:
    engine = SchedulerEngine()
    task = ScheduleTask(name="job", interval_seconds=60, handler=lambda: None)
    engine.register(task)

    assert engine.get_task("job") is task
    assert engine.get_task("missing") is None
