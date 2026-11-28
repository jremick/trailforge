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

import gzip
import hashlib
import json
import logging
import math
import os
import shutil
import tarfile
import tempfile
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from geoalchemy2.shape import from_shape
from pydantic import BaseModel, Field
from shapely.geometry import box
from sqlalchemy import Text, any_, bindparam, func, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from trailforge.config import get_settings
from trailforge.db.models import (
    POI,
    CAPADPark,
    ClosureAlert,
    ConditionReport,
    OfflinePack,
    OfflinePackTelemetryRevision,
    SegmentClosureLink,
    SourceWaterway,
    Track,
    TrackSegment,
    TrackSegmentGrade,
)
from trailforge.feeds.queries import get_closures_for_track_ids, list_closures
from trailforge.grading.baseline import SegmentGradeInput, compute_baseline_grade
from trailforge.grading.live import (
    LIVE_FORMULA_VERSION,
    MIN_PUBLIC_CONTRIBUTORS,
    get_live_summaries_for_tracks,
    has_public_telemetry,
    weather_adjusted_grade,
)
from trailforge.ingestion.terrain import read_raster_bounds
from trailforge.packs.catalog import (
    PACK_ATTRIBUTIONS,
    PACK_REGIONS,
    PackRegionSpec,
    get_pack_region,
)
from trailforge.reports.queries import list_reports
from trailforge.tiles.builder import (
    DEFAULT_SNOWLINE_BANDS_M,
    STATIC_TILE_TELEMETRY_POLICY,
    TERRAIN_PMTILES_FILENAME,
    VECTOR_PMTILES_FILENAME,
    build_tiles,
)

logger = logging.getLogger(__name__)

MANIFEST_FILENAME: Final[str] = "manifest.json"
SNAPSHOT_FILENAME: Final[str] = "snapshot.json"
SHARDS_DIRNAME: Final[str] = "shards"
VECTOR_TILE_FILENAME: Final[str] = "vector.pmtiles"
TERRAIN_TILE_FILENAME: Final[str] = "terrain.pmtiles"
STATIC_PACK_TELEMETRY_POLICY: Final[str] = "static_v2"
BUNDLE_FILENAME: Final[str] = "bundle.tar.gz"

SNAPSHOT_FORMAT_INLINE: Final[Literal["inline_v1"]] = "inline_v1"
SNAPSHOT_FORMAT_SHARDED: Final[Literal["sharded_v2"]] = "sharded_v2"
SHARD_HASH_ALGO: Final[str] = "fnv1a32"
PACK_SHARD_THRESHOLD: Final[int] = 500
MIN_SHARD_COUNT: Final[int] = 4
MAX_SHARD_COUNT: Final[int] = 64
_MAX_SHARD_PROFILE_POINTS: Final[int] = 40
_MAX_SHARD_POLYLINE_POINTS: Final[int] = 64
_SHARD_SIMPLIFY_TOLERANCE_DEG: Final[float] = 0.0001
_SHARD_SQL_SUBBATCH: Final[int] = 250

_GRADE_RANK: Final[dict[str, int]] = {"Easy": 1, "Medium": 2, "Hard": 3, "Extreme": 4}
_RANK_GRADE: Final[dict[int, str]] = {v: k for k, v in _GRADE_RANK.items()}
_SURFACE_RANK: Final[dict[str, int]] = {
    "unknown": 0,
    "sealed": 1,
    "gravel_compacted": 2,
    "dirt_unsealed": 3,
    "rough_rock_mud_sand": 4,
}
_TRACKTYPE_RANK: Final[dict[str, int]] = {
    "grade1": 1,
    "grade2": 2,
    "grade3": 3,
    "grade4": 4,
    "grade5": 5,
}
_ACCESS_STRICTNESS: Final[dict[str, int]] = {
    "public": 0,
    "seasonal": 1,
    "permit": 2,
    "private": 3,
    "restricted": 4,
    "closed": 5,
}

_HASH_CHUNK_BYTES: Final[int] = 65_536
_DEFAULT_DEMO_DEM: Final[Path] = Path("tests/fixtures/dem_alps_corridor.tif")


def fnv1a_32(value: str) -> int:
    """Deterministic FNV-1a 32-bit hash over UTF-8 bytes (matches TypeScript client)."""
    h = 0x811C9DC5
    for byte in value.encode("utf-8"):
        h ^= byte
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def compute_shard_count(track_count: int) -> int:
    """Compute deterministic shard count K in [4, 64] (~1,000 tracks/shard)."""
    if track_count <= 0:
        return MIN_SHARD_COUNT
    return min(MAX_SHARD_COUNT, max(MIN_SHARD_COUNT, math.ceil(track_count / 1000)))


def shard_index_for_track(track_id: str, shard_count: int) -> int:
    """Map ``track_id`` to a deterministic shard index in ``[0, shard_count - 1]``."""
    if shard_count <= 1:
        return 0
    return fnv1a_32(track_id) % shard_count


def shard_filename(shard_index: int) -> str:
    """Return canonical gzipped shard filename (e.g. ``tracks_00.json.gz``)."""
    return f"tracks_{shard_index:02d}.json.gz"


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
    community_telemetry_policy: str | None = None
    community_telemetry_revision: uuid.UUID | None = None
    snapshot_format: Literal["inline_v1", "sharded_v2"] = SNAPSHOT_FORMAT_INLINE
    shard_count: int = 0
    shard_hash: str | None = None
    shards_base_url: str | None = None
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
    bundle_url: str | None = None
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
    bundle_path: Path | None = None
    vector_pmtiles_path: Path | None = None
    terrain_pmtiles_path: Path | None = None
    manifest_sha256: str
    manifest: PackManifest


def get_pack_telemetry_revision(session: Session, region_id: str) -> uuid.UUID | None:
    return session.scalar(
        select(OfflinePackTelemetryRevision.revision)
        .where(OfflinePackTelemetryRevision.region_id == region_id)
    )


def _is_current_static_vector(path: Path) -> bool:
    try:
        manifest = json.loads(path.with_name("manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(manifest, dict)
        and manifest.get("community_telemetry_policy") == STATIC_TILE_TELEMETRY_POLICY
    )


def pack_manifest_is_current(manifest: PackManifest, revision: uuid.UUID | None) -> bool:
    if manifest.community_telemetry_policy == LIVE_FORMULA_VERSION:
        return manifest.community_telemetry_revision == revision
    return manifest.community_telemetry_policy == STATIC_PACK_TELEMETRY_POLICY


def _invalidate_pack_regions(session: Session, region_ids: list[str]) -> None:
    if not region_ids:
        return
    replacement = uuid.uuid4()
    session.execute(
        pg_insert(OfflinePackTelemetryRevision)
        .values([{"region_id": rid, "revision": replacement} for rid in region_ids])
        .on_conflict_do_update(
            index_elements=[OfflinePackTelemetryRevision.region_id],
            set_={"revision": replacement},
        )
    )


def invalidate_all_pack_telemetry(session: Session) -> None:
    """Expire every downloadable pack that contains live telemetry."""
    _invalidate_pack_regions(session, list(PACK_REGIONS))


def invalidate_pack_telemetry(
    session: Session, *, segment_ids: list[str], track_ids: list[str]
) -> None:
    """Invalidate regional files that may contain a withdrawn contribution's aggregates."""
    if not segment_ids:
        return
    segments = bindparam("segment_ids", segment_ids, type_=ARRAY(Text()))
    tracks = bindparam("track_ids", track_ids, type_=ARRAY(Text()))
    affected_regions: list[str] = []
    for region in PACK_REGIONS.values():
        bounds = func.ST_MakeEnvelope(*region.bbox, 4326)
        segment_hit = select(TrackSegment.segment_id).where(
            TrackSegment.segment_id == any_(segments),
            func.ST_Intersects(TrackSegment.geom, bounds),
        ).exists()
        track_hit = select(Track.track_id).where(
            Track.track_id == any_(tracks),
            func.ST_Intersects(Track.bbox_geom, bounds),
        ).exists()
        if session.scalar(select(or_(segment_hit, track_hit))):
            affected_regions.append(region.region_id)
    _invalidate_pack_regions(session, affected_regions)


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


def _list_region_track_ids(session: Session, env_geom: Any) -> list[str]:
    """Return active canonical track IDs intersecting ``env_geom`` in deterministic order."""
    track_ids_from_segs = (
        select(TrackSegment.track_id)
        .where(
            TrackSegment.is_active.is_(True),
            TrackSegment.track_id.is_not(None),
            func.ST_Intersects(TrackSegment.geom, env_geom),
        )
        .distinct()
    )
    return list(
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


def _extract_linestring_coords(geom_json: str | None) -> list[list[float]]:
    """Extract 2D ``[lon, lat]`` vertices from a GeoJSON LineString/MultiLineString."""
    if not geom_json:
        return []
    try:
        parsed = json.loads(geom_json)
    except ValueError:
        return []
    if not isinstance(parsed, dict):
        return []
    gtype = parsed.get("type")
    raw_coords = parsed.get("coordinates")
    if not isinstance(raw_coords, list):
        return []
    out: list[list[float]] = []
    if gtype == "LineString":
        for pt in raw_coords:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                out.append([round(float(pt[0]), 5), round(float(pt[1]), 5)])
    elif gtype == "MultiLineString":
        for part in raw_coords:
            if not isinstance(part, list):
                continue
            for pt in part:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    out.append([round(float(pt[0]), 5), round(float(pt[1]), 5)])
    return out


def _downsample_coords(
    coords: list[list[float]], max_points: int = _MAX_SHARD_POLYLINE_POINTS
) -> list[list[float]]:
    """Downsample a polyline evenly while preserving start and end endpoints."""
    if len(coords) <= max_points:
        return coords
    if max_points <= 2:
        return [coords[0], coords[-1]]
    step = (len(coords) - 1) / float(max_points - 1)
    indices = sorted({0, len(coords) - 1, *(int(round(i * step)) for i in range(max_points))})
    return [coords[i] for i in indices[:max_points]]


def _downsample_elevation_profile(
    points: list[dict[str, Any]], max_points: int = _MAX_SHARD_PROFILE_POINTS
) -> list[dict[str, Any]]:
    """Downsample an elevation profile preserving start, end, min/max elev, and max slope."""
    if len(points) <= max_points:
        return points
    if max_points <= 2:
        return [points[0], points[-1]]

    last_idx = len(points) - 1
    keep: set[int] = {0, last_idx}
    min_e_idx = min(range(len(points)), key=lambda i: float(points[i]["elev_m"]))
    max_e_idx = max(range(len(points)), key=lambda i: float(points[i]["elev_m"]))
    max_s_idx = max(range(len(points)), key=lambda i: float(points[i].get("slope_deg") or 0.0))
    keep.update((min_e_idx, max_e_idx, max_s_idx))

    remaining = max(0, max_points - len(keep))
    if remaining > 0:
        step = last_idx / float(remaining + 1)
        for k in range(1, remaining + 1):
            idx = min(last_idx, max(0, int(round(k * step))))
            keep.add(idx)

    return [points[i] for i in sorted(keep)]


def _stream_region_auxiliary_sections(
    session: Session,
    fh: Any,
    *,
    env_geom: Any,
    track_id_set: set[str],
    counts: dict[str, int],
    include_community: bool,
) -> None:
    """Stream POIs, CAPAD parks, closures, reports, hotspots, and final counts into ``fh``."""
    track_ids_from_segs = (
        select(TrackSegment.track_id)
        .where(
            TrackSegment.is_active.is_(True),
            TrackSegment.track_id.is_not(None),
            func.ST_Intersects(TrackSegment.geom, env_geom),
        )
        .distinct()
    )

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
        for rep in list_reports(
            session, include_resolved=False, include_vote_assessment=False, limit=500
        ):
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
            TrackSegmentGrade.sample_count >= MIN_PUBLIC_CONTRIBUTORS,
            TrackSegmentGrade.live_breakdown["version"].astext == LIVE_FORMULA_VERSION,
            include_community,
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


def _write_region_snapshot_streaming(
    session: Session,
    region: PackRegionSpec,
    snapshot_path: Path,
    *,
    version: str,
    generated_at: str,
    include_community: bool = True,
    track_ids: list[str] | None = None,
) -> dict[str, int]:
    """Stream a complete offline region snapshot JSON directly to disk (O(1) item memory)."""
    # Local import avoids circular imports with api/tracks
    from trailforge.api.tracks import get_track_details_batch

    env_geom = func.ST_MakeEnvelope(*region.bbox, 4326)
    resolved_track_ids = (
        track_ids if track_ids is not None else _list_region_track_ids(session, env_geom)
    )
    track_id_set = set(resolved_track_ids)

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
        for start in range(0, len(resolved_track_ids), batch_size):
            chunk_ids = resolved_track_ids[start : start + batch_size]
            details = get_track_details_batch(
                session, chunk_ids, include_community=include_community
            )
            for detail in details:
                track_dict = detail.model_dump(mode="json")
                if written_tracks > 0:
                    fh.write(",")
                fh.write(json.dumps(track_dict, separators=(",", ":")))
                written_tracks += 1
                counts["tracks"] += 1
                counts["segments"] += len(track_dict.get("segments") or [])
        fh.write("]")

        _stream_region_auxiliary_sections(
            session,
            fh,
            env_geom=env_geom,
            track_id_set=track_id_set,
            counts=counts,
            include_community=include_community,
        )

    return counts


def _write_region_sharded_snapshot_streaming(
    session: Session,
    region: PackRegionSpec,
    staging_dir: Path,
    snapshot_path: Path,
    *,
    version: str,
    generated_at: str,
    include_community: bool = True,
    track_ids: list[str] | None = None,
) -> tuple[dict[str, int], list[str], int]:
    """Stream compact ``sharded_v2`` snapshot index + gzipped track detail shards.

    Normalizes POIs, CAPAD parks, closures, and reports out of per-track payloads and
    buckets compact track detail records across ``K`` deterministic FNV-1a gzipped shards
    so massive regions (50k+ tracks) stay under < 300 MB build RAM and browser IndexedDB budgets.
    """
    env_geom = func.ST_MakeEnvelope(*region.bbox, 4326)
    resolved_track_ids = (
        track_ids if track_ids is not None else _list_region_track_ids(session, env_geom)
    )
    track_id_set = set(resolved_track_ids)
    shard_count = compute_shard_count(len(resolved_track_ids))

    shards_dir = staging_dir / SHARDS_DIRNAME
    shards_dir.mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = {
        "tracks": 0,
        "segments": 0,
        "pois": 0,
        "capad_parks": 0,
        "closures": 0,
        "reports": 0,
        "hotspots": 0,
    }

    # Bucket track IDs deterministically by fnv1a_32(track_id) % shard_count
    shard_buckets: list[list[str]] = [[] for _ in range(shard_count)]
    for tid in resolved_track_ids:
        shard_buckets[shard_index_for_track(tid, shard_count)].append(tid)

    # Precompute track -> (closure_status, [closure_id, ...]) ONLY for affected tracks
    closures_by_track: dict[str, tuple[str, list[int]]] = {}
    if resolved_track_ids:
        affected_track_ids = list(
            session.execute(
                select(TrackSegment.track_id)
                .join(SegmentClosureLink, SegmentClosureLink.segment_id == TrackSegment.segment_id)
                .where(
                    TrackSegment.is_active.is_(True),
                    TrackSegment.track_id.is_not(None),
                    func.ST_Intersects(TrackSegment.geom, env_geom),
                )
                .distinct()
            )
            .scalars()
            .all()
        )
        if affected_track_ids:
            raw_closures = get_closures_for_track_ids(session, affected_track_ids)
            for tid, entry in raw_closures.items():
                c_status = str(entry.get("closure_status", "open"))
                c_ids = [
                    int(c["closure_id"])
                    for c in (entry.get("active_closures") or [])
                    if c.get("closure_id") is not None
                ]
                closures_by_track[tid] = (c_status, c_ids)

    computed_grade_by_track: dict[str, str | None] = {}
    shard_rel_paths: list[str] = []

    # 1. Stream one gzipped shard at a time (O(1 shard) RAM)
    for shard_idx, bucket_ids in enumerate(shard_buckets):
        shard_tracks: dict[str, dict[str, Any]] = {}

        for start in range(0, len(bucket_ids), _SHARD_SQL_SUBBATCH):
            sub_ids = bucket_ids[start : start + _SHARD_SQL_SUBBATCH]
            if not sub_ids:
                continue

            track_rows = session.execute(
                select(
                    Track.track_id,
                    Track.name,
                    Track.alt_names,
                    Track.state,
                    Track.primary_source,
                    Track.has_osm_source,
                    Track.trail_class,
                    Track.is_vehicular,
                    Track.total_length_m,
                    Track.elev_min_m,
                    Track.elev_max_m,
                    Track.elev_gain_m,
                    Track.max_slope_deg,
                    Track.waterway_crossing_count,
                    Track.primary_park_id,
                    Track.effective_grade,
                    Track.is_active,
                    CAPADPark.name.label("primary_park_name"),
                    func.ST_XMin(Track.bbox_geom).label("xmin"),
                    func.ST_YMin(Track.bbox_geom).label("ymin"),
                    func.ST_XMax(Track.bbox_geom).label("xmax"),
                    func.ST_YMax(Track.bbox_geom).label("ymax"),
                )
                .outerjoin(CAPADPark, Track.primary_park_id == CAPADPark.id)
                .where(Track.track_id.in_(sub_ids))
            ).all()
            track_meta_by_id = {row.track_id: row for row in track_rows}
            found_ids = [tid for tid in sub_ids if tid in track_meta_by_id]
            if not found_ids:
                continue

            seg_rows_all = session.execute(
                select(
                    TrackSegment.segment_id,
                    TrackSegment.track_id,
                    TrackSegment.start_node_key,
                    TrackSegment.end_node_key,
                    TrackSegment.surface_class,
                    TrackSegment.tracktype,
                    TrackSegment.is_4wd_only,
                    TrackSegment.is_4wd_recommended,
                    TrackSegment.access_status,
                    TrackSegment.has_gate_barrier,
                    TrackSegment.is_bridged,
                    TrackSegment.length_m,
                    TrackSegment.elev_gain_m,
                    TrackSegment.elev_loss_m,
                    TrackSegment.max_slope_deg,
                    TrackSegment.sustained_slope_deg,
                    TrackSegment.is_steep_highlight,
                    TrackSegment.is_above_snowline,
                    TrackSegment.elevation_profile_m,
                    TrackSegment.dem_tier,
                    TrackSegment.waterway_crossing_count,
                    TrackSegment.has_perennial_crossing,
                    TrackSegment.source_refs,
                    TrackSegment.capad_park_id,
                    func.ST_AsGeoJSON(
                        func.ST_SimplifyPreserveTopology(
                            TrackSegment.geom, _SHARD_SIMPLIFY_TOLERANCE_DEG
                        ),
                        5,
                    ).label("geom_json"),
                    TrackSegmentGrade.baseline_grade,
                    TrackSegmentGrade.baseline_score,
                    TrackSegmentGrade.effective_grade.label("grade_effective_grade"),
                    TrackSegmentGrade.sample_count,
                    TrackSegmentGrade.live_breakdown,
                    TrackSegmentGrade.rainfall_context,
                    TrackSegmentGrade.wet_risk_level,
                )
                .outerjoin(
                    TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id
                )
                .where(
                    TrackSegment.track_id.in_(found_ids),
                    TrackSegment.is_active.is_(True),
                )
                .order_by(
                    TrackSegment.track_id.asc(),
                    TrackSegment.segment_seq.asc().nullslast(),
                    TrackSegment.segment_id.asc(),
                )
            ).all()

            segs_by_track: dict[str, list[Any]] = {tid: [] for tid in found_ids}
            crossing_seg_ids: list[str] = []
            for srow in seg_rows_all:
                if srow.track_id in segs_by_track:
                    segs_by_track[srow.track_id].append(srow)
                    if int(srow.waterway_crossing_count or 0) > 0:
                        crossing_seg_ids.append(srow.segment_id)

            # Spatial waterway lookup ONLY for segments flagged with waterway crossings
            ww_by_track: dict[str, list[Any]] = {tid: [] for tid in found_ids}
            if crossing_seg_ids:
                ww_rows = session.execute(
                    select(
                        TrackSegment.track_id,
                        SourceWaterway.source_feature_id,
                        SourceWaterway.name,
                        SourceWaterway.feature_type,
                        SourceWaterway.perenniality,
                        SourceWaterway.hierarchy,
                        TrackSegment.segment_id,
                        TrackSegment.is_bridged,
                    )
                    .join(
                        TrackSegment,
                        func.ST_DWithin(
                            func.ST_Transform(TrackSegment.geom, 3577),
                            func.ST_Transform(SourceWaterway.geom, 3577),
                            1.0,
                        ),
                    )
                    .where(
                        TrackSegment.segment_id.in_(crossing_seg_ids),
                        func.lower(SourceWaterway.feature_type) != "connector",
                        func.ST_DWithin(TrackSegment.geom, SourceWaterway.geom, 0.0005),
                    )
                ).all()
                for wrow in ww_rows:
                    if wrow.track_id in ww_by_track:
                        ww_by_track[wrow.track_id].append(wrow)

            live_map = (
                get_live_summaries_for_tracks(session, found_ids, include_community=True)
                if include_community
                else {}
            )

            for tid in found_ids:
                trk = track_meta_by_id[tid]
                srows = segs_by_track.get(tid, [])

                merged_refs: list[dict[str, Any]] = []
                seen_ref_keys: set[tuple[str, str]] = set()
                grade_dist: Counter[str] = Counter()
                worst_rank = 0
                rep_seg_id: str | None = None
                rep_score: float | None = None
                rep_sort_key: tuple[float, float, int, str] | None = None
                elev_loss_total = 0.0
                max_seg_gain = 0.0
                max_seg_loss = 0.0
                max_slope_val: float | None = None
                max_sustained_slope: float | None = None
                worst_surface = "unknown"
                worst_tracktype: str | None = None
                is_4wd_only = False
                is_4wd_recommended = False
                has_gate_barrier = False
                any_unbridged_perennial = False
                dem_tier_val: str | None = None
                access_status = "public"
                steep_count = 0
                snowline_count = 0
                park_ids: list[int] = []
                seen_park_ids: set[int] = set()
                if trk.primary_park_id is not None:
                    park_ids.append(int(trk.primary_park_id))
                    seen_park_ids.add(int(trk.primary_park_id))

                static_rainfall_ctx: dict[str, Any] | None = None
                raw_profile: list[dict[str, Any]] = []
                stitched_coords: list[list[float]] = []
                seg_mid_dist: dict[str, float] = {}
                cum_dist = 0.0
                curr_exit_node: str | None = None

                for idx, seg in enumerate(srows):
                    # Orientation chaining for elevation profile & simplified polyline
                    reverse_seg = False
                    if idx == 0:
                        if len(srows) >= 2:
                            nxt = srows[1]
                            nxt_nodes = {nxt.start_node_key, nxt.end_node_key}
                            if seg.end_node_key in nxt_nodes:
                                curr_exit_node = seg.end_node_key
                            elif seg.start_node_key in nxt_nodes:
                                reverse_seg = True
                                curr_exit_node = seg.start_node_key
                            else:
                                curr_exit_node = seg.end_node_key
                        else:
                            curr_exit_node = seg.end_node_key
                    else:
                        if seg.start_node_key == curr_exit_node:
                            curr_exit_node = seg.end_node_key
                        elif seg.end_node_key == curr_exit_node:
                            reverse_seg = True
                            curr_exit_node = seg.start_node_key
                        else:
                            curr_exit_node = seg.end_node_key

                    seg_len = float(seg.length_m or 0.0)
                    seg_mid_dist[seg.segment_id] = round(cum_dist + (seg_len * 0.5), 1)

                    seg_coords = _extract_linestring_coords(seg.geom_json)
                    if reverse_seg and len(seg_coords) >= 2:
                        seg_coords = list(reversed(seg_coords))
                    for pt in seg_coords:
                        if not stitched_coords or stitched_coords[-1] != pt:
                            stitched_coords.append(pt)

                    # Segment effective grade under active telemetry policy
                    seg_eff_grade: str | None = None
                    if seg.baseline_grade is not None:
                        share_live = include_community and has_public_telemetry(
                            seg.sample_count, seg.live_breakdown
                        )
                        seg_eff_grade = (
                            seg.grade_effective_grade
                            if share_live
                            else weather_adjusted_grade(
                                seg.baseline_grade, seg.baseline_score, seg.rainfall_context
                            )
                        ) or seg.baseline_grade
                        if (
                            not include_community
                            and static_rainfall_ctx is None
                            and seg.rainfall_context
                        ):
                            static_rainfall_ctx = seg.rainfall_context

                    raw_pts = seg.elevation_profile_m or []
                    seg_slope = round(float(seg.max_slope_deg or 0.0), 2)
                    if raw_pts:
                        parsed_pts: list[tuple[float, float]] = [
                            (float(p[0]), float(p[1]))
                            for p in raw_pts
                            if isinstance(p, (list, tuple)) and len(p) >= 2
                        ]
                        if parsed_pts:
                            span = parsed_pts[-1][0]
                            ordered_pts = (
                                [(max(0.0, span - d), z) for d, z in reversed(parsed_pts)]
                                if reverse_seg
                                else parsed_pts
                            )
                            for pt_idx, (local_d, elev_m) in enumerate(ordered_pts):
                                if idx > 0 and pt_idx == 0 and raw_profile:
                                    continue
                                raw_profile.append(
                                    {
                                        "dist_m": round(cum_dist + local_d, 1),
                                        "elev_m": round(elev_m, 2),
                                        "slope_deg": seg_slope,
                                        "grade": seg_eff_grade,
                                    }
                                )
                    cum_dist += seg_len

                    elev_loss_total += float(seg.elev_loss_m or 0.0)
                    max_seg_gain = max(max_seg_gain, float(seg.elev_gain_m or 0.0))
                    max_seg_loss = max(max_seg_loss, float(seg.elev_loss_m or 0.0))
                    if seg.max_slope_deg is not None and (
                        max_slope_val is None or float(seg.max_slope_deg) > max_slope_val
                    ):
                        max_slope_val = float(seg.max_slope_deg)
                    if seg.sustained_slope_deg is not None and (
                        max_sustained_slope is None
                        or float(seg.sustained_slope_deg) > max_sustained_slope
                    ):
                        max_sustained_slope = float(seg.sustained_slope_deg)
                    if _SURFACE_RANK.get(seg.surface_class, 0) > _SURFACE_RANK.get(
                        worst_surface, 0
                    ):
                        worst_surface = seg.surface_class
                    if seg.tracktype and _TRACKTYPE_RANK.get(
                        seg.tracktype, 0
                    ) > _TRACKTYPE_RANK.get(worst_tracktype or "", 0):
                        worst_tracktype = seg.tracktype
                    is_4wd_only = is_4wd_only or bool(seg.is_4wd_only)
                    is_4wd_recommended = is_4wd_recommended or bool(seg.is_4wd_recommended)
                    has_gate_barrier = has_gate_barrier or bool(seg.has_gate_barrier)
                    any_unbridged_perennial = any_unbridged_perennial or bool(
                        seg.has_perennial_crossing
                    )
                    if dem_tier_val is None and seg.dem_tier:
                        dem_tier_val = seg.dem_tier
                    if _ACCESS_STRICTNESS.get(seg.access_status, 0) > _ACCESS_STRICTNESS.get(
                        access_status, 0
                    ):
                        access_status = seg.access_status
                    if seg.is_steep_highlight:
                        steep_count += 1
                    if seg.is_above_snowline:
                        snowline_count += 1
                    if (
                        seg.capad_park_id is not None
                        and int(seg.capad_park_id) not in seen_park_ids
                    ):
                        pid = int(seg.capad_park_id)
                        seen_park_ids.add(pid)
                        park_ids.append(pid)
                    if len(merged_refs) < 16:
                        for ref in seg.source_refs or []:
                            if isinstance(ref, dict):
                                rkey = (
                                    str(ref.get("source", "")),
                                    str(ref.get("source_id", "")),
                                )
                                if rkey not in seen_ref_keys:
                                    seen_ref_keys.add(rkey)
                                    merged_refs.append(dict(ref))
                                    if len(merged_refs) >= 16:
                                        break
                    if seg_eff_grade is not None and seg.baseline_score is not None:
                        grade_dist[seg_eff_grade] += 1
                        rank = _GRADE_RANK.get(seg_eff_grade, 0)
                        if rank > worst_rank:
                            worst_rank = rank
                        cand_metrics = (
                            float(seg.baseline_score),
                            float(seg.max_slope_deg or 0.0),
                            int(seg.waterway_crossing_count or 0),
                        )
                        if (
                            rep_sort_key is None
                            or cand_metrics > rep_sort_key[:3]
                            or (
                                cand_metrics == rep_sort_key[:3]
                                and seg.segment_id < rep_sort_key[3]
                            )
                        ):
                            rep_sort_key = (*cand_metrics, seg.segment_id)
                            rep_score = float(seg.baseline_score)
                            rep_seg_id = seg.segment_id

                # Waterway crossings
                has_major_perennial = False
                seen_crossings: dict[tuple[str, bool], dict[str, Any]] = {}
                for (
                    _tid,
                    ww_fid,
                    ww_name,
                    ww_type,
                    perenn,
                    hierarchy,
                    seg_id,
                    seg_bridged,
                ) in ww_by_track.get(tid, []):
                    dist_m = seg_mid_dist.get(seg_id, 0.0)
                    is_perennial = str(perenn).strip().lower() == "perennial"
                    if (
                        is_perennial
                        and not seg_bridged
                        and str(hierarchy or "").strip().lower() == "major"
                    ):
                        has_major_perennial = True
                    key = (ww_fid, bool(seg_bridged))
                    if key not in seen_crossings or dist_m < float(seen_crossings[key]["dist_m"]):
                        seen_crossings[key] = {
                            "name": ww_name or "Unnamed watercourse",
                            "waterway_type": ww_type,
                            "perennial": is_perennial,
                            "bridged": bool(seg_bridged),
                            "dist_m": dist_m,
                        }
                waterway_crossings = sorted(
                    seen_crossings.values(), key=lambda c: (float(c["dist_m"]), str(c["name"]))
                )

                baseline_breakdown: dict[str, Any] | None = None
                track_grade_result = None
                if srows:
                    unbridged_items = [c for c in waterway_crossings if not c["bridged"]]
                    unbridged_names = list(
                        dict.fromkeys(str(c["name"]) for c in unbridged_items if c.get("name"))
                    )
                    unbridged_count = max(
                        int(trk.waterway_crossing_count or 0), len(unbridged_items)
                    )
                    all_bridged = all(bool(s.is_bridged) for s in srows) and unbridged_count == 0
                    track_grade_result = compute_baseline_grade(
                        SegmentGradeInput.model_validate(
                            {
                                "surface_class": worst_surface,
                                "tracktype": worst_tracktype,
                                "trail_class": trk.trail_class,
                                "max_slope_deg": (
                                    max_slope_val
                                    if max_slope_val is not None
                                    else trk.max_slope_deg
                                ),
                                "sustained_slope_deg": max_sustained_slope,
                                "elev_gain_m": max_seg_gain,
                                "elev_loss_m": max_seg_loss,
                                "unbridged_crossing_count": unbridged_count,
                                "has_perennial_crossing": any_unbridged_perennial
                                or any(bool(c["perennial"]) for c in unbridged_items),
                                "has_major_perennial_crossing": has_major_perennial,
                                "waterway_names": unbridged_names,
                                "is_bridged": all_bridged,
                                "is_ford": any(bool(c["perennial"]) for c in unbridged_items),
                                "is_4wd_only": is_4wd_only,
                                "is_4wd_recommended": is_4wd_recommended,
                                "access_status": access_status,
                                "has_gate_barrier": has_gate_barrier,
                                "dem_tier": dem_tier_val,
                            }
                        )
                    )
                    baseline_breakdown = dict(track_grade_result.baseline_breakdown)
                    baseline_breakdown["track_summary"] = {
                        "hardest_segment_id": rep_seg_id,
                        "representative_segment_id": rep_seg_id,
                        "hardest_segment_score": (
                            round(rep_score, 1) if rep_score is not None else None
                        ),
                        "segment_count": len(srows),
                        "steep_segment_count": steep_count,
                        "snowline_segment_count": snowline_count,
                        "grade_distribution": dict(grade_dist),
                    }

                if track_grade_result is not None:
                    eff_grade = _RANK_GRADE.get(
                        max(worst_rank, _GRADE_RANK.get(track_grade_result.grade, 0))
                    )
                    peak_score = round(float(track_grade_result.score), 1)
                else:
                    eff_grade = _RANK_GRADE.get(worst_rank) or (
                        trk.effective_grade if include_community else None
                    )
                    peak_score = round(rep_score, 1) if rep_score is not None else None

                computed_grade_by_track[tid] = eff_grade
                c_status, closure_ids = closures_by_track.get(tid, ("open", []))
                live_summary = live_map.get(tid) or {}
                bbox_val = (
                    [
                        round(float(trk.xmin), 6),
                        round(float(trk.ymin), 6),
                        round(float(trk.xmax), 6),
                        round(float(trk.ymax), 6),
                    ]
                    if trk.xmin is not None
                    and trk.ymin is not None
                    and trk.xmax is not None
                    and trk.ymax is not None
                    else None
                )

                shard_tracks[tid] = {
                    "track_id": tid,
                    "name": trk.name,
                    "alt_names": list(trk.alt_names or []),
                    "state": trk.state,
                    "primary_source": trk.primary_source,
                    "has_osm_source": bool(trk.has_osm_source),
                    "trail_class": trk.trail_class,
                    "is_vehicular": bool(trk.is_vehicular),
                    "total_length_m": round(float(trk.total_length_m or 0.0), 1),
                    "elev_min_m": trk.elev_min_m,
                    "elev_max_m": trk.elev_max_m,
                    "elev_gain_m": round(float(trk.elev_gain_m or 0.0), 1),
                    "elev_loss_m": round(elev_loss_total, 1),
                    "max_slope_deg": trk.max_slope_deg,
                    "sustained_slope_deg": (
                        round(max_sustained_slope, 2) if max_sustained_slope is not None else None
                    ),
                    "waterway_crossing_count": int(trk.waterway_crossing_count or 0),
                    "is_4wd_only": is_4wd_only,
                    "has_gate_barrier": has_gate_barrier,
                    "access_status": access_status,
                    "primary_park_id": int(trk.primary_park_id)
                    if trk.primary_park_id is not None
                    else None,
                    "primary_park_name": trk.primary_park_name,
                    "park_ids": park_ids,
                    "effective_grade": eff_grade,
                    "score": peak_score,
                    "live_grade": live_summary.get("live_grade") if include_community else None,
                    "live_score": live_summary.get("live_score") if include_community else None,
                    "confidence_level": (
                        str(live_summary.get("confidence_level") or "none")
                        if include_community
                        else "none"
                    ),
                    "sample_count": (
                        int(live_summary.get("sample_count") or 0) if include_community else 0
                    ),
                    "last_driven_at": (
                        live_summary.get("last_driven_at") if include_community else None
                    ),
                    "avg_speed_kmh": (
                        live_summary.get("avg_speed_kmh") if include_community else None
                    ),
                    "est_duration_min": (
                        live_summary.get("est_duration_min") if include_community else None
                    ),
                    "vehicle_class_split": (
                        live_summary.get("vehicle_class_split") or {} if include_community else {}
                    ),
                    "live_breakdown": (
                        live_summary.get("live_breakdown") if include_community else None
                    ),
                    "rainfall_context": (
                        live_summary.get("rainfall_context")
                        if include_community
                        else static_rainfall_ctx
                    ),
                    "obstacle_hotspots": (
                        live_summary.get("obstacle_hotspots") or [] if include_community else []
                    ),
                    "bbox": bbox_val,
                    "source_refs": merged_refs,
                    "baseline_breakdown": baseline_breakdown,
                    "elevation_profile": _downsample_elevation_profile(raw_profile),
                    "waterway_crossings": waterway_crossings,
                    "coords": _downsample_coords(stitched_coords),
                    "segment_count": len(srows),
                    "is_active": bool(trk.is_active),
                    "closure_status": c_status,
                    "closure_ids": closure_ids,
                }
                counts["tracks"] += 1
                counts["segments"] += len(srows)

        fname = shard_filename(shard_idx)
        shard_path = shards_dir / fname
        shard_payload = {
            "schema_version": "trailforge_pack_shard_v2",
            "region_id": region.region_id,
            "shard_index": shard_idx,
            "tracks": shard_tracks,
        }
        with gzip.open(shard_path, "wt", encoding="utf-8", compresslevel=6) as gz_fh:
            json.dump(shard_payload, gz_fh, separators=(",", ":"))
        shard_rel_paths.append(f"{SHARDS_DIRNAME}/{fname}")

    # 2. Stream compact snapshot.json (lightweight track search index + normalized region layers)
    with snapshot_path.open("w", encoding="utf-8") as fh:
        fh.write("{")
        header_pairs = [
            ("schema_version", "trailforge_pack_snapshot_v2"),
            ("region_id", region.region_id),
            ("name", region.name),
            ("description", region.description),
            ("version", version),
            ("generated_at", generated_at),
            ("shard_count", shard_count),
            ("shard_hash", SHARD_HASH_ALGO),
            ("bbox", list(region.bbox)),
            ("utm_zones", list(region.utm_zones)),
            ("attributions", list(region.attributions)),
        ]
        for idx, (key, val) in enumerate(header_pairs):
            if idx > 0:
                fh.write(",")
            fh.write(f"{json.dumps(key)}:{json.dumps(val, separators=(',', ':'))}")

        fh.write(',"tracks":[')
        written_idx = 0
        for start in range(0, len(resolved_track_ids), 500):
            chunk_ids = resolved_track_ids[start : start + 500]
            rows = session.execute(
                select(
                    Track.track_id,
                    Track.name,
                    Track.alt_names,
                    Track.state,
                    Track.total_length_m,
                    Track.effective_grade,
                    CAPADPark.name.label("primary_park_name"),
                    func.ST_XMin(Track.bbox_geom).label("xmin"),
                    func.ST_YMin(Track.bbox_geom).label("ymin"),
                    func.ST_XMax(Track.bbox_geom).label("xmax"),
                    func.ST_YMax(Track.bbox_geom).label("ymax"),
                )
                .outerjoin(CAPADPark, Track.primary_park_id == CAPADPark.id)
                .where(Track.track_id.in_(chunk_ids))
            ).all()
            row_by_id = {r.track_id: r for r in rows}
            for tid in chunk_ids:
                row = row_by_id.get(tid)
                if row is None:
                    continue
                if written_idx > 0:
                    fh.write(",")
                bbox_val = (
                    [
                        round(float(row.xmin), 5),
                        round(float(row.ymin), 5),
                        round(float(row.xmax), 5),
                        round(float(row.ymax), 5),
                    ]
                    if row.xmin is not None
                    and row.ymin is not None
                    and row.xmax is not None
                    and row.ymax is not None
                    else None
                )
                index_entry = {
                    "track_id": row.track_id,
                    "name": row.name,
                    "alt_names": list(row.alt_names or []),
                    "state": row.state,
                    "total_length_m": round(float(row.total_length_m or 0.0), 1),
                    "effective_grade": computed_grade_by_track.get(row.track_id)
                    or (row.effective_grade if include_community else None),
                    "primary_park_name": row.primary_park_name,
                    "bbox": bbox_val,
                }
                fh.write(json.dumps(index_entry, separators=(",", ":")))
                written_idx += 1
        fh.write("]")

        _stream_region_auxiliary_sections(
            session,
            fh,
            env_geom=env_geom,
            track_id_set=track_id_set,
            counts=counts,
            include_community=include_community,
        )

    return counts, shard_rel_paths, shard_count


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
    *,
    compresslevel: int = 6,
) -> int:
    """Create a compressed tar.gz archive streaming files disk-to-disk without RAM spikes."""
    with tarfile.open(bundle_path, mode="w:gz", compresslevel=compresslevel) as tar:
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
    telemetry_revision: uuid.UUID | None = None,
    telemetry_policy: str = LIVE_FORMULA_VERSION,
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
            community_telemetry_policy=telemetry_policy,
            community_telemetry_revision=telemetry_revision,
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
                "community_telemetry_policy": telemetry_policy,
                "community_telemetry_revision": telemetry_revision,
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
    static_only: bool = False,
    sharded: bool | None = None,
    include_bundle: bool = True,
) -> PackBuildResult:
    """Build a regional offline pack streaming disk-to-disk and record it in ``offline_packs``."""
    region = get_pack_region(region_id)
    packs_root = get_packs_dir(output_dir).resolve()
    packs_root.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    resolved_version = version or now.strftime("%Y.%m.%d")
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    static_only = static_only or bool(get_settings().packs_public_base_url.strip())
    policy = STATIC_PACK_TELEMETRY_POLICY if static_only else LIVE_FORMULA_VERSION
    telemetry_revision = None if static_only else get_pack_telemetry_revision(
        session, region.region_id
    )

    staging_dir = packs_root / f".{region.region_id}.staging-{uuid.uuid4().hex[:8]}"
    staging_dir.mkdir(parents=True, exist_ok=True)

    snapshot_rel = f"{region.region_id}/{SNAPSHOT_FILENAME}"
    vector_rel = f"{region.region_id}/{VECTOR_TILE_FILENAME}"
    terrain_rel: str | None = None

    try:
        # 1. Stream PostGIS snapshot.json (inline_v1 for small regions, sharded_v2 for large ones)
        staged_snapshot = staging_dir / SNAPSHOT_FILENAME
        env_geom = func.ST_MakeEnvelope(*region.bbox, 4326)
        region_track_ids = _list_region_track_ids(session, env_geom)
        use_sharded = (
            sharded if sharded is not None else (len(region_track_ids) > PACK_SHARD_THRESHOLD)
        )
        shard_rel_paths: list[str] = []
        shard_count = 0
        if use_sharded:
            counts, shard_rel_paths, shard_count = _write_region_sharded_snapshot_streaming(
                session,
                region,
                staging_dir,
                staged_snapshot,
                version=resolved_version,
                generated_at=generated_at,
                include_community=not static_only,
                track_ids=region_track_ids,
            )
            snapshot_format: Literal["inline_v1", "sharded_v2"] = SNAPSHOT_FORMAT_SHARDED
        else:
            counts = _write_region_snapshot_streaming(
                session,
                region,
                staged_snapshot,
                version=resolved_version,
                generated_at=generated_at,
                include_community=not static_only,
                track_ids=region_track_ids,
            )
            snapshot_format = SNAPSHOT_FORMAT_INLINE

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
                and _is_current_static_vector(prebuilt_vec)
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

        # 3. Compute streaming SHA-256 & byte sizes for all data artifacts (incl. shards)
        files_map: dict[str, PackFileEntry] = {}
        artifact_names = [SNAPSHOT_FILENAME, *shard_rel_paths]
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
            snapshot_format=snapshot_format,
            shard_count=shard_count,
            shard_hash=SHARD_HASH_ALGO if use_sharded else None,
            shards_base_url=f"/api/packs/{rid}/{SHARDS_DIRNAME}" if use_sharded else None,
            bbox=list(region.bbox),
            bounds=[round(float(v), 6) for v in tile_bounds],
            utm_zones=list(region.utm_zones),
            minzoom=region.default_minzoom,
            maxzoom=region.default_maxzoom,
            vector_available=vector_available,
            terrain_available=terrain_available,
            manifest_url=f"/api/packs/{rid}/{MANIFEST_FILENAME}",
            snapshot_url=f"/api/packs/{rid}/{SNAPSHOT_FILENAME}",
            bundle_url=f"/api/packs/{rid}/{BUNDLE_FILENAME}" if include_bundle else None,
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

        manifest.community_telemetry_policy = policy
        manifest.community_telemetry_revision = telemetry_revision
        staged_manifest = staging_dir / MANIFEST_FILENAME
        staged_manifest.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")

        # 5. Optionally stream downloadable bundle.tar.gz containing manifest + all pack artifacts
        bundle_files = [MANIFEST_FILENAME, *artifact_names]
        if include_bundle:
            staged_bundle = staging_dir / BUNDLE_FILENAME
            bundle_compress = 1 if use_sharded else 6
            bundle_size = _create_streaming_bundle_tar_gz(
                staging_dir, rid, bundle_files, staged_bundle, compresslevel=bundle_compress
            )
            # Record final bundle size inside manifest.json and refresh bundle so manifest matches
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
            # Rebuild bundle with final manifest so extracted manifest == served manifest
            bundle_size = _create_streaming_bundle_tar_gz(
                staging_dir, rid, bundle_files, staged_bundle, compresslevel=bundle_compress
            )
        else:
            manifest_size = int(staged_manifest.stat().st_size)
            manifest = manifest.model_copy(
                update={"total_size_bytes": total_size_bytes + manifest_size}
            )
            staged_manifest.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
            manifest_size = int(staged_manifest.stat().st_size)
            manifest = manifest.model_copy(
                update={"total_size_bytes": total_size_bytes + manifest_size}
            )
            staged_manifest.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")

        manifest_sha256 = _sha256_file(staged_manifest)

        # 6. Atomically promote staging directory to <packs_root>/<region_id>
        if not static_only and get_pack_telemetry_revision(session, rid) != telemetry_revision:
            raise RuntimeError("Community contributions changed during the pack build. Retry.")
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
            telemetry_revision=telemetry_revision,
            telemetry_policy=policy,
        )

        return PackBuildResult(
            region_id=rid,
            pack_dir=target_dir,
            manifest_path=target_dir / MANIFEST_FILENAME,
            snapshot_path=target_dir / SNAPSHOT_FILENAME,
            bundle_path=target_dir / BUNDLE_FILENAME if include_bundle else None,
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
