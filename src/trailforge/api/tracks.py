"""Read-only REST API for canonical tracks, graded segments, POIs, CAPAD parks, and search."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from trailforge.auth.dependencies import get_optional_current_user
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
from trailforge.vehicles.classifier import (
    CapabilityClassLiteral,
    TrackSuitability,
    evaluate_track_suitability,
    normalize_capability_class,
)

router = APIRouter(prefix="/api", tags=["tracks"])

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
    if min_lon >= max_lon or min_lat >= max_lat:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="bbox min coordinates must be strictly less than max coordinates",
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
    elevation_profile: list[ElevationProfilePoint] = Field(default_factory=list)
    waterway_crossings: list[WaterwayCrossingItem] = Field(default_factory=list)
    primary_park: ParkSummary | None = None
    capad_parks: list[ParkSummary] = Field(default_factory=list)
    segments: list[SegmentDetailResponse] = Field(default_factory=list)
    nearby_pois: list[PoiResponse] = Field(default_factory=list)


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
    seg_agg = (
        select(
            TrackSegment.track_id.label("track_id"),
            func.count(TrackSegment.segment_id).label("segment_count"),
            func.max(grade_rank_expr).label("worst_rank"),
            func.max(TrackSegmentGrade.baseline_score).label("max_score"),
            func.bool_or(TrackSegment.is_4wd_only).label("any_4wd_only"),
            func.bool_or(TrackSegment.has_perennial_crossing).label("any_perennial"),
        )
        .outerjoin(TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id)
        .where(TrackSegment.is_active.is_(True))
        .group_by(TrackSegment.track_id)
        .subquery("seg_agg")
    )

    stmt = (
        select(
            Track,
            CAPADPark.name.label("park_name"),
            func.coalesce(seg_agg.c.segment_count, 0).label("segment_count"),
            func.coalesce(seg_agg.c.worst_rank, 0).label("worst_rank"),
            seg_agg.c.max_score.label("max_score"),
            func.coalesce(seg_agg.c.any_4wd_only, False).label("any_4wd_only"),
            func.coalesce(seg_agg.c.any_perennial, False).label("any_perennial"),
            func.ST_XMin(Track.bbox_geom).label("xmin"),
            func.ST_YMin(Track.bbox_geom).label("ymin"),
            func.ST_XMax(Track.bbox_geom).label("xmax"),
            func.ST_YMax(Track.bbox_geom).label("ymax"),
        )
        .outerjoin(CAPADPark, Track.primary_park_id == CAPADPark.id)
        .outerjoin(seg_agg, Track.track_id == seg_agg.c.track_id)
    )
    if not include_inactive:
        stmt = stmt.where(Track.is_active.is_(True))
    if state:
        stmt = stmt.where(or_(Track.state == state, Track.state.ilike(f"%{state}%")))
    if trail_class:
        stmt = stmt.where(Track.trail_class == trail_class)
    if grade:
        target_rank = _GRADE_RANK.get(grade)
        if target_rank is None:
            return []
        stmt = stmt.where(seg_agg.c.worst_rank == target_rank)
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Track.name.ilike(pattern),
                func.array_to_string(Track.alt_names, " ").ilike(pattern),
            )
        )
    if parsed_bbox is not None:
        min_lon, min_lat, max_lon, max_lat = parsed_bbox
        env = func.ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat, 4326)
        stmt = stmt.where(func.ST_Intersects(Track.bbox_geom, env))

    stmt = stmt.order_by(Track.name.asc()).offset(offset).limit(limit)
    rows = db.execute(stmt).all()

    track_ids = [r[0].track_id for r in rows]
    closures_map = _load_closures_by_track(db, track_ids)
    fav_ids = _get_favorite_track_ids(db, current_user, track_ids)

    results: list[TrackSummaryResponse] = []
    for (
        trk,
        park_name,
        segment_count,
        worst_rank,
        max_score,
        any_4wd_only,
        any_perennial,
        xmin,
        ymin,
        xmax,
        ymax,
    ) in rows:
        eff_grade = _RANK_GRADE.get(int(worst_rank)) if worst_rank else None
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
    bbox: str | None = Query(default=None),
    state: str | None = Query(default=None),
    grade: str | None = Query(default=None),
    limit: int = Query(default=2000, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return active canonical graded track segments as a GeoJSON FeatureCollection."""
    parsed_bbox = _parse_bbox(bbox)
    stmt = (
        select(
            TrackSegment,
            func.ST_AsGeoJSON(TrackSegment.geom).label("geom_json"),
            TrackSegmentGrade,
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

    features: list[dict[str, Any]] = []
    for seg, geom_json, grd in rows:
        features.append(
            {
                "type": "Feature",
                "id": seg.segment_id,
                "geometry": json.loads(geom_json),
                "properties": {
                    "segment_id": seg.segment_id,
                    "track_id": seg.track_id,
                    "track_name": seg.name,
                    "segment_seq": seg.segment_seq,
                    "name": seg.name,
                    "state": seg.state,
                    "trail_class": seg.trail_class,
                    "surface_class": seg.surface_class,
                    "tracktype": seg.tracktype,
                    "length_m": seg.length_m,
                    "min_elev_m": seg.elev_min_m,
                    "max_elev_m": seg.elev_max_m,
                    "elev_min_m": seg.elev_min_m,
                    "elev_max_m": seg.elev_max_m,
                    "max_slope_deg": seg.max_slope_deg,
                    "sustained_slope_deg": seg.sustained_slope_deg,
                    "is_steep_highlight": seg.is_steep_highlight,
                    "is_above_snowline": seg.is_above_snowline,
                    "waterway_crossing_count": seg.waterway_crossing_count,
                    "has_perennial_crossing": seg.has_perennial_crossing,
                    "is_4wd_only": seg.is_4wd_only,
                    "access_status": seg.access_status,
                    "has_gate_barrier": seg.has_gate_barrier,
                    "primary_source": seg.primary_source,
                    "has_osm_source": seg.has_osm_source,
                    "grade": grd.effective_grade if grd else None,
                    "score": grd.baseline_score if grd else None,
                    "baseline_grade": grd.baseline_grade if grd else None,
                    "baseline_score": grd.baseline_score if grd else None,
                    "effective_grade": grd.effective_grade if grd else None,
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


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

    # Intersecting CAPAD parks via PostGIS (covers border-crossing tracks cleanly)
    if seg_rows:
        track_union_sq = (
            select(func.ST_Union(TrackSegment.geom).label("geom"))
            .where(TrackSegment.track_id == track_id, TrackSegment.is_active.is_(True))
            .subquery("track_union")
        )
        park_rows = db.execute(
            select(CAPADPark)
            .join(track_union_sq, func.ST_Intersects(CAPADPark.geom, track_union_sq.c.geom))
            .order_by(CAPADPark.name.asc())
        ).scalars().all()
        for pk in park_rows:
            parks_by_id.setdefault(pk.id, pk)

        # Nearby POIs within 1,500 m sorted by metric distance in EPSG:3577
        dist_expr = func.ST_Distance(
            func.ST_Transform(POI.geom, 3577),
            func.ST_Transform(track_union_sq.c.geom, 3577),
        ).label("distance_m")
        poi_rows = db.execute(
            select(
                POI,
                func.ST_X(POI.geom).label("lon"),
                func.ST_Y(POI.geom).label("lat"),
                CAPADPark.name.label("park_name"),
                dist_expr,
            )
            .join(
                track_union_sq,
                func.ST_DWithin(
                    func.ST_Transform(POI.geom, 3577),
                    func.ST_Transform(track_union_sq.c.geom, 3577),
                    _NEARBY_POI_RADIUS_M,
                ),
            )
            .outerjoin(CAPADPark, POI.capad_park_id == CAPADPark.id)
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
    else:
        nearby_pois = []

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
    )


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
        stmt = stmt.where(POI.name.ilike(f"%{q.strip()}%"))
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
        stmt = stmt.where(CAPADPark.name.ilike(f"%{q.strip()}%"))
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
    pattern = f"%{term}%"
    items: list[SearchResultItem] = []

    grade_rank_expr = case(
        (TrackSegmentGrade.effective_grade == "Easy", 1),
        (TrackSegmentGrade.effective_grade == "Medium", 2),
        (TrackSegmentGrade.effective_grade == "Hard", 3),
        (TrackSegmentGrade.effective_grade == "Extreme", 4),
        else_=0,
    )
    seg_agg = (
        select(
            TrackSegment.track_id.label("track_id"),
            func.max(grade_rank_expr).label("worst_rank"),
        )
        .outerjoin(TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id)
        .where(TrackSegment.is_active.is_(True))
        .group_by(TrackSegment.track_id)
        .subquery("search_seg_agg")
    )

    track_rows = db.execute(
        select(
            Track.track_id,
            Track.name,
            Track.state,
            Track.trail_class,
            func.coalesce(seg_agg.c.worst_rank, 0).label("worst_rank"),
            func.ST_X(func.ST_Centroid(Track.bbox_geom)).label("lon"),
            func.ST_Y(func.ST_Centroid(Track.bbox_geom)).label("lat"),
            func.ST_XMin(Track.bbox_geom).label("xmin"),
            func.ST_YMin(Track.bbox_geom).label("ymin"),
            func.ST_XMax(Track.bbox_geom).label("xmax"),
            func.ST_YMax(Track.bbox_geom).label("ymax"),
        )
        .outerjoin(seg_agg, Track.track_id == seg_agg.c.track_id)
        .where(
            Track.is_active.is_(True),
            or_(
                Track.name.ilike(pattern),
                func.array_to_string(Track.alt_names, " ").ilike(pattern),
            ),
        )
        .order_by(Track.name.asc())
        .limit(limit)
    ).all()
    for tid, name, state, trail_class, worst_rank, lon, lat, xmin, ymin, xmax, ymax in track_rows:
        eff_grade = _RANK_GRADE.get(int(worst_rank)) if worst_rank else None
        items.append(
            SearchResultItem(
                kind="track",
                id=tid,
                name=name,
                state=state,
                subtitle=trail_class.replace("_", " "),
                category=trail_class,
                grade=eff_grade,
                lon=round(float(lon), 6) if lon is not None else None,
                lat=round(float(lat), 6) if lat is not None else None,
                bbox=_bbox_list(xmin, ymin, xmax, ymax),
            )
        )

    poi_rows = db.execute(
        select(
            POI.id,
            POI.name,
            POI.state,
            POI.poi_type,
            func.ST_X(POI.geom).label("lon"),
            func.ST_Y(POI.geom).label("lat"),
        )
        .where(POI.name.ilike(pattern))
        .order_by(POI.name.asc())
        .limit(limit)
    ).all()
    for pid, name, state, poi_type, lon, lat in poi_rows:
        items.append(
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
        )

    park_rows = db.execute(
        select(
            CAPADPark.id,
            CAPADPark.name,
            CAPADPark.state,
            CAPADPark.park_type,
            func.ST_X(func.ST_PointOnSurface(CAPADPark.geom)).label("lon"),
            func.ST_Y(func.ST_PointOnSurface(CAPADPark.geom)).label("lat"),
            func.ST_XMin(CAPADPark.geom).label("xmin"),
            func.ST_YMin(CAPADPark.geom).label("ymin"),
            func.ST_XMax(CAPADPark.geom).label("xmax"),
            func.ST_YMax(CAPADPark.geom).label("ymax"),
        )
        .where(CAPADPark.name.ilike(pattern))
        .order_by(CAPADPark.name.asc())
        .limit(limit)
    ).all()
    for pk_id, name, state, park_type, lon, lat, xmin, ymin, xmax, ymax in park_rows:
        items.append(
            SearchResultItem(
                kind="park",
                id=str(pk_id),
                name=name,
                state=state,
                subtitle=park_type,
                category=park_type,
                grade=None,
                lon=round(float(lon), 6),
                lat=round(float(lat), 6),
                bbox=_bbox_list(xmin, ymin, xmax, ymax),
            )
        )

    trimmed = items[:limit]
    return SearchResponse(query=term, total=len(trimmed), results=trimmed)
