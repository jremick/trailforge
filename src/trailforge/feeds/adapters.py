"""Concrete VIC & NSW closure, incident, park-alert, and OSM access feed adapters (Slice 04).

All HTTP adapters accept an injectable ``httpx.Client`` and optional offline fixture payloads so
CI tests run 100% offline while live production runs talk directly to official VIC/NSW feeds.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx
from shapely.geometry import GeometryCollection, mapping, shape
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from trailforge.db.models import POI, Track, TrackSegment
from trailforge.feeds.base import (
    FEED_REGISTRY,
    ClosureStatusLiteral,
    DbAlertTypeLiteral,
    DbSeverityLiteral,
    FeedSourceLiteral,
    NormalizedClosure,
    StateLiteral,
    parse_iso_datetime,
)
from trailforge.ingestion.connectors.base import USER_AGENT, prop_ci

logger = logging.getLogger(__name__)

_RELEVANT_VIC_CATEGORIES: Final[tuple[str, ...]] = (
    "fire",
    "burn",
    "flood",
    "tree",
    "storm",
    "landslide",
    "road",
    "warning",
    "hazard",
    "closure",
)


def _normalize_geojson_geometry(raw_geom: Any) -> dict[str, Any] | None:
    """Validate/normalize GeoJSON geometry; unwrap GeometryCollection to its best polygon/line."""
    if not isinstance(raw_geom, Mapping) or "type" not in raw_geom:
        return None
    try:
        shp = shape(raw_geom)
    except Exception:
        return None
    if shp.is_empty:
        return None
    if isinstance(shp, GeometryCollection):
        # Prefer Polygon/MultiPolygon > LineString > Point inside NSW RFS GeometryCollections
        for wanted in ("MultiPolygon", "Polygon", "MultiLineString", "LineString", "Point"):
            for part in shp.geoms:
                if part.geom_type == wanted and not part.is_empty:
                    return dict(mapping(part))
        return None
    return dict(mapping(shp))


def _normalize_state(raw: Any, default: StateLiteral) -> StateLiteral:
    val = str(raw or "").strip().upper()
    if val in ("VIC", "NSW", "ACT"):
        return val  # type: ignore[return-value]
    return default


def _normalize_status(raw: Any, default: ClosureStatusLiteral = "active") -> ClosureStatusLiteral:
    val = str(raw or "").strip().lower()
    if val in ("active", "scheduled", "cleared", "stale"):
        return val  # type: ignore[return-value]
    if val in ("safe", "under control", "closed_incident", "resolved"):
        return "cleared"
    return default


def _normalize_alert_type(raw: Any, default: DbAlertTypeLiteral) -> DbAlertTypeLiteral:
    val = str(raw or "").strip().lower()
    valid = (
        "seasonal_closure",
        "road_closure",
        "park_closure",
        "bushfire",
        "flood",
        "hazard_warning",
        "private_gate",
    )
    if val in valid:
        return val  # type: ignore[return-value]
    if "seasonal" in val:
        return "seasonal_closure"
    if "park" in val and "clos" in val:
        return "park_closure"
    if "road" in val and "clos" in val:
        return "road_closure"
    if "fire" in val or "burn" in val:
        return "bushfire"
    if "flood" in val or "water" in val:
        return "flood"
    if "gate" in val or "private" in val:
        return "private_gate"
    return default


def _normalize_db_severity(raw: Any, default: DbSeverityLiteral) -> DbSeverityLiteral:
    val = str(raw or "").strip().lower()
    if val in ("info", "advisory", "watch_and_act", "closed", "emergency"):
        return val  # type: ignore[return-value]
    if "emergency" in val or "evacuate" in val or val == "critical":
        return "emergency"
    if "watch" in val and "act" in val:
        return "watch_and_act"
    if "clos" in val:
        return "closed"
    if "advic" in val or "advis" in val or "warn" in val or "caution" in val:
        return "advisory"
    return default


class _BaseHttpFeedAdapter:
    """Shared HTTP fetch + offline fixture support for official VIC/NSW feed adapters."""

    feed_source: FeedSourceLiteral

    def __init__(
        self,
        *,
        source_url: str | None = None,
        timeout_s: float = 20.0,
        client: httpx.Client | None = None,
        fixture_items: Sequence[Mapping[str, Any]] | None = None,
        fixture_payload: Mapping[str, Any] | None = None,
    ) -> None:
        meta = FEED_REGISTRY[self.feed_source]
        self.display_name = meta.display_name
        self.agency = meta.agency
        self.state = meta.state
        self.source_url = source_url or meta.resolve_url()
        self.sync_interval_s = meta.sync_interval_s
        self.stale_after_s = meta.stale_after_s
        self.timeout_s = timeout_s
        self._client = client
        self._fixture_items = list(fixture_items) if fixture_items is not None else None
        self._fixture_payload = dict(fixture_payload) if fixture_payload is not None else None

    def _fetch_json(self) -> Any:
        owns_client = self._client is None
        http = (
            self._client
            if self._client is not None
            else httpx.Client(
                timeout=httpx.Timeout(self.timeout_s, connect=min(8.0, self.timeout_s)),
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
        )
        try:
            resp = http.get(self.source_url, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            return resp.json()
        finally:
            if owns_client:
                http.close()


class VicSeasonalClosuresAdapter(_BaseHttpFeedAdapter):
    """VIC MapShareVic / DEECA seasonal road closure adapter."""

    feed_source: FeedSourceLiteral = "vic_mapshare_seasonal"

    def _parse_item(self, item: Mapping[str, Any]) -> NormalizedClosure | None:
        # Handle both GeoJSON Feature (WFS/ArcGIS) and normalized fixture dict
        if item.get("type") == "Feature":
            props = dict(item.get("properties") or {})
            geom = _normalize_geojson_geometry(item.get("geometry"))
            ext_id = str(
                prop_ci(props, "external_id", "pfi", "ufi", "objectid", "id")
                or item.get("id")
                or ""
            ).strip()
        else:
            props = dict(item)
            geom = _normalize_geojson_geometry(item.get("geometry"))
            ext_id = str(prop_ci(props, "external_id", "pfi", "objectid", "id") or "").strip()

        title = str(
            prop_ci(props, "title", "road_name", "ezi_road_name_label", "name") or ""
        ).strip()
        if not title:
            return None
        if not ext_id:
            ext_id = f"vic-seasonal-{title.lower().replace(' ', '-')}"

        return NormalizedClosure(
            source_feed=self.feed_source,
            external_id=ext_id,
            alert_type=_normalize_alert_type(
                prop_ci(props, "alert_type", "closure_type"), default="seasonal_closure"
            ),
            db_severity=_normalize_db_severity(prop_ci(props, "severity"), default="closed"),
            status=_normalize_status(prop_ci(props, "status"), default="active"),
            title=title,
            description=str(prop_ci(props, "description", "comments", "reason") or "").strip()
            or None,
            state=_normalize_state(prop_ci(props, "state"), default="VIC"),
            source_url=str(prop_ci(props, "source_url", "url") or self.source_url).strip(),
            geometry=geom,
            effective_from=parse_iso_datetime(prop_ci(props, "effective_from", "start_date")),
            effective_to=parse_iso_datetime(prop_ci(props, "effective_to", "end_date")),
            source_updated_at=parse_iso_datetime(
                prop_ci(props, "source_updated_at", "updated_at", "last_updated")
            )
            or datetime.now(UTC),
            raw_payload=props,
        )

    def fetch(self, *, session: Session | None = None) -> list[NormalizedClosure]:
        if self._fixture_items is not None:
            raw_items: list[Mapping[str, Any]] = self._fixture_items
        else:
            payload = (
                self._fixture_payload if self._fixture_payload is not None else self._fetch_json()
            )
            if isinstance(payload, Mapping) and isinstance(payload.get("features"), list):
                raw_items = payload["features"]
            elif isinstance(payload, list):
                raw_items = payload
            else:
                raise ValueError("Unexpected VIC seasonal closures payload format")

        out: list[NormalizedClosure] = []
        for item in raw_items:
            if isinstance(item, Mapping):
                parsed = self._parse_item(item)
                if parsed is not None:
                    out.append(parsed)
        return out


class VicEmergencyAdapter(_BaseHttpFeedAdapter):
    """VicEmergency public GeoJSON incidents & warnings adapter."""

    feed_source: FeedSourceLiteral = "vic_emergency"

    def _parse_item(self, item: Mapping[str, Any]) -> NormalizedClosure | None:
        if item.get("type") == "Feature":
            props = dict(item.get("properties") or {})
            geom = _normalize_geojson_geometry(item.get("geometry"))
            ext_id = str(
                prop_ci(props, "external_id", "sourceId", "id") or item.get("id") or ""
            ).strip()
        else:
            props = dict(item)
            geom = _normalize_geojson_geometry(item.get("geometry"))
            ext_id = str(prop_ci(props, "external_id", "sourceId", "id") or "").strip()

        cat_text = " ".join(
            str(prop_ci(props, k) or "")
            for k in ("alert_type", "category1", "category2", "feedType", "title")
        ).lower()
        if not any(tok in cat_text for tok in _RELEVANT_VIC_CATEGORIES):
            return None

        title = str(
            prop_ci(props, "title", "sourceTitle", "webHeadline", "name", "location") or ""
        ).strip()
        if not title:
            return None
        if not ext_id:
            ext_id = f"vicem-{title.lower().replace(' ', '-')[:48]}"

        alert_type = _normalize_alert_type(
            prop_ci(props, "alert_type", "category1", "category2"), default="hazard_warning"
        )
        raw_sev = prop_ci(props, "severity", "cap_severity", "feedType", "status")
        db_sev = _normalize_db_severity(raw_sev, default="advisory")

        return NormalizedClosure(
            source_feed=self.feed_source,
            external_id=ext_id,
            alert_type=alert_type,
            db_severity=db_sev,
            status=_normalize_status(prop_ci(props, "status"), default="active"),
            title=title,
            description=str(prop_ci(props, "description", "webBody", "text") or "").strip() or None,
            state=_normalize_state(prop_ci(props, "state"), default="VIC"),
            source_url=str(
                prop_ci(props, "source_url", "url") or "https://emergency.vic.gov.au/respond/"
            ).strip(),
            geometry=geom,
            effective_from=parse_iso_datetime(prop_ci(props, "effective_from", "created")),
            effective_to=parse_iso_datetime(prop_ci(props, "effective_to")),
            source_updated_at=parse_iso_datetime(
                prop_ci(props, "source_updated_at", "updated", "created")
            )
            or datetime.now(UTC),
            raw_payload=props,
        )

    def fetch(self, *, session: Session | None = None) -> list[NormalizedClosure]:
        if self._fixture_items is not None:
            raw_items: list[Mapping[str, Any]] = self._fixture_items
        else:
            payload = (
                self._fixture_payload if self._fixture_payload is not None else self._fetch_json()
            )
            if isinstance(payload, Mapping) and isinstance(payload.get("features"), list):
                raw_items = payload["features"]
            elif isinstance(payload, list):
                raw_items = payload
            else:
                raise ValueError("Unexpected VicEmergency GeoJSON payload format")

        out: list[NormalizedClosure] = []
        for item in raw_items:
            if isinstance(item, Mapping):
                parsed = self._parse_item(item)
                if parsed is not None:
                    out.append(parsed)
        return out


class NSWRFSAdapter(_BaseHttpFeedAdapter):
    """NSW Rural Fire Service (RFS) majorIncidents.json GeoJSON adapter."""

    feed_source: FeedSourceLiteral = "nsw_rfs"

    def _parse_item(self, item: Mapping[str, Any]) -> NormalizedClosure | None:
        if item.get("type") == "Feature":
            props = dict(item.get("properties") or {})
            geom = _normalize_geojson_geometry(item.get("geometry"))
            ext_id = str(
                prop_ci(props, "external_id", "guid", "id") or item.get("id") or ""
            ).strip()
        else:
            props = dict(item)
            geom = _normalize_geojson_geometry(item.get("geometry"))
            ext_id = str(prop_ci(props, "external_id", "guid", "id") or "").strip()

        title = str(prop_ci(props, "title", "name") or "").strip()
        if not title:
            return None
        if not ext_id:
            ext_id = f"nswrfs-{title.lower().replace(' ', '-')[:48]}"

        category = str(prop_ci(props, "severity", "category") or "Advice").strip()
        db_sev = _normalize_db_severity(category, default="advisory")

        return NormalizedClosure(
            source_feed=self.feed_source,
            external_id=ext_id,
            alert_type=_normalize_alert_type(prop_ci(props, "alert_type"), default="bushfire"),
            db_severity=db_sev,
            status=_normalize_status(prop_ci(props, "status"), default="active"),
            title=title,
            description=str(prop_ci(props, "description") or "").strip() or None,
            state=_normalize_state(prop_ci(props, "state"), default="NSW"),
            source_url=str(
                prop_ci(props, "source_url", "link", "url")
                or "https://www.rfs.nsw.gov.au/fire-information/fires-near-me"
            ).strip(),
            geometry=geom,
            effective_from=parse_iso_datetime(prop_ci(props, "effective_from", "pubDate")),
            effective_to=parse_iso_datetime(prop_ci(props, "effective_to")),
            source_updated_at=parse_iso_datetime(
                prop_ci(props, "source_updated_at", "updated", "pubDate")
            )
            or datetime.now(UTC),
            raw_payload=props,
        )

    def fetch(self, *, session: Session | None = None) -> list[NormalizedClosure]:
        if self._fixture_items is not None:
            raw_items: list[Mapping[str, Any]] = self._fixture_items
        else:
            payload = (
                self._fixture_payload if self._fixture_payload is not None else self._fetch_json()
            )
            if isinstance(payload, Mapping) and isinstance(payload.get("features"), list):
                raw_items = payload["features"]
            elif isinstance(payload, list):
                raw_items = payload
            else:
                raise ValueError("Unexpected NSW RFS majorIncidents payload format")

        out: list[NormalizedClosure] = []
        for item in raw_items:
            if isinstance(item, Mapping):
                parsed = self._parse_item(item)
                if parsed is not None:
                    out.append(parsed)
        return out


class NSWNPWSAdapter(_BaseHttpFeedAdapter):
    """NSW National Parks & Wildlife Service (NPWS) park alerts adapter (CAPAD joined)."""

    feed_source: FeedSourceLiteral = "nsw_npws"

    def _parse_item(self, item: Mapping[str, Any]) -> NormalizedClosure | None:
        if item.get("type") == "Feature":
            props = dict(item.get("properties") or {})
            geom = _normalize_geojson_geometry(item.get("geometry"))
            ext_id = str(prop_ci(props, "external_id", "id") or item.get("id") or "").strip()
        else:
            props = dict(item)
            geom = _normalize_geojson_geometry(item.get("geometry"))
            ext_id = str(prop_ci(props, "external_id", "id") or "").strip()

        title = str(prop_ci(props, "title", "headline", "name") or "").strip()
        if not title:
            return None
        if not ext_id:
            ext_id = f"npws-{title.lower().replace(' ', '-')[:48]}"

        park_name = (
            str(prop_ci(props, "park_name", "parkName", "affectedPark") or "").strip() or None
        )
        raw_type = prop_ci(props, "alert_type", "alertType", "type")
        if raw_type is None and "closed" in title.lower():
            raw_type = "park_closure"
        alert_type = _normalize_alert_type(raw_type, default="hazard_warning")
        default_sev: DbSeverityLiteral = "closed" if alert_type == "park_closure" else "advisory"
        db_sev = _normalize_db_severity(prop_ci(props, "severity"), default=default_sev)

        return NormalizedClosure(
            source_feed=self.feed_source,
            external_id=ext_id,
            alert_type=alert_type,
            db_severity=db_sev,
            status=_normalize_status(prop_ci(props, "status"), default="active"),
            title=title,
            description=str(prop_ci(props, "description", "details", "body") or "").strip() or None,
            state=_normalize_state(prop_ci(props, "state"), default="NSW"),
            source_url=str(prop_ci(props, "source_url", "url") or self.source_url).strip(),
            park_name=park_name,
            geometry=geom,
            effective_from=parse_iso_datetime(prop_ci(props, "effective_from", "startDate")),
            effective_to=parse_iso_datetime(prop_ci(props, "effective_to", "endDate")),
            source_updated_at=parse_iso_datetime(
                prop_ci(props, "source_updated_at", "updated", "lastUpdated")
            )
            or datetime.now(UTC),
            raw_payload=props,
        )

    def fetch(self, *, session: Session | None = None) -> list[NormalizedClosure]:
        if self._fixture_items is not None:
            raw_items: list[Mapping[str, Any]] = self._fixture_items
        else:
            payload = (
                self._fixture_payload if self._fixture_payload is not None else self._fetch_json()
            )
            if isinstance(payload, Mapping) and isinstance(payload.get("alerts"), list):
                raw_items = payload["alerts"]
            elif isinstance(payload, Mapping) and isinstance(payload.get("features"), list):
                raw_items = payload["features"]
            elif isinstance(payload, list):
                raw_items = payload
            else:
                raise ValueError("Unexpected NSW NPWS alerts payload format")

        out: list[NormalizedClosure] = []
        for item in raw_items:
            if isinstance(item, Mapping):
                parsed = self._parse_item(item)
                if parsed is not None:
                    out.append(parsed)
        return out


class OSMAccessAdapter:
    """Propagates OSM access=private/restricted/closed and barrier=gate flags from PostGIS."""

    feed_source: FeedSourceLiteral = "osm_access"

    def __init__(self) -> None:
        meta = FEED_REGISTRY[self.feed_source]
        self.display_name = meta.display_name
        self.agency = meta.agency
        self.state = meta.state
        self.source_url = meta.resolve_url()
        self.sync_interval_s = meta.sync_interval_s
        self.stale_after_s = meta.stale_after_s

    def fetch(self, *, session: Session | None = None) -> list[NormalizedClosure]:
        if session is None:
            raise ValueError("OSMAccessAdapter requires an active SQLAlchemy Session")

        now = datetime.now(UTC)
        out: list[NormalizedClosure] = []

        # 1. Track-level private / restricted / closed / gated segments
        seg_rows = session.execute(
            select(
                TrackSegment,
                Track.name.label("track_name"),
                func.ST_AsGeoJSON(TrackSegment.geom).label("geom_json"),
            )
            .outerjoin(Track, TrackSegment.track_id == Track.track_id)
            .where(
                TrackSegment.is_active.is_(True),
                or_(
                    TrackSegment.access_status.in_(("private", "restricted", "closed")),
                    TrackSegment.has_gate_barrier.is_(True),
                ),
            )
            .order_by(TrackSegment.track_id.asc(), TrackSegment.segment_seq.asc().nullslast())
        ).all()

        grouped: dict[str, dict[str, Any]] = {}
        for seg, track_name, geom_json in seg_rows:
            group_key = seg.track_id or seg.segment_id
            entry = grouped.setdefault(
                group_key,
                {
                    "track_id": seg.track_id,
                    "track_name": track_name or seg.name or "Unnamed Track",
                    "state": seg.state,
                    "access_statuses": set(),
                    "has_gate": False,
                    "segment_ids": [],
                    "first_geom": json.loads(geom_json),
                },
            )
            entry["access_statuses"].add(seg.access_status)
            if seg.has_gate_barrier:
                entry["has_gate"] = True
            entry["segment_ids"].append(seg.segment_id)

        for group_key, info in grouped.items():
            statuses: set[str] = info["access_statuses"]
            has_gate: bool = info["has_gate"]
            track_name = str(info["track_name"])
            seg_ids: list[str] = info["segment_ids"]
            is_private = "private" in statuses or "closed" in statuses
            is_restricted = "restricted" in statuses

            if is_private or is_restricted:
                access_label = "Private Property Access" if is_private else "Restricted Access"
                out.append(
                    NormalizedClosure(
                        source_feed=self.feed_source,
                        external_id=f"osm-access-{group_key}",
                        alert_type="private_gate",
                        db_severity="closed" if is_private else "advisory",
                        status="active",
                        title=f"{track_name} — {access_label}",
                        description=(
                            f"Tagged access={sorted(statuses)[0]} in OpenStreetMap"
                            + (" with barrier=gate present." if has_gate else ".")
                        ),
                        state=_normalize_state(info["state"], default="VIC"),
                        source_url=self.source_url,
                        geometry=info["first_geom"],
                        source_updated_at=now,
                        raw_payload={
                            "closure_type": "private_access",
                            "access_kind": "private_access",
                            "access_statuses": sorted(statuses),
                            "has_gate_barrier": has_gate,
                            "track_id": info["track_id"],
                        },
                        direct_segment_ids=seg_ids,
                    )
                )
            elif has_gate:
                out.append(
                    NormalizedClosure(
                        source_feed=self.feed_source,
                        external_id=f"osm-gate-track-{group_key}",
                        alert_type="private_gate",
                        db_severity="advisory",
                        status="active",
                        title=f"{track_name} — Gate Barrier",
                        description="Tagged barrier=gate along this track in OpenStreetMap.",
                        state=_normalize_state(info["state"], default="VIC"),
                        source_url=self.source_url,
                        geometry=info["first_geom"],
                        source_updated_at=now,
                        raw_payload={
                            "closure_type": "locked_gate",
                            "has_gate_barrier": True,
                            "track_id": info["track_id"],
                        },
                        direct_segment_ids=seg_ids,
                    )
                )

        # 2. Gate POI markers (e.g., Station Creek Locked Gate)
        gate_rows = session.execute(
            select(POI, func.ST_AsGeoJSON(POI.geom).label("geom_json"))
            .where(POI.poi_type == "gate")
            .order_by(POI.id.asc())
        ).all()
        for poi, geom_json in gate_rows:
            note = poi.access_note or "Gate / access barrier recorded in OpenStreetMap."
            is_locked = "locked" in poi.name.lower() or "locked" in note.lower()
            direct_ids = [poi.nearest_segment_id] if poi.nearest_segment_id else []
            out.append(
                NormalizedClosure(
                    source_feed=self.feed_source,
                    external_id=f"osm-gate-poi-{poi.poi_key}",
                    alert_type="private_gate",
                    db_severity="closed" if is_locked else "advisory",
                    status="active",
                    title=poi.name,
                    description=note,
                    state=_normalize_state(poi.state, default="VIC"),
                    source_url=self.source_url,
                    geometry=json.loads(geom_json),
                    source_updated_at=now,
                    raw_payload={
                        "closure_type": "locked_gate",
                        "has_gate_barrier": True,
                        "poi_id": poi.id,
                        "poi_key": poi.poi_key,
                    },
                    direct_segment_ids=direct_ids,
                )
            )

        return out


def build_default_adapters(
    *,
    client: httpx.Client | None = None,
    fixtures_path: Path | str | None = None,
    only_feeds: Sequence[str] | None = None,
) -> list[Any]:
    """Instantiate the 5 Slice 04 feed adapters (offline fixture-backed or live HTTP)."""
    fixture_feeds: dict[str, list[Mapping[str, Any]]] | None = None
    if fixtures_path is not None:
        raw = json.loads(Path(fixtures_path).read_text(encoding="utf-8"))
        fixture_feeds = dict(raw.get("feeds") or {})

    selected: set[str] | None = None
    if only_feeds:
        from trailforge.feeds.base import resolve_feed_source

        selected = {resolve_feed_source(name) for name in only_feeds}

    adapters: list[Any] = []
    if selected is None or "vic_mapshare_seasonal" in selected:
        adapters.append(
            VicSeasonalClosuresAdapter(
                client=client,
                fixture_items=fixture_feeds.get("vic_mapshare_seasonal", [])
                if fixture_feeds is not None
                else None,
            )
        )
    if selected is None or "vic_emergency" in selected:
        adapters.append(
            VicEmergencyAdapter(
                client=client,
                fixture_items=fixture_feeds.get("vic_emergency", [])
                if fixture_feeds is not None
                else None,
            )
        )
    if selected is None or "nsw_rfs" in selected:
        adapters.append(
            NSWRFSAdapter(
                client=client,
                fixture_items=fixture_feeds.get("nsw_rfs", [])
                if fixture_feeds is not None
                else None,
            )
        )
    if selected is None or "nsw_npws" in selected:
        adapters.append(
            NSWNPWSAdapter(
                client=client,
                fixture_items=fixture_feeds.get("nsw_npws", [])
                if fixture_feeds is not None
                else None,
            )
        )
    if selected is None or "osm_access" in selected:
        adapters.append(OSMAccessAdapter())

    return adapters
