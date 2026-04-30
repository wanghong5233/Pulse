from __future__ import annotations

from pulse.core.event_types import should_persist
from pulse.core.events import EventBus, InMemoryEventStore


def test_event_bus_supports_type_and_global_subscribers() -> None:
    bus = EventBus()
    rows: list[tuple[str, str]] = []

    def _type_handler(event_type: str, payload: dict[str, object]) -> None:
        rows.append((event_type, str(payload.get("value") or "")))

    def _global_handler(event_type: str, payload: dict[str, object]) -> None:
        rows.append((f"all:{event_type}", str(payload.get("value") or "")))

    bus.subscribe("demo.event", _type_handler)
    bus.subscribe_all(_global_handler)
    bus.publish("demo.event", {"value": "ok"})

    assert ("demo.event", "ok") in rows
    assert ("all:demo.event", "ok") in rows


def test_in_memory_event_store_recent_stats_and_clear() -> None:
    store = InMemoryEventStore(max_events=200)
    store.record("brain.run.started", {"trace_id": "trace_1", "source": "api"})
    store.record("brain.run.completed", {"trace_id": "trace_1", "source": "api"})
    store.record("mcp.call.completed", {"trace_id": "trace_2", "server": "local"})

    recent = store.recent(limit=10)
    assert len(recent) == 3
    assert recent[0]["event_type"] == "mcp.call.completed"

    trace_rows = store.recent(limit=10, trace_id="trace_1")
    assert len(trace_rows) == 2
    assert all(str(item.get("trace_id") or "") == "trace_1" for item in trace_rows)

    stats = store.stats(window_minutes=60)
    assert int(stats["total"]) == 3
    assert int(stats["in_window"]) == 3
    top_types = {str(item["event_type"]): int(item["count"]) for item in stats["top_event_types"]}
    assert top_types.get("brain.run.started", 0) == 1
    assert top_types.get("brain.run.completed", 0) == 1
    assert top_types.get("mcp.call.completed", 0) == 1

    removed = store.clear()
    assert removed == 3
    assert store.recent(limit=10) == []


def test_should_persist_keeps_audit_decision_events_for_job_greet() -> None:
    """Behavioral pin: agent reflexion / per-JD verdict events MUST be
    persisted to the audit JSONL. Without them post-mortem can't tell
    "really nothing matched" from "rule misfire" — exactly the failure
    mode that triggered ADR-006 reflexion redesign.

    High-volume sibling events (channel.* / module.*.scan / ...) stay
    out of the audit sink by design; this test guards the boundary.
    """
    assert should_persist("module.job_greet.match.candidate") is True
    assert should_persist("module.job_greet.trigger.reflection") is True
    # Sibling stages stay off-disk to keep the audit volume bounded.
    assert should_persist("module.job_greet.scan.started") is False
    assert should_persist("module.job_greet.trigger.started") is False
    # Pre-existing audit prefixes still work.
    assert should_persist("brain.commitment.unfulfilled") is True
    assert should_persist("preference.domain.applied") is True
    assert should_persist("channel.message.received") is False
