"""Health endpoint reporting API, PostGIS, and Alembic schema status."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

from trailforge import SERVICE_NAME, __version__
from trailforge.db.session import DbHealthStatus, check_db_health

router = APIRouter(prefix="/api", tags=["health"])


class HealthResponse(BaseModel):
    """Structured liveness/readiness response for /api/health."""

    status: Literal["ok", "degraded"]
    service: str
    version: str
    db_ok: bool
    postgis_version: str | None = None
    schema_revision: str | None = None


def get_health_checker() -> Callable[[], DbHealthStatus]:
    """Dependency indirection so offline unit tests can inject synthetic DB states."""
    return check_db_health


@router.get("/health", response_model=HealthResponse)
def api_health(
    response: Response,
    checker: Callable[[], DbHealthStatus] = Depends(get_health_checker),
) -> HealthResponse:
    """Return service health, PostGIS version, and active Alembic migration revision."""
    db_status = checker()
    if not db_status.db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if db_status.db_ok else "degraded",
        service=SERVICE_NAME,
        version=__version__,
        db_ok=db_status.db_ok,
        postgis_version=db_status.postgis_version,
        schema_revision=db_status.schema_revision,
    )
