"""Offline PostGIS integration tests verifying 35 tables, 21 GIST indexes, ODbL & privacy rules."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime

import geoalchemy2  # noqa: F401 — register PostGIS Geometry type with SQLAlchemy reflection
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from trailforge.api.main import create_app
from trailforge.db.session import check_db_health

EXPECTED_TABLES = frozenset(
    {
        # Tier A — Source tables (7)
        "source_tracks_osm",
        "source_tracks_vic",
        "source_tracks_nsw",
        "source_tracks_au_state",
        "source_waterways",
        "source_pois",
        "source_capad_parks",
        # Tier B — Canonical network & reference tables (5)
        "capad_parks",
        "tracks",
        "track_segments",
        "segment_reconciliation_log",
        "pois",
        # Tier C — Independent TrailForge intelligence, community, UI & AI/auth tables (23)
        "track_segment_grades",
        "closures_alerts",
        "segment_closure_links",
        "feed_sync_status",
        "users",
        "invite_codes",
        "usage_metrics_hourly",
        "personal_access_tokens",
        "oauth_clients",
        "oauth_authorization_codes",
        "oauth_tokens",
        "instance_ui_settings",
        "vehicle_profiles",
        "user_favorites",
        "user_pins",
        "trips",
        "telemetry_observations",
        "unmapped_track_candidates",
        "condition_reports",
        "condition_report_votes",
        "rainfall_snapshots",
        "offline_packs",
        "planned_routes",
    }
)

EXPECTED_GIST_INDEXES = frozenset(
    {
        "ix_source_tracks_osm_geom",
        "ix_source_tracks_vic_geom",
        "ix_source_tracks_nsw_geom",
        "ix_source_tracks_au_state_geom",
        "ix_source_waterways_geom",
        "ix_source_pois_geom",
        "ix_source_capad_parks_geom",
        "ix_capad_parks_geom",
        "ix_tracks_bbox_geom",
        "ix_track_segments_geom",
        "ix_track_segments_geom_active",
        "ix_pois_geom",
        "ix_closures_alerts_geom",
        "ix_user_pins_geom",
        "ix_trips_raw_geom",
        "ix_trips_trimmed_geom",
        "ix_telemetry_observations_slow_hotspot_geom",
        "ix_unmapped_track_candidates_geom",
        "ix_condition_reports_geom",
        "ix_rainfall_snapshots_centroid_geom",
        "ix_offline_packs_bbox_geom",
    }
)


EXPECTED_TRGM_INDEXES = frozenset(
    {
        "ix_tracks_name_trgm",
        "ix_tracks_search_text_trgm",
        "ix_pois_name_trgm",
        "ix_capad_parks_name_trgm",
    }
)


@pytest.mark.integration
def test_postgis_health_and_schema_revision(postgis_engine: Engine) -> None:
    status = check_db_health(postgis_engine)
    assert status.db_ok is True
    assert status.postgis_version is not None
    assert status.postgis_version.startswith("3.")
    assert status.schema_revision == "0009_search_trigram_track_grade"


@pytest.mark.integration
def test_fastapi_health_against_live_postgis(postgis_engine: Engine) -> None:
    app = create_app()
    from trailforge.api.health import get_health_checker

    app.dependency_overrides[get_health_checker] = lambda: (lambda: check_db_health(postgis_engine))
    with TestClient(app) as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db_ok"] is True
    assert body["schema_revision"] == "0009_search_trigram_track_grade"


@pytest.mark.integration
def test_all_35_tables_and_21_gist_indexes_exist(postgis_engine: Engine) -> None:
    assert len(EXPECTED_TABLES) == 35
    assert len(EXPECTED_GIST_INDEXES) == 21
    inspector = inspect(postgis_engine)
    tables = set(inspector.get_table_names(schema="public"))
    missing_tables = EXPECTED_TABLES - tables
    assert not missing_tables, f"Missing spatial tables: {sorted(missing_tables)}"

    with postgis_engine.connect() as conn:
        gist_rows = conn.execute(
            text(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND indexdef ILIKE '%USING gist%'
                """
            )
        ).scalars().all()
    gist_indexes = set(gist_rows)
    missing_gist = EXPECTED_GIST_INDEXES - gist_indexes
    assert not missing_gist, f"Missing GIST indexes: {sorted(missing_gist)}"

    # Verify pg_trgm extension and all 4 GIN gin_trgm_ops search indexes exist
    with postgis_engine.connect() as conn:
        has_trgm = conn.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
        ).scalar_one_or_none()
        trgm_rows = conn.execute(
            text(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND indexdef ILIKE '%USING gin%'
                  AND indexdef ILIKE '%gin_trgm_ops%'
                """
            )
        ).scalars().all()
    assert has_trgm == 1, "pg_trgm extension must be installed by migration 0007"
    missing_trgm = EXPECTED_TRGM_INDEXES - set(trgm_rows)
    assert not missing_trgm, f"Missing GIN trigram indexes: {sorted(missing_trgm)}"

    track_cols = {col["name"] for col in inspector.get_columns("tracks")}
    assert {"effective_grade", "search_text"}.issubset(track_cols)

    # Verify all CHECK constraints are single-prefixed (zero double-prefix / hash truncation)
    with postgis_engine.connect() as conn:
        bad_ck = conn.execute(
            text(
                r"""
                SELECT conname
                FROM pg_constraint
                WHERE connamespace = 'public'::regnamespace
                  AND contype = 'c'
                  AND conrelid != 'spatial_ref_sys'::regclass
                  AND (conname LIKE 'ck\_%\_ck\_%' ESCAPE '\' OR length(conname) > 60)
                """
            )
        ).scalars().all()
    assert not bad_ck, f"Malformed/double-prefixed CHECK constraints: {bad_ck}"


@pytest.mark.integration
def test_odbl_and_privacy_column_invariants(postgis_engine: Engine) -> None:
    inspector = inspect(postgis_engine)

    # Telemetry & unmapped candidates must NEVER store user_id or trip_id
    for table_name in ("telemetry_observations", "unmapped_track_candidates"):
        cols = {col["name"] for col in inspector.get_columns(table_name)}
        assert "user_id" not in cols, f"Privacy violation: {table_name} contains user_id"
        assert "trip_id" not in cols, f"Privacy violation: {table_name} contains trip_id"
        assert "contribution_hash" in cols

    # Tier C tables must NEVER embed raw OSM way IDs or raw OSM tag blobs
    tier_c_tables = (
        "track_segment_grades",
        "closures_alerts",
        "segment_closure_links",
        "telemetry_observations",
        "unmapped_track_candidates",
        "condition_reports",
        "rainfall_snapshots",
        "offline_packs",
        "planned_routes",
        "invite_codes",
        "usage_metrics_hourly",
        "personal_access_tokens",
        "oauth_clients",
        "oauth_authorization_codes",
        "oauth_tokens",
        "instance_ui_settings",
    )
    for table_name in tier_c_tables:
        cols = {col["name"] for col in inspector.get_columns(table_name)}
        assert "osm_way_id" not in cols
        assert "tags" not in cols
    for non_spatial_table in (
        "planned_routes",
        "invite_codes",
        "usage_metrics_hourly",
        "personal_access_tokens",
        "oauth_clients",
        "oauth_authorization_codes",
        "oauth_tokens",
        "instance_ui_settings",
    ):
        non_spatial_cols = {col["name"] for col in inspector.get_columns(non_spatial_table)}
        assert "geom" not in non_spatial_cols


@pytest.mark.integration
def test_spatial_operations_restrict_fk_and_privacy_constraints(postgis_engine: Engine) -> None:
    seg_id = f"tfseg_{uuid.uuid4().hex[:16]}"
    fingerprint = "a" * 64
    contrib_hash = "b" * 64

    with postgis_engine.connect() as conn:
        trans = conn.begin()
        try:
            # 1. Insert canonical segment and verify EPSG:3577 metric calculation
            conn.execute(
                text(
                    """
                    INSERT INTO track_segments (
                        segment_id, geom_fingerprint, geom, start_node_key, end_node_key,
                        length_m, bearing_deg, state, name, trail_class, primary_source
                    ) VALUES (
                        :seg_id, :fp,
                        ST_SetSRID(ST_MakeLine(
                            ST_MakePoint(148.1200, -36.5960),
                            ST_MakePoint(148.1200, -36.5967)
                        ), 4326),
                        '148.12000,-36.59600', '148.12000,-36.59670',
                        77.8, 180.0, 'VIC', 'Davies Plain Track Lower', '4wd_track', 'osm'
                    )
                    """
                ),
                {"seg_id": seg_id, "fp": fingerprint},
            )

            metric_len = conn.execute(
                text(
                    "SELECT ST_Length(ST_Transform(geom, 3577)) "
                    "FROM track_segments WHERE segment_id = :seg_id"
                ),
                {"seg_id": seg_id},
            ).scalar_one()
            assert 50.0 <= metric_len <= 100.0

            # 2. Insert explainable Day-1 baseline grade JSONB
            breakdown = json.dumps(
                {"formula_version": "baseline_v1", "factors": {"max_slope": {"points": 30}}}
            )
            conn.execute(
                text(
                    """
                    INSERT INTO track_segment_grades (
                        segment_id, baseline_grade, baseline_score,
                        baseline_breakdown, effective_grade
                    ) VALUES (
                        :seg_id, 'Extreme', 87.0,
                        CAST(:breakdown AS jsonb),
                        'Extreme'
                    )
                    """
                ),
                {"seg_id": seg_id, "breakdown": breakdown},
            )

            # 3. Insert anonymized telemetry observation
            conn.execute(
                text(
                    """
                    INSERT INTO telemetry_observations (
                        segment_id, contribution_hash, vehicle_capability_class, direction,
                        observed_at, observed_date, traversal_duration_s, avg_speed_kmh,
                        match_confidence, matched_point_count
                    ) VALUES (
                        :seg_id, :chash, 'Touring 4WD', 'forward',
                        :obs_at, :obs_date, 34.5, 8.1, 0.94, 5
                    )
                    """
                ),
                {
                    "seg_id": seg_id,
                    "chash": contrib_hash,
                    "obs_at": datetime(2026, 11, 20, 9, 0, tzinfo=UTC),
                    "obs_date": date(2026, 11, 20),
                },
            )

            # 4. Verify ON DELETE RESTRICT prevents accidental segment deletion/orphaning
            sp = conn.begin_nested()
            with pytest.raises(IntegrityError):
                conn.execute(
                    text("DELETE FROM track_segments WHERE segment_id = :seg_id"),
                    {"seg_id": seg_id},
                )
            sp.rollback()

            # 5. Verify trips.privacy_trim_m >= 250 m check constraint
            user_id = conn.execute(
                text(
                    """
                    INSERT INTO users (email, display_name, password_hash)
                    VALUES (:email, 'Tester', 'argon2id$stub')
                    RETURNING id
                    """
                ),
                {"email": f"tester-{uuid.uuid4().hex[:8]}@example.test"},
            ).scalar_one()

            sp2 = conn.begin_nested()
            with pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        """
                        INSERT INTO trips (
                            user_id, vehicle_capability_class, privacy_trim_m, raw_geom
                        ) VALUES (
                            :uid, 'Touring 4WD', 100.0,
                            ST_SetSRID(ST_MakeLine(
                                ST_MakePoint(148.12, -36.56),
                                ST_MakePoint(148.12, -36.60)
                            ), 4326)
                        )
                        """
                    ),
                    {"uid": user_id},
                )
            sp2.rollback()
        finally:
            trans.rollback()
