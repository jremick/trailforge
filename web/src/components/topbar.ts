import { summarizeFeedStatus } from "../closuresHelpers";
import { REGION_PRESETS, escapeHtml, getGradeColor } from "../presets";
import {
  MIN_REMOTE_SEARCH_LENGTH,
  isExactCoordinateQuery,
  peekCachedUnifiedSearch,
  preloadAuPlaces,
  resolveLocalSearch,
  runUnifiedSearch,
  shouldRunRemoteSearch,
} from "../search";
import type {
  ClosuresStatusResponse,
  HealthState,
  RegionPreset,
  UnifiedSearchItem,
  VehicleCapabilityClass,
} from "../types";

export interface TopBarCallbacks {
  onToggleSidebar?: () => void;
  onSearchSelect: (item: UnifiedSearchItem) => void;
  onSearchNoMatch: (query: string, remoteError: boolean) => void;
  onSearchClear: () => void;
  onPresetChange: (presetId: string) => void;
  onRoutePlannerClick: () => void;
  onHealthClick: () => void;
  onFeedStatusClick: () => void;
  onRigAccountClick: () => void;
  onOfflinePacksClick: () => void;
  onAdminClick?: () => void;
}

export const SEARCH_DEBOUNCE_MS = 250;
export const TOPBAR_SEARCH_LIMIT = 10;
export const MIN_CHARS_SEARCH_HINT =
  "Type 2+ characters to search tracks, POIs & parks";

export type SearchMenuMode =
  | "hidden"
  | "results"
  | "searching"
  | "hint"
  | "empty";

export type SearchMenuFooterMode = "none" | "searching" | "min_chars_hint";

export interface TopBarSearchTransition {
  isSearching: boolean;
  shouldScheduleRemote: boolean;
  shouldCancelPending: boolean;
  results: UnifiedSearchItem[];
  resultsQuery: string;
  activeIdx: number;
  menuMode: SearchMenuMode;
  footerMode: SearchMenuFooterMode;
  remoteError: boolean;
}

export type SearchSubmitAction =
  | { kind: "noop" }
  | { kind: "select"; item: UnifiedSearchItem }
  | { kind: "hint" }
  | { kind: "execute_remote" };

/**
 * Pure state transition for TopBar search input changes.
 *
 * Ensures:
 * - Empty input clears everything cleanly.
 * - Exact coordinate/MGRS inputs resolve 100% locally and mark resultsQuery final.
 * - 1-character queries resolve local matches only and never trigger remote search.
 * - Client LRU cache hits render 0 ms instant without flashing a loading state.
 * - Queries with instant local matches STILL show searching state (`isSearching: true`,
 *   `footerMode: "searching"`) and keep `resultsQuery = ""` until remote completes so
 *   pressing Enter waits for track results instead of prematurely selecting a preset.
 */
export function computeSearchInputTransition(params: {
  rawValue: string;
  localItems: UnifiedSearchItem[];
  cachedItems: UnifiedSearchItem[] | null;
}): TopBarSearchTransition {
  const q = params.rawValue.trim();

  if (!q) {
    return computeSearchClearTransition();
  }

  if (isExactCoordinateQuery(q)) {
    return {
      isSearching: false,
      shouldScheduleRemote: false,
      shouldCancelPending: true,
      results: params.localItems,
      resultsQuery: q,
      activeIdx: params.localItems.length > 0 ? 0 : -1,
      menuMode: params.localItems.length > 0 ? "results" : "empty",
      footerMode: "none",
      remoteError: false,
    };
  }

  if (q.length < MIN_REMOTE_SEARCH_LENGTH) {
    return {
      isSearching: false,
      shouldScheduleRemote: false,
      shouldCancelPending: true,
      results: params.localItems,
      resultsQuery: "",
      activeIdx: params.localItems.length > 0 ? 0 : -1,
      menuMode: params.localItems.length > 0 ? "results" : "hint",
      footerMode: params.localItems.length > 0 ? "min_chars_hint" : "none",
      remoteError: false,
    };
  }

  if (params.cachedItems !== null) {
    return {
      isSearching: false,
      shouldScheduleRemote: false,
      shouldCancelPending: true,
      results: params.cachedItems,
      resultsQuery: q,
      activeIdx: params.cachedItems.length > 0 ? 0 : -1,
      menuMode: params.cachedItems.length > 0 ? "results" : "empty",
      footerMode: "none",
      remoteError: false,
    };
  }

  if (params.localItems.length > 0) {
    return {
      isSearching: true,
      shouldScheduleRemote: true,
      shouldCancelPending: false,
      results: params.localItems,
      resultsQuery: "",
      activeIdx: 0,
      menuMode: "results",
      footerMode: "searching",
      remoteError: false,
    };
  }

  return {
    isSearching: true,
    shouldScheduleRemote: true,
    shouldCancelPending: false,
    results: [],
    resultsQuery: "",
    activeIdx: -1,
    menuMode: "searching",
    footerMode: "none",
    remoteError: false,
  };
}

/**
 * Pure state transition once remote `/api/search` finishes for a query.
 */
export function computeSearchResolvedTransition(params: {
  query: string;
  items: UnifiedSearchItem[];
  remoteError: boolean;
}): TopBarSearchTransition {
  const q = params.query.trim();
  return {
    isSearching: false,
    shouldScheduleRemote: false,
    shouldCancelPending: false,
    results: params.items,
    resultsQuery: q,
    activeIdx: params.items.length > 0 ? 0 : -1,
    menuMode: params.items.length > 0 ? "results" : "empty",
    footerMode: "none",
    remoteError: params.remoteError,
  };
}

/**
 * Pure state transition when search is cleared or dismissed cleanly.
 */
export function computeSearchClearTransition(): TopBarSearchTransition {
  return {
    isSearching: false,
    shouldScheduleRemote: false,
    shouldCancelPending: true,
    results: [],
    resultsQuery: "",
    activeIdx: -1,
    menuMode: "hidden",
    footerMode: "none",
    remoteError: false,
  };
}

/**
 * Pure decision helper for Enter / form submit in TopBar search.
 */
export function computeSearchSubmitAction(params: {
  query: string;
  resultsQuery: string;
  results: readonly UnifiedSearchItem[];
  activeIdx: number;
}): SearchSubmitAction {
  const q = params.query.trim();
  if (!q) return { kind: "noop" };

  if (
    params.resultsQuery === q &&
    params.activeIdx >= 0 &&
    params.results[params.activeIdx]
  ) {
    return { kind: "select", item: params.results[params.activeIdx]! };
  }

  if (!shouldRunRemoteSearch(q)) {
    if (params.results.length > 0) {
      const idx =
        params.activeIdx >= 0 && params.activeIdx < params.results.length
          ? params.activeIdx
          : 0;
      const picked = params.results[idx];
      if (picked) return { kind: "select", item: picked };
    }
    if (q.length < MIN_REMOTE_SEARCH_LENGTH) {
      return { kind: "hint" };
    }
  }

  return { kind: "execute_remote" };
}

export class TopBar {
  private readonly root: HTMLElement;
  private readonly callbacks: TopBarCallbacks;
  private readonly sidebarBtnEl: HTMLButtonElement;
  private readonly searchWrapEl: HTMLElement;
  private readonly searchFormEl: HTMLFormElement;
  private readonly searchInputEl: HTMLInputElement;
  private readonly searchKbdEl: HTMLElement;
  private readonly searchClearBtn: HTMLButtonElement;
  private readonly searchMenuEl: HTMLElement;
  private readonly presetWrapEl: HTMLElement;
  private readonly presetSelectEl: HTMLSelectElement;
  private readonly routeBtnEl: HTMLButtonElement;
  private readonly routeBadgeEl: HTMLElement;
  private readonly statusWrapEl: HTMLElement;
  private readonly statusBtnEl: HTMLButtonElement;
  private readonly statusDotEl: HTMLElement;
  private readonly statusBadgeEl: HTMLElement;
  private readonly statusMenuEl: HTMLElement;
  private readonly feedPillEl: HTMLButtonElement;
  private readonly feedDotEl: HTMLElement;
  private readonly feedTextEl: HTMLElement;
  private readonly feedDetailEl: HTMLElement;
  private readonly rigBtnEl: HTMLButtonElement;
  private readonly rigDotEl: HTMLElement;
  private readonly rigLabelEl: HTMLElement;
  private readonly adminBtnEl: HTMLButtonElement;
  private readonly packsBtnEl: HTMLButtonElement;
  private readonly packsDotEl: HTMLElement;
  private readonly packsLabelEl: HTMLElement;
  private readonly healthPillEl: HTMLButtonElement;
  private readonly healthDotEl: HTMLElement;
  private readonly healthTextEl: HTMLElement;
  private readonly healthDetailEl: HTMLElement;

  private searchTimer: number | null = null;
  private searchAbort: AbortController | null = null;
  private inFlightQuery: string | null = null;
  private inFlightPromise: Promise<UnifiedSearchItem[]> | null = null;
  private placesPreloadStarted = false;
  private resultsQuery = "";
  private results: UnifiedSearchItem[] = [];
  private activeIdx = -1;
  private isSearching = false;
  private lastRemoteError = false;
  private healthSeverity: "online" | "degraded" | "offline" | "connecting" =
    "connecting";
  private feedSeverity: "online" | "degraded" | "offline" | "connecting" =
    "connecting";
  private healthSummaryTitle = "Checking /api/health…";
  private feedSummaryTitle = "Checking AU agency closure feeds…";

  constructor(
    container: HTMLElement,
    initialPresetId: string,
    callbacks: TopBarCallbacks
  ) {
    this.root = container;
    this.callbacks = callbacks;
    this.root.className = "tf-topbar";

    const iconicRegions = REGION_PRESETS.filter(
      (p) => p.group === "region" && p.packCategory !== "state"
    );
    const stateRegions = REGION_PRESETS.filter(
      (p) => p.group === "region" && p.packCategory === "state"
    );
    const landmarks = REGION_PRESETS.filter((p) => p.group === "landmark");

    const renderOptions = (items: RegionPreset[]): string =>
      items
        .map(
          (p) =>
            `<option value="${escapeHtml(p.id)}" ${p.id === initialPresetId ? "selected" : ""}>${escapeHtml(p.label)}</option>`
        )
        .join("");

    this.root.innerHTML = `
      <div class="tf-topbar-left">
        <button
          type="button"
          class="tf-nav-icon-btn"
          data-role="topbar-sidebar-btn"
          title="Toggle layers sidebar (shortcut: [)"
          aria-label="Toggle layers sidebar"
          aria-expanded="true"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <rect x="3" y="3" width="18" height="18" rx="3"></rect>
            <line x1="9" y1="3" x2="9" y2="21"></line>
          </svg>
        </button>

        <div class="tf-brand">
          <div class="tf-brand-mark" aria-hidden="true">
            <svg width="24" height="24" viewBox="0 0 64 64" fill="none">
              <rect width="64" height="64" rx="14" fill="#131D26" stroke="#2C3E4F" stroke-width="2.5"/>
              <path d="M10 44 L23 22 L33 34 L42 19 L54 44 Z" fill="#182735" stroke="#38BDF8" stroke-width="2.5" stroke-linejoin="round"/>
              <path d="M12 49 C20 45, 25 41, 31 38 C37 35, 41 29, 52 24" stroke="#F59E0B" stroke-width="4" stroke-linecap="round"/>
              <circle cx="42" cy="19" r="3.8" fill="#22C55E" stroke="#0B1117" stroke-width="1.6"/>
            </svg>
          </div>
          <span class="tf-brand-name">TrailForge</span>
        </div>
      </div>

      <div class="tf-search-wrap" data-role="search-wrap">
        <form class="tf-search-form" data-role="search-form" role="search">
          <span class="tf-search-leading" aria-hidden="true">
            <svg class="tf-search-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="11" cy="11" r="8"></circle>
              <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
            </svg>
            <span class="tf-search-spinner" data-role="search-spinner"></span>
          </span>
          <input
            type="text"
            class="tf-search-input"
            data-role="search-input"
            placeholder="Search tracks, towns, huts, camps, coords, or MGRS…"
            aria-label="Search Australian tracks, towns, regions, camps, huts, coordinates, or MGRS"
            aria-autocomplete="list"
            aria-expanded="false"
            aria-busy="false"
            autocomplete="off"
            spellcheck="false"
          />
          <kbd class="tf-search-kbd mono" data-role="search-kbd" aria-hidden="true">/</kbd>
          <button
            type="button"
            class="tf-search-clear hidden"
            data-role="search-clear"
            title="Clear search"
            aria-label="Clear search"
          >×</button>
        </form>
        <div class="tf-search-menu hidden" data-role="search-menu" role="listbox"></div>
      </div>

      <div class="tf-topbar-controls">
        <label class="tf-preset-wrap" title="Jump to Australian region or corridor" aria-label="Select Pan-Australia region preset">
          <svg class="tf-preset-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"></path>
            <circle cx="12" cy="10" r="3"></circle>
          </svg>
          <select class="tf-preset-select" data-role="preset-select">
            <optgroup label="Iconic 4WD Regions (${iconicRegions.length})">
              ${renderOptions(iconicRegions)}
            </optgroup>
            <optgroup label="Australian States &amp; Territories (${stateRegions.length})">
              ${renderOptions(stateRegions)}
            </optgroup>
            <optgroup label="4WD Corridors &amp; Landmarks">
              ${renderOptions(landmarks)}
            </optgroup>
          </select>
        </label>

        <button
          type="button"
          class="tf-nav-btn tf-route-btn"
          data-role="route-btn"
          title="Interactive Multi-Track Route Planner & GridForge Strip-Route Handoff"
        >
          <svg class="tf-nav-btn-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <circle cx="6" cy="19" r="3"></circle>
            <path d="M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15"></path>
            <circle cx="18" cy="5" r="3"></circle>
          </svg>
          <span>Route</span>
          <span class="tf-route-badge hidden mono" data-role="route-badge">0</span>
        </button>

        <button
          type="button"
          class="tf-nav-btn tf-packs-btn"
          data-role="packs-btn"
          title="Download Pan-Australia Offline Regional Packs & Manage Sync Queue"
        >
          <svg class="tf-nav-btn-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
            <polyline points="7 10 12 15 17 10"></polyline>
            <line x1="12" y1="15" x2="12" y2="3"></line>
          </svg>
          <span class="tf-status-dot online hidden" data-role="packs-dot"></span>
          <span data-role="packs-label">Packs</span>
        </button>

        <span class="tf-topbar-divider" aria-hidden="true"></span>

        <div class="tf-status-wrap" data-role="status-wrap">
          <button
            type="button"
            class="tf-status-btn"
            data-role="status-btn"
            title="System & AU Agency Feeds status"
            aria-label="System and AU Agency Feeds status"
            aria-haspopup="true"
            aria-expanded="false"
          >
            <span class="tf-status-dot connecting" data-role="status-dot"></span>
            <span class="tf-status-badge hidden" data-role="status-badge">Alert</span>
          </button>

          <div class="tf-status-menu hidden" data-role="status-menu" role="dialog" aria-label="System and feeds status">
            <div class="tf-status-menu-title">System &amp; Live Feeds</div>

            <button
              type="button"
              class="tf-status-menu-row"
              data-role="feed-pill"
              title="Open AU agency closure feeds in sidebar & refresh"
            >
              <span class="tf-status-dot connecting" data-role="feed-dot"></span>
              <span class="tf-status-menu-copy">
                <span class="tf-status-menu-label" data-role="feed-text">Feeds …</span>
                <span class="tf-status-menu-sub" data-role="feed-detail">Checking AU agency closure feeds…</span>
              </span>
              <span class="tf-status-menu-action">View</span>
            </button>

            <button
              type="button"
              class="tf-status-menu-row"
              data-role="health-pill"
              title="Refresh backend /api/health status"
            >
              <span class="tf-status-dot connecting" data-role="health-dot"></span>
              <span class="tf-status-menu-copy">
                <span class="tf-status-menu-label" data-role="health-text">API …</span>
                <span class="tf-status-menu-sub" data-role="health-detail">Checking backend health…</span>
              </span>
              <span class="tf-status-menu-action">Refresh</span>
            </button>
          </div>
        </div>

        <span class="tf-topbar-divider" aria-hidden="true"></span>

        <button
          type="button"
          class="tf-account-btn"
          data-role="rig-btn"
          title="Vehicle Profiles, Rig Suitability, Saved Tracks & Account"
        >
          <span class="tf-account-dot hidden" data-role="rig-dot" aria-hidden="true"></span>
          <svg class="tf-account-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M5 17h14"></path>
            <path d="M5 17a2 2 0 0 1-2-2v-3l2-5h11l3 4 2 1v3a2 2 0 0 1-2 2"></path>
            <circle cx="7.5" cy="17.5" r="2"></circle>
            <circle cx="16.5" cy="17.5" r="2"></circle>
          </svg>
          <span class="tf-account-label" data-role="rig-label">Stock 4WD</span>
        </button>

        <button
          type="button"
          class="tf-nav-btn tf-admin-btn hidden"
          data-role="admin-btn"
          title="Admin Console — Traffic, Storage, Friend Invites & Users"
        >
          <svg class="tf-admin-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path>
          </svg>
          <span>Admin</span>
        </button>
      </div>
    `;

    this.sidebarBtnEl = this.q<HTMLButtonElement>(
      "[data-role='topbar-sidebar-btn']"
    );
    this.searchWrapEl = this.q("[data-role='search-wrap']");
    this.searchFormEl = this.q<HTMLFormElement>("[data-role='search-form']");
    this.searchInputEl = this.q<HTMLInputElement>("[data-role='search-input']");
    this.searchKbdEl = this.q("[data-role='search-kbd']");
    this.searchClearBtn = this.q<HTMLButtonElement>("[data-role='search-clear']");
    this.searchMenuEl = this.q("[data-role='search-menu']");
    this.presetWrapEl = this.q(".tf-preset-wrap");
    this.presetSelectEl = this.q<HTMLSelectElement>("[data-role='preset-select']");
    this.syncPresetTooltip();
    this.routeBtnEl = this.q<HTMLButtonElement>("[data-role='route-btn']");
    this.routeBadgeEl = this.q("[data-role='route-badge']");
    this.statusWrapEl = this.q("[data-role='status-wrap']");
    this.statusBtnEl = this.q<HTMLButtonElement>("[data-role='status-btn']");
    this.statusDotEl = this.q("[data-role='status-dot']");
    this.statusBadgeEl = this.q("[data-role='status-badge']");
    this.statusMenuEl = this.q("[data-role='status-menu']");
    this.feedPillEl = this.q<HTMLButtonElement>("[data-role='feed-pill']");
    this.feedDotEl = this.q("[data-role='feed-dot']");
    this.feedTextEl = this.q("[data-role='feed-text']");
    this.feedDetailEl = this.q("[data-role='feed-detail']");
    this.rigBtnEl = this.q<HTMLButtonElement>("[data-role='rig-btn']");
    this.rigDotEl = this.q("[data-role='rig-dot']");
    this.rigLabelEl = this.q("[data-role='rig-label']");
    this.adminBtnEl = this.q<HTMLButtonElement>("[data-role='admin-btn']");
    this.packsBtnEl = this.q<HTMLButtonElement>("[data-role='packs-btn']");
    this.packsDotEl = this.q("[data-role='packs-dot']");
    this.packsLabelEl = this.q("[data-role='packs-label']");
    this.healthPillEl = this.q<HTMLButtonElement>("[data-role='health-pill']");
    this.healthDotEl = this.q("[data-role='health-dot']");
    this.healthTextEl = this.q("[data-role='health-text']");
    this.healthDetailEl = this.q("[data-role='health-detail']");

    this.sidebarBtnEl.addEventListener("click", () => {
      callbacks.onToggleSidebar?.();
    });

    this.searchFormEl.addEventListener("submit", (e) => {
      e.preventDefault();
      void this.handleSubmit();
    });

    this.searchInputEl.addEventListener("input", () => {
      this.ensurePlacesPreloaded();
      const rawValue = this.searchInputEl.value;
      const q = rawValue.trim();
      this.syncSearchAccessoryState(q.length > 0);

      const localItems = q ? resolveLocalSearch(q) : [];
      const cachedItems = q
        ? peekCachedUnifiedSearch(q, { limit: TOPBAR_SEARCH_LIMIT })
        : null;

      const next = computeSearchInputTransition({
        rawValue,
        localItems,
        cachedItems,
      });

      if (next.shouldCancelPending) {
        this.cancelPendingSearch();
      } else if (this.inFlightQuery !== null && this.inFlightQuery !== q) {
        this.abortInFlightSearch();
      }

      this.applySearchTransition(next, q);

      if (next.shouldScheduleRemote) {
        this.scheduleSearch(q);
      }
    });

    this.searchInputEl.addEventListener("focus", () => {
      this.searchKbdEl.classList.add("hidden");
      this.ensurePlacesPreloaded();
    });

    this.searchInputEl.addEventListener("blur", () => {
      this.syncSearchAccessoryState(this.searchInputEl.value.trim().length > 0);
    });

    this.searchInputEl.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" && this.results.length > 0) {
        e.preventDefault();
        this.activeIdx = (this.activeIdx + 1) % this.results.length;
        this.highlightMenuOption();
      } else if (e.key === "ArrowUp" && this.results.length > 0) {
        e.preventDefault();
        this.activeIdx =
          (this.activeIdx - 1 + this.results.length) % this.results.length;
        this.highlightMenuOption();
      } else if (e.key === "Escape") {
        this.cancelPendingSearch();
        this.setSearchingState(false);
        this.hideMenu();
      }
    });

    this.statusBtnEl.addEventListener("click", (e) => {
      e.stopPropagation();
      const willOpen = this.statusMenuEl.classList.contains("hidden");
      this.setStatusMenuOpen(willOpen);
    });

    document.addEventListener("click", (e) => {
      const target = e.target as Node;
      if (!this.searchWrapEl.contains(target)) {
        this.cancelPendingSearch();
        this.setSearchingState(false);
        this.hideMenu();
      }
      if (!this.statusWrapEl.contains(target)) {
        this.setStatusMenuOpen(false);
      }
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !this.statusMenuEl.classList.contains("hidden")) {
        this.setStatusMenuOpen(false);
      }
    });

    this.searchClearBtn.addEventListener("click", () => {
      const cleared = computeSearchClearTransition();
      this.cancelPendingSearch();
      this.searchInputEl.value = "";
      this.syncSearchAccessoryState(false);
      this.applySearchTransition(cleared, "");
      callbacks.onSearchClear();
      this.searchInputEl.focus();
    });

    this.presetSelectEl.addEventListener("change", () => {
      this.syncPresetTooltip();
      callbacks.onPresetChange(this.presetSelectEl.value);
    });

    this.routeBtnEl.addEventListener("click", () => {
      callbacks.onRoutePlannerClick();
    });

    this.feedPillEl.addEventListener("click", () => {
      this.setStatusMenuOpen(false);
      callbacks.onFeedStatusClick();
    });

    this.rigBtnEl.addEventListener("click", () => {
      callbacks.onRigAccountClick();
    });

    this.adminBtnEl.addEventListener("click", () => {
      callbacks.onAdminClick?.();
    });

    this.packsBtnEl.addEventListener("click", () => {
      callbacks.onOfflinePacksClick();
    });

    this.healthPillEl.addEventListener("click", () => {
      callbacks.onHealthClick();
    });
  }

  updateSidebarState(collapsed: boolean): void {
    this.sidebarBtnEl.classList.toggle("active", !collapsed);
    this.sidebarBtnEl.setAttribute("aria-expanded", String(!collapsed));
    this.sidebarBtnEl.setAttribute(
      "aria-label",
      collapsed ? "Show layers sidebar" : "Hide layers sidebar"
    );
    this.sidebarBtnEl.title = collapsed
      ? "Show layers sidebar (shortcut: [)"
      : "Hide layers sidebar (shortcut: [)";
  }

  focusSearch(): void {
    this.searchInputEl.focus();
    this.searchInputEl.select();
  }

  setSearchValue(value: string): void {
    this.searchInputEl.value = value;
    this.syncSearchAccessoryState(value.trim().length > 0);
  }

  setSelectedPreset(presetId: string): void {
    this.presetSelectEl.value = presetId;
    this.syncPresetTooltip();
  }

  private syncPresetTooltip(): void {
    const selected = this.presetSelectEl.selectedOptions[0]?.textContent?.trim();
    this.presetWrapEl.title = selected
      ? `Region: ${selected}`
      : "Jump to Australian region or corridor";
  }

  updateRouteSummary(params: {
    legCount: number;
    planModeActive: boolean;
    panelOpen: boolean;
  }): void {
    const count = Math.max(0, Math.round(params.legCount));
    this.routeBadgeEl.textContent = String(count);
    this.routeBadgeEl.classList.toggle("hidden", count === 0);
    this.routeBtnEl.classList.toggle(
      "active-route",
      params.planModeActive || params.panelOpen || count > 0
    );
    this.routeBtnEl.title =
      count > 0
        ? `Route Planner (${count} leg${count === 1 ? "" : "s"}${params.planModeActive ? " · Click-to-Chain active" : ""})`
        : "Interactive Multi-Track Route Planner & GridForge Strip-Route Handoff";
  }

  updateOfflineSummary(params: {
    online: boolean;
    downloadedCount: number;
    activePackId: string | null;
    pendingSyncCount: number;
  }): void {
    const showDot =
      !params.online ||
      Boolean(params.activePackId) ||
      params.pendingSyncCount > 0;
    this.packsDotEl.className = "tf-status-dot";
    this.packsDotEl.classList.toggle("hidden", !showDot);
    if (!params.online) {
      this.packsDotEl.classList.add("degraded");
    } else if (params.activePackId) {
      this.packsDotEl.classList.add("connecting");
    } else {
      this.packsDotEl.classList.add("online");
    }

    const parts: string[] = [];
    if (!params.online) {
      parts.push("Offline");
    } else if (params.activePackId) {
      parts.push(`Pack: ${params.activePackId}`);
    } else if (params.downloadedCount > 0) {
      parts.push(`Packs (${params.downloadedCount})`);
    } else {
      parts.push("Packs");
    }

    if (params.pendingSyncCount > 0) {
      parts.push(`↑${params.pendingSyncCount}`);
    }

    this.packsLabelEl.textContent = parts.join(" · ");
    this.packsBtnEl.classList.toggle(
      "active-subtle",
      !params.online || Boolean(params.activePackId) || params.downloadedCount > 0
    );
    this.packsBtnEl.title = `${params.online ? "Online" : "Offline"} · ${params.downloadedCount} regional pack(s) cached · ${params.pendingSyncCount} pending sync item(s)`;
  }

  updateAdminAccess(isAdmin: boolean, panelOpen = false): void {
    this.adminBtnEl.classList.toggle("hidden", !isAdmin);
    this.adminBtnEl.classList.toggle("active-admin", isAdmin && panelOpen);
  }

  updateRigSummary(
    vehicleClass: VehicleCapabilityClass,
    vehicleName: string | null,
    userName: string | null,
    savedCount: number
  ): void {
    const rigText = vehicleName ? `${vehicleName}` : vehicleClass;
    const suffix = savedCount > 0 ? ` · ★${savedCount}` : "";
    this.rigLabelEl.textContent = `${rigText}${suffix}`;
    this.rigBtnEl.classList.toggle("signed-in", Boolean(userName));
    this.rigDotEl.classList.toggle("hidden", !userName);
    this.rigBtnEl.title = userName
      ? `Signed in as ${userName} · Active rig: ${vehicleName ?? vehicleClass}`
      : `Anonymous mode · Rig preview: ${vehicleClass} (click to switch rig or sign in)`;
  }

  updateFeedStatus(status: ClosuresStatusResponse | null): void {
    const summary = summarizeFeedStatus(status);
    this.feedDotEl.className = "tf-status-dot";
    if (summary.kind === "healthy") {
      this.feedDotEl.classList.add("online");
      this.feedSeverity = "online";
    } else if (summary.kind === "degraded" || summary.kind === "stale") {
      this.feedDotEl.classList.add("degraded");
      this.feedSeverity = "degraded";
    } else {
      this.feedDotEl.classList.add("offline");
      this.feedSeverity = "offline";
    }
    this.feedTextEl.textContent = summary.label;
    this.feedDetailEl.textContent = summary.tooltip;
    this.feedSummaryTitle = summary.label;
    this.feedPillEl.classList.toggle(
      "warn",
      summary.kind === "degraded" || summary.kind === "stale"
    );
    this.syncCombinedStatusIndicator();
  }

  updateHealth(state: HealthState): void {
    this.healthDotEl.className = "tf-status-dot";

    if (state.kind === "connecting") {
      this.healthDotEl.classList.add("connecting");
      this.healthTextEl.textContent = "API Connecting…";
      this.healthDetailEl.textContent = "Checking /api/health…";
      this.healthSeverity = "connecting";
      this.healthSummaryTitle = "API connecting…";
      this.syncCombinedStatusIndicator();
      return;
    }

    if (state.kind === "online" && state.data) {
      this.healthDotEl.classList.add("online");
      this.healthTextEl.textContent = "API Online";
      const details = [
        state.data.version ? `v${state.data.version}` : null,
        state.data.postgis_version
          ? `PostGIS ${state.data.postgis_version}`
          : null,
        state.data.schema_revision
          ? `Rev ${state.data.schema_revision}`
          : null,
      ]
        .filter(Boolean)
        .join(" · ");
      this.healthDetailEl.textContent = details || "Database healthy";
      this.healthSeverity = "online";
      this.healthSummaryTitle = "API Online";
      this.syncCombinedStatusIndicator();
      return;
    }

    if (state.kind === "degraded") {
      this.healthDotEl.classList.add("degraded");
      this.healthTextEl.textContent = "API Degraded";
      this.healthDetailEl.textContent =
        state.error ?? "Database health check reported degraded";
      this.healthSeverity = "degraded";
      this.healthSummaryTitle = "API Degraded";
      this.syncCombinedStatusIndicator();
      return;
    }

    this.healthDotEl.classList.add("offline");
    this.healthTextEl.textContent = "API Offline";
    this.healthDetailEl.textContent =
      state.error ?? "Backend unreachable — operating in offline shell mode";
    this.healthSeverity = "offline";
    this.healthSummaryTitle = "API Offline";
    this.syncCombinedStatusIndicator();
  }

  private syncCombinedStatusIndicator(): void {
    const severities = [this.healthSeverity, this.feedSeverity];
    let overall: "online" | "degraded" | "offline" | "connecting" = "online";
    if (severities.includes("offline")) {
      overall = "offline";
    } else if (severities.includes("degraded")) {
      overall = "degraded";
    } else if (severities.includes("connecting")) {
      overall = "connecting";
    }

    this.statusDotEl.className = `tf-status-dot ${overall}`;
    this.statusBtnEl.classList.toggle("warn", overall === "degraded");
    this.statusBtnEl.classList.toggle("danger", overall === "offline");

    if (overall === "offline") {
      this.statusBadgeEl.textContent = "Offline";
      this.statusBadgeEl.classList.remove("hidden");
    } else if (overall === "degraded") {
      this.statusBadgeEl.textContent = "Notice";
      this.statusBadgeEl.classList.remove("hidden");
    } else {
      this.statusBadgeEl.classList.add("hidden");
    }

    this.statusBtnEl.title = `${this.healthSummaryTitle} · ${this.feedSummaryTitle} (click for details)`;
  }

  private setStatusMenuOpen(open: boolean): void {
    this.statusMenuEl.classList.toggle("hidden", !open);
    this.statusBtnEl.setAttribute("aria-expanded", String(open));
    this.statusBtnEl.classList.toggle("open", open);
  }

  private syncSearchAccessoryState(hasValue: boolean): void {
    this.searchClearBtn.classList.toggle("hidden", !hasValue);
    const focused = document.activeElement === this.searchInputEl;
    this.searchKbdEl.classList.toggle("hidden", hasValue || focused);
  }

  private setSearchingState(searching: boolean): void {
    this.isSearching = searching;
    this.searchFormEl.classList.toggle("is-searching", searching);
    this.searchInputEl.setAttribute("aria-busy", String(searching));
  }

  private ensurePlacesPreloaded(): void {
    if (this.placesPreloadStarted) return;
    this.placesPreloadStarted = true;
    void preloadAuPlaces().then((places) => {
      if (places.length === 0) return;
      const q = this.searchInputEl.value.trim();
      if (!q || this.resultsQuery === q) return;
      if (this.searchMenuEl.classList.contains("hidden")) return;

      const localItems = resolveLocalSearch(q);
      if (localItems.length === 0) return;

      if (q.length < MIN_REMOTE_SEARCH_LENGTH) {
        this.results = localItems;
        this.activeIdx = 0;
        this.renderMenu(localItems, {
          remoteError: false,
          footerMode: "min_chars_hint",
          query: q,
        });
      } else if (this.isSearching) {
        this.results = localItems;
        this.activeIdx = 0;
        this.renderMenu(localItems, {
          remoteError: false,
          footerMode: "searching",
          query: q,
        });
      }
    });
  }

  private abortInFlightSearch(): void {
    this.searchAbort?.abort();
    this.searchAbort = null;
    this.inFlightQuery = null;
    this.inFlightPromise = null;
  }

  private cancelPendingSearch(): void {
    if (this.searchTimer !== null) {
      window.clearTimeout(this.searchTimer);
      this.searchTimer = null;
    }
    this.abortInFlightSearch();
  }

  private applySearchTransition(
    transition: TopBarSearchTransition,
    query: string
  ): void {
    this.setSearchingState(transition.isSearching);
    this.results = transition.results;
    this.resultsQuery = transition.resultsQuery;
    this.activeIdx = transition.activeIdx;
    this.lastRemoteError = transition.remoteError;

    switch (transition.menuMode) {
      case "hidden":
        this.hideMenu();
        break;
      case "searching":
        this.renderSearchingMenu(query);
        break;
      case "hint":
        this.renderHintMenu();
        break;
      case "empty":
        this.renderMenu([], {
          remoteError: transition.remoteError,
          footerMode: "none",
          query,
        });
        break;
      case "results":
        this.renderMenu(transition.results, {
          remoteError: transition.remoteError,
          footerMode: transition.footerMode,
          query,
        });
        break;
    }
  }

  private scheduleSearch(query: string): void {
    if (this.searchTimer !== null) {
      window.clearTimeout(this.searchTimer);
    }
    this.searchTimer = window.setTimeout(() => {
      this.searchTimer = null;
      void this.executeSearch(query);
    }, SEARCH_DEBOUNCE_MS);
  }

  private async executeSearch(query: string): Promise<UnifiedSearchItem[]> {
    if (this.inFlightQuery === query && this.inFlightPromise) {
      return this.inFlightPromise;
    }

    this.searchAbort?.abort();
    const controller = new AbortController();
    this.searchAbort = controller;
    this.inFlightQuery = query;
    this.setSearchingState(true);

    const promise = (async (): Promise<UnifiedSearchItem[]> => {
      try {
        const { items, remoteError } = await runUnifiedSearch(query, {
          limit: TOPBAR_SEARCH_LIMIT,
          signal: controller.signal,
        });
        if (
          controller.signal.aborted ||
          this.searchInputEl.value.trim() !== query
        ) {
          return [];
        }
        const resolved = computeSearchResolvedTransition({
          query,
          items,
          remoteError,
        });
        this.applySearchTransition(resolved, query);
        return items;
      } catch {
        return [];
      } finally {
        if (this.inFlightQuery === query && this.searchAbort === controller) {
          this.inFlightQuery = null;
          this.inFlightPromise = null;
          if (this.searchTimer === null) {
            this.setSearchingState(false);
          }
        }
      }
    })();

    this.inFlightPromise = promise;
    return promise;
  }

  private async handleSubmit(): Promise<void> {
    const query = this.searchInputEl.value.trim();
    if (!query) return;

    if (this.searchTimer !== null) {
      window.clearTimeout(this.searchTimer);
      this.searchTimer = null;
    }

    const action = computeSearchSubmitAction({
      query,
      resultsQuery: this.resultsQuery,
      results: this.results,
      activeIdx: this.activeIdx,
    });

    if (action.kind === "noop") return;
    if (action.kind === "select") {
      this.selectResult(action.item);
      return;
    }
    if (action.kind === "hint") {
      this.setSearchingState(false);
      this.renderHintMenu();
      return;
    }

    this.setSearchingState(true);
    if (this.results.length > 0) {
      this.renderMenu(this.results, {
        remoteError: false,
        footerMode: "searching",
        query,
      });
    } else {
      this.renderSearchingMenu(query);
    }

    const items = await this.executeSearch(query);
    if (this.searchInputEl.value.trim() !== query) return;
    if (items.length > 0 && items[0]) {
      this.selectResult(items[0]);
    } else {
      this.hideMenu();
      this.callbacks.onSearchNoMatch(query, this.lastRemoteError);
    }
  }

  private renderHintMenu(): void {
    this.searchMenuEl.innerHTML = `<div class="tf-search-hint">${escapeHtml(MIN_CHARS_SEARCH_HINT)}</div>`;
    this.searchMenuEl.classList.remove("hidden");
    this.searchInputEl.setAttribute("aria-expanded", "true");
  }

  private renderSearchingMenu(query: string): void {
    this.searchMenuEl.innerHTML = `
      <div class="tf-search-loading-head" role="status" aria-live="polite">
        <span class="tf-search-inline-spinner" aria-hidden="true"></span>
        <span class="tf-search-loading-label">Searching tracks, POIs &amp; parks for “${escapeHtml(query)}”…</span>
      </div>
      <div class="tf-search-skeleton-list" aria-hidden="true">
        <div class="tf-search-skeleton-row">
          <div class="tf-search-skeleton-copy">
            <span class="tf-search-skeleton-line tf-search-skeleton-title" style="width:64%"></span>
            <span class="tf-search-skeleton-line tf-search-skeleton-sub" style="width:42%"></span>
          </div>
          <span class="tf-search-skeleton-pill"></span>
        </div>
        <div class="tf-search-skeleton-row">
          <div class="tf-search-skeleton-copy">
            <span class="tf-search-skeleton-line tf-search-skeleton-title" style="width:52%"></span>
            <span class="tf-search-skeleton-line tf-search-skeleton-sub" style="width:36%"></span>
          </div>
          <span class="tf-search-skeleton-pill"></span>
        </div>
        <div class="tf-search-skeleton-row">
          <div class="tf-search-skeleton-copy">
            <span class="tf-search-skeleton-line tf-search-skeleton-title" style="width:58%"></span>
            <span class="tf-search-skeleton-line tf-search-skeleton-sub" style="width:46%"></span>
          </div>
          <span class="tf-search-skeleton-pill"></span>
        </div>
      </div>
    `;
    this.searchMenuEl.classList.remove("hidden");
    this.searchInputEl.setAttribute("aria-expanded", "true");
  }

  private selectResult(item: UnifiedSearchItem): void {
    this.cancelPendingSearch();
    this.setSearchingState(false);
    this.setSearchValue(item.title);
    this.hideMenu();
    this.callbacks.onSearchSelect(item);
  }

  private renderMenu(
    items: UnifiedSearchItem[],
    options: {
      remoteError: boolean;
      footerMode: SearchMenuFooterMode;
      query: string;
    }
  ): void {
    this.results = items;
    this.activeIdx = items.length > 0 ? 0 : -1;

    if (items.length === 0) {
      this.searchMenuEl.innerHTML = `<div class="tf-search-empty">${
        options.remoteError
          ? "Offline — enter lat,lon, MGRS, or a town/region name."
          : "No matching tracks, POIs, parks, or places."
      }</div>`;
      this.searchMenuEl.classList.remove("hidden");
      this.searchInputEl.setAttribute("aria-expanded", "true");
      return;
    }

    const rowsHtml = items
      .map((item, idx) => {
        const badgeClass =
          item.kind === "place"
            ? "tf-search-badge tf-search-badge-place"
            : item.kind === "park"
              ? "tf-search-badge tf-search-badge-park"
              : item.kind === "preset"
                ? "tf-search-badge tf-search-badge-preset"
                : "tf-search-badge";

        const kindBadge =
          item.kind === "track" && item.grade
            ? `<span class="tf-search-badge" style="border-color:${getGradeColor(item.grade)};color:${getGradeColor(item.grade)}">${escapeHtml(item.grade)}</span>`
            : `<span class="${badgeClass}">${escapeHtml(item.kind.toUpperCase())}</span>`;

        const iconImg = item.iconKey
          ? `<img class="tf-search-item-icon" src="/icons/${escapeHtml(item.iconKey)}.svg" alt="" width="16" height="16" />`
          : "";

        return `
          <button
            type="button"
            class="tf-search-option ${idx === this.activeIdx ? "active" : ""}"
            role="option"
            aria-selected="${idx === this.activeIdx}"
            data-search-idx="${idx}"
          >
            ${iconImg}
            <span class="tf-search-option-copy">
              <span class="tf-search-option-title">${escapeHtml(item.title)}</span>
              <span class="tf-search-option-sub">${escapeHtml(item.subtitle)}</span>
            </span>
            ${kindBadge}
          </button>
        `;
      })
      .join("");

    let footerHtml = "";
    if (options.footerMode === "searching") {
      footerHtml = `
        <div class="tf-search-pending-footer" data-role="search-pending-footer" role="status" aria-live="polite">
          <span class="tf-search-inline-spinner" aria-hidden="true"></span>
          <span class="tf-search-pending-footer-text">Searching tracks, POIs &amp; parks for “${escapeHtml(options.query)}”…</span>
        </div>
      `;
    } else if (options.footerMode === "min_chars_hint") {
      footerHtml = `
        <div class="tf-search-hint-footer" data-role="search-hint-footer">${escapeHtml(MIN_CHARS_SEARCH_HINT)}</div>
      `;
    }

    this.searchMenuEl.innerHTML = `${rowsHtml}${footerHtml}`;

    this.searchMenuEl
      .querySelectorAll<HTMLButtonElement>("[data-search-idx]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const idx = Number(btn.dataset.searchIdx);
          const item = this.results[idx];
          if (item) this.selectResult(item);
        });
      });

    this.searchMenuEl.classList.remove("hidden");
    this.searchInputEl.setAttribute("aria-expanded", "true");
  }

  private highlightMenuOption(): void {
    this.searchMenuEl
      .querySelectorAll<HTMLButtonElement>("[data-search-idx]")
      .forEach((btn, idx) => {
        const active = idx === this.activeIdx;
        btn.classList.toggle("active", active);
        btn.setAttribute("aria-selected", String(active));
      });
  }

  private hideMenu(): void {
    this.searchMenuEl.classList.add("hidden");
    this.searchInputEl.setAttribute("aria-expanded", "false");
    this.activeIdx = -1;
  }

  private q<T extends HTMLElement = HTMLElement>(selector: string): T {
    const el = this.root.querySelector<T>(selector);
    if (!el) throw new Error(`TopBar element not found: ${selector}`);
    return el;
  }
}
