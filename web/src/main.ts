import "./styles.css";
import {
  activateVehicle,
  addFavorite,
  createConditionReport,
  createPin,
  createVehicle,
  deletePin,
  deleteTrip,
  deleteVehicle,
  downloadAuthenticatedExport,
  fetchAuthMe,
  fetchClosuresGeoJson,
  fetchClosuresStatus,
  fetchFavorites,
  fetchHealth,
  fetchPins,
  fetchPois,
  fetchReportsGeoJson,
  fetchTileManifest,
  fetchTrackDetail,
  fetchTracksGeoJson,
  fetchTrips,
  fetchUnmappedCandidatesGeoJson,
  fetchVehicles,
  loginAccount,
  logoutAccount,
  registerAccount,
  removeFavorite,
  updateTrip,
  updateVehicle,
  uploadTripGpx,
  uploadTripJson,
  voteConditionReport,
} from "./api";
import {
  RigAccountPanel,
  type AccountPanelTab,
} from "./components/accountPanel";
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
import { buildTripExportUrl, getTripId } from "./tripsHelpers";
import type {
  AuthUser,
  BasemapId,
  HealthState,
  LayerVisibilityState,
  RegionPreset,
  TripItem,
  UnifiedSearchItem,
  UserFavoriteItem,
  UserPinItem,
  VehicleCapabilityClass,
  VehicleProfileItem,
} from "./types";
import {
  VEHICLE_CLASSES,
  buildTrackExportUrl,
  buildUserExportUrl,
  isVehicleActive,
} from "./vehicleHelpers";

const HEALTH_POLL_INTERVAL_MS = 15000;
const CLOSURES_POLL_INTERVAL_MS = 60000;
const ANON_RIG_STORAGE_KEY = "trailforge.anon_rig_class";

function loadStoredAnonRig(): VehicleCapabilityClass {
  try {
    const raw = window.localStorage.getItem(ANON_RIG_STORAGE_KEY);
    if (raw && (VEHICLE_CLASSES as readonly string[]).includes(raw)) {
      return raw as VehicleCapabilityClass;
    }
  } catch {
    // Ignore storage errors
  }
  return "Stock 4WD";
}

function saveStoredAnonRig(cls: VehicleCapabilityClass): void {
  try {
    window.localStorage.setItem(ANON_RIG_STORAGE_KEY, cls);
  } catch {
    // Ignore storage errors
  }
}

class TrailForgeApp {
  private readonly shellEl: HTMLElement;
  private readonly topbar: TopBar;
  private readonly sidebar: Sidebar;
  private readonly inspector: TrackInspectorDrawer;
  private readonly accountPanel: RigAccountPanel;
  private readonly map: TrailForgeMap;
  private readonly collapseBtnEl: HTMLButtonElement;
  private readonly cursorCoordsEl: HTMLElement;
  private readonly cursorMgrsEl: HTMLElement;
  private readonly offlineBannerEl: HTMLElement;
  private readonly pinDropBannerEl: HTMLElement;
  private readonly toastContainerEl: HTMLElement;
  private readonly basemapButtons: NodeListOf<HTMLButtonElement>;

  private sidebarCollapsed = false;
  private activePreset: RegionPreset = getDefaultPreset();
  private activeBasemap: BasemapId;
  private layerState: LayerVisibilityState = createInitialLayerState();
  private healthAbort: AbortController | null = null;
  private inspectorAbort: AbortController | null = null;

  // Slice 05 + 06 + 08 User, Vehicle, Trips, Favorites, Pins & Report Picker state
  private user: AuthUser | null = null;
  private anonymousRigClass: VehicleCapabilityClass = loadStoredAnonRig();
  private vehicles: VehicleProfileItem[] = [];
  private trips: TripItem[] = [];
  private latestUploadedTrip: TripItem | null = null;
  private favorites: UserFavoriteItem[] = [];
  private pins: UserPinItem[] = [];
  private coordPickMode: "pin" | "report" | null = null;
  private pendingPinCoords: { lat: number; lon: number } | null = null;
  private pickedReportCoords: { lat: number; lon: number } | null = null;

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
          <aside data-role="account-panel"></aside>

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

          <div class="tf-pin-drop-banner hidden" data-role="pin-drop-banner" role="status">
            ⌖ Click anywhere on the map to select coordinates · Esc to cancel
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
    this.pinDropBannerEl = this.q(root, "[data-role='pin-drop-banner']");
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
        onFeedStatusClick: () => {
          if (this.sidebarCollapsed) {
            this.collapseBtnEl.click();
          }
          this.sidebar.openFeedsSection();
          void this.refreshClosures(true);
        },
        onRigAccountClick: () => {
          if (this.accountPanel.isPanelOpen()) {
            this.closeAccountPanel();
          } else {
            this.openAccountPanel("rig");
          }
        },
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
          if (key === "unmapped" && enabled) {
            void this.refreshUnmappedCandidates();
          }
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
          this.pickedReportCoords = null;
        },
        onZoomToTrack: (bbox) => {
          this.map.fitBBox(bbox, this.sidebarCollapsed ? 24 : 360, 420);
        },
        onPoiSelect: (poi) => {
          this.map.flyToCoordinate(poi.lat, poi.lon, poi.name, 14);
          this.updateCursorReadout(poi.lat, poi.lon);
          this.showToast(`Located ${poi.name}`, "info");
        },
        onOpenRigPanel: () => {
          this.openAccountPanel("rig");
        },
        onRequireSignIn: () => {
          this.showToast("Sign in to submit or vote on trail condition reports", "info");
          this.openAccountPanel("account");
        },
        onToggleFavorite: (trackId, currentlyFav) => {
          void this.handleToggleFavorite(trackId, currentlyFav);
        },
        onExportTrack: (trackId, fmt) => {
          void this.handleExportTrack(trackId, fmt);
        },
        onPickReportLocation: () => {
          this.setCoordPickMode("report");
          this.shellEl.classList.remove("inspector-open");
        },
        onSubmitReport: async (input) => {
          await createConditionReport(input);
          this.pickedReportCoords = null;
          this.showToast("Trail condition report published", "success");
          await this.refreshReportsGeoJson();
          this.refreshOpenInspectorForRig();
        },
        onVoteReport: async (reportId, vote) => {
          try {
            await voteConditionReport(reportId, vote);
            this.showToast(
              vote === "confirm"
                ? "Report confirmed — freshness refreshed"
                : "Marked hazard resolved",
              "success"
            );
            await this.refreshReportsGeoJson();
            this.refreshOpenInspectorForRig();
          } catch (err) {
            const msg = err instanceof Error ? err.message : "Vote failed";
            this.showToast(msg, "error");
          }
        },
      }
    );

    this.accountPanel = new RigAccountPanel(
      this.q(root, "[data-role='account-panel']"),
      this.buildAccountPanelState(),
      {
        onClose: () => this.closeAccountPanel(),
        onSelectAnonymousRig: (cls) => {
          this.anonymousRigClass = cls;
          saveStoredAnonRig(cls);
          this.syncRigUiState();
          this.showToast(`Rig suitability preview set to ${cls}`, "info");
          this.refreshOpenInspectorForRig();
        },
        onLogin: async (email, password) => {
          const res = await loginAccount({ email, password });
          this.user = res.user;
          await this.loadSignedInUserData();
          this.showToast(`Signed in as ${res.user.display_name}`, "success");
          this.refreshOpenInspectorForRig();
        },
        onRegister: async (displayName, email, password) => {
          const res = await registerAccount({
            display_name: displayName,
            email,
            password,
          });
          this.user = res.user;
          await this.loadSignedInUserData();
          this.showToast(`Account created for ${res.user.display_name}`, "success");
          this.refreshOpenInspectorForRig();
        },
        onLogout: async () => {
          await logoutAccount();
          this.user = null;
          this.vehicles = [];
          this.trips = [];
          this.latestUploadedTrip = null;
          this.favorites = [];
          this.pins = [];
          this.setCoordPickMode(null);
          this.map.setUserPins([]);
          this.syncRigUiState();
          this.showToast("Signed out — anonymous browsing active", "info");
          this.refreshOpenInspectorForRig();
        },
        onSaveVehicle: async (payload, editingId) => {
          if (editingId) {
            await updateVehicle(editingId, payload);
            this.showToast(`Updated ${payload.name}`, "success");
          } else {
            await createVehicle(payload);
            this.showToast(`Saved vehicle profile ${payload.name}`, "success");
          }
          await this.reloadVehicles();
          this.refreshOpenInspectorForRig();
        },
        onActivateVehicle: async (id) => {
          const updated = await activateVehicle(id);
          await this.reloadVehicles();
          this.showToast(
            `Active rig: ${updated.name} (${updated.capability_class})`,
            "success"
          );
          this.refreshOpenInspectorForRig();
        },
        onDeleteVehicle: async (id) => {
          await deleteVehicle(id);
          await this.reloadVehicles();
          this.showToast("Vehicle profile deleted", "info");
          this.refreshOpenInspectorForRig();
        },
        onUploadTripGpx: async (file, params) => {
          const uploaded = await uploadTripGpx(file, params);
          this.latestUploadedTrip = uploaded;
          await this.reloadTrips();
          void this.refreshUnmappedCandidates();
          this.showToast(
            `Matched ${uploaded.matched_segment_count ?? 0} segments (${uploaded.observations_published ?? 0} published)`,
            "success"
          );
        },
        onUploadTripJson: async (payload) => {
          const uploaded = await uploadTripJson(payload);
          this.latestUploadedTrip = uploaded;
          await this.reloadTrips();
          void this.refreshUnmappedCandidates();
          this.showToast(
            `Matched ${uploaded.matched_segment_count ?? 0} segments`,
            "success"
          );
        },
        onToggleTripVisibility: async (tripId, nextVis) => {
          await updateTrip(tripId, { visibility: nextVis });
          await this.reloadTrips();
          void this.refreshUnmappedCandidates();
          this.showToast(
            nextVis === "private"
              ? "Trip set to Private — anonymized observations revoked"
              : "Trip shared as Anonymous Community telemetry",
            "info"
          );
        },
        onExportTrip: async (tripId, fmt) => {
          const url = buildTripExportUrl(tripId, fmt);
          await downloadAuthenticatedExport(
            url,
            `trip_${tripId}.${fmt === "gpx" ? "gpx" : "geojson"}`
          );
          this.showToast(`Exported trip (${fmt.toUpperCase()})`, "success");
        },
        onDeleteTrip: async (tripId) => {
          await deleteTrip(tripId);
          this.trips = this.trips.filter((t) => getTripId(t) !== tripId);
          if (
            this.latestUploadedTrip &&
            getTripId(this.latestUploadedTrip) === tripId
          ) {
            this.latestUploadedTrip = null;
          }
          this.syncRigUiState();
          void this.refreshUnmappedCandidates();
          this.showToast("Trip deleted & any public observations revoked", "info");
        },
        onOpenFavoriteTrack: (trackId) => {
          this.closeAccountPanel();
          void this.openTrackInspector(trackId);
        },
        onRemoveFavorite: async (trackId) => {
          await removeFavorite(trackId);
          this.favorites = this.favorites.filter((f) => f.track_id !== trackId);
          this.syncRigUiState();
          this.showToast("Removed track from favorites", "info");
        },
        onTogglePinDropMode: (active) => {
          this.setCoordPickMode(active ? "pin" : null);
          if (active) {
            this.shellEl.classList.remove("account-open");
          }
        },
        onCreatePin: async (payload) => {
          const created = await createPin(payload);
          this.pins = [created, ...this.pins];
          this.pendingPinCoords = null;
          this.setCoordPickMode(null);
          this.map.setUserPins(this.pins);
          this.syncRigUiState();
          this.showToast(
            `Dropped pin "${created.name ?? created.title ?? payload.name}"`,
            "success"
          );
        },
        onSelectPin: (pin) => {
          this.map.flyToCoordinate(
            pin.lat,
            pin.lon,
            pin.name ?? pin.title ?? "Pin",
            14
          );
          this.updateCursorReadout(pin.lat, pin.lon);
        },
        onDeletePin: async (id) => {
          await deletePin(id);
          this.pins = this.pins.filter((p) => p.id !== id);
          this.map.setUserPins(this.pins);
          this.syncRigUiState();
          this.showToast("Personal pin deleted", "info");
        },
        onExportUser: async (fmt) => {
          const url = buildUserExportUrl(fmt);
          await downloadAuthenticatedExport(
            url,
            `trailforge_saved.${fmt === "gpx" ? "gpx" : "geojson"}`
          );
          this.showToast(
            `Exported saved tracks & pins (${fmt.toUpperCase()})`,
            "success"
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
            "External basemap tiles unreachable — rendering corridor PMTiles & offline topo grid";
          this.offlineBannerEl.classList.remove("hidden");
        },
        onTrackClick: (trackId) => {
          this.closeAccountPanel();
          void this.openTrackInspector(trackId);
        },
        onPinDropClick: (lat, lon) => {
          this.pendingPinCoords = { lat, lon };
          this.setCoordPickMode(null);
          this.openAccountPanel("saved");
          this.showToast(
            `Pin location picked (${lat.toFixed(4)}, ${lon.toFixed(4)})`,
            "info"
          );
        },
        onReportPickClick: (lat, lon) => {
          this.pickedReportCoords = { lat, lon };
          this.setCoordPickMode(null);
          this.shellEl.classList.add("inspector-open");
          this.inspector.updateContext({ pickedReportCoords: { lat, lon } });
          this.showToast(
            `Report location set (${lat.toFixed(4)}, ${lon.toFixed(4)})`,
            "info"
          );
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
    this.syncRigUiState();
    this.bootstrap();
  }

  private getActiveRigInfo(): {
    vehicleClass: VehicleCapabilityClass;
    vehicleName: string | null;
  } {
    if (this.user && this.vehicles.length > 0) {
      const active =
        this.vehicles.find((v) => isVehicleActive(v)) ?? this.vehicles[0]!;
      return {
        vehicleClass: active.capability_class,
        vehicleName: active.name,
      };
    }
    return {
      vehicleClass: this.anonymousRigClass,
      vehicleName: null,
    };
  }

  private buildAccountPanelState() {
    return {
      user: this.user,
      anonymousRigClass: this.anonymousRigClass,
      vehicles: this.vehicles,
      trips: this.trips,
      latestUploadedTrip: this.latestUploadedTrip,
      favorites: this.favorites,
      pins: this.pins,
      pinDropActive: this.coordPickMode === "pin",
      pendingPinCoords: this.pendingPinCoords,
    };
  }

  private syncRigUiState(): void {
    const rig = this.getActiveRigInfo();
    this.topbar.updateRigSummary(
      rig.vehicleClass,
      rig.vehicleName,
      this.user?.display_name ?? null,
      this.favorites.length + this.pins.length
    );
    this.accountPanel.updateState(this.buildAccountPanelState());

    const openTrackId = this.inspector.getCurrentTrackId();
    this.inspector.updateContext({
      snowLineElevationM: this.layerState.snowLineElevationM,
      activeVehicleClass: rig.vehicleClass,
      activeVehicleName: rig.vehicleName,
      isFavorite: Boolean(
        openTrackId && this.favorites.some((f) => f.track_id === openTrackId)
      ),
      isAuthenticated: Boolean(this.user),
      pickedReportCoords: this.pickedReportCoords,
    });
  }

  private openAccountPanel(tab?: AccountPanelTab): void {
    if (this.inspector.openState()) {
      this.inspector.close();
      this.shellEl.classList.remove("inspector-open");
    }
    this.accountPanel.updateState(this.buildAccountPanelState());
    this.accountPanel.open(tab);
    this.shellEl.classList.add("account-open");
  }

  private closeAccountPanel(): void {
    this.accountPanel.close();
    this.shellEl.classList.remove("account-open");
  }

  private setCoordPickMode(mode: "pin" | "report" | null): void {
    this.coordPickMode = mode;
    this.map.setCoordPickMode(mode);
    if (mode === "pin") {
      this.pinDropBannerEl.textContent =
        "⌖ Pin drop mode — click anywhere on the map to place a personal pin · Esc to cancel";
      this.pinDropBannerEl.classList.remove("hidden");
    } else if (mode === "report") {
      this.pinDropBannerEl.textContent =
        "⌖ Report location mode — click the exact hazard or gate spot on the map · Esc to cancel";
      this.pinDropBannerEl.classList.remove("hidden");
    } else {
      this.pinDropBannerEl.classList.add("hidden");
    }
    this.accountPanel.updateState(this.buildAccountPanelState());
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
      } else if (e.key === "Escape") {
        if (this.coordPickMode === "pin") {
          this.setCoordPickMode(null);
          this.openAccountPanel("saved");
        } else if (this.coordPickMode === "report") {
          this.setCoordPickMode(null);
          if (this.inspector.openState()) {
            this.shellEl.classList.add("inspector-open");
          }
        } else if (this.accountPanel.isPanelOpen()) {
          this.closeAccountPanel();
        } else if (this.inspector.openState()) {
          this.inspector.close();
          this.shellEl.classList.remove("inspector-open");
          this.map.setSelectedTrackId(null);
        }
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
        void this.refreshClosures(false);
        void this.refreshReportsGeoJson();
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
    void this.refreshClosures(false);
    void this.refreshReportsGeoJson();
    void this.refreshUnmappedCandidates();
    void this.hydrateAuthSession();

    window.setInterval(
      () => void this.refreshHealth(false),
      HEALTH_POLL_INTERVAL_MS
    );
    window.setInterval(
      () => void this.refreshClosures(false),
      CLOSURES_POLL_INTERVAL_MS
    );

    void registerServiceWorker((status) => {
      this.sidebar.updateSwStatus(status);
    });
  }

  private async hydrateAuthSession(): Promise<void> {
    try {
      const me = await fetchAuthMe();
      if (me.authenticated && me.user) {
        this.user = me.user;
        await this.loadSignedInUserData();
      } else {
        this.user = null;
        this.syncRigUiState();
      }
    } catch {
      this.syncRigUiState();
    }
  }

  private async loadSignedInUserData(): Promise<void> {
    try {
      const [vehicles, trips, favorites, pins] = await Promise.all([
        fetchVehicles().catch(() => []),
        fetchTrips().catch(() => []),
        fetchFavorites().catch(() => []),
        fetchPins().catch(() => []),
      ]);
      this.vehicles = vehicles;
      this.trips = trips;
      this.favorites = favorites;
      this.pins = pins;
      this.map.setUserPins(pins);
    } finally {
      this.syncRigUiState();
    }
  }

  private async reloadVehicles(): Promise<void> {
    this.vehicles = await fetchVehicles();
    this.syncRigUiState();
  }

  private async reloadTrips(): Promise<void> {
    this.trips = await fetchTrips();
    this.syncRigUiState();
  }

  private async refreshReportsGeoJson(): Promise<void> {
    try {
      const fc = await fetchReportsGeoJson();
      this.map.setReportsGeoJson(fc);
    } catch {
      // Reports layer stays empty gracefully offline
    }
  }

  private async refreshUnmappedCandidates(): Promise<void> {
    try {
      const fc = await fetchUnmappedCandidatesGeoJson();
      this.map.setUnmappedGeoJson(fc);
    } catch {
      // Unmapped spurs layer stays empty gracefully offline
    }
  }

  private async refreshClosures(announce: boolean): Promise<void> {
    try {
      const [status, geojson] = await Promise.all([
        fetchClosuresStatus(),
        fetchClosuresGeoJson().catch(() => null),
      ]);
      this.topbar.updateFeedStatus(status);
      this.sidebar.updateFeedStatus(status);
      if (geojson) {
        this.map.setClosuresGeoJson(geojson);
      }
      if (announce) {
        this.showToast(
          `Closure feeds status: ${status.overall_status.toUpperCase()}`,
          status.overall_status === "healthy" ? "success" : "info"
        );
      }
    } catch {
      this.topbar.updateFeedStatus(null);
      this.sidebar.updateFeedStatus(null);
    }
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

  private refreshOpenInspectorForRig(): void {
    const trackId = this.inspector.getCurrentTrackId();
    if (trackId && this.inspector.openState()) {
      void this.openTrackInspector(trackId);
    }
  }

  private async openTrackInspector(
    trackId: string,
    trackLabel?: string
  ): Promise<void> {
    this.inspectorAbort?.abort();
    const controller = new AbortController();
    this.inspectorAbort = controller;

    this.closeAccountPanel();
    this.shellEl.classList.add("inspector-open");
    this.map.setSelectedTrackId(trackId);
    this.inspector.showLoading(trackLabel);

    const rig = this.getActiveRigInfo();
    try {
      const queryVehicleClass =
        this.user && this.vehicles.length > 0 ? null : rig.vehicleClass;
      const detail = await fetchTrackDetail(trackId, {
        vehicleClass: queryVehicleClass,
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;

      this.inspector.showTrack(detail, {
        snowLineElevationM: this.layerState.snowLineElevationM,
        activeVehicleClass: rig.vehicleClass,
        activeVehicleName: rig.vehicleName,
        isFavorite: this.favorites.some((f) => f.track_id === trackId),
        isAuthenticated: Boolean(this.user),
        pickedReportCoords: this.pickedReportCoords,
      });
    } catch (err) {
      if (controller.signal.aborted) return;
      const msg =
        err instanceof Error ? err.message : "Failed to fetch track details";
      this.inspector.showError(msg);
    }
  }

  private async handleToggleFavorite(
    trackId: string,
    currentlyFavorite: boolean
  ): Promise<void> {
    if (!this.user) {
      this.showToast("Sign in to save favorite tracks across sessions", "info");
      this.openAccountPanel("account");
      return;
    }

    try {
      if (currentlyFavorite) {
        await removeFavorite(trackId);
        this.favorites = this.favorites.filter((f) => f.track_id !== trackId);
        this.showToast("Removed track from favorites", "info");
      } else {
        const added = await addFavorite(trackId);
        this.favorites = [added, ...this.favorites];
        this.showToast("Saved track to favorites", "success");
      }
      this.syncRigUiState();
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "Failed to update favorite";
      this.showToast(msg, "error");
    }
  }

  private async handleExportTrack(
    trackId: string,
    format: "gpx" | "geojson"
  ): Promise<void> {
    try {
      const url = buildTrackExportUrl(trackId, format);
      await downloadAuthenticatedExport(
        url,
        `${trackId}.${format === "gpx" ? "gpx" : "geojson"}`
      );
      this.showToast(`Downloaded track ${format.toUpperCase()}`, "success");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Export failed";
      this.showToast(msg, "error");
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
