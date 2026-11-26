"""Stateless Streamable HTTP MCP Server & ASGI apps for `/mcp` and `/mcp/auth`.

Integrator wiring contract for `src/trailforge/api/main.py`:
    from trailforge.mcp.server import create_mcp_asgi_apps, mcp_lifespan

    mcp_apps = create_mcp_asgi_apps(auth_resolver=..., settings=cfg)
    mcp_apps.register(application)
    # inside FastAPI lifespan:
    async with mcp_lifespan(mcp_apps):
        yield
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Final

import anyio
from anyio.abc import TaskStatus
from fastapi import FastAPI
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyUrl
from sqlalchemy.orm import Session
from starlette.routing import Route
from starlette.types import Message, Receive, Scope, Send

from trailforge import __version__
from trailforge.config import Settings
from trailforge.mcp.context import (
    ANONYMOUS_CONTEXT,
    McpAuthContext,
    McpAuthResolver,
    default_auth_resolver,
    extract_bare_origin,
    get_mcp_auth_mode,
    get_mcp_settings,
    invoke_auth_resolver,
    is_mcp_enabled,
    open_mcp_db_session,
    parse_bearer_token,
    reset_mcp_auth_context,
    reset_mcp_db_factory,
    reset_mcp_public_origin,
    reset_mcp_settings,
    resolve_public_origin_from_headers,
    set_mcp_auth_context,
    set_mcp_db_factory,
    set_mcp_public_origin,
    set_mcp_settings,
)
from trailforge.mcp.prompts import get_mcp_prompt, list_mcp_prompts
from trailforge.mcp.resources import list_mcp_resources, read_mcp_resource
from trailforge.mcp.tools import call_mcp_tool, list_mcp_tools

logger = logging.getLogger(__name__)

MAX_MCP_REQUEST_BODY_BYTES: Final[int] = 1 * 1024 * 1024  # 1 MiB streaming body cap (SC-10)
MCP_EXPOSED_HEADERS: Final[str] = (
    "Mcp-Session-Id, WWW-Authenticate, X-RateLimit-Limit, X-RateLimit-Remaining"
)
MCP_ALLOWED_METHODS: Final[str] = "GET, POST, DELETE, OPTIONS"
MCP_ALLOWED_HEADERS: Final[str] = (
    "Authorization, Content-Type, Accept, Mcp-Session-Id, MCP-Protocol-Version, Last-Event-ID"
)

_SERVER_INSTRUCTIONS: Final[str] = (
    "TrailForge Australian 4WD Trail Intelligence MCP server. Always check official closures, "
    "community gate warnings, 72 h rainfall wet-risk, and vehicle suitability before recommending "
    "routes. Always preserve OpenStreetMap (ODbL), Geoscience Australia / DEA (CC BY 4.0), "
    "CAPAD, state agency, and Open-Meteo (CC BY 4.0) attributions."
)


def create_mcp_server() -> Server:
    """Create and configure the low-level TrailForge MCP ``Server``."""
    server = Server(
        name="trailforge",
        version=__version__,
        instructions=_SERVER_INSTRUCTIONS,
    )

    @server.list_tools()
    async def _handle_list_tools() -> list[types.Tool]:
        return list_mcp_tools()

    @server.call_tool(validate_input=False)
    async def _handle_call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> types.CallToolResult:
        # Run synchronous PostGIS / Pillow work on worker thread so event loop stays responsive
        return await anyio.to_thread.run_sync(call_mcp_tool, name, arguments or {})

    @server.list_resources()
    async def _handle_list_resources() -> list[types.Resource]:
        return list_mcp_resources()

    @server.read_resource()
    async def _handle_read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
        return read_mcp_resource(uri)

    @server.list_prompts()
    async def _handle_list_prompts() -> list[types.Prompt]:
        return list_mcp_prompts()

    @server.get_prompt()
    async def _handle_get_prompt(
        name: str, arguments: dict[str, str] | None
    ) -> types.GetPromptResult:
        return get_mcp_prompt(name, arguments)

    return server


def _decode_scope_headers(scope: Scope) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_k, raw_v in scope.get("headers") or ():
        headers[raw_k.decode("latin-1").lower()] = raw_v.decode("latin-1")
    return headers


def _build_cors_headers(origin: str | None) -> list[tuple[bytes, bytes]]:
    # Bearer-only / public discovery MCP routes never use cookies -> safe wildcard or bare origin
    raw_origin = (origin or "").strip()
    bare = extract_bare_origin(raw_origin) if raw_origin else None
    allow_origin = bare if (bare is not None and bare == raw_origin.lower()) else "*"
    return [
        (b"access-control-allow-origin", allow_origin.encode("latin-1")),
        (b"access-control-allow-methods", MCP_ALLOWED_METHODS.encode("latin-1")),
        (b"access-control-allow-headers", MCP_ALLOWED_HEADERS.encode("latin-1")),
        (b"access-control-expose-headers", MCP_EXPOSED_HEADERS.encode("latin-1")),
        (b"vary", b"Origin"),
    ]


async def _send_json_response(
    send: Send,
    status_code: int,
    payload: dict[str, Any],
    *,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps(payload).encode("utf-8")
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _send_empty_response(
    send: Send,
    status_code: int,
    *,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    headers: list[tuple[bytes, bytes]] = [(b"content-length", b"0")]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": b""})


def _normalize_accept_header_scope(scope: Scope, headers: dict[str, str]) -> Scope:
    """Ensure minimal HTTP JSON-RPC clients omitting Accept or sending `*/*` work smoothly."""
    if (scope.get("method") or "").upper() != "POST":
        return scope
    accept = (headers.get("accept") or "").strip().lower()
    if not accept or accept == "*/*":
        patched_headers = [
            (k, v)
            for k, v in (scope.get("headers") or ())
            if k.decode("latin-1").lower() != "accept"
        ]
        patched_headers.append((b"accept", b"application/json, text/event-stream"))
        patched = dict(scope)
        patched["headers"] = patched_headers
        return patched
    return scope


class TrailForgeMcpAsgiApp:
    """ASGI handler wrapping stateless Streamable HTTP MCP with auth, CORS, and RFC 9728 401s."""

    def __init__(
        self,
        *,
        server: Server,
        session_manager: StreamableHTTPSessionManager,
        resource_path: str,
        require_auth: bool,
        auth_resolver: McpAuthResolver = default_auth_resolver,
        settings: Settings | None = None,
        db_session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self.server = server
        self.session_manager = session_manager
        self.resource_path = resource_path
        self.require_auth = require_auth
        self.auth_resolver = auth_resolver
        self.settings = settings
        self.db_session_factory = db_session_factory

    def _resolve_settings(self, scope: Scope) -> Settings:
        if self.settings is not None:
            return self.settings
        app = scope.get("app")
        app_state = getattr(app, "state", None)
        custom = getattr(app_state, "custom_settings", None) or getattr(app_state, "settings", None)
        return custom if isinstance(custom, Settings) else get_mcp_settings()

    def _build_www_authenticate(self, public_origin: str) -> str:
        safe_origin = extract_bare_origin(public_origin) or "http://localhost:8000"
        meta_url = f"{safe_origin}/.well-known/oauth-protected-resource{self.resource_path}"
        return f'Bearer resource_metadata="{meta_url}", scope="trailforge:read trailforge:write"'

    async def _dispatch_stateless_fallback(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Handle a stateless request cleanly even if caller skipped lifespan startup."""
        transport = StreamableHTTPServerTransport(
            mcp_session_id=None,
            is_json_response_enabled=self.session_manager.json_response,
            event_store=None,
            security_settings=self.session_manager.security_settings,
        )

        async def _run_server(*, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
            async with transport.connect() as (read_stream, write_stream):
                task_status.started()
                await self.server.run(
                    read_stream,
                    write_stream,
                    self.server.create_initialization_options(),
                    stateless=True,
                )

        async with anyio.create_task_group() as tg:
            try:
                await tg.start(_run_server)
                await transport.handle_request(scope, receive, send)
            finally:
                with anyio.CancelScope(shield=True):
                    await transport.terminate()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return

        cfg = self._resolve_settings(scope)
        headers = _decode_scope_headers(scope)
        origin = headers.get("origin")
        cors_headers = _build_cors_headers(origin)

        # Master kill switch (SC-1)
        if not is_mcp_enabled(cfg):
            await _send_json_response(
                send,
                404,
                {"detail": "MCP endpoint is disabled"},
                extra_headers=cors_headers,
            )
            return

        auth_mode = get_mcp_auth_mode(cfg)
        if self.require_auth and auth_mode == "anonymous_only":
            await _send_json_response(
                send,
                404,
                {"detail": "Authenticated MCP endpoint is disabled in anonymous_only mode"},
                extra_headers=cors_headers,
            )
            return

        method = (scope.get("method") or "GET").upper()
        if method == "OPTIONS":
            await _send_empty_response(send, 204, extra_headers=cors_headers)
            return

        public_origin = resolve_public_origin_from_headers(
            headers,
            scheme=str(scope.get("scheme") or "http"),
            cfg=cfg,
        )
        www_auth = self._build_www_authenticate(public_origin)

        # Resolve Authorization header into McpAuthContext
        auth_header = headers.get("authorization")
        must_authenticate = self.require_auth or auth_mode == "oauth_required"
        auth_ctx: McpAuthContext = ANONYMOUS_CONTEXT

        if auth_header:
            if not parse_bearer_token(auth_header):
                await _send_json_response(
                    send,
                    401,
                    {"detail": "Invalid Authorization header; expected Bearer token"},
                    extra_headers=[
                        *cors_headers,
                        (b"www-authenticate", www_auth.encode("latin-1")),
                    ],
                )
                return

            with open_mcp_db_session(self.db_session_factory) as auth_db:
                auth_ctx = await invoke_auth_resolver(self.auth_resolver, auth_header, auth_db)

            if not auth_ctx.is_authenticated:
                await _send_json_response(
                    send,
                    401,
                    {"detail": "Invalid, expired, or revoked Bearer token"},
                    extra_headers=[
                        *cors_headers,
                        (b"www-authenticate", www_auth.encode("latin-1")),
                    ],
                )
                return
        elif must_authenticate:
            await _send_json_response(
                send,
                401,
                {"detail": "Authentication required"},
                extra_headers=[
                    *cors_headers,
                    (b"www-authenticate", www_auth.encode("latin-1")),
                ],
            )
            return

        # Wrap ASGI send so CORS / Expose-Headers appear on every MCP response
        async def _send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = list(message.get("headers") or ())
                existing_names = {k.lower() for k, _ in existing}
                for ck, cv in cors_headers:
                    if ck not in existing_names:
                        existing.append((ck, cv))
                message = {**message, "headers": existing}
            await send(message)

        patched_scope = _normalize_accept_header_scope(scope, headers)

        tok_auth = set_mcp_auth_context(auth_ctx)
        tok_origin = set_mcp_public_origin(public_origin)
        tok_cfg = set_mcp_settings(cfg)
        tok_db = set_mcp_db_factory(self.db_session_factory)
        try:
            if getattr(self.session_manager, "_task_group", None) is not None:
                await self.session_manager.handle_request(patched_scope, receive, _send_with_cors)
            else:
                await self._dispatch_stateless_fallback(patched_scope, receive, _send_with_cors)
        finally:
            reset_mcp_db_factory(tok_db)
            reset_mcp_settings(tok_cfg)
            reset_mcp_public_origin(tok_origin)
            reset_mcp_auth_context(tok_auth)


@dataclass(frozen=True, slots=True)
class McpAsgiApps:
    """Bundle of stateless TrailForge MCP ASGI endpoints and lifecycle manager."""

    hybrid_app: TrailForgeMcpAsgiApp
    auth_app: TrailForgeMcpAsgiApp
    server: Server
    session_manager: StreamableHTTPSessionManager
    routes: tuple[Route, ...]

    def register(self, app: FastAPI) -> None:
        """Register `/mcp`, `/mcp/`, `/mcp/auth`, `/mcp/auth/` directly (zero 307 redirects)."""
        app.router.routes.extend(self.routes)

    def lifespan(self) -> contextlib.AbstractAsyncContextManager[None]:
        """Return async context manager managing the stateless Streamable HTTP task group."""
        return mcp_lifespan(self)


def create_mcp_asgi_apps(
    *,
    auth_resolver: McpAuthResolver = default_auth_resolver,
    settings: Settings | None = None,
    db_session_factory: Callable[[], Session] | None = None,
    json_response: bool = True,
) -> McpAsgiApps:
    """Create the stateless Streamable HTTP ASGI apps for `/mcp` and `/mcp/auth`."""
    server = create_mcp_server()
    session_manager = StreamableHTTPSessionManager(
        app=server,
        event_store=None,
        json_response=json_response,
        stateless=True,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        max_request_body_size=MAX_MCP_REQUEST_BODY_BYTES,
    )

    hybrid_app = TrailForgeMcpAsgiApp(
        server=server,
        session_manager=session_manager,
        resource_path="/mcp",
        require_auth=False,
        auth_resolver=auth_resolver,
        settings=settings,
        db_session_factory=db_session_factory,
    )
    auth_app = TrailForgeMcpAsgiApp(
        server=server,
        session_manager=session_manager,
        resource_path="/mcp/auth",
        require_auth=True,
        auth_resolver=auth_resolver,
        settings=settings,
        db_session_factory=db_session_factory,
    )

    methods = ["GET", "POST", "DELETE", "OPTIONS"]
    routes = (
        Route("/mcp/auth", endpoint=auth_app, methods=methods, name="mcp_auth"),
        Route("/mcp/auth/", endpoint=auth_app, methods=methods, name="mcp_auth_slash"),
        Route("/mcp", endpoint=hybrid_app, methods=methods, name="mcp_hybrid"),
        Route("/mcp/", endpoint=hybrid_app, methods=methods, name="mcp_hybrid_slash"),
    )

    return McpAsgiApps(
        hybrid_app=hybrid_app,
        auth_app=auth_app,
        server=server,
        session_manager=session_manager,
        routes=routes,
    )


@contextlib.asynccontextmanager
async def mcp_lifespan(apps: McpAsgiApps | None = None) -> AsyncIterator[None]:
    """Run the MCP Streamable HTTP session manager inside FastAPI's lifespan."""
    if apps is None:
        yield
        return
    async with apps.session_manager.run():
        yield
