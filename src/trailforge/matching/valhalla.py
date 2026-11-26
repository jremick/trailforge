"""Pluggable Valhalla `/trace_attributes` map-matcher sidecar plugin.

Implements TrailForge's ``TrackMatcher`` protocol without altering ingestion or
grading logic:
  1. Sends privacy-trimmed GPS points to Valhalla ``POST {valhalla_url}/trace_attributes``
     with 4WD/off-road costing (``use_tracks=1.0, use_ferry=0.5``) and ``map_snap``.
  2. Decodes Valhalla's Polyline6 matched shape (pure Python, zero external deps).
  3. Projects Valhalla-snapped subtraces onto TrailForge's canonical 50-100 m
     ``track_segments`` in PostGIS so telemetry & grades stay 100% decoupled from
     OSM way IDs (strict ODbL & Slice 02 canonical segment_id separation).
  4. Extracts Valhalla-unmatched and off-network spurs (>= 100 m) into
     ``UnmatchedSubtrace`` records cleanly.
  5. Gracefully falls back to ``PostGISHMMMatcher`` on HTTP error, timeout, or
     connection failure when ``fallback_to_hmm=True`` (marking diagnostic
     ``matcher_name="postgis_hmm_fallback"`` / ``last_matcher_backend``).
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, Literal

import httpx
from shapely import STRtree
from shapely.geometry import LineString, Point
from sqlalchemy.orm import Session

from trailforge.matching.hmm import (
    PostGISHMMMatcher,
    _acute_bidirectional_diff_deg,
    _CandidateState,
    _CorridorGraph,
    _CorridorSegment,
    _DecodedObs,
    _local_point_bearings,
    _wgs84_to_albers,
)
from trailforge.matching.protocol import (
    GpsPoint,
    MatchedSegmentTraversal,
    MatchResult,
    UnmatchedSubtrace,
)

logger = logging.getLogger(__name__)

_VALHALLA_INVALID_EDGE_INDEX = 0xFFFFFFFE


class ValhallaMatcherError(RuntimeError):
    """Raised when Valhalla matching fails and HMM fallback is disabled."""


def encode_polyline6(
    coords_latlon: Sequence[tuple[float, float]],
    *,
    precision: int = 6,
) -> str:
    """Encode a sequence of ``(lat, lon)`` pairs using Valhalla Polyline6 format."""
    if not coords_latlon:
        return ""
    factor = 10**precision
    out: list[str] = []
    prev_lat = 0
    prev_lon = 0

    def _encode_delta(delta: int) -> None:
        sv = ~(delta << 1) if delta < 0 else (delta << 1)
        while sv >= 0x20:
            out.append(chr((0x20 | (sv & 0x1F)) + 63))
            sv >>= 5
        out.append(chr(sv + 63))

    for lat, lon in coords_latlon:
        if not (math.isfinite(lat) and math.isfinite(lon)):
            raise ValueError(f"Non-finite coordinate in Polyline6 encoding: ({lat!r}, {lon!r})")
        ilat = round(lat * factor)
        ilon = round(lon * factor)
        _encode_delta(ilat - prev_lat)
        _encode_delta(ilon - prev_lon)
        prev_lat, prev_lon = ilat, ilon

    return "".join(out)


def decode_polyline6(encoded: str, *, precision: int = 6) -> list[tuple[float, float]]:
    """Decode a Valhalla Polyline6 string into ``[(lat, lon), ...]`` in WGS84 degrees.

    Pure-Python implementation with zero external dependencies. Raises ``ValueError``
    on truncated varints or invalid ASCII bytes below ``'?'`` (63).
    """
    if not encoded:
        return []

    factor = float(10**precision)
    coords: list[tuple[float, float]] = []
    lat = 0
    lon = 0
    idx = 0
    n = len(encoded)

    def _read_varint() -> int:
        nonlocal idx
        shift = 0
        result = 0
        while True:
            if idx >= n:
                raise ValueError("Truncated Polyline6 stream")
            raw_byte = ord(encoded[idx]) - 63
            if raw_byte < 0 or raw_byte > 63:
                raise ValueError(f"Invalid Polyline6 character at index {idx}: {encoded[idx]!r}")
            idx += 1
            result |= (raw_byte & 0x1F) << shift
            shift += 5
            if raw_byte < 0x20:
                break
        return ~(result >> 1) if (result & 1) else (result >> 1)

    while idx < n:
        dlat = _read_varint()
        dlon = _read_varint()
        lat += dlat
        lon += dlon
        coords.append((round(lat / factor, precision), round(lon / factor, precision)))

    return coords


def _is_valid_edge_index(edge_idx: Any, edge_count: int) -> bool:
    if edge_idx is None:
        return True
    if isinstance(edge_idx, bool) or not isinstance(edge_idx, int):
        return False
    if edge_idx < 0 or edge_idx >= _VALHALLA_INVALID_EDGE_INDEX:
        return False
    return not (edge_count > 0 and edge_idx >= edge_count)


class ValhallaTrackMatcher:
    """Pluggable Valhalla `/trace_attributes` matcher implementing ``TrackMatcher``."""

    name: str = "valhalla"

    def __init__(
        self,
        session: Session | None = None,
        *,
        valhalla_url: str = "http://valhalla:8002",
        timeout_s: float = 10.0,
        fallback_to_hmm: bool = True,
        http_client: httpx.Client | None = None,
        gps_accuracy_m: float = 12.0,
        max_candidates: int = 5,
        max_graph_hops: int = 4,
        min_unmatched_length_m: float = 100.0,
        fallback_matcher: PostGISHMMMatcher | None = None,
    ) -> None:
        self._session = session
        self.valhalla_url = valhalla_url.rstrip("/")
        self.timeout_s = float(timeout_s)
        self.fallback_to_hmm = bool(fallback_to_hmm)
        self._http_client = http_client
        self.gps_accuracy_m = float(gps_accuracy_m)
        self.max_candidates = max_candidates
        self.max_graph_hops = max_graph_hops
        self.min_unmatched_length_m = min_unmatched_length_m
        self._fallback_matcher = fallback_matcher
        self.last_matcher_backend: str = "valhalla"

    def _get_fallback_matcher(self) -> PostGISHMMMatcher:
        if self._fallback_matcher is not None:
            return self._fallback_matcher
        if self._session is None:
            raise ValhallaMatcherError(
                "ValhallaTrackMatcher requires a SQLAlchemy Session for PostGIS segment lookup"
            )
        self._fallback_matcher = PostGISHMMMatcher(
            self._session,
            sigma_z_m=self.gps_accuracy_m,
            max_candidates=self.max_candidates,
            max_graph_hops=self.max_graph_hops,
            min_unmatched_length_m=self.min_unmatched_length_m,
        )
        return self._fallback_matcher

    def _fetch_corridor_segments(
        self,
        points: Sequence[GpsPoint],
        search_radius_m: float,
    ) -> list[_CorridorSegment]:
        """Load active vehicular canonical segments along the matched trace corridor."""
        return self._get_fallback_matcher()._fetch_corridor_segments(points, search_radius_m)

    def build_trace_attributes_payload(
        self,
        points: Sequence[GpsPoint],
        *,
        search_radius_m: float = 45.0,
    ) -> dict[str, Any]:
        """Build the Valhalla `/trace_attributes` request payload for 4WD/track matching."""
        return {
            "shape": [
                {
                    "lat": round(p.lat, 6),
                    "lon": round(p.lon, 6),
                    "time": round(p.timestamp.timestamp(), 3),
                }
                for p in points
            ],
            "costing": "auto",
            "costing_options": {
                "auto": {
                    "use_tracks": 1.0,
                    "use_ferry": 0.5,
                }
            },
            "shape_match": "map_snap",
            "trace_options": {
                "search_radius": float(search_radius_m),
                "gps_accuracy": float(self.gps_accuracy_m),
            },
            "filters": {
                "attributes": [
                    "edge.way_id",
                    "edge.length",
                    "edge.speed",
                    "edge.begin_shape_index",
                    "edge.end_shape_index",
                    "matched.point",
                    "matched.type",
                    "matched.edge_index",
                    "matched.distance_from_trace_point",
                    "matched.distance_along_edge",
                    "shape",
                ],
                "action": "include",
            },
        }

    def _call_valhalla(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        url = f"{self.valhalla_url}/trace_attributes"
        if self._http_client is not None:
            resp = self._http_client.post(url, json=payload, timeout=self.timeout_s)
        else:
            with httpx.Client(timeout=self.timeout_s) as client:
                resp = client.post(url, json=payload)

        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValhallaMatcherError("Valhalla /trace_attributes returned a non-object JSON body")
        return data

    def _extract_snapped_points(
        self,
        points: Sequence[GpsPoint],
        raw_xys: Sequence[tuple[float, float]],
        response_data: Mapping[str, Any],
        *,
        search_radius_m: float,
    ) -> tuple[
        list[GpsPoint | None],
        list[tuple[float, float] | None],
        list[float | None],
        LineString | None,
    ]:
        """Parse Valhalla Polyline6 shape & `matched_points` into snapped per-fix geometry."""
        fwd = _wgs84_to_albers()

        shape_raw = response_data.get("shape")
        shape_latlon: list[tuple[float, float]] = []
        if isinstance(shape_raw, str) and shape_raw.strip():
            shape_latlon = decode_polyline6(shape_raw.strip())

        shape_3577: LineString | None = None
        if len(shape_latlon) >= 2:
            s_xs, s_ys = fwd.transform(
                [lon for _, lon in shape_latlon],
                [lat for lat, _ in shape_latlon],
            )
            shape_3577 = LineString(list(zip(s_xs, s_ys, strict=True)))

        raw_mps = response_data.get("matched_points")
        edges = response_data.get("edges")
        edge_count = len(edges) if isinstance(edges, list) else 0

        if not shape_latlon and not isinstance(raw_mps, list):
            raise ValhallaMatcherError(
                "Valhalla response contained neither a Polyline6 'shape' nor 'matched_points'"
            )

        snapped_points: list[GpsPoint | None] = []
        snapped_xys: list[tuple[float, float] | None] = []
        trace_dists_m: list[float | None] = []

        if isinstance(raw_mps, list) and len(raw_mps) == len(points):
            for idx, (pt, mp) in enumerate(zip(points, raw_mps, strict=True)):
                if not isinstance(mp, Mapping):
                    snapped_points.append(None)
                    snapped_xys.append(None)
                    trace_dists_m.append(None)
                    continue

                mp_type = str(mp.get("type", "matched")).strip().lower()
                edge_idx = mp.get("edge_index")
                if mp_type == "unmatched" or not _is_valid_edge_index(edge_idx, edge_count):
                    snapped_points.append(None)
                    snapped_xys.append(None)
                    trace_dists_m.append(None)
                    continue

                lat_raw = mp.get("lat")
                lon_raw = mp.get("lon")
                if (
                    isinstance(lat_raw, (int, float))
                    and isinstance(lon_raw, (int, float))
                    and math.isfinite(lat_raw)
                    and math.isfinite(lon_raw)
                ):
                    slat = float(lat_raw)
                    slon = float(lon_raw)
                    sx, sy = fwd.transform(slon, slat)
                    sx_f, sy_f = float(sx), float(sy)
                elif shape_3577 is not None:
                    rx, ry = raw_xys[idx]
                    proj_pt = shape_3577.interpolate(shape_3577.project(Point(rx, ry)))
                    sx_f, sy_f = float(proj_pt.x), float(proj_pt.y)
                    slat, slon = pt.lat, pt.lon
                else:
                    snapped_points.append(None)
                    snapped_xys.append(None)
                    trace_dists_m.append(None)
                    continue

                raw_dist = mp.get("distance_from_trace_point")
                if (
                    isinstance(raw_dist, (int, float))
                    and not isinstance(raw_dist, bool)
                    and math.isfinite(raw_dist)
                    and raw_dist >= 0.0
                ):
                    d_trace = float(raw_dist)
                else:
                    d_trace = math.hypot(sx_f - raw_xys[idx][0], sy_f - raw_xys[idx][1])

                snapped_points.append(pt.model_copy(update={"lat": slat, "lon": slon}))
                snapped_xys.append((sx_f, sy_f))
                trace_dists_m.append(d_trace)
        elif shape_3577 is not None:
            # Polyline6-only response: snap raw fixes within search_radius_m onto decoded shape
            shape_wgs84 = LineString([(lon, lat) for lat, lon in shape_latlon])
            for idx, pt in enumerate(points):
                rx, ry = raw_xys[idx]
                p_geom = Point(rx, ry)
                dist_m = float(p_geom.distance(shape_3577))
                if dist_m > search_radius_m:
                    snapped_points.append(None)
                    snapped_xys.append(None)
                    trace_dists_m.append(None)
                    continue
                proj_m = float(shape_3577.project(p_geom))
                frac = max(0.0, min(1.0, proj_m / shape_3577.length)) if shape_3577.length else 0.0
                proj_3577 = shape_3577.interpolate(proj_m)
                proj_wgs = shape_wgs84.interpolate(frac, normalized=True)
                snapped_points.append(
                    pt.model_copy(update={"lat": float(proj_wgs.y), "lon": float(proj_wgs.x)})
                )
                snapped_xys.append((float(proj_3577.x), float(proj_3577.y)))
                trace_dists_m.append(dist_m)
        else:
            raise ValhallaMatcherError(
                "Valhalla matched_points length mismatch and no Polyline6 shape available"
            )

        return snapped_points, snapped_xys, trace_dists_m, shape_3577

    def _project_snapped_to_canonical(
        self,
        raw_points: Sequence[GpsPoint],
        raw_xys: Sequence[tuple[float, float]],
        snapped_points: Sequence[GpsPoint | None],
        snapped_xys: Sequence[tuple[float, float] | None],
        trace_dists_m: Sequence[float | None],
        shape_3577: LineString | None,
        *,
        search_radius_m: float,
        min_confidence: float,
    ) -> MatchResult:
        """Project Valhalla-matched subtraces onto TrailForge canonical `track_segments`."""
        n = len(raw_points)
        corridor_points = [
            sp if sp is not None else rp
            for rp, sp in zip(raw_points, snapped_points, strict=True)
        ]
        corridor_xys = [
            sxy if sxy is not None else rxy
            for rxy, sxy in zip(raw_xys, snapped_xys, strict=True)
        ]
        bearings = _local_point_bearings(corridor_xys)

        segments = self._fetch_corridor_segments(corridor_points, search_radius_m)
        tree = STRtree([seg.geom_3577 for seg in segments]) if segments else None

        # Precompute alignment of each canonical segment to Valhalla's decoded Polyline6
        seg_on_shape: list[bool] = []
        if shape_3577 is not None:
            for seg in segments:
                mid = seg.geom_3577.interpolate(0.5, normalized=True)
                seg_on_shape.append(float(mid.distance(shape_3577)) <= max(search_radius_m, 25.0))
        else:
            seg_on_shape = [True] * len(segments)

        candidates_per_pt: list[list[_CandidateState]] = []
        unmatched_reasons: list[Literal["no_candidates", "low_confidence"] | None] = []

        for idx, pt in enumerate(raw_points):
            sxy = snapped_xys[idx]
            if sxy is None or tree is None:
                candidates_per_pt.append([])
                unmatched_reasons.append("no_candidates")
                continue

            sx, sy = sxy
            p_geom = Point(sx, sy)
            sigma = max(self.gps_accuracy_m, pt.accuracy_m or self.gps_accuracy_m)
            pt_bearing = bearings[idx]
            d_val = trace_dists_m[idx] or 0.0

            nearby = tree.query(p_geom.buffer(search_radius_m))
            raw_cands: list[_CandidateState] = []
            had_any = False

            for seg_idx_raw in nearby:
                seg_idx = int(seg_idx_raw)
                seg = segments[seg_idx]
                perp_d = float(p_geom.distance(seg.geom_3577))
                if perp_d > search_radius_m:
                    continue
                had_any = True

                proj_d = float(seg.geom_3577.project(p_geom))
                proj_frac = max(0.0, min(1.0, proj_d / seg.length_m)) if seg.length_m > 0 else 0.0

                eff_dist = math.hypot(d_val, perp_d)
                dist_prob = math.exp(-0.5 * (eff_dist / sigma) ** 2)
                if pt_bearing is not None:
                    ang_diff = _acute_bidirectional_diff_deg(pt_bearing, seg.bearing_deg)
                    cos_sq = math.cos(math.radians(ang_diff)) ** 2
                    heading_score = 0.75 + 0.25 * cos_sq
                    viterbi_heading = 0.35 + 0.65 * cos_sq
                else:
                    heading_score = 1.0
                    viterbi_heading = 1.0

                emission_conf = max(0.0, min(1.0, dist_prob * heading_score))
                shape_bonus = 0.25 if seg_on_shape[seg_idx] else -0.5
                log_emission = (
                    -0.5 * (eff_dist / sigma) ** 2
                    + math.log(max(1e-6, viterbi_heading))
                    + shape_bonus
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
                unmatched_reasons.append("low_confidence" if had_any else "no_candidates")

        graph = _CorridorGraph(segments, max_hops=self.max_graph_hops)
        helper = self._get_fallback_matcher()
        all_chains: list[list[_DecodedObs]] = []
        unmatched_subtraces: list[UnmatchedSubtrace] = []

        idx = 0
        while idx < n:
            if candidates_per_pt[idx]:
                start = idx
                while idx < n and candidates_per_pt[idx]:
                    idx += 1
                eligible_run = list(range(start, idx))
                chains = helper._run_viterbi_chain(
                    eligible_run, corridor_points, corridor_xys, candidates_per_pt, graph
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
                            raw_xys[block_indices[j]][0] - raw_xys[block_indices[j - 1]][0],
                            raw_xys[block_indices[j]][1] - raw_xys[block_indices[j - 1]][1],
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
                                    (raw_points[k].lon, raw_points[k].lat) for k in block_indices
                                ],
                                length_m=round(block_len_m, 2),
                                started_at=raw_points[block_indices[0]].timestamp,
                                ended_at=raw_points[block_indices[-1]].timestamp,
                                reason=dominant_reason,
                            )
                        )

        matched_traversals: list[MatchedSegmentTraversal] = []
        matched_confs: list[float] = []
        for chain in all_chains:
            matched_confs.extend(obs.emission_conf for obs in chain)
            matched_traversals.extend(
                helper._aggregate_chain_traversals(chain, segments, graph, bearings)
            )

        matched_pts = len(matched_confs)
        matched_fraction = round(max(0.0, min(1.0, matched_pts / n)), 4)
        overall_confidence = (
            round(max(0.0, min(1.0, sum(matched_confs) / matched_pts)), 4)
            if matched_pts > 0
            else 0.0
        )

        self.last_matcher_backend = "valhalla"
        return MatchResult(
            matcher_name=self.name,
            matched_segments=matched_traversals,
            unmatched_subtraces=unmatched_subtraces,
            overall_confidence=overall_confidence,
            matched_fraction=matched_fraction,
        )

    def match_trace_sync(
        self,
        points: Sequence[GpsPoint],
        *,
        search_radius_m: float = 45.0,
        min_confidence: float = 0.45,
    ) -> MatchResult:
        """Synchronously map-match via Valhalla `/trace_attributes` with optional HMM fallback."""
        if len(points) < 2:
            self.last_matcher_backend = "valhalla"
            return MatchResult(
                matcher_name=self.name,
                matched_segments=[],
                unmatched_subtraces=[],
                overall_confidence=0.0,
                matched_fraction=0.0,
            )

        payload = self.build_trace_attributes_payload(points, search_radius_m=search_radius_m)

        try:
            response_data = self._call_valhalla(payload)
            fwd = _wgs84_to_albers()
            xs, ys = fwd.transform([p.lon for p in points], [p.lat for p in points])
            raw_xys = [(float(x), float(y)) for x, y in zip(xs, ys, strict=True)]

            snapped_pts, snapped_xys, trace_dists, shape_3577 = self._extract_snapped_points(
                points,
                raw_xys,
                response_data,
                search_radius_m=search_radius_m,
            )
        except (httpx.HTTPError, ValueError, ValhallaMatcherError) as exc:
            if not self.fallback_to_hmm:
                raise ValhallaMatcherError(
                    f"Valhalla map-matching failed at {self.valhalla_url}: {exc}"
                ) from exc

            logger.warning(
                "Valhalla /trace_attributes unavailable; falling back to PostGISHMMMatcher",
                extra={
                    "valhalla_url": self.valhalla_url,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "point_count": len(points),
                    "matcher_backend": "postgis_hmm_fallback",
                },
            )
            fallback_res = self._get_fallback_matcher().match_trace_sync(
                points,
                search_radius_m=search_radius_m,
                min_confidence=min_confidence,
            )
            self.last_matcher_backend = "postgis_hmm_fallback"
            return fallback_res.model_copy(update={"matcher_name": "postgis_hmm_fallback"})

        return self._project_snapped_to_canonical(
            points,
            raw_xys,
            snapped_pts,
            snapped_xys,
            trace_dists,
            shape_3577,
            search_radius_m=search_radius_m,
            min_confidence=min_confidence,
        )

    async def match_trace(
        self,
        points: Sequence[GpsPoint],
        *,
        search_radius_m: float = 45.0,
        min_confidence: float = 0.45,
    ) -> MatchResult:
        """Snap a privacy-trimmed GPS sequence via Valhalla onto canonical `track_segments`."""
        return self.match_trace_sync(
            points,
            search_radius_m=search_radius_m,
            min_confidence=min_confidence,
        )
