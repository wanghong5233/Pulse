"""Smoke test: PreferenceExtractor → DomainPreferenceDispatcher → Applier.

验证 F2 架构链路:
  - extractor 输出 {core_prefs, soul_updates, domain_prefs[]}
  - dispatcher 按 domain 派发到注册的 applier
  - reflect_interaction 集成: SoulEvolutionEngine 调 dispatcher 并汇总结果
  - confidence 阈值 / 未注册 domain / applier 异常 都被正确分类

跑:
  PYTHONPATH=src python scripts/smoke_domain_preference_dispatch.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pulse.core.learning.domain_preference_dispatcher import (  # noqa: E402
    DomainPreferenceDispatchResult,
    DomainPreferenceDispatcher,
)
from pulse.core.learning.preference_extractor import (  # noqa: E402
    DomainPref,
    PreferenceExtraction,
    PreferenceExtractor,
)


# ──────────────────────────────────────────────────────────────
# Fakes
# ──────────────────────────────────────────────────────────────


class _FakeApplier:
    def __init__(
        self,
        domain: str,
        *,
        supported: tuple[str, ...] = ("hard_constraint.set", "memory.record"),
        raise_on: str | None = None,
    ) -> None:
        self.domain = domain
        self.supported_ops = supported
        self.calls: list[DomainPref] = []
        self._raise_on = raise_on

    def apply(self, pref, *, context):
        self.calls.append(pref)
        if self._raise_on and pref.op == self._raise_on:
            raise RuntimeError("boom")
        return DomainPreferenceDispatchResult(
            domain=pref.domain,
            op=pref.op,
            status="applied",
            effect={"workspace_id": context.get("workspace_id"), **pref.args},
            evidence=pref.evidence,
            confidence=pref.confidence,
        )


class _FakeLLMRouter:
    def __init__(self, payload: dict[str, Any]) -> None:
        import json as _json
        self._out = _json.dumps(payload, ensure_ascii=False)

    def invoke_text(self, prompt: str, *, route: str = "default") -> str:
        return self._out


# ──────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────


def test_extractor_parses_new_schema() -> None:
    llm = _FakeLLMRouter({
        "core_prefs": {"preferred_name": "阿明"},
        "soul_updates": {"tone": "concise"},
        "domain_prefs": [
            {
                "domain": "job",
                "op": "hard_constraint.set",
                "args": {"field": "preferred_location", "value": ["杭州", "上海"]},
                "evidence": "base 地点优先杭州或者上海",
                "confidence": 0.9,
            },
            {
                "domain": "job",
                "op": "memory.record",
                "args": {"item": {
                    "type": "avoid_trait",
                    "target": "大厂暑期",
                    "content": "战略性放弃大厂暑期实习",
                }},
                "evidence": "暂时战略性放弃大厂暑期实习",
                "confidence": 0.85,
            },
        ],
        "evidences": ["preferred_location", "avoid_trait"],
    })
    extractor = PreferenceExtractor(llm_router=llm)
    out = extractor.extract("我是阿明, 回复简短; base 地点优先杭州或者上海; 战略性放弃大厂暑期实习")
    assert isinstance(out, PreferenceExtraction)
    assert out.core_prefs == {"preferred_name": "阿明"}
    assert out.soul_updates == {"tone": "concise"}
    assert len(out.domain_prefs) == 2
    assert all(isinstance(p, DomainPref) for p in out.domain_prefs)
    # legacy alias 兼容
    assert out.prefs_updates == out.core_prefs
    print("  [ok] extractor parses {core_prefs, soul_updates, domain_prefs}")


def test_dispatcher_routes_by_domain() -> None:
    job_applier = _FakeApplier("job")
    other_applier = _FakeApplier("other")
    emitted: list[tuple[str, dict]] = []
    dispatcher = DomainPreferenceDispatcher(
        appliers=[job_applier, other_applier],
        event_emitter=lambda t, p: emitted.append((t, p)),
    )
    prefs = [
        DomainPref(domain="job", op="memory.record",
                   args={"item": {"type": "favor_trait", "target": "初创", "content": "偏好小厂"}},
                   evidence="小厂或初创深度垂直", confidence=0.9),
        DomainPref(domain="other", op="hard_constraint.set",
                   args={"field": "x", "value": 1}, confidence=0.9),
        DomainPref(domain="unknown", op="memory.record",
                   args={}, confidence=0.9),
    ]
    results = dispatcher.dispatch(prefs, context={"workspace_id": "job.default", "trace_id": "t1"})
    assert len(results) == 3
    assert results[0].status == "applied"
    assert results[1].status == "applied"
    assert results[2].status == "skipped" and "no_applier_registered" in results[2].reason
    assert len(job_applier.calls) == 1
    assert len(other_applier.calls) == 1
    statuses = [t for t, _ in emitted]
    assert statuses.count("preference.domain.applied") == 2
    assert "preference.domain.skipped" in statuses
    print("  [ok] dispatcher routes + events ->", statuses)


def test_dispatcher_confidence_gate() -> None:
    job_applier = _FakeApplier("job")
    dispatcher = DomainPreferenceDispatcher(
        appliers=[job_applier], min_confidence=0.6,
    )
    low = DomainPref(domain="job", op="memory.record",
                     args={"item": {"type": "other", "content": "hmm"}},
                     confidence=0.3)
    [r] = dispatcher.dispatch([low], context={})
    assert r.status == "skipped" and "low_confidence" in r.reason
    assert not job_applier.calls
    print("  [ok] dispatcher filters low-confidence prefs")


def test_dispatcher_rejects_unsupported_op() -> None:
    applier = _FakeApplier("job", supported=("memory.record",))
    dispatcher = DomainPreferenceDispatcher(appliers=[applier])
    pref = DomainPref(domain="job", op="hard_constraint.set",
                      args={"field": "x", "value": 1}, confidence=0.9)
    [r] = dispatcher.dispatch([pref], context={})
    assert r.status == "rejected" and "unsupported_op" in r.reason
    assert not applier.calls
    print("  [ok] dispatcher rejects op outside applier whitelist")


def test_dispatcher_applier_exception_marked_error() -> None:
    applier = _FakeApplier("job", raise_on="memory.record")
    dispatcher = DomainPreferenceDispatcher(appliers=[applier])
    pref = DomainPref(domain="job", op="memory.record",
                      args={"item": {"type": "other", "content": "x"}},
                      confidence=0.9)
    [r] = dispatcher.dispatch([pref], context={})
    assert r.status == "error" and "RuntimeError" in r.reason
    print("  [ok] applier exception → status=error")


def test_evolution_integrates_dispatcher() -> None:
    """SoulEvolutionEngine.reflect_interaction 端到端集成 domain 派发."""
    import logging
    logging.basicConfig(level=logging.WARNING)

    class _FakeGov:
        def apply_preference_updates(self, **kw):
            return {"ok": True, **kw}
        def apply_soul_update(self, **kw):
            return {"ok": True, **kw}
        def add_mutable_belief(self, **kw):
            return {"ok": True, **kw}

    class _FakeArchival:
        def add_fact(self, **kw):
            return {"id": "fact_1", **kw}

    from pulse.core.soul.evolution import SoulEvolutionEngine

    llm = _FakeLLMRouter({
        "core_prefs": {"preferred_name": "阿明"},
        "soul_updates": {"tone": "concise"},
        "domain_prefs": [
            {
                "domain": "job",
                "op": "hard_constraint.set",
                "args": {"field": "preferred_location", "value": ["杭州"]},
                "evidence": "优先杭州",
                "confidence": 0.9,
            },
        ],
        "evidences": [],
    })
    extractor = PreferenceExtractor(llm_router=llm)
    applier = _FakeApplier("job")
    dispatcher = DomainPreferenceDispatcher(appliers=[applier])
    engine = SoulEvolutionEngine(
        governance=_FakeGov(),
        archival_memory=_FakeArchival(),
        preference_extractor=extractor,
        domain_preference_dispatcher=dispatcher,
    )
    result = engine.reflect_interaction(
        user_text="我是阿明, 回复简短; base 优先杭州",
        assistant_text="好的",
        metadata={"workspace_id": "job.alice", "trace_id": "t-e2e"},
    )
    d = result.to_dict()
    assert d["preference_applied"], "core pref should be applied"
    assert d["soul_applied"], "soul should be applied"
    assert len(d["domain_applied"]) == 1
    applied = d["domain_applied"][0]
    assert applied["status"] == "applied"
    assert applied["domain"] == "job"
    assert applied["effect"]["workspace_id"] == "job.alice"
    print("  [ok] evolution e2e -> domain_applied:", applied["status"])


def test_extractor_regex_fallback_has_empty_domain_prefs() -> None:
    extractor = PreferenceExtractor(llm_router=None)
    out = extractor.extract("默认城市 杭州")
    assert out.core_prefs.get("default_location") == "杭州"
    assert out.domain_prefs == []
    print("  [ok] regex fallback returns empty domain_prefs")


def test_op_slot_holds_type_is_rewritten() -> None:
    """R3.5 Bug B: LLM 把 memory item 的 type 直接塞进 op 字段时,
    extractor 必须把它改写成 op=memory.record, 否则 dispatcher 会 unsupported_op 拒掉.

    生产事故原文: op=\"constraint_note\" → rejected; 用户的\"不要重复投递\"规则丢失.
    """
    llm = _FakeLLMRouter({
        "core_prefs": {},
        "soul_updates": {},
        "domain_prefs": [
            {
                "domain": "job",
                "op": "constraint_note",  # 错位 — 本该是 memory.record
                "args": {"content": "已联系过的不要重投"},
                "confidence": 0.8,
            },
            {
                "domain": "job",
                "op": "avoid_company",  # 另一种错位
                "args": {"target": "大厂"},
                "confidence": 0.8,
            },
        ],
        "evidences": [],
    })
    extractor = PreferenceExtractor(llm_router=llm)
    out = extractor.extract("已联系过的不要重投; 不要大厂")
    assert len(out.domain_prefs) == 2
    for pref in out.domain_prefs:
        assert pref.op == "memory.record", pref
        item = pref.args.get("item")
        assert isinstance(item, dict), pref
        assert item["type"] in {"constraint_note", "avoid_company"}
    # content / target 必须被挪进 item
    item_types = [p.args["item"]["type"] for p in out.domain_prefs]
    assert "constraint_note" in item_types
    assert "avoid_company" in item_types
    for pref in out.domain_prefs:
        if pref.args["item"]["type"] == "constraint_note":
            assert pref.args["item"].get("content") == "已联系过的不要重投"
        else:
            assert pref.args["item"].get("target") == "大厂"
    print("  [ok] B: op-as-type rewrite → memory.record")


def test_salary_structured_amount_unit_period() -> None:
    """R3.5 Bug C (Agentic 版) × P2-B (Agentic+ 版):
    LLM 输出结构化量纲 {amount, unit, period}, extractor 做 **shape 清洗** 并**透传**,
    JobMemory 在落库时按 SI 做确定性换算并保留原始 source. 两个职责分离之后,
    审计层能同时拿到 "7 K/月" 与 "300 yuan/day × 22" 两个视角 (P2-B).

    覆盖:
      - 结构化量纲 (正职/日薪/年薪/自定义工作日) → extractor 透传 cleaned dict,
        JobMemory.set_hard_constraint 换算 → get_hard_constraints 拿 int + spec
      - 合同外裸整数 → extractor 返 int, JobMemory 当 K/月存 (没有 source)
      - 非法输入 → extractor 返 None (dispatcher 把整条 pref 丢掉)
    """
    from pulse.core.memory.workspace_memory import WorkspaceMemory
    from pulse.modules.job.memory import JobMemory

    class _StubDB:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        def execute(self, sql, params=None, *, fetch="none", commit=True):  # noqa: ANN001
            _ = commit, fetch
            norm = " ".join(str(sql).lower().split())
            p = tuple(params or ())
            if norm.startswith("create table") or norm.startswith("create index"):
                return None
            if norm.startswith("select value from workspace_facts where workspace_id"):
                ws, key = p
                for row in self.rows:
                    if row["workspace_id"] == ws and row["key"] == key:
                        return (row["value"],)
                return None
            if norm.startswith("select id from workspace_facts where workspace_id"):
                ws, key = p
                for row in self.rows:
                    if row["workspace_id"] == ws and row["key"] == key:
                        return (id(row),)
                return None
            if norm.startswith("update workspace_facts set value"):
                value, source, updated_at, ws, key = p
                for row in self.rows:
                    if row["workspace_id"] == ws and row["key"] == key:
                        row["value"] = value
                        row["source"] = source
                        row["updated_at"] = updated_at
                        return None
                return None
            if norm.startswith("insert into workspace_facts"):
                workspace_id, key, value, source, created_at, updated_at = p
                self.rows.append({
                    "workspace_id": workspace_id, "key": key, "value": value,
                    "source": source, "created_at": created_at, "updated_at": updated_at,
                })
                return None
            if norm.startswith(
                "select key, value, source, updated_at from workspace_facts where workspace_id"
            ):
                ws, prefix_pattern = p
                prefix = prefix_pattern.rstrip("%")
                return [
                    (r["key"], r["value"], r["source"], r["updated_at"])
                    for r in sorted(self.rows, key=lambda r: r["key"])
                    if r["workspace_id"] == ws and (not prefix or r["key"].startswith(prefix))
                ]
            if norm.startswith(
                "select count(*) from workspace_facts where workspace_id = %s and key like"
            ):
                ws, pat = p
                prefix = pat.rstrip("%")
                return (sum(1 for r in self.rows
                            if r["workspace_id"] == ws and r["key"].startswith(prefix)),)
            if norm.startswith(
                "delete from workspace_facts where workspace_id = %s and key = %s"
            ):
                ws, key = p
                self.rows = [r for r in self.rows
                             if not (r["workspace_id"] == ws and r["key"] == key)]
                return None
            return None

    llm_case = lambda value: _FakeLLMRouter({
        "core_prefs": {},
        "soul_updates": {},
        "domain_prefs": [{
            "domain": "job",
            "op": "hard_constraint.set",
            "args": {"field": "salary_floor_monthly", "value": value},
            "confidence": 0.9,
        }],
        "evidences": [],
    })

    def _extractor_value(value) -> Any:
        """Extractor 层: shape 清洗后的 args.value (dict | int | None)."""
        extractor = PreferenceExtractor(llm_router=llm_case(value))
        out = extractor.extract("用户说了薪资约束")
        if not out.domain_prefs:
            return None
        return out.domain_prefs[0].args.get("value")

    def _memory_roundtrip(value) -> tuple[int | None, dict[str, Any] | None]:
        """端到端: extractor → JobMemory.set_hard_constraint → get_hard_constraints.
        返回 (salary_floor_monthly, salary_floor_spec)."""
        cleaned = _extractor_value(value)
        if cleaned is None:
            return None, None
        mem = JobMemory(
            workspace_memory=WorkspaceMemory(db_engine=_StubDB()),
            workspace_id="job.smoke",
        )
        try:
            mem.set_hard_constraint("salary_floor_monthly", cleaned)
        except ValueError:
            return None, None
        hc = mem.get_hard_constraints()
        return hc.salary_floor_monthly, hc.salary_floor_spec

    # ── extractor 层: 结构化 dict 透传清洗, bare → int ──
    assert _extractor_value({"amount": 300, "unit": "yuan", "period": "day"}) == {
        "amount": 300.0, "unit": "yuan", "period": "day",
    }, "extractor 必须透传 cleaned dict, 不做换算 (P2-B)"
    assert _extractor_value(
        {"amount": 300, "unit": "yuan", "period": "day", "work_days_per_month": 20}
    ) == {
        "amount": 300.0, "unit": "yuan", "period": "day", "work_days_per_month": 20,
    }
    assert _extractor_value(30) == 30, "bare int 继续走合同外 int 路径"

    # ── 端到端: JobMemory 负责换算并保留 source ──
    # 正职
    assert _memory_roundtrip({"amount": 30, "unit": "k_yuan", "period": "month"}) == (
        30, {"amount": 30.0, "unit": "k_yuan", "period": "month"},
    )
    assert _memory_roundtrip({"amount": 2, "unit": "w_yuan", "period": "month"}) == (
        20, {"amount": 2.0, "unit": "w_yuan", "period": "month"},
    )
    assert _memory_roundtrip({"amount": 9000, "unit": "yuan", "period": "month"}) == (
        9, {"amount": 9000.0, "unit": "yuan", "period": "month"},
    )
    # 实习日薪 (关键场景): 300 元/天 × 22 工作日 = 6600 元/月 ≈ 7 K
    k, spec = _memory_roundtrip({"amount": 300, "unit": "yuan", "period": "day"})
    assert (k, spec) == (
        7,
        {"amount": 300.0, "unit": "yuan", "period": "day", "work_days_per_month": 22},
    ), f"300 yuan/day → 7 K/月 + 原始 spec 必须保留; got ({k!r}, {spec!r})"
    # 日薪 + 自定义工作日
    assert _memory_roundtrip(
        {"amount": 300, "unit": "yuan", "period": "day", "work_days_per_month": 20}
    ) == (
        6,
        {"amount": 300.0, "unit": "yuan", "period": "day", "work_days_per_month": 20},
    )
    # 年薪
    assert _memory_roundtrip({"amount": 30, "unit": "w_yuan", "period": "year"}) == (
        25, {"amount": 30.0, "unit": "w_yuan", "period": "year"},
    )
    # 合同外: bare int → K/月, 没有 source
    assert _memory_roundtrip(30) == (30, None)

    # ── 非法输入 → extractor 返 None, 不走到 JobMemory ──
    assert _extractor_value({"amount": 0, "unit": "k_yuan", "period": "month"}) is None
    assert _extractor_value({"amount": 30, "unit": "bitcoin", "period": "month"}) is None
    assert _extractor_value({"amount": 30, "unit": "k_yuan", "period": "century"}) is None
    assert _extractor_value(0) is None
    assert _extractor_value(-5) is None
    assert _extractor_value(None) is None
    print(
        "  [ok] C × P2-B: extractor shape-cleans, JobMemory converts + retains source"
    )


def test_pre_capture_then_reflect_no_double_dispatch() -> None:
    """F8: pre_capture_domain 之后再 reflect 不应重复 dispatch, 避免 memory.record
    写两条 (新 uuid 使得自然语义上的 dedup 失效).
    """
    class _FakeGov:
        def __init__(self) -> None:
            self.pref_calls = 0
        def apply_preference_updates(self, **kw):
            self.pref_calls += 1
            return {"ok": True, **kw}
        def apply_soul_update(self, **kw):
            return {"ok": True, **kw}
        def add_mutable_belief(self, **kw):
            return {"ok": True, **kw}

    class _FakeArchival:
        def add_fact(self, **kw):
            return {"id": "fact", **kw}

    from pulse.core.soul.evolution import SoulEvolutionEngine

    llm = _FakeLLMRouter({
        "core_prefs": {"preferred_name": "阿明"},
        "soul_updates": {"tone": "concise"},
        "domain_prefs": [
            {
                "domain": "job",
                "op": "memory.record",
                "args": {"item": {
                    "type": "constraint_note",
                    "content": "不要重复投递",
                }},
                "evidence": "注意已经联系过的不要重复投递",
                "confidence": 0.9,
            },
        ],
        "evidences": [],
    })
    extractor = PreferenceExtractor(llm_router=llm)
    applier = _FakeApplier("job")
    dispatcher = DomainPreferenceDispatcher(appliers=[applier])
    engine = SoulEvolutionEngine(
        governance=_FakeGov(),
        archival_memory=_FakeArchival(),
        preference_extractor=extractor,
        domain_preference_dispatcher=dispatcher,
    )

    pre = engine.pre_capture_domain(
        user_text="注意已经联系过的不要重复投递",
        metadata={"workspace_id": "job.alice", "trace_id": "t-pre"},
    )
    assert pre.already_dispatched is True
    assert len(pre.domain_applied) == 1
    assert pre.domain_applied[0]["status"] == "applied"
    assert len(applier.calls) == 1, "pre-capture should dispatch exactly once"

    result = engine.reflect_interaction(
        user_text="注意已经联系过的不要重复投递",
        assistant_text="好的",
        metadata={"workspace_id": "job.alice", "trace_id": "t-pre"},
        precaptured=pre,
    )
    d = result.to_dict()
    assert len(d["domain_applied"]) == 1, d["domain_applied"]
    assert d["domain_applied"][0]["status"] == "applied"
    assert len(applier.calls) == 1, (
        "reflect with precaptured should NOT dispatch again; "
        f"applier.calls={len(applier.calls)}"
    )
    print("  [ok] F8 pre_capture + reflect: single dispatch, no duplication")


def test_pre_capture_without_dispatcher_bound() -> None:
    """F8 降级: pre_capture 时 dispatcher 还没绑, 不应抛, 不应把结果标记为 dispatched."""
    from pulse.core.soul.evolution import SoulEvolutionEngine

    class _FakeGov:
        def apply_preference_updates(self, **kw):
            return {"ok": True, **kw}
        def apply_soul_update(self, **kw):
            return {"ok": True, **kw}
        def add_mutable_belief(self, **kw):
            return {"ok": True, **kw}

    class _FakeArchival:
        def add_fact(self, **kw):
            return {"id": "fact", **kw}

    llm = _FakeLLMRouter({
        "core_prefs": {},
        "soul_updates": {},
        "domain_prefs": [{
            "domain": "job",
            "op": "hard_constraint.set",
            "args": {"field": "preferred_location", "value": ["杭州"]},
            "confidence": 0.9,
        }],
        "evidences": [],
    })
    extractor = PreferenceExtractor(llm_router=llm)
    engine = SoulEvolutionEngine(
        governance=_FakeGov(),
        archival_memory=_FakeArchival(),
        preference_extractor=extractor,
        domain_preference_dispatcher=None,
    )
    pre = engine.pre_capture_domain(
        user_text="希望在杭州工作",
        metadata={"workspace_id": "job.alice"},
    )
    assert pre.already_dispatched is False
    assert pre.domain_applied == []
    # extraction 仍被保留供 reflect 复用
    assert pre.extraction.domain_prefs and pre.extraction.domain_prefs[0].domain == "job"

    # 后绑 dispatcher, reflect 依然能走标准 post-turn 路径 (因为 already_dispatched=False)
    applier = _FakeApplier("job")
    engine.bind_domain_preference_dispatcher(
        DomainPreferenceDispatcher(appliers=[applier])
    )
    result = engine.reflect_interaction(
        user_text="希望在杭州工作",
        assistant_text="好的",
        metadata={"workspace_id": "job.alice"},
        precaptured=pre,
    )
    d = result.to_dict()
    assert len(d["domain_applied"]) == 1
    assert d["domain_applied"][0]["status"] == "applied"
    print("  [ok] F8 degrade: pre_capture no dispatcher → reflect takes over")


def test_intent_router_skips_natural_language() -> None:
    """F5: 自然语言输入不应触发 IntentRouter 的 LLM 兜底."""
    from pulse.core.router import IntentRouter

    class _Tripwire:
        def __init__(self) -> None:
            self.called = False
        def invoke_structured(self, *a, **kw):
            self.called = True
            raise AssertionError("should not be called for natural language")

    wire = _Tripwire()
    r = IntentRouter(llm_router=wire)
    r.register_intent("email.process", target="email")
    r.register_intent("general.default", target="general")
    r.register_prefix("/email process", intent="email.process")

    # 自然语言, 中文, 长度 > 6 tokens
    decision = r.resolve("我正在找大模型应用开发 agent 实习, 帮我投递 5 个合适的 JD")
    assert decision.method == "fallback", decision
    assert "natural-language" in decision.reason
    assert not wire.called

    # slash-command 风格: 应触发 LLM(这里会 AssertionError, 说明被调了; 走不同路径)
    # 为了不触发 AssertionError, 用另一个 wire
    class _WireOK:
        def invoke_structured(self, *a, **kw):
            from pulse.core.router import _LLMIntentOutput
            return _LLMIntentOutput(intent="email.process", confidence=0.9, reason="ok")

    r2 = IntentRouter(llm_router=_WireOK())
    r2.register_intent("email.process", target="email")
    r2.register_intent("general.default", target="general")
    d2 = r2.resolve("/email process today")
    assert d2.intent == "email.process"
    assert d2.method == "llm"
    print("  [ok] IntentRouter: natural-lang → Brain, slash-cmd → LLM fallback")


if __name__ == "__main__":
    print("[smoke] domain preference dispatch pipeline")
    test_extractor_parses_new_schema()
    test_extractor_regex_fallback_has_empty_domain_prefs()
    test_dispatcher_routes_by_domain()
    test_dispatcher_confidence_gate()
    test_dispatcher_rejects_unsupported_op()
    test_dispatcher_applier_exception_marked_error()
    test_evolution_integrates_dispatcher()
    test_op_slot_holds_type_is_rewritten()
    test_salary_structured_amount_unit_period()
    test_pre_capture_then_reflect_no_double_dispatch()
    test_pre_capture_without_dispatcher_bound()
    test_intent_router_skips_natural_language()
    print("[smoke] ALL PASS")
