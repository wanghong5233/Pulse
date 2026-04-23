"""Contract C (ExecutionVerifier) guard tests — ADR-001 §4.4 (v2).

v2 surface changes vs v1:

* ``used_tools: list[str] + observations_digest: str`` inputs → replaced by
  ``turn_evidence: TurnEvidence`` (Receipt Ledger), per ADR-001 §4.4.2.
* New input ``raw_reply`` (pre-shaper) + ``shaped_reply`` (post-shaper).
  Judge evaluates commitment semantics on ``raw_reply`` (preserves action
  detail lost by shaping); ``shaped_reply`` is the rewrite baseline.
* Judge JSON output adds ``hallucination_type`` enum, surfaced in
  ``VerifierResult.hallucination_type``.

Invariants preserved from v1:

1. ``verdict == "verified"`` ⇒ reply passes through (the caller should
   use ``shaped_reply`` as the final user-visible text).
2. ``verdict == "unfulfilled"`` ⇒ reply rewritten into honest admission.
3. ``verdict == "degraded"`` ⇒ fail-OPEN; caller should pass
   ``shaped_reply`` through.
4. ``PULSE_COMMITMENT_VERIFIER=off`` short-circuits before any LLM call.
5. Classification route only (cheapest judge).

Regression target: trace_89690fb72ff8 — False-Absence **inverse** mis-fire
where pre_capture + trigger(confirm_execute=true) both delivered the
commitment but v1 verifier had no ledger to see them. v2 test
``test_verified_when_trace_89690fb72ff8_evidence_present`` locks this in.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from pulse.core.task_context import ExecutionMode, TaskContext
from pulse.core.verifier import (
    CommitmentVerifier,
    HallucinationType,
    Receipt,
    TurnEvidence,
    VerifierResult,
    VerifierVerdict,
)


# ──────────────────────────────────────────────────────────────
# Scripted fake LLM
# ──────────────────────────────────────────────────────────────


class _ScriptedJudgeRouter:
    """Replays a scripted judge JSON verdict for each call.

    A ``"__RAISE__"`` sentinel triggers a RuntimeError, for verifying the
    fail-OPEN degraded path.
    """

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def invoke_text(self, messages: list[Any], *, route: str = "default") -> str:
        self.calls.append({"messages": messages, "route": route})
        if not self._replies:
            raise RuntimeError("scripted router ran out of replies")
        reply = self._replies.pop(0)
        if reply == "__RAISE__":
            raise RuntimeError("scripted LLM failure")
        return reply


def _ctx() -> TaskContext:
    return TaskContext(mode=ExecutionMode.interactive_turn)


def _empty_evidence() -> TurnEvidence:
    return TurnEvidence(pre_capture_receipts=(), tool_receipts=())


# ──────────────────────────────────────────────────────────────
# 1. No-commitment path — pass through
# ──────────────────────────────────────────────────────────────


def test_verified_when_judge_reports_no_commitment() -> None:
    router = _ScriptedJudgeRouter([json.dumps({
        "has_commitment": False,
        "commitment_excerpt": "",
        "fulfilled": True,
        "hallucination_type": "none",
        "reason": "",
        "rewritten_reply": "",
    })])
    verifier = CommitmentVerifier(llm_router=router)

    result = verifier.verify(
        ctx=_ctx(),
        query="你好",
        raw_reply="你好,今天想聊点什么?",
        shaped_reply="你好,今天想聊点什么?",
        turn_evidence=_empty_evidence(),
    )

    assert result.verdict == "verified"
    assert result.reply == "你好,今天想聊点什么?", (
        "no-commitment path MUST pass shaped_reply through verbatim"
    )
    assert result.hallucination_type == "none"
    assert len(router.calls) == 1
    assert router.calls[0]["route"] == "classification", (
        "verifier MUST use classification route (cheapest judge) per ADR-001 §4.4.3"
    )


# ──────────────────────────────────────────────────────────────
# 2. Commitment + fulfilled (via tool receipt) — pass through
# ──────────────────────────────────────────────────────────────


def test_verified_when_commitment_matches_tool_receipt() -> None:
    """'已记录偏好' + tool receipt job.memory.record → verified."""
    router = _ScriptedJudgeRouter([json.dumps({
        "has_commitment": True,
        "commitment_excerpt": "已记录拼多多避免",
        "fulfilled": True,
        "hallucination_type": "none",
        "reason": "",
        "rewritten_reply": "",
    })])
    verifier = CommitmentVerifier(llm_router=router)

    evidence = TurnEvidence(
        pre_capture_receipts=(),
        tool_receipts=(
            Receipt(
                kind="tool",
                name="job.memory.record",
                status="ok",
                input_keys=("item",),
                result_count=None,
                extracted_facts={"type": "avoid_company", "target": "拼多多"},
                timestamp=0.0,
            ),
        ),
    )
    result = verifier.verify(
        ctx=_ctx(),
        query="拼多多别投",
        raw_reply="好的,已记录拼多多避免偏好。",
        shaped_reply="好的,已记录拼多多避免偏好。",
        turn_evidence=evidence,
    )

    assert result.verdict == "verified"
    assert result.reply == "好的,已记录拼多多避免偏好。"
    assert result.commitment_excerpt == "已记录拼多多避免"
    assert result.hallucination_type == "none"


# ──────────────────────────────────────────────────────────────
# 3. ⭐ trace_89690fb72ff8 regression: pre_capture + trigger →
#    '记录了投递意向' MUST be verified (v1 falsely said unfulfilled)
# ──────────────────────────────────────────────────────────────


def test_verified_when_trace_89690fb72ff8_evidence_present() -> None:
    """v1 False-Absence inverse mis-fire regression guard.

    Real trace: user "拼多多别投+字节可投,开始投递"; pre_capture dispatched
    preference.domain.applied ×2 (avoid 拼多多 / favor 字节), and ReAct
    called job.greet.scan + job.greet.trigger(confirm_execute=true). Draft
    reply contained "虽然投递未能成功,但我记录了你的投递意向".

    v1 judge only saw used_tools=[scan, trigger] (no memory.* tool) and
    falsely flagged "记录了投递意向" as unfulfilled, rewriting the reply
    into an honest-admission that the Agent HAD actually fulfilled.

    v2: judge sees preference.domain.applied receipts + structured facts
    on the trigger receipt (confirm_execute=True, triggered=5). Must
    judge verified (hallucination_type=none).
    """
    router = _ScriptedJudgeRouter([json.dumps({
        "has_commitment": True,
        "commitment_excerpt": "记录了你的投递意向",
        "fulfilled": True,
        "hallucination_type": "none",
        "reason": (
            "ledger has preference.domain.applied{avoid_company:拼多多,"
            "favor_company:字节跳动} + job.greet.trigger{confirm_execute=true,"
            "triggered=5}; covers '投递意向' semantics"
        ),
        "rewritten_reply": "",
    })])
    verifier = CommitmentVerifier(llm_router=router)

    evidence = TurnEvidence(
        pre_capture_receipts=(
            Receipt(
                kind="event",
                name="preference.domain.applied",
                status="ok",
                extracted_facts={
                    "domain": "job",
                    "op": "memory.record",
                    "type": "avoid_company",
                    "target": "拼多多",
                },
            ),
            Receipt(
                kind="event",
                name="preference.domain.applied",
                status="ok",
                extracted_facts={
                    "domain": "job",
                    "op": "memory.record",
                    "type": "favor_company",
                    "target": "字节跳动",
                },
            ),
        ),
        tool_receipts=(
            Receipt(
                kind="tool",
                name="job.greet.scan",
                status="ok",
                input_keys=("keyword", "max_items", "job_type"),
                result_count=10,
                extracted_facts={"scan_handle": "sh_cfc59ca1dc28", "scanned": 10},
            ),
            Receipt(
                kind="tool",
                name="job.greet.trigger",
                status="ok",
                input_keys=("keyword", "scan_handle", "batch_size",
                            "confirm_execute", "fetch_detail", "job_type"),
                result_count=5,
                extracted_facts={
                    "confirm_execute": True,
                    "triggered": 5,
                    "scan_handle_reused": True,
                },
            ),
        ),
    )

    raw_reply = (
        "已为你筛选并尝试投递以下5个大模型应用开发 Agent 实习岗位"
        "(全部为上海小厂/初创或字节跳动,均未包含拼多多): ..."
    )
    shaped_reply = (
        "我已为你筛选了5个适合的实习岗位,均为字节跳动或上海的小厂,"
        "拼多多的岗位已排除。虽然投递未能成功,但我记录了你的投递意向。"
    )

    result = verifier.verify(
        ctx=_ctx(),
        query="拼多多别投,字节可投,开始投递",
        raw_reply=raw_reply,
        shaped_reply=shaped_reply,
        turn_evidence=evidence,
    )

    assert result.verdict == "verified", (
        "trace_89690fb72ff8 regression: judge now has receipts for both "
        "pre_capture preference updates and the trigger-with-confirm — "
        "MUST NOT flag '记录了投递意向' as unfulfilled"
    )
    assert result.reply == shaped_reply, (
        "verified path returns shaped_reply (what the user sees)"
    )
    assert result.hallucination_type == "none"


# ──────────────────────────────────────────────────────────────
# 4. Commitment + UN-fulfilled — rewrite (classic trace_e48a6be0c90e)
# ──────────────────────────────────────────────────────────────


def test_unfulfilled_rewrites_reply_with_hallucination_type() -> None:
    """trace_e48a6be0c90e original case: '已记录' but evidence is empty."""
    rewritten = (
        "抱歉,我其实没能把'拼多多别投'真正写入你的偏好记忆,"
        "刚才只是嘴上答应了。麻烦再跟我说一次,我会先调记忆工具再回复。"
    )
    router = _ScriptedJudgeRouter([json.dumps({
        "has_commitment": True,
        "commitment_excerpt": "已记录以下偏好: 拼多多进入避免列表",
        "fulfilled": False,
        "hallucination_type": "false_absence",
        "reason": (
            "reply 声明 '已记录拼多多进入避免列表', turn_evidence 中既无 "
            "preference.domain.applied 也无 memory.record receipt"
        ),
        "rewritten_reply": rewritten,
    })])
    verifier = CommitmentVerifier(llm_router=router)

    result = verifier.verify(
        ctx=_ctx(),
        query="拼多多别投",
        raw_reply="好的,已记录以下偏好: 拼多多进入避免列表。",
        shaped_reply="好的,已记录以下偏好: 拼多多进入避免列表。",
        turn_evidence=_empty_evidence(),
    )

    assert result.verdict == "unfulfilled"
    assert result.reply == rewritten, (
        "unfulfilled MUST replace reply with rewritten_reply — never the "
        "original fake-success text"
    )
    assert result.hallucination_type == "false_absence"
    assert result.reason
    assert "拼多多" in result.commitment_excerpt


# ──────────────────────────────────────────────────────────────
# 5. Unfulfilled but empty rewritten → degraded (contract violation)
# ──────────────────────────────────────────────────────────────


def test_unfulfilled_falls_back_to_degraded_when_rewritten_empty() -> None:
    router = _ScriptedJudgeRouter([json.dumps({
        "has_commitment": True,
        "commitment_excerpt": "已投递",
        "fulfilled": False,
        "hallucination_type": "fabricated",
        "reason": "no apply tool invoked",
        "rewritten_reply": "",
    })])
    verifier = CommitmentVerifier(llm_router=router)

    result = verifier.verify(
        ctx=_ctx(),
        query="投 3 个",
        raw_reply="已投递 3 家公司。",
        shaped_reply="已投递 3 家公司。",
        turn_evidence=_empty_evidence(),
    )

    assert result.verdict == "degraded", (
        "judge returning (not fulfilled, empty rewritten) MUST be downgraded "
        "to 'degraded' — never pass the fake success to the user"
    )
    assert result.reply == "已投递 3 家公司。"


# ──────────────────────────────────────────────────────────────
# 6. Illegal hallucination_type enum → degraded
# ──────────────────────────────────────────────────────────────


def test_degraded_when_hallucination_type_illegal() -> None:
    """Judge output contract requires enum in the defined set.

    Off-enum value (e.g. 'whatever') → degraded, so downstream analytics
    never have to cope with free-form strings in payloads.
    """
    router = _ScriptedJudgeRouter([json.dumps({
        "has_commitment": True,
        "commitment_excerpt": "已发送",
        "fulfilled": True,
        "hallucination_type": "whatever",  # illegal
        "reason": "",
        "rewritten_reply": "",
    })])
    verifier = CommitmentVerifier(llm_router=router)

    result = verifier.verify(
        ctx=_ctx(),
        query="发邮件",
        raw_reply="已发送。",
        shaped_reply="已发送。",
        turn_evidence=_empty_evidence(),
    )

    assert result.verdict == "degraded"


# ──────────────────────────────────────────────────────────────
# 7. Degraded paths — fail-OPEN
# ──────────────────────────────────────────────────────────────


def test_degraded_when_llm_raises() -> None:
    router = _ScriptedJudgeRouter(["__RAISE__"])
    verifier = CommitmentVerifier(llm_router=router)

    result = verifier.verify(
        ctx=_ctx(),
        query="any",
        raw_reply="some reply",
        shaped_reply="some reply",
        turn_evidence=_empty_evidence(),
    )

    assert result.verdict == "degraded"
    assert result.reply == "some reply", (
        "fail-OPEN: verifier's own error MUST NOT block the user"
    )
    assert result.reason


def test_degraded_when_llm_returns_non_json() -> None:
    router = _ScriptedJudgeRouter(["sorry I can't do that"])
    verifier = CommitmentVerifier(llm_router=router)

    result = verifier.verify(
        ctx=_ctx(),
        query="any",
        raw_reply="some reply",
        shaped_reply="some reply",
        turn_evidence=_empty_evidence(),
    )

    assert result.verdict == "degraded"
    assert result.reply == "some reply"


def test_degraded_when_llm_returns_missing_required_keys() -> None:
    router = _ScriptedJudgeRouter([json.dumps({"has_commitment": True})])
    verifier = CommitmentVerifier(llm_router=router)

    result = verifier.verify(
        ctx=_ctx(),
        query="any",
        raw_reply="some reply",
        shaped_reply="some reply",
        turn_evidence=_empty_evidence(),
    )

    assert result.verdict == "degraded"
    assert result.reply == "some reply"


# ──────────────────────────────────────────────────────────────
# 8. Env kill-switch
# ──────────────────────────────────────────────────────────────


def test_off_toggle_short_circuits_without_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PULSE_COMMITMENT_VERIFIER=off` MUST skip LLM entirely (ADR-001 §5.1)."""
    monkeypatch.setenv("PULSE_COMMITMENT_VERIFIER", "off")
    router = _ScriptedJudgeRouter([])  # would raise on any call
    verifier = CommitmentVerifier(llm_router=router)

    result = verifier.verify(
        ctx=_ctx(),
        query="anything",
        raw_reply="I totally did the thing",
        shaped_reply="I totally did the thing",
        turn_evidence=_empty_evidence(),
    )

    assert result.verdict == "verified"
    assert result.reply == "I totally did the thing"
    assert router.calls == []


def test_off_toggle_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_COMMITMENT_VERIFIER", "OFF")
    router = _ScriptedJudgeRouter([])
    verifier = CommitmentVerifier(llm_router=router)

    result = verifier.verify(
        ctx=_ctx(),
        query="x",
        raw_reply="y",
        shaped_reply="y",
        turn_evidence=_empty_evidence(),
    )
    assert result.verdict == "verified"
    assert router.calls == []


# ──────────────────────────────────────────────────────────────
# 9. No router → graceful degrade
# ──────────────────────────────────────────────────────────────


def test_verify_without_llm_router_degrades_openly() -> None:
    verifier = CommitmentVerifier(llm_router=None)

    result = verifier.verify(
        ctx=_ctx(),
        query="x",
        raw_reply="y",
        shaped_reply="y",
        turn_evidence=_empty_evidence(),
    )

    assert result.verdict == "degraded"
    assert result.reply == "y"
    assert result.reason


# ──────────────────────────────────────────────────────────────
# 10. Return contract stability
# ──────────────────────────────────────────────────────────────


def test_result_is_verifier_result_not_plain_dict() -> None:
    router = _ScriptedJudgeRouter([json.dumps({
        "has_commitment": False,
        "commitment_excerpt": "",
        "fulfilled": True,
        "hallucination_type": "none",
        "reason": "",
        "rewritten_reply": "",
    })])
    verifier = CommitmentVerifier(llm_router=router)

    result = verifier.verify(
        ctx=_ctx(),
        query="x",
        raw_reply="y",
        shaped_reply="y",
        turn_evidence=_empty_evidence(),
    )

    assert isinstance(result, VerifierResult)
    assert result.verdict in ("verified", "unfulfilled", "degraded")
    assert result.hallucination_type in HallucinationType.__args__  # type: ignore[attr-defined]


def test_verdict_type_alias_is_stable() -> None:
    assert "verified" in VerifierVerdict.__args__  # type: ignore[attr-defined]
    assert "unfulfilled" in VerifierVerdict.__args__  # type: ignore[attr-defined]
    assert "degraded" in VerifierVerdict.__args__  # type: ignore[attr-defined]


def test_hallucination_type_enum_covers_five_categories_plus_none() -> None:
    """AgentHallu (arXiv 2601.06818) + NabaOS (arXiv 2603.10060) taxonomy."""
    expected = {
        "none", "fabricated", "count_mismatch",
        "fact_mismatch", "inference_as_fact", "false_absence",
    }
    assert set(HallucinationType.__args__) == expected  # type: ignore[attr-defined]


# ──────────────────────────────────────────────────────────────
# 11. Raw vs shaped: judge commitment detection uses raw; rewrite uses shaped
# ──────────────────────────────────────────────────────────────


def test_judge_prompt_receives_both_raw_and_shaped_reply() -> None:
    """raw_reply carries full action detail (e.g. company list lost by
    shaper); shaped_reply is the rewrite baseline. Both MUST flow into
    the judge prompt.
    """
    router = _ScriptedJudgeRouter([json.dumps({
        "has_commitment": False,
        "commitment_excerpt": "",
        "fulfilled": True,
        "hallucination_type": "none",
        "reason": "",
        "rewritten_reply": "",
    })])
    verifier = CommitmentVerifier(llm_router=router)

    raw = "full reply with detailed list of 5 companies: 字节跳动, 维智, ..."
    shaped = "为你筛选了5个岗位,请点击链接查看。"
    _ = verifier.verify(
        ctx=_ctx(),
        query="找 5 个",
        raw_reply=raw,
        shaped_reply=shaped,
        turn_evidence=_empty_evidence(),
    )

    assert len(router.calls) == 1
    prompt_text = "\n".join(
        str(getattr(m, "content", "")) for m in router.calls[0]["messages"]
    )
    assert raw[:40] in prompt_text, "raw_reply MUST be visible to judge"
    assert shaped[:20] in prompt_text, "shaped_reply MUST also be visible"


# ──────────────────────────────────────────────────────────────
# 12. TurnEvidence receipts serialize into judge prompt
# ──────────────────────────────────────────────────────────────


def test_turn_evidence_receipts_flow_into_judge_prompt() -> None:
    """Key facts from receipts MUST be reachable by the judge LLM.

    Without this, the receipt-ledger upgrade is architectural-only with
    no behavioural change. We verify that both pre_capture kind=event
    receipts and tool kind=tool receipts appear in the prompt text.
    """
    router = _ScriptedJudgeRouter([json.dumps({
        "has_commitment": True,
        "commitment_excerpt": "已屏蔽拼多多",
        "fulfilled": True,
        "hallucination_type": "none",
        "reason": "pre_capture receipt covers the commitment",
        "rewritten_reply": "",
    })])
    verifier = CommitmentVerifier(llm_router=router)

    evidence = TurnEvidence(
        pre_capture_receipts=(
            Receipt(
                kind="event",
                name="preference.domain.applied",
                status="ok",
                extracted_facts={
                    "domain": "job",
                    "op": "memory.record",
                    "type": "avoid_company",
                    "target": "拼多多",
                },
            ),
        ),
        tool_receipts=(
            Receipt(
                kind="tool",
                name="job.greet.trigger",
                status="ok",
                input_keys=("confirm_execute", "batch_size"),
                result_count=5,
                extracted_facts={"confirm_execute": True, "triggered": 5},
            ),
        ),
    )

    _ = verifier.verify(
        ctx=_ctx(),
        query="拼多多别投",
        raw_reply="已屏蔽拼多多,并投递了 5 个其他岗位。",
        shaped_reply="已屏蔽拼多多,并投递了 5 个岗位。",
        turn_evidence=evidence,
    )

    prompt_text = "\n".join(
        str(getattr(m, "content", "")) for m in router.calls[0]["messages"]
    )
    assert "preference.domain.applied" in prompt_text
    assert "avoid_company" in prompt_text
    assert "拼多多" in prompt_text
    assert "job.greet.trigger" in prompt_text
    assert "confirm_execute" in prompt_text


# ──────────────────────────────────────────────────────────────
# 13. P2 · per-receipt whitelist — grounding-critical counters
#     (greeted/failed/unavailable) MUST survive truncation
# ──────────────────────────────────────────────────────────────


def test_grounding_critical_counters_survive_evidence_truncation() -> None:
    """trace_16e97afe3ffc regression — when ledger is large, the judge
    MUST still see ``greeted`` / ``failed`` / ``unavailable`` on the
    trigger receipt, because those decide whether a "已投递 5 家"
    commitment is grounded.

    v2.0 rendered ``to_prompt_dict()`` then chopped the string at 2400
    chars; with many pre_capture receipts the counters got cut off.
    v2.1 uses ``_compact_receipt`` + per-receipt whitelist.
    """
    # Flood pre_capture with many receipts carrying long text values
    # so a naive serializer would push the trigger receipt's counters
    # past the 2400-char cutoff.
    def _pad(i: int) -> dict[str, Any]:
        return {
            "domain": "job",
            "op": "memory.record",
            "type": "avoid_company",
            "target": f"很长的公司名_{i}_" + ("填充" * 40),
            "note": "preserved-but-bounded-" + ("x" * 200),
        }

    pre = tuple(
        Receipt(
            kind="event",
            name="preference.domain.applied",
            status="ok",
            extracted_facts=_pad(i),
        )
        for i in range(20)
    )
    tool = (
        Receipt(
            kind="tool",
            name="job.greet.trigger",
            status="ok",
            input_keys=("keyword", "batch_size", "confirm_execute"),
            result_count=5,
            extracted_facts={
                "ok": True,
                "greeted": 0,
                "failed": 0,
                "unavailable": 5,
                "needs_confirmation": False,
                "execution_ready": True,
            },
        ),
    )
    router = _ScriptedJudgeRouter([json.dumps({
        "has_commitment": True,
        "commitment_excerpt": "已投递 5 家",
        "fulfilled": False,
        "hallucination_type": "false_absence",
        "reason": "ledger shows greeted=0, unavailable=5; no real send",
        "rewritten_reply": "我其实没能完成投递, 平台执行器未配置, 请稍后重试。",
    })])
    verifier = CommitmentVerifier(llm_router=router)

    _ = verifier.verify(
        ctx=_ctx(),
        query="投 5 家",
        raw_reply="已投递 5 家公司。",
        shaped_reply="已投递 5 家公司。",
        turn_evidence=TurnEvidence(pre_capture_receipts=pre, tool_receipts=tool),
    )

    prompt_text = "\n".join(
        str(getattr(m, "content", "")) for m in router.calls[0]["messages"]
    )
    # Grounding-critical facts on the trigger receipt MUST appear:
    assert "job.greet.trigger" in prompt_text, (
        "tool_receipts MUST NOT be dropped; they carry the 'did we send?' signal"
    )
    assert "greeted" in prompt_text
    assert "unavailable" in prompt_text
    assert "\"unavailable\": 5" in prompt_text or "unavailable: 5" in prompt_text, (
        "unavailable=5 is THE signal that distinguishes 'infra refused' "
        "from 'send failed'; it MUST survive truncation"
    )


def test_compact_receipt_strips_noise_but_keeps_priority_fields() -> None:
    """Unit test for the per-receipt whitelist transform."""
    from pulse.core.verifier import _compact_receipt

    r = Receipt(
        kind="tool",
        name="job.greet.trigger",
        status="ok",
        input_keys=("keyword", "confirm_execute"),
        result_count=5,
        extracted_facts={
            "ok": True,
            "greeted": 0,
            "failed": 0,
            "unavailable": 5,
            # noise: huge list / dict / very long string — get reduced to
            # len-only tag or truncated-string.
            "matched_details": [{"deep": "x" * 5000} for _ in range(50)],
            "errors": ["x" * 500],
            "long_note": "preserve-prefix-but-cut-" + ("y" * 1000),
        },
    )
    out = _compact_receipt(r)
    assert out["kind"] == "tool"
    assert out["name"] == "job.greet.trigger"
    assert out["result_count"] == 5
    facts = out["extracted_facts"]
    # Priority whitelist preserved.
    assert facts["greeted"] == 0
    assert facts["unavailable"] == 5
    assert facts["ok"] is True
    # Nested list is replaced by a compact length tag, not expanded.
    if "matched_details" in facts:
        assert facts["matched_details"].get("__type__") == "list"
    # Long scalar is truncated under 200 chars.
    if "long_note" in facts:
        assert len(facts["long_note"]) <= 200


def test_truncation_marker_emitted_when_ledger_exceeds_budget() -> None:
    """If compaction still doesn't fit, ``truncated_pre_capture`` /
    ``truncated_tool_receipts`` counters MUST surface in the prompt.
    Silent truncation would let the judge hallucinate absence."""
    from pulse.core.verifier import _render_evidence_for_prompt

    # pad each receipt enough that even after compaction we overflow
    fat_facts = {"intent": "preference.record", "target": "x" * 120, "note": "y" * 120}
    pre = tuple(
        Receipt(
            kind="event",
            name="preference.domain.applied",
            status="ok",
            extracted_facts=dict(fat_facts),
        )
        for _ in range(60)
    )
    rendered = _render_evidence_for_prompt(
        TurnEvidence(pre_capture_receipts=pre, tool_receipts=())
    )
    assert len(rendered) <= 2400 + 200, (
        f"rendered evidence must stay near the budget; got {len(rendered)} chars"
    )
    assert "truncated_pre_capture" in rendered, (
        "when we had to drop receipts, the judge MUST be told how many"
    )


# ──────────────────────────────────────────────────────────────
# 8. ADR-003 — ActionReport is the preferred grounding signal
# ──────────────────────────────────────────────────────────────


def _receipt_with_action_report(
    *,
    action_report: dict[str, Any],
    name: str = "job.greet.trigger",
    extracted_facts: dict[str, Any] | None = None,
) -> Receipt:
    return Receipt(
        kind="tool",
        name=name,
        status="ok",
        input_keys=("keyword", "confirm_execute"),
        result_count=(action_report.get("metrics", {}) or {}).get("attempted"),
        extracted_facts=dict(extracted_facts or {}),
        action_report=dict(action_report),
    )


def test_compact_receipt_surfaces_action_report_by_default(monkeypatch) -> None:
    """ADR-003 §Data flow: receipts carrying ``action_report`` must ship
    ``{action, status, summary, metrics}`` into the judge prompt. Details
    are the primary grounding signal for action-class commitments — if
    we don't render them, the judge has to re-derive intent from
    ``extracted_facts`` scalars, which is exactly the trace_34682759d5e7
    failure mode."""
    from pulse.core.verifier import _compact_receipt

    monkeypatch.delenv("PULSE_ACTION_REPORT_JUDGE", raising=False)

    receipt = _receipt_with_action_report(
        action_report={
            "action": "job.greet",
            "status": "succeeded",
            "summary": "已投递 1 个岗位",
            "details": [
                {"target": "AIGC视觉生成实习", "status": "succeeded"},
            ],
            "metrics": {"attempted": 1, "succeeded": 1, "failed": 0},
            # next_steps / evidence are user-facing; judge prompt must drop them
            "next_steps": ["check dashboard"],
            "evidence": {"trace_id": "trace_x"},
        },
        extracted_facts={"greeted": 1, "failed": 0},
    )
    out = _compact_receipt(receipt)
    assert "action_report" in out, (
        "action_report MUST be forwarded to the judge prompt; it is the "
        "single source of truth for action-class commitments"
    )
    ar = out["action_report"]
    assert ar["action"] == "job.greet"
    assert ar["status"] == "succeeded"
    assert ar["summary"] == "已投递 1 个岗位"
    assert ar["metrics"] == {"attempted": 1, "succeeded": 1, "failed": 0}
    assert ar["details"] and ar["details"][0]["target"] == "AIGC视觉生成实习"
    assert "next_steps" not in ar, "next_steps is user-facing only"
    assert "evidence" not in ar, "evidence block is not judge-grade"


def test_compact_receipt_drops_action_report_when_judge_switch_off(monkeypatch) -> None:
    """Kill-switch: ``PULSE_ACTION_REPORT_JUDGE=off`` must degrade
    cleanly — we drop the action_report block from the prompt while
    keeping ``extracted_facts`` scalars intact, so rollback is safe."""
    from pulse.core.verifier import _compact_receipt

    monkeypatch.setenv("PULSE_ACTION_REPORT_JUDGE", "off")
    receipt = _receipt_with_action_report(
        action_report={
            "action": "job.greet",
            "status": "succeeded",
            "summary": "已投递 1 个岗位",
            "metrics": {"attempted": 1, "succeeded": 1, "failed": 0},
        },
        extracted_facts={"greeted": 1, "failed": 0},
    )
    out = _compact_receipt(receipt)
    assert "action_report" not in out, (
        "PULSE_ACTION_REPORT_JUDGE=off must NOT render the report block"
    )
    # Back-compat path still works: scalar facts stay.
    assert out["extracted_facts"]["greeted"] == 1


def test_compact_action_report_caps_details_with_truncation_marker() -> None:
    """Large ``details[*]`` arrays must be capped at
    ``_MAX_ACTION_REPORT_DETAILS`` and expose a ``details_truncated``
    counter, otherwise the judge prompt cost explodes on batch runs."""
    from pulse.core.verifier import _MAX_ACTION_REPORT_DETAILS, _compact_action_report

    big = {
        "action": "job.greet",
        "status": "partial",
        "summary": "投递了 3/20 个岗位",
        "metrics": {"attempted": 20, "succeeded": 3, "failed": 17},
        "details": [
            {"target": f"岗位{i}", "status": "succeeded" if i < 3 else "failed"}
            for i in range(20)
        ],
    }
    out = _compact_action_report(big)
    assert len(out["details"]) == _MAX_ACTION_REPORT_DETAILS
    assert out["details_truncated"] == 20 - _MAX_ACTION_REPORT_DETAILS
    assert out["metrics"] == {"attempted": 20, "succeeded": 3, "failed": 17}


def test_verified_when_action_report_shows_succeeded_trace_34682759d5e7() -> None:
    """⭐ Regression for trace_34682759d5e7 — the user asked for 1 posting,
    the greeter handler actually submitted 1, but Pulse's shaped reply
    said "其实没能完成投递" (false-absence hallucination).

    Pre-ADR-003 root cause: Brain and Verifier consumed *different*
    projections of the observation. Verifier only saw ``extracted_facts``
    scalars; the judge couldn't tell "receipt has greeted=1 but reply
    denies it" from a genuine "nothing happened" case, so it dropped to
    unfulfilled and rewrote a legitimate success into an apology.

    ADR-003 fix: Brain snapshots ``__action_report__`` → Receipt.action_report
    → compacted into the judge prompt. This test pins that a judge that
    properly reads the report (returns ``verified`` / ``none``) lets the
    success reply through intact.
    """
    router = _ScriptedJudgeRouter([json.dumps({
        "has_commitment": True,
        "commitment_excerpt": "已投递 1 个大模型应用开发实习岗位",
        "fulfilled": True,
        "hallucination_type": "none",
        "reason": (
            "action_report.status=succeeded, action_report.metrics.succeeded=1; "
            "details[0].target=AIGC视觉生成实习 matches reply's singular claim"
        ),
        "rewritten_reply": "",
    })])
    verifier = CommitmentVerifier(llm_router=router)

    evidence = TurnEvidence(
        pre_capture_receipts=(),
        tool_receipts=(
            _receipt_with_action_report(
                action_report={
                    "action": "job.greet",
                    "status": "succeeded",
                    "summary": "已投递 1 个岗位",
                    "details": [
                        {"target": "AIGC视觉生成实习", "status": "succeeded",
                         "url": "https://example.com/job/1"},
                    ],
                    "metrics": {"attempted": 1, "succeeded": 1, "failed": 0},
                },
                extracted_facts={
                    "ok": True, "greeted": 1, "failed": 0, "unavailable": 0,
                    "needs_confirmation": False, "execution_ready": True,
                },
            ),
        ),
    )

    raw_reply = "已为你投递了 1 个合适岗位: AIGC视觉生成实习。"
    shaped_reply = "已投递 1 个大模型应用开发实习岗位 (AIGC视觉生成实习)。"

    result = verifier.verify(
        ctx=_ctx(),
        query="投 1 个大模型应用开发实习",
        raw_reply=raw_reply,
        shaped_reply=shaped_reply,
        turn_evidence=evidence,
    )

    assert result.verdict == "verified", (
        "trace_34682759d5e7 regression: receipt carries action_report with "
        "status=succeeded and succeeded=1; verdict MUST be verified (NOT "
        "rewritten into a 'I failed' apology)"
    )
    assert result.reply == shaped_reply
    assert result.hallucination_type == "none"

    # And the prompt the judge actually saw MUST include the report block.
    prompt_text = "\n".join(
        str(getattr(m, "content", "")) for m in router.calls[0]["messages"]
    )
    assert "action_report" in prompt_text, (
        "the judge prompt MUST surface action_report — that is the fix"
    )
    assert "succeeded" in prompt_text
    assert "AIGC视觉生成实习" in prompt_text


def test_unfulfilled_when_action_report_is_preview_not_run() -> None:
    """Dual side of the regression: if the handler only produced a
    preview (``status=preview``) but the reply claims "已投递", that IS a
    real unfulfilled / inference_as_fact case. ADR-003 rule must still
    route it to rewrite."""
    router = _ScriptedJudgeRouter([json.dumps({
        "has_commitment": True,
        "commitment_excerpt": "已投递 1 个岗位",
        "fulfilled": False,
        "hallucination_type": "inference_as_fact",
        "reason": (
            "action_report.status=preview — handler only built a preview, "
            "no real send occurred; reply claims '已投递' is inference as fact"
        ),
        "rewritten_reply": "这只是候选清单, 还没真正发送; 确认后再投。",
    })])
    verifier = CommitmentVerifier(llm_router=router)

    evidence = TurnEvidence(
        pre_capture_receipts=(),
        tool_receipts=(
            _receipt_with_action_report(
                action_report={
                    "action": "job.greet",
                    "status": "preview",
                    "summary": "筛选了 1 个候选岗位, 等待确认后再投",
                    "metrics": {"candidates": 1, "attempted": 0, "succeeded": 0},
                },
                extracted_facts={
                    "ok": True, "greeted": 0, "failed": 0,
                    "needs_confirmation": True, "execution_ready": True,
                },
            ),
        ),
    )

    result = verifier.verify(
        ctx=_ctx(),
        query="投 1 个",
        raw_reply="已投递 1 个岗位。",
        shaped_reply="已投递 1 个岗位。",
        turn_evidence=evidence,
    )

    assert result.verdict == "unfulfilled"
    assert result.hallucination_type == "inference_as_fact"
    assert "候选" in result.reply or "确认" in result.reply, (
        "preview-as-succeeded is a hallucination — user must see the honest "
        "rewrite, not the fake success"
    )


# NOTE: skipped meta-rule 的防线 (status=skipped 但 reply 声明已完成 →
# unfulfilled) 写在 verifier._JUDGE_SYSTEM 的 prompt 里, 是**让 judge
# LLM 按提示推理**. 用 _ScriptedJudgeRouter 直接预置 fulfilled=False
# 的 JSON 去测, 测的是 "CommitmentVerifier 解析 JSON 的管道", 而不是
# prompt 规则本身 — 前者已被 preview 等 case 充分覆盖. 真正能验 skipped
# rule 的是: (1) LLM judge contract test (跑真 LLM), 或 (2) 一条真实
# game.checkin bug 复现 trace 回放. 现在这两者都不做 — 先写在 prompt
# + ADR-003 里, 等真实 trace 出现再补 (宪法: 不造假 test).
