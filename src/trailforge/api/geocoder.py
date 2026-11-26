"""Bounded, cached Australian place-name geocoder fallback (Nominatim OSM).

Used by ``GET /api/search`` only when local PostGIS tracks/POIs/parks return few
results so users can still pan to any Australian landmark, track name, or town
(e.g. "Billy Goat Bluff") even outside currently baked corridors.

Respects OSM Nominatim usage policy:
- Custom identifying ``User-Agent``
- Strict ``<= 1 req/s`` global outbound rate gate (non-blocking fail-open)
- Bounded in-memory LRU + TTL cache (512 entries, 1 h TTL)
- Strict Australia bounding box & ``countrycodes=au`` filter
- Short timeout and bounded response size (<= 256 KiB)
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict
from typing import Any, Final

import httpx
from pydantic import BaseModel

from trailforge.config import get_settings

logger = logging.getLogger("trailforge.api.geocoder")

GEOCODER_USER_AGENT: Final[str] = (
    "TrailForge/1.1 (+https://github.com/jremick/trailforge; AU 4WD map search)"
)
AU_VIEWBOX: Final[str] = "112.85,-10.0,153.68,-43.75"
MAX_GEOCODER_BODY_BYTES: Final[int] = 256 * 1024
CACHE_MAX_ENTRIES: Final[int] = 512
CACHE_TTL_SECONDS: Final[float] = 3600.0
MIN_REQUEST_INTERVAL_S: Final[float] = 1.05
MAX_RATE_WAIT_S: Final[float] = 0.35

_STATE_NAME_TO_CODE: Final[dict[str, str]] = {
    "victoria": "VIC",
    "vic": "VIC",
    "new south wales": "NSW",
    "nsw": "NSW",
    "australian capital territory": "ACT",
    "act": "ACT",
    "tasmania": "TAS",
    "tas": "TAS",
    "queensland": "QLD",
    "qld": "QLD",
    "western australia": "WA",
    "wa": "WA",
    "south australia": "SA",
    "sa": "SA",
    "northern territory": "NT",
    "nt": "NT",
}


class GeocodedPlace(BaseModel):
    """Normalized Australian place result returned by Nominatim fallback."""

    id: str
    name: str
    state: str
    subtitle: str
    category: str
    lon: float
    lat: float
    bbox: list[float] | None = None


_cache_lock = threading.Lock()
_cache: OrderedDict[tuple[str, int], tuple[float, list[GeocodedPlace]]] = OrderedDict()
_rate_lock = threading.Lock()
_last_request_monotonic: float = 0.0


def reset_geocoder_cache() -> None:
    """Clear in-memory geocoder cache and rate-limit clock (for unit tests)."""
    global _last_request_monotonic
    with _cache_lock:
        _cache.clear()
    with _rate_lock:
        _last_request_monotonic = 0.0


def _normalize_au_state(address: dict[str, Any] | None) -> str:
    if not isinstance(address, dict):
        return "AU"
    for key in ("ISO3166-2-lvl4", "state", "territory"):
        raw = str(address.get(key) or "").strip()
        if not raw:
            continue
        if raw.upper().startswith("AU-"):
            code = raw.upper().split("AU-", 1)[1].strip()
            if code in {"VIC", "NSW", "ACT", "TAS", "QLD", "WA", "SA", "NT"}:
                return code
        mapped = _STATE_NAME_TO_CODE.get(raw.lower())
        if mapped:
            return mapped
    return "AU"


def _parse_nominatim_bbox(raw_bbox: Any) -> list[float] | None:
    """Convert Nominatim ``[south, north, west, east]`` to ``[w, s, e, n]``."""
    if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
        return None
    try:
        south, north, west, east = (float(v) for v in raw_bbox)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (west, south, east, north)):
        return None
    if not (-180.0 <= west < east <= 180.0 and -90.0 <= south < north <= 90.0):
        return None
    # Ignore tiny point-sized bounding boxes so map zoom uses flyToCoordinate cleanly
    if (east - west) < 0.0005 and (north - south) < 0.0005:
        return None
    return [round(west, 6), round(south, 6), round(east, 6), round(north, 6)]


def _parse_nominatim_items(payload: Any, *, limit: int) -> list[GeocodedPlace]:
    if not isinstance(payload, list):
        return []
    out: list[GeocodedPlace] = []
    seen_ids: set[str] = set()
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        try:
            lat = float(raw["lat"])
            lon = float(raw["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        if not (112.0 <= lon <= 154.5 and -44.5 <= lat <= -9.5):
            continue

        osm_type = str(raw.get("osm_type") or "place").strip().lower()
        osm_id = str(raw.get("osm_id") or raw.get("place_id") or len(out)).strip()
        place_id = f"nominatim:{osm_type}:{osm_id}"
        if place_id in seen_ids:
            continue
        seen_ids.add(place_id)

        display_name = str(raw.get("display_name") or "").strip()
        raw_name = str(raw.get("name") or "").strip()
        name = raw_name or (display_name.split(",", 1)[0].strip() if display_name else "")
        if not name:
            continue

        address = raw.get("address") if isinstance(raw.get("address"), dict) else None
        state = _normalize_au_state(address)
        raw_type = str(raw.get("type") or raw.get("category") or "place")
        place_type = raw_type.replace("_", " ").strip()
        locality = ""
        if isinstance(address, dict):
            for k in ("suburb", "town", "village", "municipality", "county", "region"):
                v = str(address.get(k) or "").strip()
                if v and v.lower() != name.lower():
                    locality = v
                    break

        sub_parts = [place_type.title() if place_type else "Place"]
        if locality:
            sub_parts.append(locality)
        sub_parts.append("OSM place")

        out.append(
            GeocodedPlace(
                id=place_id,
                name=name,
                state=state,
                subtitle=" · ".join(sub_parts),
                category=place_type.lower() or "place",
                lon=round(lon, 6),
                lat=round(lat, 6),
                bbox=_parse_nominatim_bbox(raw.get("boundingbox")),
            )
        )
        if len(out) >= limit:
            break
    return out


def _acquire_rate_slot() -> bool:
    """Enforce >= 1.05s between outbound Nominatim calls without stalling requests."""
    global _last_request_monotonic
    with _rate_lock:
        now = time.monotonic()
        elapsed = now - _last_request_monotonic
        if elapsed < MIN_REQUEST_INTERVAL_S:
            remaining = MIN_REQUEST_INTERVAL_S - elapsed
            if remaining > MAX_RATE_WAIT_S:
                return False
            time.sleep(remaining)
            now = time.monotonic()
        _last_request_monotonic = now
        return True


def geocode_au_places(
    query: str,
    *,
    limit: int = 5,
    client: httpx.Client | None = None,
    bypass_rate_limit: bool = False,
) -> list[GeocodedPlace]:
    """Query Nominatim bounded to Australia with caching and graceful fail-open behavior."""
    term = " ".join(query.strip().split())
    if len(term) < 3 or limit <= 0:
        return []

    settings = get_settings()
    if client is None and not settings.enable_geocoder:
        return []

    bounded_limit = max(1, min(int(limit), 8))
    cache_key = (term.lower(), bounded_limit)
    now = time.monotonic()

    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached is not None:
            expires_at, places = cached
            if now < expires_at:
                _cache.move_to_end(cache_key)
                return list(places)
            _cache.pop(cache_key, None)

    if not bypass_rate_limit and not _acquire_rate_slot():
        return []

    owns_client = client is None
    http = (
        client
        if client is not None
        else httpx.Client(
            timeout=httpx.Timeout(
                settings.geocoder_timeout_s,
                connect=min(2.0, settings.geocoder_timeout_s),
            ),
            follow_redirects=False,
            headers={
                "User-Agent": GEOCODER_USER_AGENT,
                "Accept": "application/json",
            },
        )
    )
    try:
        params = {
            "q": term,
            "format": "jsonv2",
            "countrycodes": "au",
            "viewbox": AU_VIEWBOX,
            "bounded": "1",
            "addressdetails": "1",
            "limit": str(bounded_limit),
        }
        with http.stream(
            "GET",
            settings.geocoder_url,
            params=params,
            headers={"User-Agent": GEOCODER_USER_AGENT, "Accept": "application/json"},
        ) as resp:
            resp.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > MAX_GEOCODER_BODY_BYTES:
                    logger.warning(
                        "Geocoder response exceeded %d bytes; ignoring",
                        MAX_GEOCODER_BODY_BYTES,
                    )
                    return []
                chunks.append(chunk)
            payload = httpx.Response(
                status_code=resp.status_code,
                content=b"".join(chunks),
                request=resp.request,
            ).json()

        places = _parse_nominatim_items(payload, limit=bounded_limit)
    except Exception as exc:
        logger.debug("AU place geocoder fallback unavailable for %r: %s", term, exc)
        return []
    finally:
        if owns_client:
            http.close()

    with _cache_lock:
        _cache[cache_key] = (time.monotonic() + CACHE_TTL_SECONDS, list(places))
        _cache.move_to_end(cache_key)
        while len(_cache) > CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)

    return places
