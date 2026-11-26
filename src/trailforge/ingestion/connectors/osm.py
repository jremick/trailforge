"""OpenStreetMap (ODbL) track, gate, waterway, and POI connector (PBF/Overpass/GeoJSON)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import httpx

from trailforge.ingestion.connectors.base import (
    ATTRIBUTION_OSM,
    USER_AGENT,
    AccessStatusLiteral,
    BBox,
    RawTrackFeature,
    TrailClassLiteral,
    clean_feature_name,
    normalize_surface_class,
    normalize_tracktype,
    stream_download_file,
)

logger = logging.getLogger(__name__)

DEFAULT_AU_PBF_URL: Final[str] = (
    "https://download.geofabrik.de/australia-oceania/australia-latest.osm.pbf"
)
DEFAULT_OVERPASS_URL: Final[str] = "https://overpass-api.de/api/interpreter"

_GATE_BARRIERS: Final[frozenset[str]] = frozenset(
    {"gate", "lift_gate", "swing_gate", "bollard", "chain", "cattle_grid", "block"}
)
_NON_VEHICULAR_HIGHWAYS: Final[frozenset[str]] = frozenset(
    {"footway", "steps", "pedestrian", "cycleway", "bridleway"}
)


def _classify_osm_track(
    tags: Mapping[str, Any], name: str | None
) -> tuple[TrailClassLiteral, bool]:
    """Return ``(trail_class, is_vehicular)`` from OSM tags."""
    highway = str(tags.get("highway") or "track").strip().lower()
    motor_vehicle = str(tags.get("motor_vehicle") or "").strip().lower()
    vehicle = str(tags.get("vehicle") or "").strip().lower()
    four_wd = str(tags.get("4wd_only") or "").strip().lower()
    lower_name = (name or "").lower()

    if highway in _NON_VEHICULAR_HIGHWAYS:
        return ("walking_track", False)
    if highway == "path" and (
        motor_vehicle == "no" or vehicle == "no" or four_wd not in ("yes", "recommended")
    ):
        return ("walking_track", False)
    if "fire trail" in lower_name or "firetrail" in lower_name or lower_name.endswith(" ft"):
        return ("fire_trail", True)
    if "management trail" in lower_name or "management track" in lower_name:
        return ("management_trail", True)
    if highway in ("unclassified", "tertiary", "secondary", "residential", "service"):
        if four_wd in ("yes", "recommended"):
            return ("4wd_track", True)
        return ("unsealed_road", True)
    return ("4wd_track", True)


def _normalize_osm_access(tags: Mapping[str, Any]) -> AccessStatusLiteral:
    access = str(tags.get("access") or "").strip().lower()
    motor_vehicle = str(tags.get("motor_vehicle") or "").strip().lower()
    seasonal = str(tags.get("seasonal") or "").strip().lower()

    if access == "closed":
        return "closed"
    if access in ("private", "no") or motor_vehicle == "private":
        return "private"
    if access in ("permit", "permissive_permit"):
        return "permit"
    if access in ("restricted", "forestry", "agricultural", "destination", "customers", "delivery"):
        return "restricted"
    if access == "seasonal" or seasonal in ("yes", "winter", "summer", "wet_season"):
        return "seasonal"
    return "public"


def parse_osm_track_feature(feat: Mapping[str, Any]) -> RawTrackFeature | None:
    """Normalize a single OSM GeoJSON Feature into a ``RawTrackFeature``."""
    geom = feat.get("geometry")
    if not isinstance(geom, Mapping) or geom.get("type") not in ("LineString", "MultiLineString"):
        return None
    props = dict(feat.get("properties") or {})

    raw_way_id = props.get("osm_way_id") or props.get("osm_id") or feat.get("id")
    osm_way_id: int | None = None
    if raw_way_id is not None:
        raw_str = str(raw_way_id).strip()
        if raw_str.startswith("way/"):
            raw_str = raw_str[4:]
        try:
            osm_way_id = int(raw_str)
        except ValueError:
            osm_way_id = None

    source_id = f"way/{osm_way_id}" if osm_way_id is not None else str(raw_way_id or "unknown")
    name = clean_feature_name(props.get("name"))
    highway = str(props.get("highway") or "track").strip().lower()
    trail_class, is_vehicular = _classify_osm_track(props, name)
    tracktype = normalize_tracktype(props.get("tracktype"))
    surface_raw = str(props["surface"]).strip() if props.get("surface") not in (None, "") else None
    surface_class = normalize_surface_class(
        surface_raw, tracktype=tracktype, trail_class=trail_class
    )
    smoothness = (
        str(props["smoothness"]).strip().lower()
        if props.get("smoothness") not in (None, "")
        else None
    )
    four_wd_raw = (
        str(props["4wd_only"]).strip().lower()
        if props.get("4wd_only") not in (None, "")
        else None
    )
    bridge_raw = (
        str(props["bridge"]).strip().lower() if props.get("bridge") not in (None, "") else None
    )
    ford_raw = str(props["ford"]).strip().lower() if props.get("ford") not in (None, "") else None
    barrier_raw = (
        str(props["barrier"]).strip().lower() if props.get("barrier") not in (None, "") else None
    )

    is_bridged = bridge_raw in ("yes", "viaduct", "culvert", "low_water_crossing")
    is_ford = ford_raw in ("yes", "stepping_stones", "boat") or highway == "ford"
    has_gate_barrier = bool(props.get("has_gate_barrier")) or (barrier_raw in _GATE_BARRIERS)

    osm_version: int | None = None
    if props.get("osm_version") is not None:
        try:
            osm_version = int(props["osm_version"])
        except (TypeError, ValueError):
            osm_version = None

    return RawTrackFeature(
        source="osm",
        source_layer="osm",
        source_id=source_id,
        name=name,
        trail_class=trail_class,
        is_vehicular=is_vehicular,
        surface_class=surface_class,
        surface_raw=surface_raw,
        tracktype=tracktype,
        smoothness=smoothness,
        is_4wd_only=four_wd_raw == "yes",
        is_4wd_recommended=four_wd_raw == "recommended",
        access_status=_normalize_osm_access(props),
        has_gate_barrier=has_gate_barrier,
        is_bridged=is_bridged,
        is_ford=is_ford,
        geometry=dict(geom),
        raw_props=props,
        osm_way_id=osm_way_id,
        osm_version=osm_version,
        highway=highway,
        four_wd_only_raw=four_wd_raw,
        access_raw=str(props["access"]).strip() if props.get("access") not in (None, "") else None,
        motor_vehicle=(
            str(props["motor_vehicle"]).strip()
            if props.get("motor_vehicle") not in (None, "")
            else None
        ),
        bridge_raw=bridge_raw,
        ford_raw=ford_raw,
    )


def parse_osm_geojson(geojson: Mapping[str, Any]) -> list[RawTrackFeature]:
    """Parse an OSM GeoJSON FeatureCollection into normalized ``RawTrackFeature`` records."""
    features = geojson.get("features") or []
    out: list[RawTrackFeature] = []
    for feat in features:
        if isinstance(feat, Mapping):
            parsed = parse_osm_track_feature(feat)
            if parsed is not None:
                out.append(parsed)
    return out


class OSMConnector:
    """OpenStreetMap connector supporting offline GeoJSON, streaming PBF extract, and Overpass."""

    name: str = "osm"
    attribution: str = ATTRIBUTION_OSM

    def __init__(
        self,
        *,
        overpass_url: str | None = None,
        pbf_url: str | None = None,
        cache_dir: Path | str | None = None,
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.overpass_url = overpass_url or os.environ.get(
            "TRAILFORGE_OVERPASS_URL", DEFAULT_OVERPASS_URL
        )
        self.pbf_url = pbf_url or os.environ.get("TRAILFORGE_AU_PBF_URL", DEFAULT_AU_PBF_URL)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.timeout_s = timeout_s
        self._client = client

    def load_tracks_geojson(self, path: Path | str) -> list[RawTrackFeature]:
        """Load and normalize OSM track features from a local GeoJSON file (100% offline)."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return parse_osm_geojson(payload)

    def extract_pbf_bbox(
        self,
        bbox: BBox,
        out_pbf: Path,
        *,
        source_pbf: Path | None = None,
    ) -> Path:
        """Stream-download Geofabrik Australia PBF if needed and clip bbox with ``osmium``."""
        if source_pbf is not None:
            src = Path(source_pbf).expanduser().resolve()
            if not src.is_file():
                raise FileNotFoundError(f"Source OSM PBF not found: {src}")
        else:
            if self.cache_dir is None:
                raise ValueError("cache_dir is required when downloading remote Australia PBF")
            filename = Path(self.pbf_url.split("?")[0]).name or "australia-latest.osm.pbf"
            src = stream_download_file(
                self.pbf_url,
                self.cache_dir / "geofabrik" / filename,
                client=self._client,
            )

        out_pbf.parent.mkdir(parents=True, exist_ok=True)
        bbox_str = f"{bbox.min_x:.6f},{bbox.min_y:.6f},{bbox.max_x:.6f},{bbox.max_y:.6f}"
        cmd = [
            "osmium",
            "extract",
            "-b",
            bbox_str,
            "--strategy=complete_ways",
            str(src),
            "-o",
            str(out_pbf),
            "--overwrite",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"osmium extract failed (exit {proc.returncode}): {proc.stderr.strip()}"
            )
        return out_pbf

    def fetch_overpass_tracks(self, bbox: BBox) -> list[RawTrackFeature]:
        """Query Overpass API for off-road tracks and barrier nodes in ``bbox``."""
        overpass_bbox = f"{bbox.min_y:.6f},{bbox.min_x:.6f},{bbox.max_y:.6f},{bbox.max_x:.6f}"
        query = (
            "[out:json][timeout:25];"
            "("
            f'way["highway"~"^(track|path|unclassified|service|ford)$"]({overpass_bbox});'
            f'node["barrier"~"^(gate|lift_gate|swing_gate|bollard|chain)$"]({overpass_bbox});'
            ");"
            "out body geom;"
        )

        owns_client = self._client is None
        http = (
            self._client
            if self._client is not None
            else httpx.Client(
                timeout=httpx.Timeout(self.timeout_s, connect=min(8.0, self.timeout_s)),
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
        )
        try:
            resp = http.post(
                self.overpass_url,
                data={"data": query},
                headers={"User-Agent": USER_AGENT},
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning("Overpass track query failed (%s); returning empty list", exc)
            return []
        finally:
            if owns_client:
                http.close()

        elements = payload.get("elements") if isinstance(payload, dict) else None
        if not isinstance(elements, list):
            return []

        gate_node_ids: set[int] = set()
        for el in elements:
            if isinstance(el, dict) and el.get("type") == "node":
                tags = el.get("tags") or {}
                if str(tags.get("barrier") or "").lower() in _GATE_BARRIERS:
                    nid = el.get("id")
                    if isinstance(nid, int):
                        gate_node_ids.add(nid)

        tracks: list[RawTrackFeature] = []
        for el in elements:
            if not isinstance(el, dict) or el.get("type") != "way":
                continue
            geom_pts = el.get("geometry") or []
            if len(geom_pts) < 2:
                continue
            coords = [[float(p["lon"]), float(p["lat"])] for p in geom_pts]
            tags = dict(el.get("tags") or {})
            nodes = el.get("nodes") or []
            tags["osm_way_id"] = el.get("id")
            if el.get("version") is not None:
                tags["osm_version"] = el.get("version")
            if any(nid in gate_node_ids for nid in nodes):
                tags["has_gate_barrier"] = True
            feat = {
                "type": "Feature",
                "properties": tags,
                "geometry": {"type": "LineString", "coordinates": coords},
            }
            parsed = parse_osm_track_feature(feat)
            if parsed is not None:
                tracks.append(parsed)
        return tracks
