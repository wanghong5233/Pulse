"""Generate a personalized reply to an HR message.

Pure policy component. Given:

  * ``hr_message``  — the latest HR message string (required)
  * ``conversation``— the full conversation row from ``ChatRepository``
                     (provides hr_name / company / job_title / history / cards)
  * ``snapshot``    — current :class:`JobMemorySnapshot` (blocked / prefs / user facts)
  * ``tone_hint``   — optional override, one of ``professional / friendly / concise``

Returns :class:`ReplyDraft` with ``reply_text``, ``tone``, ``confidence`` and
``needs_hitl``. ``confidence`` is a 0.0-1.0 score the service uses to decide
whether to auto-send; ``needs_hitl=True`` forces escalate regardless.

Upstream :class:`HrMessagePlanner` already picked an ``action`` (e.g. ``reply``
vs ``send_resume``); replier only runs when action == ``reply`` AND the
planner didn't return a usable ``reply_text``. This keeps the division of
labor clean — planner = classifier (route=classification), replier =
generator (route=generation).

见 ``docs/Pulse-DomainMemory与Tool模式.md`` §5.1 R4 / §5.2 性能边界。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pulse.core.llm.router import LLMRouter

from ..memory import JobMemorySnapshot

logger = logging.getLogger(__name__)

_TONES: frozenset[str] = frozenset({"professional", "friendly", "concise"})
_REPLY_MAX_CHARS = 180
_HISTORY_MAX_TURNS = 6


@dataclass(frozen=True, slots=True)
class ReplyDraft:
    reply_text: str
    tone: str = "professional"
    confidence: float = 0.0
    needs_hitl: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply_text": self.reply_text,
            "tone": self.tone,
            "confidence": self.confidence,
            "needs_hitl": self.needs_hitl,
            "reason": self.reason,
        }


class HrReplyGenerator:
    """LLM-backed reply drafter with deterministic fallback."""

    def __init__(self, llm_router: LLMRouter) -> None:
        self._llm = llm_router

    def draft(
        self,
        *,
        hr_message: str,
        conversation: dict[str, Any] | None = None,
        snapshot: JobMemorySnapshot | None = None,
        tone_hint: str = "",
        max_chars: int = _REPLY_MAX_CHARS,
    ) -> ReplyDraft:
        safe_message = str(hr_message or "").strip()
        if not safe_message:
            return ReplyDraft(
                reply_text="",
                reason="empty HR message",
                needs_hitl=True,
            )
        limit = max(30, int(max_chars))
        llm_draft = self._draft_with_llm(
            message=safe_message,
            conversation=conversation or {},
            snapshot=snapshot,
            tone_hint=tone_hint,
            max_chars=limit,
        )
        if llm_draft is not None:
            return llm_draft
        return self._draft_with_heuristic(message=safe_message, max_chars=limit)

    # ──────────────────────────────────────────── LLM path

    def _draft_with_llm(
        self,
        *,
        message: str,
        conversation: dict[str, Any],
        snapshot: JobMemorySnapshot | None,
        tone_hint: str,
        max_chars: int,
    ) -> ReplyDraft | None:
        snapshot_md = snapshot.to_prompt_section() if snapshot is not None else "(no preferences set)"
        context_md = self._render_conversation(conversation)
        tone = (tone_hint or "").strip().lower()
        tone_md = tone if tone in _TONES else "professional"

        system_prompt = (
            "You draft the USER's reply to an HR on a Chinese recruiting platform. "
            "Respect the user's preferences and facts: 不要对已屏蔽公司示好; "
            "不要承诺简历/面试/到岗时间除非 user_facts 明确支持; 不要编造技能。\n"
            f"约束: reply_text ≤ {max_chars} 汉字, 语气 {tone_md}, 一段话不换行.\n"
            "如果问题触及敏感话题(薪资具体数字 / offer 比较 / 线下面试时间) 或"
            "需要用户主观决策, 设 needs_hitl=true 并把草稿写成温和的 stall 句式(例如"
            "'这个我稍后详细回复你')。\n"
            "Respond with ONLY a JSON object. Schema:\n"
            '{"reply_text":"<中文>",'
            '"tone":"professional|friendly|concise",'
            '"confidence":<float 0-1>,'
            '"needs_hitl":<bool>,'
            '"reason":"<one line>"}\n\n'
            f"## User preferences\n{snapshot_md}"
        )
        user_prompt = (
            f"## Conversation context\n{context_md}\n\n"
            f"## Latest HR message\n{message[:1200]}\n\n"
            "Return JSON only."
        )

        parsed = self._llm.invoke_json(
            [
                _system(system_prompt),
                _user(user_prompt),
            ],
            route="generation",
        )
        if not isinstance(parsed, dict):
            return None

        text = str(parsed.get("reply_text") or "").strip()
        if not text:
            return None
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "…"

        tone_out = str(parsed.get("tone") or tone_md).strip().lower()
        if tone_out not in _TONES:
            tone_out = "professional"
        try:
            confidence = float(parsed.get("confidence", 0.6))
        except (TypeError, ValueError):
            confidence = 0.6
        confidence = max(0.0, min(confidence, 1.0))
        needs_hitl = bool(parsed.get("needs_hitl"))
        reason = str(parsed.get("reason") or "").strip()[:200] or "llm_generation"

        return ReplyDraft(
            reply_text=text,
            tone=tone_out,
            confidence=confidence,
            needs_hitl=needs_hitl,
            reason=reason,
        )

    # ──────────────────────────────────────────── heuristic fallback

    @staticmethod
    def _draft_with_heuristic(*, message: str, max_chars: int) -> ReplyDraft:
        """保守降级: 不涉及个性化信息, 给 HITL 方向的 stall 文本。"""
        lowered = message.lower()
        if any(tok in lowered for tok in ("简历", "resume", "作品集", "附件")):
            text = "您好，已收到，我这边马上发送简历。"
            needs_hitl = False
            reason = "heuristic: HR asked for resume"
        elif any(tok in lowered for tok in ("电话", "线下", "薪资", "offer", "面试时间")):
            text = "您好，这个问题我稍后详细回复您。"
            needs_hitl = True
            reason = "heuristic: sensitive topic — needs HITL"
        else:
            text = "您好，我已看到消息，稍后回复您。"
            needs_hitl = True
            reason = "heuristic: generic stall"
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "…"
        return ReplyDraft(
            reply_text=text,
            tone="professional",
            confidence=0.4,
            needs_hitl=needs_hitl,
            reason=reason,
        )

    # ──────────────────────────────────────────── helpers

    @staticmethod
    def _render_conversation(conversation: dict[str, Any]) -> str:
        if not conversation:
            return "- (no conversation context)"
        lines: list[str] = []
        hr = str(conversation.get("hr_name") or "").strip()
        company = str(conversation.get("company") or "").strip()
        job = str(conversation.get("job_title") or "").strip()
        if hr:
            lines.append(f"- hr_name: {hr}")
        if company:
            lines.append(f"- company: {company}")
        if job:
            lines.append(f"- job_title: {job}")

        history = conversation.get("history")
        if isinstance(history, list) and history:
            lines.append("- recent_turns:")
            for turn in history[-_HISTORY_MAX_TURNS:]:
                if not isinstance(turn, dict):
                    continue
                speaker = str(turn.get("speaker") or turn.get("role") or "?").strip() or "?"
                content = str(turn.get("content") or turn.get("text") or "").strip()
                if not content:
                    continue
                if len(content) > 200:
                    content = content[:200] + "…"
                lines.append(f"    * {speaker}: {content}")
        return "\n".join(lines) or "- (no conversation context)"


def _system(content: str) -> Any:
    from langchain_core.messages import SystemMessage
    return SystemMessage(content=content)


def _user(content: str) -> Any:
    from langchain_core.messages import HumanMessage
    return HumanMessage(content=content)


__all__ = ["HrReplyGenerator", "ReplyDraft"]
