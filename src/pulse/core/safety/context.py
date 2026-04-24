"""SafetyPlane · PermissionContext (ADR-006 §4.2).

PermissionContext 是 ``PermissionGate.check`` 的第二个入参容器 (Intent
是第一个)。它回答 **"谁、在哪个任务、带着什么证据、在什么会话许可下"**
发起这次 Intent。

核心设计不变式:

1. **Context 不可变**: 同一次评估里 Gate 要多次用 ``profile_view`` /
   ``rules`` 做 predicate 求值, 如果中途被改就会出现 "看起来允许实际
   拒绝" 的幽灵判决。所以 ``PermissionContext`` 是 frozen dataclass,
   而其 Mapping 字段全部被拷贝后再包 ``MappingProxyType``, 外部对原
   ``dict`` 的修改无法回灌进 ``PermissionContext``。
2. **只承载 Gate 需要的字段**: 不是 TaskContext 的 superset。Gate 不
   需要知道 token 预算 / 重试次数 / sleep 窗口 —— 那些由调用方另行
   决定。Gate 只关心"谁在什么证据和规则之下"。
3. **RuleSet / ProfileView 形态**: 用 ``Mapping[str, Any]`` + 冻结,
   不引入新的 RuleSet/ProfileView 类。YAGNI —— 当前所有 predicate
   只需要 "按 key 读值", ``Mapping`` 已够; 真有新需求再引入。

设计参见 ADR-006 §4.2。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

__all__ = ("PermissionContext",)


@dataclass(frozen=True, slots=True)
class PermissionContext:
    """Gate.check 所需的完整环境快照.

    字段
    =====

    * ``module``: 发起 Intent 的 module 名 (``job_chat`` / ``mail`` /
      ``game`` / ...)。规则文件按此命名空间组织。
    * ``task_id``: 当前任务 id (通常来自 ``TaskContext.task_id``)。
      Gate 在 ``ask`` 分支会用它做 SuspendedTask 路由键的一部分。
    * ``trace_id``: 当前 trace id, 审计事件必备。
    * ``user_id``: 用户 id。可为 None —— 纯系统任务 (例: 后台签到)
      没有归属用户; 但面向 IM 会话的任务必须非空, 否则 Gate 发不出
      "找谁 ask"。校验归调用方, ``PermissionContext`` 本身允许 None。
    * ``rules``: 合并后的规则视图 (core ∪ domain ∪ session), 已被
      规则引擎排序、静态校验过。形态是 ``{rule_id: rule_body}``。
    * ``profile_view``: 面向 Gate 的 profile 只读投影。只包含规则
      预声明会用到的字段 (由 Gate 启动时基于 rule_engine 聚合
      ``needs_profile`` 生成), 避免整个 profile 泄到规则求值上下文。
    * ``session_approvals``: 用户本会话显式授权过的 rule_id 集合。
      Predicate ``session_approval`` 读它返回 True。永远是
      ``frozenset[str]`` —— 用 ``frozenset`` 而不是 ``set`` 是为了
      "值相等时对象 hash 相等", 便于放进事件 payload 做去重。
    """

    module: str
    task_id: str
    trace_id: str
    user_id: str | None
    rules: Mapping[str, Any] = field(default_factory=dict)
    profile_view: Mapping[str, Any] = field(default_factory=dict)
    session_approvals: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        for attr in ("module", "task_id", "trace_id"):
            value = getattr(self, attr)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"PermissionContext.{attr} must be a non-empty string"
                )
        if self.user_id is not None:
            if not isinstance(self.user_id, str) or not self.user_id.strip():
                raise ValueError(
                    "PermissionContext.user_id must be None or non-empty string"
                )

        if not isinstance(self.rules, Mapping):
            raise TypeError(
                f"PermissionContext.rules must be a Mapping, "
                f"got {type(self.rules).__name__}"
            )
        if not isinstance(self.profile_view, Mapping):
            raise TypeError(
                f"PermissionContext.profile_view must be a Mapping, "
                f"got {type(self.profile_view).__name__}"
            )
        if not isinstance(self.session_approvals, frozenset):
            raise TypeError(
                f"PermissionContext.session_approvals must be frozenset, "
                f"got {type(self.session_approvals).__name__}"
            )

        # 拷贝 + 冻结, 断开与调用方持有的原 dict 的引用。
        # MappingProxyType 会拒绝一切写操作 (__setitem__ / __delitem__),
        # dict(self.rules) 的深度是"浅拷贝": key 与顶层 value 独立, 嵌套
        # 的 dict/list 仍共享引用。这是有意设计: rule body 是静态的,
        # 从不就地修改; 若要换整条规则, 应该重建 PermissionContext。
        object.__setattr__(self, "rules", MappingProxyType(dict(self.rules)))
        object.__setattr__(
            self,
            "profile_view",
            MappingProxyType(dict(self.profile_view)),
        )

    def with_session_approval(self, rule_id: str) -> "PermissionContext":
        """返回一个新的 Context, 额外登记一条 session approval.

        这个便捷方法**不是**给 "Brain 中途给自己放权" 用的 —— Brain
        永远只消费 Decision, 不自己构造 PermissionContext。是 Resume
        路径专用: 用户在 IM 回答时可能顺带"以后类似场景自动通过",
        Resume 通道把这条一次性授权注入回去, 再 replay 原 Intent。
        """
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise ValueError("rule_id must be a non-empty string")
        return PermissionContext(
            module=self.module,
            task_id=self.task_id,
            trace_id=self.trace_id,
            user_id=self.user_id,
            rules=dict(self.rules),
            profile_view=dict(self.profile_view),
            session_approvals=self.session_approvals | {rule_id},
        )
