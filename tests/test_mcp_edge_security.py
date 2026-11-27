"""100% offline integrated edge-security & W1+W2+W3+W4 E2E tests against real `create_app()`.

Covers SC-1, SC-2, SC-3, SC-6, SC-7, SC-8, and SC-10 end-to-end on the fully wired FastAPI
application (Settings, SQLAlchemy models/Alembic, PAT/OAuth dependencies, middleware, CORS,
CSRF, body limits, origin secret, rate limiting, admin metrics, and built Vite MCP App HTML).
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from trailforge.api.health import get_health_checker
from trailforge.api.main import create_app
from trailforge.api.middleware import (
    MAX_BODY_API_DEFAULT_BYTES,
    ROUTE_GROUPS,
    classify_route_group,
    flush_usage_metrics,
    reset_rate_limiter,
    reset_usage_metrics,
    resolve_body_size_limit,
)
from trailforge.auth.oauth import compute_pkce_s256
from trailforge.auth.security import create_access_token, hash_password
from trailforge.config import Settings, get_settings
from trailforge.db.models import (
    OAuthAuthorizationCode,
    OAuthClient,
    OAuthToken,
    PersonalAccessToken,
    PlannedRoute,
    Track,
    User,
    VehicleProfile,
)
from trailforge.db.session import DbHealthStatus, reset_engine
from trailforge.feeds import sync_from_fixtures
from trailforge.grading.live import recompute_live_grades
from trailforge.ingestion.connectors.fixtures import load_corridor_fixtures
from trailforge.ingestion.pipeline import ingest_corridor_dataset
from trailforge.mcp.resources import MCP_APP_MIME_TYPE, MCP_APP_URI

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_EXPLORER_HTML = REPO_ROOT / "src" / "trailforge" / "mcp" / "static" / "explorer.html"

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
_CSRF_INPUT_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
_INITIALIZE_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "trailforge-edge-test", "version": "1.0.0"},
    },
}


def _make_offline_app(settings: Settings | None = None) -> Any:
    app = create_app(settings=settings)
    app.dependency_overrides[get_health_checker] = lambda: (
        lambda: DbHealthStatus(
            db_ok=True,
            postgis_version="3.4 USE_GEOS=1 USE_PROJ=1",
            schema_revision="0006_ai_mcp_auth",
        )
    )
    return app


def _mcp_post(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    path: str = "/mcp",
    headers: dict[str, str] | None = None,
    req_id: int = 1,
) -> Any:
    merged = dict(MCP_HEADERS)
    if headers:
        merged.update(headers)
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    return client.post(path, json=payload, headers=merged)


async def _run_asgi_request(
    app: Any,
    *,
    method: str = "POST",
    path: str = "/mcp",
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] = ("198.51.100.25", 54321),
    chunks: list[bytes] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers or [],
        "client": client,
        "server": ("trails.example.com", 80),
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


def _truncate_all_integration_tables(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                TRUNCATE TABLE
                    oauth_tokens, oauth_authorization_codes, oauth_clients,
                    personal_access_tokens,
                    condition_report_votes, condition_reports,
                    telemetry_observations, unmapped_track_candidates, trips,
                    planned_routes, user_favorites, user_pins, vehicle_profiles,
                    invite_codes, usage_metrics_hourly, users,
                    segment_closure_links, closures_alerts, feed_sync_status,
                    track_segment_grades, rainfall_snapshots, pois,
                    segment_reconciliation_log, track_segments, tracks,
                    capad_parks, source_capad_parks, source_pois, source_waterways,
                    source_tracks_nsw, source_tracks_vic, source_tracks_osm,
                    source_tracks_au_state
                RESTART IDENTITY CASCADE
                """
            )
        )


# -----------------------------------------------------------------------------
# 1. Settings, Model Re-Exports, Route Classification & Docker Packaging Checks
# -----------------------------------------------------------------------------


def test_mcp_settings_defaults_aliases_and_model_reexports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = Settings()
    assert defaults.enable_mcp is True
    assert defaults.mcp_auth_mode == "hybrid"
    assert defaults.public_base_url == ""
    assert defaults.rate_limit_mcp_per_min == 60
    assert defaults.mcp_app_html == ""
    assert defaults.mcp_csp_tile_domains == ""
    assert defaults.oauth_access_token_ttl_s == 3600
    assert defaults.oauth_refresh_token_ttl_s == 2_592_000
    assert defaults.oauth_dcr_max_per_ip_per_day == 20

    monkeypatch.setenv("TRAILFORGE_ENABLE_MCP", "false")
    monkeypatch.setenv("TRAILFORGE_MCP_AUTH_MODE", "oauth_required")
    monkeypatch.setenv("TRAILFORGE_PUBLIC_BASE_URL", "https://trails.example.com")
    monkeypatch.setenv("TRAILFORGE_RATE_LIMIT_MCP_PER_MIN", "30")
    monkeypatch.setenv("TRAILFORGE_MCP_APP_HTML", "/tmp/explorer.html")
    monkeypatch.setenv("TRAILFORGE_MCP_CSP_TILE_DOMAINS", "https://tiles.example.com")
    monkeypatch.setenv("TRAILFORGE_OAUTH_ACCESS_TOKEN_TTL_S", "1800")
    monkeypatch.setenv("TRAILFORGE_OAUTH_REFRESH_TOKEN_TTL_S", "604800")
    monkeypatch.setenv("TRAILFORGE_OAUTH_DCR_MAX_PER_IP_PER_DAY", "10")

    cfg = Settings()
    assert cfg.enable_mcp is False
    assert cfg.mcp_auth_mode == "oauth_required"
    assert cfg.public_base_url == "https://trails.example.com"
    assert cfg.rate_limit_mcp_per_min == 30
    assert cfg.mcp_app_html == "/tmp/explorer.html"
    assert cfg.mcp_csp_tile_domains == "https://tiles.example.com"
    assert cfg.oauth_access_token_ttl_s == 1800
    assert cfg.oauth_refresh_token_ttl_s == 604800
    assert cfg.oauth_dcr_max_per_ip_per_day == 10

    with pytest.raises(ValidationError):
        Settings(rate_limit_mcp_per_min=0)
    with pytest.raises(ValidationError):
        Settings(oauth_access_token_ttl_s=10)
    with pytest.raises(ValidationError):
        Settings(oauth_dcr_max_per_ip_per_day=0)

    # Confirm W1 Tier C models are re-exported from trailforge.db.models
    assert PersonalAccessToken.__tablename__ == "personal_access_tokens"
    assert OAuthClient.__tablename__ == "oauth_clients"
    assert OAuthAuthorizationCode.__tablename__ == "oauth_authorization_codes"
    assert OAuthToken.__tablename__ == "oauth_tokens"


def test_mcp_route_group_classification_and_body_cap() -> None:
    assert "mcp" in ROUTE_GROUPS
    assert classify_route_group("/mcp") == "mcp"
    assert classify_route_group("/mcp/") == "mcp"
    assert classify_route_group("/mcp/auth") == "mcp"
    assert classify_route_group("/mcp/auth/") == "mcp"
    assert classify_route_group("/.well-known/oauth-protected-resource") == "auth"
    assert classify_route_group("/.well-known/oauth-protected-resource/mcp/auth") == "auth"
    assert classify_route_group("/.well-known/oauth-authorization-server") == "auth"
    assert classify_route_group("/api/oauth/register") == "auth"
    assert classify_route_group("/api/oauth/authorize") == "auth"
    assert classify_route_group("/api/oauth/token") == "auth"

    for path in ("/mcp", "/mcp/", "/mcp/auth", "/api/oauth/register", "/api/oauth/token"):
        assert resolve_body_size_limit(path) == MAX_BODY_API_DEFAULT_BYTES == 1024 * 1024


def test_dockerfile_builds_and_ships_mcp_explorer_html() -> None:
    dockerfile_path = REPO_ROOT / "Dockerfile"
    if dockerfile_path.is_file():
        df = dockerfile_path.read_text(encoding="utf-8")
        assert "AS mcp-app-builder" in df
        assert "npm run build:mcp" in df
        assert "src/trailforge/mcp/static/explorer.html" in df


# -----------------------------------------------------------------------------
# 2. SC-10 Edge Security on Real `create_app()` (Zero DB Required)
# -----------------------------------------------------------------------------


def test_sc10_mcp_rate_limit_body_cap_origin_secret_and_kill_switch() -> None:
    reset_usage_metrics()

    # 1. Dedicated sliding-window rate limit on /mcp & /mcp/auth (429 + Retry-After)
    rl_cfg = Settings(
        rate_limit_enabled=True,
        rate_limit_mcp_per_min=3,
        rate_limit_read_per_min=100,
        trust_proxy_headers=True,
        trust_cloudflare_headers=True,
    )
    rl_app = _make_offline_app(settings=rl_cfg)
    reset_rate_limiter(rl_app)
    with TestClient(rl_app) as client:
        ip_hdrs = {
            **MCP_HEADERS,
            "CF-Connecting-IP": "198.51.100.88",
            "Origin": "https://claude.ai",
        }
        r1 = client.post("/mcp", json=_INITIALIZE_BODY, headers=ip_hdrs)
        r2 = client.post("/mcp", json=_INITIALIZE_BODY, headers=ip_hdrs)
        r3 = client.post("/mcp", json=_INITIALIZE_BODY, headers=ip_hdrs)
        r4 = client.post("/mcp", json=_INITIALIZE_BODY, headers=ip_hdrs)
        r5 = client.post("/mcp/auth", json=_INITIALIZE_BODY, headers=ip_hdrs)

        assert [r1.status_code, r2.status_code, r3.status_code] == [200, 200, 200]
        assert r1.headers.get("x-ratelimit-limit") == "3"
        assert r1.headers.get("x-ratelimit-remaining") == "2"
        assert r3.headers.get("x-ratelimit-remaining") == "0"

        assert r4.status_code == 429
        assert int(r4.headers["retry-after"]) >= 1
        assert r4.headers.get("x-ratelimit-remaining") == "0"
        assert r4.headers.get("access-control-allow-origin") == "https://claude.ai"
        assert "access-control-allow-credentials" not in r4.headers

        # /mcp/auth shares the per-IP "mcp" rate-limit bucket
        assert r5.status_code == 429
        assert int(r5.headers["retry-after"]) >= 1

        # General /api/* routes are NOT blocked by the exhausted "mcp" bucket
        assert (
            client.get("/api/health", headers={"CF-Connecting-IP": "198.51.100.88"}).status_code
            == 200
        )

    # 2. 1 MiB streaming body cap (413) on /mcp, /mcp/auth, and /api/oauth/register
    cap_app = _make_offline_app(settings=Settings(rate_limit_enabled=False))
    oversized_chunks = [b'{"jsonrpc":"2.0","pad":"' + b"x" * (600 * 1024), b"y" * (600 * 1024)]
    for target_path in ("/mcp", "/mcp/auth", "/api/oauth/register"):
        code_cl, _, body_cl = asyncio.run(
            _run_asgi_request(
                cap_app,
                method="POST",
                path=target_path,
                headers=[
                    (b"content-type", b"application/json"),
                    (b"content-length", str(1024 * 1024 + 1).encode("ascii")),
                ],
                chunks=[b"{}"],
            )
        )
        assert code_cl == 413, f"Expected 413 Content-Length cap on {target_path}, got {code_cl}"
        assert "Payload Too Large" in json.loads(body_cl)["detail"]

    for target_path in ("/mcp", "/api/oauth/register"):
        code_stream, hdrs_stream, body_stream = asyncio.run(
            _run_asgi_request(
                cap_app,
                method="POST",
                path=target_path,
                headers=[(b"content-type", b"application/json")],
                chunks=oversized_chunks,
            )
        )
        assert code_stream == 413, (
            f"Expected 413 streamed cap on {target_path}, got {code_stream}"
        )
        assert "Payload Too Large" in json.loads(body_stream)["detail"]
        assert hdrs_stream.get("x-content-type-options") == "nosniff"

    # Unauthenticated chunked /mcp/auth fails early with 401 before reading any request body
    code_unauth_stream, _, _ = asyncio.run(
        _run_asgi_request(
            cap_app,
            method="POST",
            path="/mcp/auth",
            headers=[(b"content-type", b"application/json")],
            chunks=oversized_chunks,
        )
    )
    assert code_unauth_stream == 401

    # 3. X-TrailForge-Origin-Secret enforcement (403 without secret, passes with secret)
    secret = "edge-origin-secret-0123456789abcdef0123456789"
    sec_app = _make_offline_app(
        settings=Settings(origin_shared_secret=secret, rate_limit_enabled=False)
    )
    with TestClient(sec_app) as client:
        for method, path in (
            ("POST", "/mcp"),
            ("POST", "/mcp/auth"),
            ("GET", "/.well-known/oauth-protected-resource"),
            ("GET", "/.well-known/oauth-protected-resource/mcp/auth"),
            ("GET", "/.well-known/oauth-authorization-server"),
            ("POST", "/api/oauth/register"),
        ):
            r_no = client.request(method, path, json=_INITIALIZE_BODY if method == "POST" else None)
            assert r_no.status_code == 403, f"Missing origin secret not blocked on {method} {path}"
            r_bad = client.request(
                method,
                path,
                headers={"X-TrailForge-Origin-Secret": "wrong-secret"},
                json=_INITIALIZE_BODY if method == "POST" else None,
            )
            assert r_bad.status_code == 403

        r_ok_mcp = client.post(
            "/mcp",
            json=_INITIALIZE_BODY,
            headers={**MCP_HEADERS, "X-TrailForge-Origin-Secret": secret},
        )
        assert r_ok_mcp.status_code == 200
        r_ok_wk = client.get(
            "/.well-known/oauth-protected-resource/mcp/auth",
            headers={"X-TrailForge-Origin-Secret": secret},
        )
        assert r_ok_wk.status_code == 200

    # 4. TRAILFORGE_ENABLE_MCP=false kill switch returns 404 across MCP & discovery
    off_app = _make_offline_app(settings=Settings(enable_mcp=False, rate_limit_enabled=False))
    with TestClient(off_app) as client:
        assert client.post("/mcp", json=_INITIALIZE_BODY, headers=MCP_HEADERS).status_code == 404
        assert (
            client.post("/mcp/auth", json=_INITIALIZE_BODY, headers=MCP_HEADERS).status_code == 404
        )
        assert client.options("/mcp", headers={"Origin": "https://claude.ai"}).status_code == 404
        assert client.get("/.well-known/oauth-protected-resource").status_code == 404
        assert client.get("/.well-known/oauth-protected-resource/mcp/auth").status_code == 404
        assert client.get("/.well-known/oauth-authorization-server").status_code == 404
        assert (
            client.post(
                "/api/oauth/register",
                json={"client_name": "Disabled Test", "redirect_uris": ["https://claude.ai/cb"]},
            ).status_code
            == 404
        )


def test_cross_origin_mcp_cors_cookie_csrf_and_real_built_explorer_html() -> None:
    cfg = Settings(
        cors_origins="http://localhost:5173",
        public_base_url="https://trails.example.com",
        tiles_public_base_url="https://tiles.example.com",
        rate_limit_enabled=False,
    )
    app = _make_offline_app(settings=cfg)

    with TestClient(app) as client:
        # 1. Cross-origin preflight on /mcp, /mcp/auth, and /.well-known/* succeeds with 204
        for path, req_method in (
            ("/mcp", "POST"),
            ("/mcp/auth", "POST"),
            ("/.well-known/oauth-protected-resource/mcp/auth", "GET"),
            ("/.well-known/oauth-authorization-server", "GET"),
        ):
            r_opt = client.options(
                path,
                headers={
                    "Origin": "https://claude.ai",
                    "Access-Control-Request-Method": req_method,
                    "Access-Control-Request-Headers": "authorization, content-type",
                },
            )
            assert r_opt.status_code == 204, f"Preflight failed on {path}: {r_opt.status_code}"
            assert r_opt.headers.get("access-control-allow-origin") in ("https://claude.ai", "*")
            assert "access-control-allow-credentials" not in r_opt.headers
            exposed = (r_opt.headers.get("access-control-expose-headers") or "").lower()
            assert "mcp-session-id" in exposed
            assert "www-authenticate" in exposed

        # 2. Same-origin / allowlisted Origin on /.well-known/* emits single ACAO (no duplicates)
        r_wk = client.get(
            "/.well-known/oauth-protected-resource",
            headers={"Origin": "http://localhost:5173"},
        )
        assert r_wk.status_code == 200
        assert len(r_wk.headers.get_list("access-control-allow-origin")) == 1
        assert "access-control-allow-credentials" not in r_wk.headers

        # 3. Cookie CSRF skips Bearer-only /mcp while staying strict on /api/*
        r_mcp_csrf = client.post(
            "/mcp",
            json=_INITIALIZE_BODY,
            headers={
                **MCP_HEADERS,
                "Cookie": "tf_session=irrelevant-browser-cookie",
                "Sec-Fetch-Site": "cross-site",
                "Origin": "https://chatgpt.com",
            },
        )
        assert r_mcp_csrf.status_code == 200

        r_api_csrf = client.post(
            "/api/vehicles",
            json={},
            headers={
                "Cookie": "tf_session=irrelevant-browser-cookie",
                "Sec-Fetch-Site": "cross-site",
                "Origin": "https://evil.example",
            },
        )
        assert r_api_csrf.status_code == 403
        assert r_api_csrf.json() == {"detail": "Cross-site request blocked"}

        # 4. resources/read of ui://trailforge/explorer.html serves the REAL built Vite bundle
        assert STATIC_EXPLORER_HTML.is_file(), "Built explorer.html must be installed in static/"
        r_res = _mcp_post(client, "resources/read", {"uri": MCP_APP_URI}, req_id=7)
        assert r_res.status_code == 200, r_res.text
        body = r_res.json()
        contents = body["result"]["contents"]
        assert len(contents) == 1
        item = contents[0]
        assert item["mimeType"] == MCP_APP_MIME_TYPE
        html = item["text"]
        html_bytes = len(html.encode("utf-8"))
        # Real Vite single-file bundle (~1.48 MB) — strictly larger than the ~4 KB fallback
        assert 200_000 < html_bytes < 4 * 1024 * 1024, f"Unexpected HTML size: {html_bytes}"
        assert "__TRAILFORGE_MCP_APP__" in html or "TrailForge" in html
        assert "<script src=" not in html.lower()
        assert '<link rel="stylesheet" href=' not in html.lower()
        assert "/Users/" not in html
        assert "OpenStreetMap" in html
        meta_ui = item["_meta"]["ui"]
        assert meta_ui["prefersBorder"] is False
        assert "https://tiles.example.com" in meta_ui["csp"]["connectDomains"]
        assert "https://trails.example.com" in meta_ui["csp"]["connectDomains"]


# -----------------------------------------------------------------------------
# 3. Full W1 + W2 + W3 + W4 PostGIS E2E Flow on Real `create_app()`
# -----------------------------------------------------------------------------


@pytest.mark.integration
def test_full_w1_w2_w3_w4_e2e_oauth_pat_mcp_and_admin_metrics(
    postgis_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _truncate_all_integration_tables(postgis_engine)
    reset_usage_metrics()
    reset_engine()
    get_settings.cache_clear()

    # Seed deterministic synthetic Australian Alps corridor into PostGIS
    dataset = load_corridor_fixtures(REPO_ROOT / "tests" / "fixtures")
    with Session(postgis_engine) as seed_db:
        ingest_corridor_dataset(seed_db, dataset)
        sync_from_fixtures(seed_db, REPO_ROOT / "tests" / "fixtures" / "closures_sample.json")
        recompute_live_grades(seed_db, rain_override_72h_mm=16.0)

        driver = User(
            email="driver.e2e@example.com",
            display_name="Alpine Driver",
            password_hash=hash_password("DriverPass123!"),
            role="member",
            is_active=True,
        )
        admin = User(
            email="admin.e2e@example.com",
            display_name="Instance Admin",
            password_hash=hash_password("AdminPass123!"),
            role="admin",
            is_active=True,
        )
        seed_db.add_all([driver, admin])
        seed_db.flush()

        rig = VehicleProfile(
            user_id=driver.id,
            name="79 Series Tourer",
            drivetrain="4wd_part_time",
            has_low_range=True,
            lift_mm=50,
            ground_clearance_mm=245,
            tire_class="AT",
            rear_locker=True,
            has_winch=True,
            has_snorkel=True,
            capability_class="Touring 4WD",
            is_default=True,
        )
        seed_db.add(rig)
        seed_db.commit()

        track_ids = list(
            seed_db.execute(
                select(Track.track_id).where(Track.is_active.is_(True)).order_by(Track.name)
            )
            .scalars()
            .all()
        )
        assert len(track_ids) >= 2
        admin_session_tok = create_access_token(
            str(admin.id), admin.email, token_version=admin.token_version
        )
        driver_session_tok = create_access_token(
            str(driver.id), driver.email, token_version=driver.token_version
        )

    cfg = Settings(
        database_url=str(postgis_engine.url.render_as_string(hide_password=False)),
        public_base_url="https://trails.example.com",
        tiles_public_base_url="https://tiles.example.com",
        rate_limit_enabled=True,
        rate_limit_mcp_per_min=50,
        rate_limit_read_per_min=200,
    )
    app = create_app(settings=cfg)
    reset_rate_limiter(app)

    try:
        with TestClient(app) as client:
            # 1. Anonymous /mcp public tool call succeeds with real PostGIS data
            r_init = _mcp_post(client, "initialize", _INITIALIZE_BODY["params"], req_id=1)
            assert r_init.status_code == 200
            r_search = _mcp_post(
                client,
                "tools/call",
                {"name": "search_trails", "arguments": {"state": "VIC", "limit": 5}},
                req_id=2,
            )
            assert r_search.status_code == 200, r_search.text
            search_res = r_search.json()["result"]
            assert search_res.get("isError") is not True
            sc = search_res["structuredContent"]
            assert sc["schema_version"] == "1.0"
            assert sc["view_mode"] == "explore"
            assert len(sc["explore"]["tracks"]) >= 1
            assert "preview_coords" in sc["explore"]["tracks"][0]

            # 2. Anonymous /mcp/auth returns 401 + RFC 9728 WWW-Authenticate
            r_unauth = _mcp_post(
                client,
                "tools/call",
                {"name": "get_my_garage", "arguments": {}},
                path="/mcp/auth",
                req_id=3,
            )
            assert r_unauth.status_code == 401
            www_auth = r_unauth.headers.get("www-authenticate", "")
            assert (
                'resource_metadata="https://trails.example.com/'
                '.well-known/oauth-protected-resource/mcp/auth"'
                in www_auth
            )

            # 3. Full OAuth 2.1 Discovery -> DCR -> PKCE S256 -> Login + Consent -> Token
            r_prm = client.get("/.well-known/oauth-protected-resource/mcp/auth")
            assert r_prm.status_code == 200
            assert r_prm.json()["resource"] == "https://trails.example.com/mcp/auth"
            assert r_prm.json()["authorization_servers"] == ["https://trails.example.com"]

            r_asm = client.get("/.well-known/oauth-authorization-server")
            assert r_asm.status_code == 200
            asm = r_asm.json()
            assert asm["registration_endpoint"] == "https://trails.example.com/api/oauth/register"
            assert asm["token_endpoint"] == "https://trails.example.com/api/oauth/token"

            r_dcr = client.post(
                "/api/oauth/register",
                json={
                    "client_name": "Claude Web E2E",
                    "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "token_endpoint_auth_method": "none",
                },
            )
            assert r_dcr.status_code == 201, r_dcr.text
            client_id = r_dcr.json()["client_id"]

            verifier = secrets.token_urlsafe(48)
            challenge = compute_pkce_s256(verifier)
            redirect_uri = "https://claude.ai/api/mcp/auth_callback"
            state_val = "state-e2e-xyz"

            r_auth_get = client.get(
                "/api/oauth/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": "trailforge:read trailforge:write",
                    "state": state_val,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "resource": "https://trails.example.com/mcp/auth",
                },
            )
            assert r_auth_get.status_code == 200
            login_csrf_match = _CSRF_INPUT_RE.search(r_auth_get.text)
            assert login_csrf_match is not None

            r_login_post = client.post(
                "/api/oauth/authorize",
                data={
                    "action": "login",
                    "csrf_token": login_csrf_match.group(1),
                    "email": "driver.e2e@example.com",
                    "password": "DriverPass123!",
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": "trailforge:read trailforge:write",
                    "state": state_val,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "resource": "https://trails.example.com/mcp/auth",
                },
            )
            assert r_login_post.status_code == 200
            assert "form-action 'self' https://claude.ai;" in r_login_post.headers.get(
                "content-security-policy", ""
            )
            consent_csrf_match = _CSRF_INPUT_RE.search(r_login_post.text)
            assert consent_csrf_match is not None

            r_consent_post = client.post(
                "/api/oauth/authorize",
                data={
                    "action": "consent",
                    "decision": "approve",
                    "csrf_token": consent_csrf_match.group(1),
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": "trailforge:read trailforge:write",
                    "state": state_val,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "resource": "https://trails.example.com/mcp/auth",
                },
                follow_redirects=False,
            )
            assert r_consent_post.status_code == 302
            loc = r_consent_post.headers["location"]
            qs = parse_qs(urlsplit(loc).query)
            assert qs["state"] == [state_val]
            auth_code = qs["code"][0]

            r_tok = client.post(
                "/api/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": auth_code,
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "code_verifier": verifier,
                },
            )
            assert r_tok.status_code == 200, r_tok.text
            oauth_access = r_tok.json()["access_token"]
            assert oauth_access.startswith("tf_oat_")
            client.cookies.clear()

            # 4. Authenticated /mcp/auth calls using OAuth access token
            oauth_hdrs = {"Authorization": f"Bearer {oauth_access}"}
            r_garage = _mcp_post(
                client,
                "tools/call",
                {"name": "get_my_garage", "arguments": {}},
                path="/mcp/auth",
                headers=oauth_hdrs,
                req_id=4,
            )
            assert r_garage.status_code == 200, r_garage.text
            garage_res = r_garage.json()["result"]
            assert garage_res.get("isError") is not True
            assert "79 Series Tourer" in garage_res["content"][0]["text"]

            r_save_route = _mcp_post(
                client,
                "tools/call",
                {
                    "name": "save_planned_route",
                    "arguments": {
                        "name": "OAuth High Country Weekend",
                        "track_ids": track_ids[:2],
                        "vehicle_class": "Touring 4WD",
                    },
                },
                path="/mcp/auth",
                headers=oauth_hdrs,
                req_id=5,
            )
            assert r_save_route.status_code == 200, r_save_route.text
            save_res = r_save_route.json()["result"]
            assert save_res.get("isError") is not True
            saved_sc = save_res["structuredContent"]
            assert saved_sc["view_mode"] == "route"
            assert "?route=" in saved_sc["deep_link_url"]
            assert "rig=Touring" in saved_sc["deep_link_url"]

            # OAuth token cannot manage PATs, OAuth grants, admin API, or log out user sessions
            assert client.get("/api/auth/tokens", headers=oauth_hdrs).status_code == 403
            assert client.get("/api/auth/oauth-grants", headers=oauth_hdrs).status_code == 403
            assert client.get("/api/admin/stats", headers=oauth_hdrs).status_code == 403
            assert client.post("/api/auth/logout", headers=oauth_hdrs).status_code == 403

            # Driver session remains intact and lists the connected OAuth app cleanly
            driver_sess_hdrs = {"Authorization": f"Bearer {driver_session_tok}"}
            r_grants = client.get("/api/auth/oauth-grants", headers=driver_sess_hdrs)
            assert r_grants.status_code == 200
            assert any(g["client_id"] == client_id for g in r_grants.json()["grants"])

            # 5. PAT Read-Only vs Read+Write Scope Enforcement on /mcp AND /api/*
            r_ro = client.post(
                "/api/auth/tokens",
                headers=driver_sess_hdrs,
                json={"name": "ReadOnly CLI", "scopes": ["trailforge:read"]},
            )
            assert r_ro.status_code == 201, r_ro.text
            pat_ro = r_ro.json()["token"]
            ro_hdrs = {"Authorization": f"Bearer {pat_ro}"}

            r_rw = client.post(
                "/api/auth/tokens",
                headers=driver_sess_hdrs,
                json={"name": "ReadWrite CLI", "scopes": ["trailforge:read", "trailforge:write"]},
            )
            assert r_rw.status_code == 201, r_rw.text
            pat_rw = r_rw.json()["token"]
            pat_rw_id = r_rw.json()["id"]
            rw_hdrs = {"Authorization": f"Bearer {pat_rw}"}

            # Read-only PAT works on reads (/mcp + /api/vehicles) and is blocked on writes
            r_ro_garage = _mcp_post(
                client,
                "tools/call",
                {"name": "get_my_garage", "arguments": {}},
                headers=ro_hdrs,
                req_id=6,
            )
            assert r_ro_garage.status_code == 200
            assert r_ro_garage.json()["result"].get("isError") is not True
            assert client.get("/api/vehicles", headers=ro_hdrs).status_code == 200

            r_ro_write_mcp = _mcp_post(
                client,
                "tools/call",
                {
                    "name": "save_personal_pin",
                    "arguments": {"title": "Blocked Camp", "lat": -36.58, "lon": 148.12},
                },
                headers=ro_hdrs,
                req_id=7,
            )
            assert r_ro_write_mcp.status_code == 200
            assert r_ro_write_mcp.json()["result"]["isError"] is True
            assert "trailforge:write" in r_ro_write_mcp.json()["result"]["content"][0]["text"]

            r_ro_write_api = client.post(
                "/api/pins",
                headers=ro_hdrs,
                json={"name": "Blocked API Pin", "category": "camp", "lat": -36.58, "lon": 148.12},
            )
            assert r_ro_write_api.status_code == 403
            assert "trailforge:write" in r_ro_write_api.json()["detail"]

            # PATs cannot manage /api/auth/tokens, /api/auth/oauth-grants, /api/admin/*, or logout
            assert client.get("/api/auth/tokens", headers=rw_hdrs).status_code == 403
            assert client.get("/api/auth/oauth-grants", headers=rw_hdrs).status_code == 403
            assert client.get("/api/admin/stats", headers=rw_hdrs).status_code == 403
            assert client.post("/api/auth/logout", headers=ro_hdrs).status_code == 403
            assert client.post("/api/auth/logout", headers=rw_hdrs).status_code == 403
            assert client.get("/api/auth/me", headers=ro_hdrs).json()["authenticated"] is True

            # Write PAT succeeds on MCP write + REST write
            r_rw_pin = _mcp_post(
                client,
                "tools/call",
                {
                    "name": "save_personal_pin",
                    "arguments": {
                        "title": "Davies Hut Camp",
                        "lat": -36.58,
                        "lon": 148.12,
                        "category": "camp",
                    },
                },
                headers=rw_hdrs,
                req_id=8,
            )
            assert r_rw_pin.status_code == 200
            assert r_rw_pin.json()["result"].get("isError") is not True

            # Revoking the write PAT invalidates it immediately across /mcp & /api/*
            assert (
                client.delete(
                    f"/api/auth/tokens/{pat_rw_id}", headers=driver_sess_hdrs
                ).status_code
                == 200
            )
            assert (
                _mcp_post(
                    client,
                    "tools/call",
                    {"name": "get_my_garage", "arguments": {}},
                    path="/mcp/auth",
                    headers=rw_hdrs,
                    req_id=9,
                ).status_code
                == 401
            )
            assert client.get("/api/vehicles", headers=rw_hdrs).status_code == 401

            # 6. SC-10 Admin Console telemetry surfaces route_group="mcp"
            with Session(postgis_engine) as flush_db:
                flush_usage_metrics(session=flush_db)

            admin_hdrs = {"Authorization": f"Bearer {admin_session_tok}"}
            r_stats = client.get("/api/admin/stats", headers=admin_hdrs)
            assert r_stats.status_code == 200, r_stats.text
            breakdown = {
                row["route_group"]: row for row in r_stats.json()["route_breakdown"]
            }
            assert "mcp" in breakdown, f"Missing 'mcp' in route_breakdown: {breakdown}"
            assert breakdown["mcp"]["request_count"] >= 6

            # Confirm saved planned route persisted cleanly in PostGIS
            with Session(postgis_engine) as verify_db:
                routes = list(verify_db.execute(select(PlannedRoute)).scalars().all())
                assert any(r.name == "OAuth High Country Weekend" for r in routes)
    finally:
        reset_usage_metrics()
        reset_engine()
        get_settings.cache_clear()
        _truncate_all_integration_tables(postgis_engine)
