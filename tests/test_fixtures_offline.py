"""Offline unit tests verifying synthetic VIC/NSW fixtures, TrackMatcher protocol, & no PyQGIS."""

from __future__ import annotations

import hashlib
import json
import math
import struct
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import shape

from tests.fixtures.generate_fixtures import alps_corridor_elev_m
from trailforge.matching import (
    GpsPoint,
    MatchedSegmentTraversal,
    MatchResult,
    TrackMatcher,
    UnmatchedSubtrace,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_fixture_manifest_checksums_and_size_budget(fixtures_dir: Path) -> None:
    manifest_path = fixtures_dir / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["bbox"] == [148.05, -36.68, 148.20, -36.55]
    assert manifest["crs"]["storage"] == "EPSG:4326"
    assert manifest["crs"]["metric"] == "EPSG:3577"
    assert manifest["crs"]["utm"] == "EPSG:32755"

    total_bytes = 0
    for filename, meta in manifest["files"].items():
        fpath = fixtures_dir / filename
        assert fpath.exists(), f"Missing fixture file: {filename}"
        raw = fpath.read_bytes()
        assert len(raw) == meta["size_bytes"]
        assert hashlib.sha256(raw).hexdigest() == meta["sha256"]
        total_bytes += len(raw)

    # Committed fixture data budget strictly < 300 KB
    assert total_bytes < 300 * 1024, f"Fixture bundle too large: {total_bytes} bytes"


def test_geojson_fixtures_geometry_validity_and_corridor_bounds(fixtures_dir: Path) -> None:
    min_lon, min_lat, max_lon, max_lat = 148.05, -36.68, 148.20, -36.55
    expected_counts = {
        "osm_tracks.geojson": 7,
        "osm_tracks_v2_jittered.geojson": 8,  # way/1004 split into 2 ways
        "vic_tracks.geojson": 3,
        "nsw_tracks.geojson": 2,
        "waterways.geojson": 3,
        "capad_parks.geojson": 2,
        "pois.geojson": 6,
    }

    for fname, expected_n in expected_counts.items():
        fc = json.loads((fixtures_dir / fname).read_text(encoding="utf-8"))
        assert fc["type"] == "FeatureCollection"
        assert len(fc["features"]) == expected_n, f"{fname} count mismatch"
        for feat in fc["features"]:
            geom = shape(feat["geometry"])
            assert geom.is_valid, f"Invalid geometry in {fname}"
            assert not geom.is_empty
            bx0, by0, bx1, by1 = geom.bounds
            assert bx0 >= min_lon - 1e-6 and bx1 <= max_lon + 1e-6, f"{fname} lon out of bounds"
            assert by0 >= min_lat - 1e-6 and by1 <= max_lat + 1e-6, f"{fname} lat out of bounds"


def test_osm_tracks_and_v2_reimport_invariance_properties(fixtures_dir: Path) -> None:
    osm = json.loads((fixtures_dir / "osm_tracks.geojson").read_text(encoding="utf-8"))
    by_id = {f["properties"]["osm_way_id"]: f for f in osm["features"]}
    assert set(by_id.keys()) == {1001, 1002, 1003, 1004, 1005, 1006, 1007}
    assert by_id[1002]["properties"]["ford"] == "yes"
    assert by_id[1002]["properties"]["4wd_only"] == "yes"
    assert by_id[1005]["properties"]["access"] == "private"
    assert by_id[1005]["properties"]["has_gate_barrier"] is True
    assert by_id[1006]["properties"]["bridge"] == "yes"
    assert by_id[1007]["properties"]["motor_vehicle"] == "no"

    osm_v2 = json.loads(
        (fixtures_dir / "osm_tracks_v2_jittered.geojson").read_text(encoding="utf-8")
    )
    v2_by_id = {f["properties"]["osm_way_id"]: f for f in osm_v2["features"]}
    # way/1001 unchanged geometry, tag edited
    assert v2_by_id[1001]["geometry"] == by_id[1001]["geometry"]
    assert v2_by_id[1001]["properties"]["surface"] != by_id[1001]["properties"]["surface"]
    # way/1002 reversed + jittered (~3-6 m)
    c1 = by_id[1002]["geometry"]["coordinates"]
    c2 = v2_by_id[1002]["geometry"]["coordinates"]
    assert len(c1) == len(c2)
    assert c2[0][1] < c2[-1][1]  # reversed south->north vs original north->south
    # way/1004 split at degree-2 midpoint
    assert 100401 in v2_by_id and 100402 in v2_by_id
    assert (
        v2_by_id[100401]["geometry"]["coordinates"][-1]
        == v2_by_id[100402]["geometry"]["coordinates"][0]
    )


def test_dem_geotiff_header_and_elevation_slope_properties(fixtures_dir: Path) -> None:
    dem_path = fixtures_dir / "dem_alps_corridor.tif"
    raw = dem_path.read_bytes()
    # Little-endian classic TIFF magic
    assert raw[:4] == b"II*\x00"
    ifd_offset = struct.unpack_from("<I", raw, 4)[0]
    num_entries = struct.unpack_from("<H", raw, ifd_offset)[0]
    tags: dict[int, tuple[int, int, int]] = {}
    for i in range(num_entries):
        tag, dtype, count, val = struct.unpack_from("<HHII", raw, ifd_offset + 2 + i * 12)
        tags[tag] = (dtype, count, val)

    assert tags[256][2] == 300  # ImageWidth
    assert tags[257][2] == 260  # ImageLength
    assert (tags[258][2] & 0xFFFF) == 32  # BitsPerSample Float32
    assert (tags[259][2] & 0xFFFF) == 8  # DEFLATE compression
    assert (tags[339][2] & 0xFFFF) == 3  # IEEE float
    assert 33550 in tags and 33922 in tags and 34735 in tags and 42113 in tags

    # Verify analytic DEM range (snow-line >1,000 m, alpine ridge >1,200 m) and steep climb >18 deg
    valley_elev = alps_corridor_elev_m(148.120, -36.565)
    ridge_elev = alps_corridor_elev_m(148.126, -36.662)
    assert 600.0 <= valley_elev <= 700.0
    assert 1350.0 <= ridge_elev <= 1650.0

    # Measure escarpment slope along Davies Plain Lower (way/1002) using EPSG:3577 meters
    to_albers = Transformer.from_crs("EPSG:4326", "EPSG:3577", always_xy=True)
    x0, y0 = to_albers.transform(148.121, -36.594)
    x1, y1 = to_albers.transform(148.121, -36.600)
    dist_m = math.hypot(x1 - x0, y1 - y0)
    dz = alps_corridor_elev_m(148.121, -36.600) - alps_corridor_elev_m(148.121, -36.594)
    slope_deg = math.degrees(math.atan2(abs(dz), dist_m))
    assert slope_deg > 18.0, f"Expected steep escarpment >18 deg, got {slope_deg:.2f} deg"


def test_gpx_fixtures_parse_and_meet_privacy_trim_lengths(fixtures_dir: Path) -> None:
    ns = {"gpx": "http://www.topografix.com/GPX/1/1"}
    to_albers = Transformer.from_crs("EPSG:4326", "EPSG:3577", always_xy=True)

    def _parse_length_and_count(path: Path) -> tuple[int, float]:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
        pts = root.findall(".//gpx:trkpt", ns)
        coords = []
        for pt in pts:
            lat = float(pt.attrib["lat"])
            lon = float(pt.attrib["lon"])
            ele_el = pt.find("gpx:ele", ns)
            time_el = pt.find("gpx:time", ns)
            assert ele_el is not None and float(ele_el.text or "0") >= 600.0
            assert time_el is not None and time_el.text
            coords.append(to_albers.transform(lon, lat))
        total_m = sum(
            math.hypot(coords[i][0] - coords[i - 1][0], coords[i][1] - coords[i - 1][1])
            for i in range(1, len(coords))
        )
        return len(pts), total_m

    davies_count, davies_len_m = _parse_length_and_count(
        fixtures_dir / "sample_trace_davies_plain.gpx"
    )
    assert davies_count == 120
    assert davies_len_m > 3500.0  # comfortably exceeds 2 * 250 m privacy trim

    spur_count, spur_len_m = _parse_length_and_count(
        fixtures_dir / "sample_trace_unmapped_spur.gpx"
    )
    assert spur_count == 45
    assert spur_len_m > 1000.0


def test_closures_sample_payload(fixtures_dir: Path) -> None:
    payload = json.loads((fixtures_dir / "closures_sample.json").read_text(encoding="utf-8"))
    feeds = payload["feeds"]
    assert set(feeds.keys()) == {"vic_mapshare_seasonal", "nsw_npws", "vic_emergency"}
    assert len(feeds["vic_mapshare_seasonal"]) == 1
    assert len(feeds["nsw_npws"]) == 1
    assert len(feeds["vic_emergency"]) == 1


def test_track_matcher_protocol_and_pydantic_models() -> None:
    class DummyMatcher:
        name = "dummy_v1"

        async def match_trace(
            self, points, *, search_radius_m: float = 35.0, min_confidence: float = 0.55
        ) -> MatchResult:
            return MatchResult(
                matcher_name=self.name,
                matched_segments=[
                    MatchedSegmentTraversal(
                        segment_id="tfseg_0123456789abcdef",
                        direction="forward",
                        entry_at=points[0].timestamp,
                        exit_at=points[-1].timestamp,
                        duration_s=12.5,
                        avg_speed_kmh=18.0,
                        match_confidence=0.92,
                        matched_point_count=len(points),
                    )
                ],
                unmatched_subtraces=[
                    UnmatchedSubtrace(
                        coordinates=[(148.135, -36.552), (148.132, -36.551)],
                        length_m=240.0,
                        started_at=points[0].timestamp,
                        ended_at=points[-1].timestamp,
                        reason="no_candidates",
                    )
                ],
                overall_confidence=0.92,
                matched_fraction=0.8,
            )

    matcher = DummyMatcher()
    assert isinstance(matcher, TrackMatcher)
    pt = GpsPoint(lat=-36.60, lon=148.12, timestamp=datetime(2026, 11, 20, 8, 0, tzinfo=UTC))
    assert pt.lat == -36.60


def test_zero_pyqgis_in_source_tree() -> None:
    for py_file in (REPO_ROOT / "src").rglob("*.py"):
        content = py_file.read_text(encoding="utf-8").lower()
        assert "qgis.core" not in content, f"Forbidden PyQGIS reference in {py_file}"
        assert "import qgis" not in content, f"Forbidden PyQGIS import in {py_file}"
