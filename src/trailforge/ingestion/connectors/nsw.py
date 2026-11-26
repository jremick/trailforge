"""NSW Spatial Services (DCS) ArcGIS REST connector (CC BY 4.0)."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Literal

import httpx

from trailforge.ingestion.connectors.base import (
    ATTRIBUTION_NSW_SPATIAL,
    BBox,
    PoiTypeLiteral,
    RawPoiFeature,
    RawTrackFeature,
    TrailClassLiteral,
    clean_feature_name,
    dedupe_poi_features,
    deterministic_feature_id,
    is_in_nsw_coverage,
    iter_spatial_tiles,
    normalize_surface_class,
    parse_elevation_m,
    prop_ci,
    query_arcgis_geojson,
)

DEFAULT_NSW_TRANSPORT_BASE_URL: Final[str] = (
    "https://portal.spatial.nsw.gov.au/server/rest/services/NSW_Transport_Theme/MapServer"
)
DEFAULT_NSW_TRIG_URL: Final[str] = (
    "https://portal.spatial.nsw.gov.au/server/rest/services/SurveyMarkGDA2020/MapServer/0/query"
)
DEFAULT_NSW_POI_URL: Final[str] = (
    "https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_POI/MapServer/0/query"
)
DEFAULT_NSW_HUTS_URL: Final[str] = (
    "https://portal.spatial.nsw.gov.au/server/rest/services/"
    "NSW_Features_of_Interest_Category/MapServer/3/query"
)

_HUT_WORD_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(hut|huts|bivouac|shelter|refuge)\b", re.IGNORECASE
)
_NSW_SURFACE_MAP: Final[dict[str, str]] = {
    "1": "sealed",
    "2": "unsealed",
    "3": "natural",
    "4": "unsealed",
}


def compose_nsw_road_name(props: Mapping[str, Any]) -> str | None:
    """Compose official NSW road / fire trail name from Spatial Services attributes."""
    full = clean_feature_name(prop_ci(props, "roadname", "firetrailname", "trailname", "name"))
    if full:
        return full
    base = clean_feature_name(prop_ci(props, "roadnamebase"))
    rtype = clean_feature_name(prop_ci(props, "roadnametype"))
    if base and rtype:
        if base.lower().endswith(rtype.lower()):
            return base
        return f"{base} {rtype}"
    if base:
        return base
    brimsid = prop_ci(props, "brimsid")
    if brimsid is not None:
        return f"Fire Trail {str(brimsid).strip()}"
    return None


def _normalize_nsw_surface_raw(value: Any, *, default: str = "unsealed") -> str:
    if value is None:
        return default
    raw = str(value).strip().lower()
    if not raw or raw in ("0", "unknown", "none", "null"):
        return default
    return _NSW_SURFACE_MAP.get(raw, raw)


def _classify_nsw_poi(props: Mapping[str, Any]) -> PoiTypeLiteral | None:
    poitype = str(prop_ci(props, "poitype", "featuretype", "type") or "").strip().lower()
    label = str(prop_ci(props, "poilabel", "poiname", "generalname", "name") or "").strip()
    if any(k in poitype for k in ("hut", "shelter", "cabin", "refuge", "bivouac")):
        return "hut"
    if "camp" in poitype:
        return "campground"
    if "lookout" in poitype or "viewpoint" in poitype:
        return "lookout"
    if "picnic" in poitype or "rest area" in poitype:
        return "picnic"
    if _HUT_WORD_RE.search(label):
        return "hut"
    return None


def parse_nsw_track_feature(
    feat: Mapping[str, Any],
    *,
    source_layer: Literal["nsw_fire_trails_l9", "nsw_tracks_l5"] | None = None,
) -> RawTrackFeature | None:
    """Normalize an NSW ``NSW_Transport_Theme`` Layer 9 or Layer 5 feature."""
    geom = feat.get("geometry")
    if not isinstance(geom, Mapping) or geom.get("type") not in ("LineString", "MultiLineString"):
        return None
    props = dict(feat.get("properties") or {})

    layer_raw = source_layer or str(prop_ci(props, "source_layer") or "").strip().lower()
    if layer_raw not in ("nsw_fire_trails_l9", "nsw_tracks_l5"):
        layer_raw = (
            "nsw_fire_trails_l9"
            if prop_ci(props, "brimsid", "firetrailname") is not None
            else "nsw_tracks_l5"
        )
    resolved_layer: Literal["nsw_fire_trails_l9", "nsw_tracks_l5"] = layer_raw  # type: ignore[assignment]

    name = compose_nsw_road_name(props)
    lower_name = (name or "").lower()

    raw_subtype = prop_ci(props, "classsubtype")
    class_subtype: int | None = None
    if raw_subtype is not None:
        try:
            class_subtype = int(raw_subtype)
        except (TypeError, ValueError):
            class_subtype = 6 if "path" in str(raw_subtype).lower() else 5

    if resolved_layer == "nsw_fire_trails_l9":
        trail_class: TrailClassLiteral = "fire_trail"
        is_vehicular = True
    elif "fire trail" in lower_name or "firetrail" in lower_name or lower_name.endswith(" ft"):
        trail_class = "fire_trail"
        is_vehicular = True
    elif "management trail" in lower_name or "management track" in lower_name:
        trail_class = "management_trail"
        is_vehicular = True
    elif class_subtype == 6:
        trail_class = "walking_track"
        is_vehicular = False
    else:
        trail_class = "4wd_track"
        is_vehicular = True

    default_surf = "natural" if trail_class == "walking_track" else "unsealed"
    surface_raw = _normalize_nsw_surface_raw(
        prop_ci(props, "surface", "surfacetype"), default=default_surf
    )
    op_status = prop_ci(props, "operationalstatus")
    op_status_str = str(op_status).strip() if op_status is not None else None
    access_status = "closed" if (op_status_str or "").upper() in ("C", "CLOSED") else "public"

    feat_id = str(
        prop_ci(props, "objectid", "brimsid", "id")
        or feat.get("id")
        or deterministic_feature_id("nsw", geom, props)
    ).strip()

    gj_geom = dict(geom)
    if gj_geom.get("type") == "LineString":
        gj_geom = {"type": "MultiLineString", "coordinates": [gj_geom["coordinates"]]}

    return RawTrackFeature(
        source="nsw",
        source_layer=resolved_layer,
        source_id=feat_id,
        name=name,
        trail_class=trail_class,
        is_vehicular=is_vehicular,
        surface_class=normalize_surface_class(surface_raw, trail_class=trail_class),
        surface_raw=surface_raw,
        access_status=access_status,
        geometry=gj_geom,
        raw_props=props,
        class_subtype=class_subtype,
        operational_status=op_status_str,
    )


def parse_nsw_trig_feature(feat: Mapping[str, Any]) -> RawPoiFeature | None:
    geom = feat.get("geometry")
    if not isinstance(geom, Mapping) or geom.get("type") != "Point":
        return None
    props = dict(feat.get("properties") or {})
    trigname = clean_feature_name(prop_ci(props, "trigname", "name"))
    marknum = prop_ci(props, "marknumber")
    if not trigname:
        trigname = f"TS {marknum}" if marknum is not None else "Trig Station"
    feat_id = str(prop_ci(props, "marknumber", "objectid", "id") or feat.get("id") or trigname)
    return RawPoiFeature(
        source_provider="nsw_trig",
        source_license="CC-BY-4.0",
        source_feature_id=feat_id,
        name=trigname,
        poi_type="trig",
        state="NSW",
        elev_m=parse_elevation_m(prop_ci(props, "ahdheight_label", "ahdheight", "elev_m")),
        raw_props=props,
        geometry=dict(geom),
    )


def parse_nsw_poi_feature(feat: Mapping[str, Any]) -> RawPoiFeature | None:
    geom = feat.get("geometry")
    if not isinstance(geom, Mapping) or geom.get("type") != "Point":
        return None
    props = dict(feat.get("properties") or {})
    poi_type = _classify_nsw_poi(props)
    if poi_type is None:
        return None
    name = clean_feature_name(prop_ci(props, "poilabel", "poiname", "generalname", "name"))
    feat_id = str(
        prop_ci(props, "objectid", "id")
        or feat.get("id")
        or deterministic_feature_id("nsw-poi", geom, props)
    )
    return RawPoiFeature(
        source_provider="nsw_poi",
        source_license="CC-BY-4.0",
        source_feature_id=feat_id,
        name=name,
        poi_type=poi_type,
        state="NSW",
        elev_m=parse_elevation_m(prop_ci(props, "elev_m", "elevation")),
        raw_props=props,
        geometry=dict(geom),
    )


def parse_nsw_hut_feature(feat: Mapping[str, Any]) -> RawPoiFeature | None:
    geom = feat.get("geometry")
    if not isinstance(geom, Mapping) or geom.get("type") != "Point":
        return None
    props = dict(feat.get("properties") or {})
    name = clean_feature_name(prop_ci(props, "generalname", "name", "poilabel"))
    if not name:
        return None
    feat_id = str(
        prop_ci(props, "objectid", "id")
        or feat.get("id")
        or deterministic_feature_id("nsw-hut", geom, props)
    )
    return RawPoiFeature(
        source_provider="nsw_hut",
        source_license="CC-BY-4.0",
        source_feature_id=feat_id,
        name=name,
        poi_type="hut",
        state="NSW",
        elev_m=parse_elevation_m(prop_ci(props, "elev_m", "elevation")),
        raw_props=props,
        geometry=dict(geom),
    )


class NSWTrailsConnector:
    """NSW Spatial Services ArcGIS REST connector for fire trails, 4WD tracks, trigs, & POIs."""

    name: str = "au-nsw-trails"
    attribution: str = ATTRIBUTION_NSW_SPATIAL

    def __init__(
        self,
        *,
        transport_base_url: str | None = None,
        trig_url: str | None = None,
        poi_url: str | None = None,
        huts_url: str | None = None,
        cache_dir: Path | str | None = None,
        timeout_s: float = 25.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.transport_base_url = (
            transport_base_url
            or os.environ.get("TRAILFORGE_AU_NSW_TRANSPORT_URL", DEFAULT_NSW_TRANSPORT_BASE_URL)
        ).rstrip("/")
        self.trig_url = trig_url or os.environ.get(
            "TRAILFORGE_AU_NSW_TRIG_URL", DEFAULT_NSW_TRIG_URL
        )
        self.poi_url = poi_url or os.environ.get("TRAILFORGE_AU_NSW_POI_URL", DEFAULT_NSW_POI_URL)
        self.huts_url = huts_url or os.environ.get(
            "TRAILFORGE_AU_NSW_HUTS_URL", DEFAULT_NSW_HUTS_URL
        )
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.timeout_s = timeout_s
        self._client = client

    def _cache_file(self, key: str) -> Path | None:
        return (self.cache_dir / "au_nsw" / f"{key}.json") if self.cache_dir is not None else None

    def load_tracks_geojson(self, path: Path | str) -> list[RawTrackFeature]:
        """Load and normalize NSW tracks from a local GeoJSON file (100% offline)."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        out: list[RawTrackFeature] = []
        for feat in payload.get("features") or []:
            if isinstance(feat, Mapping):
                parsed = parse_nsw_track_feature(feat)
                if parsed is not None:
                    out.append(parsed)
        return out

    def fetch_tracks(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawTrackFeature]:
        """Fetch NSW Fire Trails (Layer 9) and 4WD/Walking Tracks (Layer 5)."""
        if not is_in_nsw_coverage(bbox):
            return []

        tracks: list[RawTrackFeature] = []
        seen: set[tuple[str, str]] = set()
        for tile in iter_spatial_tiles(bbox.round_enclosing(), max_deg=tile_deg):
            bhash = tile.cache_hash()
            fire_feats = query_arcgis_geojson(
                [f"{self.transport_base_url}/9/query"],
                tile,
                cache_path=self._cache_file(f"firetrails_{bhash}"),
                out_fields="*",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in fire_feats:
                parsed = parse_nsw_track_feature(feat, source_layer="nsw_fire_trails_l9")
                if parsed is not None and (parsed.source_layer, parsed.source_id) not in seen:
                    seen.add((parsed.source_layer, parsed.source_id))
                    tracks.append(parsed)

            track_feats = query_arcgis_geojson(
                [f"{self.transport_base_url}/5/query"],
                tile,
                cache_path=self._cache_file(f"tracks_{bhash}"),
                where="classsubtype IN (5, 6)",
                out_fields=(
                    "OBJECTID,classsubtype,roadnamebase,roadnametype,"
                    "functionhierarchy,surface,operationalstatus"
                ),
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in track_feats:
                parsed = parse_nsw_track_feature(feat, source_layer="nsw_tracks_l5")
                if parsed is not None and (parsed.source_layer, parsed.source_id) not in seen:
                    seen.add((parsed.source_layer, parsed.source_id))
                    tracks.append(parsed)

        return tracks

    def fetch_pois(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawPoiFeature]:
        """Fetch NSW Trig Stations, POIs, and Backcountry Huts."""
        if not is_in_nsw_coverage(bbox):
            return []

        raw_pois: list[RawPoiFeature] = []
        for tile in iter_spatial_tiles(bbox.round_enclosing(), max_deg=tile_deg):
            bhash = tile.cache_hash()
            for feat in query_arcgis_geojson(
                [self.trig_url],
                tile,
                cache_path=self._cache_file(f"trigs_{bhash}"),
                where="marktype='TS'",
                out_fields=(
                    "OBJECTID,marktype,marknumber,trigname,trigtype,"
                    "ahdheight_label,markstatus,monumenttype"
                ),
                timeout_s=self.timeout_s,
                client=self._client,
            ):
                parsed = parse_nsw_trig_feature(feat)
                if parsed is not None:
                    raw_pois.append(parsed)

            for feat in query_arcgis_geojson(
                [self.poi_url],
                tile,
                cache_path=self._cache_file(f"pois_{bhash}"),
                where="1=1",
                out_fields="*",
                timeout_s=self.timeout_s,
                client=self._client,
            ):
                parsed = parse_nsw_poi_feature(feat)
                if parsed is not None:
                    raw_pois.append(parsed)

            for feat in query_arcgis_geojson(
                [self.huts_url],
                tile,
                cache_path=self._cache_file(f"huts_{bhash}"),
                where="UPPER(generalname) LIKE '%HUT%'",
                out_fields="*",
                timeout_s=self.timeout_s,
                client=self._client,
            ):
                parsed = parse_nsw_hut_feature(feat)
                if parsed is not None:
                    raw_pois.append(parsed)

        return dedupe_poi_features(raw_pois)
