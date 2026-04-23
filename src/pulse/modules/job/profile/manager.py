"""JobProfileManager — Job domain 的 profile 文件 ↔ JobMemory 映射。

实现 ``pulse.core.profile.DomainProfileManager`` 协议。**memory 是单一事实源**,
两个文件是它的人类可读投影:

  - ``config/profile/job.yaml``  — hard_constraints (双向) + memory_items 摘要
                                    (只读) + resume parsed 摘要 (只读)
  - ``config/profile/resume.md`` — resume 原文 (双向)

同步语义:

  load()           文件 → memory (全量替换可编辑部分)
                     - hard_constraints: 清 job.hc.* 后按 yaml 重建
                     - resume raw_text:   若 resume.md 存在则 update_resume
                     - memory_items:     **保留不动** (yaml 的摘要不是权威源)
  sync_to_yaml()   memory → 文件 (全量刷新)
                     - job.yaml:   hard_constraints + memory_items 摘要 + resume 摘要
                     - resume.md:  resume raw_text (若存在)
  dump_current()   memory 当前 schema dict (不落盘)
  reset()          清整个 job.* 前缀 + 清空两个文件

本类不持有 DB 连接, 只持有注入的 ``WorkspaceMemory``。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ....core.memory.workspace_memory import WorkspaceMemory
from ....core.profile.base import DomainProfileError, DomainProfileManager, atomic_write_text
from ..memory import HARD_CONSTRAINT_FIELDS, JobMemory
from .schema import (
    HardConstraintsSchema,
    JobProfileSchema,
    MemoryItemsSummarySchema,
    MemoryItemSummary,
    ResumeSummarySchema,
)

logger = logging.getLogger(__name__)

_DOMAIN = "job"
_KEY_HC_PREFIX = "job.hc."
_KEY_DOC_RESUME = "job.doc:resume"
_KEY_DOC_RESUME_PARSED = "job.doc:resume.parsed"

_YAML_HEADER = """# ============================================================================
# Pulse Job Profile (domain=job)
#
# 本文件的三个段语义不同:
#
#   hard_constraints       双向: 你手改后跑 `pulse profile load --domain=job` 生效
#   memory_items_summary   单向 (memory → yaml): 只读摘要, 改了也会被覆盖
#   resume                 单向 (memory → yaml): 只读摘要, 原文见 resume.md
#
# 增删 memory_items 请通过自然语言对话 (Brain 会调 job.memory.record intent
# tool); 更新简历请编辑 resume.md 后 `pulse profile load --domain=job`。
# ============================================================================

"""

_RESUME_MD_HEADER = """<!--
Pulse Job Resume (raw text)
- 编辑本文件后运行 `pulse profile load --domain=job` 才会写入 memory。
- 本文件会被 `pulse profile sync` / Brain mutation 覆盖, 请把终稿留在这里。
-->

"""


class JobProfileManager(DomainProfileManager):
    domain = _DOMAIN

    def __init__(
        self,
        *,
        workspace_memory: WorkspaceMemory,
        workspace_id: str,
        yaml_path: Path,
        resume_md_path: Path,
    ) -> None:
        self._ws = workspace_memory
        self._workspace_id = str(workspace_id or "").strip()
        if not self._workspace_id:
            raise ValueError("workspace_id must be non-empty")
        self.yaml_path = Path(yaml_path)
        self.resume_md_path = Path(resume_md_path)

    # ── lifecycle ────────────────────────────────────────

    def load(self) -> None:
        """文件 → memory。只替换 hard_constraints 与 resume raw_text;
        memory_items 不受 yaml load 影响 (yaml 只有摘要, 没有权威数据)。"""
        mem = self._job_memory()

        # --- yaml 侧 ---
        yaml_payload = self._load_yaml_if_exists()
        if yaml_payload is not None:
            try:
                schema = JobProfileSchema.model_validate(yaml_payload)
            except Exception as exc:
                raise DomainProfileError(
                    f"profile yaml schema invalid at {self.yaml_path}: {exc}"
                ) from exc

            # 清掉旧 hard_constraints, 按 yaml 重建
            self._ws.delete_facts_by_prefix(self._workspace_id, _KEY_HC_PREFIX)
            self._apply_hard_constraints(mem, schema.hard_constraints)

            logger.info(
                "job profile.yaml loaded: hc_fields=%d (memory_items preserved, resume handled separately)",
                self._count_non_empty_hc(schema.hard_constraints),
            )

        # --- resume.md 侧 ---
        resume_text = self._load_resume_md_if_exists()
        if resume_text:
            mem.update_resume(resume_text)
            logger.info("resume.md loaded into memory (%d chars)", len(resume_text))

    def sync_to_yaml(self) -> None:
        """memory → 两个文件 (原子写)。IO 失败只记 warning。"""
        try:
            mem = self._job_memory()
            snap = mem.snapshot()
            schema = self._build_schema_from_snapshot(snap)
            atomic_write_text(self.yaml_path, self._render_yaml(schema))

            if snap.resume is not None and snap.resume.raw_text:
                atomic_write_text(
                    self.resume_md_path,
                    _RESUME_MD_HEADER + snap.resume.raw_text,
                )
            logger.debug("job profile synced to %s + %s", self.yaml_path, self.resume_md_path)
        except Exception as exc:   # noqa: BLE001 — Hook 调用方不关心 IO
            logger.warning("job profile sync_to_yaml failed: %s", exc)

    def dump_current(self) -> dict[str, Any]:
        mem = self._job_memory()
        schema = self._build_schema_from_snapshot(mem.snapshot())
        return schema.model_dump(mode="json", exclude_none=False)

    def memory(self) -> JobMemory:
        """Public accessor — CLI / 开发工具用它直接做 item-级 CRUD。"""
        return self._job_memory()

    def reset(self) -> None:
        mem = self._job_memory()
        removed = mem.clear_all()
        atomic_write_text(self.yaml_path, self._render_yaml(JobProfileSchema()))
        if self.resume_md_path.is_file():
            try:
                self.resume_md_path.unlink()
            except OSError as exc:
                logger.warning("failed to remove %s: %s", self.resume_md_path, exc)
        logger.info("job profile reset (removed %d facts, yaml reset, resume.md removed)", removed)

    # ── helpers ──────────────────────────────────────────

    def _job_memory(self) -> JobMemory:
        return JobMemory(
            workspace_memory=self._ws,
            workspace_id=self._workspace_id,
            source="job.profile.manager",
        )

    def _load_yaml_if_exists(self) -> dict[str, Any] | None:
        path = self.yaml_path
        if not path.is_file():
            logger.info("job profile yaml not found, skip: %s", path)
            return None
        try:
            import yaml  # noqa: PLC0415
        except ImportError as exc:
            raise DomainProfileError("PyYAML not installed; pip install pyyaml") from exc
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DomainProfileError(f"cannot read {path}: {exc}") from exc
        try:
            parsed = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            raise DomainProfileError(f"invalid yaml at {path}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise DomainProfileError(
                f"yaml {path} must be a mapping at top level, got {type(parsed).__name__}"
            )
        return parsed

    def _load_resume_md_if_exists(self) -> str:
        path = self.resume_md_path
        if not path.is_file():
            return ""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("cannot read %s: %s", path, exc)
            return ""
        # 去掉 HTML 注释头 (如果存在), 保留正文
        return self._strip_md_header(text).strip()

    @staticmethod
    def _strip_md_header(text: str) -> str:
        if text.lstrip().startswith("<!--"):
            end = text.find("-->")
            if end >= 0:
                return text[end + len("-->"):]
        return text

    def _apply_hard_constraints(
        self, mem: JobMemory, hc: HardConstraintsSchema
    ) -> None:
        """把 yaml 里非空的 hard_constraints 写进 memory。"""
        values: dict[str, Any] = {
            "preferred_location": hc.preferred_location,
            "salary_floor_monthly": hc.salary_floor_monthly,
            "target_roles": hc.target_roles,
            "experience_level": hc.experience_level,
        }
        for name in HARD_CONSTRAINT_FIELDS:
            value = values.get(name)
            if value in (None, "", [], {}):
                continue
            try:
                mem.set_hard_constraint(name, value)
            except ValueError as exc:
                logger.warning("skip invalid hard_constraint %s=%r: %s", name, value, exc)

    def _build_schema_from_snapshot(self, snap: Any) -> JobProfileSchema:
        """memory snapshot → JobProfileSchema。"""
        hc = snap.hard_constraints
        hc_schema = HardConstraintsSchema(
            preferred_location=list(hc.preferred_location or []),
            salary_floor_monthly=hc.salary_floor_monthly,
            target_roles=list(hc.target_roles or []),
            experience_level=hc.experience_level or "",
        )

        active = snap.active_items()
        recent = [
            MemoryItemSummary(
                id=it.id,
                type=it.type,
                target=it.target,
                content=it.content,
                valid_until=it.valid_until,
                superseded_by=it.superseded_by,
                created_at=it.created_at,
            )
            for it in active[:20]  # 只投影近 20 条
        ]
        items_summary = MemoryItemsSummarySchema(
            total_active=len(active),
            total_all=len(snap.memory_items),
            recent=recent,
        )

        resume_schema = ResumeSummarySchema(raw_path=str(self.resume_md_path))
        if snap.resume is not None and snap.resume.raw_text:
            resume_schema.present = True
            resume_schema.raw_hash = snap.resume.raw_hash
            resume_schema.updated_at = snap.resume.updated_at
            resume_schema.parsed_is_stale = snap.resume.parsed_is_stale
            p = snap.resume.parsed
            if p is not None:
                resume_schema.summary = p.summary
                resume_schema.years_exp = p.years_exp
                resume_schema.skills = list(p.skills or [])
                resume_schema.top_experiences = [
                    {
                        "company": e.get("company", ""),
                        "role": e.get("role", ""),
                        "period": e.get("period", ""),
                    }
                    for e in (p.experiences or [])[:3]
                ]

        return JobProfileSchema(
            hard_constraints=hc_schema,
            memory_items_summary=items_summary,
            resume=resume_schema,
        )

    @staticmethod
    def _count_non_empty_hc(hc: HardConstraintsSchema) -> int:
        count = 0
        if hc.preferred_location:
            count += 1
        if hc.salary_floor_monthly is not None:
            count += 1
        if hc.target_roles:
            count += 1
        if hc.experience_level:
            count += 1
        return count

    @staticmethod
    def _render_yaml(schema: JobProfileSchema) -> str:
        try:
            import yaml  # noqa: PLC0415
        except ImportError as exc:
            raise DomainProfileError("PyYAML not installed; pip install pyyaml") from exc
        payload = schema.model_dump(mode="json", exclude_none=False)
        body = yaml.safe_dump(
            payload,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            indent=2,
        )
        return _YAML_HEADER + body


__all__ = ["JobProfileManager"]
