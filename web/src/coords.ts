import { AUSTRALIA_BBOX, VIC_NSW_LAUNCH_BBOX } from "./presets";
import type {
  ParsedCoordinate,
  ParsedLatLon,
  ParsedMgrs,
  UtmZoneInfo,
} from "./types";

export const MIN_UTM_LAT = -80.0;
export const MAX_UTM_LAT = 84.0;

// WGS84 ellipsoid parameters (exact NGA/EPSG:4326)
const WGS84_A = 6378137.0;
const WGS84_F = 1 / 298.257223563;
const WGS84_E2 = 2 * WGS84_F - WGS84_F * WGS84_F;
const WGS84_E4 = WGS84_E2 * WGS84_E2;
const WGS84_E6 = WGS84_E4 * WGS84_E2;
const WGS84_EP2 = WGS84_E2 / (1 - WGS84_E2);
const UTM_K0 = 0.9996;
const DEG_TO_RAD = Math.PI / 180;
const RAD_TO_DEG = 180 / Math.PI;

// 20 MGRS latitude bands from 80°S to 84°N (omits I and O; X spans 72°N..84°N).
const MGRS_LAT_BANDS = "CDEFGHJKLMNPQRSTUVWX";

// NGA AA scheme 100 km square letter sets for WGS84/GRS80
const MGRS_COL_SETS = ["ABCDEFGH", "JKLMNPQR", "STUVWXYZ"] as const;
const MGRS_ROW_SETS = [
  "ABCDEFGHJKLMNPQRSTUV", // odd UTM zones
  "FGHJKLMNPQRSTUVABCDE", // even UTM zones
] as const;

const MGRS_UTM_RE =
  /^(0?[1-9]|[1-5]\d|60)([C-HJ-NP-X])([A-HJ-NP-Z]{2})(\d{0,10})$/;
const MGRS_UPS_RE = /^[ABYZ][A-HJ-NP-Z]{2}\d{0,10}$/;

const LAT_LON_COMMA_RE =
  /^\s*([-+]?\d+(?:\.\d+)?)\s*°?\s*([NS])?\s*,\s*([-+]?\d+(?:\.\d+)?)\s*°?\s*([EW])?\s*$/i;
const LAT_LON_SPACE_RE =
  /^\s*([-+]?\d+(?:\.\d+)?)\s*°?\s*([NS])?\s+([-+]?\d+(?:\.\d+)?)\s*°?\s*([EW])?\s*$/i;

function applyHemisphere(value: number, hemi: string | undefined): number {
  if (!hemi) return value;
  const upper = hemi.toUpperCase();
  if (upper === "S" || upper === "W") return -Math.abs(value);
  return Math.abs(value);
}

/**
 * Parse a decimal latitude/longitude pair such as:
 * - "-36.615, 148.125"
 * - "-36.615 148.125"
 * - "36.615°S, 148.125°E"
 */
export function parseLatLon(
  value: string
): { lat: number; lon: number } | null {
  const trimmed = value.trim();
  if (!trimmed) return null;

  const match = LAT_LON_COMMA_RE.exec(trimmed) ?? LAT_LON_SPACE_RE.exec(trimmed);
  if (!match) return null;

  const rawLatStr = match[1];
  const latHemi = match[2];
  const rawLonStr = match[3];
  const lonHemi = match[4];
  if (!rawLatStr || !rawLonStr) return null;

  // Reject plain integer space-separated pairs without hemisphere or decimal
  // (e.g. "4 4" or "100 200") unless comma-separated or hemisphere-tagged.
  const hasComma = trimmed.includes(",");
  if (
    !hasComma &&
    !latHemi &&
    !lonHemi &&
    !rawLatStr.includes(".") &&
    !rawLonStr.includes(".")
  ) {
    return null;
  }

  const rawLat = Number(rawLatStr);
  const rawLon = Number(rawLonStr);
  if (!Number.isFinite(rawLat) || !Number.isFinite(rawLon)) return null;

  // Conflicting sign + hemisphere (e.g. "-36.5N") is invalid
  if ((latHemi && rawLat < 0) || (lonHemi && rawLon < 0)) {
    return null;
  }

  const lat = applyHemisphere(rawLat, latHemi);
  const lon = applyHemisphere(rawLon, lonHemi);

  if (lat < -90 || lat > 90 || lon < -180 || lon > 180) {
    return null;
  }

  return { lat, lon };
}

/**
 * Resolve UTM zone, hemisphere, EPSG code, and MGRS Grid Zone Designation (GZD).
 * Mirrors GridForge's `resolve_utm_zone`, including Norway (32V) and Svalbard exceptions.
 */
export function resolveUtmZone(lat: number, lon: number): UtmZoneInfo {
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
    throw new RangeError(`Non-finite coordinates: lat=${lat}, lon=${lon}`);
  }
  if (lat < MIN_UTM_LAT || lat > MAX_UTM_LAT) {
    throw new RangeError(
      `Latitude ${lat} is outside UTM/MGRS bounds [${MIN_UTM_LAT}, ${MAX_UTM_LAT}].`
    );
  }
  if (lon < -180 || lon > 180) {
    throw new RangeError(`Longitude ${lon} is outside [-180, 180].`);
  }

  let zone =
    lon === 180 ? 60 : Math.max(1, Math.min(60, Math.floor((lon + 180) / 6) + 1));

  // Norway exception (band V, 3°E..12°E -> zone 32)
  if (lat >= 56 && lat < 64 && lon >= 3 && lon < 12) {
    zone = 32;
  } else if (lat >= 72 && lat <= 84) {
    // Svalbard exceptions (band X)
    if (lon >= 0 && lon < 9) zone = 31;
    else if (lon >= 9 && lon < 21) zone = 33;
    else if (lon >= 21 && lon < 33) zone = 35;
    else if (lon >= 33 && lon < 42) zone = 37;
  }

  const bandIdx = Math.min(
    MGRS_LAT_BANDS.length - 1,
    Math.max(0, Math.floor((lat + 80) / 8))
  );
  const band = MGRS_LAT_BANDS[bandIdx] ?? "H";
  const hemisphere: "N" | "S" = lat >= 0 ? "N" : "S";
  const epsg = (hemisphere === "N" ? 32600 : 32700) + zone;

  return {
    zone,
    hemisphere,
    epsg,
    gzd: `${zone}${band}`,
  };
}

function latLonToUtm(
  lat: number,
  lon: number
): {
  easting: number;
  northing: number;
  zone: number;
  hemisphere: "N" | "S";
  band: string;
  gzd: string;
} {
  const info = resolveUtmZone(lat, lon);
  const phi = lat * DEG_TO_RAD;
  const lambda = lon * DEG_TO_RAD;
  const lambda0 = ((info.zone - 1) * 6 - 180 + 3) * DEG_TO_RAD;

  const sinPhi = Math.sin(phi);
  const cosPhi = Math.cos(phi);
  const tanPhi = Math.tan(phi);

  const n = WGS84_A / Math.sqrt(1 - WGS84_E2 * sinPhi * sinPhi);
  const t = tanPhi * tanPhi;
  const c = WGS84_EP2 * cosPhi * cosPhi;
  const a = cosPhi * (lambda - lambda0);

  const m =
    WGS84_A *
    ((1 - WGS84_E2 / 4 - (3 * WGS84_E4) / 64 - (5 * WGS84_E6) / 256) * phi -
      ((3 * WGS84_E2) / 8 + (3 * WGS84_E4) / 32 + (45 * WGS84_E6) / 1024) *
        Math.sin(2 * phi) +
      ((15 * WGS84_E4) / 256 + (45 * WGS84_E6) / 1024) * Math.sin(4 * phi) -
      ((35 * WGS84_E6) / 3072) * Math.sin(6 * phi));

  const a2 = a * a;
  const a3 = a2 * a;
  const a4 = a2 * a2;
  const a5 = a4 * a;
  const a6 = a4 * a2;

  const easting =
    UTM_K0 *
      n *
      (a +
        ((1 - t + c) * a3) / 6 +
        ((5 - 18 * t + t * t + 72 * c - 58 * WGS84_EP2) * a5) / 120) +
    500000.0;

  let northing =
    UTM_K0 *
    (m +
      n *
        tanPhi *
        (a2 / 2 +
          ((5 - t + 9 * c + 4 * c * c) * a4) / 24 +
          ((61 - 58 * t + t * t + 600 * c - 330 * WGS84_EP2) * a6) / 720));

  if (lat < 0) {
    northing += 10000000.0;
  }

  return {
    easting,
    northing,
    zone: info.zone,
    hemisphere: info.hemisphere,
    band: info.gzd.slice(-1),
    gzd: info.gzd,
  };
}

function utmToLatLon(
  easting: number,
  northing: number,
  zone: number,
  isSouthern: boolean
): { lat: number; lon: number } {
  const x = easting - 500000.0;
  const y = isSouthern ? northing - 10000000.0 : northing;

  const m = y / UTM_K0;
  const mu =
    m /
    (WGS84_A *
      (1 - WGS84_E2 / 4 - (3 * WGS84_E4) / 64 - (5 * WGS84_E6) / 256));

  const sqrtOneMinusE2 = Math.sqrt(1 - WGS84_E2);
  const e1 = (1 - sqrtOneMinusE2) / (1 + sqrtOneMinusE2);
  const e1_2 = e1 * e1;
  const e1_3 = e1_2 * e1;
  const e1_4 = e1_2 * e1_2;

  const phi1 =
    mu +
    ((3 * e1) / 2 - (27 * e1_3) / 32) * Math.sin(2 * mu) +
    ((21 * e1_2) / 16 - (55 * e1_4) / 32) * Math.sin(4 * mu) +
    ((151 * e1_3) / 96) * Math.sin(6 * mu) +
    ((1097 * e1_4) / 512) * Math.sin(8 * mu);

  const sin1 = Math.sin(phi1);
  const cos1 = Math.cos(phi1);
  const tan1 = Math.tan(phi1);

  const sin1Sq = sin1 * sin1;
  const n1 = WGS84_A / Math.sqrt(1 - WGS84_E2 * sin1Sq);
  const t1 = tan1 * tan1;
  const c1 = WGS84_EP2 * cos1 * cos1;
  const r1 = (WGS84_A * (1 - WGS84_E2)) / Math.pow(1 - WGS84_E2 * sin1Sq, 1.5);
  const d = x / (n1 * UTM_K0);

  const d2 = d * d;
  const d3 = d2 * d;
  const d4 = d2 * d2;
  const d5 = d4 * d;
  const d6 = d4 * d2;

  const latRad =
    phi1 -
    ((n1 * tan1) / r1) *
      (d2 / 2 -
        ((5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * WGS84_EP2) * d4) / 24 +
        ((61 +
          90 * t1 +
          298 * c1 +
          45 * t1 * t1 -
          252 * WGS84_EP2 -
          3 * c1 * c1) *
          d6) /
          720);

  const lambda0 = ((zone - 1) * 6 - 180 + 3) * DEG_TO_RAD;
  const lonRad =
    lambda0 +
    (d -
      ((1 + 2 * t1 + c1) * d3) / 6 +
      ((5 -
        2 * c1 +
        28 * t1 -
        3 * c1 * c1 +
        8 * WGS84_EP2 +
        24 * t1 * t1) *
        d5) /
        120) /
      cos1;

  return {
    lat: latRad * RAD_TO_DEG,
    lon: lonRad * RAD_TO_DEG,
  };
}

/**
 * Format an MGRS string with readable tactical spacing: e.g. "55H FV 0064 4755".
 */
export function formatMgrsString(
  gzd: string,
  squareId: string,
  digits: string
): string {
  if (!digits) return `${gzd} ${squareId}`;
  const half = digits.length / 2;
  return `${gzd} ${squareId} ${digits.slice(0, half)} ${digits.slice(half)}`;
}

/**
 * Encode WGS84 (lat, lon) to a canonical MGRS string completely offline.
 * @param precision Digits per axis: 5 = 1 m, 4 = 10 m (default), 3 = 100 m, 2 = 1 km, 1 = 10 km, 0 = 100 km.
 */
export function latLonToMgrs(
  lat: number,
  lon: number,
  precision = 4,
  spaced = true
): string {
  const clampedPrecision = Math.max(0, Math.min(5, Math.floor(precision)));
  const utm = latLonToUtm(lat, lon);

  const colSet = MGRS_COL_SETS[(utm.zone - 1) % 3]!;
  const rowSet = MGRS_ROW_SETS[(utm.zone - 1) % 2]!;

  const colIdx = Math.floor(utm.easting / 100000) - 1;
  const rowIdx = Math.floor((utm.northing % 2000000) / 100000);
  const colLetter = colSet[colIdx];
  const rowLetter = rowSet[rowIdx];

  if (!colLetter || !rowLetter) {
    throw new RangeError(
      `UTM coordinate (${utm.easting.toFixed(1)}, ${utm.northing.toFixed(1)}) out of 100 km MGRS square range.`
    );
  }

  const squareId = `${colLetter}${rowLetter}`;
  if (clampedPrecision === 0) {
    return spaced ? `${utm.gzd} ${squareId}` : `${utm.gzd}${squareId}`;
  }

  const divisor = Math.pow(10, 5 - clampedPrecision);
  const eastRem = Math.floor((utm.easting % 100000) / divisor);
  const northRem = Math.floor((utm.northing % 100000) / divisor);
  const maxVal = Math.pow(10, clampedPrecision) - 1;
  const eStr = String(Math.min(maxVal, Math.max(0, eastRem))).padStart(
    clampedPrecision,
    "0"
  );
  const nStr = String(Math.min(maxVal, Math.max(0, northRem))).padStart(
    clampedPrecision,
    "0"
  );

  return spaced
    ? `${utm.gzd} ${squareId} ${eStr} ${nStr}`
    : `${utm.gzd}${squareId}${eStr}${nStr}`;
}

/**
 * Parse and convert a UTM-domain MGRS string (case & whitespace tolerant)
 * to WGS84 (lat, lon) at the grid cell center, completely offline.
 */
export function parseMgrs(value: string): ParsedMgrs | null {
  const cleaned = value.trim();
  if (!cleaned) return null;

  const normalized = cleaned.replace(/\s+/g, "").toUpperCase();
  if (MGRS_UPS_RE.test(normalized)) {
    return null;
  }

  const match = MGRS_UTM_RE.exec(normalized);
  if (!match) return null;

  const zoneStr = match[1];
  const band = match[2];
  const squareId = match[3];
  const digits = match[4] ?? "";
  if (!zoneStr || !band || !squareId) return null;

  if (digits.length % 2 !== 0) return null;

  const zone = Number(zoneStr);
  if (band === "X" && (zone === 32 || zone === 34 || zone === 36)) {
    return null;
  }

  const colSet = MGRS_COL_SETS[(zone - 1) % 3]!;
  const rowSet = MGRS_ROW_SETS[(zone - 1) % 2]!;
  const colIdx = colSet.indexOf(squareId[0]!);
  const rowIdx = rowSet.indexOf(squareId[1]!);
  if (colIdx < 0 || rowIdx < 0) return null;

  const half = digits.length / 2;
  const precisionMeters = Math.pow(10, 5 - half);
  const eastOffset =
    half > 0 ? Number(digits.slice(0, half)) * precisionMeters : 0;
  const northOffset =
    half > 0 ? Number(digits.slice(half)) * precisionMeters : 0;

  const easting = (colIdx + 1) * 100000 + eastOffset + precisionMeters / 2;
  const isSouthern = band < "N";

  const bandIdx = MGRS_LAT_BANDS.indexOf(band);
  if (bandIdx < 0) return null;
  const bandMinLat = -80 + bandIdx * 8;
  const bandMaxLat = band === "X" ? 84 : bandMinLat + 8;

  // Resolve the unique 2,000,000 m northing cycle matching the latitude band.
  // Consecutive cycles are ~18° apart while bands are 8° (12° for X), so at most
  // one cycle overlaps the band.
  let bestCoord: { lat: number; lon: number } | null = null;
  let overlapFallback: { lat: number; lon: number } | null = null;

  for (let cycle = 0; cycle <= 5; cycle += 1) {
    const baseNorthing = cycle * 2000000 + rowIdx * 100000 + northOffset;
    const centerNorthing = baseNorthing + precisionMeters / 2;
    const coord = utmToLatLon(easting, centerNorthing, zone, isSouthern);

    if (
      !Number.isFinite(coord.lat) ||
      !Number.isFinite(coord.lon) ||
      coord.lat < MIN_UTM_LAT ||
      coord.lat > MAX_UTM_LAT ||
      coord.lon < -180 ||
      coord.lon > 180
    ) {
      continue;
    }

    if (coord.lat >= bandMinLat - 1e-6 && coord.lat <= bandMaxLat + 1e-6) {
      bestCoord = coord;
      break;
    }

    // For coarse 100 km / 10 km squares straddling a band boundary, accept overlap.
    if (precisionMeters >= 10000) {
      const southEdge = utmToLatLon(easting, baseNorthing, zone, isSouthern);
      const northEdge = utmToLatLon(
        easting,
        baseNorthing + precisionMeters,
        zone,
        isSouthern
      );
      if (southEdge.lat <= bandMaxLat && northEdge.lat >= bandMinLat) {
        overlapFallback = coord;
      }
    }
  }

  const resolved = bestCoord ?? overlapFallback;
  if (!resolved) return null;

  // Sanity-check longitude against the zone's central meridian (allowing Svalbard width)
  const centralMeridian = (zone - 1) * 6 - 180 + 3;
  if (Math.abs(resolved.lon - centralMeridian) > 9.0) {
    return null;
  }

  const gzd = `${zone}${band}`;
  const canonicalMgrs = `${gzd}${squareId}${digits}`;

  return {
    kind: "mgrs",
    lat: resolved.lat,
    lon: resolved.lon,
    mgrs: canonicalMgrs,
    formatted: formatMgrsString(gzd, squareId, digits),
    gzd,
    zone,
    band,
    squareId,
    precisionMeters,
  };
}

export function isValidMgrs(value: string): boolean {
  return parseMgrs(value) !== null;
}

/**
 * Unified coordinate parser for the TrailForge search bar.
 * Accepts decimal lat/lon pairs or MGRS strings, returning null for free-text queries.
 */
export function parseSearchCoordinate(value: string): ParsedCoordinate | null {
  const latLon = parseLatLon(value);
  if (latLon) {
    const parsed: ParsedLatLon = {
      kind: "latlon",
      lat: latLon.lat,
      lon: latLon.lon,
      canonical: formatCoords(latLon.lat, latLon.lon),
    };
    return parsed;
  }
  return parseMgrs(value);
}

export function looksLikeCoordOrMgrs(value: string): boolean {
  return parseSearchCoordinate(value) !== null;
}

export function formatCoords(lat: number, lon: number, decimals = 5): string {
  return `${lat.toFixed(decimals)}, ${lon.toFixed(decimals)}`;
}

export function isWithinVicNswRegion(lat: number, lon: number): boolean {
  const [west, south, east, north] = VIC_NSW_LAUNCH_BBOX;
  return lon >= west && lon <= east && lat >= south && lat <= north;
}

export function isWithinAustralia(lat: number, lon: number): boolean {
  const [west, south, east, north] = AUSTRALIA_BBOX;
  return lon >= west && lon <= east && lat >= south && lat <= north;
}
