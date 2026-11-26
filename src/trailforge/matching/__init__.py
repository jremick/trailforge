"""Pluggable TrackMatcher protocol, privacy trimming, and in-PostGIS HMM matcher."""

from trailforge.matching.protocol import (
    GpsPoint,
    MatchedSegmentTraversal,
    MatchResult,
    TrackMatcher,
    UnmatchedSubtrace,
)

__all__ = [
    "GpsPoint",
    "MatchResult",
    "MatchedSegmentTraversal",
    "TrackMatcher",
    "UnmatchedSubtrace",
]
