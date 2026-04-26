from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pulse.core.server import create_app

pytestmark = pytest.mark.usefixtures("postgres_test_db")


def test_cli_ingest_dispatches_to_module() -> None:
    app = create_app()
    client = TestClient(app)

    response = client.post(
        "/api/channel/cli/ingest",
        json={
            "text": "ping",
            "user_id": "tester",
            "metadata": {"source": "test"},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["result"]["handled"] is True
    assert payload["result"]["mode"] == "brain"
    assert payload["result"]["route"]["target"] == "hello"
    assert "module.hello" in payload["result"]["brain"]["used_tools"]
    assert payload["result"]["result"]["module"] == "hello"


def test_feishu_event_challenge() -> None:
    app = create_app()
    client = TestClient(app)

    response = client.post("/api/channel/feishu/events", json={"challenge": "token-123"})
    assert response.status_code == 200
    assert response.json() == {"challenge": "token-123"}


def test_feishu_event_dispatches_prefix_intent() -> None:
    app = create_app()
    client = TestClient(app)

    payload = {
        "event": {
            "message": {
                "chat_id": "oc_abc",
                "message_id": "om_1",
                "message_type": "text",
                "content": '{"text":"/scan AI Agent"}',
            },
            "sender": {"sender_id": {"open_id": "ou_001"}},
        }
    }

    response = client.post("/api/channel/feishu/events", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["ignored"] is False
    assert body["result"]["route"]["intent"] == "boss.scan"
    assert body["result"]["route"]["target"] == "boss_greet"
    assert body["result"]["handled"] is True
    assert body["result"]["mode"] == "brain"
    assert "module.boss_greet" in body["result"]["brain"]["used_tools"]


def test_cli_ingest_dispatches_to_intel_search() -> None:
    app = create_app()
    client = TestClient(app)

    response = client.post(
        "/api/channel/cli/ingest",
        json={
            "text": "/intel search agent observability",
            "user_id": "tester-intel",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["result"]["route"]["intent"] == "intel.search"
    assert payload["result"]["route"]["target"] == "intel"
    assert payload["result"]["handled"] is True
    assert payload["result"]["mode"] == "brain"
