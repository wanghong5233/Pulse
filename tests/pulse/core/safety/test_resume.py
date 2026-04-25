"""SafetyPlane · Resume 回路合约测试.

验证:

1. ``render_ask_for_im`` 结构与字段组合 (question / draft / task_id / timeout).
2. ``build_resume_payload`` schema 白名单 / 空文本拒收 / naive datetime 拒收.
3. ``try_resume_suspended_turn`` 全路径:
   * no_awaiting (workspace 空 / store 空 / 竞态已清)
   * resolved (正常; store.resolve 被正确调用)
   * schema_rejected (payload_schema 未支持, 明文错误回执)
   * task_terminal (TaskAlreadyTerminalError)
   * store_error (list_awaiting / resolve 抛异常都兜住)
   * 多 awaiting 取最早挂起的那条.
4. ``ResumeOutcome.should_reply`` / ``should_skip_brain`` 语义不变量.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import pytest

from pulse.core.safety import (
    AskRequest,
    DEFAULT_PAYLOAD_SCHEMA,
    Decision,
    Intent,
    ResumedExecution,
    ResumeHandle,
    ResumeOutcome,
    SuspendedTask,
    TaskAlreadyTerminalError,
    TaskNotFoundError,
    build_resume_payload,
    render_ask_for_im,
    try_resume_suspended_turn,
)


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────


def _intent(name: str = "job_chat.reply.send") -> Intent:
    return Intent(
        kind="mutation",
        name=name,
        args={"conversation_id": "cv_1"},
        evidence_keys=(),
    )


def _resume_handle(task_id: str = "tsk_001", schema: str = DEFAULT_PAYLOAD_SCHEMA) -> ResumeHandle:
    return ResumeHandle(
        task_id=task_id,
        module="job_chat",
        intent="system.task.resume",
        payload_schema=schema,
    )


def _ask(
    *,
    question: str = "HR 问:下周二哪个时段面试?",
    draft: str | None = "我周二 14:00 可以",
    timeout: int = 3600,
    task_id: str = "tsk_001",
    schema: str = DEFAULT_PAYLOAD_SCHEMA,
) -> AskRequest:
    return AskRequest(
        question=question,
        draft=draft,
        resume_handle=_resume_handle(task_id=task_id, schema=schema),
        timeout_seconds=timeout,
    )


def _task(
    *,
    task_id: str = "tsk_001",
    workspace_id: str = "wechat-work:u_alice",
    suspended_at: datetime | None = None,
    ask: AskRequest | None = None,
    intent: Intent | None = None,
    status: str = "awaiting_user",
) -> SuspendedTask:
    suspended_at = suspended_at or datetime(2026, 4, 24, 9, 0, tzinfo=timezone.utc)
    return SuspendedTask(
        task_id=task_id,
        module="job_chat",
        trace_id=f"trace-{task_id}",
        workspace_id=workspace_id,
        suspended_at=suspended_at,
        ask_request=ask or _ask(task_id=task_id),
        original_intent=intent or _intent(),
        status=status,  # type: ignore[arg-type]
        resolved_at=(
            None if status == "awaiting_user" else suspended_at + timedelta(minutes=1)
        ),
    )


@dataclass
class FakeStore:
    """SuspendedTaskStore 的最小 fake —— 只暴露 B.2 用到的方法.

    不继承 WorkspaceSuspendedTaskStore, 避免把其持久化副作用牵进来;
    只做断言层需要的记录.
    """

    awaiting: list[SuspendedTask] = field(default_factory=list)
    resolve_calls: list[tuple[str, str, Mapping[str, Any]]] = field(default_factory=list)
    resolved_value: SuspendedTask | None = None
    list_exc: BaseException | None = None
    resolve_exc: BaseException | None = None

    def list_awaiting(self, *, workspace_id: str) -> list[SuspendedTask]:
        if self.list_exc:
            raise self.list_exc
        return [t for t in self.awaiting if t.workspace_id == workspace_id]

    def resolve(
        self,
        *,
        workspace_id: str,
        task_id: str,
        payload: Mapping[str, Any],
    ) -> SuspendedTask:
        self.resolve_calls.append((workspace_id, task_id, dict(payload)))
        if self.resolve_exc:
            raise self.resolve_exc
        if self.resolved_value is not None:
            return self.resolved_value
        matched = next(
            (t for t in self.awaiting if t.task_id == task_id and t.workspace_id == workspace_id),
            None,
        )
        if matched is None:
            raise TaskNotFoundError(task_id)
        return SuspendedTask(
            task_id=matched.task_id,
            module=matched.module,
            trace_id=matched.trace_id,
            workspace_id=matched.workspace_id,
            suspended_at=matched.suspended_at,
            ask_request=matched.ask_request,
            original_intent=matched.original_intent,
            status="resumed",
            resolved_at=matched.suspended_at + timedelta(minutes=1),
            resolution_payload=dict(payload),
        )

    # 其它接口 (create / get / timeout / deny) B.2 本路径不调用, 留空.
    def create(self, **_: Any) -> SuspendedTask:  # pragma: no cover
        raise NotImplementedError

    def get(self, *, workspace_id: str, task_id: str) -> SuspendedTask | None:  # pragma: no cover
        return None

    def timeout(self, *, workspace_id: str, task_id: str) -> SuspendedTask:  # pragma: no cover
        raise NotImplementedError

    def deny(self, *, workspace_id: str, task_id: str, reason: str) -> SuspendedTask:  # pragma: no cover
        raise NotImplementedError


# ──────────────────────────────────────────────────────────────
# render_ask_for_im
# ──────────────────────────────────────────────────────────────


class TestRenderAskForIm:
    def test_includes_question_draft_timeout_task_id(self) -> None:
        text = render_ask_for_im(_ask())
        assert "[需要你确认]" in text
        assert "HR 问:下周二哪个时段面试?" in text
        assert "建议回复:" in text
        assert "我周二 14:00 可以" in text
        assert "超时: 3600 秒" in text
        assert "任务 ID: tsk_001" in text

    def test_skips_draft_when_absent(self) -> None:
        text = render_ask_for_im(_ask(draft=None))
        assert "建议回复:" not in text
        assert "[需要你确认]" in text

    def test_include_task_id_false_hides_task_id(self) -> None:
        text = render_ask_for_im(_ask(), include_task_id=False)
        assert "任务 ID" not in text

    def test_rejects_non_ask_request(self) -> None:
        with pytest.raises(TypeError):
            render_ask_for_im({"question": "no"})  # type: ignore[arg-type]

    def test_channel_arg_accepted_but_unused(self) -> None:
        # 签名预留但当前不改输出; 这条测试起防回归作用.
        text_wechat = render_ask_for_im(_ask(), channel="wechat-work")
        text_cli = render_ask_for_im(_ask(), channel="cli")
        assert text_wechat == text_cli


# ──────────────────────────────────────────────────────────────
# build_resume_payload
# ──────────────────────────────────────────────────────────────


class TestBuildResumePayload:
    def test_text_answer_roundtrip(self) -> None:
        ts = datetime(2026, 4, 24, 10, 0, tzinfo=timezone.utc)
        payload = build_resume_payload(
            user_text="周二下午两点",
            schema=DEFAULT_PAYLOAD_SCHEMA,
            received_at=ts,
        )
        assert payload == {
            "schema": "safety.v1.user_answer",
            "answer": "周二下午两点",
            "received_at": ts.isoformat(),
        }

    def test_default_received_at_is_utc(self) -> None:
        payload = build_resume_payload(
            user_text="ok",
            schema=DEFAULT_PAYLOAD_SCHEMA,
        )
        parsed = datetime.fromisoformat(payload["received_at"])
        assert parsed.tzinfo is not None

    def test_preserves_whitespace_in_answer(self) -> None:
        # 审计需要保留用户输入的原始形态.
        payload = build_resume_payload(
            user_text="  好  ",
            schema=DEFAULT_PAYLOAD_SCHEMA,
        )
        assert payload["answer"] == "  好  "

    def test_rejects_empty_text(self) -> None:
        with pytest.raises(ValueError):
            build_resume_payload(user_text="", schema=DEFAULT_PAYLOAD_SCHEMA)

    def test_rejects_whitespace_only_text(self) -> None:
        with pytest.raises(ValueError):
            build_resume_payload(user_text="   \n", schema=DEFAULT_PAYLOAD_SCHEMA)

    def test_rejects_unknown_schema(self) -> None:
        with pytest.raises(ValueError):
            build_resume_payload(
                user_text="ok",
                schema="job_chat.interview_time_v1",
            )

    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError):
            build_resume_payload(
                user_text="ok",
                schema=DEFAULT_PAYLOAD_SCHEMA,
                received_at=datetime(2026, 4, 24, 10, 0),
            )


# ──────────────────────────────────────────────────────────────
# try_resume_suspended_turn
# ──────────────────────────────────────────────────────────────


class TestTryResumeSuspendedTurn:
    def test_no_awaiting_when_workspace_empty(self) -> None:
        store = FakeStore()
        outcome = try_resume_suspended_turn(
            store=store,
            workspace_id="wechat-work:u_alice",
            user_text="周二下午两点",
        )
        assert outcome.kind == "no_awaiting"
        assert outcome.should_skip_brain is False
        assert outcome.should_reply is False
        assert store.resolve_calls == []

    def test_no_awaiting_when_workspace_id_missing(self) -> None:
        # 防御性兜底: workspace_id 解析失败时视为 no_awaiting, 不打断链路.
        store = FakeStore(awaiting=[_task()])
        outcome = try_resume_suspended_turn(
            store=store,
            workspace_id="",
            user_text="ok",
        )
        assert outcome.kind == "no_awaiting"
        assert store.resolve_calls == []

    def test_resolved_happy_path(self) -> None:
        task = _task()
        store = FakeStore(awaiting=[task])
        outcome = try_resume_suspended_turn(
            store=store,
            workspace_id="wechat-work:u_alice",
            user_text="周二下午两点",
        )
        assert outcome.kind == "resolved"
        assert outcome.should_skip_brain is True
        assert outcome.should_reply is True
        assert outcome.task is not None
        assert outcome.task.status == "resumed"
        # resolve 被调用一次, 参数完整.
        assert len(store.resolve_calls) == 1
        ws, tid, payload = store.resolve_calls[0]
        assert (ws, tid) == ("wechat-work:u_alice", "tsk_001")
        assert payload["schema"] == "safety.v1.user_answer"
        assert payload["answer"] == "周二下午两点"
        # 用户回复语气: "收到 + 继续跟进", 不承诺执行结果.
        assert "收到" in outcome.user_reply
        assert "跟进" in outcome.user_reply

    def test_schema_rejected_replies_with_plain_text(self) -> None:
        # SafetyPlane 不变式: Resume 失败不得悄悄失败.
        task = _task(ask=_ask(schema="job_chat.interview_time_v1"))
        store = FakeStore(awaiting=[task])
        outcome = try_resume_suspended_turn(
            store=store,
            workspace_id="wechat-work:u_alice",
            user_text="周二下午两点",
        )
        assert outcome.kind == "schema_rejected"
        assert outcome.should_skip_brain is True
        assert outcome.should_reply is True
        assert "无法把你的回答" in outcome.user_reply
        # 不能把整个 Exception 栈丢给用户, 但要带 task_id + intent_name.
        assert "tsk_001" in outcome.user_reply
        assert "job_chat.reply.send" in outcome.user_reply
        # 未触发 resolve.
        assert store.resolve_calls == []

    def test_schema_rejected_on_empty_text(self) -> None:
        # 空回复走拒绝分支, 不 resolve (ADR: 空回复不是有效答案).
        task = _task()
        store = FakeStore(awaiting=[task])
        outcome = try_resume_suspended_turn(
            store=store,
            workspace_id="wechat-work:u_alice",
            user_text="",
        )
        assert outcome.kind == "schema_rejected"
        assert store.resolve_calls == []

    def test_task_terminal_race(self) -> None:
        task = _task()
        store = FakeStore(
            awaiting=[task],
            resolve_exc=TaskAlreadyTerminalError("already timed out"),
        )
        outcome = try_resume_suspended_turn(
            store=store,
            workspace_id="wechat-work:u_alice",
            user_text="周二下午两点",
        )
        assert outcome.kind == "task_terminal"
        assert outcome.should_skip_brain is True
        assert "已在此之前结束" in outcome.user_reply
        assert outcome.task == task

    def test_task_not_found_race_falls_back_to_no_awaiting(self) -> None:
        # list_awaiting 返回后, resolve 时任务被并发清掉 — 走 no_awaiting,
        # 让调用方继续跑 Brain (对用户最友好).
        task = _task()
        store = FakeStore(
            awaiting=[task],
            resolve_exc=TaskNotFoundError("cleared"),
        )
        outcome = try_resume_suspended_turn(
            store=store,
            workspace_id="wechat-work:u_alice",
            user_text="周二下午两点",
        )
        assert outcome.kind == "no_awaiting"
        assert outcome.should_skip_brain is False
        assert outcome.should_reply is False

    def test_store_error_on_list_awaiting(self) -> None:
        store = FakeStore(list_exc=RuntimeError("db gone"))
        outcome = try_resume_suspended_turn(
            store=store,
            workspace_id="wechat-work:u_alice",
            user_text="周二下午两点",
        )
        assert outcome.kind == "store_error"
        assert outcome.should_skip_brain is True
        assert outcome.should_reply is True
        assert "存储故障" in outcome.user_reply

    def test_store_error_on_resolve(self) -> None:
        task = _task()
        store = FakeStore(
            awaiting=[task],
            resolve_exc=RuntimeError("db write failed"),
        )
        outcome = try_resume_suspended_turn(
            store=store,
            workspace_id="wechat-work:u_alice",
            user_text="周二下午两点",
        )
        assert outcome.kind == "store_error"
        assert outcome.should_skip_brain is True
        assert "出错" in outcome.user_reply

    def test_multiple_awaiting_requires_disambiguation(self) -> None:
        base = datetime(2026, 4, 24, 9, 0, tzinfo=timezone.utc)
        later = _task(
            task_id="tsk_later",
            suspended_at=base + timedelta(minutes=5),
        )
        earlier = _task(
            task_id="tsk_earlier",
            suspended_at=base,
            ask=_ask(task_id="tsk_earlier"),
        )
        store = FakeStore(awaiting=[later, earlier])
        outcome = try_resume_suspended_turn(
            store=store,
            workspace_id="wechat-work:u_alice",
            user_text="ok",
        )
        assert outcome.kind == "ambiguous"
        assert outcome.should_skip_brain is True
        assert outcome.should_reply is True
        assert "多个待确认任务" in outcome.user_reply
        assert "tsk_earlier" in outcome.user_reply
        assert "tsk_later" in outcome.user_reply
        assert store.resolve_calls == []


# ──────────────────────────────────────────────────────────────
# ResumeOutcome 语义
# ──────────────────────────────────────────────────────────────


class TestResumeOutcome:
    def test_no_awaiting_means_run_brain_without_reply(self) -> None:
        outcome = ResumeOutcome(kind="no_awaiting")
        assert outcome.should_skip_brain is False
        assert outcome.should_reply is False

    def test_every_non_no_awaiting_skips_brain(self) -> None:
        for kind in (
            "resolved",
            "ambiguous",
            "schema_rejected",
            "task_terminal",
            "store_error",
        ):
            outcome = ResumeOutcome(kind=kind, user_reply="x")  # type: ignore[arg-type]
            assert outcome.should_skip_brain is True, kind
            assert outcome.should_reply is True, kind

    def test_should_reply_requires_user_reply(self) -> None:
        # 兜底: 虽然我们当前所有非 no_awaiting 分支都带 user_reply, 但
        # should_reply 必须严格跟随 user_reply 非空, 避免未来代码漏带时
        # 静默吞掉错误信号.
        outcome = ResumeOutcome(kind="resolved", user_reply="")
        assert outcome.should_reply is False


# ──────────────────────────────────────────────────────────────
# Resume → Re-execute (ResumedTaskExecutor)
# ──────────────────────────────────────────────────────────────


class TestResumedTaskExecutor:
    """executor 回调路径的契约测试.

    核心不变式: resolve 永远先于 execute; execute 不抛; execute 的结果
    与 ResumeOutcome 一起透传上去让 server 能写 structured audit.
    """

    def test_executor_invoked_and_summary_reaches_user(self) -> None:
        calls: list[tuple[str, str]] = []

        def executor(*, task: SuspendedTask, user_answer: str) -> ResumedExecution:
            calls.append((task.task_id, user_answer))
            return ResumedExecution(
                status="executed",
                ok=True,
                summary="已把草稿发给 HR 张三。",
            )

        task = _task()
        store = FakeStore(awaiting=[task])
        outcome = try_resume_suspended_turn(
            store=store,
            workspace_id="wechat-work:u_alice",
            user_text="y",
            executors={"job_chat": executor},
        )
        assert outcome.kind == "resolved"
        assert outcome.execution is not None
        assert outcome.execution.status == "executed"
        assert outcome.execution.ok is True
        assert "已把草稿发给 HR 张三" in outcome.user_reply
        assert calls == [("tsk_001", "y")]

    def test_missing_executor_falls_back_with_explain(self) -> None:
        # module 没注册 executor 时走降级路径: task 仍然 resume, 但用户文案
        # 要明确说出 "没自动重发通道", 这是 "Resume → Re-execute 缺口" 的
        # 用户可见承诺的最低下限.
        task = _task()
        store = FakeStore(awaiting=[task])
        outcome = try_resume_suspended_turn(
            store=store,
            workspace_id="wechat-work:u_alice",
            user_text="y",
            executors={},  # 空表, 模拟 module 未注册
        )
        assert outcome.kind == "resolved"
        assert outcome.execution is not None
        assert outcome.execution.status == "executor_missing"
        assert outcome.execution.ok is False
        assert "自动重发通道" in outcome.user_reply

    def test_executor_exception_degrades_to_failed(self) -> None:
        # Executor 违反 "不抛" 契约时, safety core 必须兜住 —— 绝不能让
        # resolve 已完成的 task 在 IM 线程抛未捕获异常把用户答复吞掉.
        def boom(*, task: SuspendedTask, user_answer: str) -> ResumedExecution:
            raise RuntimeError("network exploded")

        task = _task()
        store = FakeStore(awaiting=[task])
        outcome = try_resume_suspended_turn(
            store=store,
            workspace_id="wechat-work:u_alice",
            user_text="y",
            executors={"job_chat": boom},
        )
        assert outcome.kind == "resolved"
        assert outcome.execution is not None
        assert outcome.execution.status == "failed"
        assert outcome.execution.ok is False
        # summary 里要有面向用户的中文反馈, 不能原样丢 Traceback.
        assert "出错" in outcome.user_reply
        assert "network exploded" not in outcome.user_reply

    def test_no_executor_arg_keeps_legacy_behavior(self) -> None:
        # 不传 executors=None (旧调用方兼容) 时, resume 仍应 resolve + 回执,
        # 只是 execution 为 None. 这样即便 server 忘记接入 executors, 业务
        # 也不会崩, 只是退化回 "无自动重发" 老语义.
        task = _task()
        store = FakeStore(awaiting=[task])
        outcome = try_resume_suspended_turn(
            store=store,
            workspace_id="wechat-work:u_alice",
            user_text="y",
        )
        assert outcome.kind == "resolved"
        assert outcome.execution is None
        assert "收到" in outcome.user_reply
