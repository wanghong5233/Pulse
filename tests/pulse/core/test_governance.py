from __future__ import annotations

from pulse.core.memory import CoreMemory
from pulse.core.soul import SoulGovernance


def test_governance_rejects_core_soul_and_allows_mutable_soul(tmp_path) -> None:
    core = CoreMemory(
        storage_path=str(tmp_path / "core_memory.json"),
        soul_config_path=str(tmp_path / "soul.yaml"),
    )
    governance = SoulGovernance(
        core_memory=core,
        audit_path=str(tmp_path / "audit.json"),
        change_modes={"soul_update": "autonomous"},
    )

    rejected = governance.apply_soul_update(
        updates={"assistant_prefix": "NewPulse"},
        source="unit-test",
    )
    assert rejected["ok"] is False
    assert rejected["status"] == "rejected"

    applied = governance.apply_soul_update(
        updates={"tone": "concise"},
        source="unit-test",
        risk_level="medium",
    )
    assert applied["ok"] is True
    assert core.read_block("soul")["tone"] == "concise"


def test_governance_audit_and_rollback(tmp_path) -> None:
    core = CoreMemory(
        storage_path=str(tmp_path / "core_memory.json"),
        soul_config_path=str(tmp_path / "soul.yaml"),
    )
    governance = SoulGovernance(core_memory=core, audit_path=str(tmp_path / "audit.json"))

    result = governance.apply_preference_updates(
        updates={"default_location": "hangzhou"},
        source="unit-test",
    )
    assert result["ok"] is True
    change_id = result["change_id"]
    assert core.preference("default_location") == "hangzhou"

    rollback = governance.rollback(change_id=change_id, actor="tester")
    assert rollback["ok"] is True
    # CoreMemory.prefs 无硬编码业务默认值 (见 _build_default_data 注释),
    # rollback 到未设置状态 → preference(...) 返回 None。
    assert core.preference("default_location") is None

    audits = governance.list_audits(limit=10)
    assert len(audits) >= 1


def test_governance_supervised_requires_approval(tmp_path) -> None:
    core = CoreMemory(
        storage_path=str(tmp_path / "core_memory.json"),
        soul_config_path=str(tmp_path / "soul.yaml"),
    )
    governance = SoulGovernance(
        core_memory=core,
        audit_path=str(tmp_path / "audit.json"),
        change_modes={"prefs_update": "supervised"},
    )

    pending = governance.apply_preference_updates(
        updates={"default_location": "shanghai"},
        source="unit-test",
    )
    assert pending["ok"] is False
    assert pending["status"] == "pending_approval"
    # Pending 期间尚未写入 CoreMemory, 又无默认值 → 返回 None。
    assert core.preference("default_location") is None

    approved = governance.approve_change(change_id=pending["change_id"], actor="reviewer")
    assert approved["ok"] is True
    assert core.preference("default_location") == "shanghai"


def test_governance_gated_blocks_change(tmp_path) -> None:
    core = CoreMemory(
        storage_path=str(tmp_path / "core_memory.json"),
        soul_config_path=str(tmp_path / "soul.yaml"),
    )
    governance = SoulGovernance(
        core_memory=core,
        audit_path=str(tmp_path / "audit.json"),
        change_modes={"belief_mutation": "gated"},
    )
    blocked = governance.add_mutable_belief(
        belief="User prefers concise answer",
        source="unit-test",
    )
    assert blocked["ok"] is False
    assert blocked["status"] == "blocked_by_gate"


def test_governance_risk_override_for_preferences(tmp_path) -> None:
    core = CoreMemory(
        storage_path=str(tmp_path / "core_memory.json"),
        soul_config_path=str(tmp_path / "soul.yaml"),
    )
    governance = SoulGovernance(
        core_memory=core,
        audit_path=str(tmp_path / "audit.json"),
        change_modes={"prefs_update": "autonomous"},
        risk_mode_overrides={"high": "supervised"},
    )
    pending = governance.apply_preference_updates(
        updates={"default_location": "chengdu", "dislike": "outsourcing"},
        source="unit-test",
        risk_level="high",
    )
    assert pending["ok"] is False
    assert pending["status"] == "pending_approval"
