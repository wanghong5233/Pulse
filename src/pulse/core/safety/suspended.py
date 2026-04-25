"""SafetyPlane · SuspendedTaskStore.

Ask / Resume 三步跃迁的状态机 + 持久化 + 事件发射层. Policy 产出
``Decision.ask`` 后, 调用方 (Service 层 ``_execute_*`` / IM Resume 路由)
通过本存储管理被挂起任务的全生命周期.

* 持久化: 复用 ``WorkspaceMemory.workspace_facts``, key 前缀
  ``safety.suspended.`` —— 不新建表.
* 事件: 每次状态跃迁 publish 一枚 EventBus 事件, 审计由
  ``JsonlEventSink`` 承担, 本层不直接写盘.
* 幂等: 同一 ``(module, trace_id, intent_name)`` 的 ``awaiting_user``
  任务全局唯一, 二次 create 返回既有任务, 不骚扰用户.

规约权威: ``docs/adr/ADR-006-v2-SafetyPlane.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import logging
from typing import Any, Iterable, Literal, Mapping, Protocol, runtime_checkable

from pulse.core.safety.decision import AskRequest
from pulse.core.safety.intent import Intent

__all__ = (
    "EVENT_TASK_SUSPENDED",
    "EVENT_TASK_RESUMED",
    "EVENT_TASK_ASK_TIMEOUT",
    "EVENT_TASK_DENIED",
    "FACT_KEY_PREFIX",
    "FactsStore",
    "EventPublisher",
    "SuspendedTask",
    "SuspendedTaskStatus",
    "SuspendedTaskStore",
    "TaskAlreadyTerminalError",
    "TaskNotFoundError",
    "TERMINAL_STATUSES",
    "VALID_SUSPENDED_STATUSES",
    "WorkspaceSuspendedTaskStore",
)

_LOGGER = logging.getLogger(__name__)

SuspendedTaskStatus = Literal["awaiting_user", "resumed", "timed_out", "denied"]
VALID_SUSPENDED_STATUSES: frozenset[str] = frozenset(
    ("awaiting_user", "resumed", "timed_out", "denied")
)
TERMINAL_STATUSES: frozenset[str] = frozenset(("resumed", "timed_out", "denied"))

FACT_KEY_PREFIX = "safety.suspended."
_FACT_SOURCE = "safety_plane"

EVENT_TASK_SUSPENDED = "task.suspended"
EVENT_TASK_RESUMED = "task.resumed"
EVENT_TASK_ASK_TIMEOUT = "task.ask_timeout"
EVENT_TASK_DENIED = "task.denied"


# ── Exceptions ─────────────────────────────────────────────


class TaskNotFoundError(KeyError):
    """请求跃迁的 task_id 不存在于当前 workspace."""


class TaskAlreadyTerminalError(RuntimeError):
    """请求跃迁的任务已处于终态 (resumed / timed_out / denied)."""


# ── Backend Protocols ──────────────────────────────────────
# 用 Protocol 而非直接依赖 WorkspaceMemory/EventBus, 让单测可注入 fake
# 而不用搭 DB + 完整 bus, 同时把本模块和两个大组件解耦.


@runtime_checkable
class FactsStore(Protocol):
    """最小子集, 对齐 ``WorkspaceMemory`` 上的四个方法."""

    def get_fact(self, workspace_id: str, key: str, default: Any = None) -> Any: ...

    def set_fact(
        self,
        workspace_id: str,
        key: str,
        value: Any,
        *,
        source: str = "",
    ) -> None: ...

    def list_facts_by_prefix(self, workspace_id: str, prefix: str) -> list[Any]: ...

    def delete_fact(self, workspace_id: str, key: str) -> bool: ...


@runtime_checkable
class EventPublisher(Protocol):
    """对齐 ``EventBus.publish`` 签名."""

    def publish(
        self, event_type: str, payload: dict[str, Any] | None = None
    ) -> None: ...


# ── Value Object ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SuspendedTask:
    """一条被挂起任务的全量快照, 既持久化单元也事件 payload 依据."""

    task_id: str
    module: str
    trace_id: str
    workspace_id: str
    suspended_at: datetime
    ask_request: AskRequest
    original_intent: Intent
    origin_rule_id: str | None = None
    origin_decision_reason: str = ""
    status: SuspendedTaskStatus = "awaiting_user"
    resolved_at: datetime | None = None
    resolution_payload: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for field_name in ("task_id", "module", "trace_id", "workspace_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"SuspendedTask.{field_name} must be non-empty str, got {value!r}"
                )
        if self.status not in VALID_SUSPENDED_STATUSES:
            raise ValueError(
                f"SuspendedTask.status must be one of {sorted(VALID_SUSPENDED_STATUSES)}, "
                f"got {self.status!r}"
            )
        if self.status == "awaiting_user":
            if self.resolved_at is not None or self.resolution_payload is not None:
                raise ValueError(
                    "awaiting_user task must not carry resolved_at/resolution_payload"
                )
        else:
            if self.resolved_at is None:
                raise ValueError(
                    f"terminal task (status={self.status}) must carry resolved_at"
                )
        if self.suspended_at.tzinfo is None:
            raise ValueError("SuspendedTask.suspended_at must be timezone-aware")
        if self.resolved_at is not None and self.resolved_at.tzinfo is None:
            raise ValueError("SuspendedTask.resolved_at must be timezone-aware")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "module": self.module,
            "trace_id": self.trace_id,
            "workspace_id": self.workspace_id,
            "suspended_at": self.suspended_at.isoformat(),
            "ask_request": self.ask_request.to_dict(),
            "original_intent": self.original_intent.to_dict(),
            "origin_rule_id": self.origin_rule_id,
            "origin_decision_reason": self.origin_decision_reason,
            "status": self.status,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolution_payload": (
                dict(self.resolution_payload) if self.resolution_payload else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SuspendedTask":
        if not isinstance(data, Mapping):
            raise TypeError(
                f"SuspendedTask.from_dict requires Mapping, got {type(data).__name__}"
            )
        resolved_raw = data.get("resolved_at")
        resolved_at = (
            datetime.fromisoformat(resolved_raw) if resolved_raw else None
        )
        return cls(
            task_id=data["task_id"],
            module=data["module"],
            trace_id=data["trace_id"],
            workspace_id=data["workspace_id"],
            suspended_at=datetime.fromisoformat(data["suspended_at"]),
            ask_request=AskRequest.from_dict(dict(data["ask_request"])),
            original_intent=Intent.from_dict(data["original_intent"]),
            origin_rule_id=data.get("origin_rule_id"),
            origin_decision_reason=str(data.get("origin_decision_reason") or ""),
            status=data.get("status", "awaiting_user"),  # type: ignore[arg-type]
            resolved_at=resolved_at,
            resolution_payload=(
                dict(data["resolution_payload"])
                if data.get("resolution_payload")
                else None
            ),
        )


# ── Store Protocol ─────────────────────────────────────────


@runtime_checkable
class SuspendedTaskStore(Protocol):
    """Ask/Resume 状态机的抽象接口, 便于未来用分布式 backend 替换."""

    def create(
        self,
        *,
        task_id: str,
        module: str,
        trace_id: str,
        workspace_id: str,
        intent: Intent,
        ask_request: AskRequest,
        origin_rule_id: str | None = None,
        origin_decision_reason: str = "",
    ) -> SuspendedTask: ...

    def get(self, *, workspace_id: str, task_id: str) -> SuspendedTask | None: ...

    def list_awaiting(self, *, workspace_id: str) -> list[SuspendedTask]: ...

    def resolve(
        self,
        *,
        workspace_id: str,
        task_id: str,
        payload: Mapping[str, Any],
    ) -> SuspendedTask: ...

    def timeout(self, *, workspace_id: str, task_id: str) -> SuspendedTask: ...

    def deny(
        self, *, workspace_id: str, task_id: str, reason: str
    ) -> SuspendedTask: ...


# ── Default Implementation ─────────────────────────────────


class WorkspaceSuspendedTaskStore:
    """默认实现: facts 表存快照 + EventBus 发四事件."""

    def __init__(
        self,
        *,
        facts: FactsStore,
        events: EventPublisher,
    ) -> None:
        self._facts = facts
        self._events = events

    # ── Create ────────────────────────────────────────────

    def create(
        self,
        *,
        task_id: str,
        module: str,
        trace_id: str,
        workspace_id: str,
        intent: Intent,
        ask_request: AskRequest,
        origin_rule_id: str | None = None,
        origin_decision_reason: str = "",
    ) -> SuspendedTask:
        # 幂等短路: 同一 (module, trace_id, intent.name) 的 awaiting 任务已存在时
        # 返回既有快照, 不新建、不发事件 —— 避免反复骚扰用户.
        existing = self._find_awaiting_match(
            workspace_id=workspace_id,
            module=module,
            trace_id=trace_id,
            intent_name=intent.name,
        )
        if existing is not None:
            return existing

        task = SuspendedTask(
            task_id=task_id,
            module=module,
            trace_id=trace_id,
            workspace_id=workspace_id,
            suspended_at=_utc_now(),
            ask_request=ask_request,
            original_intent=intent,
            origin_rule_id=origin_rule_id,
            origin_decision_reason=origin_decision_reason,
            status="awaiting_user",
        )
        self._persist(task)
        self._emit(EVENT_TASK_SUSPENDED, task)
        return task

    # ── Read ──────────────────────────────────────────────

    def get(self, *, workspace_id: str, task_id: str) -> SuspendedTask | None:
        raw = self._facts.get_fact(workspace_id, _fact_key(task_id))
        if not raw:
            return None
        return SuspendedTask.from_dict(raw)

    def list_awaiting(self, *, workspace_id: str) -> list[SuspendedTask]:
        return [
            task
            for task in self._iter_all(workspace_id)
            if task.status == "awaiting_user"
        ]

    # ── Transitions ───────────────────────────────────────

    def resolve(
        self,
        *,
        workspace_id: str,
        task_id: str,
        payload: Mapping[str, Any],
    ) -> SuspendedTask:
        task = self._require_active(workspace_id=workspace_id, task_id=task_id)
        resolved = replace(
            task,
            status="resumed",
            resolved_at=_utc_now(),
            resolution_payload=dict(payload),
        )
        self._persist(resolved)
        self._emit(EVENT_TASK_RESUMED, resolved)
        return resolved

    def timeout(self, *, workspace_id: str, task_id: str) -> SuspendedTask:
        task = self._require_active(workspace_id=workspace_id, task_id=task_id)
        timed_out = replace(task, status="timed_out", resolved_at=_utc_now())
        self._persist(timed_out)
        self._emit(EVENT_TASK_ASK_TIMEOUT, timed_out)
        return timed_out

    def deny(
        self, *, workspace_id: str, task_id: str, reason: str
    ) -> SuspendedTask:
        task = self._require_active(workspace_id=workspace_id, task_id=task_id)
        denied = replace(
            task,
            status="denied",
            resolved_at=_utc_now(),
            resolution_payload={"reason": reason} if reason else None,
        )
        self._persist(denied)
        self._emit(EVENT_TASK_DENIED, denied, extra_reason=reason)
        return denied

    # ── Helpers ───────────────────────────────────────────

    def _require_active(
        self, *, workspace_id: str, task_id: str
    ) -> SuspendedTask:
        task = self.get(workspace_id=workspace_id, task_id=task_id)
        if task is None:
            raise TaskNotFoundError(
                f"suspended task not found: workspace_id={workspace_id!r} task_id={task_id!r}"
            )
        if task.is_terminal:
            raise TaskAlreadyTerminalError(
                f"task {task_id!r} is already terminal (status={task.status})"
            )
        return task

    def _iter_all(self, workspace_id: str) -> Iterable[SuspendedTask]:
        rows = self._facts.list_facts_by_prefix(workspace_id, FACT_KEY_PREFIX)
        for row in rows:
            value = getattr(row, "value", row)
            if not isinstance(value, Mapping):
                continue
            try:
                yield SuspendedTask.from_dict(value)
            except Exception:  # noqa: BLE001
                # 存量脏数据不阻塞其他任务读取; fail-loud 的场合在 get/resolve,
                # 这里是 list 扫全量, 只跳过损坏条目并记错.
                _LOGGER.exception(
                    "corrupt suspended-task fact skipped; workspace_id=%s raw=%r",
                    workspace_id,
                    value,
                )
                continue

    def _find_awaiting_match(
        self,
        *,
        workspace_id: str,
        module: str,
        trace_id: str,
        intent_name: str,
    ) -> SuspendedTask | None:
        for task in self._iter_all(workspace_id):
            if task.status != "awaiting_user":
                continue
            if (
                task.module == module
                and task.trace_id == trace_id
                and task.original_intent.name == intent_name
            ):
                return task
        return None

    def _persist(self, task: SuspendedTask) -> None:
        self._facts.set_fact(
            task.workspace_id,
            _fact_key(task.task_id),
            task.to_dict(),
            source=_FACT_SOURCE,
        )

    def _emit(
        self,
        event_type: str,
        task: SuspendedTask,
        *,
        extra_reason: str = "",
    ) -> None:
        payload = {
            "task_id": task.task_id,
            "module": task.module,
            "trace_id": task.trace_id,
            "workspace_id": task.workspace_id,
            "intent_name": task.original_intent.name,
            "rule_id": task.origin_rule_id,
            "reason": extra_reason or task.origin_decision_reason,
            "status": task.status,
        }
        try:
            self._events.publish(event_type, payload)
        except Exception:  # noqa: BLE001
            # 审计通道故障不应阻塞状态机跃迁 (状态已落 facts 表).
            # fail-loud 到日志, 留给运维排障; 调用方拿到的仍是跃迁后的 task.
            _LOGGER.exception(
                "suspended-task event publish failed; event=%s task_id=%s",
                event_type,
                task.task_id,
            )


# ── Module Internals ───────────────────────────────────────


def _fact_key(task_id: str) -> str:
    return f"{FACT_KEY_PREFIX}{task_id}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
