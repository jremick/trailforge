"""Add planned_routes Tier C table for saved multi-track routes.

Revision ID: 0002_planned_routes
Revises: 0001_initial_spatial_schema
Create Date: 2026-11-26 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_planned_routes"
down_revision: str | Sequence[str] | None = "0001_initial_spatial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "planned_routes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("track_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "waypoints_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("vehicle_class", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "vehicle_class IN ('AWD','Stock 4WD','Touring 4WD','Modified 4WD')",
            name="vehicle_class_valid",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_planned_routes_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_planned_routes"),
    )
    op.create_index("ix_planned_routes_user_id", "planned_routes", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_planned_routes_user_id", table_name="planned_routes")
    op.drop_table("planned_routes")
