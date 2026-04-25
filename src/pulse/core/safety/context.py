"""SafetyPlane · PermissionContext.

PermissionContext 是 policy 函数的第二个入参 (Intent 是第一个). 它回答
**"谁、在哪个任务、带着什么证据、在什么会话许可下"** 发起这次 Intent.

核心设计不变式:

1. **Context 不可变**: 一次 policy 评估里会多次读 ``profile_view`` /
   ``session_approvals``, 中途被改会出现 "看起来允许实际拒绝" 的幽灵
   判决. 所以 ``PermissionContext`` 是 frozen dataclass, Mapping 字段
   构造时拷贝后再包 ``MappingProxyType``, 外部对原 dict 的修改无法回灌.
2. **只承载 policy 需要的字段**: 不是 TaskContext 的 superset. Policy
   不需要知道 token 预算 / 重试次数 / sleep 窗口 —— 那些由调用方另行
   决定. Policy 只关心"谁在什么证据和会话许可之下".
3. **不再携带 ``rules``**: v2 下 policy 是 Python 函数, 规则就在代码里,
   不需要再传一份 "合并后的规则视图". 旧 Context.rules 字段已移除.

规约权威: ``docs/adr/ADR-006-v2-SafetyPlane.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

__all__ = ("PermissionContext",)


@dataclass(frozen=True, slots=True)
class PermissionContext:
    """Policy 函数所需的完整环境快照.

    字段
    =====

    * ``module``: 发起 Intent 的 module 名 (``job_chat`` / ``mail`` /
      ``game`` / ...). 写入审计便于按 module 过滤.
    * ``task_id``: 当前任务 id (通常来自 ``TaskContext.task_id``).
      Policy 在 ``ask`` 分支会用它做 SuspendedTask 路由键的一部分.
    * ``trace_id``: 当前 trace id, 审计事件必备.
    * ``user_id``: 用户 id. 可为 None —— 纯系统任务 (例: 后台签到)
      没有归属用户; 但面向 IM 会话的任务必须非空, 否则 policy 发不出
      "找谁 ask". 校验归调用方, ``PermissionContext`` 本身允许 None.
    * ``profile_view``: 面向 policy 的 profile 只读投影. 只包含动作
      ``evidence_keys`` 声明过的字段, 避免整个 profile 泄到上下文里.
    * ``session_approvals``: 用户本会话显式授权过的 token 集合 (token
      规范由各 policy 约定, 如 ``reply:<conversation_id>:<draft_hash>``).
      永远是 ``frozenset[str]`` —— 用 ``frozenset`` 而不是 ``set`` 是为了
      "值相等时对象 hash 相等", 便于放进事件 payload 做去重.
    """

    module: str
    task_id: str
    trace_id: str
    user_id: str | None
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

        # 拷贝 + 冻结, 断开与调用方持有的原 dict 的引用.
        # MappingProxyType 会拒绝一切写操作; dict(self.profile_view) 的
        # 深度是"浅拷贝": key 与顶层 value 独立, 嵌套 dict/list 仍共享引
        # 用. 这是有意: profile_view 的顶层是平铺的字段名, 不存在嵌套可
        # 变结构; 若未来真有需要再换深拷贝.
        object.__setattr__(
            self,
            "profile_view",
            MappingProxyType(dict(self.profile_view)),
        )

    def with_session_approval(self, token: str) -> "PermissionContext":
        """返回一个新的 Context, 额外登记一条 session approval.

        这个便捷方法**不是**给 "Brain 中途给自己放权" 用的 —— Brain
        永远只消费 Decision, 不自己构造 PermissionContext. 是 Resume
        路径预留的钩子: 用户在 IM 回答时可能顺带"以后类似场景自动通过",
        Resume 通道把这条一次性授权注入回去, 下轮 patrol 进 policy 时
        走 session_approved 分支直接放行.
        """
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token must be a non-empty string")
        return PermissionContext(
            module=self.module,
            task_id=self.task_id,
            trace_id=self.trace_id,
            user_id=self.user_id,
            profile_view=dict(self.profile_view),
            session_approvals=self.session_approvals | {token},
        )
