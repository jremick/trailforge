"""FastAPI authentication, scope & role-based access dependencies."""

from __future__ import annotations

import uuid
from typing import Final

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
    resolve_session_only_user,
    verify_oauth_access_token,
    verify_pat,
)
from trailforge.config import Settings, get_settings
from trailforge.db.models import User
from trailforge.db.session import get_db

_SAFE_READ_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})
_SESSION_ONLY_PATH_PREFIXES: Final[tuple[str, ...]] = (
    "/api/auth/tokens",
    "/api/auth/oauth-grants",
)


def _request_settings(request: Request) -> Settings:
    custom = getattr(getattr(request.app, "state", None), "custom_settings", None)
    if isinstance(custom, Settings):
        return custom
    return get_settings()


def _is_session_only_path(path: str) -> bool:
    normalized = path.rstrip("/") or "/"
    for prefix in _SESSION_ONLY_PATH_PREFIXES:
        if normalized == prefix or normalized.startswith(f"{prefix}/"):
            return True
    return False


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
    """Return authenticated active User when valid readable credentials exist, else None."""
    ctx = resolve_request_auth_context(request, db)
    if ctx.user is None or not ctx.user.is_active:
        return None
    if ctx.auth_method in ("pat", "oauth") and not ctx.has_scope(SCOPE_READ):
        return None
    return ctx.user


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """Require an authenticated active user, enforcing PAT/OAuth scopes & token-mgmt isolation."""
    ctx = resolve_request_auth_context(request, db)
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
    if ctx.auth_method in ("pat", "oauth"):
        if _is_session_only_path(request.url.path):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Session authentication required; Personal Access Tokens and OAuth "
                    "tokens cannot manage tokens or grants"
                ),
            )
        method = request.method.upper()
        required_scope = SCOPE_READ if method in _SAFE_READ_METHODS else SCOPE_WRITE
        if not ctx.has_scope(required_scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required scope: {required_scope}",
            )
    return user


require_user = get_current_user


def require_session_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """Require full session credentials (cookie or session Bearer); block PAT/OAuth tokens."""
    return resolve_session_only_user(request, db, settings=_request_settings(request))


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
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return user
