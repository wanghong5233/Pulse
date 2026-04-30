"""Controller / entry-point for the ``job.greet`` capability.

Thin adapter on top of :class:`JobGreetService`:

  * wires the service with a concrete :class:`JobPlatformConnector`, a
    :class:`GreetRepository`, the workspace-scoped preference store, the
    LLM-backed matcher/greeter, and the HITL policy.
  * exposes HTTP routes under ``/api/modules/job/greet``.
  * registers a patrol task on the AgentRuntime when enabled.
  * **暴露 IntentSpec 细粒度 tool** 供 Brain tool_use 调用 (scan / trigger)。
    粗粒度 ``handle_intent`` 已随 router_rules.json 业务规则下线, 不再实现;
    见 ``docs/Pulse-DomainMemory与Tool模式.md`` §4.3 / §7.3 M1。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ─────────────────────────────────────────────────────────────────────
# ToolUseContract §4.5 — extract_facts hooks (Contract C v2)
# ─────────────────────────────────────────────────────────────────────
# These project scan/trigger observations into tiny `{str: scalar}` dicts
# that CommitmentVerifier.TurnEvidence surfaces to the judge LLM. Goals:
#   * Let the judge tell "scanned 5 jobs" apart from "greeted 5 jobs".
#   * Expose confirm/execute outcome so claims like "已投递" can be
#     grounded on `greeted > 0` instead of just "tool ran".
#   * Never leak PII / long text / unbounded payload — only named scalars.
# Stay silent on errors (observation may be {"error": "..."}): default
# hook will surface that; we only add value on the happy path.


def _extract_facts_scan(observation: Any) -> dict[str, Any]:
    if not isinstance(observation, dict):
        return {}
    if observation.get("error"):
        return {}
    facts: dict[str, Any] = {"intent": "job.greet.scan"}
    for key in ("total", "pages_scanned", "source", "provider", "execution_ready"):
        val = observation.get(key)
        if isinstance(val, (str, int, float, bool)) and val is not None:
            facts[key] = val
    items = observation.get("items")
    if isinstance(items, list):
        facts["items_returned"] = len(items)
    handle = observation.get("scan_handle")
    if isinstance(handle, str) and handle:
        facts["has_scan_handle"] = True
    return facts


def _extract_facts_trigger(observation: Any) -> dict[str, Any]:
    if not isinstance(observation, dict):
        return {}
    if observation.get("error"):
        return {}
    facts: dict[str, Any] = {"intent": "job.greet.trigger"}
    # These four decide whether a "已投递" commitment is grounded.
    #   * ``needs_confirmation=True`` + ``greeted=0`` → PREVIEW, NOT sent
    #   * ``needs_confirmation=False`` + ``greeted>0`` → real send fulfilled
    #   * ``needs_confirmation=False`` + ``greeted=0`` → tried but threshold
    #     filtered everything out (legitimate "no match found" outcome)
    for key in (
        "ok",
        "needs_confirmation",
        "execution_ready",
        "greeted",
        "failed",
        "unavailable",
        "skipped",
        "daily_count",
        "daily_limit",
        "source",
        "provider",
    ):
        val = observation.get(key)
        if isinstance(val, (str, int, float, bool)) and val is not None:
            facts[key] = val
    matched = observation.get("matched_details")
    if isinstance(matched, list):
        facts["matched_count"] = len(matched)
    return facts

from fastapi import APIRouter

from ....core.llm.router import LLMRouter
from ....core.module import BaseModule, IntentSpec
from ....core.notify.notifier import ConsoleNotifier, Notifier
from ....core.scheduler.windows import is_active_hour, is_weekend
from ....core.storage.engine import DatabaseEngine
from ....core.task_context import TaskContext
from .._connectors import build_connector
from ..config import get_job_settings
from ..shared.models import JobGreetTriggerRequest, JobScanRunRequest
from ..memory import JobMemory
from .greeter import JobGreeter
from .matcher import JobSnapshotMatcher
from .reflection import ReflectionPlanner
from .repository import GreetRepository
from .service import GreetPolicy, JobGreetService
from .trait_expander import TraitCompanyExpander

logger = logging.getLogger(__name__)


_DEFAULT_GREET_LOG_PATH = Path.home() / ".pulse" / "boss_greet_log.jsonl"


class JobGreetModule(BaseModule):
    name = "job_greet"
    description = "Job-domain scan & greet capability (multi-platform ready)"
    route_prefix = "/api/modules/job/greet"
    tags = ["job", "job_greet"]

    def __init__(
        self,
        *,
        service: JobGreetService | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        super().__init__()
        self._settings = get_job_settings()
        self._notifier: Notifier = notifier or ConsoleNotifier()
        self._service = service or self._build_default_service()
        self.intents = self._build_intents()

    # ------------------------------------------------------------------ wiring

    def _build_default_service(self) -> JobGreetService:
        settings = self._settings
        connector = build_connector()
        engine: DatabaseEngine | None
        try:
            engine = DatabaseEngine()
        except Exception as exc:
            # DB is a soft dependency for greet telemetry; log but continue.
            logger.warning("greet module starting without DB engine: %s", exc)
            engine = None
        repository = GreetRepository(
            engine=engine,
            fallback_log_path=_DEFAULT_GREET_LOG_PATH,
        )
        preferences: JobMemory | None = None
        if engine is not None:
            preferences = JobMemory.from_engine(
                engine,
                workspace_id=settings.default_workspace_id,
                source="job.greet",
            )
        policy = GreetPolicy(
            batch_size=settings.greet_batch_size,
            match_threshold=settings.greet_match_threshold,
            daily_limit=settings.greet_daily_limit,
            default_keyword=settings.greet_default_keyword,
            greeting_template=settings.greet_greeting_template,
            hitl_required=settings.hitl_required,
        )
        # matcher / greeter 只需要 LLM 配置, 不依赖 DB; 即使 engine=None
        # 也可以启用(那样打分/招呼全由 LLM 做, snapshot 为 None)。
        llm_router = LLMRouter()
        matcher = JobSnapshotMatcher(llm_router)
        greeter = JobGreeter(llm_router)
        trait_expander = TraitCompanyExpander(
            llm_router,
            preferences=preferences,
        )
        reflection_planner = ReflectionPlanner(llm_router)
        return JobGreetService(
            connector=connector,
            repository=repository,
            policy=policy,
            notifier=self._notifier,
            emit_stage_event=self.emit_stage_event,
            preferences=preferences,
            matcher=matcher,
            greeter=greeter,
            trait_expander=trait_expander,
            reflection_planner=reflection_planner,
        )

    # ------------------------------------------------------------------ AgentRuntime

    def on_startup(self) -> None:
        if not self._runtime:
            return
        # ADR-004 §6.1.1: 无条件注册, 初始 enabled=False
        # (runtime 默认值); 启停由 system.patrol.enable/disable 经 IM 独占
        # 控制 — 单一认知路径。interval 字段只决定调度节拍, 与启停语义正交。
        self._runtime.register_patrol(
            name="job_greet.patrol",
            handler=self._patrol,
            peak_interval=int(self._settings.patrol_greet_interval_peak),
            offpeak_interval=int(self._settings.patrol_greet_interval_offpeak),
            weekday_windows=((9, 12), (14, 18)),
            weekend_windows=(),
        )

    def _patrol(self, ctx: TaskContext) -> dict[str, Any]:
        _ = ctx
        return self._service.run_trigger(
            keyword=self._settings.greet_default_keyword,
            batch_size=None,
            match_threshold=None,
            confirm_execute=self._settings.greet_auto_execute,
            fetch_detail=True,
        )

    # ------------------------------------------------------------------ public API

    def run_scan(
        self,
        *,
        keyword: str,
        max_items: int,
        max_pages: int,
        job_type: str = "all",
        fetch_detail: bool = False,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        return self._service.run_scan(
            keyword=keyword,
            max_items=max_items,
            max_pages=max_pages,
            job_type=job_type,
            fetch_detail=fetch_detail,
            trace_id=trace_id,
        )

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
    ) -> dict[str, Any]:
        return self._service.run_trigger(
            keyword=keyword,
            batch_size=batch_size,
            match_threshold=match_threshold,
            greeting_text=greeting_text,
            job_type=job_type,
            run_id=run_id,
            confirm_execute=confirm_execute,
            fetch_detail=fetch_detail,
            trace_id=trace_id,
        )

    # ------------------------------------------------------------------ IntentSpec

    def _build_intents(self) -> list[IntentSpec]:
        """细粒度 intent 工具, 供 Brain tool_use 直接调用。

        不再走 ``handle_intent`` + regex 参数抽取; Brain 依据 JSON Schema 结构化
        抽参后调用 handler。两个 intent 都**不写入 memory** (``mutates=False``),
        但 ``trigger`` 会向 BOSS 发真实招呼, 故 ``risk_level=2``。定时 patrol
        由 ``PULSE_JOB_GREET_AUTO_EXECUTE`` 控制是否自动真发; 交互式 trigger
        仍用 ``confirm_execute`` 区分预览 / 执行。

        见 ``docs/Pulse-DomainMemory与Tool模式.md`` §4.3 / §7.3 M1。
        """
        s = self._service
        default_keyword = self._settings.greet_default_keyword

        def _scan_handler(**kwargs: Any) -> dict[str, Any]:
            # apply_filters 默认 True: scan 返回的 items 已经按用户 JobMemory 里
            # 存的偏好 (hard constraints + 黑名单 + 历史投递 URL) 过滤; LLM 拿到
            # 的候选集自带"已遵守业务边界"的保证, 不靠它自觉过滤. 调用方可以
            # 显式传 false 跳过 (目前仅内部 run_trigger 用, 避免双重过滤).
            return s.run_scan(
                keyword=str(kwargs.get("keyword") or default_keyword),
                max_items=int(kwargs.get("max_items") or 10),
                max_pages=int(kwargs.get("max_pages") or 1),
                job_type=str(kwargs.get("job_type") or "all"),
                fetch_detail=bool(kwargs.get("fetch_detail") or False),
                apply_filters=bool(kwargs.get("apply_filters", True)),
            )

        def _trigger_handler(**kwargs: Any) -> dict[str, Any]:
            batch_size = kwargs.get("batch_size")
            if batch_size is not None:
                try:
                    batch_size = int(batch_size)
                except (TypeError, ValueError):
                    batch_size = None
            threshold = kwargs.get("match_threshold")
            if threshold is not None:
                try:
                    threshold = float(threshold)
                except (TypeError, ValueError):
                    threshold = None
            greeting_text = kwargs.get("greeting_text")
            scan_handle_raw = kwargs.get("scan_handle")
            scan_handle = (
                str(scan_handle_raw).strip()
                if scan_handle_raw not in (None, "")
                else None
            )
            return s.run_trigger(
                keyword=str(kwargs.get("keyword") or default_keyword),
                batch_size=batch_size,
                match_threshold=threshold,
                greeting_text=str(greeting_text) if greeting_text else None,
                job_type=str(kwargs.get("job_type") or "all"),
                confirm_execute=bool(kwargs.get("confirm_execute") or False),
                fetch_detail=bool(kwargs.get("fetch_detail", True)),
                scan_handle=scan_handle,
            )

        return [
            IntentSpec(
                name="job.greet.scan",
                extract_facts=_extract_facts_scan,
                description=(
                    "Read-only: scan recruiting platform job listings by keyword. "
                    "Does NOT send any greetings. Does NOT run the trigger-only "
                    "ReflectionPlanner loop (keyword evolve + extra scan rounds live in "
                    "`job.greet.trigger`). Results are pre-filtered by the user's "
                    "stored hard constraints (preferred_location / salary_floor_monthly / "
                    "experience_level), avoid lists, and past-greeted URLs. "
                    "Returns a `scan_handle` token — pass it to the next `job.greet.trigger` "
                    "to skip a redundant re-scan (Contract B hand-off)."
                ),
                when_to_use=(
                    "只读: 对招聘平台按 keyword 抓取岗位列表, 不触发任何消息发送。"
                    "返回结果已在平台侧按 JobProfile 的硬约束 (preferred_location / "
                    "salary_floor_monthly / experience_level) + avoid_list + 历史已打招呼 URL 过滤, "
                    "host 侧**不得**再做同维过滤 / 去重以免双重筛选。"
                    "用于任何需要\"先看候选、再决定动作\"的场景。"
                    "**Hand-off 契约**: 返回体里有 `scan_handle` 字段, 若下一步要真实投递, "
                    "把它传给 `job.greet.trigger` 可节省一次 MCP 浏览器调用。"
                ),
                when_not_to_use=(
                    "职责划分: 1) 需要**真实发送**打招呼 → `job.greet.trigger`; "
                    "2) 同一轮用户话语含「帮我投递/投递 N 个/打招呼/联系 HR」等**即时执行**意图时, "
                    "**禁止**仅以本工具结束本轮; 应先 `job.greet.scan` (可选) 再用 "
                    "`job.greet.trigger` 完成匹配与 (按语义) `confirm_execute`; "
                    "3) 修改/新增求职偏好 → `job.memory.record` / `job.hard_constraint.set`; "
                    "4) 读 HR 对话 → `job.chat.pull`; "
                    "5) 连接器未 ready 时返回 items=[], 不得据此编造岗位列表。"
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "keyword": {
                            "type": "string",
                            "description": (
                                "Search keyword. Extract the most specific role/tech phrase the user "
                                f"mentioned; fall back to '{default_keyword}' when nothing explicit."
                            ),
                        },
                        "max_items": {
                            "type": "integer",
                            "description": "Max jobs to return (default 10).",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 80,
                        },
                        "max_pages": {
                            "type": "integer",
                            "description": "Max search pages to crawl (default 1).",
                            "default": 1,
                            "minimum": 1,
                            "maximum": 5,
                        },
                        "job_type": {
                            "type": "string",
                            "description": "Posting type filter.",
                            "enum": ["all", "full_time", "internship"],
                            "default": "all",
                        },
                        "fetch_detail": {
                            "type": "boolean",
                            "description": "If true, also open each JD to fetch full description (slower).",
                            "default": False,
                        },
                    },
                    "required": ["keyword"],
                    "additionalProperties": False,
                },
                handler=_scan_handler,
                mutates=False,
                risk_level=0,
                examples=[
                    {
                        "user_utterance": "搜一下北京的 python 后端",
                        "kwargs": {"keyword": "python 后端", "max_items": 10},
                    },
                    {
                        "user_utterance": "看看有什么数据分析机会",
                        "kwargs": {"keyword": "数据分析"},
                    },
                ],
            ),
            IntentSpec(
                # ADR-001 §6 P3d (过渡) + P4 (终态) 跟踪:
                #   本 IntentSpec 让 LLM 在 planning 阶段判断 confirm_execute 是
                #   `true` 还是 `false`. 这是语义判断(imperative vs exploratory),
                #   LLM 本来就擅长, 但仍是"planning 阶段二次猜"——每多一次猜就
                #   多一次翻车概率. **根治方案 P4**: 把 action_intent 塞进
                #   `soul.reflection:pre_turn` 的结构化输出, Brain 在调 trigger
                #   之前直接预填 confirm_execute, 绕开 planning LLM 的判断.
                #   在 P4 落地前, 本段 description / examples 是"语义导向 contract A",
                #   不是关键词清单, 符合 code-review-checklist §B.2.
                name="job.greet.trigger",
                extract_facts=_extract_facts_trigger,
                description=(
                    "MUTATING: send real greetings to top matches on the recruiting platform. "
                    "Hosts the scan → filter → LLM match → optional ReflectionPlanner keyword "
                    "evolution (bounded extra scan rounds) pipeline. "
                    "Reuses the scan_handle from a previous job.greet.scan (preferred) or runs "
                    "its own scan when no handle is supplied. Respects daily_limit, blocked "
                    "companies/keywords, and the confirm_execute preview/execution switch."
                ),
                when_to_use=(
                    "唯一**一次性立即**在招聘平台下发打招呼的通道: scan → match_threshold → policy filter → send。"
                    "副作用: 平台侧消息发送、daily_limit 计数自增、greeted_urls 落盘。"
                    "`confirm_execute` 两态 (true=真发, false=仅预览) 如何选 —— "
                    "**唯一判据是本轮用户话语的语义类别**, 规则写在 `confirm_execute` 参数 description, "
                    "不要在这里/上层重复启发式判断. 任何\"投递/打招呼/批量 greet\"类动作"
                    "**必须**经此工具, 不得在 host 侧用 scan 结果伪装投递。"
                    "但用户说的是\"开启自动投递/自动打招呼服务/后台定时跑\"时, "
                    "那是 patrol 生命周期控制, 必须走 "
                    "`system.patrol.enable(name=\"job_greet.patrol\", trigger_now=false)`, "
                    "不是本工具。"
                    "**契约 B hand-off**: 如果刚在上一步调过 `job.greet.scan`, **必须**把它返回的 "
                    "`scan_handle` 传进来, 否则会重复扫一遍 (浪费一次 MCP 浏览器调用 + 可能触发 "
                    "`fetch_detail=True` 超时)。"
                ),
                when_not_to_use=(
                    "职责划分: 1) 仅浏览岗位 → `job.greet.scan`; "
                    "2) 回复已存在的 HR 会话 → `job.chat.process`; "
                    "3) 连接器未 ready (execution_ready=false) 时工具 fail-loud 返回错误原因, "
                    "  调用方**禁止**把 scan 里的 JD 列表当作\"已投递\"呈现给用户。"
                    "传了已过期 / 未知的 `scan_handle` 会 fail-loud 返回 "
                    "`error=scan_handle_unknown_or_expired`, 这时应该**重新**调用 `job.greet.scan` 取新 handle。"
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "keyword": {
                            "type": "string",
                            "description": (
                                "Search keyword; take from the user's utterance, falling back to "
                                f"'{default_keyword}' when unspecified."
                            ),
                        },
                        "scan_handle": {
                            "type": "string",
                            "description": (
                                "Contract B hand-off token: pass the `scan_handle` returned by the "
                                "most recent `job.greet.scan`. When present and fresh, trigger "
                                "skips the internal scan and reuses its cached items. Omit only "
                                "if you haven't called scan this turn (patrol / cold-start). "
                                "Unknown or expired tokens fail-loud; re-run scan to mint a new one."
                            ),
                        },
                        "batch_size": {
                            "type": "integer",
                            "description": "Override batch size (top-K to greet). Omit to use policy default.",
                            "minimum": 1,
                            "maximum": 20,
                        },
                        "match_threshold": {
                            "type": "number",
                            "description": "Override match-score threshold (0-100). Omit to use policy default.",
                            "minimum": 30.0,
                            "maximum": 95.0,
                        },
                        "greeting_text": {
                            "type": "string",
                            "description": (
                                "Optional override text. When omitted, the greeter generates a "
                                "personalized greeting per job using user preferences."
                            ),
                        },
                        "job_type": {
                            "type": "string",
                            "enum": ["all", "full_time", "internship"],
                            "default": "all",
                        },
                        "confirm_execute": {
                            "type": "boolean",
                            "description": (
                                "Two-mode execution flag:\n"
                                "  - false = preview only (returns match list, no greeting is sent)\n"
                                "  - true  = real send (MUTATING side-effect on the platform)\n"
                                "\n"
                                "Decision rule (the ONLY input you consider):\n"
                                "classify the **current-turn** user utterance into exactly one of:\n"
                                "  * IMPERATIVE   — a direct action request, with explicit intent to\n"
                                "                   execute now. The utterance itself IS the authorisation.\n"
                                "                   → pass `confirm_execute=true`.\n"
                                "  * EXPLORATORY — a request to inspect, list, or preview before deciding.\n"
                                "                   → pass `confirm_execute=false`.\n"
                                "\n"
                                "Scope: only the CURRENT user turn counts. A prior turn saying '投' does\n"
                                "NOT authorise the current turn. When a turn mixes constraints with an\n"
                                "imperative verb, the turn is IMPERATIVE — the constraints are filters,\n"
                                "not hesitation.\n"
                                "\n"
                                "Worked examples (utterance → classification → value):\n"
                                "  EXPLORATORY: 'show me what 数据分析 openings are around, 先给我看看'\n"
                                "               → false\n"
                                "  IMPERATIVE:  'now send greetings to 5 JD that fit me'\n"
                                "               → true\n"
                                "  IMPERATIVE:  'based on <constraints>, apply to 5 matching JD for me now'\n"
                                "               (constraints are filters; the leading verb is the order)\n"
                                "               → true\n"
                                "\n"
                                "Default false is the conservative fallback; override it only when the\n"
                                "classification is clearly IMPERATIVE. Decide from the sentential mood\n"
                                "(order vs question), not from keyword presence."
                            ),
                            "default": False,
                        },
                        "fetch_detail": {
                            "type": "boolean",
                            "description": "Fetch JD details before scoring (improves match quality).",
                            "default": True,
                        },
                    },
                    "required": ["keyword"],
                    "additionalProperties": False,
                },
                handler=_trigger_handler,
                mutates=False,
                # requires_confirmation=True 声明"这是高风险 MUTATING 工具类别" (ToolSpec 元
                # 数据, 用于审计 / UI 标注 / risk budget), 与 per-invocation 的 confirm_execute
                # 职责正交 —— 两者都保留. See ADR-001 §6 P3d 关于职责边界.
                requires_confirmation=True,
                risk_level=2,
                # NOTE: examples 字段目前**不**被 prompt_contract._section_tools 渲染,
                #       实际的 few-shot 契约靠 `confirm_execute.description` 里内嵌的
                #       worked examples 承担 (JSON schema description 由 LangChain
                #       完整透传给 LLM). examples 在此保留作为 code-review / 设计文档,
                #       并与 schema description 的 worked examples 保持语义一致.
                #       渲染路径的后续处置见 ADR-001 §6 P3d.
                examples=[
                    # EXPLORATORY — user asks to see/list before deciding → preview only.
                    {
                        "user_utterance": "有哪些数据分析的打招呼机会, 先给我看看",
                        "kwargs": {"keyword": "数据分析", "confirm_execute": False},
                    },
                    # IMPERATIVE — short direct order. Utterance itself is the authorisation.
                    {
                        "user_utterance": "刷一批数据分析的打招呼",
                        "kwargs": {"keyword": "数据分析", "confirm_execute": True},
                    },
                    # IMPERATIVE — constraints mixed with explicit command ('现在帮我投递 N 个');
                    # constraints are filters, not hesitation — see trace_4890841c2322.
                    {
                        "user_utterance": (
                            "你现在帮我投递5个合适的JD, 注意已经联系过的不要重复投递, "
                            "base 地点优先杭州或上海, 最低薪资不要低于300/天"
                        ),
                        "kwargs": {"batch_size": 5, "confirm_execute": True},
                    },
                    # IMPERATIVE — command-line style; always treated as direct execute.
                    {
                        "user_utterance": "/greet 算法 batch=5 threshold=80",
                        "kwargs": {
                            "keyword": "算法",
                            "batch_size": 5,
                            "match_threshold": 80.0,
                            "confirm_execute": True,
                        },
                    },
                ],
            ),
        ]

    # ------------------------------------------------------------------ HTTP

    def register_routes(self, router: APIRouter) -> None:
        @router.get("/health")
        async def health() -> dict[str, Any]:
            now = datetime.now(timezone.utc)
            connector_health = self._service.connector.health()
            return {
                "module": self.name,
                "status": "ok",
                "runtime": {
                    "mode": "real_connector" if self._service.connector.execution_ready else "degraded_connector",
                    "provider": self._service.connector.provider_name,
                    "hitl_required": self._service.policy.hitl_required,
                    "auto_execute": self._settings.greet_auto_execute,
                    "greet_log_path": str(self._service_greet_log_path()),
                    "connector": connector_health,
                    "weekday_guard": {
                        "is_weekend": is_weekend(now),
                        "is_active_hour": is_active_hour(
                            now,
                            weekday_start=9,
                            weekday_end=23,
                            weekend_start=10,
                            weekend_end=22,
                        ),
                    },
                },
            }

        @router.post("/scan")
        async def scan(payload: JobScanRunRequest) -> dict[str, Any]:
            return self._service.run_scan(
                keyword=payload.keyword,
                max_items=payload.max_items,
                max_pages=payload.max_pages,
                job_type=payload.job_type,
                fetch_detail=payload.fetch_detail,
            )

        @router.post("/trigger")
        async def trigger(payload: JobGreetTriggerRequest) -> dict[str, Any]:
            return self._service.run_trigger(
                keyword=payload.keyword,
                batch_size=payload.batch_size,
                match_threshold=payload.match_threshold,
                greeting_text=payload.greeting_text,
                job_type=payload.job_type,
                run_id=payload.run_id,
                confirm_execute=payload.confirm_execute,
                fetch_detail=payload.fetch_detail,
            )

        @router.get("/session/check")
        async def session_check() -> dict[str, Any]:
            return self._service.connector.check_login()

    def _service_greet_log_path(self) -> Path:
        # ``_service`` owns the repository; expose its fallback path for health reports.
        return self._service._repository.fallback_log_path  # noqa: SLF001


def get_module() -> JobGreetModule:
    return JobGreetModule()
