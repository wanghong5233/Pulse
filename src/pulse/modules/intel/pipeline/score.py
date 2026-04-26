"""Stage 3 — LLM rubric scoring.

Every surviving item is scored along the dimensions declared in the
topic's ``scoring.rubric_dimensions`` (default: depth / novelty / impact).
The model returns one JSON object per item; non-parseable responses
fall back to a neutral score and are flagged in ``score_breakdown``
so they show up in audit but don't silently flood the digest.

PR1 calls the LLM one item at a time using ``LLMRouter.invoke_json``.
This keeps the prompt short and the failure surface per-item; batching
lands when token cost becomes a problem (see ``../docs/architecture.md``
§7).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..sources import RawItem
from ..topics import TopicConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ScoredItem:
    item: RawItem
    score: float
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    is_contrarian: bool = False


class LLMScorer:
    """Wraps ``LLMRouter.invoke_json`` for one-shot rubric scoring.

    ``llm_router`` only needs to expose ``invoke_json(prompt, route)`` so
    tests can pass a fake without dragging in langchain.
    """

    def __init__(self, llm_router: Any, *, route: str = "classification") -> None:
        self._llm = llm_router
        self._route = route

    def score(self, *, topic: TopicConfig, item: RawItem) -> ScoredItem:
        prompt = _build_prompt(topic=topic, item=item)
        payload = self._llm.invoke_json(prompt, route=self._route)
        return _parse_response(topic=topic, item=item, payload=payload)


async def score_items(
    *,
    topic: TopicConfig,
    items: Sequence[RawItem],
    scorer: LLMScorer,
) -> list[ScoredItem]:
    """Synchronously score each item; the scorer's LLM call already
    runs in a worker thread under the hood.

    The function is ``async`` so the orchestrator can ``await`` it
    uniformly with the rest of the pipeline.
    """
    out: list[ScoredItem] = []
    for item in items:
        out.append(scorer.score(topic=topic, item=item))
    return out


def _build_prompt(*, topic: TopicConfig, item: RawItem) -> str:
    rubric = topic.scoring.rubric_prompt or _DEFAULT_RUBRIC
    dims = ", ".join(topic.scoring.rubric_dimensions)
    return (
        f"You score one news item for the topic '{topic.display_name}' ({topic.id}).\n"
        "Return ONLY a JSON object with these keys:\n"
        f"  - dimensions: object with numeric scores 0-10 for each of: {dims}\n"
        "  - score: numeric mean of all dimensions, 0-10\n"
        "  - tags: list of <=5 short topical tags (lowercase)\n"
        "  - is_contrarian: boolean, true if the article challenges the "
        "  prevailing narrative on this topic\n\n"
        f"Rubric:\n{rubric}\n\n"
        f"Title: {item.title[:300]}\n"
        f"URL: {item.url}\n"
        f"Source: {item.source_id}\n"
        f"Body: {item.content_raw[:1500]}\n"
    )


def _parse_response(
    *,
    topic: TopicConfig,
    item: RawItem,
    payload: Any,
) -> ScoredItem:
    if payload is None:
        return ScoredItem(
            item=item,
            score=0.0,
            score_breakdown={"_error": "llm returned None"},
        )
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            return ScoredItem(
                item=item,
                score=0.0,
                score_breakdown={"_error": f"non-json payload: {exc}"},
            )
    if not isinstance(payload, dict):
        return ScoredItem(
            item=item,
            score=0.0,
            score_breakdown={"_error": f"unexpected payload type {type(payload).__name__}"},
        )

    dimensions_raw = payload.get("dimensions") or {}
    if not isinstance(dimensions_raw, dict):
        dimensions_raw = {}
    dimensions = {
        str(name): _coerce_score(dimensions_raw.get(name))
        for name in topic.scoring.rubric_dimensions
    }
    raw_score = payload.get("score")
    if raw_score is None and dimensions:
        score = sum(dimensions.values()) / max(1, len(dimensions))
    else:
        score = _coerce_score(raw_score)

    is_contrarian = bool(payload.get("is_contrarian"))
    if is_contrarian and topic.diversity.contrarian_bonus > 0:
        score = min(10.0, score + topic.diversity.contrarian_bonus)

    tags_raw = payload.get("tags") or []
    if not isinstance(tags_raw, list):
        tags_raw = []
    tags = [str(t).strip().lower() for t in tags_raw if str(t or "").strip()][:5]

    breakdown: dict[str, Any] = {"dimensions": dimensions, "tags": tags}
    if is_contrarian:
        breakdown["is_contrarian"] = True
    return ScoredItem(
        item=item,
        score=round(score, 2),
        score_breakdown=breakdown,
        is_contrarian=is_contrarian,
    )


def _coerce_score(value: Any) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(n, 10.0))


_DEFAULT_RUBRIC = (
    "评分维度（每项 0-10，最终 score 取均值）：\n"
    "1. depth：技术深度（vs 营销 / 速食内容）\n"
    "2. novelty：新颖度（是否引入新概念 / 方法）\n"
    "3. impact：工程实用性（vs 纯学术）"
)
