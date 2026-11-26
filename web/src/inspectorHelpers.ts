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

  const crossings = detail.waterway_crossings ?? [];

  // Reconcile Track header stats with canonical segments & profile so multi-tile tracks
  // never show stale single-tile sub-ranges (e.g. flat end segment min/max/slope/length).
  const elevCandidatesMin: number[] = [];
  const elevCandidatesMax: number[] = [];
  if (typeof detail.min_elev_m === "number") elevCandidatesMin.push(detail.min_elev_m);
  else if (typeof detail.elev_min_m === "number") elevCandidatesMin.push(detail.elev_min_m);
  if (typeof detail.max_elev_m === "number") elevCandidatesMax.push(detail.max_elev_m);
  else if (typeof detail.elev_max_m === "number") elevCandidatesMax.push(detail.elev_max_m);

  let segLengthSumM = 0;
  let segGainSumM = 0;
  let segLossSumM = 0;
  let segMaxSlope: number | null = null;

  for (const seg of segments) {
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

  const elevGainM =
    segments.length > 0
      ? Math.max(detail.elev_gain_m ?? 0, segGainSumM)
      : detail.elev_gain_m;

  const elevLossM =
    typeof detail.elev_loss_m === "number"
      ? segments.length > 0
        ? Math.max(detail.elev_loss_m, segLossSumM)
        : detail.elev_loss_m
      : segments.length > 0
        ? segLossSumM
        : null;

  const sustainedSlopeDeg =
    detail.sustained_slope_deg ??
    segments.reduce<number | null>((best, s) => {
      if (typeof s.sustained_slope_deg !== "number") return best;
      return best === null ? s.sustained_slope_deg : Math.max(best, s.sustained_slope_deg);
    }, null);

  const slopeCandidates = [
    detail.max_slope_deg,
    segMaxSlope,
    sustainedSlopeDeg,
    breakdown?.factors?.max_slope?.max_slope_deg,
  ].filter((v): v is number => typeof v === "number" && Number.isFinite(v));
  const maxSlopeDeg =
    slopeCandidates.length > 0 ? Math.max(...slopeCandidates) : null;

  const reconciledLengthM = Math.max(
    detail.total_length_m || 0,
    segLengthSumM,
    profileMaxDistM
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
  const elevM = a.elev_m + (b.elev_m - a.elev_m) * t;
  const nearer = t < 0.5 ? a : b;
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

  const polylinePts = points
    .map((p) => `${toX(p.dist_m).toFixed(1)},${toY(p.elev_m).toFixed(1)}`)
    .join(" ");

  const firstX = toX(points[0]!.dist_m).toFixed(1);
  const lastX = toX(points[points.length - 1]!.dist_m).toFixed(1);
  const baseY = (padT + plotH).toFixed(1);
  const areaPath = `M ${firstX},${baseY} L ${polylinePts.replace(/ /g, " L ")} L ${lastX},${baseY} Z`;

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
