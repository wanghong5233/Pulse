"""Business-logic layer for the job_chat capability.

Orchestrates the four user-visible flows:

  * ``run_ingest``  — push HR chat events from external sources into DB
  * ``run_pull``    — read the current inbox from the connector
  * ``run_process`` — pull + classify + optionally auto-execute
  * ``run_execute`` — manually trigger a single-conversation action

The service only depends on the :class:`JobPlatformConnector` contract,
an :class:`HrMessagePlanner`, a :class:`ChatRepository`, and a policy
dataclass. It never reads environment variables nor talks to FastAPI —
both responsibilities live in ``module.py``.

SafetyPlane 集成 (ADR-006-v2)
=============================

三条会产生外部副作用的 dispatch 路径 —— ``_execute_reply`` /
``_execute_send_resume`` / ``_execute_card`` —— 都在"真正调 connector"之前先跑
对应的 policy 函数 (``reply_policy`` / ``send_resume_policy`` /
``card_policy``). policy 返回:

* ``allow``  → 继续原路径, connector 真发, ``mark_processed`` 照常.
* ``deny``   → 返回 ``status="denied"`` + ``error``, 不触达 connector,
  ``mark_processed`` 也不跑 (保留 inbox 未读状态, 等下一轮 patrol 由用户
  介入处理).
* ``ask``    → 创建 SuspendedTask 挂起, 同时 ``mark_processed`` 仍然执行
  (否则下一轮 patrol 会再 plan 出同一条回复, 再 ask, 永动机), 并通过
  ``Notifier`` 直接把 ``render_ask_for_im`` 的文案推给用户. 用 Notifier
  而非 channel adapter 是因为: patrol 路径没有 IncomingMessage 上下文,
  拿不到 channel/user; Notifier 的 Feishu/企业微信 webhook 在 server 启动
  时就绑定好收件人, 是"后台任务叫醒用户"的唯一稳定通道.

三条 policy 自己永远不抛异常 —— policy 函数内部 fail-to-ask; 若 suspend
落盘失败, service 层保守退化为 deny 并在 error 里写 store 失败原因, 让运
维能从 audit 里发现.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import blake2s
from typing import Any, Callable, Literal, Mapping
from uuid import uuid4

from pulse.core.notify.notifier import Notification, Notifier
from pulse.core.safety import (
    AskRequest,
    Intent,
    PermissionContext,
    ResumeHandle,
    SAFETY_PLANE_ENFORCE,
    SAFETY_PLANE_OFF,
    SuspendedTask,
    SuspendedTaskStore,
    card_policy,
    render_ask_for_im,
    reply_policy,
    send_resume_policy,
)
from pulse.core.safety.decision import Decision
from pulse.core.safety.resume import ResumedExecution, ResumedTaskExecutor

from .._connectors.base import JobPlatformConnector
from ..shared.enums import CardAction, CardType, ChatAction, ConversationInitiator
from ..memory import JobMemory, JobMemorySnapshot
from .planner import HrMessagePlanner, PlannedChatAction
from .replier import HrReplyGenerator
from .repository import ChatRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ChatPolicy:
    """Runtime knobs for the chat workflow, all sourced from Settings.

    .. deprecated::
        ``hitl_required`` 是会话级 "是否开启自动回复" 的旧开关, 与 SafetyPlane
        的 **内容级** 授权判决语义不同 —— 前者 per-session 一刀切, 后者 per-
        intent 按规则判定, 两者一度被混淆. v2 架构下授权判决全部迁到 policy
        函数 (``pulse.core.safety.policies``). 本字段暂保留只为不破坏
        ``/health`` 与现存测试, 后续整体移除. 新代码不要读它; 读了也不能视
        作授权依据.
    """

    default_profile_id: str
    auto_execute: bool
    hitl_required: bool


EmitStageEvent = Callable[..., str]


def _lift_error(ok: bool, *parts: dict[str, Any]) -> str | None:
    """Surface the first non-empty ``error`` / ``status`` from nested executor
    results up to the top-level envelope so fail-loud is never masked.

    When the aggregate ``ok`` is True we return ``None`` (success has no
    error to report). When ``ok`` is False we MUST return something truthy
    — a silent empty string would violate the fail-loud constitution
    (``docs/code-review-checklist.md``)."""

    if ok:
        return None
    for part in parts:
        if not isinstance(part, dict):
            continue
        err = part.get("error")
        if err:
            return str(err)
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("ok"):
            continue
        status = part.get("status")
        if status:
            return f"status={status}"
    return "unknown executor failure"


# 下游 MCP 返回 "status" 的语义分层 (见 _boss_platform_runtime.reply_conversation
# 等入口): "sent"/"clicked" 是真的对平台产生了副作用; "logged"/"logged_only" 是
# killswitch 拦截下的 dry-run, 只写了 audit 没真发; "manual_required" 是等用户
# 介入。Service 不能把这些一视同仁塞回 "sent", 否则 trace_f78829ce4576 那种
# 01:46 1ms send_resume "成功" 会重现。
_TRUE_DELIVERY_STATUSES: frozenset[str] = frozenset({"sent", "clicked"})


# 卡片类型枚举 → 给用户看的中文描述. 仅用于 ask 文案, 映射缺失时回落到
# 枚举原值 (英文 slug) —— 宁可文案难看也不编中文.
_CARD_TYPE_HUMAN: dict[str, str] = {
    CardType.EXCHANGE_RESUME.value: "换简历请求",
    CardType.INTERVIEW_INVITE.value: "面试邀请",
}


def _human_card_type(card_type: str) -> str:
    return _CARD_TYPE_HUMAN.get(card_type, card_type or "卡片")


# conversation_hint 从 inbox row 拼出来, 本身是字符串字段, 但可能混入 None /
# 非字符串值 (BOSS MCP 偶尔 hr_name 给成 int). 挂起进 SuspendedTask 之前必须
# JSON-safe, 否则 WorkspaceMemory.set_fact 走 json.dumps 直接抛 TypeError,
# 挂起动作整体崩盘. 这里把所有值显式 str() 化, 过长的值截断 (保障单条 fact
# 不过大), 只保留对 Resume → Re-execute 有用的键.
_HINT_KEYS_FOR_RESUME: tuple[str, ...] = (
    "hr_name",
    "hr_id",
    "company",
    "job_title",
    "latest_hr_message",
    "card_title",
)


def _sanitize_hint(hint: Mapping[str, Any] | None) -> dict[str, str]:
    safe: dict[str, str] = {}
    if not hint:
        return safe
    for key in _HINT_KEYS_FOR_RESUME:
        value = hint.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        safe[key] = text[:400]
    return safe


# Resume → Re-execute 时把用户文本归类成 "同意 / 拒绝 / 不确定".
# 规则刻意**保守**: 只有精确命中白名单才算同意, 其余一律 decline, 避免用户
# 误发消息被当作确认把 HR 回复发出去. "不确定" 分支对用户的交代见
# ``ResumedExecution.summary``.
_APPROVE_TOKENS: frozenset[str] = frozenset({
    "y", "yes", "ok", "okay", "好", "好的", "是", "是的", "确认", "确定",
    "同意", "发", "可以", "行", "嗯", "对", "go",
})
_DECLINE_TOKENS: frozenset[str] = frozenset({
    "n", "no", "不", "否", "取消", "停", "别", "不行", "拒绝", "先不", "不要",
})


def _classify_user_answer(user_answer: str) -> Literal["approve", "decline", "unknown"]:
    text = (user_answer or "").strip().lower()
    if not text:
        return "unknown"
    if text in _APPROVE_TOKENS:
        return "approve"
    if text in _DECLINE_TOKENS:
        return "decline"
    return "unknown"


class JobChatService:
    def __init__(
        self,
        *,
        connector: JobPlatformConnector,
        repository: ChatRepository,
        planner: HrMessagePlanner,
        policy: ChatPolicy,
        notifier: Notifier,
        emit_stage_event: EmitStageEvent,
        preferences: JobMemory | None = None,
        replier: HrReplyGenerator | None = None,
    ) -> None:
        self._connector = connector
        self._repository = repository
        self._planner = planner
        self._policy = policy
        self._notifier = notifier
        self._emit = emit_stage_event
        self._preferences = preferences
        self._replier = replier
        # SafetyPlane 依赖: 在 server.py bootstrap 完成后通过
        # ``attach_safety_plane`` 注入; 未注入时 (mode=off / bootstrap 失败 /
        # 单测最小 fixture) 三条 _execute_* 走 legacy 直发路径.
        self._suspended_store: SuspendedTaskStore | None = None
        self._safety_workspace_id: str = ""
        self._safety_mode: str = SAFETY_PLANE_OFF

    # ------------------------------------------------------------------ accessors

    @property
    def connector(self) -> JobPlatformConnector:
        return self._connector

    @property
    def policy(self) -> ChatPolicy:
        return self._policy

    # ------------------------------------------------------------------ SafetyPlane wiring

    def attach_safety_plane(
        self,
        *,
        suspended_store: SuspendedTaskStore,
        workspace_id: str,
        mode: str,
    ) -> None:
        """Inject SuspendedTaskStore after server bootstrap finishes.

        v2 架构下, ``workspace_memory`` / ``event_bus`` 在 ``ModuleRegistry.
        discover`` 之后才就绪, service 不能在 ``__init__`` 里直接拿 store. 所以
        由 ``server.py`` 走完 bootstrap 后再回调此方法把依赖补齐.

        * ``mode="off"``  → 即便调了, 也不会真跑 policy gate; _execute_* 走旧
          的直发路径. 这条路径只留给灰度回滚, 默认配置下不走.
        * ``mode="enforce"`` → _execute_* 在 connector 之前强制 policy gate.

        重复调用会覆盖之前的注入 —— server 启动过程中只调一次, 测试里按需替换.
        """
        self._suspended_store = suspended_store
        self._safety_workspace_id = str(workspace_id or "").strip()
        self._safety_mode = str(mode or SAFETY_PLANE_OFF).strip().lower()

    def _safety_enforced(self) -> bool:
        """是否真的在 enforce 模式 + store 可用 —— 三条 _execute_* 的前置条件.

        两个条件必须同时成立:

        * store 已注入 (Ask 分支要挂起任务, 没 store 就无法挂起, 只能退化)
        * mode == enforce (off 档刻意跳过所有 policy gate)

        任何一个不满足都返回 False, 让 _execute_* 走旧直发路径. 这在单测和
        灰度回滚时都很关键: 测试 fixture 不想拖上整个 store 也能跑.
        """
        return (
            self._suspended_store is not None
            and self._safety_mode == SAFETY_PLANE_ENFORCE
            and bool(self._safety_workspace_id)
        )

    # ------------------------------------------------------------------ inbox load

    def _load_inbox(
        self,
        *,
        max_conversations: int,
        unread_only: bool,
        fetch_latest_hr: bool,
        chat_tab: str,
        trace_id: str | None,
    ) -> dict[str, Any]:
        trace_id = self._emit(
            stage="inbox_load",
            status="started",
            trace_id=trace_id,
            payload={
                "max_conversations": max_conversations,
                "unread_only": unread_only,
                "fetch_latest_hr": fetch_latest_hr,
                "chat_tab": chat_tab,
            },
        )
        provider_errors: list[str] = []
        if not self._connector.execution_ready:
            provider_errors.append("provider is not execution-ready")
            result = {"items": [], "source": self._connector.provider_name, "errors": provider_errors}
            self._emit(
                stage="inbox_load",
                status="failed",
                trace_id=trace_id,
                payload={"source": result["source"], "total": 0, "errors_total": 1},
            )
            return result

        provider_result = self._connector.pull_conversations(
            max_conversations=max_conversations,
            unread_only=unread_only,
            fetch_latest_hr=fetch_latest_hr,
            chat_tab=chat_tab,
        )
        provider_errors.extend(
            [str(item)[:400] for item in list(provider_result.get("errors") or [])]
        )
        normalized: list[dict[str, Any]] = []
        for row in list(provider_result.get("items") or []):
            if not isinstance(row, dict):
                continue
            item = ChatRepository.normalize_row(row)
            if item is not None:
                normalized.append(item)

        force_unread = chat_tab in {"未读", "新招呼"}
        if unread_only or force_unread:
            normalized = [row for row in normalized if int(row.get("unread_count") or 0) > 0]

        result = {
            "items": normalized[: max(1, min(max_conversations, 100))],
            "source": str(provider_result.get("source") or self._connector.provider_name),
            "errors": provider_errors,
        }
        self._emit(
            stage="inbox_load",
            status="completed",
            trace_id=trace_id,
            payload={
                "source": result["source"],
                "total": len(result["items"]),
                "errors_total": len(result["errors"]),
            },
        )
        return result

    # ------------------------------------------------------------------ ingest

    def run_ingest(
        self,
        *,
        rows: list[Any],
        source: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        trace_id = self._emit(
            stage="ingest",
            status="started",
            trace_id=trace_id,
            payload={"source": source, "rows_total": len(rows)},
        )
        if not rows:
            self._emit(
                stage="ingest",
                status="failed",
                trace_id=trace_id,
                payload={"source": source, "rows_total": 0, "inserted": 0},
            )
            return {"ok": False, "trace_id": trace_id, "inserted": 0, "error": "no rows provided"}

        now_iso = datetime.now(timezone.utc).isoformat()
        prepared = [ChatRepository.to_ingest_payload(row, source=source, now_iso=now_iso) for row in rows]
        outcome = self._repository.ingest_events(prepared, source=source)
        result = {
            "ok": outcome.inserted > 0,
            "trace_id": trace_id,
            "inserted": outcome.inserted,
            "skipped": outcome.skipped,
            "error": outcome.error,
        }
        self._emit(
            stage="ingest",
            status="completed" if result["ok"] else "failed",
            trace_id=trace_id,
            payload={
                "source": source,
                "inserted": outcome.inserted,
                "skipped": outcome.skipped,
                "error": outcome.error,
            },
        )
        return result

    # ------------------------------------------------------------------ pull

    def run_pull(
        self,
        *,
        max_conversations: int,
        unread_only: bool,
        fetch_latest_hr: bool,
        chat_tab: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        trace_id = self._emit(
            stage="pull",
            status="started",
            trace_id=trace_id,
            payload={
                "max_conversations": max_conversations,
                "unread_only": unread_only,
                "fetch_latest_hr": fetch_latest_hr,
                "chat_tab": chat_tab,
            },
        )
        inbox = self._load_inbox(
            max_conversations=max_conversations,
            unread_only=unread_only,
            fetch_latest_hr=fetch_latest_hr,
            chat_tab=chat_tab,
            trace_id=trace_id,
        )
        items = list(inbox.get("items") or [])
        unread_total = sum(int(item.get("unread_count") or 0) for item in items)
        result = {
            "trace_id": trace_id,
            "total": len(items),
            "unread_total": unread_total,
            "items": items,
            "source": str(inbox.get("source") or "unknown"),
            "errors": [str(item)[:400] for item in list(inbox.get("errors") or [])],
        }
        self._emit(
            stage="pull",
            status="completed",
            trace_id=trace_id,
            payload={
                "source": result["source"],
                "total": result["total"],
                "unread_total": unread_total,
                "errors_total": len(result["errors"]),
            },
        )
        return result

    # ------------------------------------------------------------------ process

    def run_process(
        self,
        *,
        max_conversations: int,
        unread_only: bool,
        profile_id: str,
        notify_on_escalate: bool,
        fetch_latest_hr: bool,
        auto_execute: bool,
        chat_tab: str,
        confirm_execute: bool,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        trace_id = self._emit(
            stage="process",
            status="started",
            trace_id=trace_id,
            payload={
                "max_conversations": max_conversations,
                "unread_only": unread_only,
                "profile_id": profile_id,
                "notify_on_escalate": notify_on_escalate,
                "fetch_latest_hr": fetch_latest_hr,
                "auto_execute": auto_execute,
                "chat_tab": chat_tab,
                "confirm_execute": confirm_execute,
            },
        )
        inbox = self._load_inbox(
            max_conversations=max_conversations,
            unread_only=unread_only,
            fetch_latest_hr=fetch_latest_hr,
            chat_tab=chat_tab,
            trace_id=trace_id,
        )
        conversations, skip_reasons = self._drop_blocked_conversations(
            list(inbox.get("items") or [])
        )
        errors = [str(item)[:400] for item in list(inbox.get("errors") or [])]
        errors.extend(skip_reasons)
        source = str(inbox.get("source") or "unknown")

        # batch 内复用同一份 snapshot, 避免每个 conversation 都扫 workspace_facts。
        snapshot = self._load_snapshot()
        items: list[dict[str, Any]] = []
        notify_count = 0
        _AUTO_EXECUTE_ACTIONS = {
            ChatAction.REPLY,
            ChatAction.SEND_RESUME,
            ChatAction.ACCEPT_CARD,
            ChatAction.REJECT_CARD,
        }
        for row in conversations:
            conversation_id = str(row.get("conversation_id") or "")
            has_resume_card = any(
                card.get("card_type") == CardType.EXCHANGE_RESUME.value
                for card in row.get("cards") or []
            )
            initiated_by_raw = str(row.get("initiated_by") or "").strip().lower()
            try:
                initiated_by = ConversationInitiator(initiated_by_raw)
            except ValueError:
                initiated_by = ConversationInitiator.UNKNOWN
            plan = self._planner.plan(
                message=str(row.get("latest_message") or ""),
                has_exchange_resume_card=has_resume_card,
                initiated_by=initiated_by,
                snapshot=snapshot,
                company=str(row.get("company") or ""),
                job_title=str(row.get("job_title") or ""),
            )
            plan = self._ensure_reply_text(plan, conversation=row, snapshot=snapshot)
            self._emit(
                stage="classify",
                status="completed",
                trace_id=trace_id,
                payload={
                    "conversation_id": conversation_id,
                    "hr_name": str(row.get("hr_name") or ""),
                    "company": str(row.get("company") or ""),
                    "initiated_by": initiated_by.value,
                    "has_resume_card": has_resume_card,
                    "action": plan.action.value,
                    "reason": plan.reason[:160],
                    "reply_text_len": len((plan.reply_text or "").strip()),
                    "will_auto_execute": bool(auto_execute and plan.action in _AUTO_EXECUTE_ACTIONS),
                },
            )
            execution: dict[str, Any] | None = None
            if plan.action == ChatAction.ESCALATE and notify_on_escalate:
                # ESCALATE 只是 planner 对当前消息的 **分类标签**, 真正的
                # HITL 升级在 action 要外发时由 SafetyPlane 用 AskRequest
                # 完成. 这里保留计数 + 审计, 不再走 Notifier 单向广播
                # (单向广播 ≠ 升级).
                notify_count += 1
                logger.info(
                    "job_chat.escalate_tagged conversation=%s company=%s reason=%s",
                    conversation_id,
                    row.get("company"),
                    plan.reason[:160],
                )
            if auto_execute and plan.action in _AUTO_EXECUTE_ACTIONS:
                execution = self._maybe_execute_planned(
                    row=row,
                    plan=plan,
                    profile_id=profile_id,
                    confirm_execute=confirm_execute,
                    errors=errors,
                )
                self._emit(
                    stage="auto_execute",
                    status="completed" if bool(execution.get("ok")) else "failed",
                    trace_id=trace_id,
                    payload={
                        "conversation_id": conversation_id,
                        "action": plan.action.value,
                        "ok": bool(execution.get("ok")),
                        "status": str(execution.get("status") or ""),
                        "needs_confirmation": bool(execution.get("needs_confirmation")),
                        "error": str(execution.get("error") or "")[:200],
                    },
                )
            items.append(
                {
                    "conversation_id": row["conversation_id"],
                    "hr_name": row["hr_name"],
                    "company": row["company"],
                    "job_title": row["job_title"],
                    "latest_hr_message": row["latest_message"],
                    "latest_hr_time": row["latest_time"],
                    "initiated_by": row.get("initiated_by") or ConversationInitiator.UNKNOWN.value,
                    "cards": row.get("cards") or [],
                    "action": plan.action.value,
                    "reason": plan.reason,
                    "reply_text": plan.reply_text,
                    "auto_executed": bool(execution and execution.get("ok")),
                    "execution": execution,
                }
            )
        result = {
            "trace_id": trace_id,
            "processed_count": len(items),
            "new_count": sum(1 for row in conversations if int(row.get("unread_count") or 0) > 0),
            "duplicated_count": 0,
            "notified_count": notify_count,
            "needs_confirmation": bool(
                auto_execute
                and self._policy.hitl_required
                and not confirm_execute
                and any(
                    item.get("action")
                    in {
                        ChatAction.REPLY.value,
                        ChatAction.SEND_RESUME.value,
                        ChatAction.ACCEPT_CARD.value,
                        ChatAction.REJECT_CARD.value,
                    }
                    for item in items
                )
            ),
            "items": items,
            "summary": {
                "profile_id": profile_id,
                "chat_tab": chat_tab,
                "source": source,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            "errors": errors,
        }
        action_counts: dict[str, int] = {}
        for item in items:
            key = str(item.get("action") or "unknown")
            action_counts[key] = action_counts.get(key, 0) + 1
        auto_executed_count = sum(1 for item in items if item.get("auto_executed"))
        self._emit(
            stage="process",
            status="completed",
            trace_id=trace_id,
            payload={
                "source": source,
                "processed_count": result["processed_count"],
                "new_count": result["new_count"],
                "notified_count": notify_count,
                "auto_executed_count": auto_executed_count,
                "action_counts": action_counts,
                "errors_total": len(errors),
                "errors_sample": [err[:160] for err in errors[:3]],
                "skip_reasons_sample": [reason[:160] for reason in skip_reasons[:3]],
            },
        )
        return result

    # ------------------------------------------------------------------ execute

    def run_execute(
        self,
        *,
        conversation_id: str,
        action: str,
        reply_text: str | None,
        profile_id: str,
        run_id: str | None,
        note: str | None,
        conversation_hint: dict[str, Any] | None,
        confirm_execute: bool,
        card_id: str | None = None,
        card_type: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        safe_conv = str(conversation_id or "").strip()
        action_lower = str(action or "").strip().lower() or ChatAction.REPLY.value
        trace_id = self._emit(
            stage="execute",
            status="started",
            trace_id=trace_id,
            payload={
                "conversation_id": safe_conv,
                "action": action_lower,
                "confirm_execute": confirm_execute,
            },
        )
        if not safe_conv:
            result = {"ok": False, "trace_id": trace_id, "error": "conversation_id is required"}
            self._emit(
                stage="execute",
                status="failed",
                trace_id=trace_id,
                payload={"conversation_id": safe_conv, "error": result["error"]},
            )
            return result
        if self._policy.hitl_required and not confirm_execute:
            result = {
                "ok": True,
                "trace_id": trace_id,
                "needs_confirmation": True,
                "conversation_id": safe_conv,
                "action": action_lower,
                "reason": "confirmation required before real execution",
            }
            self._emit(
                stage="execute",
                status="preview",
                trace_id=trace_id,
                payload={"conversation_id": safe_conv, "action": action_lower},
            )
            return result

        dispatcher = self._dispatcher_for(action_lower)
        if dispatcher is None:
            result = {
                "ok": False,
                "trace_id": trace_id,
                "needs_confirmation": False,
                "conversation_id": safe_conv,
                "action": action_lower,
                "error": f"unsupported action={action_lower}",
            }
            self._emit(
                stage="execute",
                status="failed",
                trace_id=trace_id,
                payload={
                    "conversation_id": safe_conv,
                    "action": action_lower,
                    "error": result["error"],
                },
            )
            return result

        outcome = dispatcher(
            conversation_id=safe_conv,
            reply_text=reply_text,
            profile_id=profile_id,
            run_id=run_id,
            note=note,
            conversation_hint=conversation_hint,
            card_id=card_id,
            card_type=card_type,
        )
        outcome["trace_id"] = trace_id
        outcome["needs_confirmation"] = False
        outcome["conversation_id"] = safe_conv
        outcome["action"] = action_lower
        self._emit(
            stage="execute",
            status="completed" if bool(outcome.get("ok")) else "failed",
            trace_id=trace_id,
            payload={
                "conversation_id": safe_conv,
                "action": action_lower,
                "status": str(outcome.get("status") or "unknown"),
            },
        )
        return outcome

    # ------------------------------------------------------------------ helpers

    def _dispatcher_for(self, action_lower: str):
        """Map ``JobChatExecuteRequest.action`` strings onto execution methods.

        ``mark_processed`` is an escape hatch for the operator: when a
        conversation needs to be dropped from the unread queue without
        replying (e.g. we already handled it out-of-band).
        """
        alias_map = {
            ChatAction.REPLY.value: self._execute_reply,
            ChatAction.SEND_RESUME.value: self._execute_send_resume,
            ChatAction.ACCEPT_CARD.value: lambda **kw: self._execute_card(action=CardAction.ACCEPT, **kw),
            ChatAction.REJECT_CARD.value: lambda **kw: self._execute_card(action=CardAction.REJECT, **kw),
            "mark_processed": self._execute_mark_only,
        }
        return alias_map.get(action_lower)

    # ── SafetyPlane 判决共享 helper ──
    #
    # 三条 _execute_* 都要做一样的事: 构造 Intent+PermissionContext, 跑
    # policy, 再把 Decision 翻成"继续/挂起/拒绝"三条统一 branch. 抽到私有
    # helper 而不是内联是为了 (a) service 的 connector 调用序列保持可读,
    # (b) "Ask 分支永远记得 mark_processed" 这个硬约束在一个地方就能看全.

    def _hash_draft(self, text: str) -> str:
        """给草稿一个稳定短哈希, 做 session_approvals 里的 draft 标识.

        用 blake2s 而不是 md5 / sha256:
        * blake2s 在 stdlib 且 hashlib 官方推荐做非密码学 keyed hashing,
          本场景不需要抗碰撞, 只需要"同文本 → 同哈希"的确定性.
        * 截 16 hex (64 bit) —— token 只做等式比较, 没必要用完 256 bit.
        """
        return blake2s(text.encode("utf-8"), digest_size=8).hexdigest()

    def _run_policy(
        self,
        *,
        policy_fn: Callable[[Intent, PermissionContext], Decision],
        intent: Intent,
        trace_id: str | None,
    ) -> Decision | None:
        """跑给定 policy; enforce 下异常转 Ask, 不放行外部副作用.

        返回 None 的条件:
        * ``_safety_enforced()`` 为 False —— off 档 / 测试 fixture 没注入 store.
        * policy 运行时抛异常 —— 正常情况 policies 是纯函数永不抛; 抛了说明
          有 bug, 必须让用户确认或人工接管, 不能 fail-open 直发.

        返回 Decision 时, _execute_* 必须根据 kind 分派, 不能忽略.
        """
        if not self._safety_enforced():
            return None
        ctx = PermissionContext(
            module="job_chat",
            task_id=f"job_chat:{intent.args.get('conversation_id') or intent.name}",
            trace_id=str(trace_id or "-") or "-",
            user_id=None,
        )
        try:
            return policy_fn(intent, ctx)
        except Exception:  # noqa: BLE001
            logger.exception(
                "safety.policy.failure module=job_chat intent=%s", intent.name
            )
            return self._policy_failure_ask(intent=intent, trace_id=trace_id)

    def _policy_failure_ask(
        self,
        *,
        intent: Intent,
        trace_id: str | None,
    ) -> Decision:
        conversation_id = str(intent.args.get("conversation_id") or "").strip()
        draft = str(
            intent.args.get("draft_text")
            or intent.args.get("reply_text")
            or ""
        ).strip() or None
        task_key = f"job_chat:{conversation_id or intent.name}"
        return Decision.ask(
            reason="policy_exception_fail_closed",
            rule_id="job_chat.policy.exception",
            ask_request=AskRequest(
                question=(
                    "自动授权检查失败。为避免误发, 这次操作已暂停。"
                    "如果你确认要继续, 请回复 y; 不继续请回复 n。"
                ),
                draft=draft,
                timeout_seconds=3600,
                context={
                    "intent": intent.name,
                    "trace_id": str(trace_id or ""),
                },
                resume_handle=ResumeHandle(
                    task_id=task_key,
                    module="job_chat",
                    intent="system.task.resume",
                    payload_schema="safety.v1.user_answer",
                ),
            ),
        )

    def _suspend_and_mark(
        self,
        *,
        decision: Decision,
        intent: Intent,
        conversation_id: str,
        run_id: str | None,
        note: str,
    ) -> dict[str, Any]:
        """Ask 分支统一收口: 挂起任务 + mark_processed + 返回 ``status=suspended``.

        **为什么 Ask 分支也 mark_processed?**

        否则下一轮 patrol 扫未读还会命中同一条 HR 消息, planner 再生成一遍
        同样的草稿, policy 再 ask, IM 再塞一条同样的 Ask —— 用户会被打个精
        光. mark_processed 只是从平台未读列表里把这条对话踢出去, 不代表
        "已回复". 真的发送与否完全由用户回 y/n 后下一轮 patrol 重新 policy
        来决定.

        挂起 store 本身也可能抛 (disk full / db down), 此时我们保守退化为
        ``status=denied`` + 明文 error: 外部 side-effect 没发生 + inbox 保留
        未读等下一轮 (不 mark, 避免丢消息).
        """
        store = self._suspended_store
        assert store is not None, "_suspend_and_mark called without store; bug"
        ask_request = decision.ask_request
        assert ask_request is not None, "Decision(kind=ask) without ask_request; bug"
        # 两个 id 的用途拆开:
        # * ``trace_id`` = handle.task_id (= "job_chat:<conversation_id>"),
        #   对话级稳定, 作为 store 的幂等 key (module + trace_id + intent.name
        #   相同的 awaiting 任务已存在时, create 幂等返回, 不发重复 Ask).
        # * ``task_id`` 是 store 的持久化主键, 每次新挂起独立生成 UUID, 避免
        #   跨轮次同一 conversation 产生 task_id 冲突 (上一条 timed_out 了,
        #   这一条需要新主键才能独立归档).
        # 幂等短路发生时, store.create 返回既有 task, 我们生成的 new_task_id
        # 根本没写盘, 不产生孤儿条目.
        new_task_id = f"safety_{uuid4().hex[:12]}"
        try:
            task = store.create(
                task_id=new_task_id,
                workspace_id=self._safety_workspace_id,
                module="job_chat",
                trace_id=ask_request.resume_handle.task_id,
                intent=intent,
                ask_request=ask_request,
                origin_rule_id=decision.rule_id,
                origin_decision_reason=decision.reason,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "safety.suspend.failed intent=%s conversation=%s",
                intent.name,
                conversation_id,
            )
            return {
                "ok": False,
                "status": "denied",
                "error": f"safety.suspend_failed: {exc}",
            }
        # 幂等检测: store.create 返回既有 awaiting 任务时, task.task_id 会是
        # 首次挂起时的 id, 而不是我们这轮生成的 new_task_id. 此时跳过
        # mark_processed / notify —— 前者已在首次调用时做过 (action_audit
        # 不必叠一条), 后者是刚需去重: 每轮 patrol 若都发一遍 "请确认", 用户
        # 就会被每 5min 打扰一次, 直到手动回. 这条是 v2 验收时实测发现的 bug,
        # 修 + 测试锁死.
        is_new_suspension = task.task_id == new_task_id
        mark_result: dict[str, Any] | None = None
        mark_ok = False
        if is_new_suspension:
            mark_result = self._connector.mark_processed(
                conversation_id=conversation_id,
                run_id=run_id or datetime.now(timezone.utc).strftime("chat-%Y%m%d%H%M%S"),
                note=note,
            )
            mark_ok = bool(mark_result.get("ok"))

            # 挂起后立刻把 ask 问题推给用户. 用 Notifier 而非 channel adapter:
            # patrol 触发场景下没有 IncomingMessage 上下文, 拿不到 channel/user;
            # Notifier (Feishu / WeCom webhook) 在 server 启动时已配置好收件人,
            # 正是 "后台任务也能叫醒用户" 的通用通道. 失败只记日志, 不因通知
            # 发不出就让已经 suspend 的任务原子性崩掉 (SuspendedTask 还在,
            # 下次 patrol 可检测 timeout → 自动通知 + deny).
            ask_text = render_ask_for_im(ask_request, channel="feishu")
            try:
                self._notifier.send(
                    Notification(
                        level="warn",
                        title="Pulse 需要你确认",
                        content=ask_text,
                        metadata={
                            "task_id": task.task_id,
                            "intent": intent.name,
                            "conversation_id": conversation_id,
                        },
                    )
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "safety.ask.notify_failed task_id=%s intent=%s",
                    task.task_id,
                    intent.name,
                )
        else:
            logger.info(
                "safety.ask.idempotent_skip task_id=%s intent=%s conversation=%s "
                "(既有 awaiting 任务, 跳过 mark_processed + notify)",
                task.task_id,
                intent.name,
                conversation_id,
            )

        logger.info(
            "safety.ask.suspended task_id=%s intent=%s conversation=%s "
            "rule_id=%s idempotent=%s mark_ok=%s notify_sent=%s",
            task.task_id,
            intent.name,
            conversation_id,
            decision.rule_id,
            not is_new_suspension,
            mark_ok,
            is_new_suspension,
        )
        return {
            "ok": False,
            "status": "suspended",
            "needs_confirmation": True,
            "error": None,
            "safety": {
                "kind": "ask",
                "task_id": task.task_id,
                "rule_id": decision.rule_id,
                "reason": decision.reason,
                "idempotent": not is_new_suspension,
            },
            "mark_result": mark_result,
            "mark_ok": mark_ok,
        }

    def _denied(self, decision: Decision) -> dict[str, Any]:
        """Deny 分支统一收口: 不触达 connector, 不 mark_processed.

        留未读状态给下一轮 patrol, 由用户主动处理 —— 这是 "永远不替用户
        绕过拒绝" 的硬承诺. 代价是可能导致同一条 HR 消息重复进入未读队
        列一次 (到下轮 patrol 会再次被 planner 拦下并再 deny), 能接受.
        """
        return {
            "ok": False,
            "status": "denied",
            "error": decision.reason,
            "safety": {
                "kind": "deny",
                "rule_id": decision.rule_id,
                "deny_code": decision.deny_code,
                "reason": decision.reason,
            },
        }

    def _execute_reply(
        self,
        *,
        conversation_id: str,
        reply_text: str | None,
        profile_id: str,
        run_id: str | None,
        note: str | None,
        conversation_hint: dict[str, Any] | None,
        card_id: str | None = None,
        card_type: str | None = None,
    ) -> dict[str, Any]:
        _ = card_id, card_type
        safe_reply = str(reply_text or "").strip()
        if not safe_reply:
            return {"ok": False, "error": "reply_text is required for reply action", "status": "failed"}
        hint = dict(conversation_hint or {})
        # Resume → Re-execute 需要的字段全部塞进 intent.args: conversation_hint
        # / profile_id / note 在 SuspendedTask 归档序列化后仍保留, 恢复执行时
        # ResumedTaskExecutor 直接从 task.original_intent.args 读即可, 不依赖
        # service 进程级缓存 (进程重启后缓存丢, 挂起任务却还在).
        policy_intent = Intent(
            kind="mutation",
            name="job.chat.reply",
            args={
                "conversation_id": conversation_id,
                "hr_label": str(
                    hint.get("hr_name") or hint.get("company") or "HR"
                ),
                "hr_message": str(hint.get("latest_hr_message") or ""),
                "draft_text": safe_reply,
                "draft_hash": self._hash_draft(safe_reply),
                "profile_id": str(profile_id or ""),
                "conversation_hint": _sanitize_hint(hint),
            },
        )
        decision = self._run_policy(
            policy_fn=reply_policy,
            intent=policy_intent,
            trace_id=run_id,
        )
        if decision is not None:
            if decision.kind == "deny":
                return self._denied(decision)
            if decision.kind == "ask":
                return self._suspend_and_mark(
                    decision=decision,
                    intent=policy_intent,
                    conversation_id=conversation_id,
                    run_id=run_id,
                    note=note or "safety.ask awaiting user confirmation (reply)",
                )
            # decision.kind == "allow" → 继续原流程.

        reply_result = self._connector.reply_conversation(
            conversation_id=conversation_id,
            reply_text=safe_reply,
            profile_id=profile_id,
            conversation_hint=dict(conversation_hint or {}),
        )
        mark_result = self._connector.mark_processed(
            conversation_id=conversation_id,
            run_id=run_id or datetime.now(timezone.utc).strftime("chat-%Y%m%d%H%M%S"),
            note=note or "execute action=reply",
        )
        ok = bool(reply_result.get("ok")) and bool(mark_result.get("ok"))
        # 把下游 MCP 的真实 status 透传回来, 而不是把 "logged" / "manual_required"
        # 等干跑路径强行升级成 "sent"。status 升级是 trace_f78829ce4576 / 01:46
        # 1ms send_resume 假绿的根因。
        reply_status = str(reply_result.get("status") or "").strip().lower()
        if ok and reply_status in _TRUE_DELIVERY_STATUSES:
            final_status = "sent"
        elif ok:
            final_status = reply_status or "unknown"
        else:
            final_status = reply_status or "failed"
        return {
            "ok": ok and reply_status in _TRUE_DELIVERY_STATUSES,
            "status": final_status,
            "error": _lift_error(
                ok and reply_status in _TRUE_DELIVERY_STATUSES,
                reply_result,
                mark_result,
            ),
            "reply_result": reply_result,
            "mark_result": mark_result,
        }

    def _execute_send_resume(
        self,
        *,
        conversation_id: str,
        reply_text: str | None,
        profile_id: str,
        run_id: str | None,
        note: str | None,
        conversation_hint: dict[str, Any] | None,
        card_id: str | None = None,
        card_type: str | None = None,
    ) -> dict[str, Any]:
        _ = reply_text, card_id, card_type
        hint = dict(conversation_hint or {})
        resume_profile_id = profile_id or self._policy.default_profile_id
        # 用 conversation_id 做 hr_id fallback —— 平台真的 hr_id 在 hint 里
        # 没稳定透传, 先用 conversation 做一次性 session_approval 的锚点.
        # 未来 BossConnector 把 hr_id 塞进 hint["hr_id"] 时直接优先用那个.
        hr_id = str(hint.get("hr_id") or conversation_id or "").strip()
        policy_intent = Intent(
            kind="mutation",
            name="job.chat.send_resume",
            args={
                "conversation_id": conversation_id,
                "hr_id": hr_id,
                "hr_label": str(
                    hint.get("hr_name") or hint.get("company") or "HR"
                ),
                "resume_profile_id": resume_profile_id,
                "profile_id": str(profile_id or ""),
                "conversation_hint": _sanitize_hint(hint),
            },
        )
        decision = self._run_policy(
            policy_fn=send_resume_policy,
            intent=policy_intent,
            trace_id=run_id,
        )
        if decision is not None:
            if decision.kind == "deny":
                return self._denied(decision)
            if decision.kind == "ask":
                return self._suspend_and_mark(
                    decision=decision,
                    intent=policy_intent,
                    conversation_id=conversation_id,
                    run_id=run_id,
                    note=note or "safety.ask awaiting user confirmation (send_resume)",
                )

        attach_result = self._connector.send_resume_attachment(
            conversation_id=conversation_id,
            resume_profile_id=profile_id or self._policy.default_profile_id,
            conversation_hint=dict(conversation_hint or {}),
        )
        mark_result = self._connector.mark_processed(
            conversation_id=conversation_id,
            run_id=run_id or datetime.now(timezone.utc).strftime("chat-%Y%m%d%H%M%S"),
            note=note or "execute action=send_resume",
        )
        ok = bool(attach_result.get("ok")) and bool(mark_result.get("ok"))
        attach_status = str(attach_result.get("status") or "").strip().lower()
        # dry-run / killswitch 路径 (status=logged / logged_only / manual_required)
        # 必须透传, 绝不重写为 "sent"。否则 env 一改 dry-run 就恢复假绿。
        if ok and attach_status in _TRUE_DELIVERY_STATUSES:
            final_status = "sent"
        elif ok:
            final_status = attach_status or "unknown"
        else:
            final_status = attach_status or "failed"
        return {
            "ok": ok and attach_status in _TRUE_DELIVERY_STATUSES,
            "status": final_status,
            "error": _lift_error(
                ok and attach_status in _TRUE_DELIVERY_STATUSES,
                attach_result,
                mark_result,
            ),
            "attach_result": attach_result,
            "mark_result": mark_result,
        }

    def _execute_mark_only(
        self,
        *,
        conversation_id: str,
        reply_text: str | None,
        profile_id: str,
        run_id: str | None,
        note: str | None,
        conversation_hint: dict[str, Any] | None,
        card_id: str | None = None,
        card_type: str | None = None,
    ) -> dict[str, Any]:
        _ = reply_text, profile_id, conversation_hint, card_id, card_type
        mark_result = self._connector.mark_processed(
            conversation_id=conversation_id,
            run_id=run_id or datetime.now(timezone.utc).strftime("chat-%Y%m%d%H%M%S"),
            note=note or "manual mark",
        )
        ok = bool(mark_result.get("ok"))
        return {
            "ok": ok,
            "status": str(mark_result.get("status") or "failed"),
            "error": _lift_error(ok, mark_result),
            "mark_result": mark_result,
        }

    def _execute_card(
        self,
        *,
        action: CardAction,
        conversation_id: str,
        reply_text: str | None,
        profile_id: str,
        run_id: str | None,
        note: str | None,
        conversation_hint: dict[str, Any] | None,
        card_id: str | None,
        card_type: str | None,
    ) -> dict[str, Any]:
        _ = reply_text, profile_id
        safe_card_type = (card_type or "").strip()
        if not safe_card_type:
            return {"ok": False, "status": "failed", "error": "card_type is required for card action"}
        hint = dict(conversation_hint or {})
        policy_intent = Intent(
            kind="mutation",
            name=f"job.chat.card.{action.value}",
            args={
                "conversation_id": conversation_id,
                "card_type": safe_card_type,
                "card_type_human": _human_card_type(safe_card_type),
                "card_title": str(hint.get("card_title") or ""),
                "suggested_action": (
                    "接受" if action == CardAction.ACCEPT else "拒绝"
                ),
                "card_id": str(card_id or ""),
                "card_action": action.value,
                "profile_id": str(profile_id or ""),
                "conversation_hint": _sanitize_hint(hint),
            },
        )
        decision = self._run_policy(
            policy_fn=card_policy,
            intent=policy_intent,
            trace_id=run_id,
        )
        if decision is not None:
            if decision.kind == "deny":
                return self._denied(decision)
            if decision.kind == "ask":
                return self._suspend_and_mark(
                    decision=decision,
                    intent=policy_intent,
                    conversation_id=conversation_id,
                    run_id=run_id,
                    note=note or f"safety.ask awaiting user confirmation (card.{action.value})",
                )

        click_result = self._connector.click_conversation_card(
            conversation_id=conversation_id,
            card_id=str(card_id or ""),
            card_type=safe_card_type,
            action=action.value,
        )
        mark_result = self._connector.mark_processed(
            conversation_id=conversation_id,
            run_id=run_id or datetime.now(timezone.utc).strftime("chat-%Y%m%d%H%M%S"),
            note=note or f"execute action=card.{action.value}",
        )
        ok = bool(click_result.get("ok")) and bool(mark_result.get("ok"))
        click_status = str(click_result.get("status") or "").strip().lower()
        if ok and click_status in _TRUE_DELIVERY_STATUSES:
            final_status = "clicked"
        elif ok:
            final_status = click_status or "unknown"
        else:
            final_status = click_status or "failed"
        return {
            "ok": ok and click_status in _TRUE_DELIVERY_STATUSES,
            "status": final_status,
            "error": _lift_error(
                ok and click_status in _TRUE_DELIVERY_STATUSES,
                click_result,
                mark_result,
            ),
            "click_result": click_result,
            "mark_result": mark_result,
        }

    def _maybe_execute_planned(
        self,
        *,
        row: dict[str, Any],
        plan: PlannedChatAction,
        profile_id: str,
        confirm_execute: bool,
        errors: list[str],
    ) -> dict[str, Any]:
        if self._policy.hitl_required and not confirm_execute:
            return {
                "ok": False,
                "status": "pending_confirmation",
                "needs_confirmation": True,
                "error": "confirmation required before real execution",
            }
        conversation_id = str(row.get("conversation_id") or "")
        conversation_hint = {
            "hr_name": str(row.get("hr_name") or ""),
            "company": str(row.get("company") or ""),
            "job_title": str(row.get("job_title") or ""),
        }
        run_id = datetime.now(timezone.utc).strftime("chat-%Y%m%d%H%M%S")
        # Pick the first card matching the planner's target type so the
        # click/resume dispatch has a concrete ``card_id`` to click.
        cards = list(row.get("cards") or [])
        first_card: dict[str, Any] | None = None
        if plan.card_type is not None:
            first_card = next(
                (c for c in cards if c.get("card_type") == plan.card_type.value),
                None,
            )
        if plan.action in {ChatAction.REPLY}:
            outcome = self._execute_reply(
                conversation_id=conversation_id,
                reply_text=plan.reply_text or "",
                profile_id=profile_id,
                run_id=run_id,
                note="auto_execute from job_chat.process",
                conversation_hint=conversation_hint,
            )
        elif plan.action == ChatAction.SEND_RESUME:
            outcome = self._execute_send_resume(
                conversation_id=conversation_id,
                reply_text=None,
                profile_id=profile_id,
                run_id=run_id,
                note="auto_execute from job_chat.process",
                conversation_hint=conversation_hint,
            )
        elif plan.action in {ChatAction.ACCEPT_CARD, ChatAction.REJECT_CARD}:
            outcome = self._execute_card(
                action=CardAction.ACCEPT if plan.action == ChatAction.ACCEPT_CARD else CardAction.REJECT,
                conversation_id=conversation_id,
                reply_text=None,
                profile_id=profile_id,
                run_id=run_id,
                note="auto_execute from job_chat.process",
                conversation_hint=conversation_hint,
                card_id=str((first_card or {}).get("card_id") or ""),
                card_type=(plan.card_type or CardType.UNKNOWN).value,
            )
        else:  # pragma: no cover — defensive
            return {
                "ok": False,
                "status": "failed",
                "error": f"planner produced unsupported action={plan.action.value}",
            }
        # Surface errors into the outer audit channel.
        for candidate in (
            (outcome.get("reply_result") or {}).get("error"),
            (outcome.get("mark_result") or {}).get("error"),
            (outcome.get("attach_result") or {}).get("error"),
            (outcome.get("click_result") or {}).get("error"),
        ):
            error = str(candidate or "").strip()
            if error:
                errors.append(error[:400])
        outcome["needs_confirmation"] = False
        return outcome

    def _load_snapshot(self) -> JobMemorySnapshot | None:
        """batch 级一次性拿 snapshot; 无 preferences 或抛错都降级为 None。"""
        if self._preferences is None:
            return None
        try:
            return self._preferences.snapshot()
        except Exception as exc:  # pragma: no cover
            logger.warning("chat: JobMemory.snapshot() failed: %s", exc)
            return None

    def _ensure_reply_text(
        self,
        plan: PlannedChatAction,
        *,
        conversation: dict[str, Any],
        snapshot: JobMemorySnapshot | None,
    ) -> PlannedChatAction:
        """若 planner 选了 ``reply`` 但没给出 reply_text, 调 replier 补一段。

        replier 只产 **候选草稿**; 是否真的发出由 SafetyPlane 在 tool-use
        前判决. 此函数不再消费 ``draft.needs_hitl`` —— 那是 LLM 自陈信号,
        属于 Advisor 而非 Judge.
        """
        if plan.action != ChatAction.REPLY:
            return plan
        if plan.reply_text and plan.reply_text.strip():
            return plan
        if self._replier is None:
            # 没配 replier, 用 planner 的 heuristic reply 或保守 stall。
            return PlannedChatAction(
                action=plan.action,
                reason=plan.reason or "replier unavailable",
                reply_text=plan.reply_text or "您好，稍后详细回复您。",
                card_type=plan.card_type,
                card_action=plan.card_action,
            )
        try:
            draft = self._replier.draft(
                hr_message=str(conversation.get("latest_message") or ""),
                conversation=conversation,
                snapshot=snapshot,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("chat replier failed, keep planner output: %s", exc)
            return plan
        if not draft.reply_text:
            return plan
        new_reason = plan.reason
        if draft.reason:
            new_reason = f"{new_reason}; replier:{draft.reason}" if new_reason else f"replier:{draft.reason}"
        return PlannedChatAction(
            action=plan.action,
            reason=new_reason[:400] or "replier_generated",
            reply_text=draft.reply_text,
            card_type=plan.card_type,
            card_action=plan.card_action,
        )

    # ── Resume → Re-execute ────────────────────────────────
    #
    # SafetyPlane v2 的"最后一米": 用户在 IM 回 "y" 后, server 层的
    # ``try_resume_suspended_turn`` resolve 掉 SuspendedTask, 然后立刻
    # 通过 ``resumed_task_executor`` 回到业务侧把原 intent 真跑掉.
    #
    # **为什么不走 policy 再发一次?**
    # 本层已经拿到了用户明确的 "y" —— 这就是 policy 在 Ask 分支索取的
    # "用户裁决". 若此处再把 intent 包回 policy, 要么要临时构造
    # session_approvals (脆弱), 要么又落回 Ask (死循环). 明确跳过 policy
    # gate, 直接调 connector, 同时保证 mark_processed 幂等 —— 因为挂起
    # 时已经 mark 过, 这里再 mark 也只是把 run_id 的审计再写一条, 不会
    # 对 BOSS 造成副作用.
    #
    # **executor 契约** (详见 :mod:`pulse.core.safety.resume`):
    # * 不抛: 所有失败包成 ``ResumedExecution(status="failed")`` 返回.
    # * 同步: 短路径执行, 不要 await (resume 路径是 IM 入站线程).
    # * 幂等: 同一 task_id 被喂两次应返回相同结果而不重复发送 HR.

    def resumed_task_executor(
        self, *, task: SuspendedTask, user_answer: str
    ) -> ResumedExecution:
        """Resume 回调入口; 注册到 server.py 的 executors 表."""
        if task.module != "job_chat":
            return ResumedExecution(
                status="failed",
                ok=False,
                summary="内部错误: job_chat 无法处理非 job_chat 任务。",
                detail={"task_module": task.module},
            )
        verdict = _classify_user_answer(user_answer)
        if verdict == "decline":
            return ResumedExecution(
                status="declined",
                ok=True,
                summary="已记录你的拒绝, 本条不会发送给 HR。",
                detail={"intent": task.original_intent.name},
            )
        if verdict == "unknown":
            # 拿不准一律保守: 按拒绝处理, 给用户讲清楚原因, 让他知道下一步
            # 该怎么做. 不试图二次 ask —— 任务已 resolve, 再挂一条会把
            # store 幂等语义搞乱 (trace_id 仍是同一个 conversation, 新挂起
            # 的 awaiting 会命中幂等短路返回旧 task, 但旧 task 已 resumed
            # 终态, 下次 resolve 直接抛 TaskAlreadyTerminalError).
            return ResumedExecution(
                status="undetermined",
                ok=False,
                summary=(
                    "没识别出明确的 y / n, 我按\"不发送\"记录了。"
                    "需要的话请直接发一条新草稿, 我会走一个新的确认。"
                ),
                detail={
                    "intent": task.original_intent.name,
                    "user_answer_preview": (user_answer or "")[:60],
                },
            )

        intent_name = task.original_intent.name
        if intent_name == "job.chat.reply":
            return self._resume_reply(task)
        if intent_name == "job.chat.send_resume":
            return self._resume_send_resume(task)
        if intent_name.startswith("job.chat.card."):
            return self._resume_card(task)
        logger.warning(
            "job_chat.resume.unsupported_intent task_id=%s intent=%s",
            task.task_id,
            intent_name,
        )
        return ResumedExecution(
            status="failed",
            ok=False,
            summary="内部错误: 这类任务暂不支持自动重发, 请手动操作。",
            detail={"intent": intent_name},
        )

    def _resume_reply(self, task: SuspendedTask) -> ResumedExecution:
        args = task.original_intent.args
        conversation_id = str(args.get("conversation_id") or "").strip()
        draft_text = str(args.get("draft_text") or "").strip()
        profile_id = str(args.get("profile_id") or "").strip() or self._policy.default_profile_id
        hint_raw = args.get("conversation_hint") or {}
        conversation_hint = dict(hint_raw) if isinstance(hint_raw, Mapping) else {}
        if not conversation_id or not draft_text:
            return ResumedExecution(
                status="failed",
                ok=False,
                summary="原始草稿已失效, 请重新让我拉一次未读消息。",
                detail={"reason": "missing conversation_id or draft_text"},
            )
        run_id = datetime.now(timezone.utc).strftime("resume-%Y%m%d%H%M%S")
        reply_result = self._connector.reply_conversation(
            conversation_id=conversation_id,
            reply_text=draft_text,
            profile_id=profile_id,
            conversation_hint=conversation_hint,
        )
        # 挂起时已 mark_processed, 这里再 mark 只是为了重写 run_id 让审计里能
        # 看到 "resume-xxxx" 而非 "chat-xxxx", 便于把自动发出的消息和当初挂
        # 起的记录关联. 若下游 MCP 对重复 mark 敏感, 可以后续加 idempotent
        # 校验, 目前 BOSS MCP 的 mark_processed 只写 action_audit, 多写一条
        # 没有对外副作用.
        mark_result = self._connector.mark_processed(
            conversation_id=conversation_id,
            run_id=run_id,
            note="resume.execute action=reply",
        )
        reply_status = str(reply_result.get("status") or "").strip().lower()
        delivered = (
            bool(reply_result.get("ok"))
            and bool(mark_result.get("ok"))
            and reply_status in _TRUE_DELIVERY_STATUSES
        )
        if delivered:
            hr_label = str(args.get("hr_label") or "HR")
            logger.info(
                "job_chat.resume.executed task_id=%s intent=job.chat.reply "
                "conversation=%s run_id=%s status=%s mark_ok=%s",
                task.task_id,
                conversation_id,
                run_id,
                reply_status,
                bool(mark_result.get("ok")),
            )
            return ResumedExecution(
                status="executed",
                ok=True,
                summary=f"已把草稿发给 HR {hr_label}。",
                detail={
                    "intent": "job.chat.reply",
                    "status": reply_status,
                    "conversation_id": conversation_id,
                },
            )
        logger.warning(
            "job_chat.resume.failed task_id=%s intent=job.chat.reply "
            "conversation=%s run_id=%s reply_status=%s reply_ok=%s mark_ok=%s",
            task.task_id,
            conversation_id,
            run_id,
            reply_status,
            bool(reply_result.get("ok")),
            bool(mark_result.get("ok")),
        )
        return ResumedExecution(
            status="failed",
            ok=False,
            summary=(
                "已记录你的确认, 但发送给 HR 时失败, 请稍后手动发一下。"
            ),
            detail={
                "intent": "job.chat.reply",
                "reply_status": reply_status,
                "reply_error": str(reply_result.get("error") or "")[:300],
                "mark_error": str(mark_result.get("error") or "")[:300],
            },
        )

    def _resume_send_resume(self, task: SuspendedTask) -> ResumedExecution:
        args = task.original_intent.args
        conversation_id = str(args.get("conversation_id") or "").strip()
        resume_profile_id = (
            str(args.get("resume_profile_id") or "").strip()
            or str(args.get("profile_id") or "").strip()
            or self._policy.default_profile_id
        )
        hint_raw = args.get("conversation_hint") or {}
        conversation_hint = dict(hint_raw) if isinstance(hint_raw, Mapping) else {}
        if not conversation_id:
            return ResumedExecution(
                status="failed",
                ok=False,
                summary="原始会话 ID 已丢失, 请重新让我拉一次未读。",
                detail={"reason": "missing conversation_id"},
            )
        run_id = datetime.now(timezone.utc).strftime("resume-%Y%m%d%H%M%S")
        attach_result = self._connector.send_resume_attachment(
            conversation_id=conversation_id,
            resume_profile_id=resume_profile_id,
            conversation_hint=conversation_hint,
        )
        mark_result = self._connector.mark_processed(
            conversation_id=conversation_id,
            run_id=run_id,
            note="resume.execute action=send_resume",
        )
        attach_status = str(attach_result.get("status") or "").strip().lower()
        delivered = (
            bool(attach_result.get("ok"))
            and bool(mark_result.get("ok"))
            and attach_status in _TRUE_DELIVERY_STATUSES
        )
        if delivered:
            hr_label = str(args.get("hr_label") or "HR")
            logger.info(
                "job_chat.resume.executed task_id=%s intent=job.chat.send_resume "
                "conversation=%s run_id=%s status=%s mark_ok=%s profile_id=%s",
                task.task_id,
                conversation_id,
                run_id,
                attach_status,
                bool(mark_result.get("ok")),
                resume_profile_id,
            )
            return ResumedExecution(
                status="executed",
                ok=True,
                summary=f"已把简历发给 HR {hr_label}。",
                detail={
                    "intent": "job.chat.send_resume",
                    "status": attach_status,
                    "resume_profile_id": resume_profile_id,
                },
            )
        logger.warning(
            "job_chat.resume.failed task_id=%s intent=job.chat.send_resume "
            "conversation=%s run_id=%s attach_status=%s attach_ok=%s mark_ok=%s",
            task.task_id,
            conversation_id,
            run_id,
            attach_status,
            bool(attach_result.get("ok")),
            bool(mark_result.get("ok")),
        )
        return ResumedExecution(
            status="failed",
            ok=False,
            summary="已记录你的确认, 但简历发送失败, 请稍后手动处理。",
            detail={
                "intent": "job.chat.send_resume",
                "attach_status": attach_status,
                "attach_error": str(attach_result.get("error") or "")[:300],
                "mark_error": str(mark_result.get("error") or "")[:300],
            },
        )

    def _resume_card(self, task: SuspendedTask) -> ResumedExecution:
        args = task.original_intent.args
        conversation_id = str(args.get("conversation_id") or "").strip()
        card_id = str(args.get("card_id") or "").strip()
        card_type = str(args.get("card_type") or "").strip()
        card_action = str(args.get("card_action") or "").strip()
        if not conversation_id or not card_id or not card_type or not card_action:
            return ResumedExecution(
                status="failed",
                ok=False,
                summary="卡片信息已丢失, 请重新让我拉一次未读卡片。",
                detail={
                    "reason": "missing card metadata",
                    "conversation_id": conversation_id,
                    "card_id_present": bool(card_id),
                    "card_type": card_type,
                    "card_action": card_action,
                },
            )
        run_id = datetime.now(timezone.utc).strftime("resume-%Y%m%d%H%M%S")
        click_result = self._connector.click_conversation_card(
            conversation_id=conversation_id,
            card_id=card_id,
            card_type=card_type,
            action=card_action,
        )
        mark_result = self._connector.mark_processed(
            conversation_id=conversation_id,
            run_id=run_id,
            note=f"resume.execute action=card.{card_action}",
        )
        click_status = str(click_result.get("status") or "").strip().lower()
        delivered = (
            bool(click_result.get("ok"))
            and bool(mark_result.get("ok"))
            and click_status in _TRUE_DELIVERY_STATUSES
        )
        card_human = _human_card_type(card_type)
        action_human = "接受" if card_action == CardAction.ACCEPT.value else "拒绝"
        if delivered:
            logger.info(
                "job_chat.resume.executed task_id=%s intent=%s conversation=%s "
                "run_id=%s status=%s mark_ok=%s card_type=%s action=%s",
                task.task_id,
                task.original_intent.name,
                conversation_id,
                run_id,
                click_status,
                bool(mark_result.get("ok")),
                card_type,
                card_action,
            )
            return ResumedExecution(
                status="executed",
                ok=True,
                summary=f"已{action_human}该{card_human}。",
                detail={
                    "intent": task.original_intent.name,
                    "status": click_status,
                    "card_type": card_type,
                },
            )
        logger.warning(
            "job_chat.resume.failed task_id=%s intent=%s conversation=%s run_id=%s "
            "click_status=%s click_ok=%s mark_ok=%s card_type=%s action=%s",
            task.task_id,
            task.original_intent.name,
            conversation_id,
            run_id,
            click_status,
            bool(click_result.get("ok")),
            bool(mark_result.get("ok")),
            card_type,
            card_action,
        )
        return ResumedExecution(
            status="failed",
            ok=False,
            summary=f"已记录你的确认, 但{action_human}{card_human}时失败, 请手动处理。",
            detail={
                "intent": task.original_intent.name,
                "click_status": click_status,
                "click_error": str(click_result.get("error") or "")[:300],
                "mark_error": str(mark_result.get("error") or "")[:300],
            },
        )

    def _drop_blocked_conversations(
        self, rows: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if self._preferences is None:
            return list(rows), []
        snap = self._preferences.snapshot()
        kept: list[dict[str, Any]] = []
        reasons: list[str] = []
        for row in rows:
            company = str(row.get("company") or "").strip()
            avoided, avoid_reason = snap.is_company_avoided(company)
            if avoided:
                reasons.append(
                    f"skip:company_avoided conversation={row.get('conversation_id')}"
                    f" company={company} reason={avoid_reason or '-'}"
                )
                continue
            kept.append(row)
        if reasons:
            logger.info("job_chat preference filter dropped %d conversation(s)", len(reasons))
        return kept, reasons
