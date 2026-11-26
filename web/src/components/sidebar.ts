import {
  DIFFICULTY_META,
  OUTDOOR_POI_ICONS,
  escapeHtml,
} from "../presets";
import type { SwStatus } from "../pwa";
import type {
  HealthState,
  LayerKey,
  LayerVisibilityState,
  RegionPreset,
} from "../types";

export interface SidebarCallbacks {
  onLayerToggle: (key: LayerKey, enabled: boolean) => void;
  onSnowLineElevationChange: (elevationM: number) => void;
  onResetRegion: () => void;
}

interface LayerToggleDef {
  key: LayerKey;
  label: string;
  hint: string;
  tag: string;
}

const LAYER_TOGGLES: readonly LayerToggleDef[] = [
  {
    key: "tracks",
    label: "4WD Tracks (Graded)",
    hint: "Canonical 50–100 m segments colored by Easy → Extreme difficulty",
    tag: "Slice 03",
  },
  {
    key: "steepHighlights",
    label: "Steep Climbs (≥14°)",
    hint: "Highlighted sustained & max gradient hazard sections",
    tag: "Slice 03",
  },
  {
    key: "contoursHillshade",
    label: "Contours & Hillshade",
    hint: "GA 5 m LiDAR / DEA SRTM terrain relief & index contours",
    tag: "Slice 03",
  },
  {
    key: "snowLine",
    label: "Alpine Snow-Line Band",
    hint: "Configurable elevation highlight (>1,000 m default)",
    tag: "Slice 03",
  },
  {
    key: "pois",
    label: "Outdoor POIs",
    hint: "Campgrounds, alpine huts, trigs, water bores & fuel stops",
    tag: "Slice 03",
  },
];

export class Sidebar {
  private readonly root: HTMLElement;
  private readonly snowSliderRowEl: HTMLElement;
  private readonly snowValEl: HTMLElement;
  private readonly regionNameEl: HTMLElement;
  private readonly regionTagEl: HTMLElement;
  private readonly regionDescEl: HTMLElement;
  private readonly regionBboxEl: HTMLElement;
  private readonly regionPackEl: HTMLElement;
  private readonly sysApiEl: HTMLElement;
  private readonly sysPostgisEl: HTMLElement;
  private readonly sysSchemaEl: HTMLElement;
  private readonly sysNetworkEl: HTMLElement;
  private readonly sysSwEl: HTMLElement;

  constructor(
    container: HTMLElement,
    initialLayers: LayerVisibilityState,
    initialPreset: RegionPreset,
    callbacks: SidebarCallbacks
  ) {
    this.root = container;
    this.root.className = "tf-sidebar";
    this.root.setAttribute("aria-label", "Map layers and trail controls");

    const previewIcons = OUTDOOR_POI_ICONS.slice(0, 7);

    this.root.innerHTML = `
      <div class="tf-sidebar-scroll">
        <details class="tf-section" open>
          <summary class="tf-section-summary">
            <span class="tf-section-title-group">
              <span class="tf-section-index">01</span>
              <span class="tf-section-title">Map Layers</span>
            </span>
            <span class="tf-section-pill">PMTiles Ready</span>
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
                      <span class="tf-toggle-head">
                        <span class="tf-toggle-label">${escapeHtml(item.label)}</span>
                        <span class="tf-ready-tag">${escapeHtml(item.tag)}</span>
                      </span>
                      <span class="tf-toggle-hint">${escapeHtml(item.hint)}</span>
                    </span>
                  </label>
                `
              ).join("")}
            </div>

            <div class="tf-snow-control ${initialLayers.snowLine ? "" : "dimmed"}" data-role="snow-control">
              <div class="tf-snow-head">
                <span>Snow-Line Threshold</span>
                <span class="mono tf-accent-cyan" data-role="snow-val">&gt; ${initialLayers.snowLineElevationM.toLocaleString()} m</span>
              </div>
              <input
                type="range"
                class="tf-range"
                data-role="snow-slider"
                min="800"
                max="1800"
                step="50"
                value="${initialLayers.snowLineElevationM}"
                aria-label="Snow-line elevation threshold in meters"
              />
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

        <details class="tf-section" open>
          <summary class="tf-section-summary">
            <span class="tf-section-title-group">
              <span class="tf-section-index">02</span>
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
            <button type="button" class="tf-btn-secondary" data-role="reset-region">
              ⌖ Recenter on Corridor
            </button>
          </div>
        </details>

        <details class="tf-section" open>
          <summary class="tf-section-summary">
            <span class="tf-section-title-group">
              <span class="tf-section-index">03</span>
              <span class="tf-section-title">Stack &amp; Offline Shell</span>
            </span>
            <span class="tf-section-pill">Slice 01</span>
          </summary>

          <div class="tf-section-body">
            <div class="tf-kv-grid">
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
    `;

    this.snowSliderRowEl = this.q("[data-role='snow-control']");
    this.snowValEl = this.q("[data-role='snow-val']");
    this.regionNameEl = this.q("[data-role='region-name']");
    this.regionTagEl = this.q("[data-role='region-tag']");
    this.regionDescEl = this.q("[data-role='region-desc']");
    this.regionBboxEl = this.q("[data-role='region-bbox']");
    this.regionPackEl = this.q("[data-role='region-pack']");
    this.sysApiEl = this.q("[data-role='sys-api']");
    this.sysPostgisEl = this.q("[data-role='sys-postgis']");
    this.sysSchemaEl = this.q("[data-role='sys-schema']");
    this.sysNetworkEl = this.q("[data-role='sys-network']");
    this.sysSwEl = this.q("[data-role='sys-sw']");

    this.updatePreset(initialPreset);

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

    const slider = this.q<HTMLInputElement>("[data-role='snow-slider']");
    slider.addEventListener("input", () => {
      const elev = Number(slider.value);
      this.snowValEl.textContent = `> ${elev.toLocaleString()} m`;
      callbacks.onSnowLineElevationChange(elev);
    });

    this.q("[data-role='reset-region']").addEventListener("click", () => {
      callbacks.onResetRegion();
    });
  }

  updatePreset(preset: RegionPreset): void {
    const [w, s, e, n] = preset.bbox;
    this.regionNameEl.textContent = preset.label;
    this.regionTagEl.textContent = preset.stateTag;
    this.regionDescEl.textContent = preset.description;
    this.regionBboxEl.textContent = `${w.toFixed(2)}E..${e.toFixed(2)}E, ${s.toFixed(2)}..${n.toFixed(2)}`;
    this.regionPackEl.textContent = preset.estimatedPackHint
      ? `${preset.estimatedPackHint} (Slice 09)`
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

  private q<T extends HTMLElement = HTMLElement>(selector: string): T {
    const el = this.root.querySelector<T>(selector);
    if (!el) throw new Error(`Sidebar element not found: ${selector}`);
    return el;
  }
}
