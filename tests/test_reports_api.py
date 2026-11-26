"""Offline unit & PostGIS integration tests for Slice 08 Community Condition Reports."""

from __future__ import annotations

import json
import struct
import uuid
import zlib
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from trailforge.api.main import create_app
from trailforge.api.reports import router as reports_router
from trailforge.db.models import ConditionReport
from trailforge.db.session import get_db
from trailforge.ingestion.connectors.fixtures import load_corridor_fixtures
from trailforge.ingestion.pipeline import ingest_corridor_dataset
from trailforge.reports.decay import (
    GATE_WARNING_TYPES,
    HALF_LIFE_DAYS,
    STALE_FRESHNESS_THRESHOLD,
    VALID_REPORT_TYPES,
    build_osm_links,
    evaluate_report_decay,
    extract_osm_way_id,
    is_gate_warning_type,
)
from trailforge.reports.photos import (
    MAX_PHOTO_BYTES,
    PhotoNotFoundError,
    PhotoPathTraversalError,
    PhotoValidationError,
    detect_image_format,
    resolve_photo_path,
    save_report_photo,
)
from trailforge.reports.queries import get_reports_for_track, snap_point_to_nearest_segment


def _make_valid_png_bytes() -> bytes:
    """Build a valid 1x1 RGB PNG image in memory (zero external dependencies)."""

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw_scanline = b"\x00\x34\x88\x55"
    idat = zlib.compress(raw_scanline)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


def _make_valid_jpeg_bytes() -> bytes:
    """Build a minimal valid JPEG SOI + APP0 + EOI byte stream."""
    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xd9"
    )


def _make_valid_webp_bytes() -> bytes:
    """Build a minimal RIFF/WEBP header byte stream."""
    return b"RIFF\x1a\x00\x00\x00WEBPVP8 \x0e\x00\x00\x00\x30\x01\x00\x9d\x01\x2a\x01\x00\x01\x00"


def _build_test_app() -> FastAPI:
    """Create FastAPI test app and mount reports router if not yet wired in api/main.py."""
    app = create_app()
    has_reports = any(
        getattr(route, "path", "").startswith("/api/reports") for route in app.routes
    )
    if not has_reports:
        app.include_router(reports_router)
    return app


def _assert_no_email_leaked(payload: Any, emails: list[str]) -> None:
    """Assert neither email addresses nor any 'email' JSON keys appear in serialized payloads."""
    dumped = json.dumps(payload).lower()
    for email in emails:
        assert email.lower() not in dumped
    assert '"email"' not in dumped


# -----------------------------------------------------------------------------
# Pure Unit Tests (Zero DB, Zero Network)
# -----------------------------------------------------------------------------


def test_report_types_half_lives_and_gate_classification() -> None:
    expected = {
        "snow_ice": 5.0,
        "deep_water": 7.0,
        "fallen_tree": 14.0,
        "track_clear": 14.0,
        "bog_hole": 21.0,
        "washout": 30.0,
        "locked_gate": 60.0,
        "private_property": 90.0,
    }
    assert set(VALID_REPORT_TYPES) == set(expected.keys())
    assert len(VALID_REPORT_TYPES) == 8
    assert dict(HALF_LIFE_DAYS) == expected
    assert set(GATE_WARNING_TYPES) == {"locked_gate", "private_property"}
    assert is_gate_warning_type("locked_gate") is True
    assert is_gate_warning_type("private_property") is True
    assert is_gate_warning_type("fallen_tree") is False
    assert is_gate_warning_type("track_clear") is False


def test_deterministic_decay_and_auto_resolution_rules() -> None:
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)

    # Fresh report at age 0
    fresh = evaluate_report_decay("snow_ice", created_at=now, now=now)
    assert fresh.freshness_score == pytest.approx(1.0)
    assert fresh.is_stale is False
    assert fresh.effective_status == "active"

    # At exact half-life (5d for snow_ice) -> 0.5, not stale yet
    half = evaluate_report_decay(
        "snow_ice", created_at=now - timedelta(days=5), now=now
    )
    assert half.freshness_score == pytest.approx(0.5, abs=1e-4)
    assert half.is_stale is False
    assert half.effective_status == "active"

    # At 2 half-lives (10d for snow_ice) -> 0.25 < 0.35 -> stale
    stale = evaluate_report_decay(
        "snow_ice", created_at=now - timedelta(days=10), now=now
    )
    assert stale.freshness_score == pytest.approx(0.25, abs=1e-4)
    assert stale.freshness_score < STALE_FRESHNESS_THRESHOLD
    assert stale.is_stale is True
    assert stale.effective_status == "stale"

    # Locked gate (60d half-life) stays fresh at 10 days
    gate_10d = evaluate_report_decay(
        "locked_gate", created_at=now - timedelta(days=10), now=now
    )
    assert gate_10d.freshness_score > 0.85
    assert gate_10d.is_stale is False
    assert gate_10d.effective_status == "active"

    # Recent confirmation refreshes an old report back to active/fresh
    refreshed = evaluate_report_decay(
        "snow_ice",
        created_at=now - timedelta(days=20),
        last_confirmed_at=now - timedelta(hours=2),
        confirm_count=1,
        now=now,
    )
    assert refreshed.freshness_score > 0.95
    assert refreshed.is_stale is False
    assert refreshed.effective_status == "active"

    # 1 resolve vote decays freshness without resolving
    one_resolve = evaluate_report_decay(
        "fallen_tree", created_at=now, confirm_count=0, resolved_count=1, now=now
    )
    assert one_resolve.freshness_score < 1.0
    assert one_resolve.effective_status == "active"

    # 2 resolve votes vs 0 confirms -> auto-resolves
    two_resolves = evaluate_report_decay(
        "fallen_tree", created_at=now, confirm_count=0, resolved_count=2, now=now
    )
    assert two_resolves.is_resolved is True
    assert two_resolves.effective_status == "resolved"

    # Tie (2 resolves vs 2 confirms) -> stays active
    tied = evaluate_report_decay(
        "fallen_tree", created_at=now, confirm_count=2, resolved_count=2, now=now
    )
    assert tied.is_resolved is False
    assert tied.effective_status == "active"

    # Majority resolves (3 resolves vs 2 confirms) -> auto-resolves
    majority = evaluate_report_decay(
        "fallen_tree", created_at=now, confirm_count=2, resolved_count=3, now=now
    )
    assert majority.is_resolved is True
    assert majority.effective_status == "resolved"

    # Explicit stored status == 'resolved' stays resolved
    stored_res = evaluate_report_decay(
        "washout", created_at=now, status="resolved", now=now
    )
    assert stored_res.is_resolved is True
    assert stored_res.effective_status == "resolved"


def test_osm_links_and_way_id_extraction() -> None:
    links_with_way = build_osm_links(-36.566, 148.12, osm_way_id=1002)
    assert (
        links_with_way["osm_note_url"]
        == "https://www.openstreetmap.org/note/new#map=16/-36.56600/148.12000"
    )
    assert (
        links_with_way["osm_edit_url"]
        == "https://www.openstreetmap.org/edit?way=1002#map=17/-36.56600/148.12000"
    )

    links_str_way = build_osm_links(-36.566, 148.12, osm_way_id="way/1005")
    assert links_str_way["osm_edit_url"] == (
        "https://www.openstreetmap.org/edit?way=1005#map=17/-36.56600/148.12000"
    )

    links_no_way = build_osm_links(-36.566, 148.12)
    assert (
        links_no_way["osm_edit_url"]
        == "https://www.openstreetmap.org/edit#map=17/-36.56600/148.12000"
    )

    with pytest.raises(ValueError):
        build_osm_links(95.0, 148.12)
    with pytest.raises(ValueError):
        build_osm_links(-36.5, 200.0)

    assert (
        extract_osm_way_id(
            [
                {"source": "vic", "source_id": "tr_99"},
                {"source": "osm", "source_id": "way/1002"},
            ]
        )
        == 1002
    )
    assert extract_osm_way_id([{"source": "nsw", "source_id": "FT-7"}]) is None


def test_photo_magic_bytes_size_limit_and_path_confinement(tmp_path: Path) -> None:
    png_bytes = _make_valid_png_bytes()
    jpg_bytes = _make_valid_jpeg_bytes()
    webp_bytes = _make_valid_webp_bytes()

    assert detect_image_format(png_bytes) == ("png", "image/png")
    assert detect_image_format(jpg_bytes) == ("jpg", "image/jpeg")
    assert detect_image_format(webp_bytes) == ("webp", "image/webp")
    assert detect_image_format(b"<html><body>not an image</body></html>") is None
    assert detect_image_format(b"%PDF-1.7 fake pdf") is None

    saved_png = save_report_photo(png_bytes, uploads_dir=tmp_path)
    assert saved_png.filename.endswith(".png")
    assert saved_png.media_type == "image/png"
    assert saved_png.url == f"/api/reports/photos/{saved_png.filename}"
    resolved, mime = resolve_photo_path(saved_png.filename, uploads_dir=tmp_path)
    assert resolved.read_bytes() == png_bytes
    assert mime == "image/png"

    saved_webp = save_report_photo(webp_bytes, uploads_dir=tmp_path)
    assert saved_webp.filename.endswith(".webp")

    # Reject empty, fake non-image, and oversized (> 5 MB)
    with pytest.raises(PhotoValidationError):
        save_report_photo(b"", uploads_dir=tmp_path)
    with pytest.raises(PhotoValidationError):
        save_report_photo(b"<svg onload=alert(1)></svg>", uploads_dir=tmp_path)
    with pytest.raises(PhotoValidationError):
        save_report_photo(
            b"\xff\xd8\xff" + (b"\x00" * MAX_PHOTO_BYTES), uploads_dir=tmp_path
        )

    # Strict path confinement blocks traversal & symlinks
    outside = tmp_path.parent / "outside_secret.png"
    outside.write_bytes(png_bytes)
    link_path = tmp_path / "escape.png"
    link_path.symlink_to(outside)

    for bad_name in [
        "../outside_secret.png",
        "..\\outside_secret.png",
        "subdir/photo.png",
        "/etc/passwd",
        "escape.png",
        "evil.txt",
        "bad\x00.png",
    ]:
        with pytest.raises(PhotoPathTraversalError):
            resolve_photo_path(bad_name, uploads_dir=tmp_path)

    with pytest.raises(PhotoNotFoundError):
        resolve_photo_path("0123456789abcdef0123456789abcdef.png", uploads_dir=tmp_path)


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
                    telemetry_observations, unmapped_track_candidates,
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


def _register_user(
    client: TestClient, *, email: str, display_name: str, password: str = "TrailForge4WD!"
) -> dict[str, str]:
    resp = client.post(
        "/api/auth/register",
        json={"email": email, "display_name": display_name, "password": password},
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.integration
def test_reports_api_end_to_end_photos_voting_decay_snapping_and_privacy(
    postgis_engine: Engine,
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploads_dir = tmp_path / "report_uploads"
    monkeypatch.setenv("TRAILFORGE_UPLOADS_DIR", str(uploads_dir))
    _truncate_all(postgis_engine)

    emails = [
        "alice.highcountry@example.au",
        "bob.tourer@example.au",
        "carol.patrol@example.au",
    ]

    try:
        with Session(postgis_engine) as session:
            ds = load_corridor_fixtures(fixtures_dir, osm_filename="osm_tracks.geojson")
            ingest_corridor_dataset(session, ds, commit=True)

            app = _build_test_app()

            def _override_db() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db] = _override_db

            with TestClient(app) as client:
                tracks = client.get("/api/tracks").json()
                davies = next(t for t in tracks if t["name"] == "Davies Plain Track Lower")
                davies_track_id = davies["track_id"]

                # 1. Anonymous reads succeed; anonymous mutations reject with 401
                assert client.get("/api/reports").status_code == 200
                assert client.get("/api/reports").json() == []
                gj_empty = client.get("/api/reports/geojson")
                assert gj_empty.status_code == 200
                assert gj_empty.json() == {"type": "FeatureCollection", "features": []}

                assert (
                    client.post(
                        "/api/reports",
                        json={
                            "report_type": "fallen_tree",
                            "lon": 148.12,
                            "lat": -36.566,
                            "notes": "Gum tree down",
                        },
                    ).status_code
                    == 401
                )

                # 2. Register 3 users; create Alice's default Touring 4WD vehicle profile
                alice_headers = _register_user(
                    client, email=emails[0], display_name="Alice Alpine"
                )
                bob_headers = _register_user(
                    client, email=emails[1], display_name="Bob HighCountry"
                )
                carol_headers = _register_user(
                    client, email=emails[2], display_name="Carol Patrol"
                )
                # Clear cookie so Bearer headers govern each user deterministically
                client.post("/api/auth/logout")

                veh_resp = client.post(
                    "/api/vehicles",
                    headers=alice_headers,
                    json={
                        "name": "Prado 150 Touring",
                        "drivetrain": "4wd_full_time",
                        "low_range": True,
                        "lift_mm": 50,
                        "clearance_mm": 245,
                        "tire_class": "AT",
                        "has_snorkel": True,
                        "has_winch": True,
                    },
                )
                assert veh_resp.status_code == 201
                assert veh_resp.json()["capability_class"] == "Touring 4WD"

                # 3. Validation failures -> 422 (blank notes, bad coords, bad type)
                assert (
                    client.post(
                        "/api/reports",
                        headers=alice_headers,
                        json={
                            "report_type": "fallen_tree",
                            "lon": 148.12,
                            "lat": -36.566,
                            "notes": "    \t\n ",
                        },
                    ).status_code
                    == 422
                )
                assert (
                    client.post(
                        "/api/reports",
                        headers=alice_headers,
                        json={
                            "report_type": "fallen_tree",
                            "lon": 190.0,
                            "lat": -36.566,
                            "notes": "Out of range lon",
                        },
                    ).status_code
                    == 422
                )
                assert (
                    client.post(
                        "/api/reports",
                        headers=alice_headers,
                        json={
                            "report_type": "invalid_hazard",
                            "lon": 148.12,
                            "lat": -36.566,
                            "notes": "Unknown type",
                        },
                    ).status_code
                    == 422
                )

                # 4. Spatial auto-snap (within 75 m of Davies Plain Track Lower, OSM way 1002)
                snap_check = snap_point_to_nearest_segment(session, 148.1201, -36.5660)
                assert snap_check is not None
                assert snap_check.track_id == davies_track_id
                assert snap_check.osm_way_id == 1002

                r_gate = client.post(
                    "/api/reports",
                    headers=alice_headers,
                    json={
                        "report_type": "locked_gate",
                        "title": "New padlock on upper access gate",
                        "notes": "Heavy chain and combination padlock blocking entry",
                        "lon": 148.1201,
                        "lat": -36.5660,
                    },
                )
                assert r_gate.status_code == 201, r_gate.text
                gate_body = r_gate.json()
                assert gate_body["report_type"] == "locked_gate"
                assert gate_body["is_gate_warning"] is True
                assert gate_body["severity"] == "impassable"
                assert gate_body["title"] == "New padlock on upper access gate"
                assert gate_body["notes"] == "Heavy chain and combination padlock blocking entry"
                assert gate_body["track_id"] == davies_track_id
                assert gate_body["segment_id"] == snap_check.segment_id
                assert gate_body["vehicle_class"] == "Touring 4WD"
                assert gate_body["author_display_name"] == "Alice Alpine"
                assert "way=1002" in gate_body["osm_edit_url"]
                assert (
                    gate_body["osm_note_url"]
                    == "https://www.openstreetmap.org/note/new#map=16/-36.56600/148.12010"
                )
                _assert_no_email_leaked(gate_body, emails)

                # Off-network coordinate (> 75 m away) stays un-snapped cleanly
                r_far = client.post(
                    "/api/reports",
                    headers=alice_headers,
                    json={
                        "report_type": "track_clear",
                        "notes": "Remote firebreak cleared",
                        "lon": 147.0,
                        "lat": -37.0,
                        "vehicle_class": "Stock 4WD",
                    },
                )
                assert r_far.status_code == 201
                far_body = r_far.json()
                assert far_body["track_id"] is None
                assert far_body["segment_id"] is None
                assert far_body["vehicle_class"] == "Stock 4WD"
                assert "?way=" not in far_body["osm_edit_url"]

                # 5. Multipart photo upload: valid PNG & JPEG + reject fake image & oversized
                png_bytes = _make_valid_png_bytes()
                r_png = client.post(
                    "/api/reports",
                    headers=alice_headers,
                    data={
                        "report_type": "private_property",
                        "title": "Private signage posted",
                        "notes": "New private property sign at fence line",
                        "lon": "148.1200",
                        "lat": "-36.5690",
                        "track_id": davies_track_id,
                    },
                    files={"photo": ("gate_sign.png", png_bytes, "image/png")},
                )
                assert r_png.status_code == 201, r_png.text
                priv_body = r_png.json()
                assert priv_body["is_gate_warning"] is True
                assert priv_body["photo_url"] is not None
                assert priv_body["photo_url"].startswith("/api/reports/photos/")
                assert priv_body["photo_url"].endswith(".png")

                # Fetch uploaded PNG photo safely
                r_fetch_png = client.get(priv_body["photo_url"])
                assert r_fetch_png.status_code == 200
                assert r_fetch_png.content == png_bytes
                assert r_fetch_png.headers["content-type"] == "image/png"
                assert r_fetch_png.headers["x-content-type-options"] == "nosniff"

                # Upload valid JPEG photo
                jpg_bytes = _make_valid_jpeg_bytes()
                r_jpg = client.post(
                    "/api/reports",
                    headers=bob_headers,
                    data={
                        "report_type": "bog_hole",
                        "notes": "Deep mud hole after creek bend, winch recommended",
                        "lon": "148.1200",
                        "lat": "-36.5720",
                        "track_id": davies_track_id,
                        "vehicle_class": "Modified 4WD",
                    },
                    files={"photo": ("mud.jpg", jpg_bytes, "image/jpeg")},
                )
                assert r_jpg.status_code == 201, r_jpg.text
                assert r_jpg.json()["photo_url"].endswith(".jpg")
                r_fetch_jpg = client.get(r_jpg.json()["photo_url"])
                assert r_fetch_jpg.status_code == 200
                assert r_fetch_jpg.content == jpg_bytes
                assert r_fetch_jpg.headers["content-type"] == "image/jpeg"

                # Reject fake non-image masquerading as PNG -> 422
                r_fake = client.post(
                    "/api/reports",
                    headers=alice_headers,
                    data={
                        "report_type": "washout",
                        "notes": "Fake photo attempt",
                        "lon": "148.1200",
                        "lat": "-36.5750",
                    },
                    files={
                        "photo": (
                            "fake.png",
                            b"<html><script>alert(1)</script></html>",
                            "image/png",
                        )
                    },
                )
                assert r_fake.status_code == 422

                # Reject oversized photo (> 5 MB) -> 422
                r_big = client.post(
                    "/api/reports",
                    headers=alice_headers,
                    data={
                        "report_type": "washout",
                        "notes": "Oversized photo attempt",
                        "lon": "148.1200",
                        "lat": "-36.5750",
                    },
                    files={
                        "photo": (
                            "huge.jpg",
                            b"\xff\xd8\xff" + (b"\x00" * MAX_PHOTO_BYTES),
                            "image/jpeg",
                        )
                    },
                )
                assert r_big.status_code == 422

                # Reject path traversal on photo read (404 or 422)
                for trav_path in [
                    "/api/reports/photos/..%2F..%2Fetc%2Fpasswd",
                    "/api/reports/photos/sub/nested.png",
                    "/api/reports/photos/not_an_image.txt",
                ]:
                    assert client.get(trav_path).status_code in (404, 422)

                # Nonexistent valid UUID filename -> 404
                assert (
                    client.get(
                        "/api/reports/photos/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png"
                    ).status_code
                    == 404
                )

                # 6. Create remaining 4 report types so all 8 structured types are exercised
                remaining_types = [
                    ("fallen_tree", "Large snow gum blocking track"),
                    ("washout", "Eroded wheel ruts 50 cm deep"),
                    ("deep_water", "Murray headwaters crossing at bonnet height"),
                    ("snow_ice", "Black ice on shaded alpine switchbacks"),
                ]
                created_by_type: dict[str, dict[str, Any]] = {
                    "locked_gate": gate_body,
                    "private_property": priv_body,
                    "bog_hole": r_jpg.json(),
                    "track_clear": far_body,
                }
                for idx, (rtype, note) in enumerate(remaining_types):
                    resp = client.post(
                        "/api/reports",
                        headers=alice_headers,
                        json={
                            "report_type": rtype,
                            "notes": note,
                            "lon": 148.1200,
                            "lat": -36.5620 - (idx * 0.0005),
                            "track_id": davies_track_id,
                        },
                    )
                    assert resp.status_code == 201, resp.text
                    created_by_type[rtype] = resp.json()

                assert set(created_by_type.keys()) == set(VALID_REPORT_TYPES)

                # Verify report_type filter works for every type
                r_filt = client.get("/api/reports", params={"report_type": "snow_ice"})
                assert r_filt.status_code == 200
                assert len(r_filt.json()) == 1
                assert r_filt.json()[0]["report_type"] == "snow_ice"

                # 7. Staleness decay + confirm refresh + automatic resolution via voting
                snow_id = created_by_type["snow_ice"]["id"]
                snow_row = session.get(ConditionReport, uuid.UUID(snow_id))
                assert snow_row is not None
                aged = datetime.now(UTC) - timedelta(days=12)  # > 2x 5d half-life
                snow_row.created_at = aged
                snow_row.observed_at = aged
                session.commit()

                # Before confirmation: stale
                r_stale_check = client.get(f"/api/reports/{snow_id}")
                assert r_stale_check.status_code == 200
                stale_data = r_stale_check.json()
                assert stale_data["is_stale"] is True
                assert stale_data["freshness_score"] < STALE_FRESHNESS_THRESHOLD
                assert stale_data["effective_status"] == "stale"

                # Anonymous vote rejected -> 401
                anon_vote = client.post(
                    f"/api/reports/{snow_id}/vote", json={"vote": "confirm"}
                )
                assert anon_vote.status_code == 401

                # Bob confirms -> refreshes staleness back to active!
                r_v1 = client.post(
                    f"/api/reports/{snow_id}/vote",
                    headers=bob_headers,
                    json={"vote": "confirm"},
                )
                assert r_v1.status_code == 200, r_v1.text
                v1_data = r_v1.json()
                assert v1_data["confirm_count"] == 1
                assert v1_data["resolved_count"] == 0
                assert v1_data["is_stale"] is False
                assert v1_data["freshness_score"] > 0.9
                assert v1_data["effective_status"] == "active"
                assert v1_data["last_confirmed_at"] is not None
                assert v1_data["user_vote"] == "confirm"
                _assert_no_email_leaked(v1_data, emails)

                # Idempotent duplicate confirm by Bob keeps count == 1
                r_v1_dup = client.post(
                    f"/api/reports/{snow_id}/vote",
                    headers=bob_headers,
                    json={"vote": "confirm"},
                )
                assert r_v1_dup.status_code == 200
                assert r_v1_dup.json()["confirm_count"] == 1

                # Bob switches vote to resolved (1 resolve, 0 confirm -> decayed, still active)
                r_v2 = client.post(
                    f"/api/reports/{snow_id}/vote",
                    headers=bob_headers,
                    json={"vote": "resolved"},
                )
                assert r_v2.status_code == 200
                assert r_v2.json()["confirm_count"] == 0
                assert r_v2.json()["resolved_count"] == 1
                assert r_v2.json()["effective_status"] != "resolved"

                # Carol votes resolved (2 resolves > 0 confirms -> automatically resolved!)
                r_v3 = client.post(
                    f"/api/reports/{snow_id}/vote",
                    headers=carol_headers,
                    json={"vote": "resolved"},
                )
                assert r_v3.status_code == 200
                assert r_v3.json()["resolved_count"] == 2
                assert r_v3.json()["effective_status"] == "resolved"

                # Resolved report omitted by default, shown when include_resolved=true
                active_ids = {item["id"] for item in client.get("/api/reports").json()}
                assert snow_id not in active_ids

                all_ids = {
                    item["id"]
                    for item in client.get(
                        "/api/reports", params={"include_resolved": "true"}
                    ).json()
                }
                assert snow_id in all_ids

                # 8. Track Inspector helper surfaces locked_gate & private_property distinctly
                track_bundle = get_reports_for_track(session, davies_track_id)
                gate_ids = {g["id"] for g in track_bundle["community_gate_warnings"]}
                assert gate_ids == {gate_body["id"], priv_body["id"]}
                assert all(
                    g["report_type"] in ("locked_gate", "private_property")
                    and g["osm_edit_url"].startswith("https://www.openstreetmap.org/edit")
                    and g["osm_note_url"].startswith("https://www.openstreetmap.org/note/new")
                    for g in track_bundle["community_gate_warnings"]
                )
                assert snow_id not in {r["id"] for r in track_bundle["condition_reports"]}
                _assert_no_email_leaked(track_bundle, emails)

                # Resolving locked_gate removes it from community_gate_warnings
                assert (
                    client.post(
                        f"/api/reports/{gate_body['id']}/vote",
                        headers=bob_headers,
                        json={"vote": "resolved"},
                    ).status_code
                    == 200
                )
                assert (
                    client.post(
                        f"/api/reports/{gate_body['id']}/vote",
                        headers=carol_headers,
                        json={"vote": "resolved"},
                    ).status_code
                    == 200
                )
                after_resolve = get_reports_for_track(
                    session, davies_track_id, include_resolved=True
                )
                assert [g["id"] for g in after_resolve["community_gate_warnings"]] == [
                    priv_body["id"]
                ]

                # 9. GeoJSON overlay + strict privacy check across all public responses
                gj_resp = client.get("/api/reports/geojson")
                assert gj_resp.status_code == 200
                gj = gj_resp.json()
                assert gj["type"] == "FeatureCollection"
                feat_ids = {f["id"] for f in gj["features"]}
                assert priv_body["id"] in feat_ids
                assert snow_id not in feat_ids
                assert gate_body["id"] not in feat_ids

                priv_feat = next(f for f in gj["features"] if f["id"] == priv_body["id"])
                assert priv_feat["geometry"]["type"] == "Point"
                assert priv_feat["properties"]["is_gate_warning"] is True
                assert priv_feat["properties"]["author_display_name"] == "Alice Alpine"

                _assert_no_email_leaked(gj, emails)
                _assert_no_email_leaked(
                    client.get("/api/reports", params={"include_resolved": "true"}).json(),
                    emails,
                )

            app.dependency_overrides.clear()
    finally:
        _truncate_all(postgis_engine)
