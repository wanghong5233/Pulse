"""ADR-003 Step B.3 — ``job.greet.trigger`` ActionReport shape tests.

The ``run_trigger`` handler has four structural exit branches, each of
which MUST produce an ``ActionReport`` that Brain + Verifier can ground
on:

1. ``scan_miss``   — caller passed an unknown/expired ``scan_handle``;
2. ``not_ready``   — connector refuses (mode gap / degraded provider);
3. ``preview``     — HITL path (``confirm_execute=False`` when policy
   requires confirmation); only a shortlist was produced;
4. ``run``         — real send loop; per-item details + metrics.

We pin the **shape** (status / summary / metrics counters / details
status mapping), not specific wording: the Verifier judge consumes the
structured fields, not prose.

These are pure function tests on ``_build_trigger_action_report`` —
chosen deliberately so they stay stable across refactors that re-wire
how ``run_trigger`` calls its helpers.
"""

from __future__ import annotations

from pulse.core.action_report import ACTION_REPORT_KEY, ActionReport
from pulse.modules.job.greet.service import (
    _UNAVAILABLE_STATUSES,
    _build_trigger_action_report,
)


# ──────────────────────────────────────────────────────────────
# 1. scan_miss — early short-circuit, no send attempt at all
# ──────────────────────────────────────────────────────────────


def test_scan_miss_report_is_failed_with_zero_metrics() -> None:
    report = _build_trigger_action_report(
        outcome="scan_miss",
        reason="scan_handle 'sh_xxx' not in cache or TTL expired; call "
               "job.greet.scan first",
        source="boss_recorder",
        trace_id="tr_x",
    )
    assert isinstance(report, ActionReport)
    assert report.action == "job.greet"
    assert report.status == "failed"
    # summary must carry the reason so a generic LLM can narrate it
    assert "scan_handle" in report.summary
    # metrics floor: even a short-circuit must publish attempted=0,
    # otherwise the judge has no anchor for "nothing happened"
    assert report.metrics["attempted"] == 0
    assert report.metrics["succeeded"] == 0
    assert report.metrics["failed"] == 0
    assert report.metrics["unavailable"] == 0
    assert report.details == ()
    assert report.evidence.get("trace_id") == "tr_x"
    assert report.evidence.get("source") == "boss_recorder"


# ──────────────────────────────────────────────────────────────
# 2. not_ready — connector gap
# ──────────────────────────────────────────────────────────────


def test_not_ready_report_is_failed_with_reason_embedded() -> None:
    report = _build_trigger_action_report(
        outcome="not_ready",
        reason=("connector is not execution-ready "
                "(provider=boss_web_search, source=boss_web_search)"),
        source="boss_web_search",
        trace_id="tr_y",
    )
    assert report.action == "job.greet"
    assert report.status == "failed"
    assert "execution-ready" in report.summary
    assert report.metrics["succeeded"] == 0
    assert report.evidence.get("source") == "boss_web_search"


# ──────────────────────────────────────────────────────────────
# 3. preview — shortlisted but not sent
# ──────────────────────────────────────────────────────────────


def test_preview_report_is_preview_status_no_details() -> None:
    """Preview must NOT emit per-item ``succeeded`` details — that would
    let the LLM/Verifier mistake 'we shortlisted 5' for 'we applied to 5'.
    The fail-loud signal is ``status=preview`` + ``candidates=N`` in
    metrics; ``attempted`` / ``succeeded`` stay 0."""
    report = _build_trigger_action_report(
        outcome="preview",
        preview_candidate_count=3,
        source="boss_mcp",
        trace_id="tr_z",
    )
    assert report.action == "job.greet"
    assert report.status == "preview"
    assert "3" in report.summary and "候选" in report.summary
    assert report.metrics["candidates"] == 3
    assert report.metrics["attempted"] == 0
    assert report.metrics["succeeded"] == 0
    assert report.details == (), (
        "preview must not render per-item details; that would invite the "
        "LLM to narrate them as already-done"
    )
    assert report.next_steps, "preview must tell the user how to proceed"


def test_preview_report_empty_candidate_uses_no_candidates_summary() -> None:
    report = _build_trigger_action_report(
        outcome="preview",
        preview_candidate_count=0,
    )
    assert report.status == "preview"
    assert "没有候选" in report.summary
    assert report.metrics["candidates"] == 0


# ──────────────────────────────────────────────────────────────
# 4. run — real send loop (the trace_34682759d5e7 surface)
# ──────────────────────────────────────────────────────────────


def _detail(title: str, status: str, **extras) -> dict:
    row = {"job_title": title, "status": status}
    row.update(extras)
    return row


def test_run_report_all_greeted_is_succeeded_with_per_item_details() -> None:
    """trace_34682759d5e7 fix: greeted=1 of 1 → status=succeeded.

    Every per-item ``ActionDetail.target`` equals the job title so the
    LLM can narrate '已投递 X' without making things up, and every
    ``ActionDetail.status`` maps 1:1 from the greet row status.
    """
    rows = [
        _detail("AIGC视觉生成实习", "greeted",
                source_url="https://example.com/job/1",
                company="CoA", match_score=0.88, match_verdict="strong"),
    ]
    report = _build_trigger_action_report(
        outcome="run",
        matched_details=rows,
        greeted_count=1,
        failed_count=0,
        unavailable_count=0,
    )
    assert report.status == "succeeded"
    assert report.summary == "已投递 1 个岗位"
    assert report.metrics == {"attempted": 1, "succeeded": 1, "failed": 0, "unavailable": 0}
    assert len(report.details) == 1
    d0 = report.details[0]
    assert d0.target == "AIGC视觉生成实习"
    assert d0.status == "succeeded"
    assert d0.url == "https://example.com/job/1"
    # extras passed through for downstream analytics but NOT status-critical
    assert d0.extras.get("company") == "CoA"
    assert d0.extras.get("match_score") == 0.88


def test_run_report_propagates_salary_from_row_to_detail_extras() -> None:
    """trace_fe19c3ab1e43 薪资缺失回归: service 必须把 scan row 里的
    salary 透传进 ``ActionDetail.extras``, 否则下游 prompt 拿不到薪资,
    LLM reply 漏报.

    这里只锁"service → detail.extras" 这一步行为契约(row 有 salary →
    detail.extras 有 salary); core 层的渲染/排序/sanitize 由
    test_action_report.py 自己覆盖, 不在这里重测.
    """
    rows = [
        _detail(
            "后端开发实习", "greeted",
            source_url="https://www.zhipin.com/job_detail/xxx.html",
            company="字节跳动",
            salary="590-600元/天",
        ),
    ]
    report = _build_trigger_action_report(
        outcome="run",
        matched_details=rows,
        greeted_count=1,
        failed_count=0,
        unavailable_count=0,
    )
    d0 = report.details[0]
    assert d0.extras.get("salary") == "590-600元/天"
    assert d0.extras.get("company") == "字节跳动"


def test_run_report_pua_salary_survives_until_prompt_sanitize() -> None:
    """host 侧不能预先破坏性清洗 PUA — 解码逻辑在 ActionReport 渲染层.

    matched_details 里 salary 带 PUA 私有码点 (平台字体反爬) 时,
    ``ActionDetail.extras['salary']`` 保留原始串 (方便 audit), 但
    ``to_prompt_lines`` 输出必须把 PUA 段替成 ``«encoded»`` marker.
    """
    encoded = "\ue035\ue039\ue031-\ue036\ue031\ue031元/天"
    rows = [
        _detail("后端实习", "greeted", source_url="https://z", company="字节",
                salary=encoded),
    ]
    report = _build_trigger_action_report(
        outcome="run",
        matched_details=rows,
        greeted_count=1,
        failed_count=0,
        unavailable_count=0,
    )
    # extras 里保留原串 (audit trail)
    assert report.details[0].extras["salary"] == encoded
    # 流向 LLM prompt 的渲染必须被 sanitize
    text = "\n".join(report.to_prompt_lines())
    assert "\ue035" not in text
    assert "\ue031" not in text
    assert "«encoded»" in text
    assert "元/天" in text


def test_run_report_partial_when_some_greeted_some_failed() -> None:
    rows = [
        _detail("岗位A", "greeted"),
        _detail("岗位B", "failed", error="timeout"),
        _detail("岗位C", "greeted"),
    ]
    report = _build_trigger_action_report(
        outcome="run",
        matched_details=rows,
        greeted_count=2,
        failed_count=1,
        unavailable_count=0,
    )
    assert report.status == "partial"
    assert "2/3" in report.summary
    assert report.metrics["succeeded"] == 2
    assert report.metrics["failed"] == 1
    statuses = [d.status for d in report.details]
    assert statuses.count("succeeded") == 2
    assert statuses.count("failed") == 1
    failed_detail = next(d for d in report.details if d.status == "failed")
    assert failed_detail.reason == "timeout", (
        "row.error must surface into detail.reason so Verifier can tell "
        "the user WHY the failure happened"
    )


def test_run_report_unavailable_maps_to_skipped_not_failed() -> None:
    """``_UNAVAILABLE_STATUSES`` (mode_not_configured / manual_required /
    dry_run) are executor-refused — MUST NOT be counted as failed in
    per-item details. Otherwise Verifier reads '5 tried and rejected by
    platform' which is a fact error."""
    rows = [
        _detail("岗位A", "mode_not_configured"),
        _detail("岗位B", "dry_run"),
    ]
    # sanity: every status we claim is skip-class is actually in the module-level set
    for row in rows:
        assert row["status"] in _UNAVAILABLE_STATUSES

    report = _build_trigger_action_report(
        outcome="run",
        matched_details=rows,
        greeted_count=0,
        failed_count=0,
        unavailable_count=2,
    )
    # No succeeded → infer_status would say failed; summary surfaces "未成功"
    assert report.status == "failed"
    assert report.metrics["succeeded"] == 0
    assert report.metrics["unavailable"] == 2
    statuses = [d.status for d in report.details]
    assert statuses == ["skipped", "skipped"], (
        f"_UNAVAILABLE_STATUSES must map to skipped details; got {statuses}"
    )


def test_run_report_no_rows_is_failed() -> None:
    """'scanned but nothing matched threshold' is a real unfulfilled
    case — ``status=failed`` with ``attempted=0``. Verifier then catches
    a naive '已投递' reply as false-absence."""
    report = _build_trigger_action_report(
        outcome="run",
        matched_details=[],
        greeted_count=0,
        failed_count=0,
        unavailable_count=0,
    )
    assert report.status == "failed"
    assert report.metrics["attempted"] == 0


# ──────────────────────────────────────────────────────────────
# 5. ActionReport is addressable via ACTION_REPORT_KEY for Brain
# ──────────────────────────────────────────────────────────────


def test_to_dict_is_suitable_for_observation_payload() -> None:
    """Sanity: the dict we stash under ``__action_report__`` round-trips
    through ``ActionReport.from_dict`` — Brain.extract_action_report
    relies on this."""
    report = _build_trigger_action_report(
        outcome="run",
        matched_details=[_detail("岗位A", "greeted")],
        greeted_count=1,
    )
    observation = {"ok": True, ACTION_REPORT_KEY: report.to_dict()}
    restored = ActionReport.from_dict(observation[ACTION_REPORT_KEY])
    assert restored.action == "job.greet"
    assert restored.status == "succeeded"
    assert restored.summary == report.summary
    assert restored.metrics == report.metrics
    assert len(restored.details) == 1
    assert restored.details[0].target == "岗位A"
