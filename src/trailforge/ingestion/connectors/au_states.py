"""Pan-Australia official state/territory trail, hut & trig connectors (ACT/TAS/QLD/WA/SA/NT).

Ported from verified GridForge v1.2.0 Australian open-data endpoints (read-only reference).
All connectors accept an injectable ``httpx.Client`` for 100% offline testing, use bounded
spatial tiling + deterministic disk caches, preserve exact license attributions, and keep
OSM ODbL strictly isolated in Tier A.

Commercial-friendly license hygiene:
  - ACTmapi / EPSDD (ACT): CC BY 4.0
  - the LIST / NRE Tas (TAS): CC BY 3.0 AU
  - QSpatial / DESI / DNRM (QLD): CC BY 4.0
  - DBCA AGOL & Landgate SLIP (WA): CC BY 4.0 — strictly excludes any ``CC BY-NC`` records
    (including DBCA-027)
  - DEW / DHUD / Location SA + GA Topo250K (SA): CC BY 4.0 (low-RAM ``/vsizip/`` streaming)
  - NTLIS / DEPWS + NTG Roads + GA Topo250K (NT): CC BY 4.0
"""

from __future__ import annotations

import json
import logging
import os
import re
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import httpx
from pydantic import BaseModel, Field
from shapely.geometry import mapping, shape

from trailforge.ingestion.connectors.base import (
    ACT_STRICT_BBOX,
    ATTRIBUTION_ACT_ACTMAPI,
    ATTRIBUTION_NSW_SPATIAL,
    ATTRIBUTION_NT_NTLIS,
    ATTRIBUTION_QLD_QSPATIAL,
    ATTRIBUTION_SA_DEW_DHUD,
    ATTRIBUTION_TAS_LIST,
    ATTRIBUTION_VIC_DEECA,
    ATTRIBUTION_WA_DBCA_SLIP,
    BBox,
    PoiTypeLiteral,
    RawPoiFeature,
    RawTrackFeature,
    TrailClassLiteral,
    clean_feature_name,
    dedupe_poi_features,
    deterministic_feature_id,
    is_in_act_coverage,
    is_in_nsw_coverage,
    is_in_nt_coverage,
    is_in_qld_coverage,
    is_in_sa_coverage,
    is_in_tas_coverage,
    is_in_vic_coverage,
    is_in_wa_coverage,
    iter_spatial_tiles,
    normalize_surface_class,
    parse_elevation_m,
    prop_ci,
    query_arcgis_geojson,
    query_wfs_geojson,
    stream_download_file,
    to_float_or_none,
)
from trailforge.ingestion.connectors.nsw import NSWTrailsConnector
from trailforge.ingestion.connectors.vic import VICTrailsConnector

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Verified Australian State & Territory Upstream Endpoints (GridForge v1.2.0)
# -----------------------------------------------------------------------------

# ACT — ACTmapi / EPSDD ArcGIS Online FeatureServer host (CC BY 4.0)
DEFAULT_ACT_BASE_URL: Final[str] = (
    "https://services1.arcgis.com/E5n4f1VY84i0xSjy/arcgis/rest/services"
)

# TAS — the LIST / NRE Tas OpenDataWFS & TopographyAndRelief MapServers (CC BY 3.0 AU)
DEFAULT_TAS_OPENDATA_BASE_URL: Final[str] = (
    "https://services.thelist.tas.gov.au/arcgis/rest/services/Public/OpenDataWFS/MapServer"
)
DEFAULT_TAS_TOPO_BASE_URL: Final[str] = (
    "https://services.thelist.tas.gov.au/arcgis/rest/services/Public/TopographyAndRelief/MapServer"
)

# QLD — QSpatial DESI / DNRM QPWS Parks, RoadsAndTracks, Tourist Facilities & Trigs (CC BY 4.0)
DEFAULT_QLD_PARKS_BASE_URL: Final[str] = (
    "https://spatial-gis.information.qld.gov.au/arcgis/rest/services/"
    "Environment/ParksTerrestrialProtectedAreas/MapServer"
)
DEFAULT_QLD_ROADS_URL: Final[str] = (
    "https://spatial-gis.information.qld.gov.au/arcgis/rest/services/"
    "Transportation/RoadsAndTracks/MapServer/10/query"
)
DEFAULT_QLD_TOURIST_URL: Final[str] = (
    "https://spatial-gis.information.qld.gov.au/arcgis/rest/services/"
    "Structure/PhysicalInfrastructure/MapServer/60/query"
)
DEFAULT_QLD_SURVEY_URL: Final[str] = (
    "https://spatial-gis.information.qld.gov.au/arcgis/rest/services/"
    "Basemaps/FoundationData/MapServer/8/query"
)

# WA — DBCA AGOL & Landgate SLIP Public Services (strictly CC BY 4.0; excludes DBCA-027 CC BY-NC)
DEFAULT_WA_TRACKS_URL: Final[str] = (
    "https://services3.arcgis.com/lSP3CnkTXxMmSpdI/arcgis/rest/services/"
    "Tracks_and_Trails_hosted/FeatureServer/0/query"
)
DEFAULT_WA_SLIP_ROADS_URL: Final[str] = (
    "https://public-services.slip.wa.gov.au/public/rest/services/"
    "SLIP_Public_Services/Transport/MapServer/20/query"
)
DEFAULT_WA_RATIS_SITES_URL: Final[str] = (
    "https://services3.arcgis.com/lSP3CnkTXxMmSpdI/arcgis/rest/services/"
    "RATIS_Sites/FeatureServer/0/query"
)
DEFAULT_WA_RATIS_FACILITIES_URL: Final[str] = (
    "https://services3.arcgis.com/lSP3CnkTXxMmSpdI/arcgis/rest/services/"
    "RATIS_Facilities/FeatureServer/0/query"
)
DEFAULT_WA_FIRE_TOWERS_URL: Final[str] = (
    "https://public-services.slip.wa.gov.au/public/rest/services/"
    "SLIP_Public_Services/Environment/MapServer/56/query"
)
DEFAULT_WA_GEODETIC_URL: Final[str] = (
    "https://public-services.slip.wa.gov.au/public/rest/services/"
    "SLIP_Public_Services/Property_and_Planning/MapServer/1/query"
)

# SA — DEW / DHUD / Location SA + GA Topo250K (CC BY 4.0)
DEFAULT_SA_TRAILS_ZIP_URL: Final[str] = (
    "https://www.waterconnect.sa.gov.au/Content/Downloads/DEWNR/TOPO_RecreationTrails_geojson.zip"
)
DEFAULT_SA_TRAILS_ZIP_INNER: Final[str] = "TOPO_RecreationTrails_GDA2020.geojson"
DEFAULT_SA_FIRE_TRACKS_URL: Final[str] = (
    "https://services-ap1.arcgis.com/G7iVcvOu3F7YCq11/arcgis/rest/services/"
    "Fire_Management_Fire_Access_Tracks/FeatureServer/1/query"
)
DEFAULT_SA_PARKS_JSON_URL: Final[str] = (
    "https://data.sa.gov.au/data/dataset/919a3d26-e150-4282-8c30-2e090287b9b3/"
    "resource/4aec8622-c685-4484-bb83-18cda3f30959/download/parks-data.json"
)
DEFAULT_SA_SURVEY_MARKS_URL: Final[str] = (
    "https://location.sa.gov.au/server5/rest/services/MAD_WMS_PRD/MapServer/0/query"
)

# NT — NTG Road Centrelines + NTLIS GeoServer WFS 2.0 + GA Topo250K (CC BY 4.0)
DEFAULT_NT_ROADS_URL: Final[str] = (
    "https://gis-d.nt.gov.au/arcgis/rest/services/ROAD_CENTRELINES/FeatureServer/0/query"
)
DEFAULT_NT_WFS_URL: Final[str] = "https://ogc.ntlis.nt.gov.au/gs/ows"

# Shared Geoscience Australia Topo250K Roads (Outback 4WD/Bush Tracks, CC BY 4.0)
DEFAULT_GA_TOPO250K_ROADS_URL: Final[str] = (
    "https://services.ga.gov.au/gis/rest/services/Topographic_Base_Map/MapServer/434/query"
)

_HUT_WORD_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(hut|huts|bivouac|shelter|refuge|cabin)\b", re.IGNORECASE
)
_WA_NC_LICENSE_RE: Final[re.Pattern[str]] = re.compile(
    r"(cc[\s_-]*by[\s_-]*nc|\bby[\s_-]*nc\b|non[\s_-]*commercial|dbca[\s_-]*027)",
    re.IGNORECASE,
)
_ACT_GENERIC_PARK_LOCATIONS: Final[frozenset[str]] = frozenset(
    {
        "namadgi national park",
        "tidbinbilla nature reserve",
        "tidbinbilla special purpose reserve",
        "canberra nature park",
        "murrumbidgee river corridor",
    }
)
_ACT_PARK_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:namadgi national park|tidbinbilla (?:nature|special purpose) reserve)\s*,?\s+",
    re.IGNORECASE,
)
_NT_PLACENAME_POI_MAP: Final[dict[str, PoiTypeLiteral]] = {
    "lookout": "lookout",
    "cliff": "lookout",
    "bluff": "lookout",
    "gorge": "lookout",
    "waterfall": "lookout",
    "hut": "hut",
    "shelter": "hut",
    "ranger station": "hut",
    "campground": "campground",
    "camping area": "campground",
    "camping ground": "campground",
    "picnic area": "picnic",
    "rest area": "picnic",
}
_WA_LICENSE_KEYS: Final[tuple[str, ...]] = (
    "license",
    "licence",
    "data_license",
    "data_licence",
    "rights",
    "attribution",
    "dataset",
    "dataset_id",
    "source_dataset",
    "layer_id",
    "slip_id",
    "terms",
)


# -----------------------------------------------------------------------------
# Shared Geometry, Filtering & License-Hygiene Helpers
# -----------------------------------------------------------------------------


def _bbox_strictly_within(inner: BBox, outer: BBox) -> bool:
    return (
        inner.min_x >= outer.min_x
        and inner.max_x <= outer.max_x
        and inner.min_y >= outer.min_y
        and inner.max_y <= outer.max_y
    )


def _coerce_line_geometry(geom: Any) -> dict[str, Any] | None:
    if not isinstance(geom, Mapping):
        return None
    gtype = geom.get("type")
    if gtype not in ("LineString", "MultiLineString"):
        return None
    coords = geom.get("coordinates")
    if not coords:
        return None
    return dict(geom)


def _coerce_point_geometry(geom: Any) -> dict[str, Any] | None:
    """Return a GeoJSON Point dict, deriving representative point for Polygon features."""
    if not isinstance(geom, Mapping):
        return None
    gtype = geom.get("type")
    if gtype == "Point":
        coords = geom.get("coordinates")
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            return {"type": "Point", "coordinates": [float(coords[0]), float(coords[1])]}
        return None
    if gtype in ("Polygon", "MultiPolygon", "MultiPoint", "LineString", "MultiLineString"):
        try:
            shp = shape(geom)
            if shp.is_empty:
                return None
            pt = shp.representative_point()
            return dict(mapping(pt))
        except Exception:
            return None
    return None


def _clean_au_name(raw: Any, *, strip_leading_star: bool = False) -> str | None:
    if raw is None:
        return None
    text = re.sub(r"\s+", " ", str(raw)).strip()
    if strip_leading_star:
        text = text.lstrip("*").strip()
    if text.lower() in ("unnamed track", "unnamed road", "pastoral road"):
        return None
    return clean_feature_name(text)


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def is_wa_non_commercial_restricted(
    props: Mapping[str, Any] | None = None,
    *,
    source_url: str = "",
) -> bool:
    """Return True if a WA endpoint or feature carries a ``CC BY-NC`` / ``DBCA-027`` restriction."""
    if source_url and _WA_NC_LICENSE_RE.search(source_url):
        return True
    if not props:
        return False
    for key in _WA_LICENSE_KEYS:
        val = prop_ci(props, key)
        if val is not None and _WA_NC_LICENSE_RE.search(str(val)):
            return True
    # Belt-and-suspenders scan across any string value explicitly mentioning CC BY-NC or DBCA-027
    return any(isinstance(val, str) and _WA_NC_LICENSE_RE.search(val) for val in props.values())


def _stream_download_cached_safe(
    url: str,
    dest: Path,
    *,
    chunk_bytes: int = 1_048_576,
    timeout_s: float = 60.0,
    client: httpx.Client | None = None,
) -> Path | None:
    """Stream-download a file with atomic cache write, returning None gracefully on outages."""
    try:
        return stream_download_file(
            url,
            dest,
            chunk_bytes=chunk_bytes,
            timeout_s=timeout_s,
            client=client,
        )
    except Exception as exc:
        logger.warning("AU state download unavailable (%s: %s); skipping cleanly", url, exc)
        return None


def _geom_intersects_bbox(geom: Mapping[str, Any], bbox: BBox) -> bool:
    try:
        shp = shape(geom)
        if shp.is_empty:
            return False
        minx, miny, maxx, maxy = shp.bounds
        return (
            maxx >= bbox.min_x
            and minx <= bbox.max_x
            and maxy >= bbox.min_y
            and miny <= bbox.max_y
        )
    except Exception:
        return False


# -----------------------------------------------------------------------------
# 1. Australian Capital Territory (ACTmapi / EPSDD — CC BY 4.0)
# -----------------------------------------------------------------------------


def _norm_act_location(raw_name: str) -> str:
    cleaned = _ACT_PARK_PREFIX_RE.sub("", raw_name).strip()
    cleaned = re.sub(r"\s*-\s*shelter.*$", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s+\d+$", "", cleaned).strip()
    return cleaned or raw_name


def parse_act_track_feature(
    feat: Mapping[str, Any],
    *,
    source_layer: str = "act_transport_track",
) -> RawTrackFeature | None:
    """Normalize an ACTmapi transport track, equestrian route, or rural road feature."""
    geom = _coerce_line_geometry(feat.get("geometry"))
    if geom is None:
        return None
    props = dict(feat.get("properties") or {})

    if source_layer == "act_equestrian_routes":
        is_bnt = str(prop_ci(props, "BICENTENNIAL_NATIONAL_TRAIL") or "").strip().upper() == "YES"
        name = "Bicentennial National Trail" if is_bnt else _clean_au_name(prop_ci(props, "NAME"))
        trail_class: TrailClassLiteral = "walking_track" if is_bnt else "management_trail"
        is_vehicular = not is_bnt
        surface_raw = "natural"
        access_raw = str(prop_ci(props, "ROUTE_STATUS") or "").strip() or None
    elif source_layer == "act_rural_roads":
        name = _clean_au_name(prop_ci(props, "ROAD_NAME", "name"))
        lower_name = (name or "").lower()
        trail_class = "fire_trail" if "fire" in lower_name else "management_trail"
        is_vehicular = True
        surface_raw = "unsealed"
        access_raw = str(prop_ci(props, "HIERARCHY") or "").strip() or None
    else:
        name = _clean_au_name(prop_ci(props, "ROAD_NAME", "name"))
        lower_name = (name or "").lower()
        if "fire" in lower_name:
            trail_class = "fire_trail"
        elif "trail" in lower_name or "management" in lower_name:
            trail_class = "management_trail"
        else:
            trail_class = "4wd_track"
        is_vehicular = True
        surface_raw = "unsealed"
        access_raw = str(prop_ci(props, "DISTRICT_NAME") or "").strip() or None

    feat_id = str(
        prop_ci(props, "OBJECTID", "FID", "id")
        or feat.get("id")
        or deterministic_feature_id(source_layer, geom, props)
    ).strip()

    return RawTrackFeature(
        source="act",
        source_layer=source_layer,
        source_id=feat_id,
        name=name,
        trail_class=trail_class,
        is_vehicular=is_vehicular,
        surface_class=normalize_surface_class(surface_raw, trail_class=trail_class),
        surface_raw=surface_raw,
        access_status="public",
        access_raw=access_raw,
        geometry=geom,
        raw_props=props,
    )


class ACTTrailsConnector:
    """ACTmapi / EPSDD Fire Trails, Tracks, Namadgi Huts, Camps & AHD71 Survey Marks (CC BY 4.0)."""

    name: str = "au-act-trails"
    state_code: str = "ACT"
    attribution: str = ATTRIBUTION_ACT_ACTMAPI

    def __init__(
        self,
        *,
        base_url: str | None = None,
        cache_dir: Path | str | None = None,
        timeout_s: float = 25.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("TRAILFORGE_AU_ACT_BASE_URL", DEFAULT_ACT_BASE_URL)
        ).rstrip("/")
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.timeout_s = timeout_s
        self._client = client

    def _cache_file(self, key: str) -> Path | None:
        return (self.cache_dir / "au_act" / f"{key}.json") if self.cache_dir is not None else None

    def load_tracks_geojson(
        self, path: Path | str, *, source_layer: str = "act_transport_track"
    ) -> list[RawTrackFeature]:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        out: list[RawTrackFeature] = []
        for feat in payload.get("features") or []:
            if isinstance(feat, Mapping):
                parsed = parse_act_track_feature(feat, source_layer=source_layer)
                if parsed is not None:
                    out.append(parsed)
        return out

    def fetch_tracks(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawTrackFeature]:
        """Fetch ACT transport tracks, equestrian/BNT routes, and rural fire/management roads."""
        if not is_in_act_coverage(bbox):
            return []

        tracks: list[RawTrackFeature] = []
        seen: set[tuple[str, str]] = set()
        for tile in iter_spatial_tiles(bbox.round_enclosing(), max_deg=tile_deg):
            bhash = tile.cache_hash()
            layer_jobs = (
                (
                    f"{self.base_url}/ACTGOV_TRANSPORT_TRACK/FeatureServer/0/query",
                    "act_transport_track",
                    "1=1",
                    "OBJECTID,ROAD_NAME,DISTRICT_NAME",
                    f"tracks_{bhash}",
                ),
                (
                    f"{self.base_url}/ACT_Equestrian_Routes/FeatureServer/0/query",
                    "act_equestrian_routes",
                    "1=1",
                    "OBJECTID,BICENTENNIAL_NATIONAL_TRAIL,ROUTE_STATUS",
                    f"equestrian_{bhash}",
                ),
                (
                    f"{self.base_url}/ACTGOV_ROAD_CENTRELINES/FeatureServer/0/query",
                    "act_rural_roads",
                    "HIERARCHY = 'RURAL SPECIAL PURPOSE'",
                    "OBJECTID,ROAD_NAME,HIERARCHY",
                    f"rural_roads_{bhash}",
                ),
            )
            for url, layer_name, where, out_fields, ckey in layer_jobs:
                feats = query_arcgis_geojson(
                    [url],
                    tile,
                    cache_path=self._cache_file(ckey),
                    where=where,
                    out_fields=out_fields,
                    timeout_s=self.timeout_s,
                    client=self._client,
                )
                for feat in feats:
                    parsed = parse_act_track_feature(feat, source_layer=layer_name)
                    if parsed is not None and (parsed.source_layer, parsed.source_id) not in seen:
                        seen.add((parsed.source_layer, parsed.source_id))
                        tracks.append(parsed)
        return tracks

    def fetch_pois(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawPoiFeature]:
        """Fetch ACT heritage huts, lookouts, campgrounds, picnic shelters, and AHD71 trigs."""
        if not is_in_act_coverage(bbox):
            return []

        raw_pois: list[RawPoiFeature] = []
        seen_act_locations: set[tuple[str, str]] = set()
        for tile in iter_spatial_tiles(bbox.round_enclosing(), max_deg=tile_deg):
            bhash = tile.cache_hash()
            heritage_huts = query_arcgis_geojson(
                [f"{self.base_url}/ACTGOV_Heritage_Register/FeatureServer/1/query"],
                tile,
                cache_path=self._cache_file(f"heritage_huts_{bhash}"),
                where=(
                    "(UPPER(SITE_NAME) LIKE '%HUT%' OR UPPER(NAME) LIKE '%HUT%') "
                    "AND DataRestrictions = 'No'"
                ),
                out_fields="OBJECTID,SITE_NAME,NAME,DataRestrictions",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in heritage_huts:
                pt = _coerce_point_geometry(feat.get("geometry"))
                if pt is None:
                    continue
                props = dict(feat.get("properties") or {})
                name = _clean_au_name(prop_ci(props, "SITE_NAME", "NAME"))
                if not name:
                    continue
                fid = str(
                    prop_ci(props, "OBJECTID", "id")
                    or feat.get("id")
                    or deterministic_feature_id("act_heritage_hut", pt, props)
                )
                raw_pois.append(
                    RawPoiFeature(
                        source_provider="act_heritage_hut",
                        source_license="CC-BY-4.0",
                        source_feature_id=fid,
                        name=name,
                        poi_type="hut",
                        state="ACT",
                        raw_props=props,
                        geometry=pt,
                    )
                )

            feat_names = query_arcgis_geojson(
                [f"{self.base_url}/ACTGOV_FEATURE_NAME/FeatureServer/0/query"],
                tile,
                cache_path=self._cache_file(f"feature_names_{bhash}"),
                where=(
                    "TYPE IN ('LOOKOUT','CAMPGROUND','PICNIC AREA') "
                    "OR UPPER(DISPLAY_NAME) LIKE '%HUT%' OR UPPER(DISPLAY_NAME) LIKE '%COTTAGE%'"
                ),
                out_fields="OBJECTID,TYPE,DISPLAY_NAME,FEATURE_NAME",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in feat_names:
                pt = _coerce_point_geometry(feat.get("geometry"))
                if pt is None:
                    continue
                props = dict(feat.get("properties") or {})
                ftype = str(prop_ci(props, "TYPE") or "").strip().lower()
                name = _clean_au_name(prop_ci(props, "DISPLAY_NAME", "FEATURE_NAME"))
                poi_type: PoiTypeLiteral | None = None
                if "lookout" in ftype:
                    poi_type = "lookout"
                elif "camp" in ftype:
                    poi_type = "campground"
                elif "picnic" in ftype:
                    poi_type = "picnic"
                elif name and (_HUT_WORD_RE.search(name) or "cottage" in name.lower()):
                    poi_type = "hut"
                if poi_type is None:
                    continue
                fid = str(
                    prop_ci(props, "OBJECTID", "id")
                    or feat.get("id")
                    or deterministic_feature_id("act_feature_name", pt, props)
                )
                raw_pois.append(
                    RawPoiFeature(
                        source_provider="act_feature_name",
                        source_license="CC-BY-4.0",
                        source_feature_id=fid,
                        name=name,
                        poi_type=poi_type,
                        state="ACT",
                        raw_props=props,
                        geometry=pt,
                    )
                )

            bbq_feats = query_arcgis_geojson(
                [f"{self.base_url}/ACTGOV_Barbecue_Assets/FeatureServer/1/query"],
                tile,
                cache_path=self._cache_file(f"bbq_{bhash}"),
                out_fields="OBJECTID,ASSET_NAME,LOCATION",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in bbq_feats:
                pt = _coerce_point_geometry(feat.get("geometry"))
                if pt is None:
                    continue
                props = dict(feat.get("properties") or {})
                loc = _clean_au_name(prop_ci(props, "LOCATION")) or ""
                asset_name = _clean_au_name(prop_ci(props, "ASSET_NAME")) or ""
                candidate = (
                    asset_name
                    if loc.lower() in _ACT_GENERIC_PARK_LOCATIONS
                    else (loc or asset_name)
                )
                lower_cand = candidate.lower()
                if not candidate or lower_cand in _ACT_GENERIC_PARK_LOCATIONS:
                    continue
                if "camp" in lower_cand:
                    poi_type = "campground"
                elif "picnic" in lower_cand:
                    poi_type = "picnic"
                else:
                    continue
                norm_label = _norm_act_location(candidate)
                loc_key = (poi_type, norm_label.lower())
                if loc_key in seen_act_locations:
                    continue
                seen_act_locations.add(loc_key)
                fid = str(
                    prop_ci(props, "OBJECTID", "id")
                    or feat.get("id")
                    or deterministic_feature_id("act_bbq", pt, props)
                )
                raw_pois.append(
                    RawPoiFeature(
                        source_provider="act_bbq",
                        source_license="CC-BY-4.0",
                        source_feature_id=fid,
                        name=norm_label,
                        poi_type=poi_type,
                        state="ACT",
                        raw_props=props,
                        geometry=pt,
                    )
                )

            shelter_feats = query_arcgis_geojson(
                [f"{self.base_url}/ACTGOV_Shelter_Assets/FeatureServer/1/query"],
                tile,
                cache_path=self._cache_file(f"shelters_{bhash}"),
                out_fields="OBJECTID,ASSET_NAME,LOCATION",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in shelter_feats:
                pt = _coerce_point_geometry(feat.get("geometry"))
                if pt is None:
                    continue
                props = dict(feat.get("properties") or {})
                loc = _clean_au_name(prop_ci(props, "LOCATION")) or ""
                asset_name = _clean_au_name(prop_ci(props, "ASSET_NAME")) or ""
                combined = f"{asset_name} {loc}".lower()
                if "camp" in combined:
                    norm_label = _norm_act_location(
                        asset_name if "camp" in asset_name.lower() else loc
                    )
                    loc_key = ("campground", norm_label.lower())
                    if loc_key in seen_act_locations:
                        continue
                    seen_act_locations.add(loc_key)
                    poi_type = "campground"
                    out_name: str | None = norm_label
                elif "picnic" in combined:
                    norm_label = _norm_act_location(loc or asset_name)
                    loc_key = ("picnic", norm_label.lower())
                    if loc_key in seen_act_locations:
                        continue
                    seen_act_locations.add(loc_key)
                    poi_type = "picnic"
                    out_name = norm_label
                elif asset_name:
                    norm_label = _norm_act_location(asset_name)
                    poi_type = "hut"
                    out_name = None if norm_label.lower() == "shelter" else norm_label
                else:
                    continue
                fid = str(
                    prop_ci(props, "OBJECTID", "id")
                    or feat.get("id")
                    or deterministic_feature_id("act_shelter", pt, props)
                )
                raw_pois.append(
                    RawPoiFeature(
                        source_provider="act_shelter",
                        source_license="CC-BY-4.0",
                        source_feature_id=fid,
                        name=out_name,
                        poi_type=poi_type,
                        state="ACT",
                        raw_props=props,
                        geometry=pt,
                    )
                )

            trig_feats = query_arcgis_geojson(
                [f"{self.base_url}/ACTGOV_SURVEY_CONTROL_MARK_CURRENT_View/FeatureServer/3/query"],
                tile,
                cache_path=self._cache_file(f"trigs_{bhash}"),
                where=(
                    "UPPER(ALIAS) LIKE 'TS%' OR UPPER(MARK_CONSTRUCTION) LIKE '%PILLAR%' "
                    "OR (BEACON_CONSTRUCTION IS NOT NULL AND BEACON_CONSTRUCTION <> 'NONE') "
                    "OR (MARK_TYPE = 'MC' AND REDUCED_LEVEL IS NOT NULL)"
                ),
                out_fields=(
                    "OBJECTID,MARK_TYPE,MARK_NAME_FULL,ALIAS,REDUCED_LEVEL,"
                    "MARK_CONSTRUCTION,BEACON_CONSTRUCTION"
                ),
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in trig_feats:
                pt = _coerce_point_geometry(feat.get("geometry"))
                if pt is None:
                    continue
                props = dict(feat.get("properties") or {})
                name = _clean_au_name(prop_ci(props, "MARK_NAME_FULL", "ALIAS")) or "Trig Station"
                fid = str(
                    prop_ci(props, "OBJECTID", "ALIAS", "id")
                    or feat.get("id")
                    or deterministic_feature_id("act_trig", pt, props)
                )
                raw_pois.append(
                    RawPoiFeature(
                        source_provider="act_trig",
                        source_license="CC-BY-4.0",
                        source_feature_id=fid,
                        name=name,
                        poi_type="trig",
                        state="ACT",
                        elev_m=parse_elevation_m(prop_ci(props, "REDUCED_LEVEL", "elev_m")),
                        raw_props=props,
                        geometry=pt,
                    )
                )

        return dedupe_poi_features(raw_pois)


# -----------------------------------------------------------------------------
# 2. Tasmania (the LIST / NRE Tas — CC BY 3.0 AU)
# -----------------------------------------------------------------------------


def _classify_tas_segment(
    props: Mapping[str, Any], name: str | None
) -> tuple[TrailClassLiteral, str, bool]:
    lower_name = (name or "").lower()
    tseg = str(prop_ci(props, "TSEG_FEAT") or "").strip().lower()
    ttype = str(prop_ci(props, "TRANS_TYPE") or "").strip().lower()
    tclass = str(prop_ci(props, "TRAN_CLASS") or "").strip().lower()
    surf_raw = str(prop_ci(props, "SURFACE_TY") or "").strip().lower()

    if "fire" in tseg or "fire trail" in lower_name or "firetrail" in lower_name:
        trail_class: TrailClassLiteral = "fire_trail"
    elif (
        ttype == "track"
        or "as2156" in tclass
        or "walking" in tclass
        or "shared use" in tclass
        or "bike" in tclass
    ):
        trail_class = "walking_track"
    elif "access road" in tclass or "management" in lower_name:
        trail_class = "management_trail"
    else:
        trail_class = "4wd_track"

    if "sealed" in surf_raw and "unsealed" not in surf_raw:
        surface = "sealed"
    elif "boardwalk" in surf_raw:
        surface = "boardwalk"
    elif "unsealed" in surf_raw or "4wd" in surf_raw:
        surface = "unsealed"
    else:
        surface = "natural" if trail_class == "walking_track" else "unsealed"

    is_4wd_only = "4wd" in surf_raw
    return trail_class, surface, is_4wd_only


def parse_tas_track_feature(feat: Mapping[str, Any]) -> RawTrackFeature | None:
    """Normalize a LISTdata OpenDataWFS Layer 42 transport segment feature."""
    geom = _coerce_line_geometry(feat.get("geometry"))
    if geom is None:
        return None
    props = dict(feat.get("properties") or {})
    name = _clean_au_name(prop_ci(props, "PRI_NAME", "SEC_NAME", "name"))
    trail_class, surface_raw, is_4wd_only = _classify_tas_segment(props, name)
    is_vehicular = trail_class != "walking_track"
    feat_id = str(
        prop_ci(props, "OBJECTID", "TRANSEG_ID", "id")
        or feat.get("id")
        or deterministic_feature_id("tas_transport_segments", geom, props)
    ).strip()

    return RawTrackFeature(
        source="tas",
        source_layer="tas_transport_segments",
        source_id=feat_id,
        name=name,
        trail_class=trail_class,
        is_vehicular=is_vehicular,
        surface_class=normalize_surface_class(surface_raw, trail_class=trail_class),
        surface_raw=surface_raw,
        is_4wd_only=is_4wd_only,
        access_status="public",
        access_raw=str(prop_ci(props, "TRAN_CLASS") or "").strip() or None,
        geometry=geom,
        raw_props=props,
    )


class TASTrailsConnector:
    """Tasmania the LIST / NRE Tas OpenDataWFS tracks, fire trails, huts & trigs (CC BY 3.0 AU)."""

    name: str = "au-tas-trails"
    state_code: str = "TAS"
    attribution: str = ATTRIBUTION_TAS_LIST

    def __init__(
        self,
        *,
        opendata_base_url: str | None = None,
        topo_base_url: str | None = None,
        cache_dir: Path | str | None = None,
        timeout_s: float = 25.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.opendata_base_url = (
            opendata_base_url
            or os.environ.get("TRAILFORGE_AU_TAS_OPENDATA_URL", DEFAULT_TAS_OPENDATA_BASE_URL)
        ).rstrip("/")
        self.topo_base_url = (
            topo_base_url
            or os.environ.get("TRAILFORGE_AU_TAS_TOPO_URL", DEFAULT_TAS_TOPO_BASE_URL)
        ).rstrip("/")
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.timeout_s = timeout_s
        self._client = client

    def _cache_file(self, key: str) -> Path | None:
        return (self.cache_dir / "au_tas" / f"{key}.json") if self.cache_dir is not None else None

    def load_tracks_geojson(self, path: Path | str) -> list[RawTrackFeature]:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        out: list[RawTrackFeature] = []
        for feat in payload.get("features") or []:
            if isinstance(feat, Mapping):
                parsed = parse_tas_track_feature(feat)
                if parsed is not None:
                    out.append(parsed)
        return out

    def fetch_tracks(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawTrackFeature]:
        """Fetch Tasmania fire trails, 4WD tracks, and walking tracks from OpenDataWFS/42."""
        if not is_in_tas_coverage(bbox):
            return []

        tracks: list[RawTrackFeature] = []
        seen: set[tuple[str, str]] = set()
        for tile in iter_spatial_tiles(bbox.round_enclosing(), max_deg=tile_deg):
            bhash = tile.cache_hash()
            feats = query_arcgis_geojson(
                [f"{self.opendata_base_url}/42/query"],
                tile,
                cache_path=self._cache_file(f"segments_{bhash}"),
                where=(
                    "TRANS_TYPE = 'Track' OR TSEG_FEAT = 'Fire Trail' "
                    "OR TRAN_CLASS IN ('Vehicular Track', 'Walking') "
                    "OR TRAN_CLASS LIKE 'AS2156%' OR SURFACE_TY = '4WD required'"
                ),
                out_fields="OBJECTID,TRANS_TYPE,TSEG_FEAT,TRAN_CLASS,SURFACE_TY,PRI_NAME,SEC_NAME",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in feats:
                parsed = parse_tas_track_feature(feat)
                if parsed is not None and (parsed.source_layer, parsed.source_id) not in seen:
                    seen.add((parsed.source_layer, parsed.source_id))
                    tracks.append(parsed)
        return tracks

    def fetch_pois(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawPoiFeature]:
        """Fetch Tasmania alpine huts, campgrounds, lookouts, picnic sites, and state trigs."""
        if not is_in_tas_coverage(bbox):
            return []

        raw_pois: list[RawPoiFeature] = []
        for tile in iter_spatial_tiles(bbox.round_enclosing(), max_deg=tile_deg):
            bhash = tile.cache_hash()
            huts = query_arcgis_geojson(
                [f"{self.opendata_base_url}/12/query"],
                tile,
                cache_path=self._cache_file(f"huts_{bhash}"),
                where="BUILD_TY = 'Hut' OR BLD_PUR IN ('Walkers', 'Shelter')",
                out_fields="OBJECTID,BUILD_TY,BLD_PUR,BUILD_NAME",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in huts:
                pt = _coerce_point_geometry(feat.get("geometry"))
                if pt is None:
                    continue
                props = dict(feat.get("properties") or {})
                fid = str(
                    prop_ci(props, "OBJECTID", "id")
                    or feat.get("id")
                    or deterministic_feature_id("tas_hut", pt, props)
                )
                raw_pois.append(
                    RawPoiFeature(
                        source_provider="tas_hut",
                        source_license="CC-BY-3.0-AU",
                        source_feature_id=fid,
                        name=_clean_au_name(prop_ci(props, "BUILD_NAME", "name")),
                        poi_type="hut",
                        state="TAS",
                        raw_props=props,
                        geometry=pt,
                    )
                )

            nom_feats = query_arcgis_geojson(
                [f"{self.opendata_base_url}/34/query"],
                tile,
                cache_path=self._cache_file(f"nomenclature_{bhash}"),
                where="FEAT_TYPE IN ('Camping Ground', 'Lookout', 'Picnic Area')",
                out_fields="OBJECTID,FEAT_TYPE,FEAT_NAME",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in nom_feats:
                pt = _coerce_point_geometry(feat.get("geometry"))
                if pt is None:
                    continue
                props = dict(feat.get("properties") or {})
                ftype = str(prop_ci(props, "FEAT_TYPE") or "").strip().lower()
                poi_type: PoiTypeLiteral | None = None
                if "camp" in ftype:
                    poi_type = "campground"
                elif "lookout" in ftype:
                    poi_type = "lookout"
                elif "picnic" in ftype:
                    poi_type = "picnic"
                if poi_type is None:
                    continue
                fid = str(
                    prop_ci(props, "OBJECTID", "id")
                    or feat.get("id")
                    or deterministic_feature_id("tas_nomenclature", pt, props)
                )
                raw_pois.append(
                    RawPoiFeature(
                        source_provider="tas_nomenclature",
                        source_license="CC-BY-3.0-AU",
                        source_feature_id=fid,
                        name=_clean_au_name(prop_ci(props, "FEAT_NAME", "name")),
                        poi_type=poi_type,
                        state="TAS",
                        raw_props=props,
                        geometry=pt,
                    )
                )

            infra_feats = query_arcgis_geojson(
                [f"{self.topo_base_url}/69/query"],
                tile,
                cache_path=self._cache_file(f"infra_{bhash}"),
                where="INFTY2_TXT IN ('Camping', 'Lookout')",
                out_fields="OBJECTID,INFTY2_TXT,NAME,DESCRIPT",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in infra_feats:
                pt = _coerce_point_geometry(feat.get("geometry"))
                if pt is None:
                    continue
                props = dict(feat.get("properties") or {})
                itype = str(prop_ci(props, "INFTY2_TXT") or "").strip().lower()
                poi_type = (
                    "campground" if "camp" in itype else ("lookout" if "lookout" in itype else None)
                )
                if poi_type is None:
                    continue
                fid = str(
                    prop_ci(props, "OBJECTID", "id")
                    or feat.get("id")
                    or deterministic_feature_id("tas_infra", pt, props)
                )
                raw_pois.append(
                    RawPoiFeature(
                        source_provider="tas_infra",
                        source_license="CC-BY-3.0-AU",
                        source_feature_id=fid,
                        name=_clean_au_name(prop_ci(props, "NAME", "DESCRIPT")),
                        poi_type=poi_type,
                        state="TAS",
                        raw_props=props,
                        geometry=pt,
                    )
                )

            picnic_feats = query_arcgis_geojson(
                [f"{self.opendata_base_url}/36/query"],
                tile,
                cache_path=self._cache_file(f"picnic_{bhash}"),
                where="COM_TYPE2 = 'Picnic Area'",
                out_fields="OBJECTID,COM_TYPE2,NAME",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in picnic_feats:
                pt = _coerce_point_geometry(feat.get("geometry"))
                if pt is None:
                    continue
                props = dict(feat.get("properties") or {})
                fid = str(
                    prop_ci(props, "OBJECTID", "id")
                    or feat.get("id")
                    or deterministic_feature_id("tas_picnic", pt, props)
                )
                raw_pois.append(
                    RawPoiFeature(
                        source_provider="tas_picnic",
                        source_license="CC-BY-3.0-AU",
                        source_feature_id=fid,
                        name=_clean_au_name(prop_ci(props, "NAME")),
                        poi_type="picnic",
                        state="TAS",
                        raw_props=props,
                        geometry=pt,
                    )
                )

            trig_feats = query_arcgis_geojson(
                [f"{self.opendata_base_url}/40/query"],
                tile,
                cache_path=self._cache_file(f"trigs_{bhash}"),
                where="PACK_ID LIKE 'ST%'",
                out_fields="OBJECTID,PACK_ID,SCS_NAME,HEIGHT,HGT_DATUM,ORDER_SYMB",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in trig_feats:
                pt = _coerce_point_geometry(feat.get("geometry"))
                if pt is None:
                    continue
                props = dict(feat.get("properties") or {})
                trigname = (
                    _clean_au_name(prop_ci(props, "SCS_NAME", "PACK_ID", "name")) or "Trig Station"
                )
                fid = str(
                    prop_ci(props, "PACK_ID", "OBJECTID", "id")
                    or feat.get("id")
                    or deterministic_feature_id("tas_trig", pt, props)
                )
                raw_pois.append(
                    RawPoiFeature(
                        source_provider="tas_trig",
                        source_license="CC-BY-3.0-AU",
                        source_feature_id=fid,
                        name=trigname,
                        poi_type="trig",
                        state="TAS",
                        elev_m=parse_elevation_m(prop_ci(props, "HEIGHT", "elev_m")),
                        raw_props=props,
                        geometry=pt,
                    )
                )

        return dedupe_poi_features(raw_pois)


# -----------------------------------------------------------------------------
# 3. Queensland (QSpatial / DESI / DNRM — CC BY 4.0)
# -----------------------------------------------------------------------------


def _classify_qld_qpws_trail(
    props: Mapping[str, Any],
    name: str | None,
    *,
    is_walking_layer: bool = False,
) -> tuple[TrailClassLiteral | None, str, bool]:
    subcat = str(prop_ci(props, "subcat") or "").strip().lower()
    assettype = str(prop_ci(props, "assettype") or "").strip().lower()
    if (
        subcat in ("access structures", "track structures")
        or "bollard" in assettype
        or "barrier" in assettype
    ):
        return None, "", False

    lower_name = (name or "").lower()
    comments = str(prop_ci(props, "comments") or "").strip().lower()
    fireline = str(prop_ci(props, "fireline") or "").strip().lower()
    access = str(prop_ci(props, "access_") or "").strip().lower()
    traffic = str(prop_ci(props, "traffic") or "").strip().lower()
    material = str(prop_ci(props, "material") or "").strip().lower()

    if (
        (fireline and fireline not in ("not used for fire", "no", "none", "unclassified"))
        or "firebreak" in lower_name
        or "fire trail" in lower_name
        or "fireline" in lower_name
        or "firebreak" in comments
    ):
        trail_class: TrailClassLiteral = "fire_trail"
    elif (
        is_walking_layer
        or "tracks and trails" in subcat
        or "walking" in traffic
        or "walking" in assettype
    ):
        trail_class = "walking_track"
    elif "management" in access or "management" in lower_name:
        trail_class = "management_trail"
    elif "4wd" in traffic or subcat == "roads":
        trail_class = "4wd_track"
    else:
        trail_class = "walking_track"

    if "sealed" in material or "paved" in material or "bitumen" in material:
        surface = "sealed"
    elif "natural" in material:
        surface = "natural"
    elif "gravel" in material or "unsealed" in material or "formed" in material:
        surface = "unsealed"
    else:
        surface = "natural" if trail_class == "walking_track" else "unsealed"

    return trail_class, surface, ("4wd" in traffic)


def parse_qld_qpws_feature(
    feat: Mapping[str, Any],
    *,
    source_layer: str,
    is_walking_layer: bool = False,
) -> RawTrackFeature | None:
    geom = _coerce_line_geometry(feat.get("geometry"))
    if geom is None:
        return None
    props = dict(feat.get("properties") or {})
    name = _clean_au_name(prop_ci(props, "name", "visitor_id"))
    trail_class, surface_raw, is_4wd_only = _classify_qld_qpws_trail(
        props, name, is_walking_layer=is_walking_layer
    )
    if trail_class is None:
        return None

    access_raw = str(prop_ci(props, "access_") or "").strip() or None
    access_status = "restricted" if access_raw and "management" in access_raw.lower() else "public"
    feat_id = str(
        prop_ci(props, "objectid", "id")
        or feat.get("id")
        or deterministic_feature_id(source_layer, geom, props)
    ).strip()

    return RawTrackFeature(
        source="qld",
        source_layer=source_layer,
        source_id=feat_id,
        name=name,
        trail_class=trail_class,
        is_vehicular=(trail_class != "walking_track"),
        surface_class=normalize_surface_class(surface_raw, trail_class=trail_class),
        surface_raw=surface_raw,
        is_4wd_only=is_4wd_only,
        access_status=access_status,
        access_raw=access_raw,
        geometry=geom,
        raw_props=props,
    )


def parse_qld_state_road_feature(feat: Mapping[str, Any]) -> RawTrackFeature | None:
    geom = _coerce_line_geometry(feat.get("geometry"))
    if geom is None:
        return None
    props = dict(feat.get("properties") or {})
    name = _clean_au_name(prop_ci(props, "road_name_full", "name"))
    if name and name.lower() == "track":
        name = None
    lower_name = (name or "").lower()
    sub_class = str(prop_ci(props, "sub_class") or "").strip().lower()
    traff = str(prop_ci(props, "trafficability") or "").strip().lower()
    surf_raw = str(prop_ci(props, "surface_type") or "").strip().lower()

    if "firebreak" in lower_name or "fire trail" in lower_name or "fireline" in lower_name:
        trail_class: TrailClassLiteral = "fire_trail"
    elif "non vehicular" in sub_class or "pedestrian" in traff or "bicycle" in traff:
        trail_class = "walking_track"
    elif "management" in lower_name:
        trail_class = "management_trail"
    else:
        trail_class = "4wd_track"

    if "sealed" in surf_raw and "unsealed" not in surf_raw:
        surface = "sealed"
    elif "boardwalk" in surf_raw:
        surface = "boardwalk"
    elif "unsealed" in surf_raw:
        surface = "unsealed"
    else:
        surface = "natural" if trail_class == "walking_track" else "unsealed"

    feat_id = str(
        prop_ci(props, "objectid", "id")
        or feat.get("id")
        or deterministic_feature_id("qld_state_tracks", geom, props)
    ).strip()

    return RawTrackFeature(
        source="qld",
        source_layer="qld_state_tracks",
        source_id=feat_id,
        name=name,
        trail_class=trail_class,
        is_vehicular=(trail_class != "walking_track"),
        surface_class=normalize_surface_class(surface, trail_class=trail_class),
        surface_raw=surface,
        is_4wd_only=("4wd" in traff or "4wd" in sub_class),
        access_status="public",
        access_raw=str(prop_ci(props, "road_owner") or "").strip() or None,
        geometry=geom,
        raw_props=props,
    )


class QLDTrailsConnector:
    """Queensland QSpatial QPWS tracks, RoadsAndTracks, tourist sites & trigs (CC BY 4.0)."""

    name: str = "au-qld-trails"
    state_code: str = "QLD"
    attribution: str = ATTRIBUTION_QLD_QSPATIAL

    def __init__(
        self,
        *,
        parks_base_url: str | None = None,
        roads_url: str | None = None,
        tourist_url: str | None = None,
        survey_url: str | None = None,
        cache_dir: Path | str | None = None,
        timeout_s: float = 25.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.parks_base_url = (
            parks_base_url
            or os.environ.get("TRAILFORGE_AU_QLD_PARKS_URL", DEFAULT_QLD_PARKS_BASE_URL)
        ).rstrip("/")
        self.roads_url = roads_url or os.environ.get(
            "TRAILFORGE_AU_QLD_ROADS_URL", DEFAULT_QLD_ROADS_URL
        )
        self.tourist_url = tourist_url or os.environ.get(
            "TRAILFORGE_AU_QLD_TOURIST_URL", DEFAULT_QLD_TOURIST_URL
        )
        self.survey_url = survey_url or os.environ.get(
            "TRAILFORGE_AU_QLD_SURVEY_URL", DEFAULT_QLD_SURVEY_URL
        )
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.timeout_s = timeout_s
        self._client = client

    def _cache_file(self, key: str) -> Path | None:
        return (self.cache_dir / "au_qld" / f"{key}.json") if self.cache_dir is not None else None

    def load_tracks_geojson(self, path: Path | str) -> list[RawTrackFeature]:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        out: list[RawTrackFeature] = []
        for feat in payload.get("features") or []:
            if isinstance(feat, Mapping):
                parsed = parse_qld_state_road_feature(feat)
                if parsed is not None:
                    out.append(parsed)
        return out

    def fetch_tracks(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawTrackFeature]:
        """Fetch QPWS tracks/firelines (layers 6, 5, 2) and QSpatial state tracks."""
        if not is_in_qld_coverage(bbox):
            return []

        tracks: list[RawTrackFeature] = []
        qpws_by_oid: dict[str, int] = {}
        seen_state: set[str] = set()

        qpws_fields = (
            "objectid,name,visitor_id,subcat,assettype,fireline,traffic,access_,material,comments"
        )
        for tile in iter_spatial_tiles(bbox.round_enclosing(), max_deg=tile_deg):
            bhash = tile.cache_hash()
            qpws_jobs = (
                (f"{self.parks_base_url}/6/query", "qld_qpws_great_walks", True, f"gw_{bhash}"),
                (f"{self.parks_base_url}/5/query", "qld_qpws_walking", True, f"walk_{bhash}"),
                (f"{self.parks_base_url}/2/query", "qld_qpws_park_roads", False, f"prds_{bhash}"),
            )
            for url, layer_name, is_walk, ckey in qpws_jobs:
                feats = query_arcgis_geojson(
                    [url],
                    tile,
                    cache_path=self._cache_file(ckey),
                    out_fields=qpws_fields,
                    timeout_s=self.timeout_s,
                    client=self._client,
                )
                for feat in feats:
                    parsed = parse_qld_qpws_feature(
                        feat, source_layer=layer_name, is_walking_layer=is_walk
                    )
                    if parsed is None:
                        continue
                    oid = str(prop_ci(parsed.raw_props, "objectid") or "")
                    if oid and oid in qpws_by_oid:
                        existing = tracks[qpws_by_oid[oid]]
                        if parsed.trail_class == "fire_trail":
                            tracks[qpws_by_oid[oid]] = existing.model_copy(
                                update={
                                    "trail_class": "fire_trail",
                                    "is_vehicular": True,
                                    "name": existing.name or parsed.name,
                                }
                            )
                        elif not existing.name and parsed.name:
                            tracks[qpws_by_oid[oid]] = existing.model_copy(
                                update={"name": parsed.name}
                            )
                        continue
                    if oid:
                        qpws_by_oid[oid] = len(tracks)
                    tracks.append(parsed)

            state_feats = query_arcgis_geojson(
                [self.roads_url],
                tile,
                cache_path=self._cache_file(f"state_tracks_{bhash}"),
                where="class = 'Track' AND (road_owner IS NULL OR road_owner <> 'QPWS')",
                out_fields="objectid,road_name_full,sub_class,trafficability,surface_type,road_owner",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in state_feats:
                parsed = parse_qld_state_road_feature(feat)
                if parsed is not None and parsed.source_id not in seen_state:
                    seen_state.add(parsed.source_id)
                    tracks.append(parsed)

        return tracks

    def fetch_pois(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawPoiFeature]:
        """Fetch QLD campgrounds, lookouts, picnic sites, huts, and survey control marks."""
        if not is_in_qld_coverage(bbox):
            return []

        raw_pois: list[RawPoiFeature] = []
        for tile in iter_spatial_tiles(bbox.round_enclosing(), max_deg=tile_deg):
            bhash = tile.cache_hash()
            tourist_feats = query_arcgis_geojson(
                [self.tourist_url],
                tile,
                cache_path=self._cache_file(f"tourist_{bhash}"),
                where=(
                    "feature_type IN ('Camping Ground', 'Lookout', 'Picnic Area', 'Hut', 'Shelter')"
                ),
                out_fields="objectid,feature_type,name,alternate_name",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in tourist_feats:
                pt = _coerce_point_geometry(feat.get("geometry"))
                if pt is None:
                    continue
                props = dict(feat.get("properties") or {})
                ftype = str(prop_ci(props, "feature_type") or "").strip().lower()
                poi_type: PoiTypeLiteral | None = None
                if "camp" in ftype:
                    poi_type = "campground"
                elif "lookout" in ftype:
                    poi_type = "lookout"
                elif "picnic" in ftype:
                    poi_type = "picnic"
                elif "hut" in ftype or "shelter" in ftype:
                    poi_type = "hut"
                if poi_type is None:
                    continue
                fid = str(
                    prop_ci(props, "objectid", "id")
                    or feat.get("id")
                    or deterministic_feature_id("qld_tourist", pt, props)
                )
                raw_pois.append(
                    RawPoiFeature(
                        source_provider="qld_tourist",
                        source_license="CC-BY-4.0",
                        source_feature_id=fid,
                        name=_clean_au_name(prop_ci(props, "name", "alternate_name")),
                        poi_type=poi_type,
                        state="QLD",
                        raw_props=props,
                        geometry=pt,
                    )
                )

            survey_feats = query_arcgis_geojson(
                [self.survey_url],
                tile,
                cache_path=self._cache_file(f"survey_{bhash}"),
                where=(
                    "ahdfix_de = 'Trig' OR UPPER(mrktype_de) LIKE '%TRIG%' "
                    "OR UPPER(mrktype_de) LIKE '%BEACON%' OR UPPER(mrktype_de) LIKE '%PILLAR%'"
                ),
                out_fields="objectid,mrk_id,alt1_nm,locality_de,mrktype_de,ahdfix_de,ahdheight,mrkcnd_de",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            if not survey_feats:
                survey_feats = query_arcgis_geojson(
                    [self.survey_url],
                    tile,
                    cache_path=self._cache_file(f"survey_named_{bhash}"),
                    where="alt1_nm IS NOT NULL AND ahdheight IS NOT NULL AND mrkcnd_de = 'Good'",
                    out_fields=(
                        "objectid,mrk_id,alt1_nm,locality_de,mrktype_de,"
                        "ahdfix_de,ahdheight,mrkcnd_de"
                    ),
                    page_size=25,
                    max_pages=1,
                    timeout_s=self.timeout_s,
                    client=self._client,
                )
            for feat in survey_feats:
                pt = _coerce_point_geometry(feat.get("geometry"))
                if pt is None:
                    continue
                props = dict(feat.get("properties") or {})
                mrk_id = prop_ci(props, "mrk_id")
                name = _clean_au_name(prop_ci(props, "alt1_nm", "locality_de"))
                if not name:
                    name = f"PM {mrk_id}" if mrk_id is not None else "Trig Station"
                fid = str(
                    mrk_id
                    or prop_ci(props, "objectid", "id")
                    or feat.get("id")
                    or deterministic_feature_id("qld_survey", pt, props)
                )
                raw_pois.append(
                    RawPoiFeature(
                        source_provider="qld_survey",
                        source_license="CC-BY-4.0",
                        source_feature_id=fid,
                        name=name,
                        poi_type="trig",
                        state="QLD",
                        elev_m=parse_elevation_m(prop_ci(props, "ahdheight", "elev_m")),
                        raw_props=props,
                        geometry=pt,
                    )
                )

        return dedupe_poi_features(raw_pois)


# -----------------------------------------------------------------------------
# 4. Western Australia (DBCA AGOL & Landgate SLIP — CC BY 4.0, zero CC BY-NC!)
# -----------------------------------------------------------------------------


def _classify_wa_ratis_site(props: Mapping[str, Any]) -> tuple[PoiTypeLiteral | None, str]:
    status = str(prop_ci(props, "RTS_STATUS") or "OPEN").strip().upper()
    if status and status != "OPEN":
        return (None, "")
    func_str = str(prop_ci(props, "RTS_PRIMARY_FUNCTION") or "").strip().lower()
    if func_str == "management site only":
        return (None, "")
    name = _clean_au_name(prop_ci(props, "RTS_NAME", "name"))
    if not name:
        return (None, "")
    low = name.lower()
    if "camp" in func_str or "camp" in low or "retreat" in low or "accommodation" in func_str:
        return ("campground", name)
    if _HUT_WORD_RE.search(name):
        return ("hut", name)
    if (
        "picnic" in low
        or "day use" in low
        or "car park" in low
        or "trail head" in low
        or "trailhead" in low
    ):
        return ("picnic", name)
    if any(k in low for k in ("lookout", "falls", "gorge", "summit", "bluff", "ledge", "peak")):
        return ("lookout", name)
    if any(k in low for k in ("section ", " road", " drive", "entry station", "water tank")):
        return (None, "")
    if "day use" in func_str:
        return ("picnic", name)
    return (None, "")


def parse_wa_track_feature(
    feat: Mapping[str, Any],
    *,
    source_layer: str,
) -> RawTrackFeature | None:
    """Normalize a WA DBCA or Landgate SLIP track feature, strictly dropping CC BY-NC records."""
    geom = _coerce_line_geometry(feat.get("geometry"))
    if geom is None:
        return None
    props = dict(feat.get("properties") or {})
    if is_wa_non_commercial_restricted(props):
        logger.warning("Excluding non-commercial (CC BY-NC / DBCA-027) WA track record")
        return None

    if source_layer == "wa_dbca_tracks":
        name = _clean_au_name(prop_ci(props, "TRAIL_NAME", "TYPE", "name"))
        low = f"{name or ''} {prop_ci(props, 'TYPE') or ''}".lower()
        if "fire" in low or "firebreak" in low or "fire break" in low:
            trail_class: TrailClassLiteral = "fire_trail"
        elif "management" in low:
            trail_class = "management_trail"
        elif "4wd" in low or "drive" in low or "vehicle" in low:
            trail_class = "4wd_track"
        else:
            trail_class = "walking_track"
        surface_raw = "natural" if trail_class == "walking_track" else "unsealed"
    else:
        name = _clean_au_name(prop_ci(props, "road_name", "name"))
        low = (name or "").lower()
        if "fire" in low or "firebreak" in low or "fire break" in low:
            trail_class = "fire_trail"
        elif "management" in low:
            trail_class = "management_trail"
        else:
            trail_class = "4wd_track"
        raw_surf = str(prop_ci(props, "roadsurface") or "").strip().lower()
        surface_raw = raw_surf if raw_surf and raw_surf not in ("unknown", "none") else "unsealed"

    feat_id = str(
        prop_ci(props, "OBJECTID", "objectid", "id")
        or feat.get("id")
        or deterministic_feature_id(source_layer, geom, props)
    ).strip()

    return RawTrackFeature(
        source="wa",
        source_layer=source_layer,
        source_id=feat_id,
        name=name,
        trail_class=trail_class,
        is_vehicular=(trail_class != "walking_track"),
        surface_class=normalize_surface_class(surface_raw, trail_class=trail_class),
        surface_raw=surface_raw,
        access_status="public",
        access_raw=str(prop_ci(props, "mapclassification", "fcsubtype", "TYPE") or "").strip()
        or None,
        geometry=geom,
        raw_props=props,
    )


class WATrailsConnector:
    """WA DBCA AGOL & Landgate SLIP tracks, fire breaks, RATIS, fire towers & geodetic marks.

    Strictly commercial-friendly ``CC BY 4.0`` only: intentionally excludes ``DBCA-027``
    and filters out any record carrying ``CC BY-NC`` / non-commercial restrictions.
    """

    name: str = "au-wa-trails"
    state_code: str = "WA"
    attribution: str = ATTRIBUTION_WA_DBCA_SLIP

    def __init__(
        self,
        *,
        tracks_url: str | None = None,
        slip_roads_url: str | None = None,
        ratis_sites_url: str | None = None,
        ratis_facilities_url: str | None = None,
        fire_towers_url: str | None = None,
        geodetic_url: str | None = None,
        cache_dir: Path | str | None = None,
        timeout_s: float = 25.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.tracks_url = tracks_url or os.environ.get(
            "TRAILFORGE_AU_WA_TRACKS_URL", DEFAULT_WA_TRACKS_URL
        )
        self.slip_roads_url = slip_roads_url or os.environ.get(
            "TRAILFORGE_AU_WA_SLIP_ROADS_URL", DEFAULT_WA_SLIP_ROADS_URL
        )
        self.ratis_sites_url = ratis_sites_url or os.environ.get(
            "TRAILFORGE_AU_WA_RATIS_SITES_URL", DEFAULT_WA_RATIS_SITES_URL
        )
        self.ratis_facilities_url = ratis_facilities_url or os.environ.get(
            "TRAILFORGE_AU_WA_RATIS_FACILITIES_URL", DEFAULT_WA_RATIS_FACILITIES_URL
        )
        self.fire_towers_url = fire_towers_url or os.environ.get(
            "TRAILFORGE_AU_WA_FIRE_TOWERS_URL", DEFAULT_WA_FIRE_TOWERS_URL
        )
        self.geodetic_url = geodetic_url or os.environ.get(
            "TRAILFORGE_AU_WA_GEODETIC_URL", DEFAULT_WA_GEODETIC_URL
        )
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.timeout_s = timeout_s
        self._client = client
        self.excluded_nc_count: int = 0

    def _cache_file(self, key: str) -> Path | None:
        return (self.cache_dir / "au_wa" / f"{key}.json") if self.cache_dir is not None else None

    def load_tracks_geojson(
        self, path: Path | str, *, source_layer: str = "wa_slip_tracks"
    ) -> list[RawTrackFeature]:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        out: list[RawTrackFeature] = []
        for feat in payload.get("features") or []:
            if not isinstance(feat, Mapping):
                continue
            props = feat.get("properties") or {}
            if is_wa_non_commercial_restricted(props):
                self.excluded_nc_count += 1
                continue
            parsed = parse_wa_track_feature(feat, source_layer=source_layer)
            if parsed is not None:
                out.append(parsed)
        return out

    def fetch_tracks(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawTrackFeature]:
        """Fetch WA DBCA trails and Landgate SLIP tracks/fire breaks (strictly zero CC BY-NC)."""
        if not is_in_wa_coverage(bbox):
            return []

        tracks: list[RawTrackFeature] = []
        seen: set[tuple[str, str]] = set()
        for tile in iter_spatial_tiles(bbox.round_enclosing(), max_deg=tile_deg):
            bhash = tile.cache_hash()
            layer_jobs = (
                (
                    self.tracks_url,
                    "wa_dbca_tracks",
                    "1=1",
                    "OBJECTID,TYPE",
                    f"dbca_tracks_{bhash}",
                ),
                (
                    self.slip_roads_url,
                    "wa_slip_tracks",
                    "mapclassification = 'Track' OR fcsubtype = 'Track'",
                    "OBJECTID,road_name,fcsubtype,mapclassification,roadsurface",
                    f"slip_tracks_{bhash}",
                ),
            )
            for url, layer_name, where, out_fields, ckey in layer_jobs:
                if is_wa_non_commercial_restricted(source_url=url):
                    logger.warning("Refusing non-commercial WA endpoint: %s", url)
                    self.excluded_nc_count += 1
                    continue
                feats = query_arcgis_geojson(
                    [url],
                    tile,
                    cache_path=self._cache_file(ckey),
                    where=where,
                    out_fields=out_fields,
                    timeout_s=self.timeout_s,
                    client=self._client,
                )
                for feat in feats:
                    props = feat.get("properties") or {}
                    if is_wa_non_commercial_restricted(props):
                        self.excluded_nc_count += 1
                        continue
                    parsed = parse_wa_track_feature(feat, source_layer=layer_name)
                    if parsed is not None and (parsed.source_layer, parsed.source_id) not in seen:
                        seen.add((parsed.source_layer, parsed.source_id))
                        tracks.append(parsed)
        return tracks

    def fetch_pois(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawPoiFeature]:
        """Fetch WA RATIS sites/facilities, DBCA-061 fire towers, and LGATE-076 geodetic marks."""
        if not is_in_wa_coverage(bbox):
            return []

        raw_pois: list[RawPoiFeature] = []
        for tile in iter_spatial_tiles(bbox.round_enclosing(), max_deg=tile_deg):
            bhash = tile.cache_hash()

            if not is_wa_non_commercial_restricted(source_url=self.ratis_sites_url):
                sites = query_arcgis_geojson(
                    [self.ratis_sites_url],
                    tile,
                    cache_path=self._cache_file(f"ratis_sites_{bhash}"),
                    out_fields="OBJECTID,RTS_NAME,RTS_PRIMARY_FUNCTION,RTS_STATUS",
                    timeout_s=self.timeout_s,
                    client=self._client,
                )
                for feat in sites:
                    pt = _coerce_point_geometry(feat.get("geometry"))
                    if pt is None:
                        continue
                    props = dict(feat.get("properties") or {})
                    if is_wa_non_commercial_restricted(props):
                        self.excluded_nc_count += 1
                        continue
                    poi_type, label = _classify_wa_ratis_site(props)
                    if poi_type is None:
                        continue
                    fid = str(
                        prop_ci(props, "OBJECTID", "id")
                        or feat.get("id")
                        or deterministic_feature_id("wa_ratis_site", pt, props)
                    )
                    raw_pois.append(
                        RawPoiFeature(
                            source_provider="wa_ratis_site",
                            source_license="CC-BY-4.0",
                            source_feature_id=fid,
                            name=label,
                            poi_type=poi_type,
                            state="WA",
                            raw_props=props,
                            geometry=pt,
                        )
                    )

            if not is_wa_non_commercial_restricted(source_url=self.ratis_facilities_url):
                facs = query_arcgis_geojson(
                    [self.ratis_facilities_url],
                    tile,
                    cache_path=self._cache_file(f"ratis_facs_{bhash}"),
                    where="RTT_SUB_TYPE_DESC IN ('Camp Shelter','Lookout/Deck')",
                    out_fields=(
                        "OBJECTID,RTT_FAC_TYPE,RTT_SUB_TYPE_DESC,"
                        "RTT_ASSET_DESC,RTT_ASSET_STATUS"
                    ),
                    timeout_s=self.timeout_s,
                    client=self._client,
                )
                for feat in facs:
                    pt = _coerce_point_geometry(feat.get("geometry"))
                    if pt is None:
                        continue
                    props = dict(feat.get("properties") or {})
                    if is_wa_non_commercial_restricted(props):
                        self.excluded_nc_count += 1
                        continue
                    asset_status = str(prop_ci(props, "RTT_ASSET_STATUS") or "").strip().lower()
                    if any(bad in asset_status for bad in ("closed", "removed", "decommissioned")):
                        continue
                    subtype = str(prop_ci(props, "RTT_SUB_TYPE_DESC") or "").strip().lower()
                    desc = _clean_au_name(prop_ci(props, "RTT_ASSET_DESC"))
                    if "camp shelter" in subtype:
                        poi_type = "hut"
                        label = desc[:48] if desc else "Camp Shelter"
                    elif "lookout" in subtype:
                        poi_type = "lookout"
                        label = desc[:48] if desc else "Lookout"
                    else:
                        continue
                    fid = str(
                        prop_ci(props, "OBJECTID", "id")
                        or feat.get("id")
                        or deterministic_feature_id("wa_ratis_facility", pt, props)
                    )
                    raw_pois.append(
                        RawPoiFeature(
                            source_provider="wa_ratis_facility",
                            source_license="CC-BY-4.0",
                            source_feature_id=fid,
                            name=label,
                            poi_type=poi_type,
                            state="WA",
                            raw_props=props,
                            geometry=pt,
                        )
                    )

            if not is_wa_non_commercial_restricted(source_url=self.fire_towers_url):
                towers = query_arcgis_geojson(
                    [self.fire_towers_url],
                    tile,
                    cache_path=self._cache_file(f"fire_towers_{bhash}"),
                    out_fields="OBJECTID,ftw_name,ftw_hist_name,ftw_type,ftw_status",
                    timeout_s=self.timeout_s,
                    client=self._client,
                )
                for feat in towers:
                    pt = _coerce_point_geometry(feat.get("geometry"))
                    if pt is None:
                        continue
                    props = dict(feat.get("properties") or {})
                    if is_wa_non_commercial_restricted(props):
                        self.excluded_nc_count += 1
                        continue
                    status = str(prop_ci(props, "ftw_status") or "").strip().lower()
                    if "removed" in status or "demolished" in status:
                        continue
                    label = (
                        _clean_au_name(prop_ci(props, "ftw_name"))
                        or _clean_au_name(prop_ci(props, "ftw_hist_name"))
                        or "Fire Tower"
                    )
                    fid = str(
                        prop_ci(props, "OBJECTID", "id")
                        or feat.get("id")
                        or deterministic_feature_id("wa_fire_tower", pt, props)
                    )
                    raw_pois.append(
                        RawPoiFeature(
                            source_provider="wa_fire_tower",
                            source_license="CC-BY-4.0",
                            source_feature_id=fid,
                            name=label,
                            poi_type="lookout",
                            state="WA",
                            raw_props=props,
                            geometry=pt,
                        )
                    )

            if not is_wa_non_commercial_restricted(source_url=self.geodetic_url):
                geodetic = query_arcgis_geojson(
                    [self.geodetic_url],
                    tile,
                    cache_path=self._cache_file(f"geodetic_primary_{bhash}"),
                    where=(
                        "reduced_level IS NOT NULL AND "
                        "(render_value LIKE 'S0%' OR render_value LIKE 'S1%' "
                        "OR render_value LIKE 'SD%')"
                    ),
                    out_fields=(
                        "OBJECTID,geodetic_point_name,stamped_name_on_point,point_type,"
                        "render_value,filter_value,reduced_level,vert_datum,"
                        "latest_status_description"
                    ),
                    timeout_s=self.timeout_s,
                    client=self._client,
                )
                if not geodetic:
                    geodetic = query_arcgis_geojson(
                        [self.geodetic_url],
                        tile,
                        cache_path=self._cache_file(f"geodetic_outback_{bhash}"),
                        where="reduced_level IS NOT NULL AND filter_value = 2",
                        out_fields=(
                            "OBJECTID,geodetic_point_name,stamped_name_on_point,point_type,"
                            "render_value,filter_value,reduced_level,vert_datum,"
                            "latest_status_description"
                        ),
                        page_size=50,
                        max_pages=1,
                        timeout_s=self.timeout_s,
                        client=self._client,
                    )
                for feat in geodetic:
                    pt = _coerce_point_geometry(feat.get("geometry"))
                    if pt is None:
                        continue
                    props = dict(feat.get("properties") or {})
                    if is_wa_non_commercial_restricted(props):
                        self.excluded_nc_count += 1
                        continue
                    status = str(prop_ci(props, "latest_status_description") or "").strip().upper()
                    if any(bad in status for bad in ("DESTROYED", "NOT FOUND", "REMOVED")):
                        continue
                    elev_m = parse_elevation_m(prop_ci(props, "reduced_level", "elev_m"))
                    if elev_m is None:
                        continue
                    label = (
                        _clean_au_name(prop_ci(props, "geodetic_point_name"))
                        or _clean_au_name(prop_ci(props, "stamped_name_on_point"))
                        or "Geodetic Mark"
                    )
                    fid = str(
                        prop_ci(props, "OBJECTID", "geodetic_point_name", "id")
                        or feat.get("id")
                        or deterministic_feature_id("wa_geodetic", pt, props)
                    )
                    raw_pois.append(
                        RawPoiFeature(
                            source_provider="wa_geodetic",
                            source_license="CC-BY-4.0",
                            source_feature_id=fid,
                            name=label,
                            poi_type="trig",
                            state="WA",
                            elev_m=elev_m,
                            raw_props=props,
                            geometry=pt,
                        )
                    )

        return dedupe_poi_features(raw_pois)


# -----------------------------------------------------------------------------
# 5. South Australia (DEW / DHUD / Location SA + GA Topo250K — CC BY 4.0)
# -----------------------------------------------------------------------------


def _parse_sa_recreation_trail(
    geom: Mapping[str, Any], props: Mapping[str, Any]
) -> RawTrackFeature | None:
    gj = _coerce_line_geometry(geom)
    if gj is None:
        return None
    status = str(prop_ci(props, "TRAILSTATU", "trailstatus") or "").strip().lower()
    if status in ("closed", "proposed", "retired"):
        return None

    name = _clean_au_name(prop_ci(props, "TRAILNAME", "trailname", "name"))
    trail_type = str(prop_ci(props, "TRAILTYPE", "trailtype") or "").strip().lower()
    lower_name = (name or "").lower()
    if "fire" in lower_name:
        trail_class: TrailClassLiteral = "fire_trail"
    elif "4wd" in lower_name or "4wd" in trail_type or "vehicle" in trail_type:
        trail_class = "4wd_track"
    else:
        trail_class = "walking_track"

    surface_raw = (
        "natural" if "walk" in trail_type or trail_class == "walking_track" else "unsealed"
    )
    feat_id = str(
        prop_ci(props, "OBJECTID", "TRAIL_ID", "id")
        or deterministic_feature_id("sa_recreation_trails", gj, props)
    ).strip()

    return RawTrackFeature(
        source="sa",
        source_layer="sa_recreation_trails",
        source_id=feat_id,
        name=name,
        trail_class=trail_class,
        is_vehicular=(trail_class != "walking_track"),
        surface_class=normalize_surface_class(surface_raw, trail_class=trail_class),
        surface_raw=surface_raw,
        access_status="public",
        access_raw=status or None,
        geometry=gj,
        raw_props=dict(props),
    )


def read_sa_trails_from_zip(
    zip_path: Path,
    bbox: BBox,
    *,
    preferred_inner: str = DEFAULT_SA_TRAILS_ZIP_INNER,
) -> list[RawTrackFeature]:
    """Low-RAM spatial filter of SA Recreation Trails ZIP via GDAL /vsizip/ (or Python fallback)."""
    resolved = zip_path.resolve()
    if not resolved.is_file():
        return []

    inner_name: str | None = None
    try:
        with zipfile.ZipFile(resolved, "r") as zf:
            names = zf.namelist()
            if preferred_inner in names:
                inner_name = preferred_inner
            else:
                for candidate in names:
                    low = candidate.lower()
                    if low.endswith(".geojson") or low.endswith(".json"):
                        inner_name = candidate
                        break
    except zipfile.BadZipFile as exc:
        logger.warning("Corrupt SA trails archive %s (%s); skipping", resolved, exc)
        return []

    if not inner_name:
        return []

    # Fast, low-RAM streaming path via GDAL /vsizip/ OGR spatial filter when osgeo is installed
    try:
        from osgeo import ogr  # type: ignore[import-untyped]

        vsi_path = f"/vsizip/{resolved}/{inner_name}"
        ogr.UseExceptions()
        ds = ogr.Open(vsi_path)
        if ds is not None:
            out: list[RawTrackFeature] = []
            try:
                layer = ds.GetLayer(0)
                if layer is None:
                    return []
                layer.SetSpatialFilterRect(bbox.min_x, bbox.min_y, bbox.max_x, bbox.max_y)
                for feat in layer:
                    geom_ref = feat.GetGeometryRef()
                    if geom_ref is None or geom_ref.IsEmpty():
                        continue
                    gj_geom = json.loads(geom_ref.ExportToJson())
                    parsed = _parse_sa_recreation_trail(gj_geom, feat.items())
                    if parsed is not None:
                        out.append(parsed)
                return out
            finally:
                ds = None
    except ImportError:
        pass
    except Exception as exc:
        logger.warning(
            "GDAL /vsizip/ read failed for %s (%s); falling back to zip JSON", resolved, exc
        )

    # Pure-Python fallback (used on host environments without osgeo bindings)
    out_py: list[RawTrackFeature] = []
    try:
        with zipfile.ZipFile(resolved, "r") as zf, zf.open(inner_name) as fh:
            payload = json.load(fh)
    except Exception as exc:
        logger.warning("Failed reading SA trails zip %s (%s)", resolved, exc)
        return []

    for feat in payload.get("features") or []:
        if not isinstance(feat, Mapping):
            continue
        geom = feat.get("geometry")
        if not isinstance(geom, Mapping) or not _geom_intersects_bbox(geom, bbox):
            continue
        parsed = _parse_sa_recreation_trail(geom, feat.get("properties") or {})
        if parsed is not None:
            out_py.append(parsed)
    return out_py


class SATrailsConnector:
    """South Australia DEW / DHUD / Location SA + GA Topo250K trails & marks (CC BY 4.0)."""

    name: str = "au-sa-trails"
    state_code: str = "SA"
    attribution: str = ATTRIBUTION_SA_DEW_DHUD

    def __init__(
        self,
        *,
        trails_zip_url: str | None = None,
        trails_zip_path: Path | str | None = None,
        fire_tracks_url: str | None = None,
        ga_tracks_url: str | None = None,
        parks_json_url: str | None = None,
        survey_marks_url: str | None = None,
        cache_dir: Path | str | None = None,
        timeout_s: float = 25.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.trails_zip_url = trails_zip_url or os.environ.get(
            "TRAILFORGE_AU_SA_TRAILS_ZIP_URL", DEFAULT_SA_TRAILS_ZIP_URL
        )
        self.trails_zip_path = Path(trails_zip_path) if trails_zip_path is not None else None
        self.fire_tracks_url = fire_tracks_url or os.environ.get(
            "TRAILFORGE_AU_SA_FIRE_TRACKS_URL", DEFAULT_SA_FIRE_TRACKS_URL
        )
        self.ga_tracks_url = ga_tracks_url or os.environ.get(
            "TRAILFORGE_AU_GA_TOPO_ROADS_URL", DEFAULT_GA_TOPO250K_ROADS_URL
        )
        self.parks_json_url = parks_json_url or os.environ.get(
            "TRAILFORGE_AU_SA_PARKS_JSON_URL", DEFAULT_SA_PARKS_JSON_URL
        )
        self.survey_marks_url = survey_marks_url or os.environ.get(
            "TRAILFORGE_AU_SA_SURVEY_MARKS_URL", DEFAULT_SA_SURVEY_MARKS_URL
        )
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.timeout_s = timeout_s
        self._client = client

    def _cache_file(self, key: str) -> Path | None:
        return (self.cache_dir / "au_sa" / key) if self.cache_dir is not None else None

    def fetch_tracks(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawTrackFeature]:
        """Fetch SA recreation trails (/vsizip/), DEW fire access tracks, and GA Topo250K tracks."""
        if not is_in_sa_coverage(bbox):
            return []

        query_bbox = bbox.round_enclosing()
        tracks: list[RawTrackFeature] = []
        seen: set[tuple[str, str]] = set()

        # 1. DEW Recreation Trails ZIP (streamed /vsizip/ spatial filter)
        zip_path = self.trails_zip_path
        if zip_path is None:
            zip_dest = self._cache_file("sa_recreation_trails.zip")
            if zip_dest is not None:
                zip_path = _stream_download_cached_safe(
                    self.trails_zip_url,
                    zip_dest,
                    timeout_s=max(45.0, self.timeout_s),
                    client=self._client,
                )
        if zip_path is not None and zip_path.is_file():
            for parsed in read_sa_trails_from_zip(zip_path, query_bbox):
                key = (parsed.source_layer, parsed.source_id)
                if key not in seen:
                    seen.add(key)
                    tracks.append(parsed)

        for tile in iter_spatial_tiles(query_bbox, max_deg=tile_deg):
            bhash = tile.cache_hash()
            # 2. DEW SA Fire Access Tracks
            fire_feats = query_arcgis_geojson(
                [self.fire_tracks_url],
                tile,
                cache_path=self._cache_file(f"fire_tracks_{bhash}.json"),
                out_fields="OBJECTID,TRACK_NAME,RESERVE_NAME,ASSET_NAME,PROP_FA_CLASS,ACTIVITY",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in fire_feats:
                geom = _coerce_line_geometry(feat.get("geometry"))
                if geom is None:
                    continue
                props = dict(feat.get("properties") or {})
                name = (
                    _clean_au_name(prop_ci(props, "TRACK_NAME"))
                    or _clean_au_name(prop_ci(props, "RESERVE_NAME"))
                    or _clean_au_name(prop_ci(props, "ASSET_NAME"))
                )
                fid = str(
                    prop_ci(props, "OBJECTID", "id")
                    or feat.get("id")
                    or deterministic_feature_id("sa_fire_tracks", geom, props)
                ).strip()
                key = ("sa_fire_tracks", fid)
                if key in seen:
                    continue
                seen.add(key)
                tracks.append(
                    RawTrackFeature(
                        source="sa",
                        source_layer="sa_fire_tracks",
                        source_id=fid,
                        name=name,
                        trail_class="fire_trail",
                        is_vehicular=True,
                        surface_class="dirt_unsealed",
                        surface_raw="unsealed",
                        access_status="public",
                        access_raw=str(prop_ci(props, "PROP_FA_CLASS") or "").strip() or None,
                        geometry=geom,
                        raw_props=props,
                    )
                )

            # 3. GA Topo250K Outback 4WD / Bush Tracks
            ga_feats = query_arcgis_geojson(
                [self.ga_tracks_url],
                tile,
                cache_path=self._cache_file(f"ga_tracks_{bhash}.json"),
                where="class IN ('Track','Vehicular Track','Foot Track')",
                out_fields="OBJECTID,name,class,formation",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in ga_feats:
                geom = _coerce_line_geometry(feat.get("geometry"))
                if geom is None:
                    continue
                props = dict(feat.get("properties") or {})
                name = _clean_au_name(prop_ci(props, "name"))
                cls = str(prop_ci(props, "class") or "").strip().lower()
                formation = str(prop_ci(props, "formation") or "").strip().lower()
                trail_class: TrailClassLiteral = "walking_track" if "foot" in cls else "4wd_track"
                surface_raw = formation if formation and formation != "unknown" else "unsealed"
                fid = str(
                    prop_ci(props, "OBJECTID", "id")
                    or feat.get("id")
                    or deterministic_feature_id("sa_ga_topo250k", geom, props)
                ).strip()
                key = ("sa_ga_topo250k", fid)
                if key in seen:
                    continue
                seen.add(key)
                tracks.append(
                    RawTrackFeature(
                        source="sa",
                        source_layer="sa_ga_topo250k",
                        source_id=fid,
                        name=name,
                        trail_class=trail_class,
                        is_vehicular=(trail_class != "walking_track"),
                        surface_class=normalize_surface_class(surface_raw, trail_class=trail_class),
                        surface_raw=surface_raw,
                        access_status="public",
                        geometry=geom,
                        raw_props=props,
                    )
                )

        return tracks

    def _load_sa_parks_pois(self, bbox: BBox) -> list[RawPoiFeature]:
        parks_dest = self._cache_file("sa_parks_data.json")
        if parks_dest is None:
            return []
        cached_path = _stream_download_cached_safe(
            self.parks_json_url,
            parks_dest,
            chunk_bytes=262_144,
            timeout_s=self.timeout_s,
            client=self._client,
        )
        if cached_path is None or not cached_path.is_file():
            return []
        try:
            parks = json.loads(cached_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed parsing SA parks-data.json (%s); skipping", exc)
            return []
        if not isinstance(parks, list):
            return []

        out: list[RawPoiFeature] = []
        for entry in parks:
            if not isinstance(entry, Mapping):
                continue
            lat = to_float_or_none(entry.get("lat"))
            lon = to_float_or_none(entry.get("lng") if "lng" in entry else entry.get("lon"))
            if lat is None or lon is None:
                continue
            if not (bbox.min_x <= lon <= bbox.max_x and bbox.min_y <= lat <= bbox.max_y):
                continue
            name = _clean_au_name(entry.get("name"))
            if not name:
                continue
            camping = entry.get("campingAndAccommodation") or {}
            has_camp = isinstance(camping, Mapping) and any(bool(v) for v in camping.values())
            low = name.lower()
            if has_camp:
                poi_type: PoiTypeLiteral = "campground"
            elif any(k in low for k in ("summit", "lookout", "falls", "gorge", "gully", "cliff")):
                poi_type = "lookout"
            else:
                poi_type = "picnic"
            pt = {"type": "Point", "coordinates": [lon, lat]}
            fid = str(entry.get("id") or deterministic_feature_id("sa_parks", pt, entry))
            out.append(
                RawPoiFeature(
                    source_provider="sa_parks",
                    source_license="CC-BY-4.0",
                    source_feature_id=fid,
                    name=name,
                    poi_type=poi_type,
                    state="SA",
                    raw_props=dict(entry),
                    geometry=pt,
                )
            )
        return out

    def fetch_pois(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawPoiFeature]:
        """Fetch SA parks visitor sites and Location SA AHD survey marks."""
        if not is_in_sa_coverage(bbox):
            return []

        query_bbox = bbox.round_enclosing()
        raw_pois = self._load_sa_parks_pois(query_bbox)

        for tile in iter_spatial_tiles(query_bbox, max_deg=tile_deg):
            bhash = tile.cache_hash()
            # ALWAYS query where="1=1" on MAD_WMS_PRD/0 and filter in Python to avoid timeout
            survey_marks = query_arcgis_geojson(
                [self.survey_marks_url],
                tile,
                cache_path=self._cache_file(f"survey_marks_{bhash}.json"),
                where="1=1",
                out_fields="OBJECTID,MARK_NO,MARKTYPE,SPECATTRIBUTES,H_ORDER,V_ORDER,V_DATUM,HEIGHT,GONE",
                page_size=300,
                max_pages=1,
                timeout_s=self.timeout_s,
                client=self._client,
            )
            primary_marks: list[RawPoiFeature] = []
            fallback_pillar: list[RawPoiFeature] = []
            fallback_any: list[RawPoiFeature] = []
            for feat in survey_marks:
                pt = _coerce_point_geometry(feat.get("geometry"))
                if pt is None:
                    continue
                props = dict(feat.get("properties") or {})
                if str(prop_ci(props, "GONE") or "").strip().upper() == "Y":
                    continue
                elev_m = parse_elevation_m(prop_ci(props, "HEIGHT", "elev_m"))
                if elev_m is None:
                    continue
                v_datum = str(prop_ci(props, "V_DATUM") or "").strip().upper()
                if v_datum and v_datum not in ("A", "AHD", "AHD71"):
                    continue
                h_order = _to_int_or_none(prop_ci(props, "H_ORDER"))
                v_order = _to_int_or_none(prop_ci(props, "V_ORDER"))
                marktype = str(prop_ci(props, "MARKTYPE") or "").strip().upper()
                spec_attr = str(prop_ci(props, "SPECATTRIBUTES") or "").strip().upper()
                mark_no = _clean_au_name(prop_ci(props, "MARK_NO"))
                label = f"SM {mark_no}" if mark_no else "Survey Mark"
                fid = str(
                    mark_no
                    or prop_ci(props, "OBJECTID", "id")
                    or feat.get("id")
                    or deterministic_feature_id("sa_survey_mark", pt, props)
                )
                poi = RawPoiFeature(
                    source_provider="sa_survey_mark",
                    source_license="CC-BY-4.0",
                    source_feature_id=fid,
                    name=label,
                    poi_type="trig",
                    state="SA",
                    elev_m=elev_m,
                    raw_props=props,
                    geometry=pt,
                )
                is_high = (
                    (h_order is not None and h_order <= 2)
                    or (v_order is not None and v_order <= 2)
                    or marktype in ("TS", "TRIG", "PLR", "BCN", "CRN")
                    or any(k in spec_attr for k in ("TRIG", "PILLAR", "BEACON"))
                )
                if is_high:
                    primary_marks.append(poi)
                elif marktype in ("PSMP", "PSM", "BM", "MP") or (
                    v_order is not None and v_order <= 3
                ):
                    fallback_pillar.append(poi)
                else:
                    fallback_any.append(poi)

            selected = primary_marks or fallback_pillar or fallback_any
            if len(selected) > 12:
                thinned: list[RawPoiFeature] = []
                seen_cells: set[tuple[int, int]] = set()
                for poi in selected:
                    coords = poi.geometry.get("coordinates") or [0.0, 0.0]
                    cell = (
                        int(round(float(coords[0]) * 200.0)),
                        int(round(float(coords[1]) * 200.0)),
                    )
                    if cell in seen_cells:
                        continue
                    seen_cells.add(cell)
                    thinned.append(poi)
                selected = thinned
            raw_pois.extend(selected)

        return dedupe_poi_features(raw_pois)


# -----------------------------------------------------------------------------
# 6. Northern Territory (NTLIS / DEPWS WFS 2.0 + NTG Roads + GA Topo250K — CC BY 4.0)
# -----------------------------------------------------------------------------


class NTTrailsConnector:
    """Northern Territory NTG tracks, GA Topo250K & NTLIS WFS 2.0 POIs/trigs (CC BY 4.0)."""

    name: str = "au-nt-trails"
    state_code: str = "NT"
    attribution: str = ATTRIBUTION_NT_NTLIS

    def __init__(
        self,
        *,
        roads_url: str | None = None,
        ga_tracks_url: str | None = None,
        wfs_url: str | None = None,
        cache_dir: Path | str | None = None,
        timeout_s: float = 25.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.roads_url = roads_url or os.environ.get(
            "TRAILFORGE_AU_NT_ROADS_URL", DEFAULT_NT_ROADS_URL
        )
        self.ga_tracks_url = ga_tracks_url or os.environ.get(
            "TRAILFORGE_AU_GA_TOPO_ROADS_URL", DEFAULT_GA_TOPO250K_ROADS_URL
        )
        self.wfs_url = wfs_url or os.environ.get("TRAILFORGE_AU_NT_WFS_URL", DEFAULT_NT_WFS_URL)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.timeout_s = timeout_s
        self._client = client

    def _cache_file(self, key: str) -> Path | None:
        return (self.cache_dir / "au_nt" / f"{key}.json") if self.cache_dir is not None else None

    def fetch_tracks(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawTrackFeature]:
        """Fetch NTG 4WD/unformed/pastoral road centrelines and GA Topo250K outback tracks."""
        if not is_in_nt_coverage(bbox):
            return []

        tracks: list[RawTrackFeature] = []
        seen: set[tuple[str, str]] = set()
        for tile in iter_spatial_tiles(bbox.round_enclosing(), max_deg=tile_deg):
            bhash = tile.cache_hash()
            nt_roads = query_arcgis_geojson(
                [self.roads_url],
                tile,
                cache_path=self._cache_file(f"roads_{bhash}"),
                where="SURFACE_TYPE IN ('4WD','UNFORMED') OR CATEGORY LIKE 'PASTORAL%'",
                out_fields="OBJECTID,ROAD_LABEL,ROAD_NAME,ROAD_TYPE,SURFACE_TYPE,CATEGORY",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in nt_roads:
                geom = _coerce_line_geometry(feat.get("geometry"))
                if geom is None:
                    continue
                props = dict(feat.get("properties") or {})
                surf_raw = str(prop_ci(props, "SURFACE_TYPE") or "").strip().upper()
                if surf_raw == "SEALED":
                    continue
                name = _clean_au_name(
                    prop_ci(props, "ROAD_LABEL"), strip_leading_star=True
                ) or _clean_au_name(prop_ci(props, "ROAD_NAME"), strip_leading_star=True)
                low = (name or "").lower()
                if "fire" in low:
                    trail_class: TrailClassLiteral = "fire_trail"
                elif surf_raw in ("4WD", "UNFORMED"):
                    trail_class = "4wd_track"
                else:
                    trail_class = "management_trail"
                surface = "natural" if surf_raw == "UNFORMED" else "unsealed"
                fid = str(
                    prop_ci(props, "OBJECTID", "id")
                    or feat.get("id")
                    or deterministic_feature_id("nt_roads", geom, props)
                ).strip()
                key = ("nt_roads", fid)
                if key in seen:
                    continue
                seen.add(key)
                tracks.append(
                    RawTrackFeature(
                        source="nt",
                        source_layer="nt_roads",
                        source_id=fid,
                        name=name,
                        trail_class=trail_class,
                        is_vehicular=True,
                        surface_class=normalize_surface_class(surface, trail_class=trail_class),
                        surface_raw=surface,
                        is_4wd_only=(surf_raw == "4WD"),
                        access_status="public",
                        access_raw=str(prop_ci(props, "CATEGORY") or "").strip() or None,
                        geometry=geom,
                        raw_props=props,
                    )
                )

            ga_tracks = query_arcgis_geojson(
                [self.ga_tracks_url],
                tile,
                cache_path=self._cache_file(f"ga_tracks_{bhash}"),
                where="class IN ('Track','Vehicular Track','Foot Track')",
                out_fields="OBJECTID,name,class,formation",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in ga_tracks:
                geom = _coerce_line_geometry(feat.get("geometry"))
                if geom is None:
                    continue
                props = dict(feat.get("properties") or {})
                name = _clean_au_name(prop_ci(props, "name"))
                cls = str(prop_ci(props, "class") or "").strip().lower()
                formation = str(prop_ci(props, "formation") or "").strip().lower()
                trail_class = "walking_track" if "foot" in cls else "4wd_track"
                surface = formation if formation and formation != "unknown" else "unsealed"
                fid = str(
                    prop_ci(props, "OBJECTID", "id")
                    or feat.get("id")
                    or deterministic_feature_id("nt_ga_topo250k", geom, props)
                ).strip()
                key = ("nt_ga_topo250k", fid)
                if key in seen:
                    continue
                seen.add(key)
                tracks.append(
                    RawTrackFeature(
                        source="nt",
                        source_layer="nt_ga_topo250k",
                        source_id=fid,
                        name=name,
                        trail_class=trail_class,
                        is_vehicular=(trail_class != "walking_track"),
                        surface_class=normalize_surface_class(surface, trail_class=trail_class),
                        surface_raw=surface,
                        access_status="public",
                        geometry=geom,
                        raw_props=props,
                    )
                )

        return tracks

    def fetch_pois(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawPoiFeature]:
        """Fetch NTLIS WFS 2.0 place names (lookouts, gorges, camps, huts) and AHD control marks."""
        if not is_in_nt_coverage(bbox):
            return []

        raw_pois: list[RawPoiFeature] = []
        for tile in iter_spatial_tiles(bbox.round_enclosing(), max_deg=tile_deg):
            bhash = tile.cache_hash()
            placenames = query_wfs_geojson(
                self.wfs_url,
                "ntlis:PLACENAMES",
                tile,
                cache_path=self._cache_file(f"placenames_{bhash}"),
                use_bbox_param=True,
                max_features=1000,
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in placenames:
                pt = _coerce_point_geometry(feat.get("geometry"))
                if pt is None:
                    continue
                props = dict(feat.get("properties") or {})
                name = _clean_au_name(prop_ci(props, "NAME", "name"))
                if not name:
                    continue
                desig = str(prop_ci(props, "TYPE_DESIGNATION") or "").strip().lower()
                poi_type = _NT_PLACENAME_POI_MAP.get(desig)
                if poi_type is None:
                    low = name.lower()
                    if _HUT_WORD_RE.search(name):
                        poi_type = "hut"
                    elif "camp" in low:
                        poi_type = "campground"
                    elif "lookout" in low:
                        poi_type = "lookout"
                    elif "picnic" in low:
                        poi_type = "picnic"
                if poi_type is None:
                    continue
                fid = str(
                    prop_ci(props, "OBJECTID", "ID", "id")
                    or feat.get("id")
                    or deterministic_feature_id("nt_placename", pt, props)
                )
                raw_pois.append(
                    RawPoiFeature(
                        source_provider="nt_placename",
                        source_license="CC-BY-4.0",
                        source_feature_id=fid,
                        name=name,
                        poi_type=poi_type,
                        state="NT",
                        raw_props=props,
                        geometry=pt,
                    )
                )

            survey_marks = query_wfs_geojson(
                self.wfs_url,
                "survey:SURVEY_MARKS",
                tile,
                cache_path=self._cache_file(f"survey_marks_{bhash}"),
                use_bbox_param=True,
                max_features=1000,
                timeout_s=self.timeout_s,
                client=self._client,
            )
            primary_marks: list[RawPoiFeature] = []
            fallback_marks: list[RawPoiFeature] = []
            for feat in survey_marks:
                pt = _coerce_point_geometry(feat.get("geometry"))
                if pt is None:
                    continue
                props = dict(feat.get("properties") or {})
                mark_type = str(prop_ci(props, "MARKTYPE") or "").strip().upper()
                elev_m = parse_elevation_m(prop_ci(props, "ADJ_RL"))
                if elev_m is None or elev_m <= 0.0:
                    elev_m = parse_elevation_m(prop_ci(props, "QUASI_AHD"))
                if elev_m is None or elev_m <= 0.0:
                    continue
                name = _clean_au_name(prop_ci(props, "PRIMARYNAME", "name")) or "Survey Mark"
                fid = str(
                    prop_ci(props, "OBJECTID", "PRIMARYNAME", "id")
                    or feat.get("id")
                    or deterministic_feature_id("nt_survey_mark", pt, props)
                )
                poi = RawPoiFeature(
                    source_provider="nt_survey_mark",
                    source_license="CC-BY-4.0",
                    source_feature_id=fid,
                    name=name,
                    poi_type="trig",
                    state="NT",
                    elev_m=elev_m,
                    raw_props=props,
                    geometry=pt,
                )
                if mark_type in ("CM", "CRM", "TRIG", "PM"):
                    primary_marks.append(poi)
                else:
                    fallback_marks.append(poi)

            raw_pois.extend(primary_marks if primary_marks else fallback_marks)

        return dedupe_poi_features(raw_pois)


# -----------------------------------------------------------------------------
# Pan-Australia Cross-Border Auto State Trails Router (All 8 States & Territories)
# -----------------------------------------------------------------------------


def resolve_intersecting_states(bbox: BBox) -> list[str]:
    """Return ordered list of Australian state/territory codes intersecting ``bbox``.

    Extents strictly inside ``ACT_STRICT_BBOX`` resolve to ``['ACT']`` only (avoiding
    spurious NSW overlap); cross-border extents resolve to all intersecting jurisdictions
    (e.g. ``['ACT', 'NSW']``, ``['NSW', 'VIC']``, ``['SA', 'NT']``).
    """
    if _bbox_strictly_within(bbox, ACT_STRICT_BBOX):
        return ["ACT"]
    predicates = (
        ("ACT", is_in_act_coverage),
        ("NSW", is_in_nsw_coverage),
        ("VIC", is_in_vic_coverage),
        ("TAS", is_in_tas_coverage),
        ("QLD", is_in_qld_coverage),
        ("WA", is_in_wa_coverage),
        ("SA", is_in_sa_coverage),
        ("NT", is_in_nt_coverage),
    )
    return [code for code, fn in predicates if fn(bbox)]


class AUStateRouterResult(BaseModel):
    """Merged cross-border state trails/POIs and exact dynamic contributing-state attributions."""

    states_queried: list[str] = Field(default_factory=list)
    contributing_states: list[str] = Field(default_factory=list)
    vic_tracks: list[RawTrackFeature] = Field(default_factory=list)
    nsw_tracks: list[RawTrackFeature] = Field(default_factory=list)
    au_state_tracks: list[RawTrackFeature] = Field(default_factory=list)
    pois: list[RawPoiFeature] = Field(default_factory=list)
    attributions: list[str] = Field(default_factory=list)

    @property
    def all_tracks(self) -> list[RawTrackFeature]:
        return [*self.vic_tracks, *self.nsw_tracks, *self.au_state_tracks]


class AutoAUStateTrailsRouter:
    """Auto-detecting cross-border Australian state trail & POI router covering all 8 states."""

    name: str = "au-state-trails-router"

    _DEFAULT_ATTRIBUTIONS: Final[dict[str, str]] = {
        "VIC": ATTRIBUTION_VIC_DEECA,
        "NSW": ATTRIBUTION_NSW_SPATIAL,
        "ACT": ATTRIBUTION_ACT_ACTMAPI,
        "TAS": ATTRIBUTION_TAS_LIST,
        "QLD": ATTRIBUTION_QLD_QSPATIAL,
        "WA": ATTRIBUTION_WA_DBCA_SLIP,
        "SA": ATTRIBUTION_SA_DEW_DHUD,
        "NT": ATTRIBUTION_NT_NTLIS,
    }

    def __init__(
        self,
        *,
        state_connectors: Mapping[str, Any] | None = None,
        cache_dir: Path | str | None = None,
        timeout_s: float = 25.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.timeout_s = timeout_s
        self._client = client
        self._injected: dict[str, Any] = (
            {k.upper(): v for k, v in state_connectors.items()} if state_connectors else {}
        )
        self.resolved_attributions: list[str] = []

    def get_connector(self, state_code: str) -> Any:
        code = state_code.upper()
        if code in self._injected:
            return self._injected[code]
        kwargs: dict[str, Any] = {
            "cache_dir": self.cache_dir,
            "timeout_s": self.timeout_s,
            "client": self._client,
        }
        if code == "VIC":
            return VICTrailsConnector(**kwargs)
        if code == "NSW":
            return NSWTrailsConnector(**kwargs)
        if code == "ACT":
            return ACTTrailsConnector(**kwargs)
        if code == "TAS":
            return TASTrailsConnector(**kwargs)
        if code == "QLD":
            return QLDTrailsConnector(**kwargs)
        if code == "WA":
            return WATrailsConnector(**kwargs)
        if code == "SA":
            return SATrailsConnector(**kwargs)
        if code == "NT":
            return NTTrailsConnector(**kwargs)
        raise ValueError(f"Unsupported Australian state code: {state_code!r}")

    def fetch(
        self,
        bbox: BBox,
        *,
        tile_deg: float = 1.0,
        include_pois: bool = True,
    ) -> AUStateRouterResult:
        """Detect all intersecting AU states for ``bbox``, dispatch connectors, and merge results.

        Returns exact dynamic attribution strings **ONLY for contributing states** (states that
        returned at least one track or POI).
        """
        states = resolve_intersecting_states(bbox)
        vic_tracks: list[RawTrackFeature] = []
        nsw_tracks: list[RawTrackFeature] = []
        au_state_tracks: list[RawTrackFeature] = []
        all_pois: list[RawPoiFeature] = []
        contributing_states: list[str] = []
        attributions: list[str] = []

        for code in states:
            conn = self.get_connector(code)
            state_tracks: list[RawTrackFeature] = []
            state_pois: list[RawPoiFeature] = []
            try:
                state_tracks = list(conn.fetch_tracks(bbox, tile_deg=tile_deg))
            except Exception as exc:
                logger.warning(
                    "AutoAUStateTrailsRouter skipping %s tracks after error: %s", code, exc
                )
            if include_pois:
                try:
                    state_pois = list(conn.fetch_pois(bbox, tile_deg=tile_deg))
                except Exception as exc:
                    logger.warning(
                        "AutoAUStateTrailsRouter skipping %s POIs after error: %s", code, exc
                    )

            if state_tracks or state_pois:
                contributing_states.append(code)
                attr = str(
                    getattr(conn, "attribution", "") or self._DEFAULT_ATTRIBUTIONS.get(code, "")
                ).strip()
                if attr and attr not in attributions:
                    attributions.append(attr)

            for trk in state_tracks:
                if trk.source == "vic":
                    vic_tracks.append(trk)
                elif trk.source == "nsw":
                    nsw_tracks.append(trk)
                else:
                    au_state_tracks.append(trk)
            all_pois.extend(state_pois)

        self.resolved_attributions = list(attributions)
        return AUStateRouterResult(
            states_queried=states,
            contributing_states=contributing_states,
            vic_tracks=vic_tracks,
            nsw_tracks=nsw_tracks,
            au_state_tracks=au_state_tracks,
            pois=dedupe_poi_features(all_pois),
            attributions=attributions,
        )

    def fetch_tracks(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawTrackFeature]:
        return self.fetch(bbox, tile_deg=tile_deg, include_pois=False).all_tracks

    def fetch_pois(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawPoiFeature]:
        states = resolve_intersecting_states(bbox)
        raw_pois: list[RawPoiFeature] = []
        attributions: list[str] = []
        for code in states:
            conn = self.get_connector(code)
            try:
                pois = list(conn.fetch_pois(bbox, tile_deg=tile_deg))
            except Exception as exc:
                logger.warning(
                    "AutoAUStateTrailsRouter skipping %s POIs after error: %s", code, exc
                )
                continue
            if pois:
                attr = str(
                    getattr(conn, "attribution", "") or self._DEFAULT_ATTRIBUTIONS.get(code, "")
                ).strip()
                if attr and attr not in attributions:
                    attributions.append(attr)
                raw_pois.extend(pois)
        self.resolved_attributions = attributions
        return dedupe_poi_features(raw_pois)


__all__ = [
    "ACTTrailsConnector",
    "AUStateRouterResult",
    "AutoAUStateTrailsRouter",
    "DEFAULT_ACT_BASE_URL",
    "DEFAULT_GA_TOPO250K_ROADS_URL",
    "DEFAULT_NT_ROADS_URL",
    "DEFAULT_NT_WFS_URL",
    "DEFAULT_QLD_PARKS_BASE_URL",
    "DEFAULT_QLD_ROADS_URL",
    "DEFAULT_QLD_SURVEY_URL",
    "DEFAULT_QLD_TOURIST_URL",
    "DEFAULT_SA_FIRE_TRACKS_URL",
    "DEFAULT_SA_PARKS_JSON_URL",
    "DEFAULT_SA_SURVEY_MARKS_URL",
    "DEFAULT_SA_TRAILS_ZIP_INNER",
    "DEFAULT_SA_TRAILS_ZIP_URL",
    "DEFAULT_TAS_OPENDATA_BASE_URL",
    "DEFAULT_TAS_TOPO_BASE_URL",
    "DEFAULT_WA_FIRE_TOWERS_URL",
    "DEFAULT_WA_GEODETIC_URL",
    "DEFAULT_WA_RATIS_FACILITIES_URL",
    "DEFAULT_WA_RATIS_SITES_URL",
    "DEFAULT_WA_SLIP_ROADS_URL",
    "DEFAULT_WA_TRACKS_URL",
    "NTTrailsConnector",
    "QLDTrailsConnector",
    "SATrailsConnector",
    "TASTrailsConnector",
    "WATrailsConnector",
    "is_wa_non_commercial_restricted",
    "parse_act_track_feature",
    "parse_qld_qpws_feature",
    "parse_qld_state_road_feature",
    "parse_tas_track_feature",
    "parse_wa_track_feature",
    "read_sa_trails_from_zip",
    "resolve_intersecting_states",
]
