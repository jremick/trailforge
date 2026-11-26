"""Vector and Terrain-RGB PMTiles build pipeline for TrailForge (Slice 03).

Generates:
  - ``trailforge_vector.pmtiles`` via Tippecanoe (z8-z15):
      * ``track_segments`` (graded canonical 50-100 m segments)
      * ``steep_sections`` (subset with ``is_steep_highlight == true``)
      * ``waterways`` (non-Connector watercourses)
      * ``pois`` (deduplicated backcountry POIs with outdoor SVG icon keys)
      * ``capad_parks`` (CAPAD 2025 protected area polygons)
      * ``contours`` (20 m minor + 100 m labeled index contours via ``gdal_contour``)
      * ``snowline_bands`` (polygonized elevation >= threshold polygons for 800..1600 m)
  - ``trailforge_terrain.pmtiles`` via GDAL + ``rio-rgbify`` + ``pmtiles convert``:
      * Mapbox Terrain-RGB (``-b -10000 -i 0.1``) raster tiles for MapLibre ``raster-dem``
  - ``manifest.json`` describing bounds, zooms, layers, snow-line thresholds, & attributions.

Zero PyQGIS dependency; enforces ``< 300 MB`` RAM GDAL streaming defaults and temp cleanup.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TextIO

import numpy as np
from pydantic import BaseModel, Field
from shapely.geometry import MultiPolygon, Polygon, box, mapping, shape
from shapely.ops import unary_union
from shapely.validation import make_valid
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trailforge.db.models import (
    POI,
    CAPADPark,
    SourceWaterway,
    Track,
    TrackSegment,
    TrackSegmentGrade,
)
from trailforge.ingestion.connectors.base import (
    ATTRIBUTION_CAPAD,
    ATTRIBUTION_GA_DEM,
    ATTRIBUTION_GA_HYDRO,
    ATTRIBUTION_NSW_SPATIAL,
    ATTRIBUTION_OPEN_METEO,
    ATTRIBUTION_OSM,
    ATTRIBUTION_VIC_DEECA,
    GDAL_STREAMING_ENV,
)
from trailforge.ingestion.terrain import DemSampler

logger = logging.getLogger(__name__)

VECTOR_PMTILES_FILENAME: Final[str] = "trailforge_vector.pmtiles"
TERRAIN_PMTILES_FILENAME: Final[str] = "trailforge_terrain.pmtiles"
MANIFEST_FILENAME: Final[str] = "manifest.json"

DEFAULT_VECTOR_MINZOOM: Final[int] = 8
DEFAULT_VECTOR_MAXZOOM: Final[int] = 15
DEFAULT_TERRAIN_MINZOOM: Final[int] = 8
DEFAULT_TERRAIN_MAXZOOM: Final[int] = 13
DEFAULT_CONTOUR_INTERVAL_M: Final[float] = 20.0
DEFAULT_CONTOUR_INDEX_M: Final[float] = 100.0
DEFAULT_SNOWLINE_BANDS_M: Final[tuple[int, ...]] = (800, 900, 1000, 1100, 1200, 1400, 1600)

# Mapbox Terrain-RGB parameters (locked v1 contract)
TERRAIN_RGB_BASE_VAL: Final[float] = -10000.0
TERRAIN_RGB_INTERVAL: Final[float] = 0.1

# Maps canonical poi_type -> SVG icon basename under assets/icons/ & web/public/icons/
POI_CATEGORY_TO_ICON: Final[dict[str, str]] = {
    "campground": "campground",
    "hut": "hut",
    "trig": "trig",
    "water_point": "water_bore",
    "fuel": "fuel",
    "picnic": "shelter",
    "lookout": "saddle",
    "trailhead": "saddle",
    "gate": "obstacle_tower",
}

DEFAULT_ATTRIBUTIONS: Final[tuple[str, ...]] = (
    ATTRIBUTION_OSM,
    ATTRIBUTION_GA_DEM,
    ATTRIBUTION_GA_HYDRO,
    ATTRIBUTION_CAPAD,
    ATTRIBUTION_NSW_SPATIAL,
    ATTRIBUTION_VIC_DEECA,
    ATTRIBUTION_OPEN_METEO,
)

VECTOR_LAYER_ORDER: Final[tuple[str, ...]] = (
    "capad_parks",
    "snowline_bands",
    "contours",
    "waterways",
    "track_segments",
    "steep_sections",
    "pois",
)


class TileManifest(BaseModel):
    """Public tile manifest served at ``GET /api/tiles/manifest.json``."""

    generated_at: str
    bounds: list[float] = Field(..., min_length=4, max_length=4)
    minzoom: int
    maxzoom: int
    vector_url: str = f"/api/tiles/{VECTOR_PMTILES_FILENAME}"
    terrain_url: str = f"/api/tiles/{TERRAIN_PMTILES_FILENAME}"
    vector_available: bool = False
    terrain_available: bool = False
    layers: list[str] = Field(default_factory=list)
    layer_counts: dict[str, int] = Field(default_factory=dict)
    snowline_bands_m: list[int] = Field(default_factory=lambda: list(DEFAULT_SNOWLINE_BANDS_M))
    terrain_encoding: str = "mapbox"
    attributions: list[str] = Field(default_factory=lambda: list(DEFAULT_ATTRIBUTIONS))


class TileBuildResult(BaseModel):
    """Artifacts and manifest produced by ``build_tiles``."""

    output_dir: Path
    manifest_path: Path
    vector_pmtiles: Path | None = None
    terrain_pmtiles: Path | None = None
    manifest: TileManifest


def _streaming_env() -> dict[str, str]:
    env = os.environ.copy()
    for key, val in GDAL_STREAMING_ENV.items():
        env.setdefault(key, val)
    return env


def _run_cmd(cmd: list[str], *, step: str) -> None:
    proc = subprocess.run(cmd, env=_streaming_env(), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{step} failed (exit={proc.returncode}).\n"
            f"Command: {' '.join(cmd)}\n"
            f"STDERR:\n{proc.stderr[-2500:]}"
        )


def _write_geojson_fc(path: Path, features: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"type": "FeatureCollection", "features": features}
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return path


class _StreamGeoJSONWriter:
    """Write an RFC 7946 FeatureCollection directly to disk in O(1) RAM."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.count = 0
        self._fh: TextIO | None = None

    def __enter__(self) -> _StreamGeoJSONWriter:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")
        self._fh.write('{"type":"FeatureCollection","features":[')
        return self

    def write_raw_feature(self, raw_json: str) -> None:
        if self._fh is None:
            raise RuntimeError("_StreamGeoJSONWriter must be entered as a context manager")
        if self.count > 0:
            self._fh.write(",\n")
        self._fh.write(raw_json)
        self.count += 1

    def write_feature(self, feature: dict[str, Any]) -> None:
        self.write_raw_feature(json.dumps(feature, separators=(",", ":")))

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._fh is not None:
            self._fh.write("]}\n")
            self._fh.close()
            self._fh = None
        if exc_type is not None or self.count == 0:
            self.path.unlink(missing_ok=True)


def _parse_postgis_box2d(raw: str | None) -> tuple[float, float, float, float] | None:
    """Parse PostGIS ``ST_Extent`` text ``BOX(minx miny,maxx maxy)`` into a float tuple."""
    if not raw:
        return None
    text = str(raw).strip()
    if text.upper().startswith("BOX(") and text.endswith(")"):
        inner = text[4:-1]
    else:
        return None
    parts = inner.split(",")
    if len(parts) != 2:
        return None
    lo = parts[0].strip().split()
    hi = parts[1].strip().split()
    if len(lo) != 2 or len(hi) != 2:
        return None
    return (float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1]))


def _query_layer_extent(
    session: Session,
    geom_col: Any,
    *where_clauses: Any,
) -> tuple[float, float, float, float] | None:
    stmt = select(func.ST_Extent(geom_col))
    if where_clauses:
        stmt = stmt.where(*where_clauses)
    raw = session.execute(stmt).scalar_one_or_none()
    return _parse_postgis_box2d(raw)


def _update_bounds(
    acc: list[float] | None, geom_bounds: tuple[float, float, float, float]
) -> list[float]:
    min_x, min_y, max_x, max_y = geom_bounds
    if acc is None:
        return [min_x, min_y, max_x, max_y]
    return [
        min(acc[0], min_x),
        min(acc[1], min_y),
        max(acc[2], max_x),
        max(acc[3], max_y),
    ]


def export_postgis_vector_layers(
    session: Session,
    work_dir: Path,
    *,
    bbox: tuple[float, float, float, float] | None = None,
) -> tuple[dict[str, Path], dict[str, int], list[float] | None]:
    """Stream canonical PostGIS vector layers into per-layer GeoJSON files in O(1) RAM.

    Returns ``(layer_paths, layer_counts, computed_bounds_wgs84)``.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    layer_paths: dict[str, Path] = {}
    layer_counts: dict[str, int] = {}
    bounds_acc: list[float] | None = None

    env_geom = (
        func.ST_MakeEnvelope(bbox[0], bbox[1], bbox[2], bbox[3], 4326) if bbox is not None else None
    )

    # 1. track_segments + steep_sections (lightweight scalar projection, streamed to disk)
    seg_where: list[Any] = [TrackSegment.is_active.is_(True)]
    if env_geom is not None:
        seg_where.append(func.ST_Intersects(TrackSegment.geom, env_geom))

    seg_extent = _query_layer_extent(session, TrackSegment.geom, *seg_where)
    if seg_extent is not None:
        bounds_acc = _update_bounds(bounds_acc, seg_extent)

    seg_stmt = (
        select(
            TrackSegment.segment_id,
            TrackSegment.track_id,
            TrackSegment.name.label("seg_name"),
            TrackSegment.max_slope_deg,
            TrackSegment.sustained_slope_deg,
            TrackSegment.elev_min_m,
            TrackSegment.elev_max_m,
            TrackSegment.is_steep_highlight,
            TrackSegment.is_above_snowline,
            TrackSegment.waterway_crossing_count,
            TrackSegment.surface_class,
            TrackSegment.tracktype,
            TrackSegment.is_4wd_only,
            TrackSegment.has_gate_barrier,
            TrackSegment.access_status,
            func.ST_AsGeoJSON(TrackSegment.geom, 6).label("geom_json"),
            TrackSegmentGrade.effective_grade,
            TrackSegmentGrade.baseline_grade,
            TrackSegmentGrade.live_grade,
            TrackSegmentGrade.live_score,
            TrackSegmentGrade.baseline_score,
            TrackSegmentGrade.confidence_level,
            TrackSegmentGrade.wet_risk_level,
            TrackSegmentGrade.live_breakdown,
            TrackSegmentGrade.rainfall_context,
            Track.name.label("track_name"),
        )
        .outerjoin(TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id)
        .outerjoin(Track, TrackSegment.track_id == Track.track_id)
        .where(*seg_where)
        .execution_options(yield_per=2000)
    )

    seg_out = work_dir / "track_segments.geojson"
    steep_out = work_dir / "steep_sections.geojson"
    with (
        _StreamGeoJSONWriter(seg_out) as seg_writer,
        _StreamGeoJSONWriter(steep_out) as steep_writer,
    ):
        for row in session.execute(seg_stmt):
            geom_json = row.geom_json
            if not geom_json:
                continue
            resolved_name = row.track_name or row.seg_name or "Unnamed Track"
            grade_val = (
                row.effective_grade
                if row.effective_grade
                else (row.baseline_grade if row.baseline_grade else "Easy")
            )
            score_val = (
                float(row.live_score)
                if row.live_score is not None
                else (float(row.baseline_score) if row.baseline_score is not None else 0.0)
            )
            live_bd = row.live_breakdown if isinstance(row.live_breakdown, dict) else {}
            rain_ctx = row.rainfall_context if isinstance(row.rainfall_context, dict) else {}
            raw_conf = row.confidence_level or "baseline_only"
            conf_level = "none" if raw_conf in ("", "baseline_only") else raw_conf
            wet_risk = str(rain_ctx.get("wet_risk_level") or (row.wet_risk_level or "dry"))

            seg_props: dict[str, Any] = {
                "segment_id": row.segment_id,
                "track_id": row.track_id,
                "track_name": resolved_name,
                "grade": grade_val,
                "score": round(score_val, 1),
                "live_grade": row.live_grade,
                "live_score": (
                    round(float(row.live_score), 1) if row.live_score is not None else None
                ),
                "confidence_level": conf_level,
                "is_obstacle_hotspot": bool(live_bd.get("is_obstacle_hotspot", False)),
                "wet_risk_level": wet_risk,
                "max_slope_deg": round(row.max_slope_deg, 2)
                if row.max_slope_deg is not None
                else 0.0,
                "sustained_slope_deg": (
                    round(row.sustained_slope_deg, 2)
                    if row.sustained_slope_deg is not None
                    else 0.0
                ),
                "min_elev_m": round(row.elev_min_m, 1) if row.elev_min_m is not None else None,
                "max_elev_m": round(row.elev_max_m, 1) if row.elev_max_m is not None else None,
                "is_steep_highlight": bool(row.is_steep_highlight),
                "is_above_snowline": bool(row.is_above_snowline),
                "waterway_crossing_count": int(row.waterway_crossing_count or 0),
                "surface_class": row.surface_class,
                "tracktype": row.tracktype,
                "is_4wd_only": bool(row.is_4wd_only),
                "has_gate_barrier": bool(row.has_gate_barrier),
                "access_status": row.access_status,
            }
            seg_props_json = json.dumps(seg_props, separators=(",", ":"))
            seg_writer.write_raw_feature(
                f'{{"type":"Feature","geometry":{geom_json},"properties":{seg_props_json}}}'
            )

            if row.is_steep_highlight:
                steep_props_json = json.dumps(
                    {
                        "segment_id": row.segment_id,
                        "track_id": row.track_id,
                        "track_name": resolved_name,
                        "max_slope_deg": (
                            round(row.max_slope_deg, 2) if row.max_slope_deg is not None else 0.0
                        ),
                        "grade": grade_val,
                    },
                    separators=(",", ":"),
                )
                steep_writer.write_raw_feature(
                    f'{{"type":"Feature","geometry":{geom_json},"properties":{steep_props_json}}}'
                )

    layer_counts["track_segments"] = seg_writer.count
    if seg_writer.count > 0:
        layer_paths["track_segments"] = seg_out

    layer_counts["steep_sections"] = steep_writer.count
    if steep_writer.count > 0:
        layer_paths["steep_sections"] = steep_out

    logger.info(
        "Exported %d track_segments and %d steep_sections to GeoJSON",
        seg_writer.count,
        steep_writer.count,
    )

    # 2. waterways (non-Connector)
    ww_where: list[Any] = [func.lower(SourceWaterway.feature_type) != "connector"]
    if env_geom is not None:
        ww_where.append(func.ST_Intersects(SourceWaterway.geom, env_geom))
    ww_extent = _query_layer_extent(session, SourceWaterway.geom, *ww_where)
    if ww_extent is not None:
        bounds_acc = _update_bounds(bounds_acc, ww_extent)

    ww_stmt = (
        select(
            SourceWaterway.name,
            SourceWaterway.feature_type,
            SourceWaterway.perenniality,
            func.ST_AsGeoJSON(SourceWaterway.geom, 6).label("geom_json"),
        )
        .where(*ww_where)
        .execution_options(yield_per=2000)
    )
    ww_out = work_dir / "waterways.geojson"
    with _StreamGeoJSONWriter(ww_out) as ww_writer:
        for row in session.execute(ww_stmt):
            if not row.geom_json:
                continue
            ww_props_json = json.dumps(
                {
                    "name": row.name,
                    "waterway_type": row.feature_type,
                    "perennial": (row.perenniality or "").strip().lower() == "perennial",
                },
                separators=(",", ":"),
            )
            ww_writer.write_raw_feature(
                f'{{"type":"Feature","geometry":{row.geom_json},"properties":{ww_props_json}}}'
            )
    layer_counts["waterways"] = ww_writer.count
    if ww_writer.count > 0:
        layer_paths["waterways"] = ww_out

    # 3. pois
    poi_where: list[Any] = []
    if env_geom is not None:
        poi_where.append(func.ST_Intersects(POI.geom, env_geom))
    poi_extent = _query_layer_extent(session, POI.geom, *poi_where)
    if poi_extent is not None:
        bounds_acc = _update_bounds(bounds_acc, poi_extent)

    poi_stmt = (
        select(
            POI.id,
            POI.name,
            POI.poi_type,
            POI.elev_m,
            func.ST_AsGeoJSON(POI.geom, 6).label("geom_json"),
        )
        .where(*poi_where)
        .execution_options(yield_per=2000)
    )
    poi_out = work_dir / "pois.geojson"
    with _StreamGeoJSONWriter(poi_out) as poi_writer:
        for row in session.execute(poi_stmt):
            if not row.geom_json:
                continue
            poi_props_json = json.dumps(
                {
                    "poi_id": str(row.id),
                    "name": row.name,
                    "category": row.poi_type,
                    "elevation_m": round(row.elev_m, 1) if row.elev_m is not None else None,
                    "icon": POI_CATEGORY_TO_ICON.get(row.poi_type, "saddle"),
                },
                separators=(",", ":"),
            )
            poi_writer.write_raw_feature(
                f'{{"type":"Feature","geometry":{row.geom_json},"properties":{poi_props_json}}}'
            )
    layer_counts["pois"] = poi_writer.count
    if poi_writer.count > 0:
        layer_paths["pois"] = poi_out

    # 4. capad_parks
    park_where: list[Any] = []
    if env_geom is not None:
        park_where.append(func.ST_Intersects(CAPADPark.geom, env_geom))
    park_extent = _query_layer_extent(session, CAPADPark.geom, *park_where)
    if park_extent is not None:
        bounds_acc = _update_bounds(bounds_acc, park_extent)

    park_stmt = (
        select(
            CAPADPark.name,
            CAPADPark.park_type,
            CAPADPark.type_abbr,
            CAPADPark.state,
            CAPADPark.iucn,
            func.ST_AsGeoJSON(CAPADPark.geom, 5).label("geom_json"),
        )
        .where(*park_where)
        .execution_options(yield_per=200)
    )
    park_out = work_dir / "capad_parks.geojson"
    with _StreamGeoJSONWriter(park_out) as park_writer:
        for row in session.execute(park_stmt):
            if not row.geom_json:
                continue
            park_props_json = json.dumps(
                {
                    "name": row.name,
                    "park_type": row.park_type,
                    "type_abbr": row.type_abbr,
                    "state": row.state,
                    "iucn": row.iucn,
                },
                separators=(",", ":"),
            )
            park_writer.write_raw_feature(
                f'{{"type":"Feature","geometry":{row.geom_json},"properties":{park_props_json}}}'
            )
    layer_counts["capad_parks"] = park_writer.count
    if park_writer.count > 0:
        layer_paths["capad_parks"] = park_out

    logger.info(
        "Exported waterways=%d pois=%d capad_parks=%d",
        ww_writer.count,
        poi_writer.count,
        park_writer.count,
    )

    return (layer_paths, layer_counts, bounds_acc)


def generate_contours_geojson(
    dem_path: Path,
    out_geojson: Path,
    *,
    interval_m: float = DEFAULT_CONTOUR_INTERVAL_M,
    index_interval_m: float = DEFAULT_CONTOUR_INDEX_M,
) -> int:
    """Generate topographic contour lines via ``gdal_contour`` streaming in O(1) RAM."""
    if shutil.which("gdal_contour") is None:
        raise RuntimeError("gdal_contour binary not found in PATH")

    src = Path(dem_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"DEM file not found for contour generation: {src}")

    out_geojson.parent.mkdir(parents=True, exist_ok=True)
    raw_seq = out_geojson.with_suffix(".raw.geojsonseq")
    raw_seq.unlink(missing_ok=True)
    try:
        _run_cmd(
            [
                "gdal_contour",
                "-a",
                "elev_m",
                "-i",
                f"{interval_m:g}",
                "-f",
                "GeoJSONSeq",
                str(src),
                str(raw_seq),
            ],
            step="gdal_contour",
        )
        with (
            raw_seq.open("r", encoding="utf-8") as raw_fh,
            _StreamGeoJSONWriter(out_geojson) as writer,
        ):
            for raw_line in raw_fh:
                line = raw_line.lstrip("\x1e").strip()
                if not line:
                    continue
                feat = json.loads(line)
                geom = feat.get("geometry")
                if not geom:
                    continue
                props = feat.get("properties") or {}
                raw_elev = props.get("elev_m")
                if raw_elev is None:
                    continue
                elev_f = float(raw_elev)
                elev_int = int(round(elev_f))
                is_index = abs(elev_f - round(elev_f / index_interval_m) * index_interval_m) < 1e-3
                writer.write_feature(
                    {
                        "type": "Feature",
                        "tippecanoe": {"minzoom": 9 if is_index else 11},
                        "geometry": geom,
                        "properties": {
                            "elev_m": elev_int,
                            "is_index": is_index,
                        },
                    }
                )
        count = writer.count
    finally:
        raw_seq.unlink(missing_ok=True)

    logger.info("Generated %d contour lines from %s", count, src.name)
    return count


def _polygonize_mask_pure_python(
    mask: np.ndarray,
    origin_x: float,
    dx: float,
    origin_y: float,
    dy: float,
) -> Polygon | MultiPolygon | None:
    """Run-length row-strip polygonization of a 2D boolean mask (pure Python + Shapely)."""
    rows, cols = mask.shape
    rects: list[Polygon] = []
    for r in range(rows):
        row_arr = mask[r]
        if not np.any(row_arr):
            continue
        # Detect contiguous horizontal True runs in this row
        padded = np.concatenate(([False], row_arr, [False]))
        diffs = np.diff(padded.astype(np.int8))
        starts = np.flatnonzero(diffs == 1)
        ends = np.flatnonzero(diffs == -1)
        y_top = origin_y - r * dy
        y_bot = origin_y - (r + 1) * dy
        for c0, c1 in zip(starts, ends, strict=True):
            rects.append(box(origin_x + int(c0) * dx, y_bot, origin_x + int(c1) * dx, y_top))
    if not rects:
        return None
    merged = unary_union(rects)
    if merged.is_empty:
        return None
    return merged  # type: ignore[return-value]


def _extract_polygonal(geom: Any) -> Polygon | MultiPolygon | None:
    if geom is None or geom.is_empty:
        return None
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    polys: list[Polygon] = []
    for part in getattr(geom, "geoms", ()):
        if isinstance(part, Polygon) and not part.is_empty:
            polys.append(part)
        elif isinstance(part, MultiPolygon):
            polys.extend(p for p in part.geoms if not p.is_empty)
    if not polys:
        return None
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


def generate_snowline_bands_geojson(
    dem_path: Path,
    out_geojson: Path,
    *,
    thresholds_m: Sequence[int] = DEFAULT_SNOWLINE_BANDS_M,
    force_pure_python: bool = False,
) -> tuple[int, list[float]]:
    """Polygonize elevation >= threshold snow-line bands in EPSG:4326.

    Emits one MultiPolygon/Polygon feature per non-empty threshold with ``{"elev_min_m": T}``
    so MapLibre can filter ``["==", ["get", "elev_min_m"], threshold]`` cleanly at runtime.

    Uses ``rasterio.features.shapes`` when available and falls back cleanly to run-length
    Shapely polygonization on hosts without ``rasterio``.
    """
    src = Path(dem_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"DEM file not found for snow-line bands: {src}")

    use_rasterio = not force_pure_python
    if use_rasterio:
        try:
            import rasterio  # type: ignore[import-not-found]
            import rasterio.features  # type: ignore[import-not-found]
        except ImportError:
            use_rasterio = False

    if use_rasterio:
        import rasterio  # type: ignore[import-not-found]
        import rasterio.enums  # type: ignore[import-not-found]
        import rasterio.features  # type: ignore[import-not-found]

        with rasterio.open(src) as ds:
            max_pixels = 2_000_000
            total_pixels = int(ds.width) * int(ds.height)
            if total_pixels > max_pixels:
                scale = math.sqrt(max_pixels / total_pixels)
                out_h = max(64, int(round(ds.height * scale)))
                out_w = max(64, int(round(ds.width * scale)))
                arr = ds.read(
                    1,
                    out_shape=(out_h, out_w),
                    resampling=rasterio.enums.Resampling.bilinear,
                ).astype(np.float32, copy=False)
                transform = ds.transform * ds.transform.scale(
                    ds.width / float(out_w), ds.height / float(out_h)
                )
            else:
                arr = ds.read(1).astype(np.float32, copy=False)
                transform = ds.transform
            nodata = float(ds.nodata) if ds.nodata is not None else None
            dx = abs(float(transform.a))
            bounds = [
                round(float(ds.bounds.left), 6),
                round(float(ds.bounds.bottom), 6),
                round(float(ds.bounds.right), 6),
                round(float(ds.bounds.top), 6),
            ]
    else:
        with DemSampler(src, force_pure_python=True) as sampler:
            arr, nodata = sampler.read_array()
            min_x, min_y, max_x, max_y = sampler.bounds
            dx = (max_x - min_x) / sampler.width
            dy = (max_y - min_y) / sampler.height
            origin_x = min_x
            origin_y = max_y
            bounds = [
                round(min_x, 6),
                round(min_y, 6),
                round(max_x, 6),
                round(max_y, 6),
            ]

    valid = np.isfinite(arr)
    if nodata is not None and math.isfinite(nodata):
        valid &= np.abs(arr - np.float32(nodata)) >= 1e-3

    features: list[dict[str, Any]] = []
    simplify_tol = dx * 0.35

    for threshold in sorted({int(t) for t in thresholds_m}):
        mask = valid & (arr >= np.float32(threshold))
        if not np.any(mask):
            continue

        if use_rasterio:
            import rasterio.features  # type: ignore[import-not-found]

            mask_u8 = mask.astype(np.uint8)
            polys = [
                shape(geom_dict)
                for geom_dict, val in rasterio.features.shapes(
                    mask_u8, mask=mask, transform=transform
                )
                if int(val) == 1
            ]
            merged = unary_union(polys) if polys else None
        else:
            merged = _polygonize_mask_pure_python(mask, origin_x, dx, origin_y, dy)

        if merged is None or merged.is_empty:
            continue

        cleaned = make_valid(merged.simplify(simplify_tol, preserve_topology=True))
        polygonal = _extract_polygonal(cleaned)
        if polygonal is None or polygonal.is_empty:
            continue

        features.append(
            {
                "type": "Feature",
                "geometry": dict(mapping(polygonal)),
                "properties": {"elev_min_m": threshold},
            }
        )

    _write_geojson_fc(out_geojson, features)
    return (len(features), bounds)


def build_vector_pmtiles(
    layer_files: Mapping[str, Path],
    out_pmtiles: Path,
    *,
    minzoom: int = DEFAULT_VECTOR_MINZOOM,
    maxzoom: int = DEFAULT_VECTOR_MAXZOOM,
) -> Path:
    """Compile per-layer GeoJSON files into a single multi-layer vector ``PMTiles`` archive."""
    if shutil.which("tippecanoe") is None:
        raise RuntimeError("tippecanoe binary not found in PATH")

    valid_layers = [
        (name, Path(path))
        for name, path in layer_files.items()
        if Path(path).is_file() and Path(path).stat().st_size > 0
    ]
    if not valid_layers:
        raise ValueError("No non-empty vector layer GeoJSON files provided for tippecanoe")

    out_pmtiles.parent.mkdir(parents=True, exist_ok=True)
    # Tippecanoe determines PMTiles vs MBTiles output format strictly from the .pmtiles extension
    tmp_pmtiles = out_pmtiles.with_name(f".{out_pmtiles.stem}.tmp.pmtiles")
    tmp_pmtiles.unlink(missing_ok=True)

    cmd = [
        "tippecanoe",
        "-o",
        str(tmp_pmtiles),
        "--force",
        f"--minimum-zoom={minzoom}",
        f"--maximum-zoom={maxzoom}",
        "--drop-densest-as-needed",
        "--maximum-tile-bytes=1500000",
        "--detect-shared-borders",
        "--name=TrailForge Vector Tiles",
        f"--attribution={ATTRIBUTION_OSM}",
    ]
    for layer_name, geojson_path in valid_layers:
        cmd.extend(["-L", f"{layer_name}:{geojson_path}"])

    logger.info(
        "Running tippecanoe across %d layers (z%d..z%d) -> %s",
        len(valid_layers),
        minzoom,
        maxzoom,
        out_pmtiles.name,
    )
    try:
        _run_cmd(cmd, step="tippecanoe vector PMTiles")
        tmp_pmtiles.replace(out_pmtiles)
    except Exception:
        tmp_pmtiles.unlink(missing_ok=True)
        raise

    logger.info(
        "Built vector PMTiles %s (%.2f MB)",
        out_pmtiles.name,
        out_pmtiles.stat().st_size / (1024.0 * 1024.0),
    )
    return out_pmtiles


def _encode_dem_to_terrain_rgb_geotiff(
    dem_3857_tif: Path,
    out_rgb_tif: Path,
    *,
    base_val: float = TERRAIN_RGB_BASE_VAL,
    interval: float = TERRAIN_RGB_INTERVAL,
) -> Path:
    """Encode a Web Mercator Float32 DEM GeoTIFF into 3-band uint8 Mapbox Terrain-RGB in strips."""
    try:
        import rasterio  # type: ignore[import-not-found]
        from rasterio.windows import Window  # type: ignore[import-not-found]
        from rio_rgbify.encoders import data_to_rgb  # type: ignore[import-not-found]

        with rasterio.open(dem_3857_tif) as src:
            nodata = src.nodata
            has_finite_nodata = nodata is not None and math.isfinite(float(nodata))
            nodata_f32 = np.float32(nodata) if has_finite_nodata else np.float32(0.0)
            profile = src.profile.copy()
            profile.update(
                driver="GTiff",
                dtype="uint8",
                count=3,
                nodata=None,
                tiled=True,
                compress="deflate",
            )
            strip_rows = 512
            with rasterio.open(out_rgb_tif, "w", **profile) as dst:
                for row_off in range(0, src.height, strip_rows):
                    win_h = min(strip_rows, src.height - row_off)
                    window = Window(0, row_off, src.width, win_h)
                    arr = src.read(1, window=window).astype(np.float32, copy=False)
                    invalid = ~np.isfinite(arr)
                    if has_finite_nodata:
                        invalid |= np.abs(arr - nodata_f32) < 1e-3
                    if np.any(invalid):
                        arr = np.where(invalid, np.float32(0.0), arr)
                    rgb = data_to_rgb(arr, baseval=base_val, interval=interval).astype(
                        np.uint8, copy=False
                    )
                    dst.write(rgb, window=window)
        return out_rgb_tif
    except ImportError:
        # CLI fallback using gdal_translate + rio rgbify
        cleaned = dem_3857_tif.with_name("dem_3857_nonodata.tif")
        _run_cmd(
            [
                "gdal_translate",
                "-a_nodata",
                "none",
                "-co",
                "TILED=YES",
                str(dem_3857_tif),
                str(cleaned),
            ],
            step="gdal_translate strip nodata for rio rgbify",
        )
        _run_cmd(
            [
                "rio",
                "rgbify",
                "-b",
                f"{base_val:g}",
                "-i",
                f"{interval:g}",
                "-j",
                "1",
                str(cleaned),
                str(out_rgb_tif),
            ],
            step="rio rgbify",
        )
        return out_rgb_tif


def _normalize_mbtiles_metadata(
    mbtiles_path: Path,
    *,
    bounds_wgs84: Sequence[float],
    name: str = "TrailForge Terrain-RGB",
) -> None:
    """Fix GDAL MBTiles center/bounds/description metadata before ``pmtiles convert``."""
    w, s, e, n = (float(v) for v in bounds_wgs84[:4])
    c_lon = round((w + e) / 2.0, 6)
    c_lat = round((s + n) / 2.0, 6)

    conn = sqlite3.connect(str(mbtiles_path))
    try:
        cur = conn.cursor()
        row = cur.execute("SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles").fetchone()
        min_z = int(row[0]) if row and row[0] is not None else DEFAULT_TERRAIN_MINZOOM
        max_z = int(row[1]) if row and row[1] is not None else DEFAULT_TERRAIN_MAXZOOM
        center_z = max(min_z, min(max_z, 11))
        updates = {
            "name": name,
            "description": "Mapbox Terrain-RGB elevation tiles (base=-10000, interval=0.1, AHD)",
            "attribution": ATTRIBUTION_GA_DEM,
            "format": "png",
            "type": "baselayer",
            "bounds": f"{w:.6f},{s:.6f},{e:.6f},{n:.6f}",
            "center": f"{c_lon:.6f},{c_lat:.6f},{center_z}",
            "minzoom": str(min_z),
            "maxzoom": str(max_z),
        }
        for k, v in updates.items():
            cur.execute("INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)", (k, v))
        conn.commit()
    finally:
        conn.close()


def build_terrain_pmtiles(
    dem_path: Path,
    out_pmtiles: Path,
    *,
    minzoom: int = DEFAULT_TERRAIN_MINZOOM,
    maxzoom: int = DEFAULT_TERRAIN_MAXZOOM,
    base_val: float = TERRAIN_RGB_BASE_VAL,
    interval: float = TERRAIN_RGB_INTERVAL,
) -> Path:
    """Build Mapbox Terrain-RGB ``PMTiles`` from a GeoTIFF DEM across ``minzoom..maxzoom``.

    Resamples continuous Float32 elevation via ``gdalwarp -r bilinear`` at each zoom level
    *before* Terrain-RGB encoding so overview tiles have zero RGB carry-byte artifacts and
    exact zoom coverage down to ``minzoom`` (even on compact corridors).
    """
    for binary in ("gdalwarp", "gdal_translate", "pmtiles"):
        if shutil.which(binary) is None:
            raise RuntimeError(f"Required terrain tiling binary not found in PATH: {binary}")

    src = Path(dem_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"DEM file not found for terrain PMTiles: {src}")

    if minzoom > maxzoom:
        raise ValueError(f"minzoom ({minzoom}) must be <= maxzoom ({maxzoom})")

    with DemSampler(src) as sampler:
        min_x, min_y, max_x, max_y = sampler.bounds
        bounds_wgs84 = [min_x, min_y, max_x, max_y]

    out_pmtiles.parent.mkdir(parents=True, exist_ok=True)
    tmp_pmtiles = out_pmtiles.with_name(f".{out_pmtiles.stem}.tmp.pmtiles")
    tmp_pmtiles.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="tf_terrain_") as tmp_dir_s:
        tmp_dir = Path(tmp_dir_s)
        merged_mbtiles = tmp_dir / "terrain.mbtiles"
        merged_conn = sqlite3.connect(str(merged_mbtiles))
        try:
            merged_conn.execute("CREATE TABLE metadata (name TEXT UNIQUE, value TEXT)")
            merged_conn.execute(
                "CREATE TABLE tiles ("
                "zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB, "
                "UNIQUE (zoom_level, tile_column, tile_row))"
            )
            merged_conn.commit()

            for z in range(minzoom, maxzoom + 1):
                res_m = 156543.03392804097 / (2**z)
                dem_z = tmp_dir / f"dem_z{z}.tif"
                rgb_z = tmp_dir / f"rgb_z{z}.tif"
                mb_z = tmp_dir / f"terrain_z{z}.mbtiles"

                _run_cmd(
                    [
                        "gdalwarp",
                        "-overwrite",
                        "-t_srs",
                        "EPSG:3857",
                        "-r",
                        "bilinear",
                        "-tr",
                        f"{res_m:.6f}",
                        f"{res_m:.6f}",
                        "-co",
                        "TILED=YES",
                        "-co",
                        "COMPRESS=DEFLATE",
                        str(src),
                        str(dem_z),
                    ],
                    step=f"gdalwarp DEM to EPSG:3857 (z{z})",
                )
                _encode_dem_to_terrain_rgb_geotiff(
                    dem_z, rgb_z, base_val=base_val, interval=interval
                )
                _run_cmd(
                    [
                        "gdal_translate",
                        "-of",
                        "MBTILES",
                        "-co",
                        "TILE_FORMAT=PNG",
                        "-co",
                        "ZOOM_LEVEL_STRATEGY=AUTO",
                        str(rgb_z),
                        str(mb_z),
                    ],
                    step=f"gdal_translate Terrain-RGB to MBTiles (z{z})",
                )

                z_conn = sqlite3.connect(str(mb_z))
                tile_count = 0
                try:
                    z_cur = z_conn.execute(
                        "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"
                    )
                    while True:
                        batch = z_cur.fetchmany(500)
                        if not batch:
                            break
                        merged_conn.executemany(
                            "INSERT OR REPLACE INTO tiles "
                            "(zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)",
                            batch,
                        )
                        tile_count += len(batch)
                finally:
                    z_conn.close()

                merged_conn.commit()
                logger.info("Built Terrain-RGB z%d (%d PNG tiles)", z, tile_count)

                # Reclaim per-zoom temp files immediately to stay well below memory/disk caps
                dem_z.unlink(missing_ok=True)
                rgb_z.unlink(missing_ok=True)
                mb_z.unlink(missing_ok=True)
        finally:
            merged_conn.close()

        _normalize_mbtiles_metadata(merged_mbtiles, bounds_wgs84=bounds_wgs84)

        try:
            _run_cmd(
                ["pmtiles", "convert", "--force", str(merged_mbtiles), str(tmp_pmtiles)],
                step="pmtiles convert terrain MBTiles",
            )
            tmp_pmtiles.replace(out_pmtiles)
        except Exception:
            tmp_pmtiles.unlink(missing_ok=True)
            raise

    return out_pmtiles


def build_tiles(
    session: Session,
    output_dir: Path | str,
    *,
    dem_path: Path | str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    minzoom: int = DEFAULT_VECTOR_MINZOOM,
    maxzoom: int = DEFAULT_VECTOR_MAXZOOM,
    terrain_minzoom: int = DEFAULT_TERRAIN_MINZOOM,
    terrain_maxzoom: int = DEFAULT_TERRAIN_MAXZOOM,
    contour_interval_m: float = DEFAULT_CONTOUR_INTERVAL_M,
    snowline_bands_m: Sequence[int] = DEFAULT_SNOWLINE_BANDS_M,
    build_vector: bool = True,
    build_terrain: bool = True,
) -> TileBuildResult:
    """Build corridor/regional vector + Terrain-RGB PMTiles and write ``manifest.json``."""
    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    resolved_dem = Path(dem_path).expanduser().resolve() if dem_path is not None else None
    vector_out = out_dir / VECTOR_PMTILES_FILENAME
    terrain_out = out_dir / TERRAIN_PMTILES_FILENAME

    layer_counts: dict[str, int] = {}
    layers_present: list[str] = []
    bounds_acc: list[float] | None = list(bbox) if bbox is not None else None
    vector_built = False
    terrain_built = False

    with tempfile.TemporaryDirectory(prefix="tf_tiles_build_") as tmp_s:
        tmp_dir = Path(tmp_s)

        if build_vector:
            layer_paths, layer_counts, db_bounds = export_postgis_vector_layers(
                session, tmp_dir, bbox=bbox
            )
            if db_bounds is not None:
                bounds_acc = _update_bounds(bounds_acc, tuple(db_bounds))  # type: ignore[arg-type]

            if resolved_dem is not None:
                contours_path = tmp_dir / "contours.geojson"
                contour_count = generate_contours_geojson(
                    resolved_dem,
                    contours_path,
                    interval_m=contour_interval_m,
                    index_interval_m=DEFAULT_CONTOUR_INDEX_M,
                )
                layer_counts["contours"] = contour_count
                if contour_count > 0:
                    layer_paths["contours"] = contours_path

                snow_path = tmp_dir / "snowline_bands.geojson"
                snow_count, dem_bounds = generate_snowline_bands_geojson(
                    resolved_dem,
                    snow_path,
                    thresholds_m=snowline_bands_m,
                )
                layer_counts["snowline_bands"] = snow_count
                if snow_count > 0:
                    layer_paths["snowline_bands"] = snow_path
                bounds_acc = _update_bounds(bounds_acc, tuple(dem_bounds))  # type: ignore[arg-type]

            ordered_layer_files = {
                name: layer_paths[name] for name in VECTOR_LAYER_ORDER if name in layer_paths
            }
            if ordered_layer_files:
                build_vector_pmtiles(
                    ordered_layer_files, vector_out, minzoom=minzoom, maxzoom=maxzoom
                )
                layers_present = list(ordered_layer_files.keys())
                vector_built = True

        if build_terrain and resolved_dem is not None:
            build_terrain_pmtiles(
                resolved_dem,
                terrain_out,
                minzoom=terrain_minzoom,
                maxzoom=terrain_maxzoom,
            )
            terrain_built = True
            if bounds_acc is None:
                with DemSampler(resolved_dem) as sampler:
                    bounds_acc = list(sampler.bounds)

    final_bounds = (
        [round(v, 6) for v in bounds_acc]
        if bounds_acc is not None
        else [148.05, -36.68, 148.2, -36.55]
    )
    manifest = TileManifest(
        generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        bounds=final_bounds,
        minzoom=minzoom,
        maxzoom=maxzoom,
        vector_url=f"/api/tiles/{VECTOR_PMTILES_FILENAME}",
        terrain_url=f"/api/tiles/{TERRAIN_PMTILES_FILENAME}",
        vector_available=vector_built or vector_out.is_file(),
        terrain_available=terrain_built or terrain_out.is_file(),
        layers=layers_present,
        layer_counts=layer_counts,
        snowline_bands_m=[int(t) for t in sorted({int(v) for v in snowline_bands_m})],
        terrain_encoding="mapbox",
        attributions=list(DEFAULT_ATTRIBUTIONS),
    )

    manifest_path = out_dir / MANIFEST_FILENAME
    tmp_manifest = out_dir / f"{MANIFEST_FILENAME}.tmp"
    tmp_manifest.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp_manifest.replace(manifest_path)

    return TileBuildResult(
        output_dir=out_dir,
        manifest_path=manifest_path,
        vector_pmtiles=vector_out if vector_out.is_file() else None,
        terrain_pmtiles=terrain_out if terrain_out.is_file() else None,
        manifest=manifest,
    )
