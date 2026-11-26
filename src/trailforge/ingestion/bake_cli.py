"""CLI entrypoint for TrailForge real-data regional & statewide tiled bakes.

Examples:
    python -W error::RuntimeWarning -m trailforge.ingestion.bake_cli \
        --region australian-alps --bbox 148.08,-36.56,148.15,-36.50 --build-tiles

    python -W error::RuntimeWarning -m trailforge.ingestion.bake_cli \
        --region vic --pbf /data/cache/australia-latest.osm.pbf \
        --cache-dir /data/cache --build-tiles --build-pack

    python -W error::RuntimeWarning -m trailforge.ingestion.bake_cli \
        --fixtures tests/fixtures --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.orm import Session

from trailforge.db.session import get_engine
from trailforge.ingestion.live_bake import (
    DEFAULT_CACHE_DIR,
    DEFAULT_TILE_DEG,
    BakeOptions,
    parse_bbox_arg,
    run_bake,
)
from trailforge.packs.catalog import PACK_REGION_IDS


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser for ``python -m trailforge.ingestion.bake_cli``."""
    parser = argparse.ArgumentParser(
        prog="python -m trailforge.ingestion.bake_cli",
        description=(
            "Stream-bake TrailForge regional/statewide AU 4WD track networks from live "
            "open data (OSM PBF/Overpass, DataVic WFS, NSW Spatial ArcGIS, GA Hydro, "
            "CAPAD 2025, GA 5m LiDAR/DEA SRTM 1s) or offline fixtures."
        ),
    )
    parser.add_argument(
        "--region",
        choices=list(PACK_REGION_IDS),
        default=None,
        help="Curated v1 region preset (australian-alps, blue-mountains, vic, nsw).",
    )
    parser.add_argument(
        "--bbox",
        type=str,
        default=None,
        help="Optional WGS84 bounding box override 'min_lon,min_lat,max_lon,max_lat'.",
    )
    parser.add_argument(
        "--pbf",
        type=Path,
        default=None,
        help="Optional local Geofabrik .osm.pbf (or .osm) for streaming osmium extraction.",
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=None,
        help="Offline synthetic corridor fixture directory (e.g. tests/fixtures) for CI dry-runs.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help="Disk cache directory for ArcGIS/WFS/DEM clips and resumable tile manifests.",
    )
    parser.add_argument(
        "--manifest-path",
        type=Path,
        default=None,
        help="Optional explicit path for the resumable tile manifest JSON.",
    )
    parser.add_argument(
        "--tile-deg",
        type=float,
        default=DEFAULT_TILE_DEG,
        help=(
            "Spatial tile size in degrees for memory-bounded chunking "
            f"(default: {DEFAULT_TILE_DEG})."
        ),
    )
    parser.add_argument(
        "--tiles-dir",
        type=Path,
        default=None,
        help="Optional output directory for PMTiles archives when --build-tiles is enabled.",
    )
    parser.add_argument(
        "--packs-dir",
        type=Path,
        default=None,
        help="Optional output directory for offline packs when --build-pack is enabled.",
    )
    parser.add_argument(
        "--build-tiles",
        action="store_true",
        help="Build vector & Terrain-RGB PMTiles after ingestion completes.",
    )
    parser.add_argument(
        "--build-pack",
        action="store_true",
        help="Build downloadable offline regional pack archive after ingestion completes.",
    )
    parser.add_argument(
        "--skip-closures",
        action="store_true",
        help="Skip VIC/NSW closures & emergency alert feed synchronization.",
    )
    parser.add_argument(
        "--skip-rainfall",
        action="store_true",
        help="Skip Open-Meteo 72 h rainfall fetch and live grading recomputation.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan tiles (and validate offline fixtures if --fixtures is set) without DB writes.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore previously completed tiles in the resumable manifest and re-bake all tiles.",
    )
    parser.add_argument(
        "--srtm-only",
        action="store_true",
        help="Skip GA 5 m LiDAR WCS probe and stream DEA SRTM 1s COG directly.",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="Optional SQLAlchemy PostgreSQL/PostGIS URL override.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit full structured BakeRunResult JSON to stdout.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, execute the tiled bake pipeline, and print summary."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.region is None and args.bbox is None and args.fixtures is None:
        parser.error("Specify at least one of --region, --bbox, or --fixtures.")

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s [%(name)s] %(message)s",
    )

    parsed_bbox = parse_bbox_arg(args.bbox) if args.bbox else None
    options = BakeOptions(
        region_id=args.region,
        bbox=parsed_bbox,
        tile_deg=args.tile_deg,
        pbf_path=args.pbf,
        fixtures_dir=args.fixtures,
        cache_dir=args.cache_dir,
        manifest_path=args.manifest_path,
        tiles_dir=args.tiles_dir,
        packs_dir=args.packs_dir,
        build_tiles=bool(args.build_tiles),
        build_pack=bool(args.build_pack),
        skip_closures=bool(args.skip_closures),
        skip_rainfall=bool(args.skip_rainfall),
        dry_run=bool(args.dry_run),
        resume=not bool(args.no_resume),
        prefer_5m_dem=not bool(args.srtm_only),
    )

    if options.dry_run:
        result = run_bake(options, session=None)
    else:
        engine = get_engine(args.database_url)
        try:
            with Session(engine) as session:
                result = run_bake(options, session=session, commit=True)
        finally:
            engine.dispose()

    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        print(
            json.dumps(
                {
                    "bake_id": result.bake_id,
                    "region_id": result.region_id,
                    "source_mode": result.source_mode,
                    "status": result.status,
                    "dry_run": result.dry_run,
                    "tiles_total": result.tiles_total,
                    "tiles_completed": result.tiles_completed,
                    "tiles_skipped": result.tiles_skipped,
                    "tiles_failed": result.tiles_failed,
                    "total_tracks_upserted": result.total_tracks_upserted,
                    "total_segments_active": result.total_segments_active,
                    "total_pois_upserted": result.total_pois_upserted,
                    "closures_records_upserted": result.closures_records_upserted,
                    "rainfall_cells_updated": result.rainfall_cells_updated,
                    "tiles_built": result.tiles_built,
                    "vector_pmtiles_path": result.vector_pmtiles_path,
                    "terrain_pmtiles_path": result.terrain_pmtiles_path,
                    "pack_built": result.pack_built,
                    "elapsed_ms": result.elapsed_ms,
                    "peak_rss_mb": result.peak_rss_mb,
                    "manifest_path": result.manifest_path,
                },
                indent=2,
            )
        )

    return 0 if result.tiles_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
