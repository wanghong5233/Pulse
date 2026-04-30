"""Business layer for the ``job.profile`` capability (v2).

Thin wrapper around :class:`JobMemory`。职责:

  * 对外暴露三类存储的 CRUD:
      - Hard Constraints: set_hard_constraint / unset_hard_constraint
      - Memory Items:     record_item / retire_item / supersede_item / list_items
      - Resume:           update_resume / patch_resume_parsed / get_resume
  * 每个方法发射 ``intent`` stage 事件, 便于 pipeline_runs 审计
  * HTTP 路由 & Brain IntentSpec handler 都复用本 service

Natural-language 解析已移除 — 所有用户意图由 Brain 通过 tool_use 抽取为
结构化 kwargs 后直接调本 service 方法。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable

from ..memory import (
    HardConstraints,
    HARD_CONSTRAINT_FIELDS,
    MEMORY_ITEM_TYPES,
    JobMemory,
)

logger = logging.getLogger(__name__)


EmitStageEvent = Callable[..., str]


class JobProfileService:
    def __init__(
        self,
        *,
        memory: JobMemory,
        emit_stage_event: EmitStageEvent,
    ) -> None:
        self._mem = memory
        self._emit = emit_stage_event

    # ─── Snapshot ────────────────────────────────────────────

    def snapshot(self, *, trace_id: str | None = None) -> dict[str, Any]:
        trace_id = self._emit(stage="snapshot", status="started", trace_id=trace_id, payload={})
        snap = self._mem.snapshot()
        data = snap.to_dict()
        self._emit(
            stage="snapshot", status="completed", trace_id=trace_id,
            payload={
                "memory_items_active": len(snap.active_items()),
                "memory_items_total": len(snap.memory_items),
                "hc_empty": snap.hard_constraints.is_empty(),
                "has_resume": snap.resume is not None,
                "snapshot_version": snap.snapshot_version,
            },
        )
        return {"ok": True, "trace_id": trace_id, **data}

    # ─── Hard Constraints ────────────────────────────────────

    def set_hard_constraint(
        self,
        *,
        field: str,
        value: Any,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        trace_id = self._emit(
            stage="set_hard_constraint", status="started",
            trace_id=trace_id, payload={"field": field},
        )
        try:
            self._mem.set_hard_constraint(field, value)
        except ValueError as exc:
            self._emit(
                stage="set_hard_constraint", status="failed",
                trace_id=trace_id, payload={"error": str(exc), "field": field},
            )
            return {"ok": False, "trace_id": trace_id, "error": str(exc)}
        self._emit(
            stage="set_hard_constraint", status="completed",
            trace_id=trace_id, payload={"field": field},
        )
        hc = self._mem.get_hard_constraints()
        effective_value = self._effective_hard_constraint_value(field=field, hc=hc)
        return {
            "ok": True,
            "trace_id": trace_id,
            "field": field,
            "effective_value": effective_value,
            "effective_value_human": self._format_hard_constraint_value(
                field=field,
                value=effective_value,
            ),
        }

    def unset_hard_constraint(
        self,
        *,
        field: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        trace_id = self._emit(
            stage="unset_hard_constraint", status="started",
            trace_id=trace_id, payload={"field": field},
        )
        removed = self._mem.unset_hard_constraint(field)
        self._emit(
            stage="unset_hard_constraint", status="completed",
            trace_id=trace_id, payload={"field": field, "removed": removed},
        )
        return {"ok": True, "trace_id": trace_id, "field": field, "removed": removed}

    # ─── Memory Items ────────────────────────────────────────

    def record_item(
        self,
        *,
        type: str,
        content: str,
        target: str | None = None,
        raw_text: str = "",
        valid_until: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """追加一条 Memory Item (来自 Brain 的 IntentTool call)。"""
        trace_id = self._emit(
            stage="record_item", status="started",
            trace_id=trace_id, payload={"type": type, "target": target},
        )
        clean_content = (content or "").strip()
        if not clean_content:
            self._emit(
                stage="record_item", status="failed",
                trace_id=trace_id, payload={"error": "content empty"},
            )
            return {"ok": False, "trace_id": trace_id, "error": "content is required"}
        try:
            item = self._mem.record_item({
                "type": type or "other",
                "target": target,
                "content": clean_content,
                "raw_text": raw_text or clean_content,
                "valid_until": valid_until,
            })
        except Exception as exc:   # noqa: BLE001
            self._emit(
                stage="record_item", status="failed",
                trace_id=trace_id, payload={"error": str(exc)},
            )
            return {"ok": False, "trace_id": trace_id, "error": str(exc)}
        self._emit(
            stage="record_item", status="completed",
            trace_id=trace_id, payload={"id": item.id, "type": item.type},
        )
        return {"ok": True, "trace_id": trace_id, "item": item.to_dict()}

    def retire_item(
        self,
        *,
        id: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        trace_id = self._emit(
            stage="retire_item", status="started",
            trace_id=trace_id, payload={"id": id},
        )
        removed = self._mem.retire_item(id)
        self._emit(
            stage="retire_item", status="completed",
            trace_id=trace_id, payload={"id": id, "retired": removed},
        )
        return {"ok": True, "trace_id": trace_id, "id": id, "retired": removed}

    def supersede_item(
        self,
        *,
        old_id: str,
        type: str,
        content: str,
        target: str | None = None,
        raw_text: str = "",
        valid_until: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        trace_id = self._emit(
            stage="supersede_item", status="started",
            trace_id=trace_id, payload={"old_id": old_id},
        )
        clean_content = (content or "").strip()
        if not clean_content:
            return {"ok": False, "trace_id": trace_id, "error": "content is required"}
        try:
            new_item = self._mem.supersede_item(
                old_id,
                {
                    "type": type or "other",
                    "target": target,
                    "content": clean_content,
                    "raw_text": raw_text or clean_content,
                    "valid_until": valid_until,
                },
            )
        except ValueError as exc:
            self._emit(
                stage="supersede_item", status="failed",
                trace_id=trace_id, payload={"error": str(exc)},
            )
            return {"ok": False, "trace_id": trace_id, "error": str(exc)}
        self._emit(
            stage="supersede_item", status="completed",
            trace_id=trace_id, payload={"old_id": old_id, "new_id": new_item.id},
        )
        return {
            "ok": True, "trace_id": trace_id,
            "old_id": old_id, "new_item": new_item.to_dict(),
        }

    def list_items(
        self,
        *,
        type: str | None = None,
        target: str | None = None,
        include_expired: bool = False,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        trace_id = self._emit(
            stage="list_items", status="started",
            trace_id=trace_id, payload={"type": type, "target": target},
        )
        items = self._mem.list_items(
            type=type, target=target, include_expired=include_expired,
        )
        self._emit(
            stage="list_items", status="completed",
            trace_id=trace_id, payload={"count": len(items)},
        )
        return {
            "ok": True, "trace_id": trace_id,
            "count": len(items),
            "items": [it.to_dict() for it in items],
        }

    # ─── Resume ──────────────────────────────────────────────

    def update_resume(
        self,
        *,
        raw_text: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        trace_id = self._emit(
            stage="update_resume", status="started",
            trace_id=trace_id, payload={"chars": len(raw_text or "")},
        )
        clean = (raw_text or "").strip()
        if not clean:
            return {"ok": False, "trace_id": trace_id, "error": "resume raw_text is required"}
        try:
            resume = self._mem.update_resume(clean)
        except Exception as exc:   # noqa: BLE001
            self._emit(
                stage="update_resume", status="failed",
                trace_id=trace_id, payload={"error": str(exc)},
            )
            return {"ok": False, "trace_id": trace_id, "error": str(exc)}
        self._emit(
            stage="update_resume", status="completed",
            trace_id=trace_id, payload={"raw_hash": resume.raw_hash, "chars": len(clean)},
        )
        return {
            "ok": True, "trace_id": trace_id,
            "resume": resume.to_dict(),
        }

    def patch_resume_parsed(
        self,
        *,
        patch: dict[str, Any],
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        trace_id = self._emit(
            stage="patch_resume_parsed", status="started",
            trace_id=trace_id, payload={"keys": list(patch.keys())},
        )
        parsed = self._mem.patch_resume_parsed(patch)
        self._emit(
            stage="patch_resume_parsed", status="completed",
            trace_id=trace_id, payload={"applied": parsed is not None},
        )
        return {
            "ok": True, "trace_id": trace_id,
            "parsed": parsed.to_dict() if parsed else None,
        }

    def get_resume(self, *, trace_id: str | None = None) -> dict[str, Any]:
        trace_id = self._emit(
            stage="get_resume", status="started",
            trace_id=trace_id, payload={},
        )
        resume = self._mem.get_resume()
        self._emit(
            stage="get_resume", status="completed",
            trace_id=trace_id, payload={"present": resume is not None},
        )
        return {
            "ok": True, "trace_id": trace_id,
            "resume": resume.to_dict() if resume else None,
        }

    # ─── Admin (dev 期 wipe) ─────────────────────────────────

    def clear_all(self, *, trace_id: str | None = None) -> dict[str, Any]:
        """清空本 workspace 所有 job.* 记忆。开发期使用。"""
        trace_id = self._emit(
            stage="clear_all", status="started", trace_id=trace_id, payload={},
        )
        removed = self._mem.clear_all()
        self._emit(
            stage="clear_all", status="completed",
            trace_id=trace_id, payload={"removed": removed},
        )
        return {"ok": True, "trace_id": trace_id, "removed": removed}

    @staticmethod
    def _effective_hard_constraint_value(
        *, field: str, hc: HardConstraints
    ) -> Any:
        if field == "preferred_location":
            return list(hc.preferred_location)
        if field == "target_roles":
            return list(hc.target_roles)
        if field == "experience_level":
            return hc.experience_level
        if field == "salary_floor_monthly":
            if hc.salary_floor_monthly is None:
                return None
            if hc.salary_floor_spec:
                return {
                    "value_monthly_k": hc.salary_floor_monthly,
                    "source": dict(hc.salary_floor_spec),
                }
            return {"value_monthly_k": hc.salary_floor_monthly}
        return None

    @staticmethod
    def _format_hard_constraint_value(*, field: str, value: Any) -> str:
        if field in {"preferred_location", "target_roles"}:
            if not isinstance(value, list):
                return "(empty)"
            items = [str(v).strip() for v in value if str(v).strip()]
            return ", ".join(items) if items else "(empty)"
        if field == "experience_level":
            return str(value or "(empty)")
        if field == "salary_floor_monthly":
            if not isinstance(value, dict):
                return "(empty)"
            monthly_k = value.get("value_monthly_k")
            if not isinstance(monthly_k, int) or isinstance(monthly_k, bool):
                return "(empty)"
            source = value.get("source")
            if not isinstance(source, dict):
                return f"{monthly_k} K/月"
            amount = source.get("amount")
            amount_text = str(amount)
            if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                amount_text = str(int(amount)) if math.isclose(amount % 1, 0.0) else f"{amount:g}"
            unit = str(source.get("unit") or "")
            period = str(source.get("period") or "")
            wd = source.get("work_days_per_month")
            wd_tail = ""
            if period == "day" and isinstance(wd, int) and wd > 0:
                wd_tail = f" (work_days_per_month={wd})"
            return f"{amount_text} {unit}/{period} (~{monthly_k} K/月){wd_tail}"
        return str(value)


# 重新导出, 供 module.py 暴露时用
ALLOWED_HARD_CONSTRAINT_FIELDS = HARD_CONSTRAINT_FIELDS
RECOMMENDED_MEMORY_ITEM_TYPES = MEMORY_ITEM_TYPES


__all__ = [
    "JobProfileService",
    "ALLOWED_HARD_CONSTRAINT_FIELDS",
    "RECOMMENDED_MEMORY_ITEM_TYPES",
]
