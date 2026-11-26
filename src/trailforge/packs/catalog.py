"""Curated VIC & NSW regional offline pack catalog adapted from GridForge patterns."""

from __future__ import annotations

import math
from typing import Final, Literal

from pydantic import BaseModel, Field

from trailforge.ingestion.connectors.base import (
    ATTRIBUTION_CAPAD,
    ATTRIBUTION_GA_DEM,
    ATTRIBUTION_GA_HYDRO,
    ATTRIBUTION_NSW_SPATIAL,
    ATTRIBUTION_OPEN_METEO,
    ATTRIBUTION_OSM,
    ATTRIBUTION_STATE_FEEDS,
    ATTRIBUTION_VIC_DEECA,
)

PackRegionTypeLiteral = Literal["tactical", "state", "custom"]

# Standard MGRS latitude band letters from 80S to 84N (8-degree bands, excluding I & O)
_MGRS_LAT_BANDS: Final[str] = "CDEFGHJKLMNPQRSTUVWX"

PACK_ATTRIBUTIONS: Final[tuple[str, ...]] = (
    ATTRIBUTION_OSM,
    ATTRIBUTION_GA_DEM,
    ATTRIBUTION_GA_HYDRO,
    ATTRIBUTION_CAPAD,
    ATTRIBUTION_NSW_SPATIAL,
    ATTRIBUTION_VIC_DEECA,
    ATTRIBUTION_OPEN_METEO,
    ATTRIBUTION_STATE_FEEDS,
)


class PackRegionSpec(BaseModel):
    """Immutable specification for a TrailForge regional offline pack."""

    region_id: str
    name: str
    description: str
    region_type: PackRegionTypeLiteral
    bbox: tuple[float, float, float, float] = Field(
        ..., description="WGS84 bounding box (min_lon, min_lat, max_lon, max_lat)."
    )
    corridor_bbox: tuple[float, float, float, float] | None = Field(
        default=None,
        description="Optional high-detail focus corridor bbox inside the region.",
    )
    utm_zones: list[str]
    default_minzoom: int = 8
    default_maxzoom: int = 15
    terrain_minzoom: int = 8
    terrain_maxzoom: int = 13
    estimated_size_hint: str = ""
    attributions: list[str] = Field(default_factory=lambda: list(PACK_ATTRIBUTIONS))


def _resolve_mgrs_gzd(lat: float, lon: float) -> str:
    """Compute MGRS Grid Zone Designation (e.g. '55H') for a WGS84 point in Australia."""
    clamped_lat = max(-80.0, min(83.9999, lat))
    clamped_lon = max(-180.0, min(179.9999, lon))
    zone = int(math.floor((clamped_lon + 180.0) / 6.0)) + 1
    zone = max(1, min(60, zone))
    band_idx = int(math.floor((clamped_lat + 80.0) / 8.0))
    band_idx = max(0, min(len(_MGRS_LAT_BANDS) - 1, band_idx))
    return f"{zone}{_MGRS_LAT_BANDS[band_idx]}"


def compute_utm_zones_for_bbox(bbox: tuple[float, float, float, float]) -> list[str]:
    """Return sorted distinct MGRS Grid Zone Designations intersecting a WGS84 bbox."""
    min_x, min_y, max_x, max_y = bbox
    width = max(0.0, max_x - min_x)
    height = max(0.0, max_y - min_y)
    lon_steps = max(2, int(math.ceil(width / 1.0)) + 1)
    lat_steps = max(2, int(math.ceil(height / 2.0)) + 1)

    zones: set[str] = set()
    for ix in range(lon_steps):
        lon = min_x + (width * ix / (lon_steps - 1))
        for iy in range(lat_steps):
            lat = min_y + (height * iy / (lat_steps - 1))
            zones.add(_resolve_mgrs_gzd(lat, lon))

    return sorted(zones, key=lambda gzd: (int(gzd[:-1]), gzd[-1]))


def _make_region(
    *,
    region_id: str,
    name: str,
    description: str,
    region_type: PackRegionTypeLiteral,
    bbox: tuple[float, float, float, float],
    corridor_bbox: tuple[float, float, float, float] | None = None,
    default_minzoom: int = 8,
    default_maxzoom: int = 15,
    terrain_minzoom: int = 8,
    terrain_maxzoom: int = 13,
    estimated_size_hint: str = "",
) -> PackRegionSpec:
    return PackRegionSpec(
        region_id=region_id,
        name=name,
        description=description,
        region_type=region_type,
        bbox=bbox,
        corridor_bbox=corridor_bbox,
        utm_zones=compute_utm_zones_for_bbox(bbox),
        default_minzoom=default_minzoom,
        default_maxzoom=default_maxzoom,
        terrain_minzoom=terrain_minzoom,
        terrain_maxzoom=terrain_maxzoom,
        estimated_size_hint=estimated_size_hint,
        attributions=list(PACK_ATTRIBUTIONS),
    )


_CATALOG_TUPLE: Final[tuple[PackRegionSpec, ...]] = (
    _make_region(
        region_id="australian-alps",
        name="Australian Alps / High Country (VIC/NSW)",
        description=(
            "Victorian High Country, Davies Plain, Tom Groggin, Alpine National Park, "
            "and Kosciuszko National Park."
        ),
        region_type="tactical",
        bbox=(146.5, -37.6, 149.2, -35.4),
        corridor_bbox=(148.05, -36.68, 148.20, -36.55),
        default_minzoom=8,
        default_maxzoom=15,
        terrain_minzoom=8,
        terrain_maxzoom=13,
        estimated_size_hint="~65 MB",
    ),
    _make_region(
        region_id="blue-mountains",
        name="Blue Mountains & Central Tablelands (NSW)",
        description=(
            "Newnes Plateau, Wollemi, Kanangra-Boyd, Abercrombie, and Blue Mountains "
            "4WD & fire trail network."
        ),
        region_type="tactical",
        bbox=(149.7, -34.2, 150.9, -32.9),
        default_minzoom=8,
        default_maxzoom=15,
        terrain_minzoom=8,
        terrain_maxzoom=13,
        estimated_size_hint="~40 MB",
    ),
    _make_region(
        region_id="vic",
        name="Victoria (Statewide)",
        description=(
            "Full Victoria from Mallee, Grampians & Otways to the Victorian Alps "
            "and East Gippsland."
        ),
        region_type="state",
        bbox=(140.9, -39.2, 150.1, -33.9),
        default_minzoom=7,
        default_maxzoom=14,
        terrain_minzoom=7,
        terrain_maxzoom=12,
        estimated_size_hint="~450 MB",
    ),
    _make_region(
        region_id="nsw",
        name="New South Wales & ACT (Statewide)",
        description=(
            "Full New South Wales & ACT including Snowy Mountains, Blue Mountains, "
            "Barrington Tops & Outback."
        ),
        region_type="state",
        bbox=(140.9, -37.6, 153.7, -28.1),
        default_minzoom=7,
        default_maxzoom=14,
        terrain_minzoom=7,
        terrain_maxzoom=12,
        estimated_size_hint="~850 MB",
    ),
)

PACK_REGIONS: Final[dict[str, PackRegionSpec]] = {r.region_id: r for r in _CATALOG_TUPLE}
PACK_REGION_IDS: Final[tuple[str, ...]] = tuple(r.region_id for r in _CATALOG_TUPLE)


def list_pack_regions() -> list[PackRegionSpec]:
    """Return the locked 4-region v1 offline pack catalog in display order."""
    return list(_CATALOG_TUPLE)


def get_pack_region(region_id: str) -> PackRegionSpec:
    """Resolve a locked catalog region ID, raising ValueError if unknown."""
    key = region_id.strip().lower()
    if key not in PACK_REGIONS:
        allowed = ", ".join(PACK_REGION_IDS)
        raise ValueError(f"Unknown pack region {region_id!r}; expected one of: {allowed}")
    return PACK_REGIONS[key]
