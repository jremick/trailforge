import type { FeatureCollection, LineString, Point } from "geojson";
import type { ImuSample, RecordedBreadcrumbPoint } from "../types";

const EARTH_RADIUS_M = 6_371_000;

export function haversineDistanceM(
  lat1: number,
  lon1: number,
  lat2: number,
  lon2: number
): number {
  const toRad = (deg: number): number => (deg * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

/**
 * Initial forward azimuth bearing in degrees [0, 360) from point 1 -> point 2.
 */
export function computeBearingDeg(
  lat1: number,
  lon1: number,
  lat2: number,
  lon2: number
): number {
  const toRad = (deg: number): number => (deg * Math.PI) / 180;
  const phi1 = toRad(lat1);
  const phi2 = toRad(lat2);
  const dLambda = toRad(lon2 - lon1);
  const y = Math.sin(dLambda) * Math.cos(phi2);
  const x =
    Math.cos(phi1) * Math.sin(phi2) -
    Math.sin(phi1) * Math.cos(phi2) * Math.cos(dLambda);
  const theta = (Math.atan2(y, x) * 180) / Math.PI;
  return (theta + 360) % 360;
}

const COMPASS_POINTS = [
  "N",
  "NE",
  "E",
  "SE",
  "S",
  "SW",
  "W",
  "NW",
] as const;

export function formatCompassCardinal(bearingDeg: number): string {
  const normalized = ((bearingDeg % 360) + 360) % 360;
  const idx = Math.round(normalized / 45) % 8;
  return COMPASS_POINTS[idx] ?? "N";
}

/**
 * Compute RMS roughness (in g-force deviation) from DeviceMotion magnitude samples.
 */
export function computeRoughnessRms(accelMagnitudesMps2: readonly number[]): number | null {
  const valid = accelMagnitudesMps2.filter((v) => Number.isFinite(v));
  if (valid.length === 0) return null;
  const meanSq =
    valid.reduce((acc, v) => acc + v * v, 0) / valid.length;
  // Normalize relative to 1 g (9.80665 m/s^2)
  return Math.round((Math.sqrt(meanSq) / 9.80665) * 10000) / 10000;
}

export interface RecorderStats {
  distanceM: number;
  durationS: number;
  currentSpeedKmh: number;
  avgSpeedKmh: number;
  currentElevM: number | null;
  pointCount: number;
  latestRoughnessRms: number | null;
}

export function computeRecorderStats(
  points: readonly RecordedBreadcrumbPoint[],
  nowMs?: number
): RecorderStats {
  if (points.length === 0) {
    return {
      distanceM: 0,
      durationS: 0,
      currentSpeedKmh: 0,
      avgSpeedKmh: 0,
      currentElevM: null,
      pointCount: 0,
      latestRoughnessRms: null,
    };
  }

  let distanceM = 0;
  for (let i = 1; i < points.length; i += 1) {
    const prev = points[i - 1]!;
    const curr = points[i]!;
    distanceM += haversineDistanceM(prev.lat, prev.lon, curr.lat, curr.lon);
  }

  const first = points[0]!;
  const last = points[points.length - 1]!;
  const startMs = Date.parse(first.timestamp);
  const endMs = nowMs ?? Date.parse(last.timestamp);
  const durationS =
    Number.isFinite(startMs) && Number.isFinite(endMs)
      ? Math.max(0, (endMs - startMs) / 1000)
      : 0;

  let currentSpeedKmh = 0;
  if (typeof last.speed_mps === "number" && Number.isFinite(last.speed_mps)) {
    currentSpeedKmh = Math.max(0, last.speed_mps * 3.6);
  } else if (points.length >= 2) {
    const prev = points[points.length - 2]!;
    const segM = haversineDistanceM(prev.lat, prev.lon, last.lat, last.lon);
    const dtS = (Date.parse(last.timestamp) - Date.parse(prev.timestamp)) / 1000;
    if (Number.isFinite(dtS) && dtS > 0.2) {
      currentSpeedKmh = Math.min(140, (segM / dtS) * 3.6);
    }
  }

  const avgSpeedKmh = durationS > 1 ? (distanceM / durationS) * 3.6 : 0;

  return {
    distanceM: Math.round(distanceM * 10) / 10,
    durationS: Math.round(durationS),
    currentSpeedKmh: Math.round(currentSpeedKmh * 10) / 10,
    avgSpeedKmh: Math.round(avgSpeedKmh * 10) / 10,
    currentElevM: last.elev_m ?? null,
    pointCount: points.length,
    latestRoughnessRms: last.roughness_rms ?? null,
  };
}

export interface BacktrackGuidance {
  active: boolean;
  reversedPoints: RecordedBreadcrumbPoint[];
  returnDistanceM: number;
  directToStartM: number;
  nextWaypointBearingDeg: number | null;
  nextWaypointCardinal: string;
  startBearingDeg: number | null;
  startCardinal: string;
}

/**
 * Reverse recorded breadcrumb points into a backtrack return path and compute live
 * return bearing + distance back along the trail and direct to the start point.
 */
export function buildBacktrackGuidance(
  points: readonly RecordedBreadcrumbPoint[],
  currentPos?: { lat: number; lon: number } | null
): BacktrackGuidance {
  if (points.length < 2) {
    return {
      active: false,
      reversedPoints: [...points].reverse(),
      returnDistanceM: 0,
      directToStartM: 0,
      nextWaypointBearingDeg: null,
      nextWaypointCardinal: "—",
      startBearingDeg: null,
      startCardinal: "—",
    };
  }

  const reversed = [...points].reverse();
  let returnDistanceM = 0;
  for (let i = 1; i < reversed.length; i += 1) {
    returnDistanceM += haversineDistanceM(
      reversed[i - 1]!.lat,
      reversed[i - 1]!.lon,
      reversed[i]!.lat,
      reversed[i]!.lon
    );
  }

  const origin = currentPos ?? { lat: reversed[0]!.lat, lon: reversed[0]!.lon };
  const startPt = reversed[reversed.length - 1]!;
  // Pick the first reversed breadcrumb at least 15 m away for stable return heading
  const nextTarget =
    reversed.find(
      (pt, idx) =>
        idx > 0 && haversineDistanceM(origin.lat, origin.lon, pt.lat, pt.lon) >= 15
    ) ?? reversed[1]!;

  const nextBearing = computeBearingDeg(
    origin.lat,
    origin.lon,
    nextTarget.lat,
    nextTarget.lon
  );
  const startBearing = computeBearingDeg(
    origin.lat,
    origin.lon,
    startPt.lat,
    startPt.lon
  );
  const directToStartM = haversineDistanceM(
    origin.lat,
    origin.lon,
    startPt.lat,
    startPt.lon
  );

  return {
    active: true,
    reversedPoints: reversed,
    returnDistanceM: Math.round(returnDistanceM * 10) / 10,
    directToStartM: Math.round(directToStartM * 10) / 10,
    nextWaypointBearingDeg: Math.round(nextBearing),
    nextWaypointCardinal: formatCompassCardinal(nextBearing),
    startBearingDeg: Math.round(startBearing),
    startCardinal: formatCompassCardinal(startBearing),
  };
}

export function buildRecordedTrackGeoJson(
  points: readonly RecordedBreadcrumbPoint[],
  backtrackActive: boolean
): {
  liveLineFc: FeatureCollection<LineString>;
  backtrackLineFc: FeatureCollection<LineString>;
  headPointFc: FeatureCollection<Point>;
} {
  const coords = points.map((p) => [p.lon, p.lat]);
  const liveLineFc: FeatureCollection<LineString> = {
    type: "FeatureCollection",
    features:
      coords.length >= 2
        ? [
            {
              type: "Feature",
              properties: { mode: "live" },
              geometry: { type: "LineString", coordinates: coords },
            },
          ]
        : [],
  };

  const backtrackCoords = backtrackActive ? [...coords].reverse() : [];
  const backtrackLineFc: FeatureCollection<LineString> = {
    type: "FeatureCollection",
    features:
      backtrackCoords.length >= 2
        ? [
            {
              type: "Feature",
              properties: { mode: "backtrack" },
              geometry: { type: "LineString", coordinates: backtrackCoords },
            },
          ]
        : [],
  };

  const last = points[points.length - 1];
  const headPointFc: FeatureCollection<Point> = {
    type: "FeatureCollection",
    features: last
      ? [
          {
            type: "Feature",
            properties: {
              elev_m: last.elev_m ?? null,
              backtrack: backtrackActive,
            },
            geometry: { type: "Point", coordinates: [last.lon, last.lat] },
          },
        ]
      : [],
  };

  return { liveLineFc, backtrackLineFc, headPointFc };
}

export interface SimulatedHighCountryWaypoint {
  lon: number;
  lat: number;
  elev_m: number;
  speed_mps: number;
  roughness_rms: number;
  pitch_deg: number;
  roll_deg: number;
  shock_g?: number;
}

/**
 * Deterministic 22-point High Country breadcrumb drive along the Davies Plain /
 * Tom Groggin corridor (~2.4 km so it cleanly survives the mandatory 250 m
 * start/end privacy trim when uploaded or tested on GPS-less desktops).
 * Includes realistic steep pitch climbs, off-camber creek rolls, and rock-ledge shocks.
 */
export const SIMULATED_HIGH_COUNTRY_WAYPOINTS: readonly SimulatedHighCountryWaypoint[] = [
  { lon: 148.1195, lat: -36.5608, elev_m: 614, speed_mps: 6.2, roughness_rms: 0.14, pitch_deg: 2.1, roll_deg: -1.4 },
  { lon: 148.1202, lat: -36.5620, elev_m: 618, speed_mps: 5.8, roughness_rms: 0.18, pitch_deg: 4.5, roll_deg: 2.8 },
  { lon: 148.1211, lat: -36.5633, elev_m: 625, speed_mps: 5.1, roughness_rms: 0.22, pitch_deg: 6.8, roll_deg: -5.2 },
  { lon: 148.1219, lat: -36.5646, elev_m: 634, speed_mps: 4.5, roughness_rms: 0.29, pitch_deg: 9.4, roll_deg: 7.5 },
  { lon: 148.1225, lat: -36.5659, elev_m: 651, speed_mps: 4.1, roughness_rms: 0.34, pitch_deg: 13.2, roll_deg: -10.8 },
  { lon: 148.1230, lat: -36.5673, elev_m: 676, speed_mps: 3.6, roughness_rms: 0.41, pitch_deg: 16.4, roll_deg: 12.1 },
  { lon: 148.1235, lat: -36.5687, elev_m: 705, speed_mps: 2.9, roughness_rms: 0.52, pitch_deg: 19.5, roll_deg: -16.8, shock_g: 1.95 },
  { lon: 148.1241, lat: -36.5701, elev_m: 738, speed_mps: 2.4, roughness_rms: 0.61, pitch_deg: 23.8, roll_deg: 18.4, shock_g: 2.15 },
  { lon: 148.1248, lat: -36.5715, elev_m: 774, speed_mps: 2.1, roughness_rms: 0.68, pitch_deg: 26.4, roll_deg: -20.2, shock_g: 2.28 },
  { lon: 148.1254, lat: -36.5729, elev_m: 812, speed_mps: 2.6, roughness_rms: 0.55, pitch_deg: 21.2, roll_deg: 15.6, shock_g: 1.92 },
  { lon: 148.1260, lat: -36.5742, elev_m: 848, speed_mps: 3.3, roughness_rms: 0.46, pitch_deg: 17.6, roll_deg: -13.4 },
  { lon: 148.1266, lat: -36.5756, elev_m: 882, speed_mps: 3.9, roughness_rms: 0.38, pitch_deg: 14.8, roll_deg: 9.2 },
  { lon: 148.1271, lat: -36.5770, elev_m: 916, speed_mps: 4.2, roughness_rms: 0.33, pitch_deg: 12.9, roll_deg: -8.1 },
  { lon: 148.1277, lat: -36.5784, elev_m: 952, speed_mps: 3.8, roughness_rms: 0.39, pitch_deg: 15.7, roll_deg: 11.4 },
  { lon: 148.1283, lat: -36.5798, elev_m: 988, speed_mps: 3.5, roughness_rms: 0.44, pitch_deg: 16.9, roll_deg: -14.2, shock_g: 1.98 },
  { lon: 148.1289, lat: -36.5812, elev_m: 1024, speed_mps: 3.1, roughness_rms: 0.49, pitch_deg: 18.2, roll_deg: 16.1 },
  { lon: 148.1296, lat: -36.5825, elev_m: 1058, speed_mps: 3.6, roughness_rms: 0.42, pitch_deg: 13.5, roll_deg: -9.8 },
  { lon: 148.1303, lat: -36.5838, elev_m: 1091, speed_mps: 4.1, roughness_rms: 0.36, pitch_deg: 11.4, roll_deg: 6.5 },
  { lon: 148.1310, lat: -36.5851, elev_m: 1122, speed_mps: 4.4, roughness_rms: 0.31, pitch_deg: 9.8, roll_deg: -5.4 },
  { lon: 148.1318, lat: -36.5864, elev_m: 1148, speed_mps: 4.7, roughness_rms: 0.28, pitch_deg: 8.1, roll_deg: 4.1 },
  { lon: 148.1325, lat: -36.5877, elev_m: 1171, speed_mps: 5.0, roughness_rms: 0.24, pitch_deg: 6.4, roll_deg: -3.2 },
  { lon: 148.1332, lat: -36.5890, elev_m: 1194, speed_mps: 5.3, roughness_rms: 0.21, pitch_deg: 4.8, roll_deg: 2.0 },
];

/**
 * Synthesize a deterministic 50 Hz (20 ms spaced) IMU burst for a simulated High Country
 * waypoint so desktop demos exercise live pitch/roll clinometer alerts, corrugation RMS,
 * and rock-ledge shock event counting end-to-end.
 */
export function synthesizeSimulatedImuBurst(
  wp: SimulatedHighCountryWaypoint,
  baseTimestampMs: number,
  samplesPerStep = 8
): ImuSample[] {
  const count = Math.max(4, Math.floor(samplesPerStep));
  const pitchRad = (wp.pitch_deg * Math.PI) / 180;
  const rollRad = (wp.roll_deg * Math.PI) / 180;

  // Unit gravity vector tilted by pitch & roll
  const gx = Math.sin(rollRad) * Math.cos(pitchRad);
  const gy = Math.sin(pitchRad);
  const gz = Math.cos(pitchRad) * Math.cos(rollRad);

  const samples: ImuSample[] = [];
  const shockIdx = Math.floor(count / 2);

  for (let i = 0; i < count; i += 1) {
    const phase = (i / count) * Math.PI * 2;
    const corrugation = Math.sin(phase * 2) * wp.roughness_rms * 1.15;
    let totalScale = 1.0 + corrugation;
    if (typeof wp.shock_g === "number" && i === shockIdx) {
      totalScale = wp.shock_g;
    }

    samples.push({
      t_ms: Math.round(baseTimestampMs + i * 20),
      ax: Math.round(gx * totalScale * 10000) / 10000,
      ay: Math.round(gy * totalScale * 10000) / 10000,
      az: Math.round(gz * totalScale * 10000) / 10000,
      pitch_deg: wp.pitch_deg,
      roll_deg: wp.roll_deg,
    });
  }

  return samples;
}
