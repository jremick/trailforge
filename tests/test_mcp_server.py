"""100% offline tests for TrailForge MCP Server, 18 tools, resources, prompts, auth & stdio proxy.

Covers Checkable Success Criteria SC-1, SC-2, SC-3, SC-5, SC-8, SC-11, and SC-12 against
synthetic VIC/NSW Alps fixtures under `tests/fixtures/`.
"""

from __future__ import annotations

import base64
import io
import json
import uuid
from collections.abc import Generator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from geoalchemy2.shape import from_shape
from mcp import types
from PIL import Image
from shapely.geometry import LineString, Point
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from tests.conftest import _ensure_postgis_and_schema, resolve_test_database_url
from trailforge.auth.security import create_access_token, hash_password
from trailforge.config import Settings
from trailforge.db.models import (
    ConditionReport,
    PlannedRoute,
    RainfallSnapshot,
    Track,
    Trip,
    User,
    UserPin,
    VehicleProfile,
)
from trailforge.db.session import get_engine
from trailforge.feeds import sync_from_fixtures
from trailforge.grading.live import recompute_live_grades
from trailforge.ingestion.connectors.fixtures import load_corridor_fixtures
from trailforge.ingestion.pipeline import ingest_corridor_dataset
from trailforge.mcp.context import (
    ANONYMOUS_CONTEXT,
    SCOPE_READ,
    SCOPE_WRITE,
    McpAuthContext,
    McpAuthResolver,
    default_auth_resolver,
    parse_bearer_token,
)
from trailforge.mcp.prompts import get_mcp_prompt, list_mcp_prompts
from trailforge.mcp.renderer import MAX_PNG_BYTES
from trailforge.mcp.resources import (
    GRADING_METHODOLOGY_URI,
    MCP_APP_MIME_TYPE,
    MCP_APP_URI,
    REGIONS_URI,
    VEHICLE_CLASSES_URI,
    list_mcp_resources,
    read_mcp_resource,
)
from trailforge.mcp.schemas import MAX_CALL_TOOL_RESULT_BYTES
from trailforge.mcp.server import create_mcp_asgi_apps, mcp_lifespan
from trailforge.mcp.stdio_proxy import (
    _parse_sse_jsonrpc_messages,
    build_arg_parser,
    forward_jsonrpc_message,
)
from trailforge.mcp.tools import (
    ALL_TOOL_NAMES,
    APP_ONLY_TOOL_NAMES,
    PERSONAL_READ_TOOL_NAMES,
    PRIVATE_WRITE_TOOL_NAMES,
    PUBLIC_TOOL_NAMES,
)

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _build_test_mcp_app(
    *,
    auth_resolver: McpAuthResolver = default_auth_resolver,
    settings: Settings | None = None,
    db_session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    """Mount W2's MCP ASGI apps on a standalone FastAPI test app (no shared seam edits needed)."""
    mcp_apps = create_mcp_asgi_apps(
        auth_resolver=auth_resolver,
        settings=settings,
        db_session_factory=db_session_factory,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp_lifespan(mcp_apps):
            yield

    app = FastAPI(lifespan=lifespan)
    mcp_apps.register(app)
    return app


def _mcp_rpc(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    path: str = "/mcp",
    headers: dict[str, str] | None = None,
    req_id: int = 1,
) -> httpx.Response:
    merged_headers = dict(MCP_HEADERS)
    if headers:
        merged_headers.update(headers)
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(path, json=body, headers=merged_headers)


def _assert_has_attributions(text_blob: str, structured: dict[str, Any] | None) -> None:
    lower = text_blob.lower()
    assert "openstreetmap" in lower, "Missing OSM attribution in text"
    assert "geoscience australia" in lower or "ga/dea" in lower, (
        "Missing GA/DEA attribution in text"
    )
    assert "open-meteo" in lower, "Missing Open-Meteo attribution in text"
    if structured is not None:
        attrs = structured.get("attributions")
        assert isinstance(attrs, list) and len(attrs) >= 4
        joined = " ".join(str(a) for a in attrs).lower()
        assert "openstreetmap" in joined
        assert "geoscience australia" in joined
        assert "open-meteo" in joined


def _assert_valid_png_block(
    block: dict[str, Any], expected_size: tuple[int, int] | None = None
) -> None:
    assert block["type"] == "image"
    assert block["mimeType"] == "image/png"
    raw = base64.b64decode(block["data"])
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(raw) <= MAX_PNG_BYTES
    img = Image.open(io.BytesIO(raw))
    img.load()
    if expected_size is not None:
        assert img.size == expected_size


# =============================================================================
# 1. Pure Unit Tests — Resources, Prompts, Tools Metadata, Transport & Proxy (Zero DB)
# =============================================================================


def test_resources_sep1865_mime_csp_and_html_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRAILFORGE_PUBLIC_BASE_URL", "https://trails.example.com/subpath")
    monkeypatch.setenv(
        "TRAILFORGE_MCP_CSP_TILE_DOMAINS",
        "https://tiles.example.com/pmtiles/vector.pmtiles, https://cdn.example.org:8443/path",
    )
    resources = list_mcp_resources()
    uris = {str(r.uri) for r in resources}
    assert uris == {
        MCP_APP_URI,
        GRADING_METHODOLOGY_URI,
        VEHICLE_CLASSES_URI,
        REGIONS_URI,
    }

    ui_res = next(r for r in resources if str(r.uri) == MCP_APP_URI)
    assert ui_res.mimeType == MCP_APP_MIME_TYPE
    dumped = ui_res.model_dump(by_alias=True)
    ui_meta = dumped["_meta"]["ui"]
    assert ui_meta["prefersBorder"] is False
    csp = ui_meta["csp"]
    # Bare origins ONLY (no paths, no trailing slashes)
    assert csp["connectDomains"] == [
        "https://tiles.example.com",
        "https://cdn.example.org:8443",
        "https://trails.example.com",
    ]
    assert csp["resourceDomains"] == csp["connectDomains"]

    # Default embedded fallback HTML is self-contained and < 4 MiB
    contents = read_mcp_resource(MCP_APP_URI)
    assert len(contents) == 1
    assert contents[0].mime_type == MCP_APP_MIME_TYPE
    assert isinstance(contents[0].content, str)
    assert "<!doctype html>" in contents[0].content.lower()
    assert len(contents[0].content.encode("utf-8")) < 4 * 1024 * 1024
    assert contents[0].meta is not None
    assert contents[0].meta["ui"]["prefersBorder"] is False

    # Hot-reload override via TRAILFORGE_MCP_APP_HTML
    override_file = tmp_path / "custom_explorer.html"
    override_file.write_text(
        "<!doctype html><html><body>Custom MCP App</body></html>", encoding="utf-8"
    )
    monkeypatch.setenv("TRAILFORGE_MCP_APP_HTML", str(override_file))
    overridden = read_mcp_resource(MCP_APP_URI)
    assert "Custom MCP App" in str(overridden[0].content)

    # Static reference resources
    grading_md = str(read_mcp_resource(GRADING_METHODOLOGY_URI)[0].content)
    assert "baseline_v1" in grading_md and "OpenStreetMap" in grading_md

    vc_json = json.loads(str(read_mcp_resource(VEHICLE_CLASSES_URI)[0].content))
    assert {v["id"] for v in vc_json["vehicle_classes"]} == {
        "AWD",
        "Stock 4WD",
        "Touring 4WD",
        "Modified 4WD",
    }

    reg_json = json.loads(str(read_mcp_resource(REGIONS_URI)[0].content))
    assert reg_json["count"] == 15
    assert any(r["region_id"] == "australian-alps" for r in reg_json["regions"])


def test_prompts_catalog_and_rendering() -> None:
    prompts = list_mcp_prompts()
    assert {p.name for p in prompts} == {
        "plan-weekend-trip",
        "pre-trip-safety-check",
        "find-tracks-for-rig",
    }

    p1 = get_mcp_prompt("plan-weekend-trip", {"region_or_area": "High Country", "days": "3"})
    assert "High Country" in p1.messages[0].content.text  # type: ignore[union-attr]
    assert "plan_route" in p1.messages[0].content.text  # type: ignore[union-attr]

    p2 = get_mcp_prompt("pre-trip-safety-check", {"track_or_route": "Davies Plain Track"})
    assert "Davies Plain Track" in p2.messages[0].content.text  # type: ignore[union-attr]

    p3 = get_mcp_prompt("find-tracks-for-rig", {"region_or_area": "Blue Mountains"})
    assert "compare_tracks" in p3.messages[0].content.text  # type: ignore[union-attr]


def test_mcp_protocol_endpoints_auth_modes_kill_switch_and_cors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRAILFORGE_PUBLIC_BASE_URL", "https://trails.example.com")
    app = _build_test_mcp_app()

    with TestClient(app) as client:
        # 1. OPTIONS CORS preflight on /mcp and /mcp/auth (valid origin + spoofed origin rejection)
        opt = client.options("/mcp", headers={"Origin": "https://claude.ai"})
        assert opt.status_code == 204
        assert opt.headers["access-control-allow-origin"] == "https://claude.ai"
        assert "Mcp-Session-Id" in opt.headers["access-control-expose-headers"]
        assert "WWW-Authenticate" in opt.headers["access-control-expose-headers"]
        assert "access-control-allow-credentials" not in opt.headers

        for bad_origin in (
            "javascript:alert(1)",
            "https://evil.example.com/path",
            "https://user:pass@evil.example.com",
            "not-a-url",
        ):
            bad_opt = client.options("/mcp", headers={"Origin": bad_origin})
            assert bad_opt.status_code == 204
            assert bad_opt.headers["access-control-allow-origin"] == "*"

        # Spoofed Host / X-Forwarded-Host must never inject quotes or paths into WWW-Authenticate
        monkeypatch.delenv("TRAILFORGE_PUBLIC_BASE_URL", raising=False)
        spoof_401 = _mcp_rpc(
            client,
            "tools/list",
            path="/mcp/auth",
            headers={
                "Host": 'evil.com",realm="pwned',
                "X-Forwarded-Host": "attacker.example.com/inject",
            },
            req_id=99,
        )
        assert spoof_401.status_code == 401
        spoof_www = spoof_401.headers["www-authenticate"]
        assert "pwned" not in spoof_www
        assert "attacker.example.com" not in spoof_www
        expected_meta = (
            'resource_metadata="http://localhost:8000/.well-known/oauth-protected-resource/mcp/auth"'
        )
        assert expected_meta in spoof_www
        monkeypatch.setenv("TRAILFORGE_PUBLIC_BASE_URL", "https://trails.example.com")

        # 2. Stateless initialize -> tools/list -> resources/list -> resources/read -> prompts/list
        init_resp = _mcp_rpc(
            client,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest-client", "version": "1.0"},
            },
        )
        assert init_resp.status_code == 200, init_resp.text
        init_data = init_resp.json()["result"]
        assert init_data["serverInfo"]["name"] == "trailforge"

        tools_resp = _mcp_rpc(client, "tools/list", req_id=2)
        assert tools_resp.status_code == 200
        tools_list = tools_resp.json()["result"]["tools"]
        by_name = {t["name"]: t for t in tools_list}
        assert set(by_name.keys()) == ALL_TOOL_NAMES
        assert len(by_name) == 18

        # SC-8: ZERO condition report write tools or admin tools
        for forbidden in (
            "create_report",
            "vote_report",
            "submit_condition_report",
            "admin",
            "sync_closures",
        ):
            assert not any(forbidden in name for name in by_name)

        # SC-3: UI metadata, visibility: ["app"], Antigravity aux_pane entrypoint
        for visual_name in (
            "search_trails",
            "inspect_track",
            "plan_route",
            "compare_tracks",
            "check_area_conditions",
            "open_trailforge_map",
            "save_planned_route",
        ):
            assert by_name[visual_name]["_meta"]["ui"]["resourceUri"] == MCP_APP_URI

        for app_tool in APP_ONLY_TOOL_NAMES:
            assert by_name[app_tool]["_meta"]["ui"]["visibility"] == ["app"]

        open_map_meta = by_name["open_trailforge_map"]["_meta"]
        assert open_map_meta["com.google.antigravity/ui"]["entrypoints"] == [
            {"type": "aux_pane", "title": "TrailForge Map"}
        ]

        # SC-8: Write tool annotations
        for wtool in PRIVATE_WRITE_TOOL_NAMES:
            ann = by_name[wtool]["annotations"]
            assert ann["readOnlyHint"] is False
            assert ann["destructiveHint"] is False
        for rtool in PUBLIC_TOOL_NAMES | PERSONAL_READ_TOOL_NAMES | APP_ONLY_TOOL_NAMES:
            assert by_name[rtool]["annotations"]["readOnlyHint"] is True

        # SC-3: resources/read returns SEP-1865 MIME & _meta
        res_read = _mcp_rpc(client, "resources/read", {"uri": MCP_APP_URI}, req_id=3)
        assert res_read.status_code == 200
        rc = res_read.json()["result"]["contents"][0]
        assert rc["mimeType"] == MCP_APP_MIME_TYPE
        assert rc["_meta"]["ui"]["prefersBorder"] is False
        assert "https://trails.example.com" in rc["_meta"]["ui"]["csp"]["connectDomains"]

        # SC-7: Anonymous request to /mcp/auth returns 401 + RFC 9728 WWW-Authenticate
        auth_401 = _mcp_rpc(client, "tools/list", path="/mcp/auth", req_id=4)
        assert auth_401.status_code == 401
        www = auth_401.headers["www-authenticate"]
        assert (
            'resource_metadata="https://trails.example.com/.well-known/oauth-protected-resource/mcp/auth"'
            in www
        )
        assert 'scope="trailforge:read trailforge:write"' in www

        # Invalid Bearer token on /mcp is rejected immediately with 401
        bad_tok = _mcp_rpc(
            client,
            "tools/list",
            headers={"Authorization": "Bearer tf_pat_invalid_token_value"},
            req_id=5,
        )
        assert bad_tok.status_code == 401

        # SC-1: TRAILFORGE_ENABLE_MCP=false returns 404
        monkeypatch.setenv("TRAILFORGE_ENABLE_MCP", "false")
        assert _mcp_rpc(client, "tools/list", req_id=6).status_code == 404
        assert _mcp_rpc(client, "tools/list", path="/mcp/auth", req_id=7).status_code == 404

        # Auth mode: anonymous_only hides personal/write tools, blocks direct calls & 404s /mcp/auth
        monkeypatch.setenv("TRAILFORGE_ENABLE_MCP", "true")
        monkeypatch.setenv("TRAILFORGE_MCP_AUTH_MODE", "anonymous_only")
        anon_tools = _mcp_rpc(client, "tools/list", req_id=8).json()["result"]["tools"]
        anon_names = {t["name"] for t in anon_tools}
        assert anon_names == PUBLIC_TOOL_NAMES | APP_ONLY_TOOL_NAMES
        blocked_personal = _mcp_rpc(
            client, "tools/call", {"name": "get_my_garage", "arguments": {}}, req_id=81
        )
        assert blocked_personal.json()["result"]["isError"] is True
        assert _mcp_rpc(client, "tools/list", path="/mcp/auth", req_id=9).status_code == 404

        # Auth mode: oauth_required challenges /mcp with 401
        monkeypatch.setenv("TRAILFORGE_MCP_AUTH_MODE", "oauth_required")
        req_401 = _mcp_rpc(client, "tools/list", req_id=10)
        assert req_401.status_code == 401
        assert "/.well-known/oauth-protected-resource/mcp" in req_401.headers["www-authenticate"]


def test_stdio_proxy_forwarding_and_cli_parsing() -> None:
    parser = build_arg_parser()
    parsed = parser.parse_args(
        ["--url", "https://trails.example.com/mcp", "--token", "tf_pat_demo"]
    )
    assert parsed.url == "https://trails.example.com/mcp"
    assert parsed.token == "tf_pat_demo"
    assert parsed.direct is False
    assert parser.parse_args(["--direct"]).direct is True

    # Verify SSE parser handles multi-event Streamable HTTP responses
    sse_msgs = _parse_sse_jsonrpc_messages(
        'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
    )
    assert len(sse_msgs) == 1

    # Verify live forwarding against mounted ASGI MCP app via httpx.ASGITransport (100% offline!)
    app = _build_test_mcp_app()

    async def _run_proxy_roundtrip() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            req = types.JSONRPCMessage(
                types.JSONRPCRequest(jsonrpc="2.0", id=42, method="prompts/list")
            )
            replies = await forward_jsonrpc_message(
                client, "http://testserver/mcp", req, protocol_version="2025-03-26"
            )
            assert len(replies) == 1
            dumped = replies[0].model_dump(by_alias=True, exclude_none=True)
            assert dumped["id"] == 42
            prompt_names = {p["name"] for p in dumped["result"]["prompts"]}
            assert prompt_names == {
                "plan-weekend-trip",
                "pre-trip-safety-check",
                "find-tracks-for-rig",
            }

            # 401 on /mcp/auth maps cleanly to JSON-RPC error -32001
            err_replies = await forward_jsonrpc_message(client, "http://testserver/mcp/auth", req)
            assert len(err_replies) == 1
            err_dumped = err_replies[0].model_dump(by_alias=True, exclude_none=True)
            assert err_dumped["error"]["code"] == -32001

    anyio.run(_run_proxy_roundtrip)


# =============================================================================
# 2. Full PostGIS Integration Tests Against Seeded Synthetic Alps Fixtures
# =============================================================================


def _truncate_mcp_test_tables(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                TRUNCATE TABLE
                    planned_routes,
                    trips,
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


@pytest.fixture(scope="module")
def mcp_postgis_engine() -> Generator[Engine, None, None]:
    """PostGIS engine tolerant of both 0005 and newer parallel-branch migrations (e.g. 0006)."""
    db_url = resolve_test_database_url()
    engine = get_engine(db_url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        engine.dispose()
        pytest.skip(f"PostGIS database not reachable: {exc}")

    existing_tables = set(inspect(engine).get_table_names(schema="public"))
    required_tables = {"tracks", "track_segments", "planned_routes", "users", "trips"}
    if not required_tables.issubset(existing_tables):
        _ensure_postgis_and_schema(engine)

    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def seeded_mcp_env(
    mcp_postgis_engine: Engine,
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[dict[str, Any], None, None]:
    """Seed synthetic Alps corridor + closures + rainfall + 2 users (A & B) with isolated data."""
    monkeypatch.setenv("TRAILFORGE_PUBLIC_BASE_URL", "https://trails.example.com")
    monkeypatch.setenv("TRAILFORGE_ENABLE_MCP", "true")
    monkeypatch.setenv("TRAILFORGE_MCP_AUTH_MODE", "hybrid")

    _truncate_mcp_test_tables(mcp_postgis_engine)
    SessionLocal = sessionmaker(bind=mcp_postgis_engine, autoflush=False, expire_on_commit=False)

    with SessionLocal() as db:
        dataset = load_corridor_fixtures(fixtures_dir, osm_filename="osm_tracks.geojson")
        ingest_corridor_dataset(db, dataset, commit=True)
        sync_from_fixtures(db, fixtures_dir / "closures_sample.json")
        recompute_live_grades(db, rain_override_72h_mm=28.6)

        tracks = (
            db.execute(select(Track).where(Track.is_active.is_(True)).order_by(Track.name.asc()))
            .scalars()
            .all()
        )
        assert len(tracks) >= 2
        track_ids = [t.track_id for t in tracks]

        # Seed a rainfall snapshot inside Alps corridor
        db.add(
            RainfallSnapshot(
                grid_cell_id="alps_-36.60_148.12",
                centroid_geom=from_shape(Point(148.12, -36.60), srid=4326),
                source_provider="open-meteo",
                rain_24h_mm=12.4,
                rain_72h_mm=28.6,
                rain_7d_mm=42.0,
                wet_risk_level="wet",
            )
        )

        # Create User A and User B with distinct private data
        user_a = User(
            email="alice.private@example.com",
            display_name="Alice Driver",
            password_hash=hash_password("TrailPassA99"),
            role="member",
            is_active=True,
        )
        user_b = User(
            email="bob.secret@example.com",
            display_name="Bob Driver",
            password_hash=hash_password("TrailPassB99"),
            role="member",
            is_active=True,
        )
        db.add_all([user_a, user_b])
        db.flush()

        # User A default rig (Touring 4WD with snorkel) & User B rig (AWD)
        rig_a = VehicleProfile(
            user_id=user_a.id,
            name="Alice 200 Series",
            drivetrain="4wd_full_time",
            has_low_range=True,
            lift_mm=50,
            ground_clearance_mm=245,
            tire_class="AT",
            rear_locker=True,
            has_snorkel=True,
            capability_class="Touring 4WD",
            is_default=True,
        )
        rig_b = VehicleProfile(
            user_id=user_b.id,
            name="Bob Forester",
            drivetrain="awd",
            has_low_range=False,
            lift_mm=0,
            ground_clearance_mm=210,
            tire_class="HT",
            capability_class="AWD",
            is_default=True,
        )
        db.add_all([rig_a, rig_b])

        # Private trips with home-zone raw_geom (MUST NEVER leak in MCP!)
        trip_a = Trip(
            user_id=user_a.id,
            vehicle_capability_class="Touring 4WD",
            title="Alice Secret Home Run",
            visibility="private",
            privacy_trim_m=300.0,
            raw_geom=from_shape(LineString([(148.100, -36.550), (148.120, -36.580)]), srid=4326),
            raw_distance_m=4200.0,
            trimmed_distance_m=3600.0,
            duration_s=1800.0,
            match_status="skipped_private",
        )
        trip_b = Trip(
            user_id=user_b.id,
            vehicle_capability_class="AWD",
            title="Bob Private Weekend",
            visibility="private",
            privacy_trim_m=250.0,
            raw_geom=from_shape(LineString([(148.140, -36.610), (148.150, -36.620)]), srid=4326),
            raw_distance_m=2100.0,
            trimmed_distance_m=1600.0,
            duration_s=900.0,
            match_status="skipped_private",
        )
        db.add_all([trip_a, trip_b])

        # Personal pins & saved routes
        pin_a = UserPin(
            user_id=user_a.id,
            title="Alice Secret Camp",
            category="camp",
            notes="Hidden river bend",
            geom=from_shape(Point(148.122, -36.605), srid=4326),
        )
        pin_b = UserPin(
            user_id=user_b.id,
            title="Bob Private Spot",
            category="waypoint",
            geom=from_shape(Point(148.145, -36.640), srid=4326),
        )
        route_a = PlannedRoute(
            user_id=user_a.id,
            name="Alice High Country Loop",
            track_ids=track_ids[:2],
            waypoints_json=[],
            vehicle_class="Touring 4WD",
        )
        route_b = PlannedRoute(
            user_id=user_b.id,
            name="Bob Solo Track",
            track_ids=track_ids[:1],
            waypoints_json=[],
            vehicle_class="AWD",
        )
        # Seed a community condition report authored by Alice to prove email never leaks publicly
        rep_a = ConditionReport(
            user_id=user_a.id,
            track_id=track_ids[0],
            report_type="locked_gate",
            severity="caution",
            status="active",
            description="# Boundary Gate\nGate closed at northern saddle",
            geom=from_shape(Point(148.121, -36.590), srid=4326),
            observed_at=datetime.now(UTC),
        )
        db.add_all([pin_a, pin_b, route_a, route_b, rep_a])
        db.commit()
        db.refresh(route_a)
        db.refresh(route_b)

        user_a_id = user_a.id
        user_b_id = user_b.id
        route_a_id = str(route_a.id)
        route_b_id = str(route_b.id)
        session_tok_a = create_access_token(
            user_a.id, user_a.email, token_version=user_a.token_version
        )

    # Custom injectable auth resolver supporting scoped synthetic tokens + session JWT fallback
    def _test_auth_resolver(authorization_header: str | None, db: Session) -> McpAuthContext:
        tok = parse_bearer_token(authorization_header)
        if tok == "tf_pat_alice_read_only":
            u = db.get(User, user_a_id)
            return McpAuthContext(user=u, auth_method="pat", scopes=frozenset({SCOPE_READ}))
        if tok == "tf_pat_alice_rw":
            u = db.get(User, user_a_id)
            return McpAuthContext(
                user=u, auth_method="pat", scopes=frozenset({SCOPE_READ, SCOPE_WRITE})
            )
        if tok == "tf_pat_bob_rw":
            u = db.get(User, user_b_id)
            return McpAuthContext(
                user=u, auth_method="pat", scopes=frozenset({SCOPE_READ, SCOPE_WRITE})
            )
        if tok == "tf_pat_revoked":
            return ANONYMOUS_CONTEXT
        return default_auth_resolver(authorization_header, db)

    app = _build_test_mcp_app(
        auth_resolver=_test_auth_resolver,
        db_session_factory=SessionLocal,
    )
    with TestClient(app) as client:
        yield {
            "client": client,
            "track_ids": track_ids,
            "tracks": [(t.track_id, t.name) for t in tracks],
            "route_a_id": route_a_id,
            "route_b_id": route_b_id,
            "session_tok_a": session_tok_a,
            "SessionLocal": SessionLocal,
        }


@pytest.mark.integration
def test_all_public_and_app_tools_end_to_end_offline(seeded_mcp_env: dict[str, Any]) -> None:
    client: TestClient = seeded_mcp_env["client"]
    track_ids: list[str] = seeded_mcp_env["track_ids"]
    first_tid, first_name = seeded_mcp_env["tracks"][0]

    def call(
        tool_name: str, arguments: dict[str, Any], headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        resp = _mcp_rpc(
            client,
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        raw_bytes = len(resp.content)
        assert raw_bytes <= MAX_CALL_TOOL_RESULT_BYTES + 4096
        result = resp.json()["result"]
        assert result.get("isError") is False, f"{tool_name} failed: {result}"
        # Verify strict SC-12 attribution presence on every tool call
        text_blocks = [b["text"] for b in result["content"] if b["type"] == "text"]
        assert text_blocks, f"{tool_name} missing TextContent"
        _assert_has_attributions(text_blocks[0], result.get("structuredContent"))
        # SC-8: Public tools must NEVER leak any user's email or private trips/pins
        serialized = json.dumps(result)
        assert "alice.private@example.com" not in serialized
        assert "bob.secret@example.com" not in serialized
        assert "Alice Secret Home Run" not in serialized
        assert "Alice Secret Camp" not in serialized
        return result

    # 1. search_trails (default include_images=False, then include_images=True)
    s_res = call("search_trails", {"region": "australian-alps", "limit": 10})
    sc = s_res["structuredContent"]
    assert sc["schema_version"] == "1.0"
    assert sc["view_mode"] == "explore"
    assert len(sc["explore"]["tracks"]) >= 2
    assert any(len(t.get("preview_coords") or []) >= 2 for t in sc["explore"]["tracks"])
    assert all("coords_preview" not in t for t in sc["explore"]["tracks"])
    assert all(p.get("category") == p.get("poi_type") for p in sc["explore"]["pois"])
    assert not any(b["type"] == "image" for b in s_res["content"])

    s_img = call(
        "search_trails", {"region": "High Country", "open_only": True, "include_images": True}
    )
    img_blocks = [b for b in s_img["content"] if b["type"] == "image"]
    assert len(img_blocks) == 1
    _assert_valid_png_block(img_blocks[0], (960, 540))

    # 2. inspect_track (by track_id + default PNGs + by track_name)
    i_res = call("inspect_track", {"track_id": first_tid, "vehicle_class": "Stock 4WD"})
    i_sc = i_res["structuredContent"]
    assert i_sc["view_mode"] == "track"
    assert i_sc["track"]["summary"]["track_id"] == first_tid
    assert f"/?track={first_tid}" in i_sc["deep_link_url"]
    assert i_sc["track"]["summary"]["suitability"]["rating"] in (
        "Suited",
        "Challenging",
        "High Risk for your rig",
    )
    i_imgs = [b for b in i_res["content"] if b["type"] == "image"]
    assert len(i_imgs) == 2
    _assert_valid_png_block(i_imgs[0], (960, 420))
    _assert_valid_png_block(i_imgs[1], (960, 540))

    # Fuzzy name lookup resolves unique name and disambiguates ambiguous prefix
    i_name = call("inspect_track", {"track_name": first_name, "include_images": False})
    assert i_name["structuredContent"]["track"]["summary"]["track_id"] == first_tid
    i_ambig = call("inspect_track", {"track_name": "Davies Plain", "include_images": False})
    assert len(i_ambig["structuredContent"]["disambiguation"]) >= 2

    # 3. plan_route (multi-track + deep link ?route=&rig= + PNGs)
    p_res = call(
        "plan_route",
        {
            "track_ids": track_ids[:2],
            "name": "Alps Weekend",
            "vehicle_class": "Touring 4WD",
            "waypoints": [{"name": "Lunch Stop", "category": "camp", "lon": 148.12, "lat": -36.60}],
        },
    )
    p_sc = p_res["structuredContent"]
    assert p_sc["view_mode"] == "route"
    assert len(p_sc["route"]["legs"]) == 2
    assert f"/?route={track_ids[0]},{track_ids[1]}" in p_sc["deep_link_url"]
    assert "rig=Touring" in p_sc["deep_link_url"]
    assert "gridforge render" in p_sc["route"]["gridforge_cli"]
    p_imgs = [b for b in p_res["content"] if b["type"] == "image"]
    assert len(p_imgs) == 2
    _assert_valid_png_block(p_imgs[0], (960, 540))
    _assert_valid_png_block(p_imgs[1], (960, 420))

    # 4. compare_tracks (2 tracks + comparison PNG)
    c_res = call("compare_tracks", {"track_ids": track_ids[:2], "vehicle_class": "AWD"})
    c_sc = c_res["structuredContent"]
    assert c_sc["view_mode"] == "compare"
    assert len(c_sc["compare"]["tracks"]) == 2
    c_imgs = [b for b in c_res["content"] if b["type"] == "image"]
    assert len(c_imgs) == 1
    _assert_valid_png_block(c_imgs[0], (960, 460))

    # 5. check_area_conditions (closures + community gate report + rainfall + PNG)
    cond_res = call("check_area_conditions", {"region": "australian-alps"})
    cond_sc = cond_res["structuredContent"]
    assert cond_sc["view_mode"] == "explore"
    assert len(cond_sc["explore"]["reports"]) >= 1
    assert cond_sc["explore"]["rainfall_summary"]["wet_risk_level"] == "wet"
    cond_imgs = [b for b in cond_res["content"] if b["type"] == "image"]
    assert len(cond_imgs) == 1
    _assert_valid_png_block(cond_imgs[0], (960, 540))

    # 6. find_pois_near (verifies W3-compatible category field alongside poi_type)
    poi_res = call("find_pois_near", {"track_id": first_tid, "radius_km": 20})
    assert poi_res["structuredContent"]["view_mode"] == "explore"
    found_pois = poi_res["structuredContent"]["explore"]["pois"]
    assert len(found_pois) >= 1
    assert all(p["category"] == p["poi_type"] for p in found_pois)

    # 7. export_route_file (GPX + GeoJSON)
    gpx_res = call("export_route_file", {"track_ids": track_ids[:2], "format": "gpx"})
    assert gpx_res["structuredContent"]["inlined"] is True
    assert "<gpx" in gpx_res["structuredContent"]["content"]
    assert "gridforge render" in gpx_res["structuredContent"]["gridforge_cli"]

    gj_res = call("export_route_file", {"track_ids": track_ids[:2], "format": "geojson"})
    assert gj_res["structuredContent"]["format"] == "geojson"
    assert '"FeatureCollection"' in gj_res["structuredContent"]["content"]

    # 8. list_regions_and_packs
    reg_res = call("list_regions_and_packs", {"state": "VIC"})
    assert any(r["region_id"] == "australian-alps" for r in reg_res["structuredContent"]["regions"])

    # 9. open_trailforge_map (empty {} AND track_id AND track_ids)
    map_empty = call("open_trailforge_map", {})
    assert map_empty["structuredContent"]["view_mode"] == "explore"
    map_trk = call("open_trailforge_map", {"track_id": first_tid})
    assert map_trk["structuredContent"]["view_mode"] == "track"
    map_rt = call("open_trailforge_map", {"track_ids": track_ids[:2]})
    assert map_rt["structuredContent"]["view_mode"] == "route"

    # 10-12. App-only tools (compact structuredContent, no PNGs)
    app_trk = call("app_get_track_detail", {"track_id": first_tid})
    assert app_trk["structuredContent"]["view_mode"] == "track"
    assert not any(b["type"] == "image" for b in app_trk["content"])

    app_rt = call("app_plan_route_preview", {"track_ids": track_ids[:2]})
    assert app_rt["structuredContent"]["view_mode"] == "route"
    assert not any(b["type"] == "image" for b in app_rt["content"])

    app_vp = call("app_search_viewport", {"bbox": [147.9, -36.8, 148.3, -36.4], "limit": 20})
    assert app_vp["structuredContent"]["view_mode"] == "explore"
    assert len(app_vp["structuredContent"]["explore"]["tracks"]) >= 2


@pytest.mark.integration
def test_personal_tools_scope_enforcement_and_cross_user_isolation(
    seeded_mcp_env: dict[str, Any],
) -> None:
    client: TestClient = seeded_mcp_env["client"]
    track_ids: list[str] = seeded_mcp_env["track_ids"]
    route_a_id: str = seeded_mcp_env["route_a_id"]

    alice_ro = {"Authorization": "Bearer tf_pat_alice_read_only"}
    alice_rw = {"Authorization": "Bearer tf_pat_alice_rw"}
    bob_rw = {"Authorization": "Bearer tf_pat_bob_rw"}
    alice_session = {"Authorization": f"Bearer {seeded_mcp_env['session_tok_a']}"}

    def call_raw(
        tool_name: str,
        arguments: dict[str, Any],
        headers: dict[str, str] | None = None,
        path: str = "/mcp",
    ) -> dict[str, Any]:
        resp = _mcp_rpc(
            client,
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            headers=headers,
            path=path,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["result"]

    # 1. Anonymous calls to all 6 personal/write tools are rejected (isError=True)
    for tname, targs in (
        ("get_my_garage", {}),
        ("list_my_saved_routes", {}),
        ("list_my_trips_and_pins", {}),
        ("save_planned_route", {"name": "Unauth", "track_ids": track_ids[:1]}),
        ("save_personal_pin", {"title": "Unauth", "lat": -36.6, "lon": 148.12}),
        ("toggle_favorite_track", {"track_id": track_ids[0], "action": "add"}),
    ):
        err = call_raw(tname, targs)
        assert err["isError"] is True

    # 2. Read-only PAT for Alice succeeds on personal read tools & auto-resolves default rig
    garage = call_raw("get_my_garage", {}, headers=alice_ro, path="/mcp/auth")
    assert garage["isError"] is False
    vehicles = garage["structuredContent"]["vehicles"]
    assert len(vehicles) == 1
    assert vehicles[0]["name"] == "Alice 200 Series"
    assert vehicles[0]["capability_class"] == "Touring 4WD"
    assert "Bob" not in json.dumps(garage)

    # Auto-resolved rig context when vehicle_class omitted by signed-in user (preserves snorkel!)
    insp = call_raw(
        "inspect_track", {"track_id": track_ids[0], "include_images": False}, headers=alice_ro
    )
    assert insp["isError"] is False
    assert insp["structuredContent"]["rig_context"]["vehicle_name"] == "Alice 200 Series"
    assert insp["structuredContent"]["rig_context"]["has_snorkel"] is True
    # Suitability evaluated with Alice's saved rig (has_snorkel=True -> no snorkel warning)
    suit = insp["structuredContent"]["track"]["summary"]["suitability"]
    assert suit is not None
    assert suit["vehicle_class"] == "Touring 4WD"
    assert not any("snorkel recommended" in r.lower() for r in suit.get("reasons") or [])

    routes = call_raw("list_my_saved_routes", {}, headers=alice_ro)
    assert routes["isError"] is False
    assert [r["name"] for r in routes["structuredContent"]["routes"]] == ["Alice High Country Loop"]

    # Trips metadata only — NEVER raw_geom or raw home-zone coordinates!
    trips_pins = call_raw("list_my_trips_and_pins", {}, headers=alice_ro)
    assert trips_pins["isError"] is False
    tp_sc = trips_pins["structuredContent"]
    assert len(tp_sc["trips"]) == 1
    assert tp_sc["trips"][0]["title"] == "Alice Secret Home Run"
    assert "raw_geom" not in tp_sc["trips"][0]
    assert "148.1" not in json.dumps(tp_sc["trips"])
    assert [p["title"] for p in tp_sc["pins"]] == ["Alice Secret Camp"]
    assert "Bob" not in json.dumps(tp_sc)

    # 3. Read-only PAT is REJECTED on all 3 private write tools (SC-6 / SC-8)
    assert (
        call_raw(
            "save_planned_route", {"name": "Blocked", "track_ids": track_ids[:1]}, headers=alice_ro
        )["isError"]
        is True
    )
    assert (
        call_raw(
            "save_personal_pin", {"title": "Blocked", "lat": -36.6, "lon": 148.12}, headers=alice_ro
        )["isError"]
        is True
    )
    assert (
        call_raw(
            "toggle_favorite_track", {"track_id": track_ids[0], "action": "add"}, headers=alice_ro
        )["isError"]
        is True
    )

    # 4. Write-scoped PAT for Alice succeeds on all 3 private write tools
    saved_rt = call_raw(
        "save_planned_route",
        {
            "name": "Alice AI Planned Trip",
            "track_ids": track_ids[:2],
            "notes": "Packed recovery gear",
        },
        headers=alice_rw,
        path="/mcp/auth",
    )
    assert saved_rt["isError"] is False
    assert saved_rt["structuredContent"]["view_mode"] == "route"
    new_route_id = saved_rt["structuredContent"]["route"]["saved_route_id"]
    assert new_route_id is not None

    # Update owned route succeeds
    upd_rt = call_raw(
        "save_planned_route",
        {"route_id": new_route_id, "name": "Alice Renamed Trip", "track_ids": track_ids[:2]},
        headers=alice_rw,
    )
    assert upd_rt["isError"] is False
    assert upd_rt["structuredContent"]["route"]["name"] == "Alice Renamed Trip"

    # Save personal pin & toggle favorite (add + remove)
    saved_pin = call_raw(
        "save_personal_pin",
        {"title": "Creek Water Point", "lat": -36.612, "lon": 148.125, "category": "water"},
        headers=alice_rw,
    )
    assert saved_pin["isError"] is False
    assert saved_pin["structuredContent"]["pin"]["category"] == "water"

    fav_add = call_raw(
        "toggle_favorite_track",
        {"track_id": track_ids[0], "action": "add", "notes": "Must drive in autumn"},
        headers=alice_rw,
    )
    assert fav_add["isError"] is False
    assert fav_add["structuredContent"]["is_favorite"] is True

    fav_rm = call_raw(
        "toggle_favorite_track",
        {"track_id": track_ids[0], "action": "remove"},
        headers=alice_rw,
    )
    assert fav_rm["isError"] is False
    assert fav_rm["structuredContent"]["is_favorite"] is False

    # 5. Strict cross-user isolation: Bob CANNOT see or modify Alice's data!
    bob_garage = call_raw("get_my_garage", {}, headers=bob_rw)
    assert [v["name"] for v in bob_garage["structuredContent"]["vehicles"]] == ["Bob Forester"]
    bob_routes = call_raw("list_my_saved_routes", {}, headers=bob_rw)
    assert [r["name"] for r in bob_routes["structuredContent"]["routes"]] == ["Bob Solo Track"]
    bob_tp = call_raw("list_my_trips_and_pins", {}, headers=bob_rw)
    assert "Alice" not in json.dumps(bob_tp)

    # Bob attempting to overwrite Alice's route_id MUST fail and leave Alice's route untouched
    cross_attack = call_raw(
        "save_planned_route",
        {"route_id": route_a_id, "name": "Hijacked!", "track_ids": track_ids[:1]},
        headers=bob_rw,
    )
    assert cross_attack["isError"] is True
    with seeded_mcp_env["SessionLocal"]() as check_db:
        still_alice = check_db.get(PlannedRoute, uuid.UUID(route_a_id))
        assert still_alice is not None
        assert still_alice.name == "Alice High Country Loop"

    # 6. Existing session JWT works via default_auth_resolver fallback as well
    sess_garage = call_raw("get_my_garage", {}, headers=alice_session, path="/mcp/auth")
    assert sess_garage["isError"] is False
    assert sess_garage["structuredContent"]["vehicles"][0]["name"] == "Alice 200 Series"
