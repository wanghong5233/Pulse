"""Job 领域业务配置.

与 Pulse 内核配置 ``pulse.core.config.Settings`` 分层:

  - 内核关心 host/port/brain/memory/governance/mcp/event_store — 与业务无关.
  - 本文件只承载 **Job 领域** 自己的策略(batch size / 阈值 / HITL 开关 /
    patrol 调度区间 / default workspace 等).

独立的 ``BaseSettings`` 子类通过 ``env_prefix="PULSE_JOB_"`` 自载 ``.env``,
无需内核转发,新增字段时也不会污染内核.

关于 patrol 的启停: 见 ADR-004 §6.1.1 — patrol 的启停**不**使用 env,
而是在 `on_startup` 时无条件 `register_patrol(enabled=False)`,由用户通过 IM
(`system.patrol.enable/disable`) 独占控制(单一认知路径)。本文件只保留
patrol 的**调度节拍**参数(interval_peak / interval_offpeak),它们是性能 knob,
与启停语义正交。

使用方式::

    from pulse.modules.job.config import get_job_settings
    policy = get_job_settings()
    interval = policy.patrol_chat_interval_peak
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _profile_root() -> Path:
    """定位 ``<repo_root>/config/profile``, 找不到 pyproject.toml 时降级到 CWD。"""
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").is_file():
            return (parent / "config" / "profile").resolve()
    return (Path.cwd() / "config" / "profile").resolve()


def _default_profile_yaml_path() -> str:
    return str(_profile_root() / "job.yaml")


def _default_resume_md_path() -> str:
    return str(_profile_root() / "resume.md")


class JobSettings(BaseSettings):
    """Job domain business-layer knobs.

    Loads ``PULSE_JOB_*`` environment variables (and ``.env``) directly.
    """

    default_workspace_id: str = Field(default="job.default")

    # Profile yaml mirror (see modules/job/profile/manager.py).
    # memory 是事实源, yaml 是 hard_constraints 的双向投影 + memory items /
    # resume 的只读摘要; 用户手改后需要 `pulse profile load` 才进 memory。
    profile_yaml_path: str = Field(default_factory=_default_profile_yaml_path)

    # Resume markdown mirror (raw text 原文, 用户直接编辑; 双向同步)。
    profile_resume_md_path: str = Field(default_factory=_default_resume_md_path)

    # greet policy
    greet_batch_size: int = Field(default=3, ge=1, le=20)
    greet_match_threshold: float = Field(default=65.0, ge=30.0, le=95.0)
    greet_daily_limit: int = Field(default=50, ge=1, le=500)
    greet_default_keyword: str = Field(default="AI Agent 实习")
    greet_greeting_template: str = Field(default="")

    # chat policy
    chat_default_profile_id: str = Field(default="default")
    chat_auto_execute: bool = Field(default=False)

    # HITL
    hitl_required: bool = Field(default=True)

    # patrol scheduling — 只保留调度节拍; 启停语义见 ADR-004 §6.1.1
    # (IM 独占控制, 不读 env)
    patrol_greet_interval_peak: int = Field(default=900, ge=30, le=86400)
    patrol_greet_interval_offpeak: int = Field(default=1800, ge=30, le=86400)
    patrol_chat_interval_peak: int = Field(default=180, ge=30, le=86400)
    patrol_chat_interval_offpeak: int = Field(default=600, ge=30, le=86400)

    model_config = SettingsConfigDict(
        env_prefix="PULSE_JOB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_job_settings() -> JobSettings:
    return JobSettings()


__all__ = ["JobSettings", "get_job_settings"]
