from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..learning.domain_preference_dispatcher import (
    DomainPreferenceDispatcher,
)
from ..learning.preference_extractor import (
    DomainPref,
    PreferenceExtraction,
    PreferenceExtractor,
)

logger = logging.getLogger(__name__)


def _is_correction_text(text: str) -> bool:
    safe = str(text or "").strip().lower()
    if not safe:
        return False
    tokens = (
        "你错了",
        "不对",
        "纠正",
        "应该",
        "以后",
        "默认",
        "不要再",
        "我喜欢",
        "我不喜欢",
    )
    return any(token in safe for token in tokens)


@dataclass(slots=True)
class PreCaptureResult:
    """Pre-turn domain preference capture 的返回值 (F8).

    Brain 在 ReAct 开始前调 ``pre_capture_domain``, 把用户本轮新的业务域偏好
    (hard_constraint / memory item) 立即派发到 DomainMemory. 这保证当轮
    system prompt 里的 Job Snapshot section 能看到本轮偏好 — 否则 reflection
    阶段(post-turn)的 dispatch 要到下一轮才生效, 当轮 agent 依然"看不见"
    用户刚说的限制, 容易选错工具/放宽约束.

    - ``extraction``: 本次 extract 的完整结构化输出, 供 reflect_interaction 复用,
      避免对同一句话做两次 LLM extraction.
    - ``domain_applied``: 每条 domain_prefs 的派发结果 (dict 形式), 反馈给
      reflect_interaction 以合并到最终 EvolutionResult.
    - ``already_dispatched``: True 表示 reflect 不应再重复 dispatch domain_prefs;
      False 通常是 dispatcher 未绑定或没有 domain_prefs 的情况.
    """

    extraction: PreferenceExtraction
    domain_applied: list[dict[str, Any]]
    already_dispatched: bool


@dataclass(slots=True)
class EvolutionResult:
    classification: str
    preference_applied: list[dict[str, Any]]
    soul_applied: list[dict[str, Any]]
    belief_applied: list[dict[str, Any]]
    archival_facts: list[dict[str, Any]]
    # 新增: domain_prefs 派发结果(每条对应 PreferenceExtraction.domain_prefs 一项).
    # 旧字段保留以维持 API 兼容; 旧调用方只会看到 preference_applied(core path).
    domain_applied: list[dict[str, Any]] = None  # type: ignore[assignment]
    dpo_collected: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.domain_applied is None:
            self.domain_applied = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "preference_applied": list(self.preference_applied),
            "soul_applied": list(self.soul_applied),
            "belief_applied": list(self.belief_applied),
            "archival_facts": list(self.archival_facts),
            "domain_applied": list(self.domain_applied),
            "dpo_collected": dict(self.dpo_collected) if isinstance(self.dpo_collected, dict) else None,
        }


class SoulEvolutionEngine:
    """Reflection pipeline: classify -> extract -> govern -> archive -> dispatch_domain."""

    # CoreMemory.prefs 只放跨域用户级偏好, 不放业务域策略.
    # 扩展新 key 时请同步 docs/Pulse-DomainMemory与Tool模式.md §2.2 边界说明。
    CORE_PREF_KEYS = frozenset({
        "preferred_name",
        "language",
        "timezone",
        "default_location",
        "like",
        "dislike",
        "response_style",
        "response_tone",
        "response_length",
        "verbosity",
        "communication_preference",
    })

    def __init__(
        self,
        *,
        governance: Any,
        archival_memory: Any,
        preference_extractor: PreferenceExtractor | None = None,
        domain_preference_dispatcher: DomainPreferenceDispatcher | None = None,
        dpo_collector: Any | None = None,
        dpo_auto_collect: bool = True,
    ) -> None:
        self._governance = governance
        self._archival_memory = archival_memory
        self._extractor = preference_extractor or PreferenceExtractor()
        self._domain_dispatcher = domain_preference_dispatcher
        self._dpo_collector = dpo_collector
        self._dpo_auto_collect = bool(dpo_auto_collect)

    def bind_domain_preference_dispatcher(
        self,
        dispatcher: DomainPreferenceDispatcher | None,
    ) -> None:
        """允许 server 装配完成后延迟绑定(与 EventBus 绑定顺序一致)."""
        self._domain_dispatcher = dispatcher

    def pre_capture_domain(
        self,
        *,
        user_text: str,
        metadata: dict[str, Any] | None = None,
    ) -> PreCaptureResult:
        """轮内预捕获业务域偏好, 让本轮 Prompt 立刻看到 (F8).

        只做 ``extract + domain_prefs dispatch``; **不动 core_prefs / soul /
        archival / DPO** — 那些由 post-turn 的 ``reflect_interaction`` 处理.

        这条路径的必要性:
          - reflection 跑在 turn 结束之后, JobMemory 对当轮不可见;
          - 没有预捕获, 用户\"希望只投杭州\"这种**本轮**硬约束永远要到下一轮
            才反映在 Job Snapshot 里, 当轮 agent 自然就绕开业务边界.

        返回的 ``extraction`` 会透传给 ``reflect_interaction`` 复用, 保证对
        同一条 user_text 只做一次 LLM 抽取.
        """
        safe_user_text = str(user_text or "").strip()
        if not safe_user_text:
            return PreCaptureResult(
                extraction=PreferenceExtraction(),
                domain_applied=[],
                already_dispatched=False,
            )
        safe_metadata = dict(metadata or {})
        extracted = self._extractor.extract(safe_user_text)
        domain_prefs = list(self._collect_domain_prefs(extracted))
        if not domain_prefs:
            return PreCaptureResult(
                extraction=extracted,
                domain_applied=[],
                already_dispatched=False,
            )
        if self._domain_dispatcher is None:
            logger.warning(
                "pre_capture_domain: %d domain_prefs detected but no dispatcher bound; "
                "preferences will not be persisted pre-turn (post-turn reflection will "
                "log another 'no_dispatcher_bound' warning)",
                len(domain_prefs),
            )
            return PreCaptureResult(
                extraction=extracted,
                domain_applied=[],
                already_dispatched=False,
            )
        dispatch_context = self._build_dispatch_context(safe_metadata, "pre_turn")
        dispatch_results = self._domain_dispatcher.dispatch(
            domain_prefs, context=dispatch_context
        )
        domain_applied = [r.to_dict() for r in dispatch_results]
        logger.info(
            "pre_capture_domain dispatched=%d (ok=%d) workspace=%s trace=%s",
            len(domain_applied),
            sum(1 for r in domain_applied if r.get("status") == "applied"),
            safe_metadata.get("workspace_id"),
            safe_metadata.get("trace_id"),
        )
        return PreCaptureResult(
            extraction=extracted,
            domain_applied=domain_applied,
            already_dispatched=True,
        )

    def reflect_interaction(
        self,
        *,
        user_text: str,
        assistant_text: str,
        metadata: dict[str, Any] | None = None,
        precaptured: PreCaptureResult | None = None,
    ) -> EvolutionResult:
        safe_user_text = str(user_text or "").strip()
        safe_metadata = dict(metadata or {})

        classification = "correction" if _is_correction_text(safe_user_text) else "regular"
        # 复用 pre_capture_domain 已经做过的 extraction, 避免对同一句话重复
        # LLM 调用 (F8 配套: 第一性地把 extract + dispatch 拆成 pre/post 两段).
        if precaptured is not None:
            extracted = precaptured.extraction
        else:
            extracted = self._extractor.extract(safe_user_text)
        core_pref_updates, non_core_pref_updates = self._split_pref_updates(extracted.core_prefs)

        preference_applied: list[dict[str, Any]] = []
        soul_applied: list[dict[str, Any]] = []
        belief_applied: list[dict[str, Any]] = []
        archival_facts: list[dict[str, Any]] = []
        domain_applied: list[dict[str, Any]] = []
        dpo_collected: dict[str, Any] | None = None

        # --- 1) core_prefs 里混入的业务域键(LLM 偶尔写错位置)也分流给 dispatcher ---
        # 把 non-core 的 core_prefs 当作 "legacy domain_prefs hints" 看, 降级记录
        # 但不再尝试写 CoreMemory. 保留可审计日志.
        if non_core_pref_updates:
            logger.info(
                "evolution skip non-core preference keys=%s (belong to DomainMemory, "
                "should be emitted as domain_prefs by extractor)",
                sorted(non_core_pref_updates.keys())[:12],
            )
            preference_applied.append({
                "ok": False,
                "status": "skipped_non_core",
                "reason": "non-core domain preference should be persisted via domain_prefs",
                "skipped_keys": sorted(non_core_pref_updates.keys()),
                "skipped_updates": non_core_pref_updates,
            })

        # --- 2) core_prefs 进 governance → CoreMemory.prefs ---
        if core_pref_updates:
            prefs_risk = self._infer_pref_risk(core_pref_updates)
            pref_result = self._governance.apply_preference_updates(
                updates=core_pref_updates,
                source=f"evolution:{classification}",
                risk_level=prefs_risk,
            )
            preference_applied.append(pref_result)
            if pref_result.get("ok"):
                for key, value in core_pref_updates.items():
                    fact = self._archival_memory.add_fact(
                        subject="user",
                        predicate=f"preference.{key}",
                        object_value=value,
                        source="preference_extractor",
                        confidence=0.9,
                        metadata={"classification": classification, "metadata": safe_metadata},
                    )
                    archival_facts.append(fact)
                belief_text = ", ".join(f"{k}={v}" for k, v in core_pref_updates.items())
                belief_result = self._governance.add_mutable_belief(
                    belief=f"User preference updated: {belief_text}",
                    source="evolution_reflection",
                    risk_level="low",
                )
                belief_applied.append(belief_result)

        # --- 3) soul_updates 进 governance → Soul ---
        if extracted.soul_updates:
            soul_risk = self._infer_soul_risk(extracted.soul_updates)
            soul_result = self._governance.apply_soul_update(
                updates=extracted.soul_updates,
                source=f"evolution:{classification}",
                risk_level=soul_risk,
            )
            soul_applied.append(soul_result)
            if soul_result.get("ok"):
                for key, value in extracted.soul_updates.items():
                    fact = self._archival_memory.add_fact(
                        subject="assistant",
                        predicate=f"soul.{key}",
                        object_value=value,
                        source="soul_governance",
                        confidence=0.7,
                        metadata={"classification": classification, "metadata": safe_metadata},
                    )
                    archival_facts.append(fact)

        # --- 4) domain_prefs 走 DomainPreferenceDispatcher → DomainMemory ---
        # 第一性修复: 这条通路让"用户自然语言偏好 → 业务域记忆"变成架构强制,
        # 不再依赖 LLM 是否记得调 job.memory.record/hard_constraint.set 工具.
        if precaptured is not None and precaptured.already_dispatched:
            # F8: pre_capture_domain 已经在本轮开始前把 domain_prefs 派发过了,
            # 这里不能再 dispatch 一次 — memory.record 会产生**重复条目** (每条
            # MemoryItem 的 id 是新 uuid, 幂等不依靠自然键). 直接把 pre 阶段的
            # 结果透传到 EvolutionResult, 保留审计完整性.
            domain_applied.extend(precaptured.domain_applied)
            logger.debug(
                "evolution: reuse %d pre_captured domain dispatches",
                len(precaptured.domain_applied),
            )
        else:
            domain_prefs = list(self._collect_domain_prefs(extracted))
            if domain_prefs:
                if self._domain_dispatcher is None:
                    logger.warning(
                        "evolution: domain_prefs=%d but no dispatcher bound; "
                        "preferences will not be persisted",
                        len(domain_prefs),
                    )
                    for pref in domain_prefs:
                        domain_applied.append({
                            "domain": pref.domain,
                            "op": pref.op,
                            "status": "skipped",
                            "reason": "no_dispatcher_bound",
                            "evidence": pref.evidence,
                            "confidence": pref.confidence,
                        })
                else:
                    dispatch_context = self._build_dispatch_context(safe_metadata, classification)
                    dispatch_results = self._domain_dispatcher.dispatch(
                        domain_prefs,
                        context=dispatch_context,
                    )
                    domain_applied.extend(r.to_dict() for r in dispatch_results)

        # --- 5) DPO 采集(保持原逻辑) ---
        if self._dpo_collector is not None and classification == "correction":
            collect_dpo_raw = safe_metadata.get("collect_dpo")
            collect_dpo = self._dpo_auto_collect if collect_dpo_raw is None else bool(collect_dpo_raw)
            if collect_dpo:
                chosen_raw = safe_metadata.get("dpo_chosen")
                rejected_raw = safe_metadata.get("dpo_rejected")
                chosen = str(chosen_raw).strip() if isinstance(chosen_raw, str) else ""
                rejected = str(rejected_raw).strip() if isinstance(rejected_raw, str) else ""
                if not chosen:
                    chosen = f"Follow user correction: {safe_user_text[:300]}"
                if not rejected:
                    rejected = str(assistant_text or "").strip() or "N/A"
                try:
                    dpo_collected = self._dpo_collector.add_pair(
                        prompt=safe_user_text,
                        chosen=chosen,
                        rejected=rejected,
                        metadata={"source": "evolution_reflection", "metadata": safe_metadata},
                    )
                except (AttributeError, TypeError, ValueError) as exc:
                    # DPO 采集失败不能阻塞 reflection 主流程; 但不静默, 记警告.
                    logger.warning(
                        "evolution: DPO collection failed (%s): %s",
                        type(exc).__name__, str(exc)[:200],
                    )
                    dpo_collected = None

        return EvolutionResult(
            classification=classification,
            preference_applied=preference_applied,
            soul_applied=soul_applied,
            belief_applied=belief_applied,
            archival_facts=archival_facts,
            domain_applied=domain_applied,
            dpo_collected=dpo_collected,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _collect_domain_prefs(extracted: PreferenceExtraction) -> list[DomainPref]:
        # extractor 已保证是 list[DomainPref]; 这里只做一层防御复制.
        raw = list(extracted.domain_prefs or [])
        return [p for p in raw if isinstance(p, DomainPref)]

    @staticmethod
    def _build_dispatch_context(
        metadata: dict[str, Any],
        classification: str,
    ) -> dict[str, Any]:
        def _pick(*keys: str) -> Any:
            for key in keys:
                value = metadata.get(key)
                if value:
                    return value
            return None

        return {
            "workspace_id": _pick("workspace_id", "job_workspace_id"),
            "trace_id": _pick("trace_id"),
            "session_id": _pick("session_id"),
            "task_id": _pick("task_id"),
            "run_id": _pick("run_id"),
            "actor": f"soul.reflection:{classification}",
        }

    @classmethod
    def _split_pref_updates(cls, updates: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """拆分 core 可写与非 core 偏好(防御层).

        extractor 已按 docs/Pulse-DomainMemory与Tool模式.md §2.2 把业务域
        偏好放进 ``domain_prefs``, 理论上 ``core_prefs`` 里只会剩 allowlist 键.
        这里再过一遍是为了防 LLM 抽漏把业务偏好放错位置.

        规则:
          - allowlist + ``global_`` 前缀 → CoreMemory.prefs
          - 其余全部视为业务域偏好, 不在此处写 Core (仅做审计日志)
        """
        core_updates: dict[str, Any] = {}
        non_core_updates: dict[str, Any] = {}
        for key, value in dict(updates or {}).items():
            safe_key = str(key or "").strip()
            if not safe_key:
                continue
            lowered = safe_key.lower()
            if lowered in cls.CORE_PREF_KEYS or lowered.startswith("global_"):
                core_updates[safe_key] = value
            else:
                non_core_updates[safe_key] = value
        return core_updates, non_core_updates

    @staticmethod
    def _infer_pref_risk(updates: dict[str, Any]) -> str:
        keys = {str(key).strip().lower() for key in updates.keys()}
        if not keys:
            return "low"
        if "default_location" in keys and len(keys) == 1:
            return "low"
        if "preferred_name" in keys and len(keys) == 1:
            return "low"
        if "like" in keys or "dislike" in keys:
            return "medium"
        if len(keys) >= 3:
            return "high"
        return "medium"

    @staticmethod
    def _infer_soul_risk(updates: dict[str, Any]) -> str:
        keys = {str(key).strip().lower() for key in updates.keys()}
        if not keys:
            return "medium"
        if "style_rules" in keys:
            return "high"
        return "medium"
