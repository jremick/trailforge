"""Exploit-first security hardening tests (H-2, H-3, M-1, M-4, M-6, L-1, L-2, L-4)."""

from __future__ import annotations

import gc
import hashlib
import io
import logging
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin
from pydantic import ValidationError
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

import trailforge.api.auth as auth_api_module
from trailforge.api.main import create_app
from trailforge.api.middleware import reset_rate_limiter
from trailforge.api.tracks import _parse_bbox
from trailforge.api.trips import MobileSyncRequest, TripUploadJsonRequest
from trailforge.auth.security import (
    DEV_DEFAULT_AUTH_SECRET,
    DEV_DEFAULT_PRIVACY_SECRET,
    create_access_token,
    hash_password,
    validate_production_secrets,
    verify_access_token,
    verify_password,
)
from trailforge.config import Settings, get_settings
from trailforge.db.models import ConditionReport, ConditionReportVote, InviteCode, User
from trailforge.db.session import get_db
from trailforge.feeds.adapters import NSWNPWSAdapter, VicEmergencyAdapter
from trailforge.feeds.adapters_au import ACTESAAlertsAdapter
from trailforge.feeds.base import (
    MAX_FEED_BODY_BYTES,
    NormalizedClosure,
    parse_safe_xml,
    sanitize_source_url,
)
from trailforge.matching.parser import (
    MAX_IMU_SAMPLES,
    MAX_TRACE_POINTS,
    parse_gpx,
    parse_telemetry_json,
)
from trailforge.reports.photos import (
    MAX_PHOTO_DIMENSION,
    PhotoValidationError,
    save_report_photo,
    validate_photo_bytes,
)

_STRONG_AUTH_SECRET = "prod-auth-secret-a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6"
_STRONG_PRIVACY_SECRET = "prod-privacy-secret-z9y8x7w6v5u4t3s2r1q0p9o8n7m6"


def _make_jpeg_with_gps_exif(
    *, width: int = 24, height: int = 12, orientation: int = 6
) -> bytes:
    """Create a real Pillow JPEG carrying GPS EXIF tags, camera Make/Model, and Orientation."""
    img = Image.new("RGB", (width, height), color=(42, 128, 72))
    exif = Image.Exif()
    exif[0x010F] = "SECRET-CAMERA-MAKE"  # Make
    exif[0x0110] = "SECRET-HOME-CAMP-GPS"  # Model
    exif[0x0112] = orientation  # Orientation (6 = 90 deg CW -> swaps width & height)
    exif[0x0131] = "SecretPhoneOS 18.0"  # Software
    # GPS IFD (tag 0x8825) with synthetic latitude/longitude (Pillow 13 accepts floats/IFDRational)
    exif[0x8825] = {
        1: "S",
        2: (36.0, 34.0, 12.0),
        3: "E",
        4: (148.0, 7.0, 18.0),
    }
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes(), quality=90)
    raw = buf.getvalue()
    assert b"SECRET-HOME-CAMP-GPS" in raw
    assert b"Exif\x00\x00" in raw
    return raw


def _make_png_with_text_metadata(*, width: int = 16, height: int = 16) -> bytes:
    """Create a real Pillow PNG containing tEXt metadata chunks."""
    img = Image.new("RGBA", (width, height), color=(15, 90, 180, 220))
    meta = PngImagePlugin.PngInfo()
    meta.add_text("Author", "secret-home-location@example.test")
    meta.add_text("GPS", "-36.566,148.120")
    buf = io.BytesIO()
    img.save(buf, format="PNG", pnginfo=meta)
    raw = buf.getvalue()
    assert b"secret-home-location@example.test" in raw
    return raw


def _make_webp_with_exif(*, width: int = 16, height: int = 16) -> bytes:
    """Create a real Pillow WebP carrying EXIF metadata."""
    img = Image.new("RGB", (width, height), color=(190, 80, 30))
    exif = Image.Exif()
    exif[0x010F] = "SECRET-WEBP-DEVICE"
    buf = io.BytesIO()
    img.save(buf, format="WEBP", exif=exif.tobytes())
    raw = buf.getvalue()
    assert b"SECRET-WEBP-DEVICE" in raw
    return raw


def _truncate_security_tables(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                TRUNCATE TABLE
                    condition_report_votes, condition_reports,
                    telemetry_observations, unmapped_track_candidates, trips,
                    planned_routes, user_favorites, user_pins, vehicle_profiles,
                    invite_codes, usage_metrics_hourly, users
                RESTART IDENTITY CASCADE
                """
            )
        )


# -----------------------------------------------------------------------------
# Pure Offline Unit Tests (Zero DB, Zero Network)
# -----------------------------------------------------------------------------


def test_h2_validate_production_secrets_fail_closed_and_dev_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)

    # 1. Local dev mode with defaults logs warning instead of raising
    dev_cfg = Settings(
        auth_cookie_secure=False,
        registration_mode="open",
        auth_secret_key=DEV_DEFAULT_AUTH_SECRET,
        privacy_secret_key=DEV_DEFAULT_PRIVACY_SECRET,
    )
    with caplog.at_level(logging.WARNING, logger="trailforge.auth.security"):
        validate_production_secrets(dev_cfg)
    assert any("insecure development secrets" in rec.message.lower() for rec in caplog.records)

    # 2. Production triggers must fail closed on dev defaults
    for trigger_kwargs in (
        {"auth_cookie_secure": True, "registration_mode": "open"},
        {"auth_cookie_secure": False, "registration_mode": "invite_only"},
        {"auth_cookie_secure": False, "registration_mode": "closed"},
    ):
        bad_auth = Settings(
            **trigger_kwargs,
            auth_secret_key=DEV_DEFAULT_AUTH_SECRET,
            privacy_secret_key=_STRONG_PRIVACY_SECRET,
        )
        with pytest.raises(RuntimeError, match="AUTH_SECRET_KEY"):
            validate_production_secrets(bad_auth)

        bad_priv = Settings(
            **trigger_kwargs,
            auth_secret_key=_STRONG_AUTH_SECRET,
            privacy_secret_key=DEV_DEFAULT_PRIVACY_SECRET,
        )
        with pytest.raises(RuntimeError, match="TRAILFORGE_PRIVACY_SECRET"):
            validate_production_secrets(bad_priv)

    # 3. RAILWAY_ENVIRONMENT also triggers fail-closed guard
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    with pytest.raises(RuntimeError, match="AUTH_SECRET_KEY"):
        validate_production_secrets(dev_cfg)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)

    # 4. Short (< 32 char) non-default secrets also fail closed in production
    short_cfg = Settings(
        auth_cookie_secure=True,
        auth_secret_key="short-but-custom-secret",
        privacy_secret_key=_STRONG_PRIVACY_SECRET,
    )
    with pytest.raises(RuntimeError, match="at least 32 characters"):
        validate_production_secrets(short_cfg)

    # 5. Strong >= 32 char secrets + public_base_url pass cleanly in production with no warnings
    caplog.clear()
    good_cfg = Settings(
        auth_cookie_secure=True,
        registration_mode="invite_only",
        auth_secret_key=_STRONG_AUTH_SECRET,
        privacy_secret_key=_STRONG_PRIVACY_SECRET,
        public_base_url="https://trails.example.com",
    )
    with caplog.at_level(logging.WARNING, logger="trailforge.auth.security"):
        validate_production_secrets(good_cfg)
    assert not caplog.records

    # Non-MCP production deployments do not require public_base_url
    no_mcp_cfg = Settings(
        auth_cookie_secure=True,
        auth_secret_key=_STRONG_AUTH_SECRET,
        privacy_secret_key=_STRONG_PRIVACY_SECRET,
        enable_mcp=False,
    )
    validate_production_secrets(no_mcp_cfg)


def test_h3_telemetry_point_and_imu_array_caps() -> None:
    assert MAX_TRACE_POINTS == 50_000
    assert MAX_IMU_SAMPLES == 100_000

    # Verify Pydantic schema enforces max_length on both TripUploadJsonRequest & MobileSyncRequest
    for model_cls in (TripUploadJsonRequest, MobileSyncRequest):
        pts_meta = model_cls.model_fields["points"].metadata
        imu_meta = model_cls.model_fields["imu_samples"].metadata
        assert any(getattr(m, "max_length", None) == MAX_TRACE_POINTS for m in pts_meta)
        assert any(getattr(m, "max_length", None) == MAX_IMU_SAMPLES for m in imu_meta)

    # Exceeding MAX_TRACE_POINTS in parse_telemetry_json raises ValueError immediately
    oversized_pts = [{"lon": 148.12, "lat": -36.56}] * (MAX_TRACE_POINTS + 1)
    with pytest.raises(ValueError, match=str(MAX_TRACE_POINTS)):
        parse_telemetry_json({"name": "Too Big", "points": oversized_pts})

    oversized_coords = [[148.12, -36.56]] * (MAX_TRACE_POINTS + 1)
    with pytest.raises(ValueError, match=str(MAX_TRACE_POINTS)):
        parse_telemetry_json({"type": "LineString", "coordinates": oversized_coords})

    # Exceeding MAX_TRACE_POINTS in parse_gpx raises ValueError
    gpx_pts = '<trkpt lat="-36.56" lon="148.12"/>' * (MAX_TRACE_POINTS + 1)
    oversized_gpx = (
        f'<?xml version="1.0"?><gpx version="1.1"><trk><trkseg>{gpx_pts}</trkseg></trk></gpx>'
    )
    with pytest.raises(ValueError, match=str(MAX_TRACE_POINTS)):
        parse_gpx(oversized_gpx)

    # Exceeding MAX_TRACE_POINTS in MobileSyncRequest raises ValidationError (422 in FastAPI)
    with pytest.raises(ValidationError):
        MobileSyncRequest.model_validate({"points": oversized_pts})

    # Exceeding MAX_IMU_SAMPLES in MobileSyncRequest raises ValidationError (422 in FastAPI)
    valid_two_pts = [
        {"lon": 148.12, "lat": -36.56},
        {"lon": 148.13, "lat": -36.57},
    ]
    oversized_imu = [{"t_ms": 0, "ax": 0.0, "ay": 0.0, "az": 9.8}] * (MAX_IMU_SAMPLES + 1)
    with pytest.raises(ValidationError):
        MobileSyncRequest.model_validate({"points": valid_two_pts, "imu_samples": oversized_imu})

    del oversized_pts, oversized_coords, gpx_pts, oversized_gpx, oversized_imu
    gc.collect()


def test_m1_pillow_exif_gps_stripping_corrupt_rejection_and_dimension_guard(
    tmp_path: Path,
) -> None:
    # 1. Real JPEG with GPS EXIF & Orientation=6 -> EXIF completely stripped & pixels transposed
    gps_jpg = _make_jpeg_with_gps_exif(width=24, height=12, orientation=6)
    saved_jpg = save_report_photo(gps_jpg, uploads_dir=tmp_path)
    saved_jpg_bytes = saved_jpg.path.read_bytes()
    assert saved_jpg.media_type == "image/jpeg"
    assert b"SECRET-HOME-CAMP-GPS" not in saved_jpg_bytes
    assert b"SECRET-CAMERA-MAKE" not in saved_jpg_bytes
    assert b"Exif\x00\x00" not in saved_jpg_bytes

    with Image.open(io.BytesIO(saved_jpg_bytes)) as reloaded:
        assert reloaded.format == "JPEG"
        # Orientation=6 rotated 24x12 -> 12x24 cleanly before stripping EXIF
        assert reloaded.size == (12, 24)
        assert len(reloaded.getexif()) == 0

    # 2. Real PNG with tEXt metadata -> metadata stripped
    meta_png = _make_png_with_text_metadata()
    saved_png = save_report_photo(meta_png, uploads_dir=tmp_path)
    saved_png_bytes = saved_png.path.read_bytes()
    assert b"secret-home-location@example.test" not in saved_png_bytes
    with Image.open(io.BytesIO(saved_png_bytes)) as reloaded_png:
        assert reloaded_png.format == "PNG"
        assert reloaded_png.size == (16, 16)
        assert not getattr(reloaded_png, "text", {})

    # 3. Real WebP with EXIF -> EXIF stripped
    meta_webp = _make_webp_with_exif()
    saved_webp = save_report_photo(meta_webp, uploads_dir=tmp_path)
    saved_webp_bytes = saved_webp.path.read_bytes()
    assert b"SECRET-WEBP-DEVICE" not in saved_webp_bytes
    with Image.open(io.BytesIO(saved_webp_bytes)) as reloaded_webp:
        assert reloaded_webp.format == "WEBP"
        assert len(reloaded_webp.getexif()) == 0

    # 4. Corrupt / magic-byte prefix-only fake payloads rejected
    for fake_payload in (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9",
        b"\xff\xd8\xff<script>alert(1)</script>",
        b"\x89PNG\r\n\x1a\nnot-really-a-png",
        b"RIFF\x1a\x00\x00\x00WEBPVP8 \x0e\x00\x00\x00\x30\x01\x00\x9d\x01\x2a\x01\x00\x01\x00",
    ):
        with pytest.raises(PhotoValidationError):
            validate_photo_bytes(fake_payload)
        with pytest.raises(PhotoValidationError):
            save_report_photo(fake_payload, uploads_dir=tmp_path)

    # 5. Oversized dimensions (> 4096px) rejected cleanly
    big_buf = io.BytesIO()
    with Image.new("RGB", (MAX_PHOTO_DIMENSION + 1, 8), color=(10, 20, 30)) as big_img:
        big_img.save(big_buf, format="PNG")
    big_png_bytes = big_buf.getvalue()
    with pytest.raises(PhotoValidationError, match=str(MAX_PHOTO_DIMENSION)):
        validate_photo_bytes(big_png_bytes)
    with pytest.raises(PhotoValidationError, match=str(MAX_PHOTO_DIMENSION)):
        save_report_photo(big_png_bytes, uploads_dir=tmp_path)
    del big_png_bytes, big_buf
    gc.collect()


def test_m4_token_version_claim_encoding_and_validation() -> None:
    uid = str(uuid.uuid4())
    tok_default = create_access_token(uid, "member@example.com")
    payload_default = verify_access_token(tok_default)
    assert payload_default is not None
    assert payload_default["tv"] == 1

    tok_v4 = create_access_token(uid, "member@example.com", token_version=4)
    payload_v4 = verify_access_token(tok_v4)
    assert payload_v4 is not None
    assert payload_v4["tv"] == 4


def test_m6_dummy_password_hash_invokes_full_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    real_scrypt = hashlib.scrypt

    def _spy_scrypt(password: bytes, **kwargs: Any) -> bytes:
        calls.append(dict(kwargs))
        return real_scrypt(password, **kwargs)

    monkeypatch.setattr(hashlib, "scrypt", _spy_scrypt)
    assert verify_password("WrongPassword123!", auth_api_module._DUMMY_PASSWORD_HASH) is False
    assert len(calls) == 1
    assert calls[0]["n"] == 16384
    assert calls[0]["r"] == 8
    assert calls[0]["p"] == 1
    assert calls[0]["dklen"] == 32


def test_l1_bbox_rejects_nan_inf_and_out_of_range() -> None:
    assert _parse_bbox("148.0,-37.0,149.0,-36.0") == (148.0, -37.0, 149.0, -36.0)

    for bad_bbox in (
        "nan,0,1,1",
        "0,nan,1,1",
        "0,0,nan,1",
        "0,0,1,nan",
        "-inf,-inf,inf,inf",
        "148,-37,inf,-36",
        "-181,-37,148,-36",
        "148,-37,181,-36",
        "148,-91,149,-36",
        "148,-37,149,91",
        "149,-37,148,-36",
        "148,-36,149,-37",
    ):
        with pytest.raises(HTTPException) as exc_info:
            _parse_bbox(bad_bbox)
        assert exc_info.value.status_code == 422, f"Expected 422 for bbox={bad_bbox!r}"


def test_l2_feed_xml_doctype_entity_body_cap_and_source_url_sanitization() -> None:
    # 1. XML DOCTYPE / ENTITY billion-laughs payload rejected across all feed XML entrypoints
    entity_bomb = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
        "<rss><channel><item><title>&lol2;</title></item></channel></rss>"
    )
    with pytest.raises(ValueError, match="DOCTYPE/ENTITY"):
        parse_safe_xml(entity_bomb)
    with pytest.raises(ValueError, match="DOCTYPE/ENTITY"):
        parse_safe_xml("<!doctype rss><rss/>")

    npws = NSWNPWSAdapter()
    with pytest.raises(ValueError, match="DOCTYPE/ENTITY"):
        npws._parse_rss_items(entity_bomb)

    act_adapter = ACTESAAlertsAdapter(fixture_payload=entity_bomb)
    with pytest.raises(ValueError, match="DOCTYPE/ENTITY"):
        act_adapter.fetch()

    # 2. HTTP feed response body cap (10 MiB) enforced on streaming body & Content-Length header
    oversized_body = b"x" * (MAX_FEED_BODY_BYTES + 1024)
    transport_stream = httpx.MockTransport(
        lambda request, _body=oversized_body: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=_body,
        )
    )
    with httpx.Client(transport=transport_stream) as client:
        vic_em = VicEmergencyAdapter(client=client)
        with pytest.raises(ValueError, match="byte limit"):
            vic_em.fetch()

        npws_oversized = NSWNPWSAdapter(client=client)
        with pytest.raises(ValueError, match="byte limit"):
            npws_oversized.fetch()

    del oversized_body, transport_stream
    gc.collect()

    # Content-Length header exceeding 10 MiB fails fast before reading body
    transport_cl = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "content-length": str(MAX_FEED_BODY_BYTES + 1),
            },
            content=b"{}",
        )
    )
    with httpx.Client(transport=transport_cl) as client:
        vic_em_cl = VicEmergencyAdapter(client=client)
        with pytest.raises(ValueError, match="byte limit"):
            vic_em_cl.fetch()

    # 3. source_url scheme sanitization rejects javascript:/data:/file: & preserves https://
    assert sanitize_source_url("https://example.gov.au/alerts/1") == "https://example.gov.au/alerts/1"
    assert sanitize_source_url("http://example.gov.au/alerts/1") == "http://example.gov.au/alerts/1"
    assert sanitize_source_url("javascript:alert(1)") is None
    assert sanitize_source_url("data:text/html,<script>alert(1)</script>") is None
    assert sanitize_source_url("file:///etc/passwd") is None
    assert sanitize_source_url("https://example.gov.au/\r\nEvil: 1") is None
    assert (
        sanitize_source_url("javascript:alert(1)", fallback="https://example.gov.au/")
        == "https://example.gov.au/"
    )

    closure = NormalizedClosure(
        source_feed="vic_emergency",
        external_id="xss-url-1",
        alert_type="bushfire",
        db_severity="advisory",
        title="Test Fire",
        state="VIC",
        source_url="javascript:alert(1)",
    )
    assert closure.source_url is None

    # Adapter falls back to official https:// URL when upstream item carries javascript: URL
    parsed_items = VicEmergencyAdapter(
        fixture_items=[
            {
                "id": "evil-1",
                "title": "Alpine Bushfire Warning",
                "category1": "Fire",
                "url": "javascript:alert(document.cookie)",
            }
        ]
    ).fetch()
    assert len(parsed_items) == 1
    assert parsed_items[0].source_url == "https://emergency.vic.gov.au/respond/"


# -----------------------------------------------------------------------------
# PostGIS Integration Tests (100% Offline)
# -----------------------------------------------------------------------------


@pytest.mark.integration
def test_m4_logout_and_admin_changes_revoke_tokens_end_to_end(
    postgis_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _truncate_security_tables(postgis_engine)
    monkeypatch.setenv("TRAILFORGE_REGISTRATION_MODE", "open")
    monkeypatch.setenv("AUTH_SECRET_KEY", _STRONG_AUTH_SECRET)
    monkeypatch.setenv("TRAILFORGE_PRIVACY_SECRET", _STRONG_PRIVACY_SECRET)
    monkeypatch.setenv("TRAILFORGE_PUBLIC_BASE_URL", "https://trails.example.com")
    get_settings.cache_clear()

    try:
        with Session(postgis_engine) as session:
            app = create_app()

            def _override_db() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db] = _override_db

            with TestClient(app) as client:
                # Create admin directly
                admin = User(
                    email="admin.m4@example.com",
                    display_name="Admin M4",
                    password_hash=hash_password("AdminPass123!"),
                    role="admin",
                    is_active=True,
                )
                session.add(admin)
                session.commit()
                session.refresh(admin)
                admin_tok = create_access_token(
                    str(admin.id), admin.email, token_version=admin.token_version
                )
                admin_headers = {"Authorization": f"Bearer {admin_tok}"}

                # Register member -> token_v1
                r_reg = client.post(
                    "/api/auth/register",
                    json={
                        "email": "member.m4@example.com",
                        "display_name": "Member M4",
                        "password": "MemberPass123!",
                    },
                )
                assert r_reg.status_code == 201, r_reg.text
                member_id = r_reg.json()["user"]["id"]
                tok_v1 = r_reg.json()["access_token"]
                h_v1 = {"Authorization": f"Bearer {tok_v1}"}
                client.cookies.clear()

                assert client.get("/api/vehicles", headers=h_v1).status_code == 200
                assert client.get("/api/auth/me", headers=h_v1).json()["authenticated"] is True

                # 1. Logout invalidates Bearer token server-side immediately -> 401
                assert client.post("/api/auth/logout", headers=h_v1).status_code == 200
                assert client.get("/api/vehicles", headers=h_v1).status_code == 401
                assert client.get("/api/auth/me", headers=h_v1).json()["authenticated"] is False

                # 2. Fresh login issues token_v2 (tv == 2)
                r_login2 = client.post(
                    "/api/auth/login",
                    json={"email": "member.m4@example.com", "password": "MemberPass123!"},
                )
                assert r_login2.status_code == 200
                tok_v2 = r_login2.json()["access_token"]
                assert verify_access_token(tok_v2)["tv"] == 2  # type: ignore[index]
                h_v2 = {"Authorization": f"Bearer {tok_v2}"}
                client.cookies.clear()
                assert client.get("/api/vehicles", headers=h_v2).status_code == 200

                # 3. Admin role change increments token_version -> revokes token_v2 (401)
                r_prom = client.patch(
                    f"/api/admin/users/{member_id}",
                    headers=admin_headers,
                    json={"role": "admin"},
                )
                assert r_prom.status_code == 200
                assert client.get("/api/vehicles", headers=h_v2).status_code == 401

                # Fresh login after role change works (tv == 3)
                r_login3 = client.post(
                    "/api/auth/login",
                    json={"email": "member.m4@example.com", "password": "MemberPass123!"},
                )
                assert r_login3.status_code == 200
                tok_v3 = r_login3.json()["access_token"]
                assert verify_access_token(tok_v3)["tv"] == 3  # type: ignore[index]
                h_v3 = {"Authorization": f"Bearer {tok_v3}"}
                client.cookies.clear()
                assert client.get("/api/vehicles", headers=h_v3).status_code == 200

                # 4. Admin suspend increments token_version -> revokes token_v3 (401)
                r_susp = client.patch(
                    f"/api/admin/users/{member_id}",
                    headers=admin_headers,
                    json={"is_active": False},
                )
                assert r_susp.status_code == 200
                assert client.get("/api/vehicles", headers=h_v3).status_code == 401

                # Re-enabling increments token_version again -> pre-suspension token stays 401!
                r_reenable = client.patch(
                    f"/api/admin/users/{member_id}",
                    headers=admin_headers,
                    json={"is_active": True},
                )
                assert r_reenable.status_code == 200
                assert client.get("/api/vehicles", headers=h_v3).status_code == 401

                # Fresh login succeeds after re-enable (tv == 5)
                r_login5 = client.post(
                    "/api/auth/login",
                    json={"email": "member.m4@example.com", "password": "MemberPass123!"},
                )
                assert r_login5.status_code == 200
                assert verify_access_token(r_login5.json()["access_token"])["tv"] == 5  # type: ignore[index]

            app.dependency_overrides.clear()
    finally:
        get_settings.cache_clear()
        _truncate_security_tables(postgis_engine)


@pytest.mark.integration
def test_m6_invite_checked_before_email_409_and_login_dummy_scrypt(
    postgis_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _truncate_security_tables(postgis_engine)
    monkeypatch.setenv("TRAILFORGE_REGISTRATION_MODE", "invite_only")
    monkeypatch.setenv("TRAILFORGE_ADMIN_EMAIL", "boss@example.com")
    monkeypatch.setenv("AUTH_SECRET_KEY", _STRONG_AUTH_SECRET)
    monkeypatch.setenv("TRAILFORGE_PRIVACY_SECRET", _STRONG_PRIVACY_SECRET)
    monkeypatch.setenv("TRAILFORGE_PUBLIC_BASE_URL", "https://trails.example.com")
    get_settings.cache_clear()

    try:
        with Session(postgis_engine) as session:
            app = create_app()

            def _override_db() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db] = _override_db

            with TestClient(app) as client:
                # 1. SEC-M3: Unauthenticated admin email cannot bypass invite_only without invite
                r_no_inv_admin = client.post(
                    "/api/auth/register",
                    json={
                        "email": "boss@example.com",
                        "display_name": "Boss Admin",
                        "password": "BossPassword123!",
                    },
                )
                assert r_no_inv_admin.status_code == 403

                # Bootstrap first admin with an operator-provisioned invite code
                session.add(
                    InviteCode(
                        code="TF-BOSS-2345",
                        label="Operator Bootstrap",
                        role="admin",
                        max_uses=1,
                        use_count=0,
                    )
                )
                session.commit()
                r_boot = client.post(
                    "/api/auth/register",
                    json={
                        "email": "boss@example.com",
                        "display_name": "Boss Admin",
                        "password": "BossPassword123!",
                        "invite_code": "TF-BOSS-2345",
                    },
                )
                assert r_boot.status_code == 201, r_boot.text
                assert r_boot.json()["user"]["role"] == "admin"
                admin_headers = {"Authorization": f"Bearer {r_boot.json()['access_token']}"}
                client.cookies.clear()

                # Create a valid single-use invite and a revoked invite
                inv_ok = client.post(
                    "/api/admin/invites",
                    headers=admin_headers,
                    json={"label": "Valid Invite", "max_uses": 1},
                ).json()
                inv_rev = client.post(
                    "/api/admin/invites",
                    headers=admin_headers,
                    json={"label": "Revoked Invite", "max_uses": 5},
                ).json()
                assert (
                    client.delete(
                        f"/api/admin/invites/{inv_rev['id']}", headers=admin_headers
                    ).status_code
                    == 200
                )

                # Seed existing member alice@example.com
                session.add(
                    User(
                        email="alice@example.com",
                        display_name="Alice",
                        password_hash=hash_password("AlicePass123!"),
                        role="member",
                        is_active=True,
                    )
                )
                session.commit()

                # 2. Unauthenticated probing of existing email returns 403 (NEVER 409!)
                for bad_invite in (None, "TF-NOPE-0000", inv_rev["code"]):
                    r_probe = client.post(
                        "/api/auth/register",
                        json={
                            "email": "alice@example.com",
                            "display_name": "Probe",
                            "password": "ProbePassword123!",
                            **({"invite_code": bad_invite} if bad_invite is not None else {}),
                        },
                    )
                    assert r_probe.status_code == 403, (
                        f"Expected 403 for invite={bad_invite!r}, got {r_probe.status_code}"
                    )

                # Even probing TRAILFORGE_ADMIN_EMAIL after first admin exists returns 403 (not 409)
                r_probe_admin = client.post(
                    "/api/auth/register",
                    json={
                        "email": "boss@example.com",
                        "display_name": "Admin Probe",
                        "password": "ProbePassword123!",
                    },
                )
                assert r_probe_admin.status_code == 403

                # 3. Valid invite with existing email returns 409 WITHOUT burning invite use_count
                r_dup_valid = client.post(
                    "/api/auth/register",
                    json={
                        "email": "alice@example.com",
                        "display_name": "Dup Valid",
                        "password": "ProbePassword123!",
                        "invite_code": inv_ok["code"],
                    },
                )
                assert r_dup_valid.status_code == 409
                inv_row = session.execute(
                    select(InviteCode).where(InviteCode.code == inv_ok["code"])
                ).scalar_one()
                assert inv_row.use_count == 0

                # 4. Login for missing email still runs dummy scrypt via verify_password
                verified_hashes: list[str] = []
                real_verify = auth_api_module.verify_password

                def _spy_verify(pw: str, stored_hash: str) -> bool:
                    verified_hashes.append(stored_hash)
                    return real_verify(pw, stored_hash)

                monkeypatch.setattr(auth_api_module, "verify_password", _spy_verify)
                r_missing = client.post(
                    "/api/auth/login",
                    json={"email": "nobody@example.com", "password": "SomePassword123!"},
                )
                assert r_missing.status_code == 401
                assert verified_hashes == [auth_api_module._DUMMY_PASSWORD_HASH]

            app.dependency_overrides.clear()
    finally:
        get_settings.cache_clear()
        _truncate_security_tables(postgis_engine)


@pytest.mark.integration
def test_m1_report_photo_exif_strip_and_per_user_quota_e2e(
    postgis_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _truncate_security_tables(postgis_engine)
    reset_rate_limiter()
    uploads_dir = tmp_path / "sec_uploads"
    monkeypatch.setenv("TRAILFORGE_UPLOADS_DIR", str(uploads_dir))
    monkeypatch.setenv("TRAILFORGE_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("TRAILFORGE_RATE_LIMIT_UPLOAD_PER_DAY", "2")
    monkeypatch.setenv("TRAILFORGE_REGISTRATION_MODE", "open")
    get_settings.cache_clear()

    try:
        with Session(postgis_engine) as session:
            app = create_app()

            def _override_db() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db] = _override_db

            with TestClient(app) as client:
                # Register regular member + admin
                r_mem = client.post(
                    "/api/auth/register",
                    json={
                        "email": "photo.member@example.com",
                        "display_name": "Photo Member",
                        "password": "PhotoPassword123!",
                    },
                )
                assert r_mem.status_code == 201
                mem_headers = {"Authorization": f"Bearer {r_mem.json()['access_token']}"}
                client.cookies.clear()

                admin = User(
                    email="photo.admin@example.com",
                    display_name="Photo Admin",
                    password_hash=hash_password("PhotoAdmin123!"),
                    role="admin",
                    is_active=True,
                )
                session.add(admin)
                session.commit()
                session.refresh(admin)
                admin_tok = create_access_token(
                    str(admin.id), admin.email, token_version=admin.token_version
                )
                admin_headers = {"Authorization": f"Bearer {admin_tok}"}

                gps_jpg = _make_jpeg_with_gps_exif()

                # 1. Fake prefix-only JPEG rejected with 422 (and does NOT consume photo quota)
                r_fake = client.post(
                    "/api/reports",
                    headers=mem_headers,
                    data={
                        "report_type": "bog_hole",
                        "notes": "Fake photo prefix",
                        "lon": "148.12",
                        "lat": "-36.56",
                    },
                    files={"photo": ("fake.jpg", b"\xff\xd8\xffnot-a-real-jpeg", "image/jpeg")},
                )
                assert r_fake.status_code == 422

                # 2. First valid GPS-EXIF JPEG succeeds and strips EXIF on served response
                r_p1 = client.post(
                    "/api/reports",
                    headers=mem_headers,
                    data={
                        "report_type": "bog_hole",
                        "notes": "Photo 1 with GPS EXIF",
                        "lon": "148.12",
                        "lat": "-36.56",
                    },
                    files={"photo": ("gps1.jpg", gps_jpg, "image/jpeg")},
                )
                assert r_p1.status_code == 201, r_p1.text
                photo_url = r_p1.json()["photo_url"]
                assert photo_url is not None
                served = client.get(photo_url)
                assert served.status_code == 200
                assert b"SECRET-HOME-CAMP-GPS" not in served.content
                assert b"Exif\x00\x00" not in served.content
                with Image.open(io.BytesIO(served.content)) as img_check:
                    assert len(img_check.getexif()) == 0

                # 3. Second valid photo succeeds (hits limit of 2/day)
                r_p2 = client.post(
                    "/api/reports",
                    headers=mem_headers,
                    data={
                        "report_type": "fallen_tree",
                        "notes": "Photo 2",
                        "lon": "148.12",
                        "lat": "-36.561",
                    },
                    files={"photo": ("gps2.jpg", gps_jpg, "image/jpeg")},
                )
                assert r_p2.status_code == 201

                # 4. Third photo by same member hits per-user upload quota -> 429 + Retry-After
                r_p3 = client.post(
                    "/api/reports",
                    headers=mem_headers,
                    data={
                        "report_type": "washout",
                        "notes": "Photo 3 should hit quota",
                        "lon": "148.12",
                        "lat": "-36.562",
                    },
                    files={"photo": ("gps3.jpg", gps_jpg, "image/jpeg")},
                )
                assert r_p3.status_code == 429
                assert int(r_p3.headers["retry-after"]) >= 1

                # 5. Text-only report without photo is NOT blocked by photo quota
                r_text = client.post(
                    "/api/reports",
                    headers=mem_headers,
                    json={
                        "report_type": "track_clear",
                        "notes": "Text only report still works",
                        "lon": 148.12,
                        "lat": -36.563,
                    },
                )
                assert r_text.status_code == 201

                # 6. Admin bypasses per-user photo quota
                for idx in range(3):
                    r_adm = client.post(
                        "/api/reports",
                        headers=admin_headers,
                        data={
                            "report_type": "washout",
                            "notes": f"Admin photo {idx}",
                            "lon": "148.12",
                            "lat": f"-36.56{4 + idx}",
                        },
                        files={"photo": (f"adm{idx}.jpg", gps_jpg, "image/jpeg")},
                    )
                    assert r_adm.status_code == 201

            app.dependency_overrides.clear()
    finally:
        reset_rate_limiter()
        get_settings.cache_clear()
        _truncate_security_tables(postgis_engine)


@pytest.mark.integration
def test_l4_admin_pagination_for_update_vote_integrity_and_l1_bbox_http(
    postgis_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _truncate_security_tables(postgis_engine)
    monkeypatch.setenv("TRAILFORGE_REGISTRATION_MODE", "open")
    get_settings.cache_clear()

    try:
        with Session(postgis_engine) as session:
            app = create_app()

            def _override_db() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db] = _override_db

            # Seed 1 admin + 3 members + 3 invites
            admin = User(
                email="l4.admin@example.com",
                display_name="L4 Admin",
                password_hash=hash_password("AdminPass123!"),
                role="admin",
                is_active=True,
            )
            members = [
                User(
                    email=f"l4.member{i}@example.com",
                    display_name=f"Member {i}",
                    password_hash=hash_password("MemberPass123!"),
                    role="member",
                    is_active=True,
                )
                for i in range(3)
            ]
            session.add_all([admin, *members])
            session.flush()
            invites = [
                InviteCode(
                    code=f"TF-PAGE-000{i}",
                    label=f"Invite {i}",
                    role="member",
                    max_uses=2,
                    created_by_user_id=admin.id,
                )
                for i in range(3)
            ]
            session.add_all(invites)
            session.commit()
            session.refresh(admin)

            admin_tok = create_access_token(
                str(admin.id), admin.email, token_version=admin.token_version
            )
            admin_headers = {"Authorization": f"Bearer {admin_tok}"}

            with TestClient(app) as client:
                # 1. L-1 HTTP check: NaN / Inf bbox rejected with 422 on tracks endpoints
                assert client.get("/api/tracks", params={"bbox": "nan,0,1,1"}).status_code == 422
                r_inf_bbox = client.get(
                    "/api/tracks/geojson", params={"bbox": "-inf,-inf,inf,inf"}
                )
                assert r_inf_bbox.status_code == 422

                # 2. L-4 pagination on GET /api/admin/users
                p1 = client.get(
                    "/api/admin/users", headers=admin_headers, params={"limit": 2, "offset": 0}
                )
                assert p1.status_code == 200
                assert p1.json()["total"] == 4
                assert len(p1.json()["users"]) == 2

                p2 = client.get(
                    "/api/admin/users", headers=admin_headers, params={"limit": 2, "offset": 2}
                )
                assert p2.status_code == 200
                assert p2.json()["total"] == 4
                assert len(p2.json()["users"]) == 2
                ids_p1 = {u["id"] for u in p1.json()["users"]}
                ids_p2 = {u["id"] for u in p2.json()["users"]}
                assert ids_p1.isdisjoint(ids_p2)

                # Bounds validation on limit/offset
                assert (
                    client.get(
                        "/api/admin/users", headers=admin_headers, params={"limit": 0}
                    ).status_code
                    == 422
                )
                assert (
                    client.get(
                        "/api/admin/users", headers=admin_headers, params={"limit": 501}
                    ).status_code
                    == 422
                )

                # 3. L-4 pagination on GET /api/admin/invites
                inv_p = client.get(
                    "/api/admin/invites", headers=admin_headers, params={"limit": 2, "offset": 1}
                )
                assert inv_p.status_code == 200
                assert inv_p.json()["total"] == 3
                assert len(inv_p.json()["invites"]) == 2

                # 4. L-4 last-admin FOR UPDATE guard blocks demoting sole active admin -> 400
                r_last = client.patch(
                    f"/api/admin/users/{admin.id}",
                    headers=admin_headers,
                    json={"role": "member"},
                )
                assert r_last.status_code == 400
                assert "last active admin" in r_last.json()["detail"].lower()

                # 5. L-4 concurrent duplicate vote IntegrityError handled cleanly via SAVEPOINT
                r_rep = client.post(
                    "/api/reports",
                    headers=admin_headers,
                    json={
                        "report_type": "fallen_tree",
                        "notes": "Vote race test",
                        "lon": 148.12,
                        "lat": -36.56,
                    },
                )
                assert r_rep.status_code == 201
                report_id = r_rep.json()["id"]

                # Simulate a concurrent transaction inserting the vote right after our SELECT
                # returns None, so our INSERT hits UniqueConstraint IntegrityError cleanly.
                original_flush = session.flush
                race_triggered = False

                def _racy_flush(*args: Any, **kwargs: Any) -> Any:
                    nonlocal race_triggered
                    if not race_triggered:
                        pending_votes = [
                            obj for obj in session.new if isinstance(obj, ConditionReportVote)
                        ]
                        if pending_votes:
                            race_triggered = True
                            with postgis_engine.begin() as other_conn:
                                other_conn.execute(
                                    text(
                                        """
                                        INSERT INTO condition_report_votes (
                                            report_id, user_id, vote_type, created_at
                                        ) VALUES (:rid, :uid, 'confirm', :ts)
                                        """
                                    ),
                                    {
                                        "rid": uuid.UUID(report_id),
                                        "uid": admin.id,
                                        "ts": datetime.now(UTC),
                                    },
                                )
                    return original_flush(*args, **kwargs)

                monkeypatch.setattr(session, "flush", _racy_flush)
                r_vote = client.post(
                    f"/api/reports/{report_id}/vote",
                    headers=admin_headers,
                    json={"vote": "resolved"},
                )
                assert race_triggered is True
                assert r_vote.status_code == 200, r_vote.text
                body = r_vote.json()
                assert body["resolved_count"] == 1
                assert body["confirm_count"] == 0
                assert body["user_vote"] == "resolved"

                # Verify ConditionReport row in DB matches without IntegrityError leak
                rep_row = session.get(ConditionReport, uuid.UUID(report_id))
                assert rep_row is not None
                assert rep_row.resolve_count == 1
                assert rep_row.confirm_count == 0

            app.dependency_overrides.clear()
    finally:
        get_settings.cache_clear()
        _truncate_security_tables(postgis_engine)
