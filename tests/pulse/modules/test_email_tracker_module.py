from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pulse.core.server import create_app

pytestmark = pytest.mark.usefixtures("postgres_test_db")

def test_email_tracker_process_fetch_and_heartbeat_routes() -> None:
    app = create_app()
    with TestClient(app) as client:
        health_resp = client.get("/api/modules/email/tracker/health")
        one_resp = client.post(
            "/api/modules/email/tracker/process-one",
            json={
                "sender": "hr@demo.com",
                "subject": "面试邀请",
                "body": "请明天来面试",
            },
        )
        fetch_resp = client.post(
            "/api/modules/email/tracker/fetch-process",
            json={"max_items": 5, "mark_seen": False},
        )
        status_resp = client.get("/api/modules/email/tracker/heartbeat/status")

    assert health_resp.status_code == 200
    assert health_resp.json()["runtime"]["mode"] == "imap_unconfigured"

    assert one_resp.status_code == 200
    one_data = one_resp.json()
    assert one_data["classification"]["email_type"] == "interview_invite"
    assert one_data["updated_job_status"] == "interview"

    assert fetch_resp.status_code == 200
    fetch_data = fetch_resp.json()
    assert fetch_data["fetched_count"] >= 0
    assert fetch_data["processed_count"] >= 0
    assert isinstance(fetch_data["items"], list)
    assert fetch_data["source"] in {"imap_unconfigured", "imap"}

    assert status_resp.status_code == 200
    status_data = status_resp.json()
    assert "running" in status_data
    assert "interval_sec" in status_data
