"""Web-search fallback connector.

Reuses :func:`pulse.core.tools.web_search.search_web` so we inherit the
same provider selection (DuckDuckGo HTML by default, env-pluggable
elsewhere). The connector is a *fallback*: cheap to add to any topic,
but ranks low on diversity because the underlying provider tends to
cluster results.

The connector is sync-bound through ``asyncio.to_thread`` to keep it
pluggable into the ``asyncio.gather`` orchestrator without pulling
``httpx`` for one extra dependency.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ....core.tools.web_search import search_web
from ..topics import SourceConfig
from .base import (
    RawItem,
    SourceFetchResult,
    domain_of,
    register_fetcher,
)

logger = logging.getLogger(__name__)


class WebSearchFetcher:
    source_type = "web_search"

    async def fetch(self, cfg: SourceConfig) -> SourceFetchResult:
        query = (cfg.query or cfg.label or "").strip()
        source_id = cfg.label.strip() or f"web_search:{query[:48]}"
        if not query:
            return SourceFetchResult(
                source_id=source_id,
                source_type=self.source_type,
                error="web_search source missing query",
            )

        max_results = max(1, min(int(cfg.max_results), 20))
        try:
            hits = await asyncio.to_thread(search_web, query, max_results)
        except RuntimeError as exc:
            logger.warning("web_search fetch failed query=%s err=%s", query, exc)
            return SourceFetchResult(
                source_id=source_id,
                source_type=self.source_type,
                error=f"search error: {exc}",
            )

        now = datetime.now(timezone.utc)
        items: list[RawItem] = []
        for hit in hits:
            url = (hit.url or "").strip()
            title = (hit.title or "").strip()
            if not url or not title:
                continue
            items.append(
                RawItem(
                    url=url,
                    title=title,
                    content_raw=(hit.snippet or "").strip(),
                    source_type=self.source_type,
                    source_id=domain_of(url) or source_id,
                    source_label=cfg.label or "web_search",
                    published_at=now,
                    weight=cfg.weight,
                    extra={"query": query},
                )
            )
        return SourceFetchResult(
            source_id=source_id,
            source_type=self.source_type,
            items=items,
        )


register_fetcher("web_search", WebSearchFetcher)
