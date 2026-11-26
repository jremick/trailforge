import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { Protocol } from "pmtiles";
import {
  DEFAULT_CENTER_LAT,
  DEFAULT_CENTER_LON,
  DEFAULT_MAP_ZOOM,
} from "./presets";
import {
  EMPTY_FEATURE_COLLECTION,
  buildSearchPinGeoJson,
  buildTrailForgeStyle,
} from "./style";
import type {
  BasemapId,
  LayerVisibilityState,
  RegionPreset,
} from "./types";

let pmtilesProtocolRegistered = false;

/**
 * Register the PMTiles v4 protocol handler with MapLibre GL JS 5 once.
 * Enables `pmtiles://` vector and terrain tile sources in Slice 03+.
 */
export function ensurePmtilesProtocol(): void {
  if (pmtilesProtocolRegistered) return;
  const protocol = new Protocol();
  maplibregl.addProtocol("pmtiles", protocol.tile);
  pmtilesProtocolRegistered = true;
}

export interface TrailForgeMapCallbacks {
  onCursorMove?: (lat: number, lon: number) => void;
  onMapMoveEnd?: (lat: number, lon: number, zoom: number) => void;
  onBasemapTileError?: () => void;
}

export interface TrailForgeMapOptions {
  initialCenter?: readonly [number, number];
  initialZoom?: number;
  initialBasemap: BasemapId;
  initialLayers: LayerVisibilityState;
}

export class TrailForgeMap {
  private readonly map: maplibregl.Map;
  private isLoaded = false;
  private currentBasemap: BasemapId;
  private currentLayers: LayerVisibilityState;
  private reportedTileError = false;
  private pendingPin: { lat: number; lon: number; label: string } | null = null;

  constructor(
    container: HTMLElement,
    callbacks: TrailForgeMapCallbacks,
    options: TrailForgeMapOptions
  ) {
    ensurePmtilesProtocol();

    this.currentBasemap = options.initialBasemap;
    this.currentLayers = { ...options.initialLayers };

    const center: [number, number] = options.initialCenter
      ? [options.initialCenter[0], options.initialCenter[1]]
      : [DEFAULT_CENTER_LON, DEFAULT_CENTER_LAT];
    const zoom = options.initialZoom ?? DEFAULT_MAP_ZOOM;

    this.map = new maplibregl.Map({
      container,
      style: buildTrailForgeStyle({
        initialBasemap: this.currentBasemap,
        layers: this.currentLayers,
      }),
      center,
      zoom,
      attributionControl: false,
      pitchWithRotate: false,
    });

    this.map.addControl(
      new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }),
      "bottom-left"
    );

    this.map.on("mousemove", (e) => {
      callbacks.onCursorMove?.(e.lngLat.lat, e.lngLat.lng);
    });

    this.map.on("moveend", () => {
      const c = this.map.getCenter();
      callbacks.onMapMoveEnd?.(c.lat, c.lng, this.map.getZoom());
    });

    // Gracefully absorb external raster tile load failures when offline
    // without crashing MapLibre or throwing uncaught errors.
    this.map.on("error", (event) => {
      const sourceId = (event as { sourceId?: string }).sourceId;
      const isExternalBasemap =
        sourceId === "osm-standard" ||
        sourceId === "opentopomap" ||
        sourceId === "esri-imagery";

      if (isExternalBasemap && !this.reportedTileError) {
        this.reportedTileError = true;
        callbacks.onBasemapTileError?.();
      }
    });

    this.map.on("load", () => {
      this.isLoaded = true;
      this.applyBasemapVisibility();
      this.applyLayerVisibility();
      if (this.pendingPin) {
        this.applySearchPin(
          this.pendingPin.lat,
          this.pendingPin.lon,
          this.pendingPin.label
        );
        this.pendingPin = null;
      }
    });
  }

  setBasemap(basemap: BasemapId): void {
    this.currentBasemap = basemap;
    if (basemap !== "offline-topo") {
      this.reportedTileError = false;
    }
    if (this.isLoaded) {
      this.applyBasemapVisibility();
    }
  }

  setLayerVisibility(layers: LayerVisibilityState): void {
    this.currentLayers = { ...layers };
    if (this.isLoaded) {
      this.applyLayerVisibility();
    }
  }

  flyToPreset(preset: RegionPreset, sidebarWidthPx = 340): void {
    const [west, south, east, north] = preset.bbox;
    if (preset.group === "region" && (preset.id === "vic" || preset.id === "nsw")) {
      this.map.fitBounds(
        [
          [west, south],
          [east, north],
        ],
        {
          padding: {
            top: 64,
            bottom: 64,
            left: Math.min(sidebarWidthPx + 32, Math.floor(window.innerWidth * 0.4)),
            right: 48,
          },
          duration: 650,
          essential: true,
        }
      );
      return;
    }

    this.map.flyTo({
      center: [preset.center[0], preset.center[1]],
      zoom: preset.zoom,
      duration: 650,
      essential: true,
    });
  }

  flyToCoordinate(lat: number, lon: number, label: string, zoom = 13): void {
    if (this.isLoaded) {
      this.applySearchPin(lat, lon, label);
    } else {
      this.pendingPin = { lat, lon, label };
    }
    this.map.flyTo({
      center: [lon, lat],
      zoom: Math.max(this.map.getZoom(), zoom),
      duration: 650,
      essential: true,
    });
  }

  clearSearchPin(): void {
    this.pendingPin = null;
    if (!this.isLoaded) return;
    const src = this.map.getSource("tf-search-pin") as
      | maplibregl.GeoJSONSource
      | undefined;
    src?.setData(EMPTY_FEATURE_COLLECTION);
  }

  async loadOutdoorIcon(iconName: string): Promise<boolean> {
    if (!this.isLoaded) return false;
    const id = `poi-${iconName}`;
    if (this.map.hasImage(id)) return true;

    try {
      const response = await fetch(`/icons/${encodeURIComponent(iconName)}.svg`);
      if (!response.ok) return false;
      const svgText = await response.text();
      const blob = new Blob([svgText], { type: "image/svg+xml" });
      const url = URL.createObjectURL(blob);
      try {
        const img = new Image(48, 48);
        await new Promise<void>((resolve, reject) => {
          img.onload = () => resolve();
          img.onerror = () => reject(new Error(`Icon load failed: ${iconName}`));
          img.src = url;
        });
        if (!this.map.hasImage(id)) {
          this.map.addImage(id, img, { pixelRatio: 2 });
        }
        return true;
      } finally {
        URL.revokeObjectURL(url);
      }
    } catch {
      return false;
    }
  }

  zoomIn(): void {
    this.map.zoomIn({ duration: 220 });
  }

  zoomOut(): void {
    this.map.zoomOut({ duration: 220 });
  }

  resetNorth(): void {
    this.map.easeTo({ bearing: 0, pitch: 0, duration: 280 });
  }

  resize(): void {
    this.map.resize();
  }

  destroy(): void {
    this.map.remove();
  }

  private applyBasemapVisibility(): void {
    const showOsm =
      this.currentBasemap === "osm" || this.currentBasemap === "opentopo";
    const showOpenTopo = this.currentBasemap === "opentopo";
    const showSat = this.currentBasemap === "satellite";

    this.setLayerVis("basemap-osm-underlay", showOsm);
    this.setLayerVis("basemap-opentopo", showOpenTopo);
    this.setLayerVis("basemap-satellite", showSat);
  }

  private applyLayerVisibility(): void {
    this.setLayerVis("tf-tracks-casing", this.currentLayers.tracks);
    this.setLayerVis("tf-tracks-line", this.currentLayers.tracks);
    this.setLayerVis("tf-steep-highlights", this.currentLayers.steepHighlights);
    this.setLayerVis(
      "tf-contours-hillshade",
      this.currentLayers.contoursHillshade
    );
    this.setLayerVis("tf-snow-line-fill", this.currentLayers.snowLine);
    this.setLayerVis("tf-snow-line-outline", this.currentLayers.snowLine);
    this.setLayerVis("tf-pois-layer", this.currentLayers.pois);
  }

  private setLayerVis(layerId: string, visible: boolean): void {
    if (!this.map.getLayer(layerId)) return;
    this.map.setLayoutProperty(
      layerId,
      "visibility",
      visible ? "visible" : "none"
    );
  }

  private applySearchPin(lat: number, lon: number, label: string): void {
    const src = this.map.getSource("tf-search-pin") as
      | maplibregl.GeoJSONSource
      | undefined;
    src?.setData(buildSearchPinGeoJson(lat, lon, label));
  }
}
