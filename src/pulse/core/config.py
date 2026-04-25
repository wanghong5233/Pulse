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

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# v2 起只保留两档: off (不挂 SafetyPlane / 直发) 与 enforce (policy 门控).
# 旧 "shadow" 档在 v1 ADR 下是 "评估但不阻断", 实际使用中"不阻断"等于
# 没接入, 只增加审计噪声和代码分支; v2 已去除. 兼容: 旧配置里写了
# shadow 会被 field_validator 升级为 enforce (就地 fail-loud 迁移).
_VALID_SAFETY_PLANE_MODES: frozenset[str] = frozenset(("off", "enforce"))


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

    # ── SafetyPlane (ADR-006-v2) ──
    # off     = 不做授权判决, Service 层直发 (仅本地调试 / 灰度回滚用)
    # enforce = Service 层的 side-effect 前置跑 policy 函数; ask 分支走
    #           SuspendedTaskStore 挂起 + 发 IM 问用户; 默认值.
    safety_plane: str = Field(default="enforce")
    # SuspendedTaskStore 挂起 / resume 查找都落在同一个 workspace_id, Pulse
    # 是单用户自部署 (单 workspace), 默认 "default" 即可. 以后真的要多租户
    # 了, 这里换成 per-user 查表映射 —— 但 MVP 不做那事.
    safety_workspace_id: str = Field(default="default")

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

    @field_validator("safety_plane")
    @classmethod
    def _validate_safety_plane(cls, value: str) -> str:
        # fail-loud: 拼写错了的 PULSE_SAFETY_PLANE 不应默默降级, 也不应在
        # Service 层第一次产生 side-effect 时才崩.
        #
        # "shadow" 是 v1 ADR-006 的遗留档位, v2 去掉 Brain hook 后不再有意义
        # (没有 hook 就没有"评估而不阻断"这种状态). 这里把旧值自动升级成
        # enforce 而不是 raise —— 单用户自部署的用户没法也不该手动迁配置,
        # 自动把他们带进新默认比给启动失败更友好. 但升级要发 warn, 让本人
        # 看到行为变了.
        import logging as _logging
        import warnings as _warnings

        normalized = (value or "").strip().lower()
        if normalized == "shadow":
            _warnings.warn(
                "PULSE_SAFETY_PLANE=shadow is removed in ADR-006-v2; "
                "upgrading to 'enforce' (policy gate is now at the service "
                "layer, shadow mode is no longer meaningful).",
                DeprecationWarning,
                stacklevel=2,
            )
            _logging.getLogger(__name__).warning(
                "PULSE_SAFETY_PLANE=shadow auto-upgraded to 'enforce' (ADR-006-v2)"
            )
            normalized = "enforce"
        if normalized not in _VALID_SAFETY_PLANE_MODES:
            raise ValueError(
                f"PULSE_SAFETY_PLANE must be one of {sorted(_VALID_SAFETY_PLANE_MODES)}, "
                f"got {value!r}"
            )
        return normalized


@lru_cache
def get_settings() -> Settings:
    return Settings()
