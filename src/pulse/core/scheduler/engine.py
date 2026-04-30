from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from .windows import is_active_hour, is_in_windows, is_peak_hour

TaskHandler = Callable[[], Awaitable[None] | None]


@dataclass(slots=True)
class ScheduleTask:
    name: str
    interval_seconds: int
    handler: TaskHandler
    enabled: bool = True
    run_immediately: bool = False
    peak_interval_seconds: int | None = None
    offpeak_interval_seconds: int | None = None
    active_hours_only: bool = False
    active_start: int = 8
    active_end: int = 23
    weekday_windows: tuple[tuple[int, int], ...] = ()
    weekend_windows: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than 0")

    def effective_interval(self, *, now: datetime) -> int:
        """Return interval adjusted for peak/off-peak hours."""
        if self.peak_interval_seconds is not None and is_peak_hour(
            now, peak_windows=[(9, 12), (14, 18)]
        ):
            return self.peak_interval_seconds
        if self.offpeak_interval_seconds is not None and not is_peak_hour(
            now, peak_windows=[(9, 12), (14, 18)]
        ):
            return self.offpeak_interval_seconds
        return self.interval_seconds


class SchedulerEngine:
    """In-process interval scheduler with peak/off-peak time-awareness."""

    def __init__(self) -> None:
        self._tasks: dict[str, ScheduleTask] = {}
        self._last_run_at: dict[str, datetime] = {}

    def register(self, task: ScheduleTask) -> None:
        if task.name in self._tasks:
            raise ValueError(f"task already exists: {task.name}")
        self._tasks[task.name] = task

    def list_tasks(self) -> list[str]:
        return sorted(self._tasks.keys())

    def get_task(self, name: str) -> ScheduleTask | None:
        """Return the registered task, or None if unknown.

        Used by per-patrol control surfaces (ADR-004 §6.1) that need to
        read ``enabled`` / interval / active-hours flags in addition to the
        summary shape of ``status()``.
        """
        return self._tasks.get(name)

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """Flip a registered task's ``enabled`` flag in place.

        Returns ``True`` when the task exists (flag now matches ``enabled``),
        ``False`` when the task is unknown. Unknown names do **not** auto-
        create tasks — ADR-004 §6.1.7 invariant #2 (fail-loud).

        Idempotent: setting the same value twice is a no-op; the state of
        ``_last_run_at`` / ``_consecutive_errors`` on ``AgentRuntime`` is
        untouched, so flipping back on does not skip the usual gating.
        """
        task = self._tasks.get(name)
        if task is None:
            return False
        task.enabled = bool(enabled)
        return True

    def is_due(self, task: ScheduleTask, *, now: datetime) -> bool:
        if not task.enabled:
            return False
        if task.active_hours_only:
            if task.weekday_windows or task.weekend_windows:
                active = is_in_windows(
                    now,
                    weekday_windows=task.weekday_windows,
                    weekend_windows=task.weekend_windows,
                )
            else:
                active = is_active_hour(
                    now,
                    weekday_start=task.active_start,
                    weekday_end=task.active_end,
                    weekend_start=task.active_start + 1,
                    weekend_end=task.active_end,
                )
            if not active:
                return False
        last = self._last_run_at.get(task.name)
        if last is None:
            return task.run_immediately
        interval = task.effective_interval(now=now)
        return now - last >= timedelta(seconds=interval)

    async def run_pending(self, *, now: datetime | None = None) -> list[str]:
        current = now or datetime.now(timezone.utc)
        ran: list[str] = []
        for task in self._tasks.values():
            if not self.is_due(task, now=current):
                continue
            if inspect.iscoroutinefunction(task.handler):
                outcome = task.handler()
            else:
                # Run sync handlers off the scheduler event loop so a long
                # patrol turn does not stall heartbeat/tick bookkeeping.
                outcome = await asyncio.to_thread(task.handler)
            if inspect.isawaitable(outcome):
                await outcome
            self._last_run_at[task.name] = current
            ran.append(task.name)
        return ran

    def mark_ran(self, task_name: str, *, when: datetime | None = None) -> None:
        if task_name not in self._tasks:
            raise KeyError(f"task not found: {task_name}")
        self._last_run_at[task_name] = when or datetime.now(timezone.utc)

    def status(self) -> list[dict[str, object]]:
        now = datetime.now(timezone.utc)
        result: list[dict[str, object]] = []
        for task in self._tasks.values():
            last = self._last_run_at.get(task.name)
            result.append({
                "name": task.name,
                "enabled": task.enabled,
                "interval": task.effective_interval(now=now),
                "base_interval": task.interval_seconds,
                "last_run": last.isoformat() if last else None,
                "is_due": self.is_due(task, now=now),
            })
        return result
