"""Job-domain platform connector contract.

Every platform driver (BOSS / 猎聘 / 智联 / 前程无忧 / ...) must implement
this contract so that the ``job/greet``, ``job/chat`` and ``job/profile``
capabilities stay platform-agnostic at the business-logic layer.

The concrete implementations live under sibling subpackages, e.g.
``job/_connectors/boss/connector.py``. Business code should only depend on
``JobPlatformConnector`` and resolve specific instances through the
connector registry, never via direct imports of concrete classes.

### Return-payload conventions

All methods return ``dict[str, Any]`` with at minimum:

    {
        "ok": bool,
        "source": str,          # provider_name echo
        # method-specific fields below
    }

On failure:

    {
        "ok": False,
        "source": "...",
        "error": "short machine-readable code",
        "error_message": "human-readable detail",
        # optional: "status": "not_implemented" | "auth_expired" | "rate_limited"
    }

**Never** return ``ok=True`` with a fabricated success when the underlying
platform cannot actually perform the action (e.g. do *not* claim a resume
attachment was sent when only a text message went out). Use
``status="not_implemented"`` instead; business code can then decide whether
to escalate to HITL.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class JobPlatformConnector(ABC):
    """Abstract IO driver for a single job-board platform."""

    # ========================================================================
    # identity / health
    # ========================================================================

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Short, stable platform identifier, e.g. ``boss_mcp``.

        Used for provenance tagging in logs, audit trails and DB rows.
        """

    @property
    @abstractmethod
    def execution_ready(self) -> bool:
        """True when the connector has credentials and can hit the real platform.

        False when running in dry-run / stub mode (e.g. ``web_search`` fallback).
        Business layer should never call write-side methods if this is False;
        it should either escalate to HITL or short-circuit.
        """

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Return a shallow health report used by module ``/health`` endpoints."""

    @abstractmethod
    def check_login(self) -> dict[str, Any]:
        """Return session/login state as seen by the platform."""

    # ========================================================================
    # job scanning / matching
    # ========================================================================

    @abstractmethod
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
        """Search jobs by keyword via streaming-scroll on the live result page.

        Sizing parameters (preferred):
          * ``target_count`` — early-stop threshold once enough cards collected.
          * ``evaluation_cap`` — hard ceiling on total cards returned per call.
          * ``scroll_plateau_rounds`` — # of consecutive empty scrolls before
            declaring the source list exhausted.

        Legacy ``max_items`` / ``max_pages`` are accepted for back-compat but
        new callers should use the streaming knobs. Connectors over platforms
        without true infinite-scroll (e.g. one-shot web search) MUST still
        accept the new params and report ``exhausted=True`` so the business
        layer's reflection logic can decide whether to evolve keywords.

        ``city`` (optional) is a human-readable city name (e.g. ``"杭州"``)
        that the connector SHOULD scope the search to when its backing
        platform supports per-city filtering. When the connector cannot
        recognize the city it MUST fall back to a nationwide scan rather
        than silently returning no results — the business layer still
        applies ``preferred_location`` as a secondary filter.

        Returned items **must** already be platform-normalized:

            {
                "job_id": str,
                "title": str,           # cleaned, no trailing salary / tags
                "company": str,
                "source_url": str,
                "snippet": str,
                "source": str,          # provider_name
                "collected_at": iso8601,
            }

        Returned envelope MUST also expose:

            {
                "exhausted": bool,       # source list truly out of new cards
                "scroll_count": int,     # 0 for non-scrolling providers
            }

        The business layer relies on these fields and **must not** re-parse
        raw DOM strings. Connectors that cannot populate a field should use
        ``""`` / ``None``, not made-up heuristics.
        """

    @abstractmethod
    def fetch_job_detail(
        self,
        *,
        job_id: str,
        source_url: str,
    ) -> dict[str, Any]:
        """Fetch a single job's detail page."""

    # ========================================================================
    # greeting
    # ========================================================================

    @abstractmethod
    def greet_job(
        self,
        *,
        job: dict[str, Any],
        greeting_text: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Send the first 打招呼 message for a given job.

        Returns ``{"ok": bool, "conversation_id": str|None, ...}``.
        """

    def initiate_conversation(
        self,
        *,
        company: str,
        job_id: str,
        hr_hint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Proactively start a conversation with an HR for a known job.

        Optional capability — platforms that can only talk after the HR
        replies must return ``{"ok": False, "status": "not_implemented", ...}``.
        """
        return {
            "ok": False,
            "source": self.provider_name,
            "status": "not_implemented",
            "error": "initiate_conversation_not_supported",
            "error_message": (
                f"{self.provider_name} does not support proactive conversation start"
            ),
        }

    # ========================================================================
    # HR conversations
    # ========================================================================

    @abstractmethod
    def pull_conversations(
        self,
        *,
        max_conversations: int,
        unread_only: bool,
        fetch_latest_hr: bool,
        chat_tab: str,
    ) -> dict[str, Any]:
        """Pull recent HR conversations with optional unread-only filter.

        Each item in the returned ``items`` list **must** populate:

            {
                "conversation_id": str,
                "hr_name": str,
                "company": str,
                "job_title": str,
                "latest_message": str,
                "latest_time": str,          # platform-specific but iso preferred
                "unread_count": int,
                "initiated_by": str,          # "self" | "hr" | "unknown"
                "first_contact_at": str,     # iso8601 or "" if unknown
                "cards": list[dict],         # [] when no interactive card
            }

        A ``cards`` entry shape:

            {
                "card_id": str,
                "card_type": str,             # see shared/enums.CardType
                "title": str,
                "available_actions": list[str],  # e.g. ["accept", "reject"]
            }
        """

    @abstractmethod
    def reply_conversation(
        self,
        *,
        conversation_id: str,
        reply_text: str,
        profile_id: str,
        conversation_hint: dict[str, Any],
    ) -> dict[str, Any]:
        """Send a plain-text reply to a specific conversation."""

    def send_resume_attachment(
        self,
        *,
        conversation_id: str,
        resume_profile_id: str,
        conversation_hint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a resume *attachment* to the conversation.

        This is distinct from ``reply_conversation`` — implementations must
        actually upload/attach a file, not just send a "here is my resume"
        text. Platforms that do not yet expose this capability must return
        ``{"ok": False, "status": "not_implemented", ...}`` so the business
        layer escalates to HITL rather than silently degrading.
        """
        return {
            "ok": False,
            "source": self.provider_name,
            "status": "not_implemented",
            "error": "send_resume_attachment_not_supported",
            "error_message": (
                f"{self.provider_name} cannot send resume attachments yet"
            ),
        }

    def click_conversation_card(
        self,
        *,
        conversation_id: str,
        card_id: str,
        card_type: str,
        action: str,
    ) -> dict[str, Any]:
        """Click an interactive card inside a conversation.

        ``card_type`` is one of ``shared.enums.CardType``;
        ``action`` is one of ``shared.enums.CardAction``.
        """
        return {
            "ok": False,
            "source": self.provider_name,
            "status": "not_implemented",
            "error": "click_conversation_card_not_supported",
            "error_message": (
                f"{self.provider_name} cannot click conversation cards yet"
            ),
        }

    @abstractmethod
    def mark_processed(
        self,
        *,
        conversation_id: str,
        run_id: str,
        note: str,
    ) -> dict[str, Any]:
        """Mark a conversation as processed on the platform side (if supported).

        Platforms without a native "processed" state should return
        ``{"ok": True, "status": "noop", ...}`` rather than raising.
        """
