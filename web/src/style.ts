import type {
  Feature,
  FeatureCollection,
  LineString,
  Point,
  Polygon,
} from "geojson";
import type { StyleSpecification } from "maplibre-gl";
import { REGION_PRESETS } from "./presets";
import type { BasemapId, LayerVisibilityState } from "./types";

export const EMPTY_FEATURE_COLLECTION: FeatureCollection = {
  type: "FeatureCollection",
  features: [],
};

/**
 * Generate a lightweight offline 1° topographic graticule covering VIC & NSW/ACT
 * plus a finer 0.25° grid over the Australian Alps corridor so the map remains
 * visually legible and spatially oriented when external tiles are unreachable.
 */
export function buildOfflineGraticuleGeoJson(): FeatureCollection<LineString> {
  const features: Feature<LineString>[] = [];

  // 1-degree meridians across VIC/NSW (140°E .. 154°E)
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

  // 1-degree parallels across VIC/NSW (-40°S .. -28°S)
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

  // Finer 0.25-degree reference grid over Australian Alps corridor
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
}

/**
 * Build the MapLibre GL JS 5 StyleSpecification for TrailForge.
 * Designed to degrade gracefully offline (dark topo background + local GeoJSON
 * graticule/corridor framing + Slice 03 vector/terrain placeholder layers).
 */
export function buildTrailForgeStyle(
  options: BuildStyleOptions
): StyleSpecification {
  const { initialBasemap, layers } = options;

  const showOsm = initialBasemap === "osm" || initialBasemap === "opentopo";
  const showOpenTopo = initialBasemap === "opentopo";
  const showSat = initialBasemap === "satellite";

  const vis = (enabled: boolean): "visible" | "none" =>
    enabled ? "visible" : "none";

  return {
    version: 8,
    sources: {
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
      // Slice 03 vector / PMTiles overlay placeholders (swapped to pmtiles:// in Slice 03)
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
    },
    layers: [
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
          "line-width": [
            "case",
            ["==", ["get", "kind"], "major"],
            1.2,
            0.7,
          ],
          "line-opacity": 0.9,
        },
      },
      {
        id: "basemap-osm-underlay",
        type: "raster",
        source: "osm-standard",
        layout: { visibility: vis(showOsm) },
        paint: {
          "raster-opacity": 0.88,
          "raster-brightness-max": 0.85,
          "raster-saturation": -0.12,
        },
      },
      {
        id: "basemap-opentopo",
        type: "raster",
        source: "opentopomap",
        layout: { visibility: vis(showOpenTopo) },
        paint: {
          "raster-opacity": 0.9,
          "raster-brightness-max": 0.88,
          "raster-contrast": 0.08,
        },
      },
      {
        id: "basemap-satellite",
        type: "raster",
        source: "esri-imagery",
        layout: { visibility: vis(showSat) },
        paint: {
          "raster-opacity": 0.95,
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
            0.09,
            0.03,
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
          "line-width": [
            "case",
            ["==", ["get", "is_corridor"], true],
            2.0,
            1.3,
          ],
          "line-dasharray": [5, 3],
          "line-opacity": 0.72,
        },
      },
      // Slice 03 terrain / contours / hillshade layer slot
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
      // Slice 03 configurable alpine snow-line band (>1,000 m default)
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
      // Slice 03 color-coded graded 4WD tracks
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
          "line-width": 5.2,
          "line-opacity": 0.82,
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
          "line-color": [
            "match",
            ["get", "difficulty"],
            "easy",
            "#22C55E",
            "medium",
            "#38BDF8",
            "hard",
            "#F59E0B",
            "extreme",
            "#EF4444",
            "#94A3B8",
          ],
          "line-width": 3.0,
          "line-opacity": 0.96,
        },
      },
      // Slice 03 steep-grade (≥14°) highlight ribbon
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
          "line-width": 4.2,
          "line-dasharray": [2, 2],
          "line-opacity": 0.95,
        },
      },
      // Slice 03 outdoor POIs (camps, huts, trigs, water, fuel)
      {
        id: "tf-pois-layer",
        type: "circle",
        source: "tf-pois",
        layout: { visibility: vis(layers.pois) },
        paint: {
          "circle-radius": 5.5,
          "circle-color": "#10B981",
          "circle-stroke-color": "#0B1117",
          "circle-stroke-width": 1.8,
        },
      },
      // Coordinate / MGRS search target pin
      {
        id: "tf-search-pin-halo",
        type: "circle",
        source: "tf-search-pin",
        paint: {
          "circle-radius": 13,
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
          "circle-radius": 4.5,
          "circle-color": "#FBBF24",
          "circle-stroke-color": "#0B1117",
          "circle-stroke-width": 1.6,
        },
      },
    ],
  };
}
