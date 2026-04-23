"""Contract C · CommitmentVerifier — ADR-001 §4.4 (v2).

End-of-turn audit that answers one question:

    "Does the reply draft claim to have DONE something that the turn's
     Receipt Ledger shows was NOT actually done?"

v2 upgrade (driven by trace_89690fb72ff8, where v1 fired a False-Absence
inverse mis-fire):

* Input ``used_tools: list[str] + observations_digest: str`` → replaced
  by ``turn_evidence: TurnEvidence``. The ledger carries both
  pre_capture side-effects (e.g. ``preference.domain.applied`` events)
  AND tool receipts with structured facts (input_keys, result_count,
  extracted_facts). See NabaOS / arXiv 2603.10060 for the receipt-based
  paradigm this aligns with.
* Input ``reply: str`` → split into ``raw_reply`` (pre-shaper, preserves
  action detail) and ``shaped_reply`` (user-visible; rewrite baseline).
  The judge evaluates commitment semantics on the raw reply, but any
  rewrite is based on the shaped style/length.
* Output adds ``hallucination_type`` enum
  (AgentHallu arXiv 2601.06818 taxonomy), surfaced in event payloads
  for offline analytics.

Invariants preserved from v1:

* **Fail-open**: verifier's own failure → verdict ``"degraded"`` +
  returns ``shaped_reply``; ops see the event, user keeps going.
* **Fail-loud**: judge says unfulfilled → returns ``rewritten_reply``
  (honest admission), never the fake-success text.
* **Pure structural**: this module has no domain vocabulary; the judge
  LLM does all semantic work. Python just orchestrates I/O and parses
  a strict JSON envelope.
* **Env kill-switch**: ``PULSE_COMMITMENT_VERIFIER=off`` short-circuits
  to ``"verified"`` without any LLM spend (ADR-001 §5.1 rollback).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from langchain_core.messages import HumanMessage, SystemMessage

from pulse.core.task_context import TaskContext

logger = logging.getLogger(__name__)

VerifierVerdict = Literal["verified", "unfulfilled", "degraded"]

HallucinationType = Literal[
    "none",
    "fabricated",
    "count_mismatch",
    "fact_mismatch",
    "inference_as_fact",
    "false_absence",
]

_VALID_HALLUCINATION_TYPES: frozenset[str] = frozenset(HallucinationType.__args__)  # type: ignore[attr-defined]


class _LLMRouterLike(Protocol):
    """Minimal subset of ``LLMRouter`` this module depends on."""

    def invoke_text(self, messages: list[Any], *, route: str = "default") -> str: ...


# ──────────────────────────────────────────────────────────────
# Evidence (Receipt Ledger) — ADR-001 §4.4.2
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Receipt:
    """Structured record of one side-effect that happened in this turn.

    Two kinds are currently modelled:

    * ``kind == "tool"``  : LLM directly invoked a registered tool
      (ReAct loop). ``input_keys`` is the list of tool-call parameter
      keys (values omitted to save tokens and avoid PII leakage).
    * ``kind == "event"`` : pre_turn / post_turn side effect dispatched
      by soul.reflection (e.g. ``preference.domain.applied``). No
      input_keys (the event comes from a reflection dispatch, not the
      LLM directly).

    ``extracted_facts`` is the ground-truth business field map, produced
    by either ``ToolSpec.extract_facts`` (kind="tool") or the reflection
    applier's ``DispatchResult.effect`` (kind="event"). See ADR-001 §4.5
    for the contract.

    ``action_report`` (ADR-003 Step B.2a) is the serialised form of a
    ``core.action_report.ActionReport`` that the tool handler emitted via
    ``observation[ACTION_REPORT_KEY]``. When present, it is the
    **preferred** grounding signal for the judge — its ``status`` + ``metrics``
    + ``details[*]`` carry the handler's own authoritative view of "what
    just happened", which does not go through the LLM's own observation
    translation step and cannot be erased by a whitelist that missed a
    field. Absent → judge falls back to ``extracted_facts`` only.
    """

    kind: Literal["tool", "event"]
    name: str
    status: Literal["ok", "error"] = "ok"
    input_keys: tuple[str, ...] = ()
    result_count: int | None = None
    extracted_facts: dict[str, Any] = field(default_factory=dict)
    action_report: dict[str, Any] | None = None
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialisation shape used in the judge prompt (see §4.4.3)."""
        out: dict[str, Any] = {
            "kind": self.kind,
            "name": self.name,
            "status": self.status,
        }
        if self.input_keys:
            out["input_keys"] = list(self.input_keys)
        if self.result_count is not None:
            out["result_count"] = self.result_count
        if self.extracted_facts:
            out["extracted_facts"] = dict(self.extracted_facts)
        if self.action_report:
            out["action_report"] = dict(self.action_report)
        return out


@dataclass(frozen=True, slots=True)
class TurnEvidence:
    """All side-effects observed during this ReAct turn, structured.

    Sub-divided by source so the judge LLM can weight them separately
    (pre_turn dispatch is often the cheapest fulfilment path for
    preference-class commitments; tool receipts are the canonical path
    for action-class commitments).
    """

    pre_capture_receipts: tuple[Receipt, ...] = ()
    tool_receipts: tuple[Receipt, ...] = ()

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "pre_capture_receipts": [r.to_dict() for r in self.pre_capture_receipts],
            "tool_receipts": [r.to_dict() for r in self.tool_receipts],
        }


# ──────────────────────────────────────────────────────────────
# Result envelope
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VerifierResult:
    """Canonical return shape for ``CommitmentVerifier.verify``.

    Callers access ``.verdict`` / ``.reply`` by attribute (dict access
    would hide typos as runtime ``KeyError``). ``raw_judge`` is kept
    verbatim for event payload / audit — not for business logic.
    """

    verdict: VerifierVerdict
    reply: str
    reason: str = ""
    commitment_excerpt: str = ""
    hallucination_type: HallucinationType = "none"
    raw_judge: dict[str, Any] | None = field(default=None)


# ──────────────────────────────────────────────────────────────
# Judge prompts
# ──────────────────────────────────────────────────────────────


_JUDGE_SYSTEM = """\
你是一名 Agent 执行审计员. 你要判定一条给用户的 reply 里, agent 对
"自己已经做了什么"的陈述, 是否能在本轮的 Receipt Ledger (turn_evidence)
中找到具备该动作语义的兑现证据.

只判"承诺是否被兑现", 不判工具结果的业务正确性.

【承诺 (commitment)】是 raw_reply 里任何"报告已完成/记录/设置/屏蔽/
发送/投递"类陈述句. 示例:
  - "已记录拼多多避免"
  - "已发送 greeting"
  - "已投递 5 家"
  - "已把 X 加入黑名单"

【非承诺】不应判为 unfulfilled:
  - 陈述知识 / 总结 / 推理 ("我认为…", "从信息看…")
  - 提问澄清 ("你希望我…")
  - Negative commitment ("我不会做 X", "这次仅预览") — 空 ledger 本就是正解
  - Scan / read-only 预览 ("筛选了 N 条") — 只要 ledger 里有对应 scan/pull
    receipt 即算 fulfilled

【Rubric 三维】
  1. Grounding: raw_reply 中每条承诺是否能在 turn_evidence 里找到具备
     该动作语义的 receipt?
  2. Coverage: ledger 里已发生的关键副作用 (如 persisted preference /
     triggered action) 是否在 reply 中被合理体现 (不要求逐条列举, 但
     不能反向声明"没做")?
  3. Taxonomy: 若不一致, 归属哪一类 hallucination?

【Meta-rule — 不要写死"commitment → tool"字典, 要结合语义推理】
  - 偏好/记忆类承诺的兑现证据包括但不限于:
    preference.domain.applied 事件 receipt (kind=event),
    任何 *.memory.* / memory_update 工具 receipt (kind=tool).
    只要 ledger 中有语义对齐的 receipt — 例如
    preference.domain.applied{type:avoid_company, target:拼多多} 对应
    reply "记录了不投拼多多" — 即判 fulfilled.
  - 动作类承诺 (发送/投递/提醒) 的兑现证据包括但不限于:
    带执行标志 (confirm_execute=true / auto_execute=true) 的工具
    receipt, 或明确的 *.create / *.send / *.trigger 类工具 receipt.
  - 若某条 receipt 带 ``action_report`` 字段, 这是 module handler 直接产出
    的**结构化执行报告**, 为动作类承诺的首选判据 (优先级高于
    ``extracted_facts`` 散字段):
    * ``action_report.status`` ∈ {``succeeded``, ``partial``} 且
      ``action_report.metrics`` 含正数 (``succeeded`` / ``triggered`` /
      ``delivered`` 等) → 动作已真实执行, reply 中对应的 "已 X" 类承诺
      判 fulfilled.
    * ``action_report.status == "preview"`` → 仅生成预览未实际执行,
      "已完成" 类承诺判 unfulfilled (``inference_as_fact``).
    * ``action_report.status == "failed"`` → 全部失败, "已完成" 类承诺
      判 unfulfilled (``false_absence``).
    * ``action_report.status == "skipped"`` → 被幂等检查/前置条件短路,
      整体动作**未执行**任何副作用, "已完成"类承诺判 unfulfilled
      (``inference_as_fact``).
    * reply 声明的数量 > ``action_report.metrics`` 里对应 succeeded →
      判 unfulfilled (``count_mismatch``).
    * ``action_report.details[*]`` 的 target / status / reason 可用于
      逐项对照 reply 是否编造未列出的动作.
  - shaped_reply 可能压缩/丢失细节; 判 commitment 以 raw_reply 为准,
    但 rewritten_reply 必须模仿 shaped_reply 的风格和长度.

【hallucination_type 枚举 (必填)】
  - "none"               : 无幻觉 (所有承诺都 fulfilled, 或无承诺)
  - "fabricated"         : 编造了一个根本不存在的 receipt/动作
  - "count_mismatch"     : 数量不符 (ledger 说 3 条, reply 说 5 条)
  - "fact_mismatch"      : 关键字段不符 (ledger 说屏蔽 A, reply 说屏蔽 B)
  - "inference_as_fact"  : 把推测/计划当已发生事实陈述
  - "false_absence"      : 反向幻觉, ledger 里明明做了 reply 却说没做

【强 JSON 输出 — 必须且只能返回下述 JSON, 字段一个不多一个不少】
{
  "has_commitment": true/false,
  "commitment_excerpt": "从 raw_reply 摘出的最能代表承诺的单句, 无承诺则空串",
  "fulfilled": true/false,
  "hallucination_type": "none" | "fabricated" | "count_mismatch" | "fact_mismatch" | "inference_as_fact" | "false_absence",
  "reason": "不一致时必填, 引用具体 receipt 说明 why unfulfilled",
  "rewritten_reply": "不一致时必填 — 第一人称坦诚说明'我其实没能做 X', 给用户下一步, 不提 agent/工具/调用等元词, <=150 字"
}
"""

_JUDGE_USER_TEMPLATE = """\
[user_query]
{query}

[raw_reply — LLM 原文, 判 commitment 以此为准]
{raw_reply}

[shaped_reply — 用户看到的版本, 作为 rewrite 的风格/长度基线]
{shaped_reply}

[turn_evidence — 本轮 Receipt Ledger, 作为 Grounding 判据]
{turn_evidence_json}

请严格按要求输出 JSON.
"""


_REQUIRED_JUDGE_KEYS = frozenset({
    "has_commitment",
    "commitment_excerpt",
    "fulfilled",
    "hallucination_type",
    "reason",
    "rewritten_reply",
})

# ──────────────────────────────────────────────────────────────
# Evidence rendering — ADR-001 §4.4.3 (v2.1 per-receipt whitelist)
# ──────────────────────────────────────────────────────────────

# Business facts the judge MUST see to grade commitments correctly.
# Order matters: shown earlier in the dict → survives truncation. We put
# grounding-critical counters (greeted/failed/unavailable/ok) first because
# they decide "action class" commitments; then memory/match markers for
# "record / scan" commitments.
_FACT_FIELDS_PRIORITY: tuple[str, ...] = (
    # action fulfilment counters
    "ok",
    "status",
    "greeted",
    "failed",
    "unavailable",
    "skipped",
    "needs_confirmation",
    "execution_ready",
    "daily_count",
    "daily_limit",
    # scan / match markers
    "matched_count",
    "candidates",
    "pages_scanned",
    # preference / memory markers
    "intent",
    "preference_type",
    "target",
    "target_company",
    "target_keyword",
    "preference_count",
    "source",
    "provider",
)

_MAX_STRING_VALUE = 160
_MAX_RECEIPT_CHARS = 600
_MAX_EVIDENCE_CHARS = 2400

# ADR-003 Step B.2: cap on ``details[*]`` rendered into the judge prompt
# per receipt. Beyond this we emit ``details_truncated=N`` so the judge
# still sees the overall shape without blowing the budget.
_MAX_ACTION_REPORT_DETAILS = 8


def _action_report_judge_enabled() -> bool:
    """ADR-003: let the judge prompt render ``Receipt.action_report``.

    When off, the field is dropped before prompt assembly — everything
    else (Brain injection, audit serialisation) keeps working. Use as a
    one-switch rollback if we discover the judge systematically
    misinterprets the new section.
    """
    raw = (os.getenv("PULSE_ACTION_REPORT_JUDGE") or "").strip().lower()
    return raw not in {"off", "0", "false", "no"}


def _scalarize(value: Any) -> Any:
    """Reduce ``value`` to a prompt-safe scalar, dropping noise."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if len(text) > _MAX_STRING_VALUE:
            return text[:_MAX_STRING_VALUE] + "…"
        return text
    if isinstance(value, (list, tuple)):
        # keep length signal but drop potentially huge nested payloads
        return {"__type__": "list", "len": len(value)}
    if isinstance(value, dict):
        return {"__type__": "dict", "len": len(value)}
    return str(value)[:_MAX_STRING_VALUE]


def _compact_action_report(raw: dict[str, Any]) -> dict[str, Any]:
    """Compact a serialised ``ActionReport`` for judge-prompt embedding.

    Preserves grounding-critical fields (``action``/``status``/``summary``/
    ``metrics``); caps ``details`` at :data:`_MAX_ACTION_REPORT_DETAILS`
    with a ``details_truncated`` counter so the judge sees the overall
    shape; drops ``next_steps`` and ``evidence`` (they are for the user,
    not the judge).
    """
    out: dict[str, Any] = {}
    for key in ("action", "status", "summary"):
        value = raw.get(key)
        scalar = _scalarize(value)
        if scalar is not None:
            out[key] = scalar
    metrics = raw.get("metrics")
    if isinstance(metrics, dict) and metrics:
        out["metrics"] = {
            str(k): v
            for k, v in metrics.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
    details = raw.get("details")
    if isinstance(details, list) and details:
        compact: list[dict[str, Any]] = []
        for detail in details[:_MAX_ACTION_REPORT_DETAILS]:
            if not isinstance(detail, dict):
                continue
            row: dict[str, Any] = {}
            target_scalar = _scalarize(detail.get("target"))
            if target_scalar is not None:
                row["target"] = target_scalar
            status = detail.get("status")
            if isinstance(status, str) and status:
                row["status"] = status
            reason_scalar = _scalarize(detail.get("reason"))
            if reason_scalar is not None:
                row["reason"] = reason_scalar
            if row:
                compact.append(row)
        if compact:
            out["details"] = compact
        if len(details) > _MAX_ACTION_REPORT_DETAILS:
            out["details_truncated"] = len(details) - _MAX_ACTION_REPORT_DETAILS
    return out


def _compact_receipt(receipt: Receipt) -> dict[str, Any]:
    """Serialise one receipt with a field whitelist + bounded values.

    Keeps grounding-critical ``extracted_facts`` (``greeted`` / ``failed``
    / ``unavailable`` / ``ok`` / ``status`` / memory markers) in full; any
    other keys are kept only if the whole receipt still fits in
    ``_MAX_RECEIPT_CHARS``.

    ADR-003 Step B.2a: also surfaces ``action_report`` (compacted via
    :func:`_compact_action_report`) when the handler provided one and the
    ``PULSE_ACTION_REPORT_JUDGE`` switch is on.
    """
    out: dict[str, Any] = {
        "kind": receipt.kind,
        "name": receipt.name,
        "status": receipt.status,
    }
    if receipt.input_keys:
        out["input_keys"] = list(receipt.input_keys)
    if receipt.result_count is not None:
        out["result_count"] = receipt.result_count

    facts_whitelisted: dict[str, Any] = {}
    for key in _FACT_FIELDS_PRIORITY:
        if key in receipt.extracted_facts:
            scalar = _scalarize(receipt.extracted_facts[key])
            if scalar is not None:
                facts_whitelisted[key] = scalar

    # then fold in any remaining keys, budget-permitting
    remaining = {
        k: _scalarize(v)
        for k, v in receipt.extracted_facts.items()
        if k not in facts_whitelisted and _scalarize(v) is not None
    }

    if facts_whitelisted:
        out["extracted_facts"] = facts_whitelisted

    if receipt.action_report and _action_report_judge_enabled():
        compacted = _compact_action_report(receipt.action_report)
        if compacted:
            out["action_report"] = compacted

    # try to include the remainder if we still have budget
    if remaining:
        tentative = dict(out)
        tentative_facts = dict(facts_whitelisted)
        for k, v in remaining.items():
            tentative_facts[k] = v
            tentative["extracted_facts"] = tentative_facts
            if len(json.dumps(tentative, ensure_ascii=False)) > _MAX_RECEIPT_CHARS:
                tentative_facts.pop(k, None)
                tentative["extracted_facts"] = tentative_facts
                break
        out = tentative
    return out


def _render_evidence_for_prompt(evidence: TurnEvidence) -> str:
    """Render ``TurnEvidence`` as JSON with per-receipt whitelist + bounded
    total size.

    Strategy (fail-loud via explicit truncation markers):
      1. Compact each receipt through ``_compact_receipt``.
      2. Serialise the compacted ledger. If it fits ``_MAX_EVIDENCE_CHARS``,
         return as-is.
      3. Else drop the tail of ``pre_capture_receipts`` then
         ``tool_receipts``, emitting ``truncated_pre_capture`` /
         ``truncated_tool_receipts`` counters so the judge knows the
         ledger is larger than shown.
    """
    pre_capture = [_compact_receipt(r) for r in evidence.pre_capture_receipts]
    tool = [_compact_receipt(r) for r in evidence.tool_receipts]

    payload: dict[str, Any] = {
        "pre_capture_receipts": pre_capture,
        "tool_receipts": tool,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(rendered) <= _MAX_EVIDENCE_CHARS:
        return rendered

    # First, drop oldest pre_capture receipts (they tend to be more
    # verbose and less commitment-critical than tool receipts).
    dropped_pre = 0
    while pre_capture and len(rendered) > _MAX_EVIDENCE_CHARS:
        pre_capture.pop(0)
        dropped_pre += 1
        payload["pre_capture_receipts"] = pre_capture
        if dropped_pre:
            payload["truncated_pre_capture"] = dropped_pre
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)

    dropped_tool = 0
    while tool and len(rendered) > _MAX_EVIDENCE_CHARS:
        tool.pop(0)
        dropped_tool += 1
        payload["tool_receipts"] = tool
        if dropped_tool:
            payload["truncated_tool_receipts"] = dropped_tool
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)

    return rendered


# ──────────────────────────────────────────────────────────────
# Verifier
# ──────────────────────────────────────────────────────────────


class CommitmentVerifier:
    """LLM-backed commitment-vs-execution auditor (Contract C).

    Stateless; safe to share across Brain instances. Construct with a
    router and call ``verify(...)`` per ReAct turn. See module docstring
    for invariants.
    """

    def __init__(self, llm_router: _LLMRouterLike | None) -> None:
        self._llm_router = llm_router

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def verify(
        self,
        *,
        ctx: TaskContext,
        query: str,
        raw_reply: str,
        shaped_reply: str,
        turn_evidence: TurnEvidence,
    ) -> VerifierResult:
        _ = ctx  # reserved for future per-mode audits

        if self._is_disabled():
            return VerifierResult(verdict="verified", reply=shaped_reply)

        if self._llm_router is None:
            return VerifierResult(
                verdict="degraded",
                reply=shaped_reply,
                reason="commitment_verifier_no_llm_router",
            )

        judge_payload = self._invoke_judge(
            query=query,
            raw_reply=raw_reply,
            shaped_reply=shaped_reply,
            turn_evidence=turn_evidence,
        )
        if judge_payload is None:
            return VerifierResult(
                verdict="degraded",
                reply=shaped_reply,
                reason="commitment_verifier_llm_error_or_unparseable",
            )
        return self._interpret_judge(judge_payload, shaped_reply=shaped_reply)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _is_disabled() -> bool:
        raw = (os.getenv("PULSE_COMMITMENT_VERIFIER") or "").strip().lower()
        return raw == "off"

    def _invoke_judge(
        self,
        *,
        query: str,
        raw_reply: str,
        shaped_reply: str,
        turn_evidence: TurnEvidence,
    ) -> dict[str, Any] | None:
        router = self._llm_router
        if router is None:  # defensive; verify() already guards
            return None

        evidence_json = _render_evidence_for_prompt(turn_evidence)

        messages = [
            SystemMessage(content=_JUDGE_SYSTEM),
            HumanMessage(
                content=_JUDGE_USER_TEMPLATE.format(
                    query=(query or "").strip()[:400],
                    raw_reply=(raw_reply or "").strip()[:1600],
                    shaped_reply=(shaped_reply or "").strip()[:800],
                    turn_evidence_json=evidence_json,
                )
            ),
        ]
        try:
            raw = router.invoke_text(messages, route="classification")
        except Exception as exc:
            logger.warning("commitment_verifier LLM error: %s", str(exc)[:300])
            return None

        parsed = self._parse_judge_json(raw)
        if parsed is None:
            logger.warning(
                "commitment_verifier LLM returned non-conformant JSON: %s",
                (raw or "")[:300],
            )
        return parsed

    @staticmethod
    def _parse_judge_json(raw: str) -> dict[str, Any] | None:
        text = (raw or "").strip()
        if not text:
            return None
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        if not _REQUIRED_JUDGE_KEYS.issubset(data.keys()):
            return None
        return data

    @staticmethod
    def _interpret_judge(
        payload: dict[str, Any], *, shaped_reply: str
    ) -> VerifierResult:
        has_commitment = bool(payload.get("has_commitment"))
        fulfilled = bool(payload.get("fulfilled"))
        commitment_excerpt = str(payload.get("commitment_excerpt") or "")
        reason = str(payload.get("reason") or "")
        rewritten = str(payload.get("rewritten_reply") or "").strip()
        hallucination_type_raw = str(payload.get("hallucination_type") or "").strip()

        if hallucination_type_raw not in _VALID_HALLUCINATION_TYPES:
            return VerifierResult(
                verdict="degraded",
                reply=shaped_reply,
                reason=(
                    f"judge_illegal_hallucination_type: {hallucination_type_raw!r} "
                    f"not in {sorted(_VALID_HALLUCINATION_TYPES)}"
                ),
                commitment_excerpt=commitment_excerpt,
                raw_judge=payload,
            )

        hallucination_type: HallucinationType = hallucination_type_raw  # type: ignore[assignment]

        if not has_commitment or fulfilled:
            return VerifierResult(
                verdict="verified",
                reply=shaped_reply,
                commitment_excerpt=commitment_excerpt,
                hallucination_type=hallucination_type,
                raw_judge=payload,
            )

        # has_commitment and not fulfilled
        if not rewritten:
            # Judge violated its own output contract — refuse to pass the
            # fake-success reply as-is. Downgrade to degraded so caller
            # can surface the issue but still fail-open to the user.
            return VerifierResult(
                verdict="degraded",
                reply=shaped_reply,
                reason=(
                    "judge_unfulfilled_without_rewritten_reply: "
                    + (reason or "(empty)")
                ),
                commitment_excerpt=commitment_excerpt,
                hallucination_type=hallucination_type,
                raw_judge=payload,
            )

        return VerifierResult(
            verdict="unfulfilled",
            reply=rewritten,
            reason=reason or "commitment not backed by any ledger receipt",
            commitment_excerpt=commitment_excerpt,
            hallucination_type=hallucination_type,
            raw_judge=payload,
        )


__all__ = [
    "CommitmentVerifier",
    "HallucinationType",
    "Receipt",
    "TurnEvidence",
    "VerifierResult",
    "VerifierVerdict",
]
