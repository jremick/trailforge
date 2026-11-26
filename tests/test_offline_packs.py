"""Offline unit & PostGIS integration tests for Slice 09 Regional Offline Packs."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tracemalloc
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from trailforge.api.main import create_app
from trailforge.api.packs import get_packs_dir_dep
from trailforge.api.packs import router as packs_router
from trailforge.auth.dependencies import require_admin
from trailforge.auth.security import create_access_token, hash_password
from trailforge.db.models import OfflinePack, TrackSegment, TrackSegmentGrade, User
from trailforge.db.session import get_db
from trailforge.feeds.sync import sync_from_fixtures
from trailforge.ingestion.connectors.fixtures import load_corridor_fixtures
from trailforge.ingestion.pipeline import ingest_corridor_dataset
from trailforge.packs.builder import (
    MANIFEST_FILENAME,
    SNAPSHOT_FILENAME,
    VECTOR_TILE_FILENAME,
    build_regional_pack,
)
from trailforge.packs.catalog import (
    PACK_ATTRIBUTIONS,
    PACK_REGION_IDS,
    compute_utm_zones_for_bbox,
    get_pack_region,
    list_pack_regions,
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_test_app() -> FastAPI:
    """Create FastAPI test app and mount packs router if not yet mounted in api/main.py."""
    app = create_app()
    has_packs = any(getattr(route, "path", "").startswith("/api/packs") for route in app.routes)
    if not has_packs:
        app.include_router(packs_router)
    return app


def _assert_required_attributions(
    attributions: list[str], *, expected_states: tuple[str, ...] | None = None
) -> None:
    joined = "\n".join(attributions)
    assert "OpenStreetMap" in joined and "ODbL" in joined
    assert "Geoscience Australia" in joined and "CC BY 4.0" in joined
    assert "CAPAD 2025" in joined
    assert "Open-Meteo" in joined and "CC BY 4.0" in joined

    state_tokens = {
        "VIC": "Victoria",
        "NSW": "New South Wales",
        "ACT": "Australian Capital Territory",
        "TAS": "Tasmania",
        "QLD": "Queensland",
        "WA": "Western Australia",
        "SA": "South Australia",
        "NT": "Northern Territory",
    }
    if expected_states is None:
        assert "New South Wales" in joined
        assert "Victoria" in joined and "DEECA" in joined
    else:
        for st in expected_states:
            assert state_tokens[st] in joined, f"Missing {st} attribution in {attributions}"
        for st, token in state_tokens.items():
            if st not in expected_states:
                assert token not in joined, (
                    f"Unexpected non-intersecting state {st} attribution in {attributions}"
                )


# -----------------------------------------------------------------------------
# Pure Unit Tests (Zero DB, Zero Network)
# -----------------------------------------------------------------------------


def test_pack_catalog_locked_regions_bboxes_utm_and_attributions() -> None:
    regions = list_pack_regions()
    assert len(regions) == 15
    assert len(PACK_REGION_IDS) == 15
    assert set(list_pack_regions(category="region")[i].region_id for i in range(7)) == {
        "australian-alps",
        "blue-mountains",
        "tasmania-wilderness",
        "far-north-qld",
        "flinders-ranges",
        "red-centre",
        "perth-hills",
    }
    assert {r.region_id for r in list_pack_regions(category="state")} == {
        "vic",
        "nsw",
        "act",
        "tas",
        "qld",
        "wa",
        "sa",
        "nt",
    }

    alps = get_pack_region("australian-alps")
    assert alps.bbox == (146.5, -37.6, 149.2, -35.4)
    assert alps.corridor_bbox == (148.05, -36.68, 148.20, -36.55)
    assert alps.region_type == "tactical"
    assert alps.category == "region"
    assert alps.states == ("VIC", "NSW")
    assert alps.utm_zones == ["55H"]

    bm = get_pack_region("blue-mountains")
    assert bm.bbox == (149.7, -34.2, 150.9, -32.9)
    assert bm.region_type == "tactical"
    assert bm.category == "region"
    assert bm.states == ("NSW",)
    assert "56H" in bm.utm_zones

    vic = get_pack_region("vic")
    assert vic.bbox == (140.9, -39.2, 150.1, -33.9)
    assert vic.region_type == "state"
    assert vic.category == "state"
    assert vic.states == ("VIC",)
    assert {"54H", "55H"}.issubset(set(vic.utm_zones))

    nsw = get_pack_region("nsw")
    assert nsw.bbox == (140.9, -37.6, 153.7, -28.1)
    assert nsw.region_type == "state"
    assert nsw.category == "state"
    assert nsw.states == ("NSW", "ACT")
    assert {"54H", "55H", "56H", "56J"}.issubset(set(nsw.utm_zones))
    assert compute_utm_zones_for_bbox(nsw.bbox) == nsw.utm_zones

    _assert_required_attributions(list(PACK_ATTRIBUTIONS))
    for reg in regions:
        _assert_required_attributions(reg.attributions, expected_states=reg.states)

    with pytest.raises(ValueError):
        get_pack_region("unknown-region")


def test_packs_cli_help_exits_cleanly_without_runtime_warning() -> None:
    proc = subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-m", "trailforge.packs.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--region" in proc.stdout
    assert "australian-alps" in proc.stdout


def test_packs_api_range_206_and_strict_traversal_rejection(tmp_path: Path) -> None:
    packs_dir = tmp_path / "packs"
    alps_dir = packs_dir / "australian-alps"
    alps_dir.mkdir(parents=True)

    # Synthetic PMTiles payload + outside file + symlink escape attempt
    pmtiles_bytes = b"PMTiles\x03" + bytes(range(256))
    (alps_dir / VECTOR_TILE_FILENAME).write_bytes(pmtiles_bytes)
    (alps_dir / SNAPSHOT_FILENAME).write_text('{"region_id":"australian-alps"}\n', encoding="utf-8")
    (alps_dir / MANIFEST_FILENAME).write_text(
        json.dumps({"region_id": "australian-alps", "attributions": list(PACK_ATTRIBUTIONS)}),
        encoding="utf-8",
    )

    outside_file = tmp_path / "secret.pmtiles"
    outside_file.write_bytes(b"SECRET")
    (alps_dir / "terrain.pmtiles").symlink_to(outside_file)

    app = _build_test_app()
    app.dependency_overrides[get_packs_dir_dep] = lambda: packs_dir
    stub_admin = User(
        id=uuid.uuid4(),
        email="unit-admin@example.test",
        display_name="Unit Admin",
        password_hash="scrypt$stub",
        role="admin",
        is_active=True,
    )
    app.dependency_overrides[require_admin] = lambda: stub_admin
    try:
        with TestClient(app) as client:
            # HEAD full archive
            r_head = client.head("/api/packs/australian-alps/tiles/vector.pmtiles")
            assert r_head.status_code == 200
            assert r_head.headers["accept-ranges"] == "bytes"
            assert r_head.headers["content-length"] == str(len(pmtiles_bytes))
            assert r_head.headers["content-type"] == "application/vnd.pmtiles"
            etag = r_head.headers["etag"]

            # GET Range 206 Partial Content (exact slice)
            r_part = client.get(
                "/api/packs/australian-alps/tiles/vector.pmtiles",
                headers={"Range": "bytes=0-15"},
            )
            assert r_part.status_code == 206
            assert r_part.content == pmtiles_bytes[:16]
            assert r_part.headers["content-range"] == f"bytes 0-15/{len(pmtiles_bytes)}"
            assert r_part.headers["content-length"] == "16"

            # Suffix Range 206
            r_suf = client.get(
                "/api/packs/australian-alps/tiles/vector.pmtiles",
                headers={"Range": "bytes=-12"},
            )
            assert r_suf.status_code == 206
            assert r_suf.content == pmtiles_bytes[-12:]

            # Out-of-bounds & multi-range -> 416
            assert (
                client.get(
                    "/api/packs/australian-alps/tiles/vector.pmtiles",
                    headers={"Range": "bytes=99999-100000"},
                ).status_code
                == 416
            )
            assert (
                client.get(
                    "/api/packs/australian-alps/tiles/vector.pmtiles",
                    headers={"Range": "bytes=0-5, 10-15"},
                ).status_code
                == 416
            )

            # ETag conditional 304
            assert (
                client.get(
                    "/api/packs/australian-alps/tiles/vector.pmtiles",
                    headers={"If-None-Match": etag},
                ).status_code
                == 304
            )

            # Symlink escape blocked -> 422
            assert (
                client.get("/api/packs/australian-alps/tiles/terrain.pmtiles").status_code == 422
            )

            # Strict region & filename allowlist + traversal rejection (404 / 422)
            for bad_url in [
                "/api/packs/nonexistent-region/manifest.json",
                "/api/packs/..%2Fetc/manifest.json",
                "/api/packs/australian-alps/tiles/..%2F..%2Fetc%2Fpasswd",
                "/api/packs/australian-alps/tiles/snapshot.json",
                "/api/packs/australian-alps/tiles/sub/vector.pmtiles",
            ]:
                assert client.get(bad_url).status_code in (404, 422), bad_url

            # Confined dem_path validation on build endpoint rejects outside files -> 422
            assert (
                client.post(
                    "/api/packs/australian-alps/build",
                    json={"dem_path": "/etc/passwd"},
                ).status_code
                == 422
            )
            assert (
                client.post(
                    "/api/packs/australian-alps/build",
                    json={"dem_path": "../../etc/evil.tif"},
                ).status_code
                == 422
            )
    finally:
        app.dependency_overrides.clear()


# -----------------------------------------------------------------------------
# PostGIS Integration Tests (100% Offline)
# -----------------------------------------------------------------------------


def _truncate_all(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                TRUNCATE TABLE
                    offline_packs, rainfall_snapshots,
                    condition_report_votes, condition_reports,
                    telemetry_observations, unmapped_track_candidates,
                    segment_closure_links, closures_alerts, feed_sync_status,
                    segment_reconciliation_log, track_segment_grades,
                    user_favorites, user_pins, vehicle_profiles, invite_codes,
                    usage_metrics_hourly, users,
                    pois, track_segments, tracks, capad_parks,
                    source_tracks_osm, source_tracks_vic, source_tracks_nsw,
                    source_tracks_au_state,
                    source_waterways, source_pois, source_capad_parks
                RESTART IDENTITY CASCADE
                """
            )
        )


@pytest.mark.integration
def test_all_four_regional_packs_build_checksums_tarball_snapshot_range_and_memory(
    postgis_engine: Engine,
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packs_dir = tmp_path / "offline_packs"
    monkeypatch.setenv("TRAILFORGE_PACKS_DIR", str(packs_dir))
    _truncate_all(postgis_engine)

    reporter_email = "pack.reporter@highcountry.au"

    try:
        with Session(postgis_engine) as session:
            # 1. Seed corridor network, closures, a condition report, and a live hotspot
            ds = load_corridor_fixtures(fixtures_dir, osm_filename="osm_tracks.geojson")
            ingest_corridor_dataset(session, ds, commit=True)
            sync_from_fixtures(session, fixtures_dir / "closures_sample.json")

            # Seed one live obstacle hotspot + rainfall context on a Davies Plain segment
            sample_seg = session.execute(
                select(TrackSegment)
                .where(
                    TrackSegment.is_active.is_(True),
                    TrackSegment.name == "Davies Plain Track Lower",
                )
                .order_by(TrackSegment.segment_seq.asc())
                .limit(1)
            ).scalar_one()
            grd = session.get(TrackSegmentGrade, sample_seg.segment_id)
            assert grd is not None
            grd.live_grade = "Hard"
            grd.live_score = 72.5
            grd.confidence_level = "medium"
            grd.sample_count = 4
            grd.last_driven_at = datetime.now(UTC)
            grd.avg_speed_kmh = 5.4
            grd.avg_duration_s = 48.0
            grd.vehicle_class_split = {"Touring 4WD": 3, "Modified 4WD": 1}
            grd.wet_risk_level = "wet"
            grd.live_breakdown = {
                "version": "live_v1",
                "sample_count": 4,
                "confidence_level": "medium",
                "avg_speed_kmh": 5.4,
                "median_roughness_rms": 0.62,
                "is_obstacle_hotspot": True,
                "obstacle_reasons": ["Low crawl speed (5.4 km/h)", "High roughness (0.62 RMS)"],
                "live_score": 72.5,
                "live_grade": "Hard",
            }
            grd.rainfall_context = {
                "source": "Open-Meteo",
                "attribution": "Weather & Rainfall data by Open-Meteo.com (CC BY 4.0)",
                "rain_24h_mm": 12.0,
                "rain_72h_mm": 24.5,
                "wet_risk_level": "wet",
                "wet_score_adjustment": 6.0,
                "warning": "Recent rain increases mud/rut risk on unsealed climbs.",
                "is_stale": False,
            }
            session.commit()

            app = _build_test_app()
            from trailforge.api.reports import router as reports_router

            if not any(getattr(r, "path", "").startswith("/api/reports") for r in app.routes):
                app.include_router(reports_router)

            def _override_db() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db] = _override_db
            app.dependency_overrides[get_packs_dir_dep] = lambda: packs_dir

            with TestClient(app) as client:
                # Register user & create a locked_gate condition report inside australian-alps
                reg = client.post(
                    "/api/auth/register",
                    json={
                        "email": reporter_email,
                        "display_name": "High Country Scout",
                        "password": "OfflinePack4WD!",
                    },
                )
                assert reg.status_code == 201
                headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
                r_rep = client.post(
                    "/api/reports",
                    headers=headers,
                    json={
                        "report_type": "locked_gate",
                        "title": "Padlock on upper gate",
                        "notes": "Chain across gate near Davies Plain bend",
                        "lon": 148.1201,
                        "lat": -36.5660,
                    },
                )
                assert r_rep.status_code == 201

                # Before builds: all 15 Pan-AU regions listed as not_built with category & states
                r_list_before = client.get("/api/packs")
                assert r_list_before.status_code == 200
                before_regions = r_list_before.json()["regions"]
                assert [r["region_id"] for r in before_regions] == list(PACK_REGION_IDS)
                assert len(before_regions) == 15
                assert all(r["status"] == "not_built" for r in before_regions)
                assert all(r["category"] in ("region", "state") for r in before_regions)
                assert all(
                    isinstance(r["states"], list) and len(r["states"]) >= 1 for r in before_regions
                )

                # Create an admin user for POST /api/packs/{region_id}/build
                pack_admin = User(
                    email="pack.admin@highcountry.au",
                    display_name="Pack Admin",
                    password_hash=hash_password("AdminPack4WD!"),
                    role="admin",
                    is_active=True,
                )
                session.add(pack_admin)
                session.commit()
                session.refresh(pack_admin)
                admin_headers = {
                    "Authorization": (
                        f"Bearer {create_access_token(str(pack_admin.id), pack_admin.email)}"
                    )
                }

                # Regular member cannot build packs -> 403
                assert (
                    client.post(
                        "/api/packs/australian-alps/build",
                        headers=headers,
                        json={"dem_path": str(fixtures_dir / "dem_alps_corridor.tif")},
                    ).status_code
                    == 403
                )

                # 2. Build the 4 core VIC/NSW regions while tracking peak Python RAM (< 300 MB)
                built_rids = ("australian-alps", "blue-mountains", "vic", "nsw")
                dem_fixture = fixtures_dir / "dem_alps_corridor.tif"
                tracemalloc.start()
                try:
                    # Build australian-alps via HTTP endpoint (admin authenticated)
                    r_build_alps = client.post(
                        "/api/packs/australian-alps/build",
                        headers=admin_headers,
                        json={"dem_path": str(dem_fixture)},
                    )
                    assert r_build_alps.status_code == 200, r_build_alps.text
                    assert r_build_alps.json()["status"] == "ready"

                    # Build remaining 3 core regions cleanly offline
                    for rid in ("blue-mountains", "vic", "nsw"):
                        build_regional_pack(
                            session,
                            rid,
                            packs_dir,
                            dem_path=dem_fixture,
                        )
                    _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()

                peak_mb = peak_bytes / (1024 * 1024)
                assert peak_mb < 300.0, f"Peak Python memory {peak_mb:.1f} MB exceeded 300 MB cap"

                # 3. Verify PostGIS offline_packs table + GET /api/packs metadata
                db_packs = {
                    row.pack_id: row
                    for row in session.execute(select(OfflinePack)).scalars().all()
                }
                assert set(db_packs.keys()) == set(built_rids)
                for rid in built_rids:
                    assert db_packs[rid].status == "ready"
                    assert db_packs[rid].sha256 is not None
                    assert len(db_packs[rid].sha256) == 64

                r_list = client.get("/api/packs")
                assert r_list.status_code == 200
                regions_by_id = {r["region_id"]: r for r in r_list.json()["regions"]}
                assert all(regions_by_id[rid]["status"] == "ready" for rid in built_rids)
                assert regions_by_id["tas"]["status"] == "not_built"

                # 4. Verify manifest SHA-256 checksums & tarball extraction for ALL 4 built regions
                for rid in built_rids:
                    reg_spec = get_pack_region(rid)
                    r_man = client.get(f"/api/packs/{rid}/manifest.json")
                    assert r_man.status_code == 200, r_man.text
                    manifest = r_man.json()
                    assert manifest["region_id"] == rid
                    _assert_required_attributions(
                        manifest["attributions"], expected_states=reg_spec.states
                    )
                    assert SNAPSHOT_FILENAME in manifest["files"]

                    # Verify every listed file's size & SHA-256 on disk
                    for rel_name, meta in manifest["files"].items():
                        disk_file = packs_dir / rid / rel_name
                        assert disk_file.is_file()
                        raw = disk_file.read_bytes()
                        assert len(raw) == meta["size_bytes"]
                        assert _sha256_bytes(raw) == meta["sha256"]

                    # Verify bundle.tar.gz downloads & extracts cleanly with matching checksums
                    r_bundle = client.get(f"/api/packs/{rid}/bundle.tar.gz")
                    assert r_bundle.status_code == 200
                    extract_dir = tmp_path / f"extracted_{rid}"
                    extract_dir.mkdir(parents=True)
                    with tarfile.open(fileobj=io.BytesIO(r_bundle.content), mode="r:gz") as tar:
                        tar.extractall(extract_dir, filter="data")

                    ext_root = extract_dir / rid
                    assert (ext_root / MANIFEST_FILENAME).is_file()
                    assert (ext_root / SNAPSHOT_FILENAME).is_file()
                    for rel_name, meta in manifest["files"].items():
                        ext_bytes = (ext_root / rel_name).read_bytes()
                        assert _sha256_bytes(ext_bytes) == meta["sha256"]

                # 5. Deep snapshot verification for australian-alps (populated) & blue-mountains
                r_snap = client.get("/api/packs/australian-alps/snapshot.json")
                assert r_snap.status_code == 200
                snap = r_snap.json()
                assert snap["region_id"] == "australian-alps"
                _assert_required_attributions(snap["attributions"])
                assert len(snap["tracks"]) >= 5
                assert len(snap["pois"]) >= 1
                assert len(snap["capad_parks"]) >= 1
                assert len(snap["closures"]) >= 1
                assert len(snap["reports"]) >= 1
                assert snap["hotspots"]["type"] == "FeatureCollection"
                assert len(snap["hotspots"]["features"]) >= 1

                # Zero author email leaked into offline snapshot
                assert reporter_email not in r_snap.text.lower()

                davies_snap = next(
                    t for t in snap["tracks"] if t["name"] == "Davies Plain Track Lower"
                )
                assert davies_snap["baseline_breakdown"] is not None
                assert len(davies_snap["elevation_profile"]) > 0
                assert len(davies_snap["active_closures"]) >= 1
                assert len(davies_snap["community_gate_warnings"]) >= 1
                assert davies_snap["live_grade"] == "Hard"
                assert davies_snap["rainfall_context"]["wet_risk_level"] == "wet"
                assert len(davies_snap["obstacle_hotspots"]) >= 1

                # Empty region (blue-mountains) builds cleanly with 0 tracks and valid structure
                bm_snap = client.get("/api/packs/blue-mountains/snapshot.json").json()
                assert bm_snap["region_id"] == "blue-mountains"
                assert bm_snap["tracks"] == []
                assert bm_snap["counts"]["tracks"] == 0

                # 6. Verify real PMTiles Range 206 when tippecanoe & pmtiles exist (Docker env)
                if shutil.which("tippecanoe") and shutil.which("pmtiles"):
                    alps_man = client.get("/api/packs/australian-alps/manifest.json").json()
                    assert alps_man["vector_available"] is True
                    assert alps_man["terrain_available"] is True
                    for tile_name in ("vector.pmtiles", "terrain.pmtiles"):
                        r_tile_head = client.head(f"/api/packs/australian-alps/tiles/{tile_name}")
                        assert r_tile_head.status_code == 200
                        assert r_tile_head.headers["accept-ranges"] == "bytes"
                        r_tile_range = client.get(
                            f"/api/packs/australian-alps/tiles/{tile_name}",
                            headers={"Range": "bytes=0-126"},
                        )
                        assert r_tile_range.status_code == 206
                        assert r_tile_range.content[:7] == b"PMTiles"
                        assert len(r_tile_range.content) == 127

            app.dependency_overrides.clear()
    finally:
        _truncate_all(postgis_engine)
