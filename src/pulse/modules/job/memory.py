"""Job domain memory facade — v2 (三类存储)。

对应文档:
  - ``docs/Pulse-DomainMemory与Tool模式.md`` §3.1-3.3
  - ``docs/modules/job/architecture.md`` §6

JobMemory 是业务侧在 ``WorkspaceMemory`` 之上的薄 facade, 内部按
``workspace_facts`` 的 key 前缀把 ``job.*`` 命名空间分成四类存储:

    [1] Hard Constraints (job.hc.*)
        - 连接器 IO 能直接当 filter 用的字段, 字段集小且稳定
        - 字段: preferred_location / salary_floor_monthly / target_roles / experience_level

    [2] Memory Items (job.item:<uuid>)
        - schema 开放的半结构化偏好/事件/软约束池
        - 每条含 type / target / content / raw_text / valid_until / superseded_by
        - LLM 在写入时决定 type (推荐 enum + 允许 other)

    [3] Domain Documents (job.doc:resume 及衍生 key)
        - 整体替换式的领域文档, 当前唯一实例是简历
        - job.doc:resume         → {raw_text, raw_hash, updated_at}
        - job.doc:resume.parsed  → LLM 解析缓存

    [4] Derived Caches (job.derived.*)
        - 由领域组件从 LLM 推导出的可复用中间结果, 当前用于 trait→公司集合
        - key: job.derived.trait_company_set:<sha1(trait_type:trait)>
        - value: {trait_type, trait, companies, model, updated_at, expires_at}

所有值由 ``WorkspaceMemory`` 统一 JSON 编解码, 本类只处理 Python 对象。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pulse.core.memory.workspace_memory import WorkspaceMemory

if TYPE_CHECKING:
    from pulse.core.memory.core_memory import CoreMemory
    from pulse.core.storage.engine import DatabaseEngine

logger = logging.getLogger(__name__)

# ── Key namespaces (领域私有) ──────────────────────────────────
_KEY_JOB_PREFIX = "job."

# Hard Constraints
_KEY_HC_PREFIX = "job.hc."
_KEY_HC_PREFERRED_LOCATION = "job.hc.preferred_location"
_KEY_HC_SALARY_FLOOR_MONTHLY = "job.hc.salary_floor_monthly"
_KEY_HC_TARGET_ROLES = "job.hc.target_roles"
_KEY_HC_EXPERIENCE_LEVEL = "job.hc.experience_level"

# 允许设置的 Hard Constraint 字段; 其他一律走 Memory Items
HARD_CONSTRAINT_FIELDS: tuple[str, ...] = (
    "preferred_location",
    "salary_floor_monthly",
    "target_roles",
    "experience_level",
)

# Memory Items
_KEY_ITEM_PREFIX = "job.item:"

# Domain Documents (当前仅 resume)
_KEY_DOC_RESUME = "job.doc:resume"
_KEY_DOC_RESUME_PARSED = "job.doc:resume.parsed"

# Derived caches
_KEY_DERIVED_TRAIT_COMPANY_PREFIX = "job.derived.trait_company_set:"

_DEFAULT_SOURCE = "job.memory"

# Memory Item type 推荐 enum (LLM 可填枚举外值, 兜底 'other')
MEMORY_ITEM_TYPES: tuple[str, ...] = (
    "avoid_company",
    "favor_company",
    "avoid_trait",
    "favor_trait",
    "application_event",
    "capability_claim",
    "constraint_note",
    "other",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize(s: Any) -> str:
    return str(s or "").strip()


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────
# Value objects
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MemoryItem:
    """一条语义记忆 item (对应 docs §3.2)。"""

    id: str
    type: str
    target: str | None
    content: str
    raw_text: str
    valid_from: str           # ISO-8601 UTC
    valid_until: str | None   # ISO-8601 UTC 或 None (永久)
    superseded_by: str | None
    created_at: str

    @property
    def is_active(self) -> bool:
        """item 当前是否生效 (未被取代 且 未过期)。"""
        if self.superseded_by:
            return False
        if self.valid_until:
            expires = _parse_iso(self.valid_until)
            if expires is not None and expires <= datetime.now(timezone.utc):
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TraitCompanySet:
    """LLM 展开的 trait→公司集合缓存。

    ``trait_type`` 目前只允许 ``avoid_trait`` / ``favor_trait``。
    ``companies`` 是规范化后的公司名列表, 供工具层做字面命中 gate。
    """

    trait_type: str
    trait: str
    companies: list[str]
    model: str
    updated_at: str
    expires_at: str

    @property
    def is_expired(self) -> bool:
        expiry = _parse_iso(self.expires_at)
        if expiry is None:
            return True
        return expiry <= datetime.now(timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trait_type": self.trait_type,
            "trait": self.trait,
            "companies": list(self.companies),
            "model": self.model,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class HardConstraints:
    """结构化硬约束快照 (只读值对象)。

    ``salary_floor_spec`` 保留用户原始量纲 (P2-B, audit trace_f3bda835ed94):
    当 LLM 按合同输出 ``{amount, unit, period[, work_days_per_month]}`` 时, JobMemory
    除了算出整数 K/月 (``salary_floor_monthly``) 之外, 还把归一化后的原始
    三要素原样落库, 供审计 / prompt 引用, 不让 "300元/天" 在下游只剩下
    "7 K/月" 一个裸数字. 用户若只给裸数字, 此字段为 ``None``.
    """

    preferred_location: list[str] = field(default_factory=list)
    salary_floor_monthly: int | None = None
    salary_floor_spec: dict[str, Any] | None = None
    target_roles: list[str] = field(default_factory=list)
    experience_level: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "preferred_location": list(self.preferred_location),
            "salary_floor_monthly": self.salary_floor_monthly,
            "salary_floor_spec": (
                dict(self.salary_floor_spec) if self.salary_floor_spec else None
            ),
            "target_roles": list(self.target_roles),
            "experience_level": self.experience_level,
        }

    def is_empty(self) -> bool:
        return (
            not self.preferred_location
            and self.salary_floor_monthly is None
            and not self.target_roles
            and not self.experience_level
        )


@dataclass(frozen=True, slots=True)
class ResumeParsed:
    """LLM 解析后的简历结构化视图。"""

    summary: str = ""
    years_exp: int | None = None
    skills: list[str] = field(default_factory=list)
    experiences: list[dict[str, Any]] = field(default_factory=list)
    projects: list[dict[str, Any]] = field(default_factory=list)
    education: list[dict[str, Any]] = field(default_factory=list)
    raw_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "years_exp": self.years_exp,
            "skills": list(self.skills),
            "experiences": [dict(e) for e in self.experiences],
            "projects": [dict(p) for p in self.projects],
            "education": [dict(e) for e in self.education],
            "raw_hash": self.raw_hash,
        }


@dataclass(frozen=True, slots=True)
class JobResume:
    """简历领域文档。"""

    raw_text: str
    raw_hash: str
    updated_at: str
    parsed: ResumeParsed | None = None

    @property
    def parsed_is_stale(self) -> bool:
        """解析缓存是否陈旧 (raw_text 改过后尚未重跑 LLM 解析)。"""
        if self.parsed is None:
            return True
        return self.parsed.raw_hash != self.raw_hash

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_text": self.raw_text,
            "raw_hash": self.raw_hash,
            "updated_at": self.updated_at,
            "parsed": self.parsed.to_dict() if self.parsed else None,
            "parsed_is_stale": self.parsed_is_stale,
        }


# ─────────────────────────────────────────────────────────────
# Snapshot
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class JobMemorySnapshot:
    """渲染给 Brain / matcher / greeter / replier 的统一视图。

    - 写入路径: Brain 调 ``job.memory.record`` / ``job.hard_constraint.set`` /
      ``job.resume.update`` intent tool → JobMemory
    - 读取路径: 进 ReAct 循环前 ``to_prompt_section()`` 注入 system prompt;
      mutates 后重取刷新
    """

    workspace_id: str
    hard_constraints: HardConstraints = field(default_factory=HardConstraints)
    memory_items: list[MemoryItem] = field(default_factory=list)
    resume: JobResume | None = None
    user_facts: dict[str, Any] = field(default_factory=dict)  # from CoreMemory
    snapshot_version: str = ""
    rendered_at: str = ""

    # ── downstream filter helpers ──
    def active_items(self) -> list[MemoryItem]:
        return [it for it in self.memory_items if it.is_active]

    def active_items_by_type(self) -> dict[str, list[MemoryItem]]:
        buckets: dict[str, list[MemoryItem]] = {}
        for it in self.active_items():
            buckets.setdefault(it.type, []).append(it)
        return buckets

    def avoid_company_names(self) -> list[str]:
        """提取当前 avoid_company items 的 target 列表 (给 connector 粗筛用)。"""
        return [it.target for it in self.active_items()
                if it.type == "avoid_company" and it.target]

    def avoid_traits(self) -> list[str]:
        """提取 avoid_trait items 的 target / content (给 LLM 组件参考)。"""
        out: list[str] = []
        for it in self.active_items():
            if it.type == "avoid_trait":
                out.append(it.target or it.content)
        return [s for s in out if s]

    def is_company_avoided(self, name: str) -> tuple[bool, str | None]:
        """大小写不敏感匹配当前 avoid_company items, 返回 (avoided, reason_or_None)。

        替代旧 ``JobMemory.is_company_blocked`` 的语义 — 现在 reason 来自 item.content。

        刻意**只做字面公司名匹配**: 集体名词级偏好 (如 "大厂") 不进这一层,
        而是在 ``JobSnapshotMatcher`` LLM 阶段由 LLM 用世界知识展开判定.
        硬编码大厂列表是反 Agent 范式的启发式补丁 — 见 matcher.py 的
        Verdict policy 注释。
        """
        needle = _normalize(name).casefold()
        if not needle:
            return (False, None)
        for it in self.active_items():
            if it.type != "avoid_company":
                continue
            if not it.target:
                continue
            if it.target.casefold() == needle:
                return (True, it.content or None)
        return (False, None)

    def find_avoided_target_in(
        self,
        text: str,
        *,
        types: tuple[str, ...] = ("avoid_trait", "avoid_keyword"),
    ) -> tuple[bool, str | None]:
        """在 text 中查找任一 avoid_* item 的 target 作为子串 (大小写不敏感)。

        替代旧 ``JobMemory.text_contains_blocked_keyword``。默认扫
        avoid_trait 与 avoid_keyword 两类; 需要扩展时传入 types。
        """
        haystack = _normalize(text).casefold()
        if not haystack:
            return (False, None)
        for it in self.active_items():
            if it.type not in types:
                continue
            target = it.target or ""
            if target and target.casefold() in haystack:
                return (True, it.target)
        return (False, None)

    def hc_preferred_locations(self) -> list[str]:
        """直接返回 hard_constraints.preferred_location (方便 matcher 降级路径)。"""
        return list(self.hard_constraints.preferred_location)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "hard_constraints": self.hard_constraints.to_dict(),
            "memory_items": [it.to_dict() for it in self.memory_items],
            "resume": self.resume.to_dict() if self.resume else None,
            "user_facts": dict(self.user_facts),
            "snapshot_version": self.snapshot_version,
            "rendered_at": self.rendered_at,
        }

    def to_prompt_section(self) -> str:
        """渲染为 markdown 片段, 注入 system prompt。

        按文档 §4.1 的结构分三节; 仅渲染 **active** Memory Items,
        已过期/被取代的不进 prompt (审计时通过 list tool 查全量)。
        """
        parts: list[str] = [f"## Job Snapshot (workspace: `{self.workspace_id}`)"]

        # ── Hard Constraints ──
        parts.append("### Hard Constraints")
        hc = self.hard_constraints
        if hc.is_empty():
            parts.append("- (none — 用户尚未设置任何硬约束, 如有需要先问用户)")
        else:
            if hc.preferred_location:
                parts.append(f"- preferred_location: {hc.preferred_location}")
            if hc.salary_floor_monthly is not None:
                spec_tail = ""
                if hc.salary_floor_spec:
                    spec_tail = (
                        f" (原始: "
                        f"{hc.salary_floor_spec.get('amount')} "
                        f"{hc.salary_floor_spec.get('unit')}"
                        f"/{hc.salary_floor_spec.get('period')}"
                    )
                    wd = hc.salary_floor_spec.get("work_days_per_month")
                    if wd is not None:
                        spec_tail += f" × {wd}工作日"
                    spec_tail += ")"
                parts.append(
                    f"- salary_floor_monthly: {hc.salary_floor_monthly} K/月{spec_tail}"
                )
            if hc.target_roles:
                parts.append(f"- target_roles: {hc.target_roles}")
            if hc.experience_level:
                parts.append(f"- experience_level: {hc.experience_level}")

        # ── Memory Items (按 type 分组) ──
        buckets = self.active_items_by_type()
        if not buckets:
            parts.append("### Memory Items\n- (none)")
        else:
            soft_limit = 30
            type_order = list(MEMORY_ITEM_TYPES) + sorted(
                [t for t in buckets.keys() if t not in MEMORY_ITEM_TYPES]
            )
            for type_name in type_order:
                items = buckets.get(type_name)
                if not items:
                    continue
                parts.append(f"### Memory Items — {type_name}")
                shown = items[:soft_limit]
                for it in shown:
                    tail = ""
                    if it.valid_until:
                        tail = f" — valid until {it.valid_until[:10]}"
                    target_pref = f"[{it.target}] " if it.target else ""
                    parts.append(f"- {target_pref}{it.content}{tail}")
                if len(items) > len(shown):
                    parts.append(
                        f"- (共 {len(items)} 条 '{type_name}' items, "
                        f"已省略 {len(items) - len(shown)} 条, "
                        f"必要时调 `job.memory.list` 查全量)"
                    )

        # ── Resume ──
        if self.resume is None:
            parts.append("### Resume\n- (none — 用户尚未提供简历, 无法做能力匹配)")
        else:
            parts.append("### Resume (summary)")
            if self.resume.parsed_is_stale or self.resume.parsed is None:
                head = (self.resume.raw_text or "")[:200]
                parts.append(f"- (解析中) 原文前 200 字: {head}")
            else:
                p = self.resume.parsed
                if p.summary:
                    parts.append(f"- summary: {p.summary}")
                if p.years_exp is not None:
                    parts.append(f"- years_exp: {p.years_exp}")
                if p.skills:
                    parts.append(f"- skills: {p.skills}")
                if p.experiences:
                    top = p.experiences[:3]
                    parts.append(
                        "- recent_experiences: "
                        + "; ".join(
                            f"{e.get('company', '?')}·{e.get('role', '?')}"
                            for e in top
                        )
                    )
            parts.append(
                "> 完整简历可调 `job.resume.get` 获取; "
                "不要编造简历中没有的经历。"
            )

        # ── User-level Facts (from CoreMemory) ──
        if self.user_facts:
            parts.append("### User-level Facts")
            for k in sorted(self.user_facts.keys()):
                v = self.user_facts[k]
                if v in (None, "", [], {}):
                    continue
                parts.append(f"- {k}: {v}")

        parts.append(
            "> 以上是用户**当前**偏好; 若用户在对话中表达了新偏好或撤销某项, "
            "请立即调用对应 intent tool (`job.memory.record` / `job.memory.retire` / "
            "`job.memory.supersede` / `job.hard_constraint.set` / `job.resume.update`) "
            "持久化。"
        )
        return "\n".join(parts)


# ─────────────────────────────────────────────────────────────
# JobMemory facade
# ─────────────────────────────────────────────────────────────


class JobMemory:
    """Job 域的 WorkspaceMemory facade (v2 三类存储)。

    本类不持有存储; 所有 IO 都委托 ``WorkspaceMemory``。按 task run
    实例化一次即可。
    """

    def __init__(
        self,
        *,
        workspace_memory: WorkspaceMemory,
        workspace_id: str,
        core_memory: CoreMemory | None = None,
        source: str = _DEFAULT_SOURCE,
    ) -> None:
        clean_ws = _normalize(workspace_id)
        if not clean_ws:
            raise ValueError("workspace_id must be a non-empty string")
        self._ws = workspace_memory
        self._workspace_id = clean_ws
        self._core = core_memory
        self._source = source or _DEFAULT_SOURCE

    @classmethod
    def from_engine(
        cls,
        engine: DatabaseEngine,
        *,
        workspace_id: str,
        core_memory: CoreMemory | None = None,
        source: str = _DEFAULT_SOURCE,
    ) -> JobMemory:
        return cls(
            workspace_memory=WorkspaceMemory(db_engine=engine),
            workspace_id=workspace_id,
            core_memory=core_memory,
            source=source,
        )

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    # ─── Hard Constraints ────────────────────────────────────

    def set_hard_constraint(self, field_name: str, value: Any) -> None:
        """设置一个 Hard Constraint 字段。字段必须在 HARD_CONSTRAINT_FIELDS 白名单内。"""
        name = _normalize(field_name)
        if name not in HARD_CONSTRAINT_FIELDS:
            raise ValueError(
                f"hard constraint field must be one of {HARD_CONSTRAINT_FIELDS}; "
                f"got {field_name!r}. 其他字段请走 job.memory.record (Memory Items)"
            )
        normalized = self._normalize_hc_value(name, value)
        key = f"{_KEY_HC_PREFIX}{name}"
        if name == "salary_floor_monthly":
            normalized = self._preserve_salary_spec_storage(
                key=key,
                incoming=normalized,
            )
        self._ws.set_fact(self._workspace_id, key, normalized, source=self._source)

    def unset_hard_constraint(self, field_name: str) -> bool:
        name = _normalize(field_name)
        if name not in HARD_CONSTRAINT_FIELDS:
            return False
        key = f"{_KEY_HC_PREFIX}{name}"
        existed = self._ws.get_fact(self._workspace_id, key) is not None
        self._ws.delete_fact(self._workspace_id, key)
        return existed

    def get_hard_constraints(self) -> HardConstraints:
        facts = self._ws.list_facts_by_prefix(self._workspace_id, _KEY_HC_PREFIX)
        data: dict[str, Any] = {}
        for f in facts:
            sub = f.key[len(_KEY_HC_PREFIX):]
            if sub in HARD_CONSTRAINT_FIELDS:
                data[sub] = f.value
        salary_k, salary_spec = self._decode_salary_storage(
            data.get("salary_floor_monthly")
        )
        return HardConstraints(
            preferred_location=list(data.get("preferred_location") or []),
            salary_floor_monthly=salary_k,
            salary_floor_spec=salary_spec,
            target_roles=list(data.get("target_roles") or []),
            experience_level=_normalize(data.get("experience_level")) or None,
        )

    # ─── Memory Items ────────────────────────────────────────

    def record_item(self, item: dict[str, Any] | MemoryItem) -> MemoryItem:
        """追加一条 Memory Item (幂等)。

        - 若传入 dict 缺 id / 时间戳, 本方法补齐; 若显式 id 已存在则抛错。
        - 若 type 不在推荐 enum 中, 允许写入 (仅记 debug 日志)。
        - **语义级去重** (P1-C, audit trace_f3bda835ed94):
          如果当前 workspace 里已经有一条 ``(type, target, content)`` 相同且仍
          ``is_active`` 的 MemoryItem, 直接返回那条 item, 不产生新的 fact row;
          retired/superseded 的旧条目不阻止新写入, 保留重新登记的可能性。
          避免 Brain 在一轮对话里反复调 ``job.memory.record`` 时把同一偏好
          写成 N 条伪增量 (prompt 预算 + memory 审计都会被膨胀).
        """
        normalized = self._normalize_item_for_write(item)
        duplicate = self._find_active_duplicate(normalized)
        if duplicate is not None:
            logger.debug(
                "JobMemory: dedup hit on (type=%s, target=%s, content=%s...); "
                "returning existing id=%s",
                normalized.type,
                normalized.target,
                normalized.content[:40],
                duplicate.id,
            )
            return duplicate
        key = f"{_KEY_ITEM_PREFIX}{normalized.id}"
        existing = self._ws.get_fact(self._workspace_id, key)
        if existing is not None:
            raise ValueError(
                f"Memory Item id conflict: {normalized.id}; use supersede_item instead"
            )
        self._ws.set_fact(
            self._workspace_id, key, normalized.to_dict(), source=self._source
        )
        if normalized.type not in MEMORY_ITEM_TYPES:
            logger.debug(
                "JobMemory: recorded item with non-enum type=%r (id=%s); "
                "downstream will render as-is.",
                normalized.type, normalized.id,
            )
        return normalized

    def _find_active_duplicate(self, candidate: MemoryItem) -> MemoryItem | None:
        """返回与 candidate 在 (type, target, content) 上相同的 **active** item,
        否则 None. retired/superseded 的同值条目不算 duplicate, 允许重新登记."""
        needle_target = candidate.target or ""
        needle_content = candidate.content
        for existing in self.list_items(type=candidate.type, include_expired=False):
            if (existing.target or "") != needle_target:
                continue
            if existing.content != needle_content:
                continue
            return existing
        return None

    def record_items(
        self, items: list[dict[str, Any] | MemoryItem]
    ) -> list[MemoryItem]:
        """批量追加 (例如一句话同时产生 event + preference)。"""
        return [self.record_item(it) for it in items]

    def retire_item(self, item_id: str) -> bool:
        """把 item 的 valid_until 强制设为 now, 标记过期 (保留审计痕迹)。"""
        clean_id = _normalize(item_id)
        if not clean_id:
            return False
        key = f"{_KEY_ITEM_PREFIX}{clean_id}"
        raw = self._ws.get_fact(self._workspace_id, key)
        if not isinstance(raw, dict):
            return False
        raw["valid_until"] = _now_iso()
        self._ws.set_fact(self._workspace_id, key, raw, source=self._source)
        return True

    def supersede_item(
        self,
        old_id: str,
        new_item: dict[str, Any] | MemoryItem,
    ) -> MemoryItem:
        """用 new_item 取代 old_id, 互相建立链接。"""
        clean_old = _normalize(old_id)
        if not clean_old:
            raise ValueError("old_id must be non-empty")
        old_key = f"{_KEY_ITEM_PREFIX}{clean_old}"
        old_raw = self._ws.get_fact(self._workspace_id, old_key)
        if not isinstance(old_raw, dict):
            raise ValueError(f"Memory Item {clean_old!r} not found")

        new_normalized = self._normalize_item_for_write(new_item)
        new_key = f"{_KEY_ITEM_PREFIX}{new_normalized.id}"
        self._ws.set_fact(
            self._workspace_id, new_key, new_normalized.to_dict(), source=self._source
        )
        # 反向链接: 旧 item 的 superseded_by 指向新 id
        old_raw["superseded_by"] = new_normalized.id
        self._ws.set_fact(self._workspace_id, old_key, old_raw, source=self._source)
        return new_normalized

    def list_items(
        self,
        *,
        type: str | None = None,
        target: str | None = None,
        include_expired: bool = False,
    ) -> list[MemoryItem]:
        """查询 Memory Items, 按 created_at 倒序返回。"""
        facts = self._ws.list_facts_by_prefix(self._workspace_id, _KEY_ITEM_PREFIX)
        out: list[MemoryItem] = []
        type_needle = _normalize(type) or None
        target_needle = _normalize(target) or None
        for f in facts:
            it = self._decode_item(f.key, f.value)
            if it is None:
                continue
            if type_needle and it.type != type_needle:
                continue
            if target_needle and (it.target or "") != target_needle:
                continue
            if not include_expired and not it.is_active:
                continue
            out.append(it)
        out.sort(key=lambda it: it.created_at, reverse=True)
        return out

    # ─── Derived caches ──────────────────────────────────────

    def get_trait_company_set(
        self,
        *,
        trait_type: str,
        trait: str,
    ) -> TraitCompanySet | None:
        """读取一个 trait→公司集合缓存。不存在或损坏时返回 ``None``。"""
        key = self._trait_company_set_key(trait_type=trait_type, trait=trait)
        raw = self._ws.get_fact(self._workspace_id, key)
        return self._decode_trait_company_set(raw)

    def set_trait_company_set(
        self,
        *,
        trait_type: str,
        trait: str,
        companies: list[str],
        model: str,
        updated_at: str,
        expires_at: str,
    ) -> TraitCompanySet:
        """写入 trait→公司集合缓存 (upsert)。"""
        clean_trait_type = _normalize(trait_type)
        if clean_trait_type not in ("avoid_trait", "favor_trait"):
            raise ValueError(
                f"trait_type must be avoid_trait|favor_trait, got {trait_type!r}"
            )
        clean_trait = _normalize(trait)
        if not clean_trait:
            raise ValueError("trait must be non-empty")
        clean_model = _normalize(model) or "unknown"
        clean_updated_at = _normalize(updated_at)
        clean_expires_at = _normalize(expires_at)
        if not clean_updated_at:
            raise ValueError("updated_at must be non-empty ISO string")
        if not clean_expires_at:
            raise ValueError("expires_at must be non-empty ISO string")
        # 只做字面去重, 不做 alias/语义归并; 语义展开是 LLM 的职责。
        seen: set[str] = set()
        normalized_companies: list[str] = []
        for name in companies:
            text = _normalize(name)
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized_companies.append(text)
        record = TraitCompanySet(
            trait_type=clean_trait_type,
            trait=clean_trait,
            companies=normalized_companies,
            model=clean_model,
            updated_at=clean_updated_at,
            expires_at=clean_expires_at,
        )
        key = self._trait_company_set_key(
            trait_type=clean_trait_type, trait=clean_trait,
        )
        self._ws.set_fact(
            self._workspace_id,
            key,
            record.to_dict(),
            source=self._source,
        )
        return record

    # ─── Resume (Domain Document) ─────────────────────────────

    def get_resume(self) -> JobResume | None:
        raw = self._ws.get_fact(self._workspace_id, _KEY_DOC_RESUME)
        if not isinstance(raw, dict):
            return None
        raw_text = _normalize(raw.get("raw_text"))
        if not raw_text:
            return None
        parsed_raw = self._ws.get_fact(self._workspace_id, _KEY_DOC_RESUME_PARSED)
        parsed = self._decode_parsed(parsed_raw)
        return JobResume(
            raw_text=raw_text,
            raw_hash=_normalize(raw.get("raw_hash")) or _hash_text(raw_text),
            updated_at=_normalize(raw.get("updated_at")),
            parsed=parsed,
        )

    def update_resume(self, raw_text: str) -> JobResume:
        """整体替换简历原文。返回新 JobResume (parsed 字段暂保持旧值但会被标记 stale)。

        注意: 本方法**只写 raw**; 解析异步触发 (由 ProfileModule 的 hook
        或专门的 background task 调用 ``set_resume_parsed``)。
        """
        clean = _normalize(raw_text)
        if not clean:
            raise ValueError("resume raw_text must be non-empty")
        new_hash = _hash_text(clean)
        payload = {
            "raw_text": clean,
            "raw_hash": new_hash,
            "updated_at": _now_iso(),
        }
        self._ws.set_fact(self._workspace_id, _KEY_DOC_RESUME, payload, source=self._source)
        # 返回新 JobResume; parsed 仍用旧值 (若 hash 不同会自动 stale)
        parsed_raw = self._ws.get_fact(self._workspace_id, _KEY_DOC_RESUME_PARSED)
        parsed = self._decode_parsed(parsed_raw)
        return JobResume(
            raw_text=clean,
            raw_hash=new_hash,
            updated_at=payload["updated_at"],
            parsed=parsed,
        )

    def set_resume_parsed(self, parsed: ResumeParsed | dict[str, Any]) -> None:
        """写入 LLM 解析结果。由异步解析管道调用。

        parsed.raw_hash 必须与当前 raw_text 一致; 否则忽略 (解析已过期)。
        """
        if isinstance(parsed, dict):
            parsed = ResumeParsed(
                summary=_normalize(parsed.get("summary")),
                years_exp=self._as_int_or_none(parsed.get("years_exp")),
                skills=list(parsed.get("skills") or []),
                experiences=list(parsed.get("experiences") or []),
                projects=list(parsed.get("projects") or []),
                education=list(parsed.get("education") or []),
                raw_hash=_normalize(parsed.get("raw_hash")),
            )
        raw = self._ws.get_fact(self._workspace_id, _KEY_DOC_RESUME)
        current_hash = ""
        if isinstance(raw, dict):
            current_hash = _normalize(raw.get("raw_hash"))
        if current_hash and parsed.raw_hash and parsed.raw_hash != current_hash:
            logger.info(
                "JobMemory: discard stale resume.parsed (parsed.raw_hash=%s, current=%s)",
                parsed.raw_hash, current_hash,
            )
            return
        self._ws.set_fact(
            self._workspace_id, _KEY_DOC_RESUME_PARSED, parsed.to_dict(),
            source=self._source,
        )

    def patch_resume_parsed(self, patch: dict[str, Any]) -> ResumeParsed | None:
        """对 parsed 的少数字段打补丁 (如补一条 project)。

        不重跑 LLM 解析; 仅覆盖指定顶层字段。
        """
        raw = self._ws.get_fact(self._workspace_id, _KEY_DOC_RESUME_PARSED)
        current = self._decode_parsed(raw) or ResumeParsed()
        merged = current.to_dict()
        merged.update({k: v for k, v in patch.items() if k in merged})
        new_parsed = ResumeParsed(
            summary=_normalize(merged.get("summary")),
            years_exp=self._as_int_or_none(merged.get("years_exp")),
            skills=list(merged.get("skills") or []),
            experiences=list(merged.get("experiences") or []),
            projects=list(merged.get("projects") or []),
            education=list(merged.get("education") or []),
            raw_hash=_normalize(merged.get("raw_hash")),
        )
        self._ws.set_fact(
            self._workspace_id, _KEY_DOC_RESUME_PARSED, new_parsed.to_dict(),
            source=self._source,
        )
        return new_parsed

    # ─── Snapshot ────────────────────────────────────────────

    def snapshot(self) -> JobMemorySnapshot:
        """构造一次完整 snapshot (一次扫 job.* 前缀, 按子前缀分桶)。"""
        facts = self._ws.list_facts_by_prefix(self._workspace_id, _KEY_JOB_PREFIX)
        hc_data: dict[str, Any] = {}
        items: list[MemoryItem] = []
        resume_raw: dict[str, Any] | None = None
        resume_parsed: ResumeParsed | None = None
        latest_updated_at = ""

        for f in facts:
            if f.updated_at > latest_updated_at:
                latest_updated_at = f.updated_at
            key = f.key
            if key.startswith(_KEY_HC_PREFIX):
                sub = key[len(_KEY_HC_PREFIX):]
                if sub in HARD_CONSTRAINT_FIELDS:
                    hc_data[sub] = f.value
            elif key.startswith(_KEY_ITEM_PREFIX):
                it = self._decode_item(key, f.value)
                if it is not None:
                    items.append(it)
            elif key == _KEY_DOC_RESUME:
                resume_raw = f.value if isinstance(f.value, dict) else None
            elif key == _KEY_DOC_RESUME_PARSED:
                resume_parsed = self._decode_parsed(f.value)

        salary_k, salary_spec = self._decode_salary_storage(
            hc_data.get("salary_floor_monthly")
        )
        hc = HardConstraints(
            preferred_location=list(hc_data.get("preferred_location") or []),
            salary_floor_monthly=salary_k,
            salary_floor_spec=salary_spec,
            target_roles=list(hc_data.get("target_roles") or []),
            experience_level=_normalize(hc_data.get("experience_level")) or None,
        )

        resume: JobResume | None = None
        if resume_raw and _normalize(resume_raw.get("raw_text")):
            raw_text = _normalize(resume_raw.get("raw_text"))
            resume = JobResume(
                raw_text=raw_text,
                raw_hash=_normalize(resume_raw.get("raw_hash")) or _hash_text(raw_text),
                updated_at=_normalize(resume_raw.get("updated_at")),
                parsed=resume_parsed,
            )

        items.sort(key=lambda it: it.created_at, reverse=True)
        user_facts = self._load_user_facts()
        snapshot_version = f"{self._workspace_id}:{latest_updated_at or 'empty'}"

        return JobMemorySnapshot(
            workspace_id=self._workspace_id,
            hard_constraints=hc,
            memory_items=items,
            resume=resume,
            user_facts=user_facts,
            snapshot_version=snapshot_version,
            rendered_at=_now_iso(),
        )

    # ─── Maintenance ─────────────────────────────────────────

    def clear_all(self) -> int:
        """开发期: 清除本 workspace 下所有 job.* 记忆, 返回删除条数。

        生产环境不建议调用 (不可逆); 专供 wipe 脚本 / 集成测试 fixture 使用。
        """
        return self._ws.delete_facts_by_prefix(self._workspace_id, _KEY_JOB_PREFIX)

    # ─── Internal helpers ───────────────────────────────────

    @staticmethod
    def _as_int_or_none(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    # ── salary_floor_monthly 量纲换算 (P2-B) ───────────────────
    # Agentic 合同: LLM 输出 ``{amount, unit, period[, work_days_per_month]}``,
    # 我们做确定性换算到 "整数 K/月" 并**同时**保留原始三要素;
    # 合同外 (bare int) 兼容旧数据, 但 spec=None 就没法回放用户原话.
    _SALARY_UNIT_TO_YUAN: dict[str, float] = {
        "yuan": 1.0,
        "k_yuan": 1000.0,
        "w_yuan": 10000.0,
    }
    _SALARY_PERIODS: tuple[str, ...] = ("day", "month", "year")
    _SALARY_DEFAULT_WORK_DAYS_PER_MONTH: int = 22  # 21.67 ≈ 22

    @staticmethod
    def _normalize_hc_value(name: str, value: Any) -> Any:
        """对 Hard Constraint 字段做类型归一化 (宽松输入、强制输出)。"""
        if name in ("preferred_location", "target_roles"):
            if value is None:
                return []
            if isinstance(value, str):
                parts = [p.strip() for p in value.split(",")]
                return [p for p in parts if p]
            if isinstance(value, (list, tuple)):
                return [str(v).strip() for v in value if _normalize(v)]
            raise ValueError(
                f"hard constraint {name!r} expects list[str], got {type(value).__name__}"
            )
        if name == "salary_floor_monthly":
            return JobMemory._normalize_salary_floor_value(value)
        if name == "experience_level":
            return _normalize(value)
        return value

    def _preserve_salary_spec_storage(self, *, key: str, incoming: Any) -> Any:
        """避免把结构化薪资 spec 意外降级成裸整数。

        场景:
          - pre-turn dispatcher 已写入 ``{"value_monthly_k": 7, "source": 300元/天}``
          - 同一轮里 planner 又发 ``job.hard_constraint.set(value=7)``

        语义上两者等价, 但后者会丢失 source, 触发后续审计/回复漂移。
        策略: 仅当 ``incoming`` 为裸 int 且与现存 structured monthly_k 完全相等
        时, 复用现存 structured 记录; 其他情况照常覆盖。
        """
        if not isinstance(incoming, int) or isinstance(incoming, bool):
            return incoming
        existing_raw = self._ws.get_fact(self._workspace_id, key)
        if not isinstance(existing_raw, dict):
            return incoming
        existing_k, existing_spec = self._decode_salary_storage(existing_raw)
        if existing_k is None or existing_spec is None:
            return incoming
        if incoming != existing_k:
            return incoming
        return existing_raw

    @classmethod
    def _normalize_salary_floor_value(cls, value: Any) -> Any:
        """把 salary_floor_monthly 的输入归一化为存储形态。

        允许两种形态:
          * 结构化 spec ``{amount, unit, period[, work_days_per_month]}`` →
            确定性换算成 K/月 并打包为 ``{"value_monthly_k": int, "source": {...}}``,
            让 ``get_hard_constraints`` 能在读取时回放用户原始量纲 (P2-B).
          * bare int / numeric string → 直接当 K/月, 返回 int (向后兼容旧数据);
            此路径下没有原始 source 可以回放, 调用方应尽量走结构化 spec。
        ``None`` / 空串 → ``None`` (未设置).
        非法输入一律 ValueError, 不静默降级。
        """
        if value is None or value == "":
            return None
        if isinstance(value, dict):
            return cls._salary_spec_to_storage(value)
        if isinstance(value, bool):  # bool 是 int 的子类, 要提前拦
            raise ValueError(
                f"salary_floor_monthly must be int or structured spec, got bool {value!r}"
            )
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"salary_floor_monthly must be int or structured spec, got {value!r}"
            ) from exc

    @classmethod
    def _salary_spec_to_storage(cls, spec: dict[str, Any]) -> dict[str, Any]:
        """把 ``{amount, unit, period[, work_days_per_month]}`` → 存储 dict。

        存储形态:
            {"value_monthly_k": int, "source": {amount, unit, period[, work_days_per_month]}}

        ``source`` 里的 amount 一律归一成 float, unit/period 归一成 lower-case 字符串,
        work_days_per_month 仅在 ``period == "day"`` 时保留 (默认 22)。
        换算失败 / 字段非法 → ``ValueError`` (fail-loud, 永不静默降级).
        """
        amount_raw = spec.get("amount")
        if amount_raw is None:
            raise ValueError("salary spec missing 'amount'")
        try:
            amount = float(amount_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"salary spec 'amount' must be numeric, got {amount_raw!r}"
            ) from exc
        if amount <= 0:
            raise ValueError(f"salary spec 'amount' must be > 0, got {amount_raw!r}")

        unit = _normalize(spec.get("unit")).lower()
        unit_scale = cls._SALARY_UNIT_TO_YUAN.get(unit)
        if unit_scale is None:
            raise ValueError(
                f"salary spec 'unit' must be one of {sorted(cls._SALARY_UNIT_TO_YUAN)}, "
                f"got {spec.get('unit')!r}"
            )

        period = _normalize(spec.get("period")).lower()
        if period not in cls._SALARY_PERIODS:
            raise ValueError(
                f"salary spec 'period' must be one of {list(cls._SALARY_PERIODS)}, "
                f"got {spec.get('period')!r}"
            )

        source: dict[str, Any] = {
            "amount": amount,
            "unit": unit,
            "period": period,
        }
        if period == "day":
            wd_raw = spec.get("work_days_per_month")
            wd = None
            if wd_raw is not None:
                try:
                    wd = int(wd_raw)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"salary spec 'work_days_per_month' must be int, got {wd_raw!r}"
                    ) from exc
                if wd <= 0:
                    raise ValueError(
                        f"salary spec 'work_days_per_month' must be > 0, got {wd_raw!r}"
                    )
            if wd is None:
                wd = cls._SALARY_DEFAULT_WORK_DAYS_PER_MONTH
            source["work_days_per_month"] = wd
            period_factor = float(wd)
        elif period == "month":
            period_factor = 1.0
        else:  # year
            period_factor = 1.0 / 12.0

        monthly_yuan = amount * unit_scale * period_factor
        monthly_k = int(round(monthly_yuan / 1000.0))
        if monthly_k <= 0:
            raise ValueError(
                f"salary spec converts to non-positive monthly K "
                f"(amount={amount} unit={unit} period={period})"
            )
        return {"value_monthly_k": monthly_k, "source": source}

    @classmethod
    def _decode_salary_storage(
        cls, raw: Any
    ) -> tuple[int | None, dict[str, Any] | None]:
        """从存储 fact.value 里拆出 (monthly_k, raw_spec)。

        兼容两种形态:
          * 新 (结构化): {"value_monthly_k": int, "source": {...}} → (int, source)
          * 旧 (裸 int/str): 直接可转 int → (int, None)
          * 其他 / None → (None, None)
        """
        if raw is None or raw == "":
            return None, None
        if isinstance(raw, dict):
            mk = cls._as_int_or_none(raw.get("value_monthly_k"))
            src = raw.get("source")
            if not isinstance(src, dict):
                src = None
            return mk, src
        return cls._as_int_or_none(raw), None

    @staticmethod
    def _trait_company_set_key(*, trait_type: str, trait: str) -> str:
        clean_type = _normalize(trait_type)
        clean_trait = _normalize(trait).casefold()
        digest = hashlib.sha1(
            f"{clean_type}:{clean_trait}".encode("utf-8")
        ).hexdigest()
        return f"{_KEY_DERIVED_TRAIT_COMPANY_PREFIX}{digest}"

    @staticmethod
    def _decode_trait_company_set(value: Any) -> TraitCompanySet | None:
        if not isinstance(value, dict):
            return None
        trait_type = _normalize(value.get("trait_type"))
        trait = _normalize(value.get("trait"))
        updated_at = _normalize(value.get("updated_at"))
        expires_at = _normalize(value.get("expires_at"))
        if trait_type not in ("avoid_trait", "favor_trait"):
            return None
        if not trait or not updated_at or not expires_at:
            return None
        companies_raw = value.get("companies")
        if not isinstance(companies_raw, list):
            return None
        companies = []
        for item in companies_raw:
            text = _normalize(item)
            if text:
                companies.append(text)
        return TraitCompanySet(
            trait_type=trait_type,
            trait=trait,
            companies=companies,
            model=_normalize(value.get("model")) or "unknown",
            updated_at=updated_at,
            expires_at=expires_at,
        )

    def _normalize_item_for_write(
        self, item: dict[str, Any] | MemoryItem
    ) -> MemoryItem:
        """把 dict 或部分填充的 MemoryItem 归一化成完整可写的 MemoryItem。"""
        if isinstance(item, MemoryItem):
            data = item.to_dict()
        elif isinstance(item, dict):
            data = dict(item)
        else:
            raise TypeError(f"item must be dict or MemoryItem, got {type(item).__name__}")

        now = _now_iso()
        item_id = _normalize(data.get("id")) or uuid.uuid4().hex
        type_name = _normalize(data.get("type")) or "other"
        target = _normalize(data.get("target")) or None
        content = _normalize(data.get("content"))
        if not content:
            raise ValueError("MemoryItem.content must be non-empty")
        raw_text = _normalize(data.get("raw_text")) or content
        valid_from = _normalize(data.get("valid_from")) or now
        valid_until = _normalize(data.get("valid_until")) or None
        superseded_by = _normalize(data.get("superseded_by")) or None
        created_at = _normalize(data.get("created_at")) or now
        return MemoryItem(
            id=item_id,
            type=type_name,
            target=target,
            content=content,
            raw_text=raw_text,
            valid_from=valid_from,
            valid_until=valid_until,
            superseded_by=superseded_by,
            created_at=created_at,
        )

    @staticmethod
    def _decode_item(key: str, value: Any) -> MemoryItem | None:
        if not isinstance(value, dict):
            return None
        try:
            item_id = _normalize(value.get("id")) or key[len(_KEY_ITEM_PREFIX):]
            content = _normalize(value.get("content"))
            if not content:
                return None
            return MemoryItem(
                id=item_id,
                type=_normalize(value.get("type")) or "other",
                target=_normalize(value.get("target")) or None,
                content=content,
                raw_text=_normalize(value.get("raw_text")) or content,
                valid_from=_normalize(value.get("valid_from")),
                valid_until=_normalize(value.get("valid_until")) or None,
                superseded_by=_normalize(value.get("superseded_by")) or None,
                created_at=_normalize(value.get("created_at")),
            )
        except Exception as exc:   # noqa: BLE001
            logger.warning("JobMemory: skip malformed item %s (%s)", key, exc)
            return None

    @staticmethod
    def _decode_parsed(value: Any) -> ResumeParsed | None:
        if not isinstance(value, dict):
            return None
        return ResumeParsed(
            summary=_normalize(value.get("summary")),
            years_exp=JobMemory._as_int_or_none(value.get("years_exp")),
            skills=list(value.get("skills") or []),
            experiences=list(value.get("experiences") or []),
            projects=list(value.get("projects") or []),
            education=list(value.get("education") or []),
            raw_hash=_normalize(value.get("raw_hash")),
        )

    def _load_user_facts(self) -> dict[str, Any]:
        """从 CoreMemory 拉 user-level facts (姓名 / 通用沟通风格等)。

        CoreMemory 不可用或返回异常时降级为空 dict, 不阻塞 Job 域。
        """
        if self._core is None:
            return {}
        try:
            # CoreMemory 当前公开接口是 ``snapshot()``; 老版本曾叫 read_snapshot。
            reader = getattr(self._core, "snapshot", None)
            if not callable(reader):
                reader = getattr(self._core, "read_snapshot", None)
            if not callable(reader):
                raise AttributeError("core memory has neither snapshot() nor read_snapshot()")
            snap = reader()
        except Exception as exc:   # noqa: BLE001
            logger.debug("JobMemory: core memory snapshot unavailable (%s)", exc)
            return {}
        if not isinstance(snap, dict):
            return {}
        out: dict[str, Any] = {}
        user = snap.get("user") if isinstance(snap.get("user"), dict) else {}
        prefs = snap.get("prefs") if isinstance(snap.get("prefs"), dict) else {}
        for k, v in dict(user).items():
            if v in (None, "", [], {}):
                continue
            out[f"user.{k}"] = v
        for k, v in dict(prefs).items():
            if v in (None, "", [], {}):
                continue
            out[f"pref.{k}"] = v
        return out


__all__ = [
    "JobMemory",
    "JobMemorySnapshot",
    "MemoryItem",
    "TraitCompanySet",
    "HardConstraints",
    "JobResume",
    "ResumeParsed",
    "HARD_CONSTRAINT_FIELDS",
    "MEMORY_ITEM_TYPES",
]
