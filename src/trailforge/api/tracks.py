"""Read-only REST API for canonical tracks, graded segments, POIs, CAPAD parks, and search."""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import Counter, OrderedDict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import UnboundExecutionError
from sqlalchemy.orm import Session

from trailforge.api.geocoder import geocode_au_places
from trailforge.auth.dependencies import get_optional_current_user
from trailforge.config import get_settings
from trailforge.db.models import (
    POI,
    CAPADPark,
    SourceWaterway,
    Track,
    TrackSegment,
    TrackSegmentGrade,
    User,
    UserFavorite,
    VehicleProfile,
)
from trailforge.db.session import get_db
from trailforge.feeds.queries import get_closures_for_track_ids
from trailforge.grading.baseline import SegmentGradeInput, compute_baseline_grade
from trailforge.grading.live import get_live_summaries_for_tracks, get_track_live_summary
from trailforge.reports.queries import get_reports_for_track, get_reports_for_tracks
from trailforge.vehicles.classifier import (
    CapabilityClassLiteral,
    TrackSuitability,
    evaluate_track_suitability,
    normalize_capability_class,
)

router = APIRouter(prefix="/api", tags=["tracks"])

def _escape_like(value: str) -> str:
    """Escape SQL LIKE/ILIKE metacharacters (`\\`, `%`, `_`) for literal substring matching."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_GRADE_RANK = {"Easy": 1, "Medium": 2, "Hard": 3, "Extreme": 4}
_RANK_GRADE = {v: k for k, v in _GRADE_RANK.items()}
_SURFACE_RANK = {
    "unknown": 0,
    "sealed": 1,
    "gravel_compacted": 2,
    "dirt_unsealed": 3,
    "rough_rock_mud_sand": 4,
}
_TRACKTYPE_RANK = {"grade1": 1, "grade2": 2, "grade3": 3, "grade4": 4, "grade5": 5}
_ACCESS_STRICTNESS = {
    "public": 0,
    "seasonal": 1,
    "permit": 2,
    "private": 3,
    "restricted": 4,
    "closed": 5,
}
_NEARBY_POI_RADIUS_M = 1500.0
_SEARCH_CACHE_MAX_ENTRIES = 256
_SEARCH_CACHE_TTL_SECONDS = 30.0
_SEARCH_POI_SUBQUERY_CAP = 5
_SEARCH_PARK_SUBQUERY_CAP = 4
_SEARCH_KIND_ORDER = {"track": 0, "park": 1, "poi": 2, "place": 3}

_search_cache_lock = threading.Lock()
_search_cache: OrderedDict[tuple[int, str, int, bool], tuple[float, Any]] = OrderedDict()


def reset_search_cache() -> None:
    """Clear the bounded in-process ``/api/search`` LRU + TTL cache."""
    with _search_cache_lock:
        _search_cache.clear()


def _db_cache_scope(db: Session) -> int:
    """Derive a stable per-engine cache scope so isolated test engines never collide."""
    get_bind = getattr(db, "get_bind", None)
    if callable(get_bind):
        try:
            bind = get_bind()
        except (UnboundExecutionError, AttributeError, TypeError):
            return id(db)
        if bind is not None:
            engine = getattr(bind, "engine", bind)
            return id(engine)
    return id(db)


def _search_cache_get(key: tuple[int, str, int, bool]) -> SearchResponse | None:
    now = time.monotonic()
    with _search_cache_lock:
        entry = _search_cache.get(key)
        if entry is None:
            return None
        cached_at, payload = entry
        if now - cached_at > _SEARCH_CACHE_TTL_SECONDS:
            _search_cache.pop(key, None)
            return None
        _search_cache.move_to_end(key)
        return payload.model_copy(deep=True)


def _search_cache_put(key: tuple[int, str, int, bool], response: SearchResponse) -> None:
    now = time.monotonic()
    snapshot = response.model_copy(deep=True)
    with _search_cache_lock:
        _search_cache[key] = (now, snapshot)
        _search_cache.move_to_end(key)
        while len(_search_cache) > _SEARCH_CACHE_MAX_ENTRIES:
            _search_cache.popitem(last=False)


def _bbox_midpoint(
    xmin: float | None, ymin: float | None, xmax: float | None, ymax: float | None
) -> tuple[float | None, float | None]:
    if xmin is None or ymin is None or xmax is None or ymax is None:
        return (None, None)
    return (
        round((float(xmin) + float(xmax)) / 2.0, 6),
        round((float(ymin) + float(ymax)) / 2.0, 6),
    )


def _search_match_tier(name: str, term_lower: str) -> int:
    low = name.strip().lower()
    if low == term_lower:
        return 0
    if low.startswith(term_lower):
        return 1
    if term_lower in low:
        return 2
    return 3


def _merge_search_results(
    track_items: list[SearchResultItem],
    poi_items: list[SearchResultItem],
    park_items: list[SearchResultItem],
    *,
    term: str,
    limit: int,
) -> list[SearchResultItem]:
    """Budget slots so tracks lead while matching parks & POIs still surface cleanly."""
    if limit <= 0:
        return []

    term_lower = term.lower()
    indexed_tracks = list(enumerate(track_items))
    indexed_parks = list(enumerate(park_items))
    indexed_pois = list(enumerate(poi_items))

    selected: list[tuple[int, int, int, SearchResultItem]] = []
    selected_keys: set[tuple[str, str]] = set()

    def _try_add(item: SearchResultItem, orig_idx: int) -> bool:
        if len(selected) >= limit:
            return False
        key = (item.kind, item.id)
        if key in selected_keys:
            return False
        selected_keys.add(key)
        tier = _search_match_tier(item.name, term_lower)
        kind_ord = _SEARCH_KIND_ORDER.get(item.kind, 9)
        selected.append((tier, kind_ord, orig_idx, item))
        return True

    # 1. Always promote exact name matches across tracks, parks, and POIs first.
    for bucket in (indexed_tracks, indexed_parks, indexed_pois):
        for idx, item in bucket:
            if _search_match_tier(item.name, term_lower) == 0:
                _try_add(item, idx)

    if len(selected) < limit:
        remaining_budget = limit - len(selected)
        rem_tracks = [
            (idx, item)
            for idx, item in indexed_tracks
            if (item.kind, item.id) not in selected_keys
        ]
        rem_parks = [
            (idx, item)
            for idx, item in indexed_parks
            if (item.kind, item.id) not in selected_keys
        ]
        rem_pois = [
            (idx, item)
            for idx, item in indexed_pois
            if (item.kind, item.id) not in selected_keys
        ]

        if remaining_budget >= 3:
            park_reserve = min(len(rem_parks), min(2, max(1, remaining_budget // 8)))
            poi_reserve = min(len(rem_pois), min(3, max(1, remaining_budget // 6)))
            track_target = max(1, remaining_budget - park_reserve - poi_reserve)
            for idx, item in rem_tracks[:track_target]:
                _try_add(item, idx)
            for idx, item in rem_parks[:park_reserve]:
                _try_add(item, idx)
            for idx, item in rem_pois[:poi_reserve]:
                _try_add(item, idx)
        else:
            # Small limit (<3): still let prefix matches win over substring matches
            for bucket in (rem_tracks, rem_parks, rem_pois):
                for idx, item in bucket:
                    if _search_match_tier(item.name, term_lower) == 1:
                        _try_add(item, idx)

        # 3. Backfill any remaining slots up to limit: tracks -> parks -> POIs.
        for bucket in (rem_tracks, rem_parks, rem_pois):
            for idx, item in bucket:
                if len(selected) >= limit:
                    break
                _try_add(item, idx)

    selected.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    return [entry[3] for entry in selected[:limit]]


def _parse_bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    if not bbox:
        return None
    parts = [p.strip() for p in bbox.split(",")]
    if len(parts) != 4:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="bbox must be 'min_lon,min_lat,max_lon,max_lat'",
        )
    try:
        min_lon, min_lat, max_lon, max_lat = (float(p) for p in parts)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="bbox coordinates must be valid floats",
        ) from exc
    if not all(math.isfinite(v) for v in (min_lon, min_lat, max_lon, max_lat)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="bbox coordinates must be finite numbers",
        )
    if not (-180.0 <= min_lon < max_lon <= 180.0 and -90.0 <= min_lat < max_lat <= 90.0):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "bbox must satisfy -180 <= min_lon < max_lon <= 180 "
                "and -90 <= min_lat < max_lat <= 90"
            ),
        )
    return (min_lon, min_lat, max_lon, max_lat)


def _bbox_list(
    xmin: float | None, ymin: float | None, xmax: float | None, ymax: float | None
) -> list[float] | None:
    if xmin is None or ymin is None or xmax is None or ymax is None:
        return None
    return [
        round(float(xmin), 6),
        round(float(ymin), 6),
        round(float(xmax), 6),
        round(float(ymax), 6),
    ]


class ParkSummary(BaseModel):
    id: int
    capad_external_id: str
    name: str
    park_type: str
    type_abbr: str | None = None
    iucn: str | None = None
    state: str
    area_ha: float | None = None


class SegmentGradePayload(BaseModel):
    baseline_grade: str
    baseline_score: float
    baseline_breakdown: dict[str, Any]
    live_grade: str | None = None
    live_score: float | None = None
    live_breakdown: dict[str, Any] | None = None
    effective_grade: str
    confidence_level: str
    confidence_score: float
    sample_count: int
    wet_risk_level: str
    rainfall_context: dict[str, Any] | None = None


class ElevationProfilePoint(BaseModel):
    dist_m: float
    elev_m: float
    slope_deg: float
    segment_id: str
    grade: str | None = None


class WaterwayCrossingItem(BaseModel):
    name: str
    waterway_type: str
    perennial: bool
    bridged: bool
    dist_m: float


class SegmentDetailResponse(BaseModel):
    segment_id: str
    track_id: str | None = None
    segment_seq: int | None = None
    name: str | None = None
    state: str
    trail_class: str
    is_vehicular: bool
    surface_class: str
    tracktype: str | None = None
    is_4wd_only: bool
    is_4wd_recommended: bool
    access_status: str
    has_gate_barrier: bool
    is_bridged: bool
    length_m: float
    bearing_deg: float
    elev_min_m: float | None = None
    elev_max_m: float | None = None
    elev_start_m: float | None = None
    elev_end_m: float | None = None
    elev_gain_m: float
    elev_loss_m: float
    max_slope_deg: float | None = None
    sustained_slope_deg: float | None = None
    is_steep_highlight: bool
    is_above_snowline: bool
    elevation_profile_m: list[Any] = Field(default_factory=list)
    dem_tier: str | None = None
    waterway_crossing_count: int
    has_perennial_crossing: bool
    primary_source: str
    has_osm_source: bool
    source_refs: list[Any] = Field(default_factory=list)
    is_active: bool
    superseded_by_segment_id: str | None = None
    capad_park: ParkSummary | None = None
    grade: SegmentGradePayload | None = None
    geometry: dict[str, Any]


class TrackSummaryResponse(BaseModel):
    track_id: str
    name: str
    alt_names: list[str] = Field(default_factory=list)
    state: str
    primary_source: str
    has_osm_source: bool
    trail_class: str
    is_vehicular: bool
    total_length_m: float
    elev_min_m: float | None = None
    elev_max_m: float | None = None
    elev_gain_m: float
    max_slope_deg: float | None = None
    waterway_crossing_count: int
    primary_park_name: str | None = None
    effective_grade: str | None = None
    grade: str | None = None
    score: float | None = None
    live_grade: str | None = None
    live_score: float | None = None
    confidence_level: str = "none"
    sample_count: int = 0
    last_driven_at: str | None = None
    avg_speed_kmh: float | None = None
    est_duration_min: float | None = None
    bbox: list[float] | None = None
    segment_count: int = 0
    is_active: bool
    closure_status: str = "open"
    active_closures: list[dict[str, Any]] = Field(default_factory=list)
    suitability: TrackSuitability | None = None
    is_favorite: bool = False


class PoiResponse(BaseModel):
    id: int
    poi_key: str
    name: str
    poi_type: str
    state: str
    elev_m: float | None = None
    access_note: str | None = None
    nearest_segment_id: str | None = None
    capad_park_name: str | None = None
    primary_source: str
    source_license: str
    lon: float
    lat: float
    distance_m: float | None = None


class TrackDetailResponse(TrackSummaryResponse):
    """Full Track Inspector payload per Slice 03 contract."""

    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    min_elev_m: float | None = None
    max_elev_m: float | None = None
    elev_loss_m: float = 0.0
    sustained_slope_deg: float | None = None
    is_4wd_only: bool = False
    has_gate_barrier: bool = False
    access_status: str = "public"
    baseline_breakdown: dict[str, Any] | None = None
    vehicle_class_split: dict[str, Any] = Field(default_factory=dict)
    live_breakdown: dict[str, Any] | None = None
    rainfall_context: dict[str, Any] | None = None
    obstacle_hotspots: list[dict[str, Any]] = Field(default_factory=list)
    elevation_profile: list[ElevationProfilePoint] = Field(default_factory=list)
    waterway_crossings: list[WaterwayCrossingItem] = Field(default_factory=list)
    primary_park: ParkSummary | None = None
    capad_parks: list[ParkSummary] = Field(default_factory=list)
    segments: list[SegmentDetailResponse] = Field(default_factory=list)
    nearby_pois: list[PoiResponse] = Field(default_factory=list)
    condition_reports: list[dict[str, Any]] = Field(default_factory=list)
    community_gate_warnings: list[dict[str, Any]] = Field(default_factory=list)


class SearchResultItem(BaseModel):
    kind: str
    id: str
    name: str
    state: str
    subtitle: str | None = None
    category: str | None = None
    grade: str | None = None
    lon: float | None = None
    lat: float | None = None
    bbox: list[float] | None = None


class SearchResponse(BaseModel):
    query: str
    total: int
    results: list[SearchResultItem]


def _park_to_summary(park: CAPADPark | None) -> ParkSummary | None:
    if park is None:
        return None
    return ParkSummary(
        id=park.id,
        capad_external_id=park.capad_external_id,
        name=park.name,
        park_type=park.park_type,
        type_abbr=park.type_abbr,
        iucn=park.iucn,
        state=park.state,
        area_ha=park.area_ha,
    )


def _grade_to_payload(grade: TrackSegmentGrade | None) -> SegmentGradePayload | None:
    if grade is None:
        return None
    return SegmentGradePayload(
        baseline_grade=grade.baseline_grade,
        baseline_score=grade.baseline_score,
        baseline_breakdown=grade.baseline_breakdown,
        live_grade=grade.live_grade,
        live_score=grade.live_score,
        live_breakdown=grade.live_breakdown,
        effective_grade=grade.effective_grade,
        confidence_level=grade.confidence_level,
        confidence_score=grade.confidence_score,
        sample_count=grade.sample_count,
        wet_risk_level=grade.wet_risk_level,
        rainfall_context=grade.rainfall_context,
    )


def _segment_to_detail(
    seg: TrackSegment,
    geom_json: str,
    grade: TrackSegmentGrade | None,
    park: CAPADPark | None,
) -> SegmentDetailResponse:
    return SegmentDetailResponse(
        segment_id=seg.segment_id,
        track_id=seg.track_id,
        segment_seq=seg.segment_seq,
        name=seg.name,
        state=seg.state,
        trail_class=seg.trail_class,
        is_vehicular=seg.is_vehicular,
        surface_class=seg.surface_class,
        tracktype=seg.tracktype,
        is_4wd_only=seg.is_4wd_only,
        is_4wd_recommended=seg.is_4wd_recommended,
        access_status=seg.access_status,
        has_gate_barrier=seg.has_gate_barrier,
        is_bridged=seg.is_bridged,
        length_m=seg.length_m,
        bearing_deg=seg.bearing_deg,
        elev_min_m=seg.elev_min_m,
        elev_max_m=seg.elev_max_m,
        elev_start_m=seg.elev_start_m,
        elev_end_m=seg.elev_end_m,
        elev_gain_m=seg.elev_gain_m,
        elev_loss_m=seg.elev_loss_m,
        max_slope_deg=seg.max_slope_deg,
        sustained_slope_deg=seg.sustained_slope_deg,
        is_steep_highlight=seg.is_steep_highlight,
        is_above_snowline=seg.is_above_snowline,
        elevation_profile_m=seg.elevation_profile_m,
        dem_tier=seg.dem_tier,
        waterway_crossing_count=seg.waterway_crossing_count,
        has_perennial_crossing=seg.has_perennial_crossing,
        primary_source=seg.primary_source,
        has_osm_source=seg.has_osm_source,
        source_refs=seg.source_refs,
        is_active=seg.is_active,
        superseded_by_segment_id=seg.superseded_by_segment_id,
        capad_park=_park_to_summary(park),
        grade=_grade_to_payload(grade),
        geometry=json.loads(geom_json),
    )


def _stitch_elevation_profile(
    seg_rows: list[tuple[TrackSegment, str, TrackSegmentGrade | None, CAPADPark | None]],
) -> tuple[list[ElevationProfilePoint], dict[str, float]]:
    """Build a continuous, direction-consistent elevation profile across ordered track segments.

    Returns ``(profile_points, segment_mid_dist_map)``.
    """
    profile: list[ElevationProfilePoint] = []
    seg_mid_dist: dict[str, float] = {}
    if not seg_rows:
        return profile, seg_mid_dist

    cum_dist = 0.0
    curr_exit_node: str | None = None

    for idx, (seg, _geom_json, grd, _park) in enumerate(seg_rows):
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

        seg_mid_dist[seg.segment_id] = round(cum_dist + (seg.length_m * 0.5), 1)
        raw_pts = seg.elevation_profile_m or []
        eff_grade = grd.effective_grade if grd else None
        seg_slope = round(float(seg.max_slope_deg or 0.0), 2)

        if raw_pts:
            parsed: list[tuple[float, float]] = [
                (float(p[0]), float(p[1]))
                for p in raw_pts
                if isinstance(p, (list, tuple)) and len(p) >= 2
            ]
            if parsed:
                span = parsed[-1][0]
                if reverse_seg:
                    ordered_pts = [(max(0.0, span - d), z) for d, z in reversed(parsed)]
                else:
                    ordered_pts = parsed
                for pt_idx, (local_d, elev_m) in enumerate(ordered_pts):
                    if idx > 0 and pt_idx == 0 and profile:
                        continue
                    profile.append(
                        ElevationProfilePoint(
                            dist_m=round(cum_dist + local_d, 1),
                            elev_m=round(elev_m, 2),
                            slope_deg=seg_slope,
                            segment_id=seg.segment_id,
                            grade=eff_grade,
                        )
                    )
        cum_dist += float(seg.length_m)

    return profile, seg_mid_dist


def _resolve_vehicle_context(
    db: Session,
    current_user: User | None,
    vehicle_class_param: str | None,
) -> tuple[CapabilityClassLiteral | None, str | None, bool | None]:
    """Resolve active rig capability from explicit `?vehicle_class=` or signed-in active profile."""
    if vehicle_class_param is not None:
        normalized = normalize_capability_class(vehicle_class_param)
        if normalized is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "vehicle_class must be one of: AWD, Stock 4WD, Touring 4WD, Modified 4WD"
                ),
            )
        return normalized, None, None

    if current_user is None:
        return None, None, None

    profile = db.execute(
        select(VehicleProfile)
        .where(VehicleProfile.user_id == current_user.id)
        .order_by(VehicleProfile.is_default.desc(), VehicleProfile.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    if profile is None:
        return None, None, None
    return (
        profile.capability_class,  # type: ignore[return-value]
        profile.name,
        bool(profile.has_snorkel),
    )


def _load_closures_by_track(
    db: Session, track_ids: list[str]
) -> dict[str, tuple[str, list[dict[str, Any]]]]:
    """Fetch normalized `(closure_status, active_closures)` map via Slice 04 feeds helper."""
    if not track_ids:
        return {}
    raw_map = get_closures_for_track_ids(db, track_ids)
    out: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for tid, entry in raw_map.items():
        out[tid] = (
            str(entry.get("closure_status", "open")),
            list(entry.get("active_closures") or []),
        )
    return out


def _load_reports_for_track(
    db: Session, track_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch active condition reports and distinct gate/private warnings via Slice 08 helper."""
    payload = get_reports_for_track(db, track_id, include_resolved=False)
    return (
        list(payload.get("condition_reports") or []),
        list(payload.get("community_gate_warnings") or []),
    )


def _normalize_confidence(raw: str | None) -> str:
    if not raw or raw == "baseline_only":
        return "none"
    return raw if raw in ("low", "medium", "high") else "none"


def _wet_for_suitability(raw: str | None) -> str:
    if raw == "heavy_rain":
        return "saturated"
    return raw if raw in ("dry", "damp", "wet", "saturated") else "dry"


def _get_favorite_track_ids(db: Session, user: User | None, track_ids: list[str]) -> set[str]:
    if user is None or not track_ids:
        return set()
    rows = db.execute(
        select(UserFavorite.track_id).where(
            UserFavorite.user_id == user.id,
            UserFavorite.track_id.in_(track_ids),
            UserFavorite.segment_id.is_(None),
        )
    ).scalars().all()
    return {str(tid) for tid in rows if tid is not None}


@router.get("/tracks", response_model=list[TrackSummaryResponse])
def list_tracks(
    state: str | None = Query(default=None),
    grade: str | None = Query(default=None),
    trail_class: str | None = Query(default=None),
    bbox: str | None = Query(default=None),
    q: str | None = Query(default=None),
    vehicle_class: str | None = Query(default=None),
    include_inactive: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
) -> list[TrackSummaryResponse]:
    """List canonical tracks with optional spatial bbox, state, grade, and name/alias filters."""
    parsed_bbox = _parse_bbox(bbox)
    cap_class, veh_name, has_snorkel = _resolve_vehicle_context(db, current_user, vehicle_class)

    grade_rank_expr = case(
        (TrackSegmentGrade.effective_grade == "Easy", 1),
        (TrackSegmentGrade.effective_grade == "Medium", 2),
        (TrackSegmentGrade.effective_grade == "Hard", 3),
        (TrackSegmentGrade.effective_grade == "Extreme", 4),
        else_=0,
    )
    wet_rank_expr = case(
        (TrackSegmentGrade.wet_risk_level == "damp", 1),
        (TrackSegmentGrade.wet_risk_level == "wet", 2),
        (TrackSegmentGrade.wet_risk_level == "saturated", 3),
        else_=0,
    )
    track_filters: list[Any] = []
    if not include_inactive:
        track_filters.append(Track.is_active.is_(True))
    if state:
        st_pat = f"%{_escape_like(state.strip())}%"
        track_filters.append(or_(Track.state == state, Track.state.ilike(st_pat, escape="\\")))
    if trail_class:
        track_filters.append(Track.trail_class == trail_class)
    if q:
        pattern = f"%{_escape_like(q.strip())}%"
        track_filters.append(Track.search_text.ilike(pattern, escape="\\"))
    if parsed_bbox is not None:
        min_lon, min_lat, max_lon, max_lat = parsed_bbox
        env = func.ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat, 4326)
        track_filters.append(func.ST_Intersects(Track.bbox_geom, env))

    seg_agg_cols = (
        TrackSegment.track_id.label("track_id"),
        func.count(TrackSegment.segment_id).label("segment_count"),
        func.max(grade_rank_expr).label("worst_rank"),
        func.max(TrackSegmentGrade.baseline_score).label("max_score"),
        func.max(wet_rank_expr).label("max_wet_rank"),
        func.bool_or(TrackSegment.is_4wd_only).label("any_4wd_only"),
        func.bool_or(TrackSegment.has_perennial_crossing).label("any_perennial"),
    )

    if grade is None:
        # Fast path: filter & paginate tracks FIRST, then aggregate segments ONLY for <=limit IDs.
        page_rows = db.execute(
            select(
                Track,
                CAPADPark.name.label("park_name"),
                func.ST_XMin(Track.bbox_geom).label("xmin"),
                func.ST_YMin(Track.bbox_geom).label("ymin"),
                func.ST_XMax(Track.bbox_geom).label("xmax"),
                func.ST_YMax(Track.bbox_geom).label("ymax"),
            )
            .outerjoin(CAPADPark, Track.primary_park_id == CAPADPark.id)
            .where(*track_filters)
            .order_by(Track.name.asc())
            .offset(offset)
            .limit(limit)
        ).all()

        page_track_ids = [r[0].track_id for r in page_rows]
        agg_by_track: dict[str, Any] = {}
        if page_track_ids:
            for agg_row in db.execute(
                select(*seg_agg_cols)
                .outerjoin(
                    TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id
                )
                .where(
                    TrackSegment.is_active.is_(True),
                    TrackSegment.track_id.in_(page_track_ids),
                )
                .group_by(TrackSegment.track_id)
            ).all():
                if agg_row.track_id is not None:
                    agg_by_track[agg_row.track_id] = agg_row

        rows = []
        for trk, park_name, xmin, ymin, xmax, ymax in page_rows:
            agg = agg_by_track.get(trk.track_id)
            rows.append(
                (
                    trk,
                    park_name,
                    int(agg.segment_count) if agg is not None else 0,
                    int(agg.worst_rank or 0) if agg is not None else 0,
                    agg.max_score if agg is not None else None,
                    int(agg.max_wet_rank or 0) if agg is not None else 0,
                    bool(agg.any_4wd_only) if agg is not None else False,
                    bool(agg.any_perennial) if agg is not None else False,
                    xmin,
                    ymin,
                    xmax,
                    ymax,
                )
            )
    else:
        target_rank = _GRADE_RANK.get(grade)
        if target_rank is None:
            return []
        seg_agg_stmt = (
            select(*seg_agg_cols)
            .outerjoin(TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id)
            .where(TrackSegment.is_active.is_(True))
        )
        if track_filters:
            candidate_ids_sq = select(Track.track_id).where(*track_filters)
            seg_agg_stmt = seg_agg_stmt.where(TrackSegment.track_id.in_(candidate_ids_sq))
        seg_agg = seg_agg_stmt.group_by(TrackSegment.track_id).subquery("seg_agg")

        stmt = (
            select(
                Track,
                CAPADPark.name.label("park_name"),
                func.coalesce(seg_agg.c.segment_count, 0).label("segment_count"),
                func.coalesce(seg_agg.c.worst_rank, 0).label("worst_rank"),
                seg_agg.c.max_score.label("max_score"),
                func.coalesce(seg_agg.c.max_wet_rank, 0).label("max_wet_rank"),
                func.coalesce(seg_agg.c.any_4wd_only, False).label("any_4wd_only"),
                func.coalesce(seg_agg.c.any_perennial, False).label("any_perennial"),
                func.ST_XMin(Track.bbox_geom).label("xmin"),
                func.ST_YMin(Track.bbox_geom).label("ymin"),
                func.ST_XMax(Track.bbox_geom).label("xmax"),
                func.ST_YMax(Track.bbox_geom).label("ymax"),
            )
            .outerjoin(CAPADPark, Track.primary_park_id == CAPADPark.id)
            .join(seg_agg, Track.track_id == seg_agg.c.track_id)
            .where(*track_filters, seg_agg.c.worst_rank == target_rank)
            .order_by(Track.name.asc())
            .offset(offset)
            .limit(limit)
        )
        rows = list(db.execute(stmt).all())

    track_ids = [r[0].track_id for r in rows]
    closures_map = _load_closures_by_track(db, track_ids)
    fav_ids = _get_favorite_track_ids(db, current_user, track_ids)
    live_map = get_live_summaries_for_tracks(db, track_ids)

    _wet_by_rank = {0: "dry", 1: "damp", 2: "wet", 3: "saturated"}
    results: list[TrackSummaryResponse] = []
    for (
        trk,
        park_name,
        segment_count,
        worst_rank,
        max_score,
        max_wet_rank,
        any_4wd_only,
        any_perennial,
        xmin,
        ymin,
        xmax,
        ymax,
    ) in rows:
        eff_grade = _RANK_GRADE.get(int(worst_rank)) if worst_rank else None
        live_s = live_map.get(trk.track_id, {})
        rain_ctx = live_s.get("rainfall_context") or {}
        wet_lvl = _wet_for_suitability(
            str(rain_ctx.get("wet_risk_level") or _wet_by_rank.get(int(max_wet_rank), "dry"))
        )
        c_status, active_closures = closures_map.get(trk.track_id, ("open", []))
        suitability = (
            evaluate_track_suitability(
                grade=eff_grade,  # type: ignore[arg-type]
                capability_class=cap_class,
                vehicle_name=veh_name,
                max_slope_deg=trk.max_slope_deg,
                waterway_crossing_count=int(trk.waterway_crossing_count or 0),
                has_perennial_crossing=bool(any_perennial),
                is_4wd_only=bool(any_4wd_only),
                has_snorkel=has_snorkel,
                wet_risk_level=wet_lvl,  # type: ignore[arg-type]
                closure_status=c_status,
            )
            if cap_class is not None
            else None
        )
        results.append(
            TrackSummaryResponse(
                track_id=trk.track_id,
                name=trk.name,
                alt_names=list(trk.alt_names or []),
                state=trk.state,
                primary_source=trk.primary_source,
                has_osm_source=trk.has_osm_source,
                trail_class=trk.trail_class,
                is_vehicular=trk.is_vehicular,
                total_length_m=trk.total_length_m,
                elev_min_m=trk.elev_min_m,
                elev_max_m=trk.elev_max_m,
                elev_gain_m=trk.elev_gain_m,
                max_slope_deg=trk.max_slope_deg,
                waterway_crossing_count=trk.waterway_crossing_count,
                primary_park_name=park_name,
                effective_grade=eff_grade,
                grade=eff_grade,
                score=round(float(max_score), 1) if max_score is not None else None,
                live_grade=live_s.get("live_grade"),
                live_score=live_s.get("live_score"),
                confidence_level=str(live_s.get("confidence_level") or "none"),
                sample_count=int(live_s.get("sample_count") or 0),
                last_driven_at=live_s.get("last_driven_at"),
                avg_speed_kmh=live_s.get("avg_speed_kmh"),
                est_duration_min=live_s.get("est_duration_min"),
                bbox=_bbox_list(xmin, ymin, xmax, ymax),
                segment_count=int(segment_count),
                is_active=trk.is_active,
                closure_status=c_status,
                active_closures=active_closures,
                suitability=suitability,
                is_favorite=(trk.track_id in fav_ids),
            )
        )
    return results


@router.get("/tracks/geojson")
def get_corridor_tracks_geojson(
    request: Request,
    bbox: str | None = Query(default=None),
    state: str | None = Query(default=None),
    grade: str | None = Query(default=None),
    limit: int = Query(default=2000, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> Response:
    """Return active canonical graded track segments as a GeoJSON FeatureCollection with ETag."""
    parsed_bbox = _parse_bbox(bbox)
    # Project only needed scalar columns (skips heavy elevation_profile_m, source_refs & JSONB)
    stmt = (
        select(
            TrackSegment.segment_id,
            TrackSegment.track_id,
            TrackSegment.segment_seq,
            TrackSegment.name,
            TrackSegment.state,
            TrackSegment.trail_class,
            TrackSegment.surface_class,
            TrackSegment.tracktype,
            TrackSegment.length_m,
            TrackSegment.elev_min_m,
            TrackSegment.elev_max_m,
            TrackSegment.max_slope_deg,
            TrackSegment.sustained_slope_deg,
            TrackSegment.is_steep_highlight,
            TrackSegment.is_above_snowline,
            TrackSegment.waterway_crossing_count,
            TrackSegment.has_perennial_crossing,
            TrackSegment.is_4wd_only,
            TrackSegment.access_status,
            TrackSegment.has_gate_barrier,
            TrackSegment.primary_source,
            TrackSegment.has_osm_source,
            TrackSegment.updated_at.label("seg_updated_at"),
            func.ST_AsGeoJSON(TrackSegment.geom).label("geom_json"),
            TrackSegmentGrade.baseline_grade,
            TrackSegmentGrade.baseline_score,
            TrackSegmentGrade.effective_grade,
            TrackSegmentGrade.live_grade,
            TrackSegmentGrade.live_score,
            TrackSegmentGrade.confidence_level,
            TrackSegmentGrade.is_obstacle_hotspot,
            TrackSegmentGrade.wet_risk_level,
            TrackSegmentGrade.live_computed_at,
        )
        .outerjoin(TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id)
        .where(TrackSegment.is_active.is_(True))
    )
    if state:
        stmt = stmt.where(TrackSegment.state == state)
    if grade:
        stmt = stmt.where(TrackSegmentGrade.effective_grade == grade)
    if parsed_bbox is not None:
        min_lon, min_lat, max_lon, max_lat = parsed_bbox
        env = func.ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat, 4326)
        stmt = stmt.where(func.ST_Intersects(TrackSegment.geom, env))

    stmt = stmt.order_by(TrackSegment.track_id, TrackSegment.segment_seq).limit(limit)
    rows = db.execute(stmt).all()

    etag_hasher = hashlib.sha256()
    etag_hasher.update(f"{bbox or ''}|{state or ''}|{grade or ''}|{limit}|{len(rows)}".encode())
    features: list[dict[str, Any]] = []
    for row in rows:
        etag_hasher.update(
            f"|{row.segment_id}:{row.seg_updated_at}:{row.effective_grade}:{row.live_computed_at}".encode()
        )
        features.append(
            {
                "type": "Feature",
                "id": row.segment_id,
                "geometry": json.loads(row.geom_json),
                "properties": {
                    "segment_id": row.segment_id,
                    "track_id": row.track_id,
                    "track_name": row.name,
                    "segment_seq": row.segment_seq,
                    "name": row.name,
                    "state": row.state,
                    "trail_class": row.trail_class,
                    "surface_class": row.surface_class,
                    "tracktype": row.tracktype,
                    "length_m": row.length_m,
                    "min_elev_m": row.elev_min_m,
                    "max_elev_m": row.elev_max_m,
                    "elev_min_m": row.elev_min_m,
                    "elev_max_m": row.elev_max_m,
                    "max_slope_deg": row.max_slope_deg,
                    "sustained_slope_deg": row.sustained_slope_deg,
                    "is_steep_highlight": row.is_steep_highlight,
                    "is_above_snowline": row.is_above_snowline,
                    "waterway_crossing_count": row.waterway_crossing_count,
                    "has_perennial_crossing": row.has_perennial_crossing,
                    "is_4wd_only": row.is_4wd_only,
                    "access_status": row.access_status,
                    "has_gate_barrier": row.has_gate_barrier,
                    "primary_source": row.primary_source,
                    "has_osm_source": row.has_osm_source,
                    "grade": row.effective_grade,
                    "score": (
                        row.live_score if row.live_score is not None else row.baseline_score
                    ),
                    "baseline_grade": row.baseline_grade,
                    "baseline_score": row.baseline_score,
                    "effective_grade": row.effective_grade,
                    "live_grade": row.live_grade,
                    "live_score": row.live_score,
                    "confidence_level": _normalize_confidence(row.confidence_level),
                    "is_obstacle_hotspot": bool(row.is_obstacle_hotspot),
                    "wet_risk_level": str(row.wet_risk_level or "dry"),
                },
            }
        )
    etag = f'"{etag_hasher.hexdigest()[:24]}"'
    cache_headers = {
        "ETag": etag,
        "Cache-Control": "public, max-age=60",
    }
    if_none_match = (request.headers.get("if-none-match") or "").strip()
    if if_none_match and (if_none_match == etag or if_none_match == f"W/{etag}"):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=cache_headers)
    return JSONResponse(
        content={"type": "FeatureCollection", "features": features},
        headers=cache_headers,
    )


@router.get("/tracks/{track_id}", response_model=TrackDetailResponse)
def get_track_detail(
    track_id: str,
    vehicle_class: str | None = Query(default=None),
    current_user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
) -> TrackDetailResponse:
    """Return full Track Inspector payload: profile, breakdown, crossings, closures, suitability."""
    cap_class, veh_name, has_snorkel = _resolve_vehicle_context(db, current_user, vehicle_class)
    row = db.execute(
        select(
            Track,
            CAPADPark,
            func.ST_XMin(Track.bbox_geom).label("xmin"),
            func.ST_YMin(Track.bbox_geom).label("ymin"),
            func.ST_XMax(Track.bbox_geom).label("xmax"),
            func.ST_YMax(Track.bbox_geom).label("ymax"),
        )
        .outerjoin(CAPADPark, Track.primary_park_id == CAPADPark.id)
        .where(Track.track_id == track_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found")

    trk, primary_park, xmin, ymin, xmax, ymax = row

    raw_seg_rows = db.execute(
        select(
            TrackSegment,
            func.ST_AsGeoJSON(TrackSegment.geom).label("geom_json"),
            TrackSegmentGrade,
            CAPADPark,
        )
        .outerjoin(TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id)
        .outerjoin(CAPADPark, TrackSegment.capad_park_id == CAPADPark.id)
        .where(TrackSegment.track_id == track_id, TrackSegment.is_active.is_(True))
        .order_by(TrackSegment.segment_seq.asc().nullslast(), TrackSegment.segment_id.asc())
    ).all()
    seg_rows: list[tuple[TrackSegment, str, TrackSegmentGrade | None, CAPADPark | None]] = [
        (r[0], r[1], r[2], r[3]) for r in raw_seg_rows
    ]

    segments: list[SegmentDetailResponse] = []
    merged_refs: list[dict[str, Any]] = []
    seen_ref_keys: set[tuple[str, str]] = set()
    grade_dist: Counter[str] = Counter()
    worst_rank = 0
    rep_seg: TrackSegment | None = None
    rep_grade: TrackSegmentGrade | None = None
    rep_sort_key: tuple[float, float, int, str] | None = None
    elev_loss_total = 0.0
    max_seg_gain = 0.0
    max_seg_loss = 0.0
    max_slope_val: float | None = None
    max_sustained_slope: float | None = None
    worst_surface = "unknown"
    worst_tracktype: str | None = None
    is_4wd_only = False
    is_4wd_recommended = False
    has_gate_barrier = False
    any_unbridged_perennial = False
    dem_tier_val: str | None = None
    access_status = "public"
    steep_count = 0
    snowline_count = 0
    parks_by_id: dict[int, CAPADPark] = {}

    if primary_park is not None:
        parks_by_id[primary_park.id] = primary_park

    for seg, geom_json, grd, park in seg_rows:
        segments.append(_segment_to_detail(seg, geom_json, grd, park))
        elev_loss_total += float(seg.elev_loss_m or 0.0)
        max_seg_gain = max(max_seg_gain, float(seg.elev_gain_m or 0.0))
        max_seg_loss = max(max_seg_loss, float(seg.elev_loss_m or 0.0))
        if seg.max_slope_deg is not None and (
            max_slope_val is None or seg.max_slope_deg > max_slope_val
        ):
            max_slope_val = float(seg.max_slope_deg)
        if seg.sustained_slope_deg is not None and (
            max_sustained_slope is None or seg.sustained_slope_deg > max_sustained_slope
        ):
            max_sustained_slope = float(seg.sustained_slope_deg)
        if _SURFACE_RANK.get(seg.surface_class, 0) > _SURFACE_RANK.get(worst_surface, 0):
            worst_surface = seg.surface_class
        if seg.tracktype and _TRACKTYPE_RANK.get(seg.tracktype, 0) > _TRACKTYPE_RANK.get(
            worst_tracktype or "", 0
        ):
            worst_tracktype = seg.tracktype
        is_4wd_only = is_4wd_only or bool(seg.is_4wd_only)
        is_4wd_recommended = is_4wd_recommended or bool(seg.is_4wd_recommended)
        has_gate_barrier = has_gate_barrier or bool(seg.has_gate_barrier)
        any_unbridged_perennial = any_unbridged_perennial or bool(seg.has_perennial_crossing)
        if dem_tier_val is None and seg.dem_tier:
            dem_tier_val = seg.dem_tier
        if _ACCESS_STRICTNESS.get(seg.access_status, 0) > _ACCESS_STRICTNESS.get(access_status, 0):
            access_status = seg.access_status
        if seg.is_steep_highlight:
            steep_count += 1
        if seg.is_above_snowline:
            snowline_count += 1
        if park is not None:
            parks_by_id.setdefault(park.id, park)
        for ref in seg.source_refs or []:
            if isinstance(ref, dict):
                rkey = (str(ref.get("source", "")), str(ref.get("source_id", "")))
                if rkey not in seen_ref_keys:
                    seen_ref_keys.add(rkey)
                    merged_refs.append(dict(ref))
        if grd is not None:
            grade_dist[grd.effective_grade] += 1
            rank = _GRADE_RANK.get(grd.effective_grade, 0)
            if rank > worst_rank:
                worst_rank = rank
            # Deterministic hardest-segment tie-break: score DESC, max_slope DESC, crossings DESC,
            # segment_id ASC (compare on negated segment_id via explicit branch).
            cand_metrics = (
                float(grd.baseline_score),
                float(seg.max_slope_deg or 0.0),
                int(seg.waterway_crossing_count or 0),
            )
            if rep_sort_key is None or cand_metrics > rep_sort_key[:3] or (
                cand_metrics == rep_sort_key[:3] and seg.segment_id < rep_sort_key[3]
            ):
                rep_sort_key = (*cand_metrics, seg.segment_id)
                rep_grade = grd
                rep_seg = seg

    # Continuous stitched elevation profile + distance index per segment
    elevation_profile, seg_mid_dist = _stitch_elevation_profile(seg_rows)

    # Waterway crossings along track segments (PostGIS intersection against SourceWaterway)
    waterway_crossings: list[WaterwayCrossingItem] = []
    has_major_perennial = False
    if seg_rows:
        ww_rows = db.execute(
            select(
                SourceWaterway.source_feature_id,
                SourceWaterway.name,
                SourceWaterway.feature_type,
                SourceWaterway.perenniality,
                SourceWaterway.hierarchy,
                TrackSegment.segment_id,
                TrackSegment.is_bridged,
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
                TrackSegment.track_id == track_id,
                TrackSegment.is_active.is_(True),
                func.lower(SourceWaterway.feature_type) != "connector",
                func.ST_DWithin(TrackSegment.geom, SourceWaterway.geom, 0.0005),
            )
        ).all()
        seen_crossings: dict[tuple[str, bool], WaterwayCrossingItem] = {}
        for ww_fid, ww_name, ww_type, perenn, hierarchy, seg_id, seg_bridged in ww_rows:
            dist_m = seg_mid_dist.get(seg_id, 0.0)
            is_perennial = str(perenn).strip().lower() == "perennial"
            if is_perennial and not seg_bridged and str(hierarchy or "").strip().lower() == "major":
                has_major_perennial = True
            key = (ww_fid, bool(seg_bridged))
            if key not in seen_crossings or dist_m < seen_crossings[key].dist_m:
                seen_crossings[key] = WaterwayCrossingItem(
                    name=ww_name or "Unnamed watercourse",
                    waterway_type=ww_type,
                    perennial=is_perennial,
                    bridged=bool(seg_bridged),
                    dist_m=dist_m,
                )
        waterway_crossings = sorted(seen_crossings.values(), key=lambda c: (c.dist_m, c.name))

    # Track-level explainable baseline_breakdown consistent with track header stats
    baseline_breakdown: dict[str, Any] | None = None
    track_grade_result = None
    if seg_rows:
        unbridged_items = [c for c in waterway_crossings if not c.bridged]
        unbridged_names = list(dict.fromkeys(c.name for c in unbridged_items if c.name))
        unbridged_count = max(int(trk.waterway_crossing_count or 0), len(unbridged_items))
        all_bridged = all(s.is_bridged for s, _, _, _ in seg_rows) and unbridged_count == 0
        track_grade_result = compute_baseline_grade(
            SegmentGradeInput(
                surface_class=worst_surface,  # type: ignore[arg-type]
                tracktype=worst_tracktype,  # type: ignore[arg-type]
                trail_class=trk.trail_class,  # type: ignore[arg-type]
                max_slope_deg=max_slope_val if max_slope_val is not None else trk.max_slope_deg,
                sustained_slope_deg=max_sustained_slope,
                elev_gain_m=max_seg_gain,
                elev_loss_m=max_seg_loss,
                unbridged_crossing_count=unbridged_count,
                has_perennial_crossing=any_unbridged_perennial
                or any(c.perennial for c in unbridged_items),
                has_major_perennial_crossing=has_major_perennial,
                waterway_names=unbridged_names,
                is_bridged=all_bridged,
                is_ford=any(c.perennial for c in unbridged_items),
                is_4wd_only=is_4wd_only,
                is_4wd_recommended=is_4wd_recommended,
                access_status=access_status,  # type: ignore[arg-type]
                has_gate_barrier=has_gate_barrier,
                dem_tier=dem_tier_val,  # type: ignore[arg-type]
            )
        )
        baseline_breakdown = dict(track_grade_result.baseline_breakdown)
        baseline_breakdown["track_summary"] = {
            "hardest_segment_id": rep_seg.segment_id if rep_seg else None,
            "representative_segment_id": rep_seg.segment_id if rep_seg else None,
            "hardest_segment_score": round(float(rep_grade.baseline_score), 1)
            if rep_grade is not None
            else None,
            "segment_count": len(segments),
            "steep_segment_count": steep_count,
            "snowline_segment_count": snowline_count,
            "grade_distribution": dict(grade_dist),
        }

    # Intersecting CAPAD parks & nearby POIs reusing a single materialized ST_Union geometry
    nearby_pois: list[PoiResponse] = []
    if seg_rows:
        track_union_wkb = db.execute(
            select(func.ST_AsEWKB(func.ST_Union(TrackSegment.geom))).where(
                TrackSegment.track_id == track_id,
                TrackSegment.is_active.is_(True),
            )
        ).scalar_one_or_none()

        if track_union_wkb is not None:
            union_geom = func.ST_GeomFromEWKB(bytes(track_union_wkb))
            park_rows = db.execute(
                select(CAPADPark)
                .where(func.ST_Intersects(CAPADPark.geom, union_geom))
                .order_by(CAPADPark.name.asc())
            ).scalars().all()
            for pk in park_rows:
                parks_by_id.setdefault(pk.id, pk)

            # Nearby POIs within 1,500 m sorted by metric distance in EPSG:3577
            union_3577 = func.ST_Transform(union_geom, 3577)
            dist_expr = func.ST_Distance(
                func.ST_Transform(POI.geom, 3577),
                union_3577,
            ).label("distance_m")
            poi_rows = db.execute(
                select(
                    POI,
                    func.ST_X(POI.geom).label("lon"),
                    func.ST_Y(POI.geom).label("lat"),
                    CAPADPark.name.label("park_name"),
                    dist_expr,
                )
                .outerjoin(CAPADPark, POI.capad_park_id == CAPADPark.id)
                .where(
                    func.ST_DWithin(POI.geom, union_geom, 0.025),
                    func.ST_DWithin(
                        func.ST_Transform(POI.geom, 3577),
                        union_3577,
                        _NEARBY_POI_RADIUS_M,
                    ),
                )
                .order_by(dist_expr.asc(), POI.name.asc())
                .limit(25)
            ).all()
            nearby_pois = [
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
                    lon=float(lon),
                    lat=float(lat),
                    distance_m=round(float(dist_m), 1),
                )
                for poi, lon, lat, park_name, dist_m in poi_rows
            ]

    if track_grade_result is not None:
        eff_grade = _RANK_GRADE.get(
            max(worst_rank, _GRADE_RANK.get(track_grade_result.grade, 0))
        )
        peak_score = round(float(track_grade_result.score), 1)
    else:
        eff_grade = _RANK_GRADE.get(worst_rank) if worst_rank else None
        peak_score = round(float(rep_grade.baseline_score), 1) if rep_grade is not None else None
    capad_parks_list = [
        summary
        for pk in sorted(parks_by_id.values(), key=lambda p: p.name)
        if (summary := _park_to_summary(pk)) is not None
    ]

    c_status, active_closures = _load_closures_by_track(db, [trk.track_id]).get(
        trk.track_id, ("open", [])
    )
    condition_reports, community_gate_warnings = _load_reports_for_track(db, trk.track_id)
    live_summary = get_track_live_summary(db, trk.track_id)
    rain_ctx = live_summary.get("rainfall_context") or {}
    suit_wet = _wet_for_suitability(str(rain_ctx.get("wet_risk_level") or "dry"))
    is_fav = trk.track_id in _get_favorite_track_ids(db, current_user, [trk.track_id])
    suitability = (
        evaluate_track_suitability(
            grade=eff_grade,  # type: ignore[arg-type]
            capability_class=cap_class,
            vehicle_name=veh_name,
            max_slope_deg=max_slope_val if max_slope_val is not None else trk.max_slope_deg,
            waterway_crossing_count=int(trk.waterway_crossing_count or 0),
            has_perennial_crossing=any_unbridged_perennial,
            is_4wd_only=is_4wd_only,
            has_snorkel=has_snorkel,
            wet_risk_level=suit_wet,  # type: ignore[arg-type]
            surface_class=worst_surface,
            closure_status=c_status,
        )
        if cap_class is not None
        else None
    )

    return TrackDetailResponse(
        track_id=trk.track_id,
        name=trk.name,
        alt_names=list(trk.alt_names or []),
        state=trk.state,
        primary_source=trk.primary_source,
        has_osm_source=trk.has_osm_source,
        trail_class=trk.trail_class,
        is_vehicular=trk.is_vehicular,
        total_length_m=trk.total_length_m,
        elev_min_m=trk.elev_min_m,
        elev_max_m=trk.elev_max_m,
        min_elev_m=trk.elev_min_m,
        max_elev_m=trk.elev_max_m,
        elev_gain_m=trk.elev_gain_m,
        elev_loss_m=round(elev_loss_total, 2),
        max_slope_deg=trk.max_slope_deg,
        sustained_slope_deg=(
            round(max_sustained_slope, 2) if max_sustained_slope is not None else None
        ),
        waterway_crossing_count=trk.waterway_crossing_count,
        is_4wd_only=is_4wd_only,
        has_gate_barrier=has_gate_barrier,
        access_status=access_status,
        primary_park_name=primary_park.name if primary_park else None,
        effective_grade=eff_grade,
        grade=eff_grade,
        score=peak_score,
        live_grade=live_summary["live_grade"],
        live_score=live_summary["live_score"],
        confidence_level=live_summary["confidence_level"],
        sample_count=live_summary["sample_count"],
        last_driven_at=live_summary["last_driven_at"],
        avg_speed_kmh=live_summary["avg_speed_kmh"],
        est_duration_min=live_summary["est_duration_min"],
        vehicle_class_split=live_summary["vehicle_class_split"],
        live_breakdown=live_summary["live_breakdown"],
        rainfall_context=live_summary["rainfall_context"],
        obstacle_hotspots=live_summary["obstacle_hotspots"],
        bbox=_bbox_list(xmin, ymin, xmax, ymax),
        source_refs=merged_refs,
        baseline_breakdown=baseline_breakdown,
        elevation_profile=elevation_profile,
        waterway_crossings=waterway_crossings,
        segment_count=len(segments),
        is_active=trk.is_active,
        closure_status=c_status,
        active_closures=active_closures,
        suitability=suitability,
        is_favorite=is_fav,
        primary_park=_park_to_summary(primary_park),
        capad_parks=capad_parks_list,
        segments=segments,
        nearby_pois=nearby_pois,
        condition_reports=condition_reports,
        community_gate_warnings=community_gate_warnings,
    )


def get_track_details_batch(
    db: Session,
    track_ids: list[str],
) -> list[TrackDetailResponse]:
    """Batch-build anonymous TrackDetailResponse payloads for a chunk of track IDs (~8 queries).

    Used by offline regional pack streaming (`packs/builder.py`) to avoid N+1 per-track queries
    while preserving bounded memory per chunk.
    """
    unique_ids = list(dict.fromkeys(tid for tid in track_ids if tid))
    if not unique_ids:
        return []

    track_rows = db.execute(
        select(
            Track,
            CAPADPark,
            func.ST_XMin(Track.bbox_geom).label("xmin"),
            func.ST_YMin(Track.bbox_geom).label("ymin"),
            func.ST_XMax(Track.bbox_geom).label("xmax"),
            func.ST_YMax(Track.bbox_geom).label("ymax"),
        )
        .outerjoin(CAPADPark, Track.primary_park_id == CAPADPark.id)
        .where(Track.track_id.in_(unique_ids))
    ).all()
    track_meta_by_id = {r[0].track_id: r for r in track_rows}
    found_ids = [tid for tid in unique_ids if tid in track_meta_by_id]
    if not found_ids:
        return []

    # 1. Segments + grades + segment parks for all tracks in batch
    raw_seg_rows = db.execute(
        select(
            TrackSegment,
            func.ST_AsGeoJSON(TrackSegment.geom).label("geom_json"),
            TrackSegmentGrade,
            CAPADPark,
        )
        .outerjoin(TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id)
        .outerjoin(CAPADPark, TrackSegment.capad_park_id == CAPADPark.id)
        .where(TrackSegment.track_id.in_(found_ids), TrackSegment.is_active.is_(True))
        .order_by(
            TrackSegment.track_id.asc(),
            TrackSegment.segment_seq.asc().nullslast(),
            TrackSegment.segment_id.asc(),
        )
    ).all()
    segs_by_track: dict[
        str, list[tuple[TrackSegment, str, TrackSegmentGrade | None, CAPADPark | None]]
    ] = {
        tid: [] for tid in found_ids
    }
    for seg, geom_json, grd, park in raw_seg_rows:
        if seg.track_id in segs_by_track:
            segs_by_track[seg.track_id].append((seg, geom_json, grd, park))

    # 2. Waterway crossings across all tracks in batch
    ww_by_track: dict[str, list[Any]] = {tid: [] for tid in found_ids}
    ww_rows = db.execute(
        select(
            TrackSegment.track_id,
            SourceWaterway.source_feature_id,
            SourceWaterway.name,
            SourceWaterway.feature_type,
            SourceWaterway.perenniality,
            SourceWaterway.hierarchy,
            TrackSegment.segment_id,
            TrackSegment.is_bridged,
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
            TrackSegment.track_id.in_(found_ids),
            TrackSegment.is_active.is_(True),
            func.lower(SourceWaterway.feature_type) != "connector",
            func.ST_DWithin(TrackSegment.geom, SourceWaterway.geom, 0.0005),
        )
    ).all()
    for row in ww_rows:
        if row.track_id in ww_by_track:
            ww_by_track[row.track_id].append(row)

    # 3. Intersecting CAPAD parks per track in one spatial query
    parks_extra_by_track: dict[str, dict[int, CAPADPark]] = {tid: {} for tid in found_ids}
    park_join_rows = db.execute(
        select(TrackSegment.track_id, CAPADPark)
        .join(CAPADPark, func.ST_Intersects(CAPADPark.geom, TrackSegment.geom))
        .where(TrackSegment.track_id.in_(found_ids), TrackSegment.is_active.is_(True))
    ).all()
    for tid, pk in park_join_rows:
        if tid in parks_extra_by_track:
            parks_extra_by_track[tid].setdefault(pk.id, pk)

    # 4. Nearby POIs within 1,500 m per track in one spatial query (bounded to top 25 per track)
    pois_by_track: dict[str, list[PoiResponse]] = {tid: [] for tid in found_ids}
    dist_expr = func.min(
        func.ST_Distance(
            func.ST_Transform(POI.geom, 3577),
            func.ST_Transform(TrackSegment.geom, 3577),
        )
    ).label("distance_m")
    poi_join_rows = db.execute(
        select(
            TrackSegment.track_id,
            POI.id.label("poi_id"),
            POI.poi_key,
            POI.name,
            POI.poi_type,
            POI.state,
            POI.elev_m,
            POI.access_note,
            POI.nearest_segment_id,
            POI.primary_source,
            POI.source_license,
            func.ST_X(POI.geom).label("lon"),
            func.ST_Y(POI.geom).label("lat"),
            CAPADPark.name.label("park_name"),
            dist_expr,
        )
        .join(
            TrackSegment,
            and_(
                TrackSegment.track_id.in_(found_ids),
                TrackSegment.is_active.is_(True),
                func.ST_DWithin(POI.geom, TrackSegment.geom, 0.025),
                func.ST_DWithin(
                    func.ST_Transform(POI.geom, 3577),
                    func.ST_Transform(TrackSegment.geom, 3577),
                    _NEARBY_POI_RADIUS_M,
                ),
            ),
        )
        .outerjoin(CAPADPark, POI.capad_park_id == CAPADPark.id)
        .group_by(TrackSegment.track_id, POI.id, CAPADPark.name)
        .order_by(TrackSegment.track_id.asc(), dist_expr.asc(), POI.name.asc())
    ).all()
    for prow in poi_join_rows:
        bucket = pois_by_track.get(prow.track_id)
        if bucket is not None and len(bucket) < 25:
            bucket.append(
                PoiResponse(
                    id=prow.poi_id,
                    poi_key=prow.poi_key,
                    name=prow.name,
                    poi_type=prow.poi_type,
                    state=prow.state,
                    elev_m=prow.elev_m,
                    access_note=prow.access_note,
                    nearest_segment_id=prow.nearest_segment_id,
                    capad_park_name=prow.park_name,
                    primary_source=prow.primary_source,
                    source_license=prow.source_license,
                    lon=float(prow.lon),
                    lat=float(prow.lat),
                    distance_m=round(float(prow.distance_m), 1),
                )
            )

    # 5. Batched closures, reports, and live summaries
    closures_map = _load_closures_by_track(db, found_ids)
    reports_map = get_reports_for_tracks(db, found_ids, include_resolved=False)
    live_map = get_live_summaries_for_tracks(db, found_ids)

    details: list[TrackDetailResponse] = []
    for tid in found_ids:
        trk, primary_park, xmin, ymin, xmax, ymax = track_meta_by_id[tid]
        seg_rows = segs_by_track.get(tid, [])

        segments: list[SegmentDetailResponse] = []
        merged_refs: list[dict[str, Any]] = []
        seen_ref_keys: set[tuple[str, str]] = set()
        grade_dist: Counter[str] = Counter()
        worst_rank = 0
        rep_seg: TrackSegment | None = None
        rep_grade: TrackSegmentGrade | None = None
        rep_sort_key: tuple[float, float, int, str] | None = None
        elev_loss_total = 0.0
        max_seg_gain = 0.0
        max_seg_loss = 0.0
        max_slope_val: float | None = None
        max_sustained_slope: float | None = None
        worst_surface = "unknown"
        worst_tracktype: str | None = None
        is_4wd_only = False
        is_4wd_recommended = False
        has_gate_barrier = False
        any_unbridged_perennial = False
        dem_tier_val: str | None = None
        access_status = "public"
        steep_count = 0
        snowline_count = 0
        parks_by_id: dict[int, CAPADPark] = dict(parks_extra_by_track.get(tid, {}))
        if primary_park is not None:
            parks_by_id.setdefault(primary_park.id, primary_park)

        for seg, geom_json, grd, park in seg_rows:
            segments.append(_segment_to_detail(seg, geom_json, grd, park))
            elev_loss_total += float(seg.elev_loss_m or 0.0)
            max_seg_gain = max(max_seg_gain, float(seg.elev_gain_m or 0.0))
            max_seg_loss = max(max_seg_loss, float(seg.elev_loss_m or 0.0))
            if seg.max_slope_deg is not None and (
                max_slope_val is None or seg.max_slope_deg > max_slope_val
            ):
                max_slope_val = float(seg.max_slope_deg)
            if seg.sustained_slope_deg is not None and (
                max_sustained_slope is None or seg.sustained_slope_deg > max_sustained_slope
            ):
                max_sustained_slope = float(seg.sustained_slope_deg)
            if _SURFACE_RANK.get(seg.surface_class, 0) > _SURFACE_RANK.get(worst_surface, 0):
                worst_surface = seg.surface_class
            if seg.tracktype and _TRACKTYPE_RANK.get(seg.tracktype, 0) > _TRACKTYPE_RANK.get(
                worst_tracktype or "", 0
            ):
                worst_tracktype = seg.tracktype
            is_4wd_only = is_4wd_only or bool(seg.is_4wd_only)
            is_4wd_recommended = is_4wd_recommended or bool(seg.is_4wd_recommended)
            has_gate_barrier = has_gate_barrier or bool(seg.has_gate_barrier)
            any_unbridged_perennial = any_unbridged_perennial or bool(seg.has_perennial_crossing)
            if dem_tier_val is None and seg.dem_tier:
                dem_tier_val = seg.dem_tier
            if _ACCESS_STRICTNESS.get(seg.access_status, 0) > _ACCESS_STRICTNESS.get(
                access_status, 0
            ):
                access_status = seg.access_status
            if seg.is_steep_highlight:
                steep_count += 1
            if seg.is_above_snowline:
                snowline_count += 1
            if park is not None:
                parks_by_id.setdefault(park.id, park)
            for ref in seg.source_refs or []:
                if isinstance(ref, dict):
                    rkey = (str(ref.get("source", "")), str(ref.get("source_id", "")))
                    if rkey not in seen_ref_keys:
                        seen_ref_keys.add(rkey)
                        merged_refs.append(dict(ref))
            if grd is not None:
                grade_dist[grd.effective_grade] += 1
                rank = _GRADE_RANK.get(grd.effective_grade, 0)
                if rank > worst_rank:
                    worst_rank = rank
                cand_metrics = (
                    float(grd.baseline_score),
                    float(seg.max_slope_deg or 0.0),
                    int(seg.waterway_crossing_count or 0),
                )
                if rep_sort_key is None or cand_metrics > rep_sort_key[:3] or (
                    cand_metrics == rep_sort_key[:3] and seg.segment_id < rep_sort_key[3]
                ):
                    rep_sort_key = (*cand_metrics, seg.segment_id)
                    rep_grade = grd
                    rep_seg = seg

        elevation_profile, seg_mid_dist = _stitch_elevation_profile(seg_rows)

        waterway_crossings: list[WaterwayCrossingItem] = []
        has_major_perennial = False
        seen_crossings: dict[tuple[str, bool], WaterwayCrossingItem] = {}
        for (
            _tid,
            ww_fid,
            ww_name,
            ww_type,
            perenn,
            hierarchy,
            seg_id,
            seg_bridged,
        ) in ww_by_track.get(tid, []):
            dist_m = seg_mid_dist.get(seg_id, 0.0)
            is_perennial = str(perenn).strip().lower() == "perennial"
            if is_perennial and not seg_bridged and str(hierarchy or "").strip().lower() == "major":
                has_major_perennial = True
            key = (ww_fid, bool(seg_bridged))
            if key not in seen_crossings or dist_m < seen_crossings[key].dist_m:
                seen_crossings[key] = WaterwayCrossingItem(
                    name=ww_name or "Unnamed watercourse",
                    waterway_type=ww_type,
                    perennial=is_perennial,
                    bridged=bool(seg_bridged),
                    dist_m=dist_m,
                )
        waterway_crossings = sorted(seen_crossings.values(), key=lambda c: (c.dist_m, c.name))

        baseline_breakdown: dict[str, Any] | None = None
        track_grade_result = None
        if seg_rows:
            unbridged_items = [c for c in waterway_crossings if not c.bridged]
            unbridged_names = list(dict.fromkeys(c.name for c in unbridged_items if c.name))
            unbridged_count = max(int(trk.waterway_crossing_count or 0), len(unbridged_items))
            all_bridged = all(s.is_bridged for s, _, _, _ in seg_rows) and unbridged_count == 0
            track_grade_result = compute_baseline_grade(
                SegmentGradeInput.model_validate(
                    {
                        "surface_class": worst_surface,
                        "tracktype": worst_tracktype,
                        "trail_class": trk.trail_class,
                        "max_slope_deg": (
                            max_slope_val if max_slope_val is not None else trk.max_slope_deg
                        ),
                        "sustained_slope_deg": max_sustained_slope,
                        "elev_gain_m": max_seg_gain,
                        "elev_loss_m": max_seg_loss,
                        "unbridged_crossing_count": unbridged_count,
                        "has_perennial_crossing": any_unbridged_perennial
                        or any(c.perennial for c in unbridged_items),
                        "has_major_perennial_crossing": has_major_perennial,
                        "waterway_names": unbridged_names,
                        "is_bridged": all_bridged,
                        "is_ford": any(c.perennial for c in unbridged_items),
                        "is_4wd_only": is_4wd_only,
                        "is_4wd_recommended": is_4wd_recommended,
                        "access_status": access_status,
                        "has_gate_barrier": has_gate_barrier,
                        "dem_tier": dem_tier_val,
                    }
                )
            )
            baseline_breakdown = dict(track_grade_result.baseline_breakdown)
            baseline_breakdown["track_summary"] = {
                "hardest_segment_id": rep_seg.segment_id if rep_seg else None,
                "representative_segment_id": rep_seg.segment_id if rep_seg else None,
                "hardest_segment_score": round(float(rep_grade.baseline_score), 1)
                if rep_grade is not None
                else None,
                "segment_count": len(segments),
                "steep_segment_count": steep_count,
                "snowline_segment_count": snowline_count,
                "grade_distribution": dict(grade_dist),
            }

        if track_grade_result is not None:
            eff_grade = _RANK_GRADE.get(
                max(worst_rank, _GRADE_RANK.get(track_grade_result.grade, 0))
            )
            peak_score = round(float(track_grade_result.score), 1)
        else:
            eff_grade = _RANK_GRADE.get(worst_rank) if worst_rank else None
            peak_score = (
                round(float(rep_grade.baseline_score), 1) if rep_grade is not None else None
            )

        capad_parks_list = [
            summary
            for pk in sorted(parks_by_id.values(), key=lambda p: p.name)
            if (summary := _park_to_summary(pk)) is not None
        ]
        c_status, active_closures = closures_map.get(tid, ("open", []))
        rep_entry = reports_map.get(tid, {})
        condition_reports = list(rep_entry.get("condition_reports") or [])
        community_gate_warnings = list(rep_entry.get("community_gate_warnings") or [])
        live_summary = live_map.get(tid) or {}

        details.append(
            TrackDetailResponse(
                track_id=trk.track_id,
                name=trk.name,
                alt_names=list(trk.alt_names or []),
                state=trk.state,
                primary_source=trk.primary_source,
                has_osm_source=trk.has_osm_source,
                trail_class=trk.trail_class,
                is_vehicular=trk.is_vehicular,
                total_length_m=trk.total_length_m,
                elev_min_m=trk.elev_min_m,
                elev_max_m=trk.elev_max_m,
                min_elev_m=trk.elev_min_m,
                max_elev_m=trk.elev_max_m,
                elev_gain_m=trk.elev_gain_m,
                elev_loss_m=round(elev_loss_total, 2),
                max_slope_deg=trk.max_slope_deg,
                sustained_slope_deg=(
                    round(max_sustained_slope, 2) if max_sustained_slope is not None else None
                ),
                waterway_crossing_count=trk.waterway_crossing_count,
                is_4wd_only=is_4wd_only,
                has_gate_barrier=has_gate_barrier,
                access_status=access_status,
                primary_park_name=primary_park.name if primary_park else None,
                effective_grade=eff_grade,
                grade=eff_grade,
                score=peak_score,
                live_grade=live_summary.get("live_grade"),
                live_score=live_summary.get("live_score"),
                confidence_level=str(live_summary.get("confidence_level") or "none"),
                sample_count=int(live_summary.get("sample_count") or 0),
                last_driven_at=live_summary.get("last_driven_at"),
                avg_speed_kmh=live_summary.get("avg_speed_kmh"),
                est_duration_min=live_summary.get("est_duration_min"),
                vehicle_class_split=live_summary.get("vehicle_class_split") or {},
                live_breakdown=live_summary.get("live_breakdown"),
                rainfall_context=live_summary.get("rainfall_context"),
                obstacle_hotspots=live_summary.get("obstacle_hotspots") or [],
                bbox=_bbox_list(xmin, ymin, xmax, ymax),
                source_refs=merged_refs,
                baseline_breakdown=baseline_breakdown,
                elevation_profile=elevation_profile,
                waterway_crossings=waterway_crossings,
                segment_count=len(segments),
                is_active=trk.is_active,
                closure_status=c_status,
                active_closures=active_closures,
                suitability=None,
                is_favorite=False,
                primary_park=_park_to_summary(primary_park),
                capad_parks=capad_parks_list,
                segments=segments,
                nearby_pois=pois_by_track.get(tid, []),
                condition_reports=condition_reports,
                community_gate_warnings=community_gate_warnings,
            )
        )

    return details


@router.get("/segments/{segment_id}", response_model=SegmentDetailResponse)
def get_segment_detail(
    segment_id: str,
    db: Session = Depends(get_db),
) -> SegmentDetailResponse:
    """Return full canonical segment detail with explainable baseline/live grade & profile."""
    row = db.execute(
        select(
            TrackSegment,
            func.ST_AsGeoJSON(TrackSegment.geom).label("geom_json"),
            TrackSegmentGrade,
            CAPADPark,
        )
        .outerjoin(TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id)
        .outerjoin(CAPADPark, TrackSegment.capad_park_id == CAPADPark.id)
        .where(TrackSegment.segment_id == segment_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Segment not found")

    seg, geom_json, grd, park = row
    return _segment_to_detail(seg, geom_json, grd, park)


@router.get("/pois", response_model=list[PoiResponse])
def list_pois(
    poi_type: str | None = Query(default=None),
    state: str | None = Query(default=None),
    bbox: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[PoiResponse]:
    """List canonical deduplicated POIs (camps, huts, trigs, lookouts, gates)."""
    parsed_bbox = _parse_bbox(bbox)
    stmt = select(
        POI,
        func.ST_X(POI.geom).label("lon"),
        func.ST_Y(POI.geom).label("lat"),
        CAPADPark.name.label("park_name"),
    ).outerjoin(CAPADPark, POI.capad_park_id == CAPADPark.id)

    if poi_type:
        stmt = stmt.where(POI.poi_type == poi_type)
    if state:
        stmt = stmt.where(POI.state == state)
    if q:
        stmt = stmt.where(POI.name.ilike(f"%{_escape_like(q.strip())}%", escape="\\"))
    if parsed_bbox is not None:
        min_lon, min_lat, max_lon, max_lat = parsed_bbox
        env = func.ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat, 4326)
        stmt = stmt.where(func.ST_Intersects(POI.geom, env))

    stmt = stmt.order_by(POI.name.asc()).limit(limit)
    rows = db.execute(stmt).all()
    return [
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
            lon=float(lon),
            lat=float(lat),
        )
        for poi, lon, lat, park_name in rows
    ]


@router.get("/parks", response_model=list[ParkSummary])
def list_parks(
    state: str | None = Query(default=None),
    bbox: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[ParkSummary]:
    """List canonical CAPAD 2025 protected areas."""
    parsed_bbox = _parse_bbox(bbox)
    stmt = select(CAPADPark)
    if state:
        stmt = stmt.where(CAPADPark.state == state)
    if q:
        stmt = stmt.where(CAPADPark.name.ilike(f"%{_escape_like(q.strip())}%", escape="\\"))
    if parsed_bbox is not None:
        min_lon, min_lat, max_lon, max_lat = parsed_bbox
        env = func.ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat, 4326)
        stmt = stmt.where(func.ST_Intersects(CAPADPark.geom, env))
    stmt = stmt.order_by(CAPADPark.name.asc()).limit(limit)
    parks = db.execute(stmt).scalars().all()
    return [
        ParkSummary(
            id=p.id,
            capad_external_id=p.capad_external_id,
            name=p.name,
            park_type=p.park_type,
            type_abbr=p.type_abbr,
            iucn=p.iucn,
            state=p.state,
            area_ha=p.area_ha,
        )
        for p in parks
    ]


@router.get("/search", response_model=SearchResponse)
def search_network(
    q: str = Query(..., min_length=1, max_length=120),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> SearchResponse:
    """Search canonical tracks (name & aliases), POIs, and CAPAD parks with coordinates/bbox."""
    term = q.strip()
    if not term:
        return SearchResponse(query="", total=0, results=[])

    term_lower = term.lower()
    geocoder_enabled = bool(get_settings().enable_geocoder)
    cache_key = (_db_cache_scope(db), term_lower, limit, geocoder_enabled)
    cached = _search_cache_get(cache_key)
    if cached is not None:
        return cached

    escaped_term = _escape_like(term)
    pattern = f"%{escaped_term}%"
    prefix_pattern = f"{escaped_term}%"
    tokens = [_escape_like(tok) for tok in term.split() if tok][:6]

    def _token_match_track() -> Any:
        if len(tokens) <= 1:
            return Track.search_text.ilike(pattern, escape="\\")
        return and_(*(Track.search_text.ilike(f"%{tok}%", escape="\\") for tok in tokens))

    def _token_match_col(col: Any) -> Any:
        if len(tokens) <= 1:
            return col.ilike(pattern, escape="\\")
        return and_(*(col.ilike(f"%{tok}%", escape="\\") for tok in tokens))

    def _name_relevance_rank(col: Any) -> Any:
        return case(
            (func.lower(col) == term_lower, 0),
            (col.ilike(prefix_pattern, escape="\\"), 1),
            (col.ilike(pattern, escape="\\"), 2),
            else_=3,
        )

    # 1. Trigram-indexed track search reading cached Track.effective_grade directly
    relevance_rank = _name_relevance_rank(Track.name)
    class_rank = case(
        (Track.trail_class == "4wd_track", 0),
        (Track.trail_class == "4wd_route", 0),
        (Track.trail_class == "unsealed_road", 1),
        else_=2,
    )

    track_rows = db.execute(
        select(
            Track.track_id,
            Track.name,
            Track.state,
            Track.trail_class,
            Track.effective_grade,
            func.ST_XMin(Track.bbox_geom).label("xmin"),
            func.ST_YMin(Track.bbox_geom).label("ymin"),
            func.ST_XMax(Track.bbox_geom).label("xmax"),
            func.ST_YMax(Track.bbox_geom).label("ymax"),
        )
        .where(
            Track.is_active.is_(True),
            _token_match_track(),
        )
        .order_by(
            relevance_rank.asc(),
            class_rank.asc(),
            func.length(Track.name).asc(),
            Track.name.asc(),
        )
        .limit(limit)
    ).all()

    # 2. Fall back to track_segments/track_segment_grades ONLY if Track.effective_grade IS NULL
    missing_grade_ids = [r.track_id for r in track_rows if not r.effective_grade]
    fallback_rank_by_track: dict[str, int] = {}
    if missing_grade_ids:
        grade_rank_expr = case(
            (TrackSegmentGrade.effective_grade == "Easy", 1),
            (TrackSegmentGrade.effective_grade == "Medium", 2),
            (TrackSegmentGrade.effective_grade == "Hard", 3),
            (TrackSegmentGrade.effective_grade == "Extreme", 4),
            else_=0,
        )
        for tid, worst_rank in db.execute(
            select(
                TrackSegment.track_id,
                func.coalesce(func.max(grade_rank_expr), 0),
            )
            .outerjoin(TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id)
            .where(
                TrackSegment.is_active.is_(True),
                TrackSegment.track_id.in_(missing_grade_ids),
            )
            .group_by(TrackSegment.track_id)
        ).all():
            if tid is not None:
                fallback_rank_by_track[tid] = int(worst_rank or 0)

    track_items: list[SearchResultItem] = []
    for tid, name, state, trail_class, cached_grade, xmin, ymin, xmax, ymax in track_rows:
        eff_grade = cached_grade
        if not eff_grade:
            worst_rank = fallback_rank_by_track.get(tid, 0)
            eff_grade = _RANK_GRADE.get(worst_rank) if worst_rank else None
        lon, lat = _bbox_midpoint(xmin, ymin, xmax, ymax)
        track_items.append(
            SearchResultItem(
                kind="track",
                id=tid,
                name=name,
                state=state,
                subtitle=trail_class.replace("_", " "),
                category=trail_class,
                grade=eff_grade,
                lon=lon,
                lat=lat,
                bbox=_bbox_list(xmin, ymin, xmax, ymax),
            )
        )

    # 3. Budgeted trigram-indexed POI lookup (capped to avoid fetching & discarding extras)
    poi_limit = min(limit, _SEARCH_POI_SUBQUERY_CAP)
    poi_rows = db.execute(
        select(
            POI.id,
            POI.name,
            POI.state,
            POI.poi_type,
            func.ST_X(POI.geom).label("lon"),
            func.ST_Y(POI.geom).label("lat"),
        )
        .where(_token_match_col(POI.name))
        .order_by(
            _name_relevance_rank(POI.name).asc(),
            func.length(POI.name).asc(),
            POI.name.asc(),
        )
        .limit(poi_limit)
    ).all()
    poi_items = [
        SearchResultItem(
            kind="poi",
            id=str(pid),
            name=name,
            state=state,
            subtitle=poi_type,
            category=poi_type,
            grade=None,
            lon=round(float(lon), 6),
            lat=round(float(lat), 6),
            bbox=None,
        )
        for pid, name, state, poi_type, lon, lat in poi_rows
    ]

    # 4. Budgeted trigram-indexed CAPAD park lookup using bbox midpoint (zero ST_PointOnSurface /
    #    zero MultiPolygon TOAST decompression!)
    park_limit = min(limit, _SEARCH_PARK_SUBQUERY_CAP)
    park_rows = db.execute(
        select(
            CAPADPark.id,
            CAPADPark.name,
            CAPADPark.state,
            CAPADPark.park_type,
            func.ST_XMin(CAPADPark.geom).label("xmin"),
            func.ST_YMin(CAPADPark.geom).label("ymin"),
            func.ST_XMax(CAPADPark.geom).label("xmax"),
            func.ST_YMax(CAPADPark.geom).label("ymax"),
        )
        .where(_token_match_col(CAPADPark.name))
        .order_by(
            _name_relevance_rank(CAPADPark.name).asc(),
            func.length(CAPADPark.name).asc(),
            CAPADPark.name.asc(),
        )
        .limit(park_limit)
    ).all()
    park_items: list[SearchResultItem] = []
    for pk_id, name, state, park_type, xmin, ymin, xmax, ymax in park_rows:
        lon, lat = _bbox_midpoint(xmin, ymin, xmax, ymax)
        park_items.append(
            SearchResultItem(
                kind="park",
                id=str(pk_id),
                name=name,
                state=state,
                subtitle=park_type,
                category=park_type,
                grade=None,
                lon=lon,
                lat=lat,
                bbox=_bbox_list(xmin, ymin, xmax, ymax),
            )
        )

    items = _merge_search_results(
        track_items,
        poi_items,
        park_items,
        term=term,
        limit=limit,
    )

    # Fall back to external AU Nominatim geocoding ONLY when no local track/POI/park matched
    # so searching baked tracks returns fast without blocking on external HTTP calls.
    if not items and len(term) >= 3 and geocoder_enabled:
        seen_names = {item.name.strip().lower() for item in items}
        remaining = max(1, min(5, limit))
        for place in geocode_au_places(term, limit=remaining):
            if place.name.strip().lower() in seen_names:
                continue
            seen_names.add(place.name.strip().lower())
            items.append(
                SearchResultItem(
                    kind="place",
                    id=place.id,
                    name=place.name,
                    state=place.state,
                    subtitle=place.subtitle,
                    category=place.category,
                    grade=None,
                    lon=place.lon,
                    lat=place.lat,
                    bbox=place.bbox,
                )
            )

    trimmed = items[:limit]
    response = SearchResponse(query=term, total=len(trimmed), results=trimmed)
    _search_cache_put(cache_key, response)
    return response
