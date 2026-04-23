from __future__ import annotations

from pulse.core.memory import ArchivalMemory
from tests.pulse.support.fakes import FakeArchivalDB


def test_archival_memory_add_recent_and_query() -> None:
    memory = ArchivalMemory(db_engine=FakeArchivalDB())
    memory.add_fact(
        subject="user",
        predicate="preference.default_location",
        object_value="hangzhou",
        source="unit-test",
        metadata={"session_id": "u1"},
    )
    memory.add_fact(
        subject="user",
        predicate="preference.dislike",
        object_value="outsourcing",
        source="unit-test",
        metadata={"session_id": "u1"},
    )

    recent = memory.recent(limit=10)
    assert len(recent) == 2

    rows = memory.query(subject="user", predicate="preference.default_location", limit=5)
    assert len(rows) == 1
    assert rows[0]["object"] == "hangzhou"


def test_archival_memory_keyword_query_uses_sql_ilike() -> None:
    memory = ArchivalMemory(db_engine=FakeArchivalDB())
    memory.add_fact(
        subject="user",
        predicate="preference.default_location",
        object_value="hangzhou",
        source="unit-test",
    )
    memory.add_fact(
        subject="user",
        predicate="preference.focus",
        object_value="agent engineering",
        source="unit-test",
    )

    rows = memory.query(keyword="agent", limit=5)
    assert len(rows) >= 1
    assert any("agent engineering" in str(item["object"]) for item in rows)

    hits = memory.search_keyword(keywords=["agent", "engineering"], match="all", top_k=5)
    assert len(hits) == 1
    assert hits[0]["object"] == "agent engineering"

    assert memory.count() == 2
