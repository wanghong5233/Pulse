from __future__ import annotations

import pytest

from pulse.modules.job._connectors.boss.connector import (
    _RetryableConnectorError,
    build_boss_platform_connector,
)
from pulse.modules.job._connectors.boss.settings import get_boss_connector_settings


def _build_connector():
    get_boss_connector_settings.cache_clear()
    return build_boss_platform_connector()


def test_boss_connector_prefers_mcp_when_both_configured(monkeypatch) -> None:
    monkeypatch.setenv("PULSE_BOSS_MCP_BASE_URL", "http://127.0.0.1:8811")
    monkeypatch.setenv("PULSE_BOSS_OPENAPI_BASE_URL", "http://127.0.0.1:8899")
    monkeypatch.delenv("PULSE_BOSS_PROVIDER", raising=False)

    connector = _build_connector()
    assert connector.provider_name == "boss_mcp"
    assert connector.execution_ready is True


def test_boss_connector_honors_explicit_openapi(monkeypatch) -> None:
    monkeypatch.setenv("PULSE_BOSS_MCP_BASE_URL", "http://127.0.0.1:8811")
    monkeypatch.setenv("PULSE_BOSS_OPENAPI_BASE_URL", "http://127.0.0.1:8899")
    monkeypatch.setenv("PULSE_BOSS_PROVIDER", "openapi")

    connector = _build_connector()
    assert connector.provider_name == "boss_openapi"
    assert connector.execution_ready is True


def test_boss_connector_stays_unconfigured_without_real_connector(monkeypatch) -> None:
    monkeypatch.setenv("PULSE_BOSS_MCP_BASE_URL", "")
    monkeypatch.setenv("PULSE_BOSS_OPENAPI_BASE_URL", "")
    monkeypatch.delenv("PULSE_BOSS_PROVIDER", raising=False)
    monkeypatch.setenv("PULSE_BOSS_ALLOW_SEED_FALLBACK", "false")

    connector = _build_connector()
    assert connector.provider_name == "boss_unconfigured"
    assert connector.execution_ready is False
    login = connector.check_login()
    assert login["ok"] is False
    assert login["status"] == "provider_unavailable"


def test_boss_connector_allows_explicit_web_search_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("PULSE_BOSS_MCP_BASE_URL", "")
    monkeypatch.setenv("PULSE_BOSS_OPENAPI_BASE_URL", "")
    monkeypatch.setenv("PULSE_BOSS_PROVIDER", "web_search")
    monkeypatch.setenv("PULSE_BOSS_ALLOW_WEB_SEARCH_FALLBACK", "true")
    monkeypatch.setenv("PULSE_BOSS_ALLOW_SEED_FALLBACK", "true")

    connector = _build_connector()
    assert connector.provider_name == "boss_web_search"
    assert connector.execution_ready is False


def test_boss_connector_blocks_web_search_without_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("PULSE_BOSS_MCP_BASE_URL", "")
    monkeypatch.setenv("PULSE_BOSS_OPENAPI_BASE_URL", "")
    monkeypatch.setenv("PULSE_BOSS_PROVIDER", "web_search")
    monkeypatch.delenv("PULSE_BOSS_ALLOW_WEB_SEARCH_FALLBACK", raising=False)

    connector = _build_connector()
    assert connector.provider_name == "boss_unconfigured"
    assert connector.execution_ready is False
    assert "blocked" in str(connector.health().get("degraded_reason") or "")


# ---------------------------------------------------------------------------
# P1-A regression guard (see ADR / audit trace_f3bda835ed94):
# MCP scan with fetch_detail=True on top-K needs well over 10s to walk BOSS
# detail pages; a 10 s client-side timeout caused 3x retry storms and wasted
# browser runs. The settings floor must stay high enough to cover the worst
# realistic case, while still being overridable via env.
# ---------------------------------------------------------------------------


def test_boss_mcp_timeout_default_accommodates_detail_fetch(monkeypatch) -> None:
    monkeypatch.delenv("PULSE_BOSS_MCP_TIMEOUT_SEC", raising=False)
    get_boss_connector_settings.cache_clear()
    settings = get_boss_connector_settings()
    # Floor chosen to cover both scan_jobs(fetch_detail=True) 25-40s AND
    # greet_job / reply_conversation browser executor 35-70s (see audit
    # trace_a9bbc29a245c where the old 45s ceiling let the HTTP client
    # disconnect mid-click). Any regression below 60s will re-open the
    # silent-success window and MUST fail fast.
    assert settings.mcp.timeout_sec >= 60.0, (
        f"PULSE_BOSS_MCP timeout default {settings.mcp.timeout_sec}s is too tight; "
        "browser-mode greet/reply routinely run 35-70s, keep default >= 60s"
    )
    assert settings.openapi.timeout_sec >= 60.0, (
        f"PULSE_BOSS_OPENAPI timeout default {settings.openapi.timeout_sec}s is too tight"
    )


def test_boss_mcp_timeout_env_override_honored(monkeypatch) -> None:
    monkeypatch.setenv("PULSE_BOSS_MCP_TIMEOUT_SEC", "120")
    monkeypatch.setenv("PULSE_BOSS_OPENAPI_TIMEOUT_SEC", "90")
    get_boss_connector_settings.cache_clear()
    settings = get_boss_connector_settings()
    assert settings.mcp.timeout_sec == 120.0
    assert settings.openapi.timeout_sec == 90.0


# ---------------------------------------------------------------------------
# P3e regression guard (audit trace_a9bbc29a245c): MUTATING operations (those
# that trigger a real platform-side side-effect per call — send a greeting,
# post a reply, upload a resume) MUST NOT be HTTP-retried.  Retrying after a
# client-side timeout while the server still drives the browser click to
# completion produces duplicate outbound messages; the audit log showed
# 4 × (sent=True) rows for one imperative turn while the connector believed
# it had received 3 × "timed out".  The retry whitelist is enforced in
# ``BossPlatformConnector._effective_retry_count``; READ/idempotent ops
# continue to use the configured retry_count.
# ---------------------------------------------------------------------------


def _build_mcp_connector_with_retry(monkeypatch, retry_count: int = 3):
    monkeypatch.setenv("PULSE_BOSS_MCP_BASE_URL", "http://127.0.0.1:8811")
    monkeypatch.setenv("PULSE_BOSS_OPENAPI_BASE_URL", "")
    monkeypatch.delenv("PULSE_BOSS_PROVIDER", raising=False)
    monkeypatch.setenv("PULSE_BOSS_RETRY_COUNT", str(retry_count))
    monkeypatch.setenv("PULSE_BOSS_RETRY_BACKOFF_SEC", "0")
    get_boss_connector_settings.cache_clear()
    connector = build_boss_platform_connector()
    return connector


@pytest.mark.parametrize(
    "op_name",
    [
        "greet_job",
        "reply_conversation",
        "send_resume_attachment",
    ],
)
def test_mutating_operations_skip_retry(monkeypatch, op_name: str) -> None:
    """MUTATING ops must execute exactly once even with retry_count=3."""
    connector = _build_mcp_connector_with_retry(monkeypatch, retry_count=3)
    assert connector._settings.retry_count == 3
    assert connector._effective_retry_count(op_name) == 0
    calls: list[int] = []

    def _raise_retryable() -> None:
        calls.append(1)
        raise _RetryableConnectorError("mcp call failed: timed out")

    call_result = connector._invoke(op_name, {"job_id": "j1"}, _raise_retryable)
    assert call_result.ok is False
    assert call_result.attempts == 1, (
        f"{op_name} attempted {call_result.attempts} times; MUTATING retry must be 0"
    )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "op_name",
    [
        "scan_jobs",
        "job_detail",
        "pull_conversations",
        "mark_processed",
        "check_login",
    ],
)
def test_read_operations_still_retry(monkeypatch, op_name: str) -> None:
    """READ/idempotent ops continue to honour the configured retry_count."""
    connector = _build_mcp_connector_with_retry(monkeypatch, retry_count=3)
    assert connector._effective_retry_count(op_name) == 3
    calls: list[int] = []

    def _raise_retryable() -> None:
        calls.append(1)
        raise _RetryableConnectorError("mcp call failed: connection reset")

    call_result = connector._invoke(op_name, {}, _raise_retryable)
    assert call_result.ok is False
    assert call_result.attempts == 4, (
        f"{op_name} attempted {call_result.attempts} times; expected 1 + 3 retries"
    )
    assert len(calls) == 4
