import { resolvePackBundleUrl } from "../adminHelpers";
import {
  formatByteSize,
  type PackDownloadProgress,
} from "../offline/packStore";
import {
  PAN_AU_PACK_PRESETS,
  type PackFilterCategory,
  classifyPackCategory,
  escapeHtml,
  filterPackRegions,
} from "../presets";
import type {
  DownloadedPackRecord,
  OfflinePackRegionItem,
  SyncQueueItem,
} from "../types";

export interface PacksPanelState {
  online: boolean;
  isAdmin: boolean;
  loading: boolean;
  regions: OfflinePackRegionItem[];
  downloaded: DownloadedPackRecord[];
  activeOfflinePackId: string | null;
  syncQueue: SyncQueueItem[];
  syncing: boolean;
  buildingRegionId: string | null;
  downloadingRegionId: string | null;
  downloadProgress: PackDownloadProgress | null;
  error: string | null;
}

export interface PacksPanelCallbacks {
  onClose: () => void;
  onRefresh: () => Promise<void>;
  onBuildPack: (regionId: string) => Promise<void>;
  onDownloadPack: (region: OfflinePackRegionItem) => Promise<void>;
  onUseOfflinePack: (regionId: string | null) => Promise<void>;
  onRemovePack: (regionId: string) => Promise<void>;
  onSyncNow: () => Promise<void>;
}

const DEFAULT_FALLBACK_REGIONS: readonly OfflinePackRegionItem[] =
  PAN_AU_PACK_PRESETS.map((p) => ({
    region_id: p.id,
    name: p.label,
    description: p.description,
    category: p.packCategory ?? "region",
    region_type: p.packCategory === "state" ? "state" : "tactical",
    states: p.states,
    estimated_size_hint: p.estimatedPackHint,
    bbox: p.bbox,
    status: "not_built",
  }));

export class OfflinePacksPanel {
  private readonly root: HTMLElement;
  private readonly callbacks: PacksPanelCallbacks;
  private state: PacksPanelState;
  private isOpen = false;
  private activeFilter: PackFilterCategory = "all";

  constructor(
    container: HTMLElement,
    initialState: PacksPanelState,
    callbacks: PacksPanelCallbacks
  ) {
    this.root = container;
    this.state = initialState;
    this.callbacks = callbacks;
    this.root.className = "tf-account-panel tf-packs-panel";
    this.root.setAttribute("aria-label", "Offline Regional Packs & Sync Queue");
    this.root.setAttribute("aria-hidden", "true");
    this.render();
  }

  isPanelOpen(): boolean {
    return this.isOpen;
  }

  open(): void {
    this.isOpen = true;
    this.root.setAttribute("aria-hidden", "false");
    this.render();
  }

  close(): void {
    this.isOpen = false;
    this.root.setAttribute("aria-hidden", "true");
  }

  updateState(patch: Partial<PacksPanelState>): void {
    this.state = { ...this.state, ...patch };
    this.render();
  }

  private mergedRegions(): OfflinePackRegionItem[] {
    if (this.state.regions.length > 0) return this.state.regions;
    // Merge downloaded records into fallback 4 regions when offline
    const byId = new Map(this.state.downloaded.map((d) => [d.region_id, d]));
    return DEFAULT_FALLBACK_REGIONS.map((base) => {
      const cached = byId.get(base.region_id);
      if (!cached) return base;
      return {
        ...base,
        status: "ready",
        version: cached.version,
        generated_at: cached.downloaded_at,
        total_size_bytes: cached.total_size_bytes,
        track_count: cached.track_count,
        segment_count: cached.segment_count,
        poi_count: cached.poi_count,
        attributions: cached.attributions,
      };
    });
  }

  private render(): void {
    const allRegions = this.mergedRegions();
    const regionCount = filterPackRegions(allRegions, "region").length;
    const stateCount = filterPackRegions(allRegions, "state").length;
    const regions = filterPackRegions(allRegions, this.activeFilter);
    const downloadedMap = new Map(
      this.state.downloaded.map((d) => [d.region_id, d])
    );

    const activeBanner = this.state.activeOfflinePackId
      ? `
        <div class="tf-offline-pack-active-banner">
          <div>
            <strong>Offline Pack Active:</strong>
            <span class="mono">${escapeHtml(this.state.activeOfflinePackId)}</span>
            <div class="tf-panel-hint">Map PMTiles (Range 206), tracks, search, POIs &amp; closures are serving from local browser storage.</div>
          </div>
          <button type="button" class="tf-mini-btn" data-role="exit-offline-pack">Use Live API</button>
        </div>
      `
      : "";

    const syncQueueHtml = `
      <section class="tf-sync-card">
        <div class="tf-insp-sec-head">
          <h3>Pending Offline Sync Queue</h3>
          <span class="mono tf-insp-meta">${this.state.syncQueue.length} item${this.state.syncQueue.length === 1 ? "" : "s"}</span>
        </div>
        ${
          this.state.syncQueue.length === 0
            ? `<p class="tf-inspector-muted">All recorded trips, condition reports &amp; personal pins are synced.</p>`
            : `
              <div class="tf-card-stack">
                ${this.state.syncQueue
                  .map(
                    (item) => `
                      <div class="tf-list-card">
                        <div class="tf-list-card-head">
                          <span class="tf-list-card-title">${escapeHtml(item.label)}</span>
                          <span class="tf-mini-tag amber">${escapeHtml(item.kind.toUpperCase())}</span>
                        </div>
                        <div class="tf-list-card-sub mono">${escapeHtml(item.created_at.slice(0, 19).replace("T", " "))} UTC</div>
                        ${
                          item.last_error
                            ? `<div class="tf-sync-err">⚠ ${escapeHtml(item.last_error)}</div>`
                            : ""
                        }
                      </div>
                    `
                  )
                  .join("")}
              </div>
              <button
                type="button"
                class="tf-btn-primary"
                data-role="sync-now"
                ${this.state.syncing || !this.state.online ? "disabled" : ""}
              >${
                this.state.syncing
                  ? "Syncing…"
                  : this.state.online
                    ? `↑ Sync ${this.state.syncQueue.length} Queued Item${this.state.syncQueue.length === 1 ? "" : "s"} Now`
                    : "Waiting for network to sync…"
              }</button>
            `
        }
      </section>
    `;

    const cardsHtml = regions
      .map((region) => {
        const cached = downloadedMap.get(region.region_id);
        const isBuilding = this.state.buildingRegionId === region.region_id;
        const isDownloading =
          this.state.downloadingRegionId === region.region_id;
        const isActive = this.state.activeOfflinePackId === region.region_id;
        const isReady = region.status === "ready";
        const sizeText =
          cached?.total_size_bytes || region.total_size_bytes
            ? formatByteSize(cached?.total_size_bytes ?? region.total_size_bytes)
            : (region.estimated_size_hint ?? formatByteSize(0));
        const category = classifyPackCategory(region);
        const stateLabel =
          region.states && region.states.length > 0
            ? region.states.join("/")
            : null;

        const progressHtml =
          isDownloading && this.state.downloadProgress
            ? `
              <div class="tf-pack-progress">
                <div class="tf-pack-progress-head">
                  <span>${escapeHtml(this.state.downloadProgress.stage)}</span>
                  <span class="mono">${this.state.downloadProgress.pct}%</span>
                </div>
                <div class="tf-factor-track">
                  <div class="tf-factor-fill" style="width:${this.state.downloadProgress.pct}%;background:#38BDF8"></div>
                </div>
              </div>
            `
            : "";

        const bundleUrl = resolvePackBundleUrl(region);

        const attributions =
          cached?.attributions?.length
            ? cached.attributions
            : (region.attributions ?? []);

        const canBuild = this.state.isAdmin;

        return `
          <div class="tf-pack-card ${isActive ? "active" : ""}">
            <div class="tf-pack-head">
              <div>
                <div class="tf-pack-title">${escapeHtml(region.name)}</div>
                <div class="tf-pack-id mono">${escapeHtml(region.region_id)} · ${escapeHtml(region.bbox.map((n) => n.toFixed(2)).join(", "))}</div>
              </div>
              <div class="tf-pack-badges">
                ${
                  stateLabel
                    ? `<span class="tf-mini-tag">${escapeHtml(stateLabel)}</span>`
                    : ""
                }
                <span class="tf-mini-tag ${category === "state" ? "cyan" : ""}">
                  ${category === "state" ? "STATE" : "4WD REGION"}
                </span>
                <span class="tf-mini-tag ${isReady ? "emerald" : "amber"}">
                  ${isBuilding ? "BUILDING…" : escapeHtml(region.status.replace(/_/g, " ").toUpperCase())}
                </span>
                ${
                  cached
                    ? `<span class="tf-mini-tag cyan">${cached.sha256_verified ? "✓ CACHED" : "CACHED"}</span>`
                    : ""
                }
              </div>
            </div>

            <p class="tf-pack-desc">${escapeHtml(region.description)}</p>

            <div class="tf-pack-metrics mono">
              <span>Size: ${escapeHtml(sizeText)}</span>
              <span>· Tracks: ${cached?.track_count ?? region.track_count ?? 0}</span>
              <span>· POIs: ${cached?.poi_count ?? region.poi_count ?? 0}</span>
            </div>

            ${progressHtml}

            <div class="tf-pack-actions">
              ${
                !isReady
                  ? canBuild
                    ? `<button type="button" class="tf-btn-primary" data-build-pack="${escapeHtml(region.region_id)}" ${isBuilding || !this.state.online ? "disabled" : ""}>${isBuilding ? "Building…" : "⚙ Build Pack"}</button>`
                    : `<span class="tf-pack-admin-note">Not yet built by instance admin</span>`
                  : `<button type="button" class="tf-btn-primary" data-download-pack="${escapeHtml(region.region_id)}" ${isDownloading || !this.state.online ? "disabled" : ""}>${cached ? "↻ Update Offline Copy" : "⬇ Download for Offline"}</button>`
              }
              ${
                cached
                  ? `<button type="button" class="tf-mini-btn ${isActive ? "active" : ""}" data-use-pack="${escapeHtml(region.region_id)}">${isActive ? "✓ Active Offline" : "Use Offline Pack"}</button>`
                  : ""
              }
              ${
                isReady && this.state.online
                  ? `<a class="tf-mini-btn" href="${escapeHtml(bundleUrl)}" download="${escapeHtml(region.region_id)}.tar.gz" title="Download full offline tar.gz archive">⬇ .tar.gz</a>`
                  : ""
              }
              ${
                canBuild && isReady && this.state.online
                  ? `<button type="button" class="tf-mini-btn" data-build-pack="${escapeHtml(region.region_id)}" ${isBuilding ? "disabled" : ""} title="Rebuild regional pack from PostGIS">Rebuild</button>`
                  : ""
              }
              ${
                cached
                  ? `<button type="button" class="tf-mini-btn danger" data-remove-pack="${escapeHtml(region.region_id)}">Remove</button>`
                  : ""
              }
            </div>

            ${
              attributions.length > 0
                ? `<div class="tf-pack-attribs">${escapeHtml(attributions.join(" · "))}</div>`
                : ""
            }
          </div>
        `;
      })
      .join("");

    this.root.innerHTML = `
      <div class="tf-inspector-head">
        <div>
          <div class="tf-inspector-kicker">Offline Packs &amp; Sync · ${allRegions.length} AU Packs · ${this.state.online ? "Online" : "Offline Mode"}</div>
          <h2 class="tf-inspector-title">Pan-Australia Offline Packs</h2>
        </div>
        <div class="tf-head-actions">
          <button type="button" class="tf-mini-btn" data-role="refresh-packs" ${!this.state.online || this.state.loading ? "disabled" : ""}>↻</button>
          <button type="button" class="tf-inspector-close" data-role="close" aria-label="Close Offline Packs panel">×</button>
        </div>
      </div>

      <div class="tf-inspector-body">
        ${
          this.state.error
            ? `<div class="tf-inspector-error">${escapeHtml(this.state.error)}</div>`
            : ""
        }
        ${activeBanner}
        ${syncQueueHtml}

        <section class="tf-insp-section">
          <div class="tf-insp-sec-head">
            <h3>Downloadable AU Bundles</h3>
            <span class="mono tf-insp-meta">${this.state.downloaded.length} cached · ${regions.length} shown</span>
          </div>
          <p class="tf-panel-hint">
            Packs bundle vector PMTiles, Terrain-RGB hillshade PMTiles (served offline via HTTP 206 Range slicing), graded tracks, live evidence, POIs, CAPAD parks, and state agency closures.
          </p>
          <div class="tf-pack-filter-bar" role="tablist" aria-label="Filter offline packs by category">
            <button
              type="button"
              role="tab"
              aria-selected="${this.activeFilter === "all"}"
              class="tf-pack-filter-pill ${this.activeFilter === "all" ? "active" : ""}"
              data-pack-filter="all"
            >All <span class="mono">(${allRegions.length})</span></button>
            <button
              type="button"
              role="tab"
              aria-selected="${this.activeFilter === "region"}"
              class="tf-pack-filter-pill ${this.activeFilter === "region" ? "active" : ""}"
              data-pack-filter="region"
            >Iconic 4WD Regions <span class="mono">(${regionCount})</span></button>
            <button
              type="button"
              role="tab"
              aria-selected="${this.activeFilter === "state"}"
              class="tf-pack-filter-pill ${this.activeFilter === "state" ? "active" : ""}"
              data-pack-filter="state"
            >Statewide <span class="mono">(${stateCount})</span></button>
          </div>
          <div class="tf-card-stack">${cardsHtml}</div>
        </section>
      </div>
    `;

    this.bindHandlers(allRegions);
  }

  private bindHandlers(regions: OfflinePackRegionItem[]): void {
    this.root
      .querySelector<HTMLButtonElement>("[data-role='close']")
      ?.addEventListener("click", () => {
        this.close();
        this.callbacks.onClose();
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='refresh-packs']")
      ?.addEventListener("click", () => {
        void this.callbacks.onRefresh();
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='exit-offline-pack']")
      ?.addEventListener("click", () => {
        void this.callbacks.onUseOfflinePack(null);
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='sync-now']")
      ?.addEventListener("click", () => {
        void this.callbacks.onSyncNow();
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-pack-filter]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const next = btn.dataset.packFilter as PackFilterCategory | undefined;
          if (next === "all" || next === "region" || next === "state") {
            this.activeFilter = next;
            this.render();
          }
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-build-pack]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const rid = btn.dataset.buildPack;
          if (rid) void this.callbacks.onBuildPack(rid);
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-download-pack]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const rid = btn.dataset.downloadPack;
          const region = regions.find((r) => r.region_id === rid);
          if (region) void this.callbacks.onDownloadPack(region);
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-use-pack]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const rid = btn.dataset.usePack;
          if (rid) {
            const next = this.state.activeOfflinePackId === rid ? null : rid;
            void this.callbacks.onUseOfflinePack(next);
          }
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-remove-pack]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const rid = btn.dataset.removePack;
          if (rid) void this.callbacks.onRemovePack(rid);
        });
      });
  }
}
