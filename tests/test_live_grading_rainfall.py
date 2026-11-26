"""Offline unit, PostGIS integration, & opt-in live tests for Slice 07 live grading & rainfall."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select, text
from sqlalchemy.orm import Session

from trailforge.api.grading import router as grading_router
from trailforge.auth.security import create_access_token, hash_password
from trailforge.db.models import (
    RainfallSnapshot,
    TelemetryObservation,
    Track,
    TrackSegment,
    TrackSegmentGrade,
    User,
)
from trailforge.db.session import get_db
from trailforge.grading.cli import build_parser
from trailforge.grading.live import (
    _MAX_SQL_IN_PARAMS,
    LIVE_FORMULA_VERSION,
    ObservationSample,
    _chunked,
    compute_segment_live_grade,
    get_obstacle_hotspots_geojson,
    get_track_live_summary,
    recompute_live_grades,
)
from trailforge.grading.rainfall import (
    OPEN_METEO_ATTRIBUTION,
    RainfallReading,
    classify_regional_wet_risk,
    compute_segment_wet_adjustment,
    fetch_open_meteo_rainfall,
    parse_open_meteo_hourly_payload,
    resolve_cell_rainfall,
    spatial_cell_key,
)
from trailforge.ingestion.connectors import load_corridor_fixtures
from trailforge.ingestion.pipeline import ingest_corridor_dataset

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _make_reading(
    rain_72h_mm: float,
    rain_24h_mm: float | None = None,
    *,
    now: datetime | None = None,
    is_stale: bool = False,
) -> RainfallReading:
    ref = now or datetime(2026, 11, 26, 6, 0, tzinfo=UTC)
    r24 = round(rain_72h_mm * 0.4, 2) if rain_24h_mm is None else rain_24h_mm
    api_risk, db_risk = classify_regional_wet_risk(rain_72h_mm)
    cell_id, lon, lat = spatial_cell_key(148.12, -36.60)
    return RainfallReading(
        grid_cell_id=cell_id,
        centroid_lon=lon,
        centroid_lat=lat,
        rain_24h_mm=r24,
        rain_72h_mm=rain_72h_mm,
        wet_risk_level=api_risk,
        db_wet_risk_level=db_risk,
        fetched_at=ref,
        is_stale=is_stale,
    )


# -----------------------------------------------------------------------------
# Pure Offline Unit Tests (zero database required)
# -----------------------------------------------------------------------------


def test_chunked_segment_id_batches_stay_under_postgres_param_limit() -> None:
    ids = [f"seg_{i}" for i in range(72_500)]
    chunks = _chunked(ids)
    assert len(chunks) == 8
    assert all(len(c) <= _MAX_SQL_IN_PARAMS for c in chunks)
    assert [item for chunk in chunks for item in chunk] == ids


def test_rainfall_classification_and_surface_crossing_modulation() -> None:
    assert classify_regional_wet_risk(0.0) == ("dry", "dry")
    assert classify_regional_wet_risk(4.9) == ("dry", "dry")
    assert classify_regional_wet_risk(5.0) == ("damp", "damp")
    assert classify_regional_wet_risk(15.0) == ("wet", "wet")
    assert classify_regional_wet_risk(35.0) == ("heavy_rain", "saturated")

    heavy = _make_reading(48.0, 22.0)

    # Sealed road without unbridged crossings MUST have zero penalty and no mud warning
    sealed_adj = compute_segment_wet_adjustment(
        heavy, surface_class="sealed", waterway_crossing_count=0, is_bridged=False
    )
    assert sealed_adj.wet_score_adjustment == 0.0
    assert sealed_adj.wet_risk_level == "dry"
    assert sealed_adj.db_wet_risk_level == "dry"
    assert sealed_adj.warning is None
    assert "Open-Meteo" in sealed_adj.rainfall_context["attribution"]
    assert "CC BY 4.0" in sealed_adj.rainfall_context["attribution"]

    # Bridged dirt segment gets surface delta only (zero crossing bonus)
    bridged_dirt = compute_segment_wet_adjustment(
        heavy,
        surface_class="dirt_unsealed",
        waterway_crossing_count=1,
        has_perennial_crossing=True,
        is_bridged=True,
    )
    # Unbridged rough/mud perennial river crossing hits max +12.0 cap with explicit warning
    unbridged_rough = compute_segment_wet_adjustment(
        heavy,
        surface_class="rough_rock_mud_sand",
        waterway_crossing_count=1,
        has_perennial_crossing=True,
        is_bridged=False,
    )
    assert bridged_dirt.wet_score_adjustment < unbridged_rough.wet_score_adjustment
    assert unbridged_rough.wet_score_adjustment == 12.0
    assert unbridged_rough.wet_risk_level == "heavy_rain"
    assert unbridged_rough.db_wet_risk_level == "saturated"
    assert unbridged_rough.warning is not None and "48.0 mm" in unbridged_rough.warning


def test_open_meteo_mocked_http_parsing_and_attribution() -> None:
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    # 72 historical hours + 24 future forecast hours
    times: list[str] = []
    precip: list[float] = []
    for h in range(-71, 25):
        ts = now + timedelta(hours=h)
        times.append(ts.strftime("%Y-%m-%dT%H:%M"))
        if h > 0:
            # Future forecast hours MUST NOT be counted in trailing 24h / 72h totals
            precip.append(50.0)
        elif h > -24:
            precip.append(0.5)  # 24 * 0.5 = 12.0 mm in last 24 h
        else:
            precip.append(0.25)  # 48 * 0.25 = 12.0 mm in prior 48 h -> 24.0 mm total 72 h

    payload = {"hourly": {"time": times, "precipitation": precip}}
    r24, r72, _ = parse_open_meteo_hourly_payload(payload, now=now)
    assert r24 == pytest.approx(12.0, abs=0.01)
    assert r72 == pytest.approx(24.0, abs=0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "api.open-meteo.com/v1/forecast" in str(request.url)
        assert request.url.params["hourly"] == "precipitation"
        assert request.url.params["past_days"] == "3"
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        reading = fetch_open_meteo_rainfall(148.12, -36.60, client=client, now=now)

    assert reading.rain_24h_mm == pytest.approx(12.0)
    assert reading.rain_72h_mm == pytest.approx(24.0)
    assert reading.wet_risk_level == "wet"
    assert reading.db_wet_risk_level == "wet"
    assert reading.attribution == OPEN_METEO_ATTRIBUTION
    assert reading.is_stale is False


def test_recency_weighting_prioritizes_recent_traversals() -> None:
    now = datetime(2026, 11, 26, 8, 0, tzinfo=UTC)
    dry_adj = compute_segment_wet_adjustment(
        _make_reading(0.0, now=now), surface_class="dirt_unsealed"
    )

    # 3 old fast passes (270 days ago = 3 half-lives = 0.125x weight each) vs
    # 3 fresh slow rough passes (today = 1.0x weight each)
    old_fast = [
        ObservationSample(
            vehicle_capability_class="Touring 4WD",
            observed_at=now - timedelta(days=270),
            traversal_duration_s=10.0,
            avg_speed_kmh=28.0,
            roughness_rms=0.15,
        )
        for _ in range(3)
    ]
    fresh_slow = [
        ObservationSample(
            vehicle_capability_class="Touring 4WD",
            observed_at=now - timedelta(hours=2),
            traversal_duration_s=55.0,
            avg_speed_kmh=5.0,
            roughness_rms=0.80,
        )
        for _ in range(3)
    ]

    res = compute_segment_live_grade(
        baseline_score=45.0,
        baseline_grade="Medium",
        samples=[*old_fast, *fresh_slow],
        wet_adjustment=dry_adj,
        now=now,
    )
    assert res.live_score is not None and res.live_score > 50.0
    assert res.live_grade == "Hard"
    assert res.avg_speed_kmh is not None and res.avg_speed_kmh < 10.0


def test_vehicle_class_normalization_awd_vs_modified_4wd() -> None:
    now = datetime(2026, 11, 26, 8, 0, tzinfo=UTC)
    dry_adj = compute_segment_wet_adjustment(
        _make_reading(0.0, now=now), surface_class="dirt_unsealed"
    )

    # On a Hard baseline segment (score 58):
    # - 5 AWDs crawling at 7.5 km/h should NOT push grade to Extreme
    awd_samples = [
        ObservationSample(
            vehicle_capability_class="AWD",
            observed_at=now - timedelta(days=i),
            traversal_duration_s=36.0,
            avg_speed_kmh=7.5,
            roughness_rms=0.35,
        )
        for i in range(5)
    ]
    awd_res = compute_segment_live_grade(
        baseline_score=58.0,
        baseline_grade="Hard",
        samples=awd_samples,
        wet_adjustment=dry_adj,
        now=now,
    )

    # - 5 Modified 4WDs crawling at 4.0 km/h with heavy roughness MUST push difficulty much higher
    mod_samples = [
        ObservationSample(
            vehicle_capability_class="Modified 4WD",
            observed_at=now - timedelta(days=i),
            traversal_duration_s=68.0,
            avg_speed_kmh=4.0,
            roughness_rms=0.90,
        )
        for i in range(5)
    ]
    mod_res = compute_segment_live_grade(
        baseline_score=58.0,
        baseline_grade="Hard",
        samples=mod_samples,
        wet_adjustment=dry_adj,
        now=now,
    )

    assert awd_res.live_score is not None and mod_res.live_score is not None
    assert awd_res.live_grade == "Hard"  # AWD slow crawl does not falsely jump to Extreme
    assert mod_res.live_score > awd_res.live_score + 10.0
    assert mod_res.vehicle_normalized_delta > awd_res.vehicle_normalized_delta


def test_single_outlier_resistance_and_confidence_tiers() -> None:
    now = datetime(2026, 11, 26, 8, 0, tzinfo=UTC)
    dry_adj = compute_segment_wet_adjustment(
        _make_reading(0.0, now=now), surface_class="dirt_unsealed"
    )

    # 1 extreme outlier traversal on a Medium (35.0) segment MUST stay bounded (<= 5 pts, low conf)
    lone_outlier = [
        ObservationSample(
            vehicle_capability_class="Modified 4WD",
            observed_at=now,
            traversal_duration_s=300.0,
            avg_speed_kmh=0.8,
            roughness_rms=3.5,
        )
    ]
    single_res = compute_segment_live_grade(
        baseline_score=35.0,
        baseline_grade="Medium",
        samples=lone_outlier,
        wet_adjustment=dry_adj,
        now=now,
    )
    assert single_res.api_confidence_level == "low"
    assert single_res.db_confidence_level == "low"
    assert single_res.live_grade == "Medium"
    assert single_res.live_score is not None and single_res.live_score <= 40.0
    assert single_res.is_obstacle_hotspot is False  # 1 pass cannot create an obstacle hotspot

    # 6 normal Touring passes + 1 extreme outlier -> MAD Winsorization clamps the outlier
    normal_passes = [
        ObservationSample(
            vehicle_capability_class="Touring 4WD",
            observed_at=now - timedelta(days=i),
            traversal_duration_s=11.0,
            avg_speed_kmh=24.0,
            roughness_rms=0.18,
        )
        for i in range(6)
    ]
    robust_res = compute_segment_live_grade(
        baseline_score=35.0,
        baseline_grade="Medium",
        samples=[*normal_passes, *lone_outlier],
        wet_adjustment=dry_adj,
        now=now,
    )
    assert robust_res.api_confidence_level == "medium"
    assert robust_res.outlier_clamped_count >= 1
    assert robust_res.live_grade == "Medium"
    assert robust_res.live_score is not None and abs(robust_res.live_score - 35.0) < 6.0

    # 8+ samples reaches "high" confidence
    high_res = compute_segment_live_grade(
        baseline_score=35.0,
        baseline_grade="Medium",
        samples=normal_passes + normal_passes[:2],
        wet_adjustment=dry_adj,
        now=now,
    )
    assert high_res.api_confidence_level == "high"
    assert high_res.db_confidence_level == "high"


def test_obstacle_hotspot_detection_triggers() -> None:
    now = datetime(2026, 11, 26, 8, 0, tzinfo=UTC)
    dry_adj = compute_segment_wet_adjustment(
        _make_reading(0.0, now=now), surface_class="dirt_unsealed"
    )

    slow_rough_pair = [
        ObservationSample(
            vehicle_capability_class="Touring 4WD",
            observed_at=now - timedelta(days=i),
            traversal_duration_s=45.0,
            avg_speed_kmh=6.2,
            roughness_rms=0.62,
        )
        for i in range(2)
    ]
    res = compute_segment_live_grade(
        baseline_score=50.0,
        baseline_grade="Hard",
        samples=slow_rough_pair,
        wet_adjustment=dry_adj,
        now=now,
        track_median_speed_kmh=18.0,
    )
    assert res.is_obstacle_hotspot is True
    assert len(res.obstacle_reasons) >= 2
    assert res.live_breakdown is not None
    assert res.live_breakdown["version"] == LIVE_FORMULA_VERSION
    assert res.live_breakdown["is_obstacle_hotspot"] is True


def test_grading_cli_parser_and_zero_runtime_warning() -> None:
    parser = build_parser()
    args = parser.parse_args(["--offline-rain-72h", "18.5", "--json"])
    assert args.offline_rain_72h == pytest.approx(18.5)
    assert args.live_rainfall is False
    assert args.json is True

    proc = subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-m", "trailforge.grading.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"RuntimeWarning or CLI error: {proc.stderr}"


# -----------------------------------------------------------------------------
# PostGIS Integration Tests (100% offline against seeded DB)
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded_corridor_engine(postgis_engine: Engine) -> Engine:
    """Ensure the synthetic Australian Alps corridor is seeded in PostGIS."""
    with Session(postgis_engine) as session:
        seg_count = session.execute(
            text("SELECT COUNT(*) FROM track_segments WHERE is_active = TRUE")
        ).scalar_one()
        if seg_count == 0:
            ingest_corridor_dataset(session, load_corridor_fixtures(_FIXTURES_DIR))
            session.commit()
    return postgis_engine


@pytest.mark.integration
def test_postgis_live_grading_rainfall_hotspots_outage_and_api(
    seeded_corridor_engine: Engine,
) -> None:
    now = datetime.now(UTC) - timedelta(hours=1)
    test_contrib_prefix = hashlib.sha256(b"slice07-test-contrib").hexdigest()[:56]

    with Session(seeded_corridor_engine) as session:
        davies_track = session.execute(
            select(Track).where(Track.name == "Davies Plain Track Lower")
        ).scalar_one()
        access_track = session.execute(
            select(Track).where(Track.name == "Tom Groggin Access Rd")
        ).scalar_one()

        davies_segs = session.execute(
            select(TrackSegment, TrackSegmentGrade)
            .join(TrackSegmentGrade, TrackSegmentGrade.segment_id == TrackSegment.segment_id)
            .where(TrackSegment.track_id == davies_track.track_id, TrackSegment.is_active.is_(True))
            .order_by(TrackSegment.segment_seq)
        ).all()
        access_seg, access_grade_before = session.execute(
            select(TrackSegment, TrackSegmentGrade)
            .join(TrackSegmentGrade, TrackSegmentGrade.segment_id == TrackSegment.segment_id)
            .where(TrackSegment.track_id == access_track.track_id, TrackSegment.is_active.is_(True))
            .order_by(TrackSegment.segment_seq)
            .limit(1)
        ).one()

        # Snapshot original baseline grades so we prove baseline is never overwritten
        hotspot_seg, hotspot_grade_before = davies_segs[5]
        orig_hotspot_baseline_grade = hotspot_grade_before.baseline_grade
        orig_hotspot_baseline_score = float(hotspot_grade_before.baseline_score)
        orig_access_baseline_grade = access_grade_before.baseline_grade

        # Clean up any leftover test observations/rainfall before seeding isolated test samples
        session.execute(
            delete(TelemetryObservation).where(
                TelemetryObservation.contribution_hash.like(f"{test_contrib_prefix}%")
            )
        )
        session.execute(delete(RainfallSnapshot))
        session.flush()

        # Seed multi-class telemetry across first 10 Davies Plain segs; seg[5] = obstacle hotspot
        try:
            obs_to_insert: list[TelemetryObservation] = []
            vehicle_cycle = ["Stock 4WD", "Touring 4WD", "Touring 4WD", "Modified 4WD"]
            for idx, (seg, _g) in enumerate(davies_segs[:10]):
                is_spot = idx == 5
                for k, vc in enumerate(vehicle_cycle):
                    chash = f"{test_contrib_prefix}{idx:04x}{k:04x}"
                    obs_to_insert.append(
                        TelemetryObservation(
                            segment_id=seg.segment_id,
                            contribution_hash=chash,
                            vehicle_capability_class=vc,
                            direction="forward",
                            observed_at=now - timedelta(days=k * 3),
                            observed_date=(now - timedelta(days=k * 3)).date(),
                            traversal_duration_s=52.0 if is_spot else 18.0,
                            avg_speed_kmh=5.1 if is_spot else 15.5,
                            min_speed_kmh=3.2 if is_spot else 12.0,
                            roughness_rms=0.78 if is_spot else 0.22,
                            roughness_p95=0.95 if is_spot else 0.30,
                            match_confidence=0.95,
                            matched_point_count=3,
                        )
                    )
            # Single extreme outlier on Tom Groggin Access Rd (Easy)
            obs_to_insert.append(
                TelemetryObservation(
                    segment_id=access_seg.segment_id,
                    contribution_hash=f"{test_contrib_prefix}ffff0001",
                    vehicle_capability_class="Modified 4WD",
                    direction="forward",
                    observed_at=now,
                    observed_date=now.date(),
                    traversal_duration_s=240.0,
                    avg_speed_kmh=1.2,
                    min_speed_kmh=0.5,
                    roughness_rms=2.2,
                    roughness_p95=2.8,
                    match_confidence=0.90,
                    matched_point_count=2,
                )
            )
            session.add_all(obs_to_insert)
            session.commit()

            # 1. Recompute with 42 mm heavy 72h rainfall override
            summary = recompute_live_grades(
                session,
                now=now,
                rain_override_72h_mm=42.0,
                rain_override_24h_mm=18.0,
            )
            session.commit()
            assert summary.segments_evaluated > 100
            assert summary.segments_with_telemetry >= 11
            assert summary.obstacle_hotspots_count >= 1
            assert summary.rainfall_cells_updated >= 1

            # 2. Verify hotspot segment DB state & untouched baseline
            session.expire_all()
            updated_hotspot = session.get(TrackSegmentGrade, hotspot_seg.segment_id)
            assert updated_hotspot is not None
            assert updated_hotspot.baseline_grade == orig_hotspot_baseline_grade
            assert float(updated_hotspot.baseline_score) == pytest.approx(
                orig_hotspot_baseline_score
            )
            assert updated_hotspot.sample_count >= 4
            assert updated_hotspot.confidence_level in ("medium", "high")
            assert updated_hotspot.live_grade in ("Hard", "Extreme")
            assert updated_hotspot.wet_risk_level == "saturated"
            assert updated_hotspot.live_breakdown is not None
            assert updated_hotspot.live_breakdown["is_obstacle_hotspot"] is True
            assert updated_hotspot.rainfall_context is not None
            assert updated_hotspot.rainfall_context["wet_risk_level"] == "heavy_rain"
            assert updated_hotspot.rainfall_context["wet_score_adjustment"] > 0.0
            assert "Open-Meteo" in updated_hotspot.rainfall_context["attribution"]

            # 3. Verify single outlier on Easy access road remained bounded & low confidence
            updated_access = session.get(TrackSegmentGrade, access_seg.segment_id)
            assert updated_access is not None
            assert updated_access.baseline_grade == orig_access_baseline_grade
            assert updated_access.confidence_level == "low"
            assert updated_access.live_grade == "Easy"

            # 4. Verify Track Inspector live summary & hotspots GeoJSON
            track_summary = get_track_live_summary(session, davies_track.track_id)
            assert track_summary["live_grade"] in ("Hard", "Extreme")
            assert track_summary["sample_count"] >= 4
            assert track_summary["vehicle_class_split"]["Touring 4WD"]["count"] > 0
            assert track_summary["rainfall_context"]["wet_risk_level"] == "heavy_rain"
            assert any(
                h["segment_id"] == hotspot_seg.segment_id
                for h in track_summary["obstacle_hotspots"]
            )

            hotspots_fc = get_obstacle_hotspots_geojson(session, track_id=davies_track.track_id)
            assert hotspots_fc["type"] == "FeatureCollection"
            assert any(
                feat["properties"]["segment_id"] == hotspot_seg.segment_id
                for feat in hotspots_fc["features"]
            )

            # 5. Verify Open-Meteo outage gracefully falls back to cached snapshot (is_stale=True)
            def failing_handler(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(503, text="Upstream temporarily unavailable")

            with httpx.Client(transport=httpx.MockTransport(failing_handler)) as fail_client:
                fallback = resolve_cell_rainfall(
                    session,
                    148.12,
                    -36.60,
                    now=now + timedelta(minutes=10),
                    fetch_live=True,
                    client=fail_client,
                )
            assert fallback.is_stale is True
            assert fallback.rain_72h_mm == pytest.approx(42.0)

            # 6. Verify FastAPI /api/grading/* endpoints via TestClient
            admin_email = "grading-admin@example.test"
            admin_user = session.execute(
                select(User).where(User.email == admin_email).limit(1)
            ).scalar_one_or_none()
            if admin_user is None:
                admin_user = User(
                    email=admin_email,
                    display_name="Grading Admin",
                    password_hash=hash_password("AdminPass123!"),
                    role="admin",
                    is_active=True,
                )
                session.add(admin_user)
                session.commit()
                session.refresh(admin_user)
            admin_headers = {
                "Authorization": (
                    f"Bearer {create_access_token(str(admin_user.id), admin_user.email)}"
                )
            }

            app = FastAPI()
            app.include_router(grading_router)

            def _override_db() -> Any:
                yield session

            app.dependency_overrides[get_db] = _override_db
            with TestClient(app) as client:
                assert (
                    client.post(
                        "/api/grading/recompute",
                        json={"rain_72h_mm": 18.5, "rain_24h_mm": 6.0},
                    ).status_code
                    == 401
                )
                r_recomp = client.post(
                    "/api/grading/recompute",
                    headers=admin_headers,
                    json={"rain_72h_mm": 18.5, "rain_24h_mm": 6.0},
                )
                assert r_recomp.status_code == 200
                body = r_recomp.json()
                assert body["obstacle_hotspots_count"] >= 1
                assert body["rainfall_summary"]["max_wet_risk_level"] == "wet"

                r_hot = client.get("/api/grading/hotspots/geojson")
                assert r_hot.status_code == 200
                assert r_hot.json()["type"] == "FeatureCollection"
                assert r_hot.json()["count"] >= 1

                r_rain = client.get("/api/grading/rainfall")
                assert r_rain.status_code == 200
                rain_json = r_rain.json()
                assert "Open-Meteo" in rain_json["attribution"]
                assert rain_json["max_rain_72h_mm"] == pytest.approx(18.5)
        finally:
            session.execute(
                delete(TelemetryObservation).where(
                    TelemetryObservation.contribution_hash.like(f"{test_contrib_prefix}%")
                )
            )
            session.execute(delete(User).where(User.email == "grading-admin@example.test"))
            session.commit()


@pytest.mark.live
def test_live_open_meteo_australian_alps_smoke() -> None:
    reading = fetch_open_meteo_rainfall(148.12, -36.60)
    assert reading.rain_72h_mm >= 0.0
    assert reading.rain_24h_mm >= 0.0
    assert reading.wet_risk_level in ("dry", "damp", "wet", "heavy_rain")
    assert "Open-Meteo" in reading.attribution
