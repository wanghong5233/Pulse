"""Intel source connectors.

Every connector implements the :class:`SourceFetcher` Protocol; pipeline
fetches them concurrently via ``asyncio.gather`` and merges their
``RawItem`` outputs before dedup. Adding a new ``source_type`` is a code
change here plus a doc entry in ``../docs/source-types.md``.
"""

from .base import RawItem, SourceFetchResult, SourceFetcher, build_fetcher
from .github import GitHubTrendingFetcher
from .rss import RssFetcher
from .web_search import WebSearchFetcher

__all__ = [
    "GitHubTrendingFetcher",
    "RawItem",
    "RssFetcher",
    "SourceFetchResult",
    "SourceFetcher",
    "WebSearchFetcher",
    "build_fetcher",
]
