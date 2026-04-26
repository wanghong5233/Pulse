"""PostgreSQL-backed store for intel documents.

Single source of truth for the ``intel_documents`` table. The 2026-04
refactor abandoned the legacy ``category/title/content`` placeholder
schema in favour of a topic-centric model that mirrors the deterministic
DigestWorkflow:

  topic_id  – which YAML topic this row belongs to
  source_*  – which connector produced it
  canonical_url – stable de-dup key
  score / score_breakdown – LLM rubric output
  promoted_to_archival – heartbeat-time ArchivalMemory promotion flag

Retrieval stays agentic: keyword ILIKE only, no vector index. Embeddings
or full-text search land later if data volume warrants them (see
``docs/architecture.md`` §6).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ...core.storage.engine import DatabaseEngine

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IntelDocumentRecord:
    """In-memory shape of one intel_documents row.

    Used by the pipeline (build → store) and by the search tool (load →
    return). Pydantic isn't required here: the boundary with HTTP / LLM
    happens at the IntentSpec schema layer, not at the storage layer.
    """

    topic_id: str
    source_id: str
    source_type: str
    url: str
    canonical_url: str
    title: str
    content_raw: str
    content_summary: str = ""
    score: float = 0.0
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    published_at: datetime | None = None
    id: str = ""
    collected_at: datetime | None = None
    promoted_to_archival: bool = False

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid.uuid4())
        if self.collected_at is None:
            self.collected_at = datetime.now(timezone.utc)

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "topic_id": self.topic_id,
            "source_id": self.source_id,
            "source_type": self.source_type,
            "url": self.url,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "content_raw": self.content_raw,
            "content_summary": self.content_summary,
            "score": float(self.score),
            "score_breakdown": dict(self.score_breakdown or {}),
            "tags": list(self.tags or []),
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "collected_at": (self.collected_at or datetime.now(timezone.utc)).isoformat(),
            "promoted_to_archival": bool(self.promoted_to_archival),
        }


_REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "topic_id",
        "source_id",
        "source_type",
        "url",
        "canonical_url",
        "title",
        "content_raw",
        "content_summary",
        "score",
        "score_breakdown",
        "tags",
        "published_at",
        "collected_at",
        "promoted_to_archival",
    }
)


class IntelDocumentStore:
    """Thin DAL over ``intel_documents`` — append, search, list-recent.

    The store does not own scoring / dedup logic; that lives in the
    pipeline. Its only contract is "given a list of records, persist
    them idempotently keyed on ``canonical_url``".
    """

    def __init__(self, *, db_engine: DatabaseEngine | None = None) -> None:
        self._db = db_engine or DatabaseEngine()
        self._schema_ready = False

    @property
    def storage_path(self) -> str:
        return "pg://intel_documents"

    def ensure_schema(self) -> None:
        """Create the new schema if absent; fail loud on legacy schema.

        Uses ``CREATE TABLE IF NOT EXISTS`` then verifies the column set
        matches the 2026-04 contract. Refusing to run silently against a
        legacy schema is intentional — the placeholder table used a
        completely different shape and silent ALTERs would lose data.
        """
        if self._schema_ready:
            return
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS intel_documents (
                id UUID PRIMARY KEY,
                topic_id TEXT NOT NULL,
                source_id TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL,
                canonical_url TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '',
                content_raw TEXT NOT NULL DEFAULT '',
                content_summary TEXT NOT NULL DEFAULT '',
                score REAL NOT NULL DEFAULT 0,
                score_breakdown JSONB NOT NULL DEFAULT '{}'::jsonb,
                tags JSONB NOT NULL DEFAULT '[]'::jsonb,
                published_at TIMESTAMPTZ,
                collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                promoted_to_archival BOOLEAN NOT NULL DEFAULT FALSE
            )
            """
        )

        # Validate the column set before touching indexes — if a legacy
        # placeholder table is sitting in the DB, ``CREATE TABLE IF NOT
        # EXISTS`` is a no-op and the index DDL would crash with a noisy
        # ``UndefinedColumn``. Raise the actionable RuntimeError instead.
        rows = self._db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'intel_documents'",
            fetch="all",
        ) or []
        present = {str(row[0]) for row in rows}
        missing = _REQUIRED_COLUMNS - present
        if missing:
            raise RuntimeError(
                "intel_documents schema is incompatible (missing columns: "
                f"{sorted(missing)}). The 2026-04 intel module refactor introduced "
                "a fully new column set; the legacy placeholder table cannot be "
                "migrated automatically. Run `DROP TABLE intel_documents;` and "
                "restart Pulse so the new schema is created."
            )

        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_intel_docs_topic ON intel_documents(topic_id, collected_at DESC)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_intel_docs_score ON intel_documents(score DESC)"
        )
        self._schema_ready = True

    # -- mutations ----------------------------------------------------------

    def append(self, records: list[IntelDocumentRecord]) -> int:
        """Insert / upsert by ``canonical_url``. Returns rows touched."""
        self.ensure_schema()
        if not records:
            return 0

        touched = 0
        for record in records:
            payload = record.to_payload()
            self._db.execute(
                """
                INSERT INTO intel_documents(
                    id, topic_id, source_id, source_type, url, canonical_url,
                    title, content_raw, content_summary, score, score_breakdown,
                    tags, published_at, collected_at, promoted_to_archival
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s::jsonb,
                    %s::jsonb, %s::timestamptz, %s::timestamptz, %s
                )
                ON CONFLICT (canonical_url) DO UPDATE SET
                    title = EXCLUDED.title,
                    content_raw = EXCLUDED.content_raw,
                    content_summary = EXCLUDED.content_summary,
                    score = GREATEST(intel_documents.score, EXCLUDED.score),
                    score_breakdown = EXCLUDED.score_breakdown,
                    tags = EXCLUDED.tags
                """,
                (
                    payload["id"],
                    payload["topic_id"],
                    payload["source_id"],
                    payload["source_type"],
                    payload["url"],
                    payload["canonical_url"],
                    payload["title"],
                    payload["content_raw"],
                    payload["content_summary"],
                    payload["score"],
                    json.dumps(payload["score_breakdown"], ensure_ascii=False),
                    json.dumps(payload["tags"], ensure_ascii=False),
                    payload["published_at"],
                    payload["collected_at"],
                    payload["promoted_to_archival"],
                ),
            )
            touched += 1
        return touched

    def mark_promoted(self, doc_ids: list[str]) -> int:
        self.ensure_schema()
        if not doc_ids:
            return 0
        self._db.execute(
            "UPDATE intel_documents SET promoted_to_archival = TRUE WHERE id = ANY(%s::uuid[])",
            (list(doc_ids),),
        )
        return len(doc_ids)

    # -- reads --------------------------------------------------------------

    def latest_for_topic(self, topic_id: str, *, limit: int = 30) -> list[dict[str, Any]]:
        """Return most recent rows for one topic, newest first."""
        self.ensure_schema()
        safe_limit = max(1, min(int(limit), 200))
        rows = self._db.execute(
            """
            SELECT id, topic_id, source_id, source_type, url, canonical_url,
                   title, content_raw, content_summary, score, score_breakdown,
                   tags, published_at, collected_at, promoted_to_archival
            FROM intel_documents
            WHERE topic_id = %s
            ORDER BY collected_at DESC
            LIMIT %s
            """,
            (str(topic_id), safe_limit),
            fetch="all",
        ) or []
        return [self._row_to_dict(r) for r in rows]

    def list_topics(self) -> list[dict[str, Any]]:
        """Return distinct topics with counts and last-collected timestamp."""
        self.ensure_schema()
        rows = self._db.execute(
            """
            SELECT topic_id,
                   COUNT(*) AS doc_count,
                   MAX(collected_at) AS last_collected_at,
                   MAX(score) AS top_score
            FROM intel_documents
            GROUP BY topic_id
            ORDER BY last_collected_at DESC
            """,
            fetch="all",
        ) or []
        return [
            {
                "topic_id": str(row[0]),
                "doc_count": int(row[1] or 0),
                "last_collected_at": row[2].isoformat() if row[2] else None,
                "top_score": float(row[3] or 0.0),
            }
            for row in rows
        ]

    def search(
        self,
        *,
        keywords: list[str],
        topic_id: str | None = None,
        top_k: int = 10,
        match: str = "any",
    ) -> list[dict[str, Any]]:
        """Agentic keyword ILIKE search over title / content_raw / content_summary.

        Synonym expansion / query rewriting belongs to the caller (Brain).
        The store itself does literal substring match only — fast, debuggable,
        no surprise relevance.
        """
        self.ensure_schema()
        clean_kw = [str(k).strip() for k in keywords if str(k or "").strip()]
        if not clean_kw:
            return []
        safe_top_k = max(1, min(int(top_k), 100))
        joiner = " AND " if str(match).strip().lower() == "all" else " OR "
        clauses: list[str] = []
        params: list[Any] = []
        for kw in clean_kw:
            clauses.append("(title ILIKE %s OR content_raw ILIKE %s OR content_summary ILIKE %s)")
            pattern = f"%{kw}%"
            params.extend([pattern, pattern, pattern])
        where = "(" + joiner.join(clauses) + ")"
        if topic_id:
            where += " AND topic_id = %s"
            params.append(str(topic_id))
        sql = (
            "SELECT id, topic_id, source_id, source_type, url, canonical_url, "
            "title, content_raw, content_summary, score, score_breakdown, tags, "
            "published_at, collected_at, promoted_to_archival "
            f"FROM intel_documents WHERE {where} "
            "ORDER BY score DESC, collected_at DESC LIMIT %s"
        )
        params.append(safe_top_k)
        rows = self._db.execute(sql, tuple(params), fetch="all") or []
        return [self._row_to_dict(r) for r in rows]

    def serendipity_pool(
        self,
        *,
        exclude_topic_id: str,
        limit: int = 1,
        min_score: float = 7.5,
        within_days: int = 14,
    ) -> list[dict[str, Any]]:
        """Pick recent high-scoring rows from *other* topics for cross-topic injection.

        Used by ``DigestWorkflowOrchestrator`` to honour
        ``topic.diversity.serendipity_slots`` — items must come from a
        different ``topic_id`` so the digest deliberately surfaces
        content the current subscription would not normally include.
        """
        self.ensure_schema()
        safe_limit = max(0, min(int(limit), 10))
        if safe_limit == 0:
            return []
        safe_score = max(0.0, min(float(min_score), 10.0))
        safe_window = max(1, min(int(within_days), 365))
        rows = self._db.execute(
            """
            SELECT id, topic_id, source_id, source_type, url, canonical_url,
                   title, content_raw, content_summary, score, score_breakdown,
                   tags, published_at, collected_at, promoted_to_archival
            FROM intel_documents
            WHERE topic_id <> %s
              AND score >= %s
              AND collected_at >= NOW() - (%s || ' days')::interval
            ORDER BY score DESC, collected_at DESC
            LIMIT %s
            """,
            (str(exclude_topic_id), safe_score, str(safe_window), safe_limit),
            fetch="all",
        ) or []
        return [self._row_to_dict(r) for r in rows]

    def existing_canonical_urls(self, urls: list[str]) -> set[str]:
        """Bulk de-dup helper used by the pipeline before INSERT."""
        self.ensure_schema()
        clean = [str(u).strip() for u in urls if str(u or "").strip()]
        if not clean:
            return set()
        rows = self._db.execute(
            "SELECT canonical_url FROM intel_documents WHERE canonical_url = ANY(%s)",
            (clean,),
            fetch="all",
        ) or []
        return {str(row[0]) for row in rows}

    def total_count(self) -> int:
        self.ensure_schema()
        row = self._db.execute(
            "SELECT COUNT(*) FROM intel_documents",
            fetch="one",
        )
        return int(row[0]) if row else 0

    @staticmethod
    def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
        score_breakdown = row[10]
        if isinstance(score_breakdown, str):
            try:
                score_breakdown = json.loads(score_breakdown)
            except json.JSONDecodeError:
                score_breakdown = {}
        tags = row[11]
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except json.JSONDecodeError:
                tags = []
        return {
            "id": str(row[0]),
            "topic_id": str(row[1] or ""),
            "source_id": str(row[2] or ""),
            "source_type": str(row[3] or ""),
            "url": str(row[4] or ""),
            "canonical_url": str(row[5] or ""),
            "title": str(row[6] or ""),
            "content_raw": str(row[7] or ""),
            "content_summary": str(row[8] or ""),
            "score": float(row[9] or 0.0),
            "score_breakdown": dict(score_breakdown) if isinstance(score_breakdown, dict) else {},
            "tags": list(tags) if isinstance(tags, list) else [],
            "published_at": row[12].isoformat() if row[12] else None,
            "collected_at": row[13].isoformat() if row[13] else None,
            "promoted_to_archival": bool(row[14]),
        }
