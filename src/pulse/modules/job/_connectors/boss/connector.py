"""BOSS direct-recruitment platform connector.

This file is the *only* place in the job domain that knows about HTTP
endpoints, MCP tools, cookies or web-search fallbacks for BOSS. Business
modules (``job/greet``, ``job/chat``, ``job/profile``) talk to this class
through the :class:`JobPlatformConnector` contract and the connector
registry — they never import this module directly.

Responsibilities:
  * select an execution mode (``openapi`` / ``mcp`` / ``web_search``)
  * rate-limit and retry every provider call
  * append an append-only audit trail for every call
  * normalize platform payloads into the shape declared by
    :class:`JobPlatformConnector`

All configuration arrives via :class:`BossConnectorSettings` — this module
contains **zero** ``os.getenv`` calls.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pulse.core.mcp_transport_http import HttpMCPTransport
from pulse.core.tokenizer import token_preview
from pulse.core.tools.web_search import search_web

from ..base import JobPlatformConnector
from .settings import BossConnectorSettings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

_LOCAL_SEED_JOBS: tuple[tuple[str, str, str], ...] = (
    ("AI Agent Intern", "Pulse Labs", "200-300/天"),
    ("LLM Application Engineer (Intern)", "NovaMind", "180-280/天"),
    ("AI 产品实习生", "DeepBridge", "150-220/天"),
    ("RAG Engineer (Intern)", "VectorWorks", "220-320/天"),
    ("Backend Engineer (Python)", "Orbit AI", "160-240/天"),
    ("MCP Tooling Intern", "Signal Stack", "200-260/天"),
)


class _ConnectorError(RuntimeError):
    """Non-retryable provider error."""


class _RetryableConnectorError(_ConnectorError):
    """Retryable provider error."""


class _AuthExpiredConnectorError(_ConnectorError):
    """Provider auth / cookie / token is invalid."""


@dataclass(slots=True)
class _ConnectorCall:
    ok: bool
    result: Any
    attempts: int
    error: str | None = None


class _RateLimiter:
    def __init__(self, min_interval_sec: float) -> None:
        self._min_interval_sec = max(0.0, float(min_interval_sec))
        self._lock = threading.Lock()
        self._last_call_at: dict[str, float] = {}

    def wait(self, key: str) -> None:
        if self._min_interval_sec <= 0:
            return
        wait_sec = 0.0
        now = time.monotonic()
        with self._lock:
            previous = self._last_call_at.get(key)
            if previous is not None:
                elapsed = now - previous
                if elapsed < self._min_interval_sec:
                    wait_sec = self._min_interval_sec - elapsed
            self._last_call_at[key] = now + wait_sec
        if wait_sec > 0:
            time.sleep(wait_sec)


class _AuditLogger:
    def __init__(self, storage_path: Path) -> None:
        self._storage_path = storage_path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._storage_path

    def append(self, payload: dict[str, Any]) -> None:
        row = dict(payload)
        row["logged_at"] = datetime.now(timezone.utc).isoformat()
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(row, ensure_ascii=False)
            with self._lock:
                with self._storage_path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError as exc:
            # Audit logging must never crash the main flow, but must be visible.
            logger.warning("boss audit write failed at %s: %s", self._storage_path, exc)


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _read_cookie_header(cookie_path: Path | None) -> str:
    if cookie_path is None or not cookie_path.is_file():
        return ""
    try:
        raw_text = cookie_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("boss cookie read failed at %s: %s", cookie_path, exc)
        return ""
    if not raw_text:
        return ""
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return raw_text
    if isinstance(payload, dict):
        direct = str(payload.get("cookie") or payload.get("cookies") or "").strip()
        if direct:
            return direct
        cookie_rows = payload.get("items")
        if isinstance(cookie_rows, list):
            parts: list[str] = []
            for row in cookie_rows:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or "").strip()
                value = str(row.get("value") or "").strip()
                if name and value:
                    parts.append(f"{name}={value}")
            if parts:
                return "; ".join(parts)
    if isinstance(payload, list):
        parts = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            value = str(row.get("value") or "").strip()
            if name and value:
                parts.append(f"{name}={value}")
        if parts:
            return "; ".join(parts)
    return raw_text


def _extract_items(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return [dict(item) for item in body if isinstance(item, dict)]
    if not isinstance(body, dict):
        return []
    direct = body.get("items")
    if isinstance(direct, list):
        return [dict(item) for item in direct if isinstance(item, dict)]
    data = body.get("data")
    if isinstance(data, dict):
        nested = data.get("items")
        if isinstance(nested, list):
            return [dict(item) for item in nested if isinstance(item, dict)]
    return []


def _extract_errors(body: Any) -> list[str]:
    if isinstance(body, dict):
        if isinstance(body.get("errors"), list):
            return [str(item)[:400] for item in body["errors"]]
        error = str(body.get("error") or body.get("message") or "").strip()
        if error:
            return [error[:400]]
    return []


# ---------------------------------------------------------------------------
# BossPlatformConnector
# ---------------------------------------------------------------------------


class BossPlatformConnector(JobPlatformConnector):
    """Concrete :class:`JobPlatformConnector` for BOSS Zhipin."""

    def __init__(self, settings: BossConnectorSettings) -> None:
        self._settings = settings
        self._rate_limiter = _RateLimiter(settings.rate_limit_sec)
        self._audit = _AuditLogger(settings.audit_path)
        self._last_auth_error = ""
        self._degraded_reason = ""
        self._mode = self._resolve_mode(settings.provider_override)
        self._mcp_transport: HttpMCPTransport | None = None
        if self._mode == "mcp":
            try:
                self._mcp_transport = HttpMCPTransport(
                    base_url=settings.mcp.base_url,
                    timeout_sec=settings.mcp.timeout_sec,
                    auth_token=settings.mcp.token,
                )
            except Exception as exc:  # defensive: transport c-tor should not raise
                self._degraded_reason = f"mcp transport init failed: {exc}"
                self._mode = "unconfigured"
                logger.warning("boss mcp transport init failed: %s", exc)
        self._execution_ready = self._mode in {"openapi", "mcp"}
        logger.info(
            "BossPlatformConnector initialized mode=%s execution_ready=%s",
            self._mode,
            self._execution_ready,
        )

    # ------------------------------------------------------------------ identity

    @property
    def provider_name(self) -> str:
        if self._mode == "openapi":
            return "boss_openapi"
        if self._mode == "mcp":
            return "boss_mcp"
        if self._mode == "web_search":
            return "boss_web_search"
        return "boss_unconfigured"

    @property
    def execution_ready(self) -> bool:
        return self._execution_ready

    def _resolve_mode(self, override: str) -> str:
        if override in {"openapi", "boss_openapi"}:
            if self._settings.openapi.base_url:
                return "openapi"
            self._degraded_reason = "PULSE_BOSS_PROVIDER=openapi but openapi base_url is missing"
            return "unconfigured"
        if override in {"mcp", "boss_mcp"}:
            if self._settings.mcp.base_url:
                return "mcp"
            self._degraded_reason = "PULSE_BOSS_PROVIDER=mcp but mcp base_url is missing"
            return "unconfigured"
        if override in {"web_search", "search"}:
            if self._settings.allow_web_search_fallback:
                self._degraded_reason = "PULSE_BOSS_PROVIDER=web_search enables search-only mode"
                return "web_search"
            self._degraded_reason = (
                "PULSE_BOSS_PROVIDER=web_search is blocked; "
                "set PULSE_BOSS_ALLOW_WEB_SEARCH_FALLBACK=true only for explicit diagnostics"
            )
            return "unconfigured"
        if self._settings.mcp.base_url:
            return "mcp"
        if self._settings.openapi.base_url:
            return "openapi"
        self._degraded_reason = "no openapi or mcp connector configured"
        return "unconfigured"

    # ------------------------------------------------------------------ scan

    def scan_jobs(
        self,
        *,
        keyword: str,
        max_items: int | None = None,
        max_pages: int | None = None,
        target_count: int | None = None,
        evaluation_cap: int | None = None,
        scroll_plateau_rounds: int | None = None,
        job_type: str = "all",
        city: str | None = None,
    ) -> dict[str, Any]:
        """Scan jobs from BOSS via streaming-scroll (preferred) or fallbacks.

        New parameters override the legacy ``max_items`` / ``max_pages``
        sizing. The remote runtime treats them as the source of truth; old
        params survive only as deprecated aliases for back-compat with
        patrol callers that haven't been migrated yet.
        """
        safe_keyword = str(keyword or "").strip() or "AI Agent 实习"
        legacy_items = max(1, min(int(max_items), 200)) if max_items is not None else None
        safe_target = (
            max(1, min(int(target_count), 200))
            if target_count is not None
            else (legacy_items if legacy_items is not None else 10)
        )
        legacy_pages = max(1, min(int(max_pages), 8)) if max_pages is not None else None
        safe_cap = (
            max(safe_target, min(int(evaluation_cap), 200))
            if evaluation_cap is not None
            else max(safe_target, 60)
        )
        safe_plateau = (
            max(1, min(int(scroll_plateau_rounds), 8))
            if scroll_plateau_rounds is not None
            else 3
        )
        safe_city = (str(city).strip() or None) if city else None
        payload: dict[str, Any] = {
            "keyword": safe_keyword,
            "target_count": safe_target,
            "evaluation_cap": safe_cap,
            "scroll_plateau_rounds": safe_plateau,
            "job_type": str(job_type or "all").strip() or "all",
        }
        if legacy_items is not None:
            payload["max_items"] = legacy_items
        if legacy_pages is not None:
            payload["max_pages"] = legacy_pages
        if safe_city:
            # Downstream MCP/OpenAPI handlers treat an absent ``city`` as
            # nationwide scan; only pass it when the business layer explicitly
            # asked for a city-scoped search.
            payload["city"] = safe_city
        if self._mode == "openapi":
            call = self._invoke(
                "scan_jobs",
                payload,
                lambda: self._openapi_call(self._settings.openapi.scan_path, payload),
            )
            return self._normalize_scan_call(call, default_source="boss_openapi")
        if self._mode == "mcp":
            call = self._invoke(
                "scan_jobs",
                payload,
                lambda: self._mcp_call(self._settings.mcp.scan_tool, payload),
            )
            return self._normalize_scan_call(call, default_source="boss_mcp")
        if self._mode == "web_search":
            return self._scan_with_web_search(payload)
        return {
            "ok": False,
            "items": [],
            "pages_scanned": 1,
            "scroll_count": 0,
            "exhausted": False,
            "source": self.provider_name,
            "errors": [self._degraded_reason or "provider is not execution-ready"],
            "attempts": 0,
        }

    def fetch_job_detail(
        self,
        *,
        job_id: str,
        source_url: str,
    ) -> dict[str, Any]:
        payload = {
            "job_id": str(job_id or "").strip(),
            "source_url": str(source_url or "").strip(),
        }
        if self._mode == "openapi" and self._settings.openapi.detail_path:
            call = self._invoke(
                "job_detail",
                payload,
                lambda: self._openapi_call(self._settings.openapi.detail_path, payload),
            )
            return self._normalize_detail_call(call)
        if self._mode == "mcp" and self._settings.mcp.detail_tool:
            call = self._invoke(
                "job_detail",
                payload,
                lambda: self._mcp_call(self._settings.mcp.detail_tool, payload),
            )
            return self._normalize_detail_call(call)
        return {
            "ok": False,
            "detail": {},
            "provider": self.provider_name,
            "source": self.provider_name,
            "error": "job detail is unavailable in current provider mode",
            "attempts": 0,
        }

    # ------------------------------------------------------------------ greet

    def greet_job(
        self,
        *,
        job: dict[str, Any],
        greeting_text: str,
        run_id: str,
    ) -> dict[str, Any]:
        payload = {
            "run_id": str(run_id or "").strip(),
            "job_id": str(job.get("job_id") or "").strip(),
            "source_url": str(job.get("source_url") or "").strip(),
            "job_title": str(job.get("title") or "").strip(),
            "company": str(job.get("company") or "").strip(),
            "greeting_text": str(greeting_text or "").strip(),
        }
        if not self._execution_ready:
            self._audit.append(
                {
                    "provider": self.provider_name,
                    "operation": "greet_job",
                    "status": "dry_run",
                    "request": payload,
                    "reason": "provider is not execution-ready",
                }
            )
            return {
                "ok": False,
                "status": "dry_run",
                "provider": self.provider_name,
                "source": self.provider_name,
                "error": "provider is not execution-ready",
                "attempts": 0,
            }
        if self._mode == "openapi":
            call = self._invoke(
                "greet_job",
                payload,
                lambda: self._openapi_call(self._settings.openapi.greet_path, payload),
            )
        else:
            call = self._invoke(
                "greet_job",
                payload,
                lambda: self._mcp_call(self._settings.mcp.greet_tool, payload),
            )
        return self._normalize_action_call(call, success_status="sent")

    # ------------------------------------------------------------------ pull / reply

    def pull_conversations(
        self,
        *,
        max_conversations: int,
        unread_only: bool,
        fetch_latest_hr: bool,
        chat_tab: str,
    ) -> dict[str, Any]:
        payload = {
            "max_conversations": max(1, min(int(max_conversations), 200)),
            "unread_only": bool(unread_only),
            "fetch_latest_hr": bool(fetch_latest_hr),
            "chat_tab": str(chat_tab or "全部").strip() or "全部",
        }
        if not self._execution_ready:
            return {
                "ok": False,
                "items": [],
                "source": self.provider_name,
                "errors": ["provider is not execution-ready"],
                "attempts": 0,
            }
        if self._mode == "openapi":
            call = self._invoke(
                "pull_conversations",
                payload,
                lambda: self._openapi_call(self._settings.openapi.pull_path, payload),
            )
        else:
            call = self._invoke(
                "pull_conversations",
                payload,
                lambda: self._mcp_call(self._settings.mcp.pull_tool, payload),
            )
        if not call.ok:
            return {
                "ok": False,
                "items": [],
                "source": self.provider_name,
                "errors": [str(call.error or "pull conversation failed")[:400]],
                "attempts": call.attempts,
            }
        items = _extract_items(call.result)
        errors = _extract_errors(call.result)
        unread_total = 0
        if isinstance(call.result, dict):
            unread_total = max(0, int(call.result.get("unread_total") or 0))
        return {
            "ok": True,
            "items": items,
            "source": str((call.result or {}).get("source") if isinstance(call.result, dict) else "") or self.provider_name,
            "errors": errors,
            "unread_total": unread_total,
            "attempts": call.attempts,
        }

    def reply_conversation(
        self,
        *,
        conversation_id: str,
        reply_text: str,
        profile_id: str,
        conversation_hint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "conversation_id": str(conversation_id or "").strip(),
            "reply_text": str(reply_text or "").strip(),
            "profile_id": str(profile_id or "default").strip() or "default",
        }
        if conversation_hint:
            payload["conversation_hint"] = dict(conversation_hint)
        if not self._execution_ready:
            return {
                "ok": False,
                "status": "dry_run",
                "source": self.provider_name,
                "error": "provider is not execution-ready",
                "attempts": 0,
            }
        if self._mode == "openapi":
            call = self._invoke(
                "reply_conversation",
                payload,
                lambda: self._openapi_call(self._settings.openapi.reply_path, payload),
            )
        else:
            call = self._invoke(
                "reply_conversation",
                payload,
                lambda: self._mcp_call(self._settings.mcp.reply_tool, payload),
            )
        return self._normalize_action_call(call, success_status="sent")

    def send_resume_attachment(
        self,
        *,
        conversation_id: str,
        resume_profile_id: str,
        conversation_hint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._mode != "mcp":
            # Attachment upload is only wired through the browser-backed MCP
            # runtime today — OpenAPI and web_search cannot perform it.
            return {
                "ok": False,
                "source": self.provider_name,
                "status": "not_implemented",
                "error": "send_resume_attachment_not_supported_in_mode",
                "error_message": (
                    f"send_resume_attachment requires provider=mcp, current={self._mode}"
                ),
            }
        if not self._execution_ready:
            return {
                "ok": False,
                "source": self.provider_name,
                "status": "dry_run",
                "error": "provider is not execution-ready",
                "attempts": 0,
            }
        payload = {
            "conversation_id": str(conversation_id or "").strip(),
            "resume_profile_id": str(resume_profile_id or "default").strip() or "default",
        }
        if conversation_hint:
            payload["conversation_hint"] = dict(conversation_hint)
        call = self._invoke(
            "send_resume_attachment",
            payload,
            lambda: self._mcp_call(self._settings.mcp.send_attachment_tool, payload),
        )
        return self._normalize_action_call(call, success_status="sent")

    def click_conversation_card(
        self,
        *,
        conversation_id: str,
        card_id: str,
        card_type: str,
        action: str,
    ) -> dict[str, Any]:
        if self._mode != "mcp":
            return {
                "ok": False,
                "source": self.provider_name,
                "status": "not_implemented",
                "error": "click_conversation_card_not_supported_in_mode",
                "error_message": (
                    f"click_conversation_card requires provider=mcp, current={self._mode}"
                ),
            }
        if not self._execution_ready:
            return {
                "ok": False,
                "source": self.provider_name,
                "status": "dry_run",
                "error": "provider is not execution-ready",
                "attempts": 0,
            }
        payload = {
            "conversation_id": str(conversation_id or "").strip(),
            "card_id": str(card_id or "").strip(),
            "card_type": str(card_type or "").strip(),
            "action": str(action or "").strip(),
        }
        call = self._invoke(
            "click_conversation_card",
            payload,
            lambda: self._mcp_call(self._settings.mcp.click_card_tool, payload),
        )
        return self._normalize_action_call(call, success_status="clicked")

    def mark_processed(
        self,
        *,
        conversation_id: str,
        run_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        payload = {
            "conversation_id": str(conversation_id or "").strip(),
            "run_id": str(run_id or "").strip(),
            "note": str(note or "").strip(),
        }
        if not self._execution_ready:
            return {
                "ok": False,
                "status": "dry_run",
                "source": self.provider_name,
                "error": "provider is not execution-ready",
                "attempts": 0,
            }
        if self._mode == "openapi":
            call = self._invoke(
                "mark_processed",
                payload,
                lambda: self._openapi_call(self._settings.openapi.mark_path, payload),
            )
        else:
            call = self._invoke(
                "mark_processed",
                payload,
                lambda: self._mcp_call(self._settings.mcp.mark_tool, payload),
            )
        return self._normalize_action_call(call, success_status="marked")

    # ------------------------------------------------------------------ health / auth

    def check_login(self) -> dict[str, Any]:
        if not self._execution_ready:
            return {
                "ok": False,
                "status": "provider_unavailable",
                "source": self.provider_name,
                "provider": self.provider_name,
                "error": "provider is not execution-ready",
                "attempts": 0,
            }
        if self._mode == "mcp" and self._settings.mcp.check_login_tool:
            call = self._invoke(
                "check_login",
                {},
                lambda: self._mcp_call(self._settings.mcp.check_login_tool, {}),
            )
            if not call.ok:
                return {
                    "ok": False,
                    "status": "failed",
                    "source": self.provider_name,
                    "provider": self.provider_name,
                    "error": str(call.error or "check login failed")[:400],
                    "attempts": call.attempts,
                }
            body = call.result if isinstance(call.result, dict) else {"value": call.result}
            status = str(body.get("status") or "").strip() or ("ready" if bool(body.get("ok")) else "failed")
            ok = bool(body.get("ok")) if "ok" in body else status == "ready"
            error = str(body.get("error") or body.get("message") or "").strip() or None
            return {
                "ok": ok,
                "status": status,
                "source": self.provider_name,
                "provider": self.provider_name,
                "error": error[:400] if error else None,
                "attempts": call.attempts,
                "result": body,
            }
        cookie_loaded = bool(_read_cookie_header(self._settings.cookie_path))
        token_ready = bool(self._settings.openapi.token)
        auth_ready = cookie_loaded or token_ready
        return {
            "ok": auth_ready,
            "status": "ready" if auth_ready else "auth_required",
            "source": self.provider_name,
            "provider": self.provider_name,
            "error": None if auth_ready else "openapi token/cookie is missing",
            "attempts": 0,
            "result": {
                "cookie_loaded": cookie_loaded,
                "token_ready": token_ready,
            },
        }

    def health(self) -> dict[str, Any]:
        cookie_loaded = bool(_read_cookie_header(self._settings.cookie_path))
        payload: dict[str, Any] = {
            "provider": self.provider_name,
            "mode": self._mode,
            "execution_ready": self._execution_ready,
            "degraded": not self._execution_ready,
            "degraded_reason": self._degraded_reason or None,
            "fallbacks": {
                "web_search_enabled": self._mode == "web_search",
                "seed_enabled": self._settings.allow_seed_fallback,
            },
            "retry": {
                "count": self._settings.retry_count,
                "backoff_sec": self._settings.retry_backoff_sec,
            },
            "rate_limit_sec": self._settings.rate_limit_sec,
            "audit_path": str(self._audit.path),
            "check_login_supported": bool(
                (self._mode == "mcp" and self._settings.mcp.check_login_tool) or self._mode == "openapi"
            ),
            "capabilities": {
                "send_resume_attachment": self._mode == "mcp" and bool(self._settings.mcp.send_attachment_tool),
                "click_conversation_card": self._mode == "mcp" and bool(self._settings.mcp.click_card_tool),
            },
            "auth": {
                "cookie_path": str(self._settings.cookie_path) if self._settings.cookie_path else None,
                "cookie_loaded": cookie_loaded,
                "token_configured": bool(self._settings.openapi.token or self._settings.mcp.token),
                "last_auth_error": self._last_auth_error or None,
            },
        }
        if self._settings.openapi.base_url:
            payload["openapi"] = {
                "base_url": self._settings.openapi.base_url,
                "scan_path": self._settings.openapi.scan_path,
                "greet_path": self._settings.openapi.greet_path,
                "pull_path": self._settings.openapi.pull_path,
                "auth_status_path": self._settings.openapi.auth_status_path or None,
            }
        if self._settings.mcp.base_url:
            payload["mcp"] = {
                "base_url": self._settings.mcp.base_url,
                "server": self._settings.mcp.server,
                "scan_tool": self._settings.mcp.scan_tool,
                "greet_tool": self._settings.mcp.greet_tool,
                "pull_tool": self._settings.mcp.pull_tool,
                "check_login_tool": self._settings.mcp.check_login_tool or None,
                "send_attachment_tool": self._settings.mcp.send_attachment_tool or None,
                "click_card_tool": self._settings.mcp.click_card_tool or None,
            }
        return payload

    # ------------------------------------------------------------------ web-search fallback

    def _scan_with_web_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        safe_keyword = str(payload.get("keyword") or "").strip() or "AI Agent 实习"
        # Web-search fallback has no scrolling; map target/cap onto the
        # legacy items/pages knobs as a one-shot best-effort.
        safe_items = max(
            1,
            min(int(payload.get("evaluation_cap") or payload.get("max_items") or 10), 80),
        )
        safe_pages = max(1, min(int(payload.get("max_pages") or 3), 8))
        query_pool = (
            f"site:zhipin.com {safe_keyword} 实习",
            f"site:zhipin.com {safe_keyword} 招聘",
            f"site:zhipin.com {safe_keyword} 岗位",
            f"{safe_keyword} BOSS直聘",
        )
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        errors: list[str] = []
        pages_scanned = 0
        for query in query_pool[:safe_pages]:
            pages_scanned += 1
            try:
                hits = search_web(query, max_results=min(12, safe_items * 2))
            except Exception as exc:
                errors.append(str(exc)[:400])
                logger.warning("boss web_search query failed: %s", exc)
                continue
            for hit in hits:
                if len(rows) >= safe_items:
                    break
                source_url = str(hit.url or "").strip()
                title = str(hit.title or "").strip()
                if not source_url and not title:
                    continue
                dedupe_key = (source_url or title).lower()
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                rows.append(
                    {
                        "job_id": _sha(dedupe_key),
                        "title": title,
                        "company": "",
                        "salary": None,
                        "source_url": source_url,
                        "snippet": token_preview(str(hit.snippet or ""), max_tokens=700),
                        "source": "boss_web_search",
                        "collected_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
            if len(rows) >= safe_items:
                break

        if not rows and self._settings.allow_seed_fallback:
            seeded = int(hashlib.sha1(safe_keyword.encode("utf-8")).hexdigest()[:8], 16)
            for idx in range(safe_items):
                template = _LOCAL_SEED_JOBS[(seeded + idx) % len(_LOCAL_SEED_JOBS)]
                title, company, salary = template
                source_url = f"https://www.zhipin.com/job_detail/seed_{seeded}_{idx}"
                rows.append(
                    {
                        "job_id": _sha(source_url),
                        "title": title,
                        "company": company,
                        "salary": salary,
                        "source_url": source_url,
                        "snippet": f"{company} 正在招聘 {title}，关键词：{safe_keyword}",
                        "source": "boss_local_seed",
                        "collected_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
            errors.append("web provider unavailable; switched to local seed dataset")
        elif not rows:
            errors.append("web provider returned no jobs and seed fallback is disabled")

        source = "boss_web_search"
        if rows and rows[0].get("source") == "boss_local_seed":
            source = "boss_local_seed"
        return {
            "ok": bool(rows),
            "items": rows[:safe_items],
            "pages_scanned": max(1, pages_scanned),
            "scroll_count": 0,
            # Web-search has no notion of "load more"; treat the response as
            # the only batch this provider can yield so reflection can
            # decide whether to evolve keywords.
            "exhausted": True,
            "source": source,
            "errors": errors,
            "attempts": 1,
        }

    # ------------------------------------------------------------------ call normalization

    def _normalize_scan_call(self, call: _ConnectorCall, *, default_source: str) -> dict[str, Any]:
        if not call.ok:
            return {
                "ok": False,
                "items": [],
                "pages_scanned": 1,
                "scroll_count": 0,
                "exhausted": False,
                "source": default_source,
                "errors": [str(call.error or "scan failed")[:400]],
                "attempts": call.attempts,
            }
        body = call.result if isinstance(call.result, dict) else {}
        items = _extract_items(body)
        pages_scanned = max(1, int(body.get("pages_scanned") or body.get("pages") or 1))
        scroll_count = int(body.get("scroll_count") or 0)
        exhausted = bool(body.get("exhausted"))
        return {
            "ok": True,
            "items": items,
            "pages_scanned": pages_scanned,
            "scroll_count": max(0, scroll_count),
            "exhausted": exhausted,
            "source": str(body.get("source") or "") or default_source,
            "errors": _extract_errors(body),
            "attempts": call.attempts,
        }

    def _normalize_detail_call(self, call: _ConnectorCall) -> dict[str, Any]:
        if not call.ok:
            return {
                "ok": False,
                "detail": {},
                "source": self.provider_name,
                "provider": self.provider_name,
                "error": str(call.error or "job detail failed")[:400],
                "attempts": call.attempts,
            }
        detail: dict[str, Any] = {}
        if isinstance(call.result, dict):
            nested = call.result.get("detail")
            if isinstance(nested, dict):
                detail = dict(nested)
            else:
                detail = dict(call.result)
        return {
            "ok": True,
            "detail": detail,
            "source": self.provider_name,
            "provider": self.provider_name,
            "error": None,
            "attempts": call.attempts,
        }

    def _normalize_action_call(self, call: _ConnectorCall, *, success_status: str) -> dict[str, Any]:
        if not call.ok:
            return {
                "ok": False,
                "status": "failed",
                "source": self.provider_name,
                "provider": self.provider_name,
                "error": str(call.error or "provider action failed")[:400],
                "attempts": call.attempts,
            }
        status = success_status
        error = ""
        if isinstance(call.result, dict):
            status = str(call.result.get("status") or success_status).strip() or success_status
            if "ok" in call.result and not bool(call.result.get("ok")):
                error = str(call.result.get("error") or call.result.get("message") or "").strip()
        ok = not error
        return {
            "ok": ok,
            "status": status if ok else "failed",
            "source": self.provider_name,
            "provider": self.provider_name,
            "error": error[:400] if error else None,
            "attempts": call.attempts,
            "result": call.result if isinstance(call.result, dict) else {"value": call.result},
        }

    # ------------------------------------------------------------------ invoke / transport

    # MUTATING operations — HTTP retry on these is NOT idempotent (each retry
    # re-triggers the real platform-side side-effect: a new greeting message /
    # a new reply / a new resume attachment, exactly once per attempt). Even
    # when the backend-side HTTP call times out, the MCP gateway keeps the
    # browser-side click running to completion and records `status=sent` in
    # /root/.pulse/boss_mcp_actions.jsonl — retrying therefore duplicates the
    # outbound message. Observed in audit: one imperative-turn utterance
    # produced 4 × (sent=True) audit rows while the backend recorded 3 ×
    # "timed out" and the assistant claimed "投递失败" — textbook silent
    # success + fail-loud failure mismatch. See ADR-001 §6 P3e.
    #
    # This is a WHITELIST, not heuristics — membership is decided by the
    # semantic category "does one call produce a platform-side message?",
    # which matches the tool's own spec (greet_job, reply_conversation,
    # send_resume_attachment). New MUTATING ops MUST be added here.
    _MUTATING_OPERATIONS: frozenset[str] = frozenset(
        {"greet_job", "reply_conversation", "send_resume_attachment"}
    )

    def _effective_retry_count(self, operation: str) -> int:
        if operation in self._MUTATING_OPERATIONS:
            return 0
        return self._settings.retry_count

    def _invoke(
        self,
        operation: str,
        payload: dict[str, Any],
        runner: Callable[[], Any],
    ) -> _ConnectorCall:
        attempts = 0
        last_error = ""
        retry_count = self._effective_retry_count(operation)
        # ADR-005 §4: connector is the bridge between service-layer stages
        # (stage=...) and the remote MCP gateway (mcp.call.*). One start +
        # one end line lets post-mortem reconstruct "did we even *attempt*
        # the call" vs. "did we get stuck before the call".
        started = time.monotonic()
        logger.info(
            "boss.call.start op=%s mode=%s retry_count=%d",
            operation,
            self._mode,
            retry_count,
        )
        for attempt in range(retry_count + 1):
            attempts = attempt + 1
            self._rate_limiter.wait(operation)
            try:
                result = runner()
                self._audit.append(
                    {
                        "provider": self.provider_name,
                        "operation": operation,
                        "status": "ok",
                        "attempt": attempts,
                        "request": payload,
                        "response_preview": self._preview(result),
                    }
                )
                logger.info(
                    "boss.call.end op=%s status=ok attempts=%d elapsed_ms=%d",
                    operation,
                    attempts,
                    int((time.monotonic() - started) * 1000),
                )
                return _ConnectorCall(ok=True, result=result, attempts=attempts)
            except _RetryableConnectorError as exc:
                last_error = str(exc)[:600]
                self._audit.append(
                    {
                        "provider": self.provider_name,
                        "operation": operation,
                        "status": "retryable_error",
                        "attempt": attempts,
                        "request": payload,
                        "error": last_error,
                    }
                )
                logger.warning(
                    "boss %s retryable error (attempt %d/%d): %s",
                    operation,
                    attempts,
                    retry_count + 1,
                    last_error,
                )
                if attempt >= retry_count:
                    break
                time.sleep(self._settings.retry_backoff_sec * (2**attempt))
            except _AuthExpiredConnectorError as exc:
                last_error = str(exc)[:600]
                self._last_auth_error = last_error
                self._audit.append(
                    {
                        "provider": self.provider_name,
                        "operation": operation,
                        "status": "auth_error",
                        "attempt": attempts,
                        "request": payload,
                        "error": last_error,
                    }
                )
                logger.warning("boss %s auth error: %s", operation, last_error)
                break
            except Exception as exc:
                last_error = str(exc)[:600]
                self._audit.append(
                    {
                        "provider": self.provider_name,
                        "operation": operation,
                        "status": "error",
                        "attempt": attempts,
                        "request": payload,
                        "error": last_error,
                    }
                )
                logger.warning("boss %s error: %s", operation, last_error)
                break
        logger.warning(
            "boss.call.end op=%s status=failed attempts=%d elapsed_ms=%d error=%s",
            operation,
            attempts,
            int((time.monotonic() - started) * 1000),
            last_error[:200],
        )
        return _ConnectorCall(ok=False, result={}, attempts=attempts, error=last_error or "provider call failed")

    @staticmethod
    def _preview(value: Any) -> Any:
        if isinstance(value, dict):
            preview = dict(value)
            if "items" in preview and isinstance(preview["items"], list):
                preview["items"] = preview["items"][:2]
            return preview
        if isinstance(value, list):
            return value[:2]
        return str(value)[:400]

    def _openapi_call(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        base_url = self._settings.openapi.base_url
        if not base_url:
            raise _ConnectorError("openapi base url is empty")
        safe_path = path if str(path).startswith("/") else f"/{path}"
        url = f"{base_url}{safe_path}"
        data: bytes | None = None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self._settings.openapi.token:
            headers["Authorization"] = f"Bearer {self._settings.openapi.token}"
        cookie_header = _read_cookie_header(self._settings.cookie_path)
        if cookie_header:
            headers["Cookie"] = cookie_header
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self._settings.openapi.timeout_sec) as response:
                text = response.read().decode("utf-8", errors="ignore")
            if not text.strip():
                return {}
            return json.loads(text)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            message = f"openapi http {exc.code}: {body[:300]}"
            if exc.code in {401, 403}:
                raise _AuthExpiredConnectorError(message) from exc
            if exc.code in {408, 409, 425, 429, 500, 502, 503, 504}:
                raise _RetryableConnectorError(message) from exc
            raise _ConnectorError(message) from exc
        except urllib.error.URLError as exc:
            raise _RetryableConnectorError(f"openapi url error: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise _ConnectorError(f"openapi invalid json: {exc}") from exc

    def _mcp_call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if not self._mcp_transport:
            raise _ConnectorError("mcp transport is not configured")
        if not tool_name:
            raise _ConnectorError("mcp tool name is empty")
        try:
            return self._mcp_transport.call_tool(self._settings.mcp.server, tool_name, arguments)
        except RuntimeError as exc:
            message = str(exc)
            if " 401 " in message or " 403 " in message:
                raise _AuthExpiredConnectorError(f"mcp auth error: {message[:400]}") from exc
            if any(code in message for code in (" 429 ", " 500 ", " 502 ", " 503 ", " 504 ")):
                raise _RetryableConnectorError(f"mcp transient error: {message[:400]}") from exc
            raise _ConnectorError(f"mcp error: {message[:400]}") from exc
        except Exception as exc:
            raise _RetryableConnectorError(f"mcp call failed: {exc}") from exc


def build_boss_platform_connector(
    settings: BossConnectorSettings | None = None,
) -> BossPlatformConnector:
    """Factory used by the connector registry.

    ``settings`` defaults to :func:`get_boss_connector_settings` which loads
    ``PULSE_BOSS_*`` from the environment / ``.env``. Tests may inject a
    custom :class:`BossConnectorSettings` to exercise specific modes without
    touching environment variables.
    """
    from .settings import get_boss_connector_settings

    return BossPlatformConnector(settings or get_boss_connector_settings())
