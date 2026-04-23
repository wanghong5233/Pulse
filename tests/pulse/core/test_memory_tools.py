from __future__ import annotations

import asyncio

from pulse.core.memory import CoreMemory, RecallMemory, register_memory_tools
from pulse.core.tool import ToolRegistry
from tests.pulse.support.fakes import FakeRecallDB


def test_memory_tools_read_update_search(tmp_path) -> None:
    core = CoreMemory(
        storage_path=str(tmp_path / "core_memory.json"),
        soul_config_path=str(tmp_path / "soul.yaml"),
    )
    recall = RecallMemory(db_engine=FakeRecallDB())
    recall.add_interaction(
        user_text="我喜欢杭州",
        assistant_text="收到，偏好已记录。",
        session_id="u1",
    )

    registry = ToolRegistry()
    register_memory_tools(
        registry,
        core_memory=core,
        recall_memory=recall,
    )

    update_result = asyncio.run(
        registry.invoke(
            "memory_update",
            {"block": "prefs", "content": {"default_location": "hangzhou"}},
        )
    )
    assert update_result["updated"]["default_location"] == "hangzhou"

    read_result = asyncio.run(registry.invoke("memory_read", {"block": "prefs"}))
    assert read_result["value"]["default_location"] == "hangzhou"

    search_result = asyncio.run(
        registry.invoke(
            "memory_search",
            {"query": "杭州", "top_k": 3, "session_id": "u1"},
        )
    )
    assert search_result["total"] >= 1
