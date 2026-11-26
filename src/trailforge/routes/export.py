"""GridForge-compatible GPX 1.1 and RFC 7946 GeoJSON exporters for planned multi-track routes."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from trailforge.export.formats import EXPORT_ATTRIBUTIONS, GPX_NS
from trailforge.grading.rainfall import OPEN_METEO_ATTRIBUTION
from trailforge.ingestion.connectors.base import ATTRIBUTION_GA_HYDRO
from trailforge.routes.planner import OrientedLegTrack, PlannedRouteAnalysis

ROUTE_EXPORT_ATTRIBUTIONS: Final[list[str]] = [
    *EXPORT_ATTRIBUTIONS,
    ATTRIBUTION_GA_HYDRO,
    OPEN_METEO_ATTRIBUTION,
]

_POI_EXPORT_PRIORITY: Final[frozenset[str]] = frozenset(
    {"campground", "hut", "water_point", "trig", "lookout", "gate", "trailhead", "fuel", "picnic"}
)


@dataclass(frozen=True, slots=True)
class _RouteExportWaypoint:
    name: str
    lon: float
    lat: float
    category: str
    feature_kind: str
    elev_m: float | None = None
    notes: str | None = None


def _collect_route_waypoints(
    analysis: PlannedRouteAnalysis,
    leg_tracks: Sequence[OrientedLegTrack] | None = None,
) -> list[_RouteExportWaypoint]:
    """Assemble deduplicated custom waypoints, nearby camps/huts, crossings, closures & gates."""
    items: list[_RouteExportWaypoint] = []
    seen_keys: set[tuple[str, int, int]] = set()

    def _add(wp: _RouteExportWaypoint) -> None:
        key = (wp.name.strip().lower(), round(wp.lon * 10_000), round(wp.lat * 10_000))
        if key in seen_keys:
            return
        seen_keys.add(key)
        items.append(wp)

    # 1. Custom route waypoints
    for wp in analysis.waypoints:
        _add(
            _RouteExportWaypoint(
                name=wp.name,
                lon=wp.lon,
                lat=wp.lat,
                category=wp.category or "waypoint",
                feature_kind="waypoint",
                notes=f"Custom route waypoint ({wp.category})",
            )
        )

    # 2. Nearby backcountry POIs (camps, huts, trigs, water points, gates)
    for poi in analysis.nearby_pois:
        if poi.poi_type not in _POI_EXPORT_PRIORITY:
            continue
        desc_parts = [poi.poi_type.replace("_", " ").title()]
        if poi.distance_m is not None:
            desc_parts.append(f"{poi.distance_m:.0f} m from route")
        if poi.access_note:
            desc_parts.append(poi.access_note)
        _add(
            _RouteExportWaypoint(
                name=poi.name,
                lon=poi.lon,
                lat=poi.lat,
                category=poi.poi_type,
                feature_kind="poi",
                elev_m=poi.elev_m,
                notes=" | ".join(desc_parts),
            )
        )

    # 3. Waterway crossings along route with cumulative distance
    for crossing in analysis.waterway_crossings:
        if crossing.lon is None or crossing.lat is None:
            continue
        kind_label = "Bridged" if crossing.bridged else "Unbridged Ford"
        perenn_label = "Perennial" if crossing.perennial else "Intermittent"
        _add(
            _RouteExportWaypoint(
                name=f"{crossing.name} ({kind_label})",
                lon=crossing.lon,
                lat=crossing.lat,
                category="crossing",
                feature_kind="waterway_crossing",
                notes=(
                    f"km {crossing.dist_m / 1000.0:.2f} on {crossing.track_name} | "
                    f"{perenn_label} {crossing.waterway_type}"
                ),
            )
        )

    # 4. Community gate / private-property warnings
    for gw in analysis.community_gate_warnings:
        lon = gw.get("lon")
        lat = gw.get("lat")
        if lon is None or lat is None:
            continue
        title = str(gw.get("title") or gw.get("report_type_label") or "Locked Gate Warning")
        notes = str(gw.get("notes") or gw.get("description") or "Community gate warning")
        _add(
            _RouteExportWaypoint(
                name=f"[Gate] {title}",
                lon=float(lon),
                lat=float(lat),
                category="gate",
                feature_kind="gate_warning",
                notes=notes,
            )
        )

    # 5. Official closures / alerts (use alert centroid or fallback to affected leg midpoint)
    leg_mid_by_tid: dict[str, tuple[float, float]] = {}
    for lt in leg_tracks or []:
        if lt.coords_lonlat_ele and lt.track_id not in leg_mid_by_tid:
            mid = lt.coords_lonlat_ele[len(lt.coords_lonlat_ele) // 2]
            leg_mid_by_tid[lt.track_id] = (mid[0], mid[1])

    for closure in analysis.closures:
        lon = closure.get("lon")
        lat = closure.get("lat")
        if lon is None or lat is None:
            for tid in closure.get("affected_track_ids") or []:
                if tid in leg_mid_by_tid:
                    lon, lat = leg_mid_by_tid[tid]
                    break
        if lon is None or lat is None:
            continue
        c_title = str(closure.get("title") or "Official Closure")
        agency = str(closure.get("agency") or closure.get("feed_source") or "Agency")
        c_type = str(closure.get("closure_type") or "closure")
        _add(
            _RouteExportWaypoint(
                name=f"[Closure] {c_title}",
                lon=float(lon),
                lat=float(lat),
                category="hazard",
                feature_kind="closure",
                notes=f"{agency} ({c_type}) — {closure.get('description') or c_title}",
            )
        )

    return items


def _resolve_leg_coords(
    analysis: PlannedRouteAnalysis,
    leg_tracks: Sequence[OrientedLegTrack] | None,
) -> list[tuple[str, list[tuple[float, float, float | None]]]]:
    """Extract per-leg `(track_name, coords_lonlat_ele)` from `leg_tracks` or GeoJSON fallback."""
    if leg_tracks is not None:
        return [
            (lt.name, list(lt.coords_lonlat_ele))
            for lt in leg_tracks
            if len(lt.coords_lonlat_ele) >= 2
        ]

    # Fallback: reconstruct from analysis.geometry features
    out: list[tuple[str, list[tuple[float, float, float | None]]]] = []
    for feat in (analysis.geometry or {}).get("features") or []:
        geom = feat.get("geometry") or {}
        if geom.get("type") != "LineString":
            continue
        coords_raw = geom.get("coordinates") or []
        parsed: list[tuple[float, float, float | None]] = [
            (float(c[0]), float(c[1]), float(c[2]) if len(c) >= 3 else None)
            for c in coords_raw
            if isinstance(c, (list, tuple)) and len(c) >= 2
        ]
        if len(parsed) >= 2:
            props = feat.get("properties") or {}
            out.append((str(props.get("name") or analysis.name), parsed))
    return out


def build_route_gpx(
    analysis: PlannedRouteAnalysis,
    *,
    leg_tracks: Sequence[OrientedLegTrack] | None = None,
    notes: str | None = None,
    generated_at: datetime | None = None,
) -> str:
    """Serialize a planned multi-track route into GridForge-compatible GPX 1.1 XML.

    Emits a single `<trk>` containing one `<trkseg>` per oriented leg plus `<wpt>` pins
    for custom waypoints, nearby camps/huts, waterway crossings, closures, and gate warnings.
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
    ET.SubElement(meta, f"{{{GPX_NS}}}name").text = analysis.name
    attributions = analysis.attributions or ROUTE_EXPORT_ATTRIBUTIONS
    ET.SubElement(meta, f"{{{GPX_NS}}}desc").text = " | ".join(attributions)
    ET.SubElement(meta, f"{{{GPX_NS}}}time").text = ts
    if len(analysis.bbox) == 4:
        w, s, e, n = analysis.bbox
        ET.SubElement(
            meta,
            f"{{{GPX_NS}}}bounds",
            attrib={
                "minlat": f"{s:.6f}",
                "minlon": f"{w:.6f}",
                "maxlat": f"{n:.6f}",
                "maxlon": f"{e:.6f}",
            },
        )

    waypoints = _collect_route_waypoints(analysis, leg_tracks=leg_tracks)
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

    # Single `<trk>` with one `<trkseg>` per leg for seamless GridForge `--strip-route`
    trk = ET.SubElement(root, f"{{{GPX_NS}}}trk")
    ET.SubElement(trk, f"{{{GPX_NS}}}name").text = analysis.name

    desc_parts = [
        f"Legs: {len(analysis.legs)} ({' -> '.join(leg.name for leg in analysis.legs)})",
        f"Length: {analysis.total_length_m / 1000.0:.2f} km",
        f"Est. Time: {analysis.est_duration_min:.0f} min",
        f"Worst Grade: {analysis.worst_grade} ({analysis.worst_score:.1f}/100)",
        f"Climb/Drop: +{analysis.elev_gain_m:.0f} m / -{analysis.elev_loss_m:.0f} m",
    ]
    if analysis.suitability is not None:
        desc_parts.append(
            f"Suitability ({analysis.suitability.vehicle_class}): {analysis.suitability.rating}"
        )
    if analysis.safety_summary.has_blocking_closure:
        desc_parts.append(f"ALERT: {analysis.safety_summary.closure_count} active closure(s)")
    if notes:
        desc_parts.append(f"Notes: {notes.strip()}")
    ET.SubElement(trk, f"{{{GPX_NS}}}desc").text = " | ".join(desc_parts)

    leg_segments = _resolve_leg_coords(analysis, leg_tracks)
    for _leg_name, coords in leg_segments:
        seg_el = ET.SubElement(trk, f"{{{GPX_NS}}}trkseg")
        for lon, lat, ele in coords:
            trkpt = ET.SubElement(
                seg_el,
                f"{{{GPX_NS}}}trkpt",
                attrib={"lat": f"{lat:.6f}", "lon": f"{lon:.6f}"},
            )
            if ele is not None:
                ET.SubElement(trkpt, f"{{{GPX_NS}}}ele").text = f"{ele:.2f}"

    ET.indent(root, space="  ")
    xml_body = ET.tostring(root, encoding="unicode", xml_declaration=True)
    return xml_body + "\n"


def build_route_geojson(
    analysis: PlannedRouteAnalysis,
    *,
    leg_tracks: Sequence[OrientedLegTrack] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Serialize a planned multi-track route into an RFC 7946 GeoJSON FeatureCollection."""
    features: list[dict[str, Any]] = []

    if leg_tracks is not None:
        for lt in leg_tracks:
            if len(lt.coords_lonlat_ele) < 2:
                continue
            coords = [
                [round(lon, 6), round(lat, 6)]
                if ele is None
                else [round(lon, 6), round(lat, 6), round(ele, 2)]
                for lon, lat, ele in lt.coords_lonlat_ele
            ]
            features.append(
                {
                    "type": "Feature",
                    "id": f"leg-{lt.index}-{lt.track_id}",
                    "geometry": {"type": "LineString", "coordinates": coords},
                    "properties": {
                        "feature_kind": "route_leg",
                        "leg_index": lt.index,
                        "track_id": lt.track_id,
                        "name": lt.name,
                        "state": lt.state,
                        "grade": lt.grade,
                        "score": lt.score,
                        "length_m": lt.length_m,
                        "closure_status": lt.closure_status,
                        "reversed": lt.reversed,
                        "gap_from_prev_m": lt.gap_from_prev_m,
                    },
                }
            )
    else:
        for feat in (analysis.geometry or {}).get("features") or []:
            features.append(dict(feat))

    waypoints = _collect_route_waypoints(analysis, leg_tracks=leg_tracks)
    for wp in waypoints:
        coords_pt = (
            [round(wp.lon, 6), round(wp.lat, 6)]
            if wp.elev_m is None
            else [round(wp.lon, 6), round(wp.lat, 6), round(wp.elev_m, 2)]
        )
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": coords_pt},
                "properties": {
                    "feature_kind": wp.feature_kind,
                    "name": wp.name,
                    "category": wp.category,
                    "elev_m": wp.elev_m,
                    "notes": wp.notes,
                },
            }
        )

    return {
        "type": "FeatureCollection",
        "name": analysis.name,
        "bbox": analysis.bbox,
        "metadata": {
            "generator": "TrailForge (GPL-3.0-or-later)",
            "route_name": analysis.name,
            "notes": notes.strip() if notes else None,
            "track_ids": analysis.track_ids,
            "total_length_m": analysis.total_length_m,
            "est_duration_min": analysis.est_duration_min,
            "worst_grade": analysis.worst_grade,
            "worst_score": analysis.worst_score,
            "safety_summary": analysis.safety_summary.model_dump(),
            "gridforge_cli": analysis.gridforge_cli,
            "attributions": analysis.attributions or list(ROUTE_EXPORT_ATTRIBUTIONS),
        },
        "features": features,
    }
