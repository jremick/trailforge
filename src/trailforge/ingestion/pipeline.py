"""Idempotent PostGIS ingestion pipeline for Slice 02 (Tier A -> Tier B -> Tier C).

Usage:
    python -m trailforge.ingestion.pipeline --fixtures tests/fixtures
    python -m trailforge.ingestion.pipeline --fixtures tests/fixtures \
        --osm-file osm_tracks_v2_jittered.geojson
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geoalchemy2.shape import from_shape, to_shape
from shapely import STRtree
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from trailforge.db.models import (
    POI,
    CAPADPark,
    SegmentReconciliationLog,
    SourceCAPADPark,
    SourcePOI,
    SourceTrackAUState,
    SourceTrackNSW,
    SourceTrackOSM,
    SourceTrackVIC,
    SourceWaterway,
    Track,
    TrackSegment,
    TrackSegmentGrade,
)
from trailforge.db.session import get_engine
from trailforge.grading.baseline import SegmentGradeInput, compute_baseline_grade
from trailforge.ingestion.conflate import conflate_tracks, to_albers
from trailforge.ingestion.connectors.base import (
    AU_EXTENDED_STATE_CODES,
    AU_STATE_CODES,
    CorridorDataset,
    RawCapadFeature,
    RawPoiFeature,
    RawTrackFeature,
    RawWaterwayFeature,
    dedupe_poi_features,
)
from trailforge.ingestion.connectors.fixtures import load_corridor_fixtures
from trailforge.ingestion.crossings import (
    CapadSpatialIndex,
    WaterwaySpatialIndex,
)
from trailforge.ingestion.reconcile import ExistingSegmentSnapshot, reconcile_segments
from trailforge.ingestion.segmenter import (
    CanonicalTrackGroup,
    infer_state_from_lonlat,
    segment_conflated_ways,
)
from trailforge.ingestion.terrain import DemSampler, SegmentTerrainMetrics, drape_segment_dem

logger = logging.getLogger("trailforge.ingestion.pipeline")

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_POI_NEAREST_SEG_MAX_M = 250.0


@dataclass
class PipelineRunSummary:
    """Structured counters emitted at the end of a corridor ingestion run."""

    import_run_id: uuid.UUID
    corridor_name: str
    tier_a_osm_tracks: int
    tier_a_vic_tracks: int
    tier_a_nsw_tracks: int
    tier_a_waterways: int
    tier_a_pois: int
    tier_a_capad_parks: int
    conflated_ways: int
    deduplicated_ways: int
    dropped_non_vehicular: int
    tracks_upserted: int
    segments_active: int
    segments_exact_match: int
    segments_spatial_preserved: int
    segments_created: int
    segments_retired: int
    grades_upserted: int
    pois_upserted: int
    capad_parks_upserted: int
    tier_a_au_state_tracks: int = 0


def _as_linestring(geom: Any) -> LineString:
    if isinstance(geom, LineString):
        return geom
    if isinstance(geom, MultiLineString) and len(geom.geoms) > 0:
        return max(geom.geoms, key=lambda g: g.length)
    raise TypeError(f"Expected LineString/MultiLineString, got {type(geom).__name__}")


def _as_multilinestring(geom: Any) -> MultiLineString:
    if isinstance(geom, MultiLineString):
        return geom
    if isinstance(geom, LineString):
        return MultiLineString([geom])
    raise TypeError(f"Expected LineString/MultiLineString, got {type(geom).__name__}")


def _as_multipolygon(geom: Any) -> MultiPolygon:
    if isinstance(geom, MultiPolygon):
        return geom
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    raise TypeError(f"Expected Polygon/MultiPolygon, got {type(geom).__name__}")


def _slugify(value: str | None) -> str:
    if not value:
        return "unnamed"
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug or "unnamed"


_UPSERT_CHUNK_SIZE = 500


def _batch_pg_upsert(
    session: Session,
    model: type[Any],
    rows: list[dict[str, Any]],
    *,
    key_fn: Any,
    update_cols: tuple[str, ...],
    index_elements: list[Any] | None = None,
    constraint: str | None = None,
    extra_set: Any = None,
    chunk_size: int = _UPSERT_CHUNK_SIZE,
) -> int:
    """Execute deduplicated multi-row PostgreSQL ON CONFLICT DO UPDATE in chunks."""
    if not rows:
        return 0
    # Deduplicate across all rows preserving last-wins semantics (matches row-by-row loop)
    deduped = list({key_fn(r): r for r in rows}.values())
    for start in range(0, len(deduped), chunk_size):
        chunk = deduped[start : start + chunk_size]
        stmt = pg_insert(model).values(chunk)
        set_map: dict[str, Any] = {col: stmt.excluded[col] for col in update_cols}
        if extra_set is not None:
            set_map.update(extra_set(stmt))
        if constraint is not None:
            stmt = stmt.on_conflict_do_update(constraint=constraint, set_=set_map)
        else:
            stmt = stmt.on_conflict_do_update(index_elements=index_elements, set_=set_map)
        session.execute(stmt)
    return len(rows)


def _upsert_tier_a_osm(session: Session, features: list[RawTrackFeature]) -> int:
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for feat in features:
        way_id = feat.osm_way_id
        if way_id is None:
            digits = re.sub(r"[^0-9]", "", feat.source_id)
            if not digits:
                continue
            way_id = int(digits)
        line = _as_linestring(feat.to_shapely())
        rows.append(
            {
                "osm_way_id": way_id,
                "osm_version": feat.osm_version,
                "name": feat.name,
                "highway": feat.highway or "track",
                "surface": feat.surface_raw,
                "tracktype": feat.tracktype,
                "smoothness": feat.smoothness,
                "four_wd_only": feat.four_wd_only_raw,
                "access": feat.access_raw,
                "motor_vehicle": feat.motor_vehicle,
                "bridge": feat.bridge_raw,
                "ford": feat.ford_raw,
                "has_gate_barrier": feat.has_gate_barrier,
                "tags": feat.raw_props,
                "geom": from_shape(line, srid=4326),
                "source_snapshot_at": now,
                "ingested_at": now,
            }
        )
    return _batch_pg_upsert(
        session,
        SourceTrackOSM,
        rows,
        key_fn=lambda r: r["osm_way_id"],
        index_elements=[SourceTrackOSM.osm_way_id],
        update_cols=(
            "osm_version",
            "name",
            "highway",
            "surface",
            "tracktype",
            "smoothness",
            "four_wd_only",
            "access",
            "motor_vehicle",
            "bridge",
            "ford",
            "has_gate_barrier",
            "tags",
            "geom",
            "source_snapshot_at",
            "ingested_at",
        ),
    )


def _upsert_tier_a_vic(session: Session, features: list[RawTrackFeature]) -> int:
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for feat in features:
        mline = _as_multilinestring(feat.to_shapely())
        layer = (
            feat.source_layer
            if feat.source_layer in ("tr_road", "recweb_tracks")
            else "tr_road"
        )
        rows.append(
            {
                "source_layer": layer,
                "source_feature_id": feat.source_id,
                "name": feat.name,
                "trail_class": feat.trail_class,
                "class_code": feat.class_code,
                "surface": feat.surface_raw,
                "is_vehicular": feat.is_vehicular,
                "raw_props": feat.raw_props,
                "geom": from_shape(mline, srid=4326),
                "ingested_at": now,
            }
        )
    return _batch_pg_upsert(
        session,
        SourceTrackVIC,
        rows,
        key_fn=lambda r: (r["source_layer"], r["source_feature_id"]),
        constraint="uq_source_tracks_vic_feature",
        update_cols=(
            "name",
            "trail_class",
            "class_code",
            "surface",
            "is_vehicular",
            "raw_props",
            "geom",
            "ingested_at",
        ),
    )


def _upsert_tier_a_nsw(session: Session, features: list[RawTrackFeature]) -> int:
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for feat in features:
        mline = _as_multilinestring(feat.to_shapely())
        layer = (
            feat.source_layer
            if feat.source_layer in ("nsw_fire_trails_l9", "nsw_tracks_l5")
            else "nsw_fire_trails_l9"
        )
        rows.append(
            {
                "source_layer": layer,
                "source_feature_id": feat.source_id,
                "name": feat.name,
                "trail_class": feat.trail_class,
                "class_subtype": feat.class_subtype,
                "surface": feat.surface_raw,
                "operational_status": feat.operational_status,
                "is_vehicular": feat.is_vehicular,
                "raw_props": feat.raw_props,
                "geom": from_shape(mline, srid=4326),
                "ingested_at": now,
            }
        )
    return _batch_pg_upsert(
        session,
        SourceTrackNSW,
        rows,
        key_fn=lambda r: (r["source_layer"], r["source_feature_id"]),
        constraint="uq_source_tracks_nsw_feature",
        update_cols=(
            "name",
            "trail_class",
            "class_subtype",
            "surface",
            "operational_status",
            "is_vehicular",
            "raw_props",
            "geom",
            "ingested_at",
        ),
    )


def _upsert_tier_a_au_state(session: Session, features: list[RawTrackFeature]) -> int:
    """Persist ACT/TAS/QLD/WA/SA/NT source tracks into Tier A ``source_tracks_au_state``."""
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for feat in features:
        state_code = feat.source.upper()
        if state_code not in AU_EXTENDED_STATE_CODES:
            continue
        line = _as_linestring(feat.to_shapely())
        layer = (feat.source_layer or f"{feat.source}_tracks")[:64]
        feat_id = str(feat.source_id)[:128]
        rows.append(
            {
                "state_code": state_code,
                "source_layer": layer,
                "source_feature_id": feat_id,
                "name": feat.name,
                "trail_class": feat.trail_class,
                "surface_raw": feat.surface_raw,
                "access_raw": feat.access_raw,
                "properties_json": feat.raw_props,
                "geom": from_shape(line, srid=4326),
                "ingested_at": now,
            }
        )
    return _batch_pg_upsert(
        session,
        SourceTrackAUState,
        rows,
        key_fn=lambda r: (r["state_code"], r["source_layer"], r["source_feature_id"]),
        constraint="uq_source_tracks_au_state_feat",
        update_cols=(
            "name",
            "trail_class",
            "surface_raw",
            "access_raw",
            "properties_json",
            "geom",
            "ingested_at",
        ),
    )


def _upsert_tier_a_waterways(session: Session, features: list[RawWaterwayFeature]) -> int:
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for ww in features:
        geom = ww.to_shapely()
        rows.append(
            {
                "source_provider": ww.source_provider,
                "source_license": ww.source_license,
                "source_feature_id": ww.source_feature_id,
                "name": ww.name,
                "feature_type": ww.feature_type,
                "perenniality": ww.perenniality,
                "hierarchy": ww.hierarchy,
                "geom": from_shape(geom, srid=4326),
                "ingested_at": now,
            }
        )
    return _batch_pg_upsert(
        session,
        SourceWaterway,
        rows,
        key_fn=lambda r: (r["source_provider"], r["source_feature_id"]),
        constraint="uq_source_waterways_feat",
        update_cols=(
            "source_license",
            "name",
            "feature_type",
            "perenniality",
            "hierarchy",
            "geom",
            "ingested_at",
        ),
    )


def _upsert_tier_a_pois(session: Session, features: list[RawPoiFeature]) -> int:
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for poi in features:
        pt = poi.to_shapely()
        rows.append(
            {
                "source_provider": poi.source_provider,
                "source_license": poi.source_license,
                "source_feature_id": poi.source_feature_id,
                "name": poi.name,
                "poi_type": poi.poi_type,
                "state": poi.state,
                "elev_m": poi.elev_m,
                "raw_props": poi.raw_props,
                "geom": from_shape(pt, srid=4326),
                "ingested_at": now,
            }
        )
    return _batch_pg_upsert(
        session,
        SourcePOI,
        rows,
        key_fn=lambda r: (r["source_provider"], r["source_feature_id"]),
        constraint="uq_source_pois_feature",
        update_cols=(
            "source_license",
            "name",
            "poi_type",
            "state",
            "elev_m",
            "raw_props",
            "geom",
            "ingested_at",
        ),
    )


def _upsert_capad_parks(
    session: Session, features: list[RawCapadFeature]
) -> tuple[int, dict[str, int]]:
    """Upsert Tier A `source_capad_parks` and Tier B `capad_parks`, returning ext_id -> id."""
    now = datetime.now(UTC)
    id_map: dict[str, int] = {}
    for park in features:
        mpoly = _as_multipolygon(park.to_shapely())
        geom_wkb = from_shape(mpoly, srid=4326)
        area_ha = round(to_albers(mpoly).area / 10_000.0, 2)

        session.execute(
            pg_insert(SourceCAPADPark)
            .values(
                source_feature_id=park.source_feature_id,
                name=park.name,
                park_type=park.park_type,
                type_abbr=park.type_abbr,
                iucn=park.iucn,
                state=park.state,
                geom=geom_wkb,
                ingested_at=now,
            )
            .on_conflict_do_update(
                index_elements=[SourceCAPADPark.source_feature_id],
                set_={
                    "name": park.name,
                    "park_type": park.park_type,
                    "type_abbr": park.type_abbr,
                    "iucn": park.iucn,
                    "state": park.state,
                    "geom": geom_wkb,
                    "ingested_at": now,
                },
            )
        )

        park_id = session.execute(
            pg_insert(CAPADPark)
            .values(
                capad_external_id=park.source_feature_id,
                name=park.name,
                park_type=park.park_type,
                type_abbr=park.type_abbr,
                iucn=park.iucn,
                state=park.state,
                area_ha=area_ha,
                geom=geom_wkb,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[CAPADPark.capad_external_id],
                set_={
                    "name": park.name,
                    "park_type": park.park_type,
                    "type_abbr": park.type_abbr,
                    "iucn": park.iucn,
                    "state": park.state,
                    "area_ha": area_ha,
                    "geom": geom_wkb,
                    "updated_at": now,
                },
            )
            .returning(CAPADPark.id)
        ).scalar_one()
        id_map[park.source_feature_id] = int(park_id)

    return len(features), id_map


def _upsert_canonical_pois(
    session: Session,
    raw_pois: list[RawPoiFeature],
    capad_parks: list[RawCapadFeature],
    capad_id_map: dict[str, int],
    active_seg_geoms: list[tuple[str, LineString]],
    capad_index: CapadSpatialIndex | None = None,
) -> int:
    deduped = dedupe_poi_features(raw_pois)
    if not deduped:
        return 0

    seg_ids = [sid for sid, _ in active_seg_geoms]
    seg_lines = [geom for _, geom in active_seg_geoms]
    seg_tree = STRtree(seg_lines) if seg_lines else None
    park_index = capad_index if capad_index is not None else CapadSpatialIndex(capad_parks)
    now = datetime.now(UTC)

    count = 0
    for poi in deduped:
        pt_wgs84 = poi.to_shapely()
        if not isinstance(pt_wgs84, Point):
            continue
        lon = float(pt_wgs84.x)
        lat = float(pt_wgs84.y)

        tenure = park_index.find_tenure(pt_wgs84)
        park_id = capad_id_map.get(tenure["source_feature_id"]) if tenure else None
        state = poi.state or (tenure["state"] if tenure else infer_state_from_lonlat(lon, lat))

        nearest_seg_id: str | None = None
        if seg_tree is not None:
            pt_3577 = to_albers(pt_wgs84)
            best_dist = _POI_NEAREST_SEG_MAX_M
            for pos in seg_tree.query(pt_3577.buffer(_POI_NEAREST_SEG_MAX_M)):
                idx = int(pos)
                dist = pt_3577.distance(seg_lines[idx])
                if dist <= best_dist:
                    best_dist = dist
                    nearest_seg_id = seg_ids[idx]

        display_name = poi.name or f"{poi.poi_type.replace('_', ' ').title()}"
        poi_key = f"{poi.poi_type}:{_slugify(display_name)}:{round(lon * 1000)}:{round(lat * 1000)}"
        source_ref = {
            "source_provider": poi.source_provider,
            "source_feature_id": poi.source_feature_id,
            "source_license": poi.source_license,
        }

        session.execute(
            pg_insert(POI)
            .values(
                poi_key=poi_key,
                name=display_name,
                poi_type=poi.poi_type,
                state=state,
                elev_m=poi.elev_m,
                access_note=poi.access_note,
                nearest_segment_id=nearest_seg_id,
                capad_park_id=park_id,
                primary_source=poi.source_provider,
                source_license=poi.source_license,
                source_refs=[source_ref],
                geom=from_shape(pt_wgs84, srid=4326),
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[POI.poi_key],
                set_={
                    "name": display_name,
                    "poi_type": poi.poi_type,
                    "state": state,
                    "elev_m": poi.elev_m,
                    "access_note": poi.access_note,
                    "nearest_segment_id": nearest_seg_id,
                    "capad_park_id": park_id,
                    "primary_source": poi.source_provider,
                    "source_license": poi.source_license,
                    "source_refs": [source_ref],
                    "geom": from_shape(pt_wgs84, srid=4326),
                    "updated_at": now,
                },
            )
        )
        count += 1
    return count


def ingest_corridor_dataset(
    session: Session,
    dataset: CorridorDataset,
    *,
    import_run_id: uuid.UUID | None = None,
    commit: bool = True,
) -> PipelineRunSummary:
    """Execute full idempotent Slice 02 ingestion into PostGIS (Tier A -> Tier B -> Tier C)."""
    run_id = import_run_id or uuid.uuid4()
    now = datetime.now(UTC)

    # -------------------------------------------------------------------------
    # 1. Tier A — Isolated Upstream Staging Tables (ODbL & CC BY separated)
    # -------------------------------------------------------------------------
    n_osm = _upsert_tier_a_osm(session, dataset.osm_tracks)
    n_vic = _upsert_tier_a_vic(session, dataset.vic_tracks)
    n_nsw = _upsert_tier_a_nsw(session, dataset.nsw_tracks)
    n_au_state = _upsert_tier_a_au_state(session, dataset.au_state_tracks)
    n_ww = _upsert_tier_a_waterways(session, dataset.waterways)
    n_pois_raw = _upsert_tier_a_pois(session, dataset.pois)
    n_capad, capad_id_map = _upsert_capad_parks(session, dataset.capad_parks)

    # -------------------------------------------------------------------------
    # 2. Multi-Source Conflation & Canonical 50-100 m Segmentation
    # -------------------------------------------------------------------------
    conflation = conflate_tracks(dataset.all_tracks)
    capad_index = CapadSpatialIndex(dataset.capad_parks)
    waterway_index = WaterwaySpatialIndex(dataset.waterways)

    def _resolve_state(lon: float, lat: float) -> str:
        tenure = capad_index.find_tenure(Point(lon, lat))
        if tenure and tenure.get("state") in AU_STATE_CODES:
            return str(tenure["state"])
        return infer_state_from_lonlat(lon, lat)

    track_groups, segment_candidates = segment_conflated_ways(
        conflation.ways, state_resolver=_resolve_state
    )

    # -------------------------------------------------------------------------
    # 3. Load Active Corridor Segments & Execute 4-Pass ID Reconciliation
    # -------------------------------------------------------------------------
    bbox = dataset.bbox
    env = func.ST_MakeEnvelope(bbox.min_x, bbox.min_y, bbox.max_x, bbox.max_y, 4326)
    existing_rows = session.execute(
        select(TrackSegment).where(
            TrackSegment.is_active.is_(True),
            func.ST_Intersects(TrackSegment.geom, env),
        )
    ).scalars().all()

    all_known_ids = set(session.execute(select(TrackSegment.segment_id)).scalars().all())
    existing_snapshots: list[ExistingSegmentSnapshot] = []
    for row in existing_rows:
        shp_wgs84 = to_shape(row.geom)
        shp_3577 = to_albers(shp_wgs84)
        assert isinstance(shp_3577, LineString)
        existing_snapshots.append(
            ExistingSegmentSnapshot(
                segment_id=row.segment_id,
                geom_fingerprint=row.geom_fingerprint,
                geom_3577=shp_3577,
                length_m=float(row.length_m),
                bearing_deg=float(row.bearing_deg),
            )
        )

    plan = reconcile_segments(
        existing_snapshots,
        segment_candidates,
        import_run_id=run_id,
        all_known_segment_ids=all_known_ids,
    )

    # -------------------------------------------------------------------------
    # 4. DEM Draping, Waterway Crossings, CAPAD Tenure & Explainable Baseline Grading
    # -------------------------------------------------------------------------
    seg_metrics: dict[str, tuple[SegmentTerrainMetrics, int, bool, int | None, Any]] = {}
    track_by_id: dict[str, CanonicalTrackGroup] = {t.track_id: t for t in track_groups}

    def _process_segments(sampler: DemSampler | None) -> None:
        for rec in plan.reconciled_segments:
            cand = rec.candidate
            if sampler is not None:
                terrain = drape_segment_dem(cand.geom_wgs84, sampler)
            else:
                terrain = SegmentTerrainMetrics(length_m=cand.length_m)

            crossings = waterway_index.count_crossings(
                cand.geom_wgs84,
                geom_3577=cand.geom_3577,
                is_bridged=cand.is_bridged,
            )
            tenure = capad_index.find_tenure(
                cand.geom_wgs84,
                geom_3577=cand.geom_3577,
            )
            park_id = capad_id_map.get(tenure["source_feature_id"]) if tenure else None

            grade_input = SegmentGradeInput(
                surface_class=cand.surface_class,  # type: ignore[arg-type]
                tracktype=cand.tracktype,  # type: ignore[arg-type]
                smoothness=cand.smoothness,
                trail_class=cand.trail_class,  # type: ignore[arg-type]
                max_slope_deg=terrain.max_slope_deg,
                sustained_slope_deg=terrain.sustained_slope_deg,
                elev_gain_m=terrain.elev_gain_m,
                elev_loss_m=terrain.elev_loss_m,
                unbridged_crossing_count=crossings.unbridged_count,
                has_perennial_crossing=crossings.has_perennial_crossing,
                has_major_perennial_crossing=crossings.has_major_perennial_crossing,
                waterway_names=crossings.waterway_names,
                is_bridged=cand.is_bridged,
                is_ford=cand.is_ford,
                is_4wd_only=cand.is_4wd_only,
                is_4wd_recommended=cand.is_4wd_recommended,
                access_status=cand.access_status,  # type: ignore[arg-type]
                has_gate_barrier=cand.has_gate_barrier,
                dem_tier=dataset.dem_tier if sampler is not None else None,
            )
            grade_res = compute_baseline_grade(grade_input)
            seg_metrics[rec.final_segment_id] = (
                terrain,
                crossings.unbridged_count,
                crossings.has_perennial_crossing,
                park_id,
                grade_res,
            )

    if dataset.dem_path is not None and Path(dataset.dem_path).is_file():
        with DemSampler(dataset.dem_path) as sampler:
            _process_segments(sampler)
    else:
        _process_segments(None)

    # -------------------------------------------------------------------------
    # 5. Upsert Tier B `tracks` with Rolled-Up Elevation, Slope, Crossings & Tenure
    # -------------------------------------------------------------------------
    segs_by_track: dict[str, list[Any]] = {}
    for rec in plan.reconciled_segments:
        if rec.candidate.track_id:
            segs_by_track.setdefault(rec.candidate.track_id, []).append(rec)

    track_rows: list[dict[str, Any]] = []
    for tid, trk in track_by_id.items():
        t_recs = segs_by_track.get(tid, [])
        elev_mins = [
            seg_metrics[r.final_segment_id][0].min_elev_m
            for r in t_recs
            if seg_metrics[r.final_segment_id][0].min_elev_m is not None
        ]
        elev_maxs = [
            seg_metrics[r.final_segment_id][0].max_elev_m
            for r in t_recs
            if seg_metrics[r.final_segment_id][0].max_elev_m is not None
        ]
        slopes = [
            seg_metrics[r.final_segment_id][0].max_slope_deg
            for r in t_recs
            if seg_metrics[r.final_segment_id][0].max_slope_deg is not None
        ]
        total_gain = round(
            sum(seg_metrics[r.final_segment_id][0].elev_gain_m for r in t_recs), 2
        )
        total_crossings = sum(seg_metrics[r.final_segment_id][1] for r in t_recs)
        park_votes = Counter(
            seg_metrics[r.final_segment_id][3]
            for r in t_recs
            if seg_metrics[r.final_segment_id][3] is not None
        )
        primary_park_id = park_votes.most_common(1)[0][0] if park_votes else None

        bbox_wkb = from_shape(trk.bbox_geom, srid=4326) if trk.bbox_geom is not None else None
        track_rows.append(
            {
                "track_id": trk.track_id,
                "name": trk.name,
                "alt_names": trk.alt_names,
                "state": trk.state,
                "primary_source": trk.primary_source,
                "has_osm_source": trk.has_osm_source,
                "trail_class": trk.trail_class,
                "is_vehicular": trk.is_vehicular,
                "total_length_m": trk.total_length_m,
                "elev_min_m": min(elev_mins) if elev_mins else None,
                "elev_max_m": max(elev_maxs) if elev_maxs else None,
                "elev_gain_m": total_gain,
                "max_slope_deg": max(slopes) if slopes else None,
                "waterway_crossing_count": total_crossings,
                "primary_park_id": primary_park_id,
                "bbox_geom": bbox_wkb,
                "is_active": True,
                "updated_at": now,
            }
        )
    _batch_pg_upsert(
        session,
        Track,
        track_rows,
        key_fn=lambda r: r["track_id"],
        index_elements=[Track.track_id],
        update_cols=(
            "name",
            "alt_names",
            "state",
            "primary_source",
            "has_osm_source",
            "trail_class",
            "is_vehicular",
            "total_length_m",
            "elev_min_m",
            "elev_max_m",
            "elev_gain_m",
            "max_slope_deg",
            "waterway_crossing_count",
            "primary_park_id",
            "bbox_geom",
            "is_active",
            "updated_at",
        ),
    )

    # -------------------------------------------------------------------------
    # 6. Upsert Tier B `track_segments` & Soft-Retire Superseded Segments
    # -------------------------------------------------------------------------
    active_seg_geoms: list[tuple[str, LineString]] = []
    has_dem = dataset.dem_path is not None and Path(dataset.dem_path).is_file()
    dem_tier_val = dataset.dem_tier if has_dem else None

    seg_rows: list[dict[str, Any]] = []
    for rec in plan.reconciled_segments:
        cand = rec.candidate
        terrain, unbridged_cnt, has_perennial, park_id, _grade_res = seg_metrics[
            rec.final_segment_id
        ]
        geom_wkb = from_shape(cand.geom_wgs84, srid=4326)
        active_seg_geoms.append((rec.final_segment_id, cand.geom_3577))

        seg_rows.append(
            {
                "segment_id": rec.final_segment_id,
                "track_id": cand.track_id,
                "segment_seq": cand.segment_seq,
                "geom_fingerprint": cand.geom_fingerprint,
                "geom": geom_wkb,
                "start_node_key": cand.start_node_key,
                "end_node_key": cand.end_node_key,
                "length_m": cand.length_m,
                "bearing_deg": cand.bearing_deg,
                "state": cand.state,
                "name": cand.name,
                "trail_class": cand.trail_class,
                "is_vehicular": cand.is_vehicular,
                "surface_class": cand.surface_class,
                "tracktype": cand.tracktype,
                "is_4wd_only": cand.is_4wd_only,
                "is_4wd_recommended": cand.is_4wd_recommended,
                "access_status": cand.access_status,
                "has_gate_barrier": cand.has_gate_barrier,
                "is_bridged": cand.is_bridged,
                "elev_min_m": terrain.min_elev_m,
                "elev_max_m": terrain.max_elev_m,
                "elev_start_m": terrain.elev_start_m,
                "elev_end_m": terrain.elev_end_m,
                "elev_gain_m": terrain.elev_gain_m,
                "elev_loss_m": terrain.elev_loss_m,
                "max_slope_deg": terrain.max_slope_deg,
                "sustained_slope_deg": terrain.sustained_slope_deg,
                "is_steep_highlight": terrain.is_steep_highlight,
                "is_above_snowline": terrain.is_above_snowline,
                "elevation_profile_m": terrain.elevation_profile,
                "dem_tier": dem_tier_val,
                "waterway_crossing_count": unbridged_cnt,
                "has_perennial_crossing": has_perennial,
                "capad_park_id": park_id,
                "primary_source": cand.primary_source,
                "has_osm_source": cand.has_osm_source,
                "source_refs": cand.source_refs,
                "is_active": True,
                "retired_at": None,
                "superseded_by_segment_id": None,
                "updated_at": now,
            }
        )
    if seg_rows:
        seg_update_cols = tuple(k for k in seg_rows[0] if k != "segment_id")
        _batch_pg_upsert(
            session,
            TrackSegment,
            seg_rows,
            key_fn=lambda r: r["segment_id"],
            index_elements=[TrackSegment.segment_id],
            update_cols=seg_update_cols,
        )

    # Flush new segments before pointing superseded_by_segment_id FKs to them
    session.flush()

    for retired in plan.retired_segments:
        row = session.get(TrackSegment, retired.segment_id)
        if row is not None:
            row.is_active = False
            row.retired_at = now
            row.superseded_by_segment_id = retired.superseded_by_segment_id
            row.updated_at = now

    if plan.log_entries:
        session.add_all(
            [
                SegmentReconciliationLog(
                    import_run_id=entry.import_run_id,
                    reconcile_action=entry.reconcile_action,
                    old_segment_id=entry.old_segment_id,
                    new_segment_id=entry.new_segment_id,
                    overlap_ratio=entry.overlap_ratio,
                    hausdorff_m=entry.hausdorff_m,
                    heading_diff_deg=entry.heading_diff_deg,
                    created_at=now,
                )
                for entry in plan.log_entries
            ]
        )

    # -------------------------------------------------------------------------
    # 7. Upsert Tier C `track_segment_grades` (zero OSM geom/tags, preserves live fields)
    # -------------------------------------------------------------------------
    grade_rows: list[dict[str, Any]] = []
    for rec in plan.reconciled_segments:
        _, _, _, _, grade_res = seg_metrics[rec.final_segment_id]
        grade_rows.append(
            {
                "segment_id": rec.final_segment_id,
                "baseline_grade": grade_res.grade,
                "baseline_score": grade_res.score,
                "baseline_breakdown": grade_res.baseline_breakdown,
                "effective_grade": grade_res.grade,
                "baseline_computed_at": now,
            }
        )
    _batch_pg_upsert(
        session,
        TrackSegmentGrade,
        grade_rows,
        key_fn=lambda r: r["segment_id"],
        index_elements=[TrackSegmentGrade.segment_id],
        update_cols=(
            "baseline_grade",
            "baseline_score",
            "baseline_breakdown",
            "baseline_computed_at",
        ),
        extra_set=lambda stmt: {
            "effective_grade": func.coalesce(
                TrackSegmentGrade.live_grade,
                stmt.excluded.baseline_grade,
            )
        },
    )

    # -------------------------------------------------------------------------
    # 8. Upsert Deduplicated Canonical `pois`
    # -------------------------------------------------------------------------
    n_pois_canonical = _upsert_canonical_pois(
        session,
        dataset.pois,
        dataset.capad_parks,
        capad_id_map,
        active_seg_geoms,
        capad_index=capad_index,
    )

    if commit:
        session.commit()
    else:
        session.flush()

    return PipelineRunSummary(
        import_run_id=run_id,
        corridor_name=dataset.corridor_name,
        tier_a_osm_tracks=n_osm,
        tier_a_vic_tracks=n_vic,
        tier_a_nsw_tracks=n_nsw,
        tier_a_waterways=n_ww,
        tier_a_pois=n_pois_raw,
        tier_a_capad_parks=n_capad,
        conflated_ways=len(conflation.ways),
        deduplicated_ways=conflation.deduplicated_count,
        dropped_non_vehicular=conflation.dropped_non_vehicular_count,
        tracks_upserted=len(track_groups),
        segments_active=len(plan.reconciled_segments),
        segments_exact_match=plan.exact_match_count,
        segments_spatial_preserved=plan.spatial_preserved_count,
        segments_created=plan.created_count,
        segments_retired=len(plan.retired_segments),
        grades_upserted=len(plan.reconciled_segments),
        pois_upserted=n_pois_canonical,
        capad_parks_upserted=n_capad,
        tier_a_au_state_tracks=n_au_state,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: `python -m trailforge.ingestion.pipeline --fixtures tests/fixtures`."""
    parser = argparse.ArgumentParser(
        description="Run TrailForge Slice 02 Canonical Track Network & Baseline Grading Pipeline"
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        required=True,
        help="Path to offline synthetic fixture directory (e.g. tests/fixtures)",
    )
    parser.add_argument(
        "--osm-file",
        default="osm_tracks.geojson",
        help="OSM fixture GeoJSON filename inside --fixtures (default: osm_tracks.geojson)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Optional SQLAlchemy database URL override",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    dataset = load_corridor_fixtures(args.fixtures, osm_filename=args.osm_file)
    engine = get_engine(args.database_url)
    try:
        with Session(engine) as session:
            summary = ingest_corridor_dataset(session, dataset, commit=True)
    finally:
        if args.database_url is not None:
            engine.dispose()

    print(
        json.dumps(
            {
                "import_run_id": str(summary.import_run_id),
                "corridor_name": summary.corridor_name,
                "tracks_upserted": summary.tracks_upserted,
                "segments_active": summary.segments_active,
                "segments_exact_match": summary.segments_exact_match,
                "segments_spatial_preserved": summary.segments_spatial_preserved,
                "segments_created": summary.segments_created,
                "segments_retired": summary.segments_retired,
                "grades_upserted": summary.grades_upserted,
                "pois_upserted": summary.pois_upserted,
                "capad_parks_upserted": summary.capad_parks_upserted,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
