from __future__ import annotations

import email
import hashlib
import imaplib
import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ....core.llm.router import LLMRouter
from ....core.module import BaseModule
from ....core.storage.engine import DatabaseEngine

logger = logging.getLogger(__name__)


@dataclass
class _EmailItem:
    sender: str
    subject: str
    body: str
    received_at: datetime


class _EmailHeartbeatManager:
    def __init__(
        self,
        *,
        runner: Callable[[int, bool], dict[str, Any]],
        interval_sec: int,
        max_items: int,
        mark_seen: bool,
    ) -> None:
        self._runner = runner
        self._interval_sec = max(30, min(int(interval_sec), 24 * 3600))
        self._max_items = max(1, min(int(max_items), 50))
        self._mark_seen = bool(mark_seen)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        self._last_run_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None
        self._last_result: dict[str, Any] | None = None

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="pulse-email-heartbeat")
            self._thread.start()
            return True

    def stop(self, *, join_timeout_sec: float = 1.5) -> bool:
        with self._lock:
            was_running = self._running
            self._running = False
            self._stop_event.set()
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=max(0.1, join_timeout_sec))
        return was_running

    def trigger_once(self) -> dict[str, Any]:
        return self._run_once()

    def status(self) -> dict[str, Any]:
        with self._lock:
            last_result = self._last_result or {}
            return {
                "running": self._running,
                "interval_sec": self._interval_sec,
                "max_items": self._max_items,
                "mark_seen": self._mark_seen,
                "last_run_at": self._last_run_at,
                "last_success_at": self._last_success_at,
                "last_error": self._last_error,
                "last_fetched_count": int(last_result.get("fetched_count") or 0),
                "last_processed_count": int(last_result.get("processed_count") or 0),
            }

    def _loop(self) -> None:
        try:
            self._run_once()
        except Exception as exc:
            logger.warning("email heartbeat initial run failed: %s", exc)
        while not self._stop_event.wait(self._interval_sec):
            try:
                self._run_once()
            except Exception as exc:
                logger.warning("email heartbeat run failed: %s", exc)

    def _run_once(self) -> dict[str, Any]:
        with self._lock:
            self._last_run_at = datetime.utcnow()
        try:
            result = self._runner(self._max_items, self._mark_seen)
            with self._lock:
                self._last_success_at = datetime.utcnow()
                self._last_error = None
                self._last_result = result
            return result
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)[:1000]
            raise


class EmailProcessOneRequest(BaseModel):
    sender: str = Field(..., min_length=3, max_length=300)
    subject: str = Field(..., min_length=1, max_length=500)
    body: str = Field(..., min_length=1)
    received_at: datetime | None = None


class EmailFetchProcessRequest(BaseModel):
    max_items: int = Field(default=10, ge=1, le=50)
    mark_seen: bool = False


def _extract_company(sender: str, subject: str, body: str) -> str | None:
    bracket = re.search(r"[【\[]\s*([A-Za-z0-9\u4e00-\u9fff]{2,30})\s*[】\]]", subject)
    if bracket:
        return bracket.group(1).strip()
    sender_match = re.search(r"@([A-Za-z0-9\-]{2,40})\.", sender)
    if sender_match:
        return sender_match.group(1).strip()
    body_match = re.search(r"([A-Za-z0-9\u4e00-\u9fff]{2,24})(?:公司|科技|集团|实验室)", body)
    if body_match:
        return body_match.group(1).strip()
    return None


def _extract_time(text: str) -> str | None:
    patterns = (
        r"\d{4}[/-]\d{1,2}[/-]\d{1,2}\s*\d{1,2}:\d{2}",
        r"\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}",
        r"\d{4}[/-]\d{1,2}[/-]\d{1,2}",
        r"\d{4}年\d{1,2}月\d{1,2}日",
    )
    for pattern in patterns:
        matched = re.search(pattern, text)
        if matched:
            return matched.group(0).strip()
    return None


def _classify_heuristic(sender: str, subject: str, body: str) -> dict[str, Any]:
    text = f"{subject}\n{body}".lower()
    company = _extract_company(sender, subject, body)
    interview_time = _extract_time(f"{subject}\n{body}")
    if any(key in text for key in ("面试", "interview", "一面", "二面", "三面")):
        email_type = "interview_invite"
        confidence = 0.82
        reason = "Contains interview keywords"
        job_status = "interview"
    elif any(key in text for key in ("未通过", "不合适", "感谢投递", "rejected", "regret")):
        email_type = "rejection"
        confidence = 0.86
        reason = "Contains rejection keywords"
        job_status = "rejected"
        interview_time = None
    elif any(key in text for key in ("补充", "补交", "材料", "作品集", "portfolio", "resume")):
        email_type = "need_material"
        confidence = 0.75
        reason = "Contains material-request keywords"
        job_status = "need_material"
        interview_time = None
    else:
        email_type = "irrelevant"
        confidence = 0.6
        reason = "No high-confidence hiring keywords found"
        job_status = None
        interview_time = None
    return {
        "classification": {
            "email_type": email_type,
            "company": company,
            "interview_time": interview_time,
            "confidence": confidence,
            "reason": reason,
        },
        "related_job_id": None if company is None else f"job::{company.lower()}",
        "updated_job_status": job_status,
    }


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _imap_ready() -> bool:
    host = os.getenv("PULSE_EMAIL_IMAP_HOST", "").strip()
    user = os.getenv("PULSE_EMAIL_IMAP_USER", "").strip()
    password = os.getenv("PULSE_EMAIL_IMAP_PASSWORD", "").strip()
    return bool(host and user and password)


def _decode_header_text(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:
        return raw


def _extract_text_body(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            content_type = str(part.get_content_type() or "").lower()
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition or content_type != "text/plain":
                continue
            payload = part.get_payload(decode=True)
            charset = str(part.get_content_charset() or "utf-8")
            if payload is None:
                continue
            try:
                text = payload.decode(charset, errors="ignore")
            except Exception:
                text = payload.decode("utf-8", errors="ignore")
            text = text.strip()
            if text:
                return text[:4000]
    payload = msg.get_payload(decode=True)
    if payload is None:
        return ""
    charset = str(msg.get_content_charset() or "utf-8")
    try:
        text = payload.decode(charset, errors="ignore")
    except Exception:
        text = payload.decode("utf-8", errors="ignore")
    return text.strip()[:4000]


def _parse_schedule_time(text: str | None) -> datetime | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    candidates = [
        ("%Y-%m-%d %H:%M", raw.replace("/", "-")),
        ("%Y-%m-%d", raw.replace("/", "-")),
        ("%Y年%m月%d日 %H:%M", raw),
        ("%Y年%m月%d日", raw),
    ]
    for fmt, candidate in candidates:
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    return None


def _fetch_imap_emails(*, max_items: int, mark_seen: bool) -> tuple[list[_EmailItem], str | None]:
    host = os.getenv("PULSE_EMAIL_IMAP_HOST", "").strip()
    user = os.getenv("PULSE_EMAIL_IMAP_USER", "").strip()
    password = os.getenv("PULSE_EMAIL_IMAP_PASSWORD", "").strip()
    folder = os.getenv("PULSE_EMAIL_IMAP_FOLDER", "INBOX").strip() or "INBOX"
    search_query = os.getenv("PULSE_EMAIL_IMAP_SEARCH", "UNSEEN").strip() or "UNSEEN"
    use_ssl = _bool_env("PULSE_EMAIL_IMAP_SSL", True)
    port_raw = os.getenv("PULSE_EMAIL_IMAP_PORT", "993").strip()
    try:
        port = int(port_raw)
    except Exception:
        port = 993
    port = max(1, min(port, 65535))
    if not (host and user and password):
        return [], "imap credential missing"

    client: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None
    try:
        client = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
        client.login(user, password)
        readonly = not mark_seen
        status, _ = client.select(folder, readonly=readonly)
        if status != "OK":
            return [], f"imap select folder failed: {folder}"
        status, data = client.search(None, search_query)
        if status != "OK":
            return [], f"imap search failed: {search_query}"
        ids = data[0].split() if data and data[0] else []
        if not ids:
            return [], None
        selected_ids = ids[-max(1, min(max_items, 50)) :]
        emails: list[_EmailItem] = []
        for msg_id in reversed(selected_ids):
            status, parts = client.fetch(msg_id, "(RFC822)")
            if status != "OK" or not parts:
                continue
            raw_bytes = None
            for part in parts:
                if isinstance(part, tuple) and len(part) >= 2:
                    raw_bytes = part[1]
                    break
            if not isinstance(raw_bytes, (bytes, bytearray)):
                continue
            msg = email.message_from_bytes(bytes(raw_bytes))
            sender = _decode_header_text(str(msg.get("From") or ""))
            subject = _decode_header_text(str(msg.get("Subject") or ""))
            body = _extract_text_body(msg) or subject or "(empty)"
            received_at = datetime.utcnow()
            date_header = str(msg.get("Date") or "").strip()
            if date_header:
                try:
                    received_dt = parsedate_to_datetime(date_header)
                    if received_dt.tzinfo is not None:
                        received_at = received_dt.astimezone().replace(tzinfo=None)
                    else:
                        received_at = received_dt
                except Exception:
                    received_at = datetime.utcnow()
            emails.append(_EmailItem(sender=sender or user, subject=subject or "(no subject)", body=body, received_at=received_at))
            if mark_seen and not readonly:
                try:
                    client.store(msg_id, "+FLAGS", "\\Seen")
                except Exception:
                    logger.warning("failed to mark email seen: %s", msg_id)
        return emails, None
    except Exception as exc:
        return [], str(exc)[:300]
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                logger.warning("failed to logout imap client")


class EmailTrackerModule(BaseModule):
    name = "email_tracker"
    description = "Email tracking module with IMAP + LLM classification + DB persistence"
    route_prefix = "/api/modules/email/tracker"
    tags = ["email", "email_tracker"]

    def __init__(self) -> None:
        super().__init__()
        self._heartbeat: _EmailHeartbeatManager | None = None
        self._heartbeat_lock = threading.Lock()
        self._llm_router = LLMRouter()

    def _classify_email(self, *, sender: str, subject: str, body: str) -> dict[str, Any]:
        prompt = (
            "Classify this recruiting email. Return ONLY valid JSON with fields: "
            "{\"email_type\":\"interview_invite|rejection|need_material|irrelevant\","
            "\"company\":\"...\",\"interview_time\":\"... or empty\","
            "\"confidence\":0.0,\"reason\":\"...\",\"updated_job_status\":\"... or empty\"}\n\n"
            f"Sender: {sender[:200]}\n"
            f"Subject: {subject[:500]}\n"
            f"Body: {body[:2000]}"
        )
        try:
            raw = self._llm_router.invoke_text(prompt, route="classification")
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            parsed = json.loads(cleaned)
            email_type = str(parsed.get("email_type") or "").strip() or "irrelevant"
            company = str(parsed.get("company") or "").strip() or _extract_company(sender, subject, body)
            interview_time = str(parsed.get("interview_time") or "").strip() or None
            confidence = max(0.0, min(float(parsed.get("confidence") or 0.5), 1.0))
            reason = str(parsed.get("reason") or "").strip() or "llm_classification"
            updated_job_status = str(parsed.get("updated_job_status") or "").strip() or None
            return {
                "classification": {
                    "email_type": email_type,
                    "company": company,
                    "interview_time": interview_time,
                    "confidence": confidence,
                    "reason": reason,
                },
                "related_job_id": None if company is None else f"job::{company.lower()}",
                "updated_job_status": updated_job_status,
            }
        except Exception as exc:
            logger.warning("email llm classification failed, fallback to heuristic: %s", exc)
            return _classify_heuristic(sender, subject, body)

    def _persist_email_event(self, *, mail: _EmailItem, result: dict[str, Any]) -> None:
        classification = dict(result.get("classification") or {})
        db = DatabaseEngine()
        event_id = uuid.uuid4().hex
        db.execute(
            """INSERT INTO email_events(
                   id, sender, subject, body, email_type, company, interview_time,
                   raw_classification, related_job_id, updated_job_status, received_at, created_at
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, NOW())""",
            (
                event_id,
                mail.sender,
                mail.subject,
                mail.body[:4000],
                str(classification.get("email_type") or "irrelevant"),
                classification.get("company"),
                classification.get("interview_time"),
                json.dumps(classification, ensure_ascii=False),
                result.get("related_job_id"),
                result.get("updated_job_status"),
                mail.received_at,
            ),
        )
        schedule_time = _parse_schedule_time(classification.get("interview_time"))
        if schedule_time is None:
            return
        signature = hashlib.sha1(
            f"{mail.sender}|{mail.subject}|{classification.get('interview_time')}".encode("utf-8")
        ).hexdigest()[:32]
        db.execute(
            """INSERT INTO schedules(
                   id, signature, source_email_id, company, event_type, start_at, raw_time_text,
                   mode, location, contact, confidence, status, created_at, updated_at
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
               ON CONFLICT (signature) DO NOTHING""",
            (
                uuid.uuid4().hex,
                signature,
                event_id,
                classification.get("company"),
                "interview",
                schedule_time,
                classification.get("interview_time"),
                "email",
                "",
                mail.sender,
                float(classification.get("confidence") or 0.8),
                "scheduled",
            ),
        )

    def _run_once(self, *, max_items: int, mark_seen: bool) -> dict[str, Any]:
        safe_max = max(1, min(max_items, 50))
        errors: list[str] = []
        if not _imap_ready():
            return {
                "fetched_count": 0,
                "processed_count": 0,
                "notification_sent": False,
                "schedule_reminders": 0,
                "upcoming_schedules": 0,
                "items": [],
                "source": "imap_unconfigured",
                "errors": ["imap credential missing"],
            }
        emails, imap_error = _fetch_imap_emails(max_items=safe_max, mark_seen=mark_seen)
        if imap_error:
            errors.append(imap_error)
        items: list[dict[str, Any]] = []
        persisted_count = 0
        for mail in emails:
            result = self._classify_email(sender=mail.sender, subject=mail.subject, body=mail.body)
            classification = dict(result.get("classification") or {})
            try:
                self._persist_email_event(mail=mail, result=result)
                persisted_count += 1
            except Exception as exc:
                errors.append(f"persist failed: {str(exc)[:200]}")
            items.append(
                {
                    "sender": mail.sender,
                    "subject": mail.subject,
                    "email_type": classification.get("email_type"),
                    "company": classification.get("company"),
                    "interview_time": classification.get("interview_time"),
                    "related_job_id": result.get("related_job_id"),
                    "updated_job_status": result.get("updated_job_status"),
                    "confidence": classification.get("confidence"),
                }
            )
        return {
            "fetched_count": len(emails),
            "processed_count": len(items),
            "persisted_count": persisted_count,
            "notification_sent": False,
            "schedule_reminders": 0,
            "upcoming_schedules": sum(1 for item in items if item.get("interview_time")),
            "items": items,
            "source": "imap",
            "errors": errors,
        }

    def _get_heartbeat(self) -> _EmailHeartbeatManager:
        with self._heartbeat_lock:
            if self._heartbeat is not None:
                return self._heartbeat
            interval_sec = int(os.getenv("EMAIL_HEARTBEAT_INTERVAL_SEC", "300"))
            max_items = int(os.getenv("EMAIL_HEARTBEAT_MAX_ITEMS", "10"))
            mark_seen = os.getenv("EMAIL_HEARTBEAT_MARK_SEEN", "false").strip().lower() in {
                "1", "true", "yes", "on",
            }
            self._heartbeat = _EmailHeartbeatManager(
                runner=lambda n, seen: self._run_once(max_items=n, mark_seen=seen),
                interval_sec=interval_sec,
                max_items=max_items,
                mark_seen=mark_seen,
            )
            return self._heartbeat

    def on_shutdown(self) -> None:
        hb = self._heartbeat
        if hb is not None:
            hb.stop()

    def run_process_one(self, *, sender: str, subject: str, body: str) -> dict[str, Any]:
        return self._classify_email(sender=sender, subject=subject, body=body)

    def run_fetch_process(self, *, max_items: int, mark_seen: bool) -> dict[str, Any]:
        return self._run_once(max_items=max_items, mark_seen=mark_seen)

    def handle_intent(
        self,
        intent: str,
        text: str,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        _ = metadata
        if intent == "email.process":
            return self.run_process_one(
                sender="channel@pulse.local",
                subject="渠道消息转邮件处理",
                body=text or "empty message",
            )
        if intent == "email.fetch":
            return self.run_fetch_process(max_items=5, mark_seen=False)
        return None

    def register_routes(self, router: APIRouter) -> None:
        @router.get("/health")
        async def health() -> dict[str, Any]:
            return {
                "module": self.name,
                "status": "ok",
                "runtime": {
                    "mode": "imap" if _imap_ready() else "imap_unconfigured",
                    "imap_ready": _imap_ready(),
                    "imap_host": os.getenv("PULSE_EMAIL_IMAP_HOST", "").strip(),
                },
            }

        @router.post("/process-one")
        async def process_one(payload: EmailProcessOneRequest) -> dict[str, Any]:
            try:
                return self.run_process_one(
                    sender=payload.sender,
                    subject=payload.subject,
                    body=payload.body,
                )
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"email process-one failed: {exc}") from exc

        @router.post("/fetch-process")
        async def fetch_process(payload: EmailFetchProcessRequest) -> dict[str, Any]:
            try:
                return self.run_fetch_process(
                    max_items=payload.max_items,
                    mark_seen=payload.mark_seen,
                )
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"email fetch-process failed: {exc}") from exc

        @router.get("/heartbeat/status")
        async def heartbeat_status() -> dict[str, Any]:
            hb = self._get_heartbeat()
            return hb.status()

        @router.post("/heartbeat/start")
        async def heartbeat_start() -> dict[str, Any]:
            hb = self._get_heartbeat()
            started = hb.start()
            return {"ok": True, "started": bool(started), "status": hb.status()}

        @router.post("/heartbeat/stop")
        async def heartbeat_stop() -> dict[str, Any]:
            hb = self._get_heartbeat()
            stopped = hb.stop()
            return {"ok": True, "stopped": bool(stopped), "status": hb.status()}

        @router.post("/heartbeat/trigger")
        async def heartbeat_trigger() -> dict[str, Any]:
            hb = self._get_heartbeat()
            result = hb.trigger_once()
            return {"ok": True, "result": result}


module = EmailTrackerModule()
