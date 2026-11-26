"""Privacy-preserving trip logbook, GPX/JSON telemetry upload, HMM matching, and unmapped API."""

from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from geoalchemy2.shape import from_shape
from pydantic import AliasChoices, BaseModel, Field, StringConstraints, ValidationError
from pyproj import Transformer
from shapely.geometry import LineString, Point
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from trailforge.auth.dependencies import get_current_user
from trailforge.config import get_settings
from trailforge.db.models import (
    TelemetryObservation,
    TrackSegment,
    Trip,
    UnmappedTrackCandidate,
    User,
    VehicleProfile,
)
from trailforge.db.session import get_db
from trailforge.export.formats import (
    ExportTrackData,
    build_gpx_document,
    build_track_geojson,
)
from trailforge.matching import (
    MAX_PRIVACY_TRIM_M,
    MIN_PRIVACY_TRIM_M,
    GpsPoint,
    MatchResult,
    PostGISHMMMatcher,
    TrackMatcher,
    compute_contribution_hash,
    parse_gpx,
    parse_telemetry_json,
    points_to_linestring,
    trim_privacy_buffer,
)
from trailforge.vehicles.classifier import CapabilityClassLiteral, normalize_capability_class

router = APIRouter(prefix="/api", tags=["trips"])

TripVisibilityLiteral = Literal["private", "community_anonymous"]
ExportFormatLiteral = Literal["gpx", "geojson"]

_MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MiB max GPX/JSON trace upload
_MAX_SPEED_KMH = 160.0
_FILENAME_SLUG_RE = re.compile(r"[^a-z0-9]+")
_WGS84_TO_ALBERS = Transformer.from_crs("EPSG:4326", "EPSG:3577", always_xy=True)

_NonEmptyTripName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=140)
]


def get_track_matcher(db: Session = Depends(get_db)) -> TrackMatcher:
    """FastAPI dependency returning the active pluggable `TrackMatcher` (Valhalla-swappable)."""
    return PostGISHMMMatcher(db)


class TripPointJsonInput(BaseModel):
    lon: float = Field(..., ge=-180.0, le=180.0)
    lat: float = Field(..., ge=-90.0, le=90.0)
    elev_m: float | None = None
    timestamp: str | datetime | None = None
    accuracy_m: float | None = Field(default=None, ge=0.0)
    speed_mps: float | None = Field(default=None, ge=0.0)
    roughness_rms: float | None = Field(default=None, ge=0.0)


class TripUploadJsonRequest(BaseModel):
    name: _NonEmptyTripName | None = Field(
        default=None,
        validation_alias=AliasChoices("name", "title"),
    )
    points: list[TripPointJsonInput] = Field(..., min_length=2)
    vehicle_profile_id: str | None = None
    vehicle_class: str | None = None
    visibility: TripVisibilityLiteral = "private"
    privacy_trim_m: float = Field(default=MIN_PRIVACY_TRIM_M)
    client_uuid: uuid.UUID | None = None


class TripUpdateRequest(BaseModel):
    name: _NonEmptyTripName | None = Field(
        default=None,
        validation_alias=AliasChoices("name", "title"),
    )
    visibility: TripVisibilityLiteral | None = None


class TripMatchedSegmentResponse(BaseModel):
    segment_id: str
    track_id: str | None = None
    direction: str = "forward"
    confidence: float
    duration_s: float
    avg_speed_kmh: float
    min_speed_kmh: float | None = None
    roughness_rms: float | None = None
    matched_point_count: int = 1


class TripUnmappedCandidateResponse(BaseModel):
    id: int | None = None
    length_m: float
    reason: str = "no_candidates"
    coordinates: list[list[float]] = Field(default_factory=list)
    cluster_key: str | None = None
    status: str = "pending_review"


class TripItemResponse(BaseModel):
    id: str
    trip_id: str
    name: str
    visibility: TripVisibilityLiteral
    vehicle_profile_id: str | None = None
    vehicle_class: str
    vehicle_capability_class: str
    privacy_trim_m: float
    raw_length_m: float
    trimmed_length_m: float
    duration_s: float | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    match_status: str
    matcher_name: str | None = None
    matched_segment_count: int = 0
    avg_match_confidence: float | None = None
    observations_published: int = 0
    unmapped_candidate_count: int = 0
    matched_segments: list[TripMatchedSegmentResponse] = Field(default_factory=list)
    unmapped_candidates: list[TripUnmappedCandidateResponse] = Field(default_factory=list)
    created_at: datetime


class TripDetailResponse(TripItemResponse):
    trimmed_geojson: dict[str, Any] | None = None
    raw_geojson: dict[str, Any] | None = None


def _slug_filename(name: str) -> str:
    slug = _FILENAME_SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "trailforge-trip"


def _clamp_speed(speed_kmh: float) -> float:
    if not math.isfinite(speed_kmh) or speed_kmh < 0.0:
        return 0.0
    return round(min(_MAX_SPEED_KMH, float(speed_kmh)), 2)


def _cluster_key_for_coords(coords: Sequence[tuple[float, float]]) -> str:
    mid = coords[len(coords) // 2]
    grid_lon = round(mid[0] / 0.002) * 0.002
    grid_lat = round(mid[1] / 0.002) * 0.002
    return f"u_{grid_lon:.3f}_{grid_lat:.3f}"


def _validate_trim_meters(trim_m: float) -> float:
    if not math.isfinite(trim_m) or trim_m < MIN_PRIVACY_TRIM_M or trim_m > MAX_PRIVACY_TRIM_M:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"privacy_trim_m must be between {MIN_PRIVACY_TRIM_M:.0f} and "
                f"{MAX_PRIVACY_TRIM_M:.0f} meters"
            ),
        )
    return float(trim_m)


def _resolve_trip_vehicle(
    db: Session,
    user: User,
    vehicle_profile_id: str | None,
    vehicle_class: str | None,
) -> tuple[uuid.UUID | None, CapabilityClassLiteral]:
    if vehicle_profile_id is not None and vehicle_profile_id.strip():
        try:
            vid = uuid.UUID(vehicle_profile_id.strip())
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid vehicle_profile_id UUID",
            ) from exc
        profile = db.execute(
            select(VehicleProfile).where(
                VehicleProfile.id == vid, VehicleProfile.user_id == user.id
            )
        ).scalar_one_or_none()
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Vehicle profile not found",
            )
        return profile.id, profile.capability_class  # type: ignore[return-value]

    if vehicle_class is not None and vehicle_class.strip():
        norm = normalize_capability_class(vehicle_class)
        if norm is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Invalid vehicle_class; expected AWD, Stock 4WD, Touring 4WD, or Modified 4WD"
                ),
            )
        return None, norm

    default_profile = db.execute(
        select(VehicleProfile)
        .where(VehicleProfile.user_id == user.id, VehicleProfile.is_default.is_(True))
        .limit(1)
    ).scalar_one_or_none()
    if default_profile is not None:
        return default_profile.id, default_profile.capability_class  # type: ignore[return-value]

    return None, "Stock 4WD"


def _compute_trip_hash(trip_id: uuid.UUID, nonce: uuid.UUID) -> str:
    cfg = get_settings()
    return compute_contribution_hash(cfg.privacy_secret_key, trip_id, nonce)


def _revoke_trip_contributions(db: Session, contrib_hash: str) -> tuple[int, int]:
    """Immediately delete all anonymized telemetry & unmapped rows for a contribution hash."""
    obs_res = db.execute(
        delete(TelemetryObservation).where(TelemetryObservation.contribution_hash == contrib_hash)
    )
    unm_res = db.execute(
        delete(UnmappedTrackCandidate).where(
            UnmappedTrackCandidate.contribution_hash == contrib_hash
        )
    )
    return int(obs_res.rowcount or 0), int(unm_res.rowcount or 0)


def _lookup_track_ids_for_segments(
    db: Session, segment_ids: Sequence[str]
) -> dict[str, str | None]:
    unique_ids = list(dict.fromkeys(s for s in segment_ids if s))
    if not unique_ids:
        return {}
    rows = db.execute(
        select(TrackSegment.segment_id, TrackSegment.track_id).where(
            TrackSegment.segment_id.in_(unique_ids)
        )
    ).all()
    return {str(seg_id): tid for seg_id, tid in rows}


def _persist_community_contributions(
    db: Session,
    *,
    contrib_hash: str,
    vehicle_class: str,
    match_result: MatchResult,
) -> tuple[int, int]:
    """Persist strictly anonymized telemetry observations & unmapped track candidates."""
    obs_count = 0
    for seg in match_result.matched_segments:
        obs_at = (
            seg.entry_at.astimezone(UTC)
            if seg.entry_at.tzinfo is not None
            else seg.entry_at.replace(tzinfo=UTC)
        )
        hotspot_geom = (
            from_shape(Point(seg.slow_hotspot_lonlat[0], seg.slow_hotspot_lonlat[1]), srid=4326)
            if seg.slow_hotspot_lonlat is not None
            else None
        )
        db.add(
            TelemetryObservation(
                segment_id=seg.segment_id,
                contribution_hash=contrib_hash,
                vehicle_capability_class=vehicle_class,
                direction=seg.direction,
                observed_at=obs_at,
                observed_date=obs_at.date(),
                traversal_duration_s=max(0.1, round(float(seg.duration_s), 2)),
                avg_speed_kmh=_clamp_speed(seg.avg_speed_kmh),
                min_speed_kmh=(
                    _clamp_speed(seg.min_speed_kmh) if seg.min_speed_kmh is not None else None
                ),
                roughness_rms=(
                    max(0.0, round(float(seg.roughness_rms), 4))
                    if seg.roughness_rms is not None
                    else None
                ),
                roughness_p95=(
                    max(0.0, round(float(seg.roughness_p95), 4))
                    if seg.roughness_p95 is not None
                    else None
                ),
                match_confidence=max(0.0, min(1.0, round(float(seg.match_confidence), 4))),
                matched_point_count=max(1, int(seg.matched_point_count)),
                slow_hotspot_geom=hotspot_geom,
            )
        )
        obs_count += 1

    unm_count = 0
    for sub in match_result.unmatched_subtraces:
        if len(sub.coordinates) < 2 or sub.length_m <= 0.0:
            continue
        obs_dt = (
            sub.started_at.astimezone(UTC)
            if sub.started_at.tzinfo is not None
            else sub.started_at.replace(tzinfo=UTC)
        )
        db.add(
            UnmappedTrackCandidate(
                contribution_hash=contrib_hash,
                vehicle_capability_class=vehicle_class,
                geom=from_shape(LineString(sub.coordinates), srid=4326),
                length_m=round(float(sub.length_m), 2),
                observed_date=obs_dt.date(),
                cluster_key=_cluster_key_for_coords(sub.coordinates),
                status="pending_review",
            )
        )
        unm_count += 1

    return obs_count, unm_count


def _derive_match_status(match_result: MatchResult) -> str:
    has_matched = len(match_result.matched_segments) > 0
    has_unmapped = any(
        len(s.coordinates) >= 2 and s.length_m > 0.0 for s in match_result.unmatched_subtraces
    )
    if has_matched and has_unmapped:
        return "partial"
    if has_matched:
        return "matched"
    return "unmapped"


def reconstruct_trimmed_points(
    coords_lonlat: Sequence[Sequence[float]],
    *,
    started_at: datetime | None,
    ended_at: datetime | None,
    duration_s: float | None,
    fallback_at: datetime | None = None,
) -> list[GpsPoint]:
    """Reconstruct monotonically timestamped `GpsPoint`s along a persisted `trimmed_geom`."""
    if len(coords_lonlat) < 2:
        return []
    lons = [float(c[0]) for c in coords_lonlat]
    lats = [float(c[1]) for c in coords_lonlat]
    xs, ys = _WGS84_TO_ALBERS.transform(lons, lats)

    cum_m: list[float] = [0.0]
    for i in range(1, len(lons)):
        cum_m.append(cum_m[-1] + math.hypot(xs[i] - xs[i - 1], ys[i] - ys[i - 1]))
    total_m = cum_m[-1]

    base_start = started_at or fallback_at or datetime.now(UTC)
    if base_start.tzinfo is None:
        base_start = base_start.replace(tzinfo=UTC)

    total_dur = duration_s
    if (total_dur is None or total_dur <= 0.0) and started_at and ended_at:
        total_dur = (ended_at - started_at).total_seconds()
    if total_dur is None or total_dur <= 0.0:
        # Sensible 4WD touring pace (~20 km/h = 5.55 m/s) when original timestamps were omitted
        total_dur = max(float(len(lons) - 1) * 5.0, total_m / 5.55)

    points: list[GpsPoint] = []
    last_ts: datetime | None = None
    for idx, (lon, lat) in enumerate(zip(lons, lats, strict=True)):
        frac = (cum_m[idx] / total_m) if total_m > 1e-6 else (idx / max(1, len(lons) - 1))
        ts = base_start + timedelta(seconds=total_dur * frac)
        if last_ts is not None and ts <= last_ts:
            ts = last_ts + timedelta(milliseconds=100)
        last_ts = ts
        points.append(GpsPoint(lon=lon, lat=lat, timestamp=ts))
    return points


def _build_matched_segments_payload(
    db: Session, match_result: MatchResult
) -> list[TripMatchedSegmentResponse]:
    missing_track_ids = [s.segment_id for s in match_result.matched_segments if s.track_id is None]
    track_map = _lookup_track_ids_for_segments(db, missing_track_ids)
    return [
        TripMatchedSegmentResponse(
            segment_id=s.segment_id,
            track_id=s.track_id if s.track_id is not None else track_map.get(s.segment_id),
            direction=s.direction,
            confidence=round(float(s.match_confidence), 4),
            duration_s=round(float(s.duration_s), 2),
            avg_speed_kmh=_clamp_speed(s.avg_speed_kmh),
            min_speed_kmh=_clamp_speed(s.min_speed_kmh) if s.min_speed_kmh is not None else None,
            roughness_rms=round(float(s.roughness_rms), 4) if s.roughness_rms is not None else None,
            matched_point_count=int(s.matched_point_count),
        )
        for s in match_result.matched_segments
    ]


def _build_unmapped_payload(match_result: MatchResult) -> list[TripUnmappedCandidateResponse]:
    return [
        TripUnmappedCandidateResponse(
            length_m=round(float(s.length_m), 2),
            reason=s.reason,
            coordinates=[[round(c[0], 6), round(c[1], 6)] for c in s.coordinates],
            cluster_key=_cluster_key_for_coords(s.coordinates) if s.coordinates else None,
        )
        for s in match_result.unmatched_subtraces
        if len(s.coordinates) >= 2 and s.length_m > 0.0
    ]


def _load_published_for_trip(
    db: Session, trip: Trip
) -> tuple[list[TripMatchedSegmentResponse], list[TripUnmappedCandidateResponse]]:
    if trip.visibility != "community_anonymous":
        return [], []
    chash = _compute_trip_hash(trip.id, trip.contribution_nonce)

    obs_rows = db.execute(
        select(TelemetryObservation, TrackSegment.track_id)
        .outerjoin(TrackSegment, TelemetryObservation.segment_id == TrackSegment.segment_id)
        .where(TelemetryObservation.contribution_hash == chash)
        .order_by(TelemetryObservation.observed_at.asc(), TelemetryObservation.id.asc())
    ).all()
    matched = [
        TripMatchedSegmentResponse(
            segment_id=obs.segment_id,
            track_id=tid,
            direction=obs.direction,
            confidence=round(float(obs.match_confidence), 4),
            duration_s=round(float(obs.traversal_duration_s), 2),
            avg_speed_kmh=round(float(obs.avg_speed_kmh), 2),
            min_speed_kmh=(
                round(float(obs.min_speed_kmh), 2) if obs.min_speed_kmh is not None else None
            ),
            roughness_rms=(
                round(float(obs.roughness_rms), 4) if obs.roughness_rms is not None else None
            ),
            matched_point_count=int(obs.matched_point_count),
        )
        for obs, tid in obs_rows
    ]

    unm_rows = db.execute(
        select(
            UnmappedTrackCandidate,
            func.ST_AsGeoJSON(UnmappedTrackCandidate.geom).label("geom_json"),
        )
        .where(UnmappedTrackCandidate.contribution_hash == chash)
        .order_by(UnmappedTrackCandidate.id.asc())
    ).all()
    unmapped: list[TripUnmappedCandidateResponse] = []
    for cand, geom_json in unm_rows:
        g = json.loads(geom_json) if geom_json else {}
        unmapped.append(
            TripUnmappedCandidateResponse(
                id=int(cand.id),
                length_m=round(float(cand.length_m), 2),
                reason="no_candidates",
                coordinates=g.get("coordinates", []),
                cluster_key=cand.cluster_key,
                status=cand.status,
            )
        )
    return matched, unmapped


def _trip_to_response(
    trip: Trip,
    *,
    matched_segments: list[TripMatchedSegmentResponse],
    unmapped_candidates: list[TripUnmappedCandidateResponse],
    observations_published: int,
    unmapped_candidate_count: int,
) -> TripItemResponse:
    return TripItemResponse(
        id=str(trip.id),
        trip_id=str(trip.id),
        name=trip.title or "Recorded Trip",
        visibility=trip.visibility,  # type: ignore[arg-type]
        vehicle_profile_id=str(trip.vehicle_profile_id) if trip.vehicle_profile_id else None,
        vehicle_class=trip.vehicle_capability_class,
        vehicle_capability_class=trip.vehicle_capability_class,
        privacy_trim_m=round(float(trip.privacy_trim_m), 2),
        raw_length_m=round(float(trip.raw_distance_m), 2),
        trimmed_length_m=round(float(trip.trimmed_distance_m), 2),
        duration_s=round(float(trip.duration_s), 2) if trip.duration_s is not None else None,
        started_at=trip.started_at,
        ended_at=trip.ended_at,
        match_status=trip.match_status,
        matcher_name=trip.matcher_name,
        matched_segment_count=len(matched_segments),
        avg_match_confidence=(
            round(float(trip.avg_match_confidence), 4)
            if trip.avg_match_confidence is not None
            else None
        ),
        observations_published=observations_published,
        unmapped_candidate_count=unmapped_candidate_count,
        matched_segments=matched_segments,
        unmapped_candidates=unmapped_candidates,
        created_at=trip.created_at,
    )


def _get_owned_trip(db: Session, user: User, trip_id: str) -> Trip:
    try:
        tid = uuid.UUID(trip_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found"
        ) from exc
    trip = db.execute(
        select(Trip).where(Trip.id == tid, Trip.user_id == user.id)
    ).scalar_one_or_none()
    if trip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found")
    return trip


@router.post("/trips/upload", response_model=TripItemResponse, status_code=status.HTTP_201_CREATED)
async def upload_trip(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    matcher: TrackMatcher = Depends(get_track_matcher),
) -> TripItemResponse:
    """Upload a GPX 1.1 file or Telemetry JSON trace with mandatory >=250 m privacy trimming."""
    content_type = (request.headers.get("content-type") or "").lower()

    explicit_name: str | None = None
    vehicle_profile_id: str | None = None
    vehicle_class: str | None = None
    visibility: TripVisibilityLiteral = "private"
    privacy_trim_m: float = MIN_PRIVACY_TRIM_M
    client_uuid: uuid.UUID | None = None
    parsed_name: str | None = None
    points: list[GpsPoint]

    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Multipart upload requires a GPX or JSON 'file' field",
            )
        raw_bytes = await upload.read(_MAX_UPLOAD_BYTES + 1)
        if not raw_bytes:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Uploaded trace file is empty",
            )
        if len(raw_bytes) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Uploaded trace exceeds 15 MB limit",
            )

        raw_name_field = form.get("name") if "name" in form else form.get("title")
        if raw_name_field is not None:
            cand = str(raw_name_field).strip()
            if not cand:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Trip name cannot be blank",
                )
            if len(cand) > 140:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Trip name must be at most 140 characters",
                )
            explicit_name = cand

        raw_vis = str(form.get("visibility") or "private").strip()
        if raw_vis not in ("private", "community_anonymous"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="visibility must be 'private' or 'community_anonymous'",
            )
        visibility = raw_vis  # type: ignore[assignment]

        raw_trim = form.get("privacy_trim_m")
        if raw_trim is not None and str(raw_trim).strip():
            try:
                privacy_trim_m = float(str(raw_trim).strip())
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="privacy_trim_m must be a valid number",
                ) from exc

        if form.get("vehicle_profile_id") is not None:
            vehicle_profile_id = str(form.get("vehicle_profile_id") or "").strip() or None
        if form.get("vehicle_class") is not None:
            vehicle_class = str(form.get("vehicle_class") or "").strip() or None
        if form.get("client_uuid") is not None and str(form.get("client_uuid")).strip():
            try:
                client_uuid = uuid.UUID(str(form.get("client_uuid")).strip())
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Invalid client_uuid",
                ) from exc

        filename = str(getattr(upload, "filename", "") or "")
        stripped_head = raw_bytes.lstrip()[:1]
        is_json = filename.lower().endswith(".json") or stripped_head in (b"{", b"[")
        try:
            parsed = parse_telemetry_json(raw_bytes) if is_json else parse_gpx(raw_bytes)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        parsed_name = parsed.name or (
            re.sub(r"\.(gpx|json)$", "", filename, flags=re.IGNORECASE).strip() or None
        )
        points = parsed.points
    else:
        raw_body = await request.body()
        if len(raw_body) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Payload exceeds 15 MB limit",
            )
        try:
            payload_obj = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Request body must be valid JSON or multipart/form-data",
            ) from exc
        try:
            req = TripUploadJsonRequest.model_validate(payload_obj)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=exc.errors(),
            ) from exc

        explicit_name = req.name
        vehicle_profile_id = req.vehicle_profile_id
        vehicle_class = req.vehicle_class
        visibility = req.visibility
        privacy_trim_m = req.privacy_trim_m
        client_uuid = req.client_uuid

        try:
            parsed = parse_telemetry_json(
                {
                    "name": req.name,
                    "points": [p.model_dump() for p in req.points],
                }
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        parsed_name = parsed.name
        points = parsed.points

    trim_m = _validate_trim_meters(privacy_trim_m)
    resolved_vp_id, resolved_vclass = _resolve_trip_vehicle(
        db, current_user, vehicle_profile_id, vehicle_class
    )
    trip_title = (explicit_name or parsed_name or "Recorded Trip").strip()[:140]

    # Idempotent client_uuid lookup for PWA offline retries
    if client_uuid is not None:
        existing = db.execute(
            select(Trip).where(Trip.user_id == current_user.id, Trip.client_uuid == client_uuid)
        ).scalar_one_or_none()
        if existing is not None:
            m_segs, unm_cands = _load_published_for_trip(db, existing)
            return _trip_to_response(
                existing,
                matched_segments=m_segs,
                unmapped_candidates=unm_cands,
                observations_published=len(m_segs),
                unmapped_candidate_count=len(unm_cands),
            )

    try:
        trim_res = trim_privacy_buffer(points, trim_m=trim_m)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    raw_line = points_to_linestring(points)
    started_at = points[0].timestamp
    ended_at = points[-1].timestamp
    duration_s = max(0.0, (ended_at - started_at).total_seconds())

    trip_id = uuid.uuid4()
    nonce = uuid.uuid4()
    contrib_hash = _compute_trip_hash(trip_id, nonce)

    trimmed_geom = None
    match_status = "skipped_too_short"
    matcher_name: str | None = matcher.name
    avg_conf: float | None = None
    matched_payload: list[TripMatchedSegmentResponse] = []
    unmapped_payload: list[TripUnmappedCandidateResponse] = []
    obs_published = 0
    unm_published = 0

    if not trim_res.too_short and len(trim_res.trimmed_points) >= 2:
        trimmed_geom = from_shape(points_to_linestring(trim_res.trimmed_points), srid=4326)
        match_result = await matcher.match_trace(trim_res.trimmed_points)
        matcher_name = match_result.matcher_name
        avg_conf = float(match_result.overall_confidence)
        matched_payload = _build_matched_segments_payload(db, match_result)
        unmapped_payload = _build_unmapped_payload(match_result)

        if visibility == "community_anonymous":
            match_status = _derive_match_status(match_result)
            obs_published, unm_published = _persist_community_contributions(
                db,
                contrib_hash=contrib_hash,
                vehicle_class=resolved_vclass,
                match_result=match_result,
            )
        else:
            # Private logbook ONLY: zero rows written to telemetry_observations or unmapped table
            match_status = "skipped_private"

    trip = Trip(
        id=trip_id,
        user_id=current_user.id,
        client_uuid=client_uuid,
        vehicle_profile_id=resolved_vp_id,
        vehicle_capability_class=resolved_vclass,
        title=trip_title,
        visibility=visibility,
        privacy_trim_m=trim_m,
        contribution_nonce=nonce,
        raw_geom=from_shape(raw_line, srid=4326),
        trimmed_geom=trimmed_geom,
        raw_distance_m=trim_res.raw_length_m,
        trimmed_distance_m=trim_res.trimmed_length_m,
        duration_s=round(duration_s, 2),
        started_at=started_at,
        ended_at=ended_at,
        match_status=match_status,
        matcher_name=matcher_name,
        avg_match_confidence=avg_conf,
    )
    db.add(trip)
    db.commit()
    db.refresh(trip)

    return _trip_to_response(
        trip,
        matched_segments=matched_payload,
        unmapped_candidates=unmapped_payload,
        observations_published=obs_published,
        unmapped_candidate_count=unm_published,
    )


@router.get("/trips", response_model=list[TripItemResponse])
def list_trips(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TripItemResponse]:
    """List the signed-in user's trips newest-first with live contribution counts."""
    trips = db.execute(
        select(Trip).where(Trip.user_id == current_user.id).order_by(Trip.created_at.desc())
    ).scalars().all()
    if not trips:
        return []

    hashes_by_trip = {
        t.id: _compute_trip_hash(t.id, t.contribution_nonce)
        for t in trips
        if t.visibility == "community_anonymous"
    }
    obs_counts: dict[str, int] = {}
    unm_counts: dict[str, int] = {}
    if hashes_by_trip:
        hash_list = list(hashes_by_trip.values())
        obs_counts = {
            str(h): int(c)
            for h, c in db.execute(
                select(TelemetryObservation.contribution_hash, func.count(TelemetryObservation.id))
                .where(TelemetryObservation.contribution_hash.in_(hash_list))
                .group_by(TelemetryObservation.contribution_hash)
            ).all()
        }
        unm_counts = {
            str(h): int(c)
            for h, c in db.execute(
                select(
                    UnmappedTrackCandidate.contribution_hash, func.count(UnmappedTrackCandidate.id)
                )
                .where(UnmappedTrackCandidate.contribution_hash.in_(hash_list))
                .group_by(UnmappedTrackCandidate.contribution_hash)
            ).all()
        }

    items: list[TripItemResponse] = []
    for trip in trips:
        chash = hashes_by_trip.get(trip.id)
        obs_cnt = obs_counts.get(chash, 0) if chash else 0
        unm_cnt = unm_counts.get(chash, 0) if chash else 0
        resp = _trip_to_response(
            trip,
            matched_segments=[],
            unmapped_candidates=[],
            observations_published=obs_cnt,
            unmapped_candidate_count=unm_cnt,
        )
        resp.matched_segment_count = obs_cnt
        items.append(resp)
    return items


@router.get("/trips/{trip_id}", response_model=TripDetailResponse)
def get_trip_detail(
    trip_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TripDetailResponse:
    """Return owner-only trip detail including trimmed & raw GeoJSON and matched segments."""
    trip = _get_owned_trip(db, current_user, trip_id)
    geom_row = db.execute(
        select(
            func.ST_AsGeoJSON(Trip.trimmed_geom).label("trimmed_json"),
            func.ST_AsGeoJSON(Trip.raw_geom).label("raw_json"),
        ).where(Trip.id == trip.id)
    ).one()

    matched, unmapped = _load_published_for_trip(db, trip)
    base = _trip_to_response(
        trip,
        matched_segments=matched,
        unmapped_candidates=unmapped,
        observations_published=len(matched),
        unmapped_candidate_count=len(unmapped),
    )
    return TripDetailResponse(
        **base.model_dump(),
        trimmed_geojson=json.loads(geom_row.trimmed_json) if geom_row.trimmed_json else None,
        raw_geojson=json.loads(geom_row.raw_json) if geom_row.raw_json else None,
    )


@router.patch("/trips/{trip_id}", response_model=TripItemResponse)
async def update_trip(
    trip_id: str,
    payload: TripUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    matcher: TrackMatcher = Depends(get_track_matcher),
) -> TripItemResponse:
    """Rename a trip or toggle private <-> community_anonymous with instant revocation/publish."""
    trip = _get_owned_trip(db, current_user, trip_id)

    if payload.name is not None:
        trip.title = payload.name

    matched_payload: list[TripMatchedSegmentResponse] = []
    unmapped_payload: list[TripUnmappedCandidateResponse] = []
    obs_published = 0
    unm_published = 0

    if payload.visibility is not None and payload.visibility != trip.visibility:
        old_hash = _compute_trip_hash(trip.id, trip.contribution_nonce)

        if payload.visibility == "private":
            # Immediate revocation: purge anonymized telemetry & unmapped rows + rotate nonce
            _revoke_trip_contributions(db, old_hash)
            trip.contribution_nonce = uuid.uuid4()
            trip.visibility = "private"
            if trip.match_status != "skipped_too_short":
                trip.match_status = "skipped_private"
        else:
            # Publish from persisted privacy-trimmed geometry using a rotated contribution_hash
            _revoke_trip_contributions(db, old_hash)
            trip.contribution_nonce = uuid.uuid4()
            new_hash = _compute_trip_hash(trip.id, trip.contribution_nonce)
            trip.visibility = "community_anonymous"

            trimmed_json = db.execute(
                select(func.ST_AsGeoJSON(Trip.trimmed_geom)).where(Trip.id == trip.id)
            ).scalar_one_or_none()

            if trimmed_json and trip.match_status != "skipped_too_short":
                geom_obj = json.loads(trimmed_json)
                coords = geom_obj.get("coordinates") or []
                pts = reconstruct_trimmed_points(
                    coords,
                    started_at=trip.started_at,
                    ended_at=trip.ended_at,
                    duration_s=trip.duration_s,
                    fallback_at=trip.created_at,
                )
                if len(pts) >= 2:
                    match_result = await matcher.match_trace(pts)
                    trip.matcher_name = match_result.matcher_name
                    trip.avg_match_confidence = float(match_result.overall_confidence)
                    trip.match_status = _derive_match_status(match_result)
                    obs_published, unm_published = _persist_community_contributions(
                        db,
                        contrib_hash=new_hash,
                        vehicle_class=trip.vehicle_capability_class,
                        match_result=match_result,
                    )
                    matched_payload = _build_matched_segments_payload(db, match_result)
                    unmapped_payload = _build_unmapped_payload(match_result)
    else:
        matched_payload, unmapped_payload = _load_published_for_trip(db, trip)
        obs_published = len(matched_payload)
        unm_published = len(unmapped_payload)

    db.commit()
    db.refresh(trip)

    return _trip_to_response(
        trip,
        matched_segments=matched_payload,
        unmapped_candidates=unmapped_payload,
        observations_published=obs_published,
        unmapped_candidate_count=unm_published,
    )


@router.delete("/trips/{trip_id}")
def delete_trip(
    trip_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Revoke any published anonymized telemetry via contribution_hash and delete the trip."""
    trip = _get_owned_trip(db, current_user, trip_id)
    old_hash = _compute_trip_hash(trip.id, trip.contribution_nonce)
    revoked_obs, revoked_unm = _revoke_trip_contributions(db, old_hash)
    db.delete(trip)
    db.commit()
    return {
        "deleted": True,
        "trip_id": str(trip.id),
        "revoked_observations": revoked_obs,
        "revoked_unmapped": revoked_unm,
    }


@router.get("/trips/{trip_id}/export")
def export_trip(
    trip_id: str,
    export_format: ExportFormatLiteral = Query(default="gpx", alias="format"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Export an owned trip as GridForge-compatible GPX 1.1 or GeoJSON."""
    trip = _get_owned_trip(db, current_user, trip_id)
    raw_json = db.execute(
        select(func.ST_AsGeoJSON(Trip.raw_geom)).where(Trip.id == trip.id)
    ).scalar_one_or_none()
    geom_obj = json.loads(raw_json) if raw_json else {}
    coords_raw = geom_obj.get("coordinates") or []
    coords_lonlat_ele: list[tuple[float, float, float | None]] = [
        (float(c[0]), float(c[1]), float(c[2]) if len(c) >= 3 else None) for c in coords_raw
    ]

    trip_name = trip.title or "Recorded Trip"
    export_data = ExportTrackData(
        track_id=str(trip.id),
        name=trip_name,
        state="VIC/NSW",
        grade=None,
        total_length_m=float(trip.raw_distance_m or 0.0),
        coords_lonlat_ele=coords_lonlat_ele,
        description=(
            f"Visibility: {trip.visibility} | Vehicle: {trip.vehicle_capability_class} | "
            f"Privacy trim: {trip.privacy_trim_m:.0f} m"
        ),
        has_osm_source=False,
        extra_props={
            "feature_kind": "trip",
            "visibility": trip.visibility,
            "vehicle_class": trip.vehicle_capability_class,
            "privacy_trim_m": float(trip.privacy_trim_m),
            "match_status": trip.match_status,
        },
    )
    slug = _slug_filename(trip_name)

    if export_format == "gpx":
        xml_body = build_gpx_document(title=trip_name, tracks=[export_data], waypoints=[])
        return Response(
            content=xml_body,
            media_type="application/gpx+xml; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{slug}.gpx"'},
        )

    fc = build_track_geojson(export_data, [])
    return Response(
        content=json.dumps(fc, indent=2) + "\n",
        media_type="application/geo+json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{slug}.geojson"'},
    )


@router.get("/telemetry/unmapped")
def get_unmapped_candidates_geojson(
    limit: int = Query(default=250, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return strictly anonymized off-network unmapped track candidates as GeoJSON."""
    rows = db.execute(
        select(
            UnmappedTrackCandidate,
            func.ST_AsGeoJSON(UnmappedTrackCandidate.geom).label("geom_json"),
        )
        .where(UnmappedTrackCandidate.status != "rejected")
        .order_by(UnmappedTrackCandidate.created_at.desc(), UnmappedTrackCandidate.id.desc())
        .limit(limit)
    ).all()

    features: list[dict[str, Any]] = []
    for cand, geom_json in rows:
        if not geom_json:
            continue
        features.append(
            {
                "type": "Feature",
                "id": f"unmapped-{cand.id}",
                "geometry": json.loads(geom_json),
                "properties": {
                    "candidate_id": int(cand.id),
                    "vehicle_class": cand.vehicle_capability_class,
                    "length_m": round(float(cand.length_m), 1),
                    "observed_date": cand.observed_date.isoformat(),
                    "cluster_key": cand.cluster_key,
                    "status": cand.status,
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}
