from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pulse.core.server import create_app

pytestmark = pytest.mark.usefixtures("postgres_test_db")


def test_intel_techradar_collect_report_and_push_routes() -> None:
    app = create_app()
    with TestClient(app) as client:
        health_resp = client.get("/api/modules/intel/techradar/health")
        collect_resp = client.post(
            "/api/modules/intel/techradar/collect",
            json={"keyword": "MCP", "max_items": 5, "source": "real"},
        )
        report_resp = client.get(
            "/api/modules/intel/techradar/daily-report",
            params={"keyword": "MCP", "max_items": 4},
        )
        push_resp = client.post(
            "/api/modules/intel/techradar/daily-push",
            json={"keyword": "MCP", "max_items": 4, "channel": "feishu"},
        )
        schedule_status_resp = client.get("/api/modules/intel/techradar/schedule/status")
        schedule_trigger_resp = client.post("/api/modules/intel/techradar/schedule/trigger")
        schedule_start_resp = client.post("/api/modules/intel/techradar/schedule/start")
        schedule_stop_resp = client.post("/api/modules/intel/techradar/schedule/stop")

    assert health_resp.status_code == 200
    assert health_resp.json()["status"] == "ok"
    assert health_resp.json()["mode"] == "web_search"
    assert health_resp.json()["collect_pipeline"] == "web_search"

    assert collect_resp.status_code == 200
    collect_data = collect_resp.json()
    assert collect_data["source"] == "web_search"
    assert collect_data["requested_source"] == "real"
    assert collect_data["source_alias_applied"] is True
    assert collect_data["collect_pipeline"] == "web_search"
    assert collect_data["total"] == len(collect_data["items"])
    assert collect_data["total"] <= 5
    assert "persisted_docs" in collect_data

    assert report_resp.status_code == 200
    report_data = report_resp.json()
    assert report_data["summary"]["topic_count"] == len(report_data["items"])
    assert report_data["summary"]["topic_count"] <= 4

    assert push_resp.status_code == 200
    push_data = push_resp.json()
    assert push_data["ok"] is True
    assert push_data["pushed_count"] == push_data["report"]["summary"]["topic_count"]
    assert "delivery" in push_data

    assert schedule_status_resp.status_code == 200
    schedule_status = schedule_status_resp.json()
    assert "tasks" in schedule_status
    assert "intel_techradar.daily_push" in schedule_status["tasks"]

    assert schedule_trigger_resp.status_code == 200
    trigger_data = schedule_trigger_resp.json()
    assert trigger_data["ok"] is True
    assert any(task == "intel_techradar.daily_push" for task in trigger_data["ran_tasks"])

    assert schedule_start_resp.status_code == 200
    assert schedule_start_resp.json()["ok"] is True

    assert schedule_stop_resp.status_code == 200
    assert schedule_stop_resp.json()["ok"] is True
