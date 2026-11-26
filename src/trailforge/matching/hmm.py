"""In-PostGIS Hidden Markov Model (HMM) map-matcher for TrailForge Slice 06.

Implements the pluggable ``TrackMatcher`` protocol using:
  1. Single batched PostGIS GIST + ``ST_DWithin`` (EPSG:3577) corridor query over
     active vehicular canonical ``track_segments`` (zero N+1 SQL queries).
  2. Gaussian perpendicular distance emission probability combined with gentle
     bidirectional heading agreement to disambiguate perpendicular junctions.
  3. Newson & Krumm (2009) exponential transition likelihood over the local
     ``start_node_key`` / ``end_node_key`` topological graph (up to 4 hops) so
     real-world GPS spacing (~60-120 m) transitions cleanly across 50-100 m
     canonical segments and bridges skipped intermediate segments along the
     exact connected Viterbi path.
  4. Viterbi chain breaking when no candidates exist within ``search_radius_m``
     or emission confidence falls below ``min_confidence``, emitting contiguous
     off-network runs (>= 100 m) as ``UnmatchedSubtrace`` records without
     corrupting nearby canonical segments.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Literal

import numpy as np
from pyproj import Transformer
from shapely import STRtree, wkb
from shapely.geometry import LineString, Point
from sqlalchemy import text
from sqlalchemy.orm import Session

from trailforge.matching.protocol import (
    GpsPoint,
    MatchedSegmentTraversal,
    MatchResult,
    UnmatchedSubtrace,
)

_MAX_SPEED_KMH: float = 160.0
_SLOW_HOTSPOT_KMH: float = 5.0
_MIN_DURATION_S: float = 0.5
_ENDPOINT_CONNECT_TOL_M: float = 3.0


@lru_cache(maxsize=1)
def _wgs84_to_albers() -> Transformer:
    return Transformer.from_crs("EPSG:4326", "EPSG:3577", always_xy=True)


@dataclass(slots=True)
class _CorridorSegment:
    """In-memory projection of an active vehicular canonical track_segments row."""

    idx: int
    segment_id: str
    track_id: str | None
    start_node_key: str
    end_node_key: str
    length_m: float
    bearing_deg: float
    geom_wgs84: LineString
    geom_3577: LineString
    start_xy: tuple[float, float]
    end_xy: tuple[float, float]


@dataclass(slots=True)
class _CandidateState:
    """Single HMM candidate state snapping GPS fix ``t`` onto segment ``seg_idx``."""

    seg_idx: int
    proj_dist_m: float
    proj_frac: float
    perp_dist_m: float
    emission_conf: float
    log_emission: float


@dataclass(slots=True)
class _DecodedObs:
    """Viterbi-decoded point matched to a canonical segment."""

    point_idx: int
    point: GpsPoint
    xy: tuple[float, float]
    seg_idx: int
    proj_dist_m: float
    proj_frac: float
    emission_conf: float


def _bearing_from_dxdy(dx: float, dy: float) -> float | None:
    if math.hypot(dx, dy) < 1.0:
        return None
    # EPSG:3577 has +x East, +y North; bearing 0=N, 90=E
    return math.degrees(math.atan2(dx, dy)) % 360.0


def _acute_bidirectional_diff_deg(b1: float, b2: float) -> float:
    """Acute angle in [0, 90] ignoring direction reversal."""
    diff = abs(b1 - b2) % 180.0
    return min(diff, 180.0 - diff)


def _directed_angle_diff_deg(b1: float, b2: float) -> float:
    """Smallest directed angle in [0, 180] between two compass bearings."""
    diff = abs(b1 - b2) % 360.0
    return min(diff, 360.0 - diff)


def _local_point_bearings(xys: Sequence[tuple[float, float]]) -> list[float | None]:
    """Estimate local compass bearing for each GPS fix from neighbors."""
    n = len(xys)
    bearings: list[float | None] = [None] * n
    for i in range(n):
        if i == 0 and n > 1:
            dx = xys[1][0] - xys[0][0]
            dy = xys[1][1] - xys[0][1]
        elif i == n - 1 and n > 1:
            dx = xys[i][0] - xys[i - 1][0]
            dy = xys[i][1] - xys[i - 1][1]
        elif 0 < i < n - 1:
            dx = xys[i + 1][0] - xys[i - 1][0]
            dy = xys[i + 1][1] - xys[i - 1][1]
        else:
            continue
        bearings[i] = _bearing_from_dxdy(dx, dy)
    return bearings


def _build_node_adjacency(
    segments: Sequence[_CorridorSegment],
) -> tuple[dict[tuple[int, int], list[tuple[int, int]]], list[tuple[str, str]]]:
    """Build segment-endpoint adjacency graph across exact node keys and <=3 m junctions.

    Each segment has two endpoints: ``end=0`` (start) and ``end=1`` (end).
    Returns mapping ``(seg_idx, end) -> [(neighbor_seg_idx, neighbor_entry_end), ...]``.
    """
    node_buckets: dict[str, list[tuple[int, int]]] = defaultdict(list)
    endpoint_list: list[tuple[int, int, float, float]] = []
    node_pairs: list[tuple[str, str]] = []

    for seg in segments:
        node_buckets[seg.start_node_key].append((seg.idx, 0))
        node_buckets[seg.end_node_key].append((seg.idx, 1))
        endpoint_list.append((seg.idx, 0, seg.start_xy[0], seg.start_xy[1]))
        endpoint_list.append((seg.idx, 1, seg.end_xy[0], seg.end_xy[1]))
        node_pairs.append((seg.start_node_key, seg.end_node_key))

    adj_sets: dict[tuple[int, int], set[tuple[int, int]]] = defaultdict(set)
    for members in node_buckets.values():
        if len(members) < 2:
            continue
        for u in members:
            for v in members:
                if u[0] != v[0]:
                    adj_sets[u].add(v)

    # Secondary <= 3 m spatial junction fallback for tiny rounding differences
    if endpoint_list:
        ep_pts = [Point(x, y) for _, _, x, y in endpoint_list]
        tree = STRtree(ep_pts)
        for i, (seg_u, end_u, xu, yu) in enumerate(endpoint_list):
            for j_raw in tree.query(ep_pts[i].buffer(_ENDPOINT_CONNECT_TOL_M)):
                j = int(j_raw)
                if j <= i:
                    continue
                seg_v, end_v, xv, yv = endpoint_list[j]
                if seg_u == seg_v:
                    continue
                if math.hypot(xu - xv, yu - yv) <= _ENDPOINT_CONNECT_TOL_M:
                    adj_sets[(seg_u, end_u)].add((seg_v, end_v))
                    adj_sets[(seg_v, end_v)].add((seg_u, end_u))

    adj: dict[tuple[int, int], list[tuple[int, int]]] = {
        k: sorted(v) for k, v in adj_sets.items()
    }
    return adj, node_pairs


class _CorridorGraph:
    """Cached bounded shortest-path evaluator over loaded corridor segments."""

    def __init__(self, segments: Sequence[_CorridorSegment], max_hops: int) -> None:
        self.segments = segments
        self.max_hops = max_hops
        self.adj, _ = _build_node_adjacency(segments)
        # Cache: (seg_u, exit_end_u, seg_v, entry_end_v) -> (mid_dist_m, [(mid_seg_idx, enter_end)])
        self._ep_cache: dict[
            tuple[int, int, int, int],
            tuple[float, list[tuple[int, int]]] | None,
        ] = {}

    def _shortest_between_endpoints(
        self,
        seg_u: int,
        exit_end_u: int,
        seg_v: int,
        entry_end_v: int,
        max_dist_m: float,
    ) -> tuple[float, list[tuple[int, int]]] | None:
        """Find shortest intermediate segment path from leaving seg_u to entering seg_v."""
        key = (seg_u, exit_end_u, seg_v, entry_end_v)
        if key in self._ep_cache:
            cached = self._ep_cache[key]
            if cached is None:
                return None
            return cached if cached[0] <= max_dist_m else None

        # Dijkstra over directed transitions entering segments:
        # state = (cost_m, hops, cur_seg, cur_entry_end, mid_path)
        pq: list[tuple[float, int, int, int, list[tuple[int, int]]]] = []
        for nbr_seg, nbr_entry in self.adj.get((seg_u, exit_end_u), ()):
            if nbr_seg == seg_v and nbr_entry == entry_end_v:
                result: tuple[float, list[tuple[int, int]]] = (0.0, [])
                self._ep_cache[key] = result
                return result
            if self.max_hops >= 2:
                seg_len = self.segments[nbr_seg].length_m
                heapq.heappush(pq, (seg_len, 2, nbr_seg, nbr_entry, [(nbr_seg, nbr_entry)]))

        best_seen: dict[tuple[int, int], float] = {}
        found: tuple[float, list[tuple[int, int]]] | None = None

        while pq:
            cost_m, hops, cur_seg, cur_entry, mid_path = heapq.heappop(pq)
            if cost_m > max(max_dist_m, 600.0):
                break
            exit_end = 1 - cur_entry
            state_key = (cur_seg, exit_end)
            if best_seen.get(state_key, float("inf")) <= cost_m:
                continue
            best_seen[state_key] = cost_m

            for nbr_seg, nbr_entry in self.adj.get((cur_seg, exit_end), ()):
                if nbr_seg == seg_v and nbr_entry == entry_end_v:
                    found = (cost_m, mid_path)
                    break
                if hops < self.max_hops and nbr_seg != seg_u and all(
                    m_seg != nbr_seg for m_seg, _ in mid_path
                ):
                    next_cost = cost_m + self.segments[nbr_seg].length_m
                    if next_cost <= max(max_dist_m, 600.0):
                        heapq.heappush(
                            pq,
                            (
                                next_cost,
                                hops + 1,
                                nbr_seg,
                                nbr_entry,
                                [*mid_path, (nbr_seg, nbr_entry)],
                            ),
                        )
            if found is not None:
                break

        self._ep_cache[key] = found
        if found is None or found[0] > max_dist_m:
            return None
        return found

    def shortest_route(
        self,
        seg_u: int,
        dist_u: float,
        seg_v: int,
        dist_v: float,
        max_dist_m: float,
    ) -> tuple[float, int | None, int | None, list[tuple[int, int]]] | None:
        """Compute shortest network route from (seg_u, dist_u) to (seg_v, dist_v).

        Returns:
            ``(total_net_dist_m, exit_end_u, entry_end_v, mid_segments)`` where
            ``mid_segments`` is a list of ``(seg_idx, entry_end)`` for strictly
            intermediate segments crossed between ``seg_u`` and ``seg_v``, or
            ``None`` if no topological route exists within ``max_hops`` / ``max_dist_m``.
        """
        if seg_u == seg_v:
            return (abs(dist_v - dist_u), None, None, [])

        len_u = self.segments[seg_u].length_m
        len_v = self.segments[seg_v].length_m

        best: tuple[float, int, int, list[tuple[int, int]]] | None = None
        for exit_end_u, d_leave in ((0, dist_u), (1, max(0.0, len_u - dist_u))):
            if not self.adj.get((seg_u, exit_end_u)):
                continue
            for entry_end_v, d_enter in ((0, dist_v), (1, max(0.0, len_v - dist_v))):
                base_d = d_leave + d_enter
                if base_d > max_dist_m:
                    continue
                if best is not None and base_d >= best[0]:
                    continue
                mid = self._shortest_between_endpoints(
                    seg_u, exit_end_u, seg_v, entry_end_v, max_dist_m - base_d
                )
                if mid is None:
                    continue
                total_d = base_d + mid[0]
                if best is None or total_d < best[0]:
                    best = (total_d, exit_end_u, entry_end_v, mid[1])

        return best


class PostGISHMMMatcher:
    """In-PostGIS Hidden Markov Model map-matcher implementing ``TrackMatcher``."""

    name: str = "postgis_hmm_v1"

    def __init__(
        self,
        session: Session,
        *,
        sigma_z_m: float = 12.0,
        beta_m: float = 15.0,
        max_candidates: int = 5,
        max_graph_hops: int = 4,
        max_gap_m: float = 350.0,
        max_gap_s: float = 180.0,
        min_unmatched_length_m: float = 100.0,
    ) -> None:
        self._session = session
        self.sigma_z_m = sigma_z_m
        self.beta_m = beta_m
        self.max_candidates = max_candidates
        self.max_graph_hops = max_graph_hops
        self.max_gap_m = max_gap_m
        self.max_gap_s = max_gap_s
        self.min_unmatched_length_m = min_unmatched_length_m

    def _fetch_corridor_segments(
        self,
        points: Sequence[GpsPoint],
        search_radius_m: float,
    ) -> list[_CorridorSegment]:
        """Load active vehicular canonical segments near the trace in a single PostGIS query."""
        trace_coords = ", ".join(f"{p.lon:.7f} {p.lat:.7f}" for p in points)
        trace_wkt = f"LINESTRING({trace_coords})"
        # 1 deg lat ~ 111 km; generous bbox expansion in deg keeps GIST index fast & lossless
        corridor_radius_m = max(search_radius_m + 25.0, 60.0)
        expand_deg = (corridor_radius_m / 80_000.0) + 0.002

        rows = self._session.execute(
            text(
                """
                WITH trace AS (
                    SELECT
                        ST_GeomFromText(:trace_wkt, 4326) AS geom_4326,
                        ST_Transform(ST_GeomFromText(:trace_wkt, 4326), 3577) AS geom_3577
                )
                SELECT
                    ts.segment_id,
                    ts.track_id,
                    ts.start_node_key,
                    ts.end_node_key,
                    ts.length_m,
                    ts.bearing_deg,
                    ST_AsEWKB(ts.geom) AS geom_4326_wkb,
                    ST_AsEWKB(ST_Transform(ts.geom, 3577)) AS geom_3577_wkb
                FROM track_segments ts
                CROSS JOIN trace
                WHERE ts.is_active = TRUE
                  AND ts.is_vehicular = TRUE
                  AND ts.geom && ST_Expand(trace.geom_4326, :expand_deg)
                  AND ST_DWithin(
                      ST_Transform(ts.geom, 3577),
                      trace.geom_3577,
                      :corridor_radius_m
                  )
                ORDER BY ts.segment_id
                """
            ),
            {
                "trace_wkt": trace_wkt,
                "expand_deg": expand_deg,
                "corridor_radius_m": corridor_radius_m,
            },
        ).fetchall()

        segments: list[_CorridorSegment] = []
        for idx, row in enumerate(rows):
            geom_4326 = wkb.loads(bytes(row.geom_4326_wkb))
            geom_3577 = wkb.loads(bytes(row.geom_3577_wkb))
            if not isinstance(geom_4326, LineString) or not isinstance(geom_3577, LineString):
                continue
            coords_3577 = list(geom_3577.coords)
            if len(coords_3577) < 2:
                continue
            length_m = (
                float(row.length_m)
                if row.length_m and row.length_m > 0
                else geom_3577.length
            )
            segments.append(
                _CorridorSegment(
                    idx=idx,
                    segment_id=str(row.segment_id),
                    track_id=str(row.track_id) if row.track_id is not None else None,
                    start_node_key=str(row.start_node_key),
                    end_node_key=str(row.end_node_key),
                    length_m=length_m,
                    bearing_deg=float(row.bearing_deg),
                    geom_wgs84=geom_4326,
                    geom_3577=geom_3577,
                    start_xy=(float(coords_3577[0][0]), float(coords_3577[0][1])),
                    end_xy=(float(coords_3577[-1][0]), float(coords_3577[-1][1])),
                )
            )
        return segments

    def _build_candidates_per_point(
        self,
        points: Sequence[GpsPoint],
        xys: Sequence[tuple[float, float]],
        bearings: Sequence[float | None],
        segments: Sequence[_CorridorSegment],
        *,
        search_radius_m: float,
        min_confidence: float,
    ) -> tuple[
        list[list[_CandidateState]],
        list[Literal["no_candidates", "low_confidence"] | None],
    ]:
        """Compute top-k emission candidate states per GPS point."""
        n = len(points)
        if not segments:
            return ([[] for _ in range(n)], ["no_candidates"] * n)

        tree = STRtree([seg.geom_3577 for seg in segments])
        candidates_per_pt: list[list[_CandidateState]] = []
        unmatched_reasons: list[Literal["no_candidates", "low_confidence"] | None] = []

        for idx, pt in enumerate(points):
            px, py = xys[idx]
            p_geom = Point(px, py)
            sigma = max(self.sigma_z_m, pt.accuracy_m or self.sigma_z_m)
            pt_bearing = bearings[idx]

            nearby_indices = tree.query(p_geom.buffer(search_radius_m))
            raw_cands: list[_CandidateState] = []
            had_any_within_radius = False

            for seg_idx_raw in nearby_indices:
                seg_idx = int(seg_idx_raw)
                seg = segments[seg_idx]
                perp_d = float(p_geom.distance(seg.geom_3577))
                if perp_d > search_radius_m:
                    continue
                had_any_within_radius = True

                proj_d = float(seg.geom_3577.project(p_geom))
                proj_frac = max(0.0, min(1.0, proj_d / seg.length_m)) if seg.length_m > 0 else 0.0

                dist_prob = math.exp(-0.5 * (perp_d / sigma) ** 2)
                if pt_bearing is not None:
                    ang_diff = _acute_bidirectional_diff_deg(pt_bearing, seg.bearing_deg)
                    cos_sq = math.cos(math.radians(ang_diff)) ** 2
                    heading_score = 0.75 + 0.25 * cos_sq
                    viterbi_heading_factor = 0.35 + 0.65 * cos_sq
                else:
                    heading_score = 1.0
                    viterbi_heading_factor = 1.0

                emission_conf = max(0.0, min(1.0, dist_prob * heading_score))
                log_emission = -0.5 * (perp_d / sigma) ** 2 + math.log(
                    max(1e-6, viterbi_heading_factor)
                )
                raw_cands.append(
                    _CandidateState(
                        seg_idx=seg_idx,
                        proj_dist_m=proj_d,
                        proj_frac=proj_frac,
                        perp_dist_m=perp_d,
                        emission_conf=emission_conf,
                        log_emission=log_emission,
                    )
                )

            raw_cands.sort(key=lambda c: (-c.log_emission, c.perp_dist_m))
            filtered = [
                c
                for c in raw_cands[: self.max_candidates]
                if c.emission_conf >= min_confidence
            ]

            if filtered:
                candidates_per_pt.append(filtered)
                unmatched_reasons.append(None)
            else:
                candidates_per_pt.append([])
                reason: Literal["no_candidates", "low_confidence"] = (
                    "low_confidence" if had_any_within_radius else "no_candidates"
                )
                unmatched_reasons.append(reason)

        return candidates_per_pt, unmatched_reasons

    def _run_viterbi_chain(
        self,
        indices: Sequence[int],
        points: Sequence[GpsPoint],
        xys: Sequence[tuple[float, float]],
        candidates_per_pt: Sequence[Sequence[_CandidateState]],
        graph: _CorridorGraph,
    ) -> list[list[_DecodedObs]]:
        """Run Viterbi decoding over a candidate-bearing run, splitting cleanly on breaks."""
        if not indices:
            return []

        chains: list[list[_DecodedObs]] = []
        sub_indices: list[int] = [indices[0]]
        dp: list[list[float]] = [[c.log_emission for c in candidates_per_pt[indices[0]]]]
        backptr: list[list[int]] = [[-1 for _ in candidates_per_pt[indices[0]]]]

        def _flush_chain() -> None:
            if not sub_indices:
                return
            last_scores = dp[-1]
            best_j = max(range(len(last_scores)), key=last_scores.__getitem__)
            chosen_cands: list[_CandidateState] = []
            cur_j = best_j
            for step in range(len(sub_indices) - 1, -1, -1):
                pt_idx = sub_indices[step]
                chosen_cands.append(candidates_per_pt[pt_idx][cur_j])
                cur_j = backptr[step][cur_j]
            chosen_cands.reverse()
            chains.append(
                [
                    _DecodedObs(
                        point_idx=pt_idx,
                        point=points[pt_idx],
                        xy=xys[pt_idx],
                        seg_idx=cand.seg_idx,
                        proj_dist_m=cand.proj_dist_m,
                        proj_frac=cand.proj_frac,
                        emission_conf=cand.emission_conf,
                    )
                    for pt_idx, cand in zip(sub_indices, chosen_cands, strict=True)
                ]
            )

        for k in range(1, len(indices)):
            prev_idx = sub_indices[-1]
            cur_idx = indices[k]
            dt_s = (points[cur_idx].timestamp - points[prev_idx].timestamp).total_seconds()
            d_obs = math.hypot(
                xys[cur_idx][0] - xys[prev_idx][0],
                xys[cur_idx][1] - xys[prev_idx][1],
            )

            prev_cands = candidates_per_pt[prev_idx]
            cur_cands = candidates_per_pt[cur_idx]
            prev_scores = dp[-1]

            if dt_s > self.max_gap_s or d_obs > self.max_gap_m:
                _flush_chain()
                sub_indices = [cur_idx]
                dp = [[c.log_emission for c in cur_cands]]
                backptr = [[-1 for _ in cur_cands]]
                continue

            max_net_m = min(self.max_gap_m * 1.5, max(d_obs * 2.5 + 120.0, 260.0))
            cur_scores: list[float] = []
            cur_back: list[int] = []
            any_valid_transition = False

            for c_cur in cur_cands:
                best_score = -math.inf
                best_prev = -1
                for i_prev, c_prev in enumerate(prev_cands):
                    if not math.isfinite(prev_scores[i_prev]):
                        continue
                    route = graph.shortest_route(
                        c_prev.seg_idx,
                        c_prev.proj_dist_m,
                        c_cur.seg_idx,
                        c_cur.proj_dist_m,
                        max_net_m,
                    )
                    if route is None:
                        continue
                    d_net = route[0]
                    delta_d = abs(d_net - d_obs)
                    log_trans = -delta_d / self.beta_m
                    # Extra penalty against backtracking/loop detours far exceeding displacement
                    if d_net > d_obs * 1.8 + 50.0:
                        log_trans -= (d_net - (d_obs * 1.8 + 50.0)) / self.beta_m
                    score = prev_scores[i_prev] + log_trans + c_cur.log_emission
                    if score > best_score:
                        best_score = score
                        best_prev = i_prev

                cur_scores.append(best_score)
                cur_back.append(best_prev)
                if math.isfinite(best_score):
                    any_valid_transition = True

            if not any_valid_transition:
                _flush_chain()
                sub_indices = [cur_idx]
                dp = [[c.log_emission for c in cur_cands]]
                backptr = [[-1 for _ in cur_cands]]
            else:
                sub_indices.append(cur_idx)
                dp.append(cur_scores)
                backptr.append(cur_back)

        _flush_chain()
        return chains

    def _aggregate_chain_traversals(
        self,
        chain: Sequence[_DecodedObs],
        segments: Sequence[_CorridorSegment],
        graph: _CorridorGraph,
        bearings: Sequence[float | None],
    ) -> list[MatchedSegmentTraversal]:
        """Aggregate Viterbi-decoded observations into ordered ``MatchedSegmentTraversal`` rows."""
        if not chain:
            return []

        # Group contiguous observations on the same segment into runs
        runs: list[list[_DecodedObs]] = []
        for obs in chain:
            if not runs or runs[-1][0].seg_idx != obs.seg_idx:
                runs.append([obs])
            else:
                runs[-1].append(obs)

        # Precompute topological routes between consecutive runs
        routes: list[tuple[float, int | None, int | None, list[tuple[int, int]]] | None] = []
        for r_idx in range(len(runs) - 1):
            a = runs[r_idx][-1]
            b = runs[r_idx + 1][0]
            d_obs = math.hypot(b.xy[0] - a.xy[0], b.xy[1] - a.xy[1])
            max_net_m = min(self.max_gap_m * 1.5, max(d_obs * 2.5 + 120.0, 260.0))
            routes.append(
                graph.shortest_route(a.seg_idx, a.proj_dist_m, b.seg_idx, b.proj_dist_m, max_net_m)
            )

        traversals: list[MatchedSegmentTraversal] = []

        for r_idx, run in enumerate(runs):
            seg = segments[run[0].seg_idx]
            prev_route = routes[r_idx - 1] if r_idx > 0 else None
            next_route = routes[r_idx] if r_idx < len(runs) - 1 else None
            prev_obs = runs[r_idx - 1][-1] if r_idx > 0 else None
            next_obs = runs[r_idx + 1][0] if r_idx < len(runs) - 1 else None

            # 1. Determine traversal direction along canonical segment geometry
            direction: Literal["forward", "reverse"]
            if len(run) >= 2 and abs(run[-1].proj_frac - run[0].proj_frac) > 1e-3:
                direction = "forward" if run[-1].proj_frac > run[0].proj_frac else "reverse"
            elif prev_route is not None and prev_route[2] is not None:
                # Entered via start(0) => moving forward; entered via end(1) => reverse
                direction = "forward" if prev_route[2] == 0 else "reverse"
            elif next_route is not None and next_route[1] is not None:
                # Exited via end(1) => moving forward; exited via start(0) => reverse
                direction = "forward" if next_route[1] == 1 else "reverse"
            else:
                pt_b = bearings[run[0].point_idx]
                if pt_b is not None:
                    diff_deg = _directed_angle_diff_deg(pt_b, seg.bearing_deg)
                    direction = "forward" if diff_deg <= 90.0 else "reverse"
                else:
                    direction = "forward"

            # 2. Interpolate exact boundary entry/exit timestamps where adjacent runs connect
            entry_at: datetime = run[0].point.timestamp
            exit_at: datetime = run[-1].point.timestamp
            local_speeds_kmh: list[float] = []

            if prev_obs is not None and prev_route is not None and prev_route[0] > 0.0:
                dt_in = (run[0].point.timestamp - prev_obs.point.timestamp).total_seconds()
                if dt_in > 0.0:
                    entry_end = prev_route[2]
                    d_from_boundary = (
                        run[0].proj_dist_m
                        if entry_end == 0
                        else max(0.0, seg.length_m - run[0].proj_dist_m)
                    )
                    frac_in = min(1.0, d_from_boundary / prev_route[0])
                    entry_at = run[0].point.timestamp - timedelta(seconds=dt_in * frac_in)
                    local_speeds_kmh.append((prev_route[0] / dt_in) * 3.6)

            if next_obs is not None and next_route is not None and next_route[0] > 0.0:
                dt_out = (next_obs.point.timestamp - run[-1].point.timestamp).total_seconds()
                if dt_out > 0.0:
                    exit_end = next_route[1]
                    d_to_boundary = (
                        run[-1].proj_dist_m
                        if exit_end == 0
                        else max(0.0, seg.length_m - run[-1].proj_dist_m)
                    )
                    frac_out = min(1.0, d_to_boundary / next_route[0])
                    exit_at = run[-1].point.timestamp + timedelta(seconds=dt_out * frac_out)
                    local_speeds_kmh.append((next_route[0] / dt_out) * 3.6)

            # Pairwise point speeds inside this segment run
            point_speed_pairs: list[tuple[_DecodedObs, float]] = []
            for j in range(1, len(run)):
                dt_j = (run[j].point.timestamp - run[j - 1].point.timestamp).total_seconds()
                if dt_j > 0.0:
                    d_j = abs(run[j].proj_dist_m - run[j - 1].proj_dist_m)
                    spd = (d_j / dt_j) * 3.6
                    local_speeds_kmh.append(spd)
                    point_speed_pairs.append((run[j], spd))

            for obs in run:
                if obs.point.speed_mps is not None:
                    spd = obs.point.speed_mps * 3.6
                    local_speeds_kmh.append(spd)
                    point_speed_pairs.append((obs, spd))

            duration_s = (exit_at - entry_at).total_seconds()

            # Compute covered distance along segment corresponding to [entry_at, exit_at]
            if prev_route is not None and next_route is not None and duration_s > 0.0:
                covered_m = seg.length_m
            elif prev_route is not None and duration_s > 0.0:
                entry_end = prev_route[2]
                covered_m = (
                    run[-1].proj_dist_m
                    if entry_end == 0
                    else max(0.0, seg.length_m - run[-1].proj_dist_m)
                )
            elif next_route is not None and duration_s > 0.0:
                exit_end = next_route[1]
                covered_m = (
                    run[0].proj_dist_m
                    if exit_end == 0
                    else max(0.0, seg.length_m - run[0].proj_dist_m)
                )
            else:
                covered_m = abs(run[-1].proj_dist_m - run[0].proj_dist_m)

            if duration_s < _MIN_DURATION_S:
                # Single fix at start/end of a chain: estimate duration from local speed
                est_speed_kmh = (
                    sum(local_speeds_kmh) / len(local_speeds_kmh) if local_speeds_kmh else 15.0
                )
                est_speed_mps = max(0.5, est_speed_kmh / 3.6)
                duration_s = max(_MIN_DURATION_S, seg.length_m / est_speed_mps)
                exit_at = entry_at + timedelta(seconds=duration_s)
                covered_m = seg.length_m

            if covered_m > 1.0:
                avg_speed_kmh = (covered_m / duration_s) * 3.6
            elif local_speeds_kmh:
                avg_speed_kmh = sum(local_speeds_kmh) / len(local_speeds_kmh)
            else:
                avg_speed_kmh = 0.0

            avg_speed_kmh = round(max(0.0, min(_MAX_SPEED_KMH, avg_speed_kmh)), 2)
            all_speeds = [s for s in local_speeds_kmh if math.isfinite(s)]
            if not all_speeds:
                all_speeds = [avg_speed_kmh]
            min_speed_kmh = round(max(0.0, min(_MAX_SPEED_KMH, min(all_speeds))), 2)

            # Roughness summary
            rough_vals = [
                obs.point.roughness_rms
                for obs in run
                if obs.point.roughness_rms is not None
            ]
            if rough_vals:
                rough_rms: float | None = round(
                    math.sqrt(sum(r * r for r in rough_vals) / len(rough_vals)), 4
                )
                rough_p95: float | None = round(float(np.percentile(rough_vals, 95)), 4)
            else:
                rough_rms = None
                rough_p95 = None

            # Slow hotspot snapped onto canonical segment geometry when crawling (<5 km/h)
            slow_hotspot: tuple[float, float] | None = None
            if min_speed_kmh < _SLOW_HOTSPOT_KMH:
                if point_speed_pairs:
                    slow_obs = min(point_speed_pairs, key=lambda pair: pair[1])[0]
                else:
                    slow_obs = run[len(run) // 2]
                snapped = seg.geom_wgs84.interpolate(slow_obs.proj_frac, normalized=True)
                slow_hotspot = (round(float(snapped.x), 6), round(float(snapped.y), 6))

            mean_conf = sum(obs.emission_conf for obs in run) / len(run)
            traversals.append(
                MatchedSegmentTraversal(
                    segment_id=seg.segment_id,
                    track_id=seg.track_id,
                    direction=direction,
                    entry_at=entry_at,
                    exit_at=exit_at,
                    duration_s=round(duration_s, 2),
                    avg_speed_kmh=avg_speed_kmh,
                    min_speed_kmh=min_speed_kmh,
                    roughness_rms=rough_rms,
                    roughness_p95=rough_p95,
                    match_confidence=round(max(0.0, min(1.0, mean_conf)), 4),
                    matched_point_count=len(run),
                    slow_hotspot_lonlat=slow_hotspot,
                )
            )

            # 3. Emit cleanly interpolated traversals for bridged intermediate segments
            if next_route is not None and next_route[3] and next_obs is not None:
                total_net_m, exit_end_u, _entry_end_v, mid_segments = next_route
                dt_bridge = (next_obs.point.timestamp - run[-1].point.timestamp).total_seconds()
                if total_net_m > 0.0 and dt_bridge > 0.0:
                    d_leave = (
                        run[-1].proj_dist_m
                        if exit_end_u == 0
                        else max(0.0, seg.length_m - run[-1].proj_dist_m)
                    )
                    bridge_speed_kmh = round(
                        max(0.0, min(_MAX_SPEED_KMH, (total_net_m / dt_bridge) * 3.6)), 2
                    )
                    min_end_conf = min(run[-1].emission_conf, next_obs.emission_conf)
                    bridge_conf = round(max(0.0, min(1.0, min_end_conf * 0.95)), 4)
                    bridge_rough_vals = [
                        r
                        for r in (run[-1].point.roughness_rms, next_obs.point.roughness_rms)
                        if r is not None
                    ]
                    bridge_rough = (
                        round(sum(bridge_rough_vals) / len(bridge_rough_vals), 4)
                        if bridge_rough_vals
                        else None
                    )
                    cursor_m = d_leave
                    for mid_idx, mid_entry_end in mid_segments:
                        mid_seg = segments[mid_idx]
                        mid_dir: Literal["forward", "reverse"] = (
                            "forward" if mid_entry_end == 0 else "reverse"
                        )
                        t_start = run[-1].point.timestamp + timedelta(
                            seconds=dt_bridge * (cursor_m / total_net_m)
                        )
                        cursor_m += mid_seg.length_m
                        t_end = run[-1].point.timestamp + timedelta(
                            seconds=dt_bridge * min(1.0, cursor_m / total_net_m)
                        )
                        mid_dur = max(_MIN_DURATION_S, (t_end - t_start).total_seconds())
                        if t_end <= t_start:
                            t_end = t_start + timedelta(seconds=mid_dur)
                        mid_hotspot: tuple[float, float] | None = None
                        if bridge_speed_kmh < _SLOW_HOTSPOT_KMH:
                            mid_pt = mid_seg.geom_wgs84.interpolate(0.5, normalized=True)
                            mid_hotspot = (round(float(mid_pt.x), 6), round(float(mid_pt.y), 6))
                        traversals.append(
                            MatchedSegmentTraversal(
                                segment_id=mid_seg.segment_id,
                                track_id=mid_seg.track_id,
                                direction=mid_dir,
                                entry_at=t_start,
                                exit_at=t_end,
                                duration_s=round(mid_dur, 2),
                                avg_speed_kmh=bridge_speed_kmh,
                                min_speed_kmh=bridge_speed_kmh,
                                roughness_rms=bridge_rough,
                                roughness_p95=bridge_rough,
                                match_confidence=bridge_conf,
                                matched_point_count=1,
                                slow_hotspot_lonlat=mid_hotspot,
                            )
                        )

        return traversals

    def match_trace_sync(
        self,
        points: Sequence[GpsPoint],
        *,
        search_radius_m: float = 45.0,
        min_confidence: float = 0.45,
    ) -> MatchResult:
        """Synchronous in-PostGIS HMM map-matching implementation."""
        if len(points) < 2:
            return MatchResult(
                matcher_name=self.name,
                matched_segments=[],
                unmatched_subtraces=[],
                overall_confidence=0.0,
                matched_fraction=0.0,
            )

        fwd = _wgs84_to_albers()
        xs, ys = fwd.transform([p.lon for p in points], [p.lat for p in points])
        xys = [(float(x), float(y)) for x, y in zip(xs, ys, strict=True)]
        bearings = _local_point_bearings(xys)

        segments = self._fetch_corridor_segments(points, search_radius_m)
        candidates_per_pt, unmatched_reasons = self._build_candidates_per_point(
            points,
            xys,
            bearings,
            segments,
            search_radius_m=search_radius_m,
            min_confidence=min_confidence,
        )

        # Partition indices into alternating matched-eligible runs and unmatched blocks
        graph = _CorridorGraph(segments, max_hops=self.max_graph_hops)
        all_chains: list[list[_DecodedObs]] = []
        unmatched_subtraces: list[UnmatchedSubtrace] = []

        idx = 0
        n = len(points)
        while idx < n:
            if candidates_per_pt[idx]:
                start = idx
                while idx < n and candidates_per_pt[idx]:
                    idx += 1
                eligible_run = list(range(start, idx))
                chains = self._run_viterbi_chain(
                    eligible_run, points, xys, candidates_per_pt, graph
                )
                all_chains.extend(chains)
            else:
                start = idx
                reason_counts: dict[Literal["no_candidates", "low_confidence"], int] = defaultdict(
                    int
                )
                while idx < n and not candidates_per_pt[idx]:
                    r = unmatched_reasons[idx]
                    if r is not None:
                        reason_counts[r] += 1
                    idx += 1
                block_indices = list(range(start, idx))
                if len(block_indices) >= 2:
                    block_len_m = sum(
                        math.hypot(
                            xys[block_indices[j]][0] - xys[block_indices[j - 1]][0],
                            xys[block_indices[j]][1] - xys[block_indices[j - 1]][1],
                        )
                        for j in range(1, len(block_indices))
                    )
                    if block_len_m >= self.min_unmatched_length_m:
                        dominant_reason: Literal["no_candidates", "low_confidence"] = max(
                            reason_counts.items(),
                            key=lambda kv: kv[1],
                            default=("no_candidates", 1),
                        )[0]
                        unmatched_subtraces.append(
                            UnmatchedSubtrace(
                                coordinates=[
                                    (points[k].lon, points[k].lat) for k in block_indices
                                ],
                                length_m=round(block_len_m, 2),
                                started_at=points[block_indices[0]].timestamp,
                                ended_at=points[block_indices[-1]].timestamp,
                                reason=dominant_reason,
                            )
                        )

        matched_traversals: list[MatchedSegmentTraversal] = []
        matched_confs: list[float] = []
        for chain in all_chains:
            matched_confs.extend(obs.emission_conf for obs in chain)
            matched_traversals.extend(
                self._aggregate_chain_traversals(chain, segments, graph, bearings)
            )

        matched_pts = len(matched_confs)
        matched_fraction = round(max(0.0, min(1.0, matched_pts / n)), 4)
        overall_confidence = (
            round(max(0.0, min(1.0, sum(matched_confs) / matched_pts)), 4)
            if matched_pts > 0
            else 0.0
        )

        return MatchResult(
            matcher_name=self.name,
            matched_segments=matched_traversals,
            unmatched_subtraces=unmatched_subtraces,
            overall_confidence=overall_confidence,
            matched_fraction=matched_fraction,
        )

    async def match_trace(
        self,
        points: Sequence[GpsPoint],
        *,
        search_radius_m: float = 45.0,
        min_confidence: float = 0.45,
    ) -> MatchResult:
        """Snap a privacy-trimmed GPS sequence onto active vehicular canonical track_segments."""
        return self.match_trace_sync(
            points,
            search_radius_m=search_radius_m,
            min_confidence=min_confidence,
        )


# Backwards-compatible alias matching docs/SPEC_GIS_SCHEMA.md Section 4.1 naming
PostGISHMMTrackMatcher = PostGISHMMMatcher
