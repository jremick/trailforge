export type BBoxTuple = readonly [number, number, number, number];

export type BasemapId = "offline-topo" | "opentopo" | "osm" | "satellite";

export type TrackDifficulty = "easy" | "medium" | "hard" | "extreme";

export type CanonicalGrade = "Easy" | "Medium" | "Hard" | "Extreme";

export type LayerKey =
  | "tracks"
  | "steepHighlights"
  | "contoursHillshade"
  | "snowLine"
  | "parksWaterways"
  | "pois";

export interface LayerVisibilityState {
  tracks: boolean;
  steepHighlights: boolean;
  contoursHillshade: boolean;
  snowLine: boolean;
  snowLineElevationM: number;
  parksWaterways: boolean;
  pois: boolean;
}

export interface RegionPreset {
  id: string;
  label: string;
  shortLabel: string;
  group: "region" | "landmark";
  stateTag: string;
  utmZones: readonly string[];
  center: readonly [number, number]; // [lon, lat]
  zoom: number;
  bbox: BBoxTuple; // [west, south, east, north]
  description: string;
  estimatedPackHint?: string;
}

export interface ApiHealthResponse {
  status: string;
  service?: string;
  version?: string;
  db_ok?: boolean;
  postgis_version?: string | null;
  schema_revision?: string | null;
}

export interface HealthState {
  kind: "connecting" | "online" | "degraded" | "offline";
  data: ApiHealthResponse | null;
  error: string | null;
  checkedAt: number | null;
}

export interface ParsedLatLon {
  kind: "latlon";
  lat: number;
  lon: number;
  canonical: string;
}

export interface ParsedMgrs {
  kind: "mgrs";
  lat: number;
  lon: number;
  mgrs: string;
  formatted: string;
  gzd: string;
  zone: number;
  band: string;
  squareId: string;
  precisionMeters: number;
}

export type ParsedCoordinate = ParsedLatLon | ParsedMgrs;

export interface UtmZoneInfo {
  zone: number;
  hemisphere: "N" | "S";
  epsg: number;
  gzd: string;
}

export interface AttributionItem {
  id: string;
  label: string;
  license: string;
  fullText: string;
}

export interface PoiIconMeta {
  id: string;
  label: string;
  iconPath: string;
}

/* ---------- Slice 03 Tile Manifest & Map Data Types ---------- */

export interface TileManifestResponse {
  generated_at?: string;
  bounds?: BBoxTuple;
  minzoom?: number;
  maxzoom?: number;
  vector_url: string;
  terrain_url: string;
  vector_available: boolean;
  terrain_available: boolean;
  layers?: string[];
  snowline_bands_m?: number[];
  terrain_encoding?: string;
  attributions?: string[];
  layer_counts?: Record<string, number>;
  counts?: Record<string, number>;
}

export type TileMode = "loading" | "pmtiles" | "geojson-fallback" | "offline";

/* ---------- Slice 03 Track Inspector & Search Types ---------- */

export interface ParkTenureItem {
  id?: number;
  capad_external_id?: string;
  name: string;
  park_type: string;
  type_abbr?: string | null;
  iucn?: string | null;
  state: string;
  area_ha?: number | null;
}

export interface BaselineFactorEntry {
  points: number;
  max_points: number;
  surface_class?: string;
  tracktype?: string | null;
  smoothness?: string | null;
  max_slope_deg?: number | null;
  sustained_slope_deg?: number | null;
  elev_gain_m?: number;
  elev_loss_m?: number;
  unbridged_count?: number;
  perennial?: boolean;
  is_bridged?: boolean;
  waterways?: string[];
  is_4wd_only?: boolean;
  is_4wd_recommended?: boolean;
  trail_class?: string;
  access_status?: string;
  has_gate_barrier?: boolean;
}

export interface BaselineBreakdown {
  formula_version?: string;
  raw_score?: number;
  final_score?: number;
  grade?: string;
  factors?: {
    surface_tracktype?: BaselineFactorEntry;
    max_slope?: BaselineFactorEntry;
    sustained_relief?: BaselineFactorEntry;
    waterway_crossings?: BaselineFactorEntry;
    four_wd_access?: BaselineFactorEntry;
  };
  overrides?: string[];
  reasons?: string[];
  dem_tier?: string | null;
  worst_segment_id?: string | null;
  segment_grade_counts?: Record<string, number>;
}

export interface ElevationProfilePoint {
  dist_m: number;
  elev_m: number;
  slope_deg?: number | null;
  segment_id?: string | null;
  grade?: string | null;
}

export interface WaterwayCrossingItem {
  name?: string | null;
  waterway_type?: string | null;
  perennial?: boolean;
  bridged?: boolean;
  dist_m?: number | null;
}

export interface PoiItem {
  id?: number;
  poi_id?: string | number;
  poi_key?: string;
  name: string;
  poi_type?: string;
  category?: string;
  icon?: string | null;
  state?: string;
  elev_m?: number | null;
  elevation_m?: number | null;
  distance_m?: number | null;
  access_note?: string | null;
  nearest_segment_id?: string | null;
  capad_park_name?: string | null;
  primary_source?: string;
  source_license?: string;
  lon: number;
  lat: number;
}

export interface SegmentSummaryItem {
  segment_id: string;
  segment_seq?: number | null;
  name?: string | null;
  length_m?: number;
  surface_class?: string;
  tracktype?: string | null;
  max_slope_deg?: number | null;
  sustained_slope_deg?: number | null;
  elev_min_m?: number | null;
  elev_max_m?: number | null;
  elev_gain_m?: number;
  elev_loss_m?: number;
  is_steep_highlight?: boolean;
  is_above_snowline?: boolean;
  waterway_crossing_count?: number;
  has_perennial_crossing?: boolean;
  is_4wd_only?: boolean;
  has_gate_barrier?: boolean;
  access_status?: string;
  grade?:
    | string
    | {
        baseline_grade: string;
        baseline_score: number;
        baseline_breakdown?: BaselineBreakdown;
        effective_grade: string;
        sample_count?: number;
        wet_risk_level?: string;
      }
    | null;
  score?: number | null;
  effective_grade?: string | null;
}

export interface TrackInspectorDetail {
  track_id: string;
  name: string;
  alt_names?: string[];
  state: string;
  primary_source?: string;
  source_refs?: unknown[];
  trail_class?: string;
  grade?: string | null;
  effective_grade?: string | null;
  score?: number | null;
  total_length_m: number;
  min_elev_m?: number | null;
  max_elev_m?: number | null;
  elev_min_m?: number | null;
  elev_max_m?: number | null;
  elev_gain_m: number;
  elev_loss_m?: number | null;
  max_slope_deg?: number | null;
  sustained_slope_deg?: number | null;
  waterway_crossing_count: number;
  is_4wd_only?: boolean;
  has_gate_barrier?: boolean;
  access_status?: string;
  bbox?: BBoxTuple | null;
  baseline_breakdown?: BaselineBreakdown | null;
  elevation_profile?: ElevationProfilePoint[];
  waterway_crossings?: WaterwayCrossingItem[];
  nearby_pois?: PoiItem[];
  capad_parks?: ParkTenureItem[];
  primary_park?: ParkTenureItem | null;
  primary_park_name?: string | null;
  segment_count?: number;
  segments?: SegmentSummaryItem[];
}

export interface ApiSearchResultItem {
  kind: "track" | "poi" | "park" | string;
  id: string;
  name: string;
  state?: string | null;
  subtitle?: string | null;
  lon?: number | null;
  lat?: number | null;
  bbox?: BBoxTuple | null;
  grade?: string | null;
  category?: string | null;
  elev_m?: number | null;
}

export interface ApiSearchResponse {
  query: string;
  total: number;
  results: ApiSearchResultItem[];
}

export type UnifiedSearchItemKind =
  | "coordinate"
  | "mgrs"
  | "track"
  | "poi"
  | "park"
  | "preset";

export interface UnifiedSearchItem {
  kind: UnifiedSearchItemKind;
  id: string;
  title: string;
  subtitle: string;
  lat: number | null;
  lon: number | null;
  bbox?: BBoxTuple | null;
  grade?: string | null;
  iconKey?: string | null;
  presetId?: string;
}
