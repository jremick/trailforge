"""CLI entrypoint for building TrailForge regional offline packs (< 300 MB RAM).

Example:
    python -W error::RuntimeWarning -m trailforge.packs.cli \
      --region australian-alps \
      --output-dir /data/packs \
      --dem tests/fixtures/dem_alps_corridor.tif
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from trailforge.db.session import get_engine
from trailforge.packs.builder import build_regional_pack, get_packs_dir
from trailforge.packs.catalog import PACK_REGION_IDS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trailforge.packs.cli",
        description="Build streaming regional offline packs (PMTiles + snapshot + bundle.tar.gz).",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--region",
        type=str,
        choices=list(PACK_REGION_IDS),
        help="Locked v1 regional pack identifier.",
    )
    group.add_argument(
        "--all-regions",
        action="store_true",
        help="Build all 4 regional packs sequentially.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=get_packs_dir(),
        help="Root directory for regional pack folders (defaults to PACKS_DIR / /data/packs).",
    )
    parser.add_argument(
        "--dem",
        type=Path,
        default=None,
        help="Optional local DEM GeoTIFF for region-clipped contours, snowline & terrain PMTiles.",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="Optional SQLAlchemy database URL override (defaults to DATABASE_URL).",
    )
    parser.add_argument(
        "--version",
        type=str,
        default=None,
        help="Optional pack version label (default: YYYY.MM.DD UTC).",
    )
    parser.add_argument(
        "--skip-tiles",
        action="store_true",
        help="Export snapshot, manifest, and bundle without building PMTiles.",
    )
    parser.add_argument(
        "--static-only",
        action="store_true",
        help="Exclude community-trip telemetry from a pack intended for public CDN hosting.",
    )
    format_group = parser.add_mutually_exclusive_group()
    format_group.add_argument(
        "--sharded",
        action="store_true",
        help="Force compact sharded_v2 snapshot + gzipped track detail shards.",
    )
    format_group.add_argument(
        "--inline",
        action="store_true",
        help="Force legacy inline_v1 single-file snapshot.json.",
    )
    parser.add_argument(
        "--no-bundle",
        action="store_true",
        help="Skip generating bundle.tar.gz (useful when hosting large sharded packs on R2).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    target_regions = list(PACK_REGION_IDS) if args.all_regions else [args.region]
    sharded_opt: bool | None = True if args.sharded else (False if args.inline else None)

    engine = get_engine(args.database_url)
    results_summary: list[dict[str, object]] = []
    try:
        with Session(engine) as session:
            for region_id in target_regions:
                res = build_regional_pack(
                    session,
                    region_id,
                    args.output_dir,
                    dem_path=args.dem,
                    version=args.version,
                    skip_tiles=args.skip_tiles,
                    static_only=args.static_only,
                    sharded=sharded_opt,
                    include_bundle=not args.no_bundle,
                )
                results_summary.append(
                    {
                        "region_id": res.region_id,
                        "pack_dir": str(res.pack_dir),
                        "manifest_path": str(res.manifest_path),
                        "manifest_sha256": res.manifest_sha256,
                        "bundle_path": str(res.bundle_path) if res.bundle_path else None,
                        "snapshot_format": res.manifest.snapshot_format,
                        "shard_count": res.manifest.shard_count,
                        "vector_available": res.manifest.vector_available,
                        "terrain_available": res.manifest.terrain_available,
                        "counts": res.manifest.counts,
                        "total_size_bytes": res.manifest.total_size_bytes,
                        "bundle_size_bytes": res.manifest.bundle_size_bytes,
                    }
                )
    finally:
        engine.dispose()

    output = results_summary[0] if len(results_summary) == 1 else {"packs": results_summary}
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
