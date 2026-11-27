"""Offline unit & PostGIS API tests for Slice 06 Trips/Telemetry & Slice 08 Track Integration."""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from collections.abc import Generator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pyproj import Transformer
from sqlalchemy import Engine, func, inspect, select, text
from sqlalchemy.orm import Session

from trailforge.api.main import create_app
from trailforge.api.trips import get_track_matcher, reconstruct_trimmed_points
from trailforge.config import get_settings
from trailforge.db.models import TelemetryObservation, TrackSegment, UnmappedTrackCandidate
from trailforge.db.session import get_db
from trailforge.export.formats import GPX_NS
from trailforge.ingestion.connectors.fixtures import load_corridor_fixtures
from trailforge.ingestion.pipeline import ingest_corridor_dataset
from trailforge.matching import (
    GpsPoint,
    MatchedSegmentTraversal,
    MatchResult,
    TrackMatcher,
    parse_gpx,
)
from trailforge.matching.contribution_keys import (
    ContributionKeyUnavailable,
    require_complete_contribution_keyring,
)
from trailforge.worker.community_keys import rekey_contributions

_WGS84_TO_ALBERS = Transformer.from_crs("EPSG:4326", "EPSG:3577", always_xy=True)


def _metric_dist_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    x1, y1 = _WGS84_TO_ALBERS.transform(lon1, lat1)
    x2, y2 = _WGS84_TO_ALBERS.transform(lon2, lat2)
    return math.hypot(x2 - x1, y2 - y1)


# -----------------------------------------------------------------------------
# Pure Unit Tests (Zero DB, Zero Network)
# -----------------------------------------------------------------------------


def test_reconstruct_trimmed_points_monotonic_timestamps() -> None:
    start = datetime(2026, 11, 20, 8, 0, 0, tzinfo=UTC)
    end = start + timedelta(minutes=10)
    coords = [
        [148.1200, -36.5700],
        [148.1200, -36.5750],
        [148.1200, -36.5800],
        [148.1200, -36.5850],
    ]
    pts = reconstruct_trimmed_points(coords, started_at=start, ended_at=end, duration_s=600.0)
    assert len(pts) == 4
    assert pts[0].timestamp == start
    assert pts[-1].timestamp == end
    for i in range(1, len(pts)):
        assert pts[i].timestamp > pts[i - 1].timestamp


# -----------------------------------------------------------------------------
# PostGIS API Integration Tests (100% Offline)
# -----------------------------------------------------------------------------


def _truncate_all(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                TRUNCATE TABLE
                    condition_report_votes, condition_reports,
                    telemetry_observations, unmapped_track_candidates, trips,
                    segment_closure_links, closures_alerts, feed_sync_status,
                    segment_reconciliation_log, track_segment_grades,
                    user_favorites, user_pins, vehicle_profiles, users,
                    pois, track_segments, tracks, capad_parks,
                    source_tracks_osm, source_tracks_vic, source_tracks_nsw,
                    source_waterways, source_pois, source_capad_parks
                RESTART IDENTITY CASCADE
                """
            )
        )


def _register_user(client: TestClient, email: str, name: str) -> dict[str, str]:
    r = client.post(
        "/api/auth/register",
        json={"email": email, "display_name": name, "password": "SuperSecure4WDPassword!"},
    )
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.integration
def test_trips_upload_privacy_trim_hmm_toggle_unmapped_and_export(
    postgis_engine: Engine,
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _truncate_all(postgis_engine)
    try:
        with Session(postgis_engine) as session:
            ds = load_corridor_fixtures(fixtures_dir, osm_filename="osm_tracks.geojson")
            ingest_corridor_dataset(session, ds, commit=True)

            # Verify DB schema invariant: ZERO user_id / trip_id columns on public telemetry tables
            insp = inspect(postgis_engine)
            obs_cols = {c["name"] for c in insp.get_columns("telemetry_observations")}
            unm_cols = {c["name"] for c in insp.get_columns("unmapped_track_candidates")}
            assert "user_id" not in obs_cols and "trip_id" not in obs_cols
            assert "user_id" not in unm_cols and "trip_id" not in unm_cols

            app = create_app()

            def _override_db() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db] = _override_db
            with TestClient(app) as client:
                # 1. Auth guard on personal trip endpoints; /api/telemetry/unmapped is public
                assert client.get("/api/trips").status_code == 401
                r_unm_init = client.get("/api/telemetry/unmapped")
                assert r_unm_init.status_code == 200
                assert r_unm_init.json() == {"type": "FeatureCollection", "features": []}

                headers_a = _register_user(client, "driver_a@alps.au", "Alps Driver A")
                headers_b = _register_user(client, "driver_b@alps.au", "Alps Driver B")

                # Create Touring 4WD active vehicle profile for User A
                r_veh = client.post(
                    "/api/vehicles",
                    headers=headers_a,
                    json={
                        "name": "Patrol GU Touring",
                        "drivetrain": "4wd_part_time",
                        "low_range": True,
                        "lift_mm": 50,
                        "clearance_mm": 245,
                        "tire_class": "AT",
                        "rear_locker": True,
                        "has_snorkel": True,
                    },
                )
                assert r_veh.status_code == 201
                veh_id = r_veh.json()["id"]

                # 2. Validation guardrails: trim < 250 m rejected (422), blank name rejected (422)
                davies_gpx = (fixtures_dir / "sample_trace_davies_plain.gpx").read_bytes()
                parsed_davies = parse_gpx(davies_gpx)
                raw_start = parsed_davies.points[0]
                raw_end = parsed_davies.points[-1]

                assert (
                    client.post(
                        "/api/trips/upload",
                        headers=headers_a,
                        files={"file": ("davies.gpx", davies_gpx, "application/gpx+xml")},
                        data={"visibility": "community_anonymous", "privacy_trim_m": "100"},
                    ).status_code
                    == 422
                )
                assert (
                    client.post(
                        "/api/trips/upload",
                        headers=headers_a,
                        files={"file": ("davies.gpx", davies_gpx, "application/gpx+xml")},
                        data={"name": "   ", "visibility": "private"},
                    ).status_code
                    == 422
                )

                # 3. Upload sample_trace_davies_plain.gpx as community_anonymous
                r_up = client.post(
                    "/api/trips/upload",
                    headers=headers_a,
                    files={"file": ("davies_plain.gpx", davies_gpx, "application/gpx+xml")},
                    data={
                        "name": "Davies Plain November Traverse",
                        "vehicle_profile_id": veh_id,
                        "visibility": "community_anonymous",
                        "privacy_trim_m": "250",
                    },
                )
                assert r_up.status_code == 201, r_up.text
                trip_a = r_up.json()
                trip_a_id = trip_a["trip_id"]
                assert trip_a["name"] == "Davies Plain November Traverse"
                assert trip_a["visibility"] == "community_anonymous"
                assert trip_a["vehicle_class"] == "Touring 4WD"
                assert trip_a["privacy_trim_m"] == 250.0
                assert trip_a["raw_length_m"] > 10000.0
                assert trip_a["raw_length_m"] - trip_a["trimmed_length_m"] == pytest.approx(
                    500.0, abs=15.0
                )
                assert trip_a["matched_segment_count"] > 100
                assert trip_a["avg_match_confidence"] > 0.7
                assert trip_a["observations_published"] == trip_a["matched_segment_count"]
                assert trip_a["unmapped_candidate_count"] == 0
                assert len(trip_a["matched_segments"]) == trip_a["matched_segment_count"]
                assert all(s["track_id"] is not None for s in trip_a["matched_segments"])

                # Verify start & end privacy buffer strictly removed from trimmed_geojson!
                r_det = client.get(f"/api/trips/{trip_a_id}", headers=headers_a)
                assert r_det.status_code == 200
                det_a = r_det.json()
                trim_coords = det_a["trimmed_geojson"]["coordinates"]
                assert _metric_dist_m(
                    raw_start.lon, raw_start.lat, trim_coords[0][0], trim_coords[0][1]
                ) >= 240.0
                assert _metric_dist_m(
                    raw_end.lon, raw_end.lat, trim_coords[-1][0], trim_coords[-1][1]
                ) >= 240.0

                # Check DB rows directly: anonymized observations present, 64-char hash
                obs_before = session.execute(
                    select(func.count(TelemetryObservation.id))
                ).scalar_one()
                assert obs_before == trip_a["observations_published"]
                old_hashes = set(
                    session.execute(
                        select(TelemetryObservation.contribution_hash).distinct()
                    ).scalars()
                )
                assert len(old_hashes) == 1
                old_hash = next(iter(old_hashes))
                assert len(old_hash) == 64

                # 4. Toggle community_anonymous -> private: IMMEDIATELY revokes to 0 rows!
                r_priv = client.patch(
                    f"/api/trips/{trip_a_id}",
                    headers=headers_a,
                    json={"visibility": "private"},
                )
                assert r_priv.status_code == 200, r_priv.text
                assert r_priv.json()["visibility"] == "private"
                assert r_priv.json()["observations_published"] == 0
                assert (
                    session.execute(select(func.count(TelemetryObservation.id))).scalar_one() == 0
                )

                # 5. Toggle private -> community_anonymous: re-publishes with ROTATED hash!
                r_repub = client.patch(
                    f"/api/trips/{trip_a_id}",
                    headers=headers_a,
                    json={"visibility": "community_anonymous"},
                )
                assert r_repub.status_code == 200, r_repub.text
                repub = r_repub.json()
                assert repub["visibility"] == "community_anonymous"
                assert repub["observations_published"] > 100
                new_hashes = set(
                    session.execute(
                        select(TelemetryObservation.contribution_hash).distinct()
                    ).scalars()
                )
                assert len(new_hashes) == 1
                assert next(iter(new_hashes)) != old_hash

                # 6. Upload sample_trace_unmapped_spur.gpx -> matched Poplars + 1 unmapped spur!
                spur_gpx = (fixtures_dir / "sample_trace_unmapped_spur.gpx").read_bytes()
                r_spur = client.post(
                    "/api/trips/upload",
                    headers=headers_a,
                    files={"file": ("unmapped_spur.gpx", spur_gpx, "application/gpx+xml")},
                    data={"visibility": "community_anonymous", "privacy_trim_m": "250"},
                )
                assert r_spur.status_code == 201, r_spur.text
                spur = r_spur.json()
                assert spur["match_status"] == "partial"
                assert spur["observations_published"] >= 15
                assert spur["unmapped_candidate_count"] == 1
                assert spur["unmapped_candidates"][0]["length_m"] > 500.0

                # One contributor's off-network geometry remains in the owner's trip.
                r_unm_pub = client.get("/api/telemetry/unmapped")
                assert r_unm_pub.status_code == 200
                unm_fc = r_unm_pub.json()
                assert unm_fc == {"type": "FeatureCollection", "features": []}
                previous_key = get_settings().privacy_secret_key
                monkeypatch.setenv(
                    "TRAILFORGE_PREVIOUS_PRIVACY_SECRETS", json.dumps([previous_key])
                )
                monkeypatch.setenv("TRAILFORGE_PRIVACY_SECRET", "rotated-offline-fixture-key")
                get_settings.cache_clear()
                spur_id = spur["id"]
                owner = client.get(f"/api/trips/{spur_id}", headers=headers_a)
                assert owner.status_code == 200
                assert len(owner.json()["unmapped_candidates"][0]["coordinates"]) > 2
                monkeypatch.delenv("TRAILFORGE_PREVIOUS_PRIVACY_SECRETS")
                get_settings.cache_clear()
                with pytest.raises(ContributionKeyUnavailable):
                    require_complete_contribution_keyring(session)
                assert client.get(f"/api/trips/{spur_id}", headers=headers_a).status_code == 503

                monkeypatch.setenv(
                    "TRAILFORGE_PREVIOUS_PRIVACY_SECRETS", json.dumps([previous_key])
                )
                get_settings.cache_clear()
                assert rekey_contributions(session) == 2
                session.commit()
                monkeypatch.delenv("TRAILFORGE_PREVIOUS_PRIVACY_SECRETS")
                get_settings.cache_clear()
                require_complete_contribution_keyring(session)
                assert client.get(f"/api/trips/{spur_id}", headers=headers_a).status_code == 200

                # 7. JSON upload: private trip writes ZERO telemetry observations
                obs_before_priv_json = session.execute(
                    select(func.count(TelemetryObservation.id))
                ).scalar_one()
                json_pts = [
                    {
                        "lon": p.lon,
                        "lat": p.lat,
                        "elev_m": p.elev_m,
                        "timestamp": p.timestamp.isoformat(),
                        "roughness_rms": 1.8,
                    }
                    for p in parsed_davies.points[:30]
                ]
                r_json_priv = client.post(
                    "/api/trips/upload",
                    headers=headers_a,
                    json={
                        "name": "Private Valley Recon",
                        "points": json_pts,
                        "visibility": "private",
                        "privacy_trim_m": 250,
                    },
                )
                assert r_json_priv.status_code == 201, r_json_priv.text
                jp = r_json_priv.json()
                assert jp["visibility"] == "private"
                assert jp["match_status"] == "skipped_private"
                assert jp["observations_published"] == 0
                assert (
                    session.execute(select(func.count(TelemetryObservation.id))).scalar_one()
                    == obs_before_priv_json
                )

                # Short trace (<= 500 m) -> skipped_too_short, 0 observations
                r_short = client.post(
                    "/api/trips/upload",
                    headers=headers_a,
                    json={
                        "name": "Campground Loop",
                        "points": [
                            {"lon": 148.1200, "lat": -36.5600, "timestamp": "2026-11-22T08:00:00Z"},
                            {"lon": 148.1210, "lat": -36.5605, "timestamp": "2026-11-22T08:01:00Z"},
                        ],
                        "visibility": "community_anonymous",
                        "privacy_trim_m": 250,
                    },
                )
                assert r_short.status_code == 201
                assert r_short.json()["match_status"] == "skipped_too_short"
                assert r_short.json()["trimmed_length_m"] == 0.0
                assert r_short.json()["observations_published"] == 0

                # 8. Cross-user ownership isolation (User B gets 404 on User A's trip)
                assert client.get(f"/api/trips/{trip_a_id}", headers=headers_b).status_code == 404
                assert (
                    client.patch(
                        f"/api/trips/{trip_a_id}",
                        headers=headers_b,
                        json={"visibility": "private"},
                    ).status_code
                    == 404
                )
                assert (
                    client.delete(f"/api/trips/{trip_a_id}", headers=headers_b).status_code == 404
                )
                assert (
                    client.get(
                        f"/api/trips/{trip_a_id}/export?format=gpx", headers=headers_b
                    ).status_code
                    == 404
                )

                # 9. Owner GPX 1.1 & GeoJSON export
                r_gpx = client.get(f"/api/trips/{trip_a_id}/export?format=gpx", headers=headers_a)
                assert r_gpx.status_code == 200
                gpx_root = ET.fromstring(r_gpx.text)
                assert gpx_root.attrib["version"] == "1.1"
                assert len(gpx_root.findall(f".//{{{GPX_NS}}}trkpt")) == len(parsed_davies.points)

                r_geo = client.get(
                    f"/api/trips/{trip_a_id}/export?format=geojson", headers=headers_a
                )
                assert r_geo.status_code == 200
                fc = json.loads(r_geo.text)
                assert fc["type"] == "FeatureCollection"
                assert fc["features"][0]["geometry"]["type"] == "LineString"

                # 10. Deleting spur trip revokes its unmapped candidate + observations cleanly
                r_del_spur = client.delete(f"/api/trips/{spur['trip_id']}", headers=headers_a)
                assert r_del_spur.status_code == 200
                assert r_del_spur.json()["revoked_unmapped"] == 1
                assert session.scalar(select(func.count(UnmappedTrackCandidate.id))) == 0
                assert (
                    session.execute(select(func.count(UnmappedTrackCandidate.id))).scalar_one() == 0
                )

                # 11. Slice 08 cross-integration: Track Inspector surfaces condition_reports
                #     AND distinct community_gate_warnings!
                tracks = client.get("/api/tracks").json()
                station = next(t for t in tracks if "Station Creek" in t["name"])
                station_det = client.get(f"/api/tracks/{station['track_id']}").json()
                seg0_coords = station_det["segments"][0]["geometry"]["coordinates"][0]

                r_rep = client.post(
                    "/api/reports",
                    headers=headers_a,
                    json={
                        "report_type": "locked_gate",
                        "lon": seg0_coords[0],
                        "lat": seg0_coords[1],
                        "track_id": station["track_id"],
                        "notes": "Padlocked farm gate blocking track entry",
                    },
                )
                assert r_rep.status_code == 201, r_rep.text

                station_after = client.get(f"/api/tracks/{station['track_id']}").json()
                assert len(station_after["condition_reports"]) == 1
                assert len(station_after["community_gate_warnings"]) == 1
                gw = station_after["community_gate_warnings"][0]
                assert gw["report_type"] == "locked_gate"
                assert gw["osm_note_url"].startswith("https://www.openstreetmap.org/note/new")
                assert gw["osm_edit_url"].startswith("https://www.openstreetmap.org/edit")

            app.dependency_overrides.clear()
    finally:
        _truncate_all(postgis_engine)


@pytest.mark.integration
def test_pluggable_track_matcher_dependency_injection(
    postgis_engine: Engine,
    fixtures_dir: Path,
) -> None:
    """Verify a custom/Valhalla-style TrackMatcher swaps cleanly via FastAPI DI."""
    _truncate_all(postgis_engine)
    try:
        with Session(postgis_engine) as session:
            ds = load_corridor_fixtures(fixtures_dir, osm_filename="osm_tracks.geojson")
            ingest_corridor_dataset(session, ds, commit=True)

            first_seg = session.execute(select(TrackSegment).limit(1)).scalar_one()

            class StubValhallaMatcher:
                name = "stub_valhalla_v1"

                async def match_trace(
                    self,
                    points: Sequence[GpsPoint],
                    *,
                    search_radius_m: float = 35.0,
                    min_confidence: float = 0.55,
                ) -> MatchResult:
                    return MatchResult(
                        matcher_name=self.name,
                        matched_segments=[
                            MatchedSegmentTraversal(
                                segment_id=first_seg.segment_id,
                                direction="forward",
                                entry_at=points[0].timestamp,
                                exit_at=points[-1].timestamp,
                                duration_s=45.0,
                                avg_speed_kmh=18.5,
                                min_speed_kmh=12.0,
                                roughness_rms=2.1,
                                roughness_p95=3.0,
                                match_confidence=0.91,
                                matched_point_count=len(points),
                            )
                        ],
                        unmatched_subtraces=[],
                        overall_confidence=0.91,
                        matched_fraction=1.0,
                    )

            assert isinstance(StubValhallaMatcher(), TrackMatcher)

            app = create_app()

            def _override_db() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db] = _override_db
            app.dependency_overrides[get_track_matcher] = lambda: StubValhallaMatcher()

            with TestClient(app) as client:
                headers = _register_user(client, "valhalla@alps.au", "Valhalla Tester")
                davies_gpx = (fixtures_dir / "sample_trace_davies_plain.gpx").read_bytes()
                r = client.post(
                    "/api/trips/upload",
                    headers=headers,
                    files={"file": ("davies.gpx", davies_gpx, "application/gpx+xml")},
                    data={"visibility": "community_anonymous"},
                )
                assert r.status_code == 201, r.text
                body = r.json()
                assert body["matcher_name"] == "stub_valhalla_v1"
                assert body["matched_segment_count"] == 1
                assert body["observations_published"] == 1
                assert body["matched_segments"][0]["segment_id"] == first_seg.segment_id
                assert body["matched_segments"][0]["track_id"] == first_seg.track_id

            app.dependency_overrides.clear()
    finally:
        _truncate_all(postgis_engine)
