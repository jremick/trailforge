"""Offline unit & PostGIS integration tests for v1.1.0 Public Access Control & Admin API."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

import trailforge.api.admin as admin_module
from trailforge.api.admin import router as admin_router
from trailforge.api.main import create_app
from trailforge.api.packs import get_packs_dir_dep
from trailforge.auth.cli import build_parser
from trailforge.auth.cli import main as auth_cli_main
from trailforge.auth.invites import classify_invite_status, generate_invite_code
from trailforge.config import get_settings
from trailforge.db.models import InviteCode, UsageMetricHourly, User
from trailforge.db.session import get_db
from trailforge.ingestion.connectors.fixtures import load_corridor_fixtures
from trailforge.ingestion.pipeline import ingest_corridor_dataset

_INVITE_CODE_RE = re.compile(r"^TF-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")


def _build_app() -> FastAPI:
    app = create_app()
    if not any(getattr(r, "path", "").startswith("/api/admin") for r in app.routes):
        app.include_router(admin_router)
    return app


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
                    planned_routes, user_favorites, user_pins, vehicle_profiles,
                    invite_codes, usage_metrics_hourly, users,
                    pois, track_segments, tracks, capad_parks,
                    source_tracks_osm, source_tracks_vic, source_tracks_nsw,
                    source_tracks_au_state,
                    source_waterways, source_pois, source_capad_parks
                RESTART IDENTITY CASCADE
                """
            )
        )


# -----------------------------------------------------------------------------
# Pure Offline Unit Tests (Zero DB, Zero Network)
# -----------------------------------------------------------------------------


def test_invite_code_format_and_status_classification() -> None:
    generated = {generate_invite_code() for _ in range(50)}
    assert len(generated) == 50
    for code in generated:
        assert _INVITE_CODE_RE.match(code), f"Unexpected invite code format: {code}"

    now = datetime(2026, 11, 26, 8, 0, tzinfo=UTC)
    active = InviteCode(
        code="TF-ABCD-2345",
        role="member",
        max_uses=3,
        use_count=1,
        expires_at=now + timedelta(days=7),
        revoked_at=None,
    )
    exhausted = InviteCode(
        code="TF-ABCD-2346",
        role="member",
        max_uses=2,
        use_count=2,
        expires_at=now + timedelta(days=7),
        revoked_at=None,
    )
    expired = InviteCode(
        code="TF-ABCD-2347",
        role="member",
        max_uses=5,
        use_count=0,
        expires_at=now - timedelta(minutes=1),
        revoked_at=None,
    )
    revoked = InviteCode(
        code="TF-ABCD-2348",
        role="member",
        max_uses=5,
        use_count=0,
        expires_at=now - timedelta(minutes=1),
        revoked_at=now - timedelta(hours=1),
    )

    assert classify_invite_status(active, now=now) == "active"
    assert classify_invite_status(exhausted, now=now) == "exhausted"
    assert classify_invite_status(expired, now=now) == "expired"
    assert classify_invite_status(revoked, now=now) == "revoked"


def test_auth_config_endpoint_reflects_mode_and_demo_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_app()
    with TestClient(app) as client:
        for mode in ("open", "invite_only", "closed"):
            monkeypatch.setenv("TRAILFORGE_REGISTRATION_MODE", mode)
            monkeypatch.setenv("TRAILFORGE_DEMO_BANNER", f"Public preview ({mode})")
            get_settings.cache_clear()
            resp = client.get("/api/auth/config")
            assert resp.status_code == 200
            assert resp.json() == {
                "registration_mode": mode,
                "demo_banner": f"Public preview ({mode})",
                "allow_anonymous_browsing": True,
            }
    get_settings.cache_clear()


def test_packs_public_base_url_emits_r2_urls_and_default_relative_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trailforge.api.packs import _build_region_summary
    from trailforge.db.models import OfflinePack
    from trailforge.packs.catalog import get_pack_region

    region = get_pack_region("australian-alps")

    monkeypatch.delenv("PACKS_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("TRAILFORGE_PACKS_PUBLIC_BASE_URL", raising=False)
    get_settings.cache_clear()
    local_summary = _build_region_summary(region, tmp_path, None)
    assert local_summary["manifest_url"] == "/api/packs/australian-alps/manifest.json"
    assert local_summary["snapshot_url"] == "/api/packs/australian-alps/snapshot.json"
    assert local_summary["bundle_url"] == "/api/packs/australian-alps/bundle.tar.gz"
    assert local_summary["vector_pmtiles_url"] == "/api/packs/australian-alps/tiles/vector.pmtiles"
    assert (
        local_summary["terrain_pmtiles_url"] == "/api/packs/australian-alps/tiles/terrain.pmtiles"
    )

    monkeypatch.setenv("PACKS_PUBLIC_BASE_URL", "https://packs.example.com/")
    get_settings.cache_clear()
    assert _build_region_summary(region, tmp_path, None)["status"] == "not_built"
    published = OfflinePack(
        status="ready", version="test", community_telemetry_policy="static_v2",
    )
    r2_summary = _build_region_summary(region, tmp_path, published)
    assert r2_summary["status"] == "ready"
    prefix = "https://packs.example.com/australian-alps/static_v2"
    assert r2_summary["manifest_url"] == f"{prefix}/manifest.json"
    assert r2_summary["snapshot_url"] == f"{prefix}/snapshot.json"
    assert r2_summary["bundle_url"] == f"{prefix}/bundle.tar.gz"
    assert r2_summary["vector_pmtiles_url"] == f"{prefix}/vector.pmtiles"
    assert r2_summary["terrain_pmtiles_url"] == f"{prefix}/terrain.pmtiles"

    # Even when a local static_v2 manifest exists on disk (e.g. Railway /data/packs), R2 URLs win
    alps_dir = tmp_path / "australian-alps"
    alps_dir.mkdir(parents=True, exist_ok=True)
    (alps_dir / "manifest.json").write_text(
        json.dumps(
            {
                "region_id": "australian-alps",
                "name": region.name,
                "description": region.description,
                "region_type": region.region_type,
                "version": "2026.11.27",
                "generated_at": "2026-11-27T00:00:00Z",
                "community_telemetry_policy": "static_v2",
                "bbox": list(region.bbox),
                "bounds": list(region.bbox),
            }
        ),
        encoding="utf-8",
    )
    hybrid_summary = _build_region_summary(region, tmp_path, published)
    assert hybrid_summary["status"] == "ready"
    assert hybrid_summary["manifest_url"] == f"{prefix}/manifest.json"
    assert hybrid_summary["vector_pmtiles_url"] == f"{prefix}/vector.pmtiles"

    # Stale legacy local manifest falls back to the published static_v2 R2 record
    (alps_dir / "manifest.json").write_text(
        json.dumps(
            {
                "region_id": "australian-alps",
                "name": region.name,
                "description": region.description,
                "region_type": region.region_type,
                "version": "2026.11.20",
                "generated_at": "2026-11-20T00:00:00Z",
                "community_telemetry_policy": "static_v1",
                "vector_available": True,
                "bbox": list(region.bbox),
                "bounds": list(region.bbox),
            }
        ),
        encoding="utf-8",
    )
    stale_local_summary = _build_region_summary(region, tmp_path, published)
    assert stale_local_summary["status"] == "ready"
    assert stale_local_summary["vector_pmtiles_url"] == f"{prefix}/vector.pmtiles"
    published.community_telemetry_policy = "static_v1"
    assert _build_region_summary(region, tmp_path, published)["status"] == "not_built"
    get_settings.cache_clear()


def test_auth_cli_help_zero_runtime_warnings() -> None:
    parser = build_parser()
    parsed = parser.parse_args(
        ["create-invite", "--label", "Alps Crew", "--max-uses", "5", "--expires-days", "30"]
    )
    assert parsed.command == "create-invite"
    assert parsed.max_uses == 5
    assert parsed.expires_days == 30

    proc = subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-m", "trailforge.auth.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"CLI failed or emitted RuntimeWarning: {proc.stderr}"
    for subcmd in ("create-admin", "promote", "create-invite", "list-invites"):
        assert subcmd in proc.stdout


# -----------------------------------------------------------------------------
# PostGIS Integration Tests (100% Offline)
# -----------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["open", "invite_only"])
def test_registration_roles_ignore_legacy_admin_email(
    postgis_engine: Engine, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _truncate_all(postgis_engine)
    legacy_email = "unverified@example.test"
    monkeypatch.setenv("TRAILFORGE_ADMIN_EMAIL", legacy_email)
    monkeypatch.setenv("TRAILFORGE_REGISTRATION_MODE", mode)
    monkeypatch.setenv("AUTH_SECRET_KEY", "a" * 64)
    monkeypatch.setenv("TRAILFORGE_PRIVACY_SECRET", "p" * 64)
    monkeypatch.setenv("TRAILFORGE_ENABLE_MCP", "false")
    get_settings.cache_clear()

    try:
        with Session(postgis_engine) as session:
            member_invite = InviteCode(code="TF-MEMB-2345", role="member", max_uses=1)
            admin_invite = InviteCode(code="TF-ADMN-2345", role="admin", max_uses=1)
            session.add_all([member_invite, admin_invite])
            session.commit()

            app = create_app()

            def _override_db() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db] = _override_db
            with TestClient(app) as client:
                registration = {
                    "email": legacy_email,
                    "display_name": "Trail Driver",
                    "password": "TrailDriverPassword",
                }
                if mode == "invite_only":
                    assert client.post("/api/auth/register", json=registration).status_code == 403
                    registration["invite_code"] = member_invite.code
                member = client.post("/api/auth/register", json=registration)
                assert member.status_code == 201, member.text
                assert member.json()["user"]["role"] == "member"
                assert client.get("/api/admin/invites").status_code == 403

                client.cookies.clear()
                admin = client.post(
                    "/api/auth/register",
                    json={
                        **registration,
                        "email": "operator@example.test",
                        "invite_code": admin_invite.code,
                    },
                )
                assert admin.status_code == 201, admin.text
                assert admin.json()["user"]["role"] == "admin"
                assert client.get("/api/admin/invites").status_code == 200
    finally:
        _truncate_all(postgis_engine)
        get_settings.cache_clear()


@pytest.mark.integration
def test_registration_modes_invites_suspension_last_admin_and_admin_gating(
    postgis_engine: Engine,
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _truncate_all(postgis_engine)
    packs_dir = tmp_path / "packs"
    tiles_dir = tmp_path / "tiles"
    uploads_dir = tmp_path / "uploads"
    packs_dir.mkdir()
    tiles_dir.mkdir()
    uploads_dir.mkdir()
    (tiles_dir / "sample.pmtiles").write_bytes(b"0" * 1024)
    (uploads_dir / "photo.png").write_bytes(b"1" * 2048)

    monkeypatch.setenv("TILES_DIR", str(tiles_dir))
    monkeypatch.setenv("PACKS_DIR", str(packs_dir))
    monkeypatch.setenv("TRAILFORGE_UPLOADS_DIR", str(uploads_dir))
    monkeypatch.setenv("TRAILFORGE_UPLOADS_MAX_TOTAL_MB", "10")
    monkeypatch.setenv("TRAILFORGE_ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("TRAILFORGE_REGISTRATION_MODE", "invite_only")
    monkeypatch.setenv("TRAILFORGE_DEMO_BANNER", "Public preview — invite required to sign up")
    monkeypatch.setenv("AUTH_SECRET_KEY", "test-access-control-auth-secret-32chars-minimum-01")
    monkeypatch.setenv(
        "TRAILFORGE_PRIVACY_SECRET", "test-access-control-privacy-secret-32chars-min-01"
    )
    monkeypatch.setenv("TRAILFORGE_PUBLIC_BASE_URL", "https://trails.example.com")
    get_settings.cache_clear()

    try:
        with Session(postgis_engine) as session:
            ds = load_corridor_fixtures(fixtures_dir, osm_filename="osm_tracks.geojson")
            ingest_corridor_dataset(session, ds, commit=True)

            # Seed hourly usage metrics rows (one within 24h, one 3 days old)
            now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
            session.add_all(
                [
                    UsageMetricHourly(
                        bucket_hour=now - timedelta(hours=2),
                        route_group="tracks",
                        request_count=40,
                        error_count=1,
                        bytes_out=120_000,
                        rate_limited_count=0,
                    ),
                    UsageMetricHourly(
                        bucket_hour=now - timedelta(days=3),
                        route_group="tiles",
                        request_count=100,
                        error_count=2,
                        bytes_out=500_000,
                        rate_limited_count=3,
                    ),
                ]
            )
            session.commit()

            # Mock live in-memory usage snapshot to prove stats merges DB + live counters
            monkeypatch.setattr(
                admin_module,
                "_get_live_usage_snapshot_safe",
                lambda: {
                    "pending_buckets": [
                        {
                            "bucket_hour": now.isoformat(),
                            "route_group": "tracks",
                            "request_count": 5,
                            "error_count": 0,
                            "bytes_out": 5_000,
                            "rate_limited_count": 1,
                        }
                    ]
                },
            )

            app = _build_app()

            def _override_db() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db] = _override_db
            app.dependency_overrides[get_packs_dir_dep] = lambda: packs_dir

            with TestClient(app) as client:
                # 1. Anonymous user blocked (401) on compute POSTs & /api/admin/*
                for method, path, body in (
                    ("POST", "/api/closures/sync", {"use_fixtures": True}),
                    ("POST", "/api/grading/recompute", {"rain_72h_mm": 10.0}),
                    (
                        "POST",
                        "/api/packs/australian-alps/build",
                        {"skip_tiles": True},
                    ),
                    ("GET", "/api/admin/stats", None),
                    ("GET", "/api/admin/users", None),
                    ("GET", "/api/admin/invites", None),
                    ("POST", "/api/admin/invites", {"label": "Nope"}),
                    ("DELETE", f"/api/admin/invites/{uuid.uuid4()}", None),
                    ("PATCH", f"/api/admin/users/{uuid.uuid4()}", {"is_active": False}),
                ):
                    r = client.request(method, path, json=body)
                    assert r.status_code == 401, (
                        f"Expected 401 for anon {method} {path}, got {r.status_code}"
                    )

                # Public GET browsing still works anonymously
                assert client.get("/api/tracks").status_code == 200
                assert client.get("/api/packs").status_code == 200
                assert client.get("/api/closures").status_code == 200
                assert client.get("/api/auth/config").json()["registration_mode"] == "invite_only"

                # 2. Invite-only blocks non-bootstrap user without invite code -> 403
                r_no_inv = client.post(
                    "/api/auth/register",
                    json={
                        "email": "stranger@example.com",
                        "display_name": "Stranger",
                        "password": "StrongPassword123!",
                    },
                )
                assert r_no_inv.status_code == 403
                assert "invite" in r_no_inv.json()["detail"].lower()

                # 3. SEC-M3: TRAILFORGE_ADMIN_EMAIL cannot bypass invite_only without a valid invite
                r_bypass = client.post(
                    "/api/auth/register",
                    json={
                        "email": "OWNER@Example.COM",
                        "display_name": "Site Owner",
                        "password": "OwnerPassword123!",
                    },
                )
                assert r_bypass.status_code == 403

                # An operator-issued admin invite can provision the first administrator.
                boot_inv = InviteCode(
                    code="TF-BOOT-2345",
                    label="Initial Admin Bootstrap",
                    role="admin",
                    max_uses=1,
                    use_count=0,
                )
                session.add(boot_inv)
                session.commit()
                r_boot = client.post(
                    "/api/auth/register",
                    json={
                        "email": "OWNER@Example.COM",
                        "display_name": "Site Owner",
                        "password": "OwnerPassword123!",
                        "invite_code": "TF-BOOT-2345",
                    },
                )
                assert r_boot.status_code == 201, r_boot.text
                boot_data = r_boot.json()
                assert boot_data["user"]["role"] == "admin"
                assert boot_data["user"]["is_active"] is True
                admin_id = boot_data["user"]["id"]
                admin_headers = {"Authorization": f"Bearer {boot_data['access_token']}"}
                client.cookies.clear()

                # 4. Admin creates a 2-use friend invite, an expired invite, and a revocable invite
                r_inv1 = client.post(
                    "/api/admin/invites",
                    headers=admin_headers,
                    json={"label": "High Country Mates", "max_uses": 2, "expires_in_days": 14},
                )
                assert r_inv1.status_code == 201, r_inv1.text
                inv1 = r_inv1.json()
                assert _INVITE_CODE_RE.match(inv1["code"])
                assert inv1["status"] == "active"
                assert inv1["max_uses"] == 2
                assert inv1["use_count"] == 0

                r_inv_rev = client.post(
                    "/api/admin/invites",
                    headers=admin_headers,
                    json={"label": "Revoke Me", "max_uses": 5, "expires_in_days": 7},
                )
                assert r_inv_rev.status_code == 201
                inv_rev = r_inv_rev.json()
                r_del = client.delete(
                    f"/api/admin/invites/{inv_rev['id']}",
                    headers=admin_headers,
                )
                assert r_del.status_code == 200
                assert r_del.json()["status"] == "revoked"
                assert r_del.json()["revoked_at"] is not None

                # Insert an expired invite directly for expiry check
                expired_inv = InviteCode(
                    code="TF-EXPD-9999",
                    label="Old Trip",
                    role="member",
                    max_uses=5,
                    use_count=0,
                    expires_at=datetime.now(UTC) - timedelta(hours=2),
                )
                session.add(expired_inv)
                session.commit()

                # Invalid / revoked / expired invite codes all rejected with 403
                for bad_code, expected_word in (
                    ("TF-NOPE-0000", "invalid"),
                    (inv_rev["code"], "revoked"),
                    ("TF-EXPD-9999", "expired"),
                ):
                    r_bad = client.post(
                        "/api/auth/register",
                        json={
                            "email": f"bad-{uuid.uuid4().hex[:6]}@example.com",
                            "display_name": "Bad Invite",
                            "password": "StrongPassword123!",
                            "invite_code": bad_code,
                        },
                    )
                    assert r_bad.status_code == 403
                    assert expected_word in r_bad.json()["detail"].lower()

                # 5. Redeem valid 2-use invite twice (including lowercase + whitespace), 3rd fails
                r_m1 = client.post(
                    "/api/auth/register",
                    json={
                        "email": "mate1@example.com",
                        "display_name": "Mate One",
                        "password": "MatePassword123!",
                        "invite_code": f"  {inv1['code'].lower()} ",
                    },
                )
                assert r_m1.status_code == 201, r_m1.text
                m1_data = r_m1.json()
                assert m1_data["user"]["role"] == "member"
                assert m1_data["user"]["is_active"] is True
                m1_id = m1_data["user"]["id"]
                m1_token = m1_data["access_token"]
                m1_headers = {"Authorization": f"Bearer {m1_token}"}
                m1_cookie = dict(r_m1.cookies)
                client.cookies.clear()

                r_m2 = client.post(
                    "/api/auth/register",
                    json={
                        "email": "mate2@example.com",
                        "display_name": "Mate Two",
                        "password": "MatePassword123!",
                        "invite_code": inv1["code"],
                    },
                )
                assert r_m2.status_code == 201
                m2_id = r_m2.json()["user"]["id"]
                client.cookies.clear()

                # 3rd use rejected (exhausted)
                r_m3 = client.post(
                    "/api/auth/register",
                    json={
                        "email": "mate3@example.com",
                        "display_name": "Mate Three",
                        "password": "MatePassword123!",
                        "invite_code": inv1["code"],
                    },
                )
                assert r_m3.status_code == 403
                assert "maximum uses" in r_m3.json()["detail"].lower()

                # Verify invite list reports exhausted, revoked, expired cleanly
                inv_list = client.get("/api/admin/invites", headers=admin_headers).json()
                by_code = {i["code"]: i for i in inv_list["invites"]}
                assert by_code[inv1["code"]]["status"] == "exhausted"
                assert by_code[inv1["code"]]["use_count"] == 2
                assert by_code[inv_rev["code"]]["status"] == "revoked"
                assert by_code["TF-EXPD-9999"]["status"] == "expired"

                # 6. Regular member blocked (403) on all compute POSTs & /api/admin/*
                for method, path, body in (
                    ("POST", "/api/closures/sync", {"use_fixtures": True}),
                    ("POST", "/api/grading/recompute", {"rain_72h_mm": 10.0}),
                    ("POST", "/api/packs/australian-alps/build", {"skip_tiles": True}),
                    ("GET", "/api/admin/stats", None),
                    ("GET", "/api/admin/users", None),
                    ("GET", "/api/admin/invites", None),
                    ("POST", "/api/admin/invites", {"label": "Member Try"}),
                    ("DELETE", f"/api/admin/invites/{inv1['id']}", None),
                    ("PATCH", f"/api/admin/users/{m2_id}", {"role": "admin"}),
                ):
                    r = client.request(method, path, headers=m1_headers, json=body)
                    assert r.status_code == 403, (
                        f"Expected 403 for member {method} {path}, got {r.status_code}"
                    )

                # 7. Admin succeeds (200) on compute endpoints & /api/admin/stats
                r_sync = client.post(
                    "/api/closures/sync",
                    headers=admin_headers,
                    json={
                        "use_fixtures": True,
                        "fixtures_path": str(fixtures_dir / "closures_sample.json"),
                    },
                )
                assert r_sync.status_code == 200, r_sync.text

                r_grade = client.post(
                    "/api/grading/recompute",
                    headers=admin_headers,
                    json={"rain_72h_mm": 12.0, "rain_24h_mm": 4.0},
                )
                assert r_grade.status_code == 200, r_grade.text

                r_pack = client.post(
                    "/api/packs/australian-alps/build",
                    headers=admin_headers,
                    json={
                        "dem_path": str(fixtures_dir / "dem_alps_corridor.tif"),
                        "skip_tiles": True,
                    },
                )
                assert r_pack.status_code == 200, r_pack.text

                # 8. Last-admin guard: cannot suspend or demote the sole active admin -> 400
                r_self_susp = client.patch(
                    f"/api/admin/users/{admin_id}",
                    headers=admin_headers,
                    json={"is_active": False},
                )
                assert r_self_susp.status_code == 400
                r_self_demote = client.patch(
                    f"/api/admin/users/{admin_id}",
                    headers=admin_headers,
                    json={"role": "member"},
                )
                assert r_self_demote.status_code == 400

                # 9. Suspend member -> token_version bump immediately revokes existing tokens (401)
                #    and login returns 403 while suspended (M-4)
                assert client.get("/api/vehicles", headers=m1_headers).status_code == 200
                r_susp = client.patch(
                    f"/api/admin/users/{m1_id}",
                    headers=admin_headers,
                    json={"is_active": False},
                )
                assert r_susp.status_code == 200
                assert r_susp.json()["is_active"] is False
                assert r_susp.json()["disabled_at"] is not None

                # Existing Bearer token immediately revoked with 401 (token_version incremented)
                r_veh_susp = client.get("/api/vehicles", headers=m1_headers)
                assert r_veh_susp.status_code == 401

                r_me_susp = client.get("/api/auth/me", headers=m1_headers)
                assert r_me_susp.status_code == 200
                assert r_me_susp.json()["authenticated"] is False

                # Existing HttpOnly cookie also immediately revoked with 401
                r_cookie_susp = client.get("/api/vehicles", cookies=m1_cookie)
                assert r_cookie_susp.status_code == 401

                # Suspended user login rejected with 403 (valid pw) / 401 (bad pw)
                r_login_susp = client.post(
                    "/api/auth/login",
                    json={"email": "mate1@example.com", "password": "MatePassword123!"},
                )
                assert r_login_susp.status_code == 403
                assert (
                    client.post(
                        "/api/auth/login",
                        json={"email": "mate1@example.com", "password": "WrongPassword!"},
                    ).status_code
                    == 401
                )

                # Re-enable member -> old pre-suspension token stays revoked (401), fresh login ok
                r_reenable = client.patch(
                    f"/api/admin/users/{m1_id}",
                    headers=admin_headers,
                    json={"is_active": True},
                )
                assert r_reenable.status_code == 200
                assert r_reenable.json()["is_active"] is True
                assert r_reenable.json()["disabled_at"] is None
                assert client.get("/api/vehicles", headers=m1_headers).status_code == 401

                r_relogin = client.post(
                    "/api/auth/login",
                    json={"email": "mate1@example.com", "password": "MatePassword123!"},
                )
                assert r_relogin.status_code == 200
                m1_fresh_headers = {"Authorization": f"Bearer {r_relogin.json()['access_token']}"}
                client.cookies.clear()
                assert client.get("/api/vehicles", headers=m1_fresh_headers).status_code == 200

                # Promote mate2 to admin -> now owner can be demoted cleanly (2 active admins)
                r_prom = client.patch(
                    f"/api/admin/users/{m2_id}",
                    headers=admin_headers,
                    json={"role": "admin"},
                )
                assert r_prom.status_code == 200
                assert r_prom.json()["role"] == "admin"

                # Verify GET /api/admin/users lists invited_by_code cleanly
                users_list = client.get("/api/admin/users", headers=admin_headers).json()
                assert users_list["total"] == 3
                users_by_email = {u["email"]: u for u in users_list["users"]}
                assert users_by_email["mate1@example.com"]["invited_by_code"] == inv1["code"]

                # 10. Verify GET /api/admin/stats aggregates DB + live snapshot + storage + feeds
                r_stats = client.get("/api/admin/stats", headers=admin_headers)
                assert r_stats.status_code == 200, r_stats.text
                stats = r_stats.json()
                assert stats["traffic_24h"] == {
                    "request_count": 45,
                    "error_count": 1,
                    "bytes_out": 125_000,
                    "rate_limited_count": 1,
                }
                assert stats["traffic_7d"] == {
                    "request_count": 145,
                    "error_count": 3,
                    "bytes_out": 625_000,
                    "rate_limited_count": 4,
                }
                assert stats["route_breakdown"][0]["route_group"] == "tracks"
                assert stats["route_breakdown"][0]["request_count"] == 45
                assert stats["users"] == {
                    "total": 3,
                    "active": 3,
                    "admins": 2,
                    "members": 1,
                    "suspended": 0,
                }
                assert stats["invites"] == {"active": 0, "total": 4}
                assert stats["content"]["tracks"] >= 5
                assert stats["content"]["segments"] > 50
                assert stats["storage"]["tiles_bytes"] == 1024
                assert stats["storage"]["uploads_bytes"] == 2048
                assert stats["storage"]["uploads_quota_bytes"] == 10 * 1024 * 1024
                assert stats["storage"]["db_size_bytes"] > 0
                assert "feeds" in stats and "overall_status" in stats["feeds"]

                # 11. Registration mode `closed` blocks everyone (even with a fresh invite!)
                r_fresh_inv = client.post(
                    "/api/admin/invites",
                    headers=admin_headers,
                    json={"label": "Closed Mode Check", "max_uses": 2},
                )
                assert r_fresh_inv.status_code == 201
                fresh_code = r_fresh_inv.json()["code"]

                monkeypatch.setenv("TRAILFORGE_REGISTRATION_MODE", "closed")
                get_settings.cache_clear()
                r_closed = client.post(
                    "/api/auth/register",
                    json={
                        "email": "closed@example.com",
                        "display_name": "Closed User",
                        "password": "StrongPassword123!",
                        "invite_code": fresh_code,
                    },
                )
                assert r_closed.status_code == 403
                assert "closed" in r_closed.json()["detail"].lower()

                # 12. Registration mode `open` allows signup without code & still honors invite code
                monkeypatch.setenv("TRAILFORGE_REGISTRATION_MODE", "open")
                get_settings.cache_clear()
                r_open = client.post(
                    "/api/auth/register",
                    json={
                        "email": "openuser@example.com",
                        "display_name": "Open User",
                        "password": "StrongPassword123!",
                    },
                )
                assert r_open.status_code == 201
                assert r_open.json()["user"]["role"] == "member"
                client.cookies.clear()

                r_open_inv = client.post(
                    "/api/auth/register",
                    json={
                        "email": "openinvited@example.com",
                        "display_name": "Open Invited",
                        "password": "StrongPassword123!",
                        "invite_code": fresh_code,
                    },
                )
                assert r_open_inv.status_code == 201
                u_open_inv = session.execute(
                    select(User).where(User.email == "openinvited@example.com")
                ).scalar_one()
                assert u_open_inv.invited_by_code == fresh_code

            app.dependency_overrides.clear()
    finally:
        get_settings.cache_clear()
        _truncate_all(postgis_engine)


@pytest.mark.integration
def test_auth_cli_end_to_end_create_admin_promote_and_invites(
    postgis_engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _truncate_all(postgis_engine)
    try:
        with Session(postgis_engine) as session:
            # 1. create-admin
            rc1 = auth_cli_main(
                [
                    "create-admin",
                    "--email",
                    "cli-admin@example.com",
                    "--name",
                    "CLI Admin",
                    "--password",
                    "CliAdminPass123!",
                    "--json",
                ],
                session=session,
            )
            assert rc1 == 0
            out1 = json.loads(capsys.readouterr().out)
            assert out1["email"] == "cli-admin@example.com"
            assert out1["role"] == "admin"

            # 2. Seed a member and promote via CLI
            member = User(
                email="cli-member@example.com",
                display_name="CLI Member",
                password_hash="scrypt$stub",
                role="member",
                is_active=False,
            )
            session.add(member)
            session.commit()

            rc2 = auth_cli_main(
                ["promote", "--email", "CLI-Member@Example.COM", "--json"],
                session=session,
            )
            assert rc2 == 0
            out2 = json.loads(capsys.readouterr().out)
            assert out2["role"] == "admin"
            assert out2["is_active"] is True

            # 3. create-invite & list-invites
            rc3 = auth_cli_main(
                [
                    "create-invite",
                    "--label",
                    "Weekend Trip",
                    "--max-uses",
                    "5",
                    "--expires-days",
                    "30",
                    "--json",
                ],
                session=session,
            )
            assert rc3 == 0
            out3 = json.loads(capsys.readouterr().out)
            assert _INVITE_CODE_RE.match(out3["code"])
            assert out3["max_uses"] == 5
            assert out3["status"] == "active"

            rc4 = auth_cli_main(["list-invites", "--json"], session=session)
            assert rc4 == 0
            out4 = json.loads(capsys.readouterr().out)
            assert out4["total"] == 1
            assert out4["invites"][0]["code"] == out3["code"]

        # 4. Real subprocess smoke test with -W error::RuntimeWarning against trailforge_test
        env = dict(os.environ)
        env["DATABASE_URL"] = str(postgis_engine.url.render_as_string(hide_password=False))
        proc = subprocess.run(
            [
                sys.executable,
                "-W",
                "error::RuntimeWarning",
                "-m",
                "trailforge.auth.cli",
                "list-invites",
                "--json",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"Subprocess CLI failed: {proc.stderr}"
        parsed: dict[str, Any] = json.loads(proc.stdout)
        assert parsed["total"] == 1
    finally:
        _truncate_all(postgis_engine)
