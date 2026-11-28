import { renderSheetHandleMarkup } from "../bottomSheet";
import {
  formatClosureDateRange,
  formatClosurePlainText,
  formatClosureTypeLabel,
  formatRelativeTimestamp,
  getClosureStatusBadge,
  getClosureTone,
} from "../closuresHelpers";
import {
  formatConfidenceBadge,
  formatDurationMinutes,
  formatHotspotReason,
  formatLastDriven,
  formatWetRiskBanner,
  normalizeVehicleClassSplit,
} from "../gradingHelpers";
import { uiIcon } from "../icons";
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
import {
  CONDITION_REPORT_TYPES,
  formatReportFreshness,
  formatReportTypeLabel,
  getReportId,
  getReportTypeColor,
  partitionTrackReports,
  resolveReportOsmLinks,
  sanitizeReportPhotoUrl,
} from "../reportsHelpers";
import type {
  BasemapId,
  BBoxTuple,
  ClosureItem,
  ConditionReportCreateInput,
  ConditionReportType,
  PoiItem,
  ReportVoteKind,
  TileManifestResponse,
  TrackInspectorDetail,
  VehicleCapabilityClass,
  WaterwayCrossingItem,
} from "../types";
import {
  computeClientSuitability,
  getSuitabilityTone,
} from "../vehicleHelpers";
import { ElevationProfileModal } from "./elevationModal";

export { sanitizeReportPhotoUrl };

export type TrackInspectorTab = "overview" | "conditions" | "along";

export interface TrackInspectorContext {
  snowLineElevationM: number;
  activeVehicleClass: VehicleCapabilityClass;
  activeVehicleName: string | null;
  isFavorite: boolean;
  isInRoute?: boolean;
  isAuthenticated: boolean;
  pickedReportCoords?: { lat: number; lon: number } | null;
  activeBasemap?: BasemapId;
  tileManifest?: TileManifestResponse | null;
  isOfflinePack?: boolean;
}

export interface TrackInspectorCallbacks {
  onClose: () => void;
  onZoomToTrack: (bbox: BBoxTuple) => void;
  onPoiSelect: (poi: PoiItem) => void;
  onHotspotSelect?: (lat: number, lon: number, label: string) => void;
  onOpenClosure?: (closure: ClosureItem) => void;
  onOpenRigPanel: () => void;
  onRequireSignIn: () => void;
  onToggleFavorite: (trackId: string, currentlyFavorite: boolean) => void;
  onToggleTrackInRoute: (
    trackId: string,
    trackName: string,
    currentlyInRoute: boolean
  ) => void;
  onExportTrack: (trackId: string, format: "gpx" | "geojson") => void;
  onShareTrack?: (trackId: string) => void;
  onElevationModalChange?: (open: boolean) => void;
  onPickReportLocation: () => void;
  onSubmitReport: (input: ConditionReportCreateInput) => Promise<void>;
  onVoteReport: (
    reportId: string,
    vote: ReportVoteKind
  ) => Promise<void>;
}

function deduplicateCrossings(
  crossings: readonly WaterwayCrossingItem[]
): WaterwayCrossingItem[] {
  const seen = new Set<string>();
  const out: WaterwayCrossingItem[] = [];
  for (const c of crossings) {
    const nameKey = (c.name ?? "unnamed").trim().toLowerCase();
    const distBucket =
      typeof c.dist_m === "number" ? Math.round(c.dist_m / 40) : -1;
    const key = `${nameKey}:${distBucket}`;
    if (nameKey !== "unnamed" && seen.has(key)) continue;
    seen.add(key);
    out.push(c);
  }
  return out;
}

function deduplicatePois(pois: readonly PoiItem[]): PoiItem[] {
  const seen = new Set<string>();
  const out: PoiItem[] = [];
  for (const poi of pois) {
    const normName = poi.name
      .trim()
      .toLowerCase()
      .replace(/\bcampsite\b/g, "campground")
      .replace(/\s+/g, " ");
    const coordBucket = `${Math.round(poi.lat * 200)}:${Math.round(poi.lon * 200)}`;
    const key = `${normName}:${coordBucket}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(poi);
  }
  return out;
}

function stripLeadingWarningGlyph(text: string): string {
  return text.replace(/^[⚠⛔🚧!\s]+/u, "").trim();
}

export class TrackInspectorDrawer {
  private readonly root: HTMLElement;
  private readonly callbacks: TrackInspectorCallbacks;
  private readonly elevationModal: ElevationProfileModal;
  private currentDetail: TrackInspectorDetail | null = null;
  private activeTab: TrackInspectorTab = "overview";
  private exportMenuOpen = false;
  private showReportForm = false;
  private reportSubmitting = false;
  private reportError: string | null = null;
  private reportEpoch = 0;
  private context: TrackInspectorContext = {
    snowLineElevationM: 1000,
    activeVehicleClass: "Stock 4WD",
    activeVehicleName: null,
    isFavorite: false,
    isAuthenticated: false,
    pickedReportCoords: null,
  };
  private isOpen = false;

  constructor(container: HTMLElement, callbacks: TrackInspectorCallbacks) {
    this.root = container;
    this.callbacks = callbacks;
    this.elevationModal = new ElevationProfileModal({
      onZoomToTrack: (bbox) => this.callbacks.onZoomToTrack(bbox),
      onExportTrack: (trackId, format) =>
        this.callbacks.onExportTrack(trackId, format),
      onShareTrack: (trackId) => this.callbacks.onShareTrack?.(trackId),
      onOpenStateChange: (open) => this.callbacks.onElevationModalChange?.(open),
    });
    this.root.className = "tf-drawer tf-inspector";
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

  openElevationModal(): void {
    if (!this.currentDetail) return;
    const norm = normalizeTrackInspector(this.currentDetail);
    if (norm.profile.length >= 2) {
      this.elevationModal.open(this.currentDetail, {
        snowLineElevationM: this.context.snowLineElevationM,
        activeBasemap: this.context.activeBasemap,
        tileManifest: this.context.tileManifest,
        isOfflinePack: this.context.isOfflinePack,
      });
    }
  }

  closeElevationModal(): void {
    this.elevationModal.close();
  }

  isElevationModalOpen(): boolean {
    return this.elevationModal.isModalOpen();
  }

  updateContext(patch: Partial<TrackInspectorContext>): void {
    this.context = { ...this.context, ...patch };
    if (patch.pickedReportCoords) {
      this.showReportForm = true;
      this.activeTab = "conditions";
    }
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
    this.activeTab = "overview";
    this.exportMenuOpen = false;
    this.showReportForm = false;
    this.reportError = null;
    this.root.setAttribute("aria-hidden", "false");
    this.root.innerHTML = `
      ${renderSheetHandleMarkup("Resize track inspector")}
      <div class="tf-inspector-head">
        <div class="tf-inspector-head-copy">
          <h2 class="tf-inspector-title">${escapeHtml(trackLabel ?? "Loading track…")}</h2>
          <div class="tf-inspector-subline">Fetching track geometry &amp; conditions</div>
        </div>
        <button type="button" class="tf-inspector-close" data-role="close" aria-label="Close Track Inspector">${uiIcon("close", 15)}</button>
      </div>
      <div class="tf-inspector-body">
        <div class="tf-inspector-loading">Loading segments, elevation profile, agency closures, condition reports, and rig suitability…</div>
      </div>
    `;
    this.bindClose();
  }

  showError(message: string): void {
    this.isOpen = true;
    this.currentDetail = null;
    this.root.setAttribute("aria-hidden", "false");
    this.root.innerHTML = `
      ${renderSheetHandleMarkup("Resize track inspector")}
      <div class="tf-inspector-head">
        <div class="tf-inspector-head-copy">
          <h2 class="tf-inspector-title">Unable to load track</h2>
        </div>
        <button type="button" class="tf-inspector-close" data-role="close" aria-label="Close Track Inspector">${uiIcon("close", 15)}</button>
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
    const isNewTrack = this.currentDetail?.track_id !== detail.track_id;
    this.isOpen = true;
    this.currentDetail = detail;
    if (isNewTrack) {
      this.activeTab = "overview";
      this.exportMenuOpen = false;
    }
    if (contextPatch) {
      this.context = { ...this.context, ...contextPatch };
    }
    this.root.setAttribute("aria-hidden", "false");
    this.renderDetail(detail);
  }

  close(): void {
    this.isOpen = false;
    this.currentDetail = null;
    this.exportMenuOpen = false;
    this.resetReportForm();
    this.elevationModal.close();
    this.root.setAttribute("aria-hidden", "true");
    this.root.replaceChildren();
  }

  resetReportForm(): void {
    this.reportEpoch += 1;
    this.showReportForm = false;
    this.reportError = null;
    this.reportSubmitting = false;
    this.context.pickedReportCoords = null;
    if (this.isOpen && this.currentDetail) this.renderDetail(this.currentDetail);
  }

  private renderEmpty(): void {
    this.root.innerHTML = `
      ${renderSheetHandleMarkup("Resize track inspector")}
      <div class="tf-inspector-head">
        <div class="tf-inspector-head-copy">
          <h2 class="tf-inspector-title">Select a 4WD track</h2>
        </div>
        <button type="button" class="tf-inspector-close" data-role="close" aria-label="Close Track Inspector">${uiIcon("close", 15)}</button>
      </div>
      <div class="tf-inspector-body">
        <p class="tf-inspector-muted">Click any graded track on the map or search by name to inspect elevation, official closures, rig suitability, difficulty factors, creek crossings, and nearby camps.</p>
      </div>
    `;
    this.bindClose();
  }

  private resolveDefaultReportCoords(detail: TrackInspectorDetail): {
    lat: number;
    lon: number;
  } {
    if (this.context.pickedReportCoords) {
      return this.context.pickedReportCoords;
    }
    if (detail.bbox) {
      const [w, s, e, n] = detail.bbox;
      return { lat: (s + n) / 2, lon: (w + e) / 2 };
    }
    const poi = detail.nearby_pois?.[0];
    if (poi) return { lat: poi.lat, lon: poi.lon };
    return { lat: -36.615, lon: 148.125 };
  }

  private renderDetail(detail: TrackInspectorDetail): void {
    const norm = normalizeTrackInspector(detail);
    const gradeColor = getGradeColor(norm.grade);
    const factors = formatBreakdownFactors(norm.breakdown);
    const overrides = norm.breakdown?.overrides ?? [];
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

    const { gateWarnings, timelineReports } = partitionTrackReports(
      detail.condition_reports ?? [],
      detail.community_gate_warnings ?? null
    );

    const statusTagHtml =
      closureBadge.tone !== "open"
        ? `<span class="tf-status-tag tone-${closureBadge.tone === "critical" ? "danger" : closureBadge.tone === "warning" ? "warn" : "info"}">${uiIcon("alert", 12)}<span>${escapeHtml(closureBadge.label)}</span></span>`
        : gateWarnings.length > 0
          ? `<span class="tf-status-tag tone-danger">${uiIcon("gate", 12)}<span>${gateWarnings.length} Gate alert${gateWarnings.length === 1 ? "" : "s"}</span></span>`
          : accessWarning
            ? `<span class="tf-status-tag tone-warn">${uiIcon("gate", 12)}<span>${norm.hasGateBarrier ? "Gated" : escapeHtml(norm.accessStatus)}</span></span>`
            : "";

    const metaParts = [
      norm.state,
      norm.is4wdOnly ? "4WD only" : null,
      aboveSnowline
        ? `Alpine > ${this.context.snowLineElevationM.toLocaleString()} m`
        : null,
    ].filter(Boolean);

    const suitability =
      detail.suitability ??
      computeClientSuitability(norm.grade, this.context.activeVehicleClass, {
        vehicleName: this.context.activeVehicleName,
        closureStatus,
      });
    const suitTone = getSuitabilityTone(suitability.rating);
    const rigSubtitle = suitability.vehicle_name
      ? `${suitability.vehicle_name} (${suitability.vehicle_class})`
      : suitability.vehicle_class;

    const liveBreakdown = detail.live_breakdown ?? null;
    const liveGrade = detail.live_grade ?? liveBreakdown?.live_grade ?? null;
    const liveScore = detail.live_score ?? liveBreakdown?.live_score ?? null;
    const sampleCount =
      detail.sample_count ?? liveBreakdown?.sample_count ?? 0;
    const confidence = formatConfidenceBadge(
      detail.confidence_level ?? liveBreakdown?.confidence_level,
      sampleCount
    );
    const lastDriven = formatLastDriven(
      detail.last_driven_at ?? liveBreakdown?.last_driven_at
    );
    const avgSpeedKmh =
      detail.avg_speed_kmh ?? liveBreakdown?.avg_speed_kmh ?? null;
    const estDurationText = formatDurationMinutes(
      detail.est_duration_min,
      liveBreakdown?.avg_duration_s
    );
    const vehicleSplit = normalizeVehicleClassSplit(
      detail.vehicle_class_split ?? liveBreakdown?.vehicle_class_split
    );
    const wetRisk = formatWetRiskBanner(detail.rainfall_context);
    const hotspots = detail.obstacle_hotspots ?? [];
    const liveReasons = liveBreakdown?.reasons ?? [];
    const outlierClamped = liveBreakdown?.outlier_clamped_count ?? 0;

    const displayCrossings = deduplicateCrossings(norm.crossings);
    const displayPois = deduplicatePois(detail.nearby_pois ?? []);

    const alertTotal = activeClosures.length + gateWarnings.length;
    const conditionCount = alertTotal + timelineReports.length;
    const alongCount = displayCrossings.length + displayPois.length;

    // Compact Alert Summary Strip above fold so closures are unmissable without burying track stats
    const alertStripHtml =
      alertTotal > 0
        ? `
          <button
            type="button"
            class="tf-alert-strip tone-${closureBadge.tone === "critical" || gateWarnings.length > 0 ? "danger" : "warn"}"
            data-jump-tab="conditions"
          >
            ${uiIcon("alert", 15)}
            <span class="tf-alert-strip-copy">
              <strong>${alertTotal} active ${alertTotal === 1 ? "alert" : "alerts"}</strong>
              <span>${escapeHtml(
                activeClosures[0]?.title ??
                  gateWarnings[0]?.title ??
                  "Check official closures and gate warnings"
              )}</span>
            </span>
            <span class="tf-alert-strip-cta">View</span>
          </button>
        `
        : "";

    const elevRangeText =
      norm.minElevM !== null && norm.maxElevM !== null
        ? `${Math.round(norm.minElevM)}–${Math.round(norm.maxElevM)} m`
        : "-";
    const gainLossText =
      norm.elevLossM !== null
        ? `+${Math.round(norm.elevGainM)} / −${Math.round(norm.elevLossM)} m`
        : `+${Math.round(norm.elevGainM)} m`;
    const slopeText =
      norm.maxSlopeDeg !== null
        ? `${norm.maxSlopeDeg.toFixed(1)}° peak${
            norm.sustainedSlopeDeg !== null
              ? ` · ${norm.sustainedSlopeDeg.toFixed(1)}° sust`
              : ""
          }`
        : "-";

    const overviewTabHtml = this.renderOverviewTab({
      norm,
      detail,
      gradeColor,
      factors,
      overrides,
      chart,
    });

    const conditionsTabHtml = this.renderConditionsTab({
      detail,
      activeClosures,
      gateWarnings,
      timelineReports,
      wetRisk,
      liveBreakdown,
      liveGrade,
      liveScore,
      sampleCount,
      confidence,
      lastDriven,
      avgSpeedKmh,
      estDurationText,
      vehicleSplit,
      hotspots,
      liveReasons,
      outlierClamped,
    });

    const alongTabHtml = this.renderAlongTab(displayCrossings, displayPois);

    this.root.innerHTML = `
      ${renderSheetHandleMarkup("Resize track inspector")}
      <div class="tf-inspector-head">
        <div class="tf-inspector-head-copy">
          <h2 class="tf-inspector-title">${escapeHtml(norm.name)}</h2>
          ${
            norm.altNames.length > 0
              ? `<div class="tf-inspector-alt">Also: ${escapeHtml(norm.altNames.join(", "))}</div>`
              : ""
          }
          <div class="tf-inspector-subline">
            ${statusTagHtml}
            <span>${escapeHtml(metaParts.join(" · "))}</span>
          </div>
        </div>
        <button type="button" class="tf-inspector-close" data-role="close" aria-label="Close Track Inspector">${uiIcon("close", 15)}</button>
      </div>

      <div class="tf-inspector-body">
        <div class="tf-inspector-actionbar">
          <button
            type="button"
            class="tf-btn-primary tf-inspector-primary-cta ${this.context.isInRoute ? "in-route" : ""}"
            data-role="toggle-route"
            title="${this.context.isInRoute ? "Open active multi-track route" : "Add this track to the active route"}"
          >
            ${uiIcon(this.context.isInRoute ? "check" : "route", 14)}
            <span>${this.context.isInRoute ? "In route" : "Add to route"}</span>
          </button>

          <div class="tf-inspector-util-btns">
            <button
              type="button"
              class="tf-icon-action-btn ${this.context.isFavorite ? "starred" : ""}"
              data-role="toggle-fav"
              title="${this.context.isAuthenticated ? (this.context.isFavorite ? "Remove from saved" : "Save favorite") : "Sign in to save favorite"}"
              aria-label="${this.context.isFavorite ? "Remove favorite" : "Save favorite"}"
            >${uiIcon(this.context.isFavorite ? "starFilled" : "star", 15)}</button>

            <div class="tf-export-wrap" data-role="export-wrap">
              <button
                type="button"
                class="tf-icon-action-btn ${this.exportMenuOpen ? "active" : ""}"
                data-role="toggle-export-menu"
                aria-haspopup="menu"
                aria-expanded="${this.exportMenuOpen}"
                title="Export track GPX or GeoJSON"
                aria-label="Export track"
              >${uiIcon("download", 15)}</button>
              <div class="tf-export-menu ${this.exportMenuOpen ? "" : "hidden"}" data-role="export-menu" role="menu">
                <button type="button" class="tf-export-menu-item" role="menuitem" data-export-track="gpx">
                  ${uiIcon("download", 13)}
                  <span>Export GPX 1.1</span>
                </button>
                <button type="button" class="tf-export-menu-item" role="menuitem" data-export-track="geojson">
                  ${uiIcon("download", 13)}
                  <span>Export GeoJSON</span>
                </button>
              </div>
            </div>

            <button
              type="button"
              class="tf-icon-action-btn"
              data-role="share-track"
              title="Copy shareable link to this track"
              aria-label="Share track link"
            >${uiIcon("share", 15)}</button>

            ${
              detail.bbox
                ? `<button type="button" class="tf-icon-action-btn" data-role="zoom-track" title="Zoom map to full track" aria-label="Zoom to track">${uiIcon("target", 15)}</button>`
                : ""
            }
          </div>
        </div>

        ${alertStripHtml}

        <div class="tf-decision-card tone-${suitTone}" style="--grade-accent:${gradeColor}">
          <div class="tf-decision-top">
            <div class="tf-grade-hero-left">
              <span class="tf-grade-swatch" style="background:${gradeColor}"></span>
              <div>
                <div class="tf-grade-name" style="color:${gradeColor}">${escapeHtml(norm.grade)}</div>
                <div class="tf-grade-sub">${
                  liveGrade && sampleCount > 0
                    ? `Baseline + ${sampleCount} live traversal${sampleCount === 1 ? "" : "s"}`
                    : "Deterministic terrain baseline"
                }</div>
              </div>
            </div>
            ${
              norm.score !== null
                ? `<div class="tf-grade-score">${Math.round(norm.score)}<span>/100</span></div>`
                : ""
            }
          </div>

          <div class="tf-decision-suit">
            <div class="tf-suit-head">
              <div>
                <div class="tf-suit-kicker">${escapeHtml(rigSubtitle)}</div>
                <div class="tf-suit-rating">${escapeHtml(suitability.rating)}</div>
              </div>
              <button type="button" class="tf-text-action-btn" data-role="change-rig">Change rig</button>
            </div>
            ${
              suitability.reason
                ? `<div class="tf-suit-reason">${escapeHtml(suitability.reason)}</div>`
                : ""
            }
            ${
              suitability.warnings && suitability.warnings.length > 0
                ? `<ul class="tf-suit-warnings">
                    ${suitability.warnings
                      .map(
                        (w) =>
                          `<li>${uiIcon("alert", 12)}<span>${escapeHtml(stripLeadingWarningGlyph(w))}</span></li>`
                      )
                      .join("")}
                  </ul>`
                : ""
            }
          </div>
        </div>

        <div class="tf-metric-grid">
          <div class="tf-metric-cell">
            <span class="tf-metric-label">Distance</span>
            <span class="tf-metric-val">${norm.totalLengthKm.toFixed(2)} km</span>
          </div>
          <div class="tf-metric-cell">
            <span class="tf-metric-label">Elevation</span>
            <span class="tf-metric-val">${escapeHtml(elevRangeText)}</span>
          </div>
          <div class="tf-metric-cell">
            <span class="tf-metric-label">Climb / drop</span>
            <span class="tf-metric-val">${escapeHtml(gainLossText)}</span>
          </div>
          <div class="tf-metric-cell">
            <span class="tf-metric-label">Slope</span>
            <span class="tf-metric-val">${escapeHtml(slopeText)}</span>
          </div>
          <div class="tf-metric-cell">
            <span class="tf-metric-label">Crossings</span>
            <span class="tf-metric-val">${displayCrossings.length}</span>
          </div>
          <div class="tf-metric-cell">
            <span class="tf-metric-label">Est. drive time</span>
            <span class="tf-metric-val">${escapeHtml(estDurationText)}</span>
          </div>
        </div>

        <div class="tf-segmented-control tf-inspector-tabs" role="tablist" aria-label="Track detail sections">
          <button
            type="button"
            role="tab"
            class="tf-seg-btn ${this.activeTab === "overview" ? "active" : ""}"
            aria-selected="${this.activeTab === "overview"}"
            data-insp-tab="overview"
          >Overview</button>
          <button
            type="button"
            role="tab"
            class="tf-seg-btn ${this.activeTab === "conditions" ? "active" : ""}"
            aria-selected="${this.activeTab === "conditions"}"
            data-insp-tab="conditions"
          >Conditions${conditionCount > 0 ? ` (${conditionCount})` : ""}</button>
          <button
            type="button"
            role="tab"
            class="tf-seg-btn ${this.activeTab === "along" ? "active" : ""}"
            aria-selected="${this.activeTab === "along"}"
            data-insp-tab="along"
          >Along track${alongCount > 0 ? ` (${alongCount})` : ""}</button>
        </div>

        ${
          this.activeTab === "overview"
            ? overviewTabHtml
            : this.activeTab === "conditions"
              ? conditionsTabHtml
              : alongTabHtml
        }
      </div>
    `;

    this.bindDetailHandlers(detail, norm, activeClosures, hotspots, displayPois);
  }

  private renderOverviewTab(params: {
    norm: ReturnType<typeof normalizeTrackInspector>;
    detail: TrackInspectorDetail;
    gradeColor: string;
    factors: ReturnType<typeof formatBreakdownFactors>;
    overrides: string[];
    chart: ReturnType<typeof buildElevationProfileSvg>;
  }): string {
    const { norm, detail, gradeColor, factors, overrides, chart } = params;

    const factorsHtml =
      factors.length > 0
        ? factors
            .map(
              (f) => `
                <div class="tf-factor-row">
                  <div class="tf-factor-head">
                    <span class="tf-factor-label">${escapeHtml(f.label)}</span>
                    <span class="tf-factor-pts">${f.points} / ${f.maxPoints}</span>
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
                  `<div class="tf-override-badge">${uiIcon("alert", 13)}<span>${escapeHtml(formatOverrideLabel(ov))}</span></div>`
              )
              .join("")}
          </div>`
        : "";

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
                <div class="tf-clean-row">
                  <div class="tf-list-card-title">${escapeHtml(pk.name)}</div>
                  <div class="tf-list-card-sub">${escapeHtml(meta)}</div>
                </div>
              `;
            })
            .join("")
        : `<p class="tf-inspector-muted">Outside mapped CAPAD 2025 reserve boundaries.</p>`;

    return `
      <section class="tf-insp-block">
        <div class="tf-insp-sec-head">
          <h3>Elevation profile</h3>
          <div class="tf-elev-head-right">
            <span class="tf-insp-meta">${
              chart.steepZoneCount > 0
                ? `${chart.steepZoneCount} steep ≥14°`
                : "DEM AHD"
            }</span>
            ${
              norm.profile.length >= 2
                ? `<button type="button" class="tf-text-action-btn" data-role="expand-elevation" title="Open large interactive elevation & track profile window">${uiIcon("expand", 13)}<span>Expand</span></button>`
                : ""
            }
          </div>
        </div>
        <div class="tf-elev-chart-wrap ${norm.profile.length >= 2 ? "expandable" : ""}" ${norm.profile.length >= 2 ? 'data-role="expand-elevation-chart" title="Click to open large interactive elevation profile"' : ""}>
          ${chart.svgHtml}
        </div>
        ${norm.hasMultipleCourseSections ? `<p class="tf-inspector-muted">Distance combines mapped track sections. Check the map for junctions and gaps.</p>` : ""}
      </section>

      <section class="tf-insp-block">
        <div class="tf-insp-sec-head">
          <h3>Terrain difficulty breakdown</h3>
          <span class="tf-insp-meta">${escapeHtml(norm.breakdown?.dem_tier ?? "DEM baseline")}</span>
        </div>
        <div class="tf-factor-list">${factorsHtml}</div>
        ${overridesHtml}
      </section>

      <section class="tf-insp-block">
        <div class="tf-insp-sec-head">
          <h3>Park &amp; forest tenure</h3>
          <span class="tf-insp-meta">${norm.parks.length}</span>
        </div>
        <div class="tf-clean-list">${parksHtml}</div>
      </section>

      <div class="tf-insp-lineage">
        Source: ${escapeHtml(detail.primary_source ?? "canonical")}
        ${norm.breakdown?.dem_tier ? ` · DEM: ${escapeHtml(norm.breakdown.dem_tier)}` : ""}
        ${detail.segment_count ? ` · ${detail.segment_count} segments` : ""}
      </div>
    `;
  }

  private renderConditionsTab(params: {
    detail: TrackInspectorDetail;
    activeClosures: ClosureItem[];
    gateWarnings: ReturnType<typeof partitionTrackReports>["gateWarnings"];
    timelineReports: ReturnType<typeof partitionTrackReports>["timelineReports"];
    wetRisk: ReturnType<typeof formatWetRiskBanner>;
    liveBreakdown: TrackInspectorDetail["live_breakdown"];
    liveGrade: string | null;
    liveScore: number | null;
    sampleCount: number;
    confidence: ReturnType<typeof formatConfidenceBadge>;
    lastDriven: string;
    avgSpeedKmh: number | null;
    estDurationText: string;
    vehicleSplit: ReturnType<typeof normalizeVehicleClassSplit>;
    hotspots: NonNullable<TrackInspectorDetail["obstacle_hotspots"]>;
    liveReasons: string[];
    outlierClamped: number;
  }): string {
    const {
      detail,
      activeClosures,
      gateWarnings,
      timelineReports,
      wetRisk,
      liveBreakdown,
      liveGrade,
      liveScore,
      sampleCount,
      confidence,
      lastDriven,
      avgSpeedKmh,
      estDurationText,
      vehicleSplit,
      hotspots,
      liveReasons,
      outlierClamped,
    } = params;

    const closuresPanelHtml =
      activeClosures.length > 0
        ? `
          <section class="tf-insp-block">
            <div class="tf-insp-sec-head">
              <h3>Official closures &amp; alerts</h3>
              <span class="tf-insp-meta">${activeClosures.length}</span>
            </div>
            <div class="tf-card-stack">
              ${activeClosures
                .map((c, closureIdx) => {
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
                  const hasClosureId =
                    (c.closure_id !== undefined && c.closure_id !== null) ||
                    (c.id !== undefined && c.id !== null);
                  const cleanDesc = formatClosurePlainText(c.description);
                  return `
                    <div class="tf-closure-card tone-${tone}">
                      <div class="tf-closure-top">
                        <span class="tf-closure-type">${escapeHtml(typeLabel)}</span>
                        ${
                          c.is_stale
                            ? `<span class="tf-status-tag tone-warn">Stale cache</span>`
                            : ""
                        }
                      </div>
                      <div class="tf-closure-title">${escapeHtml(c.title)}</div>
                      ${
                        cleanDesc
                          ? `<p class="tf-closure-desc tf-clamp-3">${escapeHtml(cleanDesc)}</p>`
                          : ""
                      }
                      <div class="tf-closure-meta">
                        ${c.agency ? `<span>${escapeHtml(c.agency)}</span>` : ""}
                        ${dateSpan ? `<span>· ${escapeHtml(dateSpan)}</span>` : ""}
                        ${c.last_updated_at ? `<span>· Updated ${escapeHtml(updatedAgo)}</span>` : ""}
                      </div>
                      <div class="tf-closure-card-actions">
                        ${
                          hasClosureId
                            ? `<button type="button" class="tf-mini-btn" data-open-closure-idx="${closureIdx}">Full details</button>`
                            : ""
                        }
                        ${
                          safeUrl
                            ? `<a class="tf-closure-link" href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer"><span>Agency source</span>${uiIcon("externalLink", 12)}</a>`
                            : ""
                        }
                      </div>
                    </div>
                  `;
                })
                .join("")}
            </div>
          </section>
        `
        : "";

    const gateWarningsHtml =
      gateWarnings.length > 0
        ? `
          <section class="tf-insp-block">
            <div class="tf-insp-sec-head">
              <h3>Gate &amp; private access warnings</h3>
              <span class="tf-insp-meta">${gateWarnings.length}</span>
            </div>
            <div class="tf-card-stack">
              ${gateWarnings
                .map((r) => {
                  const typeLabel = formatReportTypeLabel(r.report_type);
                  const fresh = formatReportFreshness(
                    r.freshness_score,
                    r.is_stale,
                    r.effective_status ?? r.status
                  );
                  const links = resolveReportOsmLinks(r);
                  const when = formatRelativeTimestamp(
                    r.last_confirmed_at ?? r.created_at
                  );
                  return `
                    <div class="tf-closure-card tone-critical">
                      <div class="tf-closure-top">
                        <span class="tf-closure-type">${escapeHtml(typeLabel)}</span>
                        <span class="tf-fresh-badge tone-${fresh.tone}">${escapeHtml(fresh.label)}</span>
                      </div>
                      ${r.title ? `<div class="tf-closure-title">${escapeHtml(r.title)}</div>` : ""}
                      <p class="tf-closure-desc">${escapeHtml(r.notes)}</p>
                      <div class="tf-closure-meta">
                        ${r.author_display_name ? `<span>By ${escapeHtml(r.author_display_name)}</span>` : ""}
                        ${r.vehicle_class ? `<span>· ${escapeHtml(r.vehicle_class)}</span>` : ""}
                        <span>· ${r.confirm_count} confirmed / ${r.resolved_count} resolved</span>
                        <span>· ${escapeHtml(when)}</span>
                      </div>
                      <div class="tf-osm-links">
                        <a class="tf-closure-link" href="${escapeHtml(links.osmEditUrl)}" target="_blank" rel="noopener noreferrer"><span>Suggest OSM access fix</span>${uiIcon("externalLink", 12)}</a>
                        <a class="tf-closure-link" href="${escapeHtml(links.osmNoteUrl)}" target="_blank" rel="noopener noreferrer"><span>Add OSM note</span>${uiIcon("externalLink", 12)}</a>
                      </div>
                    </div>
                  `;
                })
                .join("")}
            </div>
          </section>
        `
        : "";

    const defaultCoords = this.resolveDefaultReportCoords(detail);
    const reportFormHtml = this.showReportForm
      ? `
        <form class="tf-form tf-report-form" data-role="report-form">
          ${
            this.reportError
              ? `<div class="tf-inspector-error">${escapeHtml(this.reportError)}</div>`
              : ""
          }
          <div class="tf-insp-sec-head">
            <h3>Report trail condition</h3>
            <button type="button" class="tf-text-action-btn" data-role="pick-report-loc">${uiIcon("target", 13)}<span>Pick on map</span></button>
          </div>
          <div class="tf-pin-coords mono">
            ${defaultCoords.lat.toFixed(5)}, ${defaultCoords.lon.toFixed(5)}
          </div>
          <label class="tf-field">
            <span class="tf-label">Condition or hazard type</span>
            <select class="tf-select" name="report_type">
              ${CONDITION_REPORT_TYPES.map(
                (t) =>
                  `<option value="${escapeHtml(t.value)}">${escapeHtml(t.label)}${t.isGateWarning ? " (Gate/Access)" : ""}</option>`
              ).join("")}
            </select>
          </label>
          <label class="tf-field">
            <span class="tf-label">Details &amp; advice</span>
            <textarea class="tf-textarea" name="notes" rows="3" required maxlength="500" placeholder="Describe obstacle depth, detour, locked gate, or clear conditions…"></textarea>
          </label>
          <label class="tf-field">
            <span class="tf-label">Photo attachment (optional, ≤ 5 MB)</span>
            <input type="file" class="tf-file-input" name="photo" accept="image/jpeg,image/png,image/webp" />
          </label>
          <div class="tf-btn-row">
            <button type="submit" class="tf-btn-primary" ${this.reportSubmitting ? "disabled" : ""}>Submit report</button>
            <button type="button" class="tf-btn-secondary" data-role="cancel-report">Cancel</button>
          </div>
        </form>
      `
      : "";

    const timelineHtml =
      timelineReports.length > 0
        ? timelineReports
            .map((r) => {
              const rid = getReportId(r);
              const color = getReportTypeColor(r.report_type);
              const typeLabel = formatReportTypeLabel(r.report_type);
              const fresh = formatReportFreshness(
                r.freshness_score,
                r.is_stale,
                r.effective_status ?? r.status
              );
              const when = formatRelativeTimestamp(
                r.last_confirmed_at ?? r.created_at
              );
              const links = resolveReportOsmLinks(r);
              const safePhotoUrl = sanitizeReportPhotoUrl(r.photo_url);
              const photoHtml = safePhotoUrl
                ? `<a class="tf-report-photo-link" href="${escapeHtml(safePhotoUrl)}" target="_blank" rel="noopener noreferrer"><img class="tf-report-photo" src="${escapeHtml(safePhotoUrl)}" alt="${escapeHtml(typeLabel)} attachment" loading="lazy" /></a>`
                : "";
              return `
                <div class="tf-report-card" style="border-left-color:${color}">
                  <div class="tf-report-head">
                    <span class="tf-report-type" style="color:${color}">${escapeHtml(typeLabel)}</span>
                    <span class="tf-fresh-badge tone-${fresh.tone}">${escapeHtml(fresh.label)}</span>
                  </div>
                  ${r.title ? `<div class="tf-closure-title">${escapeHtml(r.title)}</div>` : ""}
                  <p class="tf-closure-desc">${escapeHtml(r.notes)}</p>
                  ${photoHtml}
                  <div class="tf-closure-meta">
                    ${r.author_display_name ? `<span>${escapeHtml(r.author_display_name)}</span>` : ""}
                    ${r.vehicle_class ? `<span>· ${escapeHtml(r.vehicle_class)}</span>` : ""}
                    <span>· ${escapeHtml(when)}</span>
                  </div>
                  <div class="tf-report-actions">
                    <button type="button" class="tf-mini-btn" data-vote-report="${escapeHtml(rid)}" data-vote-kind="confirm">${uiIcon("check", 12)}<span>Confirm (${r.confirm_count})</span></button>
                    <button type="button" class="tf-mini-btn" data-vote-report="${escapeHtml(rid)}" data-vote-kind="resolved">Resolved (${r.resolved_count})</button>
                    <a class="tf-osm-inline" href="${escapeHtml(links.osmNoteUrl)}" target="_blank" rel="noopener noreferrer" title="Report map/access fix to OpenStreetMap">OSM fix</a>
                  </div>
                </div>
              `;
            })
            .join("")
        : `<p class="tf-inspector-muted">No recent trail condition reports yet. Been through recently? Share an update.</p>`;

    const wetBannerHtml = wetRisk
      ? `
        <div class="tf-wet-banner tone-${wetRisk.tone}">
          <div class="tf-wet-head">
            <span class="tf-wet-title">${uiIcon("rain", 14)}<span>72h rainfall · ${escapeHtml(wetRisk.label)}</span></span>
            <span class="tf-wet-totals">${escapeHtml(wetRisk.rain72hText)} (72h) · ${escapeHtml(wetRisk.rain24hText)} (24h)</span>
          </div>
          ${
            wetRisk.warning
              ? `<p class="tf-wet-warning">${escapeHtml(wetRisk.warning)}</p>`
              : ""
          }
          <div class="tf-wet-meta">
            <span>${escapeHtml(wetRisk.adjustmentText ?? "No wet penalty")}</span>
            ${wetRisk.isStale ? `<span>· Cached weather</span>` : ""}
            <span class="tf-wet-attrib">${escapeHtml(wetRisk.attribution)}</span>
          </div>
        </div>
      `
      : "";

    const vehicleSplitHtml =
      vehicleSplit.length > 0
        ? `
          <div class="tf-split-block">
            <div class="tf-subhead">Traffic by vehicle class</div>
            <div class="tf-split-bar" role="img" aria-label="Traffic split by vehicle capability class">
              ${vehicleSplit
                .filter((s) => s.pct > 0)
                .map(
                  (s) =>
                    `<span class="tf-split-seg" style="width:${s.pct}%;background:${s.color}" title="${escapeHtml(s.shortLabel)}: ${s.count} (${s.pct}%)"></span>`
                )
                .join("")}
            </div>
            <div class="tf-split-legend">
              ${vehicleSplit
                .map(
                  (s) => `
                    <span class="tf-split-chip ${s.count === 0 ? "dimmed" : ""}">
                      <span class="tf-diff-swatch" style="background:${s.color}"></span>
                      <span>${escapeHtml(s.shortLabel)}</span>
                      <span>${s.pct}% (${s.count})</span>
                    </span>
                  `
                )
                .join("")}
            </div>
          </div>
        `
        : "";

    const hotspotsHtml =
      hotspots.length > 0
        ? `
          <div class="tf-hotspots-block">
            <div class="tf-subhead">Obstacle hotspots (${hotspots.length})</div>
            <div class="tf-card-stack">
              ${hotspots
                .map((h, idx) => {
                  const kmText =
                    typeof h.dist_m === "number"
                      ? `${(h.dist_m / 1000).toFixed(2)} km`
                      : h.segment_id;
                  const speedText =
                    typeof h.avg_speed_kmh === "number"
                      ? `${h.avg_speed_kmh.toFixed(1)} km/h`
                      : null;
                  const roughVal = h.roughness_rms ?? h.median_roughness_rms;
                  const roughText =
                    typeof roughVal === "number"
                      ? `${roughVal.toFixed(2)}g RMS`
                      : null;
                  const reasonsList = (h.reasons ?? h.obstacle_reasons ?? [])
                    .map((r) => formatHotspotReason(r))
                    .join(" · ");
                  const clickable =
                    typeof h.lat === "number" && typeof h.lon === "number";
                  return `
                    <button
                      type="button"
                      class="tf-hotspot-card"
                      ${clickable ? `data-hotspot-idx="${idx}"` : "disabled"}
                    >
                      <div class="tf-list-card-head">
                        <span class="tf-list-card-title">${uiIcon("zap", 13)}<span>${escapeHtml(kmText)}</span></span>
                        <span class="tf-list-card-sub">${escapeHtml([speedText, roughText].filter(Boolean).join(" · ") || "Hotspot")}</span>
                      </div>
                      ${
                        reasonsList
                          ? `<div class="tf-list-card-sub">${escapeHtml(reasonsList)}</div>`
                          : ""
                      }
                    </button>
                  `;
                })
                .join("")}
            </div>
          </div>
        `
        : "";

    const liveGradeColor = liveGrade ? getGradeColor(liveGrade) : "var(--text-muted)";

    return `
      ${closuresPanelHtml}
      ${gateWarningsHtml}

      <section class="tf-insp-block">
        <div class="tf-insp-sec-head">
          <h3>Trail condition reports</h3>
          <button type="button" class="tf-text-action-btn" data-role="open-report-form">${uiIcon("plus", 13)}<span>Add report</span></button>
        </div>
        ${reportFormHtml}
        <div class="tf-card-stack">${timelineHtml}</div>
      </section>

      <section class="tf-insp-block">
        <div class="tf-insp-sec-head">
          <h3>Live telemetry &amp; rainfall</h3>
          <span class="tf-insp-meta">${escapeHtml(confidence.label)}</span>
        </div>

        ${wetBannerHtml}

        <div class="tf-live-hero">
          <div class="tf-live-grade-box">
            <span class="tf-metric-label">Live grade</span>
            <div class="tf-live-grade-val" style="color:${liveGradeColor}">
              ${escapeHtml(liveGrade ?? "Baseline only")}
              ${
                liveScore !== null
                  ? `<span class="tf-live-score">(${Math.round(liveScore)}/100)</span>`
                  : ""
              }
            </div>
          </div>
          <span class="tf-conf-pill tone-${confidence.tone}" title="${escapeHtml(confidence.detail)}">
            ${escapeHtml(confidence.label)}
          </span>
        </div>

        <div class="tf-metric-grid tf-metric-grid-2">
          <div class="tf-metric-cell">
            <span class="tf-metric-label">Traversals</span>
            <span class="tf-metric-val">${sampleCount}</span>
          </div>
          <div class="tf-metric-cell">
            <span class="tf-metric-label">Last driven</span>
            <span class="tf-metric-val">${escapeHtml(lastDriven)}</span>
          </div>
          <div class="tf-metric-cell">
            <span class="tf-metric-label">Avg speed</span>
            <span class="tf-metric-val">${avgSpeedKmh !== null ? `${avgSpeedKmh.toFixed(1)} km/h` : "-"}</span>
          </div>
          <div class="tf-metric-cell">
            <span class="tf-metric-label">Est. duration</span>
            <span class="tf-metric-val">${escapeHtml(estDurationText)}</span>
          </div>
        </div>

        ${
          outlierClamped > 0
            ? `<div class="tf-telemetry-note">${uiIcon("shield", 13)}<span>Outlier guard: ${outlierClamped} extreme traversal${outlierClamped === 1 ? "" : "s"} clamped and blended with terrain baseline.</span></div>`
            : confidence.level === "low" || confidence.level === "none"
              ? `<div class="tf-telemetry-note">${escapeHtml(confidence.detail)}.</div>`
              : ""
        }

        ${vehicleSplitHtml}
        ${hotspotsHtml}

        ${
          liveReasons.length > 0
            ? `<ul class="tf-reason-list">
                ${liveReasons.map((r) => `<li>${escapeHtml(r)}</li>`).join("")}
              </ul>`
            : ""
        }
        ${liveBreakdown?.version ? "" : ""}
      </section>
    `;
  }

  private renderAlongTab(
    crossings: readonly WaterwayCrossingItem[],
    nearbyPois: readonly PoiItem[]
  ): string {
    const crossingsHtml =
      crossings.length > 0
        ? crossings
            .map((c) => {
              const name = c.name || "Unnamed watercourse";
              const type = (c.waterway_type ?? "stream").replace(/_/g, " ");
              const flowTag = c.perennial ? "Perennial" : "Intermittent";
              const bridgeTag = c.bridged ? "Bridged" : "Unbridged ford";
              const distText =
                typeof c.dist_m === "number"
                  ? `${(c.dist_m / 1000).toFixed(2)} km`
                  : "";
              return `
                <div class="tf-clean-row">
                  <div class="tf-list-card-head">
                    <span class="tf-list-card-title">${escapeHtml(name)}</span>
                    <span class="tf-list-card-sub">${escapeHtml(distText)}</span>
                  </div>
                  <div class="tf-list-card-sub">
                    <span>${escapeHtml(type)}</span>
                    <span>· ${escapeHtml(flowTag)}</span>
                    <span class="${c.bridged ? "tf-accent-emerald" : "tf-accent-amber"}">· ${escapeHtml(bridgeTag)}</span>
                  </div>
                </div>
              `;
            })
            .join("")
        : `<p class="tf-inspector-muted">No recorded waterway crossings along this track.</p>`;

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
        : `<p class="tf-inspector-muted">No camps, huts, or POIs recorded within 1,500 m.</p>`;

    return `
      <section class="tf-insp-block">
        <div class="tf-insp-sec-head">
          <h3>Waterway crossings</h3>
          <span class="tf-insp-meta">${crossings.length}</span>
        </div>
        <div class="tf-clean-list">${crossingsHtml}</div>
      </section>

      <section class="tf-insp-block">
        <div class="tf-insp-sec-head">
          <h3>Nearby camps, huts &amp; POIs</h3>
          <span class="tf-insp-meta">${nearbyPois.length}</span>
        </div>
        <div class="tf-poi-list">${poisHtml}</div>
      </section>
    `;
  }

  private bindDetailHandlers(
    detail: TrackInspectorDetail,
    norm: ReturnType<typeof normalizeTrackInspector>,
    activeClosures: ClosureItem[],
    hotspots: NonNullable<TrackInspectorDetail["obstacle_hotspots"]>,
    nearbyPois: readonly PoiItem[]
  ): void {
    this.bindClose();

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-insp-tab]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const next = btn.dataset.inspTab as TrackInspectorTab | undefined;
          if (next === "overview" || next === "conditions" || next === "along") {
            this.activeTab = next;
            this.exportMenuOpen = false;
            this.renderDetail(detail);
          }
        });
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-jump-tab='conditions']")
      ?.addEventListener("click", () => {
        this.activeTab = "conditions";
        this.renderDetail(detail);
      });

    const exportToggle = this.root.querySelector<HTMLButtonElement>(
      "[data-role='toggle-export-menu']"
    );
    const exportMenu = this.root.querySelector<HTMLElement>(
      "[data-role='export-menu']"
    );
    exportToggle?.addEventListener("click", (e) => {
      e.stopPropagation();
      this.exportMenuOpen = !this.exportMenuOpen;
      exportMenu?.classList.toggle("hidden", !this.exportMenuOpen);
      exportToggle.classList.toggle("active", this.exportMenuOpen);
      exportToggle.setAttribute("aria-expanded", String(this.exportMenuOpen));
    });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='expand-elevation']")
      ?.addEventListener("click", () => this.openElevationModal());

    this.root
      .querySelector<HTMLElement>("[data-role='expand-elevation-chart']")
      ?.addEventListener("click", () => this.openElevationModal());

    this.root
      .querySelector<HTMLButtonElement>("[data-role='change-rig']")
      ?.addEventListener("click", () => this.callbacks.onOpenRigPanel());

    this.root
      .querySelector<HTMLButtonElement>("[data-role='toggle-route']")
      ?.addEventListener("click", () => {
        this.callbacks.onToggleTrackInRoute(
          detail.track_id,
          norm.name,
          Boolean(this.context.isInRoute)
        );
      });

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
          this.exportMenuOpen = false;
          exportMenu?.classList.add("hidden");
          this.callbacks.onExportTrack(detail.track_id, fmt);
        });
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='share-track']")
      ?.addEventListener("click", () => {
        this.callbacks.onShareTrack?.(detail.track_id);
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='open-report-form']")
      ?.addEventListener("click", () => {
        if (!this.context.isAuthenticated) {
          this.callbacks.onRequireSignIn();
          return;
        }
        this.showReportForm = !this.showReportForm;
        this.reportError = null;
        this.renderDetail(detail);
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='cancel-report']")
      ?.addEventListener("click", () => {
        this.showReportForm = false;
        this.reportError = null;
        this.renderDetail(detail);
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='pick-report-loc']")
      ?.addEventListener("click", () => {
        this.callbacks.onPickReportLocation();
      });

    this.root
      .querySelector<HTMLFormElement>("[data-role='report-form']")
      ?.addEventListener("submit", (e) => {
        e.preventDefault();
        const form = e.currentTarget as HTMLFormElement;
        const fd = new FormData(form);
        const reportType = String(
          fd.get("report_type") ?? "fallen_tree"
        ) as ConditionReportType;
        const notes = String(fd.get("notes") ?? "").trim();
        const photoInput = form.querySelector<HTMLInputElement>(
          "input[name='photo']"
        );
        const photo = photoInput?.files?.[0] ?? null;

        if (!notes) {
          this.reportError = "Please describe the trail condition.";
          this.renderDetail(detail);
          return;
        }

        const coords = this.resolveDefaultReportCoords(detail);
        const epoch = this.reportEpoch;
        this.reportSubmitting = true;
        this.reportError = null;
        this.renderDetail(detail);

        void this.callbacks
          .onSubmitReport({
            report_type: reportType,
            lon: coords.lon,
            lat: coords.lat,
            notes,
            track_id: detail.track_id,
            vehicle_class: this.context.activeVehicleClass,
            photo,
          })
          .then(() => {
            if (epoch !== this.reportEpoch) return;
            this.showReportForm = false;
            this.reportError = null;
          })
          .catch((err) => {
            if (epoch !== this.reportEpoch) return;
            this.reportError =
              err instanceof Error ? err.message : "Failed to submit report";
          })
          .finally(() => {
            if (epoch !== this.reportEpoch) return;
            this.reportSubmitting = false;
            if (this.currentDetail) {
              this.renderDetail(this.currentDetail);
            }
          });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-vote-report]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          if (!this.context.isAuthenticated) {
            this.callbacks.onRequireSignIn();
            return;
          }
          const rid = btn.dataset.voteReport!;
          const kind = btn.dataset.voteKind as ReportVoteKind;
          void this.callbacks.onVoteReport(rid, kind);
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
      .querySelectorAll<HTMLButtonElement>("[data-hotspot-idx]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const idx = Number(btn.dataset.hotspotIdx);
          const h = hotspots[idx];
          if (h && typeof h.lat === "number" && typeof h.lon === "number") {
            this.callbacks.onHotspotSelect?.(
              h.lat,
              h.lon,
              `${norm.name} obstacle hotspot`
            );
          }
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-open-closure-idx]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const idx = Number(btn.dataset.openClosureIdx);
          const item = activeClosures[idx];
          if (item) this.callbacks.onOpenClosure?.(item);
        });
      });

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
