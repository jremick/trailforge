"""Offline unit tests for FastAPI /api/health, worker heartbeat, and architectural guardrails."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from trailforge import SERVICE_NAME, __version__
from trailforge.config import Settings
from trailforge.db.session import DbHealthStatus
from trailforge.worker import main as worker_main


def test_api_health_ok(healthy_client: TestClient) -> None:
    response = healthy_client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": __version__,
        "db_ok": True,
        "postgis_version": "3.4 USE_GEOS=1 USE_PROJ=1",
        "schema_revision": "0001_initial_spatial_schema",
    }


def test_api_health_degraded_returns_503(degraded_client: TestClient) -> None:
    response = degraded_client.get("/api/health")
    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["db_ok"] is False
    assert payload["postgis_version"] is None
    assert payload["schema_revision"] is None


def test_root_metadata(healthy_client: TestClient) -> None:
    response = healthy_client.get("/")
    assert response.status_code == 200
    payload = response.json()
    assert payload["service"] == SERVICE_NAME
    assert payload["health"] == "/api/health"


def test_default_settings_guardrails() -> None:
    settings = Settings()
    assert settings.privacy_trim_meters == 250
    assert settings.snow_line_default_m == 1000
    assert "http://localhost:5173" in settings.cors_origins_list


@pytest.mark.parametrize("invalid_trim", [0, 100, 249, 5001])
def test_privacy_trim_enforces_250m_minimum(invalid_trim: int) -> None:
    with pytest.raises(ValidationError):
        Settings(privacy_trim_meters=invalid_trim)


@pytest.mark.parametrize("valid_trim", [250, 500, 5000])
def test_privacy_trim_accepts_valid_range(valid_trim: int) -> None:
    assert Settings(privacy_trim_meters=valid_trim).privacy_trim_meters == valid_trim


def test_worker_tick_writes_heartbeat_when_db_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat = tmp_path / "worker-healthy"
    monkeypatch.setattr(
        worker_main,
        "check_db_health",
        lambda: DbHealthStatus(db_ok=True, postgis_version="3.4", schema_revision="0001"),
    )
    status = worker_main.run_worker_tick(heartbeat_path=heartbeat)
    assert status.db_ok is True
    assert heartbeat.exists()


def test_worker_tick_skips_heartbeat_when_db_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat = tmp_path / "worker-healthy"
    monkeypatch.setattr(
        worker_main,
        "check_db_health",
        lambda: DbHealthStatus(db_ok=False, postgis_version=None, schema_revision=None),
    )
    status = worker_main.run_worker_tick(heartbeat_path=heartbeat)
    assert status.db_ok is False
    assert not heartbeat.exists()


def test_offline_network_guard_blocks_external_hosts() -> None:
    with pytest.raises(RuntimeError, match="External network call"):
        socket.create_connection(("example.com", 80), timeout=1)
