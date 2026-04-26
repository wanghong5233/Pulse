"""DigestWorkflow orchestrator — wires the six stages with structured events.

The orchestrator is the *only* place where stages are composed; every
caller (patrol, HTTP route, manual run) goes through
:meth:`DigestWorkflowOrchestrator.run`. That keeps the contract uniform
and makes audit replay trivial — one trace_id per run, six stage events
per topic.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ....core.llm.router import LLMRouter
from ....core.memory.archival_memory import ArchivalMemory
from ....core.notify.notifier import Notifier
from ..sources import RawItem, SourceFetchResult
from ..store import IntelDocumentStore
from ..topics import TopicConfig
from .dedup import dedup_items
from .diversify import diversify
from .fetch import fetch_all_sources, flatten_items
from .publish import DigestPublishResult, publish_digest
from .score import LLMScorer, ScoredItem, score_items
from .summarize import LLMSummarizer, SummarizedItem, summarize_items

logger = logging.getLogger(__name__)

try:  # db extra is optional for import-only environments.
    from psycopg import Error as PsycopgError
except ImportError:  # pragma: no cover - exercised only without the db extra
    PsycopgError = RuntimeError  # type: ignore[misc, assignment]

StageEventEmitter = Callable[[str, str, dict[str, Any]], None]
"""``emit(stage, status, payload)``; orchestrator never raises if it's None."""


@dataclass(slots=True)
class WorkflowResult:
    topic_id: str
    fetched: int
    deduped: int
    above_threshold: int
    published: int
    promoted: int
    elapsed_ms: int
    publish: DigestPublishResult | None = None
    errors: list[str] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "topic_id": self.topic_id,
            "fetched": self.fetched,
            "deduped": self.deduped,
            "above_threshold": self.above_threshold,
            "published": self.published,
            "promoted": self.promoted,
            "promoted_to_archival": (
                len(self.publish.promoted_facts) if self.publish else 0
            ),
            "elapsed_ms": self.elapsed_ms,
            "errors": list(self.errors),
            "items": list(self.items),
            "delivery": (self.publish.delivery if self.publish else {}),
        }


class DigestWorkflowOrchestrator:
    """Pure composition layer; owns no state across runs."""

    def __init__(
        self,
        *,
        store: IntelDocumentStore,
        llm_router: LLMRouter,
        notifier: Notifier,
        archival_memory: ArchivalMemory | None = None,
        emit_stage_event: StageEventEmitter | None = None,
        score_route: str = "classification",
        summary_route: str = "generation",
    ) -> None:
        self._store = store
        self._scorer = LLMScorer(llm_router, route=score_route)
        self._summarizer = LLMSummarizer(llm_router, route=summary_route)
        self._notifier = notifier
        self._archival_memory = archival_memory
        self._emit = emit_stage_event

    async def run(
        self,
        topic: TopicConfig,
        *,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> WorkflowResult:
        clock = time.monotonic()
        run_trace = str(trace_id or "").strip() or None
        errors: list[str] = []

        self._fire("workflow", "started", run_trace, {"topic_id": topic.id})

        # ---------- Stage 1: fetch ---------------------------------------
        fetch_results: list[SourceFetchResult] = await fetch_all_sources(topic)
        for r in fetch_results:
            if r.error:
                errors.append(f"{r.source_id}: {r.error}")
        raw_items: list[RawItem] = flatten_items(fetch_results)
        self._fire(
            "fetch",
            "completed",
            run_trace,
            {
                "topic_id": topic.id,
                "sources_total": len(fetch_results),
                "sources_failed": sum(1 for r in fetch_results if r.error),
                "items_total": len(raw_items),
            },
        )

        # ---------- Stage 2: dedup ---------------------------------------
        already_seen = self._store.existing_canonical_urls([r.url for r in raw_items])
        unique_items, canonical_urls = dedup_items(
            raw_items,
            seen_canonical_urls=already_seen,
        )
        self._fire(
            "dedup",
            "completed",
            run_trace,
            {
                "topic_id": topic.id,
                "items_before": len(raw_items),
                "items_after": len(unique_items),
                "items_already_known": len(raw_items) - len(unique_items),
            },
        )

        # ---------- Stage 3: score ---------------------------------------
        scored: list[ScoredItem] = await score_items(
            topic=topic,
            items=unique_items,
            scorer=self._scorer,
        )
        threshold = topic.scoring.threshold
        passing_pairs: list[tuple[ScoredItem, str]] = [
            (s, canonical_urls[idx])
            for idx, s in enumerate(scored)
            if s.score >= threshold
        ]
        self._fire(
            "score",
            "completed",
            run_trace,
            {
                "topic_id": topic.id,
                "scored": len(scored),
                "above_threshold": len(passing_pairs),
                "threshold": threshold,
            },
        )

        if not passing_pairs:
            elapsed_ms = int((time.monotonic() - clock) * 1000)
            self._fire(
                "workflow",
                "completed",
                run_trace,
                {
                    "topic_id": topic.id,
                    "above_threshold": 0,
                    "elapsed_ms": elapsed_ms,
                },
            )
            return WorkflowResult(
                topic_id=topic.id,
                fetched=len(raw_items),
                deduped=len(unique_items),
                above_threshold=0,
                published=0,
                promoted=0,
                elapsed_ms=elapsed_ms,
                errors=errors,
            )

        passing_scored = [s for s, _ in passing_pairs]
        passing_canonical = [c for _, c in passing_pairs]

        # ---------- Stage 4: summarize -----------------------------------
        summarized: list[SummarizedItem] = await summarize_items(
            topic=topic,
            scored_items=passing_scored,
            summarizer=self._summarizer,
        )
        self._fire(
            "summarize",
            "completed",
            run_trace,
            {"topic_id": topic.id, "summarized": len(summarized)},
        )

        # ---------- Stage 5: diversify -----------------------------------
        # Re-pair (summarized, canonical) so quota-driven re-ordering keeps
        # both lists aligned for publish.
        pair_index = {id(s): canonical for s, canonical in zip(passing_scored, passing_canonical, strict=True)}

        def _canonical_for(s: SummarizedItem) -> str:
            return pair_index[id(s.scored)]

        diversified: list[SummarizedItem] = diversify(
            topic=topic,
            items=summarized,
        )
        diversified_canonicals = [_canonical_for(s) for s in diversified]

        serendipity_records = self._pull_serendipity(topic, diversified_canonicals)
        self._fire(
            "diversify",
            "completed",
            run_trace,
            {
                "topic_id": topic.id,
                "items_before": len(summarized),
                "items_after": len(diversified),
                "serendipity_picked": len(serendipity_records),
            },
        )

        # ---------- Stage 6: publish -------------------------------------
        publish_result: DigestPublishResult = publish_digest(
            topic=topic,
            items=diversified,
            canonical_urls=diversified_canonicals,
            store=self._store,
            notifier=self._notifier,
            serendipity=serendipity_records,
            archival_memory=self._archival_memory,
            dry_run=dry_run,
        )
        self._fire(
            "publish",
            "completed",
            run_trace,
            {
                "topic_id": topic.id,
                "inserted": publish_result.inserted,
                "promoted": len(publish_result.promoted_ids),
                "promoted_to_archival": len(publish_result.promoted_facts),
                "channel": publish_result.delivery.get("channel"),
                "delivered": publish_result.delivery.get("delivered"),
            },
        )

        elapsed_ms = int((time.monotonic() - clock) * 1000)
        self._fire(
            "workflow",
            "completed",
            run_trace,
            {
                "topic_id": topic.id,
                "above_threshold": len(passing_pairs),
                "published": publish_result.inserted,
                "elapsed_ms": elapsed_ms,
            },
        )
        return WorkflowResult(
            topic_id=topic.id,
            fetched=len(raw_items),
            deduped=len(unique_items),
            above_threshold=len(passing_pairs),
            published=publish_result.inserted,
            promoted=len(publish_result.promoted_ids),
            elapsed_ms=elapsed_ms,
            publish=publish_result,
            errors=errors,
            items=publish_result.items,
        )

    # ------------------------------------------------------------------ helpers

    def _pull_serendipity(
        self,
        topic: TopicConfig,
        already_picked_canonicals: Sequence[str],
    ) -> list[dict[str, Any]]:
        """Cross-topic serendipity pool: pick high-score recent rows from *other* topics.

        The store query is the only IO; we filter out canonicals that
        are already in the current digest to avoid duplicates if the
        same article got picked up under two topics.
        """
        slots = max(0, int(topic.diversity.serendipity_slots))
        if slots == 0:
            return []
        try:
            pool = self._store.serendipity_pool(
                exclude_topic_id=topic.id,
                limit=slots * 3,
                min_score=max(7.0, topic.scoring.threshold),
            )
        except (RuntimeError, PsycopgError):
            logger.exception(
                "intel orchestrator serendipity_pool query failed topic=%s",
                topic.id,
            )
            return []
        already = set(already_picked_canonicals)
        deduped = [row for row in pool if row.get("canonical_url") not in already]
        return deduped[:slots]

    def _fire(
        self,
        stage: str,
        status: str,
        trace_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        if self._emit is None:
            return
        try:
            payload_with_trace = dict(payload)
            if trace_id:
                payload_with_trace.setdefault("trace_id", trace_id)
            self._emit(stage, status, payload_with_trace)
        except RuntimeError:  # pragma: no cover — emit must never break the workflow
            logger.exception(
                "intel orchestrator emit failed stage=%s status=%s",
                stage,
                status,
            )


# ---- backwards-compat re-exports for callers expecting flat names --------

DigestPublishResult = DigestPublishResult  # type: ignore[assignment]
