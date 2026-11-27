"""Offline unit, PostGIS integration, and live smoke tests for Pan-Australia closure feeds."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from geoalchemy2.shape import from_shape
from shapely.geometry import LineString, MultiPolygon, Polygon
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from trailforge.api.closures import router as closures_router
from trailforge.db.models import (
    CAPADPark,
    ClosureAlert,
    FeedSyncStatus,
    SegmentClosureLink,
    Track,
    TrackSegment,
)
from trailforge.db.session import get_db
from trailforge.feeds.adapters import (
    ADAPTER_REGISTRY,
    ACTESAAlertsAdapter,
    EmergencyWAAdapter,
    NTFireParksAdapter,
    QLDParksAlertsAdapter,
    SAAlertAdapter,
    TasAlertAdapter,
    build_default_adapters,
)
from trailforge.feeds.base import (
    ALL_AU_STATES,
    ALL_FEED_REGISTRY,
    AU_STATE_FEED_REGISTRY,
    FEED_REGISTRY,
    FeedAdapter,
    parse_state_filter,
    resolve_enabled_states,
    resolve_feed_source,
)
from trailforge.feeds.cli import build_parser
from trailforge.feeds.queries import (
    get_closures_for_track_ids,
    get_closures_geojson,
    get_feed_health_summary,
    list_closures,
)
from trailforge.feeds.sync import sync_configured_feeds, sync_feed

# ---------------------------------------------------------------------------
# Realistic offline mock payloads for all 6 new Australian states/territories
# ---------------------------------------------------------------------------

ACT_ESA_GEORSS_XML = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:georss="http://www.georss.org/georss">
  <channel>
    <title>ACT ESA Current Incidents</title>
    <item>
      <guid>ACT-ESA-2026-01</guid>
      <title>Namadgi National Park — Bushfire Watch and Act</title>
      <link>https://esa.act.gov.au/incidents/act-esa-2026-01</link>
      <description>
        &lt;p&gt;Active bushfire near Orroral Ridge in Namadgi National Park.
        Fire trails closed.&lt;/p&gt;
      </description>
      <category>Bushfire</category>
      <pubDate>Thu, 26 Nov 2026 01:15:00 +1100</pubDate>
      <georss:point>-35.645 148.955</georss:point>
    </item>
    <item>
      <guid>ACT-ESA-MED-IGNORE</guid>
      <title>Civic Medical Call</title>
      <link>https://esa.act.gov.au/incidents/med</link>
      <description>Medical ambulance response.</description>
      <category>Medical</category>
      <pubDate>Thu, 26 Nov 2026 01:20:00 +1100</pubDate>
      <georss:point>-35.280 149.130</georss:point>
    </item>
  </channel>
</rss>
"""

TAS_CAP_AU_XML = """<?xml version="1.0" encoding="utf-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>TAS-CAP-2026-88</identifier>
  <sent>2026-11-26T01:00:00+11:00</sent>
  <status>Actual</status>
  <info>
    <category>Fire</category>
    <event>Bushfire</event>
    <urgency>Immediate</urgency>
    <severity>Severe</severity>
    <headline>Watch and Act — Bushfire near Cradle Mountain-Lake St Clair National Park</headline>
    <description>
      Uncontrolled scrub fire impacting western access tracks in
      Cradle Mountain-Lake St Clair National Park.
    </description>
    <effective>2026-11-26T00:30:00+11:00</effective>
    <expires>2026-11-27T00:30:00+11:00</expires>
    <web>https://alert.tas.gov.au/incidents/tas-cap-2026-88</web>
    <area>
      <areaDesc>Cradle Mountain-Lake St Clair National Park</areaDesc>
      <polygon>-41.68,145.92 -41.68,145.98 -41.62,145.98 -41.62,145.92 -41.68,145.92</polygon>
    </area>
  </info>
</alert>
"""

TAS_LIST_GEOJSON: dict[str, Any] = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "id": 7201,
            "properties": {
                "OBJECTID": 7201,
                "headline": "Cradle Mountain-Lake St Clair National Park — Park Closure",
                "alert_level": "Closed",
                "category": "Park Closure",
                "park_name": "Cradle Mountain-Lake St Clair National Park",
                "description": "Snow and treefall closure across 4WD management tracks.",
                "url": "https://alert.tas.gov.au/alerts/7201",
                "updated": 1795651200000,
            },
            "geometry": None,
        }
    ],
}

QLD_QPWS_RSS_XML = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>QPWS Park Alerts</title>
    <item>
      <guid>QLD-QPWS-2026-DAINTREE</guid>
      <title>Daintree National Park — CREB Track Wet Weather Closure</title>
      <link>https://parks.desi.qld.gov.au/park-alerts/daintree-creb-2026</link>
      <description>
        &lt;strong&gt;Closed:&lt;/strong&gt; Heavy tropical rain has closed 4WD tracks inside
        Daintree National Park.
      </description>
      <pubDate>Thu, 26 Nov 2026 02:00:00 +1000</pubDate>
    </item>
  </channel>
</rss>
"""

QLD_QFES_GEOJSON: dict[str, Any] = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {
                "id": "QLD-QFES-44",
                "title": "Daintree National Park — Wet Season Road & Park Alert",
                "park_name": "Daintree National Park",
                "alert_type": "hazard_warning",
                "severity": "advisory",
                "description": "Flash flooding caution on unsealed crossings.",
                "url": "https://parks.desi.qld.gov.au/park-alerts/44",
                "updated": "2026-11-26T02:00:00Z",
            },
            "geometry": None,
        }
    ],
}

WA_EMERGENCY_JSON: dict[str, Any] = {
    "warnings": [
        {
            "warning_id": "WA-DFES-2026-19",
            "title": "Cape Range National Park — Emergency Warning Bushfire",
            "warningType": "Bushfire",
            "alertLevel": "Emergency Warning",
            "park_name": "Cape Range National Park",
            "description": "Fast-moving spinifex fire along Yardie Creek 4WD track.",
            "url": "https://www.emergency.wa.gov.au/#warning/wa-dfes-2026-19",
            "updated": "2026-11-26T03:00:00Z",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [113.80, -22.25],
                        [113.88, -22.25],
                        [113.88, -22.18],
                        [113.80, -22.18],
                        [113.80, -22.25],
                    ]
                ],
            },
        },
        {
            "warning_id": "WA-DBCA-2026-20",
            "title": "Cape Range National Park — Track Closure",
            "alert_type": "park_closure",
            "severity": "closed",
            "park_name": "Cape Range National Park",
            "description": "4WD access closed for prescribed burning.",
            "url": "https://alerts.dbca.wa.gov.au/wa-dbca-2026-20",
            "updated": "2026-11-26T03:10:00Z",
            "geometry": None,
        },
    ]
}

SA_CFS_CRIIMSON_JSON: list[dict[str, Any]] = [
    {
        "IncidentNo": "SA-CFS-2026-301",
        "Date": "2026-11-26",
        "Time": "03:30:00",
        "Location_name": "Ikara-Flinders Ranges National Park",
        "Type": "Bushfire",
        "Status": "GOING",
        "Level": "Watch and Act",
        "Location": "-31.515,138.625",
        "CFS_Region": "Region 4",
        "url": "https://www.cfs.sa.gov.au/warnings-restrictions/warnings/incidents-warnings/",
    },
    {
        "IncidentNo": "SA-NPWS-2026-302",
        "title": "Ikara-Flinders Ranges National Park — Park Closure",
        "parkName": "Ikara-Flinders Ranges National Park",
        "alert_type": "park_closure",
        "severity": "closed",
        "description": "Extreme fire danger park closure across Moralana & Bunyeroo tracks.",
        "url": "https://www.parks.sa.gov.au/know-before-you-go/closures-and-alerts",
        "updated": "2026-11-26T03:35:00Z",
    },
    {
        "IncidentNo": "SA-CFS-IGNORE",
        "Location_name": "Adelaide CBD",
        "Type": "Alarm - Automatic",
        "Status": "GOING",
        "Location": "-34.928,138.600",
    },
]

QLD_QFD_LIVE_GEOJSON: dict[str, Any] = {
    "type": "FeatureCollection",
    "name": "QFDWarnings",
    "features": [
        {
            "type": "Feature",
            "properties": {
                "OBJECTID": 4051,
                "UniqueID": "WARN-14232",
                "WarningTitle": (
                    "STAY INFORMED - Daintree National Park - fire as at 2:34pm Thursday"
                ),
                "WarningLevel": "Advice",
                "CallToAction": "Stay Informed",
                "Header": (
                    "A fire is burning near Herberton Petford Road in Daintree National Park."
                ),
                "WarningText": "Full bushfire advice text.",
                "WarningArea": "Daintree National Park",
                "Latitude": -16.18,
                "Longitude": 145.36,
                "GroupedType": "FIRE VEGETATION",
                "WarningType_Level": "Advice Fire",
                "EventType": "Fire",
                "ItemDateTimeLocal_ISO": "2026-11-26T14:34:51+10:00",
                "PublishDateLocal_ISO": "2026-11-26T16:10:57+10:00",
                "ItemExpiryDateTimeLocal_ISO": "2026-11-27T14:34:51+10:00",
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [145.32, -16.20],
                        [145.40, -16.20],
                        [145.40, -16.14],
                        [145.32, -16.14],
                        [145.32, -16.20],
                    ]
                ],
            },
        },
        {
            "type": "Feature",
            "properties": {
                "OBJECTID": 4052,
                "UniqueID": "WARN-14233",
                "WarningTitle": "AVOID SMOKE - Glass House Mountains - hazard reduction burn",
                "WarningLevel": "Information",
                "Header": "Smoke from hazard reduction burn near forestry tracks.",
                "WarningArea": "Glass House Mountains",
                "Latitude": -26.90,
                "Longitude": 152.95,
                "GroupedType": "FIRE VEGETATION",
                "EventType": "Fire",
                "ItemDateTimeLocal_ISO": "2026-11-26T12:00:00+10:00",
            },
            "geometry": None,
        },
    ],
}

WA_EMERGENCY_V1_API_JSON: dict[str, Any] = {
    "warnings": [
        {
            "id": "6b0560ccb2307c57b61f0766",
            "entityType": "warnings",
            "entitySubType": "warnings_bushfire--advice",
            "warning-type": "Bushfire Advice",
            "name": "Bushfire Advice",
            "title": "MONITOR CONDITIONS - DURACK AND LAKE ARGYLE",
            "headline": "CAPE RANGE NATIONAL PARK",
            "suburbs": ["DURACK", "LAKE ARGYLE"],
            "lga": ["SHIRE OF WYNDHAM-EAST KIMBERLEY"],
            "cap-category": "Fire",
            "cap-event-type": ["Bushfire"],
            "cap-severity": "Minor - minimal threat",
            "publishing-status": "Published",
            "alert-line": "<p>A Bushfire Advice is in place near Glen Hill Road.</p>",
            "action-statement": "Monitor conditions",
            "location": {
                "latitude": -15.45194,
                "longitude": 127.1944,
                "value": "Durack, Western Australia",
            },
            "published-date-time": "2026-11-25T19:16:44.267+08:00",
            "issued-date-time": "2026-11-26T10:53:14.956+08:00",
            "updatedAt": "2026-11-25T11:16:45.328Z",
            "geo-source": {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [128.3439, -16.2647],
                        },
                        "properties": {"icon-name": "ew-bushfire--advice"},
                    },
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [128.30, -16.30],
                                    [128.40, -16.30],
                                    [128.40, -16.20],
                                    [128.30, -16.20],
                                    [128.30, -16.30],
                                ]
                            ],
                        },
                        "properties": {"icon-name": "ew-bushfire--advice"},
                    },
                ],
            },
        },
        {
            "id": "6affc593683a597840d081ac",
            "entityType": "warnings",
            "entitySubType": "warnings_heatwave--advice",
            "warning-type": "Heatwave Advice",
            "name": "Heatwave Advice",
            "title": "THREAT IS REDUCED - FITZROY CROSSING",
            "cap-category": "Met",
            "cap-event-type": ["Extreme Heat"],
            "cap-severity": "Minor - minimal threat",
            "publishing-status": "Published",
            "alert-line": "<p>Severe heatwave conditions easing.</p>",
            "location": {
                "latitude": -18.19,
                "longitude": 125.56,
                "value": "Fitzroy Crossing, Western Australia",
            },
            "issued-date-time": "2026-11-26T09:00:00+08:00",
            "updatedAt": "2026-11-26T01:00:00Z",
        },
    ]
}

NT_ROADREPORT_API_JSON: dict[str, Any] = {
    "$id": "1",
    "response": [
        {
            "$id": "2",
            "recordId": 8252,
            "obstructionId": 14301,
            "status": "CURRENT",
            "road": 1358,
            "roadName": "Wildman River 4Wd",
            "obstructionType": "Flooding",
            "restrictionType": "Road Closed",
            "dateFrom": "2025-11-19 11:05:56",
            "dateTo": None,
            "dateActive": "2025-11-19 11:05:31",
            "startPoint": [-12.7241, 131.6652],
            "endPoint": [-12.7500, 131.6800],
            "comment": "-",
            "locationComment": "Closed from Arnhem Highway turnoff",
            "dateLastUpdated": "2026-11-25 13:07:51",
        },
        {
            "$id": "3",
            "recordId": 7781,
            "obstructionId": 13840,
            "status": "CURRENT",
            "road": 341,
            "roadName": "Gosse Bluff Touracc",
            "obstructionType": "Changing Surface Conditions",
            "restrictionType": "High Clearance 4WD",
            "dateFrom": "2025-04-09 13:35:38",
            "dateTo": None,
            "dateActive": "2025-04-09 13:38:40",
            "startPoint": [-23.7936, 132.3613],
            "endPoint": [-23.7936, 132.3613],
            "comment": "Motorists are advised to drive to the conditions.",
            "locationComment": "West MacDonnell National Park approach",
            "dateLastUpdated": "2026-06-24 10:16:31",
        },
        {
            "$id": "4",
            "recordId": 662,
            "obstructionId": 8785,
            "status": "CURRENT",
            "road": 98,
            "roadName": "Dorat Road",
            "obstructionType": "Maximum 7 Axles",
            "restrictionType": "Weight And Or Vehicle Type Restriction",
            "dateFrom": "2018-06-20 08:30:00",
            "dateTo": None,
            "startPoint": [-13.2419, 131.1108],
            "endPoint": [-13.4000, 131.1200],
            "comment": "Commercial freight axle limit.",
            "dateLastUpdated": "2026-01-01 00:00:00",
        },
    ],
}

NT_SECURENT_GEOJSON: dict[str, Any] = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {
                "incident_id": "NT-SEC-2026-77",
                "title": "West MacDonnell National Park — Bushfire Advice",
                "category": "Bushfire",
                "alert_level": "Advice",
                "park_name": "West MacDonnell National Park",
                "description": (
                    "Smoke and active grassfire near Finke Gorge & Namatjira 4WD access."
                ),
                "url": "https://securent.nt.gov.au/alerts/nt-sec-2026-77",
                "updated": "2026-11-26T04:00:00Z",
            },
            "geometry": None,
        }
    ],
}


# ---------------------------------------------------------------------------
# Unit tests (100% offline, zero DB required)
# ---------------------------------------------------------------------------


def test_all_eleven_adapters_registered_and_implement_protocol() -> None:
    assert ALL_AU_STATES == ("VIC", "NSW", "ACT", "TAS", "QLD", "WA", "SA", "NT")
    assert len(FEED_REGISTRY) == 5
    assert len(AU_STATE_FEED_REGISTRY) == 6
    assert len(ALL_FEED_REGISTRY) == 11
    assert set(ADAPTER_REGISTRY.keys()) == set(ALL_FEED_REGISTRY.keys())

    expected_new = {
        "act_esa": "ACT",
        "tas_alert": "TAS",
        "qld_des_alerts": "QLD",
        "wa_emergency": "WA",
        "sa_cfs_parks": "SA",
        "nt_securent": "NT",
    }
    for feed_key, state_code in expected_new.items():
        assert feed_key in AU_STATE_FEED_REGISTRY
        assert ALL_FEED_REGISTRY[feed_key].state == state_code
        adapter = ADAPTER_REGISTRY[feed_key]()
        assert isinstance(adapter, FeedAdapter)
        assert adapter.feed_source == feed_key
        assert adapter.state == state_code
        assert resolve_feed_source(feed_key) == feed_key
        assert resolve_feed_source(ALL_FEED_REGISTRY[feed_key].alias) == feed_key

    with pytest.raises(ValueError, match="Unknown feed source"):
        resolve_feed_source("nonexistent_feed")


def test_all_six_pan_au_adapters_parse_realistic_payloads_offline() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "esa.act.gov.au" in url:
            return httpx.Response(
                200,
                text=ACT_ESA_GEORSS_XML,
                headers={"Content-Type": "application/rss+xml; charset=utf-8"},
            )
        if "thelist.tas.gov.au" in url:
            return httpx.Response(200, json=TAS_LIST_GEOJSON)
        if "alert.tas.gov.au/cap" in url:
            return httpx.Response(
                200,
                text=TAS_CAP_AU_XML,
                headers={"Content-Type": "application/xml; charset=utf-8"},
            )
        if "parks.desi.qld.gov.au" in url or "fire.qld.gov.au" in url:
            return httpx.Response(
                200,
                text=QLD_QPWS_RSS_XML,
                headers={"Content-Type": "application/rss+xml; charset=utf-8"},
            )
        if "emergency.wa.gov.au" in url:
            return httpx.Response(200, json=WA_EMERGENCY_JSON)
        if "data.eso.sa.gov.au" in url:
            return httpx.Response(200, json=SA_CFS_CRIIMSON_JSON)
        if "securent.nt.gov.au" in url or "roadreport.nt.gov.au" in url:
            return httpx.Response(200, json=NT_SECURENT_GEOJSON)
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        # 1. ACT ESA GeoRSS XML (unrelated medical event filtered out)
        act_items = ACTESAAlertsAdapter(client=client).fetch()
        assert len(act_items) == 1
        assert act_items[0].state == "ACT"
        assert act_items[0].external_id == "ACT-ESA-2026-01"
        assert act_items[0].alert_type == "bushfire"
        assert act_items[0].db_severity == "watch_and_act"
        assert act_items[0].park_name == "Namadgi National Park"
        assert act_items[0].geometry is not None
        assert act_items[0].geometry["type"] == "Point"
        assert list(act_items[0].geometry["coordinates"]) == [148.955, -35.645]
        assert act_items[0].source_url == "https://esa.act.gov.au/incidents/act-esa-2026-01"

        # 2a. TasALERT LIST GeoJSON
        tas_geo = TasAlertAdapter(client=client).fetch()
        assert len(tas_geo) == 1
        assert tas_geo[0].state == "TAS"
        assert tas_geo[0].alert_type == "park_closure"
        assert tas_geo[0].db_severity == "closed"
        assert tas_geo[0].park_name == "Cradle Mountain-Lake St Clair National Park"
        assert tas_geo[0].source_url == "https://alert.tas.gov.au/alerts/7201"

        # 2b. TasALERT CAP-AU 1.2 XML polygon parsing
        tas_cap = TasAlertAdapter(
            client=client, source_url="https://alert.tas.gov.au/cap.xml"
        ).fetch()
        assert len(tas_cap) == 1
        assert tas_cap[0].external_id == "TAS-CAP-2026-88"
        assert tas_cap[0].alert_type == "bushfire"
        assert tas_cap[0].db_severity == "watch_and_act"
        assert tas_cap[0].geometry is not None and tas_cap[0].geometry["type"] == "Polygon"
        assert tas_cap[0].park_name == "Cradle Mountain-Lake St Clair National Park"

        # 3. QLD DESI / QPWS RSS XML + CAPAD park extraction
        qld_items = QLDParksAlertsAdapter(client=client).fetch()
        assert len(qld_items) == 1
        assert qld_items[0].state == "QLD"
        assert qld_items[0].alert_type == "park_closure"
        assert qld_items[0].db_severity == "closed"
        assert qld_items[0].park_name == "Daintree National Park"
        assert qld_items[0].source_url == "https://parks.desi.qld.gov.au/park-alerts/daintree-creb-2026"

        # 4. Emergency WA DFES/DBCA JSON (polygon + park closure)
        wa_items = EmergencyWAAdapter(client=client).fetch()
        assert len(wa_items) == 2
        assert wa_items[0].state == "WA"
        assert wa_items[0].db_severity == "emergency"
        assert wa_items[0].geometry is not None and wa_items[0].geometry["type"] == "Polygon"
        assert wa_items[0].source_url == "https://www.emergency.wa.gov.au/#warning/wa-dfes-2026-19"
        assert wa_items[1].alert_type == "park_closure"
        assert wa_items[1].park_name == "Cape Range National Park"

        # 5. SA CFS CRIIMSON flat JSON + NPWS SA park closure (automatic alarm filtered out)
        sa_items = SAAlertAdapter(client=client).fetch()
        assert len(sa_items) == 2
        assert sa_items[0].state == "SA"
        assert sa_items[0].external_id == "SA-CFS-2026-301"
        assert sa_items[0].db_severity == "watch_and_act"
        assert sa_items[0].geometry is not None
        assert sa_items[0].geometry["type"] == "Point"
        assert list(sa_items[0].geometry["coordinates"]) == [138.625, -31.515]
        assert sa_items[0].park_name == "Ikara-Flinders Ranges National Park"
        assert sa_items[1].alert_type == "park_closure"
        assert sa_items[1].park_name == "Ikara-Flinders Ranges National Park"

        # 6. SecureNT / Bushfires NT GeoJSON
        nt_items = NTFireParksAdapter(client=client).fetch()
        assert len(nt_items) == 1
        assert nt_items[0].state == "NT"
        assert nt_items[0].alert_type == "bushfire"
        assert nt_items[0].db_severity == "advisory"
        assert nt_items[0].park_name == "West MacDonnell National Park"
        assert nt_items[0].source_url == "https://securent.nt.gov.au/alerts/nt-sec-2026-77"


def test_real_world_qld_qfd_wa_v1_and_nt_roadreport_schemas_offline() -> None:
    """Verify exact live upstream JSON schemas for QFD (QLD), Emergency WA v1, & Road Report NT."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "fire.qld.gov.au/data/alerts/bushfireAlert.json" in url:
            return httpx.Response(200, json=QLD_QFD_LIVE_GEOJSON)
        if "api.emergency.wa.gov.au/v1/warnings" in url:
            return httpx.Response(200, json=WA_EMERGENCY_V1_API_JSON)
        if "roadreport.nt.gov.au/api/Obstruction/GetAll" in url:
            return httpx.Response(200, json=NT_ROADREPORT_API_JSON)
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        # 1. QLD QFD GeoJSON (Polygon + Lat/Lon fallback, UniqueID, WarningLevel)
        qld_items = QLDParksAlertsAdapter(client=client).fetch()
        assert len(qld_items) == 2
        assert qld_items[0].external_id == "WARN-14232"
        assert qld_items[0].state == "QLD"
        assert qld_items[0].alert_type == "bushfire"
        assert qld_items[0].db_severity == "advisory"
        assert qld_items[0].geometry is not None and qld_items[0].geometry["type"] == "Polygon"
        assert qld_items[0].park_name == "Daintree National Park"
        assert qld_items[0].effective_from is not None
        assert qld_items[0].effective_to is not None
        assert qld_items[0].source_url == "https://www.fire.qld.gov.au/current-incidents"
        assert qld_items[1].external_id == "WARN-14233"
        assert qld_items[1].db_severity == "info"
        assert qld_items[1].geometry == {"type": "Point", "coordinates": [152.95, -26.9]}

        # 2. Emergency WA v1 `/v1/warnings` (prefers Polygon in geo-source over Point,
        #    preserves warning-type severity, and maps non-fire warnings to hazard_warning)
        wa_items = EmergencyWAAdapter(client=client).fetch()
        assert len(wa_items) == 2
        assert wa_items[0].external_id == "6b0560ccb2307c57b61f0766"
        assert wa_items[0].state == "WA"
        assert wa_items[0].alert_type == "bushfire"
        assert wa_items[0].db_severity == "advisory"
        assert wa_items[0].geometry is not None and wa_items[0].geometry["type"] == "Polygon"
        assert wa_items[0].park_name == "Cape Range National Park"
        assert wa_items[0].description == "A Bushfire Advice is in place near Glen Hill Road."
        assert (
            wa_items[0].source_url
            == "https://www.emergency.wa.gov.au/alerts/6b0560ccb2307c57b61f0766"
        )
        assert wa_items[1].external_id == "6affc593683a597840d081ac"
        assert wa_items[1].alert_type == "hazard_warning"
        assert wa_items[1].db_severity == "advisory"
        assert wa_items[1].geometry == {"type": "Point", "coordinates": [125.56, -18.19]}

        # 3. Road Report NT `/api/Obstruction/GetAll` (LineString + Point + axle-limit filter)
        nt_items = NTFireParksAdapter(client=client).fetch()
        assert len(nt_items) == 2  # Dorat Road axle mass-limit filtered out cleanly
        assert nt_items[0].external_id == "nt-rr-14301"
        assert nt_items[0].state == "NT"
        assert nt_items[0].alert_type == "road_closure"
        assert nt_items[0].db_severity == "closed"
        assert nt_items[0].status == "active"
        assert nt_items[0].geometry is not None
        assert nt_items[0].geometry["type"] == "LineString"
        assert [list(pt) for pt in nt_items[0].geometry["coordinates"]] == [
            [131.6652, -12.7241],
            [131.68, -12.75],
        ]
        assert nt_items[0].source_url == "https://roadreport.nt.gov.au/"

        assert nt_items[1].external_id == "nt-rr-13840"
        assert nt_items[1].alert_type == "hazard_warning"
        assert nt_items[1].db_severity == "advisory"
        assert nt_items[1].park_name == "West MacDonnell National Park"
        assert nt_items[1].geometry is not None
        assert nt_items[1].geometry["type"] == "Point"
        assert list(nt_items[1].geometry["coordinates"]) == [132.3613, -23.7936]


def test_combined_upstream_collections_keep_independent_records() -> None:
    adapter = QLDParksAlertsAdapter(fixture_payload={
        "warnings": [{"id": "warning-one", "title": "Road closure"}],
        "incidents": {"features": [{"id": "incident-one", "title": "Flood warning"}]},
    })
    assert {item.external_id for item in adapter.fetch()} == {"warning-one", "incident-one"}


def test_enabled_states_scoping_and_adapter_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRAILFORGE_ENABLED_STATES", raising=False)
    assert resolve_enabled_states() == ("VIC", "NSW")
    default_feeds = [a.feed_source for a in build_default_adapters()]
    assert default_feeds == [
        "vic_mapshare_seasonal",
        "vic_emergency",
        "nsw_rfs",
        "nsw_npws",
        "osm_access",
    ]

    monkeypatch.setenv("TRAILFORGE_ENABLED_STATES", "VIC,NSW,ACT")
    assert resolve_enabled_states() == ("VIC", "NSW", "ACT")
    act_feeds = [a.feed_source for a in build_default_adapters()]
    assert len(act_feeds) == 6
    assert "act_esa" in act_feeds

    monkeypatch.setenv("TRAILFORGE_ENABLED_STATES", "ALL")
    assert resolve_enabled_states() == ALL_AU_STATES
    all_feeds = [a.feed_source for a in build_default_adapters()]
    assert len(all_feeds) == 11
    assert set(all_feeds) == set(ALL_FEED_REGISTRY.keys())

    # Explicit states override env
    qld_tas = [a.feed_source for a in build_default_adapters(states="QLD,TAS", include_osm=False)]
    assert qld_tas == ["tas_alert", "qld_des_alerts"]

    # Explicit only_feeds resolves aliases cleanly
    alias_selected = [
        a.feed_source
        for a in build_default_adapters(only_feeds=["act_alerts", "wa_dfes", "nt_alerts"])
    ]
    assert alias_selected == ["act_esa", "wa_emergency", "nt_securent"]

    with pytest.raises(ValueError, match="Unknown Australian state"):
        parse_state_filter("VIC,ZZZ")


def test_cli_states_flag_and_zero_runtime_warning() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["--states", "ALL", "--fixtures", "tests/fixtures/closures_sample.json"]
    )
    assert args.states == "ALL"
    assert args.fixtures == Path("tests/fixtures/closures_sample.json")

    proc = subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-m", "trailforge.feeds.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    assert "--states" in proc.stdout
    assert "RuntimeWarning" not in proc.stderr


# ---------------------------------------------------------------------------
# PostGIS integration tests (100% offline, isolated on trailforge_test DB)
# ---------------------------------------------------------------------------


def _seed_pan_au_parks_and_tracks(session: Session) -> dict[str, str]:
    """Insert synthetic CAPAD parks + canonical tracks/segments across ACT/TAS/QLD/WA/SA/NT."""
    specs = [
        (
            "ACT",
            "capad-test-act-namadgi",
            "Namadgi National Park",
            "tf_test_trk_act_1",
            "Orroral Ridge Fire Trail",
            "tf_test_seg_act_1",
            [(148.94, -35.66), (148.97, -35.63)],
            Polygon([(148.92, -35.68), (148.99, -35.68), (148.99, -35.61), (148.92, -35.61)]),
        ),
        (
            "TAS",
            "capad-test-tas-cradle",
            "Cradle Mountain-Lake St Clair National Park",
            "tf_test_trk_tas_1",
            "Cradle Link 4WD Track",
            "tf_test_seg_tas_1",
            [(145.93, -41.66), (145.96, -41.64)],
            Polygon([(145.90, -41.70), (146.00, -41.70), (146.00, -41.60), (145.90, -41.60)]),
        ),
        (
            "QLD",
            "capad-test-qld-daintree",
            "Daintree National Park",
            "tf_test_trk_qld_1",
            "CREB Track North",
            "tf_test_seg_qld_1",
            [(145.32, -16.12), (145.35, -16.10)],
            Polygon([(145.30, -16.15), (145.38, -16.15), (145.38, -16.08), (145.30, -16.08)]),
        ),
        (
            "WA",
            "capad-test-wa-caperange",
            "Cape Range National Park",
            "tf_test_trk_wa_1",
            "Yardie Creek 4WD Track",
            "tf_test_seg_wa_1",
            [(113.82, -22.23), (113.86, -22.20)],
            Polygon([(113.78, -22.27), (113.90, -22.27), (113.90, -22.16), (113.78, -22.16)]),
        ),
        (
            "SA",
            "capad-test-sa-flinders",
            "Ikara-Flinders Ranges National Park",
            "tf_test_trk_sa_1",
            "Moralana Scenic 4WD Track",
            "tf_test_seg_sa_1",
            [(138.61, -31.52), (138.64, -31.50)],
            Polygon([(138.58, -31.55), (138.68, -31.55), (138.68, -31.48), (138.58, -31.48)]),
        ),
        (
            "NT",
            "capad-test-nt-westmac",
            "West MacDonnell National Park",
            "tf_test_trk_nt_1",
            "Namatjira Backcountry Track",
            "tf_test_seg_nt_1",
            [(132.72, -23.72), (132.75, -23.70)],
            Polygon([(132.68, -23.76), (132.78, -23.76), (132.78, -23.68), (132.68, -23.68)]),
        ),
    ]

    track_ids_by_state: dict[str, str] = {}
    for state, capad_ext, park_name, track_id, track_name, seg_id, coords, poly in specs:
        park = CAPADPark(
            capad_external_id=capad_ext,
            name=park_name,
            park_type="National Park",
            type_abbr="NP",
            state=state,
            geom=from_shape(MultiPolygon([poly]), srid=4326),
        )
        session.add(park)
        session.flush()

        line = LineString(coords)
        track = Track(
            track_id=track_id,
            name=track_name,
            state=state,
            primary_source="osm",
            trail_class="4wd_track",
            total_length_m=3200.0,
            primary_park_id=park.id,
            bbox_geom=from_shape(poly, srid=4326),
            is_active=True,
        )
        session.add(track)
        session.flush()

        seg = TrackSegment(
            segment_id=seg_id,
            track_id=track_id,
            segment_seq=1,
            geom_fingerprint=f"fp_{state.lower()}_001",
            geom=from_shape(line, srid=4326),
            start_node_key=f"n_{state.lower()}_0",
            end_node_key=f"n_{state.lower()}_1",
            length_m=95.0,
            bearing_deg=45.0,
            state=state,
            name=track_name,
            trail_class="4wd_track",
            surface_class="dirt_unsealed",
            tracktype="grade4",
            access_status="public",
            capad_park_id=park.id,
            primary_source="osm",
            is_active=True,
        )
        session.add(seg)
        track_ids_by_state[state] = track_id

    session.commit()
    return track_ids_by_state


def _cleanup_pan_au_test_rows(session: Session) -> None:
    new_feeds = list(AU_STATE_FEED_REGISTRY.keys())
    alert_ids = session.execute(
        select(ClosureAlert.id).where(ClosureAlert.source_feed.in_(new_feeds))
    ).scalars().all()
    if alert_ids:
        session.execute(
            delete(SegmentClosureLink).where(SegmentClosureLink.closure_id.in_(alert_ids))
        )
        session.execute(delete(ClosureAlert).where(ClosureAlert.id.in_(alert_ids)))
    session.execute(delete(FeedSyncStatus).where(FeedSyncStatus.feed_name.in_(new_feeds)))
    session.execute(delete(TrackSegment).where(TrackSegment.segment_id.like("tf_test_seg_%")))
    session.execute(delete(Track).where(Track.track_id.like("tf_test_trk_%")))
    session.execute(
        delete(CAPADPark).where(CAPADPark.capad_external_id.like("capad-test-%"))
    )
    session.commit()


@pytest.mark.integration
def test_pan_au_capad_joins_outage_retention_api_and_cli(
    postgis_engine: Engine,
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRAILFORGE_ENABLED_STATES", raising=False)
    now = datetime(2026, 11, 26, 4, 0, 0, tzinfo=UTC)

    def ok_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "esa.act.gov.au" in url:
            return httpx.Response(
                200,
                text=ACT_ESA_GEORSS_XML,
                headers={"Content-Type": "application/rss+xml"},
            )
        if "thelist.tas.gov.au" in url:
            return httpx.Response(200, json=TAS_LIST_GEOJSON)
        if "parks.desi.qld.gov.au" in url or "fire.qld.gov.au" in url:
            return httpx.Response(200, json=QLD_QFES_GEOJSON)
        if "emergency.wa.gov.au" in url:
            return httpx.Response(200, json=WA_EMERGENCY_JSON)
        if "data.eso.sa.gov.au" in url:
            return httpx.Response(200, json=SA_CFS_CRIIMSON_JSON)
        if "securent.nt.gov.au" in url or "roadreport.nt.gov.au" in url:
            return httpx.Response(200, json=NT_SECURENT_GEOJSON)
        return httpx.Response(404)

    with Session(postgis_engine) as session:
        _cleanup_pan_au_test_rows(session)
        try:
            track_ids = _seed_pan_au_parks_and_tracks(session)

            # 1. Sync all 6 new Pan-AU feeds via mocked HTTP
            with httpx.Client(transport=httpx.MockTransport(ok_handler)) as client:
                adapters = [
                    ACTESAAlertsAdapter(client=client),
                    TasAlertAdapter(client=client),
                    QLDParksAlertsAdapter(client=client),
                    EmergencyWAAdapter(client=client),
                    SAAlertAdapter(client=client),
                    NTFireParksAdapter(client=client),
                ]
                summary = sync_configured_feeds(session, adapters=adapters, now=now)

            assert summary.overall_ok is True
            by_feed = {r.feed_source: r for r in summary.results}
            assert set(by_feed.keys()) == set(AU_STATE_FEED_REGISTRY.keys())
            for feed_key in AU_STATE_FEED_REGISTRY:
                assert by_feed[feed_key].ok is True
                assert by_feed[feed_key].records_upserted >= 1
                assert by_feed[feed_key].segments_linked >= 1, (
                    f"Expected segment link for {feed_key}"
                )

            # 2. Verify CAPAD park joins & track closure rollups across all 6 states
            rollups = get_closures_for_track_ids(
                session, list(track_ids.values()), now=now + timedelta(minutes=5)
            )
            assert rollups[track_ids["ACT"]]["closure_status"] == "warning"
            assert rollups[track_ids["TAS"]]["closure_status"] == "closed"
            assert rollups[track_ids["TAS"]]["active_closures"][0]["capad_park_name"] == (
                "Cradle Mountain-Lake St Clair National Park"
            )
            assert rollups[track_ids["QLD"]]["closure_status"] == "warning"
            assert rollups[track_ids["QLD"]]["active_closures"][0]["closure_type"] == "park_alert"
            assert rollups[track_ids["QLD"]]["active_closures"][0]["capad_park_name"] == (
                "Daintree National Park"
            )
            assert rollups[track_ids["WA"]]["closure_status"] == "closed"
            assert rollups[track_ids["SA"]]["closure_status"] == "closed"
            assert rollups[track_ids["NT"]]["closure_status"] == "warning"
            assert rollups[track_ids["NT"]]["active_closures"][0]["capad_park_name"] == (
                "West MacDonnell National Park"
            )

            # 3. Outage resilience: HTTP 503 on QLD + timeout on WA must retain last-known alerts
            #    and mark is_stale=True without wiping segment links!
            def outage_503(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(503, text="Service Unavailable")

            def outage_timeout(request: httpx.Request) -> httpx.Response:
                raise httpx.ReadTimeout("Upstream feed timed out", request=request)

            with httpx.Client(transport=httpx.MockTransport(outage_503)) as c503:
                qld_fail = sync_feed(
                    session,
                    QLDParksAlertsAdapter(client=c503),
                    now=now + timedelta(minutes=15),
                )
            assert qld_fail.ok is False
            assert qld_fail.status == "degraded"
            assert qld_fail.retained_existing == 1

            with httpx.Client(transport=httpx.MockTransport(outage_timeout)) as ctimeout:
                wa_fail = sync_feed(
                    session,
                    EmergencyWAAdapter(client=ctimeout),
                    now=now + timedelta(minutes=15),
                )
            assert wa_fail.ok is False
            assert wa_fail.status == "degraded"
            assert wa_fail.retained_existing == 2

            # Verify retained QLD & WA alerts now carry is_stale=True
            qld_during_outage = list_closures(
                session, state="QLD", now=now + timedelta(minutes=15)
            )
            assert len(qld_during_outage["items"]) == 1
            assert qld_during_outage["items"][0]["is_stale"] is True
            assert qld_during_outage["items"][0]["capad_park_name"] == "Daintree National Park"

            # 4. Verify FastAPI endpoints (?state=QLD, comma-separated, available_states, /status)
            app = FastAPI()
            app.include_router(closures_router)

            def _override_db():
                with Session(postgis_engine) as db_sess:
                    yield db_sess

            app.dependency_overrides[get_db] = _override_db
            with TestClient(app) as api_client:
                # Single-state filter (?state=QLD)
                r_qld = api_client.get("/api/closures?state=QLD")
                assert r_qld.status_code == 200
                qld_body = r_qld.json()
                assert qld_body["available_states"] == list(ALL_AU_STATES)
                assert qld_body["filtered_states"] == ["QLD"]
                assert len(qld_body["items"]) == 1
                assert qld_body["items"][0]["state"] == "QLD"
                assert qld_body["items"][0]["is_stale"] is True
                assert any(
                    f["feed_source"] == "qld_des_alerts" and f["state"] == "QLD"
                    for f in qld_body["feeds"]
                )

                # Comma-separated multi-state filter (?state=TAS,SA,NT)
                r_multi = api_client.get("/api/closures?state=TAS,SA,NT")
                assert r_multi.status_code == 200
                multi_body = r_multi.json()
                assert {i["state"] for i in multi_body["items"]} == {"TAS", "SA", "NT"}
                assert multi_body["filtered_states"] == ["TAS", "SA", "NT"]

                # Status endpoint scoped to QLD,WA shows degraded feeds + all 8 available_states
                r_status_qw = api_client.get("/api/closures/status?state=QLD,WA")
                assert r_status_qw.status_code == 200
                qw_body = r_status_qw.json()
                assert qw_body["available_states"] == list(ALL_AU_STATES)
                assert qw_body["overall_status"] == "degraded"
                assert {f["feed_source"] for f in qw_body["feeds"]} == {
                    "qld_des_alerts",
                    "wa_emergency",
                }
                assert all(f["is_stale"] for f in qw_body["feeds"])

                # Status endpoint with ?state=ALL returns all 11 Pan-AU feeds with state metadata
                r_status_all = api_client.get("/api/closures/status?state=ALL")
                assert r_status_all.status_code == 200
                all_body = r_status_all.json()
                assert len(all_body["feeds"]) == 11
                assert {f["feed_source"] for f in all_body["feeds"]} == set(
                    ALL_FEED_REGISTRY.keys()
                )
                assert all("state" in f and f["state"] for f in all_body["feeds"])

                # GeoJSON endpoint with comma-separated states
                r_geo = api_client.get("/api/closures/geojson?state=ACT,WA")
                assert r_geo.status_code == 200
                geo_body = r_geo.json()
                assert geo_body["type"] == "FeatureCollection"
                assert len(geo_body["features"]) >= 2
                assert {f["properties"]["state"] for f in geo_body["features"]} <= {"ACT", "WA"}

                # Invalid state code returns 422 cleanly
                assert api_client.get("/api/closures?state=NOPE").status_code == 422
                assert api_client.get("/api/closures/status?state=NOPE").status_code == 422
                assert api_client.get("/api/closures/geojson?state=NOPE").status_code == 422

                # Direct helper check for get_closures_geojson multi-state
                gj_tas_nt = get_closures_geojson(session, state="TAS,NT", now=now)
                assert gj_tas_nt["type"] == "FeatureCollection"
                assert len(gj_tas_nt["features"]) >= 2

                # Health summary default respects TRAILFORGE_ENABLED_STATES
                h_default = get_feed_health_summary(session, now=now)
                assert h_default["available_states"] == list(ALL_AU_STATES)
                assert h_default["enabled_states"] == ["VIC", "NSW"]
                assert len(h_default["feeds"]) == 5

            # 5. CLI `--states ALL --fixtures ...` executes cleanly with zero RuntimeWarning
            cli_proc = subprocess.run(
                [
                    sys.executable,
                    "-W",
                    "error::RuntimeWarning",
                    "-m",
                    "trailforge.feeds.cli",
                    "--states",
                    "ALL",
                    "--fixtures",
                    str(fixtures_dir / "closures_sample.json"),
                    "--database-url",
                    postgis_engine.url.render_as_string(hide_password=False),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert cli_proc.returncode == 0, f"stderr={cli_proc.stderr}"
            cli_out = json.loads(cli_proc.stdout)
            assert cli_out["overall_ok"] is True
            assert {r["feed_source"] for r in cli_out["results"]} == {
                "vic_mapshare_seasonal", "vic_emergency", "nsw_npws", "osm_access",
            }
        finally:
            _cleanup_pan_au_test_rows(session)


@pytest.mark.live
def test_live_pan_au_closure_feeds_smoke() -> None:
    """Optional live smoke check verifying all 6 Pan-Australia official state/territory feeds."""
    act_items = ACTESAAlertsAdapter(timeout_s=20.0).fetch()
    tas_items = TasAlertAdapter(timeout_s=20.0).fetch()
    qld_items = QLDParksAlertsAdapter(timeout_s=20.0).fetch()
    wa_items = EmergencyWAAdapter(timeout_s=20.0).fetch()
    sa_items = SAAlertAdapter(timeout_s=20.0).fetch()
    nt_items = NTFireParksAdapter(timeout_s=20.0).fetch()

    for expected_state, items in (
        ("ACT", act_items),
        ("TAS", tas_items),
        ("QLD", qld_items),
        ("WA", wa_items),
        ("SA", sa_items),
        ("NT", nt_items),
    ):
        assert isinstance(items, list)
        assert all(item.state == expected_state for item in items)

    # Road Report NT publishes persistent active outback road closures & 4WD advisories
    assert len(nt_items) > 0
