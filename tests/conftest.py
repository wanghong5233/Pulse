from __future__ import annotations

import os
from pathlib import Path

import pytest

try:
    import psycopg
except Exception:  # pragma: no cover - environment dependent
    psycopg = None  # type: ignore[assignment]


DEFAULT_TEST_DATABASE_URL = "postgresql://postgres:pulse@localhost:15433/pulse"


def _database_available(database_url: str) -> bool:
    if psycopg is None:
        return False
    try:
        conn = psycopg.connect(database_url, connect_timeout=1)
    except Exception:
        return False
    try:
        return True
    finally:
        conn.close()


@pytest.fixture
def postgres_test_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    database_url = os.getenv("PULSE_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL).strip() or DEFAULT_TEST_DATABASE_URL
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("PULSE_DATABASE_URL", database_url)
    monkeypatch.delenv("PULSE_BOSS_PROVIDER", raising=False)
    monkeypatch.delenv("PULSE_BOSS_MCP_BASE_URL", raising=False)
    monkeypatch.delenv("PULSE_BOSS_OPENAPI_BASE_URL", raising=False)
    monkeypatch.setenv("PULSE_CORE_MEMORY_PATH", str(tmp_path / "core_memory.json"))
    monkeypatch.setenv("PULSE_GOVERNANCE_AUDIT_PATH", str(tmp_path / "governance_audit.json"))
    monkeypatch.setenv("PULSE_GOVERNANCE_RULES_VERSIONS_PATH", str(tmp_path / "governance_rules_versions.json"))
    monkeypatch.setenv("PULSE_DPO_PAIRS_PATH", str(tmp_path / "dpo_pairs.jsonl"))
    monkeypatch.setenv("PULSE_GENERATED_SKILLS_DIR", str(tmp_path / "generated_skills"))
    monkeypatch.setenv("PULSE_BOSS_CHAT_INBOX_PATH", str(tmp_path / "boss_chat_inbox.jsonl"))
    monkeypatch.setenv("PULSE_BOSS_ALLOW_SEED_FALLBACK", "false")
    monkeypatch.setenv("PULSE_BOSS_ALLOW_LOCAL_INBOX_FALLBACK", "false")
    monkeypatch.setenv("PULSE_BOSS_MCP_SCAN_MODE", "browser_only")
    monkeypatch.setenv("PULSE_BOSS_MCP_PULL_MODE", "browser_only")
    # Test runtime must not open real WeCom long-connections.
    monkeypatch.delenv("WECHAT_WORK_BOT_ID", raising=False)
    monkeypatch.delenv("WECHAT_WORK_BOT_SECRET", raising=False)
    monkeypatch.delenv("WECHAT_WORK_CORP_ID", raising=False)
    monkeypatch.delenv("WECHAT_WORK_AGENT_ID", raising=False)
    monkeypatch.delenv("WECHAT_WORK_SECRET", raising=False)
    monkeypatch.delenv("WECHAT_WORK_TOKEN", raising=False)
    monkeypatch.delenv("WECHAT_WORK_ENCODING_AES_KEY", raising=False)
    if not _database_available(database_url):
        pytest.skip(f"requires PostgreSQL test database: {database_url}")
    return database_url
