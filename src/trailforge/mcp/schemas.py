"""Locked v1.0 wire schemas (`TrailForgeViewPayload`), simplification, and budget guards."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any, Final, Literal
from urllib.parse import quote, urlencode

from mcp import types
from pydantic import BaseModel, Field
from shapely.geometry import LineString

from trailforge.ingestion.connectors.base import (
    ATTRIBUTION_CAPAD,
    ATTRIBUTION_GA_DEM,
    ATTRIBUTION_GA_HYDRO,
    ATTRIBUTION_OPEN_METEO,
    ATTRIBUTION_OSM,
    ATTRIBUTION_STATE_FEEDS,
    attributions_for_states,
)

# Strictly < 450 KB (450 * 1024 = 460,800; keep headroom for JSON-RPC envelope)
MAX_CALL_TOOL_RESULT_BYTES: Final[int] = 445_000
DEFAULT_SIMPLIFY_TOLERANCE_DEG: Final[float] = 0.00012
LARGE_ROUTE_SIMPLIFY_TOLERANCE_DEG: Final[float] = 0.00025
COMPARE_COLORS: Final[tuple[str, ...]] = ("#38BDF8", "#F59E0B", "#22C55E", "#E879F9")

ViewModeLiteral = Literal["explore", "track", "route", "compare"]
CapabilityClassLiteral = Literal["AWD", "Stock 4WD", "Touring 4WD", "Modified 4WD"]


class RigContext(BaseModel):
    """Active vehicle rig context displayed in the MCP App and PNG header."""

    capability_class: CapabilityClassLiteral
    vehicle_name: str | None = None
    has_snorkel: bool | None = None


class TileConfig(BaseModel):
    """Optional PMTiles / basemap URLs for MapLibre GL JS primary rendering in MCP App."""

    vector_pmtiles_url: str | None = None
    terrain_pmtiles_url: str | None = None
    basemap_raster_url: str | None = None


class CompactSegmentGeom(BaseModel):
    """Simplified canonical segment polyline for interactive map & PNG rendering."""

    segment_id: str
    grade: str = "Medium"
    slope_deg: float | None = None
    length_m: float = 0.0
    is_steep: bool = False
    coords: list[list[float]] = Field(default_factory=list)


class CompactElevationPoint(BaseModel):
    """Single sample in a downsampled elevation profile."""

    dist_m: float
    elev_m: float
    slope_deg: float = 0.0
    grade: str | None = None
    segment_id: str | None = None


class CompactSuitability(BaseModel):
    """Normalized rig suitability summary (Section 7 contract)."""

    rating: str
    summary: str
    reasons: list[str] = Field(default_factory=list)
    vehicle_class: str | None = None


class CompactTrackCard(BaseModel):
    """Compact track summary card used across explore, track, and compare views."""

    track_id: str
    name: str
    state: str
    trail_class: str = "4wd_track"
    grade: str | None = None
    score: float | None = None
    live_grade: str | None = None
    total_length_m: float = 0.0
    elev_min_m: float | None = None
    elev_max_m: float | None = None
    elev_gain_m: float = 0.0
    elev_loss_m: float = 0.0
    max_slope_deg: float | None = None
    waterway_crossing_count: int = 0
    closure_status: str = "open"
    wet_risk_level: str = "dry"
    primary_park_name: str | None = None
    bbox: list[float] | None = None
    suitability: CompactSuitability | None = None
    deep_link_url: str = ""
    coords_preview: list[list[float]] = Field(default_factory=list)


class CompactClosureItem(BaseModel):
    """Compact official agency closure/alert item (zero PII)."""

    closure_id: int
    title: str
    agency: str
    closure_type: str
    severity: str
    state: str
    status: str = "active"
    effective_from: str | None = None
    effective_to: str | None = None
    source_url: str | None = None
    lon: float | None = None
    lat: float | None = None
    affected_track_ids: list[str] = Field(default_factory=list)


class CompactReportItem(BaseModel):
    """Compact community condition/gate report item (strictly zero email/user_id)."""

    id: str
    report_type: str
    report_type_label: str
    is_gate_warning: bool = False
    severity: str = "caution"
    title: str
    notes: str = ""
    freshness_score: float = 1.0
    is_stale: bool = False
    observed_at: str | None = None
    track_id: str | None = None
    track_name: str | None = None
    lon: float | None = None
    lat: float | None = None
    osm_edit_url: str | None = None


class CompactPoiItem(BaseModel):
    """Compact backcountry POI (campground, hut, water point, lookout, trig, gate)."""

    id: int
    name: str
    poi_type: str
    state: str
    elev_m: float | None = None
    lon: float
    lat: float
    distance_m: float | None = None
    access_note: str | None = None
    capad_park_name: str | None = None


class CompactCrossingItem(BaseModel):
    """Compact waterway crossing entry on a track or route."""

    name: str
    perennial: bool = False
    bridged: bool = False
    dist_m: float = 0.0


class CompactParkRef(BaseModel):
    """Compact CAPAD protected area reference."""

    name: str
    park_type: str
    state: str


class CompactLiveSummary(BaseModel):
    """Compact live crowdsourced telemetry & rainfall summary for a track."""

    live_grade: str | None = None
    confidence_level: str = "none"
    sample_count: int = 0
    last_driven_at: str | None = None
    avg_speed_kmh: float | None = None
    est_duration_min: float | None = None
    vehicle_class_split: dict[str, int] = Field(default_factory=dict)
    wet_risk_level: str = "dry"
    rain_72h_mm: float | None = None


class CompactRainfallSummary(BaseModel):
    """Regional 24 h / 72 h rainfall context summary."""

    rain_24h_mm: float | None = None
    rain_72h_mm: float | None = None
    wet_risk_level: str = "dry"


class ExploreViewData(BaseModel):
    """Payload section for ``view_mode == 'explore'``."""

    tracks: list[CompactTrackCard] = Field(default_factory=list)
    closures: list[CompactClosureItem] = Field(default_factory=list)
    reports: list[CompactReportItem] = Field(default_factory=list)
    pois: list[CompactPoiItem] = Field(default_factory=list)
    rainfall_summary: CompactRainfallSummary | None = None


class TrackViewData(BaseModel):
    """Payload section for ``view_mode == 'track'``."""

    summary: CompactTrackCard
    segments: list[CompactSegmentGeom] = Field(default_factory=list)
    elevation_profile: list[CompactElevationPoint] = Field(default_factory=list)
    crossings: list[CompactCrossingItem] = Field(default_factory=list)
    baseline_breakdown: dict[str, Any] | None = None
    live_summary: CompactLiveSummary | None = None
    closures: list[CompactClosureItem] = Field(default_factory=list)
    gate_warnings: list[CompactReportItem] = Field(default_factory=list)
    condition_reports: list[CompactReportItem] = Field(default_factory=list)
    nearby_pois: list[CompactPoiItem] = Field(default_factory=list)
    parks: list[CompactParkRef] = Field(default_factory=list)
    export_gpx_url: str = ""
    export_geojson_url: str = ""


class RouteSafetySummaryData(BaseModel):
    """Compact route safety counts & wet-risk flags (Section 7 contract)."""

    has_blocking_closure: bool = False
    closure_count: int = 0
    gate_warning_count: int = 0
    wet_risk_level: str = "dry"
    waterway_crossing_count: int = 0
    perennial_crossing_count: int = 0
    gap_count: int = 0
    max_gap_m: float = 0.0


class RouteLegData(BaseModel):
    """Per-leg geometry & metadata inside ``RouteViewData``."""

    leg_index: int
    track_id: str
    name: str
    reversed: bool = False
    length_m: float = 0.0
    grade: str | None = None
    gap_from_prev_m: float = 0.0
    segments: list[CompactSegmentGeom] = Field(default_factory=list)


class RouteWaypointData(BaseModel):
    """Custom waypoint attached to a planned route."""

    name: str
    category: str = "waypoint"
    lon: float
    lat: float


class RouteViewData(BaseModel):
    """Payload section for ``view_mode == 'route'``."""

    name: str
    saved_route_id: str | None = None
    total_length_m: float = 0.0
    est_duration_min: float = 0.0
    elev_gain_m: float = 0.0
    elev_loss_m: float = 0.0
    elev_min_m: float | None = None
    elev_max_m: float | None = None
    worst_grade: str | None = None
    suitability: CompactSuitability | None = None
    safety_summary: RouteSafetySummaryData = Field(default_factory=RouteSafetySummaryData)
    legs: list[RouteLegData] = Field(default_factory=list)
    elevation_profile: list[CompactElevationPoint] = Field(default_factory=list)
    closures: list[CompactClosureItem] = Field(default_factory=list)
    gate_warnings: list[CompactReportItem] = Field(default_factory=list)
    waypoints: list[RouteWaypointData] = Field(default_factory=list)
    nearby_pois: list[CompactPoiItem] = Field(default_factory=list)
    gridforge_cli: str = ""
    export_gpx_url: str = ""


class CompareTrackEntry(BaseModel):
    """Single track entry inside ``CompareViewData``."""

    summary: CompactTrackCard
    segments: list[CompactSegmentGeom] = Field(default_factory=list)
    elevation_profile: list[CompactElevationPoint] = Field(default_factory=list)
    color_hex: str


class CompareViewData(BaseModel):
    """Payload section for ``view_mode == 'compare'``."""

    tracks: list[CompareTrackEntry] = Field(default_factory=list)


class TrailForgeViewPayload(BaseModel):
    """Locked v1.0 Backend <-> SEP-1865 MCP App structuredContent envelope (Section 7)."""

    schema_version: Literal["1.0"] = "1.0"
    view_mode: ViewModeLiteral
    title: str
    subtitle: str | None = None
    bbox: list[float] | None = None
    public_base_url: str
    deep_link_url: str
    snowline_m: int = 1000
    attributions: list[str] = Field(default_factory=list)
    rig_context: RigContext | None = None
    tile_config: TileConfig | None = None
    explore: ExploreViewData | None = None
    track: TrackViewData | None = None
    route: RouteViewData | None = None
    compare: CompareViewData | None = None


# -----------------------------------------------------------------------------
# Attributions & Deep Links
# -----------------------------------------------------------------------------


def build_mcp_attributions(
    states: Sequence[str] | None = None,
    *,
    include_rainfall: bool = True,
) -> list[str]:
    """Assemble mandatory OSM ODbL, GA/DEA, CAPAD, state agency & Open-Meteo attributions."""
    norm_states: list[str] = []
    for raw in states or ("VIC", "NSW"):
        for part in str(raw).split("/"):
            code = part.strip().upper()
            if code and code not in norm_states:
                norm_states.append(code)
    if not norm_states:
        norm_states = ["VIC", "NSW"]

    attrs: list[str] = [
        ATTRIBUTION_OSM,
        ATTRIBUTION_GA_DEM,
        ATTRIBUTION_GA_HYDRO,
        ATTRIBUTION_CAPAD,
        *attributions_for_states(norm_states),
    ]
    if include_rainfall:
        attrs.append(ATTRIBUTION_OPEN_METEO)
    attrs.append(ATTRIBUTION_STATE_FEEDS)
    return attrs


def format_attribution_markdown(attributions: Sequence[str]) -> str:
    """Render a compact Markdown attribution footer required on every tool TextContent."""
    joined = " · ".join(attributions)
    return f"\n\n---\n*Attributions: {joined}*"


def build_track_deep_link(
    public_base_url: str,
    track_id: str,
    *,
    vehicle_class: str | None = None,
) -> str:
    base = public_base_url.rstrip("/")
    params: dict[str, str] = {"track": track_id}
    if vehicle_class:
        params["rig"] = vehicle_class
    return f"{base}/?{urlencode(params)}"


def build_route_deep_link(
    public_base_url: str,
    track_ids: Sequence[str],
    *,
    vehicle_class: str | None = None,
) -> str:
    base = public_base_url.rstrip("/")
    route_param = ",".join(quote(tid, safe="") for tid in track_ids)
    query = f"route={route_param}"
    if vehicle_class:
        query += f"&rig={quote(vehicle_class, safe='')}"
    return f"{base}/?{query}"


def build_area_deep_link(
    public_base_url: str,
    *,
    region: str | None = None,
    bbox: Sequence[float] | None = None,
) -> str:
    base = public_base_url.rstrip("/")
    params: dict[str, str] = {}
    if region:
        params["region"] = region
    elif bbox and len(bbox) == 4:
        lon = (float(bbox[0]) + float(bbox[2])) / 2.0
        lat = (float(bbox[1]) + float(bbox[3])) / 2.0
        params["lat"] = f"{lat:.4f}"
        params["lon"] = f"{lon:.4f}"
        params["z"] = "11"
    return f"{base}/?{urlencode(params)}" if params else f"{base}/"


# -----------------------------------------------------------------------------
# Geometry Simplification, Profile Downsampling & Payload Budget Guard
# -----------------------------------------------------------------------------


def simplify_linestring_coords(
    coords: Sequence[Sequence[float]],
    *,
    tolerance_deg: float = DEFAULT_SIMPLIFY_TOLERANCE_DEG,
    precision: int = 5,
    max_points: int = 120,
) -> list[list[float]]:
    """Simplify a lon/lat coordinate sequence using Shapely while preserving endpoints."""
    cleaned: list[tuple[float, float]] = []
    for pt in coords:
        if len(pt) >= 2:
            lon = float(pt[0])
            lat = float(pt[1])
            if math.isfinite(lon) and math.isfinite(lat):
                cleaned.append((lon, lat))
    if len(cleaned) <= 2:
        return [[round(x, precision), round(y, precision)] for x, y in cleaned]

    try:
        line = LineString(cleaned)
        simplified = line.simplify(tolerance_deg, preserve_topology=False)
        out = [
            [round(float(x), precision), round(float(y), precision)] for x, y in simplified.coords
        ]
    except Exception:
        out = [[round(x, precision), round(y, precision)] for x, y in cleaned]

    if len(out) > max_points and max_points >= 2:
        step = (len(out) - 1) / (max_points - 1)
        idxs = sorted({min(len(out) - 1, round(k * step)) for k in range(max_points)})
        out = [out[i] for i in idxs]
    return out


def downsample_compact_profile(
    points: Sequence[CompactElevationPoint],
    *,
    max_points: int = 350,
) -> list[CompactElevationPoint]:
    """Uniformly downsample elevation points preserving start, end, min, max, and steepest peaks."""
    n = len(points)
    if n <= max_points or max_points < 2:
        return list(points)

    min_idx = min(range(n), key=lambda i: points[i].elev_m)
    max_idx = max(range(n), key=lambda i: points[i].elev_m)
    steep_idx = max(range(n), key=lambda i: points[i].slope_deg)
    keep: set[int] = {0, n - 1, min_idx, max_idx, steep_idx}

    remaining = max(0, max_points - len(keep))
    buckets = max(1, remaining // 2)
    bucket_size = n / buckets
    for b in range(buckets):
        start_i = int(b * bucket_size)
        end_i = min(n, int((b + 1) * bucket_size))
        if start_i >= end_i:
            continue
        b_min = start_i
        b_max = start_i
        for idx in range(start_i + 1, end_i):
            if points[idx].elev_m < points[b_min].elev_m:
                b_min = idx
            if points[idx].elev_m > points[b_max].elev_m:
                b_max = idx
        keep.add(b_min)
        keep.add(b_max)

    ordered = sorted(keep)
    if len(ordered) > max_points:
        protected = {0, n - 1, min_idx, max_idx, steep_idx}
        inner = [i for i in ordered if i not in protected]
        need = max(0, max_points - len(protected))
        if need > 0 and inner:
            step = len(inner) / need
            picked = [inner[min(len(inner) - 1, int(k * step))] for k in range(need)]
        else:
            picked = []
        ordered = sorted(protected | set(picked))[:max_points]

    return [points[i] for i in ordered]


def compute_bbox_from_coords(
    coord_lists: Sequence[Sequence[Sequence[float]]],
) -> list[float] | None:
    """Compute ``[min_lon, min_lat, max_lon, max_lat]`` from one or more coordinate rings/lines."""
    min_lon = math.inf
    min_lat = math.inf
    max_lon = -math.inf
    max_lat = -math.inf
    for seq in coord_lists:
        for pt in seq:
            if len(pt) < 2:
                continue
            lon, lat = float(pt[0]), float(pt[1])
            if not (math.isfinite(lon) and math.isfinite(lat)):
                continue
            if lon < min_lon:
                min_lon = lon
            if lon > max_lon:
                max_lon = lon
            if lat < min_lat:
                min_lat = lat
            if lat > max_lat:
                max_lat = lat
    if not math.isfinite(min_lon):
        return None
    return [round(min_lon, 6), round(min_lat, 6), round(max_lon, 6), round(max_lat, 6)]


def serialized_result_size_bytes(result: types.CallToolResult) -> int:
    """Measure exact UTF-8 serialized JSON size of a ``CallToolResult``."""
    raw = json.dumps(
        result.model_dump(by_alias=True, exclude_none=True),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return len(raw.encode("utf-8"))


def _simplify_payload_dict(
    payload: dict[str, Any],
    *,
    tolerance_deg: float,
    max_profile_pts: int,
    max_seg_pts: int,
    drop_explore_previews: bool = False,
) -> dict[str, Any]:
    """Return a reduced copy of a serialized ``TrailForgeViewPayload`` dict."""
    data = json.loads(json.dumps(payload))

    def _shrink_segments(segs: list[dict[str, Any]]) -> None:
        for seg in segs:
            coords = seg.get("coords")
            if isinstance(coords, list) and len(coords) > 2:
                seg["coords"] = simplify_linestring_coords(
                    coords,
                    tolerance_deg=tolerance_deg,
                    precision=4 if tolerance_deg >= 0.0005 else 5,
                    max_points=max_seg_pts,
                )

    def _shrink_profile(pts: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
        if len(pts) <= cap:
            return pts
        parsed = [CompactElevationPoint.model_validate(p) for p in pts]
        down = downsample_compact_profile(parsed, max_points=cap)
        return [p.model_dump(exclude_none=True) for p in down]

    track = data.get("track")
    if isinstance(track, dict):
        if isinstance(track.get("segments"), list):
            _shrink_segments(track["segments"])
        if isinstance(track.get("elevation_profile"), list):
            track["elevation_profile"] = _shrink_profile(
                track["elevation_profile"], max_profile_pts
            )

    route = data.get("route")
    if isinstance(route, dict):
        for leg in route.get("legs") or []:
            if isinstance(leg, dict) and isinstance(leg.get("segments"), list):
                _shrink_segments(leg["segments"])
        if isinstance(route.get("elevation_profile"), list):
            route["elevation_profile"] = _shrink_profile(
                route["elevation_profile"], max_profile_pts
            )

    compare = data.get("compare")
    if isinstance(compare, dict):
        for entry in compare.get("tracks") or []:
            if isinstance(entry, dict):
                if isinstance(entry.get("segments"), list):
                    _shrink_segments(entry["segments"])
                if isinstance(entry.get("elevation_profile"), list):
                    entry["elevation_profile"] = _shrink_profile(
                        entry["elevation_profile"], max(60, max_profile_pts // 2)
                    )

    explore = data.get("explore")
    if isinstance(explore, dict):
        for card in explore.get("tracks") or []:
            if isinstance(card, dict) and isinstance(card.get("coords_preview"), list):
                if drop_explore_previews:
                    card["coords_preview"] = []
                elif len(card["coords_preview"]) > 2:
                    card["coords_preview"] = simplify_linestring_coords(
                        card["coords_preview"],
                        tolerance_deg=tolerance_deg,
                        precision=4,
                        max_points=max_seg_pts,
                    )

    return data


def enforce_call_tool_result_budget(
    result: types.CallToolResult,
    *,
    max_bytes: int = MAX_CALL_TOOL_RESULT_BYTES,
) -> types.CallToolResult:
    """Guarantee a ``CallToolResult`` stays strictly under ``max_bytes`` (Section 7.1)."""
    if serialized_result_size_bytes(result) <= max_bytes:
        return result

    content = list(result.content)
    structured = result.structuredContent

    # Stage 1: progressively simplify geometries & downsample elevation profiles
    for tol, prof_cap, seg_cap, drop_prev in (
        (LARGE_ROUTE_SIMPLIFY_TOLERANCE_DEG, 220, 40, False),
        (0.0005, 140, 20, True),
        (0.0012, 90, 10, True),
    ):
        if isinstance(structured, dict):
            structured = _simplify_payload_dict(
                structured,
                tolerance_deg=tol,
                max_profile_pts=prof_cap,
                max_seg_pts=seg_cap,
                drop_explore_previews=drop_prev,
            )
            candidate = types.CallToolResult(
                content=content,
                structuredContent=structured,
                isError=result.isError,
            )
            if serialized_result_size_bytes(candidate) <= max_bytes:
                return candidate

    # Stage 2: drop secondary ImageContent blocks from the end before touching text
    while True:
        img_indices = [
            idx for idx, item in enumerate(content) if isinstance(item, types.ImageContent)
        ]
        if not img_indices:
            break
        content.pop(img_indices[-1])
        candidate = types.CallToolResult(
            content=content,
            structuredContent=structured,
            isError=result.isError,
        )
        if serialized_result_size_bytes(candidate) <= max_bytes:
            return candidate

    # Stage 3: last-resort trim of large list fields in structuredContent while keeping core keys
    if isinstance(structured, dict):
        for section_key in ("route", "track", "explore"):
            sec = structured.get(section_key)
            if isinstance(sec, dict):
                for list_key in ("segments", "nearby_pois", "condition_reports", "pois", "tracks"):
                    if isinstance(sec.get(list_key), list) and len(sec[list_key]) > 20:
                        sec[list_key] = sec[list_key][:20]
                if section_key == "route" and isinstance(sec.get("legs"), list):
                    for leg in sec["legs"]:
                        if isinstance(leg, dict) and isinstance(leg.get("segments"), list):
                            leg["segments"] = leg["segments"][:5]

    return types.CallToolResult(
        content=content,
        structuredContent=structured,
        isError=result.isError,
    )
