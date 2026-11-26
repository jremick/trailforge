"""Live VIC & NSW closure, emergency, park alert, and Open-Meteo rainfall feed connectors."""

from __future__ import annotations

from typing import Any

from trailforge.feeds.adapters import (
    NSWNPWSAdapter,
    NSWRFSAdapter,
    OSMAccessAdapter,
    VicEmergencyAdapter,
    VicSeasonalClosuresAdapter,
    build_default_adapters,
)
from trailforge.feeds.base import (
    FEED_REGISTRY,
    FeedAdapter,
    FeedMetadata,
    NormalizedClosure,
    compute_feed_is_stale,
    resolve_feed_source,
)
from trailforge.feeds.queries import (
    get_closures_for_track_ids,
    get_closures_geojson,
    get_feed_health_summary,
    list_closures,
)
from trailforge.feeds.sync import (
    FeedSyncResult,
    SyncRunSummary,
    sync_configured_feeds,
    sync_feed,
    sync_from_fixtures,
)

__all__ = [
    "FEED_REGISTRY",
    "FeedAdapter",
    "FeedMetadata",
    "FeedSyncResult",
    "NSWNPWSAdapter",
    "NSWRFSAdapter",
    "NormalizedClosure",
    "OSMAccessAdapter",
    "SyncRunSummary",
    "VicEmergencyAdapter",
    "VicSeasonalClosuresAdapter",
    "build_default_adapters",
    "compute_feed_is_stale",
    "get_closures_for_track_ids",
    "get_closures_geojson",
    "get_feed_health_summary",
    "list_closures",
    "resolve_feed_source",
    "sync_configured_feeds",
    "sync_feed",
    "sync_from_fixtures",
]


def __getattr__(name: str) -> Any:
    if name == "cli":
        from trailforge.feeds import cli

        return cli
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
