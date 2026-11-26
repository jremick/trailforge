"""MCP request authentication context, ContextVars, and safe config/DB helpers."""

from __future__ import annotations

import contextlib
import importlib
import inspect
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Generator, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from trailforge.auth.security import verify_access_token
from trailforge.config import Settings, get_settings
from trailforge.db.models import User
from trailforge.db.session import get_db

SCOPE_READ: Final[str] = "trailforge:read"
SCOPE_WRITE: Final[str] = "trailforge:write"
ALL_MCP_SCOPES: Final[frozenset[str]] = frozenset({SCOPE_READ, SCOPE_WRITE})

logger = logging.getLogger(__name__)

McpAuthMethodLiteral = Literal["anonymous", "session", "pat", "oauth"]
McpAuthModeLiteral = Literal["hybrid", "oauth_required", "anonymous_only"]


class McpAuthError(PermissionError):
    """Raised when an MCP tool call lacks authentication or required OAuth/PAT scope."""

    def __init__(self, message: str, *, required_scope: str | None = None) -> None:
        super().__init__(message)
        self.required_scope = required_scope


@dataclass(frozen=True, slots=True)
class McpAuthContext:
    """Resolved principal and scopes for a single MCP request (Section 5.1 contract)."""

    user: User | None
    auth_method: McpAuthMethodLiteral
    scopes: frozenset[str]
    token_id: uuid.UUID | None = None
    client_id: str | None = None

    @property
    def is_authenticated(self) -> bool:
        return self.user is not None and bool(self.user.is_active)

    def has_scope(self, scope: str) -> bool:
        """Return True if this principal holds ``scope`` (``write`` implies ``read``)."""
        if not self.is_authenticated:
            return False
        if scope == SCOPE_READ:
            return SCOPE_READ in self.scopes or SCOPE_WRITE in self.scopes
        return scope in self.scopes


ANONYMOUS_CONTEXT: Final[McpAuthContext] = McpAuthContext(
    user=None,
    auth_method="anonymous",
    scopes=frozenset(),
)


class McpAuthResolver(Protocol):
    """Callable resolving an HTTP Authorization header into an ``McpAuthContext``."""

    def __call__(
        self,
        authorization_header: str | None,
        db: Session,
    ) -> McpAuthContext | Awaitable[McpAuthContext]: ...


_current_auth_ctx: ContextVar[McpAuthContext] = ContextVar(
    "trailforge_mcp_auth_ctx", default=ANONYMOUS_CONTEXT
)
_current_public_origin: ContextVar[str] = ContextVar("trailforge_mcp_public_origin", default="")
_current_settings: ContextVar[Settings | None] = ContextVar("trailforge_mcp_settings", default=None)
_current_db_factory: ContextVar[Callable[[], Session] | None] = ContextVar(
    "trailforge_mcp_db_factory", default=None
)


def get_mcp_auth_context() -> McpAuthContext:
    """Return the active ``McpAuthContext`` for the current MCP task."""
    return _current_auth_ctx.get()


def set_mcp_auth_context(ctx: McpAuthContext) -> Token[McpAuthContext]:
    return _current_auth_ctx.set(ctx)


def reset_mcp_auth_context(token: Token[McpAuthContext]) -> None:
    _current_auth_ctx.reset(token)


def get_mcp_settings() -> Settings:
    """Return per-request Settings override when bound, else cached global Settings."""
    override = _current_settings.get()
    return override if override is not None else get_settings()


def set_mcp_settings(settings: Settings | None) -> Token[Settings | None]:
    return _current_settings.set(settings)


def reset_mcp_settings(token: Token[Settings | None]) -> None:
    _current_settings.reset(token)


def set_mcp_public_origin(origin: str) -> Token[str]:
    return _current_public_origin.set(origin.rstrip("/"))


def reset_mcp_public_origin(token: Token[str]) -> None:
    _current_public_origin.reset(token)


def set_mcp_db_factory(
    factory: Callable[[], Session] | None,
) -> Token[Callable[[], Session] | None]:
    return _current_db_factory.set(factory)


def reset_mcp_db_factory(token: Token[Callable[[], Session] | None]) -> None:
    _current_db_factory.reset(token)


@contextlib.contextmanager
def open_mcp_db_session(
    db_factory: Callable[[], Session] | None = None,
) -> Generator[Session, None, None]:
    """Open a managed SQLAlchemy session using the injected test factory or ``get_db``."""
    factory = db_factory or _current_db_factory.get()
    if factory is not None:
        session = factory()
        try:
            yield session
        finally:
            session.close()
        return

    gen = get_db()
    session = next(gen)
    try:
        yield session
    finally:
        with contextlib.suppress(StopIteration):
            next(gen)


def require_mcp_auth(required_scope: str = SCOPE_READ) -> McpAuthContext:
    """Enforce authentication and scope on personal read/write MCP tools."""
    ctx = get_mcp_auth_context()
    if not ctx.is_authenticated or ctx.user is None:
        raise McpAuthError(
            "Authentication required. Connect via OAuth 2.1 (/mcp/auth) or supply a "
            "TrailForge Personal Access Token (Authorization: Bearer tf_pat_...).",
            required_scope=required_scope,
        )
    if not ctx.has_scope(required_scope):
        raise McpAuthError(
            f"Insufficient scope: this tool requires '{required_scope}'. "
            "Mint a token or approve an OAuth grant with write permissions.",
            required_scope=required_scope,
        )
    return ctx


# -----------------------------------------------------------------------------
# Safe Config Readers (Work Seamlessly Before & After Integrator config.py Edits)
# -----------------------------------------------------------------------------


def _parse_bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def is_mcp_enabled(cfg: Settings | None = None) -> bool:
    settings = cfg or get_mcp_settings()
    attr = getattr(settings, "enable_mcp", None)
    if attr is not None:
        return _parse_bool(attr, True)
    env_val = os.environ.get("TRAILFORGE_ENABLE_MCP")
    return _parse_bool(env_val, True)


def get_mcp_auth_mode(cfg: Settings | None = None) -> McpAuthModeLiteral:
    settings = cfg or get_mcp_settings()
    raw = getattr(settings, "mcp_auth_mode", None)
    if raw is None:
        raw = os.environ.get("TRAILFORGE_MCP_AUTH_MODE", "hybrid")
    norm = str(raw).strip().lower()
    if norm in ("hybrid", "oauth_required", "anonymous_only"):
        return norm  # type: ignore[return-value]
    return "hybrid"


def get_mcp_public_base_url_setting(cfg: Settings | None = None) -> str:
    settings = cfg or get_mcp_settings()
    raw = getattr(settings, "public_base_url", None)
    if raw is None:
        raw = os.environ.get("TRAILFORGE_PUBLIC_BASE_URL", "")
    return str(raw or "").strip().rstrip("/")


def get_mcp_app_html_path(cfg: Settings | None = None) -> str:
    settings = cfg or get_mcp_settings()
    raw = getattr(settings, "mcp_app_html", None)
    if raw is None:
        raw = os.environ.get("TRAILFORGE_MCP_APP_HTML", "")
    return str(raw or "").strip()


_VALID_HOST_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,255}$")


def is_valid_host_header(host: str) -> bool:
    """Validate an HTTP Host / X-Forwarded-Host header value (strict host[:port] syntax)."""
    cleaned = (host or "").strip()
    if not cleaned or len(cleaned) > 255:
        return False
    if any(ch in cleaned for ch in ("@", "/", "\\", '"', "'", " ", "\t", "\r", "\n")):
        return False
    return bool(_VALID_HOST_RE.match(cleaned))


def extract_bare_origin(url: str) -> str | None:
    """Return a validated ``scheme://host[:port]`` bare origin, or ``None`` if invalid."""
    raw = (url or "").strip()
    if not raw or any(ch in raw for ch in ("\r", "\n", '"', "'", "\\")):
        return None
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https"):
        return None
    if not parts.netloc or "@" in parts.netloc:
        return None
    host_port = parts.netloc.strip()
    if not is_valid_host_header(host_port):
        return None
    return f"{parts.scheme.lower()}://{host_port}"


def get_mcp_csp_tile_domains(
    cfg: Settings | None = None,
    *,
    public_origin: str | None = None,
) -> list[str]:
    """Build deduplicated list of bare origins for SEP-1865 ``_meta.ui.csp``."""
    settings = cfg or get_mcp_settings()
    candidates: list[str] = []

    raw_domains = getattr(settings, "mcp_csp_tile_domains", None)
    if raw_domains is None:
        raw_domains = os.environ.get("TRAILFORGE_MCP_CSP_TILE_DOMAINS", "")
    for item in str(raw_domains or "").split(","):
        origin = extract_bare_origin(item)
        if origin:
            candidates.append(origin)

    tiles_base = str(getattr(settings, "tiles_public_base_url", "") or "").strip()
    if tiles_base:
        origin = extract_bare_origin(tiles_base)
        if origin:
            candidates.append(origin)

    pub_base = get_mcp_public_base_url_setting(settings) or (public_origin or "")
    if pub_base:
        origin = extract_bare_origin(pub_base)
        if origin:
            candidates.append(origin)

    deduped: list[str] = []
    for origin in candidates:
        if origin not in deduped:
            deduped.append(origin)
    return deduped


def resolve_public_origin_from_headers(
    headers: Mapping[str, str],
    *,
    scheme: str = "http",
    cfg: Settings | None = None,
) -> str:
    """Resolve canonical public origin (``TRAILFORGE_PUBLIC_BASE_URL`` > proxy headers > Host)."""
    settings = cfg or get_mcp_settings()
    configured = get_mcp_public_base_url_setting(settings)
    if configured:
        bare = extract_bare_origin(configured)
        return bare or configured

    lower_headers = {k.lower(): v for k, v in headers.items()}
    trust_proxy = bool(getattr(settings, "trust_proxy_headers", True))
    if trust_proxy:
        fwd_host = (lower_headers.get("x-forwarded-host") or "").split(",")[0].strip()
        fwd_proto = (lower_headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if is_valid_host_header(fwd_host):
            proto = fwd_proto if fwd_proto in ("http", "https") else scheme
            if proto not in ("http", "https"):
                proto = "http"
            return f"{proto}://{fwd_host}"

    host = (lower_headers.get("host") or "").strip()
    if is_valid_host_header(host):
        proto = scheme if scheme in ("http", "https") else "http"
        return f"{proto}://{host}"

    return "http://localhost:8000"


def get_mcp_public_base_url(cfg: Settings | None = None) -> str:
    """Return canonical public base URL for deep links and export URLs."""
    configured = get_mcp_public_base_url_setting(cfg)
    if configured:
        return configured
    bound = _current_public_origin.get()
    if bound:
        return bound.rstrip("/")
    return "http://localhost:8000"


# -----------------------------------------------------------------------------
# Auth Resolver (Auto-Wires W1 `trailforge.auth.tokens` When Present)
# -----------------------------------------------------------------------------


def parse_bearer_token(authorization_header: str | None) -> str | None:
    """Extract Bearer token string from an HTTP Authorization header."""
    if not authorization_header:
        return None
    scheme, _, creds = authorization_header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = creds.strip()
    return token or None


def _coerce_auth_context(result: Any) -> McpAuthContext:
    """Convert a W1 or custom auth context object into W2's ``McpAuthContext``."""
    if isinstance(result, McpAuthContext):
        return result
    if result is None:
        return ANONYMOUS_CONTEXT
    user = getattr(result, "user", None)
    auth_method = getattr(result, "auth_method", "anonymous")
    raw_scopes = getattr(result, "scopes", ())
    scopes = frozenset(str(s) for s in raw_scopes) if raw_scopes else frozenset()
    token_id = getattr(result, "token_id", None)
    client_id = getattr(result, "client_id", None)
    return McpAuthContext(
        user=user,
        auth_method=auth_method,
        scopes=scopes,
        token_id=token_id,
        client_id=client_id,
    )


def _try_w1_token_resolver(token: str, db: Session) -> McpAuthContext | None:
    """Delegate to W1's ``trailforge.auth.tokens`` unified Bearer resolver if installed."""
    try:
        mod = importlib.import_module("trailforge.auth.tokens")
    except ModuleNotFoundError:
        return None

    for fn_name in (
        "resolve_bearer_token",
        "resolve_mcp_auth_context",
        "resolve_auth_context",
        "verify_bearer_token",
    ):
        fn = getattr(mod, fn_name, None)
        if not callable(fn):
            continue
        try:
            sig = inspect.signature(fn)
            params = list(sig.parameters.values())
            if len(params) >= 2:
                first_name = params[0].name.lower()
                resolved = fn(db, token) if first_name in ("db", "session") else fn(token, db)
            else:
                continue
            return _coerce_auth_context(resolved)
        except Exception as exc:
            logger.debug("W1 token resolver %s raised %s; trying fallback", fn_name, exc)
            continue
    return None


def default_auth_resolver(authorization_header: str | None, db: Session) -> McpAuthContext:
    """Resolve ``Authorization: Bearer ...`` via W1 PAT/OAuth resolver or session JWT fallback."""
    token = parse_bearer_token(authorization_header)
    if not token:
        return ANONYMOUS_CONTEXT

    # Prefer W1's unified PAT + OAuth + session resolver when present in the codebase
    w1_ctx = _try_w1_token_resolver(token, db)
    if w1_ctx is not None:
        return w1_ctx

    # Standalone fallback: verify existing TrailForge session tokens (enforcing active + tv)
    payload = verify_access_token(token)
    if payload is None:
        return ANONYMOUS_CONTEXT
    try:
        user_uuid = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError):
        return ANONYMOUS_CONTEXT

    user = db.get(User, user_uuid)
    if user is None or not user.is_active:
        return ANONYMOUS_CONTEXT

    raw_tv = payload.get("tv", 1)
    if isinstance(raw_tv, bool) or not isinstance(raw_tv, int):
        return ANONYMOUS_CONTEXT
    user_tv = int(getattr(user, "token_version", 1) or 1)
    if int(raw_tv) != user_tv:
        return ANONYMOUS_CONTEXT

    return McpAuthContext(
        user=user,
        auth_method="session",
        scopes=ALL_MCP_SCOPES,
    )


async def invoke_auth_resolver(
    resolver: McpAuthResolver,
    authorization_header: str | None,
    db: Session,
) -> McpAuthContext:
    """Invoke either a sync or async auth resolver and coerce its result."""
    res = resolver(authorization_header, db)
    if inspect.isawaitable(res):
        res = await res
    return _coerce_auth_context(res)
