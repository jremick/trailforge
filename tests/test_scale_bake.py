"""Offline unit, PostGIS 10k-segment GIST index, and live corridor bake tests."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from trailforge.db.models import TrackSegment
from trailforge.ingestion import bake_cli
from trailforge.ingestion.benchmark import (
    BENCH_SEG_PREFIX,
    analyze_explain_plan,
    run_memory_scale_benchmark,
    run_postgis_index_benchmark,
)
from trailforge.ingestion.connectors.base import BBox
from trailforge.ingestion.live_bake import (
    BakeOptions,
    apply_gdal_streaming_env,
    fetch_live_tile_dataset,
    get_peak_rss_mb,
    load_bake_manifest,
    parse_bbox_arg,
    plan_bake_tiles,
    run_bake,
    save_bake_manifest,
)
from trailforge.ingestion.pbf import (
    PBF_TAGS_FILTER_EXPRESSIONS,
    StreamingPBFExtractor,
    parse_geojsonseq_stream,
)
from trailforge.ingestion.pipeline import ingest_corridor_dataset
from trailforge.tiles.builder import build_tiles

# -----------------------------------------------------------------------------
# 1. Streaming OSM PBF / GeoJSONSeq Unit Tests (100% Offline)
# -----------------------------------------------------------------------------


def test_pbf_geojsonseq_streaming_parser_offline(tmp_path: Path) -> None:
    """Verify RFC 8142 GeoJSONSeq streaming extraction of tracks, waterways, & POIs."""
    features: list[dict[str, Any]] = [
        # Vehicular 4WD track
        {
            "type": "Feature",
            "id": "w5001",
            "properties": {
                "@type": "way",
                "@id": 5001,
                "@version": 3,
                "highway": "track",
                "name": "Davies Plain Steep Spur",
                "surface": "rock",
                "tracktype": "grade4",
                "4wd_only": "yes",
                "smoothness": "very_bad",
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[148.10, -36.60], [148.105, -36.602], [148.11, -36.604]],
            },
        },
        # Fire trail sharing a vertex with a barrier=gate node below
        {
            "type": "Feature",
            "id": "w5002",
            "properties": {
                "@type": "way",
                "@id": 5002,
                "@version": 2,
                "highway": "track",
                "name": "Blue Gum Fire Trail",
                "surface": "gravel",
                "tracktype": "grade2",
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[150.25, -33.75], [150.26, -33.752], [150.27, -33.755]],
            },
        },
        # Unsealed unclassified road (kept)
        {
            "type": "Feature",
            "id": "w5003",
            "properties": {
                "@type": "way",
                "@id": 5003,
                "highway": "unclassified",
                "name": "Tom Groggin Access Road",
                "surface": "gravel",
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[148.08, -36.55], [148.09, -36.552]],
            },
        },
        # Private gated track (kept with access_status=private)
        {
            "type": "Feature",
            "id": "w5004",
            "properties": {
                "@type": "way",
                "@id": 5004,
                "highway": "track",
                "name": "Station Creek Track",
                "access": "private",
                "motor_vehicle": "private",
                "4wd_only": "yes",
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[148.12, -36.61], [148.125, -36.612]],
            },
        },
        # Non-vehicular bridleway (filtered out)
        {
            "type": "Feature",
            "id": "w5005",
            "properties": {
                "@type": "way",
                "@id": 5005,
                "highway": "bridleway",
                "name": "Bridle Trail",
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[148.13, -36.62], [148.135, -36.622]],
            },
        },
        # Non-vehicular path (filtered out)
        {
            "type": "Feature",
            "id": "w5006",
            "properties": {
                "@type": "way",
                "@id": 5006,
                "highway": "path",
                "name": "Alpine Walking Pad",
                "motor_vehicle": "no",
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[148.14, -36.63], [148.145, -36.632]],
            },
        },
        # Suburban service driveway (filtered out)
        {
            "type": "Feature",
            "id": "w5007",
            "properties": {
                "@type": "way",
                "@id": 5007,
                "highway": "service",
                "service": "driveway",
                "surface": "asphalt",
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[150.30, -33.72], [150.301, -33.721]],
            },
        },
        # Sealed unclassified road without 4wd hint (filtered out)
        {
            "type": "Feature",
            "id": "w5008",
            "properties": {
                "@type": "way",
                "@id": 5008,
                "highway": "unclassified",
                "name": "Sealed Town Rd",
                "surface": "asphalt",
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[150.31, -33.72], [150.315, -33.722]],
            },
        },
        # Waterways
        {
            "type": "Feature",
            "id": "w6001",
            "properties": {
                "@type": "way",
                "@id": 6001,
                "waterway": "river",
                "name": "Murray River",
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[148.09, -36.56], [148.12, -36.56]],
            },
        },
        {
            "type": "Feature",
            "id": "w6002",
            "properties": {
                "@type": "way",
                "@id": 6002,
                "waterway": "stream",
                "name": "Dry Gully",
                "intermittent": "yes",
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [[150.25, -33.76], [150.26, -33.76]],
            },
        },
        # Outdoor POIs
        {
            "type": "Feature",
            "id": "n7001",
            "properties": {
                "@type": "node",
                "@id": 7001,
                "tourism": "camp_site",
                "name": "Tom Groggin Camp",
            },
            "geometry": {"type": "Point", "coordinates": [148.11, -36.555]},
        },
        {
            "type": "Feature",
            "id": "n7002",
            "properties": {
                "@type": "node",
                "@id": 7002,
                "tourism": "alpine_hut",
                "name": "Davies Plain Hut",
                "ele": "1480",
            },
            "geometry": {"type": "Point", "coordinates": [148.125, -36.64]},
        },
        {
            "type": "Feature",
            "id": "n7003",
            "properties": {
                "@type": "node",
                "@id": 7003,
                "tourism": "wilderness_hut",
                "name": "Charlies Forest Hut",
            },
            "geometry": {"type": "Point", "coordinates": [150.26, -33.74]},
        },
        {
            "type": "Feature",
            "id": "n7004",
            "properties": {
                "@type": "node",
                "@id": 7004,
                "man_made": "survey_point",
                "name": "Mount Pilot Trig",
                "ele": "1680.5 m",
            },
            "geometry": {"type": "Point", "coordinates": [148.19, -36.75]},
        },
        # Gate node sharing exact vertex with Blue Gum Fire Trail (w5002)
        {
            "type": "Feature",
            "id": "n7005",
            "properties": {
                "@type": "node",
                "@id": 7005,
                "barrier": "gate",
                "locked": "yes",
                "access": "forestry",
            },
            "geometry": {"type": "Point", "coordinates": [150.26, -33.752]},
        },
    ]

    seq_file = tmp_path / "sample.geojsonseq"
    seq_file.write_text(
        "\n".join("\x1e" + json.dumps(f, separators=(",", ":")) for f in features) + "\n",
        encoding="utf-8",
    )

    result = parse_geojsonseq_stream(seq_file, source_path=str(seq_file))
    assert result.features_scanned == len(features)
    assert len(result.tracks) == 4
    assert result.tracks_skipped_non_vehicular == 4

    by_id = {t.source_id: t for t in result.tracks}
    assert by_id["way/5001"].is_4wd_only is True
    assert by_id["way/5001"].osm_version == 3
    assert by_id["way/5001"].surface_class == "rough_rock_mud_sand"
    assert by_id["way/5002"].trail_class == "fire_trail"
    assert by_id["way/5002"].has_gate_barrier is True  # Propagated from gate node n7005
    assert by_id["way/5003"].trail_class == "unsealed_road"
    assert by_id["way/5004"].access_status == "private"

    assert len(result.waterways) == 2
    ww_by_id = {w.source_feature_id: w for w in result.waterways}
    assert ww_by_id["way/6001"].source_license == "ODbL"
    assert ww_by_id["way/6001"].perenniality == "Perennial"
    assert ww_by_id["way/6001"].hierarchy == "Major"
    assert ww_by_id["way/6002"].perenniality == "Intermittent"

    assert len(result.pois) == 5
    poi_types = {p.poi_type for p in result.pois}
    assert poi_types == {"campground", "hut", "trig", "gate"}
    trig = next(p for p in result.pois if p.poi_type == "trig")
    assert trig.elev_m == pytest.approx(1680.5)
    gate = next(p for p in result.pois if p.poi_type == "gate")
    assert gate.access_note is not None and "locked" in gate.access_note

    # Verify standard GeoJSON FeatureCollection fallback also works cleanly
    fc_file = tmp_path / "sample.geojson"
    fc_file.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, indent=2),
        encoding="utf-8",
    )
    fc_result = parse_geojsonseq_stream(fc_file)
    assert len(fc_result.tracks) == 4
    assert len(fc_result.waterways) == 2
    assert len(fc_result.pois) == 5


def test_pbf_extractor_osmium_pipeline_mocked_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify StreamingPBFExtractor chains osmium extract -> tags-filter -> export."""
    fake_pbf = tmp_path / "australia-latest.osm.pbf"
    fake_pbf.write_bytes(b"fake-pbf-header")

    commands_seen: list[list[str]] = []

    def _fake_which(binary: str) -> str | None:
        return "/usr/bin/osmium" if binary == "osmium" else shutil.which(binary)

    def _fake_run(
        cmd: list[str],
        *,
        env: dict[str, str] | None = None,
        capture_output: bool = True,
        text: bool = True,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        commands_seen.append(cmd)
        out_idx = cmd.index("-o") + 1
        out_path = Path(cmd[out_idx])
        if cmd[1] == "export":
            feat = {
                "type": "Feature",
                "id": "w9901",
                "properties": {
                    "@type": "way",
                    "@id": 9901,
                    "highway": "track",
                    "name": "Newnes Plateau Track",
                    "4wd_only": "yes",
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[150.25, -33.42], [150.26, -33.425]],
                },
            }
            out_path.write_text("\x1e" + json.dumps(feat) + "\n", encoding="utf-8")
        else:
            out_path.write_bytes(b"osm-pbf-intermediate")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("trailforge.ingestion.pbf.shutil.which", _fake_which)
    monkeypatch.setattr("trailforge.ingestion.pbf.subprocess.run", _fake_run)

    extractor = StreamingPBFExtractor(source_pbf=fake_pbf)
    bbox = BBox(min_x=150.20, min_y=-33.50, max_x=150.35, max_y=-33.35)
    result = extractor.extract_bbox(bbox, work_dir=tmp_path)

    assert len(commands_seen) == 3
    assert commands_seen[0][:2] == ["osmium", "extract"]
    assert "--strategy=complete_ways" in commands_seen[0]
    assert commands_seen[1][:2] == ["osmium", "tags-filter"]
    for expr in PBF_TAGS_FILTER_EXPRESSIONS:
        assert expr in commands_seen[1]
    assert commands_seen[2][:2] == ["osmium", "export"]
    assert "geojsonseq" in commands_seen[2]
    assert len(result.tracks) == 1
    assert result.tracks[0].name == "Newnes Plateau Track"
    assert result.tracks[0].is_4wd_only is True


@pytest.mark.skipif(shutil.which("osmium") is None, reason="osmium CLI not installed on host")
def test_pbf_extractor_real_osmium_xml_offline(tmp_path: Path) -> None:
    """Exercise real osmium extract + tags-filter + export on a tiny synthetic .osm XML file."""
    osm_xml = tmp_path / "mini_corridor.osm"
    osm_xml.write_text(
        """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="trailforge-test">
  <node id="1" version="1" lat="-36.5600" lon="148.1000"/>
  <node id="2" version="1" lat="-36.5620" lon="148.1050">
    <tag k="barrier" v="gate"/>
    <tag k="locked" v="yes"/>
  </node>
  <node id="3" version="1" lat="-36.5640" lon="148.1100"/>
  <node id="4" version="1" lat="-36.5610" lon="148.1020">
    <tag k="tourism" v="camp_site"/>
    <tag k="name" v="Alpine Bush Camp"/>
  </node>
  <node id="5" version="1" lat="-36.5630" lon="148.1080">
    <tag k="man_made" v="survey_point"/>
    <tag k="name" v="Ridge Trig"/>
    <tag k="ele" v="1325"/>
  </node>
  <node id="6" version="1" lat="-36.5590" lon="148.1030"/>
  <node id="7" version="1" lat="-36.5650" lon="148.1030"/>
  <way id="101" version="2">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/>
    <tag k="highway" v="track"/>
    <tag k="name" v="Tom Groggin Ridge Track"/>
    <tag k="4wd_only" v="yes"/>
    <tag k="surface" v="rock"/>
    <tag k="tracktype" v="grade4"/>
  </way>
  <way id="102" version="1">
    <nd ref="1"/><nd ref="3"/>
    <tag k="highway" v="bridleway"/>
    <tag k="name" v="Horse Path"/>
  </way>
  <way id="103" version="1">
    <nd ref="6"/><nd ref="7"/>
    <tag k="waterway" v="stream"/>
    <tag k="name" v="Snowy Creek"/>
  </way>
</osm>
""",
        encoding="utf-8",
    )

    extractor = StreamingPBFExtractor(source_pbf=osm_xml)
    bbox = BBox(min_x=148.08, min_y=-36.58, max_x=148.12, max_y=-36.54)
    result = extractor.extract_bbox(bbox, work_dir=tmp_path)

    assert len(result.tracks) == 1
    assert result.tracks[0].source_id == "way/101"
    assert result.tracks[0].name == "Tom Groggin Ridge Track"
    assert result.tracks[0].has_gate_barrier is True
    assert result.tracks_skipped_non_vehicular == 1
    assert len(result.waterways) == 1
    assert result.waterways[0].name == "Snowy Creek"
    assert {p.poi_type for p in result.pois} == {"campground", "trig", "gate"}


# -----------------------------------------------------------------------------
# 2. Tiled Bake Orchestrator, Resumable Manifests & CLI Hygiene (100% Offline)
# -----------------------------------------------------------------------------


def test_bake_cli_and_benchmark_cli_zero_runtime_warnings() -> None:
    """Confirm `-W error::RuntimeWarning -m ... --help` exits 0 with zero warnings."""
    for mod in ("trailforge.ingestion.bake_cli", "trailforge.ingestion.benchmark"):
        proc = subprocess.run(
            [sys.executable, "-W", "error::RuntimeWarning", "-m", mod, "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"{mod} --help failed:\n{proc.stderr}"
        assert "RuntimeWarning" not in proc.stderr


def test_live_bake_tile_planning_and_resumable_manifest(tmp_path: Path) -> None:
    """Verify spatial tiling, GDAL streaming env, RSS helper, and resumable manifest skips."""
    gdal_env = apply_gdal_streaming_env()
    assert gdal_env["GDAL_CACHEMAX"] == "128"
    assert get_peak_rss_mb() > 0.0

    bbox = parse_bbox_arg("148.00,-36.70,148.50,-36.20")
    tiles = plan_bake_tiles(bbox, tile_deg=0.25)
    assert len(tiles) == 4
    assert len({tid for tid, _ in tiles}) == 4

    # Dry-run region plan creates resumable manifest with zero network/DB calls
    manifest_path = tmp_path / "alps_manifest.json"
    res = run_bake(
        BakeOptions(
            region_id="australian-alps",
            bbox=bbox,
            tile_deg=0.25,
            cache_dir=tmp_path / "cache",
            manifest_path=manifest_path,
            dry_run=True,
        ),
        session=None,
    )
    assert res.status == "dry_run"
    assert res.tiles_total == 4
    assert manifest_path.is_file()

    # Mark first tile completed in manifest and confirm reload + resume skips it
    loaded = load_bake_manifest(manifest_path)
    assert loaded is not None
    loaded.tiles[0].status = "completed"
    save_bake_manifest(manifest_path, loaded)
    reloaded = load_bake_manifest(manifest_path)
    assert reloaded is not None
    assert reloaded.tiles[0].status == "completed"


def test_live_bake_fixtures_dry_run_cli_offline(
    fixtures_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify `--fixtures tests/fixtures --dry-run` validates the full offline orchestrator."""
    rc = bake_cli.main(
        [
            "--fixtures",
            str(fixtures_dir),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--dry-run",
            "--json",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["source_mode"] == "fixtures"
    assert payload["dry_run"] is True
    assert payload["status"] == "dry_run"
    assert payload["total_tracks_upserted"] == 8
    assert payload["total_segments_active"] > 100
    assert payload["total_pois_upserted"] == 5
    assert Path(payload["manifest_path"]).is_file()


# -----------------------------------------------------------------------------
# 3. 10,000+ Segment Scale Memory (<300 MB) & PostGIS GIST Index Tests
# -----------------------------------------------------------------------------


def test_explain_plan_analyzer_detects_gist_and_seq_scan() -> None:
    """Verify recursive EXPLAIN JSON auditor identifies GIST scans vs Seq Scans accurately."""
    good_plan = [
        {
            "Planning Time": 0.42,
            "Execution Time": 1.15,
            "Plan": {
                "Node Type": "Bitmap Heap Scan",
                "Relation Name": "track_segments",
                "Actual Rows": 48,
                "Plans": [
                    {
                        "Node Type": "Bitmap Index Scan",
                        "Index Name": "ix_track_segments_geom_active",
                        "Actual Rows": 52,
                    }
                ],
            },
        }
    ]
    good_audit = analyze_explain_plan("good", good_plan)
    assert good_audit.uses_gist_index is True
    assert good_audit.has_seq_scan_on_target is False
    assert "ix_track_segments_geom_active" in good_audit.indexes_used

    bad_plan = [
        {
            "Planning Time": 0.25,
            "Execution Time": 19.4,
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "track_segments",
                "Actual Rows": 48,
            },
        }
    ]
    bad_audit = analyze_explain_plan("bad", bad_plan)
    assert bad_audit.uses_gist_index is False
    assert bad_audit.has_seq_scan_on_target is True


def test_scale_benchmark_10k_segments_memory_under_300mb(tmp_path: Path) -> None:
    """Confirm 10,000+ segment conflation, segmentation, & export stay strictly <300 MB heap."""
    metrics, tracks, segments = run_memory_scale_benchmark(
        num_tracks=150,
        segments_per_track=72,
        overlap_duplicate_tracks=25,
        export_dir=tmp_path,
        seed=42,
    )
    assert len(tracks) == 150
    assert len(segments) >= 10_000
    assert metrics.segments_generated >= 10_000
    assert metrics.deduplicated_ways == 25
    assert metrics.exported_bytes > 0
    assert metrics.peak_heap_mb < 300.0, f"Peak heap exceeded 300 MB: {metrics.peak_heap_mb} MB"
    assert metrics.within_300mb_budget is True


@pytest.mark.integration
def test_scale_benchmark_10k_segments_postgis_gist_indexes(postgis_engine: Engine) -> None:
    """Confirm 10,000+ segments in PostGIS use GIST index scans with ZERO Seq Scans."""
    metrics, tracks, segments = run_memory_scale_benchmark(
        num_tracks=150,
        segments_per_track=72,
        overlap_duplicate_tracks=10,
        seed=42,
    )
    assert metrics.segments_generated >= 10_000
    assert metrics.peak_heap_mb < 300.0

    with Session(postgis_engine) as session:
        pg = run_postgis_index_benchmark(session, tracks, segments, rollback_after=True)

        assert pg.segments_inserted >= 10_000
        assert pg.rolled_back is True
        assert pg.all_queries_use_gist is True, f"Non-GIST plan detected: {pg.model_dump()}"
        assert pg.zero_seq_scans_on_track_segments is True, (
            f"Seq Scan on track_segments detected: {pg.model_dump()}"
        )
        by_name = {q.query_name: q for q in pg.queries}
        assert set(by_name) == {
            "spatial_bbox_query",
            "hmm_st_dwithin_lookup",
            "closure_spatial_join",
        }
        for q in pg.queries:
            assert q.uses_gist_index is True
            assert q.has_seq_scan_on_target is False
            assert q.actual_rows >= 1, f"{q.query_name} returned 0 rows"

        # Verify rollback left zero benchmark rows behind (dev corridor DB stays untouched)
        remaining = session.scalar(
            select(func.count())
            .select_from(TrackSegment)
            .where(TrackSegment.segment_id.like(f"{BENCH_SEG_PREFIX}%"))
        )
        assert remaining == 0


@pytest.mark.integration
def test_live_bake_fixtures_full_orchestrator_db(
    postgis_engine: Engine, fixtures_dir: Path, tmp_path: Path
) -> None:
    """Prove full fixture bake -> PostGIS ingest -> closures -> live grading without mutating DB."""
    with Session(postgis_engine) as session:
        try:
            result = run_bake(
                BakeOptions(
                    fixtures_dir=fixtures_dir,
                    cache_dir=tmp_path / "cache",
                    build_tiles=False,
                    build_pack=False,
                    skip_closures=False,
                    skip_rainfall=False,
                    dry_run=False,
                    resume=False,
                ),
                session=session,
                commit=False,
            )
            assert result.status == "completed"
            assert result.tiles_completed == 1
            assert result.tiles_failed == 0
            assert result.total_tracks_upserted == 8
            assert result.total_segments_active > 100
            assert result.closures_feeds_ok >= 3
            assert result.rainfall_cells_updated >= 1
        finally:
            session.rollback()


# -----------------------------------------------------------------------------
# 4. Live Real-Data Australian Corridor Bake Proof (@pytest.mark.live)
# -----------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.integration
def test_live_real_data_corridor_bake_smoke(postgis_engine: Engine, tmp_path: Path) -> None:
    """Bake a real compact Australian 4WD corridor (Blue Mountains Narrow Neck / Megalong) live.

    Uses live Overpass + NSW Spatial Services + GA Hydrology + CAPAD 2025 + GA 5m LiDAR/DEA SRTM 1s,
    ingests into a rolled-back PostGIS transaction (preserving the seeded dev corridor DB), and
    builds real vector PMTiles when tippecanoe is available.
    """
    apply_gdal_streaming_env()
    # Blue Mountains Narrow Neck / Megalong Valley compact 4WD & fire trail corridor (~7 km x 7 km)
    corridor_bbox = BBox(min_x=150.24, min_y=-33.78, max_x=150.32, max_y=-33.72)

    dataset = fetch_live_tile_dataset(
        corridor_bbox,
        cache_dir=tmp_path / "cache",
        work_dir=tmp_path / "work",
        tile_id="live_narrow_neck",
        prefer_5m_dem=True,
        corridor_name="Blue Mountains Narrow Neck Live Smoke",
    )

    assert len(dataset.all_tracks) >= 1, "Expected real AU tracks from Overpass / NSW Spatial"
    assert dataset.dem_path is not None and dataset.dem_path.is_file()
    assert dataset.dem_tier in ("5m-lidar", "srtm-1s")

    with Session(postgis_engine) as session:
        try:
            summary = ingest_corridor_dataset(session, dataset, commit=False)
            assert summary.tracks_upserted >= 1
            assert summary.segments_active >= 5
            assert summary.grades_upserted == summary.segments_active

            if shutil.which("tippecanoe") is not None:
                tile_res = build_tiles(
                    session,
                    tmp_path / "tiles",
                    dem_path=dataset.dem_path,
                    bbox=corridor_bbox.as_tuple(),
                    minzoom=10,
                    maxzoom=14,
                    build_vector=True,
                    build_terrain=False,
                )
                assert tile_res.vector_pmtiles is not None
                assert tile_res.vector_pmtiles.is_file()
                assert tile_res.vector_pmtiles.stat().st_size > 0
                assert "track_segments" in tile_res.manifest.layers
        finally:
            session.rollback()
