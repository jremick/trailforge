"""Offline tests for TrailForge vector & Terrain-RGB PMTiles build pipeline (Slice 03).

Unit tests run 100% natively on macOS host (zero GDAL/tippecanoe required), while full binary
PMTiles header, layer, and Terrain-RGB verification runs cleanly inside Docker (or any host
with GDAL + tippecanoe + pmtiles installed).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from shapely.geometry import shape
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from trailforge.ingestion.connectors import load_corridor_fixtures
from trailforge.ingestion.pipeline import ingest_corridor_dataset
from trailforge.tiles.builder import (
    DEFAULT_SNOWLINE_BANDS_M,
    MANIFEST_FILENAME,
    POI_CATEGORY_TO_ICON,
    TERRAIN_PMTILES_FILENAME,
    VECTOR_PMTILES_FILENAME,
    _parse_postgis_box2d,
    _StreamGeoJSONWriter,
    build_terrain_pmtiles,
    build_tiles,
    export_postgis_vector_layers,
    generate_contours_geojson,
    generate_snowline_bands_geojson,
)
from trailforge.tiles.cli import build_parser

REPO_ROOT = Path(__file__).resolve().parents[1]
HAS_FULL_TILE_CLI = all(
    shutil.which(cmd) is not None
    for cmd in ("gdalwarp", "gdal_translate", "gdaladdo", "gdal_contour", "tippecanoe", "pmtiles")
)


def test_poi_icon_mapping_targets_existing_svg_assets() -> None:
    assert POI_CATEGORY_TO_ICON["campground"] == "campground"
    assert POI_CATEGORY_TO_ICON["hut"] == "hut"
    assert POI_CATEGORY_TO_ICON["trig"] == "trig"
    icons_dir = REPO_ROOT / "assets" / "icons"
    if not icons_dir.is_dir():
        # Minimal runtime container only copies src/ + tests/ + migrations/; verify on full repo
        return
    for category, icon_name in POI_CATEGORY_TO_ICON.items():
        svg_path = icons_dir / f"{icon_name}.svg"
        assert svg_path.is_file(), f"POI category {category!r} points to missing icon {svg_path}"


@pytest.mark.parametrize("force_pure_python", [False, True])
def test_generate_snowline_bands_geojson_monotonic_thresholds(
    fixtures_dir: Path, tmp_path: Path, force_pure_python: bool
) -> None:
    dem_path = fixtures_dir / "dem_alps_corridor.tif"
    out_geojson = tmp_path / f"snowline_{force_pure_python}.geojson"

    count, bounds = generate_snowline_bands_geojson(
        dem_path,
        out_geojson,
        thresholds_m=DEFAULT_SNOWLINE_BANDS_M,
        force_pure_python=force_pure_python,
    )
    assert count == len(DEFAULT_SNOWLINE_BANDS_M)  # Alps DEM 600..1633 m covers 800..1600 m
    assert bounds[0] == pytest.approx(148.05, abs=1e-4)
    assert bounds[1] == pytest.approx(-36.68, abs=1e-4)
    assert bounds[2] == pytest.approx(148.20, abs=1e-4)
    assert bounds[3] == pytest.approx(-36.55, abs=1e-4)

    fc = json.loads(out_geojson.read_text(encoding="utf-8"))
    assert fc["type"] == "FeatureCollection"
    areas_by_threshold: dict[int, float] = {}
    for feat in fc["features"]:
        elev_min = feat["properties"]["elev_min_m"]
        geom = shape(feat["geometry"])
        assert geom.is_valid and not geom.is_empty
        assert geom.geom_type in ("Polygon", "MultiPolygon")
        areas_by_threshold[int(elev_min)] = float(geom.area)

    assert sorted(areas_by_threshold.keys()) == list(DEFAULT_SNOWLINE_BANDS_M)
    # Higher elevation threshold must strictly cover a smaller or equal area
    thresholds = list(DEFAULT_SNOWLINE_BANDS_M)
    for lo, hi in zip(thresholds, thresholds[1:], strict=False):
        assert areas_by_threshold[lo] > areas_by_threshold[hi] > 0.0


def test_cli_argument_parser_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["--output-dir", "/tmp/tiles", "--dem", "tests/fixtures/dem_alps_corridor.tif"]
    )
    assert args.output_dir == Path("/tmp/tiles")
    assert args.dem == Path("tests/fixtures/dem_alps_corridor.tif")
    assert args.minzoom == 8
    assert args.maxzoom == 15
    assert args.contour_interval == 20.0


def test_parse_postgis_box2d_and_stream_geojson_writer(tmp_path: Path) -> None:
    assert _parse_postgis_box2d("BOX(146.4 -37.6,148.45 -36.35)") == (
        146.4,
        -37.6,
        148.45,
        -36.35,
    )
    assert _parse_postgis_box2d(None) is None
    assert _parse_postgis_box2d("INVALID") is None

    empty_path = tmp_path / "empty.geojson"
    with _StreamGeoJSONWriter(empty_path) as writer:
        assert writer.count == 0
    assert not empty_path.exists()

    out_path = tmp_path / "streamed.geojson"
    with _StreamGeoJSONWriter(out_path) as writer:
        writer.write_feature(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [147.2, -37.1]},
                "properties": {"name": "Billy Goat Bluff"},
            }
        )
        writer.write_raw_feature(
            '{"type":"Feature","geometry":{"type":"Point","coordinates":[147.3,-37.2]},'
            '"properties":{"name":"Blue Rag"}}'
        )
    assert writer.count == 2
    fc = json.loads(out_path.read_text(encoding="utf-8"))
    assert fc["type"] == "FeatureCollection"
    assert [f["properties"]["name"] for f in fc["features"]] == [
        "Billy Goat Bluff",
        "Blue Rag",
    ]


@pytest.mark.integration
def test_export_postgis_vector_layers_contract(
    postgis_engine: Engine, fixtures_dir: Path, tmp_path: Path
) -> None:
    with Session(postgis_engine) as session:
        ingest_corridor_dataset(session, load_corridor_fixtures(fixtures_dir))
        layer_paths, layer_counts, bounds = export_postgis_vector_layers(session, tmp_path)

    assert bounds is not None
    assert layer_counts["track_segments"] == 444
    assert layer_counts["steep_sections"] > 0
    assert layer_counts["waterways"] == 2  # Murray River + Limestone Creek (Connector excluded)
    assert layer_counts["pois"] == 5
    assert layer_counts["capad_parks"] == 2

    # Validate locked vector layer property schemas (#23)
    seg_fc = json.loads(layer_paths["track_segments"].read_text(encoding="utf-8"))
    sample_seg = seg_fc["features"][0]["properties"]
    for key in (
        "segment_id",
        "track_id",
        "track_name",
        "grade",
        "score",
        "max_slope_deg",
        "sustained_slope_deg",
        "min_elev_m",
        "max_elev_m",
        "is_steep_highlight",
        "is_above_snowline",
        "waterway_crossing_count",
        "surface_class",
        "tracktype",
        "is_4wd_only",
        "has_gate_barrier",
        "access_status",
    ):
        assert key in sample_seg

    steep_fc = json.loads(layer_paths["steep_sections"].read_text(encoding="utf-8"))
    for feat in steep_fc["features"]:
        assert feat["properties"]["max_slope_deg"] >= 14.0
        assert set(feat["properties"].keys()) == {
            "segment_id",
            "track_id",
            "track_name",
            "max_slope_deg",
            "grade",
        }

    poi_fc = json.loads(layer_paths["pois"].read_text(encoding="utf-8"))
    for feat in poi_fc["features"]:
        assert set(feat["properties"].keys()) == {
            "poi_id",
            "name",
            "category",
            "elevation_m",
            "icon",
        }

    ww_fc = json.loads(layer_paths["waterways"].read_text(encoding="utf-8"))
    assert {f["properties"]["name"] for f in ww_fc["features"]} == {
        "Murray River",
        "Limestone Creek",
    }
    assert any(f["properties"]["perennial"] is True for f in ww_fc["features"])

    park_fc = json.loads(layer_paths["capad_parks"].read_text(encoding="utf-8"))
    assert {f["properties"]["name"] for f in park_fc["features"]} == {
        "Alpine National Park",
        "Kosciuszko National Park",
    }


@pytest.mark.skipif(not HAS_FULL_TILE_CLI, reason="GDAL/tippecanoe/pmtiles CLIs require Docker env")
def test_generate_contours_and_terrain_pmtiles_binary(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    dem_path = fixtures_dir / "dem_alps_corridor.tif"

    # 1. Contours via gdal_contour (20 m interval, 100 m index)
    contours_geojson = tmp_path / "contours.geojson"
    contour_count = generate_contours_geojson(dem_path, contours_geojson)
    assert contour_count > 20
    cfc = json.loads(contours_geojson.read_text(encoding="utf-8"))
    assert any(f["properties"]["is_index"] is True for f in cfc["features"])
    assert any(f["properties"]["is_index"] is False for f in cfc["features"])
    for feat in cfc["features"]:
        elev_m = feat["properties"]["elev_m"]
        assert elev_m % 20 == 0
        assert feat["properties"]["is_index"] == (elev_m % 100 == 0)

    # 2. Terrain-RGB PMTiles via gdalwarp -> rio-rgbify -> MBTiles -> pmtiles convert
    terrain_pmtiles = tmp_path / TERRAIN_PMTILES_FILENAME
    build_terrain_pmtiles(dem_path, terrain_pmtiles, minzoom=8, maxzoom=13)
    assert terrain_pmtiles.is_file()
    assert terrain_pmtiles.read_bytes()[:7] == b"PMTiles"

    proc = subprocess.run(
        ["pmtiles", "show", str(terrain_pmtiles)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "pmtiles spec version: 3" in proc.stdout
    assert "tile type: png" in proc.stdout
    assert "min zoom: 8" in proc.stdout
    assert "max zoom: 13" in proc.stdout


@pytest.mark.integration
@pytest.mark.skipif(not HAS_FULL_TILE_CLI, reason="GDAL/tippecanoe/pmtiles CLIs require Docker env")
def test_build_tiles_full_corridor_end_to_end(
    postgis_engine: Engine, fixtures_dir: Path, tmp_path: Path
) -> None:
    dem_path = fixtures_dir / "dem_alps_corridor.tif"
    with Session(postgis_engine) as session:
        ingest_corridor_dataset(session, load_corridor_fixtures(fixtures_dir))
        result = build_tiles(session, tmp_path, dem_path=dem_path)

    assert result.manifest_path == tmp_path / MANIFEST_FILENAME
    assert result.vector_pmtiles == tmp_path / VECTOR_PMTILES_FILENAME
    assert result.terrain_pmtiles == tmp_path / TERRAIN_PMTILES_FILENAME
    assert result.vector_pmtiles.is_file() and result.terrain_pmtiles.is_file()

    # Validate PMTiles v3 magic bytes
    assert result.vector_pmtiles.read_bytes()[:7] == b"PMTiles"
    assert result.terrain_pmtiles.read_bytes()[:7] == b"PMTiles"

    # Validate manifest JSON contract
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["vector_available"] is True
    assert manifest["terrain_available"] is True
    assert manifest["vector_url"] == f"/api/tiles/{VECTOR_PMTILES_FILENAME}"
    assert manifest["terrain_url"] == f"/api/tiles/{TERRAIN_PMTILES_FILENAME}"
    assert manifest["snowline_bands_m"] == list(DEFAULT_SNOWLINE_BANDS_M)
    expected_layers = {
        "track_segments",
        "steep_sections",
        "waterways",
        "pois",
        "capad_parks",
        "contours",
        "snowline_bands",
    }
    assert set(manifest["layers"]) == expected_layers
    assert manifest["layer_counts"]["track_segments"] == 444
    assert manifest["layer_counts"]["snowline_bands"] == 7

    # Inspect vector PMTiles header + metadata via pmtiles CLI
    proc = subprocess.run(
        ["pmtiles", "show", "--metadata", str(result.vector_pmtiles)],
        capture_output=True,
        text=True,
        check=True,
    )
    meta = json.loads(proc.stdout)
    vector_layer_ids = {lyr["id"] for lyr in meta["vector_layers"]}
    assert expected_layers.issubset(vector_layer_ids)
