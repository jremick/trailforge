import {
  type BackgroundLocationHandle,
  type ImuCaptureHandle,
  classifyClinometerAlert,
  getRuntimeMode,
  getRuntimeModeLabel,
  releaseKeepAwake,
  requestKeepAwake,
  runtimeModeToRecordingSource,
  startBackgroundLocationWatch,
  startHighRateImuCapture,
} from "../native/bridge";
import {
  buildClientGeoJson,
  buildClientGpx,
  slugifyExportFilename,
  triggerClientFileDownload,
} from "../offline/exporters";
import {
  SIMULATED_HIGH_COUNTRY_WAYPOINTS,
  buildBacktrackGuidance,
  haversineDistanceM,
  synthesizeSimulatedImuBurst,
} from "../offline/recorderEngine";
import { escapeHtml } from "../presets";
import { validatePrivacyTrimMeters } from "../tripsHelpers";
import type {
  ImuMetrics,
  ImuSample,
  MobileRecordingSource,
  RecordedBreadcrumbPoint,
  RuntimeMode,
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
    imu_samples: ImuSample[];
    recording_source: MobileRecordingSource;
    visibility: TripVisibility;
    privacy_trim_m: number;
    vehicle_profile_id: string | null;
  }) => Promise<void>;
  onToast: (message: string, kind: "info" | "success" | "error") => void;
}

type RecorderPhase = "idle" | "recording" | "paused" | "stopped";

const EMPTY_IMU_METRICS: ImuMetrics = {
  sample_count: 0,
  roughness_rms: null,
  roughness_p95: null,
  shock_event_count: 0,
  max_pitch_deg: null,
  max_roll_deg: null,
  latest_pitch_deg: null,
  latest_roll_deg: null,
};

function formatClock(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds));
  const hrs = Math.floor(s / 3600);
  const mins = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  const mm = String(mins).padStart(2, "0");
  const ss = String(secs).padStart(2, "0");
  return hrs > 0 ? `${hrs}:${mm}:${ss}` : `${mm}:${ss}`;
}

function formatSignedDeg(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "0.0°";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(1)}°`;
}

function clinoBarFillPct(deg: number | null): number {
  if (deg === null || !Number.isFinite(deg)) return 0;
  return Math.min(100, Math.round((Math.abs(deg) / 35) * 100));
}

export class InCabRecorderHud {
  private readonly root: HTMLElement;
  private readonly callbacks: RecorderCallbacks;
  private readonly runtimeMode: RuntimeMode;

  private expanded = false;
  private phase: RecorderPhase = "idle";
  private points: RecordedBreadcrumbPoint[] = [];
  private runningDistanceM = 0;
  private startTimeMs: number | null = null;
  private lastPointTimeMs: number | null = null;
  private lastSpeedKmh = 0;
  private backtrackActive = false;
  private wakeLockEnabled = false;
  private wakeLockStatus: "off" | "active" | "unsupported" = "off";
  private motionEnabled = false;
  private imuHandle: ImuCaptureHandle | null = null;
  private recordedImuSamples: ImuSample[] = [];
  private imuMetrics: ImuMetrics = { ...EMPTY_IMU_METRICS };
  private lastMapPublishMs = 0;
  private mapPublishTimerId: number | null = null;
  private renderedStructKey = "";

  private locationHandle: BackgroundLocationHandle | null = null;
  private isNativeBackgroundGps = false;
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
    this.runtimeMode = getRuntimeMode();
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

  private async ensureImuCapture(silent: boolean): Promise<ImuCaptureHandle> {
    if (this.imuHandle) return this.imuHandle;
    const handle = await startHighRateImuCapture({
      maxSamples: 1500,
      sampleRateHz: 50,
      metricsIntervalMs: 250,
      onMetrics: (metrics) => {
        this.imuMetrics = metrics;
        if (this.expanded && this.phase === "recording") {
          this.patchLiveMetricsInPlace();
        }
      },
    });
    this.imuHandle = handle;
    this.motionEnabled = true;
    if (!silent) {
      this.callbacks.onToast(
        handle.isNativeSensor
          ? "Native 50 Hz IMU & 4WD clinometer active"
          : "4WD clinometer & IMU roughness capture enabled",
        "info"
      );
    }
    return handle;
  }

  private async stopImuCapture(clearBuffer: boolean): Promise<void> {
    if (this.imuHandle) {
      await this.imuHandle.stop();
      if (clearBuffer) {
        this.imuHandle.clear();
        this.imuMetrics = { ...EMPTY_IMU_METRICS };
      }
      this.imuHandle = null;
    } else if (clearBuffer) {
      this.imuMetrics = { ...EMPTY_IMU_METRICS };
    }
    this.motionEnabled = false;
  }

  private async startGpsRecording(): Promise<void> {
    await this.stopTimersAndWatchers();
    this.errorMsg = null;
    this.isSimulating = false;
    this.phase = "recording";
    this.expanded = true;

    if (!this.imuHandle) {
      await this.ensureImuCapture(true);
    }

    const handle = await startBackgroundLocationWatch({
      enableHighAccuracy: true,
      distanceFilterM: 5,
      onLocation: (loc) => {
        if (this.phase !== "recording") return;
        const latestImu = this.imuHandle?.getMetrics() ?? this.imuMetrics;
        this.imuMetrics = latestImu;
        const pt: RecordedBreadcrumbPoint = {
          lon: loc.lon,
          lat: loc.lat,
          elev_m: loc.elev_m,
          timestamp: loc.timestamp,
          accuracy_m: loc.accuracy_m,
          speed_mps: loc.speed_mps,
          roughness_rms: latestImu.roughness_rms,
          pitch_deg: latestImu.latest_pitch_deg,
          roll_deg: latestImu.latest_roll_deg,
        };
        this.appendPoint(pt);
      },
      onError: (msg) => {
        this.errorMsg = `GPS warning: ${msg} (you can use Simulate Drive on desktop)`;
        this.render();
      },
    });

    this.locationHandle = handle;
    this.isNativeBackgroundGps = handle.isNativeBackground;
    this.render();
    this.callbacks.onToast(
      handle.isNativeBackground
        ? "Native locked-screen background GPS + IMU recording started"
        : "In-cab GPS breadcrumb & clinometer recording started",
      "info"
    );
  }

  private async startSimulatedDrive(): Promise<void> {
    await this.stopTimersAndWatchers();
    this.errorMsg = null;
    this.isSimulating = true;
    this.isNativeBackgroundGps = false;
    this.phase = "recording";
    this.expanded = true;

    const imu = await this.ensureImuCapture(true);

    if (
      this.points.length === 0 ||
      this.simIndex >= SIMULATED_HIGH_COUNTRY_WAYPOINTS.length
    ) {
      this.points = [];
      this.resetRunningStats();
      this.simIndex = 0;
      imu.clear();
      this.imuMetrics = { ...EMPTY_IMU_METRICS };
    }

    const pushNext = (): void => {
      if (this.phase !== "recording") return;
      const wp = SIMULATED_HIGH_COUNTRY_WAYPOINTS[this.simIndex];
      if (!wp) {
        void this.stopRecording();
        this.callbacks.onToast(
          "Simulated High Country drive finished — ready to sync or export GPX",
          "success"
        );
        return;
      }
      // Space timestamps cleanly by 25 s per simulated step for realistic 4WD speed metrics
      const baseMs =
        Date.now() -
        (SIMULATED_HIGH_COUNTRY_WAYPOINTS.length - this.simIndex) * 25_000;

      const burst = synthesizeSimulatedImuBurst(wp, baseMs, 8);
      for (const sample of burst) {
        imu.pushSample(sample);
      }
      const metrics = imu.getMetrics();
      this.imuMetrics = metrics;

      this.appendPoint({
        lon: wp.lon,
        lat: wp.lat,
        elev_m: wp.elev_m,
        timestamp: new Date(baseMs).toISOString(),
        accuracy_m: 4.0,
        speed_mps: wp.speed_mps,
        roughness_rms: metrics.roughness_rms ?? wp.roughness_rms,
        pitch_deg: wp.pitch_deg,
        roll_deg: wp.roll_deg,
      });
      this.simIndex += 1;
    };

    pushNext();
    this.simTimerId = window.setInterval(pushNext, 420);
    this.render();
    this.callbacks.onToast(
      "Simulating Davies Plain climb with live 4WD clinometer & rock-ledge IMU shocks",
      "info"
    );
  }

  private async pauseRecording(): Promise<void> {
    if (this.phase !== "recording") return;
    this.phase = "paused";
    await this.stopTimersAndWatchers();
    this.flushMapPointsNow();
    this.render();
  }

  private resumeRecording(): void {
    if (this.phase !== "paused") return;
    if (this.isSimulating) {
      void this.startSimulatedDrive();
    } else {
      void this.startGpsRecording();
    }
  }

  private async stopRecording(): Promise<void> {
    await this.stopTimersAndWatchers();
    this.flushMapPointsNow();
    // Snapshot collected IMU samples & metrics for mobile-sync while releasing live listeners
    if (this.imuHandle) {
      this.imuMetrics = this.imuHandle.getMetrics();
      this.recordedImuSamples = this.imuHandle.getSamples();
      await this.imuHandle.stop();
      this.imuHandle = null;
    }
    this.motionEnabled = false;
    if (this.points.length > 0) {
      this.phase = "stopped";
    } else {
      this.phase = "idle";
    }
    this.expanded = true;
    this.render();
  }

  private async discardRecording(): Promise<void> {
    await this.stopTimersAndWatchers();
    await this.stopImuCapture(true);
    this.recordedImuSamples = [];
    this.points = [];
    this.resetRunningStats();
    this.backtrackActive = false;
    this.phase = "idle";
    this.simIndex = 0;
    this.isSimulating = false;
    this.isNativeBackgroundGps = false;
    this.errorMsg = null;
    this.callbacks.onPointsChanged([], false);
    this.render();
  }

  private resetRunningStats(): void {
    this.runningDistanceM = 0;
    this.startTimeMs = null;
    this.lastPointTimeMs = null;
    this.lastSpeedKmh = 0;
  }

  private getCurrentStats(): {
    distanceM: number;
    durationS: number;
    currentSpeedKmh: number;
    currentElevM: number | null;
    pointCount: number;
    latestRoughnessRms: number | null;
  } {
    const count = this.points.length;
    if (count === 0) {
      return {
        distanceM: 0,
        durationS: 0,
        currentSpeedKmh: 0,
        currentElevM: null,
        pointCount: 0,
        latestRoughnessRms: null,
      };
    }
    const last = this.points[count - 1]!;
    const durationS =
      this.startTimeMs !== null && this.lastPointTimeMs !== null
        ? Math.max(0, Math.round((this.lastPointTimeMs - this.startTimeMs) / 1000))
        : 0;
    return {
      distanceM: Math.round(this.runningDistanceM * 10) / 10,
      durationS,
      currentSpeedKmh: Math.round(this.lastSpeedKmh * 10) / 10,
      currentElevM: last.elev_m ?? null,
      pointCount: count,
      latestRoughnessRms: last.roughness_rms ?? null,
    };
  }

  private scheduleMapPointsPublish(): void {
    const now = Date.now();
    if (now - this.lastMapPublishMs >= 500) {
      this.lastMapPublishMs = now;
      if (this.mapPublishTimerId !== null) {
        window.clearTimeout(this.mapPublishTimerId);
        this.mapPublishTimerId = null;
      }
      this.callbacks.onPointsChanged(this.points, this.backtrackActive);
      return;
    }
    if (this.mapPublishTimerId === null) {
      this.mapPublishTimerId = window.setTimeout(() => {
        this.mapPublishTimerId = null;
        this.lastMapPublishMs = Date.now();
        this.callbacks.onPointsChanged(this.points, this.backtrackActive);
      }, 500 - (now - this.lastMapPublishMs));
    }
  }

  private flushMapPointsNow(): void {
    if (this.mapPublishTimerId !== null) {
      window.clearTimeout(this.mapPublishTimerId);
      this.mapPublishTimerId = null;
    }
    this.lastMapPublishMs = Date.now();
    this.callbacks.onPointsChanged(this.points, this.backtrackActive);
  }

  private appendPoint(pt: RecordedBreadcrumbPoint): void {
    const prev = this.points.length > 0 ? this.points[this.points.length - 1]! : null;
    const ptMs = Date.parse(pt.timestamp);
    if (prev) {
      const segM = haversineDistanceM(prev.lat, prev.lon, pt.lat, pt.lon);
      this.runningDistanceM += segM;
      if (typeof pt.speed_mps === "number" && Number.isFinite(pt.speed_mps)) {
        this.lastSpeedKmh = Math.max(0, pt.speed_mps * 3.6);
      } else if (Number.isFinite(ptMs) && this.lastPointTimeMs !== null) {
        const dtS = (ptMs - this.lastPointTimeMs) / 1000;
        if (dtS > 0.2) {
          this.lastSpeedKmh = Math.min(140, (segM / dtS) * 3.6);
        }
      }
    } else {
      this.startTimeMs = Number.isFinite(ptMs) ? ptMs : null;
      this.lastSpeedKmh =
        typeof pt.speed_mps === "number" && Number.isFinite(pt.speed_mps)
          ? Math.max(0, pt.speed_mps * 3.6)
          : 0;
    }
    if (Number.isFinite(ptMs)) {
      this.lastPointTimeMs = ptMs;
    }

    // Mutate in place (O(1)) instead of cloning the full points array on every GPS fix
    this.points.push(pt);
    this.scheduleMapPointsPublish();
    if (this.points.length === 1 || this.points.length % 5 === 0) {
      this.callbacks.onCenterOnPoint(pt.lat, pt.lon);
    }

    if (this.canPatchInPlace()) {
      this.patchLiveMetricsInPlace();
    } else {
      this.render();
    }
  }

  private computeStructKey(): string {
    return [
      this.expanded ? "1" : "0",
      this.phase,
      this.backtrackActive ? "1" : "0",
      this.errorMsg ?? "",
      this.points.length >= 2 ? "2+" : this.points.length === 1 ? "1" : "0",
      this.wakeLockStatus,
      this.motionEnabled ? "1" : "0",
      this.savingTrip ? "1" : "0",
      this.isNativeBackgroundGps ? "1" : "0",
    ].join("|");
  }

  private canPatchInPlace(): boolean {
    return (
      this.renderedStructKey !== "" &&
      this.renderedStructKey === this.computeStructKey() &&
      !this.backtrackActive
    );
  }

  private patchLiveMetricsInPlace(): void {
    const stats = this.getCurrentStats();
    const pill = this.root.querySelector<HTMLElement>("[data-role='rec-toggle']");
    if (pill) {
      pill.textContent =
        this.phase === "recording"
          ? `● REC ${(stats.distanceM / 1000).toFixed(2)} km · ${formatClock(stats.durationS)}`
          : this.phase === "paused"
            ? `❚❚ PAUSED ${(stats.distanceM / 1000).toFixed(2)} km`
            : this.phase === "stopped"
              ? `■ SAVING (${stats.pointCount} pts)`
              : "● In-Cab Recorder";
    }
    if (!this.expanded) return;

    const setStat = (key: string, value: string): void => {
      const el = this.root.querySelector<HTMLElement>(`[data-rec-stat='${key}']`);
      if (el && el.textContent !== value) el.textContent = value;
    };

    const latestPitch = this.imuMetrics.latest_pitch_deg;
    const latestRoll = this.imuMetrics.latest_roll_deg;
    const clinoAlert = classifyClinometerAlert(latestPitch, latestRoll);
    const pitchAlert = classifyClinometerAlert(latestPitch, 0);
    const rollAlert = classifyClinometerAlert(0, latestRoll);
    const clinoBadge =
      clinoAlert === "hazard"
        ? "⚠ HAZARD ≥25°"
        : clinoAlert === "caution"
          ? "⚠ CAUTION ≥15°"
          : "LEVEL";

    const clinoCard = this.root.querySelector<HTMLElement>("[data-role='clinometer']");
    if (clinoCard) {
      clinoCard.className = `tf-clino-card tf-clino-${clinoAlert}`;
      clinoCard.dataset.severity = clinoAlert;
    }
    const pitchAxis = this.root.querySelector<HTMLElement>("[data-clino-axis='pitch']");
    if (pitchAxis) pitchAxis.className = `tf-clino-axis tf-clino-axis-${pitchAlert}`;
    const rollAxis = this.root.querySelector<HTMLElement>("[data-clino-axis='roll']");
    if (rollAxis) rollAxis.className = `tf-clino-axis tf-clino-axis-${rollAlert}`;
    const pitchBar = this.root.querySelector<HTMLElement>("[data-clino-bar='pitch']");
    if (pitchBar) pitchBar.style.width = `${clinoBarFillPct(latestPitch)}%`;
    const rollBar = this.root.querySelector<HTMLElement>("[data-clino-bar='roll']");
    if (rollBar) rollBar.style.width = `${clinoBarFillPct(latestRoll)}%`;

    const roughDisplay = this.imuMetrics.roughness_rms ?? stats.latestRoughnessRms;
    const p95Display = this.imuMetrics.roughness_p95;

    setStat("clino-badge", clinoBadge);
    setStat("pitch", formatSignedDeg(latestPitch));
    setStat("roll", formatSignedDeg(latestRoll));
    setStat(
      "max-pitch",
      `Max P ${this.imuMetrics.max_pitch_deg !== null ? `${this.imuMetrics.max_pitch_deg.toFixed(1)}°` : "—"}`
    );
    setStat(
      "max-roll",
      `· Max R ${this.imuMetrics.max_roll_deg !== null ? `${this.imuMetrics.max_roll_deg.toFixed(1)}°` : "—"}`
    );
    setStat("imu-count", `· IMU ${this.imuMetrics.sample_count} smpl`);
    setStat("dist", `${(stats.distanceM / 1000).toFixed(2)} km`);
    setStat("time", formatClock(stats.durationS));
    setStat("speed", `${stats.currentSpeedKmh.toFixed(1)} km/h`);
    setStat("elev", stats.currentElevM !== null ? `${Math.round(stats.currentElevM)} m` : "—");
    setStat("pts", String(stats.pointCount));
    setStat("rough", roughDisplay !== null ? `${roughDisplay.toFixed(2)}g` : "—");
    setStat("p95", p95Display !== null ? `${p95Display.toFixed(2)}g` : "—");

    const shocksEl = this.root.querySelector<HTMLElement>("[data-rec-stat='shocks']");
    if (shocksEl) {
      shocksEl.textContent = String(this.imuMetrics.shock_event_count);
      shocksEl.className = this.imuMetrics.shock_event_count > 0 ? "tf-accent-amber" : "";
    }
  }

  private async stopTimersAndWatchers(): Promise<void> {
    if (this.locationHandle) {
      const handle = this.locationHandle;
      this.locationHandle = null;
      await handle.stop();
    }
    if (this.simTimerId !== null) {
      window.clearInterval(this.simTimerId);
      this.simTimerId = null;
    }
    if (this.mapPublishTimerId !== null) {
      window.clearTimeout(this.mapPublishTimerId);
      this.mapPublishTimerId = null;
    }
  }

  private async toggleWakeLock(): Promise<void> {
    if (this.wakeLockEnabled) {
      this.wakeLockEnabled = false;
      this.wakeLockStatus = "off";
      await releaseKeepAwake();
      this.render();
      return;
    }

    const result = await requestKeepAwake(undefined, () => {
      this.wakeLockEnabled = false;
      this.wakeLockStatus = "off";
      this.render();
    });

    if (result.active) {
      this.wakeLockEnabled = true;
      this.wakeLockStatus = "active";
      this.callbacks.onToast(
        result.source === "capacitor"
          ? "Native KeepAwake active for in-cab display"
          : "Screen Wake Lock active for in-cab display",
        "success"
      );
    } else {
      this.wakeLockEnabled = false;
      this.wakeLockStatus = "unsupported";
      this.callbacks.onToast(
        result.reason ??
          "Screen Wake Lock unavailable in this browser — keep screen awake manually",
        "info"
      );
    }
    this.render();
  }

  private async toggleMotionSensor(): Promise<void> {
    if (this.motionEnabled) {
      await this.stopImuCapture(false);
      this.render();
      return;
    }
    await this.ensureImuCapture(false);
    this.render();
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
        description: "Recorded with TrailForge In-Cab Breadcrumb & IMU Recorder",
      });
      triggerClientFileDownload(
        xml,
        slugifyExportFilename(title, "gpx"),
        "application/gpx+xml"
      );
      this.callbacks.onToast(
        "Exported offline GPX 1.1 (GridForge-compatible)",
        "success"
      );
    } else {
      const fc = buildClientGeoJson(title, this.points, {
        description: "Recorded with TrailForge In-Cab Breadcrumb & IMU Recorder",
      });
      triggerClientFileDownload(
        JSON.stringify(fc, null, 2),
        slugifyExportFilename(title, "geojson"),
        "application/geo+json"
      );
      this.callbacks.onToast(
        "Exported offline GeoJSON FeatureCollection",
        "success"
      );
    }
  }

  private render(): void {
    const stats = this.getCurrentStats();
    const backtrack = this.backtrackActive
      ? buildBacktrackGuidance(this.points)
      : null;

    const latestPitch = this.imuMetrics.latest_pitch_deg;
    const latestRoll = this.imuMetrics.latest_roll_deg;
    const clinoAlert = classifyClinometerAlert(latestPitch, latestRoll);
    const pitchAlert = classifyClinometerAlert(latestPitch, 0);
    const rollAlert = classifyClinometerAlert(0, latestRoll);

    const clinoBadge =
      clinoAlert === "hazard"
        ? "⚠ HAZARD ≥25°"
        : clinoAlert === "caution"
          ? "⚠ CAUTION ≥15°"
          : "LEVEL";

    const runtimeLabel = this.isNativeBackgroundGps
      ? "Native BG GPS"
      : getRuntimeModeLabel(this.runtimeMode);

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

    const clinometerHtml = `
      <div class="tf-clino-card tf-clino-${clinoAlert}" data-role="clinometer" data-severity="${clinoAlert}">
        <div class="tf-clino-head">
          <span class="tf-clino-title">4WD Clinometer</span>
          <span class="tf-clino-badge mono" data-rec-stat="clino-badge">${escapeHtml(clinoBadge)}</span>
        </div>
        <div class="tf-clino-grid mono">
          <div class="tf-clino-axis tf-clino-axis-${pitchAlert}" data-clino-axis="pitch">
            <div class="tf-clino-row">
              <span class="tf-stat-label">Pitch</span>
              <strong data-rec-stat="pitch">${escapeHtml(formatSignedDeg(latestPitch))}</strong>
            </div>
            <div class="tf-clino-bar">
              <div class="tf-clino-bar-fill" data-clino-bar="pitch" style="width:${clinoBarFillPct(latestPitch)}%"></div>
            </div>
          </div>
          <div class="tf-clino-axis tf-clino-axis-${rollAlert}" data-clino-axis="roll">
            <div class="tf-clino-row">
              <span class="tf-stat-label">Roll</span>
              <strong data-rec-stat="roll">${escapeHtml(formatSignedDeg(latestRoll))}</strong>
            </div>
            <div class="tf-clino-bar">
              <div class="tf-clino-bar-fill" data-clino-bar="roll" style="width:${clinoBarFillPct(latestRoll)}%"></div>
            </div>
          </div>
        </div>
        <div class="tf-clino-foot mono">
          <span data-rec-stat="max-pitch">Max P ${this.imuMetrics.max_pitch_deg !== null ? `${this.imuMetrics.max_pitch_deg.toFixed(1)}°` : "—"}</span>
          <span data-rec-stat="max-roll">· Max R ${this.imuMetrics.max_roll_deg !== null ? `${this.imuMetrics.max_roll_deg.toFixed(1)}°` : "—"}</span>
          <span data-rec-stat="imu-count">· IMU ${this.imuMetrics.sample_count} smpl</span>
        </div>
      </div>
    `;

    const roughDisplay =
      this.imuMetrics.roughness_rms ?? stats.latestRoughnessRms;
    const p95Display = this.imuMetrics.roughness_p95;

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
                    ? "Syncing…"
                    : this.isOnline && this.isAuthenticated
                      ? "↑ Mobile Sync"
                      : "⧖ Queue Offline"
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
            <div class="tf-recorder-head-left">
              <span class="tf-recorder-title">In-Cab Recorder</span>
              <span class="tf-rec-runtime-pill mono ${this.runtimeMode !== "pwa_browser" ? "native" : ""}" data-role="runtime-mode-pill">${escapeHtml(runtimeLabel)}</span>
            </div>
            <button type="button" class="tf-mini-btn" data-role="rec-collapse" aria-label="Collapse recorder">▾</button>
          </div>

          ${this.errorMsg ? `<div class="tf-inspector-error">${escapeHtml(this.errorMsg)}</div>` : ""}
          ${backtrackHud}
          ${clinometerHtml}

          <div class="tf-recorder-stats mono">
            <div><span class="tf-stat-label">Dist</span><strong data-rec-stat="dist">${(stats.distanceM / 1000).toFixed(2)} km</strong></div>
            <div><span class="tf-stat-label">Time</span><strong data-rec-stat="time">${formatClock(stats.durationS)}</strong></div>
            <div><span class="tf-stat-label">Speed</span><strong data-rec-stat="speed">${stats.currentSpeedKmh.toFixed(1)} km/h</strong></div>
            <div><span class="tf-stat-label">Elev</span><strong data-rec-stat="elev">${stats.currentElevM !== null ? `${Math.round(stats.currentElevM)} m` : "—"}</strong></div>
            <div><span class="tf-stat-label">Pts</span><strong data-rec-stat="pts">${stats.pointCount}</strong></div>
            <div><span class="tf-stat-label">Rough</span><strong data-rec-stat="rough">${roughDisplay !== null ? `${roughDisplay.toFixed(2)}g` : "—"}</strong></div>
            <div><span class="tf-stat-label">Shocks</span><strong data-rec-stat="shocks" class="${this.imuMetrics.shock_event_count > 0 ? "tf-accent-amber" : ""}">${this.imuMetrics.shock_event_count}</strong></div>
            <div><span class="tf-stat-label">P95</span><strong data-rec-stat="p95">${p95Display !== null ? `${p95Display.toFixed(2)}g` : "—"}</strong></div>
          </div>

          <div class="tf-recorder-controls">
            ${
              this.phase === "idle" || this.phase === "stopped"
                ? `<button type="button" class="tf-btn-primary" data-role="rec-start">● Start GPS</button>
                   <button type="button" class="tf-btn-secondary" data-role="rec-sim" title="Step along Davies Plain corridor with live 4WD clinometer & IMU shocks">▶ Simulate Drive</button>`
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
              ☀ Keep Awake: ${this.wakeLockStatus === "active" ? "ON" : this.wakeLockStatus === "unsupported" ? "N/A" : "OFF"}
            </button>
            <button type="button" class="tf-mini-btn ${this.motionEnabled ? "active" : ""}" data-role="rec-motion">
              〰 50 Hz IMU: ${this.motionEnabled ? "ON" : "OFF"}
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

    this.renderedStructKey = this.computeStructKey();
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
      ?.addEventListener("click", () => void this.startGpsRecording());

    this.root
      .querySelector<HTMLButtonElement>("[data-role='rec-sim']")
      ?.addEventListener("click", () => void this.startSimulatedDrive());

    this.root
      .querySelector<HTMLButtonElement>("[data-role='rec-pause']")
      ?.addEventListener("click", () => void this.pauseRecording());

    this.root
      .querySelector<HTMLButtonElement>("[data-role='rec-resume']")
      ?.addEventListener("click", () => this.resumeRecording());

    this.root
      .querySelector<HTMLButtonElement>("[data-role='rec-stop']")
      ?.addEventListener("click", () => void this.stopRecording());

    this.root
      .querySelector<HTMLButtonElement>("[data-role='rec-discard']")
      ?.addEventListener("click", () => void this.discardRecording());

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
      ?.addEventListener("click", () => void this.toggleMotionSensor());

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

        const imuSamples = this.imuHandle
          ? this.imuHandle.getSamples()
          : [...this.recordedImuSamples];
        const recordingSource = runtimeModeToRecordingSource(this.runtimeMode);

        void this.callbacks
          .onSaveRecordedTrip({
            name,
            points: [...this.points],
            imu_samples: imuSamples,
            recording_source: recordingSource,
            visibility,
            privacy_trim_m: trimCheck.value,
            vehicle_profile_id: vehicleProfileId,
          })
          .then(async () => {
            await this.discardRecording();
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
