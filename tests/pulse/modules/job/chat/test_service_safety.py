"""SafetyPlane v2 对 JobChatService 的接入契约.

覆盖三条 side-effect 路径在 ``PULSE_SAFETY_PLANE=enforce`` 下的行为:

* ``_execute_reply`` / ``_execute_send_resume`` / ``_execute_card``
  命中 ``Decision.ask`` 时:
  1. **不** 调 connector 的 side-effect 方法
  2. 调 ``connector.mark_processed`` 把对话从未读踢出
  3. 调 ``SuspendedTaskStore.create`` 写挂起任务
  4. 调 ``Notifier.send`` 把 ask 文案推给用户

* ``_execute_reply`` 在 ``session_approvals`` 有对应 token 时放行, 真调
  connector + mark_processed, 不写 SuspendedTask.

* SafetyPlane mode="off" / 未 attach 时 _execute_* 走 legacy 直发路径,
  不跑 policy gate.

这里**不**重写 policy 本身的分支覆盖(归 ``tests/pulse/core/safety/
test_policies.py``), 只验 "service 在 enforce 下把 Decision 三种 kind
分派到正确副作用序列".
"""

from __future__ import annotations

from typing import Any

import pytest

from pulse.core.notify.notifier import Notification
from pulse.core.safety import (
    AskRequest,
    Decision,
    Intent,
    PermissionContext,
    ResumeHandle,
    SAFETY_PLANE_ENFORCE,
    SuspendedTask,
    SuspendedTaskStore,
)
from pulse.modules.job._connectors.base import JobPlatformConnector
from pulse.modules.job.chat.planner import HrMessagePlanner, PlannedChatAction
from pulse.modules.job.chat.repository import ChatRepository
from pulse.modules.job.chat.service import ChatPolicy, JobChatService
from pulse.modules.job.shared.enums import CardType


# ── Fakes ───────────────────────────────────────────────────────────────


class _FakeConnector(JobPlatformConnector):
    def __init__(self) -> None:
        self.reply_calls: list[dict[str, Any]] = []
        self.resume_calls: list[dict[str, Any]] = []
        self.card_calls: list[dict[str, Any]] = []
        self.mark_calls: list[dict[str, Any]] = []

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
        return {"items": [], "source": self.provider_name, "errors": []}

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

    def send_resume_attachment(
        self,
        *,
        conversation_id: str,
        resume_profile_id: str,
        conversation_hint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.resume_calls.append(
            {
                "conversation_id": conversation_id,
                "resume_profile_id": resume_profile_id,
            }
        )
        return {"ok": True, "source": self.provider_name, "status": "sent"}

    def click_conversation_card(
        self,
        *,
        conversation_id: str,
        card_id: str,
        card_type: str,
        action: str,
    ) -> dict[str, Any]:
        self.card_calls.append(
            {
                "conversation_id": conversation_id,
                "card_id": card_id,
                "card_type": card_type,
                "action": action,
            }
        )
        return {"ok": True, "source": self.provider_name, "status": "clicked"}

    def mark_processed(
        self,
        *,
        conversation_id: str,
        run_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        self.mark_calls.append(
            {"conversation_id": conversation_id, "run_id": run_id, "note": note}
        )
        return {"ok": True, "source": self.provider_name, "status": "noop"}


class _FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[Notification] = []

    def send(self, message: Notification) -> None:
        self.messages.append(message)


class _InMemoryStore:
    """足够测试的 SuspendedTaskStore 最小实现.

    之所以不用真 ``WorkspaceSuspendedTaskStore``:
    * 那个要求一个 FactsStore 协议实现和 EventPublisher, 在 chat 测试里
      接入 WorkspaceMemory 会把依赖面拉到 DB 层.
    * 本文件只验 service → store 的调用序列, 不校验 store 自己的持久化
      语义 (后者归 ``tests/pulse/core/safety/test_suspended.py``).
    """

    def __init__(self) -> None:
        self.created: list[SuspendedTask] = []

    def create(
        self,
        *,
        task_id: str,
        module: str,
        trace_id: str,
        workspace_id: str,
        intent: Intent,
        ask_request: AskRequest,
        origin_rule_id: str | None = None,
        origin_decision_reason: str = "",
    ) -> SuspendedTask:
        from datetime import datetime, timezone

        task = SuspendedTask(
            task_id=task_id,
            module=module,
            trace_id=trace_id,
            workspace_id=workspace_id,
            suspended_at=datetime.now(timezone.utc),
            ask_request=ask_request,
            original_intent=intent,
            origin_rule_id=origin_rule_id,
            origin_decision_reason=origin_decision_reason,
        )
        self.created.append(task)
        return task

    def get(self, *, workspace_id: str, task_id: str) -> SuspendedTask | None:
        for task in self.created:
            if task.workspace_id == workspace_id and task.task_id == task_id:
                return task
        return None

    def list_awaiting(self, *, workspace_id: str) -> list[SuspendedTask]:
        return [t for t in self.created if t.workspace_id == workspace_id]

    def resolve(self, **_: Any) -> SuspendedTask:  # pragma: no cover
        raise NotImplementedError

    def timeout(self, **_: Any) -> SuspendedTask:  # pragma: no cover
        raise NotImplementedError

    def deny(self, **_: Any) -> SuspendedTask:  # pragma: no cover
        raise NotImplementedError


# ── Helpers ────────────────────────────────────────────────────────────


class _ScriptedPlanner(HrMessagePlanner):
    def __init__(self) -> None:
        pass

    def plan(self, *, message: str, **_: Any) -> PlannedChatAction:  # pragma: no cover
        raise NotImplementedError


def _build_service(
    *,
    attach_safety: bool = True,
    mode: str = SAFETY_PLANE_ENFORCE,
) -> tuple[JobChatService, _FakeConnector, _FakeNotifier, _InMemoryStore]:
    connector = _FakeConnector()
    notifier = _FakeNotifier()
    store = _InMemoryStore()

    def _emit(**_: Any) -> str:
        return "trace-test"

    service = JobChatService(
        connector=connector,
        repository=ChatRepository(engine=None),
        planner=_ScriptedPlanner(),
        policy=ChatPolicy(
            default_profile_id="default",
            auto_execute=True,
            hitl_required=False,
        ),
        notifier=notifier,
        emit_stage_event=_emit,
    )
    if attach_safety:
        service.attach_safety_plane(
            suspended_store=store,
            workspace_id="ws-test",
            mode=mode,
        )
    # verify the SuspendedTaskStore Protocol is honored by the fake —
    # catches accidental signature drift at test import time.
    assert isinstance(store, SuspendedTaskStore)
    return service, connector, notifier, store


# ── _execute_reply ─────────────────────────────────────────────────────


class TestExecuteReplyAsk:
    """reply_policy 默认返 ask → 挂起 + mark + notify, 不调 reply_conversation."""

    def test_ask_path_suspends_marks_and_notifies(self) -> None:
        service, connector, notifier, store = _build_service()

        result = service._execute_reply(
            conversation_id="conv-1",
            reply_text="您好,方便,几点?",
            profile_id="default",
            run_id="run-1",
            note=None,
            conversation_hint={
                "hr_name": "腾讯-张三",
                "latest_hr_message": "今天下午方便吗?",
            },
        )

        assert result["status"] == "suspended"
        assert result["needs_confirmation"] is True
        assert result["ok"] is False
        assert result["safety"]["kind"] == "ask"

        # connector.reply_conversation 绝不能被调用 —— 外部 side-effect 不能
        # 发生, 这是整个 SafetyPlane 存在的原因.
        assert connector.reply_calls == []

        # 但 mark_processed 必须被调用, 否则下轮 patrol 会再次 plan+ask,
        # 造成 "ask 轰炸" 永动机.
        assert len(connector.mark_calls) == 1
        assert connector.mark_calls[0]["conversation_id"] == "conv-1"

        # SuspendedTask 已经写入 store.
        assert len(store.created) == 1
        task = store.created[0]
        assert task.module == "job_chat"
        assert task.original_intent.name == "job.chat.reply"
        # workspace_id 用的是 attach_safety_plane 注入值, 不是 conversation_id.
        assert task.workspace_id == "ws-test"

        # 用户被通过 Notifier 收到了 ask 文案.
        assert len(notifier.messages) == 1
        msg = notifier.messages[0]
        assert msg.level == "warn"
        assert "腾讯-张三" in msg.content or "HR" in msg.content


class TestExecuteReplyAllowOnSessionApproval:
    """用户此前在 session 里授权过同一 draft → 直接放行真发."""

    def test_allow_path_calls_connector_and_skips_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # session_approvals 注入方式: attach_safety_plane 只接注 store 和
        # workspace/mode; 要把"用户已授权"灌进 PermissionContext, 最实在的
        # 做法是 monkeypatch _run_policy 让它返回 Decision.allow.
        # 这不是在测 policy 函数 (那是 test_policies.py 的活), 而是在测
        # service 看到 allow 时的副作用序列.
        service, connector, notifier, store = _build_service()

        def _always_allow(
            *, policy_fn, intent, trace_id
        ) -> Decision:  # type: ignore[override]
            return Decision.allow(
                reason="session_approved", rule_id="job_chat.reply.session_approval"
            )

        monkeypatch.setattr(service, "_run_policy", _always_allow)

        result = service._execute_reply(
            conversation_id="conv-1",
            reply_text="您好,方便,几点?",
            profile_id="default",
            run_id="run-1",
            note=None,
            conversation_hint={"hr_name": "HR-A", "latest_hr_message": "?"},
        )

        # 真发路径: connector.reply_conversation 被调 + mark_processed 也被调.
        assert len(connector.reply_calls) == 1
        assert len(connector.mark_calls) == 1
        # 未产生挂起任务 / 未发 Ask 通知.
        assert store.created == []
        assert notifier.messages == []
        # 连接器返回 sent → 最终 ok/status 真值透传.
        assert result["ok"] is True
        assert result["status"] == "sent"


class TestExecuteReplyDeny:
    """policy 返 deny → 不触达 connector, 不 mark_processed, 保留未读."""

    def test_deny_path_does_not_call_connector(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, connector, notifier, store = _build_service()

        def _always_deny(*, policy_fn, intent, trace_id) -> Decision:
            return Decision.deny(
                reason="test_block",
                deny_code="job_chat.reply.denied_for_test",
                rule_id="job_chat.reply.test_deny",
            )

        monkeypatch.setattr(service, "_run_policy", _always_deny)

        result = service._execute_reply(
            conversation_id="conv-1",
            reply_text="x",
            profile_id="default",
            run_id="run-1",
            note=None,
            conversation_hint={},
        )

        assert result["status"] == "denied"
        assert result["ok"] is False
        assert result["safety"]["kind"] == "deny"
        # connector 任何一条都不该被调 —— deny 意味着"不做, 也不踢未读".
        assert connector.reply_calls == []
        assert connector.mark_calls == []
        assert store.created == []
        assert notifier.messages == []


class TestPolicyFailureFailClosed:
    def test_policy_exception_suspends_instead_of_sending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import pulse.modules.job.chat.service as service_module

        service, connector, notifier, store = _build_service()

        def _boom(*_: Any, **__: Any) -> Decision:
            raise RuntimeError("policy bug")

        monkeypatch.setattr(service_module, "reply_policy", _boom)

        result = service._execute_reply(
            conversation_id="conv-policy-fail",
            reply_text="这条不能直接发",
            profile_id="default",
            run_id="run-policy-fail",
            note=None,
            conversation_hint={"hr_name": "HR-X"},
        )

        assert result["status"] == "suspended"
        assert result["safety"]["rule_id"] == "job_chat.policy.exception"
        assert connector.reply_calls == []
        assert len(connector.mark_calls) == 1
        assert len(store.created) == 1
        assert "自动授权检查失败" in notifier.messages[0].content


# ── SafetyPlane off / not attached ─────────────────────────────────────


class TestSafetyPlaneOffBypassesPolicy:
    """mode=off 或未 attach → _execute_reply 不跑 policy, 直接调 connector."""

    def test_not_attached_bypasses_gate(self) -> None:
        service, connector, notifier, store = _build_service(
            attach_safety=False
        )

        result = service._execute_reply(
            conversation_id="conv-1",
            reply_text="hi",
            profile_id="default",
            run_id="run-1",
            note=None,
            conversation_hint={},
        )

        assert result["ok"] is True
        assert result["status"] == "sent"
        assert len(connector.reply_calls) == 1
        assert len(connector.mark_calls) == 1
        assert store.created == []
        assert notifier.messages == []

    def test_off_mode_bypasses_gate(self) -> None:
        service, connector, _notifier, store = _build_service(mode="off")

        result = service._execute_reply(
            conversation_id="conv-1",
            reply_text="hi",
            profile_id="default",
            run_id="run-1",
            note=None,
            conversation_hint={},
        )

        assert result["ok"] is True
        assert len(connector.reply_calls) == 1
        assert store.created == []


# ── _execute_send_resume ask 分支 ──────────────────────────────────────


class TestExecuteSendResumeAsk:
    def test_ask_path_does_not_send_attachment(self) -> None:
        service, connector, notifier, store = _build_service()

        result = service._execute_send_resume(
            conversation_id="conv-1",
            reply_text=None,
            profile_id="default",
            run_id="run-1",
            note=None,
            conversation_hint={"hr_name": "腾讯-李四", "hr_id": "hr-xyz"},
        )

        assert result["status"] == "suspended"
        assert connector.resume_calls == []
        assert len(connector.mark_calls) == 1
        assert len(store.created) == 1
        assert store.created[0].original_intent.name == "job.chat.send_resume"
        assert len(notifier.messages) == 1


# ── _execute_card ask 分支 ─────────────────────────────────────────────


class TestExecuteCardAsk:
    def test_card_action_always_asks(self) -> None:
        service, connector, notifier, store = _build_service()

        result = service._execute_card(
            action=__import__(
                "pulse.modules.job.shared.enums", fromlist=["CardAction"]
            ).CardAction.ACCEPT,
            conversation_id="conv-1",
            reply_text=None,
            profile_id="default",
            run_id="run-1",
            note=None,
            conversation_hint={"card_title": "Python 面试"},
            card_id="card-xyz",
            card_type=CardType.INTERVIEW_INVITE.value,
        )

        assert result["status"] == "suspended"
        assert connector.card_calls == []
        assert len(connector.mark_calls) == 1
        assert len(store.created) == 1
        assert (
            store.created[0].original_intent.name
            == "job.chat.card.accept"
        )
        # 用户文案里应出现可读的 "面试邀请" 而不是 "interview_invite" slug.
        msg_text = notifier.messages[0].content
        assert "面试邀请" in msg_text


# ── policy → Intent args 映射 (签名层面的回归) ─────────────────────────


class TestPolicyIntentArgsContract:
    """确保 service 构造的 Intent.args 键名与 policies.py 约定对齐.

    任何一个 policy 要读的 key 若 service 没塞, policy 拿 "" 走默认分支,
    ask 文案会退化成 "HR" 空模板 —— 这类 drift 在集成态才会冒出来.
    """

    def _record_intent(
        self, monkeypatch: pytest.MonkeyPatch, service: JobChatService
    ) -> list[Intent]:
        captured: list[Intent] = []

        def _spy(*, policy_fn, intent, trace_id) -> Decision:
            captured.append(intent)
            # 返回 allow 让后续不跑 ask/suspend, 减少噪声.
            return Decision.allow(reason="spy", rule_id="test.spy")

        monkeypatch.setattr(service, "_run_policy", _spy)
        return captured

    def test_reply_intent_carries_all_required_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _c, _n, _s = _build_service()
        captured = self._record_intent(monkeypatch, service)

        service._execute_reply(
            conversation_id="conv-1",
            reply_text="好的",
            profile_id="default",
            run_id="run-1",
            note=None,
            conversation_hint={"hr_name": "A", "latest_hr_message": "ping"},
        )
        assert len(captured) == 1
        intent = captured[0]
        assert intent.name == "job.chat.reply"
        for key in (
            "conversation_id",
            "hr_label",
            "hr_message",
            "draft_text",
            "draft_hash",
        ):
            assert key in intent.args, f"reply intent missing {key!r}"

    def test_send_resume_intent_carries_hr_id_for_session_scoping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service, _c, _n, _s = _build_service()
        captured = self._record_intent(monkeypatch, service)

        service._execute_send_resume(
            conversation_id="conv-1",
            reply_text=None,
            profile_id="default",
            run_id="run-1",
            note=None,
            conversation_hint={"hr_name": "A", "hr_id": "hr-xyz"},
        )
        assert captured[0].args["hr_id"] == "hr-xyz"
        # hr_id 缺失时退到 conversation_id, 避免 session_approved 分支永远
        # 失效 (没有 hr_id 就永远 ask).
        service._execute_send_resume(
            conversation_id="conv-2",
            reply_text=None,
            profile_id="default",
            run_id="run-1",
            note=None,
            conversation_hint={"hr_name": "A"},
        )
        assert captured[-1].args["hr_id"] == "conv-2"


# ── PermissionContext 结构校验 ─────────────────────────────────────────


class TestPermissionContextShape:
    def test_context_fields_are_wired_from_service_attach(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """service._run_policy 构造的 PermissionContext 必须带对齐 module/
        task_id/trace_id; 不能出现空串或 None."""
        service, _c, _n, _s = _build_service()
        captured_ctx: list[PermissionContext] = []

        def _spy_policy_fn(intent: Intent, ctx: PermissionContext) -> Decision:
            captured_ctx.append(ctx)
            return Decision.allow(reason="spy", rule_id="test.spy")

        # 直接替换内部 reply_policy 引用做不到 (service 里 import-time bound);
        # 走 _run_policy 替身, 让它真的跑 policy_fn 参数(即本函数).
        original = service._run_policy

        def _call_real(*, policy_fn, intent, trace_id):
            # 把替身 policy 喂给 _run_policy, 让它构造真实 ctx.
            return original(
                policy_fn=_spy_policy_fn, intent=intent, trace_id=trace_id
            )

        monkeypatch.setattr(service, "_run_policy", _call_real)

        service._execute_reply(
            conversation_id="conv-42",
            reply_text="hi",
            profile_id="default",
            run_id="run-42",
            note=None,
            conversation_hint={},
        )

        assert len(captured_ctx) == 1
        ctx = captured_ctx[0]
        assert ctx.module == "job_chat"
        assert ctx.task_id.endswith("conv-42") or "conv-42" in ctx.task_id
        assert ctx.trace_id == "run-42"
        # profile_view 必须是 Mapping, session_approvals 必须是 frozenset —
        # 这两类型约束在 PermissionContext.__post_init__ 也有, 但重复 assert
        # 一次防止 service 未来传错类型时错把 "bug 在哪里" 归到 safety 本体.
        from collections.abc import Mapping

        assert isinstance(ctx.profile_view, Mapping)
        assert isinstance(ctx.session_approvals, frozenset)


# ── Resume → Re-execute ────────────────────────────────────────────────


class TestResumedTaskExecutor:
    """用户答 y/n 后, service.resumed_task_executor 必须把原 intent 就地跑完.

    不走 policy gate —— 用户的 "y" 就是 policy 之前要求的那个 Ask 答复,
    再包回 policy 会陷入死循环. 契约: 同步返回 ResumedExecution, 不抛,
    失败包成 status="failed" 而非抛 Exception.
    """

    def _suspend_reply(self) -> tuple[JobChatService, _FakeConnector, SuspendedTask]:
        service, connector, _notifier, store = _build_service()
        service._execute_reply(
            conversation_id="conv-R",
            reply_text="您好, 明天上午方便。",
            profile_id="default",
            run_id="run-R",
            note=None,
            conversation_hint={
                "hr_name": "腾讯-张三",
                "company": "腾讯",
                "latest_hr_message": "今天下午方便吗?",
            },
        )
        assert len(store.created) == 1
        # 清空挂起时的那条 mark 记录, 让后续断言只看 resume 路径.
        connector.mark_calls.clear()
        return service, connector, store.created[0]

    def test_approval_reexecutes_reply_and_sends_to_hr(self) -> None:
        service, connector, task = self._suspend_reply()
        result = service.resumed_task_executor(task=task, user_answer="y")
        assert result.status == "executed"
        assert result.ok is True
        assert "张三" in result.summary
        # 真发了: reply_conversation 被调, 且草稿原文透传.
        assert len(connector.reply_calls) == 1
        assert connector.reply_calls[0]["conversation_id"] == "conv-R"
        assert connector.reply_calls[0]["reply_text"] == "您好, 明天上午方便。"
        # resume 路径也要 mark_processed (审计可追溯), 且 run_id 前缀区分.
        assert len(connector.mark_calls) == 1
        assert connector.mark_calls[0]["run_id"].startswith("resume-")

    def test_decline_does_not_send(self) -> None:
        service, connector, task = self._suspend_reply()
        result = service.resumed_task_executor(task=task, user_answer="n")
        assert result.status == "declined"
        assert result.ok is True
        # 拒绝分支: 不触达 connector 的 reply / mark, 用户明确说别发.
        assert connector.reply_calls == []
        assert connector.mark_calls == []

    def test_ambiguous_answer_conservatively_does_not_send(self) -> None:
        # "又不是 y 又不是 n" 一律保守, 绝不偷偷把 HR 回复发出去.
        service, connector, task = self._suspend_reply()
        result = service.resumed_task_executor(
            task=task, user_answer="嗯...我再想想"
        )
        assert result.status == "undetermined"
        assert result.ok is False
        assert connector.reply_calls == []
        # summary 必须解释后续该怎么做 (用户不是被吞答复的).
        assert "不发送" in result.summary or "新草稿" in result.summary

    def test_reexecute_send_resume_uses_preserved_profile_id(self) -> None:
        service, connector, _notifier, store = _build_service()
        service._execute_send_resume(
            conversation_id="conv-S",
            reply_text=None,
            profile_id="my-profile",
            run_id="run-S",
            note=None,
            conversation_hint={"hr_name": "HR-Z", "hr_id": "hr-abc"},
        )
        task = store.created[0]
        connector.mark_calls.clear()

        result = service.resumed_task_executor(task=task, user_answer="确认")
        assert result.status == "executed"
        assert len(connector.resume_calls) == 1
        # profile_id 从 intent.args 恢复, 而不是从 service 的内存状态拿 —
        # 保证即便服务重启后挂起任务持久化拉回来, resume 仍能用原 profile.
        assert connector.resume_calls[0]["resume_profile_id"] == "my-profile"

    def test_reexecute_card_restores_card_id_and_action(self) -> None:
        from pulse.modules.job.shared.enums import CardAction

        service, connector, _notifier, store = _build_service()
        service._execute_card(
            action=CardAction.ACCEPT,
            conversation_id="conv-C",
            reply_text=None,
            profile_id="default",
            run_id="run-C",
            note=None,
            conversation_hint={"card_title": "Python 面试"},
            card_id="card-777",
            card_type=CardType.INTERVIEW_INVITE.value,
        )
        task = store.created[0]
        connector.mark_calls.clear()

        result = service.resumed_task_executor(task=task, user_answer="好的")
        assert result.status == "executed"
        assert len(connector.card_calls) == 1
        call = connector.card_calls[0]
        assert call["card_id"] == "card-777"
        assert call["card_type"] == CardType.INTERVIEW_INVITE.value
        assert call["action"] == CardAction.ACCEPT.value
