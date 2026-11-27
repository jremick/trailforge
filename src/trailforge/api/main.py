"""FastAPI entrypoint and application factory for TrailForge."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from trailforge import SERVICE_NAME, __version__
from trailforge.api.admin import router as admin_router
from trailforge.api.auth import router as auth_router
from trailforge.api.closures import router as closures_router
from trailforge.api.grading import router as grading_router
from trailforge.api.health import router as health_router
from trailforge.api.middleware import (
    BodySizeLimitMiddleware,
    CookieCSRFMiddleware,
    PathAwareCORSMiddleware,
    SecurityHeadersMiddleware,
    SelectiveGZipMiddleware,
    SlidingWindowRateLimiter,
    UsageAndRateLimitMiddleware,
    flush_usage_metrics,
)
from trailforge.api.oauth import oauth_router, tokens_router, well_known_router
from trailforge.api.packs import router as packs_router
from trailforge.api.preferences import router as preferences_router
from trailforge.api.reports import router as reports_router
from trailforge.api.routes import router as routes_router
from trailforge.api.search import router as search_router
from trailforge.api.tiles import router as tiles_router
from trailforge.api.tracks import router as tracks_router
from trailforge.api.trips import router as trips_router
from trailforge.api.user_data import router as user_data_router
from trailforge.api.vehicles import router as vehicles_router
from trailforge.auth import security as auth_security
from trailforge.auth.tokens import resolve_bearer_token
from trailforge.config import (
    DEV_DEFAULT_AUTH_SECRET_KEY,
    DEV_DEFAULT_PRIVACY_SECRET_KEY,
    Settings,
    get_settings,
)
from trailforge.db.session import reset_engine
from trailforge.mcp.context import ANONYMOUS_CONTEXT, McpAuthContext, parse_bearer_token
from trailforge.mcp.server import create_mcp_asgi_apps, mcp_lifespan
from trailforge.reports.photos import PhotoStorageQuotaError

logger = logging.getLogger(__name__)

_MIN_SECRET_LENGTH = 32


def _validate_startup_secrets(cfg: Settings) -> None:
    """Fail closed at app creation if production mode runs with default or weak secrets."""
    validator = getattr(auth_security, "validate_production_secrets", None)
    if callable(validator):
        validator(cfg)
        return

    issues: list[str] = []
    auth_key = cfg.auth_secret_key.strip()
    if auth_key == DEV_DEFAULT_AUTH_SECRET_KEY or len(auth_key) < _MIN_SECRET_LENGTH:
        issues.append(
            "AUTH_SECRET_KEY must be changed from the dev default and be at least 32 characters"
        )

    privacy_key = cfg.privacy_secret_key.strip()
    if privacy_key == DEV_DEFAULT_PRIVACY_SECRET_KEY or len(privacy_key) < _MIN_SECRET_LENGTH:
        issues.append(
            "TRAILFORGE_PRIVACY_SECRET must be changed from the dev default and be at least "
            "32 characters"
        )

    if not issues:
        return

    summary = "; ".join(issues)
    if cfg.is_production_mode:
        raise RuntimeError(
            f"Refusing to start TrailForge in production mode with unsafe secrets: {summary}. "
            "Generate strong random secrets with `openssl rand -hex 32`."
        )
    logger.warning(
        "TrailForge is running in local development mode with non-production secrets: %s",
        summary,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct and configure the TrailForge FastAPI application."""
    cfg = settings or get_settings()
    _validate_startup_secrets(cfg)
    docs_enabled = cfg.docs_enabled

    def _resolve_mcp_bearer(authorization_header: str | None, db: Session) -> McpAuthContext:
        token = parse_bearer_token(authorization_header)
        if not token:
            return ANONYMOUS_CONTEXT
        active_cfg = settings or get_settings()
        resolved = resolve_bearer_token(token, db, settings=active_cfg)
        if resolved is None:
            return ANONYMOUS_CONTEXT
        return McpAuthContext(
            user=resolved.user,
            auth_method=resolved.auth_method,
            scopes=resolved.scopes,
            token_id=resolved.token_id,
            client_id=resolved.client_id,
        )

    mcp_apps = create_mcp_asgi_apps(
        auth_resolver=_resolve_mcp_bearer,
        settings=settings,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(
            level=getattr(logging, cfg.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
        logger.info("Starting %s v%s", SERVICE_NAME, __version__)
        try:
            async with mcp_lifespan(mcp_apps):
                yield
        finally:
            flush_usage_metrics()
            reset_engine()
            logger.info("Shutdown complete for %s", SERVICE_NAME)

    application = FastAPI(
        title="TrailForge API",
        description=(
            "Open-source 4WD & off-road mapping, live track-grading, and trail-intelligence "
            "platform for Victoria & New South Wales."
        ),
        version=__version__,
        license_info={"name": "GPL-3.0-or-later", "identifier": "GPL-3.0-or-later"},
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
        lifespan=lifespan,
    )

    rate_limiter = SlidingWindowRateLimiter()
    application.state.rate_limiter = rate_limiter
    application.state.settings = cfg
    application.state.mcp_apps = mcp_apps
    if settings is not None:
        application.state.custom_settings = settings

    @application.exception_handler(PhotoStorageQuotaError)
    async def _photo_quota_exception_handler(
        _request: Request,
        exc: PhotoStorageQuotaError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            content={"detail": str(exc)},
        )

    # Middleware execution order (outermost -> innermost):
    # SecurityHeaders -> PathAwareCORS -> UsageAndRateLimit (incl. Origin Secret)
    #   -> CookieCSRF -> BodySizeLimit -> FastAPI route handlers & MCP ASGI apps.
    # Registering inner middlewares first keeps CORS & security headers on 403/413/429/507.
    application.add_middleware(SelectiveGZipMiddleware, minimum_size=1024)
    application.add_middleware(BodySizeLimitMiddleware)
    application.add_middleware(CookieCSRFMiddleware, settings=settings)
    application.add_middleware(
        UsageAndRateLimitMiddleware,
        settings=settings,
        rate_limiter=rate_limiter,
    )
    application.add_middleware(
        PathAwareCORSMiddleware,
        allow_origins=cfg.cors_origins_list,
    )
    application.add_middleware(SecurityHeadersMiddleware, settings=settings)

    application.include_router(health_router)
    application.include_router(tiles_router)
    application.include_router(tracks_router)
    application.include_router(search_router)
    application.include_router(closures_router)
    application.include_router(grading_router)
    application.include_router(packs_router)
    application.include_router(reports_router)
    application.include_router(trips_router)
    application.include_router(auth_router)
    application.include_router(well_known_router)
    application.include_router(oauth_router)
    application.include_router(tokens_router)
    application.include_router(vehicles_router)
    application.include_router(user_data_router)
    application.include_router(routes_router)
    application.include_router(preferences_router)
    application.include_router(admin_router)
    mcp_apps.register(application)

    @application.get("/", tags=["meta"])
    def root() -> dict[str, str]:
        payload = {
            "service": SERVICE_NAME,
            "version": __version__,
            "health": "/api/health",
        }
        if docs_enabled:
            payload["docs"] = "/docs"
        return payload

    return application


app = create_app()


def run() -> None:
    """CLI entrypoint (`trailforge-api`) launching Uvicorn."""
    cfg = get_settings()
    uvicorn.run(
        "trailforge.api.main:app",
        host=cfg.api_host,
        port=cfg.api_port,
        log_level=cfg.log_level.lower(),
    )
