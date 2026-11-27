"""Concrete Pan-Australia closure, incident, park-alert, and OSM access feed adapters.

Preserves the 5 v1 VIC/NSW/OSM adapters 100% intact and registers all 6 additional Australian
state & territory adapters (ACT, TAS, QLD, WA, SA, NT) in ``ADAPTER_REGISTRY``.
All HTTP adapters accept an injectable ``httpx.Client`` and optional offline fixture payloads so
CI tests run 100% offline while live production runs talk directly to official AU agency feeds.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Final

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from trailforge.db.models import POI, Track, TrackSegment
from trailforge.feeds.adapters_au import (
    ACTESAAlertsAdapter,
    EmergencyWAAdapter,
    NTFireParksAdapter,
    QLDParksAlertsAdapter,
    SAAlertAdapter,
    TasAlertAdapter,
)
from trailforge.feeds.base import (
    ALL_FEED_REGISTRY,
    FEED_REGISTRY,
    MAX_FEED_BODY_BYTES,
    BaseHttpFeedAdapter,
    DbSeverityLiteral,
    FeedSourceLiteral,
    NormalizedClosure,
    check_feed_record_count,
    get_feeds_for_states,
    normalize_alert_type,
    normalize_db_severity,
    normalize_geojson_geometry,
    normalize_state,
    normalize_status,
    parse_iso_datetime,
    parse_safe_xml,
    reject_partial_feed_snapshot,
    resolve_enabled_states,
    resolve_feed_source,
    sanitize_source_url,
)
from trailforge.ingestion.connectors.base import prop_ci

logger = logging.getLogger(__name__)

# Backward-compatible internal helper aliases
_normalize_geojson_geometry = normalize_geojson_geometry
_normalize_state = normalize_state
_normalize_status = normalize_status
_normalize_alert_type = normalize_alert_type
_normalize_db_severity = normalize_db_severity
_BaseHttpFeedAdapter = BaseHttpFeedAdapter

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


class VicSeasonalClosuresAdapter(_BaseHttpFeedAdapter):
    """VIC MapShareVic / DEECA seasonal road closure adapter."""

    feed_source: FeedSourceLiteral = "vic_mapshare_seasonal"

    def _fetch_snapshot(self) -> Any:
        response = self._http_get()
        payload = response.json()
        if not isinstance(payload, Mapping):
            return payload
        features = payload.get("features")
        total = payload.get("numberMatched", payload.get("totalFeatures"))
        if not isinstance(features, list) or not isinstance(total, int) or total <= len(features):
            reject_partial_feed_snapshot(payload)
            return payload

        url = response.url
        params = {k.lower(): v.lower() for k, v in url.params.multi_items()}
        if (
            params.get("service") != "wfs"
            or params.get("request") != "getfeature"
            or params.get("startindex", "0") != "0"
            or not features
        ):
            raise ValueError("VIC seasonal feed reported an incomplete snapshot")
        check_feed_record_count(total)
        remaining_bytes = MAX_FEED_BODY_BYTES - len(response.content)
        for _ in range(19):  # The default WFS page size is 500, with at most 10,000 records.
            if remaining_bytes <= 0:
                raise ValueError("VIC seasonal feed pages exceed the response byte limit")
            next_url = url.copy_set_param("startIndex", len(features))
            response = self._http_get(str(next_url), max_bytes=remaining_bytes)
            remaining_bytes -= len(response.content)
            page = response.json()
            if not isinstance(page, Mapping):
                raise ValueError("Unexpected VIC seasonal WFS page format")
            next_features = page.get("features")
            if (
                page.get("numberMatched", page.get("totalFeatures")) != total
                or not isinstance(next_features, list)
                or not next_features
                or len(features) + len(next_features) > total
            ):
                raise ValueError("VIC seasonal WFS page counts did not match the snapshot")
            features.extend(next_features)
            if len(features) == total:
                return features
        raise ValueError("VIC seasonal feed exceeds the WFS page limit")

    def _parse_item(self, item: Mapping[str, Any]) -> NormalizedClosure | None:
        # Handle both GeoJSON Feature (WFS/ArcGIS) and normalized fixture dict
        if item.get("type") == "Feature":
            props = dict(item.get("properties") or {})
            geom = _normalize_geojson_geometry(item.get("geometry"))
            ext_id = str(
                prop_ci(props, "external_id", "pfi", "ufi", "vdpid", "objectid", "id")
                or item.get("id")
                or ""
            ).strip()
        else:
            props = dict(item)
            geom = _normalize_geojson_geometry(item.get("geometry"))
            ext_id = str(
                prop_ci(props, "external_id", "pfi", "ufi", "vdpid", "objectid", "id") or ""
            ).strip()

        title = str(
            prop_ci(props, "title", "road_name", "ezi_road_name_label", "ezi_road_name", "name")
            or ""
        ).strip()
        if not title:
            return None
        if not ext_id:
            ext_id = f"vic-seasonal-{title.lower().replace(' ', '-')}"

        raw_desc = str(
            prop_ci(props, "description", "comments", "statusnote", "reason") or ""
        ).strip()

        return NormalizedClosure(
            source_feed=self.feed_source,
            external_id=ext_id,
            alert_type=_normalize_alert_type(
                prop_ci(props, "alert_type", "closure_type", "reason", "statusnote"),
                default="seasonal_closure",
            ),
            db_severity=_normalize_db_severity(
                prop_ci(props, "severity", "status"), default="closed"
            ),
            status=_normalize_status(prop_ci(props, "status"), default="active"),
            title=title,
            description=raw_desc or None,
            state=_normalize_state(prop_ci(props, "state"), default="VIC"),
            source_url=sanitize_source_url(
                prop_ci(props, "source_url", "url", "url_string"),
                fallback=self.source_url,
            ),
            geometry=geom,
            effective_from=parse_iso_datetime(
                prop_ci(props, "effective_from", "start_date", "closure_date")
            ),
            effective_to=parse_iso_datetime(
                prop_ci(props, "effective_to", "end_date", "reopen_date")
            ),
            source_updated_at=parse_iso_datetime(
                prop_ci(
                    props,
                    "source_updated_at",
                    "updated_at",
                    "last_updated",
                    "editdate",
                    "ufi_created",
                )
            )
            or datetime.now(UTC),
            raw_payload=props,
        )

    def fetch(self, *, session: Session | None = None) -> list[NormalizedClosure]:
        if self._fixture_items is not None:
            raw_items: list[Mapping[str, Any]] = self._fixture_items
        else:
            payload = (
                self._fixture_payload
                if self._fixture_payload is not None
                else self._fetch_snapshot()
            )
            if isinstance(payload, Mapping) and isinstance(payload.get("features"), list):
                raw_items = payload["features"]
            elif isinstance(payload, list):
                raw_items = payload
            else:
                raise ValueError("Unexpected VIC seasonal closures payload format")

        check_feed_record_count(len(raw_items))
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
            source_url=sanitize_source_url(
                prop_ci(props, "source_url", "url"),
                fallback="https://emergency.vic.gov.au/respond/",
            ),
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

        check_feed_record_count(len(raw_items))
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
            source_url=sanitize_source_url(
                prop_ci(props, "source_url", "link", "url"),
                fallback="https://www.rfs.nsw.gov.au/fire-information/fires-near-me",
            ),
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

        check_feed_record_count(len(raw_items))
        out: list[NormalizedClosure] = []
        for item in raw_items:
            if isinstance(item, Mapping):
                parsed = self._parse_item(item)
                if parsed is not None:
                    out.append(parsed)
        return out


MAX_NPWS_DESCRIPTION_CHARS: Final[int] = 32_768


class _NPWSDescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.body: list[str] = []
        self.headline: list[str] = []
        self._strong_depth = 0
        self.headline_complete = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "strong" and not self.headline_complete:
            self._strong_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "strong" and self._strong_depth:
            self._strong_depth -= 1
            self.headline_complete = self._strong_depth == 0

    def handle_data(self, data: str) -> None:
        self.body.append(data)
        if self._strong_depth:
            self.headline.append(data)


def _parse_npws_description(raw: str) -> tuple[str, str]:
    if len(raw) > MAX_NPWS_DESCRIPTION_CHARS:
        raise ValueError(f"NPWS description exceeds {MAX_NPWS_DESCRIPTION_CHARS} characters")
    parser = _NPWSDescriptionParser()
    parser.feed(raw)
    parser.close()
    headline = " ".join(" ".join(parser.headline).split()) if parser.headline_complete else ""
    body = " ".join(" ".join(parser.body).split())
    return headline, body


class NSWNPWSAdapter(_BaseHttpFeedAdapter):
    """NSW National Parks & Wildlife Service (NPWS) park alerts adapter (CAPAD joined).

    Supports both the official live NPWS RSS 2.0 feed (``/api/rssfeed/get``) and
    JSON alert fixtures/payloads.
    """

    feed_source: FeedSourceLiteral = "nsw_npws"

    def _parse_rss_items(self, xml_text: str) -> list[dict[str, Any]]:
        root = parse_safe_xml(xml_text)
        channel = root.find("channel") if root.tag == "rss" else None
        if channel is None:
            raise ValueError("Unexpected NSW NPWS RSS payload format")
        parsed_items: list[dict[str, Any]] = []
        for idx, elem in enumerate(channel.iterfind("item"), start=1):
            check_feed_record_count(idx)
            park_name = (elem.findtext("title") or "").strip()
            if not park_name:
                continue
            guid = (elem.findtext("guid") or "").strip()
            link = sanitize_source_url(elem.findtext("link"), fallback=self.source_url) or ""
            pub_date = (elem.findtext("pubDate") or "").strip()
            raw_desc = elem.findtext("description") or ""

            headline, plain_desc = _parse_npws_description(raw_desc)

            lower_blob = f"{headline} {plain_desc}".lower()
            if "closed" in lower_blob or "closure" in lower_blob:
                alert_type = "park_closure"
                severity = "closed"
            elif "fire" in lower_blob:
                alert_type = "bushfire"
                severity = "advisory"
            elif "flood" in lower_blob:
                alert_type = "flood"
                severity = "advisory"
            else:
                alert_type = "hazard_warning"
                severity = "advisory"

            if len(plain_desc) > 1200:
                plain_desc = plain_desc[:1197].rstrip() + "..."
            if len(headline) > 512:
                headline = headline[:509].rstrip() + "..."
            title = f"{park_name} — {headline}" if headline else park_name

            parsed_items.append(
                {
                    "id": guid or f"npws-rss-{park_name.lower().replace(' ', '-')[:48]}",
                    "title": title,
                    "parkName": park_name,
                    "alertType": alert_type,
                    "severity": severity,
                    "description": plain_desc or None,
                    "url": link,
                    "updated": pub_date or None,
                }
            )
        return parsed_items

    def _fetch_npws_payload(self) -> Any:
        resp = self._http_get()
        content_type = (resp.headers.get("content-type") or "").lower()
        body_text = resp.text
        if (
            "xml" in content_type
            or "rss" in content_type
            or body_text.lstrip().startswith("<")
        ):
            return self._parse_rss_items(body_text)
        payload = resp.json()
        reject_partial_feed_snapshot(payload)
        return payload

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
            source_url=sanitize_source_url(
                prop_ci(props, "source_url", "url"),
                fallback=self.source_url,
            ),
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
                self._fixture_payload
                if self._fixture_payload is not None
                else self._fetch_npws_payload()
            )
            if isinstance(payload, Mapping) and isinstance(payload.get("alerts"), list):
                raw_items = payload["alerts"]
            elif isinstance(payload, Mapping) and isinstance(payload.get("features"), list):
                raw_items = payload["features"]
            elif isinstance(payload, list):
                raw_items = payload
            else:
                raise ValueError("Unexpected NSW NPWS alerts payload format")

        check_feed_record_count(len(raw_items))
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


# Full Pan-Australia adapter registry (5 core VIC/NSW/OSM + 6 ACT/TAS/QLD/WA/SA/NT)
ADAPTER_REGISTRY: Final[dict[FeedSourceLiteral, type[Any]]] = {
    "vic_mapshare_seasonal": VicSeasonalClosuresAdapter,
    "vic_emergency": VicEmergencyAdapter,
    "nsw_rfs": NSWRFSAdapter,
    "nsw_npws": NSWNPWSAdapter,
    "osm_access": OSMAccessAdapter,
    "act_esa": ACTESAAlertsAdapter,
    "tas_alert": TasAlertAdapter,
    "qld_des_alerts": QLDParksAlertsAdapter,
    "wa_emergency": EmergencyWAAdapter,
    "sa_cfs_parks": SAAlertAdapter,
    "nt_securent": NTFireParksAdapter,
}


def build_default_adapters(
    *,
    client: httpx.Client | None = None,
    fixtures_path: Path | str | None = None,
    only_feeds: Sequence[str] | None = None,
    states: str | Sequence[str] | None = None,
    include_osm: bool = True,
) -> list[Any]:
    """Instantiate configured closure feed adapters (offline fixture-backed or live HTTP).

    Selection precedence:
    1. ``only_feeds`` (explicit feed names/aliases from any of the 11 registered adapters)
    2. ``states`` (explicit AU state codes or ``"ALL"``)
    3. ``TRAILFORGE_ENABLED_STATES`` env var / settings (defaults to ``"VIC,NSW"`` -> 5 core feeds)
    """
    fixture_feeds: dict[str, list[Mapping[str, Any]]] | None = None
    if fixtures_path is not None:
        with Path(fixtures_path).open("rb") as file:
            body = file.read(MAX_FEED_BODY_BYTES + 1)
        if len(body) > MAX_FEED_BODY_BYTES:
            raise ValueError(f"Closure fixture exceeds {MAX_FEED_BODY_BYTES} bytes")
        raw = json.loads(body)
        if not isinstance(raw, dict) or not isinstance(raw.get("feeds"), dict):
            raise ValueError("Closure fixture must contain a feeds object")
        fixture_feeds = raw["feeds"]
        for feed_key, records in fixture_feeds.items():
            if not isinstance(records, list) or any(
                not isinstance(item, dict) for item in records
            ):
                raise ValueError(
                    f"Closure fixture feed {feed_key!r} must contain a list of records"
                )
            check_feed_record_count(len(records))

    if only_feeds:
        selected_keys = [resolve_feed_source(name) for name in only_feeds]
        selected_set = set(selected_keys)
        ordered_keys = [k for k in ALL_FEED_REGISTRY if k in selected_set]
    else:
        enabled_states = resolve_enabled_states(states)
        ordered_keys = list(get_feeds_for_states(enabled_states, include_national=include_osm))

    adapters: list[Any] = []
    for feed_key in ordered_keys:
        adapter_cls = ADAPTER_REGISTRY[feed_key]
        if feed_key == "osm_access":
            adapters.append(adapter_cls())
        else:
            if fixture_feeds is not None and feed_key not in fixture_feeds:
                continue
            adapters.append(
                adapter_cls(
                    client=client,
                    fixture_items=fixture_feeds[feed_key] if fixture_feeds is not None else None,
                )
            )

    return adapters


__all__ = [
    "ACTESAAlertsAdapter",
    "ADAPTER_REGISTRY",
    "EmergencyWAAdapter",
    "NSWNPWSAdapter",
    "NSWRFSAdapter",
    "NTFireParksAdapter",
    "OSMAccessAdapter",
    "QLDParksAlertsAdapter",
    "SAAlertAdapter",
    "TasAlertAdapter",
    "VicEmergencyAdapter",
    "VicSeasonalClosuresAdapter",
    "build_default_adapters",
]
