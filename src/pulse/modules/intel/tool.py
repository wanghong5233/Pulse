"""Stable wrapper for the agentic ``intel.search`` capability.

The IntentSpec declared in :mod:`pulse.modules.intel.intent` is already
the canonical surface — ``ModuleRegistry.as_tools()`` projects every
intent into the in-process ``ToolRegistry``, and
``MCPServerAdapter(tool_registry=...)`` re-exports that registry over
JSON-RPC. So ``intel.search`` automatically reaches:

  * Brain's ReAct loop (in-process tool_use);
  * Cursor / Claude Desktop / Cline (MCP stdio).

This module gives that capability a separate, importable name so
adapters (test rigs, future external connectors) can bind to a stable
shape without depending on the whole :class:`IntelModule` instance.

The agentic part is split correctly:

  * Brain extracts keywords from the natural-language utterance using
    its own LLM call (``IntentSpec`` parameters_schema documents the
    contract);
  * The store performs literal ILIKE substring match — fast, debuggable,
    no surprise relevance.

If a future caller wants "type a question, get answers" without going
through Brain, build that thin LLM-rewrite layer on *top* of this
wrapper, not inside the store.
"""

from __future__ import annotations

from typing import Any, Callable

SearchCallable = Callable[..., dict[str, Any]]


def build_search_tool(service: Any) -> SearchCallable:
    """Return ``service.search_documents`` bound to a kwargs-only API.

    The shape mirrors the IntentSpec parameters_schema so any adapter
    (e.g. the MCP server, an integration test) wires it without
    re-translating argument names.
    """

    def _call(
        *,
        keywords: list[str],
        topic_id: str | None = None,
        top_k: int = 10,
        match: str = "any",
    ) -> dict[str, Any]:
        clean_keywords = [
            str(k).strip()
            for k in (keywords or [])
            if str(k or "").strip()
        ]
        if not clean_keywords:
            return {"ok": False, "error": "at least one non-empty keyword is required"}
        return service.search_documents(
            keywords=clean_keywords,
            topic_id=topic_id,
            top_k=int(top_k),
            match=str(match or "any"),
        )

    return _call
