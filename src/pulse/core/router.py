from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class _LLMIntentOutput(BaseModel):
    intent: str = Field(..., min_length=1, max_length=120)
    confidence: float = Field(default=0.0, ge=0, le=1)
    reason: str = Field(default="", max_length=300)


class StructuredInvoker(Protocol):
    def invoke_structured(self, prompt_value: Any, schema: type[Any], *, route: str = "default") -> Any:
        ...


@dataclass(slots=True)
class RouteDecision:
    intent: str
    target: str | None
    method: str  # exact | prefix | llm | fallback
    confidence: float
    reason: str


def _normalize(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


class IntentRouter:
    """Intent resolver with exact, prefix, and slash-command LLM fallback stages.

    **Scope (first-principle)**: 这是 *legacy slash-command* 路由器, 不是自然语言
    意图识别器. 职责是把 ``/email process`` / ``/intel interview collect`` 这类
    **command-style 前缀输入**路由到对应业务 target. 自然语言输入(例如
    "帮我投递 5 个 JD") 应该**直接交给 Brain** 的 ReAct + tool-use 机制决定,
    不在这里做 LLM 意图猜测:
      - 准确率低 (``router_rules.json`` 缺失 job.* 时会误分到 intel.search 等)
      - 多花一次 classification LLM 调用 (3s+)
      - 结果通常被 Brain 忽略, 只制造 trace 噪声

    因此 ``_resolve_with_llm`` **只在输入看起来是 slash-command 但 exact/prefix
    规则未命中时触发**, 自然语言输入不会触发. 见 docs/Pulse-内核架构总览.md.
    """

    def __init__(
        self,
        *,
        llm_router: StructuredInvoker | None = None,
        fallback_intent: str = "general.default",
        fallback_target: str | None = None,
    ) -> None:
        self._llm_router = llm_router
        self._fallback_intent = fallback_intent
        self._fallback_target = fallback_target
        self._intent_targets: dict[str, str] = {}
        self._exact_routes: dict[str, str] = {}
        self._prefix_routes: list[tuple[str, str]] = []

    def register_intent(self, intent: str, *, target: str) -> None:
        safe_intent = _normalize(intent)
        if not safe_intent:
            raise ValueError("intent must be non-empty")
        self._intent_targets[safe_intent] = str(target).strip() or target

    def register_exact(self, key: str, *, intent: str) -> None:
        safe_key = _normalize(key)
        safe_intent = _normalize(intent)
        if not safe_key or not safe_intent:
            raise ValueError("exact route key/intent must be non-empty")
        self._exact_routes[safe_key] = safe_intent

    def register_prefix(self, prefix: str, *, intent: str) -> None:
        safe_prefix = _normalize(prefix)
        safe_intent = _normalize(intent)
        if not safe_prefix or not safe_intent:
            raise ValueError("prefix route key/intent must be non-empty")
        self._prefix_routes.append((safe_prefix, safe_intent))
        self._prefix_routes.sort(key=lambda item: len(item[0]), reverse=True)

    def known_intents(self) -> list[str]:
        return sorted(self._intent_targets.keys())

    def resolve(self, text: str) -> RouteDecision:
        normalized = _normalize(text)
        if normalized in self._exact_routes:
            intent = self._exact_routes[normalized]
            return RouteDecision(
                intent=intent,
                target=self._intent_targets.get(intent),
                method="exact",
                confidence=1.0,
                reason=f"exact matched: {normalized}",
            )

        for prefix, intent in self._prefix_routes:
            if normalized.startswith(prefix):
                return RouteDecision(
                    intent=intent,
                    target=self._intent_targets.get(intent),
                    method="prefix",
                    confidence=0.95,
                    reason=f"prefix matched: {prefix}",
                )

        # 只对"看起来像命令"的输入尝试 LLM 意图猜测.
        # 这里的命令启发式是保守的: 必须以 `/` / `!` 开头, 或全是拉丁英文命令 token
        # (如 ``ping`` / ``email process``) 且 ≤ 6 个词. 绝大多数自然语言句子
        # (含中文、标点、长度 > 6 词)都不满足, 直接走 fallback → Brain.
        if self._looks_like_command(normalized):
            llm_result = self._resolve_with_llm(text)
            if llm_result is not None:
                return llm_result
            fallback_reason = "command-like but no rule/llm match"
        else:
            fallback_reason = "natural-language input: defer to Brain"

        fallback_intent = _normalize(self._fallback_intent)
        return RouteDecision(
            intent=fallback_intent,
            target=self._intent_targets.get(fallback_intent, self._fallback_target),
            method="fallback",
            confidence=0.2,
            reason=fallback_reason,
        )

    @staticmethod
    def _looks_like_command(normalized_text: str) -> bool:
        """Heuristic: 判断输入是否是 slash-command 风格指令.

        保守规则 (宁缺毋滥, 不确定就 False → 交给 Brain):
          * 以 ``/`` 或 ``!`` 开头
          * 或者: 全是 ASCII 英文 + 空格 + ``./-_``, 且总词数 ≤ 6 (像 ``ping``
            / ``email process`` / ``intel interview collect``)
        """
        text = str(normalized_text or "").strip()
        if not text:
            return False
        if text[0] in ("/", "!"):
            return True
        if not text.isascii():
            return False
        tokens = text.split()
        if len(tokens) > 6:
            return False
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789._- ")
        return all(ch in allowed for ch in text)

    def _resolve_with_llm(self, text: str) -> RouteDecision | None:
        if self._llm_router is None:
            return None
        intents = self.known_intents()
        if not intents:
            return None
        prompt = (
            "Choose the most suitable intent from candidates. "
            "Return JSON with: intent, confidence(0-1), reason.\n"
            f"candidates={intents}\n"
            f"text={text}"
        )
        try:
            output = self._llm_router.invoke_structured(prompt, _LLMIntentOutput, route="classification")
        except Exception as exc:   # noqa: BLE001
            logger.warning(
                "IntentRouter LLM fallback failed (command-like input), "
                "defer to rule fallback: %s: %s",
                type(exc).__name__, str(exc)[:200],
            )
            return None

        candidate_intent = _normalize(getattr(output, "intent", ""))
        if not candidate_intent or candidate_intent not in self._intent_targets:
            return None
        confidence_raw = getattr(output, "confidence", 0.0)
        try:
            confidence = max(0.0, min(float(confidence_raw), 1.0))
        except (TypeError, ValueError):
            confidence = 0.5
        reason = str(getattr(output, "reason", "") or "llm selected")
        return RouteDecision(
            intent=candidate_intent,
            target=self._intent_targets.get(candidate_intent),
            method="llm",
            confidence=confidence,
            reason=reason[:300],
        )
