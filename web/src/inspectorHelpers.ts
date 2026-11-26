import { escapeHtml, getGradeColor } from "./presets";
import type {
  BaselineBreakdown,
  ElevationProfilePoint,
  ParkTenureItem,
  SegmentSummaryItem,
  TrackInspectorDetail,
  WaterwayCrossingItem,
} from "./types";

export interface NormalizedTrackSummary {
  trackId: string;
  name: string;
  altNames: string[];
  state: string;
  grade: string;
  score: number | null;
  totalLengthKm: number;
  minElevM: number | null;
  maxElevM: number | null;
  elevGainM: number;
  elevLossM: number | null;
  maxSlopeDeg: number | null;
  sustainedSlopeDeg: number | null;
  waterwayCrossingCount: number;
  is4wdOnly: boolean;
  hasGateBarrier: boolean;
  accessStatus: string;
  parks: ParkTenureItem[];
  breakdown: BaselineBreakdown | null;
  profile: ElevationProfilePoint[];
  crossings: WaterwayCrossingItem[];
}

export interface FormattedBreakdownFactor {
  key: string;
  label: string;
  points: number;
  maxPoints: number;
  pct: number;
  detail: string;
}

const OVERRIDE_LABELS: Record<string, string> = {
  steep_rough_or_perennial_extreme_floor:
    "Safety Floor: Extreme (≥20° slope + rough surface or perennial crossing)",
  steep_or_4wd_hard_floor:
    "Safety Floor: Hard (≥18° slope, or 4WD-only ≥14°)",
  perennial_or_4wd_medium_floor:
    "Safety Floor: Medium (perennial water crossing or 4WD-only)",
};

export function formatOverrideLabel(code: string): string {
  return OVERRIDE_LABELS[code] ?? code.replace(/_/g, " ");
}

function extractSegmentGrade(seg: SegmentSummaryItem): string | null {
  if (typeof seg.grade === "string") return seg.grade;
  if (seg.grade && typeof seg.grade === "object") {
    return seg.grade.effective_grade ?? seg.grade.baseline_grade ?? null;
  }
  return seg.effective_grade ?? null;
}

function extractSegmentScore(seg: SegmentSummaryItem): number | null {
  if (typeof seg.score === "number") return seg.score;
  if (seg.grade && typeof seg.grade === "object") {
    return seg.grade.baseline_score;
  }
  return null;
}

function extractSegmentBreakdown(
  seg: SegmentSummaryItem
): BaselineBreakdown | null {
  if (seg.grade && typeof seg.grade === "object" && seg.grade.baseline_breakdown) {
    return seg.grade.baseline_breakdown;
  }
  return null;
}

/**
 * Normalize a TrackInspectorDetail payload from either the enriched Slice 03
 * endpoint or a fallback Slice 02 payload into consistent display values.
 */
export function normalizeTrackInspector(
  detail: TrackInspectorDetail
): NormalizedTrackSummary {
  const segments = detail.segments ?? [];
  const grade = detail.grade ?? detail.effective_grade ?? "Easy";

  // Pick worst-segment breakdown if top-level baseline_breakdown wasn't provided
  let breakdown = detail.baseline_breakdown ?? null;
  let score = detail.score ?? breakdown?.final_score ?? null;

  if (!breakdown && segments.length > 0) {
    let worstScore = -1;
    for (const seg of segments) {
      const segScore = extractSegmentScore(seg) ?? 0;
      const segBd = extractSegmentBreakdown(seg);
      if (segBd && segScore >= worstScore) {
        worstScore = segScore;
        breakdown = segBd;
      }
    }
    if (score === null && worstScore >= 0) {
      score = worstScore;
    }
  }

  // Synthesize an elevation profile from ordered segments if explicit profile missing
  let profile: ElevationProfilePoint[] = detail.elevation_profile ?? [];
  if (profile.length === 0 && segments.length > 0) {
    let cumDist = 0;
    const built: ElevationProfilePoint[] = [];
    for (const seg of segments) {
      const elevMin = seg.elev_min_m;
      const elevMax = seg.elev_max_m;
      const len = seg.length_m ?? 75;
      if (typeof elevMin === "number" || typeof elevMax === "number") {
        const startE = elevMin ?? elevMax ?? 0;
        const endE = elevMax ?? elevMin ?? startE;
        if (built.length === 0) {
          built.push({
            dist_m: 0,
            elev_m: startE,
            slope_deg: seg.max_slope_deg ?? null,
            segment_id: seg.segment_id,
            grade: extractSegmentGrade(seg),
          });
        }
        cumDist += len;
        built.push({
          dist_m: Math.round(cumDist),
          elev_m: endE,
          slope_deg: seg.max_slope_deg ?? null,
          segment_id: seg.segment_id,
          grade: extractSegmentGrade(seg),
        });
      }
    }
    profile = built;
  }

  // Collect parks (enriched array or fallback primary_park)
  const parks: ParkTenureItem[] =
    detail.capad_parks && detail.capad_parks.length > 0
      ? detail.capad_parks
      : detail.primary_park
        ? [detail.primary_park]
        : [];

  const minElevM = detail.min_elev_m ?? detail.elev_min_m ?? null;
  const maxElevM = detail.max_elev_m ?? detail.elev_max_m ?? null;

  const is4wdOnly =
    detail.is_4wd_only ?? segments.some((s) => Boolean(s.is_4wd_only));
  const hasGateBarrier =
    detail.has_gate_barrier ?? segments.some((s) => Boolean(s.has_gate_barrier));
  const accessStatus =
    detail.access_status ??
    segments.find(
      (s) => s.access_status && s.access_status !== "public"
    )?.access_status ??
    "public";

  const elevLossM =
    typeof detail.elev_loss_m === "number"
      ? detail.elev_loss_m
      : segments.length > 0
        ? segments.reduce((acc, s) => acc + (s.elev_loss_m ?? 0), 0)
        : null;

  const sustainedSlopeDeg =
    detail.sustained_slope_deg ??
    segments.reduce<number | null>((best, s) => {
      if (typeof s.sustained_slope_deg !== "number") return best;
      return best === null ? s.sustained_slope_deg : Math.max(best, s.sustained_slope_deg);
    }, null);

  return {
    trackId: detail.track_id,
    name: detail.name,
    altNames: detail.alt_names ?? [],
    state: detail.state,
    grade,
    score: typeof score === "number" ? score : null,
    totalLengthKm: detail.total_length_m / 1000,
    minElevM,
    maxElevM,
    elevGainM: detail.elev_gain_m,
    elevLossM,
    maxSlopeDeg: detail.max_slope_deg ?? null,
    sustainedSlopeDeg,
    waterwayCrossingCount: detail.waterway_crossing_count,
    is4wdOnly,
    hasGateBarrier,
    accessStatus,
    parks,
    breakdown,
    profile,
    crossings: detail.waterway_crossings ?? [],
  };
}

/**
 * Extract structured explainable factor rows for the Track Inspector grade breakdown UI.
 */
export function formatBreakdownFactors(
  breakdown: BaselineBreakdown | null | undefined
): FormattedBreakdownFactor[] {
  const factors = breakdown?.factors;
  if (!factors) return [];

  const rows: FormattedBreakdownFactor[] = [];

  if (factors.surface_tracktype) {
    const f = factors.surface_tracktype;
    const surf = (f.surface_class ?? "unknown").replace(/_/g, " ");
    const tt = f.tracktype ? ` · ${f.tracktype}` : "";
    rows.push({
      key: "surface_tracktype",
      label: "Surface & Tracktype",
      points: f.points,
      maxPoints: f.max_points || 35,
      pct: Math.min(100, Math.round((f.points / (f.max_points || 35)) * 100)),
      detail: `${surf}${tt}`,
    });
  }

  if (factors.max_slope) {
    const f = factors.max_slope;
    rows.push({
      key: "max_slope",
      label: "Max Slope",
      points: f.points,
      maxPoints: f.max_points || 30,
      pct: Math.min(100, Math.round((f.points / (f.max_points || 30)) * 100)),
      detail:
        typeof f.max_slope_deg === "number"
          ? `${f.max_slope_deg.toFixed(1)}° peak`
          : "DEM slope n/a",
    });
  }

  if (factors.sustained_relief) {
    const f = factors.sustained_relief;
    const sust =
      typeof f.sustained_slope_deg === "number"
        ? `${f.sustained_slope_deg.toFixed(1)}° sustained`
        : "Gentle";
    const relief = Math.max(f.elev_gain_m ?? 0, f.elev_loss_m ?? 0);
    rows.push({
      key: "sustained_relief",
      label: "Sustained Grade & Relief",
      points: f.points,
      maxPoints: f.max_points || 15,
      pct: Math.min(100, Math.round((f.points / (f.max_points || 15)) * 100)),
      detail: `${sust} · ${Math.round(relief)} m relief`,
    });
  }

  if (factors.waterway_crossings) {
    const f = factors.waterway_crossings;
    const count = f.unbridged_count ?? 0;
    const detail = f.is_bridged
      ? "Bridged crossing (0 penalty)"
      : count > 0
        ? `${count} ${f.perennial ? "perennial" : "intermittent"} unbridged`
        : "No unbridged crossings";
    rows.push({
      key: "waterway_crossings",
      label: "Waterway Crossings",
      points: f.points,
      maxPoints: f.max_points || 12,
      pct: Math.min(100, Math.round((f.points / (f.max_points || 12)) * 100)),
      detail,
    });
  }

  if (factors.four_wd_access) {
    const f = factors.four_wd_access;
    const tags: string[] = [];
    if (f.is_4wd_only) tags.push("4WD Only");
    else if (f.is_4wd_recommended) tags.push("4WD Recommended");
    if (f.trail_class) tags.push(f.trail_class.replace(/_/g, " "));
    rows.push({
      key: "four_wd_access",
      label: "4WD & Trail Class",
      points: f.points,
      maxPoints: f.max_points || 8,
      pct: Math.min(100, Math.round((f.points / (f.max_points || 8)) * 100)),
      detail: tags.join(" · ") || "Standard",
    });
  }

  return rows;
}

export interface ElevationChartOptions {
  width?: number;
  height?: number;
  snowLineElevationM?: number;
  crossings?: readonly WaterwayCrossingItem[];
  steepThresholdDeg?: number;
}

export interface ElevationChartResult {
  svgHtml: string;
  minElevM: number;
  maxElevM: number;
  totalDistM: number;
  hasSnowLineIntersection: boolean;
  steepPointCount: number;
  steepZoneCount: number;
}

/**
 * Pure SVG elevation profile generator for the Track Inspector Drawer.
 * Renders elevation area, crisp color-coded grade polylines (Easy/Medium/Hard/Extreme),
 * subtle shaded steep zones (>=14°) with one peak marker per steep run (avoiding
 * overlapping marker occlusion on dense elevation samples), snow-line reference,
 * and waterway crossing ticks.
 */
export function buildElevationProfileSvg(
  points: readonly ElevationProfilePoint[],
  options?: ElevationChartOptions
): ElevationChartResult {
  const width = options?.width ?? 340;
  const height = options?.height ?? 138;
  const snowLineM = options?.snowLineElevationM ?? 1000;
  const steepThreshold = options?.steepThresholdDeg ?? 14.0;
  const crossings = options?.crossings ?? [];

  if (points.length < 2) {
    return {
      svgHtml: `<div class="tf-elev-empty">No elevation samples available for this track.</div>`,
      minElevM: 0,
      maxElevM: 0,
      totalDistM: 0,
      hasSnowLineIntersection: false,
      steepPointCount: 0,
      steepZoneCount: 0,
    };
  }

  const padL = 38;
  const padR = 10;
  const padT = 12;
  const padB = 22;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;

  const rawMinE = Math.min(...points.map((p) => p.elev_m));
  const rawMaxE = Math.max(...points.map((p) => p.elev_m));
  const spanE = Math.max(40, rawMaxE - rawMinE);
  const yMin = Math.floor((rawMinE - spanE * 0.08) / 20) * 20;
  const yMax = Math.ceil((rawMaxE + spanE * 0.1) / 20) * 20;
  const totalDistM = Math.max(1, points[points.length - 1]!.dist_m);

  const toX = (distM: number): number => padL + (distM / totalDistM) * plotW;
  const toY = (elevM: number): number =>
    padT + (1 - (elevM - yMin) / Math.max(1, yMax - yMin)) * plotH;

  const polylinePts = points
    .map((p) => `${toX(p.dist_m).toFixed(1)},${toY(p.elev_m).toFixed(1)}`)
    .join(" ");

  const firstX = toX(points[0]!.dist_m).toFixed(1);
  const lastX = toX(points[points.length - 1]!.dist_m).toFixed(1);
  const baseY = (padT + plotH).toFixed(1);
  const areaPath = `M ${firstX},${baseY} L ${polylinePts.replace(/ /g, " L ")} L ${lastX},${baseY} Z`;

  // Group contiguous segments sharing the same grade color into clean polylines
  // so dense profiles (500+ points) render crisp and brightly colored without stroke artifacts.
  const gradeRuns: { color: string; coords: string[] }[] = [];
  for (let i = 1; i < points.length; i += 1) {
    const prev = points[i - 1]!;
    const curr = points[i]!;
    const color = getGradeColor(curr.grade ?? prev.grade);
    const p1 = `${toX(prev.dist_m).toFixed(1)},${toY(prev.elev_m).toFixed(1)}`;
    const p2 = `${toX(curr.dist_m).toFixed(1)},${toY(curr.elev_m).toFixed(1)}`;
    const lastRun = gradeRuns[gradeRuns.length - 1];
    if (lastRun && lastRun.color === color) {
      lastRun.coords.push(p2);
    } else {
      gradeRuns.push({ color, coords: [p1, p2] });
    }
  }

  const segLinesHtml = gradeRuns
    .map(
      (run) =>
        `<polyline points="${run.coords.join(" ")}" fill="none" stroke="${run.color}" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>`
    )
    .join("");

  // Identify contiguous steep zones (slope >= 14°) and keep 1 peak marker per zone
  // so dense steep sections highlight cleanly without overlapping black strokes hiding the curve.
  const steepPoints = points.filter(
    (p) => typeof p.slope_deg === "number" && p.slope_deg >= steepThreshold
  );
  interface SteepZone {
    startDistM: number;
    endDistM: number;
    peak: ElevationProfilePoint;
  }
  const steepZones: SteepZone[] = [];
  let activeZone: SteepZone | null = null;

  for (const p of points) {
    const isSteep =
      typeof p.slope_deg === "number" && p.slope_deg >= steepThreshold;
    if (isSteep) {
      if (!activeZone) {
        activeZone = { startDistM: p.dist_m, endDistM: p.dist_m, peak: p };
      } else {
        activeZone.endDistM = p.dist_m;
        if ((p.slope_deg ?? 0) > (activeZone.peak.slope_deg ?? 0)) {
          activeZone.peak = p;
        }
      }
    } else if (activeZone) {
      steepZones.push(activeZone);
      activeZone = null;
    }
  }
  if (activeZone) {
    steepZones.push(activeZone);
  }

  const steepBandsHtml = steepZones
    .map((z) => {
      const x1 = toX(z.startDistM);
      const x2 = Math.max(x1 + 2.5, toX(z.endDistM));
      return `<rect x="${x1.toFixed(1)}" y="${padT}" width="${(x2 - x1).toFixed(1)}" height="${plotH}" fill="rgba(239, 68, 68, 0.12)"/>`;
    })
    .join("");

  const steepMarkersHtml = steepZones
    .map((z) => {
      const p = z.peak;
      const cx = toX(p.dist_m).toFixed(1);
      const cy = toY(p.elev_m).toFixed(1);
      const markerColor = getGradeColor(p.grade);
      return `<circle cx="${cx}" cy="${cy}" r="3.4" fill="${markerColor}" stroke="#F8FAFC" stroke-width="1.2"><title>Peak steep ${p.slope_deg?.toFixed(1)}° at ${(p.dist_m / 1000).toFixed(2)} km (${Math.round(p.elev_m)} m)</title></circle>`;
    })
    .join("");

  // Horizontal grid lines (min, mid, max)
  const yMid = Math.round((yMin + yMax) / 2);
  const yTicks = [yMin, yMid, yMax];
  const gridHtml = yTicks
    .map((e) => {
      const y = toY(e).toFixed(1);
      return `
        <line x1="${padL}" y1="${y}" x2="${width - padR}" y2="${y}" stroke="#233240" stroke-width="1" stroke-dasharray="2 2"/>
        <text x="${padL - 4}" y="${Number(y) + 3}" text-anchor="end" fill="#6E8396" font-size="9.5" font-family="ui-monospace, monospace">${Math.round(e)}m</text>
      `;
    })
    .join("");

  // Snow-line horizontal reference line when inside chart range
  const hasSnowLineIntersection = snowLineM >= yMin && snowLineM <= yMax;
  let snowLineHtml = "";
  if (hasSnowLineIntersection) {
    const sy = toY(snowLineM);
    const labelY = Math.max(padT + 9, sy - 3);
    snowLineHtml = `
      <rect x="${padL}" y="${padT}" width="${plotW}" height="${Math.max(0, sy - padT).toFixed(1)}" fill="rgba(186, 230, 253, 0.09)"/>
      <line x1="${padL}" y1="${sy.toFixed(1)}" x2="${width - padR}" y2="${sy.toFixed(1)}" stroke="#7DD3FC" stroke-width="1.3" stroke-dasharray="4 3"/>
      <text x="${width - padR - 2}" y="${labelY.toFixed(1)}" text-anchor="end" fill="#7DD3FC" font-size="9" font-family="ui-monospace, monospace">Snow-line ${snowLineM}m</text>
    `;
  }

  // Waterway crossing markers along bottom axis
  const crossingMarkersHtml = crossings
    .filter((c) => typeof c.dist_m === "number" && c.dist_m >= 0 && c.dist_m <= totalDistM)
    .map((c) => {
      const cx = toX(c.dist_m!).toFixed(1);
      const label = escapeHtml(c.name ?? "Water crossing");
      return `
        <line x1="${cx}" y1="${padT}" x2="${cx}" y2="${baseY}" stroke="rgba(56, 189, 248, 0.35)" stroke-width="1" stroke-dasharray="2 2"/>
        <circle cx="${cx}" cy="${baseY}" r="3.2" fill="#38BDF8" stroke="#0B1117" stroke-width="1"><title>${label} (${(c.dist_m! / 1000).toFixed(2)} km)</title></circle>
      `;
    })
    .join("");

  const midDistKm = (totalDistM / 2000).toFixed(1);
  const totalDistKm = (totalDistM / 1000).toFixed(1);

  const svgHtml = `
    <svg class="tf-elev-svg" viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-label="Track elevation profile from ${Math.round(rawMinE)} m to ${Math.round(rawMaxE)} m over ${totalDistKm} km">
      <defs>
        <linearGradient id="tfElevGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#38BDF8" stop-opacity="0.28"/>
          <stop offset="100%" stop-color="#0E151B" stop-opacity="0.02"/>
        </linearGradient>
      </defs>
      ${gridHtml}
      ${steepBandsHtml}
      ${snowLineHtml}
      <path d="${areaPath}" fill="url(#tfElevGrad)"/>
      ${segLinesHtml}
      ${crossingMarkersHtml}
      ${steepMarkersHtml}
      <line x1="${padL}" y1="${baseY}" x2="${width - padR}" y2="${baseY}" stroke="#324659" stroke-width="1"/>
      <text x="${padL}" y="${height - 5}" text-anchor="start" fill="#6E8396" font-size="9.5" font-family="ui-monospace, monospace">0 km</text>
      <text x="${(padL + plotW / 2).toFixed(1)}" y="${height - 5}" text-anchor="middle" fill="#6E8396" font-size="9.5" font-family="ui-monospace, monospace">${midDistKm} km</text>
      <text x="${width - padR}" y="${height - 5}" text-anchor="end" fill="#6E8396" font-size="9.5" font-family="ui-monospace, monospace">${totalDistKm} km</text>
    </svg>
  `;

  return {
    svgHtml,
    minElevM: rawMinE,
    maxElevM: rawMaxE,
    totalDistM,
    hasSnowLineIntersection,
    steepPointCount: steepPoints.length,
    steepZoneCount: steepZones.length,
  };
}
