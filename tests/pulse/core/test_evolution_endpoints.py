from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pulse.core.config import get_settings
from pulse.core.server import create_app

pytestmark = pytest.mark.usefixtures("postgres_test_db")


def _create_isolated_app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:pulse@localhost:15433/pulse")
    monkeypatch.setenv("PULSE_CORE_MEMORY_PATH", str(tmp_path / "core_memory.json"))
    monkeypatch.setenv("PULSE_GOVERNANCE_AUDIT_PATH", str(tmp_path / "governance_audit.json"))
    monkeypatch.setenv("PULSE_DPO_PAIRS_PATH", str(tmp_path / "dpo_pairs.jsonl"))
    monkeypatch.setenv("PULSE_EVOLUTION_SOUL_MODE", "supervised")
    monkeypatch.setenv("PULSE_SOUL_CONFIG_PATH", str(tmp_path / "soul.yaml"))
    get_settings.cache_clear()
    app = create_app()
    get_settings.cache_clear()
    return app


def test_evolution_reflect_audit_and_rollback_endpoints(tmp_path, monkeypatch) -> None:
    app = _create_isolated_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        mode_resp = client.get("/api/evolution/governance/mode")
        assert mode_resp.status_code == 200
        mode_data = mode_resp.json()
        assert mode_data["result"]["change_modes"]["soul_update"] == "supervised"

        reflect_resp = client.post(
            "/api/evolution/reflect",
            json={
                "user_text": "以后默认用深圳，我不喜欢外包岗位",
                "assistant_text": "收到",
                "metadata": {"collect_dpo": True},
            },
        )
        assert reflect_resp.status_code == 200
        reflect_data = reflect_resp.json()
        assert reflect_data["ok"] is True

        status_resp = client.get("/api/evolution/status")
        assert status_resp.status_code == 200
        status_data = status_resp.json()
        assert status_data["audit_total"] >= 1
        assert status_data["audit_in_24h"] >= 1
        assert status_data["archival_total"] >= 1
        assert status_data["dpo_pairs_total"] >= 1
        assert "governance_risk_modes" in status_data
        assert "governance_change_risk_modes" in status_data

        audits_resp = client.get("/api/evolution/audits", params={"limit": 20})
        assert audits_resp.status_code == 200
        audits_data = audits_resp.json()
        assert audits_data["total"] >= 1
        change_id = next(
            (item["change_id"] for item in audits_data["items"] if item.get("status") == "applied"),
            "",
        )
        assert change_id

        rollback_preview = client.post("/api/evolution/rollback", json={"change_id": change_id, "confirm": False})
        assert rollback_preview.status_code == 200
        assert rollback_preview.json()["needs_confirmation"] is True

        rollback_resp = client.post("/api/evolution/rollback", json={"change_id": change_id, "confirm": True})
        assert rollback_resp.status_code == 200
        assert rollback_resp.json()["ok"] is True

        reflect_style_resp = client.post(
            "/api/evolution/reflect",
            json={"user_text": "回答尽量简短一点", "assistant_text": "收到"},
        )
        assert reflect_style_resp.status_code == 200
        pending_audits_resp = client.get("/api/evolution/audits", params={"status": "pending_approval", "limit": 20})
        assert pending_audits_resp.status_code == 200
        pending_items = pending_audits_resp.json()["items"]
        assert len(pending_items) >= 1
        pending_change_id = pending_items[0]["change_id"]

        approve_preview = client.post(
            "/api/evolution/governance/approve",
            json={"change_id": pending_change_id, "confirm": False},
        )
        assert approve_preview.status_code == 200
        assert approve_preview.json()["needs_confirmation"] is True
        approve_resp = client.post(
            "/api/evolution/governance/approve",
            json={"change_id": pending_change_id, "confirm": True},
        )
        assert approve_resp.status_code == 200
        assert approve_resp.json()["ok"] is True

        stats_resp = client.get("/api/evolution/audits/stats", params={"window_hours": 24})
        assert stats_resp.status_code == 200
        stats_data = stats_resp.json()["result"]
        assert stats_data["total"] >= 1
        assert "by_status" in stats_data

        filtered_resp = client.get(
            "/api/evolution/audits",
            params={"limit": 20, "change_type": "soul_update", "status": "applied"},
        )
        assert filtered_resp.status_code == 200
        assert filtered_resp.json()["total"] >= 1

        update_mode_resp = client.post(
            "/api/evolution/governance/mode",
            json={"change_type": "prefs_update", "risk_level": "high", "mode": "gated"},
        )
        assert update_mode_resp.status_code == 200
        assert (
            update_mode_resp.json()["result"]["change_risk_mode_overrides"]["prefs_update"]["high"] == "gated"
        )

        dpo_status = client.get("/api/learning/dpo/status")
        assert dpo_status.status_code == 200
        assert dpo_status.json()["total"] >= 1
        dpo_recent = client.get("/api/learning/dpo/recent", params={"limit": 10})
        assert dpo_recent.status_code == 200
        assert dpo_recent.json()["total"] >= 1
        manual_dpo = client.post(
            "/api/learning/dpo/collect",
            json={
                "prompt": "用户要求更简洁",
                "chosen": "给出简洁结构化回答",
                "rejected": "长篇铺垫",
            },
        )
        assert manual_dpo.status_code == 200
        assert manual_dpo.json()["ok"] is True

        archival_resp = client.post("/api/memory/archival/query", json={"subject": "user", "limit": 10})
        assert archival_resp.status_code == 200
        assert archival_resp.json()["total"] >= 1
