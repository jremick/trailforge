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
  canonicalSegments: SegmentSummaryItem[];
  courseVertices: TrackCourseVertex[];
  courseBBox: [number, number, number, number] | null;
  hasMultipleCourseSections: boolean;
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

  // Build deduplicated, spatially chained canonical corridor when segment geometries exist.
  // Eliminates multi-tile duplicate segments (e.g. `_2` overlapping tiles & parallel OSM/state
  // double-lines) that otherwise inflate length and cause elevation/course cursor flickering.
  const corridor = buildCanonicalTrackCorridor(
    segments,
    detail.elevation_profile ?? [],
    detail.waterway_crossings ?? []
  );

  const canonicalSegments =
    corridor.segments.length > 0 ? corridor.segments : segments;

  // Synthesize an elevation profile from ordered segments if explicit profile missing
  let profile: ElevationProfilePoint[] = corridor.profile;
  if (profile.length === 0 && canonicalSegments.length > 0) {
    let cumDist = 0;
    const built: ElevationProfilePoint[] = [];
    for (const seg of canonicalSegments) {
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

  const crossings = corridor.crossings;

  // Reconcile Track header stats with canonical segments & profile so multi-tile tracks
  // never show stale single-tile sub-ranges or double-counted tile overlap duplicates.
  const elevCandidatesMin: number[] = [];
  const elevCandidatesMax: number[] = [];
  if (!corridor.rebuiltFromSegments) {
    if (typeof detail.min_elev_m === "number") elevCandidatesMin.push(detail.min_elev_m);
    else if (typeof detail.elev_min_m === "number") elevCandidatesMin.push(detail.elev_min_m);
    if (typeof detail.max_elev_m === "number") elevCandidatesMax.push(detail.max_elev_m);
    else if (typeof detail.elev_max_m === "number") elevCandidatesMax.push(detail.elev_max_m);
  }

  let segLengthSumM = 0;
  let segGainSumM = 0;
  let segLossSumM = 0;
  let segMaxSlope: number | null = null;

  for (const seg of canonicalSegments) {
    if (typeof seg.length_m === "number" && Number.isFinite(seg.length_m)) {
      segLengthSumM += seg.length_m;
    }
    if (typeof seg.elev_min_m === "number" && Number.isFinite(seg.elev_min_m)) {
      elevCandidatesMin.push(seg.elev_min_m);
    }
    if (typeof seg.elev_max_m === "number" && Number.isFinite(seg.elev_max_m)) {
      elevCandidatesMax.push(seg.elev_max_m);
    }
    if (typeof seg.elev_gain_m === "number" && Number.isFinite(seg.elev_gain_m)) {
      segGainSumM += seg.elev_gain_m;
    }
    if (typeof seg.elev_loss_m === "number" && Number.isFinite(seg.elev_loss_m)) {
      segLossSumM += seg.elev_loss_m;
    }
    if (typeof seg.max_slope_deg === "number" && Number.isFinite(seg.max_slope_deg)) {
      segMaxSlope = segMaxSlope === null ? seg.max_slope_deg : Math.max(segMaxSlope, seg.max_slope_deg);
    }
  }

  let profileMaxDistM = 0;
  for (const pt of profile) {
    if (typeof pt.elev_m === "number" && Number.isFinite(pt.elev_m)) {
      elevCandidatesMin.push(pt.elev_m);
      elevCandidatesMax.push(pt.elev_m);
    }
    if (typeof pt.dist_m === "number" && Number.isFinite(pt.dist_m)) {
      profileMaxDistM = Math.max(profileMaxDistM, pt.dist_m);
    }
    if (typeof pt.slope_deg === "number" && Number.isFinite(pt.slope_deg)) {
      segMaxSlope = segMaxSlope === null ? pt.slope_deg : Math.max(segMaxSlope, pt.slope_deg);
    }
  }

  const minElevM =
    elevCandidatesMin.length > 0 ? Math.min(...elevCandidatesMin) : null;
  const maxElevM =
    elevCandidatesMax.length > 0 ? Math.max(...elevCandidatesMax) : null;

  const is4wdOnly = Boolean(detail.is_4wd_only) || segments.some((s) => Boolean(s.is_4wd_only));
  const hasGateBarrier = Boolean(detail.has_gate_barrier) || segments.some((s) => Boolean(s.has_gate_barrier));
  const accessStatus =
    [detail.access_status, ...segments.map((s) => s.access_status)]
      .find((access) => access && access !== "public") ??
    "public";

  const elevGainM = corridor.rebuiltFromSegments
    ? segGainSumM
    : canonicalSegments.length > 0
      ? Math.max(detail.elev_gain_m ?? 0, segGainSumM)
      : detail.elev_gain_m;

  const elevLossM = corridor.rebuiltFromSegments
    ? segLossSumM
    : typeof detail.elev_loss_m === "number"
      ? canonicalSegments.length > 0
        ? Math.max(detail.elev_loss_m, segLossSumM)
        : detail.elev_loss_m
      : canonicalSegments.length > 0
        ? segLossSumM
        : null;

  const sustainedSlopeDeg = segments.reduce((best, s) => {
    const slope = s.sustained_slope_deg;
    return typeof slope === "number" && Number.isFinite(slope)
      ? Math.max(best ?? slope, slope)
      : best;
  }, detail.sustained_slope_deg ?? null);

  const slopeCandidates = [
    detail.max_slope_deg,
    segMaxSlope,
    sustainedSlopeDeg,
    breakdown?.factors?.max_slope?.max_slope_deg,
  ].filter((v): v is number => typeof v === "number" && Number.isFinite(v));
  const maxSlopeDeg =
    slopeCandidates.length > 0 ? Math.max(...slopeCandidates) : null;

  const courseLengthM = corridor.vertices.at(-1)?.distM ?? 0;
  const reconciledLengthM = Math.max(
    corridor.rebuiltFromSegments ? 0 : detail.total_length_m || 0,
    segLengthSumM,
    profileMaxDistM,
    courseLengthM
  );
  const waterwayCrossingCount = Math.max(
    detail.waterway_crossing_count || 0,
    crossings.length
  );

  return {
    trackId: detail.track_id,
    name: detail.name,
    altNames: detail.alt_names ?? [],
    state: detail.state,
    grade,
    score: typeof score === "number" ? score : null,
    totalLengthKm: reconciledLengthM / 1000,
    minElevM,
    maxElevM,
    elevGainM,
    elevLossM,
    maxSlopeDeg,
    sustainedSlopeDeg,
    waterwayCrossingCount,
    is4wdOnly,
    hasGateBarrier,
    accessStatus,
    parks,
    breakdown,
    profile,
    crossings,
    canonicalSegments,
    courseVertices: corridor.vertices,
    courseBBox: corridor.bbox,
    hasMultipleCourseSections: corridor.vertices[0]?.polylineId !== corridor.vertices.at(-1)?.polylineId,
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
  detailed?: boolean;
  gradientId?: string;
}

export interface ElevationSteepZone {
  index: number;
  startDistM: number;
  endDistM: number;
  peakDistM: number;
  peakElevM: number;
  peakSlopeDeg: number;
  grade: string;
}

export interface ElevationChartGeometry {
  width: number;
  height: number;
  padL: number;
  padR: number;
  padT: number;
  padB: number;
  plotW: number;
  plotH: number;
  yMin: number;
  yMax: number;
  totalDistM: number;
}

export interface ElevationChartResult {
  svgHtml: string;
  minElevM: number;
  maxElevM: number;
  totalDistM: number;
  hasSnowLineIntersection: boolean;
  steepPointCount: number;
  steepZoneCount: number;
  steepZones: ElevationSteepZone[];
  geometry: ElevationChartGeometry | null;
}

export interface ElevationSampleReadout {
  distM: number;
  elevM: number;
  slopeDeg: number | null;
  slopePct: number | null;
  grade: string;
  segmentId: string | null;
}

export interface GradeDistributionItem {
  grade: string;
  color: string;
  lengthM: number;
  pct: number;
  segmentCount: number;
}

/**
 * Sample elevation, slope, and grade at an arbitrary distance along the profile
 * via binary search + linear elevation interpolation (for interactive chart scrubbing).
 */
export function sampleElevationAtDistance(
  points: readonly ElevationProfilePoint[],
  targetDistM: number
): ElevationSampleReadout | null {
  if (points.length === 0) return null;
  const clamped = Math.max(
    points[0]!.dist_m,
    Math.min(points[points.length - 1]!.dist_m, targetDistM)
  );
  if (points.length === 1) {
    const p = points[0]!;
    const deg = typeof p.slope_deg === "number" ? p.slope_deg : null;
    return {
      distM: p.dist_m,
      elevM: p.elev_m,
      slopeDeg: deg,
      slopePct: deg !== null ? Math.tan((deg * Math.PI) / 180) * 100 : null,
      grade: p.grade ?? "Easy",
      segmentId: p.segment_id ?? null,
    };
  }

  let lo = 0;
  let hi = points.length - 1;
  while (lo + 1 < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid]!.dist_m <= clamped) lo = mid;
    else hi = mid;
  }

  const a = points[lo]!;
  const b = points[hi]!;
  const span = Math.max(1e-6, b.dist_m - a.dist_m);
  const t = Math.max(0, Math.min(1, (clamped - a.dist_m) / span));
  const nearer = t < 0.5 ? a : b;
  const elevM = a.section_id === b.section_id
    ? a.elev_m + (b.elev_m - a.elev_m) * t
    : nearer.elev_m;
  const slopeDeg =
    typeof nearer.slope_deg === "number"
      ? nearer.slope_deg
      : typeof a.slope_deg === "number"
        ? a.slope_deg
        : typeof b.slope_deg === "number"
          ? b.slope_deg
          : null;
  const slopePct =
    slopeDeg !== null ? Math.tan((slopeDeg * Math.PI) / 180) * 100 : null;

  return {
    distM: clamped,
    elevM,
    slopeDeg,
    slopePct,
    grade: nearer.grade ?? a.grade ?? b.grade ?? "Easy",
    segmentId: nearer.segment_id ?? a.segment_id ?? b.segment_id ?? null,
  };
}

/**
 * Compute distance-weighted grade distribution across Easy / Medium / Hard / Extreme.
 */
export function computeTrackGradeDistribution(
  segments: readonly SegmentSummaryItem[],
  profile: readonly ElevationProfilePoint[]
): GradeDistributionItem[] {
  const order = ["Easy", "Medium", "Hard", "Extreme"] as const;
  const lengths: Record<string, number> = { Easy: 0, Medium: 0, Hard: 0, Extreme: 0 };
  const counts: Record<string, number> = { Easy: 0, Medium: 0, Hard: 0, Extreme: 0 };

  const normalizeGrade = (raw: string | null | undefined): string => {
    const low = String(raw ?? "Easy").trim().toLowerCase();
    if (low === "extreme") return "Extreme";
    if (low === "hard") return "Hard";
    if (low === "medium") return "Medium";
    return "Easy";
  };

  if (segments.length > 0) {
    for (const seg of segments) {
      const g = normalizeGrade(extractSegmentGrade(seg));
      const len = typeof seg.length_m === "number" && seg.length_m > 0 ? seg.length_m : 75;
      lengths[g] = (lengths[g] ?? 0) + len;
      counts[g] = (counts[g] ?? 0) + 1;
    }
  } else if (profile.length >= 2) {
    for (let i = 1; i < profile.length; i += 1) {
      const prev = profile[i - 1]!;
      const curr = profile[i]!;
      const g = normalizeGrade(curr.grade ?? prev.grade);
      const len = Math.max(0, curr.dist_m - prev.dist_m);
      lengths[g] = (lengths[g] ?? 0) + len;
      counts[g] = (counts[g] ?? 0) + 1;
    }
  }

  const totalM = Object.values(lengths).reduce((a, b) => a + b, 0);
  if (totalM <= 0) return [];

  return order
    .filter((g) => (lengths[g] ?? 0) > 0)
    .map((g) => ({
      grade: g,
      color: getGradeColor(g),
      lengthM: lengths[g] ?? 0,
      pct: Math.round(((lengths[g] ?? 0) / totalM) * 1000) / 10,
      segmentCount: counts[g] ?? 0,
    }));
}

/**
 * Pure SVG elevation profile generator for the Track Inspector Drawer and
 * Large-Scale Elevation Profile Pop-Over Modal.
 */
export function buildElevationProfileSvg(
  points: readonly ElevationProfilePoint[],
  options?: ElevationChartOptions
): ElevationChartResult {
  const detailed = Boolean(options?.detailed);
  const width = options?.width ?? (detailed ? 920 : 340);
  const height = options?.height ?? (detailed ? 290 : 138);
  const snowLineM = options?.snowLineElevationM ?? 1000;
  const steepThreshold = options?.steepThresholdDeg ?? 14.0;
  const crossings = options?.crossings ?? [];
  const gradId = options?.gradientId ?? (detailed ? "tfElevGradModal" : "tfElevGrad");

  if (points.length < 2) {
    return {
      svgHtml: `<div class="tf-elev-empty">No elevation samples available for this track.</div>`,
      minElevM: 0,
      maxElevM: 0,
      totalDistM: 0,
      hasSnowLineIntersection: false,
      steepPointCount: 0,
      steepZoneCount: 0,
      steepZones: [],
      geometry: null,
    };
  }

  const padL = detailed ? 54 : 38;
  const padR = detailed ? 18 : 10;
  const padT = detailed ? 18 : 12;
  const padB = detailed ? 30 : 22;
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

  const profileSections: ElevationProfilePoint[][] = [];
  for (const point of points) {
    const previous = profileSections.at(-1);
    if (previous && previous[0]!.section_id === point.section_id) previous.push(point);
    else profileSections.push([point]);
  }
  const baseY = (padT + plotH).toFixed(1);
  const areaPath = profileSections.map((section) => {
    const firstX = toX(section[0]!.dist_m).toFixed(1);
    const lastX = toX(section.at(-1)!.dist_m).toFixed(1);
    const line = section.map((p) => `L ${toX(p.dist_m).toFixed(1)},${toY(p.elev_m).toFixed(1)}`).join(" ");
    return `M ${firstX},${baseY} ${line} L ${lastX},${baseY} Z`;
  }).join(" ");

  const gradeRuns: { color: string; coords: string[]; sectionId?: number }[] = [];
  for (let i = 1; i < points.length; i += 1) {
    const prev = points[i - 1]!;
    const curr = points[i]!;
    if (prev.section_id !== curr.section_id) continue;
    const color = getGradeColor(curr.grade ?? prev.grade);
    const p1 = `${toX(prev.dist_m).toFixed(1)},${toY(prev.elev_m).toFixed(1)}`;
    const p2 = `${toX(curr.dist_m).toFixed(1)},${toY(curr.elev_m).toFixed(1)}`;
    const lastRun = gradeRuns[gradeRuns.length - 1];
    if (lastRun && lastRun.sectionId === curr.section_id && lastRun.color === color) {
      lastRun.coords.push(p2);
    } else {
      gradeRuns.push({ color, coords: [p1, p2], sectionId: curr.section_id });
    }
  }

  const strokeW = detailed ? "3.2" : "2.6";
  const segLinesHtml = gradeRuns
    .map(
      (run) =>
        `<polyline points="${run.coords.join(" ")}" fill="none" stroke="${run.color}" stroke-width="${strokeW}" stroke-linecap="round" stroke-linejoin="round"/>`
    )
    .join("");

  const steepPoints = points.filter(
    (p) => typeof p.slope_deg === "number" && p.slope_deg >= steepThreshold
  );
  interface RawSteepZone {
    startDistM: number;
    endDistM: number;
    peak: ElevationProfilePoint;
  }
  const rawZones: RawSteepZone[] = [];
  let activeZone: RawSteepZone | null = null;

  for (const p of points) {
    if (activeZone && activeZone.peak.section_id !== p.section_id) {
      rawZones.push(activeZone);
      activeZone = null;
    }
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
      rawZones.push(activeZone);
      activeZone = null;
    }
  }
  if (activeZone) {
    rawZones.push(activeZone);
  }

  const steepZones: ElevationSteepZone[] = rawZones.map((z, idx) => ({
    index: idx,
    startDistM: z.startDistM,
    endDistM: z.endDistM,
    peakDistM: z.peak.dist_m,
    peakElevM: z.peak.elev_m,
    peakSlopeDeg: z.peak.slope_deg ?? steepThreshold,
    grade: z.peak.grade ?? "Extreme",
  }));

  const steepBandsHtml = rawZones
    .map((z) => {
      const x1 = toX(z.startDistM);
      const x2 = Math.max(x1 + (detailed ? 4 : 2.5), toX(z.endDistM));
      return `<rect x="${x1.toFixed(1)}" y="${padT}" width="${(x2 - x1).toFixed(1)}" height="${plotH}" fill="rgba(239, 68, 68, ${detailed ? "0.14" : "0.12"})"/>`;
    })
    .join("");

  const markerR = detailed ? "4.6" : "3.4";
  const steepMarkersHtml = rawZones
    .map((z, idx) => {
      const p = z.peak;
      const cx = toX(p.dist_m).toFixed(1);
      const cy = toY(p.elev_m).toFixed(1);
      const markerColor = getGradeColor(p.grade);
      return `<circle cx="${cx}" cy="${cy}" r="${markerR}" fill="${markerColor}" stroke="#F8FAFC" stroke-width="${detailed ? "1.6" : "1.2"}" data-steep-idx="${idx}"><title>Peak steep ${p.slope_deg?.toFixed(1)}° at ${(p.dist_m / 1000).toFixed(2)} km (${Math.round(p.elev_m)} m)</title></circle>`;
    })
    .join("");

  // Horizontal elevation gridlines (5 ticks for detailed modal, 3 ticks for compact drawer)
  const yTickCount = detailed ? 5 : 3;
  const yTicks: number[] = [];
  for (let i = 0; i < yTickCount; i += 1) {
    yTicks.push(Math.round(yMin + ((yMax - yMin) * i) / (yTickCount - 1)));
  }
  const axisFontSize = detailed ? "11" : "9.5";
  const gridHtml = yTicks
    .map((e) => {
      const y = toY(e).toFixed(1);
      return `
        <line x1="${padL}" y1="${y}" x2="${width - padR}" y2="${y}" stroke="#233240" stroke-width="1" stroke-dasharray="2 2"/>
        <text x="${padL - 6}" y="${Number(y) + 3.5}" text-anchor="end" fill="#7F95AA" font-size="${axisFontSize}" font-family="ui-monospace, monospace">${Math.round(e)}m</text>
      `;
    })
    .join("");

  // Vertical distance gridlines & x-axis ticks
  const xTickCount = detailed ? 6 : 3;
  const xTicksHtml: string[] = [];
  for (let i = 0; i < xTickCount; i += 1) {
    const frac = i / (xTickCount - 1);
    const distM = totalDistM * frac;
    const x = toX(distM);
    const anchor = i === 0 ? "start" : i === xTickCount - 1 ? "end" : "middle";
    const label = i === 0 ? "0 km" : `${(distM / 1000).toFixed(1)} km`;
    if (detailed && i > 0 && i < xTickCount - 1) {
      xTicksHtml.push(
        `<line x1="${x.toFixed(1)}" y1="${padT}" x2="${x.toFixed(1)}" y2="${baseY}" stroke="#1C2A36" stroke-width="1" stroke-dasharray="2 3"/>`
      );
    }
    xTicksHtml.push(
      `<text x="${x.toFixed(1)}" y="${height - (detailed ? 8 : 5)}" text-anchor="${anchor}" fill="#7F95AA" font-size="${axisFontSize}" font-family="ui-monospace, monospace">${label}</text>`
    );
  }

  // Snow-line horizontal reference line when inside chart range
  const hasSnowLineIntersection = snowLineM >= yMin && snowLineM <= yMax;
  let snowLineHtml = "";
  if (hasSnowLineIntersection) {
    const sy = toY(snowLineM);
    const labelY = Math.max(padT + (detailed ? 12 : 9), sy - 4);
    snowLineHtml = `
      <rect x="${padL}" y="${padT}" width="${plotW}" height="${Math.max(0, sy - padT).toFixed(1)}" fill="rgba(186, 230, 253, 0.09)"/>
      <line x1="${padL}" y1="${sy.toFixed(1)}" x2="${width - padR}" y2="${sy.toFixed(1)}" stroke="#7DD3FC" stroke-width="${detailed ? "1.5" : "1.3"}" stroke-dasharray="5 3"/>
      <text x="${width - padR - 4}" y="${labelY.toFixed(1)}" text-anchor="end" fill="#7DD3FC" font-size="${detailed ? "10.5" : "9"}" font-family="ui-monospace, monospace">Snow-line ${snowLineM}m</text>
    `;
  }

  // Waterway crossing markers along bottom axis
  const crossingMarkersHtml = crossings
    .filter((c) => typeof c.dist_m === "number" && c.dist_m >= 0 && c.dist_m <= totalDistM)
    .map((c) => {
      const cx = toX(c.dist_m!).toFixed(1);
      const label = escapeHtml(c.name ?? "Water crossing");
      return `
        <line x1="${cx}" y1="${padT}" x2="${cx}" y2="${baseY}" stroke="rgba(56, 189, 248, ${detailed ? "0.42" : "0.35"})" stroke-width="1" stroke-dasharray="2 2"/>
        <circle cx="${cx}" cy="${baseY}" r="${detailed ? "4.4" : "3.2"}" fill="#38BDF8" stroke="#0B1117" stroke-width="1.2"><title>${label} (${(c.dist_m! / 1000).toFixed(2)} km)</title></circle>
      `;
    })
    .join("");

  // Interactive scrub crosshair elements for the detailed pop-over chart
  const scrubOverlayHtml = detailed
    ? `
      <g class="tf-elev-crosshair hidden" data-role="elev-crosshair" pointer-events="none">
        <line data-role="crosshair-line" x1="${padL}" y1="${padT}" x2="${padL}" y2="${baseY}" stroke="#FDE047" stroke-width="1.5" stroke-dasharray="3 2"/>
        <circle data-role="crosshair-halo" cx="${padL}" cy="${baseY}" r="9" fill="rgba(253, 224, 71, 0.28)"/>
        <circle data-role="crosshair-dot" cx="${padL}" cy="${baseY}" r="4.8" fill="#FDE047" stroke="#070B10" stroke-width="2"/>
      </g>
      <rect class="tf-elev-scrub-hit" data-role="elev-scrub-hit" x="${padL}" y="${padT}" width="${plotW}" height="${plotH}" fill="transparent"/>
    `
    : "";

  const totalDistKm = (totalDistM / 1000).toFixed(1);

  const svgHtml = `
    <svg class="tf-elev-svg ${detailed ? "tf-elev-svg-lg" : ""}" viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-label="Track elevation profile from ${Math.round(rawMinE)} m to ${Math.round(rawMaxE)} m over ${totalDistKm} km">
      <defs>
        <linearGradient id="${gradId}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#38BDF8" stop-opacity="0.28"/>
          <stop offset="100%" stop-color="#0E151B" stop-opacity="0.02"/>
        </linearGradient>
      </defs>
      ${gridHtml}
      ${steepBandsHtml}
      ${snowLineHtml}
      <path d="${areaPath}" fill="url(#${gradId})"/>
      ${segLinesHtml}
      ${crossingMarkersHtml}
      ${steepMarkersHtml}
      <line x1="${padL}" y1="${baseY}" x2="${width - padR}" y2="${baseY}" stroke="#324659" stroke-width="1"/>
      ${xTicksHtml.join("")}
      ${scrubOverlayHtml}
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
    steepZones,
    geometry: {
      width,
      height,
      padL,
      padR,
      padT,
      padB,
      plotW,
      plotH,
      yMin,
      yMax,
      totalDistM,
    },
  };
}

export interface TrackCourseVertex {
  distM: number;
  lon: number;
  lat: number;
  x: number;
  y: number;
  grade: string;
  segmentId: string;
  polylineId: number;
}

export interface TrackCoursePoint {
  distM: number;
  lon: number;
  lat: number;
  x: number;
  y: number;
  grade: string;
  segmentId: string;
}

export interface TrackCourseSvgResult {
  svgHtml: string;
  vertices: TrackCourseVertex[];
  totalDistM: number;
  width: number;
  height: number;
  bbox: [number, number, number, number] | null;
  scaleBarLabel: string;
}

function haversineMeters(
  lon1: number,
  lat1: number,
  lon2: number,
  lat2: number
): number {
  const rad = Math.PI / 180;
  const dLat = (lat2 - lat1) * rad;
  const dLon = (lon2 - lon1) * rad;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
  return 6371000 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function extractSegmentCoordinateLines(
  geometry: SegmentSummaryItem["geometry"]
): Array<Array<[number, number]>> {
  if (!geometry || typeof geometry !== "object" || !Array.isArray(geometry.coordinates)) {
    return [];
  }
  const parseLine = (raw: unknown): Array<[number, number]> => {
    if (!Array.isArray(raw)) return [];
    const pts: Array<[number, number]> = [];
    for (const pt of raw) {
      if (
        Array.isArray(pt) &&
        pt.length >= 2 &&
        typeof pt[0] === "number" &&
        typeof pt[1] === "number" &&
        Number.isFinite(pt[0]) &&
        Number.isFinite(pt[1])
      ) {
        pts.push([pt[0], pt[1]]);
      }
    }
    return pts;
  };

  if (geometry.type === "LineString") {
    const line = parseLine(geometry.coordinates);
    return line.length >= 2 ? [line] : [];
  }
  if (geometry.type === "MultiLineString") {
    const lines: Array<Array<[number, number]>> = [];
    for (const part of geometry.coordinates) {
      const line = parseLine(part);
      if (line.length >= 2) lines.push(line);
    }
    return lines;
  }
  return [];
}

function chooseNiceScaleBarMeters(targetM: number): { meters: number; label: string } {
  const candidates = [100, 200, 250, 500, 1000, 2000, 5000, 10000, 20000, 50000];
  let chosen = candidates[0]!;
  for (const c of candidates) {
    if (c <= targetM * 1.15) chosen = c;
  }
  const label = chosen >= 1000 ? `${chosen / 1000} km` : `${chosen} m`;
  return { meters: chosen, label };
}

function polylineArcLength(coords: readonly [number, number][]): number {
  let total = 0;
  for (let i = 1; i < coords.length; i += 1) {
    const a = coords[i - 1]!;
    const b = coords[i]!;
    total += haversineMeters(a[0], a[1], b[0], b[1]);
  }
  return total;
}

function polylinePointAtFraction(
  coords: readonly [number, number][],
  total: number,
  fraction: number
): [number, number] {
  if (coords.length === 0) return [0, 0];
  if (coords.length === 1) return coords[0]!;
  if (total <= 1e-6) {
    const a = coords[0]!;
    const b = coords[coords.length - 1]!;
    return [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  }
  const target = total * fraction;
  let acc = 0;
  for (let i = 1; i < coords.length; i += 1) {
    const a = coords[i - 1]!;
    const b = coords[i]!;
    const step = haversineMeters(a[0], a[1], b[0], b[1]);
    if (acc + step >= target && step > 1e-6) {
      const t = (target - acc) / step;
      return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
    }
    acc += step;
  }
  return coords[coords.length - 1]!;
}

function parseSegmentElevProfile(
  raw: unknown[] | undefined
): Array<[number, number]> {
  if (!Array.isArray(raw) || raw.length < 2) return [];
  const out: Array<[number, number]> = [];
  for (const pt of raw) {
    if (
      Array.isArray(pt) &&
      pt.length >= 2 &&
      typeof pt[0] === "number" &&
      typeof pt[1] === "number" &&
      Number.isFinite(pt[0]) &&
      Number.isFinite(pt[1])
    ) {
      out.push([pt[0], pt[1]]);
    }
  }
  return out.length >= 2 ? out : [];
}

interface CanonicalTrackCorridorResult {
  segments: SegmentSummaryItem[];
  profile: ElevationProfilePoint[];
  crossings: WaterwayCrossingItem[];
  vertices: TrackCourseVertex[];
  bbox: [number, number, number, number] | null;
  rebuiltFromSegments: boolean;
}

/**
 * Deduplicate overlapping source geometry and retain every distinct track segment.
 * Junctions divide the result into separate sections so loops and branches stay visible
 * without drawing an invented connection across the course or elevation profile.
 */
export function buildCanonicalTrackCorridor(
  segments: readonly SegmentSummaryItem[],
  fallbackProfile: readonly ElevationProfilePoint[],
  rawCrossings: readonly WaterwayCrossingItem[]
): CanonicalTrackCorridorResult {
  interface GeomSeg {
    seg: SegmentSummaryItem;
    coords: Array<[number, number]>;
    start: [number, number];
    interior: Array<[number, number]>;
    end: [number, number];
    arcLenM: number;
    elevPts: Array<[number, number]>;
  }

  const geomSegs: GeomSeg[] = [];
  const seenSegIds = new Set<string>();
  for (const seg of segments) {
    if (seenSegIds.has(seg.segment_id)) continue;
    seenSegIds.add(seg.segment_id);
    const lines = extractSegmentCoordinateLines(seg.geometry);
    if (lines.length === 0) continue;
    // The API gives canonical segments as LineStrings. Use the longest
    // connected line when older offline payloads supply MultiLineStrings.
    const { coords, arcLenM } = lines.map((coords) => ({
      coords,
      arcLenM: polylineArcLength(coords),
    })).sort((a, b) => b.arcLenM - a.arcLenM)[0]!;
    if (coords.length < 2) continue;
    geomSegs.push({
      seg,
      coords,
      start: coords[0]!,
      interior: [0.25, 0.5, 0.75].map((frac) => polylinePointAtFraction(coords, arcLenM, frac)),
      end: coords[coords.length - 1]!,
      arcLenM,
      elevPts: parseSegmentElevProfile(seg.elevation_profile_m),
    });
  }

  if (geomSegs.length === 0) {
    return {
      segments: [...segments],
      profile: [...fallbackProfile],
      crossings: [...rawCrossings],
      vertices: [],
      bbox: null,
      rebuiltFromSegments: false,
    };
  }

  // Step 1: Spatial deduplication — prefer segments with elevation profiles, without numeric
  // multi-tile collision suffixes (`_2`, `_3`), and primary OSM geometry.
  const segPriority = (g: GeomSeg): number => {
    const hasProf = g.elevPts.length >= 2 ? 1000 : 0;
    const noDupSuffix = /_\d+$/.test(g.seg.segment_id) ? 0 : 100;
    const isOsm = g.seg.primary_source === "osm" ? 10 : 0;
    return hasProf + noDupSuffix + isOsm + Math.min(5, g.arcLenM / 100);
  };

  const kept: GeomSeg[] = [];
  const duplicateOf = new Map<string, { id: string; reversed: boolean }>();
  const sortedByPrio = [...geomSegs].sort((a, b) => segPriority(b) - segPriority(a));

  for (const cand of sortedByPrio) {
    let match: { id: string; reversed: boolean } | undefined;
    for (const k of kept) {
      const toleranceM = Math.min(18, Math.max(4, Math.min(cand.arcLenM, k.arcLenM) * 0.2));
      if (Math.abs(cand.arcLenM - k.arcLenM) > toleranceM) continue;
      const candidateSamples = [cand.start, ...cand.interior, cand.end];
      const keptSamples = [k.start, ...k.interior, k.end];
      const matches = (reversed: boolean): boolean => candidateSamples.every(([lon, lat], idx) => {
        const [otherLon, otherLat] = keptSamples[reversed ? keptSamples.length - idx - 1 : idx]!;
        return haversineMeters(lon, lat, otherLon, otherLat) < toleranceM;
      });
      const reversed = !matches(false);
      if (reversed && !matches(true)) continue;
      match = { id: k.seg.segment_id, reversed };
      break;
    }
    if (match) {
      duplicateOf.set(cand.seg.segment_id, match);
    } else {
      kept.push(cand);
    }
  }

  // Join neighboring endpoints, and stop each section at forks. A closed
  // component has no open endpoint, so a second pass also walks the loops.
  const nodes: Array<[number, number]> = [];
  const adjacency: number[][] = [];
  const getNode = (pt: [number, number]): number => {
    let bestIdx = -1;
    let bestDist = 28;
    for (let i = 0; i < nodes.length; i += 1) {
      const n = nodes[i]!;
      const d = haversineMeters(pt[0], pt[1], n[0], n[1]);
      if (d < bestDist) {
        bestDist = d;
        bestIdx = i;
      }
    }
    if (bestIdx >= 0) return bestIdx;
    nodes.push(pt);
    adjacency.push([]);
    return nodes.length - 1;
  };

  const edges = kept.map((g, idx) => {
    const start = getNode(g.start);
    const end = getNode(g.end);
    adjacency[start]!.push(idx);
    adjacency[end]!.push(idx);
    return { g, start, end };
  });
  const available = new Set(edges.keys());

  interface OrderedStep {
    g: GeomSeg;
    reversed: boolean;
  }

  const sections: Array<{ steps: OrderedStep[]; lengthM: number }> = [];
  const walkSection = (startNode: number, startEdge: number): void => {
    const steps: OrderedStep[] = [];
    let node = startNode;
    let edgeIdx: number | undefined = startEdge;
    let lengthM = 0;
    while (edgeIdx !== undefined && available.delete(edgeIdx)) {
      const edge = edges[edgeIdx]!;
      const reversed = node === edge.end && node !== edge.start;
      steps.push({ g: edge.g, reversed });
      lengthM += edge.g.seg.length_m ?? edge.g.arcLenM;
      node = reversed ? edge.start : edge.end;
      const next = adjacency[node]!;
      edgeIdx = next.length === 2 ? next.find((id) => available.has(id)) : undefined;
    }
    if (steps.length === 0) return;
    const first = steps[0]!;
    const last = steps[steps.length - 1]!;
    const firstSeq = first.g.seg.segment_seq;
    const lastSeq = last.g.seg.segment_seq;
    const firstCoord = first.reversed ? first.g.end : first.g.start;
    const lastCoord = last.reversed ? last.g.start : last.g.end;
    const shouldFlip = steps.length > 1 && (
      typeof firstSeq === "number" &&
      typeof lastSeq === "number" &&
      firstSeq !== lastSeq
        ? firstSeq > lastSeq
        : firstCoord[1] > lastCoord[1]
    );
    if (shouldFlip) {
      steps.reverse();
      for (const step of steps) step.reversed = !step.reversed;
    }
    sections.push({ steps, lengthM });
  };

  adjacency.forEach((edgesAtNode, node) => {
    if (edgesAtNode.length === 2) return;
    for (const edgeIdx of edgesAtNode) walkSection(node, edgeIdx);
  });
  for (const edgeIdx of available) walkSection(edges[edgeIdx]!.start, edgeIdx);

  sections.sort((a, b) => b.lengthM - a.lengthM);
  const orderedSteps = sections.flatMap(({ steps }, idx) => {
    return steps.map((step) => ({ ...step, section: idx + 1 }));
  });

  // Use the server's original cumulative segment intervals to locate crossings.
  // The server currently reports a segment midpoint, including segments with no DEM.
  const rawRanges: Array<{ id: string; start: number; length: number }> = [];
  const geometryLength = new Map(geomSegs.map((g) => [g.seg.segment_id, g.arcLenM]));
  let rawDistance = 0;
  for (const seg of segments) {
    const length = seg.length_m ?? geometryLength.get(seg.segment_id) ?? 0;
    if (!Number.isFinite(length) || length <= 0) continue;
    rawRanges.push({ id: seg.segment_id, start: rawDistance, length });
    rawDistance += length;
  }
  const rawRangeById = new Map(rawRanges.map((range) => [range.id, range]));
  const fallbackSamples = new Map<string, Array<[number, number]>>();
  for (const pt of fallbackProfile) {
    const range = pt.segment_id ? rawRangeById.get(pt.segment_id) : undefined;
    if (!range) continue;
    const list = fallbackSamples.get(range.id) ?? [];
    list.push([pt.dist_m - range.start, pt.elev_m]);
    fallbackSamples.set(range.id, list);
  }

  const rebuiltProfile: ElevationProfilePoint[] = [];
  const vertices: TrackCourseVertex[] = [];
  const newRanges = new Map<string, { start: number; length: number; reversed: boolean }>();

  let cumDist = 0;
  let profileSection = 0;
  let previousSection: number | undefined;
  let minLon = Infinity;
  let maxLon = -Infinity;
  let minLat = Infinity;
  let maxLat = -Infinity;

  for (const { g, reversed, section } of orderedSteps) {
    const coords = reversed ? [...g.coords].reverse() : g.coords;
    const length = typeof g.seg.length_m === "number" && g.seg.length_m > 0
      ? g.seg.length_m
      : Math.max(1, g.arcLenM);
    const grade = extractSegmentGrade(g.seg) ?? "Easy";
    newRanges.set(g.seg.segment_id, { start: cumDist, length, reversed });

    const slopeDeg = g.seg.max_slope_deg ?? null;
    const fallback = fallbackSamples.get(g.seg.segment_id) ?? [];
    const elevationM = g.seg.elev_min_m ?? g.seg.elev_max_m;
    const elevationSamples = g.elevPts.length > 0 ? g.elevPts : fallback;
    const connectedProfile = section === previousSection;
    if (!connectedProfile) profileSection += 1;
    const span = g.elevPts.length > 0
      ? Math.max(1e-6, g.elevPts[g.elevPts.length - 1]![0])
      : length;
    const samples = elevationSamples.length > 0
      ? elevationSamples
      : typeof elevationM === "number"
        ? [[0, elevationM], [span, elevationM]]
        : [];
    const orderedSamples = reversed ? [...samples].reverse() : samples;

    for (const [idx, sample] of orderedSamples.entries()) {
      const [localDist, z] = sample;
      const fraction = Math.max(0, Math.min(1, localDist! / span));
      const dist = cumDist + (reversed ? 1 - fraction : fraction) * length;
      if (idx === 0 && connectedProfile && rebuiltProfile.at(-1)?.dist_m === dist) continue;
      rebuiltProfile.push({
        dist_m: dist,
        elev_m: z!,
        slope_deg: slopeDeg,
        grade,
        segment_id: g.seg.segment_id,
        section_id: profileSection,
      });
    }
    previousSection = samples.length > 0 ? section : undefined;

    let arc = 0;
    for (const [idx, [lon, lat]] of coords.entries()) {
      if (idx > 0) {
        const [prevLon, prevLat] = coords[idx - 1]!;
        arc += haversineMeters(prevLon, prevLat, lon, lat);
      }
      const fraction = g.arcLenM > 1e-6 ? arc / g.arcLenM : idx / (coords.length - 1);
      minLon = Math.min(minLon, lon);
      maxLon = Math.max(maxLon, lon);
      minLat = Math.min(minLat, lat);
      maxLat = Math.max(maxLat, lat);
      vertices.push({
        distM: cumDist + fraction * length,
        lon,
        lat,
        x: 0,
        y: 0,
        grade,
        segmentId: g.seg.segment_id,
        polylineId: section,
      });
    }
    cumDist += length;
  }

  const mappedCrossings = rawCrossings.map((crossing) => {
    const dist = crossing.dist_m;
    if (typeof dist !== "number" || !Number.isFinite(dist)) return crossing;
    const raw = rawRanges.find((range) => dist >= range.start && dist <= range.start + range.length);
    if (!raw) return { ...crossing, dist_m: null };
    const duplicate = duplicateOf.get(raw.id);
    const mapped = newRanges.get(duplicate?.id ?? raw.id);
    if (!mapped) return { ...crossing, dist_m: null };
    const fraction = (dist - raw.start) / raw.length;
    const reversed = mapped.reversed !== Boolean(duplicate?.reversed);
    return {
      ...crossing,
      dist_m: Math.round((mapped.start + (reversed ? 1 - fraction : fraction) * mapped.length) * 10) / 10,
    };
  });

  const canonicalSegments = orderedSteps.map(({ g }) => g.seg);
  for (const seg of segments) {
    if (geometryLength.has(seg.segment_id)) continue;
    if (!canonicalSegments.some((canonical) => canonical.segment_id === seg.segment_id)) {
      canonicalSegments.push(seg);
    }
  }
  return {
    segments: canonicalSegments,
    profile: rebuiltProfile.length >= 2 ? rebuiltProfile : [...fallbackProfile],
    crossings: mappedCrossings,
    vertices,
    bbox:
      Number.isFinite(minLon) && Number.isFinite(minLat)
        ? [minLon, minLat, maxLon, maxLat]
        : null,
    rebuiltFromSegments: rebuiltProfile.length >= 2,
  };
}

/**
 * Build a true-aspect-ratio (North-up) interactive SVG plan view of a track's course
 * aligned 1-to-1 by cumulative distance with `elevation_profile`, enabling bidirectional
 * cursor synchronization between the elevation graph and track course (also serves as fallback
 * when WebGL is unavailable for the live MapLibre mini-map).
 */
export function buildTrackCourseSvg(
  segments: readonly SegmentSummaryItem[],
  profile: readonly ElevationProfilePoint[],
  options?: {
    width?: number;
    height?: number;
    crossings?: readonly WaterwayCrossingItem[];
    steepZones?: readonly ElevationSteepZone[];
    precomputedVertices?: readonly TrackCourseVertex[];
    precomputedBBox?: [number, number, number, number] | null;
  }
): TrackCourseSvgResult {
  const width = options?.width ?? 460;
  const height = options?.height ?? 340;
  const crossings = options?.crossings ?? [];
  const steepZones = options?.steepZones ?? [];

  let baseVertices: readonly TrackCourseVertex[];
  let bbox: [number, number, number, number] | null;

  if (options?.precomputedVertices && options.precomputedVertices.length >= 2) {
    baseVertices = options.precomputedVertices;
    bbox = options.precomputedBBox ?? null;
  } else {
    const corridor = buildCanonicalTrackCorridor(segments, profile, crossings);
    baseVertices = corridor.vertices;
    bbox = corridor.bbox;
  }

  if (baseVertices.length < 2) {
    return {
      svgHtml: `<div class="tf-elev-empty">Track course geometry unavailable for plan view.</div>`,
      vertices: [],
      totalDistM: 0,
      width,
      height,
      bbox: null,
      scaleBarLabel: "",
    };
  }

  let minLon = bbox ? bbox[0] : Infinity;
  let minLat = bbox ? bbox[1] : Infinity;
  let maxLon = bbox ? bbox[2] : -Infinity;
  let maxLat = bbox ? bbox[3] : -Infinity;
  if (!bbox) {
    for (const v of baseVertices) {
      if (v.lon < minLon) minLon = v.lon;
      if (v.lon > maxLon) maxLon = v.lon;
      if (v.lat < minLat) minLat = v.lat;
      if (v.lat > maxLat) maxLat = v.lat;
    }
  }

  const midLatRad = (((minLat + maxLat) / 2) * Math.PI) / 180;
  const cosLat = Math.max(0.2, Math.cos(midLatRad));
  const rawW = Math.max(1e-5, (maxLon - minLon) * cosLat);
  const rawH = Math.max(1e-5, maxLat - minLat);

  const pad = 34;
  const availW = width - pad * 2;
  const availH = height - pad * 2;
  const scale = Math.min(availW / rawW, availH / rawH);
  const drawnW = rawW * scale;
  const drawnH = rawH * scale;
  const offX = pad + (availW - drawnW) / 2;
  const offY = pad + (availH - drawnH) / 2;

  const projectX = (lon: number): number => offX + (lon - minLon) * cosLat * scale;
  const projectY = (lat: number): number => offY + (maxLat - lat) * scale;

  const vertices: TrackCourseVertex[] = baseVertices.map((v) => ({
    ...v,
    x: projectX(v.lon),
    y: projectY(v.lat),
  }));

  const profileTotalDistM =
    profile.length > 0 ? profile[profile.length - 1]!.dist_m : 0;
  const totalDistM = Math.max(
    1,
    profileTotalDistM,
    vertices[vertices.length - 1]!.distM
  );

  // Subtle topographic grid lines
  const gridLines: string[] = [];
  for (let i = 1; i <= 4; i += 1) {
    const gx = ((width * i) / 5).toFixed(1);
    const gy = ((height * i) / 5).toFixed(1);
    gridLines.push(
      `<line x1="${gx}" y1="0" x2="${gx}" y2="${height}" stroke="#16222D" stroke-width="1"/>`,
      `<line x1="0" y1="${gy}" x2="${width}" y2="${gy}" stroke="#16222D" stroke-width="1"/>`
    );
  }

  // Group vertices by polylineId & grade for clean SVG ribbons
  const polyGroups: TrackCourseVertex[][] = [];
  for (const v of vertices) {
    const last = polyGroups[polyGroups.length - 1];
    if (last && last[0]!.polylineId === v.polylineId && last[0]!.grade === v.grade) {
      last.push(v);
    } else if (last && last[0]!.polylineId === v.polylineId) {
      last.push(v);
      polyGroups.push([v]);
    } else {
      polyGroups.push([v]);
    }
  }

  const casingPolylinesHtml = polyGroups
    .filter((pts) => pts.length >= 2)
    .map((pts) => {
      const coords = pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
      return `<polyline points="${coords}" fill="none" stroke="#070B10" stroke-width="8.5" stroke-linecap="round" stroke-linejoin="round" opacity="0.92"/>`;
    })
    .join("");

  const gradePolylinesHtml = polyGroups
    .filter((pts) => pts.length >= 2)
    .map((pts) => {
      const coords = pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
      const color = getGradeColor(pts[0]!.grade);
      return `<polyline points="${coords}" fill="none" stroke="${color}" stroke-width="4.4" stroke-linecap="round" stroke-linejoin="round"/>`;
    })
    .join("");

  const crossingDotsHtml = crossings
    .filter((c) => typeof c.dist_m === "number" && Number.isFinite(c.dist_m))
    .map((c) => {
      const pt = sampleTrackCourseAtDistance(vertices, c.dist_m!);
      if (!pt) return "";
      const title = escapeHtml(`${c.name || "Waterway crossing"} (${(c.dist_m! / 1000).toFixed(2)} km)`);
      return `<circle cx="${pt.x.toFixed(1)}" cy="${pt.y.toFixed(1)}" r="4.2" fill="#38BDF8" stroke="#070B10" stroke-width="1.4"><title>${title}</title></circle>`;
    })
    .join("");

  const steepDotsHtml = steepZones
    .map((z) => {
      const pt = sampleTrackCourseAtDistance(vertices, z.peakDistM);
      if (!pt) return "";
      return `<circle cx="${pt.x.toFixed(1)}" cy="${pt.y.toFixed(1)}" r="3.1" fill="${getGradeColor(z.grade)}" stroke="#F8FAFC" stroke-width="1.1" opacity="0.88"><title>Steep zone #${z.index + 1}: ${z.peakSlopeDeg.toFixed(1)}°</title></circle>`;
    })
    .join("");

  const startPt = sampleTrackCourseAtDistance(vertices, 0) ?? vertices[0]!;
  const endPt = sampleTrackCourseAtDistance(vertices, totalDistM) ?? vertices[vertices.length - 1]!;
  const endpointsHtml = `
    <g pointer-events="none">
      <circle cx="${startPt.x.toFixed(1)}" cy="${startPt.y.toFixed(1)}" r="8" fill="#22C55E" stroke="#070B10" stroke-width="2"/>
      <text x="${startPt.x.toFixed(1)}" y="${(startPt.y + 3.5).toFixed(1)}" text-anchor="middle" fill="#07130C" font-size="9.5" font-weight="800" font-family="ui-monospace, monospace">S</text>
      <circle cx="${endPt.x.toFixed(1)}" cy="${endPt.y.toFixed(1)}" r="8" fill="#E2E8F0" stroke="#070B10" stroke-width="2"/>
      <text x="${endPt.x.toFixed(1)}" y="${(endPt.y + 3.5).toFixed(1)}" text-anchor="middle" fill="#0B1117" font-size="9.5" font-weight="800" font-family="ui-monospace, monospace">E</text>
    </g>
  `;

  const metersPerPx = 111132 / scale;
  const niceBar = chooseNiceScaleBarMeters(metersPerPx * 82);
  const barWidthPx = Math.max(28, Math.min(120, niceBar.meters / metersPerPx));
  const barX = 14;
  const barY = height - 14;

  const chromeHtml = `
    <g pointer-events="none">
      <g transform="translate(${width - 26}, 22)">
        <circle cx="0" cy="0" r="13" fill="rgba(11, 17, 23, 0.85)" stroke="#293B4D" stroke-width="1"/>
        <path d="M 0 -8 L 4 2 L 0 -1 L -4 2 Z" fill="#38BDF8"/>
        <text x="0" y="10" text-anchor="middle" fill="#94A3B8" font-size="7.5" font-weight="700" font-family="ui-monospace, monospace">N</text>
      </g>
      <line x1="${barX}" y1="${barY}" x2="${(barX + barWidthPx).toFixed(1)}" y2="${barY}" stroke="#94A3B8" stroke-width="2" stroke-linecap="square"/>
      <line x1="${barX}" y1="${barY - 4}" x2="${barX}" y2="${barY}" stroke="#94A3B8" stroke-width="1.5"/>
      <line x1="${(barX + barWidthPx).toFixed(1)}" y1="${barY - 4}" x2="${(barX + barWidthPx).toFixed(1)}" y2="${barY}" stroke="#94A3B8" stroke-width="1.5"/>
      <text x="${(barX + barWidthPx / 2).toFixed(1)}" y="${barY - 6}" text-anchor="middle" fill="#94A3B8" font-size="9.5" font-family="ui-monospace, monospace">${niceBar.label}</text>
    </g>
  `;

  const cursorOverlayHtml = `
    <g class="tf-course-cursor hidden" data-role="course-cursor" pointer-events="none">
      <circle data-role="course-cursor-glow" cx="${startPt.x.toFixed(1)}" cy="${startPt.y.toFixed(1)}" r="14" fill="rgba(253, 224, 71, 0.30)"/>
      <circle data-role="course-cursor-ring" cx="${startPt.x.toFixed(1)}" cy="${startPt.y.toFixed(1)}" r="7.5" fill="none" stroke="#FFFFFF" stroke-width="2.2"/>
      <circle data-role="course-cursor-dot" cx="${startPt.x.toFixed(1)}" cy="${startPt.y.toFixed(1)}" r="4.8" fill="#FDE047" stroke="#070B10" stroke-width="1.8"/>
    </g>
    <rect class="tf-course-scrub-hit" data-role="course-scrub-hit" x="0" y="0" width="${width}" height="${height}" fill="transparent"/>
  `;

  const svgHtml = `
    <svg class="tf-course-svg" viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-label="Track course plan view synchronized with terrain elevation profile">
      ${gridLines.join("")}
      ${casingPolylinesHtml}
      ${gradePolylinesHtml}
      ${crossingDotsHtml}
      ${steepDotsHtml}
      ${endpointsHtml}
      ${chromeHtml}
      ${cursorOverlayHtml}
    </svg>
  `;

  return {
    svgHtml,
    vertices,
    totalDistM,
    width,
    height,
    bbox: [minLon, minLat, maxLon, maxLat],
    scaleBarLabel: niceBar.label,
  };
}

/**
 * Sample the exact projected SVG point & geographic coordinate along the track course
 * corresponding to a cumulative distance `targetDistM` on the elevation profile.
 */
export function sampleTrackCourseAtDistance(
  vertices: readonly TrackCourseVertex[],
  targetDistM: number
): TrackCoursePoint | null {
  if (vertices.length === 0) return null;
  if (vertices.length === 1) {
    const v = vertices[0]!;
    return {
      distM: v.distM,
      lon: v.lon,
      lat: v.lat,
      x: v.x,
      y: v.y,
      grade: v.grade,
      segmentId: v.segmentId,
    };
  }

  // First check for an exact containing polyline segment [a.distM, b.distM]
  let bestNearest = vertices[0]!;
  let bestDelta = Math.abs(vertices[0]!.distM - targetDistM);

  for (let i = 1; i < vertices.length; i += 1) {
    const a = vertices[i - 1]!;
    const b = vertices[i]!;
    const dCurr = Math.abs(b.distM - targetDistM);
    if (dCurr < bestDelta) {
      bestDelta = dCurr;
      bestNearest = b;
    }

    if (a.polylineId !== b.polylineId) continue;
    const lo = Math.min(a.distM, b.distM);
    const hi = Math.max(a.distM, b.distM);
    if (targetDistM >= lo && targetDistM <= hi) {
      const span = b.distM - a.distM;
      const t = Math.abs(span) > 1e-6 ? (targetDistM - a.distM) / span : 0;
      const clampedT = Math.max(0, Math.min(1, t));
      return {
        distM: targetDistM,
        lon: a.lon + (b.lon - a.lon) * clampedT,
        lat: a.lat + (b.lat - a.lat) * clampedT,
        x: a.x + (b.x - a.x) * clampedT,
        y: a.y + (b.y - a.y) * clampedT,
        grade: clampedT < 0.5 ? a.grade : b.grade,
        segmentId: clampedT < 0.5 ? a.segmentId : b.segmentId,
      };
    }
  }

  return {
    distM: bestNearest.distM,
    lon: bestNearest.lon,
    lat: bestNearest.lat,
    x: bestNearest.x,
    y: bestNearest.y,
    grade: bestNearest.grade,
    segmentId: bestNearest.segmentId,
  };
}

/**
 * Given a pointer coordinate `(svgX, svgY)` inside the Track Course SVG, project onto the
 * nearest point along the track course polyline and return its exact cumulative distance `distM`
 * so the Elevation Profile crosshair moves in lockstep.
 */
export function findNearestPointOnTrackCourse(
  vertices: readonly TrackCourseVertex[],
  svgX: number,
  svgY: number
): TrackCoursePoint | null {
  if (vertices.length === 0) return null;
  if (vertices.length === 1) {
    const v = vertices[0]!;
    return {
      distM: v.distM,
      lon: v.lon,
      lat: v.lat,
      x: v.x,
      y: v.y,
      grade: v.grade,
      segmentId: v.segmentId,
    };
  }

  let bestDistSq = Infinity;
  let bestPoint: TrackCoursePoint | null = null;

  for (let i = 1; i < vertices.length; i += 1) {
    const a = vertices[i - 1]!;
    const b = vertices[i]!;
    if (a.polylineId !== b.polylineId) continue;

    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const lenSq = dx * dx + dy * dy;
    const t =
      lenSq > 1e-6
        ? Math.max(0, Math.min(1, ((svgX - a.x) * dx + (svgY - a.y) * dy) / lenSq))
        : 0;
    const px = a.x + dx * t;
    const py = a.y + dy * t;
    const distSq = (svgX - px) ** 2 + (svgY - py) ** 2;

    if (distSq < bestDistSq) {
      bestDistSq = distSq;
      bestPoint = {
        distM: a.distM + (b.distM - a.distM) * t,
        lon: a.lon + (b.lon - a.lon) * t,
        lat: a.lat + (b.lat - a.lat) * t,
        x: px,
        y: py,
        grade: t < 0.5 ? a.grade : b.grade,
        segmentId: t < 0.5 ? a.segmentId : b.segmentId,
      };
    }
  }

  return bestPoint;
}

/**
 * Given a geographic pointer coordinate `(lon, lat)` from the live MapLibre mini-map,
 * project orthogonally onto the nearest segment of the deduplicated canonical track polyline
 * and return its exact cumulative distance `distM` for lockstep elevation crosshair sync.
 */
export function findNearestPointOnTrackCourseGeo(
  vertices: readonly TrackCourseVertex[],
  lon: number,
  lat: number
): TrackCoursePoint | null {
  if (vertices.length === 0) return null;
  if (vertices.length === 1) {
    const v = vertices[0]!;
    return {
      distM: v.distM,
      lon: v.lon,
      lat: v.lat,
      x: v.x,
      y: v.y,
      grade: v.grade,
      segmentId: v.segmentId,
    };
  }

  const cosLat = Math.max(0.2, Math.cos((lat * Math.PI) / 180));
  let bestDistSq = Infinity;
  let bestPoint: TrackCoursePoint | null = null;

  for (let i = 1; i < vertices.length; i += 1) {
    const a = vertices[i - 1]!;
    const b = vertices[i]!;
    if (a.polylineId !== b.polylineId) continue;

    const dx = (b.lon - a.lon) * cosLat;
    const dy = b.lat - a.lat;
    const lenSq = dx * dx + dy * dy;
    const pxRel = (lon - a.lon) * cosLat;
    const pyRel = lat - a.lat;
    const t =
      lenSq > 1e-12
        ? Math.max(0, Math.min(1, (pxRel * dx + pyRel * dy) / lenSq))
        : 0;
    const projLon = a.lon + (b.lon - a.lon) * t;
    const projLat = a.lat + (b.lat - a.lat) * t;
    const dLon = (lon - projLon) * cosLat;
    const dLat = lat - projLat;
    const distSq = dLon * dLon + dLat * dLat;

    if (distSq < bestDistSq) {
      bestDistSq = distSq;
      bestPoint = {
        distM: a.distM + (b.distM - a.distM) * t,
        lon: projLon,
        lat: projLat,
        x: a.x + (b.x - a.x) * t,
        y: a.y + (b.y - a.y) * t,
        grade: t < 0.5 ? a.grade : b.grade,
        segmentId: t < 0.5 ? a.segmentId : b.segmentId,
      };
    }
  }

  return bestPoint;
}
