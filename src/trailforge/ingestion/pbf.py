"""Streaming <300 MB RAM OpenStreetMap PBF extractor using ``osmium`` CLI.

Adapted from GridForge's disk-to-disk streaming pattern:
  1. ``osmium extract -b <w,s,e,n> --strategy=complete_ways`` clips the target bbox.
  2. ``osmium tags-filter`` strips non-offroad ways/nodes before geometry assembly.
  3. ``osmium export -u type_id -a type,id,version -f geojsonseq`` emits RFC 8142
     newline-delimited GeoJSON features streamed line-by-line in O(1) memory.

All extracted tracks, waterways, and POIs carry explicit OSM ``ODbL`` provenance so
Tier A staging isolation and attribution invariants are preserved.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, Final, TextIO

import httpx
from pydantic import BaseModel, Field

from trailforge.ingestion.connectors.base import (
    ATTRIBUTION_OSM,
    GDAL_STREAMING_ENV,
    BBox,
    PoiTypeLiteral,
    RawPoiFeature,
    RawTrackFeature,
    RawWaterwayFeature,
    clean_feature_name,
    dedupe_poi_features,
    deterministic_feature_id,
    parse_elevation_m,
)
from trailforge.ingestion.connectors.osm import (
    DEFAULT_AU_PBF_URL,
    download_osm_pbf,
    parse_osm_track_feature,
)
from trailforge.ingestion.segmenter import infer_state_from_lonlat

logger = logging.getLogger(__name__)

# Locked osmium tags-filter expressions for vehicular tracks, waterways, and backcountry POIs.
PBF_TAGS_FILTER_EXPRESSIONS: Final[tuple[str, ...]] = (
    "w/highway=track,unclassified,service,path,bridleway,ford",
    "w/waterway",
    "n/tourism=camp_site,alpine_hut,wilderness_hut,viewpoint,picnic_site",
    "n/amenity=shelter,drinking_water,fuel",
    "n/man_made=survey_point",
    "n/barrier=gate,lift_gate,swing_gate,bollard,chain,cattle_grid,block",
)

_ALLOWED_TRACK_HIGHWAYS: Final[frozenset[str]] = frozenset(
    {"track", "unclassified", "service", "path", "bridleway", "ford"}
)
_EXCLUDED_SERVICE_SUBTYPES: Final[frozenset[str]] = frozenset(
    {"driveway", "parking_aisle", "alley", "drive-through", "emergency_access"}
)
_EXPLICIT_SEALED_SURFACES: Final[frozenset[str]] = frozenset(
    {"asphalt", "paved", "concrete", "bitumen", "tarmac", "chipseal", "sealed"}
)
_WATERWAY_VALUES: Final[frozenset[str]] = frozenset(
    {"river", "stream", "creek", "drain", "canal", "watercourse"}
)
_GATE_BARRIER_VALUES: Final[frozenset[str]] = frozenset(
    {"gate", "lift_gate", "swing_gate", "bollard", "chain", "cattle_grid", "block"}
)
_NUMERIC_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[nwr]?/?(\d+)$", re.IGNORECASE)
_RS_CHAR: Final[str] = "\x1e"


class PbfExtractResult(BaseModel):
    """Structured output of streaming OSM PBF / GeoJSONSeq extraction."""

    tracks: list[RawTrackFeature] = Field(default_factory=list)
    waterways: list[RawWaterwayFeature] = Field(default_factory=list)
    pois: list[RawPoiFeature] = Field(default_factory=list)
    features_scanned: int = 0
    tracks_skipped_non_vehicular: int = 0
    source_path: str | None = None
    bbox: list[float] | None = None
    attribution: str = ATTRIBUTION_OSM


def _streaming_env() -> dict[str, str]:
    env = os.environ.copy()
    for key, val in GDAL_STREAMING_ENV.items():
        env.setdefault(key, val)
    return env


def _run_osmium(cmd: list[str], *, step: str) -> None:
    if shutil.which("osmium") is None:
        raise RuntimeError(
            "osmium CLI binary not found on PATH; install osmium-tool or run inside the "
            "TrailForge Docker container."
        )
    proc = subprocess.run(cmd, env=_streaming_env(), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{step} failed (exit={proc.returncode}).\n"
            f"Command: {' '.join(cmd)}\n"
            f"STDERR:\n{proc.stderr[-2500:]}"
        )


def _extract_osm_meta(feat: Mapping[str, Any]) -> tuple[str | None, int | None, int | None]:
    """Extract ``(osm_type, numeric_id, osm_version)`` from osmium-exported GeoJSON features."""
    props = feat.get("properties") or {}
    raw_type = props.get("@type")
    osm_type = str(raw_type).strip().lower() if raw_type is not None else None

    raw_id = (
        props.get("@id")
        or props.get("osm_way_id")
        or props.get("osm_id")
        or feat.get("id")
    )
    numeric_id: int | None = None
    if raw_id is not None:
        raw_str = str(raw_id).strip()
        if raw_str.startswith(("way/", "node/", "relation/")):
            prefix, rest = raw_str.split("/", 1)
            osm_type = osm_type or prefix
            raw_str = rest
        elif len(raw_str) > 1 and raw_str[0] in ("w", "n", "r") and raw_str[1:].isdigit():
            type_map = {"w": "way", "n": "node", "r": "relation"}
            osm_type = osm_type or type_map[raw_str[0]]
            raw_str = raw_str[1:]
        match = _NUMERIC_ID_RE.match(raw_str)
        if match:
            numeric_id = int(match.group(1))

    raw_ver = props.get("@version") if props.get("@version") is not None else props.get(
        "osm_version"
    )
    osm_version: int | None = None
    if raw_ver is not None:
        try:
            osm_version = int(raw_ver)
        except (TypeError, ValueError):
            osm_version = None

    return (osm_type, numeric_id, osm_version)


def _clean_osmium_props(props: Mapping[str, Any]) -> dict[str, Any]:
    """Return OSM tag dict stripped of osmium ``@...`` meta-keys."""
    return {str(k): v for k, v in props.items() if not str(k).startswith("@")}


def iter_geojsonseq_features(
    source: Path | str | TextIO | Iterable[str],
) -> Iterator[dict[str, Any]]:
    """Yield GeoJSON Feature dicts streaming line-by-line from GeoJSONSeq or GeoJSON.

    Supports:
      - RFC 8142 GeoJSON Text Sequences (optional leading ``\\x1e`` record separator per line)
      - Newline-delimited GeoJSON Features (NDJSON / JSONL)
      - Standard GeoJSON ``FeatureCollection`` fallback for local files
    """
    if isinstance(source, (Path, str)):
        path = Path(source)
        with path.open("r", encoding="utf-8") as fh:
            yield from iter_geojsonseq_features(fh)
        return

    buffer_lines: list[str] | None = None
    for raw_line in source:
        line = raw_line.lstrip(_RS_CHAR).strip()
        if not line:
            continue

        if buffer_lines is not None:
            buffer_lines.append(line)
            continue

        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            # Multi-line standard GeoJSON FeatureCollection fallback
            if line.startswith("{"):
                buffer_lines = [line]
            else:
                logger.warning("Skipping malformed GeoJSONSeq line: %.80s", line)
            continue

        if not isinstance(obj, dict):
            continue
        obj_type = obj.get("type")
        if obj_type == "Feature":
            yield obj
        elif obj_type == "FeatureCollection":
            for feat in obj.get("features") or []:
                if isinstance(feat, dict) and feat.get("type") == "Feature":
                    yield feat

    if buffer_lines is not None:
        try:
            payload = json.loads("\n".join(buffer_lines))
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse fallback GeoJSON payload: %s", exc)
            return
        if isinstance(payload, dict):
            if payload.get("type") == "Feature":
                yield payload
            elif payload.get("type") == "FeatureCollection":
                for feat in payload.get("features") or []:
                    if isinstance(feat, dict) and feat.get("type") == "Feature":
                        yield feat


def _is_vehicular_pbf_track(parsed: RawTrackFeature, tags: Mapping[str, Any]) -> bool:
    """Apply vehicular/4WD/surface/access filtering rules aligned with ``connectors/osm.py``."""
    if not parsed.is_vehicular or parsed.trail_class == "walking_track":
        return False

    highway = (parsed.highway or "").lower()
    if highway not in _ALLOWED_TRACK_HIGHWAYS:
        return False

    motor_vehicle = str(tags.get("motor_vehicle") or "").strip().lower()
    vehicle = str(tags.get("vehicle") or "").strip().lower()
    has_4wd_hint = parsed.is_4wd_only or parsed.is_4wd_recommended

    if (motor_vehicle == "no" or vehicle == "no") and not has_4wd_hint:
        return False

    surface_raw = str(tags.get("surface") or "").strip().lower()
    if highway == "service":
        service_sub = str(tags.get("service") or "").strip().lower()
        if service_sub in _EXCLUDED_SERVICE_SUBTYPES and not has_4wd_hint:
            return False
        if surface_raw in _EXPLICIT_SEALED_SURFACES and not has_4wd_hint:
            return False

    return not (
        highway == "unclassified"
        and surface_raw in _EXPLICIT_SEALED_SURFACES
        and not has_4wd_hint
        and parsed.trail_class == "unsealed_road"
    )


def parse_osm_waterway_feature(feat: Mapping[str, Any]) -> RawWaterwayFeature | None:
    """Normalize an osmium-exported OSM waterway feature into ``RawWaterwayFeature``."""
    geom = feat.get("geometry")
    if not isinstance(geom, Mapping) or geom.get("type") not in (
        "LineString",
        "MultiLineString",
        "Polygon",
        "MultiPolygon",
    ):
        return None
    raw_props = feat.get("properties") or {}
    waterway = str(raw_props.get("waterway") or "").strip().lower()
    if waterway not in _WATERWAY_VALUES:
        return None

    tags = _clean_osmium_props(raw_props)
    osm_type, numeric_id, _ = _extract_osm_meta(feat)
    if numeric_id is not None:
        prefix = osm_type or "way"
        feat_id = f"{prefix}/{numeric_id}"
    else:
        feat_id = deterministic_feature_id("osm-ww", geom, tags)

    intermittent = str(tags.get("intermittent") or "").strip().lower()
    seasonal = str(tags.get("seasonal") or "").strip().lower()
    if intermittent in ("yes", "true", "1") or seasonal in ("yes", "true", "1", "dry", "wet"):
        perenniality = "Intermittent"
    elif waterway in ("river", "stream", "creek"):
        perenniality = "Perennial"
    else:
        perenniality = "Unknown"

    hierarchy = "Major" if waterway == "river" else "Minor"
    feature_type = waterway.capitalize()

    return RawWaterwayFeature(
        source_provider="osm",
        source_license="ODbL",
        source_feature_id=feat_id,
        name=clean_feature_name(tags.get("name")),
        feature_type=feature_type,
        perenniality=perenniality,
        hierarchy=hierarchy,
        geometry=dict(geom),
    )


def _classify_osm_poi_type(tags: Mapping[str, Any]) -> PoiTypeLiteral | None:
    tourism = str(tags.get("tourism") or "").strip().lower()
    amenity = str(tags.get("amenity") or "").strip().lower()
    man_made = str(tags.get("man_made") or "").strip().lower()
    barrier = str(tags.get("barrier") or "").strip().lower()

    if tourism == "camp_site":
        return "campground"
    if tourism in ("alpine_hut", "wilderness_hut") or amenity == "shelter":
        return "hut"
    if man_made == "survey_point":
        return "trig"
    if tourism == "viewpoint":
        return "lookout"
    if tourism == "picnic_site":
        return "picnic"
    if amenity == "drinking_water":
        return "water_point"
    if amenity == "fuel":
        return "fuel"
    if barrier in _GATE_BARRIER_VALUES:
        return "gate"
    return None


def _compose_gate_access_note(tags: Mapping[str, Any]) -> str | None:
    parts: list[str] = []
    barrier = str(tags.get("barrier") or "").strip().lower()
    locked = str(tags.get("locked") or "").strip().lower()
    access = str(tags.get("access") or "").strip().lower()
    motor_vehicle = str(tags.get("motor_vehicle") or "").strip().lower()
    if locked in ("yes", "true", "1"):
        parts.append("locked")
    if barrier:
        parts.append(barrier.replace("_", " "))
    if access:
        parts.append(f"access={access}")
    if motor_vehicle and motor_vehicle != access:
        parts.append(f"motor_vehicle={motor_vehicle}")
    return "; ".join(parts) if parts else None


def parse_osm_poi_feature(feat: Mapping[str, Any]) -> RawPoiFeature | None:
    """Normalize an osmium-exported OSM Point feature into ``RawPoiFeature``."""
    geom = feat.get("geometry")
    if not isinstance(geom, Mapping) or geom.get("type") != "Point":
        return None
    coords = geom.get("coordinates")
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None

    raw_props = feat.get("properties") or {}
    tags = _clean_osmium_props(raw_props)
    poi_type = _classify_osm_poi_type(tags)
    if poi_type is None:
        return None

    lon = float(coords[0])
    lat = float(coords[1])
    osm_type, numeric_id, _ = _extract_osm_meta(feat)
    if numeric_id is not None:
        prefix = osm_type or "node"
        feat_id = f"{prefix}/{numeric_id}"
    else:
        feat_id = deterministic_feature_id("osm-poi", geom, tags)

    name = clean_feature_name(tags.get("name"))
    access_note = _compose_gate_access_note(tags) if poi_type == "gate" else None
    if name is None and poi_type == "gate":
        name = "Locked Gate" if str(tags.get("locked") or "").lower() == "yes" else "Gate"

    state = infer_state_from_lonlat(lon, lat)
    return RawPoiFeature(
        source_provider="osm",
        source_license="ODbL",
        source_feature_id=feat_id,
        name=name,
        poi_type=poi_type,
        state=state,  # type: ignore[arg-type]
        elev_m=parse_elevation_m(tags.get("ele")),
        access_note=access_note,
        raw_props=tags,
        geometry={"type": "Point", "coordinates": [lon, lat]},
    )


def _iter_line_coords(geom: Mapping[str, Any]) -> Iterator[tuple[float, float]]:
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "LineString":
        for pt in coords:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                yield (float(pt[0]), float(pt[1]))
    elif gtype == "MultiLineString":
        for part in coords:
            for pt in part:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    yield (float(pt[0]), float(pt[1]))


def parse_geojsonseq_stream(
    source: Path | str | TextIO | Iterable[str],
    *,
    bbox: BBox | None = None,
    vehicular_only: bool = True,
    source_path: str | None = None,
) -> PbfExtractResult:
    """Stream-parse GeoJSONSeq / GeoJSON into tracks, waterways, and outdoor POIs."""
    tracks: list[RawTrackFeature] = []
    waterways: list[RawWaterwayFeature] = []
    raw_pois: list[RawPoiFeature] = []
    gate_coord_buckets: set[tuple[int, int]] = set()

    seen_track_ids: set[str] = set()
    seen_ww_ids: set[str] = set()
    features_scanned = 0
    tracks_skipped = 0

    for feat in iter_geojsonseq_features(source):
        features_scanned += 1
        geom = feat.get("geometry")
        if not isinstance(geom, Mapping):
            continue
        gtype = geom.get("type")
        raw_props = feat.get("properties") or {}
        if not isinstance(raw_props, Mapping):
            continue

        if gtype == "Point":
            poi = parse_osm_poi_feature(feat)
            if poi is not None:
                raw_pois.append(poi)
                if poi.poi_type == "gate":
                    coords = poi.geometry.get("coordinates") or []
                    if len(coords) >= 2:
                        gate_coord_buckets.add(
                            (round(float(coords[0]) * 100_000), round(float(coords[1]) * 100_000))
                        )
            continue

        if gtype in ("LineString", "MultiLineString") and raw_props.get("highway"):
            tags = _clean_osmium_props(raw_props)
            _, numeric_id, osm_version = _extract_osm_meta(feat)
            if numeric_id is not None:
                tags["osm_way_id"] = numeric_id
            if osm_version is not None:
                tags["osm_version"] = osm_version
            normalized_feat = {
                "type": "Feature",
                "id": feat.get("id"),
                "properties": tags,
                "geometry": dict(geom),
            }
            parsed = parse_osm_track_feature(normalized_feat)
            if parsed is None:
                continue
            if vehicular_only and not _is_vehicular_pbf_track(parsed, tags):
                tracks_skipped += 1
                continue
            if parsed.source_id not in seen_track_ids:
                seen_track_ids.add(parsed.source_id)
                tracks.append(parsed)

        if raw_props.get("waterway"):
            ww = parse_osm_waterway_feature(feat)
            if ww is not None and ww.source_feature_id not in seen_ww_ids:
                seen_ww_ids.add(ww.source_feature_id)
                waterways.append(ww)

    # Propagate gate barrier flags from shared barrier=gate nodes onto intersecting OSM track ways
    if gate_coord_buckets and tracks:
        for track in tracks:
            if track.has_gate_barrier:
                continue
            for lon, lat in _iter_line_coords(track.geometry):
                if (round(lon * 100_000), round(lat * 100_000)) in gate_coord_buckets:
                    track.has_gate_barrier = True
                    break

    deduped_pois = dedupe_poi_features(raw_pois)
    return PbfExtractResult(
        tracks=tracks,
        waterways=waterways,
        pois=deduped_pois,
        features_scanned=features_scanned,
        tracks_skipped_non_vehicular=tracks_skipped,
        source_path=source_path,
        bbox=list(bbox.as_tuple()) if bbox is not None else None,
    )


def extract_pbf_bbox_to_geojsonseq(
    source_pbf: Path | str,
    out_geojsonseq: Path | str,
    *,
    bbox: BBox | None = None,
    work_dir: Path | None = None,
) -> Path:
    """Run ``osmium extract`` -> ``osmium tags-filter`` -> ``osmium export`` to GeoJSONSeq.

    Streams disk-to-disk so multi-hundred-MB Geofabrik state/Australia PBFs never load into RAM.
    """
    src = Path(source_pbf).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Source OSM PBF/XML file not found: {src}")

    dst = Path(out_geojsonseq).expanduser().resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="tf_pbf_",
        dir=str(work_dir) if work_dir is not None else None,
    ) as tmp_s:
        tmp_dir = Path(tmp_s)
        working_pbf = src

        if bbox is not None:
            clipped_pbf = tmp_dir / "bbox_clip.osm.pbf"
            bbox_str = (
                f"{bbox.min_x:.6f},{bbox.min_y:.6f},{bbox.max_x:.6f},{bbox.max_y:.6f}"
            )
            _run_osmium(
                [
                    "osmium",
                    "extract",
                    "-b",
                    bbox_str,
                    "--strategy=complete_ways",
                    "--no-progress",
                    str(src),
                    "-o",
                    str(clipped_pbf),
                    "--overwrite",
                ],
                step="osmium extract bbox",
            )
            working_pbf = clipped_pbf

        filtered_pbf = tmp_dir / "filtered.osm.pbf"
        _run_osmium(
            [
                "osmium",
                "tags-filter",
                "--no-progress",
                str(working_pbf),
                *PBF_TAGS_FILTER_EXPRESSIONS,
                "-o",
                str(filtered_pbf),
                "--overwrite",
            ],
            step="osmium tags-filter offroad layers",
        )

        # Release bbox clip immediately once filtered extract exists
        if working_pbf != src:
            working_pbf.unlink(missing_ok=True)

        _run_osmium(
            [
                "osmium",
                "export",
                "--no-progress",
                "-u",
                "type_id",
                "-a",
                "type,id,version",
                "--geometry-types=point,linestring",
                "-f",
                "geojsonseq",
                str(filtered_pbf),
                "-o",
                str(dst),
                "--overwrite",
            ],
            step="osmium export geojsonseq",
        )

    if not dst.is_file():
        raise RuntimeError(f"osmium export did not produce output file: {dst}")
    return dst


class StreamingPBFExtractor:
    """Memory-bounded (<300 MB) streaming OSM PBF extractor for corridor & statewide bakes."""

    name: str = "osm-pbf-streaming"
    attribution: str = ATTRIBUTION_OSM

    def __init__(
        self,
        source_pbf: Path | str | None = None,
        *,
        cache_dir: Path | str | None = None,
        pbf_url: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.source_pbf = (
            Path(source_pbf).expanduser().resolve() if source_pbf is not None else None
        )
        self.cache_dir = (
            Path(cache_dir).expanduser().resolve() if cache_dir is not None else None
        )
        self.pbf_url = pbf_url or os.environ.get("TRAILFORGE_AU_PBF_URL", DEFAULT_AU_PBF_URL)
        self._client = client

    def ensure_source_pbf(self) -> Path:
        """Return resolved local PBF path, stream-downloading from Geofabrik if needed."""
        if self.source_pbf is not None:
            if not self.source_pbf.is_file():
                raise FileNotFoundError(f"Source OSM PBF/XML not found: {self.source_pbf}")
            return self.source_pbf

        if self.cache_dir is None:
            raise ValueError(
                "Either source_pbf or cache_dir must be configured for StreamingPBFExtractor"
            )
        return download_osm_pbf(self.pbf_url, self.cache_dir, client=self._client)

    def extract_bbox(
        self,
        bbox: BBox | None = None,
        *,
        work_dir: Path | None = None,
        vehicular_only: bool = True,
    ) -> PbfExtractResult:
        """Extract and stream-parse tracks, waterways, and POIs for ``bbox`` in O(1) memory."""
        src = self.ensure_source_pbf()
        with tempfile.TemporaryDirectory(
            prefix="tf_pbf_stream_",
            dir=str(work_dir) if work_dir is not None else None,
        ) as tmp_s:
            tmp_dir = Path(tmp_s)
            seq_path = tmp_dir / "extract.geojsonseq"
            extract_pbf_bbox_to_geojsonseq(src, seq_path, bbox=bbox, work_dir=tmp_dir)
            result = parse_geojsonseq_stream(
                seq_path,
                bbox=bbox,
                vehicular_only=vehicular_only,
                source_path=str(src),
            )
            seq_path.unlink(missing_ok=True)
            return result
