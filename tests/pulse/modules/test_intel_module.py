"""IntelModule integration tests.

Uses the postgres fixture for the real ``IntelDocumentStore`` but stubs
the LLM router, the notifier and one in-memory ``SourceFetcher`` so the
six-stage workflow runs end-to-end without external services.

What the tests pin down:

  * orchestrator wires fetch → dedup → score → summarize → diversify →
    publish in order and emits one event per stage;
  * scores below threshold drop out before publish;
  * ``intel.search`` IntentSpec round-trips through the store;
  * patrol registration is keyed by topic.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from pulse.modules.intel.module import IntelModule
from pulse.modules.intel.pipeline import DigestWorkflowOrchestrator
from pulse.modules.intel.sources import (
    RawItem,
    SourceFetchResult,
)
from pulse.modules.intel.sources.base import _REGISTRY, register_fetcher
from pulse.modules.intel.store import IntelDocumentStore
from pulse.modules.intel.topics import TopicConfig

pytestmark = pytest.mark.usefixtures("postgres_test_db")


@pytest.fixture(autouse=True)
def _reset_intel_table(postgres_test_db):
    """Drop ``intel_documents`` between tests so each starts empty.

    The shared postgres fixture is persistent — without this we'd see
    cross-test bleed (canonical_url unique conflicts, stale rows in
    ``latest_for_topic``).
    """
    from pulse.core.storage.engine import DatabaseEngine

    db = DatabaseEngine()
    db.execute("DROP TABLE IF EXISTS intel_documents CASCADE")
    yield
    db.execute("DROP TABLE IF EXISTS intel_documents CASCADE")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeFetcher:
    """In-memory fetcher registered under the ``rss`` type during tests.

    We hijack the ``rss`` slot rather than introducing a new type because
    ``SourceType`` in the topic schema is a closed Literal — keeping prod
    YAML strict beats poking a hole in the schema for tests.
    """

    source_type = "rss"

    def __init__(self, items: list[RawItem]) -> None:
        self._items = list(items)

    async def fetch(self, cfg: Any) -> SourceFetchResult:
        return SourceFetchResult(
            source_id="fake",
            source_type=self.source_type,
            items=list(self._items),
        )


@pytest.fixture
def fake_rss_fetcher():
    """Swap the ``rss`` fetcher for an in-memory fake; restore on teardown."""
    original = _REGISTRY.get("rss")
    fake_items = _items()
    register_fetcher("rss", lambda: _FakeFetcher(fake_items))
    try:
        yield fake_items
    finally:
        if original is not None:
            register_fetcher("rss", original)


class _FakeLLMRouter:
    """Replays a fixed mapping ``url -> score`` and ``url -> summary``."""

    def __init__(
        self,
        *,
        scores: dict[str, float],
        summaries: dict[str, str] | None = None,
    ) -> None:
        self._scores = scores
        self._summaries = summaries or {}

    def invoke_json(self, prompt: str, *, route: str = "default") -> Any:
        for url, score in self._scores.items():
            if url in prompt:
                return {
                    "score": float(score),
                    "dimensions": {"depth": float(score), "novelty": float(score), "impact": float(score)},
                    "tags": ["fake"],
                    "is_contrarian": False,
                }
        return {"score": 0.0, "dimensions": {}}

    def invoke_text(self, prompt: str, *, route: str = "default") -> str:
        for url, summary in self._summaries.items():
            if url in prompt:
                return summary
        return "fake summary"


class _RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, message: Any) -> None:
        self.sent.append(
            {
                "title": message.title,
                "content": message.content,
                "metadata": dict(message.metadata),
            }
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_topic(
    *,
    threshold: float = 5.0,
    promote_threshold: float = 9.0,
    diversity: dict[str, Any] | None = None,
) -> TopicConfig:
    return TopicConfig.model_validate(
        {
            "id": f"t{uuid.uuid4().hex[:8]}",
            "display_name": "Test Topic",
            "sources": [{"type": "rss", "url": "https://fake.test/feed"}],
            "scoring": {"threshold": threshold},
            "diversity": diversity or {"max_per_source": 5, "serendipity_slots": 0},
            "memory": {"promote_threshold": promote_threshold},
            "publish": {"channel": "console"},
        }
    )


def _items() -> list[RawItem]:
    now = datetime.now(timezone.utc)
    return [
        RawItem(
            url="https://fake.test/a",
            title="A High-Score",
            content_raw="High value content",
            source_type="rss",
            source_id="fake",
            published_at=now,
        ),
        RawItem(
            url="https://fake.test/b",
            title="B Low-Score",
            content_raw="Filler",
            source_type="rss",
            source_id="fake",
            published_at=now,
        ),
    ]


# ---------------------------------------------------------------------------
# Orchestrator end-to-end
# ---------------------------------------------------------------------------


def test_orchestrator_runs_six_stages_and_persists(
    postgres_test_db, fake_rss_fetcher
) -> None:
    _ = postgres_test_db
    _ = fake_rss_fetcher
    topic = _make_topic(threshold=5.0)

    store = IntelDocumentStore()
    store.ensure_schema()
    llm = _FakeLLMRouter(
        scores={"https://fake.test/a": 8.5, "https://fake.test/b": 2.0},
        summaries={"https://fake.test/a": "this is the summary"},
    )
    notifier = _RecordingNotifier()
    events: list[tuple[str, str, dict[str, Any]]] = []

    orchestrator = DigestWorkflowOrchestrator(
        store=store,
        llm_router=llm,
        notifier=notifier,
        emit_stage_event=lambda stage, status, payload: events.append(
            (stage, status, dict(payload))
        ),
    )

    result = asyncio.run(orchestrator.run(topic, dry_run=False))

    assert result.fetched == 2
    assert result.deduped == 2
    assert result.above_threshold == 1
    assert result.published == 1
    assert result.publish is not None
    assert result.publish.delivery["delivered"] is True

    stages_seen = [s for s, status, _ in events if status == "completed"]
    assert "fetch" in stages_seen
    assert "dedup" in stages_seen
    assert "score" in stages_seen
    assert "summarize" in stages_seen
    assert "diversify" in stages_seen
    assert "publish" in stages_seen
    assert "workflow" in stages_seen

    rows = store.latest_for_topic(topic.id)
    assert len(rows) == 1
    persisted = rows[0]
    assert persisted["url"] == "https://fake.test/a"
    assert persisted["score"] >= 8.0
    assert persisted["content_summary"] == "this is the summary"
    assert persisted["promoted_to_archival"] is False

    assert notifier.sent
    assert "A High-Score" in notifier.sent[0]["content"]


def test_orchestrator_drops_all_items_below_threshold(
    postgres_test_db, fake_rss_fetcher
) -> None:
    _ = postgres_test_db
    _ = fake_rss_fetcher
    topic = _make_topic(threshold=9.5)

    store = IntelDocumentStore()
    store.ensure_schema()
    llm = _FakeLLMRouter(scores={"https://fake.test/a": 3, "https://fake.test/b": 2})
    notifier = _RecordingNotifier()

    orchestrator = DigestWorkflowOrchestrator(
        store=store,
        llm_router=llm,
        notifier=notifier,
    )
    result = asyncio.run(orchestrator.run(topic))

    assert result.fetched == 2
    assert result.above_threshold == 0
    assert result.published == 0
    assert notifier.sent == []
    assert store.latest_for_topic(topic.id) == []


class _RecordingArchival:
    """Fake :class:`ArchivalMemory` that records ``add_fact`` calls."""

    def __init__(self) -> None:
        self.facts: list[dict[str, Any]] = []

    def add_fact(self, **kwargs: Any) -> dict[str, Any]:
        self.facts.append(dict(kwargs))
        return {"id": len(self.facts), **kwargs}


def test_orchestrator_promotes_high_score_to_archival(
    postgres_test_db, fake_rss_fetcher
) -> None:
    """Items above ``memory.promote_threshold`` produce one fact each."""
    _ = postgres_test_db
    _ = fake_rss_fetcher
    topic = _make_topic(threshold=5.0, promote_threshold=8.0)

    store = IntelDocumentStore()
    store.ensure_schema()
    llm = _FakeLLMRouter(
        scores={
            "https://fake.test/a": 9.0,
            "https://fake.test/b": 6.5,
        },
        summaries={"https://fake.test/a": "summary"},
    )
    notifier = _RecordingNotifier()
    archival = _RecordingArchival()

    orchestrator = DigestWorkflowOrchestrator(
        store=store,
        llm_router=llm,
        notifier=notifier,
        archival_memory=archival,  # type: ignore[arg-type]
    )
    result = asyncio.run(orchestrator.run(topic, dry_run=False))

    assert result.publish is not None
    assert result.publish.promoted_facts
    assert len(archival.facts) == 1
    fact = archival.facts[0]
    assert fact["subject"] == f"intel:{topic.id}"
    assert fact["predicate"] == "high_score_signal"
    assert fact["object_value"]["url"] == "https://fake.test/a"
    assert fact["object_value"]["score"] >= 8.0
    assert fact["promoted_from"].startswith("intel_documents:")


def test_orchestrator_dry_run_skips_archival(
    postgres_test_db, fake_rss_fetcher
) -> None:
    """``dry_run=True`` must not write facts even for high-score items."""
    _ = postgres_test_db
    _ = fake_rss_fetcher
    topic = _make_topic(threshold=5.0, promote_threshold=8.0)

    store = IntelDocumentStore()
    store.ensure_schema()
    llm = _FakeLLMRouter(scores={"https://fake.test/a": 9.0, "https://fake.test/b": 7.0})
    archival = _RecordingArchival()

    orchestrator = DigestWorkflowOrchestrator(
        store=store,
        llm_router=llm,
        notifier=_RecordingNotifier(),
        archival_memory=archival,  # type: ignore[arg-type]
    )
    asyncio.run(orchestrator.run(topic, dry_run=True))

    assert archival.facts == []


def test_orchestrator_pulls_cross_topic_serendipity(
    postgres_test_db, fake_rss_fetcher
) -> None:
    """When ``serendipity_slots>0`` the publish stage gets a cross-topic row.

    Seeds the store with one high-score row under ``other_topic``, then
    runs the orchestrator on a fresh topic — the serendipity row must
    surface in ``publish_result.text`` and the notifier metadata must
    reflect ``serendipity_count == 1``.
    """
    _ = postgres_test_db
    _ = fake_rss_fetcher
    topic = _make_topic(
        threshold=5.0, diversity={"max_per_source": 5, "serendipity_slots": 1}
    )

    store = IntelDocumentStore()
    store.ensure_schema()
    store._db.execute(  # noqa: SLF001 — DAL is the public surface for the seed
        """
        INSERT INTO intel_documents(
            id, topic_id, source_id, source_type, url, canonical_url,
            title, content_raw, content_summary, score, score_breakdown,
            tags, published_at, collected_at, promoted_to_archival
        ) VALUES (
            %s, 'other_topic', 'cross_src', 'rss',
            'https://other.example/cross', 'https://other.example/cross',
            'Cross-topic surprise', 'body', 'cross summary', 9.0, '{}'::jsonb,
            '[]'::jsonb, NOW(), NOW(), false
        )
        """,
        (str(uuid.uuid4()),),
    )

    llm = _FakeLLMRouter(
        scores={"https://fake.test/a": 8.0, "https://fake.test/b": 7.0},
        summaries={"https://fake.test/a": "primary summary"},
    )
    notifier = _RecordingNotifier()
    orchestrator = DigestWorkflowOrchestrator(
        store=store, llm_router=llm, notifier=notifier
    )

    result = asyncio.run(orchestrator.run(topic, dry_run=False))

    assert result.publish is not None
    assert result.publish.delivery["serendipity_count"] == 1
    assert "Cross-topic surprise" in result.publish.text
    assert notifier.sent
    assert notifier.sent[0]["metadata"]["serendipity_count"] == 1


def test_orchestrator_dry_run_skips_notifier(
    postgres_test_db, fake_rss_fetcher
) -> None:
    _ = postgres_test_db
    _ = fake_rss_fetcher
    topic = _make_topic(threshold=1.0)

    store = IntelDocumentStore()
    store.ensure_schema()
    llm = _FakeLLMRouter(scores={"https://fake.test/a": 8.0, "https://fake.test/b": 7.0})
    notifier = _RecordingNotifier()

    orchestrator = DigestWorkflowOrchestrator(
        store=store, llm_router=llm, notifier=notifier
    )
    result = asyncio.run(orchestrator.run(topic, dry_run=True))

    assert result.published == 2
    assert notifier.sent == []
    assert result.publish is not None
    assert result.publish.delivery["skipped"] is True


# ---------------------------------------------------------------------------
# Module-level: search + intents + patrol registration
# ---------------------------------------------------------------------------


def test_intel_module_search_round_trip(postgres_test_db) -> None:
    _ = postgres_test_db
    module = IntelModule()
    assert module._store is not None  # noqa: SLF001 — invariant for this test

    record_id = uuid.uuid4()
    module._store._db.execute(  # noqa: SLF001 — DAL is the public surface here
        """
        INSERT INTO intel_documents(
            id, topic_id, source_id, source_type, url, canonical_url,
            title, content_raw, content_summary, score, score_breakdown,
            tags, published_at, collected_at, promoted_to_archival
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s::jsonb,
            %s::jsonb, NOW(), NOW(), %s
        )
        """,
        (
            str(record_id),
            "llm_frontier",
            "fake",
            "fake",
            "https://example.com/agent-observability",
            "https://example.com/agent-observability",
            "Agent observability primer",
            "Body discussing agent observability stack.",
            "聚焦 agent 可观测性栈",
            7.5,
            "{}",
            '["agent","observability"]',
            False,
        ),
    )

    out = module.search_documents(
        keywords=["observability"],
        topic_id="llm_frontier",
        top_k=5,
    )
    assert out["ok"] is True
    assert out["count"] == 1
    assert out["items"][0]["url"] == "https://example.com/agent-observability"


def test_intel_module_exposes_four_intents() -> None:
    module = IntelModule.__new__(IntelModule)
    module.intents = []
    from pulse.modules.intel.intent import build_intel_intents

    intents = build_intel_intents(module)
    names = {i.name for i in intents}
    assert names == {
        "intel.digest.list",
        "intel.digest.latest",
        "intel.digest.run",
        "intel.search",
    }


def test_intel_search_is_registered_as_mcp_tool(postgres_test_db) -> None:
    """``intel.search`` must surface in the MCP-facing tool listing.

    ``IntentSpec`` → ``ModuleRegistry.as_tools()`` → ``ToolRegistry`` →
    ``MCPServerAdapter`` is the chain Brain ReAct + external Claude
    Desktop / Cursor / Cline rely on; pin it down here so we don't
    silently drop the agentic-search surface during a refactor.
    """
    _ = postgres_test_db
    from pulse.core.mcp_server import MCPServerAdapter
    from pulse.core.module import ModuleRegistry
    from pulse.core.tool import ToolRegistry

    registry = ModuleRegistry()
    registry.register(IntelModule())

    tool_registry = ToolRegistry()
    for tool_descriptor in registry.as_tools():
        tool_registry.register(
            name=str(tool_descriptor["name"]),
            handler=tool_descriptor["handler"],  # type: ignore[arg-type]
            description=str(tool_descriptor["description"]),
            ring=str(tool_descriptor.get("ring") or "ring2_module"),  # type: ignore[arg-type]
            schema=dict(tool_descriptor.get("schema") or {}),  # type: ignore[arg-type]
            metadata=dict(tool_descriptor.get("metadata") or {}),  # type: ignore[arg-type]
        )

    adapter = MCPServerAdapter(tool_registry=tool_registry)
    surface = {entry["name"] for entry in adapter.list_tools()}
    assert {"intel.search", "intel.digest.list", "intel.digest.run"}.issubset(surface)

    search_entry = next(t for t in adapter.list_tools() if t["name"] == "intel.search")
    schema = search_entry["inputSchema"]
    assert schema["type"] == "object"
    assert "keywords" in schema["properties"]


def test_build_search_tool_rejects_empty_keywords() -> None:
    """``build_search_tool`` is the stable wrapper used by adapters.

    Empty / whitespace-only keyword arrays must be rejected before they
    reach the store — an unbounded ``%%`` query would hammer the DB
    and return everything.
    """
    from pulse.modules.intel.tool import build_search_tool

    class _StubService:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def search_documents(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(dict(kwargs))
            return {"ok": True, "items": [], "count": 0}

    stub = _StubService()
    callable_ = build_search_tool(stub)

    out = callable_(keywords=[])
    assert out["ok"] is False
    out = callable_(keywords=["   "])
    assert out["ok"] is False
    assert stub.calls == []

    out = callable_(keywords=["agent", "  observability  "])
    assert out["ok"] is True
    assert stub.calls[0]["keywords"] == ["agent", "observability"]


class _FakeRuntime:
    def __init__(self) -> None:
        self.registered: list[dict[str, Any]] = []

    def register_patrol(self, **kwargs: Any) -> None:
        self.registered.append(kwargs)


def test_intel_module_registers_one_patrol_per_topic(postgres_test_db) -> None:
    _ = postgres_test_db
    module = IntelModule()
    runtime = _FakeRuntime()
    module.bind_runtime(runtime)
    module.on_startup()

    topic_count = len(module._topics)  # noqa: SLF001
    assert topic_count >= 1
    assert len(runtime.registered) == topic_count
    names = {p["name"] for p in runtime.registered}
    assert all(n.startswith("intel.digest.") for n in names)
