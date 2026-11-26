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
from typing import Any, Final

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
    """Export canonical PostGIS vector layers into per-layer GeoJSON files for Tippecanoe.

    Returns ``(layer_paths, layer_counts, computed_bounds_wgs84)``.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    layer_paths: dict[str, Path] = {}
    layer_counts: dict[str, int] = {}
    bounds_acc: list[float] | None = None

    env_geom = (
        func.ST_MakeEnvelope(bbox[0], bbox[1], bbox[2], bbox[3], 4326) if bbox is not None else None
    )

    # 1. track_segments + steep_sections
    seg_stmt = (
        select(
            TrackSegment,
            func.ST_AsGeoJSON(TrackSegment.geom).label("geom_json"),
            TrackSegmentGrade,
            Track.name.label("track_name"),
        )
        .outerjoin(TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id)
        .outerjoin(Track, TrackSegment.track_id == Track.track_id)
        .where(TrackSegment.is_active.is_(True))
        .order_by(TrackSegment.track_id.asc(), TrackSegment.segment_seq.asc().nullslast())
    )
    if env_geom is not None:
        seg_stmt = seg_stmt.where(func.ST_Intersects(TrackSegment.geom, env_geom))

    seg_features: list[dict[str, Any]] = []
    steep_features: list[dict[str, Any]] = []
    for seg, geom_json, grd, track_name in session.execute(seg_stmt).all():
        geom_obj = json.loads(geom_json)
        shp = shape(geom_obj)
        if not shp.is_empty:
            bounds_acc = _update_bounds(bounds_acc, shp.bounds)

        resolved_name = track_name or seg.name or "Unnamed Track"
        grade_val = (
            grd.effective_grade
            if grd is not None and grd.effective_grade
            else (grd.baseline_grade if grd is not None else "Easy")
        )
        score_val = float(grd.baseline_score) if grd is not None else 0.0

        seg_props: dict[str, Any] = {
            "segment_id": seg.segment_id,
            "track_id": seg.track_id,
            "track_name": resolved_name,
            "grade": grade_val,
            "score": round(score_val, 1),
            "max_slope_deg": round(seg.max_slope_deg, 2) if seg.max_slope_deg is not None else 0.0,
            "sustained_slope_deg": (
                round(seg.sustained_slope_deg, 2) if seg.sustained_slope_deg is not None else 0.0
            ),
            "min_elev_m": round(seg.elev_min_m, 1) if seg.elev_min_m is not None else None,
            "max_elev_m": round(seg.elev_max_m, 1) if seg.elev_max_m is not None else None,
            "is_steep_highlight": bool(seg.is_steep_highlight),
            "is_above_snowline": bool(seg.is_above_snowline),
            "waterway_crossing_count": int(seg.waterway_crossing_count),
            "surface_class": seg.surface_class,
            "tracktype": seg.tracktype,
            "is_4wd_only": bool(seg.is_4wd_only),
            "has_gate_barrier": bool(seg.has_gate_barrier),
            "access_status": seg.access_status,
        }
        seg_features.append({"type": "Feature", "geometry": geom_obj, "properties": seg_props})

        if seg.is_steep_highlight:
            steep_features.append(
                {
                    "type": "Feature",
                    "geometry": geom_obj,
                    "properties": {
                        "segment_id": seg.segment_id,
                        "track_id": seg.track_id,
                        "track_name": resolved_name,
                        "max_slope_deg": (
                            round(seg.max_slope_deg, 2) if seg.max_slope_deg is not None else 0.0
                        ),
                        "grade": grade_val,
                    },
                }
            )

    layer_counts["track_segments"] = len(seg_features)
    if seg_features:
        layer_paths["track_segments"] = _write_geojson_fc(
            work_dir / "track_segments.geojson", seg_features
        )

    layer_counts["steep_sections"] = len(steep_features)
    if steep_features:
        layer_paths["steep_sections"] = _write_geojson_fc(
            work_dir / "steep_sections.geojson", steep_features
        )

    # 2. waterways (non-Connector)
    ww_stmt = (
        select(SourceWaterway, func.ST_AsGeoJSON(SourceWaterway.geom).label("geom_json"))
        .where(func.lower(SourceWaterway.feature_type) != "connector")
        .order_by(SourceWaterway.id.asc())
    )
    if env_geom is not None:
        ww_stmt = ww_stmt.where(func.ST_Intersects(SourceWaterway.geom, env_geom))

    ww_features: list[dict[str, Any]] = []
    for ww, geom_json in session.execute(ww_stmt).all():
        geom_obj = json.loads(geom_json)
        shp = shape(geom_obj)
        if not shp.is_empty:
            bounds_acc = _update_bounds(bounds_acc, shp.bounds)
        ww_features.append(
            {
                "type": "Feature",
                "geometry": geom_obj,
                "properties": {
                    "name": ww.name,
                    "waterway_type": ww.feature_type,
                    "perennial": ww.perenniality.strip().lower() == "perennial",
                },
            }
        )
    layer_counts["waterways"] = len(ww_features)
    if ww_features:
        layer_paths["waterways"] = _write_geojson_fc(work_dir / "waterways.geojson", ww_features)

    # 3. pois
    poi_stmt = select(POI, func.ST_AsGeoJSON(POI.geom).label("geom_json")).order_by(POI.id.asc())
    if env_geom is not None:
        poi_stmt = poi_stmt.where(func.ST_Intersects(POI.geom, env_geom))

    poi_features: list[dict[str, Any]] = []
    for poi, geom_json in session.execute(poi_stmt).all():
        geom_obj = json.loads(geom_json)
        shp = shape(geom_obj)
        if not shp.is_empty:
            bounds_acc = _update_bounds(bounds_acc, shp.bounds)
        poi_features.append(
            {
                "type": "Feature",
                "geometry": geom_obj,
                "properties": {
                    "poi_id": str(poi.id),
                    "name": poi.name,
                    "category": poi.poi_type,
                    "elevation_m": round(poi.elev_m, 1) if poi.elev_m is not None else None,
                    "icon": POI_CATEGORY_TO_ICON.get(poi.poi_type, "saddle"),
                },
            }
        )
    layer_counts["pois"] = len(poi_features)
    if poi_features:
        layer_paths["pois"] = _write_geojson_fc(work_dir / "pois.geojson", poi_features)

    # 4. capad_parks
    park_stmt = select(
        CAPADPark, func.ST_AsGeoJSON(CAPADPark.geom).label("geom_json")
    ).order_by(CAPADPark.id.asc())
    if env_geom is not None:
        park_stmt = park_stmt.where(func.ST_Intersects(CAPADPark.geom, env_geom))

    park_features: list[dict[str, Any]] = []
    for park, geom_json in session.execute(park_stmt).all():
        geom_obj = json.loads(geom_json)
        shp = shape(geom_obj)
        if not shp.is_empty:
            bounds_acc = _update_bounds(bounds_acc, shp.bounds)
        park_features.append(
            {
                "type": "Feature",
                "geometry": geom_obj,
                "properties": {
                    "name": park.name,
                    "park_type": park.park_type,
                    "type_abbr": park.type_abbr,
                    "state": park.state,
                    "iucn": park.iucn,
                },
            }
        )
    layer_counts["capad_parks"] = len(park_features)
    if park_features:
        layer_paths["capad_parks"] = _write_geojson_fc(
            work_dir / "capad_parks.geojson", park_features
        )

    return (layer_paths, layer_counts, bounds_acc)


def generate_contours_geojson(
    dem_path: Path,
    out_geojson: Path,
    *,
    interval_m: float = DEFAULT_CONTOUR_INTERVAL_M,
    index_interval_m: float = DEFAULT_CONTOUR_INDEX_M,
) -> int:
    """Generate topographic contour lines via ``gdal_contour`` with ``elev_m`` and ``is_index``."""
    if shutil.which("gdal_contour") is None:
        raise RuntimeError("gdal_contour binary not found in PATH")

    src = Path(dem_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"DEM file not found for contour generation: {src}")

    out_geojson.parent.mkdir(parents=True, exist_ok=True)
    raw_geojson = out_geojson.with_suffix(".raw.geojson")
    raw_geojson.unlink(missing_ok=True)
    try:
        _run_cmd(
            [
                "gdal_contour",
                "-a",
                "elev_m",
                "-i",
                f"{interval_m:g}",
                "-f",
                "GeoJSON",
                str(src),
                str(raw_geojson),
            ],
            step="gdal_contour",
        )
        raw_fc = json.loads(raw_geojson.read_text(encoding="utf-8"))
    finally:
        raw_geojson.unlink(missing_ok=True)

    features: list[dict[str, Any]] = []
    for feat in raw_fc.get("features") or []:
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
        features.append(
            {
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "elev_m": elev_int,
                    "is_index": is_index,
                },
            }
        )

    _write_geojson_fc(out_geojson, features)
    return len(features)


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
        import rasterio.features  # type: ignore[import-not-found]

        with rasterio.open(src) as ds:
            arr = ds.read(1).astype(np.float32, copy=False)
            nodata = float(ds.nodata) if ds.nodata is not None else None
            transform = ds.transform
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
        "--no-feature-limit",
        "--no-tile-size-limit",
        "--detect-shared-borders",
        "--name=TrailForge Vector Tiles",
        f"--attribution={ATTRIBUTION_OSM}",
    ]
    for layer_name, geojson_path in valid_layers:
        cmd.extend(["-L", f"{layer_name}:{geojson_path}"])

    try:
        _run_cmd(cmd, step="tippecanoe vector PMTiles")
        tmp_pmtiles.replace(out_pmtiles)
    except Exception:
        tmp_pmtiles.unlink(missing_ok=True)
        raise

    return out_pmtiles


def _encode_dem_to_terrain_rgb_geotiff(
    dem_3857_tif: Path,
    out_rgb_tif: Path,
    *,
    base_val: float = TERRAIN_RGB_BASE_VAL,
    interval: float = TERRAIN_RGB_INTERVAL,
) -> Path:
    """Encode a Web Mercator Float32 DEM GeoTIFF into 3-band uint8 Mapbox Terrain-RGB."""
    try:
        import rasterio  # type: ignore[import-not-found]
        from rio_rgbify.encoders import data_to_rgb  # type: ignore[import-not-found]

        with rasterio.open(dem_3857_tif) as src:
            arr = src.read(1).astype(np.float32, copy=False)
            nodata = src.nodata
            invalid = ~np.isfinite(arr)
            if nodata is not None and math.isfinite(float(nodata)):
                invalid |= np.abs(arr - np.float32(nodata)) < 1e-3
            if np.any(invalid):
                arr = np.where(invalid, np.float32(0.0), arr)

            rgb = data_to_rgb(arr, baseval=base_val, interval=interval).astype(np.uint8, copy=False)
            profile = src.profile.copy()
            profile.update(
                driver="GTiff",
                dtype="uint8",
                count=3,
                nodata=None,
                tiled=True,
                compress="deflate",
            )
            with rasterio.open(out_rgb_tif, "w", **profile) as dst:
                dst.write(rgb)
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
                try:
                    rows = z_conn.execute(
                        "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"
                    ).fetchall()
                finally:
                    z_conn.close()

                merged_conn.executemany(
                    "INSERT OR REPLACE INTO tiles "
                    "(zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)",
                    rows,
                )
                merged_conn.commit()

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
