"""Stage 5 — anti-information-cocoon controls.

Three knobs, all defined per-topic in ``topic.diversity``:

  * **max_per_source** — round-robin across source buckets so no single
    feed (HN, arXiv, RSSHub-Hugo) can dominate the digest.
  * **contrarian protection** — items flagged by the scorer as
    challenging the prevailing narrative are *always* kept, even if
    their source bucket is already full.
  * **serendipity_slots** — number of cross-topic surprise picks; the
    actual picks come from the orchestrator + store
    (``IntelDocumentStore.serendipity_pool``) so this stage stays pure
    in-memory. The slot count is consumed downstream in publish.

The function operates on already-summarised items only — no IO, no
LLM, no DB. It returns the final ordered list to render.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from typing import Sequence

from ..topics import TopicConfig
from .summarize import SummarizedItem


def diversify(
    *,
    topic: TopicConfig,
    items: Sequence[SummarizedItem],
    max_total: int | None = None,
) -> list[SummarizedItem]:
    """Return a re-ordered, quota-respecting subset of ``items``.

    Score order is preserved *within* each source bucket; cross-source
    interleaving is round-robin so the digest reads as a varied mix
    rather than "10 items from one feed, then 1 from another". Items
    flagged ``is_contrarian`` that fell out of the per-source quota are
    re-appended at the end so contrarian voices never get fully buried.
    """
    if not items:
        return []

    quota = max(1, topic.diversity.max_per_source)

    sorted_items = sorted(
        items,
        key=lambda s: (s.scored.score, s.scored.item.weight),
        reverse=True,
    )
    buckets: "OrderedDict[str, list[SummarizedItem]]" = OrderedDict()
    overflow: dict[str, int] = defaultdict(int)
    for s in sorted_items:
        key = s.source_id or "unknown"
        bucket = buckets.setdefault(key, [])
        if len(bucket) < quota:
            bucket.append(s)
        else:
            overflow[key] += 1

    selected: list[SummarizedItem] = []
    selected_ids: set[int] = set()
    while buckets:
        for key in list(buckets.keys()):
            bucket = buckets[key]
            if not bucket:
                buckets.pop(key, None)
                continue
            picked = bucket.pop(0)
            selected.append(picked)
            selected_ids.add(id(picked))
            if not bucket:
                buckets.pop(key, None)

    contrarian_extras = [
        s for s in sorted_items
        if s.scored.is_contrarian and id(s) not in selected_ids
    ]
    selected.extend(contrarian_extras)

    if max_total is not None:
        selected = selected[: max(0, int(max_total))]
    return selected
