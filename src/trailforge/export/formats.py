"""GridForge-compatible GPX 1.1 and GeoJSON exporters for tracks, favorites, and personal pins."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from trailforge.ingestion.connectors.base import (
    ATTRIBUTION_CAPAD,
    ATTRIBUTION_GA_DEM,
    ATTRIBUTION_NSW_SPATIAL,
    ATTRIBUTION_OSM,
    ATTRIBUTION_VIC_DEECA,
)

GPX_NS = "http://www.topografix.com/GPX/1/1"
EXPORT_ATTRIBUTIONS = [
    ATTRIBUTION_OSM,
    ATTRIBUTION_GA_DEM,
    ATTRIBUTION_CAPAD,
    ATTRIBUTION_NSW_SPATIAL,
    ATTRIBUTION_VIC_DEECA,
]


@dataclass(frozen=True)
class ExportPin:
    """Single waypoint/POI or user pin for GPX/GeoJSON export."""

    name: str
    lon: float
    lat: float
    category: str = "waypoint"
    elev_m: float | None = None
    notes: str | None = None
    source: str = "user_pin"


@dataclass(frozen=True)
class ExportTrackData:
    """Ordered track polyline & metadata ready for GPX 1.1 or GeoJSON serialization."""

    track_id: str
    name: str
    state: str
    grade: str | None
    total_length_m: float
    coords_lonlat_ele: list[tuple[float, float, float | None]]
    description: str | None = None
    has_osm_source: bool = True
    notes: str | None = None
    extra_props: dict[str, Any] = field(default_factory=dict)


def build_gpx_document(
    *,
    title: str,
    tracks: list[ExportTrackData],
    waypoints: list[ExportPin],
    generated_at: datetime | None = None,
) -> str:
    """Serialize tracks and waypoints into standards-compliant GPX 1.1 XML.

    Preserves required OSM ODbL & Australian CC BY 4.0 attributions in `<metadata>`
    and emits standard `<wpt>` + `<trk><trkseg><trkpt>` compatible with GridForge.
    """
    ET.register_namespace("", GPX_NS)
    root = ET.Element(
        f"{{{GPX_NS}}}gpx",
        attrib={
            "version": "1.1",
            "creator": "TrailForge (GPL-3.0-or-later)",
        },
    )

    ts = (generated_at or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = ET.SubElement(root, f"{{{GPX_NS}}}metadata")
    ET.SubElement(meta, f"{{{GPX_NS}}}name").text = title
    ET.SubElement(meta, f"{{{GPX_NS}}}desc").text = " | ".join(EXPORT_ATTRIBUTIONS)
    ET.SubElement(meta, f"{{{GPX_NS}}}time").text = ts

    for wp in waypoints:
        wpt = ET.SubElement(
            root,
            f"{{{GPX_NS}}}wpt",
            attrib={"lat": f"{wp.lat:.6f}", "lon": f"{wp.lon:.6f}"},
        )
        if wp.elev_m is not None:
            ET.SubElement(wpt, f"{{{GPX_NS}}}ele").text = f"{wp.elev_m:.2f}"
        ET.SubElement(wpt, f"{{{GPX_NS}}}name").text = wp.name
        if wp.notes:
            ET.SubElement(wpt, f"{{{GPX_NS}}}desc").text = wp.notes
        ET.SubElement(wpt, f"{{{GPX_NS}}}type").text = wp.category
        ET.SubElement(wpt, f"{{{GPX_NS}}}sym").text = wp.category

    for trk_data in tracks:
        trk = ET.SubElement(root, f"{{{GPX_NS}}}trk")
        ET.SubElement(trk, f"{{{GPX_NS}}}name").text = trk_data.name
        desc_parts = [f"Track ID: {trk_data.track_id}", f"State: {trk_data.state}"]
        if trk_data.grade:
            desc_parts.append(f"Grade: {trk_data.grade}")
        desc_parts.append(f"Length: {trk_data.total_length_m / 1000.0:.2f} km")
        if trk_data.notes:
            desc_parts.append(f"Notes: {trk_data.notes}")
        elif trk_data.description:
            desc_parts.append(trk_data.description)
        ET.SubElement(trk, f"{{{GPX_NS}}}desc").text = " | ".join(desc_parts)

        seg = ET.SubElement(trk, f"{{{GPX_NS}}}trkseg")
        for lon, lat, ele in trk_data.coords_lonlat_ele:
            trkpt = ET.SubElement(
                seg,
                f"{{{GPX_NS}}}trkpt",
                attrib={"lat": f"{lat:.6f}", "lon": f"{lon:.6f}"},
            )
            if ele is not None:
                ET.SubElement(trkpt, f"{{{GPX_NS}}}ele").text = f"{ele:.2f}"

    ET.indent(root, space="  ")
    xml_body = ET.tostring(root, encoding="unicode", xml_declaration=True)
    return xml_body + "\n"


def _track_to_geojson_feature(trk: ExportTrackData) -> dict[str, Any]:
    coords = [
        [round(lon, 6), round(lat, 6)]
        if ele is None
        else [round(lon, 6), round(lat, 6), round(ele, 2)]
        for lon, lat, ele in trk.coords_lonlat_ele
    ]
    props: dict[str, Any] = {
        "feature_kind": "track",
        "track_id": trk.track_id,
        "name": trk.name,
        "state": trk.state,
        "grade": trk.grade,
        "total_length_m": trk.total_length_m,
        "has_osm_source": trk.has_osm_source,
    }
    if trk.notes:
        props["notes"] = trk.notes
    props.update(trk.extra_props)
    return {
        "type": "Feature",
        "id": trk.track_id,
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": props,
    }


def _pin_to_geojson_feature(pin: ExportPin) -> dict[str, Any]:
    coords = (
        [round(pin.lon, 6), round(pin.lat, 6)]
        if pin.elev_m is None
        else [round(pin.lon, 6), round(pin.lat, 6), round(pin.elev_m, 2)]
    )
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": coords},
        "properties": {
            "feature_kind": pin.source,
            "name": pin.name,
            "category": pin.category,
            "elev_m": pin.elev_m,
            "notes": pin.notes,
        },
    }


def build_track_geojson(
    track: ExportTrackData,
    nearby_pois: list[ExportPin],
) -> dict[str, Any]:
    """Serialize a single track and nearby POIs into a GridForge-compatible FeatureCollection."""
    features: list[dict[str, Any]] = []
    if len(track.coords_lonlat_ele) >= 2:
        features.append(_track_to_geojson_feature(track))
    features.extend(_pin_to_geojson_feature(p) for p in nearby_pois)
    return {
        "type": "FeatureCollection",
        "name": track.name,
        "metadata": {
            "generator": "TrailForge",
            "attributions": EXPORT_ATTRIBUTIONS,
        },
        "features": features,
    }


def build_user_collection_geojson(
    *,
    owner_display_name: str,
    tracks: list[ExportTrackData],
    pins: list[ExportPin],
) -> dict[str, Any]:
    """Serialize a user's saved favorite tracks and personal pins into a FeatureCollection."""
    features: list[dict[str, Any]] = [
        _track_to_geojson_feature(t) for t in tracks if len(t.coords_lonlat_ele) >= 2
    ]
    features.extend(_pin_to_geojson_feature(p) for p in pins)
    return {
        "type": "FeatureCollection",
        "name": f"{owner_display_name} — TrailForge Saved Routes & Pins",
        "metadata": {
            "generator": "TrailForge",
            "attributions": EXPORT_ATTRIBUTIONS,
        },
        "features": features,
    }
