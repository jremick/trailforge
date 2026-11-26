import { summarizeFeedStatus } from "../closuresHelpers";
import { REGION_PRESETS, escapeHtml, getGradeColor } from "../presets";
import { resolveLocalSearch, runUnifiedSearch } from "../search";
import type {
  ClosuresStatusResponse,
  HealthState,
  RegionPreset,
  UnifiedSearchItem,
  VehicleCapabilityClass,
} from "../types";

export interface TopBarCallbacks {
  onSearchSelect: (item: UnifiedSearchItem) => void;
  onSearchNoMatch: (query: string, remoteError: boolean) => void;
  onSearchClear: () => void;
  onPresetChange: (presetId: string) => void;
  onHealthClick: () => void;
  onFeedStatusClick: () => void;
  onRigAccountClick: () => void;
  onOfflinePacksClick: () => void;
}

const SEARCH_DEBOUNCE_MS = 180;

export class TopBar {
  private readonly root: HTMLElement;
  private readonly callbacks: TopBarCallbacks;
  private readonly searchWrapEl: HTMLElement;
  private readonly searchInputEl: HTMLInputElement;
  private readonly searchClearBtn: HTMLButtonElement;
  private readonly searchMenuEl: HTMLElement;
  private readonly presetSelectEl: HTMLSelectElement;
  private readonly feedPillEl: HTMLButtonElement;
  private readonly feedDotEl: HTMLElement;
  private readonly feedTextEl: HTMLElement;
  private readonly rigBtnEl: HTMLButtonElement;
  private readonly rigLabelEl: HTMLElement;
  private readonly packsBtnEl: HTMLButtonElement;
  private readonly packsDotEl: HTMLElement;
  private readonly packsLabelEl: HTMLElement;
  private readonly healthPillEl: HTMLButtonElement;
  private readonly healthDotEl: HTMLElement;
  private readonly healthTextEl: HTMLElement;

  private searchTimer: number | null = null;
  private searchAbort: AbortController | null = null;
  private results: UnifiedSearchItem[] = [];
  private activeIdx = -1;
  private lastRemoteError = false;

  constructor(
    container: HTMLElement,
    initialPresetId: string,
    callbacks: TopBarCallbacks
  ) {
    this.root = container;
    this.callbacks = callbacks;
    this.root.className = "tf-topbar";

    const regions = REGION_PRESETS.filter((p) => p.group === "region");
    const landmarks = REGION_PRESETS.filter((p) => p.group === "landmark");

    const renderOptions = (items: RegionPreset[]): string =>
      items
        .map(
          (p) =>
            `<option value="${escapeHtml(p.id)}" ${p.id === initialPresetId ? "selected" : ""}>${escapeHtml(p.label)}</option>`
        )
        .join("");

    this.root.innerHTML = `
      <div class="tf-brand">
        <div class="tf-brand-mark" aria-hidden="true">
          <svg width="26" height="26" viewBox="0 0 64 64" fill="none">
            <rect width="64" height="64" rx="14" fill="#131D26" stroke="#2C3E4F" stroke-width="2.5"/>
            <path d="M10 44 L23 22 L33 34 L42 19 L54 44 Z" fill="#182735" stroke="#38BDF8" stroke-width="2.5" stroke-linejoin="round"/>
            <path d="M12 49 C20 45, 25 41, 31 38 C37 35, 41 29, 52 24" stroke="#F59E0B" stroke-width="4" stroke-linecap="round"/>
            <circle cx="42" cy="19" r="3.8" fill="#22C55E" stroke="#0B1117" stroke-width="1.6"/>
          </svg>
        </div>
        <div class="tf-brand-text">
          <span class="tf-brand-name">TrailForge</span>
          <span class="tf-brand-badge">VIC · NSW</span>
        </div>
      </div>

      <div class="tf-search-wrap" data-role="search-wrap">
        <form class="tf-search-form" data-role="search-form" role="search">
          <span class="tf-search-icon" aria-hidden="true">⌕</span>
          <input
            type="text"
            class="tf-search-input mono"
            data-role="search-input"
            placeholder="Search tracks, huts, camps, lat,lon or MGRS…"
            aria-label="Search VIC/NSW tracks, camps, huts, coordinates, or MGRS"
            aria-autocomplete="list"
            aria-expanded="false"
            autocomplete="off"
            spellcheck="false"
          />
          <button
            type="button"
            class="tf-search-clear hidden"
            data-role="search-clear"
            title="Clear search"
            aria-label="Clear search"
          >×</button>
          <button type="submit" class="tf-search-submit" title="Search or jump to coordinate">
            Go
          </button>
        </form>
        <div class="tf-search-menu hidden" data-role="search-menu" role="listbox"></div>
      </div>

      <div class="tf-topbar-controls">
        <label class="tf-preset-wrap" aria-label="Select VIC/NSW region preset">
          <span class="tf-preset-label">Region</span>
          <select class="tf-preset-select" data-role="preset-select">
            <optgroup label="VIC &amp; NSW Regions">
              ${renderOptions(regions)}
            </optgroup>
            <optgroup label="4WD Corridors &amp; Landmarks">
              ${renderOptions(landmarks)}
            </optgroup>
          </select>
        </label>

        <button
          type="button"
          class="tf-rig-btn tf-packs-btn"
          data-role="packs-btn"
          title="Download VIC/NSW Offline Regional Packs & Manage Sync Queue"
        >
          <span class="tf-status-dot online" data-role="packs-dot"></span>
          <span data-role="packs-label">Offline Packs</span>
        </button>

        <button
          type="button"
          class="tf-rig-btn"
          data-role="rig-btn"
          title="Vehicle Profiles, Rig Suitability, Saved Tracks & Account"
        >
          <span class="tf-rig-icon" aria-hidden="true">⚙</span>
          <span data-role="rig-label">Stock 4WD</span>
        </button>

        <button
          type="button"
          class="tf-health-pill tf-feed-pill"
          data-role="feed-pill"
          title="Official VIC & NSW closure feeds status"
          aria-live="polite"
        >
          <span class="tf-status-dot connecting" data-role="feed-dot"></span>
          <span class="mono" data-role="feed-text">Feeds …</span>
        </button>

        <button
          type="button"
          class="tf-health-pill"
          data-role="health-pill"
          title="Backend /api/health status — click to refresh"
          aria-live="polite"
        >
          <span class="tf-status-dot" data-role="health-dot"></span>
          <span class="mono" data-role="health-text">API …</span>
        </button>
      </div>
    `;

    this.searchWrapEl = this.q("[data-role='search-wrap']");
    const searchForm = this.q<HTMLFormElement>("[data-role='search-form']");
    this.searchInputEl = this.q<HTMLInputElement>("[data-role='search-input']");
    this.searchClearBtn = this.q<HTMLButtonElement>("[data-role='search-clear']");
    this.searchMenuEl = this.q("[data-role='search-menu']");
    this.presetSelectEl = this.q<HTMLSelectElement>("[data-role='preset-select']");
    this.feedPillEl = this.q<HTMLButtonElement>("[data-role='feed-pill']");
    this.feedDotEl = this.q("[data-role='feed-dot']");
    this.feedTextEl = this.q("[data-role='feed-text']");
    this.rigBtnEl = this.q<HTMLButtonElement>("[data-role='rig-btn']");
    this.rigLabelEl = this.q("[data-role='rig-label']");
    this.packsBtnEl = this.q<HTMLButtonElement>("[data-role='packs-btn']");
    this.packsDotEl = this.q("[data-role='packs-dot']");
    this.packsLabelEl = this.q("[data-role='packs-label']");
    this.healthPillEl = this.q<HTMLButtonElement>("[data-role='health-pill']");
    this.healthDotEl = this.q("[data-role='health-dot']");
    this.healthTextEl = this.q("[data-role='health-text']");

    searchForm.addEventListener("submit", (e) => {
      e.preventDefault();
      void this.handleSubmit();
    });

    this.searchInputEl.addEventListener("input", () => {
      const q = this.searchInputEl.value.trim();
      this.searchClearBtn.classList.toggle("hidden", q.length === 0);
      if (!q) {
        this.hideMenu();
        return;
      }
      const instant = resolveLocalSearch(q);
      if (instant.length > 0) {
        this.renderMenu(instant, false);
      }
      this.scheduleSearch(q);
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
        this.hideMenu();
      }
    });

    document.addEventListener("click", (e) => {
      if (!this.searchWrapEl.contains(e.target as Node)) {
        this.hideMenu();
      }
    });

    this.searchClearBtn.addEventListener("click", () => {
      this.searchInputEl.value = "";
      this.searchClearBtn.classList.add("hidden");
      this.hideMenu();
      callbacks.onSearchClear();
      this.searchInputEl.focus();
    });

    this.presetSelectEl.addEventListener("change", () => {
      callbacks.onPresetChange(this.presetSelectEl.value);
    });

    this.feedPillEl.addEventListener("click", () => {
      callbacks.onFeedStatusClick();
    });

    this.rigBtnEl.addEventListener("click", () => {
      callbacks.onRigAccountClick();
    });

    this.packsBtnEl.addEventListener("click", () => {
      callbacks.onOfflinePacksClick();
    });

    this.healthPillEl.addEventListener("click", () => {
      callbacks.onHealthClick();
    });
  }

  focusSearch(): void {
    this.searchInputEl.focus();
    this.searchInputEl.select();
  }

  setSearchValue(value: string): void {
    this.searchInputEl.value = value;
    this.searchClearBtn.classList.toggle("hidden", value.trim().length === 0);
  }

  setSelectedPreset(presetId: string): void {
    this.presetSelectEl.value = presetId;
  }

  updateOfflineSummary(params: {
    online: boolean;
    downloadedCount: number;
    activePackId: string | null;
    pendingSyncCount: number;
  }): void {
    this.packsDotEl.className = "tf-status-dot";
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
      parts.push("Offline Packs");
    }

    if (params.pendingSyncCount > 0) {
      parts.push(`↑${params.pendingSyncCount}`);
    }

    this.packsLabelEl.textContent = parts.join(" · ");
    this.packsBtnEl.classList.toggle(
      "signed-in",
      Boolean(params.activePackId || params.downloadedCount > 0)
    );
    this.packsBtnEl.title = `${params.online ? "Online" : "Offline"} · ${params.downloadedCount} regional pack(s) cached · ${params.pendingSyncCount} pending sync item(s)`;
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
    this.rigBtnEl.title = userName
      ? `Signed in as ${userName} · Active rig: ${vehicleName ?? vehicleClass}`
      : `Anonymous mode · Rig preview: ${vehicleClass} (click to switch rig or sign in)`;
  }

  updateFeedStatus(status: ClosuresStatusResponse | null): void {
    const summary = summarizeFeedStatus(status);
    this.feedDotEl.className = "tf-status-dot";
    if (summary.kind === "healthy") {
      this.feedDotEl.classList.add("online");
    } else if (summary.kind === "degraded" || summary.kind === "stale") {
      this.feedDotEl.classList.add("degraded");
    } else {
      this.feedDotEl.classList.add("offline");
    }
    this.feedTextEl.textContent = summary.label;
    this.feedPillEl.title = `${summary.tooltip} (click to refresh)`;
    this.feedPillEl.classList.toggle(
      "warn",
      summary.kind === "degraded" || summary.kind === "stale"
    );
  }

  updateHealth(state: HealthState): void {
    this.healthDotEl.className = "tf-status-dot";

    if (state.kind === "connecting") {
      this.healthDotEl.classList.add("connecting");
      this.healthTextEl.textContent = "API …";
      this.healthPillEl.title = "Checking /api/health…";
      return;
    }

    if (state.kind === "online" && state.data) {
      this.healthDotEl.classList.add("online");
      const ver = state.data.version ? ` v${state.data.version}` : "";
      this.healthTextEl.textContent = `API Online${ver}`;
      const details = [
        `Status: ${state.data.status}`,
        state.data.postgis_version
          ? `PostGIS: ${state.data.postgis_version}`
          : null,
        state.data.schema_revision
          ? `Schema: ${state.data.schema_revision}`
          : null,
      ]
        .filter(Boolean)
        .join(" · ");
      this.healthPillEl.title = `${details} (click to refresh)`;
      return;
    }

    if (state.kind === "degraded") {
      this.healthDotEl.classList.add("degraded");
      this.healthTextEl.textContent = "API Degraded";
      this.healthPillEl.title =
        state.error ?? "API reachable but database is unavailable (click to retry)";
      return;
    }

    this.healthDotEl.classList.add("offline");
    this.healthTextEl.textContent = "API Offline";
    this.healthPillEl.title = state.error
      ? `Offline: ${state.error} (click to retry)`
      : "Backend unreachable — operating in offline shell mode";
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
    this.searchAbort?.abort();
    const controller = new AbortController();
    this.searchAbort = controller;

    try {
      const { items, remoteError } = await runUnifiedSearch(query, {
        limit: 10,
        signal: controller.signal,
      });
      if (controller.signal.aborted) return [];
      this.lastRemoteError = remoteError;
      this.renderMenu(items, remoteError);
      return items;
    } catch {
      return [];
    }
  }

  private async handleSubmit(): Promise<void> {
    const query = this.searchInputEl.value.trim();
    if (!query) return;

    if (this.activeIdx >= 0 && this.results[this.activeIdx]) {
      this.selectResult(this.results[this.activeIdx]!);
      return;
    }

    const items = await this.executeSearch(query);
    if (items.length > 0 && items[0]) {
      this.selectResult(items[0]);
    } else {
      this.hideMenu();
      this.callbacks.onSearchNoMatch(query, this.lastRemoteError);
    }
  }

  private selectResult(item: UnifiedSearchItem): void {
    this.setSearchValue(item.title);
    this.hideMenu();
    this.callbacks.onSearchSelect(item);
  }

  private renderMenu(items: UnifiedSearchItem[], remoteError: boolean): void {
    this.results = items;
    this.activeIdx = items.length > 0 ? 0 : -1;

    if (items.length === 0) {
      this.searchMenuEl.innerHTML = `<div class="tf-search-empty">${
        remoteError
          ? "Offline — enter lat,lon, MGRS, or a region name."
          : "No matching tracks, POIs, or parks."
      }</div>`;
      this.searchMenuEl.classList.remove("hidden");
      this.searchInputEl.setAttribute("aria-expanded", "true");
      return;
    }

    this.searchMenuEl.innerHTML = items
      .map((item, idx) => {
        const kindBadge =
          item.kind === "track" && item.grade
            ? `<span class="tf-search-badge" style="border-color:${getGradeColor(item.grade)};color:${getGradeColor(item.grade)}">${escapeHtml(item.grade)}</span>`
            : `<span class="tf-search-badge">${escapeHtml(item.kind.toUpperCase())}</span>`;

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
