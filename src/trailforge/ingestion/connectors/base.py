"""Shared Pydantic v2 models, normalization helpers, and cached ArcGIS/WFS clients.

Adapted from verified Australian open-data connector patterns in GridForge (read-only ref).
All network connectors accept an injectable ``httpx.Client`` and support offline local files.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import re
import shutil
import tempfile
import time
import zlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Locked Constants, Coverage Bounds & Required Attribution Strings
# -----------------------------------------------------------------------------

USER_AGENT: Final[str] = "trailforge/0.1 (+https://github.com/trailforge/trailforge)"
IDENTITY_HTTP_HEADERS: Final[dict[str, str]] = {
    "User-Agent": USER_AGENT,
    "Accept-Encoding": "identity",
}

_MAX_VECTOR_PAGE_BYTES: Final[int] = 12 * 1024 * 1024
_MAX_VECTOR_QUERY_BYTES: Final[int] = 32 * 1024 * 1024
_MAX_VECTOR_GEOMETRY_NODES: Final[int] = 65_536
_MAX_VECTOR_QUERY_GEOMETRY_NODES: Final[int] = 300_000
_MIN_DOWNLOAD_FREE_BYTES: Final[int] = 256 * 1024 * 1024
_MAX_DOWNLOAD_SECONDS: Final[float] = 20 * 60

ATTRIBUTION_OSM: Final[str] = (
    "Map data © OpenStreetMap contributors (ODbL) — openstreetmap.org/copyright"
)
ATTRIBUTION_GA_DEM: Final[str] = (
    "Elevation data: © Commonwealth of Australia "
    "(Geoscience Australia / Digital Earth Australia) (CC BY 4.0)"
)
ATTRIBUTION_GA_HYDRO: Final[str] = (
    "Surface Hydrology: © Commonwealth of Australia (Geoscience Australia) "
    "& AusHydro Contributors (CC BY 4.0)"
)
ATTRIBUTION_CAPAD: Final[str] = (
    "Protected Areas (CAPAD 2025): © Commonwealth of Australia (DCCEEW) (CC BY 4.0)"
)
ATTRIBUTION_NSW_SPATIAL: Final[str] = (
    "NSW Trails, Facilities & Trig Stations: "
    "© State of New South Wales (Spatial Services, DCS) (CC BY 4.0)"
)
ATTRIBUTION_VIC_DEECA: Final[str] = (
    "Victoria Trails, Huts, Sites & Closures: "
    "© State of Victoria (DEECA / DTP Vicmap) (CC BY 4.0)"
)
ATTRIBUTION_ACT_ACTMAPI: Final[str] = (
    "ACT Trails, Huts & Survey Marks: "
    "© Australian Capital Territory (ACTmapi / EPSDD) (CC BY 4.0)"
)
ATTRIBUTION_TAS_LIST: Final[str] = (
    "Tasmania Trails, Huts & Survey Control: "
    "© State of Tasmania (the LIST, NRE Tas) (CC BY 3.0 AU)"
)
ATTRIBUTION_QLD_QSPATIAL: Final[str] = (
    "Queensland Trails, Parks & Survey Control: "
    "© State of Queensland (DESI / Dept. of Natural Resources & Mines) (CC BY 4.0)"
)
ATTRIBUTION_WA_DBCA_SLIP: Final[str] = (
    "Western Australia Trails, Parks & Geodetic Marks: "
    "© State of Western Australia (DBCA & Landgate SLIP) (CC BY 4.0)"
)
ATTRIBUTION_SA_DEW_DHUD: Final[str] = (
    "South Australia Trails, Parks & Survey Marks: "
    "© Government of South Australia (DEW / DHUD) (CC BY 4.0) "
    "& Geoscience Australia (CC BY 4.0)"
)
ATTRIBUTION_NT_NTLIS: Final[str] = (
    "Northern Territory Tracks, Place Names & Survey Marks: "
    "© Northern Territory of Australia (NTLIS / DEPWS) (CC BY 4.0) "
    "& Geoscience Australia (CC BY 4.0)"
)
ATTRIBUTION_OPEN_METEO: Final[str] = "Weather & Rainfall data by Open-Meteo.com (CC BY 4.0)"
ATTRIBUTION_STATE_FEEDS: Final[str] = (
    "Live incident & park closure alerts: VicEmergency, NSW RFS, and NSW NPWS "
    "(attributed per alert with official source URL)"
)

AU_STATE_CODES: Final[tuple[str, ...]] = ("VIC", "NSW", "ACT", "TAS", "QLD", "WA", "SA", "NT")
AU_EXTENDED_STATE_CODES: Final[tuple[str, ...]] = ("ACT", "TAS", "QLD", "WA", "SA", "NT")

STATE_ATTRIBUTIONS: Final[dict[str, str]] = {
    "VIC": ATTRIBUTION_VIC_DEECA,
    "NSW": ATTRIBUTION_NSW_SPATIAL,
    "ACT": ATTRIBUTION_ACT_ACTMAPI,
    "TAS": ATTRIBUTION_TAS_LIST,
    "QLD": ATTRIBUTION_QLD_QSPATIAL,
    "WA": ATTRIBUTION_WA_DBCA_SLIP,
    "SA": ATTRIBUTION_SA_DEW_DHUD,
    "NT": ATTRIBUTION_NT_NTLIS,
}


def attributions_for_states(states: Sequence[str]) -> list[str]:
    """Return deduplicated state agency attributions strictly for ``states``."""
    out: list[str] = []
    for raw in states:
        code = str(raw).strip().upper()
        attr = STATE_ATTRIBUTIONS.get(code)
        if attr and attr not in out:
            out.append(attr)
    return out

# Memory-bounded GDAL /vsicurl/ settings (< 300 MB peak RAM streaming guardrail)
GDAL_STREAMING_ENV: Final[dict[str, str]] = {
    "GDAL_CACHEMAX": "128",
    "OMP_THREAD_LIMIT": "1",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.vrt",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "67108864",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "GDAL_HTTP_USERAGENT": USER_AGENT,
}

_PLACEHOLDER_NAMES: Final[frozenset[str]] = frozenset(
    {"unnamed", "unknown", "none", "null", "n/a", ""}
)
_NUMERIC_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"[-+]?\d+(?:\.\d+)?")

_SEALED_SURFACES: Final[frozenset[str]] = frozenset(
    {
        "asphalt",
        "paved",
        "concrete",
        "concrete:plates",
        "concrete:lanes",
        "bitumen",
        "tarmac",
        "chipseal",
        "sealed",
    }
)
_GRAVEL_SURFACES: Final[frozenset[str]] = frozenset(
    {
        "compacted",
        "fine_gravel",
        "gravel",
        "pebblestone",
        "crushed_limestone",
        "metal",
        "gravel_compacted",
    }
)
_DIRT_SURFACES: Final[frozenset[str]] = frozenset(
    {
        "unpaved",
        "unsealed",
        "unformed",
        "dirt",
        "earth",
        "ground",
        "grass",
        "natural",
        "woodchips",
        "boardwalk",
        "dirt_unsealed",
    }
)
_ROUGH_SURFACES: Final[frozenset[str]] = frozenset(
    {
        "rock",
        "rocks",
        "stone",
        "cobblestone",
        "sand",
        "mud",
        "clay",
        "scree",
        "boulders",
        "shingle",
        "rough_rock_mud_sand",
    }
)

TrackSourceLiteral = Literal["osm", "vic", "nsw", "act", "tas", "qld", "wa", "sa", "nt"]
TrailClassLiteral = Literal[
    "4wd_track", "fire_trail", "management_trail", "unsealed_road", "walking_track"
]
SurfaceClassLiteral = Literal[
    "sealed", "gravel_compacted", "dirt_unsealed", "rough_rock_mud_sand", "unknown"
]
TracktypeLiteral = Literal["grade1", "grade2", "grade3", "grade4", "grade5"]
AccessStatusLiteral = Literal["public", "seasonal", "permit", "private", "restricted", "closed"]
StateLiteral = Literal["VIC", "NSW", "ACT", "TAS", "QLD", "WA", "SA", "NT"]
SourceLicenseLiteral = Literal["CC-BY-4.0", "CC-BY-3.0-AU", "ODbL"]
PoiTypeLiteral = Literal[
    "campground", "hut", "trig", "lookout", "picnic", "water_point", "gate", "trailhead", "fuel"
]
PoiProviderLiteral = Literal[
    "vic_foi",
    "vic_recweb_hut",
    "vic_recweb_site",
    "vic_geomark",
    "nsw_poi",
    "nsw_trig",
    "nsw_hut",
    "act_heritage_hut",
    "act_feature_name",
    "act_bbq",
    "act_shelter",
    "act_trig",
    "tas_hut",
    "tas_nomenclature",
    "tas_infra",
    "tas_picnic",
    "tas_trig",
    "qld_tourist",
    "qld_survey",
    "wa_ratis_site",
    "wa_ratis_facility",
    "wa_fire_tower",
    "wa_geodetic",
    "sa_parks",
    "sa_survey_mark",
    "nt_placename",
    "nt_survey_mark",
    "ga_hydro_pt",
    "osm",
]
DemTierLiteral = Literal["5m-lidar", "srtm-1s", "synthetic"]


class BBox(BaseModel):
    """WGS84 EPSG:4326 bounding box."""

    model_config = ConfigDict(frozen=True)

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.min_x, self.min_y, self.max_x, self.max_y)

    def intersects(self, other: BBox) -> bool:
        return (
            self.max_x >= other.min_x
            and self.min_x <= other.max_x
            and self.max_y >= other.min_y
            and self.min_y <= other.max_y
        )

    def round_enclosing(self, step_deg: float = 0.001) -> BBox:
        """Round outward to ``step_deg`` so cached clips always cover the query bbox."""
        inv = 1.0 / step_deg
        return BBox(
            min_x=math.floor(self.min_x * inv) / inv,
            min_y=math.floor(self.min_y * inv) / inv,
            max_x=math.ceil(self.max_x * inv) / inv,
            max_y=math.ceil(self.max_y * inv) / inv,
        )

    def cache_hash(self) -> str:
        payload = f"{self.min_x:.3f},{self.min_y:.3f},{self.max_x:.3f},{self.max_y:.3f}"
        return hashlib.sha256(payload.encode("ascii")).hexdigest()[:24]


NSW_COVERAGE_BBOX: Final[BBox] = BBox(min_x=140.5, min_y=-37.8, max_x=154.0, max_y=-28.0)
VIC_COVERAGE_BBOX: Final[BBox] = BBox(min_x=140.5, min_y=-39.4, max_x=150.2, max_y=-33.8)
ACT_COVERAGE_BBOX: Final[BBox] = BBox(min_x=148.70, min_y=-35.95, max_x=149.45, max_y=-35.10)
ACT_STRICT_BBOX: Final[BBox] = BBox(min_x=148.72, min_y=-35.94, max_x=149.42, max_y=-35.10)
TAS_COVERAGE_BBOX: Final[BBox] = BBox(min_x=143.5, min_y=-43.8, max_x=148.6, max_y=-39.5)
QLD_COVERAGE_BBOX: Final[BBox] = BBox(min_x=137.8, min_y=-29.3, max_x=154.0, max_y=-10.5)
WA_COVERAGE_BBOX: Final[BBox] = BBox(min_x=112.9, min_y=-35.2, max_x=129.0, max_y=-13.6)
SA_COVERAGE_BBOX: Final[BBox] = BBox(min_x=129.0, min_y=-38.1, max_x=141.0, max_y=-26.0)
NT_COVERAGE_BBOX: Final[BBox] = BBox(min_x=129.0, min_y=-26.0, max_x=138.0, max_y=-10.9)
AU_COVERAGE_BBOX: Final[BBox] = BBox(min_x=112.5, min_y=-44.5, max_x=154.5, max_y=-9.5)
CORRIDOR_ALPS_BBOX: Final[BBox] = BBox(min_x=148.05, min_y=-36.68, max_x=148.20, max_y=-36.55)


def is_in_nsw_coverage(bbox: BBox) -> bool:
    return bbox.intersects(NSW_COVERAGE_BBOX)


def is_in_vic_coverage(bbox: BBox) -> bool:
    if not bbox.intersects(VIC_COVERAGE_BBOX):
        return False
    # Exclude NSW/ACT northeast of the Murray River / Forest Hill border (zero VIC land)
    return not (bbox.min_x > 148.0 and bbox.min_y > -35.95)


def is_in_act_coverage(bbox: BBox) -> bool:
    return bbox.intersects(ACT_COVERAGE_BBOX)


def is_in_tas_coverage(bbox: BBox) -> bool:
    return bbox.intersects(TAS_COVERAGE_BBOX)


def is_in_qld_coverage(bbox: BBox) -> bool:
    return bbox.intersects(QLD_COVERAGE_BBOX)


def is_in_wa_coverage(bbox: BBox) -> bool:
    return bbox.intersects(WA_COVERAGE_BBOX)


def is_in_sa_coverage(bbox: BBox) -> bool:
    return bbox.intersects(SA_COVERAGE_BBOX)


def is_in_nt_coverage(bbox: BBox) -> bool:
    return bbox.intersects(NT_COVERAGE_BBOX)


def is_in_australia_coverage(bbox: BBox) -> bool:
    return bbox.intersects(AU_COVERAGE_BBOX)


def iter_spatial_tiles(bbox: BBox, max_deg: float = 1.0) -> list[BBox]:
    """Partition a WGS84 BBox into bounded spatial tiles of at most ``max_deg`` x ``max_deg``."""
    step = max(0.1, float(max_deg))
    nx = max(1, int(math.ceil(bbox.width / step)))
    ny = max(1, int(math.ceil(bbox.height / step)))
    tiles: list[BBox] = []
    for ix in range(nx):
        w = bbox.min_x + (bbox.width * ix / nx)
        e = bbox.min_x + (bbox.width * (ix + 1) / nx)
        for iy in range(ny):
            s = bbox.min_y + (bbox.height * iy / ny)
            n = bbox.min_y + (bbox.height * (iy + 1) / ny)
            tiles.append(BBox(min_x=w, min_y=s, max_x=e, max_y=n))
    return tiles


# -----------------------------------------------------------------------------
# Normalized Ingestion Models (Pydantic v2)
# -----------------------------------------------------------------------------


def _coerce_geojson_geometry(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseGeometry):
        return dict(mapping(value))
    if isinstance(value, Mapping):
        if "type" not in value or "coordinates" not in value:
            raise ValueError("GeoJSON geometry dict must contain 'type' and 'coordinates'")
        return dict(value)
    raise TypeError(f"Expected GeoJSON dict or Shapely geometry, got {type(value).__name__}")


class RawTrackFeature(BaseModel):
    """Normalized upstream track/road feature from OSM, VIC DataVic, or NSW Spatial."""

    source: TrackSourceLiteral
    source_layer: str
    source_id: str
    name: str | None = None
    trail_class: TrailClassLiteral = "4wd_track"
    is_vehicular: bool = True
    surface_class: SurfaceClassLiteral = "unknown"
    surface_raw: str | None = None
    tracktype: TracktypeLiteral | None = None
    smoothness: str | None = None
    is_4wd_only: bool = False
    is_4wd_recommended: bool = False
    access_status: AccessStatusLiteral = "public"
    has_gate_barrier: bool = False
    is_bridged: bool = False
    is_ford: bool = False
    geometry: dict[str, Any]
    raw_props: dict[str, Any] = Field(default_factory=dict)

    # Optional upstream metadata retained for Tier A staging tables
    osm_way_id: int | None = None
    osm_version: int | None = None
    highway: str | None = None
    four_wd_only_raw: str | None = None
    access_raw: str | None = None
    motor_vehicle: str | None = None
    bridge_raw: str | None = None
    ford_raw: str | None = None
    class_code: int | None = None
    class_subtype: int | None = None
    operational_status: str | None = None

    @field_validator("geometry", mode="before")
    @classmethod
    def _validate_geometry(cls, v: Any) -> dict[str, Any]:
        return _coerce_geojson_geometry(v)

    def to_shapely(self) -> BaseGeometry:
        return shape(self.geometry)


class RawWaterwayFeature(BaseModel):
    """Normalized upstream waterway feature (GA Surface Hydrology CC BY 4.0 or OSM ODbL)."""

    source_provider: Literal["ga_hydro", "osm"]
    source_license: Literal["CC-BY-4.0", "ODbL"]
    source_feature_id: str
    name: str | None = None
    feature_type: str = "Watercourse"
    perenniality: str = "Unknown"
    hierarchy: str | None = None
    geometry: dict[str, Any]

    @field_validator("geometry", mode="before")
    @classmethod
    def _validate_geometry(cls, v: Any) -> dict[str, Any]:
        return _coerce_geojson_geometry(v)

    def to_shapely(self) -> BaseGeometry:
        return shape(self.geometry)


class RawPoiFeature(BaseModel):
    """Normalized upstream backcountry POI feature (huts, camps, trigs, lookouts, gates)."""

    source_provider: PoiProviderLiteral
    source_license: SourceLicenseLiteral
    source_feature_id: str
    name: str | None = None
    poi_type: PoiTypeLiteral
    state: StateLiteral | None = None
    elev_m: float | None = None
    access_note: str | None = None
    raw_props: dict[str, Any] = Field(default_factory=dict)
    geometry: dict[str, Any]

    @field_validator("geometry", mode="before")
    @classmethod
    def _validate_geometry(cls, v: Any) -> dict[str, Any]:
        return _coerce_geojson_geometry(v)

    def to_shapely(self) -> BaseGeometry:
        return shape(self.geometry)


class RawCapadFeature(BaseModel):
    """Normalized DCCEEW CAPAD 2025 protected area feature (MultiPolygon, EPSG:4326)."""

    source_feature_id: str
    name: str
    park_type: str
    type_abbr: str | None = None
    iucn: str | None = None
    state: StateLiteral
    geometry: dict[str, Any]

    @field_validator("geometry", mode="before")
    @classmethod
    def _validate_geometry(cls, v: Any) -> dict[str, Any]:
        gj = _coerce_geojson_geometry(v)
        if gj.get("type") == "Polygon":
            poly = shape(gj)
            if isinstance(poly, Polygon):
                return dict(mapping(MultiPolygon([poly])))
        return gj

    def to_shapely(self) -> BaseGeometry:
        geom = shape(self.geometry)
        if isinstance(geom, Polygon):
            return MultiPolygon([geom])
        return geom


class CorridorDataset(BaseModel):
    """Complete multi-source dataset for ingesting a corridor or regional snapshot."""

    corridor_name: str = ""
    bbox: BBox
    osm_tracks: list[RawTrackFeature] = Field(default_factory=list)
    vic_tracks: list[RawTrackFeature] = Field(default_factory=list)
    nsw_tracks: list[RawTrackFeature] = Field(default_factory=list)
    au_state_tracks: list[RawTrackFeature] = Field(default_factory=list)
    waterways: list[RawWaterwayFeature] = Field(default_factory=list)
    pois: list[RawPoiFeature] = Field(default_factory=list)
    capad_parks: list[RawCapadFeature] = Field(default_factory=list)
    dem_path: Path | None = None
    dem_tier: DemTierLiteral = "synthetic"
    attributions: list[str] = Field(default_factory=list)

    @property
    def all_tracks(self) -> list[RawTrackFeature]:
        return [
            *self.osm_tracks,
            *self.vic_tracks,
            *self.nsw_tracks,
            *self.au_state_tracks,
        ]


# -----------------------------------------------------------------------------
# Property & Attribute Normalization Helpers
# -----------------------------------------------------------------------------


def clean_feature_name(raw: Any) -> str | None:
    """Trim whitespace and convert placeholder strings ('unnamed', 'unknown', etc.) to None."""
    if raw is None:
        return None
    text = str(raw).strip()
    return None if text.lower() in _PLACEHOLDER_NAMES else text


def deterministic_feature_id(
    prefix: str, geom: Mapping[str, Any], props: Mapping[str, Any]
) -> str:
    """Compute a stable SHA-256 fallback feature ID when upstream omits OBJECTID/PFI."""
    payload = json.dumps(
        {"g": geom, "p": props},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def prop_ci(props: Mapping[str, Any], *keys: str) -> Any:
    """Case-insensitive lookup returning the first non-empty value matching ``keys``."""
    lower_map = {str(k).lower(): v for k, v in props.items()}
    for key in keys:
        val = lower_map.get(key.lower())
        if val is not None and str(val).strip() != "":
            return val
    return None


def to_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def parse_elevation_m(value: Any) -> float | None:
    """Parse elevation meters from numeric or string values like ``'2229.470'`` or ``'986.7 m'``."""
    direct = to_float_or_none(value)
    if direct is not None:
        return direct
    if isinstance(value, str):
        match = _NUMERIC_PREFIX_RE.search(value)
        if match:
            return to_float_or_none(match.group(0))
    return None


def normalize_surface_class(
    raw_surface: Any,
    *,
    tracktype: str | None = None,
    trail_class: str | None = None,
) -> SurfaceClassLiteral:
    """Normalize upstream OSM/VIC/NSW surface strings to canonical DB ``surface_class``."""
    if raw_surface is not None:
        token = str(raw_surface).strip().lower()
        if token in _SEALED_SURFACES:
            return "sealed"
        if token in _GRAVEL_SURFACES:
            return "gravel_compacted"
        if token in _DIRT_SURFACES:
            return "dirt_unsealed"
        if token in _ROUGH_SURFACES:
            return "rough_rock_mud_sand"

    if tracktype == "grade1":
        return "sealed"
    if tracktype == "grade2":
        return "gravel_compacted"
    if tracktype == "grade3":
        return "dirt_unsealed"
    if tracktype in ("grade4", "grade5"):
        return "rough_rock_mud_sand"

    if trail_class in ("fire_trail", "management_trail", "4wd_track"):
        return "dirt_unsealed"
    return "unknown"


def normalize_tracktype(raw: Any) -> TracktypeLiteral | None:
    if raw is None:
        return None
    token = str(raw).strip().lower()
    if token in ("grade1", "grade2", "grade3", "grade4", "grade5"):
        return token  # type: ignore[return-value]
    return None


def dedupe_poi_features(pois: Sequence[RawPoiFeature]) -> list[RawPoiFeature]:
    """Deduplicate POIs sharing ``(poi_type, lower(name), round(lon*1000), round(lat*1000))``.

    Prefers authoritative CC BY 4.0 state records over OSM duplicates (~100 m spatial bucket),
    and prefers records with populated ``elev_m``.
    """
    best_by_key: dict[tuple[str, str, int, int], RawPoiFeature] = {}
    order: list[tuple[str, str, int, int]] = []

    def _priority(poi: RawPoiFeature) -> tuple[int, int]:
        return (
            1 if poi.source_license != "ODbL" else 0,
            1 if poi.elev_m is not None else 0,
        )

    for poi in pois:
        coords = poi.geometry.get("coordinates")
        lon_bucket = 0
        lat_bucket = 0
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            try:
                lon_bucket = int(round(float(coords[0]) * 1000.0))
                lat_bucket = int(round(float(coords[1]) * 1000.0))
            except (TypeError, ValueError):
                pass
        key = (
            poi.poi_type,
            (poi.name or "").strip().lower(),
            lon_bucket,
            lat_bucket,
        )
        if key not in best_by_key:
            best_by_key[key] = poi
            order.append(key)
        elif _priority(poi) > _priority(best_by_key[key]):
            best_by_key[key] = poi

    return [best_by_key[k] for k in order]


# -----------------------------------------------------------------------------
# Cached ArcGIS REST, OGC WFS 2.0.0 & Chunked Streaming HTTP Helpers
# -----------------------------------------------------------------------------


def esri_feature_to_geojson(feat: Mapping[str, Any]) -> dict[str, Any] | None:
    """Convert either a GeoJSON Feature or Esri JSON Feature into standard GeoJSON."""
    if feat.get("type") == "Feature" and "geometry" in feat:
        return dict(feat)
    geom = feat.get("geometry")
    if not isinstance(geom, Mapping):
        return None
    properties = feat.get("attributes") or feat.get("properties")
    props = dict(properties) if isinstance(properties, Mapping) else {}
    if "x" in geom and "y" in geom:
        gj_geom: dict[str, Any] = {"type": "Point", "coordinates": [geom["x"], geom["y"]]}
    elif "paths" in geom:
        paths = geom["paths"]
        if not isinstance(paths, (list, tuple)) or not paths:
            return None
        gj_geom = (
            {"type": "LineString", "coordinates": paths[0]}
            if len(paths) == 1
            else {"type": "MultiLineString", "coordinates": paths}
        )
    elif "rings" in geom:
        gj_geom = {"type": "Polygon", "coordinates": geom["rings"]}
    else:
        return None
    return {"type": "Feature", "geometry": gj_geom, "properties": props}


class UpstreamLimitError(ValueError):
    """A remote payload exceeded its transfer or processing budget."""


@contextmanager
def stream_upstream(
    client: httpx.Client,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    method: str = "GET",
    data: Mapping[str, str] | None = None,
) -> Iterator[httpx.Response]:
    """Follow a bounded number of redirects without reading intermediary bodies."""
    request = client.build_request(
        method, url, params=params, data=data, headers=IDENTITY_HTTP_HEADERS
    )
    for _ in range(6):
        response = client.send(request, stream=True, follow_redirects=False)
        try:
            next_request = response.next_request
            if next_request is None:
                yield response
                return
            request = next_request
        finally:
            response.close()
    raise UpstreamLimitError("Upstream request exceeds the redirect limit")


def checked_content_length(response: httpx.Response, max_bytes: int) -> int | None:
    """Validate any declared length before buffering or writing the response."""
    value = response.headers.get("content-length")
    if value is None:
        return None
    if len(value) > 20 or not value.isascii() or not value.isdecimal():
        raise UpstreamLimitError("Invalid upstream Content-Length")
    length = int(value)
    if length > max_bytes:
        raise UpstreamLimitError("Upstream response exceeds the size limit")
    return length


def iter_bounded_response(
    response: httpx.Response,
    *,
    max_bytes: int,
    max_duration_s: float,
    chunk_bytes: int = 65_536,
) -> Iterator[bytes]:
    """Read an identity or gzip response with cumulative byte and duration limits."""
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("", "identity", "gzip", "x-gzip"):
        raise UpstreamLimitError(f"Unsupported upstream content encoding: {encoding!r}")

    is_gzip = encoding in ("gzip", "x-gzip")
    declared_size = checked_content_length(response, max_bytes)
    deadline = time.monotonic() + max_duration_s

    if response.is_stream_consumed:
        decoded_bytes = 0
        for chunk in response.iter_bytes():
            decoded_bytes += len(chunk)
            if decoded_bytes > max_bytes:
                raise UpstreamLimitError("Upstream response exceeds the size limit")
            if time.monotonic() > deadline:
                raise UpstreamLimitError("Upstream response exceeds the download time limit")
            for offset in range(0, len(chunk), chunk_bytes):
                yield chunk[offset : offset + chunk_bytes]
        if not is_gzip and declared_size is not None and decoded_bytes != declared_size:
            raise UpstreamLimitError("Incomplete upstream response")
        return

    wire_bytes = 0
    decoded_bytes = 0
    decoder = zlib.decompressobj(zlib.MAX_WBITS + 16) if is_gzip else None
    for chunk in response.iter_raw():
        wire_bytes += len(chunk)
        if wire_bytes > max_bytes:
            raise UpstreamLimitError("Upstream response exceeds the size limit")
        if time.monotonic() > deadline:
            raise UpstreamLimitError("Upstream response exceeds the download time limit")
        if decoder is None:
            decoded_bytes += len(chunk)
            for offset in range(0, len(chunk), chunk_bytes):
                yield chunk[offset : offset + chunk_bytes]
            continue

        data = chunk
        while data:
            step_limit = min(chunk_bytes, max_bytes - decoded_bytes + 1)
            try:
                out = decoder.decompress(data, step_limit)
            except zlib.error as exc:
                raise UpstreamLimitError("Invalid upstream gzip response") from exc
            decoded_bytes += len(out)
            if decoded_bytes > max_bytes:
                raise UpstreamLimitError("Upstream response exceeds the size limit")
            if time.monotonic() > deadline:
                raise UpstreamLimitError("Upstream response exceeds the download time limit")
            if out:
                yield out
            if decoder.eof:
                trailing = decoder.unused_data
                if not trailing:
                    break
                decoder = zlib.decompressobj(zlib.MAX_WBITS + 16)
                data = trailing
            elif decoder.unconsumed_tail:
                data = decoder.unconsumed_tail
            else:
                break

    if declared_size is not None and wire_bytes != declared_size:
        raise UpstreamLimitError("Incomplete upstream response")
    if decoder is not None and not decoder.eof:
        raise UpstreamLimitError("Incomplete upstream gzip response")


def read_bounded_json(
    response: httpx.Response, *, max_bytes: int, max_duration_s: float
) -> tuple[Any, int]:
    """Return decoded JSON and its transfer size after verifying the input budget."""
    body = bytearray()
    for chunk in iter_bounded_response(
        response, max_bytes=max_bytes, max_duration_s=max_duration_s
    ):
        body.extend(chunk)
    return json.loads(body), len(body)


def _geometry_node_count(geometry: Mapping[str, Any], max_nodes: int) -> int:
    """Count positions and nested containers before constructing GEOS geometries."""
    pending: list[tuple[Any, int]] = [(geometry, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > max_nodes or depth > 8:
            raise UpstreamLimitError("Vector geometry exceeds the complexity limit")
        if isinstance(node, Mapping):
            children = [
                node[key] for key in ("coordinates", "geometries") if key in node
            ]
        elif isinstance(node, (list, tuple)):
            if node and isinstance(node[0], (int, float)):
                if len(node) > 4 or any(
                    value is not None and not isinstance(value, (int, float)) for value in node
                ):
                    raise UpstreamLimitError("Vector coordinate exceeds the dimension limit")
                children = []
            else:
                children = node
        else:
            children = []
        if len(children) + len(pending) + count > max_nodes:
            raise UpstreamLimitError("Vector geometry exceeds the complexity limit")
        pending.extend((child, depth + 1) for child in children)
    return count


def _count_feature_geometry(
    features: Sequence[Mapping[str, Any]], *, available_nodes: int
) -> int:
    count = 0
    for feature in features:
        geometry = feature.get("geometry")
        if isinstance(geometry, Mapping):
            count += _geometry_node_count(
                geometry, min(_MAX_VECTOR_GEOMETRY_NODES, available_nodes - count)
            )
    return count


def _read_cached_feature_list(
    cache_path: Path | None, *, max_features: int
) -> list[dict[str, Any]] | None:
    if cache_path is None or not cache_path.is_file():
        return None
    try:
        with cache_path.open("rb") as file:
            if file.seek(0, 2) > _MAX_VECTOR_QUERY_BYTES:
                raise UpstreamLimitError("Cached vector response exceeds the size limit")
            file.seek(0)
            raw = file.read(_MAX_VECTOR_QUERY_BYTES + 1)
        if len(raw) > _MAX_VECTOR_QUERY_BYTES:
            raise UpstreamLimitError("Cached vector response exceeds the size limit")
        cached = json.loads(raw)
        if isinstance(cached, list) and len(cached) <= max_features:
            features = [item for item in cached if isinstance(item, dict)]
            _count_feature_geometry(features, available_nodes=_MAX_VECTOR_QUERY_GEOMETRY_NODES)
            return features
    except (OSError, ValueError, RecursionError):
        logger.warning("Ignoring invalid cached overlay JSON: %s", cache_path)
    return None


def _write_cached_feature_list(cache_path: Path | None, features: list[dict[str, Any]]) -> None:
    if cache_path is None:
        return
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=cache_path.parent, suffix=".json.tmp", delete=False
    ) as file:
        tmp = Path(file.name)
        try:
            json.dump(features, file, ensure_ascii=False, separators=(",", ":"))
            file.close()
            tmp.replace(cache_path)
        finally:
            tmp.unlink(missing_ok=True)


def query_arcgis_geojson(
    endpoints: Sequence[str],
    bbox: BBox,
    *,
    cache_path: Path | None = None,
    where: str = "1=1",
    out_fields: str = "*",
    extra_params: Mapping[str, str] | None = None,
    timeout_s: float = 25.0,
    retries: int = 2,
    page_size: int = 1000,
    max_pages: int = 5,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Query an Australian ArcGIS REST service by WGS84 bbox with pagination, fallback, & cache."""
    cached = _read_cached_feature_list(cache_path, max_features=page_size * max_pages)
    if cached is not None:
        return cached

    owns_client = client is None
    http = (
        client
        if client is not None
        else httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=min(8.0, timeout_s)),
            headers={"User-Agent": USER_AGENT},
        )
    )

    last_error: str | None = None
    try:
        for url in endpoints:
            if not url:
                continue
            for fmt in ("geojson", "json"):
                for attempt in range(max(1, retries)):
                    collected: list[dict[str, Any]] = []
                    offset = 0
                    bytes_remaining = _MAX_VECTOR_QUERY_BYTES
                    geometry_remaining = _MAX_VECTOR_QUERY_GEOMETRY_NODES
                    succeeded = True
                    for _page in range(max_pages):
                        params: dict[str, str] = {
                            "where": where,
                            "geometry": (
                                f"{bbox.min_x:.6f},{bbox.min_y:.6f},"
                                f"{bbox.max_x:.6f},{bbox.max_y:.6f}"
                            ),
                            "geometryType": "esriGeometryEnvelope",
                            "inSR": "4326",
                            "spatialRel": "esriSpatialRelIntersects",
                            "outFields": out_fields,
                            "outSR": "4326",
                            "f": fmt,
                            "resultRecordCount": str(page_size),
                            "resultOffset": str(offset),
                        }
                        if extra_params:
                            params.update(extra_params)

                        try:
                            with stream_upstream(http, url, params=params) as resp:
                                resp.raise_for_status()
                                data, page_bytes = read_bounded_json(
                                    resp,
                                    max_bytes=min(_MAX_VECTOR_PAGE_BYTES, bytes_remaining),
                                    max_duration_s=timeout_s * 2,
                                )
                            bytes_remaining -= page_bytes
                        except UpstreamLimitError as exc:
                            logger.warning("Skipping ArcGIS layer: %s", exc)
                            return []
                        except Exception as exc:
                            last_error = f"{url} (f={fmt}, try={attempt + 1}): {exc}"
                            succeeded = False
                            break

                        if not isinstance(data, dict) or "error" in data:
                            err_obj = (
                                data.get("error") if isinstance(data, dict) else type(data).__name__
                            )
                            last_error = f"{url} (f={fmt}): ArcGIS error {err_obj}"
                            succeeded = False
                            break

                        raw_features = data.get("features") or []
                        if not isinstance(raw_features, list) or len(raw_features) > page_size:
                            logger.warning("Skipping ArcGIS layer with invalid feature count")
                            return []
                        features: list[dict[str, Any]] = []
                        for feat in raw_features:
                            if isinstance(feat, Mapping):
                                gj = esri_feature_to_geojson(feat)
                                if gj is not None and gj.get("geometry") is not None:
                                    features.append(gj)
                        try:
                            geometry_remaining -= _count_feature_geometry(
                                features, available_nodes=geometry_remaining
                            )
                        except UpstreamLimitError as exc:
                            logger.warning("Skipping ArcGIS layer: %s", exc)
                            return []
                        collected.extend(features)

                        properties = data.get("properties")
                        exceeded = bool(data.get("exceededTransferLimit")) or (
                            isinstance(properties, Mapping)
                            and bool(properties.get("exceededTransferLimit"))
                        )
                        if not exceeded or not raw_features:
                            break
                        offset += len(raw_features)

                    if succeeded:
                        _write_cached_feature_list(cache_path, collected)
                        return collected
    finally:
        if owns_client:
            http.close()

    if last_error:
        logger.warning("AU ArcGIS query unavailable (%s); skipping layer cleanly", last_error)
    return []


def query_wfs_geojson(
    wfs_url: str,
    type_name: str,
    bbox: BBox,
    *,
    cache_path: Path | None = None,
    cql_filter: str | None = None,
    geom_column: str = "geom",
    use_bbox_param: bool = False,
    max_features: int = 2500,
    timeout_s: float = 25.0,
    retries: int = 2,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Query an OGC WFS 2.0.0 GeoJSON endpoint with EPSG:4326 CQL or CRS84 bbox and disk cache."""
    cached = _read_cached_feature_list(cache_path, max_features=max_features)
    if cached is not None:
        return cached

    params: dict[str, str] = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": type_name,
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "count": str(max_features),
    }
    if use_bbox_param and not cql_filter:
        params["bbox"] = (
            f"{bbox.min_x:.6f},{bbox.min_y:.6f},{bbox.max_x:.6f},{bbox.max_y:.6f},"
            "urn:ogc:def:crs:OGC:1.3:CRS84"
        )
    else:
        bbox_cql = (
            f"BBOX({geom_column},{bbox.min_x:.6f},{bbox.min_y:.6f},"
            f"{bbox.max_x:.6f},{bbox.max_y:.6f},'EPSG:4326')"
        )
        params["CQL_FILTER"] = f"{bbox_cql} AND ({cql_filter})" if cql_filter else bbox_cql

    owns_client = client is None
    http = (
        client
        if client is not None
        else httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=min(8.0, timeout_s)),
            headers={"User-Agent": USER_AGENT},
        )
    )

    last_error: str | None = None
    try:
        for attempt in range(max(1, retries)):
            try:
                with stream_upstream(http, wfs_url, params=params) as resp:
                    resp.raise_for_status()
                    data, _ = read_bounded_json(
                        resp, max_bytes=_MAX_VECTOR_PAGE_BYTES, max_duration_s=timeout_s * 2
                    )
            except UpstreamLimitError as exc:
                logger.warning("Skipping WFS layer %s: %s", type_name, exc)
                return []
            except Exception as exc:
                last_error = f"{wfs_url} ({type_name}, try={attempt + 1}): {exc}"
                continue

            if not isinstance(data, dict) or not isinstance(data.get("features"), list):
                last_error = f"{wfs_url} ({type_name}): unexpected WFS payload"
                continue

            if len(data["features"]) > max_features:
                logger.warning("Skipping WFS layer %s with too many features", type_name)
                return []
            features = [
                f
                for f in data["features"]
                if isinstance(f, dict) and f.get("geometry") is not None
            ]
            try:
                _count_feature_geometry(features, available_nodes=_MAX_VECTOR_QUERY_GEOMETRY_NODES)
            except UpstreamLimitError as exc:
                logger.warning("Skipping WFS layer %s: %s", type_name, exc)
                return []
            _write_cached_feature_list(cache_path, features)
            return features
    finally:
        if owns_client:
            http.close()

    if last_error:
        logger.warning("AU WFS query unavailable (%s); skipping layer cleanly", last_error)
    return []


def stream_download_file(
    url: str,
    dest: Path,
    *,
    chunk_bytes: int = 1_048_576,
    timeout_s: float = 120.0,
    max_bytes: int = 128 * 1024 * 1024,
    verify_cached_size: bool = False,
    client: httpx.Client | None = None,
) -> Path:
    """Download within byte and free-space budgets and publish the complete file atomically."""
    if max_bytes <= 0 or chunk_bytes <= 0:
        raise ValueError("Download and chunk size limits must be positive")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest.with_suffix(dest.suffix + ".part")
    size_path = dest.with_suffix(dest.suffix + ".size")
    size_part_path = size_path.with_suffix(".size.part")

    # Serialize downloads sharing a cache directory so their free-space checks and
    # temporary files also work across worker processes.
    with (dest.parent / ".download.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        part_path.unlink(missing_ok=True)
        size_part_path.unlink(missing_ok=True)
        if dest.is_file():
            cached_size = dest.stat().st_size
            if 0 < cached_size <= max_bytes:
                if not verify_cached_size:
                    return dest
                try:
                    with size_path.open("rb") as file:
                        if file.read(32) == str(cached_size).encode("ascii"):
                            return dest
                except FileNotFoundError:
                    pass
        return _download_to_cache(
            url, dest, part_path, size_path if verify_cached_size else None, size_part_path,
            chunk_bytes=chunk_bytes, timeout_s=timeout_s, max_bytes=max_bytes, client=client,
        )


def _download_to_cache(
    url: str,
    dest: Path,
    part_path: Path,
    size_path: Path | None,
    size_part_path: Path,
    *,
    chunk_bytes: int,
    timeout_s: float,
    max_bytes: int,
    client: httpx.Client | None,
) -> Path:
    owns_client = client is None
    http = (
        client
        if client is not None
        else httpx.Client(
            timeout=httpx.Timeout(connect=30.0, read=timeout_s, write=30.0, pool=30.0),
            headers={"User-Agent": USER_AGENT},
        )
    )
    try:
        with stream_upstream(http, url) as resp:
            resp.raise_for_status()
            if resp.status_code != 200 or "content-range" in resp.headers:
                raise UpstreamLimitError("Expected a complete upstream download")
            declared_size = checked_content_length(resp, max_bytes)
            available = shutil.disk_usage(dest.parent).free - _MIN_DOWNLOAD_FREE_BYTES
            if available <= 0 or (declared_size is not None and declared_size > available):
                raise UpstreamLimitError("Insufficient free space for upstream download")
            with part_path.open("wb") as fh:
                for chunk in iter_bounded_response(
                    resp, max_bytes=max_bytes, max_duration_s=_MAX_DOWNLOAD_SECONDS,
                    chunk_bytes=chunk_bytes,
                ):
                    if shutil.disk_usage(dest.parent).free < _MIN_DOWNLOAD_FREE_BYTES + len(chunk):
                        raise UpstreamLimitError("Insufficient free space for upstream download")
                    fh.write(chunk)
                file_size = fh.tell()
        if file_size == 0:
            raise UpstreamLimitError("Empty upstream download")
        if size_path is not None:
            size_part_path.write_text(str(file_size), encoding="ascii")
        part_path.replace(dest)
        if size_path is not None:
            size_part_path.replace(size_path)
    finally:
        part_path.unlink(missing_ok=True)
        size_part_path.unlink(missing_ok=True)
        if owns_client:
            http.close()

    return dest
