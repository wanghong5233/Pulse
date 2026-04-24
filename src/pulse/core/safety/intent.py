"""SafetyPlane · Intent 原语 (ADR-006 §4.3).

Intent 描述的是"Agent 正要发起的动作",是 ``PermissionGate.check`` 的
两个入参之一(另一个是 :class:`PermissionContext`)。它是 **只读语义快照**:

* Gate 对 Intent 的观察必须是"拍快照即不变"的,否则规则求值过程中
  被调用方偷偷改字段会产生"看起来是 A 实际是 B"的幽灵判决。
* Intent 不含调用方身份(module / task_id / trace_id),那些在
  ``PermissionContext`` 里。Intent 只回答 **"要做什么"**,
  PermissionContext 回答 **"谁在什么环境下要做"**。

设计参见 ADR-006 §4.3。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping

__all__ = (
    "IntentKind",
    "VALID_INTENT_KINDS",
    "Intent",
)


IntentKind = Literal[
    "tool_call",  # Brain ReAct loop 要调一个注册工具
    "mutation",   # Module 自己发起的 mutating 动作 (例: 发 HR 回复 / 扣款)
    "reply",      # 面向外部用户的可见回复 (草稿也算)
]

VALID_INTENT_KINDS: frozenset[str] = frozenset(IntentKind.__args__)  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class Intent:
    """一次"即将发生的动作"的只读描述.

    字段语义
    =========

    * ``kind``: 动作类别, 决定规则匹配时的 selector 命名空间
      (``tool_call:*`` / ``mutation:*`` / ``reply:*``)。
    * ``name``: 动作的规范名, 与 ToolRegistry / IntentSpec 的 ``name``
      一一对应 (例: ``job.chat.send_reply``)。规则用 ``glob`` 匹配。
    * ``args``: 动作参数 (拷贝后冻结为 ``MappingProxyType``)。Predicate
      可以读, 但不能通过 Intent 反向修改真实参数 —— 那是调用方的职责。
    * ``evidence_keys``: 这次动作 **宣称依赖的 profile 字段**。规则可以
      用 ``all_evidence_in_profile`` 一次性校验这些字段都在 profile 里
      有实锤, 避免 LLM 编造事实。空 tuple 表示此动作不依赖 profile
      (典型: 读动作 / 无副作用的查询)。

    不变式 (``__post_init__`` 强制)
    ================================

    * ``kind`` 必须是 :data:`VALID_INTENT_KINDS` 之一, 否则
      ``ValueError`` (新增动作类别要显式扩枚举, 禁止 silent 通过)。
    * ``name`` 去空白后必须非空。
    * ``args`` 必须是 mapping; 构造后被冻结 + 拷贝, 外部对原 dict 的
      修改不影响 Intent。
    * ``evidence_keys`` 被冻结为 tuple。
    """

    kind: IntentKind
    name: str
    args: Mapping[str, Any] = field(default_factory=dict)
    evidence_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in VALID_INTENT_KINDS:
            raise ValueError(
                f"Intent.kind must be one of {sorted(VALID_INTENT_KINDS)}, got {self.kind!r}"
            )
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Intent.name must be a non-empty string")
        if not isinstance(self.args, Mapping):
            raise TypeError(
                f"Intent.args must be a Mapping, got {type(self.args).__name__}"
            )
        if not isinstance(self.evidence_keys, tuple):
            # 用 tuple 而不是 list, 因为 Intent 是 frozen 的; 列表会让
            # ``for key in intent.evidence_keys`` 看起来像可变迭代体。
            object.__setattr__(self, "evidence_keys", tuple(self.evidence_keys))
        for key in self.evidence_keys:
            if not isinstance(key, str) or not key:
                raise ValueError(
                    f"Intent.evidence_keys must be non-empty strings, got {key!r}"
                )

        frozen_args = MappingProxyType(dict(self.args))
        object.__setattr__(self, "args", frozen_args)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly 投影, 供事件 payload / 审计日志消费."""
        return {
            "kind": self.kind,
            "name": self.name,
            "args": dict(self.args),
            "evidence_keys": list(self.evidence_keys),
        }
