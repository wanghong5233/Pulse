from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pulse.core.server import create_app

pytestmark = pytest.mark.usefixtures("postgres_test_db")


def test_intel_query_search_route_with_category_filter() -> None:
    app = create_app()
    with TestClient(app) as client:
        pre_health_resp = client.get("/api/modules/intel/query/health")
        ingest_resp = client.post(
            "/api/modules/intel/query/ingest",
            json={
                "source": "test",
                "items": [
                    {
                        "category": "techradar",
                        "title": "MCP ecosystem trend",
                        "content": "MCP server tools are growing rapidly in agent workflows.",
                        "summary": "Track MCP governance and security controls.",
                        "source_url": "https://example.com/mcp-trend",
                        "tags": ["mcp", "agent"],
                    },
                    {
                        "category": "techradar",
                        "title": "Agent observability baseline",
                        "content": "Observe tool latency, failure rate, and token cost together.",
                        "summary": "Build dashboards and alerts for tool-chain failures.",
                        "source_url": "https://example.com/obs",
                        "tags": ["observability", "cost"],
                    },
                    {
                        "category": "interview",
                        "title": "AI agent interview notes",
                        "content": "Interview focuses on routing, tool orchestration, and rollback.",
                        "summary": "Prepare incident recovery examples.",
                        "source_url": "https://example.com/interview",
                        "tags": ["interview"],
                    },
                ],
            },
        )
        health_resp = client.get("/api/modules/intel/query/health")
        resp = client.post(
            "/api/modules/intel/query/search",
            json={"query": "agent observability", "top_k": 3, "category": "techradar"},
        )

    assert pre_health_resp.status_code == 200
    assert pre_health_resp.json()["status"] == "ok"

    assert ingest_resp.status_code == 200
    ingest_data = ingest_resp.json()
    assert ingest_data["ok"] is True
    assert ingest_data["inserted"] >= 3

    assert health_resp.status_code == 200
    health_data = health_resp.json()
    assert health_data["status"] == "ok"
    assert health_data["mode"] == "knowledge_store"
    assert health_data["indexed_count"] >= 3
    assert health_data["knowledge_count"] >= 3

    assert resp.status_code == 200
    data = resp.json()
    assert data["top_k"] == 3
    assert data["total"] >= 1
    assert len(data["items"]) <= 3
    assert all(item["category"] == "techradar" for item in data["items"])
    assert all(0.0 <= float(item["score"]) <= 1.0 for item in data["items"])
    if len(data["items"]) >= 2:
        assert data["items"][0]["score"] >= data["items"][1]["score"]
