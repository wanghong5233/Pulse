"""Pulse Prompt Contract Builder — P1 内核组件

根据 ExecutionMode 组装不同的 system prompt，对应设计文档 §7.1-7.4。

六类 Prompt Contract:
  - systemPrompt:     interactiveTurn — 完整身份/记忆/工具/边界
  - heartbeatPrompt:  heartbeatTurn — workspace essentials + 巡视目标
  - taskPrompt:       detachedScheduledTask / subagentTask — 任务目标/成功条件/允许工具
  - compactPrompt:    压缩阶段 — 保留目标/已完成/待办/关键发现/用户纠正
  - promotionPrompt:  晋升阶段 — 提取事实/偏好/规则/证据/冲突候选
  - recoveryPrompt:   resumedTask — checkpoint/已完成步骤/失败点/下一步

组装顺序 (interactiveTurn 为例):
  1. Soul / Identity
  2. User Profile / Preferences
  3. Workspace Summary
  4. Recent Recall
  5. Relevant Archival Facts
  6. Tool Menu
  7. Safety Boundaries
  8. Current Task / User Query
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from .task_context import ExecutionMode, TaskContext
from .memory_reader import IsolatedMemoryReader
from .tokenizer import DEFAULT_INPUT_BUDGET, count_tokens
from .tool import ToolSpec

logger = logging.getLogger(__name__)


# Section priority for budget-aware drop. Lower number = higher priority;
# the budget allocator drops the *highest* numbered sections first when
# the total token count exceeds ``max_input_tokens``. P0 sections never
# drop — if they alone exceed the budget the build raises (§1.1 暴露优于兜底).
class SectionPriority(int, Enum):
    """Drop priority for prompt sections (higher = drop earlier).

    P0 — IDENTITY/TOOLS/OUTPUT_CONTRACT/BOUNDARIES: irreplaceable. Without
        them the agent loses identity/capabilities/safety frame.
    P1 — DOMAIN/PROFILE/PREFS/WORKSPACE: necessary for in-task correctness
        (e.g. JobMemory snapshot drives JD matching). Drop only when no P2/P3
        candidates remain.
    P2 — RECENT: nice-to-have continuity, can be windowed.
    P3 — RECALL/ARCHIVAL: similarity-ranked, lowest signal-to-noise — drop first.
    """

    IDENTITY = 0
    TOOLS = 0
    OUTPUT_CONTRACT = 0
    BOUNDARIES = 0
    USER_PROFILE = 1
    USER_PREFS = 1
    WORKSPACE = 1
    DOMAIN_SNAPSHOT = 1
    RECENT = 2
    RECALL = 3
    ARCHIVAL = 3


@dataclass(frozen=True)
class PromptSection:
    """A typed prompt fragment with drop-priority metadata."""

    name: str
    text: str
    priority: SectionPriority

    @property
    def is_empty(self) -> bool:
        return not (self.text and self.text.strip())

# Domain snapshot provider: 由业务域 (Job / Mail / Health / ...) 注册,
# Brain 不知道具体 domain, 只负责按顺序调用并把返回的 markdown 追加到 system prompt。
# Provider 应当是**只读 + 尽量短 (< 2KB)** 的, 异常时应返回空字符串不要抛出。
# 见 docs/Pulse-DomainMemory与Tool模式.md §3.3 / §5.3 / §8.2。
DomainSnapshotProvider = Callable[[TaskContext], str]


class ContractType(str, Enum):
    system = "systemPrompt"
    heartbeat = "heartbeatPrompt"
    task = "taskPrompt"
    compact = "compactPrompt"
    promotion = "promotionPrompt"
    recovery = "recoveryPrompt"


_MODE_TO_CONTRACT: dict[ExecutionMode, ContractType] = {
    ExecutionMode.interactive_turn: ContractType.system,
    ExecutionMode.heartbeat_turn: ContractType.heartbeat,
    ExecutionMode.detached_scheduled_task: ContractType.task,
    ExecutionMode.subagent_task: ContractType.task,
    ExecutionMode.resumed_task: ContractType.recovery,
}


class MemoryReader(Protocol):
    """Memory 层提供给 PromptContract 的只读接口。"""

    def read_core_snapshot(self) -> dict[str, Any]: ...
    def read_recent(self, session_id: str | None, limit: int) -> list[dict[str, Any]]: ...
    def search_recall(self, query: str, session_id: str | None, top_k: int) -> list[dict[str, Any]]: ...
    def search_archival(self, query: str, limit: int) -> list[dict[str, Any]]: ...
    def read_workspace_essentials(self, workspace_id: str | None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class PromptContract:
    """一次 prompt 组装的产物 (post-budget)。

    ``sections`` 是经过预算裁剪后**保留**的段落 (按渲染顺序),
    ``token_estimate`` 是基于真实 tokenizer (tiktoken / heuristic) 的合计.
    ``dropped_sections`` 记录被 budget 砍掉的段名供调用方观察 (审计 / 告警).
    """

    contract_type: ContractType
    sections: list[str]
    token_estimate: int
    dropped_sections: tuple[str, ...] = field(default_factory=tuple)

    @property
    def text(self) -> str:
        return "\n\n".join(s for s in self.sections if s)


class PromptContractBuilder:
    """根据 TaskContext 和 Memory 状态组装 prompt。"""

    DEFAULT_MAX_INPUT_TOKENS = DEFAULT_INPUT_BUDGET
    DEFAULT_TOKENIZER_MODEL = "gpt-4o-mini"

    def __init__(
        self,
        *,
        memory: MemoryReader | None = None,
        tool_names: list[str] | None = None,
        tool_specs: list[ToolSpec] | None = None,
        recent_limit: int = 8,
        archival_limit: int = 5,
        recall_top_k: int = 4,
        recall_min_similarity: float = 0.15,
        domain_snapshot_providers: list[DomainSnapshotProvider] | None = None,
        max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
        tokenizer_model: str = DEFAULT_TOKENIZER_MODEL,
    ) -> None:
        # ToolUseContract §4.1 契约 A:
        #   优先使用 ``tool_specs`` 渲染三段式 (description + when_to_use + when_not_to_use);
        #   仅 ``tool_names`` 时退化为 name 列表 (向后兼容已有测试 / 外部调用).
        # ``recall_min_similarity`` (P1-B, audit trace_f3bda835ed94):
        #   ``search_recall`` 的后端 (PG / fake) 在没有真正相似命中时会回退到
        #   ``similarity=0.0`` 的 keyword/time 命中, 这些伪相关会把 prompt 撑肿,
        #   还会误导 LLM 以为"同一问题刚答过". 默认阈值 0.15 保守地过滤掉
        #   纯 fallback 命中, 调用方若需要更严/更松的筛选可以显式覆盖.
        # ``max_input_tokens`` / ``tokenizer_model``:
        #   预算治理的两个旋钮. 默认 24k token (qwen-max worst-case 32k 留 8k 给
        #   completion). Brain 在初始化时按 router 的 primary 模型上修.
        self._memory = memory
        self._tool_specs: list[ToolSpec] = list(tool_specs or [])
        self._tool_names: list[str] = list(tool_names or []) or [s.name for s in self._tool_specs]
        self._recent_limit = recent_limit
        self._archival_limit = archival_limit
        self._recall_top_k = recall_top_k
        self._recall_min_similarity = max(0.0, float(recall_min_similarity))
        self._domain_providers: list[DomainSnapshotProvider] = list(domain_snapshot_providers or [])
        self._max_input_tokens = max(1024, int(max_input_tokens))
        self._tokenizer_model = str(tokenizer_model or self.DEFAULT_TOKENIZER_MODEL)

    @property
    def max_input_tokens(self) -> int:
        return self._max_input_tokens

    @property
    def tokenizer_model(self) -> str:
        return self._tokenizer_model

    def register_domain_snapshot_provider(self, provider: DomainSnapshotProvider) -> None:
        """动态注册一个 domain snapshot provider (构造后补注册)。

        典型用法: 业务 module 在 on_startup 时把自己的 snapshot provider 挂上来。
        同一 provider 可重复注册多次 (会被多次调用) — 调用方自行去重。
        """
        if provider is None:
            return
        self._domain_providers.append(provider)

    def _render_domain_snapshots(
        self, ctx: TaskContext
    ) -> list[PromptSection]:
        """按注册顺序调用 providers, 收集非空 section。

        任何 provider 抛异常都降级为警告日志, 不阻断 prompt 组装。
        """
        sections: list[PromptSection] = []
        for provider in self._domain_providers:
            try:
                raw = provider(ctx)
            except Exception as exc:
                logger.warning("domain snapshot provider failed: %s", exc)
                continue
            if isinstance(raw, str) and raw.strip():
                sections.append(
                    PromptSection(
                        name=f"domain:{getattr(provider, '__name__', 'provider')}",
                        text=raw.strip(),
                        priority=SectionPriority.DOMAIN_SNAPSHOT,
                    )
                )
        return sections

    def build(self, ctx: TaskContext, query: str = "") -> PromptContract:
        contract_type = _MODE_TO_CONTRACT.get(ctx.mode, ContractType.system)
        ctx.prompt_contract = contract_type.value

        memory: MemoryReader | None = self._memory
        if memory is not None:
            memory = IsolatedMemoryReader(memory, ctx)

        method_name = _CONTRACT_METHOD.get(contract_type, "_build_system")
        builder = getattr(self, method_name)
        raw_sections: list[PromptSection] = [
            s for s in builder(ctx, query, memory) if not s.is_empty
        ]

        kept, dropped, total_tokens = self._allocate_budget(
            raw_sections, contract_type
        )

        section_diag = [
            (s.name, count_tokens(s.text, model=self._tokenizer_model))
            for s in raw_sections
        ]
        logger.info(
            "prompt_assembled contract=%s mode=%s builder=%s memory_attached=%s "
            "domain_providers=%d sections_in=%d sections_kept=%d "
            "tokens_total=%d budget=%d dropped=%s diag=%s "
            "query_chars=%d session_id=%s task_id=%s",
            contract_type.value,
            getattr(ctx.mode, "value", ctx.mode),
            method_name,
            memory is not None,
            len(self._domain_providers),
            len(raw_sections),
            len(kept),
            total_tokens,
            self._max_input_tokens,
            list(dropped),
            section_diag,
            len(query or ""),
            getattr(ctx, "session_id", None),
            getattr(ctx, "task_id", None),
        )
        return PromptContract(
            contract_type=contract_type,
            sections=[s.text for s in kept],
            token_estimate=total_tokens,
            dropped_sections=dropped,
        )

    # ── Budget allocation ──────────────────────────────────

    def _allocate_budget(
        self,
        sections: list[PromptSection],
        contract_type: ContractType,
    ) -> tuple[list[PromptSection], tuple[str, ...], int]:
        """Drop lowest-priority sections until total tokens ≤ budget.

        Strategy:
          1. Token-count each section with the configured tokenizer.
          2. If sum ≤ budget → keep all (fast path).
          3. Else iteratively drop the *highest-priority-number* (lowest
             importance) section until under budget. Ties broken by *largest*
             section first (drop the biggest contributor of the lowest tier).
          4. If after dropping every P2/P3 section we are still over budget,
             fail loud with a structured ``RuntimeError`` so the caller can
             see which P0/P1 section is too big — silent truncation here
             would just make the model dumber without any signal upstream.
        """
        budget = self._max_input_tokens
        sized: list[tuple[PromptSection, int]] = [
            (s, count_tokens(s.text, model=self._tokenizer_model)) for s in sections
        ]
        total = sum(t for _, t in sized)
        if total <= budget:
            return [s for s, _ in sized], (), total

        keep: list[tuple[PromptSection, int]] = list(sized)
        dropped: list[str] = []
        # Sort drop candidates: highest priority value (=least important) first,
        # ties → largest token count first (biggest savings per drop).
        while total > budget:
            droppable = [
                (idx, sec, toks)
                for idx, (sec, toks) in enumerate(keep)
                if sec.priority.value >= SectionPriority.RECENT.value
            ]
            if not droppable:
                break
            droppable.sort(key=lambda triple: (-triple[1].priority.value, -triple[2]))
            idx, sec, toks = droppable[0]
            dropped.append(sec.name)
            keep.pop(idx)
            total -= toks
            logger.warning(
                "prompt_budget: dropped section name=%s priority=%d tokens=%d "
                "remaining_total=%d budget=%d",
                sec.name, sec.priority.value, toks, total, budget,
            )

        if total > budget:
            # P0/P1 alone exceed budget — config error or runaway memory.
            # §1.1 暴露优于兜底: raise loudly with diagnosis instead of chopping.
            diag = [(s.name, t) for s, t in keep]
            raise RuntimeError(
                f"prompt budget exhausted: contract={contract_type.value} "
                f"total_tokens={total} budget={budget} "
                f"already_dropped={dropped} "
                f"remaining_p0_p1={diag}. "
                "Fix the offending section (likely tool_specs, domain snapshot, "
                "or user_prefs) — silent truncation would degrade model quality."
            )
        return [s for s, _ in keep], tuple(dropped), total

    # ── Contract Builders ──────────────────────────────────
    #
    # Each ``_build_*`` returns ``list[PromptSection]`` (with priority tags);
    # ``build()`` runs budget allocation, drops low-priority sections if
    # over-budget, and finally serialises to ``list[str]`` on the contract.

    def _ps(
        self,
        name: str,
        text: str,
        priority: SectionPriority,
    ) -> PromptSection | None:
        if not (text and text.strip()):
            return None
        return PromptSection(name=name, text=text, priority=priority)

    def _build_system(
        self, ctx: TaskContext, query: str, mem: MemoryReader | None
    ) -> list[PromptSection]:
        """interactiveTurn: 完整 prompt。"""
        candidates = [
            self._ps("identity", self._section_identity(mem), SectionPriority.IDENTITY),
            self._ps("user_profile", self._section_user_profile(mem), SectionPriority.USER_PROFILE),
            self._ps("user_prefs", self._section_user_prefs(mem), SectionPriority.USER_PREFS),
            self._ps("workspace", self._section_workspace(mem, ctx), SectionPriority.WORKSPACE),
            *self._render_domain_snapshots(ctx),
            self._ps("recent", self._section_recent_recall(mem, ctx), SectionPriority.RECENT),
            self._ps("recall", self._section_relevant_recall(mem, query, ctx), SectionPriority.RECALL),
            self._ps("archival", self._section_archival(mem, query), SectionPriority.ARCHIVAL),
            self._ps("tools", self._section_tools(), SectionPriority.TOOLS),
            self._ps("tool_use_policy", self._section_tool_use_policy(), SectionPriority.TOOLS),
            self._ps("command_conventions", self._section_command_conventions(), SectionPriority.TOOLS),
            self._ps("output_contract", self._section_output_contract(), SectionPriority.OUTPUT_CONTRACT),
            self._ps("boundaries", self._section_boundaries(mem), SectionPriority.BOUNDARIES),
        ]
        return [s for s in candidates if s is not None]

    def _build_heartbeat(
        self, ctx: TaskContext, query: str, mem: MemoryReader | None
    ) -> list[PromptSection]:
        """heartbeatTurn: 轻量 prompt，只读 workspace essentials。"""
        candidates = [
            self._ps(
                "heartbeat_intro",
                "You are Pulse in heartbeat mode. "
                "Check workspace status, report anomalies, do NOT start heavy reasoning.",
                SectionPriority.IDENTITY,
            ),
            self._ps("identity_brief", self._section_identity_brief(mem), SectionPriority.IDENTITY),
            self._ps("workspace", self._section_workspace(mem, ctx), SectionPriority.WORKSPACE),
            self._ps("tools", self._section_tools(), SectionPriority.TOOLS),
        ]
        return [s for s in candidates if s is not None]

    def _build_task(
        self, ctx: TaskContext, query: str, mem: MemoryReader | None
    ) -> list[PromptSection]:
        """detachedScheduledTask / subagentTask: 任务聚焦 prompt。"""
        intro = (
            f"You are Pulse executing a scheduled task.\n"
            f"Task ID: {ctx.task_id}\n"
            f"Execution mode: {ctx.mode.value}\n"
            f"Focus on completing the task objective. Be efficient."
        )
        candidates = [
            self._ps("task_intro", intro, SectionPriority.IDENTITY),
            self._ps("identity_brief", self._section_identity_brief(mem), SectionPriority.IDENTITY),
            *self._render_domain_snapshots(ctx),
            self._ps("archival", self._section_archival(mem, query), SectionPriority.ARCHIVAL),
            self._ps("tools", self._section_tools(), SectionPriority.TOOLS),
            self._ps("output_contract", self._section_output_contract(), SectionPriority.OUTPUT_CONTRACT),
            self._ps("boundaries", self._section_boundaries(mem), SectionPriority.BOUNDARIES),
        ]
        return [s for s in candidates if s is not None]

    def _build_compact(
        self, ctx: TaskContext, query: str, mem: MemoryReader | None
    ) -> list[PromptSection]:
        """compaction 阶段: 指导 LLM 压缩。"""
        text = (
            "You are Pulse's compaction engine.\n"
            "Summarize the following execution trace into a concise task summary.\n"
            "Preserve: task objective, completed steps, pending items, key findings, user corrections.\n"
            "Discard: raw tool observations, intermediate reasoning, redundant context.\n"
            "Output a structured JSON with keys: objective, completed, pending, findings, corrections."
        )
        return [PromptSection(name="compact_intro", text=text, priority=SectionPriority.IDENTITY)]

    def _build_promotion(
        self, ctx: TaskContext, query: str, mem: MemoryReader | None
    ) -> list[PromptSection]:
        """promotion 阶段: 指导 LLM 提取事实。"""
        text = (
            "You are Pulse's fact extraction engine.\n"
            "From the following conversation/summary, extract stable facts as structured triples.\n"
            "Each fact: {subject, predicate, object, confidence, evidence_ref}.\n"
            "Only extract facts with high confidence (>0.7).\n"
            "Flag conflicts with existing facts if any are provided.\n"
            "Output a JSON array of fact objects."
        )
        return [PromptSection(name="promotion_intro", text=text, priority=SectionPriority.IDENTITY)]

    def _build_recovery(
        self, ctx: TaskContext, query: str, mem: MemoryReader | None
    ) -> list[PromptSection]:
        """resumedTask: 从 checkpoint 恢复。"""
        intro = (
            f"You are Pulse resuming a previously interrupted task.\n"
            f"Task ID: {ctx.task_id}\n"
            f"Review the checkpoint below, then continue from where it left off."
        )
        candidates = [
            self._ps("recovery_intro", intro, SectionPriority.IDENTITY),
            self._ps("identity_brief", self._section_identity_brief(mem), SectionPriority.IDENTITY),
            self._ps("tools", self._section_tools(), SectionPriority.TOOLS),
            self._ps("boundaries", self._section_boundaries(mem), SectionPriority.BOUNDARIES),
        ]
        return [s for s in candidates if s is not None]

    # ── Section Helpers ────────────────────────────────────

    def _section_identity(self, mem: MemoryReader | None) -> str:
        if mem is None:
            return _DEFAULT_IDENTITY
        snapshot = mem.read_core_snapshot()
        soul = snapshot.get("soul") if isinstance(snapshot.get("soul"), dict) else {}
        if not soul:
            return _DEFAULT_IDENTITY
        prefix = soul.get("assistant_prefix", "Pulse")
        role = soul.get("role", "")
        tone = soul.get("tone", "")
        principles = soul.get("principles", [])
        style = soul.get("style_rules", [])
        parts = [f"## Identity\nName: {prefix}"]
        if role:
            parts.append(f"Role: {role}")
        if tone:
            parts.append(f"Tone: {tone}")
        if principles:
            parts.append("Principles: " + "; ".join(str(p) for p in principles[:5]))
        if style:
            parts.append("Style: " + "; ".join(str(s) for s in style[:5]))
        return "\n".join(parts)

    def _section_identity_brief(self, mem: MemoryReader | None) -> str:
        if mem is None:
            return ""
        snapshot = mem.read_core_snapshot()
        soul = snapshot.get("soul") if isinstance(snapshot.get("soul"), dict) else {}
        prefix = soul.get("assistant_prefix", "Pulse")
        role = soul.get("role", "")
        return f"Identity: {prefix}" + (f" ({role})" if role else "")

    def _section_user_profile(self, mem: MemoryReader | None) -> str:
        if mem is None:
            return ""
        snapshot = mem.read_core_snapshot()
        user = snapshot.get("user") if isinstance(snapshot.get("user"), dict) else {}
        if not user or not any(v for v in user.values() if v):
            return ""
        return f"## User Profile\n{json.dumps(user, ensure_ascii=False)}"

    def _section_user_prefs(self, mem: MemoryReader | None) -> str:
        if mem is None:
            return ""
        snapshot = mem.read_core_snapshot()
        prefs = snapshot.get("prefs") if isinstance(snapshot.get("prefs"), dict) else {}
        if not prefs:
            return ""
        return f"## User Preferences\n{json.dumps(prefs, ensure_ascii=False)}"

    def _section_recent_recall(self, mem: MemoryReader | None, ctx: TaskContext) -> str:
        if mem is None:
            return ""
        recent = mem.read_recent(ctx.session_id, self._recent_limit)
        if not recent:
            return ""
        lines = ["## Recent Conversation History"]
        for entry in recent[-self._recent_limit:]:
            role = str(entry.get("role") or "")
            text = str(entry.get("text") or "")[:200]
            lines.append(f"- [{role}] {text}")
        return "\n".join(lines)

    def _section_relevant_recall(self, mem: MemoryReader | None, query: str, ctx: TaskContext) -> str:
        if mem is None or not query:
            return ""
        hits = mem.search_recall(query, ctx.session_id, self._recall_top_k)
        if not hits:
            return ""
        floor = self._recall_min_similarity
        filtered: list[tuple[float, str]] = []
        for hit in hits:
            text = str(hit.get("text") or "")[:200]
            if not text:
                continue
            sim = float(hit.get("similarity") or 0)
            if sim < floor:
                # Drop fallback / keyword-only hits (``sim≈0``): they carry no
                # real semantic evidence and polluted prompts in prod audit.
                continue
            filtered.append((sim, text))
        if not filtered:
            return ""
        lines = ["## Relevant Past Conversations"]
        for sim, text in filtered:
            lines.append(f"- (sim={sim:.2f}) {text}")
        return "\n".join(lines)

    def _section_archival(self, mem: MemoryReader | None, query: str) -> str:
        if mem is None or not query:
            return ""
        hits = mem.search_archival(query, self._archival_limit)
        if not hits:
            return ""
        lines = ["## Relevant Long-term Knowledge"]
        for fact in hits:
            s = str(fact.get("subject") or "")
            p = str(fact.get("predicate") or "")
            o = str(fact.get("object") or "")
            lines.append(f"- {s} {p} {o}")
        return "\n".join(lines)

    def _section_tools(self) -> str:
        """ToolUseContract §4.1 契约 A — 三段式工具卡片.

        有 ``tool_specs`` 时渲染每个工具的 ``description`` / ``when_to_use``
        / ``when_not_to_use`` 三段. 没有 ``when_*`` 字段的工具退化为仅 name + description.
        无 specs (仅 names) 时退化为逗号分隔名字列表 (向后兼容).
        """
        if not self._tool_specs and not self._tool_names:
            return ""
        if not self._tool_specs:
            return "## Available Tools\n" + ", ".join(self._tool_names)

        lines: list[str] = ["## Available Tools"]
        for spec in self._tool_specs:
            name = spec.name
            desc = (spec.description or "").strip()
            when = (spec.when_to_use or "").strip()
            avoid = (spec.when_not_to_use or "").strip()
            if not (when or avoid):
                # 未声明 when_* 的工具走兼容渲染, 不加结构噪音.
                lines.append(f"- `{name}` — {desc}" if desc else f"- `{name}`")
                continue
            block = [f"- `{name}`: {desc}" if desc else f"- `{name}`"]
            if when:
                block.append(f"    when_to_use: {when}")
            if avoid:
                block.append(f"    when_not_to_use: {avoid}")
            lines.append("\n".join(block))
        return "\n".join(lines)

    def _section_tool_use_policy(self) -> str:
        """强约束: 用户表达"执行动作"意图时必须调工具, 而不是凭记忆编造结果.

        背景 (2026-04 trace 9cc25f13a792):
          用户: "帮我投递 5 个合适的 JD, 不要重复投递"
          Agent: 1 step 就 completed, used_tools=[], tool_calls=0;
          回复里列了 5 家公司——全是基于 Job Snapshot 的 hard_constraints
          + 上一轮历史记忆**幻想**出来的, 没有真去 scan / trigger.

        根因: Available Tools 只有名字列表, 而 Output Contract 教的是"怎么
        措辞回复", 两者中间缺一条连接规则 —— 什么情况下**必须**走工具.
        加一段 Tool-Use Policy, 明确执行动作/信息检索类意图**禁止**靠记忆
        回答, 必须通过工具闭环.
        """
        return (
            "## Tool-Use Policy (MUST FOLLOW)\n"
            "assistant 每一轮的回复只有两种合法形态:\n"
            "  (a) 本轮**不**涉及真实副作用 / 实时数据 / 持久化写入 → 直接自然语言回复;\n"
            "  (b) 本轮涉及其中任一 → **必须**先下发相应 tool_call, 观察工具结果后再终回复.\n"
            "禁止形态: 回复文本承诺或模拟了一个真实动作, 但本轮 tool_calls 为空 —— "
            "这等于欺骗用户, 因为实际什么都没发生.\n"
            "\n"
            "判断是否需要 tool 的**唯一依据**是任务语义涉及下列哪类副作用范畴, "
            "**不是**用户用词是否命中某个动词清单:\n"
            "  - 外部系统写入 (平台消息 / 订阅 / 预约 / 下单 / 取消 ...)\n"
            "  - 外部系统读取 (实时信息 / 平台数据 / 历史对话检索 ...)\n"
            "  - 本地持久化变更 (偏好 / 画像 / 硬约束 / 长期事实 ...)\n"
            "对应归哪个工具由每个工具自己声明的 `when_to_use / when_not_to_use` 决定, "
            "读完工具卡片再判断, 不要凭关键词直觉触发.\n"
            "\n"
            "Memory / Snapshot 读到的历史事件 (application_event / favor_company 等) 是"
            "**过滤条件 / 上下文**, **不是**当前可交付内容的来源; 任何列表 (岗位 / 联系人 / 消息等) "
            "都必须来自本轮或前序步骤的工具返回值.\n"
            "\n"
            "多步任务允许连跑多 step, 走完 ReAct 再回复; 不要在第一步就宣布「完成」而 tool_calls=[].\n"
            "拿不准是咨询还是动作: 复述你对任务的理解让用户确认, 不伪造看起来像工具结果的文本.\n"
            "\n"
            "### Few-shot: 反例对照 (说明\"文本承诺 vs 真实 tool_call\"的差异)\n"
            "[BAD] assistant: \"好的, 我会筛选并准备投递 5 个合适的 JD. 以下是初步筛选: 1) ...\"\n"
            "      (tool_calls=[])\n"
            "      ↑ 违规: 把 Snapshot 里的历史公司当作\"已投\", 实际什么都没发生.\n"
            "[GOOD] assistant: [tool_call: job.greet.trigger(batch_size=5, match_threshold=60, ...)]\n"
            "       → 观察返回后回复 \"已处理 N 个岗位, M 个打招呼成功\".\n"
            "\n"
            "[BAD] assistant: \"今天暂时没有新的面试回复\"  (tool_calls=[])\n"
            "      ↑ 违规: 未读取平台就下\"无新回复\"断言.\n"
            "[GOOD] assistant: [tool_call: job.chat.pull(unread_only=true)] → 看返回再回复.\n"
            "\n"
            "[OK]  assistant: [tool_call: job.memory.record(type=preference, ...)] "
            "→ \"好, 偏好已更新.\"\n"
            "      ↑ 偏好写入也是副作用, 必须经工具落盘, 不能只口头答应.\n"
        )

    def _section_command_conventions(self) -> str:
        """教 Brain 如何处理用户输入中的 ``/`` 系统命令。

        Pulse 不再用静态 router_rules 做命令路由(见 docs/Pulse-DomainMemory与Tool模式.md §3.4),
        所有 ``/`` 开头的文本都走 Brain, 由 Brain 根据以下约定决策:
          - 识别命令意图 → 调对应 tool 或自然语言回应
          - 未登记的 ``/xxx`` → 礼貌回应"未识别"或尝试自然语言理解
          - 用户**没有用 / 前缀的自然语言**同样有效, 命令约定不是硬规则
        """
        return (
            "## Command Conventions\n"
            "Users may type slash-prefixed shortcuts. Treat them as natural language cues, not strict rules:\n"
            "- `/help`, `帮助`, `help me` — Respond with a short overview of what you can do in the current workspace. "
            "No tool call required unless a dedicated help tool is available.\n"
            "- `/cancel`, `取消`, `停下` — The user wants to abort the current in-flight task. "
            "If a cancellation tool is registered, invoke it; otherwise acknowledge verbally and stop further tool calls.\n"
            "- `/tool <name> <json-args>` — Explicit tool invocation (mostly for testing/debugging). "
            "Execute the named tool with provided args if it exists.\n"
            "- `/prefs`, `偏好`, `我的设置` — Show the user's current preferences by reading workspace memory snapshot. "
            "No mutation.\n"
            "- Anything else — Treat as normal natural language. "
            "Never refuse just because input starts with `/`.\n"
            "Important: user preferences (e.g. block a company, change target city) are dynamic. "
            "Persist them via REAL domain memory tools instead of verbal promises only. "
            "For Job domain, prefer `job.memory.record` (semantic preferences/events) and "
            "`job.hard_constraint.set` (location/salary/role/experience hard filters)."
        )

    def _section_output_contract(self) -> str:
        """硬约束: 给最终用户的回复绝不能暴露内部实现细节.

        背景 (2026-04 trace 5b887b003772):
          LLM 在 ReAct 最后一步直接把工具名/JSON 字段名/``confirm_execute=false``
          这种函数签名吐给用户, 回复读起来像调试日志而不是像人说话. 根因是
          prompt 里**从未告诉 LLM**最终回复是给人类看的.

        这一段按 Anthropic/OpenAI 的 Agent prompt 工程惯例写成硬约束,
        放在 prompt 靠后位置(recency bias — 越靠近用户 query 约束越强).
        """
        return (
            "## Output Contract (MUST FOLLOW for the final user-facing reply)\n"
            "你现在正在通过 IM(微信/飞书等)和真实用户对话. 最终那一条消息是**给人看的**, 不是给调试用的.\n"
            "强制规则:\n"
            "1. **绝对禁止**在回复里出现: 工具名(``job.greet.scan``/``job.chat.pull`` 等)、"
            "函数签名、JSON 字段名(``confirm_execute``/``max_pages`` 等)、参数键值对. "
            "用户不关心你用了什么工具, 用自然语言描述**你做了什么 / 发现了什么 / 建议什么**即可.\n"
            "2. 以第一人称(\"我帮你...\" / \"我刚查了...\")口吻, 不要说\"Agent 决定调用 xxx\"这种第三人称.\n"
            "3. 不要粘贴工具返回的原始 dict/JSON, 请自己总结成中文短句.\n"
            "4. 如果某步执行失败或能力暂时不可用, 坦诚说明(\"这一步暂时做不到, 因为...\"), "
            "不要伪装成功, 也不要泄露内部错误栈.\n"
            "5. 回复控制在 3-8 句, 必要时列点; 禁止废话开场白(\"好的, 根据您的需求...\").\n"
            "6. 如果你已经调完工具、准备给最终答复: 以用户的任务视角汇报结果, "
            "而不是复述 ReAct 过程.\n"
            "7. 涉及\"已投递/已发送 N 个\"这类数量承诺时, 以工具执行报告为准: "
            "只有 succeeded>0 才能说\"已投递\"; 若 succeeded=0 或状态是 "
            "failed/skipped/preview, 必须如实说\"未成功执行\", 并给出具体原因 "
            "(例如\"今日投递配额已满\"、\"仅完成预览\"、\"没有符合条件岗位\").\n"
        )

    def _section_boundaries(self, mem: MemoryReader | None) -> str:
        if mem is None:
            return ""
        snapshot = mem.read_core_snapshot()
        soul = snapshot.get("soul") if isinstance(snapshot.get("soul"), dict) else {}
        boundaries = soul.get("boundaries", [])
        if not boundaries:
            return ""
        return "## Safety Boundaries\n" + "\n".join(f"- {b}" for b in boundaries[:8])

    def _section_workspace(self, mem: MemoryReader | None, ctx: TaskContext) -> str:
        if mem is None:
            return ""
        essentials = mem.read_workspace_essentials(ctx.workspace_id)
        if not essentials:
            return ""
        summary = essentials.get("summary", "")
        facts = essentials.get("facts", [])
        if not summary and not facts:
            return ""
        parts = ["## Workspace Context"]
        if summary:
            parts.append(summary)
        if facts:
            parts.append("Key facts:")
            for f in facts[:10]:
                parts.append(f"- {f.get('key', '')}: {f.get('value', '')}")
        return "\n".join(parts)


_DEFAULT_IDENTITY = (
    "You are Pulse, a personal AI assistant with ReAct reasoning.\n"
    "You have access to tools. Decide what to do step by step:\n"
    "- Call tools when you need information or need to perform actions.\n"
    "- You can chain multiple tool calls across steps.\n"
    "- When you have enough information, respond directly to the user.\n"
    "- Be concise, direct, and helpful."
)

_CONTRACT_METHOD: dict[ContractType, str] = {
    ContractType.system: "_build_system",
    ContractType.heartbeat: "_build_heartbeat",
    ContractType.task: "_build_task",
    ContractType.compact: "_build_compact",
    ContractType.promotion: "_build_promotion",
    ContractType.recovery: "_build_recovery",
}
