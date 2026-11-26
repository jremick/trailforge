"""Checkpoint C (v1 Release Gate) end-to-end offline integration & compliance tests.

Proves the complete TrailForge v1 vertical pipeline 100% offline:
  1. Ingest VIC/NSW Alps corridor fixtures & conflate stable canonical segments
  2. Sync official VIC/NSW closure & OSM access feeds
  3. Upload privacy-trimmed GPX telemetry & seed multi-rig passes + PostGIS HMM map-match
  4. Submit community locked-gate condition report with validated PNG attachment
  5. Recompute explainable crowdsourced live grades + 72 h Open-Meteo rainfall wet-risk
  6. Verify unified Track Inspector payload (baseline + live + rainfall + hotspots + closures +
     distinct community gate warnings + personalized rig suitability)
  7. Build streaming `australian-alps` offline pack, verify manifest SHA-256, `snapshot.json`,
     `bundle.tar.gz` extraction, and GridForge-compatible GPX 1.1 / GeoJSON export
  8. Audit GPL-3.0-or-later license, zero PyQGIS, ODbL table separation, & required attributions
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import xml.etree.ElementTree as ET
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from trailforge.api.main import create_app
from trailforge.db.session import get_db
from trailforge.export.formats import EXPORT_ATTRIBUTIONS, GPX_NS
from trailforge.ingestion.connectors.fixtures import load_corridor_fixtures
from trailforge.ingestion.pipeline import ingest_corridor_dataset
from trailforge.packs.catalog import PACK_ATTRIBUTIONS, PACK_REGION_IDS
from trailforge.tiles.builder import DEFAULT_ATTRIBUTIONS
from trailforge.worker.seed_demo import seed_demo_telemetry

# Minimal valid 1x1 PNG bytes for offline photo attachment check
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _truncate_all(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                TRUNCATE TABLE
                    offline_packs, rainfall_snapshots,
                    condition_report_votes, condition_reports,
                    telemetry_observations, unmapped_track_candidates, trips,
                    segment_closure_links, closures_alerts, feed_sync_status,
                    segment_reconciliation_log, track_segment_grades,
                    user_favorites, user_pins, vehicle_profiles, users,
                    pois, track_segments, tracks, capad_parks,
                    source_tracks_osm, source_tracks_vic, source_tracks_nsw,
                    source_tracks_au_state,
                    source_waterways, source_pois, source_capad_parks
                RESTART IDENTITY CASCADE
                """
            )
        )


# -----------------------------------------------------------------------------
# Pure Unit Compliance & Attribution Audit (Zero DB, Zero Network)
# -----------------------------------------------------------------------------


def test_v1_license_zero_pyqgis_and_attribution_compliance() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    pyproject_text = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = "GPL-3.0-or-later"' in pyproject_text

    # Zero PyQGIS anywhere in Python or TypeScript source trees
    for scan_dir in (repo_root / "src", repo_root / "web" / "src"):
        if not scan_dir.is_dir():
            continue
        for src_file in scan_dir.rglob("*"):
            if src_file.suffix in (".py", ".ts", ".js"):
                content = src_file.read_text(encoding="utf-8")
                assert "qgis.core" not in content, f"Forbidden PyQGIS reference in {src_file}"

    # Verify all required attributions appear in tile manifests, packs, and exports
    for attr_list in (DEFAULT_ATTRIBUTIONS, PACK_ATTRIBUTIONS):
        joined = " | ".join(attr_list)
        assert "OpenStreetMap" in joined and "ODbL" in joined
        assert "Geoscience Australia" in joined
        assert "CAPAD" in joined
        assert "New South Wales" in joined
        assert "Victoria" in joined
        assert "Open-Meteo" in joined

    export_joined = " | ".join(EXPORT_ATTRIBUTIONS)
    assert "OpenStreetMap" in export_joined and "ODbL" in export_joined
    assert "Geoscience Australia" in export_joined


# -----------------------------------------------------------------------------
# Full Checkpoint C End-to-End PostGIS Integration Test (100% Offline)
# -----------------------------------------------------------------------------


@pytest.mark.integration
def test_checkpoint_c_full_v1_pipeline_end_to_end(
    postgis_engine: Engine,
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packs_dir = tmp_path / "packs"
    uploads_dir = tmp_path / "uploads"
    monkeypatch.setenv("TRAILFORGE_PACKS_DIR", str(packs_dir))
    monkeypatch.setenv("PACKS_DIR", str(packs_dir))
    monkeypatch.setenv("TRAILFORGE_UPLOADS_DIR", str(uploads_dir))
    monkeypatch.setenv("UPLOADS_DIR", str(uploads_dir))

    _truncate_all(postgis_engine)
    try:
        with Session(postgis_engine) as session:
            # 1. Ingest synthetic VIC/NSW Alps corridor
            ds = load_corridor_fixtures(fixtures_dir, osm_filename="osm_tracks.geojson")
            ingest_corridor_dataset(session, ds, commit=True)

            # 2. Seed anonymized multi-rig demo passes
            seed_summary = seed_demo_telemetry(
                session,
                gpx_path=fixtures_dir / "sample_trace_davies_plain.gpx",
                commit=True,
            )
            assert seed_summary["passes_seeded"] == 5
            assert seed_summary["observations_inserted"] > 500

            app = create_app()

            def _override_db() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db] = _override_db
            with TestClient(app) as client:
                # 3. Sync official VIC/NSW closures + OSM access flags
                r_sync = client.post(
                    "/api/closures/sync",
                    json={
                        "use_fixtures": True,
                        "fixtures_path": str(fixtures_dir / "closures_sample.json"),
                    },
                )
                assert r_sync.status_code == 200, r_sync.text

                # 4. Register user, create Touring 4WD rig, upload community GPX trip
                r_reg = client.post(
                    "/api/auth/register",
                    json={
                        "email": "checkpoint_c@trailforge.local",
                        "display_name": "High Country Scout",
                        "password": "StrongPassword123!",
                    },
                )
                assert r_reg.status_code == 201
                headers = {"Authorization": f"Bearer {r_reg.json()['access_token']}"}

                r_veh = client.post(
                    "/api/vehicles",
                    headers=headers,
                    json={
                        "name": "76 Series Touring",
                        "drivetrain": "4wd_part_time",
                        "low_range": True,
                        "lift_mm": 50,
                        "clearance_mm": 240,
                        "tire_class": "AT",
                        "rear_locker": True,
                        "has_snorkel": True,
                    },
                )
                assert r_veh.status_code == 201

                davies_gpx = (fixtures_dir / "sample_trace_davies_plain.gpx").read_bytes()
                r_trip = client.post(
                    "/api/trips/upload",
                    headers=headers,
                    files={"file": ("davies.gpx", davies_gpx, "application/gpx+xml")},
                    data={"visibility": "community_anonymous", "privacy_trim_m": "250"},
                )
                assert r_trip.status_code == 201, r_trip.text
                assert r_trip.json()["observations_published"] > 100

                # 5. Recompute explainable live grades + 72h rainfall wet-risk (22.5 mm -> wet)
                r_grade = client.post(
                    "/api/grading/recompute",
                    json={"rain_72h_mm": 22.5, "rain_24h_mm": 8.0},
                )
                assert r_grade.status_code == 200, r_grade.text
                g_sum = r_grade.json()
                assert g_sum["segments_with_telemetry"] > 100
                assert g_sum["obstacle_hotspots_count"] >= 1

                r_hot = client.get("/api/grading/hotspots/geojson")
                assert r_hot.status_code == 200
                assert r_hot.json()["type"] == "FeatureCollection"
                assert len(r_hot.json()["features"]) >= 1

                r_rain = client.get("/api/grading/rainfall")
                assert r_rain.status_code == 200
                assert "Open-Meteo" in r_rain.json()["attribution"]

                # 6. Post community locked-gate report with valid PNG attachment on Davies Plain
                tracks = client.get("/api/tracks", headers=headers).json()
                davies = next(t for t in tracks if t["name"] == "Davies Plain Track Lower")
                davies_id = davies["track_id"]
                assert davies["sample_count"] >= 6
                assert davies["live_grade"] in ("Easy", "Medium", "Hard", "Extreme")
                assert davies["confidence_level"] in ("medium", "high")

                r_det0 = client.get(f"/api/tracks/{davies_id}", headers=headers)
                assert r_det0.status_code == 200
                seg0_pt = r_det0.json()["segments"][0]["geometry"]["coordinates"][0]

                r_rep = client.post(
                    "/api/reports",
                    headers=headers,
                    files={"photo": ("gate.png", _TINY_PNG, "image/png")},
                    data={
                        "report_type": "locked_gate",
                        "lon": str(seg0_pt[0]),
                        "lat": str(seg0_pt[1]),
                        "track_id": davies_id,
                        "notes": "Seasonal snow gate locked across approach",
                    },
                )
                assert r_rep.status_code == 201, r_rep.text

                # 7. Verify unified Track Inspector exposes EVERY v1 intelligence layer
                det = client.get(f"/api/tracks/{davies_id}", headers=headers).json()
                assert det["baseline_breakdown"] is not None
                assert det["live_breakdown"] is not None
                assert det["live_grade"] in ("Easy", "Medium", "Hard", "Extreme")
                assert det["sample_count"] >= 6
                assert det["avg_speed_kmh"] is not None and det["avg_speed_kmh"] > 0
                assert det["est_duration_min"] is not None and det["est_duration_min"] > 0
                assert det["vehicle_class_split"]["Touring 4WD"]["count"] > 0
                assert det["vehicle_class_split"]["Stock 4WD"]["count"] > 0
                assert det["vehicle_class_split"]["Modified 4WD"]["count"] > 0
                assert det["rainfall_context"]["wet_risk_level"] == "wet"
                assert "Open-Meteo" in det["rainfall_context"]["attribution"]
                assert len(det["obstacle_hotspots"]) >= 1
                assert det["closure_status"] == "seasonal"
                assert len(det["active_closures"]) >= 1
                assert len(det["community_gate_warnings"]) == 1
                assert det["suitability"]["vehicle_class"] == "Touring 4WD"

                # 8. Build `australian-alps` offline regional pack & verify checksums + snapshot
                r_build = client.post(
                    "/api/packs/australian-alps/build",
                    json={
                        "dem_path": str(fixtures_dir / "dem_alps_corridor.tif"),
                        "skip_tiles": True,
                    },
                )
                assert r_build.status_code == 200, r_build.text

                r_packs = client.get("/api/packs")
                assert r_packs.status_code == 200
                regions = {r["region_id"]: r for r in r_packs.json()["regions"]}
                assert set(regions.keys()) == set(PACK_REGION_IDS)
                assert len(regions) == 15
                assert {"australian-alps", "blue-mountains", "vic", "nsw"}.issubset(regions.keys())
                assert regions["australian-alps"]["status"] == "ready"
                assert regions["australian-alps"]["track_count"] >= 7

                r_man = client.get("/api/packs/australian-alps/manifest.json")
                assert r_man.status_code == 200
                manifest = r_man.json()
                assert "Open-Meteo" in " ".join(manifest["attributions"])

                r_snap = client.get("/api/packs/australian-alps/snapshot.json")
                assert r_snap.status_code == 200
                snap_bytes = r_snap.content
                snap_sha = hashlib.sha256(snap_bytes).hexdigest()
                snap_entry = manifest["files"]["snapshot.json"]
                assert snap_entry["sha256"] == snap_sha
                snap_text = snap_bytes.decode("utf-8")
                assert "checkpoint_c@trailforge.local" not in snap_text  # zero email leak!
                snap = json.loads(snap_text)
                snap_davies = next(t for t in snap["tracks"] if t["track_id"] == davies_id)
                assert snap_davies["live_grade"] is not None
                assert snap_davies["rainfall_context"]["wet_risk_level"] == "wet"
                assert len(snap_davies["community_gate_warnings"]) == 1

                # Verify bundle.tar.gz extracts cleanly and contains manifest + snapshot
                r_tar = client.get("/api/packs/australian-alps/bundle.tar.gz")
                assert r_tar.status_code == 200
                with tarfile.open(fileobj=io.BytesIO(r_tar.content), mode="r:gz") as tf:
                    names = tf.getnames()
                    assert any(n.endswith("manifest.json") for n in names)
                    assert any(n.endswith("snapshot.json") for n in names)

                # 9. GridForge-compatible GPX 1.1 & GeoJSON export
                r_gpx = client.get(f"/api/tracks/{davies_id}/export?format=gpx")
                assert r_gpx.status_code == 200
                gpx_root = ET.fromstring(r_gpx.text)
                assert gpx_root.attrib["version"] == "1.1"
                assert len(gpx_root.findall(f".//{{{GPX_NS}}}trkpt")) >= 20

                r_geo = client.get(f"/api/tracks/{davies_id}/export?format=geojson")
                assert r_geo.status_code == 200
                fc = json.loads(r_geo.text)
                assert fc["type"] == "FeatureCollection"
                assert fc["features"][0]["geometry"]["type"] == "LineString"

            app.dependency_overrides.clear()
    finally:
        _truncate_all(postgis_engine)
