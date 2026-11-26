"""Self-hostable user registration, login, logout, config, and session inspection API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trailforge.auth.dependencies import resolve_request_auth_context
from trailforge.auth.invites import (
    get_admin_email,
    get_demo_banner,
    get_registration_mode,
    normalize_invite_code,
)
from trailforge.auth.security import (
    create_access_token,
    hash_password,
    normalize_email,
    verify_password,
)
from trailforge.auth.tokens import SCOPE_READ
from trailforge.config import get_settings
from trailforge.db.models import InviteCode, User, VehicleProfile
from trailforge.db.session import get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Pre-formatted dummy scrypt hash matching hash_password() parameters (N=16384, r=8, p=1, dklen=32)
# so login_user always runs full scrypt work even when an email does not exist (closes M-6 oracle).
_DUMMY_PASSWORD_HASH = (
    "scrypt$16384$8$1$"
    "000102030405060708090a0b0c0d0e0f$"
    "0000000000000000000000000000000000000000000000000000000000000000"
)


class RegisterRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=254)
    display_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)
    ]
    password: str = Field(..., min_length=8, max_length=128)
    invite_code: str | None = Field(default=None, max_length=64)


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=254)
    password: str = Field(..., min_length=1, max_length=128)


class UserPublic(BaseModel):
    id: str
    email: str
    display_name: str
    role: Literal["admin", "member"] = "member"
    is_active: bool = True
    active_vehicle_profile_id: str | None = None


class AuthTokenResponse(BaseModel):
    user: UserPublic
    access_token: str
    token_type: str = "bearer"


class AuthMeResponse(BaseModel):
    authenticated: bool
    user: UserPublic | None = None


class AuthConfigResponse(BaseModel):
    registration_mode: Literal["open", "invite_only", "closed"]
    demo_banner: str
    allow_anonymous_browsing: bool = True


def _get_active_vehicle_id(db: Session, user: User) -> str | None:
    vid = db.execute(
        select(VehicleProfile.id)
        .where(VehicleProfile.user_id == user.id, VehicleProfile.is_default.is_(True))
        .limit(1)
    ).scalar_one_or_none()
    return str(vid) if vid is not None else None


def _to_user_public(db: Session, user: User) -> UserPublic:
    role: Literal["admin", "member"] = "admin" if user.role == "admin" else "member"
    return UserPublic(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=role,
        is_active=bool(user.is_active),
        active_vehicle_profile_id=_get_active_vehicle_id(db, user),
    )


def _set_session_cookie(response: Response, token: str) -> None:
    cfg = get_settings()
    response.set_cookie(
        key=cfg.auth_cookie_name,
        value=token,
        max_age=cfg.auth_token_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=cfg.auth_cookie_secure,
        path="/",
    )


@router.get("/config", response_model=AuthConfigResponse)
def get_auth_config() -> AuthConfigResponse:
    """Return public registration mode and optional demo banner for frontend boot."""
    return AuthConfigResponse(
        registration_mode=get_registration_mode(),
        demo_banner=get_demo_banner(),
        allow_anonymous_browsing=True,
    )


@router.post("/register", response_model=AuthTokenResponse, status_code=status.HTTP_201_CREATED)
def register_user(
    payload: RegisterRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> AuthTokenResponse:
    """Create a user account respecting registration_mode, invites, and admin bootstrap."""
    try:
        email = normalize_email(payload.email)
        pw_hash = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    mode = get_registration_mode()
    if mode == "closed":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is closed",
        )

    # First-admin bootstrap is only allowed when zero admins exist AND email matches admin_email.
    bootstrap_email = get_admin_email()
    is_admin_bootstrap = False
    if bootstrap_email and email == bootstrap_email:
        has_existing_admin = (
            db.execute(select(User.id).where(User.role == "admin").limit(1)).scalar_one_or_none()
            is not None
        )
        is_admin_bootstrap = not has_existing_admin

    assigned_role = "member"
    used_invite_code: str | None = None
    raw_code = normalize_invite_code(payload.invite_code) if payload.invite_code else ""
    invite: InviteCode | None = None

    # Validate invite code BEFORE checking email uniqueness to prevent unauthenticated
    # email enumeration in invite_only mode (M-6).
    if is_admin_bootstrap:
        assigned_role = "admin"
    elif mode == "invite_only" and not raw_code:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="An invite code is required to register",
        )
    elif raw_code:
        now = datetime.now(UTC)
        invite = db.execute(
            select(InviteCode)
            .where(func.upper(InviteCode.code) == raw_code)
            .with_for_update()
        ).scalar_one_or_none()
        if invite is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid invite code",
            )
        if invite.revoked_at is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invite code has been revoked",
            )
        if invite.expires_at is not None:
            exp = (
                invite.expires_at.astimezone(UTC)
                if invite.expires_at.tzinfo
                else invite.expires_at.replace(tzinfo=UTC)
            )
            if exp <= now:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Invite code has expired",
                )
        if invite.use_count >= invite.max_uses:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invite code has reached its maximum uses",
            )

    existing = db.execute(
        select(User).where(func.lower(User.email) == email).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        )

    if invite is not None:
        updated = db.execute(
            update(InviteCode)
            .where(
                InviteCode.id == invite.id,
                InviteCode.revoked_at.is_(None),
                InviteCode.use_count < InviteCode.max_uses,
            )
            .values(use_count=InviteCode.use_count + 1)
            .returning(InviteCode.code, InviteCode.role)
        ).one_or_none()
        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invite code has reached its maximum uses",
            )
        used_invite_code, assigned_role = updated

    user = User(
        email=email,
        display_name=payload.display_name.strip(),
        password_hash=pw_hash,
        role=assigned_role,
        is_active=True,
        invited_by_code=used_invite_code,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        ) from exc
    db.refresh(user)

    token = create_access_token(
        str(user.id),
        user.email,
        token_version=int(getattr(user, "token_version", 1) or 1),
    )
    _set_session_cookie(response, token)
    return AuthTokenResponse(user=_to_user_public(db, user), access_token=token)


@router.post("/login", response_model=AuthTokenResponse)
def login_user(
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> AuthTokenResponse:
    """Authenticate an existing account and issue a signed Bearer token + HttpOnly cookie."""
    try:
        email = normalize_email(payload.email)
    except ValueError as exc:
        verify_password(payload.password, _DUMMY_PASSWORD_HASH)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        ) from exc

    user = db.execute(
        select(User).where(func.lower(User.email) == email).limit(1)
    ).scalar_one_or_none()
    if user is None:
        # Constant-work dummy scrypt verification closes the user-existence timing oracle (M-6)
        verify_password(payload.password, _DUMMY_PASSWORD_HASH)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is suspended",
        )

    token = create_access_token(
        str(user.id),
        user.email,
        token_version=int(getattr(user, "token_version", 1) or 1),
    )
    _set_session_cookie(response, token)
    return AuthTokenResponse(user=_to_user_public(db, user), access_token=token)


@router.post("/logout")
def logout_user(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    """Revoke active tokens server-side (incrementing ``token_version``) and clear cookie.

    Requires full session credentials; PATs and OAuth tokens are rejected with HTTP 403
    so a delegated AI token cannot globally terminate all user sessions.
    """
    ctx = resolve_request_auth_context(request, db, touch_last_used=False)
    if ctx.auth_method in ("pat", "oauth"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Session authentication required; Personal Access Tokens and OAuth tokens "
                "cannot log out user sessions"
            ),
        )
    if ctx.auth_method == "session" and ctx.user is not None:
        ctx.user.token_version = int(getattr(ctx.user, "token_version", 1) or 1) + 1
        ctx.user.updated_at = datetime.now(UTC)
        db.commit()
    cfg = get_settings()
    response.delete_cookie(key=cfg.auth_cookie_name, path="/")
    return {"ok": True}


@router.get("/me", response_model=AuthMeResponse)
def get_me(
    request: Request,
    db: Session = Depends(get_db),
) -> AuthMeResponse:
    """Return active session state; anonymous gets `authenticated: false`, suspended gets `403`."""
    ctx = resolve_request_auth_context(request, db)
    user = ctx.user
    if user is None:
        return AuthMeResponse(authenticated=False, user=None)
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is suspended",
        )
    if ctx.auth_method in ("pat", "oauth") and not ctx.has_scope(SCOPE_READ):
        return AuthMeResponse(authenticated=False, user=None)
    return AuthMeResponse(authenticated=True, user=_to_user_public(db, user))
