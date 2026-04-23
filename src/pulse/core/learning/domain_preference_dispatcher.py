"""Domain preference dispatcher.

把 ``PreferenceExtractor`` 抽取出来的 ``domain_prefs`` 按 domain 派发给对应的
``DomainPreferenceApplier``, 完成对 DomainMemory (如 ``JobMemory``) 的写入.

架构定位
--------
这是 Soul reflection pipeline 的第三段:

    raw user text
      → PreferenceExtractor (识别结构化信号)
      → core_prefs → Soul governance → CoreMemory.prefs
      → domain_prefs → **DomainPreferenceDispatcher**  ← 本模块
                        → DomainPreferenceApplier(domain=job) → JobMemory
                        → DomainPreferenceApplier(domain=...) → ...

第一性原理:
  * extractor 不碰 IO — 不依赖 workspace/数据库.
  * applier 只暴露通用 op (hard_constraint.set / memory.record), 内部翻译成
    具体 facade 方法; 每个 domain 自己拥有 schema 语义.
  * dispatcher 只做"按 domain 分路 + 统一审计事件"两件事, 不感知业务细节.

这种分层让 "自然语言 → 持久化" 的链路从"靠 LLM 调对工具"变成
"extractor + dispatcher 架构强制", 不再依赖模型运气 (首要修复目标).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from ..event_types import EventTypes, make_payload
from .preference_extractor import DomainPref

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DomainPreferenceDispatchResult:
    """一条 domain preference 派发的结果, 用于 reflection 结果审计.

    status 取值:
      * ``applied``  – applier 成功执行, 记忆层已变化
      * ``skipped``  – 配置/上下文原因跳过 (如 applier 未注册 / confidence 过低)
      * ``rejected`` – applier 显式拒绝 (非法 args / 超出白名单等)
      * ``error``    – applier 执行抛异常 (底层记忆写失败等)
    """

    domain: str
    op: str
    status: str
    reason: str = ""
    effect: dict[str, Any] = field(default_factory=dict)
    evidence: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "op": self.op,
            "status": self.status,
            "reason": self.reason,
            "effect": dict(self.effect),
            "evidence": self.evidence,
            "confidence": self.confidence,
        }


class DomainPreferenceApplier(Protocol):
    """每个业务域实现的 applier 协议.

    实现方负责:
      * 声明自己支持的 ``domain`` slug (与 extractor 的 ``domain`` 字段一致)
      * 声明白名单 ``supported_ops`` (dispatcher 在派发前会校验)
      * 实现 ``apply()``: 返回 ``DomainPreferenceDispatchResult``.
    """

    domain: str
    supported_ops: tuple[str, ...]

    def apply(
        self,
        pref: DomainPref,
        *,
        context: dict[str, Any],
    ) -> DomainPreferenceDispatchResult:
        ...


class DomainPreferenceDispatcher:
    """把一批 ``DomainPref`` 分派给对应 domain 的 applier, 并统一发事件.

    * 线程安全: 无共享可变状态, 每次 dispatch 都是独立事务.
    * 可扩展: 新增 domain 就注册新 applier, dispatcher 本身不需要改.
    """

    # dispatcher 级别的置信度下限; 低于此值直接 skip, 避免 LLM 乱写.
    _MIN_CONFIDENCE = 0.4

    def __init__(
        self,
        *,
        appliers: "dict[str, DomainPreferenceApplier] | list[DomainPreferenceApplier] | None" = None,
        event_emitter: "Callable[[str, dict[str, Any]], None] | None" = None,
        min_confidence: float | None = None,
    ) -> None:
        self._appliers: dict[str, DomainPreferenceApplier] = {}
        if isinstance(appliers, dict):
            for key, applier in appliers.items():
                self.register(applier, domain=str(key))
        elif isinstance(appliers, list):
            for applier in appliers:
                self.register(applier)
        self._emit = event_emitter
        if min_confidence is not None:
            try:
                self._min_confidence = max(0.0, min(1.0, float(min_confidence)))
            except (TypeError, ValueError):
                self._min_confidence = self._MIN_CONFIDENCE
        else:
            self._min_confidence = self._MIN_CONFIDENCE

    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------
    def register(
        self,
        applier: DomainPreferenceApplier,
        *,
        domain: str | None = None,
    ) -> None:
        """注册一个 applier; domain 默认取 applier.domain, 支持手动覆盖."""
        domain_name = str(domain or getattr(applier, "domain", "") or "").strip().lower()
        if not domain_name:
            raise ValueError("applier.domain must be a non-empty string")
        if domain_name in self._appliers:
            logger.warning(
                "DomainPreferenceDispatcher: overwriting applier for domain=%s",
                domain_name,
            )
        self._appliers[domain_name] = applier

    def bind_event_emitter(
        self,
        emitter: "Callable[[str, dict[str, Any]], None] | None",
    ) -> None:
        """绑定事件发射器 (典型是 EventBus.publish)."""
        self._emit = emitter

    def domains(self) -> list[str]:
        return sorted(self._appliers.keys())

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def dispatch(
        self,
        prefs: "list[DomainPref]",
        *,
        context: dict[str, Any] | None = None,
    ) -> list[DomainPreferenceDispatchResult]:
        """派发一批 domain preference; 返回每条的执行结果.

        ``context`` 约定字段 (全部可选):
          * ``workspace_id`` – 当前任务 workspace, applier 实例化 domain facade 用
          * ``trace_id`` / ``session_id`` / ``task_id`` / ``run_id`` – 事件上下文
          * ``actor`` – 派发发起方 (默认 ``soul.reflection``)
        """
        safe_context = dict(context or {})
        actor = str(safe_context.get("actor") or "soul.reflection")
        results: list[DomainPreferenceDispatchResult] = []

        for pref in prefs or []:
            if not isinstance(pref, DomainPref):
                logger.debug("dispatch skip non-DomainPref: %r", pref)
                continue
            result = self._dispatch_one(pref, context=safe_context, actor=actor)
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _dispatch_one(
        self,
        pref: DomainPref,
        *,
        context: dict[str, Any],
        actor: str,
    ) -> DomainPreferenceDispatchResult:
        domain = pref.domain or ""
        op = pref.op or ""
        started = time.perf_counter()

        # 1) 置信度门槛
        if pref.confidence < self._min_confidence:
            result = DomainPreferenceDispatchResult(
                domain=domain,
                op=op,
                status="skipped",
                reason=f"low_confidence<{self._min_confidence}",
                evidence=pref.evidence,
                confidence=pref.confidence,
            )
            self._emit_result(result, actor=actor, context=context, elapsed=started)
            return result

        # 2) domain 是否已注册
        applier = self._appliers.get(domain)
        if applier is None:
            logger.info(
                "dispatch skip: no applier for domain=%s (op=%s); "
                "registered=%s",
                domain, op, sorted(self._appliers.keys()),
            )
            result = DomainPreferenceDispatchResult(
                domain=domain,
                op=op,
                status="skipped",
                reason="no_applier_registered",
                evidence=pref.evidence,
                confidence=pref.confidence,
            )
            self._emit_result(result, actor=actor, context=context, elapsed=started)
            return result

        # 3) op 是否在 applier 白名单内
        supported = tuple(getattr(applier, "supported_ops", ()) or ())
        if supported and op not in supported:
            result = DomainPreferenceDispatchResult(
                domain=domain,
                op=op,
                status="rejected",
                reason=f"unsupported_op; supported={list(supported)}",
                evidence=pref.evidence,
                confidence=pref.confidence,
            )
            self._emit_result(result, actor=actor, context=context, elapsed=started)
            return result

        # 4) 真正执行
        try:
            result = applier.apply(pref, context=context)
            if not isinstance(result, DomainPreferenceDispatchResult):
                # applier 实现契约违例, 尽量解释清楚再报错
                logger.error(
                    "applier for domain=%s returned %r (expected DomainPreferenceDispatchResult)",
                    domain, type(result).__name__,
                )
                result = DomainPreferenceDispatchResult(
                    domain=domain,
                    op=op,
                    status="error",
                    reason="applier_contract_violation",
                    evidence=pref.evidence,
                    confidence=pref.confidence,
                )
        except Exception as exc:   # noqa: BLE001
            logger.warning(
                "applier domain=%s op=%s raised %s: %s",
                domain, op, type(exc).__name__, str(exc)[:200],
            )
            result = DomainPreferenceDispatchResult(
                domain=domain,
                op=op,
                status="error",
                reason=f"{type(exc).__name__}: {str(exc)[:160]}",
                evidence=pref.evidence,
                confidence=pref.confidence,
            )

        self._emit_result(result, actor=actor, context=context, elapsed=started)
        return result

    def _emit_result(
        self,
        result: DomainPreferenceDispatchResult,
        *,
        actor: str,
        context: dict[str, Any],
        elapsed: float,
    ) -> None:
        if self._emit is None:
            return
        event_type = {
            "applied": EventTypes.PREFERENCE_DOMAIN_APPLIED,
            "skipped": EventTypes.PREFERENCE_DOMAIN_SKIPPED,
            "rejected": EventTypes.PREFERENCE_DOMAIN_REJECTED,
            "error": EventTypes.PREFERENCE_DOMAIN_ERROR,
        }.get(result.status, EventTypes.PREFERENCE_DOMAIN_SKIPPED)
        payload = make_payload(
            actor=actor,
            trace_id=context.get("trace_id"),
            session_id=context.get("session_id"),
            task_id=context.get("task_id"),
            run_id=context.get("run_id"),
            workspace_id=context.get("workspace_id"),
            domain=result.domain,
            op=result.op,
            status=result.status,
            reason=result.reason or None,
            effect=dict(result.effect) if result.effect else None,
            evidence=result.evidence or None,
            confidence=result.confidence,
            latency_ms=int((time.perf_counter() - elapsed) * 1000),
        )
        try:
            self._emit(event_type, payload)
        except Exception as exc:   # noqa: BLE001
            logger.debug(
                "dispatcher emit_result failed (non-fatal): %s: %s",
                type(exc).__name__, str(exc)[:120],
            )


__all__ = [
    "DomainPreferenceDispatcher",
    "DomainPreferenceApplier",
    "DomainPreferenceDispatchResult",
]
