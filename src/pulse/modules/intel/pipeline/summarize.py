"""Stage 4 — per-item summary.

The summary feeds two consumers:

  * the published digest (so the user can decide to click through);
  * the persisted ``intel_documents.content_summary`` column, which is
    what the search tool grep-searches in PR4.

Each item gets one ``LLMRouter.invoke_text`` call (cheap route). On
failure we fall back to the original ``content_raw`` truncated, so the
digest still ships — better a verbatim snippet than no digest at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from ..topics import TopicConfig
from .score import ScoredItem

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SummarizedItem:
    scored: ScoredItem
    summary: str

    @property
    def url(self) -> str:
        return self.scored.item.url

    @property
    def title(self) -> str:
        return self.scored.item.title

    @property
    def source_id(self) -> str:
        return self.scored.item.source_id


class LLMSummarizer:
    """Wraps ``LLMRouter.invoke_text`` for short Chinese summaries."""

    def __init__(self, llm_router: Any, *, route: str = "generation") -> None:
        self._llm = llm_router
        self._route = route

    def summarize(self, *, topic: TopicConfig, scored: ScoredItem) -> SummarizedItem:
        prompt = _build_prompt(topic=topic, scored=scored)
        try:
            text = self._llm.invoke_text(prompt, route=self._route)
        except RuntimeError as exc:
            logger.warning(
                "intel summarize llm error topic=%s url=%s err=%s",
                topic.id,
                scored.item.url,
                exc,
            )
            text = scored.item.content_raw[:240]
        cleaned = (text or scored.item.content_raw or "").strip()
        if not cleaned:
            cleaned = scored.item.title
        return SummarizedItem(scored=scored, summary=cleaned[:600])


async def summarize_items(
    *,
    topic: TopicConfig,
    scored_items: Sequence[ScoredItem],
    summarizer: LLMSummarizer,
) -> list[SummarizedItem]:
    return [summarizer.summarize(topic=topic, scored=s) for s in scored_items]


def _build_prompt(*, topic: TopicConfig, scored: ScoredItem) -> str:
    return (
        "You write a concise Chinese summary (<= 120 字) for one news "
        f"item under the topic '{topic.display_name}'.\n"
        "Return only the summary text, no JSON, no preface.\n"
        "聚焦事实点（数字 / 名称 / 关键结论），不要复述标题。\n\n"
        f"标题: {scored.item.title[:300]}\n"
        f"原文摘要: {scored.item.content_raw[:1500]}\n"
        f"URL: {scored.item.url}\n"
    )
