"""Pluggable FeedAdapter protocol, feed registry, and normalized closure models (Slice 04)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from trailforge.db.models import FeedSyncStatus

FeedSourceLiteral = Literal[
    "vic_mapshare_seasonal",
    "vic_emergency",
    "nsw_rfs",
    "nsw_npws",
    "osm_access",
]
DbAlertTypeLiteral = Literal[
    "seasonal_closure",
    "road_closure",
    "park_closure",
    "bushfire",
    "flood",
    "hazard_warning",
    "private_gate",
]
ApiClosureTypeLiteral = Literal[
    "closed",
    "seasonal",
    "incident",
    "park_alert",
    "locked_gate",
    "private_access",
]
DbSeverityLiteral = Literal["info", "advisory", "watch_and_act", "closed", "emergency"]
ApiSeverityLiteral = Literal["critical", "warning", "info"]
ClosureStatusLiteral = Literal["active", "scheduled", "cleared", "stale"]
TrackClosureStatusLiteral = Literal["open", "info", "warning", "seasonal", "gated", "closed"]
StateLiteral = Literal["VIC", "NSW", "ACT"]

DEFAULT_VIC_SEASONAL_URL: Final[str] = (
    "https://opendata.maps.vic.gov.au/geoserver/wfs"
    "?service=WFS&version=2.0.0&request=GetFeature"
    "&typeNames=open-data-platform:paim_vm_tr_road_closures"
    "&outputFormat=application/json&srsName=EPSG:4326&count=500"
)
DEFAULT_VIC_EMERGENCY_URL: Final[str] = (
    "https://emergency.vic.gov.au/public/events-geojson.json"
)
DEFAULT_NSW_RFS_URL: Final[str] = "https://www.rfs.nsw.gov.au/feeds/majorIncidents.json"
DEFAULT_NSW_NPWS_URL: Final[str] = "https://www.nationalparks.nsw.gov.au/api/rssfeed/get"
DEFAULT_OSM_ACCESS_URL: Final[str] = "https://www.openstreetmap.org/copyright"


@dataclass(frozen=True)
class FeedMetadata:
    """Static registry metadata for an official or OSM access closure feed."""

    feed_source: FeedSourceLiteral
    alias: str
    display_name: str
    agency: str
    state: str
    default_url: str
    env_var: str
    sync_interval_s: int
    stale_after_s: int

    def resolve_url(self) -> str:
        return os.environ.get(self.env_var, self.default_url).strip() or self.default_url


FEED_REGISTRY: Final[dict[FeedSourceLiteral, FeedMetadata]] = {
    "vic_mapshare_seasonal": FeedMetadata(
        feed_source="vic_mapshare_seasonal",
        alias="vic_seasonal",
        display_name="VIC MapShareVic Seasonal Closures",
        agency="DEECA / Parks Victoria (MapShareVic)",
        state="VIC",
        default_url=DEFAULT_VIC_SEASONAL_URL,
        env_var="TRAILFORGE_VIC_SEASONAL_URL",
        sync_interval_s=21600,  # 6 hours
        stale_after_s=86400,  # 24 hours
    ),
    "vic_emergency": FeedMetadata(
        feed_source="vic_emergency",
        alias="vic_emergency",
        display_name="VicEmergency Incidents",
        agency="VicEmergency",
        state="VIC",
        default_url=DEFAULT_VIC_EMERGENCY_URL,
        env_var="TRAILFORGE_VIC_EMERGENCY_URL",
        sync_interval_s=900,  # 15 minutes
        stale_after_s=3600,  # 1 hour
    ),
    "nsw_rfs": FeedMetadata(
        feed_source="nsw_rfs",
        alias="nsw_rfs",
        display_name="NSW RFS Major Incidents",
        agency="NSW Rural Fire Service (RFS)",
        state="NSW",
        default_url=DEFAULT_NSW_RFS_URL,
        env_var="TRAILFORGE_NSW_RFS_URL",
        sync_interval_s=900,  # 15 minutes
        stale_after_s=3600,  # 1 hour
    ),
    "nsw_npws": FeedMetadata(
        feed_source="nsw_npws",
        alias="nsw_npws",
        display_name="NSW NPWS Park Alerts",
        agency="NSW National Parks & Wildlife Service (NPWS)",
        state="NSW",
        default_url=DEFAULT_NSW_NPWS_URL,
        env_var="TRAILFORGE_NSW_NPWS_URL",
        sync_interval_s=3600,  # 1 hour
        stale_after_s=14400,  # 4 hours
    ),
    "osm_access": FeedMetadata(
        feed_source="osm_access",
        alias="osm_access",
        display_name="OSM Gates & Private Access",
        agency="OpenStreetMap Contributors",
        state="VIC/NSW",
        default_url=DEFAULT_OSM_ACCESS_URL,
        env_var="TRAILFORGE_OSM_ACCESS_URL",
        sync_interval_s=86400,  # 24 hours
        stale_after_s=604800,  # 7 days
    ),
}

FEED_ALIAS_MAP: Final[dict[str, FeedSourceLiteral]] = {
    **{k: k for k in FEED_REGISTRY},
    **{meta.alias: k for k, meta in FEED_REGISTRY.items()},
}


def resolve_feed_source(name: str) -> FeedSourceLiteral:
    key = name.strip().lower()
    if key not in FEED_ALIAS_MAP:
        valid = ", ".join(sorted(FEED_ALIAS_MAP.keys()))
        raise ValueError(f"Unknown feed source {name!r}. Valid values: {valid}")
    return FEED_ALIAS_MAP[key]


def parse_iso_datetime(value: Any) -> datetime | None:
    """Parse ISO-8601 or RFC-2822 timestamps cleanly into UTC-aware ``datetime``."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    raw = str(value).strip()
    if not raw:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(normalized)
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(raw)
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except Exception:
        return None


def map_db_to_api_severity(db_severity: str) -> ApiSeverityLiteral:
    sev = db_severity.strip().lower()
    if sev in ("closed", "emergency", "critical"):
        return "critical"
    if sev in ("watch_and_act", "advisory", "warning"):
        return "warning"
    return "info"


def map_alert_to_closure_type(
    alert_type: str,
    *,
    source_feed: str,
    title: str = "",
    raw_payload: dict[str, Any] | None = None,
    capad_park_id: int | None = None,
) -> ApiClosureTypeLiteral:
    """Map DB ``alert_type`` + feed context to the locked Slice 04 API ``closure_type``."""
    payload = raw_payload or {}
    explicit = payload.get("closure_type")
    if explicit in (
        "closed",
        "seasonal",
        "incident",
        "park_alert",
        "locked_gate",
        "private_access",
    ):
        return explicit  # type: ignore[return-value]

    at = alert_type.strip().lower()
    if at == "seasonal_closure":
        return "seasonal"
    if at in ("road_closure", "park_closure"):
        return "closed"
    if at == "private_gate":
        if payload.get("access_kind") == "private_access":
            return "private_access"
        if payload.get("has_gate_barrier") or "gate" in title.lower():
            return "locked_gate"
        return "private_access"
    if at == "hazard_warning" and (source_feed == "nsw_npws" or capad_park_id is not None):
        return "park_alert"
    return "incident"


def compute_feed_is_stale(sync_row: FeedSyncStatus | None, *, now: datetime | None = None) -> bool:
    """Return True if a feed has consecutive failures, never succeeded, or exceeded TTL."""
    if sync_row is None:
        return True
    if sync_row.last_status in ("failed", "degraded", "never_run"):
        return True
    if sync_row.consecutive_failures > 0 or sync_row.last_success_at is None:
        return True
    current = now or datetime.now(UTC)
    last_ok = (
        sync_row.last_success_at.astimezone(UTC)
        if sync_row.last_success_at.tzinfo
        else sync_row.last_success_at.replace(tzinfo=UTC)
    )
    return (current - last_ok) > timedelta(seconds=max(1, int(sync_row.stale_after_s)))


class NormalizedClosure(BaseModel):
    """Normalized closure/incident/gate record emitted by any ``FeedAdapter``."""

    source_feed: FeedSourceLiteral
    external_id: str
    alert_type: DbAlertTypeLiteral
    db_severity: DbSeverityLiteral
    status: ClosureStatusLiteral = "active"
    title: str
    description: str | None = None
    state: StateLiteral
    source_url: str | None = None
    park_name: str | None = None
    geometry: dict[str, Any] | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    source_updated_at: datetime | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    # Optional pre-resolved links (used by osm_access adapter)
    direct_segment_ids: list[str] = Field(default_factory=list)


@runtime_checkable
class FeedAdapter(Protocol):
    """Pluggable feed connector protocol for VIC/NSW agency feeds and OSM access propagation."""

    feed_source: FeedSourceLiteral
    display_name: str
    agency: str
    state: str
    source_url: str
    sync_interval_s: int
    stale_after_s: int

    def fetch(self, *, session: Session | None = None) -> list[NormalizedClosure]:
        """Fetch and normalize current active/scheduled alerts from the upstream source."""
        ...
