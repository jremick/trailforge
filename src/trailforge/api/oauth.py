"""FastAPI routers for OAuth 2.1 discovery, PKCE+DCR flows, and Account Panel PAT/grants APIs.

Exports three routers ready to include in ``trailforge.api.main``:
  - ``well_known_router``: ``/.well-known/oauth-protected-resource*`` &
    ``/.well-known/oauth-authorization-server``
  - ``oauth_router``: ``/api/oauth/{register,authorize,token,revoke}``
  - ``tokens_router``: ``/api/auth/tokens*`` & ``/api/auth/oauth-grants*``
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Final
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trailforge.api.middleware import extract_client_ip
from trailforge.auth.oauth import (
    OAuthError,
    build_authorization_server_metadata,
    build_protected_resource_metadata,
    build_redirect_url,
    create_oauth_csrf_nonce,
    exchange_authorization_code,
    get_mcp_auth_mode,
    is_mcp_enabled,
    issue_authorization_code,
    list_user_oauth_grants,
    refresh_oauth_token,
    register_oauth_client,
    render_oauth_consent_html,
    render_oauth_error_html,
    render_oauth_login_html,
    resolve_public_origin,
    revoke_oauth_token,
    revoke_user_oauth_grant,
    validate_authorize_request,
    verify_oauth_csrf_nonce,
)
from trailforge.auth.security import (
    create_access_token,
    normalize_email,
    verify_password,
)
from trailforge.auth.tokens import (
    MAX_ACTIVE_PATS_PER_USER,
    MAX_PAT_EXPIRY_DAYS,
    MAX_PAT_NAME_LENGTH,
    SCOPE_READ,
    TokenLimitExceededError,
    as_utc,
    create_personal_access_token,
    list_personal_access_tokens,
    resolve_session_cookie_or_bearer_user,
    resolve_session_only_user,
    revoke_personal_access_token,
)
from trailforge.config import Settings, get_settings
from trailforge.db.models import User
from trailforge.db.models_ai_auth import PersonalAccessToken
from trailforge.db.session import get_db

well_known_router = APIRouter(tags=["oauth"])
oauth_router = APIRouter(prefix="/api/oauth", tags=["oauth"])
tokens_router = APIRouter(prefix="/api/auth", tags=["auth"])

# Pre-formatted dummy scrypt hash matching hash_password() parameters (closes timing oracle)
_DUMMY_PASSWORD_HASH: Final[str] = (
    "scrypt$16384$8$1$"
    "000102030405060708090a0b0c0d0e0f$"
    "0000000000000000000000000000000000000000000000000000000000000000"
)

_DISCOVERY_CORS_HEADERS: Final[dict[str, str]] = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Accept, MCP-Protocol-Version",
    "Access-Control-Expose-Headers": (
        "Mcp-Session-Id, WWW-Authenticate, X-RateLimit-Limit, X-RateLimit-Remaining"
    ),
    "Cache-Control": "public, max-age=300",
}

_NO_STORE_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}

_CONSENT_SECURITY_HEADERS: Final[dict[str, str]] = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
}


def _origin_from_redirect_uri(redirect_uri: str | None) -> str | None:
    """Extract bare ``scheme://netloc`` origin from a validated OAuth redirect URI."""
    if not redirect_uri:
        return None
    parsed = urlsplit(redirect_uri)
    scheme = parsed.scheme.lower()
    if scheme not in ("https", "http") or not parsed.netloc:
        return None
    origin = f"{scheme}://{parsed.netloc}"
    if any(ch in origin for ch in (" ", "\t", "\r", "\n", ";", ",")):
        return None
    return origin


def _build_consent_security_headers(redirect_uri: str | None = None) -> dict[str, str]:
    """Build consent-page security headers permitting form-redirect to the validated client origin.

    Chromium and Safari enforce CSP3 ``form-action`` across 302 redirects following form
    submission, so the consent page must allow both ``'self'`` and the validated redirect
    origin while keeping ``X-Frame-Options: DENY`` and ``frame-ancestors 'none'``.
    """
    redirect_origin = _origin_from_redirect_uri(redirect_uri)
    form_action = f"'self' {redirect_origin}" if redirect_origin else "'self'"
    return {
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            f"default-src 'none'; style-src 'unsafe-inline'; form-action {form_action}; "
            "frame-ancestors 'none'; base-uri 'none'"
        ),
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
    }


def _request_settings(request: Request) -> Settings:
    custom = getattr(getattr(request.app, "state", None), "custom_settings", None)
    if custom is not None:
        return custom
    return get_settings()


def _ensure_mcp_enabled(cfg: Settings, *, require_oauth: bool = False) -> None:
    if not is_mcp_enabled(cfg):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if require_oauth and get_mcp_auth_mode(cfg) == "anonymous_only":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _oauth_error_response(exc: OAuthError) -> JSONResponse:
    headers = dict(_NO_STORE_HEADERS)
    headers.update(exc.headers)
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict(), headers=headers)


def _session_binding_for_user(user: User) -> str:
    return f"user:{user.id}:tv:{int(getattr(user, 'token_version', 1) or 1)}"


async def _parse_form_or_json(request: Request) -> dict[str, Any]:
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            body = await request.json()
            return body if isinstance(body, dict) else {}
        except Exception:
            return {}
    try:
        form = await request.form()
        return {str(k): str(v) for k, v in form.items()}
    except Exception:
        return {}


def _set_session_cookie(response: Response, token: str, cfg: Settings) -> None:
    response.set_cookie(
        key=cfg.auth_cookie_name,
        value=token,
        max_age=cfg.auth_token_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=cfg.auth_cookie_secure,
        path="/",
    )


# -----------------------------------------------------------------------------
# RFC 9728 & RFC 8414 Discovery Endpoints
# -----------------------------------------------------------------------------


@well_known_router.get("/.well-known/oauth-protected-resource")
def oauth_protected_resource_root(request: Request) -> JSONResponse:
    """RFC 9728 OAuth 2.0 Protected Resource Metadata for ``/mcp``."""
    cfg = _request_settings(request)
    _ensure_mcp_enabled(cfg)
    origin = resolve_public_origin(request, settings=cfg)
    return JSONResponse(
        content=build_protected_resource_metadata(origin, resource_path="/mcp"),
        headers=_DISCOVERY_CORS_HEADERS,
    )


@well_known_router.options("/.well-known/oauth-protected-resource")
def oauth_protected_resource_root_options(request: Request) -> Response:
    cfg = _request_settings(request)
    _ensure_mcp_enabled(cfg)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=_DISCOVERY_CORS_HEADERS)


@well_known_router.get("/.well-known/oauth-protected-resource/mcp")
def oauth_protected_resource_mcp(request: Request) -> JSONResponse:
    """RFC 9728 path-scoped Protected Resource Metadata for ``/mcp``."""
    cfg = _request_settings(request)
    _ensure_mcp_enabled(cfg)
    origin = resolve_public_origin(request, settings=cfg)
    return JSONResponse(
        content=build_protected_resource_metadata(origin, resource_path="/mcp"),
        headers=_DISCOVERY_CORS_HEADERS,
    )


@well_known_router.options("/.well-known/oauth-protected-resource/mcp")
def oauth_protected_resource_mcp_options(request: Request) -> Response:
    cfg = _request_settings(request)
    _ensure_mcp_enabled(cfg)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=_DISCOVERY_CORS_HEADERS)


@well_known_router.get("/.well-known/oauth-protected-resource/mcp/auth")
def oauth_protected_resource_mcp_auth(request: Request) -> JSONResponse:
    """RFC 9728 path-scoped Protected Resource Metadata for ``/mcp/auth``."""
    cfg = _request_settings(request)
    _ensure_mcp_enabled(cfg, require_oauth=True)
    origin = resolve_public_origin(request, settings=cfg)
    return JSONResponse(
        content=build_protected_resource_metadata(origin, resource_path="/mcp/auth"),
        headers=_DISCOVERY_CORS_HEADERS,
    )


@well_known_router.options("/.well-known/oauth-protected-resource/mcp/auth")
def oauth_protected_resource_mcp_auth_options(request: Request) -> Response:
    cfg = _request_settings(request)
    _ensure_mcp_enabled(cfg, require_oauth=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=_DISCOVERY_CORS_HEADERS)


@well_known_router.get("/.well-known/oauth-authorization-server")
def oauth_authorization_server_metadata(request: Request) -> JSONResponse:
    """RFC 8414 OAuth 2.1 Authorization Server Metadata."""
    cfg = _request_settings(request)
    _ensure_mcp_enabled(cfg, require_oauth=True)
    origin = resolve_public_origin(request, settings=cfg)
    return JSONResponse(
        content=build_authorization_server_metadata(origin),
        headers=_DISCOVERY_CORS_HEADERS,
    )


@well_known_router.options("/.well-known/oauth-authorization-server")
def oauth_authorization_server_metadata_options(request: Request) -> Response:
    cfg = _request_settings(request)
    _ensure_mcp_enabled(cfg, require_oauth=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=_DISCOVERY_CORS_HEADERS)


# -----------------------------------------------------------------------------
# OAuth 2.1 Endpoints: Register (DCR), Authorize (Consent), Token, Revoke
# -----------------------------------------------------------------------------


class DcrRegisterRequest(BaseModel):
    client_name: str = Field(..., min_length=1, max_length=120)
    redirect_uris: list[str] = Field(..., min_length=1, max_length=10)
    grant_types: list[str] | None = None
    response_types: list[str] | None = None
    token_endpoint_auth_method: str = Field(default="none", max_length=32)


@oauth_router.post("/register", status_code=status.HTTP_201_CREATED)
def oauth_register_client(
    payload: DcrRegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """RFC 7591 Dynamic Client Registration with strict redirect & abuse bounds."""
    cfg = _request_settings(request)
    _ensure_mcp_enabled(cfg, require_oauth=True)

    client_ip = extract_client_ip(
        request,
        trust_proxy_headers=bool(getattr(cfg, "trust_proxy_headers", True)),
    )
    try:
        reg = register_oauth_client(
            db,
            client_name=payload.client_name,
            redirect_uris=payload.redirect_uris,
            grant_types=payload.grant_types,
            response_types=payload.response_types,
            token_endpoint_auth_method=payload.token_endpoint_auth_method,
            client_ip=client_ip,
            settings=cfg,
        )
    except OAuthError as exc:
        return _oauth_error_response(exc)

    body: dict[str, Any] = {
        "client_id": reg.client.client_id,
        "client_name": reg.client.client_name,
        "redirect_uris": list(reg.client.redirect_uris),
        "grant_types": list(reg.client.grant_types),
        "response_types": reg.response_types,
        "token_endpoint_auth_method": reg.client.token_endpoint_auth_method,
        "client_id_issued_at": int(as_utc(reg.client.created_at).timestamp()),
    }
    if reg.client_secret is not None:
        body["client_secret"] = reg.client_secret
        body["client_secret_expires_at"] = 0

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=body,
        headers=_NO_STORE_HEADERS,
    )


@oauth_router.get("/authorize")
def oauth_authorize_get(
    request: Request,
    client_id: str | None = Query(default=None),
    redirect_uri: str | None = Query(default=None),
    response_type: str | None = Query(default=None),
    scope: str | None = Query(default=None),
    state: str | None = Query(default=""),
    code_challenge: str | None = Query(default=None),
    code_challenge_method: str | None = Query(default=None),
    resource: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Render server-side auto-escaped OAuth 2.1 sign-in or consent page."""
    cfg = _request_settings(request)
    _ensure_mcp_enabled(cfg, require_oauth=True)

    try:
        params = validate_authorize_request(
            db,
            client_id=client_id,
            redirect_uri=redirect_uri,
            response_type=response_type,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            state=state,
            resource=resource,
        )
    except OAuthError as exc:
        return HTMLResponse(
            content=render_oauth_error_html(
                error=exc.error, error_description=exc.error_description
            ),
            status_code=exc.status_code,
            headers=_CONSENT_SECURITY_HEADERS,
        )

    user = resolve_session_cookie_or_bearer_user(request, db, settings=cfg)
    if user is None:
        csrf_token = create_oauth_csrf_nonce(
            session_binding="anon",
            action="login",
            client_id=params.client.client_id,
            redirect_uri=params.redirect_uri,
            code_challenge=params.code_challenge,
            state=params.state,
            scope=params.scope,
            resource=params.resource,
            settings=cfg,
        )
        return HTMLResponse(
            content=render_oauth_login_html(params=params, csrf_token=csrf_token),
            status_code=status.HTTP_200_OK,
            headers=_CONSENT_SECURITY_HEADERS,
        )

    csrf_token = create_oauth_csrf_nonce(
        session_binding=_session_binding_for_user(user),
        action="consent",
        client_id=params.client.client_id,
        redirect_uri=params.redirect_uri,
        code_challenge=params.code_challenge,
        state=params.state,
        scope=params.scope,
        resource=params.resource,
        settings=cfg,
    )
    return HTMLResponse(
        content=render_oauth_consent_html(params=params, user=user, csrf_token=csrf_token),
        status_code=status.HTTP_200_OK,
        headers=_build_consent_security_headers(params.redirect_uri),
    )


@oauth_router.post("/authorize")
async def oauth_authorize_post(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Process same-origin inline login or CSRF-protected OAuth 2.1 consent approval/denial."""
    cfg = _request_settings(request)
    _ensure_mcp_enabled(cfg, require_oauth=True)

    form = await _parse_form_or_json(request)
    try:
        params = validate_authorize_request(
            db,
            client_id=form.get("client_id"),
            redirect_uri=form.get("redirect_uri"),
            response_type=form.get("response_type"),
            scope=form.get("scope"),
            code_challenge=form.get("code_challenge"),
            code_challenge_method=form.get("code_challenge_method"),
            state=form.get("state", ""),
            resource=form.get("resource") or None,
        )
    except OAuthError as exc:
        return HTMLResponse(
            content=render_oauth_error_html(
                error=exc.error, error_description=exc.error_description
            ),
            status_code=exc.status_code,
            headers=_CONSENT_SECURITY_HEADERS,
        )

    action = (form.get("action") or "consent").strip().lower()
    csrf_token = form.get("csrf_token")

    if action == "login":
        if not verify_oauth_csrf_nonce(
            csrf_token,
            session_binding="anon",
            action="login",
            client_id=params.client.client_id,
            redirect_uri=params.redirect_uri,
            code_challenge=params.code_challenge,
            state=params.state,
            scope=params.scope,
            resource=params.resource,
            settings=cfg,
        ):
            return HTMLResponse(
                content=render_oauth_error_html(
                    error="invalid_request",
                    error_description="Invalid or expired CSRF token",
                ),
                status_code=status.HTTP_403_FORBIDDEN,
                headers=_CONSENT_SECURITY_HEADERS,
            )

        raw_email = str(form.get("email") or "")
        raw_password = str(form.get("password") or "")
        fresh_login_csrf = create_oauth_csrf_nonce(
            session_binding="anon",
            action="login",
            client_id=params.client.client_id,
            redirect_uri=params.redirect_uri,
            code_challenge=params.code_challenge,
            state=params.state,
            scope=params.scope,
            resource=params.resource,
            settings=cfg,
        )

        try:
            email = normalize_email(raw_email)
        except ValueError:
            verify_password(raw_password, _DUMMY_PASSWORD_HASH)
            return HTMLResponse(
                content=render_oauth_login_html(
                    params=params,
                    csrf_token=fresh_login_csrf,
                    error_message="Invalid email or password",
                ),
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers=_CONSENT_SECURITY_HEADERS,
            )

        user = db.execute(
            select(User).where(func.lower(User.email) == email).limit(1)
        ).scalar_one_or_none()
        if user is None:
            verify_password(raw_password, _DUMMY_PASSWORD_HASH)
            return HTMLResponse(
                content=render_oauth_login_html(
                    params=params,
                    csrf_token=fresh_login_csrf,
                    error_message="Invalid email or password",
                ),
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers=_CONSENT_SECURITY_HEADERS,
            )
        if not verify_password(raw_password, user.password_hash):
            return HTMLResponse(
                content=render_oauth_login_html(
                    params=params,
                    csrf_token=fresh_login_csrf,
                    error_message="Invalid email or password",
                ),
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers=_CONSENT_SECURITY_HEADERS,
            )
        if not user.is_active:
            return HTMLResponse(
                content=render_oauth_login_html(
                    params=params,
                    csrf_token=fresh_login_csrf,
                    error_message="Account is suspended",
                ),
                status_code=status.HTTP_403_FORBIDDEN,
                headers=_CONSENT_SECURITY_HEADERS,
            )

        session_token = create_access_token(
            str(user.id),
            user.email,
            token_version=int(getattr(user, "token_version", 1) or 1),
            settings=cfg,
        )
        consent_csrf = create_oauth_csrf_nonce(
            session_binding=_session_binding_for_user(user),
            action="consent",
            client_id=params.client.client_id,
            redirect_uri=params.redirect_uri,
            code_challenge=params.code_challenge,
            state=params.state,
            scope=params.scope,
            resource=params.resource,
            settings=cfg,
        )
        resp = HTMLResponse(
            content=render_oauth_consent_html(params=params, user=user, csrf_token=consent_csrf),
            status_code=status.HTTP_200_OK,
            headers=_build_consent_security_headers(params.redirect_uri),
        )
        _set_session_cookie(resp, session_token, cfg)
        return resp

    # Consent approval or denial requires an active user session + bound CSRF nonce
    user = resolve_session_cookie_or_bearer_user(request, db, settings=cfg)
    if user is None:
        return HTMLResponse(
            content=render_oauth_error_html(
                error="login_required",
                error_description="Sign in required before granting consent",
            ),
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers=_CONSENT_SECURITY_HEADERS,
        )

    if not verify_oauth_csrf_nonce(
        csrf_token,
        session_binding=_session_binding_for_user(user),
        action="consent",
        client_id=params.client.client_id,
        redirect_uri=params.redirect_uri,
        code_challenge=params.code_challenge,
        state=params.state,
        scope=params.scope,
        resource=params.resource,
        settings=cfg,
    ):
        return HTMLResponse(
            content=render_oauth_error_html(
                error="invalid_request",
                error_description="Invalid or expired CSRF token",
            ),
            status_code=status.HTTP_403_FORBIDDEN,
            headers=_CONSENT_SECURITY_HEADERS,
        )

    decision = (form.get("decision") or "").strip().lower()
    consent_headers = _build_consent_security_headers(params.redirect_uri)
    if decision != "approve":
        deny_params = {"error": "access_denied", "error_description": "User denied authorization"}
        if params.state:
            deny_params["state"] = params.state
        return RedirectResponse(
            url=build_redirect_url(params.redirect_uri, deny_params),
            status_code=status.HTTP_302_FOUND,
            headers=consent_headers,
        )

    try:
        raw_code = issue_authorization_code(db, params=params, user=user, settings=cfg)
    except OAuthError as exc:
        return HTMLResponse(
            content=render_oauth_error_html(
                error=exc.error, error_description=exc.error_description
            ),
            status_code=exc.status_code,
            headers=_CONSENT_SECURITY_HEADERS,
        )

    redirect_params = {"code": raw_code}
    if params.state:
        redirect_params["state"] = params.state
    return RedirectResponse(
        url=build_redirect_url(params.redirect_uri, redirect_params),
        status_code=status.HTTP_302_FOUND,
        headers=consent_headers,
    )


@oauth_router.post("/token")
async def oauth_token_endpoint(
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """OAuth 2.1 token endpoint (``authorization_code`` + PKCE S256 & ``refresh_token``)."""
    cfg = _request_settings(request)
    _ensure_mcp_enabled(cfg, require_oauth=True)

    form = await _parse_form_or_json(request)
    grant_type = str(form.get("grant_type") or "").strip()

    try:
        if grant_type == "authorization_code":
            payload = exchange_authorization_code(
                db,
                code=form.get("code"),
                redirect_uri=form.get("redirect_uri"),
                client_id=form.get("client_id"),
                code_verifier=form.get("code_verifier"),
                client_secret=form.get("client_secret"),
                settings=cfg,
            )
        elif grant_type == "refresh_token":
            payload = refresh_oauth_token(
                db,
                refresh_token=form.get("refresh_token"),
                client_id=form.get("client_id"),
                scope=form.get("scope"),
                client_secret=form.get("client_secret"),
                settings=cfg,
            )
        else:
            raise OAuthError(
                "unsupported_grant_type",
                "grant_type must be 'authorization_code' or 'refresh_token'",
            )
    except OAuthError as exc:
        return _oauth_error_response(exc)

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=payload.to_dict(),
        headers=_NO_STORE_HEADERS,
    )


@oauth_router.post("/revoke")
async def oauth_revoke_endpoint(
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """RFC 7009 OAuth 2.0 Token Revocation endpoint."""
    cfg = _request_settings(request)
    _ensure_mcp_enabled(cfg, require_oauth=True)

    form = await _parse_form_or_json(request)
    try:
        revoke_oauth_token(
            db,
            token=form.get("token"),
            token_type_hint=form.get("token_type_hint"),
            client_id=form.get("client_id"),
            client_secret=form.get("client_secret"),
        )
    except OAuthError as exc:
        return _oauth_error_response(exc)

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"revoked": True},
        headers=_NO_STORE_HEADERS,
    )


# -----------------------------------------------------------------------------
# Account Panel Management Endpoints: Personal Access Tokens & OAuth Grants
# (Strictly requires session cookie or full session Bearer — NEVER PAT/OAuth)
# -----------------------------------------------------------------------------

_PatNameStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_PAT_NAME_LENGTH),
]


class PatCreateRequest(BaseModel):
    name: _PatNameStr
    scopes: list[str] | str = Field(default_factory=lambda: [SCOPE_READ])
    expires_in_days: int | None = Field(default=None, ge=1, le=MAX_PAT_EXPIRY_DAYS)


class PatItemResponse(BaseModel):
    id: str
    name: str
    token_prefix: str
    scopes: list[str]
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    is_active: bool
    created_at: datetime


class PatListResponse(BaseModel):
    tokens: list[PatItemResponse]
    max_active_tokens: int = MAX_ACTIVE_PATS_PER_USER


class PatCreatedResponse(BaseModel):
    id: str
    name: str
    token: str
    token_prefix: str
    scopes: list[str]
    expires_at: datetime | None = None
    created_at: datetime


class PatRevokeResponse(BaseModel):
    ok: bool = True
    id: str
    revoked_at: datetime


class OAuthGrantItemResponse(BaseModel):
    client_id: str
    client_name: str
    scopes: list[str]
    granted_at: datetime
    last_used_at: datetime | None = None
    active_token_count: int


class OAuthGrantListResponse(BaseModel):
    grants: list[OAuthGrantItemResponse]


class OAuthGrantRevokeResponse(BaseModel):
    ok: bool = True
    client_id: str
    revoked_tokens: int


def _require_session_user(request: Request, db: Session = Depends(get_db)) -> User:
    return resolve_session_only_user(request, db, settings=_request_settings(request))


def _pat_to_item(pat: PersonalAccessToken, *, now: datetime) -> PatItemResponse:
    expired = pat.expires_at is not None and as_utc(pat.expires_at) <= now
    return PatItemResponse(
        id=str(pat.id),
        name=pat.name,
        token_prefix=pat.token_prefix,
        scopes=list(pat.scopes or []),
        expires_at=pat.expires_at,
        last_used_at=pat.last_used_at,
        revoked_at=pat.revoked_at,
        is_active=(pat.revoked_at is None and not expired),
        created_at=pat.created_at,
    )


@tokens_router.get("/tokens", response_model=PatListResponse)
def list_auth_tokens(
    include_revoked: bool = Query(default=False),
    user: User = Depends(_require_session_user),
    db: Session = Depends(get_db),
) -> PatListResponse:
    """List the authenticated user's Personal Access Tokens (never exposes token hashes/secrets)."""
    now = datetime.now(UTC)
    records = list_personal_access_tokens(db, user.id, include_revoked=include_revoked)
    return PatListResponse(
        tokens=[_pat_to_item(pat, now=now) for pat in records],
        max_active_tokens=MAX_ACTIVE_PATS_PER_USER,
    )


@tokens_router.post(
    "/tokens",
    response_model=PatCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_auth_token(
    payload: PatCreateRequest,
    response: Response,
    user: User = Depends(_require_session_user),
    db: Session = Depends(get_db),
) -> PatCreatedResponse:
    """Mint a new scoped Personal Access Token (`tf_pat_...`); plaintext returned once only."""
    try:
        created = create_personal_access_token(
            db,
            user,
            name=payload.name,
            scopes=payload.scopes,
            expires_in_days=payload.expires_in_days,
        )
    except TokenLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return PatCreatedResponse(
        id=str(created.record.id),
        name=created.record.name,
        token=created.plaintext_token,
        token_prefix=created.record.token_prefix,
        scopes=list(created.record.scopes),
        expires_at=created.record.expires_at,
        created_at=created.record.created_at,
    )


@tokens_router.delete("/tokens/{token_id}", response_model=PatRevokeResponse)
def delete_auth_token(
    token_id: str,
    user: User = Depends(_require_session_user),
    db: Session = Depends(get_db),
) -> PatRevokeResponse:
    """Revoke an owned Personal Access Token immediately."""
    try:
        parsed_id = uuid.UUID(token_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Personal access token not found",
        ) from exc

    pat = revoke_personal_access_token(db, token_id=parsed_id, user_id=user.id)
    if pat is None or pat.revoked_at is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Personal access token not found",
        )
    return PatRevokeResponse(ok=True, id=str(pat.id), revoked_at=pat.revoked_at)


@tokens_router.get("/oauth-grants", response_model=OAuthGrantListResponse)
def get_oauth_grants(
    user: User = Depends(_require_session_user),
    db: Session = Depends(get_db),
) -> OAuthGrantListResponse:
    """List connected OAuth AI applications with active grants for the current user."""
    grants = list_user_oauth_grants(db, user)
    return OAuthGrantListResponse(
        grants=[OAuthGrantItemResponse(**item) for item in grants]
    )


@tokens_router.delete("/oauth-grants/{client_id}", response_model=OAuthGrantRevokeResponse)
def delete_oauth_grant(
    client_id: str,
    user: User = Depends(_require_session_user),
    db: Session = Depends(get_db),
) -> OAuthGrantRevokeResponse:
    """Revoke all active OAuth tokens for a connected AI application."""
    revoked_count = revoke_user_oauth_grant(db, user_id=user.id, client_id=client_id)
    if revoked_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Connected OAuth application grant not found",
        )
    return OAuthGrantRevokeResponse(
        ok=True,
        client_id=client_id,
        revoked_tokens=revoked_count,
    )


__all__ = [
    "oauth_router",
    "tokens_router",
    "well_known_router",
]
