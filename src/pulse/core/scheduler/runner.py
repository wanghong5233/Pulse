from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from .engine import ScheduleTask, SchedulerEngine

logger = logging.getLogger(__name__)


class BackgroundSchedulerRunner:
    """Background tick runner for SchedulerEngine."""

    def __init__(
        self,
        *,
        engine: SchedulerEngine | None = None,
        tick_seconds: int = 15,
    ) -> None:
        self._engine = engine or SchedulerEngine()
        self._tick_seconds = max(1, int(tick_seconds))
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        self._last_tick_at: datetime | None = None
        self._last_error: str | None = None
        self._last_ran_tasks: list[str] = []

    @property
    def engine(self) -> SchedulerEngine:
        return self._engine

    def register(self, task: ScheduleTask) -> None:
        self._engine.register(task)

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                daemon=True,
                name="pulse-scheduler-runner",
            )
            self._thread.start()
            return True

    def stop(self, *, join_timeout_sec: float = 1.5) -> bool:
        with self._lock:
            was_running = self._running
            self._running = False
            self._stop_event.set()
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=max(0.1, join_timeout_sec))
        return was_running

    async def run_once(self) -> list[str]:
        now = datetime.now(timezone.utc)
        try:
            ran = await self._engine.run_pending(now=now)
            with self._lock:
                self._last_tick_at = now
                self._last_error = None
                self._last_ran_tasks = list(ran)
            return ran
        except Exception as exc:
            with self._lock:
                self._last_tick_at = now
                self._last_error = str(exc)[:1000]
                self._last_ran_tasks = []
            raise

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "tick_seconds": self._tick_seconds,
                "tasks": self._engine.list_tasks(),
                "last_tick_at": self._last_tick_at,
                "last_error": self._last_error,
                "last_ran_tasks": list(self._last_ran_tasks),
            }

    def _loop(self) -> None:
        self._run_once_blocking()
        while not self._stop_event.wait(self._tick_seconds):
            self._run_once_blocking()

    def _run_once_blocking(self) -> None:
        try:
            asyncio.run(self.run_once())
        except Exception:
            logger.exception("scheduler runner tick failed")
            return
