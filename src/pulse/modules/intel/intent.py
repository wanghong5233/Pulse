"""IntentSpec wiring for the Intel module.

Four IntentSpec entries map 1:1 to the contracts declared in
``router_rules.json`` and the SKILL_SCHEMA:

  * ``intel.digest.list``    — list known topics and their last digest
  * ``intel.digest.latest``  — fetch latest digest for one topic
  * ``intel.digest.run``     — kick the deterministic workflow now
  * ``intel.search``         — keyword ILIKE search across topics

Each handler is a thin shim that calls into :class:`IntelModule`'s
service surface; the module itself owns dependency wiring (store /
orchestrator / runtime). Keeping the schemas here lets the module file
stay focused on lifecycle and HTTP plumbing.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from ...core.module import IntentSpec


class IntelService(Protocol):
    """Surface that the intent handlers depend on.

    The :class:`IntelModule` implements all four methods; tests can
    pass a fake implementation without touching the runtime.
    """

    def list_digests(self) -> dict[str, Any]:
        ...

    def latest_digest(self, *, topic_id: str, limit: int = 30) -> dict[str, Any]:
        ...

    async def run_digest(
        self,
        *,
        topic_id: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        ...

    def search_documents(
        self,
        *,
        keywords: list[str],
        topic_id: str | None = None,
        top_k: int = 10,
        match: str = "any",
    ) -> dict[str, Any]:
        ...


def build_intel_intents(service: IntelService) -> list[IntentSpec]:
    """Build the four IntentSpec entries bound to ``service``."""

    list_handler = _wrap_list(service)
    latest_handler = _wrap_latest(service)
    run_handler = _wrap_run(service)
    search_handler = _wrap_search(service)

    return [
        IntentSpec(
            name="intel.digest.list",
            description=(
                "List every active intel topic together with the timestamp "
                "of its most recent digest run. Read-only."
            ),
            when_to_use=(
                "用户问\"现在订阅了哪些资讯主题 / 上一次什么时候推过\"。"
                "结果用于 Brain 决定下一步要不要拉某个具体主题的 digest。"
            ),
            when_not_to_use=(
                "1) 想看具体一期内容 → 用 intel.digest.latest;"
                "2) 想立刻拉一遍 → 用 intel.digest.run;"
                "3) 想跨主题检索关键词 → 用 intel.search."
            ),
            parameters_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=list_handler,
            mutates=False,
            risk_level=0,
            examples=[
                {"user_utterance": "我都订阅了哪些主题？", "kwargs": {}},
                {"user_utterance": "/intel digest list", "kwargs": {}},
            ],
        ),
        IntentSpec(
            name="intel.digest.latest",
            description=(
                "Return the latest digest entries for one topic, newest "
                "first. Caller chooses topic_id and optional row limit."
            ),
            when_to_use=(
                "用户问\"最近大模型前沿有什么新东西\"或\"最新一期日报是哪些条目\"。"
                "返回已落库的最近 N 条 (默认 30), 不会触发新 fetch。"
            ),
            when_not_to_use=(
                "1) 想强制重新跑一遍 workflow → 用 intel.digest.run;"
                "2) 想跨主题搜索关键词 → 用 intel.search;"
                "3) 不知道 topic_id → 先 intel.digest.list."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "topic_id": {
                        "type": "string",
                        "description": "Topic id from intel.digest.list, e.g. 'llm_frontier'.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max rows to return.",
                        "default": 30,
                        "minimum": 1,
                        "maximum": 100,
                    },
                },
                "required": ["topic_id"],
                "additionalProperties": False,
            },
            handler=latest_handler,
            mutates=False,
            risk_level=0,
            examples=[
                {
                    "user_utterance": "看一下大模型前沿最新的日报",
                    "kwargs": {"topic_id": "llm_frontier"},
                },
                {
                    "user_utterance": "/intel digest latest llm_frontier",
                    "kwargs": {"topic_id": "llm_frontier"},
                },
            ],
        ),
        IntentSpec(
            name="intel.digest.run",
            description=(
                "Trigger the deterministic six-stage workflow for one topic "
                "right now: fetch → dedup → score → summarize → diversify → "
                "publish. Sends to the configured channel unless dry_run=true."
            ),
            when_to_use=(
                "用户说\"立刻跑一遍 / 现在采一次 / 立刻看看新内容\". 不要等下次 patrol."
                "dry_run=true 时只演练不发飞书; dry_run=false (默认) 会真发."
            ),
            when_not_to_use=(
                "1) 只想看已有最新一期 → intel.digest.latest;"
                "2) 想搜旧内容 → intel.search;"
                "3) 不知道 topic_id → 先 intel.digest.list."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "topic_id": {
                        "type": "string",
                        "description": "Topic id from intel.digest.list.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": (
                            "If true, run the full workflow but skip the "
                            "Notifier / webhook side-effects."
                        ),
                        "default": False,
                    },
                },
                "required": ["topic_id"],
                "additionalProperties": False,
            },
            handler=run_handler,
            mutates=True,
            risk_level=1,
            examples=[
                {
                    "user_utterance": "立刻跑一遍秋招主题",
                    "kwargs": {"topic_id": "autumn_recruit"},
                },
                {
                    "user_utterance": "演练大模型前沿主题, 但先不要发飞书",
                    "kwargs": {"topic_id": "llm_frontier", "dry_run": True},
                },
            ],
        ),
        IntentSpec(
            name="intel.search",
            description=(
                "Keyword ILIKE search across persisted intel documents. "
                "Caller supplies LLM-extracted keywords; the store does "
                "literal substring match — agentic search style, no "
                "vector index."
            ),
            when_to_use=(
                "用户对话中提到\"你之前看到的那篇 / 资讯库里有没有 / 上次推的关于...\"。"
                "Brain 抽出关键词后一次性传 keywords 数组, match='any'/'all' 自选."
            ),
            when_not_to_use=(
                "1) 想看某主题最新一期 → intel.digest.latest;"
                "2) 想拉新内容 → intel.digest.run;"
                "3) keywords 为空 — 先做澄清提问, 不要拿空查询撞库."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Keywords / phrases for ILIKE substring match.",
                        "minItems": 1,
                        "maxItems": 8,
                    },
                    "topic_id": {
                        "type": "string",
                        "description": "Optional: scope search to one topic.",
                    },
                    "top_k": {
                        "type": "integer",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 50,
                    },
                    "match": {
                        "type": "string",
                        "enum": ["any", "all"],
                        "default": "any",
                        "description": "Combine keywords with OR ('any') or AND ('all').",
                    },
                },
                "required": ["keywords"],
                "additionalProperties": False,
            },
            handler=search_handler,
            mutates=False,
            risk_level=0,
            examples=[
                {
                    "user_utterance": "你之前看到的那篇 MCP 安全治理文章在哪？",
                    "kwargs": {"keywords": ["MCP", "安全"], "match": "all"},
                },
                {
                    "user_utterance": "/intel search agent observability",
                    "kwargs": {"keywords": ["agent", "observability"]},
                },
            ],
        ),
    ]


# ---------------------------------------------------------------------------
# Handler wrappers
# ---------------------------------------------------------------------------

def _wrap_list(service: IntelService) -> Callable[..., Any]:
    def _handler(**kwargs: Any) -> dict[str, Any]:
        _ = kwargs
        return service.list_digests()

    return _handler


def _wrap_latest(service: IntelService) -> Callable[..., Any]:
    def _handler(**kwargs: Any) -> dict[str, Any]:
        topic_id = str(kwargs.get("topic_id") or "").strip()
        if not topic_id:
            return {"ok": False, "error": "topic_id is required"}
        limit = int(kwargs.get("limit") or 30)
        return service.latest_digest(topic_id=topic_id, limit=limit)

    return _handler


def _wrap_run(service: IntelService) -> Callable[..., Any]:
    async def _handler(**kwargs: Any) -> dict[str, Any]:
        topic_id = str(kwargs.get("topic_id") or "").strip()
        if not topic_id:
            return {"ok": False, "error": "topic_id is required"}
        dry_run = bool(kwargs.get("dry_run") or False)
        return await service.run_digest(topic_id=topic_id, dry_run=dry_run)

    return _handler


def _wrap_search(service: IntelService) -> Callable[..., Any]:
    def _handler(**kwargs: Any) -> dict[str, Any]:
        raw_keywords = kwargs.get("keywords") or []
        if isinstance(raw_keywords, str):
            raw_keywords = [raw_keywords]
        keywords = [str(k).strip() for k in raw_keywords if str(k or "").strip()]
        if not keywords:
            return {"ok": False, "error": "at least one keyword is required"}
        topic_id_raw = kwargs.get("topic_id")
        topic_id = str(topic_id_raw).strip() if topic_id_raw else None
        top_k = int(kwargs.get("top_k") or 10)
        match = str(kwargs.get("match") or "any").strip().lower()
        return service.search_documents(
            keywords=keywords,
            topic_id=topic_id,
            top_k=top_k,
            match=match,
        )

    return _handler
