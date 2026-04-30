from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pulse.core.server import create_app

pytestmark = pytest.mark.usefixtures("postgres_test_db")


def test_runtime_stop_disarms_patrols_by_default() -> None:
    app = create_app()
    with TestClient(app) as client:
        name = "api_stop_disarm_probe"
        app.state.agent_runtime.register_patrol(
            name=name,
            handler=lambda ctx: None,
            peak_interval=60,
            offpeak_interval=120,
            enabled=False,
            active_hours_only=False,
            token_budget=1000,
        )

        enable_resp = client.post(f"/api/runtime/patrols/{name}/enable")
        assert enable_resp.status_code == 200
        assert bool(enable_resp.json().get("ok"))

        stop_resp = client.post("/api/runtime/stop")
        assert stop_resp.status_code == 200
        payload = stop_resp.json()
        assert payload["ok"] is True
        result = payload["result"]
        assert result["disarm_patrols"] is True

        disarm = result["disarm"]
        assert isinstance(disarm, dict)
        touched = set(disarm.get("disabled") or []) | set(disarm.get("already_disabled") or [])
        assert name in touched

        status_resp = client.get(f"/api/runtime/patrols/{name}")
        assert status_resp.status_code == 200
        status_payload = status_resp.json()
        assert status_payload["ok"] is True
        assert bool(status_payload["result"]["enabled"]) is False


def test_runtime_stop_allows_skip_disarm_when_explicit_false() -> None:
    app = create_app()
    with TestClient(app) as client:
        name = "api_stop_skip_disarm_probe"
        app.state.agent_runtime.register_patrol(
            name=name,
            handler=lambda ctx: None,
            peak_interval=60,
            offpeak_interval=120,
            enabled=False,
            active_hours_only=False,
            token_budget=1000,
        )

        enable_resp = client.post(f"/api/runtime/patrols/{name}/enable")
        assert enable_resp.status_code == 200
        assert bool(enable_resp.json().get("ok"))

        stop_resp = client.post("/api/runtime/stop", json={"disarm_patrols": False})
        assert stop_resp.status_code == 200
        payload = stop_resp.json()
        assert payload["ok"] is True
        result = payload["result"]
        assert result["disarm_patrols"] is False
        assert result["disarm"] is None

        status_resp = client.get(f"/api/runtime/patrols/{name}")
        assert status_resp.status_code == 200
        status_payload = status_resp.json()
        assert status_payload["ok"] is True
        assert bool(status_payload["result"]["enabled"]) is True


def test_lifespan_shutdown_disarms_patrols_on_testclient_exit() -> None:
    app = create_app()
    name = "lifespan_shutdown_disarm_probe"
    with TestClient(app) as client:
        app.state.agent_runtime.register_patrol(
            name=name,
            handler=lambda ctx: None,
            peak_interval=60,
            offpeak_interval=120,
            enabled=False,
            active_hours_only=False,
            token_budget=1000,
        )
        enable_resp = client.post(f"/api/runtime/patrols/{name}/enable")
        assert enable_resp.status_code == 200
        assert bool(enable_resp.json().get("ok"))

        status_resp = client.get(f"/api/runtime/patrols/{name}")
        assert status_resp.status_code == 200
        assert bool(status_resp.json()["result"]["enabled"]) is True

    after_shutdown = app.state.agent_runtime.get_patrol_stats(name)
    assert isinstance(after_shutdown, dict)
    assert bool(after_shutdown["enabled"]) is False
