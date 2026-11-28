"""Self-hosted OAuth 2.1 + PKCE (S256) + Dynamic Client Registration (RFC 7591) core logic.

Implements:
  - RFC 9728 Protected Resource Metadata & RFC 8414 Authorization Server Metadata
  - Strict redirect URI validation rejecting open redirects, wildcards, credentials,
    fragments, ``javascript:``, ``data:``, and non-loopback ``http://``
  - Mandatory PKCE ``S256`` challenge/verifier validation (RFC 7636)
  - RFC 7591 DCR with HMAC IP hashing, daily per-IP cap, stale-client pruning, and table cap
  - Single-use 60 s authorization codes with atomic consumption
  - Rotating refresh tokens with token-family reuse detection & family-wide revocation
  - RFC 7009 token revocation
  - Server-side auto-escaped login & consent HTML with HMAC-signed CSRF nonces
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Request
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from trailforge.auth.tokens import (
    ALL_SCOPES,
    OAUTH_ACCESS_PREFIX,
    OAUTH_CLIENT_PREFIX,
    OAUTH_CLIENT_SECRET_PREFIX,
    OAUTH_CODE_PREFIX,
    OAUTH_REFRESH_PREFIX,
    SCOPE_READ,
    SCOPE_WRITE,
    as_utc,
    constant_time_hash_equals,
    format_scope_string,
    generate_prefixed_token,
    hash_token,
    normalize_scopes,
)
from trailforge.config import Settings, get_settings
from trailforge.db.models import User
from trailforge.db.models_ai_auth import OAuthAuthorizationCode, OAuthClient, OAuthToken

McpAuthModeLiteral = Literal["hybrid", "oauth_required", "anonymous_only"]

DEFAULT_ACCESS_TOKEN_TTL_S: Final[int] = 3600
DEFAULT_REFRESH_TOKEN_TTL_S: Final[int] = 2_592_000  # 30 days
DEFAULT_AUTH_CODE_TTL_S: Final[int] = 60
DEFAULT_DCR_MAX_PER_IP_PER_DAY: Final[int] = 20
DEFAULT_OAUTH_MAX_CLIENTS: Final[int] = 5000
DEFAULT_UNUSED_CLIENT_TTL_DAYS: Final[int] = 90
DEFAULT_CSRF_NONCE_TTL_S: Final[int] = 600

MAX_REDIRECT_URIS_PER_CLIENT: Final[int] = 10
MAX_REDIRECT_URI_LENGTH: Final[int] = 2048
MAX_CLIENT_NAME_LENGTH: Final[int] = 120
MAX_RESOURCE_URI_LENGTH: Final[int] = 512

ALLOWED_GRANT_TYPES: Final[frozenset[str]] = frozenset({"authorization_code", "refresh_token"})
ALLOWED_AUTH_METHODS: Final[frozenset[str]] = frozenset({"none", "client_secret_post"})

_PKCE_CHALLENGE_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_PKCE_VERIFIER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_HOSTNAME_LABEL_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_HOST_HEADER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9.\-:\[\]]+$")

_CSRF_DOMAIN_SEP: Final[bytes] = b"trailforge:oauth-csrf:v1"
_IP_HASH_DOMAIN_SEP: Final[bytes] = b"trailforge:oauth-dcr-ip:v1"

SCOPE_DESCRIPTIONS: Final[dict[str, str]] = {
    SCOPE_READ: "Read your rigs, saved routes, pins, favorites & trip summaries",
    SCOPE_WRITE: "Save routes, pins & favorites",
}


class OAuthError(Exception):
    """RFC 6749 / OAuth 2.1 structured error."""

    def __init__(
        self,
        error: str,
        error_description: str,
        *,
        status_code: int = 400,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(error_description)
        self.error = error
        self.error_description = error_description
        self.status_code = status_code
        self.headers = headers or {}

    def to_dict(self) -> dict[str, str]:
        return {
            "error": self.error,
            "error_description": self.error_description,
        }


class OAuthRateLimitError(OAuthError):
    """Raised when Dynamic Client Registration exceeds per-IP daily quota."""

    def __init__(self, error_description: str, *, retry_after_s: int = 3600) -> None:
        super().__init__(
            "rate_limit_exceeded",
            error_description,
            status_code=429,
            headers={"Retry-After": str(max(1, int(retry_after_s)))},
        )
        self.retry_after_s = max(1, int(retry_after_s))


@dataclass(frozen=True, slots=True)
class ClientRegistrationResult:
    """RFC 7591 client registration result (plaintext secret returned once if confidential)."""

    client: OAuthClient
    client_secret: str | None
    response_types: list[str]


@dataclass(frozen=True, slots=True)
class ValidatedAuthorizeParams:
    """Validated parameters for an OAuth 2.1 authorization request."""

    client: OAuthClient
    redirect_uri: str
    scope: str
    scopes: list[str]
    state: str
    code_challenge: str
    code_challenge_method: str
    resource: str | None


@dataclass(frozen=True, slots=True)
class TokenResponsePayload:
    """RFC 6749 / OAuth 2.1 token endpoint payload."""

    access_token: str
    token_type: str
    expires_in: int
    scope: str
    refresh_token: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "scope": self.scope,
        }
        if self.refresh_token is not None:
            data["refresh_token"] = self.refresh_token
        return data


# -----------------------------------------------------------------------------
# Safe Settings Helpers (works both before and after config.py adds MCP fields)
# -----------------------------------------------------------------------------


def _parse_bool(raw: Any, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    return default


def _parse_int(raw: Any, default: int, *, minimum: int = 1) -> int:
    try:
        val = int(raw)
        return val if val >= minimum else default
    except (TypeError, ValueError):
        return default


def _read_setting(
    settings: Settings | None,
    attr: str,
    env_names: tuple[str, ...],
    default: Any,
) -> Any:
    cfg = settings or get_settings()
    if hasattr(cfg, attr):
        return getattr(cfg, attr)
    for name in env_names:
        val = os.environ.get(name)
        if val is not None and val.strip() != "":
            return val.strip()
    return default


def is_mcp_enabled(settings: Settings | None = None) -> bool:
    raw = _read_setting(
        settings,
        "enable_mcp",
        ("TRAILFORGE_ENABLE_MCP", "ENABLE_MCP"),
        True,
    )
    return _parse_bool(raw, True)


def get_mcp_auth_mode(settings: Settings | None = None) -> McpAuthModeLiteral:
    raw = str(
        _read_setting(
            settings,
            "mcp_auth_mode",
            ("TRAILFORGE_MCP_AUTH_MODE", "MCP_AUTH_MODE"),
            "hybrid",
        )
        or "hybrid"
    ).strip().lower()
    if raw in ("hybrid", "oauth_required", "anonymous_only"):
        return raw  # type: ignore[return-value]
    return "hybrid"


def get_public_base_url(settings: Settings | None = None) -> str:
    raw = str(
        _read_setting(
            settings,
            "public_base_url",
            ("TRAILFORGE_PUBLIC_BASE_URL", "PUBLIC_BASE_URL"),
            "",
        )
        or ""
    ).strip()
    return raw.rstrip("/")


def get_oauth_access_token_ttl_s(settings: Settings | None = None) -> int:
    raw = _read_setting(
        settings,
        "oauth_access_token_ttl_s",
        ("TRAILFORGE_OAUTH_ACCESS_TOKEN_TTL_S", "OAUTH_ACCESS_TOKEN_TTL_S"),
        DEFAULT_ACCESS_TOKEN_TTL_S,
    )
    return _parse_int(raw, DEFAULT_ACCESS_TOKEN_TTL_S, minimum=60)


def get_oauth_refresh_token_ttl_s(settings: Settings | None = None) -> int:
    raw = _read_setting(
        settings,
        "oauth_refresh_token_ttl_s",
        ("TRAILFORGE_OAUTH_REFRESH_TOKEN_TTL_S", "OAUTH_REFRESH_TOKEN_TTL_S"),
        DEFAULT_REFRESH_TOKEN_TTL_S,
    )
    return _parse_int(raw, DEFAULT_REFRESH_TOKEN_TTL_S, minimum=300)


def get_oauth_dcr_max_per_ip_per_day(settings: Settings | None = None) -> int:
    raw = _read_setting(
        settings,
        "oauth_dcr_max_per_ip_per_day",
        ("TRAILFORGE_OAUTH_DCR_MAX_PER_IP_PER_DAY", "OAUTH_DCR_MAX_PER_IP_PER_DAY"),
        DEFAULT_DCR_MAX_PER_IP_PER_DAY,
    )
    return _parse_int(raw, DEFAULT_DCR_MAX_PER_IP_PER_DAY, minimum=1)


def get_oauth_max_clients(settings: Settings | None = None) -> int:
    raw = _read_setting(
        settings,
        "oauth_max_clients",
        ("TRAILFORGE_OAUTH_MAX_CLIENTS", "OAUTH_MAX_CLIENTS"),
        DEFAULT_OAUTH_MAX_CLIENTS,
    )
    return _parse_int(raw, DEFAULT_OAUTH_MAX_CLIENTS, minimum=1)


def get_oauth_unused_client_ttl_days(settings: Settings | None = None) -> int:
    raw = _read_setting(
        settings,
        "oauth_unused_client_ttl_days",
        ("TRAILFORGE_OAUTH_UNUSED_CLIENT_TTL_DAYS",),
        DEFAULT_UNUSED_CLIENT_TTL_DAYS,
    )
    return _parse_int(raw, DEFAULT_UNUSED_CLIENT_TTL_DAYS, minimum=1)


def get_oauth_auth_code_ttl_s(settings: Settings | None = None) -> int:
    raw = _read_setting(
        settings,
        "oauth_auth_code_ttl_s",
        ("TRAILFORGE_OAUTH_AUTH_CODE_TTL_S",),
        DEFAULT_AUTH_CODE_TTL_S,
    )
    return _parse_int(raw, DEFAULT_AUTH_CODE_TTL_S, minimum=10)


# -----------------------------------------------------------------------------
# Public Origin Resolution & RFC 9728 / RFC 8414 Metadata Builders
# -----------------------------------------------------------------------------


def _sanitize_host(raw_host: str | None) -> str | None:
    if not raw_host:
        return None
    candidate = raw_host.split(",", 1)[0].strip()
    if not candidate or len(candidate) > 255:
        return None
    if not _HOST_HEADER_RE.match(candidate):
        return None
    if "@" in candidate or "/" in candidate or "\\" in candidate:
        return None
    return candidate


def resolve_public_origin(request: Request, *, settings: Settings | None = None) -> str:
    """Resolve canonical public origin without leaking internal Railway/container hostnames."""
    cfg = settings or get_settings()
    configured = get_public_base_url(cfg)
    if configured:
        parsed = urlsplit(configured)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")

    scheme = (request.url.scheme or "http").lower()
    host = _sanitize_host(request.headers.get("host")) or _sanitize_host(request.url.netloc)

    client_host = request.client.host if request.client is not None else None
    if bool(getattr(cfg, "trust_proxy_headers", False)):
        from trailforge.api.middleware import is_trusted_proxy_peer

        if is_trusted_proxy_peer(client_host, getattr(cfg, "trusted_proxy_cidrs", None)):
            xf_proto = (
                (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
            )
            if xf_proto in ("http", "https"):
                scheme = xf_proto
            xf_host = _sanitize_host(request.headers.get("x-forwarded-host"))
            if xf_host:
                host = xf_host

    resolved_host = host or "localhost"
    return f"{scheme}://{resolved_host}".rstrip("/")


def build_protected_resource_metadata(
    origin: str,
    *,
    resource_path: str = "/mcp",
) -> dict[str, Any]:
    """Build RFC 9728 OAuth 2.0 Protected Resource Metadata document."""
    clean_origin = origin.rstrip("/")
    clean_path = "/" + resource_path.lstrip("/")
    return {
        "resource": f"{clean_origin}{clean_path}",
        "authorization_servers": [clean_origin],
        "bearer_methods_supported": ["header"],
        "scopes_supported": list(ALL_SCOPES),
        "resource_name": "TrailForge MCP",
    }


def build_authorization_server_metadata(origin: str) -> dict[str, Any]:
    """Build RFC 8414 OAuth 2.1 Authorization Server Metadata document."""
    clean_origin = origin.rstrip("/")
    return {
        "issuer": clean_origin,
        "authorization_endpoint": f"{clean_origin}/api/oauth/authorize",
        "token_endpoint": f"{clean_origin}/api/oauth/token",
        "registration_endpoint": f"{clean_origin}/api/oauth/register",
        "revocation_endpoint": f"{clean_origin}/api/oauth/revoke",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "revocation_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": list(ALL_SCOPES),
    }


def _quote_header_param(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")


def build_www_authenticate_header(
    origin: str,
    *,
    resource_path: str = "/mcp/auth",
    scopes: Iterable[str] = ALL_SCOPES,
    error: str | None = None,
    error_description: str | None = None,
) -> str:
    """Format RFC 9728 ``WWW-Authenticate: Bearer`` challenge header for 401 responses."""
    clean_origin = origin.rstrip("/")
    clean_path = "/" + resource_path.lstrip("/")
    metadata_url = f"{clean_origin}/.well-known/oauth-protected-resource{clean_path}"
    scope_str = format_scope_string(scopes)
    parts = [
        f'resource_metadata="{_quote_header_param(metadata_url)}"',
        f'scope="{_quote_header_param(scope_str)}"',
    ]
    if error:
        parts.append(f'error="{_quote_header_param(error)}"')
    if error_description:
        parts.append(f'error_description="{_quote_header_param(error_description)}"')
    return "Bearer " + ", ".join(parts)


# -----------------------------------------------------------------------------
# Strict Redirect URI Validation & PKCE S256
# -----------------------------------------------------------------------------


def _is_valid_domain_name(hostname: str) -> bool:
    if not hostname or len(hostname) > 253:
        return False
    if hostname.endswith(".") or hostname.startswith(".") or ".." in hostname:
        return False
    labels = hostname.split(".")
    return all(_HOSTNAME_LABEL_RE.match(label) is not None for label in labels)


def _is_valid_https_host(hostname: str) -> bool:
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return _is_valid_domain_name(hostname)
    return not ip.is_unspecified


def _is_loopback_host(hostname: str) -> bool:
    lowered = hostname.lower()
    if lowered == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return ip in (ipaddress.IPv4Address("127.0.0.1"), ipaddress.IPv6Address("::1"))


def validate_redirect_uri(uri: str) -> str:
    """Strictly validate an OAuth 2.1 redirect URI (rejecting open redirects & dangerous schemes).

    Accepts ONLY:
      - ``https://`` URLs with a valid host, optional valid port, no credentials, no fragment
      - Loopback ``http://localhost[:port]/...``, ``http://127.0.0.1[:port]/...``,
        ``http://[::1][:port]/...``
    """
    if not isinstance(uri, str) or not uri:
        raise OAuthError("invalid_redirect_uri", "redirect_uri must be a non-empty string")
    if len(uri) > MAX_REDIRECT_URI_LENGTH:
        raise OAuthError("invalid_redirect_uri", "redirect_uri exceeds maximum length")
    if uri != uri.strip():
        raise OAuthError(
            "invalid_redirect_uri",
            "redirect_uri must not have leading/trailing whitespace",
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in uri):
        raise OAuthError(
            "invalid_redirect_uri",
            "redirect_uri contains illegal control or whitespace characters",
        )
    if "\\" in uri or "*" in uri or "#" in uri:
        raise OAuthError(
            "invalid_redirect_uri",
            "redirect_uri must not contain wildcards, backslashes, or fragments",
        )

    parsed = urlsplit(uri)
    scheme = parsed.scheme.lower()
    if scheme not in ("https", "http"):
        raise OAuthError(
            "invalid_redirect_uri",
            "redirect_uri must use https:// or loopback http://",
        )
    if parsed.fragment:
        raise OAuthError("invalid_redirect_uri", "redirect_uri must not contain a fragment")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise OAuthError(
            "invalid_redirect_uri",
            "redirect_uri must not contain userinfo credentials",
        )

    hostname = parsed.hostname
    if not hostname:
        raise OAuthError("invalid_redirect_uri", "redirect_uri must include a valid host")

    try:
        port = parsed.port
    except ValueError as exc:
        raise OAuthError("invalid_redirect_uri", "redirect_uri has an invalid port") from exc
    if port is not None and (port < 1 or port > 65535):
        raise OAuthError("invalid_redirect_uri", "redirect_uri port out of range")

    if scheme == "https":
        if not _is_valid_https_host(hostname):
            raise OAuthError("invalid_redirect_uri", "redirect_uri has an invalid HTTPS host")
    else:
        if not _is_loopback_host(hostname):
            raise OAuthError(
                "invalid_redirect_uri",
                "http:// redirect_uri is only permitted for loopback (localhost, 127.0.0.1, [::1])",
            )

    return uri


def validate_resource_indicator(
    resource: str | None,
    *,
    expected_origin: str | None = None,
) -> str | None:
    """Validate optional RFC 8707 resource indicator URI against canonical MCP URIs."""
    if resource is None:
        return None
    cleaned = resource.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_RESOURCE_URI_LENGTH:
        raise OAuthError("invalid_target", "resource parameter exceeds maximum length")
    if "#" in cleaned or any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in cleaned):
        raise OAuthError("invalid_target", "resource parameter contains invalid characters")
    parsed = urlsplit(cleaned)
    if parsed.scheme.lower() not in ("https", "http") or not parsed.hostname or parsed.fragment:
        raise OAuthError(
            "invalid_target",
            "resource must be an absolute http(s) URI without fragment",
        )
    if expected_origin:
        norm_origin = expected_origin.strip().rstrip("/").lower()
        norm_res = cleaned.rstrip("/").lower()
        allowed_targets = {f"{norm_origin}/mcp", f"{norm_origin}/mcp/auth"}
        if norm_res not in allowed_targets:
            raise OAuthError(
                "invalid_target",
                "resource must match this TrailForge server's canonical /mcp or /mcp/auth URI",
            )
    return cleaned


def compute_pkce_s256(code_verifier: str) -> str:
    """Compute RFC 7636 S256 code_challenge from a PKCE code_verifier."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def validate_code_challenge(
    code_challenge: str | None,
    code_challenge_method: str | None,
) -> str:
    """Enforce mandatory PKCE S256 code_challenge and method."""
    if code_challenge_method != "S256":
        raise OAuthError(
            "invalid_request",
            "PKCE S256 is required (code_challenge_method must be 'S256')",
        )
    if not code_challenge or not _PKCE_CHALLENGE_RE.match(code_challenge):
        raise OAuthError(
            "invalid_request",
            "Valid PKCE S256 code_challenge is required",
        )
    return code_challenge


def verify_pkce_s256(code_verifier: str | None, expected_challenge: str) -> bool:
    """Verify a PKCE code_verifier against its stored S256 code_challenge in constant time."""
    if not code_verifier or not _PKCE_VERIFIER_RE.match(code_verifier):
        return False
    actual = compute_pkce_s256(code_verifier)
    return hmac.compare_digest(actual, expected_challenge)


# -----------------------------------------------------------------------------
# RFC 7591 Dynamic Client Registration & Abuse Bounds
# -----------------------------------------------------------------------------


def hash_client_ip(client_ip: str | None, *, settings: Settings | None = None) -> str | None:
    """Return an HMAC-SHA256 hex digest of the client IP (never stores raw IP addresses)."""
    if not client_ip or not client_ip.strip():
        return None
    cfg = settings or get_settings()
    secret_key = str(getattr(cfg, "privacy_secret_key", "") or cfg.auth_secret_key).encode("utf-8")
    return hmac.new(
        secret_key,
        _IP_HASH_DOMAIN_SEP + b":" + client_ip.strip().encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def prune_stale_oauth_clients(
    db: Session,
    *,
    now: datetime | None = None,
    unused_ttl_days: int | None = None,
) -> int:
    """Remove unused DCR clients older than ``unused_ttl_days`` with zero active tokens."""
    current_now = as_utc(now) if now is not None else datetime.now(UTC)
    ttl_days = (
        unused_ttl_days if unused_ttl_days is not None else get_oauth_unused_client_ttl_days()
    )
    cutoff = current_now - timedelta(days=ttl_days)

    active_token_clients = select(OAuthToken.client_id).where(
        OAuthToken.revoked_at.is_(None),
        or_(
            OAuthToken.access_expires_at > current_now,
            and_(
                OAuthToken.refresh_expires_at.is_not(None),
                OAuthToken.refresh_expires_at > current_now,
            ),
        ),
    )
    res = db.execute(
        delete(OAuthClient).where(
            or_(
                and_(OAuthClient.last_used_at.is_(None), OAuthClient.created_at < cutoff),
                and_(OAuthClient.last_used_at.is_not(None), OAuthClient.last_used_at < cutoff),
            ),
            OAuthClient.client_id.not_in(active_token_clients),
        )
    )
    return int(res.rowcount or 0)


def register_oauth_client(
    db: Session,
    *,
    client_name: str,
    redirect_uris: list[str],
    grant_types: list[str] | None = None,
    response_types: list[str] | None = None,
    token_endpoint_auth_method: str = "none",
    client_ip: str | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> ClientRegistrationResult:
    """Register a dynamic OAuth 2.1 client (RFC 7591) with redirect, rate, and capacity guards."""
    cfg = settings or get_settings()
    current_now = as_utc(now) if now is not None else datetime.now(UTC)

    cleaned_name = (client_name or "").strip()
    if not cleaned_name or len(cleaned_name) > MAX_CLIENT_NAME_LENGTH:
        raise OAuthError(
            "invalid_client_metadata",
            f"client_name must be between 1 and {MAX_CLIENT_NAME_LENGTH} characters",
        )

    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise OAuthError("invalid_redirect_uri", "At least one redirect_uri is required")
    if len(redirect_uris) > MAX_REDIRECT_URIS_PER_CLIENT:
        raise OAuthError(
            "invalid_redirect_uri",
            f"At most {MAX_REDIRECT_URIS_PER_CLIENT} redirect_uris are permitted",
        )
    validated_uris: list[str] = []
    seen_uris: set[str] = set()
    for raw_uri in redirect_uris:
        valid_uri = validate_redirect_uri(raw_uri)
        if valid_uri not in seen_uris:
            seen_uris.add(valid_uri)
            validated_uris.append(valid_uri)

    resolved_grants = (
        list(grant_types) if grant_types is not None else ["authorization_code", "refresh_token"]
    )
    if not resolved_grants or any(g not in ALLOWED_GRANT_TYPES for g in resolved_grants):
        raise OAuthError(
            "invalid_client_metadata",
            "grant_types must be a non-empty subset of ['authorization_code', 'refresh_token']",
        )
    if "authorization_code" not in resolved_grants:
        raise OAuthError(
            "invalid_client_metadata",
            "grant_types must include 'authorization_code'",
        )
    deduped_grants = list(dict.fromkeys(resolved_grants))

    resolved_response_types = list(response_types) if response_types is not None else ["code"]
    if not resolved_response_types or any(rt != "code" for rt in resolved_response_types):
        raise OAuthError(
            "invalid_client_metadata",
            "response_types must be ['code']",
        )

    auth_method = (token_endpoint_auth_method or "none").strip()
    if auth_method not in ALLOWED_AUTH_METHODS:
        raise OAuthError(
            "invalid_client_metadata",
            "token_endpoint_auth_method must be 'none' or 'client_secret_post'",
        )

    ip_hash = hash_client_ip(client_ip, settings=cfg)
    if ip_hash is not None:
        daily_cap = get_oauth_dcr_max_per_ip_per_day(cfg)
        since_24h = current_now - timedelta(hours=24)
        recent_from_ip = (
            db.execute(
                select(func.count(OAuthClient.client_id)).where(
                    OAuthClient.registered_by_ip_hash == ip_hash,
                    OAuthClient.created_at >= since_24h,
                )
            ).scalar_one()
            or 0
        )
        if int(recent_from_ip) >= daily_cap:
            raise OAuthRateLimitError(
                "Daily dynamic client registration limit reached for this IP address",
                retry_after_s=3600,
            )

    max_clients = get_oauth_max_clients(cfg)
    total_clients = db.execute(select(func.count(OAuthClient.client_id))).scalar_one() or 0
    if int(total_clients) >= max_clients:
        # Lazy fallback prune when at capacity (routine pruning runs in the background worker)
        pruned = prune_stale_oauth_clients(
            db,
            now=current_now,
            unused_ttl_days=get_oauth_unused_client_ttl_days(cfg),
        )
        if pruned > 0:
            total_clients = db.execute(select(func.count(OAuthClient.client_id))).scalar_one() or 0
        if int(total_clients) >= max_clients:
            raise OAuthError(
                "too_many_clients",
                "OAuth client registration table capacity reached",
                status_code=503,
            )

    client_id = generate_prefixed_token(OAUTH_CLIENT_PREFIX, nbytes=16)
    client_secret: str | None = None
    client_secret_hash: str | None = None
    if auth_method == "client_secret_post":
        client_secret = generate_prefixed_token(OAUTH_CLIENT_SECRET_PREFIX, nbytes=32)
        client_secret_hash = hash_token(client_secret)

    client = OAuthClient(
        client_id=client_id,
        client_name=cleaned_name,
        client_secret_hash=client_secret_hash,
        redirect_uris=validated_uris,
        grant_types=deduped_grants,
        token_endpoint_auth_method=auth_method,
        registered_by_ip_hash=ip_hash,
        created_at=current_now,
    )
    db.add(client)
    if commit:
        db.commit()
        db.refresh(client)
    else:
        db.flush()

    return ClientRegistrationResult(
        client=client,
        client_secret=client_secret,
        response_types=["code"],
    )


# -----------------------------------------------------------------------------
# Authorization Request Validation, HMAC CSRF Nonce & Auto-Escaped Consent HTML
# -----------------------------------------------------------------------------


def validate_authorize_request(
    db: Session,
    *,
    client_id: str | None,
    redirect_uri: str | None,
    response_type: str | None,
    scope: str | None,
    code_challenge: str | None,
    code_challenge_method: str | None,
    state: str | None = None,
    resource: str | None = None,
    expected_origin: str | None = None,
) -> ValidatedAuthorizeParams:
    """Validate OAuth 2.1 authorization request parameters (exact redirect_uri & PKCE S256)."""
    if not client_id or not client_id.strip():
        raise OAuthError("invalid_client", "Missing client_id")

    client = db.get(OAuthClient, client_id.strip())
    if client is None:
        raise OAuthError("invalid_client", "Unknown OAuth client_id")

    # Exact byte-for-byte redirect_uri match against registered URIs (Spec Section 5.3 #2)
    registered_uris = list(client.redirect_uris or [])
    if not redirect_uri or redirect_uri not in registered_uris:
        raise OAuthError(
            "invalid_redirect_uri",
            "redirect_uri does not exactly match any registered redirect URI for this client",
        )
    # Re-validate structurally as defense-in-depth
    validate_redirect_uri(redirect_uri)

    if response_type != "code":
        raise OAuthError(
            "unsupported_response_type",
            "Only response_type='code' is supported",
        )

    validated_challenge = validate_code_challenge(code_challenge, code_challenge_method)
    validated_resource = validate_resource_indicator(resource, expected_origin=expected_origin)

    raw_scope = (scope or "").strip() or SCOPE_READ
    try:
        scopes_list = normalize_scopes(raw_scope)
    except ValueError as exc:
        raise OAuthError("invalid_scope", str(exc)) from exc

    clean_state = state or ""
    if len(clean_state) > 512:
        raise OAuthError("invalid_request", "state parameter exceeds maximum length")

    return ValidatedAuthorizeParams(
        client=client,
        redirect_uri=redirect_uri,
        scope=" ".join(scopes_list),
        scopes=scopes_list,
        state=clean_state,
        code_challenge=validated_challenge,
        code_challenge_method="S256",
        resource=validated_resource,
    )


def _csrf_binding_hash(
    *,
    session_binding: str,
    action: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scope: str,
    resource: str | None,
) -> str:
    payload = json.dumps(
        [
            session_binding,
            action,
            client_id,
            redirect_uri,
            code_challenge,
            state,
            scope,
            resource or "",
        ],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(part: str) -> bytes:
    padding = "=" * (-len(part) % 4)
    return base64.urlsafe_b64decode(part + padding)


def create_oauth_csrf_nonce(
    *,
    session_binding: str,
    action: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scope: str,
    resource: str | None = None,
    settings: Settings | None = None,
    ttl_seconds: int = DEFAULT_CSRF_NONCE_TTL_S,
) -> str:
    """Create a short-lived HMAC-SHA256 CSRF nonce bound to session + OAuth request parameters."""
    cfg = settings or get_settings()
    now_ts = int(time.time())
    body = {
        "b": _csrf_binding_hash(
            session_binding=session_binding,
            action=action,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            state=state,
            scope=scope,
            resource=resource,
        ),
        "n": secrets.token_hex(12),
        "iat": now_ts,
        "exp": now_ts + int(ttl_seconds),
    }
    body_bytes = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = _b64url_encode(body_bytes)
    sig = hmac.new(
        cfg.auth_secret_key.encode("utf-8"),
        _CSRF_DOMAIN_SEP + b"." + encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded}.{_b64url_encode(sig)}"


def verify_oauth_csrf_nonce(
    token: str | None,
    *,
    session_binding: str,
    action: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scope: str,
    resource: str | None = None,
    settings: Settings | None = None,
) -> bool:
    """Verify an HMAC-signed OAuth CSRF nonce and its exact session + parameter binding."""
    if not token or "." not in token:
        return False
    parts = token.split(".")
    if len(parts) != 2:
        return False
    encoded, sig_b64 = parts
    cfg = settings or get_settings()
    expected_sig = hmac.new(
        cfg.auth_secret_key.encode("utf-8"),
        _CSRF_DOMAIN_SEP + b"." + encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        actual_sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(actual_sig, expected_sig):
            return False
        body = json.loads(_b64url_decode(encoded).decode("utf-8"))
    except Exception:
        return False

    if not isinstance(body, dict):
        return False
    exp = body.get("exp")
    bound = body.get("b")
    if isinstance(exp, bool) or not isinstance(exp, int) or not isinstance(bound, str):
        return False
    if int(time.time()) >= exp:
        return False

    expected_binding = _csrf_binding_hash(
        session_binding=session_binding,
        action=action,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        state=state,
        scope=scope,
        resource=resource,
    )
    return hmac.compare_digest(bound, expected_binding)


def _hidden_oauth_inputs(params: ValidatedAuthorizeParams, csrf_token: str) -> str:
    fields = [
        ("client_id", params.client.client_id),
        ("redirect_uri", params.redirect_uri),
        ("response_type", "code"),
        ("scope", params.scope),
        ("state", params.state),
        ("code_challenge", params.code_challenge),
        ("code_challenge_method", params.code_challenge_method),
        ("resource", params.resource or ""),
        ("csrf_token", csrf_token),
    ]
    return "\n".join(
        f'<input type="hidden" name="{html.escape(k, quote=True)}" '
        f'value="{html.escape(v, quote=True)}">'
        for k, v in fields
    )


def _render_scope_list_html(scopes: Iterable[str]) -> str:
    items = []
    for scope in scopes:
        desc = SCOPE_DESCRIPTIONS.get(scope, scope)
        items.append(
            "<li>"
            f"<strong>{html.escape(scope, quote=True)}</strong> — "
            f"{html.escape(desc, quote=True)}"
            "</li>"
        )
    return "<ul>" + "".join(items) + "</ul>"


_BASE_PAGE_CSS: Final[str] = """
body {
  margin: 0; font-family: system-ui, -apple-system, sans-serif;
  background: #0b1220; color: #e2e8f0; display: flex; min-height: 100vh;
  align-items: center; justify-content: center; padding: 1.5rem;
}
.card {
  width: 100%; max-width: 440px; background: #111c2e; border: 1px solid #243550;
  border-radius: 12px; padding: 1.5rem; box-shadow: 0 12px 32px rgba(0,0,0,0.45);
}
h1 { margin: 0 0 0.5rem; font-size: 1.25rem; color: #f8fafc; }
p { margin: 0.5rem 0; color: #cbd5e1; line-height: 1.45; font-size: 0.94rem; }
ul { margin: 0.75rem 0 1rem; padding-left: 1.25rem; color: #e2e8f0; font-size: 0.92rem; }
li { margin: 0.35rem 0; }
.meta {
  background: #0b1322; border: 1px solid #1e293b; border-radius: 8px;
  padding: 0.75rem; margin: 0.75rem 0; font-size: 0.88rem; word-break: break-word;
}
.error {
  background: rgba(239,68,68,0.14); border: 1px solid #ef4444; color: #fecaca;
  border-radius: 8px; padding: 0.65rem 0.75rem; margin: 0.75rem 0; font-size: 0.9rem;
}
label { display: block; font-size: 0.86rem; color: #cbd5e1; margin: 0.65rem 0 0.25rem; }
input[type="email"], input[type="password"] {
  width: 100%; box-sizing: border-box; padding: 0.6rem 0.75rem; border-radius: 8px;
  border: 1px solid #334155; background: #0b1220; color: #f8fafc; font-size: 0.95rem;
}
.actions { display: flex; gap: 0.75rem; margin-top: 1.25rem; }
button {
  flex: 1; padding: 0.65rem 1rem; border-radius: 8px; border: none;
  font-size: 0.94rem; font-weight: 600; cursor: pointer;
}
.btn-primary { background: #22c55e; color: #052e16; }
.btn-secondary { background: #1e293b; color: #e2e8f0; border: 1px solid #334155; }
"""


def render_oauth_login_html(
    *,
    params: ValidatedAuthorizeParams,
    csrf_token: str,
    error_message: str | None = None,
) -> str:
    """Render auto-escaped same-origin login HTML before OAuth 2.1 consent."""
    client_name = html.escape(params.client.client_name, quote=True)
    error_html = (
        f'<div class="error">{html.escape(error_message, quote=True)}</div>'
        if error_message
        else ""
    )
    hidden = _hidden_oauth_inputs(params, csrf_token)
    scopes_html = _render_scope_list_html(params.scopes)
    return (
        "<!DOCTYPE html><html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Sign in to TrailForge</title>"
        f"<style>{_BASE_PAGE_CSS}</style>"
        "</head><body><main class=\"card\">"
        "<h1>Sign in to TrailForge</h1>"
        f"<p><strong>{client_name}</strong> wants to connect to your TrailForge account.</p>"
        f"<div class=\"meta\">Requested permissions:{scopes_html}</div>"
        f"{error_html}"
        "<form method=\"post\" action=\"/api/oauth/authorize\">"
        "<input type=\"hidden\" name=\"action\" value=\"login\">"
        f"{hidden}"
        "<label for=\"email\">Email</label>"
        "<input id=\"email\" name=\"email\" type=\"email\" required autocomplete=\"username\">"
        '<label for="password">Password</label>'
        '<input id="password" name="password" type="password" required '
        'autocomplete="current-password">'
        "<div class=\"actions\">"
        "<button class=\"btn-primary\" type=\"submit\">Continue</button>"
        "</div></form></main></body></html>"
    )


def render_oauth_consent_html(
    *,
    params: ValidatedAuthorizeParams,
    user: User,
    csrf_token: str,
    error_message: str | None = None,
) -> str:
    """Render auto-escaped OAuth 2.1 consent HTML displaying client name, scopes, and user email."""
    client_name = html.escape(params.client.client_name, quote=True)
    user_email = html.escape(user.email, quote=True)
    user_name = html.escape(user.display_name, quote=True)
    redirect_display = html.escape(params.redirect_uri, quote=True)
    error_html = (
        f'<div class="error">{html.escape(error_message, quote=True)}</div>'
        if error_message
        else ""
    )
    hidden = _hidden_oauth_inputs(params, csrf_token)
    scopes_html = _render_scope_list_html(params.scopes)
    return (
        "<!DOCTYPE html><html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Authorize Application — TrailForge</title>"
        f"<style>{_BASE_PAGE_CSS}</style>"
        "</head><body><main class=\"card\">"
        "<h1>Authorize Application</h1>"
        f"<p><strong>{client_name}</strong> is requesting access to your TrailForge account.</p>"
        f"<div class=\"meta\">Signed in as <strong>{user_name}</strong> ({user_email})</div>"
        f"<div class=\"meta\">Permissions requested:{scopes_html}"
        f"<p>Redirect: <code>{redirect_display}</code></p></div>"
        f"{error_html}"
        "<form method=\"post\" action=\"/api/oauth/authorize\">"
        "<input type=\"hidden\" name=\"action\" value=\"consent\">"
        f"{hidden}"
        '<div class="actions">'
        '<button class="btn-secondary" type="submit" name="decision" value="deny">'
        "Deny</button>"
        '<button class="btn-primary" type="submit" name="decision" value="approve">'
        "Approve</button>"
        "</div></form></main></body></html>"
    )


def render_oauth_error_html(*, error: str, error_description: str) -> str:
    """Render auto-escaped OAuth error page (used when redirect_uri/client_id is invalid)."""
    return (
        "<!DOCTYPE html><html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Authorization Error — TrailForge</title>"
        f"<style>{_BASE_PAGE_CSS}</style>"
        "</head><body><main class=\"card\">"
        "<h1>Authorization Error</h1>"
        f"<div class=\"error\"><strong>{html.escape(error, quote=True)}</strong>: "
        f"{html.escape(error_description, quote=True)}</div>"
        "<p>Close this window and return to your AI client.</p>"
        "</main></body></html>"
    )


def build_redirect_url(redirect_uri: str, query_params: dict[str, str]) -> str:
    """Append OAuth response parameters safely to a validated ``redirect_uri``."""
    parts = urlsplit(redirect_uri)
    merged = list(parse_qsl(parts.query, keep_blank_values=True))
    for key, val in query_params.items():
        if val is not None and val != "":
            merged.append((key, val))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(merged), ""))


# -----------------------------------------------------------------------------
# Authorization Code Issuance, Token Exchange, Rotation & Revocation
# -----------------------------------------------------------------------------


def _lock_oauth_user(db: Session, user_id: uuid.UUID) -> User | None:
    """Serialize OAuth credential changes with other credential changes and logout."""
    # FOR NO KEY UPDATE conflicts with the session version update while allowing FK reads.
    return db.get(User, user_id, populate_existing=True, with_for_update={"key_share": True})


def issue_authorization_code(
    db: Session,
    *,
    params: ValidatedAuthorizeParams,
    user: User,
    settings: Settings | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> str:
    """Mint a single-use 60 s OAuth authorization code (`tf_oac_...`) stored as SHA-256 hash."""
    # Consent was verified against this version before waiting for the account lock.
    approved_version = user.token_version
    current_user = _lock_oauth_user(db, user.id)
    if current_user is None or not current_user.is_active:
        raise OAuthError("access_denied", "Account is suspended", status_code=403)
    if current_user.token_version != approved_version:
        raise OAuthError("access_denied", "Sign in again before granting consent", status_code=403)
    cfg = settings or get_settings()
    current_now = as_utc(now) if now is not None else datetime.now(UTC)
    ttl_s = get_oauth_auth_code_ttl_s(cfg)

    raw_code = generate_prefixed_token(OAUTH_CODE_PREFIX, nbytes=32)
    row = OAuthAuthorizationCode(
        code_hash=hash_token(raw_code),
        client_id=params.client.client_id,
        user_id=current_user.id,
        user_token_version=approved_version,
        redirect_uri=params.redirect_uri,
        scope=params.scope,
        code_challenge=params.code_challenge,
        code_challenge_method="S256",
        resource=params.resource,
        expires_at=current_now + timedelta(seconds=ttl_s),
        created_at=current_now,
    )
    db.add(row)
    if commit:
        db.commit()
    else:
        db.flush()
    return raw_code


def authenticate_oauth_client(
    db: Session,
    *,
    client_id: str | None,
    client_secret: str | None = None,
) -> OAuthClient:
    """Authenticate an OAuth client using ``none`` or ``client_secret_post``."""
    if not client_id or not client_id.strip():
        raise OAuthError("invalid_client", "Missing client_id", status_code=401)
    client = db.get(OAuthClient, client_id.strip())
    if client is None:
        raise OAuthError("invalid_client", "Unknown client_id", status_code=401)
    if client.token_endpoint_auth_method == "client_secret_post":
        if not client_secret or not client.client_secret_hash:
            raise OAuthError("invalid_client", "Client authentication failed", status_code=401)
        if not constant_time_hash_equals(client_secret, client.client_secret_hash):
            raise OAuthError("invalid_client", "Client authentication failed", status_code=401)
    return client


def revoke_token_family(
    db: Session,
    token_family_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> int:
    """Revoke every active OAuth token belonging to ``token_family_id``."""
    current_now = as_utc(now) if now is not None else datetime.now(UTC)
    res = db.execute(
        update(OAuthToken)
        .where(
            OAuthToken.token_family_id == token_family_id,
            OAuthToken.revoked_at.is_(None),
        )
        .values(revoked_at=current_now)
    )
    return int(res.rowcount or 0)


def exchange_authorization_code(
    db: Session,
    *,
    code: str | None,
    redirect_uri: str | None,
    client_id: str | None,
    code_verifier: str | None,
    client_secret: str | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> TokenResponsePayload:
    """Exchange a single-use authorization code + PKCE S256 verifier for access & refresh tokens."""
    cfg = settings or get_settings()

    client = authenticate_oauth_client(db, client_id=client_id, client_secret=client_secret)
    if not code or not code.startswith(OAUTH_CODE_PREFIX):
        raise OAuthError("invalid_grant", "Invalid authorization code")
    if not redirect_uri:
        raise OAuthError("invalid_request", "Missing redirect_uri")
    if not code_verifier:
        raise OAuthError("invalid_request", "Missing PKCE code_verifier")

    code_digest = hash_token(code)
    code_row = db.get(OAuthAuthorizationCode, code_digest)
    if code_row is None or not hmac.compare_digest(code_row.code_hash, code_digest):
        raise OAuthError("invalid_grant", "Invalid or expired authorization code")
    if code_row.client_id != client.client_id:
        raise OAuthError("invalid_grant", "Authorization code was not issued to this client")
    if code_row.redirect_uri != redirect_uri:
        raise OAuthError("invalid_grant", "redirect_uri mismatch")
    user = _lock_oauth_user(db, code_row.user_id)
    if user is None:
        raise OAuthError("invalid_grant", "User account is no longer active")
    # A parallel exchange or grant revocation can change the code while the lock waits.
    code_row = db.get(OAuthAuthorizationCode, code_digest, populate_existing=True)
    if code_row is None:
        raise OAuthError("invalid_grant", "Invalid or expired authorization code")
    current_now = as_utc(now) if now is not None else datetime.now(UTC)
    if code_row.consumed_at is not None:
        # OAuth 2.1 §4.1.2 / RFC 6749 §10.5: revoke entire token family on auth-code replay
        if code_row.token_family_id is not None:
            revoke_token_family(db, code_row.token_family_id, now=current_now)
            if commit:
                db.commit()
            else:
                db.flush()
        raise OAuthError("invalid_grant", "Authorization code has already been used")
    if as_utc(code_row.expires_at) <= current_now:
        raise OAuthError("invalid_grant", "Authorization code has expired")
    if not verify_pkce_s256(code_verifier, code_row.code_challenge):
        raise OAuthError("invalid_grant", "PKCE code_verifier verification failed")
    if not user.is_active:
        raise OAuthError("invalid_grant", "User account is no longer active")
    approved_version = code_row.user_token_version
    if approved_version is None or approved_version != user.token_version:
        raise OAuthError("invalid_grant", "Session has been revoked")

    # Atomically consume single-use authorization code and bind its minted token_family_id
    family_id = uuid.uuid4()
    consumed = db.execute(
        update(OAuthAuthorizationCode)
        .where(
            OAuthAuthorizationCode.code_hash == code_digest,
            OAuthAuthorizationCode.consumed_at.is_(None),
        )
        .values(consumed_at=current_now, token_family_id=family_id)
    )
    if int(consumed.rowcount or 0) != 1:
        replayed_row = db.get(OAuthAuthorizationCode, code_digest, populate_existing=True)
        if replayed_row is not None and replayed_row.token_family_id is not None:
            revoke_token_family(db, replayed_row.token_family_id, now=current_now)
            if commit:
                db.commit()
            else:
                db.flush()
        raise OAuthError("invalid_grant", "Authorization code has already been used")

    access_ttl_s = get_oauth_access_token_ttl_s(cfg)
    refresh_ttl_s = get_oauth_refresh_token_ttl_s(cfg)
    issue_refresh = "refresh_token" in list(client.grant_types or [])

    raw_access = generate_prefixed_token(OAUTH_ACCESS_PREFIX, nbytes=32)
    raw_refresh = (
        generate_prefixed_token(OAUTH_REFRESH_PREFIX, nbytes=32) if issue_refresh else None
    )

    token_row = OAuthToken(
        token_family_id=family_id,
        client_id=client.client_id,
        user_id=user.id,
        user_token_version=approved_version,
        access_token_hash=hash_token(raw_access),
        refresh_token_hash=hash_token(raw_refresh) if raw_refresh is not None else None,
        scope=code_row.scope,
        resource=code_row.resource,
        access_expires_at=current_now + timedelta(seconds=access_ttl_s),
        refresh_expires_at=(
            current_now + timedelta(seconds=refresh_ttl_s) if raw_refresh is not None else None
        ),
        created_at=current_now,
    )
    client.last_used_at = current_now
    db.add(token_row)
    if commit:
        db.commit()
    else:
        db.flush()

    return TokenResponsePayload(
        access_token=raw_access,
        token_type="Bearer",
        expires_in=access_ttl_s,
        scope=code_row.scope,
        refresh_token=raw_refresh,
    )


def refresh_oauth_token(
    db: Session,
    *,
    refresh_token: str | None,
    client_id: str | None,
    scope: str | None = None,
    client_secret: str | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> TokenResponsePayload:
    """Rotate a refresh token atomically; revokes the entire token family if reuse is detected."""
    cfg = settings or get_settings()

    client = authenticate_oauth_client(db, client_id=client_id, client_secret=client_secret)
    if "refresh_token" not in list(client.grant_types or []):
        raise OAuthError("unauthorized_client", "Client is not authorized for refresh_token grant")
    if not refresh_token or not refresh_token.startswith(OAUTH_REFRESH_PREFIX):
        raise OAuthError("invalid_grant", "Invalid refresh token")

    refresh_digest = hash_token(refresh_token)
    token_row = db.execute(
        select(OAuthToken).where(OAuthToken.refresh_token_hash == refresh_digest).limit(1)
    ).scalar_one_or_none()
    if token_row is None or not token_row.refresh_token_hash or not hmac.compare_digest(
        token_row.refresh_token_hash, refresh_digest
    ):
        raise OAuthError("invalid_grant", "Invalid refresh token")
    if token_row.client_id != client.client_id:
        raise OAuthError("invalid_grant", "Refresh token was not issued to this client")
    user = _lock_oauth_user(db, token_row.user_id)
    if user is None:
        raise OAuthError("invalid_grant", "User account is no longer active")
    token_row = db.get(OAuthToken, token_row.id, populate_existing=True)
    if token_row is None:
        raise OAuthError("invalid_grant", "Invalid refresh token")
    current_now = as_utc(now) if now is not None else datetime.now(UTC)

    # Spec Section 5.3 #4: presenting an already-revoked refresh token revokes the whole family!
    if token_row.revoked_at is not None:
        revoke_token_family(db, token_row.token_family_id, now=current_now)
        if commit:
            db.commit()
        else:
            db.flush()
        raise OAuthError("invalid_grant", "Refresh token reuse detected; token family revoked")

    if token_row.refresh_expires_at is None or as_utc(token_row.refresh_expires_at) <= current_now:
        raise OAuthError("invalid_grant", "Refresh token has expired")

    if not user.is_active:
        raise OAuthError("invalid_grant", "User account is no longer active")
    if token_row.user_token_version != user.token_version:
        raise OAuthError("invalid_grant", "Session has been revoked")

    granted_scopes = set(normalize_scopes(token_row.scope))
    if scope and scope.strip():
        try:
            requested_scopes = normalize_scopes(scope)
        except ValueError as exc:
            raise OAuthError("invalid_scope", str(exc)) from exc
        if not set(requested_scopes).issubset(granted_scopes):
            raise OAuthError(
                "invalid_scope",
                "Requested scope exceeds originally granted scope",
            )
        effective_scope = " ".join(requested_scopes)
    else:
        effective_scope = token_row.scope

    # Atomically revoke the presented refresh token row
    rotated = db.execute(
        update(OAuthToken)
        .where(
            OAuthToken.id == token_row.id,
            OAuthToken.revoked_at.is_(None),
        )
        .values(revoked_at=current_now)
    )
    if int(rotated.rowcount or 0) != 1:
        revoke_token_family(db, token_row.token_family_id, now=current_now)
        if commit:
            db.commit()
        else:
            db.flush()
        raise OAuthError("invalid_grant", "Refresh token reuse detected; token family revoked")

    access_ttl_s = get_oauth_access_token_ttl_s(cfg)
    refresh_ttl_s = get_oauth_refresh_token_ttl_s(cfg)
    raw_access = generate_prefixed_token(OAUTH_ACCESS_PREFIX, nbytes=32)
    raw_refresh = generate_prefixed_token(OAUTH_REFRESH_PREFIX, nbytes=32)

    new_row = OAuthToken(
        token_family_id=token_row.token_family_id,
        client_id=client.client_id,
        user_id=user.id,
        user_token_version=token_row.user_token_version,
        access_token_hash=hash_token(raw_access),
        refresh_token_hash=hash_token(raw_refresh),
        scope=effective_scope,
        resource=token_row.resource,
        access_expires_at=current_now + timedelta(seconds=access_ttl_s),
        refresh_expires_at=current_now + timedelta(seconds=refresh_ttl_s),
        created_at=current_now,
    )
    client.last_used_at = current_now
    db.add(new_row)
    if commit:
        db.commit()
    else:
        db.flush()

    return TokenResponsePayload(
        access_token=raw_access,
        token_type="Bearer",
        expires_in=access_ttl_s,
        scope=effective_scope,
        refresh_token=raw_refresh,
    )


def revoke_oauth_token(
    db: Session,
    *,
    token: str | None,
    token_type_hint: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> bool:
    """Revoke an OAuth access or refresh token per RFC 7009."""
    authenticated_client: OAuthClient | None = None
    if client_id is not None and client_id.strip():
        authenticated_client = authenticate_oauth_client(
            db, client_id=client_id, client_secret=client_secret
        )

    if not token or not token.strip():
        return False

    digest = hash_token(token.strip())
    search_order = (
        ("refresh", "access") if token_type_hint == "refresh_token" else ("access", "refresh")
    )

    matched_kind: str | None = None
    matched_row: OAuthToken | None = None
    for kind in search_order:
        col = OAuthToken.refresh_token_hash if kind == "refresh" else OAuthToken.access_token_hash
        row = db.execute(select(OAuthToken).where(col == digest).limit(1)).scalar_one_or_none()
        if row is not None:
            matched_kind = kind
            matched_row = row
            break

    if matched_row is None:
        return False
    if authenticated_client is not None and matched_row.client_id != authenticated_client.client_id:
        return False
    if _lock_oauth_user(db, matched_row.user_id) is None:
        return False
    current_now = as_utc(now) if now is not None else datetime.now(UTC)

    if matched_kind == "refresh":
        revoke_token_family(db, matched_row.token_family_id, now=current_now)
    else:
        db.execute(
            update(OAuthToken)
            .where(OAuthToken.id == matched_row.id, OAuthToken.revoked_at.is_(None))
            .values(revoked_at=current_now)
        )

    if commit:
        db.commit()
    else:
        db.flush()
    return True


def list_user_oauth_grants(
    db: Session,
    user: User,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return active connected OAuth AI applications granted by ``user``."""
    current_now = as_utc(now) if now is not None else datetime.now(UTC)
    user_tv = int(getattr(user, "token_version", 1) or 1)

    rows = db.execute(
        select(OAuthToken, OAuthClient)
        .join(OAuthClient, OAuthToken.client_id == OAuthClient.client_id)
        .where(
            OAuthToken.user_id == user.id,
            OAuthToken.user_token_version == user_tv,
            OAuthToken.revoked_at.is_(None),
            or_(
                OAuthToken.access_expires_at > current_now,
                and_(
                    OAuthToken.refresh_expires_at.is_not(None),
                    OAuthToken.refresh_expires_at > current_now,
                ),
            ),
        )
        .order_by(OAuthToken.created_at.desc())
    ).all()

    grouped: dict[str, dict[str, Any]] = {}
    for token_row, client in rows:
        entry = grouped.get(client.client_id)
        row_scopes = normalize_scopes(token_row.scope)
        created_utc = as_utc(token_row.created_at)
        last_used_utc = as_utc(client.last_used_at) if client.last_used_at is not None else None
        if entry is None:
            grouped[client.client_id] = {
                "client_id": client.client_id,
                "client_name": client.client_name,
                "scopes": set(row_scopes),
                "granted_at": created_utc,
                "last_used_at": last_used_utc,
                "active_token_count": 1,
            }
        else:
            entry["scopes"].update(row_scopes)
            if created_utc < entry["granted_at"]:
                entry["granted_at"] = created_utc
            entry["active_token_count"] += 1

    result: list[dict[str, Any]] = []
    for entry in grouped.values():
        result.append(
            {
                "client_id": entry["client_id"],
                "client_name": entry["client_name"],
                "scopes": [s for s in ALL_SCOPES if s in entry["scopes"]],
                "granted_at": entry["granted_at"],
                "last_used_at": entry["last_used_at"],
                "active_token_count": entry["active_token_count"],
            }
        )
    result.sort(key=lambda item: item["granted_at"], reverse=True)
    return result


def revoke_user_oauth_grant(
    db: Session,
    *,
    user_id: uuid.UUID,
    client_id: str,
    now: datetime | None = None,
    commit: bool = True,
) -> int:
    """Revoke all active OAuth tokens and unconsumed auth codes for ``(user_id, client_id)``."""
    if _lock_oauth_user(db, user_id) is None:
        return 0
    current_now = as_utc(now) if now is not None else datetime.now(UTC)
    tok_res = db.execute(
        update(OAuthToken)
        .where(
            OAuthToken.user_id == user_id,
            OAuthToken.client_id == client_id,
            OAuthToken.revoked_at.is_(None),
        )
        .values(revoked_at=current_now)
    )
    code_res = db.execute(
        update(OAuthAuthorizationCode)
        .where(
            OAuthAuthorizationCode.user_id == user_id,
            OAuthAuthorizationCode.client_id == client_id,
            OAuthAuthorizationCode.consumed_at.is_(None),
        )
        .values(consumed_at=current_now)
    )
    revoked_count = int(tok_res.rowcount or 0) + int(code_res.rowcount or 0)
    if commit:
        db.commit()
    else:
        db.flush()
    return revoked_count


__all__ = [
    "ALLOWED_AUTH_METHODS",
    "ALLOWED_GRANT_TYPES",
    "ClientRegistrationResult",
    "DEFAULT_ACCESS_TOKEN_TTL_S",
    "DEFAULT_AUTH_CODE_TTL_S",
    "DEFAULT_CSRF_NONCE_TTL_S",
    "DEFAULT_DCR_MAX_PER_IP_PER_DAY",
    "DEFAULT_OAUTH_MAX_CLIENTS",
    "DEFAULT_REFRESH_TOKEN_TTL_S",
    "DEFAULT_UNUSED_CLIENT_TTL_DAYS",
    "MAX_CLIENT_NAME_LENGTH",
    "MAX_REDIRECT_URIS_PER_CLIENT",
    "MAX_REDIRECT_URI_LENGTH",
    "McpAuthModeLiteral",
    "OAuthError",
    "OAuthRateLimitError",
    "SCOPE_DESCRIPTIONS",
    "TokenResponsePayload",
    "ValidatedAuthorizeParams",
    "authenticate_oauth_client",
    "build_authorization_server_metadata",
    "build_protected_resource_metadata",
    "build_redirect_url",
    "build_www_authenticate_header",
    "compute_pkce_s256",
    "create_oauth_csrf_nonce",
    "exchange_authorization_code",
    "get_mcp_auth_mode",
    "get_oauth_access_token_ttl_s",
    "get_oauth_auth_code_ttl_s",
    "get_oauth_dcr_max_per_ip_per_day",
    "get_oauth_max_clients",
    "get_oauth_refresh_token_ttl_s",
    "get_oauth_unused_client_ttl_days",
    "get_public_base_url",
    "hash_client_ip",
    "is_mcp_enabled",
    "issue_authorization_code",
    "list_user_oauth_grants",
    "prune_stale_oauth_clients",
    "refresh_oauth_token",
    "register_oauth_client",
    "render_oauth_consent_html",
    "render_oauth_error_html",
    "render_oauth_login_html",
    "resolve_public_origin",
    "revoke_oauth_token",
    "revoke_token_family",
    "revoke_user_oauth_grant",
    "validate_authorize_request",
    "validate_code_challenge",
    "validate_redirect_uri",
    "validate_resource_indicator",
    "verify_oauth_csrf_nonce",
    "verify_pkce_s256",
]
