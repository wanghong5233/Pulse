"""Data-access layer for the job_greet module.

Persists greet actions and reads back the "already-greeted today" set used
for daily-quota and de-dup checks. The repository is the **single writer**
for greet telemetry — the service never touches the database directly.

The primary store is the relational ``actions`` table. A JSONL fallback is
only used when the database is unreachable and is clearly flagged via log
warnings so operators know their data source is degraded.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pulse.core.storage.engine import DatabaseEngine

logger = logging.getLogger(__name__)


class GreetRepository:
    """Persist greet telemetry and expose daily-quota reads."""

    def __init__(
        self,
        *,
        engine: DatabaseEngine | None = None,
        fallback_log_path: Path,
    ) -> None:
        self._engine = engine
        self._fallback_path = fallback_log_path

    @property
    def fallback_log_path(self) -> Path:
        return self._fallback_path

    # ---------------------------------------------------------------- queries

    def today_greeted_urls(self) -> set[str]:
        """今日(UTC) 成功打过招呼的 source_url 集合, 供 daily_limit 配额判断。

        保留本方法是因为 daily_limit 的语义强绑定"当日", 与
        ``all_greeted_urls`` 的跨天去重语义不同。DB 读失败会降级到
        JSONL, 任一源的 IO 异常都转为空集合 — 避免瞬时抖动阻塞投递流程。
        """
        return self._greeted_urls(only_today=True)

    def all_greeted_urls(self) -> set[str]:
        """所有历史成功打过招呼的 source_url 集合, 供跨天去重 (F4)。

        与 ``today_greeted_urls`` 共用底层读取实现; 区别仅是 ``only_today``
        flag. 若 DB 出问题会降级到 JSONL fallback。
        """
        return self._greeted_urls(only_today=False)

    def _greeted_urls(self, *, only_today: bool) -> set[str]:
        if self._engine is not None:
            try:
                if only_today:
                    rows = self._engine.execute(
                        """
                        SELECT output_summary
                        FROM actions
                        WHERE action_type = 'greet'
                          AND status = 'greeted'
                          AND created_at >= DATE_TRUNC('day', NOW() AT TIME ZONE 'UTC')
                        """,
                        (),
                        fetch="all",
                    ) or []
                else:
                    rows = self._engine.execute(
                        """
                        SELECT output_summary
                        FROM actions
                        WHERE action_type = 'greet'
                          AND status = 'greeted'
                        """,
                        (),
                        fetch="all",
                    ) or []
            except Exception as exc:
                logger.warning(
                    "greet repository DB read failed (only_today=%s), degrading to jsonl: %s",
                    only_today, exc,
                )
                return self._read_jsonl_greeted_urls(only_today=only_today)
            urls: set[str] = set()
            for row in rows:
                if not row:
                    continue
                raw = row[0]
                if raw is None:
                    continue
                try:
                    payload = json.loads(str(raw))
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                url = str(payload.get("source_url") or "").strip()
                if url:
                    urls.add(url)
            return urls
        return self._read_jsonl_greeted_urls(only_today=only_today)

    def _read_jsonl_greeted_urls(self, *, only_today: bool) -> set[str]:
        path = self._fallback_path
        if not path.is_file():
            return set()
        today = datetime.now(timezone.utc).date().isoformat()
        urls: set[str] = set()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("greet repository fallback read failed at %s: %s", path, exc)
            return set()
        for line in text.splitlines():
            raw = line.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "").strip() != "greeted":
                continue
            if only_today:
                ts = str(item.get("greeted_at") or "")
                if not ts.startswith(today):
                    continue
            url = str(item.get("source_url") or "").strip()
            if url:
                urls.add(url)
        return urls

    # ---------------------------------------------------------------- writes

    def append_greet_logs(self, rows: list[dict[str, Any]]) -> None:
        """Persist each greet outcome.

        Behaviour:
          * If a DB engine is wired, rows go into the ``actions`` table.
          * If ANY row fails to write to DB we log a warning and fall
            through to the JSONL sink for *that* row, so operators never
            lose telemetry silently.
          * If there's no DB engine we go straight to JSONL.
        """
        if not rows:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        if self._engine is None:
            logger.info("greet repository has no DB engine; writing %d rows to %s", len(rows), self._fallback_path)
            self._append_jsonl(rows, now_iso=now_iso)
            return

        fallback_rows: list[dict[str, Any]] = []
        for row in rows:
            payload = self._to_payload(row, now_iso=now_iso)
            try:
                self._insert_action_row(payload)
            except Exception as exc:
                logger.warning(
                    "greet repository DB insert failed for job_id=%s status=%s: %s",
                    payload.get("job_id"),
                    payload.get("status"),
                    exc,
                )
                fallback_rows.append(row)
        if fallback_rows:
            self._append_jsonl(fallback_rows, now_iso=now_iso)

    def _insert_action_row(self, payload: dict[str, Any]) -> None:
        safe_job_id: str | None = None
        raw_job_id = str(payload.get("job_id") or "").strip()
        if raw_job_id and self._engine is not None:
            exists = self._engine.execute(
                "SELECT id FROM jobs WHERE id = %s LIMIT 1",
                (raw_job_id,),
                fetch="one",
            )
            if exists is not None:
                safe_job_id = raw_job_id
        assert self._engine is not None  # guarded by caller
        self._engine.execute(
            """
            INSERT INTO actions(id, job_id, action_type, input_summary, output_summary, status, created_at)
            VALUES (%s, %s, 'greet', %s, %s, %s, NOW())
            """,
            (
                uuid.uuid4().hex,
                safe_job_id,
                json.dumps(
                    {"job_title": payload["job_title"], "company": payload["company"]},
                    ensure_ascii=False,
                ),
                json.dumps(payload, ensure_ascii=False),
                payload["status"],
            ),
        )

    def _append_jsonl(self, rows: list[dict[str, Any]], *, now_iso: str) -> None:
        try:
            self._fallback_path.parent.mkdir(parents=True, exist_ok=True)
            with self._fallback_path.open("a", encoding="utf-8") as handle:
                for row in rows:
                    payload = self._to_payload(row, now_iso=now_iso)
                    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.error("greet repository failed to write jsonl at %s: %s", self._fallback_path, exc)

    @staticmethod
    def _to_payload(row: dict[str, Any], *, now_iso: str) -> dict[str, Any]:
        return {
            "run_id": str(row.get("run_id") or ""),
            "job_id": str(row.get("job_id") or ""),
            "job_title": str(row.get("job_title") or ""),
            "company": str(row.get("company") or ""),
            "match_score": float(row.get("match_score") or 0.0),
            "source_url": str(row.get("source_url") or ""),
            "source": str(row.get("source") or ""),
            "provider": str(row.get("provider") or ""),
            "status": str(row.get("status") or "unknown"),
            "error": str(row.get("error") or "") or None,
            "attempts": int(row.get("attempts") or 0),
            "greeted_at": now_iso,
        }
