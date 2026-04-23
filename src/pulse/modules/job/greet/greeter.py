"""Generate a personalized greeting text for a single JD + user snapshot.

Pure policy component. Inputs:

  * job          — normalized scan item (title/company/snippet/detail)
  * snapshot     — :class:`JobMemorySnapshot` (偏好 + user facts)
  * match        — :class:`MatchResult`(matcher 输出的签字, 帮助 greeter 选角)
  * template     — 用户自定义默认模板(来自 ``GreetPolicy.greeting_template``),
                   LLM 失败时降级使用

Output: :class:`GreetDraft`(``greeting_text``, ``tone``, ``reason``)。

设计取舍:
  * 用 ``route=generation`` 生成更流畅的文本; classification route 通常开 temperature=0.1
    太板, generation route 仍由 ``LLMRouter`` 统一配置。
  * 不走 tool-use, 只走 ``invoke_json`` — 招呼文本只是一个字符串加 rationale,
    用 JSON 可以在 prompt 里固定 schema, 同时避免模型输出前后语助词干扰。
  * 任何 LLM 失败都降级到 template 渲染(不丢业务)。
  * 体积约束: 招呼文本控制在 90 字以内, 过长的自动截断 — BOSS 直聘招呼
    受 IM 对话框长度限制, 也避免显得像机器人。

见 ``docs/Pulse-DomainMemory与Tool模式.md`` §5.1 R3。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pulse.core.llm.router import LLMRouter

from ..memory import JobMemorySnapshot
from .matcher import MatchResult

logger = logging.getLogger(__name__)


_TONES: frozenset[str] = frozenset({"professional", "friendly", "concise"})
_GREETING_MAX_CHARS = 90
_DEFAULT_TEMPLATE = "你好，我对{job_title}这个岗位很感兴趣，期待进一步沟通。"


@dataclass(frozen=True, slots=True)
class GreetDraft:
    greeting_text: str
    tone: str = "professional"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "greeting_text": self.greeting_text,
            "tone": self.tone,
            "reason": self.reason,
        }


class JobGreeter:
    """LLM-backed personalized greeting with deterministic template fallback."""

    def __init__(self, llm_router: LLMRouter) -> None:
        self._llm = llm_router

    def compose(
        self,
        *,
        job: dict[str, Any],
        snapshot: JobMemorySnapshot | None,
        match: MatchResult | None = None,
        template: str = "",
        max_chars: int = _GREETING_MAX_CHARS,
    ) -> GreetDraft:
        """主入口: 先 LLM, 失败降级 template。

        ``max_chars`` 用于硬截断, 默认 90 (BOSS 招呼场景安全值)。
        """
        limit = max(30, int(max_chars))

        draft = self._compose_with_llm(
            job=job,
            snapshot=snapshot,
            match=match,
            max_chars=limit,
        )
        if draft is not None:
            return draft
        return self._compose_with_template(job=job, template=template, max_chars=limit)

    # ──────────────────────────────────────────── LLM path

    def _compose_with_llm(
        self,
        *,
        job: dict[str, Any],
        snapshot: JobMemorySnapshot | None,
        match: MatchResult | None,
        max_chars: int,
    ) -> GreetDraft | None:
        snapshot_md = snapshot.to_prompt_section() if snapshot is not None else "(no preferences set)"
        job_md = self._render_job(job)
        match_md = self._render_match(match)

        system_prompt = (
            "You draft the FIRST message a job seeker sends to an HR on a "
            "Chinese recruiting platform (e.g. BOSS 直聘). Tone: 自然、礼貌、"
            "言之有物; 禁止使用 '尊敬的 HR'、'贵司' 等生硬模板腔。\n"
            f"约束: greeting_text 必须 ≤ {max_chars} 汉字, 一段话不分行, "
            "优先引用用户简历中与岗位最相关的一条事实(若 snapshot 中有 "
            "user-level facts), 并提及一个具体的岗位关键词以显示非自动群发。\n"
            "不要编造用户没说过的经历; 若 snapshot 中 user_facts 为空, 只写岗位匹配点。\n"
            "Respond with ONLY a JSON object. Schema:\n"
            '{"greeting_text": "<≤N 字中文>", '
            '"tone": "professional|friendly|concise", '
            '"reason": "<one-line rationale>"}\n\n'
            f"## User preferences\n{snapshot_md}"
        )
        user_prompt = (
            f"## Job\n{job_md}\n\n"
            f"## Match verdict (from upstream matcher)\n{match_md}\n\n"
            "Return JSON only, greeting_text in 中文."
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

        text = str(parsed.get("greeting_text") or "").strip()
        if not text:
            return None
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "…"

        tone = str(parsed.get("tone") or "professional").strip().lower()
        if tone not in _TONES:
            tone = "professional"
        reason = str(parsed.get("reason") or "").strip()[:200]
        return GreetDraft(greeting_text=text, tone=tone, reason=reason or "llm_generation")

    # ──────────────────────────────────────────── template path

    @staticmethod
    def _compose_with_template(
        *,
        job: dict[str, Any],
        template: str,
        max_chars: int,
    ) -> GreetDraft:
        title = str(job.get("title") or "").strip() or "该岗位"
        tpl = (template or "").strip() or _DEFAULT_TEMPLATE
        try:
            text = tpl.format(job_title=title, title=title)
        except (KeyError, IndexError, ValueError):
            text = tpl
        text = text.strip()
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "…"
        return GreetDraft(
            greeting_text=text,
            tone="professional",
            reason="template_fallback",
        )

    # ──────────────────────────────────────────── helpers

    @staticmethod
    def _render_job(job: dict[str, Any]) -> str:
        title = str(job.get("title") or "").strip()
        company = str(job.get("company") or "").strip()
        salary = str(job.get("salary") or "").strip() or "(not provided)"
        snippet = str(job.get("snippet") or "").strip()
        return (
            f"- title: {title}\n"
            f"- company: {company}\n"
            f"- salary: {salary}\n"
            f"- snippet: {snippet[:800]}"
        )

    @staticmethod
    def _render_match(match: MatchResult | None) -> str:
        if match is None:
            return "(no prior matcher signal)"
        lines = [
            f"- score: {match.score}",
            f"- verdict: {match.verdict}",
        ]
        if match.matched_signals:
            sig = "; ".join(match.matched_signals[:5])
            lines.append(f"- matched_signals: {sig}")
        if match.concerns:
            con = "; ".join(match.concerns[:5])
            lines.append(f"- concerns: {con}")
        return "\n".join(lines)


def _system(content: str) -> Any:
    from langchain_core.messages import SystemMessage
    return SystemMessage(content=content)


def _user(content: str) -> Any:
    from langchain_core.messages import HumanMessage
    return HumanMessage(content=content)


__all__ = ["JobGreeter", "GreetDraft"]
