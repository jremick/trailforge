"""Regression tests for Sprint 2 performance optimizations and medium security hardening."""

from __future__ import annotations

import gzip
import socket
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from trailforge.api.middleware import (
    CookieCSRFMiddleware,
    PathAwareCORSMiddleware,
    SelectiveGZipMiddleware,
)
from trailforge.api.packs import _PACK_BUILD_LOCKS, _get_region_build_lock
from trailforge.auth.oauth import OAuthError, validate_resource_indicator
from trailforge.config import Settings
from trailforge.db.models import TrackSegmentGrade
from trailforge.feeds.base import _assert_safe_feed_url
from trailforge.ingestion.terrain import read_raster_bounds
from trailforge.matching import parser as gpx_parser


def test_track_segment_grade_hotspot_column_sync() -> None:
    """Assigning live_breakdown automatically syncs is_obstacle_hotspot."""
    g = TrackSegmentGrade(
        segment_id="seg-hotspot-1",
        baseline_grade="Medium",
        baseline_score=40.0,
        baseline_breakdown={},
        live_breakdown={"is_obstacle_hotspot": True, "sample_count": 4},
        effective_grade="Hard",
    )
    assert g.is_obstacle_hotspot is True

    g.live_breakdown = {"is_obstacle_hotspot": False, "sample_count": 4}
    assert g.is_obstacle_hotspot is False

    g.live_breakdown = {"is_obstacle_hotspot": True}
    assert g.is_obstacle_hotspot is True
    g.live_breakdown = None
    assert g.is_obstacle_hotspot is False


def test_validate_resource_indicator_enforces_canonical_mcp_uris() -> None:
    """SEC-M4: RFC 8707 resource parameter must match canonical /mcp or /mcp/auth URI."""
    origin = "https://trails.example.com"
    assert (
        validate_resource_indicator("https://trails.example.com/mcp", expected_origin=origin)
        == "https://trails.example.com/mcp"
    )
    assert (
        validate_resource_indicator("https://trails.example.com/mcp/auth", expected_origin=origin)
        == "https://trails.example.com/mcp/auth"
    )
    assert validate_resource_indicator(None, expected_origin=origin) is None

    for bad in (
        "https://evil.example.com/mcp",
        "https://trails.example.com/api/admin",
        "http://trails.example.com/mcp",
        "https://trails.example.com/mcp?foo=bar",
        "https://trails.example.com/mcp#frag",
    ):
        with pytest.raises(OAuthError) as exc_info:
            validate_resource_indicator(bad, expected_origin=origin)
        assert exc_info.value.error == "invalid_target"


def test_cookie_csrf_referer_fallback_and_prod_fail_closed() -> None:
    """SEC-M5: CookieCSRFMiddleware allows matching Referer and fails closed in production."""
    prod_cfg = Settings(
        cors_origins="https://trails.example.com",
        auth_cookie_secure=True,
        auth_secret_key="a" * 40,
        privacy_secret_key="b" * 40,
        public_base_url="https://trails.example.com",
    )
    app = FastAPI()
    app.add_middleware(CookieCSRFMiddleware, settings=prod_cfg)

    @app.post("/api/user/pins")
    def _create_pin() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        client.cookies.set("tf_session", "dummy-session")

        # Missing Sec-Fetch-Site, Origin, and Referer -> 403 in production mode
        r_missing = client.post("/api/user/pins")
        assert r_missing.status_code == 403

        # Matching Referer fallback passes
        r_ref_ok = client.post(
            "/api/user/pins",
            headers={"Referer": "https://trails.example.com/map?track=1"},
        )
        assert r_ref_ok.status_code == 200

        # Mismatched Referer rejected
        r_ref_bad = client.post(
            "/api/user/pins",
            headers={"Referer": "https://evil.example.com/phish"},
        )
        assert r_ref_bad.status_code == 403


def test_path_aware_cors_non_credentialed_for_oauth_endpoints() -> None:
    """SEC-M7: /api/oauth/{token,register,revoke} use non-credentialed CORS."""
    app = FastAPI()
    app.add_middleware(
        PathAwareCORSMiddleware,
        allow_origins=["https://trails.example.com"],
    )

    @app.post("/api/oauth/token")
    def _token() -> dict[str, str]:
        return {"access_token": "ok"}

    @app.get("/api/tracks")
    def _tracks() -> dict[str, list[str]]:
        return {"tracks": []}

    with TestClient(app) as client:
        r_oauth = client.options(
            "/api/oauth/token",
            headers={
                "Origin": "https://claude.ai",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert r_oauth.status_code == 204
        assert r_oauth.headers.get("access-control-allow-origin") == "https://claude.ai"
        assert r_oauth.headers.get("access-control-allow-credentials") is None

        # Regular /api/* endpoints remain restricted to configured allow_origins
        r_api = client.get("/api/tracks", headers={"Origin": "https://claude.ai"})
        assert r_api.headers.get("access-control-allow-origin") is None


def test_selective_gzip_compresses_json_and_bypasses_tiles_and_ranges() -> None:
    """SelectiveGZipMiddleware compresses JSON >= 1 KB, skips /api/tiles/ and Range requests."""
    app = FastAPI()
    app.add_middleware(SelectiveGZipMiddleware, minimum_size=256)

    payload = {"items": ["trail-segment-payload-" + str(i) for i in range(60)]}

    @app.get("/api/tracks")
    def _tracks() -> JSONResponse:
        return JSONResponse(payload)

    @app.get("/api/tiles/base.pmtiles")
    def _tiles(request: Request) -> JSONResponse:
        return JSONResponse(payload)

    with TestClient(app) as client:
        r_json = client.get("/api/tracks", headers={"Accept-Encoding": "gzip"})
        assert r_json.status_code == 200
        assert r_json.headers.get("content-encoding") == "gzip"
        assert "Accept-Encoding" in r_json.headers.get("vary", "")

        # Range requests must bypass gzip so byte offsets stay valid
        r_range = client.get(
            "/api/tracks",
            headers={"Accept-Encoding": "gzip", "Range": "bytes=0-127"},
        )
        assert r_range.headers.get("content-encoding") is None

        # Tile paths bypass gzip
        r_tile = client.get("/api/tiles/base.pmtiles", headers={"Accept-Encoding": "gzip"})
        assert r_tile.headers.get("content-encoding") is None


def test_gpx_parser_enforces_max_xml_elements(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEC-M6: Streaming GPX parser rejects files exceeding MAX_XML_ELEMENTS."""
    monkeypatch.setattr(gpx_parser, "MAX_XML_ELEMENTS", 15)
    pts = "".join(
        f'<trkpt lat="{-37.1 - i * 0.0001:.5f}" lon="146.5"><ele>500</ele></trkpt>'
        for i in range(20)
    )
    xml_bytes = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<gpx version="1.1"><trk><trkseg>{pts}</trkseg></trk></gpx>'
    ).encode()
    with pytest.raises(ValueError, match="maximum allowed"):
        gpx_parser.parse_gpx(xml_bytes)


def test_ssrf_feed_guard_rejects_private_and_loopback_targets() -> None:
    """SEC-M9: _assert_safe_feed_url rejects non-global / loopback / metadata targets."""
    for blocked in (
        "http://127.0.0.1/internal",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/feed",
        "http://192.168.1.10/feed",
        "file:///etc/passwd",
    ):
        with pytest.raises(ValueError):
            _assert_safe_feed_url(httpx.URL(blocked))

    # DNS resolving to private IP must also be rejected
    fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.30.40", 443))]
    with (
        patch("trailforge.feeds.base.socket.getaddrinfo", return_value=fake_addrinfo),
        pytest.raises(ValueError, match="non-public IP"),
    ):
        _assert_safe_feed_url(httpx.URL("https://rebind.example.com/feed.json"))

    # Public IP passes
    pub_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    with patch("trailforge.feeds.base.socket.getaddrinfo", return_value=pub_addrinfo):
        _assert_safe_feed_url(httpx.URL("https://feeds.example.com/closures.json"))


def test_read_raster_bounds_avoids_pixel_decode(tmp_path: Path) -> None:
    """read_raster_bounds reads GeoTIFF header bounds without decoding raster pixels."""
    rasterio = pytest.importorskip("rasterio")
    import numpy as np
    from rasterio.transform import from_origin

    tif_path = tmp_path / "mini_dem.tif"
    data = np.full((16, 16), 500.0, dtype=np.float32)
    with rasterio.open(
        tif_path,
        "w",
        driver="GTiff",
        height=16,
        width=16,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(146.0, -37.0, 0.01, 0.01),
    ) as dst:
        dst.write(data, 1)

    orig_open = rasterio.open

    def _forbid_read(*args: object, **kwargs: object) -> object:
        ds = orig_open(*args, **kwargs)

        def _boom(*_a: object, **_k: object) -> None:
            raise AssertionError("read_raster_bounds must not decode raster bands")

        ds.read = _boom
        return ds

    with patch.object(rasterio, "open", side_effect=_forbid_read):
        bounds = read_raster_bounds(tif_path)

    assert bounds[0] == pytest.approx(146.0)
    assert bounds[1] == pytest.approx(-37.16)
    assert bounds[2] == pytest.approx(146.16)
    assert bounds[3] == pytest.approx(-37.0)


def test_pack_build_mutex_prevents_concurrent_same_region_builds() -> None:
    """Per-region pack build lock rejects concurrent acquisition for the same region."""
    region_id = f"test-region-{uuid.uuid4().hex[:8]}"
    try:
        lock = _get_region_build_lock(region_id)
        assert lock.acquire(blocking=False) is True
        try:
            assert _get_region_build_lock(region_id).acquire(blocking=False) is False
        finally:
            lock.release()
        assert lock.acquire(blocking=False) is True
        lock.release()
    finally:
        _PACK_BUILD_LOCKS.pop(region_id, None)


def test_gzip_response_payload_roundtrip() -> None:
    """Smoke check that gzip payload decompression matches JSON."""
    raw = b'{"status":"ok"}'
    assert gzip.decompress(gzip.compress(raw)) == raw
