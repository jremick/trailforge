import type { FeatureCollection } from "geojson";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { Protocol } from "pmtiles";
import {
  DEFAULT_CENTER_LAT,
  DEFAULT_CENTER_LON,
  DEFAULT_MAP_ZOOM,
  DEFAULT_SNOWLINE_BANDS_M,
  OUTDOOR_POI_ICONS,
  escapeHtml,
  getGradeColor,
  resolvePoiIconName,
} from "./presets";
import {
  EMPTY_FEATURE_COLLECTION,
  SELECTED_TRACK_LAYER_IDS,
  SELECTED_TRACK_STEEP_LAYER_ID,
  buildSearchPinGeoJson,
  buildSelectedTrackFilter,
  buildSnowlineFilter,
  buildTrailForgeStyle,
  getTrackSelectionOpacities,
  poisToGeoJson,
  userPinsToGeoJson,
} from "./style";
import { getApiBaseUrl } from "./adminHelpers";
import { formatClosureTypeLabel } from "./closuresHelpers";
import { formatHotspotReason } from "./gradingHelpers";
import { buildRecordedTrackGeoJson } from "./offline/recorderEngine";
import { formatReportTypeLabel, isGateWarningType } from "./reportsHelpers";
import type {
  BasemapId,
  BBoxTuple,
  LayerVisibilityState,
  PoiItem,
  RecordedBreadcrumbPoint,
  RegionPreset,
  TileManifestResponse,
  UserPinItem,
} from "./types";

let pmtilesProtocolRegistered = false;

/**
 * Register the PMTiles v4 protocol handler with MapLibre GL JS 5 once.
 */
export function ensurePmtilesProtocol(): void {
  if (pmtilesProtocolRegistered) return;
  const protocol = new Protocol();
  maplibregl.addProtocol("pmtiles", protocol.tile);
  pmtilesProtocolRegistered = true;
}

function renderContourBadgeImage(elevText: string): {
  width: number;
  height: number;
  data: Uint8Array;
} | null {
  const scale = 2;
  const logicalW = Math.max(30, 10 + elevText.length * 5.6);
  const w = Math.ceil(logicalW * scale);
  const h = 13 * scale;
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;

  const r = 3.5 * scale;
  ctx.beginPath();
  ctx.roundRect(1, 1, w - 2, h - 2, r);
  ctx.fillStyle = "rgba(11, 17, 23, 0.62)";
  ctx.fill();
  ctx.strokeStyle = "rgba(148, 163, 184, 0.35)";
  ctx.lineWidth = 0.9 * scale;
  ctx.stroke();

  ctx.fillStyle = "#CBD5E1";
  ctx.font = `600 ${8.4 * scale}px ui-monospace, SFMono-Regular, Menlo, monospace`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(elevText, w / 2, h / 2 + 0.5);

  const img = ctx.getImageData(0, 0, w, h);
  return { width: w, height: h, data: new Uint8Array(img.data.buffer) };
}

function renderFallbackPoiBadge(): {
  width: number;
  height: number;
  data: Uint8Array;
} | null {
  const size = 32;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;

  ctx.beginPath();
  ctx.arc(size / 2, size / 2, 10, 0, Math.PI * 2);
  ctx.fillStyle = "#10B981";
  ctx.fill();
  ctx.strokeStyle = "#FFFFFF";
  ctx.lineWidth = 2.5;
  ctx.stroke();

  const img = ctx.getImageData(0, 0, size, size);
  return { width: size, height: size, data: new Uint8Array(img.data.buffer) };
}

function renderRoutePinBadgeImage(pinKey: string): {
  width: number;
  height: number;
  data: Uint8Array;
} | null {
  const scale = 2;
  const logicalSize = 24;
  const size = logicalSize * scale;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;

  const dashIdx = pinKey.indexOf("-");
  const kind = dashIdx >= 0 ? pinKey.slice(0, dashIdx) : "leg";
  const label = (dashIdx >= 0 ? pinKey.slice(dashIdx + 1) : pinKey).slice(0, 3) || "1";

  const fillColor =
    kind === "start"
      ? "#10B981"
      : kind === "finish"
        ? "#F59E0B"
        : kind === "wpt" || kind === "waypoint"
          ? "#38BDF8"
          : "#14B8A6";

  const cx = size / 2;
  const cy = size / 2;
  const r = 10 * scale;

  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.fillStyle = fillColor;
  ctx.fill();
  ctx.strokeStyle = "#042F2E";
  ctx.lineWidth = 2.2 * scale;
  ctx.stroke();

  ctx.fillStyle = "#042F2E";
  const fontSize = label.length >= 3 ? 7.8 * scale : label.length === 2 ? 8.8 * scale : 10 * scale;
  ctx.font = `800 ${fontSize}px ui-monospace, SFMono-Regular, Menlo, monospace`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(label, cx, cy + 0.5 * scale);

  const img = ctx.getImageData(0, 0, size, size);
  return { width: size, height: size, data: new Uint8Array(img.data.buffer) };
}

export interface TrailForgeMapCallbacks {
  onCursorMove?: (lat: number, lon: number) => void;
  onMapMoveEnd?: (lat: number, lon: number, zoom: number) => void;
  onBasemapTileError?: () => void;
  onTrackClick?: (trackId: string, segmentId?: string) => void;
  onPinDropClick?: (lat: number, lon: number) => void;
  onReportPickClick?: (lat: number, lon: number) => void;
}

export interface TrailForgeMapOptions {
  initialCenter?: readonly [number, number];
  initialZoom?: number;
  initialBasemap: BasemapId;
  initialLayers: LayerVisibilityState;
}

export class TrailForgeMap {
  private readonly container: HTMLElement;
  private readonly map: maplibregl.Map;
  private readonly hoverPopup: maplibregl.Popup;
  private isLoaded = false;
  private currentBasemap: BasemapId;
  private currentLayers: LayerVisibilityState;
  private currentManifest: TileManifestResponse | null = null;
  private selectedTrackId: string | null = null;
  private reportedTileError = false;
  private coordPickMode: "pin" | "report" | null = null;
  private pendingPin: { lat: number; lon: number; label: string } | null = null;
  private fallbackTracks: FeatureCollection | null = null;
  private fallbackPois: PoiItem[] | null = null;
  private closuresGeoJson: FeatureCollection | null = null;
  private hotspotsGeoJson: FeatureCollection | null = null;
  private reportsGeoJson: FeatureCollection | null = null;
  private unmappedGeoJson: FeatureCollection | null = null;
  private recordedPoints: RecordedBreadcrumbPoint[] = [];
  private backtrackActive = false;
  private plannedRouteLineFc: FeatureCollection = EMPTY_FEATURE_COLLECTION;
  private plannedRoutePinsFc: FeatureCollection = EMPTY_FEATURE_COLLECTION;
  private routePlanMode = false;
  private userPins: UserPinItem[] = [];
  private readonly loadedIconIds = new Set<string>();
  private readonly loadingIcons = new Set<string>();

  constructor(
    container: HTMLElement,
    callbacks: TrailForgeMapCallbacks,
    options: TrailForgeMapOptions
  ) {
    ensurePmtilesProtocol();
    this.container = container;

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
        origin: window.location.origin,
      }),
      center,
      zoom,
      attributionControl: false,
      pitchWithRotate: false,
    });

    this.hoverPopup = new maplibregl.Popup({
      closeButton: false,
      closeOnClick: false,
      offset: 12,
      className: "tf-map-popup",
    });

    this.map.addControl(
      new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }),
      "bottom-left"
    );

    this.map.on("mousemove", (e) => {
      callbacks.onCursorMove?.(e.lngLat.lat, e.lngLat.lng);
      this.handlePointerHover(e);
    });

    this.map.on("click", (e) => {
      if (this.coordPickMode === "pin") {
        callbacks.onPinDropClick?.(e.lngLat.lat, e.lngLat.lng);
        return;
      }
      if (this.coordPickMode === "report") {
        callbacks.onReportPickClick?.(e.lngLat.lat, e.lngLat.lng);
        return;
      }

      const hitLayers = [
        "tf-reports-points",
        "tf-hotspots-points",
        "tf-hotspots-line",
        "tf-closures-points",
        "tf-closures-line",
        "tf-planned-route-line",
        SELECTED_TRACK_STEEP_LAYER_ID,
        "tf-tracks-selected-core",
        "tf-steep-highlights",
        "tf-tracks-line",
      ].filter((id) => Boolean(this.map.getLayer(id)));
      if (hitLayers.length === 0) return;

      const bbox: [maplibregl.PointLike, maplibregl.PointLike] = [
        [e.point.x - 6, e.point.y - 6],
        [e.point.x + 6, e.point.y + 6],
      ];
      const features = this.map.queryRenderedFeatures(bbox, {
        layers: hitLayers,
      });
      const first = features[0];
      if (!first) return;
      const props = (first.properties ?? {}) as Record<string, unknown>;
      const trackId =
        typeof props.track_id === "string" && props.track_id.trim()
          ? props.track_id
          : null;
      const segmentId =
        typeof props.segment_id === "string" ? props.segment_id : undefined;

      if (trackId) {
        this.setSelectedTrackId(trackId);
        callbacks.onTrackClick?.(trackId, segmentId);
      }
    });

    this.map.on("moveend", () => {
      const c = this.map.getCenter();
      callbacks.onMapMoveEnd?.(c.lat, c.lng, this.map.getZoom());
    });

    // Dynamically generate missing contour elevation badges or fallback POI icons 100% offline
    this.map.on("styleimagemissing", (e) => {
      const id = e.id;
      if (id.startsWith("elev-")) {
        const elev = id.slice(5);
        const badge = renderContourBadgeImage(`${elev}m`);
        if (badge && !this.map.hasImage(id)) {
          this.map.addImage(id, badge, { pixelRatio: 2 });
        }
        return;
      }

      if (id.startsWith("route-pin-")) {
        const badge = renderRoutePinBadgeImage(id.slice(10));
        if (badge && !this.map.hasImage(id)) {
          this.map.addImage(id, badge, { pixelRatio: 2 });
        }
        return;
      }

      if (id.startsWith("poi-")) {
        const iconName = resolvePoiIconName(id.slice(4));
        void this.ensureOutdoorIcon(id, iconName);
      }
    });

    // Gracefully absorb external raster tile load failures when offline
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
      this.onStyleReady();
    });

    this.map.on("style.load", () => {
      if (this.isLoaded) {
        this.onStyleReady();
      }
    });
  }

  /**
   * Mount PMTiles vector & terrain sources from `/api/tiles/manifest.json`
   * (or Cloudflare R2 absolute `https://` URLs) without resetting camera center/zoom.
   */
  applyTileManifest(
    manifest: TileManifestResponse,
    options?: { isOfflinePack?: boolean }
  ): void {
    this.currentManifest = manifest;
    this.loadedIconIds.clear();
    const fallbackOrigin = options?.isOfflinePack
      ? window.location.origin
      : getApiBaseUrl() || window.location.origin;
    this.map.setStyle(
      buildTrailForgeStyle({
        initialBasemap: this.currentBasemap,
        layers: this.currentLayers,
        tileManifest: manifest,
        origin: fallbackOrigin,
        selectedTrackId: this.selectedTrackId,
      })
    );
  }

  /**
   * Populate GeoJSON fallback overlays when PMTiles are unavailable.
   */
  setFallbackData(
    tracksGeoJson: FeatureCollection | null,
    pois: PoiItem[] | null
  ): void {
    this.fallbackTracks = tracksGeoJson;
    this.fallbackPois = pois;
    if (this.isLoaded) {
      this.applyFallbackData();
    }
  }

  /**
   * Populate Slice 04 closures & gates GeoJSON hazard overlay.
   */
  setClosuresGeoJson(fc: FeatureCollection | null): void {
    this.closuresGeoJson = fc;
    if (this.isLoaded) {
      this.applyClosuresData();
    }
  }

  /**
   * Populate Slice 07 rough/slow obstacle hotspots GeoJSON overlay.
   */
  setHotspotsGeoJson(fc: FeatureCollection | null): void {
    this.hotspotsGeoJson = fc;
    if (this.isLoaded) {
      this.applyHotspotsData();
    }
  }

  /**
   * Populate Slice 09 live in-cab recorded breadcrumb trail & optional backtrack route.
   */
  setRecordedBreadcrumbs(
    points: readonly RecordedBreadcrumbPoint[],
    backtrackActive: boolean
  ): void {
    this.recordedPoints = [...points];
    this.backtrackActive = backtrackActive;
    if (this.isLoaded) {
      this.applyRecordedTrackData();
    }
  }

  /**
   * Populate Slice 08 community hazard & gate reports GeoJSON layer.
   */
  setReportsGeoJson(fc: FeatureCollection | null): void {
    this.reportsGeoJson = fc;
    if (this.isLoaded) {
      this.applyReportsData();
    }
  }

  /**
   * Populate Slice 06 anonymized unmapped track candidates GeoJSON layer.
   */
  setUnmappedGeoJson(fc: FeatureCollection | null): void {
    this.unmappedGeoJson = fc;
    if (this.isLoaded) {
      this.applyUnmappedData();
    }
  }

  /**
   * Populate Phase A active multi-track planned route line & numbered pins on MapLibre.
   */
  setPlannedRoute(
    lineFc: FeatureCollection | null,
    pinsFc: FeatureCollection | null
  ): void {
    this.plannedRouteLineFc = lineFc ?? EMPTY_FEATURE_COLLECTION;
    this.plannedRoutePinsFc = pinsFc ?? EMPTY_FEATURE_COLLECTION;
    if (this.isLoaded) {
      this.applyPlannedRouteData();
    }
  }

  setRoutePlanMode(active: boolean): void {
    this.routePlanMode = active;
    this.container.classList.toggle("route-plan-active", active);
  }

  /**
   * Populate Slice 05 signed-in user's personal pins on the map.
   */
  setUserPins(pins: readonly UserPinItem[]): void {
    this.userPins = [...pins];
    if (this.isLoaded) {
      this.applyUserPinsData();
    }
  }

  setCoordPickMode(mode: "pin" | "report" | null): void {
    this.coordPickMode = mode;
    this.container.classList.toggle("pin-drop-active", mode !== null);
    if (mode !== null) {
      this.hoverPopup.remove();
    }
  }

  setPinDropMode(active: boolean): void {
    this.setCoordPickMode(active ? "pin" : null);
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
      this.applySnowLineFilter();
    }
  }

  setSelectedTrackId(trackId: string | null): void {
    this.selectedTrackId = trackId;
    if (this.isLoaded) {
      this.applySelectedTrackHighlight();
    }
  }

  private applySelectedTrackHighlight(): void {
    const filter = buildSelectedTrackFilter(this.selectedTrackId);
    for (const id of [...SELECTED_TRACK_LAYER_IDS, SELECTED_TRACK_STEEP_LAYER_ID]) {
      if (this.map.getLayer(id)) {
        this.map.setFilter(id, filter);
      }
    }

    const opacities = getTrackSelectionOpacities(Boolean(this.selectedTrackId));
    const paintUpdates: Array<[string, number]> = [
      ["tf-tracks-casing", opacities.casing],
      ["tf-tracks-line", opacities.line],
      ["tf-tracks-access-warn", opacities.accessWarn],
      ["tf-steep-highlights", opacities.steep],
    ];
    for (const [id, opacity] of paintUpdates) {
      if (this.map.getLayer(id)) {
        this.map.setPaintProperty(id, "line-opacity", opacity);
      }
    }
  }

  flyToPreset(preset: RegionPreset, sidebarWidthPx = 340): void {
    if (
      preset.group === "region" &&
      (preset.id === "vic" || preset.id === "nsw")
    ) {
      this.fitBBox(preset.bbox, sidebarWidthPx, 48);
      return;
    }

    this.map.flyTo({
      center: [preset.center[0], preset.center[1]],
      zoom: preset.zoom,
      duration: 650,
      essential: true,
    });
  }

  fitBBox(
    bbox: BBoxTuple,
    paddingLeftPx = 360,
    paddingRightPx = 420
  ): void {
    const [west, south, east, north] = bbox;
    const maxPad = Math.floor(window.innerWidth * 0.38);
    this.map.fitBounds(
      [
        [west, south],
        [east, north],
      ],
      {
        padding: {
          top: 72,
          bottom: 72,
          left: Math.min(paddingLeftPx, maxPad),
          right: Math.min(paddingRightPx, maxPad),
        },
        maxZoom: 14.5,
        duration: 650,
        essential: true,
      }
    );
  }

  flyToCoordinate(lat: number, lon: number, label: string, zoom = 13.5): void {
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
    this.hoverPopup.remove();
    this.map.remove();
  }

  private onStyleReady(): void {
    this.applyBasemapVisibility();
    this.applyLayerVisibility();
    this.applySnowLineFilter();
    this.preloadContourBadges();
    void this.preloadOutdoorIcons();

    this.applySelectedTrackHighlight();

    if (this.pendingPin) {
      this.applySearchPin(
        this.pendingPin.lat,
        this.pendingPin.lon,
        this.pendingPin.label
      );
      this.pendingPin = null;
    }

    if (!this.currentManifest?.vector_available) {
      this.applyFallbackData();
    }
    this.applyClosuresData();
    this.applyHotspotsData();
    this.applyReportsData();
    this.applyUnmappedData();
    this.applyRecordedTrackData();
    this.applyPlannedRouteData();
    this.applyUserPinsData();
  }

  private preloadContourBadges(): void {
    for (let elev = 400; elev <= 2200; elev += 100) {
      const id = `elev-${elev}`;
      if (this.map.hasImage(id)) continue;
      const badge = renderContourBadgeImage(`${elev}m`);
      if (badge) {
        this.map.addImage(id, badge, { pixelRatio: 2 });
      }
    }
  }

  private async preloadOutdoorIcons(): Promise<void> {
    await Promise.all(
      OUTDOOR_POI_ICONS.map((icon) =>
        this.ensureOutdoorIcon(`poi-${icon.id}`, icon.id)
      )
    );
  }

  private async ensureOutdoorIcon(
    imageId: string,
    iconName: string
  ): Promise<void> {
    if (
      this.map.hasImage(imageId) ||
      this.loadedIconIds.has(imageId) ||
      this.loadingIcons.has(imageId)
    ) {
      return;
    }
    this.loadingIcons.add(imageId);

    try {
      const response = await fetch(`/icons/${encodeURIComponent(iconName)}.svg`);
      if (!response.ok) throw new Error("Icon fetch failed");
      const svgText = await response.text();
      const blob = new Blob([svgText], { type: "image/svg+xml" });
      const url = URL.createObjectURL(blob);
      try {
        const img = new Image(48, 48);
        await new Promise<void>((resolve, reject) => {
          img.onload = () => resolve();
          img.onerror = () => reject(new Error(`Icon decode failed: ${iconName}`));
          img.src = url;
        });
        if (!this.map.hasImage(imageId)) {
          this.map.addImage(imageId, img, { pixelRatio: 2 });
        }
        this.loadedIconIds.add(imageId);
      } finally {
        URL.revokeObjectURL(url);
      }
    } catch {
      const fallback = renderFallbackPoiBadge();
      if (fallback && !this.map.hasImage(imageId)) {
        this.map.addImage(imageId, fallback, { pixelRatio: 2 });
      }
    } finally {
      this.loadingIcons.delete(imageId);
    }
  }

  private handlePointerHover(e: maplibregl.MapMouseEvent): void {
    if (this.coordPickMode !== null) return;

    const queryLayers = [
      "tf-planned-route-pins-dot",
      "tf-reports-points",
      "tf-hotspots-points",
      "tf-hotspots-line",
      "tf-user-pins-dot",
      "tf-closures-points",
      "tf-closures-line",
      "tf-pois-icons",
      "tf-pois-layer",
      "tf-planned-route-line",
      SELECTED_TRACK_STEEP_LAYER_ID,
      "tf-tracks-selected-core",
      "tf-steep-highlights",
      "tf-tracks-line",
      "tf-unmapped-line",
      "tf-closures-polygon-fill",
    ].filter((id) => Boolean(this.map.getLayer(id)));

    if (queryLayers.length === 0) return;

    const bbox: [maplibregl.PointLike, maplibregl.PointLike] = [
      [e.point.x - 5, e.point.y - 5],
      [e.point.x + 5, e.point.y + 5],
    ];
    const features = this.map.queryRenderedFeatures(bbox, {
      layers: queryLayers,
    });
    const hit = features[0];

    if (!hit) {
      this.map.getCanvas().style.cursor = "";
      this.hoverPopup.remove();
      return;
    }

    const layerId = hit.layer.id;
    const props = (hit.properties ?? {}) as Record<string, unknown>;

    if (layerId === "tf-planned-route-pins-dot") {
      this.map.getCanvas().style.cursor = "pointer";
      const title = String(props.title ?? "Planned Route Waypoint");
      const kind = String(props.kind ?? "leg").toUpperCase();
      this.hoverPopup
        .setLngLat(e.lngLat)
        .setHTML(
          `<div class="tf-popup-title">${escapeHtml(title)}</div>
           <div class="tf-popup-meta">
             <span class="tf-popup-dot" style="background:#14b8a6"></span>
             <span>Planned Route · ${escapeHtml(kind)}</span>
           </div>`
        )
        .addTo(this.map);
      return;
    }

    if (layerId === "tf-planned-route-line") {
      this.map.getCanvas().style.cursor = "pointer";
      const name = String(props.name ?? "Planned Route Leg");
      const grade = String(props.grade ?? "Easy");
      const legNum =
        typeof props.leg_index === "number" ? `Leg ${props.leg_index + 1}` : "Planned Route";
      const hint = this.routePlanMode ? "Already in route" : "Click to inspect track";
      this.hoverPopup
        .setLngLat(e.lngLat)
        .setHTML(
          `<div class="tf-popup-title">${escapeHtml(name)}</div>
           <div class="tf-popup-meta">
             <span class="tf-popup-dot" style="background:#14b8a6"></span>
             <span>${escapeHtml(legNum)} · ${escapeHtml(grade)}</span>
             <span class="tf-popup-hint">${escapeHtml(hint)}</span>
           </div>`
        )
        .addTo(this.map);
      return;
    }

    if (layerId === "tf-reports-points") {
      this.map.getCanvas().style.cursor = "pointer";
      const reportType = String(props.report_type ?? "hazard");
      const label = formatReportTypeLabel(reportType);
      const isGate =
        props.is_gate_warning === true || isGateWarningType(reportType);
      const notes = typeof props.notes === "string" ? props.notes.slice(0, 90) : "";
      const confirms = typeof props.confirm_count === "number" ? props.confirm_count : 0;
      const stale = props.is_stale === true ? " · Stale" : "";
      this.hoverPopup
        .setLngLat(e.lngLat)
        .setHTML(
          `<div class="tf-popup-title">${isGate ? "⛔ " : "⚠ "}${escapeHtml(label)}</div>
           <div class="tf-popup-meta">
             <span class="tf-popup-warn">${isGate ? "Gate / Access Warning" : "Community Report"}</span>
             <span>· ✓${confirms}${escapeHtml(stale)}</span>
           </div>
           ${notes ? `<div class="tf-popup-note">${escapeHtml(notes)}</div>` : ""}`
        )
        .addTo(this.map);
      return;
    }

    if (layerId === "tf-hotspots-line" || layerId === "tf-hotspots-points") {
      this.map.getCanvas().style.cursor = "pointer";
      const trackName = String(
        props.track_name ?? props.name ?? "Obstacle Hotspot"
      );
      const avgSpeed =
        typeof props.avg_speed_kmh === "number"
          ? `${props.avg_speed_kmh.toFixed(1)} km/h avg`
          : "";
      const rough =
        typeof props.median_roughness_rms === "number"
          ? `${props.median_roughness_rms.toFixed(2)}g rough`
          : "";
      const wet =
        typeof props.wet_risk_level === "string" && props.wet_risk_level !== "dry"
          ? `Wet: ${props.wet_risk_level.replace(/_/g, " ")}`
          : "";
      let reasonsText = "";
      if (Array.isArray(props.obstacle_reasons)) {
        reasonsText = props.obstacle_reasons
          .map((r) => formatHotspotReason(String(r)))
          .join(", ");
      } else if (typeof props.obstacle_reasons === "string") {
        try {
          const parsed = JSON.parse(props.obstacle_reasons) as unknown;
          if (Array.isArray(parsed)) {
            reasonsText = parsed
              .map((r) => formatHotspotReason(String(r)))
              .join(", ");
          } else {
            reasonsText = formatHotspotReason(props.obstacle_reasons);
          }
        } catch {
          reasonsText = formatHotspotReason(props.obstacle_reasons);
        }
      }
      this.hoverPopup
        .setLngLat(e.lngLat)
        .setHTML(
          `<div class="tf-popup-title">⚡ Obstacle Hotspot · ${escapeHtml(trackName)}</div>
           <div class="tf-popup-meta">
             <span class="tf-popup-warn">${escapeHtml([avgSpeed, rough, wet].filter(Boolean).join(" · ") || "Slow / rough segment")}</span>
           </div>
           ${reasonsText ? `<div class="tf-popup-note">${escapeHtml(reasonsText)}</div>` : ""}`
        )
        .addTo(this.map);
      return;
    }

    if (layerId === "tf-unmapped-line") {
      this.map.getCanvas().style.cursor = "pointer";
      const count =
        typeof props.observation_count === "number"
          ? `${props.observation_count} trace${props.observation_count === 1 ? "" : "s"}`
          : "Anonymized spur";
      this.hoverPopup
        .setLngLat(e.lngLat)
        .setHTML(
          `<div class="tf-popup-title">Unmapped Track Candidate</div>
           <div class="tf-popup-meta">${escapeHtml(count)} · Off-network telemetry</div>`
        )
        .addTo(this.map);
      return;
    }

    if (layerId === "tf-user-pins-dot") {
      this.map.getCanvas().style.cursor = "pointer";
      const name = String(props.name ?? "Personal Pin");
      const cat = String(props.category ?? "waypoint");
      const notes =
        typeof props.notes === "string" && props.notes
          ? `<div class="tf-popup-note">${escapeHtml(props.notes)}</div>`
          : "";
      this.hoverPopup
        .setLngLat(e.lngLat)
        .setHTML(
          `<div class="tf-popup-title">${escapeHtml(name)}</div>
           <div class="tf-popup-meta">Personal Pin · ${escapeHtml(cat)}</div>
           ${notes}`
        )
        .addTo(this.map);
      return;
    }

    if (
      layerId === "tf-closures-line" ||
      layerId === "tf-closures-points" ||
      layerId === "tf-closures-polygon-fill"
    ) {
      this.map.getCanvas().style.cursor = "pointer";
      const title = String(props.title ?? "Closure / Access Alert");
      const typeLabel = formatClosureTypeLabel(
        String(props.closure_type ?? "warning")
      );
      const agency = typeof props.agency === "string" ? props.agency : "";
      const stale = props.is_stale === true ? " · STALE CACHE" : "";
      const hasTrack =
        typeof props.track_id === "string" && props.track_id.length > 0;
      this.hoverPopup
        .setLngLat(e.lngLat)
        .setHTML(
          `<div class="tf-popup-title">⚠ ${escapeHtml(title)}</div>
           <div class="tf-popup-meta">
             <span class="tf-popup-warn">${escapeHtml(typeLabel)}</span>
             ${agency ? `<span>· ${escapeHtml(agency)}</span>` : ""}
             ${stale ? `<span class="tf-popup-warn">${escapeHtml(stale)}</span>` : ""}
             ${hasTrack ? `<span class="tf-popup-hint">Click to inspect</span>` : ""}
           </div>`
        )
        .addTo(this.map);
      return;
    }

    if (layerId === "tf-pois-icons" || layerId === "tf-pois-layer") {
      this.map.getCanvas().style.cursor = "pointer";
      const name = String(props.name ?? "POI");
      const cat = String(props.category ?? props.poi_type ?? "").replace(
        /_/g,
        " "
      );
      const elev =
        typeof props.elevation_m === "number"
          ? `${Math.round(props.elevation_m)} m`
          : typeof props.elev_m === "number"
            ? `${Math.round(props.elev_m)} m`
            : "";
      const note =
        typeof props.access_note === "string" && props.access_note
          ? `<div class="tf-popup-note">${escapeHtml(props.access_note)}</div>`
          : "";
      this.hoverPopup
        .setLngLat(e.lngLat)
        .setHTML(
          `<div class="tf-popup-title">${escapeHtml(name)}</div>
           <div class="tf-popup-meta">${escapeHtml([cat, elev].filter(Boolean).join(" · "))}</div>
           ${note}`
        )
        .addTo(this.map);
      return;
    }

    // Track / Steep hover
    this.map.getCanvas().style.cursor = "pointer";
    const trackName = String(
      props.track_name ?? props.name ?? "4WD Track Segment"
    );
    const grade = String(props.grade ?? props.effective_grade ?? "Easy");
    const liveGrade =
      typeof props.live_grade === "string" && props.live_grade
        ? props.live_grade
        : null;
    const wetRisk =
      typeof props.wet_risk_level === "string" &&
      props.wet_risk_level &&
      props.wet_risk_level !== "dry"
        ? props.wet_risk_level.replace(/_/g, " ")
        : null;
    const slope =
      typeof props.max_slope_deg === "number"
        ? `${props.max_slope_deg.toFixed(1)}° max`
        : "";
    const color = getGradeColor(grade);
    const clickHint = this.routePlanMode
      ? "+ Click to add to route"
      : "Click to inspect";
    this.hoverPopup
      .setLngLat(e.lngLat)
      .setHTML(
        `<div class="tf-popup-title">${escapeHtml(trackName)}</div>
         <div class="tf-popup-meta">
           <span class="tf-popup-dot" style="background:${color}"></span>
           <span>${escapeHtml(grade)}</span>
           ${liveGrade ? `<span>· Live: ${escapeHtml(liveGrade)}</span>` : ""}
           ${slope ? `<span>· ${escapeHtml(slope)}</span>` : ""}
           ${wetRisk ? `<span class="tf-popup-warn">· ${escapeHtml(wetRisk)}</span>` : ""}
           <span class="tf-popup-hint">${escapeHtml(clickHint)}</span>
         </div>`
      )
      .addTo(this.map);
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
    for (const id of SELECTED_TRACK_LAYER_IDS) {
      this.setLayerVis(id, this.currentLayers.tracks);
    }
    this.setLayerVis(
      SELECTED_TRACK_STEEP_LAYER_ID,
      this.currentLayers.tracks && this.currentLayers.steepHighlights
    );
    this.setLayerVis("tf-tracks-casing", this.currentLayers.tracks);
    this.setLayerVis("tf-tracks-line", this.currentLayers.tracks);
    this.setLayerVis("tf-tracks-access-warn", this.currentLayers.tracks);
    this.setLayerVis("tf-steep-highlights", this.currentLayers.steepHighlights);
    this.setLayerVis("tf-hotspots-casing", this.currentLayers.hotspots);
    this.setLayerVis("tf-hotspots-line", this.currentLayers.hotspots);
    this.setLayerVis("tf-hotspots-points", this.currentLayers.hotspots);
    this.setLayerVis("tf-closures-polygon-fill", this.currentLayers.closures);
    this.setLayerVis("tf-closures-polygon-outline", this.currentLayers.closures);
    this.setLayerVis("tf-closures-line-casing", this.currentLayers.closures);
    this.setLayerVis("tf-closures-line", this.currentLayers.closures);
    this.setLayerVis("tf-closures-points", this.currentLayers.closures);
    this.setLayerVis("tf-reports-gate-halo", this.currentLayers.reports);
    this.setLayerVis("tf-reports-points", this.currentLayers.reports);
    this.setLayerVis("tf-unmapped-casing", this.currentLayers.unmapped);
    this.setLayerVis("tf-unmapped-line", this.currentLayers.unmapped);
    this.setLayerVis("tf-hillshade", this.currentLayers.contoursHillshade);
    this.setLayerVis(
      "tf-contours-hillshade",
      this.currentLayers.contoursHillshade
    );
    this.setLayerVis("tf-contours-index", this.currentLayers.contoursHillshade);
    this.setLayerVis("tf-contours-labels", this.currentLayers.contoursHillshade);
    this.setLayerVis("tf-snow-line-fill", this.currentLayers.snowLine);
    this.setLayerVis("tf-snow-line-outline", this.currentLayers.snowLine);
    this.setLayerVis("tf-parks-fill", this.currentLayers.parksWaterways);
    this.setLayerVis("tf-parks-outline", this.currentLayers.parksWaterways);
    this.setLayerVis(
      "tf-waterways-perennial",
      this.currentLayers.parksWaterways
    );
    this.setLayerVis(
      "tf-waterways-intermittent",
      this.currentLayers.parksWaterways
    );
    this.setLayerVis("tf-pois-layer", this.currentLayers.pois);
    this.setLayerVis("tf-pois-icons", this.currentLayers.pois);
  }

  private applySnowLineFilter(): void {
    if (!this.currentManifest?.vector_available) return;
    const bands =
      this.currentManifest.snowline_bands_m ?? DEFAULT_SNOWLINE_BANDS_M;
    const filter = buildSnowlineFilter(
      this.currentLayers.snowLineElevationM,
      bands
    );
    if (this.map.getLayer("tf-snow-line-fill")) {
      this.map.setFilter("tf-snow-line-fill", filter);
    }
    if (this.map.getLayer("tf-snow-line-outline")) {
      this.map.setFilter("tf-snow-line-outline", filter);
    }
  }

  private applyFallbackData(): void {
    if (this.fallbackTracks) {
      const tracksSrc = this.map.getSource("tf-tracks") as
        | maplibregl.GeoJSONSource
        | undefined;
      tracksSrc?.setData(this.fallbackTracks);

      const steepFeatures = this.fallbackTracks.features.filter((f) => {
        const props = (f.properties ?? {}) as Record<string, unknown>;
        return props.is_steep_highlight === true;
      });
      const steepSrc = this.map.getSource("tf-steep") as
        | maplibregl.GeoJSONSource
        | undefined;
      steepSrc?.setData({
        type: "FeatureCollection",
        features: steepFeatures,
      });
    }

    if (this.fallbackPois) {
      const poisSrc = this.map.getSource("tf-pois") as
        | maplibregl.GeoJSONSource
        | undefined;
      poisSrc?.setData(poisToGeoJson(this.fallbackPois));
    }
  }

  private applyClosuresData(): void {
    const src = this.map.getSource("tf-closures") as
      | maplibregl.GeoJSONSource
      | undefined;
    src?.setData(this.closuresGeoJson ?? EMPTY_FEATURE_COLLECTION);
  }

  private applyHotspotsData(): void {
    const src = this.map.getSource("tf-hotspots") as
      | maplibregl.GeoJSONSource
      | undefined;
    src?.setData(this.hotspotsGeoJson ?? EMPTY_FEATURE_COLLECTION);
  }

  private applyRecordedTrackData(): void {
    const { liveLineFc, backtrackLineFc, headPointFc } =
      buildRecordedTrackGeoJson(this.recordedPoints, this.backtrackActive);
    const liveSrc = this.map.getSource("tf-recorded-track") as
      | maplibregl.GeoJSONSource
      | undefined;
    const backSrc = this.map.getSource("tf-backtrack-route") as
      | maplibregl.GeoJSONSource
      | undefined;
    const headSrc = this.map.getSource("tf-recorder-head") as
      | maplibregl.GeoJSONSource
      | undefined;
    liveSrc?.setData(liveLineFc);
    backSrc?.setData(backtrackLineFc);
    headSrc?.setData(headPointFc);
  }

  private applyReportsData(): void {
    const src = this.map.getSource("tf-reports") as
      | maplibregl.GeoJSONSource
      | undefined;
    src?.setData(this.reportsGeoJson ?? EMPTY_FEATURE_COLLECTION);
  }

  private applyUnmappedData(): void {
    const src = this.map.getSource("tf-unmapped") as
      | maplibregl.GeoJSONSource
      | undefined;
    src?.setData(this.unmappedGeoJson ?? EMPTY_FEATURE_COLLECTION);
  }

  private applyPlannedRouteData(): void {
    const lineSrc = this.map.getSource("tf-planned-route") as
      | maplibregl.GeoJSONSource
      | undefined;
    const pinsSrc = this.map.getSource("tf-planned-route-pins") as
      | maplibregl.GeoJSONSource
      | undefined;
    lineSrc?.setData(this.plannedRouteLineFc);
    pinsSrc?.setData(this.plannedRoutePinsFc);
  }

  private applyUserPinsData(): void {
    const src = this.map.getSource("tf-user-pins") as
      | maplibregl.GeoJSONSource
      | undefined;
    src?.setData(userPinsToGeoJson(this.userPins));
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
