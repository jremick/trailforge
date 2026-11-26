"""Offline unit tests (and optional @pytest.mark.live smoke tests) for AU data connectors."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import httpx
import numpy as np
import pytest

from trailforge.ingestion.connectors import (
    ATTRIBUTION_CAPAD,
    ATTRIBUTION_GA_DEM,
    ATTRIBUTION_GA_HYDRO,
    ATTRIBUTION_NSW_SPATIAL,
    ATTRIBUTION_OSM,
    ATTRIBUTION_VIC_DEECA,
    CORRIDOR_ALPS_BBOX,
    AUElvisDEMConnector,
    BBox,
    CAPADConnector,
    GAHydroConnector,
    NSWTrailsConnector,
    OSMConnector,
    RawPoiFeature,
    VICTrailsConnector,
    dedupe_poi_features,
    evaluate_raster_coverage_array,
    inspect_raster_coverage,
    is_in_australia_coverage,
    is_in_nsw_coverage,
    is_in_vic_coverage,
    iter_spatial_tiles,
    load_corridor_fixtures,
)


def _write_uncompressed_geotiff(
    path: Path, arr: np.ndarray, *, origin_lon: float = 148.05, origin_lat: float = -36.55
) -> Path:
    """Write a tiny valid uncompressed Float32 EPSG:4326 GeoTIFF for offline DEM guard tests."""
    rows, cols = arr.shape
    data_bytes = np.asarray(arr, dtype="<f4").tobytes()
    dx = dy = 0.001
    nodata_bytes = b"-9999.0\x00"
    geokeys = (1, 1, 0, 3, 1024, 0, 1, 2, 1025, 0, 1, 1, 2048, 0, 1, 4326)

    num_entries = 14
    ifd_offset = 8
    after_ifd = ifd_offset + 2 + num_entries * 12 + 4
    ext = bytearray()

    def alloc(payload: bytes) -> int:
        if len(ext) % 2 == 1:
            ext.append(0)
        off = after_ifd + len(ext)
        ext.extend(payload)
        return off

    ps_off = alloc(struct.pack("<3d", dx, dy, 0.0))
    tp_off = alloc(struct.pack("<6d", 0.0, 0.0, 0.0, origin_lon, origin_lat, 0.0))
    gk_off = alloc(struct.pack(f"<{len(geokeys)}H", *geokeys))
    nd_off = alloc(nodata_bytes)
    data_off = alloc(data_bytes)

    def entry(tag: int, dtype: int, count: int, val: int) -> bytes:
        if dtype == 3 and count == 1:
            return struct.pack("<HHIHH", tag, dtype, count, val, 0)
        return struct.pack("<HHII", tag, dtype, count, val)

    entries = [
        entry(256, 4, 1, cols),
        entry(257, 4, 1, rows),
        entry(258, 3, 1, 32),
        entry(259, 3, 1, 1),  # uncompressed
        entry(262, 3, 1, 1),
        entry(273, 4, 1, data_off),
        entry(277, 3, 1, 1),
        entry(278, 4, 1, rows),
        entry(279, 4, 1, len(data_bytes)),
        entry(339, 3, 1, 3),  # IEEE float
        entry(33550, 12, 3, ps_off),
        entry(33922, 12, 6, tp_off),
        entry(34735, 3, len(geokeys), gk_off),
        entry(42113, 2, len(nodata_bytes), nd_off),
    ]
    header = struct.pack("<2sHI", b"II", 42, ifd_offset)
    ifd = struct.pack("<H", num_entries) + b"".join(entries) + struct.pack("<I", 0)
    path.write_bytes(header + ifd + bytes(ext))
    return path


def test_load_corridor_fixtures_v1_and_v2(fixtures_dir: Path) -> None:
    ds_v1 = load_corridor_fixtures(fixtures_dir)
    assert ds_v1.bbox == CORRIDOR_ALPS_BBOX
    assert len(ds_v1.osm_tracks) == 7
    assert len(ds_v1.vic_tracks) == 3
    assert len(ds_v1.nsw_tracks) == 2
    # GA-HYD-999 Connector excluded by default; Murray River + Limestone Creek retained
    assert len(ds_v1.waterways) == 2
    assert {w.name for w in ds_v1.waterways} == {"Murray River", "Limestone Creek"}
    assert len(ds_v1.capad_parks) == 2
    assert {p.name for p in ds_v1.capad_parks} == {
        "Alpine National Park",
        "Kosciuszko National Park",
    }
    assert len(ds_v1.pois) == 6
    assert ds_v1.dem_path is not None and ds_v1.dem_path.is_file()
    assert ds_v1.dem_tier == "synthetic"
    for attr in (
        ATTRIBUTION_OSM,
        ATTRIBUTION_VIC_DEECA,
        ATTRIBUTION_NSW_SPATIAL,
        ATTRIBUTION_GA_HYDRO,
        ATTRIBUTION_CAPAD,
        ATTRIBUTION_GA_DEM,
    ):
        assert attr in ds_v1.attributions

    osm_by_id = {t.source_id: t for t in ds_v1.osm_tracks}
    assert osm_by_id["way/1001"].trail_class == "unsealed_road"
    assert osm_by_id["way/1001"].surface_class == "gravel_compacted"
    assert osm_by_id["way/1002"].is_4wd_only is True
    assert osm_by_id["way/1002"].is_ford is True
    assert osm_by_id["way/1002"].surface_class == "rough_rock_mud_sand"
    assert osm_by_id["way/1004"].trail_class == "fire_trail"
    assert osm_by_id["way/1005"].access_status == "private"
    assert osm_by_id["way/1005"].has_gate_barrier is True
    assert osm_by_id["way/1006"].is_bridged is True
    assert osm_by_id["way/1007"].is_vehicular is False
    assert osm_by_id["way/1007"].trail_class == "walking_track"

    # v2 jittered fixture loads 8 ways (way/1004 split into 100401 + 100402; way/1001 asphalt)
    ds_v2 = load_corridor_fixtures(fixtures_dir, osm_filename="osm_tracks_v2_jittered.geojson")
    assert len(ds_v2.osm_tracks) == 8
    v2_by_id = {t.source_id: t for t in ds_v2.osm_tracks}
    assert v2_by_id["way/1001"].surface_class == "sealed"
    assert "way/100401" in v2_by_id and "way/100402" in v2_by_id

    # POI dedupe collapses the 35 m duplicate OSM Davies Plain Hut into the VIC state record
    deduped = dedupe_poi_features(ds_v1.pois)
    assert len(deduped) == 5
    huts = [p for p in deduped if p.poi_type == "hut"]
    assert len(huts) == 1
    assert huts[0].source_provider == "vic_recweb_hut"
    assert huts[0].source_license == "CC-BY-4.0"


def test_coverage_bounds_and_spatial_tiling() -> None:
    assert is_in_vic_coverage(CORRIDOR_ALPS_BBOX)
    assert is_in_nsw_coverage(CORRIDOR_ALPS_BBOX)
    assert is_in_australia_coverage(CORRIDOR_ALPS_BBOX)

    outside = BBox(min_x=10.0, min_y=45.0, max_x=11.0, max_y=46.0)
    assert not is_in_vic_coverage(outside)
    assert not is_in_nsw_coverage(outside)
    assert not is_in_australia_coverage(outside)

    tiles = iter_spatial_tiles(
        BBox(min_x=147.0, min_y=-37.5, max_x=149.5, max_y=-35.0), max_deg=1.0
    )
    assert len(tiles) == 9  # 3 x 3 tiles


def test_vic_wfs_connector_mocked_offline(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        type_name = request.url.params.get("typeNames", "")
        calls.append(type_name)
        if type_name == "open-data-platform:tr_road":
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "pfi": "V1",
                                "ezi_road_name_label": "BUCKY FIRE TRAIL",
                                "class_code": 6,
                                "road_seal": "2",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[148.10, -36.60], [148.11, -36.61]],
                            },
                        }
                    ],
                },
            )
        if type_name == "open-data-platform:recweb_hut":
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"pfi": "H1", "name": "Davies Plain Hut", "elev_m": 1615},
                            "geometry": {"type": "Point", "coordinates": [148.126, -36.662]},
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        conn = VICTrailsConnector(cache_dir=tmp_path, client=client)
        tracks = conn.fetch_tracks(CORRIDOR_ALPS_BBOX)
        pois = conn.fetch_pois(CORRIDOR_ALPS_BBOX)
        # Second call hits disk cache (no additional network requests)
        prev_calls = len(calls)
        tracks_cached = conn.fetch_tracks(CORRIDOR_ALPS_BBOX)

    assert len(tracks) == 1
    assert tracks[0].source == "vic"
    assert tracks[0].trail_class == "fire_trail"
    assert tracks[0].geometry["type"] == "MultiLineString"
    assert len(tracks_cached) == 1
    assert len(calls) == prev_calls  # served from disk cache
    assert len(pois) == 1
    assert pois[0].poi_type == "hut" and pois[0].elev_m == 1615.0


def test_nsw_arcgis_connector_pagination_and_esri_json_fallback(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        fmt = request.url.params.get("f")
        offset = int(request.url.params.get("resultOffset", "0"))

        if path.endswith("/9/query"):
            # Force fallback from f=geojson to f=json (Esri JSON format) + pagination
            if fmt == "geojson":
                return httpx.Response(200, json={"error": {"code": 400, "message": "Use json"}})
            if offset == 0:
                return httpx.Response(
                    200,
                    json={
                        "exceededTransferLimit": True,
                        "features": [
                            {
                                "attributes": {
                                    "OBJECTID": 10,
                                    "brimsid": "FT-10",
                                    "surface": "2",
                                    "operationalstatus": "O",
                                },
                                "geometry": {"paths": [[[148.14, -36.55], [148.15, -36.555]]]},
                            }
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "exceededTransferLimit": False,
                    "features": [
                        {
                            "attributes": {
                                "OBJECTID": 11,
                                "roadnamebase": "PINCH RIVER",
                                "roadnametype": "FIRE TRAIL",
                                "surface": "2",
                            },
                            "geometry": {"paths": [[[148.16, -36.555], [148.17, -36.558]]]},
                        }
                    ],
                },
            )
        if path.endswith("/5/query"):
            out_fields = (request.url.params.get("outFields") or "").upper()
            assert "OBJECTID" in out_fields
            if fmt == "geojson":
                return httpx.Response(200, json={"error": {"code": 400, "message": "Use json"}})
            return httpx.Response(
                200,
                json={
                    "features": [
                        {
                            "attributes": {
                                "OBJECTID": 21,
                                "classsubtype": 6,
                                "roadnamebase": "MAIN RANGE",
                                "roadnametype": "WALK",
                            },
                            "geometry": {"paths": [[[148.12, -36.55], [148.13, -36.552]]]},
                        }
                    ]
                },
            )
        if "SurveyMarkGDA2020" in path:
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "marknumber": "4812",
                                "trigname": "KOSCIUSZKO TRIG",
                                "ahdheight_label": "2228.1 m",
                            },
                            "geometry": {"type": "Point", "coordinates": [148.15, -36.55]},
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        conn = NSWTrailsConnector(cache_dir=tmp_path, client=client)
        tracks = conn.fetch_tracks(CORRIDOR_ALPS_BBOX)
        pois = conn.fetch_pois(CORRIDOR_ALPS_BBOX)

    assert len(tracks) == 3
    by_id = {t.source_id: t for t in tracks}
    assert by_id["10"].name == "Fire Trail FT-10"
    assert by_id["10"].trail_class == "fire_trail"
    assert by_id["11"].name == "PINCH RIVER FIRE TRAIL"
    assert by_id["21"].trail_class == "walking_track" and by_id["21"].is_vehicular is False
    assert len(pois) == 1
    assert pois[0].poi_type == "trig" and pois[0].elev_m == 2228.1


def test_hydro_capad_and_osm_overpass_connectors_mocked(tmp_path: Path) -> None:
    def hydro_capad_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        out_fields = (request.url.params.get("outFields") or "").upper()
        if "Surface_Hydrology/MapServer/3" in url_str:
            assert request.url.params.get("where") == "featuretype <> 'Connector'"
            assert "OBJECTID" in out_fields
            if request.url.params.get("f") == "geojson":
                return httpx.Response(200, json={"error": {"code": 400, "message": "Use json"}})
            return httpx.Response(
                200,
                json={
                    "features": [
                        {
                            "attributes": {
                                "OBJECTID": 101,
                                "name": "Murray River",
                                "featuretype": "Watercourse",
                                "perenniality": "Perennial",
                                "hierarchy": "Major",
                            },
                            "geometry": {"paths": [[[148.05, -36.565], [148.20, -36.565]]]},
                        },
                        {
                            "attributes": {"OBJECTID": 102, "featuretype": "Connector"},
                            "geometry": {"paths": [[[148.10, -36.565], [148.11, -36.565]]]},
                        },
                    ]
                },
            )
        if "Surface_Hydrology/MapServer/5" in url_str:
            assert "OBJECTID" in out_fields
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 501,
                                "name": "Blue Lake",
                                "featuretype": "Lake",
                                "perenniality": "Perennial",
                            },
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [
                                        [148.10, -36.60],
                                        [148.11, -36.60],
                                        [148.11, -36.61],
                                        [148.10, -36.61],
                                        [148.10, -36.60],
                                    ]
                                ],
                            },
                        }
                    ],
                },
            )
        if "Surface_Hydrology/MapServer/1" in url_str:
            assert "OBJECTID" in out_fields
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"OBJECTID": 301, "name": "Alpine Spring"},
                            "geometry": {"type": "Point", "coordinates": [148.12, -36.62]},
                        }
                    ],
                },
            )
        if "CAPAD/FeatureServer/0" in url_str:
            assert "OBJECTID" in out_fields
            assert request.url.params.get("maxAllowableOffset") == "0.00005"
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "PA_ID": "NP1",
                                "NAME": "Alpine National Park",
                                "TYPE": "National Park",
                                "TYPE_ABBR": "NP",
                                "IUCN": "II",
                                "STATE": "VIC",
                            },
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [
                                        [148.05, -36.68],
                                        [148.20, -36.68],
                                        [148.20, -36.565],
                                        [148.05, -36.565],
                                        [148.05, -36.68],
                                    ]
                                ],
                            },
                        }
                    ],
                },
            )
        if "overpass" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "elements": [
                        {"type": "node", "id": 500, "tags": {"barrier": "gate"}},
                        {
                            "type": "way",
                            "id": 9001,
                            "version": 3,
                            "nodes": [499, 500, 501],
                            "tags": {
                                "highway": "track",
                                "name": "Test Ridge Track",
                                "tracktype": "grade4",
                                "4wd_only": "yes",
                            },
                            "geometry": [
                                {"lon": 148.11, "lat": -36.60},
                                {"lon": 148.12, "lat": -36.61},
                            ],
                        },
                    ]
                },
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(hydro_capad_handler)) as client:
        hydro = GAHydroConnector(cache_dir=tmp_path, client=client)
        waterways = hydro.fetch_waterways(CORRIDOR_ALPS_BBOX, include_polygons=True)
        water_pts = hydro.fetch_water_points(CORRIDOR_ALPS_BBOX)
        parks = CAPADConnector(cache_dir=tmp_path, client=client).fetch_parks(CORRIDOR_ALPS_BBOX)
        osm_tracks = OSMConnector(client=client).fetch_overpass_tracks(CORRIDOR_ALPS_BBOX)

    assert len(waterways) == 2
    assert {w.source_feature_id for w in waterways} == {"101", "501"}
    assert waterways[0].name == "Murray River"
    assert len(water_pts) == 1 and water_pts[0].source_feature_id == "301"
    assert len(parks) == 1
    assert parks[0].geometry["type"] == "MultiPolygon"  # Polygon promoted to MultiPolygon
    assert len(osm_tracks) == 1
    assert osm_tracks[0].has_gate_barrier is True
    assert osm_tracks[0].is_4wd_only is True

    # Deterministic fallback IDs when upstream omits OBJECTID (never memory address id(feat))
    from trailforge.ingestion.connectors.hydro import parse_waterway_feature

    f_a = {
        "type": "Feature",
        "properties": {"name": "Unnamed Creek", "featuretype": "Watercourse"},
        "geometry": {"type": "LineString", "coordinates": [[148.1, -36.6], [148.11, -36.61]]},
    }
    f_b = json.loads(json.dumps(f_a))
    p_a = parse_waterway_feature(f_a)
    p_b = parse_waterway_feature(f_b)
    assert p_a is not None and p_b is not None
    assert p_a.source_feature_id == p_b.source_feature_id
    assert p_a.source_feature_id.startswith("hydro-")


def test_lidar_coastal_water_vs_missing_data_cliff_guard(
    fixtures_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Complete alpine DEM fixture -> accepted cleanly
    alps_stats = inspect_raster_coverage(fixtures_dir / "dem_alps_corridor.tif")
    assert alps_stats.valid_frac == 1.0
    assert alps_stats.exact_zero_frac == 0.0
    assert alps_stats.accept_5m_lidar is True

    # 2. Coastal / estuary clip: 30% exact 0.0 m water gently sloping up 1..40 m -> ACCEPTED
    coastal = np.zeros((40, 40), dtype=np.float32)
    for c in range(12, 40):
        coastal[:, c] = (c - 11) * 1.5  # shoreline pixel adjacent to 0.0 m is 1.5 m (< 15 m cliff)
    coastal_stats = evaluate_raster_coverage_array(coastal)
    assert coastal_stats.exact_zero_frac == pytest.approx(0.30, abs=0.02)
    assert coastal_stats.zero_cliff_ratio == 0.0
    assert coastal_stats.accept_5m_lidar is True

    # 3. Inland missing-LiDAR mask: 25% exact 0.0 m directly abutting 650 m ridge -> REJECTED
    missing_lidar = np.full((40, 40), 650.0, dtype=np.float32)
    missing_lidar[:, :10] = 0.0
    missing_stats = evaluate_raster_coverage_array(missing_lidar)
    assert missing_stats.exact_zero_frac == pytest.approx(0.25, abs=0.02)
    assert missing_stats.zero_cliff_ratio == 1.0
    assert missing_stats.accept_5m_lidar is False

    # 4. End-to-end AUElvisDEMConnector fallback: missing-mask 5m LiDAR -> falls back to SRTM
    missing_tif = _write_uncompressed_geotiff(tmp_path / "missing_5m.tif", missing_lidar)
    srtm_arr = np.linspace(600.0, 1400.0, 40 * 40, dtype=np.float32).reshape(40, 40)
    srtm_tif = _write_uncompressed_geotiff(tmp_path / "srtm_backup.tif", srtm_arr)

    def dem_handler(request: httpx.Request) -> httpx.Response:
        if "DEM_LiDAR_5m_2025" in str(request.url):
            return httpx.Response(
                200,
                content=missing_tif.read_bytes(),
                headers={"content-type": "image/tiff"},
            )
        if "DEM_SRTM_1Second" in str(request.url):
            return httpx.Response(
                200,
                content=srtm_tif.read_bytes(),
                headers={"content-type": "image/tiff"},
            )
        return httpx.Response(404)

    monkeypatch.setattr("trailforge.ingestion.connectors.dem.shutil.which", lambda _name: None)
    with httpx.Client(transport=httpx.MockTransport(dem_handler)) as client:
        conn = AUElvisDEMConnector(cache_dir=tmp_path / "cache", client=client)
        out_dem, resolved_tier = conn.fetch_dem(CORRIDOR_ALPS_BBOX, tmp_path / "work")
        assert resolved_tier == "srtm-1s"
        assert out_dem.is_file()
        assert inspect_raster_coverage(out_dem).accept_5m_lidar is True

    # Offline local_dem override returns synthetic tier immediately
    local_out, tier = AUElvisDEMConnector().fetch_dem(
        CORRIDOR_ALPS_BBOX,
        tmp_path / "local",
        local_dem=fixtures_dir / "dem_alps_corridor.tif",
    )
    assert local_out.is_file() and tier == "synthetic"


def test_poi_dedupe_keeps_distinct_nearby_types() -> None:
    p1 = RawPoiFeature(
        source_provider="vic_recweb_hut",
        source_license="CC-BY-4.0",
        source_feature_id="1",
        name="Davies Plain",
        poi_type="hut",
        state="VIC",
        elev_m=1615.0,
        geometry={"type": "Point", "coordinates": [148.126, -36.662]},
    )
    p2 = RawPoiFeature(
        source_provider="osm",
        source_license="ODbL",
        source_feature_id="2",
        name="Davies Plain",
        poi_type="campground",
        state="VIC",
        geometry={"type": "Point", "coordinates": [148.126, -36.662]},
    )
    # Different poi_type at same coordinates must both be preserved
    assert len(dedupe_poi_features([p1, p2])) == 2


@pytest.mark.live
def test_live_au_connectors_smoke() -> None:
    """Optional live smoke test against CAPAD, DataVic WFS, NSW Spatial & GA Hydrology."""
    parks = CAPADConnector().fetch_parks(CORRIDOR_ALPS_BBOX)
    assert any("Alpine" in p.name or "Kosciuszko" in p.name for p in parks)
    vic_tracks = VICTrailsConnector().fetch_tracks(CORRIDOR_ALPS_BBOX)
    assert isinstance(vic_tracks, list) and len(vic_tracks) > 0
    nsw_tracks = NSWTrailsConnector().fetch_tracks(CORRIDOR_ALPS_BBOX)
    assert isinstance(nsw_tracks, list) and len(nsw_tracks) > 0
    waterways = GAHydroConnector().fetch_waterways(CORRIDOR_ALPS_BBOX)
    assert isinstance(waterways, list) and len(waterways) > 0
    assert json.dumps([p.name for p in parks])
