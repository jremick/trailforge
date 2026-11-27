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
_api_engine: Engine | None = None
_api_session_factory: sessionmaker[Session] | None = None

DEFAULT_POOL_RECYCLE_S = 1800


@dataclass(frozen=True, slots=True)
class DbHealthStatus:
    """Snapshot of PostGIS connectivity and Alembic schema state."""

    db_ok: bool
    postgis_version: str | None = None
    schema_revision: str | None = None


def _build_engine(url: str, *, statement_timeout_ms: int | None = None) -> Engine:
    """Construct a lean SQLAlchemy engine with pool recycling and optional PG statement timeout."""
    connect_args: dict[str, str] = {}
    if (
        statement_timeout_ms is not None
        and statement_timeout_ms > 0
        and url.startswith("postgresql")
    ):
        connect_args["options"] = f"-c statement_timeout={int(statement_timeout_ms)}"
    kwargs: dict[str, object] = {
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 5,
        "pool_recycle": DEFAULT_POOL_RECYCLE_S,
        "hide_parameters": True,
        "future": True,
    }
    if connect_args:
        kwargs["connect_args"] = connect_args
    return create_engine(url, **kwargs)


def get_engine(
    database_url: str | None = None,
    *,
    statement_timeout_ms: int | None = None,
) -> Engine:
    """Return a lean pooled SQLAlchemy engine sized for 16 GB homelab hosts."""
    global _engine, _session_factory
    if database_url is not None:
        return _build_engine(database_url, statement_timeout_ms=statement_timeout_ms)
    if _engine is None:
        settings = get_settings()
        _engine = _build_engine(settings.database_url, statement_timeout_ms=statement_timeout_ms)
        _session_factory = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def _get_api_session_factory() -> sessionmaker[Session]:
    """Return API request sessionmaker enforcing PostgreSQL statement_timeout."""
    global _api_engine, _api_session_factory
    if _api_session_factory is None:
        settings = get_settings()
        timeout_ms = int(getattr(settings, "db_statement_timeout_ms", 15_000) or 0)
        _api_engine = _build_engine(
            settings.database_url,
            statement_timeout_ms=timeout_ms if timeout_ms > 0 else None,
        )
        _api_session_factory = sessionmaker(
            bind=_api_engine, autoflush=False, expire_on_commit=False
        )
    return _api_session_factory


def reset_engine() -> None:
    """Dispose and reset module-level engines (used during shutdown and tests)."""
    global _engine, _session_factory, _api_engine, _api_session_factory
    if _engine is not None:
        _engine.dispose()
    if _api_engine is not None:
        _api_engine.dispose()
    _engine = None
    _session_factory = None
    _api_engine = None
    _api_session_factory = None


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a managed database session with API statement_timeout."""
    factory = _get_api_session_factory()
    session = factory()
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
