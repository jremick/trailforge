"""Pluggable TrackMatcher protocol and telemetry map-matching models.

Locked per docs/SPEC_GIS_SCHEMA.md Section 4.1 so Slice 06 ships a zero-broker
in-PostGIS HMM matcher while allowing Valhalla/OSRM matchers to plug in later.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class GpsPoint(BaseModel):
    """Single GPS fix with optional elevation, accuracy, speed, and IMU roughness."""

    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    timestamp: datetime
    elev_m: float | None = None
    accuracy_m: float | None = Field(default=None, ge=0.0)
    speed_mps: float | None = Field(default=None, ge=0.0)
    roughness_rms: float | None = Field(default=None, ge=0.0)


class MatchedSegmentTraversal(BaseModel):
    """Anonymized traversal summary snapped to a canonical track_segments row."""

    segment_id: str
    track_id: str | None = None
    direction: Literal["forward", "reverse"]
    entry_at: datetime
    exit_at: datetime
    duration_s: float = Field(..., gt=0.0)
    avg_speed_kmh: float = Field(..., ge=0.0)
    min_speed_kmh: float | None = Field(default=None, ge=0.0)
    roughness_rms: float | None = Field(default=None, ge=0.0)
    roughness_p95: float | None = Field(default=None, ge=0.0)
    match_confidence: float = Field(..., ge=0.0, le=1.0)
    matched_point_count: int = Field(..., ge=1)
    slow_hotspot_lonlat: tuple[float, float] | None = None


class UnmatchedSubtrace(BaseModel):
    """Privacy-trimmed sub-trace that did not match any active canonical segment."""

    coordinates: list[tuple[float, float]]  # (lon, lat) in EPSG:4326
    length_m: float = Field(..., gt=0.0)
    started_at: datetime
    ended_at: datetime
    reason: Literal["no_candidates", "low_confidence", "transition_break"]


class MatchResult(BaseModel):
    """Output of a TrackMatcher implementation for a single privacy-trimmed trip."""

    matcher_name: str
    matched_segments: list[MatchedSegmentTraversal]
    unmatched_subtraces: list[UnmatchedSubtrace]
    overall_confidence: float = Field(..., ge=0.0, le=1.0)
    matched_fraction: float = Field(..., ge=0.0, le=1.0)


@runtime_checkable
class TrackMatcher(Protocol):
    """Pluggable map-matching interface (PostGIS HMM in v1; Valhalla/OSRM pluggable later)."""

    name: str

    async def match_trace(
        self,
        points: Sequence[GpsPoint],
        *,
        search_radius_m: float = 35.0,
        min_confidence: float = 0.55,
    ) -> MatchResult:
        """Snap a privacy-trimmed GPS sequence onto active vehicular canonical track_segments."""
        ...
