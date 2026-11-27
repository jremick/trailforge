"""Add hotspot column, pg_trgm/report/admin indexes, OAuth code family link, and drop redundant indexes.

Revision ID: 0008_perf_and_security_indexes
Revises: 0007_ai_mcp_auth
Create Date: 2026-11-27 09:45:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008_perf_and_security_indexes"
down_revision: str | Sequence[str] | None = "0007_ai_mcp_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Enable pg_trgm for fast substring ILIKE search on tracks, POIs, and CAPAD parks
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))

    # 2. Promote is_obstacle_hotspot to a first-class typed boolean column + partial index
    #    (metadata-only ADD COLUMN in PostgreSQL 11+; backfill touches only actual hotspots)
    op.add_column(
        "track_segment_grades",
        sa.Column(
            "is_obstacle_hotspot",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE track_segment_grades
            SET is_obstacle_hotspot = true
            WHERE live_breakdown IS NOT NULL
              AND (live_breakdown->>'is_obstacle_hotspot') = 'true'
            """
        )
    )
    op.create_index(
        "ix_track_segment_grades_hotspot",
        "track_segment_grades",
        ["segment_id"],
        unique=False,
        postgresql_where=sa.text("is_obstacle_hotspot = true"),
    )

    # 3. Link OAuth authorization codes to minted token_family_id for OAuth 2.1 §4.1.2 replay revocation
    op.add_column(
        "oauth_authorization_codes",
        sa.Column("token_family_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    # 4. GIN trigram indexes for substring ILIKE searches across tracks, POIs, and parks
    op.create_index(
        "ix_tracks_name_trgm",
        "tracks",
        ["name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_pois_name_trgm",
        "pois",
        ["name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_capad_parks_name_trgm",
        "capad_parks",
        ["name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )

    # 5. Operational indexes for condition reports, photo lookup, and admin listing
    op.create_index(
        "ix_condition_reports_track_status_created",
        "condition_reports",
        ["track_id", "status", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_condition_reports_photo_url",
        "condition_reports",
        ["photo_url"],
        unique=False,
        postgresql_where=sa.text("photo_url IS NOT NULL"),
    )
    op.create_index(
        "ix_users_created_at",
        "users",
        [sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_invite_codes_created_at",
        "invite_codes",
        [sa.text("created_at DESC")],
        unique=False,
    )

    # 6. Normalize existing invite codes to trimmed uppercase so InviteCode.code == raw_code uses uq index
    op.execute(
        sa.text(
            """
            UPDATE invite_codes
            SET code = UPPER(TRIM(code))
            WHERE code <> UPPER(TRIM(code))
            """
        )
    )

    # 7. Drop redundant indexes duplicating existing primary-key prefixes or unique constraints
    op.drop_index("ix_invite_codes_code", table_name="invite_codes")
    op.drop_index("ix_segment_closure_links_segment_id", table_name="segment_closure_links")
    op.drop_index("ix_usage_metrics_hourly_bucket_hour", table_name="usage_metrics_hourly")

    # 8. Consolidate uq_users_email + ix_users_lower_email into a single unique lower(email) index
    op.execute(
        sa.text(
            """
            UPDATE users
            SET email = LOWER(TRIM(email))
            WHERE email <> LOWER(TRIM(email))
            """
        )
    )
    op.drop_index("ix_users_lower_email", table_name="users")
    op.drop_constraint("uq_users_email", "users", type_="unique")
    op.create_index(
        "uq_users_lower_email",
        "users",
        [sa.text("lower(email)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_users_lower_email", table_name="users")
    op.create_unique_constraint("uq_users_email", "users", ["email"])
    op.create_index("ix_users_lower_email", "users", [sa.text("lower(email)")], unique=False)

    op.create_index(
        "ix_usage_metrics_hourly_bucket_hour",
        "usage_metrics_hourly",
        ["bucket_hour"],
        unique=False,
    )
    op.create_index(
        "ix_segment_closure_links_segment_id",
        "segment_closure_links",
        ["segment_id"],
        unique=False,
    )
    op.create_index("ix_invite_codes_code", "invite_codes", ["code"], unique=False)

    op.drop_index("ix_invite_codes_created_at", table_name="invite_codes")
    op.drop_index("ix_users_created_at", table_name="users")
    op.drop_index("ix_condition_reports_photo_url", table_name="condition_reports")
    op.drop_index("ix_condition_reports_track_status_created", table_name="condition_reports")

    op.drop_index("ix_capad_parks_name_trgm", table_name="capad_parks")
    op.drop_index("ix_pois_name_trgm", table_name="pois")
    op.drop_index("ix_tracks_name_trgm", table_name="tracks")

    op.drop_column("oauth_authorization_codes", "token_family_id")

    op.drop_index("ix_track_segment_grades_hotspot", table_name="track_segment_grades")
    op.drop_column("track_segment_grades", "is_obstacle_hotspot")
