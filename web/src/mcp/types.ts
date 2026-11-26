import { REQUIRED_ATTRIBUTIONS } from "../presets";
import type { VehicleCapabilityClass } from "../types";

export type TrailForgeViewMode = "explore" | "track" | "route" | "compare";

export interface RigContext {
  capability_class: VehicleCapabilityClass;
  vehicle_name?: string | null;
  has_snorkel?: boolean | null;
}

export interface TileConfig {
  vector_pmtiles_url?: string | null;
  terrain_pmtiles_url?: string | null;
  basemap_raster_url?: string | null;
}

export interface CompactSegmentGeom {
  segment_id: string;
  grade: "Easy" | "Medium" | "Hard" | "Extreme" | string;
  slope_deg: number | null;
  length_m: number;
  is_steep: boolean;
  coords: Array<[number, number]>; // [lon, lat]
}

export interface CompactElevationPoint {
  dist_m: number;
  elev_m: number;
  slope_deg: number;
  grade: string | null;
  segment_id?: string | null;
}

export interface CompactSuitability {
  rating: string;
  summary?: string | null;
  reason?: string | null;
  reasons?: string[];
  warnings?: string[];
}

export interface CompactTrackCard {
  track_id: string;
  name: string;
  state?: string | null;
  region?: string | null;
  grade: string | null;
  effective_grade?: string | null;
  baseline_grade?: string | null;
  score?: number | null;
  total_length_m: number;
  elev_gain_m?: number | null;
  elev_loss_m?: number | null;
  elev_min_m?: number | null;
  elev_max_m?: number | null;
  max_slope_deg?: number | null;
  sustained_slope_deg?: number | null;
  waterway_crossing_count?: number;
  is_4wd_only?: boolean;
  closure_status?: string | null;
  wet_risk_level?: string | null;
  rain_72h_mm?: number | null;
  suitability?: CompactSuitability | null;
  bbox?: [number, number, number, number] | null;
  center?: [number, number] | null; // [lon, lat]
  preview_coords?: Array<[number, number]>; // optional polyline preview in explore mode
  segments?: CompactSegmentGeom[];
  deep_link_url?: string | null;
}

export interface CompactClosureItem {
  closure_id?: string | number | null;
  title: string;
  agency?: string | null;
  closure_type?: string | null;
  severity?: string | null;
  status?: string | null;
  state?: string | null;
  effective_from?: string | null;
  effective_to?: string | null;
  source_url?: string | null;
  lon?: number | null;
  lat?: number | null;
  is_stale?: boolean;
}

export interface CompactReportItem {
  report_id?: string | number | null;
  report_type: string;
  title?: string | null;
  notes?: string | null;
  severity?: string | null;
  is_gate_warning?: boolean;
  confirm_count?: number;
  resolved_count?: number;
  created_at?: string | null;
  lon?: number | null;
  lat?: number | null;
}

export interface CompactPoiItem {
  poi_id?: string | number | null;
  name: string;
  category: string;
  state?: string | null;
  lon: number;
  lat: number;
  elev_m?: number | null;
  distance_m?: number | null;
}

export interface CompactCrossingItem {
  name: string;
  perennial: boolean;
  bridged: boolean;
  dist_m: number;
}

export interface CompactLiveSummary {
  live_grade?: string | null;
  confidence_level: string;
  sample_count: number;
  last_driven_at?: string | null;
  avg_speed_kmh?: number | null;
  est_duration_min?: number | null;
  vehicle_class_split?: Record<string, number>;
  wet_risk_level: string;
  rain_72h_mm?: number | null;
}

export interface ExploreViewData {
  tracks: CompactTrackCard[];
  closures: CompactClosureItem[];
  reports: CompactReportItem[];
  pois: CompactPoiItem[];
  rainfall_summary?: {
    rain_24h_mm?: number;
    rain_72h_mm?: number;
    wet_risk_level: string;
  } | null;
}

export interface TrackViewData {
  summary: CompactTrackCard;
  segments: CompactSegmentGeom[];
  elevation_profile: CompactElevationPoint[];
  crossings: CompactCrossingItem[];
  baseline_breakdown?: Record<string, unknown> | null;
  live_summary?: CompactLiveSummary | null;
  closures: CompactClosureItem[];
  gate_warnings: CompactReportItem[];
  condition_reports: CompactReportItem[];
  nearby_pois: CompactPoiItem[];
  parks: Array<{ name: string; park_type: string; state: string }>;
  export_gpx_url: string;
  export_geojson_url: string;
}

export interface RouteLegItem {
  leg_index: number;
  track_id: string;
  name: string;
  reversed: boolean;
  length_m: number;
  grade: string | null;
  gap_from_prev_m: number;
  segments: CompactSegmentGeom[];
}

export interface RouteViewData {
  name: string;
  saved_route_id?: string | null;
  total_length_m: number;
  est_duration_min: number;
  elev_gain_m: number;
  elev_loss_m: number;
  elev_min_m?: number | null;
  elev_max_m?: number | null;
  worst_grade: string | null;
  suitability?: CompactSuitability | null;
  safety_summary: {
    has_blocking_closure: boolean;
    closure_count: number;
    gate_warning_count: number;
    wet_risk_level: string;
    waterway_crossing_count: number;
    perennial_crossing_count: number;
    gap_count: number;
    max_gap_m: number;
  };
  legs: RouteLegItem[];
  elevation_profile: CompactElevationPoint[];
  closures: CompactClosureItem[];
  gate_warnings: CompactReportItem[];
  waypoints: Array<{ name: string; category: string; lon: number; lat: number }>;
  nearby_pois: CompactPoiItem[];
  gridforge_cli: string;
  export_gpx_url: string;
}

export interface CompareTrackEntry {
  summary: CompactTrackCard;
  segments: CompactSegmentGeom[];
  elevation_profile: CompactElevationPoint[];
  color_hex: string;
}

export interface CompareViewData {
  tracks: CompareTrackEntry[];
}

/**
 * Locked Section 7 Backend <-> MCP App wire contract (`schema_version: "1.0"`).
 */
export interface TrailForgeViewPayload {
  schema_version: "1.0";
  view_mode: TrailForgeViewMode;
  title: string;
  subtitle?: string | null;
  bbox: [number, number, number, number] | null;
  public_base_url: string;
  deep_link_url: string;
  snowline_m: number;
  attributions: string[];
  rig_context?: RigContext | null;
  tile_config?: TileConfig | null;
  explore?: ExploreViewData | null;
  track?: TrackViewData | null;
  route?: RouteViewData | null;
  compare?: CompareViewData | null;
}

export interface HostCapabilityFlags {
  openLinks: boolean;
  updateModelContext: boolean;
  serverTools: boolean;
}

export const DEFAULT_ATTRIBUTIONS: readonly string[] = REQUIRED_ATTRIBUTIONS.map(
  (item) => item.fullText
);

export const COMPARE_FALLBACK_COLORS: readonly string[] = [
  "#38BDF8",
  "#F59E0B",
  "#22C55E",
  "#A855F7",
];

/**
 * Validate an external URL for safe `openLink` / anchor usage (`http:` / `https:` only).
 */
export function sanitizeExternalHttpUrl(
  raw: string | null | undefined
): string | null {
  if (!raw) return null;
  const trimmed = raw.trim();
  if (!trimmed) return null;
  try {
    const parsed = new URL(trimmed);
    if (parsed.protocol === "https:" || parsed.protocol === "http:") {
      return parsed.toString();
    }
    return null;
  } catch (err) {
    console.debug("Rejected invalid external URL in MCP widget:", err);
    return null;
  }
}

function isValidViewMode(raw: unknown): raw is TrailForgeViewMode {
  return (
    raw === "explore" ||
    raw === "track" ||
    raw === "route" ||
    raw === "compare"
  );
}

function normalizeBbox(
  raw: unknown
): [number, number, number, number] | null {
  if (!Array.isArray(raw) || raw.length < 4) return null;
  const nums = raw.slice(0, 4).map((v) => Number(v));
  if (nums.some((n) => !Number.isFinite(n))) return null;
  return [nums[0]!, nums[1]!, nums[2]!, nums[3]!];
}

/**
 * Safely parse and normalize a `TrailForgeViewPayload` from `structuredContent`
 * (or fallback JSON text block), ensuring mandatory attributions and defaults
 * are always present.
 */
export function normalizeViewPayload(
  raw: unknown
): TrailForgeViewPayload | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;

  let mode: TrailForgeViewMode | null = isValidViewMode(obj.view_mode)
    ? obj.view_mode
    : null;
  if (!mode) {
    if (obj.route && typeof obj.route === "object") mode = "route";
    else if (obj.track && typeof obj.track === "object") mode = "track";
    else if (obj.compare && typeof obj.compare === "object") mode = "compare";
    else if (obj.explore && typeof obj.explore === "object") mode = "explore";
  }
  if (!mode) return null;

  const rawAttributions = Array.isArray(obj.attributions)
    ? obj.attributions
        .map((a) => String(a).trim())
        .filter((a) => a.length > 0)
    : [];
  const attributions =
    rawAttributions.length > 0 ? rawAttributions : [...DEFAULT_ATTRIBUTIONS];

  const publicBase =
    sanitizeExternalHttpUrl(
      typeof obj.public_base_url === "string" ? obj.public_base_url : ""
    ) ?? "https://trails.example.com";

  const deepLink =
    sanitizeExternalHttpUrl(
      typeof obj.deep_link_url === "string" ? obj.deep_link_url : ""
    ) ?? publicBase;

  const snowlineVal = Number(obj.snowline_m);
  const snowlineM =
    Number.isFinite(snowlineVal) && snowlineVal > 0 ? snowlineVal : 1000;

  return {
    schema_version: "1.0",
    view_mode: mode,
    title:
      typeof obj.title === "string" && obj.title.trim()
        ? obj.title.trim()
        : "TrailForge Explorer",
    subtitle: typeof obj.subtitle === "string" ? obj.subtitle : null,
    bbox: normalizeBbox(obj.bbox),
    public_base_url: publicBase.replace(/\/+$/, ""),
    deep_link_url: deepLink,
    snowline_m: snowlineM,
    attributions,
    rig_context:
      obj.rig_context && typeof obj.rig_context === "object"
        ? (obj.rig_context as RigContext)
        : null,
    tile_config:
      obj.tile_config && typeof obj.tile_config === "object"
        ? (obj.tile_config as TileConfig)
        : null,
    explore:
      obj.explore && typeof obj.explore === "object"
        ? (obj.explore as ExploreViewData)
        : null,
    track:
      obj.track && typeof obj.track === "object"
        ? (obj.track as TrackViewData)
        : null,
    route:
      obj.route && typeof obj.route === "object"
        ? (obj.route as RouteViewData)
        : null,
    compare:
      obj.compare && typeof obj.compare === "object"
        ? (obj.compare as CompareViewData)
        : null,
  };
}
