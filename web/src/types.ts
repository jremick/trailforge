export type BBoxTuple = readonly [number, number, number, number];

export type BasemapId = "offline-topo" | "opentopo" | "vector" | "osm" | "satellite";

export type TrackDifficulty = "easy" | "medium" | "hard" | "extreme";

export type CanonicalGrade = "Easy" | "Medium" | "Hard" | "Extreme";

export type LayerKey =
  | "placesRegions"
  | "highways"
  | "contoursHillshade"
  | "parksWaterways"
  | "pois"
  | "tracks"
  | "steepHighlights"
  | "hotspots"
  | "closures"
  | "reports"
  | "unmapped"
  | "snowLine";

export type LayerVisibilityMap = Record<LayerKey, boolean>;

export interface LayerVisibilityState {
  placesRegions: boolean;
  highways: boolean;
  tracks: boolean;
  steepHighlights: boolean;
  hotspots: boolean;
  closures: boolean;
  reports: boolean;
  unmapped: boolean;
  contoursHillshade: boolean;
  snowLine: boolean;
  snowLineElevationM: number;
  parksWaterways: boolean;
  pois: boolean;
}

export interface PublicUiConfigResponse {
  default_layer_order: LayerKey[];
  default_layer_visibility: LayerVisibilityMap;
  show_stack_telemetry_public: boolean;
}

export interface AdminUiSettingsResponse extends PublicUiConfigResponse {
  show_stack_telemetry_public_override: boolean | null;
  env_show_stack_telemetry_public: boolean;
  has_custom_layer_order: boolean;
  has_custom_layer_visibility: boolean;
  updated_at?: string | null;
}

export interface AdminUiSettingsUpdateInput {
  default_layer_order?: LayerKey[] | null;
  default_layer_visibility?: Partial<LayerVisibilityMap> | null;
  show_stack_telemetry_public?: boolean | null;
}

export interface UserUiPreferencesResponse {
  layer_order: LayerKey[] | null;
  layer_visibility: Partial<LayerVisibilityMap> | null;
  has_custom_preferences: boolean;
}

export interface UserUiPreferencesUpdateInput {
  layer_order?: LayerKey[] | null;
  layer_visibility?: Partial<LayerVisibilityMap> | null;
}

export interface RegionPreset {
  id: string;
  label: string;
  shortLabel: string;
  group: "region" | "landmark";
  packCategory?: "region" | "state";
  states?: readonly string[];
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
  vector_basemap_url?: string | null;
  vector_available: boolean;
  terrain_available: boolean;
  cdn_offload?: boolean;
  layers?: string[];
  snowline_bands_m?: number[];
  terrain_encoding?: string;
  attributions?: string[];
  layer_counts?: Record<string, number>;
  counts?: Record<string, number>;
}

export type TileMode = "loading" | "pmtiles" | "geojson-fallback" | "offline";

/* ---------- Slice 04 Closures & Agency Feed Types ---------- */

export type ClosureType =
  | "closed"
  | "seasonal"
  | "incident"
  | "park_alert"
  | "locked_gate"
  | "private_access"
  | string;

export type ClosureSeverity = "critical" | "warning" | "info" | string;

export type TrackClosureStatus =
  | "open"
  | "info"
  | "warning"
  | "seasonal"
  | "gated"
  | "closed"
  | string;

export interface ClosureItem {
  closure_id?: number | string;
  id?: number | string;
  feed_source?: string | null;
  agency?: string | null;
  external_id?: string | null;
  title: string;
  description?: string | null;
  closure_type: ClosureType;
  severity: ClosureSeverity;
  status?: string | null;
  state?: string | null;
  effective_from?: string | null;
  effective_to?: string | null;
  source_url?: string | null;
  last_updated_at?: string | null;
  is_stale?: boolean;
  affected_segment_ids?: string[];
  affected_track_ids?: string[];
  capad_park_name?: string | null;
  geometry?: unknown;
}

export interface FeedStatusItem {
  feed_source: string;
  display_name?: string | null;
  state?: string | null;
  status: string;
  last_attempt_at?: string | null;
  last_success_at?: string | null;
  consecutive_failures?: number;
  record_count?: number;
  is_stale: boolean;
  last_error?: string | null;
  source_url?: string | null;
}

export interface ClosuresStatusResponse {
  overall_status: "healthy" | "degraded" | "stale" | string;
  feeds: FeedStatusItem[];
}

export interface ClosuresListResponse {
  items: ClosureItem[];
  feeds?: FeedStatusItem[];
}

export interface ClosureAffectedTrack {
  track_id: string;
  name: string;
  state: string;
  grade?: string | null;
  total_length_m: number;
}

export interface ClosureDetailResponse {
  closure_id: number | string;
  feed_source?: string | null;
  feed_display_name?: string | null;
  agency?: string | null;
  external_id?: string | null;
  title: string;
  subtitle?: string | null;
  description?: string | null;
  closure_type: ClosureType;
  alert_type?: string | null;
  severity: ClosureSeverity;
  raw_severity?: string | null;
  status?: string | null;
  state?: string | null;
  effective_from?: string | null;
  effective_to?: string | null;
  source_url?: string | null;
  last_updated_at?: string | null;
  is_stale?: boolean;
  capad_park_name?: string | null;
  geometry_type?: string | null;
  bbox?: BBoxTuple | null;
  incident_details?: Record<string, string>;
  affected_segment_count?: number;
  affected_tracks?: ClosureAffectedTrack[];
  affected_track_ids?: string[];
  track_id?: string | null;
}

export interface MapClosureClickPayload {
  primary: ClosureDetailResponse;
  overlapping: ClosureDetailResponse[];
  clickLat: number;
  clickLon: number;
}

export type PlaceClass =
  | "state"
  | "region"
  | "capital"
  | "city"
  | "regional_hub"
  | "town"
  | "village"
  | "locality"
  | "park"
  | string;

export interface MapPlaceClickPayload {
  id: string;
  name: string;
  place_class: PlaceClass;
  state: string | null;
  lat: number;
  lon: number;
  bbox: BBoxTuple | null;
  zoom_target: number;
  region_hint: string | null;
}

/* ---------- Slice 05 Auth, Vehicle Profiles, Favorites & Pins ---------- */

export type VehicleCapabilityClass =
  | "AWD"
  | "Stock 4WD"
  | "Touring 4WD"
  | "Modified 4WD";

export type DrivetrainType = "awd" | "4wd_part_time" | "4wd_full_time";

export type TireClassType = "HT" | "AT" | "MT";

export type PinCategory =
  | "waypoint"
  | "camp"
  | "hazard"
  | "photo"
  | "water"
  | "recovery";

export interface TrackSuitability {
  vehicle_class: VehicleCapabilityClass | string;
  vehicle_name?: string | null;
  rating: string;
  reason?: string | null;
  warnings?: string[];
}

export type UserRole = "admin" | "member";

export type RegistrationMode = "open" | "invite_only" | "closed";

export interface AuthConfigResponse {
  registration_mode: RegistrationMode;
  demo_banner: string;
  allow_anonymous_browsing: boolean;
}

export interface AuthUser {
  id: string;
  email: string;
  display_name: string;
  role: UserRole;
  is_active: boolean;
  invited_by_code?: string | null;
  active_vehicle_profile_id?: string | null;
}

export interface AuthMeResponse {
  authenticated: boolean;
  user: AuthUser | null;
}

export interface AuthTokenResponse {
  user: AuthUser;
  access_token: string;
}

/* ---------- v1.1.0 Public Instance Hardening & Admin Console Types ---------- */

export interface AdminTrafficWindow {
  requests?: number;
  request_count?: number;
  errors?: number;
  error_count?: number;
  rate_limited?: number;
  rate_limited_count?: number;
  bytes_out?: number;
}

export interface AdminRouteBreakdownItem {
  route_group: string;
  requests?: number;
  request_count?: number;
  requests_24h?: number;
  errors?: number;
  error_count?: number;
  errors_24h?: number;
  rate_limited?: number;
  rate_limited_count?: number;
  rate_limited_24h?: number;
  bytes_out?: number;
}

export interface AdminStorageStats {
  tiles_bytes?: number;
  packs_bytes?: number;
  uploads_bytes?: number;
  uploads_quota_bytes?: number;
  uploads_usage_pct?: number;
  db_size_bytes?: number;
}

export interface AdminUsersSummary {
  total: number;
  active: number;
  admins: number;
  members: number;
  suspended: number;
}

export interface AdminInvitesSummary {
  active: number;
  total: number;
}

export interface AdminContentSummary {
  tracks?: number;
  segments?: number;
  trips?: number;
  telemetry_observations?: number;
  condition_reports?: number;
  planned_routes?: number;
}

export interface AdminStatsResponse {
  registration_mode?: RegistrationMode;
  generated_at?: string;
  traffic_24h?: AdminTrafficWindow;
  traffic_7d?: AdminTrafficWindow;
  route_breakdown?:
    | AdminRouteBreakdownItem[]
    | Record<string, Omit<AdminRouteBreakdownItem, "route_group">>;
  users?: AdminUsersSummary;
  invites?: AdminInvitesSummary;
  content?: AdminContentSummary;
  storage?: AdminStorageStats;
  feeds?: unknown;
}

export interface AdminUserItem {
  id: string;
  email: string;
  display_name: string;
  role: UserRole;
  is_active: boolean;
  disabled_at?: string | null;
  invited_by_code?: string | null;
  created_at?: string | null;
}

export interface AdminUserPatchInput {
  role?: UserRole;
  is_active?: boolean;
}

export interface AdminInviteItem {
  id: string;
  code: string;
  label?: string | null;
  role: UserRole;
  max_uses: number;
  use_count: number;
  expires_at?: string | null;
  revoked_at?: string | null;
  created_by_user_id?: string | null;
  created_at?: string | null;
  status?: string | null;
}

export interface AdminInviteCreateInput {
  label?: string | null;
  role?: UserRole;
  max_uses?: number;
  expires_in_days?: number | null;
}

export interface AdminComputeActionResponse {
  ok?: boolean;
  status?: string;
  synced_feeds?: number;
  records_upserted?: number;
  updated_segments?: number;
  updated_tracks?: number;
  message?: string;
  [key: string]: unknown;
}

export interface VehicleProfileInput {
  name: string;
  drivetrain: DrivetrainType;
  low_range: boolean;
  lift_mm: number;
  clearance_mm: number;
  tire_class: TireClassType;
  front_locker: boolean;
  rear_locker: boolean;
  has_winch: boolean;
  has_snorkel: boolean;
  is_active?: boolean;
}

export interface VehicleProfileItem {
  id: string;
  name: string;
  drivetrain: DrivetrainType;
  low_range?: boolean;
  has_low_range?: boolean;
  lift_mm: number;
  clearance_mm?: number;
  ground_clearance_mm?: number;
  tire_class: TireClassType;
  front_locker: boolean;
  rear_locker: boolean;
  has_winch: boolean;
  has_snorkel: boolean;
  capability_class: VehicleCapabilityClass;
  classification_reasons?: string[];
  is_active?: boolean;
  is_default?: boolean;
}

export interface UserFavoriteItem {
  id?: number;
  track_id: string;
  track_name?: string | null;
  name?: string | null;
  grade?: string | null;
  state?: string | null;
  total_length_m?: number | null;
  notes?: string | null;
  created_at?: string | null;
}

export interface UserPinInput {
  name: string;
  category: PinCategory;
  client_uuid?: string | null;
  notes?: string | null;
  lon: number;
  lat: number;
  track_id?: string | null;
}

export interface UserPinItem {
  id: string;
  name?: string;
  title?: string;
  category: PinCategory;
  notes?: string | null;
  lon: number;
  lat: number;
  elev_m?: number | null;
  track_id?: string | null;
  created_at?: string | null;
}

/* ---------- Slice 06 & Phase B Telemetry, IMU, Mobile Sync & Trips ---------- */

export type RuntimeMode =
  | "capacitor_ios"
  | "capacitor_android"
  | "pwa_browser";

export type MobileRecordingSource =
  | "pwa"
  | "capacitor_ios"
  | "capacitor_android";

export interface ImuSample {
  t_ms: number;
  ax: number;
  ay: number;
  az: number;
  pitch_deg?: number | null;
  roll_deg?: number | null;
}

export interface ImuMetrics {
  sample_count: number;
  roughness_rms: number | null;
  roughness_p95: number | null;
  shock_event_count: number;
  max_pitch_deg: number | null;
  max_roll_deg: number | null;
  latest_pitch_deg: number | null;
  latest_roll_deg: number | null;
}

export interface ImuSummary {
  sample_count: number;
  roughness_rms: number | null;
  roughness_p95: number | null;
  shock_event_count: number;
  max_pitch_deg: number | null;
  max_roll_deg: number | null;
}

export type TripVisibility = "private" | "community_anonymous";

export interface TelemetryPointInput {
  lon: number;
  lat: number;
  elev_m?: number | null;
  timestamp?: string | null;
  speed_mps?: number | null;
  roughness_rms?: number | null;
}

export interface TripUploadJsonPayload {
  name: string;
  points: TelemetryPointInput[];
  imu_samples?: ImuSample[];
  vehicle_profile_id?: string | null;
  visibility: TripVisibility;
  privacy_trim_m?: number;
  client_uuid?: string | null;
  client_session_id?: string | null;
  recording_source?: MobileRecordingSource;
}

export interface MobileTripSyncPayload extends TripUploadJsonPayload {
  imu_samples?: ImuSample[];
  client_session_id?: string | null;
  recording_source?: MobileRecordingSource;
  confirm_community?: boolean;
}

export interface TripMatchedSegment {
  segment_id: string;
  track_id?: string | null;
  confidence?: number | null;
  duration_s?: number | null;
  avg_speed_kmh?: number | null;
  roughness_rms?: number | null;
}

export interface TripItem {
  trip_id?: string;
  id?: string;
  name: string;
  visibility: TripVisibility;
  community_consent_pending?: boolean;
  vehicle_profile_id?: string | null;
  vehicle_class?: string | null;
  vehicle_capability_class?: string | null;
  privacy_trim_m?: number | null;
  raw_length_m?: number | null;
  trimmed_length_m?: number | null;
  matched_segment_count?: number;
  avg_match_confidence?: number | null;
  observations_published?: number;
  unmapped_candidate_count?: number;
  match_status?: string | null;
  matcher_name?: string | null;
  matcher_backend?: string | null;
  imu_summary?: ImuSummary | null;
  created_at?: string | null;
  matched_segments?: TripMatchedSegment[];
  unmapped_candidates?: unknown[];
}

/* ---------- Slice 08 Community Condition Reports & Gate Warnings ---------- */

export type ConditionReportType =
  | "fallen_tree"
  | "bog_hole"
  | "washout"
  | "deep_water"
  | "locked_gate"
  | "private_property"
  | "snow_ice"
  | "track_clear";

export type ReportVoteKind = "confirm" | "resolved";

export interface ConditionReportItem {
  id?: string | number;
  report_id?: string | number;
  report_type: ConditionReportType | string;
  title?: string | null;
  notes: string;
  severity?: string | null;
  lon: number;
  lat: number;
  track_id?: string | null;
  segment_id?: string | null;
  vehicle_class?: string | null;
  author_display_name?: string | null;
  photo_url?: string | null;
  confirm_count: number;
  resolved_count: number;
  freshness_score?: number | null;
  is_stale?: boolean;
  effective_status?: string | null;
  status?: string | null;
  is_gate_warning?: boolean;
  osm_note_url?: string | null;
  osm_edit_url?: string | null;
  created_at?: string | null;
  last_confirmed_at?: string | null;
}

export interface ConditionReportCreateInput {
  report_type: ConditionReportType;
  lon: number;
  lat: number;
  title?: string | null;
  notes: string;
  severity?: string;
  track_id?: string | null;
  segment_id?: string | null;
  vehicle_class?: string | null;
  client_uuid?: string | null;
  photo?: File | null;
}

/* ---------- Slice 07 Explainable Crowdsourced Grading & Rainfall ---------- */

export type ConfidenceLevel = "none" | "low" | "medium" | "high";

export type WetRiskLevel = "dry" | "damp" | "wet" | "heavy_rain";

export interface VehicleClassSplitEntry {
  count: number;
  pct: number;
}

export type VehicleClassSplit = Partial<
  Record<VehicleCapabilityClass | string, VehicleClassSplitEntry | number>
>;

export interface LiveGradeBreakdown {
  version?: string;
  sample_count?: number;
  effective_sample_weight?: number;
  confidence_level?: ConfidenceLevel | string;
  last_driven_at?: string | null;
  avg_speed_kmh?: number | null;
  median_speed_kmh?: number | null;
  avg_duration_s?: number | null;
  median_roughness_rms?: number | null;
  vehicle_class_split?: VehicleClassSplit | null;
  vehicle_normalized_delta?: number | null;
  outlier_clamped_count?: number;
  is_obstacle_hotspot?: boolean;
  obstacle_reasons?: string[];
  live_score?: number | null;
  live_grade?: string | null;
  reasons?: string[];
}

export interface RainfallContext {
  source?: string;
  attribution?: string;
  fetched_at?: string | null;
  rain_24h_mm?: number | null;
  rain_72h_mm?: number | null;
  wet_risk_level: WetRiskLevel | string;
  wet_score_adjustment?: number | null;
  warning?: string | null;
  is_stale?: boolean;
}

export interface ObstacleHotspotItem {
  segment_id: string;
  dist_m?: number | null;
  lon?: number | null;
  lat?: number | null;
  avg_speed_kmh?: number | null;
  roughness_rms?: number | null;
  median_roughness_rms?: number | null;
  reasons?: string[];
  obstacle_reasons?: string[];
}

export interface RainfallSummaryResponse {
  source?: string;
  attribution?: string;
  snapshots?: Array<{
    spatial_cell_key?: string;
    rain_24h_mm?: number;
    rain_72h_mm?: number;
    wet_risk_level?: string;
    fetched_at?: string;
    is_stale?: boolean;
  }>;
  latest?: RainfallContext | null;
}

/* ---------- Slice 09 Offline Regional Packs, Sync Queue & In-Cab Recorder ---------- */

export type PackBuildStatus = "ready" | "not_built" | "building" | "failed" | string;

export interface OfflinePackRegionItem {
  region_id: string;
  community_telemetry_policy?: string | null;
  community_telemetry_revision?: string | null;
  name: string;
  description: string;
  category?: "region" | "state" | string;
  region_type?: "tactical" | "state" | "custom" | string;
  states?: readonly string[];
  estimated_size_hint?: string;
  bbox: BBoxTuple;
  status: PackBuildStatus;
  version?: string | null;
  generated_at?: string | null;
  total_size_bytes?: number;
  track_count?: number;
  segment_count?: number;
  poi_count?: number;
  manifest_url?: string | null;
  snapshot_url?: string | null;
  bundle_url?: string | null;
  vector_pmtiles_url?: string | null;
  terrain_pmtiles_url?: string | null;
  attributions?: string[];
}

export interface OfflinePacksCatalogResponse {
  regions: OfflinePackRegionItem[];
}

export interface OfflinePackFileEntry {
  filename?: string;
  path?: string;
  url?: string;
  size_bytes?: number;
  sha256?: string;
}

export interface OfflinePackManifest {
  region_id: string;
  community_telemetry_policy?: string | null;
  community_telemetry_revision?: string | null;
  name?: string;
  version?: string;
  generated_at?: string;
  bbox?: BBoxTuple;
  bounds?: BBoxTuple;
  total_size_bytes?: number;
  counts?: Record<string, number>;
  attributions?: string[];
  files?: Record<string, OfflinePackFileEntry> | OfflinePackFileEntry[];
  vector_pmtiles_url?: string | null;
  terrain_pmtiles_url?: string | null;
  snapshot_url?: string | null;
  bundle_url?: string | null;
  snowline_bands_m?: number[];
}

export interface OfflinePackSnapshot {
  region_id: string;
  version?: string;
  generated_at?: string;
  bounds?: BBoxTuple;
  attributions?: string[];
  tracks?: TrackInspectorDetail[];
  pois?: PoiItem[];
  capad_parks?: ParkTenureItem[];
  closures?: unknown;
  reports?: unknown;
  hotspots?: unknown;
}

export interface DownloadedPackRecord {
  region_id: string;
  name: string;
  version: string;
  downloaded_at: string;
  total_size_bytes: number;
  track_count: number;
  segment_count: number;
  poi_count: number;
  bbox: BBoxTuple;
  vector_pmtiles_url: string | null;
  terrain_pmtiles_url: string | null;
  has_vector_pmtiles: boolean;
  has_terrain_pmtiles: boolean;
  snowline_bands_m: number[];
  attributions: string[];
  sha256_verified: boolean;
}

export interface RecordedBreadcrumbPoint {
  lon: number;
  lat: number;
  elev_m?: number | null;
  timestamp: string;
  accuracy_m?: number | null;
  speed_mps?: number | null;
  roughness_rms?: number | null;
  pitch_deg?: number | null;
  roll_deg?: number | null;
}

export type SyncQueueItem =
  | {
      id: string;
      kind: "trip";
      created_at: string;
      label: string;
      payload: MobileTripSyncPayload;
      last_error?: string | null;
    }
  | {
      id: string;
      kind: "report";
      created_at: string;
      label: string;
      payload: Omit<ConditionReportCreateInput, "photo">;
      last_error?: string | null;
    }
  | {
      id: string;
      kind: "pin";
      created_at: string;
      label: string;
      payload: UserPinInput;
      last_error?: string | null;
    };

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
  hardest_segment_id?: string | null;
  segment_grade_counts?: Record<string, number>;
}

export interface ElevationProfilePoint {
  dist_m: number;
  elev_m: number;
  section_id?: number;
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
  primary_source?: string;
  elevation_profile_m?: unknown[];
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
  geometry?: {
    type: string;
    coordinates?: unknown;
  } | null;
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
  closure_status?: TrackClosureStatus | null;
  active_closures?: ClosureItem[];
  condition_reports?: ConditionReportItem[];
  community_gate_warnings?: ConditionReportItem[];
  suitability?: TrackSuitability | null;
  bbox?: BBoxTuple | null;
  baseline_breakdown?: BaselineBreakdown | null;
  live_grade?: string | null;
  live_score?: number | null;
  confidence_level?: ConfidenceLevel | string | null;
  sample_count?: number;
  last_driven_at?: string | null;
  avg_speed_kmh?: number | null;
  est_duration_min?: number | null;
  vehicle_class_split?: VehicleClassSplit | null;
  live_breakdown?: LiveGradeBreakdown | null;
  rainfall_context?: RainfallContext | null;
  obstacle_hotspots?: ObstacleHotspotItem[];
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
  kind: "track" | "poi" | "park" | "place" | string;
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
  | "place"
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

/* ---------- Phase A Interactive Multi-Track Route Planner & GridForge Handoff ---------- */

export interface RouteWaypointInput {
  name: string;
  category?: PinCategory | string;
  lon: number;
  lat: number;
}

export interface RoutePlanRequest {
  name?: string | null;
  track_ids: string[];
  vehicle_class?: VehicleCapabilityClass | string | null;
  waypoints?: RouteWaypointInput[];
}

export interface RouteLegGap {
  from_track_id: string;
  to_track_id: string;
  gap_m: number;
}

export interface PlannedRouteLeg {
  index: number;
  track_id: string;
  name: string;
  length_m: number;
  grade: string;
  score?: number | null;
  closure_status?: TrackClosureStatus | null;
  gate_warning_count: number;
  reversed?: boolean;
  gap_from_prev_m?: number | null;
}

export interface RouteSafetySummary {
  has_blocking_closure: boolean;
  closure_count: number;
  gate_warning_count: number;
  active_report_count: number;
  unbridged_crossings: number;
  wet_risk_level: WetRiskLevel | string;
  warnings: string[];
}

export interface RouteElevationPoint extends ElevationProfilePoint {
  track_id?: string | null;
  track_name?: string | null;
}

export interface PlannedRouteAnalysis {
  name: string;
  track_ids: string[];
  waypoints?: RouteWaypointInput[];
  total_length_m: number;
  est_duration_min: number;
  min_elev_m?: number | null;
  max_elev_m?: number | null;
  elev_gain_m: number;
  elev_loss_m: number;
  max_slope_deg?: number | null;
  sustained_slope_deg?: number | null;
  worst_grade: string;
  worst_score?: number | null;
  effective_grade?: string | null;
  suitability?: TrackSuitability | null;
  safety_summary: RouteSafetySummary;
  closures?: ClosureItem[];
  community_gate_warnings?: ConditionReportItem[];
  condition_reports?: ConditionReportItem[];
  waterway_crossings?: WaterwayCrossingItem[];
  nearby_pois?: PoiItem[];
  capad_parks?: ParkTenureItem[];
  elevation_profile?: RouteElevationPoint[];
  legs: PlannedRouteLeg[];
  leg_gaps?: RouteLegGap[];
  bbox?: BBoxTuple | null;
  geometry?: unknown;
  gridforge_cli: string;
  offline_computed?: boolean;
}

export interface SavedRouteCreateInput {
  name: string;
  notes?: string | null;
  track_ids: string[];
  waypoints?: RouteWaypointInput[];
  vehicle_class?: VehicleCapabilityClass | string | null;
}

export interface SavedRouteUpdateInput {
  name?: string;
  notes?: string | null;
  track_ids?: string[];
  waypoints?: RouteWaypointInput[];
  vehicle_class?: VehicleCapabilityClass | string | null;
}

export interface SavedRouteItem {
  id: string;
  route_id?: string;
  user_id?: string;
  name: string;
  notes?: string | null;
  track_ids: string[];
  waypoints?: RouteWaypointInput[];
  waypoints_json?: RouteWaypointInput[];
  vehicle_class?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  total_length_m?: number | null;
  est_duration_min?: number | null;
  worst_grade?: string | null;
  leg_count?: number | null;
  has_blocking_closure?: boolean | null;
  analysis?: PlannedRouteAnalysis | null;
}
