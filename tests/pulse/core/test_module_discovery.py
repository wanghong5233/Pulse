from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pulse.core.server import create_app

pytestmark = pytest.mark.usefixtures("postgres_test_db")


def test_health_lists_phase1_modules() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    modules = set(data.get("modules", []))
    assert {
        "hello",
        "job_greet",
        "job_chat",
        "email_tracker",
        "intel",
    }.issubset(modules)
