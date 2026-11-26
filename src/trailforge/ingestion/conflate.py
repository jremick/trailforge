"""Multi-source Pan-Australia track conflation engine (OSM > Official AU State Layers) in EPSG:3577.

Implements docs/SPEC_GIS_SCHEMA.md Section 3.1 across all 8 Australian states & territories:
  - Filters non-vehicular walking tracks out of the 4WD conflated network
  - Deduplicates overlapping lower-priority geometries within 20 m corridor (>=70% overlap,
    <=30 deg acute heading diff), attaching state provenance, names, classes, and surface hints
  - Cuts partial overlaps (20%..70%), discards slivers <35 m, and snaps T-junction endpoints
    within 20 m onto the winning higher-priority line
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
from pyproj import Transformer
from shapely import STRtree
from shapely import transform as shapely_transform
from shapely.geometry import LineString, MultiLineString, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import nearest_points, unary_union

SourceKind = Literal["osm", "vic", "nsw", "act", "tas", "qld", "wa", "sa", "nt"]

_TO_ALBERS = Transformer.from_crs("EPSG:4326", "EPSG:3577", always_xy=True).transform
_TO_WGS84 = Transformer.from_crs("EPSG:3577", "EPSG:4326", always_xy=True).transform

# Conflation thresholds locked in docs/SPEC_GIS_SCHEMA.md Section 3.1
CONFLATION_BUFFER_M = 20.0
FULL_DUPLICATE_OVERLAP = 0.70
PARTIAL_OVERLAP_MIN = 0.20
MAX_HEADING_DIFF_DEG = 30.0
MIN_RESIDUAL_SLIVER_M = 35.0
T_JUNCTION_SNAP_M = 20.0

_SOURCE_PRIORITY: dict[tuple[str, str], int] = {
    ("osm", "osm"): 1,
    ("vic", "tr_road"): 2,
    ("vic", "recweb_tracks"): 3,
    ("nsw", "nsw_fire_trails_l9"): 4,
    ("nsw", "nsw_tracks_l5"): 5,
    ("act", "act_transport_track"): 6,
    ("act", "act_rural_roads"): 7,
    ("act", "act_equestrian_routes"): 8,
    ("tas", "tas_transport_segments"): 9,
    ("qld", "qld_qpws_park_roads"): 10,
    ("qld", "qld_qpws_walking"): 11,
    ("qld", "qld_qpws_great_walks"): 11,
    ("qld", "qld_state_tracks"): 12,
    ("wa", "wa_slip_tracks"): 13,
    ("wa", "wa_dbca_tracks"): 14,
    ("sa", "sa_fire_tracks"): 15,
    ("sa", "sa_recreation_trails"): 16,
    ("sa", "sa_ga_topo250k"): 17,
    ("nt", "nt_roads"): 18,
    ("nt", "nt_ga_topo250k"): 19,
}

_SOURCE_FALLBACK_PRIORITY: dict[str, int] = {
    "osm": 1,
    "vic": 3,
    "nsw": 5,
    "act": 8,
    "tas": 9,
    "qld": 12,
    "wa": 14,
    "sa": 17,
    "nt": 19,
}

_PLACEHOLDER_NAMES = frozenset({"", "unnamed", "unknown", "none", "null", "n/a"})


def _apply_transformer(geom: BaseGeometry, fn: Any) -> BaseGeometry:
    def _vec(coords: np.ndarray) -> np.ndarray:
        xs, ys = fn(coords[:, 0], coords[:, 1])
        return np.column_stack((xs, ys))

    return shapely_transform(geom, _vec)


def to_albers(geom: BaseGeometry) -> BaseGeometry:
    """Transform a Shapely geometry from EPSG:4326 to EPSG:3577 (Australian Albers meters)."""
    return _apply_transformer(geom, _TO_ALBERS)


def to_wgs84(geom: BaseGeometry) -> BaseGeometry:
    """Transform a Shapely geometry from EPSG:3577 to EPSG:4326."""
    return _apply_transformer(geom, _TO_WGS84)


def clean_name(name: str | None) -> str | None:
    """Normalize placeholder names to None."""
    if name is None:
        return None
    stripped = " ".join(name.strip().split())
    if stripped.lower() in _PLACEHOLDER_NAMES:
        return None
    return stripped


def compute_bearing_deg(line_3577: LineString) -> float:
    """Compute forward compass bearing [0, 360) from start to end of a metric LineString."""
    coords = list(line_3577.coords)
    if len(coords) < 2:
        return 0.0
    dx = coords[-1][0] - coords[0][0]
    dy = coords[-1][1] - coords[0][1]
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        # Closed loop fallback: use first non-degenerate chord
        for x, y in coords[1:]:
            dx = x - coords[0][0]
            dy = y - coords[0][1]
            if abs(dx) >= 1e-9 or abs(dy) >= 1e-9:
                break
        else:
            return 0.0
    angle = math.degrees(math.atan2(dx, dy)) % 360.0
    return 0.0 if angle >= 360.0 else angle


def acute_heading_diff_deg(bearing_a: float, bearing_b: float) -> float:
    """Return bidirectional acute angle in [0, 90] degrees between two line bearings."""
    diff = abs((bearing_a % 180.0) - (bearing_b % 180.0))
    if diff > 90.0:
        diff = 180.0 - diff
    return diff


def _local_tangent_deg(line_3577: LineString, dist_m: float) -> float:
    """Estimate local tangent bearing [0, 360) around a distance along a metric LineString."""
    total = line_3577.length
    if total <= 1e-6:
        return 0.0
    delta = min(15.0, total * 0.25)
    d0 = max(0.0, dist_m - delta)
    d1 = min(total, dist_m + delta)
    if d1 - d0 <= 1e-6:
        return compute_bearing_deg(line_3577)
    p0 = line_3577.interpolate(d0)
    p1 = line_3577.interpolate(d1)
    return math.degrees(math.atan2(p1.x - p0.x, p1.y - p0.y)) % 360.0


def overlap_heading_diff_deg(line_s_3577: LineString, line_p_3577: LineString) -> float:
    """Compute median local bidirectional acute heading diff along overlapping portion."""
    buf = line_p_3577.buffer(CONFLATION_BUFFER_M)
    inter = line_s_3577.intersection(buf)
    if inter.is_empty or inter.length <= 1e-6:
        return acute_heading_diff_deg(
            compute_bearing_deg(line_s_3577), compute_bearing_deg(line_p_3577)
        )

    samples: list[float] = []
    for part in _extract_linestrings(inter):
        if part.length < 1.0:
            continue
        n_pts = max(1, min(7, int(part.length // 80) + 1))
        for k in range(n_pts):
            frac = (k + 0.5) / n_pts
            pt = part.interpolate(frac, normalized=True)
            ds = line_s_3577.project(pt)
            dp = line_p_3577.project(pt)
            bs = _local_tangent_deg(line_s_3577, ds)
            bp = _local_tangent_deg(line_p_3577, dp)
            samples.append(acute_heading_diff_deg(bs, bp))

    if not samples:
        return acute_heading_diff_deg(
            compute_bearing_deg(line_s_3577), compute_bearing_deg(line_p_3577)
        )
    samples.sort()
    return samples[len(samples) // 2]


def _extract_linestrings(geom: BaseGeometry) -> list[LineString]:
    if geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom] if len(geom.coords) >= 2 and geom.length > 0 else []
    if isinstance(geom, MultiLineString):
        return [g for g in geom.geoms if len(g.coords) >= 2 and g.length > 0]
    if hasattr(geom, "geoms"):
        out: list[LineString] = []
        for sub in geom.geoms:
            out.extend(_extract_linestrings(sub))
        return out
    return []


def _to_shapely(geom_obj: Any) -> BaseGeometry:
    if isinstance(geom_obj, BaseGeometry):
        return geom_obj
    if isinstance(geom_obj, dict):
        return shape(geom_obj)
    raise TypeError(f"Unsupported geometry type: {type(geom_obj)!r}")


def _priority_rank(source: str, source_layer: str) -> int:
    if (source, source_layer) in _SOURCE_PRIORITY:
        return _SOURCE_PRIORITY[(source, source_layer)]
    return _SOURCE_FALLBACK_PRIORITY.get(source, 99)


@dataclass
class ConflatedWay:
    """Single vehicular LineString surviving multi-source conflation."""

    geom_3577: LineString
    geom_wgs84: LineString
    primary_source: SourceKind
    has_osm_source: bool
    name: str | None
    alt_names: list[str] = field(default_factory=list)
    trail_class: str = "4wd_track"
    is_vehicular: bool = True
    surface_class: str = "unknown"
    tracktype: str | None = None
    smoothness: str | None = None
    is_4wd_only: bool = False
    is_4wd_recommended: bool = False
    access_status: str = "public"
    has_gate_barrier: bool = False
    is_bridged: bool = False
    is_ford: bool = False
    source_refs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ConflationResult:
    """Output of multi-source conflation with audit counters."""

    ways: list[ConflatedWay]
    deduplicated_count: int = 0
    partial_trimmed_count: int = 0
    dropped_non_vehicular_count: int = 0
    dropped_sliver_count: int = 0


def _snap_residual_endpoints(
    residual: LineString, targets: list[LineString], snap_m: float = T_JUNCTION_SNAP_M
) -> LineString:
    """Snap start/end vertices of a partial residual within snap_m onto winning target lines."""
    coords = list(residual.coords)
    if len(coords) < 2 or not targets:
        return residual

    target_union = unary_union(targets)
    from shapely.geometry import Point

    for idx in (0, -1):
        pt = Point(coords[idx])
        dist = pt.distance(target_union)
        if 0.0 < dist <= snap_m:
            _, snapped = nearest_points(pt, target_union)
            coords[idx] = (snapped.x, snapped.y)
    return LineString(coords)


def _merge_metadata(winner: ConflatedWay, source_ref: dict[str, Any], feat: Any) -> None:
    """Enrich winning higher-priority way with non-conflicting state provenance/names/class."""
    if source_ref not in winner.source_refs:
        winner.source_refs.append(source_ref)

    other_name = clean_name(getattr(feat, "name", None))
    if other_name:
        if not winner.name:
            winner.name = other_name
        elif other_name.lower() != winner.name.lower() and all(
            a.lower() != other_name.lower() for a in winner.alt_names
        ):
            winner.alt_names.append(other_name)

    other_class = getattr(feat, "trail_class", None)
    if (
        other_class in ("fire_trail", "management_trail")
        and winner.trail_class == "4wd_track"
        and not winner.is_4wd_only
    ):
        winner.trail_class = other_class

    other_surface = getattr(feat, "surface_class", "unknown") or "unknown"
    if winner.surface_class == "unknown" and other_surface != "unknown":
        winner.surface_class = other_surface

    if getattr(feat, "has_gate_barrier", False):
        winner.has_gate_barrier = True


def conflate_tracks(features: Sequence[Any]) -> ConflationResult:
    """Conflate OSM and official AU state track features in EPSG:3577 (OSM > official state layers).

    Accepts any sequence of track feature objects exposing attributes matching
    `RawTrackFeature` (`source`, `source_layer`, `source_id`, `name`, `trail_class`,
    `is_vehicular`, `surface_class`, `tracktype`, `smoothness`, `is_4wd_only`,
    `is_4wd_recommended`, `access_status`, `has_gate_barrier`, `is_bridged`,
    `is_ford`, `geometry`).
    """
    vehicular_items: list[tuple[int, int, Any, LineString]] = []
    dropped_non_vehicular = 0

    for idx, feat in enumerate(features):
        is_vehicular = bool(getattr(feat, "is_vehicular", True))
        trail_class = getattr(feat, "trail_class", "4wd_track")
        if not is_vehicular or trail_class == "walking_track":
            dropped_non_vehicular += 1
            continue

        raw_geom = _to_shapely(getattr(feat, "geometry", None))
        source = feat.source
        source_layer = getattr(feat, "source_layer", source)
        rank = _priority_rank(source, source_layer)

        for part_wgs84 in _extract_linestrings(raw_geom):
            part_3577 = to_albers(part_wgs84)
            if isinstance(part_3577, LineString) and part_3577.length >= 1.0:
                vehicular_items.append((rank, idx, feat, part_3577))

    # Deterministic priority order: OSM (1) > VIC tr_road (2) > VIC recweb (3) > NSW L9 (4) > L5 (5)
    vehicular_items.sort(key=lambda item: (item[0], item[1]))

    accepted: list[ConflatedWay] = []
    accepted_geoms: list[LineString] = []
    accepted_ranks: list[int] = []

    deduplicated_count = 0
    partial_trimmed_count = 0
    dropped_sliver_count = 0

    for rank, _idx, feat, line_3577 in vehicular_items:
        source: SourceKind = feat.source
        source_layer = getattr(feat, "source_layer", source)
        source_id = str(feat.source_id)
        feat_name = clean_name(getattr(feat, "name", None))
        source_ref: dict[str, Any] = {
            "source": source,
            "source_layer": source_layer,
            "source_id": source_id,
        }
        if feat_name:
            source_ref["name"] = feat_name
        feat_class = getattr(feat, "trail_class", None)
        if feat_class:
            source_ref["trail_class"] = feat_class

        # Only deduplicate against strictly higher-priority sources (VIC vs OSM, NSW vs OSM/VIC)
        higher_indices = [i for i, r in enumerate(accepted_ranks) if r < rank]
        matching_higher: list[tuple[int, float]] = []

        if higher_indices:
            higher_geoms = [accepted_geoms[i] for i in higher_indices]
            tree = STRtree(higher_geoms)
            cand_positions = tree.query(line_3577.buffer(CONFLATION_BUFFER_M))
            for pos in cand_positions:
                acc_idx = higher_indices[int(pos)]
                acc_geom = accepted_geoms[acc_idx]
                if line_3577.distance(acc_geom) > CONFLATION_BUFFER_M:
                    continue
                dtheta = overlap_heading_diff_deg(line_3577, acc_geom)
                if dtheta <= MAX_HEADING_DIFF_DEG:
                    inter_len = line_3577.intersection(acc_geom.buffer(CONFLATION_BUFFER_M)).length
                    min_sig_overlap = max(
                        MIN_RESIDUAL_SLIVER_M,
                        0.15 * min(line_3577.length, acc_geom.length),
                    )
                    if inter_len >= min_sig_overlap:
                        matching_higher.append((acc_idx, inter_len))

        if matching_higher:
            union_buf = unary_union(
                [accepted_geoms[i].buffer(CONFLATION_BUFFER_M) for i, _ in matching_higher]
            )
            overlap_ratio = line_3577.intersection(union_buf).length / max(line_3577.length, 1e-6)

            if overlap_ratio >= FULL_DUPLICATE_OVERLAP:
                deduplicated_count += 1
                # Attach provenance & name enrichment to EVERY overlapping higher-priority way
                for acc_idx, _ in sorted(matching_higher, key=lambda x: -x[1]):
                    _merge_metadata(accepted[acc_idx], source_ref, feat)
                continue

            if overlap_ratio >= PARTIAL_OVERLAP_MIN:
                partial_trimmed_count += 1
                for acc_idx, _ in sorted(matching_higher, key=lambda x: -x[1]):
                    _merge_metadata(accepted[acc_idx], source_ref, feat)
                diff_geom = line_3577.difference(union_buf)
                target_lines = [accepted_geoms[i] for i, _ in matching_higher]
                for residual in _extract_linestrings(diff_geom):
                    if residual.length < MIN_RESIDUAL_SLIVER_M:
                        dropped_sliver_count += 1
                        continue
                    snapped = _snap_residual_endpoints(residual, target_lines, T_JUNCTION_SNAP_M)
                    wgs84 = to_wgs84(snapped)
                    assert isinstance(wgs84, LineString)
                    way = _make_conflated_way(feat, snapped, wgs84, source, source_ref)
                    accepted.append(way)
                    accepted_geoms.append(snapped)
                    accepted_ranks.append(rank)
                continue

        wgs84_geom = to_wgs84(line_3577)
        assert isinstance(wgs84_geom, LineString)
        way = _make_conflated_way(feat, line_3577, wgs84_geom, source, source_ref)
        accepted.append(way)
        accepted_geoms.append(line_3577)
        accepted_ranks.append(rank)

    return ConflationResult(
        ways=accepted,
        deduplicated_count=deduplicated_count,
        partial_trimmed_count=partial_trimmed_count,
        dropped_non_vehicular_count=dropped_non_vehicular,
        dropped_sliver_count=dropped_sliver_count,
    )


def _make_conflated_way(
    feat: Any,
    geom_3577: LineString,
    geom_wgs84: LineString,
    source: SourceKind,
    source_ref: dict[str, Any],
) -> ConflatedWay:
    return ConflatedWay(
        geom_3577=geom_3577,
        geom_wgs84=geom_wgs84,
        primary_source=source,
        has_osm_source=(source == "osm"),
        name=clean_name(getattr(feat, "name", None)),
        alt_names=[],
        trail_class=getattr(feat, "trail_class", "4wd_track"),
        is_vehicular=True,
        surface_class=getattr(feat, "surface_class", "unknown") or "unknown",
        tracktype=getattr(feat, "tracktype", None),
        smoothness=getattr(feat, "smoothness", None),
        is_4wd_only=bool(getattr(feat, "is_4wd_only", False)),
        is_4wd_recommended=bool(getattr(feat, "is_4wd_recommended", False)),
        access_status=getattr(feat, "access_status", "public") or "public",
        has_gate_barrier=bool(getattr(feat, "has_gate_barrier", False)),
        is_bridged=bool(getattr(feat, "is_bridged", False)),
        is_ford=bool(getattr(feat, "is_ford", False)),
        source_refs=[source_ref],
    )
