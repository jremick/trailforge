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
  LayerVisibilityState,
  PoiItem,
  TileManifestResponse,
} from "./types";

export const EMPTY_FEATURE_COLLECTION: FeatureCollection = {
  type: "FeatureCollection",
  features: [],
};

/**
 * Convert a relative or absolute `.pmtiles` URL into a `pmtiles://` protocol URL.
 * Always resolves relative paths against `origin` so MapLibre GL JS 5 blob Web Workers
 * can fetch Range requests cleanly.
 */
export function toPmtilesUrl(pathOrUrl: string, origin?: string): string {
  if (pathOrUrl.startsWith("pmtiles://")) return pathOrUrl;
  if (/^https?:\/\//i.test(pathOrUrl)) return `pmtiles://${pathOrUrl}`;
  const cleanOrigin = (origin ?? "").replace(/\/+$/, "");
  const cleanPath = pathOrUrl.startsWith("/") ? pathOrUrl : `/${pathOrUrl}`;
  return cleanOrigin
    ? `pmtiles://${cleanOrigin}${cleanPath}`
    : `pmtiles://${cleanPath}`;
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
  selectedTrackId?: string | null;
}

/**
 * Build the MapLibre GL JS 5 StyleSpecification for TrailForge (Slice 03).
 * - Mounts `trailforge_vector.pmtiles` & `trailforge_terrain.pmtiles` when available.
 * - Automatically falls back to live GeoJSON sources when PMTiles are unavailable.
 * - Requires zero external tile or glyph servers to render the VIC/NSW corridor cleanly offline.
 */
export function buildTrailForgeStyle(
  options: BuildStyleOptions
): StyleSpecification {
  const {
    initialBasemap,
    layers,
    tileManifest,
    origin,
    selectedTrackId = null,
  } = options;

  const showOsm = initialBasemap === "osm" || initialBasemap === "opentopo";
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
          "fill-opacity": 0.1,
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
          "line-width": 1.6,
          "line-dasharray": [4, 2],
          "line-opacity": 0.72,
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

    // 3. Topographic Contours (20 m minor + 100 m index + offline canvas badge labels)
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
          "line-width": 0.75,
          "line-opacity": 0.42,
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
          "line-color": "#A3B8CC",
          "line-width": 1.45,
          "line-opacity": 0.72,
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

    // 4. GA / OSM Waterways
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
          "line-color": "#38BDF8",
          "line-width": ["interpolate", ["linear"], ["zoom"], 9, 1.4, 14, 3.0],
          "line-opacity": 0.85,
        },
      },
      {
        id: "tf-waterways-intermittent",
        type: "line",
        source: "tf-vector",
        "source-layer": "waterways",
        filter: ["!=", ["get", "perennial"], true],
        layout: {
          visibility: vis(layers.parksWaterways),
          "line-cap": "round",
        },
        paint: {
          "line-color": "#0EA5E9",
          "line-width": 1.25,
          "line-dasharray": [3, 2],
          "line-opacity": 0.68,
        },
      }
    );

    // 5. Color-coded Canonical Graded 4WD Tracks
    styleLayers.push(
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
          "line-width": ["interpolate", ["linear"], ["zoom"], 9, 7.5, 14, 11.5],
          "line-opacity": 0.72,
        },
      },
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
          "line-color": "#090F14",
          "line-width": ["interpolate", ["linear"], ["zoom"], 9, 4.4, 14, 7.2],
          "line-opacity": 0.88,
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
          "line-width": ["interpolate", ["linear"], ["zoom"], 9, 2.4, 14, 4.4],
          "line-opacity": 0.96,
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
          "line-width": 2.4,
          "line-dasharray": [2, 2],
          "line-opacity": 0.92,
        },
      }
    );

    // 6. Highlighted Steep Sections (>=14°)
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
        "line-width": ["interpolate", ["linear"], ["zoom"], 10, 4.0, 14, 6.4],
        "line-dasharray": [1.5, 2],
        "line-opacity": 0.96,
      },
    });

    // 7. Outdoor POIs with high-contrast backing + adapted SVG symbols
    styleLayers.push(
      {
        id: "tf-pois-layer",
        type: "circle",
        source: "tf-vector",
        "source-layer": "pois",
        layout: { visibility: vis(layers.pois) },
        paint: {
          "circle-radius": 12,
          "circle-color": "rgba(241, 245, 249, 0.94)",
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
    // GeoJSON fallback layers (used automatically when PMTiles are unavailable)
    styleLayers.push(
      {
        id: "tf-contours-hillshade",
        type: "line",
        source: "tf-contours",
        layout: { visibility: vis(layers.contoursHillshade) },
        paint: {
          "line-color": "#64748B",
          "line-width": 0.85,
          "line-opacity": 0.55,
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
          "line-width": 9.5,
          "line-opacity": 0.72,
        },
      },
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
          "line-color": "#0B1117",
          "line-width": 5.8,
          "line-opacity": 0.86,
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
          "line-width": 3.4,
          "line-opacity": 0.96,
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
          "line-width": 4.8,
          "line-dasharray": [1.5, 2],
          "line-opacity": 0.95,
        },
      },
      {
        id: "tf-pois-layer",
        type: "circle",
        source: "tf-pois",
        layout: { visibility: vis(layers.pois) },
        paint: {
          "circle-radius": 11.5,
          "circle-color": "rgba(241, 245, 249, 0.94)",
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
    sources,
    layers: styleLayers,
  };
}
