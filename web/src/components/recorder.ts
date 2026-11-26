import {
  buildClientGeoJson,
  buildClientGpx,
  slugifyExportFilename,
  triggerClientFileDownload,
} from "../offline/exporters";
import {
  SIMULATED_HIGH_COUNTRY_WAYPOINTS,
  buildBacktrackGuidance,
  computeRecorderStats,
  computeRoughnessRms,
} from "../offline/recorderEngine";
import { escapeHtml } from "../presets";
import { validatePrivacyTrimMeters } from "../tripsHelpers";
import type {
  RecordedBreadcrumbPoint,
  TripVisibility,
  VehicleProfileItem,
} from "../types";

export interface RecorderCallbacks {
  onPointsChanged: (
    points: readonly RecordedBreadcrumbPoint[],
    backtrackActive: boolean
  ) => void;
  onCenterOnPoint: (lat: number, lon: number) => void;
  onSaveRecordedTrip: (params: {
    name: string;
    points: RecordedBreadcrumbPoint[];
    visibility: TripVisibility;
    privacy_trim_m: number;
    vehicle_profile_id: string | null;
  }) => Promise<void>;
  onToast: (message: string, kind: "info" | "success" | "error") => void;
}

interface WakeLockSentinelLike {
  released?: boolean;
  release: () => Promise<void>;
  addEventListener?: (event: string, listener: () => void) => void;
}

type RecorderPhase = "idle" | "recording" | "paused" | "stopped";

function formatClock(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds));
  const hrs = Math.floor(s / 3600);
  const mins = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  const mm = String(mins).padStart(2, "0");
  const ss = String(secs).padStart(2, "0");
  return hrs > 0 ? `${hrs}:${mm}:${ss}` : `${mm}:${ss}`;
}

export class InCabRecorderHud {
  private readonly root: HTMLElement;
  private readonly callbacks: RecorderCallbacks;

  private expanded = false;
  private phase: RecorderPhase = "idle";
  private points: RecordedBreadcrumbPoint[] = [];
  private backtrackActive = false;
  private wakeLockEnabled = false;
  private wakeLockStatus: "off" | "active" | "unsupported" = "off";
  private wakeLockSentinel: WakeLockSentinelLike | null = null;
  private motionEnabled = false;
  private motionSamples: number[] = [];
  private motionListener: ((ev: DeviceMotionEvent) => void) | null = null;

  private geoWatchId: number | null = null;
  private simTimerId: number | null = null;
  private simIndex = 0;
  private isSimulating = false;
  private savingTrip = false;
  private errorMsg: string | null = null;

  private vehicles: VehicleProfileItem[] = [];
  private isOnline = true;
  private isAuthenticated = false;

  constructor(container: HTMLElement, callbacks: RecorderCallbacks) {
    this.root = container;
    this.callbacks = callbacks;
    this.root.className = "tf-recorder-hud";
    this.root.setAttribute("aria-label", "In-Cab Breadcrumb Trip Recorder");
    this.render();
  }

  updateContext(ctx: {
    vehicles: VehicleProfileItem[];
    isOnline: boolean;
    isAuthenticated: boolean;
  }): void {
    this.vehicles = ctx.vehicles;
    this.isOnline = ctx.isOnline;
    this.isAuthenticated = ctx.isAuthenticated;
    if (this.phase === "stopped" && this.expanded) {
      this.render();
    }
  }

  private startGpsRecording(): void {
    if (typeof navigator === "undefined" || !navigator.geolocation) {
      this.errorMsg =
        "Browser Geolocation unavailable — use 'Simulate High Country Drive' to test.";
      this.expanded = true;
      this.render();
      return;
    }

    this.stopTimersAndWatchers();
    this.errorMsg = null;
    this.isSimulating = false;
    this.phase = "recording";
    this.expanded = true;

    this.geoWatchId = navigator.geolocation.watchPosition(
      (pos) => {
        if (this.phase !== "recording") return;
        const rough = this.consumeRoughnessSample();
        const pt: RecordedBreadcrumbPoint = {
          lon: pos.coords.longitude,
          lat: pos.coords.latitude,
          elev_m:
            typeof pos.coords.altitude === "number"
              ? Math.round(pos.coords.altitude * 10) / 10
              : null,
          timestamp: new Date(pos.timestamp || Date.now()).toISOString(),
          accuracy_m: pos.coords.accuracy ?? null,
          speed_mps: pos.coords.speed ?? null,
          roughness_rms: rough,
        };
        this.appendPoint(pt);
      },
      (err) => {
        this.errorMsg = `GPS warning: ${err.message} (you can use Simulate Drive on desktop)`;
        this.render();
      },
      {
        enableHighAccuracy: true,
        maximumAge: 2000,
        timeout: 12000,
      }
    );

    this.render();
    this.callbacks.onToast("In-cab GPS breadcrumb recording started", "info");
  }

  private startSimulatedDrive(): void {
    this.stopTimersAndWatchers();
    this.errorMsg = null;
    this.isSimulating = true;
    this.phase = "recording";
    this.expanded = true;
    if (this.points.length === 0 || this.simIndex >= SIMULATED_HIGH_COUNTRY_WAYPOINTS.length) {
      this.points = [];
      this.simIndex = 0;
    }

    const pushNext = (): void => {
      if (this.phase !== "recording") return;
      const wp = SIMULATED_HIGH_COUNTRY_WAYPOINTS[this.simIndex];
      if (!wp) {
        this.stopRecording();
        this.callbacks.onToast(
          "Simulated High Country drive finished — ready to save or export GPX",
          "success"
        );
        return;
      }
      // Space timestamps cleanly by 25 s per simulated step for realistic 4WD speed metrics
      const baseMs = Date.now() - (SIMULATED_HIGH_COUNTRY_WAYPOINTS.length - this.simIndex) * 25_000;
      this.appendPoint({
        lon: wp.lon,
        lat: wp.lat,
        elev_m: wp.elev_m,
        timestamp: new Date(baseMs).toISOString(),
        accuracy_m: 4.0,
        speed_mps: wp.speed_mps,
        roughness_rms: this.consumeRoughnessSample() ?? wp.roughness_rms,
      });
      this.simIndex += 1;
    };

    pushNext();
    this.simTimerId = window.setInterval(pushNext, 420);
    this.render();
    this.callbacks.onToast(
      "Simulating Davies Plain High Country drive (desktop GPS test)",
      "info"
    );
  }

  private pauseRecording(): void {
    if (this.phase !== "recording") return;
    this.phase = "paused";
    this.stopTimersAndWatchers();
    this.render();
  }

  private resumeRecording(): void {
    if (this.phase !== "paused") return;
    if (this.isSimulating) {
      this.startSimulatedDrive();
    } else {
      this.startGpsRecording();
    }
  }

  private stopRecording(): void {
    this.stopTimersAndWatchers();
    if (this.points.length > 0) {
      this.phase = "stopped";
    } else {
      this.phase = "idle";
    }
    this.expanded = true;
    this.render();
  }

  private discardRecording(): void {
    this.stopTimersAndWatchers();
    this.points = [];
    this.backtrackActive = false;
    this.phase = "idle";
    this.simIndex = 0;
    this.isSimulating = false;
    this.errorMsg = null;
    this.callbacks.onPointsChanged([], false);
    this.render();
  }

  private appendPoint(pt: RecordedBreadcrumbPoint): void {
    this.points = [...this.points, pt];
    this.callbacks.onPointsChanged(this.points, this.backtrackActive);
    if (this.points.length === 1 || this.points.length % 5 === 0) {
      this.callbacks.onCenterOnPoint(pt.lat, pt.lon);
    }
    this.render();
  }

  private stopTimersAndWatchers(): void {
    if (this.geoWatchId !== null && typeof navigator !== "undefined" && navigator.geolocation) {
      navigator.geolocation.clearWatch(this.geoWatchId);
      this.geoWatchId = null;
    }
    if (this.simTimerId !== null) {
      window.clearInterval(this.simTimerId);
      this.simTimerId = null;
    }
  }

  private async toggleWakeLock(): Promise<void> {
    if (this.wakeLockEnabled) {
      this.wakeLockEnabled = false;
      this.wakeLockStatus = "off";
      try {
        await this.wakeLockSentinel?.release();
      } catch {
        // Ignore release errors
      }
      this.wakeLockSentinel = null;
      this.render();
      return;
    }

    const nav = navigator as Navigator & {
      wakeLock?: { request: (type: "screen") => Promise<WakeLockSentinelLike> };
    };
    if (!nav.wakeLock || typeof nav.wakeLock.request !== "function") {
      this.wakeLockEnabled = false;
      this.wakeLockStatus = "unsupported";
      this.callbacks.onToast(
        "Screen Wake Lock API not supported in this browser — keep screen awake manually",
        "info"
      );
      this.render();
      return;
    }

    try {
      const sentinel = await nav.wakeLock.request("screen");
      this.wakeLockSentinel = sentinel;
      this.wakeLockEnabled = true;
      this.wakeLockStatus = "active";
      sentinel.addEventListener?.("release", () => {
        this.wakeLockEnabled = false;
        this.wakeLockStatus = "off";
        this.render();
      });
      this.callbacks.onToast("Screen Wake Lock active for in-cab display", "success");
    } catch {
      this.wakeLockEnabled = false;
      this.wakeLockStatus = "unsupported";
      this.callbacks.onToast(
        "Screen Wake Lock denied or unavailable in current context",
        "info"
      );
    }
    this.render();
  }

  private toggleMotionSensor(): void {
    if (this.motionEnabled) {
      this.motionEnabled = false;
      if (this.motionListener) {
        window.removeEventListener("devicemotion", this.motionListener);
        this.motionListener = null;
      }
      this.motionSamples = [];
      this.render();
      return;
    }

    if (typeof window === "undefined" || !("DeviceMotionEvent" in window)) {
      this.callbacks.onToast(
        "Web DeviceMotion roughness sensor unavailable on this device",
        "info"
      );
      return;
    }

    this.motionEnabled = true;
    this.motionSamples = [];
    this.motionListener = (ev: DeviceMotionEvent) => {
      const acc = ev.acceleration ?? ev.accelerationIncludingGravity;
      if (!acc) return;
      const x = acc.x ?? 0;
      const y = acc.y ?? 0;
      const z = acc.z ?? 0;
      const mag = Math.hypot(x, y, z);
      const adjusted = ev.acceleration ? mag : Math.abs(mag - 9.80665);
      if (Number.isFinite(adjusted)) {
        this.motionSamples.push(adjusted);
        if (this.motionSamples.length > 60) {
          this.motionSamples.shift();
        }
      }
    };
    window.addEventListener("devicemotion", this.motionListener);
    this.callbacks.onToast("DeviceMotion roughness capture enabled", "info");
    this.render();
  }

  private consumeRoughnessSample(): number | null {
    if (!this.motionEnabled || this.motionSamples.length === 0) return null;
    const rms = computeRoughnessRms(this.motionSamples);
    this.motionSamples = [];
    return rms;
  }

  private exportRecorded(format: "gpx" | "geojson"): void {
    if (this.points.length < 2) {
      this.callbacks.onToast("Record at least 2 points before exporting", "info");
      return;
    }
    const nameInput = this.root.querySelector<HTMLInputElement>(
      "input[name='rec_trip_name']"
    );
    const title =
      nameInput?.value.trim() ||
      `High Country Drive ${new Date().toISOString().slice(0, 10)}`;

    if (format === "gpx") {
      const xml = buildClientGpx(title, this.points, {
        description: "Recorded with TrailForge In-Cab PWA Breadcrumb Recorder",
      });
      triggerClientFileDownload(
        xml,
        slugifyExportFilename(title, "gpx"),
        "application/gpx+xml"
      );
      this.callbacks.onToast("Exported offline GPX 1.1 (GridForge-compatible)", "success");
    } else {
      const fc = buildClientGeoJson(title, this.points, {
        description: "Recorded with TrailForge In-Cab PWA Breadcrumb Recorder",
      });
      triggerClientFileDownload(
        JSON.stringify(fc, null, 2),
        slugifyExportFilename(title, "geojson"),
        "application/geo+json"
      );
      this.callbacks.onToast("Exported offline GeoJSON FeatureCollection", "success");
    }
  }

  private render(): void {
    const stats = computeRecorderStats(this.points);
    const backtrack = this.backtrackActive
      ? buildBacktrackGuidance(this.points)
      : null;

    const pillLabel =
      this.phase === "recording"
        ? `● REC ${(stats.distanceM / 1000).toFixed(2)} km · ${formatClock(stats.durationS)}`
        : this.phase === "paused"
          ? `❚❚ PAUSED ${(stats.distanceM / 1000).toFixed(2)} km`
          : this.phase === "stopped"
            ? `■ SAVING (${stats.pointCount} pts)`
            : "● In-Cab Recorder";

    const backtrackHud =
      backtrack && backtrack.active
        ? `
          <div class="tf-backtrack-banner">
            <div class="tf-backtrack-head">
              <strong>↩ BACKTRACK ACTIVE</strong>
              <span class="mono">${(backtrack.returnDistanceM / 1000).toFixed(2)} km return</span>
            </div>
            <div class="tf-backtrack-meta mono">
              <span>Next leg: ${backtrack.nextWaypointBearingDeg ?? "—"}° ${escapeHtml(backtrack.nextWaypointCardinal)}</span>
              <span>· Start: ${backtrack.startBearingDeg ?? "—"}° ${escapeHtml(backtrack.startCardinal)} (${(backtrack.directToStartM / 1000).toFixed(2)} km)</span>
            </div>
          </div>
        `
        : "";

    const defaultTripName = `High Country Drive ${new Date().toISOString().slice(0, 10)}`;
    const saveFormHtml =
      this.phase === "stopped" && this.points.length >= 2
        ? `
          <form class="tf-form tf-recorder-save-form" data-role="rec-save-form">
            <label class="tf-field">
              <span class="tf-label">Trip Name</span>
              <input type="text" class="tf-input" name="rec_trip_name" value="${escapeHtml(defaultTripName)}" maxlength="120" required />
            </label>
            <div class="tf-form-grid-2">
              <label class="tf-field">
                <span class="tf-label">Privacy Trim (≥250 m)</span>
                <input type="number" class="tf-input mono" name="rec_trim_m" min="250" max="5000" step="50" value="250" />
              </label>
              <label class="tf-field">
                <span class="tf-label">Visibility</span>
                <select class="tf-select" name="rec_visibility">
                  <option value="community_anonymous" selected>Anonymous Community</option>
                  <option value="private">Private Only</option>
                </select>
              </label>
            </div>
            ${
              this.vehicles.length > 0
                ? `<label class="tf-field">
                    <span class="tf-label">Vehicle Profile</span>
                    <select class="tf-select" name="rec_vehicle_id">
                      ${this.vehicles
                        .map(
                          (v) =>
                            `<option value="${escapeHtml(v.id)}">${escapeHtml(v.name)} (${escapeHtml(v.capability_class)})</option>`
                        )
                        .join("")}
                    </select>
                  </label>`
                : ""
            }
            <div class="tf-btn-row">
              <button type="submit" class="tf-btn-primary" ${this.savingTrip ? "disabled" : ""}>
                ${
                  this.savingTrip
                    ? "Saving…"
                    : this.isOnline && this.isAuthenticated
                      ? "↑ Upload & HMM Match"
                      : "⧖ Queue Offline for Sync"
                }
              </button>
              <button type="button" class="tf-mini-btn" data-rec-export="gpx">⬇ GPX</button>
              <button type="button" class="tf-mini-btn" data-rec-export="geojson">⬇ GeoJSON</button>
            </div>
          </form>
        `
        : "";

    const bodyHtml = this.expanded
      ? `
        <div class="tf-recorder-card">
          <div class="tf-recorder-card-head">
            <span class="tf-recorder-title">In-Cab Breadcrumb Recorder</span>
            <button type="button" class="tf-mini-btn" data-role="rec-collapse" aria-label="Collapse recorder">▾</button>
          </div>

          ${this.errorMsg ? `<div class="tf-inspector-error">${escapeHtml(this.errorMsg)}</div>` : ""}
          ${backtrackHud}

          <div class="tf-recorder-stats mono">
            <div><span class="tf-stat-label">Dist</span><strong>${(stats.distanceM / 1000).toFixed(2)} km</strong></div>
            <div><span class="tf-stat-label">Time</span><strong>${formatClock(stats.durationS)}</strong></div>
            <div><span class="tf-stat-label">Speed</span><strong>${stats.currentSpeedKmh.toFixed(1)} km/h</strong></div>
            <div><span class="tf-stat-label">Elev</span><strong>${stats.currentElevM !== null ? `${Math.round(stats.currentElevM)} m` : "—"}</strong></div>
            <div><span class="tf-stat-label">Pts</span><strong>${stats.pointCount}</strong></div>
            <div><span class="tf-stat-label">Rough</span><strong>${stats.latestRoughnessRms !== null ? `${stats.latestRoughnessRms.toFixed(2)}g` : "—"}</strong></div>
          </div>

          <div class="tf-recorder-controls">
            ${
              this.phase === "idle" || this.phase === "stopped"
                ? `<button type="button" class="tf-btn-primary" data-role="rec-start">● Start GPS</button>
                   <button type="button" class="tf-btn-secondary" data-role="rec-sim" title="Step along Davies Plain corridor without GPS hardware">▶ Simulate Drive</button>`
                : ""
            }
            ${
              this.phase === "recording"
                ? `<button type="button" class="tf-btn-secondary" data-role="rec-pause">❚❚ Pause</button>
                   <button type="button" class="tf-btn-primary" data-role="rec-stop">■ Stop</button>`
                : ""
            }
            ${
              this.phase === "paused"
                ? `<button type="button" class="tf-btn-primary" data-role="rec-resume">▶ Resume</button>
                   <button type="button" class="tf-btn-secondary" data-role="rec-stop">■ Stop</button>`
                : ""
            }
            ${
              this.points.length >= 2
                ? `<button type="button" class="tf-mini-btn ${this.backtrackActive ? "active" : ""}" data-role="rec-backtrack">↩ Backtrack</button>`
                : ""
            }
          </div>

          <div class="tf-recorder-toggles">
            <button type="button" class="tf-mini-btn ${this.wakeLockStatus === "active" ? "active" : ""}" data-role="rec-wakelock">
              ☀ Wake Lock: ${this.wakeLockStatus === "active" ? "ON" : this.wakeLockStatus === "unsupported" ? "N/A" : "OFF"}
            </button>
            <button type="button" class="tf-mini-btn ${this.motionEnabled ? "active" : ""}" data-role="rec-motion">
              〰 Roughness: ${this.motionEnabled ? "ON" : "OFF"}
            </button>
            ${
              this.points.length >= 2 && this.phase !== "stopped"
                ? `<button type="button" class="tf-mini-btn" data-rec-export="gpx">⬇ GPX</button>`
                : ""
            }
            ${
              this.points.length > 0
                ? `<button type="button" class="tf-mini-btn danger" data-role="rec-discard">Clear</button>`
                : ""
            }
          </div>

          ${saveFormHtml}
        </div>
      `
      : "";

    this.root.innerHTML = `
      ${bodyHtml}
      <button
        type="button"
        class="tf-recorder-pill ${this.phase === "recording" ? "recording" : ""} ${this.backtrackActive ? "backtrack" : ""}"
        data-role="rec-toggle"
      >${escapeHtml(pillLabel)}</button>
    `;

    this.bindHandlers();
  }

  private bindHandlers(): void {
    this.root
      .querySelector<HTMLButtonElement>("[data-role='rec-toggle']")
      ?.addEventListener("click", () => {
        this.expanded = !this.expanded;
        this.render();
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='rec-collapse']")
      ?.addEventListener("click", () => {
        this.expanded = false;
        this.render();
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='rec-start']")
      ?.addEventListener("click", () => this.startGpsRecording());

    this.root
      .querySelector<HTMLButtonElement>("[data-role='rec-sim']")
      ?.addEventListener("click", () => this.startSimulatedDrive());

    this.root
      .querySelector<HTMLButtonElement>("[data-role='rec-pause']")
      ?.addEventListener("click", () => this.pauseRecording());

    this.root
      .querySelector<HTMLButtonElement>("[data-role='rec-resume']")
      ?.addEventListener("click", () => this.resumeRecording());

    this.root
      .querySelector<HTMLButtonElement>("[data-role='rec-stop']")
      ?.addEventListener("click", () => this.stopRecording());

    this.root
      .querySelector<HTMLButtonElement>("[data-role='rec-discard']")
      ?.addEventListener("click", () => this.discardRecording());

    this.root
      .querySelector<HTMLButtonElement>("[data-role='rec-backtrack']")
      ?.addEventListener("click", () => {
        this.backtrackActive = !this.backtrackActive;
        this.callbacks.onPointsChanged(this.points, this.backtrackActive);
        this.render();
        this.callbacks.onToast(
          this.backtrackActive
            ? "Backtrack route activated along reversed breadcrumbs"
            : "Backtrack route hidden",
          "info"
        );
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='rec-wakelock']")
      ?.addEventListener("click", () => void this.toggleWakeLock());

    this.root
      .querySelector<HTMLButtonElement>("[data-role='rec-motion']")
      ?.addEventListener("click", () => this.toggleMotionSensor());

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-rec-export]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const fmt = btn.dataset.recExport as "gpx" | "geojson";
          this.exportRecorded(fmt);
        });
      });

    this.root
      .querySelector<HTMLFormElement>("[data-role='rec-save-form']")
      ?.addEventListener("submit", (e) => {
        e.preventDefault();
        const form = e.currentTarget as HTMLFormElement;
        const fd = new FormData(form);
        const name = String(fd.get("rec_trip_name") ?? "").trim() || "Recorded Trip";
        const trimVal = Number(fd.get("rec_trim_m") ?? 250);
        const trimCheck = validatePrivacyTrimMeters(trimVal);
        if (!trimCheck.valid) {
          this.errorMsg = trimCheck.error ?? "Privacy trim must be at least 250 m.";
          this.render();
          return;
        }
        const visibility = (String(fd.get("rec_visibility") ?? "community_anonymous") ===
        "private"
          ? "private"
          : "community_anonymous") as TripVisibility;
        const vehicleProfileId = String(fd.get("rec_vehicle_id") ?? "").trim() || null;

        this.savingTrip = true;
        this.errorMsg = null;
        this.render();

        void this.callbacks
          .onSaveRecordedTrip({
            name,
            points: [...this.points],
            visibility,
            privacy_trim_m: trimCheck.value,
            vehicle_profile_id: vehicleProfileId,
          })
          .then(() => {
            this.discardRecording();
          })
          .catch((err) => {
            this.errorMsg =
              err instanceof Error ? err.message : "Failed to save recorded trip";
          })
          .finally(() => {
            this.savingTrip = false;
            this.render();
          });
      });
  }
}
