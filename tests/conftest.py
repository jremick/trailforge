"""Shared pytest fixtures enforcing offline execution and providing API / PostGIS handles."""

from __future__ import annotations

import os
import socket
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from trailforge.api.health import get_health_checker
from trailforge.api.main import create_app
from trailforge.db.session import DbHealthStatus, get_engine

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "db", "postgres", "testserver"})


@pytest.fixture(autouse=True)
def block_external_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent accidental live network requests during offline unit/integration tests."""
    if request.node.get_closest_marker("live"):
        return

    real_create_connection = socket.create_connection

    def _guarded_create_connection(
        address: tuple[str, int], *args: Any, **kwargs: Any
    ) -> socket.socket:
        host = address[0]
        if host not in _LOCAL_HOSTS:
            raise RuntimeError(
                f"External network call to {host!r} blocked in offline test suite. "
                "Mark tests requiring external internet with @pytest.mark.live."
            )
        return real_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)


@pytest.fixture
def fixtures_dir() -> Path:
    """Return path to committed synthetic VIC/NSW test fixtures."""
    return FIXTURES_DIR


@pytest.fixture
def healthy_client() -> Generator[TestClient, None, None]:
    """FastAPI TestClient with mocked healthy PostGIS status (zero DB required)."""
    app = create_app()
    app.dependency_overrides[get_health_checker] = lambda: (
        lambda: DbHealthStatus(
            db_ok=True,
            postgis_version="3.4 USE_GEOS=1 USE_PROJ=1",
            schema_revision="0001_initial_spatial_schema",
        )
    )
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def degraded_client() -> Generator[TestClient, None, None]:
    """FastAPI TestClient simulating an unreachable PostGIS instance."""
    app = create_app()
    app.dependency_overrides[get_health_checker] = lambda: (
        lambda: DbHealthStatus(db_ok=False, postgis_version=None, schema_revision=None)
    )
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture(scope="session")
def postgis_engine() -> Generator[Engine, None, None]:
    """Return a SQLAlchemy Engine connected to a local/CI PostGIS database for integration tests."""
    db_url = os.environ.get(
        "TEST_DATABASE_URL",
        os.environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://trailforge:trailforge@localhost:5432/trailforge",
        ),
    )
    engine = get_engine(db_url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        engine.dispose()
        pytest.skip(f"PostGIS database not reachable at configured URL: {exc}")
    try:
        yield engine
    finally:
        engine.dispose()
