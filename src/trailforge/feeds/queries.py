"""Read/query helpers for Pan-Australia closures, GeoJSON overlays, feed staleness, and tracks."""

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
    ALL_AU_STATES,
    ALL_FEED_REGISTRY,
    ApiClosureTypeLiteral,
    ApiSeverityLiteral,
    FeedSourceLiteral,
    TrackClosureStatusLiteral,
    compute_feed_is_stale,
    get_feeds_for_states,
    map_alert_to_closure_type,
    map_db_to_api_severity,
    parse_state_filter,
    resolve_enabled_states,
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


def _build_feeds_health_rows(
    session: Session,
    feed_keys: Sequence[FeedSourceLiteral],
    *,
    now: datetime,
) -> tuple[str, list[dict[str, Any]]]:
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

    for feed_source in feed_keys:
        meta = ALL_FEED_REGISTRY[feed_source]
        row = rows.get(feed_source)
        is_stale = compute_feed_is_stale(row, now=now)
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
    return overall, feeds_payload


def get_feed_health_summary(
    session: Session,
    *,
    state: str | Sequence[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return feed status, staleness, and available/enabled AU states for ``/api/closures/status``.

    When ``state`` is omitted, scopes to ``TRAILFORGE_ENABLED_STATES`` (default ``VIC,NSW`` +
    ``osm_access`` = 5 core feeds) so default homelab status stays lean and Slice 04 compatible.
    When ``state`` is provided (e.g. ``"QLD"``, ``"VIC,NSW,QLD"``, or ``"ALL"``), returns health
    metadata strictly matching those state(s) across all 11 Pan-Australia feeds.
    """
    current = now or datetime.now(UTC)
    enabled_states = resolve_enabled_states()
    requested_states = parse_state_filter(state)

    if requested_states:
        include_national = set(requested_states) == set(ALL_AU_STATES)
        feed_keys = get_feeds_for_states(requested_states, include_national=include_national)
    else:
        feed_keys = get_feeds_for_states(enabled_states, include_national=True)

    overall, feeds_payload = _build_feeds_health_rows(session, feed_keys, now=current)

    payload: dict[str, Any] = {
        "overall_status": overall,
        "available_states": list(ALL_AU_STATES),
        "enabled_states": list(enabled_states),
        "feeds": feeds_payload,
    }
    if requested_states:
        payload["filtered_states"] = list(requested_states)
    return payload


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
        for feed_source in ALL_FEED_REGISTRY
    }


def list_closures(
    session: Session,
    *,
    active_only: bool = True,
    state: str | Sequence[str] | None = None,
    severity: str | None = None,
    feed_source: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return normalized closure list + feed health metadata for ``GET /api/closures``."""
    current = now or datetime.now(UTC)
    enabled_states = resolve_enabled_states()
    requested_states = parse_state_filter(state)

    # Always compute staleness across all 11 Pan-AU feeds so every alert gets exact is_stale
    stale_by_feed = _stale_map_by_feed(session, now=current)

    # Include feeds for enabled states PLUS any explicitly requested extra state(s)
    feed_states = tuple(dict.fromkeys([*enabled_states, *requested_states]))
    feed_keys = get_feeds_for_states(feed_states, include_national=True)
    _, feeds_payload = _build_feeds_health_rows(session, feed_keys, now=current)

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
    if requested_states and set(requested_states) != set(ALL_AU_STATES):
        stmt = stmt.where(ClosureAlert.state.in_(requested_states))
    if feed_source:
        resolved = resolve_feed_source(feed_source)
        stmt = stmt.where(ClosureAlert.source_feed == resolved)

    alert_rows = session.execute(stmt).all()
    closure_ids = [alert.id for alert, _, _ in alert_rows]

    seg_links: dict[int, list[str]] = {}
    track_links: dict[int, set[str]] = {}
    if closure_ids:
        # Avoid loading tens of thousands of segment IDs for giant statewide/regional polygons
        bounded_ids = [
            cid
            for cid, cnt in session.execute(
                select(
                    SegmentClosureLink.closure_id,
                    func.count(SegmentClosureLink.segment_id),
                )
                .where(SegmentClosureLink.closure_id.in_(closure_ids))
                .group_by(SegmentClosureLink.closure_id)
            ).all()
            if int(cnt) <= 500
        ]
        if bounded_ids:
            link_rows = session.execute(
                select(
                    SegmentClosureLink.closure_id,
                    SegmentClosureLink.segment_id,
                    TrackSegment.track_id,
                )
                .join(TrackSegment, SegmentClosureLink.segment_id == TrackSegment.segment_id)
                .where(SegmentClosureLink.closure_id.in_(bounded_ids))
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

        meta = ALL_FEED_REGISTRY.get(alert.source_feed)  # type: ignore[arg-type]
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

    result: dict[str, Any] = {
        "items": items,
        "feeds": feeds_payload,
        "available_states": list(ALL_AU_STATES),
        "enabled_states": list(enabled_states),
    }
    if requested_states:
        result["filtered_states"] = list(requested_states)
    return result


def get_closures_geojson(
    session: Session,
    *,
    active_only: bool = True,
    state: str | Sequence[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build RFC 7946 FeatureCollection of affected track segments + incident/gate geometries."""
    current = now or datetime.now(UTC)
    requested_states = parse_state_filter(state)
    stale_by_feed = _stale_map_by_feed(session, now=current)
    features: list[dict[str, Any]] = []

    # 1. Affected track segments (dashed red/amber overlay lines on the map).
    # Regional/statewide polygon alerts linking >400 segments already render their polygon
    # boundary in step 2 (and still show per-track in the Track Inspector), so exclude huge
    # polygon fanouts here to keep /api/closures/geojson fast (<20 ms) and compact.
    count_stmt = (
        select(
            SegmentClosureLink.closure_id,
            func.count(SegmentClosureLink.segment_id).label("seg_cnt"),
            ClosureAlert.geom.is_not(None).label("has_geom"),
        )
        .join(ClosureAlert, SegmentClosureLink.closure_id == ClosureAlert.id)
        .group_by(SegmentClosureLink.closure_id, ClosureAlert.geom.is_not(None))
    )
    if active_only:
        count_stmt = count_stmt.where(ClosureAlert.status != "cleared")
    if requested_states and set(requested_states) != set(ALL_AU_STATES):
        count_stmt = count_stmt.where(ClosureAlert.state.in_(requested_states))

    targeted_closure_ids = [
        cid
        for cid, seg_cnt, has_geom in session.execute(count_stmt).all()
        if int(seg_cnt) <= 400 or not has_geom
    ]

    if targeted_closure_ids:
        # Select only lightweight scalar columns (omit JSONB profiles & raw payloads)
        seg_stmt = (
            select(
                SegmentClosureLink.link_method,
                ClosureAlert.id.label("closure_id"),
                ClosureAlert.source_feed,
                ClosureAlert.alert_type,
                ClosureAlert.severity,
                ClosureAlert.status,
                ClosureAlert.state,
                ClosureAlert.title,
                ClosureAlert.description,
                ClosureAlert.source_url,
                ClosureAlert.effective_from,
                ClosureAlert.effective_to,
                ClosureAlert.source_updated_at,
                ClosureAlert.last_seen_at,
                ClosureAlert.capad_park_id,
                TrackSegment.segment_id,
                TrackSegment.track_id,
                TrackSegment.name.label("seg_name"),
                Track.name.label("track_name"),
                func.ST_AsGeoJSON(TrackSegment.geom, 5).label("seg_geom_json"),
            )
            .join(ClosureAlert, SegmentClosureLink.closure_id == ClosureAlert.id)
            .join(TrackSegment, SegmentClosureLink.segment_id == TrackSegment.segment_id)
            .outerjoin(Track, TrackSegment.track_id == Track.track_id)
            .where(
                SegmentClosureLink.closure_id.in_(targeted_closure_ids),
                TrackSegment.is_active.is_(True),
            )
            .limit(2000)
        )

        for row in session.execute(seg_stmt).all():
            meta = ALL_FEED_REGISTRY.get(row.source_feed)  # type: ignore[arg-type]
            closure_type = map_alert_to_closure_type(
                row.alert_type,
                source_feed=row.source_feed,
                title=row.title,
                raw_payload=None,
                capad_park_id=row.capad_park_id,
            )
            api_sev = map_db_to_api_severity(row.severity)
            features.append(
                {
                    "type": "Feature",
                    "id": f"seg-{row.closure_id}-{row.segment_id}",
                    "geometry": json.loads(row.seg_geom_json),
                    "properties": {
                        "feature_kind": "segment_closure",
                        "closure_id": row.closure_id,
                        "feed_source": row.source_feed,
                        "closure_type": closure_type,
                        "severity": api_sev,
                        "status": row.status,
                        "state": row.state,
                        "title": row.title,
                        "description": row.description,
                        "agency": meta.agency if meta else row.source_feed,
                        "source_url": row.source_url,
                        "effective_from": _iso_or_none(row.effective_from),
                        "effective_to": _iso_or_none(row.effective_to),
                        "last_updated_at": _iso_or_none(
                            row.source_updated_at or row.last_seen_at
                        ),
                        "is_stale": stale_by_feed.get(row.source_feed, False)
                        or row.status == "stale",
                        "track_id": row.track_id,
                        "track_name": row.track_name or row.seg_name,
                        "segment_id": row.segment_id,
                        "link_method": row.link_method,
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
    if requested_states and set(requested_states) != set(ALL_AU_STATES):
        alert_stmt = alert_stmt.where(ClosureAlert.state.in_(requested_states))

    for alert, geom_json in session.execute(alert_stmt).all():
        geom_obj = json.loads(geom_json)
        if geom_obj.get("type") not in ("Point", "Polygon", "MultiPolygon"):
            continue
        meta = ALL_FEED_REGISTRY.get(alert.source_feed)  # type: ignore[arg-type]
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
                    "state": alert.state,
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

        meta = ALL_FEED_REGISTRY.get(alert.source_feed)  # type: ignore[arg-type]
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
