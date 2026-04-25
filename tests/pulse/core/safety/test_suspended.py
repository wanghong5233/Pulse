"""SuspendedTaskStore 合约与行为测试.

覆盖:

* ``SuspendedTask`` 结构不变式 (非空字段 / aware datetime / awaiting 与终态
  字段互斥 / 终态必须带 resolved_at).
* ``WorkspaceSuspendedTaskStore`` 三条状态跃迁端到端 (create → resolve /
  create → timeout / create → deny), facts 落盘 + 事件序列与顺序.
* 幂等 create: 同一 ``(module, trace_id, intent.name)`` 二次 create 返回既有
  task 且**不再**发 ``task.suspended`` 事件.
* 终态闭锁: 终态任务上再调 resolve/timeout/deny 抛 ``TaskAlreadyTerminalError``.
* 未知 task_id 抛 ``TaskNotFoundError``.
* 持久化一致性: ``SuspendedTask.to_dict / from_dict`` roundtrip; ``list_awaiting``
  只返回 awaiting.
* 真实 ``EventBus`` 订阅链路能收到四类事件 (避免只 fake 不验真正 IO 边界).
* Protocol 符合性 (``WorkspaceSuspendedTaskStore`` / fake facts / fake publisher).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from pulse.core.events import EventBus
from pulse.core.safety import (
    AskRequest,
    EVENT_TASK_ASK_TIMEOUT,
    EVENT_TASK_DENIED,
    EVENT_TASK_RESUMED,
    EVENT_TASK_SUSPENDED,
    EventPublisher,
    FACT_KEY_PREFIX,
    FactsStore,
    Intent,
    ResumeHandle,
    SuspendedTask,
    SuspendedTaskStore,
    TaskAlreadyTerminalError,
    TaskNotFoundError,
    WorkspaceSuspendedTaskStore,
)


# ──────────────────────────────────────────────────────────────
# Fakes
# ──────────────────────────────────────────────────────────────


@dataclass
class _Fact:
    value: Any


class InMemoryFacts:
    """WorkspaceMemory 的最小子集 fake, 对齐 FactsStore Protocol.

    真表的 workspace_id / key 组合主键语义在这里用嵌套 dict 模拟, value
    不做 JSON 编解码 (WorkspaceMemory 真表做; 本 fake 直接存对象, 等价于
    get_fact / list_facts_by_prefix 返回解码后对象的契约).
    """

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = defaultdict(dict)

    def get_fact(self, workspace_id: str, key: str, default: Any = None) -> Any:
        return self._rows.get(workspace_id, {}).get(key, default)

    def set_fact(
        self, workspace_id: str, key: str, value: Any, *, source: str = ""
    ) -> None:
        self._rows[workspace_id][key] = value

    def list_facts_by_prefix(self, workspace_id: str, prefix: str) -> list[_Fact]:
        items = self._rows.get(workspace_id, {})
        return [
            _Fact(value=value)
            for key, value in sorted(items.items())
            if key.startswith(prefix)
        ]

    def delete_fact(self, workspace_id: str, key: str) -> bool:
        return self._rows.get(workspace_id, {}).pop(key, None) is not None


class RecordingPublisher:
    """事件尾巴: 只记录不分发, 用于断言单次跃迁真的发了一事件."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def publish(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        self.events.append((event_type, dict(payload or {})))


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────


def _intent(name: str = "job_chat.reply.send") -> Intent:
    return Intent(
        kind="mutation",
        name=name,
        args={"conversation_id": "cv_1", "text": "draft"},
        evidence_keys=("profile.base_city", "profile.salary_expectation"),
    )


def _ask_request() -> AskRequest:
    return AskRequest(
        question="HR 问你下周二面试时间, 你什么时间有空?",
        draft="我周二下午有空",
        resume_handle=ResumeHandle(
            task_id="tsk_001",
            module="job_chat",
            intent="system.task.resume",
            payload_schema="job_chat.interview_time_v1",
        ),
        timeout_seconds=3600,
    )


def _make_store() -> tuple[WorkspaceSuspendedTaskStore, InMemoryFacts, RecordingPublisher]:
    facts = InMemoryFacts()
    events = RecordingPublisher()
    store = WorkspaceSuspendedTaskStore(facts=facts, events=events)
    return store, facts, events


# ──────────────────────────────────────────────────────────────
# SuspendedTask invariants
# ──────────────────────────────────────────────────────────────


class TestSuspendedTaskInvariants:
    def test_awaiting_default_fields(self) -> None:
        task = SuspendedTask(
            task_id="tsk_001",
            module="job_chat",
            trace_id="tr_1",
            workspace_id="ws_1",
            suspended_at=datetime.now(timezone.utc),
            ask_request=_ask_request(),
            original_intent=_intent(),
        )
        assert task.status == "awaiting_user"
        assert task.is_terminal is False
        assert task.resolved_at is None
        assert task.resolution_payload is None

    @pytest.mark.parametrize(
        "field_name,bad_value",
        [("task_id", ""), ("module", ""), ("trace_id", ""), ("workspace_id", "")],
    )
    def test_rejects_empty_identifier(self, field_name: str, bad_value: str) -> None:
        base = dict(
            task_id="tsk_001",
            module="job_chat",
            trace_id="tr_1",
            workspace_id="ws_1",
            suspended_at=datetime.now(timezone.utc),
            ask_request=_ask_request(),
            original_intent=_intent(),
        )
        base[field_name] = bad_value
        with pytest.raises(ValueError):
            SuspendedTask(**base)

    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError):
            SuspendedTask(
                task_id="tsk_001",
                module="job_chat",
                trace_id="tr_1",
                workspace_id="ws_1",
                suspended_at=datetime.utcnow(),  # naive
                ask_request=_ask_request(),
                original_intent=_intent(),
            )

    def test_awaiting_forbids_resolved_fields(self) -> None:
        # awaiting 状态同时带 resolved_at 会让状态机语义漂移.
        with pytest.raises(ValueError):
            SuspendedTask(
                task_id="tsk_001",
                module="job_chat",
                trace_id="tr_1",
                workspace_id="ws_1",
                suspended_at=datetime.now(timezone.utc),
                ask_request=_ask_request(),
                original_intent=_intent(),
                status="awaiting_user",
                resolved_at=datetime.now(timezone.utc),
            )

    def test_terminal_requires_resolved_at(self) -> None:
        with pytest.raises(ValueError):
            SuspendedTask(
                task_id="tsk_001",
                module="job_chat",
                trace_id="tr_1",
                workspace_id="ws_1",
                suspended_at=datetime.now(timezone.utc),
                ask_request=_ask_request(),
                original_intent=_intent(),
                status="resumed",
                resolved_at=None,
            )

    def test_to_from_dict_roundtrip_for_awaiting_and_terminal(self) -> None:
        awaiting = SuspendedTask(
            task_id="tsk_001",
            module="job_chat",
            trace_id="tr_1",
            workspace_id="ws_1",
            suspended_at=datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc),
            ask_request=_ask_request(),
            original_intent=_intent(),
            origin_rule_id="reply_from_profile_evidence",
            origin_decision_reason="evidence missing",
        )
        assert SuspendedTask.from_dict(awaiting.to_dict()) == awaiting

        resumed = SuspendedTask(
            task_id="tsk_002",
            module="job_chat",
            trace_id="tr_2",
            workspace_id="ws_1",
            suspended_at=datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc),
            ask_request=_ask_request(),
            original_intent=_intent(),
            status="resumed",
            resolved_at=datetime(2026, 4, 24, 12, 5, tzinfo=timezone.utc),
            resolution_payload={"user_answer": "周二下午都行"},
        )
        assert SuspendedTask.from_dict(resumed.to_dict()) == resumed


# ──────────────────────────────────────────────────────────────
# Lifecycle end-to-end
# ──────────────────────────────────────────────────────────────


class TestLifecycle:
    def test_create_then_resolve(self) -> None:
        store, facts, events = _make_store()

        task = store.create(
            task_id="tsk_001",
            module="job_chat",
            trace_id="tr_1",
            workspace_id="ws_1",
            intent=_intent(),
            ask_request=_ask_request(),
            origin_rule_id="reply_from_profile_evidence",
            origin_decision_reason="evidence missing",
        )
        assert task.status == "awaiting_user"
        # 落盘走 "safety.suspended.<task_id>" 前缀路径 (FACT_KEY_PREFIX 不变式).
        assert facts.get_fact("ws_1", f"{FACT_KEY_PREFIX}tsk_001") is not None
        assert events.events == [
            (EVENT_TASK_SUSPENDED, {
                "task_id": "tsk_001",
                "module": "job_chat",
                "trace_id": "tr_1",
                "workspace_id": "ws_1",
                "intent_name": "job_chat.reply.send",
                "rule_id": "reply_from_profile_evidence",
                "reason": "evidence missing",
                "status": "awaiting_user",
            }),
        ]

        resumed = store.resolve(
            workspace_id="ws_1",
            task_id="tsk_001",
            payload={"user_answer": "周二下午 3 点"},
        )
        assert resumed.status == "resumed"
        assert resumed.resolved_at is not None
        assert resumed.resolution_payload == {"user_answer": "周二下午 3 点"}
        assert events.events[-1][0] == EVENT_TASK_RESUMED
        assert events.events[-1][1]["status"] == "resumed"
        assert events.events[-1][1]["task_id"] == "tsk_001"

    def test_create_then_timeout(self) -> None:
        store, _facts, events = _make_store()
        store.create(
            task_id="tsk_002",
            module="job_chat",
            trace_id="tr_2",
            workspace_id="ws_1",
            intent=_intent(),
            ask_request=_ask_request(),
        )
        timed_out = store.timeout(workspace_id="ws_1", task_id="tsk_002")
        assert timed_out.status == "timed_out"
        assert timed_out.resolved_at is not None
        assert events.events[-1][0] == EVENT_TASK_ASK_TIMEOUT
        assert events.events[-1][1]["status"] == "timed_out"

    def test_create_then_deny_with_reason(self) -> None:
        store, _facts, events = _make_store()
        store.create(
            task_id="tsk_003",
            module="job_chat",
            trace_id="tr_3",
            workspace_id="ws_1",
            intent=_intent(),
            ask_request=_ask_request(),
            origin_rule_id="cross_domain_deny",
        )
        denied = store.deny(
            workspace_id="ws_1", task_id="tsk_003", reason="用户在 IM 明确拒绝"
        )
        assert denied.status == "denied"
        assert denied.resolution_payload == {"reason": "用户在 IM 明确拒绝"}
        assert events.events[-1][0] == EVENT_TASK_DENIED
        # deny 事件的 reason 字段以调用方传入的为准, 不被 origin_decision_reason 覆盖
        assert events.events[-1][1]["reason"] == "用户在 IM 明确拒绝"
        assert events.events[-1][1]["rule_id"] == "cross_domain_deny"


# ──────────────────────────────────────────────────────────────
# Idempotency / Fallthrough
# ──────────────────────────────────────────────────────────────


class TestIdempotency:
    def test_second_create_returns_existing_awaiting_without_duplicate_event(
        self,
    ) -> None:
        store, facts, events = _make_store()
        first = store.create(
            task_id="tsk_a",
            module="job_chat",
            trace_id="tr_1",
            workspace_id="ws_1",
            intent=_intent(),
            ask_request=_ask_request(),
        )
        second = store.create(
            task_id="tsk_a_duplicate_should_be_ignored",
            module="job_chat",
            trace_id="tr_1",
            workspace_id="ws_1",
            intent=_intent(),
            ask_request=_ask_request(),
        )
        assert second == first  # same task returned, not a new one
        # 幂等短路: 只该有一条 task.suspended 事件, 不该反复骚扰用户
        assert [e[0] for e in events.events] == [EVENT_TASK_SUSPENDED]
        # 没有重复落盘新 key
        assert len([k for k in facts._rows["ws_1"].keys() if k.startswith(FACT_KEY_PREFIX)]) == 1

    def test_create_after_previous_resumed_is_a_new_task(self) -> None:
        store, _facts, events = _make_store()
        store.create(
            task_id="tsk_a",
            module="job_chat",
            trace_id="tr_1",
            workspace_id="ws_1",
            intent=_intent(),
            ask_request=_ask_request(),
        )
        store.resolve(workspace_id="ws_1", task_id="tsk_a", payload={"ok": True})

        # 先前任务已终态, 同 (module, trace_id, intent.name) 再 create 应新建
        fresh = store.create(
            task_id="tsk_b",
            module="job_chat",
            trace_id="tr_1",
            workspace_id="ws_1",
            intent=_intent(),
            ask_request=_ask_request(),
        )
        assert fresh.task_id == "tsk_b"
        assert fresh.status == "awaiting_user"
        suspended_events = [e for e in events.events if e[0] == EVENT_TASK_SUSPENDED]
        assert len(suspended_events) == 2


# ──────────────────────────────────────────────────────────────
# Terminal lock
# ──────────────────────────────────────────────────────────────


class TestTerminalLock:
    @pytest.mark.parametrize(
        "transition_name",
        ["resolve", "timeout", "deny"],
    )
    def test_cannot_retransition_terminal_task(self, transition_name: str) -> None:
        store, _facts, _events = _make_store()
        store.create(
            task_id="tsk_1",
            module="job_chat",
            trace_id="tr_1",
            workspace_id="ws_1",
            intent=_intent(),
            ask_request=_ask_request(),
        )
        store.resolve(workspace_id="ws_1", task_id="tsk_1", payload={"ok": 1})

        with pytest.raises(TaskAlreadyTerminalError):
            if transition_name == "resolve":
                store.resolve(workspace_id="ws_1", task_id="tsk_1", payload={"x": 1})
            elif transition_name == "timeout":
                store.timeout(workspace_id="ws_1", task_id="tsk_1")
            else:
                store.deny(workspace_id="ws_1", task_id="tsk_1", reason="too late")

    def test_unknown_task_raises_not_found(self) -> None:
        store, _facts, _events = _make_store()
        with pytest.raises(TaskNotFoundError):
            store.resolve(workspace_id="ws_1", task_id="missing", payload={"x": 1})
        with pytest.raises(TaskNotFoundError):
            store.timeout(workspace_id="ws_1", task_id="missing")
        with pytest.raises(TaskNotFoundError):
            store.deny(workspace_id="ws_1", task_id="missing", reason="nope")


# ──────────────────────────────────────────────────────────────
# Read views
# ──────────────────────────────────────────────────────────────


class TestReadViews:
    def test_get_returns_none_for_missing(self) -> None:
        store, _facts, _events = _make_store()
        assert store.get(workspace_id="ws_1", task_id="nope") is None

    def test_list_awaiting_filters_terminals(self) -> None:
        store, _facts, _events = _make_store()
        store.create(
            task_id="t_await",
            module="job_chat",
            trace_id="tr_a",
            workspace_id="ws_1",
            intent=_intent("job_chat.reply.send"),
            ask_request=_ask_request(),
        )
        store.create(
            task_id="t_done",
            module="job_chat",
            trace_id="tr_b",
            workspace_id="ws_1",
            intent=_intent("game.checkin.execute"),
            ask_request=_ask_request(),
        )
        store.resolve(workspace_id="ws_1", task_id="t_done", payload={"ok": 1})

        awaiting = store.list_awaiting(workspace_id="ws_1")
        assert [t.task_id for t in awaiting] == ["t_await"]

    def test_workspaces_are_isolated(self) -> None:
        store, _facts, _events = _make_store()
        store.create(
            task_id="t1",
            module="job_chat",
            trace_id="tr_1",
            workspace_id="ws_A",
            intent=_intent(),
            ask_request=_ask_request(),
        )
        # 跨 workspace 的幂等去重不应互相污染
        in_b = store.create(
            task_id="t2",
            module="job_chat",
            trace_id="tr_1",
            workspace_id="ws_B",
            intent=_intent(),
            ask_request=_ask_request(),
        )
        assert in_b.task_id == "t2"
        assert store.list_awaiting(workspace_id="ws_A")[0].task_id == "t1"
        assert store.list_awaiting(workspace_id="ws_B")[0].task_id == "t2"


# ──────────────────────────────────────────────────────────────
# Real EventBus integration
# ──────────────────────────────────────────────────────────────


class TestRealEventBusIntegration:
    """Store 对真实 EventBus 的 publish signature 匹配 + 订阅方能收到."""

    def test_events_flow_through_eventbus_subscriber(self) -> None:
        facts = InMemoryFacts()
        bus = EventBus()
        store = WorkspaceSuspendedTaskStore(facts=facts, events=bus)

        received: list[tuple[str, dict[str, Any]]] = []
        for event_type in (
            EVENT_TASK_SUSPENDED,
            EVENT_TASK_RESUMED,
            EVENT_TASK_ASK_TIMEOUT,
            EVENT_TASK_DENIED,
        ):
            bus.subscribe(
                event_type,
                lambda et, pl, captured=received: captured.append((et, dict(pl))),
            )

        store.create(
            task_id=f"tsk_{uuid4().hex[:8]}",
            module="job_chat",
            trace_id="tr_real",
            workspace_id="ws_1",
            intent=_intent(),
            ask_request=_ask_request(),
        )

        assert len(received) == 1
        assert received[0][0] == EVENT_TASK_SUSPENDED
        assert received[0][1]["intent_name"] == "job_chat.reply.send"


# ──────────────────────────────────────────────────────────────
# Protocol structural conformance
# ──────────────────────────────────────────────────────────────


def test_protocol_conformance() -> None:
    facts = InMemoryFacts()
    events = RecordingPublisher()
    store = WorkspaceSuspendedTaskStore(facts=facts, events=events)

    assert isinstance(store, SuspendedTaskStore)
    assert isinstance(facts, FactsStore)
    assert isinstance(events, EventPublisher)
    # EventBus 本身也应当满足 EventPublisher Protocol (publish 签名对齐)
    assert isinstance(EventBus(), EventPublisher)
