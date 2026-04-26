"""Stage 2 — deduplication.

Two layers:

  1. **Canonical URL** — strip tracking params, normalise scheme / host /
     trailing slash. Same article shared on Twitter and Hacker News with
     different ``utm_*`` tails should collapse.
  2. **Title hash** — within the current batch, identical normalised
     titles collapse (catches RSS feeds that re-emit yesterday's post
     with a fresh ``pubDate``).

PR1 stops here. MinHash / shingling lands later when we have evidence
that title collisions miss real duplicates (see ``../docs/architecture.md``
§5).
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from ..sources import RawItem

_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "spm",
}

_TITLE_NORMALIZE = re.compile(r"[\s\u3000]+")


def canonical_url(url: str) -> str:
    """Return a stable de-dup key for ``url``.

    - lowercase scheme + host
    - strip default ports
    - drop tracking query params
    - sort remaining query params
    - drop trailing slash on path (except root)
    - drop fragment
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return raw.lower()
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    cleaned_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if key.lower() not in _TRACKING_PARAMS
    ]
    cleaned_query.sort()
    query = urlencode(cleaned_query, doseq=True)
    return urlunparse((scheme, netloc, path, "", query, ""))


def normalised_title_hash(title: str) -> str:
    cleaned = _TITLE_NORMALIZE.sub(" ", str(title or "").strip().lower())
    if not cleaned:
        return ""
    return hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:16]


def dedup_items(
    items: Iterable[RawItem],
    *,
    seen_canonical_urls: set[str] | None = None,
) -> tuple[list[RawItem], list[str]]:
    """Return ``(unique_items, canonical_urls)`` after batch + corpus dedup.

    ``seen_canonical_urls`` is the set of canonical URLs already in the
    store; items hitting it are dropped immediately. The second return
    value is the canonical URLs of the items that survived, in input
    order — handy for the orchestrator to log diff counts.
    """
    already_seen = set(seen_canonical_urls or ())
    in_batch_canon: set[str] = set()
    in_batch_title: set[str] = set()
    out: list[RawItem] = []
    canon_out: list[str] = []
    for item in items:
        canon = canonical_url(item.url)
        if not canon:
            continue
        if canon in already_seen or canon in in_batch_canon:
            continue
        title_key = normalised_title_hash(item.title)
        if title_key and title_key in in_batch_title:
            continue
        in_batch_canon.add(canon)
        if title_key:
            in_batch_title.add(title_key)
        out.append(item)
        canon_out.append(canon)
    return out, canon_out
