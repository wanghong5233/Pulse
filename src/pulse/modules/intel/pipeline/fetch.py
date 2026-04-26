"""Stage 1 — concurrent source fetch.

Calls every source declared in the topic config in parallel via
``asyncio.gather``. A semaphore caps parallelism so a topic with 20
sources cannot DoS the upstreams (RSSHub / DuckDuckGo / GitHub).

Per-source failures are isolated: one broken feed contributes zero
items but never aborts the whole topic. Errors are returned alongside
the items so the orchestrator can emit them as structured events.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from ..sources import SourceFetchResult, build_fetcher
from ..topics import SourceConfig, TopicConfig

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CONCURRENCY = 6


async def fetch_all_sources(
    topic: TopicConfig,
    *,
    max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
) -> list[SourceFetchResult]:
    """Run every ``SourceFetcher`` for ``topic`` concurrently."""
    sources: list[SourceConfig] = list(topic.sources)
    if not sources:
        return []

    sem = asyncio.Semaphore(max(1, min(int(max_concurrency), 32)))

    async def _run(cfg: SourceConfig) -> SourceFetchResult:
        try:
            fetcher = build_fetcher(cfg.type)
        except KeyError as exc:
            logger.warning("intel fetch unknown source_type=%s topic=%s", cfg.type, topic.id)
            return SourceFetchResult(
                source_id=cfg.label or cfg.type,
                source_type=str(cfg.type),
                error=f"unknown source_type: {exc}",
            )
        async with sem:
            return await fetcher.fetch(cfg)

    results = await asyncio.gather(*(_run(cfg) for cfg in sources), return_exceptions=False)
    return list(results)


def flatten_items(results: Iterable[SourceFetchResult]) -> list:
    """Concatenate ``items`` across results, preserving fetch order."""
    out = []
    for result in results:
        out.extend(result.items)
    return out
