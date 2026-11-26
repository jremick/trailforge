import "./styles.css";
import {
  DEFAULT_AUTH_CONFIG,
  canManageAdminCompute,
  formatFriendlyApiError,
  isAdminUser,
} from "./adminHelpers";
import {
  ApiError,
  activateVehicle,
  addFavorite,
  buildRegionPack,
  clearLegacyStoredAuthToken,
  createAdminInvite,
  createConditionReport,
  createPin,
  createVehicle,
  deletePin,
  deleteSavedRoute,
  deleteTrip,
  deleteVehicle,
  downloadAuthenticatedExport,
  exportPlannedRoute,
  fetchAdminInvites,
  fetchAdminStats,
  fetchAdminUsers,
  fetchAuthConfig,
  fetchAuthMe,
  fetchClosuresGeoJson,
  fetchClosuresStatus,
  fetchFavorites,
  fetchHealth,
  fetchHotspotsGeoJson,
  fetchPacksCatalog,
  fetchPins,
  fetchPois,
  fetchReportsGeoJson,
  fetchTileManifest,
  fetchTrackDetail,
  fetchTracksGeoJson,
  fetchTrips,
  fetchUnmappedCandidatesGeoJson,
  fetchVehicles,
  getSavedRoute,
  getSavedRouteExportUrl,
  listSavedRoutes,
  loginAccount,
  logoutAccount,
  patchAdminUser,
  planRoute,
  registerAccount,
  removeFavorite,
  revokeAdminInvite,
  saveRoute,
  setStoredAuthToken,
  triggerAdminClosuresSync,
  triggerAdminGradingRecompute,
  updateSavedRoute,
  updateTrip,
  updateVehicle,
  uploadMobileTripSync,
  uploadTripGpx,
  uploadTripJson,
  voteConditionReport,
} from "./api";
import {
  RigAccountPanel,
  type AccountPanelTab,
} from "./components/accountPanel";
import {
  AdminConsolePanel,
  type AdminPanelTab,
} from "./components/adminPanel";
import { renderAttributionFooter } from "./components/footer";
import { TrackInspectorDrawer } from "./components/inspector";
import { OfflinePacksPanel } from "./components/packsPanel";
import { InCabRecorderHud } from "./components/recorder";
import { RoutePlannerDrawer } from "./components/routePlanner";
import { Sidebar } from "./components/sidebar";
import { TopBar } from "./components/topbar";
import {
  formatCoords,
  isWithinAustralia,
  latLonToMgrs,
} from "./coords";
import { TrailForgeMap } from "./map";
import {
  buildClientGeoJson,
  buildClientGpx,
  slugifyExportFilename,
  triggerClientFileDownload,
} from "./offline/exporters";
import {
  deleteOfflinePin,
  downloadRegionalPackToDevice,
  getActiveOfflinePackId,
  listDownloadedPacks,
  listOfflinePins,
  removeOfflinePack,
  saveOfflinePin,
  setActiveOfflinePackId,
} from "./offline/packStore";
import {
  enqueueOfflinePin,
  enqueueOfflineReport,
  enqueueOfflineTrip,
  flushSyncQueue,
  generateClientUuid,
  listSyncQueue,
} from "./offline/syncQueue";
import {
  BASEMAP_OPTIONS,
  DEFAULT_PRESET_ID,
  createInitialLayerState,
  escapeHtml,
  getDefaultPreset,
  getPresetById,
} from "./presets";
import { registerServiceWorker, subscribeNetworkStatus } from "./pwa";
import {
  addRouteTrackId,
  buildPlannedRouteMapGeoJson,
  extractAnalysisBBox,
  getSavedRouteId,
  getSavedRouteWaypoints,
  moveRouteTrackDown,
  moveRouteTrackUp,
  removeRouteTrackAt,
} from "./routeHelpers";
import { buildTripExportUrl, getTripId } from "./tripsHelpers";
import {
  SIDEBAR_DEFAULT_WIDTH_PX,
  clampSidebarWidth,
  getSidebarMapLeftPadding,
  getSidebarMaxWidth,
  isSidebarResizeEnabled,
  loadStoredSidebarCollapsed,
  loadStoredSidebarWidth,
  saveStoredSidebarCollapsed,
  saveStoredSidebarWidth,
} from "./sidebarHelpers";
import type {
  AdminInviteItem,
  AdminStatsResponse,
  AdminUserItem,
  AuthConfigResponse,
  AuthUser,
  BasemapId,
  DownloadedPackRecord,
  HealthState,
  LayerVisibilityState,
  OfflinePackRegionItem,
  PlannedRouteAnalysis,
  RecordedBreadcrumbPoint,
  RegionPreset,
  RouteWaypointInput,
  SavedRouteItem,
  SyncQueueItem,
  TrackInspectorDetail,
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
const DEMO_BANNER_DISMISSED_KEY = "trailforge.dismissed_demo_banner";

function loadDismissedDemoBanner(): string {
  try {
    return window.sessionStorage.getItem(DEMO_BANNER_DISMISSED_KEY) ?? "";
  } catch {
    return "";
  }
}

function saveDismissedDemoBanner(text: string): void {
  try {
    window.sessionStorage.setItem(DEMO_BANNER_DISMISSED_KEY, text);
  } catch (err) {
    console.debug("Unable to persist dismissed banner state:", err);
  }
}

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
  private readonly adminPanel: AdminConsolePanel;
  private readonly packsPanel: OfflinePacksPanel;
  private readonly routePlanner: RoutePlannerDrawer;
  private readonly recorderHud: InCabRecorderHud;
  private readonly map: TrailForgeMap;
  private readonly demoBannerEl: HTMLElement;
  private readonly demoBannerTextEl: HTMLElement;
  private readonly collapseBtnEl: HTMLButtonElement;
  private readonly cursorCoordsEl: HTMLElement;
  private readonly cursorMgrsEl: HTMLElement;
  private readonly offlineBannerEl: HTMLElement;
  private readonly pinDropBannerEl: HTMLElement;
  private readonly routePickBannerEl: HTMLElement;
  private readonly toastContainerEl: HTMLElement;
  private readonly basemapButtons: NodeListOf<HTMLButtonElement>;

  private sidebarCollapsed = false;
  private preferredSidebarWidthPx: number = SIDEBAR_DEFAULT_WIDTH_PX;
  private activeSidebarWidthPx: number = SIDEBAR_DEFAULT_WIDTH_PX;
  private activePreset: RegionPreset = getDefaultPreset();
  private activeBasemap: BasemapId;
  private layerState: LayerVisibilityState = createInitialLayerState();
  private healthAbort: AbortController | null = null;
  private inspectorAbort: AbortController | null = null;

  // Slice 05 + 06 + 08 + v1.1.0 User, Access Control, Admin & Vehicle state
  private user: AuthUser | null = null;
  private authConfig: AuthConfigResponse = { ...DEFAULT_AUTH_CONFIG };
  private dismissedDemoBanner: string = loadDismissedDemoBanner();
  private adminStats: AdminStatsResponse | null = null;
  private adminInvites: AdminInviteItem[] = [];
  private adminUsers: AdminUserItem[] = [];
  private latestCreatedInvite: AdminInviteItem | null = null;
  private anonymousRigClass: VehicleCapabilityClass = loadStoredAnonRig();
  private vehicles: VehicleProfileItem[] = [];
  private trips: TripItem[] = [];
  private latestUploadedTrip: TripItem | null = null;
  private favorites: UserFavoriteItem[] = [];
  private pins: UserPinItem[] = [];
  private coordPickMode: "pin" | "report" | null = null;
  private pendingPinCoords: { lat: number; lon: number } | null = null;
  private pickedReportCoords: { lat: number; lon: number } | null = null;
  private currentTrackDetail: TrackInspectorDetail | null = null;

  // Slice 09 Offline Packs, Offline Pins & Sync Queue state
  private isOnline = true;
  private packRegions: OfflinePackRegionItem[] = [];
  private downloadedPacks: DownloadedPackRecord[] = [];
  private activeOfflinePackId: string | null = getActiveOfflinePackId();
  private syncQueue: SyncQueueItem[] = [];
  private offlinePins: UserPinItem[] = [];
  private isSyncingQueue = false;

  // Phase A Interactive Multi-Track Route Planner state
  private routeName = "";
  private routeNotes = "";
  private activeSavedRouteId: string | null = null;
  private routeTrackIds: string[] = [];
  private routeWaypoints: RouteWaypointInput[] = [];
  private routeAnalysis: PlannedRouteAnalysis | null = null;
  private routeLoading = false;
  private routeError: string | null = null;
  private routePlanMode = false;
  private routeExporting: "gpx" | "geojson" | null = null;
  private routeSaving = false;
  private savedRoutes: SavedRouteItem[] = [];
  private savedRoutesLoading = false;
  private routePlanAbort: AbortController | null = null;

  constructor(root: HTMLElement) {
    const initiallyOnline =
      typeof navigator === "undefined" ? true : navigator.onLine;
    this.isOnline = initiallyOnline;
    this.activeBasemap = initiallyOnline ? "opentopo" : "offline-topo";

    const initialViewportWidth =
      typeof window !== "undefined" ? window.innerWidth : undefined;
    this.sidebarCollapsed = loadStoredSidebarCollapsed(
      undefined,
      initialViewportWidth
    );
    this.preferredSidebarWidthPx = loadStoredSidebarWidth(undefined);
    this.activeSidebarWidthPx = clampSidebarWidth(
      this.preferredSidebarWidthPx,
      initialViewportWidth
    );

    root.innerHTML = `
      <div class="tf-shell ${this.sidebarCollapsed ? "sidebar-collapsed" : ""}" data-role="shell">
        <header data-role="topbar"></header>

        <div class="tf-demo-banner hidden" data-role="demo-banner" role="status">
          <span class="tf-demo-banner-copy">
            <span class="tf-demo-banner-dot" aria-hidden="true"></span>
            <span data-role="demo-banner-text"></span>
          </span>
          <button
            type="button"
            class="tf-demo-banner-dismiss"
            data-role="demo-banner-dismiss"
            title="Dismiss notice"
            aria-label="Dismiss notice"
          >×</button>
        </div>

        <main class="tf-workspace">
          <div class="tf-map-container" data-role="map"></div>

          <button
            type="button"
            class="tf-sidebar-toggle"
            data-role="sidebar-toggle"
            title="Show layers sidebar (shortcut: [)"
            aria-label="Show layers sidebar"
            aria-expanded="${!this.sidebarCollapsed}"
            aria-hidden="${!this.sidebarCollapsed}"
            tabindex="${this.sidebarCollapsed ? "0" : "-1"}"
          >${this.renderSidebarToggleMarkup(true)}</button>

          <aside data-role="sidebar"></aside>
          <aside data-role="inspector"></aside>
          <aside data-role="account-panel"></aside>
          <aside data-role="admin-panel"></aside>
          <aside data-role="packs-panel"></aside>
          <aside data-role="route-panel"></aside>

          <div data-role="recorder-hud"></div>

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

          <div class="tf-pin-drop-banner tf-route-pick-banner hidden" data-role="route-pick-banner" role="status">
            ⇢ Click tracks on map to append to route · Esc to stop
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
    this.demoBannerEl = this.q(root, "[data-role='demo-banner']");
    this.demoBannerTextEl = this.q(root, "[data-role='demo-banner-text']");
    this.collapseBtnEl = this.q<HTMLButtonElement>(
      root,
      "[data-role='sidebar-toggle']"
    );
    this.cursorCoordsEl = this.q(root, "[data-role='cursor-coords']");
    this.cursorMgrsEl = this.q(root, "[data-role='cursor-mgrs']");
    this.offlineBannerEl = this.q(root, "[data-role='offline-banner']");
    this.pinDropBannerEl = this.q(root, "[data-role='pin-drop-banner']");
    this.routePickBannerEl = this.q(root, "[data-role='route-pick-banner']");
    this.toastContainerEl = this.q(root, "[data-role='toasts']");
    this.basemapButtons = root.querySelectorAll<HTMLButtonElement>("[data-basemap]");

    this.topbar = new TopBar(
      this.q(root, "[data-role='topbar']"),
      DEFAULT_PRESET_ID,
      {
        onToggleSidebar: () => {
          this.setSidebarCollapsed(!this.sidebarCollapsed, { persist: true });
        },
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
        onRoutePlannerClick: () => {
          if (this.routePlanner.isPanelOpen()) {
            this.closeRoutePlanner();
          } else {
            this.openRoutePlanner();
          }
        },
        onHealthClick: () => void this.refreshHealth(true),
        onFeedStatusClick: () => {
          if (this.sidebarCollapsed) {
            this.setSidebarCollapsed(false, { persist: true });
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
        onOfflinePacksClick: () => {
          if (this.packsPanel.isPanelOpen()) {
            this.closePacksPanel();
          } else {
            this.openPacksPanel();
          }
        },
        onAdminClick: () => {
          if (this.adminPanel.isPanelOpen()) {
            this.closeAdminPanel();
          } else {
            this.openAdminPanel("overview");
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
          if (key === "hotspots" && enabled) {
            void this.refreshHotspotsGeoJson();
          }
        },
        onSnowLineElevationChange: (elevationM) => {
          this.layerState = { ...this.layerState, snowLineElevationM: elevationM };
          this.map.setLayerVisibility(this.layerState);
          this.inspector.setSnowLineElevation(elevationM);
          this.syncRoutePlannerUiState();
        },
        onResetRegion: () => {
          this.map.flyToPreset(
            this.activePreset,
            this.getMapLeftPadding(16)
          );
        },
        onOpenOfflinePacks: () => {
          this.openPacksPanel();
        },
        onCollapse: () => {
          this.setSidebarCollapsed(true, { persist: true });
        },
        onResizeStateChange: (resizing) => {
          this.shellEl.classList.toggle("sidebar-resizing", resizing);
        },
        onResizePreview: (widthPx) => {
          this.applySidebarWidth(widthPx, false);
        },
        onResizeCommit: (widthPx) => {
          this.applySidebarWidth(widthPx, true);
          window.setTimeout(() => this.map.resize(), 60);
        },
        onResetWidth: () => {
          this.applySidebarWidth(SIDEBAR_DEFAULT_WIDTH_PX, true);
          window.setTimeout(() => this.map.resize(), 60);
        },
      }
    );

    this.applySidebarWidth(this.preferredSidebarWidthPx, false);
    this.sidebar.updateCollapsedState(this.sidebarCollapsed);
    this.topbar.updateSidebarState(this.sidebarCollapsed);

    this.inspector = new TrackInspectorDrawer(
      this.q(root, "[data-role='inspector']"),
      {
        onClose: () => {
          this.shellEl.classList.remove("inspector-open");
          this.map.setSelectedTrackId(null);
          this.pickedReportCoords = null;
        },
        onZoomToTrack: (bbox) => {
          this.map.fitBBox(bbox, this.getMapLeftPadding(24), 420);
        },
        onPoiSelect: (poi) => {
          this.map.flyToCoordinate(poi.lat, poi.lon, poi.name, 14);
          this.updateCursorReadout(poi.lat, poi.lon);
          this.showToast(`Located ${poi.name}`, "info");
        },
        onHotspotSelect: (lat, lon, label) => {
          this.map.flyToCoordinate(lat, lon, label, 14.5);
          this.updateCursorReadout(lat, lon);
          this.showToast(`Zoomed to ${label}`, "info");
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
        onToggleTrackInRoute: (trackId, trackName, currentlyInRoute) => {
          if (currentlyInRoute) {
            this.openRoutePlanner();
          } else {
            void this.handleAddTrackToRoute(trackId, trackName);
          }
        },
        onExportTrack: (trackId, fmt) => {
          void this.handleExportTrack(trackId, fmt);
        },
        onPickReportLocation: () => {
          this.setCoordPickMode("report");
          this.shellEl.classList.remove("inspector-open");
        },
        onSubmitReport: async (input) => {
          if (!this.isOnline) {
            const { photo: _omit, ...offlinePayload } = input;
            await enqueueOfflineReport(offlinePayload);
            await this.refreshOfflineLocalState();
            this.pickedReportCoords = null;
            this.showToast(
              "Offline: condition report queued & will sync automatically when online",
              "info"
            );
            return;
          }
          try {
            await createConditionReport(input);
            this.pickedReportCoords = null;
            this.showToast("Trail condition report published", "success");
            await this.refreshReportsGeoJson();
            this.refreshOpenInspectorForRig();
          } catch (err) {
            const status =
              err && typeof err === "object" && "status" in err
                ? Number((err as { status: unknown }).status)
                : null;
            if (status === null || status === 503) {
              const { photo: _omit, ...offlinePayload } = input;
              await enqueueOfflineReport(offlinePayload);
              await this.refreshOfflineLocalState();
              this.pickedReportCoords = null;
              this.showToast(
                "Network unavailable — condition report queued for automatic sync",
                "info"
              );
              return;
            }
            throw err;
          }
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
        onOpenAdminConsole: () => this.openAdminPanel("overview"),
        onSelectAnonymousRig: (cls) => {
          this.anonymousRigClass = cls;
          saveStoredAnonRig(cls);
          this.syncRigUiState();
          this.showToast(`Rig suitability preview set to ${cls}`, "info");
          this.refreshOpenInspectorForRig();
          this.refreshPlannedRouteForRig();
        },
        onLogin: async (email, password) => {
          const res = await loginAccount({ email, password });
          this.user = res.user;
          await this.loadSignedInUserData();
          this.showToast(`Signed in as ${res.user.display_name}`, "success");
          this.refreshOpenInspectorForRig();
          this.refreshPlannedRouteForRig();
        },
        onRegister: async (displayName, email, password, inviteCode) => {
          const res = await registerAccount({
            display_name: displayName,
            email,
            password,
            invite_code: inviteCode ?? null,
          });
          this.user = res.user;
          await this.loadSignedInUserData();
          this.showToast(`Account created for ${res.user.display_name}`, "success");
          this.refreshOpenInspectorForRig();
          this.refreshPlannedRouteForRig();
        },
        onLogout: async () => {
          await logoutAccount();
          this.user = null;
          this.vehicles = [];
          this.trips = [];
          this.latestUploadedTrip = null;
          this.favorites = [];
          this.pins = [];
          this.savedRoutes = [];
          this.activeSavedRouteId = null;
          this.adminStats = null;
          this.adminInvites = [];
          this.adminUsers = [];
          this.latestCreatedInvite = null;
          this.closeAdminPanel();
          this.setCoordPickMode(null);
          this.map.setUserPins([]);
          this.syncRigUiState();
          this.syncOfflinePanelState();
          this.syncRoutePlannerUiState();
          this.showToast("Signed out — anonymous browsing active", "info");
          this.refreshOpenInspectorForRig();
          this.refreshPlannedRouteForRig();
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
          this.refreshPlannedRouteForRig();
        },
        onActivateVehicle: async (id) => {
          const updated = await activateVehicle(id);
          await this.reloadVehicles();
          this.showToast(
            `Active rig: ${updated.name} (${updated.capability_class})`,
            "success"
          );
          this.refreshOpenInspectorForRig();
          this.refreshPlannedRouteForRig();
        },
        onDeleteVehicle: async (id) => {
          await deleteVehicle(id);
          await this.reloadVehicles();
          this.showToast("Vehicle profile deleted", "info");
          this.refreshOpenInspectorForRig();
          this.refreshPlannedRouteForRig();
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
          if (!this.isOnline || !this.user) {
            const localPin: UserPinItem = {
              id: `offline-pin-${generateClientUuid()}`,
              name: payload.name,
              category: payload.category,
              notes: payload.notes ?? null,
              lon: payload.lon,
              lat: payload.lat,
              track_id: payload.track_id ?? null,
              created_at: new Date().toISOString(),
            };
            await saveOfflinePin(localPin);
            if (this.user) {
              await enqueueOfflinePin(payload, localPin.id);
            }
            this.pendingPinCoords = null;
            this.setCoordPickMode(null);
            await this.refreshOfflineLocalState();
            this.showToast(
              `Saved offline pin "${payload.name}"${this.user ? " (queued to sync)" : ""}`,
              "success"
            );
            return;
          }
          try {
            const created = await createPin(payload);
            this.pins = [created, ...this.pins];
            this.pendingPinCoords = null;
            this.setCoordPickMode(null);
            this.renderCombinedPinsOnMap();
            this.syncRigUiState();
            this.showToast(
              `Dropped pin "${created.name ?? created.title ?? payload.name}"`,
              "success"
            );
          } catch (err) {
            const status =
              err && typeof err === "object" && "status" in err
                ? Number((err as { status: unknown }).status)
                : null;
            if (status === null || status === 503) {
              const localPin: UserPinItem = {
                id: `offline-pin-${generateClientUuid()}`,
                name: payload.name,
                category: payload.category,
                notes: payload.notes ?? null,
                lon: payload.lon,
                lat: payload.lat,
                track_id: payload.track_id ?? null,
                created_at: new Date().toISOString(),
              };
              await saveOfflinePin(localPin);
              await enqueueOfflinePin(payload, localPin.id);
              this.pendingPinCoords = null;
              this.setCoordPickMode(null);
              await this.refreshOfflineLocalState();
              this.showToast(
                `Network unreachable — saved pin "${payload.name}" offline & queued to sync`,
                "info"
              );
              return;
            }
            throw err;
          }
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
          if (id.startsWith("offline-pin-")) {
            await deleteOfflinePin(id);
            await this.refreshOfflineLocalState();
            this.showToast("Offline pin deleted", "info");
            return;
          }
          await deletePin(id);
          this.pins = this.pins.filter((p) => p.id !== id);
          this.renderCombinedPinsOnMap();
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

    this.adminPanel = new AdminConsolePanel(
      this.q(root, "[data-role='admin-panel']"),
      {
        currentUserId: null,
        registrationMode: this.authConfig.registration_mode,
        loading: false,
        stats: null,
        invites: [],
        users: [],
        latestCreatedInvite: null,
        busyAction: null,
        error: null,
      },
      {
        onClose: () => this.closeAdminPanel(),
        onRefresh: () => this.refreshAdminConsoleData(),
        onCreateInvite: async (payload) => {
          this.adminPanel.updateState({
            busyAction: "create-invite",
            error: null,
          });
          try {
            const created = await createAdminInvite(payload);
            this.latestCreatedInvite = created;
            this.adminInvites = [created, ...this.adminInvites];
            this.adminPanel.updateState({
              invites: this.adminInvites,
              latestCreatedInvite: created,
            });
            this.showToast(`Created invite code ${created.code}`, "success");
            void this.refreshAdminConsoleData();
          } catch (err) {
            const msg = formatFriendlyApiError(err, "Failed to create invite");
            this.adminPanel.updateState({ error: msg });
            this.showToast(msg, "error");
          } finally {
            this.adminPanel.updateState({ busyAction: null });
          }
        },
        onRevokeInvite: async (inviteId) => {
          this.adminPanel.updateState({
            busyAction: `revoke-${inviteId}`,
            error: null,
          });
          try {
            await revokeAdminInvite(inviteId);
            await this.refreshAdminConsoleData();
            this.showToast("Invite code revoked", "info");
          } catch (err) {
            const msg = formatFriendlyApiError(err, "Failed to revoke invite");
            this.adminPanel.updateState({ error: msg });
            this.showToast(msg, "error");
          } finally {
            this.adminPanel.updateState({ busyAction: null });
          }
        },
        onCopyInviteCode: async (code) => {
          await this.copyTextToClipboard(
            code,
            `Copied invite code ${code}`,
            "Select and copy the invite code manually"
          );
        },
        onPatchUser: async (userId, patch) => {
          this.adminPanel.updateState({
            busyAction: `user-${userId}`,
            error: null,
          });
          try {
            const updated = await patchAdminUser(userId, patch);
            this.adminUsers = this.adminUsers.map((u) =>
              u.id === updated.id ? updated : u
            );
            this.adminPanel.updateState({ users: this.adminUsers });
            this.showToast(
              `Updated ${updated.display_name} (${updated.role}, ${updated.is_active ? "active" : "suspended"})`,
              "success"
            );
            void this.refreshAdminConsoleData();
          } catch (err) {
            const msg = formatFriendlyApiError(err, "Failed to update user");
            this.adminPanel.updateState({ error: msg });
            this.showToast(msg, "error");
          } finally {
            this.adminPanel.updateState({ busyAction: null });
          }
        },
        onSyncClosures: async () => {
          this.adminPanel.updateState({
            busyAction: "sync-closures",
            error: null,
          });
          try {
            await triggerAdminClosuresSync();
            await Promise.all([
              this.refreshClosures(false),
              this.refreshAdminConsoleData(),
            ]);
            this.showToast("Agency closure feeds synced", "success");
          } catch (err) {
            const msg = formatFriendlyApiError(err, "Closure sync failed");
            this.adminPanel.updateState({ error: msg });
            this.showToast(msg, "error");
          } finally {
            this.adminPanel.updateState({ busyAction: null });
          }
        },
        onRecomputeGrades: async () => {
          this.adminPanel.updateState({
            busyAction: "recompute-grades",
            error: null,
          });
          try {
            await triggerAdminGradingRecompute();
            await Promise.all([
              this.refreshHotspotsGeoJson(),
              this.refreshAdminConsoleData(),
            ]);
            this.refreshOpenInspectorForRig();
            this.showToast("Live crowdsourced grades recomputed", "success");
          } catch (err) {
            const msg = formatFriendlyApiError(
              err,
              "Live grade recomputation failed"
            );
            this.adminPanel.updateState({ error: msg });
            this.showToast(msg, "error");
          } finally {
            this.adminPanel.updateState({ busyAction: null });
          }
        },
      }
    );

    this.packsPanel = new OfflinePacksPanel(
      this.q(root, "[data-role='packs-panel']"),
      {
        online: initiallyOnline,
        isAdmin: false,
        loading: false,
        regions: [],
        downloaded: [],
        activeOfflinePackId: this.activeOfflinePackId,
        syncQueue: [],
        syncing: false,
        buildingRegionId: null,
        downloadingRegionId: null,
        downloadProgress: null,
        error: null,
      },
      {
        onClose: () => this.closePacksPanel(),
        onRefresh: () => this.refreshPacksCatalog(),
        onBuildPack: async (regionId) => {
          this.packsPanel.updateState({
            buildingRegionId: regionId,
            error: null,
          });
          try {
            await buildRegionPack(regionId);
            await this.refreshPacksCatalog();
            this.showToast(`Built offline regional pack: ${regionId}`, "success");
          } catch (err) {
            const msg = formatFriendlyApiError(err, "Failed to build pack");
            this.packsPanel.updateState({ error: msg });
            this.showToast(msg, "error");
          } finally {
            this.packsPanel.updateState({ buildingRegionId: null });
          }
        },
        onDownloadPack: async (region) => {
          this.packsPanel.updateState({
            downloadingRegionId: region.region_id,
            downloadProgress: { stage: "Starting download…", pct: 5 },
            error: null,
          });
          try {
            const record = await downloadRegionalPackToDevice(region, (prog) => {
              this.packsPanel.updateState({ downloadProgress: prog });
            });
            await this.refreshOfflineLocalState();
            this.showToast(
              `Downloaded ${record.name} for offline use (${record.track_count} tracks)`,
              "success"
            );
          } catch (err) {
            const msg =
              err instanceof Error ? err.message : "Pack download failed";
            this.packsPanel.updateState({ error: msg });
            this.showToast(msg, "error");
          } finally {
            this.packsPanel.updateState({
              downloadingRegionId: null,
              downloadProgress: null,
            });
          }
        },
        onUseOfflinePack: async (regionId) => {
          this.activeOfflinePackId = regionId;
          setActiveOfflinePackId(regionId);
          this.syncOfflinePanelState();
          await this.loadVectorAndTerrainLayers();
          await Promise.all([
            this.refreshClosures(false),
            this.refreshReportsGeoJson(),
            this.refreshHotspotsGeoJson(),
          ]);
          if (regionId) {
            this.setActiveBasemap("offline-topo");
            const preset = getPresetById(regionId);
            if (preset) {
              this.map.flyToPreset(preset, this.getMapLeftPadding(16));
            }
            this.showToast(
              `Switched to offline pack "${regionId}" (zero network tiles)`,
              "success"
            );
          } else {
            this.showToast("Returned to live TrailForge API tiles", "info");
          }
        },
        onRemovePack: async (regionId) => {
          await removeOfflinePack(regionId);
          this.activeOfflinePackId = getActiveOfflinePackId();
          await this.refreshOfflineLocalState();
          this.showToast(`Removed cached pack ${regionId}`, "info");
        },
        onSyncNow: () => this.handleFlushSyncQueue(true),
      }
    );

    this.routePlanner = new RoutePlannerDrawer(
      this.q(root, "[data-role='route-panel']"),
      this.buildRoutePlannerState(),
      {
        onClose: () => this.closeRoutePlanner(),
        onTogglePlanMode: (active) => this.setRoutePlanMode(active),
        onRouteNameChange: (name) => {
          this.routeName = name;
        },
        onRouteNotesChange: (notes) => {
          this.routeNotes = notes;
        },
        onMoveLegUp: (idx) => {
          this.routeTrackIds = moveRouteTrackUp(this.routeTrackIds, idx);
          void this.recomputePlannedRoute(false);
        },
        onMoveLegDown: (idx) => {
          this.routeTrackIds = moveRouteTrackDown(this.routeTrackIds, idx);
          void this.recomputePlannedRoute(false);
        },
        onRemoveLeg: (idx) => {
          this.routeTrackIds = removeRouteTrackAt(this.routeTrackIds, idx);
          void this.recomputePlannedRoute(false);
        },
        onClearRoute: () => {
          this.routePlanAbort?.abort();
          this.routeTrackIds = [];
          this.routeWaypoints = [];
          this.routeAnalysis = null;
          this.routeName = "";
          this.routeNotes = "";
          this.activeSavedRouteId = null;
          this.routeError = null;
          this.setRoutePlanMode(false);
          this.map.setPlannedRoute(null, null);
          this.syncRoutePlannerUiState();
          this.syncRigUiState();
          this.showToast("Cleared active route", "info");
        },
        onInspectTrack: (trackId) => {
          this.closeRoutePlanner();
          void this.openTrackInspector(trackId);
        },
        onZoomToRoute: (bbox) => {
          this.map.fitBBox(bbox, this.getMapLeftPadding(24), 420);
        },
        onPoiSelect: (poi) => {
          this.map.flyToCoordinate(poi.lat, poi.lon, poi.name, 14);
          this.updateCursorReadout(poi.lat, poi.lon);
          this.showToast(`Located ${poi.name}`, "info");
        },
        onOpenRigPanel: () => {
          this.openAccountPanel("rig");
        },
        onOpenSignIn: () => {
          this.openAccountPanel("account");
        },
        onExportRoute: (fmt) => this.handleExportPlannedRoute(fmt),
        onSaveRoute: (asNew) => this.handleSavePlannedRoute(asNew),
        onLoadSavedRoute: (routeId) => this.handleLoadSavedRoute(routeId),
        onDeleteSavedRoute: (routeId) => this.handleDeleteSavedRoute(routeId),
        onExportSavedRoute: (routeId, fmt) =>
          this.handleExportSavedRoute(routeId, fmt),
        onCopyGridForgeCli: (cli) => this.handleCopyGridForgeCli(cli),
      }
    );

    this.recorderHud = new InCabRecorderHud(
      this.q(root, "[data-role='recorder-hud']"),
      {
        onPointsChanged: (pts, backtrack) => {
          this.map.setRecordedBreadcrumbs(pts, backtrack);
        },
        onCenterOnPoint: (lat, lon) => {
          this.updateCursorReadout(lat, lon);
        },
        onSaveRecordedTrip: async (params) => {
          const clientUuid = generateClientUuid();
          const payload = {
            name: params.name,
            points: params.points.map((p) => ({
              lon: p.lon,
              lat: p.lat,
              elev_m: p.elev_m ?? null,
              timestamp: p.timestamp,
              speed_mps: p.speed_mps ?? null,
              roughness_rms: p.roughness_rms ?? null,
            })),
            imu_samples:
              params.imu_samples.length > 0 ? params.imu_samples : undefined,
            recording_source: params.recording_source,
            vehicle_profile_id: params.vehicle_profile_id,
            visibility: params.visibility,
            privacy_trim_m: params.privacy_trim_m,
            client_uuid: clientUuid,
            client_session_id: clientUuid,
          };

          if (this.isOnline && this.user) {
            try {
              const uploaded = await uploadMobileTripSync(payload);
              this.latestUploadedTrip = uploaded;
              await this.reloadTrips();
              void this.refreshUnmappedCandidates();
              void this.refreshHotspotsGeoJson();
              const shockSuffix =
                uploaded.imu_summary && uploaded.imu_summary.shock_event_count > 0
                  ? ` · ${uploaded.imu_summary.shock_event_count} IMU shocks`
                  : "";
              this.showToast(
                `Mobile sync matched ${uploaded.matched_segment_count ?? 0} segments${shockSuffix}`,
                "success"
              );
              return;
            } catch (err) {
              const status =
                err && typeof err === "object" && "status" in err
                  ? Number((err as { status: unknown }).status)
                  : null;
              if (status !== null && status !== 503 && status !== 0) {
                throw err;
              }
            }
          }

          await enqueueOfflineTrip(payload);
          await this.refreshOfflineLocalState();
          this.showToast(
            this.user
              ? "Offline: recorded trip & IMU telemetry queued for automatic sync"
              : "Trip queued locally — sign in & sync when online (or export GPX anytime)",
            "info"
          );
        },
        onToast: (msg, kind) => this.showToast(msg, kind),
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
          if (this.routePlanMode) {
            void this.handleAddTrackToRoute(trackId);
            return;
          }
          this.closeAccountPanel();
          this.closeAdminPanel();
          this.closeRoutePlanner();
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
      authConfig: this.authConfig,
      anonymousRigClass: this.anonymousRigClass,
      vehicles: this.vehicles,
      trips: this.trips,
      latestUploadedTrip: this.latestUploadedTrip,
      favorites: this.favorites,
      pins: [...this.offlinePins, ...this.pins],
      pinDropActive: this.coordPickMode === "pin",
      pendingPinCoords: this.pendingPinCoords,
    };
  }

  private renderCombinedPinsOnMap(): void {
    this.map.setUserPins([...this.offlinePins, ...this.pins]);
  }

  private syncOfflinePanelState(): void {
    this.packsPanel.updateState({
      online: this.isOnline,
      isAdmin: canManageAdminCompute(this.user),
      regions: this.packRegions,
      downloaded: this.downloadedPacks,
      activeOfflinePackId: this.activeOfflinePackId,
      syncQueue: this.syncQueue,
      syncing: this.isSyncingQueue,
    });
    this.topbar.updateOfflineSummary({
      online: this.isOnline,
      downloadedCount: this.downloadedPacks.length,
      activePackId: this.activeOfflinePackId,
      pendingSyncCount: this.syncQueue.length,
    });
    this.recorderHud.updateContext({
      vehicles: this.vehicles,
      isOnline: this.isOnline,
      isAuthenticated: Boolean(this.user),
    });
  }

  private async refreshOfflineLocalState(): Promise<void> {
    const [downloaded, queue, offPins] = await Promise.all([
      listDownloadedPacks().catch(() => []),
      listSyncQueue().catch(() => []),
      listOfflinePins().catch(() => []),
    ]);
    this.downloadedPacks = downloaded;
    this.syncQueue = queue;
    this.offlinePins = offPins;
    this.renderCombinedPinsOnMap();
    this.syncOfflinePanelState();
    this.syncRigUiState();
  }

  private async refreshPacksCatalog(): Promise<void> {
    this.packsPanel.updateState({ loading: true, error: null });
    try {
      const catalog = await fetchPacksCatalog();
      this.packRegions = catalog.regions ?? [];
    } catch {
      // Keep cached fallback regions cleanly when offline
    } finally {
      this.packsPanel.updateState({ loading: false });
      await this.refreshOfflineLocalState();
    }
  }

  private async handleFlushSyncQueue(announce: boolean): Promise<void> {
    if (this.isSyncingQueue || !this.isOnline) return;
    if (this.syncQueue.length === 0) return;

    this.isSyncingQueue = true;
    this.syncOfflinePanelState();
    try {
      const result = await flushSyncQueue({
        syncTrip: async (payload) => {
          await uploadMobileTripSync(payload);
        },
        syncReport: async (payload) => {
          await createConditionReport(payload);
        },
        syncPin: async (payload, localPinId) => {
          const created = await createPin(payload);
          this.pins = [created, ...this.pins];
          if (localPinId) {
            await deleteOfflinePin(localPinId);
          }
        },
      });
      await this.refreshOfflineLocalState();
      if (result.synced > 0) {
        void this.reloadTrips().catch(() => undefined);
        void this.refreshReportsGeoJson();
        void this.refreshHotspotsGeoJson();
        this.showToast(
          `Synced ${result.synced} queued offline item${result.synced === 1 ? "" : "s"}`,
          "success"
        );
      } else if (result.authRequired && announce) {
        this.showToast(
          "Sign in to complete syncing queued trips/reports",
          "info"
        );
        this.openAccountPanel("account");
      } else if (result.failed > 0 && announce) {
        this.showToast(
          `${result.failed} queued item(s) could not sync yet`,
          "error"
        );
      }
    } finally {
      this.isSyncingQueue = false;
      this.syncOfflinePanelState();
    }
  }

  private openPacksPanel(): void {
    if (this.inspector.openState()) {
      this.inspector.close();
      this.shellEl.classList.remove("inspector-open");
    }
    this.closeAccountPanel();
    this.closeAdminPanel();
    this.closeRoutePlanner();
    this.packsPanel.open();
    this.shellEl.classList.add("packs-open");
    void this.refreshPacksCatalog();
  }

  private closePacksPanel(): void {
    this.packsPanel.close();
    this.shellEl.classList.remove("packs-open");
  }

  private buildRoutePlannerState() {
    const rig = this.getActiveRigInfo();
    return {
      routeName: this.routeName,
      routeNotes: this.routeNotes,
      activeSavedRouteId: this.activeSavedRouteId,
      trackIds: [...this.routeTrackIds],
      waypoints: [...this.routeWaypoints],
      analysis: this.routeAnalysis,
      loading: this.routeLoading,
      error: this.routeError,
      planModeActive: this.routePlanMode,
      exporting: this.routeExporting,
      saving: this.routeSaving,
      savedRoutes: this.savedRoutes,
      savedRoutesLoading: this.savedRoutesLoading,
      isAuthenticated: Boolean(this.user),
      activeVehicleClass: rig.vehicleClass,
      activeVehicleName: rig.vehicleName,
      snowLineElevationM: this.layerState.snowLineElevationM,
    };
  }

  private syncRoutePlannerUiState(): void {
    this.routePlanner.updateState(this.buildRoutePlannerState());
    this.topbar.updateRouteSummary({
      legCount: this.routeTrackIds.length,
      planModeActive: this.routePlanMode,
      panelOpen: this.routePlanner.isPanelOpen(),
    });
    if (this.routePlanMode) {
      const n = this.routeTrackIds.length;
      this.routePickBannerEl.textContent = `⇢ Click tracks on map to append to route (${n} leg${n === 1 ? "" : "s"}) · Esc to stop`;
      this.routePickBannerEl.classList.remove("hidden");
    } else {
      this.routePickBannerEl.classList.add("hidden");
    }
  }

  private openRoutePlanner(): void {
    if (this.inspector.openState()) {
      this.inspector.close();
      this.shellEl.classList.remove("inspector-open");
    }
    this.closeAccountPanel();
    this.closeAdminPanel();
    this.closePacksPanel();
    this.routePlanner.updateState(this.buildRoutePlannerState());
    this.routePlanner.open();
    this.shellEl.classList.add("route-open");
    this.syncRoutePlannerUiState();
    if (this.user && this.isOnline) {
      void this.refreshSavedRoutes();
    }
  }

  private closeRoutePlanner(): void {
    if (this.routePlanMode) {
      this.setRoutePlanMode(false);
    }
    this.routePlanner.close();
    this.shellEl.classList.remove("route-open");
    this.syncRoutePlannerUiState();
  }

  private setRoutePlanMode(active: boolean): void {
    if (active && this.coordPickMode !== null) {
      this.setCoordPickMode(null);
    }
    this.routePlanMode = active;
    this.map.setRoutePlanMode(active);
    this.syncRoutePlannerUiState();
  }

  private async handleAddTrackToRoute(
    trackId: string,
    trackLabel?: string
  ): Promise<void> {
    const res = addRouteTrackId(this.routeTrackIds, trackId);
    if (!res.added) {
      if (res.reason === "duplicate") {
        this.showToast(
          `${trackLabel ?? "Track"} is already in the active route`,
          "info"
        );
      } else if (res.reason === "limit") {
        this.showToast("Maximum of 30 legs per route reached", "error");
      }
      return;
    }

    this.routeTrackIds = res.trackIds;
    this.routeError = null;
    this.syncRoutePlannerUiState();
    this.syncRigUiState();
    this.showToast(
      `Added ${trackLabel ?? "track"} to Route (${this.routeTrackIds.length} leg${this.routeTrackIds.length === 1 ? "" : "s"})`,
      "success"
    );
    await this.recomputePlannedRoute(false);
  }

  private refreshPlannedRouteForRig(): void {
    if (this.routeTrackIds.length > 0) {
      void this.recomputePlannedRoute(false);
    } else {
      this.syncRoutePlannerUiState();
    }
  }

  private buildRoutePlanRequestPayload() {
    const rig = this.getActiveRigInfo();
    const queryVehicleClass =
      this.user && this.vehicles.length > 0 ? undefined : rig.vehicleClass;
    return {
      name: this.routeName.trim() || undefined,
      track_ids: [...this.routeTrackIds],
      vehicle_class: queryVehicleClass,
      waypoints: this.routeWaypoints.length > 0 ? this.routeWaypoints : undefined,
    };
  }

  private async recomputePlannedRoute(fitMap: boolean): Promise<void> {
    this.routePlanAbort?.abort();
    if (this.routeTrackIds.length === 0) {
      this.routeAnalysis = null;
      this.routeLoading = false;
      this.routeError = null;
      this.map.setPlannedRoute(null, null);
      this.syncRoutePlannerUiState();
      this.syncRigUiState();
      return;
    }

    const controller = new AbortController();
    this.routePlanAbort = controller;
    this.routeLoading = true;
    this.routeError = null;
    this.syncRoutePlannerUiState();

    try {
      const analysis = await planRoute(
        this.buildRoutePlanRequestPayload(),
        controller.signal
      );
      if (controller.signal.aborted) return;

      this.routeAnalysis = analysis;
      this.routeLoading = false;
      const { lineFc, pinsFc } = buildPlannedRouteMapGeoJson(analysis);
      this.map.setPlannedRoute(lineFc, pinsFc);

      if (fitMap) {
        const bbox = extractAnalysisBBox(analysis);
        if (bbox) {
          this.map.fitBBox(bbox, this.getMapLeftPadding(24), 420);
        }
      }
    } catch (err) {
      if (controller.signal.aborted) return;
      this.routeLoading = false;
      this.routeError =
        err instanceof Error ? err.message : "Failed to plan route";
    } finally {
      if (!controller.signal.aborted) {
        this.syncRoutePlannerUiState();
        this.syncRigUiState();
      }
    }
  }

  private async refreshSavedRoutes(): Promise<void> {
    if (!this.user) {
      this.savedRoutes = [];
      this.syncRoutePlannerUiState();
      return;
    }
    this.savedRoutesLoading = true;
    this.syncRoutePlannerUiState();
    try {
      this.savedRoutes = await listSavedRoutes();
    } catch {
      // Preserve last loaded saved routes if offline
    } finally {
      this.savedRoutesLoading = false;
      this.syncRoutePlannerUiState();
    }
  }

  private async handleExportPlannedRoute(
    format: "gpx" | "geojson"
  ): Promise<void> {
    if (this.routeTrackIds.length === 0) return;
    this.routeExporting = format;
    this.syncRoutePlannerUiState();
    try {
      await exportPlannedRoute(
        this.buildRoutePlanRequestPayload(),
        format,
        this.routeAnalysis
      );
      this.showToast(
        `Exported planned route (${format.toUpperCase()})`,
        "success"
      );
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "Route export failed";
      this.showToast(msg, "error");
    } finally {
      this.routeExporting = null;
      this.syncRoutePlannerUiState();
    }
  }

  private async handleSavePlannedRoute(asNew: boolean): Promise<void> {
    if (!this.user) {
      this.showToast("Sign in to save routes to your account", "info");
      this.openAccountPanel("account");
      return;
    }
    if (this.routeTrackIds.length === 0) return;

    const finalName =
      this.routeName.trim() ||
      this.routeAnalysis?.name ||
      "Planned 4WD Route";
    const rig = this.getActiveRigInfo();
    this.routeSaving = true;
    this.routeError = null;
    this.syncRoutePlannerUiState();

    try {
      let saved: SavedRouteItem;
      if (this.activeSavedRouteId && !asNew) {
        saved = await updateSavedRoute(this.activeSavedRouteId, {
          name: finalName,
          notes: this.routeNotes.trim() || null,
          track_ids: [...this.routeTrackIds],
          waypoints: [...this.routeWaypoints],
          vehicle_class: rig.vehicleClass,
        });
        this.showToast(`Updated saved route "${saved.name}"`, "success");
      } else {
        saved = await saveRoute({
          name: finalName,
          notes: this.routeNotes.trim() || null,
          track_ids: [...this.routeTrackIds],
          waypoints: [...this.routeWaypoints],
          vehicle_class: rig.vehicleClass,
        });
        this.showToast(`Saved route "${saved.name}"`, "success");
      }

      this.activeSavedRouteId = getSavedRouteId(saved);
      this.routeName = saved.name;
      if (saved.analysis) {
        this.routeAnalysis = saved.analysis;
        const { lineFc, pinsFc } = buildPlannedRouteMapGeoJson(saved.analysis);
        this.map.setPlannedRoute(lineFc, pinsFc);
      }
      await this.refreshSavedRoutes();
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "Failed to save route";
      this.routeError = msg;
      this.showToast(msg, "error");
    } finally {
      this.routeSaving = false;
      this.syncRoutePlannerUiState();
    }
  }

  private async handleLoadSavedRoute(routeId: string): Promise<void> {
    this.routeLoading = true;
    this.routeError = null;
    this.syncRoutePlannerUiState();
    try {
      const loaded = await getSavedRoute(routeId);
      this.activeSavedRouteId = getSavedRouteId(loaded);
      this.routeName = loaded.name;
      this.routeNotes = loaded.notes ?? "";
      this.routeTrackIds = [...(loaded.track_ids ?? [])];
      this.routeWaypoints = getSavedRouteWaypoints(loaded);

      if (loaded.analysis) {
        this.routeAnalysis = loaded.analysis;
        this.routeLoading = false;
        const { lineFc, pinsFc } = buildPlannedRouteMapGeoJson(loaded.analysis);
        this.map.setPlannedRoute(lineFc, pinsFc);
        const bbox = extractAnalysisBBox(loaded.analysis);
        if (bbox) {
          this.map.fitBBox(bbox, this.getMapLeftPadding(24), 420);
        }
        this.syncRoutePlannerUiState();
        this.syncRigUiState();
      } else {
        await this.recomputePlannedRoute(true);
      }
      this.showToast(`Loaded route "${loaded.name}"`, "success");
    } catch (err) {
      this.routeLoading = false;
      const msg =
        err instanceof Error ? err.message : "Failed to load saved route";
      this.routeError = msg;
      this.showToast(msg, "error");
      this.syncRoutePlannerUiState();
    }
  }

  private async handleDeleteSavedRoute(routeId: string): Promise<void> {
    try {
      await deleteSavedRoute(routeId);
      this.savedRoutes = this.savedRoutes.filter(
        (r) => getSavedRouteId(r) !== routeId
      );
      if (this.activeSavedRouteId === routeId) {
        this.activeSavedRouteId = null;
      }
      this.syncRoutePlannerUiState();
      this.showToast("Saved route deleted", "info");
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "Failed to delete route";
      this.showToast(msg, "error");
    }
  }

  private async handleExportSavedRoute(
    routeId: string,
    format: "gpx" | "geojson"
  ): Promise<void> {
    try {
      const url = getSavedRouteExportUrl(routeId, format);
      await downloadAuthenticatedExport(
        url,
        `route_${routeId}.${format === "gpx" ? "gpx" : "geojson"}`
      );
      this.showToast(`Downloaded route (${format.toUpperCase()})`, "success");
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : "Failed to download route";
      this.showToast(msg, "error");
    }
  }

  private async copyTextToClipboard(
    text: string,
    successToast: string,
    fallbackToast: string
  ): Promise<void> {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
      }
      this.showToast(successToast, "success");
    } catch {
      this.showToast(fallbackToast, "info");
    }
  }

  private async handleCopyGridForgeCli(cli: string): Promise<void> {
    await this.copyTextToClipboard(
      cli,
      "GridForge CLI command copied to clipboard",
      "Select & copy the CLI command manually"
    );
  }

  private syncDemoBanner(): void {
    const text = this.authConfig.demo_banner.trim();
    const shouldShow = Boolean(text) && text !== this.dismissedDemoBanner;
    const wasHidden = this.demoBannerEl.classList.contains("hidden");
    this.demoBannerTextEl.textContent = text;
    this.demoBannerEl.classList.toggle("hidden", !shouldShow);
    if (wasHidden !== !shouldShow) {
      window.setTimeout(() => this.map.resize(), 60);
    }
  }

  private syncRigUiState(): void {
    const rig = this.getActiveRigInfo();
    const isAdmin = isAdminUser(this.user);
    this.topbar.updateRigSummary(
      rig.vehicleClass,
      rig.vehicleName,
      this.user?.display_name ?? null,
      this.favorites.length + this.pins.length + this.offlinePins.length
    );
    this.topbar.updateAdminAccess(isAdmin, this.adminPanel.isPanelOpen());
    this.accountPanel.updateState(this.buildAccountPanelState());
    this.packsPanel.updateState({ isAdmin: canManageAdminCompute(this.user) });
    this.adminPanel.updateState({
      currentUserId: this.user?.id ?? null,
      registrationMode: this.authConfig.registration_mode,
    });
    if (!isAdmin && this.adminPanel.isPanelOpen()) {
      this.closeAdminPanel();
    }
    this.recorderHud.updateContext({
      vehicles: this.vehicles,
      isOnline: this.isOnline,
      isAuthenticated: Boolean(this.user),
    });

    const openTrackId = this.inspector.getCurrentTrackId();
    this.inspector.updateContext({
      snowLineElevationM: this.layerState.snowLineElevationM,
      activeVehicleClass: rig.vehicleClass,
      activeVehicleName: rig.vehicleName,
      isFavorite: Boolean(
        openTrackId && this.favorites.some((f) => f.track_id === openTrackId)
      ),
      isInRoute: Boolean(
        openTrackId && this.routeTrackIds.includes(openTrackId)
      ),
      isAuthenticated: Boolean(this.user),
      pickedReportCoords: this.pickedReportCoords,
    });
    this.syncRoutePlannerUiState();
  }

  private openAccountPanel(tab?: AccountPanelTab): void {
    if (this.inspector.openState()) {
      this.inspector.close();
      this.shellEl.classList.remove("inspector-open");
    }
    this.closeAdminPanel();
    this.closePacksPanel();
    this.closeRoutePlanner();
    this.accountPanel.updateState(this.buildAccountPanelState());
    this.accountPanel.open(tab);
    this.shellEl.classList.add("account-open");
  }

  private closeAccountPanel(): void {
    this.accountPanel.close();
    this.shellEl.classList.remove("account-open");
  }

  private openAdminPanel(tab?: AdminPanelTab): void {
    if (!isAdminUser(this.user)) {
      this.showToast("Admin privileges required", "error");
      return;
    }
    if (this.inspector.openState()) {
      this.inspector.close();
      this.shellEl.classList.remove("inspector-open");
    }
    this.closeAccountPanel();
    this.closePacksPanel();
    this.closeRoutePlanner();
    this.adminPanel.open(tab);
    this.shellEl.classList.add("admin-open");
    this.topbar.updateAdminAccess(true, true);
    void this.refreshAdminConsoleData();
  }

  private closeAdminPanel(): void {
    this.adminPanel.close();
    this.shellEl.classList.remove("admin-open");
    this.topbar.updateAdminAccess(isAdminUser(this.user), false);
  }

  private async refreshAdminConsoleData(): Promise<void> {
    if (!isAdminUser(this.user)) return;
    this.adminPanel.updateState({ loading: true, error: null });
    try {
      const [stats, invites, users] = await Promise.all([
        fetchAdminStats(),
        fetchAdminInvites(),
        fetchAdminUsers(),
      ]);
      this.adminStats = stats;
      this.adminInvites = invites;
      this.adminUsers = users;
      this.adminPanel.updateState({
        stats: this.adminStats,
        invites: this.adminInvites,
        users: this.adminUsers,
        latestCreatedInvite: this.latestCreatedInvite,
        registrationMode:
          stats.registration_mode ?? this.authConfig.registration_mode,
      });
    } catch (err) {
      this.adminPanel.updateState({
        error: formatFriendlyApiError(err, "Failed to load admin console data"),
      });
    } finally {
      this.adminPanel.updateState({ loading: false });
    }
  }

  private setCoordPickMode(mode: "pin" | "report" | null): void {
    if (mode !== null && this.routePlanMode) {
      this.setRoutePlanMode(false);
    }
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
    root
      .querySelector<HTMLButtonElement>("[data-role='demo-banner-dismiss']")
      ?.addEventListener("click", () => {
        const text = this.authConfig.demo_banner.trim();
        this.dismissedDemoBanner = text;
        saveDismissedDemoBanner(text);
        this.syncDemoBanner();
      });

    this.collapseBtnEl.addEventListener("click", () => {
      this.setSidebarCollapsed(!this.sidebarCollapsed, { persist: true });
    });

    window.addEventListener("resize", () => {
      this.syncSidebarWidthForViewport();
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
      } else if (e.key === "[" && !inInput && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        this.setSidebarCollapsed(!this.sidebarCollapsed, { persist: true });
      } else if (e.key === "Escape") {
        if (this.coordPickMode === "pin") {
          this.setCoordPickMode(null);
          this.openAccountPanel("saved");
        } else if (this.coordPickMode === "report") {
          this.setCoordPickMode(null);
          if (this.inspector.openState()) {
            this.shellEl.classList.add("inspector-open");
          }
        } else if (this.routePlanMode) {
          this.setRoutePlanMode(false);
        } else if (this.routePlanner.isPanelOpen()) {
          this.closeRoutePlanner();
        } else if (this.adminPanel.isPanelOpen()) {
          this.closeAdminPanel();
        } else if (this.packsPanel.isPanelOpen()) {
          this.closePacksPanel();
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
      this.isOnline = online;
      this.sidebar.updateNetworkStatus(online);
      this.syncOfflinePanelState();
      if (!online) {
        this.setActiveBasemap("offline-topo");
        this.offlineBannerEl.textContent =
          this.downloadedPacks.length > 0
            ? "Offline mode — serving downloaded regional pack PMTiles & snapshots"
            : "Offline mode — displaying corridor PMTiles & built-in topo grid";
        this.offlineBannerEl.classList.remove("hidden");
      } else {
        this.offlineBannerEl.classList.add("hidden");
        void this.refreshHealth(false);
        void this.refreshClosures(false);
        void this.refreshReportsGeoJson();
        void this.refreshHotspotsGeoJson();
        void this.handleFlushSyncQueue(false);
      }
    });
  }

  private bootstrap(): void {
    clearLegacyStoredAuthToken();

    const initialHealth: HealthState = {
      kind: "connecting",
      data: null,
      error: null,
      checkedAt: null,
    };
    this.topbar.updateHealth(initialHealth);
    this.sidebar.updateHealth(initialHealth);

    void this.refreshOfflineLocalState();
    void this.refreshHealth(false);
    void this.hydrateAuthConfig();
    void this.loadVectorAndTerrainLayers();
    void this.refreshClosures(false);
    void this.refreshHotspotsGeoJson();
    void this.refreshReportsGeoJson();
    void this.refreshUnmappedCandidates();
    void this.refreshPacksCatalog();
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

  private async hydrateAuthConfig(): Promise<void> {
    try {
      this.authConfig = await fetchAuthConfig();
    } catch {
      this.authConfig = { ...DEFAULT_AUTH_CONFIG };
    } finally {
      this.syncDemoBanner();
      this.syncRigUiState();
    }
  }

  private async hydrateAuthSession(): Promise<void> {
    clearLegacyStoredAuthToken();
    try {
      const me = await fetchAuthMe();
      if (me.authenticated && me.user) {
        this.user = me.user;
        await this.loadSignedInUserData();
      } else {
        this.user = null;
        this.syncRigUiState();
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        setStoredAuthToken(null);
        this.user = null;
        this.showToast(formatFriendlyApiError(err), "error");
      }
      this.syncRigUiState();
    }
  }

  private async loadSignedInUserData(): Promise<void> {
    try {
      const [vehicles, trips, favorites, pins, savedRoutes] = await Promise.all([
        fetchVehicles().catch(() => []),
        fetchTrips().catch(() => []),
        fetchFavorites().catch(() => []),
        fetchPins().catch(() => []),
        listSavedRoutes().catch(() => []),
      ]);
      this.vehicles = vehicles;
      this.trips = trips;
      this.favorites = favorites;
      this.pins = pins;
      this.savedRoutes = savedRoutes;
      this.renderCombinedPinsOnMap();
    } finally {
      this.syncRigUiState();
      if (this.isOnline && this.syncQueue.length > 0) {
        void this.handleFlushSyncQueue(false);
      }
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

  private async refreshHotspotsGeoJson(): Promise<void> {
    try {
      const fc = await fetchHotspotsGeoJson();
      this.map.setHotspotsGeoJson(fc);
    } catch {
      // Obstacle hotspots layer stays empty gracefully offline
    }
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
        const isOfflinePack =
          Boolean(this.activeOfflinePackId) || !this.isOnline;
        this.map.applyTileManifest(manifest, { isOfflinePack });
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
    this.closeAdminPanel();
    this.closePacksPanel();
    this.closeRoutePlanner();
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

      this.currentTrackDetail = detail;
      this.inspector.showTrack(detail, {
        snowLineElevationM: this.layerState.snowLineElevationM,
        activeVehicleClass: rig.vehicleClass,
        activeVehicleName: rig.vehicleName,
        isFavorite: this.favorites.some((f) => f.track_id === trackId),
        isInRoute: this.routeTrackIds.includes(trackId),
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
    if (this.isOnline) {
      try {
        const url = buildTrackExportUrl(trackId, format);
        await downloadAuthenticatedExport(
          url,
          `${trackId}.${format === "gpx" ? "gpx" : "geojson"}`
        );
        this.showToast(`Downloaded track ${format.toUpperCase()}`, "success");
        return;
      } catch {
        // Fall through to 100% client-side offline export if track detail is loaded
      }
    }

    const detail = this.currentTrackDetail;
    if (detail && detail.track_id === trackId && detail.segments?.length) {
      const pts: RecordedBreadcrumbPoint[] = [];
      for (const seg of detail.segments) {
        const geom = (
          seg as unknown as {
            geometry?: { type?: string; coordinates?: number[][] };
          }
        ).geometry;
        if (geom?.type === "LineString" && Array.isArray(geom.coordinates)) {
          for (const c of geom.coordinates) {
            if (typeof c[0] === "number" && typeof c[1] === "number") {
              pts.push({
                lon: c[0],
                lat: c[1],
                elev_m: typeof c[2] === "number" ? c[2] : (seg.elev_min_m ?? null),
                timestamp: new Date().toISOString(),
              });
            }
          }
        }
      }
      if (pts.length >= 2) {
        if (format === "gpx") {
          const xml = buildClientGpx(detail.name, pts, {
            description: `TrailForge offline track export (${detail.state})`,
          });
          triggerClientFileDownload(
            xml,
            slugifyExportFilename(detail.name, "gpx"),
            "application/gpx+xml"
          );
        } else {
          const fc = buildClientGeoJson(detail.name, pts, {
            description: `TrailForge offline track export (${detail.state})`,
          });
          triggerClientFileDownload(
            JSON.stringify(fc, null, 2),
            slugifyExportFilename(detail.name, "geojson"),
            "application/geo+json"
          );
        }
        this.showToast(
          `Exported offline ${format.toUpperCase()} (GridForge-compatible)`,
          "success"
        );
        return;
      }
    }

    this.showToast("Export failed — track geometry unavailable offline", "error");
  }

  private handleSearchSelect(item: UnifiedSearchItem): void {
    if (item.kind === "preset" && item.presetId) {
      this.selectPreset(item.presetId);
      return;
    }

    if (item.kind === "track") {
      if (item.bbox) {
        this.map.fitBBox(item.bbox, this.getMapLeftPadding(24), 420);
      } else if (item.lat !== null && item.lon !== null) {
        this.map.flyToCoordinate(item.lat, item.lon, item.title, 13.2);
      }
      void this.openTrackInspector(item.id, item.title);
      return;
    }

    if (item.kind === "place") {
      if (item.bbox) {
        this.map.fitBBox(item.bbox, this.getMapLeftPadding(24), 48);
      } else if (item.lat !== null && item.lon !== null) {
        this.map.flyToCoordinate(item.lat, item.lon, item.title, 13.0);
      }
      if (item.lat !== null && item.lon !== null) {
        this.updateCursorReadout(item.lat, item.lon);
      }
      this.showToast(`Located ${item.title}`, "success");
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
      const regionNote = isWithinAustralia(item.lat, item.lon)
        ? ""
        : " (outside Australia)";
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
    this.map.flyToPreset(preset, this.getMapLeftPadding(16));
    this.showToast(`Centered on ${preset.shortLabel}`, "info");
  }

  private renderSidebarToggleMarkup(collapsed: boolean): string {
    if (collapsed) {
      return `
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <polygon points="12 2 2 7 12 12 22 7 12 2"></polygon>
          <polyline points="2 12 12 17 22 12"></polyline>
          <polyline points="2 17 12 22 22 17"></polyline>
        </svg>
        <span class="tf-sidebar-toggle-label">Layers</span>
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <polyline points="9 18 15 12 9 6"></polyline>
        </svg>
      `;
    }
    return `
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <polyline points="15 18 9 12 15 6"></polyline>
      </svg>
    `;
  }

  private setSidebarCollapsed(
    collapsed: boolean,
    options?: { persist?: boolean }
  ): void {
    this.sidebarCollapsed = collapsed;
    this.shellEl.classList.toggle("sidebar-collapsed", collapsed);
    this.collapseBtnEl.setAttribute("aria-expanded", String(!collapsed));
    this.collapseBtnEl.setAttribute("aria-hidden", String(!collapsed));
    this.collapseBtnEl.tabIndex = collapsed ? 0 : -1;
    this.sidebar.updateCollapsedState(collapsed);
    this.topbar.updateSidebarState(collapsed);
    if (options?.persist) {
      saveStoredSidebarCollapsed(collapsed);
    }
    window.setTimeout(() => this.map.resize(), 230);
  }

  private applySidebarWidth(widthPx: number, persist: boolean): void {
    const viewportWidth =
      typeof window !== "undefined" ? window.innerWidth : undefined;
    const clampedForViewport = clampSidebarWidth(widthPx, viewportWidth);
    const maxForViewport = getSidebarMaxWidth(viewportWidth);
    this.activeSidebarWidthPx = clampedForViewport;

    if (persist) {
      this.preferredSidebarWidthPx = saveStoredSidebarWidth(clampedForViewport);
    }

    if (isSidebarResizeEnabled(viewportWidth)) {
      this.shellEl.style.setProperty("--sidebar-w", `${clampedForViewport}px`);
    } else {
      this.shellEl.style.removeProperty("--sidebar-w");
    }

    this.sidebar.updateWidthMeta(clampedForViewport, maxForViewport);
  }

  private syncSidebarWidthForViewport(): void {
    this.applySidebarWidth(this.preferredSidebarWidthPx, false);
  }

  private getMapLeftPadding(gutterPx: number): number {
    return getSidebarMapLeftPadding(
      this.sidebarCollapsed,
      this.activeSidebarWidthPx,
      gutterPx
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
