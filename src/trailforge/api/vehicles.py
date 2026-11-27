"""Vehicle Profiles CRUD, active rig switching, and capability class reference API."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AliasChoices, BaseModel, Field, StringConstraints
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from trailforge.auth.dependencies import get_current_user, get_scoped_read_user
from trailforge.db.models import User, VehicleProfile
from trailforge.db.session import get_db
from trailforge.vehicles.classifier import (
    VEHICLE_CLASS_REFERENCE,
    CapabilityClassLiteral,
    DrivetrainLiteral,
    TireClassLiteral,
    VehicleSpecs,
    classify_vehicle,
)

router = APIRouter(prefix="/api/vehicles", tags=["vehicles"])


class VehicleClassReference(BaseModel):
    id: str
    label: str
    summary: str
    typical_setup: str
    recommended_max_grade: str


_NonEmptyVehicleName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]


class VehicleProfileCreate(BaseModel):
    name: _NonEmptyVehicleName
    drivetrain: DrivetrainLiteral = "4wd_part_time"
    low_range: bool = Field(
        default=True,
        validation_alias=AliasChoices("low_range", "has_low_range"),
    )
    lift_mm: int = Field(default=0, ge=0, le=300)
    clearance_mm: int = Field(
        default=210,
        ge=120,
        le=500,
        validation_alias=AliasChoices("clearance_mm", "ground_clearance_mm"),
    )
    tire_class: TireClassLiteral = "AT"
    front_locker: bool = False
    rear_locker: bool = False
    has_winch: bool = False
    has_snorkel: bool = False
    is_active: bool = Field(
        default=False,
        validation_alias=AliasChoices("is_active", "is_default"),
    )


class VehicleProfileUpdate(BaseModel):
    name: _NonEmptyVehicleName | None = None
    drivetrain: DrivetrainLiteral | None = None
    low_range: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("low_range", "has_low_range"),
    )
    lift_mm: int | None = Field(default=None, ge=0, le=300)
    clearance_mm: int | None = Field(
        default=None,
        ge=120,
        le=500,
        validation_alias=AliasChoices("clearance_mm", "ground_clearance_mm"),
    )
    tire_class: TireClassLiteral | None = None
    front_locker: bool | None = None
    rear_locker: bool | None = None
    has_winch: bool | None = None
    has_snorkel: bool | None = None
    is_active: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("is_active", "is_default"),
    )


class VehicleProfileResponse(BaseModel):
    id: str
    name: str
    drivetrain: DrivetrainLiteral
    low_range: bool
    lift_mm: int
    clearance_mm: int
    tire_class: TireClassLiteral
    front_locker: bool
    rear_locker: bool
    has_winch: bool
    has_snorkel: bool
    capability_class: CapabilityClassLiteral
    classification_reasons: list[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime


def _specs_from_profile(profile: VehicleProfile) -> VehicleSpecs:
    return VehicleSpecs(
        drivetrain=profile.drivetrain,  # type: ignore[arg-type]
        low_range=bool(profile.has_low_range),
        lift_mm=int(profile.lift_mm),
        clearance_mm=int(profile.ground_clearance_mm),
        tire_class=profile.tire_class,  # type: ignore[arg-type]
        front_locker=bool(profile.front_locker),
        rear_locker=bool(profile.rear_locker),
        has_winch=bool(profile.has_winch),
        has_snorkel=bool(profile.has_snorkel),
    )


def _to_response(profile: VehicleProfile) -> VehicleProfileResponse:
    classification = classify_vehicle(_specs_from_profile(profile))
    return VehicleProfileResponse(
        id=str(profile.id),
        name=profile.name,
        drivetrain=profile.drivetrain,  # type: ignore[arg-type]
        low_range=bool(profile.has_low_range),
        lift_mm=int(profile.lift_mm),
        clearance_mm=int(profile.ground_clearance_mm),
        tire_class=profile.tire_class,  # type: ignore[arg-type]
        front_locker=bool(profile.front_locker),
        rear_locker=bool(profile.rear_locker),
        has_winch=bool(profile.has_winch),
        has_snorkel=bool(profile.has_snorkel),
        capability_class=profile.capability_class,  # type: ignore[arg-type]
        classification_reasons=classification.classification_reasons,
        is_active=bool(profile.is_default),
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def _clear_default_profiles(db: Session, user_id: uuid.UUID) -> None:
    db.execute(
        update(VehicleProfile)
        .where(VehicleProfile.user_id == user_id, VehicleProfile.is_default.is_(True))
        .values(is_default=False, updated_at=datetime.now(UTC))
    )
    db.flush()


def _parse_uuid(raw_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Vehicle profile not found"
        ) from exc


@router.get("/classes", response_model=list[VehicleClassReference])
def list_vehicle_classes() -> list[VehicleClassReference]:
    """Return public reference metadata for TrailForge's 4 normalized Vehicle Capability Classes."""
    return [VehicleClassReference(**item) for item in VEHICLE_CLASS_REFERENCE]


@router.get("", response_model=list[VehicleProfileResponse])
def list_vehicles(
    user: User = Depends(get_scoped_read_user),
    db: Session = Depends(get_db),
) -> list[VehicleProfileResponse]:
    """List the signed-in user's Vehicle Profiles (active profile first)."""
    rows = db.execute(
        select(VehicleProfile)
        .where(VehicleProfile.user_id == user.id)
        .order_by(VehicleProfile.is_default.desc(), VehicleProfile.created_at.asc())
    ).scalars().all()
    return [_to_response(p) for p in rows]


@router.post("", response_model=VehicleProfileResponse, status_code=status.HTTP_201_CREATED)
def create_vehicle(
    payload: VehicleProfileCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VehicleProfileResponse:
    """Create a Vehicle Profile with deterministic capability classification."""
    specs = VehicleSpecs(
        drivetrain=payload.drivetrain,
        low_range=payload.low_range,
        lift_mm=payload.lift_mm,
        clearance_mm=payload.clearance_mm,
        tire_class=payload.tire_class,
        front_locker=payload.front_locker,
        rear_locker=payload.rear_locker,
        has_winch=payload.has_winch,
        has_snorkel=payload.has_snorkel,
    )
    classification = classify_vehicle(specs)

    existing_count = db.execute(
        select(VehicleProfile.id).where(VehicleProfile.user_id == user.id).limit(1)
    ).first()
    should_be_active = payload.is_active or existing_count is None
    if should_be_active:
        _clear_default_profiles(db, user.id)

    profile = VehicleProfile(
        user_id=user.id,
        name=payload.name.strip(),
        drivetrain=payload.drivetrain,
        has_low_range=payload.low_range,
        lift_mm=payload.lift_mm,
        ground_clearance_mm=payload.clearance_mm,
        tire_class=payload.tire_class,
        front_locker=payload.front_locker,
        rear_locker=payload.rear_locker,
        has_winch=payload.has_winch,
        has_snorkel=payload.has_snorkel,
        capability_class=classification.capability_class,
        is_default=should_be_active,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return _to_response(profile)


@router.patch("/{vehicle_id}", response_model=VehicleProfileResponse)
def update_vehicle(
    vehicle_id: str,
    payload: VehicleProfileUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VehicleProfileResponse:
    """Update an owned Vehicle Profile and recompute its deterministic capability class."""
    vid = _parse_uuid(vehicle_id)
    profile = db.execute(
        select(VehicleProfile)
        .where(VehicleProfile.id == vid, VehicleProfile.user_id == user.id)
        .limit(1)
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Vehicle profile not found"
        )

    if payload.name is not None:
        profile.name = payload.name.strip()
    if payload.drivetrain is not None:
        profile.drivetrain = payload.drivetrain
    if payload.low_range is not None:
        profile.has_low_range = payload.low_range
    if payload.lift_mm is not None:
        profile.lift_mm = payload.lift_mm
    if payload.clearance_mm is not None:
        profile.ground_clearance_mm = payload.clearance_mm
    if payload.tire_class is not None:
        profile.tire_class = payload.tire_class
    if payload.front_locker is not None:
        profile.front_locker = payload.front_locker
    if payload.rear_locker is not None:
        profile.rear_locker = payload.rear_locker
    if payload.has_winch is not None:
        profile.has_winch = payload.has_winch
    if payload.has_snorkel is not None:
        profile.has_snorkel = payload.has_snorkel

    classification = classify_vehicle(_specs_from_profile(profile))
    profile.capability_class = classification.capability_class

    if payload.is_active is True and not profile.is_default:
        _clear_default_profiles(db, user.id)
        profile.is_default = True
    elif payload.is_active is False:
        profile.is_default = False

    profile.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(profile)
    return _to_response(profile)


@router.post("/{vehicle_id}/activate", response_model=VehicleProfileResponse)
def activate_vehicle(
    vehicle_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VehicleProfileResponse:
    """Switch the signed-in user's active default Vehicle Profile."""
    vid = _parse_uuid(vehicle_id)
    profile = db.execute(
        select(VehicleProfile)
        .where(VehicleProfile.id == vid, VehicleProfile.user_id == user.id)
        .limit(1)
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Vehicle profile not found"
        )

    if not profile.is_default:
        _clear_default_profiles(db, user.id)
        profile.is_default = True
        profile.updated_at = datetime.now(UTC)
        db.commit()
        db.refresh(profile)
    return _to_response(profile)


@router.delete("/{vehicle_id}")
def delete_vehicle(
    vehicle_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str | bool]:
    """Delete an owned Vehicle Profile, promoting the next profile if it was active."""
    vid = _parse_uuid(vehicle_id)
    profile = db.execute(
        select(VehicleProfile)
        .where(VehicleProfile.id == vid, VehicleProfile.user_id == user.id)
        .limit(1)
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Vehicle profile not found"
        )

    was_default = bool(profile.is_default)
    db.delete(profile)
    db.flush()

    if was_default:
        replacement = db.execute(
            select(VehicleProfile)
            .where(VehicleProfile.user_id == user.id)
            .order_by(VehicleProfile.created_at.asc())
            .limit(1)
        ).scalar_one_or_none()
        if replacement is not None:
            replacement.is_default = True
            replacement.updated_at = datetime.now(UTC)

    db.commit()
    return {"ok": True, "id": vehicle_id}
