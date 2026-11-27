import type { Feature, FeatureCollection, LineString, Point } from "geojson";
import {
  buildElevationProfileSvg,
  normalizeTrackInspector,
  type ElevationChartOptions,
  type ElevationChartResult,
} from "./inspectorHelpers";
import { getOfflineTrackDetail } from "./offline/packStore";
import { haversineDistanceM } from "./offline/recorderEngine";
import { partitionTrackReports } from "./reportsHelpers";
import type {
  BBoxTuple,
  ClosureItem,
  ConditionReportItem,
  ParkTenureItem,
  PlannedRouteAnalysis,
  PlannedRouteLeg,
  PoiItem,
  RouteElevationPoint,
  RouteLegGap,
  RoutePlanRequest,
  RouteSafetySummary,
  RouteWaypointInput,
  SavedRouteItem,
  TrackInspectorDetail,
  VehicleCapabilityClass,
  WaterwayCrossingItem,
  WetRiskLevel,
} from "./types";
import { computeClientSuitability } from "./vehicleHelpers";

export const MAX_ROUTE_LEGS = 30;
export const LEG_GAP_WARNING_M = 100;

const GRADE_RANK: Record<string, number> = {
  easy: 1,
  medium: 2,
  hard: 3,
  extreme: 4,
};

const GRADE_SPEED_KMH: Record<string, number> = {
  easy: 28,
  medium: 18,
  hard: 11,
  extreme: 7,
};

const WET_RANK: Record<string, number> = {
  dry: 0,
  damp: 1,
  wet: 2,
  heavy_rain: 3,
};

/* ---------- Leg List Management Helpers ---------- */

export function dedupeRouteTrackIds(trackIds: readonly string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of trackIds) {
    const id = raw.trim();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    out.push(id);
  }
  return out;
}

export function addRouteTrackId(
  trackIds: readonly string[],
  trackId: string,
  options?: { allowDuplicate?: boolean; maxLegs?: number }
): {
  trackIds: string[];
  added: boolean;
  reason?: "invalid" | "duplicate" | "limit";
} {
  const clean = trackId.trim();
  const maxLegs = options?.maxLegs ?? MAX_ROUTE_LEGS;
  if (!clean) {
    return { trackIds: [...trackIds], added: false, reason: "invalid" };
  }
  if (!options?.allowDuplicate && trackIds.includes(clean)) {
    return { trackIds: [...trackIds], added: false, reason: "duplicate" };
  }
  if (trackIds.length >= maxLegs) {
    return { trackIds: [...trackIds], added: false, reason: "limit" };
  }
  return { trackIds: [...trackIds, clean], added: true };
}

export function removeRouteTrackAt(
  trackIds: readonly string[],
  index: number
): string[] {
  if (index < 0 || index >= trackIds.length) return [...trackIds];
  return trackIds.filter((_, idx) => idx !== index);
}

export function removeRouteTrackId(
  trackIds: readonly string[],
  trackId: string
): string[] {
  return trackIds.filter((id) => id !== trackId);
}

export function moveRouteTrackUp(
  trackIds: readonly string[],
  index: number
): string[] {
  if (index <= 0 || index >= trackIds.length) return [...trackIds];
  const next = [...trackIds];
  const tmp = next[index - 1]!;
  next[index - 1] = next[index]!;
  next[index] = tmp;
  return next;
}

export function moveRouteTrackDown(
  trackIds: readonly string[],
  index: number
): string[] {
  if (index < 0 || index >= trackIds.length - 1) return [...trackIds];
  const next = [...trackIds];
  const tmp = next[index + 1]!;
  next[index + 1] = next[index]!;
  next[index] = tmp;
  return next;
}

export function reorderRouteTracks(
  trackIds: readonly string[],
  fromIndex: number,
  toIndex: number
): string[] {
  if (
    fromIndex < 0 ||
    fromIndex >= trackIds.length ||
    toIndex < 0 ||
    toIndex >= trackIds.length ||
    fromIndex === toIndex
  ) {
    return [...trackIds];
  }
  const next = [...trackIds];
  const [moved] = next.splice(fromIndex, 1);
  if (moved !== undefined) {
    next.splice(toIndex, 0, moved);
  }
  return next;
}

export function getSavedRouteId(route: SavedRouteItem): string {
  return String(route.id ?? route.route_id ?? "");
}

export function getSavedRouteWaypoints(
  route: SavedRouteItem
): RouteWaypointInput[] {
  return route.waypoints ?? route.waypoints_json ?? [];
}

/* ---------- Route Safety Status Badge Formatting (Priority: Closure > Gate > Wet > Clear) ---------- */

export type RouteSafetyStatusKind = "closure" | "gate" | "wet" | "clear";

export interface RouteSafetyBadgeInfo {
  kind: RouteSafetyStatusKind;
  label: string;
  tone: "critical" | "warning" | "clear";
  icon: string;
  detail: string;
}

export function formatRouteSafetyBadge(
  summary: RouteSafetySummary | null | undefined
): RouteSafetyBadgeInfo {
  if (!summary) {
    return {
      kind: "clear",
      label: "Clear",
      tone: "clear",
      icon: "✓",
      detail: "No official closures, locked gates, or heavy-rain warnings flagged.",
    };
  }

  if (summary.has_blocking_closure || summary.closure_count > 0) {
    const count = Math.max(1, summary.closure_count);
    return {
      kind: "closure",
      label: "Blocked by Closure",
      tone: "critical",
      icon: "⛔",
      detail: `${count} official closure or access alert${count === 1 ? "" : "s"} affecting this route`,
    };
  }

  if (summary.gate_warning_count > 0) {
    const count = summary.gate_warning_count;
    return {
      kind: "gate",
      label: "Gate Warning",
      tone: "warning",
      icon: "🚧",
      detail: `${count} community locked-gate or private-access warning${count === 1 ? "" : "s"} reported`,
    };
  }

  const wet = (summary.wet_risk_level ?? "dry").trim().toLowerCase();
  if (wet === "heavy_rain" || wet === "wet") {
    return {
      kind: "wet",
      label: "Wet Caution",
      tone: "warning",
      icon: "🌧",
      detail:
        wet === "heavy_rain"
          ? "Heavy 72 h rainfall warning along route — expect boggy terrain and elevated creek crossings"
          : "Recent 72 h rainfall makes clay/steep sections slippery",
    };
  }

  const notes: string[] = [];
  if (summary.unbridged_crossings > 0) {
    notes.push(
      `${summary.unbridged_crossings} unbridged crossing${summary.unbridged_crossings === 1 ? "" : "s"}`
    );
  }
  if (summary.active_report_count > 0) {
    notes.push(
      `${summary.active_report_count} community report${summary.active_report_count === 1 ? "" : "s"}`
    );
  }

  return {
    kind: "clear",
    label: "Clear",
    tone: "clear",
    icon: "✓",
    detail:
      notes.length > 0
        ? `No closures or gate blocks (${notes.join(" · ")})`
        : "No official closures, locked gates, or wet warnings flagged.",
  };
}

/* ---------- GridForge Strip-Route CLI Formatter ---------- */

export function slugifyRouteName(name: string): string {
  const slug = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return slug || "trailforge_route";
}

export function formatGridForgeCli(
  routeName: string,
  options?: { scale?: number; paper?: string }
): string {
  const cleanName = routeName.trim() || "TrailForge Planned Route";
  const slug = slugifyRouteName(cleanName);
  const scale = options?.scale ?? 25000;
  const paper = (options?.paper ?? "a4").toLowerCase();
  const safeUpperTitle = cleanName.toUpperCase().replace(/["`$\\]/g, "").trim() || "TRAILFORGE ROUTE";

  return `docker run --rm -v "$(pwd)/out:/out" gridforge:dev gridforge render --strip-route /out/${slug}.gpx --scale ${scale} --paper ${paper} --dem-source au-elvis --au-overlay au-state-trails --elevation-profile --route-card --calibration-page --title "${safeUpperTitle}" --output /out/${slug}_strip_atlas.pdf`;
}

/* ---------- Combined Multi-Track Elevation Profile SVG Builder ---------- */

export interface RouteElevationChartOptions extends ElevationChartOptions {
  legs?: readonly PlannedRouteLeg[];
}

export function buildRouteElevationProfileSvg(
  points: readonly RouteElevationPoint[],
  options?: RouteElevationChartOptions
): ElevationChartResult {
  const width = options?.width ?? 340;
  const height = options?.height ?? 144;
  const base = buildElevationProfileSvg(points, {
    ...options,
    width,
    height,
  });

  if (points.length < 2) {
    return {
      ...base,
      svgHtml: `<div class="tf-elev-empty">Add 1 or more tracks to view the combined route elevation profile.</div>`,
    };
  }

  // Namespace gradient ID so Inspector and Route Planner SVGs never clash in the DOM
  let svgHtml = base.svgHtml
    .replace(/id="tfElevGrad"/g, 'id="tfRouteElevGrad"')
    .replace(/url\(#tfElevGrad\)/g, "url(#tfRouteElevGrad)");

  // Overlay subtle leg boundary dividers when chaining >= 2 tracks
  const legs = options?.legs ?? [];
  const totalDistM = Math.max(1, base.totalDistM);
  if (legs.length > 1) {
    const padL = 38;
    const padR = 10;
    const padT = 12;
    const padB = 22;
    const plotW = width - padL - padR;
    const plotH = height - padT - padB;
    const baseY = padT + plotH;

    let cumDist = 0;
    const dividers: string[] = [];
    for (let i = 0; i < legs.length - 1; i += 1) {
      cumDist += Math.max(0, legs[i]!.length_m);
      if (cumDist <= 0 || cumDist >= totalDistM) continue;
      const x = (padL + (cumDist / totalDistM) * plotW).toFixed(1);
      dividers.push(
        `<line class="tf-route-leg-divider" x1="${x}" y1="${padT}" x2="${x}" y2="${baseY.toFixed(1)}" stroke="rgba(20, 184, 166, 0.55)" stroke-width="1.1" stroke-dasharray="3 2"/>` +
          `<text x="${(Number(x) + 2).toFixed(1)}" y="${padT + 9}" fill="#5EEAD4" font-size="8.5" font-family="ui-monospace, monospace">L${i + 2}</text>`
      );
    }

    if (dividers.length > 0) {
      svgHtml = svgHtml.replace("</svg>", `${dividers.join("")}\n    </svg>`);
    }
  }

  return {
    ...base,
    svgHtml,
  };
}

/* ---------- MapLibre Planned Route GeoJSON Normalization ---------- */

type CoordTuple = number[];

function isCoordPair(value: unknown): value is CoordTuple {
  return (
    Array.isArray(value) &&
    value.length >= 2 &&
    typeof value[0] === "number" &&
    typeof value[1] === "number" &&
    Number.isFinite(value[0]) &&
    Number.isFinite(value[1])
  );
}

function extractLineStringsFromGeometry(
  rawGeometry: unknown,
  legs: readonly PlannedRouteLeg[]
): Feature<LineString>[] {
  if (!rawGeometry || typeof rawGeometry !== "object") return [];
  const obj = rawGeometry as Record<string, unknown>;
  const out: Feature<LineString>[] = [];

  if (obj.type === "FeatureCollection" && Array.isArray(obj.features)) {
    obj.features.forEach((rawFeat, idx) => {
      if (!rawFeat || typeof rawFeat !== "object") return;
      const feat = rawFeat as Record<string, unknown>;
      const geom = feat.geometry as Record<string, unknown> | undefined;
      const props = (feat.properties ?? {}) as Record<string, unknown>;
      const fallbackLeg = legs[idx];
      if (!geom) return;

      if (geom.type === "LineString" && Array.isArray(geom.coordinates)) {
        const coords = (geom.coordinates as unknown[]).filter(isCoordPair);
        if (coords.length >= 2) {
          out.push({
            type: "Feature",
            properties: {
              leg_index:
                typeof props.leg_index === "number"
                  ? props.leg_index
                  : typeof props.index === "number"
                    ? props.index
                    : idx,
              track_id: String(props.track_id ?? fallbackLeg?.track_id ?? ""),
              name: String(props.name ?? fallbackLeg?.name ?? `Leg ${idx + 1}`),
              grade: String(props.grade ?? fallbackLeg?.grade ?? "Easy"),
            },
            geometry: { type: "LineString", coordinates: coords },
          });
        }
      } else if (
        geom.type === "MultiLineString" &&
        Array.isArray(geom.coordinates)
      ) {
        for (const rawLine of geom.coordinates as unknown[]) {
          if (!Array.isArray(rawLine)) continue;
          const coords = rawLine.filter(isCoordPair);
          if (coords.length >= 2) {
            out.push({
              type: "Feature",
              properties: {
                leg_index:
                  typeof props.leg_index === "number" ? props.leg_index : idx,
                track_id: String(props.track_id ?? fallbackLeg?.track_id ?? ""),
                name: String(
                  props.name ?? fallbackLeg?.name ?? `Leg ${idx + 1}`
                ),
                grade: String(props.grade ?? fallbackLeg?.grade ?? "Easy"),
              },
              geometry: { type: "LineString", coordinates: coords },
            });
          }
        }
      }
    });
    return out;
  }

  if (obj.type === "MultiLineString" && Array.isArray(obj.coordinates)) {
    (obj.coordinates as unknown[]).forEach((rawLine, idx) => {
      if (!Array.isArray(rawLine)) return;
      const coords = rawLine.filter(isCoordPair);
      const leg = legs[idx];
      if (coords.length >= 2) {
        out.push({
          type: "Feature",
          properties: {
            leg_index: idx,
            track_id: leg?.track_id ?? "",
            name: leg?.name ?? `Leg ${idx + 1}`,
            grade: leg?.grade ?? "Easy",
          },
          geometry: { type: "LineString", coordinates: coords },
        });
      }
    });
    return out;
  }

  if (obj.type === "LineString" && Array.isArray(obj.coordinates)) {
    const coords = (obj.coordinates as unknown[]).filter(isCoordPair);
    const leg = legs[0];
    if (coords.length >= 2) {
      out.push({
        type: "Feature",
        properties: {
          leg_index: 0,
          track_id: leg?.track_id ?? "",
          name: leg?.name ?? "Planned Route",
          grade: leg?.grade ?? "Easy",
        },
        geometry: { type: "LineString", coordinates: coords },
      });
    }
  }

  return out;
}

export function buildPlannedRouteMapGeoJson(
  analysis: PlannedRouteAnalysis | null
): {
  lineFc: FeatureCollection<LineString>;
  pinsFc: FeatureCollection<Point>;
} {
  if (!analysis) {
    return {
      lineFc: { type: "FeatureCollection", features: [] },
      pinsFc: { type: "FeatureCollection", features: [] },
    };
  }

  const lineFeatures = extractLineStringsFromGeometry(
    analysis.geometry,
    analysis.legs ?? []
  );
  const pinFeatures: Feature<Point>[] = [];

  if (lineFeatures.length > 0) {
    const firstLine = lineFeatures[0]!;
    const lastLine = lineFeatures[lineFeatures.length - 1]!;
    const startCoord = firstLine.geometry.coordinates[0]!;
    const endCoord =
      lastLine.geometry.coordinates[lastLine.geometry.coordinates.length - 1]!;

    pinFeatures.push({
      type: "Feature",
      properties: {
        kind: "start",
        label: "S",
        pin_key: "start-S",
        title: `Start · ${analysis.legs[0]?.name ?? analysis.name}`,
      },
      geometry: { type: "Point", coordinates: [startCoord[0]!, startCoord[1]!] },
    });

    // Numbered leg transition pins for multi-track routes
    for (let i = 1; i < lineFeatures.length; i += 1) {
      const segCoord = lineFeatures[i]!.geometry.coordinates[0]!;
      const legNum = i + 1;
      pinFeatures.push({
        type: "Feature",
        properties: {
          kind: "leg",
          label: String(legNum),
          pin_key: `leg-${legNum}`,
          title: `Leg ${legNum} · ${analysis.legs[i]?.name ?? `Segment ${legNum}`}`,
        },
        geometry: { type: "Point", coordinates: [segCoord[0]!, segCoord[1]!] },
      });
    }

    pinFeatures.push({
      type: "Feature",
      properties: {
        kind: "finish",
        label: "F",
        pin_key: "finish-F",
        title: `Finish · ${analysis.legs[analysis.legs.length - 1]?.name ?? analysis.name}`,
      },
      geometry: { type: "Point", coordinates: [endCoord[0]!, endCoord[1]!] },
    });
  }

  // Custom user waypoints
  (analysis.waypoints ?? []).forEach((wpt, idx) => {
    if (!Number.isFinite(wpt.lon) || !Number.isFinite(wpt.lat)) return;
    const tag = `W${idx + 1}`;
    pinFeatures.push({
      type: "Feature",
      properties: {
        kind: "waypoint",
        label: tag,
        pin_key: `wpt-${tag}`,
        title: wpt.name || `Waypoint ${idx + 1}`,
        category: wpt.category ?? "waypoint",
      },
      geometry: { type: "Point", coordinates: [wpt.lon, wpt.lat] },
    });
  });

  return {
    lineFc: { type: "FeatureCollection", features: lineFeatures },
    pinsFc: { type: "FeatureCollection", features: pinFeatures },
  };
}

export function extractAnalysisBBox(
  analysis: PlannedRouteAnalysis | null
): BBoxTuple | null {
  if (!analysis) return null;
  if (
    analysis.bbox &&
    analysis.bbox.length === 4 &&
    analysis.bbox.every((n) => typeof n === "number" && Number.isFinite(n))
  ) {
    return analysis.bbox;
  }

  const { lineFc, pinsFc } = buildPlannedRouteMapGeoJson(analysis);
  let minLon = Infinity;
  let minLat = Infinity;
  let maxLon = -Infinity;
  let maxLat = -Infinity;

  for (const f of lineFc.features) {
    for (const c of f.geometry.coordinates) {
      if (c[0]! < minLon) minLon = c[0]!;
      if (c[0]! > maxLon) maxLon = c[0]!;
      if (c[1]! < minLat) minLat = c[1]!;
      if (c[1]! > maxLat) maxLat = c[1]!;
    }
  }
  for (const p of pinsFc.features) {
    const [lon, lat] = p.geometry.coordinates;
    if (lon! < minLon) minLon = lon!;
    if (lon! > maxLon) maxLon = lon!;
    if (lat! < minLat) minLat = lat!;
    if (lat! > maxLat) maxLat = lat!;
  }

  if (!Number.isFinite(minLon)) return null;
  return [minLon, minLat, maxLon, maxLat];
}

/* ---------- Offline Fallback Multi-Track Route Planner ---------- */

function canonicalizeGrade(raw: string | null | undefined): string {
  const lower = (raw ?? "easy").trim().toLowerCase();
  if (lower === "extreme") return "Extreme";
  if (lower === "hard") return "Hard";
  if (lower === "medium") return "Medium";
  return "Easy";
}

function gradeRank(raw: string | null | undefined): number {
  return GRADE_RANK[(raw ?? "easy").trim().toLowerCase()] ?? 1;
}

function extractTrackCoordinates(detail: TrackInspectorDetail): CoordTuple[] {
  const coords: CoordTuple[] = [];
  for (const seg of detail.segments ?? []) {
    const geom = (
      seg as unknown as {
        geometry?: { type?: string; coordinates?: unknown };
      }
    ).geometry;
    if (!geom) continue;
    const pushLine = (pts: unknown[]): void => {
      for (const raw of pts) {
        if (!isCoordPair(raw)) continue;
        const prev = coords[coords.length - 1];
        if (
          prev &&
          Math.abs(prev[0]! - raw[0]!) < 1e-7 &&
          Math.abs(prev[1]! - raw[1]!) < 1e-7
        ) {
          continue;
        }
        const elev =
          typeof raw[2] === "number" && Number.isFinite(raw[2])
            ? raw[2]
            : (seg.elev_min_m ?? undefined);
        coords.push(elev !== undefined ? [raw[0]!, raw[1]!, elev] : [raw[0]!, raw[1]!]);
      }
    };

    if (geom.type === "LineString" && Array.isArray(geom.coordinates)) {
      pushLine(geom.coordinates);
    } else if (
      geom.type === "MultiLineString" &&
      Array.isArray(geom.coordinates)
    ) {
      for (const sub of geom.coordinates) {
        if (Array.isArray(sub)) pushLine(sub);
      }
    }
  }

  if (coords.length < 2 && detail.bbox) {
    const [w, s, e, n] = detail.bbox;
    coords.push([w, s], [e, n]);
  }
  return coords;
}

function downsampleProfile(
  pts: RouteElevationPoint[],
  maxPoints = 600
): RouteElevationPoint[] {
  if (pts.length <= maxPoints) return pts;
  const step = (pts.length - 1) / (maxPoints - 1);
  const out: RouteElevationPoint[] = [];
  for (let i = 0; i < maxPoints; i += 1) {
    const idx = Math.min(pts.length - 1, Math.round(i * step));
    out.push(pts[idx]!);
  }
  return out;
}

/**
 * Pure offline multi-track route planner that stitches downloaded pack tracks
 * end-to-end, auto-orients legs to minimize jump distances, flags gaps > 100 m,
 * and aggregates safety, elevation, suitability, and GridForge CLI output.
 */
export function planRouteOffline(
  tracks: readonly TrackInspectorDetail[],
  request: RoutePlanRequest,
  options?: { vehicleName?: string | null }
): PlannedRouteAnalysis {
  const trackIds = request.track_ids
    .map((id) => id.trim())
    .filter((id) => id.length > 0);
  if (trackIds.length === 0 || trackIds.length > MAX_ROUTE_LEGS) {
    throw new Error(
      `Route must contain between 1 and ${MAX_ROUTE_LEGS} tracks`
    );
  }

  const byId = new Map(tracks.map((t) => [t.track_id, t]));
  const ordered: TrackInspectorDetail[] = [];
  for (const id of trackIds) {
    const found = byId.get(id);
    if (!found) {
      throw new Error(`Track "${id}" not available in offline pack snapshot`);
    }
    ordered.push(found);
  }

  const routeName =
    request.name?.trim() ||
    (ordered.length === 1
      ? ordered[0]!.name
      : `${ordered[0]!.name} → ${ordered[ordered.length - 1]!.name}`);

  const rawCoordsPerLeg = ordered.map((t) => extractTrackCoordinates(t));
  const orientedCoords: CoordTuple[][] = [];
  const reversedFlags: boolean[] = new Array<boolean>(ordered.length).fill(false);

  // Auto-orient legs to minimize end-to-start jump distance
  if (ordered.length === 1) {
    orientedCoords.push(rawCoordsPerLeg[0]!);
  } else {
    // Pick best orientation for leg 0 relative to leg 1
    const c0 = rawCoordsPerLeg[0]!;
    const c1 = rawCoordsPerLeg[1]!;
    if (c0.length >= 2 && c1.length >= 2) {
      const s0 = c0[0]!;
      const e0 = c0[c0.length - 1]!;
      const s1 = c1[0]!;
      const e1 = c1[c1.length - 1]!;
      const fwdMin = Math.min(
        haversineDistanceM(e0[1]!, e0[0]!, s1[1]!, s1[0]!),
        haversineDistanceM(e0[1]!, e0[0]!, e1[1]!, e1[0]!)
      );
      const revMin = Math.min(
        haversineDistanceM(s0[1]!, s0[0]!, s1[1]!, s1[0]!),
        haversineDistanceM(s0[1]!, s0[0]!, e1[1]!, e1[0]!)
      );
      if (revMin + 1 < fwdMin) {
        orientedCoords.push([...c0].reverse());
        reversedFlags[0] = true;
      } else {
        orientedCoords.push(c0);
      }
    } else {
      orientedCoords.push(c0);
    }

    for (let i = 1; i < ordered.length; i += 1) {
      const prevLine = orientedCoords[i - 1]!;
      const currLine = rawCoordsPerLeg[i]!;
      if (prevLine.length >= 1 && currLine.length >= 2) {
        const prevEnd = prevLine[prevLine.length - 1]!;
        const dStart = haversineDistanceM(
          prevEnd[1]!,
          prevEnd[0]!,
          currLine[0]![1]!,
          currLine[0]![0]!
        );
        const dEnd = haversineDistanceM(
          prevEnd[1]!,
          prevEnd[0]!,
          currLine[currLine.length - 1]![1]!,
          currLine[currLine.length - 1]![0]!
        );
        if (dEnd + 1 < dStart) {
          orientedCoords.push([...currLine].reverse());
          reversedFlags[i] = true;
        } else {
          orientedCoords.push(currLine);
        }
      } else {
        orientedCoords.push(currLine);
      }
    }
  }

  let totalLengthM = 0;
  let totalEstDurationMin = 0;
  let elevGainM = 0;
  let elevLossM = 0;
  let minElevM: number | null = null;
  let maxElevM: number | null = null;
  let maxSlopeDeg: number | null = null;
  let sustainedSlopeDeg: number | null = null;
  let worstGrade = "Easy";
  let worstScore: number | null = null;
  let worstWetRisk: WetRiskLevel = "dry";
  let hasBlockingClosure = false;

  const legs: PlannedRouteLeg[] = [];
  const legGaps: RouteLegGap[] = [];
  const lineFeatures: Feature<LineString>[] = [];
  const combinedProfile: RouteElevationPoint[] = [];
  const combinedCrossings: WaterwayCrossingItem[] = [];

  const seenClosures = new Set<string>();
  const closures: ClosureItem[] = [];
  const seenGates = new Set<string>();
  const gateWarnings: ConditionReportItem[] = [];
  const seenReports = new Set<string>();
  const conditionReports: ConditionReportItem[] = [];
  const seenPois = new Set<string>();
  const nearbyPois: PoiItem[] = [];
  const seenParks = new Set<string>();
  const capadParks: ParkTenureItem[] = [];

  let cumulativeDistM = 0;

  for (let idx = 0; idx < ordered.length; idx += 1) {
    const detail = ordered[idx]!;
    const norm = normalizeTrackInspector(detail);
    const legGrade = canonicalizeGrade(
      detail.live_grade ?? detail.effective_grade ?? detail.grade ?? norm.grade
    );
    const legScore = detail.live_score ?? detail.score ?? norm.score ?? null;
    const lengthM = Math.max(0, detail.total_length_m);
    const reversed = reversedFlags[idx] ?? false;

    let gapFromPrevM: number | null = null;
    if (idx > 0) {
      const prevCoords = orientedCoords[idx - 1]!;
      const currCoords = orientedCoords[idx]!;
      if (prevCoords.length > 0 && currCoords.length > 0) {
        const prevEnd = prevCoords[prevCoords.length - 1]!;
        const currStart = currCoords[0]!;
        const gap = Math.round(
          haversineDistanceM(prevEnd[1]!, prevEnd[0]!, currStart[1]!, currStart[0]!)
        );
        gapFromPrevM = gap;
        if (gap > LEG_GAP_WARNING_M) {
          legGaps.push({
            from_track_id: ordered[idx - 1]!.track_id,
            to_track_id: detail.track_id,
            gap_m: gap,
          });
        }
      }
    }

    const { gateWarnings: legGates, timelineReports: legReports } =
      partitionTrackReports(
        detail.condition_reports ?? [],
        detail.community_gate_warnings ?? null
      );

    for (const c of detail.active_closures ?? []) {
      const key = String(c.closure_id ?? c.id ?? c.external_id ?? c.title);
      if (!seenClosures.has(key)) {
        seenClosures.add(key);
        closures.push(c);
      }
    }
    const cStatus = (detail.closure_status ?? "open").toLowerCase();
    if (
      cStatus === "closed" ||
      cStatus === "seasonal" ||
      (detail.active_closures ?? []).some((c) => {
        const t = c.closure_type.toLowerCase();
        const s = (c.severity ?? "").toLowerCase();
        return (
          t === "closed" ||
          t === "road_closure" ||
          t === "park_closure" ||
          t === "seasonal" ||
          s === "critical" ||
          s === "emergency"
        );
      })
    ) {
      hasBlockingClosure = true;
    }

    for (const g of legGates) {
      const key = String(g.report_id ?? g.id ?? `${g.lat}:${g.lon}:${g.notes}`);
      if (!seenGates.has(key)) {
        seenGates.add(key);
        gateWarnings.push(g);
      }
    }
    for (const r of legReports) {
      const key = String(r.report_id ?? r.id ?? `${r.lat}:${r.lon}:${r.notes}`);
      if (!seenReports.has(key)) {
        seenReports.add(key);
        conditionReports.push(r);
      }
    }

    const rawWet = (detail.rainfall_context?.wet_risk_level ?? "dry")
      .trim()
      .toLowerCase();
    if ((WET_RANK[rawWet] ?? 0) > (WET_RANK[worstWetRisk] ?? 0)) {
      worstWetRisk = (rawWet === "heavy_rain" || rawWet === "wet" || rawWet === "damp"
        ? rawWet
        : "dry") as WetRiskLevel;
    }

    for (const poi of detail.nearby_pois ?? []) {
      const key = String(poi.poi_id ?? poi.id ?? poi.poi_key ?? `${poi.name}:${poi.lat.toFixed(4)}`);
      if (!seenPois.has(key)) {
        seenPois.add(key);
        nearbyPois.push(poi);
      }
    }
    for (const pk of norm.parks) {
      const key = String(pk.id ?? pk.capad_external_id ?? pk.name);
      if (!seenParks.has(key)) {
        seenParks.add(key);
        capadParks.push(pk);
      }
    }

    // The offline planner orients the original leg geometry. Use distances from
    // that same track order, which can differ from the inspector's mapped sections.
    for (const cr of detail.waterway_crossings ?? norm.crossings) {
      const localDist =
        typeof cr.dist_m === "number"
          ? reversed
            ? Math.max(0, lengthM - cr.dist_m)
            : cr.dist_m
          : lengthM / 2;
      combinedCrossings.push({
        ...cr,
        dist_m: Math.round(cumulativeDistM + localDist),
      });
    }

    const profile = detail.elevation_profile?.length ? detail.elevation_profile : norm.profile;
    const legProf = reversed
      ? [...profile].reverse().map((p) => ({
          ...p,
          dist_m: Math.max(0, lengthM - p.dist_m),
        }))
      : profile;
    for (const p of legProf) {
      combinedProfile.push({
        dist_m: Math.round(cumulativeDistM + p.dist_m),
        elev_m: p.elev_m,
        slope_deg: p.slope_deg ?? null,
        segment_id: p.segment_id ?? null,
        grade: p.grade ?? legGrade,
        track_id: detail.track_id,
        track_name: detail.name,
      });
    }

    totalLengthM += lengthM;
    const speedKmh =
      typeof detail.avg_speed_kmh === "number" && detail.avg_speed_kmh > 1
        ? detail.avg_speed_kmh
        : (GRADE_SPEED_KMH[legGrade.toLowerCase()] ?? 18);
    totalEstDurationMin +=
      typeof detail.est_duration_min === "number" && detail.est_duration_min > 0
        ? detail.est_duration_min
        : (lengthM / 1000 / speedKmh) * 60;

    if (reversed) {
      elevGainM += norm.elevLossM ?? 0;
      elevLossM += norm.elevGainM;
    } else {
      elevGainM += norm.elevGainM;
      elevLossM += norm.elevLossM ?? 0;
    }

    if (norm.minElevM !== null) {
      minElevM = minElevM === null ? norm.minElevM : Math.min(minElevM, norm.minElevM);
    }
    if (norm.maxElevM !== null) {
      maxElevM = maxElevM === null ? norm.maxElevM : Math.max(maxElevM, norm.maxElevM);
    }
    if (norm.maxSlopeDeg !== null) {
      maxSlopeDeg =
        maxSlopeDeg === null
          ? norm.maxSlopeDeg
          : Math.max(maxSlopeDeg, norm.maxSlopeDeg);
    }
    if (norm.sustainedSlopeDeg !== null) {
      sustainedSlopeDeg =
        sustainedSlopeDeg === null
          ? norm.sustainedSlopeDeg
          : Math.max(sustainedSlopeDeg, norm.sustainedSlopeDeg);
    }

    if (gradeRank(legGrade) > gradeRank(worstGrade)) {
      worstGrade = legGrade;
    }
    if (legScore !== null) {
      worstScore = worstScore === null ? legScore : Math.max(worstScore, legScore);
    }

    legs.push({
      index: idx,
      track_id: detail.track_id,
      name: detail.name,
      length_m: Math.round(lengthM),
      grade: legGrade,
      score: legScore,
      closure_status: detail.closure_status ?? "open",
      gate_warning_count: legGates.length,
      reversed,
      gap_from_prev_m: gapFromPrevM,
    });

    const coords = orientedCoords[idx] ?? [];
    if (coords.length >= 2) {
      lineFeatures.push({
        type: "Feature",
        properties: {
          leg_index: idx,
          track_id: detail.track_id,
          name: detail.name,
          grade: legGrade,
          reversed,
        },
        geometry: {
          type: "LineString",
          coordinates: coords,
        },
      });
    }

    cumulativeDistM += lengthM;
  }

  const unbridgedCrossings = combinedCrossings.filter((c) => !c.bridged).length;
  const warnings: string[] = [];
  if (hasBlockingClosure || closures.length > 0) {
    warnings.push(
      `${closures.length || 1} official closure/access notice active along route`
    );
  }
  if (gateWarnings.length > 0) {
    warnings.push(
      `${gateWarnings.length} locked gate / private property report(s) flagged`
    );
  }
  if (worstWetRisk === "heavy_rain" || worstWetRisk === "wet") {
    warnings.push(
      `Wet-track risk (${worstWetRisk.replace(/_/g, " ")}) from recent 72 h rainfall`
    );
  }
  if (legGaps.length > 0) {
    warnings.push(
      `${legGaps.length} non-contiguous leg gap(s) > ${LEG_GAP_WARNING_M} m`
    );
  }

  const safetySummary: RouteSafetySummary = {
    has_blocking_closure: hasBlockingClosure,
    closure_count: closures.length,
    gate_warning_count: gateWarnings.length,
    active_report_count: conditionReports.length,
    unbridged_crossings: unbridgedCrossings,
    wet_risk_level: worstWetRisk,
    warnings,
  };

  const vehicleClass = (request.vehicle_class ?? "Stock 4WD") as VehicleCapabilityClass;
  const suitability = computeClientSuitability(worstGrade, vehicleClass, {
    vehicleName: options?.vehicleName ?? null,
    closureStatus: hasBlockingClosure
      ? "closed"
      : gateWarnings.length > 0
        ? "gated"
        : "open",
  });

  const geometryFc: FeatureCollection<LineString> = {
    type: "FeatureCollection",
    features: lineFeatures,
  };

  const partial: PlannedRouteAnalysis = {
    name: routeName,
    track_ids: trackIds,
    waypoints: request.waypoints ?? [],
    total_length_m: Math.round(totalLengthM),
    est_duration_min: Math.max(1, Math.round(totalEstDurationMin)),
    min_elev_m: minElevM,
    max_elev_m: maxElevM,
    elev_gain_m: Math.round(elevGainM),
    elev_loss_m: Math.round(elevLossM),
    max_slope_deg: maxSlopeDeg,
    sustained_slope_deg: sustainedSlopeDeg,
    worst_grade: worstGrade,
    worst_score: worstScore,
    effective_grade: worstGrade,
    suitability,
    safety_summary: safetySummary,
    closures,
    community_gate_warnings: gateWarnings,
    condition_reports: conditionReports,
    waterway_crossings: combinedCrossings,
    nearby_pois: nearbyPois,
    capad_parks: capadParks,
    elevation_profile: downsampleProfile(combinedProfile, 600),
    legs,
    leg_gaps: legGaps,
    bbox: null,
    geometry: geometryFc,
    gridforge_cli: formatGridForgeCli(routeName),
    offline_computed: true,
  };

  return {
    ...partial,
    bbox: extractAnalysisBBox(partial),
  };
}

export async function planRouteFromOfflineStore(
  request: RoutePlanRequest,
  options?: { vehicleName?: string | null }
): Promise<PlannedRouteAnalysis | null> {
  const trackIds = request.track_ids
    .map((id) => id.trim())
    .filter((id) => id.length > 0);
  if (trackIds.length === 0) return null;

  const details: TrackInspectorDetail[] = [];
  for (const id of trackIds) {
    const found = await getOfflineTrackDetail(id);
    if (!found) return null;
    details.push(found);
  }
  return planRouteOffline(details, request, options);
}

/* ---------- Client-Side Offline GPX 1.1 & GeoJSON Route Export ---------- */

function escapeXml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

export function buildRouteClientGpx(analysis: PlannedRouteAnalysis): string {
  const cleanName = analysis.name.trim() || "TrailForge Planned Route";
  const nowIso = new Date().toISOString();
  const { lineFc } = buildPlannedRouteMapGeoJson(analysis);

  const lines: string[] = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<gpx version="1.1" creator="TrailForge (GPL-3.0-or-later)" xmlns="http://www.topografix.com/GPX/1/1">',
    "  <metadata>",
    `    <name>${escapeXml(cleanName)}</name>`,
    `    <desc>${escapeXml(`Worst Grade: ${analysis.worst_grade} | ${(analysis.total_length_m / 1000).toFixed(1)} km | ${analysis.legs.length} leg(s)`)}</desc>`,
    `    <time>${escapeXml(nowIso)}</time>`,
    "  </metadata>",
  ];

  // Custom waypoints
  for (const wpt of analysis.waypoints ?? []) {
    if (!Number.isFinite(wpt.lat) || !Number.isFinite(wpt.lon)) continue;
    lines.push(
      `  <wpt lat="${wpt.lat.toFixed(6)}" lon="${wpt.lon.toFixed(6)}">`,
      `    <name>${escapeXml(wpt.name)}</name>`,
      wpt.category ? `    <type>${escapeXml(String(wpt.category))}</type>` : "",
      "  </wpt>"
    );
  }

  // Nearby camps, huts, and POIs
  for (const poi of analysis.nearby_pois ?? []) {
    if (!Number.isFinite(poi.lat) || !Number.isFinite(poi.lon)) continue;
    const elev = poi.elevation_m ?? poi.elev_m;
    lines.push(`  <wpt lat="${poi.lat.toFixed(6)}" lon="${poi.lon.toFixed(6)}">`);
    if (typeof elev === "number" && Number.isFinite(elev)) {
      lines.push(`    <ele>${elev.toFixed(1)}</ele>`);
    }
    lines.push(`    <name>${escapeXml(poi.name)}</name>`);
    const poiType = poi.category ?? poi.poi_type;
    if (poiType) {
      lines.push(`    <type>${escapeXml(poiType)}</type>`);
    }
    lines.push("  </wpt>");
  }

  // Community gate warnings as hazard waypoints
  for (const gate of analysis.community_gate_warnings ?? []) {
    if (!Number.isFinite(gate.lat) || !Number.isFinite(gate.lon)) continue;
    lines.push(
      `  <wpt lat="${gate.lat.toFixed(6)}" lon="${gate.lon.toFixed(6)}">`,
      `    <name>${escapeXml(gate.title || `Gate Warning (${gate.report_type})`)}</name>`,
      `    <desc>${escapeXml(gate.notes)}</desc>`,
      "    <type>gate_warning</type>",
      "  </wpt>"
    );
  }

  lines.push("  <trk>", `    <name>${escapeXml(cleanName)}</name>`);
  for (const feat of lineFc.features) {
    lines.push("    <trkseg>");
    for (const c of feat.geometry.coordinates) {
      const lon = c[0];
      const lat = c[1];
      const ele = c[2];
      if (typeof lon !== "number" || typeof lat !== "number") continue;
      lines.push(`      <trkpt lat="${lat.toFixed(6)}" lon="${lon.toFixed(6)}">`);
      if (typeof ele === "number" && Number.isFinite(ele)) {
        lines.push(`        <ele>${ele.toFixed(1)}</ele>`);
      }
      lines.push("      </trkpt>");
    }
    lines.push("    </trkseg>");
  }
  lines.push("  </trk>", "</gpx>");

  return `${lines.filter(Boolean).join("\n")}\n`;
}

export function buildRouteClientGeoJson(
  analysis: PlannedRouteAnalysis
): FeatureCollection {
  const { lineFc } = buildPlannedRouteMapGeoJson(analysis);
  const features: Feature[] = lineFc.features.map((feat, idx) => {
    const leg = analysis.legs[idx];
    return {
      type: "Feature",
      properties: {
        ...feat.properties,
        route_name: analysis.name,
        leg_index: idx,
        track_id: leg?.track_id ?? feat.properties?.track_id ?? "",
        name: leg?.name ?? feat.properties?.name ?? `Leg ${idx + 1}`,
        grade: leg?.grade ?? feat.properties?.grade ?? analysis.worst_grade,
        length_m: leg?.length_m ?? null,
        source: "TrailForge",
        license: "GPL-3.0-or-later",
      },
      geometry: feat.geometry,
    };
  });

  for (const wpt of analysis.waypoints ?? []) {
    if (!Number.isFinite(wpt.lon) || !Number.isFinite(wpt.lat)) continue;
    features.push({
      type: "Feature",
      properties: {
        kind: "waypoint",
        name: wpt.name,
        category: wpt.category ?? "waypoint",
      },
      geometry: { type: "Point", coordinates: [wpt.lon, wpt.lat] },
    });
  }

  for (const poi of analysis.nearby_pois ?? []) {
    if (!Number.isFinite(poi.lon) || !Number.isFinite(poi.lat)) continue;
    features.push({
      type: "Feature",
      properties: {
        kind: "poi",
        name: poi.name,
        category: poi.category ?? poi.poi_type ?? "poi",
        elev_m: poi.elevation_m ?? poi.elev_m ?? null,
      },
      geometry: { type: "Point", coordinates: [poi.lon, poi.lat] },
    });
  }

  return {
    type: "FeatureCollection",
    features,
  };
}
