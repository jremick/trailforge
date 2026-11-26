"""Offline loader for TrailForge's committed VIC/NSW Australian Alps corridor fixtures."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trailforge.ingestion.connectors.base import (
    ATTRIBUTION_CAPAD,
    ATTRIBUTION_GA_DEM,
    ATTRIBUTION_GA_HYDRO,
    ATTRIBUTION_NSW_SPATIAL,
    ATTRIBUTION_OSM,
    ATTRIBUTION_VIC_DEECA,
    BBox,
    CorridorDataset,
    PoiProviderLiteral,
    PoiTypeLiteral,
    RawPoiFeature,
    clean_feature_name,
    deterministic_feature_id,
    parse_elevation_m,
    prop_ci,
)
from trailforge.ingestion.connectors.capad import CAPADConnector
from trailforge.ingestion.connectors.hydro import GAHydroConnector
from trailforge.ingestion.connectors.nsw import NSWTrailsConnector
from trailforge.ingestion.connectors.osm import OSMConnector
from trailforge.ingestion.connectors.vic import VICTrailsConnector

_VALID_POI_PROVIDERS: frozenset[str] = frozenset(
    {
        "vic_foi",
        "vic_recweb_hut",
        "vic_recweb_site",
        "vic_geomark",
        "nsw_poi",
        "nsw_trig",
        "nsw_hut",
        "ga_hydro_pt",
        "osm",
    }
)
_VALID_POI_TYPES: frozenset[str] = frozenset(
    {
        "campground",
        "hut",
        "trig",
        "lookout",
        "picnic",
        "water_point",
        "gate",
        "trailhead",
        "fuel",
    }
)


def _parse_fixture_poi(feat: Mapping[str, Any]) -> RawPoiFeature | None:
    geom = feat.get("geometry")
    if not isinstance(geom, Mapping) or geom.get("type") != "Point":
        return None
    props = dict(feat.get("properties") or {})

    provider_raw = str(prop_ci(props, "source_provider") or "osm").strip().lower()
    provider: PoiProviderLiteral = (
        provider_raw if provider_raw in _VALID_POI_PROVIDERS else "osm"  # type: ignore[assignment]
    )
    license_raw = str(prop_ci(props, "source_license") or "").strip()
    source_license = "ODbL" if (license_raw == "ODbL" or provider == "osm") else "CC-BY-4.0"

    poi_type_raw = str(prop_ci(props, "poi_type") or "").strip().lower()
    if poi_type_raw not in _VALID_POI_TYPES:
        return None
    poi_type: PoiTypeLiteral = poi_type_raw  # type: ignore[assignment]

    state_raw = str(prop_ci(props, "state") or "").strip().upper()
    state = state_raw if state_raw in ("VIC", "NSW", "ACT") else None

    feat_id = str(
        prop_ci(props, "source_feature_id", "id")
        or feat.get("id")
        or deterministic_feature_id("poi", geom, props)
    )
    return RawPoiFeature(
        source_provider=provider,
        source_license=source_license,
        source_feature_id=feat_id,
        name=clean_feature_name(prop_ci(props, "name")),
        poi_type=poi_type,
        state=state,  # type: ignore[arg-type]
        elev_m=parse_elevation_m(prop_ci(props, "elev_m", "elevation")),
        access_note=(
            str(props["access_note"]).strip()
            if props.get("access_note") not in (None, "")
            else None
        ),
        raw_props=props,
        geometry=dict(geom),
    )


def load_corridor_fixtures(
    fixtures_dir: Path | str,
    osm_filename: str = "osm_tracks.geojson",
    *,
    exclude_connector_waterways: bool = True,
) -> CorridorDataset:
    """Load the synthetic VIC/NSW Australian Alps border corridor completely offline.

    Args:
        fixtures_dir: Directory containing ``manifest.json``, GeoJSON layers, and DEM GeoTIFF.
        osm_filename: OSM tracks GeoJSON file name (defaults to ``osm_tracks.geojson``; pass
            ``osm_tracks_v2_jittered.geojson`` to test Slice 02 re-import segment-ID stability).
        exclude_connector_waterways: Drop GA Surface Hydrology ``featuretype == 'Connector'``
            rows on load (default ``True`` per ``docs/SPEC_GIS_SCHEMA.md`` Sec 2.2).
    """
    base = Path(fixtures_dir).expanduser().resolve()
    manifest_path = base / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Fixture manifest not found: {manifest_path}")

    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_bbox = manifest["bbox"]
    bbox = BBox(
        min_x=float(raw_bbox[0]),
        min_y=float(raw_bbox[1]),
        max_x=float(raw_bbox[2]),
        max_y=float(raw_bbox[3]),
    )

    osm_tracks = OSMConnector().load_tracks_geojson(base / osm_filename)
    vic_tracks = VICTrailsConnector().load_tracks_geojson(base / "vic_tracks.geojson")
    nsw_tracks = NSWTrailsConnector().load_tracks_geojson(base / "nsw_tracks.geojson")
    waterways = GAHydroConnector().load_waterways_geojson(
        base / "waterways.geojson",
        exclude_connectors=exclude_connector_waterways,
    )
    capad_parks = CAPADConnector().load_parks_geojson(base / "capad_parks.geojson")

    pois_fc = json.loads((base / "pois.geojson").read_text(encoding="utf-8"))
    pois: list[RawPoiFeature] = []
    for feat in pois_fc.get("features") or []:
        if isinstance(feat, Mapping):
            parsed = _parse_fixture_poi(feat)
            if parsed is not None:
                pois.append(parsed)

    dem_filename = str((manifest.get("dem") or {}).get("filename") or "dem_alps_corridor.tif")
    dem_path = base / dem_filename
    if not dem_path.is_file():
        raise FileNotFoundError(f"Fixture DEM GeoTIFF not found: {dem_path}")

    return CorridorDataset(
        corridor_name=str(manifest.get("corridor") or "Australian Alps Synthetic Corridor"),
        bbox=bbox,
        osm_tracks=osm_tracks,
        vic_tracks=vic_tracks,
        nsw_tracks=nsw_tracks,
        waterways=waterways,
        pois=pois,
        capad_parks=capad_parks,
        dem_path=dem_path,
        dem_tier="synthetic",
        attributions=[
            ATTRIBUTION_OSM,
            ATTRIBUTION_VIC_DEECA,
            ATTRIBUTION_NSW_SPATIAL,
            ATTRIBUTION_GA_HYDRO,
            ATTRIBUTION_CAPAD,
            ATTRIBUTION_GA_DEM,
        ],
    )
