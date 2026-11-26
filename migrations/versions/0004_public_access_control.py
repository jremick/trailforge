"""Add public access control roles, invite_codes, and usage_metrics_hourly Tier C tables.

Revision ID: 0004_public_access_control
Revises: 0003_pan_au_source_tracks
Create Date: 2026-11-26 18:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004_public_access_control"
down_revision: str | Sequence[str] | None = "0003_pan_au_source_tracks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Update Tier C `users` table for public access control (role admin/member, suspension,
    #    and invite provenance). Strictly zero OSM ODbL impact.
    op.drop_constraint(op.f("ck_users_role_valid"), "users", type_="check")
    op.execute(sa.text("UPDATE users SET role = 'member' WHERE role NOT IN ('admin', 'member')"))
    op.alter_column(
        "users",
        "role",
        existing_type=sa.Text(),
        type_=sa.String(length=16),
        existing_nullable=False,
        server_default=sa.text("'member'"),
    )
    op.create_check_constraint(
        op.f("ck_users_role_valid"),
        "users",
        "role IN ('admin','member')",
    )
    op.add_column("users", sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("invited_by_code", sa.String(length=64), nullable=True))

    # 2. Tier C `invite_codes` table for invite-only public registration
    op.create_table(
        "invite_codes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=True),
        sa.Column(
            "role",
            sa.String(length=16),
            server_default=sa.text("'member'"),
            nullable=False,
        ),
        sa.Column("max_uses", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("use_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('admin','member')", name="role_valid"),
        sa.CheckConstraint("max_uses >= 1", name="max_uses_min"),
        sa.CheckConstraint("use_count >= 0", name="use_count_nonneg"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_invite_codes_created_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_invite_codes"),
        sa.UniqueConstraint("code", name="uq_invite_codes_code"),
    )
    op.create_index("ix_invite_codes_code", "invite_codes", ["code"], unique=False)

    # 3. Tier C `usage_metrics_hourly` table for zero-Redis hourly API usage telemetry
    op.create_table(
        "usage_metrics_hourly",
        sa.Column("bucket_hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("route_group", sa.String(length=48), nullable=False),
        sa.Column("request_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("bytes_out", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "rate_limited_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("bucket_hour", "route_group", name="pk_usage_metrics_hourly"),
    )
    op.create_index(
        "ix_usage_metrics_hourly_bucket_hour",
        "usage_metrics_hourly",
        ["bucket_hour"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_usage_metrics_hourly_bucket_hour", table_name="usage_metrics_hourly")
    op.drop_table("usage_metrics_hourly")

    op.drop_index("ix_invite_codes_code", table_name="invite_codes")
    op.drop_table("invite_codes")

    op.drop_column("users", "invited_by_code")
    op.drop_column("users", "disabled_at")

    op.drop_constraint(op.f("ck_users_role_valid"), "users", type_="check")
    op.execute(sa.text("UPDATE users SET role = 'user' WHERE role = 'member'"))
    op.alter_column(
        "users",
        "role",
        existing_type=sa.String(length=16),
        type_=sa.Text(),
        existing_nullable=False,
        server_default=sa.text("'user'"),
    )
    op.create_check_constraint(
        op.f("ck_users_role_valid"),
        "users",
        "role IN ('user','moderator','admin')",
    )
