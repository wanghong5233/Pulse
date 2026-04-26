"""SourceFetcher Protocol and shared shape.

Every connector exposes one async method::

    async def fetch(cfg: SourceConfig) -> SourceFetchResult

with no other side effects. Connectors must:

  * fail loud — raise on transport errors instead of returning empty;
    the orchestrator catches per-source errors so one broken feed cannot
    poison a topic.
  * never call the LLM — scoring / summarising is the pipeline's job,
    not the connector's.
  * never write to the store — the pipeline is the only writer.

``RawItem`` is the only data type passed downstream; keep it minimal.
``content_raw`` is "best effort body" — RSS gives summary, web search
gives snippet, GitHub gives README excerpt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import urlparse

from ..topics import SourceConfig


@dataclass(slots=True)
class RawItem:
    """Pre-dedup item produced by a connector."""

    url: str
    title: str
    content_raw: str
    source_type: str
    source_id: str
    source_label: str = ""
    published_at: datetime | None = None
    weight: float = 1.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SourceFetchResult:
    """Per-source outcome — items + structured error string."""

    source_id: str
    source_type: str
    items: list[RawItem] = field(default_factory=list)
    error: str | None = None


class SourceFetcher(Protocol):
    """Async fetcher contract; implementers live in this folder."""

    source_type: str

    async def fetch(self, cfg: SourceConfig) -> SourceFetchResult:  # pragma: no cover - protocol
        ...


_REGISTRY: dict[str, Callable[[], SourceFetcher]] = {}


def register_fetcher(source_type: str, factory: Callable[[], SourceFetcher]) -> None:
    """Register a SourceFetcher factory under its ``source_type`` string."""
    key = str(source_type or "").strip().lower()
    if not key:
        raise ValueError("source_type must be non-empty")
    _REGISTRY[key] = factory


def build_fetcher(source_type: str) -> SourceFetcher:
    key = str(source_type or "").strip().lower()
    factory = _REGISTRY.get(key)
    if factory is None:
        raise KeyError(
            f"no SourceFetcher registered for source_type={key!r}; "
            f"known types: {sorted(_REGISTRY)}"
        )
    return factory()


def domain_of(url: str) -> str:
    """Extract a stable ``source_id`` from a URL (host without port).

    RSSHub routes (``rsshub.app/openai/blog``) collapse to ``rsshub.app``
    by default; pass ``label`` in the source config to keep them separate
    when running a diversity quota.
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        host = urlparse(raw).netloc
    except ValueError:
        return ""
    if not host:
        return ""
    return host.split(":", 1)[0].lower()


# Help typing tools understand register_fetcher's expected callable.
FetcherFactory = Callable[[], SourceFetcher]
ResolvedFetch = Callable[[SourceConfig], Awaitable[SourceFetchResult]]
