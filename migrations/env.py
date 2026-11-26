"""Alembic migration environment configured for PostGIS + GeoAlchemy2."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from geoalchemy2 import alembic_helpers
from sqlalchemy import engine_from_config, pool

import trailforge.db.models  # noqa: F401 — ensure all ORM models are registered on Base.metadata
from trailforge.config import get_settings
from trailforge.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Exclude PostGIS/TIGER extension system tables from autogenerate diffs
_EXCLUDED_TABLES = frozenset({"spatial_ref_sys", "topology", "layer"})


def include_object(object_, name, type_, reflected, compare_to):
    if type_ == "table" and name in _EXCLUDED_TABLES:
        return False
    return alembic_helpers.include_object(object_, name, type_, reflected, compare_to)


def get_database_url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        process_revision_directives=alembic_helpers.writer,
        render_item=alembic_helpers.render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = get_database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # Restrict reflection/DDL to public schema at connection startup so PostGIS
        # tiger/topology tables never pollute `alembic check` and SQLAlchemy 2.0
        # autobegin transactions are never disturbed.
        connect_args={"options": "-c search_path=public"},
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            process_revision_directives=alembic_helpers.writer,
            render_item=alembic_helpers.render_item,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
