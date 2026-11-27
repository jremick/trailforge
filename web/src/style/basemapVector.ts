import type { ExpressionSpecification, LayerSpecification } from "maplibre-gl";

export const VECTOR_BASEMAP_SOURCE_ID = "tf-vector-basemap";
export const DEFAULT_VECTOR_BASEMAP_URL =
  "https://tiles.openfreemap.org/planet";
export const DEFAULT_VECTOR_BASEMAP_ATTRIBUTION =
  "© OpenFreeMap © OpenMapTiles © OpenStreetMap contributors";
export const AU_PLACES_ATTRIBUTION =
  "© GeoNames (CC BY 4.0) · Natural Earth · OpenStreetMap contributors";

export const VECTOR_BASEMAP_BOTTOM_LAYER_IDS = [
  "tf-vbase-land",
  "tf-vbase-landcover",
  "tf-vbase-park",
  "tf-vbase-landuse",
  "tf-vbase-water",
  "tf-vbase-waterway",
  "tf-vbase-boundary",
  "tf-vbase-railway",
  "tf-vbase-road-minor",
  "tf-vbase-road-secondary-casing",
  "tf-vbase-road-secondary",
  "tf-vbase-road-primary-casing",
  "tf-vbase-road-primary",
  "tf-vbase-road-trunk-casing",
  "tf-vbase-road-trunk",
  "tf-vbase-road-motorway-casing",
  "tf-vbase-road-motorway",
] as const;

export const VECTOR_BASEMAP_LABEL_LAYER_IDS = [
  "tf-vbase-water-name",
  "tf-vbase-road-name",
  "tf-vbase-mountain-peak",
  "tf-vbase-place-suburb",
] as const;

export const VECTOR_BASEMAP_ALL_LAYER_IDS = [
  ...VECTOR_BASEMAP_BOTTOM_LAYER_IDS,
  ...VECTOR_BASEMAP_LABEL_LAYER_IDS,
] as const;

export function buildVectorBasemapBottomLayers(
  visibility: "visible" | "none"
): LayerSpecification[] {
  const roadClassExpr: ExpressionSpecification = [
    "coalesce",
    ["get", "class"],
    "",
  ];
  const adminLevelExpr: ExpressionSpecification = [
    "to-number",
    ["coalesce", ["get", "admin_level"], 99],
    99,
  ];

  return [
    {
      id: "tf-vbase-land",
      type: "background",
      layout: { visibility },
      paint: {
        "background-color": "#111921",
      },
    },
    {
      id: "tf-vbase-landcover",
      type: "fill",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "landcover",
      layout: { visibility },
      paint: {
        "fill-color": [
          "match",
          ["coalesce", ["get", "class"], ""],
          ["wood", "forest"],
          "#162620",
          ["grass", "meadow", "farmland"],
          "#1A2721",
          "wetland",
          "#142628",
          "sand",
          "#232620",
          ["rock", "ice"],
          "#1E2832",
          "#152220",
        ],
        "fill-opacity": [
          "interpolate",
          ["linear"],
          ["zoom"],
          4,
          0.45,
          9,
          0.78,
          14,
          0.85,
        ],
      },
    },
    {
      id: "tf-vbase-park",
      type: "fill",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "park",
      layout: { visibility },
      paint: {
        "fill-color": "#132922",
        "fill-opacity": [
          "interpolate",
          ["linear"],
          ["zoom"],
          5,
          0.35,
          9,
          0.55,
          13,
          0.65,
        ],
      },
    },
    {
      id: "tf-vbase-landuse",
      type: "fill",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "landuse",
      minzoom: 6,
      filter: [
        "in",
        ["coalesce", ["get", "class"], ""],
        [
          "literal",
          [
            "residential",
            "suburb",
            "neighbourhood",
            "commercial",
            "industrial",
            "retail",
            "hospital",
            "school",
            "railway",
            "stadium",
            "military",
          ],
        ],
      ],
      layout: { visibility },
      paint: {
        "fill-color": [
          "match",
          ["coalesce", ["get", "class"], ""],
          ["commercial", "industrial", "retail"],
          "#1F2937",
          "#1B242E",
        ],
        "fill-opacity": [
          "interpolate",
          ["linear"],
          ["zoom"],
          6,
          0.35,
          10,
          0.72,
          14,
          0.84,
        ],
      },
    },
    {
      id: "tf-vbase-water",
      type: "fill",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "water",
      layout: { visibility },
      paint: {
        "fill-color": "#0C2336",
        "fill-opacity": [
          "case",
          ["==", ["coalesce", ["get", "intermittent"], 0], 1],
          0.55,
          1.0,
        ],
      },
    },
    {
      id: "tf-vbase-waterway",
      type: "line",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "waterway",
      layout: {
        visibility,
        "line-cap": "round",
        "line-join": "round",
      },
      paint: {
        "line-color": "#1D4E6E",
        "line-width": ["interpolate", ["linear"], ["zoom"], 7, 0.6, 11, 1.2, 15, 2.2],
        "line-opacity": [
          "case",
          ["==", ["coalesce", ["get", "intermittent"], 0], 1],
          0.38,
          0.72,
        ],
      },
    },
    {
      id: "tf-vbase-boundary",
      type: "line",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "boundary",
      filter: [
        "all",
        ["<=", adminLevelExpr, 4],
        ["!=", ["coalesce", ["get", "maritime"], 0], 1],
      ],
      layout: {
        visibility,
        "line-join": "round",
      },
      paint: {
        "line-color": ["case", ["<=", adminLevelExpr, 2], "#64748B", "#475569"],
        "line-width": ["interpolate", ["linear"], ["zoom"], 3, 0.8, 6, 1.2, 10, 1.8],
        "line-dasharray": [5, 3],
        "line-opacity": 0.78,
      },
    },
    {
      id: "tf-vbase-railway",
      type: "line",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "transportation",
      minzoom: 9,
      filter: ["in", roadClassExpr, ["literal", ["rail", "transit"]]],
      layout: {
        visibility,
      },
      paint: {
        "line-color": "#475569",
        "line-width": ["interpolate", ["linear"], ["zoom"], 9, 0.7, 13, 1.3],
        "line-dasharray": [4, 4],
        "line-opacity": 0.58,
      },
    },
    {
      id: "tf-vbase-road-minor",
      type: "line",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "transportation",
      minzoom: 11.5,
      filter: ["in", roadClassExpr, ["literal", ["minor", "service"]]],
      layout: {
        visibility,
        "line-cap": "round",
        "line-join": "round",
      },
      paint: {
        "line-color": "#2B3847",
        "line-width": ["interpolate", ["linear"], ["zoom"], 11.5, 0.7, 14, 2.2, 16, 4.5],
        "line-opacity": ["interpolate", ["linear"], ["zoom"], 11.5, 0.55, 13.5, 0.85],
      },
    },
    {
      id: "tf-vbase-road-secondary-casing",
      type: "line",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "transportation",
      minzoom: 9.5,
      filter: ["in", roadClassExpr, ["literal", ["secondary", "tertiary"]]],
      layout: {
        visibility,
        "line-cap": "round",
        "line-join": "round",
      },
      paint: {
        "line-color": "#0B1117",
        "line-width": ["interpolate", ["linear"], ["zoom"], 9.5, 1.8, 12, 3.8, 15, 7.2],
        "line-opacity": 0.85,
      },
    },
    {
      id: "tf-vbase-road-secondary",
      type: "line",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "transportation",
      minzoom: 9.5,
      filter: ["in", roadClassExpr, ["literal", ["secondary", "tertiary"]]],
      layout: {
        visibility,
        "line-cap": "round",
        "line-join": "round",
      },
      paint: {
        "line-color": ["match", roadClassExpr, "secondary", "#47596C", "#384859"],
        "line-width": ["interpolate", ["linear"], ["zoom"], 9.5, 0.9, 12, 2.2, 15, 5.0],
        "line-opacity": 0.9,
      },
    },
    {
      id: "tf-vbase-road-primary-casing",
      type: "line",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "transportation",
      minzoom: 7.5,
      filter: ["==", roadClassExpr, "primary"],
      layout: {
        visibility,
        "line-cap": "round",
        "line-join": "round",
      },
      paint: {
        "line-color": "#0B1117",
        "line-width": ["interpolate", ["linear"], ["zoom"], 7.5, 2.0, 11, 4.4, 15, 8.6],
        "line-opacity": 0.88,
      },
    },
    {
      id: "tf-vbase-road-primary",
      type: "line",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "transportation",
      minzoom: 7.5,
      filter: ["==", roadClassExpr, "primary"],
      layout: {
        visibility,
        "line-cap": "round",
        "line-join": "round",
      },
      paint: {
        "line-color": "#64748B",
        "line-width": ["interpolate", ["linear"], ["zoom"], 7.5, 1.0, 11, 2.7, 15, 6.2],
        "line-opacity": 0.92,
      },
    },
    {
      id: "tf-vbase-road-trunk-casing",
      type: "line",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "transportation",
      minzoom: 5,
      filter: ["==", roadClassExpr, "trunk"],
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
          5,
          1.8,
          8,
          3.6,
          12,
          5.6,
          15,
          9.6,
        ],
        "line-opacity": 0.9,
      },
    },
    {
      id: "tf-vbase-road-trunk",
      type: "line",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "transportation",
      minzoom: 5,
      filter: ["==", roadClassExpr, "trunk"],
      layout: {
        visibility,
        "line-cap": "round",
        "line-join": "round",
      },
      paint: {
        "line-color": "#B48A3E",
        "line-width": [
          "interpolate",
          ["linear"],
          ["zoom"],
          5,
          0.9,
          8,
          2.0,
          12,
          3.5,
          15,
          6.8,
        ],
        "line-opacity": 0.92,
      },
    },
    {
      id: "tf-vbase-road-motorway-casing",
      type: "line",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "transportation",
      minzoom: 4.5,
      filter: ["==", roadClassExpr, "motorway"],
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
          2.2,
          8,
          4.4,
          12,
          6.8,
          15,
          11.2,
        ],
        "line-opacity": 0.92,
      },
    },
    {
      id: "tf-vbase-road-motorway",
      type: "line",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "transportation",
      minzoom: 4.5,
      filter: ["==", roadClassExpr, "motorway"],
      layout: {
        visibility,
        "line-cap": "round",
        "line-join": "round",
      },
      paint: {
        "line-color": "#C88A32",
        "line-width": [
          "interpolate",
          ["linear"],
          ["zoom"],
          4.5,
          1.1,
          8,
          2.6,
          12,
          4.4,
          15,
          8.2,
        ],
        "line-opacity": 0.95,
      },
    },
  ];
}

export function buildVectorBasemapLabelLayers(options: {
  baseVis: "visible" | "none";
  roadNameVis: "visible" | "none";
  suburbVis: "visible" | "none";
}): LayerSpecification[] {
  const { baseVis, roadNameVis, suburbVis } = options;
  const latinNameExpr: ExpressionSpecification = [
    "coalesce",
    ["get", "name:latin"],
    ["get", "name"],
    "",
  ];
  const roadClassExpr: ExpressionSpecification = [
    "coalesce",
    ["get", "class"],
    "",
  ];
  const vbaseRoadLabelExpr: ExpressionSpecification = [
    "case",
    [
      "all",
      ["!=", ["coalesce", ["get", "ref"], ""], ""],
      ["!=", latinNameExpr, ""],
    ],
    ["concat", ["get", "ref"], " · ", latinNameExpr],
    ["!=", latinNameExpr, ""],
    latinNameExpr,
    ["coalesce", ["get", "ref"], ""],
  ];

  return [
    {
      id: "tf-vbase-water-name",
      type: "symbol",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "water_name",
      minzoom: 5,
      layout: {
        visibility: baseVis,
        "text-field": latinNameExpr,
        "text-font": ["Noto Sans Italic"],
        "text-size": ["interpolate", ["linear"], ["zoom"], 5, 10, 9, 11.5, 13, 12.5],
        "text-letter-spacing": 0.08,
        "text-max-width": 8,
        "text-padding": 6,
        "text-allow-overlap": false,
        "symbol-sort-key": 18,
      },
      paint: {
        "text-color": "#7DD3FC",
        "text-halo-color": "rgba(9, 14, 20, 0.92)",
        "text-halo-width": 1.6,
        "text-halo-blur": 0.5,
        "text-opacity": 0.88,
      },
    },
    {
      id: "tf-vbase-road-name",
      type: "symbol",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "transportation_name",
      minzoom: 9,
      filter: [
        "in",
        roadClassExpr,
        [
          "literal",
          [
            "motorway",
            "trunk",
            "primary",
            "secondary",
            "tertiary",
            "minor",
            "service",
          ],
        ],
      ],
      layout: {
        visibility: roadNameVis,
        "symbol-placement": "line",
        "symbol-spacing": 320,
        "text-field": [
          "step",
          ["zoom"],
          [
            "case",
            ["in", roadClassExpr, ["literal", ["motorway", "trunk"]]],
            vbaseRoadLabelExpr,
            "",
          ],
          11,
          [
            "case",
            [
              "in",
              roadClassExpr,
              ["literal", ["motorway", "trunk", "primary", "secondary"]],
            ],
            vbaseRoadLabelExpr,
            "",
          ],
          13,
          vbaseRoadLabelExpr,
        ],
        "text-font": ["Noto Sans Medium"],
        "text-size": ["interpolate", ["linear"], ["zoom"], 9, 10, 12, 11, 15, 12.2],
        "text-max-angle": 30,
        "text-padding": 5,
        "text-allow-overlap": false,
        "symbol-sort-key": [
          "match",
          roadClassExpr,
          "motorway",
          11,
          "trunk",
          12,
          "primary",
          13,
          "secondary",
          14,
          16,
        ],
      },
      paint: {
        "text-color": [
          "match",
          roadClassExpr,
          "motorway",
          "#FDE68A",
          "trunk",
          "#FEF08A",
          "#E2E8F0",
        ],
        "text-halo-color": "rgba(9, 14, 20, 0.92)",
        "text-halo-width": 1.8,
        "text-halo-blur": 0.5,
      },
    },
    {
      id: "tf-vbase-mountain-peak",
      type: "symbol",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "mountain_peak",
      minzoom: 10.5,
      filter: ["!=", latinNameExpr, ""],
      layout: {
        visibility: baseVis,
        "text-field": [
          "case",
          [">", ["to-number", ["get", "ele"], 0], 0],
          [
            "concat",
            latinNameExpr,
            "\n",
            ["to-string", ["round", ["to-number", ["get", "ele"], 0]]],
            " m",
          ],
          latinNameExpr,
        ],
        "text-font": ["Noto Sans Medium"],
        "text-size": ["interpolate", ["linear"], ["zoom"], 10.5, 9.5, 13, 10.8, 15, 11.8],
        "text-max-width": 8,
        "text-padding": 5,
        "text-allow-overlap": false,
        "symbol-sort-key": 16,
      },
      paint: {
        "text-color": "#CBD5E1",
        "text-halo-color": "rgba(9, 14, 20, 0.92)",
        "text-halo-width": 1.6,
        "text-halo-blur": 0.5,
      },
    },
    {
      id: "tf-vbase-place-suburb",
      type: "symbol",
      source: VECTOR_BASEMAP_SOURCE_ID,
      "source-layer": "place",
      minzoom: 11.5,
      filter: [
        "in",
        ["coalesce", ["get", "class"], ""],
        ["literal", ["suburb", "neighbourhood", "quarter"]],
      ],
      layout: {
        visibility: suburbVis,
        "text-field": latinNameExpr,
        "text-font": ["Noto Sans Regular"],
        "text-size": ["interpolate", ["linear"], ["zoom"], 11.5, 9.5, 13.5, 11, 15, 12],
        "text-letter-spacing": 0.04,
        "text-max-width": 8,
        "text-padding": 5,
        "text-allow-overlap": false,
        "symbol-sort-key": 17,
      },
      paint: {
        "text-color": "#94A3B8",
        "text-halo-color": "rgba(9, 14, 20, 0.92)",
        "text-halo-width": 1.6,
        "text-halo-blur": 0.5,
      },
    },
  ];
}
