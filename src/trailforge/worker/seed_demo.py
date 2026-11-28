"""Deterministic offline telemetry seeder for corridor live-grading & hotspot demo.

Populates strictly anonymized ``telemetry_observations`` (ZERO ``user_id``, ZERO ``trip_id``)
from the committed ``tests/fixtures/sample_trace_davies_plain.gpx`` trace across 3 Australian
4WD capability classes so ``make seed-corridor`` immediately surfaces explainable live grading,
vehicle-class traffic split, slow/rough escarpment obstacle hotspots, and 72 h rainfall context.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from geoalchemy2.shape import from_shape
from shapely.geometry import Point
from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from trailforge.config import get_settings
from trailforge.db.models import TelemetryObservation, TrackSegment, Trip, User
from trailforge.db.session import get_engine
from trailforge.matching import (
    PostGISHMMMatcher,
    compute_contribution_hash,
    parse_gpx,
    trim_privacy_buffer,
)
from trailforge.matching.contribution_keys import contribution_key_id, trip_contribution_hash

_DEMO_PROFILES: tuple[dict[str, Any], ...] = (
    {
        "tag": "touring_pass_1",
        "vehicle_class": "Touring 4WD",
        "days_ago": 3,
        "speed_factor": 0.95,
        "base_roughness": 0.34,
        "steep_roughness": 0.62,
        "steep_speed_cap": 6.4,
    },
    {
        "tag": "touring_pass_2",
        "vehicle_class": "Touring 4WD",
        "days_ago": 8,
        "speed_factor": 0.90,
        "base_roughness": 0.38,
        "steep_roughness": 0.68,
        "steep_speed_cap": 5.8,
    },
    {
        "tag": "stock_pass_1",
        "vehicle_class": "Stock 4WD",
        "days_ago": 12,
        "speed_factor": 0.80,
        "base_roughness": 0.42,
        "steep_roughness": 0.72,
        "steep_speed_cap": 4.9,
    },
    {
        "tag": "stock_pass_2",
        "vehicle_class": "Stock 4WD",
        "days_ago": 19,
        "speed_factor": 0.84,
        "base_roughness": 0.40,
        "steep_roughness": 0.65,
        "steep_speed_cap": 5.4,
    },
    {
        "tag": "modified_pass_1",
        "vehicle_class": "Modified 4WD",
        "days_ago": 5,
        "speed_factor": 1.08,
        "base_roughness": 0.30,
        "steep_roughness": 0.56,
        "steep_speed_cap": 7.1,
    },
)


def _demo_hash(tag: str) -> str:
    return hashlib.sha256(f"trailforge:demo_corridor_seed_v1:{tag}".encode()).hexdigest()


def seed_demo_telemetry(
    session: Session,
    *,
    gpx_path: Path,
    now: datetime | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Idempotently seed anonymized corridor telemetry passes from a GPX fixture."""
    ref_now = now or datetime.now(UTC)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=UTC)

    parsed = parse_gpx(gpx_path.read_bytes())
    trim = trim_privacy_buffer(parsed.points, trim_m=250.0)
    if trim.too_short or len(trim.trimmed_points) < 2:
        raise ValueError(f"GPX fixture {gpx_path} is too short after 250 m privacy trim")

    matcher = PostGISHMMMatcher(session)
    match_res = matcher.match_trace_sync(trim.trimmed_points)
    if not match_res.matched_segments:
        return {"passes_seeded": 0, "observations_inserted": 0, "segments_covered": 0}

    seg_ids = [s.segment_id for s in match_res.matched_segments]
    steep_flags = {
        str(sid): (bool(is_steep), float(max_slope or 0.0))
        for sid, is_steep, max_slope in session.execute(
            select(
                TrackSegment.segment_id,
                TrackSegment.is_steep_highlight,
                TrackSegment.max_slope_deg,
            ).where(TrackSegment.segment_id.in_(seg_ids))
        ).all()
    }

    demo_hashes = [_demo_hash(p["tag"]) for p in _DEMO_PROFILES]
    session.execute(
        delete(TelemetryObservation).where(TelemetryObservation.contribution_hash.in_(demo_hashes))
    )

    inserted = 0
    key = get_settings().privacy_secret_key
    canonical_geom = session.scalar(select(TrackSegment.geom).where(
        TrackSegment.segment_id == match_res.matched_segments[0].segment_id
    ))
    for prof in _DEMO_PROFILES:
        user_id = uuid.uuid5(uuid.NAMESPACE_URL, f"trailforge:demo:driver:{prof['tag']}")
        trip_id = uuid.uuid5(uuid.NAMESPACE_URL, f"trailforge:demo:trip:{prof['tag']}")
        nonce = uuid.uuid5(uuid.NAMESPACE_URL, f"trailforge:demo:nonce:{prof['tag']}")
        chash = compute_contribution_hash(key, trip_id, nonce)
        demo_email = f"demo-{prof['tag']}@example.invalid"
        demo_identity = session.scalar(
            pg_insert(User)
            .values(
                id=user_id,
                email=demo_email,
                display_name="Synthetic Demo Driver",
                password_hash="disabled",
                role="member",
                is_active=True,
                community_trusted_at=func.now(),
            )
            .on_conflict_do_update(
                index_elements=[User.id],
                where=and_(
                    User.email == demo_email,
                    User.display_name == "Synthetic Demo Driver",
                    User.password_hash == "disabled",
                    User.role == "member",
                ),
                set_={
                    "is_active": True,
                    "disabled_at": None,
                    "community_trusted_at": func.coalesce(User.community_trusted_at, func.now()),
                },
            )
            .returning(User.id)
        )
        if demo_identity is None:
            raise ValueError("Synthetic demo identity belongs to an unrelated account")
        prior_trip = session.get(Trip, trip_id)
        if prior_trip is not None:
            if prior_trip.user_id != demo_identity:
                raise ValueError("Synthetic demo trip is already assigned to an unrelated account")
            session.execute(delete(TelemetryObservation).where(
                TelemetryObservation.contribution_hash == trip_contribution_hash(prior_trip)
            ))
        # The active synthetic member has an unusable password. Its raw geometry field
        # contains only a public canonical placeholder for this explicit operator fixture.
        session.execute(pg_insert(Trip).values(
            id=trip_id,
            user_id=user_id,
            visibility="community_anonymous",
            contribution_key_id=contribution_key_id(key),
            contribution_nonce=nonce,
            community_consent_confirmed=True,
            vehicle_capability_class=prof["vehicle_class"],
            raw_geom=canonical_geom,
            match_status="matched",
        ).on_conflict_do_update(
            index_elements=[Trip.id],
            set_={
                "visibility": "community_anonymous",
                "community_consent_confirmed": True,
                "contribution_key_id": contribution_key_id(key),
                "contribution_nonce": nonce,
                "created_at": func.now(),
            },
        ))
        obs_base = ref_now - timedelta(days=int(prof["days_ago"]))
        t0 = match_res.matched_segments[0].entry_at

        for seg in match_res.matched_segments:
            offset_s = (seg.entry_at - t0).total_seconds()
            obs_at = obs_base + timedelta(seconds=offset_s)
            is_steep, slope_deg = steep_flags.get(seg.segment_id, (False, 0.0))

            avg_spd = max(2.5, min(160.0, float(seg.avg_speed_kmh) * float(prof["speed_factor"])))
            rough_rms = float(prof["base_roughness"])
            hotspot_geom = None
            if is_steep or slope_deg >= 18.0:
                avg_spd = min(avg_spd, float(prof["steep_speed_cap"]))
                rough_rms = float(prof["steep_roughness"])
                if seg.slow_hotspot_lonlat is not None:
                    hotspot_geom = from_shape(
                        Point(seg.slow_hotspot_lonlat[0], seg.slow_hotspot_lonlat[1]), srid=4326
                    )

            duration_s = max(
                1.0,
                round(
                    float(seg.duration_s) * (float(seg.avg_speed_kmh) / max(1.0, avg_spd)),
                    2,
                ),
            )
            min_spd = max(1.0, round(avg_spd * 0.72, 2))
            session.add(
                TelemetryObservation(
                    segment_id=seg.segment_id,
                    contribution_hash=chash,
                    vehicle_capability_class=str(prof["vehicle_class"]),
                    direction=seg.direction,
                    observed_at=obs_at,
                    observed_date=obs_at.date(),
                    traversal_duration_s=duration_s,
                    avg_speed_kmh=round(avg_spd, 2),
                    min_speed_kmh=min_spd,
                    roughness_rms=round(rough_rms, 4),
                    roughness_p95=round(rough_rms * 1.28, 4),
                    match_confidence=round(float(seg.match_confidence), 4),
                    matched_point_count=max(1, int(seg.matched_point_count)),
                    slow_hotspot_geom=hotspot_geom,
                )
            )
            inserted += 1

    if commit:
        session.commit()

    return {
        "passes_seeded": len(_DEMO_PROFILES),
        "observations_inserted": inserted,
        "segments_covered": len(set(seg_ids)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m trailforge.worker.seed_demo",
        description="Idempotently seed anonymized demo corridor telemetry observations.",
    )
    parser.add_argument(
        "--gpx",
        type=Path,
        default=Path("tests/fixtures/sample_trace_davies_plain.gpx"),
        help="Path to GPX fixture used for anonymized multi-rig demo passes.",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="Optional SQLAlchemy database URL override.",
    )
    args = parser.parse_args(argv)

    engine = get_engine(args.database_url)
    try:
        with Session(engine) as session:
            summary = seed_demo_telemetry(session, gpx_path=args.gpx, commit=True)
    finally:
        engine.dispose()
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
