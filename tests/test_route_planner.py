"""Offline unit & PostGIS integration tests for the Multi-Track Route Planner & GridForge Export."""

from __future__ import annotations

import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect, text
from sqlalchemy.orm import Session

from trailforge.api.main import create_app
from trailforge.api.tracks import PoiResponse
from trailforge.db.session import get_db
from trailforge.export.formats import GPX_NS
from trailforge.feeds import sync_from_fixtures
from trailforge.grading.live import recompute_live_grades
from trailforge.ingestion.connectors.fixtures import load_corridor_fixtures
from trailforge.ingestion.pipeline import ingest_corridor_dataset
from trailforge.routes import (
    MAX_PROFILE_POINTS,
    OrientedLegTrack,
    PlannedRouteAnalysis,
    RouteElevationPoint,
    RouteLegSummary,
    RouteSafetySummary,
    RouteWaterwayCrossing,
    RouteWaypoint,
    TrackNotFoundError,
    build_gridforge_cli,
    build_route_geojson,
    build_route_gpx,
    plan_route,
    slugify_route_name,
)
from trailforge.routes.planner import downsample_elevation_profile, optimize_leg_orientations

# =============================================================================
# Pure Offline Unit Tests (Zero DB Required)
# =============================================================================


def test_optimize_leg_orientations_forward_reverse_junction_and_gap() -> None:
    # Contiguous A -> B forward
    ep_a = ((148.120, -36.560), (148.123, -36.618))
    ep_b = ((148.123, -36.618), (148.126, -36.662))
    rev, gaps = optimize_leg_orientations([ep_a, ep_b])
    assert rev == [False, False]
    assert gaps == [0.0, 0.0]

    # Reversed travel B -> A flips both legs cleanly to 0 m jump
    rev_ba, gaps_ba = optimize_leg_orientations([ep_b, ep_a])
    assert rev_ba == [True, True]
    assert gaps_ba == [0.0, 0.0]

    # Start-to-start junction (Poplars starts at same point as Davies Plain Lower)
    ep_poplars = ((148.120, -36.560), (148.162, -36.553))
    rev_junc, gaps_junc = optimize_leg_orientations([ep_poplars, ep_a])
    assert rev_junc == [True, False]
    assert gaps_junc == [0.0, 0.0]

    # Disconnected tracks (> 5 km apart) report accurate non-contiguous gap > 100 m
    ep_access = ((148.065, -36.556), (148.120, -36.560))
    rev_gap, gaps_gap = optimize_leg_orientations([ep_access, ep_b])
    assert rev_gap == [False, False]
    assert gaps_gap[0] == 0.0
    assert gaps_gap[1] > 6000.0


def test_downsample_elevation_profile_caps_at_600_and_preserves_leg_boundaries() -> None:
    pts: list[RouteElevationPoint] = []
    for i in range(1500):
        tid = "tftrk_leg1" if i < 750 else "tftrk_leg2"
        tname = "Leg One" if i < 750 else "Leg Two"
        elev = 600.0 + (i * 0.5) if i < 750 else 975.0 + ((i - 750) * 0.3)
        pts.append(
            RouteElevationPoint(
                dist_m=float(i * 10),
                elev_m=round(elev, 2),
                slope_deg=8.0,
                grade="Hard" if i < 750 else "Medium",
                track_id=tid,
                track_name=tname,
            )
        )

    sampled = downsample_elevation_profile(pts, max_points=MAX_PROFILE_POINTS)
    assert len(sampled) <= MAX_PROFILE_POINTS
    assert sampled[0].dist_m == 0.0
    assert sampled[-1].dist_m == pts[-1].dist_m
    # Monotonic distances & both legs represented
    assert all(sampled[k].dist_m >= sampled[k - 1].dist_m for k in range(1, len(sampled)))
    assert {p.track_id for p in sampled} == {"tftrk_leg1", "tftrk_leg2"}


def test_gridforge_cli_and_slug_formatting() -> None:
    assert slugify_route_name('Davies Plain "High Country" Traverse!') == (
        "davies-plain-high-country-traverse"
    )
    cli = build_gridforge_cli('Davies Plain "High Country" Traverse')
    assert cli.startswith('docker run --rm -v "$(pwd)/out:/out" gridforge:dev gridforge render ')
    assert "--strip-route /out/davies-plain-high-country-traverse.gpx" in cli
    assert "--scale 25000 --paper a4 --dem-source au-elvis --au-overlay au-state-trails" in cli
    assert "--elevation-profile --route-card --calibration-page" in cli
    assert '--title "DAVIES PLAIN HIGH COUNTRY TRAVERSE"' in cli
    assert "--output /out/davies-plain-high-country-traverse_strip_atlas.pdf" in cli


def test_route_gpx_and_geojson_exporters_unit() -> None:
    analysis = PlannedRouteAnalysis(
        name="Davies Plain Traverse",
        track_ids=["tftrk_a", "tftrk_b"],
        states=["NSW", "VIC"],
        total_length_m=11300.0,
        est_duration_min=78.5,
        min_elev_m=610.0,
        max_elev_m=1640.0,
        elev_gain_m=1050.0,
        elev_loss_m=120.0,
        max_slope_deg=21.4,
        sustained_slope_deg=14.2,
        worst_grade="Extreme",
        worst_score=86.5,
        effective_grade="Extreme",
        suitability=None,
        safety_summary=RouteSafetySummary(
            has_blocking_closure=True,
            closure_count=1,
            gate_warning_count=1,
            active_report_count=1,
            unbridged_crossings=1,
            wet_risk_level="wet",
            warnings=["Seasonal closure active"],
        ),
        closures=[
            {
                "closure_id": 10,
                "title": "Winter Seasonal Closure",
                "agency": "DEECA VIC",
                "closure_type": "seasonal",
                "severity": "warning",
                "affected_track_ids": ["tftrk_a"],
                "lon": 148.121,
                "lat": -36.580,
            }
        ],
        community_gate_warnings=[
            {
                "id": "rep-gate-1",
                "title": "Locked steel gate",
                "notes": "Padlocked at boundary fence",
                "lon": 148.124,
                "lat": -36.630,
            }
        ],
        condition_reports=[],
        waterway_crossings=[
            RouteWaterwayCrossing(
                name="Murray River",
                waterway_type="River",
                perennial=True,
                bridged=False,
                dist_m=420.0,
                track_id="tftrk_a",
                track_name="Davies Plain Track Lower",
                lon=148.120,
                lat=-36.564,
            )
        ],
        nearby_pois=[
            PoiResponse(
                id=1,
                poi_key="hut:davies-plain-hut:148126:-36662",
                name="Davies Plain Hut",
                poi_type="hut",
                state="VIC",
                elev_m=1620.0,
                primary_source="vic_recweb_hut",
                source_license="CC-BY-4.0",
                lon=148.126,
                lat=-36.662,
                distance_m=45.0,
            )
        ],
        capad_parks=[],
        elevation_profile=[],
        legs=[
            RouteLegSummary(
                index=0,
                track_id="tftrk_a",
                name="Davies Plain Track Lower",
                state="VIC",
                length_m=6400.0,
                est_duration_min=54.9,
                grade="Extreme",
                score=86.5,
                closure_status="seasonal",
                gate_warning_count=0,
                reversed=False,
                gap_from_prev_m=0.0,
            ),
            RouteLegSummary(
                index=1,
                track_id="tftrk_b",
                name="Davies Plain Ridge",
                state="VIC",
                length_m=4900.0,
                est_duration_min=23.6,
                grade="Hard",
                score=68.0,
                closure_status="open",
                gate_warning_count=1,
                reversed=False,
                gap_from_prev_m=0.0,
            ),
        ],
        leg_gaps=[],
        waypoints=[
            RouteWaypoint(
                name="Lunch Clearing",
                category="camp",
                lon=148.123,
                lat=-36.618,
            )
        ],
        bbox=[148.120, -36.662, 148.126, -36.560],
        geometry={"type": "FeatureCollection", "features": []},
        gridforge_cli=build_gridforge_cli("Davies Plain Traverse"),
        has_osm_source=True,
    )
    leg_tracks = [
        OrientedLegTrack(
            index=0,
            track_id="tftrk_a",
            name="Davies Plain Track Lower",
            state="VIC",
            grade="Extreme",
            score=86.5,
            length_m=6400.0,
            closure_status="seasonal",
            reversed=False,
            gap_from_prev_m=0.0,
            coords_lonlat_ele=[
                (148.120, -36.560, 610.0),
                (148.123, -36.618, 1290.0),
            ],
        ),
        OrientedLegTrack(
            index=1,
            track_id="tftrk_b",
            name="Davies Plain Ridge",
            state="VIC",
            grade="Hard",
            score=68.0,
            length_m=4900.0,
            closure_status="open",
            reversed=False,
            gap_from_prev_m=0.0,
            coords_lonlat_ele=[
                (148.123, -36.618, 1290.0),
                (148.126, -36.662, 1640.0),
            ],
        ),
    ]

    gpx_xml = build_route_gpx(analysis, leg_tracks=leg_tracks)
    root = ET.fromstring(gpx_xml)
    ns = {"gpx": GPX_NS}
    assert root.attrib["version"] == "1.1"
    assert "GPL-3.0" in root.attrib["creator"]
    meta_desc = root.find("gpx:metadata/gpx:desc", ns)
    assert meta_desc is not None and "OpenStreetMap" in (meta_desc.text or "")
    # Single <trk> with 2 <trkseg> legs
    trks = root.findall("gpx:trk", ns)
    assert len(trks) == 1
    assert len(trks[0].findall("gpx:trkseg", ns)) == 2
    assert len(trks[0].findall("gpx:trkseg/gpx:trkpt", ns)) == 4
    # Waypoints include custom waypoint + hut + Murray River crossing + gate + closure
    wpt_names = {el.text for el in root.findall("gpx:wpt/gpx:name", ns)}
    assert "Lunch Clearing" in wpt_names
    assert "Davies Plain Hut" in wpt_names
    assert any("Murray River" in (n or "") for n in wpt_names)
    assert any("Locked steel gate" in (n or "") for n in wpt_names)
    assert any("Winter Seasonal Closure" in (n or "") for n in wpt_names)

    gj = build_route_geojson(analysis, leg_tracks=leg_tracks)
    assert gj["type"] == "FeatureCollection"
    assert gj["bbox"] == [148.120, -36.662, 148.126, -36.560]
    line_feats = [f for f in gj["features"] if f["geometry"]["type"] == "LineString"]
    pt_feats = [f for f in gj["features"] if f["geometry"]["type"] == "Point"]
    assert len(line_feats) == 2
    assert len(pt_feats) == 5
    assert any("OpenStreetMap" in a for a in gj["metadata"]["attributions"])


# =============================================================================
# PostGIS Integration Tests (Offline Corridor Fixtures)
# =============================================================================


def _truncate_route_test_tables(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                TRUNCATE TABLE
                    planned_routes,
                    condition_report_votes, condition_reports,
                    telemetry_observations, unmapped_track_candidates,
                    segment_closure_links, closures_alerts, feed_sync_status,
                    rainfall_snapshots,
                    segment_reconciliation_log, track_segment_grades,
                    user_favorites, user_pins, vehicle_profiles, users,
                    pois, track_segments, tracks, capad_parks,
                    source_tracks_osm, source_tracks_vic, source_tracks_nsw,
                    source_waterways, source_pois, source_capad_parks
                RESTART IDENTITY CASCADE
                """
            )
        )


def _register_user(client: TestClient, email: str, display_name: str) -> dict[str, str]:
    resp = client.post(
        "/api/auth/register",
        json={
            "email": email,
            "display_name": display_name,
            "password": "TrailPassword99",
        },
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]
    # Clear HttpOnly cookie on shared TestClient so Bearer headers explicitly select caller identity
    client.cookies.clear()
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.integration
def test_planned_routes_table_is_strictly_tier_c_and_cascades(postgis_engine: Engine) -> None:
    insp = inspect(postgis_engine)
    assert "planned_routes" in insp.get_table_names(schema="public")
    cols = {c["name"] for c in insp.get_columns("planned_routes")}
    assert {
        "id",
        "user_id",
        "name",
        "notes",
        "track_ids",
        "waypoints_json",
        "vehicle_class",
        "created_at",
        "updated_at",
    }.issubset(cols)
    # Tier C ODbL guarantee: zero raw OSM geometry/tag columns
    assert "geom" not in cols
    assert "osm_way_id" not in cols
    assert "tags" not in cols
    idx_names = {idx["name"] for idx in insp.get_indexes("planned_routes")}
    assert "ix_planned_routes_user_id" in idx_names


@pytest.mark.integration
def test_route_planner_chaining_orientation_safety_crud_and_exports(
    postgis_engine: Engine,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    _truncate_route_test_tables(postgis_engine)
    try:
        with Session(postgis_engine) as session:
            ds = load_corridor_fixtures(fixtures_dir, osm_filename="osm_tracks.geojson")
            ingest_corridor_dataset(session, ds, commit=True)
            # Load offline VIC/NSW closures + apply heavy-rain snapshot for safety checks
            sync_from_fixtures(session, fixtures_dir / "closures_sample.json")
            recompute_live_grades(session, rain_override_72h_mm=42.0)
            session.commit()

            app = create_app()

            def _override_db() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db] = _override_db
            with TestClient(app) as client:
                tracks = client.get("/api/tracks").json()
                by_name = {t["name"]: t for t in tracks}
                t_access = by_name["Tom Groggin Access Rd"]["track_id"]
                t_lower = by_name["Davies Plain Track Lower"]["track_id"]
                t_ridge = by_name["Davies Plain Ridge"]["track_id"]

                # 1. Direct engine check: unknown track_id raises TrackNotFoundError (a ValueError)
                with pytest.raises(TrackNotFoundError) as exc_info:
                    plan_route(session, [t_lower, "tftrk_doesnotexist"])
                assert isinstance(exc_info.value, ValueError)
                assert "tftrk_doesnotexist" in exc_info.value.missing_track_ids

                # 2. Register User A, add Touring 4WD profile, and post locked-gate report on Ridge
                headers_a = _register_user(client, "planner-a@trailforge.local", "Alpine Planner")
                r_veh = client.post(
                    "/api/vehicles",
                    headers=headers_a,
                    json={
                        "name": "76 Series LandCruiser",
                        "drivetrain": "4wd_part_time",
                        "low_range": True,
                        "lift_mm": 50,
                        "clearance_mm": 245,
                        "tire_class": "AT",
                        "front_locker": False,
                        "rear_locker": True,
                        "has_winch": True,
                        "has_snorkel": True,
                        "is_active": True,
                    },
                )
                assert r_veh.status_code == 201, r_veh.text

                r_rep = client.post(
                    "/api/reports",
                    headers=headers_a,
                    json={
                        "report_type": "locked_gate",
                        "title": "Locked boundary gate on ridge",
                        "notes": "Chain and padlock installed across ridge track.",
                        "severity": "impassable",
                        "lon": 148.1245,
                        "lat": -36.6345,
                        "track_id": t_ridge,
                    },
                )
                assert r_rep.status_code == 201, r_rep.text

                # 3. Public POST /api/routes/plan chaining Davies Plain Lower + Ridge (anonymous)
                r_plan_anon = client.post(
                    "/api/routes/plan",
                    json={
                        "name": "Davies Plain Full Climb",
                        "track_ids": [t_lower, t_ridge],
                        "waypoints": [
                            {
                                "name": "Ridge Saddle Lunch",
                                "category": "camp",
                                "lon": 148.123,
                                "lat": -36.618,
                            }
                        ],
                    },
                )
                assert r_plan_anon.status_code == 200, r_plan_anon.text
                plan = r_plan_anon.json()

                assert plan["name"] == "Davies Plain Full Climb"
                assert plan["track_ids"] == [t_lower, t_ridge]
                assert len(plan["legs"]) == 2
                assert plan["legs"][0]["reversed"] is False
                assert plan["legs"][1]["reversed"] is False
                assert plan["legs"][1]["gap_from_prev_m"] <= 10.0
                assert plan["leg_gaps"] == []

                expected_len = (
                    by_name["Davies Plain Track Lower"]["total_length_m"]
                    + by_name["Davies Plain Ridge"]["total_length_m"]
                )
                assert abs(plan["total_length_m"] - expected_len) < 150.0
                assert plan["est_duration_min"] > 20.0
                assert plan["min_elev_m"] is not None and plan["max_elev_m"] is not None
                assert plan["max_elev_m"] > plan["min_elev_m"] + 400.0
                assert plan["elev_gain_m"] > 400.0
                assert plan["worst_grade"] in ("Hard", "Extreme")
                assert plan["effective_grade"] == plan["worst_grade"]
                assert plan["worst_score"] > 50.0
                assert plan["suitability"] is None  # anonymous with no vehicle_class

                # Safety summary aggregates closure + gate warning + heavy rain + crossings
                safety = plan["safety_summary"]
                assert safety["has_blocking_closure"] is True
                assert safety["closure_count"] >= 1
                assert safety["gate_warning_count"] >= 1
                assert safety["active_report_count"] >= 1
                assert safety["unbridged_crossings"] >= 1
                assert safety["wet_risk_level"] == "heavy_rain"
                assert len(safety["warnings"]) >= 3

                assert len(plan["closures"]) >= 1
                assert len(plan["community_gate_warnings"]) == 1
                assert len(plan["waterway_crossings"]) >= 1
                assert plan["waterway_crossings"][0]["dist_m"] >= 0.0
                assert len(plan["nearby_pois"]) >= 1
                assert len(plan["capad_parks"]) >= 1
                assert 10 <= len(plan["elevation_profile"]) <= MAX_PROFILE_POINTS
                assert {p["track_id"] for p in plan["elevation_profile"]} == {t_lower, t_ridge}
                assert len(plan["bbox"]) == 4
                assert plan["geometry"]["type"] == "FeatureCollection"
                assert len(plan["geometry"]["features"]) == 2
                assert "--strip-route /out/davies-plain-full-climb.gpx" in plan["gridforge_cli"]
                assert '--title "DAVIES PLAIN FULL CLIMB"' in plan["gridforge_cli"]

                # 4. Automatic orientation when chaining in reverse [Ridge, Lower]
                r_rev = client.post(
                    "/api/routes/plan",
                    json={"track_ids": [t_ridge, t_lower]},
                )
                assert r_rev.status_code == 200, r_rev.text
                rev_plan = r_rev.json()
                assert rev_plan["legs"][0]["reversed"] is True
                assert rev_plan["legs"][1]["reversed"] is True
                assert rev_plan["leg_gaps"] == []
                assert rev_plan["elev_loss_m"] > rev_plan["elev_gain_m"]

                # 5. Non-contiguous gap detection (> 100 m) when skipping Davies Plain Lower
                r_gap = client.post(
                    "/api/routes/plan",
                    json={"track_ids": [t_access, t_ridge]},
                )
                assert r_gap.status_code == 200, r_gap.text
                gap_plan = r_gap.json()
                assert len(gap_plan["leg_gaps"]) == 1
                assert gap_plan["leg_gaps"][0]["from_track_id"] == t_access
                assert gap_plan["leg_gaps"][0]["to_track_id"] == t_ridge
                assert gap_plan["leg_gaps"][0]["gap_m"] > 5000.0
                assert gap_plan["legs"][1]["gap_from_prev_m"] > 5000.0
                assert any("gap" in w.lower() for w in gap_plan["safety_summary"]["warnings"])

                # 6. Personalized rig suitability: AWD vs active Touring 4WD profile
                r_awd = client.post(
                    "/api/routes/plan",
                    json={"track_ids": [t_lower, t_ridge], "vehicle_class": "AWD"},
                )
                assert r_awd.status_code == 200
                assert r_awd.json()["suitability"]["vehicle_class"] == "AWD"
                assert r_awd.json()["suitability"]["rating"] == "High Risk for your rig"

                r_signed_in = client.post(
                    "/api/routes/plan",
                    headers=headers_a,
                    json={"track_ids": [t_lower, t_ridge]},
                )
                assert r_signed_in.status_code == 200
                suit_a = r_signed_in.json()["suitability"]
                assert suit_a is not None
                assert suit_a["vehicle_class"] == "Touring 4WD"
                assert suit_a["vehicle_name"] == "76 Series LandCruiser"
                assert len(suit_a["warnings"]) >= 1

                # 7. Validation errors (422) & unknown track_id (404)
                assert client.post("/api/routes/plan", json={"track_ids": []}).status_code == 422
                assert (
                    client.post(
                        "/api/routes/plan",
                        json={"name": "   ", "track_ids": [t_lower]},
                    ).status_code
                    == 422
                )
                assert (
                    client.post(
                        "/api/routes/plan",
                        json={"track_ids": [t_lower] * 31},
                    ).status_code
                    == 422
                )
                assert (
                    client.post(
                        "/api/routes/plan",
                        json={"track_ids": [t_lower], "vehicle_class": "Monster Truck"},
                    ).status_code
                    == 422
                )
                assert (
                    client.post(
                        "/api/routes/plan",
                        json={"track_ids": [t_lower, "tftrk_unknown_999"]},
                    ).status_code
                    == 404
                )

                # 8. Public ad-hoc GPX 1.1 & GeoJSON export (+ GDAL ogrinfo verification!)
                r_adhoc_gpx = client.post(
                    "/api/routes/plan/export",
                    params={"format": "gpx"},
                    json={
                        "name": "Davies Plain Full Climb",
                        "track_ids": [t_lower, t_ridge],
                        "waypoints": [
                            {
                                "name": "Ridge Saddle Lunch",
                                "category": "camp",
                                "lon": 148.123,
                                "lat": -36.618,
                            }
                        ],
                    },
                )
                assert r_adhoc_gpx.status_code == 200, r_adhoc_gpx.text
                assert "application/gpx+xml" in r_adhoc_gpx.headers["content-type"]
                assert "davies-plain-full-climb.gpx" in r_adhoc_gpx.headers["content-disposition"]
                gpx_root = ET.fromstring(r_adhoc_gpx.text)
                ns = {"gpx": GPX_NS}
                assert gpx_root.attrib["version"] == "1.1"
                assert len(gpx_root.findall("gpx:trk/gpx:trkseg", ns)) == 2
                assert len(gpx_root.findall("gpx:trk/gpx:trkseg/gpx:trkpt", ns)) > 100
                assert len(gpx_root.findall("gpx:wpt", ns)) >= 4

                r_adhoc_gj = client.post(
                    "/api/routes/plan/export",
                    params={"format": "geojson"},
                    json={
                        "name": "Davies Plain Full Climb",
                        "track_ids": [t_lower, t_ridge],
                        "waypoints": [
                            {
                                "name": "Ridge Saddle Lunch",
                                "category": "camp",
                                "lon": 148.123,
                                "lat": -36.618,
                            }
                        ],
                    },
                )
                assert r_adhoc_gj.status_code == 200, r_adhoc_gj.text
                assert "application/geo+json" in r_adhoc_gj.headers["content-type"]
                adhoc_gj = json.loads(r_adhoc_gj.text)
                assert adhoc_gj["type"] == "FeatureCollection"
                assert sum(
                    1 for f in adhoc_gj["features"] if f["geometry"]["type"] == "LineString"
                ) == 2
                assert sum(
                    1 for f in adhoc_gj["features"] if f["geometry"]["type"] == "Point"
                ) >= 4

                # Verify with GDAL ogrinfo when installed (always present inside Docker backend)
                if shutil.which("ogrinfo"):
                    gpx_path = tmp_path / "davies_route.gpx"
                    gj_path = tmp_path / "davies_route.geojson"
                    gpx_path.write_text(r_adhoc_gpx.text, encoding="utf-8")
                    gj_path.write_text(r_adhoc_gj.text, encoding="utf-8")

                    gpx_check = subprocess.run(
                        ["ogrinfo", "-so", "-al", str(gpx_path)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    assert gpx_check.returncode == 0, gpx_check.stderr
                    assert "tracks" in gpx_check.stdout.lower()
                    assert "waypoints" in gpx_check.stdout.lower()

                    gj_check = subprocess.run(
                        ["ogrinfo", "-so", "-al", str(gj_path)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    assert gj_check.returncode == 0, gj_check.stderr
                    assert "Feature Count:" in gj_check.stdout

                # 9. Authenticated CRUD requires login (401 when anonymous)
                assert client.get("/api/routes").status_code == 401
                assert (
                    client.post(
                        "/api/routes",
                        json={"name": "Test", "track_ids": [t_lower]},
                    ).status_code
                    == 401
                )

                # Reject blank name & empty track_ids on POST /api/routes (422) & missing (404)
                assert (
                    client.post(
                        "/api/routes",
                        headers=headers_a,
                        json={"name": "   ", "track_ids": [t_lower]},
                    ).status_code
                    == 422
                )
                assert (
                    client.post(
                        "/api/routes",
                        headers=headers_a,
                        json={"name": "Empty", "track_ids": []},
                    ).status_code
                    == 422
                )
                assert (
                    client.post(
                        "/api/routes",
                        headers=headers_a,
                        json={"name": "Missing", "track_ids": ["tftrk_nope"]},
                    ).status_code
                    == 404
                )

                # Create saved route for User A
                r_create = client.post(
                    "/api/routes",
                    headers=headers_a,
                    json={
                        "name": "High Country Weekend",
                        "notes": "Pack recovery boards and check river depth.",
                        "track_ids": [t_lower, t_ridge],
                        "waypoints": [
                            {
                                "name": "Base Camp",
                                "category": "camp",
                                "lon": 148.1202,
                                "lat": -36.561,
                            }
                        ],
                    },
                )
                assert r_create.status_code == 201, r_create.text
                saved = r_create.json()
                route_id = saved["id"]
                assert saved["name"] == "High Country Weekend"
                assert saved["track_ids"] == [t_lower, t_ridge]
                assert len(saved["waypoints"]) == 1
                assert saved["analysis"]["total_length_m"] > 10000.0
                assert saved["analysis"]["suitability"]["vehicle_class"] == "Touring 4WD"

                # List saved routes for User A
                r_list = client.get("/api/routes", headers=headers_a)
                assert r_list.status_code == 200
                listed = r_list.json()
                assert len(listed) == 1
                assert listed[0]["id"] == route_id
                assert listed[0]["leg_count"] == 2
                assert listed[0]["has_blocking_closure"] is True
                assert listed[0]["gate_warning_count"] >= 1
                assert listed[0]["wet_risk_level"] == "heavy_rain"

                # GET saved route recomputes live analysis
                r_get = client.get(f"/api/routes/{route_id}", headers=headers_a)
                assert r_get.status_code == 200
                assert r_get.json()["analysis"]["safety_summary"]["has_blocking_closure"] is True

                # PATCH saved route (reject blank name, then update cleanly)
                assert (
                    client.patch(
                        f"/api/routes/{route_id}",
                        headers=headers_a,
                        json={"name": " \t "},
                    ).status_code
                    == 422
                )
                r_patch = client.patch(
                    f"/api/routes/{route_id}",
                    headers=headers_a,
                    json={
                        "name": "Tom Groggin to Davies Ridge",
                        "track_ids": [t_access, t_lower, t_ridge],
                        "vehicle_class": "Modified 4WD",
                        "notes": "Updated 3-leg traverse",
                    },
                )
                assert r_patch.status_code == 200, r_patch.text
                patched = r_patch.json()
                assert patched["name"] == "Tom Groggin to Davies Ridge"
                assert len(patched["track_ids"]) == 3
                assert len(patched["analysis"]["legs"]) == 3
                assert patched["analysis"]["leg_gaps"] == []
                assert patched["analysis"]["suitability"]["vehicle_class"] == "Modified 4WD"

                # Saved route export (GPX & GeoJSON)
                r_exp_gpx = client.get(
                    f"/api/routes/{route_id}/export",
                    headers=headers_a,
                    params={"format": "gpx"},
                )
                assert r_exp_gpx.status_code == 200
                saved_root = ET.fromstring(r_exp_gpx.text)
                assert len(saved_root.findall("gpx:trk/gpx:trkseg", ns)) == 3

                r_exp_gj = client.get(
                    f"/api/routes/{route_id}/export",
                    headers=headers_a,
                    params={"format": "geojson"},
                )
                assert r_exp_gj.status_code == 200
                saved_gj = json.loads(r_exp_gj.text)
                assert sum(
                    1 for f in saved_gj["features"] if f["geometry"]["type"] == "LineString"
                ) == 3

                # 10. Owner isolation: User B cannot view, patch, export, or delete User A's route
                headers_b = _register_user(client, "planner-b@trailforge.local", "Other Driver")
                assert client.get("/api/routes", headers=headers_b).json() == []
                assert client.get(f"/api/routes/{route_id}", headers=headers_b).status_code == 404
                assert (
                    client.patch(
                        f"/api/routes/{route_id}",
                        headers=headers_b,
                        json={"name": "Hijacked"},
                    ).status_code
                    == 404
                )
                assert (
                    client.get(
                        f"/api/routes/{route_id}/export",
                        headers=headers_b,
                        params={"format": "gpx"},
                    ).status_code
                    == 404
                )
                assert (
                    client.delete(f"/api/routes/{route_id}", headers=headers_b).status_code == 404
                )
                # Invalid UUID also returns 404 cleanly
                assert client.get("/api/routes/not-a-uuid", headers=headers_a).status_code == 404

                # 11. Owner deletes saved route cleanly
                r_del = client.delete(f"/api/routes/{route_id}", headers=headers_a)
                assert r_del.status_code == 200
                assert r_del.json() == {"ok": True, "id": route_id}
                assert client.get(f"/api/routes/{route_id}", headers=headers_a).status_code == 404
                assert client.get("/api/routes", headers=headers_a).json() == []

            app.dependency_overrides.clear()
    finally:
        _truncate_route_test_tables(postgis_engine)
