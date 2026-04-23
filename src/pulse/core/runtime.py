"""Pulse Agent Runtime — generic OS-level execution environment.

The Runtime is a general-purpose "operating system kernel" for Pulse.
It knows nothing about any specific business module (BOSS, Intel, etc.).
Modules self-register their patrol tasks during on_startup via
runtime.register_patrol(), keeping the kernel completely decoupled.

P3 additions:
  - HeartbeatLoop: Runtime 内核自检心跳，调整 patrol 间隔，触发 compaction/promotion
  - Recovery Ladder: L0 Skip → L1 Retry with backoff → L2 Degrade → L3 Abort
  - Hook integration: patrol 执行触发 beforeTaskStart / afterToolUse Hook
  - ManualWake: 手动触发完整 heartbeat turn
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable
from uuid import uuid4

from .hooks import HookPoint, HookRegistry
from .logging_config import set_trace_id
from .scheduler import BackgroundSchedulerRunner, ScheduleTask
from .scheduler.windows import is_active_hour
from .task_context import (
    ExecutionMode,
    IsolationLevel,
    TaskContext,
    create_heartbeat_context,
    create_patrol_context,
    create_subagent_context,
    create_resumed_context,
)

logger = logging.getLogger(__name__)

EventEmitter = Callable[[str, dict[str, Any]], None]


# ---------------------------------------------------------------------------
# Outcome enum (inspired by Claude Code StopReason)
# ---------------------------------------------------------------------------

class PatrolOutcome(str, Enum):
    """Possible outcomes of a single patrol turn."""
    completed = "completed"
    skipped_inactive = "skipped_inactive"
    skipped_disabled = "skipped_disabled"
    skipped_budget = "skipped_budget"
    skipped_not_ready = "skipped_not_ready"
    error_recovered = "error_recovered"
    error_aborted = "error_aborted"
    degraded = "degraded"


class TakeoverState(str, Enum):
    """Agent 控制权状态 (§6.3)。"""
    autonomous = "autonomous"
    paused = "paused"
    human_control = "human_control"


@dataclass
class TaskCheckpoint:
    """任务执行快照，用于 resumedTask 恢复 (§6.2)。"""
    task_id: str
    trace_id: str
    session_id: str | None = None
    workspace_id: str | None = None
    stopped_reason: str = ""
    step_index: int = 0
    messages_snapshot: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "stopped_reason": self.stopped_reason,
            "step_index": self.step_index,
            "messages_snapshot": self.messages_snapshot,
            "extra": self.extra,
            "created_at": self.created_at,
        }


@dataclass
class SubagentRecord:
    """子任务记录，用于 parent-child 生命周期管理。"""
    subagent_task_id: str
    parent_task_id: str
    ctx: TaskContext
    status: str = "pending"  # pending | running | completed | failed
    result: Any = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Runtime-level configuration (NO business-module fields)
# ---------------------------------------------------------------------------

class RuntimeConfig:
    """Pure infrastructure config — reads only AGENT_RUNTIME_* and
    GUARD_ACTIVE_* variables.  Knows nothing about BOSS, Intel, etc."""

    def __init__(self) -> None:
        self.enabled = _env_bool("AGENT_RUNTIME_ENABLED", False)
        self.timezone = os.environ.get("GUARD_TIMEZONE", "Asia/Shanghai")
        self.tick_seconds = _env_int("AGENT_RUNTIME_TICK_SECONDS", 15)
        self.max_consecutive_errors = _env_int("AGENT_RUNTIME_MAX_ERRORS", 5)

        self.active_start_hour = _env_int("GUARD_ACTIVE_START_HOUR", 9)
        self.active_end_hour = _env_int("GUARD_ACTIVE_END_HOUR", 22)
        self.weekend_start_hour = _env_int("GUARD_WEEKEND_START_HOUR", 10)
        self.weekend_end_hour = _env_int("GUARD_WEEKEND_END_HOUR", 20)

    def is_active(self, now: datetime) -> bool:
        return is_active_hour(
            now,
            weekday_start=self.active_start_hour,
            weekday_end=self.active_end_hour,
            weekend_start=self.weekend_start_hour,
            weekend_end=self.weekend_end_hour,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "timezone": self.timezone,
            "tick_seconds": self.tick_seconds,
            "max_consecutive_errors": self.max_consecutive_errors,
            "active_hours": {
                "weekday": f"{self.active_start_hour}:00-{self.active_end_hour}:00",
                "weekend": f"{self.weekend_start_hour}:00-{self.weekend_end_hour}:00",
            },
        }


# ---------------------------------------------------------------------------
# Agent Runtime — the "OS kernel"
# ---------------------------------------------------------------------------

class RecoveryLevel(str, Enum):
    """四级恢复策略 (§5.3)。"""
    L0_skip = "L0_skip"
    L1_retry = "L1_retry"
    L2_degrade = "L2_degrade"
    L3_abort = "L3_abort"


class AgentRuntime:
    """Generic, long-lived Agent Runtime for Pulse.

    Responsibilities (all business-agnostic):
      - Manage a BackgroundSchedulerRunner heartbeat loop
      - Accept patrol task registrations from *any* module
      - Wrap each patrol execution with structured error recovery,
        circuit-breaker logic, and EventBus observability
      - Run HeartbeatLoop for Runtime self-check
      - Expose lifecycle controls (start / stop / status / trigger / wake)

    Business modules (BOSS, Intel, Calendar, Research, ...) register
    themselves via ``register_patrol()`` during their ``on_startup``.
    The Runtime never imports or references any module directly.
    """

    def __init__(
        self,
        *,
        event_emitter: EventEmitter | None = None,
        config: RuntimeConfig | None = None,
        hooks: HookRegistry | None = None,
        compaction_engine: Any | None = None,
        promotion_engine: Any | None = None,
        recall_memory: Any | None = None,
        workspace_memory: Any | None = None,
    ) -> None:
        self._config = config or RuntimeConfig()
        self._event_emitter = event_emitter
        self._hooks = hooks or HookRegistry()
        self._compaction = compaction_engine
        self._promotion = promotion_engine
        self._recall_memory = recall_memory
        self._workspace_memory = workspace_memory
        self._runner = BackgroundSchedulerRunner(
            tick_seconds=self._config.tick_seconds,
        )
        self._started_at: datetime | None = None
        self._consecutive_errors: dict[str, int] = {}
        self._patrol_stats: dict[str, dict[str, Any]] = {}
        self._heartbeat_count: int = 0
        self._retry_backoff: dict[str, float] = {}  # task_name → next retry timestamp
        self._patrol_handlers: dict[str, tuple[Callable[[TaskContext], Any], str | None, int]] = {}
        # task_name → (handler, workspace_id, token_budget)
        self._heartbeat_task_name = "__runtime_heartbeat__"

        # Subagent lifecycle (§6.1)
        self._subagents: dict[str, SubagentRecord] = {}

        # Checkpoint store (§6.2)
        self._checkpoints: dict[str, TaskCheckpoint] = {}

        # Takeover state (§6.3)
        self._takeover_state: TakeoverState = TakeoverState.autonomous
        self._takeover_reason: str | None = None

        self._register_internal_heartbeat_task()

    # -- public properties --------------------------------------------------

    @property
    def config(self) -> RuntimeConfig:
        return self._config

    @property
    def runner(self) -> BackgroundSchedulerRunner:
        return self._runner

    # -- task registration (called by modules) ------------------------------

    def register_patrol(
        self,
        *,
        name: str,
        handler: Callable[[TaskContext], Any],
        peak_interval: int,
        offpeak_interval: int,
        enabled: bool = False,
        active_hours_only: bool = True,
        workspace_id: str | None = None,
        token_budget: int = 4000,
    ) -> None:
        """Register an arbitrary patrol task.

        This is the **only** coupling point between modules and the Runtime.
        The Runtime treats every task identically — it does not interpret
        the task name or know what the handler does.

        ``enabled`` defaults to ``False`` by design (ADR-004 §6.1.1): the
        module's ``on_startup`` must register patrols **unconditionally** so
        the conversational control plane can see them; the user flips them on
        later through ``system.patrol.enable`` over IM. Tests and internal
        tasks that need "registered AND running immediately" may pass
        ``enabled=True`` explicitly — a test-friendly escape hatch, never a
        production code path.

        Handler signature: ``handler(ctx: TaskContext) -> Any``
        """

        def _wrapped_handler() -> None:
            self._execute_patrol(
                name, handler,
                workspace_id=workspace_id,
                token_budget=token_budget,
            )

        self._patrol_handlers[name] = (handler, workspace_id, token_budget)

        task = ScheduleTask(
            name=name,
            interval_seconds=peak_interval,
            handler=_wrapped_handler,
            enabled=enabled,
            run_immediately=False,
            peak_interval_seconds=peak_interval,
            offpeak_interval_seconds=offpeak_interval,
            active_hours_only=active_hours_only,
            active_start=self._config.active_start_hour,
            active_end=self._config.active_end_hour,
        )
        self._runner.register(task)
        self._consecutive_errors[name] = 0
        self._patrol_stats[name] = {
            "total_turns": 0,
            "total_runs": 0,
            "total_errors": 0,
            "last_outcome": None,
            "last_run_at": None,
            "last_error": None,
            "circuit_open": False,
        }
        logger.info(
            "Registered patrol: %s  peak=%ds  offpeak=%ds  enabled=%s",
            name, peak_interval, offpeak_interval, enabled,
        )

    # -- structured patrol execution ----------------------------------------

    def _execute_patrol(
        self,
        name: str,
        handler: Callable[[TaskContext], Any],
        *,
        workspace_id: str | None = None,
        token_budget: int = 4000,
    ) -> None:
        """Execute one patrol turn with the 5-stage pipeline:
        Guard -> Hook -> Execute -> Recover -> Record -> Emit."""
        ctx = create_patrol_context(
            task_name=name,
            workspace_id=workspace_id,
            token_budget=token_budget,
        )
        ctx.start_clock()
        # ADR-005 §2 bind patrol trace_id to this thread's ContextVar so
        # every downstream logger (stage events, connector._invoke,
        # mcp_transport_http._open_response) picks it up. Without this the
        # cross-process header `X-Pulse-Trace-Id` never gets injected and
        # boss_mcp.log drops into the "-" sink — which is exactly the gap
        # the 2026-04-22 post-mortem exposed (see ADR-005 changelog).
        # Reset in finally so a reused scheduler worker thread cannot leak
        # the trace to an unrelated next job.
        set_trace_id(ctx.trace_id)
        try:
            self._execute_patrol_body(ctx, name, handler)
        finally:
            set_trace_id(None)

    def _execute_patrol_body(
        self,
        ctx: TaskContext,
        name: str,
        handler: Callable[[TaskContext], Any],
    ) -> None:
        """Inner body of ``_execute_patrol`` — extracted to keep the
        ``set_trace_id`` try/finally wrapper tight.

        Why split: the original body has ~6 early-return branches (takeover
        guard / circuit breaker / backoff / hook block / ...). Wrapping all
        of them directly in a try/finally around the full method would
        work, but this split makes the "trace bind" contract visually
        explicit at the single call site and ensures the reset runs on
        every path (including uncaught exceptions from ``handler``).
        """
        stats = self._patrol_stats.get(name, {})

        # Stage -1: Takeover guard — human_control/paused 时跳过 patrol（heartbeat 除外）
        if self._takeover_state in (TakeoverState.human_control, TakeoverState.paused):
            skip_this = True
            if self._takeover_state == TakeoverState.paused and name == self._heartbeat_task_name:
                skip_this = False
            if skip_this:
                self._finish_task(
                    ctx,
                    outcome=PatrolOutcome.skipped_disabled,
                    error="takeover_active",
                    task_name=name,
                    stats=stats,
                    count_as_run=False,
                    recovery_level=RecoveryLevel.L0_skip.value,
                )
                return

        # Stage 0: L0 — circuit breaker check (skip)
        if stats.get("circuit_open"):
            self._emit("runtime.patrol.circuit_breaker", {
                "task_name": name,
                "trace_id": ctx.trace_id,
                "action": "skipped_circuit_open",
            })
            self._hooks.fire(
                HookPoint.on_recovery,
                ctx,
                {
                    "task_name": name,
                    "source": "runtime",
                    "recovery_level": RecoveryLevel.L0_skip.value,
                    "reason": "circuit_open",
                },
            )
            self._finish_task(
                ctx,
                outcome=PatrolOutcome.error_aborted,
                error="circuit_open",
                task_name=name,
                stats=stats,
                count_as_run=False,
                recovery_level=RecoveryLevel.L0_skip.value,
            )
            return

        # Stage 0b: L1 — retry backoff check
        backoff_until = self._retry_backoff.get(name, 0.0)
        if backoff_until > time.monotonic():
            logger.debug("Patrol %s in backoff, skipping until %.0f", name, backoff_until)
            self._hooks.fire(
                HookPoint.on_recovery,
                ctx,
                {
                    "task_name": name,
                    "source": "runtime",
                    "recovery_level": RecoveryLevel.L1_retry.value,
                    "reason": "retry_backoff",
                    "backoff_until": backoff_until,
                },
            )
            self._finish_task(
                ctx,
                outcome=PatrolOutcome.error_recovered,
                error="retry_backoff",
                task_name=name,
                stats=stats,
                count_as_run=False,
                recovery_level=RecoveryLevel.L1_retry.value,
            )
            return

        # Stage 1: Hook — beforeTaskStart (可阻断)
        hook_result = self._hooks.fire(
            HookPoint.before_task_start, ctx,
            {"task_name": name, "source": "patrol"},
        )
        if hook_result.block:
            self._emit("runtime.patrol.blocked", {
                "task_name": name,
                "trace_id": ctx.trace_id,
                "reason": hook_result.reason,
            })
            self._finish_task(
                ctx,
                outcome=PatrolOutcome.skipped_not_ready,
                error=hook_result.reason,
                task_name=name,
                stats=stats,
                count_as_run=False,
                recovery_level=RecoveryLevel.L0_skip.value,
            )
            return

        self._emit("runtime.patrol.started", {
            "task_name": name,
            "trace_id": ctx.trace_id,
            "run_id": ctx.run_id,
            "task_id": ctx.task_id,
            "mode": ctx.mode.value,
        })

        outcome = PatrolOutcome.completed
        error_msg: str | None = None

        try:
            result = handler(ctx)

            # L2 — degrade: handler 返回 {ok: false}
            if isinstance(result, dict) and result.get("ok") is False:
                outcome = PatrolOutcome.degraded
                error_msg = str(result.get("errors", "unknown"))[:500]
            self._consecutive_errors[name] = 0
            self._retry_backoff.pop(name, None)

        except Exception as exc:
            error_msg = str(exc)[:500]
            consecutive = self._consecutive_errors.get(name, 0) + 1
            self._consecutive_errors[name] = consecutive

            if consecutive >= self._config.max_consecutive_errors:
                # L3 — abort + circuit break
                outcome = PatrolOutcome.error_aborted
                stats["circuit_open"] = True
                logger.error(
                    "Circuit breaker OPEN for %s after %d consecutive errors: %s",
                    name, consecutive, error_msg,
                )
                self._emit("runtime.patrol.circuit_breaker", {
                    "task_name": name,
                    "trace_id": ctx.trace_id,
                    "consecutive_errors": consecutive,
                    "error": error_msg,
                    "action": "circuit_opened",
                })
                self._hooks.fire(
                    HookPoint.on_recovery, ctx,
                    {
                        "task_name": name,
                        "source": "runtime",
                        "recovery_level": RecoveryLevel.L3_abort.value,
                        "error": error_msg,
                        "consecutive_errors": consecutive,
                    },
                )
                self._hooks.fire(
                    HookPoint.on_circuit_open, ctx,
                    {"task_name": name, "consecutive_errors": consecutive},
                )
            else:
                # L1 — retry with exponential backoff
                outcome = PatrolOutcome.error_recovered
                backoff_secs = min(300, 2 ** consecutive)
                self._retry_backoff[name] = time.monotonic() + backoff_secs
                logger.warning(
                    "Patrol %s error (%d/%d), backoff %ds: %s",
                    name, consecutive,
                    self._config.max_consecutive_errors,
                    backoff_secs, error_msg,
                )
                self._hooks.fire(
                    HookPoint.on_recovery, ctx,
                    {
                        "task_name": name,
                        "source": "runtime",
                        "recovery_level": RecoveryLevel.L1_retry.value,
                        "error": error_msg,
                        "backoff_seconds": backoff_secs,
                        "consecutive_errors": consecutive,
                    },
                )

        self._finish_task(ctx, outcome=outcome, error=error_msg, task_name=name, stats=stats)

    def _register_internal_heartbeat_task(self) -> None:
        def _heartbeat_handler() -> None:
            self.heartbeat()

        self._runner.register(
            ScheduleTask(
                name=self._heartbeat_task_name,
                interval_seconds=self._config.tick_seconds,
                handler=_heartbeat_handler,
                enabled=True,
                run_immediately=True,
                active_hours_only=False,
            )
        )

    def _finish_task(
        self,
        ctx: TaskContext,
        *,
        outcome: PatrolOutcome,
        error: str | None = None,
        task_name: str | None = None,
        stats: dict[str, Any] | None = None,
        count_as_run: bool = True,
        recovery_level: str | None = None,
    ) -> None:
        safe_task_name = task_name or ctx.task_id or "runtime_task"
        safe_stats = stats
        if safe_stats is None and task_name:
            safe_stats = self._patrol_stats.get(task_name)

        elapsed_ms = ctx.elapsed_ms()
        final_recovery_level = recovery_level or (
            RecoveryLevel.L3_abort.value if outcome == PatrolOutcome.error_aborted
            else RecoveryLevel.L1_retry.value if outcome == PatrolOutcome.error_recovered
            else RecoveryLevel.L2_degrade.value if outcome == PatrolOutcome.degraded
            else RecoveryLevel.L0_skip.value if outcome in {PatrolOutcome.skipped_not_ready, PatrolOutcome.skipped_disabled, PatrolOutcome.skipped_budget, PatrolOutcome.skipped_inactive}
            else "none"
        )

        if safe_stats is not None:
            safe_stats["total_turns"] = safe_stats.get("total_turns", 0) + 1
            if count_as_run:
                safe_stats["total_runs"] = safe_stats.get("total_runs", 0) + 1
            if count_as_run and outcome in (PatrolOutcome.error_recovered, PatrolOutcome.error_aborted):
                safe_stats["total_errors"] = safe_stats.get("total_errors", 0) + 1
            safe_stats["last_error"] = error
            safe_stats["last_outcome"] = outcome.value
            safe_stats["last_run_at"] = datetime.now(timezone.utc).isoformat()
            safe_stats["last_trace_id"] = ctx.trace_id
            safe_stats["last_run_id"] = ctx.run_id
            safe_stats["recovery_level"] = final_recovery_level

        self._hooks.fire(
            HookPoint.on_task_end,
            ctx,
            {
                "task_name": safe_task_name,
                "outcome": outcome.value,
                "error": error,
                "elapsed_ms": elapsed_ms,
                "recovery_level": final_recovery_level,
            },
        )

        if ctx.mode != ExecutionMode.heartbeat_turn:
            if outcome == PatrolOutcome.completed:
                event_type = "runtime.patrol.completed"
            elif outcome == PatrolOutcome.degraded:
                event_type = "runtime.patrol.degraded"
            else:
                event_type = "runtime.patrol.failed"
            self._emit(event_type, {
                "task_name": safe_task_name,
                "trace_id": ctx.trace_id,
                "run_id": ctx.run_id,
                "task_id": ctx.task_id,
                "outcome": outcome.value,
                "elapsed_ms": elapsed_ms,
                "error": error,
                "recovery_level": final_recovery_level,
            })

    # -- lifecycle controls -------------------------------------------------

    def start(self) -> bool:
        """Start the heartbeat loop.  Returns False if disabled or already running."""
        if not self._config.enabled:
            logger.info("AgentRuntime disabled (AGENT_RUNTIME_ENABLED != true)")
            return False

        started = self._runner.start()
        if started:
            self._started_at = datetime.now(timezone.utc)
            tasks = self._runner.engine.list_tasks()
            logger.info(
                "AgentRuntime STARTED — %d patrol task(s) registered: %s",
                len(tasks), tasks,
            )
            self._emit("runtime.lifecycle.started", {
                "tasks": tasks,
                "config": self._config.to_dict(),
            })
        return started

    def stop(self) -> bool:
        was_running = self._runner.stop()
        if was_running:
            uptime = 0.0
            if self._started_at:
                uptime = (datetime.now(timezone.utc) - self._started_at).total_seconds()
            logger.info("AgentRuntime STOPPED (uptime=%.0fs)", uptime)
            self._emit("runtime.lifecycle.stopped", {
                "reason": "manual_stop",
                "uptime_sec": uptime,
            })
            self._started_at = None
        return was_running

    def reset_circuit_breaker(self, task_name: str) -> bool:
        stats = self._patrol_stats.get(task_name)
        if stats is None:
            return False
        stats["circuit_open"] = False
        self._consecutive_errors[task_name] = 0
        logger.info("Circuit breaker reset for %s", task_name)
        self._emit("runtime.patrol.circuit_breaker", {
            "task_name": task_name,
            "action": "manual_reset",
        })
        return True

    # -- per-patrol control plane (ADR-004 §6.1) ----------------------------

    def list_patrols(self) -> list[dict[str, Any]]:
        """Snapshot every module-registered patrol. Internal heartbeat
        is excluded — it is not a user-controllable patrol (ADR-004 §6.1.7
        invariant #1).
        """
        out: list[dict[str, Any]] = []
        for name in self._runner.engine.list_tasks():
            if name == self._heartbeat_task_name:
                continue
            snapshot = self._patrol_snapshot(name)
            if snapshot is not None:
                out.append(snapshot)
        return out

    def get_patrol_stats(self, name: str) -> dict[str, Any] | None:
        """Snapshot a single patrol; ``None`` if unknown or is internal
        heartbeat. Callers treat ``None`` as fail-loud (not found)."""
        if name == self._heartbeat_task_name:
            return None
        return self._patrol_snapshot(name)

    def enable_patrol(self, name: str) -> bool:
        """Turn a patrol ON at runtime. Returns True on success, False if
        unknown or is internal heartbeat. Emits
        ``runtime.patrol.lifecycle.enabled`` on success.
        """
        if name == self._heartbeat_task_name:
            return False
        ok = self._runner.engine.set_enabled(name, True)
        if ok:
            self._emit("runtime.patrol.lifecycle.enabled", {"task_name": name})
            logger.info("Patrol enabled: %s", name)
        return ok

    def disable_patrol(self, name: str) -> bool:
        """Turn a patrol OFF at runtime. Returns True on success, False if
        unknown or is internal heartbeat. Emits
        ``runtime.patrol.lifecycle.disabled`` on success.
        """
        if name == self._heartbeat_task_name:
            return False
        ok = self._runner.engine.set_enabled(name, False)
        if ok:
            self._emit("runtime.patrol.lifecycle.disabled", {"task_name": name})
            logger.info("Patrol disabled: %s", name)
        return ok

    def run_patrol_once(self, name: str) -> dict[str, Any]:
        """Execute one tick of a patrol right now, bypassing interval
        gating. Blocks until the patrol handler returns. Full 5-stage
        pipeline (including circuit-breaker L0 skip) still applies —
        ADR-004 §6.1.7 invariant #3.
        """
        if name == self._heartbeat_task_name:
            return {"ok": False, "error": "internal heartbeat is not controllable"}
        entry = self._patrol_handlers.get(name)
        if entry is None:
            return {"ok": False, "error": f"patrol not found: {name}"}
        handler, workspace_id, token_budget = entry
        self._emit("runtime.patrol.lifecycle.triggered", {"task_name": name})
        self._execute_patrol(
            name,
            handler,
            workspace_id=workspace_id,
            token_budget=token_budget,
        )
        stats = self._patrol_stats.get(name, {})
        return {
            "ok": True,
            "task_name": name,
            "last_outcome": stats.get("last_outcome"),
            "last_run_at": stats.get("last_run_at"),
            "last_trace_id": stats.get("last_trace_id"),
            "last_error": stats.get("last_error"),
        }

    def _patrol_snapshot(self, name: str) -> dict[str, Any] | None:
        task = self._runner.engine.get_task(name)
        if task is None:
            return None
        stats = self._patrol_stats.get(name, {})
        return {
            "name": name,
            "enabled": bool(task.enabled),
            "peak_interval_seconds": task.peak_interval_seconds,
            "offpeak_interval_seconds": task.offpeak_interval_seconds,
            "active_hours_only": bool(task.active_hours_only),
            "stats": dict(stats),
        }

    def status(self) -> dict[str, Any]:
        runner_status = self._runner.status()
        return {
            "enabled": self._config.enabled,
            "running": runner_status["running"],
            "started_at": (
                self._started_at.isoformat() if self._started_at else None
            ),
            "uptime_sec": (
                (datetime.now(timezone.utc) - self._started_at).total_seconds()
                if self._started_at else None
            ),
            "config": self._config.to_dict(),
            "scheduler": runner_status,
            "patrols": dict(self._patrol_stats),
            "takeover_state": self._takeover_state.value,
            "subagents_active": sum(1 for r in self._subagents.values() if r.status == "running"),
            "checkpoints_pending": len(self._checkpoints),
        }

    async def trigger_once(self) -> list[str]:
        """Manually trigger one heartbeat tick (for debugging / API)."""
        return await self._runner.run_once()

    # -- HeartbeatLoop (P3) -------------------------------------------------

    def heartbeat(self) -> dict[str, Any]:
        """Runtime 内核自检心跳 (§3.2/§5.2)。

        该心跳由 Runtime 后台调度自动执行，负责：
          1. 汇总 patrol 健康度
          2. 聚合最近 task summaries
          3. 生成 session summaries 并在有 workspace 时继续生成 workspace summary
          4. 周期性触发 promotion
          5. 发射 heartbeat 事件
        """
        self._heartbeat_count += 1
        ctx = create_heartbeat_context()
        ctx.start_clock()
        now = datetime.now(timezone.utc)
        is_active = self._config.is_active(now)

        patrol_stats = {
            name: stat for name, stat in self._patrol_stats.items()
            if name != self._heartbeat_task_name
        }
        total_patrols = len(patrol_stats)
        healthy = sum(
            1 for s in patrol_stats.values()
            if not s.get("circuit_open") and s.get("last_outcome") != PatrolOutcome.error_aborted.value
        )
        circuit_open_tasks = [
            name for name, s in patrol_stats.items()
            if s.get("circuit_open")
        ]

        compaction_triggered = False
        compacted_sessions = 0
        compacted_workspaces = 0
        promotion_triggered = False
        promoted_facts = 0

        if self._recall_memory is not None and self._compaction is not None and self._heartbeat_count % 10 == 0:
            try:
                recent_task_summaries = self._recall_memory.recent(limit=200, role="system")
                grouped_by_session: dict[str, list[dict[str, Any]]] = {}
                for entry in recent_task_summaries:
                    metadata = entry.get("metadata") or {}
                    if str(metadata.get("envelope_kind") or "") != "task_summary":
                        continue
                    session_key = str(entry.get("session_id") or "").strip()
                    if not session_key:
                        continue
                    grouped_by_session.setdefault(session_key, []).append(entry)

                workspace_bucket: dict[str, list[str]] = {}
                for session_key, entries in grouped_by_session.items():
                    latest = entries[-5:]
                    session_ctx = TaskContext(
                        task_id=f"session:{session_key}",
                        session_id=session_key,
                        mode=ExecutionMode.heartbeat_turn,
                        isolation_level=IsolationLevel.light_context,
                        prompt_contract="heartbeatPrompt",
                        workspace_id=str(latest[-1].get("workspace_id") or "").strip() or None,
                        token_budget=2000,
                    )
                    output = self._compaction.compact_session(
                        session_ctx,
                        [str(e.get("text") or "") for e in latest],
                        outcome="heartbeat_session_compaction",
                    )
                    if self._recall_memory is not None:
                        envelope = self._compaction.to_envelope(session_ctx, output)
                        self._recall_memory.store_envelope(envelope)
                    compacted_sessions += 1
                    compaction_triggered = True
                    ws_id = session_ctx.workspace_id
                    if ws_id:
                        workspace_bucket.setdefault(ws_id, []).append(output.summary)

                if self._workspace_memory is not None:
                    for workspace_id, session_summaries in workspace_bucket.items():
                        ws_ctx = TaskContext(
                            task_id=f"workspace:{workspace_id}",
                            workspace_id=workspace_id,
                            mode=ExecutionMode.heartbeat_turn,
                            isolation_level=IsolationLevel.light_context,
                            prompt_contract="heartbeatPrompt",
                            token_budget=2000,
                        )
                        existing_summary = self._workspace_memory.get_summary(workspace_id)
                        ws_output = self._compaction.compact_workspace(
                            ws_ctx,
                            session_summaries,
                            existing_workspace_summary=existing_summary,
                        )
                        self._workspace_memory.set_summary(
                            workspace_id,
                            ws_output.summary,
                            ws_output.token_estimate,
                        )
                        compacted_workspaces += 1
                        compaction_triggered = True
            except Exception as exc:
                logger.warning("Heartbeat compaction failed: %s", exc)

        if self._promotion is not None and self._recall_memory is not None and self._heartbeat_count % 20 == 0:
            try:
                promotion_entries = self._recall_memory.recent(limit=60)
                if promotion_entries:
                    results = self._promotion.promote(ctx, promotion_entries)
                    promoted_facts = sum(1 for r in results if r.promoted)
                    promotion_triggered = promoted_facts > 0
            except Exception as exc:
                logger.warning("Heartbeat promotion failed: %s", exc)

        elapsed_ms = ctx.elapsed_ms()

        # Stage 5: 触发到期 patrol —— 必须尊重 ScheduleTask.enabled。
        # ADR-004 §6.1.1 规定启停是 IM 独占的单一认知路径
        # (system.patrol.enable/disable → engine.set_enabled)。
        # 以前这里直接遍历 _patrol_handlers, 绕过 enabled 检查, 把
        # register_patrol(enabled=False) 的保证打穿 —— 结果是用户只
        # 开 job_chat.patrol, heartbeat 每分钟还是帮他跑一次
        # job_greet.patrol, 浏览器反复跳去搜索页 scan+打分但又因
        # confirm_execute=False 不真发打招呼。典型的 ghost tick。
        triggered_patrols: list[str] = []
        if is_active and self._takeover_state == TakeoverState.autonomous:
            engine = self._runner.engine
            for name, entry in self._patrol_handlers.items():
                if name == self._heartbeat_task_name:
                    continue
                task = engine.get_task(name)
                if task is None or not task.enabled:
                    continue
                stats = self._patrol_stats.get(name, {})
                if stats.get("circuit_open"):
                    continue
                handler, ws_id, budget = entry
                self._execute_patrol(name, handler, workspace_id=ws_id, token_budget=budget)
                triggered_patrols.append(name)

        result = {
            "heartbeat_count": self._heartbeat_count,
            "is_active": is_active,
            "total_patrols": total_patrols,
            "healthy_patrols": healthy,
            "circuit_open_tasks": circuit_open_tasks,
            "compaction_triggered": compaction_triggered,
            "compacted_sessions": compacted_sessions,
            "compacted_workspaces": compacted_workspaces,
            "promotion_triggered": promotion_triggered,
            "promoted_facts": promoted_facts,
            "triggered_patrols": triggered_patrols,
            "elapsed_ms": elapsed_ms,
        }

        self._emit("runtime.heartbeat", {
            "trace_id": ctx.trace_id,
            **result,
        })
        self._finish_task(ctx, outcome=PatrolOutcome.completed, task_name=self._heartbeat_task_name)
        return result

    def manual_wake(self) -> dict[str, Any]:
        """手动触发完整 heartbeat turn (§5.2 ManualWake)。

        可通过 API `POST /api/runtime/wake` 调用。
        返回 heartbeat 结果 + 所有 patrol 的即时触发结果。
        """
        hb_result = self.heartbeat()

        # 即时触发所有非熔断 + 已 enable 的 patrol。
        # 同 heartbeat Stage 5 的契约: enabled 是单一认知路径,
        # manual_wake 不是授权通道, 不得跑用户没开的 patrol。
        triggered: list[str] = []
        engine = self._runner.engine
        for name, stats in self._patrol_stats.items():
            if stats.get("circuit_open"):
                continue
            task = engine.get_task(name)
            if task is None or not task.enabled:
                continue
            entry = self._patrol_handlers.get(name)
            if entry is None:
                continue
            handler, ws_id, budget = entry
            self._execute_patrol(name, handler, workspace_id=ws_id, token_budget=budget)
            triggered.append(name)

        hb_result["manual_wake"] = True
        hb_result["triggered_patrols"] = triggered

        self._emit("runtime.manual_wake", {
            "heartbeat": hb_result,
            "triggered_patrols": triggered,
        })

        return hb_result

    # -- Subagent lifecycle (§6.1) ------------------------------------------

    def spawn_subagent(
        self,
        *,
        parent_task_id: str,
        handler: Callable[[TaskContext], Any],
        parent_session_id: str | None = None,
        workspace_id: str | None = None,
        token_budget: int = 4000,
    ) -> SubagentRecord:
        """派生子任务。

        创建 subagentTask 模式的 TaskContext，注册到 subagent 表，
        然后立即执行 handler。parent 可通过 collect_subagent 获取结果。
        """
        if self._takeover_state == TakeoverState.human_control:
            raise RuntimeError("Cannot spawn subagent during human takeover")

        ctx = create_subagent_context(
            parent_task_id=parent_task_id,
            parent_session_id=parent_session_id,
            workspace_id=workspace_id,
            token_budget=token_budget,
        )
        ctx.start_clock()

        record = SubagentRecord(
            subagent_task_id=ctx.task_id,
            parent_task_id=parent_task_id,
            ctx=ctx,
            status="running",
        )
        self._subagents[ctx.task_id] = record

        self._emit("runtime.subagent.spawned", {
            "subagent_task_id": ctx.task_id,
            "parent_task_id": parent_task_id,
            "trace_id": ctx.trace_id,
        })

        try:
            result = handler(ctx)
            record.status = "completed"
            record.result = result
        except Exception as exc:
            record.status = "failed"
            record.result = {"error": str(exc)[:500]}
            logger.warning("Subagent %s failed: %s", ctx.task_id, exc)

        self._emit("runtime.subagent.finished", {
            "subagent_task_id": ctx.task_id,
            "parent_task_id": parent_task_id,
            "status": record.status,
            "elapsed_ms": ctx.elapsed_ms(),
        })

        return record

    def collect_subagent(self, subagent_task_id: str) -> SubagentRecord | None:
        """回收子任务结果。"""
        return self._subagents.get(subagent_task_id)

    def list_subagents(self, parent_task_id: str | None = None) -> list[dict[str, Any]]:
        """列出子任务。可按 parent_task_id 过滤。"""
        records = self._subagents.values()
        if parent_task_id:
            records = [r for r in records if r.parent_task_id == parent_task_id]
        return [
            {
                "subagent_task_id": r.subagent_task_id,
                "parent_task_id": r.parent_task_id,
                "status": r.status,
                "created_at": r.created_at,
            }
            for r in records
        ]

    # -- Checkpoint / Resume (§6.2) -----------------------------------------

    def save_checkpoint(self, checkpoint: TaskCheckpoint) -> str:
        """保存任务执行快照，用于后续 resume。"""
        self._checkpoints[checkpoint.task_id] = checkpoint
        self._emit("runtime.checkpoint.saved", {
            "task_id": checkpoint.task_id,
            "trace_id": checkpoint.trace_id,
            "stopped_reason": checkpoint.stopped_reason,
            "step_index": checkpoint.step_index,
        })
        logger.info("Checkpoint saved: task=%s step=%d", checkpoint.task_id, checkpoint.step_index)
        return checkpoint.task_id

    def resume_task(
        self,
        task_id: str,
        handler: Callable[[TaskContext], Any],
    ) -> dict[str, Any]:
        """从 checkpoint 恢复执行。

        创建 resumedTask 模式的 TaskContext（保留原始 trace_id），
        将 checkpoint 数据注入 ctx.extra["checkpoint"]，然后执行 handler。
        """
        checkpoint = self._checkpoints.get(task_id)
        if checkpoint is None:
            return {"ok": False, "error": f"No checkpoint found for task_id={task_id}"}

        if self._takeover_state == TakeoverState.human_control:
            return {"ok": False, "error": "Cannot resume during human takeover"}

        ctx = create_resumed_context(
            original_task_id=checkpoint.task_id,
            original_trace_id=checkpoint.trace_id,
            session_id=checkpoint.session_id,
            workspace_id=checkpoint.workspace_id,
            token_budget=4000,
            checkpoint_data=checkpoint.to_dict(),
        )
        ctx.start_clock()

        self._emit("runtime.task.resumed", {
            "task_id": task_id,
            "trace_id": ctx.trace_id,
            "run_id": ctx.run_id,
            "original_stopped_reason": checkpoint.stopped_reason,
        })

        try:
            result = handler(ctx)
            del self._checkpoints[task_id]
            return {"ok": True, "result": result, "elapsed_ms": ctx.elapsed_ms()}
        except Exception as exc:
            logger.warning("Resumed task %s failed: %s", task_id, exc)
            return {"ok": False, "error": str(exc)[:500], "elapsed_ms": ctx.elapsed_ms()}

    def list_checkpoints(self) -> list[dict[str, Any]]:
        """列出所有可恢复的 checkpoint。"""
        return [cp.to_dict() for cp in self._checkpoints.values()]

    # -- Manual Takeover (§6.3) ---------------------------------------------

    def pause_patrols(self, *, reason: str = "manual_pause") -> dict[str, Any]:
        """暂停 patrol 调度，但 heartbeat 仍运行。

        与 request_takeover 的区别：
          - paused: patrol 暂停，heartbeat 继续，subagent 可派生
          - human_control: 全部暂停，subagent 不可派生
        """
        prev = self._takeover_state
        self._takeover_state = TakeoverState.paused
        self._takeover_reason = reason

        self._emit("runtime.patrols.paused", {
            "previous_state": prev.value,
            "new_state": self._takeover_state.value,
            "reason": reason,
        })
        logger.info("Patrols paused: %s → paused (reason=%s)", prev.value, reason)

        return {
            "state": self._takeover_state.value,
            "reason": reason,
        }

    def request_takeover(self, *, reason: str = "manual") -> dict[str, Any]:
        """请求人工接管。暂停所有 patrol 调度和 heartbeat。"""
        prev = self._takeover_state
        self._takeover_state = TakeoverState.human_control
        self._takeover_reason = reason

        if self._runner.status()["running"]:
            self._runner.stop()

        self._emit("runtime.takeover.requested", {
            "previous_state": prev.value,
            "new_state": self._takeover_state.value,
            "reason": reason,
        })
        logger.info("Takeover requested: %s → %s (reason=%s)", prev.value, self._takeover_state.value, reason)

        return {
            "state": self._takeover_state.value,
            "reason": reason,
            "patrols_paused": True,
        }

    def release_takeover(self, *, auto_restart: bool = True) -> dict[str, Any]:
        """释放人工接管，恢复自主模式。"""
        prev = self._takeover_state
        self._takeover_state = TakeoverState.autonomous
        self._takeover_reason = None

        restarted = False
        if auto_restart and self._config.enabled:
            restarted = self._runner.start()

        self._emit("runtime.takeover.released", {
            "previous_state": prev.value,
            "new_state": self._takeover_state.value,
            "restarted": restarted,
        })
        logger.info("Takeover released: %s → autonomous (restarted=%s)", prev.value, restarted)

        return {
            "state": self._takeover_state.value,
            "restarted": restarted,
        }

    @property
    def takeover_state(self) -> TakeoverState:
        return self._takeover_state

    # -- internal helpers ---------------------------------------------------

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._event_emitter is None:
            return
        try:
            self._event_emitter(event_type, payload)
        except Exception:
            logger.exception("Failed to emit runtime event: %s", event_type)


# ---------------------------------------------------------------------------
# Env helpers (reusable by modules too — intentionally module-level)
# ---------------------------------------------------------------------------

def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, "").strip().lower()
    if not val:
        return default
    return val in ("true", "1", "yes", "on")


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default
