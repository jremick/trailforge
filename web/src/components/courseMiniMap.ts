import type { Feature, FeatureCollection, LineString, Point } from "geojson";
import * as maplibregl from "maplibre-gl";
import type {
  LayerSpecification,
  SourceSpecification,
  StyleSpecification,
} from "maplibre-gl";
import { getApiBaseUrl } from "../adminHelpers";
import {
  findNearestPointOnTrackCourseGeo,
  type TrackCoursePoint,
  type TrackCourseVertex,
} from "../inspectorHelpers";
import { ensurePmtilesProtocol } from "../map";
import { getGradeColor } from "../presets";
import {
  EMPTY_FEATURE_COLLECTION,
  buildOfflineGraticuleGeoJson,
  resolveGlyphsUrl,
  resolveVectorBasemapSource,
  toPmtilesUrl,
} from "../style";
import type { BasemapId, TileManifestResponse, WaterwayCrossingItem } from "../types";
import { sampleTrackCourseAtDistance } from "../inspectorHelpers";

export interface CourseMiniMapOptions {
  vertices: readonly TrackCourseVertex[];
  bbox: [number, number, number, number];
  totalDistM: number;
  crossings: readonly WaterwayCrossingItem[];
  initialBasemap: BasemapId;
  tileManifest?: TileManifestResponse | null;
  isOfflinePack?: boolean;
  onHoverPoint: (point: TrackCoursePoint) => void;
}

function buildTrackFeaturesFromVertices(
  vertices: readonly TrackCourseVertex[]
): FeatureCollection<LineString> {
  const features: Feature<LineString>[] = [];
  let runCoords: Array<[number, number]> = [];
  let runGrade = "";
  let runPoly = -1;

  const flush = (): void => {
    if (runCoords.length >= 2) {
      features.push({
        type: "Feature",
        properties: {
          grade: runGrade,
          color: getGradeColor(runGrade),
        },
        geometry: {
          type: "LineString",
          coordinates: runCoords,
        },
      });
    }
  };

  for (const v of vertices) {
    if (runCoords.length === 0) {
      runCoords = [[v.lon, v.lat]];
      runGrade = v.grade;
      runPoly = v.polylineId;
      continue;
    }

    if (v.polylineId !== runPoly) {
      flush();
      runCoords = [[v.lon, v.lat]];
      runGrade = v.grade;
      runPoly = v.polylineId;
      continue;
    }

    if (v.grade !== runGrade) {
      runCoords.push([v.lon, v.lat]);
      flush();
      runCoords = [[v.lon, v.lat]];
      runGrade = v.grade;
      continue;
    }

    runCoords.push([v.lon, v.lat]);
  }
  flush();

  return {
    type: "FeatureCollection",
    features,
  };
}

function buildCrossingsFeatures(
  vertices: readonly TrackCourseVertex[],
  crossings: readonly WaterwayCrossingItem[]
): FeatureCollection<Point> {
  const features: Feature<Point>[] = [];
  for (const c of crossings) {
    if (typeof c.dist_m !== "number" || !Number.isFinite(c.dist_m)) continue;
    const pt = sampleTrackCourseAtDistance(vertices, c.dist_m);
    if (!pt) continue;
    features.push({
      type: "Feature",
      properties: {
        name: c.name ?? "Waterway crossing",
        dist_m: c.dist_m,
      },
      geometry: {
        type: "Point",
        coordinates: [pt.lon, pt.lat],
      },
    });
  }
  return {
    type: "FeatureCollection",
    features,
  };
}

const MINI_VECTOR_BASEMAP_LAYER_IDS = [
  "mini-vbase-land",
  "mini-vbase-landcover",
  "mini-vbase-park",
  "mini-vbase-landuse",
  "mini-vbase-water",
  "mini-vbase-waterway",
  "mini-vbase-roads",
  "mini-vbase-water-name",
  "mini-vbase-road-name",
] as const;

export function buildCourseMiniMapStyle(
  basemap: BasemapId,
  trackFc: FeatureCollection<LineString>,
  crossingsFc: FeatureCollection<Point>,
  tileManifest?: TileManifestResponse | null,
  origin?: string,
  assetsOrigin?: string
): StyleSpecification {
  const showOpenTopo = basemap === "opentopo";
  const showOsm = basemap === "osm";
  const showSat = basemap === "satellite";
  const showOffline = basemap === "offline-topo";
  const showVector = basemap === "vector";
  // Keep vector basemap visible beneath raster layers so if external raster tiles
  // throttle or fail, the mini-map always shows crisp topographic terrain & roads
  // instead of a blank void.
  const showVectorUnderlay = showVector || showOpenTopo || showOsm;
  const showTerrainContext = showOffline || showVector || showOpenTopo;
  const vis = (enabled: boolean): "visible" | "none" => (enabled ? "visible" : "none");

  const useVector = Boolean(tileManifest?.vector_available && tileManifest.vector_url);
  const useTerrain = Boolean(tileManifest?.terrain_available && tileManifest.terrain_url);

  const sources: Record<string, SourceSpecification> = {
    "mini-opentopo": {
      type: "raster",
      tiles: ["https://tile.opentopomap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      maxzoom: 17,
    },
    "mini-osm": {
      type: "raster",
      tiles: [
        "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "https://b.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "https://c.tile.openstreetmap.org/{z}/{x}/{y}.png",
      ],
      tileSize: 256,
      maxzoom: 19,
    },
    "mini-satellite": {
      type: "raster",
      tiles: [
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      ],
      tileSize: 256,
      maxzoom: 19,
    },
    "mini-vector-basemap": resolveVectorBasemapSource(tileManifest, origin),
    "mini-graticule": {
      type: "geojson",
      data: buildOfflineGraticuleGeoJson(),
    },
    "mini-track": {
      type: "geojson",
      data: trackFc,
    },
    "mini-crossings": {
      type: "geojson",
      data: crossingsFc,
    },
    "mini-cursor": {
      type: "geojson",
      data: EMPTY_FEATURE_COLLECTION,
    },
  };

  if (useVector && tileManifest) {
    sources["mini-vector"] = {
      type: "vector",
      url: toPmtilesUrl(tileManifest.vector_url, origin),
    };
  }
  if (useTerrain && tileManifest) {
    sources["mini-terrain"] = {
      type: "raster-dem",
      url: toPmtilesUrl(tileManifest.terrain_url, origin),
      encoding: "mapbox",
      tileSize: 256,
    };
  }

  const layers: LayerSpecification[] = [
    {
      id: "mini-bg",
      type: "background",
      paint: { "background-color": "#0F1616" },
    },
    {
      id: "mini-graticule-lines",
      type: "line",
      source: "mini-graticule",
      paint: {
        "line-color": "#223332",
        "line-width": 1,
        "line-opacity": 0.8,
      },
    },
    {
      id: "mini-vbase-land",
      type: "background",
      layout: { visibility: vis(showVectorUnderlay) },
      paint: { "background-color": "#111919" },
    },
    {
      id: "mini-vbase-landcover",
      type: "fill",
      source: "mini-vector-basemap",
      "source-layer": "landcover",
      layout: { visibility: vis(showVectorUnderlay) },
      paint: {
        "fill-color": [
          "match",
          ["coalesce", ["get", "class"], ""],
          ["wood", "forest"],
          "#162620",
          ["grass", "meadow", "farmland"],
          "#1A2721",
          "#152220",
        ],
        "fill-opacity": 0.78,
      },
    },
    {
      id: "mini-vbase-park",
      type: "fill",
      source: "mini-vector-basemap",
      "source-layer": "park",
      layout: { visibility: vis(showVectorUnderlay) },
      paint: {
        "fill-color": "#132922",
        "fill-opacity": 0.52,
      },
    },
    {
      id: "mini-vbase-landuse",
      type: "fill",
      source: "mini-vector-basemap",
      "source-layer": "landuse",
      minzoom: 8,
      layout: { visibility: vis(showVectorUnderlay) },
      paint: {
        "fill-color": "#1B242E",
        "fill-opacity": 0.72,
      },
    },
    {
      id: "mini-vbase-water",
      type: "fill",
      source: "mini-vector-basemap",
      "source-layer": "water",
      layout: { visibility: vis(showVectorUnderlay) },
      paint: { "fill-color": "#0C2336" },
    },
    {
      id: "mini-vbase-waterway",
      type: "line",
      source: "mini-vector-basemap",
      "source-layer": "waterway",
      layout: { visibility: vis(showVectorUnderlay) },
      paint: {
        "line-color": "#1D4E6E",
        "line-width": 1.2,
        "line-opacity": 0.72,
      },
    },
    {
      id: "mini-vbase-roads",
      type: "line",
      source: "mini-vector-basemap",
      "source-layer": "transportation",
      filter: [
        "in",
        ["coalesce", ["get", "class"], ""],
        ["literal", ["motorway", "trunk", "primary", "secondary", "tertiary", "minor"]],
      ],
      layout: {
        visibility: vis(showVectorUnderlay),
        "line-cap": "round",
        "line-join": "round",
      },
      paint: {
        "line-color": [
          "match",
          ["coalesce", ["get", "class"], ""],
          "motorway",
          "#C88A32",
          "trunk",
          "#B48A3E",
          "primary",
          "#64748B",
          "#384859",
        ],
        "line-width": ["interpolate", ["linear"], ["zoom"], 8, 1.0, 14, 3.0],
        "line-opacity": 0.88,
      },
    },
    {
      id: "mini-basemap-opentopo",
      type: "raster",
      source: "mini-opentopo",
      layout: { visibility: vis(showOpenTopo) },
      paint: {
        "raster-opacity": 0.94,
        "raster-brightness-max": 0.92,
        "raster-contrast": 0.08,
      },
    },
    {
      id: "mini-basemap-osm",
      type: "raster",
      source: "mini-osm",
      layout: { visibility: vis(showOsm) },
      paint: {
        "raster-opacity": 0.92,
        "raster-brightness-max": 0.9,
      },
    },
    {
      id: "mini-basemap-satellite",
      type: "raster",
      source: "mini-satellite",
      layout: { visibility: vis(showSat) },
      paint: {
        "raster-opacity": 0.95,
      },
    },
  ];

  // Offline/vector DEM hillshade & vector contours/waterways for rich topo context
  if (useTerrain) {
    layers.push({
      id: "mini-hillshade",
      type: "hillshade",
      source: "mini-terrain",
      layout: { visibility: vis(showTerrainContext) },
      paint: {
        "hillshade-exaggeration": 0.55,
        "hillshade-shadow-color": "#050A0E",
        "hillshade-highlight-color": "#B8CCE0",
        "hillshade-accent-color": "#2C4052",
        "hillshade-illumination-direction": 315,
      },
    });
  }

  if (useVector) {
    layers.push(
      {
        id: "mini-contours",
        type: "line",
        source: "mini-vector",
        "source-layer": "contours",
        layout: { visibility: vis(showTerrainContext) },
        paint: {
          "line-color": "#476074",
          "line-width": 0.85,
          "line-opacity": 0.55,
        },
      },
      {
        id: "mini-waterways",
        type: "line",
        source: "mini-vector",
        "source-layer": "waterways",
        layout: { visibility: vis(showTerrainContext) },
        paint: {
          "line-color": "#3B82A8",
          "line-width": 1.3,
          "line-opacity": 0.65,
        },
      }
    );
  }

  // High-contrast selected track spotlight stack over the underlying map
  layers.push(
    {
      id: "mini-track-halo",
      type: "line",
      source: "mini-track",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": "#F8FAFC",
        "line-width": 9.5,
        "line-opacity": 0.72,
      },
    },
    {
      id: "mini-track-casing",
      type: "line",
      source: "mini-track",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": "#070B10",
        "line-width": 7.0,
        "line-opacity": 0.94,
      },
    },
    {
      id: "mini-track-line",
      type: "line",
      source: "mini-track",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": ["coalesce", ["get", "color"], "#38BDF8"],
        "line-width": 4.2,
      },
    },
    {
      id: "mini-vbase-water-name",
      type: "symbol",
      source: "mini-vector-basemap",
      "source-layer": "water_name",
      minzoom: 8,
      layout: {
        visibility: vis(showVector),
        "text-field": ["coalesce", ["get", "name:latin"], ["get", "name"], ""],
        "text-font": ["Noto Sans Italic"],
        "text-size": 10.5,
        "text-allow-overlap": false,
      },
      paint: {
        "text-color": "#7DD3FC",
        "text-halo-color": "rgba(9, 14, 20, 0.92)",
        "text-halo-width": 1.5,
      },
    },
    {
      id: "mini-vbase-road-name",
      type: "symbol",
      source: "mini-vector-basemap",
      "source-layer": "transportation_name",
      minzoom: 10,
      layout: {
        visibility: vis(showVector),
        "symbol-placement": "line",
        "symbol-spacing": 280,
        "text-field": ["coalesce", ["get", "name:latin"], ["get", "name"], ["get", "ref"], ""],
        "text-font": ["Noto Sans Medium"],
        "text-size": 10,
        "text-allow-overlap": false,
      },
      paint: {
        "text-color": "#E2E8F0",
        "text-halo-color": "rgba(9, 14, 20, 0.92)",
        "text-halo-width": 1.6,
      },
    },
    {
      id: "mini-crossings-dots",
      type: "circle",
      source: "mini-crossings",
      paint: {
        "circle-radius": 4.2,
        "circle-color": "#38BDF8",
        "circle-stroke-color": "#070B10",
        "circle-stroke-width": 1.6,
      },
    },
    // Synchronized live scrub cursor (outer yellow glow + crisp white ring + grade center dot)
    {
      id: "mini-cursor-glow",
      type: "circle",
      source: "mini-cursor",
      paint: {
        "circle-radius": 15,
        "circle-color": "#FDE047",
        "circle-opacity": 0.38,
      },
    },
    {
      id: "mini-cursor-ring",
      type: "circle",
      source: "mini-cursor",
      paint: {
        "circle-radius": 8.5,
        "circle-color": "rgba(7, 11, 16, 0.2)",
        "circle-stroke-color": "#FFFFFF",
        "circle-stroke-width": 2.4,
      },
    },
    {
      id: "mini-cursor-dot",
      type: "circle",
      source: "mini-cursor",
      paint: {
        "circle-radius": 5.2,
        "circle-color": ["coalesce", ["get", "color"], "#FDE047"],
        "circle-stroke-color": "#070B10",
        "circle-stroke-width": 1.8,
      },
    }
  );

  return {
    version: 8,
    name: "TrailForge Course MiniMap",
    glyphs: resolveGlyphsUrl(assetsOrigin ?? origin),
    sources,
    layers,
  };
}

function createEndpointMarkerEl(label: "S" | "E"): HTMLDivElement {
  const el = document.createElement("div");
  el.className = `tf-mini-endpoint ${label === "S" ? "start" : "end"}`;
  el.textContent = label;
  return el;
}

/**
 * Interactive MapLibre GL JS 5 mini-map embedded inside the expanded Elevation Modal.
 * Renders the real underlying topographic/satellite/offline map with the deduplicated
 * track overlay and a 60fps synchronized position marker.
 */
export class CourseMiniMap {
  private readonly container: HTMLElement;
  private readonly map: maplibregl.Map;
  private readonly options: CourseMiniMapOptions;
  private readonly markers: maplibregl.Marker[] = [];
  private isLoaded = false;
  private pendingCursor: { pt: TrackCoursePoint; color: string } | null = null;
  private activeBasemap: BasemapId;

  constructor(container: HTMLElement, options: CourseMiniMapOptions) {
    ensurePmtilesProtocol();
    this.container = container;
    this.options = options;
    this.activeBasemap = options.initialBasemap;

    const trackFc = buildTrackFeaturesFromVertices(options.vertices);
    const crossingsFc = buildCrossingsFeatures(options.vertices, options.crossings);
    const origin = options.isOfflinePack
      ? window.location.origin
      : getApiBaseUrl() || window.location.origin;

    const [minLon, minLat, maxLon, maxLat] = options.bbox;
    const center: [number, number] = [(minLon + maxLon) / 2, (minLat + maxLat) / 2];

    this.map = new maplibregl.Map({
      container,
      style: buildCourseMiniMapStyle(
        this.activeBasemap,
        trackFc,
        crossingsFc,
        options.tileManifest,
        origin,
        window.location.origin
      ),
      center,
      zoom: 12,
      attributionControl: false,
      dragRotate: false,
      pitchWithRotate: false,
      touchPitch: false,
    });

    this.map.addControl(
      new maplibregl.ScaleControl({ maxWidth: 90, unit: "metric" }),
      "bottom-left"
    );

    const startPt =
      sampleTrackCourseAtDistance(options.vertices, 0) ?? options.vertices[0];
    const endPt =
      sampleTrackCourseAtDistance(options.vertices, options.totalDistM) ??
      options.vertices[options.vertices.length - 1];

    if (startPt) {
      this.markers.push(
        new maplibregl.Marker({
          element: createEndpointMarkerEl("S"),
          anchor: "center",
        })
          .setLngLat([startPt.lon, startPt.lat])
          .addTo(this.map)
      );
    }
    if (endPt) {
      this.markers.push(
        new maplibregl.Marker({
          element: createEndpointMarkerEl("E"),
          anchor: "center",
        })
          .setLngLat([endPt.lon, endPt.lat])
          .addTo(this.map)
      );
    }

    const fitImmediately = (): void => {
      const spanLon = Math.max(0.002, maxLon - minLon);
      const spanLat = Math.max(0.002, maxLat - minLat);
      this.map.fitBounds(
        [
          [minLon - spanLon * 0.04, minLat - spanLat * 0.04],
          [maxLon + spanLon * 0.04, maxLat + spanLat * 0.04],
        ],
        {
          padding: 36,
          maxZoom: 15,
          duration: 0,
        }
      );
    };

    fitImmediately();

    this.map.on("load", () => {
      this.isLoaded = true;
      this.map.resize();
      fitImmediately();
      if (this.pendingCursor) {
        this.applyCursor(this.pendingCursor.pt, this.pendingCursor.color);
      }
    });

    const emitNearest = (lng: number, lat: number): void => {
      const nearest = findNearestPointOnTrackCourseGeo(
        this.options.vertices,
        lng,
        lat
      );
      if (nearest) {
        this.options.onHoverPoint(nearest);
      }
    };

    this.map.on("mousemove", (e) => {
      if (e.originalEvent && e.originalEvent.buttons !== 0) return;
      emitNearest(e.lngLat.lng, e.lngLat.lat);
    });

    this.map.on("click", (e) => {
      emitNearest(e.lngLat.lng, e.lngLat.lat);
    });
  }

  setBasemap(basemap: BasemapId): void {
    this.activeBasemap = basemap;
    if (!this.isLoaded) return;
    const setVis = (id: string, visible: boolean): void => {
      if (this.map.getLayer(id)) {
        this.map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
      }
    };
    const showVector = basemap === "vector";
    const showTerrainContext = basemap === "offline-topo" || showVector;
    setVis("mini-basemap-opentopo", basemap === "opentopo");
    setVis("mini-basemap-osm", basemap === "osm");
    setVis("mini-basemap-satellite", basemap === "satellite");
    for (const id of MINI_VECTOR_BASEMAP_LAYER_IDS) {
      setVis(id, showVector);
    }
    setVis("mini-hillshade", showTerrainContext);
    setVis("mini-contours", showTerrainContext);
    setVis("mini-waterways", showTerrainContext);
  }

  fitTrack(): void {
    const [minLon, minLat, maxLon, maxLat] = this.options.bbox;
    const spanLon = Math.max(0.002, maxLon - minLon);
    const spanLat = Math.max(0.002, maxLat - minLat);
    this.map.fitBounds(
      [
        [minLon - spanLon * 0.04, minLat - spanLat * 0.04],
        [maxLon + spanLon * 0.04, maxLat + spanLat * 0.04],
      ],
      {
        padding: 36,
        maxZoom: 15,
        duration: 280,
      }
    );
  }

  setCursor(pt: TrackCoursePoint, color: string): void {
    this.pendingCursor = { pt, color };
    this.container.dataset.cursorLat = pt.lat.toFixed(5);
    this.container.dataset.cursorLon = pt.lon.toFixed(5);
    this.container.dataset.cursorDist = pt.distM.toFixed(1);
    if (this.isLoaded) {
      this.applyCursor(pt, color);
    }
  }

  private applyCursor(pt: TrackCoursePoint, color: string): void {
    const src = this.map.getSource("mini-cursor") as
      | maplibregl.GeoJSONSource
      | undefined;
    if (!src) return;
    src.setData({
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          properties: {
            dist_m: pt.distM,
            color,
          },
          geometry: {
            type: "Point",
            coordinates: [pt.lon, pt.lat],
          },
        },
      ],
    });
  }

  destroy(): void {
    for (const marker of this.markers) {
      marker.remove();
    }
    this.markers.length = 0;
    this.map.remove();
  }
}
