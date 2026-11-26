import {
  App,
  applyDocumentTheme,
  applyHostFonts,
  applyHostStyleVariables,
  type McpUiHostCapabilities,
  type McpUiHostContext,
  type McpUiToolCancelledNotification,
  type McpUiToolInputNotification,
  type McpUiToolResultNotification,
} from "@modelcontextprotocol/ext-apps";
import {
  formatBreakdownFactors,
  formatOverrideLabel,
  type ElevationChartGeometry,
} from "../inspectorHelpers";
import { escapeHtml, getGradeColor } from "../presets";
import type { BaselineBreakdown } from "../types";
import { getSuitabilityTone } from "../vehicleHelpers";
import { getDemoPayload } from "./demoFixtures";
import {
  buildCompareElevationSvg,
  buildMcpElevationSvg,
  sampleElevationAtDistance,
} from "./elevationRenderer";
import {
  detectWebGLSupport,
  hasUsableTileConfig,
  mountMapLibreMap,
  type MapLibreOverlayController,
} from "./mapLibreRenderer";
import {
  buildVectorMapSvg,
  findNearestPointOnTrackCourse,
  sampleTrackCourseAtDistance,
  type VectorMapRenderResult,
} from "./svgMapRenderer";
import {
  COMPARE_FALLBACK_COLORS,
  DEFAULT_ATTRIBUTIONS,
  normalizeViewPayload,
  sanitizeExternalHttpUrl,
  sanitizeHexColor,
  type CompactElevationPoint,
  type HostCapabilityFlags,
  type TrailForgeViewPayload,
} from "./types";

/**
 * Minimal structural subset of `@modelcontextprotocol/ext-apps` `App` used by
 * `TrailForgeMcpApp` so the controller can run against the real SDK in production
 * AND against lightweight deterministic fakes in offline Vitest tests.
 */
export interface McpAppBridge {
  ontoolinput:
    | ((params: McpUiToolInputNotification["params"]) => void)
    | undefined;
  ontoolresult:
    | ((params: McpUiToolResultNotification["params"]) => void)
    | undefined;
  ontoolcancelled:
    | ((params: McpUiToolCancelledNotification["params"]) => void)
    | undefined;
  onhostcontextchanged: ((params: McpUiHostContext) => void) | undefined;
  connect: () => Promise<void>;
  getHostCapabilities: () => McpUiHostCapabilities | undefined;
  getHostContext: () => McpUiHostContext | undefined;
  openLink: (params: { url: string }) => Promise<{ isError?: boolean }>;
  updateModelContext: (params: {
    content?: Array<{ type: "text"; text: string }>;
    structuredContent?: Record<string, unknown>;
  }) => Promise<unknown>;
  callServerTool: (params: {
    name: string;
    arguments?: Record<string, unknown>;
  }) => Promise<{
    isError?: boolean;
    structuredContent?: Record<string, unknown>;
    content?: Array<{ type: string; text?: string }>;
  }>;
}

export interface ExplorerWidgetState {
  status: "idle" | "loading" | "ready" | "cancelled" | "error";
  statusMessage: string | null;
  payload: TrailForgeViewPayload | null;
  previousExplorePayload: TrailForgeViewPayload | null;
  theme: "light" | "dark";
  capabilities: HostCapabilityFlags;
  rendererMode: "svg" | "maplibre";
  forceSvgFallback: boolean;
  standalonePreview: boolean;
  actionToast: string | null;
}

export interface TrailForgeMcpAppOptions {
  root?: HTMLElement | null;
  bridge?: McpAppBridge;
  /** Optional location search string override (e.g. `?demo=route&theme=light`). */
  search?: string;
  /** Skip MapLibre mount attempts (useful in pure SVG / test contexts). */
  forceSvgFallback?: boolean;
  /** Custom theme applicator hooks (default to `@modelcontextprotocol/ext-apps` helpers). */
  themeHooks?: {
    applyDocumentTheme: typeof applyDocumentTheme;
    applyHostStyleVariables: typeof applyHostStyleVariables;
    applyHostFonts: typeof applyHostFonts;
  };
  /** Custom clipboard writer for GridForge CLI copy button. */
  copyText?: (value: string) => Promise<void>;
}

/**
 * Derive explicit UI capability gates from `app.getHostCapabilities()`.
 */
export function resolveHostCapabilities(
  caps: McpUiHostCapabilities | undefined
): HostCapabilityFlags {
  return {
    openLinks: Boolean(caps?.openLinks),
    updateModelContext: Boolean(caps?.updateModelContext),
    serverTools: Boolean(caps?.serverTools),
  };
}

/**
 * Apply host light/dark theme and SEP-1865 CSS custom properties cleanly.
 */
export function applyHostThemeContext(
  ctx: McpUiHostContext | undefined,
  hooks: {
    applyDocumentTheme: typeof applyDocumentTheme;
    applyHostStyleVariables: typeof applyHostStyleVariables;
    applyHostFonts: typeof applyHostFonts;
  } = { applyDocumentTheme, applyHostStyleVariables, applyHostFonts },
  rootEl?: HTMLElement
): "light" | "dark" | null {
  if (!ctx) return null;
  let nextTheme: "light" | "dark" | null = null;
  if (ctx.theme === "light" || ctx.theme === "dark") {
    nextTheme = ctx.theme;
    if (typeof document !== "undefined") {
      hooks.applyDocumentTheme(ctx.theme);
    }
  }
  if (ctx.styles?.variables && typeof document !== "undefined") {
    hooks.applyHostStyleVariables(ctx.styles.variables, rootEl);
  }
  if (ctx.styles?.css?.fonts && typeof document !== "undefined") {
    hooks.applyHostFonts(ctx.styles.css.fonts);
  }
  return nextTheme;
}

/**
 * Extract `TrailForgeViewPayload` from an MCP `CallToolResult` notification,
 * checking `structuredContent` first and falling back to JSON `TextContent`.
 */
export function extractPayloadFromToolResult(
  result: {
    isError?: boolean;
    structuredContent?: Record<string, unknown>;
    content?: Array<{ type: string; text?: string }>;
  }
): TrailForgeViewPayload | null {
  if (result.structuredContent) {
    const parsed = normalizeViewPayload(result.structuredContent);
    if (parsed) return parsed;
  }
  for (const block of result.content ?? []) {
    if (block.type === "text" && typeof block.text === "string") {
      const trimmed = block.text.trim();
      if (trimmed.startsWith("{") && trimmed.endsWith("}")) {
        try {
          const obj: unknown = JSON.parse(trimmed);
          const parsed = normalizeViewPayload(obj);
          if (parsed) return parsed;
        } catch (err) {
          console.debug("Text block was not JSON TrailForgeViewPayload:", err);
        }
      }
    }
  }
  return null;
}

function formatKm(meters: number | null | undefined): string {
  if (typeof meters !== "number" || !Number.isFinite(meters)) return "—";
  return `${(meters / 1000).toFixed(1)} km`;
}

function formatDuration(mins: number | null | undefined): string {
  if (typeof mins !== "number" || !Number.isFinite(mins) || mins <= 0) {
    return "—";
  }
  const h = Math.floor(mins / 60);
  const m = Math.round(mins % 60);
  return h > 0 ? `${h}h ${m}m` : `${m} min`;
}

function renderSuitabilityBadge(
  rating: string | null | undefined,
  rigClass?: string | null
): string {
  if (!rating) return "";
  const tone = getSuitabilityTone(rating);
  const prefix = rigClass ? `${rigClass}: ` : "";
  return `<span class="tfm-pill ${tone}" data-role="suitability-badge">${escapeHtml(prefix + rating)}</span>`;
}

function renderAttributionFooter(attributions: readonly string[]): string {
  const list = attributions.length > 0 ? attributions : DEFAULT_ATTRIBUTIONS;
  return `
    <footer class="tfm-footer" data-role="attribution-footer" aria-label="Data attributions">
      ${list.map((line) => `<span>${escapeHtml(line)}</span>`).join("")}
    </footer>
  `;
}

function renderClosuresAndWarningsHtml(
  closures: ReadonlyArray<{
    title: string;
    agency?: string | null;
    severity?: string | null;
    closure_type?: string | null;
    source_url?: string | null;
  }>,
  warnings: ReadonlyArray<{
    report_type: string;
    title?: string | null;
    notes?: string | null;
  }>,
  canOpenLinks: boolean
): string {
  if (closures.length === 0 && warnings.length === 0) return "";

  const closureRows = closures
    .map((c) => {
      const isCrit =
        c.severity === "critical" || c.closure_type === "closed";
      const safeUrl = sanitizeExternalHttpUrl(c.source_url);
      const linkBtn =
        safeUrl && canOpenLinks
          ? ` <button type="button" class="tfm-btn tfm-btn-sm" data-open-url="${escapeHtml(safeUrl)}">Official Notice ↗</button>`
          : "";
      return `
        <div class="tfm-alert-item ${isCrit ? "critical" : ""}">
          <strong>${escapeHtml(c.title)}</strong>
          ${c.agency ? `<span class="mono"> · ${escapeHtml(c.agency)}</span>` : ""}
          ${linkBtn}
        </div>
      `;
    })
    .join("");

  const warningRows = warnings
    .map((w) => {
      const heading = w.title || w.report_type.replace(/_/g, " ");
      return `
        <div class="tfm-alert-item">
          <strong>${escapeHtml(heading)}</strong>
          ${w.notes ? ` — ${escapeHtml(w.notes)}` : ""}
        </div>
      `;
    })
    .join("");

  return `
    <section class="tfm-card" data-role="alerts-section">
      <div class="tfm-card-head">
        <h3 class="tfm-card-title">Closures, Gates &amp; Hazards</h3>
        <span class="mono">${closures.length + warnings.length}</span>
      </div>
      <div class="tfm-alert-list">${closureRows}${warningRows}</div>
    </section>
  `;
}

export interface RenderedExplorerView {
  html: string;
  vectorMap: VectorMapRenderResult | null;
  elevationGeometry: ElevationChartGeometry | null;
  elevationPoints: CompactElevationPoint[];
}

/**
 * Pure deterministic renderer for `TrailForge Explorer` across all 4 `view_mode`s
 * (`explore`, `track`, `route`, `compare`) plus loading/cancelled/error states.
 */
export function renderExplorerApp(
  state: ExplorerWidgetState
): RenderedExplorerView {
  if (state.status === "loading") {
    return {
      html: `
        <div class="tfm-shell" data-status="loading">
          <div class="tfm-status-banner" role="status">
            <strong>TrailForge Explorer</strong> — ${escapeHtml(state.statusMessage ?? "Loading trail data…")}
          </div>
          ${renderAttributionFooter(DEFAULT_ATTRIBUTIONS)}
        </div>
      `,
      vectorMap: null,
      elevationGeometry: null,
      elevationPoints: [],
    };
  }

  if (state.status === "cancelled" || state.status === "error") {
    return {
      html: `
        <div class="tfm-shell" data-status="${state.status}">
          <div class="tfm-status-banner ${state.status === "error" ? "error" : ""}" role="status">
            <strong>${state.status === "cancelled" ? "Request Cancelled" : "TrailForge Notice"}</strong> — ${escapeHtml(state.statusMessage ?? "No trail data rendered.")}
          </div>
          ${renderAttributionFooter(DEFAULT_ATTRIBUTIONS)}
        </div>
      `,
      vectorMap: null,
      elevationGeometry: null,
      elevationPoints: [],
    };
  }

  const payload = state.payload;
  if (!payload) {
    return {
      html: `
        <div class="tfm-shell" data-status="idle">
          <div class="tfm-status-banner">
            <strong>TrailForge Explorer</strong> — Waiting for tool output…
          </div>
          ${renderAttributionFooter(DEFAULT_ATTRIBUTIONS)}
        </div>
      `,
      vectorMap: null,
      elevationGeometry: null,
      elevationPoints: [],
    };
  }

  const canOpenLinks = state.capabilities.openLinks || state.standalonePreview;
  const canUpdateContext = state.capabilities.updateModelContext;
  const canCallServer = state.capabilities.serverTools;

  const vectorMap = buildVectorMapSvg(payload, {
    width: 680,
    height: 310,
    theme: state.theme,
    interactiveTracks:
      canCallServer &&
      (payload.view_mode === "explore" ||
        payload.view_mode === "route" ||
        payload.view_mode === "compare"),
  });

  // Elevation profile generation (track, route, compare)
  let elevationHtml = "";
  let elevationGeometry: ElevationChartGeometry | null = null;
  let elevationPoints: CompactElevationPoint[] = [];

  if (payload.view_mode === "track" && payload.track) {
    elevationPoints = payload.track.elevation_profile ?? [];
    const chart = buildMcpElevationSvg(elevationPoints, {
      width: 680,
      height: 185,
      snowLineElevationM: payload.snowline_m,
      crossings: payload.track.crossings ?? [],
    });
    elevationGeometry = chart.geometry;
    elevationHtml = `
      <section class="tfm-card" data-role="elevation-section">
        <div class="tfm-card-head">
          <h3 class="tfm-card-title">Synchronized Elevation Profile</h3>
          <span class="mono">0 → ${formatKm(chart.totalDistM)} · Snow-line ${payload.snowline_m}m</span>
        </div>
        <div class="tfm-scrub-hud" data-role="scrub-hud">
          <span>Move cursor or drag across the elevation profile or map to sync position, elevation &amp; slope.</span>
        </div>
        <div data-role="elevation-svg-wrap">${chart.svgHtml}</div>
      </section>
    `;
  } else if (payload.view_mode === "route" && payload.route) {
    elevationPoints = payload.route.elevation_profile ?? [];
    const chart = buildMcpElevationSvg(elevationPoints, {
      width: 680,
      height: 185,
      snowLineElevationM: payload.snowline_m,
    });
    elevationGeometry = chart.geometry;
    elevationHtml = `
      <section class="tfm-card" data-role="elevation-section">
        <div class="tfm-card-head">
          <h3 class="tfm-card-title">Combined Route Elevation Profile</h3>
          <span class="mono">${payload.route.legs.length} legs · ${formatKm(payload.route.total_length_m)}</span>
        </div>
        <div class="tfm-scrub-hud" data-role="scrub-hud">
          <span>Scrub the profile or map to inspect stitched route elevation, grade &amp; coordinates.</span>
        </div>
        <div data-role="elevation-svg-wrap">${chart.svgHtml}</div>
      </section>
    `;
  } else if (payload.view_mode === "compare" && payload.compare) {
    const compChart = buildCompareElevationSvg(payload.compare.tracks ?? [], {
      width: 680,
      height: 205,
      snowLineElevationM: payload.snowline_m,
      theme: state.theme,
    });
    elevationGeometry = compChart.geometry;
    elevationHtml = `
      <section class="tfm-card" data-role="elevation-section">
        <div class="tfm-card-head">
          <h3 class="tfm-card-title">Overlaid Elevation Comparison</h3>
          <span class="mono">${payload.compare.tracks.length} tracks · Snow-line ${payload.snowline_m}m</span>
        </div>
        <div class="tfm-scrub-hud" data-role="scrub-hud">
          <span>Hover across the chart to compare elevation at matching distances.</span>
        </div>
        <div data-role="elevation-svg-wrap">${compChart.svgHtml}</div>
      </section>
    `;
  }

  // Header suitability + grade chips
  const topSuitability =
    payload.view_mode === "track"
      ? payload.track?.summary.suitability?.rating
      : payload.view_mode === "route"
        ? payload.route?.suitability?.rating
        : null;
  const topGrade =
    payload.view_mode === "track"
      ? payload.track?.summary.grade
      : payload.view_mode === "route"
        ? payload.route?.worst_grade
        : null;

  const rigChipHtml = payload.rig_context
    ? `<span class="tfm-pill info" data-role="rig-chip">${escapeHtml(payload.rig_context.vehicle_name ? `${payload.rig_context.vehicle_name} (${payload.rig_context.capability_class})` : payload.rig_context.capability_class)}${payload.rig_context.has_snorkel ? " · Snorkel" : ""}</span>`
    : "";

  const gradeChipHtml = topGrade
    ? `<span class="tfm-pill" style="border-color:${getGradeColor(topGrade)};color:${getGradeColor(topGrade)}"><span class="tfm-pill-dot" style="background:${getGradeColor(topGrade)}"></span>${escapeHtml(topGrade)}</span>`
    : "";

  // Mode-specific KPIs and detail sections
  let modeBodyHtml = "";

  if (payload.view_mode === "track" && payload.track) {
    const tr = payload.track;
    const sum = tr.summary;
    const factors = formatBreakdownFactors(
      (tr.baseline_breakdown ?? null) as BaselineBreakdown | null
    );
    const overrides = Array.isArray(
      (tr.baseline_breakdown as { overrides?: unknown } | null)?.overrides
    )
      ? ((tr.baseline_breakdown as { overrides: string[] }).overrides ?? [])
      : [];
    const gradeColor = getGradeColor(sum.grade);
    const live = tr.live_summary;

    const kpiHtml = `
      <div class="tfm-kpi-strip" data-role="kpi-strip">
        <div class="tfm-kpi"><span class="tfm-kpi-label">Distance</span><span class="tfm-kpi-val mono">${formatKm(sum.total_length_m)}</span></div>
        <div class="tfm-kpi"><span class="tfm-kpi-label">Elev Range</span><span class="tfm-kpi-val mono">${typeof sum.elev_min_m === "number" && typeof sum.elev_max_m === "number" ? `${Math.round(sum.elev_min_m)}–${Math.round(sum.elev_max_m)} m` : "—"}</span></div>
        <div class="tfm-kpi"><span class="tfm-kpi-label">Climb / Drop</span><span class="tfm-kpi-val mono">+${Math.round(sum.elev_gain_m ?? 0)} / −${Math.round(sum.elev_loss_m ?? 0)} m</span></div>
        <div class="tfm-kpi"><span class="tfm-kpi-label">Peak Slope</span><span class="tfm-kpi-val mono">${typeof sum.max_slope_deg === "number" ? `${sum.max_slope_deg.toFixed(1)}°` : "—"}</span></div>
        <div class="tfm-kpi"><span class="tfm-kpi-label">Crossings</span><span class="tfm-kpi-val mono">${sum.waterway_crossing_count ?? tr.crossings.length}</span></div>
        <div class="tfm-kpi"><span class="tfm-kpi-label">Wet Risk (72h)</span><span class="tfm-kpi-val mono">${escapeHtml(live?.wet_risk_level ?? sum.wet_risk_level ?? "dry")}${typeof (live?.rain_72h_mm ?? sum.rain_72h_mm) === "number" ? ` · ${(live?.rain_72h_mm ?? sum.rain_72h_mm)!.toFixed(1)}mm` : ""}</span></div>
      </div>
    `;

    const breakdownHtml =
      factors.length > 0
        ? `
          <section class="tfm-card" data-role="grade-breakdown">
            <div class="tfm-card-head">
              <h3 class="tfm-card-title">Explainable Grade Breakdown</h3>
              <span class="mono">${sum.score !== null && sum.score !== undefined ? `${Math.round(sum.score)}/100` : ""}</span>
            </div>
            <div class="tfm-factor-list">
              ${factors
                .map(
                  (f) => `
                    <div>
                      <div class="tfm-factor-head">
                        <span>${escapeHtml(f.label)}</span>
                        <span class="mono">${f.points}/${f.maxPoints}</span>
                      </div>
                      <div class="tfm-factor-track"><div class="tfm-factor-fill" style="width:${f.pct}%;background:${gradeColor}"></div></div>
                      <div class="tfm-factor-detail">${escapeHtml(f.detail)}</div>
                    </div>
                  `
                )
                .join("")}
            </div>
            ${
              overrides.length > 0
                ? `<div class="tfm-pill-row">${overrides.map((ov) => `<span class="tfm-pill challenging">▲ ${escapeHtml(formatOverrideLabel(ov))}</span>`).join("")}</div>`
                : ""
            }
          </section>
        `
        : "";

    const liveChipsHtml = live
      ? `
        <section class="tfm-card" data-role="live-telemetry-section">
          <div class="tfm-card-head">
            <h3 class="tfm-card-title">Live Telemetry &amp; Conditions</h3>
            <span class="mono">${live.sample_count} traversals (${escapeHtml(live.confidence_level)})</span>
          </div>
          <div class="tfm-pill-row">
            ${live.live_grade ? `<span class="tfm-pill">Live: ${escapeHtml(live.live_grade)}</span>` : ""}
            ${typeof live.avg_speed_kmh === "number" ? `<span class="tfm-pill mono">Avg ${live.avg_speed_kmh.toFixed(1)} km/h</span>` : ""}
            ${typeof live.est_duration_min === "number" ? `<span class="tfm-pill mono">Est ${formatDuration(live.est_duration_min)}</span>` : ""}
            <span class="tfm-pill info">Rain 72h: ${typeof live.rain_72h_mm === "number" ? `${live.rain_72h_mm.toFixed(1)} mm` : "—"} (${escapeHtml(live.wet_risk_level)})</span>
            ${Object.entries(live.vehicle_class_split ?? {})
              .map(
                ([cls, count]) =>
                  `<span class="tfm-pill mono">${escapeHtml(cls)}: ${count}</span>`
              )
              .join("")}
          </div>
          ${
            sum.suitability?.summary
              ? `<p class="tfm-subtitle">${escapeHtml(sum.suitability.summary)}</p>`
              : ""
          }
          ${
            tr.parks.length > 0
              ? `<div class="tfm-pill-row">${tr.parks.map((pk) => `<span class="tfm-pill">${escapeHtml(pk.name)} (${escapeHtml(pk.state)})</span>`).join("")}</div>`
              : ""
          }
        </section>
      `
      : "";

    const alertsHtml = renderClosuresAndWarningsHtml(
      tr.closures ?? [],
      [...(tr.gate_warnings ?? []), ...(tr.condition_reports ?? [])],
      canOpenLinks
    );

    modeBodyHtml = `
      ${kpiHtml}
      ${elevationHtml}
      <div class="tfm-grid-2">
        ${breakdownHtml}
        ${liveChipsHtml}
      </div>
      ${alertsHtml}
    `;
  } else if (payload.view_mode === "route" && payload.route) {
    const rt = payload.route;
    const safe = rt.safety_summary;
    const kpiHtml = `
      <div class="tfm-kpi-strip" data-role="kpi-strip">
        <div class="tfm-kpi"><span class="tfm-kpi-label">Total Length</span><span class="tfm-kpi-val mono">${formatKm(rt.total_length_m)}</span></div>
        <div class="tfm-kpi"><span class="tfm-kpi-label">Est Drive Time</span><span class="tfm-kpi-val mono">${formatDuration(rt.est_duration_min)}</span></div>
        <div class="tfm-kpi"><span class="tfm-kpi-label">Climb / Drop</span><span class="tfm-kpi-val mono">+${Math.round(rt.elev_gain_m)} / −${Math.round(rt.elev_loss_m)} m</span></div>
        <div class="tfm-kpi"><span class="tfm-kpi-label">Worst Grade</span><span class="tfm-kpi-val" style="color:${getGradeColor(rt.worst_grade)}">${escapeHtml(rt.worst_grade ?? "Easy")}</span></div>
        <div class="tfm-kpi"><span class="tfm-kpi-label">Crossings</span><span class="tfm-kpi-val mono">${safe.waterway_crossing_count} (${safe.perennial_crossing_count} per.)</span></div>
        <div class="tfm-kpi"><span class="tfm-kpi-label">Wet Risk</span><span class="tfm-kpi-val mono">${escapeHtml(safe.wet_risk_level)}</span></div>
      </div>
    `;

    const legsHtml = `
      <section class="tfm-card" data-role="route-legs-section">
        <div class="tfm-card-head">
          <h3 class="tfm-card-title">Stitched Route Legs</h3>
          <span class="mono">${rt.legs.length} legs${safe.gap_count > 0 ? ` · ${safe.gap_count} gap(s)` : " · Continuous"}</span>
        </div>
        <div class="tfm-track-list">
          ${rt.legs
            .map((leg) => {
              const col = getGradeColor(leg.grade);
              const tag = canCallServer ? "button" : "div";
              const extraAttrs = canCallServer
                ? `type="button" class="tfm-track-card clickable" data-inspect-track="${escapeHtml(leg.track_id)}"`
                : `class="tfm-track-card"`;
              return `
                <${tag} ${extraAttrs}>
                  <div>
                    <div class="tfm-track-name">#${leg.leg_index + 1} ${escapeHtml(leg.name)}${leg.reversed ? " (Rev)" : ""}</div>
                    <div class="tfm-track-meta mono">${formatKm(leg.length_m)}${leg.gap_from_prev_m > 25 ? ` · Gap ${Math.round(leg.gap_from_prev_m)}m` : ""}</div>
                  </div>
                  <span class="tfm-pill" style="border-color:${col};color:${col}">${escapeHtml(leg.grade ?? "Easy")}</span>
                </${tag}>
              `;
            })
            .join("")}
        </div>
      </section>
    `;

    const gpxSafeUrl = sanitizeExternalHttpUrl(rt.export_gpx_url);
    const handoffHtml = `
      <section class="tfm-card" data-role="gridforge-section">
        <div class="tfm-card-head">
          <h3 class="tfm-card-title">Export &amp; GridForge 300 DPI Print CLI</h3>
          ${
            gpxSafeUrl && canOpenLinks
              ? `<button type="button" class="tfm-btn tfm-btn-sm" data-open-url="${escapeHtml(gpxSafeUrl)}">⬇ GPX</button>`
              : ""
          }
        </div>
        ${
          rt.gridforge_cli
            ? `
              <div class="tfm-cli-box">
                <code class="mono tfm-cli-code" data-role="gridforge-cli">${escapeHtml(rt.gridforge_cli)}</code>
                <button type="button" class="tfm-btn tfm-btn-sm" data-copy-cli="${escapeHtml(rt.gridforge_cli)}">Copy CLI</button>
              </div>
            `
            : ""
        }
        ${
          rt.suitability?.summary
            ? `<p class="tfm-subtitle">${escapeHtml(rt.suitability.summary)}</p>`
            : ""
        }
      </section>
    `;

    const alertsHtml = renderClosuresAndWarningsHtml(
      rt.closures ?? [],
      rt.gate_warnings ?? [],
      canOpenLinks
    );

    modeBodyHtml = `
      ${kpiHtml}
      ${elevationHtml}
      <div class="tfm-grid-2">
        ${legsHtml}
        ${handoffHtml}
      </div>
      ${alertsHtml}
    `;
  } else if (payload.view_mode === "compare" && payload.compare) {
    const tracks = payload.compare.tracks ?? [];
    const tableHtml = `
      <section class="tfm-card" data-role="compare-table-section">
        <div class="tfm-card-head">
          <h3 class="tfm-card-title">Side-by-Side Track Comparison</h3>
          <span class="mono">${tracks.length} tracks</span>
        </div>
        <div class="tfm-table-wrap">
          <table class="tfm-table">
            <thead>
              <tr>
                <th>Track</th>
                <th>Grade</th>
                <th>Distance</th>
                <th>Gain</th>
                <th>Peak Slope</th>
                <th>Crossings</th>
                <th>Suitability</th>
              </tr>
            </thead>
            <tbody>
              ${tracks
                .map((entry, idx) => {
                  const s = entry.summary;
                  const col = sanitizeHexColor(
                    entry.color_hex,
                    COMPARE_FALLBACK_COLORS[idx % COMPARE_FALLBACK_COLORS.length]!
                  );
                  const gCol = getGradeColor(s.grade);
                  const nameCell = canCallServer
                    ? `<button type="button" class="tfm-btn tfm-btn-sm" data-inspect-track="${escapeHtml(s.track_id)}"><span class="tfm-pill-dot" style="background:${col}"></span>${escapeHtml(s.name)}</button>`
                    : `<span><span class="tfm-pill-dot" style="display:inline-block;margin-right:6px;background:${col}"></span><strong>${escapeHtml(s.name)}</strong></span>`;
                  return `
                    <tr>
                      <td>${nameCell}</td>
                      <td><span class="mono" style="color:${gCol};font-weight:700">${escapeHtml(s.grade ?? "Easy")}</span></td>
                      <td class="mono">${formatKm(s.total_length_m)}</td>
                      <td class="mono">+${Math.round(s.elev_gain_m ?? 0)} m</td>
                      <td class="mono">${typeof s.max_slope_deg === "number" ? `${s.max_slope_deg.toFixed(1)}°` : "—"}</td>
                      <td class="mono">${s.waterway_crossing_count ?? 0}</td>
                      <td>${renderSuitabilityBadge(s.suitability?.rating)}</td>
                    </tr>
                  `;
                })
                .join("")}
            </tbody>
          </table>
        </div>
      </section>
    `;

    modeBodyHtml = `
      ${elevationHtml}
      ${tableHtml}
    `;
  } else if (payload.explore) {
    const exp = payload.explore;
    const rainHtml = exp.rainfall_summary
      ? `<span class="tfm-pill info">Rain 72h: ${typeof exp.rainfall_summary.rain_72h_mm === "number" ? `${exp.rainfall_summary.rain_72h_mm.toFixed(1)} mm` : "—"} (${escapeHtml(exp.rainfall_summary.wet_risk_level)})</span>`
      : "";

    const tracksHtml =
      exp.tracks.length > 0
        ? exp.tracks
            .map((t) => {
              const col = getGradeColor(t.grade);
              const tag = canCallServer ? "button" : "div";
              const extraAttrs = canCallServer
                ? `type="button" class="tfm-track-card clickable" data-inspect-track="${escapeHtml(t.track_id)}"`
                : `class="tfm-track-card"`;
              const meta = [
                t.state ?? null,
                formatKm(t.total_length_m),
                typeof t.elev_gain_m === "number"
                  ? `+${Math.round(t.elev_gain_m)}m`
                  : null,
                typeof t.max_slope_deg === "number"
                  ? `max ${t.max_slope_deg.toFixed(1)}°`
                  : null,
                t.closure_status && t.closure_status !== "open"
                  ? t.closure_status.toUpperCase()
                  : null,
              ]
                .filter(Boolean)
                .join(" · ");
              return `
                <${tag} ${extraAttrs}>
                  <div>
                    <div class="tfm-track-name">${escapeHtml(t.name)}</div>
                    <div class="tfm-track-meta mono">${escapeHtml(meta)}</div>
                  </div>
                  <div class="tfm-pill-row">
                    ${renderSuitabilityBadge(t.suitability?.rating)}
                    <span class="tfm-pill" style="border-color:${col};color:${col}">${escapeHtml(t.grade ?? "Easy")}</span>
                  </div>
                </${tag}>
              `;
            })
            .join("")
        : `<div class="tfm-empty">No matching trails in this area.</div>`;

    const alertsHtml = renderClosuresAndWarningsHtml(
      exp.closures ?? [],
      exp.reports ?? [],
      canOpenLinks
    );

    const poisHtml =
      exp.pois.length > 0
        ? `
          <section class="tfm-card" data-role="pois-section">
            <div class="tfm-card-head">
              <h3 class="tfm-card-title">Nearby Camps, Huts &amp; POIs</h3>
              <span class="mono">${exp.pois.length}</span>
            </div>
            <div class="tfm-pill-row">
              ${exp.pois
                .slice(0, 12)
                .map(
                  (p) =>
                    `<span class="tfm-pill">${escapeHtml(p.name)} <span class="mono">(${escapeHtml(p.category)})</span></span>`
                )
                .join("")}
            </div>
          </section>
        `
        : "";

    modeBodyHtml = `
      <section class="tfm-card" data-role="explore-tracks-section">
        <div class="tfm-card-head">
          <h3 class="tfm-card-title">Trails in Area${canCallServer ? " (Click to Inspect)" : ""}</h3>
          <div class="tfm-pill-row">
            ${rainHtml}
            <span class="mono">${exp.tracks.length} tracks</span>
          </div>
        </div>
        <div class="tfm-track-list">${tracksHtml}</div>
      </section>
      ${alertsHtml}
      ${poisHtml}
    `;
  }

  const deepLinkSafe = sanitizeExternalHttpUrl(payload.deep_link_url);

  const html = `
    <div class="tfm-shell" data-status="ready" data-view-mode="${escapeHtml(payload.view_mode)}" data-renderer="${state.rendererMode}">
      <header class="tfm-header">
        <div class="tfm-title-group">
          <div class="tfm-kicker-row">
            <span class="tfm-brand-dot"></span>
            <span>TrailForge Explorer</span>
            <span>·</span>
            <span class="mono">${escapeHtml(payload.view_mode.toUpperCase())}</span>
          </div>
          <h1 class="tfm-title">${escapeHtml(payload.title)}</h1>
          ${payload.subtitle ? `<p class="tfm-subtitle">${escapeHtml(payload.subtitle)}</p>` : ""}
          <div class="tfm-pill-row" style="margin-top:4px">
            ${gradeChipHtml}
            ${renderSuitabilityBadge(topSuitability, payload.rig_context?.capability_class)}
            ${rigChipHtml}
          </div>
        </div>

        <div class="tfm-actions" data-role="header-actions">
          ${
            state.previousExplorePayload
              ? `<button type="button" class="tfm-btn" data-action="back-to-explore">← Back to Area</button>`
              : ""
          }
          ${
            canUpdateContext
              ? `<button type="button" class="tfm-btn" data-action="add-to-context">＋ Add to AI Context</button>`
              : ""
          }
          ${
            canOpenLinks && deepLinkSafe
              ? `<button type="button" class="tfm-btn tfm-btn-primary" data-action="open-trailforge" data-open-url="${escapeHtml(deepLinkSafe)}">Open in TrailForge ↗</button>`
              : ""
          }
        </div>
      </header>

      ${
        state.actionToast
          ? `<div class="tfm-status-banner" role="status" data-role="action-toast">${escapeHtml(state.actionToast)}</div>`
          : ""
      }

      <section class="tfm-card" data-role="map-section">
        <div class="tfm-card-head">
          <h3 class="tfm-card-title">Interactive Trail Map</h3>
          <div class="tfm-pill-row">
            <span class="tfm-pill mono" data-role="renderer-badge">${state.rendererMode === "maplibre" ? "MapLibre GL 5" : "Vector SVG (Zero-Network)"}</span>
          </div>
        </div>
        <div class="tfm-map-stage" data-role="map-stage">
          <div class="tfm-map-canvas ${state.rendererMode === "maplibre" ? "" : "hidden"}" data-role="maplibre-container"></div>
          <div class="tfm-vector-map-wrap ${state.rendererMode === "svg" ? "" : "hidden"}" data-role="svg-map-wrap">
            <div class="tfm-map-toolbar" data-role="svg-zoom-controls">
              <button type="button" class="tfm-zoom-btn" data-zoom="in" aria-label="Zoom in">+</button>
              <button type="button" class="tfm-zoom-btn" data-zoom="out" aria-label="Zoom out">−</button>
              <button type="button" class="tfm-zoom-btn" data-zoom="reset" aria-label="Reset zoom">Reset</button>
            </div>
            ${vectorMap.svgHtml}
          </div>
        </div>
      </section>

      ${modeBodyHtml}

      ${renderAttributionFooter(payload.attributions)}
    </div>
  `;

  return {
    html,
    vectorMap,
    elevationGeometry,
    elevationPoints,
  };
}

/**
 * Build concise grounded markdown context for `app.updateModelContext(...)`.
 */
export function buildModelContextSummary(payload: TrailForgeViewPayload): string {
  const lines: string[] = [
    `# TrailForge Explorer Context (${payload.view_mode})`,
    `- Title: ${payload.title}`,
  ];
  if (payload.rig_context) {
    lines.push(
      `- Active Rig: ${payload.rig_context.vehicle_name ? `${payload.rig_context.vehicle_name} (${payload.rig_context.capability_class})` : payload.rig_context.capability_class}`
    );
  }
  if (payload.view_mode === "track" && payload.track) {
    const s = payload.track.summary;
    lines.push(
      `- Track ID: ${s.track_id}`,
      `- Grade: ${s.grade ?? "Unrated"} (${formatKm(s.total_length_m)}, +${Math.round(s.elev_gain_m ?? 0)} m gain, peak slope ${typeof s.max_slope_deg === "number" ? `${s.max_slope_deg.toFixed(1)}°` : "n/a"})`
    );
  } else if (payload.view_mode === "route" && payload.route) {
    const r = payload.route;
    lines.push(
      `- Route: ${r.name} (${r.legs.length} legs, ${formatKm(r.total_length_m)}, worst grade ${r.worst_grade ?? "Easy"})`,
      `- Leg Track IDs: ${r.legs.map((l) => l.track_id).join(", ")}`
    );
  } else if (payload.view_mode === "compare" && payload.compare) {
    lines.push(
      `- Compared Tracks: ${payload.compare.tracks.map((t) => `${t.summary.name} (${t.summary.track_id}, ${t.summary.grade ?? "Unrated"})`).join("; ")}`
    );
  } else if (payload.explore) {
    lines.push(
      `- Area Tracks (${payload.explore.tracks.length}): ${payload.explore.tracks.slice(0, 8).map((t) => `${t.name} (${t.track_id}, ${t.grade ?? "Unrated"})`).join("; ")}`
    );
  }
  lines.push(`- Deep Link: ${payload.deep_link_url}`);
  return lines.join("\n");
}

async function defaultCopyText(value: string): Promise<void> {
  if (
    typeof navigator !== "undefined" &&
    navigator.clipboard &&
    typeof navigator.clipboard.writeText === "function"
  ) {
    await navigator.clipboard.writeText(value);
    return;
  }
  throw new Error("Clipboard API unavailable in this environment.");
}

/**
 * Main SEP-1865 TrailForge Explorer MCP App controller.
 * Strictly registers `ontoolinput`, `ontoolresult`, `ontoolcancelled`, and
 * `onhostcontextchanged` BEFORE calling `await app.connect()`.
 */
export class TrailForgeMcpApp {
  private readonly root: HTMLElement | null;
  private readonly bridge: McpAppBridge;
  private readonly search: string;
  private readonly themeHooks: {
    applyDocumentTheme: typeof applyDocumentTheme;
    applyHostStyleVariables: typeof applyHostStyleVariables;
    applyHostFonts: typeof applyHostFonts;
  };
  private readonly copyText: (value: string) => Promise<void>;
  private mapLibreCtrl: MapLibreOverlayController | null = null;
  private state: ExplorerWidgetState;
  private handlersRegisteredBeforeConnect = false;
  private connected = false;

  constructor(options: TrailForgeMcpAppOptions = {}) {
    this.root = options.root ?? null;
    this.bridge =
      options.bridge ??
      (new App(
        { name: "TrailForge Explorer", version: "1.2.0" },
        {}
      ) as unknown as McpAppBridge);
    this.search =
      options.search ??
      (typeof window !== "undefined" ? window.location.search : "");
    this.themeHooks = options.themeHooks ?? {
      applyDocumentTheme,
      applyHostStyleVariables,
      applyHostFonts,
    };
    this.copyText = options.copyText ?? defaultCopyText;

    const params = new URLSearchParams(
      this.search.startsWith("?") ? this.search.slice(1) : this.search
    );
    const demoMode = params.get("demo");
    const themeParam = params.get("theme");
    const modeParam = params.get("mode");
    const initialTheme: "light" | "dark" =
      themeParam === "light" ? "light" : "dark";
    const forceSvg =
      Boolean(options.forceSvgFallback) || modeParam === "svg";

    const demoPayload = getDemoPayload(demoMode);
    this.state = {
      status: demoPayload ? "ready" : "idle",
      statusMessage: null,
      payload: demoPayload,
      previousExplorePayload: null,
      theme: initialTheme,
      capabilities: {
        openLinks: false,
        updateModelContext: false,
        serverTools: false,
      },
      rendererMode: "svg",
      forceSvgFallback: forceSvg,
      standalonePreview: Boolean(demoPayload),
      actionToast: null,
    };
  }

  getState(): Readonly<ExplorerWidgetState> {
    return this.state;
  }

  wereHandlersRegisteredBeforeConnect(): boolean {
    return this.handlersRegisteredBeforeConnect;
  }

  /**
   * Register all SEP-1865 event handlers FIRST, then perform `await app.connect()`.
   */
  async start(): Promise<void> {
    if (this.state.theme === "light" && typeof document !== "undefined") {
      this.themeHooks.applyDocumentTheme("light");
    }

    // 1. Register ALL notification handlers BEFORE calling app.connect()
    this.bridge.ontoolinput = (params) => {
      const args = params?.arguments ?? {};
      const hint =
        typeof args.track_name === "string"
          ? `Inspecting ${args.track_name}…`
          : typeof args.query === "string"
            ? `Searching trails for “${args.query}”…`
            : typeof args.region === "string"
              ? `Loading ${args.region} trail intelligence…`
              : "Loading trail intelligence…";
      this.state = {
        ...this.state,
        status: "loading",
        statusMessage: hint,
        actionToast: null,
      };
      this.render();
    };

    this.bridge.ontoolresult = (result) => {
      if (result.isError) {
        const msg =
          result.content?.find((b) => b.type === "text")?.text ??
          "Tool returned an error.";
        this.state = {
          ...this.state,
          status: "error",
          statusMessage: String(msg),
        };
        this.render();
        return;
      }
      const parsed = extractPayloadFromToolResult(
        result as {
          isError?: boolean;
          structuredContent?: Record<string, unknown>;
          content?: Array<{ type: string; text?: string }>;
        }
      );
      if (!parsed) {
        this.state = {
          ...this.state,
          status: "error",
          statusMessage:
            "Received tool result without a valid TrailForgeViewPayload.",
        };
        this.render();
        return;
      }
      this.applyPayload(parsed, false);
    };

    this.bridge.ontoolcancelled = (params) => {
      this.state = {
        ...this.state,
        status: "cancelled",
        statusMessage: params?.reason
          ? `Cancelled: ${params.reason}`
          : "Tool execution was cancelled.",
      };
      this.render();
    };

    this.bridge.onhostcontextchanged = (ctx) => {
      const nextTheme = applyHostThemeContext(ctx, this.themeHooks);
      if (nextTheme && nextTheme !== this.state.theme) {
        this.state = { ...this.state, theme: nextTheme };
        this.render();
      }
    };

    this.handlersRegisteredBeforeConnect = !this.connected;

    // Standalone preview mode (e.g. `?demo=route` opened directly in a browser tab)
    if (this.state.standalonePreview && typeof window !== "undefined" && window.parent === window) {
      this.render();
      return;
    }

    // 2. Connect to host AFTER all handlers are registered
    try {
      await this.bridge.connect();
      this.connected = true;
      const caps = resolveHostCapabilities(this.bridge.getHostCapabilities());
      const ctx = this.bridge.getHostContext();
      const appliedTheme = applyHostThemeContext(ctx, this.themeHooks);
      this.state = {
        ...this.state,
        capabilities: caps,
        theme: appliedTheme ?? this.state.theme,
      };
      this.render();
    } catch (err) {
      console.warn("MCP App connect() failed (running in standalone preview mode):", err);
      this.render();
    }
  }

  /**
   * Programmatically render any `TrailForgeViewPayload` (also exposed on
   * `window.__TRAILFORGE_MCP_APP__` for browser verification).
   */
  applyPayload(payload: TrailForgeViewPayload, preserveBackStack = false): void {
    const canTryMapLibre =
      !this.state.forceSvgFallback &&
      hasUsableTileConfig(payload) &&
      detectWebGLSupport();

    this.state = {
      ...this.state,
      status: "ready",
      statusMessage: null,
      previousExplorePayload: preserveBackStack
        ? this.state.previousExplorePayload
        : null,
      payload,
      rendererMode: canTryMapLibre ? "maplibre" : "svg",
      actionToast: null,
    };
    this.render();
  }

  setTheme(theme: "light" | "dark"): void {
    if (typeof document !== "undefined") {
      this.themeHooks.applyDocumentTheme(theme);
    }
    this.state = { ...this.state, theme };
    this.render();
  }

  setCapabilities(caps: Partial<HostCapabilityFlags>): void {
    this.state = {
      ...this.state,
      capabilities: { ...this.state.capabilities, ...caps },
    };
    this.render();
  }

  async openExternalUrl(url: string): Promise<void> {
    const safe = sanitizeExternalHttpUrl(url);
    if (!safe) return;
    if (this.state.capabilities.openLinks) {
      try {
        const res = await this.bridge.openLink({ url: safe });
        if (res?.isError) {
          this.state = {
            ...this.state,
            actionToast: `Host blocked external link: ${safe}`,
          };
          this.render();
        }
      } catch (err) {
        console.warn("app.openLink failed:", err);
        this.state = {
          ...this.state,
          actionToast: `Could not open link via host: ${safe}`,
        };
        this.render();
      }
    } else if (this.state.standalonePreview && typeof window !== "undefined") {
      window.open(safe, "_blank", "noopener,noreferrer");
    }
  }

  async pushSelectionToModelContext(): Promise<void> {
    if (!this.state.capabilities.updateModelContext || !this.state.payload) {
      return;
    }
    const payload = this.state.payload;
    const text = buildModelContextSummary(payload);
    try {
      await this.bridge.updateModelContext({
        content: [{ type: "text", text }],
        structuredContent: {
          view_mode: payload.view_mode,
          title: payload.title,
          deep_link_url: payload.deep_link_url,
          track_id: payload.track?.summary.track_id ?? null,
          route_track_ids: payload.route?.legs.map((l) => l.track_id) ?? [],
        },
      });
      this.state = {
        ...this.state,
        actionToast: "Added current TrailForge view to your next AI message context.",
      };
      this.render();
    } catch (err) {
      console.warn("app.updateModelContext failed:", err);
      this.state = {
        ...this.state,
        actionToast: "Unable to attach context in this host view.",
      };
      this.render();
    }
  }

  async inspectTrackById(trackId: string): Promise<void> {
    if (!this.state.capabilities.serverTools || !trackId.trim()) {
      return;
    }
    const prevExplore =
      this.state.payload?.view_mode === "explore"
        ? this.state.payload
        : this.state.previousExplorePayload;
    const rigClass = this.state.payload?.rig_context?.capability_class;

    this.state = {
      ...this.state,
      status: "loading",
      statusMessage: `Loading track ${trackId}…`,
      previousExplorePayload: prevExplore,
    };
    this.render();

    try {
      const result = await this.bridge.callServerTool({
        name: "app_get_track_detail",
        arguments: rigClass
          ? { track_id: trackId, vehicle_class: rigClass }
          : { track_id: trackId },
      });
      if (result.isError) {
        throw new Error("Server returned an error for track detail.");
      }
      const parsed = extractPayloadFromToolResult(result);
      if (!parsed) {
        throw new Error("Track detail payload missing.");
      }
      this.applyPayload(parsed, true);
    } catch (err) {
      console.warn("app_get_track_detail failed:", err);
      this.state = {
        ...this.state,
        status: prevExplore ? "ready" : "error",
        payload: prevExplore ?? this.state.payload,
        statusMessage: "Could not load track detail from server.",
        actionToast: "Could not load track detail from server.",
      };
      this.render();
    }
  }

  destroy(): void {
    this.mapLibreCtrl?.destroy();
    this.mapLibreCtrl = null;
  }

  private render(): void {
    if (!this.root) return;
    this.mapLibreCtrl?.destroy();
    this.mapLibreCtrl = null;

    const rendered = renderExplorerApp(this.state);
    this.root.innerHTML = rendered.html;
    this.bindDomEvents(rendered);
  }

  private bindDomEvents(rendered: RenderedExplorerView): void {
    if (!this.root) return;

    // Capability-gated action buttons
    this.root
      .querySelectorAll<HTMLButtonElement>("[data-open-url]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const url = btn.dataset.openUrl;
          if (url) void this.openExternalUrl(url);
        });
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-action='add-to-context']")
      ?.addEventListener("click", () => {
        void this.pushSelectionToModelContext();
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-action='back-to-explore']")
      ?.addEventListener("click", () => {
        if (this.state.previousExplorePayload) {
          this.applyPayload(this.state.previousExplorePayload, false);
        }
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-copy-cli]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const cli = btn.dataset.copyCli ?? "";
          void this.copyText(cli)
            .then(() => {
              this.state = {
                ...this.state,
                actionToast: "Copied GridForge CLI command to clipboard.",
              };
              this.render();
            })
            .catch((err) => {
              console.warn("Clipboard copy failed for GridForge CLI:", err);
              this.state = {
                ...this.state,
                actionToast: "Select and copy the GridForge CLI command manually.",
              };
              this.render();
            });
        });
      });

    // Interactive track inspection triggers (cards + SVG polylines)
    if (this.state.capabilities.serverTools) {
      this.root
        .querySelectorAll<HTMLElement>("[data-inspect-track]")
        .forEach((el) => {
          el.addEventListener("click", () => {
            const tid = el.dataset.inspectTrack;
            if (tid) void this.inspectTrackById(tid);
          });
        });

      this.root
        .querySelectorAll<SVGElement>("[data-map-track-id]")
        .forEach((el) => {
          el.addEventListener("click", (ev) => {
            ev.stopPropagation();
            const tid = el.getAttribute("data-map-track-id");
            if (tid) void this.inspectTrackById(tid);
          });
        });
    }

    // Interactive Pan/Zoom + Lockstep Elevation <-> Map Scrubbing
    const { syncAtDistance } = this.bindSvgPanZoomAndScrub(rendered);

    // Hybrid MapLibre GL mount with automatic silent fallback to SVG
    const payload = this.state.payload;
    if (payload && this.state.rendererMode === "maplibre") {
      const mlContainer = this.root.querySelector<HTMLElement>(
        "[data-role='maplibre-container']"
      );
      if (mlContainer) {
        this.mapLibreCtrl = mountMapLibreMap({
          container: mlContainer,
          payload,
          theme: this.state.theme,
          onTrackClick: (trackId) => {
            if (this.state.capabilities.serverTools) {
              void this.inspectTrackById(trackId);
            }
          },
          onHoverCoord: (lon, lat) => {
            if (
              !syncAtDistance ||
              !rendered.vectorMap ||
              rendered.vectorMap.vertices.length < 2
            ) {
              return;
            }
            const projected = rendered.vectorMap.project(lon, lat);
            const nearest = findNearestPointOnTrackCourse(
              rendered.vectorMap.vertices,
              projected.x,
              projected.y
            );
            if (nearest) {
              syncAtDistance(nearest.distM);
            }
          },
          onFallback: (reason) => {
            console.info("TrailForge MCP switching to zero-network SVG map:", reason);
            this.mapLibreCtrl = null;
            this.state = { ...this.state, rendererMode: "svg" };
            this.root
              ?.querySelector<HTMLElement>(".tfm-shell")
              ?.setAttribute("data-renderer", "svg");
            this.root
              ?.querySelector<HTMLElement>("[data-role='maplibre-container']")
              ?.classList.add("hidden");
            this.root
              ?.querySelector<HTMLElement>("[data-role='svg-map-wrap']")
              ?.classList.remove("hidden");
            const badge = this.root?.querySelector<HTMLElement>(
              "[data-role='renderer-badge']"
            );
            if (badge) badge.textContent = "Vector SVG (Zero-Network)";
          },
        });
      }
    }
  }

  private bindSvgPanZoomAndScrub(rendered: RenderedExplorerView): {
    syncAtDistance: ((distM: number) => void) | null;
  } {
    if (!this.root || !rendered.vectorMap) {
      return { syncAtDistance: null };
    }
    const vectorMap = rendered.vectorMap;
    const svgEl = this.root.querySelector<SVGSVGElement>(
      "[data-role='vector-map-svg']"
    );
    const panLayer = this.root.querySelector<SVGGElement>(
      "[data-role='map-panzoom-layer']"
    );
    const wrapEl = this.root.querySelector<HTMLElement>(
      "[data-role='svg-map-wrap']"
    );

    let zoomScale = 1;
    let panX = 0;
    let panY = 0;

    const applyTransform = (): void => {
      if (!panLayer) return;
      panLayer.setAttribute(
        "transform",
        `translate(${panX.toFixed(1)} ${panY.toFixed(1)}) scale(${zoomScale.toFixed(3)})`
      );
    };

    const setZoomAroundCenter = (nextScale: number): void => {
      const clamped = Math.max(1, Math.min(8, nextScale));
      const cx = vectorMap.width / 2;
      const cy = vectorMap.height / 2;
      const factor = clamped / zoomScale;
      panX = cx - factor * (cx - panX);
      panY = cy - factor * (cy - panY);
      zoomScale = clamped;
      if (zoomScale === 1) {
        panX = 0;
        panY = 0;
      }
      applyTransform();
    };

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-zoom]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const action = btn.dataset.zoom;
          if (action === "in") setZoomAroundCenter(zoomScale * 1.4);
          else if (action === "out") setZoomAroundCenter(zoomScale / 1.4);
          else {
            zoomScale = 1;
            panX = 0;
            panY = 0;
            applyTransform();
          }
        });
      });

    if (svgEl && wrapEl) {
      svgEl.addEventListener(
        "wheel",
        (ev) => {
          ev.preventDefault();
          const factor = ev.deltaY < 0 ? 1.2 : 1 / 1.2;
          setZoomAroundCenter(zoomScale * factor);
        },
        { passive: false }
      );

      let dragging = false;
      let startClientX = 0;
      let startClientY = 0;
      let startPanX = 0;
      let startPanY = 0;

      svgEl.addEventListener("pointerdown", (ev) => {
        if (ev.button !== 0) return;
        dragging = true;
        startClientX = ev.clientX;
        startClientY = ev.clientY;
        startPanX = panX;
        startPanY = panY;
        wrapEl.classList.add("dragging");
      });

      const stopDrag = (): void => {
        dragging = false;
        wrapEl.classList.remove("dragging");
      };

      svgEl.addEventListener("pointerup", stopDrag);
      svgEl.addEventListener("pointerleave", stopDrag);

      svgEl.addEventListener("pointermove", (ev) => {
        const rect = svgEl.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return;
        if (dragging && zoomScale > 1) {
          const dx = ((ev.clientX - startClientX) / rect.width) * vectorMap.width;
          const dy = ((ev.clientY - startClientY) / rect.height) * vectorMap.height;
          panX = startPanX + dx;
          panY = startPanY + dy;
          applyTransform();
          return;
        }
        // Hover over SVG course syncs to elevation profile when vertices exist
        if (vectorMap.vertices.length >= 2 && rendered.elevationGeometry) {
          const rawX = ((ev.clientX - rect.left) / rect.width) * vectorMap.width;
          const rawY = ((ev.clientY - rect.top) / rect.height) * vectorMap.height;
          const unzoomedX = (rawX - panX) / zoomScale;
          const unzoomedY = (rawY - panY) / zoomScale;
          const nearest = findNearestPointOnTrackCourse(
            vectorMap.vertices,
            unzoomedX,
            unzoomedY
          );
          if (nearest) {
            syncAtDistance(nearest.distM);
          }
        }
      });
    }

    // Synchronized elevation scrubbing
    const geom = rendered.elevationGeometry;
    const elevSvg = this.root.querySelector<SVGSVGElement>(".tf-elev-svg");
    const elevHit = this.root.querySelector<SVGRectElement>(
      "[data-role='elev-scrub-hit']"
    );
    const crosshair = this.root.querySelector<SVGGElement>(
      "[data-role='elev-crosshair']"
    );
    const crossLine = this.root.querySelector<SVGLineElement>(
      "[data-role='crosshair-line']"
    );
    const crossHalo = this.root.querySelector<SVGCircleElement>(
      "[data-role='crosshair-halo']"
    );
    const crossDot = this.root.querySelector<SVGCircleElement>(
      "[data-role='crosshair-dot']"
    );
    const hud = this.root.querySelector<HTMLElement>("[data-role='scrub-hud']");

    const svgCursor = this.root.querySelector<SVGGElement>(
      "[data-role='svg-map-cursor']"
    );
    const svgGlow = this.root.querySelector<SVGCircleElement>(
      "[data-role='svg-cursor-glow']"
    );
    const svgRing = this.root.querySelector<SVGCircleElement>(
      "[data-role='svg-cursor-ring']"
    );
    const svgDot = this.root.querySelector<SVGCircleElement>(
      "[data-role='svg-cursor-dot']"
    );

    const payload = this.state.payload;
    if (!geom || !elevSvg || !elevHit || !crosshair || !crossLine || !hud || !payload) {
      return { syncAtDistance: null };
    }

    const syncAtDistance = (distM: number): void => {
      if (payload.view_mode === "compare" && payload.compare) {
        const clamped = Math.max(0, Math.min(geom.totalDistM, distM));
        const x = geom.padL + (clamped / Math.max(1, geom.totalDistM)) * geom.plotW;
        crossLine.setAttribute("x1", x.toFixed(1));
        crossLine.setAttribute("x2", x.toFixed(1));
        crosshair.classList.remove("hidden");

        const items = payload.compare.tracks
          .map((t, idx) => {
            const sample = sampleElevationAtDistance(
              t.elevation_profile.map((p) => ({
                dist_m: p.dist_m,
                elev_m: p.elev_m,
                slope_deg: p.slope_deg,
                grade: p.grade,
              })),
              clamped
            );
            if (!sample) return "";
            const col = sanitizeHexColor(
              t.color_hex,
              COMPARE_FALLBACK_COLORS[idx % COMPARE_FALLBACK_COLORS.length]!
            );
            return `<span class="mono"><span class="tfm-pill-dot" style="display:inline-block;background:${col};margin-right:4px"></span>${escapeHtml(t.summary.name)}: <strong>${Math.round(sample.elevM)}m</strong></span>`;
          })
          .filter(Boolean)
          .join("");
        hud.innerHTML = `<span class="mono"><span class="tfm-hud-k">DIST</span><strong>${(clamped / 1000).toFixed(2)} km</strong></span>${items}`;
        return;
      }

      const sample = sampleElevationAtDistance(
        rendered.elevationPoints.map((p) => ({
          dist_m: p.dist_m,
          elev_m: p.elev_m,
          slope_deg: p.slope_deg,
          grade: p.grade,
          segment_id: p.segment_id ?? null,
        })),
        distM
      );
      if (!sample) return;

      const x = geom.padL + (sample.distM / Math.max(1, geom.totalDistM)) * geom.plotW;
      const y =
        geom.padT +
        (1 - (sample.elevM - geom.yMin) / Math.max(1, geom.yMax - geom.yMin)) *
          geom.plotH;
      const color = getGradeColor(sample.grade);

      crossLine.setAttribute("x1", x.toFixed(1));
      crossLine.setAttribute("x2", x.toFixed(1));
      if (crossHalo) {
        crossHalo.setAttribute("cx", x.toFixed(1));
        crossHalo.setAttribute("cy", y.toFixed(1));
      }
      if (crossDot) {
        crossDot.setAttribute("cx", x.toFixed(1));
        crossDot.setAttribute("cy", y.toFixed(1));
        crossDot.setAttribute("fill", color);
      }
      crosshair.classList.remove("hidden");

      const mapPt = sampleTrackCourseAtDistance(vectorMap.vertices, sample.distM);
      if (mapPt) {
        if (svgCursor && svgGlow && svgRing && svgDot) {
          svgGlow.setAttribute("cx", mapPt.x.toFixed(1));
          svgGlow.setAttribute("cy", mapPt.y.toFixed(1));
          svgRing.setAttribute("cx", mapPt.x.toFixed(1));
          svgRing.setAttribute("cy", mapPt.y.toFixed(1));
          svgDot.setAttribute("cx", mapPt.x.toFixed(1));
          svgDot.setAttribute("cy", mapPt.y.toFixed(1));
          svgDot.setAttribute("fill", color);
          svgCursor.classList.remove("hidden");
        }
        this.mapLibreCtrl?.setCursorPosition(mapPt.lon, mapPt.lat, color);
      }

      const aboveSnow = sample.elevM >= payload.snowline_m;
      hud.innerHTML = `
        <span class="mono"><span class="tfm-hud-k">DIST</span><strong>${(sample.distM / 1000).toFixed(2)} km</strong></span>
        <span class="mono"><span class="tfm-hud-k">ELEV</span><strong>${Math.round(sample.elevM)} m</strong></span>
        <span class="mono"><span class="tfm-hud-k">SLOPE</span><strong>${sample.slopeDeg !== null ? `${sample.slopeDeg.toFixed(1)}°` : "—"}</strong></span>
        ${mapPt ? `<span class="mono"><span class="tfm-hud-k">COORDS</span><strong>${mapPt.lat.toFixed(4)}, ${mapPt.lon.toFixed(4)}</strong></span>` : ""}
        <span class="tfm-pill" style="border-color:${color};color:${color}">${escapeHtml(sample.grade)}</span>
        ${aboveSnow ? `<span class="tfm-pill info">Above ${payload.snowline_m}m snow-line</span>` : ""}
      `;
    };

    const handleElevPointer = (clientX: number): void => {
      const rect = elevSvg.getBoundingClientRect();
      if (rect.width <= 0) return;
      const svgX = ((clientX - rect.left) / rect.width) * geom.width;
      const clampedX = Math.max(geom.padL, Math.min(geom.padL + geom.plotW, svgX));
      const frac = (clampedX - geom.padL) / Math.max(1, geom.plotW);
      syncAtDistance(frac * geom.totalDistM);
    };

    elevHit.addEventListener("pointermove", (ev) => handleElevPointer(ev.clientX));
    elevHit.addEventListener("pointerdown", (ev) => handleElevPointer(ev.clientX));

    return { syncAtDistance };
  }
}
