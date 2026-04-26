"""Stage 6 — persistence + delivery.

Three side effects, in this order:

  1. Build :class:`IntelDocumentRecord` rows and append to the store
     (idempotent on ``canonical_url``).
  2. Render a Chinese digest as Markdown for the notifier content.
  3. Push to the configured channel via the injected :class:`Notifier`,
     unless ``dry_run=True``.

The Notifier abstraction owns channel formatting (Feishu card vs
console vs others); the publish stage does not talk to the webhook
directly. That keeps multi-channel routing and webhook URL secrets
out of the module.

ArchivalMemory promotion (PR3) plugs in here: any item whose final
``score >= topic.memory.promote_threshold`` gets ``promoted_to_archival``
flipped after store insert. PR1 records the flag but does not actually
write to MemoryRuntime.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from ....core.memory.archival_memory import ArchivalMemory
from ....core.notify.notifier import Notification, Notifier
from ..store import IntelDocumentRecord, IntelDocumentStore
from ..topics import TopicConfig
from .summarize import SummarizedItem

logger = logging.getLogger(__name__)

try:  # db extra is optional for import-only environments.
    from psycopg import Error as PsycopgError
except ImportError:  # pragma: no cover - exercised only without the db extra
    PsycopgError = RuntimeError  # type: ignore[misc, assignment]


@dataclass(slots=True)
class DigestPublishResult:
    topic_id: str
    inserted: int
    promoted_ids: list[str]
    text: str
    delivery: dict[str, Any] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    promoted_facts: list[dict[str, Any]] = field(default_factory=list)


def build_records(
    *,
    topic: TopicConfig,
    items: Sequence[SummarizedItem],
    canonical_urls: Sequence[str],
) -> list[IntelDocumentRecord]:
    """Project ``SummarizedItem`` (+ canonical URL list) into store rows.

    ``canonical_urls`` must be aligned with ``items`` 1:1 — the dedup
    stage produced both in lockstep, so the orchestrator just passes
    them through here.
    """
    if len(canonical_urls) != len(items):
        raise ValueError(
            f"build_records contract violation: {len(items)} items vs "
            f"{len(canonical_urls)} canonical_urls"
        )
    out: list[IntelDocumentRecord] = []
    promote_threshold = topic.memory.promote_threshold
    for summarized, canonical in zip(items, canonical_urls, strict=True):
        scored = summarized.scored
        item = scored.item
        promoted = scored.score >= promote_threshold
        out.append(
            IntelDocumentRecord(
                topic_id=topic.id,
                source_id=item.source_id,
                source_type=item.source_type,
                url=item.url,
                canonical_url=canonical,
                title=item.title,
                content_raw=item.content_raw,
                content_summary=summarized.summary,
                score=float(scored.score),
                score_breakdown=dict(scored.score_breakdown),
                tags=list(scored.score_breakdown.get("tags", [])),
                published_at=item.published_at,
                promoted_to_archival=promoted,
            )
        )
    return out


def build_digest_text(
    *,
    topic: TopicConfig,
    items: Sequence[SummarizedItem],
    serendipity: Sequence[dict[str, Any]] | None = None,
) -> str:
    """Render a plain-text digest for IM consumption.

    ``serendipity`` is an optional list of stored ``intel_documents``
    rows from *other* topics — rendered in a separate section so
    readers can see at a glance that the recommendation crosses their
    declared subscription.
    """
    if not items and not serendipity:
        return f"# {topic.display_name}\n\n(本期暂无符合阈值的条目)"
    lines = [f"# {topic.display_name}", ""]
    if items:
        for idx, summarized in enumerate(items, start=1):
            score = summarized.scored.score
            item = summarized.scored.item
            lines.append(f"{idx}. **{item.title}** · score {score:.1f} · {item.source_id}")
            if summarized.summary:
                lines.append(f"   {summarized.summary}")
            lines.append(f"   {item.url}")
            lines.append("")
    if serendipity:
        lines.append("---")
        lines.append("📡 跨主题彩蛋（来自其它订阅）")
        lines.append("")
        for idx, row in enumerate(serendipity, start=1):
            title = str(row.get("title") or "").strip() or "(no title)"
            score = float(row.get("score") or 0.0)
            cross_topic = str(row.get("topic_id") or "")
            source = str(row.get("source_id") or "")
            url = str(row.get("url") or "")
            lines.append(
                f"{idx}. **{title}** · score {score:.1f} · {source} · ↳{cross_topic}"
            )
            summary = str(row.get("content_summary") or "").strip()
            if summary:
                lines.append(f"   {summary}")
            if url:
                lines.append(f"   {url}")
            lines.append("")
    return "\n".join(lines).strip()


def publish_digest(
    *,
    topic: TopicConfig,
    items: Sequence[SummarizedItem],
    canonical_urls: Sequence[str],
    store: IntelDocumentStore,
    notifier: Notifier,
    serendipity: Sequence[dict[str, Any]] | None = None,
    archival_memory: ArchivalMemory | None = None,
    dry_run: bool = False,
) -> DigestPublishResult:
    records = build_records(topic=topic, items=items, canonical_urls=canonical_urls)
    inserted = store.append(records)

    promoted_ids = [r.id for r in records if r.promoted_to_archival]
    promoted_facts: list[dict[str, Any]] = []
    if promoted_ids and not dry_run:
        store.mark_promoted(promoted_ids)
        if archival_memory is not None:
            promoted_facts = _promote_to_archival(
                topic=topic,
                records=[r for r in records if r.promoted_to_archival],
                archival_memory=archival_memory,
            )

    text = build_digest_text(topic=topic, items=items, serendipity=serendipity)
    delivery: dict[str, Any] = {
        "channel": topic.publish.channel,
        "skipped": dry_run,
        "delivered": False,
        "error": None,
        "serendipity_count": len(serendipity or []),
    }
    if not dry_run and (items or serendipity):
        try:
            notifier.send(
                Notification(
                    level="info",
                    title=f"Pulse · {topic.display_name} 日报",
                    content=text,
                    metadata={
                        "topic_id": topic.id,
                        "channel": topic.publish.channel,
                        "item_count": len(items),
                        "serendipity_count": len(serendipity or []),
                    },
                )
            )
            delivery["delivered"] = True
        except RuntimeError as exc:
            logger.warning(
                "intel publish notifier error topic=%s err=%s",
                topic.id,
                exc,
            )
            delivery["error"] = str(exc)[:200]
    return DigestPublishResult(
        topic_id=topic.id,
        inserted=inserted,
        promoted_ids=promoted_ids,
        text=text,
        delivery=delivery,
        items=[
            {
                "title": s.title,
                "url": s.url,
                "summary": s.summary,
                "score": s.scored.score,
                "source_id": s.source_id,
            }
            for s in items
        ],
        promoted_facts=promoted_facts,
    )


def _promote_to_archival(
    *,
    topic: TopicConfig,
    records: Sequence[IntelDocumentRecord],
    archival_memory: ArchivalMemory,
) -> list[dict[str, Any]]:
    """Add one ``facts`` row per high-score intel record.

    Schema: ``subject = "intel:" + topic_id``,
    ``predicate = "high_score_signal"``,
    ``object = {title, url, score, summary, source_id}``.
    Failures on individual rows log + continue — promotion must never
    block publish.
    """
    out: list[dict[str, Any]] = []
    for record in records:
        try:
            object_value = {
                "title": record.title,
                "url": record.url,
                "summary": record.content_summary,
                "score": record.score,
                "source_id": record.source_id,
                "source_type": record.source_type,
                "tags": list(record.tags),
            }
            fact = archival_memory.add_fact(
                subject=f"intel:{topic.id}",
                predicate="high_score_signal",
                object_value=object_value,
                source=f"intel:{record.source_id}",
                confidence=max(0.0, min(record.score / 10.0, 1.0)),
                metadata={
                    "topic_id": topic.id,
                    "intel_doc_id": record.id,
                    "canonical_url": record.canonical_url,
                },
                evidence_refs=[f"intel_documents:{record.id}"],
                promoted_from=f"intel_documents:{record.id}",
                promotion_reason=(
                    f"score {record.score:.2f} ≥ promote_threshold "
                    f"{topic.memory.promote_threshold:.2f}"
                ),
            )
        except (ValueError, RuntimeError, PsycopgError) as exc:
            logger.warning(
                "intel archival promotion failed topic=%s doc=%s err=%s",
                topic.id,
                record.id,
                exc,
            )
            continue
        out.append(
            {
                "fact_id": fact.get("id"),
                "intel_doc_id": record.id,
                "score": record.score,
            }
        )
    return out
