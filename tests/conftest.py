"""Shared pytest fixtures enforcing offline execution and providing API / PostGIS handles."""

from __future__ import annotations

import os
import socket
from collections.abc import Generator, Mapping
from pathlib import Path
from typing import Any

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from trailforge.api.health import get_health_checker
from trailforge.api.main import create_app
from trailforge.config import get_settings
from trailforge.db.session import DbHealthStatus, get_engine, reset_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://trailforge:trailforge@localhost:5432/trailforge_test"

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "db", "postgres", "testserver"})


def resolve_test_database_url(env: Mapping[str, str] | None = None) -> str:
    """Resolve the PostGIS database URL for integration tests.

    Prefers ``TEST_DATABASE_URL`` so local runs can target an isolated ``trailforge_test``
    database without racing the live Compose worker or mutating seeded dev data, while
    remaining compatible with CI pipelines (GitHub Actions / Cloud Build) that point
    ``TEST_DATABASE_URL`` and ``DATABASE_URL`` at an ephemeral CI PostGIS instance.
    """
    source = os.environ if env is None else env
    for key in ("TEST_DATABASE_URL", "DATABASE_URL"):
        val = (source.get(key) or "").strip()
        if val:
            return val
    return DEFAULT_TEST_DATABASE_URL


def _ensure_postgis_and_schema(engine: Engine) -> None:
    """Ensure the PostGIS extension is enabled and Alembic migrations are at head."""
    with engine.begin() as conn:
        has_postgis = conn.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'postgis'")
        ).scalar_one_or_none()
        if has_postgis is None:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))

    alembic_cfg = AlembicConfig()
    alembic_cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    head_rev = ScriptDirectory.from_config(alembic_cfg).get_current_head()

    with engine.connect() as conn:
        has_alembic = conn.execute(
            text("SELECT to_regclass('public.alembic_version')")
        ).scalar_one_or_none()
        current_rev: str | None = None
        if has_alembic is not None:
            current_rev = conn.execute(
                text("SELECT version_num FROM alembic_version LIMIT 1")
            ).scalar_one_or_none()

    if head_rev is not None and current_rev != head_rev:
        alembic_command.upgrade(alembic_cfg, "head")


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
    monkeypatch.setenv("TRAILFORGE_ENABLE_GEOCODER", "false")
    get_settings.cache_clear()


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
    """Return a SQLAlchemy Engine connected to an isolated/CI PostGIS database."""
    db_url = resolve_test_database_url()
    engine = get_engine(db_url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        engine.dispose()
        pytest.skip(f"PostGIS database not reachable at configured URL: {exc}")

    prev_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    get_settings.cache_clear()
    reset_engine()
    try:
        _ensure_postgis_and_schema(engine)
        yield engine
    finally:
        engine.dispose()
        reset_engine()
        if prev_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_database_url
        get_settings.cache_clear()
