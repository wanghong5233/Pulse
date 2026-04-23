"""Controller for the ``job.profile`` capability (v2)。

暴露 Job domain memory 的三类存储 (hard_constraints / memory_items / resume),
所有数据落在 ``WorkspaceMemory`` 的 ``job.*`` 命名空间。

两个入口:

1. **HTTP REST** (保留给 /prefs 页面与运维, 见 register_routes)
2. **Intent Tools** (给 Brain tool_use, 通用智能体写入路径)

Intent 清单 (见 docs/modules/job/architecture.md §7.1):

   job.memory.record          追加一条或多条 MemoryItem
   job.memory.retire          将 MemoryItem 标记为过期
   job.memory.supersede       用新 item 取代旧 item
   job.memory.list            按 type/target 查询 (read-only)
   job.hard_constraint.set    设置 hard constraint 某字段
   job.hard_constraint.unset  取消 hard constraint 某字段
   job.resume.update          整体替换简历原文
   job.resume.patch_parsed    对简历解析结果打补丁
   job.resume.get             读取完整简历 (read-only)
   job.snapshot.get           读取完整 Job snapshot (read-only, 调试用)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ....core.memory.workspace_memory import WorkspaceMemory
from ....core.module import BaseModule, IntentSpec
from ....core.profile.base import DomainProfileManager
from ....core.storage.engine import DatabaseEngine
from ....core.task_context import TaskContext
# ─────────────────────────────────────────────────────────────────────
# ToolUseContract §4.5 — extract_facts hooks (Contract C v2)
# ─────────────────────────────────────────────────────────────────────
# Project job.memory.* observations into tiny `{str: scalar}` dicts so
# CommitmentVerifier's TurnEvidence can tell the judge exactly what was
# persisted. The judge needs ``ok=true`` + ``item_type`` + ``item_target``
# to ground claims like "已记录你不投拼多多".


def _extract_facts_record(observation: Any) -> dict[str, Any]:
    if not isinstance(observation, dict):
        return {}
    facts: dict[str, Any] = {"intent": "job.memory.record"}
    ok = observation.get("ok")
    if isinstance(ok, bool):
        facts["ok"] = ok
    if not ok:
        err = observation.get("error")
        if isinstance(err, str) and err:
            facts["error_class"] = err[:80]
        return facts
    item = observation.get("item")
    if isinstance(item, dict):
        for src, dst in (("type", "item_type"), ("target", "item_target"), ("id", "item_id")):
            val = item.get(src)
            if isinstance(val, (str, int, float, bool)) and val is not None:
                facts[dst] = val
    return facts


def _extract_facts_retire(observation: Any) -> dict[str, Any]:
    if not isinstance(observation, dict):
        return {}
    facts: dict[str, Any] = {"intent": "job.memory.retire"}
    for key in ("ok", "id", "retired"):
        val = observation.get(key)
        if isinstance(val, (str, int, float, bool)) and val is not None:
            facts[key if key != "id" else "item_id"] = val
    return facts


from ..config import get_job_settings
from ..memory import (
    HARD_CONSTRAINT_FIELDS,
    MEMORY_ITEM_TYPES,
    JobMemory,
)
from ..preference_applier import JobPreferenceApplier
from .manager import JobProfileManager
from .service import JobProfileService

logger = logging.getLogger(__name__)


# ── HTTP request DTOs ──────────────────────────────────────

class RecordItemRequest(BaseModel):
    type: str = Field(..., min_length=1, max_length=60)
    content: str = Field(..., min_length=1, max_length=600)
    target: str | None = Field(default=None, max_length=160)
    raw_text: str = Field(default="", max_length=2000)
    valid_until: str | None = Field(default=None, max_length=40)


class RetireItemRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=80)


class SupersedeItemRequest(BaseModel):
    old_id: str = Field(..., min_length=1, max_length=80)
    type: str = Field(..., min_length=1, max_length=60)
    content: str = Field(..., min_length=1, max_length=600)
    target: str | None = Field(default=None, max_length=160)
    raw_text: str = Field(default="", max_length=2000)
    valid_until: str | None = Field(default=None, max_length=40)


class SetHardConstraintRequest(BaseModel):
    field: str = Field(..., min_length=1, max_length=60)
    value: Any


class UnsetHardConstraintRequest(BaseModel):
    field: str = Field(..., min_length=1, max_length=60)


class UpdateResumeRequest(BaseModel):
    raw_text: str = Field(..., min_length=1, max_length=50_000)


class PatchResumeParsedRequest(BaseModel):
    patch: dict[str, Any]


# ─────────────────────────────────────────────────────────────


class JobProfileModule(BaseModule):
    name = "job_profile"
    description = (
        "Job-domain memory (hard constraints / memory items / resume). "
        "Brain writes via intent tools; matcher/greeter/replier read via snapshot."
    )
    route_prefix = "/api/modules/job/profile"
    tags = ["job", "job_profile"]

    def __init__(self, *, service: JobProfileService | None = None) -> None:
        super().__init__()
        self._settings = get_job_settings()
        self._workspace_memory: WorkspaceMemory | None = None
        self._service = service or self._build_default_service()
        self.intents = self._build_intents()

    # ── wiring ─────────────────────────────────────────────

    def _build_default_service(self) -> JobProfileService | None:
        try:
            engine = DatabaseEngine()
        except Exception as exc:
            logger.warning("job_profile starting without DB engine: %s", exc)
            return None
        self._workspace_memory = WorkspaceMemory(db_engine=engine)
        memory = JobMemory(
            workspace_memory=self._workspace_memory,
            workspace_id=self._settings.default_workspace_id,
            source="job.profile",
        )
        return JobProfileService(
            memory=memory,
            emit_stage_event=self.emit_stage_event,
        )

    # ── Domain profile manager (file ↔ memory 同步) ────────

    def get_profile_manager(self) -> DomainProfileManager | None:
        if self._workspace_memory is None:
            return None
        try:
            return JobProfileManager(
                workspace_memory=self._workspace_memory,
                workspace_id=self._settings.default_workspace_id,
                yaml_path=Path(self._settings.profile_yaml_path),
                resume_md_path=Path(self._settings.profile_resume_md_path),
            )
        except Exception as exc:
            logger.warning("JobProfileModule.get_profile_manager failed: %s", exc)
            return None

    # ── Preference applier (Soul reflection → JobMemory 持久化) ─────
    def get_preference_appliers(self) -> list[Any]:
        """把 PreferenceExtractor 抽出的 ``domain=job`` 偏好派发给 JobMemory.

        架构依据: ``docs/Pulse-DomainMemory与Tool模式.md`` §2.2.
        没有 WorkspaceMemory (= 没连上 DB) 时返回空列表, 让 dispatcher
        把所有 job.* domain_prefs 标记为 skipped 并记事件, 而不是硬报错.
        """
        if self._workspace_memory is None:
            logger.info(
                "JobProfileModule: skip preference applier registration "
                "(WorkspaceMemory unavailable)",
            )
            return []
        engine = self._workspace_memory.db_engine
        return [
            JobPreferenceApplier(
                db_engine=engine,
                default_workspace_id=self._settings.default_workspace_id,
            ),
        ]

    # ── Domain snapshot provider (Brain system prompt 注入) ─

    def get_domain_snapshot_provider(self) -> Callable[[TaskContext], str] | None:
        ws_memory = self._workspace_memory
        if ws_memory is None:
            return None
        default_ws = self._settings.default_workspace_id

        def _provider(ctx: TaskContext) -> str:
            workspace_id = str(getattr(ctx, "workspace_id", "") or default_ws)
            try:
                mem = JobMemory(
                    workspace_memory=ws_memory,
                    workspace_id=workspace_id,
                    source="job.profile",
                )
                return mem.snapshot().to_prompt_section()
            except Exception as exc:
                logger.warning("job snapshot provider failed: %s", exc)
                return ""

        return _provider

    # ── Intent tools (给 Brain tool_use) ───────────────────

    def _build_intents(self) -> list[IntentSpec]:
        if self._service is None:
            return []
        s = self._service

        type_enum_hint = list(MEMORY_ITEM_TYPES)
        hc_field_enum = list(HARD_CONSTRAINT_FIELDS)

        return [
            IntentSpec(
                name="job.memory.record",
                extract_facts=_extract_facts_record,
                description=(
                    "Record a semantic job-seeking memory item (preference / aversion / "
                    "capability / application event). Non hard-constraint facts only."
                ),
                when_to_use=(
                    "把一条自然语言事实拆成 1..N 个 JobMemoryItem (type / content / target / "
                    "valid_until) 写入 JobMemory。用于 hard constraint 覆盖不到的所有语义范畴: "
                    "公司黑白名单 / 风格偏好 / 能力主张 / 投递事件等。"
                    "一条用户消息可能产生多条 item (例: 'application_event' + 'avoid_company'), "
                    "需对每个独立 item 分别调用。"
                ),
                when_not_to_use=(
                    "不用于: 1) 硬约束 (preferred_location / salary_floor_monthly / "
                    "target_roles / experience_level) → 走 `job.hard_constraint.set`; "
                    "2) 通用画像 (姓名/身份/角色设定) → 走 `memory_update`; "
                    "3) 只是闲聊, 没有可落盘的事实。"
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "description": (
                                f"Recommended: one of {type_enum_hint}. "
                                "You MAY use a custom string if none fits; 'other' is the safe fallback."
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": (
                                "A single concise sentence describing this item, "
                                "written to be directly injected into downstream LLM prompts. "
                                "Avoid pronouns; include the target and the user's intent."
                            ),
                        },
                        "target": {
                            "type": ["string", "null"],
                            "description": (
                                "Entity this item is about (company name / keyword / role / null). "
                                "Null when the preference is general."
                            ),
                        },
                        "raw_text": {
                            "type": "string",
                            "description": "User's original wording (for audit & fallback).",
                            "default": "",
                        },
                        "valid_until": {
                            "type": ["string", "null"],
                            "description": (
                                "ISO-8601 UTC timestamp when this item expires. "
                                "Estimate when the user hints at temporariness (e.g. '暂时'→30 days); "
                                "null for permanent preferences."
                            ),
                        },
                    },
                    "required": ["type", "content"],
                    "additionalProperties": False,
                },
                handler=s.record_item,
                mutates=True,
                examples=[
                    {
                        "user_utterance": "不要投拼多多了, 笔试挂了",
                        "kwargs": {
                            "type": "avoid_company",
                            "target": "拼多多",
                            "content": "避免投递拼多多 (用户笔试挂过)",
                            "raw_text": "不要投拼多多了, 笔试挂了",
                            "valid_until": None,
                        },
                    },
                    {
                        "user_utterance": "我能力还不够, 暂时不要投大厂",
                        "kwargs": {
                            "type": "avoid_trait",
                            "target": "大厂",
                            "content": "用户自评能力不足, 暂时回避大厂岗位",
                            "raw_text": "我能力还不够, 暂时不要投大厂",
                            "valid_until": None,
                        },
                    },
                ],
            ),
            IntentSpec(
                name="job.memory.retire",
                extract_facts=_extract_facts_retire,
                description=(
                    "Mark a previously-recorded job memory item as expired (effective immediately)."
                ),
                when_to_use=(
                    "把一条 JobMemoryItem 标记为失效 (valid_until=now, 不留 superseded_by)。"
                    "前置条件: id 必须由 `job.memory.list` 或 `job.snapshot.get` 返回值提供, "
                    "不得构造或猜测。语义为\"撤销且不替换\"。"
                ),
                when_not_to_use=(
                    "职责划分: 1) 需要**带新值替换** → `job.memory.supersede`; "
                    "2) 清空 hard constraint 字段 → `job.hard_constraint.unset`; "
                    "3) 没有确切 id 时先调 `job.memory.list` 取, 不调用本工具。"
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Item id to retire"},
                    },
                    "required": ["id"],
                    "additionalProperties": False,
                },
                handler=s.retire_item,
                mutates=True,
                examples=[],
            ),
            IntentSpec(
                name="job.memory.supersede",
                description=(
                    "Replace an existing job memory item with a new one; old item linked via "
                    "``superseded_by`` (no replacement → use retire)."
                ),
                when_to_use=(
                    "原子 retire(old_id) + record(new_item), 新 item 标注 superseded_by=old_id。"
                    "前置条件: 必须同时提供 old_id + 完整新字段 (type / content / target / valid_until)。"
                    "old_id 只能从 `job.memory.list` 或 `job.snapshot.get` 取, 不得构造。"
                ),
                when_not_to_use=(
                    "职责划分: 1) 撤销不替换 → `job.memory.retire`; "
                    "2) 新增且无旧 item → `job.memory.record`; "
                    "3) 硬约束 (preferred_location 等) 变更 → `job.hard_constraint.set` 直接覆盖, "
                    "不走 supersede (两者数据表不同)。"
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "old_id": {"type": "string"},
                        "type": {"type": "string"},
                        "content": {"type": "string"},
                        "target": {"type": ["string", "null"]},
                        "raw_text": {"type": "string", "default": ""},
                        "valid_until": {"type": ["string", "null"]},
                    },
                    "required": ["old_id", "type", "content"],
                    "additionalProperties": False,
                },
                handler=s.supersede_item,
                mutates=True,
                examples=[],
            ),
            IntentSpec(
                name="job.memory.list",
                description=(
                    "Read-only: list job memory items, optionally filtered by type and/or target."
                ),
                when_to_use=(
                    "只读枚举 JobMemoryItem, 支持按 type / target 过滤, include_expired 默认 false。"
                    "两类主要场景: (1) 回答\"我有哪些 X 类偏好\"的查询; "
                    "(2) 作为 retire / supersede 的前置步骤, 获取目标 item 的 id。"
                ),
                when_not_to_use=(
                    "职责划分: 1) 读硬约束 → `job.snapshot.get`; "
                    "2) 对话历史检索 → `memory_search`; "
                    "3) 展示给用户时优先用 snapshot 汇总视图, 避免与 list 重复渲染。"
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "type": {"type": ["string", "null"]},
                        "target": {"type": ["string", "null"]},
                        "include_expired": {"type": "boolean", "default": False},
                    },
                    "additionalProperties": False,
                },
                handler=s.list_items,
                mutates=False,
                examples=[],
            ),
            IntentSpec(
                name="job.hard_constraint.set",
                description=(
                    "Set a hard constraint used by the job search connector (maps to BOSS URL "
                    f"params). Allowed fields: {hc_field_enum}."
                ),
                when_to_use=(
                    "写入 / 覆盖 JobProfile 的单个硬约束字段 (直接拼入招聘平台搜索 URL, 触发平台侧过滤)。"
                    "参数约束: field ∈ allowed enum; value 按字段类型: "
                    "preferred_location / target_roles = list[str], salary_floor_monthly = int (月薪, 单位 k), "
                    "experience_level = str。同字段重复调用为覆盖语义 (非 append)。"
                ),
                when_not_to_use=(
                    "职责划分: 1) 任何**不在 allowed enum**的字段 → `job.memory.record`, 禁止伪造新字段; "
                    "2) 公司黑名单 / 工作风格 / 调性 / 技能主张 → `job.memory.record`; "
                    "3) 值本身不确定时 (例: 用户只表达\"稍微高一点\") 先向用户澄清具体值, 不推断数字。"
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "field": {
                            "type": "string",
                            "enum": hc_field_enum,
                        },
                        "value": {
                            "description": (
                                "Field value; list[str] for preferred_location and target_roles, "
                                "int for salary_floor_monthly (monthly salary in K), "
                                "string for experience_level."
                            ),
                        },
                    },
                    "required": ["field", "value"],
                    "additionalProperties": False,
                },
                handler=s.set_hard_constraint,
                mutates=True,
                examples=[
                    {
                        "user_utterance": "我想找杭州或上海的工作",
                        "kwargs": {
                            "field": "preferred_location",
                            "value": ["杭州", "上海"],
                        },
                    },
                    {
                        "user_utterance": "月薪不能低于 25k",
                        "kwargs": {"field": "salary_floor_monthly", "value": 25},
                    },
                ],
            ),
            IntentSpec(
                name="job.hard_constraint.unset",
                description="Clear a hard constraint field (rarely needed; prefer overwrite via set).",
                when_to_use=(
                    "清空 JobProfile 的单个硬约束字段, 使该维度不再参与平台侧过滤。"
                    "field 必须在 allowed enum 内; 语义等价\"该字段无值\"。"
                ),
                when_not_to_use=(
                    "职责划分与边界: 1) 想换新值 → `job.hard_constraint.set` 直接覆盖即可, 更紧凑; "
                    "2) field 不在 allowed enum → 属于 JobMemory, 没有硬约束可清; "
                    "3) 用户表达是\"放宽\"而非\"清空\"(仍想保留方向性偏好) → 改用 "
                    "`job.memory.record` 记语义 / 或先向用户澄清具体值。"
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "field": {"type": "string", "enum": hc_field_enum},
                    },
                    "required": ["field"],
                    "additionalProperties": False,
                },
                handler=s.unset_hard_constraint,
                mutates=True,
                examples=[],
            ),
            IntentSpec(
                name="job.resume.update",
                description=(
                    "Destructively replace the user's resume raw_text in JobProfile; "
                    "triggers LLM re-parse of the structured view."
                ),
                when_to_use=(
                    "整份替换简历正文 (raw_text) 并触发重新结构化解析。"
                    "副作用: 旧 raw_text + 旧 parsed 都被覆盖, 无历史版本快照。"
                    "前置: 必须提供完整 raw_text (plain / markdown), 空或极短字符串视为无效。"
                ),
                when_not_to_use=(
                    "职责划分: 1) 小幅修正 (加一个项目 / 改 years_exp / 增一个技能) → "
                    "`job.resume.patch_parsed` (避免全量重解析); "
                    "2) 仅调整 hard constraint (目标岗位 / 城市 / 薪资) → `job.hard_constraint.set`; "
                    "3) 只读查看 → `job.resume.get`。"
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "raw_text": {
                            "type": "string",
                            "description": "Full resume text (plain or markdown).",
                        },
                    },
                    "required": ["raw_text"],
                    "additionalProperties": False,
                },
                handler=s.update_resume,
                mutates=True,
                examples=[],
            ),
            IntentSpec(
                name="job.resume.patch_parsed",
                description=(
                    "Patch specific fields of the resume's parsed (structured) view without "
                    "re-running LLM parsing."
                ),
                when_to_use=(
                    "对 ResumeParsed 结构化视图做**局部**更新: summary / years_exp / skills / "
                    "experiences / projects / education 任一或多个字段。patch 是 dict, "
                    "仅覆盖给定 key, 未给出的 key 保持不变。不会触发 LLM 重解析, 成本约 0。"
                ),
                when_not_to_use=(
                    "职责划分: 1) 简历文本整体换新 → `job.resume.update` (触发重解析); "
                    "2) 只是查看 → `job.resume.get`; "
                    "3) patch 中包含 allowed key 之外的字段时请求失败, 需先对齐 schema。"
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "patch": {
                            "type": "object",
                            "description": (
                                "Partial ResumeParsed dict. Keys: summary, years_exp, skills, "
                                "experiences, projects, education."
                            ),
                        },
                    },
                    "required": ["patch"],
                    "additionalProperties": False,
                },
                handler=s.patch_resume_parsed,
                mutates=True,
                examples=[],
            ),
            IntentSpec(
                name="job.resume.get",
                description="Read-only: fetch the full resume (raw_text + parsed view).",
                when_to_use=(
                    "需要简历**完整**字段 (raw_text + parsed 全结构) 时调用, 用于生成定制打招呼 / "
                    "回复 / 自我介绍文案。token 成本高于 Job Snapshot 摘要, 仅在 snapshot 的 "
                    "resume_summary 字段不足以覆盖所需细节时使用。"
                ),
                when_not_to_use=(
                    "职责划分: 1) 仅需要 summary 级别信息 (总经验 / 主力方向) → 已在 Job Snapshot 内, "
                    "不必再调本工具; "
                    "2) 需要写入 / 更新 → `job.resume.update` 或 `job.resume.patch_parsed`。"
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                handler=s.get_resume,
                mutates=False,
                examples=[],
            ),
            IntentSpec(
                name="job.snapshot.get",
                description=(
                    "Read-only: full Job memory snapshot (hard constraints + active memory items "
                    "+ resume summary) as one aggregated view."
                ),
                when_to_use=(
                    "一次性拉取 JobProfile 的**聚合视图**用于调试 / introspection / 回答"
                    "\"我求职画像长什么样\"类综合查询。等价于 hard_constraint + memory.list + resume.summary 的并集, "
                    "token 成本中等。"
                ),
                when_not_to_use=(
                    "职责划分: 1) 只需要 memory items 列表 → `job.memory.list` (带 type/target 过滤更精准); "
                    "2) 只需要简历详情 → `job.resume.get`; "
                    "3) 运行时每一步都拉 snapshot 会抬升 token 成本, 非必要不重复调用。"
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                handler=lambda: s.snapshot(),
                mutates=False,
                examples=[],
            ),
        ]

    # ── HTTP routes ────────────────────────────────────────

    def register_routes(self, router: APIRouter) -> None:
        def _require_service() -> JobProfileService:
            if self._service is None:
                raise HTTPException(
                    status_code=503,
                    detail="job_profile requires a configured database; set DATABASE_URL",
                )
            return self._service

        @router.get("/health")
        async def health() -> dict[str, Any]:
            return {
                "module": self.name,
                "status": "ok" if self._service is not None else "degraded",
                "runtime": {
                    "workspace_id": self._settings.default_workspace_id,
                    "db_available": self._service is not None,
                    "intents_exposed": len(self.intents),
                    "hard_constraint_fields": list(HARD_CONSTRAINT_FIELDS),
                    "memory_item_types": list(MEMORY_ITEM_TYPES),
                },
            }

        @router.get("/snapshot")
        async def snapshot() -> dict[str, Any]:
            return _require_service().snapshot()

        @router.post("/memory/record")
        async def record_item(payload: RecordItemRequest) -> dict[str, Any]:
            return _require_service().record_item(**payload.model_dump())

        @router.post("/memory/retire")
        async def retire_item(payload: RetireItemRequest) -> dict[str, Any]:
            return _require_service().retire_item(id=payload.id)

        @router.post("/memory/supersede")
        async def supersede_item(payload: SupersedeItemRequest) -> dict[str, Any]:
            return _require_service().supersede_item(**payload.model_dump())

        @router.get("/memory/list")
        async def list_items(
            type: str | None = None,
            target: str | None = None,
            include_expired: bool = False,
        ) -> dict[str, Any]:
            return _require_service().list_items(
                type=type, target=target, include_expired=include_expired,
            )

        @router.post("/hard_constraint/set")
        async def set_hard_constraint(payload: SetHardConstraintRequest) -> dict[str, Any]:
            return _require_service().set_hard_constraint(
                field=payload.field, value=payload.value,
            )

        @router.post("/hard_constraint/unset")
        async def unset_hard_constraint(payload: UnsetHardConstraintRequest) -> dict[str, Any]:
            return _require_service().unset_hard_constraint(field=payload.field)

        @router.post("/resume/update")
        async def update_resume(payload: UpdateResumeRequest) -> dict[str, Any]:
            return _require_service().update_resume(raw_text=payload.raw_text)

        @router.post("/resume/patch_parsed")
        async def patch_resume_parsed(payload: PatchResumeParsedRequest) -> dict[str, Any]:
            return _require_service().patch_resume_parsed(patch=payload.patch)

        @router.get("/resume")
        async def get_resume() -> dict[str, Any]:
            return _require_service().get_resume()


def get_module() -> JobProfileModule:
    return JobProfileModule()
