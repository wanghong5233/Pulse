from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from .action_report import ActionReport, extract_action_report
from .compaction import CompactionEngine
from .cost import CostController
from .event_types import EventTypes, make_payload
from .hooks import HookPoint, HookRegistry
from .llm.router import LLMRouter
from .logging_config import set_trace_id
from .memory.envelope import MemoryLayer
from .memory_reader import MemoryReaderAdapter
from .prompt_contract import PromptContractBuilder
from .task_context import ExecutionMode, StopReason, TaskContext
from .tool import ToolRegistry
from .verifier import (
    CommitmentVerifier,
    Receipt,
    TurnEvidence,
    VerifierResult,
)


def _action_report_inject_enabled() -> bool:
    """ADR-003: prompt-level ActionReport injection can be turned off.

    When off, handlers may still emit ``__action_report__``; it just
    doesn't get rendered as a SystemMessage for the LLM. The receipt
    path (Verifier grounding) is controlled separately in ``verifier.py``.
    """
    raw = (os.getenv("PULSE_ACTION_REPORT_INJECT") or "").strip().lower()
    return raw not in {"off", "0", "false", "no"}

EventEmitter = Any  # Callable[[str, dict[str, Any]], None] - 避免循环 import


def _sanitize_tool_name(name: str) -> str:
    """OpenAI function names: ^[a-zA-Z0-9_-]+$. Replace dots/spaces."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(name or ""))


def _normalize_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    safe = dict(schema or {})
    if safe.get("type") != "object":
        safe = {"type": "object", "properties": dict(safe.get("properties") or {})}
    if "properties" not in safe:
        safe["properties"] = {}
    return safe


@dataclass(slots=True)
class BrainStep:
    index: int
    thought: str
    action: str
    tool_name: str | None
    tool_args: dict[str, Any]
    observation: Any
    # ADR-003 Step B.1a: if the tool handler returned a structured
    # ActionReport via ``__action_report__``, we snapshot it here (as a
    # JSON-friendly dict) so downstream _build_tool_receipts can forward
    # it into Receipt.action_report, and finalize_result can serialise
    # the step without a live object reference.
    action_report: dict[str, Any] | None = None


@dataclass(slots=True)
class BrainRunResult:
    answer: str
    used_tools: list[str]
    steps: list[BrainStep]
    stopped_reason: StopReason

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "used_tools": list(self.used_tools),
            "steps": [asdict(step) for step in self.steps],
            "stopped_reason": self.stopped_reason.value if isinstance(self.stopped_reason, StopReason) else str(self.stopped_reason),
        }


class Brain:
    """ReAct reasoning loop per architecture spec section 5.2.

    Each turn:
      1. Load memory context (Core + Recall + Archival) into system prompt
      2. Send messages + tool definitions to LLM
      3. If LLM returns tool_calls -> execute -> append observation -> loop
      4. If LLM returns text -> final response
    Terminates on: final text, max_steps (20), consecutive_errors (3), or budget.
    """

    MAX_STEPS = 20
    MAX_CONSECUTIVE_ERRORS = 3

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry,
        llm_router: LLMRouter | None = None,
        cost_controller: CostController | None = None,
        max_steps: int = 20,
        core_memory: Any | None = None,
        recall_memory: Any | None = None,
        archival_memory: Any | None = None,
        workspace_memory: Any | None = None,
        memory_recent_limit: int = 8,
        evolution_engine: Any | None = None,
        correction_detector: Any | None = None,
        prompt_builder: PromptContractBuilder | None = None,
        hooks: HookRegistry | None = None,
        compaction: CompactionEngine | None = None,
        promotion: Any | None = None,
        event_emitter: EventEmitter | None = None,
        commitment_verifier: CommitmentVerifier | None = None,
    ) -> None:
        self._tool_registry = tool_registry
        self._llm_router = llm_router
        self._cost_controller = cost_controller
        self._max_steps = max(1, min(int(max_steps), self.MAX_STEPS))
        self._core_memory = core_memory
        self._recall_memory = recall_memory
        self._archival_memory = archival_memory
        self._workspace_memory = workspace_memory
        self._memory_recent_limit = max(1, min(int(memory_recent_limit), 50))
        self._evolution_engine = evolution_engine
        self._correction_detector = correction_detector
        if prompt_builder is None:
            if any(
                m is not None
                for m in (core_memory, recall_memory, archival_memory, workspace_memory)
            ):
                _auto_reader = MemoryReaderAdapter(
                    core_memory=core_memory,
                    recall_memory=recall_memory,
                    archival_memory=archival_memory,
                    workspace_memory=workspace_memory,
                )
                prompt_builder = PromptContractBuilder(memory=_auto_reader)
            else:
                prompt_builder = PromptContractBuilder()
        self._prompt_builder = prompt_builder
        self._hooks = hooks or HookRegistry()
        self._compaction = compaction or CompactionEngine()
        self._promotion = promotion
        self._promotion_counters: dict[str, int] = {}
        self._event_emitter = event_emitter
        self._commitment_verifier = commitment_verifier

    def bind_event_emitter(self, emitter: EventEmitter | None) -> None:
        self._event_emitter = emitter

    def _emit_event(self, event_type: str, ctx: TaskContext | None = None, **fields: Any) -> None:
        emitter = self._event_emitter
        if emitter is None:
            return
        try:
            payload = make_payload(
                trace_id=getattr(ctx, "trace_id", None) if ctx else None,
                actor="brain",
                session_id=getattr(ctx, "session_id", None) if ctx else None,
                task_id=getattr(ctx, "task_id", None) if ctx else None,
                run_id=getattr(ctx, "run_id", None) if ctx else None,
                workspace_id=getattr(ctx, "workspace_id", None) if ctx else None,
                **fields,
            )
            emitter(event_type, payload)
        except Exception:  # pragma: no cover - 观测侧绝不阻塞主流程
            logger.debug("brain event emit failed", exc_info=True)

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    async def run(
        self,
        *,
        query: str,
        ctx: TaskContext,
        metadata: dict[str, Any] | None = None,
        max_steps: int | None = None,
        prefer_llm: bool = True,
    ) -> BrainRunResult:
        set_trace_id(getattr(ctx, "trace_id", None))
        safe_query = str(query or "").strip()
        if not safe_query:
            result = BrainRunResult(answer="Empty query.", used_tools=[], steps=[], stopped_reason=StopReason.empty_query)
            self._hooks.fire(
                HookPoint.on_task_end,
                ctx,
                {"stopped_reason": result.stopped_reason, "used_tools": [], "step_count": 0},
            )
            return result

        ctx.start_clock()

        safe_metadata = dict(metadata or {})
        safe_metadata["channel"] = ctx.extra.get("channel", safe_metadata.get("channel"))
        safe_metadata["user_id"] = ctx.extra.get("user_id", safe_metadata.get("user_id"))
        ctx.extra.update({
            "channel": safe_metadata.get("channel"),
            "user_id": safe_metadata.get("user_id"),
            "intent": safe_metadata.get("intent"),
            "route_hint": safe_metadata.get("route_hint"),
        })

        budget_steps = max(1, min(int(max_steps or self._max_steps), self.MAX_STEPS))

        logger.info(
            "brain_run_start mode=%s max_steps=%d query_chars=%d session_id=%s task_id=%s",
            getattr(ctx.mode, "value", ctx.mode),
            budget_steps,
            len(safe_query),
            getattr(ctx, "session_id", None),
            getattr(ctx, "task_id", None),
        )

        explicit = self._parse_explicit_tool(safe_query)
        if explicit is not None:
            return await self._run_explicit_tool(explicit, safe_query, ctx)

        route_hint_explicit = self._route_hint_tool_call(safe_query, safe_metadata)

        if self._llm_router is None or not prefer_llm:
            if route_hint_explicit is not None:
                return await self._run_explicit_tool(route_hint_explicit, safe_query, ctx)
            return self._finalize_result(ctx, self._fallback_no_llm(safe_query, safe_metadata))

        if not self._llm_available():
            if route_hint_explicit is not None:
                return await self._run_explicit_tool(route_hint_explicit, safe_query, ctx)
            return self._finalize_result(ctx, self._fallback_no_llm(safe_query, safe_metadata))

        return await self._react_loop(query=safe_query, ctx=ctx, metadata=safe_metadata, budget_steps=budget_steps)

    # ------------------------------------------------------------------
    # Contract B · L5: tool_choice structural decision
    # ------------------------------------------------------------------

    @staticmethod
    def _decide_tool_choice(
        *,
        mode: ExecutionMode,
        step_idx: int,
        prev_ai_was_text_only: bool,
        used_tools_count: int,
    ) -> str:
        """Pure-function decision for Contract B (ADR-001 §3.2 L5).

        Returns ``"auto"`` or ``"required"`` based on **structural signals**
        only — mode, step index, previous-step shape, tool-use history.
        Never inspects message content (no keyword heuristics).

        Rules:

        * Once any tool has actually been invoked → ``"auto"``. The nudge
          is a fail-fast aid, not a cage; lock-in would block the LLM from
          writing final summaries.
        * Previous AI step was pure text AND no tool has fired yet →
          ``"required"`` next step. Guards against "I'll check the weather"
          hallucinations without ever reading the prose.
        * First step of a non-``interactive_turn`` mode (scheduled tasks,
          heartbeats, sub-agents, resumed tasks) → ``"required"``. These
          modes exist to *do work*; "say without doing" on step 0 is never
          acceptable.
        * Interactive first step → ``"auto"``. User might just be chatting.
        """
        if used_tools_count > 0:
            return "auto"
        if prev_ai_was_text_only:
            return "required"
        if step_idx == 0 and mode != ExecutionMode.interactive_turn:
            return "required"
        return "auto"

    # ------------------------------------------------------------------
    # Core ReAct loop
    # ------------------------------------------------------------------

    async def _react_loop(
        self,
        *,
        query: str,
        ctx: TaskContext,
        metadata: dict[str, Any],
        budget_steps: int,
    ) -> BrainRunResult:
        assert self._llm_router is not None

        # Hook: beforeTaskStart — 可阻断
        hook_result = self._hooks.fire(
            HookPoint.before_task_start, ctx,
            {"query": query},
        )
        if hook_result.block:
            return self._finalize_result(
                ctx,
                BrainRunResult(
                    answer=f"Task blocked: {hook_result.reason}",
                    used_tools=[], steps=[], stopped_reason=StopReason.task_blocked,
                ),
            )

        # F8: 轮内预捕获业务域偏好.
        # reflection 跑在 turn 结束后, DomainMemory 对当轮 system prompt 不可见 —
        # 用户本轮新声明的硬约束 (如"只投杭州 / 实习 / 不要大厂") 要到下一轮才反映
        # 在 Job Snapshot 里, 当轮 agent 自然就绕开业务边界.
        # 这里在 build_system_prompt **之前**, 先对 query 做一次 extract + domain
        # dispatch, 把偏好落盘到 JobMemory; build_system_prompt 紧接着读快照就能
        # 看到本轮偏好. extraction 透传给 _remember_interaction → reflect,
        # 避免对同一条 user_text 重复 LLM extract.
        #
        # 关键: pre_capture 走的是 soul_reflection:pre_turn 派发链路,
        # workspace_id / trace_id / session_id / task_id 必须齐全 — 否则
        # JobPreferenceApplier 会退到 default_workspace_id, 事件流也会丢掉
        # trace 关联性 (审计断链).
        pre_capture_metadata = self._build_evolution_metadata(ctx, metadata)
        precaptured = self._pre_capture_domain_prefs(
            query=query, metadata=pre_capture_metadata
        )

        system_prompt = self._build_system_prompt(ctx=ctx, query=query)
        tool_defs, alias_map = self._build_tool_definitions()
        logger.info(
            "brain_react_begin budget_steps=%d system_prompt_chars=%d tools_registered=%d "
            "precaptured_domain=%d",
            budget_steps,
            len(system_prompt),
            len(tool_defs),
            len(precaptured.domain_applied) if precaptured is not None else 0,
        )

        messages: list[Any] = [SystemMessage(content=system_prompt)]

        route_hint = metadata.get("route_hint")
        if isinstance(route_hint, dict):
            target = str(route_hint.get("target") or "").strip()
            intent = str(route_hint.get("intent") or "").strip()
            method = str(route_hint.get("method") or "").strip().lower()
            # 防御层 (配合 server.py F10): 只有规则/LLM 命中时注入 hint;
            # fallback 路径不产生 route_hint, 即便上游未过滤也在此兜底.
            if target and method in ("exact", "prefix", "llm"):
                hint = (
                    f"The intent router detected intent '{intent}' targeting module '{target}'. "
                    f"Consider using tool 'module_{target}' if the request aligns."
                )
                messages.append(SystemMessage(content=hint))

        messages.append(HumanMessage(content=query))

        steps: list[BrainStep] = []
        used_tools: list[str] = []
        consecutive_errors = 0
        stopped_reason = StopReason.max_steps
        # Contract B · L5 escalation state (ADR-001 §3.2).
        # * prev_ai_was_text_only: previous step returned content-only no
        #   tool_calls — structural signal for "pure talk" hallucination.
        # * escalated_once: cap escalation retries at 1 so a LLM that keeps
        #   refusing to call tools even under tool_choice=required doesn't
        #   spin the loop forever. Fail-loud after the nudge is spent.
        prev_ai_was_text_only = False
        escalated_once = False

        for idx in range(budget_steps):
            if ctx.over_budget:
                stopped_reason = StopReason.budget_exhausted
                break
            if not self._reserve_cost(route="brain:react", query=query, tool_args={}, ctx=ctx):
                stopped_reason = StopReason.budget_exhausted
                break

            llm_route = "planning"
            if self._cost_controller is not None:
                llm_route = self._cost_controller.recommend_route("planning")

            tool_choice = self._decide_tool_choice(
                mode=ctx.mode,
                step_idx=idx,
                prev_ai_was_text_only=prev_ai_was_text_only,
                used_tools_count=len(used_tools),
            )

            logger.info(
                "brain_react_step step=%d/%d llm_route=%s messages_in_turn=%d tool_choice=%s",
                idx + 1,
                budget_steps,
                llm_route,
                len(messages),
                tool_choice,
            )

            try:
                ai_msg = await asyncio.to_thread(
                    self._llm_router.invoke_chat,
                    messages,
                    tools=tool_defs or None,
                    route=llm_route,
                    tool_choice=tool_choice,
                )
            except Exception as exc:
                logger.warning(
                    "brain_react_llm_failed step=%d llm_route=%s err=%s",
                    idx + 1,
                    llm_route,
                    str(exc)[:500],
                )
                steps.append(BrainStep(
                    index=idx, thought=f"LLM error: {str(exc)[:200]}", action="error",
                    tool_name=None, tool_args={}, observation=str(exc)[:300],
                ))
                self._hooks.fire(
                    HookPoint.on_recovery,
                    ctx,
                    {"source": "brain", "error": str(exc)[:300], "recovery_level": "abort"},
                )
                stopped_reason = StopReason.llm_error
                break

            if not isinstance(ai_msg, AIMessage) or not ai_msg.tool_calls:
                content = ""
                if isinstance(ai_msg, AIMessage):
                    content = _coerce_text(ai_msg.content)
                else:
                    content = str(ai_msg)

                # Contract B · L5 escalation path (ADR-001 §4.3, Phase 1c):
                # if we haven't touched a tool yet and haven't burned our one
                # escalation retry, give the LLM exactly ONE more step under
                # tool_choice="required" before admitting defeat. Cap at one
                # retry — we're not building a "force forever" cage.
                #
                # Phase 1c (trace_e48a6be0c90e): mode guard removed. Interactive
                # turns also escalate once — the cost is ~1 extra LLM call on
                # pure chitchat ("你好"), the payoff is command-style IM input
                # ("拼多多别投") no longer silently drops the record call.
                if (
                    not used_tools
                    and not escalated_once
                    and idx + 1 < budget_steps
                    and isinstance(ai_msg, AIMessage)
                ):
                    logger.info(
                        "brain_react_escalate step=%d mode=%s reason=text_only_no_tools",
                        idx + 1,
                        ctx.mode.value,
                    )
                    escalated_once = True
                    prev_ai_was_text_only = True
                    messages.append(ai_msg)
                    steps.append(BrainStep(
                        index=idx, thought="text-only no tool_call; escalating", action="escalate",
                        tool_name=None, tool_args={}, observation=content[:500],
                    ))
                    continue

                raw_answer = content.strip() or self._summarize_steps(steps) or "任务已完成。"
                shaped = self._reshape_reply_if_needed(
                    raw_answer=raw_answer, query=query, used_tools=used_tools, ctx=ctx,
                )
                # Contract C v2 (ADR-001 §4.4): commitment-vs-evidence audit.
                # * raw_reply    = LLM pre-shape output — canonical for
                #   commitment detection (shaper often compresses away
                #   "已投递 5 家" → "已完成").
                # * shaped_reply = style-cleaned text; rewrite baseline &
                #   fallback on degraded/verified verdicts.
                # * turn_evidence = Receipt Ledger
                #   (pre_capture + tool receipts with input_keys /
                #   extracted_facts / result_count — see §4.4.2).
                turn_evidence = self._build_turn_evidence(
                    precaptured=precaptured, steps=steps,
                )
                verified_text, verify_verdict = self._verify_commitment(
                    ctx=ctx, query=query,
                    raw_reply=raw_answer, shaped_reply=shaped,
                    turn_evidence=turn_evidence,
                )
                # When the verifier had to rewrite the reply (unfulfilled
                # commitment), bypass soul styling: the rewrite is a literal
                # honest statement + next-step guidance, and prefixing it with
                # an emotive persona destroys the meaning. For verified /
                # degraded verdicts the text is the shaped_reply, which is
                # safe to style as usual.
                if verify_verdict == "unfulfilled":
                    answer = verified_text
                else:
                    answer = self._apply_soul_style(verified_text)
                logger.info(
                    "brain_react_done reason=completed step=%d used_tools=%s "
                    "raw_chars=%d shaped_chars=%d answer_chars=%d",
                    idx + 1,
                    used_tools,
                    len(raw_answer),
                    len(shaped),
                    len(answer),
                )
                steps.append(BrainStep(
                    index=idx, thought="final response", action="respond",
                    tool_name=None, tool_args={}, observation=answer,
                ))
                stopped_reason = StopReason.completed
                self._remember_interaction(
                    query=query, answer=answer, ctx=ctx,
                    used_tools=used_tools, stopped_reason=stopped_reason, steps=steps,
                    precaptured=precaptured,
                )
                self._run_compaction(ctx, steps)
                return self._finalize_result(
                    ctx,
                    BrainRunResult(answer=answer, used_tools=used_tools, steps=steps, stopped_reason=stopped_reason),
                )

            messages.append(ai_msg)

            for tc in ai_msg.tool_calls:
                tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                tc_args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
                tc_raw_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")
                sanitized = str(tc_name or "").strip()
                original = alias_map.get(sanitized, sanitized)
                args = dict(tc_args or {})
                tc_id = str(tc_raw_id or f"call_{uuid.uuid4().hex[:10]}")

                logger.info(
                    "brain_tool_call tool=%s args_keys=%s",
                    original,
                    list(args.keys())[:12],
                )

                if ctx.over_budget or not self._reserve_cost(route=f"tool:{original}", query=query, tool_args=args, ctx=ctx):
                    obs: Any = {"error": "Budget exceeded for this tool call"}
                    messages.append(ToolMessage(content=_serialize(obs), tool_call_id=tc_id))
                    steps.append(BrainStep(
                        index=idx, thought="budget check failed", action="use_tool",
                        tool_name=original, tool_args=args, observation=obs,
                    ))
                    stopped_reason = StopReason.budget_exhausted
                    continue

                # Hook: beforeToolUse — 可阻断
                tool_hook = self._hooks.fire(
                    HookPoint.before_tool_use, ctx,
                    {"tool_name": original, "tool_args": args},
                )
                if tool_hook.block:
                    obs = {"error": f"Tool blocked by hook: {tool_hook.reason}"}
                    messages.append(ToolMessage(content=_serialize(obs), tool_call_id=tc_id))
                    steps.append(BrainStep(
                        index=idx, thought=f"hook blocked {original}", action="use_tool",
                        tool_name=original, tool_args=args, observation=obs,
                    ))
                    stopped_reason = StopReason.tool_blocked
                    continue

                started = time.perf_counter()
                status = "ok"
                try:
                    obs = await self._tool_registry.invoke(original, args)
                    consecutive_errors = 0
                except Exception as exc:
                    obs = {"error": str(exc)[:500], "tool_name": original}
                    consecutive_errors += 1
                    status = "error"
                latency = int((time.perf_counter() - started) * 1000)

                # Hook: afterToolUse — 只观测
                self._hooks.fire(
                    HookPoint.after_tool_use, ctx,
                    {"tool_name": original, "tool_args": args, "observation": obs,
                     "status": status, "latency_ms": latency},
                )

                messages.append(ToolMessage(content=_serialize(obs), tool_call_id=tc_id))

                # ADR-003 Step B.1b: if the handler emitted a structured
                # ActionReport, inject it as a SystemMessage immediately
                # after the ToolMessage so the LLM's next reply is grounded
                # on a machine-verifiable report (not its own recollection
                # of observation). ``PULSE_ACTION_REPORT_INJECT=off`` makes
                # this prompt-side injection degrade to a Receipt-only
                # code path (Verifier still sees the report).
                ar_obj: ActionReport | None = extract_action_report(obs) if status == "ok" else None
                ar_dict: dict[str, Any] | None = None
                if ar_obj is not None:
                    ar_dict = ar_obj.to_dict()
                    if _action_report_inject_enabled():
                        lines = ar_obj.to_prompt_lines()
                        if lines:
                            messages.append(SystemMessage(content="\n".join(lines)))

                used_tools.append(original)
                steps.append(BrainStep(
                    index=idx, thought=f"called {original}", action="use_tool",
                    tool_name=original, tool_args=args, observation=obs,
                    action_report=ar_dict,
                ))

                self._record_tool_call(
                    ctx=ctx, tool_name=original, tool_args=args,
                    observation=obs, status=status, latency_ms=latency,
                )

                # 若 tool mutates=True, 强制刷新 domain snapshot 注入一条 system
                # 消息, 让 LLM 看到最新偏好/黑名单 (避免基于 stale snapshot 继续推理)。
                # 见 docs/Pulse-DomainMemory与Tool模式.md §3.3。
                if status == "ok" and self._is_mutating_tool(original):
                    refreshed = self._render_domain_snapshot_refresh(ctx)
                    if refreshed:
                        logger.info(
                            "brain_domain_snapshot_refreshed after_tool=%s extra_chars=%d",
                            original,
                            len(refreshed),
                        )
                        messages.append(SystemMessage(content=refreshed))

            if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                self._hooks.fire(
                    HookPoint.on_recovery,
                    ctx,
                    {"source": "brain", "recovery_level": "abort", "reason": "consecutive_tool_errors"},
                )
                stopped_reason = StopReason.error_aborted
                break

            # End of a tool-call iteration: previous AI message was NOT text-only
            # (it had tool_calls). Reset the escalation signal so the next loop
            # re-reads truth from the just-observed AI shape.
            prev_ai_was_text_only = False

        logger.info(
            "brain_react_exit reason=%s steps=%d used_tools=%s",
            getattr(stopped_reason, "value", stopped_reason),
            len(steps),
            used_tools,
        )
        fallback = self._apply_soul_style(self._summarize_steps(steps) or "已达到最大推理步数。")
        result = BrainRunResult(answer=fallback, used_tools=used_tools, steps=steps, stopped_reason=stopped_reason)
        self._remember_interaction(
            query=query, answer=fallback, ctx=ctx,
            used_tools=used_tools, stopped_reason=stopped_reason, steps=steps,
            precaptured=precaptured,
        )
        self._run_compaction(ctx, steps)
        return self._finalize_result(ctx, result)

    # ------------------------------------------------------------------
    # System prompt construction
    # ------------------------------------------------------------------

    def _build_system_prompt(self, *, ctx: TaskContext, query: str) -> str:
        """通过 PromptContractBuilder 组装 system prompt。"""
        contract = self._prompt_builder.build(ctx, query)
        prompt = contract.text
        ctx.consume_tokens(contract.token_estimate)
        if len(prompt) > 6000:
            prompt = prompt[:6000] + "\n...(context truncated)"
        return prompt

    # ------------------------------------------------------------------
    # Tool definitions for LLM
    # ------------------------------------------------------------------

    def _build_tool_definitions(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        specs = self._tool_registry.list_tools()
        if not specs:
            return [], {}
        defs: list[dict[str, Any]] = []
        alias_map: dict[str, str] = {}
        for spec in specs:
            sanitized = _sanitize_tool_name(spec.name)
            alias_map[sanitized] = spec.name
            defs.append({
                "type": "function",
                "function": {
                    "name": sanitized,
                    "description": str(spec.description or spec.name)[:512],
                    "parameters": _normalize_schema(spec.schema),
                },
            })
        return defs, alias_map

    def _is_mutating_tool(self, tool_name: str) -> bool:
        """判断 tool 是否会修改 memory (由 ``IntentSpec.mutates`` 标记)。

        ``ModuleRegistry._build_intent_tools`` 把 ``mutates`` 放进
        ``tool.metadata["mutates"]``; 本方法读它判断是否刷新 snapshot。
        未登记或非 intent-level 的 tool 一律视为非 mutating (安全默认)。
        """
        spec = self._tool_registry.get(tool_name)
        if spec is None:
            return False
        return bool((spec.metadata or {}).get("mutates"))

    def _render_domain_snapshot_refresh(self, ctx: TaskContext) -> str:
        """Mutation 后刷新 domain snapshots, 组装成一条 SystemMessage 内容。

        返回形如:
            [Memory updated after tool call]
            ## Job Preferences (...)
            ...
        空结果直接返回空串 (调用方会跳过插入)。
        """
        try:
            sections = self._prompt_builder._render_domain_snapshots(ctx)
        except Exception as exc:
            logger.warning("domain snapshot refresh failed: %s", exc)
            return ""
        if not sections:
            return ""
        body = "\n\n".join(sections)
        return "[Memory updated after tool call — use the following as the new baseline]\n\n" + body

    def _build_evolution_metadata(
        self,
        ctx: TaskContext,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """统一构造进入 evolution_engine 的 metadata.

        pre_capture_domain / reflect_interaction 都依赖下面这些字段才能把
        preference.domain.* 事件正确关联到 trace / workspace / session, 以及
        让 JobPreferenceApplier 把偏好落到正确的 workspace_id 分区. 以前
        pre_capture 只拿 _react_loop 的 safe_metadata (只有 channel/user_id/
        intent/route_hint), trace_id/workspace_id 缺失 — 事件流里 actor=
        soul.reflection:pre_turn 那批 preference.domain.applied 没法按 trace
        回放, 这是观测平面的审计缺口.
        """
        m = dict(metadata or {})
        m.setdefault("session_id", ctx.session_id or "default")
        m.setdefault("channel", ctx.extra.get("channel"))
        m.setdefault("user_id", ctx.extra.get("user_id"))
        m["trace_id"] = ctx.trace_id
        m["task_id"] = ctx.task_id
        m["run_id"] = ctx.run_id
        m["workspace_id"] = ctx.workspace_id
        return m

    def _pre_capture_domain_prefs(
        self,
        *,
        query: str,
        metadata: dict[str, Any],
    ) -> Any | None:
        """轮内预捕获业务域偏好, 让本轮 system prompt 立刻看到 (F8).

        语义: 在 build_system_prompt 之前, 对当前 user query 先做一次
        PreferenceExtractor.extract, 并把识别出来的 domain_prefs (job.hard_
        constraint.set / job.memory.record / ...) 立刻 dispatch 到 DomainMemory.
        紧接着 build_system_prompt 读 JobMemorySnapshot 时就能看到当轮偏好 —
        不再要等到 post-turn reflection.

        **所有错误都被吞掉并降级为 warning**: pre-capture 是增强通路, 失败不应
        阻塞 ReAct 主流程(会退化为 post-turn dispatch, 仅损失"当轮可见"这一个
        特性). 返回值可能是 ``None`` (未绑定/失败/evolution 不支持) 或
        ``PreCaptureResult``; 调用方只需透传给 ``_remember_interaction``.
        """
        if self._evolution_engine is None:
            return None
        fn = getattr(self._evolution_engine, "pre_capture_domain", None)
        if not callable(fn):
            return None
        try:
            return fn(user_text=query, metadata=metadata)
        except Exception as exc:  # pragma: no cover - 防御式降级
            logger.warning(
                "brain_pre_capture_domain_failed err=%s",
                str(exc)[:300],
            )
            return None

    # ------------------------------------------------------------------
    # Explicit /tool command (kept for backward compat & testing)
    # ------------------------------------------------------------------

    def _parse_explicit_tool(self, query: str) -> tuple[str, dict[str, Any]] | None:
        safe = str(query or "").strip()
        if not safe.lower().startswith("/tool "):
            return None
        body = safe[6:].strip()
        if not body:
            return None
        parts = body.split(maxsplit=1)
        tool_name = parts[0].strip()
        if not tool_name or self._tool_registry.get(tool_name) is None:
            return None
        raw_args = parts[1].strip() if len(parts) > 1 else ""
        if raw_args.startswith("{") and raw_args.endswith("}"):
            try:
                parsed = json.loads(raw_args)
                if isinstance(parsed, dict):
                    return tool_name, parsed
            except (json.JSONDecodeError, ValueError):
                logger.debug("Failed to parse tool args as JSON: %s", raw_args[:100])
        if raw_args:
            return tool_name, {"query": raw_args, "text": raw_args}
        return tool_name, {}

    async def _run_explicit_tool(
        self,
        explicit: tuple[str, dict[str, Any]],
        query: str,
        ctx: TaskContext,
    ) -> BrainRunResult:
        tool_name, tool_args = explicit
        logger.info(
            "brain_explicit_tool tool=%s args_keys=%s",
            tool_name,
            list(tool_args.keys())[:12] if isinstance(tool_args, dict) else [],
        )
        started = time.perf_counter()
        try:
            observation = await self._tool_registry.invoke(tool_name, tool_args)
            status = "ok"
        except Exception as exc:
            observation = {"error": str(exc)[:500]}
            status = "error"
        latency = int((time.perf_counter() - started) * 1000)

        self._record_tool_call(
            ctx=ctx, tool_name=tool_name, tool_args=tool_args,
            observation=observation, status=status, latency_ms=latency,
        )
        step = BrainStep(
            index=0, thought="explicit /tool command", action="use_tool",
            tool_name=tool_name, tool_args=tool_args, observation=observation,
        )
        answer = self._apply_soul_style(self._summarize_steps([step]) or "工具已执行。")
        result = BrainRunResult(answer=answer, used_tools=[tool_name], steps=[step], stopped_reason=StopReason.completed)
        self._remember_interaction(
            query=query, answer=answer, ctx=ctx,
            used_tools=[tool_name], stopped_reason=StopReason.completed, steps=[step],
        )
        self._run_compaction(ctx, [step])
        return self._finalize_result(ctx, result)

    def _fallback_no_llm(self, query: str, metadata: dict[str, Any]) -> BrainRunResult:
        msg = (
            "LLM is not configured. Use '/tool <name> <args>' to call tools directly.\n"
            "Available tools: " + ", ".join(s.name for s in self._tool_registry.list_tools()[:20])
        )
        return BrainRunResult(answer=msg, used_tools=[], steps=[], stopped_reason=StopReason.no_llm)

    def _route_hint_tool_call(
        self,
        query: str,
        metadata: dict[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        route_hint = metadata.get("route_hint")
        if not isinstance(route_hint, dict):
            return None
        tool_name = str(route_hint.get("tool_name") or "").strip()
        if not tool_name or self._tool_registry.get(tool_name) is None:
            return None
        intent = str(route_hint.get("intent") or f"module.{tool_name.split('.')[-1]}").strip()
        return (
            tool_name,
            {
                "intent": intent,
                "text": query,
                "metadata": metadata,
            },
        )

    def _llm_available(self) -> bool:
        if self._llm_router is None:
            return False
        resolver = getattr(self._llm_router, "resolve_api_config", None)
        if not callable(resolver):
            return True
        try:
            resolver()
        except (AttributeError, TypeError, RuntimeError) as exc:
            logger.warning("LLM resolver check failed: %s", exc)
            return False
        return True

    def _finalize_result(self, ctx: TaskContext, result: BrainRunResult) -> BrainRunResult:
        self._hooks.fire(
            HookPoint.on_task_end,
            ctx,
            {
                "stopped_reason": result.stopped_reason.value if isinstance(result.stopped_reason, StopReason) else str(result.stopped_reason),
                "used_tools": list(result.used_tools),
                "step_count": len(result.steps),
            },
        )
        return result

    # ------------------------------------------------------------------
    # Cost control
    # ------------------------------------------------------------------

    def _reserve_cost(self, *, route: str, query: str, tool_args: dict[str, Any], ctx: TaskContext | None = None) -> bool:
        text = json.dumps(tool_args, ensure_ascii=False)
        tokens = self._cost_controller.estimate_tokens(query, text) if self._cost_controller is not None else max(1, len(query) // 4 + len(text) // 4)
        if ctx is not None:
            ctx.consume_tokens(tokens)
            if ctx.over_budget:
                self._hooks.fire(
                    HookPoint.on_recovery, ctx,
                    {"source": "budget", "recovery_level": "abort", "reason": "task_token_budget_exhausted"},
                )
                return False
            ratio = ctx.tokens_used / max(1, ctx.token_budget)
            if ratio >= 0.8 and not ctx.extra.get("_budget_warning_fired"):
                ctx.extra["_budget_warning_fired"] = True
                self._hooks.fire(
                    HookPoint.on_recovery, ctx,
                    {"source": "budget", "recovery_level": "degrade",
                     "reason": f"task_budget_80pct (used={ctx.tokens_used}/{ctx.token_budget})"},
                )
        if self._cost_controller is None:
            return True
        return self._cost_controller.reserve(route=route, tokens=tokens)

    # ------------------------------------------------------------------
    # Soul styling
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Reply shaper — 把 ReAct 原始输出改写成"对用户说的人话"
    # ------------------------------------------------------------------

    # 触发 reshape 的可疑模式: 工具名(含点号/下划线), JSON 字段赋值, 函数签名
    _LEAK_PATTERNS = (
        re.compile(r"\b[a-z][a-z0-9]*(?:[._][a-z0-9_]+){1,}\b"),  # e.g. job.greet.scan / jobgreet_scan
        re.compile(r"\b(?:tool_name|function|arguments|parameters|confirm_execute|max_pages|tool_calls)\b", re.IGNORECASE),
        re.compile(r"[\{\[][^\n]{0,20}(?:\"[a-zA-Z_]+\"\s*:|[a-zA-Z_]+\s*=)"),  # dict-like
    )

    def _looks_leaking_internals(self, text: str) -> tuple[bool, str]:
        """粗略判断回复里是否漏了工具名/字段名. 返回 (是否漏, 首个命中片段)."""
        s = str(text or "")
        if len(s) < 10:
            return False, ""
        for pat in self._LEAK_PATTERNS:
            m = pat.search(s)
            if m:
                return True, m.group(0)[:60]
        return False, ""

    def _reshape_reply_if_needed(
        self,
        *,
        raw_answer: str,
        query: str,
        used_tools: list[str],
        ctx: TaskContext,
    ) -> str:
        """如果原始回复漏了内部实现细节, 走一次 cheap LLM 改写成人话; 否则原样返回.

        契约:
          - **默认不绕**: 检测到干净的自然语言就直接返回 raw_answer. 少跑一次 LLM.
          - 检测到泄漏: 走 ``route="generation"``(代码层 default = gpt-4o-mini)改写.
          - 改写失败: **不静默兜底**, emit ``brain.reply.shape.degraded`` 事件并
            返回 raw_answer(让用户至少能看到点东西, 但事件流会记录降级原因).
        """
        leaked, sample = self._looks_leaking_internals(raw_answer)
        if not leaked:
            self._emit_event(
                EventTypes.BRAIN_REPLY_SHAPED,
                ctx=ctx,
                mode="skip_clean",
                raw_chars=len(raw_answer),
                shaped_chars=len(raw_answer),
            )
            return raw_answer

        if self._llm_router is None:
            logger.warning(
                "reply_shaper skipped: no llm_router; raw leaks pattern=%s",
                sample,
            )
            self._emit_event(
                EventTypes.BRAIN_REPLY_SHAPE_DEGRADED,
                ctx=ctx,
                reason="no_llm_router",
                leak_sample=sample,
                raw_preview=raw_answer[:300],
            )
            return raw_answer

        shape_prompt = [
            SystemMessage(content=(
                "你是一个回复润色器, 职责是把 agent 的原始输出改写成发给真实用户的一句话 IM 消息.\n"
                "**严格要求**:\n"
                "1. 去掉所有工具名/函数签名/JSON 字段 (如 `job.greet.scan`, `confirm_execute=false`, `max_pages` 等).\n"
                "2. 以第一人称(我/我帮你)写, 3-6 句话, 中文为主, 简洁.\n"
                "3. 只陈述做了什么、发现了什么、或接下来建议什么. **不要**提\"agent\"、\"工具\"、\"调用\"等元词.\n"
                "4. 如果原文说的是\"暂时做不到/失败\", 你也要如实转达, 不要编造进展.\n"
                "5. 输出**只**包含最终消息正文, 不要加前言/后记/引号.\n"
            )),
            HumanMessage(content=(
                f"[用户的原始请求]\n{query.strip()[:400]}\n\n"
                f"[Agent 这一轮实际调用的工具]\n{', '.join(used_tools) if used_tools else '(无)'}\n\n"
                f"[Agent 的原始输出, 需要你改写]\n{raw_answer.strip()[:1200]}\n\n"
                "请输出改写后的用户消息:"
            )),
        ]

        started = time.perf_counter()
        try:
            shaped_text = self._llm_router.invoke_text(shape_prompt, route="generation")
        except Exception as exc:
            logger.warning("reply_shaper LLM failed: %s", str(exc)[:300])
            self._emit_event(
                EventTypes.BRAIN_REPLY_SHAPE_DEGRADED,
                ctx=ctx,
                reason="llm_error",
                error=str(exc)[:300],
                leak_sample=sample,
                raw_preview=raw_answer[:300],
            )
            return raw_answer

        shaped = (shaped_text or "").strip()
        if not shaped:
            self._emit_event(
                EventTypes.BRAIN_REPLY_SHAPE_DEGRADED,
                ctx=ctx,
                reason="empty_output",
                leak_sample=sample,
                raw_preview=raw_answer[:300],
            )
            return raw_answer

        # Re-check: 改写完如果还漏, 再发一次 degrade 事件但仍用 shaped (通常比 raw 好)
        still_leaked, still_sample = self._looks_leaking_internals(shaped)
        self._emit_event(
            EventTypes.BRAIN_REPLY_SHAPED,
            ctx=ctx,
            mode="rewrote",
            raw_chars=len(raw_answer),
            shaped_chars=len(shaped),
            raw_preview=raw_answer[:500],
            shaped_preview=shaped[:500],
            leak_before=sample,
            leak_after=still_sample if still_leaked else "",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return shaped

    # ------------------------------------------------------------------
    # Contract C · CommitmentVerifier integration (ADR-001 §4.4.3)
    # ------------------------------------------------------------------

    def _verify_commitment(
        self,
        *,
        ctx: TaskContext,
        query: str,
        raw_reply: str,
        shaped_reply: str,
        turn_evidence: TurnEvidence,
    ) -> tuple[str, str]:
        """Contract C v2 · audit commitments in reply vs TurnEvidence ledger.

        Returns ``(text, verdict)`` where:
          * ``verdict == "unfulfilled"`` → ``text`` is the judge's honest
            rewrite; caller MUST emit it verbatim (no soul_style prefix).
          * ``verdict == "verified"`` / ``"degraded"`` → ``text`` is the
            original ``shaped_reply``; caller styles as usual.
          * ``verdict == "skipped"`` → verifier not wired or heartbeat turn;
            ``text`` is the original ``shaped_reply``.

        Always emits exactly one ``brain.commitment.*`` event per completed
        turn when the verifier runs (payload spec: ADR §4.4.5).
        """
        verifier = self._commitment_verifier
        if verifier is None or ctx.mode == ExecutionMode.heartbeat_turn:
            return shaped_reply, "skipped"

        pre_count = len(turn_evidence.pre_capture_receipts)
        tool_count = len(turn_evidence.tool_receipts)
        started = time.perf_counter()
        try:
            result: VerifierResult = verifier.verify(
                ctx=ctx,
                query=query,
                raw_reply=raw_reply,
                shaped_reply=shaped_reply,
                turn_evidence=turn_evidence,
            )
        except Exception as exc:  # never block the user on verifier errors
            logger.warning("commitment_verifier raised: %s", str(exc)[:300])
            self._emit_event(
                EventTypes.BRAIN_COMMITMENT_DEGRADED,
                ctx=ctx,
                error_class=type(exc).__name__,
                error_message=str(exc)[:300],
                pre_capture_count=pre_count,
                tool_receipt_count=tool_count,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            return shaped_reply, "degraded"

        latency_ms = int((time.perf_counter() - started) * 1000)

        if result.verdict == "unfulfilled":
            self._emit_event(
                EventTypes.BRAIN_COMMITMENT_UNFULFILLED,
                ctx=ctx,
                has_commitment=True,
                hallucination_type=result.hallucination_type,
                pre_capture_count=pre_count,
                tool_receipt_count=tool_count,
                commitment_excerpt=result.commitment_excerpt[:300],
                reason=result.reason[:500],
                rewritten_reply_preview=result.reply[:120],
                raw_reply_preview=raw_reply[:200],
                shaped_reply_preview=shaped_reply[:200],
                latency_ms=latency_ms,
            )
            return result.reply, "unfulfilled"

        if result.verdict == "degraded":
            self._emit_event(
                EventTypes.BRAIN_COMMITMENT_DEGRADED,
                ctx=ctx,
                error_message=result.reason[:300],
                hallucination_type=result.hallucination_type,
                pre_capture_count=pre_count,
                tool_receipt_count=tool_count,
                latency_ms=latency_ms,
            )
            return shaped_reply, "degraded"

        # verdict == "verified"
        self._emit_event(
            EventTypes.BRAIN_COMMITMENT_VERIFIED,
            ctx=ctx,
            has_commitment=bool(result.commitment_excerpt),
            hallucination_type=result.hallucination_type,
            pre_capture_count=pre_count,
            tool_receipt_count=tool_count,
            commitment_excerpt=result.commitment_excerpt[:300] or None,
            latency_ms=latency_ms,
        )
        return shaped_reply, "verified"

    # ------------------------------------------------------------------
    # Contract C v2 · TurnEvidence (Receipt Ledger) assembly (ADR §4.4.2)
    # ------------------------------------------------------------------

    def _build_turn_evidence(
        self,
        *,
        precaptured: Any | None,
        steps: list[BrainStep],
    ) -> TurnEvidence:
        """Assemble Receipt Ledger from pre_capture + tool steps.

        Contract:
          * pre_capture side-effects (preference.domain.applied) → Receipt
            kind="event", ``extracted_facts`` pulled from
            ``DispatchResult.effect`` (the applier already normalised the
            business fields); ``name`` is the event identity.
          * tool ReAct steps → Receipt kind="tool"; structured facts come
            from ``ToolSpec.extract_facts`` if declared, else
            ``_default_extract_facts`` fallback (top-level scalar whitelist).
          * Ordering preserved within each bucket; pre_capture receipts
            are always produced BEFORE the ReAct loop so the judge can
            see them as "already-done" evidence.
        """
        pre_receipts: list[Receipt] = self._build_pre_capture_receipts(precaptured)
        tool_receipts = self._build_tool_receipts(steps)
        return TurnEvidence(
            pre_capture_receipts=tuple(pre_receipts),
            tool_receipts=tuple(tool_receipts),
        )

    @staticmethod
    def _build_pre_capture_receipts(precaptured: Any | None) -> list[Receipt]:
        if precaptured is None:
            return []
        raw_list = getattr(precaptured, "domain_applied", None) or []
        receipts: list[Receipt] = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or "").strip() or "unknown"
            op = str(item.get("op") or "").strip() or "unknown"
            status_raw = str(item.get("status") or "").strip()
            # Dispatcher statuses: applied / skipped / rejected / error.
            # For ledger purposes, only "applied" counts as ok; anything
            # else is surfaced as error so the judge doesn't treat a
            # skip / reject as fulfilment evidence.
            status: str = "ok" if status_raw == "applied" else "error"
            effect = item.get("effect") or {}
            facts: dict[str, Any] = {}
            if isinstance(effect, dict):
                for k, v in effect.items():
                    if isinstance(v, (str, int, float, bool)) and v is not None:
                        facts[str(k)] = v
            facts.setdefault("dispatch_status", status_raw or "unknown")
            receipts.append(Receipt(
                kind="event",
                name=f"preference.domain.applied:{domain}.{op}",
                status=status,  # type: ignore[arg-type]
                extracted_facts=facts,
            ))
        return receipts

    def _build_tool_receipts(self, steps: list[BrainStep]) -> list[Receipt]:
        receipts: list[Receipt] = []
        for step in steps:
            if step.action != "use_tool" or not step.tool_name:
                continue
            spec = self._tool_registry.get(step.tool_name)
            observation = step.observation
            # Status convention: observation dict with "error" key → error,
            # else ok. Consistent with how tool_hook / _record_tool_call
            # classifies outcomes.
            status: str = "error" if (
                isinstance(observation, dict) and observation.get("error")
            ) else "ok"

            input_keys = tuple(
                str(k) for k in (step.tool_args or {}).keys()
            )[:12]

            facts = self._extract_tool_facts(spec, observation)
            result_count = _infer_result_count(observation)

            # ADR-003 Step B.1a: merge ActionReport-projected facts into
            # extracted_facts for Verifier grounding. ``extract_facts``
            # (handcrafted per-tool whitelist) wins on name clashes — it
            # is the authoritative business projection; action_report
            # only fills in the gaps (action / status / scalar metrics).
            action_report_dict = step.action_report
            if action_report_dict:
                projected = self._project_action_report_facts(action_report_dict)
                facts = {**projected, **facts}

            receipts.append(Receipt(
                kind="tool",
                name=step.tool_name,
                status=status,  # type: ignore[arg-type]
                input_keys=input_keys,
                result_count=result_count,
                extracted_facts=facts,
                action_report=action_report_dict,
            ))
        return receipts

    @staticmethod
    def _project_action_report_facts(action_report_dict: dict[str, Any]) -> dict[str, Any]:
        """ADR-003: project a serialised ActionReport into scalar facts.

        Only ``action`` (str), ``status`` (str) and numeric metric values
        make it into ``extracted_facts`` — non-scalar fields (details,
        next_steps, evidence) stay on ``Receipt.action_report`` and are
        rendered there by the judge prompt.
        """
        out: dict[str, Any] = {}
        action = action_report_dict.get("action")
        if isinstance(action, str) and action:
            out["action"] = action
        status = action_report_dict.get("status")
        if isinstance(status, str) and status:
            out["status"] = status
        metrics = action_report_dict.get("metrics")
        if isinstance(metrics, dict):
            for k, v in metrics.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[str(k)] = v
        return out

    @staticmethod
    def _extract_tool_facts(spec: Any, observation: Any) -> dict[str, Any]:
        from .tool import _default_extract_facts

        extractor = getattr(spec, "extract_facts", None) if spec is not None else None
        if callable(extractor):
            try:
                produced = extractor(observation)
            except Exception as exc:
                logger.warning(
                    "tool_extract_facts raised for %s: %s",
                    getattr(spec, "name", "?"),
                    str(exc)[:200],
                )
                produced = None
            if isinstance(produced, dict):
                # Enforce scalar-only shape (contract: ToolSpec §4.5).
                return {
                    str(k): v
                    for k, v in produced.items()
                    if isinstance(v, (str, int, float, bool)) and v is not None
                }
        return _default_extract_facts(observation)

    def _apply_soul_style(self, answer: str) -> str:
        text = str(answer or "").strip()
        if not text:
            return text
        if self._core_memory is not None:
            try:
                soul = self._core_memory.read_block("soul")
            except (KeyError, TypeError, RuntimeError) as exc:
                logger.debug("Failed to read soul block: %s", exc)
                soul = None
            if isinstance(soul, dict):
                prefix = str(soul.get("assistant_prefix") or "").strip()
                if prefix and not text.startswith(prefix):
                    text = f"{prefix}: {text}"
        if len(text) > 2000:
            return text[:2000] + "...(truncated)"
        return text

    # ------------------------------------------------------------------
    # Memory write-back
    # ------------------------------------------------------------------

    def _remember_interaction(
        self,
        *,
        query: str,
        answer: str,
        ctx: TaskContext,
        used_tools: list[str],
        stopped_reason: StopReason,
        steps: list[BrainStep],
        precaptured: Any | None = None,
    ) -> None:
        if self._recall_memory is None:
            return
        session_id = ctx.session_id or "default"
        # workspace_id 是 domain preference dispatcher 的关键路由键;
        # 没传的话 dispatcher 会落到 applier 的 default_workspace_id.
        record_metadata = self._build_evolution_metadata(ctx, None)
        record_metadata["used_tools"] = list(used_tools)
        record_metadata["stopped_reason"] = (
            stopped_reason.value
            if isinstance(stopped_reason, StopReason)
            else str(stopped_reason)
        )

        prev_assistant = ""
        if self._correction_detector is not None:
            recent_before = self._recall_memory.recent(limit=3, session_id=session_id)
            for entry in reversed(recent_before):
                if str(entry.get("role") or "") == "assistant":
                    prev_assistant = str(entry.get("text") or "")
                    break

        self._recall_memory.add_interaction(
            user_text=query,
            assistant_text=answer,
            metadata=record_metadata,
            session_id=session_id,
            task_id=ctx.task_id or None,
            run_id=ctx.run_id or None,
            workspace_id=ctx.workspace_id,
        )

        if self._evolution_engine is not None:
            # F8: 把轮首的 pre_capture 结果透传给 reflection. reflection 会
            # 1) 复用 extraction 避免重复 LLM extract, 2) 跳过 domain dispatch
            # 避免对 JobMemory 的 memory.record 写两次(uuid 不同 → 重复条目).
            evolution_result = self._evolution_engine.reflect_interaction(
                user_text=query,
                assistant_text=answer,
                metadata=record_metadata,
                precaptured=precaptured,
            )
            record_metadata["evolution"] = evolution_result.to_dict()

        if self._correction_detector is not None and prev_assistant:
            self._correction_detector.check(
                user_text=query,
                previous_assistant_text=prev_assistant,
                metadata=record_metadata,
            )

    def _run_compaction(self, ctx: TaskContext, steps: list[BrainStep]) -> None:
        """每轮结束后执行 turn → taskRun 压缩，并根据 envelope.layer 路由写入。"""
        if not steps:
            return
        raw = [asdict(s) for s in steps]

        self._hooks.fire(HookPoint.before_compact, ctx, {"step_count": len(steps)})

        output = self._compaction.compact_turn(ctx, raw)
        envelope = self._compaction.to_envelope(ctx, output)

        self._route_envelope(envelope, ctx=ctx)

        self._hooks.fire(
            HookPoint.after_compact, ctx,
            {"summary": output.summary, "token_estimate": output.token_estimate},
        )

        self._run_promotion(ctx)

    def _route_envelope(self, envelope: Any, *, ctx: TaskContext | None = None) -> None:
        """根据 ``envelope.layer`` 把记忆写入对应存储层, 成功后发布 ``memory.write`` 事件.

        **存储层 × 事件流分离**(Event Sourcing 简化版):

          - 记忆写入只负责"给 LLM 下次读":
            operational → 纯内存; recall → RecallMemory; archival → ArchivalMemory;
            workspace → WorkspaceMemory; core → CoreMemory.
          - 审计/合规走独立的 EventLog(见 ``event_types.py``):
            每次 envelope 成功写入后发一条 ``memory.write`` 事件到 EventBus,
            由 ``JsonlEventSink`` 持久化.

        ``MemoryLayer.meta`` 已废弃(见 ``docs/Pulse-内核架构总览.md`` §6);
        历史代码若仍产生 meta envelope, 会被转成一条纯事件(不进记忆存储).
        """
        layer = getattr(envelope, "layer", None)
        memory_id = getattr(envelope, "memory_id", None)
        written = False

        if layer == MemoryLayer.operational:
            logger.debug("Operational envelope %s — ephemeral, not persisted", memory_id or "?")
            written = True
        elif layer == MemoryLayer.recall:
            if self._recall_memory is not None:
                self._recall_memory.store_envelope(envelope)
                written = True
        elif layer == MemoryLayer.archival:
            if self._archival_memory is not None:
                self._archival_memory.store_envelope(envelope)
                written = True
        elif layer == MemoryLayer.workspace:
            if self._workspace_memory is not None:
                ws_id = getattr(envelope, "workspace_id", None) or "default"
                content = getattr(envelope, "content", {})
                summary = content.get("summary", "") if isinstance(content, dict) else str(content)
                self._workspace_memory.set_summary(ws_id, summary)
                written = True
        elif layer == MemoryLayer.core:
            if self._core_memory is not None:
                content = getattr(envelope, "content", {})
                # CoreMemory 是跨域全局记忆, 必须严格限制写入边界:
                #   - 只允许 soul/user/prefs/context 四个 block
                #   - 业务域偏好应走 DomainMemory (workspace facts), 不在此透传
                block_name = "context"
                block_content: Any = {}
                if isinstance(content, dict):
                    # 兼容旧 envelope: {"predicate": "...", "object": ...}
                    hinted = str(content.get("block") or content.get("predicate") or "context").strip().lower()
                    value = content.get("object", content)
                    if hinted in ("soul", "user", "prefs", "context"):
                        block_name = hinted
                        block_content = value if isinstance(value, dict) else {"value": value}
                    elif hinted.startswith("prefs.") and len(hinted) > len("prefs."):
                        # 允许细粒度 prefs.<key> envelope 映射到 prefs block.
                        block_name = "prefs"
                        block_content = {hinted[len("prefs."):]: value}
                    else:
                        logger.warning(
                            "core envelope ignored: unsupported block/predicate=%s; "
                            "domain preference should persist via DomainMemory tools",
                            hinted,
                        )
                        block_content = {}
                else:
                    block_name = "context"
                    block_content = {"value": str(content)}
                if not block_content:
                    # 非法/越界 envelope 直接跳过, 避免污染 CoreMemory.
                    written = False
                    # 不发 memory.write 事件(因为实际没有写入), 继续走函数收尾。
                    pass
                else:
                    try:
                        self._core_memory.update_block(
                            block=block_name, content=block_content, merge=True,
                        )
                        written = True
                    except (AttributeError, TypeError, ValueError) as exc:
                        logger.warning(
                            "Core memory write failed layer=core block=%s err=%s",
                            block_name,
                            exc,
                        )
        elif layer == MemoryLayer.meta:
            logger.warning(
                "MemoryLayer.meta is deprecated; envelope %s rerouted to EventLog only. "
                "Use event_types.EventTypes for audit trails.",
                memory_id or "?",
            )
            # 不写入记忆存储, 只在事件流里留痕(见下方 _emit_event)
        else:
            if self._recall_memory is not None:
                self._recall_memory.store_envelope(envelope)
                written = True
            logger.debug("Unknown layer %s, fell back to recall", layer)

        if written or layer == MemoryLayer.meta:
            self._emit_event(
                EventTypes.MEMORY_WRITE,
                ctx=ctx,
                memory_id=memory_id,
                layer=str(layer.value if hasattr(layer, "value") else layer),
                kind=str(getattr(envelope, "kind", "") or ""),
                scope=str(getattr(envelope, "scope", "") or ""),
                deprecated_meta=(layer == MemoryLayer.meta),
            )

    def _run_promotion(self, ctx: TaskContext) -> None:
        """从 recall 中提取候选事实并晋升到 archival。

        节流: 同一 session 内每 5 轮才触发一次。
        """
        if self._promotion is None or self._recall_memory is None:
            return

        key = ctx.session_id or "default"
        count = self._promotion_counters.get(key, 0) + 1
        self._promotion_counters[key] = count
        if count % 5 != 0:
            return

        recent = self._recall_memory.recent(
            limit=20, session_id=key,
        )
        if recent:
            self._promotion.promote(ctx, recent)

    def _record_tool_call(
        self,
        *,
        ctx: TaskContext,
        tool_name: str,
        tool_args: dict[str, Any],
        observation: Any,
        status: str,
        latency_ms: int,
    ) -> None:
        if self._recall_memory is None:
            return
        self._recall_memory.record_tool_call(
            session_id=ctx.session_id or "default",
            task_id=ctx.task_id or None,
            run_id=ctx.run_id or None,
            workspace_id=ctx.workspace_id,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=observation,
            status=status,
            latency_ms=latency_ms,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _summarize_steps(self, steps: list[BrainStep]) -> str:
        tool_steps = [s for s in steps if s.action == "use_tool" and s.tool_name]
        if not tool_steps:
            return ""
        lines = ["已完成工具链执行："]
        for s in tool_steps:
            lines.append(f"- {s.tool_name}: {_short_observation(s.observation)}")
        text = "\n".join(lines)
        return text[:480] + "...(truncated)" if len(text) > 480 else text

    @staticmethod
    def _render_observation(*, tool_name: str, observation: Any) -> str:
        body = _serialize(observation).strip()
        if len(body) > 2000:
            body = body[:2000] + "...(truncated)"
        return f"[{tool_name}] {body}"


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _coerce_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "\n".join(parts).strip()
    return str(content)


def _serialize(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


_COMMON_COUNT_KEYS: tuple[str, ...] = (
    "triggered_count",
    "applied_count",
    "recorded_count",
    "scan_count",
    "result_count",
    "total",
    "count",
)


def _infer_result_count(observation: Any) -> int | None:
    """Best-effort count-extraction for Receipt.result_count (§4.4.2).

    Priority: explicit count-ish scalar keys > top-level ``results``/
    ``items``/``jobs`` list length. Returns ``None`` when the
    observation shape is unknown — the judge prompt then simply omits
    ``result_count`` for that receipt.

    We deliberately do NOT recurse or walk the whole tree; this is a
    receipt hint for the judge, not a summariser.
    """
    if isinstance(observation, dict):
        for key in _COMMON_COUNT_KEYS:
            value = observation.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and value >= 0:
                return int(value)
        for key in ("results", "items", "jobs", "records", "greetings"):
            value = observation.get(key)
            if isinstance(value, list):
                return len(value)
    if isinstance(observation, list):
        return len(observation)
    return None


def _short_observation(observation: Any) -> str:
    if isinstance(observation, dict):
        if "error" in observation:
            return f"error={str(observation.get('error') or '')[:120]}"
        try:
            text = json.dumps(observation, ensure_ascii=False)
        except Exception:
            text = str(observation)
        return text[:200] + ("..." if len(text) > 200 else "")
    if isinstance(observation, list):
        return f"list(len={len(observation)})"
    text = str(observation or "").strip()
    return text[:200] + ("..." if len(text) > 200 else "")
