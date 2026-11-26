"""100% offline unit tests for pure-Pillow MCP PNG visual renderers & payload budget guards."""

from __future__ import annotations

import base64
import io

from mcp import types
from PIL import Image

from trailforge.mcp.renderer import (
    COMPARISON_PNG_SIZE,
    ELEVATION_PNG_SIZE,
    MAP_CARD_PNG_SIZE,
    MAX_PNG_BYTES,
    render_comparison_png,
    render_elevation_profile_png,
    render_map_card_png,
)
from trailforge.mcp.schemas import (
    MAX_CALL_TOOL_RESULT_BYTES,
    CompactClosureItem,
    CompactCrossingItem,
    CompactElevationPoint,
    CompactPoiItem,
    CompactReportItem,
    CompactSegmentGeom,
    CompactTrackCard,
    CompareTrackEntry,
    downsample_compact_profile,
    enforce_call_tool_result_budget,
    serialized_result_size_bytes,
    simplify_linestring_coords,
)


def _decode_png(data: bytes) -> Image.Image:
    assert data.startswith(b"\x89PNG\r\n\x1a\n"), "Output must have valid PNG magic header"
    assert len(data) <= MAX_PNG_BYTES, f"PNG exceeded 120 KB cap: {len(data)} bytes"
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def _has_non_uniform_pixels(img: Image.Image) -> bool:
    extrema = img.convert("RGB").getextrema()
    return any(hi - lo > 40 for lo, hi in extrema)


def _sample_profile(
    n: int = 80, start_elev: float = 650.0, peak_elev: float = 1620.0
) -> list[CompactElevationPoint]:
    grades = ["Easy", "Medium", "Hard", "Extreme"]
    pts: list[CompactElevationPoint] = []
    for i in range(n):
        frac = i / max(1, n - 1)
        elev = start_elev + (peak_elev - start_elev) * (
            0.5 - 0.5 * (1.0 - 2.0 * frac) ** 2 + 0.5 * frac
        )
        slope = 4.0 + 18.0 * abs(frac - 0.5)
        pts.append(
            CompactElevationPoint(
                dist_m=round(frac * 12_400.0, 1),
                elev_m=round(elev, 1),
                slope_deg=round(slope, 1),
                grade=grades[min(3, int(frac * 4))],
                segment_id=f"seg_{i // 10}",
            )
        )
    return pts


def _sample_segments() -> list[CompactSegmentGeom]:
    return [
        CompactSegmentGeom(
            segment_id="seg_1",
            grade="Easy",
            slope_deg=5.2,
            length_m=1800.0,
            is_steep=False,
            coords=[[148.090, -36.560], [148.105, -36.575], [148.118, -36.588]],
        ),
        CompactSegmentGeom(
            segment_id="seg_2",
            grade="Medium",
            slope_deg=11.0,
            length_m=2400.0,
            is_steep=False,
            coords=[[148.118, -36.588], [148.124, -36.608], [148.128, -36.625]],
        ),
        CompactSegmentGeom(
            segment_id="seg_3",
            grade="Hard",
            slope_deg=16.5,
            length_m=2100.0,
            is_steep=True,
            coords=[[148.128, -36.625], [148.132, -36.642], [148.140, -36.652]],
        ),
        CompactSegmentGeom(
            segment_id="seg_4",
            grade="Extreme",
            slope_deg=22.4,
            length_m=1600.0,
            is_steep=True,
            coords=[[148.140, -36.652], [148.155, -36.662]],
        ),
    ]


def test_render_elevation_profile_png_rich_and_under_budget() -> None:
    png = render_elevation_profile_png(
        title="Davies Plain Track — Elevation Profile",
        points=_sample_profile(120),
        grade="Extreme",
        total_length_m=12_400.0,
        elev_gain_m=1080.0,
        elev_loss_m=210.0,
        max_slope_deg=22.4,
        snowline_m=1000,
        crossings=[
            CompactCrossingItem(name="Murray River", perennial=True, bridged=False, dist_m=1400.0),
            CompactCrossingItem(
                name="Buckwong Creek", perennial=False, bridged=True, dist_m=7200.0
            ),
        ],
        suitability_label="Touring 4WD: Challenging",
    )
    img = _decode_png(png)
    assert img.size == ELEVATION_PNG_SIZE
    assert _has_non_uniform_pixels(img)
    # Verify vibrant grade/accent colors rendered (not a blank monochrome canvas)
    rgb = img.convert("RGB")
    raw_pixels = rgb.get_flattened_data() if hasattr(rgb, "get_flattened_data") else rgb.getdata()
    distinct_colors = len(set(raw_pixels))
    assert distinct_colors > 120


def test_render_elevation_profile_png_handles_empty_and_flat_gracefully() -> None:
    empty_png = render_elevation_profile_png(
        title="Empty Profile Track",
        points=[],
        grade="Easy",
    )
    empty_img = _decode_png(empty_png)
    assert empty_img.size == ELEVATION_PNG_SIZE
    assert _has_non_uniform_pixels(empty_img)

    flat_pts = [
        CompactElevationPoint(dist_m=0.0, elev_m=800.0, slope_deg=0.0, grade="Easy"),
        CompactElevationPoint(dist_m=500.0, elev_m=800.0, slope_deg=0.0, grade="Easy"),
    ]
    flat_png = render_elevation_profile_png(
        title="Flat Track",
        points=flat_pts,
        grade="Easy",
        total_length_m=500.0,
    )
    flat_img = _decode_png(flat_png)
    assert flat_img.size == ELEVATION_PNG_SIZE


def test_render_map_card_png_with_pois_closures_and_reports() -> None:
    png = render_map_card_png(
        title="Davies Plain Traverse",
        subtitle="VIC/NSW · 4 legs · 11.4 km",
        segments=_sample_segments(),
        closures=[
            CompactClosureItem(
                closure_id=1,
                title="Winter Seasonal Closure",
                agency="DEECA VIC",
                closure_type="seasonal",
                severity="warning",
                state="VIC",
                lon=148.124,
                lat=-36.608,
            )
        ],
        reports=[
            CompactReportItem(
                id="rep-1",
                report_type="locked_gate",
                report_type_label="Locked Gate",
                is_gate_warning=True,
                severity="impassable",
                title="Padlocked Gate",
                notes="Boundary fence locked",
                lon=148.132,
                lat=-36.642,
            )
        ],
        pois=[
            CompactPoiItem(
                id=1,
                name="Tom Groggin Camp",
                poi_type="campground",
                state="VIC",
                lon=148.095,
                lat=-36.565,
            ),
            CompactPoiItem(
                id=2,
                name="Davies Plain Hut",
                poi_type="hut",
                state="VIC",
                elev_m=1620.0,
                lon=148.152,
                lat=-36.660,
            ),
            CompactPoiItem(
                id=3,
                name="Murray River Crossing",
                poi_type="water_point",
                state="NSW",
                lon=148.110,
                lat=-36.580,
            ),
        ],
        show_endpoints=True,
    )
    img = _decode_png(png)
    assert img.size == MAP_CARD_PNG_SIZE
    assert _has_non_uniform_pixels(img)


def test_render_map_card_png_handles_empty_geometry_and_degenerate_bbox() -> None:
    png = render_map_card_png(
        title="Empty Area",
        subtitle="No tracks",
        segments=[],
        bbox=[148.12, -36.60, 148.12, -36.60],
    )
    img = _decode_png(png)
    assert img.size == MAP_CARD_PNG_SIZE
    assert _has_non_uniform_pixels(img)


def test_render_comparison_png_two_to_four_tracks_and_empty() -> None:
    entries = [
        CompareTrackEntry(
            summary=CompactTrackCard(
                track_id=f"tftrk_{i}",
                name=f"Alpine Track {i + 1}",
                state="VIC",
                grade=["Easy", "Medium", "Hard", "Extreme"][i],
                total_length_m=6000.0 + i * 2500.0,
                elev_gain_m=320.0 + i * 210.0,
                max_slope_deg=8.5 + i * 4.5,
                waterway_crossing_count=i,
                closure_status="open" if i != 2 else "seasonal",
            ),
            elevation_profile=_sample_profile(
                60, start_elev=600.0 + i * 60.0, peak_elev=1200.0 + i * 160.0
            ),
            color_hex=["#38BDF8", "#F59E0B", "#22C55E", "#E879F9"][i],
        )
        for i in range(4)
    ]
    png = render_comparison_png(
        title="Compare: 4 High Country Tracks",
        tracks=entries,
        snowline_m=1000,
    )
    img = _decode_png(png)
    assert img.size == COMPARISON_PNG_SIZE
    assert _has_non_uniform_pixels(img)

    empty_cmp = render_comparison_png(title="Empty Compare", tracks=[])
    assert _decode_png(empty_cmp).size == COMPARISON_PNG_SIZE


def test_simplification_downsampling_and_450kb_result_budget_guard() -> None:
    # Dense wavy line simplifies cleanly with bounded precision
    dense = [[148.0 + i * 0.0001, -36.6 + (0.00002 if i % 2 else 0.0)] for i in range(400)]
    simplified = simplify_linestring_coords(
        dense, tolerance_deg=0.00012, precision=5, max_points=50
    )
    assert 2 <= len(simplified) <= 50
    assert simplified[0] == [148.0, -36.6]

    # Elevation profile downsampling preserves start, end, min, max, and steepest peak
    prof = _sample_profile(900)
    prof[450] = CompactElevationPoint(dist_m=6200.0, elev_m=1899.0, slope_deg=31.5, grade="Extreme")
    down = downsample_compact_profile(prof, max_points=180)
    assert len(down) <= 180
    assert down[0].dist_m == prof[0].dist_m
    assert down[-1].dist_m == prof[-1].dist_m
    assert any(p.elev_m == 1899.0 for p in down)

    # Oversized CallToolResult gets pruned strictly below 450 KB ceiling
    huge_segs = [
        {
            "segment_id": f"seg_{i}",
            "grade": "Hard",
            "slope_deg": 15.0,
            "length_m": 90.0,
            "is_steep": True,
            "coords": [[148.0 + j * 0.00001, -36.6 - j * 0.00001] for j in range(120)],
        }
        for i in range(160)
    ]
    fake_png_b64 = base64.b64encode(b"\x89PNG" + (b"A" * 110_000)).decode("ascii")
    oversized = types.CallToolResult(
        content=[
            types.TextContent(type="text", text="Route summary"),
            types.ImageContent(type="image", data=fake_png_b64, mimeType="image/png"),
            types.ImageContent(type="image", data=fake_png_b64, mimeType="image/png"),
        ],
        structuredContent={
            "schema_version": "1.0",
            "view_mode": "route",
            "route": {
                "legs": [{"leg_index": 0, "track_id": "t1", "segments": huge_segs}],
                "elevation_profile": [p.model_dump() for p in prof],
            },
        },
        isError=False,
    )
    assert serialized_result_size_bytes(oversized) > MAX_CALL_TOOL_RESULT_BYTES
    bounded = enforce_call_tool_result_budget(oversized)
    assert serialized_result_size_bytes(bounded) <= MAX_CALL_TOOL_RESULT_BYTES
