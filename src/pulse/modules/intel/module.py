"""IntelModule — single deterministic-workflow entry point.

The module is the only place that:

  * loads YAML topic configs at startup and validates them;
  * owns one :class:`DigestWorkflowOrchestrator` instance shared across
    all topics;
  * registers one patrol per topic with the AgentRuntime (peak /
    offpeak intervals come from the topic config);
  * exposes the four IntentSpec tools (``intel.digest.*`` / ``intel.search``)
    for Brain tool-use, plus the matching HTTP routes under
    ``/api/modules/intel/*``.

The orchestrator and the topic configs are immutable for the lifetime
of the process; reloading topics requires a restart, which is fine for
PR1 — config file count is small and reloading mid-run would invalidate
in-flight patrol contexts.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...core.llm.router import LLMRouter
from ...core.memory.archival_memory import ArchivalMemory
from ...core.module import BaseModule, IntentSpec
from ...core.notify.notifier import (
    ConsoleNotifier,
    FeishuNotifier,
    MultiNotifier,
    Notifier,
)
from ...core.storage.engine import DatabaseEngine
from ...core.task_context import TaskContext
from .intent import build_intel_intents
from .pipeline import DigestWorkflowOrchestrator
from .sources import (  # noqa: F401  side-effect: register fetchers
    GitHubTrendingFetcher,
    RssFetcher,
    WebSearchFetcher,
)
from .store import IntelDocumentStore
from .topics import TopicConfig, load_topic_configs

logger = logging.getLogger(__name__)

_TOPICS_DIR = Path(__file__).parent / "topics"

try:  # db extra is optional when importing modules in a non-storage environment.
    from psycopg import Error as PsycopgError
except ImportError:  # pragma: no cover - exercised only without the db extra
    PsycopgError = RuntimeError  # type: ignore[misc, assignment]


# ---------------------------------------------------------------------------
# HTTP request schemas
# ---------------------------------------------------------------------------


class DigestRunRequest(BaseModel):
    dry_run: bool = False


class IntelSearchRequest(BaseModel):
    keywords: list[str] = Field(default_factory=list, min_length=1, max_length=8)
    topic_id: str | None = None
    top_k: int = Field(default=10, ge=1, le=50)
    match: str = Field(default="any", pattern="^(any|all)$")


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------


class IntelModule(BaseModule):
    """Single Intel module — owns YAML topics + DigestWorkflow."""

    name = "intel"
    description = (
        "Multi-channel signal subscription module. Runs a deterministic "
        "fetch → dedup → score → summarize → diversify → publish workflow "
        "per declared topic, plus a keyword search tool over the persisted "
        "corpus."
    )
    route_prefix = "/api/modules/intel"
    tags = ["intel"]

    def __init__(
        self,
        *,
        topics_dir: Path | None = None,
        store: IntelDocumentStore | None = None,
        llm_router: LLMRouter | None = None,
        notifier: Notifier | None = None,
        archival_memory: ArchivalMemory | None = None,
    ) -> None:
        super().__init__()
        self._topics_dir = topics_dir or _TOPICS_DIR
        self._topics: dict[str, TopicConfig] = {}
        self._load_topics()

        self._store: IntelDocumentStore | None = store or self._build_store()
        self._llm_router = llm_router or LLMRouter()
        self._notifier: Notifier = notifier or self._build_notifier()
        self._archival_memory: ArchivalMemory | None = (
            archival_memory if archival_memory is not None else self._build_archival_memory()
        )
        self._orchestrator: DigestWorkflowOrchestrator | None = (
            self._build_orchestrator() if self._store is not None else None
        )

        self.intents: list[IntentSpec] = build_intel_intents(self)

    # ------------------------------------------------------------------ wiring

    def _load_topics(self) -> None:
        if not self._topics_dir.is_dir():
            logger.warning("intel topics dir missing: %s", self._topics_dir)
            self._topics = {}
            return
        configs = load_topic_configs(self._topics_dir)
        self._topics = {cfg.id: cfg for cfg in configs}
        logger.info("intel module loaded %d topic(s): %s", len(self._topics), sorted(self._topics))

    def _build_store(self) -> IntelDocumentStore | None:
        try:
            store = IntelDocumentStore(db_engine=DatabaseEngine())
        except RuntimeError as exc:
            logger.warning("intel module starting without DB engine: %s", exc)
            return None
        try:
            store.ensure_schema()
        except RuntimeError as exc:
            logger.error("intel store schema check failed: %s", exc)
            raise
        except PsycopgError as exc:
            logger.warning("intel store unavailable, deferring schema init: %s", exc)
        return store

    def _build_notifier(self) -> Notifier:
        return MultiNotifier([ConsoleNotifier(), FeishuNotifier()])

    def _build_archival_memory(self) -> ArchivalMemory | None:
        """Best-effort ArchivalMemory wiring; absent DB → no promotion."""
        try:
            return ArchivalMemory()
        except (RuntimeError, PsycopgError) as exc:
            logger.warning(
                "intel module starting without ArchivalMemory promotion: %s", exc
            )
            return None

    def _build_orchestrator(self) -> DigestWorkflowOrchestrator:
        assert self._store is not None
        return DigestWorkflowOrchestrator(
            store=self._store,
            llm_router=self._llm_router,
            notifier=self._notifier,
            archival_memory=self._archival_memory,
            emit_stage_event=self._emit_pipeline_event,
        )

    def _emit_pipeline_event(
        self,
        stage: str,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        """Project orchestrator events onto :meth:`emit_stage_event`."""
        trace_id = str(payload.get("trace_id") or "").strip() or None
        clean_payload = {k: v for k, v in payload.items() if k != "trace_id"}
        self.emit_stage_event(
            stage=f"intel.{stage}",
            status=status,
            trace_id=trace_id,
            payload=clean_payload,
        )

    # ------------------------------------------------------------------ AgentRuntime

    def on_startup(self) -> None:
        if not self._runtime:
            return
        if self._orchestrator is None:
            logger.warning(
                "intel module skip patrol registration: store/orchestrator not ready"
            )
            return
        for topic in self._topics.values():
            self._register_topic_patrol(topic)

    def _register_topic_patrol(self, topic: TopicConfig) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        peak = int(topic.publish.peak_interval_seconds)
        offpeak = int(topic.publish.offpeak_interval_seconds)
        runtime.register_patrol(
            name=topic.patrol_name,
            handler=self._make_patrol_handler(topic.id),
            peak_interval=peak,
            offpeak_interval=offpeak,
            weekday_windows=((0, 24),),
            weekend_windows=((0, 24),),
        )
        logger.info(
            "intel patrol registered topic=%s peak=%ds offpeak=%ds",
            topic.id,
            peak,
            offpeak,
        )

    def _make_patrol_handler(self, topic_id: str):
        def _handler(ctx: TaskContext) -> dict[str, Any]:
            _ = ctx
            return _run_async(self.run_digest(topic_id=topic_id, dry_run=False))

        return _handler

    # ------------------------------------------------------------------ service surface

    def list_digests(self) -> dict[str, Any]:
        """Snapshot of every active topic + last collection metadata."""
        topics_meta: dict[str, dict[str, Any]] = {}
        if self._store is not None:
            for row in self._store.list_topics():
                topics_meta[row["topic_id"]] = row

        topics_out: list[dict[str, Any]] = []
        for topic in self._topics.values():
            meta = topics_meta.get(topic.id, {})
            topics_out.append(
                {
                    "id": topic.id,
                    "display_name": topic.display_name,
                    "description": topic.description,
                    "sources": len(topic.sources),
                    "schedule_cron": topic.publish.schedule_cron,
                    "channel": topic.publish.channel,
                    "doc_count": int(meta.get("doc_count") or 0),
                    "last_collected_at": meta.get("last_collected_at"),
                    "top_score": float(meta.get("top_score") or 0.0),
                }
            )
        return {
            "ok": True,
            "topics": topics_out,
            "store_ready": self._store is not None,
        }

    def latest_digest(self, *, topic_id: str, limit: int = 30) -> dict[str, Any]:
        topic = self._topics.get(topic_id)
        if topic is None:
            return {"ok": False, "error": f"unknown topic_id: {topic_id}"}
        if self._store is None:
            return {"ok": False, "error": "intel store unavailable"}
        rows = self._store.latest_for_topic(topic_id, limit=limit)
        return {
            "ok": True,
            "topic_id": topic.id,
            "display_name": topic.display_name,
            "items": rows,
            "count": len(rows),
        }

    async def run_digest(
        self,
        *,
        topic_id: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        topic = self._topics.get(topic_id)
        if topic is None:
            return {"ok": False, "error": f"unknown topic_id: {topic_id}"}
        if self._orchestrator is None:
            return {"ok": False, "error": "intel orchestrator unavailable (store missing)"}
        result = await self._orchestrator.run(topic, dry_run=dry_run)
        return result.to_dict()

    def search_documents(
        self,
        *,
        keywords: list[str],
        topic_id: str | None = None,
        top_k: int = 10,
        match: str = "any",
    ) -> dict[str, Any]:
        if self._store is None:
            return {"ok": False, "error": "intel store unavailable"}
        rows = self._store.search(
            keywords=keywords,
            topic_id=topic_id,
            top_k=top_k,
            match=match,
        )
        return {
            "ok": True,
            "keywords": list(keywords),
            "topic_id": topic_id,
            "match": match,
            "count": len(rows),
            "items": rows,
        }

    # ------------------------------------------------------------------ HTTP

    def register_routes(self, router: APIRouter) -> None:
        @router.get("/health")
        async def health() -> dict[str, Any]:
            return {
                "module": self.name,
                "status": "ok",
                "topics": len(self._topics),
                "store_ready": self._store is not None,
                "orchestrator_ready": self._orchestrator is not None,
                "topic_ids": sorted(self._topics),
            }

        @router.get("/digests")
        async def list_digests() -> dict[str, Any]:
            return self.list_digests()

        @router.get("/digests/{topic_id}/latest")
        async def latest(topic_id: str, limit: int = 30) -> dict[str, Any]:
            result = self.latest_digest(topic_id=topic_id, limit=limit)
            if not result.get("ok"):
                raise HTTPException(status_code=404, detail=result.get("error"))
            return result

        @router.post("/digests/{topic_id}/run")
        async def run(topic_id: str, payload: DigestRunRequest) -> dict[str, Any]:
            return await self.run_digest(topic_id=topic_id, dry_run=payload.dry_run)

        @router.post("/search")
        async def search(payload: IntelSearchRequest) -> dict[str, Any]:
            return self.search_documents(
                keywords=payload.keywords,
                topic_id=payload.topic_id,
                top_k=payload.top_k,
                match=payload.match,
            )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run_async(coro) -> Any:
    """Run an async coroutine from sync context.

    AgentRuntime calls patrol handlers synchronously. We bridge with
    ``asyncio.run`` only when there isn't already a running loop; if a
    loop is active (e.g. a test calls ``await module._patrol(...)``)
    fall through to ``asyncio.run_coroutine_threadsafe`` semantics is
    overkill for PR1 — instead document the constraint and raise so
    callers fix the bug rather than hide it.
    """
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if running_loop is not None:
        raise RuntimeError(
            "intel patrol handler invoked from inside an active event loop; "
            "the AgentRuntime is supposed to call this synchronously"
        )
    return asyncio.run(coro)


def get_module() -> IntelModule:
    return IntelModule()
