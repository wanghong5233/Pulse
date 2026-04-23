from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ....core.intel_store import IntelKnowledgeStore
from ....core.module import BaseModule


class IntelSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    top_k: int = Field(default=5, ge=1, le=20)
    category: str | None = Field(default=None, max_length=30)


class IntelIngestItem(BaseModel):
    category: str = Field(..., min_length=1, max_length=40)
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=2000)
    summary: str | None = Field(default=None, max_length=800)
    source_url: str | None = Field(default=None, max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=30)


class IntelIngestRequest(BaseModel):
    items: list[IntelIngestItem] = Field(default_factory=list, max_length=200)
    source: str = Field(default="manual", max_length=40)


def _normalize(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _extract_query(text: str, *, fallback: str = "agent") -> str:
    raw = str(text or "").strip()
    lowered = raw.lower()
    prefix = "/intel query"
    if lowered.startswith(prefix):
        candidate = raw[len(prefix) :].strip()
        if candidate:
            return candidate
    return raw or fallback


class IntelQueryModule(BaseModule):
    name = "intel_query"
    description = "Intelligence semantic query module"
    route_prefix = "/api/modules/intel/query"
    tags = ["intel", "intel_query"]

    def __init__(self) -> None:
        super().__init__()
        self._knowledge_store = IntelKnowledgeStore(storage_path=os.getenv("PULSE_INTEL_KNOWLEDGE_PATH", ""))

    @staticmethod
    def _doc_id(*, category: str, title: str, source_url: str) -> str:
        text = f"{_normalize(category)}|{_normalize(title)}|{source_url.strip().lower()}"
        return "doc-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]

    def run_ingest(self, *, items: list[IntelIngestItem], source: str = "manual") -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        now_iso = datetime.now(timezone.utc).isoformat()
        for item in items:
            category = _normalize(item.category)
            title = str(item.title).strip()
            source_url = str(item.source_url or "").strip()
            rows.append(
                {
                    "id": self._doc_id(category=category, title=title, source_url=source_url),
                    "category": category or "general",
                    "title": title,
                    "content": str(item.content).strip(),
                    "summary": str(item.summary or "").strip(),
                    "source_url": source_url,
                    "source": str(source or "manual").strip() or "manual",
                    "tags": [str(tag).strip() for tag in list(item.tags or []) if str(tag).strip()],
                    "collected_at": now_iso,
                    "metadata": {},
                }
            )
        inserted = self._knowledge_store.append(rows)
        return {
            "ok": inserted > 0,
            "inserted": inserted,
            "knowledge_path": self._knowledge_store.storage_path,
            "indexed_count": inserted,
        }

    def run_search(
        self,
        *,
        query: str,
        top_k: int,
        category: str | None = None,
    ) -> dict[str, Any]:
        safe_top_k = max(1, min(top_k, 20))
        safe_category = _normalize(category or "")
        items = self._knowledge_store.search(query=query, top_k=safe_top_k, category=safe_category or None)
        return {
            "query": query,
            "category": safe_category or None,
            "top_k": safe_top_k,
            "total": len(items),
            "items": items,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "note": "No intel documents indexed yet. Collect intel first." if not items else None,
        }

    def handle_intent(
        self,
        intent: str,
        text: str,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        _ = metadata
        if intent == "intel.query.search":
            query = _extract_query(text, fallback="agent")
            return self.run_search(query=query, top_k=5, category=None)
        return None

    def register_routes(self, router: APIRouter) -> None:
        @router.get("/health")
        async def health() -> dict[str, Any]:
            knowledge_count = len(self._knowledge_store.recent(limit=20000))
            return {
                "module": self.name,
                "status": "ok",
                "mode": "knowledge_store",
                "knowledge_count": knowledge_count,
                "indexed_count": knowledge_count,
                "knowledge_path": self._knowledge_store.storage_path,
            }

        @router.post("/ingest")
        async def ingest(payload: IntelIngestRequest) -> dict[str, Any]:
            return self.run_ingest(items=payload.items, source=payload.source)

        @router.post("/search")
        async def search(payload: IntelSearchRequest) -> dict[str, Any]:
            return self.run_search(
                query=payload.query,
                top_k=payload.top_k,
                category=payload.category,
            )


module = IntelQueryModule()
