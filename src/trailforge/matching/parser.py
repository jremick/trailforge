"""GPX 1.1 and JSON telemetry trace parsers for Slice 06 ingestion.

Parses GPX XML (<trk>/<trkseg>/<trkpt> with fallback to <rte>/<rtept>) and
JSON telemetry payloads into validated ``GpsPoint`` sequences while rejecting
malformed XML, XML entity expansion payloads, non-finite coordinates, and
degenerate (<2 point) traces.
"""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from pydantic import BaseModel, Field

from trailforge.matching.protocol import GpsPoint

MAX_TRACE_POINTS: Final[int] = 50_000
MAX_IMU_SAMPLES: Final[int] = 100_000

_UNSAFE_XML_PATTERN = re.compile(r"<!(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


class ParsedTrace(BaseModel):
    """Validated trace name and chronologically ordered GPS point sequence."""

    name: str | None = None
    points: list[GpsPoint] = Field(..., min_length=2, max_length=MAX_TRACE_POINTS)


def _local_tag(tag: str) -> str:
    """Strip optional XML namespace URI from an ElementTree tag name."""
    if "}" in tag:
        return tag.rsplit("}", 1)[1].lower()
    return tag.lower()


def _parse_finite_float(raw: Any, field_name: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid numeric value for {field_name}: {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"Non-finite numeric value for {field_name}: {raw!r}")
    return value


def _parse_optional_nonneg_float(raw: Any, field_name: str) -> float | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    value = _parse_finite_float(raw, field_name)
    if value < 0.0:
        raise ValueError(f"{field_name} must be non-negative, got {value}")
    return value


def _parse_timestamp(raw: Any) -> datetime:
    """Parse ISO-8601 string, datetime, or Unix epoch (s or ms) into UTC datetime."""
    if isinstance(raw, datetime):
        return raw.astimezone(UTC) if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        val = _parse_finite_float(raw, "timestamp")
        if abs(val) > 1e11:
            val /= 1000.0
        return datetime.fromtimestamp(val, tz=UTC)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise ValueError("Empty timestamp string")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"Invalid ISO-8601 timestamp: {raw!r}") from exc
        return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    raise ValueError(f"Unsupported timestamp type: {type(raw).__name__}")


_RawPointTuple = tuple[
    float,
    float,
    datetime | None,
    float | None,
    float | None,
    float | None,
    float | None,
]


def _finalize_points(
    raw_entries: list[_RawPointTuple],
    *,
    fallback_start: datetime | None = None,
) -> list[GpsPoint]:
    """Validate bounds, backfill missing timestamps monotonically, sort & dedupe."""
    if len(raw_entries) < 2:
        raise ValueError("Trace must contain at least 2 valid GPS points")

    base_ts = fallback_start or datetime.now(UTC).replace(microsecond=0)
    # If any entry has a real timestamp, anchor missing leading timestamps to it
    first_real_idx = next((i for i, row in enumerate(raw_entries) if row[2] is not None), None)
    if first_real_idx is not None:
        first_ts = raw_entries[first_real_idx][2]
        assert first_ts is not None
        base_ts = first_ts - timedelta(seconds=first_real_idx)

    resolved: list[GpsPoint] = []
    last_ts: datetime | None = None
    has_out_of_order = False
    for idx, (lat, lon, ts, elev_m, acc_m, speed_mps, rough_rms) in enumerate(raw_entries):
        if not (-90.0 <= lat <= 90.0):
            raise ValueError(f"Latitude out of range [-90, 90]: {lat}")
        if not (-180.0 <= lon <= 180.0):
            raise ValueError(f"Longitude out of range [-180, 180]: {lon}")
        if ts is None:
            ts = (last_ts + timedelta(seconds=1)) if last_ts is not None else (
                base_ts + timedelta(seconds=idx)
            )
        if last_ts is not None and ts < last_ts:
            has_out_of_order = True
        last_ts = ts
        resolved.append(
            GpsPoint(
                lat=lat,
                lon=lon,
                timestamp=ts,
                elev_m=elev_m,
                accuracy_m=acc_m,
                speed_mps=speed_mps,
                roughness_rms=rough_rms,
            )
        )

    if has_out_of_order:
        resolved.sort(key=lambda pt: pt.timestamp)

    # Remove exact consecutive duplicate fixes (same coords and timestamp)
    deduped: list[GpsPoint] = [resolved[0]]
    for pt in resolved[1:]:
        prev = deduped[-1]
        if pt.lat == prev.lat and pt.lon == prev.lon and pt.timestamp == prev.timestamp:
            continue
        deduped.append(pt)

    if len(deduped) < 2:
        raise ValueError("Trace must contain at least 2 distinct GPS points")
    return deduped


def _extract_extensions(pt_el: ET.Element) -> tuple[float | None, float | None, float | None]:
    """Extract optional (accuracy_m, speed_mps, roughness_rms) from GPX <extensions>."""
    accuracy_m: float | None = None
    speed_mps: float | None = None
    roughness_rms: float | None = None

    for child in pt_el:
        if _local_tag(child.tag) != "extensions":
            continue
        for node in child.iter():
            tag = _local_tag(node.tag)
            text = (node.text or "").strip()
            if not text:
                continue
            if tag in ("accuracy_m", "accuracy", "hacc") and accuracy_m is None:
                accuracy_m = _parse_optional_nonneg_float(text, tag)
            elif tag in ("speed_mps", "speed") and speed_mps is None:
                speed_mps = _parse_optional_nonneg_float(text, tag)
            elif tag in ("roughness_rms", "roughness") and roughness_rms is None:
                roughness_rms = _parse_optional_nonneg_float(text, tag)
    return accuracy_m, speed_mps, roughness_rms


def parse_gpx(raw: bytes | str) -> ParsedTrace:
    """Parse GPX 1.1 / 1.0 XML bytes or string into a validated ``ParsedTrace``."""
    if isinstance(raw, bytes):
        try:
            xml_text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("GPX payload is not valid UTF-8") from exc
    elif isinstance(raw, str):
        xml_text = raw.lstrip("\ufeff")
    else:
        raise ValueError(f"Expected bytes or str for GPX input, got {type(raw).__name__}")

    if not xml_text.strip():
        raise ValueError("GPX payload is empty")
    if _UNSAFE_XML_PATTERN.search(xml_text):
        raise ValueError("XML DOCTYPE/ENTITY declarations are not permitted in GPX input")

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"Malformed GPX XML: {exc}") from exc

    if _local_tag(root.tag) != "gpx":
        raise ValueError(f"Expected <gpx> root element, got <{root.tag}>")

    meta_name: str | None = None
    meta_time: datetime | None = None
    trk_name: str | None = None
    rte_name: str | None = None
    trkpts: list[ET.Element] = []
    rtepts: list[ET.Element] = []

    for child in root:
        tag = _local_tag(child.tag)
        if tag == "metadata":
            for m_el in child:
                m_tag = _local_tag(m_el.tag)
                m_text = (m_el.text or "").strip()
                if m_tag == "name" and m_text and meta_name is None:
                    meta_name = m_text
                elif m_tag == "time" and m_text and meta_time is None:
                    meta_time = _parse_timestamp(m_text)
        elif tag == "trk":
            for t_el in child:
                t_tag = _local_tag(t_el.tag)
                if t_tag == "name" and (t_el.text or "").strip() and trk_name is None:
                    trk_name = (t_el.text or "").strip()
                elif t_tag == "trkseg":
                    for pt_el in t_el:
                        if _local_tag(pt_el.tag) == "trkpt":
                            if len(trkpts) >= MAX_TRACE_POINTS:
                                raise ValueError(
                                    f"GPX trace exceeds maximum allowed {MAX_TRACE_POINTS} points"
                                )
                            trkpts.append(pt_el)
        elif tag == "rte":
            for r_el in child:
                r_tag = _local_tag(r_el.tag)
                if r_tag == "name" and (r_el.text or "").strip() and rte_name is None:
                    rte_name = (r_el.text or "").strip()
                elif r_tag == "rtept":
                    if len(rtepts) >= MAX_TRACE_POINTS:
                        raise ValueError(
                            f"GPX trace exceeds maximum allowed {MAX_TRACE_POINTS} points"
                        )
                    rtepts.append(r_el)

    selected_pts = trkpts if trkpts else rtepts
    if len(selected_pts) < 2:
        raise ValueError("GPX trace must contain at least 2 trackpoints (<trkpt> or <rtept>)")
    if len(selected_pts) > MAX_TRACE_POINTS:
        raise ValueError(f"GPX trace exceeds maximum allowed {MAX_TRACE_POINTS} points")

    raw_entries: list[
        tuple[float, float, datetime | None, float | None, float | None, float | None, float | None]
    ] = []
    for pt_el in selected_pts:
        if "lat" not in pt_el.attrib or "lon" not in pt_el.attrib:
            raise ValueError("GPX trackpoint is missing required 'lat' or 'lon' attribute")
        lat = _parse_finite_float(pt_el.attrib["lat"], "lat")
        lon = _parse_finite_float(pt_el.attrib["lon"], "lon")

        elev_m: float | None = None
        ts: datetime | None = None
        hdop_acc: float | None = None
        direct_speed: float | None = None

        for sub in pt_el:
            s_tag = _local_tag(sub.tag)
            s_text = (sub.text or "").strip()
            if not s_text:
                continue
            if s_tag == "ele":
                elev_m = _parse_finite_float(s_text, "ele")
            elif s_tag == "time":
                ts = _parse_timestamp(s_text)
            elif s_tag == "hdop":
                hdop_val = _parse_optional_nonneg_float(s_text, "hdop")
                if hdop_val is not None:
                    hdop_acc = round(hdop_val * 5.0, 2)
            elif s_tag == "speed":
                direct_speed = _parse_optional_nonneg_float(s_text, "speed")

        ext_acc, ext_speed, ext_rough = _extract_extensions(pt_el)
        accuracy_m = ext_acc if ext_acc is not None else hdop_acc
        speed_mps = ext_speed if ext_speed is not None else direct_speed
        raw_entries.append((lat, lon, ts, elev_m, accuracy_m, speed_mps, ext_rough))

    points = _finalize_points(raw_entries, fallback_start=meta_time)
    return ParsedTrace(name=trk_name or rte_name or meta_name, points=points)


def parse_telemetry_json(
    payload: Mapping[str, Any] | Sequence[Mapping[str, Any]] | str | bytes,
) -> ParsedTrace:
    """Parse a JSON telemetry trace payload into a validated ``ParsedTrace``.

    Accepts:
      - ``{"name"?: str, "points": [{"lon", "lat", "timestamp"?, "elev_m"?, ...}]}``
      - ``[{"lon", "lat", ...}, ...]``
      - GeoJSON ``LineString`` or ``Feature`` with ``LineString`` geometry
      - Raw JSON ``str`` or ``bytes`` encoding any of the above.
    """
    if isinstance(payload, (str, bytes)):
        try:
            decoded: Any = json.loads(
                payload.decode("utf-8-sig") if isinstance(payload, bytes) else payload
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Malformed telemetry JSON: {exc}") from exc
    else:
        decoded = payload

    trace_name: str | None = None
    point_items: Sequence[Any]

    if isinstance(decoded, Mapping):
        if decoded.get("type") == "Feature" and isinstance(decoded.get("geometry"), Mapping):
            props = decoded.get("properties")
            if isinstance(props, Mapping):
                raw_name = props.get("name") or props.get("title")
                trace_name = str(raw_name).strip() or None if raw_name is not None else None
            geom = decoded["geometry"]
            if geom.get("type") != "LineString" or not isinstance(geom.get("coordinates"), list):
                raise ValueError("GeoJSON Feature geometry must be a LineString")
            if len(geom["coordinates"]) > MAX_TRACE_POINTS:
                raise ValueError(
                    f"Telemetry JSON trace exceeds maximum allowed {MAX_TRACE_POINTS} points"
                )
            point_items = [
                {
                    "lon": coord[0],
                    "lat": coord[1],
                    "elev_m": coord[2] if len(coord) >= 3 else None,
                }
                for coord in geom["coordinates"]
                if isinstance(coord, Sequence) and len(coord) >= 2
            ]
        elif decoded.get("type") == "LineString" and isinstance(decoded.get("coordinates"), list):
            if len(decoded["coordinates"]) > MAX_TRACE_POINTS:
                raise ValueError(
                    f"Telemetry JSON trace exceeds maximum allowed {MAX_TRACE_POINTS} points"
                )
            point_items = [
                {
                    "lon": coord[0],
                    "lat": coord[1],
                    "elev_m": coord[2] if len(coord) >= 3 else None,
                }
                for coord in decoded["coordinates"]
                if isinstance(coord, Sequence) and len(coord) >= 2
            ]
        else:
            raw_name = decoded.get("name") or decoded.get("title")
            trace_name = str(raw_name).strip() or None if raw_name is not None else None
            pts = decoded.get("points")
            if pts is None:
                pts = decoded.get("trackpoints")
            if not isinstance(pts, Sequence) or isinstance(pts, (str, bytes)):
                raise ValueError("Telemetry JSON object must contain a 'points' array")
            point_items = pts
    elif isinstance(decoded, Sequence) and not isinstance(decoded, (str, bytes)):
        point_items = decoded
    else:
        raise ValueError("Telemetry JSON must be an object or array of points")

    if len(point_items) < 2:
        raise ValueError("Telemetry JSON trace must contain at least 2 points")
    if len(point_items) > MAX_TRACE_POINTS:
        raise ValueError(
            f"Telemetry JSON trace exceeds maximum allowed {MAX_TRACE_POINTS} points"
        )

    raw_entries: list[
        tuple[float, float, datetime | None, float | None, float | None, float | None, float | None]
    ] = []
    for idx, item in enumerate(point_items):
        if not isinstance(item, Mapping):
            raise ValueError(f"Point at index {idx} must be a JSON object")
        if "lat" not in item or "lon" not in item:
            raise ValueError(f"Point at index {idx} is missing required 'lat' or 'lon'")
        lat = _parse_finite_float(item["lat"], "lat")
        lon = _parse_finite_float(item["lon"], "lon")

        raw_ts = item.get("timestamp")
        if raw_ts is None:
            raw_ts = item.get("time")
        if raw_ts is None:
            raw_ts = item.get("recorded_at")
        ts = _parse_timestamp(raw_ts) if raw_ts is not None else None

        raw_elev = item.get("elev_m")
        if raw_elev is None:
            raw_elev = item.get("ele")
        if raw_elev is None:
            raw_elev = item.get("elevation_m")
        elev_m = _parse_finite_float(raw_elev, "elev_m") if raw_elev is not None else None

        raw_acc = item.get("accuracy_m")
        if raw_acc is None:
            raw_acc = item.get("accuracy")
        accuracy_m = _parse_optional_nonneg_float(raw_acc, "accuracy_m")

        raw_speed = item.get("speed_mps")
        if raw_speed is None:
            raw_speed = item.get("speed")
        speed_mps = _parse_optional_nonneg_float(raw_speed, "speed_mps")

        raw_rough = item.get("roughness_rms")
        if raw_rough is None:
            raw_rough = item.get("roughness")
        roughness_rms = _parse_optional_nonneg_float(raw_rough, "roughness_rms")

        raw_entries.append((lat, lon, ts, elev_m, accuracy_m, speed_mps, roughness_rms))

    points = _finalize_points(raw_entries)
    return ParsedTrace(name=trace_name, points=points)
