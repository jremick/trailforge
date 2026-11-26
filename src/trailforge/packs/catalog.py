"""Curated 15-region Pan-Australia offline pack catalog (8 Statewide + 7 Iconic 4WD Regions)."""

from __future__ import annotations

import math
from collections.abc import Sequence
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
    attributions_for_states,
)

PackRegionTypeLiteral = Literal["tactical", "state", "custom"]
PackCategoryLiteral = Literal["region", "state"]

# Standard MGRS latitude band letters from 80S to 84N (8-degree bands, excluding I & O)
_MGRS_LAT_BANDS: Final[str] = "CDEFGHJKLMNPQRSTUVWX"

_STATE_ALERT_AGENCY: Final[dict[str, str]] = {
    "VIC": "VicEmergency & DEECA",
    "NSW": "NSW RFS & NSW NPWS",
    "ACT": "ACT ESA & Parks",
    "TAS": "TasALERT & PWS Tasmania",
    "QLD": "QLD DES (QPWS) & QFES",
    "WA": "Emergency WA (DFES & DBCA)",
    "SA": "SA CFS & NPWS SA",
    "NT": "SecureNT & Bushfires NT",
}

# Backward-compatible default attribution list (preserves v1 compliance & Checkpoint C checks)
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


def build_pack_attributions(states: Sequence[str]) -> list[str]:
    """Build dynamic attributions crediting ONLY the region's intersecting AU state agencies."""
    norm_states: list[str] = []
    for raw in states:
        code = str(raw).strip().upper()
        if code and code not in norm_states:
            norm_states.append(code)

    attrs: list[str] = [
        ATTRIBUTION_OSM,
        ATTRIBUTION_GA_DEM,
        ATTRIBUTION_GA_HYDRO,
        ATTRIBUTION_CAPAD,
    ]
    attrs.extend(attributions_for_states(norm_states))
    attrs.append(ATTRIBUTION_OPEN_METEO)

    agencies = [_STATE_ALERT_AGENCY[s] for s in norm_states if s in _STATE_ALERT_AGENCY]
    if agencies:
        attrs.append(
            f"Live incident & park closure alerts: {', '.join(agencies)} "
            "(attributed per alert with official source URL)"
        )
    return attrs


class PackRegionSpec(BaseModel):
    """Immutable specification for a TrailForge regional or statewide offline pack."""

    region_id: str
    name: str
    description: str
    region_type: PackRegionTypeLiteral
    category: PackCategoryLiteral = "region"
    states: tuple[str, ...] = ("VIC", "NSW")
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
    category: PackCategoryLiteral,
    states: tuple[str, ...],
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
        category=category,
        states=states,
        bbox=bbox,
        corridor_bbox=corridor_bbox,
        utm_zones=compute_utm_zones_for_bbox(bbox),
        default_minzoom=default_minzoom,
        default_maxzoom=default_maxzoom,
        terrain_minzoom=terrain_minzoom,
        terrain_maxzoom=terrain_maxzoom,
        estimated_size_hint=estimated_size_hint,
        attributions=build_pack_attributions(states),
    )


_CATALOG_TUPLE: Final[tuple[PackRegionSpec, ...]] = (
    # -------------------------------------------------------------------------
    # Iconic 4WD Regions (7)
    # -------------------------------------------------------------------------
    _make_region(
        region_id="australian-alps",
        name="Australian Alps / High Country (VIC/NSW)",
        description=(
            "Victorian High Country, Davies Plain, Tom Groggin, Alpine National Park, "
            "and Kosciuszko National Park."
        ),
        region_type="tactical",
        category="region",
        states=("VIC", "NSW"),
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
        category="region",
        states=("NSW",),
        bbox=(149.7, -34.2, 150.9, -32.9),
        default_minzoom=8,
        default_maxzoom=15,
        terrain_minzoom=8,
        terrain_maxzoom=13,
        estimated_size_hint="~40 MB",
    ),
    _make_region(
        region_id="tasmania-wilderness",
        name="Tasmania Wilderness & West Coast (TAS)",
        description=(
            "Tarkine, Climies Track, Sandy Cape, Cradle Mountain-Lake St Clair, "
            "and Central Highlands 4WD tracks."
        ),
        region_type="tactical",
        category="region",
        states=("TAS",),
        bbox=(145.0, -42.6, 146.8, -41.1),
        default_minzoom=8,
        default_maxzoom=15,
        terrain_minzoom=8,
        terrain_maxzoom=13,
        estimated_size_hint="~55 MB",
    ),
    _make_region(
        region_id="far-north-qld",
        name="Far North QLD / Cape York & Wet Tropics (QLD)",
        description=(
            "Old Telegraph Track, CREB Track, Bloomfield Track, Atherton Tablelands, "
            "and Cape York Peninsula."
        ),
        region_type="tactical",
        category="region",
        states=("QLD",),
        bbox=(142.0, -17.6, 146.2, -10.6),
        default_minzoom=7,
        default_maxzoom=14,
        terrain_minzoom=7,
        terrain_maxzoom=12,
        estimated_size_hint="~120 MB",
    ),
    _make_region(
        region_id="flinders-ranges",
        name="Flinders Ranges & Gammon Ranges (SA)",
        description=(
            "Ikara-Flinders Ranges, Brachina Gorge, Moralana Scenic Drive, Skytrek, "
            "and Vulkathunha-Gammon Ranges."
        ),
        region_type="tactical",
        category="region",
        states=("SA",),
        bbox=(138.0, -32.6, 139.6, -30.2),
        default_minzoom=8,
        default_maxzoom=15,
        terrain_minzoom=8,
        terrain_maxzoom=13,
        estimated_size_hint="~60 MB",
    ),
    _make_region(
        region_id="red-centre",
        name="Red Centre & Outback Border (NT/SA)",
        description=(
            "West MacDonnell Ranges, Finke Gorge & Palm Valley, Mereenie Loop, "
            "Uluru-Kata Tjuta, and northern SA Outback tracks."
        ),
        region_type="tactical",
        category="region",
        states=("NT", "SA"),
        bbox=(130.4, -26.5, 134.5, -23.4),
        default_minzoom=7,
        default_maxzoom=14,
        terrain_minzoom=7,
        terrain_maxzoom=12,
        estimated_size_hint="~90 MB",
    ),
    _make_region(
        region_id="perth-hills",
        name="Perth Hills & WA Southwest (WA)",
        description=(
            "Mundaring Powerlines Track, Dwellingup, Collie, Harvey, and "
            "Margaret River / Southwest forests."
        ),
        region_type="tactical",
        category="region",
        states=("WA",),
        bbox=(115.0, -34.5, 116.8, -31.5),
        default_minzoom=8,
        default_maxzoom=15,
        terrain_minzoom=8,
        terrain_maxzoom=13,
        estimated_size_hint="~70 MB",
    ),
    # -------------------------------------------------------------------------
    # Statewide Packs (8 — All Australian States & Territories)
    # -------------------------------------------------------------------------
    _make_region(
        region_id="vic",
        name="Victoria (Statewide)",
        description=(
            "Full Victoria from Mallee, Grampians & Otways to the Victorian Alps "
            "and East Gippsland."
        ),
        region_type="state",
        category="state",
        states=("VIC",),
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
        category="state",
        states=("NSW", "ACT"),
        bbox=(140.9, -37.6, 153.7, -28.1),
        default_minzoom=7,
        default_maxzoom=14,
        terrain_minzoom=7,
        terrain_maxzoom=12,
        estimated_size_hint="~850 MB",
    ),
    _make_region(
        region_id="act",
        name="Australian Capital Territory (Territory-wide)",
        description=(
            "Canberra, Namadgi National Park, Tidbinbilla, and Brindabella Range fire trails."
        ),
        region_type="state",
        category="state",
        states=("ACT",),
        bbox=(148.74, -35.94, 149.42, -35.10),
        default_minzoom=8,
        default_maxzoom=15,
        terrain_minzoom=8,
        terrain_maxzoom=13,
        estimated_size_hint="~45 MB",
    ),
    _make_region(
        region_id="tas",
        name="Tasmania (Statewide)",
        description=(
            "Full Tasmania including West Coast, Tarkine, Central Highlands, "
            "Northeast forests, and Bass Strait islands."
        ),
        region_type="state",
        category="state",
        states=("TAS",),
        bbox=(143.80, -43.75, 148.55, -39.55),
        default_minzoom=7,
        default_maxzoom=14,
        terrain_minzoom=7,
        terrain_maxzoom=12,
        estimated_size_hint="~220 MB",
    ),
    _make_region(
        region_id="qld",
        name="Queensland (Statewide)",
        description=(
            "Full Queensland from Southeast QLD, K'gari & Glass House Mountains "
            "to Cape York & Channel Country."
        ),
        region_type="state",
        category="state",
        states=("QLD",),
        bbox=(137.95, -29.20, 153.60, -10.00),
        default_minzoom=6,
        default_maxzoom=13,
        terrain_minzoom=6,
        terrain_maxzoom=11,
        estimated_size_hint="~1.6 GB",
    ),
    _make_region(
        region_id="wa",
        name="Western Australia (Statewide)",
        description=(
            "Full Western Australia including Southwest, Pilbara, Kimberley, "
            "Gibb River Road, and Canning Stock Route."
        ),
        region_type="state",
        category="state",
        states=("WA",),
        bbox=(112.85, -35.20, 129.05, -13.65),
        default_minzoom=6,
        default_maxzoom=13,
        terrain_minzoom=6,
        terrain_maxzoom=11,
        estimated_size_hint="~2.1 GB",
    ),
    _make_region(
        region_id="sa",
        name="South Australia (Statewide)",
        description=(
            "Full South Australia including Flinders Ranges, Gawler Ranges, "
            "Eyre Peninsula, Simpson Desert & Border Track."
        ),
        region_type="state",
        category="state",
        states=("SA",),
        bbox=(128.98, -38.10, 141.05, -25.95),
        default_minzoom=6,
        default_maxzoom=13,
        terrain_minzoom=6,
        terrain_maxzoom=11,
        estimated_size_hint="~850 MB",
    ),
    _make_region(
        region_id="nt",
        name="Northern Territory (Territory-wide)",
        description=(
            "Full Northern Territory including Top End, Kakadu, Litchfield, "
            "Arnhem Land, Barkly Tableland, and Red Centre."
        ),
        region_type="state",
        category="state",
        states=("NT",),
        bbox=(128.98, -26.05, 138.05, -10.90),
        default_minzoom=6,
        default_maxzoom=13,
        terrain_minzoom=6,
        terrain_maxzoom=11,
        estimated_size_hint="~1.0 GB",
    ),
)

PACK_REGIONS: Final[dict[str, PackRegionSpec]] = {r.region_id: r for r in _CATALOG_TUPLE}
PACK_REGION_IDS: Final[tuple[str, ...]] = tuple(r.region_id for r in _CATALOG_TUPLE)


def list_pack_regions(category: PackCategoryLiteral | None = None) -> list[PackRegionSpec]:
    """Return the 15-region Pan-Australia offline pack catalog, optionally filtered by category."""
    if category is None:
        return list(_CATALOG_TUPLE)
    return [r for r in _CATALOG_TUPLE if r.category == category]


def get_pack_region(region_id: str) -> PackRegionSpec:
    """Resolve a catalog region ID, raising ValueError if unknown."""
    key = region_id.strip().lower()
    if key not in PACK_REGIONS:
        allowed = ", ".join(PACK_REGION_IDS)
        raise ValueError(f"Unknown pack region {region_id!r}; expected one of: {allowed}")
    return PACK_REGIONS[key]
