"""REST API for interactive multi-track route planning, saved routes CRUD, and GridForge exports."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, StringConstraints, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from trailforge.auth.dependencies import get_current_user, get_optional_current_user
from trailforge.db.models import PlannedRoute, User, VehicleProfile
from trailforge.db.session import get_db
from trailforge.routes.export import build_route_geojson, build_route_gpx
from trailforge.routes.planner import (
    MAX_ROUTE_TRACKS,
    PlannedRouteAnalysis,
    RouteWaypoint,
    TrackNotFoundError,
    build_gridforge_cli,
    plan_route_with_geometry,
    slugify_route_name,
)
from trailforge.vehicles.classifier import normalize_capability_class

router = APIRouter(prefix="/api/routes", tags=["routes"])

ExportFormatLiteral = Literal["gpx", "geojson"]

_NonEmptyRouteName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)
]
_NonEmptyTrackId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]
_NonEmptyWaypointName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]
_WaypointCategory = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)
]


class RouteWaypointInput(BaseModel):
    """Input schema for a custom route waypoint."""

    name: _NonEmptyWaypointName
    category: _WaypointCategory = "waypoint"
    lon: float = Field(..., ge=-180.0, le=180.0)
    lat: float = Field(..., ge=-90.0, le=90.0)


def _validate_optional_vehicle_class(val: str | None) -> str | None:
    if val is None:
        return None
    normalized = normalize_capability_class(val)
    if normalized is None:
        raise ValueError(
            "vehicle_class must be one of: AWD, Stock 4WD, Touring 4WD, Modified 4WD"
        )
    return normalized


class RoutePlanRequest(BaseModel):
    """Public or authenticated route planning / export request body."""

    name: _NonEmptyRouteName | None = None
    track_ids: list[_NonEmptyTrackId] = Field(..., min_length=1, max_length=MAX_ROUTE_TRACKS)
    vehicle_class: str | None = None
    waypoints: list[RouteWaypointInput] = Field(default_factory=list, max_length=100)

    @field_validator("vehicle_class")
    @classmethod
    def _check_vehicle_class(cls, v: str | None) -> str | None:
        return _validate_optional_vehicle_class(v)


class SavedRouteCreateRequest(BaseModel):
    """Authenticated request body to persist a planned route."""

    name: _NonEmptyRouteName
    notes: str | None = Field(default=None, max_length=2000)
    track_ids: list[_NonEmptyTrackId] = Field(..., min_length=1, max_length=MAX_ROUTE_TRACKS)
    waypoints: list[RouteWaypointInput] = Field(default_factory=list, max_length=100)
    vehicle_class: str | None = None

    @field_validator("vehicle_class")
    @classmethod
    def _check_vehicle_class(cls, v: str | None) -> str | None:
        return _validate_optional_vehicle_class(v)


class SavedRouteUpdateRequest(BaseModel):
    """Authenticated PATCH body to update an owned saved route."""

    name: _NonEmptyRouteName | None = None
    notes: str | None = Field(default=None, max_length=2000)
    track_ids: list[_NonEmptyTrackId] | None = Field(
        default=None, min_length=1, max_length=MAX_ROUTE_TRACKS
    )
    waypoints: list[RouteWaypointInput] | None = Field(default=None, max_length=100)
    vehicle_class: str | None = None

    @field_validator("vehicle_class")
    @classmethod
    def _check_vehicle_class(cls, v: str | None) -> str | None:
        return _validate_optional_vehicle_class(v)


class SavedRouteSummaryResponse(BaseModel):
    """Summary entry returned by `GET /api/routes`."""

    id: str
    name: str
    notes: str | None = None
    track_ids: list[str]
    waypoints: list[RouteWaypoint] = Field(default_factory=list)
    vehicle_class: str | None = None
    leg_count: int
    total_length_m: float = 0.0
    est_duration_min: float = 0.0
    worst_grade: str | None = None
    effective_grade: str | None = None
    has_blocking_closure: bool = False
    closure_count: int = 0
    gate_warning_count: int = 0
    wet_risk_level: str = "dry"
    gridforge_cli: str
    created_at: datetime
    updated_at: datetime


class SavedRouteDetailResponse(BaseModel):
    """Persisted route record paired with live recomputed `PlannedRouteAnalysis`."""

    id: str
    name: str
    notes: str | None = None
    track_ids: list[str]
    waypoints: list[RouteWaypoint] = Field(default_factory=list)
    vehicle_class: str | None = None
    created_at: datetime
    updated_at: datetime
    analysis: PlannedRouteAnalysis


def _get_active_vehicle_profile(db: Session, user: User | None) -> VehicleProfile | None:
    if user is None:
        return None
    return db.execute(
        select(VehicleProfile)
        .where(VehicleProfile.user_id == user.id)
        .order_by(VehicleProfile.is_default.desc(), VehicleProfile.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()


def _parse_route_uuid(route_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(route_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Planned route not found"
        ) from exc


def _load_owned_route(db: Session, user: User, route_id: str) -> PlannedRoute:
    rid = _parse_route_uuid(route_id)
    route = db.execute(
        select(PlannedRoute)
        .where(PlannedRoute.id == rid, PlannedRoute.user_id == user.id)
        .limit(1)
    ).scalar_one_or_none()
    if route is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Planned route not found"
        )
    return route


def _run_plan_or_http_error(
    db: Session,
    *,
    track_ids: list[str],
    vehicle_class: str | None,
    vehicle_profile: VehicleProfile | None,
    waypoints: list[RouteWaypoint] | list[dict[str, object]],
    name: str | None,
) -> tuple[PlannedRouteAnalysis, list]:
    try:
        return plan_route_with_geometry(
            db,
            track_ids,
            vehicle_class=vehicle_class,
            vehicle_profile=vehicle_profile,
            waypoints=waypoints,
            name=name,
        )
    except TrackNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


def _route_waypoints_from_model(route: PlannedRoute) -> list[RouteWaypoint]:
    raw = route.waypoints_json or []
    out: list[RouteWaypoint] = []
    for entry in raw:
        if isinstance(entry, dict):
            out.append(RouteWaypoint.model_validate(entry))
    return out


def _build_saved_detail(
    route: PlannedRoute, analysis: PlannedRouteAnalysis
) -> SavedRouteDetailResponse:
    return SavedRouteDetailResponse(
        id=str(route.id),
        name=route.name,
        notes=route.notes,
        track_ids=list(route.track_ids or []),
        waypoints=_route_waypoints_from_model(route),
        vehicle_class=route.vehicle_class,
        created_at=route.created_at,
        updated_at=route.updated_at,
        analysis=analysis,
    )


def _export_response(
    analysis: PlannedRouteAnalysis,
    leg_tracks: list,
    *,
    fmt: ExportFormatLiteral,
    notes: str | None = None,
) -> Response:
    slug = slugify_route_name(analysis.name)
    if fmt == "gpx":
        gpx_xml = build_route_gpx(analysis, leg_tracks=leg_tracks, notes=notes)
        return Response(
            content=gpx_xml,
            media_type="application/gpx+xml",
            headers={"Content-Disposition": f'attachment; filename="{slug}.gpx"'},
        )
    gj = build_route_geojson(analysis, leg_tracks=leg_tracks, notes=notes)
    return Response(
        content=json.dumps(gj, indent=2) + "\n",
        media_type="application/geo+json",
        headers={"Content-Disposition": f'attachment; filename="{slug}.geojson"'},
    )


# -----------------------------------------------------------------------------
# Public / Optionally Authenticated Planning & Ad-Hoc Export Endpoints
# -----------------------------------------------------------------------------


@router.post("/plan", response_model=PlannedRouteAnalysis)
def plan_ad_hoc_route(
    payload: RoutePlanRequest,
    current_user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
) -> PlannedRouteAnalysis:
    """Analyze a multi-track route (works anonymously and uses active rig profile if signed in)."""
    profile = _get_active_vehicle_profile(db, current_user)
    waypoints = [RouteWaypoint(**wp.model_dump()) for wp in payload.waypoints]
    analysis, _ = _run_plan_or_http_error(
        db,
        track_ids=list(payload.track_ids),
        vehicle_class=payload.vehicle_class,
        vehicle_profile=profile,
        waypoints=waypoints,
        name=payload.name,
    )
    return analysis


@router.post("/plan/export")
def export_ad_hoc_route(
    payload: RoutePlanRequest,
    format: ExportFormatLiteral = Query(default="gpx"),
    current_user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Export any ad-hoc planned route as GridForge-compatible GPX 1.1 or GeoJSON."""
    profile = _get_active_vehicle_profile(db, current_user)
    waypoints = [RouteWaypoint(**wp.model_dump()) for wp in payload.waypoints]
    analysis, leg_tracks = _run_plan_or_http_error(
        db,
        track_ids=list(payload.track_ids),
        vehicle_class=payload.vehicle_class,
        vehicle_profile=profile,
        waypoints=waypoints,
        name=payload.name,
    )
    return _export_response(analysis, leg_tracks, fmt=format)


# -----------------------------------------------------------------------------
# Authenticated Saved Routes CRUD & Export Endpoints (Strictly Owner-Only)
# -----------------------------------------------------------------------------


@router.get("", response_model=list[SavedRouteSummaryResponse])
def list_saved_routes(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[SavedRouteSummaryResponse]:
    """List the signed-in user's saved multi-track routes with live summary metrics."""
    routes = db.execute(
        select(PlannedRoute)
        .where(PlannedRoute.user_id == user.id)
        .order_by(PlannedRoute.updated_at.desc(), PlannedRoute.created_at.desc())
    ).scalars().all()

    profile = _get_active_vehicle_profile(db, user)
    summaries: list[SavedRouteSummaryResponse] = []
    for route in routes:
        tids = list(route.track_ids or [])
        wps = _route_waypoints_from_model(route)
        try:
            analysis, _ = plan_route_with_geometry(
                db,
                tids,
                vehicle_class=route.vehicle_class,
                vehicle_profile=profile,
                waypoints=wps,
                name=route.name,
            )
            summaries.append(
                SavedRouteSummaryResponse(
                    id=str(route.id),
                    name=route.name,
                    notes=route.notes,
                    track_ids=tids,
                    waypoints=wps,
                    vehicle_class=route.vehicle_class,
                    leg_count=len(analysis.legs),
                    total_length_m=analysis.total_length_m,
                    est_duration_min=analysis.est_duration_min,
                    worst_grade=analysis.worst_grade,
                    effective_grade=analysis.effective_grade,
                    has_blocking_closure=analysis.safety_summary.has_blocking_closure,
                    closure_count=analysis.safety_summary.closure_count,
                    gate_warning_count=analysis.safety_summary.gate_warning_count,
                    wet_risk_level=analysis.safety_summary.wet_risk_level,
                    gridforge_cli=analysis.gridforge_cli,
                    created_at=route.created_at,
                    updated_at=route.updated_at,
                )
            )
        except ValueError:
            # Saved route references a retired/missing track — still list gracefully
            summaries.append(
                SavedRouteSummaryResponse(
                    id=str(route.id),
                    name=route.name,
                    notes=route.notes,
                    track_ids=tids,
                    waypoints=wps,
                    vehicle_class=route.vehicle_class,
                    leg_count=len(tids),
                    gridforge_cli=build_gridforge_cli(route.name),
                    created_at=route.created_at,
                    updated_at=route.updated_at,
                )
            )
    return summaries


@router.post("", response_model=SavedRouteDetailResponse, status_code=status.HTTP_201_CREATED)
def create_saved_route(
    payload: SavedRouteCreateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SavedRouteDetailResponse:
    """Persist a planned multi-track route (Tier C) and return live computed analysis."""
    profile = _get_active_vehicle_profile(db, user)
    waypoints = [RouteWaypoint(**wp.model_dump()) for wp in payload.waypoints]
    analysis, _ = _run_plan_or_http_error(
        db,
        track_ids=list(payload.track_ids),
        vehicle_class=payload.vehicle_class,
        vehicle_profile=profile,
        waypoints=waypoints,
        name=payload.name,
    )

    now = datetime.now(UTC)
    route = PlannedRoute(
        user_id=user.id,
        name=payload.name.strip(),
        notes=payload.notes.strip() if payload.notes and payload.notes.strip() else None,
        track_ids=list(payload.track_ids),
        waypoints_json=[wp.model_dump() for wp in waypoints],
        vehicle_class=payload.vehicle_class,
        created_at=now,
        updated_at=now,
    )
    db.add(route)
    db.commit()
    db.refresh(route)
    return _build_saved_detail(route, analysis)


@router.get("/{route_id}", response_model=SavedRouteDetailResponse)
def get_saved_route(
    route_id: str,
    vehicle_class: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SavedRouteDetailResponse:
    """Return an owned saved route with freshly recomputed live analysis."""
    route = _load_owned_route(db, user, route_id)
    effective_vc = route.vehicle_class
    if vehicle_class is not None:
        normalized_vc = normalize_capability_class(vehicle_class)
        if normalized_vc is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="vehicle_class must be one of: AWD, Stock 4WD, Touring 4WD, Modified 4WD",
            )
        effective_vc = normalized_vc

    profile = _get_active_vehicle_profile(db, user)
    waypoints = _route_waypoints_from_model(route)
    analysis, _ = _run_plan_or_http_error(
        db,
        track_ids=list(route.track_ids or []),
        vehicle_class=effective_vc,
        vehicle_profile=profile,
        waypoints=waypoints,
        name=route.name,
    )
    return _build_saved_detail(route, analysis)


@router.patch("/{route_id}", response_model=SavedRouteDetailResponse)
def update_saved_route(
    route_id: str,
    payload: SavedRouteUpdateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SavedRouteDetailResponse:
    """Update an owned saved route's name, notes, track_ids, waypoints, or vehicle_class."""
    route = _load_owned_route(db, user, route_id)

    fields_set = payload.model_fields_set
    new_name = payload.name.strip() if payload.name is not None else route.name
    if "name" in fields_set and payload.name is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Route name cannot be null or blank",
        )
    if "track_ids" in fields_set and payload.track_ids is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="track_ids cannot be null or empty",
        )
    new_track_ids = (
        list(payload.track_ids) if payload.track_ids is not None else list(route.track_ids or [])
    )
    new_waypoints = (
        [RouteWaypoint(**wp.model_dump()) for wp in payload.waypoints]
        if payload.waypoints is not None
        else _route_waypoints_from_model(route)
    )
    if "vehicle_class" in fields_set:
        new_vehicle_class = payload.vehicle_class
    else:
        new_vehicle_class = route.vehicle_class
    if "notes" in fields_set:
        new_notes = payload.notes.strip() if payload.notes and payload.notes.strip() else None
    else:
        new_notes = route.notes

    profile = _get_active_vehicle_profile(db, user)
    analysis, _ = _run_plan_or_http_error(
        db,
        track_ids=new_track_ids,
        vehicle_class=new_vehicle_class,
        vehicle_profile=profile,
        waypoints=new_waypoints,
        name=new_name,
    )

    route.name = new_name
    route.notes = new_notes
    route.track_ids = new_track_ids
    route.waypoints_json = [wp.model_dump() for wp in new_waypoints]
    route.vehicle_class = new_vehicle_class
    route.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(route)
    return _build_saved_detail(route, analysis)


@router.delete("/{route_id}")
def delete_saved_route(
    route_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str | bool]:
    """Delete an owned saved route."""
    route = _load_owned_route(db, user, route_id)
    db.delete(route)
    db.commit()
    return {"ok": True, "id": str(route.id)}


@router.get("/{route_id}/export")
def export_saved_route(
    route_id: str,
    format: ExportFormatLiteral = Query(default="gpx"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Download an owned saved route as GridForge-compatible GPX 1.1 or GeoJSON."""
    route = _load_owned_route(db, user, route_id)
    profile = _get_active_vehicle_profile(db, user)
    waypoints = _route_waypoints_from_model(route)
    analysis, leg_tracks = _run_plan_or_http_error(
        db,
        track_ids=list(route.track_ids or []),
        vehicle_class=route.vehicle_class,
        vehicle_profile=profile,
        waypoints=waypoints,
        name=route.name,
    )
    return _export_response(analysis, leg_tracks, fmt=format, notes=route.notes)
