"""Pulse MemoryEnvelope — 统一记忆信封

所有记忆读写操作都通过 MemoryEnvelope 进行，确保：
1. 每条记忆都携带完整的 ID 链路（trace_id → workspace_id）
2. Layer × Scope 双轴模型可追溯
3. 为后续 Compaction / Promotion 提供统一数据结构

注：不含 `embedding` 字段。Pulse 检索路径采用 agentic search，
内核不维护向量表示。详见 `docs/Pulse-MemoryRuntime设计.md` 附录 B。

设计参考：Pulse-MemoryRuntime设计.md §8 / §9
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class MemoryLayer(str, Enum):
    """记忆层级 — 回答"存在哪一层".

    架构决策: ``meta`` 已**废弃**(2026-04). 审计/合规改走独立的 ``EventLog``
    观测平面 (``core/event_types.py`` + ``core/event_sinks.py``), 不再和
    "给 LLM 读" 的记忆存储混在一起. 历史代码若仍产生 ``MemoryLayer.meta``
    envelope, Brain 会在 ``_route_envelope`` 里把它重路由到事件流, 不再落
    入记忆表.

    详见 ``docs/Pulse-内核架构总览.md`` §6 Observability Plane.
    """

    operational = "operational"
    recall = "recall"
    workspace = "workspace"
    archival = "archival"
    core = "core"
    meta = "meta"  # DEPRECATED: 改用 EventLog (event_types.EventTypes)


class MemoryScope(str, Enum):
    """记忆作用域 — 回答"属于哪个范围" """

    turn = "turn"
    task_run = "taskRun"
    session = "session"
    workspace = "workspace"
    global_ = "global"


class MemoryKind(str, Enum):
    """记忆内容类型"""

    conversation = "conversation"
    tool_call = "tool_call"
    fact = "fact"
    summary = "summary"
    task_summary = "task_summary"
    belief = "belief"
    preference = "preference"
    workspace_summary = "workspace_summary"
    correction = "correction"
    audit_trail = "audit_trail"


@dataclass
class MemoryEnvelope:
    """统一记忆信封 — 所有记忆操作的标准载体。

    不管是写入 recall、archival 还是 core，都先包装成 envelope，
    再由各 memory 实现解包写入。这样 ID 链路和 scope 信息不会丢失。
    """

    memory_id: str = field(default_factory=lambda: f"mem_{uuid4().hex[:12]}")

    kind: MemoryKind = MemoryKind.conversation
    layer: MemoryLayer = MemoryLayer.recall
    scope: MemoryScope = MemoryScope.session

    # ── 关键 ID 链路（从 TaskContext.id_dict() 注入）──────────
    trace_id: str = ""
    run_id: str = ""
    task_id: str = ""
    session_id: str | None = None
    workspace_id: str | None = None

    # ── 内容 ────────────────────────────────────────────────
    content: str | dict[str, Any] = field(default_factory=dict)

    # ── 元数据 ──────────────────────────────────────────────
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    updated_at: datetime | None = None
    source: str = ""
    confidence: float = 1.0
    status: str = "active"

    # ── 时间有效性 ────────────────────────────────────────────
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    # ── Promotion / Compaction 追溯 ─────────────────────────
    promoted_from: str | None = None
    promotion_reason: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    superseded_by: str | None = None

    # ── 序列化 ──────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "kind": self.kind.value,
            "layer": self.layer.value,
            "scope": self.scope.value,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "source": self.source,
            "confidence": self.confidence,
            "status": self.status,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "promoted_from": self.promoted_from,
            "promotion_reason": self.promotion_reason,
            "evidence_refs": self.evidence_refs,
            "superseded_by": self.superseded_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEnvelope:
        def _parse_dt(val: Any) -> datetime | None:
            if val is None:
                return None
            if isinstance(val, datetime):
                return val
            try:
                return datetime.fromisoformat(str(val))
            except (ValueError, TypeError):
                return None

        return cls(
            memory_id=data.get("memory_id", f"mem_{uuid4().hex[:12]}"),
            kind=MemoryKind(data["kind"]) if "kind" in data else MemoryKind.conversation,
            layer=MemoryLayer(data["layer"]) if "layer" in data else MemoryLayer.recall,
            scope=MemoryScope(data["scope"]) if "scope" in data else MemoryScope.session,
            trace_id=data.get("trace_id", ""),
            run_id=data.get("run_id", ""),
            task_id=data.get("task_id", ""),
            session_id=data.get("session_id"),
            workspace_id=data.get("workspace_id"),
            content=data.get("content", {}),
            created_at=_parse_dt(data.get("created_at")) or datetime.now(timezone.utc),
            updated_at=_parse_dt(data.get("updated_at")),
            source=data.get("source", ""),
            confidence=float(data.get("confidence", 1.0)),
            status=data.get("status", "active"),
            valid_from=_parse_dt(data.get("valid_from")),
            valid_to=_parse_dt(data.get("valid_to")),
            promoted_from=data.get("promoted_from"),
            promotion_reason=data.get("promotion_reason"),
            evidence_refs=data.get("evidence_refs", []),
            superseded_by=data.get("superseded_by"),
        )


# ── 工厂函数 ────────────────────────────────────────────────


def envelope_from_task_context(
    task_ctx_ids: dict[str, str | None],
    *,
    kind: MemoryKind,
    layer: MemoryLayer,
    scope: MemoryScope,
    content: dict[str, Any],
    source: str = "",
    confidence: float = 1.0,
    evidence_refs: list[str] | None = None,
) -> MemoryEnvelope:
    """从 TaskContext.id_dict() 快速构造 envelope。

    典型用法：
        ids = task_context.id_dict()
        env = envelope_from_task_context(ids, kind=..., layer=..., ...)
    """
    return MemoryEnvelope(
        kind=kind,
        layer=layer,
        scope=scope,
        trace_id=task_ctx_ids.get("trace_id") or "",
        run_id=task_ctx_ids.get("run_id") or "",
        task_id=task_ctx_ids.get("task_id") or "",
        session_id=task_ctx_ids.get("session_id"),
        workspace_id=task_ctx_ids.get("workspace_id"),
        content=content,
        source=source,
        confidence=confidence,
        evidence_refs=evidence_refs or [],
    )


def conversation_envelope(
    task_ctx_ids: dict[str, str | None],
    *,
    role: str,
    text: str,
    extra_metadata: dict[str, Any] | None = None,
) -> MemoryEnvelope:
    """快捷构造对话记忆 envelope。"""
    content: dict[str, Any] = {"role": role, "text": text}
    if extra_metadata:
        content["metadata"] = extra_metadata
    return envelope_from_task_context(
        task_ctx_ids,
        kind=MemoryKind.conversation,
        layer=MemoryLayer.recall,
        scope=MemoryScope.session,
        content=content,
        source="brain",
    )


def tool_call_envelope(
    task_ctx_ids: dict[str, str | None],
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_result: Any,
    status: str = "success",
    latency_ms: int = 0,
) -> MemoryEnvelope:
    """快捷构造工具调用记忆 envelope。"""
    return envelope_from_task_context(
        task_ctx_ids,
        kind=MemoryKind.tool_call,
        layer=MemoryLayer.recall,
        scope=MemoryScope.task_run,
        content={
            "tool_name": tool_name,
            "tool_args": tool_args,
            "tool_result": tool_result,
            "status": status,
            "latency_ms": latency_ms,
        },
        source="tool_executor",
    )


def fact_envelope(
    task_ctx_ids: dict[str, str | None],
    *,
    subject: str,
    predicate: str,
    object_value: str,
    confidence: float = 0.8,
    evidence_refs: list[str] | None = None,
    source: str = "promotion",
) -> MemoryEnvelope:
    """快捷构造事实记忆 envelope（用于 archival 写入或 promotion）。"""
    return envelope_from_task_context(
        task_ctx_ids,
        kind=MemoryKind.fact,
        layer=MemoryLayer.archival,
        scope=MemoryScope.global_,
        content={
            "subject": subject,
            "predicate": predicate,
            "object": object_value,
        },
        source=source,
        confidence=confidence,
        evidence_refs=evidence_refs or [],
    )
