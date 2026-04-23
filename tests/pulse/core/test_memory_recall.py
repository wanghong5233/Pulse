from __future__ import annotations

from pulse.core.memory.recall_memory import RecallMemory
from tests.pulse.support.fakes import FakeRecallDB


def test_recall_memory_add_recent_and_keyword_search() -> None:
    db = FakeRecallDB()
    memory = RecallMemory(db_engine=db)
    memory.add_interaction(
        user_text="我想看看杭州天气",
        assistant_text="好的，我来查杭州天气。",
        metadata={"channel": "cli"},
        session_id="s1",
    )
    memory.add_interaction(
        user_text="再查一下上海航班",
        assistant_text="好的，我来查上海航班。",
        metadata={"channel": "cli"},
        session_id="s2",
    )

    recent_s1 = memory.recent(limit=10, session_id="s1")
    assert len(recent_s1) == 2
    assert all(item["metadata"]["session_id"] == "s1" for item in recent_s1)

    hits = memory.search_keyword(keywords=["杭州"], top_k=3, session_id="s1")
    assert len(hits) >= 1
    assert all("杭州" in item["text"] for item in hits)
    assert memory.count() == 4


def test_recall_memory_survives_reinstantiation() -> None:
    db = FakeRecallDB()
    memory = RecallMemory(db_engine=db)
    memory.add_entry(role="user", text="记录偏好：杭州", metadata={"session_id": "s3"})
    memory.add_entry(role="assistant", text="已记录杭州偏好", metadata={"session_id": "s3"})

    restored = RecallMemory(db_engine=db)
    hits = restored.search_keyword(keywords=["杭州偏好"], top_k=3, session_id="s3")
    assert len(hits) >= 1
    assert restored.count() == 2


def test_recall_memory_keyword_any_vs_all() -> None:
    db = FakeRecallDB()
    memory = RecallMemory(db_engine=db)
    memory.add_entry(role="user", text="我不想投拼多多", metadata={"session_id": "s"})
    memory.add_entry(role="user", text="字节笔试挂了", metadata={"session_id": "s"})

    any_hits = memory.search_keyword(keywords=["拼多多", "字节"], match="any", top_k=10)
    assert len(any_hits) == 2

    all_hits = memory.search_keyword(keywords=["拼多多", "字节"], match="all", top_k=10)
    assert all_hits == []
