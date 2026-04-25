"""Controller / entry-point for the ``job.chat`` capability.

Thin adapter on top of :class:`JobChatService` — wires in the concrete
platform connector, the chat repository, the LLM-backed planner, the
HITL policy and the workspace-scoped preference store. Exposes HTTP
routes under ``/api/modules/job/chat`` and binds a patrol on the
AgentRuntime when enabled.

This module contains **zero** ``os.getenv`` calls and no
platform-specific branching — BOSS (or any future platform) is hidden
behind the connector registry.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

from ....core.llm.router import LLMRouter
from ....core.module import BaseModule, IntentSpec
from ....core.notify.notifier import ConsoleNotifier, Notifier
from ....core.safety import (
    ResumedTaskExecutor,
    SuspendedTask,
    SuspendedTaskStore,
)
from ....core.safety.resume import ResumedExecution
from ....core.storage.engine import DatabaseEngine
from ....core.task_context import TaskContext
from .._connectors import build_connector
from ..config import get_job_settings
from ..shared.models import (
    JobChatExecuteRequest,
    JobChatIngestRequest,
    JobChatProcessRequest,
    JobChatPullRequest,
)
from ..memory import JobMemory
from .planner import HrMessagePlanner
from .replier import HrReplyGenerator
from .repository import ChatRepository
from .service import ChatPolicy, JobChatService

logger = logging.getLogger(__name__)


class JobChatModule(BaseModule):
    name = "job_chat"
    description = "Job-domain HR chat copilot (multi-platform ready)"
    route_prefix = "/api/modules/job/chat"
    tags = ["job", "job_chat"]

    def __init__(
        self,
        *,
        service: JobChatService | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        super().__init__()
        self._settings = get_job_settings()
        self._notifier: Notifier = notifier or ConsoleNotifier()
        self._service = service or self._build_default_service()
        self.intents = self._build_intents()

    # ------------------------------------------------------------------ wiring

    def _build_default_service(self) -> JobChatService:
        settings = self._settings
        connector = build_connector()
        engine: DatabaseEngine | None
        try:
            engine = DatabaseEngine()
        except Exception as exc:
            logger.warning("chat module starting without DB engine: %s", exc)
            engine = None
        repository = ChatRepository(engine=engine)
        preferences: JobMemory | None = None
        if engine is not None:
            preferences = JobMemory.from_engine(
                engine,
                workspace_id=settings.default_workspace_id,
                source="job.chat",
            )
        # planner 与 replier 共享同一个 LLMRouter 实例, 避免重复构造客户端;
        # LLMRouter 按 route 内部取不同模型, 本身是无状态的。
        llm_router = LLMRouter()
        planner = HrMessagePlanner(llm_router)
        replier = HrReplyGenerator(llm_router)
        policy = ChatPolicy(
            default_profile_id=settings.chat_default_profile_id,
            auto_execute=settings.chat_auto_execute,
            hitl_required=settings.hitl_required,
        )
        return JobChatService(
            connector=connector,
            repository=repository,
            planner=planner,
            policy=policy,
            notifier=self._notifier,
            emit_stage_event=self.emit_stage_event,
            preferences=preferences,
            replier=replier,
        )

    # ------------------------------------------------------------------ SafetyPlane

    def attach_safety_plane(
        self,
        *,
        suspended_store: SuspendedTaskStore,
        workspace_id: str,
        mode: str,
    ) -> None:
        """Forward SafetyPlane wiring down to the inner service.

        server.py 在所有 module ``on_startup`` 之前调此方法, 让 service 拿到
        ``SuspendedTaskStore`` 并在 _execute_* 前置跑 policy gate. 这里直接
        透传 server 下发的 ``workspace_id`` —— 全局一致, 避免与 inbound
        resume 查出的 workspace 对不齐.
        """
        self._service.attach_safety_plane(
            suspended_store=suspended_store,
            workspace_id=workspace_id,
            mode=mode,
        )

    def get_resumed_task_executor(self) -> ResumedTaskExecutor | None:
        """Expose the service's Resume → Re-execute callback.

        server.py 把 module -> executor 表注入
        ``try_resume_suspended_turn``; 用户答 "y" 后 resume 回路调回这里把
        原 intent 就地重跑 (见 ``JobChatService.resumed_task_executor``).
        """

        def _executor(
            *, task: SuspendedTask, user_answer: str
        ) -> ResumedExecution:
            return self._service.resumed_task_executor(
                task=task, user_answer=user_answer
            )

        return _executor

    # ------------------------------------------------------------------ AgentRuntime

    def on_startup(self) -> None:
        if not self._runtime:
            return
        # ADR-004 §6.1.1: 无条件注册, 初始 enabled=False
        # (runtime 默认值); 启停由 system.patrol.enable/disable 经 IM 独占
        # 控制 — 单一认知路径。interval 字段只决定调度节拍, 与启停语义正交。
        self._runtime.register_patrol(
            name="job_chat.patrol",
            handler=self._patrol,
            peak_interval=int(self._settings.patrol_chat_interval_peak),
            offpeak_interval=int(self._settings.patrol_chat_interval_offpeak),
            weekday_windows=((9, 18),),
            weekend_windows=((9, 18),),
        )

    def _patrol(self, ctx: TaskContext) -> dict[str, Any]:
        _ = ctx
        # Patrol is the explicit "自动回复已开启" path. Once user enables this
        # background task via IM, execution should be real (not preview-only):
        # - auto_execute=True: actually send reply / click resume card
        # - confirm_execute=True: patrol-enable action itself is the HITL gate
        # This avoids the historical trap where policy defaults
        # (chat_auto_execute=false + hitl_required=true) made patrol run but
        # never perform any external action.
        return self._service.run_process(
            max_conversations=20,
            unread_only=True,
            profile_id=self._service.policy.default_profile_id,
            notify_on_escalate=True,
            fetch_latest_hr=True,
            auto_execute=True,
            chat_tab="未读",
            confirm_execute=True,
        )

    # ------------------------------------------------------------------ IntentSpec

    def _build_intents(self) -> list[IntentSpec]:
        """细粒度 intent 工具, 供 Brain tool_use 直接调用。

        - ``job.chat.pull``: 只读,拉未读对话预览;
        - ``job.chat.process``: 分类 + 可选自动回复/发送简历, 触达真实 BOSS 动作时
          依赖 ``ChatPolicy.auto_execute`` + ``confirm_execute`` + HITL 门控。

        两个 intent 本身都不写 memory (``mutates=False``); ``process`` 因为可能
        产生对外副作用标记为 ``risk_level=2`` + ``requires_confirmation=True``。

        见 ``docs/Pulse-DomainMemory与Tool模式.md`` §4.3 / §7.3 M1。
        """
        s = self._service
        default_profile_id = s.policy.default_profile_id

        def _pull_handler(**kwargs: Any) -> dict[str, Any]:
            return s.run_pull(
                max_conversations=int(kwargs.get("max_conversations") or 10),
                unread_only=bool(kwargs.get("unread_only", True)),
                fetch_latest_hr=bool(kwargs.get("fetch_latest_hr", True)),
                chat_tab=str(kwargs.get("chat_tab") or "未读"),
            )

        def _process_handler(**kwargs: Any) -> dict[str, Any]:
            profile_id = kwargs.get("profile_id")
            return s.run_process(
                max_conversations=int(kwargs.get("max_conversations") or 10),
                unread_only=bool(kwargs.get("unread_only", True)),
                profile_id=str(profile_id) if profile_id else default_profile_id,
                notify_on_escalate=bool(kwargs.get("notify_on_escalate", True)),
                fetch_latest_hr=bool(kwargs.get("fetch_latest_hr", True)),
                auto_execute=bool(kwargs.get("auto_execute") or False),
                chat_tab=str(kwargs.get("chat_tab") or "未读"),
                confirm_execute=bool(kwargs.get("confirm_execute") or False),
            )

        return [
            IntentSpec(
                name="job.chat.pull",
                description=(
                    "Read-only: fetch the latest (optionally unread-only) HR conversations "
                    "from the recruiting platform inbox. No replies sent."
                ),
                when_to_use=(
                    "只读: 从招聘平台收件箱拉对话列表。参数: unread_only (默认 true), "
                    "fetch_latest_hr=true 会深入每个会话页抓最新 HR 消息, max_conversations 上限 50。"
                    "无发送副作用, 不做分类, 返回值直接是对话对象数组。"
                ),
                when_not_to_use=(
                    "职责划分: 1) 分类 / 回复 / 发送简历 → `job.chat.process`; "
                    "2) 检索历史对话关键词 → `memory_search`; "
                    "3) 连接器未 ready 时工具返回 ok=false, 调用方不得伪造\"今日无新消息\"。"
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "max_conversations": {
                            "type": "integer",
                            "description": "Max conversations to return (default 10).",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 50,
                        },
                        "unread_only": {
                            "type": "boolean",
                            "description": "If true, only pull unread conversations.",
                            "default": True,
                        },
                        "fetch_latest_hr": {
                            "type": "boolean",
                            "description": "If true, open each conversation to get the latest HR message.",
                            "default": True,
                        },
                        "chat_tab": {
                            "type": "string",
                            "description": "Which inbox tab to read ('未读' / '全部' etc).",
                            "default": "未读",
                        },
                    },
                    "additionalProperties": False,
                },
                handler=_pull_handler,
                mutates=False,
                risk_level=0,
                examples=[
                    {
                        "user_utterance": "看一下 HR 都说了什么",
                        "kwargs": {"max_conversations": 10, "unread_only": True},
                    },
                    {
                        "user_utterance": "/chat pull 20 条",
                        "kwargs": {"max_conversations": 20},
                    },
                ],
            ),
            IntentSpec(
                name="job.chat.process",
                description=(
                    "Classify unread HR messages and (optionally) reply / send resume. "
                    "auto_execute=false → preview only; auto_execute=true + confirm_execute=true → "
                    "actually reply on BOSS under HITL policy."
                ),
                when_to_use=(
                    "用户\"当下就要我扫一遍未读并回复\"的一次性请求。对未读 HR "
                    "对话做 LLM 分类, 并可选地回复 / 发送简历。"
                    "两级 HITL 闸门: auto_execute=false (默认) 仅返回分类 + 预览计划, 无发送; "
                    "auto_execute=true + confirm_execute=true 才真发到平台。"
                    "回复 / 附件只能走这里, 这是唯一的招聘平台对话框写入通道。"
                ),
                when_not_to_use=(
                    "职责划分: 1) 只查看未读不回复 → `job.chat.pull`; "
                    "2) 主动发起 (非回复) 招呼 → `job.greet.trigger`; "
                    "3) 仅有一级 HITL 时只返回预览, 不得把预览当作\"已发送\"呈现。"
                    "4) 用户表达\"开启 / 启动 / 打开 / 托管 / 让它持续监听新消息\" "
                    "→ 走 `system.patrol.enable(name=\"job_chat.patrol\")`, "
                    "那边默认会立即跑一次 + 继续调度, 不要用本工具代替长程开启。"
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "max_conversations": {
                            "type": "integer",
                            "description": "Max conversations to classify/process (default 10).",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 50,
                        },
                        "unread_only": {"type": "boolean", "default": True},
                        "profile_id": {
                            "type": "string",
                            "description": (
                                "Resume profile id to use when sending attachments. "
                                f"Omit to use the default '{default_profile_id}'."
                            ),
                        },
                        "notify_on_escalate": {
                            "type": "boolean",
                            "description": "If true, notify channel when HITL escalation is needed.",
                            "default": True,
                        },
                        "fetch_latest_hr": {"type": "boolean", "default": True},
                        "auto_execute": {
                            "type": "boolean",
                            "description": (
                                "If false, only return the plan. If true, dispatch the action "
                                "(still gated by confirm_execute + HITL policy)."
                            ),
                            "default": False,
                        },
                        "chat_tab": {"type": "string", "default": "未读"},
                        "confirm_execute": {
                            "type": "boolean",
                            "description": "Explicit user confirmation gate for real BOSS actions.",
                            "default": False,
                        },
                    },
                    "additionalProperties": False,
                },
                handler=_process_handler,
                mutates=False,
                requires_confirmation=True,
                risk_level=2,
                examples=[
                    {
                        "user_utterance": "帮我看看 HR 发了什么, 先不用回",
                        "kwargs": {"auto_execute": False, "confirm_execute": False},
                    },
                    {
                        "user_utterance": "自动回复一下 HR",
                        "kwargs": {"auto_execute": True, "confirm_execute": False},
                    },
                ],
            ),
        ]

    # ------------------------------------------------------------------ HTTP

    def register_routes(self, router: APIRouter) -> None:
        @router.get("/health")
        async def health() -> dict[str, Any]:
            connector = self._service.connector
            return {
                "module": self.name,
                "status": "ok",
                "runtime": {
                    "mode": "real_connector" if connector.execution_ready else "degraded_connector",
                    "provider": connector.provider_name,
                    "hitl_required": self._service.policy.hitl_required,
                    "auto_execute": self._service.policy.auto_execute,
                    "default_profile_id": self._service.policy.default_profile_id,
                    "connector": connector.health(),
                },
            }

        @router.post("/inbox/ingest")
        async def inbox_ingest(payload: JobChatIngestRequest) -> dict[str, Any]:
            return self._service.run_ingest(
                rows=list(payload.items),
                source=payload.source,
            )

        @router.post("/process")
        async def process(payload: JobChatProcessRequest) -> dict[str, Any]:
            return self._service.run_process(
                max_conversations=payload.max_conversations,
                unread_only=payload.unread_only,
                profile_id=payload.profile_id,
                notify_on_escalate=payload.notify_on_escalate,
                fetch_latest_hr=payload.fetch_latest_hr,
                auto_execute=payload.auto_execute,
                chat_tab=payload.chat_tab,
                confirm_execute=payload.confirm_execute,
            )

        @router.post("/pull")
        async def pull(payload: JobChatPullRequest) -> dict[str, Any]:
            return self._service.run_pull(
                max_conversations=payload.max_conversations,
                unread_only=payload.unread_only,
                fetch_latest_hr=payload.fetch_latest_hr,
                chat_tab=payload.chat_tab,
            )

        @router.post("/execute")
        async def execute(payload: JobChatExecuteRequest) -> dict[str, Any]:
            return self._service.run_execute(
                conversation_id=payload.conversation_id,
                action=payload.action,
                reply_text=payload.reply_text,
                profile_id=payload.profile_id,
                run_id=payload.run_id,
                note=payload.note,
                conversation_hint=payload.conversation_hint,
                confirm_execute=payload.confirm_execute,
                card_id=payload.card_id,
                card_type=payload.card_type,
            )

        @router.get("/session/check")
        async def session_check() -> dict[str, Any]:
            return self._service.connector.check_login()


def get_module() -> JobChatModule:
    return JobChatModule()
