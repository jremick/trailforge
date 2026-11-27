import type {
  ExpressionSpecification,
  FilterSpecification,
  LayerSpecification,
} from "maplibre-gl";
import type { LayerVisibilityState } from "../types";
import { buildHighwayLineLayers } from "./labelsAndHighways";

export interface TrackSelectionOpacities {
  casing: number;
  line: number;
  accessWarn: number;
  steep: number;
}

export const SELECTED_TRACK_LAYER_IDS = [
  "tf-tracks-selected-glow",
  "tf-tracks-selected",
  "tf-tracks-selected-inner-casing",
  "tf-tracks-selected-core",
] as const;

export const SELECTED_TRACK_STEEP_LAYER_ID = "tf-tracks-selected-steep";
export const SELECTED_CLOSURE_OUTLINE_LAYER_ID = "tf-closures-selected-outline";

export function buildVectorTrailLayers(options: {
  layers: LayerVisibilityState;
  showVector: boolean;
  gradeColor: ExpressionSpecification;
  selectedFilter: FilterSpecification;
  snowFilter: FilterSpecification;
  trackOpacities: TrackSelectionOpacities;
}): LayerSpecification[] {
  const {
    layers,
    showVector,
    gradeColor,
    selectedFilter,
    snowFilter,
    trackOpacities,
  } = options;
  const vis = (enabled: boolean): "visible" | "none" =>
    enabled ? "visible" : "none";

  return [
    // 1. CAPAD 2025 Protected Areas
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
    },
    // 2. Configurable Alpine Snow-Line Elevation Band
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
    },
    // 3. Topographic Contours (subtle terrain context so foreground tracks pop cleanly)
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
    },
    // 4. GA / OSM Waterways (muted hydro slate-blue so creeks never compete with Medium tracks)
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
    },
    // 4b. Major Australian Highways (rendered BELOW 4WD tracks; hidden on Vector basemap)
    ...buildHighwayLineLayers(vis(layers.highways && !showVector)),
    // 5. Color-coded Canonical Graded 4WD Tracks (crisp dark-cased foreground ribbons)
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
    },
    // 6. Highlighted Steep Sections (>=14° — crisp inner dash that preserves track grade color)
    {
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
    },
    // 7. Selected Track Spotlight Stack (rendered strictly ABOVE all unselected tracks)
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
    },
  ];
}

export function buildFallbackTrailLayers(options: {
  layers: LayerVisibilityState;
  showVector: boolean;
  gradeColor: ExpressionSpecification;
  selectedFilter: FilterSpecification;
  trackOpacities: TrackSelectionOpacities;
}): LayerSpecification[] {
  const { layers, showVector, gradeColor, selectedFilter, trackOpacities } =
    options;
  const vis = (enabled: boolean): "visible" | "none" =>
    enabled ? "visible" : "none";

  return [
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
    ...buildHighwayLineLayers(vis(layers.highways && !showVector)),
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
    },
  ];
}

export function buildHazardAndRouteLineLayers(options: {
  layers: LayerVisibilityState;
  closureColor: ExpressionSpecification;
  selectedClosureFilter: FilterSpecification;
}): LayerSpecification[] {
  const { layers, closureColor, selectedClosureFilter } = options;
  const vis = (enabled: boolean): "visible" | "none" =>
    enabled ? "visible" : "none";

  return [
    // Slice 04 Live Closures, Seasonal Restrictions, Incidents & Gates hazard lines/polygons
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
    },
    // Slice 07 Rough / Slow Obstacle Hotspots line overlays
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
    },
    // Slice 06 Anonymized Unmapped Track Areas (approximate corroborated grid cells)
    {
      id: "tf-unmapped-fill",
      type: "fill",
      source: "tf-unmapped",
      layout: {
        visibility: vis(layers.unmapped),
      },
      paint: {
        "fill-color": "#A855F7",
        "fill-opacity": 0.14,
      },
    },
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
    },
    // Slice 09 In-Cab PWA Breadcrumb Recorder & One-Tap Backtrack Route lines
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
    },
    // Phase A Interactive Multi-Track Planned Route lines (bright cyan/emerald #14b8a6)
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
    },
  ];
}
