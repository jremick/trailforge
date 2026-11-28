"""Add explicit community trust and remove pre-attestation public derivatives.

Revision ID: 0013_community_contributor_trust
Revises: 0012_oauth_code_session_version
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_community_contributor_trust"
down_revision: str | Sequence[str] | None = "0012_oauth_code_session_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("community_trusted_at", sa.DateTime(timezone=True)))

    # Every report that automatic voting could have marked resolved or stale is
    # assessed again against its natural age. Explicit hidden states remain in force.
    op.execute("""
        WITH dates AS (
            SELECT id, LEAST(created_at, observed_at) AS report_at,
                   CASE report_type
                       WHEN 'snow_ice' THEN 5.0 WHEN 'deep_water' THEN 7.0
                       WHEN 'fallen_tree' THEN 14.0 WHEN 'track_clear' THEN 14.0
                       WHEN 'bog_hole' THEN 21.0 WHEN 'washout' THEN 30.0
                       WHEN 'locked_gate' THEN 60.0 WHEN 'private_property' THEN 90.0
                   END AS half_life
            FROM condition_reports
            WHERE (status IN ('active', 'stale') AND (
                   confirm_count > 0 OR resolve_count > 0 OR EXISTS (
                       SELECT 1 FROM condition_report_votes AS votes
                       WHERE votes.report_id = condition_reports.id
                   )
               ))
               OR (status = 'resolved' AND (
                   resolve_count > 0 OR eligible_resolve_count > 0 OR EXISTS (
                       SELECT 1 FROM condition_report_votes AS votes
                       WHERE votes.report_id = condition_reports.id
                         AND votes.vote_type = 'resolved'
                   )
               ))
        )
        UPDATE condition_reports AS report
        SET status = 'active',
            decay_score = round(power(0.5, greatest(0, extract(epoch FROM now() - dates.report_at))
                / (86400 * dates.half_life)), 4),
            expires_at = dates.report_at + make_interval(
                secs => dates.half_life * 86400 * ln(1.0 / 0.35) / ln(2.0)
            )
        FROM dates
        WHERE report.id = dates.id
    """)
    op.drop_column("condition_reports", "eligible_resolve_count")

    # No existing account is automatically trusted. Restore the terrain/weather
    # values on every segment whose stored results contained historical telemetry.
    op.execute("""
        WITH affected AS (
            SELECT segment_id, baseline_grade, baseline_score,
                   CASE WHEN jsonb_typeof(rainfall_context -> 'wet_score_adjustment') = 'number'
                        THEN (rainfall_context ->> 'wet_score_adjustment')::double precision
                        ELSE 0 END AS wet_adjustment
            FROM track_segment_grades
            WHERE sample_count > 0 OR live_grade IS NOT NULL OR live_breakdown IS NOT NULL
               OR live_score IS NOT NULL OR is_obstacle_hotspot
               OR last_driven_at IS NOT NULL OR avg_speed_kmh IS NOT NULL
               OR avg_duration_s IS NOT NULL OR vehicle_class_split <> '{}'::jsonb
               OR confidence_level <> 'baseline_only' OR confidence_score <> 0
        )
        UPDATE track_segment_grades AS grades
        SET live_grade = NULL, live_score = NULL, live_breakdown = NULL,
            effective_grade = CASE
                WHEN affected.wet_adjustment <= 0 THEN affected.baseline_grade
                WHEN affected.baseline_score + affected.wet_adjustment < 25 THEN 'Easy'
                WHEN affected.baseline_score + affected.wet_adjustment < 50 THEN 'Medium'
                WHEN affected.baseline_score + affected.wet_adjustment < 75 THEN 'Hard'
                ELSE 'Extreme'
            END,
            confidence_level = 'baseline_only', confidence_score = 0, sample_count = 0,
            last_driven_at = NULL, avg_speed_kmh = NULL, avg_duration_s = NULL,
            vehicle_class_split = '{}'::jsonb, is_obstacle_hotspot = FALSE, live_computed_at = NULL
        FROM affected
        WHERE grades.segment_id = affected.segment_id
    """)
    op.execute("""
        WITH track_ranks AS (
            SELECT tracks.track_id, MAX(CASE grades.effective_grade
                WHEN 'Easy' THEN 1 WHEN 'Medium' THEN 2
                WHEN 'Hard' THEN 3 WHEN 'Extreme' THEN 4 END) AS worst_rank
            FROM tracks
            LEFT JOIN track_segments AS segments
                ON segments.track_id = tracks.track_id AND segments.is_active
            LEFT JOIN track_segment_grades AS grades ON grades.segment_id = segments.segment_id
            GROUP BY tracks.track_id
        ), track_grades AS (
            SELECT track_id, CASE worst_rank WHEN 1 THEN 'Easy' WHEN 2 THEN 'Medium'
                WHEN 3 THEN 'Hard' WHEN 4 THEN 'Extreme' END AS effective_grade
            FROM track_ranks
        )
        UPDATE tracks SET effective_grade = track_grades.effective_grade
        FROM track_grades
        WHERE tracks.track_id = track_grades.track_id
          AND tracks.effective_grade IS DISTINCT FROM track_grades.effective_grade
    """)


def downgrade() -> None:
    op.add_column(
        "condition_reports",
        sa.Column("eligible_resolve_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.drop_column("users", "community_trusted_at")
