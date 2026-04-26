"""Pure-logic tests for the Intel pipeline (no DB, no LLM).

Covers the deterministic helpers that don't need a postgres fixture:

  * canonical URL normalisation + dedup
  * diversify (max_per_source, contrarian preserved, serendipity)
  * topic config validation (pydantic schema)

End-to-end orchestrator coverage lives in ``test_intel_module.py`` where
we already need the postgres fixture for the store.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from pulse.modules.intel.pipeline.dedup import canonical_url, dedup_items
from pulse.modules.intel.pipeline.diversify import diversify
from pulse.modules.intel.pipeline.score import ScoredItem
from pulse.modules.intel.pipeline.summarize import SummarizedItem
from pulse.modules.intel.sources import RawItem
from pulse.modules.intel.sources.github import GitHubTrendingFetcher
from pulse.modules.intel.topics import (
    SourceConfig,
    TopicConfig,
    discover_topic_files,
    load_topic_configs,
    load_topic_file,
)


# ---------------------------------------------------------------------------
# canonical_url
# ---------------------------------------------------------------------------


def test_canonical_url_strips_tracking_params_and_orders_query() -> None:
    raw = "HTTPS://Example.com/Post/?utm_source=feed&b=2&a=1#frag"
    assert canonical_url(raw) == "https://example.com/Post?a=1&b=2"


def test_canonical_url_drops_default_ports_and_trailing_slash() -> None:
    assert canonical_url("https://example.com:443/path/") == "https://example.com/path"
    assert canonical_url("http://example.com:80/") == "http://example.com/"


def test_canonical_url_returns_empty_for_blank_input() -> None:
    assert canonical_url("") == ""
    assert canonical_url("   ") == ""


# ---------------------------------------------------------------------------
# dedup_items
# ---------------------------------------------------------------------------


def _raw(url: str, title: str = "t", source_id: str = "demo") -> RawItem:
    return RawItem(
        url=url,
        title=title,
        content_raw="",
        source_type="rss",
        source_id=source_id,
    )


def test_dedup_drops_known_canonical_urls() -> None:
    items = [
        _raw("https://example.com/a"),
        _raw("https://example.com/b?utm_source=x"),
    ]
    seen = {"https://example.com/a"}
    out, canon = dedup_items(items, seen_canonical_urls=seen)
    assert len(out) == 1
    assert out[0].url == "https://example.com/b?utm_source=x"
    assert canon == ["https://example.com/b"]


def test_dedup_collapses_in_batch_duplicates() -> None:
    items = [
        _raw("https://example.com/a"),
        _raw("https://example.com/a/?utm_source=tw"),
        _raw("https://example.com/b", title="Different"),
        _raw("https://example.com/c", title="different"),
    ]
    out, canon = dedup_items(items)
    urls = [i.url for i in out]
    assert urls[0] == "https://example.com/a"
    assert "https://example.com/b" in urls
    assert "https://example.com/c" not in urls
    assert len(canon) == len(out)


# ---------------------------------------------------------------------------
# diversify
# ---------------------------------------------------------------------------


def _summarized(
    *,
    score: float,
    source_id: str,
    url: str,
    is_contrarian: bool = False,
) -> SummarizedItem:
    raw = RawItem(
        url=url,
        title=url,
        content_raw="",
        source_type="rss",
        source_id=source_id,
    )
    scored = ScoredItem(item=raw, score=score, is_contrarian=is_contrarian)
    return SummarizedItem(scored=scored, summary="...")


def _topic_with_diversity(**diversity: Any) -> TopicConfig:
    return TopicConfig.model_validate(
        {
            "id": "t",
            "display_name": "T",
            "sources": [{"type": "rss", "url": "https://example.com/feed"}],
            "diversity": diversity or {"max_per_source": 2, "serendipity_slots": 0},
        }
    )


def test_diversify_enforces_max_per_source() -> None:
    items = [
        _summarized(score=9.0, source_id="hn", url="u1"),
        _summarized(score=8.0, source_id="hn", url="u2"),
        _summarized(score=7.0, source_id="hn", url="u3"),
        _summarized(score=6.0, source_id="arxiv", url="u4"),
    ]
    topic = _topic_with_diversity(max_per_source=2, serendipity_slots=0)
    out = diversify(topic=topic, items=items)
    out_urls = [s.url for s in out]
    hn_count = sum(1 for s in out if s.source_id == "hn")
    assert hn_count == 2
    assert "u1" in out_urls and "u2" in out_urls
    assert "u3" not in out_urls
    assert "u4" in out_urls


def test_diversify_keeps_contrarian_even_if_quota_full() -> None:
    items = [
        _summarized(score=9.0, source_id="hn", url="hot1"),
        _summarized(score=8.5, source_id="hn", url="hot2"),
        _summarized(
            score=7.0, source_id="hn", url="against", is_contrarian=True
        ),
    ]
    topic = _topic_with_diversity(max_per_source=2, serendipity_slots=0)
    out = diversify(topic=topic, items=items)
    out_urls = [s.url for s in out]
    assert "against" in out_urls


def test_diversify_round_robins_across_sources() -> None:
    """Highest-scoring item from each bucket alternates so no feed dominates."""
    items = [
        _summarized(score=9.0, source_id="hn", url="hn1"),
        _summarized(score=8.0, source_id="hn", url="hn2"),
        _summarized(score=7.0, source_id="arxiv", url="ax1"),
        _summarized(score=6.0, source_id="arxiv", url="ax2"),
    ]
    topic = _topic_with_diversity(max_per_source=2, serendipity_slots=0)
    out = diversify(topic=topic, items=items)
    out_urls = [s.url for s in out]
    assert out_urls[0] == "hn1"
    assert out_urls[1] == "ax1"
    assert set(out_urls) == {"hn1", "hn2", "ax1", "ax2"}


def test_diversify_max_total_caps_output() -> None:
    items = [
        _summarized(score=9.0, source_id="hn", url="u1"),
        _summarized(score=8.0, source_id="arxiv", url="u2"),
        _summarized(score=7.0, source_id="ml", url="u3"),
    ]
    topic = _topic_with_diversity(max_per_source=2, serendipity_slots=0)
    out = diversify(topic=topic, items=items, max_total=2)
    assert len(out) == 2


# ---------------------------------------------------------------------------
# topic schema
# ---------------------------------------------------------------------------


_TOPICS_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "pulse"
    / "modules"
    / "intel"
    / "topics"
)


def test_llm_frontier_topic_loads_cleanly() -> None:
    topic = load_topic_file(_TOPICS_DIR / "llm_frontier.yaml")
    assert topic.id == "llm_frontier"
    assert topic.sources, "topic must declare at least one source"
    assert topic.publish.peak_interval_seconds > 0
    assert 0.0 <= topic.scoring.threshold <= 10.0


def test_discover_skips_underscore_prefixed_files() -> None:
    files = discover_topic_files(_TOPICS_DIR)
    assert all(not p.name.startswith("_") for p in files)
    names = {p.stem for p in files}
    assert {"llm_frontier", "autumn_recruit", "interview_prep"}.issubset(names)


def test_load_topic_configs_returns_only_active() -> None:
    configs = load_topic_configs(_TOPICS_DIR)
    ids = {c.id for c in configs}
    assert {"llm_frontier", "autumn_recruit", "interview_prep"}.issubset(ids)


def test_active_topics_cover_pr2_set() -> None:
    """The two newly-activated topics expose the diversity knobs PR2 ships."""
    configs = {c.id: c for c in load_topic_configs(_TOPICS_DIR)}
    for tid in ("autumn_recruit", "interview_prep"):
        topic = configs[tid]
        assert topic.diversity.max_per_source >= 1
        assert topic.diversity.serendipity_slots >= 1
        assert topic.publish.peak_interval_seconds > 0
        assert topic.publish.offpeak_interval_seconds > 0


def test_topic_requires_at_least_one_source() -> None:
    with pytest.raises(Exception):  # noqa: B017 — ValidationError wrapped in RuntimeError shape
        TopicConfig.model_validate(
            {
                "id": "empty",
                "display_name": "Empty",
                "sources": [],
            }
        )


def test_source_config_rejects_blank_url() -> None:
    with pytest.raises(Exception):  # noqa: B017 — pydantic raises ValidationError
        SourceConfig.model_validate({"type": "rss", "url": "   "})


# ---------------------------------------------------------------------------
# GitHub Trending fetcher (URL building + payload mapping; no network)
# ---------------------------------------------------------------------------


def test_github_trending_url_includes_filters() -> None:
    fetcher = GitHubTrendingFetcher()
    cfg = SourceConfig.model_validate(
        {
            "type": "github_trending",
            "language": "python",
            "spoken_language": "en",
            "since": "weekly",
            "max_results": 50,
            "label": "py-weekly",
        }
    )
    url = fetcher._build_url(cfg)  # noqa: SLF001 — pure helper, easier than a fake server
    assert url.startswith("https://api.github.com/search/repositories?")
    assert "language%3Apython" in url
    assert "spoken_language%3Aen" in url
    assert "stars%3A%3E1" in url
    assert "sort=stars" in url
    assert "per_page=50" in url


def test_github_trending_rejects_unknown_since() -> None:
    fetcher = GitHubTrendingFetcher()
    cfg = SourceConfig.model_validate({"type": "github_trending", "since": "yearly"})
    with pytest.raises(ValueError):
        fetcher._build_url(cfg)  # noqa: SLF001


def test_github_trending_maps_payload_to_raw_items() -> None:
    fetcher = GitHubTrendingFetcher()
    cfg = SourceConfig.model_validate(
        {"type": "github_trending", "language": "python", "since": "weekly"}
    )
    payload = {
        "items": [
            {
                "html_url": "https://github.com/owner/repo",
                "full_name": "owner/repo",
                "description": "An LLM toolkit.",
                "stargazers_count": 1234,
                "forks_count": 56,
                "language": "Python",
                "topics": ["llm", "agent", "framework"],
                "created_at": "2026-04-20T00:00:00Z",
            },
            # missing html_url → filtered
            {"full_name": "broken/repo"},
        ]
    }
    items = fetcher._items_from_payload(payload, cfg=cfg, source_id="gh-test")  # noqa: SLF001
    assert len(items) == 1
    item = items[0]
    assert item.url == "https://github.com/owner/repo"
    assert item.title == "owner/repo"
    assert "An LLM toolkit." in item.content_raw
    assert "llm, agent, framework" in item.content_raw
    assert item.extra["stars"] == 1234
    assert item.extra["language"] == "Python"
    assert item.published_at is not None


# ---------------------------------------------------------------------------
# IntelSettings + RSSHub resolver
# ---------------------------------------------------------------------------


def test_intel_settings_parses_instance_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """``rsshub_instance_list`` strips trailing slashes and skips blanks."""
    from pulse.modules.intel.config import IntelSettings

    monkeypatch.setenv(
        "PULSE_INTEL_RSSHUB_INSTANCES",
        "http://rsshub:1200/, , https://rsshub.app",
    )
    settings = IntelSettings()
    assert settings.rsshub_instance_list == [
        "http://rsshub:1200",
        "https://rsshub.app",
    ]


def test_rss_fetcher_resolves_rsshub_url_to_first_healthy_instance() -> None:
    """``rsshub://`` URLs should fan out across configured instances.

    First base whose probe succeeds wins; the route is appended verbatim
    so source-id stays stable regardless of which mirror answered.
    """
    import asyncio

    from pulse.modules.intel.config import IntelSettings
    from pulse.modules.intel.sources.rss import RssFetcher
    from pulse.modules.intel.topics import SourceConfig

    settings = IntelSettings(
        rsshub_instances="http://primary:1200,https://fallback.example",
        rsshub_probe_timeout_sec=2.0,
        rsshub_health_ttl_sec=60,
    )

    probed: list[str] = []
    downloaded: list[str] = []

    fetcher = RssFetcher(intel_settings=settings)

    def fake_head(base: str) -> None:
        probed.append(base)
        if base == "http://primary:1200":
            import urllib.error

            raise urllib.error.URLError("connection refused")

    def fake_download(url: str) -> bytes:
        downloaded.append(url)
        return b"<rss version='2.0'><channel></channel></rss>"

    fetcher._head = fake_head  # type: ignore[method-assign]
    fetcher._download = fake_download  # type: ignore[method-assign]

    cfg = SourceConfig.model_validate(
        {"type": "rss", "url": "rsshub:///nowcoder/discuss/2", "label": "test"}
    )
    result = asyncio.run(fetcher.fetch(cfg))

    assert result.error is None
    assert probed == ["http://primary:1200", "https://fallback.example"]
    assert downloaded == ["https://fallback.example/nowcoder/discuss/2"]
    assert result.source_id == "test"


def test_rss_fetcher_returns_error_when_all_instances_unhealthy() -> None:
    """Every instance failing the probe must surface a clean error."""
    import asyncio
    import urllib.error

    from pulse.modules.intel.config import IntelSettings
    from pulse.modules.intel.sources.rss import RssFetcher
    from pulse.modules.intel.topics import SourceConfig

    settings = IntelSettings(
        rsshub_instances="http://a:1200,http://b:1200",
        rsshub_health_ttl_sec=60,
    )
    fetcher = RssFetcher(intel_settings=settings)

    def always_fail(base: str) -> None:
        raise urllib.error.URLError("down")

    fetcher._head = always_fail  # type: ignore[method-assign]
    fetcher._download = lambda url: pytest.fail(  # type: ignore[method-assign]
        f"download must not be reached, was called with {url}"
    )

    cfg = SourceConfig.model_validate(
        {"type": "rss", "url": "rsshub:///foo/bar"}
    )
    result = asyncio.run(fetcher.fetch(cfg))

    assert result.items == []
    assert result.error is not None
    assert "rsshub" in result.error
    assert "unhealthy" in result.error or "all instances failed" in result.error


def test_rss_fetcher_uses_health_cache_within_ttl() -> None:
    """Healthy probe is cached so we don't hammer RSSHub on every fetch."""
    import asyncio

    from pulse.modules.intel.config import IntelSettings
    from pulse.modules.intel.sources.rss import RssFetcher
    from pulse.modules.intel.topics import SourceConfig

    settings = IntelSettings(
        rsshub_instances="http://only:1200",
        rsshub_health_ttl_sec=300,
    )
    fetcher = RssFetcher(intel_settings=settings)

    probe_calls: list[str] = []
    download_calls: list[str] = []

    fetcher._head = lambda base: probe_calls.append(base)  # type: ignore[method-assign]
    fetcher._download = lambda url: (  # type: ignore[method-assign]
        download_calls.append(url) or b"<rss version='2.0'><channel></channel></rss>"
    )

    cfg = SourceConfig.model_validate({"type": "rss", "url": "rsshub:///a/b"})

    asyncio.run(fetcher.fetch(cfg))
    asyncio.run(fetcher.fetch(cfg))

    assert len(probe_calls) == 1, "second fetch must reuse the cached probe"
    assert len(download_calls) == 2
