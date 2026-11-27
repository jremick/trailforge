"""Offline unit & PostGIS integration tests for Valhalla sidecar matcher & high-rate mobile IMU."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import shutil
import subprocess
from collections.abc import Generator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pyproj import Transformer
from shapely import STRtree
from shapely.geometry import Point
from sqlalchemy import Engine, func, inspect, select, text
from sqlalchemy.orm import Session

from trailforge.api.main import create_app
from trailforge.api.trips import get_track_matcher as api_get_track_matcher
from trailforge.config import Settings
from trailforge.db.models import TelemetryObservation
from trailforge.db.session import get_db
from trailforge.ingestion.conflate import conflate_tracks
from trailforge.ingestion.connectors.fixtures import load_corridor_fixtures
from trailforge.ingestion.pipeline import ingest_corridor_dataset
from trailforge.ingestion.segmenter import segment_conflated_ways
from trailforge.matching import (
    GpsPoint,
    ImuSample,
    PostGISHMMMatcher,
    TrackMatcher,
    ValhallaMatcherError,
    ValhallaTrackMatcher,
    align_imu_to_trimmed_points,
    compute_pitch_roll_deg,
    decode_polyline6,
    encode_polyline6,
    get_track_matcher,
    parse_gpx,
    process_imu_batch,
    trim_privacy_buffer,
)
from trailforge.matching.hmm import _CorridorSegment

REPO_ROOT = Path(__file__).resolve().parent.parent
IN_REPO_CHECKOUT = (REPO_ROOT / "docker-compose.yml").is_file()
requires_repo_checkout = pytest.mark.skipif(
    not IN_REPO_CHECKOUT,
    reason="Repo-root deployment manifests are not copied into the minimal runtime container image",
)
_WGS84_TO_ALBERS = Transformer.from_crs("EPSG:4326", "EPSG:3577", always_xy=True)


# -----------------------------------------------------------------------------
# Helpers for deterministic offline corridor & mock Valhalla /trace_attributes
# -----------------------------------------------------------------------------


class _InMemoryHMMFallback(PostGISHMMMatcher):
    def __init__(self, corridor: list[_CorridorSegment]) -> None:
        dummy_session: Any = None
        super().__init__(dummy_session)
        self._corridor = corridor

    def _fetch_corridor_segments(
        self,
        points: Sequence[GpsPoint],
        search_radius_m: float,
    ) -> list[_CorridorSegment]:
        return list(self._corridor)


def _build_offline_corridor_segments(
    fixtures_dir: Path,
) -> tuple[list[_CorridorSegment], dict[str, str]]:
    dataset = load_corridor_fixtures(fixtures_dir)
    conflated = conflate_tracks(
        [*dataset.osm_tracks, *dataset.vic_tracks, *dataset.nsw_tracks]
    ).ways
    tracks, candidates = segment_conflated_ways(conflated)
    track_names = {t.track_id: t.name for t in tracks}
    vehicular = [seg for seg in candidates if seg.is_vehicular]
    corridor: list[_CorridorSegment] = []
    seg_to_track_name: dict[str, str] = {}
    for idx, seg in enumerate(vehicular):
        coords_3577 = list(seg.geom_3577.coords)
        corridor.append(
            _CorridorSegment(
                idx=idx,
                segment_id=seg.segment_id,
                track_id=seg.track_id,
                start_node_key=seg.start_node_key,
                end_node_key=seg.end_node_key,
                length_m=seg.length_m,
                bearing_deg=seg.bearing_deg,
                geom_wgs84=seg.geom_wgs84,
                geom_3577=seg.geom_3577,
                start_xy=(float(coords_3577[0][0]), float(coords_3577[0][1])),
                end_xy=(float(coords_3577[-1][0]), float(coords_3577[-1][1])),
            )
        )
        if seg.track_id is not None:
            seg_to_track_name[seg.segment_id] = track_names[seg.track_id]
    return corridor, seg_to_track_name


def _build_valhalla_mock_response_from_corridor(
    shape_input: Sequence[dict[str, Any]],
    corridor: Sequence[_CorridorSegment],
    *,
    snap_radius_m: float = 40.0,
) -> dict[str, Any]:
    """Synthesize a realistic Valhalla `/trace_attributes` response against corridor centerlines."""
    tree = STRtree([seg.geom_3577 for seg in corridor])
    matched_points: list[dict[str, Any]] = []
    shape_latlon: list[tuple[float, float]] = []
    edges: list[dict[str, Any]] = []
    edge_index_by_seg: dict[str, int] = {}

    for item in shape_input:
        lat = float(item["lat"])
        lon = float(item["lon"])
        px, py = _WGS84_TO_ALBERS.transform(lon, lat)
        pt_3577 = Point(px, py)
        best_seg: _CorridorSegment | None = None
        best_dist = float("inf")
        for idx_raw in tree.query(pt_3577.buffer(snap_radius_m)):
            seg = corridor[int(idx_raw)]
            d = float(pt_3577.distance(seg.geom_3577))
            if d <= snap_radius_m and d < best_dist:
                best_dist = d
                best_seg = seg

        if best_seg is None:
            matched_points.append(
                {
                    "lat": lat,
                    "lon": lon,
                    "type": "unmatched",
                    "edge_index": 18446744073709551615,
                    "distance_from_trace_point": 0.0,
                }
            )
            continue

        proj_m = float(best_seg.geom_3577.project(pt_3577))
        frac = max(0.0, min(1.0, proj_m / best_seg.length_m)) if best_seg.length_m > 0 else 0.0
        snapped_wgs = best_seg.geom_wgs84.interpolate(frac, normalized=True)
        slat = round(float(snapped_wgs.y), 6)
        slon = round(float(snapped_wgs.x), 6)
        shape_latlon.append((slat, slon))

        if best_seg.segment_id not in edge_index_by_seg:
            edge_idx = len(edges)
            edge_index_by_seg[best_seg.segment_id] = edge_idx
            edges.append(
                {
                    "way_id": 10000 + edge_idx,
                    "length": round(best_seg.length_m / 1000.0, 4),
                    "speed": 20,
                    "begin_shape_index": max(0, len(shape_latlon) - 1),
                    "end_shape_index": len(shape_latlon),
                }
            )
        else:
            edge_idx = edge_index_by_seg[best_seg.segment_id]

        matched_points.append(
            {
                "lat": slat,
                "lon": slon,
                "type": "matched",
                "edge_index": edge_idx,
                "distance_from_trace_point": round(best_dist, 2),
                "distance_along_edge": round(frac, 4),
            }
        )

    return {
        "units": "kilometers",
        "shape": encode_polyline6(shape_latlon),
        "edges": edges,
        "matched_points": matched_points,
    }


def _generate_imu_samples_for_trace(
    raw_points: Sequence[GpsPoint],
    *,
    trimmed_start: datetime,
    trimmed_end: datetime,
    hz: int = 10,
    inject_buffer_shocks: bool = True,
    interior_shock_offsets_s: Sequence[float] = (120.0, 300.0),
) -> list[ImuSample]:
    """Generate deterministic IMU samples over a GPS trace, optionally injecting buffer shocks."""
    t0_ms = raw_points[0].timestamp.timestamp() * 1000.0
    t_end_ms = raw_points[-1].timestamp.timestamp() * 1000.0
    trim_s_ms = trimmed_start.timestamp() * 1000.0
    trim_e_ms = trimmed_end.timestamp() * 1000.0
    step_ms = 1000.0 / hz

    shock_targets_ms = [trim_s_ms + s * 1000.0 for s in interior_shock_offsets_s]
    samples: list[ImuSample] = []
    cur = t0_ms
    i = 0
    while cur <= t_end_ms:
        in_buffer = cur < trim_s_ms or cur > trim_e_ms
        if in_buffer and inject_buffer_shocks:
            # Extreme fake driveway/campsite bump + tilt inside the 250 m privacy buffer
            is_buf_spike = i % (hz * 2) == 0
            az = 4.2 if is_buf_spike else 1.0
            samples.append(
                ImuSample(
                    t_ms=cur,
                    ax=0.05,
                    ay=0.04,
                    az=az,
                    pitch_deg=38.5 if is_buf_spike else 2.0,
                    roll_deg=33.0 if is_buf_spike else 1.5,
                )
            )
        else:
            # Moderate corrugated trail interior + controlled shock impacts
            corrugation = 0.12 * math.sin(2.0 * math.pi * 0.4 * i)
            hit_shock = any(abs(cur - st) < step_ms * 0.5 for st in shock_targets_ms)
            az = 1.0 + corrugation + (1.45 if hit_shock else 0.0)
            pitch = 19.0 if hit_shock else (8.0 + 2.0 * math.sin(i * 0.05))
            roll = 14.0 if hit_shock else (4.0 + 1.5 * math.cos(i * 0.04))
            samples.append(
                ImuSample(
                    t_ms=cur,
                    ax=0.02,
                    ay=-0.01,
                    az=round(az, 4),
                    pitch_deg=round(pitch, 2),
                    roll_deg=round(roll, 2),
                )
            )
        cur += step_ms
        i += 1
    return samples


# -----------------------------------------------------------------------------
# Pure Offline Unit Tests (Zero DB, Zero External Network)
# -----------------------------------------------------------------------------


def test_config_defaults_env_overrides_and_matcher_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "TRAILFORGE_MATCHER_BACKEND",
        "MATCHER_BACKEND",
        "VALHALLA_URL",
        "VALHALLA_TIMEOUT_S",
        "VALHALLA_FALLBACK_TO_HMM",
        "TRAILFORGE_ENABLED_STATES",
        "ENABLED_STATES",
    ):
        monkeypatch.delenv(key, raising=False)

    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    assert cfg.matcher_backend == "postgis_hmm"
    assert cfg.valhalla_url == "http://valhalla:8002"
    assert cfg.valhalla_timeout_s == pytest.approx(10.0)
    assert cfg.valhalla_fallback_to_hmm is True
    assert cfg.enabled_states == "VIC,NSW"
    assert cfg.enabled_states_list == ["VIC", "NSW"]

    default_matcher = get_track_matcher(cfg)
    assert isinstance(default_matcher, PostGISHMMMatcher)
    assert isinstance(default_matcher, TrackMatcher)

    monkeypatch.setenv("TRAILFORGE_MATCHER_BACKEND", "valhalla")
    monkeypatch.setenv("VALHALLA_URL", "http://127.0.0.1:8002")
    monkeypatch.setenv("VALHALLA_TIMEOUT_S", "4.5")
    monkeypatch.setenv("VALHALLA_FALLBACK_TO_HMM", "false")
    monkeypatch.setenv("TRAILFORGE_ENABLED_STATES", "VIC,NSW,ACT,TAS")

    cfg_val = Settings(_env_file=None)  # type: ignore[call-arg]
    assert cfg_val.matcher_backend == "valhalla"
    assert cfg_val.valhalla_url == "http://127.0.0.1:8002"
    assert cfg_val.valhalla_timeout_s == pytest.approx(4.5)
    assert cfg_val.valhalla_fallback_to_hmm is False
    assert cfg_val.enabled_states_list == ["VIC", "NSW", "ACT", "TAS"]

    val_matcher = get_track_matcher(cfg_val)
    assert isinstance(val_matcher, ValhallaTrackMatcher)
    assert isinstance(val_matcher, TrackMatcher)
    assert val_matcher.valhalla_url == "http://127.0.0.1:8002"
    assert val_matcher.timeout_s == pytest.approx(4.5)
    assert val_matcher.fallback_to_hmm is False


def test_polyline6_encode_decode_roundtrip_and_error_handling() -> None:
    assert decode_polyline6("") == []
    assert encode_polyline6([]) == ""

    au_coords = [
        (-36.561234, 148.119876),  # Tom Groggin (VIC/NSW)
        (-36.597812, 148.124321),  # Davies Plain escarpment
        (-36.645901, 148.130115),  # Davies Plain Hut
        (-33.868820, 151.209290),  # Sydney
        (-37.813628, 144.963058),  # Melbourne
        (-12.463440, 130.845642),  # Darwin
    ]
    encoded = encode_polyline6(au_coords)
    assert isinstance(encoded, str) and len(encoded) > 10
    decoded = decode_polyline6(encoded)
    assert len(decoded) == len(au_coords)
    for (exp_lat, exp_lon), (got_lat, got_lon) in zip(au_coords, decoded, strict=True):
        assert got_lat == pytest.approx(exp_lat, abs=1e-6)
        assert got_lon == pytest.approx(exp_lon, abs=1e-6)

    # Known precision-5 vector check (Google Maps documentation vector)
    google_vec = decode_polyline6("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
    assert google_vec == [
        pytest.approx((38.5, -120.2), abs=1e-5),
        pytest.approx((40.7, -120.95), abs=1e-5),
        pytest.approx((43.252, -126.453), abs=1e-5),
    ]

    with pytest.raises(ValueError, match="Truncated"):
        decode_polyline6(encoded[:-1])

    with pytest.raises(ValueError, match="Invalid Polyline6"):
        decode_polyline6("abc\x10def")


def test_valhalla_offline_matches_davies_plain_and_verifies_request_payload(
    fixtures_dir: Path,
) -> None:
    corridor, seg_to_track_name = _build_offline_corridor_segments(fixtures_dir)
    parsed = parse_gpx((fixtures_dir / "sample_trace_davies_plain.gpx").read_bytes())
    enriched = [
        pt.model_copy(update={"roughness_rms": round(0.18 + (i % 4) * 0.05, 3)})
        for i, pt in enumerate(parsed.points)
    ]
    trimmed = trim_privacy_buffer(enriched, trim_m=250.0)
    assert not trimmed.too_short

    captured_requests: list[tuple[str, dict[str, Any]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured_requests.append((str(request.url), body))
        resp_json = _build_valhalla_mock_response_from_corridor(body["shape"], corridor)
        return httpx.Response(200, json=resp_json)

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    matcher = ValhallaTrackMatcher(
        valhalla_url="http://valhalla.local:8002",
        http_client=client,
        fallback_matcher=_InMemoryHMMFallback(corridor),
    )

    result = asyncio.run(matcher.match_trace(trimmed.trimmed_points))

    assert len(captured_requests) == 1
    req_url, req_body = captured_requests[0]
    assert req_url == "http://valhalla.local:8002/trace_attributes"
    assert req_body["costing"] == "auto"
    assert req_body["costing_options"] == {"auto": {"use_tracks": 1.0, "use_ferry": 0.5}}
    assert req_body["shape_match"] == "map_snap"
    assert req_body["trace_options"] == {"search_radius": 45.0, "gps_accuracy": 12.0}
    assert req_body["filters"]["action"] == "include"
    assert "shape" in req_body["filters"]["attributes"]
    assert "matched.point" in req_body["filters"]["attributes"]
    assert len(req_body["shape"]) == len(trimmed.trimmed_points)

    assert result.matcher_name == "valhalla"
    assert matcher.last_matcher_backend == "valhalla"
    assert result.overall_confidence > 0.75
    assert result.matched_fraction > 0.85
    assert len(result.matched_segments) >= 95
    assert result.unmatched_subtraces == []
    matched_tracks = {seg_to_track_name[s.segment_id] for s in result.matched_segments}
    assert matched_tracks == {"Davies Plain Track Lower", "Davies Plain Ridge"}


def test_valhalla_offline_splits_poplars_and_unmapped_spur(fixtures_dir: Path) -> None:
    corridor, seg_to_track_name = _build_offline_corridor_segments(fixtures_dir)
    parsed = parse_gpx((fixtures_dir / "sample_trace_unmapped_spur.gpx").read_bytes())
    trimmed = trim_privacy_buffer(parsed.points, trim_m=250.0)
    assert not trimmed.too_short

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        resp_json = _build_valhalla_mock_response_from_corridor(body["shape"], corridor)
        return httpx.Response(200, json=resp_json)

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    matcher = ValhallaTrackMatcher(
        valhalla_url="http://valhalla.local:8002",
        http_client=client,
        fallback_matcher=_InMemoryHMMFallback(corridor),
    )

    result = asyncio.run(matcher.match_trace(trimmed.trimmed_points))

    assert result.matcher_name == "valhalla"
    assert matcher.last_matcher_backend == "valhalla"
    assert len(result.matched_segments) >= 10
    matched_tracks = {seg_to_track_name[s.segment_id] for s in result.matched_segments}
    assert "Poplars Fire Trail" in matched_tracks
    assert "Davies Plain Track Lower" not in matched_tracks

    assert len(result.unmatched_subtraces) == 1
    spur = result.unmatched_subtraces[0]
    assert spur.length_m >= 500.0
    assert spur.reason == "no_candidates"
    assert spur.ended_at > spur.started_at


def test_valhalla_offline_polyline6_only_response_snaps_to_canonical(fixtures_dir: Path) -> None:
    corridor, seg_to_track_name = _build_offline_corridor_segments(fixtures_dir)
    parsed = parse_gpx((fixtures_dir / "sample_trace_davies_plain.gpx").read_bytes())
    trimmed = trim_privacy_buffer(parsed.points, trim_m=250.0)

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        full_resp = _build_valhalla_mock_response_from_corridor(body["shape"], corridor)
        # Return Polyline6 shape only (no matched_points array)
        return httpx.Response(200, json={"shape": full_resp["shape"], "edges": full_resp["edges"]})

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    matcher = ValhallaTrackMatcher(
        valhalla_url="http://valhalla.local:8002",
        http_client=client,
        fallback_matcher=_InMemoryHMMFallback(corridor),
    )

    result = asyncio.run(matcher.match_trace(trimmed.trimmed_points))
    assert result.matcher_name == "valhalla"
    assert len(result.matched_segments) >= 85
    matched_tracks = {seg_to_track_name[s.segment_id] for s in result.matched_segments}
    assert "Davies Plain Track Lower" in matched_tracks


def test_valhalla_graceful_fallback_on_503_timeout_and_connect_error(
    fixtures_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    corridor, _ = _build_offline_corridor_segments(fixtures_dir)
    parsed = parse_gpx((fixtures_dir / "sample_trace_davies_plain.gpx").read_bytes())
    trimmed = trim_privacy_buffer(parsed.points, trim_m=250.0)

    def _error_503(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "Valhalla tiles loading"})

    def _timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    def _connect_err(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    for label, handler in (("503", _error_503), ("timeout", _timeout), ("connect", _connect_err)):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="trailforge.matching.valhalla"):
            client = httpx.Client(transport=httpx.MockTransport(handler))
            matcher = ValhallaTrackMatcher(
                valhalla_url="http://valhalla.local:8002",
                fallback_to_hmm=True,
                http_client=client,
                fallback_matcher=_InMemoryHMMFallback(corridor),
            )
            res = asyncio.run(matcher.match_trace(trimmed.trimmed_points))
        assert res.matcher_name == "postgis_hmm_fallback", label
        assert matcher.last_matcher_backend == "postgis_hmm_fallback", label
        assert len(res.matched_segments) >= 95, label
        assert res.overall_confidence > 0.7, label
        assert any("falling back to PostGISHMMMatcher" in rec.message for rec in caplog.records)

    # When fallback_to_hmm=False, failure must raise ValhallaMatcherError
    strict_client = httpx.Client(transport=httpx.MockTransport(_error_503))
    strict_matcher = ValhallaTrackMatcher(
        valhalla_url="http://valhalla.local:8002",
        fallback_to_hmm=False,
        http_client=strict_client,
        fallback_matcher=_InMemoryHMMFallback(corridor),
    )
    with pytest.raises(ValhallaMatcherError, match="Valhalla map-matching failed"):
        asyncio.run(strict_matcher.match_trace(trimmed.trimmed_points))


def test_imu_50hz_smooth_vs_corrugated_rock_step_and_clinometer() -> None:
    # 1. 10 seconds at 50 Hz (500 samples) on smooth sealed road
    smooth_samples: list[ImuSample] = []
    for i in range(500):
        t_ms = float(i * 20)
        az = 1.0 + 0.015 * math.sin(2.0 * math.pi * 2.0 * (t_ms / 1000.0))
        smooth_samples.append(
            ImuSample(t_ms=t_ms, ax=0.01, ay=0.01, az=az, pitch_deg=1.8, roll_deg=0.9)
        )
    smooth_summary = process_imu_batch(smooth_samples)
    assert smooth_summary.sample_count == 500
    assert smooth_summary.roughness_rms is not None and smooth_summary.roughness_rms < 0.03
    assert smooth_summary.roughness_p95 is not None and smooth_summary.roughness_p95 < 0.04
    assert smooth_summary.shock_event_count == 0
    assert smooth_summary.max_pitch_deg == pytest.approx(1.8, abs=0.05)
    assert smooth_summary.max_roll_deg == pytest.approx(0.9, abs=0.05)

    # 2. 10 seconds at 50 Hz (500 samples) on heavily corrugated track + 4 discrete rock shocks.
    #    Each rock-step impact lasts 2 consecutive 20 ms samples -> must count as 1 shock each.
    shock_indices = {75, 76, 175, 176, 275, 276, 400, 401}
    rough_samples: list[ImuSample] = []
    for i in range(500):
        t_ms = float(i * 20)
        corr = 0.35 * math.sin(2.0 * math.pi * 6.0 * (t_ms / 1000.0))
        jolt = 1.35 if i in shock_indices else 0.0
        az = 1.0 + corr + jolt
        pitch = 22.4 if i == 275 else 11.5
        roll = -17.6 if i == 400 else 6.2
        rough_samples.append(
            ImuSample(t_ms=t_ms, ax=0.05, ay=-0.04, az=az, pitch_deg=pitch, roll_deg=roll)
        )
    rough_summary = process_imu_batch(rough_samples)
    assert rough_summary.sample_count == 500
    assert rough_summary.roughness_rms is not None
    assert rough_summary.roughness_p95 is not None
    assert rough_summary.roughness_rms > smooth_summary.roughness_rms * 10.0
    assert rough_summary.roughness_p95 > 0.30
    assert rough_summary.shock_event_count == 4
    assert rough_summary.max_pitch_deg == pytest.approx(22.4, abs=0.05)
    assert rough_summary.max_roll_deg == pytest.approx(17.6, abs=0.05)

    # 3. Derived clinometer angles from 3-axis g-vector when explicit pitch/roll are None
    pitch_rad = math.radians(20.0)
    tilted_pitch_samples = [
        ImuSample(
            t_ms=float(i * 20),
            ax=math.sin(pitch_rad),
            ay=0.0,
            az=math.cos(pitch_rad),
        )
        for i in range(50)
    ]
    tilted_summary = process_imu_batch(tilted_pitch_samples)
    assert tilted_summary.max_pitch_deg == pytest.approx(20.0, abs=0.2)
    assert tilted_summary.max_roll_deg == pytest.approx(0.0, abs=0.2)

    p_deg, r_deg = compute_pitch_roll_deg(
        0.0,
        math.sin(math.radians(15.0)),
        math.cos(math.radians(15.0)),
    )
    assert p_deg == pytest.approx(0.0, abs=0.1)
    assert r_deg == pytest.approx(15.0, abs=0.1)

    with pytest.raises(ValueError, match="finite"):
        ImuSample(t_ms=0.0, ax=float("nan"), ay=0.0, az=1.0)


def test_imu_sparse_low_rate_samples_do_not_self_cancel() -> None:
    # Regression test for sparse/browser-throttled IMU batches (>500 ms spacing):
    # rolling window counts < 5 must fall back to batch gravity mean rather than
    # subtracting each sample from itself (which zeroed roughness_rms & roughness_p95).
    sparse_1hz: list[ImuSample] = []
    for i in range(80):
        t_ms = float(i * 1000)  # 1 Hz (spacing > 500 ms half-window)
        corrugation = 0.12 * math.sin(i * 0.9)
        az = 2.35 if i == 40 else (1.0 + corrugation)
        sparse_1hz.append(
            ImuSample(t_ms=t_ms, ax=0.02, ay=-0.01, az=az, pitch_deg=9.0, roll_deg=5.0)
        )

    summary_1hz = process_imu_batch(sparse_1hz)
    assert summary_1hz.sample_count == 80
    assert summary_1hz.roughness_rms is not None and summary_1hz.roughness_rms > 0.10
    assert summary_1hz.roughness_p95 is not None and summary_1hz.roughness_p95 > 0.10
    assert summary_1hz.shock_event_count == 1
    assert summary_1hz.max_pitch_deg == pytest.approx(9.0, abs=0.05)
    assert summary_1hz.max_roll_deg == pytest.approx(5.0, abs=0.05)

    # Also verify 2 Hz (500 ms spacing, local counts == 3 < 5) does not attenuate roughness/shocks
    sparse_2hz = [
        ImuSample(
            t_ms=float(i * 500),
            ax=0.0,
            ay=0.0,
            az=2.25 if i == 30 else (1.0 + 0.15 * math.sin(i * 1.1)),
        )
        for i in range(60)
    ]
    summary_2hz = process_imu_batch(sparse_2hz)
    assert summary_2hz.roughness_rms is not None and summary_2hz.roughness_rms > 0.12
    assert summary_2hz.roughness_p95 is not None and summary_2hz.roughness_p95 > 0.12
    assert summary_2hz.shock_event_count == 1


def test_imu_privacy_trim_strictly_discards_start_and_end_250m_buffer(
    fixtures_dir: Path,
) -> None:
    parsed = parse_gpx((fixtures_dir / "sample_trace_davies_plain.gpx").read_bytes())
    trimmed = trim_privacy_buffer(parsed.points, trim_m=250.0)
    assert not trimmed.too_short

    # Inject violent 4.2g shocks + 38.5 deg pitch strictly INSIDE the 250 m start/end buffers
    # and zero shocks inside the trimmed interior!
    imu_samples = _generate_imu_samples_for_trace(
        parsed.points,
        trimmed_start=trimmed.trimmed_points[0].timestamp,
        trimmed_end=trimmed.trimmed_points[-1].timestamp,
        hz=5,
        inject_buffer_shocks=True,
        interior_shock_offsets_s=(),
    )

    aligned = align_imu_to_trimmed_points(
        trimmed.trimmed_points,
        imu_samples,
        raw_points=parsed.points,
    )
    assert aligned.discarded_sample_count > 100
    assert aligned.summary.sample_count > 0
    assert aligned.summary.sample_count + aligned.discarded_sample_count == len(imu_samples)
    assert aligned.summary.shock_event_count == 0
    assert aligned.summary.max_pitch_deg is not None and aligned.summary.max_pitch_deg < 12.0
    assert aligned.summary.max_roll_deg is not None and aligned.summary.max_roll_deg < 8.0

    trim_start_ms = trimmed.trimmed_points[0].timestamp.timestamp() * 1000.0
    trim_end_ms = trimmed.trimmed_points[-1].timestamp.timestamp() * 1000.0
    assert all(trim_start_ms <= s.t_ms <= trim_end_ms for s in aligned.trimmed_samples)

    # Every trimmed GPS point received aligned roughness_rms and roughness_p95
    assert all(pt.roughness_rms is not None and pt.roughness_rms > 0.0 for pt in aligned.points)
    assert all(p95 is not None and p95 > 0.0 for p95 in aligned.point_roughness_p95)

    # Relative t_ms (offset from trip start = 0 ms) trims identically
    t0_ms = parsed.points[0].timestamp.timestamp() * 1000.0
    relative_samples = [s.model_copy(update={"t_ms": s.t_ms - t0_ms}) for s in imu_samples]
    aligned_rel = align_imu_to_trimmed_points(
        trimmed.trimmed_points,
        relative_samples,
        raw_points=parsed.points,
    )
    assert aligned_rel.summary.sample_count == aligned.summary.sample_count
    assert aligned_rel.summary.shock_event_count == 0
    assert aligned_rel.discarded_sample_count == aligned.discarded_sample_count

    # Short trace (<= 500 m) discards 100% of IMU samples
    short_aligned = align_imu_to_trimmed_points([], imu_samples)
    assert short_aligned.summary.sample_count == 0
    assert short_aligned.discarded_sample_count == len(imu_samples)


@requires_repo_checkout
def test_valhalla_compose_override_file_and_docker_config(monkeypatch: pytest.MonkeyPatch) -> None:
    override_path = REPO_ROOT / "deploy" / "valhalla-compose.override.yml"
    docs_path = REPO_ROOT / "deploy" / "valhalla.md"
    base_compose = REPO_ROOT / "docker-compose.yml"
    ghcr_compose = REPO_ROOT / "docker-compose.ghcr.yml"

    assert override_path.exists()
    assert docs_path.exists()

    override_text = override_path.read_text(encoding="utf-8")
    assert "ghcr.io/gis-ops/docker-valhalla/valhalla:latest" in override_text
    assert "TRAILFORGE_MATCHER_BACKEND" in override_text
    assert "VALHALLA_URL" in override_text
    assert "trailforge_valhalla_tiles" in override_text
    assert "http://127.0.0.1:8002/status" in override_text

    # Core docker-compose.yml must remain lean (zero valhalla service by default)
    assert "valhalla:" not in base_compose.read_text(encoding="utf-8")

    docker_bin = shutil.which("docker")
    if docker_bin is not None:
        monkeypatch.setenv("AUTH_SECRET_KEY", "synthetic-valhalla-compose-fixture")
        monkeypatch.setenv("TRAILFORGE_PRIVACY_SECRET", "synthetic-valhalla-privacy-fixture")
        for base_file in (base_compose, ghcr_compose):
            proc = subprocess.run(
                [
                    docker_bin,
                    "compose",
                    "-f",
                    str(base_file),
                    "-f",
                    str(override_path),
                    "config",
                    "-q",
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            assert proc.returncode == 0, proc.stderr


# -----------------------------------------------------------------------------
# PostGIS Integration Tests (100% Offline against isolated trailforge_test DB)
# -----------------------------------------------------------------------------


def _truncate_all(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                TRUNCATE TABLE
                    condition_report_votes, condition_reports,
                    telemetry_observations, unmapped_track_candidates, trips,
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


def _register_user(client: TestClient, email: str, name: str) -> dict[str, str]:
    r = client.post(
        "/api/auth/register",
        json={"email": email, "display_name": name, "password": "SuperSecure4WDPassword!"},
    )
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.integration
def test_valhalla_postgis_matching_spur_and_fallback(
    postgis_engine: Engine,
    fixtures_dir: Path,
) -> None:
    _truncate_all(postgis_engine)
    try:
        corridor, _ = _build_offline_corridor_segments(fixtures_dir)
        with Session(postgis_engine) as session:
            ds = load_corridor_fixtures(fixtures_dir, osm_filename="osm_tracks.geojson")
            ingest_corridor_dataset(session, ds, commit=True)

            def _mock_ok(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8"))
                return httpx.Response(
                    200,
                    json=_build_valhalla_mock_response_from_corridor(body["shape"], corridor),
                )

            def _mock_503(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(503, text="Service Unavailable")

            davies = parse_gpx((fixtures_dir / "sample_trace_davies_plain.gpx").read_bytes())
            davies_trimmed = trim_privacy_buffer(davies.points, trim_m=250.0)

            val_matcher = ValhallaTrackMatcher(
                session=session,
                valhalla_url="http://valhalla.local:8002",
                http_client=httpx.Client(transport=httpx.MockTransport(_mock_ok)),
            )
            res = asyncio.run(val_matcher.match_trace(davies_trimmed.trimmed_points))
            assert res.matcher_name == "valhalla"
            assert val_matcher.last_matcher_backend == "valhalla"
            assert len(res.matched_segments) >= 85
            assert res.unmatched_subtraces == []

            # Unmapped spur cleanly separated from Poplars Fire Trail in PostGIS
            spur = parse_gpx((fixtures_dir / "sample_trace_unmapped_spur.gpx").read_bytes())
            spur_trimmed = trim_privacy_buffer(spur.points, trim_m=250.0)
            spur_res = asyncio.run(val_matcher.match_trace(spur_trimmed.trimmed_points))
            assert spur_res.matcher_name == "valhalla"
            assert len(spur_res.matched_segments) >= 10
            assert len(spur_res.unmatched_subtraces) == 1
            assert spur_res.unmatched_subtraces[0].length_m >= 500.0

            # Graceful fallback to PostGISHMMMatcher on HTTP 503
            fb_matcher = ValhallaTrackMatcher(
                session=session,
                valhalla_url="http://valhalla.local:8002",
                fallback_to_hmm=True,
                http_client=httpx.Client(transport=httpx.MockTransport(_mock_503)),
            )
            fb_res = asyncio.run(fb_matcher.match_trace(davies_trimmed.trimmed_points))
            assert fb_res.matcher_name == "postgis_hmm_fallback"
            assert fb_matcher.last_matcher_backend == "postgis_hmm_fallback"
            assert len(fb_res.matched_segments) >= 85
    finally:
        _truncate_all(postgis_engine)


@pytest.mark.integration
def test_mobile_sync_endpoint_imu_privacy_valhalla_and_revocation(
    postgis_engine: Engine,
    fixtures_dir: Path,
) -> None:
    _truncate_all(postgis_engine)
    try:
        corridor, _ = _build_offline_corridor_segments(fixtures_dir)
        with Session(postgis_engine) as session:
            ds = load_corridor_fixtures(fixtures_dir, osm_filename="osm_tracks.geojson")
            ingest_corridor_dataset(session, ds, commit=True)

            # Confirm zero user_id / trip_id columns on public telemetry tables
            insp = inspect(postgis_engine)
            obs_cols = {c["name"] for c in insp.get_columns("telemetry_observations")}
            unm_cols = {c["name"] for c in insp.get_columns("unmapped_track_candidates")}
            assert "user_id" not in obs_cols and "trip_id" not in obs_cols
            assert "user_id" not in unm_cols and "trip_id" not in unm_cols

            davies = parse_gpx((fixtures_dir / "sample_trace_davies_plain.gpx").read_bytes())
            davies_trimmed = trim_privacy_buffer(davies.points, trim_m=250.0)
            imu_samples = _generate_imu_samples_for_trace(
                davies.points,
                trimmed_start=davies_trimmed.trimmed_points[0].timestamp,
                trimmed_end=davies_trimmed.trimmed_points[-1].timestamp,
                hz=5,
                inject_buffer_shocks=True,
                interior_shock_offsets_s=(120.0, 300.0),
            )
            points_payload = [
                {
                    "lon": p.lon,
                    "lat": p.lat,
                    "elev_m": p.elev_m,
                    "timestamp": p.timestamp.isoformat(),
                }
                for p in davies.points
            ]
            imu_payload = [s.model_dump() for s in imu_samples]

            app = create_app()

            def _override_db() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db] = _override_db
            with TestClient(app) as client:
                # 1. Auth required (401)
                assert (
                    client.post(
                        "/api/trips/mobile-sync",
                        json={"name": "Anon", "points": points_payload[:5]},
                    ).status_code
                    == 401
                )

                headers = _register_user(client, "mobile_imu@alps.au", "Mobile IMU Driver")

                # 2. Strict privacy_trim_m >= 250 m enforcement (422)
                assert (
                    client.post(
                        "/api/trips/mobile-sync",
                        headers=headers,
                        json={
                            "name": "Bad Trim",
                            "points": points_payload,
                            "imu_samples": imu_payload[:20],
                            "visibility": "community_anonymous",
                            "confirm_community": True,
                            "privacy_trim_m": 150,
                        },
                    ).status_code
                    == 422
                )

                # 3. Full mobile-sync with high-rate IMU (default PostGIS HMM backend)
                sync_req = {
                    "name": "Davies Plain Capacitor iOS Run",
                    "points": points_payload,
                    "imu_samples": imu_payload,
                    "visibility": "community_anonymous",
                    "confirm_community": True,
                    "privacy_trim_m": 250,
                    "client_session_id": "ios-session-davies-001",
                    "recording_source": "capacitor_ios",
                }
                r_sync = client.post("/api/trips/mobile-sync", headers=headers, json=sync_req)
                assert r_sync.status_code == 201, r_sync.text
                body = r_sync.json()
                assert body["matcher_backend"] == "postgis_hmm"
                assert body["recording_source"] == "capacitor_ios"
                assert body["client_session_id"] == "ios-session-davies-001"
                assert body["observations_published"] >= 85
                assert body["imu_samples_discarded_for_privacy"] > 100

                imu_sum = body["imu_summary"]
                assert imu_sum["sample_count"] > 0
                assert imu_sum["roughness_rms"] > 0.0
                assert imu_sum["roughness_p95"] >= imu_sum["roughness_rms"]
                # Driveway/campsite 4.2g shocks were trimmed; only 2 interior rock shocks remain!
                assert imu_sum["shock_event_count"] == 2
                assert imu_sum["max_pitch_deg"] == pytest.approx(19.0, abs=0.5)
                assert imu_sum["max_roll_deg"] == pytest.approx(14.0, abs=0.5)

                # Verify persisted telemetry_observations carry high-rate roughness_rms & p95
                obs_rows = session.execute(select(TelemetryObservation)).scalars().all()
                assert len(obs_rows) == body["observations_published"]
                assert any(r.roughness_rms is not None and r.roughness_rms > 0 for r in obs_rows)
                assert any(r.roughness_p95 is not None and r.roughness_p95 > 0 for r in obs_rows)

                # Idempotent retry with same client_session_id returns same trip without duplication
                r_retry = client.post("/api/trips/mobile-sync", headers=headers, json=sync_req)
                assert r_retry.status_code == 201
                assert r_retry.json()["trip_id"] == body["trip_id"]
                assert (
                    session.execute(select(func.count(TelemetryObservation.id))).scalar_one()
                    == body["observations_published"]
                )

                # 4. Instant revocation on community_anonymous -> private
                r_priv = client.patch(
                    f"/api/trips/{body['trip_id']}",
                    headers=headers,
                    json={"visibility": "private"},
                )
                assert r_priv.status_code == 200, r_priv.text
                assert r_priv.json()["observations_published"] == 0
                assert (
                    session.execute(select(func.count(TelemetryObservation.id))).scalar_one() == 0
                )

                # 5. JSON /api/trips/upload with optional imu_samples also computes imu_summary
                r_up_imu = client.post(
                    "/api/trips/upload",
                    headers=headers,
                    json={
                        "name": "JSON Upload With IMU",
                        "points": points_payload[:40],
                        "imu_samples": imu_payload[:1000],
                        "visibility": "private",
                        "privacy_trim_m": 250,
                    },
                )
                assert r_up_imu.status_code == 201, r_up_imu.text
                up_body = r_up_imu.json()
                assert up_body["visibility"] == "private"
                assert up_body["observations_published"] == 0
                assert up_body["imu_summary"] is not None
                assert up_body["imu_summary"]["sample_count"] > 0

                # 6. Wire ValhallaTrackMatcher via DI -> verify "valhalla" & "postgis_hmm_fallback"
                def _mock_valhalla_ok(request: httpx.Request) -> httpx.Response:
                    req_json = json.loads(request.content.decode("utf-8"))
                    return httpx.Response(
                        200,
                        json=_build_valhalla_mock_response_from_corridor(
                            req_json["shape"], corridor
                        ),
                    )

                app.dependency_overrides[api_get_track_matcher] = lambda: ValhallaTrackMatcher(
                    session=session,
                    valhalla_url="http://valhalla.local:8002",
                    http_client=httpx.Client(transport=httpx.MockTransport(_mock_valhalla_ok)),
                )
                r_val = client.post(
                    "/api/trips/mobile-sync",
                    headers=headers,
                    json={
                        "name": "Davies Plain Valhalla Sync",
                        "points": points_payload,
                        "imu_samples": imu_payload,
                        "visibility": "community_anonymous",
                        "confirm_community": True,
                        "privacy_trim_m": 250,
                        "recording_source": "capacitor_android",
                    },
                )
                assert r_val.status_code == 201, r_val.text
                assert r_val.json()["matcher_backend"] == "valhalla"
                assert r_val.json()["matcher_name"] == "valhalla"
                assert r_val.json()["recording_source"] == "capacitor_android"
                assert r_val.json()["observations_published"] >= 85

                def _mock_valhalla_down(_request: httpx.Request) -> httpx.Response:
                    return httpx.Response(503, text="Down")

                app.dependency_overrides[api_get_track_matcher] = lambda: ValhallaTrackMatcher(
                    session=session,
                    valhalla_url="http://valhalla.local:8002",
                    fallback_to_hmm=True,
                    http_client=httpx.Client(transport=httpx.MockTransport(_mock_valhalla_down)),
                )
                r_fb = client.post(
                    "/api/trips/mobile-sync",
                    headers=headers,
                    json={
                        "name": "Davies Plain Fallback Sync",
                        "points": points_payload,
                        "visibility": "private",
                        "privacy_trim_m": 250,
                        "recording_source": "pwa_browser",
                    },
                )
                assert r_fb.status_code == 201, r_fb.text
                assert r_fb.json()["matcher_backend"] == "postgis_hmm_fallback"
                assert r_fb.json()["matcher_name"] == "postgis_hmm_fallback"
                assert r_fb.json()["recording_source"] == "pwa"

            app.dependency_overrides.clear()
    finally:
        _truncate_all(postgis_engine)
