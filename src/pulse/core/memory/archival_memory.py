"""Archival memory —— 长期结构化事实存储。

检索路径为 **agentic search**：
- `recent(...)`: 时间序最近 N 条；
- `query(subject=, predicate=)`: 按 SPO 精确字段过滤；
- `search_keyword(keywords=...)`: SQL ILIKE 跨 subject/predicate/object 关键词匹配。

内核不自带向量语义召回，参见
`docs/Pulse-MemoryRuntime设计.md` 附录 B: Retrieval 策略抉择。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from ..storage.engine import DatabaseEngine
from .envelope import MemoryEnvelope

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_metadata(raw: Any) -> dict[str, Any]:
    """DB metadata 列 → dict. 解析失败 (schema 漂移) 记 debug, 不 raise."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.debug(
                "archival_memory: metadata JSON decode failed (skipping): %s; raw=%r",
                exc, text[:120],
            )
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _normalize_object_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False)


class ArchivalMemory:
    """PostgreSQL-backed archival memory (agentic search, no vector)."""

    def __init__(
        self,
        *,
        storage_path: str | None = None,
        db_engine: DatabaseEngine | None = None,
    ) -> None:
        _ = storage_path
        self._db = db_engine or DatabaseEngine()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS facts (
                id BIGSERIAL PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                "object" TEXT NOT NULL,
                object_json JSONB,
                metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                valid_to TIMESTAMPTZ,
                confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                source TEXT,
                superseded_by BIGINT REFERENCES facts(id),
                evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
                promoted_from TEXT,
                promotion_reason TEXT,
                task_id TEXT,
                run_id TEXT,
                workspace_id TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        self._migrate_add_columns()
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_facts_subject_predicate ON facts(subject, predicate)")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_facts_created_at ON facts(created_at DESC)")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_facts_valid_from ON facts(valid_from DESC)")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_facts_task_id ON facts(task_id)")

    def _migrate_add_columns(self) -> None:
        """Idempotent migration: add P0 columns to existing facts table."""
        for col, col_type in [
            ("evidence_refs", "JSONB NOT NULL DEFAULT '[]'::jsonb"),
            ("promoted_from", "TEXT"),
            ("promotion_reason", "TEXT"),
            ("task_id", "TEXT"),
            ("run_id", "TEXT"),
            ("workspace_id", "TEXT"),
        ]:
            self._db.execute(
                f"ALTER TABLE facts ADD COLUMN IF NOT EXISTS {col} {col_type}"  # noqa: S608
            )

    def add_fact(
        self,
        *,
        subject: str,
        predicate: str,
        object_value: Any,
        source: str,
        confidence: float = 0.8,
        metadata: dict[str, Any] | None = None,
        evidence_refs: list[str] | None = None,
        promoted_from: str | None = None,
        promotion_reason: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        safe_subject = str(subject or "").strip()
        safe_predicate = str(predicate or "").strip()
        if not safe_subject or not safe_predicate:
            raise ValueError("subject and predicate are required")
        safe_source = str(source or "").strip()
        safe_confidence = max(0.0, min(float(confidence), 1.0))
        timestamp = _utc_now_iso()
        metadata_json = dict(metadata or {})
        object_text = _normalize_object_text(object_value)
        safe_evidence = list(evidence_refs or [])
        row = self._db.execute(
            """
            INSERT INTO facts(
                subject, predicate, "object", object_json, metadata_json, valid_from,
                confidence, source, evidence_refs, promoted_from, promotion_reason,
                task_id, run_id, workspace_id
            )
            VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::timestamptz,
                    %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
            RETURNING id, valid_from
            """,
            (
                safe_subject,
                safe_predicate,
                object_text,
                json.dumps(object_value, ensure_ascii=False),
                json.dumps(metadata_json, ensure_ascii=False),
                timestamp,
                safe_confidence,
                safe_source,
                json.dumps(safe_evidence),
                promoted_from,
                promotion_reason,
                task_id,
                run_id,
                workspace_id,
            ),
            fetch="one",
        )
        if not row:
            raise RuntimeError("failed to insert fact")
        fact_id = int(row[0])
        valid_from = row[1]
        valid_from_text = str(valid_from.isoformat() if hasattr(valid_from, "isoformat") else valid_from or timestamp)
        return {
            "id": fact_id,
            "timestamp": valid_from_text,
            "subject": safe_subject,
            "predicate": safe_predicate,
            "object": object_value,
            "source": safe_source,
            "confidence": safe_confidence,
            "metadata": metadata_json,
        }

    def recent(self, *, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        rows = self._db.execute(
            """
            SELECT id, subject, predicate, object, source, confidence, metadata_json, valid_from
            FROM facts
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (safe_limit,),
            fetch="all",
        ) or []
        return [
            {
                "id": int(fact_id),
                "timestamp": str(valid_from.isoformat() if hasattr(valid_from, "isoformat") else valid_from or ""),
                "subject": str(subject or ""),
                "predicate": str(predicate or ""),
                "object": obj,
                "source": str(source or ""),
                "confidence": float(confidence or 0.0),
                "metadata": _parse_metadata(metadata_raw),
            }
            for fact_id, subject, predicate, obj, source, confidence, metadata_raw, valid_from in rows
        ]

    def query(
        self,
        *,
        subject: str | None = None,
        predicate: str | None = None,
        keyword: str | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """按 subject/predicate 精确过滤 + 可选 keyword ILIKE 匹配。

        keyword 会对 subject/predicate/object 三个字段做 OR ILIKE 匹配。
        多关键词分词、同义词展开由上层 (Brain / Domain) 负责。
        """
        safe_subject = str(subject or "").strip()
        safe_predicate = str(predicate or "").strip()
        safe_keyword = str(keyword or "").strip()
        safe_limit = max(1, min(int(limit), 300))

        where: list[str] = ["1=1"]
        params: list[Any] = []

        if safe_subject:
            where.append("subject = %s")
            params.append(safe_subject)
        if safe_predicate:
            where.append("predicate = %s")
            params.append(safe_predicate)
        if safe_keyword:
            where.append('(subject ILIKE %s OR predicate ILIKE %s OR "object" ILIKE %s)')
            pattern = f"%{safe_keyword}%"
            params.extend([pattern, pattern, pattern])

        sql = (
            "SELECT id, subject, predicate, object, source, confidence, metadata_json, valid_from "
            "FROM facts WHERE " + " AND ".join(where) + " ORDER BY created_at DESC LIMIT %s"
        )
        params.append(safe_limit)
        rows = self._db.execute(sql, tuple(params), fetch="all") or []
        return [
            {
                "id": int(fact_id),
                "timestamp": str(valid_from.isoformat() if hasattr(valid_from, "isoformat") else valid_from or ""),
                "subject": str(subject_value or ""),
                "predicate": str(predicate_value or ""),
                "object": obj,
                "source": str(source_value or ""),
                "confidence": float(confidence_value or 0.0),
                "metadata": _parse_metadata(metadata_raw),
            }
            for (
                fact_id,
                subject_value,
                predicate_value,
                obj,
                source_value,
                confidence_value,
                metadata_raw,
                valid_from,
            ) in rows
        ]

    def search_keyword(
        self,
        *,
        keywords: list[str] | str,
        top_k: int = 20,
        subject: str | None = None,
        predicate: str | None = None,
        match: str = "any",
    ) -> list[dict[str, Any]]:
        """Agentic search：跨 subject/predicate/object 做多关键词 ILIKE。

        match="any" (默认): 任一关键词命中；"all" 所有关键词都命中。
        """
        if isinstance(keywords, str):
            kw_list = [keywords]
        else:
            kw_list = [str(k).strip() for k in keywords if str(k or "").strip()]
        if not kw_list:
            return []

        safe_top_k = max(1, min(int(top_k), 200))
        match_mode = "all" if str(match).strip().lower() == "all" else "any"
        joiner = " AND " if match_mode == "all" else " OR "

        where: list[str] = []
        params: list[Any] = []

        kw_clauses: list[str] = []
        for kw in kw_list:
            kw_clauses.append('(subject ILIKE %s OR predicate ILIKE %s OR "object" ILIKE %s)')
            pattern = f"%{kw}%"
            params.extend([pattern, pattern, pattern])
        where.append("(" + joiner.join(kw_clauses) + ")")

        if str(subject or "").strip():
            where.append("subject = %s")
            params.append(str(subject).strip())
        if str(predicate or "").strip():
            where.append("predicate = %s")
            params.append(str(predicate).strip())

        sql = (
            "SELECT id, subject, predicate, object, source, confidence, metadata_json, valid_from "
            "FROM facts WHERE " + " AND ".join(where) + " ORDER BY created_at DESC LIMIT %s"
        )
        params.append(safe_top_k)
        rows = self._db.execute(sql, tuple(params), fetch="all") or []
        return [
            {
                "id": int(fact_id),
                "timestamp": str(valid_from.isoformat() if hasattr(valid_from, "isoformat") else valid_from or ""),
                "subject": str(subj_value or ""),
                "predicate": str(pred_value or ""),
                "object": obj,
                "source": str(source_value or ""),
                "confidence": float(conf_value or 0.0),
                "metadata": _parse_metadata(metadata_raw),
            }
            for (
                fact_id,
                subj_value,
                pred_value,
                obj,
                source_value,
                conf_value,
                metadata_raw,
                valid_from,
            ) in rows
        ]

    def count(self) -> int:
        row = self._db.execute("SELECT COUNT(1) FROM facts", fetch="one")
        if not row:
            return 0
        return int(row[0] or 0)

    # -- Envelope-based write -----------------------------------------------

    def store_envelope(self, envelope: MemoryEnvelope) -> dict[str, Any]:
        """Write a MemoryEnvelope to archival storage.

        Expects envelope.kind == MemoryKind.fact with content containing
        subject/predicate/object keys.
        """
        c = envelope.content
        return self.add_fact(
            subject=str(c.get("subject", "")),
            predicate=str(c.get("predicate", "")),
            object_value=c.get("object", ""),
            source=envelope.source or "envelope",
            confidence=envelope.confidence,
            metadata={"envelope_id": envelope.memory_id, "scope": envelope.scope.value},
            evidence_refs=envelope.evidence_refs,
            promoted_from=envelope.promoted_from,
            promotion_reason=envelope.promotion_reason,
            task_id=envelope.task_id or None,
            run_id=envelope.run_id or None,
            workspace_id=envelope.workspace_id or None,
        )

    def supersede_fact(self, *, old_fact_id: str | int, new_fact_id: str | int) -> bool:
        """标记旧 fact 被新 fact 取代 (§9.4 Step 5)。

        设置 old_fact 的 superseded_by 字段和 valid_to 时间戳。
        """
        self._db.execute(
            """
            UPDATE facts
            SET superseded_by = %s,
                valid_to = NOW()
            WHERE id = %s AND superseded_by IS NULL
            """,
            (int(new_fact_id) if str(new_fact_id).isdigit() else None, int(old_fact_id)),
        )
        return True
