"""FastAPI entrypoint and application factory for TrailForge."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from trailforge import SERVICE_NAME, __version__
from trailforge.api.auth import router as auth_router
from trailforge.api.closures import router as closures_router
from trailforge.api.grading import router as grading_router
from trailforge.api.health import router as health_router
from trailforge.api.packs import router as packs_router
from trailforge.api.reports import router as reports_router
from trailforge.api.tiles import router as tiles_router
from trailforge.api.tracks import router as tracks_router
from trailforge.api.trips import router as trips_router
from trailforge.api.user_data import router as user_data_router
from trailforge.api.vehicles import router as vehicles_router
from trailforge.config import Settings, get_settings
from trailforge.db.session import reset_engine

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct and configure the TrailForge FastAPI application."""
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(
            level=getattr(logging, cfg.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
        logger.info("Starting %s v%s", SERVICE_NAME, __version__)
        try:
            yield
        finally:
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
        lifespan=lifespan,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(health_router)
    application.include_router(tiles_router)
    application.include_router(tracks_router)
    application.include_router(closures_router)
    application.include_router(grading_router)
    application.include_router(packs_router)
    application.include_router(reports_router)
    application.include_router(trips_router)
    application.include_router(auth_router)
    application.include_router(vehicles_router)
    application.include_router(user_data_router)

    @application.get("/", tags=["meta"])
    def root() -> dict[str, str]:
        return {
            "service": SERVICE_NAME,
            "version": __version__,
            "health": "/api/health",
            "docs": "/docs",
        }

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
