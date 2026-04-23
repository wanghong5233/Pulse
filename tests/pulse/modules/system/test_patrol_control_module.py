"""PatrolControlModule contract tests (ADR-004 §6.1).

Two kinds of assertion:
  1. Contract-A shape: each of the 5 IntentSpec entries must declare
     when_to_use / when_not_to_use / schema with a required ``name`` field
     (except list) / correct mutates+risk+confirmation triplet.  This is
     the aggregate shape test, not per-field shadow (see testing
     constitution §虚假测试).
  2. End-to-end delegation: bind a real ``AgentRuntime`` with a real
     registered patrol, then call each handler and assert the
     observable result — state changes reflected by ``list_patrols`` and
     ``get_patrol_stats`` on the same runtime, not by mock call counts.
"""
from __future__ import annotations

import pytest

from pulse.core.runtime import AgentRuntime, RuntimeConfig
from pulse.modules.system.patrol.module import PatrolControlModule


_EXPECTED = {
    "system.patrol.list":    {"mutates": False, "risk": 0, "confirm": False, "requires_name": False},
    "system.patrol.status":  {"mutates": False, "risk": 0, "confirm": False, "requires_name": True},
    "system.patrol.enable":  {"mutates": True,  "risk": 2, "confirm": True,  "requires_name": True},
    "system.patrol.disable": {"mutates": True,  "risk": 1, "confirm": False, "requires_name": True},
    "system.patrol.trigger": {"mutates": True,  "risk": 2, "confirm": True,  "requires_name": True},
}


def test_module_declares_five_intents_with_contract_a_fields() -> None:
    mod = PatrolControlModule()
    intents_by_name = {intent.name: intent for intent in mod.intents}

    assert set(intents_by_name) == set(_EXPECTED), (
        "PatrolControlModule must expose exactly the 5 intents pinned by "
        "ADR-004 §6.1.4; adding/removing one requires an ADR amendment."
    )

    for name, expected in _EXPECTED.items():
        spec = intents_by_name[name]
        assert spec.when_to_use.strip(), f"{name}: when_to_use must be non-empty (ADR-001 contract A)"
        assert spec.when_not_to_use.strip(), f"{name}: when_not_to_use must be non-empty (ADR-001 contract A)"
        assert spec.examples, f"{name}: at least one example required"
        assert spec.mutates is expected["mutates"], f"{name}: mutates mismatch"
        assert spec.risk_level == expected["risk"], f"{name}: risk_level mismatch"
        assert spec.requires_confirmation is expected["confirm"], (
            f"{name}: requires_confirmation mismatch — high-risk (enable/trigger) must demand HITL"
        )

        schema = spec.parameters_schema
        assert schema.get("type") == "object"
        assert schema.get("additionalProperties") is False
        required = list(schema.get("required") or [])
        if expected["requires_name"]:
            assert "name" in required, f"{name}: schema must require ``name``"
            assert schema["properties"]["name"]["type"] == "string"
        else:
            assert required == [], f"{name}: list has no required params"


def _register_noop_patrol(rt: AgentRuntime, name: str, *, enabled: bool = True) -> None:
    rt.register_patrol(
        name=name,
        handler=lambda ctx: None,
        peak_interval=60,
        offpeak_interval=120,
        enabled=enabled,
        active_hours_only=False,
        token_budget=1000,
    )


@pytest.fixture
def bound_module() -> tuple[PatrolControlModule, AgentRuntime]:
    rt = AgentRuntime(config=RuntimeConfig())
    mod = PatrolControlModule()
    mod.bind_runtime(rt)
    return mod, rt


def test_list_handler_reflects_runtime_registered_patrols(bound_module) -> None:
    mod, rt = bound_module
    _register_noop_patrol(rt, "alpha")
    _register_noop_patrol(rt, "beta", enabled=False)

    out = mod._list_handler()

    assert out["ok"] is True
    assert out["total"] == 2
    returned_names = sorted(p["name"] for p in out["patrols"])
    assert returned_names == ["alpha", "beta"]


def test_status_handler_returns_snapshot_for_known_and_error_for_unknown(bound_module) -> None:
    mod, rt = bound_module
    _register_noop_patrol(rt, "alpha")

    ok_out = mod._status_handler(name="alpha")
    assert ok_out["ok"] is True
    assert ok_out["name"] == "alpha"
    assert ok_out["patrol"]["name"] == "alpha"

    miss_out = mod._status_handler(name="nope")
    assert miss_out["ok"] is False
    assert miss_out["name"] == "nope"
    assert "not found" in miss_out["error"]


def test_enable_handler_default_triggers_once_so_user_sees_effect(bound_module) -> None:
    """End-to-end: default ``trigger_now=True`` flips enabled AND runs
    one tick immediately.

    Why this matters behaviorally: a user typing "开启自动回复" expects
    an observable effect now, not one patrol interval later. The handler
    composes ``enable_patrol`` + ``run_patrol_once`` to give that
    guarantee without changing the kernel API (``enable_patrol`` itself
    still returns ``bool``). Observability comes through three
    co-changing facts: enabled flag flipped, handler ran once (call
    sink non-empty), and total_runs bumped on the stats snapshot.
    """
    mod, rt = bound_module
    executed: list[str] = []
    rt.register_patrol(
        name="alpha",
        handler=lambda ctx: executed.append(ctx.task_id),
        peak_interval=60,
        offpeak_interval=120,
        enabled=False,
        active_hours_only=False,
        token_budget=1000,
    )

    out = mod._enable_handler(name="alpha")

    assert out["ok"] is True
    assert out["name"] == "alpha"
    assert out["enabled"] is True
    assert "first_run" in out, (
        "default trigger_now=True must expose first_run so Brain can surface "
        "the one-shot outcome to the user without a second round-trip."
    )
    assert out["first_run"]["ok"] is True
    assert out["first_run"]["last_outcome"] == "completed"
    assert len(executed) == 1, "enable(trigger_now=True) must run the handler exactly once"
    assert rt.get_patrol_stats("alpha")["enabled"] is True
    assert rt.get_patrol_stats("alpha")["stats"]["total_runs"] == 1


def test_enable_handler_without_trigger_now_only_flips_enabled(bound_module) -> None:
    """``trigger_now=False`` is the "arm but don't fire" escape hatch
    for users who want to hand-control the first run (e.g. during
    debugging or when they explicitly say "挂起来, 先别跑").
    """
    mod, rt = bound_module
    executed: list[str] = []
    rt.register_patrol(
        name="alpha",
        handler=lambda ctx: executed.append(ctx.task_id),
        peak_interval=60,
        offpeak_interval=120,
        enabled=False,
        active_hours_only=False,
        token_budget=1000,
    )

    out = mod._enable_handler(name="alpha", trigger_now=False)

    assert out == {"ok": True, "name": "alpha", "enabled": True}
    assert rt.get_patrol_stats("alpha")["enabled"] is True
    assert executed == [], "trigger_now=False must skip the one-shot run"
    assert rt.get_patrol_stats("alpha")["stats"].get("total_runs", 0) == 0


def test_disable_handler_flips_observable_state(bound_module) -> None:
    mod, rt = bound_module
    _register_noop_patrol(rt, "alpha", enabled=True)

    disable_out = mod._disable_handler(name="alpha")
    assert disable_out == {"ok": True, "name": "alpha", "enabled": False}
    assert rt.get_patrol_stats("alpha")["enabled"] is False


def test_enable_intent_schema_declares_trigger_now_default_true(bound_module) -> None:
    """IntentSpec contract: the schema Brain sees must make trigger_now
    a visible, defaulted boolean. Without this, Brain has no way to
    learn "enable implies one-shot" and would keep misrouting to
    job.chat.process for the 'please start and handle the current
    backlog' class of utterance (the regression that motivated this
    test)."""
    mod, _ = bound_module
    enable_spec = next(i for i in mod.intents if i.name == "system.patrol.enable")

    trigger_prop = enable_spec.parameters_schema["properties"].get("trigger_now")
    assert trigger_prop is not None, "enable schema must expose trigger_now"
    assert trigger_prop["type"] == "boolean"
    assert trigger_prop["default"] is True, (
        "default must be True — aligns tool semantics with what '开启' means "
        "to the user (start AND show me it working)."
    )
    assert "name" in enable_spec.parameters_schema.get("required", [])
    assert "trigger_now" not in enable_spec.parameters_schema.get("required", []), (
        "trigger_now is optional — defaulted, not required."
    )


def test_enable_disable_unknown_patrol_returns_fail_loud_payload(bound_module) -> None:
    mod, _ = bound_module

    for handler in (mod._enable_handler, mod._disable_handler):
        out = handler(name="does_not_exist")
        assert out["ok"] is False
        assert out["name"] == "does_not_exist"
        assert "not found" in out["error"] or "not controllable" in out["error"]


def test_trigger_handler_runs_patrol_and_returns_outcome(bound_module) -> None:
    mod, rt = bound_module
    executed: list[str] = []
    rt.register_patrol(
        name="alpha",
        handler=lambda ctx: executed.append(ctx.task_id),
        peak_interval=60,
        offpeak_interval=120,
        enabled=True,
        active_hours_only=False,
        token_budget=1000,
    )

    out = mod._trigger_handler(name="alpha")

    assert out["ok"] is True
    assert out["task_name"] == "alpha"
    assert out["last_outcome"] == "completed"
    assert len(executed) == 1


def test_unbound_module_raises_runtime_error(bound_module) -> None:
    """If the module is not attached to a runtime (misconfigured server
    startup), handlers must fail loudly, not silently return empty dicts
    (coding constitution §类型 A: except + return {}). RuntimeError is
    the explicit signal that bind_runtime wiring is broken."""
    unbound = PatrolControlModule()
    with pytest.raises(RuntimeError, match="AgentRuntime"):
        unbound._list_handler()
