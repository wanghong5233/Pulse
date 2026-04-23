"""JobProfile 结构化契约 — 对应 ``config/profile/job.yaml``。

v2 架构下 JobMemory 分三类存储 (见 ``docs/modules/job/architecture.md`` §6):

  [1] Hard Constraints  → 本 schema 的 ``hard_constraints`` 段 (人类可编辑)
  [2] Memory Items      → 本 schema 的 ``memory_items_summary`` 段 (仅摘要, 不可编辑)
  [3] Domain Documents  → resume 原文走独立的 ``config/profile/resume.md``;
                          本 schema 的 ``resume`` 段只投影 parsed 摘要

只有 ``hard_constraints`` 是 yaml ↔ memory 双向同步的; 其他字段是 memory →
yaml 的单向投影 (给人看的镜像), 用户手工编辑无效。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class HardConstraintsSchema(BaseModel):
    """可编辑的硬约束 (对应 JobMemory.HardConstraints)。

    字段名与 ``pulse.modules.job.memory.HARD_CONSTRAINT_FIELDS`` 保持一致。
    """

    model_config = ConfigDict(extra="forbid")

    preferred_location: list[str] = Field(default_factory=list)
    salary_floor_monthly: int | None = None
    target_roles: list[str] = Field(default_factory=list)
    experience_level: str = ""


class MemoryItemSummary(BaseModel):
    """Memory Item 的只读摘要 (给人眼审阅用)。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    target: str | None = None
    content: str
    valid_until: str | None = None
    superseded_by: str | None = None
    created_at: str = ""


class MemoryItemsSummarySchema(BaseModel):
    """Memory Items 的 yaml 投影 — 仅列 active items 的近期若干条。"""

    model_config = ConfigDict(extra="forbid")

    total_active: int = 0
    total_all: int = 0
    recent: list[MemoryItemSummary] = Field(default_factory=list)
    note: str = (
        "Memory items 在 yaml 中只做只读摘要; 要增删请通过自然语言"
        " (Brain 会调 job.memory.record / retire / supersede intent tool)。"
    )


class ResumeSummarySchema(BaseModel):
    """Resume 的 yaml 投影 — 结构化摘要, 原文在 resume.md。"""

    model_config = ConfigDict(extra="forbid")

    present: bool = False
    raw_hash: str = ""
    updated_at: str = ""
    parsed_is_stale: bool = True
    summary: str = ""
    years_exp: int | None = None
    skills: list[str] = Field(default_factory=list)
    top_experiences: list[dict[str, Any]] = Field(default_factory=list)
    raw_path: str = ""  # 提示用户原文在哪个 md 文件


class JobProfileSchema(BaseModel):
    """Job domain 的 yaml 顶层结构。

    设计约定:
      - ``hard_constraints`` 字段由用户自由编辑, ``pulse profile load`` 后生效。
      - ``memory_items_summary`` / ``resume`` 字段是 memory → yaml 的单向投影,
        人工改写后会在下次 mutation 时被覆盖。
    """

    model_config = ConfigDict(extra="forbid")

    hard_constraints: HardConstraintsSchema = Field(default_factory=HardConstraintsSchema)
    memory_items_summary: MemoryItemsSummarySchema = Field(
        default_factory=MemoryItemsSummarySchema
    )
    resume: ResumeSummarySchema = Field(default_factory=ResumeSummarySchema)


__all__ = [
    "JobProfileSchema",
    "HardConstraintsSchema",
    "MemoryItemsSummarySchema",
    "MemoryItemSummary",
    "ResumeSummarySchema",
]
