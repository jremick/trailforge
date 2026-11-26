"""Read/query helpers for closures, GeoJSON overlays, feed staleness, and track enrichment."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trailforge.db.models import (
    CAPADPark,
    ClosureAlert,
    FeedSyncStatus,
    SegmentClosureLink,
    Track,
    TrackSegment,
)
from trailforge.feeds.base import (
    FEED_REGISTRY,
    ApiClosureTypeLiteral,
    ApiSeverityLiteral,
    TrackClosureStatusLiteral,
    compute_feed_is_stale,
    map_alert_to_closure_type,
    map_db_to_api_severity,
    resolve_feed_source,
)

_TRACK_STATUS_RANK: dict[TrackClosureStatusLiteral, int] = {
    "open": 0,
    "info": 1,
    "warning": 2,
    "gated": 3,
    "seasonal": 4,
    "closed": 5,
}


def _iso_or_none(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    aware = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")


def _closure_to_track_status(
    closure_type: ApiClosureTypeLiteral, severity: ApiSeverityLiteral
) -> TrackClosureStatusLiteral:
    if closure_type == "closed" or (closure_type == "incident" and severity == "critical"):
        return "closed"
    if closure_type == "seasonal":
        return "seasonal"
    if closure_type in ("locked_gate", "private_access"):
        return "gated"
    if severity == "warning":
        return "warning"
    return "info"


def get_feed_health_summary(
    session: Session, *, now: datetime | None = None
) -> dict[str, Any]:
    """Return feed status and staleness summary for ``GET /api/closures/status``."""
    current = now or datetime.now(UTC)
    rows = {
        row.feed_name: row
        for row in session.execute(select(FeedSyncStatus)).scalars().all()
    }

    counts_by_feed = {
        feed: int(cnt)
        for feed, cnt in session.execute(
            select(ClosureAlert.source_feed, func.count(ClosureAlert.id))
            .where(ClosureAlert.status != "cleared")
            .group_by(ClosureAlert.source_feed)
        ).all()
    }

    feeds_payload: list[dict[str, Any]] = []
    any_degraded = False
    any_stale = False

    for feed_source, meta in FEED_REGISTRY.items():
        row = rows.get(feed_source)
        is_stale = compute_feed_is_stale(row, now=current)
        status_str = row.last_status if row is not None else "never_run"
        if status_str in ("failed", "degraded"):
            any_degraded = True
        if is_stale:
            any_stale = True

        feeds_payload.append(
            {
                "feed_source": feed_source,
                "display_name": meta.display_name,
                "agency": meta.agency,
                "state": meta.state,
                "status": status_str,
                "last_attempt_at": _iso_or_none(row.last_attempt_at if row else None),
                "last_success_at": _iso_or_none(row.last_success_at if row else None),
                "consecutive_failures": int(row.consecutive_failures) if row else 0,
                "record_count": counts_by_feed.get(feed_source, 0),
                "is_stale": is_stale,
                "last_error": row.last_error if row else None,
                "source_url": row.source_url if row else meta.resolve_url(),
            }
        )

    if any_degraded:
        overall = "degraded"
    elif any_stale:
        overall = "stale"
    else:
        overall = "healthy"

    return {"overall_status": overall, "feeds": feeds_payload}


def _stale_map_by_feed(
    session: Session, *, now: datetime | None = None
) -> dict[str, bool]:
    current = now or datetime.now(UTC)
    rows = {
        row.feed_name: row
        for row in session.execute(select(FeedSyncStatus)).scalars().all()
    }
    return {
        feed_source: compute_feed_is_stale(rows.get(feed_source), now=current)
        for feed_source in FEED_REGISTRY
    }


def list_closures(
    session: Session,
    *,
    active_only: bool = True,
    state: str | None = None,
    severity: str | None = None,
    feed_source: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return normalized closure list + feed health metadata for ``GET /api/closures``."""
    current = now or datetime.now(UTC)
    health = get_feed_health_summary(session, now=current)
    stale_by_feed = {f["feed_source"]: bool(f["is_stale"]) for f in health["feeds"]}

    stmt = (
        select(
            ClosureAlert,
            func.ST_AsGeoJSON(ClosureAlert.geom).label("geom_json"),
            CAPADPark.name.label("park_name"),
        )
        .outerjoin(CAPADPark, ClosureAlert.capad_park_id == CAPADPark.id)
        .order_by(ClosureAlert.last_seen_at.desc(), ClosureAlert.id.asc())
    )
    if active_only:
        stmt = stmt.where(ClosureAlert.status != "cleared")
    if state:
        stmt = stmt.where(ClosureAlert.state == state.strip().upper())
    if feed_source:
        resolved = resolve_feed_source(feed_source)
        stmt = stmt.where(ClosureAlert.source_feed == resolved)

    alert_rows = session.execute(stmt).all()
    closure_ids = [alert.id for alert, _, _ in alert_rows]

    seg_links: dict[int, list[str]] = {}
    track_links: dict[int, set[str]] = {}
    if closure_ids:
        link_rows = session.execute(
            select(
                SegmentClosureLink.closure_id,
                SegmentClosureLink.segment_id,
                TrackSegment.track_id,
            )
            .join(TrackSegment, SegmentClosureLink.segment_id == TrackSegment.segment_id)
            .where(SegmentClosureLink.closure_id.in_(closure_ids))
            .order_by(SegmentClosureLink.segment_id.asc())
        ).all()
        for cid, seg_id, tid in link_rows:
            seg_links.setdefault(cid, []).append(seg_id)
            if tid:
                track_links.setdefault(cid, set()).add(tid)

    sev_filter = severity.strip().lower() if severity else None
    items: list[dict[str, Any]] = []

    for alert, geom_json, park_name in alert_rows:
        api_sev = map_db_to_api_severity(alert.severity)
        if sev_filter and sev_filter not in (api_sev, alert.severity.lower()):
            continue

        meta = FEED_REGISTRY.get(alert.source_feed)  # type: ignore[arg-type]
        closure_type = map_alert_to_closure_type(
            alert.alert_type,
            source_feed=alert.source_feed,
            title=alert.title,
            raw_payload=alert.raw_payload,
            capad_park_id=alert.capad_park_id,
        )
        last_updated = alert.source_updated_at or alert.last_seen_at

        items.append(
            {
                "closure_id": alert.id,
                "feed_source": alert.source_feed,
                "agency": meta.agency if meta else alert.source_feed,
                "external_id": alert.external_id,
                "title": alert.title,
                "description": alert.description,
                "closure_type": closure_type,
                "severity": api_sev,
                "status": alert.status,
                "state": alert.state,
                "effective_from": _iso_or_none(alert.effective_from),
                "effective_to": _iso_or_none(alert.effective_to),
                "source_url": alert.source_url,
                "last_updated_at": _iso_or_none(last_updated),
                "is_stale": stale_by_feed.get(alert.source_feed, False)
                or alert.status == "stale",
                "affected_segment_ids": seg_links.get(alert.id, []),
                "affected_track_ids": sorted(track_links.get(alert.id, set())),
                "capad_park_name": park_name,
                "geometry": json.loads(geom_json) if geom_json else None,
            }
        )

    return {"items": items, "feeds": health["feeds"]}


def get_closures_geojson(
    session: Session,
    *,
    active_only: bool = True,
    state: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build RFC 7946 FeatureCollection of affected track segments + incident/gate geometries."""
    current = now or datetime.now(UTC)
    stale_by_feed = _stale_map_by_feed(session, now=current)
    features: list[dict[str, Any]] = []

    # 1. Affected track segments (dashed red/amber overlay lines on the map)
    seg_stmt = (
        select(
            SegmentClosureLink,
            ClosureAlert,
            TrackSegment,
            Track.name.label("track_name"),
            func.ST_AsGeoJSON(TrackSegment.geom).label("seg_geom_json"),
        )
        .join(ClosureAlert, SegmentClosureLink.closure_id == ClosureAlert.id)
        .join(TrackSegment, SegmentClosureLink.segment_id == TrackSegment.segment_id)
        .outerjoin(Track, TrackSegment.track_id == Track.track_id)
        .where(TrackSegment.is_active.is_(True))
        .order_by(ClosureAlert.id.asc(), TrackSegment.segment_seq.asc().nullslast())
    )
    if active_only:
        seg_stmt = seg_stmt.where(ClosureAlert.status != "cleared")
    if state:
        seg_stmt = seg_stmt.where(ClosureAlert.state == state.strip().upper())

    for link, alert, seg, track_name, seg_geom_json in session.execute(seg_stmt).all():
        meta = FEED_REGISTRY.get(alert.source_feed)  # type: ignore[arg-type]
        closure_type = map_alert_to_closure_type(
            alert.alert_type,
            source_feed=alert.source_feed,
            title=alert.title,
            raw_payload=alert.raw_payload,
            capad_park_id=alert.capad_park_id,
        )
        api_sev = map_db_to_api_severity(alert.severity)
        features.append(
            {
                "type": "Feature",
                "id": f"seg-{alert.id}-{seg.segment_id}",
                "geometry": json.loads(seg_geom_json),
                "properties": {
                    "feature_kind": "segment_closure",
                    "closure_id": alert.id,
                    "feed_source": alert.source_feed,
                    "closure_type": closure_type,
                    "severity": api_sev,
                    "status": alert.status,
                    "title": alert.title,
                    "description": alert.description,
                    "agency": meta.agency if meta else alert.source_feed,
                    "source_url": alert.source_url,
                    "effective_from": _iso_or_none(alert.effective_from),
                    "effective_to": _iso_or_none(alert.effective_to),
                    "last_updated_at": _iso_or_none(alert.source_updated_at or alert.last_seen_at),
                    "is_stale": stale_by_feed.get(alert.source_feed, False)
                    or alert.status == "stale",
                    "track_id": seg.track_id,
                    "track_name": track_name or seg.name,
                    "segment_id": seg.segment_id,
                    "link_method": link.link_method,
                },
            }
        )

    # 2. Alert polygons & point markers (incident perimeters, gate markers, etc.)
    alert_stmt = (
        select(ClosureAlert, func.ST_AsGeoJSON(ClosureAlert.geom).label("geom_json"))
        .where(ClosureAlert.geom.is_not(None))
        .order_by(ClosureAlert.id.asc())
    )
    if active_only:
        alert_stmt = alert_stmt.where(ClosureAlert.status != "cleared")
    if state:
        alert_stmt = alert_stmt.where(ClosureAlert.state == state.strip().upper())

    for alert, geom_json in session.execute(alert_stmt).all():
        geom_obj = json.loads(geom_json)
        if geom_obj.get("type") not in ("Point", "Polygon", "MultiPolygon"):
            continue
        meta = FEED_REGISTRY.get(alert.source_feed)  # type: ignore[arg-type]
        closure_type = map_alert_to_closure_type(
            alert.alert_type,
            source_feed=alert.source_feed,
            title=alert.title,
            raw_payload=alert.raw_payload,
            capad_park_id=alert.capad_park_id,
        )
        api_sev = map_db_to_api_severity(alert.severity)
        features.append(
            {
                "type": "Feature",
                "id": f"alert-{alert.id}",
                "geometry": geom_obj,
                "properties": {
                    "feature_kind": "alert_geometry",
                    "closure_id": alert.id,
                    "feed_source": alert.source_feed,
                    "closure_type": closure_type,
                    "severity": api_sev,
                    "status": alert.status,
                    "title": alert.title,
                    "description": alert.description,
                    "agency": meta.agency if meta else alert.source_feed,
                    "source_url": alert.source_url,
                    "effective_from": _iso_or_none(alert.effective_from),
                    "effective_to": _iso_or_none(alert.effective_to),
                    "last_updated_at": _iso_or_none(alert.source_updated_at or alert.last_seen_at),
                    "is_stale": stale_by_feed.get(alert.source_feed, False)
                    or alert.status == "stale",
                    "track_id": (alert.raw_payload or {}).get("track_id"),
                    "segment_id": None,
                },
            }
        )

    return {"type": "FeatureCollection", "features": features}


def get_closures_for_track_ids(
    session: Session,
    track_ids: Sequence[str],
    *,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Return closure status rollup + active closure summaries keyed by ``track_id``.

    Called by ``src/trailforge/api/tracks.py`` for ``GET /api/tracks`` and
    ``GET /api/tracks/{track_id}``.
    """
    unique_ids = list(dict.fromkeys(tid for tid in track_ids if tid))
    result: dict[str, dict[str, Any]] = {
        tid: {"closure_status": "open", "active_closures": []} for tid in unique_ids
    }
    if not unique_ids:
        return result

    stale_by_feed = _stale_map_by_feed(session, now=now)

    rows = session.execute(
        select(
            TrackSegment.track_id,
            ClosureAlert,
            CAPADPark.name.label("park_name"),
        )
        .join(SegmentClosureLink, TrackSegment.segment_id == SegmentClosureLink.segment_id)
        .join(ClosureAlert, SegmentClosureLink.closure_id == ClosureAlert.id)
        .outerjoin(CAPADPark, ClosureAlert.capad_park_id == CAPADPark.id)
        .where(
            TrackSegment.track_id.in_(unique_ids),
            TrackSegment.is_active.is_(True),
            ClosureAlert.status != "cleared",
        )
        .order_by(ClosureAlert.id.asc())
    ).all()

    seen_pairs: set[tuple[str, int]] = set()
    for tid, alert, park_name in rows:
        if not tid or (tid, alert.id) in seen_pairs:
            continue
        seen_pairs.add((tid, alert.id))

        meta = FEED_REGISTRY.get(alert.source_feed)  # type: ignore[arg-type]
        closure_type = map_alert_to_closure_type(
            alert.alert_type,
            source_feed=alert.source_feed,
            title=alert.title,
            raw_payload=alert.raw_payload,
            capad_park_id=alert.capad_park_id,
        )
        api_sev = map_db_to_api_severity(alert.severity)
        candidate_status = _closure_to_track_status(closure_type, api_sev)

        bucket = result[tid]
        if _TRACK_STATUS_RANK[candidate_status] > _TRACK_STATUS_RANK[bucket["closure_status"]]:
            bucket["closure_status"] = candidate_status

        bucket["active_closures"].append(
            {
                "closure_id": alert.id,
                "feed_source": alert.source_feed,
                "title": alert.title,
                "agency": meta.agency if meta else alert.source_feed,
                "closure_type": closure_type,
                "severity": api_sev,
                "status": alert.status,
                "state": alert.state,
                "effective_from": _iso_or_none(alert.effective_from),
                "effective_to": _iso_or_none(alert.effective_to),
                "source_url": alert.source_url,
                "last_updated_at": _iso_or_none(alert.source_updated_at or alert.last_seen_at),
                "is_stale": stale_by_feed.get(alert.source_feed, False)
                or alert.status == "stale",
                "capad_park_name": park_name,
                "description": alert.description,
            }
        )

    return result
