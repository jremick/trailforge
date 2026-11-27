"""Personal Access Tokens, scope enforcement, and unified Bearer authentication context.

Supports TrailForge's three credential types behind a single ``McpAuthContext``:
  - ``session``: existing HMAC-signed user session tokens / ``tf_session`` cookie
  - ``pat``: self-hosted Personal Access Tokens (``tf_pat_...``, SHA-256 hash only)
  - ``oauth``: OAuth 2.1 access tokens (``tf_oat_...``, SHA-256 hash only)
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from fastapi import HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from trailforge.auth.security import verify_access_token
from trailforge.config import Settings, get_settings
from trailforge.db.models import User
from trailforge.db.models_ai_auth import OAuthClient, OAuthToken, PersonalAccessToken

SCOPE_READ: Final[str] = "trailforge:read"
SCOPE_WRITE: Final[str] = "trailforge:write"
ALL_SCOPES: Final[tuple[str, ...]] = (SCOPE_READ, SCOPE_WRITE)
VALID_SCOPES: Final[frozenset[str]] = frozenset(ALL_SCOPES)

PAT_PREFIX: Final[str] = "tf_pat_"
OAUTH_ACCESS_PREFIX: Final[str] = "tf_oat_"
OAUTH_REFRESH_PREFIX: Final[str] = "tf_ort_"
OAUTH_CODE_PREFIX: Final[str] = "tf_oac_"
OAUTH_CLIENT_PREFIX: Final[str] = "tf_cli_"
OAUTH_CLIENT_SECRET_PREFIX: Final[str] = "tf_cls_"

MAX_ACTIVE_PATS_PER_USER: Final[int] = 25
MAX_PAT_NAME_LENGTH: Final[int] = 80
MAX_PAT_EXPIRY_DAYS: Final[int] = 3650
_LAST_USED_TOUCH_INTERVAL_S: Final[int] = 60

AuthMethodLiteral = Literal["anonymous", "session", "pat", "oauth"]


class TokenLimitExceededError(ValueError):
    """Raised when a user reaches the maximum active Personal Access Token cap."""


@dataclass(frozen=True, slots=True)
class McpAuthContext:
    """Typed principal context passed to MCP tools and unified auth helpers."""

    user: User | None
    auth_method: AuthMethodLiteral
    scopes: frozenset[str]
    token_id: uuid.UUID | None = None
    client_id: str | None = None

    @property
    def is_authenticated(self) -> bool:
        return self.user is not None and self.auth_method != "anonymous"

    @property
    def can_read(self) -> bool:
        return self.has_scope(SCOPE_READ)

    @property
    def can_write(self) -> bool:
        return self.has_scope(SCOPE_WRITE)

    def has_scope(self, scope: str) -> bool:
        if not self.is_authenticated:
            return False
        if scope == SCOPE_READ:
            return SCOPE_READ in self.scopes or SCOPE_WRITE in self.scopes
        return scope in self.scopes


ANONYMOUS_AUTH_CONTEXT: Final[McpAuthContext] = McpAuthContext(
    user=None,
    auth_method="anonymous",
    scopes=frozenset(),
    token_id=None,
    client_id=None,
)


@dataclass(frozen=True, slots=True)
class PatCreationResult:
    """Result of minting a Personal Access Token (plaintext returned once only)."""

    record: PersonalAccessToken
    plaintext_token: str


def as_utc(dt: datetime) -> datetime:
    """Normalize naive (e.g. SQLite) or aware datetimes to timezone-aware UTC."""
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def normalize_scopes(raw_scopes: Iterable[str] | str) -> list[str]:
    """Validate and canonicalize TrailForge scopes (`write` implies `read`)."""
    items: list[str]
    if isinstance(raw_scopes, str):
        items = [part.strip() for part in raw_scopes.split() if part.strip()]
    else:
        items = []
        for entry in raw_scopes:
            if not isinstance(entry, str):
                raise ValueError("Scopes must be strings")
            for part in entry.split():
                cleaned = part.strip()
                if cleaned:
                    items.append(cleaned)

    if not items:
        raise ValueError("At least one valid scope is required")

    scope_set: set[str] = set()
    for item in items:
        if item not in VALID_SCOPES:
            raise ValueError(f"Unsupported scope: {item!r}")
        scope_set.add(item)

    # Spec Section 5.1: trailforge:write implies trailforge:read
    if SCOPE_WRITE in scope_set:
        scope_set.add(SCOPE_READ)

    return [scope for scope in ALL_SCOPES if scope in scope_set]


def format_scope_string(scopes: Iterable[str] | str) -> str:
    """Format validated scopes into a canonical space-delimited OAuth scope string."""
    return " ".join(normalize_scopes(scopes))


def check_scope(ctx: McpAuthContext, required_scope: str) -> bool:
    """Return True iff ``ctx`` is authenticated and holds ``required_scope``."""
    return ctx.has_scope(required_scope)


def require_scope(ctx: McpAuthContext, required_scope: str) -> None:
    """Raise HTTP 401/403 unless ``ctx`` is authenticated and holds ``required_scope``."""
    if not ctx.is_authenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not ctx.has_scope(required_scope):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing required scope: {required_scope}",
        )


def generate_prefixed_token(prefix: str, *, nbytes: int = 32) -> str:
    """Generate a high-entropy prefixed token (default 256-bit urlsafe secret)."""
    return f"{prefix}{secrets.token_urlsafe(nbytes)}"


def hash_token(raw_token: str) -> str:
    """Return the lowercase 64-char SHA-256 hex digest of a raw secret or token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def constant_time_hash_equals(raw_secret: str, stored_hash: str) -> bool:
    """Compare SHA-256(raw_secret) to a stored hex digest in constant time."""
    if not raw_secret or not stored_hash:
        return False
    return hmac.compare_digest(hash_token(raw_secret), stored_hash)


def token_display_prefix(raw_token: str, *, max_len: int = 16) -> str:
    """Return a non-sensitive display prefix snippet (e.g. ``tf_pat_ab12cd``)."""
    limit = min(max(len(PAT_PREFIX) + 6, 13), max_len)
    return raw_token[:limit]


def _maybe_touch_last_used(
    db: Session,
    obj: PersonalAccessToken | OAuthClient,
    *,
    now: datetime,
) -> None:
    """Throttled, transaction-safe update of ``last_used_at`` without committing caller state."""
    last_used = obj.last_used_at
    if (
        last_used is not None
        and (now - as_utc(last_used)).total_seconds() < _LAST_USED_TOUCH_INTERVAL_S
    ):
        return
    # Never commit unrelated pending ORM changes from the caller's session.
    has_pending_caller_changes = bool(db.new or db.dirty or db.deleted)
    obj.last_used_at = now
    if has_pending_caller_changes:
        db.flush()
    else:
        db.commit()


def create_personal_access_token(
    db: Session,
    user: User,
    *,
    name: str,
    scopes: Iterable[str] | str = (SCOPE_READ,),
    expires_in_days: int | None = None,
    expires_at: datetime | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> PatCreationResult:
    """Mint a scoped ``tf_pat_...`` Personal Access Token and persist only its SHA-256 hash."""
    if not user.is_active:
        raise PermissionError("Account is suspended")

    cleaned_name = name.strip()
    if not cleaned_name or len(cleaned_name) > MAX_PAT_NAME_LENGTH:
        raise ValueError(f"Token name must be between 1 and {MAX_PAT_NAME_LENGTH} characters")

    canonical_scopes = normalize_scopes(scopes)
    current_now = as_utc(now) if now is not None else datetime.now(UTC)

    resolved_expires_at: datetime | None = None
    if expires_at is not None:
        resolved_expires_at = as_utc(expires_at)
        if resolved_expires_at <= current_now:
            raise ValueError("Token expiration must be in the future")
    elif expires_in_days is not None:
        if expires_in_days < 1 or expires_in_days > MAX_PAT_EXPIRY_DAYS:
            raise ValueError(f"expires_in_days must be between 1 and {MAX_PAT_EXPIRY_DAYS}")
        resolved_expires_at = current_now + timedelta(days=int(expires_in_days))

    active_count = (
        db.execute(
            select(func.count(PersonalAccessToken.id)).where(
                PersonalAccessToken.user_id == user.id,
                PersonalAccessToken.revoked_at.is_(None),
                or_(
                    PersonalAccessToken.expires_at.is_(None),
                    PersonalAccessToken.expires_at > current_now,
                ),
            )
        ).scalar_one()
        or 0
    )
    if int(active_count) >= MAX_ACTIVE_PATS_PER_USER:
        raise TokenLimitExceededError(
            f"Maximum of {MAX_ACTIVE_PATS_PER_USER} active personal access tokens reached"
        )

    raw_token = generate_prefixed_token(PAT_PREFIX, nbytes=32)
    record = PersonalAccessToken(
        user_id=user.id,
        user_token_version=int(getattr(user, "token_version", 1) or 1),
        name=cleaned_name,
        token_prefix=token_display_prefix(raw_token),
        token_hash=hash_token(raw_token),
        scopes=canonical_scopes,
        expires_at=resolved_expires_at,
        created_at=current_now,
    )
    db.add(record)
    if commit:
        db.commit()
        db.refresh(record)
    else:
        db.flush()

    return PatCreationResult(record=record, plaintext_token=raw_token)


def list_personal_access_tokens(
    db: Session,
    user_id: uuid.UUID,
    *,
    include_revoked: bool = False,
) -> list[PersonalAccessToken]:
    """List Personal Access Tokens owned by ``user_id`` ordered newest first."""
    stmt = select(PersonalAccessToken).where(PersonalAccessToken.user_id == user_id)
    if not include_revoked:
        stmt = stmt.where(PersonalAccessToken.revoked_at.is_(None))
    stmt = stmt.order_by(PersonalAccessToken.created_at.desc())
    return list(db.execute(stmt).scalars().all())


def revoke_personal_access_token(
    db: Session,
    *,
    token_id: uuid.UUID,
    user_id: uuid.UUID,
    now: datetime | None = None,
    commit: bool = True,
) -> PersonalAccessToken | None:
    """Revoke a PAT owned by ``user_id``; returns None if not found or not owned."""
    pat = db.execute(
        select(PersonalAccessToken).where(
            PersonalAccessToken.id == token_id,
            PersonalAccessToken.user_id == user_id,
        )
    ).scalar_one_or_none()
    if pat is None:
        return None
    if pat.revoked_at is None:
        pat.revoked_at = as_utc(now) if now is not None else datetime.now(UTC)
        if commit:
            db.commit()
            db.refresh(pat)
        else:
            db.flush()
    return pat


def verify_pat(
    raw_token: str,
    db: Session,
    *,
    now: datetime | None = None,
    touch_last_used: bool = True,
) -> McpAuthContext | None:
    """Verify a ``tf_pat_...`` PAT, enforcing revocation, expiry, ``is_active`` & version."""
    if not raw_token or not raw_token.startswith(PAT_PREFIX):
        return None

    digest = hash_token(raw_token)
    pat = db.execute(
        select(PersonalAccessToken).where(PersonalAccessToken.token_hash == digest).limit(1)
    ).scalar_one_or_none()
    if pat is None or not hmac.compare_digest(pat.token_hash, digest):
        return None
    if pat.revoked_at is not None:
        return None

    current_now = as_utc(now) if now is not None else datetime.now(UTC)
    if pat.expires_at is not None and as_utc(pat.expires_at) <= current_now:
        return None

    user = db.get(User, pat.user_id)
    if user is None or not user.is_active:
        return None

    user_tv = int(getattr(user, "token_version", 1) or 1)
    if int(pat.user_token_version) != user_tv:
        return None

    try:
        scopes = frozenset(normalize_scopes(pat.scopes or []))
    except ValueError:
        return None

    if touch_last_used:
        _maybe_touch_last_used(db, pat, now=current_now)

    return McpAuthContext(
        user=user,
        auth_method="pat",
        scopes=scopes,
        token_id=pat.id,
        client_id=None,
    )


def verify_oauth_access_token(
    raw_token: str,
    db: Session,
    *,
    now: datetime | None = None,
    touch_last_used: bool = True,
    expected_resource_origin: str | None = None,
) -> McpAuthContext | None:
    """Verify a ``tf_oat_...`` OAuth access token (revocation, expiry, version, resource)."""
    if not raw_token or not raw_token.startswith(OAUTH_ACCESS_PREFIX):
        return None

    digest = hash_token(raw_token)
    token_row = db.execute(
        select(OAuthToken).where(OAuthToken.access_token_hash == digest).limit(1)
    ).scalar_one_or_none()
    if token_row is None or not hmac.compare_digest(token_row.access_token_hash, digest):
        return None
    if token_row.revoked_at is not None:
        return None

    current_now = as_utc(now) if now is not None else datetime.now(UTC)
    if as_utc(token_row.access_expires_at) <= current_now:
        return None

    # RFC 8707 audience check when token carries a resource indicator and canonical origin is known
    if token_row.resource:
        canon_origin = expected_resource_origin
        if not canon_origin:
            canon_origin = str(getattr(get_settings(), "public_base_url", "") or "").strip() or None
        if canon_origin:
            norm_origin = canon_origin.rstrip("/").lower()
            norm_res = token_row.resource.strip().rstrip("/").lower()
            if norm_res not in {f"{norm_origin}/mcp", f"{norm_origin}/mcp/auth"}:
                return None

    user = db.get(User, token_row.user_id)
    if user is None or not user.is_active:
        return None

    user_tv = int(getattr(user, "token_version", 1) or 1)
    if int(token_row.user_token_version) != user_tv:
        return None

    try:
        scopes = frozenset(normalize_scopes(token_row.scope))
    except ValueError:
        return None

    if touch_last_used:
        client = db.get(OAuthClient, token_row.client_id)
        if client is not None:
            _maybe_touch_last_used(db, client, now=current_now)

    return McpAuthContext(
        user=user,
        auth_method="oauth",
        scopes=scopes,
        token_id=token_row.id,
        client_id=token_row.client_id,
    )


def verify_session_token(
    raw_token: str,
    db: Session,
    *,
    settings: Settings | None = None,
) -> McpAuthContext | None:
    """Verify an HMAC-signed session token, enforcing ``is_active`` and ``token_version``."""
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
    if user is None or not user.is_active:
        return None
    raw_tv = payload.get("tv", 1)
    if isinstance(raw_tv, bool) or not isinstance(raw_tv, int):
        return None
    user_tv = int(getattr(user, "token_version", 1) or 1)
    if int(raw_tv) != user_tv:
        return None
    return McpAuthContext(
        user=user,
        auth_method="session",
        scopes=frozenset(ALL_SCOPES),
        token_id=None,
        client_id=None,
    )


def resolve_bearer_token(
    raw_token: str,
    db: Session,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    touch_last_used: bool = True,
) -> McpAuthContext | None:
    """Unified Bearer resolver for PATs, OAuth access tokens, and existing session tokens."""
    cleaned = raw_token.strip()
    if not cleaned:
        return None
    if cleaned.startswith(PAT_PREFIX):
        return verify_pat(cleaned, db, now=now, touch_last_used=touch_last_used)
    if cleaned.startswith(OAUTH_ACCESS_PREFIX):
        return verify_oauth_access_token(cleaned, db, now=now, touch_last_used=touch_last_used)
    return verify_session_token(cleaned, db, settings=settings)


def has_bearer_header(request: Request) -> bool:
    """Return True if the request carries an ``Authorization: Bearer ...`` header."""
    auth_header = (request.headers.get("authorization") or "").strip()
    if not auth_header:
        return False
    scheme, sep, creds = auth_header.partition(" ")
    return bool(sep and scheme.lower() == "bearer" and creds.strip())


def extract_bearer_token(request: Request) -> str | None:
    """Extract the raw token from an ``Authorization: Bearer`` header, if present."""
    auth_header = (request.headers.get("authorization") or "").strip()
    if not auth_header:
        return None
    scheme, sep, creds = auth_header.partition(" ")
    if sep and scheme.lower() == "bearer" and creds.strip():
        return creds.strip()
    return None


def extract_session_cookie(
    request: Request,
    *,
    settings: Settings | None = None,
) -> str | None:
    """Extract the HttpOnly ``tf_session`` cookie value, if present."""
    cfg = settings or get_settings()
    cookie_val = request.cookies.get(cfg.auth_cookie_name)
    if cookie_val and cookie_val.strip():
        return cookie_val.strip()
    return None


def resolve_auth_context(
    request: Request,
    db: Session,
    *,
    allow_cookie: bool = True,
    settings: Settings | None = None,
    now: datetime | None = None,
    touch_last_used: bool = True,
) -> McpAuthContext:
    """Resolve an HTTP request into a typed ``McpAuthContext``.

    If an ``Authorization: Bearer`` header is supplied and fails verification, never
    silently falls back to the session cookie.
    """
    bearer = extract_bearer_token(request)
    if bearer is not None:
        resolved = resolve_bearer_token(
            bearer,
            db,
            settings=settings,
            now=now,
            touch_last_used=touch_last_used,
        )
        return resolved if resolved is not None else ANONYMOUS_AUTH_CONTEXT

    if allow_cookie:
        cookie_token = extract_session_cookie(request, settings=settings)
        if cookie_token is not None:
            resolved = verify_session_token(cookie_token, db, settings=settings)
            if resolved is not None:
                return resolved

    return ANONYMOUS_AUTH_CONTEXT


def _resolve_raw_session_user(
    raw_token: str,
    db: Session,
    *,
    settings: Settings | None = None,
) -> User | None:
    """Verify session token signature + ``token_version`` without hiding suspended state."""
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


def resolve_session_cookie_or_bearer_user(
    request: Request,
    db: Session,
    *,
    settings: Settings | None = None,
) -> User | None:
    """Return an active User iff authenticated via session cookie or full session Bearer token."""
    bearer = extract_bearer_token(request)
    if bearer is not None:
        ctx = verify_session_token(bearer, db, settings=settings)
        return ctx.user if ctx is not None else None
    cookie_token = extract_session_cookie(request, settings=settings)
    if cookie_token is not None:
        ctx = verify_session_token(cookie_token, db, settings=settings)
        return ctx.user if ctx is not None else None
    return None


def resolve_session_only_user(
    request: Request,
    db: Session,
    *,
    settings: Settings | None = None,
) -> User:
    """Require full user session auth (cookie or session Bearer); reject PATs & OAuth tokens.

    Enforces Spec Section 5.1 so leaked AI tokens cannot mint replacement PATs or
    tamper with OAuth grants.
    """
    bearer = extract_bearer_token(request)
    if bearer is not None:
        if bearer.startswith((PAT_PREFIX, OAUTH_ACCESS_PREFIX)):
            ctx = resolve_bearer_token(bearer, db, settings=settings, touch_last_used=False)
            if ctx is not None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        "Session authentication required; Personal Access Tokens and OAuth "
                        "tokens cannot manage tokens or grants"
                    ),
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user = _resolve_raw_session_user(bearer, db, settings=settings)
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

    cookie_token = extract_session_cookie(request, settings=settings)
    if cookie_token is not None:
        user = _resolve_raw_session_user(cookie_token, db, settings=settings)
        if user is not None:
            if not user.is_active:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Account is suspended",
                )
            return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


__all__ = [
    "ALL_SCOPES",
    "ANONYMOUS_AUTH_CONTEXT",
    "AuthMethodLiteral",
    "MAX_ACTIVE_PATS_PER_USER",
    "MAX_PAT_EXPIRY_DAYS",
    "MAX_PAT_NAME_LENGTH",
    "McpAuthContext",
    "OAUTH_ACCESS_PREFIX",
    "OAUTH_CLIENT_PREFIX",
    "OAUTH_CLIENT_SECRET_PREFIX",
    "OAUTH_CODE_PREFIX",
    "OAUTH_REFRESH_PREFIX",
    "PAT_PREFIX",
    "PatCreationResult",
    "SCOPE_READ",
    "SCOPE_WRITE",
    "TokenLimitExceededError",
    "VALID_SCOPES",
    "as_utc",
    "check_scope",
    "constant_time_hash_equals",
    "create_personal_access_token",
    "extract_bearer_token",
    "extract_session_cookie",
    "format_scope_string",
    "generate_prefixed_token",
    "has_bearer_header",
    "hash_token",
    "list_personal_access_tokens",
    "normalize_scopes",
    "require_scope",
    "resolve_auth_context",
    "resolve_bearer_token",
    "resolve_session_cookie_or_bearer_user",
    "resolve_session_only_user",
    "revoke_personal_access_token",
    "token_display_prefix",
    "verify_oauth_access_token",
    "verify_pat",
    "verify_session_token",
]
