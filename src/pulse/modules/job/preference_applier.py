"""Job 域 preference applier.

实现 ``DomainPreferenceApplier`` 协议; 把通用的 ``domain_prefs[*]`` 指令翻译成
``JobMemory`` facade 的具体方法调用.

支持的 op:
  * ``hard_constraint.set``   → ``JobMemory.set_hard_constraint(field, value)``
  * ``hard_constraint.unset`` → ``JobMemory.unset_hard_constraint(field)``
  * ``memory.record``         → ``JobMemory.record_item(item_dict)``

架构文档: ``docs/Pulse-DomainMemory与Tool模式.md`` §3.1-3.3.

用法(由 server.py 装配):

    job_applier = JobPreferenceApplier(db_engine=engine, core_memory=core_memory)
    dispatcher.register(job_applier)

    # reflection 时:
    dispatcher.dispatch(extracted.domain_prefs, context={"workspace_id": ws})
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pulse.core.learning.domain_preference_dispatcher import (
    DomainPreferenceDispatchResult,
)
from pulse.core.learning.preference_extractor import DomainPref

from .memory import HARD_CONSTRAINT_FIELDS, MEMORY_ITEM_TYPES, JobMemory

if TYPE_CHECKING:
    from pulse.core.memory.core_memory import CoreMemory
    from pulse.core.storage.engine import DatabaseEngine

logger = logging.getLogger(__name__)


class JobPreferenceApplier:
    """把 ``DomainPref`` 翻译成 ``JobMemory`` 操作.

    本类按 task 临时实例化 ``JobMemory`` (workspace_id 由 dispatch context 提供),
    保证多 workspace 场景下不会串数据.
    """

    domain = "job"
    supported_ops: tuple[str, ...] = (
        "hard_constraint.set",
        "hard_constraint.unset",
        "memory.record",
    )

    def __init__(
        self,
        *,
        db_engine: "DatabaseEngine",
        core_memory: "CoreMemory | None" = None,
        default_workspace_id: str = "job.default",
    ) -> None:
        self._engine = db_engine
        self._core = core_memory
        self._default_workspace_id = str(default_workspace_id or "job.default").strip()

    def apply(
        self,
        pref: DomainPref,
        *,
        context: dict[str, Any],
    ) -> DomainPreferenceDispatchResult:
        workspace_id = self._resolve_workspace_id(context)
        job_mem = JobMemory.from_engine(
            self._engine,
            workspace_id=workspace_id,
            core_memory=self._core,
            source=f"preference_dispatch:{pref.op}",
        )

        op = pref.op
        if op == "hard_constraint.set":
            return self._apply_hc_set(pref, job_mem)
        if op == "hard_constraint.unset":
            return self._apply_hc_unset(pref, job_mem)
        if op == "memory.record":
            return self._apply_memory_record(pref, job_mem)

        # supported_ops 白名单外的调用不应到这里, dispatcher 已拦截
        return DomainPreferenceDispatchResult(
            domain=self.domain,
            op=op,
            status="rejected",
            reason="unsupported_op",
            evidence=pref.evidence,
            confidence=pref.confidence,
        )

    # ------------------------------------------------------------------
    # op handlers
    # ------------------------------------------------------------------
    def _apply_hc_set(
        self,
        pref: DomainPref,
        job_mem: JobMemory,
    ) -> DomainPreferenceDispatchResult:
        field_name = str(pref.args.get("field") or "").strip()
        value = pref.args.get("value")
        if not field_name:
            return self._reject(pref, "missing_field")
        if field_name not in HARD_CONSTRAINT_FIELDS:
            return self._reject(
                pref,
                f"unknown_hc_field: {field_name!r}; "
                f"allowed={list(HARD_CONSTRAINT_FIELDS)}",
            )
        try:
            job_mem.set_hard_constraint(field_name, value)
        except ValueError as exc:
            return self._reject(pref, f"validation_failed: {exc}")
        return DomainPreferenceDispatchResult(
            domain=self.domain,
            op=pref.op,
            status="applied",
            effect={
                "workspace_id": job_mem.workspace_id,
                "field": field_name,
                "value": value,
            },
            evidence=pref.evidence,
            confidence=pref.confidence,
        )

    def _apply_hc_unset(
        self,
        pref: DomainPref,
        job_mem: JobMemory,
    ) -> DomainPreferenceDispatchResult:
        field_name = str(pref.args.get("field") or "").strip()
        if not field_name:
            return self._reject(pref, "missing_field")
        if field_name not in HARD_CONSTRAINT_FIELDS:
            return self._reject(pref, f"unknown_hc_field: {field_name!r}")
        existed = job_mem.unset_hard_constraint(field_name)
        return DomainPreferenceDispatchResult(
            domain=self.domain,
            op=pref.op,
            status="applied" if existed else "skipped",
            reason="" if existed else "field_not_set",
            effect={
                "workspace_id": job_mem.workspace_id,
                "field": field_name,
                "existed": existed,
            },
            evidence=pref.evidence,
            confidence=pref.confidence,
        )

    def _apply_memory_record(
        self,
        pref: DomainPref,
        job_mem: JobMemory,
    ) -> DomainPreferenceDispatchResult:
        item = pref.args.get("item")
        if not isinstance(item, dict):
            return self._reject(pref, "missing_item_dict")
        # type / content 最小合法性; 其它字段由 JobMemory 归一化补齐.
        item_type = str(item.get("type") or "").strip()
        content = str(item.get("content") or "").strip()
        if not content:
            return self._reject(pref, "item.content is empty")
        if not item_type:
            # 容忍: LLM 偶尔忘记 type, 回落到 'other' 而不是拒绝.
            item = dict(item, type="other")
            item_type = "other"
        if item_type not in MEMORY_ITEM_TYPES:
            logger.debug(
                "JobPreferenceApplier: non-enum item.type=%r (workspace=%s); record as-is",
                item_type, job_mem.workspace_id,
            )
        try:
            stored = job_mem.record_item(item)
        except ValueError as exc:
            return self._reject(pref, f"record_failed: {exc}")
        return DomainPreferenceDispatchResult(
            domain=self.domain,
            op=pref.op,
            status="applied",
            effect={
                "workspace_id": job_mem.workspace_id,
                "item_id": stored.id,
                "type": stored.type,
                "target": stored.target,
            },
            evidence=pref.evidence,
            confidence=pref.confidence,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _resolve_workspace_id(self, context: dict[str, Any]) -> str:
        raw = context.get("workspace_id")
        value = str(raw or "").strip()
        return value or self._default_workspace_id

    def _reject(self, pref: DomainPref, reason: str) -> DomainPreferenceDispatchResult:
        return DomainPreferenceDispatchResult(
            domain=self.domain,
            op=pref.op,
            status="rejected",
            reason=reason,
            evidence=pref.evidence,
            confidence=pref.confidence,
        )


__all__ = ["JobPreferenceApplier"]
