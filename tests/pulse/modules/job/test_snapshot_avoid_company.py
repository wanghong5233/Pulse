"""Pin the contract that ``JobMemorySnapshot.is_company_avoided`` only does
**literal company-name** matching against ``avoid_company`` items.

Why this guard exists (post-mortem 2026-04-28 trace_753fecf70cc5):
A previous iteration tried to fix "user said 不要大厂 but bot kept greeting
字节/阿里" by adding a hardcoded BIG_TECH_COMPANIES list and expanding
``avoid_trait="大厂"`` against it inside the snapshot. That violated three
project axioms at once:

* §1.3 (YAGNI / no exhaustive enumeration) — 大厂 list is unbounded and
  evolves over time; today's startup is tomorrow's 大厂.
* §1.2 (源头修, no downstream patches) — the right place for "is this
  company a 大厂?" is the LLM matcher with world knowledge, not a static
  table buried in the data layer.
* Agent-system core principle — semantic mass-noun preferences are exactly
  what an LLM is good at; reducing them to a frozen alias table makes the
  system brittle and reactive.

These tests therefore assert the **negative** contract: the snapshot layer
must NOT silently treat ``avoid_trait="大厂"`` as a company filter. That
responsibility belongs to ``JobSnapshotMatcher`` LLM stage.
"""
from __future__ import annotations

from pulse.modules.job.memory import (
    HardConstraints,
    JobMemorySnapshot,
    MemoryItem,
)


def _avoid_company(target: str, *, content: str = "") -> MemoryItem:
    return MemoryItem(
        id=f"item-avoid-company-{target}",
        type="avoid_company",
        target=target,
        content=content or f"avoid {target}",
        raw_text=content or f"avoid {target}",
        valid_from="2026-04-28T00:00:00+00:00",
        valid_until=None,
        superseded_by=None,
        created_at="2026-04-28T00:00:00+00:00",
    )


def _avoid_trait(target: str, *, content: str = "") -> MemoryItem:
    return MemoryItem(
        id=f"item-avoid-trait-{target}",
        type="avoid_trait",
        target=target,
        content=content or f"avoid trait {target}",
        raw_text=content or f"avoid trait {target}",
        valid_from="2026-04-28T00:00:00+00:00",
        valid_until=None,
        superseded_by=None,
        created_at="2026-04-28T00:00:00+00:00",
    )


def _snapshot(items: list[MemoryItem]) -> JobMemorySnapshot:
    return JobMemorySnapshot(
        workspace_id="job.test",
        hard_constraints=HardConstraints(),
        memory_items=items,
    )


def test_avoid_company_literal_match_drops_exact_company() -> None:
    snap = _snapshot([_avoid_company("拼多多", content="笔试挂了")])
    avoided, reason = snap.is_company_avoided("拼多多")
    assert avoided is True
    assert reason == "笔试挂了"


def test_avoid_company_match_is_case_insensitive() -> None:
    snap = _snapshot([_avoid_company("ByteDance")])
    avoided, _ = snap.is_company_avoided("bytedance")
    assert avoided is True


def test_avoid_company_does_not_drop_unrelated_company() -> None:
    snap = _snapshot([_avoid_company("拼多多")])
    avoided, _ = snap.is_company_avoided("某创业公司")
    assert avoided is False


def test_avoid_trait_does_not_drop_companies_at_snapshot_layer() -> None:
    """Negative contract — see module docstring for rationale.

    User says ``avoid_trait="大厂"``. The snapshot layer must NOT magically
    decide that 字节跳动 / 阿里巴巴 are 大厂 and drop them. That decision
    belongs to the LLM matcher (matcher.py Verdict policy §(b)). If this
    test starts failing because someone reintroduced an alias table here,
    delete the alias table — do not loosen the test.
    """
    snap = _snapshot([_avoid_trait("大厂", content="暂时战略性放弃大厂暑期实习")])

    for big_tech_candidate in ("字节跳动", "阿里巴巴", "阿里云", "腾讯", "美团"):
        avoided, reason = snap.is_company_avoided(big_tech_candidate)
        assert avoided is False, (
            f"snapshot layer must not classify {big_tech_candidate} as 大厂; "
            f"trait-to-company expansion is the matcher LLM's job. "
            f"got reason={reason!r}"
        )


def test_avoid_trait_in_snapshot_still_renders_into_prompt_section() -> None:
    """Even though is_company_avoided ignores avoid_trait, the trait MUST be
    visible in the markdown prompt section so the matcher LLM can read and
    reason about it. Otherwise the world-knowledge path has no input."""
    snap = _snapshot([_avoid_trait("大厂", content="暂时战略性放弃大厂暑期实习")])
    md = snap.to_prompt_section()
    assert "大厂" in md
    assert "avoid" in md.lower() or "回避" in md or "不投" in md or "战略性放弃" in md
