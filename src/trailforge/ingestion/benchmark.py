"""Offline 10,000+ segment scale, memory (<300 MB), & PostGIS GIST index benchmark.

Generates a realistic 150-track / 10,000+ canonical segment VIC/NSW network offline and proves:
  1. Peak Python heap (``tracemalloc``) stays well under ``< 300 MB`` across conflation,
     canonical 50-100 m segmentation, and streaming GeoJSONSeq export.
  2. PostGIS ``EXPLAIN (ANALYZE, FORMAT JSON)`` uses GIST index scans (``Index Scan`` or
     ``Bitmap Index Scan`` on ``ix_track_segments_geom*``) with ZERO ``Seq Scan`` on
     ``track_segments`` for:
       - Spatial viewport bbox queries (``ST_Intersects``)
       - HMM ``ST_DWithin`` corridor candidate lookup (``matching/hmm.py`` SQL)
       - Closure alert spatial joins (``closures_alerts`` JOIN ``track_segments``)

Usage:
    python -W error::RuntimeWarning -m trailforge.ingestion.benchmark
    python -W error::RuntimeWarning -m trailforge.ingestion.benchmark --skip-db
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import tempfile
import time
import tracemalloc
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from geoalchemy2.shape import from_shape
from pydantic import BaseModel, Field
from shapely.geometry import LineString, Polygon, mapping
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from trailforge.db.models import ClosureAlert, Track, TrackSegment, TrackSegmentGrade
from trailforge.db.session import get_engine
from trailforge.ingestion.conflate import conflate_tracks
from trailforge.ingestion.connectors.base import RawTrackFeature
from trailforge.ingestion.live_bake import get_peak_rss_mb
from trailforge.ingestion.segmenter import (
    CanonicalSegmentCandidate,
    CanonicalTrackGroup,
    segment_conflated_ways,
)

DEFAULT_NUM_TRACKS: Final[int] = 150
DEFAULT_SEGMENTS_PER_TRACK: Final[int] = 72
DEFAULT_OVERLAP_DUPLICATES: Final[int] = 25
MEMORY_HEAP_BUDGET_MB: Final[float] = 300.0
BENCH_TRACK_PREFIX: Final[str] = "tftrk_bench_"
BENCH_SEG_PREFIX: Final[str] = "tfseg_bench_"

_GIST_INDEX_PREFIXES: Final[tuple[str, ...]] = (
    "ix_track_segments_geom",
    "ix_track_segments_geom_active",
)


class MemoryScaleMetrics(BaseModel):
    """Offline conflation, segmentation, and streaming export memory/latency metrics."""

    input_features: int
    conflated_ways: int
    deduplicated_ways: int
    tracks_generated: int
    segments_generated: int
    generate_ms: float
    conflate_ms: float
    segment_ms: float
    export_ms: float
    total_pipeline_ms: float
    exported_bytes: int
    peak_heap_mb: float
    current_heap_mb: float
    peak_rss_mb: float
    within_300mb_budget: bool


class ExplainPlanAudit(BaseModel):
    """Structured audit of a PostgreSQL ``EXPLAIN (ANALYZE, FORMAT JSON)`` plan."""

    query_name: str
    planning_time_ms: float
    execution_time_ms: float
    actual_rows: int
    uses_gist_index: bool
    has_seq_scan_on_target: bool
    indexes_used: list[str] = Field(default_factory=list)
    target_scan_nodes: list[str] = Field(default_factory=list)


class PostGISIndexBenchmarkResult(BaseModel):
    """Structured PostGIS bulk load and GIST index verification results."""

    tracks_inserted: int
    segments_inserted: int
    closures_inserted: int
    bulk_insert_ms: float
    analyze_ms: float
    rolled_back: bool
    all_queries_use_gist: bool
    zero_seq_scans_on_track_segments: bool
    queries: list[ExplainPlanAudit] = Field(default_factory=list)


class FullScaleBenchmarkReport(BaseModel):
    """Top-level JSON report emitted by ``python -m trailforge.ingestion.benchmark``."""

    generated_at: str
    memory_budget_mb: float = MEMORY_HEAP_BUDGET_MB
    memory: MemoryScaleMetrics
    postgis: PostGISIndexBenchmarkResult | None = None


def generate_synthetic_vic_nsw_network(
    *,
    num_tracks: int = DEFAULT_NUM_TRACKS,
    segments_per_track: int = DEFAULT_SEGMENTS_PER_TRACK,
    overlap_duplicate_tracks: int = DEFAULT_OVERLAP_DUPLICATES,
    seed: int = 42,
) -> list[RawTrackFeature]:
    """Generate a realistic offline VIC/NSW 4WD track network yielding ~10,000+ canonical segments.

    Spreads tracks across a 3.6° × 3.2° VIC/NSW High Country & Tablelands grid so every track
    is ~5.4 km long (~72 canonical 75 m segments -> 10,800 segments for 150 tracks), plus
    overlapping lower-priority VIC/NSW state duplicates to exercise multi-source conflation.
    """
    features: list[RawTrackFeature] = []
    # Grid across VIC High Country + NSW Snowy Mountains / Tablelands
    cols = max(1, int(math.ceil(math.sqrt(num_tracks))))
    rows = max(1, int(math.ceil(num_tracks / cols)))
    origin_lon = 146.60
    origin_lat = -37.45
    cell_lon = 3.40 / max(1, cols)
    cell_lat = 3.10 / max(1, rows)

    # Target track length ~ segments_per_track * 75 m (~5.4 km -> ~0.060 deg lon at -36 deg lat)
    target_len_m = float(segments_per_track) * 75.0
    approx_deg_lon = target_len_m / 90_000.0
    n_vertices = 16

    surfaces = ("gravel", "dirt", "rock", "clay")
    tracktypes = ("grade2", "grade3", "grade4", "grade5")

    for i in range(num_tracks):
        gx = i % cols
        gy = i // cols
        base_lon = origin_lon + gx * cell_lon + 0.015
        base_lat = origin_lat + gy * cell_lat + 0.015

        phase = ((i * 37 + seed * 13) % 360) * math.pi / 180.0
        amp_lat = 0.0022 * (1.0 + (i % 3) * 0.25)
        drift_lat = 0.004 * (1 if i % 2 == 0 else -1)

        coords: list[list[float]] = []
        for k in range(n_vertices):
            frac = k / (n_vertices - 1)
            lon = round(base_lon + frac * approx_deg_lon, 6)
            lat = round(
                base_lat + frac * drift_lat + amp_lat * math.sin(phase + frac * 2.0 * math.pi),
                6,
            )
            coords.append([lon, lat])

        surf = surfaces[i % len(surfaces)]
        tt = tracktypes[i % len(tracktypes)]
        is_fire = (i % 5) == 0
        tclass = "fire_trail" if is_fire else "4wd_track"
        track_name = (
            f"Bench Fire Trail {i:03d}" if is_fire else f"Bench High Country Track {i:03d}"
        )

        features.append(
            RawTrackFeature(
                source="osm",
                source_layer="osm",
                source_id=f"way/{900_000 + i}",
                osm_way_id=900_000 + i,
                osm_version=1,
                highway="track",
                name=track_name,
                trail_class=tclass,  # type: ignore[arg-type]
                is_vehicular=True,
                surface_class=(
                    "gravel_compacted"
                    if surf == "gravel"
                    else ("rough_rock_mud_sand" if surf in ("rock", "clay") else "dirt_unsealed")
                ),
                surface_raw=surf,
                tracktype=tt,  # type: ignore[arg-type]
                is_4wd_only=(tt in ("grade4", "grade5")),
                is_4wd_recommended=(tt == "grade3"),
                access_status="public",
                geometry={"type": "LineString", "coordinates": coords},
            )
        )

        # Add overlapping lower-priority VIC/NSW duplicate tracks (~4 m offset) for conflation test
        if i < overlap_duplicate_tracks:
            is_vic = base_lat < -36.50
            dup_coords = [[round(c[0] + 0.00003, 6), round(c[1] + 0.00003, 6)] for c in coords]
            features.append(
                RawTrackFeature(
                    source="vic" if is_vic else "nsw",
                    source_layer="tr_road" if is_vic else "nsw_fire_trails_l9",
                    source_id=f"{'vic' if is_vic else 'nsw'}_dup_{i:03d}",
                    name=f"{track_name} State Ref",
                    trail_class=tclass,  # type: ignore[arg-type]
                    is_vehicular=True,
                    surface_class="dirt_unsealed",
                    surface_raw="unsealed",
                    access_status="public",
                    geometry={"type": "MultiLineString", "coordinates": [dup_coords]},
                )
            )

    return features


def _stream_export_segments_geojsonseq(
    segments: Sequence[CanonicalSegmentCandidate],
    out_path: Path,
) -> int:
    """Stream canonical segments line-by-line to GeoJSONSeq without building a giant in-RAM dict."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for seg in segments:
            feat = {
                "type": "Feature",
                "id": seg.segment_id,
                "geometry": mapping(seg.geom_wgs84),
                "properties": {
                    "segment_id": seg.segment_id,
                    "track_id": seg.track_id,
                    "name": seg.name,
                    "state": seg.state,
                    "trail_class": seg.trail_class,
                    "surface_class": seg.surface_class,
                    "length_m": seg.length_m,
                    "bearing_deg": seg.bearing_deg,
                },
            }
            fh.write("\x1e")
            fh.write(json.dumps(feat, separators=(",", ":")))
            fh.write("\n")
    return int(out_path.stat().st_size)


def run_memory_scale_benchmark(
    *,
    num_tracks: int = DEFAULT_NUM_TRACKS,
    segments_per_track: int = DEFAULT_SEGMENTS_PER_TRACK,
    overlap_duplicate_tracks: int = DEFAULT_OVERLAP_DUPLICATES,
    export_dir: Path | None = None,
    seed: int = 42,
) -> tuple[MemoryScaleMetrics, list[CanonicalTrackGroup], list[CanonicalSegmentCandidate]]:
    """Run offline 10k+ segment conflation, segmentation, and streaming export with tracemalloc."""
    gc.collect()
    was_tracing = tracemalloc.is_tracing()
    if was_tracing:
        tracemalloc.reset_peak()
    else:
        tracemalloc.start()

    t_all = time.perf_counter()
    try:
        t0 = time.perf_counter()
        raw_features = generate_synthetic_vic_nsw_network(
            num_tracks=num_tracks,
            segments_per_track=segments_per_track,
            overlap_duplicate_tracks=overlap_duplicate_tracks,
            seed=seed,
        )
        generate_ms = round((time.perf_counter() - t0) * 1000.0, 2)

        t1 = time.perf_counter()
        conflation = conflate_tracks(raw_features)
        conflate_ms = round((time.perf_counter() - t1) * 1000.0, 2)

        t2 = time.perf_counter()
        tracks, segments = segment_conflated_ways(conflation.ways)
        segment_ms = round((time.perf_counter() - t2) * 1000.0, 2)

        t3 = time.perf_counter()
        if export_dir is not None:
            exported_bytes = _stream_export_segments_geojsonseq(
                segments, export_dir / "bench_segments.geojsonseq"
            )
        else:
            with tempfile.TemporaryDirectory(prefix="tf_bench_export_") as tmp_s:
                exported_bytes = _stream_export_segments_geojsonseq(
                    segments, Path(tmp_s) / "bench_segments.geojsonseq"
                )
        export_ms = round((time.perf_counter() - t3) * 1000.0, 2)

        curr_bytes, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        if not was_tracing:
            tracemalloc.stop()

    total_ms = round((time.perf_counter() - t_all) * 1000.0, 2)
    peak_heap_mb = round(peak_bytes / (1024.0 * 1024.0), 2)
    current_heap_mb = round(curr_bytes / (1024.0 * 1024.0), 2)

    metrics = MemoryScaleMetrics(
        input_features=len(raw_features),
        conflated_ways=len(conflation.ways),
        deduplicated_ways=conflation.deduplicated_count,
        tracks_generated=len(tracks),
        segments_generated=len(segments),
        generate_ms=generate_ms,
        conflate_ms=conflate_ms,
        segment_ms=segment_ms,
        export_ms=export_ms,
        total_pipeline_ms=total_ms,
        exported_bytes=exported_bytes,
        peak_heap_mb=peak_heap_mb,
        current_heap_mb=current_heap_mb,
        peak_rss_mb=get_peak_rss_mb(),
        within_300mb_budget=peak_heap_mb < MEMORY_HEAP_BUDGET_MB,
    )
    return (metrics, tracks, segments)


def analyze_explain_plan(
    query_name: str,
    explain_output: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    *,
    target_relation: str = "track_segments",
) -> ExplainPlanAudit:
    """Recursively audit a PostgreSQL ``EXPLAIN (ANALYZE, FORMAT JSON)`` output tree."""
    root = explain_output[0] if isinstance(explain_output, (list, tuple)) else explain_output
    plan = root.get("Plan") or {}
    planning_ms = round(float(root.get("Planning Time") or 0.0), 3)
    execution_ms = round(float(root.get("Execution Time") or 0.0), 3)
    actual_rows = int(plan.get("Actual Rows") or 0)

    target_scan_nodes: list[str] = []
    indexes_used: list[str] = []
    uses_gist = False
    has_seq_scan = False

    def _walk(node: Mapping[str, Any], inherited_rel: str | None = None) -> None:
        nonlocal uses_gist, has_seq_scan
        node_type = str(node.get("Node Type") or "")
        rel_name = node.get("Relation Name")
        index_name = node.get("Index Name")
        effective_rel = str(rel_name) if rel_name is not None else inherited_rel

        if rel_name == target_relation:
            desc = f"{node_type}({index_name})" if index_name else node_type
            target_scan_nodes.append(desc)
            if node_type == "Seq Scan":
                has_seq_scan = True

        if index_name is not None:
            idx_str = str(index_name)
            if idx_str not in indexes_used and (
                rel_name == target_relation
                or effective_rel == target_relation
                or idx_str.startswith(_GIST_INDEX_PREFIXES)
            ):
                indexes_used.append(idx_str)
            if (
                node_type in ("Index Scan", "Index Only Scan", "Bitmap Index Scan")
                and idx_str.startswith(_GIST_INDEX_PREFIXES)
            ):
                uses_gist = True
                if rel_name != target_relation:
                    target_scan_nodes.append(f"{node_type}({idx_str})")

        for child in node.get("Plans") or []:
            if isinstance(child, Mapping):
                _walk(child, effective_rel)

    _walk(plan)

    return ExplainPlanAudit(
        query_name=query_name,
        planning_time_ms=planning_ms,
        execution_time_ms=execution_ms,
        actual_rows=actual_rows,
        uses_gist_index=uses_gist,
        has_seq_scan_on_target=has_seq_scan,
        indexes_used=indexes_used,
        target_scan_nodes=target_scan_nodes,
    )


def _bulk_insert_benchmark_network(
    session: Session,
    tracks: Sequence[CanonicalTrackGroup],
    segments: Sequence[CanonicalSegmentCandidate],
    *,
    batch_size: int = 1000,
) -> tuple[int, int, int, list[tuple[float, float]]]:
    """Batch-insert benchmark tracks, 10k+ segments, grades, and sample closures."""
    now = datetime.now(UTC)

    track_id_map: dict[str, str] = {}
    track_rows: list[dict[str, Any]] = []
    for idx, trk in enumerate(tracks):
        bench_tid = f"{BENCH_TRACK_PREFIX}{idx:04d}_{trk.track_id[-8:]}"
        track_id_map[trk.track_id] = bench_tid
        track_rows.append(
            {
                "track_id": bench_tid,
                "name": trk.name,
                "alt_names": trk.alt_names,
                "state": trk.state,
                "primary_source": trk.primary_source,
                "has_osm_source": trk.has_osm_source,
                "trail_class": trk.trail_class,
                "is_vehicular": trk.is_vehicular,
                "total_length_m": trk.total_length_m,
                "elev_min_m": 650.0,
                "elev_max_m": 1280.0,
                "elev_gain_m": 420.0,
                "max_slope_deg": 12.5,
                "waterway_crossing_count": 0,
                "bbox_geom": (
                    from_shape(trk.bbox_geom, srid=4326) if trk.bbox_geom is not None else None
                ),
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
        )

    for start in range(0, len(track_rows), batch_size):
        session.execute(
            pg_insert(Track)
            .values(track_rows[start : start + batch_size])
            .on_conflict_do_nothing(index_elements=[Track.track_id])
        )

    seg_rows: list[dict[str, Any]] = []
    grade_rows: list[dict[str, Any]] = []
    sample_centers: list[tuple[float, float]] = []

    for idx, seg in enumerate(segments):
        bench_sid = f"{BENCH_SEG_PREFIX}{idx:06d}_{seg.segment_id[-8:]}"
        bench_tid = track_id_map.get(seg.track_id) if seg.track_id else None
        if idx in (len(segments) // 4, len(segments) // 2, (3 * len(segments)) // 4):
            mid = seg.geom_wgs84.interpolate(0.5, normalized=True)
            sample_centers.append((float(mid.x), float(mid.y)))

        seg_rows.append(
            {
                "segment_id": bench_sid,
                "track_id": bench_tid,
                "segment_seq": seg.segment_seq,
                "geom_fingerprint": seg.geom_fingerprint,
                "geom": from_shape(seg.geom_wgs84, srid=4326),
                "start_node_key": seg.start_node_key,
                "end_node_key": seg.end_node_key,
                "length_m": seg.length_m,
                "bearing_deg": seg.bearing_deg,
                "state": seg.state,
                "name": seg.name,
                "trail_class": seg.trail_class,
                "is_vehicular": seg.is_vehicular,
                "surface_class": seg.surface_class,
                "tracktype": seg.tracktype,
                "is_4wd_only": seg.is_4wd_only,
                "is_4wd_recommended": seg.is_4wd_recommended,
                "access_status": seg.access_status,
                "has_gate_barrier": seg.has_gate_barrier,
                "is_bridged": seg.is_bridged,
                "elev_min_m": 820.0,
                "elev_max_m": 845.0,
                "elev_start_m": 820.0,
                "elev_end_m": 845.0,
                "elev_gain_m": 25.0,
                "elev_loss_m": 0.0,
                "max_slope_deg": 9.5,
                "sustained_slope_deg": 7.0,
                "is_steep_highlight": False,
                "is_above_snowline": False,
                "elevation_profile_m": [[0.0, 820.0], [37.5, 832.5], [75.0, 845.0]],
                "dem_tier": "synthetic",
                "waterway_crossing_count": 0,
                "has_perennial_crossing": False,
                "primary_source": seg.primary_source,
                "has_osm_source": seg.has_osm_source,
                "source_refs": seg.source_refs,
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
        )
        grade_rows.append(
            {
                "segment_id": bench_sid,
                "baseline_grade": "Medium",
                "baseline_score": 42.0,
                "baseline_breakdown": {"source": "benchmark", "score": 42.0},
                "effective_grade": "Medium",
                "confidence_level": "baseline_only",
                "confidence_score": 0.0,
                "sample_count": 0,
                "vehicle_class_split": {},
                "wet_risk_level": "dry",
                "baseline_computed_at": now,
            }
        )

    for start in range(0, len(seg_rows), batch_size):
        session.execute(
            pg_insert(TrackSegment)
            .values(seg_rows[start : start + batch_size])
            .on_conflict_do_nothing(index_elements=[TrackSegment.segment_id])
        )
        session.execute(
            pg_insert(TrackSegmentGrade)
            .values(grade_rows[start : start + batch_size])
            .on_conflict_do_nothing(index_elements=[TrackSegmentGrade.segment_id])
        )

    # Insert 3 sample active closure alerts spanning benchmark track clusters
    closure_rows: list[dict[str, Any]] = []
    for c_idx, (cx, cy) in enumerate(sample_centers):
        poly = Polygon(
            [
                (cx - 0.015, cy - 0.015),
                (cx + 0.015, cy - 0.015),
                (cx + 0.015, cy + 0.015),
                (cx - 0.015, cy + 0.015),
                (cx - 0.015, cy - 0.015),
            ]
        )
        closure_rows.append(
            {
                "source_feed": "vic_mapshare_seasonal",
                "external_id": f"bench-closure-{c_idx:03d}",
                "alert_type": "seasonal_closure",
                "severity": "closed",
                "status": "active",
                "title": f"Benchmark Seasonal Closure {c_idx}",
                "state": "VIC" if cy < -36.5 else "NSW",
                "geom": from_shape(poly, srid=4326),
                "last_seen_at": now,
                "raw_payload": {"benchmark": True},
                "created_at": now,
            }
        )

    if closure_rows:
        session.execute(
            pg_insert(ClosureAlert)
            .values(closure_rows)
            .on_conflict_do_nothing(constraint="uq_closures_alerts_feed_ext")
        )

    session.flush()
    return (len(track_rows), len(seg_rows), len(closure_rows), sample_centers)


def run_postgis_index_benchmark(
    session: Session,
    tracks: Sequence[CanonicalTrackGroup],
    segments: Sequence[CanonicalSegmentCandidate],
    *,
    rollback_after: bool = True,
) -> PostGISIndexBenchmarkResult:
    """Bulk-load 10k+ segments and verify PostGIS GIST index plans via EXPLAIN ANALYZE."""
    t_ins = time.perf_counter()
    n_tracks, n_segs, n_closures, centers = _bulk_insert_benchmark_network(
        session, tracks, segments
    )
    bulk_insert_ms = round((time.perf_counter() - t_ins) * 1000.0, 2)

    try:
        # Refresh planner statistics so PostgreSQL has accurate 10k+ row & GIST histograms
        t_an = time.perf_counter()
        session.execute(text("ANALYZE tracks"))
        session.execute(text("ANALYZE track_segments"))
        session.execute(text("ANALYZE track_segment_grades"))
        session.execute(text("ANALYZE closures_alerts"))
        analyze_ms = round((time.perf_counter() - t_an) * 1000.0, 2)

        cx, cy = centers[1] if len(centers) >= 2 else (148.20, -36.20)

        # 1. Spatial Viewport BBox Query (MapLibre vector tile / API bbox query)
        bbox_plan = session.execute(
            text(
                """
                EXPLAIN (ANALYZE, FORMAT JSON)
                SELECT ts.segment_id, ts.track_id, ts.name, ts.length_m
                FROM track_segments ts
                WHERE ts.is_active = TRUE
                  AND ST_Intersects(
                      ts.geom,
                      ST_MakeEnvelope(:min_x, :min_y, :max_x, :max_y, 4326)
                  )
                """
            ),
            {
                "min_x": cx - 0.04,
                "min_y": cy - 0.04,
                "max_x": cx + 0.04,
                "max_y": cy + 0.04,
            },
        ).scalar_one()

        # 2. HMM ST_DWithin Candidate Lookup (exact query from matching/hmm.py)
        trace_line = LineString([(cx - 0.01, cy - 0.002), (cx, cy), (cx + 0.01, cy + 0.002)])
        hmm_plan = session.execute(
            text(
                """
                EXPLAIN (ANALYZE, FORMAT JSON)
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
                    ts.bearing_deg
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
                "trace_wkt": trace_line.wkt,
                "expand_deg": 0.003,
                "corridor_radius_m": 60.0,
            },
        ).scalar_one()

        # 3. Closure Spatial Join (active closures_alerts joined onto track_segments via GIST)
        closure_plan = session.execute(
            text(
                """
                EXPLAIN (ANALYZE, FORMAT JSON)
                SELECT ts.segment_id, ca.id AS closure_id, ca.alert_type
                FROM closures_alerts ca
                JOIN track_segments ts
                  ON ts.is_active = TRUE
                 AND ts.geom && ST_Expand(ca.geom, :expand_deg)
                 AND ST_DWithin(
                     ST_Transform(ts.geom, 3577),
                     ST_Transform(ca.geom, 3577),
                     :buf_m
                 )
                WHERE ca.status = 'active'
                  AND ca.external_id LIKE 'bench-closure-%'
                  AND ca.geom IS NOT NULL
                """
            ),
            {
                "expand_deg": 0.002,
                "buf_m": 100.0,
            },
        ).scalar_one()

        audits = [
            analyze_explain_plan("spatial_bbox_query", bbox_plan),
            analyze_explain_plan("hmm_st_dwithin_lookup", hmm_plan),
            analyze_explain_plan("closure_spatial_join", closure_plan),
        ]
    finally:
        if rollback_after:
            session.rollback()

    return PostGISIndexBenchmarkResult(
        tracks_inserted=n_tracks,
        segments_inserted=n_segs,
        closures_inserted=n_closures,
        bulk_insert_ms=bulk_insert_ms,
        analyze_ms=analyze_ms,
        rolled_back=rollback_after,
        all_queries_use_gist=all(a.uses_gist_index for a in audits),
        zero_seq_scans_on_track_segments=all(not a.has_seq_scan_on_target for a in audits),
        queries=audits,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser for ``python -m trailforge.ingestion.benchmark``."""
    parser = argparse.ArgumentParser(
        prog="python -m trailforge.ingestion.benchmark",
        description=(
            "Run TrailForge 10,000+ segment scale memory (<300 MB) and PostGIS GIST index "
            "EXPLAIN ANALYZE benchmark."
        ),
    )
    parser.add_argument(
        "--num-tracks",
        type=int,
        default=DEFAULT_NUM_TRACKS,
        help=f"Number of synthetic VIC/NSW tracks to generate (default: {DEFAULT_NUM_TRACKS}).",
    )
    parser.add_argument(
        "--segments-per-track",
        type=int,
        default=DEFAULT_SEGMENTS_PER_TRACK,
        help=f"Target 75 m segments per track (default: {DEFAULT_SEGMENTS_PER_TRACK}).",
    )
    parser.add_argument(
        "--overlap-duplicates",
        type=int,
        default=DEFAULT_OVERLAP_DUPLICATES,
        help=(
            "Number of overlapping VIC/NSW duplicate tracks for conflation "
            f"(default: {DEFAULT_OVERLAP_DUPLICATES})."
        ),
    )
    parser.add_argument(
        "--skip-db",
        action="store_true",
        help="Run only the offline Python memory/conflation/segmentation/export benchmark.",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="Optional SQLAlchemy PostGIS URL (defaults to TEST_DATABASE_URL / DATABASE_URL).",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Optional file path to write the structured JSON benchmark report.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Execute the 10k+ segment benchmark and print structured JSON output."""
    parser = build_parser()
    args = parser.parse_args(argv)

    mem_metrics, tracks, segments = run_memory_scale_benchmark(
        num_tracks=args.num_tracks,
        segments_per_track=args.segments_per_track,
        overlap_duplicate_tracks=args.overlap_duplicates,
    )

    pg_result: PostGISIndexBenchmarkResult | None = None
    if not args.skip_db:
        db_url = args.database_url or os.environ.get("TEST_DATABASE_URL") or os.environ.get(
            "DATABASE_URL"
        )
        engine = get_engine(db_url)
        try:
            with Session(engine) as session:
                pg_result = run_postgis_index_benchmark(
                    session, tracks, segments, rollback_after=True
                )
        finally:
            engine.dispose()

    report = FullScaleBenchmarkReport(
        generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        memory=mem_metrics,
        postgis=pg_result,
    )
    payload = report.model_dump_json(indent=2)
    if args.output_json is not None:
        out_p = args.output_json.expanduser().resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(payload + "\n", encoding="utf-8")
    print(payload)

    ok = mem_metrics.within_300mb_budget and mem_metrics.segments_generated >= 10_000
    if pg_result is not None:
        ok = (
            ok
            and pg_result.all_queries_use_gist
            and pg_result.zero_seq_scans_on_track_segments
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
