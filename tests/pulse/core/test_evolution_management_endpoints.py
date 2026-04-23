from __future__ import annotations

import json
import pytest

from fastapi.testclient import TestClient

from pulse.core.config import get_settings
from pulse.core.server import create_app

pytestmark = pytest.mark.usefixtures("postgres_test_db")


def _write_rules(path, *, soul_mode: str, prefs_mode: str = "autonomous") -> None:
    path.write_text(
        json.dumps(
            {
                "default_mode": "autonomous",
                "change_modes": {
                    "prefs_update": prefs_mode,
                    "soul_update": soul_mode,
                    "belief_mutation": "autonomous",
                },
                "risk_mode_overrides": {"critical": "gated"},
                "change_risk_mode_overrides": {
                    "soul_update": {"high": "supervised", "critical": "gated"},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _create_isolated_app(tmp_path, monkeypatch, rules_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:pulse@localhost:15433/pulse")
    monkeypatch.setenv("PULSE_CORE_MEMORY_PATH", str(tmp_path / "core_memory.json"))
    monkeypatch.setenv("PULSE_GOVERNANCE_AUDIT_PATH", str(tmp_path / "governance_audit.json"))
    monkeypatch.setenv("PULSE_DPO_PAIRS_PATH", str(tmp_path / "dpo_pairs.jsonl"))
    monkeypatch.setenv("PULSE_EVOLUTION_RULES_PATH", str(rules_path))
    monkeypatch.setenv("PULSE_SOUL_CONFIG_PATH", str(tmp_path / "soul.yaml"))
    monkeypatch.delenv("PULSE_EVOLUTION_DEFAULT_MODE", raising=False)
    monkeypatch.delenv("PULSE_EVOLUTION_PREFS_MODE", raising=False)
    monkeypatch.delenv("PULSE_EVOLUTION_SOUL_MODE", raising=False)
    monkeypatch.delenv("PULSE_EVOLUTION_BELIEF_MODE", raising=False)
    get_settings.cache_clear()
    app = create_app()
    get_settings.cache_clear()
    return app


def test_evolution_reload_export_and_dashboard_endpoints(tmp_path, monkeypatch) -> None:
    rules_path = tmp_path / "evolution_rules.json"
    _write_rules(rules_path, soul_mode="supervised")
    app = _create_isolated_app(tmp_path, monkeypatch, rules_path)

    with TestClient(app) as client:
        mode_before = client.get("/api/evolution/governance/mode")
        assert mode_before.status_code == 200
        assert mode_before.json()["result"]["change_modes"]["soul_update"] == "supervised"
        versions_before = client.get("/api/evolution/governance/versions", params={"limit": 10})
        assert versions_before.status_code == 200
        assert versions_before.json()["total"] >= 1

        # Mutate rules file on disk, then hot-reload without restarting service.
        _write_rules(rules_path, soul_mode="gated", prefs_mode="supervised")
        preview = client.post("/api/evolution/governance/reload", json={"confirm": False})
        assert preview.status_code == 200
        assert preview.json()["needs_confirmation"] is True

        reload_resp = client.post("/api/evolution/governance/reload", json={"confirm": True})
        assert reload_resp.status_code == 200
        assert reload_resp.json()["ok"] is True
        assert reload_resp.json()["new_version_id"]

        mode_after = client.get("/api/evolution/governance/mode")
        assert mode_after.status_code == 200
        mode_data = mode_after.json()["result"]
        assert mode_data["change_modes"]["soul_update"] == "gated"
        assert mode_data["change_modes"]["prefs_update"] == "supervised"
        manual_mode_update = client.post(
            "/api/evolution/governance/mode",
            json={"change_type": "prefs_update", "risk_level": "medium", "mode": "supervised", "persist": True},
        )
        assert manual_mode_update.status_code == 200
        assert manual_mode_update.json()["ok"] is True
        assert manual_mode_update.json()["persisted"] is True

        versions_after = client.get("/api/evolution/governance/versions", params={"limit": 20})
        assert versions_after.status_code == 200
        versions_data = versions_after.json()
        assert versions_data["total"] >= 2
        rollback_target_version_id = next(
            (
                item["version_id"]
                for item in versions_data["items"]
                if (item.get("rules") or {}).get("change_modes", {}).get("soul_update") == "supervised"
            ),
            "",
        )
        assert rollback_target_version_id

        diff_resp = client.get("/api/evolution/governance/versions/diff", params={"limit": 100})
        assert diff_resp.status_code == 200
        diff_data = diff_resp.json()
        assert diff_data["ok"] is True
        assert diff_data["summary"]["total"] >= 1
        assert diff_data["changes_total"] >= 1

        rollback_preview = client.post(
            "/api/evolution/governance/versions/rollback",
            json={"version_id": rollback_target_version_id, "confirm": False},
        )
        assert rollback_preview.status_code == 200
        assert rollback_preview.json()["needs_confirmation"] is True
        rollback_apply = client.post(
            "/api/evolution/governance/versions/rollback",
            json={"version_id": rollback_target_version_id, "confirm": True, "persist": True},
        )
        assert rollback_apply.status_code == 200
        assert rollback_apply.json()["ok"] is True
        assert rollback_apply.json()["persisted"] is True

        mode_after_rollback = client.get("/api/evolution/governance/mode")
        assert mode_after_rollback.status_code == 200
        assert mode_after_rollback.json()["result"]["change_modes"]["soul_update"] == "supervised"

        reflect_resp = client.post(
            "/api/evolution/reflect",
            json={"user_text": "以后默认用南京", "assistant_text": "收到"},
        )
        assert reflect_resp.status_code == 200
        assert reflect_resp.json()["ok"] is True
        reflect_resp2 = client.post(
            "/api/evolution/reflect",
            json={"user_text": "以后默认用苏州", "assistant_text": "收到"},
        )
        assert reflect_resp2.status_code == 200
        assert reflect_resp2.json()["ok"] is True

        export_json = client.get("/api/evolution/audits/export", params={"format": "json", "limit": 1})
        assert export_json.status_code == 200
        export_payload = export_json.json()
        assert export_payload["ok"] is True
        assert export_payload["format"] == "json"
        assert export_payload["total"] >= 1
        assert export_payload["next_cursor"] is not None

        export_json_next = client.get(
            "/api/evolution/audits/export",
            params={"format": "json", "limit": 1, "cursor": export_payload["next_cursor"]},
        )
        assert export_json_next.status_code == 200
        assert export_json_next.json()["ok"] is True

        export_empty = client.get(
            "/api/evolution/audits/export",
            params={"format": "json", "start_at": "2099-01-01T00:00:00+00:00", "limit": 10},
        )
        assert export_empty.status_code == 200
        assert export_empty.json()["total"] == 0

        export_csv = client.get("/api/evolution/audits/export", params={"format": "csv", "limit": 1})
        assert export_csv.status_code == 200
        assert "text/csv" in export_csv.headers.get("content-type", "")
        assert "change_id,timestamp,status" in export_csv.text
        assert export_csv.headers.get("X-Next-Cursor") is not None

        paged_audits = client.get("/api/evolution/audits", params={"limit": 1})
        assert paged_audits.status_code == 200
        paged_data = paged_audits.json()
        assert paged_data["total"] >= 1
        assert paged_data["next_cursor"] is not None

        dashboard = client.get("/api/evolution/dashboard", params={"window_hours": 24, "recent_limit": 8})
        assert dashboard.status_code == 200
        dashboard_data = dashboard.json()
        assert dashboard_data["ok"] is True
        assert "governance" in dashboard_data
        assert "audits" in dashboard_data
        assert "memory" in dashboard_data
        assert dashboard_data["audits"]["stats"]["total"] >= 1
        assert "trends" in dashboard_data["audits"]
        assert len(dashboard_data["audits"]["trends"]["hourly"]) >= 1
        assert len(dashboard_data["audits"]["trends"]["daily"]) >= 1
        assert "alerts" in dashboard_data["audits"]
