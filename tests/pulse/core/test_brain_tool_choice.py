"""Contract B (Call Contract) · Phase 1b guard tests.

Brain's **structural-signal** policy for `tool_choice` — the L5 layer in
ADR-001 §3.2. It answers: *when should Brain force the LLM to emit a
tool_call on this step?*

Invariants (not heuristics — pure structural):

1. First step in a **non-interactive** run (detachedScheduledTask /
   heartbeatTurn / subagentTask / resumedTask) → ``"required"``. These
   modes exist precisely to *do work*, so "say without doing" is never
   acceptable on step 0.
2. First step in an **interactive_turn** → ``"auto"``. The user might
   just be chatting; forcing a tool call would feel hostile.
3. **Escalation**: if the *previous* AI message in this run was pure
   text AND no tool has been used yet, the *next* step upgrades to
   ``"required"`` — once. This guards against the "I will check the
   weather" hallucination pattern without ever inspecting the message
   content (no keyword heuristics).
4. De-escalation: as soon as any tool has actually been invoked
   (used_tools non-empty), tool_choice returns to ``"auto"``. Contract B
   is a nudge, not a cage.

Tests are **pure-function** on ``Brain._decide_tool_choice`` plus one
integration smoke on ``_react_loop`` to prove the nudge reaches
``invoke_chat``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import AIMessage

from pulse.core.brain import Brain
from pulse.core.cost import CostController
from pulse.core.task_context import ExecutionMode, TaskContext
from pulse.core.tool import ToolRegistry, tool


@tool(name="noop.ping", description="mock tool for contract B tests")
def _noop_ping(args: dict[str, object]) -> dict[str, object]:
    return {"echo": args}


# ──────────────────────────────────────────────────────────────
# Pure-function tests of the decision itself
# ──────────────────────────────────────────────────────────────


def test_decide_tool_choice_interactive_first_step_is_auto() -> None:
    choice = Brain._decide_tool_choice(
        mode=ExecutionMode.interactive_turn,
        step_idx=0,
        prev_ai_was_text_only=False,
        used_tools_count=0,
    )
    assert choice == "auto", (
        "interactive first step must stay 'auto' — user might just be "
        "chatting; forcing tool_call would be hostile"
    )


def test_decide_tool_choice_detached_first_step_is_required() -> None:
    choice = Brain._decide_tool_choice(
        mode=ExecutionMode.detached_scheduled_task,
        step_idx=0,
        prev_ai_was_text_only=False,
        used_tools_count=0,
    )
    assert choice == "required", (
        "detached scheduled tasks exist to DO work on step 0; 'say without "
        "doing' on step 0 is never acceptable for this mode"
    )


def test_decide_tool_choice_heartbeat_first_step_is_required() -> None:
    choice = Brain._decide_tool_choice(
        mode=ExecutionMode.heartbeat_turn,
        step_idx=0,
        prev_ai_was_text_only=False,
        used_tools_count=0,
    )
    assert choice == "required"


def test_decide_tool_choice_subagent_first_step_is_required() -> None:
    choice = Brain._decide_tool_choice(
        mode=ExecutionMode.subagent_task,
        step_idx=0,
        prev_ai_was_text_only=False,
        used_tools_count=0,
    )
    assert choice == "required"


def test_decide_tool_choice_escalates_after_empty_text_step() -> None:
    """Escalation is purely structural — we check (prev_ai_was_text_only AND
    used_tools_count==0), never the message content."""
    choice = Brain._decide_tool_choice(
        mode=ExecutionMode.interactive_turn,
        step_idx=1,
        prev_ai_was_text_only=True,
        used_tools_count=0,
    )
    assert choice == "required", (
        "'I will check the weather' style hallucination on step 0 must "
        "escalate step 1 to 'required' without inspecting text content"
    )


def test_decide_tool_choice_deescalates_after_tool_used() -> None:
    """Once any tool has run, the nudge goes away — Contract B is not a cage."""
    choice = Brain._decide_tool_choice(
        mode=ExecutionMode.interactive_turn,
        step_idx=2,
        prev_ai_was_text_only=True,
        used_tools_count=1,
    )
    assert choice == "auto"


def test_decide_tool_choice_noninteractive_deescalates_after_tool_used() -> None:
    """Even detached tasks drop the force once action has occurred — we
    don't want to lock the LLM out of writing a final summary."""
    choice = Brain._decide_tool_choice(
        mode=ExecutionMode.detached_scheduled_task,
        step_idx=3,
        prev_ai_was_text_only=False,
        used_tools_count=2,
    )
    assert choice == "auto"


def test_decide_tool_choice_interactive_second_step_without_empty_text() -> None:
    """Step > 0 in interactive mode without the escalation signal stays auto."""
    choice = Brain._decide_tool_choice(
        mode=ExecutionMode.interactive_turn,
        step_idx=1,
        prev_ai_was_text_only=False,
        used_tools_count=0,
    )
    assert choice == "auto"


# ──────────────────────────────────────────────────────────────
# Integration: decision reaches invoke_chat
# ──────────────────────────────────────────────────────────────


class _RecordingLLMRouter:
    """Replay a scripted sequence of AIMessages; record tool_choice per step."""

    def __init__(self, scripted: list[AIMessage]) -> None:
        self._scripted = list(scripted)
        self.calls: list[dict[str, Any]] = []

    def invoke_chat(
        self,
        messages: list[Any],
        *,
        tools: list[dict[str, Any]] | None = None,
        route: str = "default",
        tool_choice: Any = None,
    ) -> AIMessage:
        _ = messages, tools, route
        self.calls.append({"tool_choice": tool_choice, "step": len(self.calls)})
        if not self._scripted:
            return AIMessage(content="done", tool_calls=[])
        return self._scripted.pop(0)


def _make_brain(router: _RecordingLLMRouter) -> Brain:
    registry = ToolRegistry()
    registry.register_callable(_noop_ping)
    return Brain(
        tool_registry=registry,
        llm_router=router,
        cost_controller=CostController(daily_budget_usd=5.0),
        max_steps=4,
    )


def test_react_loop_interactive_first_step_is_auto_by_default() -> None:
    """interactive_turn + first AI emits tool_call directly → step 0 uses 'auto'."""
    router = _RecordingLLMRouter([
        AIMessage(
            content="",
            tool_calls=[{"name": "noop_ping", "args": {"x": 1}, "id": "c1"}],
        ),
        AIMessage(content="done"),
    ])
    brain = _make_brain(router)

    asyncio.run(
        brain.run(
            query="please run the ping",
            ctx=TaskContext(mode=ExecutionMode.interactive_turn),
            prefer_llm=True,
        )
    )

    assert router.calls[0]["tool_choice"] == "auto", (
        f"interactive first step must send tool_choice='auto'; got "
        f"{router.calls[0]['tool_choice']!r}"
    )


def test_react_loop_detached_first_step_forces_required() -> None:
    router = _RecordingLLMRouter([
        AIMessage(
            content="",
            tool_calls=[{"name": "noop_ping", "args": {}, "id": "c1"}],
        ),
        AIMessage(content="done"),
    ])
    brain = _make_brain(router)

    asyncio.run(
        brain.run(
            query="scheduled probe",
            ctx=TaskContext(mode=ExecutionMode.detached_scheduled_task),
            prefer_llm=True,
        )
    )

    assert router.calls[0]["tool_choice"] == "required", (
        f"detached first step must send tool_choice='required'; got "
        f"{router.calls[0]['tool_choice']!r}"
    )


def test_react_loop_escalates_interactive_turn_after_empty_text() -> None:
    """Phase 1c: interactive_turn MUST escalate once after a pure-text step.

    Motivated by trace `e48a6be0c90e`: LLM replied "已记录..." to user prefs
    without calling ``job.memory.record`` (`used_tools=[]`). With the mode
    guard removed, step 1 MUST be forced to ``"required"`` so the LLM is
    structurally pushed to actually call a tool.
    """
    router = _RecordingLLMRouter([
        AIMessage(content="好的,已记录你的偏好。"),  # step 0: pure text commit
        AIMessage(
            content="",
            tool_calls=[{"name": "noop_ping", "args": {}, "id": "c1"}],
        ),  # step 1: escalated to required
        AIMessage(content="done"),
    ])
    brain = _make_brain(router)

    asyncio.run(
        brain.run(
            query="拼多多别投,学历过滤 985/211",
            ctx=TaskContext(mode=ExecutionMode.interactive_turn),
            prefer_llm=True,
        )
    )

    observed = [c["tool_choice"] for c in router.calls]
    assert len(observed) >= 2, (
        f"interactive escalation MUST run step 1 (not terminate after step 0); "
        f"got only {len(observed)} call(s): {observed}"
    )
    assert observed[0] == "auto", (
        f"interactive step 0 must stay 'auto'; got {observed[0]!r}"
    )
    assert observed[1] == "required", (
        f"Phase 1c: interactive step 1 MUST escalate to 'required' after "
        f"pure-text step 0 with used_tools=[]; got sequence {observed}"
    )


def test_react_loop_escalates_detached_after_empty_text() -> None:
    """Detached modes must also escalate after a pure-text step (unchanged from Phase 1b)."""
    router = _RecordingLLMRouter([
        AIMessage(content="I'll do it."),  # step 0: pure text (shouldn't happen under required but LLMs do lie)
        AIMessage(
            content="",
            tool_calls=[{"name": "noop_ping", "args": {}, "id": "c1"}],
        ),
        AIMessage(content="done"),
    ])
    brain = _make_brain(router)

    asyncio.run(
        brain.run(
            query="scheduled probe",
            ctx=TaskContext(mode=ExecutionMode.detached_scheduled_task),
            prefer_llm=True,
        )
    )

    observed = [c["tool_choice"] for c in router.calls]
    assert observed[0] == "required"  # detached rule, step 0
    assert len(observed) >= 2, (
        f"detached escalation MUST run step 1; got {observed}"
    )
    assert observed[1] == "required", (
        f"step after empty-text must stay 'required' via escalation; got {observed}"
    )


def test_react_loop_escalation_capped_at_one_retry() -> None:
    """Phase 1c invariant: escalation fires at most once per run, never loops.

    If the LLM insists on pure text even under `required`, we give up (completion
    path) rather than spin forever. Guards against cage-like behavior.
    """
    router = _RecordingLLMRouter([
        AIMessage(content="step 0 text"),  # auto → text
        AIMessage(content="step 1 still text"),  # required → still text (LLM refused)
        # would-be step 2 — MUST NOT happen: escalation already spent
        AIMessage(content="step 2 SHOULD NOT BE CALLED"),
    ])
    brain = _make_brain(router)

    asyncio.run(
        brain.run(
            query="just chat",
            ctx=TaskContext(mode=ExecutionMode.interactive_turn),
            prefer_llm=True,
        )
    )

    observed = [c["tool_choice"] for c in router.calls]
    assert len(observed) == 2, (
        f"Escalation must cap at 1 retry; expected exactly 2 LLM calls, got "
        f"{len(observed)}: {observed}"
    )
    assert observed == ["auto", "required"], observed


def test_react_loop_deescalates_after_tool_used() -> None:
    """After the first tool runs, subsequent steps must NOT force required."""
    router = _RecordingLLMRouter([
        AIMessage(
            content="",
            tool_calls=[{"name": "noop_ping", "args": {}, "id": "c1"}],
        ),  # step 0: tool call
        AIMessage(content=""),  # step 1: pure text — but we already used a tool
        AIMessage(content="done"),  # step 2: final
    ])
    brain = _make_brain(router)

    asyncio.run(
        brain.run(
            query="do it",
            ctx=TaskContext(mode=ExecutionMode.detached_scheduled_task),
            prefer_llm=True,
        )
    )

    observed = [c["tool_choice"] for c in router.calls]
    assert observed[0] == "required"  # detached rule, step 0
    # step 1+: no more forcing because used_tools > 0
    for idx, choice in enumerate(observed[1:], start=1):
        assert choice == "auto", (
            f"step {idx} must be 'auto' after tool use; got {choice!r}"
        )
