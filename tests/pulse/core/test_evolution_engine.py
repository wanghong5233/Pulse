from __future__ import annotations

from pulse.core.learning import DPOCollector, PreferenceExtractor
from pulse.core.memory import ArchivalMemory, CoreMemory
from pulse.core.soul import SoulEvolutionEngine, SoulGovernance
from tests.pulse.support.fakes import FakeArchivalDB, FakeCorrectionsDB


def test_evolution_engine_applies_preference_and_archives_fact(tmp_path) -> None:
    core = CoreMemory(
        storage_path=str(tmp_path / "core_memory.json"),
        soul_config_path=str(tmp_path / "soul.yaml"),
    )
    governance = SoulGovernance(core_memory=core, audit_path=str(tmp_path / "audit.json"))
    archival = ArchivalMemory(db_engine=FakeArchivalDB())
    dpo_collector = DPOCollector(db_engine=FakeCorrectionsDB())
    engine = SoulEvolutionEngine(
        governance=governance,
        archival_memory=archival,
        preference_extractor=PreferenceExtractor(),
        dpo_collector=dpo_collector,
    )

    result = engine.reflect_interaction(
        user_text="以后默认用上海，我不喜欢外包岗位",
        assistant_text="收到。",
        metadata={"session_id": "u1"},
    )
    payload = result.to_dict()
    assert payload["classification"] == "correction"
    assert len(payload["preference_applied"]) >= 1
    assert core.preference("default_location") == "上海"
    assert archival.count() >= 1
    assert payload["dpo_collected"] is not None
    assert dpo_collector.count() == 1

    context = core.read_block("context")
    assert "beliefs" in context
    assert len(context["beliefs"]["mutable"]) >= 1
