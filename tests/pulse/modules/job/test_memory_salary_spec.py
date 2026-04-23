"""P2-B regression guard: ``salary_floor_monthly`` retains raw unit spec.

Audit of trace_f3bda835ed94 showed the user saying "300元/天" collapsed into
a bare int ``7`` (K/月) in JobMemory, losing the original
``{amount:300, unit:yuan, period:day}`` context. That made it impossible to
explain to the user **why** the filter behaved as it did, and hid upstream
extractor bugs (e.g. a wrong ``work_days_per_month``) under a round number.

Contract the tests below pin down:

1. ``set_hard_constraint("salary_floor_monthly", {amount, unit, period})``
   **preserves** the raw spec alongside the monthly K conversion.
2. ``get_hard_constraints().salary_floor_spec`` returns the normalized raw
   spec dict; ``salary_floor_monthly`` keeps the integer K/月 value so
   downstream matcher / prompt code stays unchanged.
3. Bare integer inputs remain supported for backward compatibility (legacy
   fact rows written before this change) — they simply produce
   ``salary_floor_spec = None``.
4. ``render_snapshot()`` surfaces the raw unit context so the audit log and
   LLM prompt can cite the user's original wording, not just the derived
   number.
5. Invalid specs fail loud (``ValueError``), never silently degrade to a
   bare int — that was the exact anti-pattern that produced the
   ``7 K/月`` artefact to begin with.
"""

from __future__ import annotations

from typing import Any

import pytest

from pulse.core.memory.workspace_memory import WorkspaceMemory
from pulse.modules.job.memory import JobMemory


class _FakeWorkspaceDB:
    """Minimal SQL stub mirroring the one in test_job_memory_dedup."""

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
            self.rows.append(
                {
                    "workspace_id": workspace_id,
                    "key": key,
                    "value": value,
                    "source": source,
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
            )
            return None

        if norm.startswith("delete from workspace_facts where workspace_id = %s and key = %s"):
            ws, key = p
            self.rows = [
                r for r in self.rows
                if not (r["workspace_id"] == ws and r["key"] == key)
            ]
            return None

        if norm.startswith(
            "select key, value, source, updated_at from workspace_facts where workspace_id"
        ):
            ws, prefix_pattern = p
            prefix = prefix_pattern.rstrip("%")
            out = []
            for row in sorted(self.rows, key=lambda r: r["key"]):
                if row["workspace_id"] == ws and (
                    not prefix or row["key"].startswith(prefix)
                ):
                    out.append(
                        (row["key"], row["value"], row["source"], row["updated_at"])
                    )
            return out

        if norm.startswith(
            "select count(*) from workspace_facts where workspace_id = %s and key like"
        ):
            ws, pat = p
            prefix = pat.rstrip("%")
            return (
                sum(
                    1
                    for r in self.rows
                    if r["workspace_id"] == ws and r["key"].startswith(prefix)
                ),
            )

        if norm.startswith(
            "delete from workspace_facts where workspace_id = %s and key like"
        ):
            ws, pat = p
            prefix = pat.rstrip("%")
            self.rows = [
                r for r in self.rows
                if not (r["workspace_id"] == ws and r["key"].startswith(prefix))
            ]
            return None

        raise AssertionError(f"unexpected SQL in fake DB: {sql}")


@pytest.fixture()
def memory() -> JobMemory:
    return JobMemory(
        workspace_memory=WorkspaceMemory(db_engine=_FakeWorkspaceDB()),
        workspace_id="job.test",
    )


# ─────────────────────────────────────────────────────────────
# Structured spec round-trip
# ─────────────────────────────────────────────────────────────


def test_set_salary_floor_with_structured_spec_preserves_source(
    memory: JobMemory,
) -> None:
    """300元/天 (22 工作日) → 6.6 K/月 ≈ 7;  spec 必须完整保留原始三要素."""
    memory.set_hard_constraint(
        "salary_floor_monthly",
        {"amount": 300, "unit": "yuan", "period": "day"},
    )

    hc = memory.get_hard_constraints()
    assert hc.salary_floor_monthly == 7, (
        f"300 yuan × 22 天 / 1000 ≈ 6.6 → round to 7 K/月; "
        f"got {hc.salary_floor_monthly!r}"
    )
    assert hc.salary_floor_spec == {
        "amount": 300.0,
        "unit": "yuan",
        "period": "day",
        "work_days_per_month": 22,
    }, (
        "salary_floor_spec must echo the *normalized* raw spec so the audit "
        "log can cite the user's original wording"
    )


def test_set_salary_floor_structured_spec_honors_custom_work_days(
    memory: JobMemory,
) -> None:
    memory.set_hard_constraint(
        "salary_floor_monthly",
        {
            "amount": 500,
            "unit": "yuan",
            "period": "day",
            "work_days_per_month": 20,
        },
    )

    hc = memory.get_hard_constraints()
    assert hc.salary_floor_monthly == 10, (
        f"500 × 20 / 1000 = 10 K/月; got {hc.salary_floor_monthly!r}"
    )
    assert hc.salary_floor_spec == {
        "amount": 500.0,
        "unit": "yuan",
        "period": "day",
        "work_days_per_month": 20,
    }


def test_set_salary_floor_structured_spec_k_yuan_monthly(
    memory: JobMemory,
) -> None:
    memory.set_hard_constraint(
        "salary_floor_monthly",
        {"amount": 25, "unit": "k_yuan", "period": "month"},
    )

    hc = memory.get_hard_constraints()
    assert hc.salary_floor_monthly == 25
    assert hc.salary_floor_spec == {
        "amount": 25.0,
        "unit": "k_yuan",
        "period": "month",
    }


def test_set_salary_floor_bare_int_is_backward_compatible(
    memory: JobMemory,
) -> None:
    """Legacy callers that already did the conversion must still work —
    they simply get ``salary_floor_spec = None`` (no raw context available)."""
    memory.set_hard_constraint("salary_floor_monthly", 25)

    hc = memory.get_hard_constraints()
    assert hc.salary_floor_monthly == 25
    assert hc.salary_floor_spec is None


def test_snapshot_exposes_salary_spec(memory: JobMemory) -> None:
    memory.set_hard_constraint(
        "salary_floor_monthly",
        {"amount": 8, "unit": "k_yuan", "period": "month"},
    )

    snap = memory.snapshot()
    assert snap.hard_constraints.salary_floor_monthly == 8
    assert snap.hard_constraints.salary_floor_spec == {
        "amount": 8.0,
        "unit": "k_yuan",
        "period": "month",
    }


def test_prompt_section_quotes_original_salary_spec(memory: JobMemory) -> None:
    memory.set_hard_constraint(
        "salary_floor_monthly",
        {"amount": 300, "unit": "yuan", "period": "day"},
    )

    rendered = memory.snapshot().to_prompt_section()
    assert "7 K/月" in rendered, "derived monthly K must still appear"
    assert "300" in rendered and "yuan" in rendered and "day" in rendered, (
        "to_prompt_section must cite the raw (amount, unit, period) so the "
        "audit/LLM prompt can explain *why* the filter uses 7 K/月"
    )


# ─────────────────────────────────────────────────────────────
# Validation: invalid specs fail loud
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_spec",
    [
        {"amount": 0, "unit": "yuan", "period": "day"},
        {"amount": -5, "unit": "yuan", "period": "day"},
        {"amount": 300, "unit": "moon_stones", "period": "day"},
        {"amount": 300, "unit": "yuan", "period": "fortnight"},
        {"amount": "not_a_number", "unit": "yuan", "period": "day"},
        {"unit": "yuan", "period": "day"},  # missing amount
    ],
)
def test_set_salary_floor_invalid_spec_raises(
    memory: JobMemory, bad_spec: dict[str, Any]
) -> None:
    """Invalid spec → fail loud. Never silently degrade to ``None`` or to a
    bare int — that is what produced the original P2-B artefact."""
    with pytest.raises(ValueError):
        memory.set_hard_constraint("salary_floor_monthly", bad_spec)


# ─────────────────────────────────────────────────────────────
# HardConstraints value object contract
# ─────────────────────────────────────────────────────────────


def test_hard_constraints_to_dict_includes_salary_spec(
    memory: JobMemory,
) -> None:
    memory.set_hard_constraint(
        "salary_floor_monthly",
        {"amount": 15, "unit": "k_yuan", "period": "month"},
    )
    hc = memory.get_hard_constraints()
    as_dict = hc.to_dict()

    assert as_dict["salary_floor_monthly"] == 15
    assert as_dict["salary_floor_spec"] == {
        "amount": 15.0,
        "unit": "k_yuan",
        "period": "month",
    }


def test_hard_constraints_is_empty_when_no_salary_set(memory: JobMemory) -> None:
    """Empty salary must **not** be populated as an empty spec dict."""
    hc = memory.get_hard_constraints()
    assert hc.salary_floor_monthly is None
    assert hc.salary_floor_spec is None
    assert hc.is_empty()
