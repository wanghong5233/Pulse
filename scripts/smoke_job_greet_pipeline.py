"""Smoke test: job greet pipeline — F3 hard filter + F4 dedup + F6 preview short-circuit.

验证第二轮架构改动:
  - F3: snapshot.hard_constraints (preferred_location / salary_floor / experience_level)
        在 matcher LLM 之前**硬过滤**违反约束的 JD, 而且只在"证据清晰"时丢弃.
  - F4: repository.all_greeted_urls ∪ snapshot.active_items("application_event")
        的 URL 集合做跨天去重; 成功的 greet 会被 ``_record_application_events``
        写成 MemoryItem, 保证 JobMemory 里有"已投递"显式痕迹.
  - F6: confirm_execute=False 时预览不调 greeter LLM, 给占位文本; 真执行路径
        (confirm_execute=True) 才 compose.

跑:
  cd Pulse && PYTHONPATH=src python scripts/smoke_job_greet_pipeline.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pulse.modules.job.greet.service import (  # noqa: E402
    GreetPolicy,
    JobGreetService,
    _detect_city_structured,
    _parse_salary_range_k,
)
from pulse.modules.job.memory import (  # noqa: E402
    HardConstraints,
    JobMemorySnapshot,
    MemoryItem,
)


# ──────────────────────────────────────────────────────────────
# Fakes
# ──────────────────────────────────────────────────────────────


class _FakeConnector:
    provider_name = "fake"
    execution_ready = True

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items
        self.greet_calls: list[dict[str, Any]] = []

    def health(self) -> dict[str, Any]:
        return {"ok": True}

    def scan_jobs(
        self,
        *,
        keyword: str,
        max_items: int,
        max_pages: int,
        job_type: str,
        city: str | None = None,
    ) -> dict[str, Any]:
        _ = city  # smoke fake 忽略 city, greet service 侧按 preferred_location fan-out
        return {
            "items": list(self._items),
            "pages_scanned": 1,
            "source": "fake",
            "errors": [],
        }

    def fetch_job_detail(self, *, job_id: str, source_url: str) -> dict[str, Any]:
        return {"detail": {}}

    def greet_job(self, *, job: dict[str, Any], greeting_text: str, run_id: str) -> dict[str, Any]:
        self.greet_calls.append({"job": job, "greeting_text": greeting_text, "run_id": run_id})
        return {"ok": True, "status": "greeted", "provider": "fake", "source": "fake", "attempts": 1}

    def check_login(self) -> dict[str, Any]:
        return {"ok": True}


class _FakeRepository:
    def __init__(self, *, today: set[str] | None = None, all_urls: set[str] | None = None) -> None:
        self._today = set(today or [])
        self._all = set(all_urls or [])
        self.appended: list[list[dict[str, Any]]] = []

    @property
    def fallback_log_path(self) -> Path:
        return Path("/tmp/fake.jsonl")

    def today_greeted_urls(self) -> set[str]:
        return set(self._today)

    def all_greeted_urls(self) -> set[str]:
        return set(self._all) | set(self._today)

    def append_greet_logs(self, rows: list[dict[str, Any]]) -> None:
        self.appended.append(list(rows))


class _FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def send(self, message: Any) -> None:
        self.messages.append(message)


class _FakeJobMemory:
    """只关心 snapshot + record_item 这两个接口的最小替身。"""

    def __init__(self, snapshot: JobMemorySnapshot) -> None:
        self._snapshot = snapshot
        self.recorded: list[dict[str, Any]] = []

    def snapshot(self) -> JobMemorySnapshot:
        return self._snapshot

    def record_item(self, item: dict[str, Any]) -> Any:
        self.recorded.append(dict(item))
        return MemoryItem(
            id=item.get("id", "x"),
            type=item.get("type", "other"),
            target=item.get("target"),
            content=item.get("content", ""),
            raw_text=item.get("raw_text", ""),
            valid_from="2026-01-01T00:00:00+00:00",
            valid_until=None,
            superseded_by=None,
            created_at="2026-01-01T00:00:00+00:00",
        )


class _FakeMatcher:
    """把 matcher LLM 的行为钉死: 每条 JD 给 80 分 good verdict,
    这样 F3/F4 过滤后的条目才是 selected 的唯一来源。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def match(self, *, job: dict[str, Any], snapshot: Any, keyword: str) -> Any:
        self.calls.append({"job_id": job.get("job_id"), "keyword": keyword})
        from pulse.modules.job.greet.matcher import MatchResult
        return MatchResult(score=80.0, verdict="good", matched_signals=["fake"], concerns=[], reason="fake")


class _SpyGreeter:
    """记录是否被调过 — 用于验证 F6 预览短路。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def compose(self, *, job, snapshot, match, template, max_chars=90):  # noqa: ARG002
        self.calls.append({"job_id": job.get("job_id")})
        from pulse.modules.job.greet.greeter import GreetDraft
        return GreetDraft(greeting_text=f"hi {job.get('title')}", tone="professional", reason="spy")


def _make_snapshot(
    *,
    hc: HardConstraints | None = None,
    items: list[MemoryItem] | None = None,
) -> JobMemorySnapshot:
    return JobMemorySnapshot(
        workspace_id="ws.test",
        hard_constraints=hc or HardConstraints(),
        memory_items=list(items or []),
        resume=None,
        user_facts={},
        snapshot_version="test",
        rendered_at="2026-01-01T00:00:00+00:00",
    )


def _events_sink() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    events: list[tuple[str, dict[str, Any]]] = []

    def _emit(*, stage: str, status: str, trace_id: str | None = None, payload: dict[str, Any] | None = None) -> str:
        events.append((f"{stage}.{status}", dict(payload or {})))
        return trace_id or "trace_fake"

    return events, _emit


def _make_service(
    *,
    scan_items: list[dict[str, Any]],
    snapshot: JobMemorySnapshot,
    repo: _FakeRepository,
    policy: GreetPolicy | None = None,
    job_memory: _FakeJobMemory | None = None,
    matcher: _FakeMatcher | None = None,
    greeter: _SpyGreeter | None = None,
) -> tuple[JobGreetService, _FakeConnector, _SpyGreeter, _FakeMatcher, list[tuple[str, dict[str, Any]]]]:
    connector = _FakeConnector(scan_items)
    notifier = _FakeNotifier()
    events, emit = _events_sink()
    pol = policy or GreetPolicy(
        batch_size=5,
        match_threshold=60.0,
        daily_limit=10,
        default_keyword="python",
        greeting_template="你好，我对{job_title}很感兴趣",
        hitl_required=True,
    )
    # preferences 用 _FakeJobMemory, 但 service 的 _filter_by_preferences 期望
    # preferences.snapshot() 返回 JobMemorySnapshot, 这个 shape 一致就行。
    prefs = job_memory or _FakeJobMemory(snapshot)
    spy_matcher = matcher or _FakeMatcher()
    spy_greeter = greeter or _SpyGreeter()
    svc = JobGreetService(
        connector=connector,
        repository=repo,  # type: ignore[arg-type]
        policy=pol,
        notifier=notifier,  # type: ignore[arg-type]
        emit_stage_event=emit,
        preferences=prefs,  # type: ignore[arg-type]
        matcher=spy_matcher,  # type: ignore[arg-type]
        greeter=spy_greeter,  # type: ignore[arg-type]
    )
    return svc, connector, spy_greeter, spy_matcher, events


# ──────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────


def test_parse_salary_range_k() -> None:
    assert _parse_salary_range_k("20-40K") == (20, 40)
    assert _parse_salary_range_k("15K") == (15, 15)
    # 真实 BOSS 都带 "K" 单位; 没 K 的字符串不能被硬猜成月薪
    assert _parse_salary_range_k("20-40K·15薪") == (20, 40)
    assert _parse_salary_range_k("20-40·15薪") == (None, None)
    assert _parse_salary_range_k("面议") == (None, None)
    assert _parse_salary_range_k("") == (None, None)
    assert _parse_salary_range_k("200-400元/小时") == (None, None)
    print("  [ok] parse_salary_range_k")


def test_detect_city_structured_only() -> None:
    """Agent-first 契约: 硬过滤只认结构化字段, 不再扫 JD 正文 substring."""
    # 结构化字段缺失 → None (放行, 交给 matcher LLM)
    assert _detect_city_structured(None) is None
    assert _detect_city_structured({}) is None
    # 只认 detail 字段, 不再看任何 haystack 文本
    assert _detect_city_structured({"city": "杭州"}) == "杭州"
    assert _detect_city_structured({"address": "深圳福田"}) == "深圳福田"
    assert _detect_city_structured({"location": "上海浦东"}) == "上海浦东"
    # key 优先级 city > location > address
    assert _detect_city_structured({"city": "杭州", "address": "深圳"}) == "杭州"
    print("  [ok] detect_city_structured")


def test_f3_hard_constraint_drops_out_of_city() -> None:
    """preferred_location=['杭州'] 时, 上海岗位应被硬过滤掉 (结构化 detail.city)."""
    snapshot = _make_snapshot(hc=HardConstraints(preferred_location=["杭州"]))
    scan_items = [
        {"job_id": "a", "title": "Python 后端", "company": "A", "source_url": "https://a.example/1",
         "snippet": "Python 岗位", "detail": {"city": "上海"}},
        {"job_id": "b", "title": "Python 后端", "company": "B", "source_url": "https://b.example/1",
         "snippet": "Python 岗位", "detail": {"city": "杭州"}},
    ]
    repo = _FakeRepository()
    svc, _, _, matcher, events = _make_service(
        scan_items=scan_items, snapshot=snapshot, repo=repo,
    )
    result = svc.run_trigger(keyword="python", confirm_execute=False, fetch_detail=False)
    # 只有 b 进 matcher
    assert {c["job_id"] for c in matcher.calls} == {"b"}, matcher.calls
    # 过滤原因记录
    errors = result["errors"]
    assert any("skip:hc_location" in e and "上海" in e for e in errors), errors
    # 事件里有 hc_filter (F7 重构后 payload 汇总 pref+hc, hc_dropped 是纯 hc 那段)
    hc_events = [ev for ev in events if ev[0] == "trigger.hc_filter"]
    assert hc_events, hc_events
    hc_payload = hc_events[0][1]
    assert hc_payload["hc_dropped"] == 1, hc_payload
    assert hc_payload["pref_dropped"] == 0, hc_payload
    print("  [ok] F3 preferred_location 硬过滤")


def test_f3_hard_constraint_salary_floor_keeps_ambiguous() -> None:
    """salary_floor_monthly=25: '面议' 与 None 保守保留, '10-15K' 丢弃。"""
    snapshot = _make_snapshot(hc=HardConstraints(salary_floor_monthly=25))
    scan_items = [
        {"job_id": "lo", "title": "实习", "company": "L", "salary": "10-15K",
         "source_url": "https://l.example", "snippet": ""},
        {"job_id": "hi", "title": "全职", "company": "H", "salary": "30-45K",
         "source_url": "https://h.example", "snippet": ""},
        {"job_id": "unk", "title": "面议岗位", "company": "U", "salary": "面议",
         "source_url": "https://u.example", "snippet": ""},
        {"job_id": "none", "title": "无薪资字段", "company": "N", "salary": None,
         "source_url": "https://n.example", "snippet": ""},
    ]
    svc, _, _, matcher, _ = _make_service(
        scan_items=scan_items, snapshot=snapshot, repo=_FakeRepository(),
    )
    result = svc.run_trigger(keyword="python", confirm_execute=False, fetch_detail=False)
    kept_ids = {c["job_id"] for c in matcher.calls}
    # lo 必须被过滤; hi/unk/none 都保留 (后两者不确定, 保守)
    assert "lo" not in kept_ids and "hi" in kept_ids, kept_ids
    assert "unk" in kept_ids and "none" in kept_ids, kept_ids
    errors = result["errors"]
    assert any("skip:hc_salary" in e and "10-15K" in e for e in errors), errors
    print("  [ok] F3 salary_floor 只在证据清晰时丢弃")


def test_f4_dedup_against_historic_urls_and_application_events() -> None:
    """all_greeted_urls ∪ application_event items raw_text → 同一 URL 丢弃。"""
    applied_item = MemoryItem(
        id="app1",
        type="application_event",
        target="C",
        content="已投递 C",
        raw_text=json.dumps({"source_url": "https://c.example/1"}, ensure_ascii=False),
        valid_from="2026-01-01T00:00:00+00:00",
        valid_until=None,
        superseded_by=None,
        created_at="2026-01-01T00:00:00+00:00",
    )
    snapshot = _make_snapshot(items=[applied_item])
    scan_items = [
        # repository 里历史有的 URL
        {"job_id": "h1", "title": "岗位A", "company": "A", "source_url": "https://a.example/1",
         "salary": "30K", "snippet": ""},
        # application_event 里记录过的 URL
        {"job_id": "h2", "title": "岗位C", "company": "C", "source_url": "https://c.example/1",
         "salary": "30K", "snippet": ""},
        # 新的, 应该保留
        {"job_id": "h3", "title": "岗位D", "company": "D", "source_url": "https://d.example/1",
         "salary": "30K", "snippet": ""},
    ]
    repo = _FakeRepository(all_urls={"https://a.example/1"})
    svc, _, _, matcher, events = _make_service(
        scan_items=scan_items, snapshot=snapshot, repo=repo,
    )
    result = svc.run_trigger(keyword="python", confirm_execute=False, fetch_detail=False)
    kept = {c["job_id"] for c in matcher.calls}
    assert kept == {"h3"}, kept
    errors = result["errors"]
    assert sum(1 for e in errors if "skip:already_greeted" in e) == 2, errors
    dedup_events = [ev for ev in events if ev[0] == "trigger.dedup_filter"]
    assert dedup_events and dedup_events[0][1]["dropped"] == 2, dedup_events
    print("  [ok] F4 历史 URL + application_event 去重")


def test_f6_preview_does_not_call_greeter_llm() -> None:
    """confirm_execute=False 预览路径不应触发 greeter.compose()。"""
    snapshot = _make_snapshot()
    scan_items = [
        {"job_id": "p1", "title": "Python", "company": "A", "source_url": "https://a.example/1",
         "salary": "30K", "snippet": ""},
        {"job_id": "p2", "title": "Python", "company": "B", "source_url": "https://b.example/1",
         "salary": "30K", "snippet": ""},
    ]
    svc, _, greeter_spy, _, _ = _make_service(
        scan_items=scan_items, snapshot=snapshot, repo=_FakeRepository(),
    )
    result = svc.run_trigger(keyword="python", confirm_execute=False, fetch_detail=False)
    assert result["needs_confirmation"] is True
    assert len(result["matched_details"]) == 2
    # preview 占位文本不走 greeter LLM
    for row in result["matched_details"]:
        assert "(招呼文本将在你确认后生成)" in row["greeting_text"], row
    assert greeter_spy.calls == [], greeter_spy.calls
    assert result.get("greeting_deferred") is True
    print("  [ok] F6 预览模式短路 greeter LLM")


def test_f6_confirm_true_still_composes_per_item() -> None:
    """confirm_execute=True 时, greeter.compose() 必须被调, 每条 JD 一次。"""
    snapshot = _make_snapshot()
    scan_items = [
        {"job_id": "p1", "title": "Python", "company": "A", "source_url": "https://a.example/1",
         "salary": "30K", "snippet": ""},
    ]
    # 关闭 HITL 以便走真发送分支; policy.hitl_required=False 时无需 confirm_execute
    policy = GreetPolicy(
        batch_size=5, match_threshold=60.0, daily_limit=10,
        default_keyword="python", greeting_template="", hitl_required=False,
    )
    svc, conn, greeter_spy, _, _ = _make_service(
        scan_items=scan_items, snapshot=snapshot, repo=_FakeRepository(), policy=policy,
    )
    result = svc.run_trigger(keyword="python", confirm_execute=True, fetch_detail=False)
    assert result["needs_confirmation"] is False
    assert greeter_spy.calls and greeter_spy.calls[0]["job_id"] == "p1", greeter_spy.calls
    # 真发送: connector.greet_job 也被调
    assert len(conn.greet_calls) == 1
    print("  [ok] F6 真执行路径仍然 compose greeter")


def test_f4_records_application_event_after_success() -> None:
    """成功打完招呼要把 application_event 写进 JobMemory。"""
    snapshot = _make_snapshot()
    scan_items = [
        {"job_id": "z1", "title": "Python", "company": "Z", "source_url": "https://z.example/1",
         "salary": "30K", "snippet": ""},
    ]
    policy = GreetPolicy(
        batch_size=5, match_threshold=60.0, daily_limit=10,
        default_keyword="python", greeting_template="", hitl_required=False,
    )
    memory = _FakeJobMemory(snapshot)
    svc, _, _, _, _ = _make_service(
        scan_items=scan_items, snapshot=snapshot, repo=_FakeRepository(),
        policy=policy, job_memory=memory,
    )
    result = svc.run_trigger(keyword="python", confirm_execute=True, fetch_detail=False)
    assert result["greeted"] == 1, result
    assert len(memory.recorded) == 1
    rec = memory.recorded[0]
    assert rec["type"] == "application_event"
    assert rec["target"] == "Z"
    meta = json.loads(rec["raw_text"])
    assert meta["source_url"] == "https://z.example/1"
    assert meta["keyword"] == "python"
    print("  [ok] F4 成功投递后写 application_event")


def test_empty_snapshot_degrades_gracefully() -> None:
    """没有 JobMemory (preferences=None) 或 hc=empty 时, 不过滤任何条目。"""
    snapshot = _make_snapshot()
    scan_items = [
        {"job_id": "x", "title": "T", "company": "C", "source_url": "https://x.example",
         "salary": "30K", "snippet": ""},
    ]
    svc, _, _, matcher, _ = _make_service(
        scan_items=scan_items, snapshot=snapshot, repo=_FakeRepository(),
    )
    svc.run_trigger(keyword="python", confirm_execute=False, fetch_detail=False)
    assert {c["job_id"] for c in matcher.calls} == {"x"}
    print("  [ok] empty snapshot 不阻塞投递")


# ──────────────────────────────────────────────────────────────


def main() -> int:
    print("# smoke_job_greet_pipeline — R2 F3/F4/F6")
    tests = [
        test_parse_salary_range_k,
        test_detect_city_structured_only,
        test_f3_hard_constraint_drops_out_of_city,
        test_f3_hard_constraint_salary_floor_keeps_ambiguous,
        test_f4_dedup_against_historic_urls_and_application_events,
        test_f6_preview_does_not_call_greeter_llm,
        test_f6_confirm_true_still_composes_per_item,
        test_f4_records_application_event_after_success,
        test_empty_snapshot_degrades_gracefully,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except Exception as exc:   # noqa: BLE001
            failures += 1
            print(f"  [FAIL] {t.__name__}: {exc}")
            import traceback
            traceback.print_exc()
    if failures:
        print(f"FAILED: {failures}/{len(tests)}")
        return 1
    print(f"OK: {len(tests)}/{len(tests)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
