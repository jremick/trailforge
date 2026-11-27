"""Pluggable FeedAdapter protocol, Pan-Australia feed registry, and normalized closure models."""

from __future__ import annotations

import html
import io
import ipaddress
import os
import re
import socket
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Final, Literal, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, Field, field_validator
from shapely.geometry import GeometryCollection, mapping, shape
from sqlalchemy.orm import Session

from trailforge.config import get_settings
from trailforge.db.models import FeedSyncStatus
from trailforge.ingestion.connectors.base import USER_AGENT

FeedSourceLiteral = Literal[
    "vic_mapshare_seasonal",
    "vic_emergency",
    "nsw_rfs",
    "nsw_npws",
    "osm_access",
    "act_esa",
    "tas_alert",
    "qld_des_alerts",
    "wa_emergency",
    "sa_cfs_parks",
    "nt_securent",
]
DbAlertTypeLiteral = Literal[
    "seasonal_closure",
    "road_closure",
    "park_closure",
    "bushfire",
    "flood",
    "hazard_warning",
    "private_gate",
]
ApiClosureTypeLiteral = Literal[
    "closed",
    "seasonal",
    "incident",
    "park_alert",
    "locked_gate",
    "private_access",
]
DbSeverityLiteral = Literal["info", "advisory", "watch_and_act", "closed", "emergency"]
ApiSeverityLiteral = Literal["critical", "warning", "info"]
ClosureStatusLiteral = Literal["active", "scheduled", "cleared", "stale"]
TrackClosureStatusLiteral = Literal["open", "info", "warning", "seasonal", "gated", "closed"]
StateLiteral = Literal["VIC", "NSW", "ACT", "TAS", "QLD", "WA", "SA", "NT"]

ALL_AU_STATES: Final[tuple[StateLiteral, ...]] = (
    "VIC",
    "NSW",
    "ACT",
    "TAS",
    "QLD",
    "WA",
    "SA",
    "NT",
)
DEFAULT_ENABLED_STATES: Final[tuple[StateLiteral, ...]] = ("VIC", "NSW")

DEFAULT_VIC_SEASONAL_URL: Final[str] = (
    "https://opendata.maps.vic.gov.au/geoserver/wfs"
    "?service=WFS&version=2.0.0&request=GetFeature"
    "&typeNames=open-data-platform:paim_vm_tr_road_closures"
    "&outputFormat=application/json&srsName=EPSG:4326&count=500"
)
DEFAULT_VIC_EMERGENCY_URL: Final[str] = (
    "https://emergency.vic.gov.au/public/events-geojson.json"
)
DEFAULT_NSW_RFS_URL: Final[str] = "https://www.rfs.nsw.gov.au/feeds/majorIncidents.json"
DEFAULT_NSW_NPWS_URL: Final[str] = "https://www.nationalparks.nsw.gov.au/api/rssfeed/get"
DEFAULT_OSM_ACCESS_URL: Final[str] = "https://www.openstreetmap.org/copyright"

# Pan-Australia official state & territory alert/closure feed defaults
DEFAULT_ACT_ESA_URL: Final[str] = "https://www.esa.act.gov.au/feeds/currentincidents.xml"
DEFAULT_TAS_ALERT_URL: Final[str] = (
    "https://services.thelist.tas.gov.au/arcgis/rest/services/Public/"
    "EmergencyManagementPublic/MapServer/72/query"
    "?where=1%3D1&outFields=*&f=geojson"
)
DEFAULT_QLD_DES_ALERTS_URL: Final[str] = "https://www.fire.qld.gov.au/data/alerts/bushfireAlert.json"
DEFAULT_WA_EMERGENCY_URL: Final[str] = "https://api.emergency.wa.gov.au/v1/warnings"
DEFAULT_SA_CFS_PARKS_URL: Final[str] = (
    "https://data.eso.sa.gov.au/prod/cfs/criimson/cfs_current_incidents.json"
)
DEFAULT_NT_SECURENT_URL: Final[str] = "https://roadreport.nt.gov.au/api/Obstruction/GetAll"

LIVE_DATASET_KEY: Final[str] = "live"
FIXTURE_DATASET_KEY: Final[str] = "fixture"
_URL_SCOPED_FEEDS: Final[frozenset[str]] = frozenset({"qld_des_alerts", "nt_securent"})

_PARK_ALERT_FEEDS: Final[frozenset[str]] = frozenset(
    {
        "nsw_npws",
        "qld_des_alerts",
    }
)


@dataclass(frozen=True)
class FeedMetadata:
    """Static registry metadata for an official or OSM access closure feed."""

    feed_source: FeedSourceLiteral
    alias: str
    display_name: str
    agency: str
    state: str
    default_url: str
    env_var: str
    sync_interval_s: int
    stale_after_s: int

    def resolve_url(self) -> str:
        return os.environ.get(self.env_var, self.default_url).strip() or self.default_url


# Core v1 VIC/NSW/OSM registry (preserved 100% backward-compatible for Slice 04 tests)
FEED_REGISTRY: Final[dict[FeedSourceLiteral, FeedMetadata]] = {
    "vic_mapshare_seasonal": FeedMetadata(
        feed_source="vic_mapshare_seasonal",
        alias="vic_seasonal",
        display_name="VIC MapShareVic Seasonal Closures",
        agency="DEECA / Parks Victoria (MapShareVic)",
        state="VIC",
        default_url=DEFAULT_VIC_SEASONAL_URL,
        env_var="TRAILFORGE_VIC_SEASONAL_URL",
        sync_interval_s=21600,  # 6 hours
        stale_after_s=86400,  # 24 hours
    ),
    "vic_emergency": FeedMetadata(
        feed_source="vic_emergency",
        alias="vic_emergency",
        display_name="VicEmergency Incidents",
        agency="VicEmergency",
        state="VIC",
        default_url=DEFAULT_VIC_EMERGENCY_URL,
        env_var="TRAILFORGE_VIC_EMERGENCY_URL",
        sync_interval_s=900,  # 15 minutes
        stale_after_s=3600,  # 1 hour
    ),
    "nsw_rfs": FeedMetadata(
        feed_source="nsw_rfs",
        alias="nsw_rfs",
        display_name="NSW RFS Major Incidents",
        agency="NSW Rural Fire Service (RFS)",
        state="NSW",
        default_url=DEFAULT_NSW_RFS_URL,
        env_var="TRAILFORGE_NSW_RFS_URL",
        sync_interval_s=900,  # 15 minutes
        stale_after_s=3600,  # 1 hour
    ),
    "nsw_npws": FeedMetadata(
        feed_source="nsw_npws",
        alias="nsw_npws",
        display_name="NSW NPWS Park Alerts",
        agency="NSW National Parks & Wildlife Service (NPWS)",
        state="NSW",
        default_url=DEFAULT_NSW_NPWS_URL,
        env_var="TRAILFORGE_NSW_NPWS_URL",
        sync_interval_s=3600,  # 1 hour
        stale_after_s=14400,  # 4 hours
    ),
    "osm_access": FeedMetadata(
        feed_source="osm_access",
        alias="osm_access",
        display_name="OSM Gates & Private Access",
        agency="OpenStreetMap Contributors",
        state="AU",
        default_url=DEFAULT_OSM_ACCESS_URL,
        env_var="TRAILFORGE_OSM_ACCESS_URL",
        sync_interval_s=86400,  # 24 hours
        stale_after_s=604800,  # 7 days
    ),
}

# Pan-Australia expansion feeds for ACT, TAS, QLD, WA, SA, and NT (Phase B / v1.1.0)
AU_STATE_FEED_REGISTRY: Final[dict[FeedSourceLiteral, FeedMetadata]] = {
    "act_esa": FeedMetadata(
        feed_source="act_esa",
        alias="act_alerts",
        display_name="ACT ESA & Parks Alerts",
        agency="ACT Emergency Services Agency / Parks & Conservation Service",
        state="ACT",
        default_url=DEFAULT_ACT_ESA_URL,
        env_var="TRAILFORGE_ACT_ESA_URL",
        sync_interval_s=900,  # 15 minutes
        stale_after_s=3600,  # 1 hour
    ),
    "tas_alert": FeedMetadata(
        feed_source="tas_alert",
        alias="tas_alerts",
        display_name="TasALERT & PWS Tasmania Alerts",
        agency="TasALERT / Tasmania Fire Service / Parks & Wildlife Service Tasmania",
        state="TAS",
        default_url=DEFAULT_TAS_ALERT_URL,
        env_var="TRAILFORGE_TAS_ALERT_URL",
        sync_interval_s=900,  # 15 minutes
        stale_after_s=3600,  # 1 hour
    ),
    "qld_des_alerts": FeedMetadata(
        feed_source="qld_des_alerts",
        alias="qld_parks",
        display_name="QLD QFD Bushfire Alerts & QPWS Park Alerts",
        agency="Queensland Fire Department (QFD) / QLD DESI (QPWS)",
        state="QLD",
        default_url=DEFAULT_QLD_DES_ALERTS_URL,
        env_var="TRAILFORGE_QLD_DES_ALERTS_URL",
        sync_interval_s=900,  # 15 minutes
        stale_after_s=3600,  # 1 hour
    ),
    "wa_emergency": FeedMetadata(
        feed_source="wa_emergency",
        alias="wa_dfes",
        display_name="Emergency WA Bushfire & Park Alerts",
        agency="Emergency WA (DFES / DBCA Parks & Wildlife)",
        state="WA",
        default_url=DEFAULT_WA_EMERGENCY_URL,
        env_var="TRAILFORGE_WA_EMERGENCY_URL",
        sync_interval_s=900,  # 15 minutes
        stale_after_s=3600,  # 1 hour
    ),
    "sa_cfs_parks": FeedMetadata(
        feed_source="sa_cfs_parks",
        alias="sa_cfs",
        display_name="SA CFS Incidents & NPWS SA Park Alerts",
        agency="SA Country Fire Service (CFS) / National Parks & Wildlife Service SA",
        state="SA",
        default_url=DEFAULT_SA_CFS_PARKS_URL,
        env_var="TRAILFORGE_SA_CFS_PARKS_URL",
        sync_interval_s=900,  # 15 minutes
        stale_after_s=3600,  # 1 hour
    ),
    "nt_securent": FeedMetadata(
        feed_source="nt_securent",
        alias="nt_alerts",
        display_name="NT Road Report Closures & Bushfire/Parks Alerts",
        agency="NT DIPL (Road Report NT) / SecureNT / Bushfires NT / Parks NT",
        state="NT",
        default_url=DEFAULT_NT_SECURENT_URL,
        env_var="TRAILFORGE_NT_SECURENT_URL",
        sync_interval_s=1800,  # 30 minutes
        stale_after_s=7200,  # 2 hours
    ),
}

# Full Pan-Australia feed registry (all 11 feeds across all 8 states & territories)
ALL_FEED_REGISTRY: Final[dict[FeedSourceLiteral, FeedMetadata]] = {
    **FEED_REGISTRY,
    **AU_STATE_FEED_REGISTRY,
}

FEED_ALIAS_MAP: Final[dict[str, FeedSourceLiteral]] = {
    **{k: k for k in ALL_FEED_REGISTRY},
    **{meta.alias: k for k, meta in ALL_FEED_REGISTRY.items()},
}


def resolve_feed_source(name: str) -> FeedSourceLiteral:
    key = name.strip().lower()
    if key not in FEED_ALIAS_MAP:
        valid = ", ".join(sorted(FEED_ALIAS_MAP.keys()))
        raise ValueError(f"Unknown feed source {name!r}. Valid values: {valid}")
    return FEED_ALIAS_MAP[key]


def parse_state_filter(raw: str | Sequence[str] | None) -> tuple[StateLiteral, ...]:
    """Parse a state code, comma-separated list, or ``ALL`` into canonical AU state codes."""
    if raw is None:
        return ()

    tokens: list[str] = []
    if isinstance(raw, str):
        parts = raw.split(",")
    else:
        parts = []
        for entry in raw:
            parts.extend(str(entry).split(","))

    for part in parts:
        cleaned = part.strip().upper()
        if cleaned:
            tokens.append(cleaned)

    if not tokens:
        return ()

    if any(tok in ("ALL", "*", "AU") for tok in tokens):
        return ALL_AU_STATES

    valid_set = set(ALL_AU_STATES)
    resolved: list[StateLiteral] = []
    seen: set[str] = set()
    for tok in tokens:
        if tok not in valid_set:
            valid_list = ", ".join(ALL_AU_STATES)
            raise ValueError(
                f"Unknown Australian state {tok!r}. Valid values: {valid_list}, or ALL"
            )
        if tok not in seen:
            seen.add(tok)
            resolved.append(tok)  # type: ignore[arg-type]
    return tuple(resolved)


def resolve_enabled_states(states: str | Sequence[str] | None = None) -> tuple[StateLiteral, ...]:
    """Resolve enabled states from explicit override, ``TRAILFORGE_ENABLED_STATES``, or default.

    Defaults to ``("VIC", "NSW")`` when unset so default homelab polling and existing
    Slice 04 VIC/NSW tests stay lean and 100% backward-compatible. Set
    ``TRAILFORGE_ENABLED_STATES=VIC,NSW,ACT`` or ``ALL`` to opt into additional states.
    """
    if states is not None:
        parsed = parse_state_filter(states)
        if parsed:
            return parsed

    env_val = os.environ.get("TRAILFORGE_ENABLED_STATES")
    if env_val is not None and env_val.strip():
        return parse_state_filter(env_val)

    try:
        settings_val = getattr(get_settings(), "enabled_states", None)
    except Exception:
        settings_val = None
    if settings_val:
        parsed_settings = parse_state_filter(settings_val)
        if parsed_settings:
            return parsed_settings

    return DEFAULT_ENABLED_STATES


def get_feeds_for_states(
    states: Sequence[str],
    *,
    include_national: bool = True,
) -> tuple[FeedSourceLiteral, ...]:
    """Return feed keys from ``ALL_FEED_REGISTRY`` matching the given AU state codes."""
    state_set = {s.strip().upper() for s in states if s and s.strip()}
    if "ALL" in state_set or "AU" in state_set or "*" in state_set:
        state_set = set(ALL_AU_STATES)
        include_national = True

    selected: list[FeedSourceLiteral] = []
    for feed_key, meta in ALL_FEED_REGISTRY.items():
        if meta.state in state_set or (include_national and meta.state in ("AU", "VIC/NSW")):
            selected.append(feed_key)
    return tuple(selected)


def parse_iso_datetime(value: Any) -> datetime | None:
    """Parse ISO-8601, epoch-ms, or RFC-2822 timestamps cleanly into UTC-aware ``datetime``."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = float(value)
        if ts > 1e11:  # ArcGIS epoch milliseconds
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.isdigit() and len(raw) >= 10:
        try:
            ts = float(raw)
            if ts > 1e11:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=UTC)
        except (OSError, OverflowError, ValueError):
            pass
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(normalized)
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(raw)
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except Exception:
        return None


def normalize_geojson_geometry(raw_geom: Any) -> dict[str, Any] | None:
    """Validate/normalize GeoJSON or ArcGIS geometry; unwrap GeometryCollection cleanly."""
    if not isinstance(raw_geom, Mapping):
        return None

    geom_obj: Mapping[str, Any] = raw_geom
    if "type" not in geom_obj:
        # Handle ArcGIS JSON geometry fallbacks (x/y, rings, paths in WGS84)
        if "x" in geom_obj and "y" in geom_obj:
            try:
                geom_obj = {
                    "type": "Point",
                    "coordinates": [float(geom_obj["x"]), float(geom_obj["y"])],
                }
            except (TypeError, ValueError):
                return None
        elif isinstance(geom_obj.get("rings"), list) and geom_obj["rings"]:
            geom_obj = {"type": "Polygon", "coordinates": geom_obj["rings"]}
        elif isinstance(geom_obj.get("paths"), list) and geom_obj["paths"]:
            paths = geom_obj["paths"]
            if len(paths) == 1:
                geom_obj = {"type": "LineString", "coordinates": paths[0]}
            else:
                geom_obj = {"type": "MultiLineString", "coordinates": paths}
        else:
            return None

    _check_geometry_budget(geom_obj)
    try:
        shp = shape(geom_obj)
    except Exception:
        return None
    if shp.is_empty:
        return None
    if isinstance(shp, GeometryCollection):
        for wanted in ("MultiPolygon", "Polygon", "MultiLineString", "LineString", "Point"):
            for part in shp.geoms:
                if part.geom_type == wanted and not part.is_empty:
                    return dict(mapping(part))
        return None
    return dict(mapping(shp))


def _check_geometry_budget(geometry: Mapping[str, Any]) -> None:
    """Bound coordinate and collection traversal before passing data to GEOS."""
    remaining = MAX_FEED_GEOMETRY_VALUES

    def visit(value: Any, depth: int) -> None:
        nonlocal remaining
        if depth > MAX_FEED_GEOMETRY_DEPTH:
            raise ValueError("Upstream feed geometry is nested too deeply")
        if isinstance(value, Mapping):
            visit(value.get("coordinates"), depth + 1)
            geom_type = str(value.get("type", "")).lower()
            if geom_type == "geometrycollection":
                visit(value.get("geometries"), depth + 1)
            elif geom_type == "feature":
                visit(value.get("geometry"), depth + 1)
        elif isinstance(value, (list, tuple)):
            remaining -= len(value)
            if remaining < 0:
                raise ValueError("Upstream feed geometry exceeds the coordinate budget")
            for child in value:
                if isinstance(child, (list, tuple, Mapping)):
                    visit(child, depth + 1)

    visit(geometry, 0)


def normalize_state(raw: Any, default: StateLiteral) -> StateLiteral:
    val = str(raw or "").strip().upper()
    if val in ALL_AU_STATES:
        return val  # type: ignore[return-value]
    return default


def normalize_status(raw: Any, default: ClosureStatusLiteral = "active") -> ClosureStatusLiteral:
    val = str(raw or "").strip().lower()
    if val in ("active", "scheduled", "cleared", "stale"):
        return val  # type: ignore[return-value]
    if val in (
        "safe",
        "under control",
        "closed_incident",
        "resolved",
        "complete",
        "completed",
        "all clear",
        "cancelled",
        "expired",
    ):
        return "cleared"
    if val in ("planned", "upcoming", "future", "pending"):
        return "scheduled"
    return default


def normalize_alert_type(raw: Any, default: DbAlertTypeLiteral) -> DbAlertTypeLiteral:
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
    if ("park" in val or "reserve" in val or "camp" in val or "trail" in val) and "clos" in val:
        return "park_closure"
    if ("road" in val or "track" in val or "highway" in val or "bridge" in val) and "clos" in val:
        return "road_closure"
    if "fire" in val or "burn" in val or "smoke" in val or "bushfire" in val:
        return "bushfire"
    if "flood" in val or "water" in val or "cyclone" in val or "river" in val:
        return "flood"
    if "gate" in val or "private" in val:
        return "private_gate"
    if "clos" in val:
        return "road_closure"
    return default


def normalize_db_severity(raw: Any, default: DbSeverityLiteral) -> DbSeverityLiteral:
    val = str(raw or "").strip().lower()
    if val in ("info", "advisory", "watch_and_act", "closed", "emergency"):
        return val  # type: ignore[return-value]
    if "emergency" in val or "evacuate" in val or val == "critical":
        return "emergency"
    if ("watch" in val and "act" in val) or "severe" in val:
        return "watch_and_act"
    if "extreme" in val:
        return "emergency"
    if "clos" in val:
        return "closed"
    if (
        "advic" in val
        or "advis" in val
        or "warn" in val
        or "caution" in val
        or "moderate" in val
        or "alert" in val
    ):
        return "advisory"
    if "minor" in val or "info" in val or "community" in val:
        return "info"
    return default


_HTML_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<[^<>]+>")
_WS_COLLAPSE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")
_UNSAFE_XML_PATTERN: Final[re.Pattern[str]] = re.compile(r"<!(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_SAFE_HTTP_URL_RE: Final[re.Pattern[str]] = re.compile(r"^https?://", re.IGNORECASE)
MAX_FEED_BODY_BYTES: Final[int] = 10 * 1024 * 1024  # 10 MiB upstream feed response cap
MAX_FEED_XML_ELEMENTS: Final[int] = 250_000
MAX_FEED_RECORDS: Final[int] = 10_000
MAX_FEED_GEOMETRY_VALUES: Final[int] = 200_000
MAX_FEED_GEOMETRY_DEPTH: Final[int] = 12
MAX_FEED_REDIRECTS: Final[int] = 5


def check_feed_record_count(count: int) -> None:
    if count > MAX_FEED_RECORDS:
        raise ValueError(f"Upstream feed contains too many records (limit {MAX_FEED_RECORDS})")


def _int_not_bool(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def extract_wfs_total_count(payload: Mapping[str, Any]) -> int | None:
    """Return WFS total count, falling back to totalFeatures when numberMatched is null."""
    raw = payload.get("numberMatched")
    if raw is None:
        raw = payload.get("totalFeatures")
    return _int_not_bool(raw)


def reject_partial_feed_snapshot(payload: Any) -> None:
    """Reject upstream responses that explicitly indicate missing records."""
    if not isinstance(payload, Mapping):
        return
    if payload.get("exceededTransferLimit") is True:
        raise ValueError("Upstream feed reported an incomplete snapshot")
    matched = extract_wfs_total_count(payload)
    returned = _int_not_bool(payload.get("numberReturned"))
    features = payload.get("features")
    if returned is None and isinstance(features, list):
        returned = len(features)
    if matched is not None and returned is not None and matched > returned:
        raise ValueError("Upstream feed reported an incomplete snapshot")


def _bounded_feed_response_body(resp: httpx.Response, max_bytes: int) -> bytes:
    over_limit = f"Upstream feed response exceeds {max_bytes} byte limit"
    if resp.is_stream_consumed:
        # In-process test transports can supply an already-buffered, decoded response.
        if len(resp.content) > max_bytes:
            raise ValueError(over_limit)
        return resp.content

    encoding = resp.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("", "identity", "gzip", "x-gzip"):
        raise ValueError("Unsupported upstream feed content encoding")
    decoder = zlib.decompressobj(zlib.MAX_WBITS + 16) if encoding in ("gzip", "x-gzip") else None
    wire_bytes = 0
    body = bytearray()
    for chunk in resp.iter_raw():
        wire_bytes += len(chunk)
        if wire_bytes > max_bytes:
            raise ValueError(over_limit)
        if decoder is not None:
            data = chunk
            while data:
                try:
                    out = decoder.decompress(data, max_bytes - len(body) + 1)
                except zlib.error as exc:
                    raise ValueError("Invalid upstream gzip feed") from exc
                if len(body) + len(out) > max_bytes:
                    raise ValueError(over_limit)
                body.extend(out)
                if decoder.eof:
                    trailing = decoder.unused_data
                    if not trailing:
                        break
                    decoder = zlib.decompressobj(zlib.MAX_WBITS + 16)
                    data = trailing
                else:
                    break
        else:
            if len(body) + len(chunk) > max_bytes:
                raise ValueError(over_limit)
            body.extend(chunk)
    if decoder is not None and not decoder.eof:
        raise ValueError("Incomplete upstream gzip feed")
    return bytes(body)


@contextmanager
def _stream_feed_response(http: httpx.Client, url: str) -> Iterator[httpx.Response]:
    request = http.build_request(
        "GET", url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, identity"}
    )
    for _ in range(MAX_FEED_REDIRECTS + 1):
        # HTTPX automatic redirects buffer intermediate response bodies.
        with closing(http.send(request, stream=True, follow_redirects=False)) as response:
            if response.next_request is None:
                yield response
                return
            request = response.next_request
    raise httpx.TooManyRedirects("Too many upstream feed redirects", request=request)


def _assert_safe_feed_url(url: httpx.URL) -> None:
    """SSRF guard blocking redirects or requests targeting non-public IP ranges."""
    scheme = (url.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Unsupported upstream feed URL scheme: {scheme!r}")
    host = (url.host or "").strip().lower()
    if not host or host in ("localhost", "localhost.localdomain"):
        raise ValueError(f"Upstream feed target host is not allowed: {host!r}")
    try:
        literal_ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if not literal_ip.is_global:
            raise ValueError(f"Upstream feed target IP is not publicly routable: {literal_ip}")
        return
    try:
        port = url.port or (443 if scheme == "https" else 80)
        addr_infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return
    for info in addr_infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        try:
            resolved_ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if not resolved_ip.is_global:
            raise ValueError(f"Upstream feed target resolves to non-public IP: {resolved_ip}")


def strip_html(raw: str) -> str:
    """Decode HTML entities and strip markup into normalized whitespace text."""
    unescaped = html.unescape(raw)
    cleaned = _HTML_TAG_RE.sub(" ", unescaped)
    return _WS_COLLAPSE_RE.sub(" ", cleaned).strip()


def parse_safe_xml(xml_text: str) -> ET.Element:
    """Parse upstream XML feed text rejecting ``<!DOCTYPE``/``<!ENTITY`` and huge DOMs."""
    if _UNSAFE_XML_PATTERN.search(xml_text):
        raise ValueError("XML DOCTYPE/ENTITY declarations are not permitted in upstream XML feeds")
    stream = io.BytesIO(xml_text.encode("utf-8"))
    root: ET.Element | None = None
    elem_count = 0
    for event, elem in ET.iterparse(stream, events=("start",)):
        if event == "start":
            elem_count += 1
            if elem_count > MAX_FEED_XML_ELEMENTS:
                raise ValueError(
                    f"Upstream XML feed exceeds maximum allowed {MAX_FEED_XML_ELEMENTS} elements"
                )
            if root is None:
                root = elem
    if root is None:
        raise ValueError("Upstream XML feed is empty")
    return root


def sanitize_source_url(url: Any, *, fallback: str | None = None) -> str | None:
    """Return the first valid ``http(s)://`` URL (stripping control chars), else ``None``."""
    for candidate in (url, fallback):
        if candidate is None:
            continue
        cleaned = str(candidate).strip()
        if not cleaned:
            continue
        if any(ch in cleaned for ch in ("\r", "\n", "\x00")):
            continue
        if _SAFE_HTTP_URL_RE.match(cleaned):
            return cleaned
    return None


class BaseHttpFeedAdapter:
    """Shared HTTP fetch + offline fixture support for official AU feed adapters."""

    feed_source: FeedSourceLiteral

    def __init__(
        self,
        *,
        source_url: str | None = None,
        timeout_s: float = 20.0,
        client: httpx.Client | None = None,
        fixture_items: Sequence[Mapping[str, Any]] | None = None,
        fixture_payload: Mapping[str, Any] | str | None = None,
    ) -> None:
        meta = ALL_FEED_REGISTRY[self.feed_source]
        self.display_name = meta.display_name
        self.agency = meta.agency
        self.state = meta.state
        self.source_url = source_url or meta.resolve_url()
        self.sync_interval_s = meta.sync_interval_s
        self.stale_after_s = meta.stale_after_s
        self.timeout_s = timeout_s
        self._client = client
        self._fixture_items = list(fixture_items) if fixture_items is not None else None
        if isinstance(fixture_payload, Mapping):
            self._fixture_payload: dict[str, Any] | str | None = dict(fixture_payload)
        else:
            self._fixture_payload = fixture_payload

    def _http_get(
        self,
        url: str | None = None,
        *,
        max_bytes: int = MAX_FEED_BODY_BYTES,
    ) -> httpx.Response:
        target = url or self.source_url
        owns_client = self._client is None
        http = (
            self._client
            if self._client is not None
            else httpx.Client(
                timeout=httpx.Timeout(self.timeout_s, connect=min(8.0, self.timeout_s)),
                headers={"User-Agent": USER_AGENT},
                event_hooks={"request": [lambda req: _assert_safe_feed_url(req.url)]},
            )
        )
        try:
            if hasattr(http, "build_request") and hasattr(http, "send"):
                with _stream_feed_response(http, target) as resp:
                    resp.raise_for_status()
                    raw_cl = resp.headers.get("content-length")
                    if raw_cl:
                        try:
                            declared = int(raw_cl)
                        except ValueError:
                            declared = -1
                        if declared > max_bytes:
                            raise ValueError(
                                f"Upstream feed response exceeds {max_bytes} byte limit"
                            )
                    body = _bounded_feed_response_body(resp, max_bytes)
                    clean_headers = [
                        (k, v)
                        for k, v in resp.headers.multi_items()
                        if k.lower()
                        not in {"content-encoding", "content-length", "transfer-encoding"}
                    ]
                    return httpx.Response(
                        status_code=resp.status_code,
                        headers=clean_headers,
                        content=body,
                        request=resp.request,
                    )

            resp = http.get(target, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            if len(resp.content) > max_bytes:
                raise ValueError(f"Upstream feed response exceeds {max_bytes} byte limit")
            return resp
        finally:
            if owns_client:
                http.close()

    def _fetch_json(self) -> Any:
        payload = self._http_get().json()
        reject_partial_feed_snapshot(payload)
        return payload

    @property
    def uses_fixtures(self) -> bool:
        return self._fixture_items is not None or self._fixture_payload is not None


def map_db_to_api_severity(db_severity: str) -> ApiSeverityLiteral:
    sev = db_severity.strip().lower()
    if sev in ("closed", "emergency", "critical"):
        return "critical"
    if sev in ("watch_and_act", "advisory", "warning"):
        return "warning"
    return "info"


def map_alert_to_closure_type(
    alert_type: str,
    *,
    source_feed: str,
    title: str = "",
    raw_payload: dict[str, Any] | None = None,
    capad_park_id: int | None = None,
) -> ApiClosureTypeLiteral:
    """Map DB ``alert_type`` + feed context to the locked API ``closure_type``."""
    payload = raw_payload or {}
    explicit = payload.get("closure_type")
    if explicit in (
        "closed",
        "seasonal",
        "incident",
        "park_alert",
        "locked_gate",
        "private_access",
    ):
        return explicit  # type: ignore[return-value]

    at = alert_type.strip().lower()
    if at == "seasonal_closure":
        return "seasonal"
    if at in ("road_closure", "park_closure"):
        return "closed"
    if at == "private_gate":
        if payload.get("access_kind") == "private_access":
            return "private_access"
        if payload.get("has_gate_barrier") or "gate" in title.lower():
            return "locked_gate"
        return "private_access"
    if at == "hazard_warning" and (
        source_feed in _PARK_ALERT_FEEDS
        or capad_park_id is not None
        or bool(payload.get("park_alert"))
    ):
        return "park_alert"
    return "incident"


def compute_feed_is_stale(sync_row: FeedSyncStatus | None, *, now: datetime | None = None) -> bool:
    """Return True if a feed has consecutive failures, never succeeded, or exceeded TTL."""
    if sync_row is None:
        return True
    if sync_row.last_status in ("failed", "degraded", "never_run"):
        return True
    if sync_row.consecutive_failures > 0 or sync_row.last_success_at is None:
        return True
    current = now or datetime.now(UTC)
    last_ok = (
        sync_row.last_success_at.astimezone(UTC)
        if sync_row.last_success_at.tzinfo
        else sync_row.last_success_at.replace(tzinfo=UTC)
    )
    return (current - last_ok) > timedelta(seconds=max(1, int(sync_row.stale_after_s)))


class NormalizedClosure(BaseModel):
    """Normalized closure/incident/gate record emitted by any ``FeedAdapter``."""

    source_feed: FeedSourceLiteral
    external_id: str = Field(min_length=1, max_length=512)
    alert_type: DbAlertTypeLiteral
    db_severity: DbSeverityLiteral
    status: ClosureStatusLiteral = "active"
    title: str
    description: str | None = None
    state: StateLiteral
    source_url: str | None = None
    park_name: str | None = None
    geometry: dict[str, Any] | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    source_updated_at: datetime | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    # Optional pre-resolved links (used by osm_access adapter)
    direct_segment_ids: list[str] = Field(default_factory=list)

    @field_validator("source_url", mode="before")
    @classmethod
    def _validate_source_url(cls, value: Any) -> str | None:
        return sanitize_source_url(value)


@runtime_checkable
class FeedAdapter(Protocol):
    """Pluggable feed connector protocol for Australian agency feeds and OSM access propagation."""

    feed_source: FeedSourceLiteral
    display_name: str
    agency: str
    state: str
    source_url: str
    sync_interval_s: int
    stale_after_s: int

    def fetch(self, *, session: Session | None = None) -> list[NormalizedClosure]:
        """Fetch and normalize current active/scheduled alerts from the upstream source."""
        ...


def live_dataset_key(feed_source: str, source_url: str) -> str:
    """QLD and NT feed slots can point at independent agency datasets."""
    if feed_source in _URL_SCOPED_FEEDS:
        # Keep query parameters, which may select a specific upstream layer/filter.
        endpoint = source_url.strip().partition("#")[0]
        return f"endpoint:{sha256(endpoint.encode()).hexdigest()}"
    return LIVE_DATASET_KEY


def live_feed_polling_enabled() -> bool:
    enabled = os.environ.get("TRAILFORGE_ENABLE_LIVE_FEEDS", "").strip().lower()
    return enabled in ("1", "true", "yes", "on")


def adapter_dataset_key(adapter: FeedAdapter) -> str:
    if isinstance(adapter, BaseHttpFeedAdapter) and adapter.uses_fixtures:
        return FIXTURE_DATASET_KEY
    return live_dataset_key(adapter.feed_source, adapter.source_url)
