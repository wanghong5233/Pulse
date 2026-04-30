"""Business logic for scan → match → greet.

Pure orchestration: no FastAPI, no ``os.getenv``. The service is driven
entirely by its constructor arguments so it can be unit-tested against a
fake connector/repository and easily reused from a patrol tick, a chat
command, or a programmatic CLI.

Multi-platform readiness: the service only knows about the
:class:`JobPlatformConnector` contract — it never imports BOSS-specific
code. Swapping platforms is a matter of injecting a different connector.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Literal

from pulse.core.action_report import (
    ACTION_REPORT_KEY,
    ActionDetail,
    ActionReport,
    ActionStatus,
)
from pulse.core.notify.notifier import Notification, Notifier
from pulse.core.tokenizer import token_preview

from .._connectors.base import JobPlatformConnector
from ..memory import HardConstraints, JobMemory, JobMemorySnapshot
from .greeter import JobGreeter
from .matcher import JobSnapshotMatcher, MatchResult
from .reflection import ReflectionPlanner, RoundSummary
from .repository import GreetRepository
from .trait_expander import TraitCompanyExpander

logger = logging.getLogger(__name__)

# 预览模式 (F6) 不调 greeter LLM, 用这条占位替代 — 用户看到清单后确认才会
# 触发真实文本生成, 避免"看了一眼就取消"的场景浪费 per-job LLM 调用。
_PREVIEW_GREETING_PLACEHOLDER = "(招呼文本将在你确认后生成)"

# Executor-refusal statuses. These are NOT real send-and-failed outcomes —
# the underlying MCP refused to try because the account/profile isn't
# configured for sending yet (mode / credentials gap). We keep them out of
# ``failed_count`` (Contract C would misread "5 failed" as "5 tried and
# rejected by the platform") and bubble them up as ``unavailable``. The
# ActionReport builder maps these to ``status=skipped`` in the per-item
# ``ActionDetail`` so the judge sees the right semantics.
_UNAVAILABLE_STATUSES: frozenset[str] = frozenset({
    "mode_not_configured", "manual_required", "dry_run",
})

# Verdicts that qualify a JD to enter the candidate pool. Filtering is
# verdict-membership based (ordinal LLM-as-Judge), not score-threshold
# based — see matcher.py module docstring for the rationale.
_SHORTLIST_VERDICTS: frozenset[str] = frozenset({"good", "okay"})

# Reflection budget when the candidate pool is empty after the first
# scan+match pass. Hard constraints (city / experience_level / salary
# floor / avoid_trait) are NEVER relaxed; only the search keyword is
# evolved by the LLM, with the previous round's skip reasons fed back
# in. Two extra rounds is the documented industry sweet spot for
# Reflexion-style loops on browser-scraped corpora — past round 3 the
# scan returns near-duplicates and quality plateaus.
_REFLECTION_MAX_ROUNDS: int = 2


def _build_trigger_action_report(
    *,
    outcome: Literal["scan_miss", "not_ready", "quota_reached", "preview", "run"],
    reason: str | None = None,
    matched_details: list[dict[str, Any]] | None = None,
    preview_candidate_count: int = 0,
    greeted_count: int = 0,
    failed_count: int = 0,
    unavailable_count: int = 0,
    daily_count: int = 0,
    daily_limit: int = 0,
    source: str | None = None,
    trace_id: str = "",
) -> ActionReport:
    """ADR-003 Step B.3: structured execution report for ``job.greet.trigger``.

    Four exit branches of ``run_trigger`` all funnel through this builder
    so every observation carries the same ActionReport shape:

    * ``scan_miss`` / ``not_ready`` — infrastructure short-circuit before
      any send. ``status=failed`` with zero metrics; summary repeats the
      user-facing ``reason``.
    * ``preview`` — HITL preview path. ``status=preview`` with
      ``candidates`` count in metrics; no per-item details (LLM should
      only say "candidates shortlisted", not "applied").
    * ``run`` — real send loop. Per-item ``ActionDetail`` whose
      ``status`` is ``succeeded`` for ``greeted``, ``skipped`` for
      ``_UNAVAILABLE_STATUSES`` (executor refused to try), ``failed``
      otherwise. Overall status by infer_status over per-item statuses
      with a forced floor: all-skipped or empty → ``failed``.
    """
    evidence: dict[str, Any] = {}
    if source:
        evidence["source"] = str(source)
    if trace_id:
        evidence["trace_id"] = str(trace_id)

    if outcome in ("scan_miss", "not_ready"):
        return ActionReport.build(
            action="job.greet",
            status="failed",
            summary=str(reason or "job.greet.trigger failed before any send attempt"),
            metrics={
                "attempted": 0,
                "succeeded": 0,
                "failed": 0,
                "unavailable": 0,
            },
            evidence=evidence,
        )

    if outcome == "quota_reached":
        safe_limit = max(1, int(daily_limit))
        safe_count = max(0, int(daily_count))
        return ActionReport.build(
            action="job.greet",
            status="skipped",
            summary=(
                str(reason)
                if reason
                else f"今日投递配额已满 ({safe_count}/{safe_limit}), 本轮未执行投递"
            ),
            metrics={
                "attempted": 0,
                "succeeded": 0,
                "failed": 0,
                "unavailable": 0,
                "daily_count": safe_count,
                "daily_limit": safe_limit,
            },
            next_steps=(
                "可明日继续投递, 或调整每日投递上限后重试",
            ),
            evidence=evidence,
        )

    if outcome == "preview":
        return ActionReport.build(
            action="job.greet",
            status="preview",
            summary=(
                f"筛选了 {preview_candidate_count} 个候选岗位, 等待确认后再投"
                if preview_candidate_count > 0
                else "没有候选岗位匹配当前筛选条件"
            ),
            metrics={
                "candidates": preview_candidate_count,
                "attempted": 0,
                "succeeded": 0,
            },
            next_steps=('如需投递, 请明确说 "确认" 或 "开始投"',),
            evidence=evidence,
        )

    # outcome == "run" — real per-item send loop result
    rows = matched_details or []
    action_details: list[ActionDetail] = []
    for row in rows:
        row_status = str(row.get("status") or "")
        if row_status == "greeted":
            d_status: ActionStatus = "succeeded"
        elif row_status in _UNAVAILABLE_STATUSES:
            d_status = "skipped"
        else:
            d_status = "failed"
        extras: dict[str, Any] = {}
        # Render order is **user-facing priority**: 公司/薪资 先呈现,
        # 匹配分/裁决放后面 (求职视角 salary 比 match_score 更重要).
        # ActionReport.to_prompt_lines 会按 extras 插入顺序渲染.
        for extra_key in ("company", "salary", "match_score", "match_verdict"):
            value = row.get(extra_key)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            extras[extra_key] = value
        action_details.append(ActionDetail(
            target=str(row.get("job_title") or "未知岗位"),
            status=d_status,
            reason=(str(row["error"]) if row.get("error") else None),
            url=(str(row["source_url"]) if row.get("source_url") else None),
            extras=extras,
        ))

    total = len(rows)
    if greeted_count == total and greeted_count > 0:
        overall: ActionStatus = "succeeded"
        summary = f"已投递 {greeted_count} 个岗位"
    elif greeted_count > 0:
        overall = "partial"
        summary = f"投递了 {greeted_count}/{total} 个岗位"
    elif total > 0:
        overall = "failed"
        tried = failed_count + unavailable_count
        summary = (
            f"全部 {total} 次尝试未成功"
            if tried > 0
            else f"筛到 {total} 个候选但未发起发送"
        )
    else:
        overall = "failed"
        summary = reason or "没有符合条件的岗位"

    return ActionReport.build(
        action="job.greet",
        status=overall,
        summary=summary,
        details=action_details,
        metrics={
            "attempted": total,
            "succeeded": greeted_count,
            "failed": failed_count,
            "unavailable": unavailable_count,
        },
        evidence=evidence,
    )

# experience_level 归一化: 把用户/provider 可能写的别名折叠到 HC 枚举值.
# 只做字面归一, 不做语义推断 — 语义推断是 matcher LLM 的职责.
_EXP_LEVEL_ALIASES: dict[str, str] = {
    "intern": "intern",
    "internship": "intern",
    "实习": "intern",
    "new_grad": "new_grad",
    "campus": "new_grad",
    "应届": "new_grad",
    "校招": "new_grad",
    "full_time": "full_time",
    "fulltime": "full_time",
    "正式": "full_time",
    "全职": "full_time",
    "社招": "full_time",
    "senior": "senior",
    "资深": "senior",
}

_INTERN_HINT_KEYWORDS: tuple[str, ...] = (
    "实习",
    "intern",
    "internship",
    "应届",
    "校招",
)
_FULL_TIME_STRONG_HINT_KEYWORDS: tuple[str, ...] = (
    "工程师",
    "全职",
    "社招",
    "资深",
    "专家",
    "架构师",
    "负责人",
    "经理",
    "总监",
    "leader",
)
_DAILY_SALARY_HINT_KEYWORDS: tuple[str, ...] = (
    "日薪",
    "/天",
    "／天",
    "元/天",
    "元／天",
    "每天",
)
_MONTHLY_SALARY_HINT_KEYWORDS: tuple[str, ...] = (
    "/月",
    "／月",
    "月薪",
    "k/月",
    "k／月",
    "万/月",
    "万／月",
)


@dataclass(frozen=True, slots=True)
class FilterPipelineResult:
    """``_apply_filter_pipeline`` 的结构化返回值 (F7).

    - ``kept``: 依次通过 pref / hc / dedup 三段过滤后剩下的 items
    - ``pref_reasons`` / ``hc_reasons`` / ``dedup_reasons``: 每段被丢的原因串
      (给 run_trigger 拆成 3 条 stage 事件), 按顺序追加到 ``all_reasons``
    - ``dedup_urls``: 本次用到的 dedup URL 集合 (历史 greeted ∪ application_event
      items), 方便 caller 事件里报 ``known_dedup_urls`` 数量
    """

    kept: list[dict[str, Any]]
    pref_reasons: list[str]
    hc_reasons: list[str]
    dedup_reasons: list[str]
    dedup_urls: set[str]

    @property
    def all_reasons(self) -> list[str]:
        return [*self.pref_reasons, *self.hc_reasons, *self.dedup_reasons]


@dataclass(frozen=True, slots=True)
class GreetPolicy:
    """Runtime-tunable knobs for the greet workflow.

    Every field is sourced from :class:`pulse.core.config.Settings` at the
    controller boundary — the service itself never reads env vars.
    """

    batch_size: int
    match_threshold: float
    daily_limit: int
    default_keyword: str
    greeting_template: str
    hitl_required: bool


EmitStageEvent = Callable[..., str]


@dataclass(frozen=True, slots=True)
class _ScanHandleEntry:
    """Cached scan result for the scan → trigger hand-off contract
    (ADR-001 §3.2 Contract B Phase 2).

    A handle lets the LLM / caller say "reuse the scan I just asked for"
    to avoid doubling MCP browser cost. Intentionally in-process only
    (not persisted): handles are *step-local* — if the service restarts,
    the LLM gets a fresh ``sh_...`` and expired handles fail-loud.
    """

    handle: str
    trace_id: str
    keyword: str
    scan_result: dict[str, Any]
    created_at: float  # monotonic seconds


class JobGreetService:
    # Contract B Phase 2 cache knobs — see docs/adr/ADR-001-ToolUseContract.md §3.2.
    # TTL: long enough for a 2-turn HITL conversation (scan → preview →
    # confirm → trigger) but short enough that a stale handle from a
    # forgotten session can't accidentally greet old listings.
    _SCAN_HANDLE_TTL_SEC: float = 600.0
    _SCAN_HANDLE_MAX_ENTRIES: int = 32

    def __init__(
        self,
        *,
        connector: JobPlatformConnector,
        repository: GreetRepository,
        policy: GreetPolicy,
        notifier: Notifier,
        emit_stage_event: EmitStageEvent,
        preferences: JobMemory | None = None,
        matcher: JobSnapshotMatcher | None = None,
        greeter: JobGreeter | None = None,
        trait_expander: TraitCompanyExpander | None = None,
        reflection_planner: ReflectionPlanner | None = None,
    ) -> None:
        self._connector = connector
        self._repository = repository
        self._policy = policy
        self._notifier = notifier
        self._emit = emit_stage_event
        self._preferences = preferences
        self._matcher = matcher
        self._greeter = greeter
        self._trait_expander = trait_expander
        self._reflection_planner = reflection_planner
        # Contract B Phase 2: per-service, in-process cache. Python dict
        # insertion order gives us FIFO eviction for free; LRU is
        # overkill here (handles burn within one agent loop).
        self._scan_handles: dict[str, _ScanHandleEntry] = {}

    # ------------------------------------------------------------------
    # Contract B Phase 2 · scan_handle cache
    # ------------------------------------------------------------------

    @staticmethod
    def _now_monotonic() -> float:
        """Indirection point for tests: monkeypatch this to force TTL expiry."""
        return time.monotonic()

    def _mint_scan_handle(self, *, scan_result: dict[str, Any], trace_id: str,
                          keyword: str) -> str:
        handle = f"sh_{uuid.uuid4().hex[:12]}"
        now = self._now_monotonic()
        # Housekeeping: evict expired first, then cap size (oldest wins the boot).
        self._evict_expired_handles(now=now)
        while len(self._scan_handles) >= self._SCAN_HANDLE_MAX_ENTRIES:
            oldest_key = next(iter(self._scan_handles))
            self._scan_handles.pop(oldest_key, None)
        self._scan_handles[handle] = _ScanHandleEntry(
            handle=handle,
            trace_id=trace_id,
            keyword=keyword,
            scan_result=scan_result,
            created_at=now,
        )
        return handle

    def _lookup_scan_handle(self, handle: str) -> _ScanHandleEntry | None:
        entry = self._scan_handles.get(handle)
        if entry is None:
            return None
        now = self._now_monotonic()
        if now - entry.created_at > self._SCAN_HANDLE_TTL_SEC:
            # drop expired (lazy eviction) — caller gets fail-loud
            self._scan_handles.pop(handle, None)
            return None
        return entry

    def _evict_expired_handles(self, *, now: float) -> None:
        cutoff = now - self._SCAN_HANDLE_TTL_SEC
        expired = [k for k, v in self._scan_handles.items() if v.created_at < cutoff]
        for key in expired:
            self._scan_handles.pop(key, None)

    # ------------------------------------------------------------------ public API

    @property
    def connector(self) -> JobPlatformConnector:
        return self._connector

    @property
    def policy(self) -> GreetPolicy:
        return self._policy

    def run_scan(
        self,
        *,
        keyword: str,
        max_items: int,
        max_pages: int,
        target_count: int | None = None,
        evaluation_cap: int | None = None,
        scroll_plateau_rounds: int | None = None,
        job_type: str = "all",
        fetch_detail: bool = False,
        trace_id: str | None = None,
        apply_filters: bool = True,
    ) -> dict[str, Any]:
        """扫描职位并 (默认) 应用 JobMemory 存储的**工具层硬过滤** (F7).

        ``apply_filters=True`` (默认): 返回的 ``items`` 已经按以下顺序过滤:
          1. ``_filter_by_preferences`` — avoid_company / avoid_trait 黑名单
          2. ``_apply_hard_constraints`` — preferred_location / salary / level
          3. ``_apply_dedup`` — 过去成功投递过的 URL

        这保证"纯读"的 scan tool 也不会给 LLM 回灌违反用户硬约束或已投过的
        岗位; 否则 LLM 只能靠自己"记性"筛选, 很容易漏掉业务边界.

        ``apply_filters=False``: 由 ``run_trigger`` 使用, 让 trigger 在调用点
        内自己切分 pref/hc/dedup 三段, 发各自的 stage 事件 — scan 同一份
        pipeline 内部只发一条汇总 ``scan.filtered`` 事件, 不重复发包.
        """
        # P2-A: 预读 snapshot 一次, 取 preferred_location 以便 fan-out 爬取;
        # apply_filters=True 的分支下还会复用这份 snapshot 做硬约束/偏好过滤,
        # 避免在同一轮 scan 里重复访问 JobMemory.
        snapshot = self._load_snapshot()
        preferred_cities = self._resolve_scan_cities(snapshot)
        trace_id = self._emit(
            stage="scan",
            status="started",
            trace_id=trace_id,
            payload={
                "keyword": keyword,
                "max_items": max_items,
                "max_pages": max_pages,
                "target_count": target_count,
                "evaluation_cap": evaluation_cap,
                "scroll_plateau_rounds": scroll_plateau_rounds,
                "job_type": job_type,
                "fetch_detail": fetch_detail,
                "apply_filters": apply_filters,
                "cities": list(preferred_cities),
            },
        )
        try:
            normalized, errors, pages_scanned, scan_scroll_count, scan_source, exhausted_all = self._scan_cities(
                keyword=keyword,
                cities=preferred_cities,
                max_items=max_items,
                max_pages=max_pages,
                target_count=target_count,
                evaluation_cap=evaluation_cap,
                scroll_plateau_rounds=scroll_plateau_rounds,
                job_type=job_type,
                fetch_detail=fetch_detail,
            )

            pre_filter_total = len(normalized)
            filter_stats: dict[str, int] = {
                "pre_filter_total": pre_filter_total,
                "pref_dropped": 0,
                "hc_dropped": 0,
                "dedup_dropped": 0,
            }
            if apply_filters:
                pipeline = self._apply_filter_pipeline(
                    normalized, snapshot=snapshot
                )
                normalized = pipeline.kept
                errors.extend(pipeline.all_reasons)
                filter_stats.update({
                    "pref_dropped": len(pipeline.pref_reasons),
                    "hc_dropped": len(pipeline.hc_reasons),
                    "dedup_dropped": len(pipeline.dedup_reasons),
                    "post_filter_total": len(normalized),
                    "known_dedup_urls": len(pipeline.dedup_urls),
                })
                self._emit(
                    stage="scan",
                    status="filtered",
                    trace_id=trace_id,
                    payload={
                        "keyword": keyword,
                        **filter_stats,
                        # 仅取前 N 条原因供审计, 防止事件体过大 (也防 DB JSON 膨胀).
                        "dropped_reasons_sample": pipeline.all_reasons[:20],
                    },
                )
            result: dict[str, Any] = {
                "trace_id": trace_id,
                "keyword": keyword,
                "total": len(normalized),
                "pages_scanned": int(pages_scanned or 1),
                "scroll_count": int(scan_scroll_count or 0),
                "exhausted": bool(exhausted_all),
                "screenshot_path": None,
                "items": normalized,
                "source": str(scan_source or self._connector.provider_name),
                "provider": self._connector.provider_name,
                "execution_ready": self._connector.execution_ready,
                "errors": errors,
                "filter_stats": filter_stats,
                "filters_applied": bool(apply_filters),
                "cities_scanned": list(preferred_cities),
            }
        except Exception as exc:
            self._emit(
                stage="scan",
                status="failed",
                trace_id=trace_id,
                payload={"keyword": keyword, "error": str(exc)[:500]},
            )
            raise

        # Contract B Phase 2: mint a scan_handle so the LLM can pass it to
        # run_trigger and skip the double-scan. Non-participating callers
        # (legacy patrols) just ignore the extra field.
        scan_handle = self._mint_scan_handle(
            scan_result=result,
            trace_id=trace_id,
            keyword=keyword,
        )
        result["scan_handle"] = scan_handle

        self._emit(
            stage="scan",
            status="completed",
            trace_id=trace_id,
            payload={
                "keyword": keyword,
                "total": int(result["total"]),
                "pages_scanned": int(result["pages_scanned"]),
                "scroll_count": int(result.get("scroll_count") or 0),
                "exhausted": bool(result.get("exhausted")),
                "source": result["source"],
                "errors_total": len(result["errors"]),
                "filters_applied": bool(apply_filters),
                "scan_handle": scan_handle,
            },
        )
        return result

    def run_trigger(
        self,
        *,
        keyword: str,
        batch_size: int | None = None,
        match_threshold: float | None = None,
        greeting_text: str | None = None,
        job_type: str = "all",
        run_id: str | None = None,
        confirm_execute: bool = False,
        fetch_detail: bool = True,
        trace_id: str | None = None,
        scan_handle: str | None = None,
    ) -> dict[str, Any]:
        # Contract B Phase 2 · scan_handle hand-off (ADR-001 §3.2).
        #
        # * ``scan_handle`` given + known/fresh → reuse the cached scan,
        #   reuse its trace_id for audit chain, SKIP the extra run_scan.
        # * given + unknown/expired → **fail-loud**. We refuse to silently
        #   fall back to a fresh scan, because that would make the
        #   hand-off contract a lie: caller thinks "I'm reusing" but
        #   actually pays full MCP cost. The LLM sees the error code and
        #   re-issues ``job.greet.scan`` explicitly.
        # * omitted → legacy behavior (own scan + fresh trace_id). Kept
        #   for patrols / CLI paths that don't know about handles yet.
        scan_handle_miss = False
        reused_scan: dict[str, Any] | None = None
        if scan_handle is not None:
            entry = self._lookup_scan_handle(scan_handle)
            if entry is None:
                scan_handle_miss = True
            else:
                reused_scan = entry.scan_result
                # Reuse scan's trace_id so trigger events land under the
                # same audit umbrella as the scan that produced them.
                trace_id = trace_id or entry.trace_id
                # Trigger's own trace_id takes precedence if caller forced
                # one, but the reuse is still detectable via audit field.

        snapshot = self._load_snapshot()
        requested_keyword = str(keyword or "").strip()
        scan_keyword = (
            str(reused_scan.get("keyword") or "").strip()
            if isinstance(reused_scan, dict)
            else requested_keyword
        )
        effective_keywords = self._resolve_trigger_keywords(
            keyword=scan_keyword,
            snapshot=snapshot,
        )
        effective_keyword = effective_keywords[0]

        trace_id = self._emit(
            stage="trigger",
            status="started",
            trace_id=trace_id,
            payload={
                "keyword": effective_keyword,
                "keywords": list(effective_keywords),
                "requested_keyword": requested_keyword,
                "batch_size": batch_size,
                "match_threshold": match_threshold,
                "confirm_execute": confirm_execute,
                "fetch_detail": fetch_detail,
                "scan_handle": scan_handle,
                "scan_handle_reused": reused_scan is not None,
            },
        )

        if scan_handle_miss:
            reason = (
                f"scan_handle {scan_handle!r} not in cache or TTL expired; "
                "call job.greet.scan first and pass the returned scan_handle"
            )
            self._emit(
                stage="trigger",
                status="failed",
                trace_id=trace_id,
                payload={
                    "keyword": effective_keyword,
                    "error": "scan_handle_unknown_or_expired",
                    "scan_handle": scan_handle,
                },
            )
            daily_limit = max(1, int(self._policy.daily_limit))
            return {
                "ok": False,
                "error": "scan_handle_unknown_or_expired",
                "trace_id": trace_id,
                "keyword": effective_keyword,
                "requested_keyword": requested_keyword,
                "needs_confirmation": False,
                "execution_ready": self._connector.execution_ready,
                "greeted": 0,
                "failed": 0,
                "skipped": 0,
                "daily_count": 0,
                "daily_limit": daily_limit,
                "reason": reason,
                "pages_scanned": 0,
                "matched_details": [],
                "source": self._connector.provider_name,
                "provider": self._connector.provider_name,
                "errors": [reason],
                ACTION_REPORT_KEY: _build_trigger_action_report(
                    outcome="scan_miss",
                    reason=reason,
                    source=self._connector.provider_name,
                    trace_id=trace_id,
                ).to_dict(),
            }

        try:
            daily_limit = max(1, int(self._policy.daily_limit))
            greeted_today = self._repository.today_greeted_urls()
            remaining_quota = max(0, daily_limit - len(greeted_today))
            if remaining_quota <= 0:
                reason = (
                    f"daily_limit_reached: 今日投递配额已满 ({len(greeted_today)}/{daily_limit}), "
                    "本轮未执行投递"
                )
                self._emit(
                    stage="trigger",
                    status="quota_reached",
                    trace_id=trace_id,
                    payload={
                        "keyword": effective_keyword,
                        "daily_count": len(greeted_today),
                        "daily_limit": daily_limit,
                    },
                )
                return {
                    "ok": True,
                    "trace_id": trace_id,
                    "keyword": effective_keyword,
                    "requested_keyword": requested_keyword,
                    "needs_confirmation": False,
                    "execution_ready": self._connector.execution_ready,
                    "greeted": 0,
                    "failed": 0,
                    "unavailable": 0,
                    "skipped": 0,
                    "daily_count": len(greeted_today),
                    "daily_limit": daily_limit,
                    "reason": reason,
                    "pages_scanned": 0,
                    "matched_details": [],
                    "source": self._connector.provider_name,
                    "provider": self._connector.provider_name,
                    "errors": [reason],
                    ACTION_REPORT_KEY: _build_trigger_action_report(
                        outcome="quota_reached",
                        reason=reason,
                        daily_count=len(greeted_today),
                        daily_limit=daily_limit,
                        source=self._connector.provider_name,
                        trace_id=trace_id or "",
                    ).to_dict(),
                }
            if reused_scan is not None:
                scan = reused_scan
                logger.info(
                    "job_greet trigger reusing scan_handle=%s trace_id=%s items=%d",
                    scan_handle,
                    trace_id,
                    len(scan.get("items") or []),
                )
            else:
                # Streaming scroll: ask scan to fill an evaluation pool deep
                # enough to feed two-stage matching. Stage-A operates on
                # metadata only; detail-fetches are deferred to stage-B over
                # the list-stage shortlist, so scan stays pure-list here.
                # batch_size * 6 gives stage-A room for ~70% drop rate
                # (typical on BOSS) and still hits the ``evaluation_cap``
                # ceiling well before the absolute scroll cap.
                safe_batch_for_scan = self._clamp_batch_size(batch_size)
                scan = self._run_trigger_scan(
                    keywords=effective_keywords,
                    target_count=max(20, safe_batch_for_scan * 6),
                    evaluation_cap=120,
                    scroll_plateau_rounds=3,
                    job_type=job_type,
                    fetch_detail=False,
                    trace_id=trace_id,
                )
            if not self._connector.execution_ready:
                source = str(scan.get("source") or self._connector.provider_name)
                provider = str(scan.get("provider") or self._connector.provider_name)
                reason = (
                    f"connector is not execution-ready (provider={provider}, source={source}); "
                    "trigger requires real connector (mcp/openapi)"
                )
                self._emit(
                    stage="trigger",
                    status="failed",
                    trace_id=trace_id,
                    payload={"keyword": effective_keyword, "error": reason[:500]},
                )
                return {
                    "ok": False,
                    "trace_id": trace_id,
                    "keyword": effective_keyword,
                    "requested_keyword": requested_keyword,
                    "needs_confirmation": False,
                    "execution_ready": False,
                    "greeted": 0,
                    "failed": 0,
                    "skipped": 0,
                    "daily_count": len(greeted_today),
                    "daily_limit": daily_limit,
                    "reason": reason,
                    "pages_scanned": int(scan.get("pages_scanned") or 0),
                    "matched_details": [],
                    "source": source,
                    "provider": provider,
                    "errors": list(scan.get("errors") or []) + [reason],
                    ACTION_REPORT_KEY: _build_trigger_action_report(
                        outcome="not_ready",
                        reason=reason,
                        source=source,
                        trace_id=trace_id or "",
                    ).to_dict(),
                }
            items = list(scan.get("items") or [])
            pages_scanned = int(scan.get("pages_scanned") or 0)
            threshold = self._clamp_threshold(match_threshold)
            safe_batch_size = self._clamp_batch_size(batch_size)

            # snapshot 是过滤 / matcher / greeter 的共同输入; 一次 trigger
            # 内复用一份 (见 JobMemory.snapshot O(N))。F3 把 hard_constraint
            # / dedup 放在 matcher LLM 之前, 显式过滤可硬性判定的条目,
            # 省 per-item LLM 调用且保证业务边界不靠 LLM 判断是否遵守。
            errors = list(scan.get("errors") or [])

            pipeline = self._apply_filter_pipeline(items, snapshot=snapshot)
            errors.extend(pipeline.pref_reasons)
            errors.extend(pipeline.hc_reasons)
            errors.extend(pipeline.dedup_reasons)
            self._emit(
                stage="trigger",
                status="hc_filter",
                trace_id=trace_id,
                payload={
                    "kept_after_pref_hc": len(items)
                    - len(pipeline.pref_reasons) - len(pipeline.hc_reasons),
                    "pref_dropped": len(pipeline.pref_reasons),
                    "hc_dropped": len(pipeline.hc_reasons),
                    "dropped_reasons": pipeline.hc_reasons[:20],
                },
            )
            self._emit(
                stage="trigger",
                status="dedup_filter",
                trace_id=trace_id,
                payload={
                    "kept": len(pipeline.kept),
                    "dropped": len(pipeline.dedup_reasons),
                    "dedup_total_known_urls": len(pipeline.dedup_urls),
                    "dropped_reasons": pipeline.dedup_reasons[:20],
                },
            )

            target_pool_size = min(safe_batch_size, remaining_quota)
            # ``threshold`` is retained in the request for back-compat but
            # no longer gates inclusion. We still log it so audit can see
            # whether a caller is shipping a stale threshold value.
            _ = threshold

            # ── Stage A — list-only matcher ───────────────────────────
            # Run the LLM-as-Judge over scan metadata (title/company/
            # salary/snippet). Cheap; cuts ~70% obvious misses without
            # paying detail-page cost.
            initial_skipped_audit: list[dict[str, Any]] = []
            list_shortlist = self._score_items(
                pipeline.kept,
                snapshot=snapshot,
                keyword=effective_keyword,
                errors=errors,
                trace_id=trace_id,
                skipped_audit=initial_skipped_audit,
                stage_label="list",
            )

            # ── Stage B — detail-aware refinement ─────────────────────
            # Only stage-A survivors pay fetch_job_detail cost. Stage-B
            # re-scores with the JD body included; matcher prompt
            # auto-deepens when row['detail'] is populated.
            if fetch_detail:
                matched = self._refine_with_detail(
                    list_shortlist,
                    snapshot=snapshot,
                    keyword=effective_keyword,
                    errors=errors,
                    trace_id=trace_id,
                    target_count=target_pool_size,
                )
            else:
                matched = list_shortlist
            self._emit(
                stage="trigger",
                status="two_stage_match",
                trace_id=trace_id,
                payload={
                    "stage_a_kept": len(list_shortlist),
                    "stage_b_kept": len(matched),
                    "detail_enabled": bool(fetch_detail),
                    "scan_exhausted": bool(scan.get("exhausted")),
                    "scan_scroll_count": int(scan.get("scroll_count") or 0),
                },
            )

            # ── Reflection (degraded fallback) ────────────────────────
            # Only invoked when the source list is *truly* exhausted on
            # the original keyword(s) AND we still don't meet target.
            # Within the same keyword, scroll-driven scan already
            # collected up to ``evaluation_cap`` cards; evolving keywords
            # while there's still tail content would pollute the pool
            # with off-target results.
            tried_keywords: list[str] = list(effective_keywords)
            round_history: list[RoundSummary] = [
                RoundSummary(
                    keyword=effective_keyword,
                    scanned_total=len(items),
                    shortlisted_total=len(matched),
                    skipped_examples=initial_skipped_audit[:8],
                )
            ]
            reflection_iterations = 0
            seen_urls: set[str] = {
                str(row.get("source_url") or "").strip()
                for row in matched
                if row.get("source_url")
            }
            scan_exhausted = bool(scan.get("exhausted"))
            while (
                len(matched) < target_pool_size
                and reflection_iterations < _REFLECTION_MAX_ROUNDS
                and self._reflection_planner is not None
                and scan_exhausted
            ):
                reflection_iterations += 1
                missing = target_pool_size - len(matched)
                hard_md = self._render_hard_constraints_md(snapshot)
                plan = self._reflection_planner.plan_next_keywords(
                    original_user_intent=requested_keyword,
                    hard_constraints_md=hard_md,
                    round_history=round_history,
                    target_remaining=missing,
                    already_tried_keywords=tried_keywords,
                )
                self._emit(
                    stage="trigger",
                    status="reflection",
                    trace_id=trace_id,
                    payload={
                        "iteration": reflection_iterations,
                        "missing": missing,
                        "next_keywords": list(plan.next_keywords),
                        "rationale": plan.rationale,
                        "trigger": "scan_exhausted",
                    },
                )
                if not plan.next_keywords:
                    break

                next_matched, next_errors, next_summary, round_exhausted = self._collect_round(
                    keywords=list(plan.next_keywords),
                    snapshot=snapshot,
                    job_type=job_type,
                    fetch_detail=fetch_detail,
                    trace_id=trace_id,
                    seen_urls=seen_urls,
                    target_count=target_pool_size - len(matched),
                )
                tried_keywords.extend(plan.next_keywords)
                errors.extend(next_errors)
                round_history.append(next_summary)
                # Hard gate: reflection can continue only if this round's
                # keyword space is also exhausted. Otherwise we must keep
                # consuming the current keyword's scroll tail instead of
                # evolving again.
                scan_exhausted = bool(round_exhausted)
                if next_matched:
                    matched.extend(next_matched)

            matched.sort(
                key=lambda row: float(row.get("match_score") or 0.0),
                reverse=True,
            )
            selected = matched[:target_pool_size]
            override_greeting = self._override_greeting(greeting_text)
            safe_run_id = run_id or datetime.now(timezone.utc).strftime("run-%Y%m%d%H%M%S")

            if self._policy.hitl_required and not confirm_execute:
                # F6: 预览短路 — 不调 greeter LLM, 给占位文本; 等 confirm_execute=True
                # 的真正发送链路再生成个性化文案, 避免用户 "看一眼就取消" 也白烧 LLM.
                preview_details = []
                for item in selected:
                    item_greeting = override_greeting or _PREVIEW_GREETING_PLACEHOLDER
                    preview_details.append(
                        self._make_preview_row(
                            item,
                            run_id=safe_run_id,
                            greeting_text=item_greeting,
                            scan=scan,
                        )
                    )
                result = {
                    "ok": True,
                    "trace_id": trace_id,
                    "keyword": effective_keyword,
                    "requested_keyword": requested_keyword,
                    "needs_confirmation": True,
                    "execution_ready": self._connector.execution_ready,
                    "greeted": 0,
                    "failed": 0,
                    "skipped": max(0, len(items) - len(selected)),
                    "daily_count": len(greeted_today),
                    "daily_limit": daily_limit,
                    "reason": "confirmation required before real execution",
                    "pages_scanned": pages_scanned,
                    "matched_details": preview_details,
                    "source": scan.get("source"),
                    "provider": scan.get("provider"),
                    "errors": errors,
                    "greeting_deferred": override_greeting is None,
                    ACTION_REPORT_KEY: _build_trigger_action_report(
                        outcome="preview",
                        preview_candidate_count=len(selected),
                        source=str(scan.get("source") or self._connector.provider_name),
                        trace_id=trace_id or "",
                    ).to_dict(),
                }
                self._emit(
                    stage="trigger",
                    status="preview",
                    trace_id=trace_id,
                    payload={
                        "keyword": effective_keyword,
                        "selected_total": len(selected),
                        "pages_scanned": pages_scanned,
                        "source": scan.get("source"),
                        "greeting_deferred": override_greeting is None,
                    },
                )
                return result

            details: list[dict[str, Any]] = []
            for item in selected:
                item_greeting = self._compose_greeting(
                    job=item,
                    snapshot=snapshot,
                    override=override_greeting,
                )
                action = self._connector.greet_job(
                    job=item,
                    greeting_text=item_greeting,
                    run_id=safe_run_id,
                )
                ok = bool(action.get("ok"))
                error = str(action.get("error") or "").strip()
                if error:
                    errors.append(error[:400])
                details.append(
                    {
                        "run_id": safe_run_id,
                        "job_id": item.get("job_id"),
                        "job_title": item["title"],
                        "company": item["company"],
                        # Raw platform salary text (可能含招聘平台反爬字体的
                        # PUA 私有码点 ``\ue000-\uf8ff``). ActionReport 层会
                        # 在渲染进 LLM prompt 之前统一 sanitize, host 侧这里
                        # 只负责原样透传, 不做破坏性清洗.
                        "salary": item.get("salary"),
                        "match_score": item["match_score"],
                        "match_verdict": item.get("match_verdict"),
                        "match_reason": item.get("match_reason"),
                        "status": "greeted" if ok else str(action.get("status") or "failed"),
                        "greeting_text": item_greeting,
                        "source_url": item["source_url"],
                        "source": action.get("source") or item.get("source") or scan.get("source"),
                        "provider": action.get("provider") or scan.get("provider"),
                        "error": error or None,
                        "attempts": int(action.get("attempts") or 0),
                    }
                )
            self._repository.append_greet_logs(details)
            self._record_application_events(
                details, run_id=safe_run_id, keyword=effective_keyword
            )
            greeted_count = sum(1 for row in details if row.get("status") == "greeted")
            # ``unavailable`` = executor refused to try (mode_not_configured /
            # manual_required / dry_run) — infrastructure gap, NOT a real
            # send-and-failed. Splitting it out so Contract C can tell
            # "已投递 5 家" from "尝试了但平台拒绝了" (fail-loud rather than
            # blending into failed_count). See module-level
            # ``_UNAVAILABLE_STATUSES``.
            unavailable_count = sum(
                1 for row in details if str(row.get("status") or "") in _UNAVAILABLE_STATUSES
            )
            failed_count = len(details) - greeted_count - unavailable_count
            notif_level = "warning" if unavailable_count > 0 else "info"
            self._notifier.send(
                Notification(
                    level=notif_level,
                    title="job_greet trigger",
                    content=(
                        f"keyword={effective_keyword}; greeted={greeted_count}; "
                        f"failed={failed_count}; unavailable={unavailable_count}; "
                        f"threshold={threshold}"
                    ),
                    metadata={"run_id": safe_run_id},
                )
            )
            if unavailable_count > 0:
                logger.warning(
                    "job_greet trigger: %d/%d attempts refused by executor "
                    "(status in %s); check PULSE_BOSS_MCP_GREET_MODE and MCP health",
                    unavailable_count, len(details), sorted(_UNAVAILABLE_STATUSES),
                )
            result = {
                "ok": True,
                "trace_id": trace_id,
                "keyword": effective_keyword,
                "requested_keyword": requested_keyword,
                "needs_confirmation": False,
                "execution_ready": self._connector.execution_ready,
                "greeted": greeted_count,
                "failed": failed_count,
                "unavailable": unavailable_count,
                "skipped": max(0, len(items) - len(selected)),
                "daily_count": len(greeted_today) + greeted_count,
                "daily_limit": daily_limit,
                "reason": None if details else "no job passed threshold",
                "pages_scanned": pages_scanned,
                "matched_details": details,
                "source": scan.get("source"),
                "provider": scan.get("provider"),
                "errors": errors,
                ACTION_REPORT_KEY: _build_trigger_action_report(
                    outcome="run",
                    matched_details=details,
                    greeted_count=greeted_count,
                    failed_count=failed_count,
                    unavailable_count=unavailable_count,
                    source=str(scan.get("source") or self._connector.provider_name),
                    trace_id=trace_id or "",
                ).to_dict(),
            }
        except Exception as exc:
            self._emit(
                stage="trigger",
                status="failed",
                trace_id=trace_id,
                payload={"keyword": effective_keyword, "error": str(exc)[:500]},
            )
            raise

        self._emit(
            stage="trigger",
            status="completed",
            trace_id=trace_id,
            payload={
                "keyword": effective_keyword,
                "greeted": int(result["greeted"]),
                "failed": int(result["failed"]),
                "skipped": int(result["skipped"]),
                "source": result["source"],
            },
        )
        return result

    # ------------------------------------------------------------------ helpers

    def _clamp_batch_size(self, value: int | None) -> int:
        if value is None:
            return max(1, min(self._policy.batch_size, 20))
        return max(1, min(int(value), 20))

    def _clamp_threshold(self, value: float | None) -> float:
        if value is None:
            return max(30.0, min(float(self._policy.match_threshold), 95.0))
        return max(30.0, min(float(value), 95.0))

    @staticmethod
    def _override_greeting(override: str | None) -> str | None:
        """返回用户显式覆盖的招呼文本(若有), 让所有 item 用同一份文本。

        区别于 ``_compose_greeting``: 这里只处理 "HTTP payload 显式指定
        greeting_text" 这条路径, 该场景下 greeter/personalization 都跳过,
        严格遵循用户给的文本。
        """
        candidate = (override or "").strip()
        return candidate or None

    def _load_snapshot(self) -> JobMemorySnapshot | None:
        """trigger 内复用一份 snapshot; 失败不阻断 pipeline。"""
        if self._preferences is None:
            return None
        try:
            return self._preferences.snapshot()
        except Exception as exc:  # pragma: no cover - 防御性
            logger.warning("greet: JobMemory.snapshot() failed: %s", exc)
            return None

    def _resolve_scan_cities(self, snapshot: JobMemorySnapshot | None) -> list[str]:
        """从 snapshot 读出 scan 应覆盖的城市列表 (P2-A).

        * ``None`` snapshot 或空 ``preferred_location`` → 返回 ``[]``, 调用方
          解读成"一次全国搜索".
        * 非空 list → 逐项 strip 去重, 保留原顺序.
        设计取舍: fan-out 的目标是让 BOSS 按 city 搜到的结果不再偏向单一城市
        (如 "大模型 agent 实习" 默认推荐流常年偏上海). 即便 city 解析失败,
        下游 ``_apply_hard_constraints.preferred_location`` 仍是兜底过滤.
        """
        if snapshot is None:
            return []
        cities_raw = list(snapshot.hard_constraints.preferred_location or [])
        seen: set[str] = set()
        out: list[str] = []
        for item in cities_raw:
            name = str(item or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(name)
        return out

    def _resolve_trigger_keywords(
        self,
        *,
        keyword: str | None,
        snapshot: JobMemorySnapshot | None,
    ) -> list[str]:
        """Resolve search keywords for both interactive and scheduled trigger.

        Interactive calls may pass an explicit role phrase parsed from the
        current utterance. Patrol calls have no current utterance, so they read
        durable JobMemory target_roles and scan each active target instead of
        freezing forever on one env default keyword.
        """
        requested = str(keyword or "").strip()
        default_keyword = str(self._policy.default_keyword or "").strip()
        target_roles = (
            list(snapshot.hard_constraints.target_roles)
            if snapshot is not None
            else []
        )
        if requested and requested != default_keyword:
            candidates = [requested]
        elif target_roles:
            candidates = [str(role or "").strip() for role in target_roles]
        else:
            candidates = [requested or default_keyword or "AI Agent 实习"]
        keywords = _dedupe_nonempty(candidates)
        if (
            snapshot is not None
            and _EXP_LEVEL_ALIASES.get(
                str(snapshot.hard_constraints.experience_level or "").strip().lower()
            )
            == "intern"
        ):
            keywords = [self._ensure_intern_keyword(k) for k in keywords]
        return _dedupe_nonempty(keywords)

    @staticmethod
    def _ensure_intern_keyword(keyword: str) -> str:
        text = str(keyword or "").strip()
        if not text:
            return "AI Agent 实习"
        lowered = text.lower()
        if any(tok in lowered for tok in _INTERN_HINT_KEYWORDS):
            return text
        return f"{text} 实习"

    def _run_trigger_scan(
        self,
        *,
        keywords: list[str],
        target_count: int | None = None,
        evaluation_cap: int | None = None,
        scroll_plateau_rounds: int | None = None,
        max_items: int | None = None,
        max_pages: int | None = None,
        job_type: str,
        fetch_detail: bool,
        trace_id: str | None,
    ) -> dict[str, Any]:
        """Scan all currently active target-role keywords for trigger mode.

        New trigger callers pass ``target_count`` / ``evaluation_cap`` /
        ``scroll_plateau_rounds`` directly. Legacy ``max_items`` /
        ``max_pages`` are accepted for back-compat (older patrol harnesses)
        and projected onto the streaming-scroll knobs.
        """
        safe_keywords = _dedupe_nonempty(keywords)
        if target_count is None:
            target_count = max_items if max_items is not None else 30
        if evaluation_cap is None:
            evaluation_cap = max(target_count, 60)
        if scroll_plateau_rounds is None:
            scroll_plateau_rounds = 3

        if len(safe_keywords) <= 1:
            keyword = safe_keywords[0] if safe_keywords else self._policy.default_keyword
            return self.run_scan(
                keyword=keyword,
                max_items=target_count,
                max_pages=max_pages or scroll_plateau_rounds,
                target_count=target_count,
                evaluation_cap=evaluation_cap,
                scroll_plateau_rounds=scroll_plateau_rounds,
                job_type=job_type,
                fetch_detail=fetch_detail,
                trace_id=trace_id,
                apply_filters=False,
            )

        per_keyword = max(1, (target_count + len(safe_keywords) - 1) // len(safe_keywords))
        per_keyword_cap = max(per_keyword, evaluation_cap // max(1, len(safe_keywords)))
        merged_items: list[dict[str, Any]] = []
        errors: list[str] = []
        seen: set[str] = set()
        pages_scanned = 0
        scroll_count = 0
        sources: list[str] = []
        providers: list[str] = []
        exhausted_all = True

        for keyword in safe_keywords:
            scan = self.run_scan(
                keyword=keyword,
                max_items=per_keyword,
                max_pages=max_pages or scroll_plateau_rounds,
                target_count=per_keyword,
                evaluation_cap=per_keyword_cap,
                scroll_plateau_rounds=scroll_plateau_rounds,
                job_type=job_type,
                fetch_detail=fetch_detail,
                trace_id=trace_id,
                apply_filters=False,
            )
            errors.extend(str(err) for err in list(scan.get("errors") or []))
            pages_scanned = max(pages_scanned, int(scan.get("pages_scanned") or 0))
            scroll_count = max(scroll_count, int(scan.get("scroll_count") or 0))
            # Across keywords, exhaustion is only true if every keyword's
            # source list ran dry. If any keyword still has tail content
            # we shouldn't trigger reflection-driven keyword evolution.
            if not bool(scan.get("exhausted")):
                exhausted_all = False
            source = str(scan.get("source") or "").strip()
            provider = str(scan.get("provider") or "").strip()
            if source and source not in sources:
                sources.append(source)
            if provider and provider not in providers:
                providers.append(provider)
            for item in list(scan.get("items") or []):
                if not isinstance(item, dict):
                    continue
                key = (
                    f"{item.get('job_id') or ''}::{item.get('source_url') or ''}".lower()
                )
                if key in seen:
                    continue
                seen.add(key)
                merged_items.append(item)
                if len(merged_items) >= evaluation_cap:
                    break
            if len(merged_items) >= evaluation_cap:
                break

        return {
            "trace_id": trace_id,
            "keyword": safe_keywords[0],
            "keywords": safe_keywords,
            "total": len(merged_items),
            "pages_scanned": max(1, pages_scanned),
            "scroll_count": max(0, scroll_count),
            "exhausted": exhausted_all,
            "screenshot_path": None,
            "items": merged_items,
            "source": ",".join(sources) or self._connector.provider_name,
            "provider": ",".join(providers) or self._connector.provider_name,
            "execution_ready": self._connector.execution_ready,
            "errors": errors,
            "filter_stats": {
                "pre_filter_total": len(merged_items),
                "pref_dropped": 0,
                "hc_dropped": 0,
                "dedup_dropped": 0,
            },
            "filters_applied": False,
        }

    def _scan_cities(
        self,
        *,
        keyword: str,
        cities: list[str],
        max_items: int,
        max_pages: int,
        target_count: int | None = None,
        evaluation_cap: int | None = None,
        scroll_plateau_rounds: int | None = None,
        job_type: str,
        fetch_detail: bool,
    ) -> tuple[list[dict[str, Any]], list[str], int, int, str, bool]:
        """按 cities 列表 fan-out 调 ``connector.scan_jobs`` 并合并结果.

        - ``cities=[]``: 一次调用, ``city=None``;
        - ``cities=[c]``: 一次调用, ``city=c``;
        - ``cities=[c1, c2, ...]``: 每个 city 一次, ``max_items`` 按 N 均分 (向上
          取整, 每轮至少 1), 同 ``(job_id, source_url)`` 跨 city 去重.

        `fetch_detail=True` 时对每条命中再单独取详情. 返回
        ``(items, errors, pages_scanned, scroll_count, source_label, exhausted_all)``;
        ``exhausted_all=True`` 仅当所有 city 的源列表都报到底, 用作上层
        reflection 兜底门控.
        """
        target_total = max(1, min(int(max_items), 200))
        if target_count is None:
            target_count = target_total
        if evaluation_cap is None:
            evaluation_cap = max(target_count, 60)
        if scroll_plateau_rounds is None:
            scroll_plateau_rounds = 3
        city_list: list[str | None]
        if cities:
            city_list = [c for c in cities]  # type: ignore[list-item]
            # 多城市 fan-out: 每 city 至少爬 1 条, 合并后截断到 evaluation_cap.
            per_city = max(1, (target_count + len(city_list) - 1) // len(city_list))
            per_city_cap = max(per_city, evaluation_cap // len(city_list))
        else:
            city_list = [None]
            per_city = target_count
            per_city_cap = evaluation_cap

        merged: list[dict[str, Any]] = []
        merged_errors: list[str] = []
        seen_keys: set[str] = set()
        pages_scanned_max = 0
        scroll_count_max = 0
        source_labels: list[str] = []
        exhausted_all = True

        for city in city_list:
            scan_result = self._connector.scan_jobs(
                keyword=keyword,
                max_items=per_city,
                max_pages=max_pages,
                target_count=per_city,
                evaluation_cap=per_city_cap,
                scroll_plateau_rounds=scroll_plateau_rounds,
                job_type=job_type,
                city=city,
            )
            if not bool(scan_result.get("exhausted")):
                exhausted_all = False
            merged_errors.extend(
                str(item)[:400] for item in list(scan_result.get("errors") or [])
            )
            pages_scanned_max = max(
                pages_scanned_max, int(scan_result.get("pages_scanned") or 1)
            )
            scroll_count_max = max(
                scroll_count_max, int(scan_result.get("scroll_count") or 0)
            )
            source_label = str(scan_result.get("source") or self._connector.provider_name)
            if source_label and source_label not in source_labels:
                source_labels.append(source_label)

            for row in list(scan_result.get("items") or []):
                if not isinstance(row, dict):
                    continue
                item = self._normalize_scan_item(keyword, row)
                dedupe_key = f"{item['job_id']}::{item['source_url']}".lower()
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                if fetch_detail:
                    detail_result = self._connector.fetch_job_detail(
                        job_id=str(item.get("job_id") or ""),
                        source_url=str(item.get("source_url") or ""),
                    )
                    detail = detail_result.get("detail")
                    if isinstance(detail, dict) and detail:
                        item["detail"] = detail
                    err = str(detail_result.get("error") or "").strip()
                    if err:
                        merged_errors.append(err[:400])
                merged.append(item)
                if len(merged) >= evaluation_cap:
                    break
            if len(merged) >= evaluation_cap:
                break

        combined_source = ",".join(source_labels) or self._connector.provider_name
        return (
            merged,
            merged_errors,
            max(1, pages_scanned_max),
            max(0, scroll_count_max),
            combined_source,
            exhausted_all,
        )

    def _score_items(
        self,
        items: list[dict[str, Any]],
        *,
        snapshot: JobMemorySnapshot | None,
        keyword: str,
        errors: list[str] | None = None,
        trace_id: str | None = None,
        skipped_audit: list[dict[str, Any]] | None = None,
        stage_label: str = "list",
    ) -> list[dict[str, Any]]:
        """Run LLM-as-Judge over every JD and emit a per-item audit event.

        Contract:

        * The matcher is an ordinal classifier (good/okay/poor/skip).
        * **Every** JD that reaches this method emits one
          ``module.job_greet.match.candidate`` event so post-mortem can
          tell "no shortlist because LLM said poor/skip" apart from
          "no shortlist because matcher crashed silently".
        * ``stage_label`` distinguishes list-only vs detail-aware passes
          in the audit event so the same JD can be re-scored after
          ``fetch_job_detail`` without ambiguity.
        * Inclusion in the returned list is verdict-based: only ``good``
          and ``okay`` are returned. ``poor`` / ``skip`` rows are dropped
          here but still appear in the audit event.
        * No heuristic fallback when the matcher is missing — auto-greet
          is a real outbound action; degrading to keywords is worse than
          failing loud.
        """
        if self._matcher is None:
            logger.warning("greet matcher unavailable; skip all trigger candidates")
            return []
        shortlist_verdicts = _SHORTLIST_VERDICTS
        out: list[dict[str, Any]] = []
        for row in items:
            try:
                result: MatchResult = self._matcher.match(
                    job=row, snapshot=snapshot, keyword=keyword
                )
            except (RuntimeError, ValueError) as exc:
                # Only LLM/parse-layer transient errors are absorbed here.
                # KeyError/TypeError indicate dirty upstream payload and
                # must fail-fast so the source can be fixed (§代码宪法 §2).
                logger.warning(
                    "greet matcher failed; skip candidate job_id=%s err=%s",
                    row.get("job_id"),
                    exc,
                )
                self._emit_match_candidate(
                    trace_id=trace_id,
                    keyword=keyword,
                    row=row,
                    verdict="skip",
                    score=0.0,
                    matched_signals=[],
                    concerns=[],
                    hard_violations=[],
                    reason=f"matcher_exception:{type(exc).__name__}",
                    shortlisted=False,
                    stage_label=stage_label,
                )
                continue

            verdict = str(result.verdict or "").strip().lower()
            shortlisted = verdict in shortlist_verdicts and not result.hard_violations

            self._emit_match_candidate(
                trace_id=trace_id,
                keyword=keyword,
                row=row,
                verdict=verdict,
                score=float(result.score),
                matched_signals=list(result.matched_signals),
                concerns=list(result.concerns),
                hard_violations=list(result.hard_violations),
                reason=result.reason,
                shortlisted=shortlisted,
                stage_label=stage_label,
            )

            if not shortlisted:
                if errors is not None and result.hard_violations:
                    reason_excerpt = ";".join(result.hard_violations[:3])
                    errors.append(
                        f"skip:matcher_hard_violations company={row.get('company') or '?'} "
                        f"violations={reason_excerpt}"
                    )
                if skipped_audit is not None:
                    skipped_audit.append(
                        {
                            "company": row.get("company"),
                            "job_title": row.get("title"),
                            "verdict": verdict,
                            "hard_violations": list(result.hard_violations)[:3],
                            "reason": (result.reason or "")[:160],
                        }
                    )
                continue

            row["match_score"] = result.score
            row["match_verdict"] = verdict
            row["match_signals"] = list(result.matched_signals)
            row["match_concerns"] = list(result.concerns)
            row["match_hard_violations"] = list(result.hard_violations)
            row["match_reason"] = result.reason
            out.append(row)
        return out

    def _refine_with_detail(
        self,
        list_shortlist: list[dict[str, Any]],
        *,
        snapshot: JobMemorySnapshot | None,
        keyword: str,
        errors: list[str],
        trace_id: str | None,
        target_count: int,
    ) -> list[dict[str, Any]]:
        """Stage-B: fetch JD detail for top list-shortlist rows and re-score.

        Stage-A matcher runs on metadata only (title/company/salary/snippet).
        That's enough to drop obvious misses (city/salary/avoid_trait) without
        paying detail-page cost. Survivors come here, where we pull the JD
        body and re-run the matcher; the matcher prompt automatically
        deepens when ``row['detail']`` is present (see ``matcher._render_job``).

        ``target_count`` caps how many detail-fetches we pay for. We refine
        a 1.5× headroom over target so detail-stage poor/skip drops still
        leave enough survivors for outreach. List rows are pre-sorted by
        code-side ranking so refinement spends budget on the most promising
        candidates first.
        """
        if not list_shortlist or target_count <= 0:
            return []
        list_shortlist.sort(
            key=lambda row: float(row.get("match_score") or 0.0),
            reverse=True,
        )
        refine_budget = max(target_count, int(target_count * 1.5))
        refine_pool = list_shortlist[:refine_budget]

        enriched: list[dict[str, Any]] = []
        for row in refine_pool:
            detail_call = self._connector.fetch_job_detail(
                job_id=str(row.get("job_id") or ""),
                source_url=str(row.get("source_url") or ""),
            )
            detail = detail_call.get("detail") if isinstance(detail_call, dict) else None
            if isinstance(detail, dict) and detail:
                row["detail"] = detail
            else:
                err = str(detail_call.get("error") or "").strip() if isinstance(detail_call, dict) else ""
                if err:
                    errors.append(
                        f"detail_fetch_failed company={row.get('company') or '?'} err={err[:160]}"
                    )
            enriched.append(row)

        return self._score_items(
            enriched,
            snapshot=snapshot,
            keyword=keyword,
            errors=errors,
            trace_id=trace_id,
            skipped_audit=None,
            stage_label="detail",
        )

    def _collect_round(
        self,
        *,
        keywords: list[str],
        snapshot: JobMemorySnapshot | None,
        job_type: str,
        fetch_detail: bool,
        trace_id: str | None,
        seen_urls: set[str],
        target_count: int = 5,
    ) -> tuple[list[dict[str, Any]], list[str], RoundSummary, bool]:
        """One additional scan + two-stage match round driven by reflection.

        Mirrors the trigger main path: streaming-scroll scan → list-stage
        matcher → detail-stage refinement. Cross-round dedup is handled
        here via ``seen_urls`` — never return an item whose ``source_url``
        was already shortlisted by an earlier round, even if it survives
        all filters again. The set is mutated in-place so the caller's
        view stays in sync.
        ``fetch_detail`` only controls whether stage-B detail refinement is
        enabled. Regardless of the flag, scan itself stays list-only; detail
        fetch (when enabled) runs over stage-A survivors only.

        Returns ``round_exhausted`` as the 4th tuple item so caller can
        enforce the hard invariant: keyword evolution is allowed only after
        the current keyword space is confirmed exhausted.
        """
        round_items_target = max(target_count, 20)
        scan = self._run_trigger_scan(
            keywords=keywords,
            target_count=max(round_items_target, target_count * 6),
            evaluation_cap=120,
            scroll_plateau_rounds=3,
            job_type=job_type,
            fetch_detail=False,
            trace_id=trace_id,
        )
        round_items = list(scan.get("items") or [])
        round_errors: list[str] = list(scan.get("errors") or [])

        pipeline = self._apply_filter_pipeline(round_items, snapshot=snapshot)
        round_errors.extend(pipeline.pref_reasons)
        round_errors.extend(pipeline.hc_reasons)
        round_errors.extend(pipeline.dedup_reasons)

        kept_after_seen = [
            row for row in pipeline.kept
            if str(row.get("source_url") or "").strip() not in seen_urls
        ]
        skipped_audit: list[dict[str, Any]] = []
        primary_keyword = keywords[0] if keywords else ""
        list_shortlist = self._score_items(
            kept_after_seen,
            snapshot=snapshot,
            keyword=primary_keyword,
            errors=round_errors,
            trace_id=trace_id,
            skipped_audit=skipped_audit,
            stage_label="list",
        )
        if fetch_detail:
            scored = self._refine_with_detail(
                list_shortlist,
                snapshot=snapshot,
                keyword=primary_keyword,
                errors=round_errors,
                trace_id=trace_id,
                target_count=target_count,
            )
        else:
            scored = list_shortlist
        for row in scored:
            url = str(row.get("source_url") or "").strip()
            if url:
                seen_urls.add(url)
        summary = RoundSummary(
            keyword=primary_keyword,
            scanned_total=len(round_items),
            shortlisted_total=len(scored),
            skipped_examples=skipped_audit[:8],
        )
        return scored, round_errors, summary, bool(scan.get("exhausted"))

    @staticmethod
    def _render_hard_constraints_md(snapshot: JobMemorySnapshot | None) -> str:
        """Compact, LLM-readable view of hard constraints for the planner.

        Reuses the snapshot's prompt section but trims preference items
        the planner doesn't need (it already gets skip reasons separately).
        Empty snapshot returns an explicit '(none set)' so the LLM cannot
        confuse "no constraints" with "constraints withheld".
        """
        if snapshot is None:
            return "(none set)"
        return snapshot.to_prompt_section()

    def _emit_match_candidate(
        self,
        *,
        trace_id: str | None,
        keyword: str,
        row: dict[str, Any],
        verdict: str,
        score: float,
        matched_signals: list[str],
        concerns: list[str],
        hard_violations: list[str],
        reason: str,
        shortlisted: bool,
        stage_label: str = "list",
    ) -> None:
        """Emit one structured per-JD match audit event.

        We deliberately keep the payload small (no full snippet/detail
        bodies) — the goal is offline triage of "why was this JD skipped",
        not full JD replay. ``source_url`` lets a human re-open the
        original JD if they want to challenge the LLM's verdict.

        ``stage_label`` distinguishes list-only vs detail-aware verdicts
        for the same JD: the same source_url can appear twice with
        different verdicts when stage-A skipped on metadata and stage-B
        re-evaluated after ``fetch_job_detail``.
        """
        self._emit(
            stage="match",
            status="candidate",
            trace_id=trace_id,
            payload={
                "keyword": keyword,
                "match_stage": stage_label,
                "job_id": row.get("job_id"),
                "company": row.get("company"),
                "job_title": row.get("title"),
                "city": row.get("city"),
                "salary": row.get("salary"),
                "source_url": row.get("source_url"),
                "verdict": verdict,
                "score": round(float(score), 1),
                "shortlisted": bool(shortlisted),
                "hard_violations": hard_violations[:6],
                "matched_signals": matched_signals[:6],
                "concerns": concerns[:6],
                "reason": (reason or "")[:240],
            },
        )

    def _compose_greeting(
        self,
        *,
        job: dict[str, Any],
        snapshot: JobMemorySnapshot | None,
        override: str | None,
    ) -> str:
        """返回单条 JD 的招呼文本 — 优先级: override > greeter > template > default。"""
        if override:
            return override
        template = (self._policy.greeting_template or "").strip()
        if self._greeter is not None:
            match = self._match_from_item(job)
            try:
                draft = self._greeter.compose(
                    job=job,
                    snapshot=snapshot,
                    match=match,
                    template=template,
                )
                return draft.greeting_text
            except (RuntimeError, ValueError, TypeError) as exc:  # pragma: no cover
                logger.warning("greet greeter failed, fallback to template: %s", exc)
        if template:
            try:
                return template.format(job_title=str(job.get("title") or "该岗位"))
            except (KeyError, IndexError, ValueError):
                return template
        return "你好，我对这个岗位很感兴趣，期待进一步沟通。"

    @staticmethod
    def _match_from_item(job: dict[str, Any]) -> MatchResult | None:
        """从已经被 matcher 写入的 item 顶层 key 里重建一个 MatchResult,
        供 greeter 参考 matched_signals / concerns 做个性化。"""
        verdict = str(job.get("match_verdict") or "").strip().lower()
        if not verdict:
            return None
        try:
            score = float(job.get("match_score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        signals = [str(s) for s in (job.get("match_signals") or []) if str(s).strip()]
        concerns = [str(s) for s in (job.get("match_concerns") or []) if str(s).strip()]
        hard_violations = [
            str(s) for s in (job.get("match_hard_violations") or []) if str(s).strip()
        ]
        return MatchResult(
            score=score,
            verdict=verdict,
            matched_signals=signals,
            concerns=concerns,
            hard_violations=hard_violations,
            reason=str(job.get("match_reason") or ""),
        )

    def _apply_filter_pipeline(
        self,
        items: list[dict[str, Any]],
        *,
        snapshot: JobMemorySnapshot | None,
    ) -> FilterPipelineResult:
        """单一入口汇总 pref → hc → dedup 三段过滤 (F7).

        两类调用方复用它:
          * ``run_scan(apply_filters=True)``: 给 LLM 回的纯读结果就已经按用户硬
            约束 / 投递历史过滤过, 不用靠 LLM 自觉避免越界.
          * ``run_trigger``: 真正投递前再过一次, 保证 scan → trigger 链路里的
            偏好不过期 (用户在两次调用之间更新过偏好也会被本次 snapshot 捕获).

        这里**不发事件** — 事件粒度由 caller 决定 (trigger 拆 3 段,
        scan 发 1 条汇总).
        """
        after_pref, pref_reasons = self._filter_by_preferences(items, snapshot=snapshot)
        after_hc, hc_reasons = self._apply_hard_constraints(after_pref, snapshot=snapshot)
        dedup_urls = self._dedup_url_set(snapshot)
        after_dedup, dedup_reasons = self._apply_dedup(after_hc, dedup_urls=dedup_urls)
        return FilterPipelineResult(
            kept=after_dedup,
            pref_reasons=pref_reasons,
            hc_reasons=hc_reasons,
            dedup_reasons=dedup_reasons,
            dedup_urls=dedup_urls,
        )

    def _dedup_url_set(self, snapshot: JobMemorySnapshot | None) -> set[str]:
        """聚合两个去重来源 (F4 主 + 副通路, F7 复用).

        主: ``GreetRepository.all_greeted_urls()`` — actions 表里所有成功
            greet 过的 source_url, 跨天跨 session 都算.
        副: ``_urls_from_application_events(snapshot)`` — JobMemory 里的
            application_event items 解析出的 URL; 允许用户手工/跨模块补录.
        """
        all_greeted: set[str] = set()
        try:
            all_greeted = self._repository.all_greeted_urls()
        except Exception as exc:  # pragma: no cover - repository 失败不阻塞
            logger.warning("greet: repository.all_greeted_urls failed: %s", exc)
        return all_greeted | self._urls_from_application_events(snapshot)

    def _filter_by_preferences(
        self,
        items: list[dict[str, Any]],
        *,
        snapshot: JobMemorySnapshot | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """按 snapshot 中的黑名单过滤。

        返回 ``(kept, reasons)`` — reasons 是 audit 级的 skip 原因串,
        调用方会把它拼进 result.errors 供观测。没有 snapshot (== 没配 JobMemory /
        数据库不可用) 直接透传, 不做任何过滤。

        过滤顺序:
          1) ``avoid_company`` 字面公司名命中
          2) ``avoid_trait`` 展开后的公司集合命中 (TraitCompanyExpander + cache)
          3) 标题/snippet 中命中 avoid_trait/avoid_keyword 目标词 (字面子串)
        """
        if snapshot is None:
            return list(items), []
        trait_company_lookup = self._resolve_avoid_trait_company_lookup(snapshot)
        kept: list[dict[str, Any]] = []
        reasons: list[str] = []
        for item in items:
            company = str(item.get("company") or "").strip()
            title = str(item.get("title") or "")
            snippet = str(item.get("snippet") or "")
            avoided, avoid_reason = snapshot.is_company_avoided(company)
            if avoided:
                reasons.append(
                    f"skip:company_avoided company={company} reason={avoid_reason or '-'}"
                )
                continue
            if company:
                trait_hits = trait_company_lookup.get(company.casefold())
                if trait_hits:
                    traits = ",".join(sorted(trait_hits))
                    reasons.append(
                        f"skip:avoid_trait_company_set company={company} traits={traits}"
                    )
                    continue
            haystack = f"{title}\n{snippet}"
            hit, which = snapshot.find_avoided_target_in(haystack)
            if hit:
                reasons.append(f"skip:trait_avoided target={which} company={company}")
                continue
            kept.append(item)
        if reasons:
            logger.info("greet preference filter dropped %d items", len(reasons))
        return kept, reasons

    def _resolve_avoid_trait_company_lookup(
        self,
        snapshot: JobMemorySnapshot,
    ) -> dict[str, set[str]]:
        expander = self._trait_expander
        if expander is None:
            return {}
        try:
            expanded = expander.resolve_avoid_trait_companies(snapshot=snapshot)
        except (RuntimeError, ValueError, TypeError) as exc:
            raise RuntimeError(f"trait company expansion failed: {exc}") from exc
        lookup: dict[str, set[str]] = {}
        for trait, companies in expanded.items():
            trait_name = str(trait or "").strip()
            if not trait_name:
                continue
            for company in companies:
                clean_company = str(company or "").strip()
                if not clean_company:
                    continue
                marker = clean_company.casefold()
                lookup.setdefault(marker, set()).add(trait_name)
        return lookup

    def _apply_hard_constraints(
        self,
        items: list[dict[str, Any]],
        *,
        snapshot: JobMemorySnapshot | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """按 snapshot.hard_constraints 做**工具层**硬过滤 (F3)。

        设计原则:
          * **保守**: 只在"证据清晰"时丢弃 — 解析不出薪资/城市/level 就放过,
            避免把未解析信号当作违反硬约束。
          * **显式**: 丢弃原因写进 reasons (供 errors 字段回显), 并发
            ``trigger.hc_filter`` 事件用于审计。
          * **前置**: 放在 matcher LLM 之前, 省 per-item LLM 调用 并保证业务
            边界不会因为 LLM 一时判断失误就被绕开。
        """
        if snapshot is None or snapshot.hard_constraints.is_empty():
            return list(items), []
        hc = snapshot.hard_constraints
        kept: list[dict[str, Any]] = []
        reasons: list[str] = []
        for item in items:
            verdict = self._check_hard_constraints(item, hc)
            if verdict is None:
                kept.append(item)
                continue
            reasons.append(verdict)
        if reasons:
            logger.info(
                "greet hard-constraint filter dropped %d items (kept %d)",
                len(reasons), len(kept),
            )
        return kept, reasons

    def _check_hard_constraints(
        self, item: dict[str, Any], hc: HardConstraints
    ) -> str | None:
        """返回 skip 原因字符串; None 表示放行.

        **Agent-first 硬过滤契约** (2026-04 重构):
          只在**结构化证据清晰**时硬丢. 判断依据仅限:
            - item["detail"]["city"] (或 location/address 结构字段)
            - item["salary"] 文本 (解析需显式 K/月单位, 见 _parse_salary_range_k)
            - item["detail"]["experience_level"] (结构字段)
          **不再**用 JD 文本做 substring 关键词匹配来反推 city / level —
          那是传统工作流的启发式套路, 会在"上海岗位 (杭州可选)"这种用词上误判.
          结构化字段缺失时 → 放行给 matcher LLM, 由 LLM 读 JD 原文 + system prompt 里
          的 hard_constraint 自行判断.
        """
        company = str(item.get("company") or "").strip()
        detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}

        # 1) preferred_location: 仅看 detail.city / location / address 结构字段
        if hc.preferred_location:
            jd_city = _detect_city_structured(detail)
            if jd_city and not _city_matches_any(jd_city, hc.preferred_location):
                return (
                    f"skip:hc_location company={company or '?'} "
                    f"jd_city={jd_city} preferred={list(hc.preferred_location)}"
                )

        # 2) salary_floor_monthly: JD salary 文本有明确 K/月 上限且低于 floor 才丢
        if hc.salary_floor_monthly is not None:
            salary_raw = str(item.get("salary") or "").strip()
            _, ceiling = _parse_salary_range_k(salary_raw)
            if ceiling is not None and ceiling < hc.salary_floor_monthly:
                return (
                    f"skip:hc_salary company={company or '?'} "
                    f"jd_salary={salary_raw or '-'} "
                    f"floor={hc.salary_floor_monthly}K"
                )

        # 3) experience_level: 先看结构字段, 缺失时允许用 title/snippet 强信号兜底
        if hc.experience_level:
            hc_level = _EXP_LEVEL_ALIASES.get(hc.experience_level.strip().lower())
            jd_level_raw = detail.get("experience_level") or detail.get("exp_level")
            jd_level = None
            if hc_level and jd_level_raw:
                jd_level = _EXP_LEVEL_ALIASES.get(str(jd_level_raw).strip().lower())
            if hc_level and jd_level is None:
                jd_level = self._infer_experience_level_from_text(
                    str(item.get("title") or ""),
                    str(item.get("job_title") or ""),
                    str(item.get("snippet") or ""),
                )
            if hc_level and jd_level is None:
                jd_level = self._infer_experience_level_from_salary_text(
                    str(item.get("salary") or ""),
                )
            if hc_level and jd_level:
                if jd_level and jd_level != hc_level:
                    return (
                        f"skip:hc_experience_level company={company or '?'} "
                        f"hc={hc_level} jd={jd_level}"
                    )
        return None

    @staticmethod
    def _infer_experience_level_from_text(*texts: str) -> str | None:
        merged = " ".join(str(t or "").strip().lower() for t in texts if str(t or "").strip())
        if not merged:
            return None
        if any(tok in merged for tok in _INTERN_HINT_KEYWORDS):
            return "intern"
        if any(tok in merged for tok in _FULL_TIME_STRONG_HINT_KEYWORDS):
            return "full_time"
        return None

    @staticmethod
    def _infer_experience_level_from_salary_text(salary_text: str) -> str | None:
        raw = str(salary_text or "").strip()
        if not raw:
            return None
        lowered = raw.lower()
        if any(tok in lowered for tok in _DAILY_SALARY_HINT_KEYWORDS):
            return "intern"
        if any(tok in lowered for tok in _MONTHLY_SALARY_HINT_KEYWORDS):
            return "full_time"
        if re.search(r"\b\d+(?:\.\d+)?\s*[-~至]\s*\d+(?:\.\d+)?\s*[kw]\b", lowered):
            return "full_time"
        if re.search(r"\b\d+(?:\.\d+)?\s*[kw]\b", lowered):
            return "full_time"
        return None

    # ------------------------------------------------------------------ dedup

    def _apply_dedup(
        self,
        items: list[dict[str, Any]],
        *,
        dedup_urls: set[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """按已投递过的 source_url 去重 (F4)。

        ``dedup_urls`` 由调用方合并 (today+history greeted URLs ∪
        application_event items 记录的 URL), 这里只做 O(N) 过滤并输出审计
        原因串。
        """
        if not dedup_urls:
            return list(items), []
        kept: list[dict[str, Any]] = []
        reasons: list[str] = []
        for item in items:
            url = str(item.get("source_url") or "").strip()
            if url and url in dedup_urls:
                reasons.append(
                    f"skip:already_greeted company={item.get('company') or '?'} "
                    f"url={url}"
                )
                continue
            kept.append(item)
        if reasons:
            logger.info("greet dedup filter dropped %d items", len(reasons))
        return kept, reasons

    @staticmethod
    def _urls_from_application_events(
        snapshot: JobMemorySnapshot | None,
    ) -> set[str]:
        """从 snapshot 的 application_event items 里抽 source_url (F4 副通路)。

        ``application_event`` items 的 raw_text 字段里放 JSON 形态的结构化
        元数据 (见 ``_record_application_events``); 解析失败的 item 不致命,
        会被简单忽略, 由 repository 的 URL 主通路兜底。
        """
        if snapshot is None:
            return set()
        urls: set[str] = set()
        for it in snapshot.active_items():
            if it.type != "application_event":
                continue
            raw = str(it.raw_text or "").strip()
            if not raw:
                continue
            try:
                meta = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(meta, dict):
                continue
            url = str(meta.get("source_url") or "").strip()
            if url:
                urls.add(url)
        return urls

    def _record_application_events(
        self,
        details: list[dict[str, Any]],
        *,
        run_id: str,
        keyword: str,
    ) -> None:
        """成功的招呼写入一条 ``application_event`` MemoryItem (F4)。

        为什么要重复写 (DB actions 表已经有记录)?
          * **可移植**: actions 表是关系库运营资产, Domain Memory 是 LLM 可见
            的上下文. 没有这条记忆, LLM 在后续对话里无从得知 "已经投过了",
            不能如实回答用户 "之前投过谁?".
          * **用户可增量**: 若未来用户手动告诉我们 "我在别的渠道投过 X", 同一
            schema 可以被 PreferenceExtractor → DomainPreferenceDispatcher
            路径直接写入, 无需再分叉一条代码。
          * **可审计**: 作为 JobMemorySnapshot 的一部分, 会被 brain/matcher/
            greeter 的 prompt 看到, 使 "已投递" 成为显式业务状态。

        写失败不会阻断主投递流程, 只记 warning — actions 表作为主存档仍然
        完整, dedup 下一轮会走 repository 主通路。
        """
        if self._preferences is None:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        for row in details:
            if str(row.get("status") or "").strip() != "greeted":
                continue
            url = str(row.get("source_url") or "").strip()
            company = str(row.get("company") or "").strip() or None
            title = str(row.get("job_title") or "").strip() or "该岗位"
            meta = {
                "source_url": url,
                "job_id": str(row.get("job_id") or ""),
                "run_id": run_id,
                "keyword": keyword,
                "greeted_at": now_iso,
                "provider": str(row.get("provider") or ""),
            }
            try:
                self._preferences.record_item({
                    "type": "application_event",
                    "target": company,
                    "content": f"已投递 {company or '未知公司'} · {title}",
                    "raw_text": json.dumps(meta, ensure_ascii=False),
                })
            except Exception as exc:   # noqa: BLE001 — 不阻断主流程
                logger.warning(
                    "greet: failed to record application_event for %s (%s): %s",
                    url, company, exc,
                )

    def _make_preview_row(
        self,
        item: dict[str, Any],
        *,
        run_id: str,
        greeting_text: str,
        scan: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "job_id": item.get("job_id"),
            "job_title": item["title"],
            "company": item["company"],
            "match_score": item["match_score"],
            "match_verdict": item.get("match_verdict"),
            "match_reason": item.get("match_reason"),
            "status": "pending_confirmation",
            "greeting_text": greeting_text,
            "source_url": item["source_url"],
            "source": item.get("source") or scan.get("source"),
        }

    def _normalize_scan_item(self, keyword: str, row: dict[str, Any]) -> dict[str, Any]:
        source_url = str(row.get("source_url") or row.get("url") or "").strip()
        title_raw = str(row.get("title") or "").strip()
        dedupe_seed = (source_url or title_raw or json.dumps(row, ensure_ascii=False)[:120]).lower()
        title = _guess_title(title_raw, keyword=keyword)
        snippet = token_preview(
            str(row.get("snippet") or row.get("description") or title_raw or ""),
            max_tokens=700,
        )
        company_raw = str(row.get("company") or "").strip()
        company = company_raw if company_raw else _guess_company(title_raw, source_url)
        if not source_url:
            # Provider couldn't give us a URL at all; manufacture a stable,
            # provider-neutral placeholder so downstream de-dup still works.
            source_url = f"pulse://{self._connector.provider_name}/job/{_sha16(dedupe_seed)}"
        job_id = str(row.get("job_id") or "").strip() or _sha16(source_url)
        detail_raw = row.get("detail")
        detail = dict(detail_raw) if isinstance(detail_raw, dict) else {}
        return {
            "job_id": job_id,
            "title": title,
            "company": company,
            "salary": row.get("salary"),
            "source_url": source_url,
            "snippet": snippet,
            "detail": detail,
            "match_score": _score_keyword_match(keyword, title, snippet),
            "source": str(row.get("source") or "").strip() or self._connector.provider_name,
            "collected_at": str(row.get("collected_at") or datetime.now(timezone.utc).isoformat()),
        }


# ---------------------------------------------------------------------------
# module-private helpers (kept here because they have no other reuse)
# ---------------------------------------------------------------------------


def _sha16(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _score_keyword_match(keyword: str, title: str, snippet: str) -> float:
    lowered = f"{title} {snippet}".lower()
    key = keyword.strip().lower()
    if not key:
        return 60.0
    score = 52.0
    if key in lowered:
        score += 28.0
    tokens = [token for token in key.replace("/", " ").replace("-", " ").split() if token]
    if tokens:
        hits = sum(1 for token in tokens if token in lowered)
        score += (hits / max(1, len(tokens))) * 20.0
    return round(max(35.0, min(score, 97.0)), 1)


def _guess_title(raw_title: str, *, keyword: str) -> str:
    title = re.sub(r"\s+", " ", str(raw_title or "").strip())
    if not title:
        return f"{keyword} 招聘信息"
    for sep in (" - ", " | ", " _ ", "｜", "|", "-", "_"):
        if sep in title:
            candidate = title.split(sep, 1)[0].strip()
            if len(candidate) >= 4:
                return candidate[:120]
    return title[:120]


_SALARY_DEFAULT_WORK_DAYS_PER_MONTH = 22


def _parse_salary_range_k(salary: str) -> tuple[int | None, int | None]:
    """把常见薪资文本拆成 (floor, ceiling), 单位 K/月。

    规则:
      * ``20-40K`` / ``20-40k`` → (20, 40)
      * ``15K``                → (15, 15)
      * ``200-300元/天``        → (4, 7)  # 22 工作日/月, round 到 K/月
      * ``20-40·15薪``         → (20, 40) — ``·N薪`` 不影响月薪区间
      * 无法识别 (如 ``面议`` / 空) → (None, None)
      * 时薪等无法稳定换算的单位返回 (None, None), 不乱猜
    """
    text = str(salary or "").strip()
    if not text:
        return None, None
    lowered = text.lower()
    if any(tok in lowered for tok in ("时薪", "小时", "面议")):
        return None, None
    is_daily = any(tok in lowered for tok in ("日薪", "/天", "／天", "每天", "元/天", "元／天"))
    match = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)", lowered)
    if match:
        try:
            lo = float(match.group(1))
            hi = float(match.group(2))
        except ValueError:
            return None, None
        if hi < lo:
            lo, hi = hi, lo
        if is_daily:
            return _daily_yuan_to_monthly_k(lo), _daily_yuan_to_monthly_k(hi)
        if "k" not in lowered:
            return None, None
        return int(lo), int(hi)
    single = re.search(r"(\d+(?:\.\d+)?)\s*k", lowered)
    if single:
        try:
            v = int(float(single.group(1)))
        except ValueError:
            return None, None
        return v, v
    daily_single = re.search(
        r"(?:日薪)?\s*(\d+(?:\.\d+)?)\s*(?:元)?\s*(?:/|／)?\s*(?:天|日)?",
        lowered,
    ) if is_daily else None
    if daily_single:
        try:
            v = float(daily_single.group(1))
        except ValueError:
            return None, None
        monthly_k = _daily_yuan_to_monthly_k(v)
        return monthly_k, monthly_k
    return None, None


def _daily_yuan_to_monthly_k(amount_yuan: float) -> int:
    return max(
        1,
        int(round(float(amount_yuan) * _SALARY_DEFAULT_WORK_DAYS_PER_MONTH / 1000.0)),
    )


def _dedupe_nonempty(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _detect_city_structured(detail: dict[str, Any] | None) -> str | None:
    """仅从 provider 的**结构化** detail 字段读取 JD 所在城市.

    查找键顺序: ``city`` > ``location`` > ``address`` > ``work_place`` /
    ``workplace``. 找到第一个非空串即返回 (保留原文 — 交给
    :func:`_city_matches_any` 做 casefold / substring 比较).

    **为什么不再扫全文 / 不再维护城市白名单**:
      substring 命中"上海岗位 (杭州可选)"会把用户偏好=杭州的 JD 误判成上海;
      维护 Top-N 城市名单又会漏掉"嘉兴""合肥高新区"之类. 硬约束过滤只应基
      于清晰的结构化信号. 无结构化字段 → 放行给下游 matcher LLM, 由 LLM 读
      JD 原文 + system prompt 里 preferred_location 自行判断.
    """
    if not isinstance(detail, dict):
        return None
    for key in ("city", "location", "address", "work_place", "workplace"):
        value = detail.get(key)
        if value in (None, "", [], {}):
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _city_matches_any(jd_city: str, preferred: list[str]) -> bool:
    """判断 JD 所在城市是否落在 preferred_location 之内 (大小写 / 前缀宽松)。"""
    if not jd_city:
        return True
    jd_norm = str(jd_city).strip().casefold()
    for loc in preferred:
        pref = str(loc or "").strip().casefold()
        if not pref:
            continue
        if pref == jd_norm or pref in jd_norm or jd_norm in pref:
            return True
    return False


def _guess_company(title: str, url: str) -> str:
    for sep in (" - ", " | ", " _ ", "｜", "|", "-", "_"):
        if sep in title:
            parts = [item.strip() for item in title.split(sep) if item.strip()]
            if len(parts) >= 2:
                return parts[1][:80]
    if "://" in url:
        host = url.split("://", 1)[1].split("/", 1)[0].strip()
        if host:
            return host[:80]
    return "Unknown"
