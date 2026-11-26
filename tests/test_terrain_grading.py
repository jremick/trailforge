"""Offline tests for DEM sampling/draping, waterway crossings, CAPAD tenure, & baseline grading."""

from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import LineString

from tests.fixtures.generate_fixtures import alps_corridor_elev_m
from trailforge.grading.baseline import (
    FORMULA_VERSION,
    SegmentGradeInput,
    compute_baseline_grade,
    compute_vehicle_suitability,
)
from trailforge.ingestion.connectors import RawWaterwayFeature, load_corridor_fixtures
from trailforge.ingestion.crossings import count_waterway_crossings, find_capad_tenure
from trailforge.ingestion.terrain import DemSampler, drape_segment_dem


@pytest.mark.parametrize("force_pure_python", [False, True])
def test_dem_sampler_bilinear_accuracy_and_bounds(
    fixtures_dir: Path, force_pure_python: bool
) -> None:
    dem_path = fixtures_dir / "dem_alps_corridor.tif"
    with DemSampler(dem_path, force_pure_python=force_pure_python) as sampler:
        assert sampler.width == 300
        assert sampler.height == 260
        assert sampler.crs_epsg == 4326
        assert sampler.nodata == -9999.0

        # Sample interior valley, steep escarpment, and alpine ridge points
        for lon, lat in (
            (148.120, -36.565),
            (148.121, -36.597),
            (148.125, -36.640),
            (148.162, -36.553),
        ):
            sampled = sampler.sample_point(lon, lat)
            expected = alps_corridor_elev_m(lon, lat)
            assert sampled is not None
            # Bilinear interpolation across ~50 m pixels matches smooth analytic surface closely
            assert abs(sampled - expected) < 1.5, (
                f"Mismatch at ({lon}, {lat}): sampled={sampled}, expected={expected}"
            )

        # Out-of-bounds query safely returns None
        assert sampler.sample_point(140.0, -30.0) is None


def test_drape_segment_dem_corridor_tracks(fixtures_dir: Path) -> None:
    ds = load_corridor_fixtures(fixtures_dir)
    assert ds.dem_path is not None
    osm_by_id = {t.source_id: t for t in ds.osm_tracks}

    with DemSampler(ds.dem_path) as sampler:
        # way/1001: Tom Groggin Access Rd -> gentle NSW valley road (< 8 deg, below snow-line)
        m_easy = drape_segment_dem(osm_by_id["way/1001"].geometry, sampler)
        assert m_easy.length_m > 4500.0
        assert m_easy.max_elev_m is not None and m_easy.max_elev_m < 1000.0
        assert m_easy.max_slope_deg is not None and m_easy.max_slope_deg < 8.0
        assert m_easy.is_steep_highlight is False
        assert m_easy.is_above_snowline is False
        assert len(m_easy.elevation_profile) >= 10

        # way/1002: Davies Plain Lower -> steep >18 deg escarpment climb from ~605 m to >1200 m
        m_steep = drape_segment_dem(osm_by_id["way/1002"].geometry, sampler)
        assert m_steep.min_elev_m is not None and m_steep.min_elev_m < 650.0
        assert m_steep.max_elev_m is not None and m_steep.max_elev_m > 1200.0
        assert m_steep.max_slope_deg is not None and m_steep.max_slope_deg > 18.0
        assert m_steep.is_steep_highlight is True
        assert m_steep.is_above_snowline is True
        assert m_steep.elev_gain_m > 600.0

        # way/1003: Davies Plain Ridge -> entirely above 1,000 m snow-line up to >1,600 m
        m_ridge = drape_segment_dem(osm_by_id["way/1003"].geometry, sampler)
        assert m_ridge.min_elev_m is not None and m_ridge.min_elev_m > 1200.0
        assert m_ridge.max_elev_m is not None and m_ridge.max_elev_m > 1550.0
        assert m_ridge.is_above_snowline is True

        # Short ~80 m canonical steep segment directly across the escarpment inflection
        steep_seg = LineString([(148.121, -36.5966), (148.121, -36.5974)])
        m_seg = drape_segment_dem(steep_seg, sampler, sample_step_m=10.0)
        assert 75.0 <= m_seg.length_m <= 100.0
        assert m_seg.max_slope_deg is not None and m_seg.max_slope_deg > 18.0
        assert m_seg.is_steep_highlight is True


def test_waterway_crossings_and_capad_tenure(fixtures_dir: Path) -> None:
    ds = load_corridor_fixtures(fixtures_dir)
    osm_by_id = {t.source_id: t for t in ds.osm_tracks}
    vic_by_id = {t.source_id: t for t in ds.vic_tracks}

    # way/1002 crosses Murray River (Perennial Major, unbridged)
    c_davies = count_waterway_crossings(
        osm_by_id["way/1002"].geometry, ds.waterways, is_bridged=False
    )
    assert c_davies.total_count == 1
    assert c_davies.unbridged_count == 1
    assert c_davies.has_perennial_crossing is True
    assert c_davies.has_major_perennial_crossing is True
    assert c_davies.waterway_names == ["Murray River"]

    # way/1006 crosses Limestone Creek WITH bridge=yes -> 0 unbridged crossings!
    c_bridged = count_waterway_crossings(
        osm_by_id["way/1006"].geometry, ds.waterways, is_bridged=True
    )
    assert c_bridged.total_count == 1
    assert c_bridged.unbridged_count == 0
    assert c_bridged.has_perennial_crossing is False

    # VIC-only Bucky Ridge Fire Trail crosses Limestone Creek (Intermittent, unbridged)
    c_bucky = count_waterway_crossings(
        vic_by_id["VIC-TR-2002"].geometry, ds.waterways, is_bridged=False
    )
    assert c_bucky.unbridged_count == 1
    assert c_bucky.has_perennial_crossing is False
    assert c_bucky.waterway_names == ["Limestone Creek"]

    # Synthetic Connector passed directly must still be ignored defensively
    connector = RawWaterwayFeature(
        source_provider="ga_hydro",
        source_license="CC-BY-4.0",
        source_feature_id="GA-CONN-1",
        feature_type="Connector",
        geometry={"type": "LineString", "coordinates": [[148.118, -36.597], [148.124, -36.597]]},
    )
    c_conn = count_waterway_crossings(osm_by_id["way/1002"].geometry, [connector])
    assert c_conn.total_count == 0 and c_conn.unbridged_count == 0

    # CAPAD tenure lookup distinguishes VIC Alpine NP vs NSW Kosciuszko NP
    park_vic = find_capad_tenure(osm_by_id["way/1003"].geometry, ds.capad_parks)
    assert park_vic is not None
    assert park_vic["name"] == "Alpine National Park" and park_vic["state"] == "VIC"

    park_nsw = find_capad_tenure(osm_by_id["way/1001"].geometry, ds.capad_parks)
    assert park_nsw is not None
    assert park_nsw["name"] == "Kosciuszko National Park" and park_nsw["state"] == "NSW"


def test_explainable_baseline_grading_grades_and_floors() -> None:
    # 1. Easy: gentle gravel road (Tom Groggin Access Rd)
    easy = compute_baseline_grade(
        SegmentGradeInput(
            surface_class="gravel_compacted",
            tracktype="grade1",
            trail_class="unsealed_road",
            max_slope_deg=3.2,
            sustained_slope_deg=2.1,
            elev_gain_m=2.5,
            dem_tier="synthetic",
        )
    )
    assert easy.grade == "Easy"
    assert easy.score == 8.0
    assert easy.baseline_breakdown["formula_version"] == FORMULA_VERSION
    assert easy.baseline_breakdown["factors"]["surface_tracktype"]["points"] == 8
    assert len(easy.reasons) >= 1

    # 2. Medium: Poplars Fire Trail (grade3, ~9 deg climb, 4wd recommended)
    medium = compute_baseline_grade(
        SegmentGradeInput(
            surface_class="gravel_compacted",
            tracktype="grade3",
            trail_class="fire_trail",
            max_slope_deg=9.5,
            sustained_slope_deg=7.2,
            elev_gain_m=8.0,
            is_4wd_recommended=True,
            dem_tier="synthetic",
        )
    )
    assert medium.grade == "Medium"
    assert 25.0 <= medium.score < 50.0
    assert medium.baseline_breakdown["factors"]["four_wd_access"]["points"] == 5

    # 3. Hard via safety floor: 18.5 deg slope on dirt even if raw sum < 50
    hard_floor = compute_baseline_grade(
        SegmentGradeInput(
            surface_class="gravel_compacted",
            tracktype="grade2",
            trail_class="fire_trail",
            max_slope_deg=18.5,
            sustained_slope_deg=6.0,
            elev_gain_m=6.0,
        )
    )
    assert hard_floor.grade == "Hard"
    assert hard_floor.score >= 50.0
    assert "steep_or_4wd_hard_floor" in hard_floor.overrides

    # 4. Extreme: Davies Plain steep rock climb + Murray River crossing (exact spec example)
    extreme = compute_baseline_grade(
        SegmentGradeInput(
            surface_class="rough_rock_mud_sand",
            tracktype="grade4",
            trail_class="4wd_track",
            max_slope_deg=20.4,
            sustained_slope_deg=11.8,
            elev_gain_m=15.2,
            unbridged_crossing_count=1,
            has_perennial_crossing=True,
            has_major_perennial_crossing=True,
            waterway_names=["Murray River"],
            is_4wd_only=True,
            dem_tier="5m-lidar",
        )
    )
    assert extreme.grade == "Extreme"
    assert extreme.raw_score == 87.0
    assert extreme.score == 87.0
    assert "steep_rough_or_perennial_extreme_floor" in extreme.overrides
    factors = extreme.baseline_breakdown["factors"]
    assert factors["surface_tracktype"]["points"] == 26
    assert factors["max_slope"]["points"] == 30
    assert factors["sustained_relief"]["points"] == 11
    assert factors["waterway_crossings"]["points"] == 12
    assert factors["four_wd_access"]["points"] == 8

    # 5. Bridged crossing incurs 0 water points even if crossing counts were populated
    bridged = compute_baseline_grade(
        SegmentGradeInput(
            surface_class="gravel_compacted",
            tracktype="grade2",
            trail_class="4wd_track",
            max_slope_deg=4.0,
            unbridged_crossing_count=1,
            has_perennial_crossing=True,
            is_bridged=True,
        )
    )
    assert bridged.baseline_breakdown["factors"]["waterway_crossings"]["points"] == 0
    assert bridged.grade == "Easy"

    # 6. Vehicle capability suitability & wet-weather shift
    assert compute_vehicle_suitability("Easy", "AWD") == "Suited"
    assert compute_vehicle_suitability("Medium", "AWD") == "Challenging"
    assert compute_vehicle_suitability("Hard", "Stock 4WD") == "Challenging"
    assert compute_vehicle_suitability("Hard", "Touring 4WD") == "Suited"
    assert (
        compute_vehicle_suitability("Hard", "Touring 4WD", wet_risk_level="wet")
        == "Challenging"
    )
    assert (
        compute_vehicle_suitability(
            "Easy", "AWD", wet_risk_level="saturated", surface_class="sealed"
        )
        == "Suited"
    )
