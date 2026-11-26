"""Pan-Australia official state & territory closure/alert adapters (ACT, TAS, QLD, WA, SA, NT).

All 6 adapters implement the ``FeedAdapter`` protocol with:
- Injectable ``httpx.Client`` and optional offline fixture payloads for 100% offline CI
- Multi-format parsing (GeoJSON FeatureCollection, ArcGIS JSON, CAP-AU XML, RSS/GeoRSS, agency JSON)
- CAPAD protected-area ``park_name`` extraction for park-level joins when no narrow geometry exists
- Official agency ``source_url`` preservation
- Graceful outage propagation (raising ``httpx.HTTPError`` so ``sync_feed`` retains last-known
  alerts with ``is_stale=True``)
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy.orm import Session

from trailforge.feeds.base import (
    BaseHttpFeedAdapter,
    DbAlertTypeLiteral,
    DbSeverityLiteral,
    FeedSourceLiteral,
    NormalizedClosure,
    StateLiteral,
    normalize_alert_type,
    normalize_db_severity,
    normalize_geojson_geometry,
    normalize_state,
    normalize_status,
    parse_iso_datetime,
    strip_html,
)
from trailforge.ingestion.connectors.base import prop_ci

logger = logging.getLogger(__name__)

_RELEVANT_HAZARD_TOKENS: Final[tuple[str, ...]] = (
    "fire",
    "burn",
    "bushfire",
    "grass",
    "scrub",
    "smoke",
    "flood",
    "cyclone",
    "storm",
    "tree",
    "landslide",
    "rockfall",
    "road",
    "track",
    "trail",
    "park",
    "reserve",
    "camp",
    "forest",
    "warning",
    "watch",
    "advice",
    "advisory",
    "hazard",
    "clos",
    "restrict",
    "water",
    "bridge",
    "seasonal",
)

_IGNORE_NON_TRAIL_TOKENS: Final[tuple[str, ...]] = (
    "medical",
    "ambulance",
    "cardiac",
    "structure fire - residential",
    "alarm - automatic",
    "hazmat - domestic",
)

_PARK_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"[A-Z][A-Za-z0-9'\-/]+(?:\s+[A-Z0-9][A-Za-z0-9'\-/]+){0,6}\s+"
    r"(?:National Park|Conservation Park|Nature Reserve|State Forest|State Reserve|"
    r"Regional Park|Wilderness Area|Marine Park|Recreation Park|Forest Reserve|"
    r"Conservation Reserve|Resources Reserve)"
    r")\b"
)


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def _parse_lat_lon_pair(text: str) -> tuple[float, float] | None:
    """Parse ``'lat,lon'`` or ``'lat lon'`` strings into validated ``(lon, lat)``."""
    cleaned = text.strip()
    if not cleaned:
        return None
    parts = [p.strip() for p in re.split(r"[,\s]+", cleaned) if p.strip()]
    if len(parts) < 2:
        return None
    try:
        lat = float(parts[0])
        lon = float(parts[1])
    except ValueError:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return (lon, lat)


def _parse_georss_polygon(text: str) -> dict[str, Any] | None:
    """Parse GeoRSS ``lat1 lon1 lat2 lon2 ...`` or CAP ``lat1,lon1 lat2,lon2 ...`` polygon."""
    cleaned = text.strip()
    if not cleaned:
        return None
    ring: list[list[float]] = []
    if "," in cleaned:
        for pair in cleaned.split():
            coord = _parse_lat_lon_pair(pair)
            if coord is not None:
                ring.append([coord[0], coord[1]])
    else:
        nums = [p for p in cleaned.split() if p]
        if len(nums) >= 6 and len(nums) % 2 == 0:
            try:
                for i in range(0, len(nums), 2):
                    lat = float(nums[i])
                    lon = float(nums[i + 1])
                    if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                        ring.append([lon, lat])
            except ValueError:
                return None

    if len(ring) < 3:
        return None
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    if len(ring) < 4:
        return None
    return normalize_geojson_geometry({"type": "Polygon", "coordinates": [ring]})


def _extract_feature_collection_geometry(fc: Any) -> dict[str, Any] | None:
    """Extract highest-dimensional valid GeoJSON geometry from a FeatureCollection."""
    if not isinstance(fc, Mapping) or not isinstance(fc.get("features"), list):
        return None
    rank = {
        "MultiPolygon": 5,
        "Polygon": 4,
        "MultiLineString": 3,
        "LineString": 2,
        "Point": 1,
    }
    best: dict[str, Any] | None = None
    best_rank = 0
    for feat in fc["features"]:
        if not isinstance(feat, Mapping):
            continue
        geom = normalize_geojson_geometry(feat.get("geometry"))
        if geom is None:
            continue
        score = rank.get(str(geom.get("type") or ""), 0)
        if score > best_rank:
            best = geom
            best_rank = score
    return best


def _extract_geometry_from_props(
    props: Mapping[str, Any], raw_geom: Any = None
) -> dict[str, Any] | None:
    """Resolve GeoJSON geometry from explicit geometry field, lat/lon keys, or location string."""
    geom = normalize_geojson_geometry(raw_geom)
    if geom is not None:
        return geom

    nested_geom = props.get("geometry")
    if nested_geom is not None:
        geom = normalize_geojson_geometry(nested_geom)
        if geom is not None:
            return geom

    geo_source = props.get("geo-source") or props.get("geo_source")
    if geo_source is not None:
        geom = _extract_feature_collection_geometry(geo_source)
        if geom is not None:
            return geom

    lat = prop_ci(props, "latitude", "lat", "y", "geo_lat")
    lon = prop_ci(props, "longitude", "lon", "lng", "long", "x", "geo_lon")
    if lat is not None and lon is not None:
        try:
            lat_f, lon_f = float(lat), float(lon)
            if -90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0:
                return {"type": "Point", "coordinates": [lon_f, lat_f]}
        except (TypeError, ValueError):
            pass

    loc_str = prop_ci(props, "location", "coords", "coordinates", "point")
    if isinstance(loc_str, Mapping):
        mlat = prop_ci(loc_str, "latitude", "lat", "y")
        mlon = prop_ci(loc_str, "longitude", "lon", "lng", "long", "x")
        if mlat is not None and mlon is not None:
            try:
                lat_f, lon_f = float(mlat), float(mlon)
                if -90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0:
                    return {"type": "Point", "coordinates": [lon_f, lat_f]}
            except (TypeError, ValueError):
                pass
    elif isinstance(loc_str, str):
        pair = _parse_lat_lon_pair(loc_str)
        if pair is not None:
            return {"type": "Point", "coordinates": [pair[0], pair[1]]}
    elif isinstance(loc_str, (list, tuple)) and len(loc_str) == 2:
        try:
            a, b = float(loc_str[0]), float(loc_str[1])
            # Australian longitudes are ~110..155, latitudes ~-45..-9
            if 100.0 <= a <= 165.0 and -50.0 <= b <= -5.0:
                return {"type": "Point", "coordinates": [a, b]}
            if -50.0 <= a <= -5.0 and 100.0 <= b <= 165.0:
                return {"type": "Point", "coordinates": [b, a]}
        except (TypeError, ValueError):
            pass

    return None


def _extract_park_name(props: Mapping[str, Any], *fallback_texts: str | None) -> str | None:
    """Resolve CAPAD-joinable protected-area name from explicit fields or alert text."""
    explicit = prop_ci(
        props,
        "park_name",
        "parkName",
        "affectedPark",
        "affected_park",
        "park",
        "parks",
        "reserve",
        "reserve_name",
        "reserveName",
        "national_park",
        "protected_area",
    )
    if isinstance(explicit, list):
        for entry in explicit:
            if isinstance(entry, str) and entry.strip():
                return entry.strip()
            if isinstance(entry, Mapping):
                nm = str(prop_ci(entry, "name", "park_name", "parkName", "title") or "").strip()
                if nm:
                    return nm
    elif explicit is not None and str(explicit).strip():
        return str(explicit).strip()

    for txt in fallback_texts:
        if not txt:
            continue
        match = _PARK_NAME_RE.search(txt)
        if match:
            return match.group(1).strip()
    return None


def _parse_cap_au_xml(root: ET.Element, *, default_url: str) -> list[dict[str, Any]]:
    """Parse one or more CAP-AU 1.2 ``<alert>`` XML nodes into normalized item dicts."""
    alert_nodes: list[ET.Element] = []
    if _xml_local_name(root.tag) == "alert":
        alert_nodes.append(root)
    else:
        for node in root.iter():
            if _xml_local_name(node.tag) == "alert":
                alert_nodes.append(node)

    items: list[dict[str, Any]] = []
    for alert_el in alert_nodes:
        ident = ""
        sent = ""
        status_txt = ""
        info_nodes: list[ET.Element] = []
        for child in alert_el:
            lname = _xml_local_name(child.tag)
            if lname == "identifier":
                ident = (child.text or "").strip()
            elif lname == "sent":
                sent = (child.text or "").strip()
            elif lname == "status":
                status_txt = (child.text or "").strip()
            elif lname == "info":
                info_nodes.append(child)

        for idx, info_el in enumerate(info_nodes):
            fields: dict[str, str] = {}
            geom: dict[str, Any] | None = None
            area_descs: list[str] = []
            for child in info_el:
                lname = _xml_local_name(child.tag)
                if lname in (
                    "event",
                    "headline",
                    "description",
                    "severity",
                    "urgency",
                    "category",
                    "effective",
                    "expires",
                    "web",
                    "sendername",
                ):
                    fields[lname] = (child.text or "").strip()
                elif lname == "area":
                    for achild in child:
                        alname = _xml_local_name(achild.tag)
                        atext = (achild.text or "").strip()
                        if alname == "areadesc" and atext:
                            area_descs.append(atext)
                        elif alname == "polygon" and atext and geom is None:
                            geom = _parse_georss_polygon(atext)
                        elif alname == "circle" and atext and geom is None:
                            pair = _parse_lat_lon_pair(atext.split()[0])
                            if pair is not None:
                                geom = {"type": "Point", "coordinates": [pair[0], pair[1]]}

            title = fields.get("headline") or fields.get("event") or ""
            if not title:
                continue
            ext_id = ident if len(info_nodes) == 1 else f"{ident}-{idx + 1}"
            desc = strip_html(fields.get("description") or "") or None
            area_desc = ", ".join(area_descs) if area_descs else None
            severity_hint = " ".join(
                v for v in (fields.get("headline", ""), fields.get("severity", "")) if v
            )

            items.append(
                {
                    "id": ext_id or f"cap-{title.lower().replace(' ', '-')[:48]}",
                    "title": title,
                    "description": desc,
                    "category": fields.get("event") or fields.get("category") or "",
                    "severity": severity_hint or fields.get("severity") or "advisory",
                    "status": "cleared" if status_txt.lower() == "cancel" else "active",
                    "url": fields.get("web") or default_url,
                    "effective_from": fields.get("effective") or sent or None,
                    "effective_to": fields.get("expires") or None,
                    "updated": sent or fields.get("effective") or None,
                    "areaDesc": area_desc,
                    "geometry": geom,
                }
            )
    return items


def _parse_rss_or_atom_xml(root: ET.Element, *, default_url: str) -> list[dict[str, Any]]:
    """Parse RSS 2.0 / GeoRSS or Atom XML feeds into normalized item dicts."""
    items: list[dict[str, Any]] = []
    entry_nodes = [
        node for node in root.iter() if _xml_local_name(node.tag) in ("item", "entry")
    ]
    for node in entry_nodes:
        title = ""
        guid = ""
        link = ""
        desc_raw = ""
        pub_date = ""
        categories: list[str] = []
        geom: dict[str, Any] | None = None
        geo_lat: str | None = None
        geo_lon: str | None = None
        park_hint: str | None = None

        for child in node:
            lname = _xml_local_name(child.tag)
            text = (child.text or "").strip()
            if lname == "title":
                title = strip_html(text)
            elif lname in ("guid", "id"):
                guid = text
            elif lname == "link":
                href = (child.attrib.get("href") or "").strip()
                link = href or text or link
            elif lname in ("description", "summary", "content"):
                if text:
                    desc_raw = text
            elif lname in ("pubdate", "updated", "published"):
                pub_date = text or pub_date
            elif lname == "category":
                cat = (child.attrib.get("term") or text).strip()
                if cat:
                    categories.append(cat)
            elif lname == "point" and text and geom is None:
                pair = _parse_lat_lon_pair(text)
                if pair is not None:
                    geom = {"type": "Point", "coordinates": [pair[0], pair[1]]}
            elif lname == "polygon" and text and geom is None:
                geom = _parse_georss_polygon(text)
            elif lname == "lat" and text:
                geo_lat = text
            elif lname in ("long", "lon", "lng") and text:
                geo_lon = text
            elif lname in ("park", "parkname", "area") and text:
                park_hint = text

        if geom is None and geo_lat is not None and geo_lon is not None:
            pair = _parse_lat_lon_pair(f"{geo_lat},{geo_lon}")
            if pair is not None:
                geom = {"type": "Point", "coordinates": [pair[0], pair[1]]}

        if not title:
            continue

        plain_desc = strip_html(desc_raw)
        if len(plain_desc) > 1200:
            plain_desc = plain_desc[:1197].rstrip() + "..."

        items.append(
            {
                "id": guid or f"rss-{title.lower().replace(' ', '-')[:48]}",
                "title": title,
                "description": plain_desc or None,
                "category": " ".join(categories),
                "url": link or default_url,
                "updated": pub_date or None,
                "park_name": park_hint,
                "geometry": geom,
            }
        )
    return items


def _parse_xml_feed(xml_text: str, *, default_url: str) -> list[dict[str, Any]]:
    """Auto-detect CAP-AU 1.2 XML vs RSS/GeoRSS/Atom XML."""
    root = ET.fromstring(xml_text)
    lname = _xml_local_name(root.tag)
    if lname == "alert" or any(_xml_local_name(n.tag) == "alert" for n in root.iter()):
        cap_items = _parse_cap_au_xml(root, default_url=default_url)
        if cap_items:
            return cap_items
    return _parse_rss_or_atom_xml(root, default_url=default_url)


class _PanAUFeedAdapter(BaseHttpFeedAdapter):
    """Shared multi-format adapter base for ACT, TAS, QLD, WA, SA, and NT feeds."""

    default_state: StateLiteral
    default_official_url: str
    default_alert_type: DbAlertTypeLiteral = "hazard_warning"
    default_severity: DbSeverityLiteral = "advisory"
    filter_irrelevant_hazards: bool = False
    id_prefix: str = "au"

    def _fetch_raw_payload(self) -> Any:
        if self._fixture_payload is not None:
            if isinstance(self._fixture_payload, str):
                stripped = self._fixture_payload.lstrip()
                if stripped.startswith("<"):
                    return _parse_xml_feed(
                        self._fixture_payload, default_url=self.default_official_url
                    )
            return self._fixture_payload

        resp = self._http_get()
        content_type = (resp.headers.get("content-type") or "").lower()
        body_text = resp.text
        if (
            "xml" in content_type
            or "rss" in content_type
            or "atom" in content_type
            or body_text.lstrip().startswith("<")
        ):
            return _parse_xml_feed(body_text, default_url=self.default_official_url)
        return resp.json()

    def _extract_raw_items(self, payload: Any) -> list[Mapping[str, Any]]:
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, Mapping)]
        if isinstance(payload, Mapping):
            for key in (
                "features",
                "alerts",
                "warnings",
                "incidents",
                "items",
                "events",
                "results",
                "data",
                "response",
            ):
                val = payload.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, Mapping)]
            # Combined Emergency WA / multi-key envelopes
            combined: list[Mapping[str, Any]] = []
            for key in ("warnings", "incidents", "alerts", "features"):
                val = payload.get(key)
                if isinstance(val, Mapping) and isinstance(val.get("features"), list):
                    combined.extend(x for x in val["features"] if isinstance(x, Mapping))
            if combined:
                return combined
        raise ValueError(f"Unexpected {self.feed_source} payload format")

    def _is_relevant(self, props: Mapping[str, Any], title: str, desc: str | None) -> bool:
        if not self.filter_irrelevant_hazards:
            return True
        blob = " ".join(
            str(x or "")
            for x in (
                prop_ci(
                    props,
                    "alert_type",
                    "alertType",
                    "category",
                    "category1",
                    "type",
                    "incidentType",
                    "warningType",
                    "event",
                ),
                title,
                desc or "",
            )
        ).lower()
        if any(tok in blob for tok in _IGNORE_NON_TRAIL_TOKENS):
            return False
        return any(tok in blob for tok in _RELEVANT_HAZARD_TOKENS)

    def _parse_item(self, item: Mapping[str, Any]) -> NormalizedClosure | None:
        if item.get("type") == "Feature":
            props = dict(item.get("properties") or {})
            raw_geom = item.get("geometry")
            feature_id = item.get("id")
        elif "attributes" in item and isinstance(item.get("attributes"), Mapping):
            # ArcGIS REST JSON feature format
            props = dict(item["attributes"])
            raw_geom = item.get("geometry")
            feature_id = props.get("OBJECTID") or props.get("objectid")
        else:
            props = dict(item)
            raw_geom = item.get("geometry")
            feature_id = None

        title = str(
            prop_ci(
                props,
                "title",
                "headline",
                "name",
                "incident_name",
                "incidentName",
                "warning_title",
                "event",
                "location_name",
                "locationName",
                "areaName",
                "road_name",
            )
            or ""
        ).strip()
        if not title:
            return None

        desc_raw = prop_ci(
            props,
            "description",
            "details",
            "body",
            "summary",
            "message",
            "advice",
            "webBody",
            "status_note",
            "comments",
        )
        desc = strip_html(str(desc_raw)) if desc_raw is not None and str(desc_raw).strip() else None

        if not self._is_relevant(props, title, desc):
            return None

        ext_id = str(
            prop_ci(
                props,
                "external_id",
                "id",
                "guid",
                "incident_id",
                "incidentId",
                "IncidentNo",
                "alert_id",
                "alertId",
                "warning_id",
                "warningId",
                "sourceId",
                "objectid",
            )
            or feature_id
            or ""
        ).strip()
        if not ext_id:
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48]
            ext_id = f"{self.id_prefix}-{slug}"

        park_name = _extract_park_name(
            props,
            str(prop_ci(props, "areaDesc", "area_desc", "region", "location_name") or ""),
            title,
            desc,
        )

        explicit_alert_type = str(prop_ci(props, "alert_type") or "").strip().lower()
        if explicit_alert_type in (
            "seasonal_closure",
            "road_closure",
            "park_closure",
            "bushfire",
            "flood",
            "hazard_warning",
            "private_gate",
        ):
            alert_type: DbAlertTypeLiteral = explicit_alert_type  # type: ignore[assignment]
        else:
            raw_type_blob = " ".join(
                str(x or "")
                for x in (
                    prop_ci(
                        props,
                        "alert_type",
                        "alertType",
                        "closure_type",
                        "type",
                        "incidentType",
                        "warningType",
                        "category",
                        "hazard",
                        "event",
                    ),
                    title,
                )
            )
            alert_type = normalize_alert_type(raw_type_blob, default=self.default_alert_type)

        raw_sev = prop_ci(
            props,
            "severity",
            "alert_level",
            "alertLevel",
            "warning_level",
            "warningLevel",
            "level",
            "cap_severity",
            "feedType",
        )
        default_sev: DbSeverityLiteral = (
            "closed"
            if alert_type in ("park_closure", "road_closure", "seasonal_closure")
            else self.default_severity
        )
        db_sev = normalize_db_severity(
            raw_sev if raw_sev is not None else title,
            default=default_sev,
        )

        raw_status = prop_ci(props, "status", "incident_status", "state_status")
        closure_status = normalize_status(raw_status, default="active")

        geom = _extract_geometry_from_props(props, raw_geom=raw_geom)
        source_url = str(
            prop_ci(props, "source_url", "url", "link", "web", "more_info", "moreInfo")
            or self.default_official_url
        ).strip()

        effective_from = parse_iso_datetime(
            prop_ci(
                props,
                "effective_from",
                "effective",
                "start_date",
                "startDate",
                "created",
                "published",
                "pubDate",
            )
        )
        effective_to = parse_iso_datetime(
            prop_ci(props, "effective_to", "expires", "end_date", "endDate", "reopen_date")
        )
        updated_at = (
            parse_iso_datetime(
                prop_ci(
                    props,
                    "source_updated_at",
                    "updated",
                    "updated_at",
                    "last_updated",
                    "lastUpdated",
                    "modified",
                    "pubDate",
                    "sent",
                )
            )
            or effective_from
            or datetime.now(UTC)
        )

        raw_payload = dict(props)
        if park_name and alert_type == "hazard_warning":
            raw_payload.setdefault("park_alert", True)

        return NormalizedClosure(
            source_feed=self.feed_source,
            external_id=ext_id,
            alert_type=alert_type,
            db_severity=db_sev,
            status=closure_status,
            title=title,
            description=desc,
            state=normalize_state(prop_ci(props, "state"), default=self.default_state),
            source_url=source_url,
            park_name=park_name,
            geometry=geom,
            effective_from=effective_from,
            effective_to=effective_to,
            source_updated_at=updated_at,
            raw_payload=raw_payload,
        )

    def fetch(self, *, session: Session | None = None) -> list[NormalizedClosure]:
        if self._fixture_items is not None:
            raw_items: list[Mapping[str, Any]] = self._fixture_items
        else:
            payload = self._fetch_raw_payload()
            raw_items = self._extract_raw_items(payload)

        out: list[NormalizedClosure] = []
        for item in raw_items:
            if isinstance(item, Mapping):
                parsed = self._parse_item(item)
                if parsed is not None:
                    out.append(parsed)
        return out


class ACTESAAlertsAdapter(_PanAUFeedAdapter):
    """ACT Emergency Services Agency (ESA) & Parks & Conservation Service alerts adapter."""

    feed_source: FeedSourceLiteral = "act_esa"
    default_state: StateLiteral = "ACT"
    default_official_url: str = "https://esa.act.gov.au/"
    default_alert_type: DbAlertTypeLiteral = "hazard_warning"
    default_severity: DbSeverityLiteral = "advisory"
    filter_irrelevant_hazards: bool = True
    id_prefix: str = "act-esa"


class TasAlertAdapter(_PanAUFeedAdapter):
    """TasALERT / Tasmania Fire Service / Parks & Wildlife Service Tasmania adapter."""

    feed_source: FeedSourceLiteral = "tas_alert"
    default_state: StateLiteral = "TAS"
    default_official_url: str = "https://alert.tas.gov.au/"
    default_alert_type: DbAlertTypeLiteral = "hazard_warning"
    default_severity: DbSeverityLiteral = "advisory"
    filter_irrelevant_hazards: bool = True
    id_prefix: str = "tas-alert"


def _parse_lat_lon_list_pair(value: Any) -> tuple[float, float] | None:
    """Parse ``[lat, lon]`` or ``[lon, lat]`` numeric 2-tuples into validated ``(lon, lat)``."""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        a, b = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    if a == 0.0 and b == 0.0:
        return None
    if -50.0 <= a <= -5.0 and 100.0 <= b <= 165.0:
        return (b, a)
    if 100.0 <= a <= 165.0 and -50.0 <= b <= -5.0:
        return (a, b)
    if -90.0 <= a <= 90.0 and -180.0 <= b <= 180.0:
        return (b, a)
    return None


class QLDParksAlertsAdapter(_PanAUFeedAdapter):
    """QLD QFD live GeoJSON bushfire alerts + QPWS park alerts (RSS/JSON, CAPAD joined)."""

    feed_source: FeedSourceLiteral = "qld_des_alerts"
    default_state: StateLiteral = "QLD"
    default_official_url: str = "https://www.fire.qld.gov.au/current-incidents"
    default_alert_type: DbAlertTypeLiteral = "hazard_warning"
    default_severity: DbSeverityLiteral = "advisory"
    filter_irrelevant_hazards: bool = False
    id_prefix: str = "qld-des"

    def _parse_item(self, item: Mapping[str, Any]) -> NormalizedClosure | None:
        # Enrich Queensland Fire Department (QFD) GeoJSON `bushfireAlert.json` features
        if item.get("type") == "Feature":
            raw_props = item.get("properties")
            if isinstance(raw_props, Mapping) and (
                "WarningTitle" in raw_props
                or "UniqueID" in raw_props
                or "WarningLevel" in raw_props
            ):
                props = dict(raw_props)
                ext_id = str(
                    prop_ci(props, "UniqueID", "OBJECTID", "id") or item.get("id") or ""
                ).strip()
                title = str(
                    prop_ci(props, "WarningTitle", "title", "WarningArea") or ""
                ).strip()
                if not title:
                    return None
                header = str(prop_ci(props, "Header") or "").strip()
                body = str(prop_ci(props, "WarningText") or "").strip()
                desc_text = header or body
                if len(desc_text) > 1200:
                    desc_text = desc_text[:1197].rstrip() + "..."
                event_blob = " ".join(
                    str(v or "")
                    for v in (
                        prop_ci(props, "EventType"),
                        prop_ci(props, "GroupedType"),
                        prop_ci(props, "WarningType_Level"),
                    )
                ).strip()
                props.setdefault("id", ext_id)
                props.setdefault("title", title)
                if desc_text:
                    props.setdefault("description", desc_text)
                if event_blob:
                    props.setdefault("category", event_blob)
                if prop_ci(props, "WarningLevel"):
                    props.setdefault("severity", prop_ci(props, "WarningLevel"))
                if prop_ci(props, "WarningArea"):
                    props.setdefault("areaDesc", prop_ci(props, "WarningArea"))
                if prop_ci(props, "ItemDateTimeLocal_ISO"):
                    props.setdefault("effective_from", prop_ci(props, "ItemDateTimeLocal_ISO"))
                if prop_ci(props, "ItemExpiryDateTimeLocal_ISO"):
                    props.setdefault("effective_to", prop_ci(props, "ItemExpiryDateTimeLocal_ISO"))
                updated = prop_ci(props, "PublishDateLocal_ISO", "ItemDateTimeLocal_ISO")
                if updated:
                    props.setdefault("updated", updated)
                props.setdefault("url", self.default_official_url)
                enriched = {
                    "type": "Feature",
                    "id": ext_id or item.get("id"),
                    "properties": props,
                    "geometry": item.get("geometry"),
                }
                return super()._parse_item(enriched)
        return super()._parse_item(item)


class EmergencyWAAdapter(_PanAUFeedAdapter):
    """Emergency WA v1 API (DFES / DBCA Parks & Wildlife) bushfire & park alerts adapter."""

    feed_source: FeedSourceLiteral = "wa_emergency"
    default_state: StateLiteral = "WA"
    default_official_url: str = "https://www.emergency.wa.gov.au/"
    default_alert_type: DbAlertTypeLiteral = "bushfire"
    default_severity: DbSeverityLiteral = "advisory"
    filter_irrelevant_hazards: bool = True
    id_prefix: str = "wa-em"

    def _parse_item(self, item: Mapping[str, Any]) -> NormalizedClosure | None:
        # Enrich Emergency WA v1 `/v1/warnings` & `/v1/incidents` JSON records
        if item.get("type") != "Feature" and (
            "geo-source" in item
            or "warning-type" in item
            or "cap-event-type" in item
            or "alert-line" in item
        ):
            props = dict(item)
            ext_id = str(prop_ci(props, "id", "warning_id", "event") or "").strip()
            warning_type = str(prop_ci(props, "warning-type", "name") or "").strip()
            entity_sub = str(prop_ci(props, "entitySubType") or "").strip()
            raw_title = str(prop_ci(props, "title", "headline") or warning_type).strip()
            if not raw_title:
                return None
            if warning_type and warning_type.lower() not in raw_title.lower():
                title = f"{warning_type} — {raw_title}"
            else:
                title = raw_title
            props["title"] = title

            if not props.get("description"):
                desc_candidate = (
                    prop_ci(props, "alert-line", "action-statement", "what-to-do-note") or ""
                )
                if str(desc_candidate).strip():
                    props["description"] = str(desc_candidate)

            cap_events = props.get("cap-event-type")
            cap_event_txt = (
                " ".join(str(x) for x in cap_events if x)
                if isinstance(cap_events, list)
                else str(cap_events or "")
            )
            cat_blob = " ".join(
                v
                for v in (
                    cap_event_txt,
                    str(props.get("cap-category") or ""),
                    warning_type,
                    entity_sub,
                )
                if v
            ).strip()
            if cat_blob:
                props.setdefault("category", cat_blob)

            # Avoid defaulting non-fire warnings (e.g. Heatwave/Storm Advice) to "bushfire"
            cat_lower = cat_blob.lower()
            if not any(
                tok in cat_lower
                for tok in ("fire", "burn", "smoke", "flood", "cyclone", "clos", "gate")
            ):
                props.setdefault("alert_type", "hazard_warning")

            # Derive severity from warning-type/entitySubType before CAP minor fallback
            sev_hint = warning_type or entity_sub or str(props.get("cap-severity") or "")
            if sev_hint:
                props["severity"] = sev_hint

            pub_status = str(props.get("publishing-status") or "").strip().lower()
            if pub_status == "published":
                props.setdefault("status", "active")
            elif pub_status:
                props.setdefault("status", pub_status)

            area_parts: list[str] = []
            for key in ("headline", "suburbs", "lga"):
                val = props.get(key)
                if isinstance(val, list):
                    area_parts.extend(str(x).strip() for x in val if str(x).strip())
                elif isinstance(val, str) and val.strip():
                    area_parts.append(val.strip())
            loc_obj = props.get("location")
            if isinstance(loc_obj, Mapping) and loc_obj.get("value"):
                area_parts.append(str(loc_obj["value"]).strip())
            if area_parts:
                area_joined = ", ".join(area_parts)
                # Include title-cased variant so ALL-CAPS WA area strings match park-name regex
                props.setdefault("areaDesc", f"{area_joined} | {area_joined.title()}")

            if not prop_ci(props, "source_url", "url", "link"):
                props["url"] = (
                    f"https://www.emergency.wa.gov.au/alerts/{ext_id}"
                    if ext_id
                    else self.default_official_url
                )
            eff = prop_ci(props, "issued-date-time", "published-date-time")
            if eff:
                props.setdefault("effective_from", eff)
            upd = prop_ci(props, "updatedAt", "issued-date-time", "published-date-time")
            if upd:
                props.setdefault("updated", upd)
            return super()._parse_item(props)
        return super()._parse_item(item)


class SAAlertAdapter(_PanAUFeedAdapter):
    """SA CFS CRIIMSON incidents + National Parks & Wildlife Service SA alerts adapter."""

    feed_source: FeedSourceLiteral = "sa_cfs_parks"
    default_state: StateLiteral = "SA"
    default_official_url: str = (
        "https://www.cfs.sa.gov.au/warnings-restrictions/warnings/incidents-warnings/"
    )
    default_alert_type: DbAlertTypeLiteral = "bushfire"
    default_severity: DbSeverityLiteral = "advisory"
    filter_irrelevant_hazards: bool = True
    id_prefix: str = "sa-cfs"

    def _parse_item(self, item: Mapping[str, Any]) -> NormalizedClosure | None:
        # Enrich SA CFS CRIIMSON flat JSON objects before standard parsing
        if (
            item.get("type") != "Feature"
            and ("IncidentNo" in item or "Location_name" in item or "CFS_Region" in item)
        ):
            props = dict(item)
            loc_name = str(prop_ci(props, "Location_name", "location_name") or "").strip()
            inc_type = str(prop_ci(props, "Type", "incidentType") or "").strip()
            region = str(prop_ci(props, "CFS_Region", "region") or "").strip()
            if not props.get("title"):
                if loc_name and inc_type:
                    props["title"] = f"{loc_name} — {inc_type}"
                else:
                    props["title"] = loc_name or inc_type
            if not props.get("description") and (inc_type or region):
                parts = [p for p in (inc_type, f"Region: {region}" if region else "") if p]
                props["description"] = " | ".join(parts)
            date_part = str(prop_ci(props, "Date") or "").strip()
            time_part = str(prop_ci(props, "Time") or "").strip()
            if date_part and not props.get("updated"):
                props["updated"] = f"{date_part}T{time_part}" if time_part else date_part
            return super()._parse_item(props)
        return super()._parse_item(item)


class NTFireParksAdapter(_PanAUFeedAdapter):
    """NT Road Report closures + SecureNT / Bushfires NT / Parks NT alerts adapter."""

    feed_source: FeedSourceLiteral = "nt_securent"
    default_state: StateLiteral = "NT"
    default_official_url: str = "https://roadreport.nt.gov.au/"
    default_alert_type: DbAlertTypeLiteral = "hazard_warning"
    default_severity: DbSeverityLiteral = "advisory"
    filter_irrelevant_hazards: bool = True
    id_prefix: str = "nt-securent"

    def _parse_item(self, item: Mapping[str, Any]) -> NormalizedClosure | None:
        # 1. Road Report NT public API (`https://roadreport.nt.gov.au/api/Obstruction/GetAll`)
        if item.get("type") != "Feature" and (
            "roadName" in item and ("obstructionId" in item or "restrictionType" in item)
        ):
            road_name = str(item.get("roadName") or "").strip()
            restriction = str(item.get("restrictionType") or "").strip()
            obstruction = str(item.get("obstructionType") or "").strip()
            if not road_name:
                return None

            rest_low = restriction.lower()
            obs_low = obstruction.lower()
            # Filter out commercial heavy-vehicle axle/mass-limit advisories unrelated to 4WD access
            if (
                "weight" in rest_low
                and "clos" not in rest_low
                and "impass" not in rest_low
                and "4wd" not in rest_low
                and "flood" not in obs_low
                and "fire" not in obs_low
            ):
                return None

            if restriction and obstruction and obstruction.lower() != rest_low:
                title = f"{road_name} — {restriction} ({obstruction})"
            elif restriction or obstruction:
                title = f"{road_name} — {restriction or obstruction}"
            else:
                title = road_name

            desc_parts = [
                str(v).strip()
                for v in (item.get("locationComment"), item.get("comment"))
                if v is not None and str(v).strip() not in ("", "-")
            ]
            desc = " | ".join(desc_parts) if desc_parts else None

            if "road closed" in rest_low or "impassable" in rest_low or "closed" in rest_low:
                alert_type: DbAlertTypeLiteral = "road_closure"
                severity: DbSeverityLiteral = "closed"
            elif "flood" in obs_low or "water" in obs_low:
                alert_type = "flood"
                severity = "advisory"
            elif "fire" in obs_low or "smoke" in obs_low:
                alert_type = "bushfire"
                severity = "advisory"
            else:
                alert_type = "hazard_warning"
                severity = "advisory"

            raw_status = str(item.get("status") or "CURRENT").strip().upper()
            status = "active" if raw_status == "CURRENT" else normalize_status(raw_status)

            start_pt = _parse_lat_lon_list_pair(item.get("startPoint"))
            end_pt = _parse_lat_lon_list_pair(item.get("endPoint"))
            geom: dict[str, Any] | None = None
            if start_pt is not None and end_pt is not None:
                if abs(start_pt[0] - end_pt[0]) > 1e-5 or abs(start_pt[1] - end_pt[1]) > 1e-5:
                    geom = {
                        "type": "LineString",
                        "coordinates": [
                            [start_pt[0], start_pt[1]],
                            [end_pt[0], end_pt[1]],
                        ],
                    }
                else:
                    geom = {"type": "Point", "coordinates": [start_pt[0], start_pt[1]]}
            elif start_pt is not None:
                geom = {"type": "Point", "coordinates": [start_pt[0], start_pt[1]]}
            elif end_pt is not None:
                geom = {"type": "Point", "coordinates": [end_pt[0], end_pt[1]]}

            obs_id = item.get("obstructionId") or item.get("recordId")
            props = dict(item)
            props["id"] = f"nt-rr-{obs_id}" if obs_id is not None else ""
            props["title"] = title
            if desc:
                props["description"] = desc
            props["alert_type"] = alert_type
            props["severity"] = severity
            props["status"] = status
            props["url"] = self.default_official_url
            if geom is not None:
                props["geometry"] = geom
            if item.get("dateFrom") or item.get("dateActive"):
                props["effective_from"] = item.get("dateFrom") or item.get("dateActive")
            if item.get("dateTo"):
                props["effective_to"] = item.get("dateTo")
            updated = item.get("dateLastUpdated") or item.get("dateActive") or item.get("dateFrom")
            if updated:
                props["updated"] = updated
            return super()._parse_item(props)

        # 2. NTG Hosted ArcGIS Fire_Incidents FeatureServer GeoJSON fallback
        if item.get("type") == "Feature":
            raw_props = item.get("properties")
            if isinstance(raw_props, Mapping) and (
                "f_eventtype" in raw_props or "fire_type" in raw_props
            ):
                props = dict(raw_props)
                loc = str(prop_ci(props, "location", "f_location") or "").strip()
                ev = str(prop_ci(props, "fire_type", "f_eventtype") or "").strip()
                props.setdefault("title", f"{loc} — {ev}" if loc and ev else (loc or ev))
                props.setdefault("category", ev or str(props.get("f_category") or ""))
                props.setdefault("status", prop_ci(props, "status", "f_status") or "active")
                props.setdefault("severity", prop_ci(props, "alert_level") or "advisory")
                props.setdefault("effective_from", prop_ci(props, "f_datenotified"))
                props.setdefault("updated", prop_ci(props, "f_lastupdate", "f_datenotified"))
                props.setdefault("url", "https://securent.nt.gov.au/")
                return super()._parse_item(
                    {
                        "type": "Feature",
                        "id": item.get("id") or prop_ci(props, "objectid"),
                        "properties": props,
                        "geometry": item.get("geometry"),
                    }
                )

        return super()._parse_item(item)


AU_STATE_ADAPTER_CLASSES: Final[dict[FeedSourceLiteral, type[BaseHttpFeedAdapter]]] = {
    "act_esa": ACTESAAlertsAdapter,
    "tas_alert": TasAlertAdapter,
    "qld_des_alerts": QLDParksAlertsAdapter,
    "wa_emergency": EmergencyWAAdapter,
    "sa_cfs_parks": SAAlertAdapter,
    "nt_securent": NTFireParksAdapter,
}
