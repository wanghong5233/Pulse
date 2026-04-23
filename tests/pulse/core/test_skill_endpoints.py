from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pulse.core.config import get_settings
from pulse.core.server import create_app

pytestmark = pytest.mark.usefixtures("postgres_test_db")


def _create_isolated_app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:pulse@localhost:15433/pulse")
    monkeypatch.setenv("PULSE_GENERATED_SKILLS_DIR", str(tmp_path / "generated_skills"))
    monkeypatch.setenv("PULSE_CORE_MEMORY_PATH", str(tmp_path / "core_memory.json"))
    monkeypatch.setenv("PULSE_SOUL_CONFIG_PATH", str(tmp_path / "soul.yaml"))
    get_settings.cache_clear()
    app = create_app()
    get_settings.cache_clear()
    return app


def test_skill_generate_activate_and_invoke_endpoints(tmp_path, monkeypatch) -> None:
    app = _create_isolated_app(tmp_path, monkeypatch)
    code = """
from pulse.core.tool import tool

@tool(
    name="btc_monitor",
    description="Return deterministic BTC monitor summary",
    schema={"type": "object", "properties": {"symbol": {"type": "string"}}},
)
def btc_monitor(args: dict[str, object]) -> dict[str, object]:
    symbol = str(args.get("symbol") or "BTC").upper()
    return {
        "symbol": symbol,
        "price_usd": 65000,
        "summary": f"{symbol} monitor is active",
    }
"""
    with TestClient(app) as client:
        gen_resp = client.post(
            "/api/skills/generate",
            json={
                "prompt": "Please monitor BTC price and return summary",
                "tool_name": "btc_monitor",
                "description": "BTC price monitor",
                "code": code,
            },
        )
        assert gen_resp.status_code == 200
        gen_data = gen_resp.json()
        assert "skill" in gen_data
        skill = gen_data["skill"]
        assert skill["status"] == "draft"
        skill_id = skill["skill_id"]
        tool_name = skill["tool_name"]

        preview_resp = client.post("/api/skills/activate", json={"skill_id": skill_id, "confirm": False})
        assert preview_resp.status_code == 200
        preview_data = preview_resp.json()
        assert preview_data["ok"] is False
        assert preview_data["needs_confirmation"] is True

        activate_resp = client.post("/api/skills/activate", json={"skill_id": skill_id, "confirm": True})
        assert activate_resp.status_code == 200
        activate_data = activate_resp.json()
        assert activate_data["ok"] is True
        assert tool_name in activate_data["result"]["activated_tools"]

        tools_resp = client.get("/api/brain/tools")
        assert tools_resp.status_code == 200
        names = {item["name"] for item in tools_resp.json()["items"]}
        assert tool_name in names

        call_resp = client.post(
            "/api/mcp/call",
            json={"name": tool_name, "arguments": {"symbol": "BTC"}},
        )
        assert call_resp.status_code == 200
        call_data = call_resp.json()
        assert call_data["ok"] is True
        assert call_data["mode"] == "local"
        assert "price_usd" in call_data["result"]
