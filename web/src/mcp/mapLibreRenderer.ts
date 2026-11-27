import type { Feature, FeatureCollection, LineString, Point } from "geojson";
import * as maplibregl from "maplibre-gl";
import maplibreWorkerRaw from "maplibre-gl/dist/maplibre-gl-worker.mjs?raw";
import type {
  GeoJSONSource,
  LngLatBoundsLike,
  Map as MapLibreMap,
  StyleSpecification,
} from "maplibre-gl";
import { Protocol } from "pmtiles";
import { getGradeColor } from "../presets";
import {
  COMPARE_FALLBACK_COLORS,
  sanitizeExternalHttpUrl,
  sanitizeHexColor,
  type TrailForgeViewPayload,
} from "./types";

let pmtilesRegistered = false;
let mcpWorkerBlobUrl: string | null = null;

function ensurePmtilesProtocol(): void {
  if (pmtilesRegistered) return;
  try {
    if (
      !mcpWorkerBlobUrl &&
      maplibreWorkerRaw &&
      typeof URL !== "undefined" &&
      typeof URL.createObjectURL === "function" &&
      typeof Blob !== "undefined" &&
      typeof maplibregl.setWorkerUrl === "function"
    ) {
      mcpWorkerBlobUrl = URL.createObjectURL(
        new Blob([maplibreWorkerRaw], { type: "text/javascript" })
      );
      maplibregl.setWorkerUrl(mcpWorkerBlobUrl);
    }
    const protocol = new Protocol();
    maplibregl.addProtocol("pmtiles", protocol.tile);
    pmtilesRegistered = true;
  } catch (err) {
    console.warn("Unable to register pmtiles protocol for MCP MapLibre:", err);
  }
}

export function detectWebGLSupport(): boolean {
  if (typeof document === "undefined") return false;
  try {
    const canvas = document.createElement("canvas");
    const gl =
      canvas.getContext("webgl2") ??
      canvas.getContext("webgl") ??
      canvas.getContext("experimental-webgl");
    return Boolean(gl);
  } catch (err) {
    console.debug("WebGL support check failed in MCP widget:", err);
    return false;
  }
}

export function hasUsableTileConfig(payload: TrailForgeViewPayload): boolean {
  const cfg = payload.tile_config;
  if (!cfg) return false;
  return Boolean(
    sanitizeExternalHttpUrl(cfg.vector_pmtiles_url) ||
      sanitizeExternalHttpUrl(cfg.basemap_raster_url)
  );
}

export interface MapLibreOverlayController {
  setCursorPosition: (lon: number, lat: number, color: string) => void;
  clearCursor: () => void;
  fitToBbox: (bbox: [number, number, number, number]) => void;
  destroy: () => void;
}

export interface MountMapLibreOptions {
  container: HTMLElement;
  payload: TrailForgeViewPayload;
  theme: "light" | "dark";
  onFallback: (reason: string) => void;
  onTrackClick?: (trackId: string) => void;
  onHoverCoord?: (lon: number, lat: number) => void;
}

function buildOverlayGeoJson(payload: TrailForgeViewPayload): {
  lines: FeatureCollection<LineString>;
  markers: FeatureCollection<Point>;
  bounds: [number, number, number, number] | null;
} {
  const lineFeatures: Array<Feature<LineString>> = [];
  const markerFeatures: Array<Feature<Point>> = [];

  let minLon = Infinity;
  let minLat = Infinity;
  let maxLon = -Infinity;
  let maxLat = -Infinity;
  const include = (lon: number, lat: number): void => {
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) return;
    minLon = Math.min(minLon, lon);
    minLat = Math.min(minLat, lat);
    maxLon = Math.max(maxLon, lon);
    maxLat = Math.max(maxLat, lat);
  };

  if (payload.view_mode === "track" && payload.track) {
    for (const seg of payload.track.segments ?? []) {
      if (seg.coords.length >= 2) {
        seg.coords.forEach(([lon, lat]) => include(lon, lat));
        lineFeatures.push({
          type: "Feature",
          properties: {
            color: getGradeColor(seg.grade),
            track_id: payload.track.summary.track_id,
            width: seg.is_steep ? 5 : 4,
          },
          geometry: { type: "LineString", coordinates: seg.coords },
        });
      }
    }
    const firstCoords = payload.track.segments?.[0]?.coords?.[0];
    const lastSeg = payload.track.segments?.[payload.track.segments.length - 1];
    const lastCoords = lastSeg?.coords?.[lastSeg.coords.length - 1];
    if (firstCoords) {
      markerFeatures.push({
        type: "Feature",
        properties: { label: "S", color: "#22C55E" },
        geometry: { type: "Point", coordinates: firstCoords },
      });
    }
    if (lastCoords) {
      markerFeatures.push({
        type: "Feature",
        properties: { label: "F", color: "#E2E8F0" },
        geometry: { type: "Point", coordinates: lastCoords },
      });
    }
    for (const poi of payload.track.nearby_pois ?? []) {
      include(poi.lon, poi.lat);
      markerFeatures.push({
        type: "Feature",
        properties: { label: poi.name, color: "#38BDF8" },
        geometry: { type: "Point", coordinates: [poi.lon, poi.lat] },
      });
    }
  } else if (payload.view_mode === "route" && payload.route) {
    for (const leg of payload.route.legs ?? []) {
      for (const seg of leg.segments ?? []) {
        if (seg.coords.length >= 2) {
          seg.coords.forEach(([lon, lat]) => include(lon, lat));
          lineFeatures.push({
            type: "Feature",
            properties: {
              color: getGradeColor(seg.grade),
              track_id: leg.track_id,
              width: seg.is_steep ? 5 : 4,
            },
            geometry: { type: "LineString", coordinates: seg.coords },
          });
        }
      }
    }
    for (const wp of payload.route.waypoints ?? []) {
      include(wp.lon, wp.lat);
      markerFeatures.push({
        type: "Feature",
        properties: { label: wp.name, color: "#22C55E" },
        geometry: { type: "Point", coordinates: [wp.lon, wp.lat] },
      });
    }
  } else if (payload.view_mode === "compare" && payload.compare) {
    (payload.compare.tracks ?? []).forEach((entry, idx) => {
      const color = sanitizeHexColor(
        entry.color_hex,
        COMPARE_FALLBACK_COLORS[idx % COMPARE_FALLBACK_COLORS.length]!
      );
      for (const seg of entry.segments ?? []) {
        if (seg.coords.length >= 2) {
          seg.coords.forEach(([lon, lat]) => include(lon, lat));
          lineFeatures.push({
            type: "Feature",
            properties: {
              color,
              track_id: entry.summary.track_id,
              width: 4,
            },
            geometry: { type: "LineString", coordinates: seg.coords },
          });
        }
      }
    });
  } else if (payload.explore) {
    for (const t of payload.explore.tracks ?? []) {
      for (const seg of t.segments ?? []) {
        if (seg.coords.length >= 2) {
          seg.coords.forEach(([lon, lat]) => include(lon, lat));
          lineFeatures.push({
            type: "Feature",
            properties: {
              color: getGradeColor(seg.grade || t.grade),
              track_id: t.track_id,
              width: 4,
            },
            geometry: { type: "LineString", coordinates: seg.coords },
          });
        }
      }
    }
    for (const poi of payload.explore.pois ?? []) {
      include(poi.lon, poi.lat);
      markerFeatures.push({
        type: "Feature",
        properties: { label: poi.name, color: "#38BDF8" },
        geometry: { type: "Point", coordinates: [poi.lon, poi.lat] },
      });
    }
  }

  const computedBounds: [number, number, number, number] | null =
    Number.isFinite(minLon) && Number.isFinite(maxLon)
      ? [minLon, minLat, maxLon, maxLat]
      : payload.bbox;

  return {
    lines: { type: "FeatureCollection", features: lineFeatures },
    markers: { type: "FeatureCollection", features: markerFeatures },
    bounds: computedBounds,
  };
}

function buildInlineStyle(
  payload: TrailForgeViewPayload,
  theme: "light" | "dark",
  overlays: ReturnType<typeof buildOverlayGeoJson>
): StyleSpecification {
  const isLight = theme === "light";
  const rasterUrl = sanitizeExternalHttpUrl(
    payload.tile_config?.basemap_raster_url
  );
  const vectorUrl = sanitizeExternalHttpUrl(
    payload.tile_config?.vector_pmtiles_url
  );

  const sources: StyleSpecification["sources"] = {
    "tfm-lines": { type: "geojson", data: overlays.lines },
    "tfm-markers": { type: "geojson", data: overlays.markers },
    "tfm-cursor": {
      type: "geojson",
      data: { type: "FeatureCollection", features: [] },
    },
  };

  const layers: StyleSpecification["layers"] = [
    {
      id: "tfm-bg",
      type: "background",
      paint: { "background-color": isLight ? "#F1F5F9" : "#0B1218" },
    },
  ];

  if (rasterUrl) {
    sources["tfm-raster"] = {
      type: "raster",
      tiles: [rasterUrl],
      tileSize: 256,
    };
    layers.push({
      id: "tfm-raster-layer",
      type: "raster",
      source: "tfm-raster",
      paint: { "raster-opacity": isLight ? 0.92 : 0.72 },
    });
  }

  if (vectorUrl) {
    ensurePmtilesProtocol();
    sources["tfm-vector"] = {
      type: "vector",
      url: `pmtiles://${vectorUrl}`,
    };
    layers.push({
      id: "tfm-vector-tracks",
      type: "line",
      source: "tfm-vector",
      "source-layer": "track_segments",
      paint: {
        "line-color": isLight ? "#64748B" : "#334155",
        "line-width": 1.6,
        "line-opacity": 0.6,
      },
    });
  }

  layers.push(
    {
      id: "tfm-lines-casing",
      type: "line",
      source: "tfm-lines",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": isLight ? "#FFFFFF" : "#060A0F",
        "line-width": ["+", ["coalesce", ["get", "width"], 4], 3.5],
        "line-opacity": 0.9,
      },
    },
    {
      id: "tfm-lines-main",
      type: "line",
      source: "tfm-lines",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": ["coalesce", ["get", "color"], "#38BDF8"],
        "line-width": ["coalesce", ["get", "width"], 4],
      },
    },
    {
      id: "tfm-markers-dots",
      type: "circle",
      source: "tfm-markers",
      paint: {
        "circle-radius": 6,
        "circle-color": ["coalesce", ["get", "color"], "#38BDF8"],
        "circle-stroke-color": isLight ? "#FFFFFF" : "#070B10",
        "circle-stroke-width": 2,
      },
    },
    {
      id: "tfm-cursor-halo",
      type: "circle",
      source: "tfm-cursor",
      paint: {
        "circle-radius": 11,
        "circle-color": "rgba(253, 224, 71, 0.35)",
      },
    },
    {
      id: "tfm-cursor-dot",
      type: "circle",
      source: "tfm-cursor",
      paint: {
        "circle-radius": 5.5,
        "circle-color": ["coalesce", ["get", "color"], "#FDE047"],
        "circle-stroke-color": "#070B10",
        "circle-stroke-width": 2,
      },
    }
  );

  return {
    version: 8,
    sources,
    layers,
  };
}

/**
 * Attempt to mount MapLibre GL JS 5 inside the MCP App map stage.
 * Automatically triggers `onFallback` (destroying cleanly without leaking WebGL contexts)
 * whenever WebGL is unavailable, `tile_config` is empty, or tile loading fails under CSP.
 */
export function mountMapLibreMap(
  options: MountMapLibreOptions
): MapLibreOverlayController | null {
  if (!detectWebGLSupport()) {
    options.onFallback("WebGL unavailable");
    return null;
  }
  if (!hasUsableTileConfig(options.payload)) {
    options.onFallback("No external tile_config configured");
    return null;
  }

  const overlays = buildOverlayGeoJson(options.payload);
  const style = buildInlineStyle(options.payload, options.theme, overlays);

  let map: MapLibreMap | null = null;
  let fallenBack = false;
  let tileErrorCount = 0;

  const triggerFallback = (reason: string): void => {
    if (fallenBack) return;
    fallenBack = true;
    try {
      map?.remove();
    } catch (err) {
      console.debug("MapLibre cleanup error during fallback:", err);
    }
    map = null;
    options.onFallback(reason);
  };

  try {
    const center: [number, number] = overlays.bounds
      ? [
          (overlays.bounds[0] + overlays.bounds[2]) / 2,
          (overlays.bounds[1] + overlays.bounds[3]) / 2,
        ]
      : [148.125, -36.615];

    const m = new maplibregl.Map({
      container: options.container,
      style,
      center,
      zoom: 11,
      attributionControl: false,
      pitchWithRotate: false,
      dragRotate: false,
    });
    map = m;

    m.addControl(
      new maplibregl.NavigationControl({ showCompass: true, showZoom: true }),
      "top-right"
    );
    m.addControl(
      new maplibregl.ScaleControl({ maxWidth: 110, unit: "metric" }),
      "bottom-left"
    );

    m.on("load", () => {
      if (!map || fallenBack) return;
      if (overlays.bounds) {
        const b = overlays.bounds;
        if (Math.abs(b[2] - b[0]) > 1e-5 || Math.abs(b[3] - b[1]) > 1e-5) {
          map.fitBounds(
            [
              [b[0], b[1]],
              [b[2], b[3]],
            ] as LngLatBoundsLike,
            { padding: 36, duration: 0, maxZoom: 14.5 }
          );
        }
      }
    });

    m.on("error", (ev) => {
      tileErrorCount += 1;
      console.debug("MapLibre MCP error event:", ev.error);
      if (tileErrorCount >= 2) {
        triggerFallback("MapLibre tile or CSP error");
      }
    });

    m.on("click", "tfm-lines-main", (ev) => {
      const feat = ev.features?.[0];
      const trackId = feat?.properties?.track_id;
      if (typeof trackId === "string" && trackId) {
        options.onTrackClick?.(trackId);
      }
    });

    m.on("mousemove", "tfm-lines-main", (ev) => {
      if (!map) return;
      map.getCanvas().style.cursor = "pointer";
      if (ev.lngLat) {
        options.onHoverCoord?.(ev.lngLat.lng, ev.lngLat.lat);
      }
    });

    m.on("mouseleave", "tfm-lines-main", () => {
      if (map) map.getCanvas().style.cursor = "";
    });
  } catch (err) {
    console.warn("MapLibre initialization failed, falling back to SVG map:", err);
    triggerFallback("MapLibre init exception");
    return null;
  }

  return {
    setCursorPosition(lon: number, lat: number, color: string): void {
      if (!map || fallenBack) return;
      const src = map.getSource("tfm-cursor") as GeoJSONSource | undefined;
      if (!src) return;
      src.setData({
        type: "FeatureCollection",
        features: [
          {
            type: "Feature",
            properties: { color },
            geometry: { type: "Point", coordinates: [lon, lat] },
          },
        ],
      });
    },
    clearCursor(): void {
      if (!map || fallenBack) return;
      const src = map.getSource("tfm-cursor") as GeoJSONSource | undefined;
      if (!src) return;
      src.setData({ type: "FeatureCollection", features: [] });
    },
    fitToBbox(bbox: [number, number, number, number]): void {
      if (!map || fallenBack) return;
      map.fitBounds(
        [
          [bbox[0], bbox[1]],
          [bbox[2], bbox[3]],
        ] as LngLatBoundsLike,
        { padding: 36, duration: 200, maxZoom: 14.5 }
      );
    },
    destroy(): void {
      fallenBack = true;
      try {
        map?.remove();
      } catch (err) {
        console.debug("MapLibre destroy error:", err);
      }
      map = null;
    },
  };
}
