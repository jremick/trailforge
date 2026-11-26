"""Real-data regional & statewide tiled bake orchestrator for TrailForge.

Streams open-data ingestion tile-by-tile (default ``tile_deg=0.25``) with:
  - Per-tile progress logging and cross-platform peak RSS tracking (``resource.getrusage``)
  - Locked ``< 300 MB`` ``GDAL_STREAMING_ENV`` guardrails
  - Atomic resumable tile manifests so statewide VIC/NSW bakes can resume cleanly after interruption
  - Live AU open-data fetch per tile: OSM (streaming PBF OR Overpass fallback) + VIC DataVic WFS +
    NSW Spatial ArcGIS + GA Surface Hydrology + DCCEEW CAPAD 2025 + GA 5m LiDAR / DEA SRTM 1s DEM
  - Full canonical ingestion (``ingest_corridor_dataset``) -> optional live closures sync ->
    optional Open-Meteo rainfall + live grading -> optional PMTiles & offline pack builds
  - Offline ``--fixtures`` mode for deterministic CI verification of the entire orchestrator.
"""

from __future__ import annotations

import gc
import json
import logging
import math
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

import httpx
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from trailforge.config import get_settings
from trailforge.feeds.adapters import build_default_adapters
from trailforge.feeds.sync import sync_configured_feeds, sync_from_fixtures
from trailforge.grading.live import recompute_live_grades
from trailforge.ingestion.conflate import conflate_tracks
from trailforge.ingestion.connectors.base import (
    ATTRIBUTION_CAPAD,
    ATTRIBUTION_GA_DEM,
    ATTRIBUTION_GA_HYDRO,
    ATTRIBUTION_NSW_SPATIAL,
    ATTRIBUTION_OPEN_METEO,
    ATTRIBUTION_OSM,
    ATTRIBUTION_STATE_FEEDS,
    ATTRIBUTION_VIC_DEECA,
    GDAL_STREAMING_ENV,
    BBox,
    CorridorDataset,
    DemTierLiteral,
    RawPoiFeature,
    RawWaterwayFeature,
    dedupe_poi_features,
    is_in_nsw_coverage,
    is_in_vic_coverage,
    iter_spatial_tiles,
)
from trailforge.ingestion.connectors.capad import CAPADConnector
from trailforge.ingestion.connectors.dem import AUElvisDEMConnector
from trailforge.ingestion.connectors.fixtures import load_corridor_fixtures
from trailforge.ingestion.connectors.hydro import GAHydroConnector
from trailforge.ingestion.connectors.nsw import NSWTrailsConnector
from trailforge.ingestion.connectors.osm import OSMConnector
from trailforge.ingestion.connectors.vic import VICTrailsConnector
from trailforge.ingestion.pbf import StreamingPBFExtractor
from trailforge.ingestion.pipeline import ingest_corridor_dataset
from trailforge.ingestion.segmenter import segment_conflated_ways
from trailforge.packs.builder import build_regional_pack
from trailforge.packs.catalog import PACK_REGIONS, get_pack_region
from trailforge.tiles.builder import build_tiles

logger = logging.getLogger("trailforge.ingestion.live_bake")

DEFAULT_TILE_DEG: Final[float] = 0.25
DEFAULT_CACHE_DIR: Final[Path] = Path("/data/cache")
MAX_OVERPASS_TILE_DEG: Final[float] = 0.50

TileStatusLiteral = Literal["pending", "completed", "failed", "skipped"]
BakeSourceModeLiteral = Literal["live_pbf", "live_overpass", "fixtures"]
BakeStatusLiteral = Literal["running", "completed", "completed_with_errors", "dry_run"]


def get_peak_rss_mb() -> float:
    """Return process peak resident set size in MiB across macOS (bytes) and Linux (KiB)."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return round(raw / (1024.0 * 1024.0), 2)
    return round(raw / 1024.0, 2)


def apply_gdal_streaming_env() -> dict[str, str]:
    """Ensure ``< 300 MB`` GDAL streaming memory defaults are set in the process environment."""
    for key, val in GDAL_STREAMING_ENV.items():
        os.environ.setdefault(key, val)
    return dict(GDAL_STREAMING_ENV)


def parse_bbox_arg(raw: str) -> BBox:
    """Parse ``min_lon,min_lat,max_lon,max_lat`` string into a validated WGS84 ``BBox``."""
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise ValueError(f"--bbox must have 4 comma-separated floats (w,s,e,n), got: {raw!r}")
    min_x, min_y, max_x, max_y = (float(p) for p in parts)
    if not (-180.0 <= min_x < max_x <= 180.0 and -90.0 <= min_y < max_y <= 90.0):
        raise ValueError(
            f"Invalid WGS84 bbox ({min_x}, {min_y}, {max_x}, {max_y}); "
            "expected -180<=w<e<=180 and -90<=s<n<=90."
        )
    return BBox(min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y)


def plan_bake_tiles(bbox: BBox, *, tile_deg: float = DEFAULT_TILE_DEG) -> list[tuple[str, BBox]]:
    """Partition ``bbox`` into deterministic ``(tile_id, tile_bbox)`` spatial chunks."""
    step = float(tile_deg)
    if step <= 0.0:
        raise ValueError(f"tile_deg must be positive, got {tile_deg}")

    if step >= 0.1:
        raw_tiles = iter_spatial_tiles(bbox, max_deg=step)
    else:
        nx = max(1, int(math.ceil(bbox.width / step)))
        ny = max(1, int(math.ceil(bbox.height / step)))
        raw_tiles = []
        for ix in range(nx):
            w = bbox.min_x + (bbox.width * ix / nx)
            e = bbox.min_x + (bbox.width * (ix + 1) / nx)
            for iy in range(ny):
                s = bbox.min_y + (bbox.height * iy / ny)
                n = bbox.min_y + (bbox.height * (iy + 1) / ny)
                raw_tiles.append(BBox(min_x=w, min_y=s, max_x=e, max_y=n))

    planned: list[tuple[str, BBox]] = []
    for idx, tile in enumerate(raw_tiles):
        tile_id = f"t{idx:04d}_{tile.cache_hash()[:12]}"
        planned.append((tile_id, tile))
    return planned


class TileStatusRecord(BaseModel):
    """Per-tile progress, ingestion counts, and telemetry stored in the resumable bake manifest."""

    tile_id: str
    index: int
    bbox: list[float] = Field(..., min_length=4, max_length=4)
    status: TileStatusLiteral = "pending"
    osm_tracks: int = 0
    vic_tracks: int = 0
    nsw_tracks: int = 0
    waterways: int = 0
    pois: int = 0
    capad_parks: int = 0
    tracks_upserted: int = 0
    segments_active: int = 0
    segments_created: int = 0
    segments_preserved: int = 0
    pois_upserted: int = 0
    dem_tier: str | None = None
    dem_path: str | None = None
    elapsed_ms: float = 0.0
    peak_rss_mb: float = 0.0
    error: str | None = None
    completed_at: str | None = None


class BakeTileManifest(BaseModel):
    """Resumable JSON manifest persisted atomically after every processed tile."""

    schema_version: str = "trailforge_bake_manifest_v1"
    bake_id: str
    region_id: str
    region_name: str
    bbox: list[float] = Field(..., min_length=4, max_length=4)
    tile_deg: float = DEFAULT_TILE_DEG
    source_mode: BakeSourceModeLiteral
    dry_run: bool = False
    started_at: str
    updated_at: str
    status: BakeStatusLiteral = "running"
    tiles_total: int = 0
    tiles_completed: int = 0
    tiles_failed: int = 0
    tiles_skipped: int = 0
    peak_rss_mb: float = 0.0
    tiles: list[TileStatusRecord] = Field(default_factory=list)


class BakeOptions(BaseModel):
    """Configuration for a regional, corridor, statewide, or offline fixture bake."""

    region_id: str | None = None
    bbox: BBox | None = None
    tile_deg: float = Field(default=DEFAULT_TILE_DEG, gt=0.0, le=2.0)
    pbf_path: Path | None = None
    fixtures_dir: Path | None = None
    cache_dir: Path = DEFAULT_CACHE_DIR
    manifest_path: Path | None = None
    tiles_dir: Path | None = None
    packs_dir: Path | None = None
    build_tiles: bool = False
    build_pack: bool = False
    skip_closures: bool = False
    skip_rainfall: bool = False
    dry_run: bool = False
    resume: bool = True
    prefer_5m_dem: bool = True


class BakeRunResult(BaseModel):
    """Structured summary emitted at the end of a TrailForge bake run."""

    bake_id: str
    region_id: str
    region_name: str
    bbox: list[float] = Field(..., min_length=4, max_length=4)
    source_mode: BakeSourceModeLiteral
    dry_run: bool
    status: BakeStatusLiteral
    manifest_path: str
    tiles_total: int
    tiles_completed: int
    tiles_skipped: int
    tiles_failed: int
    total_tracks_upserted: int = 0
    total_segments_active: int = 0
    total_pois_upserted: int = 0
    closures_feeds_ok: int = 0
    closures_records_upserted: int = 0
    rainfall_cells_updated: int = 0
    tiles_built: bool = False
    vector_pmtiles_path: str | None = None
    terrain_pmtiles_path: str | None = None
    pack_built: bool = False
    pack_manifest_path: str | None = None
    elapsed_ms: float = 0.0
    peak_rss_mb: float = 0.0
    attributions: list[str] = Field(default_factory=list)
    tile_results: list[TileStatusRecord] = Field(default_factory=list)


def load_bake_manifest(path: Path) -> BakeTileManifest | None:
    """Load an existing resumable bake manifest if present and valid."""
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        return BakeTileManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Ignoring unreadable bake manifest at %s (%s)", path, exc)
        return None


def save_bake_manifest(path: Path, manifest: BakeTileManifest) -> None:
    """Atomically write ``manifest`` to disk via ``.tmp`` + ``replace()``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _resolve_region_and_bbox(options: BakeOptions) -> tuple[str, str, BBox]:
    if options.fixtures_dir is not None:
        manifest_file = options.fixtures_dir.expanduser().resolve() / "manifest.json"
        if manifest_file.is_file():
            raw = json.loads(manifest_file.read_text(encoding="utf-8"))
            fb = raw["bbox"]
            fix_bbox = BBox(
                min_x=float(fb[0]),
                min_y=float(fb[1]),
                max_x=float(fb[2]),
                max_y=float(fb[3]),
            )
            region_id = options.region_id or "australian-alps"
            region_name = str(raw.get("corridor") or "Offline Synthetic Corridor")
            return (region_id, region_name, options.bbox or fix_bbox)

    if options.region_id is not None:
        spec = get_pack_region(options.region_id)
        resolved_bbox = options.bbox or BBox(
            min_x=spec.bbox[0],
            min_y=spec.bbox[1],
            max_x=spec.bbox[2],
            max_y=spec.bbox[3],
        )
        return (spec.region_id, spec.name, resolved_bbox)

    if options.bbox is not None:
        return ("custom", "Custom Bounding Box Corridor", options.bbox)

    raise ValueError("Specify --region, --bbox, or --fixtures to select a bake extent.")


def _default_manifest_path(cache_dir: Path, region_id: str, bbox: BBox) -> Path:
    return cache_dir / "manifests" / f"bake_{region_id}_{bbox.cache_hash()[:12]}.json"


def _persist_tile_dem_to_cache(
    dem_path: Path, cache_dir: Path, tile_id: str, dem_tier: str
) -> Path:
    dem_dir = cache_dir / "bake_dems"
    dem_dir.mkdir(parents=True, exist_ok=True)
    dest = dem_dir / f"{tile_id}_{dem_tier}.tif"
    if dem_path.resolve() != dest.resolve():
        tmp = dest.with_suffix(".tif.tmp")
        shutil.copy2(dem_path, tmp)
        tmp.replace(dest)
    return dest


def _resolve_mosaic_dem(dem_paths: list[Path], work_dir: Path) -> Path | None:
    """Return a single DEM path or build a zero-copy GDAL VRT mosaic across tile DEMs."""
    existing = [p for p in dem_paths if p.is_file()]
    if not existing:
        return None
    if len(existing) == 1:
        return existing[0]
    if shutil.which("gdalbuildvrt") is None:
        return existing[0]

    vrt_path = work_dir / "bake_dem_mosaic.vrt"
    proc = subprocess.run(
        ["gdalbuildvrt", str(vrt_path), *(str(p) for p in existing)],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0 and vrt_path.is_file():
        return vrt_path
    logger.warning("gdalbuildvrt failed (%s); using first tile DEM", proc.stderr[-500:])
    return existing[0]


def fetch_live_tile_dataset(
    tile_bbox: BBox,
    *,
    cache_dir: Path,
    work_dir: Path,
    tile_id: str,
    pbf_extractor: StreamingPBFExtractor | None = None,
    prefer_5m_dem: bool = True,
    http_client: httpx.Client | None = None,
    corridor_name: str = "",
) -> CorridorDataset:
    """Fetch all live AU open-data layers for a single spatial tile."""
    attributions: list[str] = [ATTRIBUTION_OSM]
    osm_tracks = []
    osm_waterways: list[RawWaterwayFeature] = []
    all_pois: list[RawPoiFeature] = []

    # 1. OpenStreetMap (streaming PBF extract OR Overpass fallback for compact tiles)
    if pbf_extractor is not None:
        pbf_res = pbf_extractor.extract_bbox(tile_bbox, work_dir=work_dir)
        osm_tracks = pbf_res.tracks
        osm_waterways = pbf_res.waterways
        all_pois.extend(pbf_res.pois)
    else:
        if tile_bbox.width > MAX_OVERPASS_TILE_DEG or tile_bbox.height > MAX_OVERPASS_TILE_DEG:
            logger.warning(
                "Tile %s (%.2fx%.2f deg) exceeds recommended Overpass size; pass --pbf for "
                "large regional/statewide bakes.",
                tile_id,
                tile_bbox.width,
                tile_bbox.height,
            )
        osm_conn = OSMConnector(cache_dir=cache_dir, client=http_client)
        osm_tracks = osm_conn.fetch_overpass_tracks(tile_bbox)

    # 2. Victoria DataVic WFS (if intersecting VIC)
    vic_tracks = []
    if is_in_vic_coverage(tile_bbox):
        attributions.append(ATTRIBUTION_VIC_DEECA)
        vic_conn = VICTrailsConnector(cache_dir=cache_dir, client=http_client)
        vic_tracks = vic_conn.fetch_tracks(tile_bbox, tile_deg=max(0.25, tile_bbox.width))
        all_pois.extend(vic_conn.fetch_pois(tile_bbox, tile_deg=max(0.25, tile_bbox.width)))

    # 3. NSW Spatial Services ArcGIS (if intersecting NSW/ACT)
    nsw_tracks = []
    if is_in_nsw_coverage(tile_bbox):
        attributions.append(ATTRIBUTION_NSW_SPATIAL)
        nsw_conn = NSWTrailsConnector(cache_dir=cache_dir, client=http_client)
        nsw_tracks = nsw_conn.fetch_tracks(tile_bbox, tile_deg=max(0.25, tile_bbox.width))
        all_pois.extend(nsw_conn.fetch_pois(tile_bbox, tile_deg=max(0.25, tile_bbox.width)))

    # 4. GA National Surface Hydrology
    attributions.append(ATTRIBUTION_GA_HYDRO)
    hydro_conn = GAHydroConnector(cache_dir=cache_dir, client=http_client)
    ga_waterways = hydro_conn.fetch_waterways(tile_bbox, tile_deg=max(0.25, tile_bbox.width))
    all_pois.extend(hydro_conn.fetch_water_points(tile_bbox, tile_deg=max(0.25, tile_bbox.width)))

    waterways: list[RawWaterwayFeature] = []
    seen_ww: set[tuple[str, str]] = set()
    for ww in (*ga_waterways, *osm_waterways):
        key = (ww.source_provider, ww.source_feature_id)
        if key not in seen_ww:
            seen_ww.add(key)
            waterways.append(ww)

    # 5. DCCEEW CAPAD 2025 Protected Areas
    attributions.append(ATTRIBUTION_CAPAD)
    capad_conn = CAPADConnector(cache_dir=cache_dir, client=http_client)
    capad_parks = capad_conn.fetch_parks(tile_bbox, tile_deg=max(1.0, tile_bbox.width))

    # 6. GA 5m LiDAR WCS / DEA SRTM 1s COG DEM (graceful fallback if ocean/outside coverage)
    dem_path: Path | None = None
    dem_tier: DemTierLiteral = "srtm-1s"
    attributions.append(ATTRIBUTION_GA_DEM)
    try:
        dem_conn = AUElvisDEMConnector(
            cache_dir=cache_dir,
            prefer_5m=prefer_5m_dem,
            client=http_client,
        )
        raw_dem, dem_tier = dem_conn.fetch_dem(tile_bbox, work_dir / "dem")
        dem_path = _persist_tile_dem_to_cache(raw_dem, cache_dir, tile_id, dem_tier)
    except Exception as exc:
        logger.warning(
            "DEM unavailable for tile %s (%s); continuing without DEM draping", tile_id, exc
        )
        dem_path = None

    return CorridorDataset(
        corridor_name=corridor_name or f"Tile {tile_id}",
        bbox=tile_bbox,
        osm_tracks=osm_tracks,
        vic_tracks=vic_tracks,
        nsw_tracks=nsw_tracks,
        waterways=waterways,
        pois=dedupe_poi_features(all_pois),
        capad_parks=capad_parks,
        dem_path=dem_path,
        dem_tier=dem_tier,
        attributions=list(dict.fromkeys(attributions)),
    )


def _run_post_ingestion_steps(
    session: Session,
    options: BakeOptions,
    *,
    region_id: str,
    bbox: BBox,
    dem_path: Path | None,
    is_fixtures: bool,
    commit: bool,
    http_client: httpx.Client | None,
) -> tuple[int, int, int, bool, str | None, str | None, bool, str | None]:
    """Execute optional closures sync, Open-Meteo rainfall + live grading, PMTiles & pack builds."""
    closures_feeds_ok = 0
    closures_records_upserted = 0
    rainfall_cells_updated = 0
    tiles_built = False
    vector_pmtiles_path: str | None = None
    terrain_pmtiles_path: str | None = None
    pack_built = False
    pack_manifest_path: str | None = None

    if not options.skip_closures:
        if is_fixtures and options.fixtures_dir is not None:
            closures_file = options.fixtures_dir.expanduser().resolve() / "closures_sample.json"
            if closures_file.is_file():
                csum = sync_from_fixtures(session, closures_file, commit=commit)
                closures_feeds_ok = sum(1 for r in csum.results if r.ok)
                closures_records_upserted = sum(r.records_upserted for r in csum.results)
        else:
            adapters = build_default_adapters(client=http_client)
            csum = sync_configured_feeds(session, adapters=adapters, commit=commit)
            closures_feeds_ok = sum(1 for r in csum.results if r.ok)
            closures_records_upserted = sum(r.records_upserted for r in csum.results)

    if not options.skip_rainfall:
        if is_fixtures:
            rsum = recompute_live_grades(
                session,
                rain_override_72h_mm=12.0,
                fetch_live_rainfall=False,
            )
        else:
            rsum = recompute_live_grades(
                session,
                fetch_live_rainfall=True,
                http_client=http_client,
            )
        if commit:
            session.commit()
        else:
            session.flush()
        rainfall_cells_updated = rsum.rainfall_cells_updated

    settings = get_settings()
    if options.build_tiles:
        target_tiles_dir = (
            options.tiles_dir.expanduser().resolve()
            if options.tiles_dir is not None
            else Path(settings.trailforge_tiles_dir).expanduser().resolve()
        )
        can_vec = shutil.which("tippecanoe") is not None
        can_ter = (
            dem_path is not None
            and shutil.which("gdalwarp") is not None
            and shutil.which("gdal_translate") is not None
            and shutil.which("pmtiles") is not None
        )
        if can_vec or can_ter:
            tile_res = build_tiles(
                session,
                target_tiles_dir,
                dem_path=dem_path,
                bbox=bbox.as_tuple(),
                build_vector=can_vec,
                build_terrain=can_ter,
            )
            tiles_built = bool(tile_res.vector_pmtiles or tile_res.terrain_pmtiles)
            vector_pmtiles_path = (
                str(tile_res.vector_pmtiles) if tile_res.vector_pmtiles is not None else None
            )
            terrain_pmtiles_path = (
                str(tile_res.terrain_pmtiles) if tile_res.terrain_pmtiles is not None else None
            )
        else:
            logger.warning(
                "Skipping --build-tiles because tippecanoe/gdalwarp/pmtiles are not installed."
            )

    if options.build_pack and region_id in PACK_REGIONS:
        target_packs_dir = (
            options.packs_dir.expanduser().resolve()
            if options.packs_dir is not None
            else Path(settings.trailforge_packs_dir).expanduser().resolve()
        )
        pack_res = build_regional_pack(
            session,
            region_id,
            output_dir=target_packs_dir,
            dem_path=dem_path,
            skip_tiles=not options.build_tiles,
        )
        pack_built = True
        pack_manifest_path = str(pack_res.manifest_path)

    return (
        closures_feeds_ok,
        closures_records_upserted,
        rainfall_cells_updated,
        tiles_built,
        vector_pmtiles_path,
        terrain_pmtiles_path,
        pack_built,
        pack_manifest_path,
    )


def run_bake(
    options: BakeOptions,
    *,
    session: Session | None = None,
    http_client: httpx.Client | None = None,
    commit: bool = True,
) -> BakeRunResult:
    """Execute a full tiled real-data (or offline ``--fixtures``) TrailForge bake."""
    t_start = time.perf_counter()
    apply_gdal_streaming_env()

    region_id, region_name, bbox = _resolve_region_and_bbox(options)
    cache_dir = options.cache_dir.expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = (
        options.manifest_path.expanduser().resolve()
        if options.manifest_path is not None
        else _default_manifest_path(cache_dir, region_id, bbox)
    )

    is_fixtures = options.fixtures_dir is not None
    if is_fixtures:
        source_mode: BakeSourceModeLiteral = "fixtures"
    elif options.pbf_path is not None:
        source_mode = "live_pbf"
    else:
        source_mode = "live_overpass"

    if options.pbf_path is not None:
        pbf_resolved = options.pbf_path.expanduser().resolve()
        if not pbf_resolved.is_file():
            raise FileNotFoundError(f"Requested --pbf file not found: {pbf_resolved}")

    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    planned_tiles = (
        [(f"t0000_{bbox.cache_hash()[:12]}", bbox)]
        if is_fixtures
        else plan_bake_tiles(bbox, tile_deg=options.tile_deg)
    )

    existing_manifest = load_bake_manifest(manifest_path) if options.resume else None
    prior_completed: dict[str, TileStatusRecord] = {}
    if (
        existing_manifest is not None
        and existing_manifest.region_id == region_id
        and existing_manifest.source_mode == source_mode
        and abs(existing_manifest.tile_deg - options.tile_deg) < 1e-6
        and not options.dry_run
    ):
        for rec in existing_manifest.tiles:
            if rec.status == "completed":
                prior_completed[rec.tile_id] = rec

    bake_id = (
        existing_manifest.bake_id
        if existing_manifest is not None and prior_completed
        else f"bake_{uuid.uuid4().hex[:12]}"
    )

    tile_records: list[TileStatusRecord] = []
    for idx, (tid, tbbox) in enumerate(planned_tiles):
        if tid in prior_completed:
            prev = prior_completed[tid].model_copy(update={"status": "skipped"})
            tile_records.append(prev)
        else:
            tile_records.append(
                TileStatusRecord(
                    tile_id=tid,
                    index=idx,
                    bbox=list(tbbox.as_tuple()),
                    status="pending",
                )
            )

    started_at = (
        existing_manifest.started_at if prior_completed and existing_manifest else now_iso
    )
    manifest = BakeTileManifest(
        bake_id=bake_id,
        region_id=region_id,
        region_name=region_name,
        bbox=list(bbox.as_tuple()),
        tile_deg=options.tile_deg,
        source_mode=source_mode,
        dry_run=options.dry_run,
        started_at=started_at,
        updated_at=now_iso,
        status="dry_run" if options.dry_run else "running",
        tiles_total=len(tile_records),
        tiles_skipped=sum(1 for r in tile_records if r.status == "skipped"),
        peak_rss_mb=get_peak_rss_mb(),
        tiles=tile_records,
    )
    save_bake_manifest(manifest_path, manifest)

    attributions: list[str] = [
        ATTRIBUTION_OSM,
        ATTRIBUTION_VIC_DEECA,
        ATTRIBUTION_NSW_SPATIAL,
        ATTRIBUTION_GA_HYDRO,
        ATTRIBUTION_CAPAD,
        ATTRIBUTION_GA_DEM,
        ATTRIBUTION_OPEN_METEO,
        ATTRIBUTION_STATE_FEEDS,
    ]

    # -------------------------------------------------------------------------
    # Dry-Run Mode (zero DB writes; offline fixtures verify conflation/segmenter)
    # -------------------------------------------------------------------------
    if options.dry_run:
        total_tracks = 0
        total_segs = 0
        total_pois = 0
        if is_fixtures and options.fixtures_dir is not None:
            t0 = time.perf_counter()
            ds = load_corridor_fixtures(options.fixtures_dir)
            conf = conflate_tracks(ds.all_tracks)
            trk_groups, seg_cands = segment_conflated_ways(conf.ways)
            deduped_pois = dedupe_poi_features(ds.pois)
            rec = tile_records[0]
            rec.status = "completed"
            rec.osm_tracks = len(ds.osm_tracks)
            rec.vic_tracks = len(ds.vic_tracks)
            rec.nsw_tracks = len(ds.nsw_tracks)
            rec.waterways = len(ds.waterways)
            rec.pois = len(ds.pois)
            rec.capad_parks = len(ds.capad_parks)
            rec.tracks_upserted = len(trk_groups)
            rec.segments_active = len(seg_cands)
            rec.pois_upserted = len(deduped_pois)
            rec.dem_tier = ds.dem_tier
            rec.dem_path = str(ds.dem_path) if ds.dem_path else None
            rec.elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            rec.peak_rss_mb = get_peak_rss_mb()
            rec.completed_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            total_tracks = len(trk_groups)
            total_segs = len(seg_cands)
            total_pois = len(deduped_pois)
            manifest.tiles_completed = 1

        manifest.updated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest.peak_rss_mb = get_peak_rss_mb()
        manifest.status = "dry_run"
        save_bake_manifest(manifest_path, manifest)

        return BakeRunResult(
            bake_id=bake_id,
            region_id=region_id,
            region_name=region_name,
            bbox=list(bbox.as_tuple()),
            source_mode=source_mode,
            dry_run=True,
            status="dry_run",
            manifest_path=str(manifest_path),
            tiles_total=len(tile_records),
            tiles_completed=manifest.tiles_completed,
            tiles_skipped=manifest.tiles_skipped,
            tiles_failed=0,
            total_tracks_upserted=total_tracks,
            total_segments_active=total_segs,
            total_pois_upserted=total_pois,
            elapsed_ms=round((time.perf_counter() - t_start) * 1000.0, 2),
            peak_rss_mb=manifest.peak_rss_mb,
            attributions=attributions,
            tile_results=tile_records,
        )

    if session is None:
        raise ValueError("A SQLAlchemy Session is required when dry_run=False.")

    # -------------------------------------------------------------------------
    # Full Tiled Execution Mode (Live Open-Data OR Offline Fixtures)
    # -------------------------------------------------------------------------
    pbf_extractor = (
        StreamingPBFExtractor(
            source_pbf=options.pbf_path,
            cache_dir=cache_dir,
            client=http_client,
        )
        if options.pbf_path is not None
        else None
    )

    collected_dem_paths: list[Path] = []
    for prev in prior_completed.values():
        if prev.dem_path and Path(prev.dem_path).is_file():
            collected_dem_paths.append(Path(prev.dem_path))

    total_tracks = 0
    total_segs = 0
    total_pois = 0

    for idx, (tid, tbbox) in enumerate(planned_tiles):
        rec = tile_records[idx]
        if rec.status == "skipped":
            logger.info(
                "[%d/%d] Skipping already-completed tile %s (resume)",
                idx + 1,
                len(planned_tiles),
                tid,
            )
            continue

        t0 = time.perf_counter()
        logger.info(
            "[%d/%d] Baking tile %s bbox=(%.4f,%.4f,%.4f,%.4f) rss=%.1f MiB",
            idx + 1,
            len(planned_tiles),
            tid,
            tbbox.min_x,
            tbbox.min_y,
            tbbox.max_x,
            tbbox.max_y,
            get_peak_rss_mb(),
        )
        try:
            with tempfile.TemporaryDirectory(prefix=f"tf_bake_{tid}_") as tmp_s:
                tile_work = Path(tmp_s)
                if is_fixtures and options.fixtures_dir is not None:
                    dataset = load_corridor_fixtures(options.fixtures_dir)
                else:
                    dataset = fetch_live_tile_dataset(
                        tbbox,
                        cache_dir=cache_dir,
                        work_dir=tile_work,
                        tile_id=tid,
                        pbf_extractor=pbf_extractor,
                        prefer_5m_dem=options.prefer_5m_dem,
                        http_client=http_client,
                        corridor_name=f"{region_name} ({tid})",
                    )

                rec.osm_tracks = len(dataset.osm_tracks)
                rec.vic_tracks = len(dataset.vic_tracks)
                rec.nsw_tracks = len(dataset.nsw_tracks)
                rec.waterways = len(dataset.waterways)
                rec.pois = len(dataset.pois)
                rec.capad_parks = len(dataset.capad_parks)
                rec.dem_tier = dataset.dem_tier if dataset.dem_path is not None else None
                rec.dem_path = str(dataset.dem_path) if dataset.dem_path is not None else None

                if dataset.dem_path is not None and dataset.dem_path.is_file():
                    collected_dem_paths.append(dataset.dem_path)

                summary = ingest_corridor_dataset(session, dataset, commit=commit)
                rec.tracks_upserted = summary.tracks_upserted
                rec.segments_active = summary.segments_active
                rec.segments_created = summary.segments_created
                rec.segments_preserved = (
                    summary.segments_exact_match + summary.segments_spatial_preserved
                )
                rec.pois_upserted = summary.pois_upserted
                rec.status = "completed"
                rec.error = None
                rec.elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                rec.peak_rss_mb = get_peak_rss_mb()
                rec.completed_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

                total_tracks += summary.tracks_upserted
                total_segs += summary.segments_active
                total_pois += summary.pois_upserted

                del dataset
                if commit:
                    session.expire_all()
                gc.collect()
        except Exception as exc:
            logger.exception("Tile %s failed: %s", tid, exc)
            rec.status = "failed"
            rec.error = str(exc)[:500]
            rec.elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            rec.peak_rss_mb = get_peak_rss_mb()
            if commit:
                session.rollback()

        manifest.tiles_completed = sum(1 for r in tile_records if r.status == "completed")
        manifest.tiles_failed = sum(1 for r in tile_records if r.status == "failed")
        manifest.tiles_skipped = sum(1 for r in tile_records if r.status == "skipped")
        manifest.peak_rss_mb = get_peak_rss_mb()
        manifest.updated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        save_bake_manifest(manifest_path, manifest)

        logger.info(
            "[%d/%d] Tile %s -> %s: tracks=%d segs=%d pois=%d dem=%s %.0f ms rss=%.1f MiB",
            idx + 1,
            len(planned_tiles),
            tid,
            rec.status,
            rec.tracks_upserted,
            rec.segments_active,
            rec.pois_upserted,
            rec.dem_tier or "none",
            rec.elapsed_ms,
            rec.peak_rss_mb,
        )

    # Post-ingestion: closures, Open-Meteo rainfall + live grading, PMTiles, offline pack
    with tempfile.TemporaryDirectory(prefix="tf_bake_post_") as post_tmp_s:
        mosaic_dem = _resolve_mosaic_dem(collected_dem_paths, Path(post_tmp_s))
        (
            closures_feeds_ok,
            closures_records_upserted,
            rainfall_cells_updated,
            tiles_built,
            vector_pmtiles_path,
            terrain_pmtiles_path,
            pack_built,
            pack_manifest_path,
        ) = _run_post_ingestion_steps(
            session,
            options,
            region_id=region_id,
            bbox=bbox,
            dem_path=mosaic_dem,
            is_fixtures=is_fixtures,
            commit=commit,
            http_client=http_client,
        )

    final_status: BakeStatusLiteral = (
        "completed_with_errors" if manifest.tiles_failed > 0 else "completed"
    )
    manifest.status = final_status
    manifest.peak_rss_mb = get_peak_rss_mb()
    manifest.updated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_bake_manifest(manifest_path, manifest)

    return BakeRunResult(
        bake_id=bake_id,
        region_id=region_id,
        region_name=region_name,
        bbox=list(bbox.as_tuple()),
        source_mode=source_mode,
        dry_run=False,
        status=final_status,
        manifest_path=str(manifest_path),
        tiles_total=len(tile_records),
        tiles_completed=manifest.tiles_completed,
        tiles_skipped=manifest.tiles_skipped,
        tiles_failed=manifest.tiles_failed,
        total_tracks_upserted=total_tracks,
        total_segments_active=total_segs,
        total_pois_upserted=total_pois,
        closures_feeds_ok=closures_feeds_ok,
        closures_records_upserted=closures_records_upserted,
        rainfall_cells_updated=rainfall_cells_updated,
        tiles_built=tiles_built,
        vector_pmtiles_path=vector_pmtiles_path,
        terrain_pmtiles_path=terrain_pmtiles_path,
        pack_built=pack_built,
        pack_manifest_path=pack_manifest_path,
        elapsed_ms=round((time.perf_counter() - t_start) * 1000.0, 2),
        peak_rss_mb=manifest.peak_rss_mb,
        attributions=attributions,
        tile_results=tile_records,
    )
