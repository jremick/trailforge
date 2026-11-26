"""Geoscience Australia National Surface Hydrology connector (CC BY 4.0)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import httpx

from trailforge.ingestion.connectors.base import (
    ATTRIBUTION_GA_HYDRO,
    BBox,
    RawPoiFeature,
    RawWaterwayFeature,
    clean_feature_name,
    deterministic_feature_id,
    iter_spatial_tiles,
    prop_ci,
    query_arcgis_geojson,
)

DEFAULT_AU_HYDRO_BASE_URL: Final[str] = (
    "https://services.ga.gov.au/gis/rest/services/Surface_Hydrology/MapServer"
)


def parse_waterway_feature(
    feat: Mapping[str, Any], *, exclude_connectors: bool = True
) -> RawWaterwayFeature | None:
    """Normalize a GA Surface Hydrology or OSM waterway GeoJSON feature."""
    geom = feat.get("geometry")
    if not isinstance(geom, Mapping) or "type" not in geom:
        return None
    props = dict(feat.get("properties") or {})
    feature_type = str(prop_ci(props, "featuretype", "feature_type") or "Watercourse").strip()
    if exclude_connectors and feature_type.lower() == "connector":
        return None

    provider_raw = str(prop_ci(props, "source_provider") or "ga_hydro").strip().lower()
    provider = "osm" if provider_raw == "osm" else "ga_hydro"
    license_raw = str(prop_ci(props, "source_license") or "").strip()
    source_license = "ODbL" if (license_raw == "ODbL" or provider == "osm") else "CC-BY-4.0"

    feat_id = str(
        prop_ci(props, "source_feature_id", "objectid", "hydroid", "id")
        or feat.get("id")
        or deterministic_feature_id("hydro", geom, props)
    ).strip()

    perenniality = str(prop_ci(props, "perenniality") or "Unknown").strip()
    hierarchy_val = prop_ci(props, "hierarchy")
    hierarchy = str(hierarchy_val).strip() if hierarchy_val is not None else None

    return RawWaterwayFeature(
        source_provider=provider,
        source_license=source_license,
        source_feature_id=feat_id,
        name=clean_feature_name(prop_ci(props, "name")),
        feature_type=feature_type,
        perenniality=perenniality,
        hierarchy=hierarchy,
        geometry=dict(geom),
    )


class GAHydroConnector:
    """Geoscience Australia Surface Hydrology ArcGIS REST connector (L1/L3/L5)."""

    name: str = "au-ga-hydro"
    attribution: str = ATTRIBUTION_GA_HYDRO

    def __init__(
        self,
        *,
        base_url: str | None = None,
        cache_dir: Path | str | None = None,
        timeout_s: float = 25.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("TRAILFORGE_AU_HYDRO_URL", DEFAULT_AU_HYDRO_BASE_URL)
        ).rstrip("/")
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.timeout_s = timeout_s
        self._client = client

    def _cache_file(self, key: str) -> Path | None:
        return (self.cache_dir / "au_hydro" / f"{key}.json") if self.cache_dir is not None else None

    def load_waterways_geojson(
        self, path: Path | str, *, exclude_connectors: bool = True
    ) -> list[RawWaterwayFeature]:
        """Load waterways from a local GeoJSON file (100% offline)."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        out: list[RawWaterwayFeature] = []
        for feat in payload.get("features") or []:
            if isinstance(feat, Mapping):
                parsed = parse_waterway_feature(feat, exclude_connectors=exclude_connectors)
                if parsed is not None:
                    out.append(parsed)
        return out

    def fetch_waterways(
        self, bbox: BBox, *, include_polygons: bool = False, tile_deg: float = 1.0
    ) -> list[RawWaterwayFeature]:
        """Fetch GA Surface Hydrology watercourses (Layer 3, excluding ``Connector``)."""
        waterways: list[RawWaterwayFeature] = []
        seen: set[str] = set()
        for tile in iter_spatial_tiles(bbox.round_enclosing(), max_deg=tile_deg):
            bhash = tile.cache_hash()
            line_feats = query_arcgis_geojson(
                [f"{self.base_url}/3/query"],
                tile,
                cache_path=self._cache_file(f"lines_{bhash}"),
                where="featuretype <> 'Connector'",
                out_fields="OBJECTID,name,featuretype,perenniality,hierarchy",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in line_feats:
                parsed = parse_waterway_feature(feat, exclude_connectors=True)
                if parsed is not None and parsed.source_feature_id not in seen:
                    seen.add(parsed.source_feature_id)
                    waterways.append(parsed)

            if include_polygons:
                poly_feats = query_arcgis_geojson(
                    [f"{self.base_url}/5/query"],
                    tile,
                    cache_path=self._cache_file(f"polys_{bhash}"),
                    out_fields="OBJECTID,name,featuretype,perenniality",
                    timeout_s=self.timeout_s,
                    client=self._client,
                )
                for feat in poly_feats:
                    parsed = parse_waterway_feature(feat, exclude_connectors=True)
                    if parsed is not None and parsed.source_feature_id not in seen:
                        seen.add(parsed.source_feature_id)
                        waterways.append(parsed)

        return waterways

    def fetch_water_points(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawPoiFeature]:
        """Fetch GA Surface Hydrology Layer 1 water points (bores, waterholes, springs)."""
        points: list[RawPoiFeature] = []
        seen: set[str] = set()
        for tile in iter_spatial_tiles(bbox.round_enclosing(), max_deg=tile_deg):
            bhash = tile.cache_hash()
            pt_feats = query_arcgis_geojson(
                [f"{self.base_url}/1/query"],
                tile,
                cache_path=self._cache_file(f"pts_{bhash}"),
                out_fields="OBJECTID,name,featuretype,type,perenniality",
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in pt_feats:
                geom = feat.get("geometry")
                if not isinstance(geom, Mapping) or geom.get("type") != "Point":
                    continue
                props = dict(feat.get("properties") or {})
                fid = str(
                    prop_ci(props, "objectid", "id")
                    or feat.get("id")
                    or deterministic_feature_id("hydpt", geom, props)
                )
                if fid in seen:
                    continue
                seen.add(fid)
                points.append(
                    RawPoiFeature(
                        source_provider="ga_hydro_pt",
                        source_license="CC-BY-4.0",
                        source_feature_id=fid,
                        name=clean_feature_name(prop_ci(props, "name")) or "Water Point",
                        poi_type="water_point",
                        raw_props=props,
                        geometry=dict(geom),
                    )
                )
        return points
