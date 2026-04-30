from __future__ import annotations

from fastmcp import FastMCP

try:
    from . import _boss_platform_runtime as runtime
except Exception:  # pragma: no cover - fallback for direct script execution
    from pulse.mcp_servers import _boss_platform_runtime as runtime

_MCP = FastMCP("boss-platform")


@_MCP.tool
def health() -> dict:
    """Return boss platform MCP runtime health."""
    return runtime.health()


@_MCP.tool
def check_login(check_url: str = "") -> dict:
    """Validate BOSS login session via browser profile."""
    return runtime.check_login(check_url=check_url)


@_MCP.tool
def reset_browser_session(reason: str = "manual") -> dict:
    """Close browser session; next call recreates it."""
    return runtime.reset_browser_session(reason=reason)


@_MCP.tool
def scan_jobs(
    keyword: str,
    target_count: int = 10,
    evaluation_cap: int = 60,
    scroll_plateau_rounds: int = 3,
    max_items: int = 0,
    max_pages: int = 0,
    job_type: str = "all",
    city: str = "",
) -> dict:
    """Streaming scan via sidebar scroll until target/cap/plateau hit.

    ``target_count`` = early-stop threshold; ``evaluation_cap`` = hard
    ceiling; ``scroll_plateau_rounds`` = scroll-empty streak that declares
    sidebar exhausted. ``max_items`` / ``max_pages`` are deprecated aliases
    kept for backward-compat with older callers; pass 0 to ignore them.
    """
    return runtime.scan_jobs(
        keyword=keyword,
        target_count=target_count if target_count > 0 else None,
        evaluation_cap=evaluation_cap if evaluation_cap > 0 else None,
        scroll_plateau_rounds=scroll_plateau_rounds if scroll_plateau_rounds > 0 else None,
        max_items=max_items if max_items > 0 else None,
        max_pages=max_pages if max_pages > 0 else None,
        job_type=job_type,
        city=(city or None),
    )


@_MCP.tool
def job_detail(job_id: str = "", source_url: str = "") -> dict:
    """Fetch a compact job detail payload."""
    return runtime.job_detail(job_id=job_id, source_url=source_url)


@_MCP.tool
def greet_job(
    run_id: str = "",
    job_id: str = "",
    source_url: str = "",
    job_title: str = "",
    company: str = "",
    greeting_text: str = "",
) -> dict:
    """Trigger greet action (audit-first, executor pluggable)."""
    return runtime.greet_job(
        run_id=run_id,
        job_id=job_id,
        source_url=source_url,
        job_title=job_title,
        company=company,
        greeting_text=greeting_text,
    )


@_MCP.tool
def pull_conversations(
    max_conversations: int = 20,
    unread_only: bool = False,
    fetch_latest_hr: bool = True,
    chat_tab: str = "全部",
) -> dict:
    """Pull conversation list from source inbox."""
    return runtime.pull_conversations(
        max_conversations=max_conversations,
        unread_only=unread_only,
        fetch_latest_hr=fetch_latest_hr,
        chat_tab=chat_tab,
    )


@_MCP.tool
def reply_conversation(
    conversation_id: str,
    reply_text: str,
    profile_id: str = "default",
    conversation_hint: dict | None = None,
) -> dict:
    """Reply to one conversation with profile context."""
    return runtime.reply_conversation(
        conversation_id=conversation_id,
        reply_text=reply_text,
        profile_id=profile_id,
        conversation_hint=dict(conversation_hint or {}),
    )


@_MCP.tool
def send_resume_attachment(
    conversation_id: str,
    resume_profile_id: str = "default",
    conversation_hint: dict | None = None,
) -> dict:
    """Send the user's resume as an actual file attachment to the conversation.

    Distinct from ``reply_conversation`` — this executes a real upload /
    built-in resume menu click. Returns ``manual_required`` when the
    runtime has no browser executor so the business layer escalates to
    HITL instead of pretending success.
    """
    return runtime.send_resume_attachment(
        conversation_id=conversation_id,
        resume_profile_id=resume_profile_id,
        conversation_hint=dict(conversation_hint or {}),
    )


@_MCP.tool
def click_conversation_card(
    conversation_id: str,
    card_id: str = "",
    card_type: str = "",
    action: str = "",
) -> dict:
    """Click an interactive conversation card (exchange-resume, interview-invite, ...).

    ``card_type`` is one of ``exchange_resume`` / ``exchange_contact`` /
    ``interview_invite`` / ``job_recommend``. ``action`` is one of
    ``accept`` / ``reject`` / ``view``.
    """
    return runtime.click_conversation_card(
        conversation_id=conversation_id,
        card_id=card_id,
        card_type=card_type,
        action=action,
    )


@_MCP.tool
def auto_reply_cycle(
    max_conversations: int = 5,
    chat_tab: str = "未读",
    dry_run: bool = True,
    profile_id: str = "default",
    run_id: str = "",
) -> dict:
    """Scan unread chat-list, decide per-conversation action, optionally execute.

    ADR-004 §4.5. ``dry_run=True`` (default) returns the decision plan only:
    no DOM click, no audit write. Pass ``dry_run=False`` to actually click
    BOSS buttons (guarded by idempotency on ``(conversation_id,
    decision_kind, trigger_mid)``). Set env ``PULSE_BOSS_AUTOREPLY=off`` to
    kill the feature entirely; ``PULSE_BOSS_AUTOREPLY_FORCE_DRY_RUN=on`` to
    override any caller's ``dry_run=False`` at the runtime boundary.
    """
    return runtime.run_auto_reply_cycle(
        max_conversations=max_conversations,
        chat_tab=chat_tab,
        dry_run=dry_run,
        profile_id=profile_id,
        run_id=run_id,
    )


@_MCP.tool
def mark_processed(conversation_id: str, run_id: str = "", note: str = "") -> dict:
    """Mark a conversation processed."""
    return runtime.mark_processed(
        conversation_id=conversation_id,
        run_id=run_id,
        note=note,
    )


if __name__ == "__main__":
    _MCP.run()
