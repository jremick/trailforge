"""Backfill revocable trip contribution keys and clear pre-cohort grades.

Revision ID: 0010_community_telemetry
Revises: 0009_search_trigram_track_grade
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0010_community_telemetry"
down_revision: str | Sequence[str] | None = "0009_search_trigram_track_grade"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _backfill_contribution_key_ids() -> None:
    from trailforge.config import get_settings

    # The former application derived hashes using the key that was current at upload time.
    # Use retained earlier keys during migration without persisting any of the secrets.
    cfg = get_settings()
    keys = tuple(dict.fromkeys(filter(None, (
        cfg.privacy_secret_key,
        *cfg.previous_privacy_secret_keys,
    ))))
    conn = op.get_bind()
    trips = sa.text("""
        SELECT id, contribution_nonce
        FROM trips
        WHERE visibility = 'community_anonymous'
    """)
    save_hash = sa.text("""
        UPDATE trips SET contribution_key_id = :key_id
        WHERE id = :trip_id AND contribution_key_id IS NULL AND (
            EXISTS (SELECT 1 FROM telemetry_observations WHERE contribution_hash = :digest)
            OR EXISTS (SELECT 1 FROM unmapped_track_candidates WHERE contribution_hash = :digest)
        )
    """)
    for batch in conn.execute(trips).partitions(1000):
        for key in keys:
            key_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
            conn.execute(save_hash, [
                {
                    "trip_id": trip_id,
                    "key_id": key_id,
                    "digest": hmac.new(
                        key.encode("utf-8"),
                        f"trailforge:contrib:v1:{trip_id}:{nonce}".encode(),
                        hashlib.sha256,
                    ).hexdigest(),
                }
                for trip_id, nonce in batch
            ])


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("This migration requires an online database to backfill withdrawal keys")

    op.add_column("trips", sa.Column("contribution_key_id", sa.String(length=32)))
    op.add_column("trips", sa.Column(
        "community_consent_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()
    ))
    _backfill_contribution_key_ids()
    op.create_index("ix_trips_contribution_key_id", "trips", ["contribution_key_id"])
    for name, axis in (("public_cell_lon", "X"), ("public_cell_lat", "Y")):
        op.add_column("unmapped_track_candidates", sa.Column(
            name, sa.Double(),
            sa.Computed(
                f"ST_{axis}(ST_SnapToGrid(ST_LineInterpolatePoint(geom, 0.5), 0.02))",
                persisted=True,
            ),
            nullable=False,
        ))
    op.create_index(
        "ix_unmapped_track_candidates_public_recent", "unmapped_track_candidates",
        [sa.text("created_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("status <> 'rejected'"),
    )

    # Existing aggregates were computed per traversal, sometimes including unlinked legacy rows.
    # Keep terrain and rainfall, and let the worker restore aggregates from verified cohorts.
    op.execute("""
        WITH affected AS (
            SELECT segment_id, baseline_grade,
                   baseline_score + COALESCE(
                       (rainfall_context ->> 'wet_score_adjustment')::double precision, 0
                   ) AS wet_score,
                   COALESCE((rainfall_context ->> 'wet_score_adjustment')::double precision, 0)
                       AS wet_adjustment
            FROM track_segment_grades
            WHERE sample_count > 0 OR live_grade IS NOT NULL OR live_score IS NOT NULL
               OR live_breakdown IS NOT NULL OR is_obstacle_hotspot
               OR last_driven_at IS NOT NULL OR avg_speed_kmh IS NOT NULL
               OR avg_duration_s IS NOT NULL OR vehicle_class_split <> '{}'::jsonb
        )
        UPDATE track_segment_grades AS grades
        SET live_grade = NULL, live_score = NULL, live_breakdown = NULL,
            effective_grade = CASE
                WHEN affected.wet_adjustment <= 0 THEN affected.baseline_grade
                WHEN affected.wet_score < 25 THEN 'Easy'
                WHEN affected.wet_score < 50 THEN 'Medium'
                WHEN affected.wet_score < 75 THEN 'Hard'
                ELSE 'Extreme'
            END,
            confidence_level = 'baseline_only', confidence_score = 0, sample_count = 0,
            last_driven_at = NULL, avg_speed_kmh = NULL, avg_duration_s = NULL,
            vehicle_class_split = '{}'::jsonb, is_obstacle_hotspot = FALSE,
            live_computed_at = NULL
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

    op.add_column(
        "condition_reports",
        sa.Column("eligible_resolve_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute("""
        WITH eligible AS (
            SELECT votes.report_id, COUNT(*) FILTER (
                WHERE votes.vote_type = 'resolved'
                  AND votes.created_at >= users.created_at + interval '7 days'
            ) AS eligible_count
            FROM condition_report_votes AS votes
            JOIN users ON users.id = votes.user_id
            GROUP BY votes.report_id
        )
        UPDATE condition_reports AS reports
        SET eligible_resolve_count = eligible.eligible_count,
            status = CASE
                WHEN eligible.eligible_count >= 3 AND eligible.eligible_count > reports.confirm_count
                    THEN 'resolved'
                WHEN reports.resolve_count > 0 AND reports.status IN ('resolved', 'stale')
                    THEN 'active'
                ELSE reports.status
            END
        FROM eligible
        WHERE reports.id = eligible.report_id AND reports.status != 'hidden'
    """)

    op.add_column("offline_packs", sa.Column("community_telemetry_policy", sa.Text()))
    op.add_column("offline_packs", sa.Column("community_telemetry_revision", sa.Uuid()))
    op.create_table(
        "offline_pack_telemetry_revisions",
        sa.Column("region_id", sa.Text(), primary_key=True),
        sa.Column("revision", sa.Uuid(), nullable=False),
    )

def downgrade() -> None:
    # Old derivatives cannot be reconstructed without the previous privacy policy.
    op.drop_table("offline_pack_telemetry_revisions")
    op.drop_column("offline_packs", "community_telemetry_revision")
    op.drop_column("offline_packs", "community_telemetry_policy")
    op.drop_column("condition_reports", "eligible_resolve_count")
    op.drop_index("ix_unmapped_track_candidates_public_recent", "unmapped_track_candidates")
    op.drop_column("unmapped_track_candidates", "public_cell_lat")
    op.drop_column("unmapped_track_candidates", "public_cell_lon")
    op.drop_index("ix_trips_contribution_key_id", table_name="trips")
    op.drop_column("trips", "community_consent_confirmed")
    op.drop_column("trips", "contribution_key_id")
