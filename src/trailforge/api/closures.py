"""REST API for Pan-Australia closures, incident alerts, GeoJSON overlays, and feed status."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from trailforge.db.session import get_db
from trailforge.feeds.adapters import build_default_adapters
from trailforge.feeds.queries import (
    get_closures_geojson,
    get_feed_health_summary,
    list_closures,
)
from trailforge.feeds.sync import SyncRunSummary, sync_configured_feeds, sync_from_fixtures

router = APIRouter(prefix="/api/closures", tags=["closures"])

_FIXTURES_ROOT = Path("tests/fixtures").resolve()
_DEFAULT_FIXTURES_PATH = _FIXTURES_ROOT / "closures_sample.json"


def _resolve_confined_fixtures_path(raw_path: str | None) -> Path:
    """Resolve and strictly confine ``fixtures_path`` inside ``tests/fixtures``."""
    root = _FIXTURES_ROOT
    if not raw_path:
        raw = os.environ.get("TRAILFORGE_CLOSURES_FIXTURES_PATH") or str(_DEFAULT_FIXTURES_PATH)
    else:
        raw = raw_path

    if "\x00" in raw:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid fixtures_path",
        )

    p = Path(raw)
    candidate = (
        (root / p).resolve() if not p.is_absolute() and len(p.parts) == 1 else p.resolve()
    )

    if not candidate.is_relative_to(root) or candidate.suffix.lower() != ".json":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="fixtures_path must resolve to a .json file inside tests/fixtures",
        )
    if not candidate.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Closures fixture file not found: {candidate.name}",
        )
    return candidate


class ClosureSyncRequest(BaseModel):
    """Optional request payload for ``POST /api/closures/sync``."""

    use_fixtures: bool = Field(
        default=True,
        description="Sync offline fixture payloads instead of calling external agency URLs.",
    )
    fixtures_path: str | None = Field(
        default=None,
        description="Optional override path to closures_sample.json.",
    )
    feeds: list[str] | None = Field(
        default=None,
        description="Optional subset of feed names or aliases to sync.",
    )
    states: list[str] | None = Field(
        default=None,
        description=(
            "Optional list of Australian state codes (or ['ALL']) to sync; "
            "defaults to TRAILFORGE_ENABLED_STATES."
        ),
    )


@router.get("")
def get_closures(
    active_only: bool = Query(default=True),
    state: str | None = Query(
        default=None,
        description="Optional AU state code or comma-separated list (e.g. QLD or VIC,NSW,QLD).",
    ),
    severity: str | None = Query(default=None),
    feed_source: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """List normalized Australian closures, emergency incidents, park alerts, and OSM gates."""
    try:
        return list_closures(
            db,
            active_only=active_only,
            state=state,
            severity=severity,
            feed_source=feed_source,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.get("/geojson")
def get_closures_feature_collection(
    active_only: bool = Query(default=True),
    state: str | None = Query(
        default=None,
        description="Optional AU state code or comma-separated list (e.g. QLD or VIC,NSW,QLD).",
    ),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return RFC 7946 FeatureCollection of affected track segments & alert geometries."""
    try:
        return get_closures_geojson(db, active_only=active_only, state=state)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.get("/status")
def get_closures_status(
    state: str | None = Query(
        default=None,
        description="Optional AU state code, comma-separated list, or ALL.",
    ),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return live feed sync health and staleness indicators for configured AU feeds."""
    try:
        return get_feed_health_summary(db, state=state)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.post("/sync", response_model=SyncRunSummary)
def trigger_closures_sync(
    body: ClosureSyncRequest | None = None,
    db: Session = Depends(get_db),
) -> SyncRunSummary:
    """Trigger an on-demand sync of closure feeds (defaults to offline fixtures when available)."""
    req = body or ClosureSyncRequest()
    try:
        if req.use_fixtures:
            fpath = _resolve_confined_fixtures_path(req.fixtures_path)
            return sync_from_fixtures(db, fpath, only_feeds=req.feeds, states=req.states)

        adapters = build_default_adapters(only_feeds=req.feeds, states=req.states)
        return sync_configured_feeds(db, adapters=adapters)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
