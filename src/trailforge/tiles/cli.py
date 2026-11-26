"""CLI entry point for building TrailForge vector & Terrain-RGB PMTiles.

Example:
    python -m trailforge.tiles.cli \
      --output-dir /data/tiles \
      --dem tests/fixtures/dem_alps_corridor.tif
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from trailforge.config import get_settings
from trailforge.db.session import get_engine
from trailforge.tiles.builder import (
    DEFAULT_CONTOUR_INTERVAL_M,
    DEFAULT_SNOWLINE_BANDS_M,
    DEFAULT_TERRAIN_MAXZOOM,
    DEFAULT_TERRAIN_MINZOOM,
    DEFAULT_VECTOR_MAXZOOM,
    DEFAULT_VECTOR_MINZOOM,
    build_tiles,
)


def _parse_bbox(raw: str | None) -> tuple[float, float, float, float] | None:
    if not raw:
        return None
    parts = [float(p.strip()) for p in raw.split(",")]
    if len(parts) != 4:
        raise ValueError("--bbox must be 'min_lon,min_lat,max_lon,max_lat'")
    return (parts[0], parts[1], parts[2], parts[3])


def _parse_thresholds(raw: str | None) -> list[int]:
    if not raw:
        return list(DEFAULT_SNOWLINE_BANDS_M)
    return sorted({int(p.strip()) for p in raw.split(",") if p.strip()})


def build_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="python -m trailforge.tiles.cli",
        description="Build vector & Terrain-RGB PMTiles from PostGIS + DEM.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(settings.trailforge_tiles_dir),
        help="Destination directory for PMTiles archives and manifest.json.",
    )
    parser.add_argument(
        "--dem",
        type=Path,
        default=None,
        help="Path to input DEM GeoTIFF (generates contours, snowline_bands, and terrain PMTiles).",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="Optional SQLAlchemy database URL override (defaults to DATABASE_URL).",
    )
    parser.add_argument(
        "--bbox",
        type=str,
        default=None,
        help="Optional WGS84 clip bbox 'min_lon,min_lat,max_lon,max_lat'.",
    )
    parser.add_argument("--minzoom", type=int, default=DEFAULT_VECTOR_MINZOOM)
    parser.add_argument("--maxzoom", type=int, default=DEFAULT_VECTOR_MAXZOOM)
    parser.add_argument("--terrain-minzoom", type=int, default=DEFAULT_TERRAIN_MINZOOM)
    parser.add_argument("--terrain-maxzoom", type=int, default=DEFAULT_TERRAIN_MAXZOOM)
    parser.add_argument("--contour-interval", type=float, default=DEFAULT_CONTOUR_INTERVAL_M)
    parser.add_argument(
        "--snowline-bands",
        type=str,
        default=None,
        help=(
            "Comma-separated snow-line thresholds in meters "
            "(default: 800,900,1000,1100,1200,1400,1600)."
        ),
    )
    parser.add_argument("--skip-vector", action="store_true", help="Skip vector PMTiles build.")
    parser.add_argument("--skip-terrain", action="store_true", help="Skip terrain PMTiles build.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    engine = get_engine(args.database_url)
    try:
        with Session(engine) as session:
            result = build_tiles(
                session,
                args.output_dir,
                dem_path=args.dem,
                bbox=_parse_bbox(args.bbox),
                minzoom=args.minzoom,
                maxzoom=args.maxzoom,
                terrain_minzoom=args.terrain_minzoom,
                terrain_maxzoom=args.terrain_maxzoom,
                contour_interval_m=args.contour_interval,
                snowline_bands_m=_parse_thresholds(args.snowline_bands),
                build_vector=not args.skip_vector,
                build_terrain=not args.skip_terrain,
            )
    finally:
        engine.dispose()

    summary = {
        "output_dir": str(result.output_dir),
        "manifest_path": str(result.manifest_path),
        "vector_pmtiles": str(result.vector_pmtiles) if result.vector_pmtiles else None,
        "vector_size_bytes": (
            result.vector_pmtiles.stat().st_size if result.vector_pmtiles else 0
        ),
        "terrain_pmtiles": str(result.terrain_pmtiles) if result.terrain_pmtiles else None,
        "terrain_size_bytes": (
            result.terrain_pmtiles.stat().st_size if result.terrain_pmtiles else 0
        ),
        "manifest": result.manifest.model_dump(),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
