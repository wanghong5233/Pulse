from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pulse.modules.job.chat.module import JobChatModule
from pulse.core.server import create_app

pytestmark = pytest.mark.usefixtures("postgres_test_db")


def test_boss_chat_requires_explicit_local_inbox_fallback(monkeypatch, tmp_path) -> None:
    inbox_path = tmp_path / "boss_chat_inbox.jsonl"
    inbox_path.write_text(
        json.dumps(
            {
                "conversation_id": "conv-1",
                "hr_name": "赵老师",
                "company": "Pulse Labs",
                "job_title": "AI Agent Intern",
                "latest_message": "请补充你的项目经历和到岗时间。",
                "latest_time": "刚刚",
                "unread_count": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PULSE_BOSS_CHAT_INBOX_PATH", str(inbox_path))
    monkeypatch.setenv("PULSE_BOSS_ALLOW_LOCAL_INBOX_FALLBACK", "false")

    module = JobChatModule()
    result = module.run_pull(
        max_conversations=10,
        unread_only=False,
        fetch_latest_hr=True,
        chat_tab="全部",
    )

    assert result["total"] == 0
    assert result["source"] == "boss_unconfigured"
    assert "local inbox fallback is disabled" in result["errors"]


def test_boss_chat_pull_and_process_routes(monkeypatch) -> None:
    monkeypatch.setenv("PULSE_BOSS_ALLOW_LOCAL_INBOX_FALLBACK", "true")
    app = create_app()
    with TestClient(app) as client:
        health_resp = client.get("/api/modules/job/chat/health")
        session_resp = client.get("/api/modules/job/chat/session/check")
        ingest_resp = client.post(
            "/api/modules/job/chat/inbox/ingest",
            json={
                "source": "test",
                "items": [
                    {
                        "hr_name": "赵老师",
                        "company": "Pulse Labs",
                        "job_title": "AI Agent Intern",
                        "latest_message": "请补充你的项目经历和到岗时间。",
                        "latest_time": "刚刚",
                        "unread_count": 1,
                    }
                ],
            },
        )
        process_resp = client.post(
            "/api/modules/job/chat/process",
            json={
                "max_conversations": 10,
                "unread_only": True,
                "profile_id": "default",
                "notify_on_escalate": True,
                "fetch_latest_hr": True,
                "auto_execute": False,
                "chat_tab": "未读",
                "confirm_execute": False,
            },
        )
        pull_resp = client.post(
            "/api/modules/job/chat/pull",
            json={
                "max_conversations": 10,
                "unread_only": False,
                "fetch_latest_hr": True,
                "chat_tab": "全部",
            },
        )
        pull_data = pull_resp.json()
        execute_preview_resp = client.post(
            "/api/modules/job/chat/execute",
            json={
                "conversation_id": pull_data["items"][0]["conversation_id"],
                "action": "reply_from_profile",
                "reply_text": "你好，这是测试回复",
                "confirm_execute": False,
            },
        )

    assert health_resp.status_code == 200
    assert health_resp.json()["runtime"]["mode"] in {"real_connector", "degraded_connector"}
    assert health_resp.json()["runtime"]["local_inbox_fallback_enabled"] is True
    assert session_resp.status_code == 200
    assert "status" in session_resp.json()
    assert ingest_resp.status_code == 200
    assert ingest_resp.json()["ok"] is True

    assert process_resp.status_code == 200
    process_data = process_resp.json()
    assert process_data["processed_count"] >= 1
    assert process_data["new_count"] >= 1
    assert isinstance(process_data["items"], list)
    assert "summary" in process_data
    assert "source" in process_data["summary"]

    assert pull_resp.status_code == 200
    data = pull_data
    assert data["total"] >= 1
    assert data["unread_total"] >= 0
    assert isinstance(data["items"], list)

    assert execute_preview_resp.status_code == 200
    execute_preview = execute_preview_resp.json()
    assert execute_preview["ok"] is True
    assert execute_preview["needs_confirmation"] is True


def test_patrol_forces_real_auto_execute_even_if_policy_defaults_are_preview_only() -> None:
    class _FakeService:
        def __init__(self) -> None:
            # Simulate the historical default that blocked real patrol execution:
            # chat_auto_execute=false + hitl_required=true.
            self.policy = SimpleNamespace(
                default_profile_id="default",
                auto_execute=False,
                hitl_required=True,
            )
            self.calls: list[dict[str, object]] = []

        def run_process(self, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(dict(kwargs))
            return {"ok": True, "items": []}

    fake = _FakeService()
    module = JobChatModule(service=fake)
    out = module._patrol(ctx=object())  # _patrol ignores ctx payload.

    assert out["ok"] is True
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["unread_only"] is True
    assert call["chat_tab"] == "未读"
    assert call["fetch_latest_hr"] is True
    assert call["auto_execute"] is True, (
        "patrol path must execute real actions after explicit user enable; "
        "preview-only defaults belong to interactive process intent, not patrol."
    )
    assert call["confirm_execute"] is True, (
        "enabling patrol is already an explicit confirmation; patrol ticks must "
        "not be blocked by per-turn HITL gates."
    )
