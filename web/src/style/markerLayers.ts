import type { ExpressionSpecification, LayerSpecification } from "maplibre-gl";
import type { LayerVisibilityState } from "../types";

/**
 * Point, Icon & Marker Layers — strictly ABOVE all track & overlay lines
 * and strictly BELOW the dedicated Top-of-Stack Text Label Band.
 */
export function buildOperationalMarkerLayers(options: {
  layers: LayerVisibilityState;
  useVectorPmtiles: boolean;
  closureColor: ExpressionSpecification;
  reportColor: ExpressionSpecification;
}): LayerSpecification[] {
  const { layers, useVectorPmtiles, closureColor, reportColor } = options;
  const vis = (enabled: boolean): "visible" | "none" =>
    enabled ? "visible" : "none";

  const poiLayers: LayerSpecification[] = useVectorPmtiles
    ? [
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
        },
      ]
    : [
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
        },
      ];

  return [
    // Subtle city / regional hub / town dots (above highway & track lines, below operational pins)
    {
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
    },
    // Obstacle hotspot point markers
    {
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
    },
    // Outdoor POIs with high-contrast backing + adapted SVG symbols (on top of all track/closure lines)
    ...poiLayers,
    // Closure & incident point markers
    {
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
    },
    // Slice 08 Community Condition Reports & Distinct Locked Gate / Private Property Warnings
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
    },
    // Recorder live GPS position marker
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
    },
    // Planned route start/leg/waypoint/finish badges
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
    },
    // Slice 05 Personal User Pins (always visible when signed in)
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
    },
  ];
}
