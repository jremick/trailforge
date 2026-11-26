"""Pluggable map-matching protocol, parsers, privacy trimming, IMU, HMM & Valhalla matchers."""

from trailforge.matching.factory import get_track_matcher
from trailforge.matching.hmm import PostGISHMMMatcher, PostGISHMMTrackMatcher
from trailforge.matching.imu import (
    DEFAULT_BASELINE_WINDOW_MS,
    DEFAULT_SHOCK_DEBOUNCE_MS,
    SHOCK_DYNAMIC_THRESHOLD_G,
    SHOCK_TOTAL_THRESHOLD_G,
    AlignedImuResult,
    ImuSample,
    ImuSummary,
    align_imu_to_trimmed_points,
    compute_pitch_roll_deg,
    enrich_match_result_with_imu,
    process_imu_batch,
    trim_imu_samples_to_window,
)
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
from trailforge.matching.valhalla import (
    ValhallaMatcherError,
    ValhallaTrackMatcher,
    decode_polyline6,
    encode_polyline6,
)

__all__ = [
    "AlignedImuResult",
    "DEFAULT_BASELINE_WINDOW_MS",
    "DEFAULT_SHOCK_DEBOUNCE_MS",
    "GpsPoint",
    "ImuSample",
    "ImuSummary",
    "MAX_PRIVACY_TRIM_M",
    "MIN_PRIVACY_TRIM_M",
    "MatchResult",
    "MatchedSegmentTraversal",
    "ParsedTrace",
    "PostGISHMMMatcher",
    "PostGISHMMTrackMatcher",
    "PrivacyTrimResult",
    "SHOCK_DYNAMIC_THRESHOLD_G",
    "SHOCK_TOTAL_THRESHOLD_G",
    "TrackMatcher",
    "UnmatchedSubtrace",
    "ValhallaMatcherError",
    "ValhallaTrackMatcher",
    "align_imu_to_trimmed_points",
    "compute_contribution_hash",
    "compute_pitch_roll_deg",
    "decode_polyline6",
    "encode_polyline6",
    "enrich_match_result_with_imu",
    "get_track_matcher",
    "measure_trace_length_m",
    "parse_gpx",
    "parse_telemetry_json",
    "points_to_linestring",
    "process_imu_batch",
    "trim_imu_samples_to_window",
    "trim_privacy_buffer",
]
