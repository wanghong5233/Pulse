"""Pulse Workspace Memory — P2 内核组件

对应设计文档 §12.2: workspace 级 summary/facts 聚合。

WorkspaceMemory 是 Memory Runtime 的中间层，位于 recall 和 archival 之间：
  - 存储 workspace summary（由 session→workspace compaction 产出）
  - 存储 workspace-scoped facts（从 session 中提取的中频事实）
  - 为 PromptContract 提供 workspace essentials（heartbeat/task 模式使用）

存储后端复用 DatabaseEngine，独立表 workspace_summaries / workspace_facts。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..storage.engine import DatabaseEngine


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class Fact:
    """workspace_facts 表的一行结构化视图。

    读写约定 (单一事实源):
      - ``workspace_facts.value`` 列永远存 JSON 字符串 (由 ``set_fact`` 统一编码)
      - 读出时永远解码回 Python 对象 (dict / list / primitive / str)
      - Fact.value 的类型即业务写入时的类型, 无 "字符串 vs JSON" 分叉语义

    ``reason`` 是业务侧语义约定的便捷属性: 若 value 是 dict 且含 "reason"
    字段, 通过 ``fact.reason`` 读取。facade 不强制 value 结构, 业务自主。
    """

    workspace_id: str
    key: str
    value: Any
    source: str = ""
    updated_at: str = ""

    @property
    def reason(self) -> str:
        """语义便捷: 如果 value 是 dict 且含 'reason' 字段则返回, 否则空串。"""
        if isinstance(self.value, dict):
            return str(self.value.get("reason", "") or "")
        return ""


def _encode_value(value: Any) -> str:
    """写入前统一 JSON 编码 (ensure_ascii=False 保留中文)。"""
    return json.dumps(value, ensure_ascii=False)


def _decode_value(raw: Any) -> Any:
    """读取时 JSON 解码。

    极端情况 (手工改库 / 外部工具写入非 JSON 文本) 降级为原字符串,
    避免整个 memory 子系统因一行脏数据不可用。业务代码不应依赖此降级。
    """
    if raw is None:
        return ""
    text = str(raw)
    stripped = text.strip()
    if not stripped:
        return ""
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return text


class WorkspaceMemory:
    """Workspace 级别的记忆聚合层。"""

    def __init__(
        self,
        *,
        db_engine: DatabaseEngine | None = None,
    ) -> None:
        self._db = db_engine or DatabaseEngine()
        self._ensure_schema()

    @property
    def db_engine(self) -> DatabaseEngine:
        """对底层 engine 的只读访问; 供需要直连 engine 的 facade 复用同一连接池."""
        return self._db

    def _ensure_schema(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_summaries (
                id BIGSERIAL PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                token_estimate INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_facts (
                id BIGSERIAL PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )

    # ── Summary ────────────────────────────────────────────

    def get_summary(self, workspace_id: str) -> str:
        """读取 workspace summary。"""
        row = self._db.execute(
            "SELECT summary FROM workspace_summaries WHERE workspace_id = %s "
            "ORDER BY updated_at DESC LIMIT 1",
            (workspace_id,),
            fetch="one",
        )
        if not row:
            return ""
        return str(row[0] or "")

    def set_summary(
        self,
        workspace_id: str,
        summary: str,
        token_estimate: int = 0,
    ) -> None:
        """写入或更新 workspace summary（upsert 语义）。"""
        now = _utc_now_iso()
        existing = self._db.execute(
            "SELECT id FROM workspace_summaries WHERE workspace_id = %s LIMIT 1",
            (workspace_id,),
            fetch="one",
        )
        if existing:
            self._db.execute(
                "UPDATE workspace_summaries SET summary = %s, token_estimate = %s, updated_at = %s "
                "WHERE workspace_id = %s",
                (summary, token_estimate, now, workspace_id),
            )
        else:
            self._db.execute(
                "INSERT INTO workspace_summaries (workspace_id, summary, token_estimate, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (workspace_id, summary, token_estimate, now, now),
            )

    # ── Facts (key-value) ──────────────────────────────────
    #
    # 设计契约 (见 docs/Pulse-DomainMemory与Tool模式.md §3.1):
    #   - value 列永远存 JSON 字符串, 由 set_fact 统一编码
    #   - 公共读 API (get_fact / list_facts_by_prefix) 永远返回解码后对象
    #   - 业务 facade 只需关心 Python 对象, 不需关心序列化

    def get_fact(
        self,
        workspace_id: str,
        key: str,
        default: Any = None,
    ) -> Any:
        """读取单个 workspace fact, 已 JSON 解码。

        - key 不存在 → 返回 ``default``
        - 其他情况 → 返回解码后 Python 对象 (dict / list / primitive / str)
        """
        row = self._db.execute(
            "SELECT value FROM workspace_facts WHERE workspace_id = %s AND key = %s LIMIT 1",
            (workspace_id, key),
            fetch="one",
        )
        if not row:
            return default
        return _decode_value(row[0])

    def set_fact(
        self,
        workspace_id: str,
        key: str,
        value: Any,
        *,
        source: str = "",
    ) -> None:
        """写入或更新单个 workspace fact (upsert), value 自动 JSON 编码。

        ``value`` 可以是任意 JSON 可序列化对象 (dict / list / primitive / str)。
        业务侧如需记录 reason, 直接在 value dict 中放 ``reason`` 字段。
        """
        payload = _encode_value(value)
        now = _utc_now_iso()
        existing = self._db.execute(
            "SELECT id FROM workspace_facts WHERE workspace_id = %s AND key = %s LIMIT 1",
            (workspace_id, key),
            fetch="one",
        )
        if existing:
            self._db.execute(
                "UPDATE workspace_facts SET value = %s, source = %s, updated_at = %s "
                "WHERE workspace_id = %s AND key = %s",
                (payload, source, now, workspace_id, key),
            )
        else:
            self._db.execute(
                "INSERT INTO workspace_facts (workspace_id, key, value, source, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (workspace_id, key, payload, source, now, now),
            )

    def delete_fact(self, workspace_id: str, key: str) -> bool:
        """删除单个 workspace fact。"""
        self._db.execute(
            "DELETE FROM workspace_facts WHERE workspace_id = %s AND key = %s",
            (workspace_id, key),
        )
        return True

    def list_facts_by_prefix(
        self,
        workspace_id: str,
        prefix: str,
    ) -> list[Fact]:
        """查询 workspace 下 key 以 prefix 开头的所有 facts。

        返回 ``Fact`` 列表, ``value`` 已 JSON 解码; 按 key 升序排列。
        空 prefix 退化为"列出全部"。
        """
        like = f"{prefix}%" if prefix else "%"
        rows = self._db.execute(
            """
            SELECT key, value, source, updated_at
            FROM workspace_facts
            WHERE workspace_id = %s AND key LIKE %s
            ORDER BY key ASC
            """,
            (workspace_id, like),
            fetch="all",
        ) or []
        out: list[Fact] = []
        for row in rows:
            if not row:
                continue
            out.append(
                Fact(
                    workspace_id=workspace_id,
                    key=str(row[0] or ""),
                    value=_decode_value(row[1]),
                    source=str(row[2] or ""),
                    updated_at=str(row[3] or ""),
                )
            )
        return out

    def delete_facts_by_prefix(
        self,
        workspace_id: str,
        prefix: str,
    ) -> int:
        """批量删除 key 以 prefix 开头的 facts, 返回删除行数。

        空 prefix 会拒绝执行 (防止误删整张表的 workspace 数据)。
        """
        if not prefix:
            raise ValueError("delete_facts_by_prefix requires a non-empty prefix")
        existing = self._db.execute(
            "SELECT COUNT(*) FROM workspace_facts "
            "WHERE workspace_id = %s AND key LIKE %s",
            (workspace_id, f"{prefix}%"),
            fetch="one",
        )
        count = int(existing[0]) if existing and existing[0] is not None else 0
        if count > 0:
            self._db.execute(
                "DELETE FROM workspace_facts WHERE workspace_id = %s AND key LIKE %s",
                (workspace_id, f"{prefix}%"),
            )
        return count

    # ── Essentials (for PromptContract) ────────────────────

    def read_essentials(self, workspace_id: str) -> dict[str, Any]:
        """读取 workspace essentials, 供 PromptContract 渲染到 system prompt。

        返回 summary + facts 列表; facts 里 ``value`` 是渲染用的字符串
        (非原始 JSON 对象), 因为 PromptContract 的消费者直接拼接到 markdown。
        业务侧若要拿结构化对象, 请用 ``get_fact`` / ``list_facts_by_prefix``。
        """
        summary = self.get_summary(workspace_id)
        facts = [
            {
                "key": fact.key,
                "value": _encode_value(fact.value) if not isinstance(fact.value, str) else fact.value,
                "source": fact.source,
            }
            for fact in self.list_facts_by_prefix(workspace_id, "")
        ]
        return {
            "workspace_id": workspace_id,
            "summary": summary,
            "facts": facts,
        }
