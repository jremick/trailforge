import "./styles.css";
import {
  fetchHealth,
  fetchPois,
  fetchTileManifest,
  fetchTrackDetail,
  fetchTracksGeoJson,
} from "./api";
import { renderAttributionFooter } from "./components/footer";
import { TrackInspectorDrawer } from "./components/inspector";
import { Sidebar } from "./components/sidebar";
import { TopBar } from "./components/topbar";
import {
  formatCoords,
  isWithinVicNswRegion,
  latLonToMgrs,
} from "./coords";
import { TrailForgeMap } from "./map";
import {
  BASEMAP_OPTIONS,
  DEFAULT_PRESET_ID,
  createInitialLayerState,
  escapeHtml,
  getDefaultPreset,
  getPresetById,
} from "./presets";
import { registerServiceWorker, subscribeNetworkStatus } from "./pwa";
import type {
  BasemapId,
  HealthState,
  LayerVisibilityState,
  RegionPreset,
  UnifiedSearchItem,
} from "./types";

const HEALTH_POLL_INTERVAL_MS = 15000;

class TrailForgeApp {
  private readonly shellEl: HTMLElement;
  private readonly topbar: TopBar;
  private readonly sidebar: Sidebar;
  private readonly inspector: TrackInspectorDrawer;
  private readonly map: TrailForgeMap;
  private readonly collapseBtnEl: HTMLButtonElement;
  private readonly cursorCoordsEl: HTMLElement;
  private readonly cursorMgrsEl: HTMLElement;
  private readonly offlineBannerEl: HTMLElement;
  private readonly toastContainerEl: HTMLElement;
  private readonly basemapButtons: NodeListOf<HTMLButtonElement>;

  private sidebarCollapsed = false;
  private activePreset: RegionPreset = getDefaultPreset();
  private activeBasemap: BasemapId;
  private layerState: LayerVisibilityState = createInitialLayerState();
  private healthAbort: AbortController | null = null;
  private inspectorAbort: AbortController | null = null;

  constructor(root: HTMLElement) {
    const initiallyOnline =
      typeof navigator === "undefined" ? true : navigator.onLine;
    this.activeBasemap = initiallyOnline ? "opentopo" : "offline-topo";

    // Start collapsed on narrow mobile screens so the map is immediately prominent
    this.sidebarCollapsed =
      typeof window !== "undefined" && window.innerWidth <= 768;

    root.innerHTML = `
      <div class="tf-shell ${this.sidebarCollapsed ? "sidebar-collapsed" : ""}" data-role="shell">
        <header data-role="topbar"></header>

        <main class="tf-workspace">
          <div class="tf-map-container" data-role="map"></div>

          <button
            type="button"
            class="tf-sidebar-toggle"
            data-role="sidebar-toggle"
            title="Toggle layers sidebar"
            aria-label="Toggle layers sidebar"
            aria-expanded="${!this.sidebarCollapsed}"
          >${this.sidebarCollapsed ? "▸" : "◂"}</button>

          <aside data-role="sidebar"></aside>
          <aside data-role="inspector"></aside>

          <div class="tf-map-toolbar">
            <div class="tf-basemap-switcher" role="group" aria-label="Basemap switcher">
              ${BASEMAP_OPTIONS.map(
                (b) =>
                  `<button type="button" class="tf-basemap-btn ${b.id === this.activeBasemap ? "active" : ""}" data-basemap="${escapeHtml(b.id)}" title="${escapeHtml(b.hint)}">${escapeHtml(b.label)}</button>`
              ).join("")}
            </div>
            <div class="tf-map-actions" role="group" aria-label="Map navigation controls">
              <button type="button" class="tf-map-icon-btn" data-map-action="zoom-in" title="Zoom in" aria-label="Zoom in">+</button>
              <button type="button" class="tf-map-icon-btn" data-map-action="zoom-out" title="Zoom out" aria-label="Zoom out">−</button>
              <button type="button" class="tf-map-icon-btn" data-map-action="north" title="Reset north" aria-label="Reset north">N</button>
            </div>
          </div>

          <div class="tf-offline-banner ${initiallyOnline ? "hidden" : ""}" data-role="offline-banner" role="status">
            Offline mode — displaying built-in dark topo graticule &amp; corridor bounds
          </div>

          <div class="tf-coord-hud" data-role="coord-hud">
            <span class="mono" data-role="cursor-coords">${formatCoords(this.activePreset.center[1], this.activePreset.center[0], 4)}</span>
            <span class="tf-hud-sep" aria-hidden="true">|</span>
            <span class="mono tf-accent-cyan" data-role="cursor-mgrs">${latLonToMgrs(this.activePreset.center[1], this.activePreset.center[0], 4)}</span>
          </div>
        </main>

        <footer data-role="footer"></footer>
        <div class="tf-toast-container" data-role="toasts" aria-live="polite"></div>
      </div>
    `;

    this.shellEl = this.q(root, "[data-role='shell']");
    this.collapseBtnEl = this.q<HTMLButtonElement>(
      root,
      "[data-role='sidebar-toggle']"
    );
    this.cursorCoordsEl = this.q(root, "[data-role='cursor-coords']");
    this.cursorMgrsEl = this.q(root, "[data-role='cursor-mgrs']");
    this.offlineBannerEl = this.q(root, "[data-role='offline-banner']");
    this.toastContainerEl = this.q(root, "[data-role='toasts']");
    this.basemapButtons = root.querySelectorAll<HTMLButtonElement>("[data-basemap]");

    this.topbar = new TopBar(
      this.q(root, "[data-role='topbar']"),
      DEFAULT_PRESET_ID,
      {
        onSearchSelect: (item) => this.handleSearchSelect(item),
        onSearchNoMatch: (query, remoteError) => {
          this.showToast(
            remoteError
              ? `Offline search: no local match for "${query}". Try lat,lon or MGRS.`
              : `No tracks, POIs, or parks found for "${query}".`,
            "info"
          );
        },
        onSearchClear: () => this.map.clearSearchPin(),
        onPresetChange: (presetId) => this.selectPreset(presetId),
        onHealthClick: () => void this.refreshHealth(true),
      }
    );

    this.sidebar = new Sidebar(
      this.q(root, "[data-role='sidebar']"),
      this.layerState,
      this.activePreset,
      {
        onLayerToggle: (key, enabled) => {
          this.layerState = { ...this.layerState, [key]: enabled };
          this.map.setLayerVisibility(this.layerState);
        },
        onSnowLineElevationChange: (elevationM) => {
          this.layerState = { ...this.layerState, snowLineElevationM: elevationM };
          this.map.setLayerVisibility(this.layerState);
          this.inspector.setSnowLineElevation(elevationM);
        },
        onResetRegion: () => {
          this.map.flyToPreset(
            this.activePreset,
            this.sidebarCollapsed ? 16 : 340
          );
        },
      }
    );

    this.inspector = new TrackInspectorDrawer(
      this.q(root, "[data-role='inspector']"),
      {
        onClose: () => {
          this.shellEl.classList.remove("inspector-open");
          this.map.setSelectedTrackId(null);
        },
        onZoomToTrack: (bbox) => {
          this.map.fitBBox(bbox, this.sidebarCollapsed ? 24 : 360, 420);
        },
        onPoiSelect: (poi) => {
          this.map.flyToCoordinate(poi.lat, poi.lon, poi.name, 14);
          this.updateCursorReadout(poi.lat, poi.lon);
          this.showToast(`Located ${poi.name}`, "info");
        },
      }
    );

    renderAttributionFooter(this.q(root, "[data-role='footer']"));

    this.map = new TrailForgeMap(
      this.q(root, "[data-role='map']"),
      {
        onCursorMove: (lat, lon) => this.updateCursorReadout(lat, lon),
        onBasemapTileError: () => {
          this.offlineBannerEl.textContent =
            "External basemap tiles unreachable — rendering corridor PMTiles & offline topo grid";
          this.offlineBannerEl.classList.remove("hidden");
        },
        onTrackClick: (trackId) => {
          void this.openTrackInspector(trackId);
        },
      },
      {
        initialCenter: this.activePreset.center,
        initialZoom: this.activePreset.zoom,
        initialBasemap: this.activeBasemap,
        initialLayers: this.layerState,
      }
    );

    this.bindEvents(root);
    this.sidebar.updateNetworkStatus(initiallyOnline);
    this.bootstrap();
  }

  private bindEvents(root: HTMLElement): void {
    this.collapseBtnEl.addEventListener("click", () => {
      this.sidebarCollapsed = !this.sidebarCollapsed;
      this.shellEl.classList.toggle("sidebar-collapsed", this.sidebarCollapsed);
      this.collapseBtnEl.textContent = this.sidebarCollapsed ? "▸" : "◂";
      this.collapseBtnEl.setAttribute(
        "aria-expanded",
        String(!this.sidebarCollapsed)
      );
      window.setTimeout(() => this.map.resize(), 220);
    });

    this.basemapButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = btn.dataset.basemap as BasemapId;
        this.setActiveBasemap(id);
      });
    });

    root
      .querySelectorAll<HTMLButtonElement>("[data-map-action]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const action = btn.dataset.mapAction;
          if (action === "zoom-in") this.map.zoomIn();
          else if (action === "zoom-out") this.map.zoomOut();
          else if (action === "north") this.map.resetNorth();
        });
      });

    window.addEventListener("keydown", (e) => {
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName;
      const inInput =
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        tag === "SELECT" ||
        Boolean(target?.isContentEditable);

      if (e.key === "/" && !inInput) {
        e.preventDefault();
        this.topbar.focusSearch();
      } else if (e.key === "Escape" && this.inspector.openState()) {
        this.inspector.close();
        this.shellEl.classList.remove("inspector-open");
        this.map.setSelectedTrackId(null);
      }
    });

    subscribeNetworkStatus((online) => {
      this.sidebar.updateNetworkStatus(online);
      if (!online) {
        this.setActiveBasemap("offline-topo");
        this.offlineBannerEl.textContent =
          "Offline mode — displaying corridor PMTiles & built-in topo grid";
        this.offlineBannerEl.classList.remove("hidden");
      } else {
        this.offlineBannerEl.classList.add("hidden");
        void this.refreshHealth(false);
      }
    });
  }

  private bootstrap(): void {
    const initialHealth: HealthState = {
      kind: "connecting",
      data: null,
      error: null,
      checkedAt: null,
    };
    this.topbar.updateHealth(initialHealth);
    this.sidebar.updateHealth(initialHealth);

    void this.refreshHealth(false);
    void this.loadVectorAndTerrainLayers();

    window.setInterval(
      () => void this.refreshHealth(false),
      HEALTH_POLL_INTERVAL_MS
    );

    void registerServiceWorker((status) => {
      this.sidebar.updateSwStatus(status);
    });
  }

  private async loadVectorAndTerrainLayers(): Promise<void> {
    this.sidebar.updateTileMode("loading");
    try {
      const manifest = await fetchTileManifest();
      if (manifest.snowline_bands_m && manifest.snowline_bands_m.length > 0) {
        this.sidebar.setSnowLineBands(
          manifest.snowline_bands_m,
          this.layerState.snowLineElevationM
        );
      }

      if (manifest.vector_available || manifest.terrain_available) {
        this.map.applyTileManifest(manifest);
        const modeLabel =
          manifest.vector_available && manifest.terrain_available
            ? "PMTiles (Vector + DEM)"
            : manifest.vector_available
              ? "PMTiles (Vector)"
              : "PMTiles (Terrain) + GeoJSON";
        this.sidebar.updateTileMode("pmtiles", modeLabel);

        // If terrain is present but vector PMTiles missing, hydrate tracks/POIs via GeoJSON
        if (!manifest.vector_available) {
          await this.loadGeoJsonFallback();
        }
        return;
      }
    } catch {
      // Manifest unavailable — fall through to GeoJSON fallback cleanly
    }

    await this.loadGeoJsonFallback();
  }

  private async loadGeoJsonFallback(): Promise<void> {
    try {
      const [tracksFc, pois] = await Promise.all([
        fetchTracksGeoJson(),
        fetchPois(),
      ]);
      this.map.setFallbackData(tracksFc, pois);
      this.sidebar.updateTileMode(
        "geojson-fallback",
        `GeoJSON (${tracksFc.features.length} segs)`
      );
    } catch {
      this.sidebar.updateTileMode("offline", "Offline graticule shell");
    }
  }

  private async openTrackInspector(
    trackId: string,
    trackLabel?: string
  ): Promise<void> {
    this.inspectorAbort?.abort();
    const controller = new AbortController();
    this.inspectorAbort = controller;

    this.shellEl.classList.add("inspector-open");
    this.map.setSelectedTrackId(trackId);
    this.inspector.showLoading(trackLabel);

    try {
      const detail = await fetchTrackDetail(trackId, controller.signal);
      if (controller.signal.aborted) return;
      this.inspector.showTrack(detail, this.layerState.snowLineElevationM);
    } catch (err) {
      if (controller.signal.aborted) return;
      const msg =
        err instanceof Error ? err.message : "Failed to fetch track details";
      this.inspector.showError(msg);
    }
  }

  private handleSearchSelect(item: UnifiedSearchItem): void {
    if (item.kind === "preset" && item.presetId) {
      this.selectPreset(item.presetId);
      return;
    }

    if (item.kind === "track") {
      if (item.bbox) {
        this.map.fitBBox(item.bbox, this.sidebarCollapsed ? 24 : 360, 420);
      } else if (item.lat !== null && item.lon !== null) {
        this.map.flyToCoordinate(item.lat, item.lon, item.title, 13.2);
      }
      void this.openTrackInspector(item.id, item.title);
      return;
    }

    if (item.lat !== null && item.lon !== null) {
      this.map.flyToCoordinate(
        item.lat,
        item.lon,
        item.title,
        item.kind === "park" ? 11.5 : 13.8
      );
      this.updateCursorReadout(item.lat, item.lon);
      const regionNote = isWithinVicNswRegion(item.lat, item.lon)
        ? ""
        : " (outside VIC/NSW launch area)";
      this.showToast(`Located ${item.title}${regionNote}`, "success");
    }
  }

  private setActiveBasemap(basemap: BasemapId): void {
    this.activeBasemap = basemap;
    this.map.setBasemap(basemap);
    this.basemapButtons.forEach((b) =>
      b.classList.toggle("active", b.dataset.basemap === basemap)
    );
    if (basemap === "offline-topo") {
      this.offlineBannerEl.textContent =
        "Offline Topo active — zero external raster tile requests";
      this.offlineBannerEl.classList.remove("hidden");
    } else if (typeof navigator === "undefined" || navigator.onLine) {
      this.offlineBannerEl.classList.add("hidden");
    }
  }

  private selectPreset(presetId: string): void {
    const preset = getPresetById(presetId);
    if (!preset) return;
    this.activePreset = preset;
    this.topbar.setSelectedPreset(preset.id);
    this.sidebar.updatePreset(preset);
    this.map.flyToPreset(preset, this.sidebarCollapsed ? 16 : 340);
    this.showToast(`Centered on ${preset.shortLabel}`, "info");
  }

  private updateCursorReadout(lat: number, lon: number): void {
    this.cursorCoordsEl.textContent = formatCoords(lat, lon, 4);
    try {
      this.cursorMgrsEl.textContent = latLonToMgrs(lat, lon, 4);
    } catch {
      this.cursorMgrsEl.textContent = "—";
    }
  }

  private async refreshHealth(announce: boolean): Promise<void> {
    this.healthAbort?.abort();
    const controller = new AbortController();
    this.healthAbort = controller;

    try {
      const data = await fetchHealth(controller.signal);
      if (controller.signal.aborted) return;

      const isHealthy =
        (data.status === "ok" || data.status === "healthy") &&
        data.db_ok !== false;

      const state: HealthState = {
        kind: isHealthy ? "online" : "degraded",
        data,
        error: isHealthy ? null : "Database health check reported degraded",
        checkedAt: Date.now(),
      };
      this.topbar.updateHealth(state);
      this.sidebar.updateHealth(state);

      if (announce) {
        this.showToast(
          isHealthy
            ? `Backend healthy (PostGIS ${data.postgis_version ?? "ready"})`
            : "Backend reachable but database degraded",
          isHealthy ? "success" : "error"
        );
      }
    } catch (err) {
      if (controller.signal.aborted) return;
      const message =
        err instanceof Error ? err.message : "Unable to reach /api/health";
      const state: HealthState = {
        kind: "offline",
        data: null,
        error: message,
        checkedAt: Date.now(),
      };
      this.topbar.updateHealth(state);
      this.sidebar.updateHealth(state);

      if (announce) {
        this.showToast(`API offline: ${message}`, "error");
      }
    }
  }

  private showToast(
    message: string,
    kind: "info" | "success" | "error"
  ): void {
    const el = document.createElement("div");
    el.className = `tf-toast ${kind}`;
    el.textContent = message;
    this.toastContainerEl.appendChild(el);
    window.setTimeout(() => {
      el.remove();
    }, 3600);
  }

  private q<T extends HTMLElement = HTMLElement>(
    root: ParentNode,
    selector: string
  ): T {
    const el = root.querySelector<T>(selector);
    if (!el) throw new Error(`App element not found: ${selector}`);
    return el;
  }
}

const appRoot = document.getElementById("app");
if (appRoot) {
  new TrailForgeApp(appRoot);
}
