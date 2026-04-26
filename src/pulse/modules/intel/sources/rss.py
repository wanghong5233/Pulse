"""RSS / Atom connector — covers standard RSS feeds and RSSHub routes.

Standard library only on purpose: RSSHub already normalises 99% of the
sites we care about, and ``feedparser`` would be a third-party dep with
its own surprise behaviour (HTML entity rewrites, redirect retries).
``xml.etree.ElementTree`` plus a small ``items()`` helper covers RSS 2.0
and Atom feeds; anything stranger goes through RSSHub first.

Networking uses ``urllib.request`` inside ``asyncio.to_thread`` so the
pipeline stays single-threaded asyncio-friendly without pulling in
``httpx``.

URLs that start with ``rsshub://`` are resolved against the configured
:class:`pulse.modules.intel.config.IntelSettings` instances. The fetcher
keeps a tiny in-memory health cache so a permanently-down instance
doesn't get hammered on every patrol.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from ..config import IntelSettings, get_intel_settings
from ..topics import SourceConfig
from .base import (
    RawItem,
    SourceFetchResult,
    domain_of,
    register_fetcher,
)

logger = logging.getLogger(__name__)

_USER_AGENT = "Mozilla/5.0 (Pulse Intel; +https://github.com/) RSS-Reader"
_DEFAULT_TIMEOUT = 10.0
_NS = {"atom": "http://www.w3.org/2005/Atom"}
_RSSHUB_SCHEME = "rsshub://"


class RssFetcher:
    source_type = "rss"

    def __init__(
        self,
        *,
        timeout_sec: float = _DEFAULT_TIMEOUT,
        intel_settings: IntelSettings | None = None,
    ) -> None:
        self._timeout = max(2.0, min(float(timeout_sec), 60.0))
        self._settings = intel_settings or get_intel_settings()
        # ``base_url -> (healthy, expires_at)``; absent ⇒ never probed.
        self._health_cache: dict[str, tuple[bool, float]] = {}

    async def fetch(self, cfg: SourceConfig) -> SourceFetchResult:
        raw_url = (cfg.url or "").strip()
        source_id = self._derive_source_id(cfg)
        if not raw_url:
            return SourceFetchResult(
                source_id=source_id,
                source_type=self.source_type,
                error="rss source missing url",
            )
        try:
            body, used_url = await self._download_resolved(raw_url)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            logger.warning("rss fetch failed url=%s err=%s", raw_url, exc)
            return SourceFetchResult(
                source_id=source_id,
                source_type=self.source_type,
                error=f"transport error: {exc}",
            )
        except RuntimeError as exc:
            # Raised when no RSSHub instance is healthy — keep the message
            # explicit rather than turning it into a generic transport
            # error so operators see "rsshub: all instances down".
            logger.warning("rsshub fetch unresolved url=%s err=%s", raw_url, exc)
            return SourceFetchResult(
                source_id=source_id,
                source_type=self.source_type,
                error=str(exc),
            )
        url = used_url

        try:
            items = self._parse(body, cfg=cfg, source_id=source_id)
        except ET.ParseError as exc:
            logger.warning("rss parse failed url=%s err=%s", url, exc)
            return SourceFetchResult(
                source_id=source_id,
                source_type=self.source_type,
                error=f"parse error: {exc}",
            )
        return SourceFetchResult(
            source_id=source_id,
            source_type=self.source_type,
            items=items[: cfg.max_results],
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _derive_source_id(cfg: SourceConfig) -> str:
        if cfg.label:
            return cfg.label.strip()
        url = (cfg.url or "").strip()
        if not url:
            return "rss:unknown"
        if url.startswith(_RSSHUB_SCHEME):
            return f"rsshub:{url[len(_RSSHUB_SCHEME):].lstrip('/')}"
        return domain_of(url) or url

    async def _download_resolved(self, raw_url: str) -> tuple[bytes, str]:
        """Download the feed, resolving ``rsshub://`` against settings.

        Returns ``(body, effective_url)``. Raises ``RuntimeError`` when
        every configured RSSHub instance is unreachable.
        """
        if not raw_url.startswith(_RSSHUB_SCHEME):
            body = await asyncio.to_thread(self._download, raw_url)
            return body, raw_url

        route = raw_url[len(_RSSHUB_SCHEME):]
        if not route.startswith("/"):
            route = "/" + route
        bases = self._settings.rsshub_instance_list
        if not bases:
            raise RuntimeError("rsshub: no instances configured")

        last_error: Exception | None = None
        for base in bases:
            if not await self._probe(base):
                continue
            target = f"{base}{route}"
            try:
                body = await asyncio.to_thread(self._download, target)
                return body, target
            except (
                urllib.error.HTTPError,
                urllib.error.URLError,
                TimeoutError,
            ) as exc:
                # This instance was healthy on probe but failed for the
                # actual route — invalidate its cache entry so the next
                # request re-probes, and try the next instance.
                self._health_cache[base] = (False, time.monotonic() + 30.0)
                last_error = exc
                logger.info(
                    "rsshub instance %s failed on route %s: %s",
                    base,
                    route,
                    exc,
                )
        if last_error is not None:
            raise RuntimeError(
                f"rsshub: all instances failed for {route} (last error: {last_error})"
            )
        raise RuntimeError(f"rsshub: all instances unhealthy for {route}")

    async def _probe(self, base: str) -> bool:
        """Return whether ``base`` is healthy, using a short-lived cache."""
        now = time.monotonic()
        cached = self._health_cache.get(base)
        if cached is not None:
            healthy, expires = cached
            if now < expires:
                return healthy
        try:
            await asyncio.to_thread(self._head, base)
            healthy = True
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            logger.debug("rsshub probe failed base=%s err=%s", base, exc)
            healthy = False
        ttl = float(self._settings.rsshub_health_ttl_sec)
        self._health_cache[base] = (healthy, now + ttl)
        return healthy

    def _head(self, base: str) -> None:
        """Cheap liveness check — HEAD on the base URL."""
        req = urllib.request.Request(
            base,
            headers={"User-Agent": _USER_AGENT},
            method="HEAD",
        )
        timeout = float(self._settings.rsshub_probe_timeout_sec)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            _ = resp.read(1)

    def _download(self, url: str) -> bytes:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return resp.read()

    def _parse(
        self,
        body: bytes,
        *,
        cfg: SourceConfig,
        source_id: str,
    ) -> list[RawItem]:
        root = ET.fromstring(body)
        if root.tag.endswith("rss") or root.tag == "rss":
            return self._parse_rss(root, cfg=cfg, source_id=source_id)
        if root.tag.endswith("feed"):
            return self._parse_atom(root, cfg=cfg, source_id=source_id)
        # Some feeds wrap with rdf:RDF (RSS 1.0) — handle as RSS-ish.
        return self._parse_rss(root, cfg=cfg, source_id=source_id)

    def _parse_rss(
        self,
        root: ET.Element,
        *,
        cfg: SourceConfig,
        source_id: str,
    ) -> list[RawItem]:
        items: list[RawItem] = []
        for entry in root.iter("item"):
            link = _text(entry.find("link"))
            title = _text(entry.find("title"))
            description = _text(entry.find("description"))
            pub = _parse_rfc822(_text(entry.find("pubDate")))
            if not link or not title:
                continue
            items.append(
                RawItem(
                    url=link,
                    title=title,
                    content_raw=description,
                    source_type=self.source_type,
                    source_id=source_id,
                    source_label=cfg.label,
                    published_at=pub,
                    weight=cfg.weight,
                )
            )
        return items

    def _parse_atom(
        self,
        root: ET.Element,
        *,
        cfg: SourceConfig,
        source_id: str,
    ) -> list[RawItem]:
        items: list[RawItem] = []
        for entry in root.findall("atom:entry", _NS):
            title = _text(entry.find("atom:title", _NS))
            summary = _text(entry.find("atom:summary", _NS)) or _text(
                entry.find("atom:content", _NS)
            )
            link = ""
            for link_el in entry.findall("atom:link", _NS):
                href = link_el.attrib.get("href")
                rel = link_el.attrib.get("rel", "alternate")
                if href and rel == "alternate":
                    link = href
                    break
            published_str = _text(entry.find("atom:published", _NS)) or _text(
                entry.find("atom:updated", _NS)
            )
            if not link or not title:
                continue
            items.append(
                RawItem(
                    url=link,
                    title=title,
                    content_raw=summary,
                    source_type=self.source_type,
                    source_id=source_id,
                    source_label=cfg.label,
                    published_at=_parse_iso8601(published_str),
                    weight=cfg.weight,
                )
            )
        return items


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _parse_rfc822(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_iso8601(value: str) -> datetime | None:
    if not value:
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


register_fetcher("rss", RssFetcher)
