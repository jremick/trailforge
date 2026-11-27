"""Offline regression tests for Sprint 1 P0 Security & Event-Loop Stability hardening.

Covers:
  - SEC-H1: Default-false proxy trust, CIDR peer validation, right-to-left XFF parsing,
    explicit Cloudflare header gating, and partitioned rate-limiter buckets.
  - SEC-H2: 2048 px downscaling + EXIF/PNG-tEXt metadata stripping without quadruple copies.
  - SEC-H3: Vectorized O(M + N log M) IMU alignment parity + non-blocking UsageMetricsStore flush.
  - SEC-H4 & SEC-M3: Zero scrypt CPU work on rejected registrations & no invite_only admin bypass.
  - SEC-H5 & SEC-M2: Cookie-bound OAuth login CSRF nonce, same-origin enforcement on anonymous
    POST /api/oauth/authorize, and strict OAuth JSON parameter type validation (HTTP 400).
  - SEC-H6: MCP JSON Schema input validation (`validate_input=True`) & runtime bounds checks.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import re
import threading
import time
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

import trailforge.api.auth as auth_api_module
from trailforge.api.main import create_app
from trailforge.api.middleware import (
    SlidingWindowRateLimiter,
    UsageMetricsStore,
    extract_client_ip_from_headers,
    reset_rate_limiter,
)
from trailforge.auth.security import hash_password
from trailforge.config import Settings, get_settings
from trailforge.db.models import User
from trailforge.db.session import get_db
from trailforge.matching.imu import ImuSample, align_imu_to_trimmed_points
from trailforge.matching.protocol import GpsPoint
from trailforge.mcp.context import resolve_public_origin_from_headers
from trailforge.mcp.tools import call_mcp_tool
from trailforge.reports.photos import MAX_STORED_PHOTO_DIMENSION, sanitize_photo_bytes

_STRONG_AUTH_SECRET = "a" * 64
_STRONG_PRIVACY_SECRET = "b" * 64


# ---------------------------------------------------------------------------
# SEC-H1: Proxy Trust, CIDR Verification, Right-to-Left XFF & Bucket Partitioning
# ---------------------------------------------------------------------------


def test_sec_h1_proxy_trust_defaults_cidr_and_right_to_left_xff() -> None:
    defaults = Settings()
    assert defaults.trust_proxy_headers is False
    assert defaults.trust_cloudflare_headers is False
    assert defaults.should_trust_cloudflare_headers is False

    # Origin shared secret auto-enables Cloudflare header trust
    cf_cfg = Settings(origin_shared_secret="s" * 32)
    assert cf_cfg.should_trust_cloudflare_headers is True

    hdrs = Headers(
        {
            "CF-Connecting-IP": "198.51.100.11",
            "X-Forwarded-For": "203.0.113.1, 198.51.100.22, 10.0.0.5",
        }
    )

    # 1. Direct untrusted public peer ignores ALL proxy headers even if trust_proxy_headers=True
    assert (
        extract_client_ip_from_headers(
            hdrs,
            client_host="198.51.100.99",
            trust_proxy_headers=True,
            trust_cloudflare_headers=True,
        )
        == "198.51.100.99"
    )

    # 2. Trusted proxy peer WITHOUT Cloudflare trust strips trusted right-side hops and picks
    #    rightmost untrusted hop (prevents client-side XFF left-prepend spoofing)
    assert (
        extract_client_ip_from_headers(
            hdrs,
            client_host="10.0.0.2",
            trust_proxy_headers=True,
            trust_cloudflare_headers=False,
        )
        == "198.51.100.22"
    )

    # 3. Trusted proxy peer WITH Cloudflare trust prefers valid CF-Connecting-IP
    assert (
        extract_client_ip_from_headers(
            hdrs,
            client_host="10.0.0.2",
            trust_proxy_headers=True,
            trust_cloudflare_headers=True,
        )
        == "198.51.100.11"
    )

    # 4. Malformed CF-Connecting-IP falls through to XFF when from trusted peer
    bad_cf = Headers(
        {
            "CF-Connecting-IP": "garbage-not-ip",
            "X-Forwarded-For": "203.0.113.7, 10.0.0.8",
        }
    )
    assert (
        extract_client_ip_from_headers(
            bad_cf,
            client_host="10.0.0.2",
            trust_proxy_headers=True,
            trust_cloudflare_headers=True,
        )
        == "203.0.113.7"
    )

    # 5. resolve_public_origin_from_headers ignores X-Forwarded-Host from untrusted peer or default
    spoof_hdrs = {"host": "legit.example.com", "x-forwarded-host": "evil.example.net"}
    assert (
        resolve_public_origin_from_headers(
            spoof_hdrs,
            scheme="https",
            cfg=Settings(trust_proxy_headers=False),
            client_host="10.0.0.2",
        )
        == "https://legit.example.com"
    )
    assert (
        resolve_public_origin_from_headers(
            spoof_hdrs,
            scheme="https",
            cfg=Settings(trust_proxy_headers=True),
            client_host="203.0.113.50",
        )
        == "https://legit.example.com"
    )
    assert (
        resolve_public_origin_from_headers(
            spoof_hdrs,
            scheme="https",
            cfg=Settings(trust_proxy_headers=True),
            client_host="10.0.0.2",
        )
        == "https://evil.example.net"
    )


def test_sec_h1_partitioned_rate_limiter_isolates_auth_and_dcr_from_api_flood() -> None:
    now = [1000.0]
    limiter = SlidingWindowRateLimiter(clock=lambda: now[0], max_keys=32)

    # Exhaust auth and oauth_dcr buckets for an attacker IP
    assert limiter.check("auth:198.51.100.10", limit=2, window_s=60.0).allowed is True
    assert limiter.check("auth:198.51.100.10", limit=2, window_s=60.0).allowed is True
    assert limiter.check("auth:198.51.100.10", limit=2, window_s=60.0).allowed is False

    assert limiter.check("oauth_dcr:198.51.100.10", limit=1, window_s=60.0).allowed is True
    assert limiter.check("oauth_dcr:198.51.100.10", limit=1, window_s=60.0).allowed is False

    # Flood 300 distinct api:* keys to force LRU eviction inside the api partition
    for i in range(300):
        now[0] += 0.001
        limiter.check(f"api:198.51.{i // 250}.{i % 250}", limit=100, window_s=60.0)

    # Auth & OAuth DCR partitions MUST remain intact (still blocked!)
    assert limiter.check("auth:198.51.100.10", limit=2, window_s=60.0).allowed is False
    assert limiter.check("oauth_dcr:198.51.100.10", limit=1, window_s=60.0).allowed is False


# ---------------------------------------------------------------------------
# SEC-H2: Report Photo Downscaling & EXIF/PNG-tEXt Stripping
# ---------------------------------------------------------------------------


def test_sec_h2_photo_downscaled_to_2048_and_metadata_stripped() -> None:
    # Oversized PNG with embedded text metadata
    png_img = Image.new("RGB", (3000, 2400), color=(42, 128, 64))
    png_info = PngImagePlugin.PngInfo()
    png_info.add_text("Author", "Secret User")
    png_info.add_text("GPS", "-37.1234, 146.5678")
    png_buf = io.BytesIO()
    png_img.save(png_buf, format="PNG", pnginfo=png_info)

    clean_png, png_ext, png_mime = sanitize_photo_bytes(png_buf.getvalue())
    assert png_mime == "image/png"
    assert png_ext == "png"
    assert b"Secret User" not in clean_png
    assert b"-37.1234" not in clean_png
    with Image.open(io.BytesIO(clean_png)) as reopened_png:
        assert max(reopened_png.size) == MAX_STORED_PHOTO_DIMENSION
        assert reopened_png.size == (2048, 1638)
        assert not reopened_png.info.get("Author")
        assert not reopened_png.info.get("GPS")

    # Oversized JPEG with EXIF metadata
    jpg_img = Image.new("RGB", (3200, 1600), color=(180, 90, 40))
    exif = jpg_img.getexif()
    exif[0x010E] = "Private GPS Note"
    jpg_buf = io.BytesIO()
    jpg_img.save(jpg_buf, format="JPEG", exif=exif.tobytes())

    clean_jpg, jpg_ext, jpg_mime = sanitize_photo_bytes(jpg_buf.getvalue())
    assert jpg_mime == "image/jpeg"
    assert jpg_ext == "jpg"
    assert b"Private GPS Note" not in clean_jpg
    with Image.open(io.BytesIO(clean_jpg)) as reopened_jpg:
        assert reopened_jpg.size == (2048, 1024)
        assert len(reopened_jpg.getexif()) == 0


# ---------------------------------------------------------------------------
# SEC-H3: Vectorized IMU Alignment & Non-Blocking UsageMetricsStore Async Flush
# ---------------------------------------------------------------------------


def test_sec_h3_vectorized_imu_alignment_parity_and_speed() -> None:
    t0 = datetime(2026, 6, 1, 8, 0, 0, tzinfo=UTC)
    n_gps = 2000
    pts = [
        GpsPoint(
            lon=146.5 + i * 0.0001,
            lat=-37.1 + i * 0.00005,
            timestamp=t0 + timedelta(seconds=i),
        )
        for i in range(n_gps)
    ]
    # 5 Hz IMU spanning [t0, t0 + (n_gps - 1)s] inclusive -> 9,996 samples inside window
    imu: list[ImuSample] = []
    for k in range((n_gps - 1) * 5 + 1):
        t_ms = int((t0.timestamp() + k * 0.2) * 1000)
        dyn_z = 2.5 if (2000 <= k < 2100) else 0.15
        imu.append(
            ImuSample(
                t_ms=t_ms,
                ax=1.7,  # ~10 deg pitch
                ay=0.0,
                az=9.66 + (dyn_z if k % 2 == 0 else -dyn_z),
            )
        )

    start = time.perf_counter()
    res = align_imu_to_trimmed_points(pts, imu)
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    assert res.discarded_sample_count == 0
    assert res.summary.sample_count == len(imu)
    assert res.summary.roughness_rms is not None and res.summary.roughness_rms > 0.0
    assert res.summary.max_pitch_deg is not None and res.summary.max_pitch_deg > 5.0
    assert res.points[405].roughness_rms is not None
    assert res.points[405].roughness_rms > (res.points[100].roughness_rms or 0.0)
    assert elapsed_ms < 500.0, f"Expected fast vectorized alignment, took {elapsed_ms:.1f} ms"

    # Non-monotonic GPS timestamps still work cleanly via argsort fallback
    swapped = [pts[0], pts[2], pts[1], pts[3]]
    res_non_mono = align_imu_to_trimmed_points(swapped, imu[:30])
    assert len(res_non_mono.points) == 4
    assert res_non_mono.summary.sample_count > 0


def test_sec_h3_usage_metrics_maybe_flush_async_dispatches_to_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trailforge.db import session as db_session

    store = UsageMetricsStore(flush_interval_s=1.0, flush_batch_threshold=1)
    flushed_threads: list[int] = []

    def _fake_flush(session: Any = None) -> int:
        flushed_threads.append(threading.get_ident())
        return 1

    monkeypatch.setattr(db_session, "_engine", object())
    monkeypatch.setattr(store, "flush", _fake_flush)
    store.record(route_group="tracks", status_code=200, bytes_out=128)

    loop_tid = threading.get_ident()

    async def _drive() -> None:
        store.maybe_flush_async()
        for _ in range(50):
            if flushed_threads:
                break
            await asyncio.sleep(0.01)

    asyncio.run(_drive())
    assert len(flushed_threads) == 1
    assert flushed_threads[0] != loop_tid


# ---------------------------------------------------------------------------
# SEC-H4 & SEC-M3: No Scrypt Before Policy Check + No Invite-Only Admin Bypass
# ---------------------------------------------------------------------------


def test_sec_h4_and_m3_no_scrypt_on_rejected_register_and_no_admin_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE users (
                    id CHAR(32) PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role VARCHAR(16) NOT NULL DEFAULT 'member',
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    token_version INTEGER NOT NULL DEFAULT 1,
                    disabled_at DATETIME,
                    invited_by_code VARCHAR(64),
                    ui_preferences JSON NOT NULL DEFAULT '{}',
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    hash_calls: list[str] = []

    def _spy_hash(password: str) -> str:
        hash_calls.append(password)
        return hash_password(password)

    monkeypatch.setattr(auth_api_module, "hash_password", _spy_hash)

    for mode in ("closed", "invite_only"):
        monkeypatch.setenv("TRAILFORGE_REGISTRATION_MODE", mode)
        monkeypatch.setenv("TRAILFORGE_ADMIN_EMAIL", "admin@example.com")
        monkeypatch.setenv("AUTH_SECRET_KEY", _STRONG_AUTH_SECRET)
        monkeypatch.setenv("TRAILFORGE_PRIVACY_SECRET", _STRONG_PRIVACY_SECRET)
        monkeypatch.setenv("TRAILFORGE_PUBLIC_BASE_URL", "https://trails.example.com")
        get_settings.cache_clear()
        try:
            app = create_app(settings=get_settings())

            def _override_db() -> Generator[Session, None, None]:
                db = session_local()
                try:
                    yield db
                finally:
                    db.close()

            app.dependency_overrides[get_db] = _override_db
            with TestClient(app) as client:
                r = client.post(
                    "/api/auth/register",
                    json={
                        "email": "admin@example.com",
                        "display_name": "Attempted Bypass",
                        "password": "StrongPassword123!",
                    },
                )
                assert r.status_code == 403, f"Expected 403 in mode={mode}, got {r.status_code}"
        finally:
            get_settings.cache_clear()

    assert hash_calls == [], f"hash_password should never run on rejected register: {hash_calls}"


# ---------------------------------------------------------------------------
# SEC-H5 & SEC-M2: Cookie-Bound OAuth Login CSRF + Strict OAuth Parameter Typing
# ---------------------------------------------------------------------------


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _extract_hidden_input(html: str, name: str) -> str:
    m = re.search(rf'name="{re.escape(name)}"\s+value="([^"]*)"', html)
    assert m is not None, f"Missing hidden input {name!r} in HTML"
    return m.group(1)


def test_sec_h5_and_m2_oauth_login_nonce_cookie_and_strict_json_types() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        for ddl in (
            """
            CREATE TABLE users (
                id CHAR(32) PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role VARCHAR(16) NOT NULL DEFAULT 'member',
                is_active BOOLEAN NOT NULL DEFAULT 1,
                token_version INTEGER NOT NULL DEFAULT 1,
                disabled_at DATETIME,
                invited_by_code VARCHAR(64),
                ui_preferences JSON NOT NULL DEFAULT '{}',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE personal_access_tokens (
                id CHAR(32) PRIMARY KEY,
                user_id CHAR(32) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                user_token_version INTEGER NOT NULL DEFAULT 1,
                name VARCHAR(80) NOT NULL,
                token_prefix VARCHAR(16) NOT NULL,
                token_hash VARCHAR(64) NOT NULL UNIQUE,
                scopes JSON NOT NULL,
                expires_at DATETIME,
                last_used_at DATETIME,
                revoked_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE oauth_clients (
                client_id VARCHAR(64) PRIMARY KEY,
                client_name VARCHAR(120) NOT NULL,
                client_secret_hash VARCHAR(64),
                redirect_uris JSON NOT NULL,
                grant_types JSON NOT NULL,
                token_endpoint_auth_method VARCHAR(32) NOT NULL,
                registered_by_ip_hash VARCHAR(64),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at DATETIME
            )
            """,
            """
            CREATE TABLE oauth_authorization_codes (
                code_hash VARCHAR(64) PRIMARY KEY,
                client_id VARCHAR(64) NOT NULL REFERENCES oauth_clients(client_id),
                user_id CHAR(32) NOT NULL REFERENCES users(id),
                redirect_uri TEXT NOT NULL,
                scope TEXT NOT NULL,
                code_challenge VARCHAR(128) NOT NULL,
                code_challenge_method VARCHAR(16) NOT NULL,
                resource TEXT,
                expires_at DATETIME NOT NULL,
                consumed_at DATETIME,
                token_family_id CHAR(32),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE oauth_tokens (
                id CHAR(32) PRIMARY KEY,
                token_family_id CHAR(32) NOT NULL,
                client_id VARCHAR(64) NOT NULL REFERENCES oauth_clients(client_id),
                user_id CHAR(32) NOT NULL REFERENCES users(id),
                user_token_version INTEGER NOT NULL,
                access_token_hash VARCHAR(64) NOT NULL UNIQUE,
                refresh_token_hash VARCHAR(64) UNIQUE,
                scope TEXT NOT NULL,
                resource TEXT,
                access_expires_at DATETIME NOT NULL,
                refresh_expires_at DATETIME,
                revoked_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ):
            conn.execute(text(ddl))
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with session_local() as seed_db:
        seed_db.add(
            User(
                id=uuid.uuid4(),
                email="driver@example.com",
                display_name="Trail Driver",
                password_hash=hash_password("DriverPass123!"),
                role="member",
                is_active=True,
            )
        )
        seed_db.commit()

    cfg = Settings(
        auth_secret_key=_STRONG_AUTH_SECRET,
        privacy_secret_key=_STRONG_PRIVACY_SECRET,
        public_base_url="http://testserver",
        enable_mcp=True,
        mcp_auth_mode="hybrid",
        rate_limit_enabled=False,
    )
    app = create_app(settings=cfg)

    def _override_db() -> Generator[Session, None, None]:
        db = session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    reset_rate_limiter(app)

    with TestClient(app) as client:
        # 1. Register public PKCE client via DCR
        dcr = client.post(
            "/api/oauth/register",
            json={
                "client_name": "Sprint1 Client",
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert dcr.status_code == 201, dcr.text
        client_id = dcr.json()["client_id"]
        verifier = "v" * 48
        challenge = _pkce_challenge(verifier)

        auth_params = {
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "response_type": "code",
            "scope": "trailforge:read",
            "state": "state-sec-h5",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": "http://testserver/mcp/auth",
        }

        # 2. GET /api/oauth/authorize issues HttpOnly tf_oauth_login_nonce cookie
        r_get = client.get("/api/oauth/authorize", params=auth_params)
        assert r_get.status_code == 200
        set_cookie = r_get.headers.get("set-cookie", "")
        assert "tf_oauth_login_nonce=" in set_cookie
        assert "httponly" in set_cookie.lower()
        csrf_tok = _extract_hidden_input(r_get.text, "csrf_token")
        saved_nonce_cookie = client.cookies.get("tf_oauth_login_nonce")
        assert saved_nonce_cookie

        base_login_form = {
            **auth_params,
            "action": "login",
            "csrf_token": csrf_tok,
            "email": "driver@example.com",
            "password": "DriverPass123!",
        }

        # 3a. Cross-site POST /api/oauth/authorize blocked by CookieCSRFMiddleware (even anon!)
        r_xsite = client.post(
            "/api/oauth/authorize",
            data=base_login_form,
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert r_xsite.status_code == 403

        # 3b. Missing login nonce cookie rejects valid csrf_token (blocks pre-login CSRF forgery)
        client.cookies.clear()
        r_no_cookie = client.post("/api/oauth/authorize", data=base_login_form)
        assert r_no_cookie.status_code == 403

        # 3c. Mismatched login nonce cookie also rejected
        client.cookies.set("tf_oauth_login_nonce", "forged-different-nonce-value")
        r_wrong_cookie = client.post("/api/oauth/authorize", data=base_login_form)
        assert r_wrong_cookie.status_code == 403

        # 3d. Matching cookie + valid csrf_token succeeds, sets session cookie & clears login nonce
        client.cookies.set("tf_oauth_login_nonce", saved_nonce_cookie)
        r_login_ok = client.post("/api/oauth/authorize", data=base_login_form)
        assert r_login_ok.status_code == 200, r_login_ok.text
        assert "tf_session=" in (r_login_ok.headers.get("set-cookie") or "")
        assert 'name="decision"' in r_login_ok.text

        # 4. SEC-M2: Non-string JSON parameters return HTTP 400 invalid_request (never 500)
        for endpoint, bad_payload in (
            ("/api/oauth/token", {"grant_type": "authorization_code", "client_id": 12345}),
            ("/api/oauth/token", {"grant_type": "authorization_code", "code": ["list-not-str"]}),
            ("/api/oauth/token", {"grant_type": "refresh_token", "refresh_token": {"nested": 1}}),
            ("/api/oauth/revoke", {"token": 99999}),
            ("/api/oauth/revoke", ["not", "an", "object"]),
        ):
            r_bad = client.post(endpoint, json=bad_payload)
            assert r_bad.status_code == 400, f"{endpoint} {bad_payload}: got {r_bad.status_code}"
            assert r_bad.json()["error"] == "invalid_request"

        # Malformed JSON body -> HTTP 400 invalid_request
        r_malformed = client.post(
            "/api/oauth/token",
            content=b"{not-valid-json",
            headers={"Content-Type": "application/json"},
        )
        assert r_malformed.status_code == 400
        assert r_malformed.json()["error"] == "invalid_request"


# ---------------------------------------------------------------------------
# SEC-H6: MCP JSON Schema Validation (`validate_input=True`) & Runtime Bounds
# ---------------------------------------------------------------------------


def test_sec_h6_mcp_schema_validation_and_runtime_bounds() -> None:
    cfg = Settings(
        enable_mcp=True,
        mcp_auth_mode="hybrid",
        rate_limit_enabled=False,
    )
    app = create_app(settings=cfg)

    mcp_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    def _rpc_call_tool(client: TestClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        resp = client.post(
            "/mcp",
            headers=mcp_headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    with TestClient(app) as client:
        # Wrong type (string instead of integer) rejected by MCP SDK validate_input=True
        bad_limit = _rpc_call_tool(client, "search_trails", {"limit": "not-an-integer"})
        assert bad_limit["result"]["isError"] is True

        # Exceeding maxLength on query rejected before touching PostGIS
        long_query = _rpc_call_tool(client, "search_trails", {"query": "x" * 201})
        assert long_query["result"]["isError"] is True

        # Exceeding maxItems on poi_types rejected
        too_many_pois = _rpc_call_tool(
            client,
            "search_trails",
            {"poi_types": [f"type_{i}" for i in range(11)]},
        )
        assert too_many_pois["result"]["isError"] is True

        # Out-of-range snowline_m rejected
        bad_snowline = _rpc_call_tool(
            client,
            "inspect_track",
            {"track_id": "trk-1", "snowline_m": 9999},
        )
        assert bad_snowline["result"]["isError"] is True

        # > 100 waypoints rejected
        many_wps = [
            {"name": f"WP{i}", "lon": 146.5, "lat": -37.1}
            for i in range(101)
        ]
        too_many_wps = _rpc_call_tool(
            client,
            "plan_route",
            {"track_ids": ["trk-1"], "waypoints": many_wps},
        )
        assert too_many_wps["result"]["isError"] is True

    # Direct runtime guards reject NaN / Inf & huge inputs cleanly with isError=True
    nan_res = call_mcp_tool(
        "search_trails",
        {"near_lat": float("nan"), "near_lon": 146.5, "radius_km": 20.0},
    )
    assert nan_res.isError is True

    inf_res = call_mcp_tool(
        "find_pois_near",
        {"lat": -37.1, "lon": float("inf"), "radius_km": 10.0},
    )
    assert inf_res.isError is True


def test_cli_create_admin_password_stdin(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json
    import sys

    from trailforge.auth.cli import main as auth_cli_main
    from trailforge.auth.security import verify_password

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE users (
                    id CHAR(32) PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role VARCHAR(16) NOT NULL DEFAULT 'member',
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    token_version INTEGER NOT NULL DEFAULT 1,
                    disabled_at DATETIME,
                    invited_by_code VARCHAR(64),
                    ui_preferences JSON NOT NULL DEFAULT '{}',
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(sys, "stdin", io.StringIO("StdinAdminPass123!\n"))
    with session_local() as db:
        rc = auth_cli_main(
            [
                "create-admin",
                "--email",
                "stdin-admin@example.com",
                "--name",
                "Stdin Admin",
                "--password-stdin",
                "--json",
            ],
            session=db,
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["email"] == "stdin-admin@example.com"
        assert payload["role"] == "admin"

        stored = db.query(User).filter(User.email == "stdin-admin@example.com").one()
        assert verify_password("StdinAdminPass123!", stored.password_hash) is True

