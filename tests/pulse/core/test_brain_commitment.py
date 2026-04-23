"""Contract C v2 · Brain integration guard tests — ADR-001 §4.4.4.

These tests pin how ``Brain._react_loop`` wires Contract C v2 into the
final-response path:

1. Exactly one ``commitment_verifier.verify`` call per completed
   ``interactive_turn`` / ``detached_scheduled_task`` turn, with the
   v2 input surface (``raw_reply`` / ``shaped_reply`` / ``turn_evidence``).
2. Verdict ``"unfulfilled"`` REPLACES the user-visible reply with
   ``result.reply`` (honest rewrite) — the original fake-success text
   must NOT reach the user.
3. Verdicts ``"verified"`` / ``"degraded"`` leave the shaped reply
   intact (fail-OPEN on verifier errors).
4. Exactly one ``brain.commitment.*`` event per verdict, payload
   carries ``hallucination_type`` + receipt counts.
5. ``heartbeat_turn`` skips Contract C entirely (no user-visible reply
   → judge spend is waste).
6. No verifier wired → no call, no event (backwards compat).
7. ``TurnEvidence`` flowing to the verifier carries tool receipts with
   ``input_keys`` / ``extracted_facts`` (§4.4.2), enabling the judge
   to ground commitment claims on structured facts.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import AIMessage

from pulse.core.brain import Brain
from pulse.core.cost import CostController
from pulse.core.task_context import ExecutionMode, TaskContext
from pulse.core.tool import ToolRegistry, tool
from pulse.core.verifier import (
    CommitmentVerifier,
    TurnEvidence,
    VerifierResult,
)


@tool(
    name="noop.echo",
    description="mock tool for contract C tests",
    extract_facts=lambda obs: {"echoed_keys_count": len((obs or {}).get("echo") or {})},
)
def _noop_echo(args: dict[str, Any]) -> dict[str, Any]:
    return {"echo": args, "result_count": 1}


class _ScriptedRouter:
    def __init__(self, scripted: list[AIMessage]) -> None:
        self._scripted = list(scripted)
        self.calls = 0

    def invoke_chat(
        self,
        messages: list[Any],
        *,
        tools: list[dict[str, Any]] | None = None,
        route: str = "default",
        tool_choice: Any = None,
    ) -> AIMessage:
        _ = messages, tools, route, tool_choice
        self.calls += 1
        if not self._scripted:
            return AIMessage(content="done", tool_calls=[])
        return self._scripted.pop(0)

    # Reshaper fallback path; tests keep reply clean so this stays unused.
    def invoke_text(self, messages: list[Any], *, route: str = "default") -> str:
        _ = messages, route
        return ""


class _StubVerifier(CommitmentVerifier):
    """Captures the v2 input envelope and returns a pre-baked verdict.

    Bypasses the real judge LLM; the only thing we care about in these
    integration tests is the shape of what Brain hands to the verifier.
    """

    def __init__(self, result: VerifierResult) -> None:
        super().__init__(llm_router=None)
        self._result = result
        self.call_args: dict[str, Any] | None = None

    def verify(  # type: ignore[override]
        self,
        *,
        ctx: TaskContext,
        query: str,
        raw_reply: str,
        shaped_reply: str,
        turn_evidence: TurnEvidence,
    ) -> VerifierResult:
        self.call_args = {
            "ctx": ctx,
            "query": query,
            "raw_reply": raw_reply,
            "shaped_reply": shaped_reply,
            "turn_evidence": turn_evidence,
        }
        return self._result


class _EventRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))


class _StubCoreMemoryWithSoulPrefix:
    """Minimal CoreMemory stub: exposes ``read_block('soul')`` with a prefix.

    Used to verify Contract C v2 P1 fix — rewritten honest replies MUST
    bypass ``_apply_soul_style`` (no persona prefix distortion). Covers
    the small subset Brain touches during ``_react_loop``: ``read_block``
    for soul-style, ``snapshot`` for the memory-reader adapter invoked
    by ``PromptContractBuilder._section_identity``.
    """

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    def read_block(self, name: str) -> dict[str, Any]:
        if name == "soul":
            return {"assistant_prefix": self._prefix}
        raise KeyError(name)

    def snapshot(self) -> dict[str, Any]:
        return {"soul": {"assistant_prefix": self._prefix}}

    def read_identity(self) -> dict[str, Any]:
        return {}

    def read_prefs(self) -> dict[str, Any]:
        return {}


def _make_brain(
    router: _ScriptedRouter,
    verifier: CommitmentVerifier | None = None,
    *,
    core_memory: Any = None,
) -> tuple[Brain, _EventRecorder]:
    registry = ToolRegistry()
    registry.register_callable(_noop_echo)
    emitter = _EventRecorder()
    brain = Brain(
        tool_registry=registry,
        llm_router=router,
        cost_controller=CostController(daily_budget_usd=5.0),
        max_steps=4,
        core_memory=core_memory,
        commitment_verifier=verifier,
        event_emitter=emitter,
    )
    return brain, emitter


# ──────────────────────────────────────────────────────────────
# Verified path — shaped reply passes through unchanged
# ──────────────────────────────────────────────────────────────


def test_verified_verdict_keeps_reply_unchanged() -> None:
    # Two-step script: step 0 calls a tool (keeps used_tools non-empty so
    # Phase 1c escalation does NOT fire), step 1 emits final text.
    router = _ScriptedRouter([
        AIMessage(
            content="",
            tool_calls=[{"name": "noop_echo", "args": {}, "id": "c1"}],
        ),
        AIMessage(content="这是回复。"),
    ])
    verifier = _StubVerifier(
        VerifierResult(
            verdict="verified",
            reply="这是回复。",
            commitment_excerpt="",
            hallucination_type="none",
        )
    )
    brain, emitter = _make_brain(router, verifier)

    result = asyncio.run(brain.run(
        query="随便聊聊",
        ctx=TaskContext(mode=ExecutionMode.interactive_turn),
        prefer_llm=True,
    ))

    assert verifier.call_args is not None, "verifier MUST be called once on completed turn"
    assert verifier.call_args["query"] == "随便聊聊"
    assert "这是回复" in result.answer

    verdicts = [et for et, _ in emitter.events if et.startswith("brain.commitment.")]
    assert verdicts == ["brain.commitment.verified"], (
        f"exactly one 'verified' event expected; got {verdicts}"
    )
    # Payload contract (ADR-001 §4.4.5)
    _, payload = next(
        (et, p) for et, p in emitter.events if et == "brain.commitment.verified"
    )
    assert payload.get("hallucination_type") == "none"
    assert payload.get("tool_receipt_count") == 1
    assert payload.get("pre_capture_count") == 0


# ──────────────────────────────────────────────────────────────
# Unfulfilled path — reply MUST be overridden (fail-LOUD)
# ──────────────────────────────────────────────────────────────


def test_unfulfilled_verdict_overrides_reply_and_persists_event() -> None:
    """trace_e48a6be0c90e regression guard.

    LLM claims "已记录" but no tool ran. Verifier returns an honest
    rewrite. Brain MUST serve the rewrite to the user, not the
    original fake-success text.
    """
    original_reply = "好的,已记录拼多多避免偏好。"
    rewritten = "抱歉,我其实没能把'拼多多别投'写入记忆,请再跟我说一次。"
    # Two-step script: first with tool_calls (dummy) to suppress Phase 1c
    # escalation, second the fake-success text that must get rewritten.
    router = _ScriptedRouter([
        AIMessage(
            content="",
            tool_calls=[{"name": "noop_echo", "args": {}, "id": "c1"}],
        ),
        AIMessage(content=original_reply),
    ])
    verifier = _StubVerifier(VerifierResult(
        verdict="unfulfilled",
        reply=rewritten,
        reason="reply claimed memory.record but ledger has no matching receipt",
        commitment_excerpt="已记录拼多多避免偏好",
        hallucination_type="false_absence",
    ))
    brain, emitter = _make_brain(router, verifier)

    result = asyncio.run(brain.run(
        query="拼多多别投",
        ctx=TaskContext(mode=ExecutionMode.interactive_turn),
        prefer_llm=True,
    ))

    assert rewritten in result.answer, (
        f"unfulfilled MUST replace reply with rewritten text; got: {result.answer!r}"
    )
    assert "已记录拼多多" not in result.answer, (
        f"original fake-success text MUST NOT reach the user; got: {result.answer!r}"
    )

    unfulfilled = [
        (et, p) for et, p in emitter.events if et == "brain.commitment.unfulfilled"
    ]
    assert len(unfulfilled) == 1, (
        f"exactly one 'unfulfilled' event expected; got {emitter.events}"
    )
    _, payload = unfulfilled[0]
    assert payload.get("commitment_excerpt") == "已记录拼多多避免偏好"
    assert payload.get("reason")
    assert payload.get("hallucination_type") == "false_absence"
    assert "rewritten_reply_preview" in payload
    assert "raw_reply_preview" in payload
    assert "shaped_reply_preview" in payload


# ──────────────────────────────────────────────────────────────
# P1 regression — rewritten honest reply MUST bypass soul_style
# ──────────────────────────────────────────────────────────────


def test_unfulfilled_rewrite_bypasses_soul_style_prefix() -> None:
    """Regression for trace_16e97afe3ffc.

    Before the P1 fix, ``_apply_soul_style`` blindly prepended the
    CoreMemory ``assistant_prefix`` to **every** outgoing reply — including
    the verifier's honest rewrite — yielding confusing emotive strings
    like "你真是咱家的超能跳水小勇者: 我其实没能完成投递…".

    Fix: when verdict is ``"unfulfilled"`` the rewrite is emitted verbatim;
    soul styling is only applied on ``verified`` / ``degraded`` paths.
    """
    prefix = "你真是咱家的超能跳水小勇者"
    core_mem = _StubCoreMemoryWithSoulPrefix(prefix=prefix)

    rewritten = "我其实没能完成投递这5个岗位,请稍后重试或手动点开链接。"
    router = _ScriptedRouter([
        AIMessage(
            content="",
            tool_calls=[{"name": "noop_echo", "args": {}, "id": "c1"}],
        ),
        AIMessage(content="已为你投递了 5 个岗位."),
    ])
    verifier = _StubVerifier(VerifierResult(
        verdict="unfulfilled",
        reply=rewritten,
        reason="ledger greeted=0, unavailable=5; no send actually landed",
        commitment_excerpt="已为你投递了 5 个岗位",
        hallucination_type="false_absence",
    ))
    brain, _ = _make_brain(router, verifier, core_memory=core_mem)

    result = asyncio.run(brain.run(
        query="帮我投 5 个",
        ctx=TaskContext(mode=ExecutionMode.interactive_turn),
        prefer_llm=True,
    ))

    assert rewritten in result.answer
    assert not result.answer.startswith(prefix), (
        f"P1 violated: rewritten reply got soul_style prefix; answer={result.answer!r}"
    )
    assert prefix not in result.answer, (
        f"persona prefix MUST NOT appear anywhere in honest rewrite; "
        f"answer={result.answer!r}"
    )


def test_verified_path_still_receives_soul_style_prefix() -> None:
    """Counter-test for P1: non-unfulfilled paths DO keep styling.

    Ensures the P1 bypass is surgical — verified replies (no commitment
    issue) still get the persona prefix so normal UX stays intact.
    """
    prefix = "Pulse"
    core_mem = _StubCoreMemoryWithSoulPrefix(prefix=prefix)

    router = _ScriptedRouter([
        AIMessage(
            content="",
            tool_calls=[{"name": "noop_echo", "args": {}, "id": "c1"}],
        ),
        AIMessage(content="这是回复。"),
    ])
    verifier = _StubVerifier(VerifierResult(
        verdict="verified",
        reply="这是回复。",
        hallucination_type="none",
    ))
    brain, _ = _make_brain(router, verifier, core_memory=core_mem)

    result = asyncio.run(brain.run(
        query="随便聊聊",
        ctx=TaskContext(mode=ExecutionMode.interactive_turn),
        prefer_llm=True,
    ))

    assert result.answer.startswith(f"{prefix}:"), (
        f"verified path MUST still apply soul_style prefix; answer={result.answer!r}"
    )


# ──────────────────────────────────────────────────────────────
# Degraded path — fail-OPEN: shaped reply survives
# ──────────────────────────────────────────────────────────────


def test_degraded_verdict_keeps_original_reply_and_emits_event() -> None:
    router = _ScriptedRouter([
        AIMessage(
            content="",
            tool_calls=[{"name": "noop_echo", "args": {}, "id": "c1"}],
        ),
        AIMessage(content="some reply"),
    ])
    verifier = _StubVerifier(VerifierResult(
        verdict="degraded",
        reply="some reply",  # verifier never rewrites on its own failure
        reason="commitment_verifier_llm_error_or_unparseable",
    ))
    brain, emitter = _make_brain(router, verifier)

    result = asyncio.run(brain.run(
        query="anything",
        ctx=TaskContext(mode=ExecutionMode.interactive_turn),
        prefer_llm=True,
    ))

    assert "some reply" in result.answer, (
        "degraded path MUST pass through the shaped reply (fail-OPEN)"
    )
    degraded = [et for et, _ in emitter.events if et == "brain.commitment.degraded"]
    assert degraded == ["brain.commitment.degraded"]


# ──────────────────────────────────────────────────────────────
# No verifier wired — backwards compat (no call, no event)
# ──────────────────────────────────────────────────────────────


def test_no_verifier_skips_contract_c_entirely() -> None:
    router = _ScriptedRouter([
        AIMessage(
            content="",
            tool_calls=[{"name": "noop_echo", "args": {}, "id": "c1"}],
        ),
        AIMessage(content="some reply"),
    ])
    brain, emitter = _make_brain(router, verifier=None)

    result = asyncio.run(brain.run(
        query="x",
        ctx=TaskContext(mode=ExecutionMode.interactive_turn),
        prefer_llm=True,
    ))

    assert "some reply" in result.answer
    commitment_events = [et for et, _ in emitter.events if et.startswith("brain.commitment.")]
    assert commitment_events == [], (
        f"no commitment.* events should fire when verifier is not wired; "
        f"got {commitment_events}"
    )


# ──────────────────────────────────────────────────────────────
# Heartbeat skip — cost control
# ──────────────────────────────────────────────────────────────


def test_heartbeat_turn_skips_verifier() -> None:
    """Heartbeats have no user-visible reply; Contract C spend would be waste."""
    router = _ScriptedRouter([AIMessage(content="heartbeat reply")])
    verifier = _StubVerifier(VerifierResult(verdict="verified", reply="heartbeat reply"))
    brain, emitter = _make_brain(router, verifier)

    _ = asyncio.run(brain.run(
        query="heartbeat",
        ctx=TaskContext(mode=ExecutionMode.heartbeat_turn),
        prefer_llm=True,
    ))

    assert verifier.call_args is None, (
        "heartbeat MUST skip CommitmentVerifier — no user-visible reply, "
        "no reason to pay judge LLM cost"
    )
    commitment_events = [et for et, _ in emitter.events if et.startswith("brain.commitment.")]
    assert commitment_events == []


# ──────────────────────────────────────────────────────────────
# TurnEvidence shape — tool receipts flow through (Contract C v2)
# ──────────────────────────────────────────────────────────────


def test_verifier_receives_turn_evidence_with_tool_receipts() -> None:
    """Integration detail (§4.4.2): tool ReAct steps produce Receipt
    entries with ``input_keys`` + ``extracted_facts`` + ``result_count``,
    visible to the verifier as ``turn_evidence.tool_receipts``.
    """
    router = _ScriptedRouter([
        AIMessage(
            content="",
            tool_calls=[{"name": "noop_echo", "args": {"x": 1, "y": 2}, "id": "c1"}],
        ),
        AIMessage(content="done"),
    ])
    verifier = _StubVerifier(
        VerifierResult(verdict="verified", reply="done", hallucination_type="none")
    )
    brain, _ = _make_brain(router, verifier)

    asyncio.run(brain.run(
        query="run echo",
        ctx=TaskContext(mode=ExecutionMode.interactive_turn),
        prefer_llm=True,
    ))

    assert verifier.call_args is not None
    evidence: TurnEvidence = verifier.call_args["turn_evidence"]
    assert isinstance(evidence, TurnEvidence)

    assert len(evidence.tool_receipts) == 1
    receipt = evidence.tool_receipts[0]
    assert receipt.kind == "tool"
    assert "noop" in receipt.name
    assert receipt.status == "ok"
    assert set(receipt.input_keys) == {"x", "y"}
    # The @tool decorator's extract_facts hook ran — we got business facts
    # rather than a blind top-level scalar dump.
    assert "echoed_keys_count" in receipt.extracted_facts
    # result_count inferred from observation (default heuristic hit the
    # tool's explicit "result_count" key).
    assert receipt.result_count == 1

    # raw_reply / shaped_reply are BOTH forwarded (Contract C v2 γ).
    assert verifier.call_args["raw_reply"]
    assert verifier.call_args["shaped_reply"]
