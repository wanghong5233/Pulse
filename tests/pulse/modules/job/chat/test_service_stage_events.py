"""Hard contract: ``JobChatService.run_process`` MUST emit per-conversation
stage events so that 3 未读被处理到哪一步 / 结果是什么 在日志里直接可见.

Before this contract existed 循环里只在 process started/completed 各打一条,
任何 "plan 落在 ESCALATE/IGNORE 不执行 auto_execute" 或
"auto_execute 返回 pending_confirmation / browser_failed" 都是完全看不见的
盲区 —— 导致 M9.2 在生产环境 "processed_count=3 但 0 条回复发出"
无法从 trace 文件倒推原因. 这里把 per-conversation 的 classify
+ auto_execute 事件以及 errors_sample / action_counts 做成不可回退的合同.
"""

from __future__ import annotations

from typing import Any

from pulse.core.action_report import ACTION_REPORT_KEY
from pulse.core.notify.notifier import Notification
from pulse.modules.job._connectors.base import JobPlatformConnector
from pulse.modules.job.chat.planner import HrMessagePlanner, PlannedChatAction
from pulse.modules.job.chat.repository import ChatRepository
from pulse.modules.job.chat.service import ChatPolicy, JobChatService
from pulse.modules.job.shared.enums import ChatAction


# ----------------------------------------------------------------------
# minimal fakes — stay in-process, touch no DB, no LLM, no browser
# ----------------------------------------------------------------------


class _FakeConnector(JobPlatformConnector):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.reply_calls: list[dict[str, Any]] = []

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def execution_ready(self) -> bool:
        return True

    def health(self) -> dict[str, Any]:
        return {"ok": True}

    def check_login(self) -> dict[str, Any]:
        return {"logged_in": True}

    def scan_jobs(self, **_: Any) -> dict[str, Any]:
        return {"ok": True, "items": [], "source": self.provider_name}

    def fetch_job_detail(self, **_: Any) -> dict[str, Any]:
        return {"ok": True, "source": self.provider_name}

    def greet_job(self, **_: Any) -> dict[str, Any]:
        return {"ok": True, "source": self.provider_name, "conversation_id": ""}

    def pull_conversations(self, **_: Any) -> dict[str, Any]:
        return {"items": list(self._rows), "source": self.provider_name, "errors": []}

    def reply_conversation(
        self,
        *,
        conversation_id: str,
        reply_text: str,
        profile_id: str,
        conversation_hint: dict[str, Any],
    ) -> dict[str, Any]:
        self.reply_calls.append(
            {
                "conversation_id": conversation_id,
                "reply_text": reply_text,
                "profile_id": profile_id,
            }
        )
        return {"ok": True, "source": self.provider_name, "status": "sent"}

    def mark_processed(self, **_: Any) -> dict[str, Any]:
        return {"ok": True, "source": self.provider_name, "status": "noop"}


class _FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[Notification] = []

    def send(self, message: Notification) -> None:
        self.messages.append(message)


class _ScriptedPlanner(HrMessagePlanner):
    """Return a preset ``PlannedChatAction`` per conversation, no LLM."""

    def __init__(self, scripts: dict[str, PlannedChatAction]) -> None:
        # deliberately skip super().__init__ — we never touch the router.
        self._scripts = scripts

    def plan(self, *, message: str, **_: Any) -> PlannedChatAction:
        return self._scripts[message]


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _row(
    *,
    conversation_id: str,
    hr_name: str,
    company: str,
    latest_message: str,
    unread_count: int = 1,
) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "hr_name": hr_name,
        "company": company,
        "job_title": "AI Agent Intern",
        "latest_message": latest_message,
        "latest_time": "刚刚",
        "unread_count": unread_count,
        "initiated_by": "hr",
    }


def _build_service(
    *,
    rows: list[dict[str, Any]],
    scripts: dict[str, PlannedChatAction],
    auto_execute: bool = True,
    hitl_required: bool = False,
) -> tuple[JobChatService, list[dict[str, Any]], _FakeConnector]:
    events: list[dict[str, Any]] = []

    def _emit(
        *,
        stage: str,
        status: str,
        trace_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        resolved_trace = trace_id or "tr_test"
        events.append(
            {
                "stage": stage,
                "status": status,
                "trace_id": resolved_trace,
                "payload": dict(payload or {}),
            }
        )
        return resolved_trace

    connector = _FakeConnector(rows)
    service = JobChatService(
        connector=connector,
        repository=ChatRepository(engine=None),
        planner=_ScriptedPlanner(scripts),
        policy=ChatPolicy(
            default_profile_id="default",
            auto_execute=auto_execute,
            hitl_required=hitl_required,
        ),
        notifier=_FakeNotifier(),
        emit_stage_event=_emit,
    )
    return service, events, connector


# ----------------------------------------------------------------------
# 1. per-conversation ``classify`` event — without this we cannot see
#    what planner decided for each of the N unread rows.
# ----------------------------------------------------------------------


def test_run_process_emits_one_classify_event_per_conversation() -> None:
    rows = [
        _row(conversation_id="c1", hr_name="韦先生", company="洁岸", latest_message="你好 1"),
        _row(conversation_id="c2", hr_name="周晨业", company="阿里", latest_message="你好 2"),
        _row(conversation_id="c3", hr_name="胡女士", company="实苍", latest_message="你好 3"),
    ]
    scripts = {
        "你好 1": PlannedChatAction(action=ChatAction.IGNORE, reason="noise"),
        "你好 2": PlannedChatAction(action=ChatAction.ESCALATE, reason="sensitive"),
        "你好 3": PlannedChatAction(
            action=ChatAction.REPLY, reason="answerable", reply_text="您好 已收到"
        ),
    }
    service, events, _connector = _build_service(rows=rows, scripts=scripts)

    service.run_process(
        max_conversations=10,
        unread_only=True,
        profile_id="default",
        notify_on_escalate=False,
        fetch_latest_hr=True,
        auto_execute=True,
        chat_tab="未读",
        confirm_execute=True,
    )

    classify_events = [e for e in events if e["stage"] == "classify"]
    assert len(classify_events) == 3, (
        "run_process MUST emit exactly one classify event per processed conversation; "
        f"got {len(classify_events)} out of 3 rows"
    )
    keyed = {e["payload"]["conversation_id"]: e["payload"] for e in classify_events}
    assert set(keyed.keys()) == {"c1", "c2", "c3"}
    assert keyed["c1"]["action"] == ChatAction.IGNORE.value
    assert keyed["c1"]["will_auto_execute"] is False
    assert keyed["c2"]["action"] == ChatAction.ESCALATE.value
    assert keyed["c2"]["will_auto_execute"] is False
    assert keyed["c3"]["action"] == ChatAction.REPLY.value
    assert keyed["c3"]["will_auto_execute"] is True
    assert keyed["c3"]["reply_text_len"] == len("您好 已收到")
    for payload in keyed.values():
        assert "hr_name" in payload
        assert "company" in payload
        assert "initiated_by" in payload
        assert "reason" in payload


# ----------------------------------------------------------------------
# 2. ``auto_execute`` event — only when planner picks a whitelisted
#    action AND auto_execute=True; its payload surfaces ok/status/error
#    so broken browser paths do not silently swallow.
# ----------------------------------------------------------------------


def test_run_process_emits_auto_execute_event_only_for_whitelisted_actions() -> None:
    rows = [
        _row(conversation_id="c1", hr_name="A", company="A", latest_message="m1"),
        _row(conversation_id="c2", hr_name="B", company="B", latest_message="m2"),
    ]
    scripts = {
        "m1": PlannedChatAction(
            action=ChatAction.REPLY, reason="ok", reply_text="好的"
        ),
        "m2": PlannedChatAction(action=ChatAction.IGNORE, reason="noise"),
    }
    service, events, connector = _build_service(rows=rows, scripts=scripts)

    service.run_process(
        max_conversations=10,
        unread_only=True,
        profile_id="default",
        notify_on_escalate=False,
        fetch_latest_hr=True,
        auto_execute=True,
        chat_tab="未读",
        confirm_execute=True,
    )

    auto_events = [e for e in events if e["stage"] == "auto_execute"]
    assert len(auto_events) == 1, (
        "auto_execute event must be emitted exactly for whitelisted actions "
        f"(expected 1 for REPLY row); got {len(auto_events)}"
    )
    assert auto_events[0]["payload"]["conversation_id"] == "c1"
    assert auto_events[0]["payload"]["action"] == ChatAction.REPLY.value
    assert auto_events[0]["status"] == "completed"
    assert auto_events[0]["payload"]["ok"] is True
    assert len(connector.reply_calls) == 1
    assert connector.reply_calls[0]["reply_text"] == "好的"


# ----------------------------------------------------------------------
# 3. ``process completed`` payload MUST expose the data needed to
#    answer "why did 3 unread translate to 0 auto-executions?"
# ----------------------------------------------------------------------


def test_process_completed_payload_exposes_action_counts_and_errors_sample() -> None:
    rows = [
        _row(conversation_id="c1", hr_name="A", company="A", latest_message="m1"),
        _row(conversation_id="c2", hr_name="B", company="B", latest_message="m2"),
        _row(conversation_id="c3", hr_name="C", company="C", latest_message="m3"),
    ]
    scripts = {
        "m1": PlannedChatAction(action=ChatAction.IGNORE, reason="noise"),
        "m2": PlannedChatAction(action=ChatAction.ESCALATE, reason="sensitive"),
        "m3": PlannedChatAction(action=ChatAction.ESCALATE, reason="sensitive"),
    }
    service, events, _connector = _build_service(rows=rows, scripts=scripts)

    service.run_process(
        max_conversations=10,
        unread_only=True,
        profile_id="default",
        notify_on_escalate=False,
        fetch_latest_hr=True,
        auto_execute=True,
        chat_tab="未读",
        confirm_execute=True,
    )

    done = next(
        e for e in events if e["stage"] == "process" and e["status"] == "completed"
    )
    payload = done["payload"]
    assert payload["processed_count"] == 3
    assert payload["auto_executed_count"] == 0, (
        "with IGNORE/ESCALATE plans, no auto-execute should run — payload "
        "must report 0 so downstream can see '3 未读 → 0 回复'"
    )
    assert payload["action_counts"] == {
        ChatAction.IGNORE.value: 1,
        ChatAction.ESCALATE.value: 2,
    }
    assert isinstance(payload["errors_sample"], list)
    assert isinstance(payload["skip_reasons_sample"], list)


def test_process_completed_payload_surfaces_execution_error_strings() -> None:
    rows = [_row(conversation_id="c1", hr_name="A", company="A", latest_message="m1")]
    scripts = {
        "m1": PlannedChatAction(
            action=ChatAction.REPLY, reason="ok", reply_text="好的"
        ),
    }
    service, events, connector = _build_service(rows=rows, scripts=scripts)

    def _broken_reply(**_: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "source": "fake",
            "error": "browser_send_failed: conversation drawer not open",
        }

    connector.reply_conversation = _broken_reply  # type: ignore[assignment]

    service.run_process(
        max_conversations=10,
        unread_only=True,
        profile_id="default",
        notify_on_escalate=False,
        fetch_latest_hr=True,
        auto_execute=True,
        chat_tab="未读",
        confirm_execute=True,
    )

    done = next(
        e for e in events if e["stage"] == "process" and e["status"] == "completed"
    )
    payload = done["payload"]
    assert payload["errors_total"] >= 1
    assert payload["errors_sample"], (
        "errors_sample must be non-empty when errors_total >= 1 — otherwise "
        "the operator sees 'errors_total=1' with no way to know what failed"
    )
    assert any(
        "browser_send_failed" in sample for sample in payload["errors_sample"]
    )
    auto_events = [e for e in events if e["stage"] == "auto_execute"]
    assert len(auto_events) == 1
    assert auto_events[0]["status"] == "failed"
    assert auto_events[0]["payload"]["ok"] is False
    assert auto_events[0]["payload"]["error"], (
        "fail-loud: ok=False 时 auto_execute stage event 的 error 字段必须非空。"
        "production 回归 trace_a825a6d00d13 正因为此字段被空字符串静默吞掉,"
        "selector drift ('attach trigger selector not found') 只能在 "
        "errors_sample 里才看到 —— 现在必须在 stage event 直接暴露."
    )
    assert "browser_send_failed" in auto_events[0]["payload"]["error"]


# ----------------------------------------------------------------------
# 5. auto_execute event MUST lift nested executor errors.
# Real trace to reproduce:
#   trace_a825a6d00d13 韦先生 send_resume ->
#     attach_result.ok=False
#     attach_result.error="attach trigger selector not found"
#   But auto_execute stage event logged "error": "" because the service
#   layer only looked at the top-level envelope. That masked the root cause
#   (selector drift in `_default_attach_trigger_selectors`) for one test
#   cycle. This test pins the contract so the bug cannot return.
# ----------------------------------------------------------------------


def test_auto_execute_event_surfaces_nested_send_resume_selector_error() -> None:
    rows = [_row(conversation_id="c1", hr_name="韦先生", company="流岸", latest_message="m1")]
    scripts = {
        "m1": PlannedChatAction(
            action=ChatAction.SEND_RESUME, reason="hr greeted, send resume first"
        ),
    }
    service, events, connector = _build_service(rows=rows, scripts=scripts)

    def _selector_missing_attach(**_: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "source": "fake",
            "status": "selector_missing",
            "error": "attach trigger selector not found",
        }

    connector.send_resume_attachment = _selector_missing_attach  # type: ignore[assignment]

    service.run_process(
        max_conversations=10,
        unread_only=True,
        profile_id="default",
        notify_on_escalate=False,
        fetch_latest_hr=True,
        auto_execute=True,
        chat_tab="未读",
        confirm_execute=True,
    )

    auto_events = [e for e in events if e["stage"] == "auto_execute"]
    assert len(auto_events) == 1
    payload = auto_events[0]["payload"]
    assert payload["action"] == ChatAction.SEND_RESUME.value
    assert payload["ok"] is False
    assert "attach trigger selector not found" in payload["error"], (
        "The nested attach_result.error MUST be lifted to the stage event "
        "top-level `error` field, not buried inside attach_result/mark_result. "
        "Otherwise log readers will see 'ok=false error=\"\"' and waste time "
        "hunting for the real cause."
    )


# ----------------------------------------------------------------------
# 6. Dry-run killswitch must NOT be laundered into "sent".
# Real-world reproducer: logs/pulse.log 2026-04-23 01:46~02:06 contained
# 10+ send_resume_attachment calls returning ok=true in 1-2 ms because
# PULSE_BOSS_MCP_REPLY_MODE defaulted to log_only / manual_required.
# Service layer blindly rewrote status to "sent" and the operator saw
# "send_resume sent" without BOSS ever receiving the file. The contract
# here: when the MCP executor returns status="logged_only" (or any status
# outside the {sent, clicked} whitelist), service must transparently
# propagate that status and set ok=False.
# ----------------------------------------------------------------------


def test_logged_only_status_is_not_laundered_into_sent_for_send_resume() -> None:
    rows = [_row(conversation_id="c1", hr_name="A", company="A", latest_message="m1")]
    scripts = {
        "m1": PlannedChatAction(
            action=ChatAction.SEND_RESUME, reason="hr asked for resume"
        ),
    }
    service, events, connector = _build_service(rows=rows, scripts=scripts)

    def _dryrun_send_resume(**_: Any) -> dict[str, Any]:
        # Mirrors _boss_platform_runtime.send_resume_attachment's
        # log_only/dry_run_ok branch exactly.
        return {
            "ok": True,
            "source": "fake",
            "status": "logged_only",
            "error": None,
        }

    connector.send_resume_attachment = _dryrun_send_resume  # type: ignore[assignment]

    service.run_process(
        max_conversations=10,
        unread_only=True,
        profile_id="default",
        notify_on_escalate=False,
        fetch_latest_hr=True,
        auto_execute=True,
        chat_tab="未读",
        confirm_execute=True,
    )

    auto_events = [e for e in events if e["stage"] == "auto_execute"]
    assert len(auto_events) == 1
    payload = auto_events[0]["payload"]
    assert payload["status"] == "logged_only", (
        "Dry-run status MUST be propagated verbatim; rewriting it into 'sent' "
        "is exactly how trace_f78829ce4576 produced 10+ 1ms 'successful' "
        "send_resume calls with zero actual delivery."
    )
    assert payload["ok"] is False, (
        "Dry-run path did NOT produce a real side effect on BOSS, so the "
        "aggregate ok MUST be False — upstream reporting 'I sent your resume' "
        "on a killswitch dry-run is the canonical fail-loud violation."
    )


def test_logged_only_status_is_not_laundered_into_sent_for_reply() -> None:
    rows = [_row(conversation_id="c1", hr_name="A", company="A", latest_message="m1")]
    scripts = {
        "m1": PlannedChatAction(
            action=ChatAction.REPLY, reason="ok", reply_text="好的"
        ),
    }
    service, events, connector = _build_service(rows=rows, scripts=scripts)

    def _dryrun_reply(**_: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "source": "fake",
            "status": "logged_only",
            "error": None,
        }

    connector.reply_conversation = _dryrun_reply  # type: ignore[assignment]

    service.run_process(
        max_conversations=10,
        unread_only=True,
        profile_id="default",
        notify_on_escalate=False,
        fetch_latest_hr=True,
        auto_execute=True,
        chat_tab="未读",
        confirm_execute=True,
    )

    auto_events = [e for e in events if e["stage"] == "auto_execute"]
    assert len(auto_events) == 1
    payload = auto_events[0]["payload"]
    assert payload["status"] == "logged_only"
    assert payload["ok"] is False


def test_manual_required_status_is_not_laundered_into_sent() -> None:
    rows = [_row(conversation_id="c1", hr_name="A", company="A", latest_message="m1")]
    scripts = {
        "m1": PlannedChatAction(
            action=ChatAction.SEND_RESUME, reason="hr asked for resume"
        ),
    }
    service, events, connector = _build_service(rows=rows, scripts=scripts)

    def _manual_required(**_: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "source": "fake",
            "status": "manual_required",
            "error": "resume attachment executor is not configured",
        }

    connector.send_resume_attachment = _manual_required  # type: ignore[assignment]

    service.run_process(
        max_conversations=10,
        unread_only=True,
        profile_id="default",
        notify_on_escalate=False,
        fetch_latest_hr=True,
        auto_execute=True,
        chat_tab="未读",
        confirm_execute=True,
    )

    auto_events = [e for e in events if e["stage"] == "auto_execute"]
    assert len(auto_events) == 1
    payload = auto_events[0]["payload"]
    assert payload["status"] == "manual_required"
    assert payload["ok"] is False


def test_run_process_returns_action_report_with_manual_chat_link() -> None:
    rows = [_row(conversation_id="c1", hr_name="A", company="A", latest_message="m1")]
    rows[0]["conversation_url"] = "https://www.zhipin.com/web/geek/chat?conversationId=abc"
    scripts = {
        "m1": PlannedChatAction(
            action=ChatAction.ESCALATE,
            reason="需要用户提供实时信息",
        ),
    }
    service, _events, _connector = _build_service(rows=rows, scripts=scripts)
    result = service.run_process(
        max_conversations=10,
        unread_only=True,
        profile_id="default",
        notify_on_escalate=True,
        fetch_latest_hr=True,
        auto_execute=True,
        chat_tab="未读",
        confirm_execute=True,
    )
    report = result[ACTION_REPORT_KEY]
    assert report["action"] == "job.chat"
    assert report["metrics"]["manual_required"] == 1
    assert "会话链接" in report["summary"]
    detail = report["details"][0]
    assert detail["status"] == "failed"
    assert detail["url"] == "https://www.zhipin.com/web/geek/chat?conversationId=abc"
    assert detail["extras"]["manual_required"] is True
