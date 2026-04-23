"""Pulse Promotion Engine — P2 内核组件

对应设计文档 §9.3-9.4: recall → archival 事实晋升。

晋升流程 (§9.4):
  1. Detection:  从 recall 中提取候选事实（规则或 LLM）
  2. Validation: 检查冲突、时效、evidence 充分性
  3. Approval:   低风险自动，中高风险经 Governance
  4. Write:      写入 archival，保留 evidence_refs
  5. Supersede:  若是更新事实，旧记录标记 superseded_by
  6. Audit:      进入审计轨迹

三条晋升路径 (§9.3):
  - Recall → Archival:   检测到稳定事实
  - Recall → Core:       检测到偏好/身份变更
  - Workspace → Archival: workspace summary 中出现稳定事实

PromotionEngine 不直接调用 LLM，通过 PromotionStrategy 接口解耦。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from .hooks import HookPoint, HookRegistry
from .task_context import TaskContext
from .memory.envelope import (
    MemoryEnvelope,
    MemoryKind,
    MemoryLayer,
    MemoryScope,
)

logger = logging.getLogger(__name__)


class PromotionPath(str, Enum):
    recall_to_archival = "recall→archival"
    recall_to_core = "recall→core"
    workspace_to_archival = "workspace→archival"


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


@dataclass
class FactCandidate:
    """一个待晋升的事实候选。"""

    subject: str
    predicate: str
    object_value: str
    confidence: float = 0.0
    evidence_refs: list[str] = field(default_factory=list)
    source_memory_id: str = ""
    risk: RiskLevel = RiskLevel.low


@dataclass
class PromotionResult:
    """单次晋升的结果。"""

    promoted: bool
    path: PromotionPath
    candidate: FactCandidate
    reason: str = ""
    conflict_with: str | None = None
    elapsed_ms: int = 0


class PromotionStrategy(Protocol):
    """事实提取策略接口。"""

    def extract_candidates(
        self,
        entries: list[dict[str, Any]],
    ) -> list[FactCandidate]: ...


class RulePromotionStrategy:
    """基于规则的事实提取 — 零 LLM 成本。

    规则:
      - 如果同一 subject+predicate 在 recall 中出现 >= threshold 次，视为稳定事实
      - confidence 取出现频率的归一化值
    """

    def __init__(self, *, min_occurrences: int = 2, min_confidence: float = 0.7) -> None:
        self._min_occurrences = min_occurrences
        self._min_confidence = min_confidence

    def extract_candidates(
        self,
        entries: list[dict[str, Any]],
    ) -> list[FactCandidate]:
        freq: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for entry in entries:
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            # 简单启发式: 寻找 "X is Y" / "X are Y" 模式
            for pattern in (" is ", " are ", " was ", " were "):
                if pattern in text.lower():
                    idx = text.lower().index(pattern)
                    subj = text[:idx].strip()[-60:]
                    obj = text[idx + len(pattern):].strip()[:120]
                    pred = pattern.strip()
                    if subj and obj:
                        key = (subj.lower(), pred)
                        freq.setdefault(key, []).append({
                            "subject": subj, "predicate": pred,
                            "object": obj, "id": str(entry.get("id", "")),
                        })
                    break

        candidates: list[FactCandidate] = []
        for key, occurrences in freq.items():
            if len(occurrences) < self._min_occurrences:
                continue
            best = occurrences[-1]
            conf = min(1.0, len(occurrences) / (self._min_occurrences * 2))
            if conf < self._min_confidence:
                continue
            candidates.append(FactCandidate(
                subject=best["subject"],
                predicate=best["predicate"],
                object_value=best["object"],
                confidence=conf,
                evidence_refs=[o["id"] for o in occurrences if o.get("id")],
                source_memory_id=best.get("id", ""),
            ))
        return candidates


class PromotionEngine:
    """管理三条晋升路径 (§9.3):
      - recall → archival: 稳定事实
      - recall → core: 偏好/身份变更
      - workspace → archival: workspace summary 中的稳定事实
    """

    CORE_PREDICATES = frozenset({
        "prefers", "preference", "identity", "name", "role",
        "likes", "dislikes", "style", "language", "timezone",
    })

    def __init__(
        self,
        *,
        strategy: PromotionStrategy | None = None,
        hooks: HookRegistry | None = None,
        archival_memory: Any | None = None,
        core_memory: Any | None = None,
    ) -> None:
        self._strategy = strategy or RulePromotionStrategy()
        self._hooks = hooks
        self._archival = archival_memory
        self._core = core_memory

    def promote(
        self,
        ctx: TaskContext,
        recall_entries: list[dict[str, Any]],
    ) -> list[PromotionResult]:
        """从 recall entries 中提取候选事实并晋升到 archival。"""
        t0 = time.monotonic()
        candidates = self._strategy.extract_candidates(recall_entries)
        if not candidates:
            return []

        results: list[PromotionResult] = []
        for candidate in candidates:
            result = self._promote_one(ctx, candidate)
            result.elapsed_ms = int((time.monotonic() - t0) * 1000)
            results.append(result)

        logger.info(
            "Promotion: %d candidates → %d promoted, task=%s",
            len(candidates),
            sum(1 for r in results if r.promoted),
            ctx.task_id,
        )
        return results

    def _promote_one(self, ctx: TaskContext, candidate: FactCandidate) -> PromotionResult:
        """执行单个候选的晋升流程，自动选择路径。"""
        path = self._resolve_path(candidate)

        # Step 2: Validation — 冲突检测（记录 conflict_id，不直接拒绝）
        conflict_id = self._check_conflict(candidate) if path != PromotionPath.recall_to_core else None

        # Step 3: Approval — Hook 可阻断
        if self._hooks is not None:
            hook_result = self._hooks.fire(
                HookPoint.before_promotion, ctx,
                {
                    "subject": candidate.subject,
                    "predicate": candidate.predicate,
                    "object": candidate.object_value,
                    "confidence": candidate.confidence,
                    "risk": candidate.risk.value,
                },
            )
            if hook_result.block:
                return PromotionResult(
                    promoted=False, path=path, candidate=candidate,
                    reason=f"blocked by hook: {hook_result.reason}",
                )

        # Step 4: Write — 根据路径写入不同目标
        envelope = self._to_envelope(ctx, candidate)
        if path == PromotionPath.recall_to_core:
            if self._core is not None:
                try:
                    # CoreMemory.update_block 是 keyword-only 且 block 必须是
                    # soul/user/prefs/context 之一. 不匹配则落 prefs[<predicate>].
                    pred_key = candidate.predicate.lower().replace(" ", "_")
                    if pred_key in ("soul", "user", "prefs", "context"):
                        block_name = pred_key
                        block_content: Any = (
                            candidate.object_value
                            if isinstance(candidate.object_value, dict)
                            else {"value": candidate.object_value}
                        )
                    else:
                        block_name = "prefs"
                        block_content = {pred_key: candidate.object_value}
                    self._core.update_block(
                        block=block_name, content=block_content, merge=True,
                    )
                except (AttributeError, TypeError, ValueError) as exc:
                    return PromotionResult(
                        promoted=False, path=path, candidate=candidate,
                        reason=f"core write failed: {exc}",
                    )
            else:
                return PromotionResult(
                    promoted=False, path=path, candidate=candidate,
                    reason="no core_memory available",
                )
        else:
            if self._archival is not None:
                try:
                    self._archival.store_envelope(envelope)
                except Exception as exc:
                    return PromotionResult(
                        promoted=False, path=path, candidate=candidate,
                        reason=f"write failed: {exc}",
                    )

        # Step 5: Supersede — 冲突时标记旧 fact 的 superseded_by
        if conflict_id is not None and self._archival is not None:
            try:
                self._archival.supersede_fact(
                    old_fact_id=conflict_id,
                    new_fact_id=envelope.memory_id,
                )
                logger.info("Superseded fact %s with %s", conflict_id, envelope.memory_id)
            except (AttributeError, TypeError, RuntimeError) as exc:
                logger.warning("Supersede failed for fact %s: %s", conflict_id, exc)

        # Step 6: Audit (via Hook)
        if self._hooks is not None:
            self._hooks.fire(
                HookPoint.after_promotion, ctx,
                {
                    "subject": candidate.subject,
                    "predicate": candidate.predicate,
                    "object": candidate.object_value,
                    "promoted": True,
                },
            )

        return PromotionResult(promoted=True, path=path, candidate=candidate)

    def _check_conflict(self, candidate: FactCandidate) -> str | None:
        """检查 archival 中是否存在冲突事实。"""
        if self._archival is None:
            return None
        try:
            existing = self._archival.search(
                query=f"{candidate.subject} {candidate.predicate}",
                limit=3,
            )
        except (AttributeError, TypeError, RuntimeError) as exc:
            logger.debug("Conflict check failed: %s", exc)
            return None

        for fact in existing:
            subj = str(fact.get("subject") or "").lower()
            pred = str(fact.get("predicate") or "").lower()
            obj = str(fact.get("object") or "").lower()
            if (subj == candidate.subject.lower()
                    and pred == candidate.predicate.lower()
                    and obj != candidate.object_value.lower()):
                return str(fact.get("id", "unknown"))
        return None

    def _to_envelope(self, ctx: TaskContext, candidate: FactCandidate) -> MemoryEnvelope:
        ids = ctx.id_dict()
        path = self._resolve_path(candidate)
        layer = MemoryLayer.core if path == PromotionPath.recall_to_core else MemoryLayer.archival
        return MemoryEnvelope(
            kind=MemoryKind.fact,
            layer=layer,
            scope=MemoryScope.workspace,
            trace_id=ids.get("trace_id") or "",
            run_id=ids.get("run_id") or "",
            task_id=ids.get("task_id") or "",
            session_id=ids.get("session_id") or "",
            workspace_id=ids.get("workspace_id"),
            content={
                "subject": candidate.subject,
                "predicate": candidate.predicate,
                "object": candidate.object_value,
            },
            source="promotion_engine",
            confidence=candidate.confidence,
            evidence_refs=candidate.evidence_refs,
            promoted_from=candidate.source_memory_id,
            promotion_reason=f"rule: {len(candidate.evidence_refs)} occurrences",
        )

    def _resolve_path(self, candidate: FactCandidate) -> PromotionPath:
        """根据 predicate 自动选择晋升路径。"""
        pred_lower = candidate.predicate.lower().replace(" ", "_")
        if pred_lower in self.CORE_PREDICATES:
            return PromotionPath.recall_to_core
        return PromotionPath.recall_to_archival

    def promote_from_workspace(
        self,
        ctx: TaskContext,
        workspace_facts: list[dict[str, Any]],
    ) -> list[PromotionResult]:
        """workspace → archival 晋升路径 (§9.3)。

        从 workspace facts 中提取候选并晋升到 archival。
        """
        candidates = self._strategy.extract_candidates(workspace_facts)
        if not candidates:
            return []

        results: list[PromotionResult] = []
        for candidate in candidates:
            candidate.risk = RiskLevel.low
            result = self._promote_one(ctx, candidate)
            result.path = PromotionPath.workspace_to_archival
            results.append(result)

        logger.info(
            "Workspace promotion: %d candidates → %d promoted",
            len(candidates),
            sum(1 for r in results if r.promoted),
        )
        return results
