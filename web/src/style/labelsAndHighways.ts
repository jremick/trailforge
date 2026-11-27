import type { ExpressionSpecification, LayerSpecification } from "maplibre-gl";
import type {
  BBoxTuple,
  LayerVisibilityState,
  MapPlaceClickPayload,
} from "../types";
import { buildVectorBasemapLabelLayers } from "./basemapVector";

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

export function buildHighwayLineLayers(
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
 * Dedicated Top-of-Stack Text Label Band — ABOVE hillshade, parks,
 * contours, waterways, highways, 4WD tracks & markers
 * (Ordered lowest -> highest collision priority: MapLibre places later
 * symbol layers first during GPU collision avoidance.)
 */
export function buildTopLabelBandLayers(options: {
  layers: LayerVisibilityState;
  showVector: boolean;
  useVectorPmtiles: boolean;
}): LayerSpecification[] {
  const { layers, showVector, useVectorPmtiles } = options;
  const vis = (enabled: boolean): "visible" | "none" =>
    enabled ? "visible" : "none";

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

  const out: LayerSpecification[] = [
    // Split-Stack Vector Basemap TOP LABEL STACK (floated strictly ABOVE all track & terrain lines; self-hosted Noto Sans SDF glyphs)
    ...buildVectorBasemapLabelLayers({
      baseVis: vis(showVector),
      roadNameVis: vis(showVector && layers.highways),
      suburbVis: vis(showVector && layers.placesRegions),
    }),
    // Highway route shield + name labels (hidden when Vector basemap's tf-vbase-road-name is active)
    {
      id: HIGHWAY_LABEL_LAYER_ID,
      type: "symbol",
      source: "tf-highways",
      minzoom: 6.5,
      layout: {
        visibility: vis(layers.highways && !showVector),
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
    },
  ];

  // CAPAD National Park & Reserve labels from Vector PMTiles (z7.5–z14.5, tiered by area_ha)
  if (useVectorPmtiles) {
    out.push({
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

  out.push(
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
    },
    // Coordinate / MGRS / POI search target pin (always on top)
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

  return out;
}
