"""Data-access layer for the job_chat module.

Persists HR chat events into the ``boss_chat_events`` table and exposes
structured reads for downstream stages. The local JSONL "inbox" fallback
that used to live in ``module.py`` has been retired — the connector is
now the sole source of truth for inbound conversations.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pulse.core.storage.engine import DatabaseEngine

logger = logging.getLogger(__name__)


def _normalize_http_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not (text.startswith("http://") or text.startswith("https://")):
        return ""
    return text[:600]


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    inserted: int
    skipped: int
    error: str | None


class ChatRepository:
    """Persist HR chat events and perform de-dup via ``message_signature``."""

    def __init__(self, engine: DatabaseEngine | None) -> None:
        self._engine = engine

    @property
    def available(self) -> bool:
        return self._engine is not None

    def ingest_events(self, rows: list[dict[str, Any]], *, source: str) -> IngestOutcome:
        """Upsert ingest rows into ``boss_chat_events``.

        Each row must already be normalized (``conversation_id``,
        ``hr_name``, ``company``, ``job_title``, ``latest_message`` all
        populated). De-dup is done on a SHA1 of
        ``conversation_id:latest_message`` so the same HR message ingested
        twice is idempotent.
        """
        if not rows:
            return IngestOutcome(inserted=0, skipped=0, error=None)
        if self._engine is None:
            return IngestOutcome(
                inserted=0,
                skipped=0,
                error="DB engine is not configured; chat ingest requires a database",
            )
        now_iso = datetime.now(timezone.utc).isoformat()
        inserted = 0
        skipped = 0
        last_error: str | None = None
        for payload in rows:
            try:
                row_inserted = self._insert_single(payload, source=source, now_iso=now_iso)
            except Exception as exc:
                last_error = str(exc)[:400]
                logger.warning(
                    "chat repository insert failed for conversation_id=%s: %s",
                    payload.get("conversation_id"),
                    exc,
                )
                continue
            if row_inserted:
                inserted += 1
            else:
                skipped += 1
        if inserted > 0:
            # A single transient failure mid-batch should not mask the
            # successes — we still surface the last error so the caller
            # can attach it to the audit trail.
            return IngestOutcome(inserted=inserted, skipped=skipped, error=last_error)
        return IngestOutcome(inserted=0, skipped=skipped, error=last_error)

    def _insert_single(
        self,
        payload: dict[str, Any],
        *,
        source: str,
        now_iso: str,
    ) -> bool:
        assert self._engine is not None  # guarded by caller
        _ = source, now_iso  # reserved for future schema columns
        conversation_id = str(payload.get("conversation_id") or "").strip()
        latest_message = str(payload.get("latest_message") or "").strip()
        if not conversation_id:
            logger.warning("chat repository skip row missing conversation_id: %s", payload)
            return False
        msg_sig = hashlib.sha1(
            f"{conversation_id}-{latest_message}".encode("utf-8")
        ).hexdigest()[:32]
        result = self._engine.execute(
            """
            INSERT INTO boss_chat_events(
                id, conversation_id, hr_name, company, job_title,
                latest_hr_message, message_signature, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (message_signature) DO NOTHING
            RETURNING id
            """,
            (
                uuid.uuid4().hex,
                conversation_id,
                str(payload.get("hr_name") or ""),
                str(payload.get("company") or ""),
                str(payload.get("job_title") or ""),
                latest_message,
                msg_sig,
            ),
            fetch="one",
        )
        return result is not None

    @staticmethod
    def normalize_row(row: dict[str, Any]) -> dict[str, Any] | None:
        """Project an arbitrary provider/ingest payload into the canonical shape."""
        hr_name = str(row.get("hr_name") or "").strip()
        company = str(row.get("company") or "").strip()
        job_title = str(row.get("job_title") or "").strip()
        latest_message = str(
            row.get("latest_message") or row.get("latest_hr_message") or ""
        ).strip()
        if not hr_name or not company or not job_title or not latest_message:
            return None
        conversation_id = str(row.get("conversation_id") or "").strip()
        if not conversation_id:
            seed = f"{company}-{job_title}-{hr_name}"
            conversation_id = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
        conversation_url = _normalize_http_url(
            row.get("conversation_url") or row.get("chat_url") or row.get("url")
        )
        latest_time = str(row.get("latest_time") or row.get("latest_hr_time") or "刚刚")
        unread_count = max(0, min(int(row.get("unread_count") or 0), 99))
        cards_raw = row.get("cards")
        cards: list[dict[str, Any]] = []
        if isinstance(cards_raw, list):
            for card in cards_raw:
                if isinstance(card, dict) and card.get("card_type"):
                    cards.append(
                        {
                            "card_id": str(card.get("card_id") or ""),
                            "card_type": str(card.get("card_type") or ""),
                            "title": str(card.get("title") or ""),
                            "available_actions": [
                                str(action)
                                for action in (card.get("available_actions") or [])
                                if str(action).strip()
                            ],
                        }
                    )
        return {
            "conversation_id": conversation_id,
            "conversation_url": conversation_url,
            "hr_name": hr_name,
            "company": company,
            "job_title": job_title,
            "latest_message": latest_message,
            "latest_time": latest_time,
            "unread_count": unread_count,
            "initiated_by": str(row.get("initiated_by") or "unknown"),
            "first_contact_at": str(row.get("first_contact_at") or ""),
            "cards": cards,
        }

    @staticmethod
    def to_ingest_payload(row: Any, *, source: str, now_iso: str) -> dict[str, Any]:
        """Project a ``JobChatIngestItem``-like object into an insert payload."""
        payload = row.model_dump() if hasattr(row, "model_dump") else dict(row)
        if not payload.get("conversation_id"):
            seed = (
                f"{payload.get('company','')}-{payload.get('job_title','')}-"
                f"{payload.get('hr_name','')}-{payload.get('latest_message','')}"
            )
            payload["conversation_id"] = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
        payload["source"] = str(source or "manual").strip() or "manual"
        payload["ingested_at"] = now_iso
        return payload


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


__all__ = ["ChatRepository", "IngestOutcome"]
