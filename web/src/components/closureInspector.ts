import {
  buildClosureMetadataRows,
  formatClosureSeverityLabel,
  formatClosureTypeLabel,
  getClosureTone,
  sanitizeExternalHttpUrl,
} from "../closuresHelpers";
import { escapeHtml, getGradeColor } from "../presets";
import type { BBoxTuple, ClosureDetailResponse } from "../types";

export interface ClosureInspectorCallbacks {
  onClose: () => void;
  onZoomToClosure: (bbox: BBoxTuple) => void;
  onOpenTrack: (trackId: string, trackName?: string) => void;
  onSelectOverlapping: (closure: ClosureDetailResponse) => void;
}

export class ClosureInspectorDrawer {
  private readonly root: HTMLElement;
  private readonly callbacks: ClosureInspectorCallbacks;
  private isOpen = false;
  private detail: ClosureDetailResponse | null = null;
  private overlapping: ClosureDetailResponse[] = [];
  private loading = false;
  private error: string | null = null;

  constructor(container: HTMLElement, callbacks: ClosureInspectorCallbacks) {
    this.root = container;
    this.callbacks = callbacks;
    this.root.className = "tf-inspector tf-closure-inspector";
    this.root.setAttribute("aria-label", "Incident & Closure Details");
    this.root.setAttribute("aria-hidden", "true");
    this.renderEmpty();
  }

  openState(): boolean {
    return this.isOpen;
  }

  getCurrentClosureId(): number | string | null {
    return this.detail?.closure_id ?? null;
  }

  showClosure(
    detail: ClosureDetailResponse,
    options?: {
      overlapping?: ClosureDetailResponse[];
      loading?: boolean;
      error?: string | null;
    }
  ): void {
    this.isOpen = true;
    this.detail = detail;
    if (options?.overlapping !== undefined) {
      this.overlapping = options.overlapping;
    }
    this.loading = Boolean(options?.loading);
    this.error = options?.error ?? null;
    this.root.setAttribute("aria-hidden", "false");
    this.render();
  }

  updateDetail(
    detail: ClosureDetailResponse,
    options?: { loading?: boolean; error?: string | null }
  ): void {
    if (!this.isOpen) return;
    this.detail = detail;
    this.loading = Boolean(options?.loading);
    this.error = options?.error ?? null;
    this.render();
  }

  setLoadingError(error: string | null): void {
    if (!this.isOpen) return;
    this.loading = false;
    this.error = error;
    this.render();
  }

  close(): void {
    this.isOpen = false;
    this.detail = null;
    this.overlapping = [];
    this.loading = false;
    this.error = null;
    this.root.setAttribute("aria-hidden", "true");
  }

  private renderEmpty(): void {
    this.root.innerHTML = `
      <div class="tf-inspector-head">
        <div class="tf-inspector-head-copy">
          <div class="tf-inspector-kicker">Incident &amp; Closure Details</div>
          <h2 class="tf-inspector-title">Select an Alert Area</h2>
        </div>
        <button type="button" class="tf-inspector-close" data-role="close" aria-label="Close Incident Details">×</button>
      </div>
      <div class="tf-inspector-body">
        <p class="tf-inspector-muted">Click any emergency incident area, seasonal closure, or agency alert on the map to inspect its official details, affected 4WD tracks, and agency source link.</p>
      </div>
    `;
    this.bindEvents();
  }

  private render(): void {
    const detail = this.detail;
    if (!detail) {
      this.renderEmpty();
      return;
    }

    const tone = getClosureTone(detail.closure_type, detail.severity);
    const typeLabel = formatClosureTypeLabel(detail.closure_type);
    const sevLabel = formatClosureSeverityLabel(
      detail.severity,
      detail.raw_severity
    );
    const safeUrl = sanitizeExternalHttpUrl(detail.source_url);
    const metaRows = buildClosureMetadataRows(detail);

    const tonePillClass =
      tone === "critical"
        ? "tf-pill-red"
        : tone === "warning"
          ? "tf-pill-amber"
          : "tf-pill-ice";

    const badgesHtml = [
      `<span class="tf-pill ${tonePillClass}">⚠ ${escapeHtml(sevLabel)}</span>`,
      detail.state
        ? `<span class="tf-pill mono">${escapeHtml(detail.state)}</span>`
        : "",
      detail.status
        ? `<span class="tf-pill">${escapeHtml(detail.status.replace(/_/g, " ").toUpperCase())}</span>`
        : "",
      detail.is_stale
        ? `<span class="tf-stale-badge" title="Upstream agency feed had errors; displaying last-known cached alert">STALE CACHE</span>`
        : "",
    ]
      .filter(Boolean)
      .join("");

    const overlappingHtml =
      this.overlapping.length > 1
        ? `
          <section class="tf-insp-section">
            <div class="tf-insp-sec-head">
              <h3>Overlapping Alerts at Location</h3>
              <span class="mono tf-insp-meta">${this.overlapping.length}</span>
            </div>
            <div class="tf-closure-overlap-list">
              ${this.overlapping
                .map((item, idx) => {
                  const active =
                    String(item.closure_id) === String(detail.closure_id);
                  const itemTone = getClosureTone(
                    item.closure_type,
                    item.severity
                  );
                  const label = item.subtitle
                    ? `${item.title} — ${item.subtitle}`
                    : item.title;
                  return `
                    <button
                      type="button"
                      class="tf-closure-overlap-btn tone-${itemTone} ${active ? "active" : ""}"
                      data-overlap-idx="${idx}"
                    >
                      <span class="tf-closure-overlap-title">${escapeHtml(label)}</span>
                      <span class="tf-closure-overlap-sub">${escapeHtml(formatClosureTypeLabel(item.closure_type))}${item.agency ? ` · ${escapeHtml(item.agency)}` : ""}</span>
                    </button>
                  `;
                })
                .join("")}
            </div>
          </section>
        `
        : "";

    const actionsHtml =
      detail.bbox || safeUrl
        ? `
          <div class="tf-btn-row">
            ${
              detail.bbox
                ? `<button type="button" class="tf-btn-secondary" data-role="zoom-closure">⌖ Zoom to Area</button>`
                : ""
            }
            ${
              safeUrl
                ? `<a class="tf-btn-secondary tf-btn-link" href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">Official Agency Source ↗</a>`
                : ""
            }
          </div>
        `
        : "";

    const descriptionParagraphs = (detail.description ?? "")
      .split(/\n+/)
      .map((line) => line.trim())
      .filter(Boolean);

    const descriptionHtml =
      descriptionParagraphs.length > 0
        ? `
          <section class="tf-insp-section">
            <div class="tf-insp-sec-head">
              <h3>Official Advisory &amp; Details</h3>
              ${this.loading ? `<span class="mono tf-insp-meta">Refreshing…</span>` : ""}
            </div>
            <div class="tf-closure-card tone-${tone} tf-closure-detail-body">
              ${descriptionParagraphs
                .map((p) => `<p class="tf-closure-detail-para">${escapeHtml(p)}</p>`)
                .join("")}
            </div>
          </section>
        `
        : this.loading
          ? `<div class="tf-inspector-loading">Fetching full agency incident details &amp; affected tracks…</div>`
          : `
            <section class="tf-insp-section">
              <div class="tf-insp-sec-head">
                <h3>Official Advisory &amp; Details</h3>
              </div>
              <p class="tf-inspector-muted">No extended description text was published by the agency feed for this alert.${safeUrl ? " Open the official agency source above for the latest emergency updates." : ""}</p>
            </section>
          `;

    const metadataHtml =
      metaRows.length > 0
        ? `
          <section class="tf-insp-section">
            <div class="tf-insp-sec-head">
              <h3>Incident Metadata</h3>
              ${detail.geometry_type ? `<span class="mono tf-insp-meta">${escapeHtml(detail.geometry_type)}</span>` : ""}
            </div>
            <div class="tf-kv-grid">
              ${metaRows
                .map(
                  (row) => `
                    <span class="tf-kv-key">${escapeHtml(row.label)}</span>
                    <span class="tf-kv-val ${row.mono ? "mono" : ""}">${escapeHtml(row.value)}</span>
                  `
                )
                .join("")}
            </div>
          </section>
        `
        : "";

    const affectedTracks = detail.affected_tracks ?? [];
    const affectedSegCount = detail.affected_segment_count ?? 0;
    const fallbackTrackId =
      affectedTracks.length === 0 && detail.track_id ? detail.track_id : null;

    const affectedTracksHtml =
      affectedTracks.length > 0
        ? `
          <section class="tf-insp-section">
            <div class="tf-insp-sec-head">
              <h3>Affected 4WD Tracks</h3>
              <span class="mono tf-insp-meta">${affectedTracks.length}${affectedSegCount > 0 ? ` · ${affectedSegCount} segs` : ""}</span>
            </div>
            <div class="tf-card-stack">
              ${affectedTracks
                .map((trk) => {
                  const gradeColor = trk.grade ? getGradeColor(trk.grade) : null;
                  const distKm =
                    trk.total_length_m > 0
                      ? `${(trk.total_length_m / 1000).toFixed(1)} km`
                      : "";
                  return `
                    <button
                      type="button"
                      class="tf-closure-track-row"
                      data-open-track="${escapeHtml(trk.track_id)}"
                      data-track-name="${escapeHtml(trk.name)}"
                    >
                      <span class="tf-closure-track-main">
                        <span class="tf-closure-track-name">${escapeHtml(trk.name)}</span>
                        <span class="tf-closure-track-sub mono">${escapeHtml([trk.state, distKm].filter(Boolean).join(" · "))}</span>
                      </span>
                      ${
                        trk.grade && gradeColor
                          ? `<span class="tf-pill" style="border-color:${gradeColor};color:${gradeColor}">${escapeHtml(trk.grade)}</span>`
                          : `<span class="tf-closure-track-arrow">→</span>`
                      }
                    </button>
                  `;
                })
                .join("")}
            </div>
          </section>
        `
        : fallbackTrackId
          ? `
            <section class="tf-insp-section">
              <div class="tf-insp-sec-head">
                <h3>Linked 4WD Track</h3>
              </div>
              <button
                type="button"
                class="tf-btn-secondary"
                data-open-track="${escapeHtml(fallbackTrackId)}"
              >Inspect Linked Track →</button>
            </section>
          `
          : affectedSegCount > 0
            ? `
              <section class="tf-insp-section">
                <div class="tf-insp-sec-head">
                  <h3>Affected Network</h3>
                  <span class="mono tf-insp-meta">${affectedSegCount} segments</span>
                </div>
                <p class="tf-inspector-muted">This alert intersects ${affectedSegCount} canonical track segment${affectedSegCount === 1 ? "" : "s"}.</p>
              </section>
            `
            : "";

    const subtitleHtml =
      detail.subtitle &&
      detail.subtitle.toLowerCase() !== detail.title.toLowerCase()
        ? `<div class="tf-inspector-alt">${escapeHtml(detail.subtitle)}</div>`
        : "";

    const errorBannerHtml = this.error
      ? `<div class="tf-feed-warn-banner">Showing cached map alert data (${escapeHtml(this.error)})</div>`
      : "";

    this.root.innerHTML = `
      <div class="tf-inspector-head">
        <div class="tf-inspector-head-copy">
          <div class="tf-inspector-kicker tf-closure-kicker-${tone}">⚠ ${escapeHtml(typeLabel)}</div>
          <h2 class="tf-inspector-title">${escapeHtml(detail.title)}</h2>
          ${subtitleHtml}
          <div class="tf-inspector-badges">${badgesHtml}</div>
        </div>
        <button type="button" class="tf-inspector-close" data-role="close" aria-label="Close Incident Details">×</button>
      </div>
      <div class="tf-inspector-body">
        ${errorBannerHtml}
        ${actionsHtml}
        ${overlappingHtml}
        ${descriptionHtml}
        ${metadataHtml}
        ${affectedTracksHtml}
      </div>
    `;

    this.bindEvents();
  }

  private bindEvents(): void {
    this.root
      .querySelector<HTMLButtonElement>("[data-role='close']")
      ?.addEventListener("click", () => {
        this.close();
        this.callbacks.onClose();
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='zoom-closure']")
      ?.addEventListener("click", () => {
        if (this.detail?.bbox) {
          this.callbacks.onZoomToClosure(this.detail.bbox);
        }
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-overlap-idx]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const idx = Number(btn.dataset.overlapIdx);
          const chosen = this.overlapping[idx];
          if (chosen) {
            this.callbacks.onSelectOverlapping(chosen);
          }
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-open-track]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const trackId = btn.dataset.openTrack;
          const trackName = btn.dataset.trackName;
          if (trackId) {
            this.callbacks.onOpenTrack(trackId, trackName);
          }
        });
      });
  }
}
