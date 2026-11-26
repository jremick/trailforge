"""Privacy-by-design buffer trimming and keyed HMAC contribution hashing.

Enforces docs/SPEC_GIS_SCHEMA.md Section 4.2:
  1. Mandatory >= 250 m start/end privacy buffer trimming in EPSG:3577
     (Australian Albers meters) before any map-matching or community persistence.
  2. Trips with raw metric length <= 2 * trim_m (<= 500 m at default 250 m)
     are marked ``too_short=True`` and emit zero trimmed points.
  3. Keyed HMAC-SHA256 ``contribution_hash`` decouples ``telemetry_observations``
     and ``unmapped_track_candidates`` from ``user_id`` and ``trip_id`` while
     allowing deterministic revocation when a user toggles visibility or deletes.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Sequence
from datetime import timedelta
from functools import lru_cache
from uuid import UUID

from pydantic import BaseModel, Field
from pyproj import Transformer
from shapely.geometry import LineString

from trailforge.matching.protocol import GpsPoint

MIN_PRIVACY_TRIM_M: float = 250.0
MAX_PRIVACY_TRIM_M: float = 5000.0
_MIN_RETAINED_LENGTH_M: float = 1.0


@lru_cache(maxsize=1)
def _wgs84_to_albers() -> Transformer:
    return Transformer.from_crs("EPSG:4326", "EPSG:3577", always_xy=True)


@lru_cache(maxsize=1)
def _albers_to_wgs84() -> Transformer:
    return Transformer.from_crs("EPSG:3577", "EPSG:4326", always_xy=True)


class PrivacyTrimResult(BaseModel):
    """Result of mandatory start/end privacy buffer trimming in EPSG:3577."""

    trimmed_points: list[GpsPoint]
    raw_length_m: float = Field(..., ge=0.0)
    trimmed_length_m: float = Field(..., ge=0.0)
    trim_m: float = Field(..., ge=MIN_PRIVACY_TRIM_M, le=MAX_PRIVACY_TRIM_M)
    too_short: bool


def _interp_optional(a: float | None, b: float | None, frac: float) -> float | None:
    if a is None or b is None:
        return None
    return a + (b - a) * frac


def _interpolate_point_at_distance(
    points: Sequence[GpsPoint],
    xs: Sequence[float],
    ys: Sequence[float],
    cum_m: Sequence[float],
    target_m: float,
) -> GpsPoint:
    """Linearly interpolate a GpsPoint at exact cumulative EPSG:3577 distance ``target_m``."""
    inv = _albers_to_wgs84()
    for i in range(1, len(cum_m)):
        if cum_m[i] >= target_m or i == len(cum_m) - 1:
            span = cum_m[i] - cum_m[i - 1]
            frac = 0.0 if span <= 1e-9 else max(0.0, min(1.0, (target_m - cum_m[i - 1]) / span))
            x = xs[i - 1] + (xs[i] - xs[i - 1]) * frac
            y = ys[i - 1] + (ys[i] - ys[i - 1]) * frac
            lon, lat = inv.transform(x, y)

            p0 = points[i - 1]
            p1 = points[i]
            dt = (p1.timestamp - p0.timestamp).total_seconds()
            ts = p0.timestamp + timedelta(seconds=dt * frac)
            return GpsPoint(
                lat=round(float(lat), 7),
                lon=round(float(lon), 7),
                timestamp=ts,
                elev_m=_interp_optional(p0.elev_m, p1.elev_m, frac),
                accuracy_m=_interp_optional(p0.accuracy_m, p1.accuracy_m, frac),
                speed_mps=_interp_optional(p0.speed_mps, p1.speed_mps, frac),
                roughness_rms=_interp_optional(p0.roughness_rms, p1.roughness_rms, frac),
            )
    raise AssertionError("Unreachable interpolation state")


def measure_trace_length_m(points: Sequence[GpsPoint]) -> float:
    """Measure total metric trace length in EPSG:3577 (Australian Albers meters)."""
    if len(points) < 2:
        return 0.0
    fwd = _wgs84_to_albers()
    xs, ys = fwd.transform([p.lon for p in points], [p.lat for p in points])
    return sum(math.hypot(xs[i] - xs[i - 1], ys[i] - ys[i - 1]) for i in range(1, len(points)))


def points_to_linestring(points: Sequence[GpsPoint]) -> LineString:
    """Convert a sequence of ``GpsPoint`` fixes into a WGS84 ``LineString``."""
    if len(points) < 2:
        raise ValueError("LineString requires at least 2 GPS points")
    return LineString([(p.lon, p.lat) for p in points])


def trim_privacy_buffer(
    points: Sequence[GpsPoint],
    trim_m: float = MIN_PRIVACY_TRIM_M,
) -> PrivacyTrimResult:
    """Strictly trim ``trim_m`` (>= 250 m) from both ends of a GPS trace in EPSG:3577.

    Raises:
        ValueError: If ``trim_m`` is non-finite, below 250 m, or above 5000 m.
    """
    if not math.isfinite(trim_m) or trim_m < MIN_PRIVACY_TRIM_M or trim_m > MAX_PRIVACY_TRIM_M:
        raise ValueError(
            f"privacy_trim_m must be between {MIN_PRIVACY_TRIM_M:.1f} and "
            f"{MAX_PRIVACY_TRIM_M:.1f} meters, got {trim_m!r}"
        )

    trim_val = float(trim_m)
    if len(points) < 2:
        return PrivacyTrimResult(
            trimmed_points=[],
            raw_length_m=0.0,
            trimmed_length_m=0.0,
            trim_m=trim_val,
            too_short=True,
        )

    fwd = _wgs84_to_albers()
    xs, ys = fwd.transform([p.lon for p in points], [p.lat for p in points])

    cum_m: list[float] = [0.0]
    for i in range(1, len(points)):
        step = math.hypot(xs[i] - xs[i - 1], ys[i] - ys[i - 1])
        cum_m.append(cum_m[-1] + step)

    total_m = cum_m[-1]
    if total_m <= 2.0 * trim_val:
        return PrivacyTrimResult(
            trimmed_points=[],
            raw_length_m=round(total_m, 2),
            trimmed_length_m=0.0,
            trim_m=trim_val,
            too_short=True,
        )

    start_cut = trim_val
    end_cut = total_m - trim_val

    start_pt = _interpolate_point_at_distance(points, xs, ys, cum_m, start_cut)
    end_pt = _interpolate_point_at_distance(points, xs, ys, cum_m, end_cut)

    interior = [
        points[i]
        for i in range(len(points))
        if (start_cut + 1e-6) < cum_m[i] < (end_cut - 1e-6)
    ]
    trimmed = [start_pt, *interior, end_pt]

    trimmed_len = measure_trace_length_m(trimmed)
    if trimmed_len < _MIN_RETAINED_LENGTH_M:
        return PrivacyTrimResult(
            trimmed_points=[],
            raw_length_m=round(total_m, 2),
            trimmed_length_m=0.0,
            trim_m=trim_val,
            too_short=True,
        )

    return PrivacyTrimResult(
        trimmed_points=trimmed,
        raw_length_m=round(total_m, 2),
        trimmed_length_m=round(trimmed_len, 2),
        trim_m=trim_val,
        too_short=False,
    )


def compute_contribution_hash(
    secret_key: str,
    trip_id: str | UUID,
    nonce: str | UUID,
) -> str:
    """Compute a deterministic 64-char hex HMAC-SHA256 key for revocable anonymous telemetry.

    Allows deletion of all public ``telemetry_observations`` and
    ``unmapped_track_candidates`` rows belonging to a trip when its visibility
    is toggled back to ``private`` or deleted, without ever storing ``user_id``
    or ``trip_id`` in public tables.
    """
    if not secret_key:
        raise ValueError("secret_key must be non-empty")
    message = f"trailforge:contrib:v1:{trip_id}:{nonce}".encode()
    return hmac.new(secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()
