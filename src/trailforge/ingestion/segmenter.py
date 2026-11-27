"""Canonical 50-100 m track segmentation & deterministic stable segment-ID minting.

Implements docs/SPEC_GIS_SCHEMA.md Sections 3.2 & 3.3.1:
  1. Topological noding at junctions in EPSG:3577 (1.0 m snap tolerance)
  2. Degree-2 continuation merge for identical attributes with <45 deg deflection
     (makes segmentation immune to arbitrary mid-way upstream splits)
  3. Canonical direction normalization (1e-5 deg lexicographic endpoints)
  4. Deterministic equal-length chunking in [50, 100] m for edges >100 m
     (preserving short connectors/spurs in [15, 100] m without dropping them)
  5. 5-anchor 1e-5 deg quantized geom_fingerprint -> deterministic tfseg_<16hex> ID
  6. Logical Track grouping with deterministic tftrk_<12hex> IDs
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from shapely import STRtree
from shapely.geometry import LineString, MultiPoint, Point, Polygon
from shapely.ops import substring, unary_union

from trailforge.ingestion.conflate import (
    ConflatedWay,
    compute_bearing_deg,
    to_albers,
    to_wgs84,
)

JUNCTION_SNAP_M = 1.0
JUNCTION_ENDPOINT_MARGIN_M = 6.0
MAX_MERGE_DEFLECTION_DEG = 45.0
MIN_SPUR_SLIVER_M = 15.0
TARGET_CHUNK_M = 75.0
MIN_CHUNK_M = 50.0
MAX_CHUNK_M = 100.0

_ANCHOR_FRACTIONS = (0.0, 0.25, 0.50, 0.75, 1.0)


@dataclass
class CanonicalSegmentCandidate:
    """Pre-reconciliation canonical 50-100 m segment with deterministic ID & topology keys."""

    segment_id: str
    geom_fingerprint: str
    geom_wgs84: LineString
    geom_3577: LineString
    start_node_key: str
    end_node_key: str
    length_m: float
    bearing_deg: float
    state: str
    name: str | None
    alt_names: list[str]
    trail_class: str
    is_vehicular: bool
    surface_class: str
    tracktype: str | None
    smoothness: str | None
    is_4wd_only: bool
    is_4wd_recommended: bool
    access_status: str
    has_gate_barrier: bool
    is_bridged: bool
    is_ford: bool
    primary_source: str
    has_osm_source: bool
    source_refs: list[dict[str, Any]]
    track_id: str | None = None
    segment_seq: int | None = None


@dataclass
class CanonicalTrackGroup:
    """Logical named trail grouping ordered canonical segments."""

    track_id: str
    name: str
    alt_names: list[str] = field(default_factory=list)
    state: str = "VIC"
    primary_source: str = "osm"
    has_osm_source: bool = False
    trail_class: str = "4wd_track"
    is_vehicular: bool = True
    total_length_m: float = 0.0
    bbox_geom: Polygon | None = None
    segment_ids: list[str] = field(default_factory=list)


def quantize_5(value: float) -> float:
    """Quantize a WGS84 degree coordinate to 1e-5 deg (~1.1 m)."""
    return round(value, 5)


def node_key_from_lonlat(lon: float, lat: float) -> str:
    """Format canonical 1e-5 deg junction node key for HMM topology adjacency."""
    return f"{quantize_5(lon):.5f},{quantize_5(lat):.5f}"


def normalize_direction_wgs84(line_wgs84: LineString) -> LineString:
    """Orient LineString so (q5(start.lon), q5(start.lat)) <= (q5(end.lon), q5(end.lat))."""
    coords = list(line_wgs84.coords)
    if len(coords) < 2:
        return line_wgs84

    p0 = (quantize_5(coords[0][0]), quantize_5(coords[0][1]))
    p1 = (quantize_5(coords[-1][0]), quantize_5(coords[-1][1]))
    if p0 > p1:
        return LineString(list(reversed(coords)))
    if p0 == p1:
        # Tie-break closed or near-closed loop using 25% vs 75% anchor points
        q25_pt = line_wgs84.interpolate(0.25, normalized=True)
        q75_pt = line_wgs84.interpolate(0.75, normalized=True)
        k25 = (quantize_5(q25_pt.x), quantize_5(q25_pt.y))
        k75 = (quantize_5(q75_pt.x), quantize_5(q75_pt.y))
        if k25 > k75:
            return LineString(list(reversed(coords)))
    return line_wgs84


def compute_geom_fingerprint_and_id(
    line_wgs84: LineString,
) -> tuple[LineString, LineString, str, str, str, str]:
    """Normalize geometry direction and compute 5-anchor fingerprint + deterministic segment_id.

    Returns:
        (norm_wgs84, norm_3577, geom_fingerprint, segment_id, start_node_key, end_node_key)
    """
    norm_wgs84 = normalize_direction_wgs84(line_wgs84)
    norm_3577 = to_albers(norm_wgs84)
    assert isinstance(norm_3577, LineString)

    anchors_str: list[str] = []
    anchor_lonlats: list[tuple[float, float]] = []
    for frac in _ANCHOR_FRACTIONS:
        pt_3577 = norm_3577.interpolate(frac, normalized=True)
        pt_wgs84 = to_wgs84(pt_3577)
        assert isinstance(pt_wgs84, Point)
        lon5 = quantize_5(pt_wgs84.x)
        lat5 = quantize_5(pt_wgs84.y)
        anchor_lonlats.append((lon5, lat5))
        anchors_str.append(f"{lon5:.5f},{lat5:.5f}")

    canonical = "v1|" + "|".join(anchors_str)
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    seg_hash = hashlib.sha256(f"trailforge:seg:v1|{fingerprint}".encode()).hexdigest()[:16]
    segment_id = f"tfseg_{seg_hash}"

    start_key = f"{anchor_lonlats[0][0]:.5f},{anchor_lonlats[0][1]:.5f}"
    end_key = f"{anchor_lonlats[-1][0]:.5f},{anchor_lonlats[-1][1]:.5f}"
    return norm_wgs84, norm_3577, fingerprint, segment_id, start_key, end_key


def compute_chunk_count(length_m: float) -> int:
    """Compute deterministic equal-length chunk count N keeping L/N strictly in [50, 100] m."""
    if length_m <= MAX_CHUNK_M:
        return 1
    n_min = math.ceil(length_m / MAX_CHUNK_M)
    n_max = math.floor(length_m / MIN_CHUNK_M)
    n_target = round(length_m / TARGET_CHUNK_M)
    return min(n_max, max(n_min, n_target))


def infer_state_from_lonlat(lon: float, lat: float) -> str:
    """Infer Australian state/territory (all 8 jurisdictions) from a WGS84 midpoint coordinate.

    Uses the Murray River border approximation for the Australian Alps corridor and
    falls back cleanly across all 8 Australian state/territory extents when CAPAD polygons
    aren't passed.
    """
    # Western Australia (west of 129.0 E)
    if lon < 129.0:
        return "WA"
    # Tasmania (south of Bass Strait)
    if lat < -39.5 and 143.5 <= lon <= 148.6:
        return "TAS"
    # Northern Territory (129..138 E, north of 26 S)
    if 129.0 <= lon < 138.0 and lat >= -26.0:
        return "NT"
    # South Australia (129..141 E, south of 26 S)
    if 129.0 <= lon < 141.0 and lat < -26.0:
        return "SA"
    # Queensland (east of 138 E, north of ~29 S)
    if lon >= 138.0 and lat > -29.0:
        return "QLD"
    # ACT approximate box
    if 148.72 <= lon <= 149.42 and -35.94 <= lat <= -35.10:
        return "ACT"
    # Murray River border along the Australian Alps corridor (148.00..148.25 E)
    if 148.00 <= lon <= 148.25 and -36.75 <= lat <= -36.50:
        river_lat = -36.565 + 0.004 * math.sin((lon - 148.05) * 40.0)
        return "VIC" if lat <= river_lat else "NSW"
    # Broad VIC vs NSW latitude split (Murray River runs roughly -36.0 to -36.8)
    if lat < -36.50:
        return "VIC"
    return "NSW"


def _snap_endpoint_clusters(ways: list[ConflatedWay], tol_m: float = JUNCTION_SNAP_M) -> None:
    """Snap near-coincident endpoints within tol_m to a shared deterministic coordinate."""
    endpoints: list[tuple[int, int, float, float]] = []
    for wi, w in enumerate(ways):
        coords = list(w.geom_3577.coords)
        endpoints.append((wi, 0, coords[0][0], coords[0][1]))
        endpoints.append((wi, -1, coords[-1][0], coords[-1][1]))

    # Deterministic O(N) spatial-grid clustering (check only 3x3 neighboring tol_m cells)
    cell_size = max(tol_m, 1e-6)
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for idx, (_wi, _end, x, y) in enumerate(endpoints):
        grid[(math.floor(x / cell_size), math.floor(y / cell_size))].append(idx)

    assigned: dict[int, tuple[float, float]] = {}
    for i, (_wi, _end, xi, yi) in enumerate(endpoints):
        if i in assigned:
            continue
        gx = math.floor(xi / cell_size)
        gy = math.floor(yi / cell_size)
        cluster = [i]
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((gx + dx, gy + dy), ()):
                    if j <= i or j in assigned:
                        continue
                    _, _, xj, yj = endpoints[j]
                    if math.hypot(xi - xj, yi - yj) <= tol_m:
                        cluster.append(j)
        if len(cluster) > 1 and any(
            endpoints[k][2] != xi or endpoints[k][3] != yi for k in cluster[1:]
        ):
            # Only snap clusters with slight mismatch so untouched exact junctions never drift
            cx = round(sum(endpoints[k][2] for k in cluster) / len(cluster), 2)
            cy = round(sum(endpoints[k][3] for k in cluster) / len(cluster), 2)
            for k in cluster:
                assigned[k] = (cx, cy)

    per_way: dict[int, dict[int, tuple[float, float]]] = defaultdict(dict)
    for k, coord in assigned.items():
        wi, end_idx, _, _ = endpoints[k]
        per_way[wi][end_idx] = coord

    for wi, updates in per_way.items():
        coords = list(ways[wi].geom_3577.coords)
        for end_idx, new_xy in updates.items():
            coords[end_idx] = new_xy
        ways[wi].geom_3577 = LineString(coords)


def _node_at_junctions(ways: list[ConflatedWay]) -> list[ConflatedWay]:
    """Split conflated ways at all interior junctions/T-intersections preserving attributes."""
    if not ways:
        return []
    _snap_endpoint_clusters(ways, JUNCTION_SNAP_M)
    geoms = [w.geom_3577 for w in ways]
    tree = STRtree(geoms)

    noded: list[ConflatedWay] = []
    for i, way in enumerate(ways):
        line = way.geom_3577
        total_len = line.length
        if total_len < 1.0:
            continue

        split_dists: list[float] = []
        for j in tree.query(line.buffer(JUNCTION_SNAP_M)):
            j_idx = int(j)
            if j_idx == i:
                continue
            other = geoms[j_idx]
            inter = line.intersection(other)
            candidate_pts: list[Point] = []
            if isinstance(inter, Point):
                candidate_pts.append(inter)
            elif isinstance(inter, MultiPoint):
                candidate_pts.extend(inter.geoms)
            elif not inter.is_empty and hasattr(inter, "geoms"):
                for sub in inter.geoms:
                    if isinstance(sub, Point):
                        candidate_pts.append(sub)
            # Also check near-touching endpoints of `other` within JUNCTION_SNAP_M
            other_coords = list(other.coords)
            for xy in (other_coords[0], other_coords[-1]):
                ep = Point(xy)
                if ep.distance(line) <= JUNCTION_SNAP_M:
                    candidate_pts.append(ep)

            for pt in candidate_pts:
                d = line.project(pt)
                if JUNCTION_ENDPOINT_MARGIN_M < d < (total_len - JUNCTION_ENDPOINT_MARGIN_M):
                    split_dists.append(d)

        if not split_dists:
            noded.append(way)
            continue

        # Deduplicate split distances within 1.0 m
        split_dists.sort()
        dedup_dists: list[float] = [0.0]
        for d in split_dists:
            if d - dedup_dists[-1] > JUNCTION_SNAP_M:
                dedup_dists.append(d)
        if total_len - dedup_dists[-1] > JUNCTION_SNAP_M:
            dedup_dists.append(total_len)
        else:
            dedup_dists[-1] = total_len

        for k in range(len(dedup_dists) - 1):
            sub = substring(line, dedup_dists[k], dedup_dists[k + 1], normalized=False)
            if isinstance(sub, LineString) and sub.length >= 1.0:
                sub_wgs84 = to_wgs84(sub)
                assert isinstance(sub_wgs84, LineString)
                noded.append(
                    ConflatedWay(
                        geom_3577=sub,
                        geom_wgs84=sub_wgs84,
                        primary_source=way.primary_source,
                        has_osm_source=way.has_osm_source,
                        name=way.name,
                        alt_names=list(way.alt_names),
                        trail_class=way.trail_class,
                        is_vehicular=way.is_vehicular,
                        surface_class=way.surface_class,
                        tracktype=way.tracktype,
                        smoothness=way.smoothness,
                        is_4wd_only=way.is_4wd_only,
                        is_4wd_recommended=way.is_4wd_recommended,
                        access_status=way.access_status,
                        has_gate_barrier=way.has_gate_barrier,
                        is_bridged=way.is_bridged,
                        is_ford=way.is_ford,
                        source_refs=[dict(r) for r in way.source_refs],
                    )
                )
    return noded


def _metric_node_key(x: float, y: float) -> tuple[int, int]:
    """Quantize metric EPSG:3577 point to 1.0 m topological bucket."""
    return (round(x), round(y))


def _merge_key(way: ConflatedWay) -> tuple[Any, ...]:
    return (
        (way.name or "").strip().lower(),
        way.trail_class,
        way.surface_class,
        way.tracktype,
        way.access_status,
        way.is_4wd_only,
        way.is_4wd_recommended,
        way.is_bridged,
        way.is_ford,
        way.has_gate_barrier,
        way.primary_source,
    )


def _deflection_deg(coords_a: list[tuple[float, ...]], coords_b: list[tuple[float, ...]]) -> float:
    """Compute deflection angle [0, 180] where coords_a ends at the start of coords_b."""
    ax = coords_a[-1][0] - coords_a[-2][0]
    ay = coords_a[-1][1] - coords_a[-2][1]
    bx = coords_b[1][0] - coords_b[0][0]
    by = coords_b[1][1] - coords_b[0][1]
    la = math.hypot(ax, ay)
    lb = math.hypot(bx, by)
    if la < 1e-9 or lb < 1e-9:
        return 0.0
    cos_val = max(-1.0, min(1.0, (ax * bx + ay * by) / (la * lb)))
    return math.degrees(math.acos(cos_val))


def _merge_degree2_continuations(ways: list[ConflatedWay]) -> list[ConflatedWay]:
    """Dissolve degree-2 vertices joining compatible ways with <45 deg deflection."""
    active: list[ConflatedWay | None] = list(ways)

    changed = True
    while changed:
        changed = False
        incident: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
        for idx, way in enumerate(active):
            if way is None:
                continue
            c = list(way.geom_3577.coords)
            k0 = _metric_node_key(c[0][0], c[0][1])
            k1 = _metric_node_key(c[-1][0], c[-1][1])
            incident[k0].append((idx, 0))
            incident[k1].append((idx, -1))

        touched_this_pass: set[int] = set()
        for _node_k, edges in incident.items():
            if len(edges) != 2:
                continue
            (i, end_i), (j, end_j) = edges
            if i == j or i in touched_this_pass or j in touched_this_pass:
                continue
            wi = active[i]
            wj = active[j]
            if wi is None or wj is None:
                continue
            if _merge_key(wi) != _merge_key(wj):
                continue

            ci = list(wi.geom_3577.coords)
            cj = list(wj.geom_3577.coords)
            # Orient so ci ends at the shared vertex and cj starts at the shared vertex
            if end_i == 0:
                ci.reverse()
            if end_j == -1:
                cj.reverse()

            if _deflection_deg(ci, cj) >= MAX_MERGE_DEFLECTION_DEG:
                continue

            merged_line_3577 = LineString(ci + cj[1:])
            merged_wgs84 = to_wgs84(merged_line_3577)
            assert isinstance(merged_wgs84, LineString)

            merged_refs = list(wi.source_refs)
            for ref in wj.source_refs:
                if ref not in merged_refs:
                    merged_refs.append(ref)

            merged_alts = list(wi.alt_names)
            for alt in wj.alt_names:
                if all(a.lower() != alt.lower() for a in merged_alts):
                    merged_alts.append(alt)

            wi.geom_3577 = merged_line_3577
            wi.geom_wgs84 = merged_wgs84
            wi.has_osm_source = wi.has_osm_source or wj.has_osm_source
            wi.source_refs = merged_refs
            wi.alt_names = merged_alts
            active[j] = None
            touched_this_pass.add(i)
            touched_this_pass.add(j)
            changed = True

    return [w for w in active if w is not None]


def _drop_dangling_slivers(ways: list[ConflatedWay]) -> list[ConflatedWay]:
    """Discard dangling spurs shorter than 15 m while preserving short connected links."""
    degree: dict[tuple[int, int], int] = defaultdict(int)
    for w in ways:
        c = list(w.geom_3577.coords)
        degree[_metric_node_key(c[0][0], c[0][1])] += 1
        degree[_metric_node_key(c[-1][0], c[-1][1])] += 1

    kept: list[ConflatedWay] = []
    for w in ways:
        c = list(w.geom_3577.coords)
        d0 = degree[_metric_node_key(c[0][0], c[0][1])]
        d1 = degree[_metric_node_key(c[-1][0], c[-1][1])]
        if w.geom_3577.length < MIN_SPUR_SLIVER_M and (d0 <= 1 or d1 <= 1):
            continue
        kept.append(w)
    return kept


def segment_conflated_ways(
    ways: Sequence[ConflatedWay],
    *,
    state_resolver: Any | None = None,
) -> tuple[list[CanonicalTrackGroup], list[CanonicalSegmentCandidate]]:
    """Node, merge degree-2 continuations, chunk into 50-100 m segments, and group tracks."""
    noded = _node_at_junctions(list(ways))
    merged = _merge_degree2_continuations(noded)
    cleaned = _drop_dangling_slivers(merged)

    segments: list[CanonicalSegmentCandidate] = []
    seen_ids: set[str] = set()

    for way in cleaned:
        # Canonicalize edge direction before chunking so chunk boundaries are reversal-invariant
        edge_wgs84 = normalize_direction_wgs84(way.geom_wgs84)
        edge_3577 = to_albers(edge_wgs84)
        assert isinstance(edge_3577, LineString)

        length_m = edge_3577.length
        if length_m <= 0.0:
            continue
        n_chunks = compute_chunk_count(length_m)

        for idx in range(n_chunks):
            f0 = idx / n_chunks
            f1 = (idx + 1) / n_chunks
            chunk_3577 = substring(edge_3577, f0, f1, normalized=True)
            if not isinstance(chunk_3577, LineString) or chunk_3577.length <= 0.0:
                continue
            chunk_wgs84 = to_wgs84(chunk_3577)
            assert isinstance(chunk_wgs84, LineString)

            (
                norm_wgs84,
                norm_3577,
                fingerprint,
                segment_id,
                start_key,
                end_key,
            ) = compute_geom_fingerprint_and_id(chunk_wgs84)

            # Rare exact-geometry collision guard
            if segment_id in seen_ids:
                continue
            seen_ids.add(segment_id)

            mid_wgs84 = to_wgs84(norm_3577.interpolate(0.5, normalized=True))
            assert isinstance(mid_wgs84, Point)
            if state_resolver is not None:
                state = state_resolver(mid_wgs84.x, mid_wgs84.y)
            else:
                state = infer_state_from_lonlat(mid_wgs84.x, mid_wgs84.y)

            segments.append(
                CanonicalSegmentCandidate(
                    segment_id=segment_id,
                    geom_fingerprint=fingerprint,
                    geom_wgs84=norm_wgs84,
                    geom_3577=norm_3577,
                    start_node_key=start_key,
                    end_node_key=end_key,
                    length_m=round(norm_3577.length, 2),
                    bearing_deg=round(compute_bearing_deg(norm_3577), 2),
                    state=state,
                    name=way.name,
                    alt_names=list(way.alt_names),
                    trail_class=way.trail_class,
                    is_vehicular=way.is_vehicular,
                    surface_class=way.surface_class,
                    tracktype=way.tracktype,
                    smoothness=way.smoothness,
                    is_4wd_only=way.is_4wd_only,
                    is_4wd_recommended=way.is_4wd_recommended,
                    access_status=way.access_status,
                    has_gate_barrier=way.has_gate_barrier,
                    is_bridged=way.is_bridged,
                    is_ford=way.is_ford,
                    primary_source=way.primary_source,
                    has_osm_source=way.has_osm_source,
                    source_refs=[dict(r) for r in way.source_refs],
                )
            )

    tracks = _group_segments_into_tracks(segments)
    return tracks, segments


def _order_segments_topologically(
    segs: list[CanonicalSegmentCandidate],
) -> list[CanonicalSegmentCandidate]:
    """Order track segments along start_node_key <-> end_node_key adjacency chains.

    Walks from the deterministic lexicographically smallest degree-1 endpoint (or smallest
    node key for closed loops) so curved, U-shaped, and hairpin/switchback tracks preserve
    physically contiguous ``segment_seq`` ordering for elevation profiles.
    """
    n = len(segs)
    if n <= 1:
        return list(segs)

    # Map each node key to incident (seg_idx, other_node_key) sorted deterministically
    adj: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for idx, seg in enumerate(segs):
        adj[seg.start_node_key].append((idx, seg.end_node_key))
        adj[seg.end_node_key].append((idx, seg.start_node_key))
    for node_k in adj:
        adj[node_k].sort(key=lambda item: (item[1], segs[item[0]].segment_id))

    visited: set[int] = set()
    ordered: list[CanonicalSegmentCandidate] = []

    while len(visited) < n:
        # Compute remaining unvisited degree per node
        rem_deg: dict[str, int] = defaultdict(int)
        for idx, seg in enumerate(segs):
            if idx in visited:
                continue
            rem_deg[seg.start_node_key] += 1
            rem_deg[seg.end_node_key] += 1

        endpoints = sorted(nk for nk, deg in rem_deg.items() if deg == 1)
        start_node = endpoints[0] if endpoints else sorted(rem_deg.keys())[0]

        curr = start_node
        while True:
            next_choice: tuple[int, str] | None = None
            for seg_idx, other_node in adj[curr]:
                if seg_idx not in visited:
                    next_choice = (seg_idx, other_node)
                    break
            if next_choice is None:
                break
            seg_idx, other_node = next_choice
            visited.add(seg_idx)
            ordered.append(segs[seg_idx])
            curr = other_node

    return ordered


def _group_segments_into_tracks(
    segments: list[CanonicalSegmentCandidate],
) -> list[CanonicalTrackGroup]:
    """Group canonical segments into deterministic logical tracks with topological ordering."""
    buckets: dict[str, list[CanonicalSegmentCandidate]] = defaultdict(list)
    for seg in segments:
        if seg.name:
            key = f"named:{seg.name.strip().lower()}"
        else:
            key = f"unnamed:{seg.primary_source}:{seg.trail_class}:{seg.start_node_key}"
        buckets[key].append(seg)

    tracks: list[CanonicalTrackGroup] = []
    for key in sorted(buckets.keys()):
        segs = _order_segments_topologically(buckets[key])

        display_name = segs[0].name or f"Unnamed {segs[0].trail_class.replace('_', ' ').title()}"
        trk_hash = hashlib.sha256(f"trailforge:trk:v1|{key}".encode()).hexdigest()[:12]
        track_id = f"tftrk_{trk_hash}"

        state_order = ("VIC", "NSW", "ACT", "TAS", "QLD", "WA", "SA", "NT")
        states = {s.state for s in segs}
        ordered_states = [st for st in state_order if st in states]
        if not ordered_states:
            ordered_states = sorted(states)
        track_state = "/".join(ordered_states)

        alt_names_seen: dict[str, str] = {}
        for s in segs:
            for alt in s.alt_names:
                if alt.lower() != display_name.lower():
                    alt_names_seen.setdefault(alt.lower(), alt)

        source_order = ("osm", "vic", "nsw", "act", "tas", "qld", "wa", "sa", "nt")
        sources = {s.primary_source for s in segs}
        primary_source = next(
            (src for src in source_order if src in sources), segs[0].primary_source
        )
        has_osm = any(s.has_osm_source for s in segs)

        union_wgs84 = unary_union([s.geom_wgs84 for s in segs])
        minx, miny, maxx, maxy = union_wgs84.bounds
        # Expand degenerate point/vertical/horizontal envelope slightly so Polygon is always valid
        if maxx - minx < 1e-6:
            minx -= 5e-5
            maxx += 5e-5
        if maxy - miny < 1e-6:
            miny -= 5e-5
            maxy += 5e-5
        bbox_poly = Polygon(
            [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)]
        )

        for seq, seg in enumerate(segs):
            seg.track_id = track_id
            seg.segment_seq = seq

        tracks.append(
            CanonicalTrackGroup(
                track_id=track_id,
                name=display_name,
                alt_names=sorted(alt_names_seen.values()),
                state=track_state,
                primary_source=primary_source,
                has_osm_source=has_osm,
                trail_class=segs[0].trail_class,
                is_vehicular=any(s.is_vehicular for s in segs),
                total_length_m=round(sum(s.length_m for s in segs), 2),
                bbox_geom=bbox_poly,
                segment_ids=[s.segment_id for s in segs],
            )
        )

    return tracks
