"""system.patrol — conversational control plane for AgentRuntime patrols.

ADR-004 §6.1 decision B: exposes 5 ``IntentSpec`` entries
(``system.patrol.list / status / enable / disable / trigger``) so that Brain
can translate user utterances like "开启自动回复" / "后台在跑什么" /
"现在就跑一次 job_chat" into in-process ``AgentRuntime`` calls.

Why an ``IntentSpec`` module and not an MCP tool: Brain, ``BaseModule`` and
``AgentRuntime`` all live in the same FastAPI process; MCP tools pay
cross-process serialization cost and would lose the in-memory runtime
reference this module needs. Kernel-internal control plane → in-process
tool; cross-process side effects → MCP.

Semantics mirror the ``/api/runtime/patrols/*`` HTTP routes (see
``core/server.py``). Same underlying ``AgentRuntime`` API, two audiences:
IntentSpec for LLM tool_use, HTTP for CLI / 前端 / ops scripts.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ....core.module import BaseModule, IntentSpec


class PatrolControlModule(BaseModule):
    name = "system_patrol"
    description = "Conversational control plane for AgentRuntime patrols (ADR-004 §6.1)"
    route_prefix = "/api/modules/system/patrol"
    tags = ["system", "patrol"]

    def __init__(self) -> None:
        super().__init__()
        self.intents = self._build_intents()

    def register_routes(self, router: APIRouter) -> None:
        # Control-plane HTTP already lives under /api/runtime/patrols/* in
        # core/server.py; keeping it there avoids duplicating the runtime
        # reference. This module exposes the same surface via IntentSpec
        # only, so its module-scoped router stays intentionally empty.
        return None

    def _resolve_runtime(self) -> Any:
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError(
                "system.patrol.* requires AgentRuntime binding; module was not "
                "attached (server startup should call bind_runtime before on_startup)."
            )
        return runtime

    def _list_handler(self) -> dict[str, Any]:
        runtime = self._resolve_runtime()
        patrols = runtime.list_patrols()
        return {"ok": True, "patrols": patrols, "total": len(patrols)}

    def _status_handler(self, *, name: str) -> dict[str, Any]:
        runtime = self._resolve_runtime()
        snapshot = runtime.get_patrol_stats(name)
        if snapshot is None:
            return {"ok": False, "name": name, "error": f"patrol not found: {name}"}
        return {"ok": True, "name": name, "patrol": snapshot}

    def _enable_handler(
        self,
        *,
        name: str,
        trigger_now: bool = True,
    ) -> dict[str, Any]:
        """Flip the patrol ON and (by default) run one tick immediately.

        ``trigger_now`` defaults to ``True`` because "开启自动回复" as a
        user utterance implies "start it AND let me see it work" — if we
        only flipped ``enabled`` the user would wait up to one full
        ``peak_interval_seconds`` before seeing any effect, which reads
        as "nothing happened". Scheduler semantics stay intact: this is
        just a one-shot trigger on top of the normal tick schedule.

        enable + trigger are two separate kernel calls; between them
        another scheduler tick could in theory grab its own first run,
        producing at most one duplicate execution. That is accepted —
        patrol handlers are idempotent at the business layer (ADR-004
        §6.1.7 invariant #4), and dedupe in ``JobChatService.run_process``
        guards against re-replying to the same conversation.
        """

        runtime = self._resolve_runtime()
        ok = runtime.enable_patrol(name)
        if not ok:
            return {
                "ok": False,
                "name": name,
                "error": f"patrol not found or not controllable: {name}",
            }
        result: dict[str, Any] = {"ok": True, "name": name, "enabled": True}
        if trigger_now:
            first_run = runtime.run_patrol_once(name)
            result["first_run"] = first_run
        return result

    def _disable_handler(self, *, name: str) -> dict[str, Any]:
        runtime = self._resolve_runtime()
        ok = runtime.disable_patrol(name)
        if not ok:
            return {
                "ok": False,
                "name": name,
                "error": f"patrol not found or not controllable: {name}",
            }
        return {"ok": True, "name": name, "enabled": False}

    def _trigger_handler(self, *, name: str) -> dict[str, Any]:
        runtime = self._resolve_runtime()
        return runtime.run_patrol_once(name)

    def _build_intents(self) -> list[IntentSpec]:
        name_schema = {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Patrol name (e.g. 'job_chat.patrol'). "
                        "Retrieve the full list with system.patrol.list."
                    ),
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        }
        return [
            IntentSpec(
                name="system.patrol.list",
                description=(
                    "List every module-registered patrol with enabled flag, "
                    "peak/offpeak interval and recent execution stats. "
                    "No side effects."
                ),
                when_to_use=(
                    "用户想全局掌握后台 patrol 状态, 没有指定某一条. 典型触发: "
                    "'看看后台在跑什么', '现在开着哪些自动任务', 'patrol 状态', "
                    "'自动回复 / 自动投递都开着吗'."
                ),
                when_not_to_use=(
                    "用户只关心某一条具体 patrol — 用 system.patrol.status(name); "
                    "用户要改状态 — 用 enable/disable/trigger; "
                    "用户问某条 BOSS 会话的回复内容 — 那是业务层 job.chat.* 的事, "
                    "本工具只暴露 patrol 调度元数据, 不返回业务载荷."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                handler=self._list_handler,
                mutates=False,
                risk_level=0,
                tags=["system", "patrol", "readonly"],
                examples=[
                    {"input": "看看后台在跑什么", "kwargs": {}},
                    {"input": "列出所有自动任务", "kwargs": {}},
                ],
            ),
            IntentSpec(
                name="system.patrol.status",
                description=(
                    "Return a single patrol's latest snapshot: enabled flag, "
                    "peak/offpeak interval, last_run_at, last_outcome, last_error, "
                    "circuit_open. No side effects."
                ),
                when_to_use=(
                    "用户问某个具体 patrol 最近跑得怎样. 典型触发: "
                    "'自动回复最近跑得怎样', 'job_chat.patrol 现在什么状态', "
                    "'投递 patrol 上次是不是失败了'."
                ),
                when_not_to_use=(
                    "用户没给 patrol 名或想要全局概览 — 用 system.patrol.list; "
                    "用户想改变状态 — 用 enable/disable/trigger, 不是 status."
                ),
                parameters_schema=name_schema,
                handler=self._status_handler,
                mutates=False,
                risk_level=0,
                tags=["system", "patrol", "readonly"],
                examples=[
                    {
                        "input": "job_chat.patrol 现在什么状态",
                        "kwargs": {"name": "job_chat.patrol"},
                    },
                ],
            ),
            IntentSpec(
                name="system.patrol.enable",
                description=(
                    "Turn ON a patrol and (by default) execute one tick "
                    "immediately so the user sees an effect without waiting "
                    "a full interval. Mutates in-memory ScheduleTask.enabled; "
                    "no persistence (restart falls back to module initial state). "
                    "Does NOT bypass business-layer killswitch env vars — if "
                    "the handler itself is disabled it still returns disabled."
                ),
                when_to_use=(
                    "用户表达\"开启 / 启动 / 打开 / 启用 / 托管 / 让它开始 / 帮我监听\" "
                    "这类意图, 针对某个 patrol (自动回复 / 自动投递 / 自动签到 等). "
                    "典型触发: '开启自动回复', '帮我开启 boss 的自动消息回复', "
                    "'把 job_chat.patrol 打开', '启动自动投递 patrol', "
                    "'start auto reply', 'turn on the chat patrol'. "
                    "这是\"用户要让某个后台循环工作跑起来\"的唯一正确 intent — "
                    "即便用户同一句话里还描述了\"顺便处理一下当前未读消息\", "
                    "也应优先选本工具 (默认 trigger_now=true 会立刻处理一次), "
                    "不要改走 job.chat.process 之类的同步即时 intent."
                ),
                when_not_to_use=(
                    "用户只想了解状态 — 用 list/status; "
                    "用户明确说\"不要长期开着, 只跑这一次\" — 用 trigger "
                    "(trigger 不改 enabled 标志); "
                    "用户要关闭 — 用 disable. "
                    "patrol 对应业务有独立 killswitch (如 PULSE_BOSS_AUTOREPLY=off) "
                    "时, enable 只把 patrol 放回调度队列, 不改 killswitch 语义."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "Patrol name (e.g. 'job_chat.patrol'). "
                                "Retrieve the full list with system.patrol.list."
                            ),
                        },
                        "trigger_now": {
                            "type": "boolean",
                            "description": (
                                "If true (default), run one tick immediately "
                                "after flipping enabled=true so the user sees "
                                "an effect without waiting a full interval. "
                                "Set to false only when the user explicitly "
                                "says 'just arm it, do not run yet'."
                            ),
                            "default": True,
                        },
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
                handler=self._enable_handler,
                mutates=True,
                requires_confirmation=True,
                risk_level=2,
                tags=["system", "patrol", "lifecycle"],
                examples=[
                    {
                        "input": "开启自动回复",
                        "kwargs": {"name": "job_chat.patrol"},
                    },
                    {
                        "input": "帮我开启 boss 的自动消息回复, 处理一下未读",
                        "kwargs": {"name": "job_chat.patrol", "trigger_now": True},
                    },
                    {
                        "input": "把自动回复挂起来, 先别跑, 我晚点再触发",
                        "kwargs": {"name": "job_chat.patrol", "trigger_now": False},
                    },
                ],
            ),
            IntentSpec(
                name="system.patrol.disable",
                description=(
                    "Turn OFF a patrol so that future scheduler ticks skip it. "
                    "Protective action — prevents future side effects, does not "
                    "undo any already-emitted action (already-sent messages / "
                    "already-submitted resumes stay sent). No HITL confirmation."
                ),
                when_to_use=(
                    "用户要关闭某个 patrol. 典型触发: '关闭自动回复', "
                    "'停掉 job_chat.patrol', '自动投递先别跑了'."
                ),
                when_not_to_use=(
                    "用户想撤回已发出的动作 — 本工具做不到 (patrol 已点的按钮不可撤); "
                    "全局升级 / 人工接管 — 用 /api/runtime/pause 或 /takeover, 粒度更粗且可恢复; "
                    "想永久关 — 仍需改 env var 或 module 初始 enabled (见 ADR-004 §6.1 决策 A)."
                ),
                parameters_schema=name_schema,
                handler=self._disable_handler,
                mutates=True,
                requires_confirmation=False,
                risk_level=1,
                tags=["system", "patrol", "lifecycle"],
                examples=[
                    {"input": "关闭自动回复", "kwargs": {"name": "job_chat.patrol"}},
                ],
            ),
            IntentSpec(
                name="system.patrol.trigger",
                description=(
                    "Run a patrol ONCE right now, bypassing its interval "
                    "gating. Blocks until the handler returns. Full 5-stage "
                    "pipeline still applies — if circuit breaker is open, "
                    "trigger L0-skips. Side effects are identical to a "
                    "normal scheduled tick of that patrol."
                ),
                when_to_use=(
                    "用户不想等下一个 interval, 要求立刻执行一次. 典型触发: "
                    "'现在就跑一次自动回复', '手动触发 job_chat.patrol', "
                    "'立即拉一次未读消息'. 也可用于诊断 (跑完看 last_error)."
                ),
                when_not_to_use=(
                    "用户只想开启持续调度 — 用 enable (不会立刻跑); "
                    "用户想跑一个**未注册**的动作 — 不是 patrol, 应走对应业务 intent "
                    "(如 job.chat.process), 不要往本工具强行塞名字; "
                    "trigger 不重置 circuit breaker — 如果熔断已开, 先用 "
                    "/api/runtime/reset/{name} 或业务层的 reset 入口."
                ),
                parameters_schema=name_schema,
                handler=self._trigger_handler,
                mutates=True,
                requires_confirmation=True,
                risk_level=2,
                tags=["system", "patrol", "lifecycle"],
                examples=[
                    {
                        "input": "现在就跑一次自动回复",
                        "kwargs": {"name": "job_chat.patrol"},
                    },
                ],
            ),
        ]


module = PatrolControlModule()
