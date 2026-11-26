import {
  formatClosureDateRange,
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

export { sanitizeReportPhotoUrl };
import type {
  BBoxTuple,
  ConditionReportCreateInput,
  ConditionReportType,
  PoiItem,
  ReportVoteKind,
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
  isInRoute?: boolean;
  isAuthenticated: boolean;
  pickedReportCoords?: { lat: number; lon: number } | null;
}

export interface TrackInspectorCallbacks {
  onClose: () => void;
  onZoomToTrack: (bbox: BBoxTuple) => void;
  onPoiSelect: (poi: PoiItem) => void;
  onHotspotSelect?: (lat: number, lon: number, label: string) => void;
  onOpenRigPanel: () => void;
  onRequireSignIn: () => void;
  onToggleFavorite: (trackId: string, currentlyFavorite: boolean) => void;
  onToggleTrackInRoute: (
    trackId: string,
    trackName: string,
    currentlyInRoute: boolean
  ) => void;
  onExportTrack: (trackId: string, format: "gpx" | "geojson") => void;
  onPickReportLocation: () => void;
  onSubmitReport: (input: ConditionReportCreateInput) => Promise<void>;
  onVoteReport: (
    reportId: string,
    vote: ReportVoteKind
  ) => Promise<void>;
}

export class TrackInspectorDrawer {
  private readonly root: HTMLElement;
  private readonly callbacks: TrackInspectorCallbacks;
  private currentDetail: TrackInspectorDetail | null = null;
  private showReportForm = false;
  private reportSubmitting = false;
  private reportError: string | null = null;
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
    if (patch.pickedReportCoords) {
      this.showReportForm = true;
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
    this.showReportForm = false;
    this.reportError = null;
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
        <div class="tf-inspector-loading">Fetching canonical segments, closures, condition reports, rig suitability &amp; elevation profile…</div>
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
    this.showReportForm = false;
    this.reportError = null;
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
        <p class="tf-inspector-muted">Click any graded track on the map or search by track name to inspect elevation profile, official closures, community condition reports &amp; gate warnings, rig suitability, explainable difficulty breakdown, creek crossings, nearby POIs, and CAPAD park tenure.</p>
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

    // Slice 08 Community Condition Reports & Distinct Gate Warnings
    const { gateWarnings, timelineReports } = partitionTrackReports(
      detail.condition_reports ?? [],
      detail.community_gate_warnings ?? null
    );

    const badgesHtml = [
      closureBadge.tone !== "open"
        ? `<span class="tf-pill tf-pill-${closureBadge.tone === "critical" ? "red" : closureBadge.tone === "warning" ? "amber" : "ice"}">⚠ ${escapeHtml(closureBadge.label)}</span>`
        : "",
      gateWarnings.length > 0
        ? `<span class="tf-pill tf-pill-red">⛔ ${gateWarnings.length} Gate / Access Report${gateWarnings.length === 1 ? "" : "s"}</span>`
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

    // Slice 05 Personalized Rig Suitability Card
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
              <h3>⚠ Official Closures &amp; Alerts</h3>
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

    // Slice 08 Distinct Community Gate / Private Property Warnings Callout
    const gateWarningsHtml =
      gateWarnings.length > 0
        ? `
          <section class="tf-insp-section tf-gate-warnings-panel">
            <div class="tf-insp-sec-head">
              <h3>⛔ Community Gate &amp; Private Access Warnings</h3>
              <span class="mono tf-insp-meta">${gateWarnings.length}</span>
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
                        <span>· ✓${r.confirm_count} / ✔${r.resolved_count}</span>
                        <span>· ${escapeHtml(when)}</span>
                      </div>
                      <div class="tf-osm-links">
                        <a class="tf-closure-link" href="${escapeHtml(links.osmEditUrl)}" target="_blank" rel="noopener noreferrer">Suggest OSM Access Fix ↗</a>
                        <a class="tf-closure-link" href="${escapeHtml(links.osmNoteUrl)}" target="_blank" rel="noopener noreferrer">Add OSM Note ↗</a>
                      </div>
                    </div>
                  `;
                })
                .join("")}
            </div>
          </section>
        `
        : "";

    // Slice 08 Trail Condition Timeline & Report Submission Form
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
            <h3>Report Trail Condition</h3>
            <button type="button" class="tf-mini-btn" data-role="pick-report-loc">⌖ Pick on Map</button>
          </div>
          <div class="tf-pin-coords mono">
            Location: ${defaultCoords.lat.toFixed(5)}, ${defaultCoords.lon.toFixed(5)}
          </div>
          <label class="tf-field">
            <span class="tf-label">Condition / Hazard Type</span>
            <select class="tf-select" name="report_type">
              ${CONDITION_REPORT_TYPES.map(
                (t) =>
                  `<option value="${escapeHtml(t.value)}">${escapeHtml(t.label)}${t.isGateWarning ? " (Gate/Access Warning)" : ""}</option>`
              ).join("")}
            </select>
          </label>
          <label class="tf-field">
            <span class="tf-label">Details &amp; Advice</span>
            <textarea class="tf-textarea" name="notes" rows="3" required maxlength="500" placeholder="Describe obstacle depth, detour, locked padlock, or clear conditions…"></textarea>
          </label>
          <label class="tf-field">
            <span class="tf-label">Photo Attachment (optional, JPEG/PNG/WebP ≤ 5 MB)</span>
            <input type="file" class="tf-file-input" name="photo" accept="image/jpeg,image/png,image/webp" />
          </label>
          <div class="tf-btn-row">
            <button type="submit" class="tf-btn-primary" ${this.reportSubmitting ? "disabled" : ""}>Submit Report</button>
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
                    <button type="button" class="tf-mini-btn" data-vote-report="${escapeHtml(rid)}" data-vote-kind="confirm">✓ Confirm (${r.confirm_count})</button>
                    <button type="button" class="tf-mini-btn" data-vote-report="${escapeHtml(rid)}" data-vote-kind="resolved">✔ Resolved (${r.resolved_count})</button>
                    <a class="tf-osm-inline" href="${escapeHtml(links.osmNoteUrl)}" target="_blank" rel="noopener noreferrer" title="Report map/access fix to OpenStreetMap">OSM Fix ↗</a>
                  </div>
                </div>
              `;
            })
            .join("")
        : `<p class="tf-inspector-muted">No recent trail condition reports yet. Been through recently? Share an update above.</p>`;

    // Slice 07 Live Crowdsourced Evidence & 72h Rainfall Context
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

    const wetBannerHtml = wetRisk
      ? `
        <div class="tf-wet-banner tone-${wetRisk.tone}">
          <div class="tf-wet-head">
            <span class="tf-wet-title">🌧 72h Rainfall · ${escapeHtml(wetRisk.label)}</span>
            <span class="mono tf-wet-totals">${escapeHtml(wetRisk.rain72hText)} (72h) · ${escapeHtml(wetRisk.rain24hText)} (24h)</span>
          </div>
          ${
            wetRisk.warning
              ? `<p class="tf-wet-warning">${escapeHtml(wetRisk.warning)}</p>`
              : ""
          }
          <div class="tf-wet-meta">
            ${
              wetRisk.adjustmentText
                ? `<span class="tf-mini-tag amber">${escapeHtml(wetRisk.adjustmentText)}</span>`
                : `<span class="tf-mini-tag">No wet penalty</span>`
            }
            ${
              wetRisk.isStale
                ? `<span class="tf-stale-badge">CACHED WEATHER</span>`
                : ""
            }
            <span class="tf-wet-attrib">${escapeHtml(wetRisk.attribution)}</span>
          </div>
        </div>
      `
      : "";

    const vehicleSplitHtml =
      vehicleSplit.length > 0
        ? `
          <div class="tf-split-block">
            <div class="tf-subhead">Vehicle Capability Traffic Split</div>
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
                      <span class="mono">${s.pct}% (${s.count})</span>
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
            <div class="tf-subhead">Rough / Slow Obstacle Hotspots (${hotspots.length})</div>
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
                        <span class="tf-list-card-title">⚡ ${escapeHtml(kmText)}</span>
                        <span class="mono tf-list-card-sub">${escapeHtml([speedText, roughText].filter(Boolean).join(" · ") || "Hotspot")}</span>
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

    const liveGradeColor = liveGrade ? getGradeColor(liveGrade) : "#94A3B8";
    const liveSectionHtml = `
      <section class="tf-insp-section tf-live-section">
        <div class="tf-insp-sec-head">
          <h3>Live Crowdsourced Evidence</h3>
          <span class="mono tf-insp-meta">${escapeHtml(liveBreakdown?.version ?? "live_v1")}</span>
        </div>

        ${wetBannerHtml}

        <div class="tf-live-hero">
          <div class="tf-live-grade-box">
            <span class="tf-live-kicker">Live Grade</span>
            <div class="tf-live-grade-val" style="color:${liveGradeColor}">
              ${escapeHtml(liveGrade ?? "Baseline Only")}
              ${
                liveScore !== null
                  ? `<span class="mono tf-live-score">(${Math.round(liveScore)}/100)</span>`
                  : ""
              }
            </div>
          </div>
          <span class="tf-conf-pill tone-${confidence.tone}" title="${escapeHtml(confidence.detail)}">
            ${escapeHtml(confidence.label)}
          </span>
        </div>

        <div class="tf-live-stats-grid">
          <div class="tf-stat-card">
            <span class="tf-stat-label">Traversals</span>
            <span class="mono tf-stat-val">${sampleCount}</span>
          </div>
          <div class="tf-stat-card">
            <span class="tf-stat-label">Last Driven</span>
            <span class="mono tf-stat-val tf-stat-sm">${escapeHtml(lastDriven)}</span>
          </div>
          <div class="tf-stat-card">
            <span class="tf-stat-label">Avg Speed</span>
            <span class="mono tf-stat-val">${avgSpeedKmh !== null ? `${avgSpeedKmh.toFixed(1)} km/h` : "—"}</span>
          </div>
          <div class="tf-stat-card">
            <span class="tf-stat-label">Est. Duration</span>
            <span class="mono tf-stat-val">${escapeHtml(estDurationText)}</span>
          </div>
        </div>

        ${
          outlierClamped > 0
            ? `<div class="tf-telemetry-note">🛡 Outlier guard: ${outlierClamped} extreme traversal sample${outlierClamped === 1 ? "" : "s"} clamped &amp; blended with baseline terrain prior.</div>`
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
      </section>
    `;

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
            class="tf-action-btn ${this.context.isInRoute ? "in-route" : ""}"
            data-role="toggle-route"
            title="${this.context.isInRoute ? "Track is in active route — click to open Route Planner" : "Add this track to the active multi-track route"}"
          >${this.context.isInRoute ? "✓ In Route" : "+ Route"}</button>
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
        ${gateWarningsHtml}

        <div class="tf-grade-hero" style="border-color:${gradeColor}">
          <div class="tf-grade-hero-left">
            <span class="tf-grade-swatch" style="background:${gradeColor}"></span>
            <div>
              <div class="tf-grade-name" style="color:${gradeColor}">${escapeHtml(norm.grade)}</div>
              <div class="tf-grade-sub">${
                liveGrade && sampleCount > 0
                  ? `Effective Grade · Baseline + ${sampleCount} Live Traversals`
                  : "Day-1 Deterministic Terrain Baseline"
              }</div>
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
            <h3>Trail Condition Timeline</h3>
            <button type="button" class="tf-mini-btn" data-role="open-report-form">+ Report</button>
          </div>
          ${reportFormHtml}
          <div class="tf-card-stack">${timelineHtml}</div>
        </section>

        <section class="tf-insp-section">
          <div class="tf-insp-sec-head">
            <h3>Static Terrain Baseline</h3>
            <span class="mono tf-insp-meta">${escapeHtml(norm.breakdown?.formula_version ?? "baseline_v1")}</span>
          </div>
          <div class="tf-factor-list">${factorsHtml}</div>
          ${overridesHtml}
          ${reasonsHtml}
        </section>

        ${liveSectionHtml}

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
          this.callbacks.onExportTrack(detail.track_id, fmt);
        });
      });

    // Slice 08 Report form & voting handlers
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
            this.showReportForm = false;
            this.reportError = null;
          })
          .catch((err) => {
            this.reportError =
              err instanceof Error ? err.message : "Failed to submit report";
          })
          .finally(() => {
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
