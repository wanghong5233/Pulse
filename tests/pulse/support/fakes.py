from __future__ import annotations

from datetime import datetime
from typing import Any


def _ilike(text: str, pattern: str) -> bool:
    """Minimal ILIKE: case-insensitive substring (handles `%kw%` pattern)."""
    core = str(pattern or "").strip("%")
    return core.lower() in str(text or "").lower()


class FakeRecallDB:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []

    def execute(self, sql, params=None, *, fetch="none", commit=True):  # noqa: ANN001
        _ = commit, fetch
        normalized = " ".join(str(sql).lower().split())
        if (
            normalized.startswith("create table")
            or normalized.startswith("create index")
            or normalized.startswith("alter table")
        ):
            return None
        if normalized.startswith("insert into conversations"):
            (
                row_id,
                role,
                text,
                metadata_json,
                session_id,
                task_id,
                run_id,
                workspace_id,
                created_at,
            ) = params
            self.rows.append(
                {
                    "id": row_id,
                    "role": role,
                    "text": text,
                    "metadata_json": metadata_json,
                    "session_id": session_id,
                    "task_id": task_id,
                    "run_id": run_id,
                    "workspace_id": workspace_id,
                    "created_at": datetime.fromisoformat(str(created_at).replace("z", "+00:00")),
                }
            )
            return None
        if normalized.startswith("insert into tool_calls"):
            (
                row_id,
                conversation_id,
                session_id,
                task_id,
                run_id,
                workspace_id,
                tool_name,
                tool_args,
                tool_result,
                status,
                latency_ms,
            ) = params
            self.tool_calls.append(
                {
                    "id": row_id,
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "task_id": task_id,
                    "run_id": run_id,
                    "workspace_id": workspace_id,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "tool_result": tool_result,
                    "status": status,
                    "latency_ms": latency_ms,
                }
            )
            return None

        if normalized.startswith("select count(1) from conversations"):
            return (len(self.rows),)

        if "from conversations" in normalized and "select" in normalized:
            remaining = list(params or [])
            data = list(self.rows)

            # Keyword ILIKE against text — consume matching patterns.
            if "text ilike" in normalized:
                kw_count = normalized.count("text ilike")
                kw_patterns = [str(remaining.pop(0)) for _ in range(kw_count)]
                # Joiner for keyword group: OR if " or text ilike" appears, else AND.
                joiner_or = " or text ilike" in normalized
                if joiner_or:
                    data = [item for item in data if any(_ilike(item["text"], p) for p in kw_patterns)]
                else:
                    data = [item for item in data if all(_ilike(item["text"], p) for p in kw_patterns)]

            if "session_id = %s" in normalized:
                session_id = remaining.pop(0)
                data = [item for item in data if item["session_id"] == session_id]
            if "task_id = %s" in normalized:
                task_id = remaining.pop(0)
                data = [item for item in data if item.get("task_id") == task_id]
            if "workspace_id = %s" in normalized:
                workspace_id = remaining.pop(0)
                data = [item for item in data if item.get("workspace_id") == workspace_id]
            if "role = %s" in normalized:
                role = remaining.pop(0)
                data = [item for item in data if str(item["role"]).lower() == str(role).lower()]

            reverse = "order by created_at desc" in normalized
            data = sorted(data, key=lambda it: it["created_at"], reverse=reverse)

            if "limit" in normalized and remaining:
                limit = int(remaining.pop(0))
                data = data[:limit]

            return [
                (
                    item["id"],
                    item["role"],
                    item["text"],
                    item["metadata_json"],
                    item.get("session_id"),
                    item.get("task_id"),
                    item.get("run_id"),
                    item.get("workspace_id"),
                    item["created_at"],
                )
                for item in data
            ]

        raise AssertionError(f"unexpected SQL: {sql}")


class FakeArchivalDB:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._next_id = 1

    def execute(self, sql, params=None, *, fetch="none", commit=True):  # noqa: ANN001
        _ = fetch, commit
        normalized = " ".join(str(sql).lower().split())
        if (
            normalized.startswith("create table")
            or normalized.startswith("create index")
            or normalized.startswith("alter table")
        ):
            return None
        if normalized.startswith("insert into facts"):
            (
                subject,
                predicate,
                object_text,
                object_json,
                metadata_json,
                valid_from,
                confidence,
                source,
                evidence_refs,
                promoted_from,
                promotion_reason,
                task_id,
                run_id,
                workspace_id,
            ) = params
            row = {
                "id": self._next_id,
                "subject": subject,
                "predicate": predicate,
                "object": object_text,
                "object_json": object_json,
                "metadata_json": metadata_json,
                "valid_from": datetime.fromisoformat(str(valid_from).replace("z", "+00:00")),
                "confidence": float(confidence),
                "source": source,
                "evidence_refs": evidence_refs,
                "promoted_from": promoted_from,
                "promotion_reason": promotion_reason,
                "task_id": task_id,
                "run_id": run_id,
                "workspace_id": workspace_id,
                "created_at": datetime.fromisoformat(str(valid_from).replace("z", "+00:00")),
            }
            self._next_id += 1
            self.rows.append(row)
            return (row["id"], row["valid_from"])

        if normalized.startswith("select count(1) from facts"):
            return (len(self.rows),)

        if "from facts where" in normalized and "select" in normalized:
            remaining = list(params or [])
            data = list(self.rows)

            # keyword ILIKE clauses — each keyword contributes 3 %s (subject/predicate/object).
            if "subject ilike" in normalized:
                ilike_groups = normalized.count("subject ilike")
                for _ in range(ilike_groups):
                    p1 = str(remaining.pop(0))
                    _ = remaining.pop(0)  # predicate pattern (same value)
                    _ = remaining.pop(0)  # object pattern (same value)
                    data = [
                        item
                        for item in data
                        if _ilike(item["subject"], p1)
                        or _ilike(item["predicate"], p1)
                        or _ilike(item["object"], p1)
                    ]

            if "subject = %s" in normalized:
                subject = remaining.pop(0)
                data = [item for item in data if item["subject"] == subject]
            if "predicate = %s" in normalized:
                predicate = remaining.pop(0)
                data = [item for item in data if item["predicate"] == predicate]

            reverse = "order by created_at desc" in normalized
            data = sorted(data, key=lambda it: it["created_at"], reverse=reverse)

            if "limit" in normalized and remaining:
                limit = int(remaining.pop(0))
                data = data[:limit]

            return [
                (
                    item["id"],
                    item["subject"],
                    item["predicate"],
                    item["object"],
                    item["source"],
                    item["confidence"],
                    item["metadata_json"],
                    item["valid_from"],
                )
                for item in data
            ]

        if "from facts order by created_at desc" in normalized:
            limit = int(params[0])
            data = sorted(self.rows, key=lambda item: item["created_at"], reverse=True)[:limit]
            return [
                (
                    item["id"],
                    item["subject"],
                    item["predicate"],
                    item["object"],
                    item["source"],
                    item["confidence"],
                    item["metadata_json"],
                    item["valid_from"],
                )
                for item in data
            ]
        raise AssertionError(f"unexpected SQL: {sql}")


class FakeCorrectionsDB:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._sequence = 0

    def execute(self, sql, params=None, *, fetch="none", commit=True):  # noqa: ANN001
        _ = fetch, commit
        normalized = " ".join(str(sql).lower().split())
        if normalized.startswith("create table") or normalized.startswith("create index"):
            return None
        if normalized.startswith("insert into corrections"):
            row_id, session_id, user_text, assistant_text, correction_json = params
            self._sequence += 1
            self.rows.append(
                {
                    "id": row_id,
                    "session_id": session_id,
                    "user_text": user_text,
                    "assistant_text": assistant_text,
                    "correction_json": correction_json,
                    "created_at": datetime.utcnow(),
                    "created_seq": self._sequence,
                }
            )
            return None
        if "from corrections order by created_at desc" in normalized:
            limit = int(params[0])
            ordered = sorted(self.rows, key=lambda item: item["created_seq"], reverse=True)[:limit]
            return [
                (
                    item["id"],
                    item["session_id"],
                    item["user_text"],
                    item["assistant_text"],
                    item["correction_json"],
                    item["created_at"],
                )
                for item in ordered
            ]
        if normalized.startswith("select count(1) from corrections"):
            return (len(self.rows),)
        raise AssertionError(f"unexpected SQL: {sql}")
