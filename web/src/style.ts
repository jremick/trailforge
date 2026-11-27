import type {
  Feature,
  FeatureCollection,
  LineString,
  Point,
  Polygon,
} from "geojson";
import type {
  ExpressionSpecification,
  FilterSpecification,
  LayerSpecification,
  SourceSpecification,
  StyleSpecification,
} from "maplibre-gl";
import {
  DEFAULT_SNOWLINE_BANDS_M,
  REGION_PRESETS,
  resolvePoiIconName,
  snapSnowlineBand,
} from "./presets";
import type {
  BasemapId,
  BBoxTuple,
  LayerVisibilityState,
  MapPlaceClickPayload,
  PoiItem,
  TileManifestResponse,
  UserPinItem,
} from "./types";
import { getPinCategoryColor } from "./vehicleHelpers";

export const EMPTY_FEATURE_COLLECTION: FeatureCollection = {
  type: "FeatureCollection",
  features: [],
};

/**
 * Convert a relative or absolute `.pmtiles` URL into a `pmtiles://` protocol URL.
 * Absolute `http://` / `https://` URLs (e.g. Cloudflare R2 CDN URLs) are wrapped
 * directly as `pmtiles://${url}` WITHOUT prepending `origin`. Relative paths resolve
 * against `origin` so MapLibre GL JS 5 blob Web Workers can fetch Range requests cleanly.
 */
export function toPmtilesUrl(pathOrUrl: string, origin?: string): string {
  const trimmed = pathOrUrl.trim();
  if (trimmed.startsWith("pmtiles://")) return trimmed;
  if (/^https?:\/\//i.test(trimmed)) return `pmtiles://${trimmed}`;
  const cleanOrigin = (origin ?? "").replace(/\/+$/, "");
  const cleanPath = trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
  return cleanOrigin
    ? `pmtiles://${cleanOrigin}${cleanPath}`
    : `pmtiles://${cleanPath}`;
}

/**
 * Resolve a static frontend asset URL (e.g. `/data/au_places.geojson`) against
 * an optional origin so MapLibre GL JS 5 Web Workers fetch it cleanly across
 * dev, Docker, and Cloudflare Pages deployments.
 */
export function resolveStaticAssetUrl(
  pathOrUrl: string,
  origin?: string
): string {
  const trimmed = pathOrUrl.trim();
  if (/^https?:\/\//i.test(trimmed)) return trimmed;
  const cleanOrigin = (origin ?? "").replace(/\/+$/, "");
  const cleanPath = trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
  return cleanOrigin ? `${cleanOrigin}${cleanPath}` : cleanPath;
}

/**
 * Build the self-hosted MapLibre SDF glyph template URL.
 */
export function resolveGlyphsUrl(origin?: string): string {
  const cleanOrigin = (origin ?? "").replace(/\/+$/, "");
  return cleanOrigin
    ? `${cleanOrigin}/fonts/{fontstack}/{range}.pbf`
    : "/fonts/{fontstack}/{range}.pbf";
}

export const HIGHWAY_LINE_LAYER_IDS = [
  "tf-highways-casing",
  "tf-highways-line",
] as const;

export const HIGHWAY_LABEL_LAYER_ID = "tf-highways-labels";

export const PLACE_INTERACTIVE_LAYER_IDS = [
  "tf-places-major",
  "tf-places-town",
  "tf-places-locality",
  "tf-places-region",
  "tf-places-state",
  "tf-park-labels",
] as const;

export const PLACES_REGIONS_LAYER_IDS = [
  "tf-places-cities-dots",
  "tf-places-state",
  "tf-places-region",
  "tf-park-labels",
  "tf-places-major",
  "tf-places-town",
  "tf-places-locality",
] as const;

/**
 * Safely parse a bounding box `[w, s, e, n]` from MapLibre feature properties
 * (MapLibre serializes nested array properties as JSON strings in `queryRenderedFeatures`).
 */
export function parseFeatureBBox(raw: unknown): BBoxTuple | null {
  let candidate: unknown = raw;
  if (typeof raw === "string") {
    const trimmed = raw.trim();
    if (!trimmed || trimmed === "null") return null;
    try {
      candidate = JSON.parse(trimmed) as unknown;
    } catch {
      return null;
    }
  }
  if (!Array.isArray(candidate) || candidate.length !== 4) return null;
  const nums = candidate.map((v) => Number(v));
  if (nums.some((n) => !Number.isFinite(n))) return null;
  const [w, s, e, n] = nums as [number, number, number, number];
  if (w >= e || s >= n) return null;
  return [w, s, e, n] as const;
}

export function formatPlaceClassLabel(
  placeClass: string,
  parkType?: string | null
): string {
  const norm = placeClass.trim().toLowerCase();
  if (norm === "state") return "State";
  if (norm === "region") return "4WD Region";
  if (norm === "capital") return "Capital City";
  if (norm === "city") return "City";
  if (norm === "regional_hub") return "Regional Hub";
  if (norm === "town") return "Town";
  if (norm === "village") return "Village";
  if (norm === "locality") return "Locality";
  if (norm === "park") {
    return parkType && parkType.trim() ? parkType.trim() : "National Park";
  }
  return placeClass.replace(/_/g, " ");
}

export function defaultZoomForPlaceClass(
  placeClass: string,
  areaHa?: number | null
): number {
  const norm = placeClass.trim().toLowerCase();
  if (norm === "state") return 6.2;
  if (norm === "region") return 9.2;
  if (norm === "capital") return 10.5;
  if (norm === "city") return 11.0;
  if (norm === "regional_hub") return 11.5;
  if (norm === "town") return 12.5;
  if (norm === "village") return 13.2;
  if (norm === "locality") return 13.5;
  if (norm === "park") {
    if (typeof areaHa === "number" && Number.isFinite(areaHa)) {
      if (areaHa >= 100000) return 10.2;
      if (areaHa >= 20000) return 11.2;
    }
    return 12.0;
  }
  return 12.5;
}

/**
 * Extract a normalized `MapPlaceClickPayload` from a rendered place, region,
 * state, or CAPAD park label feature.
 */
export function extractPlaceClickPayload(
  layerId: string,
  props: Record<string, unknown>,
  geometry: unknown,
  fallbackLat: number,
  fallbackLon: number
): MapPlaceClickPayload | null {
  const rawName = typeof props.name === "string" ? props.name.trim() : "";
  if (!rawName) return null;

  let lon = fallbackLon;
  let lat = fallbackLat;
  if (
    geometry &&
    typeof geometry === "object" &&
    (geometry as { type?: unknown }).type === "Point" &&
    Array.isArray((geometry as { coordinates?: unknown }).coordinates)
  ) {
    const coords = (geometry as { coordinates: unknown[] }).coordinates;
    const cLon = Number(coords[0]);
    const cLat = Number(coords[1]);
    if (Number.isFinite(cLon) && Number.isFinite(cLat)) {
      lon = cLon;
      lat = cLat;
    }
  }

  const state =
    typeof props.state === "string" && props.state.trim()
      ? props.state.trim()
      : null;

  if (layerId === "tf-park-labels") {
    const areaHa =
      typeof props.area_ha === "number"
        ? props.area_ha
        : typeof props.area_ha === "string" &&
            Number.isFinite(Number(props.area_ha))
          ? Number(props.area_ha)
          : null;
    const parkType =
      typeof props.park_type === "string" && props.park_type.trim()
        ? props.park_type.trim()
        : null;
    const id =
      typeof props.id === "string" && props.id.trim()
        ? props.id.trim()
        : typeof props.capad_external_id === "string" &&
            props.capad_external_id.trim()
          ? props.capad_external_id.trim()
          : `park-${rawName.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
    return {
      id,
      name: rawName,
      place_class: "park",
      state,
      lat,
      lon,
      bbox: parseFeatureBBox(props.bbox),
      zoom_target: defaultZoomForPlaceClass("park", areaHa),
      region_hint: parkType,
    };
  }

  const placeClass =
    typeof props.place_class === "string" && props.place_class.trim()
      ? props.place_class.trim()
      : "town";
  const id =
    typeof props.id === "string" && props.id.trim()
      ? props.id.trim()
      : `place-${rawName.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
  const rawZoom =
    typeof props.zoom_target === "number"
      ? props.zoom_target
      : typeof props.zoom_target === "string" && props.zoom_target.trim()
        ? Number(props.zoom_target)
        : Number.NaN;
  const zoomTarget = Number.isFinite(rawZoom)
    ? rawZoom
    : defaultZoomForPlaceClass(placeClass);
  const regionHint =
    typeof props.region_hint === "string" && props.region_hint.trim()
      ? props.region_hint.trim()
      : null;

  return {
    id,
    name: rawName,
    place_class: placeClass,
    state,
    lat,
    lon,
    bbox: parseFeatureBBox(props.bbox),
    zoom_target: zoomTarget,
    region_hint: regionHint,
  };
}

/**
 * Build the MapLibre filter expression for the `snowline_bands` vector layer.
 * Each feature in `snowline_bands` covers terrain `>= elev_min_m`.
 */
export function buildSnowlineFilter(
  thresholdM: number,
  availableBands: readonly number[] = DEFAULT_SNOWLINE_BANDS_M
): FilterSpecification {
  const snapped = snapSnowlineBand(thresholdM, availableBands);
  return ["==", ["get", "elev_min_m"], snapped];
}

export const SELECTED_TRACK_LAYER_IDS = [
  "tf-tracks-selected-glow",
  "tf-tracks-selected",
  "tf-tracks-selected-inner-casing",
  "tf-tracks-selected-core",
] as const;

export const SELECTED_TRACK_STEEP_LAYER_ID = "tf-tracks-selected-steep";

export const SELECTED_CLOSURE_OUTLINE_LAYER_ID = "tf-closures-selected-outline";

/**
 * Build the MapLibre filter expression for highlighting a selected closure/incident geometry.
 */
export function buildSelectedClosureFilter(
  closureId: string | number | null
): FilterSpecification {
  return [
    "all",
    ["in", ["geometry-type"], ["literal", ["Polygon", "LineString"]]],
    [
      "==",
      ["to-string", ["coalesce", ["get", "closure_id"], ""]],
      closureId !== null && closureId !== undefined
        ? String(closureId)
        : "__none__",
    ],
  ];
}

export interface TrackSelectionOpacities {
  casing: number;
  line: number;
  accessWarn: number;
  steep: number;
}

/**
 * Spotlight opacity curve: when a track is actively selected, surrounding
 * unselected tracks recede calmly into context so the selected track pops
 * immediately without losing grade readability.
 */
export function getTrackSelectionOpacities(
  hasSelection: boolean
): TrackSelectionOpacities {
  return hasSelection
    ? { casing: 0.36, line: 0.44, accessWarn: 0.32, steep: 0.28 }
    : { casing: 0.92, line: 0.98, accessWarn: 0.92, steep: 0.9 };
}

/**
 * Build the MapLibre filter expression for highlighting a selected canonical track.
 */
export function buildSelectedTrackFilter(
  trackId: string | null
): FilterSpecification {
  return ["==", ["coalesce", ["get", "track_id"], ""], trackId ?? "__none__"];
}

/**
 * Color-code graded 4WD segments (supports canonical TitleCase & lowercase grade properties).
 */
export function buildGradeColorExpression(): ExpressionSpecification {
  return [
    "match",
    [
      "downcase",
      [
        "coalesce",
        ["get", "grade"],
        ["get", "effective_grade"],
        ["get", "difficulty"],
        "",
      ],
    ],
    "easy",
    "#22C55E",
    "medium",
    "#38BDF8",
    "hard",
    "#F59E0B",
    "extreme",
    "#EF4444",
    "#94A3B8",
  ];
}

/**
 * Color expression for Slice 08 community condition reports & gate warnings.
 */
export function buildReportColorExpression(): ExpressionSpecification {
  return [
    "match",
    ["downcase", ["coalesce", ["get", "report_type"], ""]],
    "locked_gate",
    "#EF4444",
    "private_property",
    "#F97316",
    "washout",
    "#EF4444",
    "fallen_tree",
    "#F59E0B",
    "bog_hole",
    "#D97706",
    "deep_water",
    "#38BDF8",
    "snow_ice",
    "#BAE6FD",
    "track_clear",
    "#22C55E",
    "#F59E0B",
  ];
}

/**
 * Color expression for Slice 04 closures, incidents, seasonal restrictions, and locked gates.
 */
export function buildClosureColorExpression(): ExpressionSpecification {
  return [
    "case",
    [
      "in",
      ["downcase", ["coalesce", ["get", "closure_type"], ""]],
      ["literal", ["closed", "road_closure", "park_closure", "incident", "bushfire", "flood"]],
    ],
    "#EF4444",
    [
      "in",
      ["downcase", ["coalesce", ["get", "severity"], ""]],
      ["literal", ["critical", "emergency", "closed"]],
    ],
    "#EF4444",
    [
      "in",
      ["downcase", ["coalesce", ["get", "closure_type"], ""]],
      ["literal", ["seasonal", "seasonal_closure", "locked_gate", "private_gate", "private_access"]],
    ],
    "#F59E0B",
    [
      "in",
      ["downcase", ["coalesce", ["get", "severity"], ""]],
      ["literal", ["warning", "watch_and_act"]],
    ],
    "#F59E0B",
    "#38BDF8",
  ];
}

/**
 * Convert signed-in user personal pins into a GeoJSON FeatureCollection for MapLibre rendering.
 */
export function userPinsToGeoJson(
  pins: readonly UserPinItem[]
): FeatureCollection<Point> {
  return {
    type: "FeatureCollection",
    features: pins
      .filter((p) => Number.isFinite(p.lon) && Number.isFinite(p.lat))
      .map((p) => ({
        type: "Feature",
        properties: {
          id: p.id,
          name: p.name ?? p.title ?? "Pin",
          category: p.category,
          notes: p.notes ?? null,
          color: getPinCategoryColor(p.category),
        },
        geometry: {
          type: "Point",
          coordinates: [p.lon, p.lat],
        },
      })),
  };
}

/**
 * Convert `/api/pois` list items into a GeoJSON FeatureCollection for GeoJSON fallback mode.
 */
export function poisToGeoJson(pois: readonly PoiItem[]): FeatureCollection<Point> {
  return {
    type: "FeatureCollection",
    features: pois
      .filter((p) => Number.isFinite(p.lon) && Number.isFinite(p.lat))
      .map((p) => ({
        type: "Feature",
        properties: {
          poi_id: String(p.poi_id ?? p.id ?? p.poi_key ?? p.name),
          name: p.name,
          category: p.category ?? p.poi_type ?? "poi",
          elevation_m: p.elevation_m ?? p.elev_m ?? null,
          access_note: p.access_note ?? null,
          state: p.state ?? null,
          icon: resolvePoiIconName(p.icon ?? p.category ?? p.poi_type),
        },
        geometry: {
          type: "Point",
          coordinates: [p.lon, p.lat],
        },
      })),
  };
}

/**
 * Generate a lightweight offline 1° topographic graticule covering VIC & NSW/ACT
 * plus a finer 0.25° grid over the Australian Alps corridor so the map remains
 * visually legible and spatially oriented when external tiles are unreachable.
 */
export function buildOfflineGraticuleGeoJson(): FeatureCollection<LineString> {
  const features: Feature<LineString>[] = [];

  for (let lon = 140; lon <= 154; lon += 1) {
    features.push({
      type: "Feature",
      properties: { kind: "major", axis: "lon", value: lon },
      geometry: {
        type: "LineString",
        coordinates: [
          [lon, -40],
          [lon, -28],
        ],
      },
    });
  }

  for (let lat = -40; lat <= -28; lat += 1) {
    features.push({
      type: "Feature",
      properties: { kind: "major", axis: "lat", value: lat },
      geometry: {
        type: "LineString",
        coordinates: [
          [140, lat],
          [154, lat],
        ],
      },
    });
  }

  for (let lon = 147.75; lon <= 149.25; lon += 0.25) {
    if (Number.isInteger(lon)) continue;
    features.push({
      type: "Feature",
      properties: { kind: "minor", axis: "lon", value: lon },
      geometry: {
        type: "LineString",
        coordinates: [
          [lon, -37.25],
          [lon, -35.75],
        ],
      },
    });
  }

  for (let lat = -37.25; lat <= -35.75; lat += 0.25) {
    if (Number.isInteger(lat)) continue;
    features.push({
      type: "Feature",
      properties: { kind: "minor", axis: "lat", value: lat },
      geometry: {
        type: "LineString",
        coordinates: [
          [147.75, lat],
          [149.25, lat],
        ],
      },
    });
  }

  return { type: "FeatureCollection", features };
}

/**
 * Lightweight regional pack & corridor reference outlines for offline fallback orientation.
 */
export function buildRegionalBoundsGeoJson(): FeatureCollection<Polygon> {
  const targetIds = new Set([
    "australian-alps",
    "blue-mountains",
    "davies-plain-corridor",
  ]);
  const features: Feature<Polygon>[] = REGION_PRESETS.filter((p) =>
    targetIds.has(p.id)
  ).map((preset) => {
    const [w, s, e, n] = preset.bbox;
    return {
      type: "Feature",
      properties: {
        id: preset.id,
        label: preset.shortLabel,
        is_corridor: preset.id === "davies-plain-corridor",
      },
      geometry: {
        type: "Polygon",
        coordinates: [
          [
            [w, s],
            [e, s],
            [e, n],
            [w, n],
            [w, s],
          ],
        ],
      },
    };
  });

  return { type: "FeatureCollection", features };
}

export function buildSearchPinGeoJson(
  lat: number,
  lon: number,
  label: string
): FeatureCollection<Point> {
  return {
    type: "FeatureCollection",
    features: [
      {
        type: "Feature",
        properties: { label },
        geometry: {
          type: "Point",
          coordinates: [lon, lat],
        },
      },
    ],
  };
}

export interface BuildStyleOptions {
  initialBasemap: BasemapId;
  layers: LayerVisibilityState;
  tileManifest?: TileManifestResponse | null;
  origin?: string;
  assetsOrigin?: string;
  selectedTrackId?: string | null;
  selectedClosureId?: string | number | null;
}

function buildHighwayLineLayers(
  visibility: "visible" | "none"
): LayerSpecification[] {
  const highwayClassExpr: ExpressionSpecification = [
    "downcase",
    ["coalesce", ["get", "highway_class"], "primary"],
  ];

  return [
    {
      id: "tf-highways-casing",
      type: "line",
      source: "tf-highways",
      minzoom: 4.2,
      layout: {
        visibility,
        "line-cap": "round",
        "line-join": "round",
      },
      paint: {
        "line-color": "#0B1117",
        "line-width": [
          "interpolate",
          ["linear"],
          ["zoom"],
          4.5,
          ["match", highwayClassExpr, "motorway", 2.6, "trunk", 2.0, 1.4],
          8,
          ["match", highwayClassExpr, "motorway", 4.2, "trunk", 3.4, 2.4],
          12,
          ["match", highwayClassExpr, "motorway", 5.8, "trunk", 4.6, 3.4],
          15,
          ["match", highwayClassExpr, "motorway", 6.8, "trunk", 5.4, 4.0],
        ],
        "line-opacity": [
          "step",
          ["zoom"],
          0,
          4.5,
          ["case", ["<=", ["coalesce", ["get", "minzoom"], 6], 4.5], 0.52, 0],
          5.5,
          ["case", ["<=", ["coalesce", ["get", "minzoom"], 6], 5.5], 0.58, 0],
          7.5,
          ["case", ["<=", ["coalesce", ["get", "minzoom"], 6], 7.5], 0.65, 0],
          9.0,
          0.65,
        ],
      },
    },
    {
      id: "tf-highways-line",
      type: "line",
      source: "tf-highways",
      minzoom: 4.2,
      layout: {
        visibility,
        "line-cap": "round",
        "line-join": "round",
      },
      paint: {
        "line-color": [
          "match",
          highwayClassExpr,
          "motorway",
          "#F59E0B",
          "trunk",
          "#EAB308",
          "#CBD5E1",
        ],
        "line-width": [
          "interpolate",
          ["linear"],
          ["zoom"],
          4.5,
          ["match", highwayClassExpr, "motorway", 1.5, "trunk", 1.1, 0.7],
          8,
          ["match", highwayClassExpr, "motorway", 2.4, "trunk", 1.9, 1.3],
          12,
          ["match", highwayClassExpr, "motorway", 3.4, "trunk", 2.6, 1.9],
          15,
          ["match", highwayClassExpr, "motorway", 4.0, "trunk", 3.1, 2.3],
        ],
        "line-opacity": [
          "step",
          ["zoom"],
          0,
          4.5,
          ["case", ["<=", ["coalesce", ["get", "minzoom"], 6], 4.5], 0.76, 0],
          5.5,
          ["case", ["<=", ["coalesce", ["get", "minzoom"], 6], 5.5], 0.8, 0],
          7.5,
          ["case", ["<=", ["coalesce", ["get", "minzoom"], 6], 7.5], 0.82, 0],
          9.0,
          0.78,
        ],
      },
    },
  ];
}

/**
 * Build the MapLibre GL JS 5 StyleSpecification for TrailForge (Slice 03 + Phase 1 Option A).
 * - Mounts `trailforge_vector.pmtiles` & `trailforge_terrain.pmtiles` when available.
 * - Mounts self-hosted SDF glyphs, Pan-Australia places/regions, and major highways cleanly offline.
 * - Automatically falls back to live GeoJSON sources when PMTiles are unavailable.
 */
export function buildTrailForgeStyle(
  options: BuildStyleOptions
): StyleSpecification {
  const {
    initialBasemap,
    layers,
    tileManifest,
    origin,
    assetsOrigin,
    selectedTrackId = null,
    selectedClosureId = null,
  } = options;

  const effectiveAssetsOrigin = assetsOrigin ?? origin;

  const showOsm = initialBasemap === "osm";
  const showOpenTopo = initialBasemap === "opentopo";
  const showSat = initialBasemap === "satellite";

  const useVectorPmtiles = Boolean(
    tileManifest?.vector_available && tileManifest.vector_url
  );
  const useTerrainPmtiles = Boolean(
    tileManifest?.terrain_available && tileManifest.terrain_url
  );
  const snowBands = tileManifest?.snowline_bands_m ?? DEFAULT_SNOWLINE_BANDS_M;

  const vis = (enabled: boolean): "visible" | "none" =>
    enabled ? "visible" : "none";

  const sources: Record<string, SourceSpecification> = {
    "osm-standard": {
      type: "raster",
      tiles: [
        "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "https://b.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "https://c.tile.openstreetmap.org/{z}/{x}/{y}.png",
      ],
      tileSize: 256,
      maxzoom: 19,
      attribution: "© OpenStreetMap contributors (ODbL)",
    },
    opentopomap: {
      type: "raster",
      tiles: ["https://tile.opentopomap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      maxzoom: 17,
      attribution:
        "Map data: © OpenStreetMap contributors, SRTM | Style: © OpenTopoMap (CC-BY-SA)",
    },
    "esri-imagery": {
      type: "raster",
      tiles: [
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      ],
      tileSize: 256,
      maxzoom: 19,
      attribution: "Tiles © Esri — World Imagery",
    },
    "offline-graticule": {
      type: "geojson",
      data: buildOfflineGraticuleGeoJson(),
    },
    "offline-regions": {
      type: "geojson",
      data: buildRegionalBoundsGeoJson(),
    },
    // Pan-Australia places, regions & major highways (self-hosted offline-capable GeoJSON)
    "tf-places": {
      type: "geojson",
      data: resolveStaticAssetUrl(
        "/data/au_places.geojson",
        effectiveAssetsOrigin
      ),
    },
    "tf-highways": {
      type: "geojson",
      data: resolveStaticAssetUrl(
        "/data/au_highways.geojson",
        effectiveAssetsOrigin
      ),
    },
    // GeoJSON fallback & interactive overlay sources
    "tf-contours": {
      type: "geojson",
      data: EMPTY_FEATURE_COLLECTION,
    },
    "tf-snow-line": {
      type: "geojson",
      data: EMPTY_FEATURE_COLLECTION,
    },
    "tf-tracks": {
      type: "geojson",
      data: EMPTY_FEATURE_COLLECTION,
    },
    "tf-steep": {
      type: "geojson",
      data: EMPTY_FEATURE_COLLECTION,
    },
    "tf-pois": {
      type: "geojson",
      data: EMPTY_FEATURE_COLLECTION,
    },
    "tf-closures": {
      type: "geojson",
      data: EMPTY_FEATURE_COLLECTION,
    },
    "tf-hotspots": {
      type: "geojson",
      data: EMPTY_FEATURE_COLLECTION,
    },
    "tf-unmapped": {
      type: "geojson",
      data: EMPTY_FEATURE_COLLECTION,
    },
    "tf-reports": {
      type: "geojson",
      data: EMPTY_FEATURE_COLLECTION,
    },
    "tf-recorded-track": {
      type: "geojson",
      data: EMPTY_FEATURE_COLLECTION,
    },
    "tf-backtrack-route": {
      type: "geojson",
      data: EMPTY_FEATURE_COLLECTION,
    },
    "tf-recorder-head": {
      type: "geojson",
      data: EMPTY_FEATURE_COLLECTION,
    },
    "tf-planned-route": {
      type: "geojson",
      data: EMPTY_FEATURE_COLLECTION,
    },
    "tf-planned-route-pins": {
      type: "geojson",
      data: EMPTY_FEATURE_COLLECTION,
    },
    "tf-user-pins": {
      type: "geojson",
      data: EMPTY_FEATURE_COLLECTION,
    },
    "tf-search-pin": {
      type: "geojson",
      data: EMPTY_FEATURE_COLLECTION,
    },
  };

  if (useVectorPmtiles && tileManifest) {
    sources["tf-vector"] = {
      type: "vector",
      url: toPmtilesUrl(tileManifest.vector_url, origin),
    };
  }

  if (useTerrainPmtiles && tileManifest) {
    sources["tf-terrain-dem"] = {
      type: "raster-dem",
      url: toPmtilesUrl(tileManifest.terrain_url, origin),
      encoding: "mapbox",
      tileSize: 256,
    };
  }

  const gradeColor = buildGradeColorExpression();
  const selectedFilter = buildSelectedTrackFilter(selectedTrackId);
  const snowFilter = buildSnowlineFilter(layers.snowLineElevationM, snowBands);

  const styleLayers: LayerSpecification[] = [
    {
      id: "bg-dark-outdoors",
      type: "background",
      paint: {
        "background-color": "#0E151B",
      },
    },
    {
      id: "offline-graticule-lines",
      type: "line",
      source: "offline-graticule",
      paint: {
        "line-color": [
          "case",
          ["==", ["get", "kind"], "major"],
          "#223342",
          "#182530",
        ],
        "line-width": ["case", ["==", ["get", "kind"], "major"], 1.2, 0.7],
        "line-opacity": 0.9,
      },
    },
    {
      id: "basemap-osm-underlay",
      type: "raster",
      source: "osm-standard",
      layout: { visibility: vis(showOsm) },
      paint: {
        "raster-opacity": 0.85,
        "raster-brightness-max": 0.82,
        "raster-saturation": -0.15,
      },
    },
    {
      id: "basemap-opentopo",
      type: "raster",
      source: "opentopomap",
      layout: { visibility: vis(showOpenTopo) },
      paint: {
        "raster-opacity": 0.86,
        "raster-brightness-max": 0.84,
        "raster-contrast": 0.08,
      },
    },
    {
      id: "basemap-satellite",
      type: "raster",
      source: "esri-imagery",
      layout: { visibility: vis(showSat) },
      paint: {
        "raster-opacity": 0.92,
      },
    },
    {
      id: "offline-regions-fill",
      type: "fill",
      source: "offline-regions",
      paint: {
        "fill-color": [
          "case",
          ["==", ["get", "is_corridor"], true],
          "#F59E0B",
          "#38BDF8",
        ],
        "fill-opacity": [
          "case",
          ["==", ["get", "is_corridor"], true],
          0.05,
          0.02,
        ],
      },
    },
    {
      id: "offline-regions-outline",
      type: "line",
      source: "offline-regions",
      paint: {
        "line-color": [
          "case",
          ["==", ["get", "is_corridor"], true],
          "#F59E0B",
          "#38BDF8",
        ],
        "line-width": ["case", ["==", ["get", "is_corridor"], true], 1.5, 1.1],
        "line-dasharray": [5, 3],
        "line-opacity": 0.52,
      },
    },
  ];

  // Terrain-RGB hillshade layer when terrain PMTiles are mounted
  if (useTerrainPmtiles) {
    styleLayers.push({
      id: "tf-hillshade",
      type: "hillshade",
      source: "tf-terrain-dem",
      layout: { visibility: vis(layers.contoursHillshade) },
      paint: {
        "hillshade-exaggeration": 0.48,
        "hillshade-shadow-color": "#050A0E",
        "hillshade-highlight-color": "#B8CCE0",
        "hillshade-accent-color": "#2C4052",
        "hillshade-illumination-direction": 315,
      },
    });
  }

  const trackOpacities = getTrackSelectionOpacities(Boolean(selectedTrackId));

  if (useVectorPmtiles) {
    // 1. CAPAD 2025 Protected Areas
    styleLayers.push(
      {
        id: "tf-parks-fill",
        type: "fill",
        source: "tf-vector",
        "source-layer": "capad_parks",
        layout: { visibility: vis(layers.parksWaterways) },
        paint: {
          "fill-color": "#10B981",
          "fill-opacity": 0.08,
        },
      },
      {
        id: "tf-parks-outline",
        type: "line",
        source: "tf-vector",
        "source-layer": "capad_parks",
        layout: { visibility: vis(layers.parksWaterways) },
        paint: {
          "line-color": "#34D399",
          "line-width": 1.4,
          "line-dasharray": [4, 2],
          "line-opacity": 0.56,
        },
      }
    );

    // 2. Configurable Alpine Snow-Line Elevation Band
    styleLayers.push(
      {
        id: "tf-snow-line-fill",
        type: "fill",
        source: "tf-vector",
        "source-layer": "snowline_bands",
        filter: snowFilter,
        layout: { visibility: vis(layers.snowLine) },
        paint: {
          "fill-color": "#BAE6FD",
          "fill-opacity": 0.25,
        },
      },
      {
        id: "tf-snow-line-outline",
        type: "line",
        source: "tf-vector",
        "source-layer": "snowline_bands",
        filter: snowFilter,
        layout: { visibility: vis(layers.snowLine) },
        paint: {
          "line-color": "#7DD3FC",
          "line-width": 1.8,
          "line-dasharray": [3, 2],
          "line-opacity": 0.88,
        },
      }
    );

    // 3. Topographic Contours (subtle terrain context so foreground tracks pop cleanly)
    styleLayers.push(
      {
        id: "tf-contours-hillshade",
        type: "line",
        source: "tf-vector",
        "source-layer": "contours",
        filter: ["!=", ["get", "is_index"], true],
        minzoom: 11,
        layout: { visibility: vis(layers.contoursHillshade) },
        paint: {
          "line-color": "#7A91A8",
          "line-width": 0.65,
          "line-opacity": 0.28,
        },
      },
      {
        id: "tf-contours-index",
        type: "line",
        source: "tf-vector",
        "source-layer": "contours",
        filter: ["==", ["get", "is_index"], true],
        minzoom: 9,
        layout: { visibility: vis(layers.contoursHillshade) },
        paint: {
          "line-color": "#9BB1C5",
          "line-width": 1.25,
          "line-opacity": 0.52,
        },
      },
      {
        id: "tf-contours-labels",
        type: "symbol",
        source: "tf-vector",
        "source-layer": "contours",
        filter: ["==", ["get", "is_index"], true],
        minzoom: 12.2,
        layout: {
          visibility: vis(layers.contoursHillshade),
          "symbol-placement": "line",
          "symbol-spacing": 520,
          "icon-padding": 28,
          "icon-image": [
            "concat",
            "elev-",
            ["to-string", ["round", ["coalesce", ["get", "elev_m"], 0]]],
          ],
          "icon-rotation-alignment": "map",
          "icon-keep-upright": true,
          "icon-allow-overlap": false,
        },
        paint: {
          "icon-opacity": 0.78,
        },
      }
    );

    // 4. GA / OSM Waterways (muted hydro slate-blue so creeks never compete with Medium tracks)
    styleLayers.push(
      {
        id: "tf-waterways-perennial",
        type: "line",
        source: "tf-vector",
        "source-layer": "waterways",
        filter: ["==", ["get", "perennial"], true],
        layout: {
          visibility: vis(layers.parksWaterways),
          "line-cap": "round",
          "line-join": "round",
        },
        paint: {
          "line-color": "#3B82A6",
          "line-width": ["interpolate", ["linear"], ["zoom"], 9, 1.0, 14, 2.2],
          "line-opacity": 0.58,
        },
      },
      {
        id: "tf-waterways-intermittent",
        type: "line",
        source: "tf-vector",
        "source-layer": "waterways",
        filter: ["!=", ["get", "perennial"], true],
        minzoom: 12,
        layout: {
          visibility: vis(layers.parksWaterways),
          "line-cap": "round",
        },
        paint: {
          "line-color": "#4E89AB",
          "line-width": ["interpolate", ["linear"], ["zoom"], 12, 0.75, 15, 1.25],
          "line-dasharray": [3, 3],
          "line-opacity": 0.38,
        },
      }
    );

    // 4b. Major Australian Highways (rendered BELOW 4WD tracks so graded tracks remain foreground hero)
    styleLayers.push(...buildHighwayLineLayers(vis(layers.highways)));

    // 5. Color-coded Canonical Graded 4WD Tracks (crisp dark-cased foreground ribbons)
    styleLayers.push(
      {
        id: "tf-tracks-casing",
        type: "line",
        source: "tf-vector",
        "source-layer": "track_segments",
        layout: {
          visibility: vis(layers.tracks),
          "line-cap": "round",
          "line-join": "round",
        },
        paint: {
          "line-color": "#070B10",
          "line-width": ["interpolate", ["linear"], ["zoom"], 9, 4.8, 14, 7.8],
          "line-opacity": trackOpacities.casing,
        },
      },
      {
        id: "tf-tracks-line",
        type: "line",
        source: "tf-vector",
        "source-layer": "track_segments",
        layout: {
          visibility: vis(layers.tracks),
          "line-cap": "round",
          "line-join": "round",
        },
        paint: {
          "line-color": gradeColor,
          "line-width": ["interpolate", ["linear"], ["zoom"], 9, 2.6, 14, 4.6],
          "line-opacity": trackOpacities.line,
        },
      },
      {
        id: "tf-tracks-access-warn",
        type: "line",
        source: "tf-vector",
        "source-layer": "track_segments",
        filter: [
          "any",
          ["==", ["get", "has_gate_barrier"], true],
          [
            "in",
            ["coalesce", ["get", "access_status"], "public"],
            ["literal", ["private", "restricted", "closed"]],
          ],
        ],
        layout: {
          visibility: vis(layers.tracks),
          "line-cap": "butt",
        },
        paint: {
          "line-color": "#FDE047",
          "line-width": 2.2,
          "line-dasharray": [2, 2],
          "line-opacity": trackOpacities.accessWarn,
        },
      }
    );

    // 6. Highlighted Steep Sections (>=14° — crisp inner dash that preserves track grade color)
    styleLayers.push({
      id: "tf-steep-highlights",
      type: "line",
      source: "tf-vector",
      "source-layer": "steep_sections",
      layout: {
        visibility: vis(layers.steepHighlights),
        "line-cap": "round",
      },
      paint: {
        "line-color": "#FB7185",
        "line-width": ["interpolate", ["linear"], ["zoom"], 10, 1.8, 14, 2.6],
        "line-dasharray": [1.3, 2.1],
        "line-opacity": trackOpacities.steep,
      },
    });

    // 7. Selected Track Spotlight Stack (rendered strictly ABOVE all unselected tracks)
    styleLayers.push(
      {
        id: "tf-tracks-selected-glow",
        type: "line",
        source: "tf-vector",
        "source-layer": "track_segments",
        filter: selectedFilter,
        layout: {
          visibility: vis(layers.tracks),
          "line-cap": "round",
          "line-join": "round",
        },
        paint: {
          "line-color": "#FDE047",
          "line-width": ["interpolate", ["linear"], ["zoom"], 9, 16, 14, 24],
          "line-blur": 5,
          "line-opacity": 0.62,
        },
      },
      {
        id: "tf-tracks-selected",
        type: "line",
        source: "tf-vector",
        "source-layer": "track_segments",
        filter: selectedFilter,
        layout: {
          visibility: vis(layers.tracks),
          "line-cap": "round",
          "line-join": "round",
        },
        paint: {
          "line-color": "#FFFFFF",
          "line-width": ["interpolate", ["linear"], ["zoom"], 9, 10.5, 14, 15.5],
          "line-opacity": 0.98,
        },
      },
      {
        id: "tf-tracks-selected-inner-casing",
        type: "line",
        source: "tf-vector",
        "source-layer": "track_segments",
        filter: selectedFilter,
        layout: {
          visibility: vis(layers.tracks),
          "line-cap": "round",
          "line-join": "round",
        },
        paint: {
          "line-color": "#070B10",
          "line-width": ["interpolate", ["linear"], ["zoom"], 9, 7.0, 14, 10.6],
          "line-opacity": 0.96,
        },
      },
      {
        id: "tf-tracks-selected-core",
        type: "line",
        source: "tf-vector",
        "source-layer": "track_segments",
        filter: selectedFilter,
        layout: {
          visibility: vis(layers.tracks),
          "line-cap": "round",
          "line-join": "round",
        },
        paint: {
          "line-color": gradeColor,
          "line-width": ["interpolate", ["linear"], ["zoom"], 9, 4.4, 14, 6.8],
          "line-opacity": 1.0,
        },
      },
      {
        id: SELECTED_TRACK_STEEP_LAYER_ID,
        type: "line",
        source: "tf-vector",
        "source-layer": "steep_sections",
        filter: selectedFilter,
        layout: {
          visibility: vis(layers.tracks && layers.steepHighlights),
          "line-cap": "round",
        },
        paint: {
          "line-color": "#FFE4E6",
          "line-width": ["interpolate", ["linear"], ["zoom"], 10, 2.0, 14, 2.8],
          "line-dasharray": [1.3, 2.1],
          "line-opacity": 0.94,
        },
      }
    );
  } else {
    // GeoJSON fallback layers (used automatically when PMTiles are unavailable)
    styleLayers.push(
      {
        id: "tf-contours-hillshade",
        type: "line",
        source: "tf-contours",
        layout: { visibility: vis(layers.contoursHillshade) },
        paint: {
          "line-color": "#64748B",
          "line-width": 0.75,
          "line-opacity": 0.35,
        },
      },
      {
        id: "tf-snow-line-fill",
        type: "fill",
        source: "tf-snow-line",
        layout: { visibility: vis(layers.snowLine) },
        paint: {
          "fill-color": "#BAE6FD",
          "fill-opacity": 0.24,
        },
      },
      {
        id: "tf-snow-line-outline",
        type: "line",
        source: "tf-snow-line",
        layout: { visibility: vis(layers.snowLine) },
        paint: {
          "line-color": "#7DD3FC",
          "line-width": 1.6,
          "line-dasharray": [3, 2],
          "line-opacity": 0.85,
        },
      },
      ...buildHighwayLineLayers(vis(layers.highways)),
      {
        id: "tf-tracks-casing",
        type: "line",
        source: "tf-tracks",
        layout: {
          visibility: vis(layers.tracks),
          "line-cap": "round",
          "line-join": "round",
        },
        paint: {
          "line-color": "#070B10",
          "line-width": 6.4,
          "line-opacity": trackOpacities.casing,
        },
      },
      {
        id: "tf-tracks-line",
        type: "line",
        source: "tf-tracks",
        layout: {
          visibility: vis(layers.tracks),
          "line-cap": "round",
          "line-join": "round",
        },
        paint: {
          "line-color": gradeColor,
          "line-width": 3.8,
          "line-opacity": trackOpacities.line,
        },
      },
      {
        id: "tf-steep-highlights",
        type: "line",
        source: "tf-steep",
        layout: {
          visibility: vis(layers.steepHighlights),
          "line-cap": "round",
        },
        paint: {
          "line-color": "#FB7185",
          "line-width": 2.2,
          "line-dasharray": [1.3, 2.1],
          "line-opacity": trackOpacities.steep,
        },
      },
      {
        id: "tf-tracks-selected-glow",
        type: "line",
        source: "tf-tracks",
        filter: selectedFilter,
        layout: {
          visibility: vis(layers.tracks),
          "line-cap": "round",
          "line-join": "round",
        },
        paint: {
          "line-color": "#FDE047",
          "line-width": 20,
          "line-blur": 5,
          "line-opacity": 0.62,
        },
      },
      {
        id: "tf-tracks-selected",
        type: "line",
        source: "tf-tracks",
        filter: selectedFilter,
        layout: {
          visibility: vis(layers.tracks),
          "line-cap": "round",
          "line-join": "round",
        },
        paint: {
          "line-color": "#FFFFFF",
          "line-width": 13.5,
          "line-opacity": 0.98,
        },
      },
      {
        id: "tf-tracks-selected-inner-casing",
        type: "line",
        source: "tf-tracks",
        filter: selectedFilter,
        layout: {
          visibility: vis(layers.tracks),
          "line-cap": "round",
          "line-join": "round",
        },
        paint: {
          "line-color": "#070B10",
          "line-width": 9.2,
          "line-opacity": 0.96,
        },
      },
      {
        id: "tf-tracks-selected-core",
        type: "line",
        source: "tf-tracks",
        filter: selectedFilter,
        layout: {
          visibility: vis(layers.tracks),
          "line-cap": "round",
          "line-join": "round",
        },
        paint: {
          "line-color": gradeColor,
          "line-width": 5.8,
          "line-opacity": 1.0,
        },
      },
      {
        id: SELECTED_TRACK_STEEP_LAYER_ID,
        type: "line",
        source: "tf-steep",
        filter: selectedFilter,
        layout: {
          visibility: vis(layers.tracks && layers.steepHighlights),
          "line-cap": "round",
        },
        paint: {
          "line-color": "#FFE4E6",
          "line-width": 2.4,
          "line-dasharray": [1.3, 2.1],
          "line-opacity": 0.94,
        },
      }
    );
  }

  const closureColor = buildClosureColorExpression();
  const selectedClosureFilter = buildSelectedClosureFilter(selectedClosureId);

  // Line & polygon overlays (all rendered BELOW point/icon/marker layers)
  // Slice 04 Live Closures, Seasonal Restrictions, Incidents & Gates hazard lines/polygons
  styleLayers.push(
    {
      id: "tf-closures-polygon-fill",
      type: "fill",
      source: "tf-closures",
      filter: ["==", ["geometry-type"], "Polygon"],
      layout: { visibility: vis(layers.closures) },
      paint: {
        "fill-color": closureColor,
        "fill-opacity": 0.18,
      },
    },
    {
      id: "tf-closures-polygon-outline",
      type: "line",
      source: "tf-closures",
      filter: ["==", ["geometry-type"], "Polygon"],
      layout: { visibility: vis(layers.closures) },
      paint: {
        "line-color": closureColor,
        "line-width": 2.2,
        "line-dasharray": [4, 2],
        "line-opacity": 0.92,
      },
    },
    {
      id: SELECTED_CLOSURE_OUTLINE_LAYER_ID,
      type: "line",
      source: "tf-closures",
      filter: selectedClosureFilter,
      layout: { visibility: vis(layers.closures) },
      paint: {
        "line-color": "#FDE047",
        "line-width": 4.2,
        "line-opacity": 0.96,
      },
    },
    {
      id: "tf-closures-line-casing",
      type: "line",
      source: "tf-closures",
      filter: ["==", ["geometry-type"], "LineString"],
      layout: {
        visibility: vis(layers.closures),
        "line-cap": "round",
        "line-join": "round",
      },
      paint: {
        "line-color": "#090F14",
        "line-width": 8.2,
        "line-opacity": 0.84,
      },
    },
    {
      id: "tf-closures-line",
      type: "line",
      source: "tf-closures",
      filter: ["==", ["geometry-type"], "LineString"],
      layout: {
        visibility: vis(layers.closures),
        "line-cap": "butt",
        "line-join": "round",
      },
      paint: {
        "line-color": closureColor,
        "line-width": 4.8,
        "line-dasharray": [2.2, 1.6],
        "line-opacity": 0.98,
      },
    }
  );

  // Slice 07 Rough / Slow Obstacle Hotspots line overlays
  styleLayers.push(
    {
      id: "tf-hotspots-casing",
      type: "line",
      source: "tf-hotspots",
      filter: ["in", ["geometry-type"], ["literal", ["LineString", "MultiLineString"]]],
      layout: {
        visibility: vis(layers.hotspots),
        "line-cap": "round",
        "line-join": "round",
      },
      paint: {
        "line-color": "#090F14",
        "line-width": ["interpolate", ["linear"], ["zoom"], 9, 7.6, 14, 11.2],
        "line-opacity": 0.78,
      },
    },
    {
      id: "tf-hotspots-line",
      type: "line",
      source: "tf-hotspots",
      filter: ["in", ["geometry-type"], ["literal", ["LineString", "MultiLineString"]]],
      layout: {
        visibility: vis(layers.hotspots),
        "line-cap": "round",
        "line-join": "round",
      },
      paint: {
        "line-color": "#FF6B00",
        "line-width": ["interpolate", ["linear"], ["zoom"], 9, 4.2, 14, 6.8],
        "line-dasharray": [1.8, 1.4],
        "line-opacity": 0.96,
      },
    }
  );

  // Slice 06 Anonymized Unmapped Track Candidates (subtle violet dashed spurs)
  styleLayers.push(
    {
      id: "tf-unmapped-casing",
      type: "line",
      source: "tf-unmapped",
      layout: {
        visibility: vis(layers.unmapped),
        "line-cap": "round",
        "line-join": "round",
      },
      paint: {
        "line-color": "#0B1117",
        "line-width": 5.2,
        "line-opacity": 0.65,
      },
    },
    {
      id: "tf-unmapped-line",
      type: "line",
      source: "tf-unmapped",
      layout: {
        visibility: vis(layers.unmapped),
        "line-cap": "round",
        "line-join": "round",
      },
      paint: {
        "line-color": "#A855F7",
        "line-width": 2.8,
        "line-dasharray": [3, 2.5],
        "line-opacity": 0.9,
      },
    }
  );

  // Slice 09 In-Cab PWA Breadcrumb Recorder & One-Tap Backtrack Route lines
  styleLayers.push(
    {
      id: "tf-recorded-track-casing",
      type: "line",
      source: "tf-recorded-track",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": "#052E16",
        "line-width": 7.5,
        "line-opacity": 0.85,
      },
    },
    {
      id: "tf-recorded-track-line",
      type: "line",
      source: "tf-recorded-track",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": "#34D399",
        "line-width": 4.2,
        "line-opacity": 0.98,
      },
    },
    {
      id: "tf-backtrack-route-casing",
      type: "line",
      source: "tf-backtrack-route",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": "#0B1117",
        "line-width": 8.4,
        "line-opacity": 0.88,
      },
    },
    {
      id: "tf-backtrack-route-line",
      type: "line",
      source: "tf-backtrack-route",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": "#F472B6",
        "line-width": 4.8,
        "line-dasharray": [2.4, 1.8],
        "line-opacity": 0.98,
      },
    }
  );

  // Phase A Interactive Multi-Track Planned Route lines (bright cyan/emerald #14b8a6)
  styleLayers.push(
    {
      id: "tf-planned-route-casing",
      type: "line",
      source: "tf-planned-route",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": "#042F2E",
        "line-width": ["interpolate", ["linear"], ["zoom"], 9, 8.6, 14, 12.6],
        "line-opacity": 0.9,
      },
    },
    {
      id: "tf-planned-route-line",
      type: "line",
      source: "tf-planned-route",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": "#14b8a6",
        "line-width": ["interpolate", ["linear"], ["zoom"], 9, 4.6, 14, 7.2],
        "line-opacity": 0.98,
      },
    }
  );

  // =========================================================================
  // Point, Icon & Marker Layers — strictly ABOVE all track & overlay lines
  // =========================================================================

  // Subtle city / regional hub / town dots (above highway & track lines, below operational pins)
  styleLayers.push({
    id: "tf-places-cities-dots",
    type: "circle",
    source: "tf-places",
    filter: [
      "in",
      ["coalesce", ["get", "place_class"], ""],
      ["literal", ["capital", "city", "regional_hub", "town"]],
    ],
    minzoom: 4,
    maxzoom: 13.5,
    layout: {
      visibility: vis(layers.placesRegions),
    },
    paint: {
      "circle-radius": [
        "interpolate",
        ["linear"],
        ["zoom"],
        4,
        [
          "match",
          ["coalesce", ["get", "place_class"], ""],
          "capital",
          3.6,
          "city",
          2.8,
          0,
        ],
        6,
        [
          "match",
          ["coalesce", ["get", "place_class"], ""],
          "capital",
          4.2,
          "city",
          3.4,
          "regional_hub",
          2.8,
          0,
        ],
        7.2,
        [
          "match",
          ["coalesce", ["get", "place_class"], ""],
          "capital",
          4.5,
          "city",
          3.6,
          "regional_hub",
          3.1,
          "town",
          2.4,
          0,
        ],
        11,
        [
          "match",
          ["coalesce", ["get", "place_class"], ""],
          "capital",
          4.8,
          "city",
          3.8,
          "regional_hub",
          3.3,
          "town",
          2.8,
          0,
        ],
      ],
      "circle-color": [
        "match",
        ["coalesce", ["get", "place_class"], ""],
        "capital",
        "#F8FAFC",
        "city",
        "#F1F5F9",
        "#E2E8F0",
      ],
      "circle-stroke-color": "#090E14",
      "circle-stroke-width": [
        "step",
        ["zoom"],
        0,
        4,
        [
          "match",
          ["coalesce", ["get", "place_class"], ""],
          "capital",
          1.6,
          "city",
          1.4,
          0,
        ],
        5.8,
        [
          "match",
          ["coalesce", ["get", "place_class"], ""],
          "capital",
          1.6,
          "city",
          1.4,
          "regional_hub",
          1.3,
          0,
        ],
        7.2,
        1.3,
      ],
      "circle-opacity": [
        "interpolate",
        ["linear"],
        ["zoom"],
        4,
        [
          "match",
          ["coalesce", ["get", "place_class"], ""],
          "capital",
          0.95,
          "city",
          0.9,
          0,
        ],
        5.8,
        [
          "match",
          ["coalesce", ["get", "place_class"], ""],
          "capital",
          0.95,
          "city",
          0.92,
          "regional_hub",
          0.88,
          0,
        ],
        7.2,
        0.88,
        12.2,
        0.82,
        13.4,
        0,
      ],
      "circle-stroke-opacity": [
        "interpolate",
        ["linear"],
        ["zoom"],
        4,
        [
          "match",
          ["coalesce", ["get", "place_class"], ""],
          "capital",
          0.95,
          "city",
          0.9,
          0,
        ],
        5.8,
        [
          "match",
          ["coalesce", ["get", "place_class"], ""],
          "capital",
          0.95,
          "city",
          0.92,
          "regional_hub",
          0.88,
          0,
        ],
        7.2,
        0.88,
        12.2,
        0.82,
        13.4,
        0,
      ],
    },
  });

  // Obstacle hotspot point markers
  styleLayers.push({
    id: "tf-hotspots-points",
    type: "circle",
    source: "tf-hotspots",
    filter: ["==", ["geometry-type"], "Point"],
    layout: { visibility: vis(layers.hotspots) },
    paint: {
      "circle-radius": [
        "interpolate",
        ["linear"],
        ["zoom"],
        10,
        3.2,
        13,
        4.5,
        16,
        6.5,
      ],
      "circle-color": "#FF6B00",
      "circle-opacity": 0.88,
      "circle-stroke-color": "#FEF08A",
      "circle-stroke-width": 1.2,
    },
  });

  // Outdoor POIs with high-contrast backing + adapted SVG symbols (on top of all track/closure lines)
  if (useVectorPmtiles) {
    styleLayers.push(
      {
        id: "tf-pois-layer",
        type: "circle",
        source: "tf-vector",
        "source-layer": "pois",
        layout: { visibility: vis(layers.pois) },
        paint: {
          "circle-radius": 12,
          "circle-color": "rgba(241, 245, 249, 0.96)",
          "circle-stroke-color": "#0F172A",
          "circle-stroke-width": 1.8,
        },
      },
      {
        id: "tf-pois-icons",
        type: "symbol",
        source: "tf-vector",
        "source-layer": "pois",
        layout: {
          visibility: vis(layers.pois),
          "icon-image": [
            "concat",
            "poi-",
            ["coalesce", ["get", "icon"], ["get", "category"], "trig"],
          ],
          "icon-size": 0.72,
          "icon-allow-overlap": true,
        },
      }
    );
  } else {
    styleLayers.push(
      {
        id: "tf-pois-layer",
        type: "circle",
        source: "tf-pois",
        layout: { visibility: vis(layers.pois) },
        paint: {
          "circle-radius": 11.5,
          "circle-color": "rgba(241, 245, 249, 0.96)",
          "circle-stroke-color": "#0F172A",
          "circle-stroke-width": 1.8,
        },
      },
      {
        id: "tf-pois-icons",
        type: "symbol",
        source: "tf-pois",
        layout: {
          visibility: vis(layers.pois),
          "icon-image": [
            "concat",
            "poi-",
            ["coalesce", ["get", "icon"], ["get", "category"], "trig"],
          ],
          "icon-size": 0.72,
          "icon-allow-overlap": true,
        },
      }
    );
  }

  // Closure & incident point markers
  styleLayers.push({
    id: "tf-closures-points",
    type: "circle",
    source: "tf-closures",
    filter: ["==", ["geometry-type"], "Point"],
    layout: { visibility: vis(layers.closures) },
    paint: {
      "circle-radius": 7.5,
      "circle-color": closureColor,
      "circle-stroke-color": "#F8FAFC",
      "circle-stroke-width": 2,
    },
  });

  const reportColor = buildReportColorExpression();

  // Slice 08 Community Condition Reports & Distinct Locked Gate / Private Property Warnings
  styleLayers.push(
    {
      id: "tf-reports-gate-halo",
      type: "circle",
      source: "tf-reports",
      filter: [
        "any",
        ["==", ["get", "is_gate_warning"], true],
        [
          "in",
          ["downcase", ["coalesce", ["get", "report_type"], ""]],
          ["literal", ["locked_gate", "private_property"]],
        ],
      ],
      layout: { visibility: vis(layers.reports) },
      paint: {
        "circle-radius": 13.5,
        "circle-color": reportColor,
        "circle-opacity": 0.24,
        "circle-stroke-color": "#FDE047",
        "circle-stroke-width": 1.8,
      },
    },
    {
      id: "tf-reports-points",
      type: "circle",
      source: "tf-reports",
      layout: { visibility: vis(layers.reports) },
      paint: {
        "circle-radius": 7.2,
        "circle-color": reportColor,
        "circle-stroke-color": "#F8FAFC",
        "circle-stroke-width": 2.0,
      },
    }
  );

  // Recorder live GPS position marker
  styleLayers.push(
    {
      id: "tf-recorder-head-halo",
      type: "circle",
      source: "tf-recorder-head",
      paint: {
        "circle-radius": 14,
        "circle-color": [
          "case",
          ["==", ["get", "backtrack"], true],
          "rgba(244, 114, 182, 0.28)",
          "rgba(52, 211, 153, 0.28)",
        ],
        "circle-stroke-color": [
          "case",
          ["==", ["get", "backtrack"], true],
          "#F472B6",
          "#34D399",
        ],
        "circle-stroke-width": 2,
      },
    },
    {
      id: "tf-recorder-head-dot",
      type: "circle",
      source: "tf-recorder-head",
      paint: {
        "circle-radius": 5.5,
        "circle-color": "#FFFFFF",
        "circle-stroke-color": "#0B1117",
        "circle-stroke-width": 2,
      },
    }
  );

  // Planned route start/leg/waypoint/finish badges
  styleLayers.push(
    {
      id: "tf-planned-route-pins-dot",
      type: "circle",
      source: "tf-planned-route-pins",
      paint: {
        "circle-radius": 11,
        "circle-color": [
          "match",
          ["coalesce", ["get", "kind"], "leg"],
          "start",
          "#10B981",
          "finish",
          "#F59E0B",
          "waypoint",
          "#38BDF8",
          "#14b8a6",
        ],
        "circle-stroke-color": "#042F2E",
        "circle-stroke-width": 2.2,
      },
    },
    {
      id: "tf-planned-route-pins-badge",
      type: "symbol",
      source: "tf-planned-route-pins",
      layout: {
        "icon-image": [
          "concat",
          "route-pin-",
          ["coalesce", ["get", "pin_key"], "start-S"],
        ],
        "icon-size": 1,
        "icon-allow-overlap": true,
      },
    }
  );

  // Slice 05 Personal User Pins (always visible when signed in)
  styleLayers.push(
    {
      id: "tf-user-pins-halo",
      type: "circle",
      source: "tf-user-pins",
      paint: {
        "circle-radius": 11,
        "circle-color": ["coalesce", ["get", "color"], "#EC4899"],
        "circle-opacity": 0.24,
      },
    },
    {
      id: "tf-user-pins-dot",
      type: "circle",
      source: "tf-user-pins",
      paint: {
        "circle-radius": 6,
        "circle-color": ["coalesce", ["get", "color"], "#EC4899"],
        "circle-stroke-color": "#FFFFFF",
        "circle-stroke-width": 2,
      },
    }
  );

  // =========================================================================
  // Dedicated Top-of-Stack Text Label Band — ABOVE hillshade, parks,
  // contours, waterways, highways, 4WD tracks & markers
  // (Ordered lowest -> highest collision priority: MapLibre places later
  // symbol layers first during GPU collision avoidance.)
  // =========================================================================
  const highwayClassExpr: ExpressionSpecification = [
    "downcase",
    ["coalesce", ["get", "highway_class"], "primary"],
  ];
  const highwayFullLabelExpr: ExpressionSpecification = [
    "case",
    [
      "all",
      ["!=", ["coalesce", ["get", "ref"], ""], ""],
      ["!=", ["coalesce", ["get", "name"], ""], ""],
    ],
    ["concat", ["get", "ref"], " · ", ["get", "name"]],
    ["!=", ["coalesce", ["get", "name"], ""], ""],
    ["get", "name"],
    ["coalesce", ["get", "ref"], ""],
  ];

  styleLayers.push(
    // Highway route shield + name labels
    {
      id: HIGHWAY_LABEL_LAYER_ID,
      type: "symbol",
      source: "tf-highways",
      minzoom: 6.5,
      layout: {
        visibility: vis(layers.highways),
        "symbol-placement": "line",
        "symbol-spacing": 360,
        "text-field": [
          "step",
          ["zoom"],
          "",
          6.5,
          [
            "case",
            ["<=", ["coalesce", ["get", "minzoom"], 6], 5.5],
            highwayFullLabelExpr,
            "",
          ],
          8.5,
          highwayFullLabelExpr,
        ],
        "text-font": ["Noto Sans Medium"],
        "text-size": ["interpolate", ["linear"], ["zoom"], 6.5, 9.5, 11, 11, 14, 12],
        "text-letter-spacing": 0.03,
        "text-max-angle": 30,
        "text-padding": 6,
        "text-allow-overlap": false,
        "symbol-sort-key": [
          "match",
          highwayClassExpr,
          "motorway",
          10,
          "trunk",
          11,
          12,
        ],
      },
      paint: {
        "text-color": [
          "match",
          highwayClassExpr,
          "motorway",
          "#FDE68A",
          "trunk",
          "#FEF08A",
          "#E2E8F0",
        ],
        "text-halo-color": "rgba(9, 14, 20, 0.92)",
        "text-halo-width": 1.8,
        "text-halo-blur": 0.5,
        "text-opacity": ["interpolate", ["linear"], ["zoom"], 6.5, 0.85, 8, 0.96],
      },
    },
    // Villages & rural 4WD localities (z9.8+)
    {
      id: "tf-places-locality",
      type: "symbol",
      source: "tf-places",
      filter: [
        "in",
        ["coalesce", ["get", "place_class"], ""],
        ["literal", ["village", "locality"]],
      ],
      minzoom: 9.8,
      layout: {
        visibility: vis(layers.placesRegions),
        "text-field": [
          "step",
          ["zoom"],
          [
            "case",
            ["<=", ["coalesce", ["get", "minzoom"], 9.8], 10.2],
            ["get", "name"],
            "",
          ],
          11.0,
          ["get", "name"],
        ],
        "text-font": ["Noto Sans Regular"],
        "text-size": ["interpolate", ["linear"], ["zoom"], 9.8, 9.5, 12, 10.8, 15, 12],
        "text-max-width": 9,
        "text-padding": 4,
        "text-allow-overlap": false,
        "symbol-sort-key": ["coalesce", ["get", "rank"], 8],
      },
      paint: {
        "text-color": "#CBD5E1",
        "text-halo-color": "rgba(9, 14, 20, 0.92)",
        "text-halo-width": 1.8,
        "text-halo-blur": 0.5,
      },
    }
  );

  // CAPAD National Park & Reserve labels from Vector PMTiles (z7.5–z14.5, tiered by area_ha)
  if (useVectorPmtiles) {
    styleLayers.push({
      id: "tf-park-labels",
      type: "symbol",
      source: "tf-vector",
      "source-layer": "park_labels",
      minzoom: 7.5,
      maxzoom: 14.8,
      layout: {
        visibility: vis(layers.placesRegions),
        "text-field": [
          "step",
          ["zoom"],
          [
            "case",
            [">=", ["coalesce", ["get", "area_ha"], 0], 100000],
            ["coalesce", ["get", "name"], ""],
            "",
          ],
          9.0,
          [
            "case",
            [">=", ["coalesce", ["get", "area_ha"], 0], 20000],
            ["coalesce", ["get", "name"], ""],
            "",
          ],
          10.5,
          ["coalesce", ["get", "name"], ""],
        ],
        "text-font": ["Noto Sans Italic"],
        "text-size": ["interpolate", ["linear"], ["zoom"], 7.5, 10, 11, 11.8, 14, 13],
        "text-letter-spacing": 0.04,
        "text-max-width": 9,
        "text-padding": 5,
        "text-allow-overlap": false,
        "symbol-sort-key": [
          "case",
          [">=", ["coalesce", ["get", "area_ha"], 0], 100000],
          6,
          [">=", ["coalesce", ["get", "area_ha"], 0], 20000],
          7,
          8,
        ],
      },
      paint: {
        "text-color": "#86EFAC",
        "text-halo-color": "rgba(9, 14, 20, 0.92)",
        "text-halo-width": 1.8,
        "text-halo-blur": 0.5,
        "text-opacity": [
          "interpolate",
          ["linear"],
          ["zoom"],
          7.5,
          0.84,
          9.0,
          0.92,
          13.8,
          0.85,
          14.5,
          0,
        ],
      },
    });
  }

  styleLayers.push(
    // Towns & 4WD gateways (z7.0+)
    {
      id: "tf-places-town",
      type: "symbol",
      source: "tf-places",
      filter: ["==", ["get", "place_class"], "town"],
      minzoom: 7.0,
      layout: {
        visibility: vis(layers.placesRegions),
        "text-field": ["get", "name"],
        "text-font": ["Noto Sans Medium"],
        "text-size": ["interpolate", ["linear"], ["zoom"], 7, 10.5, 11, 12.2, 14, 13.5],
        "text-offset": [0, 0.55],
        "text-anchor": "top",
        "text-max-width": 9,
        "text-padding": 4,
        "text-allow-overlap": false,
        "symbol-sort-key": ["coalesce", ["get", "rank"], 6],
      },
      paint: {
        "text-color": "#F1F5F9",
        "text-halo-color": "rgba(9, 14, 20, 0.92)",
        "text-halo-width": 1.8,
        "text-halo-blur": 0.5,
      },
    },
    // Australian geographic & 4WD regions (z5.0–z10.8)
    {
      id: "tf-places-region",
      type: "symbol",
      source: "tf-places",
      filter: ["==", ["get", "place_class"], "region"],
      minzoom: 5.0,
      maxzoom: 11.0,
      layout: {
        visibility: vis(layers.placesRegions),
        "text-field": ["get", "name"],
        "text-transform": "uppercase",
        "text-font": ["Noto Sans Italic"],
        "text-letter-spacing": 0.16,
        "text-size": ["interpolate", ["linear"], ["zoom"], 5.2, 10.5, 7.5, 12.5, 10, 14],
        "text-max-width": 10,
        "text-padding": 6,
        "text-allow-overlap": false,
        "symbol-sort-key": ["coalesce", ["get", "rank"], 5],
      },
      paint: {
        "text-color": "#FDE68A",
        "text-halo-color": "rgba(9, 14, 20, 0.92)",
        "text-halo-width": 1.8,
        "text-halo-blur": 0.5,
        "text-opacity": [
          "interpolate",
          ["linear"],
          ["zoom"],
          5.0,
          0.0,
          5.5,
          0.92,
          9.6,
          0.88,
          10.8,
          0.0,
        ],
      },
    },
    // Australian States & Territories (z3–z7.6)
    {
      id: "tf-places-state",
      type: "symbol",
      source: "tf-places",
      filter: ["==", ["get", "place_class"], "state"],
      minzoom: 3,
      maxzoom: 7.7,
      layout: {
        visibility: vis(layers.placesRegions),
        "text-field": ["get", "name"],
        "text-transform": "uppercase",
        "text-font": ["Noto Sans Bold"],
        "text-letter-spacing": 0.22,
        "text-size": ["interpolate", ["linear"], ["zoom"], 3.5, 10.5, 5.5, 13.5, 7.2, 15],
        "text-max-width": 11,
        "text-allow-overlap": false,
        "symbol-sort-key": ["coalesce", ["get", "rank"], 3],
      },
      paint: {
        "text-color": "#CBD5E1",
        "text-halo-color": "rgba(9, 14, 20, 0.92)",
        "text-halo-width": 1.8,
        "text-halo-blur": 0.5,
        "text-opacity": [
          "interpolate",
          ["linear"],
          ["zoom"],
          3.2,
          0.82,
          5.0,
          0.92,
          6.5,
          0.88,
          7.6,
          0,
        ],
      },
    },
    // State capitals, major cities & regional hubs (highest symbol priority)
    {
      id: "tf-places-major",
      type: "symbol",
      source: "tf-places",
      filter: [
        "in",
        ["coalesce", ["get", "place_class"], ""],
        ["literal", ["capital", "city", "regional_hub"]],
      ],
      minzoom: 3,
      layout: {
        visibility: vis(layers.placesRegions),
        "text-field": [
          "step",
          ["zoom"],
          [
            "case",
            ["<=", ["coalesce", ["get", "minzoom"], 3], 3.5],
            ["get", "name"],
            "",
          ],
          4.5,
          [
            "case",
            ["<=", ["coalesce", ["get", "minzoom"], 4.5], 4.8],
            ["get", "name"],
            "",
          ],
          5.8,
          ["get", "name"],
        ],
        "text-font": ["Noto Sans Bold"],
        "text-size": ["interpolate", ["linear"], ["zoom"], 4, 11.5, 8, 13.5, 13, 15.5],
        "text-offset": [0, 0.7],
        "text-anchor": "top",
        "text-max-width": 9,
        "text-padding": 5,
        "text-allow-overlap": false,
        "symbol-sort-key": ["coalesce", ["get", "rank"], 4],
      },
      paint: {
        "text-color": "#F8FAFC",
        "text-halo-color": "rgba(9, 14, 20, 0.92)",
        "text-halo-width": 1.8,
        "text-halo-blur": 0.5,
      },
    }
  );

  // Coordinate / MGRS / POI search target pin (always on top)
  styleLayers.push(
    {
      id: "tf-search-pin-halo",
      type: "circle",
      source: "tf-search-pin",
      paint: {
        "circle-radius": 14,
        "circle-color": "rgba(245, 158, 11, 0.22)",
        "circle-stroke-color": "#F59E0B",
        "circle-stroke-width": 2,
      },
    },
    {
      id: "tf-search-pin-dot",
      type: "circle",
      source: "tf-search-pin",
      paint: {
        "circle-radius": 4.8,
        "circle-color": "#FBBF24",
        "circle-stroke-color": "#0B1117",
        "circle-stroke-width": 1.6,
      },
    }
  );

  return {
    version: 8,
    glyphs: resolveGlyphsUrl(effectiveAssetsOrigin),
    sources,
    layers: styleLayers,
  };
}
