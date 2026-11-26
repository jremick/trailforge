import {
  formatClosureDateRange,
  formatClosureTypeLabel,
  formatRelativeTimestamp,
  getClosureStatusBadge,
  getClosureTone,
} from "../closuresHelpers";
import {
  buildElevationProfileSvg,
  formatBreakdownFactors,
  formatOverrideLabel,
  normalizeTrackInspector,
} from "../inspectorHelpers";
import {
  escapeHtml,
  getGradeColor,
  resolvePoiIconName,
} from "../presets";
import type {
  BBoxTuple,
  PoiItem,
  TrackInspectorDetail,
  VehicleCapabilityClass,
} from "../types";
import {
  computeClientSuitability,
  getSuitabilityTone,
} from "../vehicleHelpers";

export interface TrackInspectorContext {
  snowLineElevationM: number;
  activeVehicleClass: VehicleCapabilityClass;
  activeVehicleName: string | null;
  isFavorite: boolean;
  isAuthenticated: boolean;
}

export interface TrackInspectorCallbacks {
  onClose: () => void;
  onZoomToTrack: (bbox: BBoxTuple) => void;
  onPoiSelect: (poi: PoiItem) => void;
  onOpenRigPanel: () => void;
  onToggleFavorite: (trackId: string, currentlyFavorite: boolean) => void;
  onExportTrack: (trackId: string, format: "gpx" | "geojson") => void;
}

export class TrackInspectorDrawer {
  private readonly root: HTMLElement;
  private readonly callbacks: TrackInspectorCallbacks;
  private currentDetail: TrackInspectorDetail | null = null;
  private context: TrackInspectorContext = {
    snowLineElevationM: 1000,
    activeVehicleClass: "Stock 4WD",
    activeVehicleName: null,
    isFavorite: false,
    isAuthenticated: false,
  };
  private isOpen = false;

  constructor(container: HTMLElement, callbacks: TrackInspectorCallbacks) {
    this.root = container;
    this.callbacks = callbacks;
    this.root.className = "tf-inspector";
    this.root.setAttribute("aria-label", "Track Inspector");
    this.root.setAttribute("aria-hidden", "true");
    this.renderEmpty();
  }

  openState(): boolean {
    return this.isOpen;
  }

  getCurrentTrackId(): string | null {
    return this.currentDetail?.track_id ?? null;
  }

  updateContext(patch: Partial<TrackInspectorContext>): void {
    this.context = { ...this.context, ...patch };
    if (this.isOpen && this.currentDetail) {
      this.renderDetail(this.currentDetail);
    }
  }

  setSnowLineElevation(elevationM: number): void {
    this.updateContext({ snowLineElevationM: elevationM });
  }

  showLoading(trackLabel?: string): void {
    this.isOpen = true;
    this.currentDetail = null;
    this.root.setAttribute("aria-hidden", "false");
    this.root.innerHTML = `
      <div class="tf-inspector-head">
        <div>
          <div class="tf-inspector-kicker">Track Inspector</div>
          <h2 class="tf-inspector-title">${escapeHtml(trackLabel ?? "Loading track…")}</h2>
        </div>
        <button type="button" class="tf-inspector-close" data-role="close" aria-label="Close Track Inspector">×</button>
      </div>
      <div class="tf-inspector-body">
        <div class="tf-inspector-loading">Fetching canonical segments, closures, rig suitability &amp; elevation profile…</div>
      </div>
    `;
    this.bindClose();
  }

  showError(message: string): void {
    this.isOpen = true;
    this.currentDetail = null;
    this.root.setAttribute("aria-hidden", "false");
    this.root.innerHTML = `
      <div class="tf-inspector-head">
        <div>
          <div class="tf-inspector-kicker">Track Inspector</div>
          <h2 class="tf-inspector-title">Unable to load track</h2>
        </div>
        <button type="button" class="tf-inspector-close" data-role="close" aria-label="Close Track Inspector">×</button>
      </div>
      <div class="tf-inspector-body">
        <div class="tf-inspector-error">${escapeHtml(message)}</div>
      </div>
    `;
    this.bindClose();
  }

  showTrack(
    detail: TrackInspectorDetail,
    contextPatch?: Partial<TrackInspectorContext>
  ): void {
    this.isOpen = true;
    this.currentDetail = detail;
    if (contextPatch) {
      this.context = { ...this.context, ...contextPatch };
    }
    this.root.setAttribute("aria-hidden", "false");
    this.renderDetail(detail);
  }

  close(): void {
    this.isOpen = false;
    this.currentDetail = null;
    this.root.setAttribute("aria-hidden", "true");
  }

  private renderEmpty(): void {
    this.root.innerHTML = `
      <div class="tf-inspector-head">
        <div>
          <div class="tf-inspector-kicker">Track Inspector</div>
          <h2 class="tf-inspector-title">Select a 4WD Track</h2>
        </div>
        <button type="button" class="tf-inspector-close" data-role="close" aria-label="Close Track Inspector">×</button>
      </div>
      <div class="tf-inspector-body">
        <p class="tf-inspector-muted">Click any graded track on the map or search by track name to inspect elevation profile, closures &amp; alerts, rig suitability, explainable difficulty breakdown, creek crossings, nearby POIs, and CAPAD park tenure.</p>
      </div>
    `;
    this.bindClose();
  }

  private renderDetail(detail: TrackInspectorDetail): void {
    const norm = normalizeTrackInspector(detail);
    const gradeColor = getGradeColor(norm.grade);
    const factors = formatBreakdownFactors(norm.breakdown);
    const overrides = norm.breakdown?.overrides ?? [];
    const reasons = norm.breakdown?.reasons ?? [];
    const chart = buildElevationProfileSvg(norm.profile, {
      snowLineElevationM: this.context.snowLineElevationM,
      crossings: norm.crossings,
    });

    const aboveSnowline =
      norm.maxElevM !== null &&
      norm.maxElevM >= this.context.snowLineElevationM;
    const accessWarning =
      norm.hasGateBarrier ||
      ["private", "restricted", "closed"].includes(
        norm.accessStatus.toLowerCase()
      );

    const closureStatus = detail.closure_status ?? "open";
    const closureBadge = getClosureStatusBadge(closureStatus);
    const activeClosures = detail.active_closures ?? [];

    const badgesHtml = [
      closureBadge.tone !== "open"
        ? `<span class="tf-pill tf-pill-${closureBadge.tone === "critical" ? "red" : closureBadge.tone === "warning" ? "amber" : "ice"}">⚠ ${escapeHtml(closureBadge.label)}</span>`
        : "",
      `<span class="tf-pill mono">${escapeHtml(norm.state)}</span>`,
      norm.is4wdOnly
        ? `<span class="tf-pill tf-pill-amber">4WD Only</span>`
        : "",
      accessWarning && closureBadge.tone === "open"
        ? `<span class="tf-pill tf-pill-red">${norm.hasGateBarrier ? "Gate / " : ""}${escapeHtml(norm.accessStatus)}</span>`
        : "",
      aboveSnowline
        ? `<span class="tf-pill tf-pill-ice">Alpine &gt; ${this.context.snowLineElevationM} m</span>`
        : "",
    ]
      .filter(Boolean)
      .join("");

    // Slice 05 Personalized Rig Suitability Card (server-authoritative when returned, client fallback offline)
    const suitability =
      detail.suitability ??
      computeClientSuitability(norm.grade, this.context.activeVehicleClass, {
        vehicleName: this.context.activeVehicleName,
        closureStatus,
      });
    const suitTone = getSuitabilityTone(suitability.rating);
    const rigSubtitle = suitability.vehicle_name
      ? `${suitability.vehicle_name} (${suitability.vehicle_class})`
      : `${suitability.vehicle_class} profile`;

    const suitabilityHtml = `
      <section class="tf-suitability-card tone-${suitTone}">
        <div class="tf-suit-head">
          <div>
            <div class="tf-suit-kicker">Rig Suitability · ${escapeHtml(rigSubtitle)}</div>
            <div class="tf-suit-rating">${escapeHtml(suitability.rating)}</div>
          </div>
          <button type="button" class="tf-mini-btn" data-role="change-rig">Change Rig</button>
        </div>
        ${
          suitability.reason
            ? `<div class="tf-suit-reason">${escapeHtml(suitability.reason)}</div>`
            : ""
        }
        ${
          suitability.warnings && suitability.warnings.length > 0
            ? `<ul class="tf-suit-warnings">
                ${suitability.warnings.map((w) => `<li>⚠ ${escapeHtml(w)}</li>`).join("")}
              </ul>`
            : ""
        }
      </section>
    `;

    // Slice 04 Official Closures & Access Alerts Panel
    const closuresPanelHtml =
      activeClosures.length > 0
        ? `
          <section class="tf-insp-section tf-closures-panel">
            <div class="tf-insp-sec-head">
              <h3>⚠ Closures &amp; Access Alerts</h3>
              <span class="mono tf-insp-meta">${activeClosures.length}</span>
            </div>
            <div class="tf-card-stack">
              ${activeClosures
                .map((c) => {
                  const tone = getClosureTone(c.closure_type, c.severity);
                  const typeLabel = formatClosureTypeLabel(c.closure_type);
                  const dateSpan = formatClosureDateRange(
                    c.effective_from,
                    c.effective_to
                  );
                  const updatedAgo = formatRelativeTimestamp(c.last_updated_at);
                  const safeUrl =
                    c.source_url && /^https?:\/\//i.test(c.source_url)
                      ? c.source_url
                      : null;
                  return `
                    <div class="tf-closure-card tone-${tone}">
                      <div class="tf-closure-top">
                        <span class="tf-closure-type">${escapeHtml(typeLabel)}</span>
                        ${
                          c.is_stale
                            ? `<span class="tf-stale-badge" title="Upstream agency feed had errors; displaying last-known cached closure">STALE CACHE</span>`
                            : ""
                        }
                      </div>
                      <div class="tf-closure-title">${escapeHtml(c.title)}</div>
                      ${
                        c.description
                          ? `<p class="tf-closure-desc">${escapeHtml(c.description)}</p>`
                          : ""
                      }
                      <div class="tf-closure-meta">
                        ${c.agency ? `<span>${escapeHtml(c.agency)}</span>` : ""}
                        ${dateSpan ? `<span>· ${escapeHtml(dateSpan)}</span>` : ""}
                        ${c.last_updated_at ? `<span>· Updated ${escapeHtml(updatedAgo)}</span>` : ""}
                      </div>
                      ${
                        safeUrl
                          ? `<a class="tf-closure-link" href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">Official Agency Source ↗</a>`
                          : ""
                      }
                    </div>
                  `;
                })
                .join("")}
            </div>
          </section>
        `
        : "";

    const elevRangeText =
      norm.minElevM !== null && norm.maxElevM !== null
        ? `${Math.round(norm.minElevM)}–${Math.round(norm.maxElevM)} m`
        : "—";

    const gainLossText =
      norm.elevLossM !== null
        ? `+${Math.round(norm.elevGainM)} / −${Math.round(norm.elevLossM)} m`
        : `+${Math.round(norm.elevGainM)} m`;

    const factorsHtml =
      factors.length > 0
        ? factors
            .map(
              (f) => `
                <div class="tf-factor-row">
                  <div class="tf-factor-head">
                    <span class="tf-factor-label">${escapeHtml(f.label)}</span>
                    <span class="mono tf-factor-pts">${f.points} / ${f.maxPoints}</span>
                  </div>
                  <div class="tf-factor-track">
                    <div class="tf-factor-fill" style="width:${f.pct}%;background:${gradeColor}"></div>
                  </div>
                  <div class="tf-factor-detail">${escapeHtml(f.detail)}</div>
                </div>
              `
            )
            .join("")
        : `<p class="tf-inspector-muted">No structured baseline breakdown returned for this track.</p>`;

    const overridesHtml =
      overrides.length > 0
        ? `<div class="tf-override-list">
            ${overrides
              .map(
                (ov) =>
                  `<div class="tf-override-badge">▲ ${escapeHtml(formatOverrideLabel(ov))}</div>`
              )
              .join("")}
          </div>`
        : "";

    const reasonsHtml =
      reasons.length > 0
        ? `<ul class="tf-reason-list">
            ${reasons.map((r) => `<li>${escapeHtml(r)}</li>`).join("")}
          </ul>`
        : "";

    const crossingsHtml =
      norm.crossings.length > 0
        ? norm.crossings
            .map((c) => {
              const name = c.name || "Unnamed watercourse";
              const type = (c.waterway_type ?? "stream").replace(/_/g, " ");
              const flowTag = c.perennial ? "Perennial" : "Intermittent";
              const bridgeTag = c.bridged ? "Bridged" : "Unbridged Ford";
              const distText =
                typeof c.dist_m === "number"
                  ? `${(c.dist_m / 1000).toFixed(2)} km`
                  : "";
              return `
                <div class="tf-list-card">
                  <div class="tf-list-card-head">
                    <span class="tf-list-card-title">${escapeHtml(name)}</span>
                    <span class="mono tf-list-card-sub">${escapeHtml(distText)}</span>
                  </div>
                  <div class="tf-list-card-tags">
                    <span class="tf-mini-tag">${escapeHtml(type)}</span>
                    <span class="tf-mini-tag ${c.perennial ? "cyan" : ""}">${escapeHtml(flowTag)}</span>
                    <span class="tf-mini-tag ${c.bridged ? "emerald" : "amber"}">${escapeHtml(bridgeTag)}</span>
                  </div>
                </div>
              `;
            })
            .join("")
        : `<p class="tf-inspector-muted">No recorded waterway crossings along this track.</p>`;

    const nearbyPois = detail.nearby_pois ?? [];
    const poisHtml =
      nearbyPois.length > 0
        ? nearbyPois
            .map((poi, idx) => {
              const iconName = resolvePoiIconName(
                poi.icon ?? poi.category ?? poi.poi_type
              );
              const elev = poi.elevation_m ?? poi.elev_m;
              const meta = [
                (poi.category ?? poi.poi_type ?? "poi").replace(/_/g, " "),
                typeof elev === "number" ? `${Math.round(elev)} m` : null,
                typeof poi.distance_m === "number"
                  ? `${Math.round(poi.distance_m)} m away`
                  : null,
              ]
                .filter(Boolean)
                .join(" · ");
              return `
                <button type="button" class="tf-poi-item" data-poi-index="${idx}">
                  <img src="/icons/${escapeHtml(iconName)}.svg" alt="" width="18" height="18" />
                  <span class="tf-poi-copy">
                    <span class="tf-poi-name">${escapeHtml(poi.name)}</span>
                    <span class="tf-poi-meta">${escapeHtml(meta)}</span>
                    ${poi.access_note ? `<span class="tf-poi-note">${escapeHtml(poi.access_note)}</span>` : ""}
                  </span>
                </button>
              `;
            })
            .join("")
        : `<p class="tf-inspector-muted">No POIs recorded within 1,500 m.</p>`;

    const parksHtml =
      norm.parks.length > 0
        ? norm.parks
            .map((pk) => {
              const meta = [
                pk.park_type,
                pk.type_abbr,
                pk.iucn ? `IUCN ${pk.iucn}` : null,
                pk.state,
              ]
                .filter(Boolean)
                .join(" · ");
              return `
                <div class="tf-list-card">
                  <div class="tf-list-card-title">${escapeHtml(pk.name)}</div>
                  <div class="tf-list-card-sub">${escapeHtml(meta)}</div>
                </div>
              `;
            })
            .join("")
        : `<p class="tf-inspector-muted">Outside mapped CAPAD 2025 reserve boundaries.</p>`;

    this.root.innerHTML = `
      <div class="tf-inspector-head">
        <div class="tf-inspector-head-copy">
          <div class="tf-inspector-kicker">Track Inspector</div>
          <h2 class="tf-inspector-title">${escapeHtml(norm.name)}</h2>
          ${
            norm.altNames.length > 0
              ? `<div class="tf-inspector-alt">Also: ${escapeHtml(norm.altNames.join(", "))}</div>`
              : ""
          }
          <div class="tf-inspector-badges">${badgesHtml}</div>
        </div>
        <button type="button" class="tf-inspector-close" data-role="close" aria-label="Close Track Inspector">×</button>
      </div>

      <div class="tf-inspector-body">
        <div class="tf-inspector-actions">
          <button
            type="button"
            class="tf-action-btn ${this.context.isFavorite ? "starred" : ""}"
            data-role="toggle-fav"
            title="${this.context.isAuthenticated ? "Toggle saved favorite" : "Sign in to save favorite"}"
          >${this.context.isFavorite ? "★ Saved" : "☆ Save"}</button>
          <button type="button" class="tf-action-btn" data-export-track="gpx" title="Download GridForge-compatible GPX 1.1">⬇ GPX</button>
          <button type="button" class="tf-action-btn" data-export-track="geojson" title="Download GeoJSON FeatureCollection">⬇ GeoJSON</button>
          ${
            detail.bbox
              ? `<button type="button" class="tf-action-btn" data-role="zoom-track">⌖ Zoom</button>`
              : ""
          }
        </div>

        ${closuresPanelHtml}

        <div class="tf-grade-hero" style="border-color:${gradeColor}">
          <div class="tf-grade-hero-left">
            <span class="tf-grade-swatch" style="background:${gradeColor}"></span>
            <div>
              <div class="tf-grade-name" style="color:${gradeColor}">${escapeHtml(norm.grade)}</div>
              <div class="tf-grade-sub">Day-1 Deterministic Baseline</div>
            </div>
          </div>
          <div class="tf-grade-hero-right">
            ${
              norm.score !== null
                ? `<div class="tf-grade-score mono">${Math.round(norm.score)}<span>/100</span></div>`
                : ""
            }
          </div>
        </div>

        ${suitabilityHtml}

        <div class="tf-stat-grid">
          <div class="tf-stat-card">
            <span class="tf-stat-label">Distance</span>
            <span class="tf-stat-val mono">${norm.totalLengthKm.toFixed(2)} km</span>
          </div>
          <div class="tf-stat-card">
            <span class="tf-stat-label">Elevation</span>
            <span class="tf-stat-val mono">${escapeHtml(elevRangeText)}</span>
          </div>
          <div class="tf-stat-card">
            <span class="tf-stat-label">Climb / Drop</span>
            <span class="tf-stat-val mono">${escapeHtml(gainLossText)}</span>
          </div>
          <div class="tf-stat-card">
            <span class="tf-stat-label">Max Slope</span>
            <span class="tf-stat-val mono">${norm.maxSlopeDeg !== null ? `${norm.maxSlopeDeg.toFixed(1)}°` : "—"}</span>
          </div>
          <div class="tf-stat-card">
            <span class="tf-stat-label">Sustained</span>
            <span class="tf-stat-val mono">${norm.sustainedSlopeDeg !== null ? `${norm.sustainedSlopeDeg.toFixed(1)}°` : "—"}</span>
          </div>
          <div class="tf-stat-card">
            <span class="tf-stat-label">Crossings</span>
            <span class="tf-stat-val mono">${norm.waterwayCrossingCount}</span>
          </div>
        </div>

        <section class="tf-insp-section">
          <div class="tf-insp-sec-head">
            <h3>Elevation Profile</h3>
            <span class="mono tf-insp-meta">${
              chart.steepZoneCount > 0
                ? `${chart.steepZoneCount} steep ≥14° ${chart.steepZoneCount === 1 ? "zone" : "zones"}`
                : "DEM AHD"
            }</span>
          </div>
          <div class="tf-elev-chart-wrap">
            ${chart.svgHtml}
          </div>
        </section>

        <section class="tf-insp-section">
          <div class="tf-insp-sec-head">
            <h3>Explainable Grade Breakdown</h3>
            <span class="mono tf-insp-meta">${escapeHtml(norm.breakdown?.formula_version ?? "baseline_v1")}</span>
          </div>
          <div class="tf-factor-list">${factorsHtml}</div>
          ${overridesHtml}
          ${reasonsHtml}
          <div class="tf-telemetry-note">
            Live crowdsourced traversals, vehicle-class split &amp; 72 h Open-Meteo rainfall context arrive in Slice 07.
          </div>
        </section>

        <section class="tf-insp-section">
          <div class="tf-insp-sec-head">
            <h3>Waterway Crossings</h3>
            <span class="mono tf-insp-meta">${norm.crossings.length}</span>
          </div>
          <div class="tf-card-stack">${crossingsHtml}</div>
        </section>

        <section class="tf-insp-section">
          <div class="tf-insp-sec-head">
            <h3>Nearby POIs</h3>
            <span class="mono tf-insp-meta">${nearbyPois.length}</span>
          </div>
          <div class="tf-poi-list">${poisHtml}</div>
        </section>

        <section class="tf-insp-section">
          <div class="tf-insp-sec-head">
            <h3>CAPAD Park / Forest Tenure</h3>
            <span class="mono tf-insp-meta">${norm.parks.length}</span>
          </div>
          <div class="tf-card-stack">${parksHtml}</div>
        </section>

        <div class="tf-insp-lineage mono">
          Source: ${escapeHtml(detail.primary_source ?? "canonical")}
          ${norm.breakdown?.dem_tier ? ` · DEM: ${escapeHtml(norm.breakdown.dem_tier)}` : ""}
          ${detail.segment_count ? ` · ${detail.segment_count} segments` : ""}
        </div>
      </div>
    `;

    this.bindClose();

    this.root
      .querySelector<HTMLButtonElement>("[data-role='change-rig']")
      ?.addEventListener("click", () => this.callbacks.onOpenRigPanel());

    this.root
      .querySelector<HTMLButtonElement>("[data-role='toggle-fav']")
      ?.addEventListener("click", () => {
        this.callbacks.onToggleFavorite(
          detail.track_id,
          this.context.isFavorite
        );
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-export-track]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const fmt = btn.dataset.exportTrack as "gpx" | "geojson";
          this.callbacks.onExportTrack(detail.track_id, fmt);
        });
      });

    const zoomBtn = this.root.querySelector<HTMLButtonElement>(
      "[data-role='zoom-track']"
    );
    if (zoomBtn && detail.bbox) {
      const bbox = detail.bbox;
      zoomBtn.addEventListener("click", () => this.callbacks.onZoomToTrack(bbox));
    }

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-poi-index]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const idx = Number(btn.dataset.poiIndex);
          const poi = nearbyPois[idx];
          if (poi) this.callbacks.onPoiSelect(poi);
        });
      });
  }

  private bindClose(): void {
    const btn = this.root.querySelector<HTMLButtonElement>("[data-role='close']");
    btn?.addEventListener("click", () => {
      this.close();
      this.callbacks.onClose();
    });
  }
}
