"""Admin management, traffic observability, invite codes, and user moderation API."""

from __future__ import annotations

import importlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from trailforge.auth.community_trust import revoke_community_trust
from trailforge.auth.dependencies import require_admin
from trailforge.auth.invites import (
    classify_invite_status,
    create_invite_code,
    get_registration_mode,
)
from trailforge.config import get_settings
from trailforge.db.models import (
    ConditionReport,
    InviteCode,
    PlannedRoute,
    TelemetryObservation,
    Track,
    TrackSegment,
    Trip,
    UsageMetricHourly,
    User,
)
from trailforge.db.session import get_db
from trailforge.feeds.queries import get_feed_health_summary
from trailforge.ui_preferences import (
    get_admin_ui_settings,
    reset_instance_ui_settings,
    upsert_instance_ui_settings,
)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


class AdminUserItem(BaseModel):
    id: str
    email: str
    display_name: str
    role: Literal["admin", "member"]
    is_active: bool
    disabled_at: str | None = None
    invited_by_code: str | None = None
    created_at: str | None = None


class AdminUserListResponse(BaseModel):
    users: list[AdminUserItem]
    total: int


class AdminUserUpdateRequest(BaseModel):
    role: Literal["admin", "member"] | None = None
    is_active: bool | None = None


class AdminInviteItem(BaseModel):
    id: str
    code: str
    label: str | None = None
    role: Literal["admin", "member"]
    max_uses: int
    use_count: int
    status: Literal["active", "exhausted", "expired", "revoked"]
    expires_at: str | None = None
    revoked_at: str | None = None
    created_by_user_id: str | None = None
    created_at: str | None = None


class AdminInviteListResponse(BaseModel):
    invites: list[AdminInviteItem]
    total: int


class CreateInviteRequest(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    role: Literal["member", "admin"] = Field(default="member")
    max_uses: int = Field(default=1, ge=1, le=1000)
    expires_in_days: int | None = Field(default=14, ge=1, le=3650)


class AdminUiSettingsResponse(BaseModel):
    default_layer_order: list[str]
    default_layer_visibility: dict[str, bool]
    show_stack_telemetry_public: bool
    show_stack_telemetry_public_override: bool | None = None
    env_show_stack_telemetry_public: bool
    has_custom_layer_order: bool = False
    has_custom_layer_visibility: bool = False
    updated_at: str | None = None


class AdminUiSettingsUpdateRequest(BaseModel):
    default_layer_order: list[str] | None = None
    default_layer_visibility: dict[str, bool] | None = None
    show_stack_telemetry_public: bool | None = None


def _iso_or_none(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    aware = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_bucket_hour(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return fallback
    return fallback


def _dir_size_bytes(path: Path) -> int:
    """Safely sum regular non-symlink file sizes under ``path`` (returns 0 if missing)."""
    try:
        if not path.exists() or not path.is_dir():
            return 0
        total = 0
        for root, _dirs, files in os.walk(path):
            root_path = Path(root)
            for fname in files:
                fpath = root_path / fname
                try:
                    if not fpath.is_symlink() and fpath.is_file():
                        total += int(fpath.stat().st_size)
                except OSError:
                    continue
        return total
    except OSError:
        return 0


def _get_uploads_max_total_mb() -> int:
    cfg = get_settings()
    val = getattr(cfg, "uploads_max_total_mb", None)
    if val is not None:
        try:
            return max(1, int(val))
        except (TypeError, ValueError):
            return 500
    raw = os.environ.get("TRAILFORGE_UPLOADS_MAX_TOTAL_MB") or os.environ.get(
        "UPLOADS_MAX_TOTAL_MB", "500"
    )
    try:
        return max(1, int(raw))
    except ValueError:
        return 500


def _get_db_size_bytes(db: Session) -> int:
    try:
        size = db.execute(text("SELECT pg_database_size(current_database())")).scalar_one_or_none()
        return int(size) if size is not None else 0
    except SQLAlchemyError:
        return 0


def _get_live_usage_snapshot_safe() -> dict[str, Any] | None:
    """Import and call ``trailforge.api.middleware.get_live_usage_snapshot`` if present."""
    try:
        mod = importlib.import_module("trailforge.api.middleware")
        fn = getattr(mod, "get_live_usage_snapshot", None)
        if callable(fn):
            snap = fn()
            if isinstance(snap, dict):
                return snap
    except Exception:
        return None
    return None


def _empty_counters() -> dict[str, int]:
    return {
        "request_count": 0,
        "error_count": 0,
        "bytes_out": 0,
        "rate_limited_count": 0,
    }


def _add_counters(
    target: dict[str, int],
    *,
    request_count: int,
    error_count: int,
    bytes_out: int,
    rate_limited_count: int,
) -> None:
    target["request_count"] += int(request_count)
    target["error_count"] += int(error_count)
    target["bytes_out"] += int(bytes_out)
    target["rate_limited_count"] += int(rate_limited_count)


def _merge_live_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    now: datetime,
    cutoff_24h: datetime,
    cutoff_7d: datetime,
    traffic_24h: dict[str, int],
    traffic_7d: dict[str, int],
    route_totals: dict[str, dict[str, int]],
) -> None:
    if not snapshot:
        return

    current_hour = now.replace(minute=0, second=0, microsecond=0)

    buckets = snapshot.get("pending_buckets")
    if not isinstance(buckets, list):
        buckets = snapshot.get("buckets")

    if isinstance(buckets, list):
        for entry in buckets:
            if not isinstance(entry, dict):
                continue
            rg = str(entry.get("route_group") or "other")[:48]
            bh = _parse_bucket_hour(entry.get("bucket_hour"), current_hour)
            req = int(entry.get("request_count", 0) or 0)
            err = int(entry.get("error_count", 0) or 0)
            bout = int(entry.get("bytes_out", 0) or 0)
            rl = int(entry.get("rate_limited_count", 0) or 0)
            if bh >= cutoff_7d:
                _add_counters(
                    traffic_7d,
                    request_count=req,
                    error_count=err,
                    bytes_out=bout,
                    rate_limited_count=rl,
                )
            if bh >= cutoff_24h:
                _add_counters(
                    traffic_24h,
                    request_count=req,
                    error_count=err,
                    bytes_out=bout,
                    rate_limited_count=rl,
                )
                rg_bucket = route_totals.setdefault(rg, _empty_counters())
                _add_counters(
                    rg_bucket,
                    request_count=req,
                    error_count=err,
                    bytes_out=bout,
                    rate_limited_count=rl,
                )
        return

    by_group = snapshot.get("by_route_group")
    if not isinstance(by_group, dict):
        by_group = snapshot.get("route_groups")
    if not isinstance(by_group, dict):
        by_group = {
            k: v for k, v in snapshot.items() if isinstance(v, dict) and "request_count" in v
        }

    for rg_key, entry in by_group.items():
        if not isinstance(entry, dict):
            continue
        rg = str(rg_key)[:48]
        req = int(entry.get("request_count", 0) or 0)
        err = int(entry.get("error_count", 0) or 0)
        bout = int(entry.get("bytes_out", 0) or 0)
        rl = int(entry.get("rate_limited_count", 0) or 0)
        _add_counters(
            traffic_7d,
            request_count=req,
            error_count=err,
            bytes_out=bout,
            rate_limited_count=rl,
        )
        _add_counters(
            traffic_24h,
            request_count=req,
            error_count=err,
            bytes_out=bout,
            rate_limited_count=rl,
        )
        rg_bucket = route_totals.setdefault(rg, _empty_counters())
        _add_counters(
            rg_bucket,
            request_count=req,
            error_count=err,
            bytes_out=bout,
            rate_limited_count=rl,
        )


def _to_admin_user(user: User) -> AdminUserItem:
    role: Literal["admin", "member"] = "admin" if user.role == "admin" else "member"
    return AdminUserItem(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=role,
        is_active=bool(user.is_active),
        disabled_at=_iso_or_none(user.disabled_at),
        invited_by_code=user.invited_by_code,
        created_at=_iso_or_none(user.created_at),
    )


def _to_admin_invite(invite: InviteCode, *, now: datetime | None = None) -> AdminInviteItem:
    role: Literal["admin", "member"] = "admin" if invite.role == "admin" else "member"
    return AdminInviteItem(
        id=str(invite.id),
        code=invite.code,
        label=invite.label,
        role=role,
        max_uses=int(invite.max_uses),
        use_count=int(invite.use_count),
        status=classify_invite_status(invite, now=now),
        expires_at=_iso_or_none(invite.expires_at),
        revoked_at=_iso_or_none(invite.revoked_at),
        created_by_user_id=str(invite.created_by_user_id) if invite.created_by_user_id else None,
        created_at=_iso_or_none(invite.created_at),
    )


@router.get("/stats")
def get_admin_stats(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return traffic, user, invite, content, storage, and feed observability metrics."""
    now = datetime.now(UTC)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)

    traffic_24h = _empty_counters()
    traffic_7d = _empty_counters()
    route_totals: dict[str, dict[str, int]] = {}

    metric_rows = (
        db.execute(
            select(UsageMetricHourly).where(UsageMetricHourly.bucket_hour >= cutoff_7d)
        )
        .scalars()
        .all()
    )
    for row in metric_rows:
        bh = (
            row.bucket_hour.astimezone(UTC)
            if row.bucket_hour.tzinfo
            else row.bucket_hour.replace(tzinfo=UTC)
        )
        _add_counters(
            traffic_7d,
            request_count=row.request_count,
            error_count=row.error_count,
            bytes_out=row.bytes_out,
            rate_limited_count=row.rate_limited_count,
        )
        if bh >= cutoff_24h:
            _add_counters(
                traffic_24h,
                request_count=row.request_count,
                error_count=row.error_count,
                bytes_out=row.bytes_out,
                rate_limited_count=row.rate_limited_count,
            )
            rg_bucket = route_totals.setdefault(row.route_group, _empty_counters())
            _add_counters(
                rg_bucket,
                request_count=row.request_count,
                error_count=row.error_count,
                bytes_out=row.bytes_out,
                rate_limited_count=row.rate_limited_count,
            )

    _merge_live_snapshot(
        _get_live_usage_snapshot_safe(),
        now=now,
        cutoff_24h=cutoff_24h,
        cutoff_7d=cutoff_7d,
        traffic_24h=traffic_24h,
        traffic_7d=traffic_7d,
        route_totals=route_totals,
    )

    route_breakdown = [
        {"route_group": rg, **counters}
        for rg, counters in sorted(
            route_totals.items(),
            key=lambda kv: (-kv[1]["request_count"], kv[0]),
        )
    ]

    user_stats = db.execute(
        select(
            func.count(User.id).label("total"),
            func.count(User.id).filter(User.is_active.is_(True)).label("active"),
            func.count(User.id).filter(User.role == "admin").label("admins"),
            func.count(User.id).filter(User.role == "member").label("members"),
        )
    ).one()
    users_total = int(user_stats.total or 0)
    users_active = int(user_stats.active or 0)
    users_admins = int(user_stats.admins or 0)
    users_members = int(user_stats.members or 0)
    users_suspended = users_total - users_active

    invite_stats = db.execute(
        select(
            func.count(InviteCode.id).label("total"),
            func.count(InviteCode.id)
            .filter(
                InviteCode.revoked_at.is_(None),
                InviteCode.use_count < InviteCode.max_uses,
                or_(InviteCode.expires_at.is_(None), InviteCode.expires_at > now),
            )
            .label("active"),
        )
    ).one()
    invites_total = int(invite_stats.total or 0)
    invites_active = int(invite_stats.active or 0)

    content_stats = db.execute(
        select(
            select(func.count(Track.track_id))
            .where(Track.is_active.is_(True))
            .scalar_subquery()
            .label("tracks"),
            select(func.count(TrackSegment.segment_id))
            .where(TrackSegment.is_active.is_(True))
            .scalar_subquery()
            .label("segments"),
            select(func.count(Trip.id)).scalar_subquery().label("trips"),
            select(func.count(TelemetryObservation.id)).scalar_subquery().label("telemetry"),
            select(func.count(ConditionReport.id)).scalar_subquery().label("reports"),
            select(func.count(PlannedRoute.id)).scalar_subquery().label("planned_routes"),
        )
    ).one()
    tracks_count = int(content_stats.tracks or 0)
    segments_count = int(content_stats.segments or 0)
    trips_count = int(content_stats.trips or 0)
    telemetry_count = int(content_stats.telemetry or 0)
    reports_count = int(content_stats.reports or 0)
    planned_routes_count = int(content_stats.planned_routes or 0)

    cfg = get_settings()
    tiles_bytes = _dir_size_bytes(Path(cfg.tiles_dir))
    packs_bytes = _dir_size_bytes(Path(cfg.packs_dir))
    uploads_bytes = _dir_size_bytes(Path(cfg.uploads_dir))
    uploads_quota_bytes = _get_uploads_max_total_mb() * 1024 * 1024
    uploads_usage_pct = (
        round((uploads_bytes / uploads_quota_bytes) * 100.0, 2)
        if uploads_quota_bytes > 0
        else 0.0
    )
    db_size_bytes = _get_db_size_bytes(db)

    feeds = get_feed_health_summary(db, now=now)

    return {
        "registration_mode": get_registration_mode(),
        "generated_at": _iso_or_none(now),
        "traffic_24h": traffic_24h,
        "traffic_7d": traffic_7d,
        "route_breakdown": route_breakdown,
        "users": {
            "total": users_total,
            "active": users_active,
            "admins": users_admins,
            "members": users_members,
            "suspended": users_suspended,
        },
        "invites": {
            "active": invites_active,
            "total": invites_total,
        },
        "content": {
            "tracks": tracks_count,
            "segments": segments_count,
            "trips": trips_count,
            "telemetry_observations": telemetry_count,
            "condition_reports": reports_count,
            "planned_routes": planned_routes_count,
        },
        "storage": {
            "tiles_bytes": tiles_bytes,
            "packs_bytes": packs_bytes,
            "uploads_bytes": uploads_bytes,
            "uploads_quota_bytes": uploads_quota_bytes,
            "uploads_usage_pct": uploads_usage_pct,
            "db_size_bytes": db_size_bytes,
        },
        "feeds": feeds,
    }


@router.get("/users", response_model=AdminUserListResponse)
def list_admin_users(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AdminUserListResponse:
    """List registered accounts for admin moderation with bounded pagination."""
    total = int(db.execute(select(func.count(User.id))).scalar_one())
    rows = (
        db.execute(
            select(User)
            .order_by(User.created_at.desc(), User.id.desc())
            .offset(offset)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    items = [_to_admin_user(u) for u in rows]
    return AdminUserListResponse(users=items, total=total)


@router.patch("/users/{user_id}", response_model=AdminUserItem)
def update_admin_user(
    user_id: uuid.UUID,
    payload: AdminUserUpdateRequest,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AdminUserItem:
    """Update a user's role or suspension state while atomically guarding the last active admin."""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    resulting_role = payload.role if payload.role is not None else target.role
    resulting_active = payload.is_active if payload.is_active is not None else target.is_active
    was_active_admin = target.role == "admin" and bool(target.is_active)
    stays_active_admin = resulting_role == "admin" and bool(resulting_active)

    if was_active_admin and not stays_active_admin:
        # Lock all active admin rows in canonical ID order to prevent concurrent mutual demotion
        locked_admin_ids = (
            db.execute(
                select(User.id)
                .where(User.role == "admin", User.is_active.is_(True))
                .order_by(User.id.asc())
                .with_for_update(key_share=True)
            )
            .scalars()
            .all()
        )
        db.refresh(target, with_for_update={"key_share": True})
        was_active_admin = target.role == "admin" and bool(target.is_active)
        if was_active_admin and len(locked_admin_ids) <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot suspend or demote the last active admin",
            )
    else:
        db.execute(
            select(User.id).where(User.id == user_id).with_for_update(key_share=True)
        ).scalar_one_or_none()
        db.refresh(target)

    now = datetime.now(UTC)
    role_changed = payload.role is not None and payload.role != target.role
    active_changed = payload.is_active is not None and bool(payload.is_active) != bool(
        target.is_active
    )

    if payload.role is not None:
        target.role = payload.role
    if payload.is_active is not None:
        target.is_active = payload.is_active
        target.disabled_at = None if payload.is_active else now
    if role_changed or active_changed:
        target.token_version = int(getattr(target, "token_version", 1) or 1) + 1
    target.updated_at = now
    if payload.is_active is False:
        revoke_community_trust(db, target)

    db.commit()
    db.refresh(target)
    return _to_admin_user(target)


@router.get("/invites", response_model=AdminInviteListResponse)
def list_admin_invites(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AdminInviteListResponse:
    """List invite codes with bounded pagination and computed active/exhausted/expired/revoked."""
    now = datetime.now(UTC)
    total = int(db.execute(select(func.count(InviteCode.id))).scalar_one())
    rows = (
        db.execute(
            select(InviteCode)
            .order_by(InviteCode.created_at.desc(), InviteCode.id.desc())
            .offset(offset)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    items = [_to_admin_invite(inv, now=now) for inv in rows]
    return AdminInviteListResponse(invites=items, total=total)


@router.post("/invites", response_model=AdminInviteItem, status_code=status.HTTP_201_CREATED)
def create_admin_invite(
    payload: CreateInviteRequest,
    admin_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AdminInviteItem:
    """Generate a new ``TF-XXXX-XXXX`` invite code."""
    try:
        invite = create_invite_code(
            db,
            label=payload.label,
            role=payload.role,
            max_uses=payload.max_uses,
            expires_in_days=payload.expires_in_days,
            created_by_user_id=admin_user.id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    db.commit()
    db.refresh(invite)
    return _to_admin_invite(invite)


@router.delete("/invites/{invite_id}", response_model=AdminInviteItem)
def revoke_admin_invite(
    invite_id: uuid.UUID,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AdminInviteItem:
    """Revoke an invite code immediately by setting ``revoked_at``."""
    invite = db.get(InviteCode, invite_id)
    if invite is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invite code not found",
        )
    if invite.revoked_at is None:
        invite.revoked_at = datetime.now(UTC)
        db.commit()
        db.refresh(invite)
    return _to_admin_invite(invite)


@router.get("/ui-settings", response_model=AdminUiSettingsResponse)
def read_admin_ui_settings(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AdminUiSettingsResponse:
    """Return instance-wide default map layer order, visibility, and sidebar telemetry settings."""
    return AdminUiSettingsResponse(**get_admin_ui_settings(db))


@router.put("/ui-settings", response_model=AdminUiSettingsResponse)
def update_admin_ui_settings(
    payload: AdminUiSettingsUpdateRequest,
    admin_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AdminUiSettingsResponse:
    """Update admin-configured instance UI defaults (layer order, on/off defaults, telemetry)."""
    kwargs: dict[str, Any] = {"updated_by_user_id": admin_user.id}
    if "default_layer_order" in payload.model_fields_set:
        kwargs["default_layer_order"] = payload.default_layer_order
    if "default_layer_visibility" in payload.model_fields_set:
        kwargs["default_layer_visibility"] = payload.default_layer_visibility
    if "show_stack_telemetry_public" in payload.model_fields_set:
        kwargs["show_stack_telemetry_public"] = payload.show_stack_telemetry_public

    try:
        updated = upsert_instance_ui_settings(db, **kwargs)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    return AdminUiSettingsResponse(**updated)


@router.delete("/ui-settings", response_model=AdminUiSettingsResponse)
def clear_admin_ui_settings(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AdminUiSettingsResponse:
    """Reset instance-wide admin UI overrides back to factory and environment defaults."""
    return AdminUiSettingsResponse(**reset_instance_ui_settings(db))
