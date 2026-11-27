"""FastAPI authentication, scope & role-based access dependencies."""

from __future__ import annotations

import uuid
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from trailforge.auth.security import verify_access_token
from trailforge.auth.tokens import (
    ALL_SCOPES,
    ANONYMOUS_AUTH_CONTEXT,
    OAUTH_ACCESS_PREFIX,
    PAT_PREFIX,
    SCOPE_READ,
    SCOPE_WRITE,
    McpAuthContext,
    extract_bearer_token,
    extract_session_cookie,
    verify_oauth_access_token,
    verify_pat,
)
from trailforge.config import Settings, get_settings
from trailforge.db.models import User
from trailforge.db.session import get_db


def _request_settings(request: Request) -> Settings:
    custom = getattr(getattr(request.app, "state", None), "custom_settings", None)
    if isinstance(custom, Settings):
        return custom
    return get_settings()


def _resolve_raw_session_user(
    raw_token: str,
    db: Session,
    *,
    settings: Settings | None = None,
) -> User | None:
    """Resolve User from an HMAC session token enforcing ``token_version`` (keeps suspended)."""
    if not raw_token or raw_token.startswith((PAT_PREFIX, OAUTH_ACCESS_PREFIX)):
        return None
    payload = verify_access_token(raw_token, settings=settings)
    if payload is None:
        return None
    try:
        user_uuid = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError):
        return None
    user = db.get(User, user_uuid)
    if user is None:
        return None
    raw_tv = payload.get("tv", 1)
    if isinstance(raw_tv, bool) or not isinstance(raw_tv, int):
        return None
    user_tv = int(getattr(user, "token_version", 1) or 1)
    if int(raw_tv) != user_tv:
        return None
    return user


def extract_token(request: Request) -> str | None:
    """Extract Bearer token or HttpOnly session cookie from ``request``."""
    bearer = extract_bearer_token(request)
    if bearer is not None:
        return bearer
    return extract_session_cookie(request, settings=_request_settings(request))


def resolve_request_auth_context(
    request: Request,
    db: Session,
    *,
    touch_last_used: bool = True,
) -> McpAuthContext:
    """Resolve ``request`` into ``McpAuthContext`` across session, PAT, and OAuth credentials.

    When an ``Authorization: Bearer`` header is present and fails verification, never falls back
    to the ``tf_session`` cookie. For session credentials, retains a suspended ``User`` on the
    context so ``get_current_user`` can still return HTTP 403 ``Account is suspended``.
    """
    cfg = _request_settings(request)
    bearer = extract_bearer_token(request)
    if bearer is not None:
        if bearer.startswith(PAT_PREFIX):
            pat_ctx = verify_pat(bearer, db, touch_last_used=touch_last_used)
            return pat_ctx if pat_ctx is not None else ANONYMOUS_AUTH_CONTEXT
        if bearer.startswith(OAUTH_ACCESS_PREFIX):
            oauth_ctx = verify_oauth_access_token(bearer, db, touch_last_used=touch_last_used)
            return oauth_ctx if oauth_ctx is not None else ANONYMOUS_AUTH_CONTEXT
        session_user = _resolve_raw_session_user(bearer, db, settings=cfg)
        if session_user is None:
            return ANONYMOUS_AUTH_CONTEXT
        return McpAuthContext(
            user=session_user,
            auth_method="session",
            scopes=frozenset(ALL_SCOPES),
        )

    cookie_token = extract_session_cookie(request, settings=cfg)
    if cookie_token is not None:
        session_user = _resolve_raw_session_user(cookie_token, db, settings=cfg)
        if session_user is not None:
            return McpAuthContext(
                user=session_user,
                auth_method="session",
                scopes=frozenset(ALL_SCOPES),
            )

    return ANONYMOUS_AUTH_CONTEXT


def resolve_token_user(request: Request, db: Session) -> User | None:
    """Resolve a User row from a valid session, PAT, or OAuth token."""
    ctx = resolve_request_auth_context(request, db)
    return ctx.user


def get_auth_context(
    request: Request,
    db: Session = Depends(get_db),
) -> McpAuthContext:
    """FastAPI dependency returning ``McpAuthContext`` (anonymous if unauthenticated)."""
    ctx = resolve_request_auth_context(request, db)
    if ctx.user is not None and not ctx.user.is_active:
        return ANONYMOUS_AUTH_CONTEXT
    return ctx


def get_optional_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User | None:
    """Return an active session user when personalizing an otherwise public endpoint."""
    ctx = resolve_request_auth_context(request, db, touch_last_used=False)
    user = ctx.user
    if ctx.auth_method != "session" or user is None or not user.is_active:
        user = None
    require_expected_account(request, user)
    return user


def require_expected_account(request: Request, user: User | None) -> None:
    """Reject stale UI requests when another tab has changed the session cookie."""
    expected = request.headers.get("x-trailforge-account")
    if expected is not None and (user is None or expected != str(user.id)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The signed-in account has changed",
        )


def _require_route_user(request: Request, db: Session, delegated_scope: str | None) -> User:
    ctx = resolve_request_auth_context(request, db, touch_last_used=delegated_scope is not None)
    user = ctx.user
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
    if ctx.auth_method != "session":
        if delegated_scope is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Full session authentication required",
            )
        if not ctx.has_scope(delegated_scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required scope: {delegated_scope}",
            )
    require_expected_account(request, user)
    return user


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Require an active session unless a route explicitly allows delegated access."""
    return _require_route_user(request, db, delegated_scope=None)


def get_scoped_read_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Allow a session or delegated access to one of the consented read operations."""
    return _require_route_user(request, db, delegated_scope=SCOPE_READ)


def get_scoped_write_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Allow a session or delegated access to a consented saved-item write operation."""
    return _require_route_user(request, db, delegated_scope=SCOPE_WRITE)


def _require_browser_queue_account(request: Request) -> None:
    """Require cookie and identifiable browser writers to name the originating account."""
    if request.headers.get("x-trailforge-account"):
        return
    bearer = extract_bearer_token(request)
    if bearer is None or _is_web_session_bearer(request, bearer):
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="Refresh TrailForge before saving to your account",
        )


def _is_web_session_bearer(request: Request, bearer: str) -> bool:
    if bearer.startswith((PAT_PREFIX, OAUTH_ACCESS_PREFIX)):
        return False
    if request.headers.get("sec-fetch-mode") != "cors" or request.headers.get(
        "sec-fetch-site"
    ) not in {"same-origin", "same-site", "cross-site"}:
        return False
    try:
        origin = urlsplit(request.headers.get("origin", ""))
    except ValueError:
        return False
    # Capacitor uses capacitor://localhost on iOS and https://localhost on
    # Android. Those origins and terminal/service clients retain bearer access.
    return (
        origin.scheme in {"http", "https"}
        and bool(origin.hostname)
        and origin.hostname not in {"localhost", "127.0.0.1", "::1"}
    )


def require_browser_queue_owner(
    request: Request, user: User = Depends(get_current_user)
) -> User:
    """Keep full-session writes while requiring account binding for browser requests."""
    _require_browser_queue_account(request)
    return user


def require_browser_queue_write_owner(
    request: Request, user: User = Depends(get_scoped_write_user)
) -> User:
    """Keep consented bearer writes while requiring account binding for browser requests."""
    _require_browser_queue_account(request)
    return user


require_user = get_current_user
require_session_user = get_current_user


def require_admin(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """Require an authenticated active admin via full session auth (401 anon, 403 non-admin/PAT)."""
    ctx = resolve_request_auth_context(request, db, touch_last_used=False)
    user = ctx.user
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
    if ctx.auth_method != "session":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin endpoints require full session authentication",
        )
    require_expected_account(request, user)
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return user
