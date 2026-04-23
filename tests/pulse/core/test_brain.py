from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage

from pulse.core.brain import Brain
from pulse.core.cost import CostController
from pulse.core.learning import PreferenceExtractor
from pulse.core.memory import CoreMemory, RecallMemory
from pulse.core.memory.archival_memory import ArchivalMemory
from pulse.core.soul import SoulEvolutionEngine, SoulGovernance
from pulse.core.task_context import TaskContext
from pulse.core.tool import ToolRegistry, tool
from tests.pulse.support.fakes import FakeArchivalDB, FakeRecallDB


@tool(name="weather.current", description="mock weather tool")
def _weather(args: dict[str, object]) -> dict[str, object]:
    return {"location": args.get("location"), "temperature_c": 22}


@tool(name="flight.search", description="mock flight tool")
def _flight(args: dict[str, object]) -> dict[str, object]:
    return {"query": args.get("query"), "items": [{"flight_no": "PL101"}]}


@tool(name="alarm.create", description="mock alarm tool")
def _alarm(args: dict[str, object]) -> dict[str, object]:
    return {"ok": True, "minutes": args.get("minutes"), "message": args.get("message")}


class _FakePlannerLLMRouter:
    def invoke_chat(self, messages, *, tools=None, route="default", tool_choice=None):  # noqa: ANN001, ANN201
        _ = tools, route, tool_choice
        full_text = "\n".join(str(getattr(message, "content", "")) for message in messages)
        tool_calls_seen = sum(1 for message in messages if getattr(message, "tool_call_id", None))
        query = str(getattr(messages[-1], "content", "")) if messages else ""
        if tool_calls_seen <= 0:
            if "天气再查航班并设提醒" in query:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "weather_current", "args": {"location": "北京"}, "id": "call_weather"},
                        {"name": "flight_search", "args": {"query": "北京 上海"}, "id": "call_flight"},
                        {"name": "alarm_create", "args": {"minutes": 30, "message": "关注航班"}, "id": "call_alarm"},
                    ],
                )
            location = "北京"
            if "default_location" in full_text and "杭州" in full_text:
                location = "杭州"
            if "default_location" in full_text and "上海" in full_text:
                location = "上海"
            return AIMessage(
                content="",
                tool_calls=[{"name": "weather_current", "args": {"location": location}, "id": "call_weather"}],
            )
        if "杭州" in full_text:
            return AIMessage(content="杭州天气已查询完成。")
        if "上海" in full_text:
            return AIMessage(content="上海天气已查询完成。")
        if "temperature_c" in full_text and "flight_no" in full_text:
            return AIMessage(content="天气、航班和提醒都已处理完成。")
        return AIMessage(content="任务已完成。")


def test_brain_runs_explicit_tool_command() -> None:
    registry = ToolRegistry()
    registry.register_callable(_weather)
    brain = Brain(tool_registry=registry, llm_router=None, cost_controller=CostController(daily_budget_usd=5.0))

    result = asyncio.run(brain.run(query="/tool weather.current Beijing", ctx=TaskContext(), prefer_llm=False))
    assert result.stopped_reason == "completed"
    assert "weather.current" in result.used_tools
    assert "temperature_c" in result.answer


def test_brain_runs_multi_tool_plan() -> None:
    registry = ToolRegistry()
    registry.register_callable(_weather)
    registry.register_callable(_flight)
    registry.register_callable(_alarm)
    brain = Brain(
        tool_registry=registry,
        llm_router=_FakePlannerLLMRouter(),
        cost_controller=CostController(daily_budget_usd=5.0),
        max_steps=6,
    )

    result = asyncio.run(brain.run(query="请先查天气再查航班并设提醒", ctx=TaskContext(), prefer_llm=True))
    assert result.stopped_reason == "completed"
    assert "weather.current" in result.used_tools
    assert "flight.search" in result.used_tools
    assert "alarm.create" in result.used_tools
    assert len(result.used_tools) >= 3


def test_brain_returns_fallback_without_tool_hint() -> None:
    registry = ToolRegistry()
    brain = Brain(tool_registry=registry, llm_router=None)
    result = asyncio.run(brain.run(query="hello", ctx=TaskContext(), prefer_llm=False))
    assert result.stopped_reason == "no_llm"
    assert "tool" in result.answer.lower()


def test_brain_uses_memory_preference_and_writes_recall(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register_callable(_weather)
    core_memory = CoreMemory(
        storage_path=str(tmp_path / "core_memory.json"),
        soul_config_path=str(tmp_path / "soul.yaml"),
    )
    recall_memory = RecallMemory(db_engine=FakeRecallDB())
    brain = Brain(
        tool_registry=registry,
        llm_router=_FakePlannerLLMRouter(),
        core_memory=core_memory,
        recall_memory=recall_memory,
        max_steps=3,
    )

    core_memory.update_preferences({"default_location": "杭州"})
    assert core_memory.preference("default_location") == "杭州"

    result = asyncio.run(brain.run(query="帮我查天气", ctx=TaskContext(), prefer_llm=True))
    assert result.stopped_reason == "completed"
    assert "杭州" in result.answer or "hangzhou" in result.answer.lower()

    recent = recall_memory.recent(limit=10)
    assert len(recent) >= 2
    hits = recall_memory.search_keyword(keywords=["查天气"], top_k=3)
    assert len(hits) >= 1


def test_brain_reflection_updates_preference_for_followup(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register_callable(_weather)
    core_memory = CoreMemory(
        storage_path=str(tmp_path / "core_memory.json"),
        soul_config_path=str(tmp_path / "soul.yaml"),
    )
    recall_memory = RecallMemory(db_engine=FakeRecallDB())
    governance = SoulGovernance(core_memory=core_memory, audit_path=str(tmp_path / "audit.json"))
    archival_memory = ArchivalMemory(db_engine=FakeArchivalDB())
    evolution = SoulEvolutionEngine(
        governance=governance,
        archival_memory=archival_memory,
        preference_extractor=PreferenceExtractor(),
    )
    brain = Brain(
        tool_registry=registry,
        llm_router=_FakePlannerLLMRouter(),
        core_memory=core_memory,
        recall_memory=recall_memory,
        evolution_engine=evolution,
        max_steps=3,
    )

    evolution.reflect_interaction(
        user_text="以后默认用上海",
        assistant_text="收到。",
        metadata={"session_id": "u1"},
    )
    assert core_memory.preference("default_location") == "上海"

    result = asyncio.run(brain.run(query="帮我查天气", ctx=TaskContext(), prefer_llm=True))
    assert result.stopped_reason == "completed"
    assert "上海" in result.answer or "shanghai" in result.answer.lower()
