from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pulse.core.server import create_app

pytestmark = pytest.mark.usefixtures("postgres_test_db")


def test_feedback_loop_submit_triggers_evolution_pipeline() -> None:
    app = create_app()
    with TestClient(app) as client:
        stats_resp = client.get("/api/modules/system/feedback/stats")
        submit_resp = client.post(
            "/api/modules/system/feedback/submit",
            json={
                "type": "correction",
                "content": "默认城市 杭州，以后回答简短一点。",
                "assistant_text": "我会默认使用北京，并保持当前表达风格。",
                "collect_dpo": True,
                "session_id": "feedback-session",
            },
        )

    assert stats_resp.status_code == 200
    assert stats_resp.json()["module"] == "feedback_loop"
    assert stats_resp.json()["evolution_bound"] is True

    assert submit_resp.status_code == 200
    payload = submit_resp.json()
    assert payload["ok"] is True
    assert payload["recorded"] is True
    assert payload["assistant_text_present"] is True
    assert payload["collect_dpo"] is True
    assert payload["trace_id"]
    assert payload["evolution_error"] is None

    evolution = payload["evolution"]
    assert evolution["classification"] == "correction"
    assert evolution["preference_applied"]
    assert evolution["dpo_collected"] is not None
    assert evolution["dpo_collected"]["pair_id"].startswith("dpo_")
