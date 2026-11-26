import {
  formatRelativeTimestamp,
  summarizeFeedStatus,
} from "../closuresHelpers";
import {
  DEFAULT_SNOWLINE_BANDS_M,
  DIFFICULTY_META,
  OUTDOOR_POI_ICONS,
  escapeHtml,
  snapSnowlineBand,
} from "../presets";
import {
  SIDEBAR_DEFAULT_WIDTH_PX,
  SIDEBAR_KEYBOARD_STEP_PX,
  SIDEBAR_MAX_WIDTH_PX,
  SIDEBAR_MIN_WIDTH_PX,
} from "../sidebarHelpers";
import type { SwStatus } from "../pwa";
import type {
  ClosuresStatusResponse,
  HealthState,
  LayerKey,
  LayerVisibilityState,
  RegionPreset,
  TileMode,
} from "../types";

export interface SidebarCallbacks {
  onLayerToggle: (key: LayerKey, enabled: boolean) => void;
  onSnowLineElevationChange: (elevationM: number) => void;
  onResetRegion: () => void;
  onOpenOfflinePacks?: () => void;
  onCollapse?: () => void;
  onResizePreview?: (widthPx: number) => void;
  onResizeCommit?: (widthPx: number) => void;
  onResizeStateChange?: (resizing: boolean) => void;
  onResetWidth?: () => void;
}

interface LayerToggleDef {
  key: LayerKey;
  label: string;
  hint: string;
}

const LAYER_TOGGLES: readonly LayerToggleDef[] = [
  {
    key: "tracks",
    label: "4WD Tracks (Graded)",
    hint: "Canonical 50–100 m segments colored Easy → Extreme (click to inspect)",
  },
  {
    key: "steepHighlights",
    label: "Steep Climbs (≥14°)",
    hint: "Highlighted sustained & max gradient hazard pitches",
  },
  {
    key: "hotspots",
    label: "Obstacle Hotspots",
    hint: "Crowdsourced slow-crawl & high-roughness terrain pinch points",
  },
  {
    key: "closures",
    label: "Closures & Gates",
    hint: "Official AU state seasonal closures, fire/park alerts & locked gates",
  },
  {
    key: "reports",
    label: "Hazard & Gate Reports",
    hint: "Community trail condition reports & distinct locked-gate warnings",
  },
  {
    key: "unmapped",
    label: "Unmapped Track Spurs",
    hint: "Anonymized off-network GPS traces discovered outside canonical roads",
  },
  {
    key: "contoursHillshade",
    label: "Contours & Hillshade",
    hint: "20 m minor / 100 m index topo contours + Terrain-RGB shaded relief",
  },
  {
    key: "snowLine",
    label: "Alpine Snow-Line Band",
    hint: "Configurable elevation highlight (>1,000 m default)",
  },
  {
    key: "parksWaterways",
    label: "Parks & Waterways",
    hint: "CAPAD 2025 reserve boundaries & GA/OSM perennial/intermittent streams",
  },
  {
    key: "pois",
    label: "Outdoor POIs",
    hint: "Campgrounds, alpine huts, trigs, lookouts & access gates",
  },
];

export class Sidebar {
  private readonly root: HTMLElement;
  private readonly callbacks: SidebarCallbacks;
  private readonly snowSliderRowEl: HTMLElement;
  private readonly snowSliderEl: HTMLInputElement;
  private readonly snowValEl: HTMLElement;
  private readonly snowTicksEl: HTMLElement;
  private readonly tileModePillEl: HTMLElement;
  private readonly feedsSectionEl: HTMLDetailsElement;
  private readonly feedsPillEl: HTMLElement;
  private readonly feedsListEl: HTMLElement;
  private readonly regionNameEl: HTMLElement;
  private readonly regionTagEl: HTMLElement;
  private readonly regionDescEl: HTMLElement;
  private readonly regionBboxEl: HTMLElement;
  private readonly regionPackEl: HTMLElement;
  private readonly sysTilesEl: HTMLElement;
  private readonly sysApiEl: HTMLElement;
  private readonly sysPostgisEl: HTMLElement;
  private readonly sysSchemaEl: HTMLElement;
  private readonly sysNetworkEl: HTMLElement;
  private readonly sysSwEl: HTMLElement;
  private readonly collapseBtnEl: HTMLButtonElement;
  private readonly resizerEl: HTMLElement;

  private snowBands: number[] = [...DEFAULT_SNOWLINE_BANDS_M];
  private currentWidthPx: number = SIDEBAR_DEFAULT_WIDTH_PX;
  private maxWidthPx: number = SIDEBAR_MAX_WIDTH_PX;

  constructor(
    container: HTMLElement,
    initialLayers: LayerVisibilityState,
    initialPreset: RegionPreset,
    callbacks: SidebarCallbacks
  ) {
    this.root = container;
    this.callbacks = callbacks;
    this.root.className = "tf-sidebar";
    this.root.setAttribute("aria-label", "Map layers and trail controls");

    const previewIcons = OUTDOOR_POI_ICONS.slice(0, 7);
    const snappedInit = snapSnowlineBand(
      initialLayers.snowLineElevationM,
      this.snowBands
    );
    const initIdx = Math.max(0, this.snowBands.indexOf(snappedInit));

    this.root.innerHTML = `
      <div class="tf-sidebar-head">
        <div class="tf-sidebar-head-title">
          <svg class="tf-sidebar-head-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <polygon points="12 2 2 7 12 12 22 7 12 2"></polygon>
            <polyline points="2 12 12 17 22 12"></polyline>
            <polyline points="2 17 12 22 22 17"></polyline>
          </svg>
          <span>Layers &amp; Controls</span>
        </div>
        <button
          type="button"
          class="tf-sidebar-collapse-btn"
          data-role="sidebar-collapse-btn"
          title="Hide sidebar (shortcut: [)"
          aria-label="Hide layers sidebar"
          aria-expanded="true"
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <polyline points="15 18 9 12 15 6"></polyline>
          </svg>
          <span>Hide</span>
        </button>
      </div>
      <div class="tf-sidebar-scroll">
        <details class="tf-section" open>
          <summary class="tf-section-summary">
            <span class="tf-section-title-group">
              <span class="tf-section-index">01</span>
              <span class="tf-section-title">Map Layers</span>
            </span>
            <span class="tf-section-pill" data-role="tile-mode-pill">Loading…</span>
          </summary>

          <div class="tf-section-body">
            <div class="tf-layer-list">
              ${LAYER_TOGGLES.map(
                (item) => `
                  <label class="tf-toggle-row">
                    <input
                      type="checkbox"
                      class="tf-checkbox"
                      data-layer-key="${escapeHtml(item.key)}"
                      ${initialLayers[item.key] ? "checked" : ""}
                    />
                    <span class="tf-toggle-copy">
                      <span class="tf-toggle-label">${escapeHtml(item.label)}</span>
                      <span class="tf-toggle-hint">${escapeHtml(item.hint)}</span>
                    </span>
                  </label>
                `
              ).join("")}
            </div>

            <div class="tf-snow-control ${initialLayers.snowLine ? "" : "dimmed"}" data-role="snow-control">
              <div class="tf-snow-head">
                <span>Snow-Line Threshold</span>
                <span class="mono tf-accent-cyan" data-role="snow-val">&ge; ${snappedInit.toLocaleString()} m</span>
              </div>
              <input
                type="range"
                class="tf-range"
                data-role="snow-slider"
                min="0"
                max="${this.snowBands.length - 1}"
                step="1"
                value="${initIdx}"
                aria-label="Snow-line elevation threshold in meters"
              />
              <div class="tf-snow-ticks mono" data-role="snow-ticks"></div>
            </div>

            <div class="tf-legend-block">
              <div class="tf-subhead">Track Difficulty Grade</div>
              <div class="tf-difficulty-grid">
                ${DIFFICULTY_META.map(
                  (d) => `
                    <div class="tf-diff-item" title="${escapeHtml(d.hint)}">
                      <span class="tf-diff-swatch" style="background:${d.color}"></span>
                      <span>${escapeHtml(d.label)}</span>
                    </div>
                  `
                ).join("")}
              </div>
            </div>

            <div class="tf-legend-block">
              <div class="tf-subhead">Outdoor POI Symbols (${OUTDOOR_POI_ICONS.length} loaded)</div>
              <div class="tf-icon-strip">
                ${previewIcons
                  .map(
                    (icon) => `
                      <span class="tf-icon-chip" title="${escapeHtml(icon.label)}">
                        <img src="${escapeHtml(icon.iconPath)}" alt="${escapeHtml(icon.label)}" width="16" height="16" loading="lazy" />
                        <span>${escapeHtml(icon.label.split(" ")[0] ?? icon.label)}</span>
                      </span>
                    `
                  )
                  .join("")}
              </div>
            </div>
          </div>
        </details>

        <details class="tf-section" data-role="feeds-section">
          <summary class="tf-section-summary">
            <span class="tf-section-title-group">
              <span class="tf-section-index">02</span>
              <span class="tf-section-title">AU Agency Feeds</span>
            </span>
            <span class="tf-section-pill mono" data-role="feeds-pill">Checking…</span>
          </summary>

          <div class="tf-section-body" data-role="feeds-list">
            <div class="tf-inspector-muted">Loading official agency closure feeds…</div>
          </div>
        </details>

        <details class="tf-section" open>
          <summary class="tf-section-summary">
            <span class="tf-section-title-group">
              <span class="tf-section-index">03</span>
              <span class="tf-section-title">Active Corridor</span>
            </span>
            <span class="tf-section-pill mono" data-role="region-tag">${escapeHtml(initialPreset.stateTag)}</span>
          </summary>

          <div class="tf-section-body">
            <div class="tf-region-title" data-role="region-name">${escapeHtml(initialPreset.label)}</div>
            <p class="tf-region-desc" data-role="region-desc">${escapeHtml(initialPreset.description)}</p>
            <div class="tf-kv-grid">
              <span class="tf-kv-key">Bounds</span>
              <span class="tf-kv-val mono" data-role="region-bbox"></span>
              <span class="tf-kv-key">Offline Pack</span>
              <span class="tf-kv-val mono" data-role="region-pack"></span>
            </div>
            <div class="tf-btn-row">
              <button type="button" class="tf-btn-secondary" data-role="reset-region">
                ⌖ Recenter
              </button>
              <button type="button" class="tf-btn-secondary" data-role="open-packs">
                ⬇ Offline Packs
              </button>
            </div>
          </div>
        </details>

        <details class="tf-section">
          <summary class="tf-section-summary">
            <span class="tf-section-title-group">
              <span class="tf-section-index">04</span>
              <span class="tf-section-title">Stack &amp; Offline Telemetry</span>
            </span>
            <span class="tf-section-pill">v0.1</span>
          </summary>

          <div class="tf-section-body">
            <div class="tf-kv-grid">
              <span class="tf-kv-key">Vector/DEM</span>
              <span class="tf-kv-val mono" data-role="sys-tiles">Checking…</span>
              <span class="tf-kv-key">API Health</span>
              <span class="tf-kv-val mono" data-role="sys-api">Connecting…</span>
              <span class="tf-kv-key">PostGIS</span>
              <span class="tf-kv-val mono" data-role="sys-postgis">—</span>
              <span class="tf-kv-key">Schema Rev</span>
              <span class="tf-kv-val mono" data-role="sys-schema">—</span>
              <span class="tf-kv-key">Network</span>
              <span class="tf-kv-val mono" data-role="sys-network">Online</span>
              <span class="tf-kv-key">PWA Cache</span>
              <span class="tf-kv-val mono" data-role="sys-sw">Registering…</span>
            </div>
          </div>
        </details>
      </div>
      <div
        class="tf-sidebar-resizer"
        data-role="sidebar-resizer"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize layers sidebar (drag or use Left/Right arrows, double-click to reset)"
        aria-valuemin="${SIDEBAR_MIN_WIDTH_PX}"
        aria-valuemax="${SIDEBAR_MAX_WIDTH_PX}"
        aria-valuenow="${SIDEBAR_DEFAULT_WIDTH_PX}"
        tabindex="0"
        title="Drag to resize sidebar · Double-click to reset width"
      >
        <span class="tf-sidebar-resizer-bar" aria-hidden="true"></span>
      </div>
    `;

    this.snowSliderRowEl = this.q("[data-role='snow-control']");
    this.snowSliderEl = this.q<HTMLInputElement>("[data-role='snow-slider']");
    this.snowValEl = this.q("[data-role='snow-val']");
    this.snowTicksEl = this.q("[data-role='snow-ticks']");
    this.tileModePillEl = this.q("[data-role='tile-mode-pill']");
    this.feedsSectionEl = this.q<HTMLDetailsElement>("[data-role='feeds-section']");
    this.feedsPillEl = this.q("[data-role='feeds-pill']");
    this.feedsListEl = this.q("[data-role='feeds-list']");
    this.regionNameEl = this.q("[data-role='region-name']");
    this.regionTagEl = this.q("[data-role='region-tag']");
    this.regionDescEl = this.q("[data-role='region-desc']");
    this.regionBboxEl = this.q("[data-role='region-bbox']");
    this.regionPackEl = this.q("[data-role='region-pack']");
    this.sysTilesEl = this.q("[data-role='sys-tiles']");
    this.sysApiEl = this.q("[data-role='sys-api']");
    this.sysPostgisEl = this.q("[data-role='sys-postgis']");
    this.sysSchemaEl = this.q("[data-role='sys-schema']");
    this.sysNetworkEl = this.q("[data-role='sys-network']");
    this.sysSwEl = this.q("[data-role='sys-sw']");
    this.collapseBtnEl = this.q<HTMLButtonElement>("[data-role='sidebar-collapse-btn']");
    this.resizerEl = this.q("[data-role='sidebar-resizer']");

    this.renderSnowTicks();
    this.updatePreset(initialPreset);

    this.collapseBtnEl.addEventListener("click", () => {
      callbacks.onCollapse?.();
    });

    this.bindResizerEvents();

    this.root
      .querySelectorAll<HTMLInputElement>("[data-layer-key]")
      .forEach((input) => {
        input.addEventListener("change", () => {
          const key = input.dataset.layerKey as LayerKey;
          if (key === "snowLine") {
            this.snowSliderRowEl.classList.toggle("dimmed", !input.checked);
          }
          callbacks.onLayerToggle(key, input.checked);
        });
      });

    this.snowSliderEl.addEventListener("input", () => {
      const idx = Number(this.snowSliderEl.value);
      const elev = this.snowBands[idx] ?? 1000;
      this.snowValEl.textContent = `≥ ${elev.toLocaleString()} m`;
      this.callbacks.onSnowLineElevationChange(elev);
    });

    this.q("[data-role='reset-region']").addEventListener("click", () => {
      callbacks.onResetRegion();
    });

    this.root
      .querySelector("[data-role='open-packs']")
      ?.addEventListener("click", () => {
        callbacks.onOpenOfflinePacks?.();
      });
  }

  openFeedsSection(): void {
    this.feedsSectionEl.open = true;
    this.feedsSectionEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  updateWidthMeta(widthPx: number, maxWidthPx: number): void {
    this.currentWidthPx = widthPx;
    this.maxWidthPx = maxWidthPx;
    this.resizerEl.setAttribute("aria-valuenow", String(Math.round(widthPx)));
    this.resizerEl.setAttribute("aria-valuemax", String(Math.round(maxWidthPx)));
  }

  updateCollapsedState(collapsed: boolean): void {
    this.root.setAttribute("aria-hidden", String(collapsed));
    this.collapseBtnEl.setAttribute("aria-expanded", String(!collapsed));
  }

  private bindResizerEvents(): void {
    let activePointerId: number | null = null;
    let startX = 0;
    let startWidth = this.currentWidthPx;
    let latestWidth = this.currentWidthPx;

    const finishDrag = (commit: boolean): void => {
      if (activePointerId === null) return;
      const pid = activePointerId;
      activePointerId = null;
      this.resizerEl.classList.remove("dragging");
      try {
        if (this.resizerEl.hasPointerCapture?.(pid)) {
          this.resizerEl.releasePointerCapture(pid);
        }
      } catch {
        // Ignore release errors when pointer already ended
      }
      this.callbacks.onResizeStateChange?.(false);
      if (commit) {
        this.callbacks.onResizeCommit?.(latestWidth);
      }
    };

    this.resizerEl.addEventListener("pointerdown", (e: PointerEvent) => {
      if (e.button !== 0 && e.pointerType === "mouse") return;
      e.preventDefault();
      activePointerId = e.pointerId;
      startX = e.clientX;
      startWidth = this.root.getBoundingClientRect().width || this.currentWidthPx;
      latestWidth = startWidth;
      this.resizerEl.classList.add("dragging");
      try {
        this.resizerEl.setPointerCapture?.(e.pointerId);
      } catch {
        // Fallback to window listeners below
      }
      this.callbacks.onResizeStateChange?.(true);
    });

    window.addEventListener("pointermove", (e: PointerEvent) => {
      if (activePointerId === null || e.pointerId !== activePointerId) return;
      const nextWidth = startWidth + (e.clientX - startX);
      latestWidth = nextWidth;
      this.callbacks.onResizePreview?.(nextWidth);
    });

    window.addEventListener("pointerup", (e: PointerEvent) => {
      if (activePointerId === null || e.pointerId !== activePointerId) return;
      finishDrag(true);
    });

    window.addEventListener("pointercancel", (e: PointerEvent) => {
      if (activePointerId === null || e.pointerId !== activePointerId) return;
      finishDrag(true);
    });

    this.resizerEl.addEventListener("dblclick", (e) => {
      e.preventDefault();
      this.callbacks.onResetWidth?.();
    });

    this.resizerEl.addEventListener("keydown", (e: KeyboardEvent) => {
      if (e.key === "ArrowRight") {
        e.preventDefault();
        this.callbacks.onResizeCommit?.(
          this.currentWidthPx + SIDEBAR_KEYBOARD_STEP_PX
        );
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        this.callbacks.onResizeCommit?.(
          this.currentWidthPx - SIDEBAR_KEYBOARD_STEP_PX
        );
      } else if (e.key === "Home") {
        e.preventDefault();
        this.callbacks.onResetWidth?.();
      } else if (e.key === "End") {
        e.preventDefault();
        this.callbacks.onResizeCommit?.(this.maxWidthPx);
      }
    });
  }

  updateFeedStatus(status: ClosuresStatusResponse | null): void {
    const summary = summarizeFeedStatus(status);
    this.feedsPillEl.textContent = summary.label;
    this.feedsPillEl.classList.toggle(
      "tf-pill-warn-text",
      summary.kind === "degraded" || summary.kind === "stale"
    );

    if (!status || status.feeds.length === 0) {
      this.feedsListEl.innerHTML = `<div class="tf-inspector-muted">${escapeHtml(summary.tooltip)}</div>`;
      return;
    }

    const bannerHtml =
      summary.kind === "degraded" || summary.kind === "stale"
        ? `<div class="tf-feed-warn-banner">⚠ ${escapeHtml(summary.tooltip)}</div>`
        : "";

    this.feedsListEl.innerHTML = `
      ${bannerHtml}
      <div class="tf-feed-rows">
        ${status.feeds
          .map((feed) => {
            const name = feed.display_name ?? feed.feed_source;
            const dotCls =
              feed.is_stale ||
              feed.status === "degraded" ||
              feed.status === "failed"
                ? "degraded"
                : feed.status === "ok"
                  ? "online"
                  : "connecting";
            const updated = formatRelativeTimestamp(feed.last_success_at);
            return `
              <div class="tf-feed-row" title="${escapeHtml(feed.last_error ?? feed.source_url ?? name)}">
                <div class="tf-feed-row-head">
                  <span class="tf-feed-name"><span class="tf-status-dot ${dotCls}"></span>${escapeHtml(name)}</span>
                  <span class="mono tf-feed-count">${feed.record_count ?? 0}</span>
                </div>
                <div class="tf-feed-row-sub">
                  <span>${escapeHtml(feed.state ?? "AU")} · ${escapeHtml(feed.status)}${feed.is_stale ? " (STALE)" : ""}</span>
                  <span class="mono">${escapeHtml(updated)}</span>
                </div>
              </div>
            `;
          })
          .join("")}
      </div>
    `;
  }

  setSnowLineBands(bands: readonly number[], currentElevationM: number): void {
    if (bands.length === 0) return;
    this.snowBands = [...bands].sort((a, b) => a - b);
    const snapped = snapSnowlineBand(currentElevationM, this.snowBands);
    const idx = Math.max(0, this.snowBands.indexOf(snapped));
    this.snowSliderEl.min = "0";
    this.snowSliderEl.max = String(this.snowBands.length - 1);
    this.snowSliderEl.value = String(idx);
    this.snowValEl.textContent = `≥ ${snapped.toLocaleString()} m`;
    this.renderSnowTicks();
  }

  updateTileMode(mode: TileMode, detail?: string): void {
    const pillLabels: Record<TileMode, string> = {
      loading: "Loading…",
      pmtiles: "PMTiles Live",
      "geojson-fallback": "GeoJSON Mode",
      offline: "Offline Shell",
    };
    this.tileModePillEl.textContent = pillLabels[mode];
    this.sysTilesEl.textContent = detail ?? pillLabels[mode];
  }

  updatePreset(preset: RegionPreset): void {
    const [w, s, e, n] = preset.bbox;
    this.regionNameEl.textContent = preset.label;
    this.regionTagEl.textContent = preset.stateTag;
    this.regionDescEl.textContent = preset.description;
    this.regionBboxEl.textContent = `${w.toFixed(2)}E..${e.toFixed(2)}E, ${s.toFixed(2)}..${n.toFixed(2)}`;
    this.regionPackEl.textContent = preset.estimatedPackHint
      ? `${preset.estimatedPackHint} (Offline Pack)`
      : "Corridor view";
  }

  updateHealth(state: HealthState): void {
    if (state.kind === "connecting") {
      this.sysApiEl.textContent = "Checking…";
      return;
    }
    if (state.data) {
      this.sysApiEl.textContent =
        state.kind === "online"
          ? `${state.data.status.toUpperCase()} (${state.data.service ?? "trailforge"})`
          : "DEGRADED (DB offline)";
      this.sysPostgisEl.textContent = state.data.postgis_version ?? "—";
      this.sysSchemaEl.textContent = state.data.schema_revision ?? "—";
      return;
    }
    this.sysApiEl.textContent = "Offline (standalone shell)";
    this.sysPostgisEl.textContent = "—";
    this.sysSchemaEl.textContent = "—";
  }

  updateNetworkStatus(online: boolean): void {
    this.sysNetworkEl.textContent = online ? "Online" : "Offline";
    this.sysNetworkEl.classList.toggle("tf-accent-emerald", online);
    this.sysNetworkEl.classList.toggle("tf-accent-amber", !online);
  }

  updateSwStatus(status: SwStatus): void {
    const labels: Record<SwStatus, string> = {
      unsupported: "Not supported",
      registering: "Registering…",
      ready: "Shell cached",
      error: "Unavailable",
    };
    this.sysSwEl.textContent = labels[status];
  }

  private renderSnowTicks(): void {
    const first = this.snowBands[0] ?? 800;
    const mid = this.snowBands[Math.floor(this.snowBands.length / 2)] ?? 1100;
    const last = this.snowBands[this.snowBands.length - 1] ?? 1600;
    this.snowTicksEl.innerHTML = `<span>${first}m</span><span>${mid}m</span><span>${last}m</span>`;
  }

  private q<T extends HTMLElement = HTMLElement>(selector: string): T {
    const el = this.root.querySelector<T>(selector);
    if (!el) throw new Error(`Sidebar element not found: ${selector}`);
    return el;
  }
}
