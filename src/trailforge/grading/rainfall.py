"""Open-Meteo 72-hour precipitation connector and wet-condition risk modulation (Slice 07).

Provides:
  - Spatial 0.15-degree grid cell snapping (``r_<lon>_<lat>``) for regional rainfall caching.
  - Injectable ``httpx.Client`` Open-Meteo forecast/past-days connector with CC BY 4.0 attribution.
  - Persistence to PostGIS ``rainfall_snapshots`` with graceful outage fallback (``is_stale=True``).
  - Transparent per-segment wet-condition risk classification & bounded score delta
    (0 pts on sealed/bridged surfaces; up to +12 pts on mud/clay/unbridged creeks in heavy rain).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import httpx
from geoalchemy2.shape import from_shape
from pydantic import BaseModel, Field
from shapely.geometry import Point
from sqlalchemy import desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from trailforge.db.models import RainfallSnapshot
from trailforge.grading.baseline import WetRiskLiteral
from trailforge.ingestion.connectors.base import (
    ATTRIBUTION_OPEN_METEO,
    IDENTITY_HTTP_HEADERS,
    read_bounded_json,
    stream_upstream,
)

logger = logging.getLogger(__name__)

OPEN_METEO_FORECAST_URL: Final[str] = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_SOURCE: Final[str] = "Open-Meteo"
OPEN_METEO_ATTRIBUTION: Final[str] = ATTRIBUTION_OPEN_METEO
RAINFALL_GRID_STEP_DEG: Final[float] = 0.15
RAINFALL_STALE_AFTER_HOURS: Final[float] = 12.0
MAX_WET_SCORE_ADJUSTMENT: Final[float] = 12.0
_MAX_OPEN_METEO_BYTES: Final[int] = 128 * 1024
_MAX_OPEN_METEO_HOURS: Final[int] = 192  # Up to one week of history plus a day of forecast

ApiWetRiskLiteral = Literal["dry", "damp", "wet", "heavy_rain"]
DbWetRiskLiteral = WetRiskLiteral  # "dry" | "damp" | "wet" | "saturated"

_SURFACE_WET_DELTA: Final[dict[str, dict[ApiWetRiskLiteral, float]]] = {
    "sealed": {"dry": 0.0, "damp": 0.0, "wet": 0.0, "heavy_rain": 0.0},
    "gravel_compacted": {"dry": 0.0, "damp": 1.0, "wet": 3.0, "heavy_rain": 5.0},
    "dirt_unsealed": {"dry": 0.0, "damp": 2.0, "wet": 6.0, "heavy_rain": 9.5},
    "unknown": {"dry": 0.0, "damp": 2.0, "wet": 6.0, "heavy_rain": 9.5},
    "rough_rock_mud_sand": {"dry": 0.0, "damp": 2.5, "wet": 7.0, "heavy_rain": 10.0},
}

_CROSSING_WET_DELTA_INTERMITTENT: Final[dict[ApiWetRiskLiteral, float]] = {
    "dry": 0.0,
    "damp": 0.5,
    "wet": 1.5,
    "heavy_rain": 2.0,
}

_CROSSING_WET_DELTA_PERENNIAL: Final[dict[ApiWetRiskLiteral, float]] = {
    "dry": 0.0,
    "damp": 1.0,
    "wet": 2.5,
    "heavy_rain": 3.0,
}

_API_TO_DB_WET_RISK: Final[dict[ApiWetRiskLiteral, DbWetRiskLiteral]] = {
    "dry": "dry",
    "damp": "damp",
    "wet": "wet",
    "heavy_rain": "saturated",
}

_DB_TO_API_WET_RISK: Final[dict[str, ApiWetRiskLiteral]] = {
    "dry": "dry",
    "damp": "damp",
    "wet": "wet",
    "saturated": "heavy_rain",
    "heavy_rain": "heavy_rain",
}


class RainfallReading(BaseModel):
    """Normalized regional rainfall snapshot for a 0.15-degree spatial grid cell."""

    grid_cell_id: str
    centroid_lon: float
    centroid_lat: float
    source: str = OPEN_METEO_SOURCE
    attribution: str = OPEN_METEO_ATTRIBUTION
    rain_24h_mm: float = Field(..., ge=0.0)
    rain_72h_mm: float = Field(..., ge=0.0)
    rain_7d_mm: float | None = Field(default=None, ge=0.0)
    wet_risk_level: ApiWetRiskLiteral
    db_wet_risk_level: DbWetRiskLiteral
    fetched_at: datetime
    is_stale: bool = False


class SegmentWetAdjustment(BaseModel):
    """Per-segment wet-condition risk assessment and explainable JSONB context."""

    wet_risk_level: ApiWetRiskLiteral
    db_wet_risk_level: DbWetRiskLiteral
    wet_score_adjustment: float = Field(..., ge=0.0, le=MAX_WET_SCORE_ADJUSTMENT)
    warning: str | None = None
    rainfall_context: dict[str, Any]


def spatial_cell_key(
    lon: float,
    lat: float,
    step_deg: float = RAINFALL_GRID_STEP_DEG,
) -> tuple[str, float, float]:
    """Snap WGS84 (lon, lat) to a deterministic 0.15-degree regional rainfall cell."""
    cell_lon = round(round(lon / step_deg) * step_deg, 2)
    cell_lat = round(round(lat / step_deg) * step_deg, 2)
    return (f"r_{cell_lon:.2f}_{cell_lat:.2f}", cell_lon, cell_lat)


def classify_regional_wet_risk(
    rain_72h_mm: float,
) -> tuple[ApiWetRiskLiteral, DbWetRiskLiteral]:
    """Classify 72-hour precipitation into API & DB wet-risk tiers.

    Thresholds:
      - ``dry``: ``< 5 mm``
      - ``damp``: ``5–<15 mm``
      - ``wet``: ``15–<35 mm``
      - ``heavy_rain`` (DB ``saturated``): ``>= 35 mm``
    """
    mm = max(0.0, float(rain_72h_mm))
    if mm < 5.0:
        return ("dry", "dry")
    if mm < 15.0:
        return ("damp", "damp")
    if mm < 35.0:
        return ("wet", "wet")
    return ("heavy_rain", "saturated")


def db_to_api_wet_risk(db_level: str) -> ApiWetRiskLiteral:
    """Map DB ``wet_risk_level`` ('saturated') to public API tier ('heavy_rain')."""
    return _DB_TO_API_WET_RISK.get(db_level, "dry")


def compute_segment_wet_adjustment(
    reading: RainfallReading,
    *,
    surface_class: str = "dirt_unsealed",
    waterway_crossing_count: int = 0,
    has_perennial_crossing: bool = False,
    is_bridged: bool = False,
) -> SegmentWetAdjustment:
    """Modulate regional 72 h rainfall into a bounded, explainable segment wet adjustment.

    Sealed surfaces with no unbridged crossings receive strictly 0.0 score adjustment
    and no mud/clay warning. Bridged crossings add zero waterway wet penalty.
    Unsealed mud/clay/dirt segments with unbridged creeks scale up to +12.0 pts.
    """
    regional_risk = reading.wet_risk_level
    has_unbridged = (not is_bridged) and (waterway_crossing_count > 0 or has_perennial_crossing)

    base_map = _SURFACE_WET_DELTA.get(surface_class, _SURFACE_WET_DELTA["dirt_unsealed"])
    surface_delta = base_map[regional_risk]

    crossing_delta = 0.0
    if has_unbridged:
        if has_perennial_crossing:
            crossing_delta = _CROSSING_WET_DELTA_PERENNIAL[regional_risk]
        else:
            crossing_delta = _CROSSING_WET_DELTA_INTERMITTENT[regional_risk]

    total_delta = round(min(MAX_WET_SCORE_ADJUSTMENT, surface_delta + crossing_delta), 2)

    if surface_class == "sealed" and not has_unbridged:
        seg_api_risk: ApiWetRiskLiteral = "dry"
        seg_db_risk: DbWetRiskLiteral = "dry"
        warning: str | None = None
    else:
        seg_api_risk = regional_risk
        seg_db_risk = _API_TO_DB_WET_RISK[seg_api_risk]
        warning = _build_wet_warning(
            rain_72h_mm=reading.rain_72h_mm,
            rain_24h_mm=reading.rain_24h_mm,
            wet_risk_level=seg_api_risk,
            surface_class=surface_class,
            has_unbridged=has_unbridged,
            has_perennial_crossing=has_perennial_crossing,
            delta=total_delta,
        )

    context: dict[str, Any] = {
        "source": reading.source,
        "attribution": reading.attribution,
        "grid_cell_id": reading.grid_cell_id,
        "fetched_at": reading.fetched_at.isoformat(),
        "rain_24h_mm": round(reading.rain_24h_mm, 2),
        "rain_72h_mm": round(reading.rain_72h_mm, 2),
        "wet_risk_level": seg_api_risk,
        "wet_score_adjustment": total_delta,
        "warning": warning,
        "is_stale": reading.is_stale,
    }
    return SegmentWetAdjustment(
        wet_risk_level=seg_api_risk,
        db_wet_risk_level=seg_db_risk,
        wet_score_adjustment=total_delta,
        warning=warning,
        rainfall_context=context,
    )


def _build_wet_warning(
    *,
    rain_72h_mm: float,
    rain_24h_mm: float,
    wet_risk_level: ApiWetRiskLiteral,
    surface_class: str,
    has_unbridged: bool,
    has_perennial_crossing: bool,
    delta: float,
) -> str | None:
    if delta <= 0.0 or wet_risk_level == "dry":
        return None
    if wet_risk_level == "heavy_rain":
        crossing_part = (
            " and elevated unbridged river/creek water levels"
            if has_unbridged
            else ""
        )
        return (
            f"Heavy rainfall ({rain_72h_mm:.1f} mm / 72 h, {rain_24h_mm:.1f} mm / 24 h): "
            f"expect slippery mud/clay, bog holes{crossing_part} (+{delta:.1f} wet risk pts)."
        )
    if wet_risk_level == "wet":
        crossing_part = (
            " and deeper unbridged creek crossings" if has_unbridged else ""
        )
        return (
            f"Wet conditions ({rain_72h_mm:.1f} mm / 72 h): unsealed surfaces may be slick "
            f"with rutted clay{crossing_part} (+{delta:.1f} wet risk pts)."
        )
    if has_perennial_crossing or surface_class in ("dirt_unsealed", "rough_rock_mud_sand"):
        return (
            f"Damp trail conditions ({rain_72h_mm:.1f} mm / 72 h): shaded sections may be "
            f"slippery (+{delta:.1f} wet risk pts)."
        )
    return None


def _parse_open_meteo_time(raw: str) -> datetime:
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def parse_open_meteo_hourly_payload(
    payload: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[float, float, float | None]:
    """Sum trailing 24-hour and 72-hour precipitation (mm) from an Open-Meteo hourly response."""
    hourly = payload.get("hourly")
    if not isinstance(hourly, Mapping):
        raise ValueError("Open-Meteo payload missing 'hourly' object")
    times = hourly.get("time")
    precip = hourly.get("precipitation")
    if not isinstance(times, list) or not isinstance(precip, list) or len(times) != len(precip):
        raise ValueError("Open-Meteo 'hourly.time' and 'hourly.precipitation' arrays invalid")
    if len(times) > _MAX_OPEN_METEO_HOURS:
        raise ValueError("Open-Meteo hourly response exceeds the sample limit")
    if not times:
        return (0.0, 0.0, None)

    parsed_pairs: list[tuple[datetime, float]] = []
    for raw_t, raw_p in zip(times, precip, strict=True):
        if not isinstance(raw_t, str):
            continue
        ts = _parse_open_meteo_time(raw_t)
        val = 0.0 if raw_p is None else float(raw_p)
        if not math.isfinite(val) or val < 0.0:
            val = 0.0
        parsed_pairs.append((ts, val))

    if not parsed_pairs:
        return (0.0, 0.0, None)

    parsed_pairs.sort(key=lambda item: item[0])
    ref_now = (now or datetime.now(UTC)).astimezone(UTC)
    min_ts = parsed_pairs[0][0]
    max_ts = parsed_pairs[-1][0]

    # When `now` is within payload window (live or aligned mock), filter strictly <= now
    if min_ts <= ref_now <= max_ts + timedelta(hours=24):
        cutoff = ref_now
    else:
        # Fallback for historical fixtures: exclude trailing 24 h forecast if >= 96 h present
        cutoff = max_ts - timedelta(hours=24) if len(parsed_pairs) >= 96 else max_ts

    cut_24 = cutoff - timedelta(hours=24)
    cut_72 = cutoff - timedelta(hours=72)
    cut_7d = cutoff - timedelta(days=7)

    rain_24h = sum(val for ts, val in parsed_pairs if cut_24 < ts <= cutoff)
    rain_72h = sum(val for ts, val in parsed_pairs if cut_72 < ts <= cutoff)
    span_hours = (cutoff - min_ts).total_seconds() / 3600.0
    rain_7d: float | None = (
        round(sum(val for ts, val in parsed_pairs if cut_7d < ts <= cutoff), 2)
        if span_hours >= 144.0
        else None
    )
    return (round(rain_24h, 2), round(rain_72h, 2), rain_7d)


def fetch_open_meteo_rainfall(
    lon: float,
    lat: float,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    timeout_s: float = 10.0,
) -> RainfallReading:
    """Fetch 72-hour precipitation from Open-Meteo for the snapped 0.15-degree cell."""
    cell_id, cell_lon, cell_lat = spatial_cell_key(lon, lat)
    ref_now = (now or datetime.now(UTC)).astimezone(UTC)
    params = {
        "latitude": f"{cell_lat:.4f}",
        "longitude": f"{cell_lon:.4f}",
        "hourly": "precipitation",
        "past_days": "3",
        "forecast_days": "1",
        "timezone": "UTC",
    }

    owns_client = client is None
    http_client = client or httpx.Client(
        timeout=timeout_s,
        headers=IDENTITY_HTTP_HEADERS,
    )
    try:
        with stream_upstream(http_client, OPEN_METEO_FORECAST_URL, params=params) as response:
            response.raise_for_status()
            payload, _ = read_bounded_json(
                response, max_bytes=_MAX_OPEN_METEO_BYTES, max_duration_s=timeout_s * 2
            )
    finally:
        if owns_client:
            http_client.close()

    if not isinstance(payload, Mapping):
        raise ValueError("Invalid Open-Meteo response")
    rain_24h, rain_72h, rain_7d = parse_open_meteo_hourly_payload(payload, now=ref_now)
    api_risk, db_risk = classify_regional_wet_risk(rain_72h)
    return RainfallReading(
        grid_cell_id=cell_id,
        centroid_lon=cell_lon,
        centroid_lat=cell_lat,
        rain_24h_mm=rain_24h,
        rain_72h_mm=rain_72h,
        rain_7d_mm=rain_7d,
        wet_risk_level=api_risk,
        db_wet_risk_level=db_risk,
        fetched_at=ref_now,
        is_stale=False,
    )


def persist_rainfall_snapshot(session: Session, reading: RainfallReading) -> None:
    """Idempotently persist a ``RainfallReading`` into PostGIS ``rainfall_snapshots``."""
    stmt = (
        pg_insert(RainfallSnapshot)
        .values(
            grid_cell_id=reading.grid_cell_id,
            centroid_geom=from_shape(
                Point(reading.centroid_lon, reading.centroid_lat), srid=4326
            ),
            source_provider="open-meteo",
            rain_24h_mm=round(reading.rain_24h_mm, 2),
            rain_72h_mm=round(reading.rain_72h_mm, 2),
            rain_7d_mm=round(reading.rain_7d_mm, 2) if reading.rain_7d_mm is not None else None,
            wet_risk_level=reading.db_wet_risk_level,
            fetched_at=reading.fetched_at,
        )
        .on_conflict_do_update(
            constraint="uq_rainfall_snapshots_cell_fetched",
            set_={
                "rain_24h_mm": round(reading.rain_24h_mm, 2),
                "rain_72h_mm": round(reading.rain_72h_mm, 2),
                "rain_7d_mm": (
                    round(reading.rain_7d_mm, 2) if reading.rain_7d_mm is not None else None
                ),
                "wet_risk_level": reading.db_wet_risk_level,
            },
        )
    )
    session.execute(stmt)


def get_latest_cached_rainfall(
    session: Session,
    grid_cell_id: str,
    *,
    now: datetime | None = None,
    mark_stale: bool = False,
) -> RainfallReading | None:
    """Return the latest persisted ``RainfallSnapshot`` for ``grid_cell_id`` if present."""
    ref_now = (now or datetime.now(UTC)).astimezone(UTC)
    row = session.execute(
        select(
            RainfallSnapshot.grid_cell_id,
            RainfallSnapshot.rain_24h_mm,
            RainfallSnapshot.rain_72h_mm,
            RainfallSnapshot.rain_7d_mm,
            RainfallSnapshot.wet_risk_level,
            RainfallSnapshot.fetched_at,
        )
        .where(RainfallSnapshot.grid_cell_id == grid_cell_id)
        .order_by(desc(RainfallSnapshot.fetched_at))
        .limit(1)
    ).first()
    if row is None:
        return None

    # Parse centroid coordinates back from deterministic cell ID r_<lon>_<lat>
    parts = row.grid_cell_id.split("_")
    try:
        cell_lon, cell_lat = float(parts[1]), float(parts[2])
    except (IndexError, ValueError):
        cell_lon, cell_lat = 0.0, 0.0

    fetched_at = row.fetched_at
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    age_hours = (ref_now - fetched_at).total_seconds() / 3600.0
    is_stale = mark_stale or age_hours > RAINFALL_STALE_AFTER_HOURS
    api_risk, db_risk = classify_regional_wet_risk(float(row.rain_72h_mm))
    return RainfallReading(
        grid_cell_id=row.grid_cell_id,
        centroid_lon=cell_lon,
        centroid_lat=cell_lat,
        rain_24h_mm=round(float(row.rain_24h_mm), 2),
        rain_72h_mm=round(float(row.rain_72h_mm), 2),
        rain_7d_mm=round(float(row.rain_7d_mm), 2) if row.rain_7d_mm is not None else None,
        wet_risk_level=api_risk,
        db_wet_risk_level=db_risk,
        fetched_at=fetched_at,
        is_stale=is_stale,
    )


def resolve_cell_rainfall(
    session: Session,
    lon: float,
    lat: float,
    *,
    now: datetime | None = None,
    rain_override_72h_mm: float | None = None,
    rain_override_24h_mm: float | None = None,
    fetch_live: bool = False,
    client: httpx.Client | None = None,
) -> RainfallReading:
    """Resolve rainfall for a spatial cell via override, live Open-Meteo, or cached fallback."""
    cell_id, cell_lon, cell_lat = spatial_cell_key(lon, lat)
    ref_now = (now or datetime.now(UTC)).astimezone(UTC)

    if rain_override_72h_mm is not None:
        r72 = round(max(0.0, float(rain_override_72h_mm)), 2)
        r24 = (
            round(max(0.0, min(r72, float(rain_override_24h_mm))), 2)
            if rain_override_24h_mm is not None
            else round(r72 * 0.4, 2)
        )
        api_risk, db_risk = classify_regional_wet_risk(r72)
        reading = RainfallReading(
            grid_cell_id=cell_id,
            centroid_lon=cell_lon,
            centroid_lat=cell_lat,
            rain_24h_mm=r24,
            rain_72h_mm=r72,
            rain_7d_mm=None,
            wet_risk_level=api_risk,
            db_wet_risk_level=db_risk,
            fetched_at=ref_now,
            is_stale=False,
        )
        persist_rainfall_snapshot(session, reading)
        return reading

    if fetch_live:
        try:
            reading = fetch_open_meteo_rainfall(cell_lon, cell_lat, client=client, now=ref_now)
            persist_rainfall_snapshot(session, reading)
            return reading
        except Exception as exc:  # noqa: BLE001 — graceful outage fallback per Slice 07
            logger.warning(
                "Open-Meteo rainfall fetch failed for cell %s (%s); falling back to cache",
                cell_id,
                exc,
            )
            cached = get_latest_cached_rainfall(session, cell_id, now=ref_now, mark_stale=True)
            if cached is not None:
                return cached
            return RainfallReading(
                grid_cell_id=cell_id,
                centroid_lon=cell_lon,
                centroid_lat=cell_lat,
                rain_24h_mm=0.0,
                rain_72h_mm=0.0,
                rain_7d_mm=None,
                wet_risk_level="dry",
                db_wet_risk_level="dry",
                fetched_at=ref_now,
                is_stale=True,
            )

    cached = get_latest_cached_rainfall(session, cell_id, now=ref_now, mark_stale=False)
    if cached is not None:
        return cached
    return RainfallReading(
        grid_cell_id=cell_id,
        centroid_lon=cell_lon,
        centroid_lat=cell_lat,
        rain_24h_mm=0.0,
        rain_72h_mm=0.0,
        rain_7d_mm=None,
        wet_risk_level="dry",
        db_wet_risk_level="dry",
        fetched_at=ref_now,
        is_stale=False,
    )


def get_rainfall_summary(
    session: Session,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return latest ``rainfall_snapshots`` summary across all tracked cells with attribution."""
    ref_now = (now or datetime.now(UTC)).astimezone(UTC)
    ranked = (
        select(
            RainfallSnapshot.grid_cell_id.label("grid_cell_id"),
            RainfallSnapshot.rain_24h_mm.label("rain_24h_mm"),
            RainfallSnapshot.rain_72h_mm.label("rain_72h_mm"),
            RainfallSnapshot.rain_7d_mm.label("rain_7d_mm"),
            RainfallSnapshot.wet_risk_level.label("wet_risk_level"),
            RainfallSnapshot.fetched_at.label("fetched_at"),
            func.row_number()
            .over(
                partition_by=RainfallSnapshot.grid_cell_id,
                order_by=desc(RainfallSnapshot.fetched_at),
            )
            .label("rn"),
        )
        .subquery()
    )
    rows = session.execute(
        select(
            ranked.c.grid_cell_id,
            ranked.c.rain_24h_mm,
            ranked.c.rain_72h_mm,
            ranked.c.rain_7d_mm,
            ranked.c.wet_risk_level,
            ranked.c.fetched_at,
        )
        .where(ranked.c.rn == 1)
        .order_by(ranked.c.grid_cell_id)
    ).fetchall()

    snapshots: list[dict[str, Any]] = []
    max_72h = 0.0
    latest_fetched: datetime | None = None
    any_stale = False

    for row in rows:
        fetched = row.fetched_at
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=UTC)
        age_hours = (ref_now - fetched).total_seconds() / 3600.0
        is_stale = age_hours > RAINFALL_STALE_AFTER_HOURS
        any_stale = any_stale or is_stale
        if latest_fetched is None or fetched > latest_fetched:
            latest_fetched = fetched

        r72 = round(float(row.rain_72h_mm), 2)
        max_72h = max(max_72h, r72)
        api_risk, db_risk = classify_regional_wet_risk(r72)

        parts = row.grid_cell_id.split("_")
        try:
            cell_lon, cell_lat = float(parts[1]), float(parts[2])
        except (IndexError, ValueError):
            cell_lon, cell_lat = 0.0, 0.0

        snapshots.append(
            {
                "grid_cell_id": row.grid_cell_id,
                "lon": cell_lon,
                "lat": cell_lat,
                "rain_24h_mm": round(float(row.rain_24h_mm), 2),
                "rain_72h_mm": r72,
                "rain_7d_mm": round(float(row.rain_7d_mm), 2)
                if row.rain_7d_mm is not None
                else None,
                "wet_risk_level": api_risk,
                "db_wet_risk_level": db_risk,
                "fetched_at": fetched.isoformat(),
                "is_stale": is_stale,
            }
        )

    max_api_risk, _ = classify_regional_wet_risk(max_72h)
    return {
        "source": OPEN_METEO_SOURCE,
        "attribution": OPEN_METEO_ATTRIBUTION,
        "snapshot_count": len(snapshots),
        "latest_fetched_at": latest_fetched.isoformat() if latest_fetched is not None else None,
        "is_stale": any_stale if snapshots else False,
        "max_rain_72h_mm": round(max_72h, 2),
        "max_wet_risk_level": max_api_risk,
        "snapshots": snapshots,
    }
