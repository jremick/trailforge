import { renderSheetHandleMarkup } from "../bottomSheet";
import {
  formatClosureDateRange,
  formatClosureTypeLabel,
  getClosureStatusBadge,
  getClosureTone,
} from "../closuresHelpers";
import { formatDurationMinutes } from "../gradingHelpers";
import { uiIcon } from "../icons";
import { escapeHtml, getGradeColor, resolvePoiIconName } from "../presets";
import { formatReportFreshness, formatReportTypeLabel } from "../reportsHelpers";
import {
  LEG_GAP_WARNING_M,
  MAX_ROUTE_LEGS,
  buildRouteElevationProfileSvg,
  extractAnalysisBBox,
  formatGridForgeCli,
  formatRouteSafetyBadge,
  getSavedRouteId,
} from "../routeHelpers";
import type {
  BBoxTuple,
  PlannedRouteAnalysis,
  PoiItem,
  RouteWaypointInput,
  SavedRouteItem,
  VehicleCapabilityClass,
} from "../types";
import {
  computeClientSuitability,
  getSuitabilityTone,
} from "../vehicleHelpers";

export interface RoutePlannerState {
  routeName: string;
  routeNotes: string;
  activeSavedRouteId: string | null;
  trackIds: string[];
  waypoints: RouteWaypointInput[];
  analysis: PlannedRouteAnalysis | null;
  loading: boolean;
  error: string | null;
  planModeActive: boolean;
  exporting: "gpx" | "geojson" | null;
  saving: boolean;
  savedRoutes: SavedRouteItem[];
  savedRoutesLoading: boolean;
  isAuthenticated: boolean;
  activeVehicleClass: VehicleCapabilityClass;
  activeVehicleName: string | null;
  snowLineElevationM: number;
}

export interface RoutePlannerCallbacks {
  onClose: () => void;
  onTogglePlanMode: (active: boolean) => void;
  onRouteNameChange: (name: string) => void;
  onRouteNotesChange: (notes: string) => void;
  onMoveLegUp: (index: number) => void;
  onMoveLegDown: (index: number) => void;
  onRemoveLeg: (index: number) => void;
  onClearRoute: () => void;
  onInspectTrack: (trackId: string) => void;
  onZoomToRoute: (bbox: BBoxTuple) => void;
  onPoiSelect: (poi: PoiItem) => void;
  onOpenRigPanel: () => void;
  onOpenSignIn: () => void;
  onExportRoute: (format: "gpx" | "geojson") => Promise<void>;
  onSaveRoute: (asNew: boolean) => Promise<void>;
  onLoadSavedRoute: (routeId: string) => Promise<void>;
  onDeleteSavedRoute: (routeId: string) => Promise<void>;
  onExportSavedRoute: (
    routeId: string,
    format: "gpx" | "geojson"
  ) => Promise<void>;
  onCopyGridForgeCli: (cli: string) => Promise<void>;
}

function formatGapDistance(gapM: number): string {
  if (gapM >= 1000) return `${(gapM / 1000).toFixed(2)} km`;
  return `${Math.round(gapM)} m`;
}

export class RoutePlannerDrawer {
  private readonly root: HTMLElement;
  private readonly callbacks: RoutePlannerCallbacks;
  private state: RoutePlannerState;
  private isOpen = false;
  private gridForgeExpanded = false;
  private copiedCliTimer: number | null = null;
  private cliCopied = false;

  constructor(
    container: HTMLElement,
    initialState: RoutePlannerState,
    callbacks: RoutePlannerCallbacks
  ) {
    this.root = container;
    this.state = initialState;
    this.callbacks = callbacks;
    this.root.className = "tf-drawer tf-account-panel tf-route-panel";
    this.root.setAttribute("aria-label", "Interactive Multi-Track Route Planner");
    this.root.setAttribute("aria-hidden", "true");
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

  updateState(patch: Partial<RoutePlannerState>): void {
    this.state = { ...this.state, ...patch };
    if (this.isOpen) {
      this.render();
    }
  }

  private render(): void {
    const {
      routeName,
      routeNotes,
      activeSavedRouteId,
      trackIds,
      analysis,
      loading,
      error,
      planModeActive,
      exporting,
      saving,
      savedRoutes,
      savedRoutesLoading,
      isAuthenticated,
      activeVehicleClass,
      activeVehicleName,
      snowLineElevationM,
    } = this.state;

    const legCount = trackIds.length;
    const bbox = extractAnalysisBBox(analysis);
    const displayName =
      routeName.trim() || analysis?.name || "Untitled 4WD Route";

    const planModeHtml = `
      <div class="tf-route-mode-box ${planModeActive ? "active" : ""}">
        <div class="tf-route-mode-top">
          <button
            type="button"
            class="tf-route-mode-btn ${planModeActive ? "active" : ""}"
            data-role="toggle-plan-mode"
          >${
            planModeActive
              ? `● Click-to-Chain Active (${legCount} leg${legCount === 1 ? "" : "s"}) · Stop`
              : `+ Click Tracks on Map to Chain`
          }</button>
          ${
            bbox
              ? `<button type="button" class="tf-mini-btn" data-role="zoom-route" title="Zoom map to full route">⌖ Zoom</button>`
              : ""
          }
          ${
            legCount > 0
              ? `<button type="button" class="tf-mini-btn danger" data-role="clear-route" title="Clear all route legs">Clear</button>`
              : ""
          }
        </div>
        ${
          planModeActive
            ? `<div class="tf-route-mode-hint">Click tracks on the map to append to your route · ${legCount}/${MAX_ROUTE_LEGS} legs · Esc to stop</div>`
            : `<div class="tf-route-mode-hint">Toggle map clicking above or use <strong>+ Route</strong> inside any Track Inspector to chain up to ${MAX_ROUTE_LEGS} tracks.</div>`
        }
      </div>
    `;

    // Ordered Route Legs section
    let legsHtml = "";
    if (legCount === 0) {
      legsHtml = `
        <div class="tf-route-empty">
          <div class="tf-route-empty-title">No tracks chained yet</div>
          <p class="tf-inspector-muted">
            Chain 2+ tracks (e.g. <strong>Davies Plain Track Lower</strong> + <strong>Davies Plain Ridge</strong>) to compute total distance, combined elevation, closures &amp; gate alerts, rig suitability, and 300 DPI GridForge strip-atlas exports.
          </p>
        </div>
      `;
    } else {
      const rows = trackIds
        .map((trackId, idx) => {
          const leg =
            analysis?.legs?.[idx]?.track_id === trackId
              ? analysis.legs[idx]
              : analysis?.legs?.find((l) => l.track_id === trackId);
          const name = leg?.name ?? trackId;
          const grade = leg?.grade ?? "—";
          const gradeColor = leg ? getGradeColor(grade) : "#94A3B8";
          const lengthText = leg
            ? `${(leg.length_m / 1000).toFixed(2)} km`
            : loading
              ? "calculating…"
              : "—";
          const closureBadge = leg?.closure_status
            ? getClosureStatusBadge(leg.closure_status)
            : null;
          const hasClosure = closureBadge && closureBadge.tone !== "open";
          const gateCount = leg?.gate_warning_count ?? 0;
          const gapM = leg?.gap_from_prev_m ?? null;

          const gapBanner =
            idx > 0 && typeof gapM === "number" && gapM > LEG_GAP_WARNING_M
              ? `<div class="tf-route-gap-warn">⚠ ${escapeHtml(formatGapDistance(gapM))} non-contiguous gap from Leg ${idx}</div>`
              : "";

          return `
            ${gapBanner}
            <div class="tf-route-leg-card">
              <span class="tf-route-leg-num mono">${idx + 1}</span>
              <div class="tf-route-leg-main">
                <button
                  type="button"
                  class="tf-route-leg-name"
                  data-inspect-leg="${escapeHtml(trackId)}"
                  title="Inspect ${escapeHtml(name)}"
                >${escapeHtml(name)}</button>
                <div class="tf-route-leg-meta">
                  <span class="mono">${escapeHtml(lengthText)}</span>
                  ${
                    leg
                      ? `<span class="tf-route-grade-chip" style="border-color:${gradeColor};color:${gradeColor}">${escapeHtml(grade)}</span>`
                      : ""
                  }
                  ${
                    leg?.reversed
                      ? `<span class="tf-mini-tag cyan" title="Auto-oriented end-to-end for continuity">⇄ Rev</span>`
                      : ""
                  }
                  ${
                    hasClosure
                      ? `<span class="tf-mini-tag red" title="Official closure / restriction: ${escapeHtml(closureBadge.label)}">⛔ ${escapeHtml(closureBadge.label)}</span>`
                      : ""
                  }
                  ${
                    gateCount > 0
                      ? `<span class="tf-mini-tag amber" title="${gateCount} community locked-gate / private-access warning(s)">🚧 ${gateCount}</span>`
                      : ""
                  }
                </div>
              </div>
              <div class="tf-route-leg-actions">
                <button type="button" class="tf-leg-btn" data-leg-up="${idx}" ${idx === 0 ? "disabled" : ""} title="Move leg up" aria-label="Move leg ${idx + 1} up">↑</button>
                <button type="button" class="tf-leg-btn" data-leg-down="${idx}" ${idx === legCount - 1 ? "disabled" : ""} title="Move leg down" aria-label="Move leg ${idx + 1} down">↓</button>
                <button type="button" class="tf-leg-btn danger" data-leg-remove="${idx}" title="Remove leg" aria-label="Remove leg ${idx + 1}">×</button>
              </div>
            </div>
          `;
        })
        .join("");

      legsHtml = `<div class="tf-card-stack">${rows}</div>`;
    }

    // Route Safety, Metrics, Suitability, Alerts, Elevation, Crossings/POIs & Exports
    let analysisHtml = "";
    if (loading && !analysis && legCount > 0) {
      analysisHtml = `<div class="tf-inspector-loading">Stitching route legs, computing safety summary, rig suitability &amp; elevation profile…</div>`;
    } else if (analysis && legCount > 0) {
      const safety = formatRouteSafetyBadge(analysis.safety_summary);
      const worstColor = getGradeColor(analysis.worst_grade);
      const suitability =
        analysis.suitability ??
        computeClientSuitability(analysis.worst_grade, activeVehicleClass, {
          vehicleName: activeVehicleName,
          closureStatus: analysis.safety_summary.has_blocking_closure
            ? "closed"
            : analysis.safety_summary.gate_warning_count > 0
              ? "gated"
              : "open",
        });
      const suitTone = getSuitabilityTone(suitability.rating);
      const rigSubtitle = suitability.vehicle_name
        ? `${suitability.vehicle_name} (${suitability.vehicle_class})`
        : `${suitability.vehicle_class} profile`;

      const closures = analysis.closures ?? [];
      const gateWarnings = analysis.community_gate_warnings ?? [];
      const crossings = analysis.waterway_crossings ?? [];
      const nearbyPois = analysis.nearby_pois ?? [];
      const parks = analysis.capad_parks ?? [];
      const wetLevel = (analysis.safety_summary.wet_risk_level ?? "dry").toLowerCase();
      const hasWetWarning = wetLevel === "wet" || wetLevel === "heavy_rain";
      const extraWarnings = analysis.safety_summary.warnings ?? [];

      const hasAnyAlert =
        closures.length > 0 ||
        gateWarnings.length > 0 ||
        hasWetWarning ||
        extraWarnings.length > 0;

      const alertsCalloutHtml = hasAnyAlert
        ? `
          <section class="tf-insp-section tf-route-alerts-panel tone-${safety.tone}">
            <div class="tf-insp-sec-head">
              <h3>⚠ Closures, Gates &amp; Wet-Risk Alerts</h3>
              <span class="mono tf-insp-meta">${closures.length + gateWarnings.length + (hasWetWarning ? 1 : 0)}</span>
            </div>
            ${
              extraWarnings.length > 0
                ? `<ul class="tf-suit-warnings">
                    ${extraWarnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}
                  </ul>`
                : ""
            }
            ${
              closures.length > 0
                ? `<div class="tf-card-stack">
                    ${closures
                      .map((c) => {
                        const tone = getClosureTone(c.closure_type, c.severity);
                        const dateSpan = formatClosureDateRange(
                          c.effective_from,
                          c.effective_to
                        );
                        return `
                          <div class="tf-closure-card tone-${tone}">
                            <div class="tf-closure-top">
                              <span class="tf-closure-type">${escapeHtml(formatClosureTypeLabel(c.closure_type))}</span>
                              ${c.agency ? `<span class="tf-mini-tag">${escapeHtml(c.agency)}</span>` : ""}
                            </div>
                            <div class="tf-closure-title">${escapeHtml(c.title)}</div>
                            ${dateSpan ? `<div class="tf-closure-meta">${escapeHtml(dateSpan)}</div>` : ""}
                          </div>
                        `;
                      })
                      .join("")}
                  </div>`
                : ""
            }
            ${
              gateWarnings.length > 0
                ? `<div class="tf-card-stack">
                    ${gateWarnings
                      .map((g) => {
                        const fresh = formatReportFreshness(
                          g.freshness_score,
                          g.is_stale,
                          g.effective_status ?? g.status
                        );
                        return `
                          <div class="tf-closure-card tone-warning">
                            <div class="tf-closure-top">
                              <span class="tf-closure-type">🚧 ${escapeHtml(formatReportTypeLabel(g.report_type))}</span>
                              <span class="tf-fresh-badge tone-${fresh.tone}">${escapeHtml(fresh.label)}</span>
                            </div>
                            ${g.title ? `<div class="tf-closure-title">${escapeHtml(g.title)}</div>` : ""}
                            <p class="tf-closure-desc">${escapeHtml(g.notes)}</p>
                          </div>
                        `;
                      })
                      .join("")}
                  </div>`
                : ""
            }
          </section>
        `
        : "";

      const chart = buildRouteElevationProfileSvg(
        analysis.elevation_profile ?? [],
        {
          snowLineElevationM,
          crossings,
          legs: analysis.legs,
        }
      );

      const elevRangeText =
        typeof analysis.min_elev_m === "number" &&
        typeof analysis.max_elev_m === "number"
          ? `${Math.round(analysis.min_elev_m)}–${Math.round(analysis.max_elev_m)} m`
          : "—";

      const gridForgeCli =
        analysis.gridforge_cli?.trim() || formatGridForgeCli(displayName);

      const crossingsSummaryHtml =
        crossings.length > 0
          ? `<div class="tf-card-stack">
              ${crossings
                .slice(0, 8)
                .map((c) => {
                  const name = c.name || "Unnamed watercourse";
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
                        <span class="tf-mini-tag ${c.perennial ? "cyan" : ""}">${c.perennial ? "Perennial" : "Intermittent"}</span>
                        <span class="tf-mini-tag ${c.bridged ? "emerald" : "amber"}">${c.bridged ? "Bridged" : "Unbridged Ford"}</span>
                      </div>
                    </div>
                  `;
                })
                .join("")}
            </div>`
          : `<p class="tf-inspector-muted">No recorded waterway crossings along this route.</p>`;

      const poisSummaryHtml =
        nearbyPois.length > 0
          ? `<div class="tf-poi-list">
              ${nearbyPois
                .slice(0, 8)
                .map((poi, idx) => {
                  const iconName = resolvePoiIconName(
                    poi.icon ?? poi.category ?? poi.poi_type
                  );
                  const elev = poi.elevation_m ?? poi.elev_m;
                  const meta = [
                    (poi.category ?? poi.poi_type ?? "poi").replace(/_/g, " "),
                    typeof elev === "number" ? `${Math.round(elev)} m` : null,
                  ]
                    .filter(Boolean)
                    .join(" · ");
                  return `
                    <button type="button" class="tf-poi-item" data-route-poi-idx="${idx}">
                      <img src="/icons/${escapeHtml(iconName)}.svg" alt="" width="18" height="18" />
                      <span class="tf-poi-copy">
                        <span class="tf-poi-name">${escapeHtml(poi.name)}</span>
                        <span class="tf-poi-meta">${escapeHtml(meta)}</span>
                      </span>
                    </button>
                  `;
                })
                .join("")}
            </div>`
          : `<p class="tf-inspector-muted">No camps, huts, or POIs within route corridor.</p>`;

      const parksTagsHtml =
        parks.length > 0
          ? `<div class="tf-list-card-tags">
              ${parks
                .map(
                  (p) =>
                    `<span class="tf-mini-tag emerald">${escapeHtml(p.name)}${p.state ? ` (${escapeHtml(p.state)})` : ""}</span>`
                )
                .join("")}
            </div>`
          : "";

      analysisHtml = `
        <div class="tf-route-safety-banner tone-${safety.tone}">
          <div class="tf-route-safety-head">
            <span class="tf-route-safety-badge">${escapeHtml(safety.icon)} ${escapeHtml(safety.label)}</span>
            ${
              analysis.offline_computed
                ? `<span class="tf-mini-tag cyan">OFFLINE PACK</span>`
                : loading
                  ? `<span class="tf-mini-tag">Updating…</span>`
                  : ""
            }
          </div>
          <div class="tf-route-safety-detail">${escapeHtml(safety.detail)}</div>
        </div>

        <div class="tf-grade-hero" style="border-color:${worstColor}">
          <div class="tf-grade-hero-left">
            <span class="tf-grade-swatch" style="background:${worstColor}"></span>
            <div>
              <div class="tf-grade-name" style="color:${worstColor}">${escapeHtml(analysis.worst_grade)}</div>
              <div class="tf-grade-sub">Overall Worst Grade · ${analysis.legs.length} Leg${analysis.legs.length === 1 ? "" : "s"}</div>
            </div>
          </div>
          <div class="tf-grade-hero-right">
            ${
              typeof analysis.worst_score === "number"
                ? `<div class="tf-grade-score mono">${Math.round(analysis.worst_score)}<span>/100</span></div>`
                : ""
            }
          </div>
        </div>

        <div class="tf-live-stats-grid">
          <div class="tf-stat-card">
            <span class="tf-stat-label">Total Distance</span>
            <span class="tf-stat-val mono">${(analysis.total_length_m / 1000).toFixed(2)} km</span>
          </div>
          <div class="tf-stat-card">
            <span class="tf-stat-label">Est. Time</span>
            <span class="tf-stat-val mono">${escapeHtml(formatDurationMinutes(analysis.est_duration_min))}</span>
          </div>
          <div class="tf-stat-card">
            <span class="tf-stat-label">Climb / Drop</span>
            <span class="tf-stat-val mono">+${Math.round(analysis.elev_gain_m)} / −${Math.round(analysis.elev_loss_m)} m</span>
          </div>
          <div class="tf-stat-card">
            <span class="tf-stat-label">Elev / Max Slope</span>
            <span class="tf-stat-val mono">${escapeHtml(elevRangeText)}${typeof analysis.max_slope_deg === "number" ? ` · ${analysis.max_slope_deg.toFixed(1)}°` : ""}</span>
          </div>
        </div>

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

        ${alertsCalloutHtml}

        <section class="tf-insp-section">
          <div class="tf-insp-sec-head">
            <h3>Combined Elevation Profile</h3>
            <span class="mono tf-insp-meta">${
              chart.steepZoneCount > 0
                ? `${chart.steepZoneCount} steep ≥14°`
                : "DEM AHD"
            }</span>
          </div>
          <div class="tf-elev-chart-wrap">
            ${chart.svgHtml}
          </div>
        </section>

        <section class="tf-insp-section">
          <div class="tf-insp-sec-head">
            <h3>Exports &amp; GridForge Print Handoff</h3>
          </div>
          <div class="tf-btn-row">
            <button
              type="button"
              class="tf-btn-primary"
              data-export-route="gpx"
              ${exporting !== null ? "disabled" : ""}
              title="Download GridForge-compatible GPX 1.1 with per-leg segments and waypoints"
            >${exporting === "gpx" ? "Exporting…" : "⬇ GPX 1.1"}</button>
            <button
              type="button"
              class="tf-btn-secondary"
              data-export-route="geojson"
              ${exporting !== null ? "disabled" : ""}
              title="Download RFC 7946 GeoJSON FeatureCollection"
            >${exporting === "geojson" ? "Exporting…" : "⬇ GeoJSON"}</button>
          </div>

          <details class="tf-gridforge-card" data-role="gridforge-details" ${this.gridForgeExpanded ? "open" : ""}>
            <summary class="tf-gridforge-summary">
              <span>🖨 Print in GridForge (300 DPI Strip Atlas)</span>
              <span class="tf-mini-tag cyan">CLI</span>
            </summary>
            <div class="tf-gridforge-body">
              <p class="tf-panel-hint">
                Download <strong>GPX 1.1</strong> into <code>./out/</code>, then run GridForge to render a 300 DPI multi-page strip atlas with elevation profile, route card &amp; calibration page:
              </p>
              <pre class="tf-cli-block mono">${escapeHtml(gridForgeCli)}</pre>
              <button
                type="button"
                class="tf-btn-secondary tf-copy-cli-btn ${this.cliCopied ? "copied" : ""}"
                data-role="copy-cli"
              >${this.cliCopied ? "✓ Copied CLI" : "📋 Copy CLI"}</button>
            </div>
          </details>
        </section>

        <section class="tf-insp-section">
          <div class="tf-insp-sec-head">
            <h3>Waterway Crossings &amp; Camps/Huts</h3>
            <span class="mono tf-insp-meta">${crossings.length} ford${crossings.length === 1 ? "" : "s"} · ${nearbyPois.length} POI${nearbyPois.length === 1 ? "" : "s"}</span>
          </div>
          ${parksTagsHtml}
          <div class="tf-subhead">Waterway Crossings (${analysis.safety_summary.unbridged_crossings} unbridged)</div>
          ${crossingsSummaryHtml}
          <div class="tf-subhead">Camps, Huts &amp; Landmarks</div>
          ${poisSummaryHtml}
        </section>
      `;
    }

    // Save / Load / Delete Saved Routes section
    const savedRoutesHtml = isAuthenticated
      ? `
        <section class="tf-insp-section">
          <div class="tf-insp-sec-head">
            <h3>Saved Routes</h3>
            <span class="mono tf-insp-meta">${savedRoutesLoading ? "Loading…" : `${savedRoutes.length} saved`}</span>
          </div>

          ${
            legCount > 0
              ? `
                <div class="tf-route-save-box">
                  <label class="tf-field">
                    <span class="tf-label">Trip Notes (optional)</span>
                    <input
                      type="text"
                      class="tf-input"
                      data-role="route-notes"
                      value="${escapeHtml(routeNotes)}"
                      maxlength="300"
                      placeholder="Fuel stops, camp plan, recovery gear notes…"
                    />
                  </label>
                  <div class="tf-btn-row">
                    <button
                      type="button"
                      class="tf-btn-primary"
                      data-role="save-route"
                      ${saving ? "disabled" : ""}
                    >${
                      saving
                        ? "Saving…"
                        : activeSavedRouteId
                          ? "💾 Update Saved Route"
                          : "💾 Save Route"
                    }</button>
                    ${
                      activeSavedRouteId
                        ? `<button type="button" class="tf-btn-secondary" data-role="save-route-copy" ${saving ? "disabled" : ""}>Save as New</button>`
                        : ""
                    }
                  </div>
                </div>
              `
              : ""
          }

          ${
            savedRoutes.length === 0
              ? `<p class="tf-inspector-muted">No saved routes yet. Chain tracks above and save your route for quick reload and GPX export.</p>`
              : `
                <div class="tf-card-stack">
                  ${savedRoutes
                    .map((sr) => {
                      const rid = getSavedRouteId(sr);
                      const isCurrent = rid === activeSavedRouteId;
                      const distKm =
                        typeof sr.total_length_m === "number"
                          ? `${(sr.total_length_m / 1000).toFixed(1)} km`
                          : null;
                      const legTxt = `${sr.leg_count ?? sr.track_ids?.length ?? 0} leg${(sr.leg_count ?? sr.track_ids?.length ?? 0) === 1 ? "" : "s"}`;
                      const sub = [
                        legTxt,
                        distKm,
                        sr.worst_grade ?? null,
                        sr.has_blocking_closure ? "⛔ Closure" : null,
                      ]
                        .filter(Boolean)
                        .join(" · ");
                      return `
                        <div class="tf-saved-row ${isCurrent ? "active" : ""}">
                          <button type="button" class="tf-saved-main" data-load-route="${escapeHtml(rid)}">
                            <span class="tf-saved-title">${escapeHtml(sr.name)}</span>
                            <span class="tf-saved-sub mono">${escapeHtml(sub)}</span>
                            ${sr.notes ? `<span class="tf-saved-note">${escapeHtml(sr.notes)}</span>` : ""}
                          </button>
                          <button type="button" class="tf-mini-btn" data-export-saved="${escapeHtml(rid)}" title="Download GPX 1.1">GPX</button>
                          <button type="button" class="tf-mini-btn danger" data-delete-route="${escapeHtml(rid)}" title="Delete saved route">×</button>
                        </div>
                      `;
                    })
                    .join("")}
                </div>
              `
          }
        </section>
      `
      : `
        <section class="tf-insp-section tf-route-signin-card">
          <div class="tf-insp-sec-head">
            <h3>Save &amp; Sync Routes</h3>
            <span class="tf-mini-tag">Optional</span>
          </div>
          <p class="tf-inspector-muted">
            Planning, safety checks, and GPX/GeoJSON/GridForge exports work without signing in. Sign in to save and sync your multi-track routes across sessions.
          </p>
          <button type="button" class="tf-btn-secondary" data-role="open-sign-in">Sign In to Save Routes</button>
        </section>
      `;

    this.root.innerHTML = `
      ${renderSheetHandleMarkup("Resize route planner")}
      <div class="tf-inspector-head">
        <div class="tf-inspector-head-copy">
          <h2 class="tf-inspector-title">Route planner</h2>
          <div class="tf-inspector-subline">${legCount} leg${legCount === 1 ? "" : "s"} · GPX &amp; GridForge strip atlas</div>
        </div>
        <div class="tf-head-actions">
          <button type="button" class="tf-inspector-close" data-role="close" aria-label="Close Route Planner">${uiIcon("close", 15)}</button>
        </div>
      </div>

      <div class="tf-inspector-body">
        ${error ? `<div class="tf-inspector-error">${escapeHtml(error)}</div>` : ""}

        ${planModeHtml}

        <label class="tf-field">
          <span class="tf-label">Route Name</span>
          <input
            type="text"
            class="tf-input"
            data-role="route-name"
            value="${escapeHtml(routeName)}"
            maxlength="160"
            placeholder="${escapeHtml(analysis?.name ?? "e.g. Davies Plain Alpine Traverse")}"
          />
        </label>

        <section class="tf-insp-section">
          <div class="tf-insp-sec-head">
            <h3>Route Legs</h3>
            <span class="mono tf-insp-meta">${legCount} / ${MAX_ROUTE_LEGS}</span>
          </div>
          ${legsHtml}
        </section>

        ${analysisHtml}
        ${savedRoutesHtml}
      </div>
    `;

    this.bindHandlers(analysis);
  }

  private bindHandlers(analysis: PlannedRouteAnalysis | null): void {
    this.root
      .querySelector<HTMLButtonElement>("[data-role='close']")
      ?.addEventListener("click", () => {
        this.close();
        this.callbacks.onClose();
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='toggle-plan-mode']")
      ?.addEventListener("click", () => {
        this.callbacks.onTogglePlanMode(!this.state.planModeActive);
      });

    const bbox = extractAnalysisBBox(analysis);
    const zoomBtn = this.root.querySelector<HTMLButtonElement>(
      "[data-role='zoom-route']"
    );
    if (zoomBtn && bbox) {
      zoomBtn.addEventListener("click", () =>
        this.callbacks.onZoomToRoute(bbox)
      );
    }

    this.root
      .querySelector<HTMLButtonElement>("[data-role='clear-route']")
      ?.addEventListener("click", () => {
        this.callbacks.onClearRoute();
      });

    this.root
      .querySelector<HTMLInputElement>("[data-role='route-name']")
      ?.addEventListener("input", (e) => {
        const val = (e.currentTarget as HTMLInputElement).value;
        this.state = { ...this.state, routeName: val };
        this.callbacks.onRouteNameChange(val);
      });

    this.root
      .querySelector<HTMLInputElement>("[data-role='route-notes']")
      ?.addEventListener("input", (e) => {
        const val = (e.currentTarget as HTMLInputElement).value;
        this.state = { ...this.state, routeNotes: val };
        this.callbacks.onRouteNotesChange(val);
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-inspect-leg]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const trackId = btn.dataset.inspectLeg;
          if (trackId) this.callbacks.onInspectTrack(trackId);
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-leg-up]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const idx = Number(btn.dataset.legUp);
          this.callbacks.onMoveLegUp(idx);
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-leg-down]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const idx = Number(btn.dataset.legDown);
          this.callbacks.onMoveLegDown(idx);
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-leg-remove]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const idx = Number(btn.dataset.legRemove);
          this.callbacks.onRemoveLeg(idx);
        });
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='change-rig']")
      ?.addEventListener("click", () => {
        this.callbacks.onOpenRigPanel();
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='open-sign-in']")
      ?.addEventListener("click", () => {
        this.callbacks.onOpenSignIn();
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-export-route]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const fmt = btn.dataset.exportRoute as "gpx" | "geojson";
          void this.callbacks.onExportRoute(fmt);
        });
      });

    const gfDetails = this.root.querySelector<HTMLDetailsElement>(
      "[data-role='gridforge-details']"
    );
    gfDetails?.addEventListener("toggle", () => {
      this.gridForgeExpanded = gfDetails.open;
    });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='copy-cli']")
      ?.addEventListener("click", () => {
        const displayName =
          this.state.routeName.trim() ||
          analysis?.name ||
          "TrailForge Planned Route";
        const cli =
          analysis?.gridforge_cli?.trim() || formatGridForgeCli(displayName);
        this.cliCopied = true;
        if (this.copiedCliTimer !== null) {
          window.clearTimeout(this.copiedCliTimer);
        }
        this.render();
        this.copiedCliTimer = window.setTimeout(() => {
          this.cliCopied = false;
          this.copiedCliTimer = null;
          if (this.isOpen) this.render();
        }, 2000);
        void this.callbacks.onCopyGridForgeCli(cli);
      });

    const nearbyPois = analysis?.nearby_pois ?? [];
    this.root
      .querySelectorAll<HTMLButtonElement>("[data-route-poi-idx]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const idx = Number(btn.dataset.routePoiIdx);
          const poi = nearbyPois[idx];
          if (poi) this.callbacks.onPoiSelect(poi);
        });
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='save-route']")
      ?.addEventListener("click", () => {
        void this.callbacks.onSaveRoute(false);
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='save-route-copy']")
      ?.addEventListener("click", () => {
        void this.callbacks.onSaveRoute(true);
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-load-route]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const id = btn.dataset.loadRoute;
          if (id) void this.callbacks.onLoadSavedRoute(id);
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-delete-route]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const id = btn.dataset.deleteRoute;
          if (id) void this.callbacks.onDeleteSavedRoute(id);
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-export-saved]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const id = btn.dataset.exportSaved;
          if (id) void this.callbacks.onExportSavedRoute(id, "gpx");
        });
      });
  }
}
