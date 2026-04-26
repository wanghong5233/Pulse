"""DigestWorkflow — deterministic six-stage pipeline.

Each stage is a pure async function with explicit inputs / outputs:

    fetch.run    : TopicConfig → list[RawItem]
    dedup.run    : list[RawItem] → list[RawItem]      (drops known canonical_url)
    score.run    : list[RawItem] → list[ScoredItem]   (LLM rubric per topic)
    summarize.run: list[ScoredItem] → list[SummarizedItem]
    diversify.run: list[SummarizedItem] → list[SummarizedItem]
    publish.run  : list[SummarizedItem] → DigestResult (Notifier + store)

The orchestrator wires them together with structured events on every
stage transition and returns a :class:`WorkflowResult` for the patrol /
HTTP caller.
"""

from .dedup import canonical_url, dedup_items
from .fetch import fetch_all_sources
from .orchestrator import (
    DigestPublishResult,
    DigestWorkflowOrchestrator,
    StageEventEmitter,
    WorkflowResult,
)
from .publish import build_digest_text
from .score import ScoredItem, score_items
from .summarize import SummarizedItem, summarize_items

__all__ = [
    "DigestPublishResult",
    "DigestWorkflowOrchestrator",
    "ScoredItem",
    "StageEventEmitter",
    "SummarizedItem",
    "WorkflowResult",
    "build_digest_text",
    "canonical_url",
    "dedup_items",
    "fetch_all_sources",
    "score_items",
    "summarize_items",
]
