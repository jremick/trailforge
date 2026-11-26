"""Structured condition report types, deterministic staleness decay, and OSM fix links."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, TypedDict

ReportTypeLiteral = Literal[
    "fallen_tree",
    "bog_hole",
    "washout",
    "deep_water",
    "locked_gate",
    "private_property",
    "snow_ice",
    "track_clear",
]

ReportSeverityLiteral = Literal["info", "caution", "impassable"]
EffectiveStatusLiteral = Literal["active", "stale", "resolved", "hidden"]

VALID_REPORT_TYPES: Final[tuple[ReportTypeLiteral, ...]] = (
    "fallen_tree",
    "bog_hole",
    "washout",
    "deep_water",
    "locked_gate",
    "private_property",
    "snow_ice",
    "track_clear",
)

VALID_SEVERITIES: Final[tuple[ReportSeverityLiteral, ...]] = (
    "info",
    "caution",
    "impassable",
)

GATE_WARNING_TYPES: Final[frozenset[str]] = frozenset({"locked_gate", "private_property"})

HALF_LIFE_DAYS: Final[dict[ReportTypeLiteral, float]] = {
    "snow_ice": 5.0,
    "deep_water": 7.0,
    "fallen_tree": 14.0,
    "track_clear": 14.0,
    "bog_hole": 21.0,
    "washout": 30.0,
    "locked_gate": 60.0,
    "private_property": 90.0,
}

DEFAULT_SEVERITY_BY_TYPE: Final[dict[ReportTypeLiteral, ReportSeverityLiteral]] = {
    "fallen_tree": "caution",
    "bog_hole": "caution",
    "washout": "caution",
    "deep_water": "caution",
    "locked_gate": "impassable",
    "private_property": "impassable",
    "snow_ice": "caution",
    "track_clear": "info",
}

REPORT_TYPE_LABELS: Final[dict[ReportTypeLiteral, str]] = {
    "fallen_tree": "Fallen Tree",
    "bog_hole": "Bog Hole",
    "washout": "Washout",
    "deep_water": "Deep Water",
    "locked_gate": "Locked Gate",
    "private_property": "Private Property",
    "snow_ice": "Snow / Ice",
    "track_clear": "Track Clear",
}

STALE_FRESHNESS_THRESHOLD: Final[float] = 0.35
_RESOLVE_DECAY_MULTIPLIER: Final[float] = 0.8
_OSM_WAY_RE: Final[re.Pattern[str]] = re.compile(r"^(?:way/)?(\d+)$")


class OsmFixLinks(TypedDict):
    """One-click OpenStreetMap note and iD editor handoff URLs."""

    osm_note_url: str
    osm_edit_url: str


@dataclass(frozen=True, slots=True)
class DecayEvaluation:
    """Deterministic freshness, staleness, and effective resolution state for a report."""

    freshness_score: float
    is_stale: bool
    is_resolved: bool
    effective_status: EffectiveStatusLiteral
    age_days: float
    half_life_days: float


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def is_gate_warning_type(report_type: str) -> bool:
    """Return True when the report represents a locked gate or private property warning."""
    return report_type in GATE_WARNING_TYPES


def get_half_life_days(report_type: str) -> float:
    """Return the configured staleness half-life (in days) for a report type."""
    if report_type not in HALF_LIFE_DAYS:
        raise ValueError(
            f"Unsupported report_type {report_type!r}; expected one of {VALID_REPORT_TYPES}"
        )
    return HALF_LIFE_DAYS[report_type]  # type: ignore[index]


def compute_expires_at(report_type: str, reference_at: datetime) -> datetime:
    """Compute UTC time when unconfirmed report freshness crosses the stale threshold."""
    half_life = get_half_life_days(report_type)
    stale_days = half_life * math.log2(1.0 / STALE_FRESHNESS_THRESHOLD)
    return _as_utc(reference_at) + timedelta(days=stale_days)


def evaluate_report_decay(
    report_type: str,
    created_at: datetime,
    *,
    last_confirmed_at: datetime | None = None,
    confirm_count: int = 0,
    resolved_count: int = 0,
    status: str = "active",
    now: datetime | None = None,
) -> DecayEvaluation:
    """Evaluate deterministic report freshness, staleness, and auto-resolution state.

    Freshness decays exponentially from ``last_confirmed_at or created_at`` using the
    per-type half-life, with an additional decay factor for net unresolved ``resolved``
    votes. Reports become stale when ``freshness_score < 0.35`` and resolve
    automatically when ``resolved_count >= 2 and resolved_count > confirm_count`` (or
    when stored ``status == "resolved"``).
    """
    half_life = get_half_life_days(report_type)
    current = _as_utc(now or datetime.now(UTC))
    created_utc = _as_utc(created_at)
    if last_confirmed_at is not None:
        confirmed_utc = _as_utc(last_confirmed_at)
        reference = max(created_utc, confirmed_utc)
    else:
        reference = created_utc

    age_days = max(0.0, (current - reference).total_seconds() / 86400.0)
    raw_freshness = 0.5 ** (age_days / half_life)

    net_resolves = max(0, int(resolved_count) - int(confirm_count))
    if net_resolves > 0:
        raw_freshness *= _RESOLVE_DECAY_MULTIPLIER**net_resolves

    freshness_score = round(max(0.0, min(1.0, raw_freshness)), 4)
    is_stale = freshness_score < STALE_FRESHNESS_THRESHOLD
    is_resolved = status == "resolved" or (
        int(resolved_count) >= 2 and int(resolved_count) > int(confirm_count)
    )

    if status == "hidden":
        effective_status: EffectiveStatusLiteral = "hidden"
        freshness_score = 0.0
        is_stale = True
    elif is_resolved:
        effective_status = "resolved"
    elif status == "stale" or is_stale:
        effective_status = "stale"
    else:
        effective_status = "active"

    return DecayEvaluation(
        freshness_score=freshness_score,
        is_stale=is_stale,
        is_resolved=is_resolved,
        effective_status=effective_status,
        age_days=round(age_days, 4),
        half_life_days=half_life,
    )


def build_osm_links(
    lat: float,
    lon: float,
    osm_way_id: int | str | None = None,
) -> OsmFixLinks:
    """Build one-click OpenStreetMap Note and iD Editor links for a hazard/gate location."""
    if not (math.isfinite(lat) and -90.0 <= lat <= 90.0):
        raise ValueError(f"Latitude out of range: {lat!r}")
    if not (math.isfinite(lon) and -180.0 <= lon <= 180.0):
        raise ValueError(f"Longitude out of range: {lon!r}")

    way_id: int | None = None
    if osm_way_id is not None:
        if isinstance(osm_way_id, int) and not isinstance(osm_way_id, bool):
            if osm_way_id > 0:
                way_id = osm_way_id
        else:
            match = _OSM_WAY_RE.match(str(osm_way_id).strip())
            if match:
                parsed = int(match.group(1))
                if parsed > 0:
                    way_id = parsed

    osm_note_url = f"https://www.openstreetmap.org/note/new#map=16/{lat:.5f}/{lon:.5f}"
    if way_id is not None:
        osm_edit_url = f"https://www.openstreetmap.org/edit?way={way_id}#map=17/{lat:.5f}/{lon:.5f}"
    else:
        osm_edit_url = f"https://www.openstreetmap.org/edit#map=17/{lat:.5f}/{lon:.5f}"

    return OsmFixLinks(osm_note_url=osm_note_url, osm_edit_url=osm_edit_url)


def extract_osm_way_id(source_refs: Iterable[Any] | None) -> int | None:
    """Extract a numeric OpenStreetMap way ID from canonical segment ``source_refs`` metadata."""
    if not source_refs:
        return None
    for ref in source_refs:
        if not isinstance(ref, dict):
            continue
        source = str(ref.get("source") or ref.get("source_provider") or "").strip().lower()
        if source != "osm":
            continue
        for key in ("osm_way_id", "source_id", "source_feature_id"):
            raw = ref.get(key)
            if raw is None:
                continue
            if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                return raw
            match = _OSM_WAY_RE.match(str(raw).strip())
            if match:
                parsed = int(match.group(1))
                if parsed > 0:
                    return parsed
    return None
