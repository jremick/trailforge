import "./styles.css";
import { MobileBottomSheetController } from "./bottomSheet";
import { uiIcon } from "./icons";
import {
  applyUiThemeToDocument,
  getNextUiTheme,
  getThemeLabel,
  resolveInitialUiTheme,
  saveStoredUiTheme,
  type UiTheme,
} from "./themeHelpers";
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
  fetchAdminUiSettings,
  fetchAdminUsers,
  fetchAuthConfig,
  fetchAuthMe,
  fetchClosureDetail,
  fetchClosuresGeoJson,
  fetchClosuresStatus,
  fetchFavorites,
  fetchHealth,
  fetchHotspotsGeoJson,
  fetchPacksCatalog,
  fetchPins,
  fetchPois,
  fetchPublicUiConfig,
  fetchReportsGeoJson,
  fetchTileManifest,
  fetchTrackDetail,
  fetchTracksGeoJson,
  fetchTrips,
  fetchUnmappedCandidatesGeoJson,
  fetchUserPreferences,
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
  resetAdminUiSettings,
  resetUserPreferences,
  revokeAdminInvite,
  saveRoute,
  triggerAdminClosuresSync,
  triggerAdminGradingRecompute,
  updateAdminUiSettings,
  updateSavedRoute,
  updateTrip,
  updateUserPreferences,
  updateVehicle,
  uploadMobileTripSync,
  uploadTripGpx,
  uploadTripJson,
  usesSessionBearer,
  voteConditionReport,
} from "./api";
import type {
  AccountPanelTab,
  RigAccountPanel,
} from "./components/accountPanel";
import type {
  AdminConsolePanel,
  AdminPanelTab,
} from "./components/adminPanel";
import { ClosureInspectorDrawer } from "./components/closureInspector";
import { renderAttributionFooter } from "./components/footer";
import { TrackInspectorDrawer } from "./components/inspector";
import type { OfflinePacksPanel } from "./components/packsPanel";
import { InCabRecorderHud } from "./components/recorder";
import type { RoutePlannerDrawer } from "./components/routePlanner";
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
  downloadRegionalPackToDevice,
  getActiveOfflinePackId,
  listDownloadedPacks,
  removeOfflinePack,
  setActiveOfflinePackId,
} from "./offline/packStore";
import {
  deleteQueuedPin,
  enqueueOfflinePin,
  enqueueOfflineReport,
  enqueueOfflineTrip,
  flushSyncQueue,
  generateClientUuid,
  listSyncQueue,
  pinsFromSyncQueue,
} from "./offline/syncQueue";
import {
  CANONICAL_LAYER_ORDER,
  DEFAULT_PUBLIC_UI_CONFIG,
  clearStoredUserLayerPrefs,
  extractLayerVisibilityMap,
  loadStoredUserLayerPrefs,
  resolveEffectiveLayerPreferences,
  saveStoredUserLayerPrefs,
  shouldShowStackTelemetrySection,
  type StoredUserLayerPrefs,
} from "./layerPrefsHelpers";
import {
  BASEMAP_OPTIONS,
  DEFAULT_SNOWLINE_ELEVATION_M,
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
  AdminUiSettingsResponse,
  AdminUserItem,
  AuthConfigResponse,
  AuthUser,
  BasemapId,
  ClosureDetailResponse,
  ClosureItem,
  DownloadedPackRecord,
  HealthState,
  LayerKey,
  LayerVisibilityState,
  MapClosureClickPayload,
  MapPlaceClickPayload,
  OfflinePackRegionItem,
  PlannedRouteAnalysis,
  PublicUiConfigResponse,
  RecordedBreadcrumbPoint,
  RegionPreset,
  RouteWaypointInput,
  SavedRouteItem,
  SyncQueueItem,
  TileManifestResponse,
  TrackInspectorDetail,
  TripItem,
  UnifiedSearchItem,
  UserFavoriteItem,
  UserPinInput,
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
import {
  buildAppUrlSearch,
  parseAppUrlState,
  type ParsedAppUrlState,
} from "./urlState";

const HEALTH_POLL_INTERVAL_MS = 15000;
const CLOSURES_POLL_INTERVAL_MS = 60000;
const ANON_RIG_STORAGE_KEY = "trailforge.anon_rig_class";
const DEMO_BANNER_DISMISSED_KEY = "trailforge.dismissed_demo_banner";
const ACCOUNT_CHANGE_KEY = "trailforge.account_change";
const PENDING_SIGN_OUT_KEY = "trailforge.pending_sign_out";

function loadPendingSignOut(): string | null {
  if (usesSessionBearer()) return null;
  try {
    const val = window.localStorage.getItem(PENDING_SIGN_OUT_KEY);
    return val && val.trim() ? val.trim() : null;
  } catch {
    return null;
  }
}

function loadLocalSignOut(): boolean {
  return loadPendingSignOut() !== null;
}

function announceAccountChange(
  state: "signed-in" | "signed-out" | "signed-out-complete"
): void {
  if (usesSessionBearer()) return;
  try {
    window.localStorage.setItem(ACCOUNT_CHANGE_KEY, `${state}:${generateClientUuid()}`);
  } catch (err) {
    // Visible tabs also recheck the session when they regain focus.
    console.warn("Unable to broadcast account change to other tabs:", err);
  }
}

function persistPendingSignOut(ownerId: string | null): void {
  if (usesSessionBearer()) return;
  try {
    if (ownerId) window.localStorage.setItem(PENDING_SIGN_OUT_KEY, ownerId);
    else window.localStorage.removeItem(PENDING_SIGN_OUT_KEY);
  } catch (err) {
    // The current tab can still retry its own queued sign-out.
    console.warn("Unable to persist pending sign-out state locally:", err);
  }
}

async function withBrowserAuthLock<T>(action: () => Promise<T>): Promise<T> {
  if (!usesSessionBearer() && navigator.locks) {
    return await navigator.locks.request("trailforge-auth", action);
  }
  return action();
}

async function withAccountSyncLock<T>(
  ownerId: string,
  action: () => Promise<T>
): Promise<T> {
  if (typeof navigator !== "undefined" && navigator.locks) {
    return await navigator.locks.request(`trailforge-sync:${ownerId}`, action);
  }
  return action();
}

interface AccountRequest {
  ownerId: string;
  signal: AbortSignal;
}

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
  private readonly closureInspector: ClosureInspectorDrawer;
  private readonly accountPanelEl: HTMLElement;
  private readonly adminPanelEl: HTMLElement;
  private readonly packsPanelEl: HTMLElement;
  private readonly routePanelEl: HTMLElement;
  private accountPanel: RigAccountPanel | null = null;
  private accountPanelPromise: Promise<RigAccountPanel> | null = null;
  private adminPanel: AdminConsolePanel | null = null;
  private adminPanelPromise: Promise<AdminConsolePanel> | null = null;
  private packsPanel: OfflinePacksPanel | null = null;
  private packsPanelPromise: Promise<OfflinePacksPanel> | null = null;
  private routePlanner: RoutePlannerDrawer | null = null;
  private routePlannerPromise: Promise<RoutePlannerDrawer> | null = null;
  private readonly recorderHud: InCabRecorderHud;
  private readonly map: TrailForgeMap;
  private readonly demoBannerEl: HTMLElement;
  private readonly demoBannerTextEl: HTMLElement;
  private readonly mobileScrimEl: HTMLElement;
  private readonly basemapWrapEl: HTMLElement;
  private readonly basemapMobileBtnEl: HTMLButtonElement;
  private readonly basemapMobileLabelEl: HTMLElement;
  private readonly cursorCoordsEl: HTMLElement;
  private readonly cursorMgrsEl: HTMLElement;
  private readonly offlineBannerEl: HTMLElement;
  private readonly pinDropBannerEl: HTMLElement;
  private readonly routePickBannerEl: HTMLElement;
  private readonly toastContainerEl: HTMLElement;
  private readonly basemapButtons: NodeListOf<HTMLButtonElement>;
  private readonly sheetControllers: {
    inspector: MobileBottomSheetController;
    closure: MobileBottomSheetController;
    account: MobileBottomSheetController;
    admin: MobileBottomSheetController;
    packs: MobileBottomSheetController;
    route: MobileBottomSheetController;
  };

  private activeTheme: UiTheme = resolveInitialUiTheme();
  private sidebarCollapsed = false;
  private preferredSidebarWidthPx: number = SIDEBAR_DEFAULT_WIDTH_PX;
  private activeSidebarWidthPx: number = SIDEBAR_DEFAULT_WIDTH_PX;
  private activePreset: RegionPreset = getDefaultPreset();
  private activeBasemap: BasemapId;
  private currentTileManifest: TileManifestResponse | null = null;
  private uiConfig: PublicUiConfigResponse = {
    default_layer_order: [...DEFAULT_PUBLIC_UI_CONFIG.default_layer_order],
    default_layer_visibility: {
      ...DEFAULT_PUBLIC_UI_CONFIG.default_layer_visibility,
    },
    show_stack_telemetry_public:
      DEFAULT_PUBLIC_UI_CONFIG.show_stack_telemetry_public,
  };
  private userLayerPrefs: StoredUserLayerPrefs = loadStoredUserLayerPrefs();
  private layerOrder: LayerKey[] = [...CANONICAL_LAYER_ORDER];
  private layerState: LayerVisibilityState = createInitialLayerState();
  private hasCustomLayerPrefs = false;
  private userPrefsSaveTimer: number | null = null;
  private healthAbort: AbortController | null = null;
  private inspectorAbort: AbortController | null = null;
  private closureInspectorAbort: AbortController | null = null;
  private activeOverlappingClosures: ClosureDetailResponse[] = [];

  // Bookmarkable / shareable URL state
  private readonly initialUrlState: ParsedAppUrlState;
  private urlTrackId: string | null = null;
  private urlRegionId: string | null = null;
  private urlProfileOpen = false;
  private urlRouteTrackIds: string[] = [];
  private urlRigClass: VehicleCapabilityClass | null = null;
  private urlView: { lat: number; lon: number; zoom: number } | null = null;
  private suppressUrlSync = false;
  private privateMapFocus = false;

  // Slice 05 + 06 + 08 + v1.1.0 User, Access Control, Admin & Vehicle state
  private user: AuthUser | null = null;
  private accountAbort = new AbortController();
  private authCheck: AbortController | null = null;
  private authChanging = false;
  private authTransitionPromise: Promise<void> | null = null;
  private pendingAuthRecheck = false;
  private signedOutLocally = loadLocalSignOut();
  private pendingSignOutOwner = loadPendingSignOut();
  private authConfig: AuthConfigResponse = { ...DEFAULT_AUTH_CONFIG };
  private dismissedDemoBanner: string = loadDismissedDemoBanner();
  private adminStats: AdminStatsResponse | null = null;
  private adminInvites: AdminInviteItem[] = [];
  private adminUsers: AdminUserItem[] = [];
  private adminUiSettings: AdminUiSettingsResponse | null = null;
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
  private guestPins: UserPinItem[] = [];
  private isSyncingQueue = false;
  private offlineRefreshRevision = 0;

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
    applyUiThemeToDocument(this.activeTheme);
    const initiallyOnline =
      typeof navigator === "undefined" ? true : navigator.onLine;
    this.isOnline = initiallyOnline;
    this.activeBasemap = initiallyOnline ? "opentopo" : "offline-topo";

    this.initialUrlState =
      typeof window !== "undefined"
        ? parseAppUrlState(window.location.search)
        : {
            trackId: null,
            regionId: null,
            lat: null,
            lon: null,
            zoom: null,
            profileOpen: false,
            routeTrackIds: [],
            rigClass: null,
          };

    if (this.initialUrlState.regionId) {
      const parsedPreset = getPresetById(this.initialUrlState.regionId);
      if (parsedPreset) {
        this.activePreset = parsedPreset;
      }
    }

    if (this.initialUrlState.rigClass) {
      this.anonymousRigClass = this.initialUrlState.rigClass;
      this.urlRigClass = this.initialUrlState.rigClass;
    }
    if (this.initialUrlState.routeTrackIds.length > 0) {
      this.routeTrackIds = [...this.initialUrlState.routeTrackIds];
      this.urlRouteTrackIds = [...this.initialUrlState.routeTrackIds];
    }

    this.urlTrackId = this.initialUrlState.trackId;
    this.urlRegionId = this.initialUrlState.regionId;
    this.urlProfileOpen = this.initialUrlState.profileOpen;
    if (
      this.initialUrlState.lat !== null &&
      this.initialUrlState.lon !== null
    ) {
      this.urlView = {
        lat: this.initialUrlState.lat,
        lon: this.initialUrlState.lon,
        zoom: this.initialUrlState.zoom ?? this.activePreset.zoom,
      };
    }

    const initialCenter: readonly [number, number] =
      this.initialUrlState.lat !== null && this.initialUrlState.lon !== null
        ? [this.initialUrlState.lon, this.initialUrlState.lat]
        : this.activePreset.center;
    const initialZoom =
      this.initialUrlState.lat !== null && this.initialUrlState.lon !== null
        ? (this.initialUrlState.zoom ?? this.activePreset.zoom)
        : this.activePreset.zoom;

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

    const initialResolvedPrefs = resolveEffectiveLayerPreferences(
      this.uiConfig,
      this.userLayerPrefs,
      DEFAULT_SNOWLINE_ELEVATION_M
    );
    this.layerOrder = initialResolvedPrefs.layerOrder;
    this.layerState = initialResolvedPrefs.layerState;
    this.hasCustomLayerPrefs = initialResolvedPrefs.hasCustomPreferences;

    const activeBasemapMeta =
      BASEMAP_OPTIONS.find((b) => b.id === this.activeBasemap) ?? BASEMAP_OPTIONS[0]!;

    root.innerHTML = `
      <div class="tf-shell ${this.sidebarCollapsed ? "sidebar-collapsed" : ""}" data-role="shell">
        <header data-role="topbar"></header>

        <div class="tf-demo-banner hidden" data-role="demo-banner" role="status">
          <span class="tf-demo-banner-copy">
            <span class="tf-demo-banner-dot" aria-hidden="true"></span>
            <span class="tf-demo-banner-text" data-role="demo-banner-text"></span>
          </span>
          <button
            type="button"
            class="tf-demo-banner-dismiss"
            data-role="demo-banner-dismiss"
            title="Dismiss notice"
            aria-label="Dismiss notice"
          >${uiIcon("close", 13)}</button>
        </div>

        <main class="tf-workspace">
          <div class="tf-map-container" data-role="map"></div>
          <div class="tf-mobile-scrim hidden" data-role="mobile-scrim" aria-hidden="true"></div>

          <aside data-role="sidebar"></aside>
          <aside data-role="inspector"></aside>
          <aside data-role="closure-inspector"></aside>
          <aside data-role="account-panel"></aside>
          <aside data-role="admin-panel"></aside>
          <aside data-role="packs-panel"></aside>
          <aside data-role="route-panel"></aside>

          <div data-role="recorder-hud"></div>

          <div class="tf-map-toolbar">
            <div class="tf-basemap-wrap" data-role="basemap-wrap">
              <button
                type="button"
                class="tf-basemap-mobile-btn"
                data-role="basemap-mobile-btn"
                aria-haspopup="true"
                aria-expanded="false"
                title="Switch map basemap style"
              >
                ${uiIcon("map", 14)}
                <span data-role="basemap-mobile-label">${escapeHtml(activeBasemapMeta.label)}</span>
                ${uiIcon("chevronDown", 13)}
              </button>
              <div class="tf-basemap-switcher" role="group" aria-label="Basemap switcher">
                ${BASEMAP_OPTIONS.map(
                  (b) =>
                    `<button type="button" class="tf-basemap-btn ${b.id === this.activeBasemap ? "active" : ""}" data-basemap="${escapeHtml(b.id)}" title="${escapeHtml(b.hint)}">${escapeHtml(b.label)}</button>`
                ).join("")}
              </div>
            </div>
            <div class="tf-map-actions" role="group" aria-label="Map navigation controls">
              <button type="button" class="tf-map-icon-btn" data-map-action="zoom-in" title="Zoom in" aria-label="Zoom in">+</button>
              <button type="button" class="tf-map-icon-btn" data-map-action="zoom-out" title="Zoom out" aria-label="Zoom out">−</button>
              <button type="button" class="tf-map-icon-btn" data-map-action="north" title="Reset north" aria-label="Reset north">N</button>
              <button type="button" class="tf-map-icon-btn" data-map-action="share" title="Copy shareable link to current map view" aria-label="Copy shareable link">
                ${uiIcon("share", 13)}
              </button>
            </div>
          </div>

          <div class="tf-offline-banner ${initiallyOnline ? "hidden" : ""}" data-role="offline-banner" role="status">
            Offline mode: displaying built-in topo graticule &amp; corridor bounds
          </div>

          <div class="tf-pin-drop-banner hidden" data-role="pin-drop-banner" role="status">
            Click anywhere on the map to pick coordinates · Esc to cancel
          </div>

          <div class="tf-pin-drop-banner tf-route-pick-banner hidden" data-role="route-pick-banner" role="status">
            Click tracks on map to append to route · Esc to stop
          </div>

          <div class="tf-coord-hud" data-role="coord-hud">
            <span class="mono" data-role="cursor-coords">${formatCoords(initialCenter[1], initialCenter[0], 4)}</span>
            <span class="tf-hud-sep" aria-hidden="true">|</span>
            <span class="mono tf-accent-cyan" data-role="cursor-mgrs">${latLonToMgrs(initialCenter[1], initialCenter[0], 4)}</span>
          </div>
        </main>

        <footer data-role="footer"></footer>
        <div class="tf-toast-container" data-role="toasts" aria-live="polite"></div>
      </div>
    `;

    this.shellEl = this.q(root, "[data-role='shell']");
    this.demoBannerEl = this.q(root, "[data-role='demo-banner']");
    this.demoBannerTextEl = this.q(root, "[data-role='demo-banner-text']");
    this.mobileScrimEl = this.q(root, "[data-role='mobile-scrim']");
    this.basemapWrapEl = this.q(root, "[data-role='basemap-wrap']");
    this.basemapMobileBtnEl = this.q<HTMLButtonElement>(
      root,
      "[data-role='basemap-mobile-btn']"
    );
    this.basemapMobileLabelEl = this.q(root, "[data-role='basemap-mobile-label']");
    this.cursorCoordsEl = this.q(root, "[data-role='cursor-coords']");
    this.cursorMgrsEl = this.q(root, "[data-role='cursor-mgrs']");
    this.offlineBannerEl = this.q(root, "[data-role='offline-banner']");
    this.pinDropBannerEl = this.q(root, "[data-role='pin-drop-banner']");
    this.routePickBannerEl = this.q(root, "[data-role='route-pick-banner']");
    this.toastContainerEl = this.q(root, "[data-role='toasts']");
    this.basemapButtons = root.querySelectorAll<HTMLButtonElement>("[data-basemap]");

    this.topbar = new TopBar(
      this.q(root, "[data-role='topbar']"),
      this.activePreset.id,
      {
        onToggleSidebar: () => {
          this.setSidebarCollapsed(!this.sidebarCollapsed, { persist: true });
        },
        onToggleTheme: () => {
          this.toggleUiTheme();
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
          if (this.routePlanner?.isPanelOpen()) {
            this.closeRoutePlanner();
          } else {
            void this.openRoutePlanner();
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
          if (this.accountPanel?.isPanelOpen()) {
            this.closeAccountPanel();
          } else {
            void this.openAccountPanel("rig");
          }
        },
        onOfflinePacksClick: () => {
          if (this.packsPanel?.isPanelOpen()) {
            this.closePacksPanel();
          } else {
            void this.openPacksPanel();
          }
        },
        onAdminClick: () => {
          if (this.adminPanel?.isPanelOpen()) {
            this.closeAdminPanel();
          } else {
            void this.openAdminPanel("overview");
          }
        },
      },
      { initialTheme: this.activeTheme }
    );

    this.sidebar = new Sidebar(
      this.q(root, "[data-role='sidebar']"),
      this.layerState,
      this.activePreset,
      {
        onLayerToggle: (key, enabled) => {
          this.layerState = { ...this.layerState, [key]: enabled };
          this.map.setLayerVisibility(this.layerState);
          this.persistUserLayerPreferenceChange({
            layerVisibility: {
              ...extractLayerVisibilityMap(this.layerState),
              [key]: enabled,
            },
          });
          if (key === "unmapped" && enabled) {
            void this.refreshUnmappedCandidates();
          }
          if (key === "hotspots" && enabled) {
            void this.refreshHotspotsGeoJson();
          }
        },
        onLayerOrderChange: (nextOrder) => {
          this.layerOrder = [...nextOrder];
          this.persistUserLayerPreferenceChange({
            layerOrder: [...nextOrder],
          });
        },
        onResetLayerPreferences: () => {
          void this.handleResetUserLayerPreferences();
        },
        onSnowLineElevationChange: (elevationM) => {
          this.layerState = { ...this.layerState, snowLineElevationM: elevationM };
          this.map.setLayerVisibility(this.layerState);
          this.inspector.setSnowLineElevation(elevationM);
          this.syncRoutePlannerUiState();
        },
        onPresetChange: (presetId) => {
          this.selectPreset(presetId);
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
      },
      {
        initialOrder: this.layerOrder,
        hasCustomLayerPrefs: this.hasCustomLayerPrefs,
        showTelemetrySection: shouldShowStackTelemetrySection(
          this.user,
          this.uiConfig.show_stack_telemetry_public
        ),
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
          this.syncUrlState({ trackId: null, profileOpen: false }, "push");
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
        onOpenClosure: (closure: ClosureItem) => {
          const cid = closure.closure_id ?? closure.id;
          if (cid === undefined || cid === null) return;
          const summary: ClosureDetailResponse = {
            closure_id: cid,
            feed_source: closure.feed_source,
            agency: closure.agency,
            external_id: closure.external_id,
            title: closure.title,
            description: closure.description,
            closure_type: closure.closure_type,
            severity: closure.severity,
            status: closure.status,
            state: closure.state,
            effective_from: closure.effective_from,
            effective_to: closure.effective_to,
            source_url: closure.source_url,
            last_updated_at: closure.last_updated_at,
            is_stale: closure.is_stale,
            capad_park_name: closure.capad_park_name,
          };
          void this.openClosureInspector(summary, [summary]);
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
        onShareTrack: (trackId) => {
          void this.handleShareTrack(trackId);
        },
        onElevationModalChange: (open) => {
          this.syncUrlState({ profileOpen: open }, "replace");
        },
        onPickReportLocation: () => {
          this.setCoordPickMode("report");
          this.shellEl.classList.remove("inspector-open");
        },
        onSubmitReport: async (input) => {
          const request = this.requireAccountRequest();
          const report = { ...input, client_uuid: input.client_uuid ?? generateClientUuid() };
          if (this.isOnline) {
            try {
              await createConditionReport(report, request);
              if (request.signal.aborted) return;
              this.pickedReportCoords = null;
              this.showToast("Trail condition report published", "success");
              await this.refreshReportsGeoJson();
              if (!request.signal.aborted) this.refreshOpenInspectorForRig();
              return;
            } catch (err) {
              if (request.signal.aborted) return;
              if (err instanceof ApiError && err.status !== 503 && err.status !== 0) throw err;
            }
          }
          const { photo: _omit, ...offlinePayload } = report;
          await enqueueOfflineReport(request.ownerId, offlinePayload);
          await this.refreshOfflineLocalState();
          if (request.signal.aborted) return;
          this.pickedReportCoords = null;
          this.showToast("Condition report queued to sync when online", "info");
        },
        onVoteReport: async (reportId, vote) => {
          const request = this.accountRequest();
          if (!request) return;
          try {
            await voteConditionReport(reportId, vote, request);
            if (request.signal.aborted) return;
            this.showToast(
              vote === "confirm"
                ? "Report confirmed — freshness refreshed"
                : "Marked hazard resolved",
              "success"
            );
            await this.refreshReportsGeoJson();
            if (!request.signal.aborted) this.refreshOpenInspectorForRig();
          } catch (err) {
            if (request.signal.aborted) return;
            const msg = err instanceof Error ? err.message : "Vote failed";
            this.showToast(msg, "error");
          }
        },
      }
    );

    this.closureInspector = new ClosureInspectorDrawer(
      this.q(root, "[data-role='closure-inspector']"),
      {
        onClose: () => {
          this.closureInspectorAbort?.abort();
          this.shellEl.classList.remove("closure-open");
          this.map.setSelectedClosureId(null);
        },
        onZoomToClosure: (bbox) => {
          this.map.fitBBox(bbox, this.getMapLeftPadding(24), 420);
        },
        onOpenTrack: (trackId, trackName) => {
          this.closeClosureInspector();
          void this.openTrackInspector(trackId, trackName, {
            autoZoomIfNoCoords: true,
          });
        },
        onSelectOverlapping: (chosen) => {
          void this.openClosureInspector(
            chosen,
            this.activeOverlappingClosures
          );
        },
      }
    );

    this.accountPanelEl = this.q(root, "[data-role='account-panel']");
    this.adminPanelEl = this.q(root, "[data-role='admin-panel']");
    this.packsPanelEl = this.q(root, "[data-role='packs-panel']");
    this.routePanelEl = this.q(root, "[data-role='route-panel']");

    this.sheetControllers = {
      inspector: new MobileBottomSheetController(
        this.q(root, "[data-role='inspector']"),
        {
          onDismiss: () => {
            this.inspector.close();
            this.shellEl.classList.remove("inspector-open");
            this.map.setSelectedTrackId(null);
            this.pickedReportCoords = null;
            this.syncUrlState({ trackId: null, profileOpen: false }, "push");
          },
        }
      ),
      closure: new MobileBottomSheetController(
        this.q(root, "[data-role='closure-inspector']"),
        { onDismiss: () => this.closeClosureInspector() }
      ),
      account: new MobileBottomSheetController(this.accountPanelEl, {
        onDismiss: () => this.closeAccountPanel(),
      }),
      admin: new MobileBottomSheetController(this.adminPanelEl, {
        onDismiss: () => this.closeAdminPanel(),
      }),
      packs: new MobileBottomSheetController(this.packsPanelEl, {
        onDismiss: () => this.closePacksPanel(),
      }),
      route: new MobileBottomSheetController(this.routePanelEl, {
        onDismiss: () => this.closeRoutePlanner(),
      }),
    };

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
          const request = this.requireAccountRequest();
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
            confirm_community: params.visibility === "community_anonymous" ? true : undefined,
            privacy_trim_m: params.privacy_trim_m,
            client_uuid: clientUuid,
            client_session_id: clientUuid,
          };

          if (this.isOnline) {
            let uploaded: TripItem | null = null;
            try {
              uploaded = await uploadMobileTripSync(payload, request);
            } catch (err) {
              if (request.signal.aborted) return;
              if (err instanceof ApiError && err.status !== 503 && err.status !== 0) throw err;
            }
            if (request.signal.aborted) return;
            if (uploaded) {
              this.latestUploadedTrip = uploaded;
              await this.reloadTrips(request).catch(() => undefined);
              if (request.signal.aborted) return;
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
            }
          }

          await enqueueOfflineTrip(request.ownerId, payload);
          await this.refreshOfflineLocalState();
          if (request.signal.aborted) return;
          this.showToast("Recorded trip queued to sync when online", "info");
        },
        onToast: (msg, kind) => this.showToast(msg, kind),
      }
    );

    renderAttributionFooter(this.q(root, "[data-role='footer']"));

    this.map = new TrailForgeMap(
      this.q(root, "[data-role='map']"),
      {
        onCursorMove: (lat, lon) => this.updateCursorReadout(lat, lon),
        onMapMoveEnd: (lat, lon, zoom) => {
          if (!this.privateMapFocus) this.syncUrlState({ lat, lon, zoom }, "replace");
        },
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
          this.closeClosureInspector();
          this.closeAccountPanel();
          this.closeAdminPanel();
          this.closeRoutePlanner();
          void this.openTrackInspector(trackId);
        },
        onClosureClick: (payload: MapClosureClickPayload) => {
          void this.openClosureInspector(payload.primary, payload.overlapping);
        },
        onPlaceClick: (payload: MapPlaceClickPayload) => {
          this.handlePlaceClick(payload);
        },
        onPinDropClick: (lat, lon) => {
          this.setPrivateMapFocus();
          this.pendingPinCoords = { lat, lon };
          this.setCoordPickMode(null);
          this.openAccountPanel("saved");
          this.showToast(
            `Pin location picked (${lat.toFixed(4)}, ${lon.toFixed(4)})`,
            "info"
          );
        },
        onReportPickClick: (lat, lon) => {
          this.setPrivateMapFocus();
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
        initialCenter,
        initialZoom,
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

  private accountRequest(): AccountRequest | undefined {
    return this.user ? { ownerId: this.user.id, signal: this.accountAbort.signal } : undefined;
  }

  private requireAccountRequest(): AccountRequest {
    const request = this.accountRequest();
    if (!request) throw new Error("Sign in to continue");
    return request;
  }

  private get offlinePins(): UserPinItem[] {
    return this.user ? pinsFromSyncQueue(this.syncQueue) : this.guestPins;
  }

  private setPrivateMapFocus(): void {
    this.privateMapFocus = true;
    this.syncUrlState({ lat: null, lon: null, zoom: null }, "replace");
  }

  private clearAccountState(user: AuthUser | null, preserveStartupView = false): void {
    const hadUser = this.user !== null;
    this.accountAbort.abort();
    this.accountAbort = new AbortController();
    this.user = user;
    this.vehicles = [];
    this.trips = [];
    this.latestUploadedTrip = null;
    this.favorites = [];
    this.pins = [];
    this.guestPins = [];
    this.syncQueue = [];
    this.isSyncingQueue = false;
    this.savedRoutes = [];
    this.savedRoutesLoading = false;
    this.adminStats = null;
    this.adminInvites = [];
    this.adminUsers = [];
    this.adminUiSettings = null;
    this.latestCreatedInvite = null;
    this.closeAdminPanel();
    this.adminPanel?.updateState({
      currentUserId: user?.id ?? null,
      stats: null,
      invites: [],
      users: [],
      uiSettings: null,
      latestCreatedInvite: null,
      loading: false,
      busyAction: null,
      error: null,
    });
    this.adminPanelEl.replaceChildren();
    this.resetLocalLayerPreferences();
    this.pendingPinCoords = null;
    this.pickedReportCoords = null;
    this.setCoordPickMode(null);
    this.recorderHud.resetForAccountChange();
    if (preserveStartupView) this.inspector.resetReportForm();
    this.toastContainerEl.replaceChildren();

    if (!preserveStartupView) {
      this.inspectorAbort?.abort();
      this.inspector.close();
      this.currentTrackDetail = null;
      this.shellEl.classList.remove("inspector-open");
      this.map.setSelectedTrackId(null);
      this.clearPlannedRoute();
      this.closeRoutePlanner();
      this.routePanelEl.replaceChildren();
      const wasPrivateMapFocus = this.privateMapFocus;
      this.privateMapFocus = false;
      this.map.clearLocationPopups();
      if (wasPrivateMapFocus) {
        const { center, zoom } = this.activePreset;
        this.map.setView(center[1], center[0], zoom);
        this.updateCursorReadout(center[1], center[0]);
      } else {
        this.cursorCoordsEl.textContent = "—";
        this.cursorMgrsEl.textContent = "—";
      }
      this.suppressUrlSync = false;
      this.syncUrlState({ trackId: null, routeTrackIds: [], lat: null, lon: null, zoom: null });
    }
    this.renderCombinedPinsOnMap();
    this.syncRigUiState();
    this.syncOfflinePanelState();
    if (!this.packsPanel?.isPanelOpen()) this.packsPanelEl.replaceChildren();
    this.syncRoutePlannerUiState();
    if (hadUser && user === null) {
      void this.refreshReportsGeoJson();
    }
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

  private ensureAccountPanel(): Promise<RigAccountPanel> {
    if (this.accountPanel) return Promise.resolve(this.accountPanel);
    if (!this.accountPanelPromise) {
      this.accountPanelPromise = import("./components/accountPanel").then(
        ({ RigAccountPanel }) => {
          const panel = new RigAccountPanel(
            this.accountPanelEl,
            this.buildAccountPanelState(),
            {
              onClose: () => this.closeAccountPanel(),
              onOpenAdminConsole: () => void this.openAdminPanel("overview"),
              onSelectAnonymousRig: (cls) => {
                this.anonymousRigClass = cls;
                saveStoredAnonRig(cls);
                this.syncUrlState({ rigClass: cls }, "replace");
                this.syncRigUiState();
                this.showToast(`Rig suitability preview set to ${cls}`, "info");
                this.refreshOpenInspectorForRig();
                this.refreshPlannedRouteForRig();
              },
              onLogin: (email, password) => this.signIn(
                () => loginAccount({ email, password }),
                (name) => `Signed in as ${name}`
              ),
              onRegister: (displayName, email, password, inviteCode) => this.signIn(
                () => registerAccount({
                  display_name: displayName,
                  email,
                  password,
                  invite_code: inviteCode ?? null,
                }),
                (name) => `Account created for ${name}`
              ),
              onLogout: () => this.signOut(),
              onSaveVehicle: async (payload, editingId) => {
                const request = this.requireAccountRequest();
                if (editingId) {
                  await updateVehicle(editingId, payload, request);
                  if (request.signal.aborted) return;
                  this.showToast(`Updated ${payload.name}`, "success");
                } else {
                  await createVehicle(payload, request);
                  if (request.signal.aborted) return;
                  this.showToast(`Saved vehicle profile ${payload.name}`, "success");
                }
                await this.reloadVehicles(request);
                if (request.signal.aborted) return;
                this.refreshOpenInspectorForRig();
                this.refreshPlannedRouteForRig();
              },
              onActivateVehicle: async (id) => {
                const request = this.requireAccountRequest();
                const updated = await activateVehicle(id, request);
                if (request.signal.aborted) return;
                await this.reloadVehicles(request);
                if (request.signal.aborted) return;
                this.showToast(
                  `Active rig: ${updated.name} (${updated.capability_class})`,
                  "success"
                );
                this.refreshOpenInspectorForRig();
                this.refreshPlannedRouteForRig();
              },
              onDeleteVehicle: async (id) => {
                const request = this.requireAccountRequest();
                await deleteVehicle(id, request);
                if (request.signal.aborted) return;
                await this.reloadVehicles(request);
                if (request.signal.aborted) return;
                this.showToast("Vehicle profile deleted", "info");
                this.refreshOpenInspectorForRig();
                this.refreshPlannedRouteForRig();
              },
              onUploadTripGpx: async (file, params) => {
                const request = this.requireAccountRequest();
                const uploaded = await uploadTripGpx(file, params, request);
                await this.handleTripUploaded(
                  uploaded,
                  request,
                  `Matched ${uploaded.matched_segment_count ?? 0} segments (${uploaded.observations_published ?? 0} published)`
                );
              },
              onUploadTripJson: async (payload) => {
                const request = this.requireAccountRequest();
                const uploaded = await uploadTripJson(payload, request);
                await this.handleTripUploaded(
                  uploaded,
                  request,
                  `Matched ${uploaded.matched_segment_count ?? 0} segments`
                );
              },
              onToggleTripVisibility: async (tripId, nextVis) => {
                const request = this.requireAccountRequest();
                await updateTrip(tripId, { visibility: nextVis }, request);
                if (request.signal.aborted) return;
                await this.reloadTrips(request);
                if (request.signal.aborted) return;
                if (this.layerState.unmapped) void this.refreshUnmappedCandidates();
                this.showToast(
                  nextVis === "private"
                    ? "Trip set to Private — anonymized observations revoked"
                    : "Trip shared as Anonymous Community telemetry",
                  "info"
                );
              },
              onExportTrip: async (tripId, fmt) => {
                const request = this.requireAccountRequest();
                const url = buildTripExportUrl(tripId, fmt);
                await downloadAuthenticatedExport(
                  url,
                  `trip_${tripId}.${fmt === "gpx" ? "gpx" : "geojson"}`,
                  request
                );
                if (request.signal.aborted) return;
                this.showToast(`Exported trip (${fmt.toUpperCase()})`, "success");
              },
              onDeleteTrip: async (tripId) => {
                const request = this.requireAccountRequest();
                await deleteTrip(tripId, request);
                if (request.signal.aborted) return;
                this.trips = this.trips.filter((t) => getTripId(t) !== tripId);
                if (
                  this.latestUploadedTrip &&
                  getTripId(this.latestUploadedTrip) === tripId
                ) {
                  this.latestUploadedTrip = null;
                }
                this.syncRigUiState();
                if (this.layerState.unmapped) void this.refreshUnmappedCandidates();
                this.showToast(
                  "Trip deleted & any public observations revoked",
                  "info"
                );
              },
              onOpenFavoriteTrack: (trackId) => {
                this.closeAccountPanel();
                void this.openTrackInspector(trackId);
              },
              onRemoveFavorite: async (trackId) => {
                const request = this.requireAccountRequest();
                await removeFavorite(trackId, request);
                if (request.signal.aborted) return;
                this.favorites = this.favorites.filter(
                  (f) => f.track_id !== trackId
                );
                this.syncRigUiState();
                this.showToast("Removed track from favorites", "info");
              },
              onTogglePinDropMode: (active) => {
                this.setCoordPickMode(active ? "pin" : null);
                if (active) {
                  this.shellEl.classList.remove("account-open");
                }
              },
              onCreatePin: (payload) => this.handleCreatePin(payload),
              onSelectPin: (pin) => {
                this.setPrivateMapFocus();
                this.map.flyToCoordinate(
                  pin.lat,
                  pin.lon,
                  pin.name ?? pin.title ?? "Pin",
                  14
                );
                this.updateCursorReadout(pin.lat, pin.lon);
              },
              onDeletePin: async (id) => {
                const request = this.accountRequest();
                const signal = this.accountAbort.signal;
                if (id.startsWith("offline-pin-")) {
                  if (request) {
                    await deleteQueuedPin(request.ownerId, id);
                  } else {
                    this.guestPins = this.guestPins.filter((pin) => pin.id !== id);
                  }
                  await this.refreshOfflineLocalState();
                  if (!signal.aborted) this.showToast("Offline pin deleted", "info");
                  return;
                }
                if (!request) return;
                await deletePin(id, request);
                if (signal.aborted) return;
                this.pins = this.pins.filter((p) => p.id !== id);
                this.renderCombinedPinsOnMap();
                this.syncRigUiState();
                this.showToast("Personal pin deleted", "info");
              },
              onExportUser: async (fmt) => {
                const request = this.requireAccountRequest();
                const url = buildUserExportUrl(fmt);
                await downloadAuthenticatedExport(
                  url,
                  `trailforge_saved.${fmt === "gpx" ? "gpx" : "geojson"}`,
                  request
                );
                if (request.signal.aborted) return;
                this.showToast(
                  `Exported saved tracks & pins (${fmt.toUpperCase()})`,
                  "success"
                );
              },
            }
          );
          this.accountPanel = panel;
          return panel;
        }
      );
    }
    return this.accountPanelPromise;
  }

  private async handleCreatePin(payload: UserPinInput): Promise<void> {
    const request = this.accountRequest();
    const signal = this.accountAbort.signal;
    const input = { ...payload, client_uuid: payload.client_uuid ?? generateClientUuid() };
    if (request && this.isOnline) {
      let created: UserPinItem | null = null;
      try {
        created = await createPin(input, request);
      } catch (err) {
        if (signal.aborted) return;
        if (err instanceof ApiError && err.status !== 503 && err.status !== 0) throw err;
      }
      if (signal.aborted) return;
      if (created) {
        this.pins = [created, ...this.pins];
        this.pendingPinCoords = null;
        this.setCoordPickMode(null);
        this.renderCombinedPinsOnMap();
        this.syncRigUiState();
        this.showToast(`Dropped pin "${payload.name}"`, "success");
        return;
      }
    }

    if (request) {
      await enqueueOfflinePin(request.ownerId, input);
    } else {
      this.guestPins = [{
        ...payload,
        id: `offline-pin-${generateClientUuid()}`,
        created_at: new Date().toISOString(),
      }, ...this.guestPins];
    }
    if (signal.aborted) return;
    this.pendingPinCoords = null;
    this.setCoordPickMode(null);
    await this.refreshOfflineLocalState();
    if (signal.aborted) return;
    this.showToast(
      request ? `Queued pin "${payload.name}" to sync` : `Dropped pin "${payload.name}" in this tab`,
      "success"
    );
  }

  private ensureAdminPanel(): Promise<AdminConsolePanel> {
    if (this.adminPanel) return Promise.resolve(this.adminPanel);
    if (!this.adminPanelPromise) {
      this.adminPanelPromise = import("./components/adminPanel").then(
        ({ AdminConsolePanel }) => {
          const panel = new AdminConsolePanel(
            this.adminPanelEl,
            {
              currentUserId: this.user?.id ?? null,
              registrationMode: this.authConfig.registration_mode,
              loading: false,
              stats: this.adminStats,
              invites: this.adminInvites,
              users: this.adminUsers,
              uiSettings: this.adminUiSettings,
              latestCreatedInvite: this.latestCreatedInvite,
              busyAction: null,
              error: null,
            },
            {
              onClose: () => this.closeAdminPanel(),
              onRefresh: () => this.refreshAdminConsoleData(),
              onCreateInvite: async (payload) => {
                const request = this.requireAccountRequest();
                panel.updateState({ busyAction: "create-invite", error: null });
                try {
                  const created = await createAdminInvite(payload, request);
                  if (request.signal.aborted) return;
                  this.latestCreatedInvite = created;
                  this.adminInvites = [created, ...this.adminInvites];
                  panel.updateState({
                    invites: this.adminInvites,
                    latestCreatedInvite: created,
                  });
                  this.showToast(`Created invite code ${created.code}`, "success");
                  void this.refreshAdminConsoleData();
                } catch (err) {
                  if (request.signal.aborted) return;
                  const msg = formatFriendlyApiError(err, "Failed to create invite");
                  panel.updateState({ error: msg });
                  this.showToast(msg, "error");
                } finally {
                  if (!request.signal.aborted) panel.updateState({ busyAction: null });
                }
              },
              onRevokeInvite: async (inviteId) => {
                const request = this.requireAccountRequest();
                panel.updateState({ busyAction: `revoke-${inviteId}`, error: null });
                try {
                  await revokeAdminInvite(inviteId, request);
                  if (request.signal.aborted) return;
                  await this.refreshAdminConsoleData();
                  if (request.signal.aborted) return;
                  this.showToast("Invite code revoked", "info");
                } catch (err) {
                  if (request.signal.aborted) return;
                  const msg = formatFriendlyApiError(err, "Failed to revoke invite");
                  panel.updateState({ error: msg });
                  this.showToast(msg, "error");
                } finally {
                  if (!request.signal.aborted) panel.updateState({ busyAction: null });
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
                const request = this.requireAccountRequest();
                panel.updateState({ busyAction: `user-${userId}`, error: null });
                try {
                  const updated = await patchAdminUser(userId, patch, request);
                  if (request.signal.aborted) return;
                  this.adminUsers = this.adminUsers.map((u) =>
                    u.id === updated.id ? updated : u
                  );
                  panel.updateState({ users: this.adminUsers });
                  this.showToast(
                    `Updated ${updated.display_name} (${updated.role}, ${updated.is_active ? "active" : "suspended"})`,
                    "success"
                  );
                  void this.refreshAdminConsoleData();
                } catch (err) {
                  if (request.signal.aborted) return;
                  const msg = formatFriendlyApiError(err, "Failed to update user");
                  panel.updateState({ error: msg });
                  this.showToast(msg, "error");
                } finally {
                  if (!request.signal.aborted) panel.updateState({ busyAction: null });
                }
              },
              onSyncClosures: async () => {
                const request = this.requireAccountRequest();
                panel.updateState({ busyAction: "sync-closures", error: null });
                try {
                  await triggerAdminClosuresSync(request);
                  if (request.signal.aborted) return;
                  await Promise.all([
                    this.refreshClosures(false),
                    this.refreshAdminConsoleData(),
                  ]);
                  if (request.signal.aborted) return;
                  this.showToast("Agency closure feeds synced", "success");
                } catch (err) {
                  if (request.signal.aborted) return;
                  const msg = formatFriendlyApiError(err, "Closure sync failed");
                  panel.updateState({ error: msg });
                  this.showToast(msg, "error");
                } finally {
                  if (!request.signal.aborted) panel.updateState({ busyAction: null });
                }
              },
              onRecomputeGrades: async () => {
                const request = this.requireAccountRequest();
                panel.updateState({ busyAction: "recompute-grades", error: null });
                try {
                  await triggerAdminGradingRecompute(request);
                  if (request.signal.aborted) return;
                  await Promise.all([
                    this.refreshHotspotsGeoJson(),
                    this.refreshAdminConsoleData(),
                  ]);
                  if (request.signal.aborted) return;
                  this.refreshOpenInspectorForRig();
                  this.showToast("Live crowdsourced grades recomputed", "success");
                } catch (err) {
                  if (request.signal.aborted) return;
                  const msg = formatFriendlyApiError(
                    err,
                    "Live grade recomputation failed"
                  );
                  panel.updateState({ error: msg });
                  this.showToast(msg, "error");
                } finally {
                  if (!request.signal.aborted) panel.updateState({ busyAction: null });
                }
              },
              onSaveUiSettings: async (payload) => {
                const request = this.requireAccountRequest();
                panel.updateState({
                  busyAction: "save-ui-settings",
                  error: null,
                });
                try {
                  const updated = await updateAdminUiSettings(payload, request);
                  if (request.signal.aborted) return;
                  this.adminUiSettings = updated;
                  this.uiConfig = {
                    default_layer_order: [...updated.default_layer_order],
                    default_layer_visibility: { ...updated.default_layer_visibility },
                    show_stack_telemetry_public: updated.show_stack_telemetry_public,
                  };
                  this.applyResolvedLayerPreferences();
                  this.syncTelemetrySectionVisibility();
                  panel.updateState({
                    uiSettings: updated,
                    busyAction: null,
                  });
                  this.showToast(
                    "Saved instance default map layers & visibility",
                    "success"
                  );
                } catch (err) {
                  if (request.signal.aborted) return;
                  const msg = formatFriendlyApiError(
                    err,
                    "Failed to save instance UI settings"
                  );
                  panel.updateState({ error: msg, busyAction: null });
                  this.showToast(msg, "error");
                }
              },
              onResetUiSettings: async () => {
                const request = this.requireAccountRequest();
                panel.updateState({
                  busyAction: "reset-ui-settings",
                  error: null,
                });
                try {
                  const reset = await resetAdminUiSettings(request);
                  if (request.signal.aborted) return;
                  this.adminUiSettings = reset;
                  this.uiConfig = {
                    default_layer_order: [...reset.default_layer_order],
                    default_layer_visibility: { ...reset.default_layer_visibility },
                    show_stack_telemetry_public: reset.show_stack_telemetry_public,
                  };
                  this.applyResolvedLayerPreferences();
                  this.syncTelemetrySectionVisibility();
                  panel.updateState({
                    uiSettings: reset,
                    busyAction: null,
                  });
                  this.showToast(
                    "Reset instance map layer defaults to factory settings",
                    "info"
                  );
                } catch (err) {
                  if (request.signal.aborted) return;
                  const msg = formatFriendlyApiError(
                    err,
                    "Failed to reset instance UI settings"
                  );
                  panel.updateState({ error: msg, busyAction: null });
                  this.showToast(msg, "error");
                }
              },
            }
          );
          this.adminPanel = panel;
          return panel;
        }
      );
    }
    return this.adminPanelPromise;
  }

  private ensurePacksPanel(): Promise<OfflinePacksPanel> {
    if (this.packsPanel) return Promise.resolve(this.packsPanel);
    if (!this.packsPanelPromise) {
      this.packsPanelPromise = import("./components/packsPanel").then(
        ({ OfflinePacksPanel }) => {
          const panel = new OfflinePacksPanel(
            this.packsPanelEl,
            {
              online: this.isOnline,
              isAdmin: canManageAdminCompute(this.user),
              loading: false,
              regions: this.packRegions,
              downloaded: this.downloadedPacks,
              activeOfflinePackId: this.activeOfflinePackId,
              syncQueue: this.syncQueue,
              syncing: this.isSyncingQueue,
              buildingRegionId: null,
              downloadingRegionId: null,
              downloadProgress: null,
              error: null,
            },
            {
              onClose: () => this.closePacksPanel(),
              onRefresh: () => this.refreshPacksCatalog(),
              onBuildPack: async (regionId) => {
                const request = this.requireAccountRequest();
                panel.updateState({ buildingRegionId: regionId, error: null });
                try {
                  await buildRegionPack(regionId, request);
                  if (request.signal.aborted) return;
                  await this.refreshPacksCatalog();
                  if (request.signal.aborted) return;
                  this.showToast(
                    `Built offline regional pack: ${regionId}`,
                    "success"
                  );
                } catch (err) {
                  if (request.signal.aborted) return;
                  const msg = formatFriendlyApiError(err, "Failed to build pack");
                  panel.updateState({ error: msg });
                  this.showToast(msg, "error");
                } finally {
                  if (!request.signal.aborted) panel.updateState({ buildingRegionId: null });
                }
              },
              onDownloadPack: async (region) => {
                panel.updateState({
                  downloadingRegionId: region.region_id,
                  downloadProgress: { stage: "Starting download…", pct: 5 },
                  error: null,
                });
                try {
                  const record = await downloadRegionalPackToDevice(
                    region,
                    (prog) => {
                      panel.updateState({ downloadProgress: prog });
                    }
                  );
                  await this.refreshOfflineLocalState();
                  this.showToast(
                    `Downloaded ${record.name} for offline use (${record.track_count} tracks)`,
                    "success"
                  );
                } catch (err) {
                  const msg =
                    err instanceof Error ? err.message : "Pack download failed";
                  panel.updateState({ error: msg });
                  this.showToast(msg, "error");
                } finally {
                  panel.updateState({
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
          this.packsPanel = panel;
          return panel;
        }
      );
    }
    return this.packsPanelPromise;
  }

  private ensureRoutePlanner(): Promise<RoutePlannerDrawer> {
    if (this.routePlanner) return Promise.resolve(this.routePlanner);
    if (!this.routePlannerPromise) {
      this.routePlannerPromise = import("./components/routePlanner").then(
        ({ RoutePlannerDrawer }) => {
          const panel = new RoutePlannerDrawer(
            this.routePanelEl,
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
                this.syncUrlState(
                  { routeTrackIds: [...this.routeTrackIds] },
                  "replace"
                );
                void this.recomputePlannedRoute(false);
              },
              onMoveLegDown: (idx) => {
                this.routeTrackIds = moveRouteTrackDown(this.routeTrackIds, idx);
                this.syncUrlState(
                  { routeTrackIds: [...this.routeTrackIds] },
                  "replace"
                );
                void this.recomputePlannedRoute(false);
              },
              onRemoveLeg: (idx) => {
                this.routeTrackIds = removeRouteTrackAt(this.routeTrackIds, idx);
                this.syncUrlState(
                  { routeTrackIds: [...this.routeTrackIds] },
                  "replace"
                );
                void this.recomputePlannedRoute(false);
              },
              onClearRoute: () => {
                this.clearPlannedRoute();
                this.showToast("Cleared active route", "info");
              },
              onInspectTrack: (trackId) => {
                this.closeRoutePlanner();
                void this.openTrackInspector(trackId);
              },
              onZoomToRoute: (bbox) => {
                if (this.user) this.setPrivateMapFocus();
                this.map.fitBBox(bbox, this.getMapLeftPadding(24), 420);
              },
              onPoiSelect: (poi) => {
                if (this.user) this.setPrivateMapFocus();
                this.map.flyToCoordinate(poi.lat, poi.lon, poi.name, 14);
                this.updateCursorReadout(poi.lat, poi.lon);
                this.showToast(`Located ${poi.name}`, "info");
              },
              onOpenRigPanel: () => {
                void this.openAccountPanel("rig");
              },
              onOpenSignIn: () => {
                void this.openAccountPanel("account");
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
          this.routePlanner = panel;
          return panel;
        }
      );
    }
    return this.routePlannerPromise;
  }

  private syncOfflinePanelState(): void {
    this.packsPanel?.updateState({
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
    const revision = ++this.offlineRefreshRevision;
    const signal = this.accountAbort.signal;
    const ownerId = this.user?.id;
    const [downloaded, queue] = await Promise.all([
      listDownloadedPacks().catch(() => []),
      ownerId ? listSyncQueue(ownerId).catch(() => []) : [],
    ]);
    if (signal.aborted || revision !== this.offlineRefreshRevision) return;
    this.downloadedPacks = downloaded;
    this.syncQueue = queue;
    this.renderCombinedPinsOnMap();
    this.syncOfflinePanelState();
    this.syncRigUiState();
  }

  private async refreshPacksCatalog(): Promise<void> {
    this.packsPanel?.updateState({ loading: true, error: null });
    try {
      const catalog = await fetchPacksCatalog();
      this.packRegions = catalog.regions ?? [];
    } catch {
      // Keep cached fallback regions cleanly when offline
    } finally {
      this.packsPanel?.updateState({ loading: false });
      await this.refreshOfflineLocalState();
    }
  }

  private async handleFlushSyncQueue(announce: boolean): Promise<void> {
    const request = this.accountRequest();
    if (!request || this.isSyncingQueue || !this.isOnline) return;
    if (this.syncQueue.length === 0) return;

    this.isSyncingQueue = true;
    this.syncOfflinePanelState();
    try {
      const result = await withAccountSyncLock(request.ownerId, () =>
        flushSyncQueue(request.ownerId, request.signal, {
          syncTrip: async (payload) => {
            await uploadMobileTripSync(payload, request);
          },
          syncReport: async (payload) => {
            await createConditionReport(payload, request);
          },
          syncPin: async (payload) => {
            const created = await createPin(payload, request);
            if (!request.signal.aborted) this.pins = [created, ...this.pins];
          },
        })
      );
      if (request.signal.aborted) return;
      await this.refreshOfflineLocalState();
      if (request.signal.aborted) return;
      if (result.synced > 0) {
        void this.reloadTrips(request).catch(() => undefined);
        void this.refreshReportsGeoJson();
        void this.refreshHotspotsGeoJson();
        this.showToast(
          `Synced ${result.synced} queued offline item${result.synced === 1 ? "" : "s"}`,
          "success"
        );
      }
      if (result.authRequired) {
        await this.hydrateAuthSession();
        if (announce && !this.user) {
          this.showToast("Sign in to sync your queued items", "info");
          this.openAccountPanel("account");
        }
      } else if (result.failed > 0 && announce) {
        this.showToast(
          `${result.failed} queued item(s) could not sync yet`,
          "error"
        );
      }
    } catch (err) {
      if (announce && !request.signal.aborted) {
        this.showToast(formatFriendlyApiError(err, "Could not sync offline items"), "error");
      }
    } finally {
      if (!request.signal.aborted) {
        this.isSyncingQueue = false;
        this.syncOfflinePanelState();
      }
    }
  }

  private async openPacksPanel(): Promise<void> {
    if (this.inspector.openState()) {
      this.inspector.close();
      this.shellEl.classList.remove("inspector-open");
    }
    this.closeClosureInspector();
    this.closeAccountPanel();
    this.closeAdminPanel();
    this.closeRoutePlanner();
    const panel = await this.ensurePacksPanel();
    panel.updateState({
      online: this.isOnline,
      isAdmin: canManageAdminCompute(this.user),
      regions: this.packRegions,
      downloaded: this.downloadedPacks,
      activeOfflinePackId: this.activeOfflinePackId,
      syncQueue: this.syncQueue,
      syncing: this.isSyncingQueue,
    });
    panel.open();
    this.sheetControllers.packs.reset("half");
    this.shellEl.classList.add("packs-open");
    void this.refreshPacksCatalog();
  }

  private closePacksPanel(): void {
    this.packsPanel?.close();
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
    this.routePlanner?.updateState(this.buildRoutePlannerState());
    this.topbar.updateRouteSummary({
      legCount: this.routeTrackIds.length,
      planModeActive: this.routePlanMode,
      panelOpen: this.routePlanner?.isPanelOpen() ?? false,
    });
    if (this.routePlanMode) {
      const n = this.routeTrackIds.length;
      this.routePickBannerEl.textContent = `⇢ Click tracks on map to append to route (${n} leg${n === 1 ? "" : "s"}) · Esc to stop`;
      this.routePickBannerEl.classList.remove("hidden");
    } else {
      this.routePickBannerEl.classList.add("hidden");
    }
  }

  private async openRoutePlanner(): Promise<void> {
    if (this.inspector.openState()) {
      this.inspector.close();
      this.shellEl.classList.remove("inspector-open");
    }
    this.closeClosureInspector();
    this.closeAccountPanel();
    this.closeAdminPanel();
    this.closePacksPanel();
    const panel = await this.ensureRoutePlanner();
    panel.updateState(this.buildRoutePlannerState());
    panel.open();
    this.sheetControllers.route.reset("half");
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
    this.routePlanner?.close();
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

  private clearPlannedRoute(): void {
    this.routePlanAbort?.abort();
    this.routePlanAbort = null;
    this.routeTrackIds = [];
    this.routeWaypoints = [];
    this.routeAnalysis = null;
    this.routeName = "";
    this.routeNotes = "";
    this.activeSavedRouteId = null;
    this.routeError = null;
    this.routeLoading = false;
    this.routeExporting = null;
    this.routeSaving = false;
    this.setRoutePlanMode(false);
    this.map.setPlannedRoute(null, null);
    this.syncUrlState({ routeTrackIds: [] });
    this.syncRigUiState();
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
    this.syncUrlState({ routeTrackIds: [...this.routeTrackIds] }, "replace");
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
        { signal: controller.signal, ownerId: this.user?.id, anonymous: !this.user }
      );
      if (controller.signal.aborted) return;

      this.routeAnalysis = analysis;
      this.routeLoading = false;
      const { lineFc, pinsFc } = buildPlannedRouteMapGeoJson(analysis);
      this.map.setPlannedRoute(lineFc, pinsFc);

      if (fitMap) {
        const bbox = extractAnalysisBBox(analysis);
        if (bbox) {
          if (this.user) this.setPrivateMapFocus();
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
    const request = this.accountRequest();
    if (!request) {
      this.savedRoutes = [];
      this.syncRoutePlannerUiState();
      return;
    }
    this.savedRoutesLoading = true;
    this.syncRoutePlannerUiState();
    try {
      const routes = await listSavedRoutes(request);
      if (!request.signal.aborted) this.savedRoutes = routes;
    } catch (err) {
      // Preserve last loaded saved routes if offline
      if (!request.signal.aborted) {
        console.warn("Unable to refresh saved routes; preserving cached list:", err);
      }
    } finally {
      if (!request.signal.aborted) {
        this.savedRoutesLoading = false;
        this.syncRoutePlannerUiState();
      }
    }
  }

  private async handleExportPlannedRoute(
    format: "gpx" | "geojson"
  ): Promise<void> {
    if (this.routeTrackIds.length === 0) return;
    const signal = this.accountAbort.signal;
    this.routeExporting = format;
    this.syncRoutePlannerUiState();
    try {
      await exportPlannedRoute(
        this.buildRoutePlanRequestPayload(),
        format,
        this.routeAnalysis,
        { signal, ownerId: this.user?.id, anonymous: !this.user }
      );
      if (signal.aborted) return;
      this.showToast(
        `Exported planned route (${format.toUpperCase()})`,
        "success"
      );
    } catch (err) {
      if (signal.aborted) return;
      const msg =
        err instanceof Error ? err.message : "Route export failed";
      this.showToast(msg, "error");
    } finally {
      if (!signal.aborted) {
        this.routeExporting = null;
        this.syncRoutePlannerUiState();
      }
    }
  }

  private async handleSavePlannedRoute(asNew: boolean): Promise<void> {
    const request = this.accountRequest();
    if (!request) {
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
      const input = {
        name: finalName,
        notes: this.routeNotes.trim() || null,
        track_ids: [...this.routeTrackIds],
        waypoints: [...this.routeWaypoints],
        vehicle_class: rig.vehicleClass,
      };
      const routeId = asNew ? null : this.activeSavedRouteId;
      const saved = routeId
        ? await updateSavedRoute(routeId, input, request)
        : await saveRoute(input, request);
      if (request.signal.aborted) return;
      this.showToast(`${routeId ? "Updated saved route" : "Saved route"} "${saved.name}"`, "success");

      this.activeSavedRouteId = getSavedRouteId(saved);
      this.routeName = saved.name;
      if (saved.analysis) {
        this.routeAnalysis = saved.analysis;
        const { lineFc, pinsFc } = buildPlannedRouteMapGeoJson(saved.analysis);
        this.map.setPlannedRoute(lineFc, pinsFc);
      }
      await this.refreshSavedRoutes();
    } catch (err) {
      if (request.signal.aborted) return;
      const msg =
        err instanceof Error ? err.message : "Failed to save route";
      this.routeError = msg;
      this.showToast(msg, "error");
    } finally {
      if (!request.signal.aborted) {
        this.routeSaving = false;
        this.syncRoutePlannerUiState();
      }
    }
  }

  private async handleLoadSavedRoute(routeId: string): Promise<void> {
    const request = this.accountRequest();
    if (!request) return;
    this.routePlanAbort?.abort();
    this.routeLoading = true;
    this.routeError = null;
    this.syncRoutePlannerUiState();
    try {
      const loaded = await getSavedRoute(routeId, request);
      if (request.signal.aborted) return;
      this.activeSavedRouteId = getSavedRouteId(loaded);
      this.routeName = loaded.name;
      this.routeNotes = loaded.notes ?? "";
      this.routeTrackIds = [...(loaded.track_ids ?? [])];
      this.routeWaypoints = getSavedRouteWaypoints(loaded);
      this.syncUrlState({ routeTrackIds: [...this.routeTrackIds] }, "replace");
      this.setPrivateMapFocus();

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
      if (!request.signal.aborted) this.showToast(`Loaded route "${loaded.name}"`, "success");
    } catch (err) {
      if (request.signal.aborted) return;
      this.routeLoading = false;
      const msg =
        err instanceof Error ? err.message : "Failed to load saved route";
      this.routeError = msg;
      this.showToast(msg, "error");
      this.syncRoutePlannerUiState();
    }
  }

  private async handleDeleteSavedRoute(routeId: string): Promise<void> {
    const request = this.accountRequest();
    if (!request) return;
    try {
      await deleteSavedRoute(routeId, request);
      if (request.signal.aborted) return;
      this.savedRoutes = this.savedRoutes.filter(
        (r) => getSavedRouteId(r) !== routeId
      );
      if (this.activeSavedRouteId === routeId) {
        this.activeSavedRouteId = null;
      }
      this.syncRoutePlannerUiState();
      this.showToast("Saved route deleted", "info");
    } catch (err) {
      if (request.signal.aborted) return;
      const msg =
        err instanceof Error ? err.message : "Failed to delete route";
      this.showToast(msg, "error");
    }
  }

  private async handleExportSavedRoute(
    routeId: string,
    format: "gpx" | "geojson"
  ): Promise<void> {
    const request = this.accountRequest();
    if (!request) return;
    try {
      const url = getSavedRouteExportUrl(routeId, format);
      await downloadAuthenticatedExport(
        url,
        `route_${routeId}.${format === "gpx" ? "gpx" : "geojson"}`,
        request
      );
      if (request.signal.aborted) return;
      this.showToast(`Downloaded route (${format.toUpperCase()})`, "success");
    } catch (err) {
      if (request.signal.aborted) return;
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
    this.topbar.updateAdminAccess(isAdmin, this.adminPanel?.isPanelOpen() ?? false);
    this.accountPanel?.updateState(this.buildAccountPanelState());
    this.packsPanel?.updateState({ isAdmin: canManageAdminCompute(this.user) });
    this.adminPanel?.updateState({
      currentUserId: this.user?.id ?? null,
      registrationMode: this.authConfig.registration_mode,
    });
    if (!isAdmin && this.adminPanel?.isPanelOpen()) {
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
      activeBasemap: this.activeBasemap,
      tileManifest: this.currentTileManifest,
      isOfflinePack: Boolean(this.activeOfflinePackId) || !this.isOnline,
    });
    this.syncRoutePlannerUiState();
    this.syncTelemetrySectionVisibility();
  }

  private syncTelemetrySectionVisibility(): void {
    this.sidebar.setTelemetrySectionVisible(
      shouldShowStackTelemetrySection(
        this.user,
        this.uiConfig.show_stack_telemetry_public
      )
    );
  }

  private applyResolvedLayerPreferences(): void {
    const resolved = resolveEffectiveLayerPreferences(
      this.uiConfig,
      this.userLayerPrefs,
      this.layerState.snowLineElevationM
    );
    const newlyEnabledUnmapped =
      !this.layerState.unmapped && resolved.layerState.unmapped;
    const newlyEnabledHotspots =
      !this.layerState.hotspots && resolved.layerState.hotspots;

    this.layerOrder = resolved.layerOrder;
    this.layerState = resolved.layerState;
    this.hasCustomLayerPrefs = resolved.hasCustomPreferences;

    this.sidebar.syncLayerPreferences(
      this.layerOrder,
      this.layerState,
      this.hasCustomLayerPrefs
    );
    this.map.setLayerVisibility(this.layerState);

    if (newlyEnabledUnmapped) {
      void this.refreshUnmappedCandidates();
    }
    if (newlyEnabledHotspots) {
      void this.refreshHotspotsGeoJson();
    }
  }

  private persistUserLayerPreferenceChange(patch: {
    layerOrder?: LayerKey[];
    layerVisibility?: Partial<Record<LayerKey, boolean>>;
  }): void {
    this.userLayerPrefs = {
      layerOrder:
        patch.layerOrder !== undefined
          ? [...patch.layerOrder]
          : this.userLayerPrefs.layerOrder,
      layerVisibility:
        patch.layerVisibility !== undefined
          ? { ...(this.userLayerPrefs.layerVisibility ?? {}), ...patch.layerVisibility }
          : this.userLayerPrefs.layerVisibility,
    };
    this.hasCustomLayerPrefs = Boolean(
      this.userLayerPrefs.layerOrder !== null ||
        this.userLayerPrefs.layerVisibility !== null
    );
    saveStoredUserLayerPrefs(this.userLayerPrefs);
    this.sidebar.syncLayerPreferences(
      this.layerOrder,
      this.layerState,
      this.hasCustomLayerPrefs
    );

    const request = this.accountRequest();
    if (request && this.isOnline) {
      if (this.userPrefsSaveTimer !== null) {
        window.clearTimeout(this.userPrefsSaveTimer);
      }
      const snapshot = {
        layer_order: this.userLayerPrefs.layerOrder,
        layer_visibility: this.userLayerPrefs.layerVisibility,
      };
      this.userPrefsSaveTimer = window.setTimeout(() => {
        this.userPrefsSaveTimer = null;
        if (request.signal.aborted) return;
        void updateUserPreferences(snapshot, request).catch((err) => {
          if (!request.signal.aborted) console.warn("Failed to persist map layer preferences to account:", err);
        });
      }, 250);
    }
  }

  private resetLocalLayerPreferences(): void {
    if (this.userPrefsSaveTimer !== null) {
      window.clearTimeout(this.userPrefsSaveTimer);
      this.userPrefsSaveTimer = null;
    }
    this.userLayerPrefs = { layerOrder: null, layerVisibility: null };
    clearStoredUserLayerPrefs();
    this.applyResolvedLayerPreferences();
  }

  private async handleResetUserLayerPreferences(): Promise<void> {
    const request = this.accountRequest();
    this.resetLocalLayerPreferences();
    let syncedRemote = true;
    if (request && this.isOnline) {
      try {
        await resetUserPreferences(request);
      } catch (err) {
        if (request.signal.aborted) return;
        syncedRemote = false;
        console.warn("Failed to clear saved server layer preferences:", err);
      }
    }
    if (request?.signal.aborted) return;
    this.showToast(
      syncedRemote
        ? "Map layers reset to instance defaults"
        : "Map layers reset locally (server sync failed)",
      "info"
    );
  }

  private async openAccountPanel(tab?: AccountPanelTab): Promise<void> {
    if (this.inspector.openState()) {
      this.inspector.close();
      this.shellEl.classList.remove("inspector-open");
    }
    this.closeClosureInspector();
    this.closeAdminPanel();
    this.closePacksPanel();
    this.closeRoutePlanner();
    const panel = await this.ensureAccountPanel();
    panel.updateState(this.buildAccountPanelState());
    panel.open(tab);
    this.sheetControllers.account.reset("half");
    this.shellEl.classList.add("account-open");
  }

  private closeAccountPanel(): void {
    this.accountPanel?.close();
    this.shellEl.classList.remove("account-open");
  }

  private async openAdminPanel(tab?: AdminPanelTab): Promise<void> {
    const request = this.accountRequest();
    if (!request || !isAdminUser(this.user)) {
      this.showToast("Admin privileges required", "error");
      return;
    }
    if (this.inspector.openState()) {
      this.inspector.close();
      this.shellEl.classList.remove("inspector-open");
    }
    this.closeClosureInspector();
    this.closeAccountPanel();
    this.closePacksPanel();
    this.closeRoutePlanner();
    const panel = await this.ensureAdminPanel();
    if (request.signal.aborted) return;
    panel.updateState({ currentUserId: request.ownerId });
    panel.open(tab);
    this.sheetControllers.admin.reset("half");
    this.shellEl.classList.add("admin-open");
    this.topbar.updateAdminAccess(true, true);
    void this.refreshAdminConsoleData();
  }

  private closeAdminPanel(): void {
    this.adminPanel?.close();
    this.shellEl.classList.remove("admin-open");
    this.topbar.updateAdminAccess(isAdminUser(this.user), false);
  }

  private async refreshAdminConsoleData(): Promise<void> {
    const request = this.accountRequest();
    if (!request || !isAdminUser(this.user)) return;
    this.adminPanel?.updateState({ loading: true, error: null });
    try {
      const [stats, invites, users, uiSettings] = await Promise.all([
        fetchAdminStats(request),
        fetchAdminInvites(request),
        fetchAdminUsers(request),
        fetchAdminUiSettings(request).catch(() => this.adminUiSettings),
      ]);
      if (request.signal.aborted) return;
      this.adminStats = stats;
      this.adminInvites = invites;
      this.adminUsers = users;
      this.adminUiSettings = uiSettings;
      this.adminPanel?.updateState({
        stats: this.adminStats,
        invites: this.adminInvites,
        users: this.adminUsers,
        uiSettings: this.adminUiSettings,
        latestCreatedInvite: this.latestCreatedInvite,
        registrationMode:
          stats.registration_mode ?? this.authConfig.registration_mode,
      });
    } catch (err) {
      if (request.signal.aborted) return;
      this.adminPanel?.updateState({
        error: formatFriendlyApiError(err, "Failed to load admin console data"),
      });
    } finally {
      if (!request.signal.aborted) this.adminPanel?.updateState({ loading: false });
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
    this.accountPanel?.updateState(this.buildAccountPanelState());
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

    this.mobileScrimEl.addEventListener("click", () => {
      if (!this.sidebarCollapsed) {
        this.setSidebarCollapsed(true, { persist: false });
      }
    });

    this.basemapMobileBtnEl.addEventListener("click", (e) => {
      e.stopPropagation();
      const willOpen = !this.basemapWrapEl.classList.contains("menu-open");
      this.basemapWrapEl.classList.toggle("menu-open", willOpen);
      this.basemapMobileBtnEl.setAttribute("aria-expanded", String(willOpen));
    });

    document.addEventListener("click", (e) => {
      if (!this.basemapWrapEl.classList.contains("menu-open")) return;
      if (!this.basemapWrapEl.contains(e.target as Node)) {
        this.basemapWrapEl.classList.remove("menu-open");
        this.basemapMobileBtnEl.setAttribute("aria-expanded", "false");
      }
    });

    window.addEventListener("resize", () => {
      this.syncSidebarWidthForViewport();
    });
    window.addEventListener("storage", (event) => {
      if (usesSessionBearer()) return;
      if (event.key === PENDING_SIGN_OUT_KEY) {
        this.pendingSignOutOwner = loadPendingSignOut();
        this.signedOutLocally = this.pendingSignOutOwner !== null;
        return;
      }
      if (event.key !== ACCOUNT_CHANGE_KEY) return;
      this.authCheck?.abort();
      this.authCheck = null;
      this.pendingSignOutOwner = loadPendingSignOut();
      if (event.newValue?.startsWith("signed-out-complete:")) {
        this.signedOutLocally = this.pendingSignOutOwner !== null;
        if (this.user !== null) this.clearAccountState(null);
        return;
      }
      this.signedOutLocally =
        this.pendingSignOutOwner !== null ||
        (event.newValue?.startsWith("signed-out:") ?? false);
      this.clearAccountState(null);
      if (!this.signedOutLocally) {
        if (this.authChanging) this.pendingAuthRecheck = true;
        else void this.hydrateAuthSession();
      }
    });
    window.addEventListener("pageshow", (event) => {
      if (event.persisted) void this.hydrateAuthSession();
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
          else if (action === "share") void this.handleShareCurrentView();
        });
      });

    window.addEventListener("popstate", () => {
      this.handlePopState();
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
        } else if (this.routePlanner?.isPanelOpen()) {
          this.closeRoutePlanner();
        } else if (this.adminPanel?.isPanelOpen()) {
          this.closeAdminPanel();
        } else if (this.packsPanel?.isPanelOpen()) {
          this.closePacksPanel();
        } else if (this.accountPanel?.isPanelOpen()) {
          this.closeAccountPanel();
        } else if (this.closureInspector.openState()) {
          this.closeClosureInspector();
        } else if (this.inspector.openState()) {
          this.inspector.close();
          this.shellEl.classList.remove("inspector-open");
          this.map.setSelectedTrackId(null);
          this.syncUrlState({ trackId: null, profileOpen: false }, "push");
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
        void this.resumeOnlineSession();
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
    void this.hydrateUiConfig();
    void this.loadVectorAndTerrainLayers();
    void this.refreshClosures(false);
    void this.refreshHotspotsGeoJson();
    void this.refreshReportsGeoJson();
    if (this.layerState.unmapped) {
      void this.refreshUnmappedCandidates();
    }
    if (this.pendingSignOutOwner && this.isOnline) void this.resumeOnlineSession();
    else void this.hydrateAuthSession(true);

    if (this.initialUrlState.routeTrackIds.length > 0) {
      void this.openRoutePlanner();
      void this.recomputePlannedRoute(this.initialUrlState.lat === null);
    } else if (this.initialUrlState.trackId) {
      void this.openTrackInspector(this.initialUrlState.trackId, undefined, {
        autoZoomIfNoCoords: this.initialUrlState.lat === null,
        autoOpenProfile: this.initialUrlState.profileOpen,
        skipHistoryPush: true,
      });
    }

    const isPageVisible = (): boolean =>
      typeof document === "undefined" || document.visibilityState === "visible";

    window.setInterval(() => {
      if (isPageVisible()) {
        void this.refreshHealth(false);
      }
    }, HEALTH_POLL_INTERVAL_MS);
    window.setInterval(() => {
      if (isPageVisible()) {
        void this.refreshClosures(false);
      }
    }, CLOSURES_POLL_INTERVAL_MS);

    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible" && this.isOnline) {
          void this.refreshHealth(false);
          void this.resumeOnlineSession();
        }
      });
    }

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

  private async hydrateUiConfig(): Promise<void> {
    try {
      this.uiConfig = await fetchPublicUiConfig();
    } catch {
      this.uiConfig = {
        default_layer_order: [...DEFAULT_PUBLIC_UI_CONFIG.default_layer_order],
        default_layer_visibility: {
          ...DEFAULT_PUBLIC_UI_CONFIG.default_layer_visibility,
        },
        show_stack_telemetry_public:
          DEFAULT_PUBLIC_UI_CONFIG.show_stack_telemetry_public,
      };
    } finally {
      this.applyResolvedLayerPreferences();
      this.syncTelemetrySectionVisibility();
    }
  }

  private async signIn(
    authenticate: () => Promise<{ user: AuthUser }>,
    success: (name: string) => string
  ): Promise<void> {
    while (this.authTransitionPromise) {
      await this.authTransitionPromise.catch(() => undefined);
    }
    this.authChanging = true;
    this.authCheck?.abort();
    this.authCheck = null;
    let user: AuthUser | null = null;
    const run = (async () => {
      user = await withBrowserAuthLock(async () => {
        const res = await authenticate();
        this.signedOutLocally = false;
        this.pendingSignOutOwner = null;
        persistPendingSignOut(null);
        this.clearAccountState(res.user);
        announceAccountChange("signed-in");
        return res.user;
      });
    })();
    this.authTransitionPromise = run;
    try {
      await run;
    } finally {
      if (this.authTransitionPromise === run) this.authTransitionPromise = null;
      this.authChanging = false;
      if (this.pendingAuthRecheck) {
        this.pendingAuthRecheck = false;
        void this.hydrateAuthSession();
      }
    }
    if (!user || this.user?.id !== (user as AuthUser).id) return;
    const signedInUser = user as AuthUser;
    const signal = this.accountAbort.signal;
    await this.loadSignedInUserData();
    if (!signal.aborted) this.showToast(success(signedInUser.display_name), "success");
  }

  private async signOut(): Promise<void> {
    const ownerId = this.user?.id;
    if (!ownerId || this.authChanging) return;
    this.authChanging = true;
    this.authCheck?.abort();
    this.authCheck = null;
    this.signedOutLocally = true;
    this.pendingSignOutOwner = usesSessionBearer() ? null : ownerId;
    persistPendingSignOut(this.pendingSignOutOwner);
    announceAccountChange("signed-out");
    this.clearAccountState(null);
    const signal = this.accountAbort.signal;
    const run = (async () => {
      try {
        await this.finishSignOut(ownerId);
        if (!signal.aborted) this.showToast("Signed out", "info");
      } catch {
        if (!signal.aborted) {
          this.showToast("Signed out in this browser", "info");
        }
      }
    })();
    this.authTransitionPromise = run;
    try {
      await run;
    } finally {
      if (this.authTransitionPromise === run) this.authTransitionPromise = null;
      this.authChanging = false;
      if (this.pendingAuthRecheck) {
        this.pendingAuthRecheck = false;
        void this.hydrateAuthSession();
      }
    }
  }

  private async finishSignOut(ownerId: string): Promise<void> {
    await withBrowserAuthLock(async () => {
      try {
        await logoutAccount({ ownerId });
      } catch (err) {
        if (!(err instanceof ApiError) || ![401, 403, 409].includes(err.status)) throw err;
      }
      if (loadPendingSignOut() === ownerId) persistPendingSignOut(null);
      this.pendingSignOutOwner = loadPendingSignOut();
      this.signedOutLocally = this.pendingSignOutOwner !== null;
      announceAccountChange("signed-out-complete");
    });
  }

  private async resumeOnlineSession(): Promise<void> {
    const ownerId = this.pendingSignOutOwner ?? loadPendingSignOut();
    if (ownerId && !this.authChanging) {
      this.authChanging = true;
      const run = (async () => {
        try {
          await this.finishSignOut(ownerId);
        } catch (err) {
          // Retry while the browser is locally signed out.
          console.warn("Deferred online sign-out retry failed:", err);
        }
      })();
      this.authTransitionPromise = run;
      try {
        await run;
      } finally {
        if (this.authTransitionPromise === run) this.authTransitionPromise = null;
        this.authChanging = false;
      }
      if (this.signedOutLocally) return;
    }
    await this.hydrateAuthSession();
    void this.handleFlushSyncQueue(false);
  }

  private async hydrateAuthSession(startup = false): Promise<void> {
    if (!this.isOnline || this.signedOutLocally || this.authChanging || this.authCheck) return;
    const controller = new AbortController();
    this.authCheck = controller;
    try {
      const me = await fetchAuthMe(controller.signal);
      if (controller.signal.aborted) return;
      const user = me.authenticated ? me.user : null;
      if (user?.id === this.user?.id) return;
      const preserveStartupView = startup && !this.privateMapFocus &&
        this.routeName === "" && this.routeNotes === "" &&
        this.routeTrackIds.join(",") === this.initialUrlState.routeTrackIds.join(",");
      this.clearAccountState(user, preserveStartupView);
      if (user) {
        await this.loadSignedInUserData();
        if (!controller.signal.aborted && preserveStartupView) this.refreshPlannedRouteForRig();
      }
    } catch (err) {
      if (!controller.signal.aborted && err instanceof ApiError && [401, 403].includes(err.status)) {
        if (this.user) this.clearAccountState(null);
        this.showToast(formatFriendlyApiError(err), "error");
      }
    } finally {
      if (this.authCheck === controller) this.authCheck = null;
    }
  }

  private async loadSignedInUserData(): Promise<void> {
    const request = this.accountRequest();
    if (!request) return;
    try {
      const [vehicles, trips, favorites, pins, savedRoutes, serverPrefs] =
        await Promise.all([
          fetchVehicles(request).catch(() => []),
          fetchTrips(request).catch(() => []),
          fetchFavorites(request).catch(() => []),
          fetchPins(request).catch(() => []),
          listSavedRoutes(request).catch(() => []),
          fetchUserPreferences(request).catch(() => null),
          this.refreshOfflineLocalState(),
        ]);
      if (request.signal.aborted) return;
      this.vehicles = vehicles;
      this.trips = trips;
      this.favorites = favorites;
      this.pins = pins;
      this.savedRoutes = savedRoutes;
      if (serverPrefs !== null) {
        this.userLayerPrefs = {
          layerOrder: serverPrefs.layer_order,
          layerVisibility: serverPrefs.layer_visibility,
        };
        saveStoredUserLayerPrefs(this.userLayerPrefs);
        this.applyResolvedLayerPreferences();
      }
      this.renderCombinedPinsOnMap();
      void this.refreshReportsGeoJson();
    } finally {
      if (!request.signal.aborted) {
        this.syncRigUiState();
        if (this.isOnline && this.syncQueue.length > 0) void this.handleFlushSyncQueue(false);
      }
    }
  }

  private async reloadVehicles(request: AccountRequest): Promise<void> {
    const vehicles = await fetchVehicles(request);
    if (request.signal.aborted) return;
    this.vehicles = vehicles;
    this.syncRigUiState();
  }

  private async handleTripUploaded(
    uploaded: TripItem,
    request: AccountRequest,
    toastMessage: string
  ): Promise<void> {
    if (request.signal.aborted) return;
    this.latestUploadedTrip = uploaded;
    await this.reloadTrips(request);
    if (request.signal.aborted) return;
    if (this.layerState.unmapped) void this.refreshUnmappedCandidates();
    this.showToast(toastMessage, "success");
  }

  private async reloadTrips(request: AccountRequest): Promise<void> {
    const trips = await fetchTrips(request);
    if (request.signal.aborted) return;
    this.trips = trips;
    this.syncRigUiState();
  }

  private async refreshHotspotsGeoJson(): Promise<void> {
    try {
      const fc = await fetchHotspotsGeoJson();
      this.map.setHotspotsGeoJson(fc);
    } catch (err) {
      // Obstacle hotspots layer stays empty gracefully offline
      console.debug("Unable to refresh obstacle hotspots layer:", err);
    }
  }

  private async refreshReportsGeoJson(): Promise<void> {
    const signal = this.accountAbort.signal;
    try {
      const fc = await fetchReportsGeoJson({ signal, ownerId: this.user?.id, anonymous: !this.user });
      if (!signal.aborted) this.map.setReportsGeoJson(fc);
    } catch (err) {
      // Reports layer stays empty gracefully offline
      if (!signal.aborted) {
        console.debug("Unable to refresh condition reports layer:", err);
      }
    }
  }

  private async refreshUnmappedCandidates(): Promise<void> {
    try {
      const fc = await fetchUnmappedCandidatesGeoJson();
      this.map.setUnmappedGeoJson(fc);
    } catch (err) {
      // Unmapped spurs layer stays empty gracefully offline
      console.debug("Unable to refresh unmapped candidates layer:", err);
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
      this.currentTileManifest = manifest;
      this.inspector.updateContext({
        tileManifest: manifest,
        isOfflinePack: Boolean(this.activeOfflinePackId) || !this.isOnline,
      });
      if (manifest.snowline_bands_m && manifest.snowline_bands_m.length > 0) {
        this.sidebar.setSnowLineBands(
          manifest.snowline_bands_m,
          this.layerState.snowLineElevationM
        );
      }

      const isOfflinePack =
        Boolean(this.activeOfflinePackId) || !this.isOnline;
      if (manifest.vector_available || manifest.terrain_available) {
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

      if (manifest.vector_basemap_url) {
        this.map.applyTileManifest(manifest, { isOfflinePack });
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
      void this.openTrackInspector(trackId, undefined, {
        skipHistoryPush: true,
      });
    }
  }

  private closeClosureInspector(): void {
    this.closureInspectorAbort?.abort();
    this.closureInspector.close();
    this.shellEl.classList.remove("closure-open");
    this.map.setSelectedClosureId(null);
  }

  private async openClosureInspector(
    primary: ClosureDetailResponse,
    overlapping: ClosureDetailResponse[]
  ): Promise<void> {
    this.closureInspectorAbort?.abort();
    const controller = new AbortController();
    this.closureInspectorAbort = controller;

    if (this.inspector.openState()) {
      this.inspector.close();
      this.shellEl.classList.remove("inspector-open");
      this.map.setSelectedTrackId(null);
      this.syncUrlState({ trackId: null, profileOpen: false }, "replace");
    }
    this.closeAccountPanel();
    this.closeAdminPanel();
    this.closePacksPanel();
    this.closeRoutePlanner();

    this.activeOverlappingClosures =
      overlapping.length > 0 ? overlapping : [primary];
    this.sheetControllers.closure.reset("half");
    this.shellEl.classList.add("closure-open");
    this.map.setSelectedClosureId(primary.closure_id);

    const numericId = Number(primary.closure_id);
    const canFetchFull =
      this.isOnline && Number.isInteger(numericId) && numericId > 0;

    this.closureInspector.showClosure(primary, {
      overlapping: this.activeOverlappingClosures,
      loading: canFetchFull,
      error: null,
    });

    if (!canFetchFull) return;

    try {
      const full = await fetchClosureDetail(numericId, controller.signal);
      if (controller.signal.aborted) return;
      const merged: ClosureDetailResponse = {
        ...primary,
        ...full,
        bbox: full.bbox ?? primary.bbox ?? null,
      };
      this.closureInspector.updateDetail(merged, {
        loading: false,
        error: null,
      });
    } catch (err) {
      if (controller.signal.aborted) return;
      const msg =
        err instanceof Error ? err.message : "Offline or unavailable";
      this.closureInspector.setLoadingError(msg);
    }
  }

  private async openTrackInspector(
    trackId: string,
    trackLabel?: string,
    options?: {
      autoZoomIfNoCoords?: boolean;
      autoOpenProfile?: boolean;
      skipHistoryPush?: boolean;
    }
  ): Promise<void> {
    this.inspectorAbort?.abort();
    const controller = new AbortController();
    this.inspectorAbort = controller;

    this.closeClosureInspector();
    this.closeAccountPanel();
    this.closeAdminPanel();
    this.closePacksPanel();
    this.closeRoutePlanner();
    this.sheetControllers.inspector.reset("half");
    this.shellEl.classList.add("inspector-open");
    this.map.setSelectedTrackId(trackId);
    this.inspector.showLoading(trackLabel);

    this.syncUrlState(
      {
        trackId,
        profileOpen: Boolean(options?.autoOpenProfile),
      },
      options?.skipHistoryPush ? "replace" : "push"
    );

    const rig = this.getActiveRigInfo();
    try {
      const queryVehicleClass =
        this.user && this.vehicles.length > 0 ? null : rig.vehicleClass;
      const detail = await fetchTrackDetail(trackId, {
        vehicleClass: queryVehicleClass,
        signal: controller.signal,
        ownerId: this.user?.id,
        anonymous: !this.user,
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
        activeBasemap: this.activeBasemap,
        tileManifest: this.currentTileManifest,
        isOfflinePack: Boolean(this.activeOfflinePackId) || !this.isOnline,
      });

      if (options?.autoZoomIfNoCoords && detail.bbox) {
        this.map.fitBBox(detail.bbox, this.getMapLeftPadding(24), 420);
      }
      if (options?.autoOpenProfile) {
        this.inspector.openElevationModal();
      }
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
    const request = this.accountRequest();
    if (!request) {
      this.showToast("Sign in to save favorite tracks across sessions", "info");
      this.openAccountPanel("account");
      return;
    }

    try {
      if (currentlyFavorite) {
        await removeFavorite(trackId, request);
        if (request.signal.aborted) return;
        this.favorites = this.favorites.filter((f) => f.track_id !== trackId);
        this.showToast("Removed track from favorites", "info");
      } else {
        const added = await addFavorite(trackId, null, request);
        if (request.signal.aborted) return;
        this.favorites = [added, ...this.favorites];
        this.showToast("Saved track to favorites", "success");
      }
      this.syncRigUiState();
    } catch (err) {
      if (request.signal.aborted) return;
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

  private handlePlaceClick(payload: MapPlaceClickPayload): void {
    const currentView = this.map.getView();
    const targetZoom = Math.max(currentView.zoom, payload.zoom_target);
    if (payload.bbox) {
      this.map.fitBBox(payload.bbox, this.getMapLeftPadding(24), 48);
    } else {
      this.map.flyToLocation(payload.lat, payload.lon, targetZoom);
    }
    this.updateCursorReadout(payload.lat, payload.lon);
    this.syncUrlState(
      {
        lat: payload.lat,
        lon: payload.lon,
        zoom: payload.bbox ? payload.zoom_target : targetZoom,
      },
      "push"
    );
    const label = payload.state
      ? `${payload.name}, ${payload.state}`
      : payload.name;
    this.showToast(`Focused on ${label}`, "info");
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
    this.inspector.updateContext({ activeBasemap: basemap });
    this.basemapButtons.forEach((b) =>
      b.classList.toggle("active", b.dataset.basemap === basemap)
    );
    const meta = BASEMAP_OPTIONS.find((b) => b.id === basemap);
    if (meta) {
      this.basemapMobileLabelEl.textContent = meta.label;
    }
    this.basemapWrapEl.classList.remove("menu-open");
    this.basemapMobileBtnEl.setAttribute("aria-expanded", "false");
    if (basemap === "offline-topo") {
      this.offlineBannerEl.textContent =
        "Offline Topo active: zero external raster tile requests";
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
    this.syncUrlState(
      {
        regionId: preset.id,
        lat: preset.center[1],
        lon: preset.center[0],
        zoom: preset.zoom,
      },
      "push"
    );
    this.showToast(`Centered on ${preset.shortLabel}`, "info");
  }

  private syncUrlState(
    patch: Partial<{
      trackId: string | null;
      regionId: string | null;
      profileOpen: boolean;
      routeTrackIds: string[];
      rigClass: VehicleCapabilityClass | null;
      lat: number | null;
      lon: number | null;
      zoom: number | null;
    }>,
    mode: "push" | "replace" = "replace",
    shareRoute = false
  ): void {
    if (this.suppressUrlSync || typeof window === "undefined") return;

    if ("trackId" in patch) {
      this.urlTrackId = patch.trackId ?? null;
      if (!this.urlTrackId) {
        this.urlProfileOpen = false;
      }
    }
    if ("regionId" in patch) {
      this.urlRegionId = patch.regionId ?? null;
    }
    if ("profileOpen" in patch) {
      this.urlProfileOpen = Boolean(this.urlTrackId && patch.profileOpen);
    }
    if ("routeTrackIds" in patch) {
      this.urlRouteTrackIds = (!this.user || shareRoute) && Array.isArray(patch.routeTrackIds)
        ? [...patch.routeTrackIds]
        : [];
    }
    if ("rigClass" in patch) {
      this.urlRigClass = patch.rigClass ?? null;
    }
    if ("lat" in patch && "lon" in patch) {
      if (
        typeof patch.lat === "number" &&
        Number.isFinite(patch.lat) &&
        typeof patch.lon === "number" &&
        Number.isFinite(patch.lon)
      ) {
        this.urlView = {
          lat: patch.lat,
          lon: patch.lon,
          zoom:
            typeof patch.zoom === "number" && Number.isFinite(patch.zoom)
              ? patch.zoom
              : (this.urlView?.zoom ?? this.activePreset.zoom),
        };
      } else {
        this.urlView = null;
      }
    } else if (!this.privateMapFocus && !this.urlView && (this.urlTrackId || this.urlRegionId) && this.map) {
      this.urlView = this.map.getView();
    }

    const nextSearch = buildAppUrlSearch(
      {
        trackId: this.urlTrackId,
        regionId: this.urlRegionId,
        profileOpen: this.urlProfileOpen,
        routeTrackIds: this.urlRouteTrackIds,
        rigClass: this.urlRigClass,
        lat: this.urlView?.lat ?? null,
        lon: this.urlView?.lon ?? null,
        zoom: this.urlView?.zoom ?? null,
      },
      window.location.search
    );

    if (nextSearch === window.location.search) return;

    const nextUrl = `${window.location.pathname}${nextSearch}${window.location.hash}`;
    try {
      if (mode === "push") {
        window.history.pushState(null, "", nextUrl);
      } else {
        window.history.replaceState(null, "", nextUrl);
      }
    } catch (err) {
      // Ignore history restrictions in sandboxed environments
      console.debug("TrailForge URL history sync skipped:", err);
    }
  }

  private handlePopState(): void {
    if (typeof window === "undefined") return;
    const state = parseAppUrlState(window.location.search);
    this.suppressUrlSync = true;
    try {
      this.urlTrackId = state.trackId;
      this.urlRegionId = state.regionId;
      this.urlProfileOpen = state.profileOpen;
      this.urlRouteTrackIds = [...state.routeTrackIds];
      this.urlRigClass = state.rigClass;
      if (state.rigClass && state.rigClass !== this.anonymousRigClass) {
        this.anonymousRigClass = state.rigClass;
        this.syncRigUiState();
      }
      this.urlView =
        state.lat !== null && state.lon !== null
          ? {
              lat: state.lat,
              lon: state.lon,
              zoom: state.zoom ?? this.activePreset.zoom,
            }
          : null;

      if (state.regionId) {
        const preset = getPresetById(state.regionId);
        if (preset) {
          this.activePreset = preset;
          this.topbar.setSelectedPreset(preset.id);
          this.sidebar.updatePreset(preset);
        }
      }

      if (state.lat !== null && state.lon !== null) {
        this.map.setView(
          state.lat,
          state.lon,
          state.zoom ?? this.activePreset.zoom
        );
      } else if (state.regionId) {
        const preset = getPresetById(state.regionId);
        if (preset) {
          this.map.setView(preset.center[1], preset.center[0], preset.zoom);
        }
      }

      if (state.routeTrackIds.length > 0) {
        this.routeTrackIds = [...state.routeTrackIds];
        this.openRoutePlanner();
        void this.recomputePlannedRoute(state.lat === null);
      } else if (state.trackId) {
        if (
          this.inspector.getCurrentTrackId() === state.trackId &&
          this.inspector.openState()
        ) {
          if (state.profileOpen && !this.inspector.isElevationModalOpen()) {
            this.inspector.openElevationModal();
          } else if (
            !state.profileOpen &&
            this.inspector.isElevationModalOpen()
          ) {
            this.inspector.closeElevationModal();
          }
        } else {
          void this.openTrackInspector(state.trackId, undefined, {
            autoZoomIfNoCoords: state.lat === null,
            autoOpenProfile: state.profileOpen,
            skipHistoryPush: true,
          });
        }
      } else if (this.inspector.openState()) {
        this.inspector.close();
        this.shellEl.classList.remove("inspector-open");
        this.map.setSelectedTrackId(null);
        this.pickedReportCoords = null;
      }
    } finally {
      window.setTimeout(() => {
        this.suppressUrlSync = false;
      }, 120);
    }
  }

  private async handleShareTrack(trackId: string): Promise<void> {
    const view = this.map.getView();
    this.syncUrlState(
      {
        trackId,
        profileOpen: this.inspector.isElevationModalOpen(),
        lat: view.lat,
        lon: view.lon,
        zoom: view.zoom,
      },
      "replace"
    );
    await this.copyTextToClipboard(
      window.location.href,
      "Shareable track link copied to clipboard",
      "Copy the URL directly from your browser address bar"
    );
  }

  private async handleShareCurrentView(): Promise<void> {
    const view = this.map.getView();
    const activeTrackId = this.inspector.openState()
      ? this.inspector.getCurrentTrackId()
      : null;
    this.syncUrlState(
      {
        trackId: activeTrackId,
        profileOpen: activeTrackId ? this.inspector.isElevationModalOpen() : false,
        routeTrackIds: [...this.routeTrackIds],
        rigClass:
          this.urlRigClass ??
          (this.routeTrackIds.length > 0 ? this.getActiveRigInfo().vehicleClass : null),
        lat: view.lat,
        lon: view.lon,
        zoom: view.zoom,
      },
      "replace",
      true
    );
    await this.copyTextToClipboard(
      window.location.href,
      "Shareable map view link copied to clipboard",
      "Copy the URL directly from your browser address bar"
    );
  }

  private toggleUiTheme(): void {
    const next = getNextUiTheme(this.activeTheme);
    this.activeTheme = saveStoredUiTheme(next);
    applyUiThemeToDocument(this.activeTheme);
    this.topbar.updateTheme(this.activeTheme);
    this.showToast(`Switched to ${getThemeLabel(this.activeTheme)} theme`, "info");
  }

  private setSidebarCollapsed(
    collapsed: boolean,
    options?: { persist?: boolean }
  ): void {
    this.sidebarCollapsed = collapsed;
    this.shellEl.classList.toggle("sidebar-collapsed", collapsed);
    this.mobileScrimEl.classList.toggle("hidden", collapsed);
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
