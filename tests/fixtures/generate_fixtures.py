#!/usr/bin/env python3
"""Deterministic generator for TrailForge's offline VIC/NSW Australian Alps corridor fixtures.

Generates all 12 compact files specified in docs/SPEC_GIS_SCHEMA.md Section 5:
  - manifest.json
  - osm_tracks.geojson
  - osm_tracks_v2_jittered.geojson
  - vic_tracks.geojson
  - nsw_tracks.geojson
  - dem_alps_corridor.tif (300x260 Float32 DEFLATE EPSG:4326 GeoTIFF, NoData=-9999.0)
  - waterways.geojson
  - capad_parks.geojson
  - pois.geojson
  - closures_sample.json
  - sample_trace_davies_plain.gpx
  - sample_trace_unmapped_spur.gpx

Runs 100% offline using only the Python standard library so it works identically on host
macOS/Linux (without local GDAL) and inside the multi-arch Docker image.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import struct
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parent

BBOX = (148.05, -36.68, 148.20, -36.55)  # min_lon, min_lat, max_lon, max_lat
DEM_COLS = 300
DEM_ROWS = 260
DEM_DX = 0.0005
DEM_DY = 0.0005
DEM_NODATA = -9999.0


def river_lat_at(lon: float) -> float:
    """Murray River valley axis meandering gently around lat = -36.565."""
    return -36.565 + 0.004 * math.sin((lon - 148.05) * 40.0)


def alps_corridor_elev_m(lon: float, lat: float) -> float:
    """Deterministic AHD elevation (600..1650 m) per docs/SPEC_GIS_SCHEMA.md Section 5.3."""
    r_lat = river_lat_at(lon)
    d_south = r_lat - lat  # positive moving south into VIC Alpine NP

    if d_south >= 0.0:
        escarpment = 780.0 / (1.0 + math.exp(-(d_south - 0.032) / 0.0048))
        ridge = 240.0 * math.exp(-((lon - 148.125) / 0.055) ** 2) * min(1.0, d_south / 0.065)
        valley_grade = 600.0 + 30.0 * abs(lon - 148.12) / 0.08
        undulation = 14.0 * math.sin((lon - 148.05) * 70.0) * math.cos((lat + 36.68) * 65.0)
        elev = valley_grade + escarpment + ridge + undulation
    else:
        d_north = -d_south
        foothill = 480.0 * min(1.0, d_north / 0.015) * (0.4 + 0.6 * ((lon - 148.05) / 0.15))
        elev = 605.0 + foothill + 10.0 * math.sin((lon - 148.05) * 55.0)

    return round(max(600.0, min(1650.0, elev)), 2)


def _interpolate_polyline(
    anchors: list[tuple[float, float]], points_per_segment: int
) -> list[list[float]]:
    coords: list[list[float]] = []
    for idx in range(len(anchors) - 1):
        lon0, lat0 = anchors[idx]
        lon1, lat1 = anchors[idx + 1]
        steps = points_per_segment
        for s in range(steps):
            t = s / steps
            coords.append([round(lon0 + (lon1 - lon0) * t, 6), round(lat0 + (lat1 - lat0) * t, 6)])
    coords.append([round(anchors[-1][0], 6), round(anchors[-1][1], 6)])
    return coords


def _offset_coords(
    coords: list[list[float]], dlon: float, dlat: float
) -> list[list[float]]:
    return [[round(c[0] + dlon, 6), round(c[1] + dlat, 6)] for c in coords]


def _encode_float32_predictor3_row(row_vals: list[float]) -> bytes:
    """Apply TIFF Technical Note 3 floating-point horizontal differencing (Predictor=3)."""
    n = len(row_vals)
    # TIFF Predictor=3 rearranges big-endian IEEE 754 bytes into 4 planes, then byte-diffs
    be_bytes = struct.pack(f">{n}f", *row_vals)
    reordered = bytearray(4 * n)
    reordered[0:n] = be_bytes[0::4]
    reordered[n : 2 * n] = be_bytes[1::4]
    reordered[2 * n : 3 * n] = be_bytes[2::4]
    reordered[3 * n : 4 * n] = be_bytes[3::4]
    out = bytearray(4 * n)
    out[0] = reordered[0]
    for idx in range(1, 4 * n):
        out[idx] = (reordered[idx] - reordered[idx - 1]) & 0xFF
    return bytes(out)


def _write_geotiff_float32_deflate(
    path: Path,
    cols: int,
    rows: int,
    origin_lon: float,
    origin_lat: float,
    dx: float,
    dy: float,
    nodata: float,
) -> tuple[float, float]:
    """Write a standards-compliant DEFLATE+PREDICTOR=3 Float32 EPSG:4326 GeoTIFF (stdlib only)."""
    rows_per_strip = 20
    strips: list[bytes] = []
    min_val = float("inf")
    max_val = float("-inf")

    for r0 in range(0, rows, rows_per_strip):
        r1 = min(rows, r0 + rows_per_strip)
        strip_buf = bytearray()
        for r in range(r0, r1):
            lat = origin_lat - (r + 0.5) * dy
            row_vals: list[float] = []
            for c in range(cols):
                lon = origin_lon + (c + 0.5) * dx
                z = alps_corridor_elev_m(lon, lat)
                if z < min_val:
                    min_val = z
                if z > max_val:
                    max_val = z
                row_vals.append(z)
            strip_buf.extend(_encode_float32_predictor3_row(row_vals))
        strips.append(zlib.compress(bytes(strip_buf), level=9))

    num_strips = len(strips)

    # TIFF layout:
    # [0..8) Header -> points to IFD at offset 8
    # IFD: 2 bytes count + 15 entries * 12 bytes + 4 bytes next_ifd(0)
    num_entries = 15
    ifd_offset = 8
    after_ifd = ifd_offset + 2 + num_entries * 12 + 4

    # External tag data payload (strictly after IFD)
    ext_buf = bytearray()

    def alloc_ext(data: bytes) -> int:
        if len(ext_buf) % 2 == 1:
            ext_buf.append(0)
        offset = after_ifd + len(ext_buf)
        ext_buf.extend(data)
        return offset

    strip_counts_bytes = struct.pack(f"<{num_strips}I", *(len(s) for s in strips))
    strip_counts_offset = alloc_ext(strip_counts_bytes)

    pixel_scale_offset = alloc_ext(struct.pack("<3d", dx, dy, 0.0))
    tiepoint_offset = alloc_ext(struct.pack("<6d", 0.0, 0.0, 0.0, origin_lon, origin_lat, 0.0))

    # GeoKeyDirectoryTag: version 1.1.0, 3 keys:
    #   1024 GTModelTypeGeoKey = 2 (ModelTypeGeographic)
    #   1025 GTRasterTypeGeoKey = 1 (RasterPixelIsArea)
    #   2048 GeographicTypeGeoKey = 4326 (GCS_WGS_84)
    geokeys = (1, 1, 0, 3, 1024, 0, 1, 2, 1025, 0, 1, 1, 2048, 0, 1, 4326)
    geokeys_offset = alloc_ext(struct.pack(f"<{len(geokeys)}H", *geokeys))

    nodata_bytes = f"{nodata:.1f}\x00".encode("ascii")
    nodata_offset = alloc_ext(nodata_bytes)

    # Reserve space for StripOffsets array before writing compressed strips
    if len(ext_buf) % 4 != 0:
        ext_buf.extend(b"\x00" * (4 - (len(ext_buf) % 4)))
    strip_offsets_pos = len(ext_buf)
    strip_offsets_file_offset = after_ifd + strip_offsets_pos
    ext_buf.extend(b"\x00" * (num_strips * 4))

    strip_data_start = after_ifd + len(ext_buf)
    strip_offsets: list[int] = []
    cursor = strip_data_start
    for s in strips:
        strip_offsets.append(cursor)
        cursor += len(s)

    struct.pack_into(f"<{num_strips}I", ext_buf, strip_offsets_pos, *strip_offsets)

    def ifd_entry(tag: int, dtype: int, count: int, val_or_offset: int) -> bytes:
        # dtype: 2=ASCII, 3=SHORT, 4=LONG, 12=DOUBLE
        if dtype == 3 and count == 1:
            return struct.pack("<HHIHH", tag, dtype, count, val_or_offset, 0)
        return struct.pack("<HHII", tag, dtype, count, val_or_offset)

    # Entries strictly sorted by ascending TIFF tag ID
    entries = [
        ifd_entry(256, 4, 1, cols),  # ImageWidth
        ifd_entry(257, 4, 1, rows),  # ImageLength
        ifd_entry(258, 3, 1, 32),  # BitsPerSample = 32
        ifd_entry(259, 3, 1, 8),  # Compression = 8 (DEFLATE)
        ifd_entry(262, 3, 1, 1),  # PhotometricInterpretation = MinIsBlack
        ifd_entry(273, 4, num_strips, strip_offsets_file_offset),  # StripOffsets
        ifd_entry(277, 3, 1, 1),  # SamplesPerPixel = 1
        ifd_entry(278, 4, 1, rows_per_strip),  # RowsPerStrip
        ifd_entry(279, 4, num_strips, strip_counts_offset),  # StripByteCounts
        ifd_entry(317, 3, 1, 3),  # Predictor = 3 (Floating-point horizontal diff)
        ifd_entry(339, 3, 1, 3),  # SampleFormat = IEEE Float
        ifd_entry(33550, 12, 3, pixel_scale_offset),  # ModelPixelScaleTag
        ifd_entry(33922, 12, 6, tiepoint_offset),  # ModelTiepointTag
        ifd_entry(34735, 3, len(geokeys), geokeys_offset),  # GeoKeyDirectoryTag
        ifd_entry(42113, 2, len(nodata_bytes), nodata_offset),  # GDAL_NODATA
    ]
    assert len(entries) == num_entries

    header = struct.pack("<2sHI", b"II", 42, ifd_offset)
    ifd = struct.pack("<H", num_entries) + b"".join(entries) + struct.pack("<I", 0)

    with path.open("wb") as fh:
        fh.write(header)
        fh.write(ifd)
        fh.write(ext_buf)
        for s in strips:
            fh.write(s)

    return round(min_val, 2), round(max_val, 2)


def _feature_collection(features: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features}


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _build_gpx(
    name: str,
    points: list[tuple[float, float]],
    start_time: datetime,
    step_seconds: int,
    rng: random.Random,
    noise_deg: float,
) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="TrailForge Synthetic Fixture Generator" '
        'xmlns="http://www.topografix.com/GPX/1/1">',
        "  <metadata>",
        f"    <name>{name}</name>",
        f"    <time>{start_time.strftime('%Y-%m-%dT%H:%M:%SZ')}</time>",
        "  </metadata>",
        "  <trk>",
        f"    <name>{name}</name>",
        "    <trkseg>",
    ]
    for i, (lon, lat) in enumerate(points):
        raw_lon = lon + rng.uniform(-noise_deg, noise_deg)
        raw_lat = lat + rng.uniform(-noise_deg, noise_deg)
        j_lon = max(BBOX[0] + 0.0005, min(BBOX[2] - 0.0005, raw_lon))
        j_lat = max(BBOX[1] + 0.0005, min(BBOX[3] - 0.0005, raw_lat))
        ele = alps_corridor_elev_m(j_lon, j_lat)
        ts = (start_time + timedelta(seconds=i * step_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(
            f'      <trkpt lat="{j_lat:.6f}" lon="{j_lon:.6f}">'
            f"<ele>{ele:.2f}</ele><time>{ts}</time></trkpt>"
        )
    lines.extend(["    </trkseg>", "  </trk>", "</gpx>", ""])
    return "\n".join(lines)


def _resample_polyline(coords: list[list[float]], count: int) -> list[tuple[float, float]]:
    # Cumulative Euclidean length in degrees (sufficient for compact synthetic spacing)
    cum = [0.0]
    for i in range(1, len(coords)):
        dx = coords[i][0] - coords[i - 1][0]
        dy = coords[i][1] - coords[i - 1][1]
        cum.append(cum[-1] + math.hypot(dx, dy))
    total = cum[-1]
    out: list[tuple[float, float]] = []
    for k in range(count):
        target = total * k / (count - 1)
        for i in range(1, len(cum)):
            if cum[i] >= target or i == len(cum) - 1:
                span = cum[i] - cum[i - 1]
                frac = 0.0 if span == 0 else (target - cum[i - 1]) / span
                lon = coords[i - 1][0] + (coords[i][0] - coords[i - 1][0]) * frac
                lat = coords[i - 1][1] + (coords[i][1] - coords[i - 1][1]) * frac
                out.append((lon, lat))
                break
    return out


def generate_all(output_dir: Path = FIXTURES_DIR) -> dict[str, Any]:
    """Generate all 12 deterministic synthetic fixtures and return the manifest dictionary."""
    output_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(42)

    # -------------------------------------------------------------------------
    # 1. Waterways (Murray River + Limestone Creek + synthetic Connector)
    # -------------------------------------------------------------------------
    murray_coords = [
        [round(148.055 + i * 0.01, 6), round(river_lat_at(148.055 + i * 0.01), 6)]
        for i in range(15)
    ]
    limestone_coords = _interpolate_polyline(
        [(148.070, -36.635), (148.105, -36.625), (148.140, -36.612), (148.175, -36.595)], 3
    )
    connector_coords = [[148.110, -36.566], [148.115, -36.565]]

    waterways_fc = _feature_collection(
        [
            {
                "type": "Feature",
                "properties": {
                    "source_provider": "ga_hydro",
                    "source_license": "CC-BY-4.0",
                    "source_feature_id": "GA-HYD-101",
                    "name": "Murray River",
                    "featuretype": "Watercourse",
                    "perenniality": "Perennial",
                    "hierarchy": "Major",
                },
                "geometry": {"type": "LineString", "coordinates": murray_coords},
            },
            {
                "type": "Feature",
                "properties": {
                    "source_provider": "ga_hydro",
                    "source_license": "CC-BY-4.0",
                    "source_feature_id": "GA-HYD-102",
                    "name": "Limestone Creek",
                    "featuretype": "Watercourse",
                    "perenniality": "Intermittent",
                    "hierarchy": "Minor",
                },
                "geometry": {"type": "LineString", "coordinates": limestone_coords},
            },
            {
                "type": "Feature",
                "properties": {
                    "source_provider": "ga_hydro",
                    "source_license": "CC-BY-4.0",
                    "source_feature_id": "GA-HYD-999",
                    "name": None,
                    "featuretype": "Connector",
                    "perenniality": "Unknown",
                    "hierarchy": "Minor",
                },
                "geometry": {"type": "LineString", "coordinates": connector_coords},
            },
        ]
    )
    _write_json(output_dir / "waterways.geojson", waterways_fc)

    # -------------------------------------------------------------------------
    # 2. CAPAD 2025 Protected Areas (Alpine NP in VIC, Kosciuszko NP in NSW)
    # -------------------------------------------------------------------------
    river_border = [
        [round(148.05 + i * 0.015, 6), round(river_lat_at(148.05 + i * 0.015), 6)]
        for i in range(11)
    ]
    alpine_ring = river_border + [[148.20, -36.68], [148.05, -36.68], river_border[0]]
    kosciuszko_ring = list(reversed(river_border)) + [
        [148.05, -36.55],
        [148.20, -36.55],
        river_border[-1],
    ]
    capad_fc = _feature_collection(
        [
            {
                "type": "Feature",
                "properties": {
                    "PA_ID": "CAPAD-VIC-ALPINE",
                    "NAME": "Alpine National Park",
                    "TYPE": "National Park",
                    "TYPE_ABBR": "NP",
                    "IUCN": "II",
                    "STATE": "VIC",
                },
                "geometry": {"type": "MultiPolygon", "coordinates": [[alpine_ring]]},
            },
            {
                "type": "Feature",
                "properties": {
                    "PA_ID": "CAPAD-NSW-KOSCIUSZKO",
                    "NAME": "Kosciuszko National Park",
                    "TYPE": "National Park",
                    "TYPE_ABBR": "NP",
                    "IUCN": "II",
                    "STATE": "NSW",
                },
                "geometry": {"type": "MultiPolygon", "coordinates": [[kosciuszko_ring]]},
            },
        ]
    )
    _write_json(output_dir / "capad_parks.geojson", capad_fc)

    # -------------------------------------------------------------------------
    # 3. OSM Tracks (7 ways: way/1001 .. way/1007)
    # -------------------------------------------------------------------------
    # way/1001: Tom Groggin Access Rd (NSW, north of river, gentle ~2-4 deg)
    way_1001_coords = _interpolate_polyline(
        [(148.065, -36.556), (148.095, -36.558), (148.120, -36.560)], 4
    )
    # way/1002: Davies Plain Track Lower (crosses Murray River at ~148.120,-36.565,
    # climbs steeply south across 19-22 deg escarpment around lat -36.597 to -36.618)
    way_1002_coords = _interpolate_polyline(
        [
            (148.120, -36.560),
            (148.120, -36.575),
            (148.121, -36.597),
            (148.123, -36.618),
        ],
        5,
    )
    # way/1003: Davies Plain Ridge (VIC alpine plateau 1250->1620m above snow-line)
    way_1003_coords = _interpolate_polyline(
        [(148.123, -36.618), (148.125, -36.640), (148.126, -36.662)], 4
    )
    # way/1004: Poplars Fire Trail (NSW foothills 640->1080m, 7-11 deg)
    way_1004_coords = _interpolate_polyline(
        [(148.120, -36.560), (148.140, -36.557), (148.162, -36.553)], 4
    )
    # way/1005: Station Creek Private Spur (VIC, access=private, barrier=gate)
    way_1005_coords = _interpolate_polyline(
        [(148.123, -36.618), (148.105, -36.614), (148.090, -36.610)], 3
    )
    # way/1006: Limestone Creek Bridged Link (VIC, bridge=yes crossing Limestone Creek)
    way_1006_coords = _interpolate_polyline(
        [(148.123, -36.618), (148.132, -36.615), (148.144, -36.609)], 3
    )
    # way/1007: Main Range Footpad (VIC, highway=path, motor_vehicle=no)
    way_1007_coords = _interpolate_polyline(
        [(148.126, -36.662), (148.138, -36.666), (148.152, -36.669)], 3
    )

    osm_features = [
        {
            "type": "Feature",
            "properties": {
                "osm_way_id": 1001,
                "osm_version": 1,
                "name": "Tom Groggin Access Rd",
                "highway": "unclassified",
                "surface": "gravel",
                "tracktype": "grade1",
                "smoothness": "good",
                "4wd_only": "no",
                "access": "public",
                "motor_vehicle": "yes",
                "bridge": None,
                "ford": None,
                "has_gate_barrier": False,
            },
            "geometry": {"type": "LineString", "coordinates": way_1001_coords},
        },
        {
            "type": "Feature",
            "properties": {
                "osm_way_id": 1002,
                "osm_version": 1,
                "name": "Davies Plain Track Lower",
                "highway": "track",
                "surface": "rock",
                "tracktype": "grade4",
                "smoothness": "very_bad",
                "4wd_only": "yes",
                "access": "public",
                "motor_vehicle": "yes",
                "bridge": None,
                "ford": "yes",
                "has_gate_barrier": False,
            },
            "geometry": {"type": "LineString", "coordinates": way_1002_coords},
        },
        {
            "type": "Feature",
            "properties": {
                "osm_way_id": 1003,
                "osm_version": 1,
                "name": "Davies Plain Ridge",
                "highway": "track",
                "surface": "dirt",
                "tracktype": "grade3",
                "smoothness": "bad",
                "4wd_only": "recommended",
                "access": "public",
                "motor_vehicle": "yes",
                "bridge": None,
                "ford": None,
                "has_gate_barrier": False,
            },
            "geometry": {"type": "LineString", "coordinates": way_1003_coords},
        },
        {
            "type": "Feature",
            "properties": {
                "osm_way_id": 1004,
                "osm_version": 1,
                "name": "Poplars Fire Trail",
                "highway": "track",
                "surface": "gravel",
                "tracktype": "grade3",
                "smoothness": "intermediate",
                "4wd_only": "recommended",
                "access": "public",
                "motor_vehicle": "yes",
                "bridge": None,
                "ford": None,
                "has_gate_barrier": False,
            },
            "geometry": {"type": "LineString", "coordinates": way_1004_coords},
        },
        {
            "type": "Feature",
            "properties": {
                "osm_way_id": 1005,
                "osm_version": 1,
                "name": "Station Creek Private Spur",
                "highway": "track",
                "surface": "dirt",
                "tracktype": "grade3",
                "smoothness": "bad",
                "4wd_only": "yes",
                "access": "private",
                "motor_vehicle": "private",
                "barrier": "gate",
                "locked": "yes",
                "bridge": None,
                "ford": None,
                "has_gate_barrier": True,
            },
            "geometry": {"type": "LineString", "coordinates": way_1005_coords},
        },
        {
            "type": "Feature",
            "properties": {
                "osm_way_id": 1006,
                "osm_version": 1,
                "name": "Limestone Creek Bridged Link",
                "highway": "track",
                "surface": "gravel",
                "tracktype": "grade2",
                "smoothness": "intermediate",
                "4wd_only": "no",
                "access": "public",
                "motor_vehicle": "yes",
                "bridge": "yes",
                "ford": None,
                "has_gate_barrier": False,
            },
            "geometry": {"type": "LineString", "coordinates": way_1006_coords},
        },
        {
            "type": "Feature",
            "properties": {
                "osm_way_id": 1007,
                "osm_version": 1,
                "name": "Main Range Footpad",
                "highway": "path",
                "surface": "ground",
                "tracktype": None,
                "smoothness": "bad",
                "4wd_only": None,
                "access": "public",
                "motor_vehicle": "no",
                "foot": "yes",
                "bridge": None,
                "ford": None,
                "has_gate_barrier": False,
            },
            "geometry": {"type": "LineString", "coordinates": way_1007_coords},
        },
    ]
    _write_json(output_dir / "osm_tracks.geojson", _feature_collection(osm_features))

    # -------------------------------------------------------------------------
    # 4. OSM v2 Jittered Re-Import Fixture (tests Pass 1, Pass 2, degree-2 merge)
    # -------------------------------------------------------------------------
    jitter_rng = random.Random(4202)
    way_1002_jittered = [
        [
            round(c[0] + jitter_rng.uniform(-0.000045, 0.000045), 6),
            round(c[1] + jitter_rng.uniform(-0.000040, 0.000040), 6),
        ]
        for c in reversed(way_1002_coords)
    ]
    mid_1004 = len(way_1004_coords) // 2
    v2_features: list[dict[str, Any]] = []
    for feat in osm_features:
        wid = feat["properties"]["osm_way_id"]
        if wid == 1001:
            props = dict(feat["properties"], osm_version=2, surface="asphalt")
            v2_features.append(
                {"type": "Feature", "properties": props, "geometry": feat["geometry"]}
            )
        elif wid == 1002:
            props = dict(feat["properties"], osm_version=2)
            v2_features.append(
                {
                    "type": "Feature",
                    "properties": props,
                    "geometry": {"type": "LineString", "coordinates": way_1002_jittered},
                }
            )
        elif wid == 1004:
            # Split at degree-2 midpoint vertex with identical tags
            props_a = dict(feat["properties"], osm_way_id=100401, osm_version=2)
            props_b = dict(feat["properties"], osm_way_id=100402, osm_version=2)
            v2_features.append(
                {
                    "type": "Feature",
                    "properties": props_a,
                    "geometry": {
                        "type": "LineString",
                        "coordinates": way_1004_coords[: mid_1004 + 1],
                    },
                }
            )
            v2_features.append(
                {
                    "type": "Feature",
                    "properties": props_b,
                    "geometry": {
                        "type": "LineString",
                        "coordinates": way_1004_coords[mid_1004:],
                    },
                }
            )
        else:
            v2_features.append(feat)

    _write_json(output_dir / "osm_tracks_v2_jittered.geojson", _feature_collection(v2_features))

    # -------------------------------------------------------------------------
    # 5. VIC State Tracks (DataVic tr_road & recweb_tracks)
    # -------------------------------------------------------------------------
    vic_overlap_coords = _offset_coords(way_1002_coords + way_1003_coords[1:], 0.00007, 0.00002)
    bucky_ridge_coords = _interpolate_polyline(
        [(148.144, -36.609), (148.155, -36.620), (148.166, -36.635), (148.172, -36.652)], 3
    )
    vic_walking_coords = _interpolate_polyline(
        [(148.080, -36.645), (148.095, -36.655), (148.110, -36.664)], 3
    )
    vic_fc = _feature_collection(
        [
            {
                "type": "Feature",
                "properties": {
                    "source_layer": "tr_road",
                    "pfi": "VIC-TR-2001",
                    "ezi_road_name_label": "DAVIES PLAIN TRACK",
                    "class_code": 7,
                    "road_seal": "3",
                    "feature_type_code": "road",
                    "trail_class": "4wd_track",
                    "is_vehicular": True,
                },
                "geometry": {"type": "MultiLineString", "coordinates": [vic_overlap_coords]},
            },
            {
                "type": "Feature",
                "properties": {
                    "source_layer": "tr_road",
                    "pfi": "VIC-TR-2002",
                    "ezi_road_name_label": "BUCKY RIDGE FIRE TRAIL",
                    "class_code": 6,
                    "road_seal": "2",
                    "feature_type_code": "fire_trail",
                    "trail_class": "fire_trail",
                    "is_vehicular": True,
                },
                "geometry": {"type": "MultiLineString", "coordinates": [bucky_ridge_coords]},
            },
            {
                "type": "Feature",
                "properties": {
                    "source_layer": "recweb_tracks",
                    "pfi": "VIC-RW-3001",
                    "ezi_road_name_label": "ALPINE WALKING TRACK",
                    "class_code": 8,
                    "road_seal": "3",
                    "feature_type_code": "trail",
                    "access_dsc": "Walking Track",
                    "trk_class": "Walking",
                    "trail_class": "walking_track",
                    "is_vehicular": False,
                },
                "geometry": {"type": "MultiLineString", "coordinates": [vic_walking_coords]},
            },
        ]
    )
    _write_json(output_dir / "vic_tracks.geojson", vic_fc)

    # -------------------------------------------------------------------------
    # 6. NSW State Tracks (NSW_Transport_Theme L9)
    # -------------------------------------------------------------------------
    nsw_overlap_coords = _offset_coords(way_1004_coords, 0.00005, -0.00005)
    pinch_river_coords = _interpolate_polyline(
        [(148.162, -36.553), (148.176, -36.555), (148.190, -36.558)], 3
    )
    nsw_fc = _feature_collection(
        [
            {
                "type": "Feature",
                "properties": {
                    "source_layer": "nsw_fire_trails_l9",
                    "objectid": 9001,
                    "classsubtype": 5,
                    "roadnamebase": "POPLARS",
                    "roadnametype": "FIRE TRAIL",
                    "brimsid": "FT-401",
                    "surface": "2",
                    "operationalstatus": "O",
                    "trail_class": "fire_trail",
                    "is_vehicular": True,
                },
                "geometry": {"type": "MultiLineString", "coordinates": [nsw_overlap_coords]},
            },
            {
                "type": "Feature",
                "properties": {
                    "source_layer": "nsw_fire_trails_l9",
                    "objectid": 9002,
                    "classsubtype": 5,
                    "roadnamebase": "PINCH RIVER",
                    "roadnametype": "FIRE TRAIL",
                    "brimsid": "FT-409",
                    "surface": "2",
                    "operationalstatus": "O",
                    "trail_class": "fire_trail",
                    "is_vehicular": True,
                },
                "geometry": {"type": "MultiLineString", "coordinates": [pinch_river_coords]},
            },
        ]
    )
    _write_json(output_dir / "nsw_tracks.geojson", nsw_fc)

    # -------------------------------------------------------------------------
    # 7. POIs (6 points including 35 m duplicate Davies Plain Hut for dedupe)
    # -------------------------------------------------------------------------
    pois_fc = _feature_collection(
        [
            {
                "type": "Feature",
                "properties": {
                    "source_provider": "nsw_poi",
                    "source_license": "CC-BY-4.0",
                    "source_feature_id": "NSW-POI-101",
                    "name": "Tom Groggin Campground",
                    "poi_type": "campground",
                    "state": "NSW",
                    "elev_m": 612.0,
                    "access_note": "2WD/AWD accessible in dry conditions",
                },
                "geometry": {"type": "Point", "coordinates": [148.1195, -36.5602]},
            },
            {
                "type": "Feature",
                "properties": {
                    "source_provider": "vic_recweb_hut",
                    "source_license": "CC-BY-4.0",
                    "source_feature_id": "VIC-HUT-201",
                    "name": "Davies Plain Hut",
                    "poi_type": "hut",
                    "state": "VIC",
                    "elev_m": 1615.0,
                    "access_note": "Emergency shelter only; alpine snow zone",
                },
                "geometry": {"type": "Point", "coordinates": [148.1260, -36.6620]},
            },
            {
                "type": "Feature",
                "properties": {
                    "source_provider": "osm",
                    "source_license": "ODbL",
                    "source_feature_id": "node/88001",
                    "name": "Davies Plain Hut",
                    "poi_type": "hut",
                    "state": "VIC",
                    "elev_m": 1614.0,
                    "access_note": "Duplicate OSM node ~35 m away for dedupe testing",
                },
                "geometry": {"type": "Point", "coordinates": [148.12635, -36.66215]},
            },
            {
                "type": "Feature",
                "properties": {
                    "source_provider": "vic_geomark",
                    "source_license": "CC-BY-4.0",
                    "source_feature_id": "TS-4812",
                    "name": "Davies Trig (TS 4812)",
                    "poi_type": "trig",
                    "state": "VIC",
                    "elev_m": 1648.0,
                    "access_note": None,
                },
                "geometry": {"type": "Point", "coordinates": [148.1285, -36.6645]},
            },
            {
                "type": "Feature",
                "properties": {
                    "source_provider": "vic_foi",
                    "source_license": "CC-BY-4.0",
                    "source_feature_id": "VIC-FOI-305",
                    "name": "Murray Lookout",
                    "poi_type": "lookout",
                    "state": "VIC",
                    "elev_m": 1290.0,
                    "access_note": None,
                },
                "geometry": {"type": "Point", "coordinates": [148.1225, -36.6150]},
            },
            {
                "type": "Feature",
                "properties": {
                    "source_provider": "osm",
                    "source_license": "ODbL",
                    "source_feature_id": "node/88002",
                    "name": "Station Creek Locked Gate",
                    "poi_type": "gate",
                    "state": "VIC",
                    "elev_m": 1180.0,
                    "access_note": "access=private; locked gate across Station Creek Spur",
                },
                "geometry": {"type": "Point", "coordinates": [148.1180, -36.6168]},
            },
        ]
    )
    _write_json(output_dir / "pois.geojson", pois_fc)

    # -------------------------------------------------------------------------
    # 8. Closures & Emergency Incident Mock Payloads (Slice 04 offline)
    # -------------------------------------------------------------------------
    closures_sample = {
        "generated_at": "2026-11-26T00:00:00Z",
        "feeds": {
            "vic_mapshare_seasonal": [
                {
                    "external_id": "VIC-SEASONAL-DAVIES-2026",
                    "title": "Davies Plain Track — Winter Seasonal Closure",
                    "alert_type": "seasonal_closure",
                    "severity": "closed",
                    "status": "scheduled",
                    "state": "VIC",
                    "description": (
                        "Closed annually from Queen's Birthday weekend (June) to late October "
                        "to protect alpine soils and track surfaces."
                    ),
                    "source_url": "https://mapshare.vic.gov.au/mapsharevic/",
                    "effective_from": "2026-06-08T00:00:00Z",
                    "effective_to": "2026-10-31T23:59:59Z",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[148.120, -36.570], [148.123, -36.618], [148.126, -36.662]],
                    },
                }
            ],
            "nsw_npws": [
                {
                    "external_id": "NPWS-KOSCI-2026-01",
                    "title": "Kosciuszko National Park — Alpine Track Wet Conditions Advisory",
                    "alert_type": "hazard_warning",
                    "severity": "advisory",
                    "status": "active",
                    "state": "NSW",
                    "park_name": "Kosciuszko National Park",
                    "description": (
                        "High river levels possible at Tom Groggin crossing after alpine rain."
                    ),
                    "source_url": "https://www.nationalparks.nsw.gov.au/alerts",
                    "effective_from": "2026-11-20T00:00:00Z",
                    "effective_to": None,
                    "geometry": None,
                }
            ],
            "vic_emergency": [
                {
                    "external_id": "VICEM-2026-9941",
                    "title": "Bucky Ridge — Planned Fuel Reduction Burn Watch & Act",
                    "alert_type": "bushfire",
                    "severity": "watch_and_act",
                    "status": "active",
                    "state": "VIC",
                    "description": (
                        "Smoke and active fire appliances along Bucky Ridge Fire Trail."
                    ),
                    "source_url": "https://emergency.vic.gov.au/respond/",
                    "effective_from": "2026-11-25T22:00:00Z",
                    "effective_to": "2026-11-27T12:00:00Z",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [148.150, -36.618],
                                [148.175, -36.618],
                                [148.175, -36.645],
                                [148.150, -36.645],
                                [148.150, -36.618],
                            ]
                        ],
                    },
                }
            ],
        },
    }
    _write_json(output_dir / "closures_sample.json", closures_sample)

    # -------------------------------------------------------------------------
    # 9. Synthetic DEM GeoTIFF (300x260 Float32 DEFLATE EPSG:4326)
    # -------------------------------------------------------------------------
    dem_path = output_dir / "dem_alps_corridor.tif"
    elev_min_m, elev_max_m = _write_geotiff_float32_deflate(
        path=dem_path,
        cols=DEM_COLS,
        rows=DEM_ROWS,
        origin_lon=BBOX[0],
        origin_lat=BBOX[3],
        dx=DEM_DX,
        dy=DEM_DY,
        nodata=DEM_NODATA,
    )

    # -------------------------------------------------------------------------
    # 10. Sample GPX Traces (Davies Plain full traverse + Unmapped logging spur)
    # -------------------------------------------------------------------------
    davies_path = way_1002_coords + way_1003_coords[1:]
    davies_pts = _resample_polyline(davies_path, 120)
    davies_gpx = _build_gpx(
        name="Davies Plain Traverse (Tom Groggin to Davies Plain Hut)",
        points=davies_pts,
        start_time=datetime(2026, 11, 20, 8, 0, 0, tzinfo=UTC),
        step_seconds=25,
        rng=rng,
        noise_deg=0.00005,  # ~5 m GPS noise
    )
    (output_dir / "sample_trace_davies_plain.gpx").write_text(davies_gpx, encoding="utf-8")

    # Starts along Poplars Fire Trail (way/1004) then branches ~500 m north-west onto unmapped spur
    unmapped_path = way_1004_coords[:5] + [
        [148.140, -36.557],
        [148.138, -36.554],
        [148.135, -36.552],
        [148.132, -36.551],
    ]
    unmapped_pts = _resample_polyline(unmapped_path, 45)
    unmapped_gpx = _build_gpx(
        name="Poplars Fire Trail + Unmapped Logging Spur",
        points=unmapped_pts,
        start_time=datetime(2026, 11, 21, 9, 30, 0, tzinfo=UTC),
        step_seconds=18,
        rng=rng,
        noise_deg=0.00004,
    )
    (output_dir / "sample_trace_unmapped_spur.gpx").write_text(unmapped_gpx, encoding="utf-8")

    # -------------------------------------------------------------------------
    # 11. Manifest with expected counts & SHA-256 checksums
    # -------------------------------------------------------------------------
    fixture_filenames = [
        "osm_tracks.geojson",
        "osm_tracks_v2_jittered.geojson",
        "vic_tracks.geojson",
        "nsw_tracks.geojson",
        "dem_alps_corridor.tif",
        "waterways.geojson",
        "capad_parks.geojson",
        "pois.geojson",
        "closures_sample.json",
        "sample_trace_davies_plain.gpx",
        "sample_trace_unmapped_spur.gpx",
    ]
    files_meta: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    for fname in fixture_filenames:
        fpath = output_dir / fname
        raw = fpath.read_bytes()
        total_bytes += len(raw)
        files_meta[fname] = {
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    manifest = {
        "corridor": "Davies Plain / Tom Groggin / Murray River (VIC-NSW Australian Alps)",
        "seed": 42,
        "bbox": list(BBOX),
        "crs": {
            "storage": "EPSG:4326",
            "metric": "EPSG:3577",
            "utm": "EPSG:32755",
            "mgrs_zone": "55H FV",
        },
        "dem": {
            "filename": "dem_alps_corridor.tif",
            "cols": DEM_COLS,
            "rows": DEM_ROWS,
            "dx_deg": DEM_DX,
            "dy_deg": DEM_DY,
            "dtype": "Float32",
            "compression": "DEFLATE",
            "nodata": DEM_NODATA,
            "elev_min_m": elev_min_m,
            "elev_max_m": elev_max_m,
        },
        "expected_counts": {
            "osm_tracks": len(osm_features),
            "osm_tracks_v2_jittered": len(v2_features),
            "vic_tracks": len(vic_fc["features"]),
            "nsw_tracks": len(nsw_fc["features"]),
            "waterways": len(waterways_fc["features"]),
            "capad_parks": len(capad_fc["features"]),
            "pois": len(pois_fc["features"]),
            "closures_feeds": len(closures_sample["feeds"]),
            "gpx_davies_plain_points": len(davies_pts),
            "gpx_unmapped_spur_points": len(unmapped_pts),
        },
        "total_data_bytes": total_bytes,
        "files": files_meta,
    }
    _write_json(output_dir / "manifest.json", manifest)
    return manifest


if __name__ == "__main__":
    result = generate_all()
    print(
        f"Generated {len(result['files']) + 1} fixture files in {FIXTURES_DIR} "
        f"({result['total_data_bytes'] / 1024:.1f} KiB data; "
        f"DEM {result['dem']['elev_min_m']}..{result['dem']['elev_max_m']} m AHD)"
    )
