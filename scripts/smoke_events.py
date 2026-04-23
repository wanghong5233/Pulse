"""冒烟: 验证 Observability Plane 三件套 + 核心组件事件发射的端到端活路径."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path


def main() -> int:
    # 1. 基础 import
    from pulse.core.event_types import EventTypes, make_payload, should_persist
    from pulse.core.event_sinks import JsonlEventSink
    from pulse.core.events import EventBus, InMemoryEventStore

    print("[1] import ok")

    # 2. should_persist 过滤器
    assert should_persist(EventTypes.LLM_INVOKE_OK), "llm.invoke.ok should persist"
    assert should_persist(EventTypes.MEMORY_CORE_UPDATED), "memory.core.updated should persist"
    assert not should_persist("channel.message.received"), "channel.* should NOT persist"
    print("[2] should_persist ok")

    # 3. make_payload
    payload = make_payload(
        trace_id="trace-abc",
        actor="unit_test",
        session_id="sess-1",
        extra_field="hello",
    )
    assert payload["actor"] == "unit_test"
    assert payload["trace_id"] == "trace-abc"
    assert payload["extra_field"] == "hello"
    assert payload["event_id"].startswith("evt_")
    print("[3] make_payload ok:", payload["event_id"])

    # 4. EventBus + InMemoryEventStore + JsonlEventSink 端到端
    tmpdir = Path(tempfile.mkdtemp(prefix="pulse_smoke_"))
    try:
        bus = EventBus()
        store = InMemoryEventStore(max_events=100)
        sink = JsonlEventSink(directory=str(tmpdir))
        bus.subscribe_all(store.record)
        bus.subscribe_all(sink.handle)

        bus.publish(EventTypes.LLM_INVOKE_OK, make_payload(
            trace_id="t1", actor="llm_router", kind="chat", model="qwen-plus",
            tool_calls=2, content_chars=123,
        ))
        bus.publish(EventTypes.MEMORY_CORE_UPDATED, make_payload(
            trace_id="t1", actor="core_memory", block="prefs",
            hash_before="aaa", hash_after="bbb",
        ))
        bus.publish("channel.message.received", make_payload(
            trace_id="t1", actor="runtime",
        ))

        # 内存 store 收到全部 3 条
        recent = store.recent(limit=50)
        assert len(recent) == 3, f"expected 3 in-memory events, got {len(recent)}"
        print("[4a] InMemoryEventStore captured 3 events")

        # JSONL sink 只落盘持久化类型(2 条, channel.* 被过滤)
        f = sink.current_file()
        assert f.is_file(), f"sink file not created: {f}"
        lines = f.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2, f"expected 2 persisted lines, got {len(lines)}: {lines}"
        import json
        recs = [json.loads(line) for line in lines]
        types = sorted(r["event_type"] for r in recs)
        assert types == [EventTypes.LLM_INVOKE_OK, EventTypes.MEMORY_CORE_UPDATED], types
        assert all("timestamp" in r for r in recs)
        assert all(r.get("trace_id") == "t1" for r in recs)
        print(f"[4b] JsonlEventSink persisted {len(lines)} events (channel.* filtered)")
        print(f"     file: {f}")
        print(f"     types: {types}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # 5. CoreMemory 真实对象: update_block 触发 memory.core.updated
    from pulse.core.memory.core_memory import CoreMemory
    store_path = Path(tempfile.mkdtemp(prefix="pulse_core_")) / "core.json"
    captured: list[tuple[str, dict]] = []

    def emitter(event_type: str, payload: dict) -> None:
        captured.append((event_type, payload))

    cm = CoreMemory(storage_path=str(store_path), event_emitter=emitter)
    cm.update_block(block="prefs", content={"city": "Beijing"}, merge=True)
    cm.update_block(block="prefs", content={"city": "Beijing"}, merge=True)  # 幂等: 不应再发
    assert len(captured) == 1, f"expected exactly 1 core event, got {len(captured)}"
    ev_type, ev_payload = captured[0]
    assert ev_type == EventTypes.MEMORY_CORE_UPDATED, ev_type
    assert ev_payload["block"] == "prefs"
    assert ev_payload["actor"] == "core_memory"
    assert ev_payload["hash_before"] != ev_payload["hash_after"]
    print(f"[5] CoreMemory.update_block → {ev_type} (actor={ev_payload['actor']}, "
          f"hash {ev_payload['hash_before']}→{ev_payload['hash_after']})")

    # 6. LLMRouter: bind_event_emitter 不崩(真实调用需网络, 此处仅结构验证)
    from pulse.core.llm.router import LLMRouter
    router = LLMRouter()
    router.bind_event_emitter(emitter)
    assert router._event_emitter is emitter
    print("[6] LLMRouter.bind_event_emitter ok")

    # 7. MemoryLayer.meta 枚举值仍可 import(向后兼容), 但有 deprecated 标注
    from pulse.core.memory.envelope import MemoryLayer
    assert MemoryLayer.meta.value == "meta"
    print("[7] MemoryLayer.meta still importable (deprecated)")

    print("\nALL SMOKE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
