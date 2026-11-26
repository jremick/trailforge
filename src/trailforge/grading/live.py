"""Explainable crowdsourced live track-grading & obstacle hotspot engine (Slice 07).

Combines deterministic DEM/OSM baseline grades with:
  1. Exponentially recency-weighted anonymized ``telemetry_observations`` (90-day half-life).
  2. Vehicle Capability Class normalization (AWD vs Stock 4WD vs Touring 4WD vs Modified 4WD).
  3. Robust outlier protection (MAD Winsorization + Bayesian prior shrinkage ``eff_n/(eff_n+4)``
     + low-sample safety cap so 1 outlier traversal cannot skew a track grade).
  4. Rough/slow obstacle hotspot detection across segments.
  5. Transparent 72-hour rainfall wet-condition risk modulation (Open-Meteo CC BY 4.0).

Strictly leaves ``baseline_grade``, ``baseline_score``, and ``baseline_breakdown`` untouched.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

import httpx
import numpy as np
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from trailforge.db.models import (
    TelemetryObservation,
    Track,
    TrackSegment,
    TrackSegmentGrade,
)
from trailforge.grading.baseline import GradeLiteral, score_to_grade
from trailforge.grading.rainfall import (
    OPEN_METEO_ATTRIBUTION,
    RainfallReading,
    SegmentWetAdjustment,
    compute_segment_wet_adjustment,
    get_rainfall_summary,
    resolve_cell_rainfall,
    spatial_cell_key,
)

LIVE_FORMULA_VERSION: Final[str] = "live_v1"
RECENCY_HALF_LIFE_DAYS: Final[float] = 90.0
PRIOR_STRENGTH: Final[float] = 4.0

HOTSPOT_SLOW_SPEED_KMH: Final[float] = 8.0
HOTSPOT_ROUGHNESS_RMS: Final[float] = 0.45
HOTSPOT_MIN_SAMPLES: Final[int] = 2
HOTSPOT_REL_SLOWDOWN_RATIO: Final[float] = 0.55

VEHICLE_CLASSES: Final[tuple[str, ...]] = (
    "AWD",
    "Stock 4WD",
    "Touring 4WD",
    "Modified 4WD",
)

# Relative expected speed multiplier on unsealed 4WD terrain (Touring 4WD = 1.0 reference)
_VEHICLE_SPEED_MULT: Final[dict[str, float]] = {
    "AWD": 0.72,
    "Stock 4WD": 0.88,
    "Touring 4WD": 1.00,
    "Modified 4WD": 1.18,
}

# Upward difficulty sensitivity: an AWD going slow on a Hard track is expected (low upward weight);
# a Modified 4WD crawling slowly with high roughness is a strong signal the track got harder.
_VEHICLE_UPWARD_SENS: Final[dict[str, float]] = {
    "AWD": 0.55,
    "Stock 4WD": 0.85,
    "Touring 4WD": 1.00,
    "Modified 4WD": 1.30,
}

# Downward difficulty sensitivity: an AWD cruising smoothly is strong evidence a track is easy;
# a Modified 4WD flying fast does not imply it is easy for stock rigs.
_VEHICLE_DOWNWARD_SENS: Final[dict[str, float]] = {
    "AWD": 1.25,
    "Stock 4WD": 1.05,
    "Touring 4WD": 0.90,
    "Modified 4WD": 0.65,
}

DbConfidenceLiteral = Literal["baseline_only", "low", "medium", "high"]
ApiConfidenceLiteral = Literal["none", "low", "medium", "high"]
_GRADE_RANK: Final[dict[str, int]] = {"Easy": 1, "Medium": 2, "Hard": 3, "Extreme": 4}
_CONF_RANK: Final[dict[str, int]] = {
    "none": 0,
    "baseline_only": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
}


@dataclass(slots=True, frozen=True)
class ObservationSample:
    """In-memory anonymized telemetry traversal observation for pure grading math."""

    vehicle_capability_class: str
    observed_at: datetime
    traversal_duration_s: float
    avg_speed_kmh: float
    min_speed_kmh: float | None = None
    roughness_rms: float | None = None
    roughness_p95: float | None = None
    match_confidence: float = 0.9


class SegmentLiveComputation(BaseModel):
    """Explainable live telemetry & wet-condition result for a single canonical segment."""

    sample_count: int = Field(..., ge=0)
    effective_sample_weight: float = Field(..., ge=0.0)
    db_confidence_level: DbConfidenceLiteral
    api_confidence_level: ApiConfidenceLiteral
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    last_driven_at: datetime | None = None
    avg_speed_kmh: float | None = None
    median_speed_kmh: float | None = None
    avg_duration_s: float | None = None
    median_roughness_rms: float | None = None
    vehicle_class_split: dict[str, dict[str, Any]] = Field(default_factory=dict)
    vehicle_normalized_delta: float = 0.0
    outlier_clamped_count: int = 0
    is_obstacle_hotspot: bool = False
    obstacle_reasons: list[str] = Field(default_factory=list)
    live_score: float | None = Field(default=None, ge=0.0, le=100.0)
    live_grade: GradeLiteral | None = None
    effective_grade: GradeLiteral
    live_breakdown: dict[str, Any] | None = None
    wet_adjustment: SegmentWetAdjustment


class LiveGradingRecomputeSummary(BaseModel):
    """Structured summary returned by live grading recomputation."""

    segments_evaluated: int
    segments_with_telemetry: int
    obstacle_hotspots_count: int
    rainfall_cells_updated: int
    computed_at: str
    rainfall_summary: dict[str, Any]


def classify_confidence(sample_count: int) -> tuple[DbConfidenceLiteral, ApiConfidenceLiteral]:
    """Map sample count to DB (``baseline_only|low|medium|high``) and API confidence labels."""
    if sample_count <= 0:
        return ("baseline_only", "none")
    if sample_count <= 2:
        return ("low", "low")
    if sample_count <= 7:
        return ("medium", "medium")
    return ("high", "high")


def expected_touring_speed_kmh(baseline_score: float) -> float:
    """Expected reference speed (km/h) for a Touring 4WD given static baseline score (0..100)."""
    clamped = max(0.0, min(100.0, baseline_score))
    return max(6.0, 45.0 * math.exp(-0.0185 * clamped))


def compute_observation_delta(
    sample: ObservationSample,
    baseline_score: float,
) -> float:
    """Compute a single vehicle-normalized difficulty delta (score pts) relative to baseline."""
    vclass = sample.vehicle_capability_class
    speed_mult = _VEHICLE_SPEED_MULT.get(vclass, 1.0)
    up_sens = _VEHICLE_UPWARD_SENS.get(vclass, 1.0)
    down_sens = _VEHICLE_DOWNWARD_SENS.get(vclass, 1.0)

    exp_speed = max(4.5, expected_touring_speed_kmh(baseline_score) * speed_mult)
    obs_speed = max(0.5, float(sample.avg_speed_kmh))
    ratio = max(0.15, min(2.5, obs_speed / exp_speed))

    if ratio < 1.0:
        speed_delta = min(24.0, 20.0 * (-math.log(ratio))) * up_sens
    else:
        speed_delta = max(-18.0, -14.0 * math.log(ratio)) * down_sens

    rough_delta = 0.0
    if sample.roughness_rms is not None and math.isfinite(sample.roughness_rms):
        exp_rough = 0.20 + 0.002 * max(0.0, min(100.0, baseline_score))
        if sample.roughness_rms > exp_rough:
            rough_delta = min(14.0, (sample.roughness_rms - exp_rough) * 16.0) * up_sens

    return max(-22.0, min(28.0, speed_delta + rough_delta))


def compute_recency_weight(
    observed_at: datetime,
    *,
    now: datetime,
    match_confidence: float = 1.0,
) -> float:
    """Exponential 90-day half-life recency weight scaled by HMM match confidence."""
    obs_utc = observed_at.astimezone(UTC) if observed_at.tzinfo else observed_at.replace(tzinfo=UTC)
    age_days = max(0.0, (now - obs_utc).total_seconds() / 86400.0)
    recency = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
    conf_scale = max(0.3, min(1.0, float(match_confidence)))
    return recency * conf_scale


def build_vehicle_class_split(samples: Sequence[ObservationSample]) -> dict[str, dict[str, Any]]:
    """Build normalized count & percentage breakdown across all 4 Vehicle Capability Classes."""
    counts: dict[str, int] = {vc: 0 for vc in VEHICLE_CLASSES}
    for s in samples:
        if s.vehicle_capability_class in counts:
            counts[s.vehicle_capability_class] += 1
    total = sum(counts.values())
    return {
        vc: {
            "count": counts[vc],
            "pct": round((counts[vc] / total) * 100.0, 1) if total > 0 else 0.0,
        }
        for vc in VEHICLE_CLASSES
    }


def compute_segment_live_grade(
    *,
    baseline_score: float,
    baseline_grade: GradeLiteral,
    samples: Sequence[ObservationSample],
    wet_adjustment: SegmentWetAdjustment,
    now: datetime | None = None,
    track_median_speed_kmh: float | None = None,
) -> SegmentLiveComputation:
    """Pure, deterministic live telemetry + rainfall grading computation for one segment."""
    ref_now = (now or datetime.now(UTC)).astimezone(UTC)
    n = len(samples)
    db_conf, api_conf = classify_confidence(n)

    if n == 0:
        eff_score = min(100.0, baseline_score + wet_adjustment.wet_score_adjustment)
        eff_grade = (
            score_to_grade(eff_score)
            if wet_adjustment.wet_score_adjustment > 0.0
            else baseline_grade
        )
        return SegmentLiveComputation(
            sample_count=0,
            effective_sample_weight=0.0,
            db_confidence_level=db_conf,
            api_confidence_level=api_conf,
            confidence_score=0.0,
            effective_grade=eff_grade,
            wet_adjustment=wet_adjustment,
        )

    weights = [
        compute_recency_weight(s.observed_at, now=ref_now, match_confidence=s.match_confidence)
        for s in samples
    ]
    eff_n = sum(weights)
    w_sum = max(1e-9, eff_n)

    raw_deltas = [compute_observation_delta(s, baseline_score) for s in samples]
    outlier_clamped_count = 0

    if n >= 3:
        arr = np.array(raw_deltas, dtype=float)
        med = float(np.median(arr))
        mad = float(np.median(np.abs(arr - med)))
        mad_eff = max(4.0, 1.4826 * mad)
        lo = med - 2.5 * mad_eff
        hi = med + 2.5 * mad_eff
        clamped_deltas: list[float] = []
        for d in raw_deltas:
            if d < lo:
                clamped_deltas.append(lo)
                outlier_clamped_count += 1
            elif d > hi:
                clamped_deltas.append(hi)
                outlier_clamped_count += 1
            else:
                clamped_deltas.append(d)
    else:
        clamped_deltas = list(raw_deltas)

    weighted_delta = sum(w * d for w, d in zip(weights, clamped_deltas, strict=True)) / w_sum
    shrinkage_weight = eff_n / (eff_n + PRIOR_STRENGTH)
    shrunk_delta = weighted_delta * shrinkage_weight

    # Hard single-sample & low-sample protection so 1 outlier cannot jump a track grade
    if n == 1:
        shrunk_delta = max(-5.0, min(5.0, shrunk_delta))
    elif n == 2:
        shrunk_delta = max(-9.0, min(9.0, shrunk_delta))

    speeds = [float(s.avg_speed_kmh) for s in samples]
    durations = [float(s.traversal_duration_s) for s in samples]
    rough_vals = [float(s.roughness_rms) for s in samples if s.roughness_rms is not None]

    avg_speed_kmh = sum(w * v for w, v in zip(weights, speeds, strict=True)) / w_sum
    median_speed_kmh = float(np.median(speeds))
    avg_duration_s = sum(w * d for w, d in zip(weights, durations, strict=True)) / w_sum
    median_roughness_rms = float(np.median(rough_vals)) if rough_vals else None

    last_driven_at = max(
        (
            s.observed_at.astimezone(UTC)
            if s.observed_at.tzinfo
            else s.observed_at.replace(tzinfo=UTC)
        )
        for s in samples
    )
    vehicle_split = build_vehicle_class_split(samples)

    # Rough/slow obstacle hotspot detection (requires >=2 traversals to suppress lone stops)
    obstacle_reasons: list[str] = []
    if n >= HOTSPOT_MIN_SAMPLES:
        if avg_speed_kmh <= HOTSPOT_SLOW_SPEED_KMH:
            obstacle_reasons.append(f"Slow crawl speed ({avg_speed_kmh:.1f} km/h avg)")
        if median_roughness_rms is not None and median_roughness_rms >= HOTSPOT_ROUGHNESS_RMS:
            obstacle_reasons.append(f"High surface roughness ({median_roughness_rms:.2f} RMS)")
        if (
            track_median_speed_kmh is not None
            and track_median_speed_kmh >= 12.0
            and avg_speed_kmh <= track_median_speed_kmh * HOTSPOT_REL_SLOWDOWN_RATIO
        ):
            obstacle_reasons.append(
                f"Sharp slowdown ({avg_speed_kmh:.1f} vs "
                f"{track_median_speed_kmh:.1f} km/h track median)"
            )
    is_obstacle_hotspot = len(obstacle_reasons) > 0

    live_score = round(
        max(0.0, min(100.0, baseline_score + shrunk_delta + wet_adjustment.wet_score_adjustment)),
        1,
    )
    live_grade = score_to_grade(live_score)

    reasons: list[str] = [
        f"{n} anonymous traversal{'s' if n != 1 else ''} "
        f"(recency-weighted eff. n={eff_n:.1f}, {api_conf} confidence)",
        f"Weighted avg speed {avg_speed_kmh:.1f} km/h (median {median_speed_kmh:.1f} km/h)",
    ]
    if abs(shrunk_delta) >= 0.5:
        sign = "+" if shrunk_delta > 0 else ""
        reasons.append(f"Vehicle-normalized telemetry adjustment: {sign}{shrunk_delta:.1f} pts")
    else:
        reasons.append("Telemetry closely confirms static terrain baseline")
    if outlier_clamped_count > 0:
        plural = "s" if outlier_clamped_count != 1 else ""
        reasons.append(
            f"{outlier_clamped_count} outlier observation{plural} Winsorized for stability"
        )
    if wet_adjustment.wet_score_adjustment > 0.0:
        reasons.append(
            f"72 h rainfall ({wet_adjustment.rainfall_context['rain_72h_mm']:.1f} mm) "
            f"adds +{wet_adjustment.wet_score_adjustment:.1f} wet risk pts"
        )
    if is_obstacle_hotspot:
        reasons.append("Obstacle hotspot: " + "; ".join(obstacle_reasons))

    breakdown: dict[str, Any] = {
        "version": LIVE_FORMULA_VERSION,
        "formula_version": LIVE_FORMULA_VERSION,
        "sample_count": n,
        "effective_sample_weight": round(eff_n, 3),
        "confidence_level": api_conf,
        "last_driven_at": last_driven_at.isoformat(),
        "avg_speed_kmh": round(avg_speed_kmh, 2),
        "median_speed_kmh": round(median_speed_kmh, 2),
        "avg_duration_s": round(avg_duration_s, 2),
        "median_roughness_rms": (
            round(median_roughness_rms, 4) if median_roughness_rms is not None else None
        ),
        "vehicle_class_split": vehicle_split,
        "vehicle_normalized_delta": round(shrunk_delta, 2),
        "outlier_clamped_count": outlier_clamped_count,
        "is_obstacle_hotspot": is_obstacle_hotspot,
        "obstacle_reasons": obstacle_reasons,
        "baseline_score": round(baseline_score, 1),
        "wet_score_adjustment": round(wet_adjustment.wet_score_adjustment, 2),
        "live_score": live_score,
        "live_grade": live_grade,
        "reasons": reasons,
    }

    return SegmentLiveComputation(
        sample_count=n,
        effective_sample_weight=round(eff_n, 3),
        db_confidence_level=db_conf,
        api_confidence_level=api_conf,
        confidence_score=round(min(1.0, shrinkage_weight), 4),
        last_driven_at=last_driven_at,
        avg_speed_kmh=round(avg_speed_kmh, 2),
        median_speed_kmh=round(median_speed_kmh, 2),
        avg_duration_s=round(avg_duration_s, 2),
        median_roughness_rms=(
            round(median_roughness_rms, 4) if median_roughness_rms is not None else None
        ),
        vehicle_class_split=vehicle_split,
        vehicle_normalized_delta=round(shrunk_delta, 2),
        outlier_clamped_count=outlier_clamped_count,
        is_obstacle_hotspot=is_obstacle_hotspot,
        obstacle_reasons=obstacle_reasons,
        live_score=live_score,
        live_grade=live_grade,
        effective_grade=live_grade,
        live_breakdown=breakdown,
        wet_adjustment=wet_adjustment,
    )


_MAX_SQL_IN_PARAMS: Final[int] = 10_000
_LIVE_GRADE_BATCH_SIZE: Final[int] = 2_000


def _chunked(items: Sequence[str], size: int = _MAX_SQL_IN_PARAMS) -> list[list[str]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def recompute_live_grades(
    session: Session,
    *,
    now: datetime | None = None,
    rain_override_72h_mm: float | None = None,
    rain_override_24h_mm: float | None = None,
    fetch_live_rainfall: bool = False,
    http_client: httpx.Client | None = None,
    segment_ids: Sequence[str] | None = None,
) -> LiveGradingRecomputeSummary:
    """Recompute live grades, obstacle hotspots, and 72 h rainfall across active segments.

    Streams updates in bounded 2,000-row scalar batches via bulk primary-key ``UPDATE`` so
    even 250k+ segment statewide networks run in <60 MB RAM inside a 0.5 GB worker container.
    Leaves ``baseline_grade``, ``baseline_score``, and ``baseline_breakdown`` strictly intact.
    """
    ref_now = (now or datetime.now(UTC)).astimezone(UTC)

    # 0. Collect target active vehicular segment IDs (strings only -> ~6 MB per 250k segments)
    id_query = (
        select(TrackSegment.segment_id)
        .join(TrackSegmentGrade, TrackSegmentGrade.segment_id == TrackSegment.segment_id)
        .where(TrackSegment.is_active.is_(True), TrackSegment.is_vehicular.is_(True))
    )
    if segment_ids is None:
        target_seg_ids = list(session.execute(id_query).scalars().all())
    else:
        unique_requested = list(dict.fromkeys(segment_ids))
        target_seg_ids = []
        for id_chunk in _chunked(unique_requested):
            target_seg_ids.extend(
                session.execute(
                    id_query.where(TrackSegment.segment_id.in_(id_chunk))
                )
                .scalars()
                .all()
            )

    if not target_seg_ids:
        return LiveGradingRecomputeSummary(
            segments_evaluated=0,
            segments_with_telemetry=0,
            obstacle_hotspots_count=0,
            rainfall_cells_updated=0,
            computed_at=ref_now.isoformat(),
            rainfall_summary=get_rainfall_summary(session, now=ref_now),
        )

    # 1. Load anonymized telemetry observations without exceeding PostgreSQL's 65,535 param limit
    obs_cols = (
        TelemetryObservation.segment_id,
        TelemetryObservation.vehicle_capability_class,
        TelemetryObservation.observed_at,
        TelemetryObservation.traversal_duration_s,
        TelemetryObservation.avg_speed_kmh,
        TelemetryObservation.min_speed_kmh,
        TelemetryObservation.roughness_rms,
        TelemetryObservation.roughness_p95,
        TelemetryObservation.match_confidence,
    )
    if segment_ids is None:
        obs_rows = session.execute(
            select(*obs_cols)
            .join(TrackSegment, TrackSegment.segment_id == TelemetryObservation.segment_id)
            .where(TrackSegment.is_active.is_(True), TrackSegment.is_vehicular.is_(True))
        ).all()
    else:
        obs_rows = []
        for id_chunk in _chunked(target_seg_ids):
            obs_rows.extend(
                session.execute(
                    select(*obs_cols).where(TelemetryObservation.segment_id.in_(id_chunk))
                ).all()
            )

    samples_by_seg: dict[str, list[ObservationSample]] = defaultdict(list)
    for obs in obs_rows:
        samples_by_seg[obs.segment_id].append(
            ObservationSample(
                vehicle_capability_class=obs.vehicle_capability_class,
                observed_at=obs.observed_at,
                traversal_duration_s=float(obs.traversal_duration_s),
                avg_speed_kmh=float(obs.avg_speed_kmh),
                min_speed_kmh=float(obs.min_speed_kmh) if obs.min_speed_kmh is not None else None,
                roughness_rms=float(obs.roughness_rms) if obs.roughness_rms is not None else None,
                roughness_p95=float(obs.roughness_p95) if obs.roughness_p95 is not None else None,
                match_confidence=float(obs.match_confidence),
            )
        )

    # 2. Compute per-track median observed speed only for segments with telemetry samples
    track_seg_speeds: dict[str, list[float]] = defaultdict(list)
    if samples_by_seg:
        sampled_ids = list(samples_by_seg.keys())
        for id_chunk in _chunked(sampled_ids):
            for seg_id, track_id in session.execute(
                select(TrackSegment.segment_id, TrackSegment.track_id).where(
                    TrackSegment.segment_id.in_(id_chunk),
                    TrackSegment.track_id.is_not(None),
                )
            ).all():
                segsamp = samples_by_seg.get(seg_id)
                if segsamp and track_id is not None:
                    track_seg_speeds[track_id].append(
                        float(np.median([s.avg_speed_kmh for s in segsamp]))
                    )
    track_median_speed: dict[str, float] = {
        tid: float(np.median(spds)) for tid, spds in track_seg_speeds.items() if spds
    }

    # 3 & 4. Stream scalar segment+baseline columns in 2,000-row batches and bulk-UPDATE grades
    cell_readings: dict[str, RainfallReading] = {}
    segments_evaluated = 0
    segments_with_telemetry = 0
    obstacle_hotspots_count = 0

    batch_query_base = (
        select(
            TrackSegment.segment_id,
            TrackSegment.track_id,
            TrackSegment.surface_class,
            TrackSegment.waterway_crossing_count,
            TrackSegment.has_perennial_crossing,
            TrackSegment.is_bridged,
            func.ST_X(func.ST_LineInterpolatePoint(TrackSegment.geom, 0.5)).label("mid_lon"),
            func.ST_Y(func.ST_LineInterpolatePoint(TrackSegment.geom, 0.5)).label("mid_lat"),
            TrackSegmentGrade.baseline_score,
            TrackSegmentGrade.baseline_grade,
        )
        .join(TrackSegmentGrade, TrackSegmentGrade.segment_id == TrackSegment.segment_id)
    )

    for id_batch in _chunked(target_seg_ids, size=_LIVE_GRADE_BATCH_SIZE):
        rows = session.execute(
            batch_query_base.where(TrackSegment.segment_id.in_(id_batch))
        ).all()
        batch_updates: list[dict[str, Any]] = []
        for row in rows:
            lon = float(row.mid_lon)
            lat = float(row.mid_lat)
            cell_id, _, _ = spatial_cell_key(lon, lat)
            reading = cell_readings.get(cell_id)
            if reading is None:
                reading = resolve_cell_rainfall(
                    session,
                    lon,
                    lat,
                    now=ref_now,
                    rain_override_72h_mm=rain_override_72h_mm,
                    rain_override_24h_mm=rain_override_24h_mm,
                    fetch_live=fetch_live_rainfall,
                    client=http_client,
                )
                cell_readings[cell_id] = reading

            wet_adj = compute_segment_wet_adjustment(
                reading,
                surface_class=row.surface_class,
                waterway_crossing_count=int(row.waterway_crossing_count or 0),
                has_perennial_crossing=bool(row.has_perennial_crossing),
                is_bridged=bool(row.is_bridged),
            )
            segsamp = samples_by_seg.get(row.segment_id, [])
            t_med = track_median_speed.get(row.track_id) if row.track_id is not None else None

            result = compute_segment_live_grade(
                baseline_score=float(row.baseline_score),
                baseline_grade=row.baseline_grade,  # type: ignore[arg-type]
                samples=segsamp,
                wet_adjustment=wet_adj,
                now=ref_now,
                track_median_speed_kmh=t_med,
            )

            batch_updates.append(
                {
                    "segment_id": row.segment_id,
                    "live_grade": result.live_grade,
                    "live_score": result.live_score,
                    "live_breakdown": result.live_breakdown,
                    "effective_grade": result.effective_grade,
                    "confidence_level": result.db_confidence_level,
                    "confidence_score": result.confidence_score,
                    "sample_count": result.sample_count,
                    "last_driven_at": result.last_driven_at,
                    "avg_speed_kmh": result.avg_speed_kmh,
                    "avg_duration_s": result.avg_duration_s,
                    "vehicle_class_split": result.vehicle_class_split,
                    "rainfall_context": result.wet_adjustment.rainfall_context,
                    "wet_risk_level": result.wet_adjustment.db_wet_risk_level,
                    "live_computed_at": ref_now,
                }
            )
            segments_evaluated += 1
            if result.sample_count > 0:
                segments_with_telemetry += 1
            if result.is_obstacle_hotspot:
                obstacle_hotspots_count += 1

        if batch_updates:
            session.execute(update(TrackSegmentGrade), batch_updates)

    session.flush()
    session.expire_all()
    return LiveGradingRecomputeSummary(
        segments_evaluated=segments_evaluated,
        segments_with_telemetry=segments_with_telemetry,
        obstacle_hotspots_count=obstacle_hotspots_count,
        rainfall_cells_updated=len(cell_readings),
        computed_at=ref_now.isoformat(),
        rainfall_summary=get_rainfall_summary(session, now=ref_now),
    )


def get_obstacle_hotspots_geojson(
    session: Session,
    *,
    track_id: str | None = None,
) -> dict[str, Any]:
    """Return RFC 7946 Point FeatureCollection of rough/slow obstacle hotspot segments."""
    stmt = (
        select(
            TrackSegment.segment_id,
            TrackSegment.track_id,
            Track.name.label("track_name"),
            func.ST_X(func.ST_LineInterpolatePoint(TrackSegment.geom, 0.5)).label("lon"),
            func.ST_Y(func.ST_LineInterpolatePoint(TrackSegment.geom, 0.5)).label("lat"),
            TrackSegmentGrade.live_grade,
            TrackSegmentGrade.effective_grade,
            TrackSegmentGrade.avg_speed_kmh,
            TrackSegmentGrade.sample_count,
            TrackSegmentGrade.wet_risk_level,
            TrackSegmentGrade.live_breakdown,
            TrackSegmentGrade.rainfall_context,
        )
        .join(TrackSegmentGrade, TrackSegmentGrade.segment_id == TrackSegment.segment_id)
        .outerjoin(Track, Track.track_id == TrackSegment.track_id)
        .where(
            TrackSegment.is_active.is_(True),
            TrackSegment.is_vehicular.is_(True),
            TrackSegmentGrade.live_breakdown.is_not(None),
        )
        .order_by(TrackSegment.track_id, TrackSegment.segment_seq, TrackSegment.segment_id)
    )
    if track_id is not None:
        stmt = stmt.where(TrackSegment.track_id == track_id)

    rows = session.execute(stmt).all()
    features: list[dict[str, Any]] = []
    for row in rows:
        lb = row.live_breakdown if isinstance(row.live_breakdown, dict) else {}
        if not lb.get("is_obstacle_hotspot"):
            continue
        rc = row.rainfall_context if isinstance(row.rainfall_context, dict) else {}
        lon = round(float(row.lon), 6)
        lat = round(float(row.lat), 6)
        features.append(
            {
                "type": "Feature",
                "id": row.segment_id,
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "segment_id": row.segment_id,
                    "track_id": row.track_id,
                    "track_name": row.track_name or "Unnamed Track",
                    "avg_speed_kmh": (
                        round(float(row.avg_speed_kmh), 2)
                        if row.avg_speed_kmh is not None
                        else lb.get("avg_speed_kmh")
                    ),
                    "median_roughness_rms": lb.get("median_roughness_rms"),
                    "obstacle_reasons": list(lb.get("obstacle_reasons") or []),
                    "live_grade": row.live_grade or row.effective_grade,
                    "wet_risk_level": rc.get("wet_risk_level") or row.wet_risk_level,
                    "sample_count": int(row.sample_count or 0),
                    "lon": lon,
                    "lat": lat,
                },
            }
        )

    return {
        "type": "FeatureCollection",
        "count": len(features),
        "attribution": OPEN_METEO_ATTRIBUTION,
        "features": features,
    }


def get_live_summaries_for_tracks(
    session: Session,
    track_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Aggregate Slice 07 live telemetry, rainfall, and obstacle hotspots per track.

    Designed for direct use by ``api/tracks.py`` (Track List & Track Inspector) and
    ``packs/builder.py`` offline snapshots.
    """
    unique_ids = list(dict.fromkeys(track_ids))
    if not unique_ids:
        return {}

    rows = session.execute(
        select(
            TrackSegment.track_id,
            TrackSegment.segment_id,
            TrackSegment.segment_seq,
            TrackSegment.length_m,
            func.ST_X(func.ST_LineInterpolatePoint(TrackSegment.geom, 0.5)).label("lon"),
            func.ST_Y(func.ST_LineInterpolatePoint(TrackSegment.geom, 0.5)).label("lat"),
            TrackSegmentGrade.baseline_score,
            TrackSegmentGrade.live_grade,
            TrackSegmentGrade.live_score,
            TrackSegmentGrade.confidence_level,
            TrackSegmentGrade.sample_count,
            TrackSegmentGrade.last_driven_at,
            TrackSegmentGrade.avg_speed_kmh,
            TrackSegmentGrade.avg_duration_s,
            TrackSegmentGrade.vehicle_class_split,
            TrackSegmentGrade.live_breakdown,
            TrackSegmentGrade.rainfall_context,
        )
        .join(TrackSegmentGrade, TrackSegmentGrade.segment_id == TrackSegment.segment_id)
        .where(
            TrackSegment.track_id.in_(unique_ids),
            TrackSegment.is_active.is_(True),
            TrackSegment.is_vehicular.is_(True),
        )
        .order_by(TrackSegment.track_id, TrackSegment.segment_seq, TrackSegment.segment_id)
    ).all()

    by_track: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        if row.track_id is not None:
            by_track[row.track_id].append(row)

    summaries: dict[str, dict[str, Any]] = {}
    for tid in unique_ids:
        seg_rows = by_track.get(tid, [])
        summaries[tid] = _summarize_track_segments(seg_rows)
    return summaries


def get_track_live_summary(session: Session, track_id: str) -> dict[str, Any]:
    """Return Slice 07 live grading, rainfall context, and obstacle hotspots for one track."""
    return get_live_summaries_for_tracks(session, [track_id]).get(
        track_id, _summarize_track_segments([])
    )


def _summarize_track_segments(seg_rows: Sequence[Any]) -> dict[str, Any]:
    """Roll up segment-level live grading & rainfall rows into a Track Inspector summary."""
    empty_split = {vc: {"count": 0, "pct": 0.0} for vc in VEHICLE_CLASSES}
    if not seg_rows:
        return {
            "live_grade": None,
            "live_score": None,
            "confidence_level": "none",
            "sample_count": 0,
            "last_driven_at": None,
            "avg_speed_kmh": None,
            "est_duration_min": None,
            "vehicle_class_split": empty_split,
            "live_breakdown": None,
            "rainfall_context": None,
            "obstacle_hotspots": [],
        }

    # Pick most impactful rainfall context across segments (highest wet_score_adjustment / rain_72h)
    best_rain: dict[str, Any] | None = None
    best_rain_key: tuple[float, float] = (-1.0, -1.0)
    for row in seg_rows:
        rc = row.rainfall_context
        if isinstance(rc, dict):
            key = (
                float(rc.get("wet_score_adjustment") or 0.0),
                float(rc.get("rain_72h_mm") or 0.0),
            )
            if key > best_rain_key:
                best_rain_key = key
                best_rain = dict(rc)

    # Walk segments in order to compute cumulative distance for hotspot pins + live roll-up
    cum_dist_m = 0.0
    total_len_m = 0.0
    live_rows: list[Any] = []
    obstacle_hotspots: list[dict[str, Any]] = []
    vc_counts: dict[str, int] = {vc: 0 for vc in VEHICLE_CLASSES}
    max_sample_count = 0
    latest_driven: datetime | None = None
    speeds_weighted: list[tuple[float, float]] = []
    rough_vals: list[float] = []
    deltas: list[float] = []
    clamped_total = 0

    for row in seg_rows:
        seg_len = float(row.length_m or 0.0)
        mid_dist = cum_dist_m + seg_len * 0.5
        cum_dist_m += seg_len
        total_len_m += seg_len

        sc = int(row.sample_count or 0)
        if sc <= 0 or row.live_score is None:
            continue

        live_rows.append(row)
        max_sample_count = max(max_sample_count, sc)

        if row.last_driven_at is not None:
            dt = row.last_driven_at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            if latest_driven is None or dt > latest_driven:
                latest_driven = dt

        if row.avg_speed_kmh is not None and row.avg_speed_kmh > 0:
            speeds_weighted.append((float(row.avg_speed_kmh), max(1.0, seg_len)))

        lb = row.live_breakdown if isinstance(row.live_breakdown, dict) else {}
        if lb.get("median_roughness_rms") is not None:
            rough_vals.append(float(lb["median_roughness_rms"]))
        if lb.get("vehicle_normalized_delta") is not None:
            deltas.append(float(lb["vehicle_normalized_delta"]))
        clamped_total += int(lb.get("outlier_clamped_count") or 0)

        vcs = row.vehicle_class_split if isinstance(row.vehicle_class_split, dict) else {}
        for vc in VEHICLE_CLASSES:
            entry = vcs.get(vc)
            if isinstance(entry, dict):
                vc_counts[vc] += int(entry.get("count") or 0)
            elif isinstance(entry, (int, float)):
                vc_counts[vc] += int(entry)

        if lb.get("is_obstacle_hotspot"):
            obstacle_hotspots.append(
                {
                    "segment_id": row.segment_id,
                    "dist_m": round(mid_dist, 1),
                    "lon": round(float(row.lon), 6),
                    "lat": round(float(row.lat), 6),
                    "avg_speed_kmh": (
                        round(float(row.avg_speed_kmh), 2)
                        if row.avg_speed_kmh is not None
                        else lb.get("avg_speed_kmh")
                    ),
                    "roughness_rms": lb.get("median_roughness_rms"),
                    "reasons": list(lb.get("obstacle_reasons") or []),
                }
            )

    if not live_rows:
        return {
            "live_grade": None,
            "live_score": None,
            "confidence_level": "none",
            "sample_count": 0,
            "last_driven_at": None,
            "avg_speed_kmh": None,
            "est_duration_min": None,
            "vehicle_class_split": empty_split,
            "live_breakdown": None,
            "rainfall_context": best_rain,
            "obstacle_hotspots": [],
        }

    # Track-level live score uses p90 of live segment scores (crux difficulty minus 1-seg fluke)
    live_scores = sorted(float(r.live_score) for r in live_rows)
    p90_score = round(float(np.percentile(live_scores, 90)), 1)
    live_grade = score_to_grade(p90_score)

    _, api_conf = classify_confidence(max_sample_count)
    vc_total = sum(vc_counts.values())
    vc_split = {
        vc: {
            "count": vc_counts[vc],
            "pct": round((vc_counts[vc] / vc_total) * 100.0, 1) if vc_total > 0 else 0.0,
        }
        for vc in VEHICLE_CLASSES
    }

    avg_speed_kmh: float | None = None
    est_duration_min: float | None = None
    if speeds_weighted:
        w_tot = sum(w for _, w in speeds_weighted)
        avg_speed_kmh = round(sum(s * w for s, w in speeds_weighted) / w_tot, 2)
        if avg_speed_kmh > 0.5 and total_len_m > 0.0:
            est_duration_min = round((total_len_m / 1000.0) / avg_speed_kmh * 60.0, 1)

    median_speed_kmh = (
        round(float(np.median([s for s, _ in speeds_weighted])), 2) if speeds_weighted else None
    )
    median_rough = round(float(np.median(rough_vals)), 4) if rough_vals else None
    mean_delta = round(sum(deltas) / len(deltas), 2) if deltas else 0.0

    # Pick representative crux breakdown for detailed reasons
    crux_row = max(live_rows, key=lambda r: float(r.live_score))
    crux_lb = crux_row.live_breakdown if isinstance(crux_row.live_breakdown, dict) else {}
    avg_dur_s = round(
        sum(float(r.avg_duration_s or 0.0) for r in live_rows) / len(live_rows), 2
    )

    reasons: list[str] = [
        f"Live p90 track score {p90_score:.1f} ({live_grade}) from up to "
        f"{max_sample_count} traversals/segment ({api_conf} confidence)",
    ]
    if avg_speed_kmh is not None:
        dur_part = f" (~{est_duration_min:.0f} min total)" if est_duration_min is not None else ""
        reasons.append(f"Average observed travel speed {avg_speed_kmh:.1f} km/h{dur_part}")
    if obstacle_hotspots:
        reasons.append(
            f"{len(obstacle_hotspots)} slow/rough obstacle hotspot"
            f"{'s' if len(obstacle_hotspots) != 1 else ''} detected along track"
        )
    if best_rain and best_rain.get("warning"):
        reasons.append(str(best_rain["warning"]))

    track_live_breakdown: dict[str, Any] = {
        "version": LIVE_FORMULA_VERSION,
        "sample_count": max_sample_count,
        "effective_sample_weight": crux_lb.get("effective_sample_weight", float(max_sample_count)),
        "confidence_level": api_conf,
        "last_driven_at": latest_driven.isoformat() if latest_driven is not None else None,
        "avg_speed_kmh": avg_speed_kmh,
        "median_speed_kmh": median_speed_kmh,
        "avg_duration_s": avg_dur_s,
        "median_roughness_rms": median_rough,
        "vehicle_class_split": vc_split,
        "vehicle_normalized_delta": mean_delta,
        "outlier_clamped_count": clamped_total,
        "is_obstacle_hotspot": len(obstacle_hotspots) > 0,
        "obstacle_reasons": (
            list(obstacle_hotspots[0]["reasons"]) if obstacle_hotspots else []
        ),
        "live_score": p90_score,
        "live_grade": live_grade,
        "reasons": reasons,
    }

    return {
        "live_grade": live_grade,
        "live_score": p90_score,
        "confidence_level": api_conf,
        "sample_count": max_sample_count,
        "last_driven_at": latest_driven.isoformat() if latest_driven is not None else None,
        "avg_speed_kmh": avg_speed_kmh,
        "est_duration_min": est_duration_min,
        "vehicle_class_split": vc_split,
        "live_breakdown": track_live_breakdown,
        "rainfall_context": best_rain,
        "obstacle_hotspots": obstacle_hotspots,
    }
