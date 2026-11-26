"""Offline unit, PostGIS integration, and optional @pytest.mark.live tests for Pan-AU connectors."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from trailforge.db.models import (
    POI,
    CAPADPark,
    SourceTrackAUState,
    SourceTrackNSW,
    SourceTrackVIC,
    Track,
    TrackSegment,
)
from trailforge.ingestion.conflate import conflate_tracks
from trailforge.ingestion.connectors import (
    ATTRIBUTION_ACT_ACTMAPI,
    ATTRIBUTION_NSW_SPATIAL,
    ATTRIBUTION_NT_NTLIS,
    ATTRIBUTION_QLD_QSPATIAL,
    ATTRIBUTION_SA_DEW_DHUD,
    ATTRIBUTION_TAS_LIST,
    ATTRIBUTION_VIC_DEECA,
    ATTRIBUTION_WA_DBCA_SLIP,
    ACTTrailsConnector,
    AutoAUStateTrailsRouter,
    BBox,
    CorridorDataset,
    NTTrailsConnector,
    QLDTrailsConnector,
    RawCapadFeature,
    RawPoiFeature,
    RawTrackFeature,
    SATrailsConnector,
    TASTrailsConnector,
    WATrailsConnector,
    is_wa_non_commercial_restricted,
    resolve_intersecting_states,
)
from trailforge.ingestion.live_bake import fetch_live_tile_dataset
from trailforge.ingestion.pipeline import ingest_corridor_dataset
from trailforge.packs.catalog import (
    PACK_REGION_IDS,
    PACK_REGIONS,
    get_pack_region,
    list_pack_regions,
)

# Compact bboxes representing each Australian jurisdiction
ACT_NAMADGI_BBOX = BBox(min_x=148.85, min_y=-35.80, max_x=149.10, max_y=-35.55)
ACT_NSW_BORDER_BBOX = BBox(min_x=148.65, min_y=-35.80, max_x=149.10, max_y=-35.55)
VIC_NSW_BORDER_BBOX = BBox(min_x=148.05, min_y=-36.68, max_x=148.20, max_y=-36.55)
TAS_CRADLE_BBOX = BBox(min_x=145.80, min_y=-41.80, max_x=146.10, max_y=-41.55)
QLD_CAPE_YORK_BBOX = BBox(min_x=145.20, min_y=-16.40, max_x=145.60, max_y=-16.05)
WA_PERTH_HILLS_BBOX = BBox(min_x=116.05, min_y=-32.10, max_x=116.30, max_y=-31.85)
SA_FLINDERS_BBOX = BBox(min_x=138.50, min_y=-31.65, max_x=138.80, max_y=-31.35)
NT_MACDONNELL_BBOX = BBox(min_x=132.40, min_y=-23.90, max_x=132.80, max_y=-23.55)
SA_NT_BORDER_BBOX = BBox(min_x=131.00, min_y=-26.30, max_x=132.00, max_y=-25.70)


def _fc(features: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features}


# -----------------------------------------------------------------------------
# 1. Individual State Connectors (ACT, TAS, QLD, WA, SA, NT) — 100% Offline
# -----------------------------------------------------------------------------


def test_act_trails_connector_mocked_offline(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "ACTGOV_TRANSPORT_TRACK" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 101,
                                "ROAD_NAME": "Bimberi Fire Trail",
                                "DISTRICT_NAME": "Namadgi",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[148.90, -35.65], [148.92, -35.66]],
                            },
                        }
                    ]
                ),
            )
        if "ACT_Equestrian_Routes" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 201,
                                "BICENTENNIAL_NATIONAL_TRAIL": "YES",
                                "ROUTE_STATUS": "OPEN",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[148.91, -35.64], [148.93, -35.65]],
                            },
                        }
                    ]
                ),
            )
        if "ACTGOV_ROAD_CENTRELINES" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 301,
                                "ROAD_NAME": "Boboyan Management Trail",
                                "HIERARCHY": "RURAL SPECIAL PURPOSE",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[148.95, -35.70], [148.96, -35.72]],
                            },
                        }
                    ]
                ),
            )
        if "ACTGOV_Heritage_Register" in url:
            # Polygon hut -> must be converted to a valid WGS84 Point centroid!
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 401,
                                "SITE_NAME": "Brayshaws Hut",
                                "DataRestrictions": "No",
                            },
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [
                                        [148.950, -35.710],
                                        [148.951, -35.710],
                                        [148.951, -35.711],
                                        [148.950, -35.711],
                                        [148.950, -35.710],
                                    ]
                                ],
                            },
                        }
                    ]
                ),
            )
        if "ACTGOV_Barbecue_Assets" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 501,
                                "ASSET_NAME": "BBQ 1",
                                "LOCATION": "Namadgi National Park, Orroral Campground 1",
                            },
                            "geometry": {"type": "Point", "coordinates": [148.97, -35.67]},
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 502,
                                "ASSET_NAME": "BBQ 2",
                                "LOCATION": "Namadgi National Park, Orroral Campground 2",
                            },
                            "geometry": {"type": "Point", "coordinates": [148.9701, -35.6701]},
                        },
                    ]
                ),
            )
        if "ACTGOV_SURVEY_CONTROL_MARK" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 601,
                                "MARK_NAME_FULL": "Mount Bimberi Trig",
                                "ALIAS": "TS BIMBERI",
                                "REDUCED_LEVEL": 1912.4,
                            },
                            "geometry": {"type": "Point", "coordinates": [148.79, -35.66]},
                        }
                    ]
                ),
            )
        return httpx.Response(200, json=_fc([]))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        conn = ACTTrailsConnector(cache_dir=tmp_path, client=client)
        tracks = conn.fetch_tracks(ACT_NAMADGI_BBOX)
        pois = conn.fetch_pois(ACT_NAMADGI_BBOX)
        prev_calls = len(calls)
        tracks_cached = conn.fetch_tracks(ACT_NAMADGI_BBOX)
        # Outside ACT returns empty with zero HTTP calls
        assert conn.fetch_tracks(WA_PERTH_HILLS_BBOX) == []

    assert len(tracks) == 3
    assert len(tracks_cached) == 3
    assert len(calls) == prev_calls  # cached!
    by_layer = {t.source_layer: t for t in tracks}
    assert by_layer["act_transport_track"].source == "act"
    assert by_layer["act_transport_track"].trail_class == "fire_trail"
    assert by_layer["act_equestrian_routes"].trail_class == "walking_track"
    assert by_layer["act_equestrian_routes"].is_vehicular is False
    assert by_layer["act_rural_roads"].trail_class == "management_trail"

    poi_by_type = {p.poi_type: p for p in pois}
    assert poi_by_type["hut"].name == "Brayshaws Hut"
    assert poi_by_type["hut"].geometry["type"] == "Point"
    assert poi_by_type["campground"].name == "Orroral Campground"
    assert poi_by_type["trig"].elev_m == pytest.approx(1912.4)
    assert "ACTmapi" in conn.attribution and "CC BY 4.0" in conn.attribution


def test_tas_trails_connector_cc_by_3_au_mocked_offline(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/OpenDataWFS/MapServer/42/query" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 10,
                                "TRANS_TYPE": "Road",
                                "TSEG_FEAT": "Fire Trail",
                                "TRAN_CLASS": "Vehicular Track",
                                "SURFACE_TY": "Unsealed",
                                "PRI_NAME": "Murchison Fire Trail",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[145.90, -41.65], [145.92, -41.66]],
                            },
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 11,
                                "TRANS_TYPE": "Road",
                                "TSEG_FEAT": "Track",
                                "TRAN_CLASS": "Vehicular Track",
                                "SURFACE_TY": "4WD required",
                                "PRI_NAME": "Climies Track",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[145.85, -41.70], [145.88, -41.72]],
                            },
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 12,
                                "TRANS_TYPE": "Track",
                                "TRAN_CLASS": "AS2156 Class 4",
                                "SURFACE_TY": "Boardwalk",
                                "PRI_NAME": "Overland Track",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[145.95, -41.66], [145.96, -41.68]],
                            },
                        },
                    ]
                ),
            )
        if "/OpenDataWFS/MapServer/12/query" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 20,
                                "BUILD_TY": "Hut",
                                "BLD_PUR": "Walkers",
                                "BUILD_NAME": "Waterfall Valley Hut",
                            },
                            "geometry": {"type": "Point", "coordinates": [145.95, -41.71]},
                        }
                    ]
                ),
            )
        if "/OpenDataWFS/MapServer/40/query" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 30,
                                "PACK_ID": "ST042",
                                "SCS_NAME": "Cradle Mountain Trig",
                                "HEIGHT": 1545.2,
                            },
                            "geometry": {"type": "Point", "coordinates": [145.951, -41.685]},
                        }
                    ]
                ),
            )
        return httpx.Response(200, json=_fc([]))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        conn = TASTrailsConnector(cache_dir=tmp_path, client=client)
        tracks = conn.fetch_tracks(TAS_CRADLE_BBOX)
        pois = conn.fetch_pois(TAS_CRADLE_BBOX)

    assert len(tracks) == 3
    by_id = {t.source_id: t for t in tracks}
    assert by_id["10"].source == "tas" and by_id["10"].trail_class == "fire_trail"
    assert by_id["11"].trail_class == "4wd_track" and by_id["11"].is_4wd_only is True
    assert by_id["12"].trail_class == "walking_track" and by_id["12"].is_vehicular is False

    assert len(pois) == 2
    assert all(p.source_license == "CC-BY-3.0-AU" for p in pois)
    assert all(p.state == "TAS" for p in pois)
    trig = next(p for p in pois if p.poi_type == "trig")
    assert trig.elev_m == pytest.approx(1545.2)
    assert "CC BY 3.0 AU" in conn.attribution


def test_qld_trails_connector_qpws_dedupe_and_state_tracks_offline(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/ParksTerrestrialProtectedAreas/MapServer/5/query" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "objectid": 77,
                                "name": "Daintree Ridge Trail",
                                "subcat": "Tracks and Trails",
                                "traffic": "Walking",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[145.35, -16.20], [145.38, -16.22]],
                            },
                        },
                        # Bollard / access structure must be filtered out
                        {
                            "type": "Feature",
                            "properties": {
                                "objectid": 78,
                                "name": "Gate Bollard",
                                "subcat": "Access Structures",
                                "assettype": "Bollard",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[145.35, -16.20], [145.351, -16.201]],
                            },
                        },
                    ]
                ),
            )
        if "/ParksTerrestrialProtectedAreas/MapServer/2/query" in url:
            # Same objectid=77 in layer /2 marked as fireline -> upgrades to fire_trail!
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "objectid": 77,
                                "name": "Daintree Ridge Fireline",
                                "subcat": "Roads",
                                "fireline": "Primary Fireline",
                                "traffic": "4WD",
                                "material": "Gravel",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[145.35, -16.20], [145.38, -16.22]],
                            },
                        }
                    ]
                ),
            )
        if "/RoadsAndTracks/MapServer/10/query" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "objectid": 88,
                                "road_name_full": "CREB Track",
                                "sub_class": "4WD Track",
                                "trafficability": "4WD only",
                                "surface_type": "Unsealed",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[145.30, -16.15], [145.32, -16.18]],
                            },
                        }
                    ]
                ),
            )
        if "/PhysicalInfrastructure/MapServer/60/query" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "objectid": 91,
                                "feature_type": "Lookout",
                                "name": "Alexandra Lookout",
                            },
                            "geometry": {"type": "Point", "coordinates": [145.41, -16.24]},
                        }
                    ]
                ),
            )
        if "/FoundationData/MapServer/8/query" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "objectid": 92,
                                "mrk_id": "14822",
                                "alt1_nm": "Thornton Peak Trig",
                                "ahdheight": 1374.0,
                            },
                            "geometry": {"type": "Point", "coordinates": [145.37, -16.16]},
                        }
                    ]
                ),
            )
        return httpx.Response(200, json=_fc([]))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        conn = QLDTrailsConnector(cache_dir=tmp_path, client=client)
        tracks = conn.fetch_tracks(QLD_CAPE_YORK_BBOX)
        pois = conn.fetch_pois(QLD_CAPE_YORK_BBOX)

    assert len(tracks) == 2  # oid 77 deduped + bollard 78 excluded + CREB 88 kept
    by_id = {t.source_id: t for t in tracks}
    assert by_id["77"].trail_class == "fire_trail" and by_id["77"].is_vehicular is True
    assert by_id["88"].name == "CREB Track" and by_id["88"].is_4wd_only is True
    assert {p.poi_type for p in pois} == {"lookout", "trig"}


def test_wa_trails_connector_strict_cc_by_nc_exclusion_guard(tmp_path: Path) -> None:
    """Verify WATrailsConnector strictly excludes CC BY-NC / DBCA-027 records and endpoints."""
    assert is_wa_non_commercial_restricted({"licence": "CC BY-NC 4.0"}) is True
    assert is_wa_non_commercial_restricted({"dataset_id": "DBCA-027"}) is True
    assert is_wa_non_commercial_restricted(source_url="https://example.test/DBCA-027/query") is True
    assert is_wa_non_commercial_restricted({"licence": "CC BY 4.0", "name": "Powerlines"}) is False

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "Tracks_and_Trails_hosted" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 1,
                                "TYPE": "Mundaring Fire Break",
                                "licence": "CC BY 4.0",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[116.15, -31.95], [116.17, -31.96]],
                            },
                        },
                        # MUST BE STRICTLY EXCLUDED (CC BY-NC!)
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 2,
                                "TYPE": "Restricted Non-Commercial Trail",
                                "licence": "Creative Commons CC BY-NC 4.0",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[116.16, -31.95], [116.18, -31.96]],
                            },
                        },
                        # MUST BE STRICTLY EXCLUDED (DBCA-027 dataset tag!)
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 3,
                                "TYPE": "DBCA-027 Trail",
                                "dataset_id": "DBCA-027",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[116.17, -31.95], [116.19, -31.96]],
                            },
                        },
                    ]
                ),
            )
        if "/Transport/MapServer/20/query" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 10,
                                "road_name": "Mundaring Powerlines Track",
                                "mapclassification": "Track",
                                "roadsurface": "gravel",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[116.18, -31.92], [116.22, -31.94]],
                            },
                        }
                    ]
                ),
            )
        if "RATIS_Sites" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 20,
                                "RTS_NAME": "Perth Hills Campground",
                                "RTS_PRIMARY_FUNCTION": "Camping",
                                "RTS_STATUS": "OPEN",
                            },
                            "geometry": {"type": "Point", "coordinates": [116.16, -31.96]},
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 21,
                                "RTS_NAME": "Blocked NC Site",
                                "RTS_PRIMARY_FUNCTION": "Camping",
                                "RTS_STATUS": "OPEN",
                                "rights": "CC-BY-NC",
                            },
                            "geometry": {"type": "Point", "coordinates": [116.17, -31.97]},
                        },
                    ]
                ),
            )
        if "/Environment/MapServer/56/query" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 30,
                                "ftw_name": "Mount Dale Fire Tower",
                                "ftw_status": "Active",
                            },
                            "geometry": {"type": "Point", "coordinates": [116.29, -32.12]},
                        }
                    ]
                ),
            )
        if "/Property_and_Planning/MapServer/1/query" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 40,
                                "geodetic_point_name": "MT DALE S0",
                                "reduced_level": 546.8,
                                "latest_status_description": "GOOD",
                            },
                            "geometry": {"type": "Point", "coordinates": [116.28, -32.11]},
                        }
                    ]
                ),
            )
        return httpx.Response(200, json=_fc([]))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        conn = WATrailsConnector(cache_dir=tmp_path, client=client)
        tracks = conn.fetch_tracks(WA_PERTH_HILLS_BBOX)
        pois = conn.fetch_pois(WA_PERTH_HILLS_BBOX)

        # Also test URL-level DBCA-027 refusal
        nc_conn = WATrailsConnector(
            tracks_url="https://example.test/DBCA-027/FeatureServer/0/query",
            slip_roads_url="https://example.test/DBCA_027/MapServer/0/query",
            client=client,
        )
        assert nc_conn.fetch_tracks(WA_PERTH_HILLS_BBOX) == []
        assert nc_conn.excluded_nc_count >= 2

    assert len(tracks) == 2
    assert {t.name for t in tracks} == {"Mundaring Fire Break", "Mundaring Powerlines Track"}
    assert conn.excluded_nc_count == 3  # 2 tracks + 1 RATIS site
    assert {p.poi_type for p in pois} == {"campground", "lookout", "trig"}
    assert all("Blocked NC" not in (p.name or "") for p in pois)


def test_sa_trails_connector_vsizip_streaming_and_survey_filter_offline(tmp_path: Path) -> None:
    # Build synthetic SA Recreation Trails ZIP in memory
    sa_zip_buf = io.BytesIO()
    with zipfile.ZipFile(sa_zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "TOPO_RecreationTrails_GDA2020.geojson",
            json.dumps(
                _fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 1,
                                "TRAILNAME": "Heysen Trail",
                                "TRAILTYPE": "Walking",
                                "TRAILSTATU": "Open",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[138.60, -31.50], [138.62, -31.52]],
                            },
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 2,
                                "TRAILNAME": "Skytrek 4WD Loop",
                                "TRAILTYPE": "4WD Vehicle",
                                "TRAILSTATU": "Open",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[138.65, -31.48], [138.67, -31.50]],
                            },
                        },
                        # Closed trail must be filtered out
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 3,
                                "TRAILNAME": "Retired Track",
                                "TRAILTYPE": "4WD",
                                "TRAILSTATU": "Closed",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[138.61, -31.50], [138.63, -31.51]],
                            },
                        },
                        # Out-of-bbox feature must be filtered out spatially
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 4,
                                "TRAILNAME": "Adelaide Hills Trail",
                                "TRAILTYPE": "4WD",
                                "TRAILSTATU": "Open",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[138.70, -34.95], [138.72, -34.96]],
                            },
                        },
                    ]
                )
            ),
        )
    sa_zip_bytes = sa_zip_buf.getvalue()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "TOPO_RecreationTrails_geojson.zip" in url:
            return httpx.Response(200, content=sa_zip_bytes)
        if "Fire_Management_Fire_Access_Tracks" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 10,
                                "TRACK_NAME": "Wilpena Fire Access Track",
                                "PROP_FA_CLASS": "Primary",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[138.60, -31.53], [138.63, -31.54]],
                            },
                        }
                    ]
                ),
            )
        if "Topographic_Base_Map/MapServer/434" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 20,
                                "name": "Brachina Gorge Track",
                                "class": "Vehicular Track",
                                "formation": "unsealed",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[138.58, -31.34], [138.62, -31.35]],
                            },
                        }
                    ]
                ),
            )
        if "parks-data.json" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "p1",
                        "name": "Ikara-Flinders Ranges National Park",
                        "lat": -31.52,
                        "lng": 138.60,
                        "campingAndAccommodation": {"tentCamping": True},
                    }
                ],
            )
        if "MAD_WMS_PRD/MapServer/0" in url:
            assert request.url.params.get("where") == "1=1"
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 30,
                                "MARK_NO": "6635-101",
                                "MARKTYPE": "TS",
                                "H_ORDER": 1,
                                "V_DATUM": "AHD",
                                "HEIGHT": 1171.0,
                                "GONE": "N",
                            },
                            "geometry": {"type": "Point", "coordinates": [138.56, -31.55]},
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 31,
                                "MARK_NO": "6635-999",
                                "MARKTYPE": "TS",
                                "V_DATUM": "AHD",
                                "HEIGHT": 900.0,
                                "GONE": "Y",
                            },
                            "geometry": {"type": "Point", "coordinates": [138.57, -31.56]},
                        },
                    ]
                ),
            )
        return httpx.Response(200, json=_fc([]))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        conn = SATrailsConnector(cache_dir=tmp_path, client=client)
        tracks = conn.fetch_tracks(SA_FLINDERS_BBOX)
        pois = conn.fetch_pois(SA_FLINDERS_BBOX)

    names = {t.name for t in tracks}
    assert names == {
        "Heysen Trail",
        "Skytrek 4WD Loop",
        "Wilpena Fire Access Track",
        "Brachina Gorge Track",
    }
    assert "Retired Track" not in names
    assert "Adelaide Hills Trail" not in names
    assert {p.poi_type for p in pois} == {"campground", "trig"}
    trig = next(p for p in pois if p.poi_type == "trig")
    assert trig.name == "SM 6635-101" and trig.elev_m == pytest.approx(1171.0)


def test_nt_trails_connector_wfs_crs84_and_unformed_roads_offline(tmp_path: Path) -> None:
    nt_roads_queries: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "ROAD_CENTRELINES" in url:
            params = dict(request.url.params)
            nt_roads_queries.append(params)
            out_fields = params.get("outFields", "")
            # NTG ROAD_CENTRELINES OID field is UFI (not OBJECTID, which returns ArcGIS 400)
            assert "UFI" in out_fields
            assert "OBJECTID" not in out_fields
            assert params.get("inSR") == "4326"
            assert params.get("outSR") == "4326"
            assert params.get("spatialRel") == "esriSpatialRelIntersects"
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "UFI": 22350,
                                "ROAD_LABEL": "*Finke River 4WD Track",
                                "ROAD_NAME": "*FINKE RIVER",
                                "ROAD_TYPE": "TRACK",
                                "SURFACE_TYPE": "4WD",
                                "CATEGORY": "TRACK",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[132.60, -23.75], [132.65, -23.78]],
                            },
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "UFI": 22351,
                                "ROAD_LABEL": "*REDBANK GORGE ROAD",
                                "ROAD_NAME": "*REDBANK GORGE",
                                "ROAD_TYPE": "ROAD",
                                "SURFACE_TYPE": "FORMED",
                                "CATEGORY": "PASTORAL - LOCAL",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[132.52, -23.59], [132.54, -23.61]],
                            },
                        },
                        {
                            "type": "Feature",
                            "properties": {
                                "UFI": 22352,
                                "ROAD_LABEL": "Stuart Highway",
                                "SURFACE_TYPE": "SEALED",
                                "CATEGORY": "HIGHWAY",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[132.50, -23.70], [132.55, -23.72]],
                            },
                        },
                    ]
                ),
            )
        if "Topographic_Base_Map/MapServer/434" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 10,
                                "name": "Larapinta Trail",
                                "class": "Foot Track",
                                "formation": "natural",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[132.55, -23.70], [132.58, -23.71]],
                            },
                        }
                    ]
                ),
            )
        if "ogc.ntlis.nt.gov.au" in url:
            assert "urn:ogc:def:crs:OGC:1.3:CRS84" in (request.url.params.get("bbox") or "")
            tname = request.url.params.get("typeNames")
            if tname == "ntlis:PLACENAMES":
                return httpx.Response(
                    200,
                    json=_fc(
                        [
                            {
                                "type": "Feature",
                                "properties": {
                                    "OBJECTID": 50,
                                    "NAME": "Ormiston Gorge",
                                    "TYPE_DESIGNATION": "GORGE",
                                },
                                "geometry": {"type": "Point", "coordinates": [132.72, -23.63]},
                            }
                        ]
                    ),
                )
            if tname == "survey:SURVEY_MARKS":
                return httpx.Response(
                    200,
                    json=_fc(
                        [
                            {
                                "type": "Feature",
                                "properties": {
                                    "OBJECTID": 60,
                                    "PRIMARYNAME": "MT SONDER CM",
                                    "MARKTYPE": "CM",
                                    "ADJ_RL": 1380.4,
                                },
                                "geometry": {"type": "Point", "coordinates": [132.59, -23.57]},
                            }
                        ]
                    ),
                )
        return httpx.Response(200, json=_fc([]))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        conn = NTTrailsConnector(cache_dir=tmp_path, client=client)
        tracks = conn.fetch_tracks(NT_MACDONNELL_BBOX)
        pois = conn.fetch_pois(NT_MACDONNELL_BBOX)

    assert len(nt_roads_queries) >= 1
    assert len(tracks) == 3  # SEALED Stuart Hwy excluded; 4WD + FORMED pastoral + GA kept
    by_id = {t.source_id: t for t in tracks}
    assert by_id["22350"].source_layer == "nt_roads"
    assert by_id["22350"].name == "Finke River 4WD Track"  # leading * stripped
    assert by_id["22350"].is_4wd_only is True
    assert by_id["22351"].source_layer == "nt_roads"
    assert by_id["22351"].name == "REDBANK GORGE ROAD"
    assert by_id["22351"].trail_class == "management_trail"
    assert by_id["10"].source_layer == "nt_ga_topo250k"
    assert by_id["10"].trail_class == "walking_track"
    assert by_id["10"].is_vehicular is False
    assert {p.poi_type for p in pois} == {"lookout", "trig"}
    trig = next(p for p in pois if p.poi_type == "trig")
    assert trig.elev_m == pytest.approx(1380.4)


# -----------------------------------------------------------------------------
# 2. AutoAUStateTrailsRouter Cross-Border Dispatch & Dynamic Attributions
# -----------------------------------------------------------------------------


class _StubConnector:
    def __init__(
        self,
        *,
        attribution: str,
        tracks: list[RawTrackFeature] | None = None,
        pois: list[RawPoiFeature] | None = None,
        fail: bool = False,
    ) -> None:
        self.attribution = attribution
        self._tracks = tracks or []
        self._pois = pois or []
        self._fail = fail

    def fetch_tracks(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawTrackFeature]:
        if self._fail:
            raise RuntimeError("Upstream 503 outage")
        return list(self._tracks)

    def fetch_pois(self, bbox: BBox, *, tile_deg: float = 1.0) -> list[RawPoiFeature]:
        if self._fail:
            raise RuntimeError("Upstream 503 outage")
        return list(self._pois)


def test_auto_au_state_router_cross_border_and_dynamic_attributions() -> None:
    # 1. Strictly inside ACT -> ONLY ['ACT'] (no NSW overlap)
    assert resolve_intersecting_states(ACT_NAMADGI_BBOX) == ["ACT"]
    # 2. ACT/NSW border -> ['ACT', 'NSW']
    assert resolve_intersecting_states(ACT_NSW_BORDER_BBOX) == ["ACT", "NSW"]
    # 3. VIC/NSW Alps border -> ['NSW', 'VIC']
    assert resolve_intersecting_states(VIC_NSW_BORDER_BBOX) == ["NSW", "VIC"]
    # 4. SA/NT Outback border -> ['SA', 'NT']
    assert resolve_intersecting_states(SA_NT_BORDER_BBOX) == ["SA", "NT"]
    # 5. Outside Australia -> []
    assert resolve_intersecting_states(BBox(min_x=10.0, min_y=45.0, max_x=11.0, max_y=46.0)) == []

    act_trk = RawTrackFeature(
        source="act",
        source_layer="act_transport_track",
        source_id="act-1",
        name="Brindabella Fire Trail",
        trail_class="fire_trail",
        geometry={"type": "LineString", "coordinates": [[148.80, -35.65], [148.82, -35.66]]},
    )
    nsw_trk = RawTrackFeature(
        source="nsw",
        source_layer="nsw_fire_trails_l9",
        source_id="nsw-1",
        name="Goodradigbee Fire Trail",
        trail_class="fire_trail",
        geometry={"type": "LineString", "coordinates": [[148.68, -35.65], [148.70, -35.66]]},
    )
    vic_trk = RawTrackFeature(
        source="vic",
        source_layer="tr_road",
        source_id="vic-1",
        name="Davies Plain Track",
        trail_class="4wd_track",
        geometry={"type": "LineString", "coordinates": [[148.10, -36.60], [148.12, -36.62]]},
    )
    sa_trk = RawTrackFeature(
        source="sa",
        source_layer="sa_ga_topo250k",
        source_id="sa-1",
        name="Mulga Park Track SA",
        trail_class="4wd_track",
        geometry={"type": "LineString", "coordinates": [[131.50, -26.10], [131.52, -26.12]]},
    )
    nt_trk = RawTrackFeature(
        source="nt",
        source_layer="nt_roads",
        source_id="nt-1",
        name="Mulga Park Track NT",
        trail_class="4wd_track",
        geometry={"type": "LineString", "coordinates": [[131.50, -25.90], [131.52, -25.88]]},
    )

    # Cross-border ACT/NSW where BOTH contribute
    router_act_nsw = AutoAUStateTrailsRouter(
        state_connectors={
            "ACT": _StubConnector(attribution=ATTRIBUTION_ACT_ACTMAPI, tracks=[act_trk]),
            "NSW": _StubConnector(attribution=ATTRIBUTION_NSW_SPATIAL, tracks=[nsw_trk]),
            "VIC": _StubConnector(attribution=ATTRIBUTION_VIC_DEECA, tracks=[vic_trk]),
        }
    )
    res_act_nsw = router_act_nsw.fetch(ACT_NSW_BORDER_BBOX)
    assert res_act_nsw.contributing_states == ["ACT", "NSW"]
    assert res_act_nsw.attributions == [ATTRIBUTION_ACT_ACTMAPI, ATTRIBUTION_NSW_SPATIAL]
    assert ATTRIBUTION_VIC_DEECA not in res_act_nsw.attributions
    assert len(res_act_nsw.au_state_tracks) == 1
    assert len(res_act_nsw.nsw_tracks) == 1

    # Cross-border VIC/NSW where NSW has an outage -> ONLY VIC attribution returned!
    router_vic_only = AutoAUStateTrailsRouter(
        state_connectors={
            "NSW": _StubConnector(attribution=ATTRIBUTION_NSW_SPATIAL, fail=True),
            "VIC": _StubConnector(attribution=ATTRIBUTION_VIC_DEECA, tracks=[vic_trk]),
        }
    )
    res_vic = router_vic_only.fetch(VIC_NSW_BORDER_BBOX)
    assert res_vic.contributing_states == ["VIC"]
    assert res_vic.attributions == [ATTRIBUTION_VIC_DEECA]
    assert ATTRIBUTION_NSW_SPATIAL not in res_vic.attributions

    # Cross-border SA/NT Red Centre -> ONLY SA & NT attributions
    router_sa_nt = AutoAUStateTrailsRouter(
        state_connectors={
            "SA": _StubConnector(attribution=ATTRIBUTION_SA_DEW_DHUD, tracks=[sa_trk]),
            "NT": _StubConnector(attribution=ATTRIBUTION_NT_NTLIS, tracks=[nt_trk]),
        }
    )
    res_sa_nt = router_sa_nt.fetch(SA_NT_BORDER_BBOX)
    assert res_sa_nt.contributing_states == ["SA", "NT"]
    assert res_sa_nt.attributions == [ATTRIBUTION_SA_DEW_DHUD, ATTRIBUTION_NT_NTLIS]
    assert len(res_sa_nt.au_state_tracks) == 2


# -----------------------------------------------------------------------------
# 3. Pan-AU Conflation Priority (OSM > Official State Layers) & Live Bake Router
# -----------------------------------------------------------------------------


def test_pan_au_conflation_osm_priority_and_state_enrichment() -> None:
    osm_tas = RawTrackFeature(
        source="osm",
        source_layer="osm",
        source_id="way/50001",
        name=None,  # unnamed in OSM -> should inherit official TAS state name & fire_trail class!
        trail_class="4wd_track",
        surface_class="unknown",
        geometry={
            "type": "LineString",
            "coordinates": [[145.9000, -41.6500], [145.9000, -41.6530]],
        },
    )
    tas_overlap = RawTrackFeature(
        source="tas",
        source_layer="tas_transport_segments",
        source_id="tas-42",
        name="Murchison Fire Trail",
        trail_class="fire_trail",
        surface_class="dirt_unsealed",
        geometry={
            "type": "LineString",
            "coordinates": [[145.90005, -41.6500], [145.90005, -41.6530]],
        },
    )
    wa_only = RawTrackFeature(
        source="wa",
        source_layer="wa_slip_tracks",
        source_id="wa-99",
        name="Powerlines Track",
        trail_class="4wd_track",
        surface_class="gravel_compacted",
        geometry={
            "type": "LineString",
            "coordinates": [[116.1800, -31.9200], [116.1800, -31.9240]],
        },
    )
    qld_walk = RawTrackFeature(
        source="qld",
        source_layer="qld_qpws_walking",
        source_id="qld-w1",
        name="Thorsborne Walking Track",
        trail_class="walking_track",
        is_vehicular=False,
        geometry={
            "type": "LineString",
            "coordinates": [[146.10, -18.30], [146.11, -18.31]],
        },
    )

    res = conflate_tracks([tas_overlap, osm_tas, wa_only, qld_walk])
    assert res.dropped_non_vehicular_count == 1
    assert res.deduplicated_count == 1
    assert len(res.ways) == 2

    by_source = {w.primary_source: w for w in res.ways}
    assert by_source["osm"].has_osm_source is True
    assert by_source["osm"].name == "Murchison Fire Trail"
    assert by_source["osm"].trail_class == "fire_trail"
    assert by_source["osm"].surface_class == "dirt_unsealed"
    assert any(
        r["source"] == "tas" and r["source_id"] == "tas-42" for r in by_source["osm"].source_refs
    )

    assert by_source["wa"].has_osm_source is False
    assert by_source["wa"].name == "Powerlines Track"


def test_live_bake_fetch_uses_auto_au_router_for_tasmania(tmp_path: Path) -> None:
    """Confirm fetch_live_tile_dataset dispatches Tasmania via AutoAUStateTrailsRouter."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "thelist.tas.gov.au" in url and "/42/query" in url:
            return httpx.Response(
                200,
                json=_fc(
                    [
                        {
                            "type": "Feature",
                            "properties": {
                                "OBJECTID": 1001,
                                "TRANS_TYPE": "Road",
                                "TSEG_FEAT": "Fire Trail",
                                "TRAN_CLASS": "Vehicular Track",
                                "SURFACE_TY": "Unsealed",
                                "PRI_NAME": "Balfour Track",
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[145.90, -41.65], [145.92, -41.66]],
                            },
                        }
                    ]
                ),
            )
        if "overpass" in url:
            return httpx.Response(200, json={"elements": []})
        if "DEM_" in url or "dea-public-data" in url:
            return httpx.Response(404)
        return httpx.Response(200, json=_fc([]))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        ds = fetch_live_tile_dataset(
            TAS_CRADLE_BBOX,
            cache_dir=tmp_path / "cache",
            work_dir=tmp_path / "work",
            tile_id="tas_test_tile",
            http_client=client,
            corridor_name="Tasmania Live Bake Unit Test",
        )

    assert len(ds.au_state_tracks) == 1
    assert ds.au_state_tracks[0].source == "tas"
    assert ds.vic_tracks == []
    assert ds.nsw_tracks == []
    assert ATTRIBUTION_TAS_LIST in ds.attributions
    assert ATTRIBUTION_VIC_DEECA not in ds.attributions
    assert ATTRIBUTION_NSW_SPATIAL not in ds.attributions


# -----------------------------------------------------------------------------
# 4. 15-Region Pan-Australia Pack Catalog Validation
# -----------------------------------------------------------------------------


def test_15_region_pan_au_pack_catalog_validity() -> None:
    all_packs = list_pack_regions()
    assert len(all_packs) == 15
    assert len(PACK_REGION_IDS) == 15
    assert len(PACK_REGIONS) == 15

    state_packs = list_pack_regions(category="state")
    region_packs = list_pack_regions(category="region")
    assert len(state_packs) == 8
    assert len(region_packs) == 7
    assert {r.region_id for r in state_packs} == {
        "vic",
        "nsw",
        "act",
        "tas",
        "qld",
        "wa",
        "sa",
        "nt",
    }
    assert {r.region_id for r in region_packs} == {
        "australian-alps",
        "blue-mountains",
        "tasmania-wilderness",
        "far-north-qld",
        "flinders-ranges",
        "red-centre",
        "perth-hills",
    }

    for pack in all_packs:
        minx, miny, maxx, maxy = pack.bbox
        assert 112.0 <= minx < maxx <= 155.0, pack.region_id
        assert -45.0 <= miny < maxy <= -9.0, pack.region_id
        assert len(pack.utm_zones) >= 1, pack.region_id
        assert len(pack.states) >= 1, pack.region_id

    # Dynamic per-state attributions: TAS pack credits LIST CC BY 3.0 AU and NOT VIC/NSW!
    tas_pack = get_pack_region("tas")
    assert ATTRIBUTION_TAS_LIST in tas_pack.attributions
    assert ATTRIBUTION_VIC_DEECA not in tas_pack.attributions
    assert ATTRIBUTION_NSW_SPATIAL not in tas_pack.attributions

    # Red Centre credits NT & SA only
    red_centre = get_pack_region("red-centre")
    assert red_centre.states == ("NT", "SA")
    assert ATTRIBUTION_NT_NTLIS in red_centre.attributions
    assert ATTRIBUTION_SA_DEW_DHUD in red_centre.attributions
    assert ATTRIBUTION_WA_DBCA_SLIP not in red_centre.attributions

    # WA & QLD dynamic attributions
    assert ATTRIBUTION_WA_DBCA_SLIP in get_pack_region("perth-hills").attributions
    assert ATTRIBUTION_QLD_QSPATIAL in get_pack_region("far-north-qld").attributions


# -----------------------------------------------------------------------------
# 5. PostGIS Integration: source_tracks_au_state & Pan-AU Pipeline Persistence
# -----------------------------------------------------------------------------


@pytest.mark.integration
def test_pan_au_source_tracks_au_state_persistence_and_idempotency(postgis_engine: Engine) -> None:
    """Verify ACT/TAS/QLD/WA/SA/NT tracks persist into source_tracks_au_state & canonical tables."""
    state_samples = [
        (
            "act",
            "act_transport_track",
            "ACT-1",
            "Bimberi Fire Trail",
            [148.90, -35.65],
            [148.90, -35.652],
        ),
        (
            "tas",
            "tas_transport_segments",
            "TAS-1",
            "Climies 4WD Track",
            [145.90, -41.65],
            [145.90, -41.652],
        ),
        (
            "qld",
            "qld_state_tracks",
            "QLD-1",
            "CREB 4WD Track",
            [145.30, -16.15],
            [145.30, -16.152],
        ),
        (
            "wa",
            "wa_slip_tracks",
            "WA-1",
            "Powerlines 4WD Track",
            [116.18, -31.92],
            [116.18, -31.922],
        ),
        (
            "sa",
            "sa_fire_tracks",
            "SA-1",
            "Skytrek Flinders Track",
            [138.60, -31.50],
            [138.60, -31.502],
        ),
        (
            "nt",
            "nt_roads",
            "NT-1",
            "Finke Gorge Track",
            [132.60, -23.75],
            [132.60, -23.752],
        ),
    ]
    au_tracks = [
        RawTrackFeature(
            source=src,  # type: ignore[arg-type]
            source_layer=layer,
            source_id=fid,
            name=name,
            trail_class="4wd_track",
            surface_class="dirt_unsealed",
            surface_raw="unsealed",
            access_raw="public",
            geometry={"type": "LineString", "coordinates": [p0, p1]},
            raw_props={"agency_code": src.upper()},
        )
        for src, layer, fid, name, p0, p1 in state_samples
    ]
    tas_poi = RawPoiFeature(
        source_provider="tas_trig",
        source_license="CC-BY-3.0-AU",
        source_feature_id="ST042",
        name="Cradle Mtn Trig",
        poi_type="trig",
        state="TAS",
        elev_m=1545.0,
        geometry={"type": "Point", "coordinates": [145.9001, -41.651]},
    )
    wa_park = RawCapadFeature(
        source_feature_id="CAPAD-WA-TEST-1",
        name="Beelu National Park",
        park_type="National Park",
        type_abbr="NP",
        iucn="II",
        state="WA",
        geometry={
            "type": "Polygon",
            "coordinates": [
                [
                    [116.10, -32.00],
                    [116.30, -32.00],
                    [116.30, -31.85],
                    [116.10, -31.85],
                    [116.10, -32.00],
                ]
            ],
        },
    )

    dataset = CorridorDataset(
        corridor_name="Pan-AU 6-State Integration Proof",
        bbox=BBox(min_x=112.5, min_y=-44.0, max_x=154.0, max_y=-10.0),
        au_state_tracks=au_tracks,
        pois=[tas_poi],
        capad_parks=[wa_park],
        dem_path=None,
    )

    with Session(postgis_engine) as session:
        try:
            vic_before = session.scalar(select(func.count()).select_from(SourceTrackVIC))
            nsw_before = session.scalar(select(func.count()).select_from(SourceTrackNSW))

            summary1 = ingest_corridor_dataset(session, dataset, commit=False)
            assert summary1.tier_a_au_state_tracks == 6
            assert summary1.tracks_upserted == 6
            assert summary1.segments_active >= 12

            # Idempotent re-upsert updates cleanly on (state_code, source_layer, source_feature_id)
            summary2 = ingest_corridor_dataset(session, dataset, commit=False)
            assert summary2.tier_a_au_state_tracks == 6
            assert summary2.segments_created == 0
            assert summary2.segments_retired == 0

            rows = session.execute(select(SourceTrackAUState)).scalars().all()
            assert {r.state_code for r in rows} == {"ACT", "TAS", "QLD", "WA", "SA", "NT"}
            assert session.scalar(select(func.count()).select_from(SourceTrackVIC)) == vic_before
            assert session.scalar(select(func.count()).select_from(SourceTrackNSW)) == nsw_before

            # Canonical tables reflect all 6 states with zero CHECK constraint failures
            canon_tracks = session.execute(
                select(Track).where(
                    Track.primary_source.in_(("act", "tas", "qld", "wa", "sa", "nt"))
                )
            ).scalars().all()
            assert {t.state for t in canon_tracks} == {"ACT", "TAS", "QLD", "WA", "SA", "NT"}

            canon_segs = session.execute(
                select(TrackSegment).where(
                    TrackSegment.primary_source.in_(("act", "tas", "qld", "wa", "sa", "nt"))
                )
            ).scalars().all()
            assert {s.state for s in canon_segs} == {"ACT", "TAS", "QLD", "WA", "SA", "NT"}

            tas_poi_row = session.execute(
                select(POI).where(POI.state == "TAS", POI.source_license == "CC-BY-3.0-AU")
            ).scalar_one()
            assert tas_poi_row.name == "Cradle Mtn Trig"

            wa_park_row = session.execute(
                select(CAPADPark).where(CAPADPark.capad_external_id == "CAPAD-WA-TEST-1")
            ).scalar_one()
            assert wa_park_row.state == "WA"

            # Verify GIST index exists on source_tracks_au_state.geom
            gist_exists = session.execute(
                text(
                    "SELECT 1 FROM pg_indexes "
                    "WHERE tablename = 'source_tracks_au_state' "
                    "AND indexname = 'ix_source_tracks_au_state_geom' "
                    "AND indexdef ILIKE '%USING gist%'"
                )
            ).scalar_one_or_none()
            assert gist_exists == 1
        finally:
            session.rollback()


# -----------------------------------------------------------------------------
# 6. Optional Live Pan-AU Endpoints Smoke Test (@pytest.mark.live)
# -----------------------------------------------------------------------------


@pytest.mark.live
def test_live_pan_au_connectors_smoke(tmp_path: Path) -> None:
    """Optional live smoke test verifying all 6 Pan-AU state/territory connectors (ACT..NT)."""
    router = AutoAUStateTrailsRouter(cache_dir=tmp_path)
    qld_glass_house_bbox = BBox(min_x=152.85, min_y=-27.00, max_x=153.05, max_y=-26.85)
    checks = (
        ("ACT", ACT_NAMADGI_BBOX, ATTRIBUTION_ACT_ACTMAPI),
        ("TAS", TAS_CRADLE_BBOX, ATTRIBUTION_TAS_LIST),
        ("QLD", qld_glass_house_bbox, ATTRIBUTION_QLD_QSPATIAL),
        ("WA", WA_PERTH_HILLS_BBOX, ATTRIBUTION_WA_DBCA_SLIP),
        ("SA", SA_FLINDERS_BBOX, ATTRIBUTION_SA_DEW_DHUD),
        ("NT", NT_MACDONNELL_BBOX, ATTRIBUTION_NT_NTLIS),
    )
    for state_code, bbox, expected_attr in checks:
        res = router.fetch(bbox, include_pois=True)
        assert state_code in res.states_queried, f"{state_code} missing from states_queried"
        assert len(res.au_state_tracks) > 0, f"Expected live tracks for {state_code}"
        assert len(res.pois) > 0, f"Expected live POIs for {state_code}"
        assert expected_attr in res.attributions, f"Missing attribution for {state_code}"
        if state_code == "NT":
            assert any(t.source_layer == "nt_roads" for t in res.au_state_tracks), (
                "Expected live NTG ROAD_CENTRELINES (UFI) tracks for West MacDonnell"
            )
