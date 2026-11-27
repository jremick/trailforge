"""High-rate mobile IMU (10-100 Hz) accelerometer & clinometer signal processor.

Processes mobile accelerometer / clinometer sample batches captured by PWA
DeviceMotion or native Capacitor sensors:
  1. Gravity baseline removal via centered rolling-window / batch mean subtraction
     on acceleration magnitude ``||a|| = sqrt(ax^2 + ay^2 + az^2)`` (in g units).
  2. Vibration roughness extraction (``roughness_rms``, ``roughness_p95``), discrete
     shock impact counting (peaks ``> 0.8g`` dynamic jolt or ``> 1.8g`` total), and
     clinometer peak angles (``max_pitch_deg``, ``max_roll_deg``).
  3. Privacy-by-design window enforcement: IMU samples inside the mandatory
     start/end ``>= 250 m`` privacy buffer are strictly discarded BEFORE any
     roughness metrics or public segment observations are computed.
  4. Voronoi time alignment of trimmed IMU samples onto GPS breadcrumbs and
     matched canonical segment intervals.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime

import numpy as np
from pydantic import BaseModel, Field, field_validator

from trailforge.matching.protocol import GpsPoint, MatchedSegmentTraversal, MatchResult

SHOCK_DYNAMIC_THRESHOLD_G: float = 0.8
SHOCK_TOTAL_THRESHOLD_G: float = 1.8
DEFAULT_BASELINE_WINDOW_MS: float = 1000.0
DEFAULT_SHOCK_DEBOUNCE_MS: float = 150.0
_EPOCH_MS_CUTOFF: float = 1e11  # Values >= 1e11 (~1973-03-03) are Unix epoch milliseconds
_MAX_SEGMENT_IMU_SAMPLE_VISITS = 500_000


def _require_finite(value: float, field_name: str) -> float:
    val = float(value)
    if not math.isfinite(val):
        raise ValueError(f"{field_name} must be a finite number")
    return val


class ImuSample(BaseModel):
    """Single high-rate mobile IMU sample in g-force units with optional clinometer angles."""

    t_ms: float = Field(..., ge=0.0, description="Sample timestamp in ms (epoch ms or offset ms).")
    ax: float = Field(..., description="X-axis acceleration in g.")
    ay: float = Field(..., description="Y-axis acceleration in g.")
    az: float = Field(..., description="Z-axis acceleration in g.")
    pitch_deg: float | None = Field(
        default=None,
        ge=-180.0,
        le=180.0,
        description="Optional vehicle pitch angle in degrees.",
    )
    roll_deg: float | None = Field(
        default=None,
        ge=-180.0,
        le=180.0,
        description="Optional vehicle roll angle in degrees.",
    )

    @field_validator("t_ms", "ax", "ay", "az")
    @classmethod
    def _validate_finite_required(cls, value: float, info: object) -> float:
        field_name = getattr(info, "field_name", "value")
        return _require_finite(value, str(field_name))

    @field_validator("pitch_deg", "roll_deg")
    @classmethod
    def _validate_finite_optional(cls, value: float | None, info: object) -> float | None:
        if value is None:
            return None
        field_name = getattr(info, "field_name", "value")
        return _require_finite(value, str(field_name))


class ImuSummary(BaseModel):
    """Aggregate vibration roughness, shock count, and clinometer peaks for a trimmed session."""

    sample_count: int = Field(default=0, ge=0)
    roughness_rms: float | None = Field(default=None, ge=0.0)
    roughness_p95: float | None = Field(default=None, ge=0.0)
    shock_event_count: int = Field(default=0, ge=0)
    max_pitch_deg: float | None = Field(default=None, ge=0.0)
    max_roll_deg: float | None = Field(default=None, ge=0.0)


class AlignedImuResult(BaseModel):
    """Privacy-trimmed IMU alignment output ready for map-matching & segment enrichment."""

    points: list[GpsPoint]
    point_roughness_p95: list[float | None]
    trimmed_samples: list[ImuSample]
    summary: ImuSummary
    discarded_sample_count: int = Field(default=0, ge=0)


def compute_pitch_roll_deg(ax: float, ay: float, az: float) -> tuple[float, float]:
    """Estimate static pitch and roll tilt angles (degrees) from a 3-axis g vector."""
    pitch = math.degrees(math.atan2(ax, math.hypot(ay, az)))
    roll = math.degrees(math.atan2(ay, math.hypot(ax, az)))
    return round(pitch, 3), round(roll, 3)


def _to_epoch_ms(dt: datetime) -> float:
    aware = dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return aware.timestamp() * 1000.0


def _rolling_time_mean(
    t_ms: np.ndarray,
    values: np.ndarray,
    window_ms: float,
    *,
    min_window_samples: int = 5,
) -> np.ndarray:
    """O(N) centered rolling mean over a time window ``[t - w/2, t + w/2]``.

    Falls back to the full batch mean when the batch spans less than ``window_ms``,
    contains fewer than 15 samples, or when a sample's local time window contains
    fewer than ``min_window_samples`` samples (preventing low-rate/sparse IMU
    self-cancellation where ``baseline_g[i] == mag_g[i]``).
    """
    n = int(values.shape[0])
    if n == 0:
        return np.empty(0, dtype=np.float64)
    if n == 1:
        return np.array([1.0], dtype=np.float64)

    batch_mean = float(np.mean(values))
    span_ms = float(t_ms[-1] - t_ms[0])
    if n < 15 or window_ms <= 0.0 or span_ms <= window_ms:
        return np.full(n, batch_mean, dtype=np.float64)

    half_w = window_ms * 0.5
    left = np.searchsorted(t_ms, t_ms - half_w, side="left")
    right = np.searchsorted(t_ms, t_ms + half_w, side="right")

    csum = np.empty(n + 1, dtype=np.float64)
    csum[0] = 0.0
    np.cumsum(values, out=csum[1:])

    counts = right - left
    rolling = (csum[right] - csum[left]) / np.maximum(1, counts)
    return np.where(counts < min_window_samples, batch_mean, rolling)


def _rolling_axis_mean(t_ms: np.ndarray, values: np.ndarray, window_ms: float) -> np.ndarray:
    """Centered rolling mean for low-pass gravity axis estimation (pitch/roll)."""
    n = int(values.shape[0])
    if n == 0:
        return np.empty(0, dtype=np.float64)
    if n == 1:
        return values.astype(np.float64, copy=True)

    half_w = max(window_ms * 0.5, 1.0)
    left = np.searchsorted(t_ms, t_ms - half_w, side="left")
    right = np.searchsorted(t_ms, t_ms + half_w, side="right")

    csum = np.empty(n + 1, dtype=np.float64)
    csum[0] = 0.0
    np.cumsum(values, out=csum[1:])

    counts = np.maximum(1, right - left)
    return (csum[right] - csum[left]) / counts


def _compute_dynamic_series(
    samples: Sequence[ImuSample],
    *,
    baseline_window_ms: float = DEFAULT_BASELINE_WINDOW_MS,
    already_sorted: bool = False,
) -> tuple[list[ImuSample], np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(sorted_samples, t_ms, ax, ay, az, mag_g, dynamic_g)``."""
    ordered = list(samples) if already_sorted else sorted(samples, key=lambda s: s.t_ms)
    n = len(ordered)
    t_ms = np.fromiter((s.t_ms for s in ordered), dtype=np.float64, count=n)
    ax = np.fromiter((s.ax for s in ordered), dtype=np.float64, count=n)
    ay = np.fromiter((s.ay for s in ordered), dtype=np.float64, count=n)
    az = np.fromiter((s.az for s in ordered), dtype=np.float64, count=n)

    mag_g = np.sqrt(ax * ax + ay * ay + az * az)
    if n == 1:
        baseline_g = np.array([1.0], dtype=np.float64)
    else:
        baseline_g = _rolling_time_mean(t_ms, mag_g, baseline_window_ms)
    dynamic_g = mag_g - baseline_g
    return ordered, t_ms, ax, ay, az, mag_g, dynamic_g


def _summarize_dynamic_series(
    ordered: Sequence[ImuSample],
    t_ms: np.ndarray,
    ax: np.ndarray,
    ay: np.ndarray,
    az: np.ndarray,
    mag_g: np.ndarray,
    dynamic_g: np.ndarray,
    *,
    baseline_window_ms: float = DEFAULT_BASELINE_WINDOW_MS,
    shock_dynamic_threshold_g: float = SHOCK_DYNAMIC_THRESHOLD_G,
    shock_total_threshold_g: float = SHOCK_TOTAL_THRESHOLD_G,
    shock_debounce_ms: float = DEFAULT_SHOCK_DEBOUNCE_MS,
) -> ImuSummary:
    """Compute vectorized ImuSummary from precomputed dynamic series (zero redundant passes)."""
    n = len(ordered)
    abs_dyn = np.abs(dynamic_g)

    roughness_rms = round(float(np.sqrt(np.mean(dynamic_g * dynamic_g))), 4)
    roughness_p95 = round(float(np.percentile(abs_dyn, 95)), 4)
    shock_count = _count_shock_events(
        t_ms,
        mag_g,
        dynamic_g,
        dynamic_threshold_g=shock_dynamic_threshold_g,
        total_threshold_g=shock_total_threshold_g,
        debounce_ms=shock_debounce_ms,
    )

    # Vectorized pitch/roll extraction (replaces 100k-iteration Python math.atan2 loop)
    pitch_arr = np.fromiter(
        (math.nan if s.pitch_deg is None else abs(s.pitch_deg) for s in ordered),
        dtype=np.float64,
        count=n,
    )
    roll_arr = np.fromiter(
        (math.nan if s.roll_deg is None else abs(s.roll_deg) for s in ordered),
        dtype=np.float64,
        count=n,
    )
    missing_pitch = np.isnan(pitch_arr)
    missing_roll = np.isnan(roll_arr)
    if np.any(missing_pitch) or np.any(missing_roll):
        lp_win = min(baseline_window_ms, 500.0)
        ax_lp = _rolling_axis_mean(t_ms, ax, lp_win)
        ay_lp = _rolling_axis_mean(t_ms, ay, lp_win)
        az_lp = _rolling_axis_mean(t_ms, az, lp_win)
        if np.any(missing_pitch):
            est_pitch = np.abs(np.degrees(np.arctan2(ax_lp, np.hypot(ay_lp, az_lp))))
            pitch_arr = np.where(missing_pitch, est_pitch, pitch_arr)
        if np.any(missing_roll):
            est_roll = np.abs(np.degrees(np.arctan2(ay_lp, np.hypot(ax_lp, az_lp))))
            roll_arr = np.where(missing_roll, est_roll, roll_arr)

    max_pitch = float(np.max(pitch_arr)) if n > 0 else 0.0
    max_roll = float(np.max(roll_arr)) if n > 0 else 0.0

    return ImuSummary(
        sample_count=n,
        roughness_rms=roughness_rms,
        roughness_p95=roughness_p95,
        shock_event_count=shock_count,
        max_pitch_deg=round(max_pitch, 2),
        max_roll_deg=round(max_roll, 2),
    )


def _count_shock_events(
    t_ms: np.ndarray,
    mag_g: np.ndarray,
    dynamic_g: np.ndarray,
    *,
    dynamic_threshold_g: float = SHOCK_DYNAMIC_THRESHOLD_G,
    total_threshold_g: float = SHOCK_TOTAL_THRESHOLD_G,
    debounce_ms: float = DEFAULT_SHOCK_DEBOUNCE_MS,
) -> int:
    """Count discrete shock events exceeding dynamic (> 0.8g) or total (> 1.8g) thresholds.

    Consecutive samples within a single impact pulse or separated by less than
    ``debounce_ms`` are grouped into one discrete shock event so a 40-80 ms rock
    hit at 50 Hz is counted once rather than 3-4 times.
    """
    exceed_mask = (np.abs(dynamic_g) > dynamic_threshold_g) | (mag_g > total_threshold_g)
    exceed_indices = np.flatnonzero(exceed_mask)
    if exceed_indices.size == 0:
        return 0

    events = 0
    last_event_end_ms = -math.inf
    prev_idx = -2

    for idx_raw in exceed_indices:
        idx = int(idx_raw)
        cur_t = float(t_ms[idx])
        is_contiguous = idx == prev_idx + 1
        within_debounce = (cur_t - last_event_end_ms) < debounce_ms
        if not is_contiguous and not within_debounce:
            events += 1
        last_event_end_ms = cur_t
        prev_idx = idx

    return events


def process_imu_batch(
    samples: Sequence[ImuSample],
    *,
    baseline_window_ms: float = DEFAULT_BASELINE_WINDOW_MS,
    shock_dynamic_threshold_g: float = SHOCK_DYNAMIC_THRESHOLD_G,
    shock_total_threshold_g: float = SHOCK_TOTAL_THRESHOLD_G,
    shock_debounce_ms: float = DEFAULT_SHOCK_DEBOUNCE_MS,
) -> ImuSummary:
    """Compute gravity-removed roughness, shock events, and clinometer peaks for an IMU batch."""
    if not samples:
        return ImuSummary()

    ordered, t_ms, ax, ay, az, mag_g, dynamic_g = _compute_dynamic_series(
        samples, baseline_window_ms=baseline_window_ms
    )
    return _summarize_dynamic_series(
        ordered,
        t_ms,
        ax,
        ay,
        az,
        mag_g,
        dynamic_g,
        baseline_window_ms=baseline_window_ms,
        shock_dynamic_threshold_g=shock_dynamic_threshold_g,
        shock_total_threshold_g=shock_total_threshold_g,
        shock_debounce_ms=shock_debounce_ms,
    )


def _normalize_samples_to_epoch_ms(
    samples: Sequence[ImuSample],
    *,
    anchor_start_at: datetime | None = None,
) -> list[ImuSample]:
    """Convert relative-offset ``t_ms`` samples to Unix epoch ms when anchored to a trip start."""
    if not samples:
        return []
    max_t = max(s.t_ms for s in samples)
    if max_t >= _EPOCH_MS_CUTOFF or anchor_start_at is None:
        return list(samples)

    anchor_ms = _to_epoch_ms(anchor_start_at)
    return [s.model_copy(update={"t_ms": anchor_ms + s.t_ms}) for s in samples]


def trim_imu_samples_to_window(
    samples: Sequence[ImuSample],
    *,
    start_at: datetime,
    end_at: datetime,
    anchor_start_at: datetime | None = None,
) -> tuple[list[ImuSample], int]:
    """Strictly discard IMU samples outside ``[start_at, end_at]`` (privacy buffer enforcement).

    Returns ``(retained_samples_sorted, discarded_count)``.
    """
    if not samples:
        return [], 0

    start_ms = _to_epoch_ms(start_at)
    end_ms = _to_epoch_ms(end_at)
    if end_ms < start_ms:
        return [], len(samples)

    normalized = _normalize_samples_to_epoch_ms(samples, anchor_start_at=anchor_start_at)
    retained = [s for s in normalized if start_ms <= s.t_ms <= end_ms]
    retained.sort(key=lambda s: s.t_ms)
    discarded = len(normalized) - len(retained)
    return retained, discarded


def align_imu_to_trimmed_points(
    trimmed_points: Sequence[GpsPoint],
    imu_samples: Sequence[ImuSample],
    *,
    raw_points: Sequence[GpsPoint] | None = None,
    baseline_window_ms: float = DEFAULT_BASELINE_WINDOW_MS,
) -> AlignedImuResult:
    """Privacy-trim and time-align high-rate IMU samples onto trimmed GPS breadcrumbs.

    Any IMU sample occurring before ``trimmed_points[0].timestamp`` or after
    ``trimmed_points[-1].timestamp`` (i.e. inside the start/end >= 250 m privacy
    buffer) is strictly discarded before gravity baseline removal or roughness
    computation.
    """
    if len(trimmed_points) < 2:
        return AlignedImuResult(
            points=list(trimmed_points),
            point_roughness_p95=[None] * len(trimmed_points),
            trimmed_samples=[],
            summary=ImuSummary(),
            discarded_sample_count=len(imu_samples),
        )

    if not imu_samples:
        return AlignedImuResult(
            points=list(trimmed_points),
            point_roughness_p95=[None] * len(trimmed_points),
            trimmed_samples=[],
            summary=ImuSummary(),
            discarded_sample_count=0,
        )

    anchor_start = raw_points[0].timestamp if raw_points else trimmed_points[0].timestamp
    retained, discarded_count = trim_imu_samples_to_window(
        imu_samples,
        start_at=trimmed_points[0].timestamp,
        end_at=trimmed_points[-1].timestamp,
        anchor_start_at=anchor_start,
    )

    if not retained:
        return AlignedImuResult(
            points=list(trimmed_points),
            point_roughness_p95=[None] * len(trimmed_points),
            trimmed_samples=[],
            summary=ImuSummary(),
            discarded_sample_count=discarded_count,
        )

    ordered, t_ms, ax, ay, az, mag_g, dynamic_g = _compute_dynamic_series(
        retained, baseline_window_ms=baseline_window_ms, already_sorted=True
    )
    summary = _summarize_dynamic_series(
        ordered,
        t_ms,
        ax,
        ay,
        az,
        mag_g,
        dynamic_g,
        baseline_window_ms=baseline_window_ms,
    )
    abs_dyn = np.abs(dynamic_g)
    dyn_sq = dynamic_g * dynamic_g

    n_gps = len(trimmed_points)
    n_imu = len(retained)
    gps_ms = np.fromiter(
        (_to_epoch_ms(pt.timestamp) for pt in trimmed_points),
        dtype=np.float64,
        count=n_gps,
    )

    # O(M + N log M) Voronoi midpoint slicing on chronologically sorted t_ms (SEC-H3)
    midpoints = 0.5 * (gps_ms[:-1] + gps_ms[1:])
    if np.all(np.diff(gps_ms) >= 0.0):
        edges = np.searchsorted(t_ms, midpoints, side="left")
        starts = np.empty(n_gps, dtype=np.intp)
        ends = np.empty(n_gps, dtype=np.intp)
        starts[0] = 0
        starts[1:] = edges
        ends[:-1] = edges
        ends[-1] = n_imu

        csum_sq = np.empty(n_imu + 1, dtype=np.float64)
        csum_sq[0] = 0.0
        np.cumsum(dyn_sq, out=csum_sq[1:])
        counts = ends - starts
        rms_all = np.where(
            counts > 0,
            np.sqrt((csum_sq[ends] - csum_sq[starts]) / np.maximum(1, counts)),
            0.0,
        )

        enriched_points: list[GpsPoint] = []
        point_p95: list[float | None] = []
        for k, pt in enumerate(trimmed_points):
            lo = int(starts[k])
            hi = int(ends[k])
            if hi > lo:
                pt_rms = round(float(rms_all[k]), 4)
                win_abs = abs_dyn[lo:hi]
                pt_p95 = round(
                    float(win_abs[0]) if hi - lo == 1 else float(np.percentile(win_abs, 95)),
                    4,
                )
                enriched_points.append(pt.model_copy(update={"roughness_rms": pt_rms}))
                point_p95.append(pt_p95)
            else:
                enriched_points.append(pt)
                point_p95.append(None)
    else:
        # Rare out-of-order GPS timestamps: group by bin index via argsort in O(M log M)
        bin_indices = np.searchsorted(midpoints, t_ms, side="right")
        order = np.argsort(bin_indices, kind="mergesort")
        sorted_bins = bin_indices[order]
        sorted_dyn_sq = dyn_sq[order]
        sorted_abs = abs_dyn[order]
        bin_starts = np.searchsorted(sorted_bins, np.arange(n_gps), side="left")
        bin_ends = np.searchsorted(sorted_bins, np.arange(n_gps), side="right")

        enriched_points = []
        point_p95 = []
        for k, pt in enumerate(trimmed_points):
            lo = int(bin_starts[k])
            hi = int(bin_ends[k])
            if hi > lo:
                pt_rms = round(float(np.sqrt(np.mean(sorted_dyn_sq[lo:hi]))), 4)
                pt_p95 = round(float(np.percentile(sorted_abs[lo:hi], 95)), 4)
                enriched_points.append(pt.model_copy(update={"roughness_rms": pt_rms}))
                point_p95.append(pt_p95)
            else:
                enriched_points.append(pt)
                point_p95.append(None)

    return AlignedImuResult(
        points=enriched_points,
        point_roughness_p95=point_p95,
        trimmed_samples=retained,
        summary=summary,
        discarded_sample_count=discarded_count,
    )


def enrich_match_result_with_imu(
    match_result: MatchResult,
    trimmed_imu_samples: Sequence[ImuSample],
    *,
    baseline_window_ms: float = DEFAULT_BASELINE_WINDOW_MS,
) -> MatchResult:
    """Refine matched segment ``roughness_rms`` and ``roughness_p95`` using trimmed IMU samples."""
    if not trimmed_imu_samples or not match_result.matched_segments:
        return match_result

    _, t_ms, _ax, _ay, _az, _mag_g, dynamic_g = _compute_dynamic_series(
        trimmed_imu_samples, baseline_window_ms=baseline_window_ms
    )
    abs_dyn = np.abs(dynamic_g)

    segments = match_result.matched_segments
    start_times = np.fromiter(
        (_to_epoch_ms(seg.entry_at) for seg in segments), dtype=np.float64, count=len(segments)
    )
    end_times = np.fromiter(
        (_to_epoch_ms(seg.exit_at) for seg in segments), dtype=np.float64, count=len(segments)
    )
    starts = np.searchsorted(t_ms, start_times, side="left")
    ends = np.searchsorted(t_ms, end_times, side="right")
    if int(np.maximum(ends - starts, 0).sum()) > _MAX_SEGMENT_IMU_SAMPLE_VISITS:
        # Heavily overlapping estimated traversal times can repeatedly visit the
        # same IMU samples. Keep the existing point-aligned segment measurements.
        return match_result

    updated_segments: list[MatchedSegmentTraversal] = []
    for seg, lo, hi in zip(segments, starts, ends, strict=True):
        if hi > lo:
            win_dyn = dynamic_g[lo:hi]
            win_abs = abs_dyn[lo:hi]
            seg_rms = round(float(np.sqrt(np.mean(win_dyn * win_dyn))), 4)
            seg_p95 = round(float(win_abs[0] if hi - lo == 1 else np.percentile(win_abs, 95)), 4)
            updated_segments.append(
                seg.model_copy(update={"roughness_rms": seg_rms, "roughness_p95": seg_p95})
            )
        else:
            updated_segments.append(seg)

    return match_result.model_copy(update={"matched_segments": updated_segments})
