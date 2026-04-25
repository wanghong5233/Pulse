"""SafetyPlane · Intent 原语.

Intent 描述"即将发生的一次动作", 是 policy 函数的两个入参之一 (另一个
是 :class:`PermissionContext`). 它是 **只读语义快照**:

* Policy 对 Intent 的观察必须是"拍快照即不变"的, 否则求值过程中
  被调用方偷偷改字段会产生"看起来是 A 实际是 B"的幽灵判决.
* Intent 不含调用方身份 (module / task_id / trace_id), 那些在
  ``PermissionContext`` 里. Intent 只回答 **"要做什么"**,
  PermissionContext 回答 **"谁在什么环境下要做"**.

规约权威: ``docs/adr/ADR-006-v2-SafetyPlane.md``.
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
    # v2 架构下, 所有 side-effect 都在 Module service 层自己发起 (例: 发
    # HR 回复 / 发简历 / 点卡片); Brain 不再直接触发外部动作, 因此只保留
    # ``mutation``. ``tool_call`` / ``reply`` 留做历史枚举值供旧序列化快照
    # 回放, 活体代码不应产生这两种新 Intent.
    "tool_call",
    "mutation",
    "reply",
]

VALID_INTENT_KINDS: frozenset[str] = frozenset(IntentKind.__args__)  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class Intent:
    """一次"即将发生的动作"的只读描述.

    字段语义
    =========

    * ``kind``: 动作类别, 仅做审计/历史分类用 (v2 下活体 policy 都是
      ``mutation``, 不再据 kind 分派逻辑).
    * ``name``: 动作的规范名 (例: ``job.chat.send_reply``), 写入审计事件
      便于回溯.
    * ``args``: 动作参数 (拷贝后冻结为 ``MappingProxyType``). Policy 函数
      可以读, 但不能通过 Intent 反向修改真实参数 —— 那是调用方的职责.
    * ``evidence_keys``: 这次动作 **宣称依赖的 profile 字段**. Policy 用
      :func:`pulse.core.safety.policies.profile_covers` 一次性校验这些
      字段在 profile 里有实锤, 避免 LLM 编造事实. 空 tuple 表示此动作不
      走 profile 证据豁免分支.

    不变式 (``__post_init__`` 强制)
    ================================

    * ``kind`` 必须是 :data:`VALID_INTENT_KINDS` 之一 —— 新增类别要显式
      扩枚举, 禁止 silent 通过.
    * ``name`` 去空白后必须非空.
    * ``args`` 必须是 mapping; 构造后被冻结 + 拷贝, 外部对原 dict 的
      修改不影响 Intent.
    * ``evidence_keys`` 被冻结为 tuple.
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

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Intent":
        """从 to_dict 产出恢复 Intent. 与持久化层对称, 避免调用方手搓."""
        if not isinstance(data, Mapping):
            raise TypeError(f"Intent.from_dict requires Mapping, got {type(data).__name__}")
        return cls(
            kind=data["kind"],
            name=data["name"],
            args=data.get("args") or {},
            evidence_keys=tuple(data.get("evidence_keys") or ()),
        )
