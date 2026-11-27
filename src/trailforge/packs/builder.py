"""Streaming <300 MB RAM offline regional pack builder for TrailForge (Slice 09).

Produces an atomic, self-contained regional bundle under ``<packs_dir>/<region_id>/``:
  - ``snapshot.json``   — streamed offline Track Inspector, POI, CAPAD, closure, report &
                          obstacle hotspot dataset
  - ``vector.pmtiles``  — region-clipped multi-layer vector PMTiles (when layers & tippecanoe exist)
  - ``terrain.pmtiles`` — region-clipped Terrain-RGB PMTiles (when intersecting DEM & tools exist)
  - ``manifest.json``   — exact file sizes, SHA-256 checksums, bounds, counts, and attributions
  - ``bundle.tar.gz``   — chunk-streamed downloadable archive of the full pack

Zero PyQGIS dependency; enforces streaming I/O and atomic staging promotion.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tarfile
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from geoalchemy2.shape import from_shape
from pydantic import BaseModel, Field
from shapely.geometry import box
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from trailforge.config import get_settings
from trailforge.db.models import (
    POI,
    CAPADPark,
    ClosureAlert,
    ConditionReport,
    OfflinePack,
    SegmentClosureLink,
    Track,
    TrackSegment,
    TrackSegmentGrade,
)
from trailforge.feeds.queries import list_closures
from trailforge.ingestion.terrain import read_raster_bounds
from trailforge.packs.catalog import PACK_ATTRIBUTIONS, PackRegionSpec, get_pack_region
from trailforge.reports.queries import list_reports
from trailforge.tiles.builder import (
    DEFAULT_SNOWLINE_BANDS_M,
    TERRAIN_PMTILES_FILENAME,
    VECTOR_PMTILES_FILENAME,
    build_tiles,
)

logger = logging.getLogger(__name__)

MANIFEST_FILENAME: Final[str] = "manifest.json"
SNAPSHOT_FILENAME: Final[str] = "snapshot.json"
VECTOR_TILE_FILENAME: Final[str] = "vector.pmtiles"
TERRAIN_TILE_FILENAME: Final[str] = "terrain.pmtiles"
BUNDLE_FILENAME: Final[str] = "bundle.tar.gz"

_HASH_CHUNK_BYTES: Final[int] = 65_536
_DEFAULT_DEMO_DEM: Final[Path] = Path("tests/fixtures/dem_alps_corridor.tif")


class PackFileEntry(BaseModel):
    """Per-file integrity entry stored inside ``manifest.json``."""

    path: str
    size_bytes: int
    sha256: str


class PackManifest(BaseModel):
    """Manifest served at ``GET /api/packs/{region_id}/manifest.json``."""

    schema_version: str = "trailforge_pack_manifest_v1"
    region_id: str
    pack_id: str
    name: str
    description: str
    region_type: str
    version: str
    generated_at: str
    bbox: list[float] = Field(..., min_length=4, max_length=4)
    bounds: list[float] = Field(..., min_length=4, max_length=4)
    utm_zones: list[str] = Field(default_factory=list)
    minzoom: int = 8
    maxzoom: int = 15
    snowline_bands_m: list[int] = Field(default_factory=lambda: list(DEFAULT_SNOWLINE_BANDS_M))
    vector_available: bool = False
    terrain_available: bool = False
    manifest_url: str
    snapshot_url: str
    bundle_url: str
    vector_pmtiles_url: str
    terrain_pmtiles_url: str
    counts: dict[str, int] = Field(default_factory=dict)
    track_count: int = 0
    segment_count: int = 0
    poi_count: int = 0
    total_size_bytes: int = 0
    bundle_size_bytes: int = 0
    files: dict[str, PackFileEntry] = Field(default_factory=dict)
    attributions: list[str] = Field(default_factory=lambda: list(PACK_ATTRIBUTIONS))


class PackBuildResult(BaseModel):
    """Summary returned by ``build_regional_pack``."""

    region_id: str
    pack_dir: Path
    manifest_path: Path
    snapshot_path: Path
    bundle_path: Path
    vector_pmtiles_path: Path | None = None
    terrain_pmtiles_path: Path | None = None
    manifest_sha256: str
    manifest: PackManifest


def get_packs_dir(override: Path | str | None = None) -> Path:
    """Resolve the offline packs directory across override, env, and Settings."""
    if override is not None:
        return Path(override).expanduser()

    env_dir = os.environ.get("TRAILFORGE_PACKS_DIR") or os.environ.get("PACKS_DIR")
    if env_dir and env_dir.strip():
        return Path(env_dir.strip()).expanduser()

    settings = get_settings()
    configured = getattr(settings, "packs_dir", None) or getattr(
        settings, "trailforge_packs_dir", "/data/packs"
    )
    return Path(str(configured)).expanduser()


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 digest streaming in 64 KiB chunks (O(1) RAM)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _bboxes_intersect(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def _resolve_intersecting_dem(
    region: PackRegionSpec,
    dem_path: Path | str | None,
) -> Path | None:
    """Return a resolved DEM path only if it exists AND spatially intersects the region bbox."""
    candidate: Path | None = None
    if dem_path is not None:
        candidate = Path(dem_path).expanduser().resolve()
        if not candidate.is_file():
            raise FileNotFoundError(f"DEM file not found: {candidate}")
    else:
        env_dem = os.environ.get("TRAILFORGE_PACK_DEM_PATH")
        if env_dem and env_dem.strip():
            p = Path(env_dem.strip()).expanduser().resolve()
            if p.is_file():
                candidate = p
        elif _DEFAULT_DEMO_DEM.is_file():
            candidate = _DEFAULT_DEMO_DEM.resolve()

    if candidate is None:
        return None

    try:
        dem_bounds = read_raster_bounds(candidate)
    except Exception as exc:
        logger.warning("Unable to inspect DEM bounds at %s (%s); skipping DEM", candidate, exc)
        return None

    if not _bboxes_intersect(region.bbox, dem_bounds):
        return None
    return candidate


def _enrich_track_with_live_grading(session: Session, track_payload: dict[str, Any]) -> None:
    """Ensure Slice 07 live grading & rainfall keys are populated on offline track snapshots."""
    if "live_breakdown" in track_payload and "rainfall_context" in track_payload:
        return

    track_id = track_payload.get("track_id")
    if not track_id:
        return

    rows = session.execute(
        select(
            TrackSegment.segment_id,
            TrackSegment.length_m,
            func.ST_X(func.ST_Centroid(TrackSegment.geom)).label("lon"),
            func.ST_Y(func.ST_Centroid(TrackSegment.geom)).label("lat"),
            TrackSegmentGrade,
        )
        .join(TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id)
        .where(TrackSegment.track_id == track_id, TrackSegment.is_active.is_(True))
        .order_by(TrackSegment.segment_seq.asc().nullslast(), TrackSegment.segment_id.asc())
    ).all()

    grade_rank = {"Easy": 1, "Medium": 2, "Hard": 3, "Extreme": 4}
    rank_grade = {v: k for k, v in grade_rank.items()}
    conf_rank = {"baseline_only": 0, "none": 0, "low": 1, "medium": 2, "high": 3}

    worst_live_rank = 0
    best_live_score: float | None = None
    best_conf = "none"
    max_samples = 0
    latest_driven: datetime | None = None
    speed_vals: list[float] = []
    duration_total_s = 0.0
    veh_counts: dict[str, int] = {}
    rainfall_ctx: dict[str, Any] | None = None
    rep_live_breakdown: dict[str, Any] | None = None
    hotspots: list[dict[str, Any]] = []
    cum_dist = 0.0

    for seg_id, seg_len, lon, lat, grd in rows:
        seg_mid = round(cum_dist + float(seg_len or 0.0) * 0.5, 1)
        cum_dist += float(seg_len or 0.0)

        if grd.live_grade and grade_rank.get(grd.live_grade, 0) > worst_live_rank:
            worst_live_rank = grade_rank[grd.live_grade]
            rep_live_breakdown = grd.live_breakdown
        if grd.live_score is not None and (
            best_live_score is None or float(grd.live_score) > best_live_score
        ):
            best_live_score = round(float(grd.live_score), 1)
            if rep_live_breakdown is None and grd.live_breakdown:
                rep_live_breakdown = grd.live_breakdown
        if conf_rank.get(grd.confidence_level, 0) > conf_rank.get(best_conf, 0):
            best_conf = "none" if grd.confidence_level == "baseline_only" else grd.confidence_level
        max_samples = max(max_samples, int(grd.sample_count or 0))
        if grd.last_driven_at is not None and (
            latest_driven is None or grd.last_driven_at > latest_driven
        ):
            latest_driven = grd.last_driven_at
        if grd.avg_speed_kmh is not None:
            speed_vals.append(float(grd.avg_speed_kmh))
        if grd.avg_duration_s is not None:
            duration_total_s += float(grd.avg_duration_s)
        for cls_name, cnt in (grd.vehicle_class_split or {}).items():
            if isinstance(cnt, dict):
                veh_counts[cls_name] = veh_counts.get(cls_name, 0) + int(cnt.get("count", 0))
            elif isinstance(cnt, (int, float)):
                veh_counts[cls_name] = veh_counts.get(cls_name, 0) + int(cnt)
        if grd.rainfall_context and rainfall_ctx is None:
            rainfall_ctx = grd.rainfall_context

        lb = grd.live_breakdown or {}
        if isinstance(lb, dict) and lb.get("is_obstacle_hotspot"):
            hotspots.append(
                {
                    "segment_id": seg_id,
                    "dist_m": seg_mid,
                    "lon": round(float(lon), 6),
                    "lat": round(float(lat), 6),
                    "avg_speed_kmh": lb.get("avg_speed_kmh", grd.avg_speed_kmh),
                    "roughness_rms": lb.get("median_roughness_rms"),
                    "reasons": list(lb.get("obstacle_reasons") or []),
                }
            )

    total_veh = sum(veh_counts.values())
    split_payload = {
        cls_name: {
            "count": cnt,
            "pct": round((cnt / total_veh) * 100.0, 1) if total_veh > 0 else 0.0,
        }
        for cls_name, cnt in veh_counts.items()
    }

    track_payload.setdefault(
        "live_grade",
        rank_grade.get(worst_live_rank) if worst_live_rank else None,
    )
    track_payload.setdefault("live_score", best_live_score)
    track_payload.setdefault("confidence_level", best_conf)
    track_payload.setdefault("sample_count", max_samples)
    track_payload.setdefault(
        "last_driven_at",
        latest_driven.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if latest_driven is not None
        else None,
    )
    track_payload.setdefault(
        "avg_speed_kmh",
        round(sum(speed_vals) / len(speed_vals), 1) if speed_vals else None,
    )
    track_payload.setdefault(
        "est_duration_min",
        round(duration_total_s / 60.0, 1) if duration_total_s > 0 else None,
    )
    track_payload.setdefault("vehicle_class_split", split_payload)
    track_payload.setdefault("live_breakdown", rep_live_breakdown)
    track_payload.setdefault("rainfall_context", rainfall_ctx)
    track_payload.setdefault("obstacle_hotspots", hotspots)


def _write_region_snapshot_streaming(
    session: Session,
    region: PackRegionSpec,
    snapshot_path: Path,
    *,
    version: str,
    generated_at: str,
) -> dict[str, int]:
    """Stream a complete offline region snapshot JSON directly to disk (O(1) item memory)."""
    # Local import avoids circular imports with api/tracks
    from trailforge.api.tracks import get_track_details_batch

    env_geom = func.ST_MakeEnvelope(*region.bbox, 4326)

    # 1. Identify intersecting active track IDs
    track_ids_from_segs = (
        select(TrackSegment.track_id)
        .where(
            TrackSegment.is_active.is_(True),
            TrackSegment.track_id.is_not(None),
            func.ST_Intersects(TrackSegment.geom, env_geom),
        )
        .distinct()
    )
    track_ids: list[str] = list(
        session.execute(
            select(Track.track_id)
            .where(
                Track.is_active.is_(True),
                or_(
                    func.ST_Intersects(Track.bbox_geom, env_geom),
                    Track.track_id.in_(track_ids_from_segs),
                ),
            )
            .order_by(Track.name.asc(), Track.track_id.asc())
        )
        .scalars()
        .all()
    )
    track_id_set = set(track_ids)

    counts: dict[str, int] = {
        "tracks": 0,
        "segments": 0,
        "pois": 0,
        "capad_parks": 0,
        "closures": 0,
        "reports": 0,
        "hotspots": 0,
    }

    with snapshot_path.open("w", encoding="utf-8") as fh:
        # Header fields
        fh.write("{")
        header_pairs = [
            ("schema_version", "trailforge_pack_snapshot_v1"),
            ("region_id", region.region_id),
            ("name", region.name),
            ("description", region.description),
            ("version", version),
            ("generated_at", generated_at),
            ("bbox", list(region.bbox)),
            ("utm_zones", list(region.utm_zones)),
            ("attributions", list(region.attributions)),
        ]
        for idx, (key, val) in enumerate(header_pairs):
            if idx > 0:
                fh.write(",")
            fh.write(f"{json.dumps(key)}:{json.dumps(val, separators=(',', ':'))}")

        # 2. Stream full Track Inspector payloads in 50-track chunks (~50x fewer SQL queries)
        fh.write(',"tracks":[')
        batch_size = 50
        written_tracks = 0
        for start in range(0, len(track_ids), batch_size):
            chunk_ids = track_ids[start : start + batch_size]
            details = get_track_details_batch(session, chunk_ids)
            for detail in details:
                track_dict = detail.model_dump(mode="json")
                if written_tracks > 0:
                    fh.write(",")
                fh.write(json.dumps(track_dict, separators=(",", ":")))
                written_tracks += 1
                counts["tracks"] += 1
                counts["segments"] += len(track_dict.get("segments") or [])
        fh.write("]")

        # 3. Stream region POIs
        fh.write(',"pois":[')
        poi_rows = session.execute(
            select(
                POI,
                func.ST_X(POI.geom).label("lon"),
                func.ST_Y(POI.geom).label("lat"),
                CAPADPark.name.label("park_name"),
            )
            .outerjoin(CAPADPark, POI.capad_park_id == CAPADPark.id)
            .where(func.ST_Intersects(POI.geom, env_geom))
            .order_by(POI.name.asc(), POI.id.asc())
        )
        for poi, lon, lat, park_name in poi_rows.yield_per(200):
            if counts["pois"] > 0:
                fh.write(",")
            item = {
                "id": poi.id,
                "poi_key": poi.poi_key,
                "name": poi.name,
                "poi_type": poi.poi_type,
                "state": poi.state,
                "elev_m": poi.elev_m,
                "access_note": poi.access_note,
                "nearest_segment_id": poi.nearest_segment_id,
                "capad_park_name": park_name,
                "primary_source": poi.primary_source,
                "source_license": poi.source_license,
                "lon": round(float(lon), 6),
                "lat": round(float(lat), 6),
            }
            fh.write(json.dumps(item, separators=(",", ":")))
            counts["pois"] += 1
        fh.write("]")

        # 4. Stream CAPAD parks
        fh.write(',"capad_parks":[')
        park_rows = session.execute(
            select(CAPADPark)
            .where(func.ST_Intersects(CAPADPark.geom, env_geom))
            .order_by(CAPADPark.name.asc(), CAPADPark.id.asc())
        )
        for (park,) in park_rows.yield_per(100):
            if counts["capad_parks"] > 0:
                fh.write(",")
            item = {
                "id": park.id,
                "capad_external_id": park.capad_external_id,
                "name": park.name,
                "park_type": park.park_type,
                "type_abbr": park.type_abbr,
                "iucn": park.iucn,
                "state": park.state,
                "area_ha": park.area_ha,
            }
            fh.write(json.dumps(item, separators=(",", ":")))
            counts["capad_parks"] += 1
        fh.write("]")

        # 5. Stream intersecting active closures
        fh.write(',"closures":[')
        linked_closure_ids = set(
            session.execute(
                select(SegmentClosureLink.closure_id)
                .join(TrackSegment, SegmentClosureLink.segment_id == TrackSegment.segment_id)
                .where(
                    TrackSegment.is_active.is_(True),
                    func.ST_Intersects(TrackSegment.geom, env_geom),
                )
                .distinct()
            )
            .scalars()
            .all()
        )
        geom_closure_ids = set(
            session.execute(
                select(ClosureAlert.id)
                .where(
                    ClosureAlert.status != "cleared",
                    ClosureAlert.geom.is_not(None),
                    func.ST_Intersects(ClosureAlert.geom, env_geom),
                )
            )
            .scalars()
            .all()
        )
        region_closure_ids = linked_closure_ids | geom_closure_ids
        if region_closure_ids:
            all_closures = list_closures(session, active_only=True).get("items") or []
            for closure in all_closures:
                if closure.get("closure_id") not in region_closure_ids:
                    continue
                if counts["closures"] > 0:
                    fh.write(",")
                fh.write(json.dumps(closure, separators=(",", ":")))
                counts["closures"] += 1
        fh.write("]")

        # 6. Stream active condition reports inside region (zero author email)
        fh.write(',"reports":[')
        report_spatial_filter: Any = func.ST_Intersects(ConditionReport.geom, env_geom)
        if track_id_set:
            report_spatial_filter = or_(
                report_spatial_filter,
                ConditionReport.track_id.in_(track_ids_from_segs),
            )
        region_report_ids = set(
            session.execute(
                select(ConditionReport.id).where(
                    ConditionReport.status != "hidden",
                    report_spatial_filter,
                )
            )
            .scalars()
            .all()
        )
        if region_report_ids:
            region_report_id_strs = {str(rid) for rid in region_report_ids}
            for rep in list_reports(session, include_resolved=False, limit=500):
                if rep.get("id") not in region_report_id_strs:
                    continue
                if counts["reports"] > 0:
                    fh.write(",")
                fh.write(json.dumps(rep, separators=(",", ":")))
                counts["reports"] += 1
        fh.write("]")

        # 7. Stream rough/slow obstacle hotspots inside region as GeoJSON FeatureCollection
        fh.write(',"hotspots":{"type":"FeatureCollection","features":[')
        hotspot_rows = session.execute(
            select(
                TrackSegment.segment_id,
                TrackSegment.track_id,
                Track.name.label("track_name"),
                func.ST_AsGeoJSON(TrackSegment.geom).label("geom_json"),
                func.ST_X(func.ST_Centroid(TrackSegment.geom)).label("lon"),
                func.ST_Y(func.ST_Centroid(TrackSegment.geom)).label("lat"),
                TrackSegmentGrade,
            )
            .join(TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id)
            .outerjoin(Track, TrackSegment.track_id == Track.track_id)
            .where(
                TrackSegment.is_active.is_(True),
                func.ST_Intersects(TrackSegment.geom, env_geom),
                TrackSegmentGrade.is_obstacle_hotspot.is_(True),
            )
            .order_by(TrackSegment.track_id.asc(), TrackSegment.segment_seq.asc().nullslast())
        )
        for seg_id, tid, tname, geom_json, lon, lat, grd in hotspot_rows.yield_per(200):
            lb = grd.live_breakdown if isinstance(grd.live_breakdown, dict) else {}
            if counts["hotspots"] > 0:
                fh.write(",")
            rc = grd.rainfall_context or {}
            feat = {
                "type": "Feature",
                "id": seg_id,
                "geometry": json.loads(geom_json),
                "properties": {
                    "segment_id": seg_id,
                    "track_id": tid,
                    "track_name": tname,
                    "lon": round(float(lon), 6),
                    "lat": round(float(lat), 6),
                    "avg_speed_kmh": lb.get("avg_speed_kmh", grd.avg_speed_kmh),
                    "median_roughness_rms": lb.get("median_roughness_rms"),
                    "obstacle_reasons": list(lb.get("obstacle_reasons") or []),
                    "live_grade": grd.live_grade or grd.effective_grade,
                    "wet_risk_level": rc.get("wet_risk_level", grd.wet_risk_level),
                },
            }
            fh.write(json.dumps(feat, separators=(",", ":")))
            counts["hotspots"] += 1
        fh.write("]}")

        # 8. Final counts object
        fh.write(f',"counts":{json.dumps(counts, separators=(",", ":"))}')
        fh.write("}\n")

    return counts


def _build_region_pmtiles(
    session: Session,
    region: PackRegionSpec,
    staging_dir: Path,
    *,
    dem_path: Path | None,
) -> tuple[bool, bool, list[float]]:
    """Build region-clipped ``vector.pmtiles`` and ``terrain.pmtiles`` when tools & data exist."""
    can_vector = shutil.which("tippecanoe") is not None
    can_terrain = (
        dem_path is not None
        and shutil.which("gdalwarp") is not None
        and shutil.which("gdal_translate") is not None
        and shutil.which("pmtiles") is not None
    )

    if not can_vector and not can_terrain:
        return (False, False, list(region.bbox))

    with tempfile.TemporaryDirectory(prefix=f"tf_pack_tiles_{region.region_id}_") as tmp_s:
        tmp_dir = Path(tmp_s)
        try:
            tile_res = build_tiles(
                session,
                tmp_dir,
                dem_path=dem_path if (can_vector or can_terrain) else None,
                bbox=region.bbox,
                minzoom=region.default_minzoom,
                maxzoom=region.default_maxzoom,
                terrain_minzoom=region.terrain_minzoom,
                terrain_maxzoom=region.terrain_maxzoom,
                build_vector=can_vector,
                build_terrain=can_terrain,
            )
        except Exception as exc:
            logger.warning(
                "Tile generation skipped/partial for pack %s (%s); continuing with snapshot",
                region.region_id,
                exc,
            )
            return (False, False, list(region.bbox))

        vector_available = False
        terrain_available = False

        src_vec = tmp_dir / VECTOR_PMTILES_FILENAME
        if src_vec.is_file() and src_vec.stat().st_size > 0:
            shutil.move(str(src_vec), str(staging_dir / VECTOR_TILE_FILENAME))
            vector_available = True

        src_ter = tmp_dir / TERRAIN_PMTILES_FILENAME
        if src_ter.is_file() and src_ter.stat().st_size > 0:
            shutil.move(str(src_ter), str(staging_dir / TERRAIN_TILE_FILENAME))
            terrain_available = True

        bounds = list(tile_res.manifest.bounds) if tile_res.manifest.bounds else list(region.bbox)
        return (vector_available, terrain_available, bounds)


def _create_streaming_bundle_tar_gz(
    staging_dir: Path,
    region_id: str,
    rel_files: list[str],
    bundle_path: Path,
) -> int:
    """Create a compressed tar.gz archive streaming files disk-to-disk without RAM spikes."""
    with tarfile.open(bundle_path, mode="w:gz", compresslevel=6) as tar:
        for rel_name in rel_files:
            file_path = staging_dir / rel_name
            if file_path.is_file():
                tar.add(file_path, arcname=f"{region_id}/{rel_name}", recursive=False)
    return int(bundle_path.stat().st_size)


def _promote_staging_dir(staging_dir: Path, target_dir: Path) -> None:
    """Atomically swap a staged pack directory into ``target_dir``."""
    backup_dir: Path | None = None
    if target_dir.exists():
        backup_dir = target_dir.parent / f".{target_dir.name}.old-{uuid.uuid4().hex[:8]}"
        target_dir.rename(backup_dir)
    try:
        staging_dir.rename(target_dir)
    except Exception:
        if backup_dir is not None and backup_dir.exists() and not target_dir.exists():
            backup_dir.rename(target_dir)
        raise
    finally:
        if backup_dir is not None and backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)


def _upsert_offline_pack_row(
    session: Session,
    region: PackRegionSpec,
    *,
    version: str,
    status_val: str,
    vector_rel: str,
    terrain_rel: str | None,
    snapshot_rel: str,
    size_bytes: int,
    sha256_hex: str | None,
    built_at: datetime,
) -> None:
    bbox_poly = from_shape(box(*region.bbox), srid=4326)
    stmt = (
        pg_insert(OfflinePack)
        .values(
            pack_id=region.region_id,
            name=region.name,
            region_type=region.region_type,
            version=version,
            bbox_geom=bbox_poly,
            utm_zones=list(region.utm_zones),
            vector_pmtiles_path=vector_rel,
            terrain_pmtiles_path=terrain_rel,
            snapshot_json_path=snapshot_rel,
            size_bytes=size_bytes,
            sha256=sha256_hex,
            attributions=list(region.attributions),
            status=status_val,
            built_at=built_at,
        )
        .on_conflict_do_update(
            index_elements=[OfflinePack.pack_id],
            set_={
                "name": region.name,
                "region_type": region.region_type,
                "version": version,
                "bbox_geom": bbox_poly,
                "utm_zones": list(region.utm_zones),
                "vector_pmtiles_path": vector_rel,
                "terrain_pmtiles_path": terrain_rel,
                "snapshot_json_path": snapshot_rel,
                "size_bytes": size_bytes,
                "sha256": sha256_hex,
                "attributions": list(region.attributions),
                "status": status_val,
                "built_at": built_at,
            },
        )
    )
    session.execute(stmt)
    session.commit()


def build_regional_pack(
    session: Session,
    region_id: str,
    output_dir: Path | str | None = None,
    *,
    dem_path: Path | str | None = None,
    version: str | None = None,
    skip_tiles: bool = False,
    prebuilt_vector_pmtiles: Path | str | None = None,
    prebuilt_terrain_pmtiles: Path | str | None = None,
    prebuilt_tile_bounds: list[float] | None = None,
) -> PackBuildResult:
    """Build a regional offline pack streaming disk-to-disk and record it in ``offline_packs``."""
    region = get_pack_region(region_id)
    packs_root = get_packs_dir(output_dir).resolve()
    packs_root.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    resolved_version = version or now.strftime("%Y.%m.%d")
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    staging_dir = packs_root / f".{region.region_id}.staging-{uuid.uuid4().hex[:8]}"
    staging_dir.mkdir(parents=True, exist_ok=True)

    snapshot_rel = f"{region.region_id}/{SNAPSHOT_FILENAME}"
    vector_rel = f"{region.region_id}/{VECTOR_TILE_FILENAME}"
    terrain_rel: str | None = None

    try:
        # 1. Stream PostGIS snapshot.json
        staged_snapshot = staging_dir / SNAPSHOT_FILENAME
        counts = _write_region_snapshot_streaming(
            session,
            region,
            staged_snapshot,
            version=resolved_version,
            generated_at=generated_at,
        )

        # 2. Reuse pre-built region PMTiles if supplied, otherwise build region-clipped PMTiles
        vector_available = False
        terrain_available = False
        tile_bounds = list(prebuilt_tile_bounds) if prebuilt_tile_bounds else list(region.bbox)
        prebuilt_vec = (
            Path(prebuilt_vector_pmtiles).expanduser().resolve()
            if prebuilt_vector_pmtiles is not None
            else None
        )
        prebuilt_ter = (
            Path(prebuilt_terrain_pmtiles).expanduser().resolve()
            if prebuilt_terrain_pmtiles is not None
            else None
        )
        if not skip_tiles:
            if (
                prebuilt_vec is not None
                and prebuilt_vec.is_file()
                and prebuilt_vec.stat().st_size > 0
            ):
                shutil.copy2(prebuilt_vec, staging_dir / VECTOR_TILE_FILENAME)
                vector_available = True
                if (
                    prebuilt_ter is not None
                    and prebuilt_ter.is_file()
                    and prebuilt_ter.stat().st_size > 0
                ):
                    shutil.copy2(prebuilt_ter, staging_dir / TERRAIN_TILE_FILENAME)
                    terrain_available = True
            else:
                intersecting_dem = _resolve_intersecting_dem(region, dem_path)
                vector_available, terrain_available, tile_bounds = _build_region_pmtiles(
                    session,
                    region,
                    staging_dir,
                    dem_path=intersecting_dem,
                )

        terrain_rel = f"{region.region_id}/{TERRAIN_TILE_FILENAME}" if terrain_available else None

        # 3. Compute streaming SHA-256 & byte sizes for all data artifacts
        files_map: dict[str, PackFileEntry] = {}
        artifact_names = [SNAPSHOT_FILENAME]
        if vector_available:
            artifact_names.append(VECTOR_TILE_FILENAME)
        if terrain_available:
            artifact_names.append(TERRAIN_TILE_FILENAME)

        total_size_bytes = 0
        for fname in artifact_names:
            fpath = staging_dir / fname
            fsize = int(fpath.stat().st_size)
            fhash = _sha256_file(fpath)
            files_map[fname] = PackFileEntry(path=fname, size_bytes=fsize, sha256=fhash)
            total_size_bytes += fsize

        # 4. Write manifest.json (pre-bundle, then update bundle_size_bytes)
        rid = region.region_id
        manifest = PackManifest(
            region_id=rid,
            pack_id=rid,
            name=region.name,
            description=region.description,
            region_type=region.region_type,
            version=resolved_version,
            generated_at=generated_at,
            bbox=list(region.bbox),
            bounds=[round(float(v), 6) for v in tile_bounds],
            utm_zones=list(region.utm_zones),
            minzoom=region.default_minzoom,
            maxzoom=region.default_maxzoom,
            vector_available=vector_available,
            terrain_available=terrain_available,
            manifest_url=f"/api/packs/{rid}/{MANIFEST_FILENAME}",
            snapshot_url=f"/api/packs/{rid}/{SNAPSHOT_FILENAME}",
            bundle_url=f"/api/packs/{rid}/{BUNDLE_FILENAME}",
            vector_pmtiles_url=f"/api/packs/{rid}/tiles/{VECTOR_TILE_FILENAME}",
            terrain_pmtiles_url=f"/api/packs/{rid}/tiles/{TERRAIN_TILE_FILENAME}",
            counts=counts,
            track_count=counts["tracks"],
            segment_count=counts["segments"],
            poi_count=counts["pois"],
            total_size_bytes=total_size_bytes,
            bundle_size_bytes=0,
            files=files_map,
            attributions=list(region.attributions),
        )

        staged_manifest = staging_dir / MANIFEST_FILENAME
        staged_manifest.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")

        # 5. Stream downloadable bundle.tar.gz containing manifest + all pack artifacts
        staged_bundle = staging_dir / BUNDLE_FILENAME
        bundle_files = [MANIFEST_FILENAME, *artifact_names]
        bundle_size = _create_streaming_bundle_tar_gz(
            staging_dir, rid, bundle_files, staged_bundle
        )

        # Record final bundle size inside manifest.json and refresh bundle so manifest matches disk
        manifest = manifest.model_copy(
            update={
                "bundle_size_bytes": bundle_size,
                "total_size_bytes": total_size_bytes + int(staged_manifest.stat().st_size),
            }
        )
        staged_manifest.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
        manifest_size = int(staged_manifest.stat().st_size)
        manifest = manifest.model_copy(
            update={"total_size_bytes": total_size_bytes + manifest_size}
        )
        staged_manifest.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
        # Rebuild bundle with final manifest so extracted manifest == served manifest byte-for-byte
        bundle_size = _create_streaming_bundle_tar_gz(
            staging_dir, rid, bundle_files, staged_bundle
        )
        manifest_sha256 = _sha256_file(staged_manifest)

        # 6. Atomically promote staging directory to <packs_root>/<region_id>
        target_dir = packs_root / rid
        _promote_staging_dir(staging_dir, target_dir)

        # 7. Record pack metadata in PostGIS offline_packs table
        _upsert_offline_pack_row(
            session,
            region,
            version=resolved_version,
            status_val="ready",
            vector_rel=vector_rel if vector_available else "",
            terrain_rel=terrain_rel,
            snapshot_rel=snapshot_rel,
            size_bytes=manifest.total_size_bytes,
            sha256_hex=manifest_sha256,
            built_at=now,
        )

        return PackBuildResult(
            region_id=rid,
            pack_dir=target_dir,
            manifest_path=target_dir / MANIFEST_FILENAME,
            snapshot_path=target_dir / SNAPSHOT_FILENAME,
            bundle_path=target_dir / BUNDLE_FILENAME,
            vector_pmtiles_path=(
                target_dir / VECTOR_TILE_FILENAME if vector_available else None
            ),
            terrain_pmtiles_path=(
                target_dir / TERRAIN_TILE_FILENAME if terrain_available else None
            ),
            manifest_sha256=manifest_sha256,
            manifest=manifest,
        )
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        try:
            session.rollback()
            _upsert_offline_pack_row(
                session,
                region,
                version=resolved_version,
                status_val="failed",
                vector_rel="",
                terrain_rel=None,
                snapshot_rel=snapshot_rel,
                size_bytes=0,
                sha256_hex=None,
                built_at=now,
            )
        except Exception:
            session.rollback()
        raise


def load_pack_manifest(packs_root: Path | str, region_id: str) -> PackManifest | None:
    """Load and validate ``manifest.json`` for a built regional pack, or None if missing."""
    region = get_pack_region(region_id)
    base = Path(packs_root).expanduser().resolve()
    manifest_path = (base / region.region_id / MANIFEST_FILENAME).resolve()
    if not manifest_path.is_relative_to(base) or not manifest_path.is_file():
        return None
    try:
        return PackManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed reading pack manifest %s: %s", manifest_path, exc)
        return None
