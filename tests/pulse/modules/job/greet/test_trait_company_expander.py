from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from pulse.core.memory.workspace_memory import WorkspaceMemory
from pulse.modules.job.greet.trait_expander import TraitCompanyExpander
from pulse.modules.job.memory import (
    HardConstraints,
    JobMemory,
    JobMemorySnapshot,
    MemoryItem,
)
from tests.pulse.modules.job.test_scan_multi_city import _FakeWorkspaceDB


class _FakeLLM:
    def __init__(self, response: dict[str, Any] | None) -> None:
        self.response = response
        self.calls = 0

    def invoke_json(
        self,
        prompt_value: Any,
        *,
        route: str = "default",
        default: Any = None,
    ) -> Any:
        _ = prompt_value, route, default
        self.calls += 1
        return self.response

    def primary_model(self, route: str = "default") -> str:
        _ = route
        return "gpt-4.1"


def _memory() -> JobMemory:
    return JobMemory(
        workspace_memory=WorkspaceMemory(db_engine=_FakeWorkspaceDB()),
        workspace_id="job.test.expander",
    )


def _snapshot(*traits: str) -> JobMemorySnapshot:
    items: list[MemoryItem] = []
    for trait in traits:
        items.append(
            MemoryItem(
                id=f"it-{trait}",
                type="avoid_trait",
                target=trait,
                content=f"avoid {trait}",
                raw_text=f"avoid {trait}",
                valid_from="2026-04-28T00:00:00+00:00",
                valid_until=None,
                superseded_by=None,
                created_at="2026-04-28T00:00:00+00:00",
            )
        )
    return JobMemorySnapshot(
        workspace_id="job.test.expander",
        hard_constraints=HardConstraints(),
        memory_items=items,
    )


def test_expander_writes_and_reuses_cache() -> None:
    memory = _memory()
    llm = _FakeLLM(
        response={
            "mode": "company_set",
            "trait": "大厂",
            "companies": ["字节跳动", "阿里巴巴", "腾讯"],
            "reason": "high confidence set",
        }
    )
    expander = TraitCompanyExpander(llm, preferences=memory, ttl_hours=24)

    first = expander.resolve_avoid_trait_companies(snapshot=_snapshot("大厂"))
    second = expander.resolve_avoid_trait_companies(snapshot=_snapshot("大厂"))

    assert llm.calls == 1, "second resolve must hit cache, not call LLM again"
    assert first["大厂"] == {"字节跳动", "阿里巴巴", "腾讯"}
    assert second["大厂"] == {"字节跳动", "阿里巴巴", "腾讯"}

    stored = memory.get_trait_company_set(trait_type="avoid_trait", trait="大厂")
    assert stored is not None
    assert stored.model == "gpt-4.1"
    assert stored.is_expired is False


def test_expander_uses_stale_cache_when_refresh_fails() -> None:
    memory = _memory()
    now = datetime.now(timezone.utc)
    memory.set_trait_company_set(
        trait_type="avoid_trait",
        trait="大厂",
        companies=["字节跳动"],
        model="gpt-4.1",
        updated_at=(now - timedelta(days=10)).isoformat(),
        expires_at=(now - timedelta(days=1)).isoformat(),
    )
    llm = _FakeLLM(response=None)
    expander = TraitCompanyExpander(llm, preferences=memory, ttl_hours=24)

    resolved = expander.resolve_avoid_trait_companies(snapshot=_snapshot("大厂"))

    assert resolved["大厂"] == {"字节跳动"}
    assert llm.calls == 1


def test_expander_fail_loud_when_no_cache_and_llm_unavailable() -> None:
    memory = _memory()
    llm = _FakeLLM(response=None)
    expander = TraitCompanyExpander(llm, preferences=memory, ttl_hours=24)

    with pytest.raises(RuntimeError, match="trait_expander unavailable"):
        expander.resolve_avoid_trait_companies(snapshot=_snapshot("大厂"))

