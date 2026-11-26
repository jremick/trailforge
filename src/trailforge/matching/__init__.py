"""Pluggable map-matching protocol, trace parsers, privacy trimming, and PostGIS HMM matcher."""

from trailforge.matching.hmm import PostGISHMMMatcher, PostGISHMMTrackMatcher
from trailforge.matching.parser import ParsedTrace, parse_gpx, parse_telemetry_json
from trailforge.matching.privacy import (
    MAX_PRIVACY_TRIM_M,
    MIN_PRIVACY_TRIM_M,
    PrivacyTrimResult,
    compute_contribution_hash,
    measure_trace_length_m,
    points_to_linestring,
    trim_privacy_buffer,
)
from trailforge.matching.protocol import (
    GpsPoint,
    MatchedSegmentTraversal,
    MatchResult,
    TrackMatcher,
    UnmatchedSubtrace,
)

__all__ = [
    "GpsPoint",
    "MAX_PRIVACY_TRIM_M",
    "MIN_PRIVACY_TRIM_M",
    "MatchResult",
    "MatchedSegmentTraversal",
    "ParsedTrace",
    "PostGISHMMMatcher",
    "PostGISHMMTrackMatcher",
    "PrivacyTrimResult",
    "TrackMatcher",
    "UnmatchedSubtrace",
    "compute_contribution_hash",
    "measure_trace_length_m",
    "parse_gpx",
    "parse_telemetry_json",
    "points_to_linestring",
    "trim_privacy_buffer",
]
