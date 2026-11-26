"""Victoria DataVic / DEECA / DTP Vicmap OGC WFS 2.0.0 connector (CC BY 4.0)."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Literal

import httpx

from trailforge.ingestion.connectors.base import (
    ATTRIBUTION_VIC_DEECA,
    BBox,
    PoiProviderLiteral,
    PoiTypeLiteral,
    RawPoiFeature,
    RawTrackFeature,
    TrailClassLiteral,
    clean_feature_name,
    dedupe_poi_features,
    deterministic_feature_id,
    is_in_vic_coverage,
    iter_spatial_tiles,
    normalize_surface_class,
    parse_elevation_m,
    prop_ci,
    query_wfs_geojson,
)

DEFAULT_VIC_WFS_URL: Final[str] = "https://opendata.maps.vic.gov.au/geoserver/wfs"

_HUT_WORD_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(hut|huts|bivouac|shelter|refuge)\b", re.IGNORECASE
)
_VIC_SEAL_MAP: Final[dict[str, str]] = {
    "1": "sealed",
    "2": "unsealed",
    "3": "natural",
}


def _classify_vic_road_trail(
    props: Mapping[str, Any], name: str | None
) -> tuple[TrailClassLiteral, bool]:
    lower_name = (name or "").lower()
    ftype = str(prop_ci(props, "feature_type_code") or "").strip().lower()
    if (
        "fire trail" in lower_name
        or "firetrail" in lower_name
        or "fireline" in lower_name
        or "fire access" in lower_name
        or "fire" in ftype
    ):
        return ("fire_trail", True)
    if "management" in lower_name:
        return ("management_trail", True)

    raw_class = prop_ci(props, "class_code")
    try:
        class_code = int(raw_class) if raw_class is not None else -1
    except (TypeError, ValueError):
        class_code = -1

    if class_code in (8, 9) or ftype == "trail":
        return ("walking_track", False)
    if class_code == 6:
        return ("management_trail", True)
    return ("4wd_track", True)


def _classify_vic_recweb_track(
    props: Mapping[str, Any], name: str | None
) -> tuple[TrailClassLiteral, bool]:
    lower_name = (name or "").lower()
    access = str(prop_ci(props, "access_dsc", "trk_class", "track_type") or "").strip().lower()
    if "fire" in lower_name or "fire" in access:
        return ("fire_trail", True)
    if "management" in lower_name or "management" in access:
        return ("management_trail", True)
    if any(k in access for k in ("4wd", "vehicle", "motor", "drive")):
        return ("4wd_track", True)
    return ("walking_track", False)


def _classify_vic_poi(
    props: Mapping[str, Any], *, default_type: PoiTypeLiteral | None = None
) -> PoiTypeLiteral | None:
    subtype = str(
        prop_ci(props, "feature_subtype", "feature_type", "site_type", "site_class", "type") or ""
    ).strip().lower()
    name = str(prop_ci(props, "name_label", "name", "hut_name", "site_name") or "").strip()
    if any(k in subtype for k in ("hut", "shelter", "cabin", "refuge", "bivouac")):
        return "hut"
    if "survey" in subtype or "trig" in subtype:
        return "trig"
    if "camp" in subtype:
        return "campground"
    if "lookout" in subtype or "viewpoint" in subtype:
        return "lookout"
    if "picnic" in subtype or "day visitor" in subtype or "visitor area" in subtype:
        return "picnic"
    if "trailhead" in subtype:
        return "trailhead"
    if _HUT_WORD_RE.search(name):
        return "hut"
    return default_type


def parse_vic_track_feature(
    feat: Mapping[str, Any],
    *,
    source_layer: Literal["tr_road", "recweb_tracks"] | None = None,
) -> RawTrackFeature | None:
    """Normalize a Vicmap ``tr_road`` or DEECA ``recweb_tracks`` GeoJSON feature."""
    geom = feat.get("geometry")
    if not isinstance(geom, Mapping) or geom.get("type") not in ("LineString", "MultiLineString"):
        return None
    props = dict(feat.get("properties") or {})

    layer_raw = source_layer or str(prop_ci(props, "source_layer") or "tr_road").strip().lower()
    resolved_layer: Literal["tr_road", "recweb_tracks"] = (
        "recweb_tracks" if layer_raw == "recweb_tracks" else "tr_road"
    )

    if resolved_layer == "tr_road":
        name = clean_feature_name(prop_ci(props, "ezi_road_name_label", "road_name", "name"))
        trail_class, is_vehicular = _classify_vic_road_trail(props, name)
        raw_seal = str(prop_ci(props, "road_seal") or "").strip().lower()
        default_surf = "natural" if trail_class == "walking_track" else "unsealed"
        surface_raw = _VIC_SEAL_MAP.get(
            raw_seal, default_surf if raw_seal in ("", "0", "unknown") else raw_seal
        )
    else:
        name = clean_feature_name(prop_ci(props, "name", "track_name", "ezi_road_name_label"))
        trail_class, is_vehicular = _classify_vic_recweb_track(props, name)
        surface_raw = "natural" if trail_class == "walking_track" else "unsealed"

    raw_class = prop_ci(props, "class_code")
    class_code: int | None = None
    if raw_class is not None:
        try:
            class_code = int(raw_class)
        except (TypeError, ValueError):
            class_code = None

    feat_id = str(
        prop_ci(props, "pfi", "ufi", "objectid", "id")
        or feat.get("id")
        or deterministic_feature_id("vic", geom, props)
    ).strip()

    # Coerce to MultiLineString to match source_tracks_vic schema cleanly
    gj_geom = dict(geom)
    if gj_geom.get("type") == "LineString":
        gj_geom = {"type": "MultiLineString", "coordinates": [gj_geom["coordinates"]]}

    return RawTrackFeature(
        source="vic",
        source_layer=resolved_layer,
        source_id=feat_id,
        name=name,
        trail_class=trail_class,
        is_vehicular=is_vehicular,
        surface_class=normalize_surface_class(surface_raw, trail_class=trail_class),
        surface_raw=surface_raw,
        access_status="public",
        geometry=gj_geom,
        raw_props=props,
        class_code=class_code,
    )


def parse_vic_poi_feature(
    feat: Mapping[str, Any],
    *,
    source_provider: PoiProviderLiteral,
    default_type: PoiTypeLiteral | None = None,
) -> RawPoiFeature | None:
    """Normalize a Vicmap/DEECA WFS POI feature into a ``RawPoiFeature``."""
    geom = feat.get("geometry")
    if not isinstance(geom, Mapping) or geom.get("type") != "Point":
        return None
    props = dict(feat.get("properties") or {})
    poi_type = _classify_vic_poi(props, default_type=default_type)
    if poi_type is None:
        return None

    name = clean_feature_name(prop_ci(props, "name_label", "name", "hut_name", "site_name"))
    if not name and poi_type == "hut":
        name = "Hut"
    feat_id = str(
        prop_ci(props, "pfi", "ufi", "objectid", "id")
        or feat.get("id")
        or deterministic_feature_id(source_provider, geom, props)
    ).strip()

    return RawPoiFeature(
        source_provider=source_provider,
        source_license="CC-BY-4.0",
        source_feature_id=feat_id,
        name=name,
        poi_type=poi_type,
        state="VIC",
        elev_m=parse_elevation_m(prop_ci(props, "elev_m", "elevation", "height", "ahd_height")),
        raw_props=props,
        geometry=dict(geom),
    )


class VICTrailsConnector:
    """Victoria DataVic / DEECA / Vicmap WFS 2.0.0 connector (tr_road, recweb_tracks, POIs)."""

    name: str = "au-vic-trails"
    attribution: str = ATTRIBUTION_VIC_DEECA

    def __init__(
        self,
        *,
        wfs_url: str | None = None,
        cache_dir: Path | str | None = None,
        timeout_s: float = 25.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.wfs_url = wfs_url or os.environ.get("TRAILFORGE_AU_VIC_WFS_URL", DEFAULT_VIC_WFS_URL)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.timeout_s = timeout_s
        self._client = client

    def _cache_file(self, key: str) -> Path | None:
        return (self.cache_dir / "au_vic" / f"{key}.json") if self.cache_dir is not None else None

    def load_tracks_geojson(self, path: Path | str) -> list[RawTrackFeature]:
        """Load and normalize VIC tracks from a local GeoJSON file (100% offline)."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        out: list[RawTrackFeature] = []
        for feat in payload.get("features") or []:
            if isinstance(feat, Mapping):
                parsed = parse_vic_track_feature(feat)
                if parsed is not None:
                    out.append(parsed)
        return out

    def fetch_tracks(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawTrackFeature]:
        """Fetch VIC ``tr_road`` and ``recweb_tracks`` via tiled WFS 2.0.0 queries."""
        if not is_in_vic_coverage(bbox):
            return []

        tracks: list[RawTrackFeature] = []
        seen: set[tuple[str, str]] = set()
        for tile in iter_spatial_tiles(bbox.round_enclosing(), max_deg=tile_deg):
            bhash = tile.cache_hash()
            road_feats = query_wfs_geojson(
                self.wfs_url,
                "open-data-platform:tr_road",
                tile,
                cache_path=self._cache_file(f"tr_road_{bhash}"),
                cql_filter="class_code IN (7,8,9) OR (class_code=6 AND road_seal='2')",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in road_feats:
                parsed = parse_vic_track_feature(feat, source_layer="tr_road")
                if parsed is not None and (parsed.source_layer, parsed.source_id) not in seen:
                    seen.add((parsed.source_layer, parsed.source_id))
                    tracks.append(parsed)

            rec_feats = query_wfs_geojson(
                self.wfs_url,
                "open-data-platform:recweb_tracks",
                tile,
                cache_path=self._cache_file(f"recweb_tracks_{bhash}"),
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in rec_feats:
                parsed = parse_vic_track_feature(feat, source_layer="recweb_tracks")
                if parsed is not None and (parsed.source_layer, parsed.source_id) not in seen:
                    seen.add((parsed.source_layer, parsed.source_id))
                    tracks.append(parsed)

        return tracks

    def fetch_pois(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawPoiFeature]:
        """Fetch VIC huts, campsites, lookouts, picnic sites, and survey marks via WFS."""
        if not is_in_vic_coverage(bbox):
            return []

        raw_pois: list[RawPoiFeature] = []
        for tile in iter_spatial_tiles(bbox.round_enclosing(), max_deg=tile_deg):
            bhash = tile.cache_hash()
            layer_jobs: list[tuple[str, str | None, PoiProviderLiteral, PoiTypeLiteral | None]] = [
                (
                    "open-data-platform:foi_point",
                    "feature_subtype IN ('hut','camp ground','camping ground',"
                    "'picnic site','day visitor area','lookout','trailhead','survey monument')",
                    "vic_foi",
                    None,
                ),
                ("open-data-platform:recweb_hut", None, "vic_recweb_hut", "hut"),
                ("open-data-platform:recweb_site", None, "vic_recweb_site", "campground"),
                (
                    "open-data-platform:geomark_point",
                    "feature_subtype IN ('survey monument','hut')",
                    "vic_geomark",
                    None,
                ),
            ]
            for type_name, cql, provider, default_type in layer_jobs:
                feats = query_wfs_geojson(
                    self.wfs_url,
                    type_name,
                    tile,
                    cache_path=self._cache_file(f"{provider}_{bhash}"),
                    cql_filter=cql,
                    timeout_s=self.timeout_s,
                    client=self._client,
                )
                for feat in feats:
                    parsed = parse_vic_poi_feature(
                        feat, source_provider=provider, default_type=default_type
                    )
                    if parsed is not None:
                        raw_pois.append(parsed)

        return dedupe_poi_features(raw_pois)
