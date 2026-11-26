"""Self-hostable user registration, login, logout, and optional session inspection API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trailforge.auth.dependencies import get_optional_current_user
from trailforge.auth.security import (
    create_access_token,
    hash_password,
    normalize_email,
    verify_password,
)
from trailforge.config import get_settings
from trailforge.db.models import User, VehicleProfile
from trailforge.db.session import get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=254)
    display_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)
    ]
    password: str = Field(..., min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=254)
    password: str = Field(..., min_length=1, max_length=128)


class UserPublic(BaseModel):
    id: str
    email: str
    display_name: str
    active_vehicle_profile_id: str | None = None


class AuthTokenResponse(BaseModel):
    user: UserPublic
    access_token: str
    token_type: str = "bearer"


class AuthMeResponse(BaseModel):
    authenticated: bool
    user: UserPublic | None = None


def _get_active_vehicle_id(db: Session, user: User) -> str | None:
    vid = db.execute(
        select(VehicleProfile.id)
        .where(VehicleProfile.user_id == user.id, VehicleProfile.is_default.is_(True))
        .limit(1)
    ).scalar_one_or_none()
    return str(vid) if vid is not None else None


def _to_user_public(db: Session, user: User) -> UserPublic:
    return UserPublic(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
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


@router.post("/register", response_model=AuthTokenResponse, status_code=status.HTTP_201_CREATED)
def register_user(
    payload: RegisterRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> AuthTokenResponse:
    """Create a self-hosted user account and issue a signed Bearer token + HttpOnly cookie."""
    try:
        email = normalize_email(payload.email)
        pw_hash = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    existing = db.execute(
        select(User).where(func.lower(User.email) == email).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        )

    user = User(
        email=email,
        display_name=payload.display_name.strip(),
        password_hash=pw_hash,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(str(user.id), user.email)
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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        ) from exc

    user = db.execute(
        select(User).where(func.lower(User.email) == email).limit(1)
    ).scalar_one_or_none()
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password, user.password_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    token = create_access_token(str(user.id), user.email)
    _set_session_cookie(response, token)
    return AuthTokenResponse(user=_to_user_public(db, user), access_token=token)


@router.post("/logout")
def logout_user(response: Response) -> dict[str, bool]:
    """Clear the HttpOnly session cookie."""
    cfg = get_settings()
    response.delete_cookie(key=cfg.auth_cookie_name, path="/")
    return {"ok": True}


@router.get("/me", response_model=AuthMeResponse)
def get_me(
    user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
) -> AuthMeResponse:
    """Return active session state; returns `authenticated: false` cleanly when anonymous."""
    if user is None:
        return AuthMeResponse(authenticated=False, user=None)
    return AuthMeResponse(authenticated=True, user=_to_user_public(db, user))
