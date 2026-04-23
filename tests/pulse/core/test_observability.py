"""End-to-end observability contract tests (ADR-005).

Three hard contracts we must never regress:

  1. Every user turn bound to one ``trace_id`` produces a per-trace
     directory ``logs/traces/<trace_id>/`` with at minimum a ``pulse.log``
     and a ``meta.json`` after ``channel.message.completed``.
  2. ``pulse.log`` under the trace bucket contains the full backbone:
     ``channel.msg.received`` → stage events from the dispatched module →
     ``channel.msg.completed``. If any link is missing the "log system"
     is broken by definition.
  3. The ``meta.json`` carries the user text, trace id, used tools and
     latency so a post-mortem does not need to diff .log files.

These tests run real ``setup_logging`` with a tmp ``PULSE_LOG_DIR``; we
explicitly avoid mocking any handler — the whole point is to exercise
the on-disk pipeline the operator will grep in production.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("postgres_test_db")


def _reset_and_setup(tmp_path: Path, service: str = "pulse") -> None:
    # Drop any handlers leaked by prior tests; setup_logging itself purges
    # root handlers, but belt-and-braces in case someone registered one.
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    from pulse.core.logging_config import setup_logging

    setup_logging(service_name=service)


@pytest.fixture
def logdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_root = tmp_path / "logs"
    monkeypatch.setenv("PULSE_LOG_DIR", str(log_root))
    _reset_and_setup(tmp_path)
    return log_root


def test_user_turn_creates_per_trace_bucket_with_meta(logdir: Path) -> None:
    """A channel ingest must drop logs/traces/<tid>/pulse.log + meta.json."""
    from pulse.core.server import create_app

    app = create_app()
    client = TestClient(app)

    response = client.post(
        "/api/channel/cli/ingest",
        json={
            "text": "ping",
            "user_id": "observability-probe",
            "metadata": {"source": "test_observability"},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    trace_id = payload["result"].get("trace_id")
    assert trace_id and trace_id.startswith("trace_"), (
        f"backend must expose trace_id in response, got {payload['result']!r}"
    )

    # ── Contract 1: per-trace directory exists ──
    trace_dir = logdir / "traces" / trace_id
    assert trace_dir.is_dir(), (
        f"ADR-005 §2 broken: no per-trace bucket at {trace_dir}. "
        f"Dir tree under logdir: {sorted(p.name for p in logdir.rglob('*'))}"
    )

    # ── Contract 1 cont.: pulse.log exists in the bucket ──
    bucket_log = trace_dir / "pulse.log"
    assert bucket_log.is_file(), f"no pulse.log in {trace_dir}"
    text = bucket_log.read_text(encoding="utf-8")

    # ── Contract 2: backbone links ──
    assert "channel.msg.received" in text, (
        f"missing channel.msg.received in {bucket_log}: {text!r}"
    )
    assert "channel.msg.completed" in text, (
        f"missing channel.msg.completed in {bucket_log}: {text!r}"
    )
    # Every line must carry the bound trace_id (no "trace=-" leak in the
    # per-trace bucket — that would mean propagation is broken).
    for line in text.splitlines():
        if not line.strip():
            continue
        assert f"trace={trace_id}" in line, (
            f"leaked trace=- line in {bucket_log}: {line!r}"
        )

    # ── Contract 3: meta.json summary ──
    meta_path = trace_dir / "meta.json"
    assert meta_path.is_file(), f"no meta.json in {trace_dir}"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["trace_id"] == trace_id
    assert meta["user_text"] == "ping"
    assert meta["channel"] == "cli"
    assert meta["user_id"] == "observability-probe"
    assert isinstance(meta["used_tools"], list)
    assert meta["handled"] is True
    # latency must be non-negative int (observability regression bait).
    assert isinstance(meta["latency_ms"], int) and meta["latency_ms"] >= 0


def test_two_concurrent_turns_land_in_separate_buckets(logdir: Path) -> None:
    """Two user turns must NOT share a per-trace directory.

    This is the whole reason the user demanded per-request bucketing: if
    traces mix, the log system is worthless for post-mortem.
    """
    from pulse.core.server import create_app

    app = create_app()
    client = TestClient(app)

    r1 = client.post(
        "/api/channel/cli/ingest",
        json={"text": "ping", "user_id": "u1"},
    )
    r2 = client.post(
        "/api/channel/cli/ingest",
        json={"text": "ping", "user_id": "u2"},
    )
    tid1 = r1.json()["result"]["trace_id"]
    tid2 = r2.json()["result"]["trace_id"]
    assert tid1 != tid2, "two turns must get distinct trace_ids"

    bucket1 = (logdir / "traces" / tid1 / "pulse.log").read_text(encoding="utf-8")
    bucket2 = (logdir / "traces" / tid2 / "pulse.log").read_text(encoding="utf-8")
    # Cross-contamination detector: bucket1 must not contain tid2's trace.
    assert tid2 not in bucket1, f"trace bleed: {tid2} showed up in {tid1}'s bucket"
    assert tid1 not in bucket2, f"trace bleed: {tid1} showed up in {tid2}'s bucket"


def test_stage_events_are_mirrored_to_logs(logdir: Path) -> None:
    """``emit_stage_event`` must also write a logger line so post-mortem
    does not require an SSE subscriber. ADR-005 §4.
    """
    from fastapi import APIRouter

    from pulse.core.module import BaseModule

    class _ProbeModule(BaseModule):
        name = "obs_probe"

        def register_routes(self, router: APIRouter) -> None:  # pragma: no cover
            return None

    module = _ProbeModule()
    tid = module.emit_stage_event(
        stage="probe",
        status="started",
        trace_id="trace_probe_stage",
        payload={"max": 1},
    )
    assert tid == "trace_probe_stage"

    # Force handlers to flush to disk before assertion.
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass

    bucket = logdir / "traces" / "trace_probe_stage" / "pulse.log"
    assert bucket.is_file(), f"no bucket for stage event: {bucket}"
    text = bucket.read_text(encoding="utf-8")
    assert "stage module=obs_probe stage=probe status=started" in text, text


# ---------------------------------------------------------------------------
# Patrol → MCP trace propagation (ADR-005 §2, 2026-04-22 post-mortem)
#
# Contract: `AgentRuntime._execute_patrol` MUST bind ``ctx.trace_id`` into
# the ContextVar so that every downstream reader — logger filter, stage
# events, and most importantly ``mcp_transport_http._open_response`` —
# sees the same id. Without this binding the ``X-Pulse-Trace-Id`` header
# silently drops, and all boss_mcp logs land in the ``trace=-`` black hole
# (which is exactly what 2026-04-22's real run exposed).
#
# We test the three invariants that together kill the regression class:
#   A) handler observes the same trace_id via both ctx and get_trace_id()
#   B) transport._open_response injects that trace_id as HTTP header
#   C) the binding resets after the patrol turn (thread-pool safety)
# ---------------------------------------------------------------------------


def test_patrol_binds_trace_id_to_contextvar_during_handler() -> None:
    """A) handler's ``ctx.trace_id`` must equal ``get_trace_id()``.

    This is what makes every ``logger.info(..., trace_id=implicit)`` line
    in connector / runtime / module code tag with the right trace.
    """
    from pulse.core.logging_config import get_trace_id, set_trace_id
    from pulse.core.runtime import AgentRuntime

    set_trace_id(None)  # clean slate — prior tests may leak ContextVar state
    rt = AgentRuntime()
    observed: dict[str, str] = {}

    def handler(ctx):  # type: ignore[no-untyped-def]
        observed["ctx_trace"] = ctx.trace_id
        observed["ctxvar_trace"] = get_trace_id()

    rt._execute_patrol("test.trace_bind", handler)

    assert observed["ctx_trace"].startswith("trace_"), observed
    assert observed["ctx_trace"] == observed["ctxvar_trace"], (
        "patrol did not bind ctx.trace_id into ContextVar — "
        "mcp_transport_http will drop trace header. "
        f"ctx={observed['ctx_trace']!r} ctxvar={observed['ctxvar_trace']!r}"
    )
    # C) reset: next call on the same thread must not leak old trace.
    assert get_trace_id() == "-", (
        "patrol turn finished but ContextVar still holds "
        f"{get_trace_id()!r}; scheduler thread pool reuse will leak trace."
    )


def test_patrol_trace_resets_even_when_handler_raises() -> None:
    """C) exception path also resets — otherwise one bad patrol poisons the
    whole scheduler worker thread for subsequent unrelated tasks."""
    from pulse.core.logging_config import get_trace_id, set_trace_id
    from pulse.core.runtime import AgentRuntime

    set_trace_id(None)
    rt = AgentRuntime()

    def bad(ctx):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated handler failure")

    rt._execute_patrol("test.trace_reset_on_error", bad)
    assert get_trace_id() == "-", (
        "handler raised and runtime recovered, but ContextVar still holds "
        f"{get_trace_id()!r}. finally-reset contract broken."
    )


def test_patrol_trace_reaches_mcp_http_transport_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B) inside a patrol, ``HttpMCPTransport._open_response`` must inject
    ``X-Pulse-Trace-Id: <patrol_trace>`` on its HTTP request.

    We intercept ``urllib.request.urlopen`` (the *one* environment-boundary
    mock allowed by the testing constitution) to capture the actual
    ``Request`` headers, then assert the header matches the patrol trace.
    Everything above ``urlopen`` — transport init, context binding, header
    assembly — runs for real.
    """
    import io
    import urllib.request

    from pulse.core.logging_config import get_trace_id, set_trace_id
    from pulse.core.mcp_transport_http import TRACE_HEADER, HttpMCPTransport
    from pulse.core.runtime import AgentRuntime

    set_trace_id(None)

    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
        # Snapshot whatever state the caller composed into the Request.
        captured["headers"] = {k: v for k, v in request.header_items()}
        captured["url"] = request.full_url
        captured["trace_at_call"] = get_trace_id()
        # Minimal JSON response that ``_custom_call_tool`` will accept.
        body = b'{"result": {"ok": true}}'
        resp = io.BytesIO(body)
        resp.headers = {"Content-Type": "application/json"}  # type: ignore[attr-defined]
        resp.close = lambda: None  # type: ignore[method-assign]
        return resp

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    rt = AgentRuntime()

    def handler(ctx):  # type: ignore[no-untyped-def]
        transport = HttpMCPTransport(
            base_url="http://127.0.0.1:65535",  # never actually dialed
            transport_mode="custom_http",
        )
        # Skip MCP initialize — fake transport directly into custom_http mode.
        transport._active_mode = "custom_http"
        transport._custom_call_tool(
            server="boss",
            name="pull_conversations",
            arguments={"chat_tab": "未读"},
        )

    rt._execute_patrol("test.trace_header_inject", handler)

    assert "headers" in captured, "urlopen was never called — test setup broken"
    # urllib.request stores headers title-cased; normalize for lookup.
    normalized = {k.lower(): v for k, v in captured["headers"].items()}
    actual = normalized.get(TRACE_HEADER.lower())
    assert actual is not None, (
        f"HTTP request missing {TRACE_HEADER}. "
        f"headers={captured['headers']!r}. "
        "Patrol-to-MCP trace propagation is broken — boss_mcp.log will "
        "drop into the trace=- black hole."
    )
    assert actual.startswith("trace_"), (
        f"{TRACE_HEADER}={actual!r} does not look like a Pulse trace id"
    )
    assert actual == captured["trace_at_call"], (
        "trace_id observed inside urlopen disagrees with the header value — "
        "indicates ContextVar raced with Request construction."
    )
