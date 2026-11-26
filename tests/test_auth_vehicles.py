"""Offline unit & PostGIS integration tests for Slice 05 Auth, Vehicles, and Track Suitability."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from trailforge.api.main import create_app
from trailforge.auth.security import (
    create_access_token,
    hash_password,
    normalize_email,
    verify_access_token,
    verify_password,
)
from trailforge.db.models import User
from trailforge.db.session import get_db
from trailforge.ingestion.connectors.fixtures import load_corridor_fixtures
from trailforge.ingestion.pipeline import ingest_corridor_dataset
from trailforge.vehicles.classifier import (
    VehicleSpecs,
    classify_vehicle,
    evaluate_track_suitability,
)

# -----------------------------------------------------------------------------
# Pure Unit Tests (Zero DB, Zero Network)
# -----------------------------------------------------------------------------


def test_password_scrypt_and_signed_token_lifecycle() -> None:
    assert normalize_email("  Alice@Example.ORG ") == "alice@example.org"
    with pytest.raises(ValueError):
        normalize_email("not-an-email")

    pw_hash = hash_password("BushTrack4WD!")
    assert pw_hash.startswith("scrypt$")
    assert verify_password("BushTrack4WD!", pw_hash) is True
    assert verify_password("WrongPass123", pw_hash) is False

    token = create_access_token("11111111-1111-1111-1111-111111111111", "alice@example.org")
    payload = verify_access_token(token)
    assert payload is not None
    assert payload["sub"] == "11111111-1111-1111-1111-111111111111"
    assert payload["email"] == "alice@example.org"

    # Tampered signature rejected
    bad_token = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    assert verify_access_token(bad_token) is None

    # Expired token rejected
    expired = create_access_token(
        "11111111-1111-1111-1111-111111111111",
        "alice@example.org",
        ttl_seconds=-10,
    )
    assert verify_access_token(expired) is None


def test_deterministic_vehicle_capability_classification() -> None:
    # 1. AWD SUV (Subaru Forester / RAV4)
    awd = classify_vehicle(
        VehicleSpecs(
            drivetrain="awd",
            low_range=False,
            lift_mm=25,
            clearance_mm=220,
            tire_class="AT",
        )
    )
    assert awd.capability_class == "AWD"
    assert awd.classification_reasons

    # 2. Stock 4WD (factory Hilux / Prado on HT or AT with no touring upgrades)
    stock = classify_vehicle(
        VehicleSpecs(
            drivetrain="4wd_part_time",
            low_range=True,
            lift_mm=0,
            clearance_mm=210,
            tire_class="HT",
        )
    )
    assert stock.capability_class == "Stock 4WD"

    # 3. Touring 4WD (LandCruiser 200/Prado with 50 mm lift, AT tires, snorkel/winch)
    touring = classify_vehicle(
        VehicleSpecs(
            drivetrain="4wd_full_time",
            low_range=True,
            lift_mm=50,
            clearance_mm=245,
            tire_class="AT",
            has_snorkel=True,
            has_winch=True,
        )
    )
    assert touring.capability_class == "Touring 4WD"

    # 4. Modified 4WD (79 Series / Patrol with 50 mm+ lift, MT tires, locker/winch)
    modified = classify_vehicle(
        VehicleSpecs(
            drivetrain="4wd_part_time",
            low_range=True,
            lift_mm=75,
            clearance_mm=275,
            tire_class="MT",
            rear_locker=True,
            front_locker=True,
            has_winch=True,
            has_snorkel=True,
        )
    )
    assert modified.capability_class == "Modified 4WD"


def test_track_suitability_guidance_and_warnings() -> None:
    awd_extreme = evaluate_track_suitability(
        grade="Extreme",
        capability_class="AWD",
        max_slope_deg=21.9,
        waterway_crossing_count=1,
        has_perennial_crossing=True,
        is_4wd_only=True,
        closure_status="seasonal",
    )
    assert awd_extreme.rating == "High Risk for your rig"
    assert len(awd_extreme.warnings) >= 3

    touring_extreme = evaluate_track_suitability(
        grade="Extreme",
        capability_class="Touring 4WD",
        vehicle_name="Prado 150 Touring",
        max_slope_deg=21.9,
        waterway_crossing_count=1,
        has_perennial_crossing=True,
        has_snorkel=False,
    )
    assert touring_extreme.rating == "Challenging"
    assert touring_extreme.vehicle_name == "Prado 150 Touring"
    assert any("snorkel" in w.lower() for w in touring_extreme.warnings)

    mod_extreme = evaluate_track_suitability(
        grade="Extreme",
        capability_class="Modified 4WD",
        max_slope_deg=21.9,
        waterway_crossing_count=1,
        has_perennial_crossing=True,
        has_snorkel=True,
    )
    assert mod_extreme.rating == "Suited"


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
                    user_favorites, user_pins, vehicle_profiles, invite_codes,
                    usage_metrics_hourly, users,
                    pois, track_segments, tracks, capad_parks,
                    source_tracks_osm, source_tracks_vic, source_tracks_nsw,
                    source_waterways, source_pois, source_capad_parks
                RESTART IDENTITY CASCADE
                """
            )
        )


@pytest.mark.integration
def test_auth_vehicle_profiles_and_track_suitability_end_to_end(
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
                # 1. Anonymous browsing always works without sign-in
                r_me = client.get("/api/auth/me")
                assert r_me.status_code == 200
                assert r_me.json() == {"authenticated": False, "user": None}

                r_cls = client.get("/api/vehicles/classes")
                assert r_cls.status_code == 200
                assert [c["id"] for c in r_cls.json()] == [
                    "AWD",
                    "Stock 4WD",
                    "Touring 4WD",
                    "Modified 4WD",
                ]

                # Anonymous quick rig preview via ?vehicle_class=AWD
                r_trks = client.get("/api/tracks", params={"vehicle_class": "AWD"})
                assert r_trks.status_code == 200
                davies = next(t for t in r_trks.json() if t["name"] == "Davies Plain Track Lower")
                assert davies["suitability"]["vehicle_class"] == "AWD"
                assert davies["suitability"]["rating"] == "High Risk for your rig"

                # Protected endpoints reject anonymous calls with 401
                assert client.get("/api/vehicles").status_code == 401

                # 2. Register, duplicate conflict (409), wrong password (401), login & tokens
                assert (
                    client.post(
                        "/api/auth/register",
                        json={
                            "email": "blank@highcountry.au",
                            "display_name": "   ",
                            "password": "SuperSecure4WDPassword",
                        },
                    ).status_code
                    == 422
                )
                r_reg = client.post(
                    "/api/auth/register",
                    json={
                        "email": "Driver@HighCountry.AU",
                        "display_name": "High Country Driver",
                        "password": "SuperSecure4WDPassword",
                    },
                )
                assert r_reg.status_code == 201
                reg_body = r_reg.json()
                assert reg_body["user"]["email"] == "driver@highcountry.au"
                token = reg_body["access_token"]
                assert token

                # Duplicate email rejected cleanly
                assert (
                    client.post(
                        "/api/auth/register",
                        json={
                            "email": "driver@highcountry.au",
                            "display_name": "Dup",
                            "password": "AnotherPassword1",
                        },
                    ).status_code
                    == 409
                )

                # Cookie auth works immediately on /api/auth/me
                r_me_auth = client.get("/api/auth/me")
                assert r_me_auth.status_code == 200
                assert r_me_auth.json()["authenticated"] is True
                assert r_me_auth.json()["user"]["email"] == "driver@highcountry.au"

                # Logout clears cookie; Bearer token still authenticates statelessly
                assert client.post("/api/auth/logout").status_code == 200
                assert client.get("/api/auth/me").json()["authenticated"] is False

                headers = {"Authorization": f"Bearer {token}"}
                assert client.get("/api/auth/me", headers=headers).json()["authenticated"] is True

                # Bad password rejected with 401; valid login restores cookie
                assert (
                    client.post(
                        "/api/auth/login",
                        json={"email": "driver@highcountry.au", "password": "wrong"},
                    ).status_code
                    == 401
                )
                assert (
                    client.post(
                        "/api/auth/login",
                        json={
                            "email": "driver@highcountry.au",
                            "password": "SuperSecure4WDPassword",
                        },
                    ).status_code
                    == 200
                )

                # 3. Create AWD profile (becomes active default automatically)
                assert (
                    client.post(
                        "/api/vehicles",
                        json={"name": "   \t ", "drivetrain": "awd", "low_range": False},
                    ).status_code
                    == 422
                )
                r_v1 = client.post(
                    "/api/vehicles",
                    json={
                        "name": "Subaru Outback Wilderness",
                        "drivetrain": "awd",
                        "low_range": False,
                        "lift_mm": 0,
                        "clearance_mm": 213,
                        "tire_class": "AT",
                    },
                )
                assert r_v1.status_code == 201
                v1 = r_v1.json()
                assert v1["capability_class"] == "AWD"
                assert v1["is_active"] is True
                assert (
                    client.patch(f"/api/vehicles/{v1['id']}", json={"name": "   "}).status_code
                    == 422
                )

                # Track Inspector automatically reflects active AWD rig
                det_awd = client.get(f"/api/tracks/{davies['track_id']}").json()
                assert det_awd["suitability"]["vehicle_name"] == "Subaru Outback Wilderness"
                assert det_awd["suitability"]["rating"] == "High Risk for your rig"

                # 4. Create Modified 4WD rig & switch active rig -> suitability flips to Suited!
                r_v2 = client.post(
                    "/api/vehicles",
                    json={
                        "name": "2021 LandCruiser 79 Built",
                        "drivetrain": "4wd_part_time",
                        "low_range": True,
                        "lift_mm": 75,
                        "clearance_mm": 270,
                        "tire_class": "MT",
                        "front_locker": True,
                        "rear_locker": True,
                        "has_winch": True,
                        "has_snorkel": True,
                        "is_active": False,
                    },
                )
                assert r_v2.status_code == 201
                v2 = r_v2.json()
                assert v2["capability_class"] == "Modified 4WD"
                assert v2["is_active"] is False

                r_act = client.post(f"/api/vehicles/{v2['id']}/activate")
                assert r_act.status_code == 200
                assert r_act.json()["is_active"] is True

                det_mod = client.get(f"/api/tracks/{davies['track_id']}").json()
                assert det_mod["suitability"]["vehicle_class"] == "Modified 4WD"
                assert det_mod["suitability"]["vehicle_name"] == "2021 LandCruiser 79 Built"
                assert det_mod["suitability"]["rating"] == "Suited"

                # 5. PATCH vehicle downgrades to Stock 4WD deterministically
                r_patch = client.patch(
                    f"/api/vehicles/{v2['id']}",
                    json={
                        "lift_mm": 0,
                        "clearance_mm": 210,
                        "tire_class": "HT",
                        "front_locker": False,
                        "rear_locker": False,
                        "has_winch": False,
                        "has_snorkel": False,
                    },
                )
                assert r_patch.status_code == 200
                assert r_patch.json()["capability_class"] == "Stock 4WD"

                # 6. Deleting active vehicle promotes remaining vehicle cleanly
                assert client.delete(f"/api/vehicles/{v2['id']}").status_code == 200
                remaining = client.get("/api/vehicles").json()
                assert len(remaining) == 1
                assert remaining[0]["id"] == v1["id"]
                assert remaining[0]["is_active"] is True

                # 7. Slice 04 + 05 cross-integration: sync offline closures (admin) & verify tracks
                sync_admin = User(
                    email="sync.admin@highcountry.au",
                    display_name="Sync Admin",
                    password_hash=hash_password("AdminPassword123!"),
                    role="admin",
                    is_active=True,
                )
                session.add(sync_admin)
                session.commit()
                session.refresh(sync_admin)
                admin_headers = {
                    "Authorization": (
                        f"Bearer {create_access_token(str(sync_admin.id), sync_admin.email)}"
                    )
                }
                r_sync = client.post(
                    "/api/closures/sync",
                    headers=admin_headers,
                    json={
                        "use_fixtures": True,
                        "fixtures_path": str(fixtures_dir / "closures_sample.json"),
                    },
                )
                assert r_sync.status_code == 200
                r_trks_closed = client.get("/api/tracks")
                assert r_trks_closed.status_code == 200
                trks_by_name = {t["name"]: t for t in r_trks_closed.json()}
                assert trks_by_name["Davies Plain Track Lower"]["closure_status"] == "seasonal"
                assert len(trks_by_name["Davies Plain Track Lower"]["active_closures"]) >= 1
                assert trks_by_name["Limestone Creek Bridged Link"]["closure_status"] == "open"
                assert trks_by_name["Limestone Creek Bridged Link"]["active_closures"] == []

                det_closed = client.get(f"/api/tracks/{davies['track_id']}").json()
                assert det_closed["closure_status"] == "seasonal"
                assert any(
                    "seasonal" in w.lower() for w in det_closed["suitability"]["warnings"]
                )
            app.dependency_overrides.clear()
    finally:
        _truncate_all(postgis_engine)
