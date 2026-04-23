"""P1-C regression guard: JobMemory.record_item dedup.

Audit of trace_f3bda835ed94 revealed the same ``constraint_note`` content
(``"已经联系过的不要重复投递"``) being written 4 times within one user turn
because ``record_item`` only deduplicated by ``id`` (which is auto-generated
per call) — never by semantic ``(type, target, content)`` tuple.

Contract we enforce here:
  * Two calls with identical ``(type, target, content)`` tuple while the
    first item is still **active** MUST be idempotent: the second call
    returns the existing item and does **not** create a second fact row.
  * Dedup must key on *normalized* fields (content.strip() / target or "").
  * If the prior item has been retired or superseded, a fresh write is
    allowed (so users can re-raise a preference after explicitly removing
    it).
  * Dedup must be scoped by ``type`` — same content under different types
    is considered distinct (e.g. "Meta" as ``avoid_company`` vs
    ``favor_company``).
"""

from __future__ import annotations

from typing import Any

import pytest

from pulse.core.memory.workspace_memory import WorkspaceMemory
from pulse.modules.job.memory import JobMemory


class _FakeWorkspaceDB:
    """Minimal subset of WorkspaceMemory's SQL contract."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def execute(self, sql, params=None, *, fetch="none", commit=True):  # noqa: ANN001
        _ = commit, fetch
        norm = " ".join(str(sql).lower().split())
        p = tuple(params or ())

        if norm.startswith("create table") or norm.startswith("create index"):
            return None

        if norm.startswith("select value from workspace_facts where workspace_id"):
            ws, key = p
            for row in self.rows:
                if row["workspace_id"] == ws and row["key"] == key:
                    return (row["value"],)
            return None

        if norm.startswith("select id from workspace_facts where workspace_id"):
            ws, key = p
            for row in self.rows:
                if row["workspace_id"] == ws and row["key"] == key:
                    return (id(row),)
            return None

        if norm.startswith("update workspace_facts set value"):
            value, source, updated_at, ws, key = p
            for row in self.rows:
                if row["workspace_id"] == ws and row["key"] == key:
                    row["value"] = value
                    row["source"] = source
                    row["updated_at"] = updated_at
                    return None
            return None

        if norm.startswith("insert into workspace_facts"):
            workspace_id, key, value, source, created_at, updated_at = p
            self.rows.append(
                {
                    "workspace_id": workspace_id,
                    "key": key,
                    "value": value,
                    "source": source,
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
            )
            return None

        if norm.startswith("delete from workspace_facts where workspace_id = %s and key = %s"):
            ws, key = p
            self.rows = [r for r in self.rows if not (r["workspace_id"] == ws and r["key"] == key)]
            return None

        if norm.startswith("select key, value, source, updated_at from workspace_facts where workspace_id"):
            ws, prefix_pattern = p
            prefix = prefix_pattern.rstrip("%")
            out = []
            for row in sorted(self.rows, key=lambda r: r["key"]):
                if row["workspace_id"] == ws and (not prefix or row["key"].startswith(prefix)):
                    out.append((row["key"], row["value"], row["source"], row["updated_at"]))
            return out

        if norm.startswith("select count(*) from workspace_facts where workspace_id = %s and key like"):
            ws, pat = p
            prefix = pat.rstrip("%")
            return (sum(1 for r in self.rows if r["workspace_id"] == ws and r["key"].startswith(prefix)),)

        if norm.startswith("delete from workspace_facts where workspace_id = %s and key like"):
            ws, pat = p
            prefix = pat.rstrip("%")
            self.rows = [
                r for r in self.rows
                if not (r["workspace_id"] == ws and r["key"].startswith(prefix))
            ]
            return None

        raise AssertionError(f"unexpected SQL in fake DB: {sql}")


@pytest.fixture()
def memory() -> JobMemory:
    return JobMemory(
        workspace_memory=WorkspaceMemory(db_engine=_FakeWorkspaceDB()),
        workspace_id="job.test",
    )


def _active_items_of_type(mem: JobMemory, type_: str) -> list[dict[str, Any]]:
    return [it.to_dict() for it in mem.list_items(type=type_)]


def test_record_item_dedups_active_same_type_and_content(memory: JobMemory) -> None:
    first = memory.record_item(
        {
            "type": "constraint_note",
            "content": "已经联系过的不要重复投递",
        }
    )
    second = memory.record_item(
        {
            "type": "constraint_note",
            "content": "已经联系过的不要重复投递",
        }
    )

    assert second.id == first.id, "duplicate record must return the existing id"
    active = _active_items_of_type(memory, "constraint_note")
    assert len(active) == 1, (
        "duplicate (type, content) must not add a second fact row; "
        f"got {len(active)} active constraint_note items"
    )


def test_record_item_dedup_is_whitespace_tolerant(memory: JobMemory) -> None:
    first = memory.record_item(
        {"type": "constraint_note", "content": "已投的不重投"}
    )
    second = memory.record_item(
        {"type": "constraint_note", "content": "  已投的不重投  "}
    )
    assert second.id == first.id
    assert len(_active_items_of_type(memory, "constraint_note")) == 1


def test_record_item_dedup_is_scoped_by_type(memory: JobMemory) -> None:
    avoid = memory.record_item(
        {"type": "avoid_company", "target": "Meta", "content": "不考虑 Meta"}
    )
    favor = memory.record_item(
        {"type": "favor_company", "target": "Meta", "content": "不考虑 Meta"}
    )
    assert avoid.id != favor.id, "same content under different type must be distinct"


def test_record_item_dedup_is_scoped_by_target(memory: JobMemory) -> None:
    a = memory.record_item(
        {"type": "avoid_company", "target": "字节", "content": "加班太多"}
    )
    b = memory.record_item(
        {"type": "avoid_company", "target": "拼多多", "content": "加班太多"}
    )
    assert a.id != b.id, "same (type, content) with different target must be distinct"


def test_record_item_allows_rewrite_after_retire(memory: JobMemory) -> None:
    first = memory.record_item(
        {"type": "constraint_note", "content": "只投远程"}
    )
    assert memory.retire_item(first.id) is True
    second = memory.record_item(
        {"type": "constraint_note", "content": "只投远程"}
    )
    assert second.id != first.id, (
        "after the prior item is retired, a fresh record must create a new id"
    )


def test_record_item_preserves_inactive_duplicates(memory: JobMemory) -> None:
    """Retired items with the same (type,content) must not block a new write,
    but must also remain queryable with ``include_expired=True``."""
    first = memory.record_item(
        {"type": "avoid_trait", "content": "周末加班"}
    )
    memory.retire_item(first.id)
    second = memory.record_item(
        {"type": "avoid_trait", "content": "周末加班"}
    )
    all_including_expired = memory.list_items(type="avoid_trait", include_expired=True)
    ids = {it.id for it in all_including_expired}
    assert {first.id, second.id}.issubset(ids)
