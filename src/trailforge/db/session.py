"""Database engine, session helpers, and PostGIS health diagnostics."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from trailforge.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


@dataclass(frozen=True, slots=True)
class DbHealthStatus:
    """Snapshot of PostGIS connectivity and Alembic schema state."""

    db_ok: bool
    postgis_version: str | None = None
    schema_revision: str | None = None


def get_engine(database_url: str | None = None) -> Engine:
    """Return a lean pooled SQLAlchemy engine sized for 16 GB homelab hosts."""
    global _engine, _session_factory
    if database_url is not None:
        return create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            future=True,
        )
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            future=True,
        )
        _session_factory = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    """Dispose and reset the module-level engine (used during shutdown and tests)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a managed database session."""
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    session = _session_factory()
    try:
        yield session
    finally:
        session.close()


def check_db_health(engine: Engine | None = None) -> DbHealthStatus:
    """Verify PostgreSQL connectivity, PostGIS availability, and Alembic schema revision."""
    target_engine = engine or get_engine()
    try:
        with target_engine.connect() as conn:
            postgis_version = conn.execute(text("SELECT PostGIS_Version()")).scalar_one_or_none()
            has_alembic = conn.execute(
                text("SELECT to_regclass('public.alembic_version')")
            ).scalar_one_or_none()
            schema_revision: str | None = None
            if has_alembic is not None:
                schema_revision = conn.execute(
                    text("SELECT version_num FROM alembic_version LIMIT 1")
                ).scalar_one_or_none()
        return DbHealthStatus(
            db_ok=True,
            postgis_version=str(postgis_version) if postgis_version is not None else None,
            schema_revision=str(schema_revision) if schema_revision is not None else None,
        )
    except SQLAlchemyError:
        return DbHealthStatus(db_ok=False, postgis_version=None, schema_revision=None)
