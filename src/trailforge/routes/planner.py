"""Interactive multi-track route planning engine with auto-orientation & safety aggregation.

Stitches 1..30 canonical TrailForge tracks end-to-end (automatically orienting each leg
to minimize end-to-start jumps and flagging gaps > 100 m), aggregates difficulty,
duration, elevation profile, closures, gate warnings, waterway crossings, nearby POIs,
CAPAD tenure, rig suitability, and generates a ready-to-run GridForge strip-atlas CLI.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, defer, load_only

from trailforge.api.tracks import ParkSummary, PoiResponse
from trailforge.db.models import (
    POI,
    CAPADPark,
    ClosureAlert,
    SourceWaterway,
    Track,
    TrackSegment,
    TrackSegmentGrade,
    VehicleProfile,
)
from trailforge.export.formats import EXPORT_ATTRIBUTIONS
from trailforge.feeds.queries import get_closures_for_track_ids
from trailforge.grading.live import get_live_summaries_for_tracks
from trailforge.grading.rainfall import (
    OPEN_METEO_ATTRIBUTION,
    ApiWetRiskLiteral,
    db_to_api_wet_risk,
)
from trailforge.ingestion.connectors.base import ATTRIBUTION_GA_HYDRO
from trailforge.reports.queries import get_reports_for_tracks
from trailforge.vehicles.classifier import (
    CapabilityClassLiteral,
    TrackSuitability,
    evaluate_track_suitability,
    normalize_capability_class,
)

MAX_ROUTE_TRACKS: Final[int] = 30
LEG_GAP_THRESHOLD_M: Final[float] = 100.0
MAX_PROFILE_POINTS: Final[int] = 600
_NEARBY_POI_RADIUS_M: Final[float] = 1500.0
_MAX_NEARBY_POIS: Final[int] = 40
_MAX_ROUTE_SEGMENTS: Final[int] = 3_000
_MAX_ROUTE_COORDINATES: Final[int] = 24_000
_MAX_ROUTE_PROFILE_SAMPLES: Final[int] = 32_000
_MAX_ROUTE_SAFETY_ITEMS: Final[int] = 512
_MAX_ROUTE_ANALYSIS_BYTES: Final[int] = 2 * 1024 * 1024
MAX_ROUTE_EXPORT_BYTES: Final[int] = 4 * 1024 * 1024
_EARTH_RADIUS_M: Final[float] = 6_371_008.8

GRADE_SPEED_KMH: Final[dict[str, float]] = {
    "Easy": 28.0,
    "Medium": 18.0,
    "Hard": 11.0,
    "Extreme": 7.0,
}

_GRADE_RANK: Final[dict[str, int]] = {
    "Easy": 1,
    "Medium": 2,
    "Hard": 3,
    "Extreme": 4,
}
_RANK_GRADE: Final[dict[int, str]] = {v: k for k, v in _GRADE_RANK.items()}

_WET_RANK: Final[dict[ApiWetRiskLiteral, int]] = {
    "dry": 0,
    "damp": 1,
    "wet": 2,
    "heavy_rain": 3,
}
_RANK_WET: Final[dict[int, ApiWetRiskLiteral]] = {v: k for k, v in _WET_RANK.items()}

_CLOSURE_STATUS_RANK: Final[dict[str, int]] = {
    "open": 0,
    "info": 1,
    "warning": 2,
    "gated": 3,
    "seasonal": 4,
    "closed": 5,
}
_SURFACE_RANK: Final[dict[str, int]] = {
    "unknown": 0,
    "sealed": 1,
    "gravel_compacted": 2,
    "dirt_unsealed": 3,
    "rough_rock_mud_sand": 4,
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_TITLE_UNSAFE_RE = re.compile(r'["`$\\\r\n\t]+')

ROUTE_ATTRIBUTIONS: Final[list[str]] = [
    *EXPORT_ATTRIBUTIONS,
    ATTRIBUTION_GA_HYDRO,
    OPEN_METEO_ATTRIBUTION,
]


class TrackNotFoundError(ValueError):
    """Raised when one or more requested canonical track IDs do not exist or are inactive."""

    def __init__(self, missing_track_ids: Sequence[str]) -> None:
        self.missing_track_ids = list(missing_track_ids)
        joined = ", ".join(self.missing_track_ids)
        super().__init__(f"Unknown or inactive track_id(s): {joined}")


class RouteWorkBudgetExceeded(ValueError):
    """The route exceeded the work budget for full analysis or export."""

    def __init__(self) -> None:
        super().__init__(
            "Route has too much detail to analyze in one request. Split the route and retry."
        )


class RouteWaypoint(BaseModel):
    """Custom user waypoint attached to a planned route."""

    name: str
    category: str = "waypoint"
    lon: float = Field(..., ge=-180.0, le=180.0)
    lat: float = Field(..., ge=-90.0, le=90.0)


class RouteLegGap(BaseModel):
    """Non-contiguous gap (> 100 m) between consecutive stitched route legs."""

    from_track_id: str
    to_track_id: str
    from_track_name: str | None = None
    to_track_name: str | None = None
    gap_m: float


class RouteLegSummary(BaseModel):
    """Per-leg summary in an ordered multi-track route."""

    index: int
    track_id: str
    name: str
    state: str
    length_m: float
    est_duration_min: float
    grade: str
    score: float
    baseline_grade: str | None = None
    live_grade: str | None = None
    closure_status: str = "open"
    gate_warning_count: int = 0
    reversed: bool = False
    gap_from_prev_m: float = 0.0
    start_dist_m: float = 0.0
    end_dist_m: float = 0.0


class RouteElevationPoint(BaseModel):
    """Single sample in the continuous stitched multi-track elevation profile."""

    dist_m: float
    elev_m: float
    slope_deg: float
    grade: str | None = None
    track_id: str
    track_name: str


class RouteWaterwayCrossing(BaseModel):
    """Waterway crossing along the stitched route with cumulative route distance."""

    name: str
    waterway_type: str
    perennial: bool
    bridged: bool
    dist_m: float
    track_id: str
    track_name: str
    lon: float | None = None
    lat: float | None = None


class RouteSafetySummary(BaseModel):
    """Consolidated safety, access, weather, and hazard summary across all route legs."""

    has_blocking_closure: bool = False
    closure_count: int = 0
    gate_warning_count: int = 0
    active_report_count: int = 0
    unbridged_crossings: int = 0
    wet_risk_level: Literal["dry", "damp", "wet", "heavy_rain"] = "dry"
    warnings: list[str] = Field(default_factory=list)


class PlannedRouteAnalysis(BaseModel):
    """Complete live-computed analysis for a chained multi-track route."""

    name: str
    track_ids: list[str]
    states: list[str] = Field(default_factory=list)
    total_length_m: float
    est_duration_min: float
    min_elev_m: float | None = None
    max_elev_m: float | None = None
    elev_gain_m: float = 0.0
    elev_loss_m: float = 0.0
    max_slope_deg: float | None = None
    sustained_slope_deg: float | None = None
    worst_grade: str
    worst_score: float
    effective_grade: str
    suitability: TrackSuitability | None = None
    safety_summary: RouteSafetySummary
    closures: list[dict[str, Any]] = Field(default_factory=list)
    community_gate_warnings: list[dict[str, Any]] = Field(default_factory=list)
    condition_reports: list[dict[str, Any]] = Field(default_factory=list)
    waterway_crossings: list[RouteWaterwayCrossing] = Field(default_factory=list)
    nearby_pois: list[PoiResponse] = Field(default_factory=list)
    capad_parks: list[ParkSummary] = Field(default_factory=list)
    elevation_profile: list[RouteElevationPoint] = Field(default_factory=list)
    legs: list[RouteLegSummary] = Field(default_factory=list)
    leg_gaps: list[RouteLegGap] = Field(default_factory=list)
    waypoints: list[RouteWaypoint] = Field(default_factory=list)
    bbox: list[float]
    start_coord: list[float] | None = None
    end_coord: list[float] | None = None
    geometry: dict[str, Any]
    gridforge_cli: str
    has_osm_source: bool = False
    attributions: list[str] = Field(default_factory=lambda: list(ROUTE_ATTRIBUTIONS))


@dataclass(frozen=True, slots=True)
class OrientedLegTrack:
    """Oriented 3D polyline for one leg, retained for GPX 1.1 & GeoJSON export."""

    index: int
    track_id: str
    name: str
    state: str
    grade: str
    score: float
    length_m: float
    closure_status: str
    reversed: bool
    gap_from_prev_m: float
    coords_lonlat_ele: list[tuple[float, float, float | None]]


@dataclass(slots=True)
class _RawProfileSample:
    local_dist_m: float
    elev_m: float
    slope_deg: float
    grade: str | None


@dataclass(slots=True)
class _TrackGeometryData:
    track: Track
    coords_fwd: list[tuple[float, float, float | None]]
    profile_fwd: list[_RawProfileSample]
    seg_mid_dist_fwd: dict[str, float]
    length_m: float
    elev_min_m: float | None
    elev_max_m: float | None
    elev_gain_fwd_m: float
    elev_loss_fwd_m: float
    max_slope_deg: float | None
    sustained_slope_deg: float | None
    worst_surface: str
    is_4wd_only: bool
    has_perennial_crossing: bool
    unbridged_seg_crossings: int
    baseline_grade: str
    baseline_score: float
    effective_grade_from_segs: str
    wet_risk_level: ApiWetRiskLiteral
    park_ids: set[int]


def slugify_route_name(name: str) -> str:
    """Convert a route name into a safe lowercase filename/CLI slug."""
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug[:80].strip("-") or "trailforge-route"


def sanitize_gridforge_title(name: str) -> str:
    """Format an uppercase shell-safe title for GridForge `--title`."""
    cleaned = _TITLE_UNSAFE_RE.sub(" ", name)
    collapsed = " ".join(cleaned.split()).upper()
    return collapsed[:120] or "TRAILFORGE ROUTE"


def build_gridforge_cli(route_name: str) -> str:
    """Generate the ready-to-run GridForge 300 DPI strip-atlas Docker command."""
    slug = slugify_route_name(route_name)
    title = sanitize_gridforge_title(route_name)
    return (
        'docker run --rm -v "$(pwd)/out:/out" gridforge:dev '
        f"gridforge render --strip-route /out/{slug}.gpx "
        "--scale 25000 --paper a4 --dem-source au-elvis "
        "--au-overlay au-state-trails --elevation-profile --route-card "
        f'--calibration-page --title "{title}" '
        f"--output /out/{slug}_strip_atlas.pdf"
    )


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Compute great-circle distance in meters between two WGS84 points."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return _EARTH_RADIUS_M * c


def optimize_leg_orientations(
    leg_endpoints: Sequence[tuple[tuple[float, float], tuple[float, float]] | None],
) -> tuple[list[bool], list[float]]:
    """Choose per-leg forward/reverse flags minimizing total end-to-start jump distance.

    Uses exact 2-state dynamic programming (`O(N)`) so even leg 0 flips cleanly when its
    start connects to leg 1. Ties break toward forward (`reversed=False`).
    """
    n = len(leg_endpoints)
    if n == 0:
        return [], []
    if n == 1:
        return [False], [0.0]

    # Tiny penalty per reversed leg so equal-distance paths preserve canonical forward direction
    rev_tiebreak = 1e-3
    dp: list[list[float]] = [[0.0, rev_tiebreak] for _ in range(n)]
    backptr: list[list[int]] = [[0, 0] for _ in range(n)]

    for i in range(1, n):
        prev_ep = leg_endpoints[i - 1]
        curr_ep = leg_endpoints[i]
        for curr_r in (0, 1):
            best_cost = math.inf
            best_prev = 0
            for prev_r in (0, 1):
                if prev_ep is None or curr_ep is None:
                    jump = 0.0
                else:
                    prev_exit = prev_ep[1] if prev_r == 0 else prev_ep[0]
                    curr_entry = curr_ep[0] if curr_r == 0 else curr_ep[1]
                    jump = haversine_m(prev_exit[0], prev_exit[1], curr_entry[0], curr_entry[1])
                cand = dp[i - 1][prev_r] + jump + (rev_tiebreak if curr_r == 1 else 0.0)
                if cand < best_cost:
                    best_cost = cand
                    best_prev = prev_r
            dp[i][curr_r] = best_cost
            backptr[i][curr_r] = best_prev

    chosen = [0] * n
    chosen[n - 1] = 0 if dp[n - 1][0] <= dp[n - 1][1] else 1
    for i in range(n - 1, 0, -1):
        chosen[i - 1] = backptr[i][chosen[i]]

    reversals = [bool(r) for r in chosen]
    gaps: list[float] = [0.0]
    for i in range(1, n):
        prev_ep = leg_endpoints[i - 1]
        curr_ep = leg_endpoints[i]
        if prev_ep is None or curr_ep is None:
            gaps.append(0.0)
        else:
            prev_exit = prev_ep[0] if reversals[i - 1] else prev_ep[1]
            curr_entry = curr_ep[1] if reversals[i] else curr_ep[0]
            dist_m = haversine_m(prev_exit[0], prev_exit[1], curr_entry[0], curr_entry[1])
            gaps.append(round(dist_m, 1))

    return reversals, gaps


def downsample_elevation_profile(
    points: Sequence[RouteElevationPoint],
    *,
    max_points: int = MAX_PROFILE_POINTS,
) -> list[RouteElevationPoint]:
    """Deterministically downsample an elevation profile to `<= max_points` keeping legs crisp.

    Preserves start, finish, every leg boundary transition, and local min/max extrema per bucket.
    """
    n = len(points)
    if n <= max_points or max_points < 2:
        return list(points)

    keep: set[int] = {0, n - 1}
    for i in range(1, n):
        if points[i].track_id != points[i - 1].track_id:
            keep.add(i - 1)
            keep.add(i)

    if len(keep) >= max_points:
        # Extreme edge case (many legs & very low max_points): uniform stride including endpoints
        step = (n - 1) / (max_points - 1)
        idxs = sorted({min(n - 1, round(k * step)) for k in range(max_points)})
        return [points[i] for i in idxs[:max_points]]

    remaining = max_points - len(keep)
    # Use min/max bucket sampling so steep peaks and valley crossings stay visually sharp
    buckets = max(1, remaining // 2)
    bucket_size = n / buckets
    for b in range(buckets):
        start_i = int(b * bucket_size)
        end_i = min(n, int((b + 1) * bucket_size))
        if start_i >= end_i:
            continue
        min_i = start_i
        max_i = start_i
        for idx in range(start_i + 1, end_i):
            if points[idx].elev_m < points[min_i].elev_m:
                min_i = idx
            if points[idx].elev_m > points[max_i].elev_m:
                max_i = idx
        keep.add(min_i)
        keep.add(max_i)

    # Trim if bucket min/max exceeded budget by a couple points, while always protecting 0 & n-1
    ordered = sorted(keep)
    if len(ordered) > max_points:
        inner = ordered[1:-1]
        step = len(inner) / (max_points - 2)
        picked = [inner[min(len(inner) - 1, int(k * step))] for k in range(max_points - 2)]
        ordered = [ordered[0], *picked, ordered[-1]]

    return [points[i] for i in ordered]


def _wet_for_suitability(
    wet_level: ApiWetRiskLiteral,
) -> Literal["dry", "damp", "wet", "saturated"]:
    if wet_level == "heavy_rain":
        return "saturated"
    return wet_level


def _normalize_waypoints(
    waypoints: Sequence[RouteWaypoint | dict[str, Any]] | None,
) -> list[RouteWaypoint]:
    if not waypoints:
        return []
    out: list[RouteWaypoint] = []
    for raw in waypoints:
        if isinstance(raw, RouteWaypoint):
            wp = raw
        elif isinstance(raw, dict):
            wp = RouteWaypoint.model_validate(raw)
        else:
            raise ValueError("Invalid waypoint payload")
        clean_name = wp.name.strip()
        if not clean_name:
            raise ValueError("Waypoint name cannot be blank")
        clean_cat = (wp.category or "waypoint").strip() or "waypoint"
        out.append(
            RouteWaypoint(
                name=clean_name,
                category=clean_cat,
                lon=round(float(wp.lon), 6),
                lat=round(float(wp.lat), 6),
            )
        )
    return out


def _stitch_single_track(
    track: Track,
    seg_rows: list[tuple[TrackSegment, str, TrackSegmentGrade | None]],
) -> _TrackGeometryData:
    """Assemble forward-oriented geometry, elevation profile, and segment stats for one track."""
    coords_fwd: list[tuple[float, float, float | None]] = []
    profile_fwd: list[_RawProfileSample] = []
    seg_mid_dist_fwd: dict[str, float] = {}

    cum_dist = 0.0
    curr_exit_node: str | None = None
    elev_min: float | None = None
    elev_max: float | None = None
    elev_gain_fwd = 0.0
    elev_loss_fwd = 0.0
    max_slope: float | None = None
    sustained_slope: float | None = None
    worst_surface = "unknown"
    is_4wd_only = False
    has_perennial = False
    unbridged_seg_crossings = 0
    worst_base_rank = 0
    worst_eff_rank = 0
    max_base_score = 0.0
    worst_wet_rank = 0
    park_ids: set[int] = set()

    if track.primary_park_id is not None:
        park_ids.add(int(track.primary_park_id))

    for idx, (seg, geom_json, grd) in enumerate(seg_rows):
        reverse_seg = False
        if idx == 0:
            if len(seg_rows) >= 2:
                nxt = seg_rows[1][0]
                nxt_nodes = {nxt.start_node_key, nxt.end_node_key}
                if seg.end_node_key in nxt_nodes:
                    reverse_seg = False
                    curr_exit_node = seg.end_node_key
                elif seg.start_node_key in nxt_nodes:
                    reverse_seg = True
                    curr_exit_node = seg.start_node_key
                else:
                    curr_exit_node = seg.end_node_key
            else:
                curr_exit_node = seg.end_node_key
        else:
            if seg.start_node_key == curr_exit_node:
                reverse_seg = False
                curr_exit_node = seg.end_node_key
            elif seg.end_node_key == curr_exit_node:
                reverse_seg = True
                curr_exit_node = seg.start_node_key
            else:
                reverse_seg = False
                curr_exit_node = seg.end_node_key

        seg_len = float(seg.length_m or 0.0)
        seg_mid_dist_fwd[seg.segment_id] = cum_dist + (seg_len * 0.5)

        if seg.capad_park_id is not None:
            park_ids.add(int(seg.capad_park_id))

        if seg.elev_min_m is not None and (elev_min is None or seg.elev_min_m < elev_min):
            elev_min = float(seg.elev_min_m)
        if seg.elev_max_m is not None and (elev_max is None or seg.elev_max_m > elev_max):
            elev_max = float(seg.elev_max_m)

        if reverse_seg:
            elev_gain_fwd += float(seg.elev_loss_m or 0.0)
            elev_loss_fwd += float(seg.elev_gain_m or 0.0)
        else:
            elev_gain_fwd += float(seg.elev_gain_m or 0.0)
            elev_loss_fwd += float(seg.elev_loss_m or 0.0)

        if seg.max_slope_deg is not None and (
            max_slope is None or float(seg.max_slope_deg) > max_slope
        ):
            max_slope = float(seg.max_slope_deg)
        if seg.sustained_slope_deg is not None and (
            sustained_slope is None or float(seg.sustained_slope_deg) > sustained_slope
        ):
            sustained_slope = float(seg.sustained_slope_deg)
        if _SURFACE_RANK.get(seg.surface_class, 0) > _SURFACE_RANK.get(worst_surface, 0):
            worst_surface = seg.surface_class

        is_4wd_only = is_4wd_only or bool(seg.is_4wd_only)
        has_perennial = has_perennial or bool(seg.has_perennial_crossing and not seg.is_bridged)
        if not seg.is_bridged:
            unbridged_seg_crossings += int(seg.waterway_crossing_count or 0)

        eff_grade: str | None = None
        if grd is not None:
            eff_grade = grd.effective_grade
            worst_base_rank = max(worst_base_rank, _GRADE_RANK.get(grd.baseline_grade, 0))
            worst_eff_rank = max(worst_eff_rank, _GRADE_RANK.get(grd.effective_grade, 0))
            max_base_score = max(max_base_score, float(grd.baseline_score or 0.0))
            rc = grd.rainfall_context if isinstance(grd.rainfall_context, dict) else {}
            raw_wet = str(rc.get("wet_risk_level") or grd.wet_risk_level or "dry")
            api_wet = db_to_api_wet_risk(raw_wet)
            worst_wet_rank = max(worst_wet_rank, _WET_RANK.get(api_wet, 0))

        # Coordinates with linearly interpolated segment elevation
        gj = json.loads(geom_json)
        raw_coords = list(gj.get("coordinates") or [])
        if reverse_seg:
            raw_coords.reverse()

        start_ele = seg.elev_end_m if reverse_seg else seg.elev_start_m
        end_ele = seg.elev_start_m if reverse_seg else seg.elev_end_m
        n_pts = len(raw_coords)
        for p_idx, pair in enumerate(raw_coords):
            if idx > 0 and p_idx == 0 and coords_fwd:
                continue
            lon, lat = float(pair[0]), float(pair[1])
            ele: float | None = None
            if start_ele is not None and end_ele is not None and n_pts >= 2:
                frac = p_idx / (n_pts - 1)
                ele = round(float(start_ele) + (float(end_ele) - float(start_ele)) * frac, 2)
            elif seg.elev_min_m is not None:
                ele = round(float(seg.elev_min_m), 2)
            coords_fwd.append((lon, lat, ele))

        # Segment elevation profile points
        seg_slope = round(float(seg.max_slope_deg or 0.0), 2)
        raw_profile = seg.elevation_profile_m or []
        parsed: list[tuple[float, float]] = [
            (float(p[0]), float(p[1]))
            for p in raw_profile
            if isinstance(p, (list, tuple)) and len(p) >= 2
        ]
        if not parsed and start_ele is not None and end_ele is not None:
            parsed = [(0.0, float(start_ele)), (seg_len, float(end_ele))]
            if reverse_seg:
                parsed = [(0.0, float(end_ele)), (seg_len, float(start_ele))]
                reverse_profile = False
            else:
                reverse_profile = False
        else:
            reverse_profile = reverse_seg

        if parsed:
            span = parsed[-1][0]
            ordered_pts = (
                [(max(0.0, span - d), z) for d, z in reversed(parsed)]
                if reverse_profile
                else parsed
            )
            for pt_idx, (local_d, elev_m) in enumerate(ordered_pts):
                if idx > 0 and pt_idx == 0 and profile_fwd:
                    continue
                profile_fwd.append(
                    _RawProfileSample(
                        local_dist_m=cum_dist + local_d,
                        elev_m=elev_m,
                        slope_deg=seg_slope,
                        grade=eff_grade,
                    )
                )

        cum_dist += seg_len

    length_m = cum_dist if cum_dist > 0 else float(track.total_length_m or 0.0)
    if elev_min is None and track.elev_min_m is not None:
        elev_min = float(track.elev_min_m)
    if elev_max is None and track.elev_max_m is not None:
        elev_max = float(track.elev_max_m)
    if max_slope is None and track.max_slope_deg is not None:
        max_slope = float(track.max_slope_deg)

    baseline_grade = _RANK_GRADE.get(worst_base_rank, "Medium")
    effective_grade_from_segs = _RANK_GRADE.get(worst_eff_rank, baseline_grade)

    return _TrackGeometryData(
        track=track,
        coords_fwd=coords_fwd,
        profile_fwd=profile_fwd,
        seg_mid_dist_fwd=seg_mid_dist_fwd,
        length_m=length_m,
        elev_min_m=elev_min,
        elev_max_m=elev_max,
        elev_gain_fwd_m=elev_gain_fwd,
        elev_loss_fwd_m=elev_loss_fwd,
        max_slope_deg=max_slope,
        sustained_slope_deg=sustained_slope,
        worst_surface=worst_surface,
        is_4wd_only=is_4wd_only,
        has_perennial_crossing=has_perennial,
        unbridged_seg_crossings=unbridged_seg_crossings,
        baseline_grade=baseline_grade,
        baseline_score=round(max_base_score, 1),
        effective_grade_from_segs=effective_grade_from_segs,
        wet_risk_level=_RANK_WET.get(worst_wet_rank, "dry"),
        park_ids=park_ids,
    )


def _bounded_route_segment_ids(session: Session, track_ids: Sequence[str]) -> list[str]:
    """Check weighted route size before loading geometries, grades, or profile arrays."""
    uses_by_track = Counter(track_ids)
    profile_len = case(
        (
            func.jsonb_typeof(TrackSegment.elevation_profile_m) == "array",
            func.jsonb_array_length(TrackSegment.elevation_profile_m),
        ),
        else_=0,
    )
    rows = session.execute(
        select(
            TrackSegment.segment_id,
            TrackSegment.track_id,
            func.ST_NPoints(TrackSegment.geom).label("vertices"),
            func.greatest(2, profile_len).label("profile_samples"),
        )
        .where(TrackSegment.track_id.in_(uses_by_track), TrackSegment.is_active.is_(True))
        .limit(_MAX_ROUTE_SEGMENTS + 1)
    ).all()
    segments = coordinates = profile_samples = 0
    for row in rows:
        use_count = uses_by_track[row.track_id]
        segments += use_count
        coordinates += row.vertices * use_count
        profile_samples += row.profile_samples * use_count
    if (
        segments > _MAX_ROUTE_SEGMENTS
        or coordinates > _MAX_ROUTE_COORDINATES
        or profile_samples > _MAX_ROUTE_PROFILE_SAMPLES
    ):
        raise RouteWorkBudgetExceeded()
    return [row.segment_id for row in rows]


def plan_route_with_geometry(
    session: Session,
    track_ids: Sequence[str],
    *,
    vehicle_class: str | None = None,
    vehicle_profile: VehicleProfile | None = None,
    waypoints: Sequence[RouteWaypoint | dict[str, Any]] | None = None,
    name: str | None = None,
) -> tuple[PlannedRouteAnalysis, list[OrientedLegTrack]]:
    """Plan and analyze a chained multi-track route, returning `(analysis, oriented_leg_tracks)`."""
    if not isinstance(track_ids, (list, tuple)):
        raise ValueError("track_ids must be a list of canonical track IDs")
    cleaned_ids = [str(tid).strip() for tid in track_ids]
    if not cleaned_ids:
        raise ValueError("Route must include at least 1 track_id")
    if len(cleaned_ids) > MAX_ROUTE_TRACKS:
        raise ValueError(f"Route cannot exceed {MAX_ROUTE_TRACKS} tracks")
    if any(not tid for tid in cleaned_ids):
        raise ValueError("track_ids cannot contain blank entries")

    if name is not None and not name.strip():
        raise ValueError("Route name cannot be blank")

    norm_waypoints = _normalize_waypoints(waypoints)

    # Resolve vehicle capability context (explicit vehicle_class overrides vehicle_profile)
    resolved_cap_class: CapabilityClassLiteral | None = None
    resolved_veh_name: str | None = None
    resolved_has_snorkel: bool | None = None
    if vehicle_class is not None:
        normalized_vc = normalize_capability_class(vehicle_class)
        if normalized_vc is None:
            raise ValueError(
                "vehicle_class must be one of: AWD, Stock 4WD, Touring 4WD, Modified 4WD"
            )
        resolved_cap_class = normalized_vc
    elif vehicle_profile is not None:
        resolved_cap_class = normalize_capability_class(vehicle_profile.capability_class)
        resolved_veh_name = vehicle_profile.name
        resolved_has_snorkel = bool(vehicle_profile.has_snorkel)

    unique_ids = list(dict.fromkeys(cleaned_ids))

    # 1. Load canonical tracks and verify every requested ID exists & is active
    track_rows = session.execute(
        select(Track)
        .where(Track.track_id.in_(unique_ids), Track.is_active.is_(True))
        .options(defer(Track.bbox_geom))
    ).scalars().all()
    tracks_by_id: dict[str, Track] = {t.track_id: t for t in track_rows}
    missing = [tid for tid in unique_ids if tid not in tracks_by_id]
    if missing:
        raise TrackNotFoundError(missing)

    # 2. Enforce budgets including repeated legs before loading the full segment rows.
    segment_ids = _bounded_route_segment_ids(session, cleaned_ids)
    raw_seg_rows = session.execute(
        select(
            TrackSegment,
            func.ST_AsGeoJSON(TrackSegment.geom).label("geom_json"),
            TrackSegmentGrade,
        )
        .outerjoin(TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id)
        .where(TrackSegment.segment_id.in_(segment_ids), TrackSegment.is_active.is_(True))
        .options(
            defer(TrackSegment.geom),
            load_only(
                TrackSegmentGrade.effective_grade,
                TrackSegmentGrade.baseline_grade,
                TrackSegmentGrade.baseline_score,
                TrackSegmentGrade.rainfall_context,
                TrackSegmentGrade.wet_risk_level,
            ),
        )
        .order_by(
            TrackSegment.track_id.asc(),
            TrackSegment.segment_seq.asc().nullslast(),
            TrackSegment.segment_id.asc(),
        )
    ).all()

    segs_by_track: dict[str, list[tuple[TrackSegment, str, TrackSegmentGrade | None]]] = {
        tid: [] for tid in unique_ids
    }
    for seg, geom_json, grd in raw_seg_rows:
        if seg.track_id is not None and seg.track_id in segs_by_track:
            segs_by_track[seg.track_id].append((seg, geom_json, grd))

    track_geom_map: dict[str, _TrackGeometryData] = {
        tid: _stitch_single_track(tracks_by_id[tid], segs_by_track[tid])
        for tid in unique_ids
    }

    # 3. Bulk-load live grading summaries, official closures, and community condition reports
    live_map = get_live_summaries_for_tracks(session, unique_ids)
    closures_map = get_closures_for_track_ids(
        session, unique_ids, max_results=_MAX_ROUTE_SAFETY_ITEMS
    )
    raw_reports_map = get_reports_for_tracks(
        session, unique_ids, include_resolved=False, max_results=_MAX_ROUTE_SAFETY_ITEMS
    )
    reports_by_track: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {
        tid: (
            list(raw_reports_map.get(tid, {}).get("condition_reports") or []),
            list(raw_reports_map.get(tid, {}).get("community_gate_warnings") or []),
        )
        for tid in unique_ids
    }

    # 4. Optimize per-leg orientation (start/end jump minimization via 2-state DP)
    leg_endpoints: list[tuple[tuple[float, float], tuple[float, float]] | None] = []
    for tid in cleaned_ids:
        coords = track_geom_map[tid].coords_fwd
        if len(coords) >= 2:
            leg_endpoints.append(((coords[0][0], coords[0][1]), (coords[-1][0], coords[-1][1])))
        else:
            leg_endpoints.append(None)

    reversals, gaps_from_prev = optimize_leg_orientations(leg_endpoints)

    # 5. Load PostGIS waterway crossings for all segments of the route's tracks
    ww_rows = session.execute(
        select(
            SourceWaterway.source_feature_id,
            SourceWaterway.name,
            SourceWaterway.feature_type,
            SourceWaterway.perenniality,
            TrackSegment.track_id,
            TrackSegment.segment_id,
            TrackSegment.is_bridged,
            func.ST_X(func.ST_LineInterpolatePoint(TrackSegment.geom, 0.5)).label("seg_lon"),
            func.ST_Y(func.ST_LineInterpolatePoint(TrackSegment.geom, 0.5)).label("seg_lat"),
        )
        .join(
            TrackSegment,
            func.ST_DWithin(
                func.ST_Transform(TrackSegment.geom, 3577),
                func.ST_Transform(SourceWaterway.geom, 3577),
                1.0,
            ),
        )
        .where(
            TrackSegment.segment_id.in_(segment_ids),
            TrackSegment.is_active.is_(True),
            func.lower(SourceWaterway.feature_type) != "connector",
            func.ST_DWithin(TrackSegment.geom, SourceWaterway.geom, 0.0005),
        )
        .limit(_MAX_ROUTE_SAFETY_ITEMS + 1)
    ).all()
    track_uses = Counter(cleaned_ids)
    if sum(track_uses[row.track_id] for row in ww_rows) > _MAX_ROUTE_SAFETY_ITEMS:
        raise RouteWorkBudgetExceeded()

    ww_by_track: dict[str, list[Any]] = {tid: [] for tid in unique_ids}
    for row in ww_rows:
        if row.track_id in ww_by_track:
            ww_by_track[row.track_id].append(row)

    # 6. Stitch legs in order, computing cumulative metrics, profile, crossings, and GeoJSON
    legs: list[RouteLegSummary] = []
    leg_gaps: list[RouteLegGap] = []
    oriented_tracks: list[OrientedLegTrack] = []
    raw_profile_points: list[RouteElevationPoint] = []
    waterway_crossings: list[RouteWaterwayCrossing] = []
    geojson_features: list[dict[str, Any]] = []

    cum_dist_m = 0.0
    total_duration_min = 0.0
    total_elev_gain_m = 0.0
    total_elev_loss_m = 0.0
    route_min_elev: float | None = None
    route_max_elev: float | None = None
    route_max_slope: float | None = None
    route_sustained_slope: float | None = None
    worst_grade_rank = 0
    worst_score = 0.0
    worst_surface = "unknown"
    worst_wet_rank = 0
    worst_closure_rank = 0
    worst_closure_status = "open"
    any_4wd_only = False
    any_unbridged_perennial = False
    fallback_unbridged_count = 0
    states_set: set[str] = set()
    has_osm_source = False

    min_lon = math.inf
    min_lat = math.inf
    max_lon = -math.inf
    max_lat = -math.inf

    for idx, tid in enumerate(cleaned_ids):
        tg = track_geom_map[tid]
        trk = tg.track
        rev = reversals[idx]
        gap_m = gaps_from_prev[idx]

        live_s = live_map.get(tid, {})
        live_grade: str | None = live_s.get("live_grade")
        live_score: float | None = (
            float(live_s["live_score"]) if live_s.get("live_score") is not None else None
        )
        leg_grade = live_grade or tg.effective_grade_from_segs or tg.baseline_grade
        leg_score = round(
            live_score if live_score is not None else tg.baseline_score,
            1,
        )

        # Wet risk from live rainfall_context or segment grades
        rain_ctx = live_s.get("rainfall_context") or {}
        if isinstance(rain_ctx, dict) and rain_ctx.get("wet_risk_level"):
            leg_wet = db_to_api_wet_risk(str(rain_ctx["wet_risk_level"]))
        else:
            leg_wet = tg.wet_risk_level
        worst_wet_rank = max(
            worst_wet_rank,
            _WET_RANK.get(leg_wet, 0),
            _WET_RANK.get(tg.wet_risk_level, 0),
        )

        # Closure status & community gate count for this leg
        c_entry = closures_map.get(tid, {"closure_status": "open", "active_closures": []})
        c_status = str(c_entry.get("closure_status", "open"))
        if _CLOSURE_STATUS_RANK.get(c_status, 0) > worst_closure_rank:
            worst_closure_rank = _CLOSURE_STATUS_RANK.get(c_status, 0)
            worst_closure_status = c_status

        _, leg_gate_warnings = reports_by_track.get(tid, ([], []))

        # Speed & duration: prefer live telemetry speed, else terrain-grade heuristic
        live_speed = live_s.get("avg_speed_kmh")
        if live_speed is not None and float(live_speed) > 0.5:
            speed_kmh = float(live_speed)
        else:
            speed_kmh = GRADE_SPEED_KMH.get(leg_grade, 18.0)
        leg_duration_min = round((tg.length_m / 1000.0) / speed_kmh * 60.0, 1)
        total_duration_min += leg_duration_min

        # Orientation-aware coordinates, elevation gain/loss, profile, and crossings
        coords_oriented = list(reversed(tg.coords_fwd)) if rev else list(tg.coords_fwd)
        leg_gain = tg.elev_loss_fwd_m if rev else tg.elev_gain_fwd_m
        leg_loss = tg.elev_gain_fwd_m if rev else tg.elev_loss_fwd_m
        total_elev_gain_m += leg_gain
        total_elev_loss_m += leg_loss

        if tg.elev_min_m is not None and (route_min_elev is None or tg.elev_min_m < route_min_elev):
            route_min_elev = tg.elev_min_m
        if tg.elev_max_m is not None and (route_max_elev is None or tg.elev_max_m > route_max_elev):
            route_max_elev = tg.elev_max_m
        if tg.max_slope_deg is not None and (
            route_max_slope is None or tg.max_slope_deg > route_max_slope
        ):
            route_max_slope = tg.max_slope_deg
        if tg.sustained_slope_deg is not None and (
            route_sustained_slope is None or tg.sustained_slope_deg > route_sustained_slope
        ):
            route_sustained_slope = tg.sustained_slope_deg

        if _SURFACE_RANK.get(tg.worst_surface, 0) > _SURFACE_RANK.get(worst_surface, 0):
            worst_surface = tg.worst_surface
        any_4wd_only = any_4wd_only or tg.is_4wd_only
        any_unbridged_perennial = any_unbridged_perennial or tg.has_perennial_crossing
        fallback_unbridged_count += max(
            int(trk.waterway_crossing_count or 0), tg.unbridged_seg_crossings
        )
        has_osm_source = has_osm_source or bool(trk.has_osm_source)
        for st_part in trk.state.split("/"):
            st_clean = st_part.strip()
            if st_clean:
                states_set.add(st_clean)

        g_rank = _GRADE_RANK.get(leg_grade, 2)
        if g_rank > worst_grade_rank:
            worst_grade_rank = g_rank
        if leg_score > worst_score:
            worst_score = leg_score

        leg_start_dist = cum_dist_m
        leg_end_dist = cum_dist_m + tg.length_m

        # Stitch continuous elevation profile for this leg
        if tg.profile_fwd:
            span = tg.profile_fwd[-1].local_dist_m
            ordered_prof = (
                [
                    _RawProfileSample(
                        local_dist_m=max(0.0, span - p.local_dist_m),
                        elev_m=p.elev_m,
                        slope_deg=p.slope_deg,
                        grade=p.grade,
                    )
                    for p in reversed(tg.profile_fwd)
                ]
                if rev
                else tg.profile_fwd
            )
            for p_idx, sample in enumerate(ordered_prof):
                if idx > 0 and p_idx == 0 and raw_profile_points and gap_m <= LEG_GAP_THRESHOLD_M:
                    continue
                raw_profile_points.append(
                    RouteElevationPoint(
                        dist_m=round(leg_start_dist + sample.local_dist_m, 1),
                        elev_m=round(sample.elev_m, 2),
                        slope_deg=sample.slope_deg,
                        grade=sample.grade or leg_grade,
                        track_id=tid,
                        track_name=trk.name,
                    )
                )

        # Stitch waterway crossings with cumulative route dist_m
        seen_leg_crossings: dict[tuple[str, bool], RouteWaterwayCrossing] = {}
        for ww_row in ww_by_track.get(tid, []):
            seg_mid = tg.seg_mid_dist_fwd.get(ww_row.segment_id, 0.0)
            in_leg_dist = max(0.0, tg.length_m - seg_mid) if rev else seg_mid
            cum_crossing_dist = round(leg_start_dist + in_leg_dist, 1)
            is_perennial = str(ww_row.perenniality).strip().lower() == "perennial"
            if is_perennial and not ww_row.is_bridged:
                any_unbridged_perennial = True
            ckey = (str(ww_row.source_feature_id), bool(ww_row.is_bridged))
            if (
                ckey not in seen_leg_crossings
                or cum_crossing_dist < seen_leg_crossings[ckey].dist_m
            ):
                seen_leg_crossings[ckey] = RouteWaterwayCrossing(
                    name=ww_row.name or "Unnamed watercourse",
                    waterway_type=ww_row.feature_type,
                    perennial=is_perennial,
                    bridged=bool(ww_row.is_bridged),
                    dist_m=cum_crossing_dist,
                    track_id=tid,
                    track_name=trk.name,
                    lon=round(float(ww_row.seg_lon), 6) if ww_row.seg_lon is not None else None,
                    lat=round(float(ww_row.seg_lat), 6) if ww_row.seg_lat is not None else None,
                )
        waterway_crossings.extend(
            sorted(seen_leg_crossings.values(), key=lambda c: (c.dist_m, c.name))
        )

        # Update bbox bounds from leg coordinates
        for lon, lat, _ele in coords_oriented:
            if lon < min_lon:
                min_lon = lon
            if lon > max_lon:
                max_lon = lon
            if lat < min_lat:
                min_lat = lat
            if lat > max_lat:
                max_lat = lat

        if idx > 0 and gap_m > LEG_GAP_THRESHOLD_M:
            prev_tid = cleaned_ids[idx - 1]
            leg_gaps.append(
                RouteLegGap(
                    from_track_id=prev_tid,
                    to_track_id=tid,
                    from_track_name=tracks_by_id[prev_tid].name,
                    to_track_name=trk.name,
                    gap_m=gap_m,
                )
            )

        legs.append(
            RouteLegSummary(
                index=idx,
                track_id=tid,
                name=trk.name,
                state=trk.state,
                length_m=round(tg.length_m, 1),
                est_duration_min=leg_duration_min,
                grade=leg_grade,
                score=leg_score,
                baseline_grade=tg.baseline_grade,
                live_grade=live_grade,
                closure_status=c_status,
                gate_warning_count=len(leg_gate_warnings),
                reversed=rev,
                gap_from_prev_m=gap_m,
                start_dist_m=round(leg_start_dist, 1),
                end_dist_m=round(leg_end_dist, 1),
            )
        )

        oriented_tracks.append(
            OrientedLegTrack(
                index=idx,
                track_id=tid,
                name=trk.name,
                state=trk.state,
                grade=leg_grade,
                score=leg_score,
                length_m=round(tg.length_m, 1),
                closure_status=c_status,
                reversed=rev,
                gap_from_prev_m=gap_m,
                coords_lonlat_ele=coords_oriented,
            )
        )

        if len(coords_oriented) >= 2:
            geojson_features.append(
                {
                    "type": "Feature",
                    "id": f"leg-{idx}-{tid}",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [round(lon, 6), round(lat, 6)] for lon, lat, _ in coords_oriented
                        ],
                    },
                    "properties": {
                        "feature_kind": "route_leg",
                        "leg_index": idx,
                        "track_id": tid,
                        "name": trk.name,
                        "state": trk.state,
                        "grade": leg_grade,
                        "score": leg_score,
                        "closure_status": c_status,
                        "gate_warning_count": len(leg_gate_warnings),
                        "reversed": rev,
                        "gap_from_prev_m": gap_m,
                        "length_m": round(tg.length_m, 1),
                    },
                }
            )

        cum_dist_m = leg_end_dist

    # Include custom waypoints in bbox if present
    for wp in norm_waypoints:
        min_lon = min(min_lon, wp.lon)
        max_lon = max(max_lon, wp.lon)
        min_lat = min(min_lat, wp.lat)
        max_lat = max(max_lat, wp.lat)

    # 7. Deduplicate official closures across all legs & enrich with optional centroid coords
    dedup_closures: dict[int, dict[str, Any]] = {}
    for tid in unique_ids:
        entry = closures_map.get(tid, {})
        for c in entry.get("active_closures") or []:
            cid = int(c["closure_id"])
            if cid not in dedup_closures:
                c_copy = dict(c)
                c_copy["affected_track_ids"] = [tid]
                dedup_closures[cid] = c_copy
            else:
                if tid not in dedup_closures[cid]["affected_track_ids"]:
                    dedup_closures[cid]["affected_track_ids"].append(tid)

    if dedup_closures:
        closure_coord_rows = session.execute(
            select(
                ClosureAlert.id,
                func.ST_X(func.ST_PointOnSurface(ClosureAlert.geom)).label("lon"),
                func.ST_Y(func.ST_PointOnSurface(ClosureAlert.geom)).label("lat"),
            ).where(
                ClosureAlert.id.in_(list(dedup_closures.keys())),
                ClosureAlert.geom.is_not(None),
            )
        ).all()
        for cid, clon, clat in closure_coord_rows:
            if clon is not None and clat is not None and int(cid) in dedup_closures:
                dedup_closures[int(cid)]["lon"] = round(float(clon), 6)
                dedup_closures[int(cid)]["lat"] = round(float(clat), 6)

    closures_list = list(dedup_closures.values())

    # 8. Deduplicate community condition reports and gate warnings across all legs
    dedup_reports: dict[str, dict[str, Any]] = {}
    dedup_gate_warnings: dict[str, dict[str, Any]] = {}
    for tid in unique_ids:
        reps, gates = reports_by_track.get(tid, ([], []))
        for rep in reps:
            dedup_reports.setdefault(str(rep["id"]), rep)
        for gw in gates:
            dedup_gate_warnings.setdefault(str(gw["id"]), gw)

    condition_reports = sorted(
        dedup_reports.values(),
        key=lambda r: str(r.get("observed_at") or r.get("created_at") or ""),
        reverse=True,
    )
    community_gate_warnings = sorted(
        dedup_gate_warnings.values(),
        key=lambda r: str(r.get("observed_at") or r.get("created_at") or ""),
        reverse=True,
    )

    # 9. Intersecting CAPAD parks & nearby POIs within 1,500 m of combined route segments
    route_geometry = (
        select(func.ST_Collect(TrackSegment.geom).label("geom"))
        .where(TrackSegment.segment_id.in_(segment_ids), TrackSegment.is_active.is_(True))
        .subquery("route_geometry")
    )

    all_park_ids: set[int] = set()
    for tg in track_geom_map.values():
        all_park_ids.update(tg.park_ids)
    if len(all_park_ids) > _MAX_ROUTE_SAFETY_ITEMS:
        raise RouteWorkBudgetExceeded()

    parks_by_id: dict[int, CAPADPark] = {}
    if all_park_ids:
        park_objs = (
            session.execute(
                select(CAPADPark)
                .where(CAPADPark.id.in_(all_park_ids))
                .options(defer(CAPADPark.geom))
            )
            .scalars()
            .all()
        )
        for pk in park_objs:
            parks_by_id[pk.id] = pk

    intersecting_parks = session.execute(
        select(CAPADPark)
        .join(route_geometry, func.ST_Intersects(CAPADPark.geom, route_geometry.c.geom))
        .options(defer(CAPADPark.geom))
        .order_by(CAPADPark.name.asc())
        .limit(_MAX_ROUTE_SAFETY_ITEMS + 1)
    ).scalars().all()
    for pk in intersecting_parks:
        parks_by_id.setdefault(pk.id, pk)
    if len(parks_by_id) > _MAX_ROUTE_SAFETY_ITEMS:
        raise RouteWorkBudgetExceeded()

    capad_parks = [
        ParkSummary(
            id=pk.id,
            capad_external_id=pk.capad_external_id,
            name=pk.name,
            park_type=pk.park_type,
            type_abbr=pk.type_abbr,
            iucn=pk.iucn,
            state=pk.state,
            area_ha=pk.area_ha,
        )
        for pk in sorted(parks_by_id.values(), key=lambda p: p.name)
    ]

    dist_expr = func.ST_Distance(
        func.ST_Transform(POI.geom, 3577),
        func.ST_Transform(route_geometry.c.geom, 3577),
    ).label("distance_m")
    poi_rows = session.execute(
        select(
            POI,
            func.ST_X(POI.geom).label("lon"),
            func.ST_Y(POI.geom).label("lat"),
            CAPADPark.name.label("park_name"),
            dist_expr,
        )
        .join(
            route_geometry,
            func.ST_DWithin(
                func.ST_Transform(POI.geom, 3577),
                func.ST_Transform(route_geometry.c.geom, 3577),
                _NEARBY_POI_RADIUS_M,
            ),
        )
        .outerjoin(CAPADPark, POI.capad_park_id == CAPADPark.id)
        .where(func.ST_DWithin(POI.geom, route_geometry.c.geom, 0.025))
        .options(defer(POI.geom))
        .order_by(dist_expr.asc(), POI.name.asc())
        .limit(_MAX_NEARBY_POIS)
    ).all()

    seen_poi_keys: set[str] = set()
    nearby_pois: list[PoiResponse] = []
    for poi, plon, plat, park_name, dist_m in poi_rows:
        if poi.poi_key in seen_poi_keys:
            continue
        seen_poi_keys.add(poi.poi_key)
        nearby_pois.append(
            PoiResponse(
                id=poi.id,
                poi_key=poi.poi_key,
                name=poi.name,
                poi_type=poi.poi_type,
                state=poi.state,
                elev_m=poi.elev_m,
                access_note=poi.access_note,
                nearest_segment_id=poi.nearest_segment_id,
                capad_park_name=park_name,
                primary_source=poi.primary_source,
                source_license=poi.source_license,
                lon=round(float(plon), 6),
                lat=round(float(plat), 6),
                distance_m=round(float(dist_m), 1),
            )
        )

    # 10. Safety summary & consolidated warnings
    route_wet_risk: ApiWetRiskLiteral = _RANK_WET.get(worst_wet_rank, "dry")
    unbridged_from_ww = sum(1 for c in waterway_crossings if not c.bridged)
    unbridged_crossings_count = max(unbridged_from_ww, fallback_unbridged_count)

    has_blocking_closure = any(
        leg.closure_status in ("closed", "seasonal") for leg in legs
    ) or any(
        c.get("closure_type") in ("closed", "seasonal")
        or (c.get("closure_type") == "incident" and c.get("severity") == "critical")
        for c in closures_list
    )

    safety_warnings: list[str] = []
    if has_blocking_closure:
        blocking_titles = [
            str(c.get("title") or "Official Closure")
            for c in closures_list
            if c.get("closure_type") in ("closed", "seasonal")
            or (c.get("closure_type") == "incident" and c.get("severity") == "critical")
        ]
        summary_titles = "; ".join(blocking_titles[:3])
        safety_warnings.append(
            f"Blocking closure on route ({len(blocking_titles)}): {summary_titles}"
            if blocking_titles
            else "One or more legs have an active blocking closure — do not enter."
        )
    elif closures_list:
        safety_warnings.append(
            f"{len(closures_list)} official access/advisory alert(s) affect this route."
        )

    if community_gate_warnings:
        safety_warnings.append(
            f"{len(community_gate_warnings)} community locked-gate / private-property "
            "warning(s) reported along route."
        )

    if leg_gaps:
        max_gap = max(g.gap_m for g in leg_gaps)
        safety_warnings.append(
            f"{len(leg_gaps)} non-contiguous leg gap(s) detected "
            f"(largest {max_gap / 1000.0:.2f} km)."
        )

    if route_wet_risk in ("wet", "heavy_rain"):
        label = "Heavy rain" if route_wet_risk == "heavy_rain" else "Wet conditions"
        safety_warnings.append(
            f"{label} over the past 72 h — expect slick clay/mud and elevated creek crossings."
        )

    if unbridged_crossings_count > 0:
        if any_unbridged_perennial:
            safety_warnings.append(
                f"{unbridged_crossings_count} unbridged waterway crossing(s) including perennial "
                "river/creek — walk and check depth before crossing."
            )
        else:
            safety_warnings.append(
                f"{unbridged_crossings_count} unbridged creek/gully crossing(s) along route."
            )

    impassable_reports = [
        r for r in condition_reports if str(r.get("severity") or "").lower() == "impassable"
    ]
    if impassable_reports:
        safety_warnings.append(
            f"{len(impassable_reports)} active community report(s) marked impassable along route."
        )

    safety_summary = RouteSafetySummary(
        has_blocking_closure=has_blocking_closure,
        closure_count=len(closures_list),
        gate_warning_count=len(community_gate_warnings),
        active_report_count=len(condition_reports),
        unbridged_crossings=unbridged_crossings_count,
        wet_risk_level=route_wet_risk,
        warnings=safety_warnings,
    )

    # 11. Worst grade & personalized rig suitability
    worst_grade = _RANK_GRADE.get(worst_grade_rank, "Medium")
    worst_score_rounded = round(float(worst_score), 1)

    suitability: TrackSuitability | None = None
    if resolved_cap_class is not None:
        suitability = evaluate_track_suitability(
            grade=worst_grade,  # type: ignore[arg-type]
            capability_class=resolved_cap_class,
            vehicle_name=resolved_veh_name,
            max_slope_deg=route_max_slope,
            waterway_crossing_count=unbridged_crossings_count,
            has_perennial_crossing=any_unbridged_perennial,
            is_4wd_only=any_4wd_only,
            has_snorkel=resolved_has_snorkel,
            wet_risk_level=_wet_for_suitability(route_wet_risk),
            surface_class=worst_surface,
            closure_status=worst_closure_status,
        )
        if community_gate_warnings and not any(
            "gate" in w.lower() for w in suitability.warnings
        ):
            suitability = suitability.model_copy(
                update={
                    "warnings": [
                        *suitability.warnings,
                        f"{len(community_gate_warnings)} community locked-gate / private-property "
                        "warning(s) reported on this route.",
                    ]
                }
            )

    # 12. Downsample elevation profile cleanly to <= 600 points
    elevation_profile = downsample_elevation_profile(
        raw_profile_points, max_points=MAX_PROFILE_POINTS
    )

    # 13. Derive display name, bbox, start/end coordinates, and GridForge CLI
    if name is not None and name.strip():
        route_name = name.strip()
    elif len(legs) == 1:
        route_name = legs[0].name
    else:
        route_name = f"{legs[0].name} → {legs[-1].name}"

    if math.isfinite(min_lon) and math.isfinite(min_lat):
        bbox = [
            round(min_lon, 6),
            round(min_lat, 6),
            round(max_lon, 6),
            round(max_lat, 6),
        ]
    else:
        bbox = [0.0, 0.0, 0.0, 0.0]

    non_empty_tracks = [ot for ot in oriented_tracks if ot.coords_lonlat_ele]
    start_coord = (
        [
            round(non_empty_tracks[0].coords_lonlat_ele[0][0], 6),
            round(non_empty_tracks[0].coords_lonlat_ele[0][1], 6),
        ]
        if non_empty_tracks
        else None
    )
    end_coord = (
        [
            round(non_empty_tracks[-1].coords_lonlat_ele[-1][0], 6),
            round(non_empty_tracks[-1].coords_lonlat_ele[-1][1], 6),
        ]
        if non_empty_tracks
        else None
    )

    waterway_crossings.sort(key=lambda c: (c.dist_m, c.name))

    analysis = PlannedRouteAnalysis(
        name=route_name,
        track_ids=cleaned_ids,
        states=sorted(states_set),
        total_length_m=round(cum_dist_m, 1),
        est_duration_min=round(total_duration_min, 1),
        min_elev_m=round(route_min_elev, 1) if route_min_elev is not None else None,
        max_elev_m=round(route_max_elev, 1) if route_max_elev is not None else None,
        elev_gain_m=round(total_elev_gain_m, 1),
        elev_loss_m=round(total_elev_loss_m, 1),
        max_slope_deg=round(route_max_slope, 1) if route_max_slope is not None else None,
        sustained_slope_deg=(
            round(route_sustained_slope, 1) if route_sustained_slope is not None else None
        ),
        worst_grade=worst_grade,
        worst_score=worst_score_rounded,
        effective_grade=worst_grade,
        suitability=suitability,
        safety_summary=safety_summary,
        closures=closures_list,
        community_gate_warnings=community_gate_warnings,
        condition_reports=condition_reports,
        waterway_crossings=waterway_crossings,
        nearby_pois=nearby_pois,
        capad_parks=capad_parks,
        elevation_profile=elevation_profile,
        legs=legs,
        leg_gaps=leg_gaps,
        waypoints=norm_waypoints,
        bbox=bbox,
        start_coord=start_coord,
        end_coord=end_coord,
        geometry={
            "type": "FeatureCollection",
            "features": geojson_features,
        },
        gridforge_cli=build_gridforge_cli(route_name),
        has_osm_source=has_osm_source,
        attributions=list(ROUTE_ATTRIBUTIONS),
    )

    if len(analysis.model_dump_json().encode("utf-8")) > _MAX_ROUTE_ANALYSIS_BYTES:
        raise RouteWorkBudgetExceeded()
    return analysis, oriented_tracks


def summarize_saved_routes(
    session: Session,
    routes: Sequence[Any],
) -> dict[Any, dict[str, Any]]:
    """Compute lightweight live summary metrics for many saved PlannedRoutes in batched queries.

    Avoids running full geometry stitching, waterway spatial joins, elevation profile
    resampling, and POI queries once per saved route on list endpoints.
    """
    if not routes:
        return {}

    all_tids: list[str] = []
    for route in routes:
        for tid in route.track_ids or []:
            cleaned = str(tid).strip()
            if cleaned:
                all_tids.append(cleaned)
    unique_ids = list(dict.fromkeys(all_tids))
    if not unique_ids:
        return {}

    active_tracks = {
        t.track_id: t
        for t in session.execute(
            select(Track).where(Track.track_id.in_(unique_ids), Track.is_active.is_(True))
        )
        .scalars()
        .all()
    }
    active_ids = list(active_tracks.keys())
    if not active_ids:
        return {}

    eff_rank_expr = case(
        (TrackSegmentGrade.effective_grade == "Easy", 1),
        (TrackSegmentGrade.effective_grade == "Medium", 2),
        (TrackSegmentGrade.effective_grade == "Hard", 3),
        (TrackSegmentGrade.effective_grade == "Extreme", 4),
        else_=0,
    )
    base_rank_expr = case(
        (TrackSegmentGrade.baseline_grade == "Easy", 1),
        (TrackSegmentGrade.baseline_grade == "Medium", 2),
        (TrackSegmentGrade.baseline_grade == "Hard", 3),
        (TrackSegmentGrade.baseline_grade == "Extreme", 4),
        else_=0,
    )
    wet_rank_expr = case(
        (TrackSegmentGrade.wet_risk_level == "damp", 1),
        (TrackSegmentGrade.wet_risk_level == "wet", 2),
        (TrackSegmentGrade.wet_risk_level == "saturated", 3),
        else_=0,
    )
    seg_stats_by_track: dict[str, Any] = {}
    for row in session.execute(
        select(
            TrackSegment.track_id.label("track_id"),
            func.coalesce(func.sum(TrackSegment.length_m), 0.0).label("seg_length_m"),
            func.coalesce(func.max(eff_rank_expr), 0).label("eff_rank"),
            func.coalesce(func.max(base_rank_expr), 0).label("base_rank"),
            func.coalesce(func.max(wet_rank_expr), 0).label("wet_rank"),
        )
        .outerjoin(TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id)
        .where(TrackSegment.is_active.is_(True), TrackSegment.track_id.in_(active_ids))
        .group_by(TrackSegment.track_id)
    ).all():
        if row.track_id is not None:
            seg_stats_by_track[row.track_id] = row

    live_map = get_live_summaries_for_tracks(session, active_ids)
    closures_map = get_closures_for_track_ids(session, active_ids)
    reports_map = get_reports_for_tracks(session, active_ids, include_resolved=False)

    wet_by_rank: dict[int, ApiWetRiskLiteral] = {
        0: "dry",
        1: "damp",
        2: "wet",
        3: "heavy_rain",
    }

    summaries: dict[Any, dict[str, Any]] = {}
    for route in routes:
        tids = [str(tid).strip() for tid in (route.track_ids or []) if str(tid).strip()]
        if not tids or any(tid not in active_tracks for tid in tids):
            # Missing/retired track — caller falls back to basic metadata
            continue

        total_length_m = 0.0
        total_duration_min = 0.0
        worst_grade_rank = 0
        worst_wet_rank = 0
        seen_closure_ids: set[Any] = set()
        has_blocking_closure = False
        seen_gate_ids: set[Any] = set()

        for tid in tids:
            trk = active_tracks[tid]
            s_row = seg_stats_by_track.get(tid)
            seg_len = float(s_row.seg_length_m) if s_row and s_row.seg_length_m else 0.0
            leg_length_m = seg_len if seg_len > 0.0 else float(trk.total_length_m or 0.0)
            total_length_m += leg_length_m

            live_s = live_map.get(tid, {})
            live_grade = live_s.get("live_grade")
            eff_from_segs = (
                _RANK_GRADE.get(int(s_row.eff_rank)) if s_row and s_row.eff_rank else None
            )
            base_from_segs = (
                _RANK_GRADE.get(int(s_row.base_rank)) if s_row and s_row.base_rank else "Medium"
            )
            leg_grade = live_grade or eff_from_segs or base_from_segs
            worst_grade_rank = max(worst_grade_rank, _GRADE_RANK.get(leg_grade, 2))

            rain_ctx = live_s.get("rainfall_context") or {}
            seg_wet = wet_by_rank.get(int(s_row.wet_rank) if s_row else 0, "dry")
            if isinstance(rain_ctx, dict) and rain_ctx.get("wet_risk_level"):
                leg_wet = db_to_api_wet_risk(str(rain_ctx["wet_risk_level"]))
            else:
                leg_wet = seg_wet
            worst_wet_rank = max(
                worst_wet_rank,
                _WET_RANK.get(leg_wet, 0),
                _WET_RANK.get(seg_wet, 0),
            )

            live_speed = live_s.get("avg_speed_kmh")
            if live_speed is not None and float(live_speed) > 0.5:
                speed_kmh = float(live_speed)
            else:
                speed_kmh = GRADE_SPEED_KMH.get(leg_grade, 18.0)
            total_duration_min += round((leg_length_m / 1000.0) / speed_kmh * 60.0, 1)

            c_entry = closures_map.get(tid, {"closure_status": "open", "active_closures": []})
            c_status = str(c_entry.get("closure_status", "open"))
            if c_status in ("closed", "seasonal"):
                has_blocking_closure = True
            for cl in c_entry.get("active_closures") or []:
                cl_id = cl.get("id") if isinstance(cl, dict) else getattr(cl, "id", None)
                seen_closure_ids.add(cl_id if cl_id is not None else id(cl))

            for gw in reports_map.get(tid, {}).get("community_gate_warnings") or []:
                gw_id = gw.get("id") if isinstance(gw, dict) else id(gw)
                seen_gate_ids.add(gw_id)

        worst_grade = _RANK_GRADE.get(worst_grade_rank, "Medium")
        summaries[route.id] = {
            "leg_count": len(tids),
            "total_length_m": round(total_length_m, 1),
            "est_duration_min": round(total_duration_min, 1),
            "worst_grade": worst_grade,
            "effective_grade": worst_grade,
            "has_blocking_closure": has_blocking_closure,
            "closure_count": len(seen_closure_ids),
            "gate_warning_count": len(seen_gate_ids),
            "wet_risk_level": _RANK_WET.get(worst_wet_rank, "dry"),
            "gridforge_cli": build_gridforge_cli(route.name),
        }

    return summaries


def plan_route(
    session: Session,
    track_ids: Sequence[str],
    *,
    vehicle_class: str | None = None,
    vehicle_profile: VehicleProfile | None = None,
    waypoints: Sequence[RouteWaypoint | dict[str, Any]] | None = None,
    name: str | None = None,
) -> PlannedRouteAnalysis:
    """Plan and analyze a chained multi-track route (public engine entrypoint)."""
    analysis, _ = plan_route_with_geometry(
        session,
        track_ids,
        vehicle_class=vehicle_class,
        vehicle_profile=vehicle_profile,
        waypoints=waypoints,
        name=name,
    )
    return analysis
