import { CourseMiniMap } from "./courseMiniMap";
import {
  buildElevationProfileSvg,
  buildTrackCourseSvg,
  computeTrackGradeDistribution,
  findNearestPointOnTrackCourse,
  formatBreakdownFactors,
  formatOverrideLabel,
  normalizeTrackInspector,
  sampleElevationAtDistance,
  sampleTrackCourseAtDistance,
  type ElevationChartGeometry,
  type NormalizedTrackSummary,
  type TrackCoursePoint,
  type TrackCourseSvgResult,
} from "../inspectorHelpers";
import { escapeHtml, getGradeColor } from "../presets";
import type {
  BasemapId,
  BBoxTuple,
  SegmentSummaryItem,
  TileManifestResponse,
  TrackInspectorDetail,
} from "../types";

export interface ElevationModalCallbacks {
  onZoomToTrack?: (bbox: BBoxTuple) => void;
  onExportTrack?: (trackId: string, format: "gpx" | "geojson") => void;
  onShareTrack?: (trackId: string) => void;
  onOpenStateChange?: (isOpen: boolean) => void;
}

export interface ElevationModalOpenOptions {
  snowLineElevationM: number;
  activeBasemap?: BasemapId;
  tileManifest?: TileManifestResponse | null;
  isOfflinePack?: boolean;
}

/**
 * Large-scale interactive Terrain Elevation Profile & Synchronized Track Course Pop-Over Modal.
 * Embeds a live MapLibre topographic/satellite mini-map alongside the Elevation Profile so
 * scrubbing either graph moves both position markers in lockstep over the real map.
 */
export class ElevationProfileModal {
  private readonly backdropEl: HTMLDivElement;
  private readonly callbacks: ElevationModalCallbacks;
  private isOpen = false;
  private miniMap: CourseMiniMap | null = null;
  private readonly keydownHandler: (e: KeyboardEvent) => void;

  constructor(callbacks: ElevationModalCallbacks = {}) {
    this.callbacks = callbacks;
    this.backdropEl = document.createElement("div");
    this.backdropEl.className = "tf-elev-modal-backdrop hidden";
    this.backdropEl.setAttribute("role", "dialog");
    this.backdropEl.setAttribute("aria-modal", "true");
    this.backdropEl.setAttribute(
      "aria-label",
      "Expanded terrain elevation profile and track map"
    );
    document.body.appendChild(this.backdropEl);

    this.backdropEl.addEventListener("click", (e) => {
      if (e.target === this.backdropEl) {
        this.close();
      }
    });

    this.keydownHandler = (e: KeyboardEvent) => {
      if (e.key === "Escape" && this.isOpen) {
        e.stopPropagation();
        this.close();
      }
    };
    document.addEventListener("keydown", this.keydownHandler);
  }

  isModalOpen(): boolean {
    return this.isOpen;
  }

  open(
    detail: TrackInspectorDetail,
    snowLineOrOptions: number | ElevationModalOpenOptions
  ): void {
    const opts: ElevationModalOpenOptions =
      typeof snowLineOrOptions === "number"
        ? { snowLineElevationM: snowLineOrOptions }
        : snowLineOrOptions;
    const snowLineElevationM = opts.snowLineElevationM;
    const activeBasemap: BasemapId = opts.activeBasemap ?? "opentopo";

    this.destroyMiniMap();
    const wasOpen = this.isOpen;
    const norm = normalizeTrackInspector(detail);
    const chart = buildElevationProfileSvg(norm.profile, {
      width: 920,
      height: 340,
      snowLineElevationM,
      crossings: norm.crossings,
      detailed: true,
      gradientId: "tfElevGradModal",
    });

    const segments = norm.canonicalSegments;
    const segById = new Map<string, SegmentSummaryItem>();
    for (const seg of detail.segments ?? []) {
      segById.set(seg.segment_id, seg);
    }

    const course = buildTrackCourseSvg(segments, norm.profile, {
      width: 460,
      height: 340,
      crossings: norm.crossings,
      steepZones: chart.steepZones,
      precomputedVertices: norm.courseVertices,
      precomputedBBox: norm.courseBBox,
    });
    const hasCourse = course.vertices.length >= 2 && course.bbox !== null;

    const gradeColor = getGradeColor(norm.grade);
    const gradeDist = computeTrackGradeDistribution(segments, norm.profile);
    const factors = formatBreakdownFactors(norm.breakdown);
    const overrides = norm.breakdown?.overrides ?? [];
    const surfaceSet = Array.from(
      new Set(
        segments
          .map((s) => s.surface_class)
          .filter((v): v is string => Boolean(v && v !== "unknown"))
          .map((v) => v.replace(/_/g, " "))
      )
    );
    const elevRangeText =
      norm.minElevM !== null && norm.maxElevM !== null
        ? `${Math.round(norm.minElevM)}–${Math.round(norm.maxElevM)} m`
        : "—";
    const gainLossText =
      norm.elevLossM !== null
        ? `+${Math.round(norm.elevGainM)} / −${Math.round(norm.elevLossM)} m`
        : `+${Math.round(norm.elevGainM)} m`;
    const maxSlopePct =
      norm.maxSlopeDeg !== null
        ? `${Math.round(Math.tan((norm.maxSlopeDeg * Math.PI) / 180) * 100)}%`
        : null;
    const rainfall72h =
      typeof detail.rainfall_context?.rain_72h_mm === "number"
        ? `${detail.rainfall_context.rain_72h_mm.toFixed(1)} mm (72h)`
        : null;

    const gradeBarHtml =
      gradeDist.length > 0
        ? `
          <div class="tf-elev-grade-bar" aria-label="Track difficulty distribution">
            ${gradeDist
              .map(
                (item) =>
                  `<div class="tf-elev-grade-seg" style="width:${item.pct}%;background:${item.color}" title="${escapeHtml(item.grade)}: ${(item.lengthM / 1000).toFixed(2)} km (${item.pct}%)"></div>`
              )
              .join("")}
          </div>
          <div class="tf-elev-grade-legend">
            ${gradeDist
              .map(
                (item) => `
                  <span class="tf-elev-grade-pill">
                    <span class="tf-elev-grade-dot" style="background:${item.color}"></span>
                    <strong>${escapeHtml(item.grade)}</strong>
                    <span class="mono">${(item.lengthM / 1000).toFixed(1)} km (${item.pct}%)</span>
                  </span>
                `
              )
              .join("")}
          </div>
        `
        : `<p class="tf-inspector-muted">No segment-level grade split recorded.</p>`;

    const steepZonesHtml =
      chart.steepZones.length > 0
        ? chart.steepZones
            .slice(0, 24)
            .map((z) => {
              const pct = Math.round(Math.tan((z.peakSlopeDeg * Math.PI) / 180) * 100);
              const spanKm =
                z.endDistM > z.startDistM + 20
                  ? `${(z.startDistM / 1000).toFixed(2)}–${(z.endDistM / 1000).toFixed(2)} km`
                  : `${(z.peakDistM / 1000).toFixed(2)} km`;
              return `
                <button
                  type="button"
                  class="tf-elev-jump-row"
                  data-focus-dist="${z.peakDistM}"
                >
                  <span class="tf-elev-jump-dot" style="background:${getGradeColor(z.grade)}"></span>
                  <span class="tf-elev-jump-main">
                    <span class="tf-elev-jump-title">Zone #${z.index + 1} · ${escapeHtml(spanKm)}</span>
                    <span class="tf-elev-jump-sub">${Math.round(z.peakElevM)} m AHD · ${escapeHtml(z.grade)}</span>
                  </span>
                  <span class="tf-elev-jump-val mono">${z.peakSlopeDeg.toFixed(1)}° (${pct}%)</span>
                </button>
              `;
            })
            .join("")
        : `<p class="tf-inspector-muted">No steep pitches ≥14° detected along this track.</p>`;

    const crossingsHtml =
      norm.crossings.length > 0
        ? norm.crossings
            .map((c) => {
              const hasDist = typeof c.dist_m === "number" && Number.isFinite(c.dist_m);
              const distLabel = hasDist ? `${(c.dist_m! / 1000).toFixed(2)} km` : "—";
              const tags = [
                c.perennial ? "Perennial" : "Intermittent",
                c.bridged ? "Bridged" : "Unbridged Ford",
              ].join(" · ");
              return `
                <button
                  type="button"
                  class="tf-elev-jump-row"
                  ${hasDist ? `data-focus-dist="${c.dist_m}"` : "disabled"}
                >
                  <span class="tf-elev-jump-dot" style="background:#38BDF8"></span>
                  <span class="tf-elev-jump-main">
                    <span class="tf-elev-jump-title">${escapeHtml(c.name || "Unnamed watercourse")}</span>
                    <span class="tf-elev-jump-sub">${escapeHtml(tags)}</span>
                  </span>
                  <span class="tf-elev-jump-val mono">${escapeHtml(distLabel)}</span>
                </button>
              `;
            })
            .join("")
        : `<p class="tf-inspector-muted">No recorded waterway crossings along this track.</p>`;

    const factorsHtml = factors
      .map(
        (f) => `
          <div class="tf-elev-factor-item">
            <div class="tf-elev-factor-head">
              <span>${escapeHtml(f.label)}</span>
              <span class="mono">${f.points}/${f.maxPoints}</span>
            </div>
            <div class="tf-factor-track">
              <div class="tf-factor-fill" style="width:${f.pct}%;background:${gradeColor}"></div>
            </div>
            <div class="tf-elev-factor-detail">${escapeHtml(f.detail)}</div>
          </div>
        `
      )
      .join("");

    const miniBasemaps: Array<{ id: BasemapId; label: string }> = [
      { id: "opentopo", label: "Topo" },
      { id: "satellite", label: "Sat" },
      { id: "osm", label: "OSM" },
    ];

    const visualStageHtml = hasCourse
      ? `
        <div class="tf-elev-visual-split">
          <div class="tf-elev-pane tf-elev-pane-profile">
            <div class="tf-elev-pane-head">
              <span class="tf-elev-pane-title">Elevation Profile</span>
              <span class="tf-elev-pane-meta mono">0 → ${norm.totalLengthKm.toFixed(1)} km · Linked scrub</span>
            </div>
            <div class="tf-elev-stage-svg">
              ${chart.svgHtml}
            </div>
          </div>
          <div class="tf-elev-pane tf-elev-pane-course">
            <div class="tf-elev-pane-head">
              <span class="tf-elev-pane-title">Track Map (North-Up)</span>
              <div class="tf-mini-map-controls">
                <div class="tf-mini-basemap-group" role="group" aria-label="Track map basemap">
                  ${miniBasemaps
                    .map(
                      (b) =>
                        `<button type="button" class="tf-mini-bm-btn ${activeBasemap === b.id ? "active" : ""}" data-mini-basemap="${b.id}">${b.label}</button>`
                    )
                    .join("")}
                </div>
                <button type="button" class="tf-mini-bm-btn" data-mini-action="fit" title="Refit full track">⌖</button>
              </div>
            </div>
            <div class="tf-elev-course-map-wrap">
              <div class="tf-elev-course-map" data-role="course-minimap" aria-label="Interactive topographic track map synchronized with elevation profile"></div>
              <div class="tf-elev-course-svg-fallback hidden" data-role="course-svg-fallback">
                ${course.svgHtml}
              </div>
            </div>
          </div>
        </div>
      `
      : `
        <div class="tf-elev-stage-svg">
          ${chart.svgHtml}
        </div>
      `;

    this.backdropEl.innerHTML = `
      <div class="tf-elev-modal">
        <div class="tf-elev-modal-head">
          <div class="tf-elev-modal-title-group">
            <div class="tf-inspector-kicker">Terrain Elevation &amp; Synchronized Track Map</div>
            <div class="tf-elev-modal-title-row">
              <h2 class="tf-elev-modal-title">${escapeHtml(norm.name)}</h2>
              <span class="tf-elev-grade-chip" style="border-color:${gradeColor};color:${gradeColor}">
                <span class="tf-elev-grade-dot" style="background:${gradeColor}"></span>
                ${escapeHtml(norm.grade)}${norm.score !== null ? ` · ${Math.round(norm.score)}/100` : ""}
              </span>
              <span class="tf-pill mono">${escapeHtml(norm.state)}</span>
              ${norm.is4wdOnly ? `<span class="tf-pill tf-pill-amber">4WD Only</span>` : ""}
            </div>
          </div>

          <div class="tf-elev-modal-actions">
            <button type="button" class="tf-action-btn" data-modal-action="share" title="Copy shareable link to this track and profile view">🔗 Share</button>
            ${
              detail.bbox
                ? `<button type="button" class="tf-action-btn" data-modal-action="zoom">⌖ Zoom Map</button>`
                : ""
            }
            <button type="button" class="tf-action-btn" data-modal-action="gpx">⬇ GPX</button>
            <button type="button" class="tf-inspector-close" data-modal-action="close" aria-label="Close elevation profile window">×</button>
          </div>
        </div>

        <div class="tf-elev-modal-body">
          <div class="tf-elev-kpi-strip">
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
              <span class="tf-stat-label">Max / Sustained</span>
              <span class="tf-stat-val mono">${
                norm.maxSlopeDeg !== null
                  ? `${norm.maxSlopeDeg.toFixed(1)}°${maxSlopePct ? ` (${maxSlopePct})` : ""}`
                  : "—"
              }${norm.sustainedSlopeDeg !== null ? ` · ${norm.sustainedSlopeDeg.toFixed(1)}°` : ""}</span>
            </div>
            <div class="tf-stat-card">
              <span class="tf-stat-label">Steep ≥14° Zones</span>
              <span class="tf-stat-val mono">${chart.steepZoneCount}</span>
            </div>
            <div class="tf-stat-card">
              <span class="tf-stat-label">Crossings</span>
              <span class="tf-stat-val mono">${norm.waterwayCrossingCount}</span>
            </div>
          </div>

          <div class="tf-elev-stage">
            ${norm.hasMultipleCourseSections ? `<p class="tf-inspector-muted">Distance combines the mapped track sections. Check the map for junctions and gaps.</p>` : ""}
            <div class="tf-elev-scrub-hud" data-role="scrub-hud">
              <span class="tf-elev-scrub-hint">${
                hasCourse
                  ? "Move cursor across the Elevation Profile or Track Map — both markers stay synced in real time."
                  : "Move cursor or drag across the profile to inspect exact elevation, slope, grade, and surface."
              }</span>
            </div>
            ${visualStageHtml}
          </div>

          <div class="tf-elev-detail-grid">
            <section class="tf-elev-panel">
              <div class="tf-insp-sec-head">
                <h3>Difficulty Mix &amp; Terrain Factors</h3>
                <span class="mono tf-insp-meta">${segments.length || norm.profile.length} ${segments.length ? "segments" : "samples"}</span>
              </div>
              ${gradeBarHtml}
              <div class="tf-elev-meta-tags">
                ${surfaceSet.map((s) => `<span class="tf-mini-tag">${escapeHtml(s)}</span>`).join("")}
                ${norm.parks.map((p) => `<span class="tf-mini-tag emerald">${escapeHtml(p.name)}</span>`).join("")}
                ${norm.breakdown?.dem_tier ? `<span class="tf-mini-tag">DEM: ${escapeHtml(norm.breakdown.dem_tier)}</span>` : ""}
                ${rainfall72h ? `<span class="tf-mini-tag cyan">Rain: ${escapeHtml(rainfall72h)}</span>` : ""}
              </div>
              ${
                factorsHtml
                  ? `<div class="tf-elev-factors-grid">${factorsHtml}</div>`
                  : ""
              }
              ${
                overrides.length > 0
                  ? `<div class="tf-override-list">${overrides.map((ov) => `<div class="tf-override-badge">▲ ${escapeHtml(formatOverrideLabel(ov))}</div>`).join("")}</div>`
                  : ""
              }
            </section>

            <section class="tf-elev-panel">
              <div class="tf-insp-sec-head">
                <h3>Steep Climbs &amp; Descents (≥14°)</h3>
                <span class="mono tf-insp-meta">${chart.steepZoneCount}</span>
              </div>
              <div class="tf-elev-jump-list">${steepZonesHtml}</div>

              <div class="tf-insp-sec-head tf-elev-subhead">
                <h3>Waterway Crossings</h3>
                <span class="mono tf-insp-meta">${norm.crossings.length}</span>
              </div>
              <div class="tf-elev-jump-list">${crossingsHtml}</div>
            </section>
          </div>
        </div>
      </div>
    `;

    this.backdropEl.classList.remove("hidden");
    this.isOpen = true;
    if (!wasOpen) {
      this.callbacks.onOpenStateChange?.(true);
    }

    this.backdropEl
      .querySelector<HTMLButtonElement>("[data-modal-action='close']")
      ?.addEventListener("click", () => this.close());

    this.backdropEl
      .querySelector<HTMLButtonElement>("[data-modal-action='share']")
      ?.addEventListener("click", () => {
        this.callbacks.onShareTrack?.(detail.track_id);
      });

    this.backdropEl
      .querySelector<HTMLButtonElement>("[data-modal-action='zoom']")
      ?.addEventListener("click", () => {
        if (detail.bbox) {
          this.callbacks.onZoomToTrack?.(detail.bbox);
          this.close();
        }
      });

    this.backdropEl
      .querySelector<HTMLButtonElement>("[data-modal-action='gpx']")
      ?.addEventListener("click", () => {
        this.callbacks.onExportTrack?.(detail.track_id, "gpx");
      });

    if (chart.geometry) {
      this.bindScrubbing(
        norm,
        chart.geometry,
        hasCourse ? course : null,
        snowLineElevationM,
        segById,
        activeBasemap,
        opts.tileManifest ?? null,
        Boolean(opts.isOfflinePack)
      );
    }
  }

  close(): void {
    this.destroyMiniMap();
    const wasOpen = this.isOpen;
    this.isOpen = false;
    this.backdropEl.classList.add("hidden");
    this.backdropEl.innerHTML = "";
    if (wasOpen) {
      this.callbacks.onOpenStateChange?.(false);
    }
  }

  destroy(): void {
    this.destroyMiniMap();
    document.removeEventListener("keydown", this.keydownHandler);
    this.backdropEl.remove();
  }

  private destroyMiniMap(): void {
    if (this.miniMap) {
      try {
        this.miniMap.destroy();
      } catch {
        // Ignore teardown errors
      }
      this.miniMap = null;
    }
  }

  private bindScrubbing(
    norm: NormalizedTrackSummary,
    geom: ElevationChartGeometry,
    course: TrackCourseSvgResult | null,
    snowLineElevationM: number,
    segById: Map<string, SegmentSummaryItem>,
    initialBasemap: BasemapId,
    tileManifest: TileManifestResponse | null,
    isOfflinePack: boolean
  ): void {
    const elevSvg = this.backdropEl.querySelector<SVGSVGElement>(".tf-elev-svg-lg");
    const elevHit = this.backdropEl.querySelector<SVGRectElement>(
      "[data-role='elev-scrub-hit']"
    );
    const crosshair = this.backdropEl.querySelector<SVGGElement>(
      "[data-role='elev-crosshair']"
    );
    const crossLine = this.backdropEl.querySelector<SVGLineElement>(
      "[data-role='crosshair-line']"
    );
    const crossHalo = this.backdropEl.querySelector<SVGCircleElement>(
      "[data-role='crosshair-halo']"
    );
    const crossDot = this.backdropEl.querySelector<SVGCircleElement>(
      "[data-role='crosshair-dot']"
    );
    const hud = this.backdropEl.querySelector<HTMLElement>("[data-role='scrub-hud']");

    const miniMapContainer = this.backdropEl.querySelector<HTMLElement>(
      "[data-role='course-minimap']"
    );
    const svgFallbackWrap = this.backdropEl.querySelector<HTMLElement>(
      "[data-role='course-svg-fallback']"
    );
    const courseSvg = this.backdropEl.querySelector<SVGSVGElement>(".tf-course-svg");
    const courseHit = this.backdropEl.querySelector<SVGRectElement>(
      "[data-role='course-scrub-hit']"
    );
    const courseCursor = this.backdropEl.querySelector<SVGGElement>(
      "[data-role='course-cursor']"
    );
    const courseGlow = this.backdropEl.querySelector<SVGCircleElement>(
      "[data-role='course-cursor-glow']"
    );
    const courseRing = this.backdropEl.querySelector<SVGCircleElement>(
      "[data-role='course-cursor-ring']"
    );
    const courseDot = this.backdropEl.querySelector<SVGCircleElement>(
      "[data-role='course-cursor-dot']"
    );

    if (!elevSvg || !elevHit || !crosshair || !crossLine || !crossHalo || !crossDot || !hud) {
      return;
    }

    const syncAtDistance = (
      distM: number,
      explicitCoursePt?: TrackCoursePoint | null
    ): void => {
      const sample = sampleElevationAtDistance(norm.profile, distM);
      if (!sample) return;

      const x = geom.padL + (sample.distM / geom.totalDistM) * geom.plotW;
      const y =
        geom.padT +
        (1 - (sample.elevM - geom.yMin) / Math.max(1, geom.yMax - geom.yMin)) *
          geom.plotH;
      const color = getGradeColor(sample.grade);

      crossLine.setAttribute("x1", x.toFixed(1));
      crossLine.setAttribute("x2", x.toFixed(1));
      crossHalo.setAttribute("cx", x.toFixed(1));
      crossHalo.setAttribute("cy", y.toFixed(1));
      crossDot.setAttribute("cx", x.toFixed(1));
      crossDot.setAttribute("cy", y.toFixed(1));
      crossDot.setAttribute("fill", color);
      crosshair.classList.remove("hidden");

      const coursePt =
        explicitCoursePt ??
        (course ? sampleTrackCourseAtDistance(course.vertices, sample.distM) : null);

      if (coursePt) {
        if (this.miniMap) {
          this.miniMap.setCursor(coursePt, color);
        }
        if (courseCursor && courseGlow && courseRing && courseDot) {
          const cx = coursePt.x.toFixed(1);
          const cy = coursePt.y.toFixed(1);
          courseGlow.setAttribute("cx", cx);
          courseGlow.setAttribute("cy", cy);
          courseRing.setAttribute("cx", cx);
          courseRing.setAttribute("cy", cy);
          courseDot.setAttribute("cx", cx);
          courseDot.setAttribute("cy", cy);
          courseDot.setAttribute("fill", color);
          courseCursor.classList.remove("hidden");
        }
      }

      const seg = sample.segmentId ? segById.get(sample.segmentId) : undefined;
      const surfaceText = seg?.surface_class
        ? seg.surface_class.replace(/_/g, " ")
        : null;
      const aboveSnow = sample.elevM >= snowLineElevationM;
      const nearCrossing = norm.crossings.find(
        (c) =>
          typeof c.dist_m === "number" &&
          Math.abs(c.dist_m - sample.distM) <= Math.max(120, geom.totalDistM * 0.015)
      );

      hud.innerHTML = `
        <span class="tf-elev-hud-item"><span class="tf-elev-hud-k">DIST</span><strong class="mono">${(sample.distM / 1000).toFixed(2)} km</strong></span>
        <span class="tf-elev-hud-item"><span class="tf-elev-hud-k">ELEV</span><strong class="mono">${Math.round(sample.elevM)} m</strong></span>
        <span class="tf-elev-hud-item"><span class="tf-elev-hud-k">SLOPE</span><strong class="mono">${
          sample.slopeDeg !== null
            ? `${sample.slopeDeg.toFixed(1)}° (${Math.round(sample.slopePct ?? 0)}%)`
            : "—"
        }</strong></span>
        ${
          coursePt
            ? `<span class="tf-elev-hud-item"><span class="tf-elev-hud-k">COORDS</span><strong class="mono">${coursePt.lat.toFixed(4)}, ${coursePt.lon.toFixed(4)}</strong></span>`
            : ""
        }
        <span class="tf-elev-grade-chip sm" style="border-color:${color};color:${color}">${escapeHtml(sample.grade)}</span>
        ${surfaceText ? `<span class="tf-mini-tag">${escapeHtml(surfaceText)}</span>` : ""}
        ${aboveSnow ? `<span class="tf-pill tf-pill-ice">Above ${snowLineElevationM}m snow-line</span>` : ""}
        ${nearCrossing ? `<span class="tf-mini-tag cyan">≈ ${escapeHtml(nearCrossing.name || "Waterway crossing")}</span>` : ""}
      `;
    };

    // Mount real interactive MapLibre mini-map with underlying topographic/satellite map
    if (course && course.bbox && miniMapContainer) {
      try {
        this.miniMap = new CourseMiniMap(miniMapContainer, {
          vertices: course.vertices,
          bbox: course.bbox,
          totalDistM: course.totalDistM,
          crossings: norm.crossings,
          initialBasemap,
          tileManifest,
          isOfflinePack,
          onHoverPoint: (pt) => syncAtDistance(pt.distM, pt),
        });

        // Wire mini-map basemap switcher (Topo / Sat / OSM) & Fit button
        const bmButtons = this.backdropEl.querySelectorAll<HTMLButtonElement>(
          "[data-mini-basemap]"
        );
        bmButtons.forEach((btn) => {
          btn.addEventListener("click", () => {
            const bm = btn.dataset.miniBasemap as BasemapId;
            this.miniMap?.setBasemap(bm);
            bmButtons.forEach((b) => b.classList.toggle("active", b === btn));
          });
        });

        this.backdropEl
          .querySelector<HTMLButtonElement>("[data-mini-action='fit']")
          ?.addEventListener("click", () => {
            this.miniMap?.fitTrack();
          });
      } catch {
        // Fallback gracefully to SVG plan view if WebGL is unavailable
        miniMapContainer.classList.add("hidden");
        svgFallbackWrap?.classList.remove("hidden");
      }
    }

    const handleElevPointer = (clientX: number): void => {
      const rect = elevSvg.getBoundingClientRect();
      if (rect.width <= 0) return;
      const svgX = ((clientX - rect.left) / rect.width) * geom.width;
      const clampedX = Math.max(geom.padL, Math.min(geom.padL + geom.plotW, svgX));
      const frac = (clampedX - geom.padL) / Math.max(1, geom.plotW);
      syncAtDistance(frac * geom.totalDistM);
    };

    elevHit.addEventListener("pointermove", (e) => handleElevPointer(e.clientX));
    elevHit.addEventListener("pointerdown", (e) => handleElevPointer(e.clientX));

    if (course && courseSvg && courseHit) {
      const handleCoursePointer = (clientX: number, clientY: number): void => {
        const rect = courseSvg.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return;
        const svgX = ((clientX - rect.left) / rect.width) * course.width;
        const svgY = ((clientY - rect.top) / rect.height) * course.height;
        const nearest = findNearestPointOnTrackCourse(course.vertices, svgX, svgY);
        if (!nearest) return;
        syncAtDistance(nearest.distM, nearest);
      };

      courseHit.addEventListener("pointermove", (e) =>
        handleCoursePointer(e.clientX, e.clientY)
      );
      courseHit.addEventListener("pointerdown", (e) =>
        handleCoursePointer(e.clientX, e.clientY)
      );
    }

    // Clicking or hovering any steep zone / crossing row highlights that exact point on BOTH graphs
    this.backdropEl
      .querySelectorAll<HTMLButtonElement>("[data-focus-dist]")
      .forEach((btn) => {
        const dist = Number(btn.dataset.focusDist);
        if (!Number.isFinite(dist)) return;
        btn.addEventListener("mouseenter", () => syncAtDistance(dist));
        btn.addEventListener("click", () => syncAtDistance(dist));
      });
  }
}
