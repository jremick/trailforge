"""FastAPI authentication & role-based access dependencies."""

from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from trailforge.auth.security import verify_access_token
from trailforge.config import get_settings
from trailforge.db.models import User
from trailforge.db.session import get_db


def extract_token(request: Request) -> str | None:
    """Extract session token from Authorization Bearer header or HttpOnly session cookie."""
    auth_header = request.headers.get("authorization")
    if auth_header:
        scheme, _, creds = auth_header.partition(" ")
        if scheme.lower() == "bearer" and creds.strip():
            return creds.strip()

    cookie_name = get_settings().auth_cookie_name
    cookie_val = request.cookies.get(cookie_name)
    if cookie_val and cookie_val.strip():
        return cookie_val.strip()
    return None


def resolve_token_user(request: Request, db: Session) -> User | None:
    """Resolve a User row from a valid Bearer/cookie token without filtering ``is_active``."""
    token = extract_token(request)
    if not token:
        return None
    payload = verify_access_token(token)
    if payload is None:
        return None
    try:
        user_uuid = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError):
        return None
    return db.get(User, user_uuid)


def get_optional_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User | None:
    """Return authenticated active User when valid credentials exist, else None (never 401)."""
    user = resolve_token_user(request, db)
    if user is None or not user.is_active:
        return None
    return user


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """Require an authenticated active user (401 anonymous, 403 suspended)."""
    user = resolve_token_user(request, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is suspended",
        )
    return user


require_user = get_current_user


def require_admin(
    user: User = Depends(get_current_user),
) -> User:
    """Require an authenticated active admin user (401 anonymous, 403 member/suspended)."""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return user
