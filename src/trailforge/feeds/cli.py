"""CLI entry point for syncing VIC & NSW closures, incidents, park alerts, and OSM gates.

Examples:
    python -m trailforge.feeds.cli --fixtures tests/fixtures/closures_sample.json
    python -m trailforge.feeds.cli --live
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from trailforge.db.session import get_engine
from trailforge.feeds.adapters import build_default_adapters
from trailforge.feeds.sync import sync_configured_feeds, sync_from_fixtures


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trailforge.feeds.cli",
        description="Sync official VIC/NSW closure & emergency feeds plus OSM gate/access flags.",
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=None,
        help="Path to offline closures_sample.json fixture payload.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Fetch live upstream VIC/NSW agency feeds instead of offline fixtures.",
    )
    parser.add_argument(
        "--feeds",
        type=str,
        default=None,
        help="Optional comma-separated subset of feed names to sync.",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="Optional SQLAlchemy database URL override (defaults to DATABASE_URL).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.fixtures is None and not args.live:
        parser.error("Specify either --fixtures <path> for offline sync or --live for live feeds.")

    only_feeds = (
        [f.strip() for f in args.feeds.split(",") if f.strip()] if args.feeds else None
    )

    engine = get_engine(args.database_url)
    try:
        with Session(engine) as session:
            if args.fixtures is not None:
                summary = sync_from_fixtures(session, args.fixtures, only_feeds=only_feeds)
            else:
                adapters = build_default_adapters(only_feeds=only_feeds)
                summary = sync_configured_feeds(session, adapters=adapters)
    finally:
        engine.dispose()

    print(json.dumps(summary.model_dump(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
