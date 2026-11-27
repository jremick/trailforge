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
import {
  AU_PLACES_ATTRIBUTION,
  DEFAULT_VECTOR_BASEMAP_ATTRIBUTION,
  DEFAULT_VECTOR_BASEMAP_URL,
  VECTOR_BASEMAP_SOURCE_ID,
  buildVectorBasemapBottomLayers,
} from "./style/basemapVector";
import { buildTopLabelBandLayers } from "./style/labelsAndHighways";
import { buildOperationalMarkerLayers } from "./style/markerLayers";
import {
  type TrackSelectionOpacities,
  buildFallbackTrailLayers,
  buildHazardAndRouteLineLayers,
  buildVectorTrailLayers,
} from "./style/trailLayers";
import type {
  BasemapId,
  LayerVisibilityState,
  PoiItem,
  TileManifestResponse,
  UserPinItem,
} from "./types";
import { getPinCategoryColor } from "./vehicleHelpers";

export {
  AU_PLACES_ATTRIBUTION,
  DEFAULT_VECTOR_BASEMAP_ATTRIBUTION,
  DEFAULT_VECTOR_BASEMAP_URL,
  VECTOR_BASEMAP_ALL_LAYER_IDS,
  VECTOR_BASEMAP_BOTTOM_LAYER_IDS,
  VECTOR_BASEMAP_LABEL_LAYER_IDS,
  VECTOR_BASEMAP_SOURCE_ID,
} from "./style/basemapVector";
export {
  HIGHWAY_LABEL_LAYER_ID,
  HIGHWAY_LINE_LAYER_IDS,
  PLACES_REGIONS_LAYER_IDS,
  PLACE_INTERACTIVE_LAYER_IDS,
  defaultZoomForPlaceClass,
  extractPlaceClickPayload,
  formatPlaceClassLabel,
  parseFeatureBBox,
} from "./style/labelsAndHighways";
export {
  SELECTED_CLOSURE_OUTLINE_LAYER_ID,
  SELECTED_TRACK_LAYER_IDS,
  SELECTED_TRACK_STEEP_LAYER_ID,
  type TrackSelectionOpacities,
} from "./style/trailLayers";

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

/**
 * Resolve the OpenMapTiles vector basemap source:
 * - Self-hosted `.pmtiles` (or `pmtiles://`) configured via `TRAILFORGE_VECTOR_BASEMAP_URL`
 * - Self-hosted TileJSON URL configured via `TRAILFORGE_VECTOR_BASEMAP_URL`
 * - Zero-key online OpenFreeMap OpenMapTiles (`https://tiles.openfreemap.org/planet`)
 */
export function resolveVectorBasemapSource(
  tileManifest?: TileManifestResponse | null,
  origin?: string
): SourceSpecification {
  const configured = (tileManifest?.vector_basemap_url ?? "").trim();
  if (configured) {
    const isPmtiles =
      /^pmtiles:\/\//i.test(configured) ||
      /\.pmtiles(?:[?#].*)?$/i.test(configured);
    return {
      type: "vector",
      url: isPmtiles
        ? toPmtilesUrl(configured, origin)
        : resolveStaticAssetUrl(configured, origin),
      attribution: DEFAULT_VECTOR_BASEMAP_ATTRIBUTION,
    };
  }
  return {
    type: "vector",
    url: DEFAULT_VECTOR_BASEMAP_URL,
    attribution: DEFAULT_VECTOR_BASEMAP_ATTRIBUTION,
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

function buildStyleSources(options: {
  tileManifest?: TileManifestResponse | null;
  origin?: string;
  effectiveAssetsOrigin?: string;
  useVectorPmtiles: boolean;
  useTerrainPmtiles: boolean;
}): Record<string, SourceSpecification> {
  const {
    tileManifest,
    origin,
    effectiveAssetsOrigin,
    useVectorPmtiles,
    useTerrainPmtiles,
  } = options;

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
    [VECTOR_BASEMAP_SOURCE_ID]: resolveVectorBasemapSource(
      tileManifest,
      origin
    ),
    "offline-graticule": {
      type: "geojson",
      data: buildOfflineGraticuleGeoJson(),
    },
    "offline-regions": {
      type: "geojson",
      data: buildRegionalBoundsGeoJson(),
    },
    "tf-places": {
      type: "geojson",
      data: resolveStaticAssetUrl(
        "/data/au_places.geojson",
        effectiveAssetsOrigin
      ),
      attribution: AU_PLACES_ATTRIBUTION,
    },
    "tf-highways": {
      type: "geojson",
      data: resolveStaticAssetUrl(
        "/data/au_highways.geojson",
        effectiveAssetsOrigin
      ),
    },
    "tf-contours": { type: "geojson", data: EMPTY_FEATURE_COLLECTION },
    "tf-snow-line": { type: "geojson", data: EMPTY_FEATURE_COLLECTION },
    "tf-tracks": { type: "geojson", data: EMPTY_FEATURE_COLLECTION },
    "tf-steep": { type: "geojson", data: EMPTY_FEATURE_COLLECTION },
    "tf-pois": { type: "geojson", data: EMPTY_FEATURE_COLLECTION },
    "tf-closures": { type: "geojson", data: EMPTY_FEATURE_COLLECTION },
    "tf-hotspots": { type: "geojson", data: EMPTY_FEATURE_COLLECTION },
    "tf-unmapped": { type: "geojson", data: EMPTY_FEATURE_COLLECTION },
    "tf-reports": { type: "geojson", data: EMPTY_FEATURE_COLLECTION },
    "tf-recorded-track": { type: "geojson", data: EMPTY_FEATURE_COLLECTION },
    "tf-backtrack-route": { type: "geojson", data: EMPTY_FEATURE_COLLECTION },
    "tf-recorder-head": { type: "geojson", data: EMPTY_FEATURE_COLLECTION },
    "tf-planned-route": { type: "geojson", data: EMPTY_FEATURE_COLLECTION },
    "tf-planned-route-pins": { type: "geojson", data: EMPTY_FEATURE_COLLECTION },
    "tf-user-pins": { type: "geojson", data: EMPTY_FEATURE_COLLECTION },
    "tf-search-pin": { type: "geojson", data: EMPTY_FEATURE_COLLECTION },
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

  return sources;
}

function buildBasemapFoundationLayers(options: {
  showOsm: boolean;
  showOpenTopo: boolean;
  showSat: boolean;
  showVector: boolean;
  useTerrainPmtiles: boolean;
  contoursHillshade: boolean;
}): LayerSpecification[] {
  const {
    showOsm,
    showOpenTopo,
    showSat,
    showVector,
    useTerrainPmtiles,
    contoursHillshade,
  } = options;
  const vis = (enabled: boolean): "visible" | "none" =>
    enabled ? "visible" : "none";

  const foundation: LayerSpecification[] = [
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
    // Split-Stack Vector Basemap BOTTOM STACK (strictly BELOW hillshade, parks, contours, waterways, highways & 4WD tracks)
    ...buildVectorBasemapBottomLayers(vis(showVector)),
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

  if (useTerrainPmtiles) {
    foundation.push({
      id: "tf-hillshade",
      type: "hillshade",
      source: "tf-terrain-dem",
      layout: { visibility: vis(contoursHillshade) },
      paint: {
        "hillshade-exaggeration": 0.48,
        "hillshade-shadow-color": "#050A0E",
        "hillshade-highlight-color": "#B8CCE0",
        "hillshade-accent-color": "#2C4052",
        "hillshade-illumination-direction": 315,
      },
    });
  }

  return foundation;
}

/**
 * Build the MapLibre GL JS 5 StyleSpecification for TrailForge (Slice 03 + Option A + Option C).
 * - Mounts `trailforge_vector.pmtiles` & `trailforge_terrain.pmtiles` when available.
 * - Mounts self-hosted SDF glyphs, Pan-Australia places/regions, and major highways cleanly offline.
 * - Mounts split-stack OpenMapTiles vector basemap (`tf-vector-basemap`) with zero tile fetches when hidden.
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
  const showVector = initialBasemap === "vector";

  const useVectorPmtiles = Boolean(
    tileManifest?.vector_available && tileManifest.vector_url
  );
  const useTerrainPmtiles = Boolean(
    tileManifest?.terrain_available && tileManifest.terrain_url
  );
  const snowBands = tileManifest?.snowline_bands_m ?? DEFAULT_SNOWLINE_BANDS_M;

  const sources = buildStyleSources({
    tileManifest,
    origin,
    effectiveAssetsOrigin,
    useVectorPmtiles,
    useTerrainPmtiles,
  });

  const gradeColor = buildGradeColorExpression();
  const closureColor = buildClosureColorExpression();
  const reportColor = buildReportColorExpression();
  const selectedFilter = buildSelectedTrackFilter(selectedTrackId);
  const selectedClosureFilter = buildSelectedClosureFilter(selectedClosureId);
  const snowFilter = buildSnowlineFilter(layers.snowLineElevationM, snowBands);
  const trackOpacities = getTrackSelectionOpacities(Boolean(selectedTrackId));

  const trailLayers = useVectorPmtiles
    ? buildVectorTrailLayers({
        layers,
        showVector,
        gradeColor,
        selectedFilter,
        snowFilter,
        trackOpacities,
      })
    : buildFallbackTrailLayers({
        layers,
        showVector,
        gradeColor,
        selectedFilter,
        trackOpacities,
      });

  const styleLayers: LayerSpecification[] = [
    ...buildBasemapFoundationLayers({
      showOsm,
      showOpenTopo,
      showSat,
      showVector,
      useTerrainPmtiles,
      contoursHillshade: layers.contoursHillshade,
    }),
    ...trailLayers,
    ...buildHazardAndRouteLineLayers({
      layers,
      closureColor,
      selectedClosureFilter,
    }),
    ...buildOperationalMarkerLayers({
      layers,
      useVectorPmtiles,
      closureColor,
      reportColor,
    }),
    ...buildTopLabelBandLayers({
      layers,
      showVector,
      useVectorPmtiles,
    }),
  ];

  return {
    version: 8,
    glyphs: resolveGlyphsUrl(effectiveAssetsOrigin),
    sources,
    layers: styleLayers,
  };
}
