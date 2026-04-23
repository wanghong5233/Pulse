"""事件持久化 Sink(append-only JSONL).

职责边界(见 ``event_types.py`` 顶部注释):
- ``InMemoryEventStore``: 滑动窗口, 供实时观测(WS/SSE)和最近 N 条查询
- ``JsonlEventSink``: append-only 落盘, 供**审计/回放/合规**

本 sink 只订阅 ``should_persist()`` 认定的事件(LLM/工具/连接器/记忆/策略),
避免把 runtime 会话流和健康检查这类高频低价值事件塞满磁盘.

文件按天滚动: ``<audit_dir>/pulse_events-YYYYMMDD.jsonl``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from .event_types import should_persist

logger = logging.getLogger(__name__)


class JsonlEventSink:
    """Append-only JSONL 持久化 sink, 订阅 ``EventBus`` 使用.

    使用:
        sink = JsonlEventSink(directory="./data/exports/events")
        event_bus.subscribe_all(sink.handle)

    设计要点:
    - 写入失败**永不抛出**, 观测侧绝不能阻塞主流程
    - 每次 append 后 flush, 牺牲吞吐换复盘可用性(事件量不大, 可接受)
    - 线程安全: 单 ``RLock`` 串行化写
    - 按天滚动文件名, 便于 ``jq`` / ``grep`` 按日期筛选
    """

    def __init__(
        self,
        *,
        directory: str | os.PathLike[str] = "./data/exports/events",
        filename_prefix: str = "pulse_events",
        persist_filter: bool = True,
    ) -> None:
        self._dir = Path(directory).expanduser()
        self._prefix = str(filename_prefix).strip() or "pulse_events"
        self._persist_filter = bool(persist_filter)
        self._lock = RLock()
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("JsonlEventSink mkdir failed: dir=%s err=%s", self._dir, exc)

    def _current_path(self, now: datetime) -> Path:
        return self._dir / f"{self._prefix}-{now.strftime('%Y%m%d')}.jsonl"

    def handle(self, event_type: str, payload: dict[str, Any]) -> None:
        """``EventBus.subscribe_all`` 回调签名."""
        safe_type = str(event_type or "").strip() or "unknown"
        if self._persist_filter and not should_persist(safe_type):
            return
        now = datetime.now(timezone.utc)
        record: dict[str, Any] = {
            "timestamp": now.isoformat(),
            "event_type": safe_type,
        }
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key in record:
                    continue
                record[key] = value
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:
            logger.warning("JsonlEventSink serialize failed: type=%s err=%s", safe_type, exc)
            return
        path = self._current_path(now)
        with self._lock:
            try:
                with path.open("a", encoding="utf-8") as fp:
                    fp.write(line)
                    fp.write("\n")
            except OSError as exc:
                logger.warning("JsonlEventSink write failed: path=%s err=%s", path, exc)

    def current_file(self) -> Path:
        return self._current_path(datetime.now(timezone.utc))


__all__ = ["JsonlEventSink"]
