from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

import uvicorn
from fastapi import Body, FastAPI, Header, HTTPException

try:
    from . import _boss_platform_runtime as runtime
    from ..core.logging_config import set_trace_id, setup_logging
except Exception:  # pragma: no cover - fallback for direct script execution
    from pulse.mcp_servers import _boss_platform_runtime as runtime
    from pulse.core.logging_config import set_trace_id, setup_logging


# ADR-005 §2: boss_mcp runs in its own process. Without an explicit
# ``setup_logging`` call, ``getLogger(__name__).info(...)`` goes into the
# void. We install handlers lazily: if the process already has a root
# handler configured (i.e. imported by the parent Pulse backend for tests
# or by someone reusing the FastAPI ``app``), we don't overwrite it — a
# double setup would blow away the parent's ``pulse.log`` sink.
if not logging.getLogger().handlers:
    setup_logging(service_name="boss_mcp")
logger = logging.getLogger(__name__)


TRACE_HEADER = "X-Pulse-Trace-Id"


@dataclass(slots=True)
class _ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]


# Sync Playwright 需要稳定线程上下文，使用单线程执行器避免跨线程切换。
_TOOL_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="boss-mcp")


def _run_tool_handler_bound_to_trace(
    handler: Callable[[dict[str, Any]], Any],
    arguments: dict[str, Any],
    trace_id: str | None,
) -> Any:
    """Executor worker wrapper that rebinds ``trace_id`` inside the pool
    thread (ContextVars do not propagate automatically into executor
    threads). Keeps ``logger.info`` inside ``_boss_platform_runtime``
    tagged with the caller's trace — ADR-005 §2.
    """

    set_trace_id(trace_id)
    return handler(arguments)


def _build_tools() -> dict[str, _ToolSpec]:
    return {
        "health": _ToolSpec(
            name="health",
            description="Return boss platform MCP runtime health",
            schema={"type": "object", "properties": {}},
            handler=lambda args: runtime.health(),
        ),
        "reset_browser_session": _ToolSpec(
            name="reset_browser_session",
            description="Close and recreate browser session on next call",
            schema={
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                },
            },
            handler=lambda args: runtime.reset_browser_session(
                reason=str(args.get("reason") or "manual").strip() or "manual",
            ),
        ),
        "check_login": _ToolSpec(
            name="check_login",
            description="Validate BOSS login session",
            schema={
                "type": "object",
                "properties": {
                    "check_url": {"type": "string"},
                },
            },
            handler=lambda args: runtime.check_login(
                check_url=str(args.get("check_url") or "").strip(),
            ),
        ),
        "scan_jobs": _ToolSpec(
            name="scan_jobs",
            description=(
                "Scan jobs from BOSS sources. Streaming scroll on the live "
                "search sidebar (BOSS is an infinite-scroll SPA) until "
                "target_count is satisfied, evaluation_cap hits, or the "
                "list plateaus. Returned payload always carries `exhausted` "
                "so the host can decide whether to evolve keywords."
            ),
            schema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "target_count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "description": "Desired # of cards before early stop.",
                    },
                    "evaluation_cap": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "description": "Hard ceiling on collected cards.",
                    },
                    "scroll_plateau_rounds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 8,
                        "description": "# of consecutive empty scrolls before declaring exhausted.",
                    },
                    "max_items": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "description": "Deprecated alias for target_count.",
                    },
                    "max_pages": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 8,
                        "description": "Deprecated; pre-scroll back-compat budget hint.",
                    },
                    "job_type": {"type": "string"},
                    "city": {
                        "type": "string",
                        "description": (
                            "Optional city name (e.g. 杭州/上海). "
                            "Unknown names fall back to nationwide scan."
                        ),
                    },
                },
                "required": ["keyword"],
            },
            handler=lambda args: runtime.scan_jobs(
                keyword=str(args.get("keyword") or "").strip(),
                max_items=int(args["max_items"]) if args.get("max_items") is not None else None,
                max_pages=int(args["max_pages"]) if args.get("max_pages") is not None else None,
                target_count=int(args["target_count"]) if args.get("target_count") is not None else None,
                evaluation_cap=int(args["evaluation_cap"]) if args.get("evaluation_cap") is not None else None,
                scroll_plateau_rounds=(
                    int(args["scroll_plateau_rounds"])
                    if args.get("scroll_plateau_rounds") is not None else None
                ),
                job_type=str(args.get("job_type") or "all"),
                city=(str(args.get("city") or "").strip() or None),
            ),
        ),
        "job_detail": _ToolSpec(
            name="job_detail",
            description="Fetch compact job detail payload",
            schema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "source_url": {"type": "string"},
                },
            },
            handler=lambda args: runtime.job_detail(
                job_id=str(args.get("job_id") or "").strip(),
                source_url=str(args.get("source_url") or "").strip(),
            ),
        ),
        "greet_job": _ToolSpec(
            name="greet_job",
            description="Trigger greet action (audit-first)",
            schema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "job_id": {"type": "string"},
                    "source_url": {"type": "string"},
                    "job_title": {"type": "string"},
                    "company": {"type": "string"},
                    "greeting_text": {"type": "string"},
                },
            },
            handler=lambda args: runtime.greet_job(
                run_id=str(args.get("run_id") or "").strip(),
                job_id=str(args.get("job_id") or "").strip(),
                source_url=str(args.get("source_url") or "").strip(),
                job_title=str(args.get("job_title") or "").strip(),
                company=str(args.get("company") or "").strip(),
                greeting_text=str(args.get("greeting_text") or "").strip(),
            ),
        ),
        "pull_conversations": _ToolSpec(
            name="pull_conversations",
            description="Pull conversation list",
            schema={
                "type": "object",
                "properties": {
                    "max_conversations": {"type": "integer", "minimum": 1, "maximum": 200},
                    "unread_only": {"type": "boolean"},
                    "fetch_latest_hr": {"type": "boolean"},
                    "chat_tab": {"type": "string"},
                },
            },
            handler=lambda args: runtime.pull_conversations(
                max_conversations=int(args.get("max_conversations") or 20),
                unread_only=bool(args.get("unread_only", False)),
                fetch_latest_hr=bool(args.get("fetch_latest_hr", True)),
                chat_tab=str(args.get("chat_tab") or "全部"),
            ),
        ),
        "reply_conversation": _ToolSpec(
            name="reply_conversation",
            description="Reply to one conversation",
            schema={
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string"},
                    "reply_text": {"type": "string"},
                    "profile_id": {"type": "string"},
                    "conversation_hint": {"type": "object"},
                },
                "required": ["conversation_id", "reply_text"],
            },
            handler=lambda args: runtime.reply_conversation(
                conversation_id=str(args.get("conversation_id") or "").strip(),
                reply_text=str(args.get("reply_text") or "").strip(),
                profile_id=str(args.get("profile_id") or "default").strip() or "default",
                conversation_hint=dict(args.get("conversation_hint") or {})
                if isinstance(args.get("conversation_hint"), dict)
                else {},
            ),
        ),
        "send_resume_attachment": _ToolSpec(
            name="send_resume_attachment",
            description="Send the user's resume as a file attachment to the conversation",
            schema={
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string"},
                    "resume_profile_id": {"type": "string"},
                    "conversation_hint": {"type": "object"},
                },
                "required": ["conversation_id"],
            },
            handler=lambda args: runtime.send_resume_attachment(
                conversation_id=str(args.get("conversation_id") or "").strip(),
                resume_profile_id=str(args.get("resume_profile_id") or "default").strip() or "default",
                conversation_hint=dict(args.get("conversation_hint") or {})
                if isinstance(args.get("conversation_hint"), dict)
                else {},
            ),
        ),
        "click_conversation_card": _ToolSpec(
            name="click_conversation_card",
            description="Click an interactive card (exchange-resume / interview-invite / ...)",
            schema={
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string"},
                    "card_id": {"type": "string"},
                    "card_type": {
                        "type": "string",
                        "enum": [
                            "exchange_resume",
                            "exchange_contact",
                            "interview_invite",
                            "job_recommend",
                        ],
                    },
                    "action": {
                        "type": "string",
                        "enum": ["accept", "reject", "view"],
                    },
                },
                "required": ["conversation_id", "card_type", "action"],
            },
            handler=lambda args: runtime.click_conversation_card(
                conversation_id=str(args.get("conversation_id") or "").strip(),
                card_id=str(args.get("card_id") or "").strip(),
                card_type=str(args.get("card_type") or "").strip(),
                action=str(args.get("action") or "").strip(),
            ),
        ),
        "mark_processed": _ToolSpec(
            name="mark_processed",
            description="Mark one conversation as processed",
            schema={
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string"},
                    "run_id": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["conversation_id"],
            },
            handler=lambda args: runtime.mark_processed(
                conversation_id=str(args.get("conversation_id") or "").strip(),
                run_id=str(args.get("run_id") or "").strip(),
                note=str(args.get("note") or "").strip(),
            ),
        ),
    }


def create_app() -> FastAPI:
    app = FastAPI(title="Pulse Boss MCP Gateway", version="0.1.0")
    tools = _build_tools()

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return runtime.health()

    @app.get("/tools")
    async def list_tools() -> dict[str, Any]:
        return {
            "tools": [
                {
                    "server": "boss",
                    "name": spec.name,
                    "description": spec.description,
                    "schema": spec.schema,
                }
                for spec in tools.values()
            ]
        }

    @app.post("/call")
    async def call_tool(
        payload: dict[str, Any] = Body(default_factory=dict),
        x_pulse_trace_id: str | None = Header(default=None, alias=TRACE_HEADER),
    ) -> dict[str, Any]:
        # ADR-005 §2: parent process (pulse backend) forwards the current
        # ``trace_id`` in ``X-Pulse-Trace-Id``; bind it to this request's
        # async context so every ``logger.info`` under this call — including
        # those inside ``_boss_platform_runtime`` running in the tool
        # executor — lands in ``logs/traces/<tid>/boss_mcp.log``.
        set_trace_id(x_pulse_trace_id)

        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        spec = tools.get(name)
        if spec is None:
            logger.info("mcp.call.not_found tool=%s", name)
            raise HTTPException(status_code=404, detail=f"tool not found: {name}")
        arguments = payload.get("arguments")
        safe_arguments = dict(arguments) if isinstance(arguments, dict) else {}
        logger.info(
            "mcp.call.start tool=%s args_keys=%s",
            name,
            sorted(safe_arguments.keys()),
        )
        started = time.monotonic()
        status = "ok"
        try:
            loop = asyncio.get_running_loop()
            trace_for_worker = x_pulse_trace_id
            result = await loop.run_in_executor(
                _TOOL_EXECUTOR,
                _run_tool_handler_bound_to_trace,
                spec.handler,
                safe_arguments,
                trace_for_worker,
            )
        except HTTPException:
            status = "http_error"
            raise
        except Exception as exc:
            status = "error"
            logger.exception("mcp.call.error tool=%s", name)
            raise HTTPException(status_code=500, detail=str(exc)[:300]) from exc
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.info(
                "mcp.call.end tool=%s status=%s elapsed_ms=%d",
                name,
                status,
                elapsed_ms,
            )
        return {"ok": True, "result": result}

    return app


app = create_app()


if __name__ == "__main__":
    host = "127.0.0.1"
    port = int(__import__("os").getenv("PULSE_BOSS_MCP_GATEWAY_PORT", "8811"))
    # access_log=False: uvicorn's generic "GET /health 200" line is noise.
    # /health fires every 20s from start.sh monitor_loop. The real business
    # signal (``mcp.call.start|end ...``) is emitted by /call itself above.
    # log_config=None so uvicorn does not replace the logging config we
    # installed at module import (setup_logging("boss_mcp")).
    uvicorn.run(app, host=host, port=port, access_log=False, log_config=None)
