"""Pulse 内核配置.

此文件只承载 **内核(Kernel)和接入层(IM)** 的配置. 业务域(Job/Intel/Email)
的配置各自拥有独立的 ``BaseSettings`` 子类:

  * ``pulse.modules.job.config.JobSettings``              — Job 业务策略
  * ``pulse.modules.job._connectors.boss.settings.BossConnectorSettings``
                                                         — Boss 平台连接器

每个 Settings 子类通过自己的 ``env_prefix`` 独立加载 ``.env``, 互不干扰.
这样新增一个业务域或新增一个平台驱动, 都不需要修改本文件.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── 服务进程 ──
    app_name: str = Field(default="Pulse")
    environment: str = Field(default="dev")
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8010, ge=1, le=65535)
    reload: bool = Field(default=False)

    # ── 规则 / 策略 ──
    router_rules_path: str = Field(default="config/router_rules.json")
    policy_rules_path: str = Field(default="config/policy_rules.json")
    policy_blocked_keywords: str = Field(default="")
    policy_confirm_keywords: str = Field(default="")

    # ── Brain ──
    brain_max_steps: int = Field(default=20, ge=1, le=20)
    brain_daily_budget_usd: float = Field(default=2.0, ge=0.0)
    brain_prefer_llm: bool = Field(default=True)

    # ── Memory Runtime ──
    core_memory_path: str = Field(default="~/.pulse/core_memory.json")
    memory_recent_limit: int = Field(default=8, ge=1, le=50)

    # ── Governance / Evolution ──
    governance_audit_path: str = Field(default="~/.pulse/governance_audit.json")
    governance_rules_versions_path: str = Field(default="~/.pulse/governance_rules_versions.json")
    evolution_rules_path: str = Field(default="config/evolution_rules.json")
    evolution_default_mode: str = Field(default="autonomous")
    evolution_prefs_mode: str = Field(default="autonomous")
    evolution_soul_mode: str = Field(default="supervised")
    evolution_belief_mode: str = Field(default="autonomous")
    dpo_pairs_path: str = Field(default="~/.pulse/dpo_pairs.jsonl")
    dpo_auto_collect: bool = Field(default=True)
    soul_config_path: str = Field(default="config/soul.yaml")

    # ── Skill Generator / Event Store ──
    generated_skills_dir: str = Field(default="generated/skills")
    event_store_max_events: int = Field(default=2000, ge=100, le=20000)
    # Observability Plane: append-only 审计事件落盘目录(llm.*/tool.*/memory.*/policy.*)
    event_audit_dir: str = Field(default="./data/exports/events")

    # ── MCP 客户端(内核 Tool 调用通道) ──
    mcp_http_base_url: str = Field(default="")
    mcp_http_timeout_sec: float = Field(default=8.0, ge=1.0, le=30.0)
    mcp_http_auth_token: str = Field(default="")
    mcp_servers_config_path: str = Field(default="config/mcp_servers.yaml")
    mcp_preferred_server: str = Field(default="")

    # ── 接入层: 飞书 / 企业微信 ──
    # 历史原因这些变量不带 ``PULSE_`` 前缀(由第三方文档指定的命名), 用
    # ``AliasChoices`` 同时兼容 ``PULSE_`` 前缀写法.
    feishu_sign_secret: str = Field(default="")
    wechat_work_corp_id: str = Field(
        default="", validation_alias=AliasChoices("WECHAT_WORK_CORP_ID")
    )
    wechat_work_agent_id: str = Field(
        default="", validation_alias=AliasChoices("WECHAT_WORK_AGENT_ID")
    )
    wechat_work_secret: str = Field(
        default="", validation_alias=AliasChoices("WECHAT_WORK_SECRET")
    )
    wechat_work_token: str = Field(
        default="", validation_alias=AliasChoices("WECHAT_WORK_TOKEN")
    )
    wechat_work_encoding_aes_key: str = Field(
        default="", validation_alias=AliasChoices("WECHAT_WORK_ENCODING_AES_KEY")
    )
    wechat_work_bot_id: str = Field(
        default="", validation_alias=AliasChoices("WECHAT_WORK_BOT_ID")
    )
    wechat_work_bot_secret: str = Field(
        default="", validation_alias=AliasChoices("WECHAT_WORK_BOT_SECRET")
    )

    model_config = SettingsConfigDict(
        env_prefix="PULSE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
