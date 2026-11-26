"""Offline unit & PostGIS integration tests for Favorites, Personal Pins, and GPX/GeoJSON Export."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from trailforge.api.main import create_app
from trailforge.db.session import get_db
from trailforge.export.formats import (
    GPX_NS,
    ExportPin,
    ExportTrackData,
    build_gpx_document,
    build_track_geojson,
    build_user_collection_geojson,
)
from trailforge.ingestion.connectors.fixtures import load_corridor_fixtures
from trailforge.ingestion.pipeline import ingest_corridor_dataset


def test_gpx_and_geojson_formatters_unit() -> None:
    trk = ExportTrackData(
        track_id="tftrk_test123",
        name="Davies Plain Track Lower",
        state="VIC/NSW",
        grade="Extreme",
        total_length_m=6450.0,
        coords_lonlat_ele=[
            (148.1200, -36.5600, 610.5),
            (148.1210, -36.5970, 980.0),
            (148.1230, -36.6180, 1295.2),
        ],
        notes="Check river depth first",
    )
    pin = ExportPin(
        name="Murray Camp",
        lon=148.1195,
        lat=-36.5602,
        category="camp",
        elev_m=612.0,
        notes="Flat river camp",
    )

    gpx_xml = build_gpx_document(title="Test Export", tracks=[trk], waypoints=[pin])
    root = ET.fromstring(gpx_xml)
    ns = {"gpx": GPX_NS}
    assert root.attrib["version"] == "1.1"
    meta_desc = root.find("gpx:metadata/gpx:desc", ns)
    assert meta_desc is not None and "OpenStreetMap" in (meta_desc.text or "")
    assert len(root.findall("gpx:wpt", ns)) == 1
    assert len(root.findall("gpx:trk/gpx:trkseg/gpx:trkpt", ns)) == 3

    gj_track = build_track_geojson(trk, [pin])
    assert gj_track["type"] == "FeatureCollection"
    assert len(gj_track["features"]) == 2
    assert gj_track["features"][0]["geometry"]["type"] == "LineString"
    assert gj_track["features"][1]["geometry"]["type"] == "Point"

    gj_user = build_user_collection_geojson(
        owner_display_name="Tester", tracks=[trk], pins=[pin]
    )
    assert gj_user["type"] == "FeatureCollection"
    assert len(gj_user["features"]) == 2
    assert any("OpenStreetMap" in a for a in gj_user["metadata"]["attributions"])


def _truncate_all(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                TRUNCATE TABLE
                    condition_report_votes, condition_reports,
                    telemetry_observations, unmapped_track_candidates,
                    segment_closure_links, closures_alerts,
                    segment_reconciliation_log, track_segment_grades,
                    user_favorites, user_pins, vehicle_profiles, users,
                    pois, track_segments, tracks, capad_parks,
                    source_tracks_osm, source_tracks_vic, source_tracks_nsw,
                    source_waterways, source_pois, source_capad_parks
                RESTART IDENTITY CASCADE
                """
            )
        )


@pytest.mark.integration
def test_favorites_pins_and_gpx_geojson_export_api(
    postgis_engine: Engine,
    fixtures_dir: Path,
) -> None:
    _truncate_all(postgis_engine)
    try:
        with Session(postgis_engine) as session:
            ds = load_corridor_fixtures(fixtures_dir, osm_filename="osm_tracks.geojson")
            ingest_corridor_dataset(session, ds, commit=True)

            app = create_app()

            def _override_db() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db] = _override_db
            with TestClient(app) as client:
                tracks = client.get("/api/tracks").json()
                davies = next(t for t in tracks if t["name"] == "Davies Plain Track Lower")
                tid = davies["track_id"]

                # 1. Public Track Export works anonymously (GPX 1.1 + GeoJSON)
                r_gpx = client.get(f"/api/tracks/{tid}/export", params={"format": "gpx"})
                assert r_gpx.status_code == 200
                assert "application/gpx+xml" in r_gpx.headers["content-type"]
                assert ".gpx" in r_gpx.headers["content-disposition"]
                root = ET.fromstring(r_gpx.text)
                ns = {"gpx": GPX_NS}
                trkpts = root.findall("gpx:trk/gpx:trkseg/gpx:trkpt", ns)
                assert len(trkpts) > 80
                assert len(root.findall("gpx:wpt", ns)) >= 1

                r_gj = client.get(f"/api/tracks/{tid}/export", params={"format": "geojson"})
                assert r_gj.status_code == 200
                gj = json.loads(r_gj.text)
                assert gj["type"] == "FeatureCollection"
                assert gj["features"][0]["geometry"]["type"] == "LineString"
                assert len(gj["features"][0]["geometry"]["coordinates"]) > 80

                # User export requires auth
                assert client.get("/api/user/export").status_code == 401

                # 2. Register user A and save favorite + personal pins
                r_reg = client.post(
                    "/api/auth/register",
                    json={
                        "email": "explorer@trailforge.local",
                        "display_name": "Alpine Explorer",
                        "password": "MyStrongPassword9",
                    },
                )
                assert r_reg.status_code == 201
                token_a = r_reg.json()["access_token"]
                headers_a = {"Authorization": f"Bearer {token_a}"}

                r_fav = client.post(
                    "/api/favorites",
                    headers=headers_a,
                    json={"track_id": tid, "notes": "Summer crossing plan"},
                )
                assert r_fav.status_code == 201
                assert r_fav.json()["track_id"] == tid
                assert r_fav.json()["notes"] == "Summer crossing plan"

                # Idempotent re-POST updates notes without duplicate rows
                r_fav2 = client.post(
                    "/api/favorites",
                    headers=headers_a,
                    json={"track_id": tid, "notes": "Updated plan"},
                )
                assert r_fav2.status_code == 201
                favs = client.get("/api/favorites", headers=headers_a).json()
                assert len(favs) == 1
                assert favs[0]["notes"] == "Updated plan"

                # Track Inspector reflects is_favorite=True for user A
                det_a = client.get(f"/api/tracks/{tid}", headers=headers_a).json()
                assert det_a["is_favorite"] is True

                # Create, update, and verify personal pin (whitespace-only name rejected with 422)
                assert (
                    client.post(
                        "/api/pins",
                        headers=headers_a,
                        json={
                            "name": "   ",
                            "category": "camp",
                            "lon": 148.1202,
                            "lat": -36.5635,
                        },
                    ).status_code
                    == 422
                )
                r_pin = client.post(
                    "/api/pins",
                    headers=headers_a,
                    json={
                        "name": "River Camp Spot",
                        "category": "camp",
                        "notes": "Sheltered flat beside Murray",
                        "lon": 148.1202,
                        "lat": -36.5635,
                        "elev_m": 615.0,
                    },
                )
                assert r_pin.status_code == 201
                pin = r_pin.json()
                assert pin["name"] == "River Camp Spot"
                assert pin["category"] == "camp"
                assert (
                    client.patch(
                        f"/api/pins/{pin['id']}",
                        headers=headers_a,
                        json={"name": " \t "},
                    ).status_code
                    == 422
                )

                r_pin_upd = client.patch(
                    f"/api/pins/{pin['id']}",
                    headers=headers_a,
                    json={"name": "Murray Camp Flat", "category": "water"},
                )
                assert r_pin_upd.status_code == 200
                assert r_pin_upd.json()["name"] == "Murray Camp Flat"
                assert r_pin_upd.json()["category"] == "water"

                # 3. Authenticated User Collection Export (GPX + GeoJSON)
                r_ugpx = client.get(
                    "/api/user/export", headers=headers_a, params={"format": "gpx"}
                )
                assert r_ugpx.status_code == 200
                uroot = ET.fromstring(r_ugpx.text)
                assert len(uroot.findall("gpx:trk", ns)) == 1
                wpts = uroot.findall("gpx:wpt", ns)
                assert len(wpts) == 1
                assert wpts[0].find("gpx:name", ns).text == "Murray Camp Flat"

                r_ugj = client.get(
                    "/api/user/export", headers=headers_a, params={"format": "geojson"}
                )
                assert r_ugj.status_code == 200
                ugj = json.loads(r_ugj.text)
                assert len(ugj["features"]) == 2
                assert {f["geometry"]["type"] for f in ugj["features"]} == {"LineString", "Point"}

                # 4. Cross-user isolation: user B cannot see or delete user A's pin
                r_reg_b = client.post(
                    "/api/auth/register",
                    json={
                        "email": "other@trailforge.local",
                        "display_name": "Other User",
                        "password": "OtherPassword99",
                    },
                )
                headers_b = {"Authorization": f"Bearer {r_reg_b.json()['access_token']}"}
                assert client.get("/api/pins", headers=headers_b).json() == []
                assert client.delete(f"/api/pins/{pin['id']}", headers=headers_b).status_code == 404

                # 5. Cleanup delete favorite & pin for user A
                assert client.delete(f"/api/favorites/{tid}", headers=headers_a).status_code == 200
                assert client.get("/api/favorites", headers=headers_a).json() == []
                assert client.delete(f"/api/pins/{pin['id']}", headers=headers_a).status_code == 200
                assert client.get("/api/pins", headers=headers_a).json() == []
            app.dependency_overrides.clear()
    finally:
        _truncate_all(postgis_engine)
