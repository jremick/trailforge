"""Offline unit & PostGIS integration tests for Slice 06 telemetry parsing, privacy & HMM."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pyproj import Transformer
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from trailforge.ingestion.conflate import conflate_tracks
from trailforge.ingestion.connectors import load_corridor_fixtures
from trailforge.ingestion.pipeline import ingest_corridor_dataset
from trailforge.ingestion.segmenter import segment_conflated_ways
from trailforge.matching import (
    MIN_PRIVACY_TRIM_M,
    GpsPoint,
    MatchedSegmentTraversal,
    MatchResult,
    PostGISHMMMatcher,
    PostGISHMMTrackMatcher,
    TrackMatcher,
    compute_contribution_hash,
    measure_trace_length_m,
    parse_gpx,
    parse_telemetry_json,
    points_to_linestring,
    trim_privacy_buffer,
)
from trailforge.matching.hmm import _CorridorSegment

# -----------------------------------------------------------------------------
# Pure offline unit tests (zero database required)
# -----------------------------------------------------------------------------


def test_parse_gpx_fixtures_and_extensions(fixtures_dir: Path) -> None:
    davies_raw = (fixtures_dir / "sample_trace_davies_plain.gpx").read_bytes()
    parsed = parse_gpx(davies_raw)
    assert parsed.name == "Davies Plain Traverse (Tom Groggin to Davies Plain Hut)"
    assert len(parsed.points) == 120
    assert parsed.points[0].timestamp.tzinfo is not None
    assert parsed.points[0].elev_m is not None and parsed.points[0].elev_m >= 600.0
    assert parsed.points[-1].timestamp > parsed.points[0].timestamp

    # Test GPX with custom roughness/accuracy/speed extensions and no XML namespace
    ext_gpx = """<?xml version="1.0" encoding="UTF-8"?>
    <gpx version="1.1">
      <trk><name>Rough Climb</name><trkseg>
        <trkpt lat="-36.5800" lon="148.1200">
          <ele>720.5</ele><time>2026-11-22T01:00:00Z</time>
          <extensions><roughness_rms>2.4</roughness_rms><accuracy_m>6.5</accuracy_m></extensions>
        </trkpt>
        <trkpt lat="-36.5820" lon="148.1205">
          <ele>780.0</ele><time>2026-11-22T01:00:30Z</time>
          <extensions><roughness_rms>3.8</roughness_rms><speed_mps>4.2</speed_mps></extensions>
        </trkpt>
      </trkseg></trk>
    </gpx>"""
    ext_parsed = parse_gpx(ext_gpx)
    assert ext_parsed.name == "Rough Climb"
    assert len(ext_parsed.points) == 2
    assert ext_parsed.points[0].roughness_rms == pytest.approx(2.4)
    assert ext_parsed.points[0].accuracy_m == pytest.approx(6.5)
    assert ext_parsed.points[1].roughness_rms == pytest.approx(3.8)
    assert ext_parsed.points[1].speed_mps == pytest.approx(4.2)


def test_parse_gpx_rejects_malformed_unsafe_and_short_inputs() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_gpx(b"   ")

    with pytest.raises(ValueError, match="DOCTYPE|ENTITY"):
        parse_gpx('<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe "bad">]><gpx/>')

    with pytest.raises(ValueError, match="Malformed"):
        parse_gpx("<gpx><trk><trkseg>")

    with pytest.raises(ValueError, match="at least 2"):
        parse_gpx(
            '<gpx version="1.1"><trk><trkseg>'
            '<trkpt lat="-36.60" lon="148.12"><time>2026-11-20T00:00:00Z</time></trkpt>'
            "</trkseg></trk></gpx>"
        )


def test_parse_telemetry_json_variants_and_validation() -> None:
    payload = {
        "name": "PWA Offline Recording",
        "points": [
            {
                "lon": 148.120,
                "lat": -36.560,
                "elev_m": 612.0,
                "timestamp": "2026-11-22T02:00:00Z",
                "accuracy_m": 8.0,
                "roughness_rms": 1.5,
            },
            {
                "lon": 148.120,
                "lat": -36.565,
                "elev_m": 625.0,
                "timestamp": "2026-11-22T02:01:00Z",
                "speed_mps": 6.5,
                "roughness_rms": 2.9,
            },
        ],
    }
    parsed = parse_telemetry_json(json.dumps(payload))
    assert parsed.name == "PWA Offline Recording"
    assert len(parsed.points) == 2
    assert parsed.points[0].roughness_rms == pytest.approx(1.5)
    assert parsed.points[1].speed_mps == pytest.approx(6.5)

    # GeoJSON Feature LineString also supported cleanly
    gj = {
        "type": "Feature",
        "properties": {"title": "GeoJSON Track"},
        "geometry": {
            "type": "LineString",
            "coordinates": [[148.120, -36.560, 610.0], [148.121, -36.565, 640.0]],
        },
    }
    parsed_gj = parse_telemetry_json(gj)
    assert parsed_gj.name == "GeoJSON Track"
    assert len(parsed_gj.points) == 2
    assert parsed_gj.points[0].elev_m == pytest.approx(610.0)

    with pytest.raises(ValueError, match="at least 2"):
        parse_telemetry_json({"points": [{"lon": 148.12, "lat": -36.56}]})

    with pytest.raises(ValueError, match="out of range"):
        parse_telemetry_json(
            [
                {"lon": 148.12, "lat": -95.0},
                {"lon": 148.12, "lat": -36.56},
            ]
        )


def test_privacy_buffer_rejects_under_250m_and_handles_too_short_traces() -> None:
    t0 = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
    short_pts = [
        GpsPoint(lon=148.1200, lat=-36.5600, timestamp=t0),
        GpsPoint(lon=148.1200, lat=-36.5635, timestamp=t0 + timedelta(seconds=60)),  # ~390 m
    ]
    assert measure_trace_length_m(short_pts) < 500.0

    for invalid_trim in (0.0, 100.0, 249.9, -10.0, 5001.0, float("nan")):
        with pytest.raises(ValueError, match="privacy_trim_m"):
            trim_privacy_buffer(short_pts, trim_m=invalid_trim)

    res = trim_privacy_buffer(short_pts, trim_m=MIN_PRIVACY_TRIM_M)
    assert res.too_short is True
    assert res.trimmed_points == []
    assert res.trimmed_length_m == 0.0
    assert res.raw_length_m > 300.0


def test_privacy_buffer_strictly_strips_start_and_end_of_davies_plain(
    fixtures_dir: Path,
) -> None:
    parsed = parse_gpx((fixtures_dir / "sample_trace_davies_plain.gpx").read_bytes())
    raw_start = parsed.points[0]
    raw_end = parsed.points[-1]

    trimmed = trim_privacy_buffer(parsed.points, trim_m=250.0)
    assert trimmed.too_short is False
    assert len(trimmed.trimmed_points) >= 2
    assert trimmed.trimmed_length_m == pytest.approx(trimmed.raw_length_m - 500.0, abs=1.5)

    # Verify original start and end coordinates are strictly >= 230 m away from trimmed geometry
    to_albers = Transformer.from_crs("EPSG:4326", "EPSG:3577", always_xy=True)
    sx, sy = to_albers.transform(raw_start.lon, raw_start.lat)
    ex, ey = to_albers.transform(raw_end.lon, raw_end.lat)

    t_start = trimmed.trimmed_points[0]
    t_end = trimmed.trimmed_points[-1]
    tsx, tsy = to_albers.transform(t_start.lon, t_start.lat)
    tex, tey = to_albers.transform(t_end.lon, t_end.lat)

    assert math.hypot(tsx - sx, tsy - sy) >= 230.0
    assert math.hypot(tex - ex, tey - ey) >= 230.0
    assert (raw_start.lon, raw_start.lat) not in [(p.lon, p.lat) for p in trimmed.trimmed_points]
    assert (raw_end.lon, raw_end.lat) not in [(p.lon, p.lat) for p in trimmed.trimmed_points]

    # Trimmed geometry builds a valid LineString
    line = points_to_linestring(trimmed.trimmed_points)
    assert line.is_valid and not line.is_empty


def test_compute_contribution_hash_is_deterministic_and_rotatable() -> None:
    trip_id = uuid4()
    nonce_a = uuid4()
    nonce_b = uuid4()

    h1 = compute_contribution_hash("super-secret-key", trip_id, nonce_a)
    h2 = compute_contribution_hash("super-secret-key", str(trip_id), str(nonce_a))
    h_rotated = compute_contribution_hash("super-secret-key", trip_id, nonce_b)
    h_other_secret = compute_contribution_hash("different-secret", trip_id, nonce_a)

    assert len(h1) == 64
    assert h1 == h2
    assert h1 != h_rotated
    assert h1 != h_other_secret

    with pytest.raises(ValueError, match="non-empty"):
        compute_contribution_hash("", trip_id, nonce_a)


def test_pluggable_custom_matcher_protocol_conformance() -> None:
    class StubValhallaMatcher:
        name = "valhalla_stub_v0"

        async def match_trace(
            self,
            points: list[GpsPoint],
            *,
            search_radius_m: float = 35.0,
            min_confidence: float = 0.55,
        ) -> MatchResult:
            return MatchResult(
                matcher_name=self.name,
                matched_segments=[
                    MatchedSegmentTraversal(
                        segment_id="tfseg_deadbeef01234567",
                        track_id="tftrk_0123456789ab",
                        direction="forward",
                        entry_at=points[0].timestamp,
                        exit_at=points[-1].timestamp,
                        duration_s=20.0,
                        avg_speed_kmh=14.0,
                        match_confidence=0.95,
                        matched_point_count=len(points),
                    )
                ],
                unmatched_subtraces=[],
                overall_confidence=0.95,
                matched_fraction=1.0,
            )

    stub = StubValhallaMatcher()
    assert isinstance(stub, TrackMatcher)
    assert PostGISHMMTrackMatcher is PostGISHMMMatcher


# -----------------------------------------------------------------------------
# Pure-offline HMM algorithm verification against real corridor segments (no DB)
# -----------------------------------------------------------------------------


class _InMemoryCorridorHMMMatcher(PostGISHMMMatcher):
    """Test harness feeding real segmented corridor geometries directly into HMM DP."""

    def __init__(self, corridor_segments: list[_CorridorSegment]) -> None:
        dummy_session: Any = None
        super().__init__(dummy_session)
        self._corridor_segments = corridor_segments

    def _fetch_corridor_segments(
        self,
        points: Sequence[GpsPoint],
        search_radius_m: float,
    ) -> list[_CorridorSegment]:
        return list(self._corridor_segments)


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


def test_hmm_offline_davies_plain_reverse_and_slow_hotspot(fixtures_dir: Path) -> None:
    corridor, seg_to_track_name = _build_offline_corridor_segments(fixtures_dir)
    matcher = _InMemoryCorridorHMMMatcher(corridor)
    assert isinstance(matcher, TrackMatcher)

    parsed = parse_gpx((fixtures_dir / "sample_trace_davies_plain.gpx").read_bytes())
    enriched_pts = [
        pt.model_copy(update={"roughness_rms": round(1.4 + (i % 5) * 0.4, 2)})
        for i, pt in enumerate(parsed.points)
    ]
    trimmed = trim_privacy_buffer(enriched_pts, trim_m=250.0)
    assert not trimmed.too_short

    result = asyncio.run(matcher.match_trace(trimmed.trimmed_points))
    assert result.matcher_name == "postgis_hmm_v1"
    assert result.overall_confidence > 0.7
    assert result.matched_fraction > 0.85
    assert len(result.matched_segments) >= 100
    assert result.unmatched_subtraces == []

    matched_track_names = {seg_to_track_name[s.segment_id] for s in result.matched_segments}
    assert matched_track_names == {"Davies Plain Track Lower", "Davies Plain Ridge"}

    # Verify direction flips cleanly when traversing the exact same track in reverse
    rev_start = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
    reversed_pts = [
        pt.model_copy(update={"timestamp": rev_start + timedelta(seconds=i * 25)})
        for i, pt in enumerate(reversed(trimmed.trimmed_points))
    ]
    rev_result = asyncio.run(matcher.match_trace(reversed_pts))
    fwd_dirs = {s.segment_id: s.direction for s in result.matched_segments}
    rev_dirs = {s.segment_id: s.direction for s in rev_result.matched_segments}
    shared_ids = set(fwd_dirs) & set(rev_dirs)
    assert len(shared_ids) >= 80
    flipped_ratio = sum(1 for sid in shared_ids if fwd_dirs[sid] != rev_dirs[sid]) / len(shared_ids)
    assert flipped_ratio > 0.95

    # Verify slow-crawl (<5 km/h) emits snapped slow_hotspot_lonlat on canonical geometry
    slow_start = datetime(2026, 11, 22, 8, 0, tzinfo=UTC)
    slow_pts = [
        pt.model_copy(update={"timestamp": slow_start + timedelta(seconds=i * 150)})  # ~2.3 km/h
        for i, pt in enumerate(trimmed.trimmed_points[:12])
    ]
    slow_result = asyncio.run(matcher.match_trace(slow_pts))
    assert any(seg.slow_hotspot_lonlat is not None for seg in slow_result.matched_segments)


def test_hmm_offline_splits_poplars_and_unmapped_spur(fixtures_dir: Path) -> None:
    corridor, seg_to_track_name = _build_offline_corridor_segments(fixtures_dir)
    matcher = _InMemoryCorridorHMMMatcher(corridor)

    parsed = parse_gpx((fixtures_dir / "sample_trace_unmapped_spur.gpx").read_bytes())
    trimmed = trim_privacy_buffer(parsed.points, trim_m=250.0)
    assert not trimmed.too_short

    result = asyncio.run(matcher.match_trace(trimmed.trimmed_points))
    assert len(result.matched_segments) >= 10
    assert result.overall_confidence > 0.7
    assert 0.35 <= result.matched_fraction <= 0.85

    matched_track_names = {seg_to_track_name[s.segment_id] for s in result.matched_segments}
    assert "Poplars Fire Trail" in matched_track_names
    assert "Davies Plain Track Lower" not in matched_track_names
    assert "Davies Plain Ridge" not in matched_track_names

    assert len(result.unmatched_subtraces) == 1
    spur = result.unmatched_subtraces[0]
    assert spur.length_m >= 100.0
    assert len(spur.coordinates) >= 3
    assert spur.ended_at > spur.started_at
    assert spur.reason in ("no_candidates", "low_confidence")


# -----------------------------------------------------------------------------
# PostGIS HMM Map-Matching Integration Tests (100% offline against seeded DB)
# -----------------------------------------------------------------------------


_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="module")
def seeded_corridor_engine(postgis_engine: Engine) -> Engine:
    """Ensure the synthetic Australian Alps corridor is seeded in PostGIS."""
    with Session(postgis_engine) as session:
        seg_count = session.execute(
            text("SELECT COUNT(*) FROM track_segments WHERE is_active = TRUE")
        ).scalar_one()
        if seg_count == 0:
            ingest_corridor_dataset(session, load_corridor_fixtures(_FIXTURES_DIR))
            session.commit()
    return postgis_engine


@pytest.mark.integration
def test_hmm_postgis_matches_davies_plain_traverse_end_to_end(
    seeded_corridor_engine: Engine,
    fixtures_dir: Path,
) -> None:
    parsed = parse_gpx((fixtures_dir / "sample_trace_davies_plain.gpx").read_bytes())
    enriched_pts = [
        pt.model_copy(update={"roughness_rms": round(1.5 + (i % 5) * 0.4, 2)})
        for i, pt in enumerate(parsed.points)
    ]
    trimmed = trim_privacy_buffer(enriched_pts, trim_m=250.0)
    assert not trimmed.too_short

    with Session(seeded_corridor_engine) as session:
        matcher = PostGISHMMMatcher(session)
        assert isinstance(matcher, TrackMatcher)

        result = asyncio.run(matcher.match_trace(trimmed.trimmed_points))

        assert result.matcher_name == "postgis_hmm_v1"
        assert result.overall_confidence > 0.7
        assert result.matched_fraction > 0.85
        assert len(result.matched_segments) >= 80
        assert result.unmatched_subtraces == []

        seg_ids = list({seg.segment_id for seg in result.matched_segments})
        rows = session.execute(
            text(
                """
                SELECT ts.segment_id, ts.track_id, ts.is_vehicular, t.name
                FROM track_segments ts
                JOIN tracks t ON t.track_id = ts.track_id
                WHERE ts.segment_id = ANY(:seg_ids)
                """
            ),
            {"seg_ids": seg_ids},
        ).fetchall()
        matched_names = {row.name for row in rows}
        assert "Davies Plain Track Lower" in matched_names
        assert "Davies Plain Ridge" in matched_names
        assert all(row.is_vehicular for row in rows)

        for trav in result.matched_segments:
            assert trav.track_id is not None
            assert trav.direction in ("forward", "reverse")
            assert trav.duration_s > 0.0
            assert 0.0 <= trav.avg_speed_kmh <= 160.0
            assert trav.min_speed_kmh is not None and 0.0 <= trav.min_speed_kmh <= 160.0
            assert 0.0 <= trav.match_confidence <= 1.0
            assert trav.matched_point_count >= 1
            assert trav.exit_at >= trav.entry_at
            assert trav.roughness_rms is not None and trav.roughness_rms > 0.0
            assert trav.roughness_p95 is not None and trav.roughness_p95 >= trav.roughness_rms


@pytest.mark.integration
def test_hmm_postgis_splits_poplars_and_unmapped_logging_spur(
    seeded_corridor_engine: Engine,
    fixtures_dir: Path,
) -> None:
    parsed = parse_gpx((fixtures_dir / "sample_trace_unmapped_spur.gpx").read_bytes())
    trimmed = trim_privacy_buffer(parsed.points, trim_m=250.0)
    assert not trimmed.too_short

    with Session(seeded_corridor_engine) as session:
        matcher = PostGISHMMMatcher(session)
        result = asyncio.run(matcher.match_trace(trimmed.trimmed_points))

        assert len(result.matched_segments) >= 10
        assert result.overall_confidence > 0.7
        assert 0.35 <= result.matched_fraction <= 0.85

        seg_ids = list({seg.segment_id for seg in result.matched_segments})
        rows = session.execute(
            text(
                """
                SELECT DISTINCT t.name
                FROM track_segments ts
                JOIN tracks t ON t.track_id = ts.track_id
                WHERE ts.segment_id = ANY(:seg_ids)
                """
            ),
            {"seg_ids": seg_ids},
        ).fetchall()
        matched_names = {row.name for row in rows}
        assert "Poplars Fire Trail" in matched_names
        assert "Davies Plain Track Lower" not in matched_names
        assert "Davies Plain Ridge" not in matched_names

        assert len(result.unmatched_subtraces) == 1
        spur = result.unmatched_subtraces[0]
        assert spur.length_m >= 100.0
        assert len(spur.coordinates) >= 3
        assert spur.ended_at > spur.started_at
        assert spur.reason in ("no_candidates", "low_confidence")
