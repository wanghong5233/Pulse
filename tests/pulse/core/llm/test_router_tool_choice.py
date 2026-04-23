"""Contract B (Call Contract) · Phase 1a guard tests.

`LLMRouter.invoke_chat` must transparently forward the caller-specified
``tool_choice`` to ``client.bind_tools(...)`` and surface the effective
value in the ``llm.invoke.ok`` event payload (``tool_choice_applied``).

This is the **API-layer** half of Contract B: the L4 in ADR-001 §3.2.
Brain's structural-signal decision (L5) is a separate phase.

Contract we pin down here:

1. Default call ``invoke_chat(messages, tools=[...])`` stays backward
   compatible: no ``tool_choice`` keyword is forwarded to ``bind_tools``,
   event payload reports ``tool_choice_applied = None``.
2. Caller can pass ``tool_choice="auto" | "required" | "none"`` or a
   provider-specific dict; router forwards verbatim via
   ``bind_tools(tools, tool_choice=...)``.
3. When no tools are bound, ``tool_choice`` is silently ignored (it makes
   no sense to force a tool call with nothing to call) — but the event
   payload still reports the caller's intent so audits can see the
   LLM was asked for tool use but had none available.
4. The router never synthesizes its own ``tool_choice`` value — only the
   caller decides. This keeps Brain (L5) the single source of truth for
   "when to force tool use".
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from pulse.core.llm.router import LLMRouter


class _RecorderClient:
    """Fake LangChain chat client that records bind_tools / invoke args."""

    def __init__(self, model: str) -> None:
        self._model = model
        self.bind_tools_calls: list[dict[str, Any]] = []
        self.bound_tools: list[dict[str, Any]] | None = None
        self.bound_tool_choice: Any = "__NOT_FORWARDED__"
        self.invoke_args: list[Any] = []

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN201
        self.bind_tools_calls.append({"tools": tools, "kwargs": dict(kwargs)})
        self.bound_tools = tools
        if "tool_choice" in kwargs:
            self.bound_tool_choice = kwargs["tool_choice"]
        return self

    def invoke(self, messages):  # noqa: ANN001, ANN201
        self.invoke_args.append(messages)
        return AIMessage(content="ok", tool_calls=[])


class _EventSink:
    """Mirrors the event_emitter callable contract: ``(event_type, payload_dict)``."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, dict(payload)))


def _make_router(client: _RecorderClient, sink: _EventSink) -> LLMRouter:
    router = LLMRouter(
        route_defaults={"default": ("m1", "m2")},
        client_factory=lambda model, base_url, api_key: client,  # noqa: ARG005
    )
    # stub API config so build_client never hits real env lookups
    router.resolve_api_config = lambda model="": ("https://stub", "sk-stub")  # type: ignore[method-assign]
    router.bind_event_emitter(sink.emit)
    return router


# ──────────────────────────────────────────────────────────────
# 1. Default: backward-compatible, no tool_choice forwarded
# ──────────────────────────────────────────────────────────────


def test_invoke_chat_default_does_not_forward_tool_choice() -> None:
    client = _RecorderClient("m1")
    sink = _EventSink()
    router = _make_router(client, sink)

    router.invoke_chat(
        [HumanMessage(content="hi")],
        tools=[{"type": "function", "function": {"name": "t1"}}],
    )

    assert len(client.bind_tools_calls) == 1
    assert "tool_choice" not in client.bind_tools_calls[0]["kwargs"], (
        "when caller omits tool_choice, router must NOT invent one — "
        "letting the provider apply its default (usually 'auto')"
    )
    # make_payload drops None-valued fields, so tool_choice_applied must
    # be **absent** from the payload when no choice was specified (the
    # audit reader interprets "missing" as "auto / provider default").
    applied = _last_applied(sink)
    assert applied == "__MISSING__", (
        f"default call must NOT surface tool_choice_applied (None is "
        f"filtered out by make_payload); got {applied!r}"
    )


# ──────────────────────────────────────────────────────────────
# 2. Caller-specified values are forwarded verbatim
# ──────────────────────────────────────────────────────────────


def test_invoke_chat_forwards_tool_choice_required() -> None:
    client = _RecorderClient("m1")
    sink = _EventSink()
    router = _make_router(client, sink)

    router.invoke_chat(
        [HumanMessage(content="hi")],
        tools=[{"type": "function", "function": {"name": "t1"}}],
        tool_choice="required",
    )

    assert client.bound_tool_choice == "required"
    assert _last_applied(sink) == "required"


def test_invoke_chat_forwards_tool_choice_auto() -> None:
    client = _RecorderClient("m1")
    sink = _EventSink()
    router = _make_router(client, sink)

    router.invoke_chat(
        [HumanMessage(content="hi")],
        tools=[{"type": "function", "function": {"name": "t1"}}],
        tool_choice="auto",
    )

    assert client.bound_tool_choice == "auto"
    assert _last_applied(sink) == "auto"


def test_invoke_chat_forwards_specific_tool_choice_dict() -> None:
    """Provider-specific shapes (e.g. OpenAI's
    ``{"type": "function", "function": {"name": "t1"}}``) must pass through
    untouched — router is a transport, not a translator."""
    client = _RecorderClient("m1")
    sink = _EventSink()
    router = _make_router(client, sink)

    choice = {"type": "function", "function": {"name": "t1"}}
    router.invoke_chat(
        [HumanMessage(content="hi")],
        tools=[{"type": "function", "function": {"name": "t1"}}],
        tool_choice=choice,
    )

    assert client.bound_tool_choice == choice
    assert _last_applied(sink) == choice


# ──────────────────────────────────────────────────────────────
# 3. No tools → tool_choice is noise; event still records intent
# ──────────────────────────────────────────────────────────────


def test_invoke_chat_no_tools_ignores_bind_but_records_intent() -> None:
    client = _RecorderClient("m1")
    sink = _EventSink()
    router = _make_router(client, sink)

    router.invoke_chat(
        [HumanMessage(content="hi")],
        tools=None,
        tool_choice="required",
    )

    assert client.bind_tools_calls == [], (
        "with tools=None, bind_tools must not be called — there's nothing "
        "to force; silently forcing on an empty toolset would be a footgun"
    )
    # but audit trail still shows the caller's intent, so we can detect
    # "Brain forced required but had no tools to offer" at review time
    assert _last_applied(sink) == "required"


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────


def _last_applied(sink: _EventSink) -> Any:
    for event_type, payload in reversed(sink.events):
        if event_type.endswith(".ok") or event_type.endswith("invoke.ok"):
            return payload.get("tool_choice_applied", "__MISSING__")
    raise AssertionError(
        f"no llm.invoke.ok event emitted; got: {[e[0] for e in sink.events]}"
    )
