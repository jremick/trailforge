"""REST API for Slice 07 live crowdsourced grading, obstacle hotspots, and 72h rainfall."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from trailforge.auth.dependencies import require_admin
from trailforge.db.models import User
from trailforge.db.session import get_db
from trailforge.grading.live import (
    LiveGradingRecomputeSummary,
    get_obstacle_hotspots_geojson,
    recompute_live_grades,
)
from trailforge.grading.rainfall import get_rainfall_summary

router = APIRouter(prefix="/api/grading", tags=["grading"])


class RecomputeGradingRequest(BaseModel):
    """Optional payload for ``POST /api/grading/recompute``."""

    rain_72h_mm: float | None = Field(
        default=None,
        ge=0.0,
        le=2000.0,
        description="Optional deterministic 72-hour rainfall override (mm) for offline CI/demo.",
    )
    rain_24h_mm: float | None = Field(
        default=None,
        ge=0.0,
        le=1000.0,
        description="Optional deterministic 24-hour rainfall override (mm).",
    )
    fetch_live_rainfall: bool = Field(
        default=False,
        description="When true (and no override given), fetch live Open-Meteo 72h rainfall.",
    )


@router.post("/recompute", response_model=LiveGradingRecomputeSummary)
def trigger_live_grading_recompute(
    body: RecomputeGradingRequest | None = None,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> LiveGradingRecomputeSummary:
    """Recompute live crowdsourced difficulty grades, obstacle hotspots, and 72h rainfall."""
    req = body or RecomputeGradingRequest()
    summary = recompute_live_grades(
        db,
        rain_override_72h_mm=req.rain_72h_mm,
        rain_override_24h_mm=req.rain_24h_mm,
        fetch_live_rainfall=req.fetch_live_rainfall,
    )
    db.commit()
    return summary


@router.get("/hotspots/geojson")
def get_grading_hotspots_geojson(
    response: Response,
    track_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return RFC 7946 FeatureCollection of rough/slow obstacle hotspot segments."""
    response.headers["Cache-Control"] = "no-store"
    return get_obstacle_hotspots_geojson(db, track_id=track_id)


@router.get("/rainfall")
def get_grading_rainfall(
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return latest 72-hour regional rainfall snapshots and Open-Meteo CC BY 4.0 attribution."""
    return get_rainfall_summary(db)
