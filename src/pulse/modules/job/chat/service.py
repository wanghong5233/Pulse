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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from pulse.core.notify.notifier import Notification, Notifier

from .._connectors.base import JobPlatformConnector
from ..shared.enums import CardAction, CardType, ChatAction, ConversationInitiator
from ..memory import JobMemory, JobMemorySnapshot
from .planner import HrMessagePlanner, PlannedChatAction
from .replier import HrReplyGenerator
from .repository import ChatRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ChatPolicy:
    """Runtime knobs for the chat workflow, all sourced from Settings."""

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

    # ------------------------------------------------------------------ accessors

    @property
    def connector(self) -> JobPlatformConnector:
        return self._connector

    @property
    def policy(self) -> ChatPolicy:
        return self._policy

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
                notify_count += 1
                self._notifier.send(
                    Notification(
                        level="warning",
                        title="job_chat escalated",
                        content=f"{row.get('company')} / {row.get('job_title')}: {plan.reason}",
                        metadata={"conversation_id": row.get("conversation_id")},
                    )
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
        _ = reply_text, profile_id, conversation_hint
        safe_card_type = (card_type or "").strip()
        if not safe_card_type:
            return {"ok": False, "status": "failed", "error": "card_type is required for card action"}
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

        replier 会基于 snapshot + 对话历史生成更贴合用户偏好的文本, 并给出
        ``needs_hitl``/``confidence`` 信号。若 needs_hitl=True, 当前实现
        保持 action=reply 但让 auto_execute 通过 HITL 关卡 (由 policy 控制);
        未来可以改为直接升级为 escalate。
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
        if draft.needs_hitl:
            new_reason += " (replier:needs_hitl)"
        return PlannedChatAction(
            action=plan.action,
            reason=new_reason[:400] or "replier_generated",
            reply_text=draft.reply_text,
            card_type=plan.card_type,
            card_action=plan.card_action,
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
