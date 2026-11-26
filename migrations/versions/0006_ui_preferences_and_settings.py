"""Add Tier C instance UI settings table and user UI preferences column.

Revision ID: 0006_ui_preferences_and_settings
Revises: 0005_user_token_version
Create Date: 2026-11-27 09:15:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006_ui_preferences_and_settings"
down_revision: str | Sequence[str] | None = "0005_user_token_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Tier C only — zero OSM geometries or raw OSM tag blobs.
    op.add_column(
        "users",
        sa.Column(
            "ui_preferences",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    op.create_table(
        "instance_ui_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "default_layer_order",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "default_layer_visibility",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("show_stack_telemetry_public", sa.Boolean(), nullable=True),
        sa.Column("updated_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="singleton_row"),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_instance_ui_settings_updated_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_instance_ui_settings"),
    )


def downgrade() -> None:
    op.drop_table("instance_ui_settings")
    op.drop_column("users", "ui_preferences")
