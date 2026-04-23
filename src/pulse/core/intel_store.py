"""Intel knowledge store —— PG-backed，agentic search 检索。

Intel 是 Pulse 用来沉淀"领域知识片段"的存储（例如岗位情报、趋势摘要等）。
它的检索路径是 **agentic search**：上层生成关键词或调用 `search_keyword`，
内核不自带向量语义召回。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .storage.engine import DatabaseEngine


class IntelKnowledgeStore:
    """PostgreSQL-backed intel knowledge store (agentic search, no vector)."""

    def __init__(
        self,
        *,
        storage_path: str | None = None,
        db_engine: DatabaseEngine | None = None,
    ) -> None:
        _ = storage_path
        self._db = db_engine or DatabaseEngine()
        self._ensure_schema()

    @property
    def storage_path(self) -> str:
        return "pg://intel_documents"

    def _ensure_schema(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS intel_documents (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'unknown',
                tags JSONB NOT NULL DEFAULT '[]'::jsonb,
                collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_intel_docs_category ON intel_documents(category)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_intel_docs_collected ON intel_documents(collected_at)"
        )

    def append(self, rows: list[dict[str, Any]]) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        inserted = 0

        for row in rows:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            content = str(row.get("content") or "").strip()
            category = str(row.get("category") or "").strip().lower()
            if not title or not content or not category:
                continue

            tags_raw = row.get("tags")
            tags = [str(item).strip() for item in list(tags_raw or []) if str(item).strip()]
            doc_id = str(row.get("id") or uuid.uuid4().hex)
            summary = str(row.get("summary") or "").strip()
            source_url = str(row.get("source_url") or "").strip()
            source = str(row.get("source") or "").strip() or "unknown"
            collected_at = str(row.get("collected_at") or now_iso)
            metadata = dict(row.get("metadata") or {})

            self._db.execute(
                """
                INSERT INTO intel_documents(id, category, title, content, summary, source_url, source, tags, collected_at, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::timestamptz, %s::jsonb)
                ON CONFLICT (id) DO UPDATE SET
                    content = EXCLUDED.content,
                    summary = EXCLUDED.summary,
                    metadata = EXCLUDED.metadata
                """,
                (
                    doc_id, category, title, content, summary, source_url, source,
                    json.dumps(tags, ensure_ascii=False),
                    collected_at,
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
            inserted += 1
        return inserted

    def recent(self, *, limit: int = 5000, category: str | None = None) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 20000))
        safe_category = str(category or "").strip().lower()

        if safe_category:
            rows = self._db.execute(
                "SELECT id, category, title, content, summary, source_url, source, tags, collected_at, metadata "
                "FROM intel_documents WHERE category = %s ORDER BY collected_at DESC LIMIT %s",
                (safe_category, safe_limit),
                fetch="all",
            )
        else:
            rows = self._db.execute(
                "SELECT id, category, title, content, summary, source_url, source, tags, collected_at, metadata "
                "FROM intel_documents ORDER BY collected_at DESC LIMIT %s",
                (safe_limit,),
                fetch="all",
            )
        if not rows:
            return []
        return [self._row_to_dict(r) for r in rows]

    def search(
        self,
        *,
        query: str | None = None,
        keywords: list[str] | None = None,
        top_k: int = 10,
        category: str | None = None,
        match: str = "any",
    ) -> list[dict[str, Any]]:
        """Agentic keyword search over title / content / summary.

        支持两种入参（兼容历史调用方）：
        - `query`: 单个查询串（当成一个 keyword）
        - `keywords`: 多关键词列表，match="any"|"all"

        多关键词同义词展开由上层 (Brain / Domain) 负责。
        """
        kw_list: list[str] = []
        if keywords:
            kw_list = [str(k).strip() for k in keywords if str(k or "").strip()]
        elif query and str(query).strip():
            kw_list = [str(query).strip()]
        if not kw_list:
            return []

        safe_top_k = max(1, min(int(top_k), 100))
        safe_category = str(category or "").strip().lower()
        match_mode = "all" if str(match).strip().lower() == "all" else "any"
        joiner = " AND " if match_mode == "all" else " OR "

        where: list[str] = []
        params: list[Any] = []

        kw_clauses: list[str] = []
        for kw in kw_list:
            kw_clauses.append("(title ILIKE %s OR content ILIKE %s OR summary ILIKE %s)")
            pattern = f"%{kw}%"
            params.extend([pattern, pattern, pattern])
        where.append("(" + joiner.join(kw_clauses) + ")")

        if safe_category:
            where.append("category = %s")
            params.append(safe_category)

        sql = (
            "SELECT id, category, title, content, summary, source_url, source, tags, collected_at, metadata "
            "FROM intel_documents WHERE " + " AND ".join(where)
            + " ORDER BY collected_at DESC LIMIT %s"
        )
        params.append(safe_top_k)
        rows = self._db.execute(sql, tuple(params), fetch="all")
        if not rows:
            return []
        return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
        tags = row[7]
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except Exception:
                tags = []
        metadata = row[9]
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        return {
            "id": str(row[0]),
            "category": str(row[1] or ""),
            "title": str(row[2] or ""),
            "content": str(row[3] or ""),
            "summary": str(row[4] or ""),
            "source_url": str(row[5] or ""),
            "source": str(row[6] or "unknown"),
            "tags": list(tags) if isinstance(tags, list) else [],
            "collected_at": str(row[8] or ""),
            "metadata": dict(metadata) if isinstance(metadata, dict) else {},
        }
