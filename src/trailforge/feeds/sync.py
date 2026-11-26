"""Outage-resilient Pan-Australia closure feed sync engine and spatial segment propagation."""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from geoalchemy2.shape import from_shape
from pydantic import BaseModel, Field
from pyproj import Transformer
from shapely import transform as shapely_transform_coords
from shapely.geometry import LineString, MultiLineString, shape
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from trailforge.db.models import (
    CAPADPark,
    ClosureAlert,
    FeedSyncStatus,
    SegmentClosureLink,
    TrackSegment,
)
from trailforge.feeds.adapters import build_default_adapters
from trailforge.feeds.base import (
    FeedAdapter,
    FeedSourceLiteral,
    NormalizedClosure,
)

logger = logging.getLogger(__name__)

# Spatial propagation buffer thresholds (meters in EPSG:3577 Albers)
LINE_BUFFER_M: float = 80.0
LINE_MAX_HEADING_DIFF_DEG: float = 35.0
LINE_MIN_OVERLAP_FRAC: float = 0.50
POLYGON_BUFFER_M: float = 100.0
POINT_INCIDENT_BUFFER_M: float = 500.0
GATE_POINT_BUFFER_M: float = 60.0

_TO_ALBERS = Transformer.from_crs("EPSG:4326", "EPSG:3577", always_xy=True)

_PARK_SUFFIX_RE = re.compile(
    r"\s+(?:National Park|Conservation Park|Nature Reserve|State Forest|State Reserve|"
    r"Regional Park|Wilderness Area|Recreation Park|Conservation Reserve|Resources Reserve)$",
    re.IGNORECASE,
)


def _to_albers(geom: LineString | MultiLineString) -> LineString | MultiLineString:
    def _arr(coords: np.ndarray) -> np.ndarray:
        if coords.size == 0:
            return coords
        xs, ys = _TO_ALBERS.transform(coords[:, 0], coords[:, 1])
        return np.column_stack((xs, ys))

    return shapely_transform_coords(geom, _arr)  # type: ignore[return-value]


def _local_line_bearing_deg(line_m: LineString, dist_m: float) -> float:
    total = float(line_m.length)
    if total <= 1e-6:
        return 0.0
    d0 = max(0.0, dist_m - 10.0)
    d1 = min(total, dist_m + 10.0)
    if d1 - d0 < 1e-3:
        d0, d1 = 0.0, total
    p0 = line_m.interpolate(d0)
    p1 = line_m.interpolate(d1)
    dx = p1.x - p0.x
    dy = p1.y - p0.y
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def _acute_bearing_diff_deg(a: float, b: float) -> float:
    diff = abs(a - b) % 180.0
    return min(diff, 180.0 - diff)


def _segment_aligned_with_line_closure(
    seg_geom_wgs84: dict[str, object],
    seg_bearing_deg: float,
    alert_line_m: LineString | MultiLineString,
    alert_buf_m: object,
) -> bool:
    """Prevent LineString closures from bleeding onto perpendicular side tracks at junctions."""
    seg_shp = shape(seg_geom_wgs84)
    if seg_shp.is_empty:
        return False
    seg_m = _to_albers(seg_shp)  # type: ignore[arg-type]
    seg_len = float(seg_m.length)
    if seg_len <= 0.0:
        return False

    overlap_len = float(seg_m.intersection(alert_buf_m).length)
    if (overlap_len / seg_len) < LINE_MIN_OVERLAP_FRAC:
        return False

    mid = seg_m.interpolate(0.5, normalized=True)
    if isinstance(alert_line_m, MultiLineString):
        nearest_part = min(alert_line_m.geoms, key=lambda g: g.distance(mid))
    else:
        nearest_part = alert_line_m
    proj_d = float(nearest_part.project(mid))
    closure_bearing = _local_line_bearing_deg(nearest_part, proj_d)
    return _acute_bearing_diff_deg(seg_bearing_deg, closure_bearing) <= LINE_MAX_HEADING_DIFF_DEG


class FeedSyncResult(BaseModel):
    """Outcome of syncing a single feed adapter."""

    feed_source: FeedSourceLiteral
    ok: bool
    status: str
    records_upserted: int = 0
    records_cleared: int = 0
    segments_linked: int = 0
    retained_existing: int = 0
    error: str | None = None


class SyncRunSummary(BaseModel):
    """Aggregated summary across all synced feeds."""

    synced_at: str
    overall_ok: bool
    results: list[FeedSyncResult] = Field(default_factory=list)


def _get_or_create_sync_status(session: Session, adapter: FeedAdapter) -> FeedSyncStatus:
    row = session.get(FeedSyncStatus, adapter.feed_source)
    if row is None:
        row = FeedSyncStatus(
            feed_name=adapter.feed_source,
            source_url=adapter.source_url,
            sync_interval_s=adapter.sync_interval_s,
            stale_after_s=adapter.stale_after_s,
            last_status="never_run",
            consecutive_failures=0,
            records_synced=0,
        )
        session.add(row)
        session.flush()
    else:
        row.source_url = adapter.source_url
        row.sync_interval_s = adapter.sync_interval_s
        row.stale_after_s = adapter.stale_after_s
    return row


def _resolve_capad_park_id(session: Session, item: NormalizedClosure) -> int | None:
    """Resolve matching CAPAD protected-area ID by exact/fuzzy name or spatial intersection."""
    if item.park_name:
        cleaned = item.park_name.strip()
        # 1. Exact state-scoped name match
        exact_state = session.execute(
            select(CAPADPark.id)
            .where(func.lower(CAPADPark.name) == cleaned.lower(), CAPADPark.state == item.state)
            .limit(1)
        ).scalar_one_or_none()
        if exact_state is not None:
            return int(exact_state)

        # 2. Exact cross-state name match (e.g. border parks)
        exact = session.execute(
            select(CAPADPark.id).where(func.lower(CAPADPark.name) == cleaned.lower()).limit(1)
        ).scalar_one_or_none()
        if exact is not None:
            return int(exact)

        # 3. Substring match within the alert's state
        fuzzy = session.execute(
            select(CAPADPark.id)
            .where(CAPADPark.name.ilike(f"%{cleaned}%"), CAPADPark.state == item.state)
            .limit(1)
        ).scalar_one_or_none()
        if fuzzy is not None:
            return int(fuzzy)

        # 4. Suffix-stripped match (e.g. "Daintree National Park" -> CAPAD "Daintree")
        stem = _PARK_SUFFIX_RE.sub("", cleaned).strip()
        if stem and stem.lower() != cleaned.lower():
            stem_hit = session.execute(
                select(CAPADPark.id)
                .where(CAPADPark.name.ilike(f"%{stem}%"), CAPADPark.state == item.state)
                .limit(1)
            ).scalar_one_or_none()
            if stem_hit is not None:
                return int(stem_hit)

    if item.geometry is not None and item.alert_type == "park_closure":
        geom_wkb = from_shape(shape(item.geometry), srid=4326)
        hit = session.execute(
            select(CAPADPark.id)
            .where(func.ST_Intersects(CAPADPark.geom, geom_wkb))
            .limit(1)
        ).scalar_one_or_none()
        if hit is not None:
            return int(hit)

    return None


def _propagate_segment_links(
    session: Session,
    alert: ClosureAlert,
    item: NormalizedClosure,
    *,
    now: datetime,
) -> int:
    """Compute and replace ``segment_closure_links`` for ``alert`` using PostGIS."""
    session.execute(
        delete(SegmentClosureLink).where(SegmentClosureLink.closure_id == alert.id)
    )

    if alert.status == "cleared":
        return 0

    links: dict[str, tuple[str, float]] = {}  # segment_id -> (link_method, distance_m)

    # 1. Direct segment IDs (e.g., OSM access / gate tags already mapped to segments)
    if item.direct_segment_ids:
        existing_ids = session.execute(
            select(TrackSegment.segment_id).where(
                TrackSegment.segment_id.in_(item.direct_segment_ids),
                TrackSegment.is_active.is_(True),
            )
        ).scalars().all()
        for seg_id in existing_ids:
            links[seg_id] = ("osm_access_flag", 0.0)

    # 2. CAPAD Park tenure join (park alerts without narrower geometry, or park-wide Point closures)
    use_capad_join = alert.capad_park_id is not None and (
        item.geometry is None
        or (item.alert_type == "park_closure" and item.geometry.get("type") == "Point")
    )
    if use_capad_join and alert.capad_park_id is not None:
        park_seg_ids = session.execute(
            select(TrackSegment.segment_id)
            .join(CAPADPark, CAPADPark.id == alert.capad_park_id)
            .where(
                TrackSegment.is_active.is_(True),
                (TrackSegment.capad_park_id == alert.capad_park_id)
                | func.ST_Intersects(TrackSegment.geom, CAPADPark.geom),
            )
        ).scalars().all()
        for seg_id in park_seg_ids:
            links.setdefault(seg_id, ("capad_park_join", 0.0))

    # 3. Spatial intersection & EPSG:3577 metric buffer match when geometry is provided
    if item.geometry is not None:
        geom_type = str(item.geometry.get("type") or "")
        geom_json_str = json.dumps(item.geometry)
        alert_geom_4326 = func.ST_SetSRID(func.ST_GeomFromGeoJSON(geom_json_str), 4326)
        alert_geom_3577 = func.ST_Transform(alert_geom_4326, 3577)
        seg_geom_3577 = func.ST_Transform(TrackSegment.geom, 3577)

        if geom_type in ("Polygon", "MultiPolygon"):
            buf_m = POLYGON_BUFFER_M
        elif geom_type in ("LineString", "MultiLineString"):
            buf_m = LINE_BUFFER_M
        elif item.source_feed == "osm_access":
            buf_m = GATE_POINT_BUFFER_M
        else:
            buf_m = POINT_INCIDENT_BUFFER_M

        is_line_closure = geom_type in ("LineString", "MultiLineString")
        alert_line_m: LineString | MultiLineString | None = None
        alert_buf_m: object | None = None
        if is_line_closure:
            alert_line_m = _to_albers(shape(item.geometry))  # type: ignore[arg-type]
            alert_buf_m = alert_line_m.buffer(LINE_BUFFER_M)

        dist_expr = func.ST_Distance(seg_geom_3577, alert_geom_3577)
        spatial_rows = session.execute(
            select(
                TrackSegment.segment_id,
                TrackSegment.bearing_deg,
                func.ST_AsGeoJSON(TrackSegment.geom).label("seg_geom_json"),
                func.ST_Intersects(TrackSegment.geom, alert_geom_4326).label("intersects"),
                dist_expr.label("dist_m"),
            ).where(
                TrackSegment.is_active.is_(True),
                func.ST_DWithin(seg_geom_3577, alert_geom_3577, buf_m),
            )
        ).all()

        for seg_id, seg_bearing, seg_geom_json, intersects, dist_m in spatial_rows:
            if seg_id in links:
                continue
            if (
                is_line_closure
                and alert_line_m is not None
                and alert_buf_m is not None
                and not _segment_aligned_with_line_closure(
                    json.loads(seg_geom_json), float(seg_bearing), alert_line_m, alert_buf_m
                )
            ):
                continue
            if item.source_feed == "osm_access":
                method = "osm_access_flag"
            else:
                method = "spatial_intersect" if bool(intersects) else "spatial_buffer"
            links[seg_id] = (method, round(float(dist_m or 0.0), 2))

    for seg_id, (method, dist_m) in links.items():
        session.add(
            SegmentClosureLink(
                segment_id=seg_id,
                closure_id=alert.id,
                link_method=method,
                distance_m=dist_m,
                linked_at=now,
            )
        )

    return len(links)


def sync_feed(
    session: Session,
    adapter: FeedAdapter,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> FeedSyncResult:
    """Sync one closure feed with outage resilience (retains last-known state on failure)."""
    ts = now or datetime.now(UTC)
    sync_row = _get_or_create_sync_status(session, adapter)
    sync_row.last_attempt_at = ts

    try:
        items = adapter.fetch(session=session)
    except Exception as exc:
        logger.warning(
            "Closure feed %s failed (%s); retaining last-known state", adapter.feed_source, exc
        )
        sync_row.consecutive_failures = int(sync_row.consecutive_failures or 0) + 1
        sync_row.last_error = str(exc)[:500]
        sync_row.last_status = "degraded" if sync_row.last_success_at is not None else "failed"

        retained = int(
            session.execute(
                select(func.count(ClosureAlert.id)).where(
                    ClosureAlert.source_feed == adapter.feed_source,
                    ClosureAlert.status != "cleared",
                )
            ).scalar_one()
        )
        if commit:
            session.commit()
        else:
            session.flush()
        return FeedSyncResult(
            feed_source=adapter.feed_source,
            ok=False,
            status=sync_row.last_status,
            retained_existing=retained,
            error=str(exc),
        )

    existing_rows = session.execute(
        select(ClosureAlert).where(ClosureAlert.source_feed == adapter.feed_source)
    ).scalars().all()
    existing_by_ext = {row.external_id: row for row in existing_rows}

    seen_ext_ids: set[str] = set()
    segments_linked = 0
    upserted = 0

    for item in items:
        seen_ext_ids.add(item.external_id)
        capad_park_id = _resolve_capad_park_id(session, item)
        geom_val = (
            from_shape(shape(item.geometry), srid=4326) if item.geometry is not None else None
        )

        alert = existing_by_ext.get(item.external_id)
        if alert is None:
            alert = ClosureAlert(
                source_feed=item.source_feed,
                external_id=item.external_id,
                alert_type=item.alert_type,
                severity=item.db_severity,
                status=item.status,
                title=item.title,
                description=item.description,
                state=item.state,
                source_url=item.source_url,
                capad_park_id=capad_park_id,
                geom=geom_val,
                effective_from=item.effective_from,
                effective_to=item.effective_to,
                source_updated_at=item.source_updated_at or ts,
                last_seen_at=ts,
                raw_payload=item.raw_payload,
            )
            session.add(alert)
            session.flush()
        else:
            alert.alert_type = item.alert_type
            alert.severity = item.db_severity
            alert.status = item.status
            alert.title = item.title
            alert.description = item.description
            alert.state = item.state
            alert.source_url = item.source_url
            alert.capad_park_id = capad_park_id
            alert.geom = geom_val
            alert.effective_from = item.effective_from
            alert.effective_to = item.effective_to
            alert.source_updated_at = item.source_updated_at or ts
            alert.last_seen_at = ts
            alert.raw_payload = item.raw_payload
            session.flush()

        segments_linked += _propagate_segment_links(session, alert, item, now=ts)
        upserted += 1

    # Clear previously active alerts from this feed that disappeared from upstream
    cleared = 0
    for ext_id, old_alert in existing_by_ext.items():
        if ext_id not in seen_ext_ids and old_alert.status != "cleared":
            old_alert.status = "cleared"
            session.execute(
                delete(SegmentClosureLink).where(SegmentClosureLink.closure_id == old_alert.id)
            )
            cleared += 1

    sync_row.last_success_at = ts
    sync_row.last_status = "ok"
    sync_row.consecutive_failures = 0
    sync_row.records_synced = upserted
    sync_row.last_error = None

    if commit:
        session.commit()
    else:
        session.flush()

    return FeedSyncResult(
        feed_source=adapter.feed_source,
        ok=True,
        status="ok",
        records_upserted=upserted,
        records_cleared=cleared,
        segments_linked=segments_linked,
    )


def sync_configured_feeds(
    session: Session,
    *,
    adapters: Sequence[FeedAdapter] | None = None,
    states: str | Sequence[str] | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> SyncRunSummary:
    """Run sync across all provided (or state-scoped configured) feed adapters."""
    ts = now or datetime.now(UTC)
    active_adapters = (
        list(adapters) if adapters is not None else build_default_adapters(states=states)
    )
    results = [sync_feed(session, adapter, now=ts, commit=commit) for adapter in active_adapters]
    return SyncRunSummary(
        synced_at=ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        overall_ok=all(r.ok for r in results),
        results=results,
    )


def sync_from_fixtures(
    session: Session,
    fixtures_path: Path | str,
    *,
    only_feeds: Sequence[str] | None = None,
    states: str | Sequence[str] | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> SyncRunSummary:
    """Sync configured AU closure feeds + OSM access flags offline from a fixture payload."""
    adapters = build_default_adapters(
        fixtures_path=fixtures_path,
        only_feeds=only_feeds,
        states=states,
    )
    return sync_configured_feeds(session, adapters=adapters, now=now, commit=commit)
