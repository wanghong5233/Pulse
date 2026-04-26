"""GitHub Trending connector — official Search API, no scraping.

GitHub does not expose the html-rendered ``/trending`` page through a
documented API, but ``/search/repositories`` with a ``created:`` filter
plus ``sort=stars`` is a stable proxy: "repos created in the last
``since`` window, sorted by stars". That gives us the same shape as
trending without depending on an undocumented endpoint that the GitHub
team has historically broken.

Auth is optional and comes from ``IntelSettings.github_token``; without
it we still get the public 60 req/h limit which is plenty for one
patrol run per day.

PR3 returns repos as :class:`RawItem` with description as
``content_raw``. README extraction is deliberately left out — it'd
double the request count and the description is enough for the
LLM scorer to grade depth/novelty.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from ..config import IntelSettings, get_intel_settings
from ..topics import SourceConfig
from .base import RawItem, SourceFetchResult, register_fetcher

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com/search/repositories"
_USER_AGENT = "Pulse-Intel-GitHubTrending"
_DEFAULT_TIMEOUT = 10.0
_SINCE_DAYS = {
    "daily": 1,
    "weekly": 7,
    "monthly": 30,
}


class GitHubTrendingFetcher:
    """Fetches recent high-star repos via GitHub's Search API.

    ``cfg.language``     — optional GitHub language filter (e.g. ``python``).
    ``cfg.since``        — one of ``daily`` / ``weekly`` / ``monthly``;
                           defaults to ``weekly``.
    ``cfg.max_results``  — capped at 100 (GitHub's per-page maximum).
    ``cfg.label``        — optional human-readable source_id override.
    """

    source_type = "github_trending"

    def __init__(
        self,
        *,
        timeout_sec: float = _DEFAULT_TIMEOUT,
        intel_settings: IntelSettings | None = None,
    ) -> None:
        self._timeout = max(2.0, min(float(timeout_sec), 60.0))
        settings = intel_settings or get_intel_settings()
        self._token = settings.github_token.strip() or None

    async def fetch(self, cfg: SourceConfig) -> SourceFetchResult:
        source_id = (cfg.label or "").strip() or self._derive_source_id(cfg)
        try:
            url = self._build_url(cfg)
        except ValueError as exc:
            return SourceFetchResult(
                source_id=source_id,
                source_type=self.source_type,
                error=str(exc),
            )

        try:
            payload = await asyncio.to_thread(self._request, url)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            logger.warning("github trending fetch failed url=%s err=%s", url, exc)
            return SourceFetchResult(
                source_id=source_id,
                source_type=self.source_type,
                error=f"transport error: {exc}",
            )
        except json.JSONDecodeError as exc:
            return SourceFetchResult(
                source_id=source_id,
                source_type=self.source_type,
                error=f"non-JSON response: {exc}",
            )

        items = self._items_from_payload(payload, cfg=cfg, source_id=source_id)
        return SourceFetchResult(
            source_id=source_id,
            source_type=self.source_type,
            items=items[: cfg.max_results],
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _derive_source_id(cfg: SourceConfig) -> str:
        lang = (cfg.language or "any").strip().lower() or "any"
        since = (cfg.since or "weekly").strip().lower() or "weekly"
        return f"github_trending:{lang}:{since}"

    def _build_url(self, cfg: SourceConfig) -> str:
        since_key = (cfg.since or "weekly").strip().lower() or "weekly"
        days = _SINCE_DAYS.get(since_key)
        if days is None:
            raise ValueError(
                f"github_trending since must be one of {sorted(_SINCE_DAYS)}; got {since_key!r}"
            )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        query_parts = [f"created:>={cutoff}", "stars:>1"]
        if cfg.language:
            query_parts.append(f"language:{cfg.language.strip()}")
        if cfg.spoken_language:
            query_parts.append(f"spoken_language:{cfg.spoken_language.strip()}")
        params = {
            "q": " ".join(query_parts),
            "sort": "stars",
            "order": "desc",
            "per_page": str(min(max(cfg.max_results, 1), 100)),
        }
        return f"{_GITHUB_API}?{urllib.parse.urlencode(params)}"

    def _request(self, url: str) -> dict:
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            body = resp.read()
        return json.loads(body.decode("utf-8"))

    def _items_from_payload(
        self,
        payload: dict,
        *,
        cfg: SourceConfig,
        source_id: str,
    ) -> list[RawItem]:
        if not isinstance(payload, dict):
            return []
        repos = payload.get("items") or []
        if not isinstance(repos, list):
            return []
        out: list[RawItem] = []
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            url = str(repo.get("html_url") or "").strip()
            full_name = str(repo.get("full_name") or "").strip()
            description = str(repo.get("description") or "").strip()
            if not url or not full_name:
                continue
            stars = int(repo.get("stargazers_count") or 0)
            forks = int(repo.get("forks_count") or 0)
            language = str(repo.get("language") or "").strip()
            topics = repo.get("topics") or []
            if not isinstance(topics, list):
                topics = []
            content_lines = [
                f"⭐ {stars} · 🔱 {forks}" + (f" · {language}" if language else ""),
            ]
            if description:
                content_lines.append(description)
            if topics:
                content_lines.append("topics: " + ", ".join(str(t) for t in topics[:8]))
            published_at = _parse_iso8601(repo.get("created_at"))
            out.append(
                RawItem(
                    url=url,
                    title=full_name,
                    content_raw="\n".join(content_lines),
                    source_type=self.source_type,
                    source_id=source_id,
                    source_label=cfg.label,
                    published_at=published_at,
                    weight=cfg.weight,
                    extra={
                        "stars": stars,
                        "forks": forks,
                        "language": language,
                        "topics": list(topics),
                    },
                )
            )
        return out


def _parse_iso8601(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


register_fetcher("github_trending", GitHubTrendingFetcher)
