"""Smoke tests for CoreMemory vs JobMemory boundary invariants.

Run:
  cd Pulse && python scripts/smoke_memory_boundary.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def test_evolution_pref_split() -> None:
    from pulse.core.soul.evolution import SoulEvolutionEngine

    updates = {
        "preferred_name": "老王",
        "language": "zh-CN",
        "global_reply_style": "concise",
        "career_strategy": "暂时避开大厂",
        "preferred_city": "上海",
    }
    core, non_core = SoulEvolutionEngine._split_pref_updates(updates)
    assert set(core.keys()) == {"preferred_name", "language", "global_reply_style"}
    assert set(non_core.keys()) == {"career_strategy", "preferred_city"}
    print("  [ok] evolution pref split")


def test_brain_core_envelope_guard() -> None:
    from pulse.core.brain import Brain
    from pulse.core.memory.envelope import MemoryEnvelope, MemoryKind, MemoryLayer, MemoryScope
    from pulse.core.task_context import ExecutionMode, TaskContext
    from pulse.core.tool import ToolRegistry

    class DummyCoreMemory:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object, bool]] = []

        def update_block(self, *, block: str, content: object, merge: bool = True) -> dict[str, object]:
            self.calls.append((block, content, merge))
            return {"ok": True}

    brain = Brain(tool_registry=ToolRegistry(), core_memory=DummyCoreMemory())
    core = brain._core_memory  # type: ignore[assignment]
    assert core is not None

    ctx = TaskContext(
        mode=ExecutionMode.interactive_turn,
        trace_id="trace_smoke",
        task_id="task_smoke",
        run_id="run_smoke",
        session_id="session_smoke",
        workspace_id="workspace_smoke",
    )

    # 非 core predicate: 应被拒绝, 不写 CoreMemory
    env_non_core = MemoryEnvelope(
        kind=MemoryKind.preference,
        layer=MemoryLayer.core,
        scope=MemoryScope.global_,
        trace_id=ctx.trace_id,
        run_id=ctx.run_id,
        task_id=ctx.task_id,
        session_id=ctx.session_id,
        workspace_id=ctx.workspace_id,
        content={"predicate": "career_strategy", "object": "暂时避开大厂"},
        source="smoke",
    )
    brain._route_envelope(env_non_core, ctx=ctx)
    assert len(core.calls) == 0, f"non-core predicate should be ignored: {core.calls}"

    # 合法 prefs.<key>: 应写入 CoreMemory.prefs
    env_core = MemoryEnvelope(
        kind=MemoryKind.preference,
        layer=MemoryLayer.core,
        scope=MemoryScope.global_,
        trace_id=ctx.trace_id,
        run_id=ctx.run_id,
        task_id=ctx.task_id,
        session_id=ctx.session_id,
        workspace_id=ctx.workspace_id,
        content={"predicate": "prefs.preferred_name", "object": "老王"},
        source="smoke",
    )
    brain._route_envelope(env_core, ctx=ctx)
    assert len(core.calls) == 1
    block, payload, merge = core.calls[0]
    assert block == "prefs"
    assert payload == {"preferred_name": "老王"}
    assert merge is True
    print("  [ok] brain core envelope guard")


def test_job_memory_core_slice_reader() -> None:
    from pulse.modules.job.memory import JobMemory

    class DummyWorkspace:
        def list_facts_by_prefix(self, workspace_id: str, prefix: str) -> list[SimpleNamespace]:
            _ = workspace_id, prefix
            return []

    class DummyCore:
        def snapshot(self) -> dict[str, object]:
            return {
                "user": {"name": "Alice"},
                "prefs": {"language": "zh-CN"},
            }

    mem = JobMemory(workspace_memory=DummyWorkspace(), workspace_id="job_ws_1", core_memory=DummyCore())  # type: ignore[arg-type]
    snap = mem.snapshot()
    assert snap.user_facts.get("user.name") == "Alice"
    assert snap.user_facts.get("pref.language") == "zh-CN"
    print("  [ok] job memory core slice reader")


if __name__ == "__main__":
    print("[smoke] CoreMemory vs JobMemory boundary")
    test_evolution_pref_split()
    test_brain_core_envelope_guard()
    test_job_memory_core_slice_reader()
    print("[smoke] ALL PASS")
