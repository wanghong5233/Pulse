"""Recall memory —— 中期对话与工具调用记忆。

检索路径为 **agentic search**：
- `recent(...)`: 按 scope (session/task/workspace) 拉最近 N 条；
- `search_keyword(...)`: 对 text 字段做 PG ILIKE 匹配，由 Brain 侧生成多组关键词串联查询。

内核不自带语义召回。关于这一决策，参见
`docs/Pulse-MemoryRuntime设计.md` 附录 B: Retrieval 策略抉择。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from ..storage.engine import DatabaseEngine
from .envelope import MemoryEnvelope, MemoryKind

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_metadata(raw: Any) -> dict[str, Any]:
    """DB 里 metadata 列可能是 JSON 文本 / dict / NULL, 这里统一成 dict.

    解析失败 = **schema 漂移**或**上游写入 bug**, 不是业务异常 — 记 debug 日志
    便于排查, 但不 raise (单条记录坏不该导致整个 recall 列表抓取失败).
    """
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.debug(
                "recall_memory: metadata JSON decode failed (skipping): %s; raw=%r",
                exc, text[:120],
            )
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


class RecallMemory:
    """PostgreSQL-backed recall memory (agentic search, no vector)."""

    def __init__(
        self,
        *,
        storage_path: str | None = None,
        db_engine: DatabaseEngine | None = None,
    ) -> None:
        _ = storage_path
        self._db = db_engine or DatabaseEngine()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                session_id TEXT,
                task_id TEXT,
                run_id TEXT,
                workspace_id TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_calls (
                id TEXT PRIMARY KEY,
                conversation_id TEXT REFERENCES conversations(id),
                session_id TEXT,
                task_id TEXT,
                run_id TEXT,
                workspace_id TEXT,
                tool_name TEXT NOT NULL,
                tool_args JSONB NOT NULL DEFAULT '{}'::jsonb,
                tool_result JSONB,
                status TEXT NOT NULL,
                latency_ms INTEGER,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        self._migrate_add_columns()
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversations_session_created_at ON conversations(session_id, created_at DESC)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_conversations_created_at ON conversations(created_at DESC)")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_conversations_task_id ON conversations(task_id)")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_conversations_run_id ON conversations(run_id)")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tool_calls_session_created_at ON tool_calls(session_id, created_at DESC)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_name ON tool_calls(tool_name)")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_task_id ON tool_calls(task_id)")

    def _migrate_add_columns(self) -> None:
        """Idempotent migration: add task_id/run_id/workspace_id to existing tables."""
        for col in ("task_id", "run_id", "workspace_id"):
            for table in ("conversations", "tool_calls"):
                self._db.execute(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} TEXT"  # noqa: S608
                )

    def _insert_entry(
        self,
        *,
        role: str,
        text: str,
        metadata: dict[str, Any],
        session_id: str,
        task_id: str | None = None,
        run_id: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        entry_id = uuid.uuid4().hex
        timestamp = _utc_now_iso()
        self._db.execute(
            """
            INSERT INTO conversations(id, role, text, metadata_json, session_id, task_id, run_id, workspace_id, created_at)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::timestamptz)
            """,
            (
                entry_id,
                role,
                text,
                json.dumps(metadata, ensure_ascii=False),
                session_id,
                task_id,
                run_id,
                workspace_id,
                timestamp,
            ),
        )
        return {
            "id": entry_id,
            "role": role,
            "text": text,
            "timestamp": timestamp,
            "metadata": dict(metadata),
        }

    def add_interaction(
        self,
        *,
        user_text: str,
        assistant_text: str,
        metadata: dict[str, Any] | None = None,
        session_id: str = "default",
        task_id: str | None = None,
        run_id: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        safe_session_id = str(session_id or "default").strip() or "default"
        safe_metadata = dict(metadata or {})
        safe_metadata["session_id"] = safe_session_id
        user_entry = self._insert_entry(
            role="user",
            text=str(user_text or "").strip(),
            metadata=safe_metadata,
            session_id=safe_session_id,
            task_id=task_id,
            run_id=run_id,
            workspace_id=workspace_id,
        )
        assistant_entry = self._insert_entry(
            role="assistant",
            text=str(assistant_text or "").strip(),
            metadata=safe_metadata,
            session_id=safe_session_id,
            task_id=task_id,
            run_id=run_id,
            workspace_id=workspace_id,
        )
        return {
            "user_id": user_entry["id"],
            "assistant_id": assistant_entry["id"],
            "total": self.count(),
        }

    def add_entry(self, *, role: str, text: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        safe_role = str(role or "").strip().lower()
        if safe_role not in {"user", "assistant", "system"}:
            raise ValueError("role must be one of: user, assistant, system")
        safe_text = str(text or "").strip()
        if not safe_text:
            raise ValueError("text must be non-empty")
        safe_metadata = dict(metadata or {})
        safe_session_id = str(safe_metadata.get("session_id") or "default").strip() or "default"
        return self._insert_entry(
            role=safe_role,
            text=safe_text,
            metadata=safe_metadata,
            session_id=safe_session_id,
        )

    def recent(
        self,
        *,
        limit: int = 20,
        session_id: str | None = None,
        task_id: str | None = None,
        workspace_id: str | None = None,
        role: str | None = None,
        roles: "tuple[str, ...] | list[str] | None" = None,
    ) -> list[dict[str, Any]]:
        """最近 N 条 conversation 记录 (倒序查询后正序返回).

        ``roles`` 优先于 ``role``; 传 ``roles=("user","assistant")`` 用于
        只拉"真实对话", 排除 ``store_envelope`` 写入的 role=system 的
        task_summary 条目 (否则它们会被当成对话历史回灌给 LLM, 形成
        递归嵌套 summary, 见 F11).
        """
        safe_limit = max(1, min(int(limit), 200))
        safe_session = str(session_id or "").strip()
        safe_task = str(task_id or "").strip()
        safe_workspace = str(workspace_id or "").strip()
        safe_role = str(role or "").strip().lower()
        safe_roles: tuple[str, ...] = tuple(
            {str(r or "").strip().lower() for r in (roles or ()) if str(r or "").strip()}
        )

        where: list[str] = []
        params: list[Any] = []
        if safe_session:
            where.append("session_id = %s")
            params.append(safe_session)
        if safe_task:
            where.append("task_id = %s")
            params.append(safe_task)
        if safe_workspace:
            where.append("workspace_id = %s")
            params.append(safe_workspace)
        if safe_roles:
            # role ∈ (...) — ANY(%s) 对 postgres text[] 也适用, 保持与
            # DatabaseEngine 的 param 转义一致, 不手工拼 SQL.
            placeholders = ",".join(["%s"] * len(safe_roles))
            where.append(f"role IN ({placeholders})")
            params.extend(safe_roles)
        elif safe_role:
            where.append("role = %s")
            params.append(safe_role)

        sql = (
            "SELECT id, role, text, metadata_json, session_id, task_id, run_id, workspace_id, created_at "
            "FROM conversations"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(safe_limit)

        rows = self._db.execute(sql, tuple(params), fetch="all") or []
        rows = list(reversed(rows))
        return [
            {
                "id": str(row_id),
                "role": str(entry_role or ""),
                "text": str(text or ""),
                "session_id": str(entry_session or ""),
                "task_id": str(entry_task or ""),
                "run_id": str(entry_run or ""),
                "workspace_id": str(entry_workspace or ""),
                "timestamp": str(created_at.isoformat() if hasattr(created_at, "isoformat") else created_at or ""),
                "metadata": _parse_metadata(metadata_raw),
            }
            for row_id, entry_role, text, metadata_raw, entry_session, entry_task, entry_run, entry_workspace, created_at in rows
        ]

    def search_keyword(
        self,
        *,
        keywords: list[str] | str,
        top_k: int = 5,
        session_id: str | None = None,
        workspace_id: str | None = None,
        role: str | None = None,
        match: str = "any",
    ) -> list[dict[str, Any]]:
        """Agentic search：按关键词做 SQL ILIKE 匹配。

        由 Brain（或上层模块）自行决定生成哪些关键词（同义词、改写、拆分），
        内核只负责执行匹配。推荐用法：

            hits = recall.search_keyword(
                keywords=["字节", "笔试挂", "bytedance"],
                match="any",
                session_id=ctx.session_id,
            )

        参数：
        - keywords: 单个或多个关键词，全部对 text 字段 ILIKE。
        - match="any": 任一关键词命中即返回（OR）。
        - match="all": 所有关键词都命中才返回（AND）。
        """
        if isinstance(keywords, str):
            kw_list = [keywords]
        else:
            kw_list = [str(k).strip() for k in keywords if str(k or "").strip()]
        if not kw_list:
            return []

        safe_top_k = max(1, min(int(top_k), 50))
        match_mode = "all" if str(match).strip().lower() == "all" else "any"
        joiner = " AND " if match_mode == "all" else " OR "

        where_clauses: list[str] = []
        params: list[Any] = []

        kw_clauses: list[str] = []
        for kw in kw_list:
            kw_clauses.append("text ILIKE %s")
            params.append(f"%{kw}%")
        where_clauses.append("(" + joiner.join(kw_clauses) + ")")

        if str(session_id or "").strip():
            where_clauses.append("session_id = %s")
            params.append(str(session_id).strip())
        if str(workspace_id or "").strip():
            where_clauses.append("workspace_id = %s")
            params.append(str(workspace_id).strip())
        if str(role or "").strip():
            where_clauses.append("role = %s")
            params.append(str(role).strip().lower())

        sql = (
            "SELECT id, role, text, metadata_json, session_id, task_id, run_id, workspace_id, created_at "
            "FROM conversations WHERE " + " AND ".join(where_clauses)
            + " ORDER BY created_at DESC LIMIT %s"
        )
        params.append(safe_top_k)
        rows = self._db.execute(sql, tuple(params), fetch="all") or []

        items: list[dict[str, Any]] = []
        for row_id, entry_role, text, metadata_raw, entry_session, entry_task, entry_run, entry_workspace, created_at in rows:
            items.append(
                {
                    "id": str(row_id),
                    "role": str(entry_role or ""),
                    "text": str(text or ""),
                    "session_id": str(entry_session or ""),
                    "task_id": str(entry_task or ""),
                    "run_id": str(entry_run or ""),
                    "workspace_id": str(entry_workspace or ""),
                    "timestamp": str(created_at.isoformat() if hasattr(created_at, "isoformat") else created_at or ""),
                    "metadata": _parse_metadata(metadata_raw),
                    "matched_keywords": [kw for kw in kw_list if kw.lower() in str(text or "").lower()],
                }
            )
        return items

    def count(self) -> int:
        row = self._db.execute("SELECT COUNT(1) FROM conversations", fetch="one")
        if not row:
            return 0
        return int(row[0] or 0)

    def record_tool_call(
        self,
        *,
        session_id: str = "default",
        task_id: str | None = None,
        run_id: str | None = None,
        workspace_id: str | None = None,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_result: Any = None,
        status: str = "ok",
        latency_ms: int | None = None,
        conversation_id: str | None = None,
    ) -> str:
        """Write a tool invocation record to the tool_calls table."""
        entry_id = uuid.uuid4().hex
        result_json: str | None = None
        if tool_result is not None:
            try:
                result_json = json.dumps(tool_result, ensure_ascii=False, default=str)
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "recall_memory: tool_result not JSON-serializable, storing string "
                    "preview tool=%s err=%s",
                    tool_name, exc,
                )
                result_json = json.dumps({"_str": str(tool_result)[:2000]})
        self._db.execute(
            """
            INSERT INTO tool_calls(id, conversation_id, session_id, task_id, run_id, workspace_id,
                                   tool_name, tool_args, tool_result, status, latency_ms, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, NOW())
            """,
            (
                entry_id,
                conversation_id,
                str(session_id or "default"),
                task_id,
                run_id,
                workspace_id,
                str(tool_name or ""),
                json.dumps(dict(tool_args or {}), ensure_ascii=False),
                result_json,
                str(status or "ok"),
                latency_ms,
            ),
        )
        return entry_id

    # -- Envelope-based write -----------------------------------------------

    def store_envelope(self, envelope: MemoryEnvelope) -> str:
        """Write a MemoryEnvelope to recall storage.

        Routes to the appropriate internal method based on envelope.kind.
        Returns the stored entry ID.
        """
        c = envelope.content
        if envelope.kind == MemoryKind.conversation:
            entry = self._insert_entry(
                role=str(c.get("role", "system")),
                text=str(c.get("text", "")),
                metadata=c.get("metadata") or {},
                session_id=envelope.session_id or "default",
                task_id=envelope.task_id or None,
                run_id=envelope.run_id or None,
                workspace_id=envelope.workspace_id or None,
            )
            return str(entry["id"])

        if envelope.kind == MemoryKind.tool_call:
            return self.record_tool_call(
                session_id=envelope.session_id or "default",
                task_id=envelope.task_id or None,
                run_id=envelope.run_id or None,
                workspace_id=envelope.workspace_id or None,
                tool_name=str(c.get("tool_name", "")),
                tool_args=c.get("tool_args") or {},
                tool_result=c.get("tool_result"),
                status=str(c.get("status", "ok")),
                latency_ms=c.get("latency_ms"),
            )

        entry = self._insert_entry(
            role="system",
            text=json.dumps(c, ensure_ascii=False),
            metadata={"envelope_kind": envelope.kind.value},
            session_id=envelope.session_id or "default",
            task_id=envelope.task_id or None,
            run_id=envelope.run_id or None,
            workspace_id=envelope.workspace_id or None,
        )
        return str(entry["id"])
