from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pulse.core.server import create_app
from pulse.modules.job._connectors.boss.settings import get_boss_connector_settings
from pulse.modules.job.greet.matcher import JobSnapshotMatcher
from pulse.modules.job.greet.module import JobGreetModule
from pulse.modules.job.greet.service import (
    GreetPolicy,
    JobGreetService,
    _parse_salary_range_k,
)
from pulse.modules.job.memory import HardConstraints, JobMemorySnapshot

pytestmark = pytest.mark.usefixtures("postgres_test_db")

def test_boss_greet_scan_and_trigger_routes(monkeypatch) -> None:
    monkeypatch.setenv("PULSE_BOSS_PROVIDER", "web_search")
    monkeypatch.setenv("PULSE_BOSS_ALLOW_WEB_SEARCH_FALLBACK", "true")
    monkeypatch.setenv("PULSE_BOSS_ALLOW_SEED_FALLBACK", "true")
    get_boss_connector_settings.cache_clear()
    app = create_app()
    with TestClient(app) as client:
        health_resp = client.get("/api/modules/job/greet/health")
        session_resp = client.get("/api/modules/job/greet/session/check")
        scan_resp = client.post(
            "/api/modules/job/greet/scan",
            json={"keyword": "AI", "max_items": 5, "max_pages": 2},
        )
        trigger_resp = client.post(
            "/api/modules/job/greet/trigger",
            json={
                "keyword": "AI Agent",
                "batch_size": 3,
                "match_threshold": 60,
                "greeting_text": "你好",
                "job_type": "intern",
                "run_id": "run-1",
                "confirm_execute": False,
            },
        )

    assert health_resp.status_code == 200
    assert health_resp.json()["status"] == "ok"
    assert health_resp.json()["runtime"]["mode"] in {"real_connector", "degraded_connector"}
    assert health_resp.json()["runtime"]["provider"] == "boss_web_search"
    assert "provider" in health_resp.json()["runtime"]
    assert session_resp.status_code == 200
    assert "status" in session_resp.json()

    assert scan_resp.status_code == 200
    scan_data = scan_resp.json()
    assert scan_data["keyword"] == "AI"
    assert scan_data["total"] == len(scan_data["items"])
    assert scan_data["total"] <= 5

    assert trigger_resp.status_code == 200
    trigger_data = trigger_resp.json()
    # Fail-loud contract: search-only provider cannot execute real greet trigger.
    assert trigger_data["ok"] is False
    assert isinstance(trigger_data["matched_details"], list)
    assert trigger_data["execution_ready"] is False
    assert trigger_data["greeted"] == 0
    assert "execution-ready" in str(trigger_data.get("reason") or "")


def test_greet_patrol_executes_without_per_item_hitl() -> None:
    class _FakeService:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def run_trigger(self, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(dict(kwargs))
            return {"ok": True, "greeted": 1}

    fake = _FakeService()
    module = JobGreetModule(service=fake)
    out = module._patrol(ctx=object())  # _patrol ignores ctx payload.

    assert out["ok"] is True
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["confirm_execute"] is True, (
        "定时打招呼是用户启用后的无人干预自动化,不能被全局 HITL 默认值卡成预览。"
    )
    assert call["fetch_detail"] is True


def test_matcher_llm_failure_does_not_fallback_to_autosend_heuristic() -> None:
    class _BrokenLLM:
        def __init__(self) -> None:
            self.routes: list[str] = []

        def invoke_json(self, messages, *, route):  # type: ignore[no-untyped-def]
            self.routes.append(route)
            return "not-json"

    llm = _BrokenLLM()
    matcher = JobSnapshotMatcher(llm)
    result = matcher.match(
        job={
            "title": "AI Agent 实习",
            "snippet": "AI Agent 实习,大模型应用方向",
            "company": "Example",
        },
        snapshot=None,
        keyword="AI Agent 实习",
    )

    assert result.verdict == "skip"
    assert result.score == 0.0
    assert result.reason == "llm_required_no_heuristic_autosend"
    assert llm.routes == ["job_match"]


def test_matcher_prompt_preview_keeps_jd_tail_constraints() -> None:
    class _CapturingLLM:
        def __init__(self) -> None:
            self.user_prompt = ""

        def invoke_json(self, messages, *, route):  # type: ignore[no-untyped-def]
            _ = route
            self.user_prompt = str(messages[-1].content)
            return {
                "score": 88,
                "verdict": "good",
                "matched_signals": ["agent"],
                "concerns": [],
                "reason": "ok",
            }

    tail = "尾部关键信息：base杭州，日薪300元以上，AI Agent垂直实习"
    llm = _CapturingLLM()
    matcher = JobSnapshotMatcher(llm)

    matcher.match(
        job={
            "title": "AI Agent 实习",
            "snippet": "岗位职责：" + ("负责大模型应用开发。" * 600) + tail,
            "company": "Example",
        },
        snapshot=None,
        keyword="AI Agent 实习",
    )

    assert tail in llm.user_prompt


def test_patrol_keyword_uses_memory_target_role_when_no_explicit_keyword() -> None:
    service = JobGreetService(
        connector=object(),  # type: ignore[arg-type]
        repository=object(),  # type: ignore[arg-type]
        policy=GreetPolicy(
            batch_size=3,
            match_threshold=65,
            daily_limit=50,
            default_keyword="AI Agent 实习",
            greeting_template="",
            hitl_required=True,
        ),
        notifier=object(),  # type: ignore[arg-type]
        emit_stage_event=lambda **kwargs: str(kwargs.get("trace_id") or "trace-test"),
    )
    snapshot = JobMemorySnapshot(
        workspace_id="job.default",
        hard_constraints=HardConstraints(
            target_roles=["大模型应用开发 Agent 实习"],
        ),
    )

    assert service._resolve_trigger_keywords(  # noqa: SLF001
        keyword="AI Agent 实习",
        snapshot=snapshot,
    ) == ["大模型应用开发 Agent 实习"]
    assert service._resolve_trigger_keywords(  # noqa: SLF001
        keyword="后端实习",
        snapshot=snapshot,
    ) == ["后端实习"]


def test_patrol_keyword_fans_out_all_memory_target_roles() -> None:
    service = JobGreetService(
        connector=object(),  # type: ignore[arg-type]
        repository=object(),  # type: ignore[arg-type]
        policy=GreetPolicy(
            batch_size=3,
            match_threshold=65,
            daily_limit=50,
            default_keyword="AI Agent 实习",
            greeting_template="",
            hitl_required=True,
        ),
        notifier=object(),  # type: ignore[arg-type]
        emit_stage_event=lambda **kwargs: str(kwargs.get("trace_id") or "trace-test"),
    )
    snapshot = JobMemorySnapshot(
        workspace_id="job.default",
        hard_constraints=HardConstraints(
            target_roles=["大模型应用开发 Agent 实习", "初创公司 AI 工程实习"],
        ),
    )

    assert service._resolve_trigger_keywords(  # noqa: SLF001
        keyword="AI Agent 实习",
        snapshot=snapshot,
    ) == ["大模型应用开发 Agent 实习", "初创公司 AI 工程实习"]


def test_patrol_keyword_appends_intern_when_hard_constraint_is_intern() -> None:
    service = JobGreetService(
        connector=object(),  # type: ignore[arg-type]
        repository=object(),  # type: ignore[arg-type]
        policy=GreetPolicy(
            batch_size=3,
            match_threshold=65,
            daily_limit=50,
            default_keyword="AI Agent",
            greeting_template="",
            hitl_required=True,
        ),
        notifier=object(),  # type: ignore[arg-type]
        emit_stage_event=lambda **kwargs: str(kwargs.get("trace_id") or "trace-test"),
    )
    snapshot = JobMemorySnapshot(
        workspace_id="job.default",
        hard_constraints=HardConstraints(
            target_roles=["大模型应用开发 Agent"],
            experience_level="intern",
        ),
    )

    assert service._resolve_trigger_keywords(  # noqa: SLF001
        keyword="AI Agent",
        snapshot=snapshot,
    ) == ["大模型应用开发 Agent 实习"]


def test_trigger_scan_fans_out_target_role_keywords() -> None:
    class _FakeConnector:
        provider_name = "fake_boss"
        execution_ready = False

        def __init__(self) -> None:
            self.keywords: list[str] = []

        def scan_jobs(self, *, keyword, **kwargs):  # type: ignore[no-untyped-def]
            _ = kwargs
            self.keywords.append(str(keyword))
            return {
                "items": [
                    {
                        "job_id": f"id-{len(self.keywords)}",
                        "title": keyword,
                        "company": "Example",
                        "source_url": f"https://example.com/{len(self.keywords)}",
                    }
                ],
                "errors": [],
                "pages_scanned": 1,
                "scroll_count": 0,
                "exhausted": True,
                "source": "fake",
            }

    connector = _FakeConnector()
    service = JobGreetService(
        connector=connector,  # type: ignore[arg-type]
        repository=object(),  # type: ignore[arg-type]
        policy=GreetPolicy(
            batch_size=3,
            match_threshold=65,
            daily_limit=50,
            default_keyword="AI Agent 实习",
            greeting_template="",
            hitl_required=True,
        ),
        notifier=object(),  # type: ignore[arg-type]
        emit_stage_event=lambda **kwargs: str(kwargs.get("trace_id") or "trace-test"),
    )

    scan = service._run_trigger_scan(  # noqa: SLF001
        keywords=["大模型应用开发 Agent 实习", "初创公司 AI 工程实习"],
        max_items=30,
        max_pages=3,
        job_type="all",
        fetch_detail=False,
        trace_id="trace-test",
    )

    assert connector.keywords == ["大模型应用开发 Agent 实习", "初创公司 AI 工程实习"]
    assert scan["keywords"] == connector.keywords
    assert scan["total"] == 2


def test_salary_parser_supports_daily_intern_salary() -> None:
    assert _parse_salary_range_k("200-300元/天") == (4, 7)
    assert _parse_salary_range_k("日薪300元") == (7, 7)
    assert _parse_salary_range_k("20-40K") == (20, 40)
