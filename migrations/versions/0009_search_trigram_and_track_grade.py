"""Add cached tracks.effective_grade, tracks.search_text, and trigram index.

Revision ID: 0009_search_trigram_track_grade
Revises: 0008_perf_and_security_indexes
Create Date: 2026-11-27 13:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_search_trigram_track_grade"
down_revision: str | Sequence[str] | None = "0008_perf_and_security_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EFFECTIVE_GRADE_CHECK_SQL = "effective_grade IN ('Easy','Medium','Hard','Extreme')"

_SYNC_SEARCH_TEXT_FN_SQL = """
CREATE OR REPLACE FUNCTION trailforge_sync_track_search_text()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.search_text := NULLIF(
        btrim(
            concat_ws(
                ' ',
                NULLIF(btrim(COALESCE(NEW.name, '')), ''),
                NULLIF(btrim(array_to_string(COALESCE(NEW.alt_names, ARRAY[]::text[]), ' ')), '')
            )
        ),
        ''
    );
    RETURN NEW;
END;
$$;
"""

_SYNC_SEARCH_TEXT_TRIGGER_SQL = """
DROP TRIGGER IF EXISTS trg_tracks_sync_search_text ON tracks;
CREATE TRIGGER trg_tracks_sync_search_text
BEFORE INSERT OR UPDATE OF name, alt_names ON tracks
FOR EACH ROW
EXECUTE FUNCTION trailforge_sync_track_search_text();
"""

_BACKFILL_SEARCH_TEXT_SQL = """
UPDATE tracks
SET search_text = NULLIF(
    btrim(
        concat_ws(
            ' ',
            NULLIF(btrim(COALESCE(name, '')), ''),
            NULLIF(btrim(array_to_string(COALESCE(alt_names, ARRAY[]::text[]), ' ')), '')
        )
    ),
    ''
)
"""

_BACKFILL_EFFECTIVE_GRADE_SQL = """
UPDATE tracks AS t
SET effective_grade = CASE agg.worst_rank
    WHEN 1 THEN 'Easy'
    WHEN 2 THEN 'Medium'
    WHEN 3 THEN 'Hard'
    WHEN 4 THEN 'Extreme'
    ELSE NULL
END
FROM (
    SELECT
        ts.track_id,
        MAX(
            CASE tsg.effective_grade
                WHEN 'Easy' THEN 1
                WHEN 'Medium' THEN 2
                WHEN 'Hard' THEN 3
                WHEN 'Extreme' THEN 4
                ELSE 0
            END
        ) AS worst_rank
    FROM track_segments AS ts
    JOIN track_segment_grades AS tsg
      ON tsg.segment_id = ts.segment_id
    WHERE ts.is_active IS TRUE
      AND ts.track_id IS NOT NULL
    GROUP BY ts.track_id
) AS agg
WHERE t.track_id = agg.track_id
"""


def upgrade() -> None:
    # 1. Ensure pg_trgm is enabled (also created by 0008) for fast substring ILIKE search
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # 2. Add cached effective_grade and trigram-indexed search_text columns to Tier B tracks
    op.add_column("tracks", sa.Column("effective_grade", sa.Text(), nullable=True))
    op.add_column("tracks", sa.Column("search_text", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_tracks_effective_grade_valid"),
        "tracks",
        _EFFECTIVE_GRADE_CHECK_SQL,
    )

    # 3. Keep search_text synchronized on every INSERT/UPDATE and backfill existing baked data
    op.execute(_SYNC_SEARCH_TEXT_FN_SQL)
    op.execute(_SYNC_SEARCH_TEXT_TRIGGER_SQL)
    op.execute(_BACKFILL_SEARCH_TEXT_SQL)
    op.execute(_BACKFILL_EFFECTIVE_GRADE_SQL)

    # 4. Create GIN gin_trgm_ops index on tracks.search_text (name/POI/park trgm indexes exist from 0008)
    op.create_index(
        "ix_tracks_search_text_trgm",
        "tracks",
        ["search_text"],
        unique=False,
        if_not_exists=True,
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_tracks_search_text_trgm", table_name="tracks", if_exists=True)
    op.execute("DROP TRIGGER IF EXISTS trg_tracks_sync_search_text ON tracks")
    op.execute("DROP FUNCTION IF EXISTS trailforge_sync_track_search_text()")
    op.execute("ALTER TABLE tracks DROP CONSTRAINT IF EXISTS ck_tracks_effective_grade_valid")
    op.drop_column("tracks", "search_text")
    op.drop_column("tracks", "effective_grade")
