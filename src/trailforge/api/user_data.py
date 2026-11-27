"""User favorites, personal pins CRUD, and GridForge-compatible GPX 1.1 / GeoJSON exports."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from geoalchemy2.shape import from_shape
from pydantic import AliasChoices, BaseModel, Field, StringConstraints
from shapely.geometry import Point
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trailforge.auth.dependencies import (
    get_current_user,
    get_scoped_read_user,
    get_scoped_write_user,
    require_browser_queue_write_owner,
)
from trailforge.db.models import (
    POI,
    Track,
    TrackSegment,
    TrackSegmentGrade,
    User,
    UserFavorite,
    UserPin,
)
from trailforge.db.session import get_db
from trailforge.export.formats import (
    ExportPin,
    ExportTrackData,
    build_gpx_document,
    build_track_geojson,
    build_user_collection_geojson,
)

router = APIRouter(prefix="/api", tags=["user-data"])

PinCategoryLiteral = Literal["waypoint", "camp", "hazard", "photo", "water", "recovery"]
ExportFormatLiteral = Literal["gpx", "geojson"]

_GRADE_RANK = {"Easy": 1, "Medium": 2, "Hard": 3, "Extreme": 4}
_RANK_GRADE = {v: k for k, v in _GRADE_RANK.items()}
_FILENAME_SLUG_RE = re.compile(r"[^a-z0-9]+")
_EXPORT_POI_RADIUS_M = 1500.0


def _slug_filename(name: str) -> str:
    slug = _FILENAME_SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "trailforge-export"


_NonEmptyTrackId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]
_NonEmptyPinName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]


class FavoriteCreateRequest(BaseModel):
    track_id: _NonEmptyTrackId
    notes: str | None = Field(default=None, max_length=1000)


class FavoriteResponse(BaseModel):
    id: int
    track_id: str
    track_name: str
    state: str
    grade: str | None = None
    total_length_m: float
    notes: str | None = None
    created_at: datetime


class PinCreateRequest(BaseModel):
    name: _NonEmptyPinName = Field(
        ...,
        validation_alias=AliasChoices("name", "title"),
    )
    category: PinCategoryLiteral = "waypoint"
    notes: str | None = Field(default=None, max_length=1000)
    lon: float = Field(..., ge=-180.0, le=180.0)
    lat: float = Field(..., ge=-90.0, le=90.0)
    elev_m: float | None = Field(default=None, ge=-500.0, le=5000.0)
    track_id: str | None = None
    client_uuid: uuid.UUID | None = None


class PinUpdateRequest(BaseModel):
    name: _NonEmptyPinName | None = Field(
        default=None,
        validation_alias=AliasChoices("name", "title"),
    )
    category: PinCategoryLiteral | None = None
    notes: str | None = Field(default=None, max_length=1000)
    lon: float | None = Field(default=None, ge=-180.0, le=180.0)
    lat: float | None = Field(default=None, ge=-90.0, le=90.0)
    elev_m: float | None = Field(default=None, ge=-500.0, le=5000.0)


class PinResponse(BaseModel):
    id: str
    name: str
    title: str
    category: PinCategoryLiteral
    notes: str | None = None
    lon: float
    lat: float
    elev_m: float | None = None
    created_at: datetime
    updated_at: datetime


def _assemble_export_track_from_segments(
    track: Track,
    seg_rows: list[tuple[TrackSegment, str, str | None]],
    *,
    notes: str | None = None,
) -> ExportTrackData:
    """Stitch ordered segment coordinates & grade into an ``ExportTrackData``."""
    stitched_coords: list[tuple[float, float, float | None]] = []
    worst_rank = 0
    curr_exit: str | None = None

    for idx, (seg, geom_json, eff_grade) in enumerate(seg_rows):
        if eff_grade and eff_grade in _GRADE_RANK:
            worst_rank = max(worst_rank, _GRADE_RANK[eff_grade])

        reverse_seg = False
        if idx == 0:
            if len(seg_rows) >= 2:
                nxt = seg_rows[1][0]
                nxt_nodes = {nxt.start_node_key, nxt.end_node_key}
                if seg.end_node_key in nxt_nodes:
                    curr_exit = seg.end_node_key
                elif seg.start_node_key in nxt_nodes:
                    reverse_seg = True
                    curr_exit = seg.start_node_key
                else:
                    curr_exit = seg.end_node_key
            else:
                curr_exit = seg.end_node_key
        else:
            if seg.start_node_key == curr_exit:
                curr_exit = seg.end_node_key
            elif seg.end_node_key == curr_exit:
                reverse_seg = True
                curr_exit = seg.start_node_key
            else:
                curr_exit = seg.end_node_key

        gj = json.loads(geom_json)
        raw_coords = list(gj.get("coordinates") or [])
        if reverse_seg:
            raw_coords.reverse()

        start_ele = seg.elev_end_m if reverse_seg else seg.elev_start_m
        end_ele = seg.elev_start_m if reverse_seg else seg.elev_end_m
        n_pts = len(raw_coords)
        for p_idx, pair in enumerate(raw_coords):
            if idx > 0 and p_idx == 0 and stitched_coords:
                continue
            lon, lat = float(pair[0]), float(pair[1])
            ele: float | None = None
            if start_ele is not None and end_ele is not None and n_pts >= 2:
                frac = p_idx / (n_pts - 1)
                ele = round(float(start_ele) + (float(end_ele) - float(start_ele)) * frac, 2)
            elif seg.elev_min_m is not None:
                ele = round(float(seg.elev_min_m), 2)
            stitched_coords.append((lon, lat, ele))

    grade = _RANK_GRADE.get(worst_rank) if worst_rank else None
    return ExportTrackData(
        track_id=track.track_id,
        name=track.name,
        state=track.state,
        grade=grade,
        total_length_m=float(track.total_length_m or 0.0),
        coords_lonlat_ele=stitched_coords,
        has_osm_source=bool(track.has_osm_source),
        notes=notes,
        extra_props={
            "trail_class": track.trail_class,
            "elev_min_m": track.elev_min_m,
            "elev_max_m": track.elev_max_m,
            "max_slope_deg": track.max_slope_deg,
        },
    )


def _build_export_tracks_batch(
    db: Session,
    fav_rows: list[tuple[UserFavorite, Track]],
) -> list[ExportTrackData]:
    """Batch-load segments for all favorited tracks in one SQL query (zero unused POI joins)."""
    if not fav_rows:
        return []
    unique_tids = list(dict.fromkeys(trk.track_id for _, trk in fav_rows))
    raw_segs = db.execute(
        select(
            TrackSegment,
            func.ST_AsGeoJSON(TrackSegment.geom).label("geom_json"),
            TrackSegmentGrade.effective_grade,
        )
        .outerjoin(TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id)
        .where(TrackSegment.track_id.in_(unique_tids), TrackSegment.is_active.is_(True))
        .order_by(
            TrackSegment.track_id.asc(),
            TrackSegment.segment_seq.asc().nullslast(),
            TrackSegment.segment_id.asc(),
        )
    ).all()
    segs_by_track: dict[str, list[tuple[TrackSegment, str, str | None]]] = {
        tid: [] for tid in unique_tids
    }
    for seg, geom_json, eff_grade in raw_segs:
        if seg.track_id in segs_by_track:
            segs_by_track[seg.track_id].append((seg, geom_json, eff_grade))

    return [
        _assemble_export_track_from_segments(
            trk, segs_by_track.get(trk.track_id, []), notes=fav.notes
        )
        for fav, trk in fav_rows
    ]


def _build_export_track_data(
    db: Session, track: Track, *, notes: str | None = None
) -> tuple[ExportTrackData, list[ExportPin]]:
    """Assemble stitched coordinates (lon, lat, elev) & nearby POIs for GPX/GeoJSON export."""
    seg_rows = [
        (r[0], r[1], r[2])
        for r in db.execute(
            select(
                TrackSegment,
                func.ST_AsGeoJSON(TrackSegment.geom).label("geom_json"),
                TrackSegmentGrade.effective_grade,
            )
            .outerjoin(TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id)
            .where(TrackSegment.track_id == track.track_id, TrackSegment.is_active.is_(True))
            .order_by(TrackSegment.segment_seq.asc().nullslast(), TrackSegment.segment_id.asc())
        ).all()
    ]
    export_track = _assemble_export_track_from_segments(track, seg_rows, notes=notes)

    nearby_pins: list[ExportPin] = []
    if seg_rows:
        track_union_sq = (
            select(func.ST_Union(TrackSegment.geom).label("geom"))
            .where(TrackSegment.track_id == track.track_id, TrackSegment.is_active.is_(True))
            .subquery("export_trk_union")
        )
        poi_rows = db.execute(
            select(
                POI.name,
                POI.poi_type,
                POI.elev_m,
                POI.access_note,
                func.ST_X(POI.geom).label("lon"),
                func.ST_Y(POI.geom).label("lat"),
            )
            .join(
                track_union_sq,
                func.ST_DWithin(
                    func.ST_Transform(POI.geom, 3577),
                    func.ST_Transform(track_union_sq.c.geom, 3577),
                    _EXPORT_POI_RADIUS_M,
                ),
            )
            .where(func.ST_DWithin(POI.geom, track_union_sq.c.geom, 0.025))
            .order_by(POI.name.asc())
            .limit(25)
        ).all()
        for pname, ptype, pelev, pnote, plon, plat in poi_rows:
            nearby_pins.append(
                ExportPin(
                    name=pname,
                    lon=float(plon),
                    lat=float(plat),
                    category=ptype,
                    elev_m=float(pelev) if pelev is not None else None,
                    notes=pnote,
                    source="poi",
                )
            )

    return export_track, nearby_pins


# -----------------------------------------------------------------------------
# Favorites CRUD (Authenticated)
# -----------------------------------------------------------------------------


@router.get("/favorites", response_model=list[FavoriteResponse])
def list_favorites(
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_scoped_read_user),
    db: Session = Depends(get_db),
) -> list[FavoriteResponse]:
    """List the signed-in user's saved favorite tracks."""
    fav_rows = db.execute(
        select(UserFavorite, Track)
        .join(Track, UserFavorite.track_id == Track.track_id)
        .where(UserFavorite.user_id == user.id, UserFavorite.track_id.is_not(None))
        .order_by(UserFavorite.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()

    rank_by_track: dict[str, int] = {}
    if fav_rows:
        fav_track_ids = list({trk.track_id for _, trk in fav_rows})
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
                TrackSegment.track_id.in_(fav_track_ids),
            )
            .group_by(TrackSegment.track_id)
        ).all():
            if tid is not None:
                rank_by_track[tid] = int(worst_rank or 0)

    return [
        FavoriteResponse(
            id=fav.id,
            track_id=trk.track_id,
            track_name=trk.name,
            state=trk.state,
            grade=_RANK_GRADE.get(rank_by_track.get(trk.track_id, 0))
            if rank_by_track.get(trk.track_id, 0)
            else None,
            total_length_m=float(trk.total_length_m or 0.0),
            notes=fav.notes,
            created_at=fav.created_at,
        )
        for fav, trk in fav_rows
    ]


@router.post("/favorites", response_model=FavoriteResponse, status_code=status.HTTP_201_CREATED)
def add_favorite(
    payload: FavoriteCreateRequest,
    user: User = Depends(get_scoped_write_user),
    db: Session = Depends(get_db),
) -> FavoriteResponse:
    """Save a canonical track to the signed-in user's favorites (idempotent)."""
    trk = db.get(Track, payload.track_id)
    if trk is None or not trk.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found")

    existing = db.execute(
        select(UserFavorite)
        .where(
            UserFavorite.user_id == user.id,
            UserFavorite.track_id == payload.track_id,
            UserFavorite.segment_id.is_(None),
        )
        .limit(1)
    ).scalar_one_or_none()

    if existing is not None:
        if payload.notes is not None:
            existing.notes = payload.notes.strip() or None
            db.commit()
            db.refresh(existing)
        fav = existing
    else:
        fav = UserFavorite(
            user_id=user.id,
            track_id=payload.track_id,
            segment_id=None,
            notes=payload.notes.strip() if payload.notes else None,
        )
        db.add(fav)
        db.commit()
        db.refresh(fav)

    worst_rank = db.execute(
        select(
            func.max(
                case(
                    (TrackSegmentGrade.effective_grade == "Easy", 1),
                    (TrackSegmentGrade.effective_grade == "Medium", 2),
                    (TrackSegmentGrade.effective_grade == "Hard", 3),
                    (TrackSegmentGrade.effective_grade == "Extreme", 4),
                    else_=0,
                )
            )
        )
        .select_from(TrackSegment)
        .outerjoin(TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id)
        .where(TrackSegment.track_id == trk.track_id, TrackSegment.is_active.is_(True))
    ).scalar_one_or_none()

    return FavoriteResponse(
        id=fav.id,
        track_id=trk.track_id,
        track_name=trk.name,
        state=trk.state,
        grade=_RANK_GRADE.get(int(worst_rank)) if worst_rank else None,
        total_length_m=float(trk.total_length_m or 0.0),
        notes=fav.notes,
        created_at=fav.created_at,
    )


@router.delete("/favorites/{track_id}")
def remove_favorite(
    track_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str | bool]:
    """Remove a saved favorite track for the signed-in user."""
    fav = db.execute(
        select(UserFavorite)
        .where(
            UserFavorite.user_id == user.id,
            UserFavorite.track_id == track_id,
            UserFavorite.segment_id.is_(None),
        )
        .limit(1)
    ).scalar_one_or_none()
    if fav is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Favorite not found")
    db.delete(fav)
    db.commit()
    return {"ok": True, "track_id": track_id}


# -----------------------------------------------------------------------------
# Personal Pins CRUD (Authenticated)
# -----------------------------------------------------------------------------


def _pin_to_response(pin: UserPin, lon: float, lat: float) -> PinResponse:
    return PinResponse(
        id=str(pin.id),
        name=pin.title,
        title=pin.title,
        category=pin.category,  # type: ignore[arg-type]
        notes=pin.notes,
        lon=round(float(lon), 6),
        lat=round(float(lat), 6),
        elev_m=pin.elev_m,
        created_at=pin.created_at,
        updated_at=pin.updated_at,
    )


@router.get("/pins", response_model=list[PinResponse])
def list_pins(
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_scoped_read_user),
    db: Session = Depends(get_db),
) -> list[PinResponse]:
    """List the signed-in user's personal map pins."""
    rows = db.execute(
        select(
            UserPin,
            func.ST_X(UserPin.geom).label("lon"),
            func.ST_Y(UserPin.geom).label("lat"),
        )
        .where(UserPin.user_id == user.id)
        .order_by(UserPin.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [_pin_to_response(pin, lon, lat) for pin, lon, lat in rows]


@router.post("/pins", response_model=PinResponse, status_code=status.HTTP_201_CREATED)
def create_pin(
    payload: PinCreateRequest,
    user: User = Depends(require_browser_queue_write_owner),
    db: Session = Depends(get_db),
) -> PinResponse:
    """Create a personal waypoint/camp/hazard pin persisted in PostGIS."""
    if payload.client_uuid is not None:
        existing = db.execute(
            select(
                UserPin,
                func.ST_X(UserPin.geom).label("lon"),
                func.ST_Y(UserPin.geom).label("lat"),
            )
            .where(UserPin.user_id == user.id, UserPin.client_uuid == payload.client_uuid)
            .limit(1)
        ).first()
        if existing is not None:
            return _pin_to_response(existing[0], existing[1], existing[2])

    pin = UserPin(
        user_id=user.id,
        client_uuid=payload.client_uuid,
        title=payload.name.strip(),
        category=payload.category,
        notes=payload.notes.strip() if payload.notes else None,
        elev_m=payload.elev_m,
        geom=from_shape(Point(payload.lon, payload.lat), srid=4326),
    )
    db.add(pin)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if payload.client_uuid is not None:
            existing = db.execute(
                select(
                    UserPin,
                    func.ST_X(UserPin.geom).label("lon"),
                    func.ST_Y(UserPin.geom).label("lat"),
                )
                .where(UserPin.user_id == user.id, UserPin.client_uuid == payload.client_uuid)
                .limit(1)
            ).first()
            if existing is not None:
                return _pin_to_response(existing[0], existing[1], existing[2])
        raise
    db.refresh(pin)
    return _pin_to_response(pin, payload.lon, payload.lat)


@router.patch("/pins/{pin_id}", response_model=PinResponse)
def update_pin(
    pin_id: str,
    payload: PinUpdateRequest,
    user: User = Depends(get_scoped_write_user),
    db: Session = Depends(get_db),
) -> PinResponse:
    """Update an owned personal pin."""
    try:
        pid = uuid.UUID(pin_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pin not found") from exc

    row = db.execute(
        select(
            UserPin,
            func.ST_X(UserPin.geom).label("lon"),
            func.ST_Y(UserPin.geom).label("lat"),
        )
        .where(UserPin.id == pid, UserPin.user_id == user.id)
        .limit(1)
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pin not found")

    pin, cur_lon, cur_lat = row
    if payload.name is not None:
        pin.title = payload.name.strip()
    if payload.category is not None:
        pin.category = payload.category
    if payload.notes is not None:
        pin.notes = payload.notes.strip() or None
    if payload.elev_m is not None:
        pin.elev_m = payload.elev_m
    new_lon = payload.lon if payload.lon is not None else float(cur_lon)
    new_lat = payload.lat if payload.lat is not None else float(cur_lat)
    if payload.lon is not None or payload.lat is not None:
        pin.geom = from_shape(Point(new_lon, new_lat), srid=4326)

    pin.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(pin)
    return _pin_to_response(pin, new_lon, new_lat)


@router.delete("/pins/{pin_id}")
def delete_pin(
    pin_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str | bool]:
    """Delete an owned personal pin."""
    try:
        pid = uuid.UUID(pin_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pin not found") from exc

    pin = db.execute(
        select(UserPin).where(UserPin.id == pid, UserPin.user_id == user.id).limit(1)
    ).scalar_one_or_none()
    if pin is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pin not found")
    db.delete(pin)
    db.commit()
    return {"ok": True, "id": pin_id}


# -----------------------------------------------------------------------------
# GPX 1.1 & GeoJSON Exports (Public Track Export + Authenticated User Export)
# -----------------------------------------------------------------------------


@router.get("/tracks/{track_id}/export")
def export_track(
    track_id: str,
    format: ExportFormatLiteral = Query(default="gpx"),
    db: Session = Depends(get_db),
) -> Response:
    """Export a single track + nearby POIs as GridForge-compatible GPX 1.1 or GeoJSON."""
    trk = db.get(Track, track_id)
    if trk is None or not trk.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track not found")

    export_trk, nearby_pois = _build_export_track_data(db, trk)
    slug = _slug_filename(trk.name)

    if format == "gpx":
        gpx_xml = build_gpx_document(
            title=trk.name,
            tracks=[export_trk],
            waypoints=nearby_pois,
        )
        return Response(
            content=gpx_xml,
            media_type="application/gpx+xml",
            headers={"Content-Disposition": f'attachment; filename="{slug}.gpx"'},
        )

    gj = build_track_geojson(export_trk, nearby_pois)
    return Response(
        content=json.dumps(gj, separators=(",", ":")),
        media_type="application/geo+json",
        headers={"Content-Disposition": f'attachment; filename="{slug}.geojson"'},
    )


@router.get("/user/export")
def export_user_saved_data(
    format: ExportFormatLiteral = Query(default="gpx"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Export the signed-in user's favorite tracks and personal pins as GPX 1.1 or GeoJSON."""
    fav_rows = [
        (r[0], r[1])
        for r in db.execute(
            select(UserFavorite, Track)
            .join(Track, UserFavorite.track_id == Track.track_id)
            .where(
                UserFavorite.user_id == user.id,
                UserFavorite.track_id.is_not(None),
                Track.is_active.is_(True),
            )
            .order_by(UserFavorite.created_at.desc())
        ).all()
    ]

    tracks_data = _build_export_tracks_batch(db, fav_rows)

    pin_rows = db.execute(
        select(
            UserPin,
            func.ST_X(UserPin.geom).label("lon"),
            func.ST_Y(UserPin.geom).label("lat"),
        )
        .where(UserPin.user_id == user.id)
        .order_by(UserPin.created_at.desc())
    ).all()
    pins_data = [
        ExportPin(
            name=pin.title,
            lon=float(lon),
            lat=float(lat),
            category=pin.category,
            elev_m=pin.elev_m,
            notes=pin.notes,
            source="user_pin",
        )
        for pin, lon, lat in pin_rows
    ]

    slug = _slug_filename(f"trailforge-{user.display_name}-saved")
    if format == "gpx":
        gpx_xml = build_gpx_document(
            title=f"{user.display_name} — TrailForge Saved Routes & Pins",
            tracks=tracks_data,
            waypoints=pins_data,
        )
        return Response(
            content=gpx_xml,
            media_type="application/gpx+xml",
            headers={"Content-Disposition": f'attachment; filename="{slug}.gpx"'},
        )

    gj = build_user_collection_geojson(
        owner_display_name=user.display_name,
        tracks=tracks_data,
        pins=pins_data,
    )
    return Response(
        content=json.dumps(gj, separators=(",", ":")),
        media_type="application/geo+json",
        headers={"Content-Disposition": f'attachment; filename="{slug}.geojson"'},
    )
