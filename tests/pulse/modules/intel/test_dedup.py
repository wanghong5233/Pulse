"""Unit tests for intel pipeline.dedup pure helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from pulse.modules.intel.pipeline.dedup import (
    canonical_url,
    dedup_items,
    normalised_title_hash,
)
from pulse.modules.intel.sources import RawItem


def _item(url: str, title: str = "") -> RawItem:
    return RawItem(
        url=url,
        title=title or url,
        content_raw="",
        source_type="rss",
        source_id="example",
        published_at=datetime.now(timezone.utc),
    )


class TestCanonicalUrl:
    def test_strips_utm_and_normalises_host(self) -> None:
        a = canonical_url("https://Example.COM/post/?utm_source=tw&utm_medium=x&id=42")
        b = canonical_url("https://example.com:443/post?id=42&utm_campaign=z")
        assert a == b

    def test_drops_fragment_and_trailing_slash(self) -> None:
        a = canonical_url("https://example.com/foo/#section")
        b = canonical_url("https://example.com/foo")
        assert a == b

    def test_preserves_real_query_params(self) -> None:
        c1 = canonical_url("https://example.com/?id=1")
        c2 = canonical_url("https://example.com/?id=2")
        assert c1 != c2

    def test_empty_or_invalid(self) -> None:
        assert canonical_url("") == ""
        assert canonical_url("   ") == ""

    def test_query_param_order_irrelevant(self) -> None:
        a = canonical_url("https://example.com/?b=2&a=1")
        b = canonical_url("https://example.com/?a=1&b=2")
        assert a == b


class TestNormalisedTitleHash:
    def test_collapses_whitespace_and_case(self) -> None:
        h1 = normalised_title_hash("Hello   World")
        h2 = normalised_title_hash("hello world")
        h3 = normalised_title_hash("HELLO\u3000WORLD")
        assert h1 == h2 == h3

    def test_empty(self) -> None:
        assert normalised_title_hash("") == ""


class TestDedupItems:
    def test_drops_duplicates_within_batch(self) -> None:
        items = [
            _item("https://example.com/a?utm_source=x", "Same Title"),
            _item("https://example.com/a", "Same Title"),
            _item("https://example.com/b", "Different"),
        ]
        unique, canon = dedup_items(items)
        assert len(unique) == 2
        assert len(canon) == 2
        assert unique[0].url == "https://example.com/a?utm_source=x"
        assert unique[1].url == "https://example.com/b"

    def test_drops_already_seen_canonical(self) -> None:
        items = [
            _item("https://example.com/a", "A"),
            _item("https://example.com/b", "B"),
        ]
        seen = {canonical_url("https://example.com/a")}
        unique, canon = dedup_items(items, seen_canonical_urls=seen)
        assert len(unique) == 1
        assert unique[0].url == "https://example.com/b"
        assert canon == [canonical_url("https://example.com/b")]

    def test_drops_same_title_with_different_urls(self) -> None:
        items = [
            _item("https://a.com/post", "Cool Article"),
            _item("https://b.com/post", "Cool Article"),
        ]
        unique, _ = dedup_items(items)
        assert len(unique) == 1

    def test_skips_empty_url(self) -> None:
        items = [_item("", "no url"), _item("https://a.com/x", "ok")]
        unique, _ = dedup_items(items)
        assert len(unique) == 1
        assert unique[0].url == "https://a.com/x"
