"""ADR-003 Step B.1b — Brain ActionReport injection tests.

Scope: prove that Brain is a **pass-through** for the ActionReport
contract — when a tool handler emits ``__action_report__``, Brain

1. snapshots it onto the corresponding ``BrainStep.action_report`` so
   downstream ``_build_tool_receipts`` can forward it into
   ``Receipt.action_report``;
2. (default) injects an extra ``SystemMessage`` with the
   "[Action Report — ground-truth facts ...]" block right after the
   ``ToolMessage``, so the LLM's *next* reply is forced to ground on a
   machine-verifiable summary, not on re-reading the raw observation;
3. can degrade gracefully when ``PULSE_ACTION_REPORT_INJECT=off`` — the
   SystemMessage is skipped, but the step-side snapshot is still
   captured for Verifier consumption.

These tests do NOT touch verifier / job.greet. They pin the universal
Brain wiring behavior that other modules (game.checkin / trip.plan /
notification.send) will piggy-back on.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage

from pulse.core.action_report import ACTION_REPORT_KEY
from pulse.core.brain import Brain
from pulse.core.cost import CostController
from pulse.core.task_context import TaskContext
from pulse.core.tool import ToolRegistry, tool


_TOOL_NAME = "job.greet.trigger"


@tool(name=_TOOL_NAME, description="mock job.greet trigger for ADR-003 tests")
def _greet_trigger(args: dict[str, object]) -> dict[str, object]:
    """Mock handler: return a job.greet-shaped observation carrying an
    ActionReport dict — the real service does the same via ``to_dict()``.

    Keeping ``__action_report__`` at top-level mirrors production:
    Brain.extract_action_report() looks for that key first.
    """

    _ = args
    return {
        "ok": True,
        "greeted": 1,
        "failed": 0,
        "matched_details": [
            {"job_title": "AIGC视觉生成实习", "status": "greeted",
             "source_url": "https://example.com/job/1"},
        ],
        ACTION_REPORT_KEY: {
            "action": "job.greet",
            "status": "succeeded",
            "summary": "已投递 1 个岗位",
            "details": [
                {"target": "AIGC视觉生成实习", "status": "succeeded",
                 "url": "https://example.com/job/1"},
            ],
            "metrics": {"attempted": 1, "succeeded": 1, "failed": 0},
        },
    }


class _RecordingLLM:
    """Minimal planner LLM that records every ``messages`` list it sees.

    Turn 0 → fire ``job.greet.trigger`` once.
    Turn 1 → respond with a plain text (ending the ReAct loop).

    The test then replays ``self.message_logs`` to assert whether a
    SystemMessage carrying "[Action Report" prefix arrived before turn 1.
    """

    def __init__(self) -> None:
        self.message_logs: list[list[Any]] = []

    def invoke_chat(self, messages, *, tools=None, route="default", tool_choice=None):  # noqa: ANN001, ANN201
        _ = tools, route, tool_choice
        self.message_logs.append(list(messages))
        tool_calls_seen = sum(1 for m in messages if getattr(m, "tool_call_id", None))
        if tool_calls_seen == 0:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": _TOOL_NAME.replace(".", "_"),
                    "args": {"keyword": "大模型应用开发", "batch_size": 1, "confirm_execute": True},
                    "id": "call_greet",
                }],
            )
        return AIMessage(content="已完成投递 1 个合适岗位。")


def _make_brain(llm: _RecordingLLM) -> Brain:
    reg = ToolRegistry()
    reg.register_callable(_greet_trigger)
    return Brain(
        tool_registry=reg,
        llm_router=llm,
        cost_controller=CostController(daily_budget_usd=5.0),
        max_steps=4,
    )


def _system_texts_after_tool(messages: list[Any]) -> list[str]:
    """Return contents of SystemMessages that appeared *after* the
    (first) ToolMessage in the turn — this is where the ActionReport
    injection shows up (Brain appends it right after the ToolMessage).
    """

    out: list[str] = []
    seen_tool = False
    for msg in messages:
        if getattr(msg, "tool_call_id", None):
            seen_tool = True
            continue
        if seen_tool and isinstance(msg, SystemMessage):
            out.append(str(getattr(msg, "content", "")))
    return out


# ──────────────────────────────────────────────────────────────
# 1. SystemMessage injection (default: PULSE_ACTION_REPORT_INJECT unset → on)
# ──────────────────────────────────────────────────────────────


def test_brain_injects_action_report_system_message_by_default(monkeypatch) -> None:
    monkeypatch.delenv("PULSE_ACTION_REPORT_INJECT", raising=False)
    llm = _RecordingLLM()
    brain = _make_brain(llm)

    result = asyncio.run(brain.run(query="投一个大模型岗位", ctx=TaskContext(), prefer_llm=True))

    assert result.stopped_reason == "completed"
    assert _TOOL_NAME in result.used_tools

    # The LLM was called twice; turn 1 must have received the Action Report.
    assert len(llm.message_logs) >= 2
    sys_texts = _system_texts_after_tool(llm.message_logs[1])
    assert any("[Action Report" in t for t in sys_texts), (
        "Brain must inject a SystemMessage carrying the Action Report "
        f"block after the ToolMessage; got system texts: {sys_texts!r}"
    )
    injected = next(t for t in sys_texts if "[Action Report" in t)
    assert "action: job.greet" in injected
    assert "status: succeeded" in injected
    assert "已投递 1 个岗位" in injected


def test_brain_step_snapshots_action_report_dict() -> None:
    llm = _RecordingLLM()
    brain = _make_brain(llm)

    result = asyncio.run(brain.run(query="投一个大模型岗位", ctx=TaskContext(), prefer_llm=True))

    tool_steps = [s for s in result.steps if s.action == "use_tool" and s.tool_name == _TOOL_NAME]
    assert tool_steps, "expected at least one use_tool step for job.greet.trigger"
    ar = tool_steps[0].action_report
    assert ar is not None, "BrainStep.action_report must be populated from observation"
    assert ar["action"] == "job.greet"
    assert ar["status"] == "succeeded"
    assert ar["metrics"]["succeeded"] == 1


# ──────────────────────────────────────────────────────────────
# 2. Degrade switch: PULSE_ACTION_REPORT_INJECT=off
# ──────────────────────────────────────────────────────────────


def test_brain_injection_can_be_disabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("PULSE_ACTION_REPORT_INJECT", "off")
    llm = _RecordingLLM()
    brain = _make_brain(llm)

    result = asyncio.run(brain.run(query="投一个大模型岗位", ctx=TaskContext(), prefer_llm=True))

    assert result.stopped_reason == "completed"
    # No "[Action Report" SystemMessage in the message stream.
    for log in llm.message_logs:
        for msg in log:
            if isinstance(msg, SystemMessage):
                assert "[Action Report" not in str(getattr(msg, "content", "")), (
                    "PULSE_ACTION_REPORT_INJECT=off must not render the "
                    "Action Report SystemMessage into the prompt"
                )
    # …but step snapshot is still captured (Verifier side relies on it).
    tool_steps = [s for s in result.steps if s.action == "use_tool" and s.tool_name == _TOOL_NAME]
    assert tool_steps
    assert tool_steps[0].action_report is not None


# ──────────────────────────────────────────────────────────────
# 3. Back-compat: tool without ActionReport → no injection, no snapshot
# ──────────────────────────────────────────────────────────────


@tool(name="weather.current", description="mock weather tool, no ActionReport")
def _weather(args: dict[str, object]) -> dict[str, object]:
    return {"location": args.get("location"), "temperature_c": 22}


class _WeatherLLM:
    def __init__(self) -> None:
        self.message_logs: list[list[Any]] = []

    def invoke_chat(self, messages, *, tools=None, route="default", tool_choice=None):  # noqa: ANN001, ANN201
        _ = tools, route, tool_choice
        self.message_logs.append(list(messages))
        if not any(getattr(m, "tool_call_id", None) for m in messages):
            return AIMessage(
                content="",
                tool_calls=[{"name": "weather_current",
                             "args": {"location": "Beijing"}, "id": "call_w"}],
            )
        return AIMessage(content="北京当前 22°C。")


def test_brain_tolerates_tools_without_action_report() -> None:
    reg = ToolRegistry()
    reg.register_callable(_weather)
    llm = _WeatherLLM()
    brain = Brain(
        tool_registry=reg,
        llm_router=llm,
        cost_controller=CostController(daily_budget_usd=5.0),
        max_steps=3,
    )

    result = asyncio.run(brain.run(query="帮我查天气", ctx=TaskContext(), prefer_llm=True))
    assert result.stopped_reason == "completed"

    # No Action Report SystemMessage anywhere.
    for log in llm.message_logs:
        for msg in log:
            if isinstance(msg, SystemMessage):
                assert "[Action Report" not in str(getattr(msg, "content", ""))

    tool_steps = [s for s in result.steps if s.action == "use_tool"]
    assert tool_steps
    assert all(s.action_report is None for s in tool_steps)
