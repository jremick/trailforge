import { REGION_PRESETS, escapeHtml } from "../presets";
import type { HealthState, RegionPreset } from "../types";

export interface TopBarCallbacks {
  onSearchSubmit: (query: string) => void;
  onSearchClear: () => void;
  onPresetChange: (presetId: string) => void;
  onHealthClick: () => void;
}

export class TopBar {
  private readonly root: HTMLElement;
  private readonly searchInputEl: HTMLInputElement;
  private readonly searchClearBtn: HTMLButtonElement;
  private readonly presetSelectEl: HTMLSelectElement;
  private readonly healthPillEl: HTMLButtonElement;
  private readonly healthDotEl: HTMLElement;
  private readonly healthTextEl: HTMLElement;

  constructor(
    container: HTMLElement,
    initialPresetId: string,
    callbacks: TopBarCallbacks
  ) {
    this.root = container;
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

      <form class="tf-search-form" data-role="search-form" role="search">
        <span class="tf-search-icon" aria-hidden="true">⌕</span>
        <input
          type="text"
          class="tf-search-input mono"
          data-role="search-input"
          placeholder="Search lat,lon · MGRS (55H FV 0064 4755) · tracks (Slice 03)…"
          aria-label="Search coordinates, MGRS, or VIC/NSW tracks"
          autocomplete="off"
          spellcheck="false"
        />
        <button
          type="button"
          class="tf-search-clear hidden"
          data-role="search-clear"
          title="Clear search pin"
          aria-label="Clear search"
        >×</button>
        <button type="submit" class="tf-search-submit" title="Go to coordinate or search">
          Go
        </button>
      </form>

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

    const searchForm = this.q<HTMLFormElement>("[data-role='search-form']");
    this.searchInputEl = this.q<HTMLInputElement>("[data-role='search-input']");
    this.searchClearBtn = this.q<HTMLButtonElement>("[data-role='search-clear']");
    this.presetSelectEl = this.q<HTMLSelectElement>("[data-role='preset-select']");
    this.healthPillEl = this.q<HTMLButtonElement>("[data-role='health-pill']");
    this.healthDotEl = this.q("[data-role='health-dot']");
    this.healthTextEl = this.q("[data-role='health-text']");

    searchForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const query = this.searchInputEl.value.trim();
      if (query) {
        callbacks.onSearchSubmit(query);
      }
    });

    this.searchInputEl.addEventListener("input", () => {
      this.searchClearBtn.classList.toggle(
        "hidden",
        this.searchInputEl.value.trim().length === 0
      );
    });

    this.searchClearBtn.addEventListener("click", () => {
      this.searchInputEl.value = "";
      this.searchClearBtn.classList.add("hidden");
      callbacks.onSearchClear();
      this.searchInputEl.focus();
    });

    this.presetSelectEl.addEventListener("change", () => {
      callbacks.onPresetChange(this.presetSelectEl.value);
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

  private q<T extends HTMLElement = HTMLElement>(selector: string): T {
    const el = this.root.querySelector<T>(selector);
    if (!el) throw new Error(`TopBar element not found: ${selector}`);
    return el;
  }
}
