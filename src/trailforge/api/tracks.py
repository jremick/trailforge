"""Read-only REST API for canonical tracks, graded segments, POIs, CAPAD parks, and search."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from trailforge.db.models import POI, CAPADPark, Track, TrackSegment, TrackSegmentGrade
from trailforge.db.session import get_db

router = APIRouter(prefix="/api", tags=["tracks"])

_GRADE_RANK = {"Easy": 1, "Medium": 2, "Hard": 3, "Extreme": 4}
_RANK_GRADE = {v: k for k, v in _GRADE_RANK.items()}


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
    segment_count: int = 0
    is_active: bool


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


class TrackDetailResponse(TrackSummaryResponse):
    primary_park: ParkSummary | None = None
    segments: list[SegmentDetailResponse] = Field(default_factory=list)
    nearby_pois: list[PoiResponse] = Field(default_factory=list)


class SearchResultItem(BaseModel):
    kind: str
    id: str
    name: str
    state: str
    subtitle: str | None = None
    lon: float | None = None
    lat: float | None = None


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


@router.get("/tracks", response_model=list[TrackSummaryResponse])
def list_tracks(
    state: str | None = Query(default=None),
    grade: str | None = Query(default=None),
    trail_class: str | None = Query(default=None),
    bbox: str | None = Query(default=None),
    q: str | None = Query(default=None),
    include_inactive: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[TrackSummaryResponse]:
    """List canonical tracks with optional spatial bbox, state, grade, and name filters."""
    parsed_bbox = _parse_bbox(bbox)

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

    return [
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
            effective_grade=_RANK_GRADE.get(int(worst_rank)) if worst_rank else None,
            segment_count=int(segment_count),
            is_active=trk.is_active,
        )
        for trk, park_name, segment_count, worst_rank in rows
    ]


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
                    "segment_seq": seg.segment_seq,
                    "name": seg.name,
                    "state": seg.state,
                    "trail_class": seg.trail_class,
                    "surface_class": seg.surface_class,
                    "tracktype": seg.tracktype,
                    "length_m": seg.length_m,
                    "elev_min_m": seg.elev_min_m,
                    "elev_max_m": seg.elev_max_m,
                    "max_slope_deg": seg.max_slope_deg,
                    "sustained_slope_deg": seg.sustained_slope_deg,
                    "is_steep_highlight": seg.is_steep_highlight,
                    "is_above_snowline": seg.is_above_snowline,
                    "waterway_crossing_count": seg.waterway_crossing_count,
                    "has_perennial_crossing": seg.has_perennial_crossing,
                    "access_status": seg.access_status,
                    "has_gate_barrier": seg.has_gate_barrier,
                    "primary_source": seg.primary_source,
                    "has_osm_source": seg.has_osm_source,
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
    db: Session = Depends(get_db),
) -> TrackDetailResponse:
    """Return full track metadata, ordered graded segments, CAPAD tenure, and nearby POIs."""
    row = db.execute(
        select(Track, CAPADPark)
        .outerjoin(CAPADPark, Track.primary_park_id == CAPADPark.id)
        .where(Track.track_id == track_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found")

    trk, primary_park = row

    seg_rows = db.execute(
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

    segments: list[SegmentDetailResponse] = []
    seg_ids: list[str] = []
    worst = 0
    for seg, geom_json, grd, park in seg_rows:
        seg_ids.append(seg.segment_id)
        if grd and grd.effective_grade in _GRADE_RANK:
            worst = max(worst, _GRADE_RANK[grd.effective_grade])
        segments.append(_segment_to_detail(seg, geom_json, grd, park))

    nearby_pois: list[PoiResponse] = []
    if seg_ids:
        poi_rows = db.execute(
            select(
                POI,
                func.ST_X(POI.geom).label("lon"),
                func.ST_Y(POI.geom).label("lat"),
                CAPADPark.name.label("park_name"),
            )
            .outerjoin(CAPADPark, POI.capad_park_id == CAPADPark.id)
            .where(POI.nearest_segment_id.in_(seg_ids))
            .order_by(POI.name.asc())
        ).all()
        for poi, lon, lat, park_name in poi_rows:
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
                    lon=float(lon),
                    lat=float(lat),
                )
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
        elev_gain_m=trk.elev_gain_m,
        max_slope_deg=trk.max_slope_deg,
        waterway_crossing_count=trk.waterway_crossing_count,
        primary_park_name=primary_park.name if primary_park else None,
        effective_grade=_RANK_GRADE.get(worst) if worst else None,
        segment_count=len(segments),
        is_active=trk.is_active,
        primary_park=_park_to_summary(primary_park),
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
    """Search canonical tracks (name & aliases), POIs, and CAPAD parks."""
    term = q.strip()
    pattern = f"%{term}%"
    items: list[SearchResultItem] = []

    track_rows = db.execute(
        select(
            Track.track_id,
            Track.name,
            Track.state,
            Track.trail_class,
            func.ST_X(func.ST_Centroid(Track.bbox_geom)).label("lon"),
            func.ST_Y(func.ST_Centroid(Track.bbox_geom)).label("lat"),
        )
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
    for tid, name, state, trail_class, lon, lat in track_rows:
        items.append(
            SearchResultItem(
                kind="track",
                id=tid,
                name=name,
                state=state,
                subtitle=trail_class.replace("_", " "),
                lon=float(lon) if lon is not None else None,
                lat=float(lat) if lat is not None else None,
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
                lon=float(lon),
                lat=float(lat),
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
        )
        .where(CAPADPark.name.ilike(pattern))
        .order_by(CAPADPark.name.asc())
        .limit(limit)
    ).all()
    for pk_id, name, state, park_type, lon, lat in park_rows:
        items.append(
            SearchResultItem(
                kind="park",
                id=str(pk_id),
                name=name,
                state=state,
                subtitle=park_type,
                lon=float(lon),
                lat=float(lat),
            )
        )

    trimmed = items[:limit]
    return SearchResponse(query=term, total=len(trimmed), results=trimmed)
