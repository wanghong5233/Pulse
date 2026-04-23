"""BOSS 平台连接器的所有配置.

自载 ``PULSE_BOSS_*`` 环境变量,**不**走内核 ``Settings``, 与业务层、内核彻底解耦:

  * 内核(`core/config.py`)只关心 host/port/brain/memory/...
  * Job 业务策略(`modules/job/config.py`)只关心 batch / 阈值 / HITL / patrol
  * 本文件只关心 BOSS 这一家平台的驱动细节(provider/OpenAPI/MCP/cookie/retry)

通过 ``BaseSettings`` 子类独立加载,多个 Settings 互不干扰.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_repo_path(raw: str, *, default: Path) -> Path:
    """Resolve ``raw`` as either an absolute path or a repo-relative one.

    This module lives under ``Pulse/src/pulse/modules/job/_connectors/boss/``
    so going 6 parents up lands at the repository root (``Pulse/``).
    """
    value = (raw or "").strip()
    if not value:
        return default
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    repo_root = Path(__file__).resolve().parents[6]
    return (repo_root / candidate).resolve()


class BossOpenApiSettings(BaseSettings):
    """Route templates for the OpenAPI mode."""

    base_url: str = Field(default="")
    token: str = Field(default="")
    # Default 90 s (upper cap 180 s). Rationale: the OpenAPI tier mirrors the
    # MCP tier when BOSS routes its endpoint through a browser executor on the
    # server side, and we have empirical audit rows showing single
    # greet/reply calls costing 35-70 s wall-clock end-to-end (page.goto →
    # wait_for_selector → click → domcontentloaded → send click → idle).
    # A 45 s ceiling was hit in audit trace_a9bbc29a245c where the HTTP
    # client disconnected mid-flight while the server kept the browser click
    # running, producing silent-success (status=sent) the caller never saw.
    # See ADR-001 §6 P3e.
    timeout_sec: float = Field(default=90.0, ge=1.0, le=180.0)
    auth_status_path: str = Field(default="/auth/status")
    scan_path: str = Field(default="/jobs/scan")
    detail_path: str = Field(default="/jobs/detail")
    greet_path: str = Field(default="/jobs/greet")
    pull_path: str = Field(default="/chats/pull")
    reply_path: str = Field(default="/chats/reply")
    mark_path: str = Field(default="/chats/mark_processed")

    model_config = SettingsConfigDict(
        env_prefix="PULSE_BOSS_OPENAPI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class BossMcpSettings(BaseSettings):
    """Tool names for the MCP mode (and the MCP gateway endpoint)."""

    base_url: str = Field(default="")
    token: str = Field(default="")
    # Default 90 s (upper cap 180 s). Two audited workloads set the floor:
    #  (a) scan_jobs with ``fetch_detail=True`` on ~30 JDs: 25-40 s
    #      (trace_f3bda835ed94).
    #  (b) greet_job / reply_conversation browser executor: 35-70 s per call
    #      because each step (page.goto, wait greet_selector, click,
    #      wait_for_load_state, wait input_selector, fill, send) has its own
    #      20 s step budget on the MCP side (trace_a9bbc29a245c).
    # The previous 45 s default disconnected the HTTP client mid-flight while
    # the server's browser kept clicking, producing silent successes
    # (status=sent in boss_mcp_actions.jsonl) that the caller could not see.
    # Combined with the MUTATING retry whitelist in connector.py, 90 s keeps
    # one attempt under the real p95 without cascading re-sends.
    # See ADR-001 §6 P3e.
    timeout_sec: float = Field(default=90.0, ge=1.0, le=180.0)
    server: str = Field(default="boss")
    scan_tool: str = Field(default="scan_jobs")
    detail_tool: str = Field(default="job_detail")
    greet_tool: str = Field(default="greet_job")
    pull_tool: str = Field(default="pull_conversations")
    reply_tool: str = Field(default="reply_conversation")
    mark_tool: str = Field(default="mark_processed")
    check_login_tool: str = Field(default="check_login")
    send_attachment_tool: str = Field(default="send_resume_attachment")
    click_card_tool: str = Field(default="click_conversation_card")

    model_config = SettingsConfigDict(
        env_prefix="PULSE_BOSS_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class BossConnectorSettings(BaseSettings):
    """Top-level BOSS connector config bag.

    ``PULSE_BOSS_*`` for scalar knobs; nested ``openapi``/``mcp`` groups are
    loaded by their own ``BaseSettings`` subclasses via ``default_factory``
    so the precedence ordering (env > .env > defaults) is preserved per
    group. Immutable at runtime (``frozen=True``) so business code cannot
    mutate a value mid-flight.
    """

    # provider 选择: openapi / mcp / web_search, 留空自动判别
    # 注意: web_search 仅用于诊断/演示, 默认禁用; 需要显式 opt-in.
    provider: str = Field(default="")

    openapi: BossOpenApiSettings = Field(default_factory=BossOpenApiSettings)
    mcp: BossMcpSettings = Field(default_factory=BossMcpSettings)

    retry_count: int = Field(default=2, ge=0, le=10)
    retry_backoff_sec: float = Field(default=0.8, ge=0.0, le=10.0)
    rate_limit_sec: float = Field(default=1.2, ge=0.0, le=30.0)

    cookie_path_raw: str = Field(default="", alias="PULSE_BOSS_COOKIE_PATH")
    connector_audit_path_raw: str = Field(
        default="./data/exports/audit/boss_connector_audit.jsonl",
        alias="PULSE_BOSS_CONNECTOR_AUDIT_PATH",
    )
    allow_web_search_fallback: bool = Field(default=False)
    allow_seed_fallback: bool = Field(default=False)

    model_config = SettingsConfigDict(
        env_prefix="PULSE_BOSS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        frozen=True,
    )

    # ---------------------------------------------------------------- derived

    @property
    def provider_override(self) -> str:
        return self.provider.strip().lower()

    @property
    def cookie_path(self) -> Path | None:
        if not self.cookie_path_raw.strip():
            return None
        return _resolve_repo_path(
            self.cookie_path_raw,
            default=Path.home() / ".pulse" / "boss.cookies.json",
        )

    @property
    def audit_path(self) -> Path:
        return _resolve_repo_path(
            self.connector_audit_path_raw,
            default=Path.home() / ".pulse" / "boss_connector_audit.jsonl",
        )


@lru_cache
def get_boss_connector_settings() -> BossConnectorSettings:
    return BossConnectorSettings()


__all__ = [
    "BossConnectorSettings",
    "BossMcpSettings",
    "BossOpenApiSettings",
    "get_boss_connector_settings",
]
