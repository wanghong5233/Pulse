"""Memory Reader adapter — 将现有 Memory 实例适配为 PromptContractBuilder.MemoryReader 接口。

包含两层:
  - MemoryReaderAdapter: 直接包装底层 memory 实例
  - IsolatedMemoryReader: 根据 IsolationLevel 过滤读取范围 (§10.3)

Session Isolation 策略:
  - shared (mainSession):    完整上下文 — core + recall + archival
  - light_context:           Core + workspace essentials，不读 recall/archival
  - isolated:                Core + task brief，空 recall，空 archival

**失败语义** (fail-loud, don't fail-closed):
  Memory backend 抛异常 = 基础设施问题(DB 掉了 / schema 不兼容), Brain 需要感知.
  这里**统一**捕获为 ``logger.exception`` 并返回安全空值 — 但日志级别是 ERROR,
  事件面可通过日志聚合报警. 不再用 bare ``except Exception: return {}`` 这种"让
  prompt builder 静默跑空"的反模式(会让 Brain 以为"用户没任何记忆", 从而做出错
  决策且无从审计).
"""

from __future__ import annotations

import logging
from typing import Any

from .task_context import IsolationLevel, TaskContext

logger = logging.getLogger(__name__)


class MemoryReaderAdapter:
    """包装 core_memory / recall_memory / archival_memory 为统一只读接口。"""

    def __init__(
        self,
        *,
        core_memory: Any | None = None,
        recall_memory: Any | None = None,
        archival_memory: Any | None = None,
        workspace_memory: Any | None = None,
    ) -> None:
        self._core = core_memory
        self._recall = recall_memory
        self._archival = archival_memory
        self._workspace = workspace_memory

    def read_core_snapshot(self) -> dict[str, Any]:
        if self._core is None:
            return {}
        try:
            return self._core.snapshot()
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            logger.exception("memory_reader: read_core_snapshot failed; returning empty")
            return {}

    def read_recent(self, session_id: str | None, limit: int) -> list[dict[str, Any]]:
        if self._recall is None:
            return []
        try:
            # 明确只拉真实对话 (user / assistant); role=system 的 envelope
            # JSON 条目 (由 compaction 写入) **不应该**出现在 "Recent
            # Conversation History" section — 那会让 summary 被当作对话
            # 历史回灌给 LLM, 下一轮 compaction 再包一层, 形成递归嵌套
            # summary (F11 的根因).
            return self._recall.recent(
                limit=limit,
                session_id=session_id or "default",
                roles=("user", "assistant"),
            )
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            logger.exception(
                "memory_reader: read_recent failed session=%s limit=%s; returning []",
                session_id, limit,
            )
            return []

    def search_recall(self, query: str, session_id: str | None, top_k: int) -> list[dict[str, Any]]:
        if self._recall is None:
            return []
        try:
            return self._recall.search_keyword(
                keywords=query,
                top_k=top_k,
                session_id=session_id,
            )
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            logger.exception(
                "memory_reader: search_recall failed q=%r top_k=%s; returning []",
                query[:60] if isinstance(query, str) else query, top_k,
            )
            return []

    def search_archival(self, query: str, limit: int) -> list[dict[str, Any]]:
        if self._archival is None:
            return []
        try:
            return self._archival.query(keyword=query, limit=limit)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            logger.exception(
                "memory_reader: search_archival failed q=%r limit=%s; returning []",
                query[:60] if isinstance(query, str) else query, limit,
            )
            return []

    def read_workspace_essentials(self, workspace_id: str | None) -> dict[str, Any]:
        if self._workspace is None or not workspace_id:
            return {}
        try:
            return self._workspace.read_essentials(workspace_id)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            logger.exception(
                "memory_reader: read_workspace_essentials failed ws=%s; returning {}",
                workspace_id,
            )
            return {}


class IsolatedMemoryReader:
    """根据 TaskContext.isolation_level 过滤 MemoryReader 的读取范围。

    策略 (§10.3):
      shared (mainSession):  完整上下文
      light_context:         Core + workspace essentials，recall/archival 返回空
      isolated:              Core only，recall/archival/workspace 全部返回空
    """

    def __init__(self, inner: MemoryReaderAdapter, ctx: TaskContext) -> None:
        self._inner = inner
        self._isolation = ctx.isolation_level
        self._workspace_id = ctx.workspace_id

    def read_core_snapshot(self) -> dict[str, Any]:
        # 所有隔离级别都可以读 core
        return self._inner.read_core_snapshot()

    def read_recent(self, session_id: str | None, limit: int) -> list[dict[str, Any]]:
        if self._isolation != IsolationLevel.shared:
            return []
        return self._inner.read_recent(session_id, limit)

    def search_recall(self, query: str, session_id: str | None, top_k: int) -> list[dict[str, Any]]:
        if self._isolation != IsolationLevel.shared:
            return []
        return self._inner.search_recall(query, session_id, top_k)

    def search_archival(self, query: str, limit: int) -> list[dict[str, Any]]:
        if self._isolation != IsolationLevel.shared:
            return []
        return self._inner.search_archival(query, limit)

    def read_workspace_essentials(self, workspace_id: str | None) -> dict[str, Any]:
        if self._isolation == IsolationLevel.isolated:
            return {}
        return self._inner.read_workspace_essentials(workspace_id or self._workspace_id)
