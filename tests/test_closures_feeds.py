"""Offline unit & PostGIS integration tests for Slice 04 closures & incident feeds."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from trailforge.api.closures import router as closures_router
from trailforge.auth.security import create_access_token, hash_password
from trailforge.db.models import ClosureAlert, FeedSyncStatus, SegmentClosureLink, Track, User
from trailforge.db.session import get_db
from trailforge.feeds import (
    FEED_REGISTRY,
    FeedAdapter,
    NSWNPWSAdapter,
    NSWRFSAdapter,
    OSMAccessAdapter,
    VicEmergencyAdapter,
    VicSeasonalClosuresAdapter,
    get_closures_for_track_ids,
    get_closures_geojson,
    get_feed_health_summary,
    list_closures,
    sync_feed,
    sync_from_fixtures,
)
from trailforge.feeds.cli import build_parser
from trailforge.ingestion.connectors import load_corridor_fixtures
from trailforge.ingestion.pipeline import ingest_corridor_dataset
from trailforge.worker.main import run_due_closure_syncs


def test_all_five_adapters_implement_protocol_and_parse_mocked_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "mapshare" in url or "paim_vm_tr_road_closures" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "pfi": "SEASONAL-1",
                                "road_name": "Davies Plain Track",
                                "status": "scheduled",
                                "start_date": "2026-06-08T00:00:00Z",
                                "end_date": "2026-10-31T23:59:59Z",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[148.12, -36.58], [148.123, -36.62]],
                            },
                        }
                    ],
                },
            )
        if "emergency.vic.gov.au" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "sourceId": "VE-1",
                                "category1": "Bushfire",
                                "feedType": "Watch and Act",
                                "name": "Alpine Ridge Burn",
                                "webBody": "Active fire edge near Bucky Ridge.",
                                "updated": "2026-11-26T00:00:00Z",
                            },
                            "geometry": {"type": "Point", "coordinates": [148.16, -36.63]},
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "sourceId": "VE-IGNORE",
                                "category1": "Medical",
                                "name": "Unrelated Medical Call",
                            },
                            "geometry": {"type": "Point", "coordinates": [148.16, -36.63]},
                        },
                    ],
                },
            )
        if "rfs.nsw.gov.au" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "guid": "RFS-99",
                                "title": "Pinch River Bushfire",
                                "category": "Emergency Warning",
                                "description": "Fast-moving fire near Pinch River Fire Trail.",
                                "link": "https://www.rfs.nsw.gov.au/fire-information/fires-near-me",
                                "pubDate": "Thu, 26 Nov 2026 00:00:00 GMT",
                            },
                            "geometry": {
                                "type": "GeometryCollection",
                                "geometries": [
                                    {"type": "Point", "coordinates": [148.175, -36.555]},
                                    {
                                        "type": "Polygon",
                                        "coordinates": [
                                            [
                                                [148.16, -36.56],
                                                [148.19, -36.56],
                                                [148.19, -36.55],
                                                [148.16, -36.55],
                                                [148.16, -36.56],
                                            ]
                                        ],
                                    },
                                ],
                            },
                        }
                    ],
                },
            )
        if "nationalparks.nsw.gov.au" in url:
            return httpx.Response(
                200,
                json={
                    "alerts": [
                        {
                            "id": "NPWS-42",
                            "title": "Kosciuszko National Park — Track Closure",
                            "parkName": "Kosciuszko National Park",
                            "alertType": "park_closure",
                            "description": "Snowstorm closure across alpine roads.",
                            "url": "https://www.nationalparks.nsw.gov.au/alerts/42",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapters = [
            VicSeasonalClosuresAdapter(client=client),
            VicEmergencyAdapter(client=client),
            NSWRFSAdapter(client=client),
            NSWNPWSAdapter(client=client),
            OSMAccessAdapter(),
        ]
        for adapter in adapters:
            assert isinstance(adapter, FeedAdapter)
            assert adapter.feed_source in FEED_REGISTRY

        vic_s = adapters[0].fetch()
        assert len(vic_s) == 1
        assert vic_s[0].alert_type == "seasonal_closure"

        vic_e = adapters[1].fetch()
        assert len(vic_e) == 1  # unrelated Medical event filtered out
        assert vic_e[0].db_severity == "watch_and_act"

        nsw_rfs = adapters[2].fetch()
        assert len(nsw_rfs) == 1
        assert nsw_rfs[0].db_severity == "emergency"
        assert nsw_rfs[0].geometry is not None and nsw_rfs[0].geometry["type"] == "Polygon"

        nsw_npws = adapters[3].fetch()
        assert len(nsw_npws) == 1
        assert nsw_npws[0].park_name == "Kosciuszko National Park"
        assert nsw_npws[0].alert_type == "park_closure"


def test_feeds_cli_parser() -> None:
    parser = build_parser()
    args = parser.parse_args(["--fixtures", "tests/fixtures/closures_sample.json"])
    assert args.fixtures == Path("tests/fixtures/closures_sample.json")
    assert args.live is False


@pytest.mark.integration
def test_closures_sync_propagation_capad_join_and_outage_resilience(
    postgis_engine: Engine, fixtures_dir: Path
) -> None:
    now = datetime(2026, 11, 26, 1, 0, 0, tzinfo=UTC)

    with Session(postgis_engine) as session:
        # Ensure canonical corridor is populated first
        ingest_corridor_dataset(session, load_corridor_fixtures(fixtures_dir))

        # Clean closure tables for isolated assertion
        session.execute(delete(SegmentClosureLink))
        session.execute(delete(ClosureAlert))
        session.execute(delete(FeedSyncStatus))
        session.commit()

        # 1. Sync all 5 feeds offline from committed fixtures + OSM PostGIS flags
        summary = sync_from_fixtures(session, fixtures_dir / "closures_sample.json", now=now)
        assert summary.overall_ok is True
        by_feed = {r.feed_source: r for r in summary.results}
        assert set(by_feed.keys()) == set(FEED_REGISTRY.keys())
        assert by_feed["vic_mapshare_seasonal"].records_upserted == 1
        assert by_feed["vic_mapshare_seasonal"].segments_linked > 0
        assert by_feed["vic_emergency"].records_upserted == 1
        assert by_feed["vic_emergency"].segments_linked > 0
        assert by_feed["nsw_npws"].records_upserted == 1
        # NSW NPWS alert had geometry=None -> MUST join via CAPAD Kosciuszko National Park!
        assert by_feed["nsw_npws"].segments_linked > 0
        assert by_feed["osm_access"].records_upserted >= 2
        assert by_feed["osm_access"].segments_linked > 0

        # 2. Verify feed health is healthy & fresh
        health = get_feed_health_summary(session, now=now + timedelta(minutes=5))
        assert health["overall_status"] == "healthy"
        assert all(not f["is_stale"] for f in health["feeds"])

        # 3. Verify track-level closure rollup across all corridor tracks
        tracks = session.execute(select(Track).where(Track.is_active.is_(True))).scalars().all()
        by_name = {t.name: t.track_id for t in tracks}
        closures_by_track = get_closures_for_track_ids(
            session, list(by_name.values()), now=now + timedelta(minutes=5)
        )

        # Davies Plain Track Lower -> VIC seasonal closure (+ NSW NPWS Kosciuszko alert)
        davies_status = closures_by_track[by_name["Davies Plain Track Lower"]]
        assert davies_status["closure_status"] == "seasonal"
        assert any(c["closure_type"] == "seasonal" for c in davies_status["active_closures"])
        assert all(c["source_url"] for c in davies_status["active_closures"])

        # Bucky Ridge Fire Trail -> VicEmergency Watch & Act planned burn polygon
        bucky_status = closures_by_track[by_name["BUCKY RIDGE FIRE TRAIL"]]
        assert bucky_status["closure_status"] == "warning"
        assert any(c["closure_type"] == "incident" for c in bucky_status["active_closures"])

        # Tom Groggin Access Rd -> NSW NPWS Kosciuszko National Park CAPAD join alert
        tom_status = closures_by_track[by_name["Tom Groggin Access Rd"]]
        assert tom_status["closure_status"] == "warning"
        assert any(
            c["closure_type"] == "park_alert" and c["capad_park_name"] == "Kosciuszko National Park"
            for c in tom_status["active_closures"]
        )

        # Station Creek Private Spur -> OSM private_access + locked_gate (no junction bleed!)
        station_status = closures_by_track[by_name["Station Creek Private Spur"]]
        assert station_status["closure_status"] == "gated"
        station_types = {c["closure_type"] for c in station_status["active_closures"]}
        assert "private_access" in station_types
        assert "locked_gate" in station_types

        # Limestone Creek Bridged Link -> stays completely open (no seasonal closure bleed!)
        limestone_status = closures_by_track[by_name["Limestone Creek Bridged Link"]]
        assert limestone_status["closure_status"] == "open"
        assert limestone_status["active_closures"] == []

        # 4. Verify GeoJSON overlay contains affected segments + polygon/gate geometries
        gj = get_closures_geojson(session, now=now + timedelta(minutes=5))
        assert gj["type"] == "FeatureCollection"
        assert any(f["properties"]["feature_kind"] == "segment_closure" for f in gj["features"])
        assert any(f["properties"]["feature_kind"] == "alert_geometry" for f in gj["features"])

        # 5. Simulate NSW RFS fire incident via mocked HTTP + verify linking
        def rfs_ok(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "guid": "RFS-PINCH-1",
                                "title": "Pinch River — Emergency Fire",
                                "category": "Emergency Warning",
                                "description": "Emergency warning along Pinch River Fire Trail.",
                                "link": "https://www.rfs.nsw.gov.au/fire-information/fires-near-me",
                            },
                            "geometry": {"type": "Point", "coordinates": [148.176, -36.555]},
                        }
                    ],
                },
            )

        with httpx.Client(transport=httpx.MockTransport(rfs_ok)) as ok_client:
            rfs_res = sync_feed(session, NSWRFSAdapter(client=ok_client), now=now)
            assert rfs_res.ok is True
            assert rfs_res.segments_linked > 0

        pinch_after_rfs = get_closures_for_track_ids(
            session, [by_name["PINCH RIVER FIRE TRAIL"]], now=now
        )[by_name["PINCH RIVER FIRE TRAIL"]]
        assert pinch_after_rfs["closure_status"] == "closed"

        # 6. OUTAGE RESILIENCE: simulate HTTP 503 outage on VicEmergency -> MUST retain existing
        #    closures & segment links untouched and flag feed degraded + is_stale=True!
        def outage_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="Upstream Agency Temporarily Unavailable")

        with httpx.Client(transport=httpx.MockTransport(outage_handler)) as failing_client:
            fail_res = sync_feed(
                session,
                VicEmergencyAdapter(client=failing_client),
                now=now + timedelta(minutes=20),
            )
            assert fail_res.ok is False
            assert fail_res.status == "degraded"
            assert fail_res.retained_existing == 1
            assert fail_res.error is not None

        # Existing VicEmergency closure MUST still be active & linked, now flagged is_stale=True!
        bucky_during_outage = get_closures_for_track_ids(
            session, [by_name["BUCKY RIDGE FIRE TRAIL"]], now=now + timedelta(minutes=20)
        )[by_name["BUCKY RIDGE FIRE TRAIL"]]
        assert bucky_during_outage["closure_status"] == "warning"
        assert bucky_during_outage["active_closures"][0]["is_stale"] is True

        degraded_health = get_feed_health_summary(session, now=now + timedelta(minutes=20))
        assert degraded_health["overall_status"] == "degraded"
        ve_feed = next(f for f in degraded_health["feeds"] if f["feed_source"] == "vic_emergency")
        assert ve_feed["status"] == "degraded"
        assert ve_feed["consecutive_failures"] == 1
        assert ve_feed["is_stale"] is True
        assert ve_feed["record_count"] == 1

        # 7. TTL Staleness check: advance clock 30 days -> all feeds become stale
        stale_health = get_feed_health_summary(session, now=now + timedelta(days=30))
        assert stale_health["overall_status"] in ("degraded", "stale")
        assert all(f["is_stale"] for f in stale_health["feeds"])

        # 8. Test Closures FastAPI router endpoints end-to-end (including admin POST /sync)
        admin_email = "closures-sync-admin@example.test"
        existing_admin = session.execute(
            select(User).where(User.email == admin_email).limit(1)
        ).scalar_one_or_none()
        if existing_admin is None:
            admin_user = User(
                email=admin_email,
                display_name="Closures Admin",
                password_hash=hash_password("AdminPass123!"),
                role="admin",
                is_active=True,
            )
            session.add(admin_user)
            session.commit()
            session.refresh(admin_user)
        else:
            admin_user = existing_admin
        admin_headers = {
            "Authorization": f"Bearer {create_access_token(str(admin_user.id), admin_user.email)}"
        }

        app = FastAPI()
        app.include_router(closures_router)

        def _override_db():
            with Session(postgis_engine) as db_sess:
                yield db_sess

        app.dependency_overrides[get_db] = _override_db
        try:
            with TestClient(app) as api_client:
                r_list = api_client.get("/api/closures?active_only=true&state=VIC")
                assert r_list.status_code == 200
                payload = r_list.json()
                assert len(payload["items"]) >= 3
                assert len(payload["feeds"]) == 5

                r_geo = api_client.get("/api/closures/geojson")
                assert r_geo.status_code == 200
                assert r_geo.json()["type"] == "FeatureCollection"
                assert len(r_geo.json()["features"]) > 0

                r_status = api_client.get("/api/closures/status")
                assert r_status.status_code == 200
                assert r_status.json()["overall_status"] == "degraded"

                # Anonymous POST /sync must be rejected with 401
                r_anon = api_client.post("/api/closures/sync", json={"use_fixtures": True})
                assert r_anon.status_code == 401

                # Re-sync from fixtures recovers vic_emergency back to healthy (admin required)
                r_sync = api_client.post(
                    "/api/closures/sync",
                    headers=admin_headers,
                    json={
                        "use_fixtures": True,
                        "fixtures_path": str(fixtures_dir / "closures_sample.json"),
                    },
                )
                assert r_sync.status_code == 200
                assert r_sync.json()["overall_ok"] is True

                r_status_ok = api_client.get("/api/closures/status")
                assert r_status_ok.json()["overall_status"] == "healthy"

                # Path-traversal / arbitrary-file-read confinement regression checks (#40)
                for bad_path in ("/etc/passwd", "../../pyproject.toml", "/tmp/outside.json"):
                    r_bad = api_client.post(
                        "/api/closures/sync",
                        headers=admin_headers,
                        json={"use_fixtures": True, "fixtures_path": bad_path},
                    )
                    assert r_bad.status_code == 422, f"Expected 422 for {bad_path}"
        finally:
            session.execute(delete(User).where(User.email == admin_email))
            session.commit()

        # 9. Verify worker periodic sync respects sync_interval_s and runs offline cleanly
        res_not_due = run_due_closure_syncs(
            now=datetime.now(UTC),
            enable_live_feeds=False,
            fixtures_path=fixtures_dir / "closures_sample.json",
        )
        assert res_not_due == []

        res_due = run_due_closure_syncs(
            now=datetime.now(UTC) + timedelta(days=2),
            enable_live_feeds=False,
            fixtures_path=fixtures_dir / "closures_sample.json",
        )
        assert len(res_due) == 5
        assert all(r.ok for r in res_due)

        # Confirm list_closures with severity filter works cleanly
        crit = list_closures(session, severity="critical")
        assert all(i["severity"] == "critical" for i in crit["items"])


def test_npws_rss_and_vic_paim_wfs_parsing_offline() -> None:
    rss_xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<rss version="2.0"><channel><title>NSW National Parks</title><item>'
        "<guid>Kosciuszko-NP-Closure-1</guid>"
        "<link>https://www.nationalparks.nsw.gov.au/alerts</link>"
        "<title>Kosciuszko National Park</title>"
        "<description>&lt;strong&gt;Closed areas: Alpine Way snow closure&lt;/strong&gt;"
        "&lt;br/&gt;4WD tracks closed due to snow.</description>"
        "<pubDate>Fri, 18 Sep 2026 08:37:08 +1100</pubDate>"
        "</item></channel></rss>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "paim_vm_tr_road_closures" in url:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "vdpid": 101,
                                "ezi_road_name_label": "Block Six Rd",
                                "status": "Closed - Full",
                                "reason": "Seasonal Closure",
                                "closure_date": "2026-06-16T00:00:00Z",
                                "reopen_date": "2026-10-31T00:00:00Z",
                                "editdate": "2026-03-30T00:00:00Z",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[148.12, -36.58], [148.123, -36.62]],
                            },
                        }
                    ],
                },
            )
        if "nationalparks.nsw.gov.au" in url:
            return httpx.Response(
                200,
                text=rss_xml,
                headers={"Content-Type": "application/rss+xml; charset=utf-8"},
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        vic_items = VicSeasonalClosuresAdapter(client=client).fetch()
        assert len(vic_items) == 1
        assert vic_items[0].external_id == "101"
        assert vic_items[0].title == "Block Six Rd"
        assert vic_items[0].alert_type == "seasonal_closure"
        assert vic_items[0].db_severity == "closed"
        assert vic_items[0].effective_from is not None

        npws_items = NSWNPWSAdapter(client=client).fetch()
        assert len(npws_items) == 1
        assert npws_items[0].park_name == "Kosciuszko National Park"
        assert "Alpine Way snow closure" in npws_items[0].title
        assert npws_items[0].alert_type == "park_closure"
        assert npws_items[0].db_severity == "closed"


@pytest.mark.live
def test_live_vic_nsw_incident_feeds_smoke() -> None:
    """Optional live smoke check against VIC/NSW official closure & incident feeds."""
    vic_seasonal = VicSeasonalClosuresAdapter(timeout_s=20.0).fetch()
    vic_items = VicEmergencyAdapter(timeout_s=15.0).fetch()
    rfs_items = NSWRFSAdapter(timeout_s=15.0).fetch()
    npws_items = NSWNPWSAdapter(timeout_s=20.0).fetch()
    assert isinstance(vic_seasonal, list) and len(vic_seasonal) > 0
    assert isinstance(vic_items, list)
    assert isinstance(rfs_items, list)
    assert isinstance(npws_items, list) and len(npws_items) > 0
