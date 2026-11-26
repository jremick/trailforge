"""PostGIS query, spatial snapping, and serialization helpers for community condition reports."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from trailforge.db.models import (
    ConditionReport,
    ConditionReportVote,
    Track,
    TrackSegment,
    User,
)
from trailforge.reports.decay import (
    REPORT_TYPE_LABELS,
    VALID_REPORT_TYPES,
    build_osm_links,
    evaluate_report_decay,
    extract_osm_way_id,
    is_gate_warning_type,
)

DEFAULT_SNAP_MAX_DISTANCE_M: Final[float] = 75.0
_WGS84_PREFILTER_DEG: Final[float] = 0.0025  # ~250 m conservative prefilter for GIST index


@dataclass(frozen=True, slots=True)
class SnappedSegmentMatch:
    """Nearest active canonical track segment matched to a report coordinate."""

    segment_id: str
    track_id: str | None
    distance_m: float
    osm_way_id: int | None


def _iso_or_none(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    aware = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")


def pack_report_description(title: str | None, notes: str) -> str:
    """Pack optional single-line title and required notes into ``condition_reports.description``."""
    clean_notes = notes.strip()
    clean_title = " ".join(title.split()) if title else ""
    if clean_title:
        return f"# {clean_title}\n{clean_notes}"
    return clean_notes


def unpack_report_description(description: str | None, report_type: str) -> tuple[str, str]:
    """Extract ``(title, notes)`` losslessly from ``condition_reports.description``."""
    raw = (description or "").strip()
    if raw.startswith("# ") and "\n" in raw:
        first_line, rest = raw.split("\n", 1)
        parsed_title = first_line[2:].strip()
        parsed_notes = rest.strip()
        if parsed_title and parsed_notes:
            return parsed_title, parsed_notes
    default_title = REPORT_TYPE_LABELS.get(report_type, report_type.replace("_", " ").title())  # type: ignore[arg-type]
    return default_title, raw


def snap_point_to_nearest_segment(
    session: Session,
    lon: float,
    lat: float,
    *,
    max_distance_m: float = DEFAULT_SNAP_MAX_DISTANCE_M,
    track_id: str | None = None,
) -> SnappedSegmentMatch | None:
    """Spatially snap ``(lon, lat)`` to the nearest active canonical ``TrackSegment`` in EPSG:3577.

    Returns ``None`` when no active segment lies within ``max_distance_m`` (default 75 m).
    """
    pt_4326 = func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326)
    pt_3577 = func.ST_Transform(pt_4326, 3577)
    seg_3577 = func.ST_Transform(TrackSegment.geom, 3577)
    dist_expr = func.ST_Distance(seg_3577, pt_3577).label("distance_m")

    prefilter_deg = max(_WGS84_PREFILTER_DEG, (max_distance_m / 111_000.0) * 2.0)

    stmt = (
        select(TrackSegment, dist_expr)
        .where(
            TrackSegment.is_active.is_(True),
            func.ST_DWithin(TrackSegment.geom, pt_4326, prefilter_deg),
            func.ST_DWithin(seg_3577, pt_3577, max_distance_m),
        )
        .order_by(dist_expr.asc(), TrackSegment.segment_id.asc())
        .limit(1)
    )
    if track_id is not None:
        stmt = stmt.where(TrackSegment.track_id == track_id)

    row = session.execute(stmt).first()
    if row is None:
        return None

    seg, distance_m = row
    return SnappedSegmentMatch(
        segment_id=seg.segment_id,
        track_id=seg.track_id,
        distance_m=round(float(distance_m), 2),
        osm_way_id=extract_osm_way_id(seg.source_refs),
    )


def resolve_osm_way_id_for_track(
    session: Session,
    *,
    segment_id: str | None = None,
    track_id: str | None = None,
) -> int | None:
    """Look up an OSM way ID from segment or track canonical ``source_refs`` if present."""
    if segment_id:
        seg_refs = session.execute(
            select(TrackSegment.source_refs).where(TrackSegment.segment_id == segment_id).limit(1)
        ).scalar_one_or_none()
        way_id = extract_osm_way_id(seg_refs)
        if way_id is not None:
            return way_id

    if track_id:
        rows = session.execute(
            select(TrackSegment.source_refs)
            .where(
                TrackSegment.track_id == track_id,
                TrackSegment.is_active.is_(True),
                TrackSegment.has_osm_source.is_(True),
            )
            .order_by(TrackSegment.segment_seq.asc().nullslast(), TrackSegment.segment_id.asc())
            .limit(5)
        ).scalars().all()
        for refs in rows:
            way_id = extract_osm_way_id(refs)
            if way_id is not None:
                return way_id

    return None


def _load_vote_metadata(
    session: Session,
    report_ids: Sequence[uuid.UUID],
    *,
    current_user_id: uuid.UUID | None = None,
) -> tuple[dict[uuid.UUID, datetime | None], dict[uuid.UUID, str]]:
    """Return ``(last_confirmed_at_by_report, current_user_vote_by_report)`` in bulk."""
    if not report_ids:
        return {}, {}

    confirm_rows = session.execute(
        select(
            ConditionReportVote.report_id,
            func.max(ConditionReportVote.created_at).label("last_confirmed_at"),
        )
        .where(
            ConditionReportVote.report_id.in_(report_ids),
            ConditionReportVote.vote_type == "confirm",
        )
        .group_by(ConditionReportVote.report_id)
    ).all()
    last_confirmed_map: dict[uuid.UUID, datetime | None] = {
        rid: ts for rid, ts in confirm_rows
    }

    user_vote_map: dict[uuid.UUID, str] = {}
    if current_user_id is not None:
        user_rows = session.execute(
            select(ConditionReportVote.report_id, ConditionReportVote.vote_type).where(
                ConditionReportVote.report_id.in_(report_ids),
                ConditionReportVote.user_id == current_user_id,
            )
        ).all()
        user_vote_map = {rid: vtype for rid, vtype in user_rows}

    return last_confirmed_map, user_vote_map


def _effective_reference_created_at(report: ConditionReport) -> datetime:
    """Return earliest of ``created_at`` and ``observed_at`` so backdated reports decay."""
    created = report.created_at
    observed = report.observed_at
    if observed is None:
        return created
    if created is None:
        return observed
    c_utc = created.astimezone(UTC) if created.tzinfo else created.replace(tzinfo=UTC)
    o_utc = observed.astimezone(UTC) if observed.tzinfo else observed.replace(tzinfo=UTC)
    return min(c_utc, o_utc)


def serialize_condition_report(
    report: ConditionReport,
    *,
    lon: float,
    lat: float,
    author_display_name: str,
    track_name: str | None = None,
    last_confirmed_at: datetime | None = None,
    user_vote: str | None = None,
    osm_way_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Serialize a ``ConditionReport`` row with live decay evaluation and zero author PII."""
    base_ts = _effective_reference_created_at(report)
    decay = evaluate_report_decay(
        report.report_type,
        created_at=base_ts,
        last_confirmed_at=last_confirmed_at,
        confirm_count=int(report.confirm_count or 0),
        resolved_count=int(report.resolve_count or 0),
        status=report.status,
        now=now,
    )

    lon_f = round(float(lon), 6)
    lat_f = round(float(lat), 6)
    links = build_osm_links(lat_f, lon_f, osm_way_id=osm_way_id)
    osm_edit_url = report.osm_edit_url or links["osm_edit_url"]
    title, notes = unpack_report_description(report.description, report.report_type)
    label = REPORT_TYPE_LABELS.get(report.report_type, report.report_type)  # type: ignore[arg-type]

    return {
        "id": str(report.id),
        "report_type": report.report_type,
        "report_type_label": label,
        "is_gate_warning": is_gate_warning_type(report.report_type),
        "severity": report.severity,
        "status": report.status,
        "effective_status": decay.effective_status,
        "title": title,
        "notes": notes,
        "description": notes,
        "photo_url": report.photo_url,
        "vehicle_class": report.vehicle_capability_class,
        "author_display_name": author_display_name,
        "track_id": report.track_id,
        "track_name": track_name,
        "segment_id": report.segment_id,
        "lon": lon_f,
        "lat": lat_f,
        "confirm_count": int(report.confirm_count or 0),
        "resolved_count": int(report.resolve_count or 0),
        "freshness_score": decay.freshness_score,
        "is_stale": decay.is_stale,
        "half_life_days": decay.half_life_days,
        "osm_note_url": links["osm_note_url"],
        "osm_edit_url": osm_edit_url,
        "observed_at": _iso_or_none(report.observed_at),
        "created_at": _iso_or_none(report.created_at),
        "updated_at": _iso_or_none(report.updated_at),
        "last_confirmed_at": _iso_or_none(last_confirmed_at),
        "expires_at": _iso_or_none(report.expires_at),
        "user_vote": user_vote,
    }


def list_reports(
    session: Session,
    *,
    track_id: str | None = None,
    report_type: str | None = None,
    include_resolved: bool = False,
    limit: int = 100,
    current_user_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Query community condition reports with dynamic decay filtering and zero email leakage."""
    if report_type is not None and report_type not in VALID_REPORT_TYPES:
        raise ValueError(
            f"Invalid report_type {report_type!r}; expected one of {', '.join(VALID_REPORT_TYPES)}"
        )

    stmt = (
        select(
            ConditionReport,
            func.ST_X(ConditionReport.geom).label("lon"),
            func.ST_Y(ConditionReport.geom).label("lat"),
            User.display_name.label("author_display_name"),
            Track.name.label("track_name"),
        )
        .join(User, ConditionReport.user_id == User.id)
        .outerjoin(Track, ConditionReport.track_id == Track.track_id)
        .where(ConditionReport.status != "hidden")
        .order_by(ConditionReport.observed_at.desc(), ConditionReport.created_at.desc())
    )

    if track_id is not None:
        seg_ids_sq = select(TrackSegment.segment_id).where(TrackSegment.track_id == track_id)
        stmt = stmt.where(
            or_(
                ConditionReport.track_id == track_id,
                ConditionReport.segment_id.in_(seg_ids_sq),
            )
        )

    if report_type is not None:
        stmt = stmt.where(ConditionReport.report_type == report_type)

    if not include_resolved:
        stmt = stmt.where(ConditionReport.status != "resolved")

    # Fetch extra headroom when filtering dynamically resolved rows in Python
    fetch_limit = min(max(limit * 2, limit), 1000)
    rows = session.execute(stmt.limit(fetch_limit)).all()
    if not rows:
        return []

    report_ids = [r[0].id for r in rows]
    last_confirmed_map, user_vote_map = _load_vote_metadata(
        session, report_ids, current_user_id=current_user_id
    )

    results: list[dict[str, Any]] = []
    for report, lon, lat, author_name, track_name in rows:
        payload = serialize_condition_report(
            report,
            lon=float(lon),
            lat=float(lat),
            author_display_name=author_name,
            track_name=track_name,
            last_confirmed_at=last_confirmed_map.get(report.id),
            user_vote=user_vote_map.get(report.id),
            now=now,
        )
        if not include_resolved and payload["effective_status"] == "resolved":
            continue
        results.append(payload)
        if len(results) >= limit:
            break

    return results


def get_report_by_id(
    session: Session,
    report_id: uuid.UUID,
    *,
    current_user_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Fetch and serialize a single non-hidden condition report by UUID."""
    row = session.execute(
        select(
            ConditionReport,
            func.ST_X(ConditionReport.geom).label("lon"),
            func.ST_Y(ConditionReport.geom).label("lat"),
            User.display_name.label("author_display_name"),
            Track.name.label("track_name"),
        )
        .join(User, ConditionReport.user_id == User.id)
        .outerjoin(Track, ConditionReport.track_id == Track.track_id)
        .where(ConditionReport.id == report_id, ConditionReport.status != "hidden")
        .limit(1)
    ).first()
    if row is None:
        return None

    report, lon, lat, author_name, track_name = row
    last_confirmed_map, user_vote_map = _load_vote_metadata(
        session, [report.id], current_user_id=current_user_id
    )
    return serialize_condition_report(
        report,
        lon=float(lon),
        lat=float(lat),
        author_display_name=author_name,
        track_name=track_name,
        last_confirmed_at=last_confirmed_map.get(report.id),
        user_vote=user_vote_map.get(report.id),
        now=now,
    )


def get_reports_for_track(
    session: Session,
    track_id: str,
    *,
    include_resolved: bool = False,
    current_user_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return track condition reports + distinct active gate/private-property warnings.

    Consumed by ``src/trailforge/api/tracks.py`` for ``GET /api/tracks/{track_id}`` so
    locked-gate and private-property warnings surface distinctly alongside official closures.
    """
    reports = list_reports(
        session,
        track_id=track_id,
        include_resolved=include_resolved,
        limit=200,
        current_user_id=current_user_id,
        now=now,
    )
    community_gate_warnings = [
        item
        for item in reports
        if item["is_gate_warning"] and item["effective_status"] not in ("resolved", "hidden")
    ]
    return {
        "condition_reports": reports,
        "community_gate_warnings": community_gate_warnings,
    }


def get_reports_geojson(
    session: Session,
    *,
    track_id: str | None = None,
    report_type: str | None = None,
    include_resolved: bool = False,
    limit: int = 500,
    current_user_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return RFC 7946 FeatureCollection of community hazard & gate warning markers."""
    reports = list_reports(
        session,
        track_id=track_id,
        report_type=report_type,
        include_resolved=include_resolved,
        limit=limit,
        current_user_id=current_user_id,
        now=now,
    )
    features: list[dict[str, Any]] = [
        {
            "type": "Feature",
            "id": item["id"],
            "geometry": {
                "type": "Point",
                "coordinates": [item["lon"], item["lat"]],
            },
            "properties": dict(item),
        }
        for item in reports
    ]
    return {"type": "FeatureCollection", "features": features}
