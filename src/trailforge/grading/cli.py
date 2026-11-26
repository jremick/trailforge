"""CLI entrypoint for Slice 07 explainable live grading & 72h rainfall recomputation.

Usage:
    python -W error::RuntimeWarning -m trailforge.grading.cli --offline-rain-72h 18.5
    python -W error::RuntimeWarning -m trailforge.grading.cli --live-rainfall
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from sqlalchemy.orm import Session

from trailforge.db.session import get_engine
from trailforge.grading.live import recompute_live_grades


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser for ``python -m trailforge.grading.cli``."""
    parser = argparse.ArgumentParser(
        prog="trailforge-grading",
        description=(
            "Recompute TrailForge live crowdsourced difficulty grades, obstacle hotspots, "
            "and 72-hour Open-Meteo rainfall wet-condition context."
        ),
    )
    parser.add_argument(
        "--offline-rain-72h",
        type=float,
        default=None,
        help="Deterministic 72-hour rainfall override (mm) for offline CI/demo runs.",
    )
    parser.add_argument(
        "--offline-rain-24h",
        type=float,
        default=None,
        help="Optional 24-hour rainfall override (mm); defaults to 40%% of --offline-rain-72h.",
    )
    parser.add_argument(
        "--live-rainfall",
        action="store_true",
        help="Fetch live 72-hour precipitation from Open-Meteo (with cached outage fallback).",
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
        help="Output structured JSON summary to stdout.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Execute live grading & rainfall recomputation and print summary."""
    parser = build_parser()
    args = parser.parse_args(argv)

    engine = get_engine(args.database_url)
    try:
        with Session(engine) as session:
            summary = recompute_live_grades(
                session,
                rain_override_72h_mm=args.offline_rain_72h,
                rain_override_24h_mm=args.offline_rain_24h,
                fetch_live_rainfall=bool(args.live_rainfall),
            )
            session.commit()
    finally:
        engine.dispose()

    if args.json:
        print(json.dumps(summary.model_dump(mode="json"), indent=2))
    else:
        max_rain = summary.rainfall_summary.get("max_rain_72h_mm", 0.0)
        max_risk = summary.rainfall_summary.get("max_wet_risk_level", "dry")
        print(
            f"Live grading recomputed: {summary.segments_evaluated} segments evaluated, "
            f"{summary.segments_with_telemetry} with telemetry, "
            f"{summary.obstacle_hotspots_count} obstacle hotspots, "
            f"{summary.rainfall_cells_updated} rainfall cell(s) "
            f"(max 72h={max_rain:.1f} mm [{max_risk}])."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
