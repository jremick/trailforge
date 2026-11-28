import {
  formatRelativeTimestamp,
  summarizeFeedStatus,
} from "../closuresHelpers";
import { uiIcon } from "../icons";
import {
  CANONICAL_LAYER_ORDER,
  LAYER_DEFINITION_MAP,
  groupOrderedLayerKeys,
  moveLayerInOrder,
  normalizeLayerOrder,
  reorderLayerKeys,
} from "../layerPrefsHelpers";
import {
  DEFAULT_SNOWLINE_BANDS_M,
  DIFFICULTY_META,
  OUTDOOR_POI_ICONS,
  REGION_PRESETS,
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
  onLayerOrderChange: (order: LayerKey[]) => void;
  onResetLayerPreferences?: () => void;
  onSnowLineElevationChange: (elevationM: number) => void;
  onPresetChange?: (presetId: string) => void;
  onResetRegion: () => void;
  onOpenOfflinePacks?: () => void;
  onCollapse?: () => void;
  onResizePreview?: (widthPx: number) => void;
  onResizeCommit?: (widthPx: number) => void;
  onResizeStateChange?: (resizing: boolean) => void;
  onResetWidth?: () => void;
}

export interface SidebarOptions {
  initialOrder?: readonly LayerKey[];
  hasCustomLayerPrefs?: boolean;
  showTelemetrySection?: boolean;
}

export class Sidebar {
  private readonly root: HTMLElement;
  private readonly callbacks: SidebarCallbacks;
  private readonly layerListEl: HTMLElement;
  private readonly reorderToggleBtn: HTMLButtonElement;
  private readonly layerPrefsBarEl: HTMLElement;
  private readonly tileModePillEl: HTMLElement;
  private readonly feedsSectionEl: HTMLDetailsElement;
  private readonly feedsPillEl: HTMLElement;
  private readonly feedsListEl: HTMLElement;
  private readonly presetSelectEl: HTMLSelectElement;
  private readonly regionNameEl: HTMLElement;
  private readonly regionTagEl: HTMLElement;
  private readonly regionDescEl: HTMLElement;
  private readonly regionBboxEl: HTMLElement;
  private readonly regionPackEl: HTMLElement;
  private readonly telemetrySectionEl: HTMLDetailsElement;
  private readonly sysTilesEl: HTMLElement;
  private readonly sysApiEl: HTMLElement;
  private readonly sysPostgisEl: HTMLElement;
  private readonly sysSchemaEl: HTMLElement;
  private readonly sysNetworkEl: HTMLElement;
  private readonly sysSwEl: HTMLElement;
  private readonly collapseBtnEl: HTMLButtonElement;
  private readonly resizerEl: HTMLElement;

  private layerOrder: LayerKey[];
  private layerState: LayerVisibilityState;
  private hasCustomLayerPrefs: boolean;
  private reorderMode = false;
  private showAllPoiLegend = false;
  private draggingKey: LayerKey | null = null;
  private snowBands: number[] = [...DEFAULT_SNOWLINE_BANDS_M];
  private currentWidthPx: number = SIDEBAR_DEFAULT_WIDTH_PX;
  private maxWidthPx: number = SIDEBAR_MAX_WIDTH_PX;

  constructor(
    container: HTMLElement,
    initialLayers: LayerVisibilityState,
    initialPreset: RegionPreset,
    callbacks: SidebarCallbacks,
    options?: SidebarOptions
  ) {
    this.root = container;
    this.callbacks = callbacks;
    this.layerOrder = normalizeLayerOrder(
      options?.initialOrder ?? CANONICAL_LAYER_ORDER
    );
    this.layerState = { ...initialLayers };
    this.hasCustomLayerPrefs = Boolean(options?.hasCustomLayerPrefs);
    const showTelemetry = Boolean(options?.showTelemetrySection);

    this.root.className = "tf-sidebar";
    this.root.setAttribute("aria-label", "Map layers and trail controls");

    const iconicRegions = REGION_PRESETS.filter(
      (p) => p.group === "region" && p.packCategory !== "state"
    );
    const stateRegions = REGION_PRESETS.filter(
      (p) => p.group === "region" && p.packCategory === "state"
    );
    const landmarks = REGION_PRESETS.filter((p) => p.group === "landmark");

    const renderPresetOptions = (items: RegionPreset[]): string =>
      items
        .map(
          (p) =>
            `<option value="${escapeHtml(p.id)}" ${p.id === initialPreset.id ? "selected" : ""}>${escapeHtml(p.label)}</option>`
        )
        .join("");

    this.root.innerHTML = `
      <div class="tf-sidebar-head">
        <div class="tf-sidebar-head-title">
          ${uiIcon("layers", 15, "tf-sidebar-head-icon")}
          <span>Layers &amp; controls</span>
        </div>
        <button
          type="button"
          class="tf-sidebar-collapse-btn"
          data-role="sidebar-collapse-btn"
          title="Hide sidebar (shortcut: [)"
          aria-label="Hide layers sidebar"
          aria-expanded="true"
        >
          ${uiIcon("chevronLeft", 14)}
          <span>Hide</span>
        </button>
      </div>

      <div class="tf-sidebar-scroll">
        <details class="tf-section" open>
          <summary class="tf-section-summary">
            <span class="tf-section-title-group">
              <span class="tf-section-title">Map layers</span>
            </span>
            <span class="tf-section-meta" data-role="tile-mode-pill">Loading…</span>
          </summary>

          <div class="tf-section-body">
            <div class="tf-layer-toolbar">
              <span class="tf-layer-toolbar-caption">Grouped by category</span>
              <button
                type="button"
                class="tf-text-action-btn"
                data-role="toggle-layer-reorder"
                aria-pressed="false"
                title="Customize list order with drag-and-drop or arrows"
              >
                ${uiIcon("sliders", 13)}
                <span>Reorder</span>
              </button>
            </div>

            <div class="tf-layer-list" data-role="layer-list"></div>

            <div class="tf-layer-prefs-bar ${this.hasCustomLayerPrefs ? "" : "hidden"}" data-role="layer-prefs-bar">
              <span class="tf-layer-prefs-note">Custom layer preferences active</span>
              <button
                type="button"
                class="tf-mini-btn"
                data-role="reset-layer-prefs"
                title="Reset map layer order and on/off toggles to defaults"
              >Reset defaults</button>
            </div>

            <div class="tf-poi-legend-block" data-role="poi-legend-block"></div>
          </div>
        </details>

        <details class="tf-section" open>
          <summary class="tf-section-summary">
            <span class="tf-section-title-group">
              <span class="tf-section-title">Active corridor</span>
            </span>
            <span class="tf-section-meta" data-role="region-tag">${escapeHtml(initialPreset.stateTag)}</span>
          </summary>

          <div class="tf-section-body">
            <label class="tf-field">
              <span class="tf-label">Jump to region or state</span>
              <select class="tf-select" data-role="sidebar-preset-select" aria-label="Jump to Australian region or corridor">
                <optgroup label="Iconic 4WD Regions (${iconicRegions.length})">
                  ${renderPresetOptions(iconicRegions)}
                </optgroup>
                <optgroup label="Australian States &amp; Territories (${stateRegions.length})">
                  ${renderPresetOptions(stateRegions)}
                </optgroup>
                <optgroup label="4WD Corridors &amp; Landmarks">
                  ${renderPresetOptions(landmarks)}
                </optgroup>
              </select>
            </label>

            <div class="tf-region-title" data-role="region-name">${escapeHtml(initialPreset.label)}</div>
            <p class="tf-region-desc" data-role="region-desc">${escapeHtml(initialPreset.description)}</p>
            <div class="tf-kv-grid">
              <span class="tf-kv-key">Bounds</span>
              <span class="tf-kv-val mono" data-role="region-bbox"></span>
              <span class="tf-kv-key">Offline pack</span>
              <span class="tf-kv-val" data-role="region-pack"></span>
            </div>
            <div class="tf-btn-row">
              <button type="button" class="tf-btn-secondary" data-role="reset-region">
                ${uiIcon("target", 13)}
                <span>Recenter</span>
              </button>
              <button type="button" class="tf-btn-secondary" data-role="open-packs">
                ${uiIcon("download", 13)}
                <span>Offline packs</span>
              </button>
            </div>
          </div>
        </details>

        <details class="tf-section" data-role="feeds-section">
          <summary class="tf-section-summary">
            <span class="tf-section-title-group">
              <span class="tf-section-title">Agency closure feeds</span>
            </span>
            <span class="tf-section-meta" data-role="feeds-pill">Checking…</span>
          </summary>

          <div class="tf-section-body" data-role="feeds-list">
            <div class="tf-inspector-muted">Loading official agency closure feeds…</div>
          </div>
        </details>

        <details class="tf-section ${showTelemetry ? "" : "hidden"}" data-role="telemetry-section">
          <summary class="tf-section-summary">
            <span class="tf-section-title-group">
              <span class="tf-section-title">Stack &amp; offline status</span>
            </span>
            <span class="tf-section-meta">Diagnostics</span>
          </summary>

          <div class="tf-section-body">
            <div class="tf-kv-grid">
              <span class="tf-kv-key">Vector/DEM</span>
              <span class="tf-kv-val mono" data-role="sys-tiles">Checking…</span>
              <span class="tf-kv-key">API health</span>
              <span class="tf-kv-val mono" data-role="sys-api">Connecting…</span>
              <span class="tf-kv-key">PostGIS</span>
              <span class="tf-kv-val mono" data-role="sys-postgis">-</span>
              <span class="tf-kv-key">Schema rev</span>
              <span class="tf-kv-val mono" data-role="sys-schema">-</span>
              <span class="tf-kv-key">Network</span>
              <span class="tf-kv-val" data-role="sys-network">Online</span>
              <span class="tf-kv-key">PWA cache</span>
              <span class="tf-kv-val" data-role="sys-sw">Registering…</span>
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

    this.layerListEl = this.q("[data-role='layer-list']");
    this.reorderToggleBtn = this.q<HTMLButtonElement>(
      "[data-role='toggle-layer-reorder']"
    );
    this.layerPrefsBarEl = this.q("[data-role='layer-prefs-bar']");
    this.tileModePillEl = this.q("[data-role='tile-mode-pill']");
    this.feedsSectionEl = this.q<HTMLDetailsElement>("[data-role='feeds-section']");
    this.feedsPillEl = this.q("[data-role='feeds-pill']");
    this.feedsListEl = this.q("[data-role='feeds-list']");
    this.presetSelectEl = this.q<HTMLSelectElement>(
      "[data-role='sidebar-preset-select']"
    );
    this.regionNameEl = this.q("[data-role='region-name']");
    this.regionTagEl = this.q("[data-role='region-tag']");
    this.regionDescEl = this.q("[data-role='region-desc']");
    this.regionBboxEl = this.q("[data-role='region-bbox']");
    this.regionPackEl = this.q("[data-role='region-pack']");
    this.telemetrySectionEl = this.q<HTMLDetailsElement>(
      "[data-role='telemetry-section']"
    );
    this.sysTilesEl = this.q("[data-role='sys-tiles']");
    this.sysApiEl = this.q("[data-role='sys-api']");
    this.sysPostgisEl = this.q("[data-role='sys-postgis']");
    this.sysSchemaEl = this.q("[data-role='sys-schema']");
    this.sysNetworkEl = this.q("[data-role='sys-network']");
    this.sysSwEl = this.q("[data-role='sys-sw']");
    this.collapseBtnEl = this.q<HTMLButtonElement>("[data-role='sidebar-collapse-btn']");
    this.resizerEl = this.q("[data-role='sidebar-resizer']");

    this.renderLayerList();
    this.renderPoiLegend();
    this.updatePreset(initialPreset);

    this.collapseBtnEl.addEventListener("click", () => {
      callbacks.onCollapse?.();
    });

    this.reorderToggleBtn.addEventListener("click", () => {
      this.reorderMode = !this.reorderMode;
      this.syncReorderToggleUi();
      this.renderLayerList();
    });

    this.bindResizerEvents();

    this.root
      .querySelector<HTMLButtonElement>("[data-role='reset-layer-prefs']")
      ?.addEventListener("click", () => {
        callbacks.onResetLayerPreferences?.();
      });

    this.presetSelectEl.addEventListener("change", () => {
      callbacks.onPresetChange?.(this.presetSelectEl.value);
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

  private syncReorderToggleUi(): void {
    const caption = this.root.querySelector<HTMLElement>(
      ".tf-layer-toolbar-caption"
    );
    if (caption) {
      caption.textContent = this.reorderMode
        ? "Drag rows or use arrows to reorder"
        : "Grouped by category";
    }
    this.reorderToggleBtn.setAttribute("aria-pressed", String(this.reorderMode));
    this.reorderToggleBtn.classList.toggle("active", this.reorderMode);
    this.reorderToggleBtn.innerHTML = this.reorderMode
      ? `${uiIcon("check", 13)}<span>Done</span>`
      : `${uiIcon("sliders", 13)}<span>Reorder</span>`;
  }

  /**
   * Synchronize sidebar layer rows, checkboxes, conditional controls, and custom-prefs bar
   * after async config/preferences load, login/logout, admin change, or reset.
   */
  syncLayerPreferences(
    order: readonly LayerKey[],
    layers: LayerVisibilityState,
    hasCustomPrefs: boolean
  ): void {
    this.layerOrder = normalizeLayerOrder(order);
    this.layerState = { ...layers };
    this.hasCustomLayerPrefs = hasCustomPrefs;
    this.renderLayerList();
    this.layerPrefsBarEl.classList.toggle("hidden", !hasCustomPrefs);
  }

  setTelemetrySectionVisible(visible: boolean): void {
    this.telemetrySectionEl.classList.toggle("hidden", !visible);
    if (!visible) {
      this.telemetrySectionEl.open = false;
    }
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

  private renderInlineLayerAccessory(key: LayerKey): string {
    if (key === "tracks" && this.layerState.tracks) {
      return `
        <div class="tf-inline-grade-legend" data-role="difficulty-legend">
          ${DIFFICULTY_META.map(
            (d) => `
              <span class="tf-inline-grade-item" title="${escapeHtml(d.hint)}">
                <span class="tf-diff-swatch" style="background:${d.color}"></span>
                <span>${escapeHtml(d.label)}</span>
              </span>
            `
          ).join("")}
        </div>
      `;
    }

    if (key === "snowLine" && this.layerState.snowLine) {
      const snapped = snapSnowlineBand(
        this.layerState.snowLineElevationM,
        this.snowBands
      );
      const idx = Math.max(0, this.snowBands.indexOf(snapped));
      const first = this.snowBands[0] ?? 800;
      const mid = this.snowBands[Math.floor(this.snowBands.length / 2)] ?? 1100;
      const last = this.snowBands[this.snowBands.length - 1] ?? 1600;
      return `
        <div class="tf-snow-control" data-role="snow-control">
          <div class="tf-snow-head">
            <span>Elevation threshold</span>
            <span class="tf-snow-val" data-role="snow-val">&ge; ${snapped.toLocaleString()} m</span>
          </div>
          <input
            type="range"
            class="tf-range"
            data-role="snow-slider"
            min="0"
            max="${this.snowBands.length - 1}"
            step="1"
            value="${idx}"
            aria-label="Snow-line elevation threshold in meters"
          />
          <div class="tf-snow-ticks" data-role="snow-ticks">
            <span>${first} m</span><span>${mid} m</span><span>${last} m</span>
          </div>
        </div>
      `;
    }

    return "";
  }

  private renderLayerList(): void {
    if (this.reorderMode) {
      const total = this.layerOrder.length;
      this.layerListEl.innerHTML = this.layerOrder
        .map((key, idx) => {
          const item = LAYER_DEFINITION_MAP[key];
          const checked = Boolean(this.layerState[key]);
          return `
            <div
              class="tf-toggle-row tf-layer-row is-reordering"
              data-layer-row="${escapeHtml(key)}"
              draggable="true"
            >
              <span
                class="tf-layer-drag-handle"
                title="Drag to reorder layer"
                aria-hidden="true"
              >⋮⋮</span>
              <label class="tf-layer-toggle-main">
                <input
                  type="checkbox"
                  class="tf-checkbox"
                  data-layer-key="${escapeHtml(key)}"
                  ${checked ? "checked" : ""}
                />
                <span class="tf-toggle-copy">
                  <span class="tf-toggle-label">${escapeHtml(item.label)}</span>
                </span>
              </label>
              <div class="tf-layer-move-controls always-visible">
                <button
                  type="button"
                  class="tf-layer-move-btn"
                  data-move-layer="${escapeHtml(key)}"
                  data-move-dir="up"
                  title="Move ${escapeHtml(item.shortLabel)} up"
                  aria-label="Move ${escapeHtml(item.shortLabel)} up"
                  ${idx === 0 ? "disabled" : ""}
                >↑</button>
                <button
                  type="button"
                  class="tf-layer-move-btn"
                  data-move-layer="${escapeHtml(key)}"
                  data-move-dir="down"
                  title="Move ${escapeHtml(item.shortLabel)} down"
                  aria-label="Move ${escapeHtml(item.shortLabel)} down"
                  ${idx === total - 1 ? "disabled" : ""}
                >↓</button>
              </div>
            </div>
          `;
        })
        .join("");
    } else {
      const groups = groupOrderedLayerKeys(this.layerOrder);
      this.layerListEl.innerHTML = groups
        .map(
          (group) => `
            <div class="tf-layer-group" data-layer-group="${escapeHtml(group.id)}">
              <div class="tf-layer-group-title">${escapeHtml(group.label)}</div>
              <div class="tf-layer-group-rows">
                ${group.keys
                  .map((key) => {
                    const item = LAYER_DEFINITION_MAP[key];
                    const checked = Boolean(this.layerState[key]);
                    const accessory = this.renderInlineLayerAccessory(key);
                    return `
                      <div class="tf-layer-item-wrap">
                        <label class="tf-layer-clean-row" title="${escapeHtml(item.hint)}">
                          <input
                            type="checkbox"
                            class="tf-checkbox"
                            data-layer-key="${escapeHtml(key)}"
                            ${checked ? "checked" : ""}
                          />
                          <span class="tf-toggle-copy">
                            <span class="tf-toggle-label">${escapeHtml(item.label)}</span>
                            <span class="tf-toggle-hint">${escapeHtml(item.hint)}</span>
                          </span>
                        </label>
                        ${accessory}
                      </div>
                    `;
                  })
                  .join("")}
              </div>
            </div>
          `
        )
        .join("");
    }

    this.bindLayerListEvents();
  }

  private renderPoiLegend(): void {
    const mount = this.root.querySelector<HTMLElement>("[data-role='poi-legend-block']");
    if (!mount) return;
    const shown = this.showAllPoiLegend
      ? OUTDOOR_POI_ICONS
      : OUTDOOR_POI_ICONS.slice(0, 6);

    mount.innerHTML = `
      <div class="tf-poi-legend-head">
        <span class="tf-layer-group-title">Outdoor POI symbols (${OUTDOOR_POI_ICONS.length})</span>
        <button type="button" class="tf-text-action-btn" data-role="toggle-poi-legend">
          ${this.showAllPoiLegend ? "Show fewer" : "Show all"}
        </button>
      </div>
      <div class="tf-icon-grid">
        ${shown
          .map(
            (icon) => `
              <span class="tf-icon-legend-item" title="${escapeHtml(icon.label)}">
                <img src="${escapeHtml(icon.iconPath)}" alt="" width="16" height="16" loading="lazy" />
                <span>${escapeHtml(icon.label)}</span>
              </span>
            `
          )
          .join("")}
      </div>
    `;

    mount
      .querySelector<HTMLButtonElement>("[data-role='toggle-poi-legend']")
      ?.addEventListener("click", () => {
        this.showAllPoiLegend = !this.showAllPoiLegend;
        this.renderPoiLegend();
      });
  }

  private bindLayerListEvents(): void {
    this.layerListEl
      .querySelectorAll<HTMLInputElement>("[data-layer-key]")
      .forEach((input) => {
        input.addEventListener("change", () => {
          const key = input.dataset.layerKey as LayerKey;
          this.layerState = { ...this.layerState, [key]: input.checked };
          if (key === "tracks" || key === "snowLine") {
            this.renderLayerList();
          }
          this.callbacks.onLayerToggle(key, input.checked);
        });
      });

    const snowSlider = this.layerListEl.querySelector<HTMLInputElement>(
      "[data-role='snow-slider']"
    );
    const snowVal = this.layerListEl.querySelector<HTMLElement>(
      "[data-role='snow-val']"
    );
    snowSlider?.addEventListener("input", () => {
      const idx = Number(snowSlider.value);
      const elev = this.snowBands[idx] ?? 1000;
      if (snowVal) snowVal.textContent = `≥ ${elev.toLocaleString()} m`;
      this.layerState = { ...this.layerState, snowLineElevationM: elev };
      this.callbacks.onSnowLineElevationChange(elev);
    });

    if (!this.reorderMode) return;

    this.layerListEl
      .querySelectorAll<HTMLButtonElement>("[data-move-layer]")
      .forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          const key = btn.dataset.moveLayer as LayerKey | undefined;
          const dir = btn.dataset.moveDir as "up" | "down" | undefined;
          if (!key || (dir !== "up" && dir !== "down")) return;
          const next = moveLayerInOrder(this.layerOrder, key, dir);
          this.layerOrder = next;
          this.renderLayerList();
          this.callbacks.onLayerOrderChange(next);
        });
      });

    this.layerListEl
      .querySelectorAll<HTMLElement>("[data-layer-row]")
      .forEach((row) => {
        const key = row.dataset.layerRow as LayerKey | undefined;
        if (!key) return;

        row.addEventListener("dragstart", (e) => {
          this.draggingKey = key;
          row.classList.add("dragging");
          if (e.dataTransfer) {
            e.dataTransfer.effectAllowed = "move";
            e.dataTransfer.setData("text/plain", key);
          }
        });

        row.addEventListener("dragover", (e) => {
          if (!this.draggingKey || this.draggingKey === key) return;
          e.preventDefault();
          if (e.dataTransfer) {
            e.dataTransfer.dropEffect = "move";
          }
          row.classList.add("drag-over");
        });

        row.addEventListener("dragleave", () => {
          row.classList.remove("drag-over");
        });

        row.addEventListener("drop", (e) => {
          e.preventDefault();
          row.classList.remove("drag-over");
          const src = this.draggingKey;
          this.draggingKey = null;
          if (!src || src === key) return;
          const next = reorderLayerKeys(this.layerOrder, src, key);
          this.layerOrder = next;
          this.renderLayerList();
          this.callbacks.onLayerOrderChange(next);
        });

        row.addEventListener("dragend", () => {
          this.draggingKey = null;
          row.classList.remove("dragging");
          this.layerListEl
            .querySelectorAll(".drag-over")
            .forEach((el) => el.classList.remove("drag-over"));
        });
      });
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
        ? `<div class="tf-feed-warn-banner">${uiIcon("alert", 13)}<span>${escapeHtml(summary.tooltip)}</span></div>`
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
                  <span class="tf-feed-count">${feed.record_count ?? 0}</span>
                </div>
                <div class="tf-feed-row-sub">
                  <span>${escapeHtml(feed.state ?? "AU")} · ${escapeHtml(feed.status)}${feed.is_stale ? " (stale)" : ""}</span>
                  <span>${escapeHtml(updated)}</span>
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
    this.layerState = { ...this.layerState, snowLineElevationM: snapped };
    if (this.layerState.snowLine && !this.reorderMode) {
      this.renderLayerList();
    }
  }

  updateTileMode(mode: TileMode, detail?: string): void {
    const pillLabels: Record<TileMode, string> = {
      loading: "Loading…",
      pmtiles: "PMTiles live",
      "geojson-fallback": "GeoJSON mode",
      offline: "Offline shell",
    };
    this.tileModePillEl.textContent = pillLabels[mode];
    this.sysTilesEl.textContent = detail ?? pillLabels[mode];
  }

  updatePreset(preset: RegionPreset): void {
    const [w, s, e, n] = preset.bbox;
    if (this.presetSelectEl.value !== preset.id) {
      this.presetSelectEl.value = preset.id;
    }
    this.regionNameEl.textContent = preset.label;
    this.regionTagEl.textContent = preset.stateTag;
    this.regionDescEl.textContent = preset.description;
    this.regionBboxEl.textContent = `${w.toFixed(2)}E..${e.toFixed(2)}E, ${s.toFixed(2)}..${n.toFixed(2)}`;
    this.regionPackEl.textContent = preset.estimatedPackHint
      ? `${preset.estimatedPackHint} offline pack`
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
      this.sysPostgisEl.textContent = state.data.postgis_version ?? "-";
      this.sysSchemaEl.textContent = state.data.schema_revision ?? "-";
      return;
    }
    this.sysApiEl.textContent = "Offline (standalone shell)";
    this.sysPostgisEl.textContent = "-";
    this.sysSchemaEl.textContent = "-";
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

  private q<T extends HTMLElement = HTMLElement>(selector: string): T {
    const el = this.root.querySelector<T>(selector);
    if (!el) throw new Error(`Sidebar element not found: ${selector}`);
    return el;
  }
}
