"""偏好抽取器.

从用户一句话里抽出"可持久化的用户意图信号", 按**目标记忆层**分三类输出:

    ┌ core_prefs     → CoreMemory.prefs (跨业务域全局偏好, e.g. 称呼/语言/风格)
    ├ soul_updates   → Soul 人格/风格规则
    └ domain_prefs   → DomainMemory (业务域, e.g. JobMemory.hard_constraints /
                         memory_items). 由 ``DomainPreferenceDispatcher`` 按
                         domain × op 分派到具体 facade.

架构依据:
  - ``docs/Pulse-MemoryRuntime设计.md`` §Memory Layers
  - ``docs/Pulse-DomainMemory与Tool模式.md`` §2.2 边界 & §3.1-3.3 Job 存储分类

设计原则:
  * **extractor 只负责识别结构, 不负责 IO**. 任何 mutations 都交给
    Soul governance (core) / DomainPreferenceDispatcher (domain) 执行.
  * **LLM 一次产出全部分类**, 不要二次 LLM 调用再分流; 降低延迟也降低漂移.
  * **Regex fallback 保证 LLM 不可用时仍能工作**, 但只覆盖 CoreMemory 最常见字段
    (业务域偏好的 regex 成本太高, 由 LLM 兜不住时直接放弃 + 日志提示).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Domain preference 操作字典: ``domain → {op: schema_hint}``
# 扩展新 domain 时请同步 DomainPreferenceDispatcher / 对应 Applier。
_DOMAIN_OP_CATALOG: dict[str, dict[str, str]] = {
    "job": {
        "hard_constraint.set": (
            "args={field: preferred_location|salary_floor_monthly|target_roles|"
            "experience_level, value: ...}"
        ),
        "hard_constraint.unset": "args={field: ...}",
        "memory.record": (
            "args={item: {type: avoid_company|favor_company|avoid_trait|favor_trait|"
            "application_event|capability_claim|constraint_note|other, "
            "target: str|null, content: str, valid_until: iso|null}}"
        ),
    },
}


def _clean_value(text: str, *, max_len: int = 80) -> str:
    value = str(text or "").strip().strip("。！？!?，,；;:：")
    value = re.sub(r"\s+", " ", value)
    return value[:max_len].strip()


@dataclass(slots=True)
class DomainPref:
    """一条业务域偏好的结构化指令.

    由 extractor 识别, 由 ``DomainPreferenceDispatcher`` 派发给对应 domain facade 执行.

    fields:
      * ``domain`` – 业务域 slug(``job``/...), 与 Applier 注册 key 一致.
      * ``op`` – 操作名, 形如 ``hard_constraint.set`` / ``memory.record``.
      * ``args`` – 传给 Applier 的参数字典, 按 op 约定.
      * ``evidence`` – 原文证据片段, 方便审计."识别到什么 + 出自哪几个字".
      * ``confidence`` – 0.0-1.0, LLM 越不确定越低; dispatcher 可按阈值拒绝.
    """

    domain: str
    op: str
    args: dict[str, Any] = field(default_factory=dict)
    evidence: str = ""
    confidence: float = 0.8

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PreferenceExtraction:
    """抽取器输出的统一视图.

    * ``core_prefs``: 跨域通用偏好, 最终由 Soul governance 写 CoreMemory.prefs.
    * ``soul_updates``: 人格 / style rules, 由 Soul governance apply.
    * ``domain_prefs``: 业务域结构化意图, 由 DomainPreferenceDispatcher 派发.
    * ``evidences``: 粗粒度的命中线索(人读用).

    为兼容历史调用方, ``to_dict()`` 同时输出 ``prefs_updates`` 作为
    ``core_prefs`` 的别名; 新代码**请直接读 ``core_prefs``**.
    """

    core_prefs: dict[str, Any] = field(default_factory=dict)
    soul_updates: dict[str, Any] = field(default_factory=dict)
    domain_prefs: list[DomainPref] = field(default_factory=list)
    evidences: list[str] = field(default_factory=list)

    # 历史别名, 供旧调用方 (evolution.py 未迁移部分 / 外部消费者) 平滑读取.
    @property
    def prefs_updates(self) -> dict[str, Any]:
        return dict(self.core_prefs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "core_prefs": dict(self.core_prefs),
            "prefs_updates": dict(self.core_prefs),  # legacy alias
            "soul_updates": dict(self.soul_updates),
            "domain_prefs": [p.to_dict() for p in self.domain_prefs],
            "evidences": list(self.evidences),
        }


class PreferenceExtractor:
    """Extract preference/soul update signals from user text.

    Uses LLM when available for nuanced extraction (含 domain_prefs),
    falls back to regex patterns (仅 core 偏好) when LLM is not configured
    or失败.
    """

    _re_default_location = re.compile(
        r"(?:默认城市|默认用|默认使用|以后默认用|以后查天气用)\s*[:：]?\s*([^\s，。！？,!?]{1,20})",
        flags=re.IGNORECASE,
    )
    _re_dislike = re.compile(r"(?:我不喜欢|不要再推荐|以后不要推荐)\s*[:：]?\s*([^。！？!?]{1,80})")
    _re_like = re.compile(r"(?:我喜欢|我偏好|我更喜欢)\s*[:：]?\s*([^。！？!?]{1,80})")
    _re_name = re.compile(r"(?:叫我|以后称呼我)\s*[:：]?\s*([^。！？!?]{1,30})")

    def __init__(self, *, llm_router: Any | None = None) -> None:
        self._llm_router = llm_router

    def extract(self, text: str) -> PreferenceExtraction:
        """从 user text 抽偏好信号.

        **Agent-first 契约** (2026-04 重构):
          LLM 是偏好识别的**唯一权威路径**. LLM 抛异常时, 不再静默回退到 regex
          假装"识别到了"(那会让 Brain 以为用户从未表达过业务域偏好, 下游 filter
          就会按空 HardConstraints 工作 — 无从审计, 偏好永久丢失).

          - ``llm_router`` 注入且调用成功 → 走 LLM 结构化输出(生产路径)
          - ``llm_router`` 注入但调用失败 → 记 ERROR + 返回**空** PreferenceExtraction,
            由上游 (Brain / reflect / evolution) 决定是否重试或报告用户. 不再偷
            偷跑 regex.
          - ``llm_router=None`` (显式) → 走 regex (仅限测试夹具 / 无 LLM 环境).
        """
        safe_text = str(text or "").strip()
        if not safe_text:
            return PreferenceExtraction()

        if self._llm_router is not None:
            try:
                result = self._extract_with_llm(safe_text)
            except (RuntimeError, ValueError, TypeError, KeyError, OSError) as exc:
                # 故意只捕获"可预见"的失败类型; 绝不吞 Exception 大类.
                logger.error(
                    "preference_extract llm_failed (agentic contract says no regex "
                    "fallback); returning empty. err=%s",
                    str(exc)[:300],
                )
                return PreferenceExtraction()
            logger.info(
                "preference_extract kind=llm core_keys=%s soul_keys=%s "
                "domain_prefs=%d evidences=%d text_chars=%d",
                sorted(result.core_prefs.keys())[:8],
                sorted(result.soul_updates.keys())[:5],
                len(result.domain_prefs),
                len(result.evidences),
                len(safe_text),
            )
            return result

        # llm_router 显式未注入: 仅此一条路径允许 regex (测试 / 离线诊断)
        result = self._extract_with_regex(safe_text)
        logger.info(
            "preference_extract kind=regex(no-llm) core_keys=%s soul_keys=%s "
            "domain_prefs=%d evidences=%d text_chars=%d",
            sorted(result.core_prefs.keys())[:8],
            sorted(result.soul_updates.keys())[:5],
            len(result.domain_prefs),
            len(result.evidences),
            len(safe_text),
        )
        return result

    # ------------------------------------------------------------------
    # LLM path
    # ------------------------------------------------------------------
    def _extract_with_llm(self, text: str) -> PreferenceExtraction:
        instruction = self._build_instruction(text)
        raw = self._llm_router.invoke_text(instruction, route="classification")
        parsed = self._parse_json(raw)
        if not isinstance(parsed, dict):
            return PreferenceExtraction()

        core_prefs = parsed.get("core_prefs")
        # 兼容 legacy LLM 输出 (``prefs_updates``).
        if not isinstance(core_prefs, dict):
            legacy = parsed.get("prefs_updates")
            if isinstance(legacy, dict):
                core_prefs = legacy
        soul = parsed.get("soul_updates")
        domain_raw = parsed.get("domain_prefs")
        evidences = parsed.get("evidences")

        core_dict = dict(core_prefs) if isinstance(core_prefs, dict) else {}
        domain_prefs = self._coerce_domain_prefs(domain_raw)
        # 保险层: LLM 偶尔会把业务域键(preferred_location / target_roles ...)
        # 错写进 core_prefs. 这里把它们抢救成 job hard_constraint.set,
        # 并从 core_prefs 删除 — 避免 CoreMemory 被业务偏好污染, 也避免信号丢失.
        core_dict, rescued = self._rescue_domain_from_core(core_dict)
        if rescued:
            domain_prefs.extend(rescued)

        return PreferenceExtraction(
            core_prefs=core_dict,
            soul_updates=dict(soul) if isinstance(soul, dict) else {},
            domain_prefs=domain_prefs,
            evidences=[str(e) for e in evidences] if isinstance(evidences, list) else [],
        )

    @classmethod
    def _rescue_domain_from_core(
        cls, core: dict[str, Any]
    ) -> tuple[dict[str, Any], list[DomainPref]]:
        """从 core_prefs 中抢救被 LLM 错放进去的业务域键, 转成 domain_prefs.

        只处理 ``_CORE_TO_JOB_HC_FIELD`` 映射里登记的键; 其它未识别键交由
        ``SoulEvolutionEngine._split_pref_updates`` 进一步审计 (non-core skip).
        """
        rescued: list[DomainPref] = []
        cleaned: dict[str, Any] = {}
        for key, value in core.items():
            safe_key = str(key or "").strip().lower()
            if safe_key in cls._CORE_TO_JOB_HC_FIELD:
                hc_field = cls._CORE_TO_JOB_HC_FIELD[safe_key]
                args = cls._normalize_domain_pref_args(
                    "job",
                    "hard_constraint.set",
                    {"field": hc_field, "value": value},
                )
                logger.info(
                    "preference_extract: rescue core_prefs[%s] → job.hard_constraint.set(%s)",
                    safe_key, hc_field,
                )
                rescued.append(
                    DomainPref(
                        domain="job",
                        op="hard_constraint.set",
                        args=args,
                        evidence=f"rescued_from_core_prefs:{safe_key}",
                        confidence=0.7,
                    )
                )
            else:
                cleaned[key] = value
        return cleaned, rescued

    @staticmethod
    def _build_instruction(text: str) -> str:
        # 文档指引: docs/Pulse-DomainMemory与Tool模式.md §2.2.
        ops_hint_lines: list[str] = []
        for dom, ops in _DOMAIN_OP_CATALOG.items():
            for op_name, sig in ops.items():
                ops_hint_lines.append(f"  * domain={dom!r} op={op_name!r} → {sig}")
        ops_hint = "\n".join(ops_hint_lines)

        return (
            "你在分析一条用户消息, 把其中【可持久化的偏好意图】抽成结构化 JSON.\n"
            "输出三类字段 (每类都严格按下面的边界, 不要越位):\n\n"
            "1) core_prefs (dict): **跨业务域**通用的用户级偏好, 写入 CoreMemory.prefs.\n"
            "   允许键: preferred_name / language / timezone / default_location / "
            "like / dislike / response_style / response_tone / response_length / "
            "verbosity / communication_preference.\n"
            "   键一律 snake_case 英文; 值可以是 str / list / dict. 用户没明确说就不要出现.\n"
            "   * default_location 仅当用户说 \"默认城市/默认用 X 查\" 这种**跨场景查询"
            "基准**(例如 默认查北京天气)时使用; **不要**把\"找工作希望在杭州\""
            "这种**业务域**偏好塞进这里.\n\n"
            "2) soul_updates (dict): 人格/风格偏好.\n"
            "   允许键: tone (concise|detailed|casual|formal), style_rules (list[str]).\n\n"
            "3) domain_prefs (list[dict]): **业务域**结构化意图, 由下游 "
            "DomainPreferenceDispatcher 派发到 DomainMemory.\n"
            "   凡是求职域的 {偏好城市 / 目标岗位 / 期望薪资 / 实习 or 全职 / "
            "公司黑白名单 / 不重复投递等规则 / 已投记录} 一律从这里出, "
            "**严禁**同步写进 core_prefs.\n"
            "   每条 dict 形如: {\n"
            "     \"domain\": \"job\",\n"
            "     \"op\": \"hard_constraint.set\" | \"memory.record\" | ...,\n"
            "     \"args\": {...},\n"
            "     \"evidence\": \"出自原文: ...\",\n"
            "     \"confidence\": 0.0-1.0\n"
            "   }\n"
            "   目前已知的 domain × op 及 args schema:\n"
            f"{ops_hint}\n\n"
            "   args 细节约束 (**务必遵守, 否则被下游拒绝**):\n"
            "   * hard_constraint.set + field=preferred_location → value 必须是 "
            "list[str], 一个城市也要包成 [\"杭州\"]; 多个写全 [\"杭州\", \"上海\"].\n"
            "   * hard_constraint.set + field=target_roles → value 同样 list[str] "
            "(每个 role 一个字符串).\n"
            "   * hard_constraint.set + field=salary_floor_monthly → value 必须是"
            "**结构化量纲 dict**, 由代码确定性换算成月薪 K; **你不要自己算**,\n"
            "     只负责把用户原话里的 数字 + 货币单位 + 时间周期 三要素如实抽出:\n"
            "     {\"amount\": <float>, \"unit\": \"yuan\"|\"k_yuan\"|\"w_yuan\", "
            "\"period\": \"day\"|\"month\"|\"year\", \"work_days_per_month\"?: int}\n"
            "       - unit: 元=yuan, 千元/K=k_yuan, 万元=w_yuan\n"
            "       - period: 日/天=day, 月=month, 年=year\n"
            "       - work_days_per_month 仅 period=day 时可选, 用户没说就省略 (代码默认 22)\n"
            "     示例:\n"
            "       \"月薪 ≥ 30K\"           → {amount:30,  unit:k_yuan, period:month}\n"
            "       \"不低于 2 万/月\"       → {amount:2,   unit:w_yuan, period:month}\n"
            "       \"日薪 300 元/天\"       → {amount:300, unit:yuan,   period:day}\n"
            "       \"实习 200-400 每天\"    → {amount:200, unit:yuan,   period:day}  # 取下限\n"
            "       \"年薪 30 万\"           → {amount:30,  unit:w_yuan, period:year}\n"
            "     用户没提薪资 / 说\"不卡薪资\" → 不要输出这条 hard_constraint.set.\n"
            "   * hard_constraint.set + field=experience_level → value ∈ "
            "{intern, new_grad, full_time, senior}; 用户说\"实习\" → intern.\n\n"
            "   * memory.record 的 op **必须**填 \"memory.record\" (字面值),\n"
            "     item.type 才放 constraint_note / application_event / avoid_company 等:\n"
            "       正确: {\"op\":\"memory.record\",\"args\":{\"item\":"
            "{\"type\":\"constraint_note\",\"content\":\"已联系过的不要重投\"}}}\n"
            "       错误: {\"op\":\"constraint_note\",\"args\":{\"content\":\"...\"}}  "
            "← op 槽不能直接放 type.\n"
            "     item.type 选择:\n"
            "       - constraint_note: 用户下的**规则/指令**, 例如\"已经联系过的不要"
            "重复投递\"、\"不要大厂\"、\"优先小厂/初创\".\n"
            "       - application_event: 已经**发生**的投递事件 (必须有具体 company+URL), "
            "\"我投过 X\" 这种才记; 没有具体公司/URL → 用 constraint_note.\n"
            "       - avoid_company / favor_company: 公司黑白名单(含类型描述, "
            "比如\"不要大厂\"走 avoid_trait).\n\n"
            "4) evidences (list[str]): 每一条描述\"识别到了什么 + 出自用户原文哪几个字\".\n\n"
            "硬规则:\n"
            "  - 只抽用户**明确说出**的信息, 不要脑补.\n"
            "  - 业务域优先走 domain_prefs(具体指令); 不要把业务域偏好塞进 core_prefs.\n"
            "  - 如果没识别到任何信号, 返回 {core_prefs:{}, soul_updates:{}, "
            "domain_prefs:[], evidences:[]}.\n"
            "  - 输出合法 JSON, 不要 markdown fence, 不要解释文字.\n\n"
            f"用户消息: {text[:1200]}"
        )

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any] | None:
        cleaned = str(raw or "").strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            start = 1
            end = len(lines)
            for i in range(len(lines) - 1, 0, -1):
                if lines[i].strip() == "```":
                    end = i
                    break
            cleaned = "\n".join(lines[start:end]).strip()
        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.warning("preference_extract: LLM output not valid JSON: %s", str(exc)[:200])
            return None
        return obj if isinstance(obj, dict) else None

    # 业务域 hard_constraint 字段里, 这几个规定必须是 list[str].
    # LLM 有时会按字面写成单个字符串("杭州或者上海") — 我们负责拆回 list,
    # 而不是让 JobMemory / JobGreetService 在下游替它收拾烂摊子.
    _HC_LIST_FIELDS: frozenset[str] = frozenset({
        "preferred_location",
        "target_roles",
    })
    _HC_LIST_SPLIT = re.compile(r"[、/\|，,;；\s]+|(?:\s*(?:或者|或|以及|and|or)\s*)")

    # core_prefs 里**不应该**出现的业务域键: 一旦发现, extractor 自身把它抢救成
    # 一条 job hard_constraint.set domain_pref, 既不丢信号也不污染 CoreMemory.
    _CORE_TO_JOB_HC_FIELD: dict[str, str] = {
        "preferred_location": "preferred_location",
        "work_location": "preferred_location",
        "target_roles": "target_roles",
        "target_role": "target_roles",
        "salary_floor_monthly": "salary_floor_monthly",
        "experience_level": "experience_level",
    }

    # 合法 domain op 白名单. op 不在这里而是像 "constraint_note" / "application_event"
    # / "avoid_company" 之类时, 绝大概率是 LLM 把 memory item 的 **type** 误当作
    # **op** 输出 — 我们按"op 实际上是 type, 真正的 op 应该是 memory.record"把它
    # 平滑改写. 参见 JobPreferenceApplier._MEMORY_ITEM_TYPES.
    _KNOWN_DOMAIN_OPS: frozenset[str] = frozenset({
        "hard_constraint.set",
        "hard_constraint.unset",
        "memory.record",
    })
    _MEMORY_TYPE_SYNONYMS: frozenset[str] = frozenset({
        "constraint_note",
        "application_event",
        "avoid_company",
        "favor_company",
        "avoid_trait",
        "favor_trait",
        "capability_claim",
        "other",
    })

    # salary_floor_monthly 存储单位固定为 "整数 K/月". 上游 LLM 按合同输出结构化
    # 量纲 {amount, unit, period, work_days_per_month?}, 我们做**确定性换算** —
    # 不再按数值大小猜单位 (那是反 Agentic 的补丁). 参见 _normalize_salary_floor.
    _SALARY_UNIT_TO_YUAN: dict[str, float] = {
        "yuan": 1.0,
        "k_yuan": 1000.0,
        "w_yuan": 10000.0,
    }
    _SALARY_DEFAULT_WORK_DAYS_PER_MONTH: int = 22  # 周一到周五 * 52 / 12 ≈ 21.67, 取 22

    @classmethod
    def _coerce_domain_prefs(cls, raw: Any) -> list[DomainPref]:
        if not isinstance(raw, list):
            return []
        out: list[DomainPref] = []
        for idx, entry in enumerate(raw):
            if not isinstance(entry, dict):
                logger.debug("preference_extract: skip non-dict domain_prefs[%d]", idx)
                continue
            domain = str(entry.get("domain") or "").strip().lower()
            op = str(entry.get("op") or "").strip().lower()
            if not domain or not op:
                logger.debug(
                    "preference_extract: skip domain_prefs[%d] missing domain/op: %r",
                    idx, entry,
                )
                continue
            args = entry.get("args")
            if not isinstance(args, dict):
                args = {}
            args = dict(args)
            # 入口级归一: LLM 把 memory item 的 **type** (constraint_note /
            # application_event / avoid_company / ...) 直接塞进 **op** 字段
            # 是这次生产链路上最常见的错位, 会被 dispatcher 以 unsupported_op
            # 直接拒掉, 用户的规则/黑白名单彻底丢失. 这里平滑改写:
            #   op=<type> 且 args 里没有 item → op=memory.record, args.item.type=<type>
            if domain == "job" and op in cls._MEMORY_TYPE_SYNONYMS and op not in cls._KNOWN_DOMAIN_OPS:
                logger.info(
                    "preference_extract: rewrite op=%s → memory.record "
                    "(LLM put memory-item type into op slot)",
                    op,
                )
                item = args.get("item") if isinstance(args.get("item"), dict) else {}
                item = dict(item)
                item.setdefault("type", op)
                # args 里 raw_text/content/target/source_url 这些常见字段移进 item
                for k in ("content", "raw_text", "target", "source_url", "company"):
                    if k in args and k not in item:
                        item[k] = args.pop(k)
                args = {"item": item}
                op = "memory.record"
            args = cls._normalize_domain_pref_args(domain, op, args)
            evidence = str(entry.get("evidence") or "").strip()
            conf_raw = entry.get("confidence")
            try:
                conf = float(conf_raw) if conf_raw is not None else 0.8
            except (TypeError, ValueError):
                conf = 0.8
            conf = max(0.0, min(1.0, conf))
            out.append(
                DomainPref(
                    domain=domain,
                    op=op,
                    args=args,
                    evidence=evidence,
                    confidence=conf,
                )
            )
        return out

    @classmethod
    def _normalize_domain_pref_args(
        cls, domain: str, op: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """把 LLM 输出的 args 归一化成下游 applier 期望的形态.

        规则都是\"**架构层兜底**\": LLM prompt 里已要求正确格式, 这里只是防它
        偶尔写串, 不要让 JobMemory._normalize_hc_value 之流在下游靠字符串硬猜.

        目前处理:
          * job + hard_constraint.set
              - field ∈ {preferred_location, target_roles} → value 强制 list[str]
              - field = salary_floor_monthly → value 转 int (解析失败丢弃)
              - field = experience_level → value 小写 + strip, 非字符串丢弃
          * job + memory.record
              - item.type=application_event 但 item 里既没 source_url 也没 target
                → 判为\"其实是指令/规则\", 降级成 constraint_note
        """
        if domain == "job" and op == "hard_constraint.set":
            field = str(args.get("field") or "").strip().lower()
            if field:
                args["field"] = field
            value = args.get("value")
            if field in cls._HC_LIST_FIELDS:
                args["value"] = cls._as_str_list(value)
            elif field == "salary_floor_monthly":
                args["value"] = cls._normalize_salary_floor(value)
                # 领域换算 (structured spec → 整数 K/月) 现在由 JobMemory 统一
                # 负责, 见 JobMemory._salary_spec_to_storage. 这里只做 shape 清洗,
                # 保证 dict 透传时原始三要素 (amount/unit/period) 不丢失,
                # 下游审计/prompt 能回放 "300元/天" 而不是只剩 "7 K/月" (P2-B).
            elif field == "experience_level":
                if isinstance(value, str):
                    args["value"] = value.strip().lower() or None
                else:
                    args["value"] = None
        elif domain == "job" and op == "memory.record":
            item = args.get("item")
            if isinstance(item, dict):
                item_type = str(item.get("type") or "").strip().lower()
                target = str(item.get("target") or "").strip()
                raw_text = str(item.get("raw_text") or "")
                has_url = "source_url" in raw_text or "http" in raw_text.lower()
                if item_type == "application_event" and not target and not has_url:
                    # 没有 company 也没有 URL 的 "application_event" 绝大多数是
                    # 被错分类的"投递规则/约束", 按实际语义落到 constraint_note.
                    logger.info(
                        "preference_extract: reclassify memory.record "
                        "application_event→constraint_note (no target/url)",
                    )
                    item["type"] = "constraint_note"
                args["item"] = item
        return args

    @classmethod
    def _as_str_list(cls, value: Any) -> list[str]:
        """把 \"杭州或者上海\" / [\"杭州\", \"上海\"] / \"杭州、上海\" 统一成 list[str]."""
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, tuple):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str):
            parts = [p.strip() for p in cls._HC_LIST_SPLIT.split(value) if p and p.strip()]
            return parts
        return [str(value).strip()] if str(value).strip() else []

    @classmethod
    def _normalize_salary_floor(cls, value: Any) -> Any:
        """Shape 清洗 (**不**做换算).

        **Agentic 合同**:
          - 上游 LLM 负责**语义理解**: 把用户话里的三要素抽出来, 绝不自己做换算.
          - 本函数负责**shape 清洗**: 校验三要素合法, 透传给 JobMemory;
            JobMemory 在落库时做确定性换算并把原始 spec 原样保留, 审计/prompt
            可以回放用户原话 (P2-B, see ``JobMemory._salary_spec_to_storage``).
          - 之所以把换算放到 JobMemory 而不是这里: **领域知识归领域模块**;
            这里只能判断"LLM 输出是否结构合法", 无权决定具体换算规则.

        合同输入 (**首选**, LLM 按 prompt 产出):
            {"amount": float, "unit": "yuan"|"k_yuan"|"w_yuan",
             "period": "day"|"month"|"year",
             "work_days_per_month"?: int  # 仅 period=day 时有意义, 缺省交 JobMemory}

        返回:
          - dict with well-formed {amount, unit, period[, work_days_per_month]}
              → 透传 (shape 清洗过: amount→float, unit/period→小写, 非法 key 丢弃)
          - bare int/float/str → int (合同外回退路径, 兼容 LLM 不按合同的偶发情况;
              按 schema 字面含义当 K/月, 但**没有 source**, 下游没法回放)
          - None / 0 / 负数 / 非法结构 → None (用户未明确设置, 或 LLM 输出废话)
        """
        if value is None:
            return None

        if isinstance(value, dict):
            return cls._clean_salary_spec(value)

        # 合同外: bare 数值. 按 schema 字面含义当 K/月.
        v = cls._as_int_or_none(value)
        if v is None or v <= 0:
            return None
        logger.warning(
            "preference_extract: salary_floor_monthly received bare value %r; "
            "treating as K/月 per schema. LLM should emit structured "
            "{amount, unit, period} instead (see prompt).",
            value,
        )
        return v

    @classmethod
    def _clean_salary_spec(cls, value: dict[str, Any]) -> dict[str, Any] | None:
        """Shape-validate LLM 输出的 structured salary spec, 不做单位换算.

        校验失败一律返回 None (不抛, 让 dispatcher 把整条 pref 丢掉). 换算规则
        由 ``JobMemory._salary_spec_to_storage`` 负责, 并在那里落库原始 source.
        """
        amount_raw = value.get("amount")
        try:
            amount = float(amount_raw) if amount_raw is not None else 0.0
        except (TypeError, ValueError):
            logger.warning(
                "preference_extract: salary amount not numeric: %r", amount_raw,
            )
            return None
        if amount <= 0:
            return None

        unit = str(value.get("unit") or "").strip().lower()
        if unit not in cls._SALARY_UNIT_TO_YUAN:
            logger.warning(
                "preference_extract: salary.unit=%r invalid; expected one of %s",
                unit, sorted(cls._SALARY_UNIT_TO_YUAN),
            )
            return None

        period = str(value.get("period") or "").strip().lower()
        if period not in ("day", "month", "year"):
            logger.warning(
                "preference_extract: salary.period=%r invalid; expected day|month|year",
                period,
            )
            return None

        clean: dict[str, Any] = {"amount": amount, "unit": unit, "period": period}
        if period == "day":
            wd_raw = value.get("work_days_per_month")
            if wd_raw is not None:
                wd = cls._as_int_or_none(wd_raw)
                if wd is not None and wd > 0:
                    clean["work_days_per_month"] = wd
        return clean

    @staticmethod
    def _as_int_or_none(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            match = re.search(r"\d+(?:\.\d+)?", value)
            if not match:
                return None
            try:
                return int(float(match.group(0)))
            except (TypeError, ValueError):
                return None
        return None

    # ------------------------------------------------------------------
    # Regex path (fallback)
    # ------------------------------------------------------------------
    def _extract_with_regex(self, text: str) -> PreferenceExtraction:
        core_prefs: dict[str, Any] = {}
        soul_updates: dict[str, Any] = {}
        evidences: list[str] = []

        location_match = self._re_default_location.search(text)
        if location_match:
            location = _clean_value(location_match.group(1), max_len=20)
            if location:
                core_prefs["default_location"] = location
                evidences.append("default_location")

        dislike_match = self._re_dislike.search(text)
        if dislike_match:
            dislike = _clean_value(dislike_match.group(1))
            if dislike:
                core_prefs["dislike"] = dislike
                evidences.append("dislike")

        like_match = self._re_like.search(text)
        if like_match:
            like = _clean_value(like_match.group(1))
            if like:
                core_prefs["like"] = like
                evidences.append("like")

        name_match = self._re_name.search(text)
        if name_match:
            name = _clean_value(name_match.group(1), max_len=30)
            if name:
                core_prefs["preferred_name"] = name
                evidences.append("preferred_name")

        lowered = text.lower()
        if any(token in lowered for token in ("简短", "简洁", "别太啰嗦", "精炼")):
            soul_updates["tone"] = "concise"
            soul_updates["style_rules"] = ["Keep responses concise unless user asks for details."]
            evidences.append("style_concise")
        elif any(token in lowered for token in ("详细一点", "展开讲", "讲细点", "解释详细")):
            soul_updates["tone"] = "detailed"
            soul_updates["style_rules"] = ["Provide more detail with examples when user asks."]
            evidences.append("style_detailed")

        # Regex 路径不支持业务域偏好识别(成本太高), 有 LLM 失败的记录足够审计.
        return PreferenceExtraction(
            core_prefs=core_prefs,
            soul_updates=soul_updates,
            domain_prefs=[],
            evidences=evidences,
        )


__all__ = ["PreferenceExtractor", "PreferenceExtraction", "DomainPref"]
