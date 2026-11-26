export type BBoxTuple = readonly [number, number, number, number];

export type BasemapId = "offline-topo" | "opentopo" | "osm" | "satellite";

export type TrackDifficulty = "easy" | "medium" | "hard" | "extreme";

export type LayerKey =
  | "tracks"
  | "steepHighlights"
  | "contoursHillshade"
  | "snowLine"
  | "pois";

export interface LayerVisibilityState {
  tracks: boolean;
  steepHighlights: boolean;
  contoursHillshade: boolean;
  snowLine: boolean;
  snowLineElevationM: number;
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
