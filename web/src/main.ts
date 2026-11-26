import "./styles.css";
import { fetchHealth } from "./api";
import { renderAttributionFooter } from "./components/footer";
import { Sidebar } from "./components/sidebar";
import { TopBar } from "./components/topbar";
import {
  formatCoords,
  isWithinVicNswRegion,
  latLonToMgrs,
  parseSearchCoordinate,
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
} from "./types";

const HEALTH_POLL_INTERVAL_MS = 15000;

class TrailForgeApp {
  private readonly shellEl: HTMLElement;
  private readonly topbar: TopBar;
  private readonly sidebar: Sidebar;
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
        onSearchSubmit: (query) => this.handleSearch(query),
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
        },
        onResetRegion: () => {
          this.map.flyToPreset(
            this.activePreset,
            this.sidebarCollapsed ? 16 : 340
          );
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
            "External map tiles unreachable — showing offline topo grid fallback";
          this.offlineBannerEl.classList.remove("hidden");
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
      }
    });

    subscribeNetworkStatus((online) => {
      this.sidebar.updateNetworkStatus(online);
      if (!online) {
        this.setActiveBasemap("offline-topo");
        this.offlineBannerEl.textContent =
          "Offline mode — displaying built-in dark topo graticule & corridor bounds";
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
    window.setInterval(() => void this.refreshHealth(false), HEALTH_POLL_INTERVAL_MS);

    void registerServiceWorker((status) => {
      this.sidebar.updateSwStatus(status);
    });
  }

  private setActiveBasemap(basemap: BasemapId): void {
    this.activeBasemap = basemap;
    this.map.setBasemap(basemap);
    this.basemapButtons.forEach((b) =>
      b.classList.toggle("active", b.dataset.basemap === basemap)
    );
    if (basemap === "offline-topo") {
      this.offlineBannerEl.textContent =
        "Offline Topo Grid active — zero external tile requests";
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

  private handleSearch(query: string): void {
    const parsed = parseSearchCoordinate(query);
    if (parsed) {
      const label =
        parsed.kind === "mgrs" ? parsed.formatted : parsed.canonical;
      this.map.flyToCoordinate(parsed.lat, parsed.lon, label, 13);
      this.updateCursorReadout(parsed.lat, parsed.lon);
      const regionNote = isWithinVicNswRegion(parsed.lat, parsed.lon)
        ? ""
        : " (outside VIC/NSW launch area)";
      this.showToast(`Located ${label}${regionNote}`, "success");
      return;
    }

    // Match built-in VIC/NSW region or landmark presets offline
    const lower = query.toLowerCase();
    const matchedPreset = getPresetById(lower);
    if (matchedPreset) {
      this.selectPreset(matchedPreset.id);
      return;
    }

    this.showToast(
      `Track & POI search ("${query}") arrives in Slice 03 — enter lat,lon or MGRS now.`,
      "info"
    );
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
