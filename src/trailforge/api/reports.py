"""REST API for Slice 08 Community Condition Reports, Voting, Decay, and Safe Photo Uploads."""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from geoalchemy2.shape import from_shape
from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)
from shapely.geometry import Point
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

from trailforge.api.middleware import check_upload_quota
from trailforge.auth.dependencies import get_current_user, get_optional_current_user
from trailforge.config import get_settings
from trailforge.db.models import (
    ConditionReport,
    ConditionReportVote,
    Track,
    TrackSegment,
    User,
    VehicleProfile,
)
from trailforge.db.session import get_db
from trailforge.reports.decay import (
    DEFAULT_SEVERITY_BY_TYPE,
    ReportSeverityLiteral,
    ReportTypeLiteral,
    build_osm_links,
    compute_expires_at,
    evaluate_report_decay,
    extract_osm_way_id,
)
from trailforge.reports.photos import (
    MAX_PHOTO_BYTES,
    PhotoNotFoundError,
    PhotoPathTraversalError,
    PhotoValidationError,
    resolve_photo_path,
    sanitize_photo_bytes,
    save_sanitized_report_photo,
)
from trailforge.reports.queries import (
    get_report_by_id,
    get_reports_geojson,
    list_reports,
    pack_report_description,
    resolve_osm_way_id_for_track,
    snap_point_to_nearest_segment,
)
from trailforge.vehicles.classifier import normalize_capability_class

router = APIRouter(prefix="/api/reports", tags=["reports"])

VoteActionLiteral = Literal["confirm", "resolved"]

_NonEmptyNotes = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]


class ConditionReportCreateInput(BaseModel):
    """Validated payload for creating a community condition report (JSON or multipart form)."""

    report_type: ReportTypeLiteral
    lon: float = Field(..., ge=-180.0, le=180.0)
    lat: float = Field(..., ge=-90.0, le=90.0)
    notes: _NonEmptyNotes = Field(
        ...,
        validation_alias=AliasChoices("notes", "description"),
    )
    title: str | None = Field(default=None, max_length=120)
    severity: ReportSeverityLiteral | None = None
    track_id: str | None = Field(default=None, max_length=64)
    segment_id: str | None = Field(default=None, max_length=64)
    vehicle_class: str | None = Field(
        default=None,
        validation_alias=AliasChoices("vehicle_class", "vehicle_capability_class"),
    )
    client_uuid: uuid.UUID | None = None

    @field_validator("lon", "lat")
    @classmethod
    def _ensure_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Coordinate must be a finite number")
        return value

    @field_validator("title", "track_id", "segment_id", "vehicle_class", mode="before")
    @classmethod
    def _empty_str_to_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("severity", mode="before")
    @classmethod
    def _empty_severity_to_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("client_uuid", mode="before")
    @classmethod
    def _empty_uuid_to_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class ReportVoteRequest(BaseModel):
    """Idempotent confirm or resolved vote payload."""

    vote: VoteActionLiteral = Field(
        ...,
        validation_alias=AliasChoices("vote", "vote_type"),
    )


class ConditionReportResponse(BaseModel):
    """Public condition report representation (strictly zero author email exposure)."""

    id: str
    report_type: ReportTypeLiteral
    report_type_label: str
    is_gate_warning: bool
    severity: ReportSeverityLiteral
    status: str
    effective_status: str
    title: str
    notes: str
    description: str
    photo_url: str | None = None
    vehicle_class: str | None = None
    author_display_name: str
    track_id: str | None = None
    track_name: str | None = None
    segment_id: str | None = None
    lon: float
    lat: float
    confirm_count: int
    resolved_count: int
    freshness_score: float
    is_stale: bool
    half_life_days: float
    osm_note_url: str
    osm_edit_url: str
    observed_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    last_confirmed_at: str | None = None
    expires_at: str | None = None
    user_vote: str | None = None


def _enforce_photo_upload_quota(request: Request, current_user: User) -> None:
    """Enforce daily per-member photo upload quota (skipping admins or when disabled)."""
    cfg = getattr(getattr(request.app, "state", None), "custom_settings", None) or get_settings()
    if not cfg.rate_limit_enabled:
        return
    if getattr(current_user, "role", "member") == "admin":
        return
    check_upload_quota(current_user.id, limit_per_day=cfg.rate_limit_upload_per_day)


def _enforce_text_report_quota(request: Request, current_user: User) -> None:
    """Enforce daily per-member text-only report quota (skipping admins or when disabled)."""
    cfg = getattr(getattr(request.app, "state", None), "custom_settings", None) or get_settings()
    if not cfg.rate_limit_enabled:
        return
    if getattr(current_user, "role", "member") == "admin":
        return
    text_limit = max(50, int(cfg.rate_limit_upload_per_day) * 2)
    check_upload_quota(f"report_text:{current_user.id}", limit_per_day=text_limit)


def _get_user_default_vehicle_class(db: Session, user: User) -> str | None:
    profile = db.execute(
        select(VehicleProfile)
        .where(VehicleProfile.user_id == user.id)
        .order_by(VehicleProfile.is_default.desc(), VehicleProfile.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    return profile.capability_class if profile is not None else None


async def _parse_create_request(
    request: Request,
) -> tuple[ConditionReportCreateInput, tuple[bytes, str, str] | None]:
    """Parse either ``application/json`` or ``multipart/form-data`` (+ optional ``photo``)."""
    content_type = (request.headers.get("content-type") or "").lower()
    photo_bytes: bytes | None = None

    if content_type.startswith("multipart/form-data") or content_type.startswith(
        "application/x-www-form-urlencoded"
    ):
        form = await request.form()
        raw_fields: dict[str, Any] = {}
        for key, value in form.multi_items():
            if key == "photo" and isinstance(value, StarletteUploadFile):
                chunk = await value.read(MAX_PHOTO_BYTES + 1)
                if len(chunk) > 0:
                    photo_bytes = chunk
                elif value.filename:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail="Uploaded photo is empty",
                    )
            elif not isinstance(value, StarletteUploadFile):
                raw_fields[key] = value
    else:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Request body must be valid JSON or multipart/form-data",
            ) from exc
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="JSON payload must be an object",
            )
        raw_fields = body

    try:
        parsed = ConditionReportCreateInput.model_validate(raw_fields)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.errors(),
        ) from exc

    sanitized_photo: tuple[bytes, str, str] | None = None
    if photo_bytes is not None:
        try:
            # Single Pillow decode/sanitize pass executed off the asyncio event loop (SEC-H2)
            sanitized_photo = await anyio.to_thread.run_sync(sanitize_photo_bytes, photo_bytes)
        except PhotoValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc

    return parsed, sanitized_photo


@router.get("", response_model=list[ConditionReportResponse])
def get_reports(
    track_id: str | None = Query(default=None),
    report_type: str | None = Query(default=None),
    include_resolved: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """List community hazard and gate condition reports with live staleness decay."""
    try:
        return list_reports(
            db,
            track_id=track_id,
            report_type=report_type,
            include_resolved=include_resolved,
            limit=limit,
            current_user_id=current_user.id if current_user else None,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@router.get("/geojson")
def get_reports_feature_collection(
    track_id: str | None = Query(default=None),
    report_type: str | None = Query(default=None),
    include_resolved: bool = Query(default=False),
    limit: int = Query(default=500, ge=1, le=1000),
    current_user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return RFC 7946 FeatureCollection of community condition reports for MapLibre markers."""
    try:
        return get_reports_geojson(
            db,
            track_id=track_id,
            report_type=report_type,
            include_resolved=include_resolved,
            limit=limit,
            current_user_id=current_user.id if current_user else None,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@router.get("/photos/{filename:path}")
def get_report_photo(
    filename: str,
    db: Session = Depends(get_db),
) -> Response:
    """Serve a validated, non-hidden report photo attachment via streaming FileResponse."""
    try:
        photo_path, media_type = resolve_photo_path(filename)
    except PhotoPathTraversalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except PhotoNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    expected_url = f"/api/reports/photos/{photo_path.name}"
    report_statuses = db.execute(
        select(ConditionReport.status).where(ConditionReport.photo_url == expected_url)
    ).scalars().all()
    if report_statuses and all(st == "hidden" for st in report_statuses):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Photo not found",
        )

    return FileResponse(
        path=photo_path,
        media_type=media_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


@router.get("/{report_id}", response_model=ConditionReportResponse)
def get_single_report(
    report_id: str,
    current_user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Fetch a single community condition report by UUID."""
    try:
        rid = uuid.UUID(report_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Condition report not found"
        ) from exc

    payload = get_report_by_id(
        db,
        rid,
        current_user_id=current_user.id if current_user else None,
    )
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Condition report not found"
        )
    return payload


@router.post("", response_model=ConditionReportResponse, status_code=status.HTTP_201_CREATED)
async def create_report(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Create a structured condition report (supports JSON or multipart/form-data with photo)."""
    payload, sanitized_photo = await _parse_create_request(request)

    # Idempotent PWA sync check via (user_id, client_uuid)
    if payload.client_uuid is not None:
        existing = db.execute(
            select(ConditionReport)
            .where(
                ConditionReport.user_id == user.id,
                ConditionReport.client_uuid == payload.client_uuid,
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            existing_payload = get_report_by_id(db, existing.id, current_user_id=user.id)
            if existing_payload is not None:
                return existing_payload

    # Resolve vehicle capability class (explicit override or active Vehicle Profile)
    vehicle_class: str | None
    if payload.vehicle_class is not None:
        normalized_vc = normalize_capability_class(payload.vehicle_class)
        if normalized_vc is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="vehicle_class must be one of: AWD, Stock 4WD, Touring 4WD, Modified 4WD",
            )
        vehicle_class = normalized_vc
    else:
        vehicle_class = _get_user_default_vehicle_class(db, user)

    # Resolve track_id, segment_id, and osm_way_id (explicit or 75 m spatial auto-snap)
    resolved_segment_id = payload.segment_id
    resolved_track_id = payload.track_id
    osm_way_id: int | None = None

    if resolved_segment_id is not None:
        seg = db.get(TrackSegment, resolved_segment_id)
        if seg is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Track segment not found",
            )
        if resolved_track_id is not None and seg.track_id and resolved_track_id != seg.track_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="segment_id does not belong to the specified track_id",
            )
        if resolved_track_id is None:
            resolved_track_id = seg.track_id
        osm_way_id = extract_osm_way_id(seg.source_refs)
    elif resolved_track_id is not None:
        trk = db.get(Track, resolved_track_id)
        if trk is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Track not found",
            )
        snapped_on_track = snap_point_to_nearest_segment(
            db, payload.lon, payload.lat, track_id=resolved_track_id
        )
        if snapped_on_track is not None:
            resolved_segment_id = snapped_on_track.segment_id
            osm_way_id = snapped_on_track.osm_way_id
        if osm_way_id is None:
            osm_way_id = resolve_osm_way_id_for_track(db, track_id=resolved_track_id)
    else:
        snapped = snap_point_to_nearest_segment(db, payload.lon, payload.lat)
        if snapped is not None:
            resolved_segment_id = snapped.segment_id
            resolved_track_id = snapped.track_id
            osm_way_id = snapped.osm_way_id

    if osm_way_id is None and (resolved_segment_id or resolved_track_id):
        osm_way_id = resolve_osm_way_id_for_track(
            db, segment_id=resolved_segment_id, track_id=resolved_track_id
        )

    # Enforce per-user daily report quota (photo or text) & save EXIF-stripped photo
    saved_photo_path = None
    photo_url: str | None = None
    if sanitized_photo is not None:
        _enforce_photo_upload_quota(request, user)
        clean_bytes, ext, media_type = sanitized_photo
        try:
            saved = await anyio.to_thread.run_sync(
                save_sanitized_report_photo, clean_bytes, ext, media_type
            )
            saved_photo_path = saved.path
            photo_url = saved.url
        except PhotoValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
    else:
        _enforce_text_report_quota(request, user)

    now = datetime.now(UTC)
    severity = payload.severity or DEFAULT_SEVERITY_BY_TYPE[payload.report_type]
    links = build_osm_links(payload.lat, payload.lon, osm_way_id=osm_way_id)
    expires_at = compute_expires_at(payload.report_type, now)

    report = ConditionReport(
        user_id=user.id,
        client_uuid=payload.client_uuid,
        segment_id=resolved_segment_id,
        track_id=resolved_track_id,
        report_type=payload.report_type,
        severity=severity,
        status="active",
        description=pack_report_description(payload.title, payload.notes),
        photo_url=photo_url,
        vehicle_capability_class=vehicle_class,
        geom=from_shape(Point(payload.lon, payload.lat), srid=4326),
        confirm_count=0,
        resolve_count=0,
        decay_score=1.0,
        osm_edit_url=links["osm_edit_url"],
        observed_at=now,
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
    )

    try:
        db.add(report)
        db.commit()
        db.refresh(report)
    except Exception:
        db.rollback()
        if saved_photo_path is not None:
            saved_photo_path.unlink(missing_ok=True)
        raise

    created_payload = get_report_by_id(db, report.id, current_user_id=user.id, now=now)
    assert created_payload is not None
    return created_payload


@router.post("/{report_id}/vote", response_model=ConditionReportResponse)
def vote_on_report(
    report_id: str,
    payload: ReportVoteRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Idempotently upsert a user's ``confirm`` or ``resolved`` vote and recalculate decay."""
    try:
        rid = uuid.UUID(report_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Condition report not found"
        ) from exc

    report = db.execute(
        select(ConditionReport)
        .where(ConditionReport.id == rid, ConditionReport.status != "hidden")
        .limit(1)
    ).scalar_one_or_none()
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Condition report not found"
        )

    now = datetime.now(UTC)
    existing_vote = db.execute(
        select(ConditionReportVote)
        .where(
            ConditionReportVote.report_id == rid,
            ConditionReportVote.user_id == user.id,
        )
        .limit(1)
    ).scalar_one_or_none()

    if existing_vote is not None:
        existing_vote.vote_type = payload.vote
        existing_vote.created_at = now
        db.flush()
    else:
        try:
            with db.begin_nested():
                db.add(
                    ConditionReportVote(
                        report_id=rid,
                        user_id=user.id,
                        vote_type=payload.vote,
                        created_at=now,
                    )
                )
                db.flush()
        except IntegrityError:
            existing_vote = db.execute(
                select(ConditionReportVote)
                .where(
                    ConditionReportVote.report_id == rid,
                    ConditionReportVote.user_id == user.id,
                )
                .limit(1)
            ).scalar_one_or_none()
            if existing_vote is None:
                raise
            existing_vote.vote_type = payload.vote
            existing_vote.created_at = now
            db.flush()

    votes = db.execute(
        select(ConditionReportVote.vote_type, ConditionReportVote.created_at).where(
            ConditionReportVote.report_id == rid
        )
    ).all()

    confirm_count = sum(1 for vtype, _ in votes if vtype == "confirm")
    resolved_count = sum(1 for vtype, _ in votes if vtype == "resolved")
    confirm_timestamps = [ts for vtype, ts in votes if vtype == "confirm" and ts is not None]
    last_confirmed_at = max(confirm_timestamps) if confirm_timestamps else None

    created_ref = report.created_at
    if report.observed_at is not None and created_ref is not None:
        c_utc = (
            created_ref.astimezone(UTC) if created_ref.tzinfo else created_ref.replace(tzinfo=UTC)
        )
        o_utc = (
            report.observed_at.astimezone(UTC)
            if report.observed_at.tzinfo
            else report.observed_at.replace(tzinfo=UTC)
        )
        base_ts = min(c_utc, o_utc)
    else:
        base_ts = report.observed_at or report.created_at

    decay = evaluate_report_decay(
        report.report_type,
        created_at=base_ts,
        last_confirmed_at=last_confirmed_at,
        confirm_count=confirm_count,
        resolved_count=resolved_count,
        status="active",
        now=now,
    )

    report.confirm_count = confirm_count
    report.resolve_count = resolved_count
    report.decay_score = decay.freshness_score
    report.status = decay.effective_status
    report.expires_at = compute_expires_at(report.report_type, last_confirmed_at or base_ts)
    report.updated_at = now

    db.commit()

    updated = get_report_by_id(db, rid, current_user_id=user.id, now=now)
    assert updated is not None
    return updated
