"""Offline PostGIS integration tests proving Slice 02 end-to-end pipeline & API invariants.

Verifies:
  1. Full High Country corridor ingestion (Tier A -> Tier B -> Tier C) with strict ODbL separation
  2. Deduplicated 50-100 m segments with DEM elevation draping, >18 deg steep escarpment,
     >1,000 m snow-line ridge, unbridged Murray River / bridged Limestone Creek crossings,
     and explainable Day-1 baseline_breakdown JSONB
  3. Re-ingesting `osm_tracks_v2_jittered.geojson` preserves 100% of segment IDs across tag edits,
     mid-way way splits, and reversed 3-6 m vertex jitter while keeping attached synthetic
     `telemetry_observations`, `segment_closure_links`, and `condition_reports` intact with
     ZERO orphaned rows
  4. FastAPI read endpoints (`/api/tracks`, `/api/tracks/geojson`, `/api/tracks/{id}`,
     `/api/segments/{id}`, `/api/pois`, `/api/parks`, `/api/search`)
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from geoalchemy2.shape import from_shape
from shapely.geometry import Point
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from trailforge.api.main import create_app
from trailforge.db.models import (
    POI,
    ClosureAlert,
    ConditionReport,
    SegmentClosureLink,
    SegmentReconciliationLog,
    SourceCAPADPark,
    SourcePOI,
    SourceTrackNSW,
    SourceTrackOSM,
    SourceTrackVIC,
    SourceWaterway,
    TelemetryObservation,
    TrackSegment,
    TrackSegmentGrade,
    User,
)
from trailforge.db.session import get_db
from trailforge.ingestion.connectors.fixtures import load_corridor_fixtures
from trailforge.ingestion.pipeline import ingest_corridor_dataset


def _truncate_pipeline_tables(conn: Any) -> None:
    conn.execute(
        text(
            """
            TRUNCATE TABLE
                condition_report_votes,
                condition_reports,
                telemetry_observations,
                unmapped_track_candidates,
                segment_closure_links,
                closures_alerts,
                segment_reconciliation_log,
                track_segment_grades,
                pois,
                track_segments,
                tracks,
                capad_parks,
                users,
                source_tracks_osm,
                source_tracks_vic,
                source_tracks_nsw,
                source_waterways,
                source_pois,
                source_capad_parks
            RESTART IDENTITY CASCADE
            """
        )
    )


@pytest.fixture
def clean_db_session(postgis_engine: Engine) -> Generator[Session, None, None]:
    """Provide a clean PostGIS session for Slice 02 pipeline tests and tidy up after."""
    with postgis_engine.begin() as conn:
        _truncate_pipeline_tables(conn)
    with Session(postgis_engine) as session:
        yield session
    with postgis_engine.begin() as conn:
        _truncate_pipeline_tables(conn)


@pytest.mark.integration
def test_slice02_corridor_ingestion_v2_reimport_stability_and_read_api(
    clean_db_session: Session,
    fixtures_dir: Path,
) -> None:
    session = clean_db_session

    # =========================================================================
    # PASS 1: Initial High Country Corridor Ingestion (v1)
    # =========================================================================
    ds_v1 = load_corridor_fixtures(fixtures_dir, osm_filename="osm_tracks.geojson")
    summary_v1 = ingest_corridor_dataset(session, ds_v1, commit=True)

    # Tier A ODbL isolation counts
    assert session.scalar(select(func.count()).select_from(SourceTrackOSM)) == 7
    assert session.scalar(select(func.count()).select_from(SourceTrackVIC)) == 3
    assert session.scalar(select(func.count()).select_from(SourceTrackNSW)) == 2
    assert session.scalar(select(func.count()).select_from(SourceWaterway)) == 2
    assert session.scalar(select(func.count()).select_from(SourcePOI)) == 6
    assert session.scalar(select(func.count()).select_from(SourceCAPADPark)) == 2

    # Tier B canonical counts
    assert summary_v1.deduplicated_ways == 2
    assert summary_v1.dropped_non_vehicular == 2
    assert summary_v1.tracks_upserted == 8
    assert summary_v1.segments_active > 100
    assert summary_v1.segments_created == summary_v1.segments_active
    assert summary_v1.segments_retired == 0
    assert summary_v1.pois_upserted == 5  # Davies Plain Hut OSM duplicate deduped!
    assert summary_v1.capad_parks_upserted == 2

    # Verify every active segment is 50-100 m with DEM elevation draping & explainable grade
    segs_v1 = session.execute(
        select(TrackSegment).where(TrackSegment.is_active.is_(True))
    ).scalars().all()
    assert len(segs_v1) == summary_v1.segments_active

    for seg in segs_v1:
        assert 49.5 <= seg.length_m <= 100.5, f"{seg.name}: length={seg.length_m}"
        assert seg.elev_min_m is not None and 600.0 <= seg.elev_min_m <= 1650.0
        assert seg.elev_max_m is not None and 600.0 <= seg.elev_max_m <= 1650.0
        assert seg.max_slope_deg is not None and seg.max_slope_deg >= 0.0
        assert seg.sustained_slope_deg is not None and seg.sustained_slope_deg >= 0.0
        assert len(seg.elevation_profile_m) >= 3
        assert seg.capad_park_id is not None

    # Verify Davies Plain Track Lower steep escarpment (>18 deg) & unbridged Murray River crossing
    davies_lower_segs = [s for s in segs_v1 if s.name == "Davies Plain Track Lower"]
    assert davies_lower_segs
    assert max(s.max_slope_deg or 0.0 for s in davies_lower_segs) > 18.0
    assert any(s.is_steep_highlight for s in davies_lower_segs)
    assert any(
        s.waterway_crossing_count >= 1 and s.has_perennial_crossing for s in davies_lower_segs
    )

    # Verify Davies Plain Ridge alpine snow-line (>1,000 m, >1,200 m plateau)
    davies_ridge_segs = [s for s in segs_v1 if s.name == "Davies Plain Ridge"]
    assert davies_ridge_segs
    assert all(s.is_above_snowline for s in davies_ridge_segs)
    assert max(s.elev_max_m or 0.0 for s in davies_ridge_segs) > 1200.0

    # Verify bridged Limestone Creek Link incurs zero unbridged waterway crossing penalty
    bridged_segs = [s for s in segs_v1 if s.name == "Limestone Creek Bridged Link"]
    assert bridged_segs
    assert all(s.is_bridged for s in bridged_segs)
    assert all(s.waterway_crossing_count == 0 for s in bridged_segs)

    # Verify VIC-only Bucky Ridge Fire Trail detects unbridged Limestone Creek crossing
    bucky_segs = [s for s in segs_v1 if s.name == "BUCKY RIDGE FIRE TRAIL"]
    assert bucky_segs
    assert all(s.primary_source == "vic" and not s.has_osm_source for s in bucky_segs)
    assert any(s.waterway_crossing_count >= 1 for s in bucky_segs)

    # Verify Tier C explainable grades exist for 100% of segments with expected difficulty spread
    grades_v1 = {
        g.segment_id: g
        for g in session.execute(select(TrackSegmentGrade)).scalars().all()
    }
    assert len(grades_v1) == len(segs_v1)
    for seg in segs_v1:
        g = grades_v1[seg.segment_id]
        assert g.baseline_breakdown["formula_version"] == "baseline_v1"
        assert set(g.baseline_breakdown["factors"].keys()) == {
            "surface_tracktype",
            "max_slope",
            "sustained_relief",
            "waterway_crossings",
            "four_wd_access",
        }
        assert isinstance(g.baseline_breakdown.get("reasons"), list)

    tom_segs = [s for s in segs_v1 if s.name == "Tom Groggin Access Rd"]
    assert all(grades_v1[s.segment_id].baseline_grade == "Easy" for s in tom_segs)
    assert any(grades_v1[s.segment_id].baseline_grade == "Extreme" for s in davies_lower_segs)

    # Verify POI deduplication kept authoritative CC BY 4.0 Davies Plain Hut & linked segment
    hut_pois = session.execute(select(POI).where(POI.name == "Davies Plain Hut")).scalars().all()
    assert len(hut_pois) == 1
    assert hut_pois[0].source_license == "CC-BY-4.0"
    assert hut_pois[0].nearest_segment_id is not None

    # =========================================================================
    # ATTACH SYNTHETIC TELEMETRY, CLOSURES & CONDITION REPORTS TO V1 SEGMENTS
    # =========================================================================
    poplars_segs = [s for s in segs_v1 if s.name == "Poplars Fire Trail"]
    target_tom = tom_segs[0]  # way/1001: tag edit only in v2
    target_davies = davies_lower_segs[len(davies_lower_segs) // 2]  # way/1002: reversed+jittered
    target_poplars = poplars_segs[len(poplars_segs) // 2]  # way/1004: split mid-way in v2
    attached_ids = [target_tom.segment_id, target_davies.segment_id, target_poplars.segment_id]

    test_user = User(
        email="alps-tester@trailforge.local",
        display_name="AlpsTester",
        password_hash="argon2id$test",
    )
    session.add(test_user)
    session.flush()

    closure = ClosureAlert(
        source_feed="vic_mapshare_seasonal",
        external_id="VIC-SEASONAL-DAVIES-TEST",
        alert_type="seasonal_closure",
        severity="closed",
        status="active",
        title="Davies Plain Track Winter Closure",
        state="VIC",
    )
    session.add(closure)
    session.flush()

    for idx, sid in enumerate(attached_ids):
        session.add(
            TelemetryObservation(
                segment_id=sid,
                contribution_hash=f"{idx:064x}",
                vehicle_capability_class="Touring 4WD",
                direction="forward",
                observed_at=datetime(2026, 11, 20, 8, idx, tzinfo=UTC),
                observed_date=date(2026, 11, 20),
                traversal_duration_s=28.0,
                avg_speed_kmh=10.5,
                match_confidence=0.95,
                matched_point_count=4,
            )
        )
        session.add(
            SegmentClosureLink(
                segment_id=sid,
                closure_id=closure.id,
                link_method="spatial_intersect",
                distance_m=0.0,
            )
        )
        session.add(
            ConditionReport(
                user_id=test_user.id,
                segment_id=sid,
                report_type="bog_hole",
                severity="caution",
                description=f"Test report attached to {sid}",
                geom=from_shape(Point(148.121, -36.597), srid=4326),
            )
        )
    session.commit()

    # =========================================================================
    # PASS 2: Re-Ingest Jittered/Split/Tag-Edited v2 OSM Snapshot
    # =========================================================================
    ds_v2 = load_corridor_fixtures(fixtures_dir, osm_filename="osm_tracks_v2_jittered.geojson")
    summary_v2 = ingest_corridor_dataset(session, ds_v2, commit=True)

    assert summary_v2.segments_retired == 0
    assert summary_v2.segments_created == 0
    assert summary_v2.segments_exact_match > 0
    assert summary_v2.segments_spatial_preserved > 0
    assert (
        summary_v2.segments_exact_match + summary_v2.segments_spatial_preserved
        == summary_v1.segments_active
    )

    # Confirm 100% of v1 segment IDs remain active & attached records have ZERO orphans
    active_ids_v2 = set(
        session.execute(
            select(TrackSegment.segment_id).where(TrackSegment.is_active.is_(True))
        ).scalars().all()
    )
    assert active_ids_v2 == {s.segment_id for s in segs_v1}

    # Verify tag edit updated way/1001 surface in place without changing segment_id
    tom_after = session.get(TrackSegment, target_tom.segment_id)
    assert tom_after is not None and tom_after.is_active is True
    assert tom_after.surface_class == "sealed"

    # Verify ALL attached telemetry, closure links, and condition reports remain intact!
    telemetry_links = session.execute(
        select(TelemetryObservation.segment_id)
        .join(TrackSegment, TelemetryObservation.segment_id == TrackSegment.segment_id)
        .where(TrackSegment.is_active.is_(True))
    ).scalars().all()
    assert set(telemetry_links) == set(attached_ids)

    closure_links = session.execute(
        select(SegmentClosureLink.segment_id)
        .join(TrackSegment, SegmentClosureLink.segment_id == TrackSegment.segment_id)
        .where(TrackSegment.is_active.is_(True))
    ).scalars().all()
    assert set(closure_links) == set(attached_ids)

    report_links = session.execute(
        select(ConditionReport.segment_id)
        .join(TrackSegment, ConditionReport.segment_id == TrackSegment.segment_id)
        .where(TrackSegment.is_active.is_(True))
    ).scalars().all()
    assert set(report_links) == set(attached_ids)

    # Verify reconciliation audit log captured spatial_preserved metrics for jittered way/1002
    v2_logs = session.execute(
        select(SegmentReconciliationLog).where(
            SegmentReconciliationLog.import_run_id == summary_v2.import_run_id
        )
    ).scalars().all()
    preserved_logs = [lg for lg in v2_logs if lg.reconcile_action == "spatial_preserved"]
    assert len(preserved_logs) == summary_v2.segments_spatial_preserved
    assert all(
        lg.overlap_ratio is not None
        and lg.overlap_ratio >= 0.75
        and lg.hausdorff_m is not None
        and lg.hausdorff_m <= 15.0
        for lg in preserved_logs
    )

    # =========================================================================
    # PASS 3: Verify FastAPI Track, Segment, GeoJSON, POI, Park & Search API
    # =========================================================================
    app = create_app()

    def _override_db() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as client:
        # GET /api/tracks
        r_tracks = client.get("/api/tracks")
        assert r_tracks.status_code == 200
        tracks_payload = r_tracks.json()
        assert len(tracks_payload) == 8
        davies_trk = next(t for t in tracks_payload if t["name"] == "Davies Plain Track Lower")
        assert davies_trk["effective_grade"] == "Extreme"
        assert davies_trk["waterway_crossing_count"] >= 1
        assert davies_trk["max_slope_deg"] > 18.0
        assert davies_trk["segment_count"] == len(davies_lower_segs)

        # GET /api/tracks filtered by grade + strict limit (must filter in SQL before LIMIT!)
        r_ext = client.get(
            "/api/tracks",
            params={"grade": "Extreme", "limit": 1, "bbox": "148.05,-36.68,148.20,-36.55"},
        )
        assert r_ext.status_code == 200
        ext_tracks = r_ext.json()
        assert len(ext_tracks) == 1
        assert ext_tracks[0]["name"] == "Davies Plain Track Lower"

        # GET /api/tracks?q= matches alt_names (VIC alias 'DAVIES PLAIN TRACK' on both legs)
        r_alias = client.get("/api/tracks", params={"q": "DAVIES PLAIN TRACK"})
        assert r_alias.status_code == 200
        alias_names = {t["name"] for t in r_alias.json()}
        assert {"Davies Plain Track Lower", "Davies Plain Ridge"}.issubset(alias_names)

        # GET /api/tracks/{track_id}
        r_detail = client.get(f"/api/tracks/{davies_trk['track_id']}")
        assert r_detail.status_code == 200
        detail = r_detail.json()
        assert len(detail["segments"]) == len(davies_lower_segs)
        first_bd = detail["segments"][0]["grade"]["baseline_breakdown"]
        assert first_bd["formula_version"] == "baseline_v1"
        assert len(detail["segments"][0]["elevation_profile_m"]) >= 3
        assert detail["primary_park"]["name"] in (
            "Alpine National Park",
            "Kosciuszko National Park",
        )

        # GET /api/segments/{segment_id}
        r_seg = client.get(f"/api/segments/{target_davies.segment_id}")
        assert r_seg.status_code == 200
        seg_payload = r_seg.json()
        assert seg_payload["segment_id"] == target_davies.segment_id
        assert seg_payload["geometry"]["type"] == "LineString"
        seg_factors = seg_payload["grade"]["baseline_breakdown"]["factors"]
        assert seg_factors["max_slope"]["max_points"] == 30

        # GET /api/tracks/geojson
        r_gj = client.get("/api/tracks/geojson", params={"bbox": "148.05,-36.68,148.20,-36.55"})
        assert r_gj.status_code == 200
        gj = r_gj.json()
        assert gj["type"] == "FeatureCollection"
        assert len(gj["features"]) == summary_v1.segments_active

        # GET /api/pois & /api/parks
        r_pois = client.get("/api/pois")
        assert r_pois.status_code == 200
        assert len(r_pois.json()) == 5

        r_parks = client.get("/api/parks")
        assert r_parks.status_code == 200
        assert {p["name"] for p in r_parks.json()} == {
            "Alpine National Park",
            "Kosciuszko National Park",
        }

        # GET /api/search
        r_search = client.get("/api/search", params={"q": "Davies"})
        assert r_search.status_code == 200
        kinds = {item["kind"] for item in r_search.json()["results"]}
        assert {"track", "poi"}.issubset(kinds)
    app.dependency_overrides.clear()
