"""SafetyPlane · Decision / AskRequest / ResumeHandle 契约原语.

Policy 函数的输出三元组. 核心语义:

* :class:`Decision` 是 ``allow`` / ``deny`` / ``ask`` 三值决策的容器,
  Service 层 (``chat/service.py`` 等) 只消费它, 不得自己重新判断
  "是否该 ask".
* :class:`AskRequest` 是 ``kind == "ask"`` 时的附加 payload —— 包含
  给用户的问题、可选草稿、恢复任务所需的回路信息 (:class:`ResumeHandle`)
  和超时时长.
* :class:`ResumeHandle` 不是 "handle 指针", 是 Resume 时路由回
  SuspendedTaskStore 所需的 **静态元数据** (task_id / module / intent
  名 / payload schema).

规约权威: ``docs/adr/ADR-006-v2-SafetyPlane.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = (
    "DecisionKind",
    "VALID_DECISION_KINDS",
    "ResumeHandle",
    "AskRequest",
    "Decision",
)


DecisionKind = Literal["allow", "deny", "ask"]

VALID_DECISION_KINDS: frozenset[str] = frozenset(DecisionKind.__args__)  # type: ignore[attr-defined]


# 超时默认值仅用于 from_dict 回填旧事件 (来自 payload_schema 升级前的历史).
# 活体代码必须显式传 ``timeout_seconds``, 不走默认, 见 ``__post_init__``。
_LEGACY_ASK_TIMEOUT_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class ResumeHandle:
    """Resume 路由元数据.

    Policy 在发出 ``ask`` 判决时, 必须同时给出 ``ResumeHandle`` —— 告诉
    SuspendedTaskStore 和 Resume 通道:

    * ``task_id``: 本次挂起任务的 id, 也是用户回复里带回的路由键.
    * ``module``: 原始调用方的模块名 (``job_chat`` / ``mail`` / ...),
      Resume 时需要把用户回答派发回同一个 module 消费.
    * ``intent``: Resume 时要触发的 intent 路由名 (MVP 固定
      ``system.task.resume``; 预留字段是为了未来域特化的 Resume 动作).
    * ``payload_schema``: 用户回答 payload 的 schema id, Resume 时用来
      校验 "用户答的东西结构是对的", 避免 "用户随便打一句话就被认成
      resume 参数" 的幽灵漏洞.
    """

    task_id: str
    module: str
    intent: str
    payload_schema: str

    def __post_init__(self) -> None:
        for attr in ("task_id", "module", "intent", "payload_schema"):
            value = getattr(self, attr)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"ResumeHandle.{attr} must be a non-empty string"
                )

    def to_dict(self) -> dict[str, str]:
        return {
            "task_id": self.task_id,
            "module": self.module,
            "intent": self.intent,
            "payload_schema": self.payload_schema,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResumeHandle":
        return cls(
            task_id=str(data.get("task_id", "") or ""),
            module=str(data.get("module", "") or ""),
            intent=str(data.get("intent", "") or ""),
            payload_schema=str(data.get("payload_schema", "") or ""),
        )


@dataclass(frozen=True, slots=True)
class AskRequest:
    """面向人类用户的一次 Ask (Suspend-Ask-Resume 的 Ask 环节).

    * ``question``: 用自然语言呈现给用户的主问题 (典型: 转述 HR 的话
      或说明工具缺什么信息), 必须非空.
    * ``draft``: Agent 对"如果你同意 / 答 X"的建议草稿. 允许为 None
      (例: 时间类问题没有默认答案), 但给了就必须是可直接发送的成品.
    * ``context``: 帮助用户判断的背景字典 (HR 名 / 岗位 / 历史对话
      摘要等). 拷贝后由 SuspendedTask 保管; 允许为空 dict.
    * ``resume_handle``: 恢复任务的路由元数据, 见 :class:`ResumeHandle`.
    * ``timeout_seconds``: 用户不回答的等待上限. 超时后 SuspendedTaskStore
      把挂起任务标 ``timed_out`` 并落审计事件, 等价于 deny 处理 ——
      绝不沉默走掉. 必须是正整数.
    """

    question: str
    resume_handle: ResumeHandle
    timeout_seconds: int
    draft: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.question, str) or not self.question.strip():
            raise ValueError("AskRequest.question must be a non-empty string")
        if not isinstance(self.resume_handle, ResumeHandle):
            raise TypeError(
                f"AskRequest.resume_handle must be ResumeHandle, "
                f"got {type(self.resume_handle).__name__}"
            )
        if not isinstance(self.timeout_seconds, int) or self.timeout_seconds <= 0:
            raise ValueError(
                f"AskRequest.timeout_seconds must be a positive int, "
                f"got {self.timeout_seconds!r}"
            )
        if self.draft is not None and not isinstance(self.draft, str):
            raise TypeError("AskRequest.draft must be str or None")
        if not isinstance(self.context, dict):
            raise TypeError("AskRequest.context must be dict")
        object.__setattr__(self, "context", dict(self.context))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "question": self.question,
            "resume_handle": self.resume_handle.to_dict(),
            "timeout_seconds": self.timeout_seconds,
        }
        if self.draft:
            out["draft"] = self.draft
        if self.context:
            out["context"] = dict(self.context)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AskRequest":
        raw_handle = data.get("resume_handle") or {}
        if isinstance(raw_handle, ResumeHandle):
            handle = raw_handle
        else:
            handle = ResumeHandle.from_dict(raw_handle)
        timeout = data.get("timeout_seconds")
        if timeout is None:
            timeout = _LEGACY_ASK_TIMEOUT_SECONDS
        return cls(
            question=str(data.get("question", "") or ""),
            resume_handle=handle,
            timeout_seconds=int(timeout),
            draft=data.get("draft") or None,
            context=dict(data.get("context") or {}),
        )


@dataclass(frozen=True, slots=True)
class Decision:
    """Policy 的三值决策 (``allow`` / ``deny`` / ``ask``).

    三种 kind 的字段约束 (``__post_init__`` 强制):

    =========  ============  =========  ===============
    kind       ask_request   deny_code  其它
    =========  ============  =========  ===============
    allow      None          None       reason 非空
    deny       None          非空字符串 reason 非空
    ask        非 None        None       reason 非空
    =========  ============  =========  ===============

    字段叫 ``ask_request`` 而不是 ``ask`` —— 因为便捷构造器
    :meth:`Decision.ask` 是同名的, 在 dataclass 上两者冲突
    (classmethod 会覆盖 field 默认值). 名词 ``ask_request`` 也比
    裸名词 ``ask`` 语义更明确.

    ``rule_id`` 允许为 None —— policy 走 fail-to-ask 异常路径时可能无具体
    rule 命中. 其它情况应该填命中 policy 分支的 id, 审计会用它回溯.

    ``reason`` 始终非空, 做为人类可读解释 (例: ``"session_approved"`` /
    ``"job_chat.reply.requires_user_confirmation"``).
    """

    kind: DecisionKind
    reason: str
    rule_id: str | None = None
    ask_request: AskRequest | None = None
    deny_code: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in VALID_DECISION_KINDS:
            raise ValueError(
                f"Decision.kind must be one of {sorted(VALID_DECISION_KINDS)}, "
                f"got {self.kind!r}"
            )
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("Decision.reason must be a non-empty string")
        if self.rule_id is not None:
            if not isinstance(self.rule_id, str) or not self.rule_id.strip():
                raise ValueError("Decision.rule_id must be None or non-empty string")

        if self.kind == "allow":
            if self.ask_request is not None or self.deny_code is not None:
                raise ValueError(
                    "Decision(kind='allow') must have ask_request=None and deny_code=None"
                )
        elif self.kind == "deny":
            if self.ask_request is not None:
                raise ValueError("Decision(kind='deny') must have ask_request=None")
            if not isinstance(self.deny_code, str) or not self.deny_code.strip():
                raise ValueError(
                    "Decision(kind='deny') must have non-empty deny_code"
                )
        else:  # kind == "ask"
            if self.deny_code is not None:
                raise ValueError("Decision(kind='ask') must have deny_code=None")
            if not isinstance(self.ask_request, AskRequest):
                raise ValueError(
                    "Decision(kind='ask') must carry an AskRequest"
                )

    # ── 便捷构造器 ─────────────────────────────────────────────
    #
    # 这三个 classmethod 不是糖, 是 **给调用方提供不变式保证**:
    # 业务代码只会写 ``Decision.allow(reason=..., rule_id=...)``,
    # 不会手抖写出 ``kind == "allow"`` 但 ``deny_code`` 不为 None 的
    # 病态对象。__post_init__ 是第二道闸, 不是唯一闸。

    @classmethod
    def allow(cls, *, reason: str, rule_id: str | None = None) -> "Decision":
        return cls(kind="allow", reason=reason, rule_id=rule_id)

    @classmethod
    def deny(
        cls,
        *,
        reason: str,
        deny_code: str,
        rule_id: str | None = None,
    ) -> "Decision":
        return cls(
            kind="deny",
            reason=reason,
            deny_code=deny_code,
            rule_id=rule_id,
        )

    @classmethod
    def ask(
        cls,
        *,
        reason: str,
        ask_request: AskRequest,
        rule_id: str | None = None,
    ) -> "Decision":
        return cls(
            kind="ask",
            reason=reason,
            ask_request=ask_request,
            rule_id=rule_id,
        )

    # ── 序列化 ─────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "reason": self.reason}
        if self.rule_id:
            out["rule_id"] = self.rule_id
        if self.ask_request is not None:
            out["ask_request"] = self.ask_request.to_dict()
        if self.deny_code:
            out["deny_code"] = self.deny_code
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Decision":
        kind = data.get("kind")
        if kind not in VALID_DECISION_KINDS:
            raise ValueError(f"invalid Decision.kind: {kind!r}")
        raw_ask = data.get("ask_request")
        ask_request: AskRequest | None
        if raw_ask is None:
            ask_request = None
        elif isinstance(raw_ask, AskRequest):
            ask_request = raw_ask
        else:
            ask_request = AskRequest.from_dict(raw_ask)
        return cls(
            kind=kind,  # type: ignore[arg-type]
            reason=str(data.get("reason", "") or ""),
            rule_id=data.get("rule_id") or None,
            ask_request=ask_request,
            deny_code=data.get("deny_code") or None,
        )
