"""ToolUseContract 契约 A 单测 (ADR-001).

覆盖:
  1. ``_section_tools`` 三段式渲染 (有 when_* 时)
  2. 单个 tool 未声明 when_* 时退化为 name+description 的兼容模式
  3. 完全没有 tool_specs 仅有 tool_names 时退化为逗号名单
  4. ``_section_tool_use_policy`` 保留行为原则 + 反例 few-shot, 不含关键词字典
  5. interactive 模式 build() 产物包含 Available Tools 三段式卡片
"""

from __future__ import annotations

from typing import Any

from pulse.core.prompt_contract import PromptContractBuilder
from pulse.core.task_context import create_interactive_context
from pulse.core.tool import ToolSpec


class _StubMemoryReader:
    """Minimal ``MemoryReader`` stub for prompt-builder unit tests.

    Only ``search_recall`` is exercised here. Everything else returns empty
    so downstream sections render as no-ops and don't pollute assertions.
    """

    def __init__(self, recall_hits: list[dict[str, Any]] | None = None) -> None:
        self._recall_hits = list(recall_hits or [])

    def read_core_snapshot(self) -> dict[str, Any]:
        return {}

    def read_recent(self, session_id: str | None, limit: int) -> list[dict[str, Any]]:
        return []

    def search_recall(
        self, query: str, session_id: str | None, top_k: int
    ) -> list[dict[str, Any]]:
        return list(self._recall_hits)

    def search_archival(self, query: str, limit: int) -> list[dict[str, Any]]:
        return []


def _fixed_specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="demo.act",
            description="Perform a write action with side effects.",
            when_to_use="写入外部系统 (持久化副作用); 参数 target 必填.",
            when_not_to_use="只读预览归 demo.scan; target 缺失时请求方先澄清.",
        ),
        ToolSpec(
            name="demo.scan",
            description="Read-only preview.",
            when_to_use="只读预览候选对象, 不触发写入.",
            when_not_to_use="需要真实副作用时走 demo.act.",
        ),
    ]


def _builder_with_specs(specs: list[ToolSpec]) -> PromptContractBuilder:
    return PromptContractBuilder(tool_specs=specs)


def test_section_tools_renders_three_part_card_for_specs_with_when_fields() -> None:
    text = _builder_with_specs(_fixed_specs())._section_tools()
    assert text.startswith("## Available Tools")
    assert "`demo.act`" in text
    assert "when_to_use: 写入外部系统" in text
    assert "when_not_to_use: 只读预览归 demo.scan" in text
    assert "`demo.scan`" in text


def test_section_tools_degrades_per_tool_when_when_fields_missing() -> None:
    specs = [
        ToolSpec(name="demo.bare", description="No when fields declared."),
        _fixed_specs()[0],
    ]
    text = _builder_with_specs(specs)._section_tools()
    assert "- `demo.bare` — No when fields declared." in text
    assert "when_to_use" not in text.split("demo.bare")[0]
    assert "when_to_use: 写入外部系统" in text


def test_section_tools_falls_back_to_name_list_without_specs() -> None:
    builder = PromptContractBuilder(tool_names=["a", "b"])
    text = builder._section_tools()
    assert text == "## Available Tools\na, b"


def test_section_tools_returns_empty_when_nothing_registered() -> None:
    assert PromptContractBuilder()._section_tools() == ""


def test_tool_use_policy_keeps_principles_and_drops_keyword_dictionary() -> None:
    policy = PromptContractBuilder()._section_tool_use_policy()
    assert "Tool-Use Policy" in policy
    assert "(a) 本轮**不**涉及真实副作用" in policy
    assert "(b) 本轮涉及其中任一" in policy
    assert "Few-shot: 反例对照" in policy
    assert "[BAD]" in policy and "[GOOD]" in policy
    for forbidden in ("触发关键词", "帮我投递 / 发送 / 打招呼 / 回复 TA", "帮我找"):
        assert forbidden not in policy, (
            f"policy 里不应出现口语动词清单 {forbidden!r}"
        )


def test_interactive_build_includes_three_part_tool_card() -> None:
    builder = _builder_with_specs(_fixed_specs())
    ctx = create_interactive_context(session_id="s-t")
    contract = builder.build(ctx, "hello")
    text = contract.text
    assert "## Available Tools" in text
    assert "when_to_use: 写入外部系统" in text
    assert "Tool-Use Policy" in text


# ---------------------------------------------------------------------------
# P1-B regression guard (see audit trace_f3bda835ed94):
# ``Relevant Past Conversations`` used to include hits whose similarity was
# ``0.00`` — i.e. pure fallback matches with no real semantic evidence —
# which both bloated the prompt and misled the LLM into thinking it had
# "already answered" the same question multiple times.
# The builder must filter by a similarity floor before rendering the section.
# ---------------------------------------------------------------------------


def test_relevant_recall_drops_sim_zero_hits_by_default() -> None:
    memory = _StubMemoryReader(
        recall_hits=[
            {"text": "我正在找大模型应用开发 agent 实习", "similarity": 0.0},
            {"text": "帮我投递 5 个合适的 JD", "similarity": 0.0},
        ]
    )
    builder = PromptContractBuilder(memory=memory)
    text = builder._section_relevant_recall(memory, "大模型 agent 实习", create_interactive_context(session_id="s"))
    assert text == "", (
        "sim=0.00 hits must not appear in `## Relevant Past Conversations` "
        "(they pollute the prompt and mislead the model)"
    )


def test_relevant_recall_keeps_hits_above_default_floor() -> None:
    memory = _StubMemoryReader(
        recall_hits=[
            {"text": "low-sim noise", "similarity": 0.05},
            {"text": "genuinely related chat", "similarity": 0.32},
        ]
    )
    builder = PromptContractBuilder(memory=memory)
    text = builder._section_relevant_recall(
        memory, "related query", create_interactive_context(session_id="s")
    )
    assert "## Relevant Past Conversations" in text
    assert "genuinely related chat" in text
    assert "low-sim noise" not in text, "hits below the floor must be filtered"


def test_relevant_recall_floor_is_configurable() -> None:
    memory = _StubMemoryReader(
        recall_hits=[
            {"text": "medium hit", "similarity": 0.25},
        ]
    )
    builder_strict = PromptContractBuilder(memory=memory, recall_min_similarity=0.5)
    strict_text = builder_strict._section_relevant_recall(
        memory, "q", create_interactive_context(session_id="s")
    )
    assert strict_text == "", "strict floor (0.5) must drop the 0.25 hit"

    builder_loose = PromptContractBuilder(memory=memory, recall_min_similarity=0.1)
    loose_text = builder_loose._section_relevant_recall(
        memory, "q", create_interactive_context(session_id="s")
    )
    assert "medium hit" in loose_text
