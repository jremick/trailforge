"""Offline unit & PostGIS integration tests for Edge Guard, quotas, telemetry & R2 sync."""

from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import os
import struct
import subprocess
import sys
import uuid
import zlib
from collections import OrderedDict
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from starlette.datastructures import Headers

from trailforge.api.health import get_health_checker
from trailforge.api.main import create_app
from trailforge.api.middleware import (
    MAX_BODY_API_DEFAULT_BYTES,
    MAX_BODY_REPORTS_BYTES,
    MAX_BODY_TRIPS_BYTES,
    BodySizeLimitMiddleware,
    SlidingWindowRateLimiter,
    UsageAndRateLimitMiddleware,
    UsageMetricsStore,
    check_upload_quota,
    classify_route_group,
    extract_client_ip_from_headers,
    flush_usage_metrics,
    get_live_usage_snapshot,
    reset_rate_limiter,
    reset_usage_metrics,
    resolve_body_size_limit,
)
from trailforge.api.tiles import get_tiles_dir
from trailforge.config import Settings, get_settings
from trailforge.db.session import DbHealthStatus, get_db
from trailforge.packs import r2_sync
from trailforge.reports.photos import (
    PhotoStorageQuotaError,
    get_directory_size_bytes,
    save_report_photo,
)


def _make_valid_png_bytes(payload_pad: int = 0) -> bytes:
    """Build a valid RGB PNG image in memory (using real pixel rows when padded)."""

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    width = 32 if payload_pad > 0 else 1
    height = max(1, min(64, (payload_pad // 32) + 8)) if payload_pad > 0 else 1
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.append((x * 37 + y * 91 + 17) & 0xFF)
            rows.append((x * 73 + y * 29 + 53) & 0xFF)
            rows.append((x * 19 + y * 67 + 97) & 0xFF)
    idat = zlib.compress(bytes(rows))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


def _make_healthy_app(
    settings: Settings | None = None,
    *,
    tiles_dir: Path | None = None,
) -> Any:
    app = create_app(settings=settings)
    app.dependency_overrides[get_health_checker] = lambda: (
        lambda: DbHealthStatus(
            db_ok=True,
            postgis_version="3.4 USE_GEOS=1 USE_PROJ=1",
            schema_revision="0004_public_access_control",
        )
    )
    if tiles_dir is not None:
        app.dependency_overrides[get_tiles_dir] = lambda: tiles_dir
    return app


# -----------------------------------------------------------------------------
# 1. Configuration Fields & Env Aliases
# -----------------------------------------------------------------------------


def test_hardening_and_r2_settings_defaults_aliases_and_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = Settings()
    assert defaults.registration_mode == "open"
    assert defaults.demo_banner == ""
    assert defaults.rate_limit_enabled is True
    assert defaults.rate_limit_read_per_min == 180
    assert defaults.rate_limit_auth_per_min == 12
    assert defaults.rate_limit_upload_per_day == 25
    assert defaults.uploads_max_total_mb == 500
    assert defaults.uploads_max_total_bytes == 500 * 1024 * 1024
    assert defaults.trust_proxy_headers is False
    assert defaults.trust_cloudflare_headers is False
    assert "127.0.0.0/8" in defaults.trusted_proxy_cidrs
    assert defaults.tiles_public_base_url == ""
    assert defaults.packs_public_base_url == ""
    assert defaults.r2_endpoint_url == ""
    assert defaults.r2_bucket == ""
    assert defaults.r2_access_key_id == ""
    assert defaults.r2_secret_access_key == ""

    monkeypatch.setenv("TRAILFORGE_REGISTRATION_MODE", "invite_only")
    monkeypatch.setenv("TRAILFORGE_DEMO_BANNER", "Public demo instance")
    monkeypatch.setenv("TRAILFORGE_RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("TRAILFORGE_RATE_LIMIT_READ_PER_MIN", "90")
    monkeypatch.setenv("TRAILFORGE_RATE_LIMIT_AUTH_PER_MIN", "6")
    monkeypatch.setenv("TRAILFORGE_RATE_LIMIT_UPLOAD_PER_DAY", "10")
    monkeypatch.setenv("TRAILFORGE_UPLOADS_MAX_TOTAL_MB", "250")
    monkeypatch.setenv("TRAILFORGE_TRUST_PROXY_HEADERS", "false")
    monkeypatch.setenv("TILES_PUBLIC_BASE_URL", "https://tiles.example.com")
    monkeypatch.setenv("PACKS_PUBLIC_BASE_URL", "https://packs.example.com")
    monkeypatch.setenv("TRAILFORGE_R2_ENDPOINT_URL", "https://acct.r2.example.com")
    monkeypatch.setenv("TRAILFORGE_R2_BUCKET", "trailforge-public")
    monkeypatch.setenv("TRAILFORGE_R2_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("TRAILFORGE_R2_SECRET_ACCESS_KEY", "test-secret-key")

    cfg = Settings()
    assert cfg.registration_mode == "invite_only"
    assert cfg.demo_banner == "Public demo instance"
    assert cfg.rate_limit_enabled is False
    assert cfg.rate_limit_read_per_min == 90
    assert cfg.rate_limit_auth_per_min == 6
    assert cfg.rate_limit_upload_per_day == 10
    assert cfg.uploads_max_total_mb == 250
    assert cfg.trust_proxy_headers is False
    assert cfg.tiles_public_base_url == "https://tiles.example.com"
    assert cfg.packs_public_base_url == "https://packs.example.com"
    assert cfg.r2_endpoint_url == "https://acct.r2.example.com"
    assert cfg.r2_bucket == "trailforge-public"
    assert cfg.r2_access_key_id == "test-access-key"
    assert cfg.r2_secret_access_key == "test-secret-key"

    with pytest.raises(ValidationError):
        Settings(rate_limit_read_per_min=5)
    with pytest.raises(ValidationError):
        Settings(rate_limit_auth_per_min=2)
    with pytest.raises(ValidationError):
        Settings(rate_limit_upload_per_day=0)
    with pytest.raises(ValidationError):
        Settings(uploads_max_total_mb=5)


# -----------------------------------------------------------------------------
# 1b. Security Audit Fixes (H-1, H-2, H-3, M-3, M-5, L-4) — Exploit-First Tests
# -----------------------------------------------------------------------------


_STRONG_AUTH_SECRET = "a" * 64
_STRONG_PRIVACY_SECRET = "b" * 64


async def _run_asgi_request(
    app: Any,
    *,
    method: str = "GET",
    path: str = "/api/health",
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] = ("203.0.113.5", 54321),
    chunks: list[bytes] | None = None,
    scheme: str = "http",
) -> tuple[int, dict[str, str], bytes]:
    """Execute a single raw ASGI HTTP request with controllable peer IP and body chunks."""
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": scheme,
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers or [],
        "client": client,
        "server": ("api.example.com", 443 if scheme == "https" else 80),
    }
    body_chunks = list(chunks) if chunks is not None else [b""]
    chunk_iter = iter(body_chunks)

    async def receive() -> dict[str, Any]:
        try:
            data = next(chunk_iter)
            more = True
        except StopIteration:
            data = b""
            more = False
        return {"type": "http.request", "body": data, "more_body": more}

    status_code = 500
    resp_headers: dict[str, str] = {}
    body_parts: list[bytes] = []

    async def send(message: dict[str, Any]) -> None:
        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = int(message["status"])
            for k, v in message.get("headers", []):
                resp_headers[k.decode("latin-1").lower()] = v.decode("latin-1")
        elif message["type"] == "http.response.body":
            body_parts.append(message.get("body") or b"")

    await app(scope, receive, send)
    return status_code, resp_headers, b"".join(body_parts)


def test_h1_ip_validation_spoofed_testclient_bypass_trailing_slash_and_o1_lru(
    tmp_path: Path,
) -> None:
    # 1. Invalid header IPs (including "testclient" and garbage) fall back to raw peer IP
    assert (
        extract_client_ip_from_headers(
            Headers({"CF-Connecting-IP": "testclient"}),
            client_host="10.0.0.1",
            trust_proxy_headers=True,
            trust_cloudflare_headers=True,
        )
        == "10.0.0.1"
    )
    assert (
        extract_client_ip_from_headers(
            Headers({"CF-Connecting-IP": "not-an-ip", "X-Forwarded-For": "198.51.100.1"}),
            client_host="203.0.113.5",
            trust_proxy_headers=True,
            trust_cloudflare_headers=True,
        )
        == "203.0.113.5"
    )
    assert (
        extract_client_ip_from_headers(
            Headers({"X-Forwarded-For": "testclient, 203.0.113.5"}),
            client_host="198.51.100.44",
            trust_proxy_headers=True,
        )
        == "198.51.100.44"
    )
    assert (
        extract_client_ip_from_headers(
            Headers({"CF-Connecting-IP": "2001:0db8:0000::0001"}),
            client_host="10.0.0.1",
            trust_proxy_headers=True,
            trust_cloudflare_headers=True,
        )
        == "2001:db8::1"
    )

    # 2. Exploit proof: CF-Connecting-IP: testclient STILL hits 429 on TestClient!
    cfg = Settings(
        rate_limit_enabled=True,
        rate_limit_read_per_min=10,
        rate_limit_auth_per_min=3,
        trust_proxy_headers=True,
        trust_cloudflare_headers=True,
    )
    app = _make_healthy_app(settings=cfg, tiles_dir=tmp_path)
    with TestClient(app) as client:
        statuses = [
            client.post(
                "/api/auth/login",
                headers={"CF-Connecting-IP": "testclient"},
                json={},
            ).status_code
            for _ in range(5)
        ]
        assert statuses == [422, 422, 422, 429, 429]

        # Same exploit via X-Forwarded-For: testclient also hits 429
        reset_rate_limiter(app)
        xff_statuses = [
            client.post(
                "/api/auth/login",
                headers={"X-Forwarded-For": "testclient, 203.0.113.5"},
                json={},
            ).status_code
            for _ in range(5)
        ]
        assert xff_statuses == [422, 422, 422, 429, 429]

        # 3. Trailing-slash normalization: POST /api/auth/login/ hits the 3/min auth bucket!
        reset_rate_limiter(app)
        slash_statuses = [
            client.post(
                "/api/auth/login/",
                headers={"CF-Connecting-IP": "198.51.100.77"},
                json={},
                follow_redirects=False,
            ).status_code
            for _ in range(5)
        ]
        assert slash_statuses[:3] == [307, 307, 307]
        assert slash_statuses[3:] == [429, 429]
    app.dependency_overrides.clear()

    # 4. Raw ASGI real peer (203.0.113.5) spoofing CF-Connecting-IP: testclient shares peer bucket
    async def ok_app(scope: Any, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    mw = UsageAndRateLimitMiddleware(
        ok_app,
        settings=cfg,
        rate_limiter=SlidingWindowRateLimiter(),
    )
    raw_statuses: list[int] = []
    for _ in range(5):
        code, _, _ = asyncio.run(
            _run_asgi_request(
                mw,
                method="POST",
                path="/api/auth/login/",
                headers=[(b"cf-connecting-ip", b"testclient")],
                client=("203.0.113.5", 54321),
            )
        )
        raw_statuses.append(code)
    assert raw_statuses == [200, 200, 200, 429, 429]

    # 5. O(1) OrderedDict LRU eviction under flood (preserves recently used key, strictly <= max)
    now = [1000.0]
    lru_limiter = SlidingWindowRateLimiter(clock=lambda: now[0], max_keys=64)
    assert isinstance(lru_limiter._buckets, OrderedDict)
    assert not hasattr(lru_limiter, "_prune_locked")

    for i in range(64):
        now[0] += 0.001
        lru_limiter.check(f"api:198.51.100.{i}", limit=5, window_s=60.0)
    # Touch key 0 so it moves to MRU; key 1 becomes the LRU victim
    now[0] += 0.001
    lru_limiter.check("api:198.51.100.0", limit=5, window_s=60.0)
    # Insert 1 key beyond max_keys -> evicts key 1, keeps key 0
    now[0] += 0.001
    lru_limiter.check("api:198.51.100.999", limit=5, window_s=60.0)
    assert len(lru_limiter._buckets) == 64
    assert "api:198.51.100.0" in lru_limiter._buckets
    assert "api:198.51.100.1" not in lru_limiter._buckets

    # Flood 2,000 distinct keys in another partition -> each partition strictly bounded at 64
    # AND flood partition cannot evict the isolated api partition (SEC-H1)
    for i in range(2_000):
        now[0] += 0.0001
        lru_limiter.check(f"flood:{i}", limit=5, window_s=60.0)
    assert len(lru_limiter._partitions["flood"]) == 64
    assert len(lru_limiter._partitions["api"]) == 64
    assert "api:198.51.100.0" in lru_limiter._buckets


def test_h1_origin_shared_secret_blocks_direct_origin_and_allows_health(
    tmp_path: Path,
) -> None:
    secret = "origin-edge-shared-secret-0123456789abcdef"
    cfg = Settings(origin_shared_secret=secret)
    app = _make_healthy_app(settings=cfg, tiles_dir=tmp_path)

    with TestClient(app) as client:
        # Missing secret -> 403 on API routes AND root
        r_missing = client.get("/api/tiles/manifest.json")
        assert r_missing.status_code == 403
        assert r_missing.json() == {"detail": "Direct origin access forbidden"}
        assert r_missing.headers.get("x-content-type-options") == "nosniff"
        assert r_missing.headers.get("x-frame-options") == "DENY"

        r_root = client.get("/")
        assert r_root.status_code == 403

        # Wrong secret -> 403
        assert (
            client.get(
                "/api/tiles/manifest.json",
                headers={"X-TrailForge-Origin-Secret": "wrong-secret"},
            ).status_code
            == 403
        )

        # Non-ASCII raw header bytes -> 403 (never 500 TypeError from compare_digest)
        code_non_ascii, _, _ = asyncio.run(
            _run_asgi_request(
                app,
                method="GET",
                path="/api/tiles/manifest.json",
                headers=[(b"x-trailforge-origin-secret", b"invalid-\xe9-secret")],
            )
        )
        assert code_non_ascii == 403

        # Health endpoints remain exempt for Railway healthchecks
        assert client.get("/api/health").status_code == 200
        assert client.get("/healthz").status_code == 404  # bypassed secret check (not 403)

        # Correct shared secret -> 200
        r_ok = client.get(
            "/api/tiles/manifest.json",
            headers={"X-TrailForge-Origin-Secret": secret},
        )
        assert r_ok.status_code == 200
    app.dependency_overrides.clear()


def test_h2_create_app_fails_closed_on_prod_default_or_short_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("TRAILFORGE_ENABLE_DOCS", raising=False)

    # Local dev mode allows dev defaults (not production mode)
    dev_cfg = Settings()
    assert dev_cfg.is_production_mode is False
    assert create_app(settings=dev_cfg) is not None

    # Production mode via auth_cookie_secure=True fails closed with default secrets
    with pytest.raises(RuntimeError, match="AUTH_SECRET_KEY|secret"):
        create_app(settings=Settings(auth_cookie_secure=True))

    # Production mode via registration_mode=invite_only fails closed with default secrets
    with pytest.raises(RuntimeError, match="secret"):
        create_app(settings=Settings(registration_mode="invite_only"))

    # Production mode via short (<32 chars) secret fails closed
    with pytest.raises(RuntimeError, match="secret"):
        create_app(
            settings=Settings(
                auth_cookie_secure=True,
                auth_secret_key="short-secret",
                privacy_secret_key=_STRONG_PRIVACY_SECRET,
            )
        )

    # Missing strong privacy secret also fails closed
    with pytest.raises(RuntimeError, match="secret"):
        create_app(
            settings=Settings(
                auth_cookie_secure=True,
                auth_secret_key=_STRONG_AUTH_SECRET,
            )
        )

    # RAILWAY_ENVIRONMENT triggers production mode even if registration_mode=open
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert Settings().is_production_mode is True
    with pytest.raises(RuntimeError, match="secret"):
        create_app(settings=Settings())

    # Missing TRAILFORGE_PUBLIC_BASE_URL with MCP enabled fails closed in production mode
    with pytest.raises(RuntimeError, match="TRAILFORGE_PUBLIC_BASE_URL"):
        create_app(
            settings=Settings(
                auth_cookie_secure=True,
                registration_mode="invite_only",
                auth_secret_key=_STRONG_AUTH_SECRET,
                privacy_secret_key=_STRONG_PRIVACY_SECRET,
                enable_mcp=True,
                public_base_url="",
            )
        )

    # Wildcard CORS_ORIGINS='*' with credentialed cookies fails closed in production mode
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        create_app(
            settings=Settings(
                auth_cookie_secure=True,
                auth_secret_key=_STRONG_AUTH_SECRET,
                privacy_secret_key=_STRONG_PRIVACY_SECRET,
                public_base_url="https://trails.example.com",
                cors_origins="*",
            )
        )

    # Strong >= 32-char secrets + public_base_url succeed cleanly in production mode
    prod_cfg = Settings(
        auth_cookie_secure=True,
        registration_mode="invite_only",
        auth_secret_key=_STRONG_AUTH_SECRET,
        privacy_secret_key=_STRONG_PRIVACY_SECRET,
        public_base_url="https://trails.example.com",
    )
    assert prod_cfg.is_production_mode is True
    assert create_app(settings=prod_cfg) is not None


def test_h3_streaming_body_size_limit_middleware_rejects_oversized_413() -> None:
    # 1. Route tier caps
    assert resolve_body_size_limit("/api/trips/upload") == MAX_BODY_TRIPS_BYTES == 16 * 1024 * 1024
    assert resolve_body_size_limit("/api/trips/mobile-sync") == 16 * 1024 * 1024
    assert resolve_body_size_limit("/api/reports") == MAX_BODY_REPORTS_BYTES == 6 * 1024 * 1024
    assert resolve_body_size_limit("/api/reports/") == 6 * 1024 * 1024
    assert resolve_body_size_limit("/api/auth/login") == MAX_BODY_API_DEFAULT_BYTES == 1024 * 1024
    assert resolve_body_size_limit("/api/routes/plan") == 1024 * 1024
    assert resolve_body_size_limit("/docs") is None

    # 2. Content-Length fast-fail rejects BEFORE downstream handler executes
    handler_called = False

    async def reader_app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal handler_called
        handler_called = True
        total = 0
        while True:
            msg = await receive()
            total += len(msg.get("body") or b"")
            if not msg.get("more_body", False):
                break
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": f'{{"bytes":{total}}}'.encode()})

    mw = BodySizeLimitMiddleware(
        reader_app,
        trips_max_bytes=4096,
        reports_max_bytes=2048,
        default_api_max_bytes=1024,
    )

    code_cl, _, body_cl = asyncio.run(
        _run_asgi_request(
            mw,
            method="POST",
            path="/api/auth/login",
            headers=[(b"content-length", b"1025")],
            chunks=[b"x" * 1025],
        )
    )
    assert code_cl == 413
    assert handler_called is False
    assert "Payload Too Large" in json.loads(body_cl)["detail"]

    # 3. Chunked stream WITHOUT Content-Length aborts mid-stream with 413
    code_stream, _, body_stream = asyncio.run(
        _run_asgi_request(
            mw,
            method="POST",
            path="/api/auth/login",
            headers=[],  # No Content-Length (chunked transfer)
            chunks=[b"a" * 512, b"b" * 512, b"c" * 10],
        )
    )
    assert code_stream == 413
    assert "Payload Too Large" in json.loads(body_stream)["detail"]

    # 4. Reports & trips tiers honor their higher limits
    code_rep_ok, _, _ = asyncio.run(
        _run_asgi_request(
            mw,
            method="POST",
            path="/api/reports",
            headers=[],
            chunks=[b"r" * 2000],
        )
    )
    assert code_rep_ok == 200

    code_rep_big, _, _ = asyncio.run(
        _run_asgi_request(
            mw,
            method="POST",
            path="/api/reports",
            headers=[],
            chunks=[b"r" * 1500, b"r" * 1500],
        )
    )
    assert code_rep_big == 413

    code_trip_ok, _, _ = asyncio.run(
        _run_asgi_request(
            mw,
            method="POST",
            path="/api/trips/mobile-sync",
            headers=[],
            chunks=[b"t" * 4000],
        )
    )
    assert code_trip_ok == 200

    code_trip_big, _, _ = asyncio.run(
        _run_asgi_request(
            mw,
            method="POST",
            path="/api/trips/mobile-sync",
            headers=[],
            chunks=[b"t" * 3000, b"t" * 2000],
        )
    )
    assert code_trip_big == 413

    # 5. End-to-end FastAPI app rejects > 1 MiB chunked POST to /api/auth/login with 413
    app = _make_healthy_app()
    large_chunks = [
        b'{"email":"a@b.co","password":"' + (b"x" * (600 * 1024)),
        b"y" * (600 * 1024),
    ]
    code_e2e, hdrs_e2e, body_e2e = asyncio.run(
        _run_asgi_request(
            app,
            method="POST",
            path="/api/auth/login",
            headers=[(b"content-type", b"application/json")],
            chunks=large_chunks,
        )
    )
    assert code_e2e == 413
    assert "Payload Too Large" in json.loads(body_e2e)["detail"]
    assert hdrs_e2e.get("x-content-type-options") == "nosniff"
    app.dependency_overrides.clear()
    del large_chunks, app
    gc.collect()


def test_m3_global_security_headers_and_conditional_hsts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    dev_app = _make_healthy_app(tiles_dir=tmp_path)
    with TestClient(dev_app) as client:
        r_http = client.get("/api/health")
        assert r_http.status_code == 200
        assert r_http.headers["x-content-type-options"] == "nosniff"
        assert r_http.headers["x-frame-options"] == "DENY"
        assert r_http.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert (
            r_http.headers["permissions-policy"]
            == "camera=(), microphone=(), geolocation=(self)"
        )
        assert "strict-transport-security" not in r_http.headers

        # X-Forwarded-Proto: https enables HSTS
        r_xfp = client.get("/api/health", headers={"X-Forwarded-Proto": "https"})
        assert r_xfp.status_code == 200
        assert (
            r_xfp.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
        )
    dev_app.dependency_overrides.clear()

    # auth_cookie_secure=True always emits HSTS
    prod_cfg = Settings(
        auth_cookie_secure=True,
        auth_secret_key=_STRONG_AUTH_SECRET,
        privacy_secret_key=_STRONG_PRIVACY_SECRET,
        public_base_url="https://trails.example.com",
    )
    prod_app = _make_healthy_app(settings=prod_cfg, tiles_dir=tmp_path)
    with TestClient(prod_app) as client:
        r_prod = client.get("/api/health")
        assert r_prod.status_code == 200
        assert (
            r_prod.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
        )
    prod_app.dependency_overrides.clear()


def test_m5_cookie_csrf_guard_blocks_cross_site_and_allows_legitimate_clients(
    tmp_path: Path,
) -> None:
    cfg = Settings(cors_origins="https://trails.example.com")
    app = _make_healthy_app(settings=cfg, tiles_dir=tmp_path)

    @app.post("/api/_test_csrf_target")
    def _csrf_target() -> dict[str, bool]:
        return {"mutated": True}

    with TestClient(app) as client:
        cookie_hdr = {"Cookie": "tf_session=fake-session-token"}

        # 1. Cross-site & same-site (sibling subdomain) browser POST with cookie -> 403
        r_cross = client.post(
            "/api/_test_csrf_target",
            headers={**cookie_hdr, "Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        )
        assert r_cross.status_code == 403
        assert r_cross.json() == {"detail": "Cross-site request blocked"}

        r_sibling = client.post(
            "/api/_test_csrf_target",
            headers={
                **cookie_hdr,
                "Sec-Fetch-Site": "same-site",
                "Origin": "https://blog.example.com",
            },
        )
        assert r_sibling.status_code == 403

        # 2. Missing Sec-Fetch-Site with untrusted Origin or Origin: null -> 403
        r_bad_origin = client.post(
            "/api/_test_csrf_target",
            headers={**cookie_hdr, "Origin": "https://evil.example"},
        )
        assert r_bad_origin.status_code == 403

        r_null_origin = client.post(
            "/api/_test_csrf_target",
            headers={**cookie_hdr, "Origin": "null"},
        )
        assert r_null_origin.status_code == 403

        # 3. Same-origin / none Sec-Fetch-Site allowed
        assert (
            client.post(
                "/api/_test_csrf_target",
                headers={**cookie_hdr, "Sec-Fetch-Site": "same-origin"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/_test_csrf_target",
                headers={**cookie_hdr, "Sec-Fetch-Site": "none"},
            ).status_code
            == 200
        )

        # 4. Allowlisted CORS Origin or matching request host allowed when Sec-Fetch-Site absent
        assert (
            client.post(
                "/api/_test_csrf_target",
                headers={**cookie_hdr, "Origin": "https://trails.example.com"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/_test_csrf_target",
                headers={**cookie_hdr, "Origin": "http://testserver"},
            ).status_code
            == 200
        )

        # 5. Non-browser client without Origin or Sec-Fetch-Site continues working
        assert client.post("/api/_test_csrf_target", headers=cookie_hdr).status_code == 200

        # 6. Bearer Authorization bypasses cookie CSRF guard
        assert (
            client.post(
                "/api/_test_csrf_target",
                headers={
                    **cookie_hdr,
                    "Authorization": "Bearer some-jwt-token",
                    "Sec-Fetch-Site": "cross-site",
                },
            ).status_code
            == 200
        )

        # 7. Requests without tf_session cookie & safe GET requests are never blocked
        assert (
            client.post(
                "/api/_test_csrf_target",
                headers={"Sec-Fetch-Site": "cross-site"},
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/api/health",
                headers={**cookie_hdr, "Sec-Fetch-Site": "cross-site"},
            ).status_code
            == 200
        )
    app.dependency_overrides.clear()


def test_l4_openapi_docs_disabled_in_production_mode_unless_explicitly_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.setenv("TRAILFORGE_ENABLE_DOCS", "")
    dev_cfg = Settings()
    assert dev_cfg.enable_docs is None
    assert dev_cfg.docs_enabled is True

    with TestClient(create_app(settings=dev_cfg)) as dev_client:
        assert dev_client.get("/docs").status_code == 200
        assert dev_client.get("/openapi.json").status_code == 200
        assert dev_client.get("/").json().get("docs") == "/docs"

    # Production mode disables /docs, /redoc, /openapi.json by default
    prod_cfg = Settings(
        auth_cookie_secure=True,
        auth_secret_key=_STRONG_AUTH_SECRET,
        privacy_secret_key=_STRONG_PRIVACY_SECRET,
        public_base_url="https://trails.example.com",
    )
    assert prod_cfg.docs_enabled is False
    with TestClient(create_app(settings=prod_cfg)) as prod_client:
        assert prod_client.get("/docs").status_code == 404
        assert prod_client.get("/redoc").status_code == 404
        assert prod_client.get("/openapi.json").status_code == 404
        assert "docs" not in prod_client.get("/").json()

    # Explicit TRAILFORGE_ENABLE_DOCS=true re-enables docs even in production mode
    prod_docs_cfg = Settings(
        auth_cookie_secure=True,
        auth_secret_key=_STRONG_AUTH_SECRET,
        privacy_secret_key=_STRONG_PRIVACY_SECRET,
        public_base_url="https://trails.example.com",
        enable_docs=True,
    )
    assert prod_docs_cfg.docs_enabled is True
    with TestClient(create_app(settings=prod_docs_cfg)) as prod_docs_client:
        assert prod_docs_client.get("/docs").status_code == 200
        assert prod_docs_client.get("/openapi.json").status_code == 200


# -----------------------------------------------------------------------------
# 2. Client IP Extraction, Sliding Window Limiter & HTTP 429 Middleware
# -----------------------------------------------------------------------------


def test_extract_client_ip_and_route_group_classification() -> None:
    hdrs = Headers(
        {
            "CF-Connecting-IP": "198.51.100.11",
            "X-Forwarded-For": "203.0.113.5, 10.0.0.1",
        }
    )
    # Secure default: trust_proxy_headers=False returns direct peer IP
    assert extract_client_ip_from_headers(hdrs, client_host="127.0.0.1") == "127.0.0.1"
    # Trusted proxy + trusted Cloudflare header honors CF-Connecting-IP
    assert (
        extract_client_ip_from_headers(
            hdrs,
            client_host="127.0.0.1",
            trust_proxy_headers=True,
            trust_cloudflare_headers=True,
        )
        == "198.51.100.11"
    )
    # Trusted proxy WITHOUT Cloudflare trust ignores CF-Connecting-IP and strips trusted XFF hops
    assert (
        extract_client_ip_from_headers(
            hdrs,
            client_host="127.0.0.1",
            trust_proxy_headers=True,
            trust_cloudflare_headers=False,
        )
        == "203.0.113.5"
    )
    assert (
        extract_client_ip_from_headers(
            hdrs, client_host="127.0.0.1", trust_proxy_headers=False
        )
        == "127.0.0.1"
    )

    # Right-to-left XFF parsing prevents left-side spoofing by returning rightmost untrusted hop
    xff_only = Headers({"X-Forwarded-For": " 203.0.113.42 , 192.0.2.9 "})
    assert (
        extract_client_ip_from_headers(
            xff_only, client_host="10.0.0.2", trust_proxy_headers=True
        )
        == "192.0.2.9"
    )
    assert (
        extract_client_ip_from_headers(Headers({}), client_host="192.0.2.77") == "192.0.2.77"
    )
    assert extract_client_ip_from_headers(Headers({}), client_host=None) == "unknown"

    assert classify_route_group("/api/tracks") == "tracks"
    assert classify_route_group("/api/segments/seg-1") == "tracks"
    assert classify_route_group("/api/tiles/manifest.json") == "tiles"
    assert classify_route_group("/api/packs/australian-alps/manifest.json") == "packs"
    assert classify_route_group("/api/routes/plan") == "routes"
    assert classify_route_group("/api/trips/upload") == "trips"
    assert classify_route_group("/api/telemetry/unmapped") == "trips"
    assert classify_route_group("/api/reports/geojson") == "reports"
    assert classify_route_group("/api/closures") == "closures"
    assert classify_route_group("/api/grading/recompute") == "grading"
    assert classify_route_group("/api/auth/login") == "auth"
    assert classify_route_group("/api/search") == "search"
    assert classify_route_group("/api/admin/stats") == "admin"
    assert classify_route_group("/api/vehicles") == "other"
    assert classify_route_group("/healthz") == "other"


def test_sliding_window_limiter_expiry_and_bounded_memory() -> None:
    now = [1000.0]
    limiter = SlidingWindowRateLimiter(clock=lambda: now[0], max_keys=64)

    r1 = limiter.check("api:198.51.100.1", limit=2, window_s=60.0)
    r2 = limiter.check("api:198.51.100.1", limit=2, window_s=60.0)
    r3 = limiter.check("api:198.51.100.1", limit=2, window_s=60.0)
    assert r1.allowed is True and r1.remaining == 1
    assert r2.allowed is True and r2.remaining == 0
    assert r3.allowed is False and r3.remaining == 0 and r3.retry_after_s == 60

    # Advance past window -> allowed again
    now[0] += 60.5
    r4 = limiter.check("api:198.51.100.1", limit=2, window_s=60.0)
    assert r4.allowed is True and r4.remaining == 1

    # Flood distinct keys -> stays strictly bounded at max_keys
    for idx in range(150):
        now[0] += 0.01
        limiter.check(f"api:198.51.100.{idx}", limit=10, window_s=60.0)
    assert len(limiter._buckets) <= 65


def test_http_rate_limiting_auth_and_read_buckets_429_headers_and_health_bypass(
    tmp_path: Path,
) -> None:
    reset_usage_metrics()
    cfg = Settings(
        rate_limit_enabled=True,
        rate_limit_read_per_min=10,
        rate_limit_auth_per_min=3,
        cors_origins="http://localhost:5173",
        trust_proxy_headers=True,
        trust_cloudflare_headers=True,
    )
    app = _make_healthy_app(settings=cfg, tiles_dir=tmp_path)

    with TestClient(app) as client:
        ip_a = {"CF-Connecting-IP": "198.51.100.10", "Origin": "http://localhost:5173"}
        ip_b = {"X-Forwarded-For": "198.51.100.20, 10.0.0.1"}

        # 1. Read/API bucket allows exactly 10 requests and emits X-RateLimit-* headers
        for idx in range(10):
            resp = client.get("/api/tiles/manifest.json", headers=ip_a)
            assert resp.status_code == 200
            assert resp.headers["x-ratelimit-limit"] == "10"
            assert resp.headers["x-ratelimit-remaining"] == str(9 - idx)

        # 11th request from ip_a -> HTTP 429 + Retry-After + CORS header preserved
        blocked = client.get("/api/tiles/manifest.json", headers=ip_a)
        assert blocked.status_code == 429
        assert blocked.json() == {"detail": "Rate limit exceeded. Please try again shortly."}
        assert int(blocked.headers["retry-after"]) >= 1
        assert blocked.headers["x-ratelimit-limit"] == "10"
        assert blocked.headers["x-ratelimit-remaining"] == "0"
        assert blocked.headers.get("access-control-allow-origin") == "http://localhost:5173"

        # Different IP (via X-Forwarded-For) still succeeds
        other = client.get("/api/tiles/manifest.json", headers=ip_b)
        assert other.status_code == 200
        assert other.headers["x-ratelimit-remaining"] == "9"

        # /api/health and OPTIONS preflight are NEVER rate-limited even for blocked ip_a
        for _ in range(5):
            assert client.get("/api/health", headers=ip_a).status_code == 200
        opt = client.options(
            "/api/tiles/manifest.json",
            headers={
                **ip_a,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert opt.status_code == 200

        # 2. Auth bucket (limit 3/min) enforces tighter limit on POST /api/auth/login
        auth_ip = {"CF-Connecting-IP": "198.51.100.30"}
        for idx in range(3):
            r_auth = client.post("/api/auth/login", headers=auth_ip, json={})
            # Validation fails with 422 (zero DB needed) while still consuming auth tokens
            assert r_auth.status_code == 422
            assert r_auth.headers["x-ratelimit-limit"] == "3"
            assert r_auth.headers["x-ratelimit-remaining"] == str(2 - idx)

        r_auth_blocked = client.post("/api/auth/login", headers=auth_ip, json={})
        assert r_auth_blocked.status_code == 429
        assert int(r_auth_blocked.headers["retry-after"]) >= 1
        assert r_auth_blocked.headers["x-ratelimit-limit"] == "3"
        assert r_auth_blocked.headers["x-ratelimit-remaining"] == "0"

        # 3. reset_rate_limiter(app) restores capacity immediately
        reset_rate_limiter(app)
        after_reset = client.get("/api/tiles/manifest.json", headers=ip_a)
        assert after_reset.status_code == 200
        assert after_reset.headers["x-ratelimit-remaining"] == "9"

        # 4. Usage telemetry captured tiles + auth requests, errors, bytes_out, and 429s
        snap = get_live_usage_snapshot()
        assert snap["by_route_group"]["tiles"]["request_count"] == 13
        assert snap["by_route_group"]["tiles"]["rate_limited_count"] == 1
        assert snap["by_route_group"]["tiles"]["error_count"] == 1
        assert snap["by_route_group"]["tiles"]["bytes_out"] > 0
        assert snap["by_route_group"]["auth"]["request_count"] == 4
        assert snap["by_route_group"]["auth"]["rate_limited_count"] == 1
        assert snap["by_route_group"]["auth"]["error_count"] == 4
        assert snap["totals"]["rate_limited_count"] == 2
        assert len(snap["pending_buckets"]) >= 2

    app.dependency_overrides.clear()


def test_rate_limit_disabled_allows_burst_and_upload_quota_helper(tmp_path: Path) -> None:
    cfg = Settings(rate_limit_enabled=False, rate_limit_read_per_min=10)
    app = _make_healthy_app(settings=cfg, tiles_dir=tmp_path)
    with TestClient(app) as client:
        for _ in range(15):
            resp = client.get(
                "/api/tiles/manifest.json",
                headers={"CF-Connecting-IP": "198.51.100.99"},
            )
            assert resp.status_code == 200
            assert "x-ratelimit-limit" not in resp.headers
    app.dependency_overrides.clear()

    # trust_proxy_headers=False ignores spoofed CF-Connecting-IP / X-Forwarded-For headers
    untrusted_cfg = Settings(
        rate_limit_enabled=True,
        rate_limit_read_per_min=10,
        trust_proxy_headers=False,
    )
    untrusted_app = _make_healthy_app(settings=untrusted_cfg, tiles_dir=tmp_path)
    untrusted_app.state.enforce_rate_limit_in_tests = True
    with TestClient(untrusted_app) as client:
        for idx in range(10):
            # Even with rotating spoofed proxy headers, all hit the same client.host bucket
            resp = client.get(
                "/api/tiles/manifest.json",
                headers={"CF-Connecting-IP": f"198.51.100.{idx + 1}"},
            )
            assert resp.status_code == 200
            assert resp.headers["x-ratelimit-remaining"] == str(9 - idx)
        blocked = client.get(
            "/api/tiles/manifest.json",
            headers={"CF-Connecting-IP": "198.51.100.250"},
        )
        assert blocked.status_code == 429
    untrusted_app.dependency_overrides.clear()

    # Daily upload quota helper
    reset_rate_limiter()
    u1 = uuid.uuid4()
    u2 = uuid.uuid4()
    assert check_upload_quota(u1, limit_per_day=2).remaining == 1
    assert check_upload_quota(u1, limit_per_day=2).remaining == 0
    with pytest.raises(HTTPException) as exc_info:
        check_upload_quota(u1, limit_per_day=2)
    assert exc_info.value.status_code == 429
    assert exc_info.value.headers is not None
    assert int(exc_info.value.headers["Retry-After"]) >= 1
    # Distinct user still has full quota
    assert check_upload_quota(u2, limit_per_day=2).remaining == 1
    reset_rate_limiter()


# -----------------------------------------------------------------------------
# 3. Photo Upload Disk Quota Guard
# -----------------------------------------------------------------------------


def test_photo_storage_disk_quota_guard_and_507_handler(tmp_path: Path) -> None:
    png_small = _make_valid_png_bytes(payload_pad=256)
    assert get_directory_size_bytes(tmp_path) == 0

    # Save first photo with generous cap to measure exact stored size (works with EXIF re-encoding)
    saved1 = save_report_photo(png_small, uploads_dir=tmp_path, max_total_bytes=1024 * 1024)
    assert saved1.path.is_file()
    stored_size = saved1.size_bytes
    assert stored_size > 0
    assert get_directory_size_bytes(tmp_path) == stored_size

    # Tight quota allowing exactly two stored copies
    quota = stored_size * 2 + 10
    saved2 = save_report_photo(png_small, uploads_dir=tmp_path, max_total_bytes=quota)
    assert saved2.path.is_file()
    expected_two_size = stored_size + saved2.size_bytes
    assert get_directory_size_bytes(tmp_path) == expected_two_size

    # Symlinks inside uploads dir are ignored by size calculation
    outside = tmp_path.parent / "outside_blob.bin"
    outside.write_bytes(b"0" * 10_000)
    (tmp_path / "ignore_link.bin").symlink_to(outside)
    assert get_directory_size_bytes(tmp_path) == expected_two_size

    # Third photo exceeds quota -> raises PhotoStorageQuotaError with no orphan temp files
    with pytest.raises(PhotoStorageQuotaError):
        save_report_photo(png_small, uploads_dir=tmp_path, max_total_bytes=quota)
    assert [p.name for p in tmp_path.glob("*.tmp")] == []

    # Verify FastAPI exception handler maps PhotoStorageQuotaError -> HTTP 507
    app = _make_healthy_app()

    @app.get("/api/_test_photo_quota_507")
    def _trigger_quota() -> dict[str, str]:
        raise PhotoStorageQuotaError("Disk quota reached")

    with TestClient(app) as client:
        r_507 = client.get("/api/_test_photo_quota_507")
        assert r_507.status_code == 507
        assert "Disk quota reached" in r_507.json()["detail"]
    app.dependency_overrides.clear()


# -----------------------------------------------------------------------------
# 4. Cloudflare R2 Tile Offload Manifest
# -----------------------------------------------------------------------------


def test_tiles_manifest_r2_cdn_offload_and_local_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Local mode (no TILES_PUBLIC_BASE_URL, empty tiles_dir)
    monkeypatch.delenv("TILES_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("TRAILFORGE_TILES_PUBLIC_BASE_URL", raising=False)
    get_settings.cache_clear()

    local_app = _make_healthy_app(tiles_dir=tmp_path)
    with TestClient(local_app) as client:
        r_local = client.get("/api/tiles/manifest.json")
        assert r_local.status_code == 200
        body = r_local.json()
        assert body["cdn_offload"] is False
        assert body["vector_url"] == "/api/tiles/trailforge_vector.pmtiles"
        assert body["terrain_url"] == "/api/tiles/trailforge_terrain.pmtiles"
        assert body["vector_available"] is False
        assert body["terrain_available"] is False
    local_app.dependency_overrides.clear()

    # R2 CDN offload mode (trailing slash normalized; zero local PMTiles required!)
    cdn_cfg = Settings(tiles_public_base_url="https://tiles.example.com/")
    cdn_app = _make_healthy_app(settings=cdn_cfg, tiles_dir=tmp_path)
    with TestClient(cdn_app) as client:
        r_cdn = client.get("/api/tiles/manifest.json")
        assert r_cdn.status_code == 200
        body = r_cdn.json()
        assert body["cdn_offload"] is True
        assert body["vector_url"] == (
            "https://tiles.example.com/trailforge_vector_baseline_weather_v1.pmtiles"
        )
        assert body["terrain_url"] == "https://tiles.example.com/trailforge_terrain.pmtiles"
        assert body["vector_available"] is True
        assert body["terrain_available"] is True
    cdn_app.dependency_overrides.clear()


# -----------------------------------------------------------------------------
# 5. Pure-Python AWS SigV4 + Mocked-HTTP R2 Sync & CLI Dry-Run
# -----------------------------------------------------------------------------


def test_sigv4_signing_determinism_and_no_boto3() -> None:
    assert "boto3" not in sys.modules
    fixed_ts = datetime(2026, 11, 26, 7, 30, 0, tzinfo=UTC)
    headers = r2_sync.sign_s3_v4_request(
        method="PUT",
        url="https://acct.r2.example.com/trailforge-tiles/trailforge_vector.pmtiles",
        access_key_id="AKIAEXAMPLEKEY",
        secret_access_key="SecretExampleKey123",
        region="auto",
        payload_hash=r2_sync.UNSIGNED_PAYLOAD,
        extra_headers={
            "Content-Type": "application/vnd.pmtiles",
            "Cache-Control": "public, max-age=86400",
            "Content-Length": "128",
        },
        now=fixed_ts,
    )
    assert headers["x-amz-date"] == "20261126T073000Z"
    assert headers["x-amz-content-sha256"] == "UNSIGNED-PAYLOAD"
    auth = headers["Authorization"]
    assert auth.startswith(
        "AWS4-HMAC-SHA256 Credential=AKIAEXAMPLEKEY/20261126/auto/s3/aws4_request, "
    )
    assert (
        "SignedHeaders=cache-control;content-length;content-type;host;"
        "x-amz-content-sha256;x-amz-date"
    ) in auth
    sig = auth.rsplit("Signature=", 1)[1]
    assert len(sig) == 64 and all(ch in "0123456789abcdef" for ch in sig)

    # Identical inputs -> identical signature; altered secret -> different signature
    headers_same = r2_sync.sign_s3_v4_request(
        method="PUT",
        url="https://acct.r2.example.com/trailforge-tiles/trailforge_vector.pmtiles",
        access_key_id="AKIAEXAMPLEKEY",
        secret_access_key="SecretExampleKey123",
        region="auto",
        payload_hash=r2_sync.UNSIGNED_PAYLOAD,
        extra_headers={
            "Content-Type": "application/vnd.pmtiles",
            "Cache-Control": "public, max-age=86400",
            "Content-Length": "128",
        },
        now=fixed_ts,
    )
    assert headers_same["Authorization"] == auth

    headers_diff = r2_sync.sign_s3_v4_request(
        method="PUT",
        url="https://acct.r2.example.com/trailforge-tiles/trailforge_vector.pmtiles",
        access_key_id="AKIAEXAMPLEKEY",
        secret_access_key="DifferentSecretKey999",
        region="auto",
        payload_hash=r2_sync.UNSIGNED_PAYLOAD,
        extra_headers={
            "Content-Type": "application/vnd.pmtiles",
            "Cache-Control": "public, max-age=86400",
            "Content-Length": "128",
        },
        now=fixed_ts,
    )
    assert headers_diff["Authorization"] != auth


def test_r2_sync_mock_http_streaming_etag_skip_and_cli_dry_run(tmp_path: Path) -> None:
    tiles_dir = tmp_path / "tiles"
    packs_dir = tmp_path / "packs"
    alps_dir = packs_dir / "australian-alps"
    staging_dir = packs_dir / ".australian-alps.staging-1234"
    tiles_dir.mkdir()
    alps_dir.mkdir(parents=True)
    staging_dir.mkdir(parents=True)

    # Populate synthetic tiles + pack artifacts (+ hidden staging dir that MUST be ignored)
    tile_files = {
        "trailforge_vector.pmtiles": b"PMTilesVEC" + b"\x01" * 1024,
        "trailforge_terrain.pmtiles": b"PMTilesTER" + b"\x02" * 512,
        "manifest.json": json.dumps({
            "version": "1.1.0",
            "bounds": [148.0, -36.7, 148.2, -36.5],
            "community_telemetry_policy": "baseline_weather_v1",
        }).encode(),
    }
    for fname, blob in tile_files.items():
        (tiles_dir / fname).write_bytes(blob)

    pack_files = {
        "manifest.json": json.dumps({
            "region_id": "australian-alps",
            "version": "2026.11.26",
            "community_telemetry_policy": "static_v1",
        }).encode(),
        "snapshot.json": b'{"tracks":[]}\n',
        "vector.pmtiles": b"PMTilesPACKV" + b"\x03" * 256,
        "terrain.pmtiles": b"PMTilesPACKT" + b"\x04" * 256,
        "bundle.tar.gz": b"\x1f\x8b\x08\x00" + b"\x05" * 128,
    }
    for fname, blob in pack_files.items():
        (alps_dir / fname).write_bytes(blob)
    (staging_dir / "manifest.json").write_bytes(b'{"ignore":true}\n')

    # In-memory mock R2 object store via httpx.MockTransport (zero network!)
    stored_objects: dict[str, dict[str, Any]] = {}
    recorded_calls: list[tuple[str, str]] = []

    def _mock_r2_handler(request: httpx.Request) -> httpx.Response:
        recorded_calls.append((request.method, request.url.path))
        auth = request.headers.get("authorization", "")
        assert auth.startswith("AWS4-HMAC-SHA256 ")
        assert "x-amz-date" in request.headers
        assert "x-amz-content-sha256" in request.headers

        prefix = "/trailforge-bucket/"
        assert request.url.path.startswith(prefix)
        key = request.url.path[len(prefix) :]

        if request.method == "HEAD":
            obj = stored_objects.get(key)
            if obj is None:
                return httpx.Response(404)
            return httpx.Response(
                200,
                headers={
                    "Content-Length": str(len(obj["body"])),
                    "ETag": f'"{obj["md5"]}"',
                    "Cache-Control": obj["cache_control"],
                },
            )
        if request.method == "PUT":
            body = request.read()
            md5_hex = hashlib.md5(body, usedforsecurity=False).hexdigest()
            stored_objects[key] = {
                "body": body,
                "md5": md5_hex,
                "content_type": request.headers.get("content-type"),
                "cache_control": request.headers.get("cache-control"),
                "content_length": request.headers.get("content-length"),
            }
            return httpx.Response(200, headers={"ETag": f'"{md5_hex}"'})
        return httpx.Response(405)

    transport = httpx.MockTransport(_mock_r2_handler)
    with httpx.Client(transport=transport) as mock_client:
        # 1. First sync uploads all 8 artifacts with exact Content-Type & Cache-Control
        res1 = r2_sync.sync_to_r2(
            tiles_dir=tiles_dir,
            packs_dir=packs_dir,
            endpoint_url="https://acct.r2.example.com",
            bucket="trailforge-bucket",
            access_key_id="AKIA_TEST",
            secret_access_key="SECRET_TEST",
            client=mock_client,
        )
        assert res1["dry_run"] is False
        assert res1["total_files"] == 8
        assert res1["uploaded"] == 8
        assert res1["skipped"] == 0

        vector_key = "trailforge_vector_baseline_weather_v1.pmtiles"
        assert stored_objects[vector_key]["content_type"] == (
            "application/vnd.pmtiles"
        )
        assert stored_objects[vector_key]["cache_control"] == (
            "public, max-age=86400"
        )
        assert stored_objects[vector_key]["body"] == (
            tile_files["trailforge_vector.pmtiles"]
        )
        assert stored_objects["manifest.json"]["content_type"] == "application/json"
        assert stored_objects["manifest.json"]["cache_control"] == "public, max-age=300"
        assert stored_objects["australian-alps/static_v1/bundle.tar.gz"]["content_type"] == (
            "application/gzip"
        )
        assert stored_objects["australian-alps/static_v1/bundle.tar.gz"]["cache_control"] == (
            "no-store"
        )
        assert stored_objects["australian-alps/static_v1/vector.pmtiles"]["content_type"] == (
            "application/vnd.pmtiles"
        )

        # 2. Second sync skips all 8 unchanged files via HEAD ETag/size match
        recorded_calls.clear()
        res2 = r2_sync.sync_to_r2(
            tiles_dir=tiles_dir,
            packs_dir=packs_dir,
            endpoint_url="https://acct.r2.example.com",
            bucket="trailforge-bucket",
            access_key_id="AKIA_TEST",
            secret_access_key="SECRET_TEST",
            client=mock_client,
        )
        assert res2["uploaded"] == 0
        assert res2["skipped"] == 8
        assert all(method == "HEAD" for method, _ in recorded_calls)

        # 3. Same-length content mutation triggers single re-upload via MD5 ETag mismatch
        updated_manifest = tile_files["manifest.json"].replace(b"1.1.0", b"1.1.1")
        assert len(updated_manifest) == len(tile_files["manifest.json"])
        (tiles_dir / "manifest.json").write_bytes(updated_manifest)
        res3 = r2_sync.sync_to_r2(
            tiles_dir=tiles_dir,
            packs_dir=packs_dir,
            endpoint_url="https://acct.r2.example.com",
            bucket="trailforge-bucket",
            access_key_id="AKIA_TEST",
            secret_access_key="SECRET_TEST",
            client=mock_client,
        )
        assert res3["uploaded"] == 1
        assert res3["skipped"] == 7
        assert stored_objects["manifest.json"]["body"] == updated_manifest

        # 4. --force skips HEAD checks and re-uploads selected region only
        recorded_calls.clear()
        res4 = r2_sync.sync_to_r2(
            packs_dir=packs_dir,
            region="australian-alps",
            endpoint_url="https://acct.r2.example.com",
            bucket="trailforge-bucket",
            access_key_id="AKIA_TEST",
            secret_access_key="SECRET_TEST",
            force=True,
            client=mock_client,
        )
        assert res4["total_files"] == 5
        assert res4["uploaded"] == 5
        assert all(method == "PUT" for method, _ in recorded_calls)

    # 5. Subprocess CLI dry-run with -W error::RuntimeWarning (0 warnings, 0 network)
    proc = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::RuntimeWarning",
            "-m",
            "trailforge.packs.r2_sync",
            "--tiles-dir",
            str(tiles_dir),
            "--packs-dir",
            str(packs_dir),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")},
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    assert proc.stderr.strip() == ""
    cli_summary = json.loads(proc.stdout)
    assert cli_summary["dry_run"] is True
    assert cli_summary["total_files"] == 8
    assert cli_summary["planned"] == 8
    keys = {item["object_key"] for item in cli_summary["items"]}
    assert "trailforge_vector_baseline_weather_v1.pmtiles" in keys
    assert "australian-alps/static_v1/bundle.tar.gz" in keys


# -----------------------------------------------------------------------------
# 6. PostGIS Integration Tests (Usage DB Rollup + Trip Upload Daily Quota + Photo 507)
# -----------------------------------------------------------------------------


@pytest.mark.integration
def test_usage_metrics_postgis_rollup_and_trip_upload_daily_cap(
    postgis_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_rate_limiter()
    reset_usage_metrics()

    # 1. Verify hourly rollup upsert to usage_metrics_hourly when migration 0004 is applied
    with Session(postgis_engine) as session:
        has_usage_table = session.execute(
            text("SELECT to_regclass('public.usage_metrics_hourly')")
        ).scalar_one_or_none()
        if has_usage_table is not None:
            session.execute(text("DELETE FROM usage_metrics_hourly"))
            session.commit()

            store = UsageMetricsStore()
            fixed_hour = datetime(2026, 11, 26, 8, 15, 0, tzinfo=UTC)
            store.record(route_group="tracks", status_code=200, bytes_out=1500, at=fixed_hour)
            store.record(
                route_group="tracks",
                status_code=429,
                bytes_out=80,
                rate_limited=True,
                at=fixed_hour,
            )
            flushed1 = flush_usage_metrics(session, store=store)
            assert flushed1 == 1
            assert store.get_snapshot()["pending_buckets"] == []

            # Second flush adds onto same (bucket_hour, route_group) PK row
            store.record(route_group="tracks", status_code=500, bytes_out=200, at=fixed_hour)
            flushed2 = flush_usage_metrics(session, store=store)
            assert flushed2 == 1

            row = session.execute(
                text(
                    """
                    SELECT request_count, error_count, bytes_out, rate_limited_count
                    FROM usage_metrics_hourly
                    WHERE route_group = 'tracks'
                    """
                )
            ).one()
            assert tuple(row) == (3, 2, 1780, 1)

            session.execute(text("DELETE FROM usage_metrics_hourly"))
            session.commit()

    # 2. Verify daily per-member trip upload cap on POST /api/trips/upload & /mobile-sync
    uploads_dir = tmp_path / "quota_uploads"
    uploads_dir.mkdir()
    monkeypatch.setenv("TRAILFORGE_UPLOADS_DIR", str(uploads_dir))
    monkeypatch.setenv("TRAILFORGE_REGISTRATION_MODE", "open")
    get_settings.cache_clear()

    cfg = Settings(
        registration_mode="open",
        rate_limit_enabled=True,
        rate_limit_upload_per_day=2,
        uploads_dir=str(uploads_dir),
    )

    with postgis_engine.begin() as conn:
        conn.execute(
            text(
                """
                TRUNCATE TABLE
                    condition_report_votes, condition_reports,
                    telemetry_observations, unmapped_track_candidates,
                    trips, vehicle_profiles, users
                RESTART IDENTITY CASCADE
                """
            )
        )

    try:
        with Session(postgis_engine) as session:
            app = create_app(settings=cfg)

            def _override_db() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db] = _override_db

            with TestClient(app) as client:
                reg = client.post(
                    "/api/auth/register",
                    json={
                        "email": "quota.member@example.au",
                        "display_name": "Quota Member",
                        "password": "TrailForge4WD!",
                    },
                )
                assert reg.status_code == 201, reg.text
                token = reg.json()["access_token"]
                headers = {"Authorization": f"Bearer {token}"}

                trace_payload = {
                    "name": "Short Quota Test",
                    "visibility": "private",
                    "points": [
                        {
                            "lon": 148.1200,
                            "lat": -36.5660,
                            "timestamp": "2026-11-26T01:00:00Z",
                        },
                        {
                            "lon": 148.1205,
                            "lat": -36.5662,
                            "timestamp": "2026-11-26T01:01:00Z",
                        },
                    ],
                }
                r1 = client.post("/api/trips/upload", headers=headers, json=trace_payload)
                assert r1.status_code == 201, r1.text

                r2 = client.post("/api/trips/mobile-sync", headers=headers, json=trace_payload)
                assert r2.status_code == 201, r2.text

                # 3rd upload in 24h hits daily member cap -> HTTP 429 + Retry-After
                r3 = client.post("/api/trips/upload", headers=headers, json=trace_payload)
                assert r3.status_code == 429
                assert int(r3.headers["retry-after"]) >= 1
                assert r3.headers["x-ratelimit-limit"] == "2"

                # Admin users bypass the daily member upload cap
                has_role_col = session.execute(
                    text(
                        """
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'users' AND column_name = 'role'
                        """
                    )
                ).scalar_one_or_none()
                if has_role_col is not None:
                    session.execute(
                        text("UPDATE users SET role = 'admin' WHERE email = :email"),
                        {"email": "quota.member@example.au"},
                    )
                    session.commit()
                    r_admin = client.post("/api/trips/upload", headers=headers, json=trace_payload)
                    assert r_admin.status_code == 201, r_admin.text

                # 3. Verify POST /api/reports photo quota exceeded returns HTTP 507
                monkeypatch.setenv("TRAILFORGE_UPLOADS_MAX_TOTAL_MB", "0.0001")
                r_507 = client.post(
                    "/api/reports",
                    headers=headers,
                    data={
                        "report_type": "washout",
                        "notes": "Testing photo quota exhaustion",
                        "lon": "148.1200",
                        "lat": "-36.5660",
                    },
                    files={
                        "photo": (
                            "quota.png",
                            _make_valid_png_bytes(payload_pad=512),
                            "image/png",
                        )
                    },
                )
                assert r_507.status_code == 507, r_507.text

            app.dependency_overrides.clear()
    finally:
        reset_rate_limiter()
        reset_usage_metrics()
        get_settings.cache_clear()
        with postgis_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    TRUNCATE TABLE
                        condition_report_votes, condition_reports,
                        telemetry_observations, unmapped_track_candidates,
                        trips, vehicle_profiles, users
                    RESTART IDENTITY CASCADE
                    """
                )
            )
