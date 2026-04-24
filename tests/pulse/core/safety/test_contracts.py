"""SafetyPlane 契约原语单测 — ADR-006 §4.1/4.2/4.3.

这里只覆盖 ``core/safety/{intent,decision,context}.py`` 三个模块的契约:

* 构造约束 (哪些组合必须 raise)
* 不可变性 (frozen / MappingProxyType / 拷贝隔离)
* to_dict ↔ from_dict 语义等价 (事件 payload / 审计)
* 便捷构造器 (``Decision.allow`` / ``.deny`` / ``.ask``) 走与直接
  构造同样的不变式闸门, 不能绕过

不覆盖: 规则引擎 / Gate 默认实现 / SuspendedTaskStore —— 那些分别在
Step A.2 / A.3 / A.4 落地后各自的 test 文件里覆盖, 不在这里膨胀。

测试宪法对齐:

* 不做 per-field unit: 每个 class 用 "整体契约" test 聚合多个字段约束
* 不复读常量: 断言围绕"行为"与"不变式" (frozen 抛错 / roundtrip 等价 /
  边界 raise), 不去 ``assert intent.name == "foo"`` 复读自己的 fixture
* 不 mock: 这一层就是纯数据类, 没什么可 mock 的
"""

from __future__ import annotations

import pytest

from pulse.core.safety import (
    AskRequest,
    Decision,
    Intent,
    PermissionContext,
    ResumeHandle,
    VALID_DECISION_KINDS,
    VALID_INTENT_KINDS,
)


# ──────────────────────────────────────────────────────────────
# Fixtures — 最小合法对象, 让其它测试聚焦在 "被测的那一项约束"
# ──────────────────────────────────────────────────────────────


def _handle() -> ResumeHandle:
    return ResumeHandle(
        task_id="task_abc",
        module="job_chat",
        intent="system.task.resume",
        payload_schema="safety.resume.v1",
    )


def _ask() -> AskRequest:
    return AskRequest(
        question="HR 问了你有没有空周四下午面试, 你方便吗?",
        resume_handle=_handle(),
        timeout_seconds=3600,
        draft="我周四下午 3 点可以, 请问面试形式是?",
        context={"hr_name": "李姐", "company": "某公司"},
    )


# ──────────────────────────────────────────────────────────────
# 1. Intent 契约
# ──────────────────────────────────────────────────────────────


def test_intent_valid_construction_and_frozenness() -> None:
    intent = Intent(
        kind="tool_call",
        name="job.chat.send_reply",
        args={"reply_text": "好的", "hr_id": "hr_001"},
        evidence_keys=("profile.base_city",),
    )

    assert intent.kind == "tool_call"
    assert intent.name == "job.chat.send_reply"
    assert intent.evidence_keys == ("profile.base_city",)

    with pytest.raises(Exception):  # frozen dataclass 禁止赋值
        intent.name = "other"  # type: ignore[misc]


def test_intent_rejects_invalid_kind_and_empty_name() -> None:
    with pytest.raises(ValueError):
        Intent(kind="invalid_kind", name="x")  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        Intent(kind="tool_call", name="")

    with pytest.raises(ValueError):
        Intent(kind="tool_call", name="   ")  # whitespace-only


def test_intent_args_are_copied_and_frozen() -> None:
    original_args = {"key": "value"}
    intent = Intent(kind="tool_call", name="t", args=original_args)

    original_args["key"] = "mutated"
    original_args["new"] = "added"
    assert intent.args["key"] == "value"
    assert "new" not in intent.args

    with pytest.raises(TypeError):
        intent.args["key"] = "hacked"  # type: ignore[index]


def test_intent_evidence_keys_normalization_and_validation() -> None:
    # list 输入被 coerce 成 tuple, 但不吞异常式吃错类型
    intent = Intent(
        kind="tool_call",
        name="t",
        evidence_keys=["a", "b"],  # type: ignore[arg-type]
    )
    assert isinstance(intent.evidence_keys, tuple)
    assert intent.evidence_keys == ("a", "b")

    with pytest.raises(ValueError):
        Intent(kind="tool_call", name="t", evidence_keys=("",))  # 空 key


def test_intent_to_dict_roundtrip_preserves_payload() -> None:
    intent = Intent(
        kind="mutation",
        name="game.checkin.execute",
        args={"game": "原神", "account_id": "uid_100"},
        evidence_keys=("profile.game_accounts.原神",),
    )

    payload = intent.to_dict()

    reconstructed = Intent(
        kind=payload["kind"],
        name=payload["name"],
        args=payload["args"],
        evidence_keys=tuple(payload["evidence_keys"]),
    )
    assert reconstructed == intent


def test_intent_kind_literal_whitelist_stays_in_sync() -> None:
    # VALID_INTENT_KINDS 只能用扩枚举的方式增加, 不能靠字符串绕过
    for kind in VALID_INTENT_KINDS:
        Intent(kind=kind, name="probe")  # type: ignore[arg-type]
    assert "allow_everything" not in VALID_INTENT_KINDS  # 防误扩语义


# ──────────────────────────────────────────────────────────────
# 2. ResumeHandle 契约
# ──────────────────────────────────────────────────────────────


def test_resume_handle_rejects_any_empty_field() -> None:
    for empty_field in ("task_id", "module", "intent", "payload_schema"):
        kwargs = {
            "task_id": "t",
            "module": "m",
            "intent": "i",
            "payload_schema": "s",
        }
        kwargs[empty_field] = ""
        with pytest.raises(ValueError):
            ResumeHandle(**kwargs)  # type: ignore[arg-type]


def test_resume_handle_roundtrip() -> None:
    handle = _handle()
    assert ResumeHandle.from_dict(handle.to_dict()) == handle


# ──────────────────────────────────────────────────────────────
# 3. AskRequest 契约
# ──────────────────────────────────────────────────────────────


def test_ask_request_minimal_and_frozenness() -> None:
    # draft / context 可省, 其它必填
    ask = AskRequest(
        question="你周四能面试吗?",
        resume_handle=_handle(),
        timeout_seconds=60,
    )
    assert ask.draft is None
    assert ask.context == {}

    with pytest.raises(Exception):  # frozen
        ask.question = "x"  # type: ignore[misc]


def test_ask_request_rejects_empty_question_and_non_positive_timeout() -> None:
    with pytest.raises(ValueError):
        AskRequest(question="", resume_handle=_handle(), timeout_seconds=60)

    with pytest.raises(ValueError):
        AskRequest(question="q", resume_handle=_handle(), timeout_seconds=0)

    with pytest.raises(ValueError):
        AskRequest(question="q", resume_handle=_handle(), timeout_seconds=-1)


def test_ask_request_rejects_wrong_type_resume_handle() -> None:
    with pytest.raises(TypeError):
        AskRequest(
            question="q",
            resume_handle={"task_id": "t"},  # type: ignore[arg-type]
            timeout_seconds=60,
        )


def test_ask_request_context_is_copied_from_caller() -> None:
    caller_ctx = {"hr_name": "李姐"}
    ask = AskRequest(
        question="q",
        resume_handle=_handle(),
        timeout_seconds=60,
        context=caller_ctx,
    )

    caller_ctx["hr_name"] = "张哥"
    caller_ctx["company"] = "新字段"
    assert ask.context == {"hr_name": "李姐"}


def test_ask_request_roundtrip_preserves_handle_and_optionals() -> None:
    ask = _ask()
    reconstructed = AskRequest.from_dict(ask.to_dict())
    assert reconstructed == ask

    # 最小形态也要能 roundtrip, 确认 optional 字段不是靠 "必须存在" 支撑的
    minimal = AskRequest(
        question="q", resume_handle=_handle(), timeout_seconds=60
    )
    assert AskRequest.from_dict(minimal.to_dict()) == minimal


# ──────────────────────────────────────────────────────────────
# 4. Decision 契约 —— 三 kind 的字段组合约束
# ──────────────────────────────────────────────────────────────


def test_decision_allow_deny_ask_happy_paths_via_factories() -> None:
    allow = Decision.allow(reason="core.default.allow", rule_id="core.default")
    assert allow.kind == "allow"
    assert allow.ask_request is None and allow.deny_code is None

    deny = Decision.deny(
        reason="core.denylist.hit",
        deny_code="core.denylist",
        rule_id="core.denylist",
    )
    assert deny.kind == "deny"
    assert deny.deny_code == "core.denylist"
    assert deny.ask_request is None

    ask = Decision.ask(
        reason="job_chat.interview_time",
        ask_request=_ask(),
        rule_id="job_chat.interview_time",
    )
    assert ask.kind == "ask"
    assert ask.deny_code is None
    assert isinstance(ask.ask_request, AskRequest)


def test_decision_allow_forbids_ask_or_deny_code() -> None:
    with pytest.raises(ValueError):
        Decision(kind="allow", reason="r", deny_code="x")
    with pytest.raises(ValueError):
        Decision(kind="allow", reason="r", ask_request=_ask())


def test_decision_deny_requires_non_empty_deny_code_and_no_ask() -> None:
    with pytest.raises(ValueError):
        Decision(kind="deny", reason="r")  # 缺 deny_code
    with pytest.raises(ValueError):
        Decision(kind="deny", reason="r", deny_code="")
    with pytest.raises(ValueError):
        Decision(kind="deny", reason="r", deny_code="x", ask_request=_ask())


def test_decision_ask_requires_askrequest_and_no_deny_code() -> None:
    with pytest.raises(ValueError):
        Decision(kind="ask", reason="r")  # 缺 ask_request
    with pytest.raises(ValueError):
        Decision(kind="ask", reason="r", ask_request=_ask(), deny_code="x")


def test_decision_rejects_invalid_kind_and_empty_reason() -> None:
    with pytest.raises(ValueError):
        Decision(kind="maybe", reason="r")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Decision(kind="allow", reason="")
    with pytest.raises(ValueError):
        Decision(kind="allow", reason="   ")


def test_decision_kind_whitelist_stays_in_sync() -> None:
    # 校验 Literal / frozenset 两套定义同步, 防止枚举漂移 (例: 有人
    # 在 Literal 里加了 'ask_user' 但忘了同步 frozenset)
    assert VALID_DECISION_KINDS == {"allow", "deny", "ask"}


def test_decision_to_from_dict_roundtrip_for_all_kinds() -> None:
    for decision in (
        Decision.allow(reason="r1", rule_id="rule_a"),
        Decision.deny(reason="r2", deny_code="code_b", rule_id="rule_b"),
        Decision.ask(reason="r3", ask_request=_ask(), rule_id="rule_c"),
    ):
        roundtrip = Decision.from_dict(decision.to_dict())
        assert roundtrip == decision


# ──────────────────────────────────────────────────────────────
# 5. PermissionContext 契约 —— 不变性 + 拷贝隔离
# ──────────────────────────────────────────────────────────────


def _ctx() -> PermissionContext:
    return PermissionContext(
        module="job_chat",
        task_id="job_chat:hr_001",
        trace_id="trace_abc",
        user_id="user_x",
        rules={"core.default": {"decision": "allow"}},
        profile_view={"profile.base_city": "杭州"},
        session_approvals=frozenset({"job_chat.loose_salary"}),
    )


def test_permission_context_valid_construction_and_frozenness() -> None:
    ctx = _ctx()
    assert ctx.module == "job_chat"
    assert "core.default" in ctx.rules
    assert "profile.base_city" in ctx.profile_view
    assert "job_chat.loose_salary" in ctx.session_approvals

    with pytest.raises(Exception):  # frozen
        ctx.module = "mail"  # type: ignore[misc]


def test_permission_context_rejects_empty_identifiers() -> None:
    base = {
        "module": "m",
        "task_id": "t",
        "trace_id": "tr",
        "user_id": None,
    }
    for attr in ("module", "task_id", "trace_id"):
        kwargs = dict(base)
        kwargs[attr] = ""
        with pytest.raises(ValueError):
            PermissionContext(**kwargs)  # type: ignore[arg-type]


def test_permission_context_user_id_must_be_none_or_non_empty() -> None:
    PermissionContext(module="m", task_id="t", trace_id="tr", user_id=None)
    with pytest.raises(ValueError):
        PermissionContext(module="m", task_id="t", trace_id="tr", user_id="")


def test_permission_context_isolates_rules_and_profile_from_external_mutation() -> None:
    """外部持有原 dict 后, 修改不应影响已构造的 Context —— 避免评估中
    途被改而产生幽灵判决。这是 ADR-006 §4.2 不变式的代码锚点。
    """
    rules_caller: dict[str, object] = {"rule.a": {"decision": "allow"}}
    profile_caller: dict[str, object] = {"profile.x": "old"}

    ctx = PermissionContext(
        module="m",
        task_id="t",
        trace_id="tr",
        user_id="u",
        rules=rules_caller,
        profile_view=profile_caller,
    )

    rules_caller["rule.a"] = {"decision": "deny"}
    rules_caller["rule.b"] = {"decision": "ask"}
    profile_caller["profile.x"] = "mutated"
    profile_caller["profile.y"] = "leaked"

    assert ctx.rules["rule.a"] == {"decision": "allow"}
    assert "rule.b" not in ctx.rules
    assert ctx.profile_view["profile.x"] == "old"
    assert "profile.y" not in ctx.profile_view

    # 通过 Context 也无法反向写回
    with pytest.raises(TypeError):
        ctx.rules["rule.c"] = {"decision": "allow"}  # type: ignore[index]
    with pytest.raises(TypeError):
        ctx.profile_view["profile.z"] = "hack"  # type: ignore[index]


def test_permission_context_session_approvals_type_is_enforced() -> None:
    # set 不等于 frozenset, 必须显式用 frozenset (可 hash / 可放事件 payload)
    with pytest.raises(TypeError):
        PermissionContext(
            module="m",
            task_id="t",
            trace_id="tr",
            user_id=None,
            session_approvals={"rule_a"},  # type: ignore[arg-type]
        )


def test_permission_context_with_session_approval_is_functional() -> None:
    ctx = _ctx()
    extended = ctx.with_session_approval("job_chat.one_shot_time_allow")

    assert extended is not ctx
    assert "job_chat.one_shot_time_allow" in extended.session_approvals
    assert "job_chat.loose_salary" in extended.session_approvals
    # 原 Context 不被污染
    assert "job_chat.one_shot_time_allow" not in ctx.session_approvals

    with pytest.raises(ValueError):
        ctx.with_session_approval("")
