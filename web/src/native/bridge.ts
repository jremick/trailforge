/**
 * TrailForge Native Runtime Bridge (Capacitor 7 <-> Browser PWA).
 *
 * Provides a zero-crash abstraction over:
 * - Runtime platform detection (`capacitor_ios` | `capacitor_android` | `pwa_browser`)
 * - Background geolocation watching (Capacitor BackgroundGeolocation/Geolocation -> Web Geolocation API)
 * - High-rate 50 Hz IMU accelerometer & 4WD clinometer capture (Capacitor Motion -> Web DeviceMotion/DeviceOrientation)
 * - Screen KeepAwake (Capacitor KeepAwake -> Web Screen Wake Lock API)
 * - Pure TypeScript 4WD clinometer & roughness signal math (100% offline-testable)
 */

import type {
  ImuMetrics,
  ImuSample,
  MobileRecordingSource,
  RuntimeMode,
} from "../types";

export const STANDARD_GRAVITY_MPS2 = 9.80665;
export const CLINOMETER_CAUTION_DEG = 15;
export const CLINOMETER_HAZARD_DEG = 25;
export const SHOCK_DYNAMIC_THRESHOLD_G = 0.8;
export const SHOCK_TOTAL_THRESHOLD_G = 1.8;
export const DEFAULT_IMU_MAX_SAMPLES = 1500;

export type ClinometerSeverity = "normal" | "caution" | "hazard";

export interface BackgroundLocationPoint {
  lon: number;
  lat: number;
  elev_m: number | null;
  accuracy_m: number | null;
  speed_mps: number | null;
  heading_deg: number | null;
  timestamp: string;
}

export interface BackgroundLocationWatchOptions {
  enableHighAccuracy?: boolean;
  distanceFilterM?: number;
  backgroundTitle?: string;
  backgroundMessage?: string;
  onLocation: (pt: BackgroundLocationPoint) => void;
  onError?: (message: string) => void;
}

export interface BackgroundLocationHandle {
  mode: RuntimeMode;
  isNativeBackground: boolean;
  stop: () => Promise<void>;
}

export interface ImuCaptureOptions {
  maxSamples?: number;
  sampleRateHz?: number;
  onMetrics?: (metrics: ImuMetrics) => void;
}

export interface ImuCaptureHandle {
  mode: RuntimeMode;
  isNativeSensor: boolean;
  getSamples: () => ImuSample[];
  getMetrics: () => ImuMetrics;
  pushSample: (sample: ImuSample) => ImuMetrics;
  clear: () => void;
  stop: () => Promise<void>;
}

export interface KeepAwakeResult {
  active: boolean;
  source: "capacitor" | "wakelock" | "unsupported";
  reason?: string;
}

interface CapacitorBgGeoLocation {
  longitude: number;
  latitude: number;
  altitude?: number | null;
  accuracy?: number | null;
  speed?: number | null;
  bearing?: number | null;
  time?: number | null;
}

interface CapacitorBgGeoPlugin {
  addWatcher: (
    options: {
      backgroundMessage?: string;
      backgroundTitle?: string;
      requestPermissions?: boolean;
      stale?: boolean;
      distanceFilter?: number;
    },
    callback: (
      location?: CapacitorBgGeoLocation | null,
      error?: { code?: string; message?: string } | null
    ) => void
  ) => Promise<string> | string;
  removeWatcher: (options: { id: string }) => Promise<void> | void;
}

interface CapacitorGeolocationPlugin {
  watchPosition: (
    options: {
      enableHighAccuracy?: boolean;
      timeout?: number;
      maximumAge?: number;
    },
    callback: (
      position?: {
        coords: {
          longitude: number;
          latitude: number;
          altitude?: number | null;
          accuracy?: number | null;
          speed?: number | null;
          heading?: number | null;
        };
        timestamp?: number;
      } | null,
      error?: { message?: string } | null
    ) => void
  ) => Promise<string> | string;
  clearWatch: (options: { id: string }) => Promise<void> | void;
}

interface CapacitorPluginListenerHandle {
  remove: () => Promise<void> | void;
}

interface CapacitorMotionPlugin {
  addListener: (
    eventName: "accel" | "orientation",
    listenerFunc: (event: Record<string, unknown>) => void
  ) => Promise<CapacitorPluginListenerHandle> | CapacitorPluginListenerHandle;
}

interface CapacitorKeepAwakePlugin {
  keepAwake: () => Promise<void> | void;
  allowSleep: () => Promise<void> | void;
}

export interface CapacitorGlobalLike {
  isNativePlatform?: () => boolean;
  getPlatform?: () => string;
  Plugins?: {
    BackgroundGeolocation?: CapacitorBgGeoPlugin;
    Geolocation?: CapacitorGeolocationPlugin;
    Motion?: CapacitorMotionPlugin;
    KeepAwake?: CapacitorKeepAwakePlugin;
    [key: string]: unknown;
  };
}

interface WakeLockSentinelLike {
  released?: boolean;
  release: () => Promise<void>;
  addEventListener?: (event: string, listener: () => void) => void;
}

export interface RuntimeBridgeEnv {
  windowObj?: (Window & { Capacitor?: CapacitorGlobalLike }) | {
    Capacitor?: CapacitorGlobalLike;
    DeviceMotionEvent?: unknown;
    DeviceOrientationEvent?: unknown;
    addEventListener?: (type: string, listener: (ev: never) => void) => void;
    removeEventListener?: (type: string, listener: (ev: never) => void) => void;
  };
  navigatorObj?: {
    userAgent?: string;
    geolocation?: {
      watchPosition: (
        success: (pos: {
          coords: {
            longitude: number;
            latitude: number;
            altitude: number | null;
            accuracy: number | null;
            speed: number | null;
            heading?: number | null;
          };
          timestamp: number;
        }) => void,
        error?: (err: { message: string }) => void,
        options?: {
          enableHighAccuracy?: boolean;
          maximumAge?: number;
          timeout?: number;
        }
      ) => number;
      clearWatch: (id: number) => void;
    };
    wakeLock?: {
      request: (type: "screen") => Promise<WakeLockSentinelLike>;
    };
  };
}

function resolveWindow(env?: RuntimeBridgeEnv): RuntimeBridgeEnv["windowObj"] {
  if (env && "windowObj" in env) return env.windowObj;
  if (typeof window !== "undefined") {
    return window as Window & { Capacitor?: CapacitorGlobalLike };
  }
  return undefined;
}

function resolveNavigator(
  env?: RuntimeBridgeEnv
): RuntimeBridgeEnv["navigatorObj"] {
  if (env && "navigatorObj" in env) return env.navigatorObj;
  if (typeof navigator !== "undefined") {
    return navigator as unknown as RuntimeBridgeEnv["navigatorObj"];
  }
  return undefined;
}

function round1(value: number): number {
  const rounded = Math.round(value * 10) / 10;
  return rounded === 0 ? 0 : rounded;
}

function round4(value: number): number {
  const rounded = Math.round(value * 10000) / 10000;
  return rounded === 0 ? 0 : rounded;
}

function clampAngleDeg(deg: number): number {
  if (!Number.isFinite(deg)) return 0;
  return Math.max(-90, Math.min(90, deg));
}

function percentile(sorted: readonly number[], p: number): number {
  if (sorted.length === 0) return 0;
  if (sorted.length === 1) return sorted[0]!;
  const idx = Math.max(0, Math.min(1, p)) * (sorted.length - 1);
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  if (lo === hi) return sorted[lo]!;
  const frac = idx - lo;
  return sorted[lo]! * (1 - frac) + sorted[hi]! * frac;
}

/**
 * Detect whether TrailForge is executing inside native Capacitor iOS,
 * native Capacitor Android, or a standard desktop/mobile web PWA browser.
 */
export function getRuntimeMode(env?: RuntimeBridgeEnv): RuntimeMode {
  const win = resolveWindow(env);
  const cap = win?.Capacitor;
  if (cap && typeof cap.isNativePlatform === "function") {
    try {
      if (cap.isNativePlatform()) {
        const platform = (cap.getPlatform?.() ?? "").toLowerCase();
        if (platform === "ios") return "capacitor_ios";
        if (platform === "android") return "capacitor_android";
        const ua = (resolveNavigator(env)?.userAgent ?? "").toLowerCase();
        if (ua.includes("iphone") || ua.includes("ipad") || ua.includes("ios")) {
          return "capacitor_ios";
        }
        return "capacitor_android";
      }
    } catch {
      return "pwa_browser";
    }
  }
  return "pwa_browser";
}

/**
 * Map frontend `RuntimeMode` to backend `POST /api/trips/mobile-sync` `recording_source`.
 */
export function runtimeModeToRecordingSource(
  mode: RuntimeMode
): MobileRecordingSource {
  if (mode === "capacitor_ios") return "capacitor_ios";
  if (mode === "capacitor_android") return "capacitor_android";
  return "pwa";
}

/**
 * Compact badge label for the In-Cab Recorder HUD header.
 */
export function getRuntimeModeLabel(mode: RuntimeMode): string {
  if (mode === "capacitor_ios") return "Native BG GPS · iOS";
  if (mode === "capacitor_android") return "Native BG GPS · Android";
  return "PWA Mode";
}

/**
 * Compute 4WD vehicle pitch and roll angles (in degrees, [-90, +90]) from an
 * accelerometer gravity vector `(ax, ay, az)` in either g-units or m/s² (scale-invariant).
 *
 * Right-handed dash-mount frame:
 * - `+X`: right (starboard) -> positive `roll_deg` when leaning right
 * - `+Y`: forward (nose)    -> positive `pitch_deg` when climbing nose-up
 * - `+Z`: up (roof)         -> `(0, 0, +1g)` on flat level ground
 */
export function computePitchRollDeg(
  ax: number,
  ay: number,
  az: number
): { pitch_deg: number; roll_deg: number } {
  if (!Number.isFinite(ax) || !Number.isFinite(ay) || !Number.isFinite(az)) {
    return { pitch_deg: 0, roll_deg: 0 };
  }
  const mag = Math.hypot(ax, ay, az);
  if (mag < 1e-6) {
    return { pitch_deg: 0, roll_deg: 0 };
  }

  const pitchRad = Math.atan2(ay, Math.hypot(ax, az));
  const rollRad = Math.atan2(ax, Math.hypot(ay, az));
  const pitchDeg = clampAngleDeg((pitchRad * 180) / Math.PI);
  const rollDeg = clampAngleDeg((rollRad * 180) / Math.PI);

  return {
    pitch_deg: round1(pitchDeg),
    roll_deg: round1(rollDeg),
  };
}

/**
 * Classify 4WD clinometer alert severity:
 * - Caution Amber: |pitch| >= 15° or |roll| >= 15°
 * - Hazard Red:    |pitch| >= 25° or |roll| >= 25°
 */
export function classifyClinometerAlert(
  pitchDeg: number | null | undefined,
  rollDeg: number | null | undefined
): ClinometerSeverity {
  const absPitch =
    typeof pitchDeg === "number" && Number.isFinite(pitchDeg)
      ? Math.abs(pitchDeg)
      : 0;
  const absRoll =
    typeof rollDeg === "number" && Number.isFinite(rollDeg)
      ? Math.abs(rollDeg)
      : 0;
  const peak = Math.max(absPitch, absRoll);
  if (peak >= CLINOMETER_HAZARD_DEG) return "hazard";
  if (peak >= CLINOMETER_CAUTION_DEG) return "caution";
  return "normal";
}

/**
 * Compute live roughness RMS, p95 roughness, rock-ledge shock events, and
 * peak/latest clinometer pitch & roll angles from high-rate IMU samples (in g).
 *
 * Mirrors backend `trailforge.matching.imu`:
 * - Gravity baseline removal via batch mean magnitude subtraction
 * - Shock impulse detection: dynamic jolt > 0.8g OR total magnitude > 1.8g
 *   with rising-edge impulse grouping so sustained 50 Hz hits count once
 */
export function computeImuMetrics(samples: readonly ImuSample[]): ImuMetrics {
  const valid = samples.filter(
    (s) =>
      Number.isFinite(s.ax) && Number.isFinite(s.ay) && Number.isFinite(s.az)
  );

  if (valid.length === 0) {
    return {
      sample_count: 0,
      roughness_rms: null,
      roughness_p95: null,
      shock_event_count: 0,
      max_pitch_deg: null,
      max_roll_deg: null,
      latest_pitch_deg: null,
      latest_roll_deg: null,
    };
  }

  const magnitudes = valid.map((s) => Math.hypot(s.ax, s.ay, s.az));
  const baseline =
    valid.length === 1
      ? 1.0
      : magnitudes.reduce((acc, m) => acc + m, 0) / magnitudes.length;

  const dynamicDevs: number[] = [];
  let sumSq = 0;
  let shockCount = 0;
  let inShock = false;
  let maxPitchAbs = 0;
  let maxRollAbs = 0;
  let latestPitch = 0;
  let latestRoll = 0;

  for (let i = 0; i < valid.length; i += 1) {
    const sample = valid[i]!;
    const mag = magnitudes[i]!;
    const dyn = Math.abs(mag - baseline);
    dynamicDevs.push(dyn);
    sumSq += dyn * dyn;

    const isShock =
      dyn > SHOCK_DYNAMIC_THRESHOLD_G || mag > SHOCK_TOTAL_THRESHOLD_G;
    if (isShock && !inShock) {
      shockCount += 1;
      inShock = true;
    } else if (!isShock) {
      inShock = false;
    }

    const derived = computePitchRollDeg(sample.ax, sample.ay, sample.az);
    const pitch =
      typeof sample.pitch_deg === "number" && Number.isFinite(sample.pitch_deg)
        ? round1(clampAngleDeg(sample.pitch_deg))
        : derived.pitch_deg;
    const roll =
      typeof sample.roll_deg === "number" && Number.isFinite(sample.roll_deg)
        ? round1(clampAngleDeg(sample.roll_deg))
        : derived.roll_deg;

    if (Math.abs(pitch) > maxPitchAbs) maxPitchAbs = Math.abs(pitch);
    if (Math.abs(roll) > maxRollAbs) maxRollAbs = Math.abs(roll);
    latestPitch = pitch;
    latestRoll = roll;
  }

  const sortedDyn = [...dynamicDevs].sort((a, b) => a - b);
  const rms = Math.sqrt(sumSq / valid.length);
  const p95 = percentile(sortedDyn, 0.95);

  return {
    sample_count: valid.length,
    roughness_rms: round4(rms),
    roughness_p95: round4(p95),
    shock_event_count: shockCount,
    max_pitch_deg: round1(maxPitchAbs),
    max_roll_deg: round1(maxRollAbs),
    latest_pitch_deg: latestPitch,
    latest_roll_deg: latestRoll,
  };
}

/**
 * Start background-capable GPS location tracking:
 * 1. Native Capacitor `BackgroundGeolocation` plugin (locked-screen iOS/Android)
 * 2. Native Capacitor `Geolocation` plugin fallback
 * 3. Standard browser `navigator.geolocation.watchPosition`
 */
export async function startBackgroundLocationWatch(
  options: BackgroundLocationWatchOptions,
  env?: RuntimeBridgeEnv
): Promise<BackgroundLocationHandle> {
  const mode = getRuntimeMode(env);
  const win = resolveWindow(env);
  const nav = resolveNavigator(env);
  const plugins = win?.Capacitor?.Plugins;

  if (mode !== "pwa_browser" && plugins?.BackgroundGeolocation?.addWatcher) {
    const bgPlugin = plugins.BackgroundGeolocation;
    try {
      const watcherId = await bgPlugin.addWatcher(
        {
          backgroundTitle:
            options.backgroundTitle ?? "TrailForge 4WD Track Recording",
          backgroundMessage:
            options.backgroundMessage ??
            "Background GPS & IMU logging active (>=250 m privacy trim).",
          requestPermissions: true,
          stale: false,
          distanceFilter: options.distanceFilterM ?? 5,
        },
        (loc, err) => {
          if (err) {
            options.onError?.(err.message ?? err.code ?? "Background GPS error");
            return;
          }
          if (!loc) return;
          options.onLocation({
            lon: loc.longitude,
            lat: loc.latitude,
            elev_m:
              typeof loc.altitude === "number" && Number.isFinite(loc.altitude)
                ? round1(loc.altitude)
                : null,
            accuracy_m:
              typeof loc.accuracy === "number" && Number.isFinite(loc.accuracy)
                ? loc.accuracy
                : null,
            speed_mps:
              typeof loc.speed === "number" && Number.isFinite(loc.speed)
                ? Math.max(0, loc.speed)
                : null,
            heading_deg:
              typeof loc.bearing === "number" && Number.isFinite(loc.bearing)
                ? loc.bearing
                : null,
            timestamp: new Date(loc.time || Date.now()).toISOString(),
          });
        }
      );

      return {
        mode,
        isNativeBackground: true,
        stop: async () => {
          try {
            await bgPlugin.removeWatcher({ id: watcherId });
          } catch {
            // Ignore plugin cleanup errors
          }
        },
      };
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : "Native BackgroundGeolocation failed; falling back";
      options.onError?.(message);
    }
  }

  if (mode !== "pwa_browser" && plugins?.Geolocation?.watchPosition) {
    const geoPlugin = plugins.Geolocation;
    try {
      const watchId = await geoPlugin.watchPosition(
        {
          enableHighAccuracy: options.enableHighAccuracy ?? true,
          maximumAge: 2000,
          timeout: 12000,
        },
        (pos, err) => {
          if (err) {
            options.onError?.(err.message ?? "Capacitor GPS warning");
            return;
          }
          if (!pos?.coords) return;
          options.onLocation({
            lon: pos.coords.longitude,
            lat: pos.coords.latitude,
            elev_m:
              typeof pos.coords.altitude === "number" &&
              Number.isFinite(pos.coords.altitude)
                ? round1(pos.coords.altitude)
                : null,
            accuracy_m: pos.coords.accuracy ?? null,
            speed_mps:
              typeof pos.coords.speed === "number" &&
              Number.isFinite(pos.coords.speed)
                ? Math.max(0, pos.coords.speed)
                : null,
            heading_deg: pos.coords.heading ?? null,
            timestamp: new Date(pos.timestamp || Date.now()).toISOString(),
          });
        }
      );

      return {
        mode,
        isNativeBackground: true,
        stop: async () => {
          try {
            await geoPlugin.clearWatch({ id: watchId });
          } catch {
            // Ignore cleanup errors
          }
        },
      };
    } catch {
      // Fall through to browser navigator.geolocation
    }
  }

  if (nav?.geolocation && typeof nav.geolocation.watchPosition === "function") {
    const watchId = nav.geolocation.watchPosition(
      (pos) => {
        options.onLocation({
          lon: pos.coords.longitude,
          lat: pos.coords.latitude,
          elev_m:
            typeof pos.coords.altitude === "number" &&
            Number.isFinite(pos.coords.altitude)
              ? round1(pos.coords.altitude)
              : null,
          accuracy_m: pos.coords.accuracy ?? null,
          speed_mps:
            typeof pos.coords.speed === "number" &&
            Number.isFinite(pos.coords.speed)
              ? Math.max(0, pos.coords.speed)
              : null,
          heading_deg:
            typeof pos.coords.heading === "number" &&
            Number.isFinite(pos.coords.heading)
              ? pos.coords.heading
              : null,
          timestamp: new Date(pos.timestamp || Date.now()).toISOString(),
        });
      },
      (err) => {
        options.onError?.(err.message);
      },
      {
        enableHighAccuracy: options.enableHighAccuracy ?? true,
        maximumAge: 2000,
        timeout: 12000,
      }
    );

    return {
      mode,
      isNativeBackground: false,
      stop: async () => {
        try {
          nav.geolocation?.clearWatch(watchId);
        } catch {
          // Ignore browser cleanup errors
        }
      },
    };
  }

  options.onError?.(
    "Geolocation unavailable in this environment — use Simulate Drive to test."
  );
  return {
    mode,
    isNativeBackground: false,
    stop: async () => undefined,
  };
}

/**
 * Start bounded high-rate IMU & 4WD clinometer capture:
 * - Native Capacitor `Motion` plugin (`accel` + `orientation`) when available
 * - Browser `DeviceMotionEvent` + `DeviceOrientationEvent` fallback
 * - Supports synthetic sample injection via `pushSample()` for desktop Simulate Drive
 */
export async function startHighRateImuCapture(
  options: ImuCaptureOptions = {},
  env?: RuntimeBridgeEnv
): Promise<ImuCaptureHandle> {
  const mode = getRuntimeMode(env);
  const win = resolveWindow(env);
  const maxSamples = Math.max(10, Math.floor(options.maxSamples ?? DEFAULT_IMU_MAX_SAMPLES));

  const ring: ImuSample[] = [];
  let cachedOrientation: { pitch_deg: number | null; roll_deg: number | null } = {
    pitch_deg: null,
    roll_deg: null,
  };
  let isNativeSensor = false;
  let stopped = false;
  const cleanups: Array<() => Promise<void> | void> = [];

  const pushSample = (sample: ImuSample): ImuMetrics => {
    const derived = computePitchRollDeg(sample.ax, sample.ay, sample.az);
    const normalized: ImuSample = {
      t_ms: Number.isFinite(sample.t_ms) ? Math.round(sample.t_ms) : Date.now(),
      ax: round4(sample.ax),
      ay: round4(sample.ay),
      az: round4(sample.az),
      pitch_deg:
        typeof sample.pitch_deg === "number" && Number.isFinite(sample.pitch_deg)
          ? round1(clampAngleDeg(sample.pitch_deg))
          : (cachedOrientation.pitch_deg ?? derived.pitch_deg),
      roll_deg:
        typeof sample.roll_deg === "number" && Number.isFinite(sample.roll_deg)
          ? round1(clampAngleDeg(sample.roll_deg))
          : (cachedOrientation.roll_deg ?? derived.roll_deg),
    };
    ring.push(normalized);
    if (ring.length > maxSamples) {
      ring.splice(0, ring.length - maxSamples);
    }
    const metrics = computeImuMetrics(ring);
    options.onMetrics?.(metrics);
    return metrics;
  };

  const motionPlugin = win?.Capacitor?.Plugins?.Motion;
  if (mode !== "pwa_browser" && motionPlugin?.addListener) {
    try {
      const accelHandle = await motionPlugin.addListener("accel", (ev) => {
        if (stopped) return;
        const accelG =
          (ev.accelerationIncludingGravity as
            | { x?: number; y?: number; z?: number }
            | undefined) ??
          (ev.acceleration as
            | { x?: number; y?: number; z?: number }
            | undefined);
        if (!accelG) return;
        const ax = (accelG.x ?? 0) / STANDARD_GRAVITY_MPS2;
        const ay = (accelG.y ?? 0) / STANDARD_GRAVITY_MPS2;
        const az = (accelG.z ?? 0) / STANDARD_GRAVITY_MPS2;
        if (!Number.isFinite(ax) || !Number.isFinite(ay) || !Number.isFinite(az)) {
          return;
        }
        pushSample({
          t_ms: Date.now(),
          ax,
          ay,
          az,
        });
      });
      cleanups.push(() => accelHandle.remove());

      try {
        const orientHandle = await motionPlugin.addListener("orientation", (ev) => {
          if (stopped) return;
          const beta = typeof ev.beta === "number" ? ev.beta : null;
          const gamma = typeof ev.gamma === "number" ? ev.gamma : null;
          cachedOrientation = {
            pitch_deg: beta !== null && Number.isFinite(beta) ? round1(clampAngleDeg(beta)) : null,
            roll_deg: gamma !== null && Number.isFinite(gamma) ? round1(clampAngleDeg(gamma)) : null,
          };
        });
        cleanups.push(() => orientHandle.remove());
      } catch {
        // Orientation listener is optional; pitch/roll still derived from gravity vector
      }

      isNativeSensor = true;
    } catch {
      isNativeSensor = false;
    }
  }

  if (!isNativeSensor && win && typeof win.addEventListener === "function") {
    const onOrientation = (ev: DeviceOrientationEvent): void => {
      if (stopped) return;
      const beta = typeof ev.beta === "number" && Number.isFinite(ev.beta) ? ev.beta : null;
      const gamma = typeof ev.gamma === "number" && Number.isFinite(ev.gamma) ? ev.gamma : null;
      if (beta !== null || gamma !== null) {
        cachedOrientation = {
          pitch_deg: beta !== null ? round1(clampAngleDeg(beta)) : cachedOrientation.pitch_deg,
          roll_deg: gamma !== null ? round1(clampAngleDeg(gamma)) : cachedOrientation.roll_deg,
        };
      }
    };

    const onMotion = (ev: DeviceMotionEvent): void => {
      if (stopped) return;
      const acc = ev.accelerationIncludingGravity ?? ev.acceleration;
      if (!acc) return;
      const rawX = acc.x ?? 0;
      const rawY = acc.y ?? 0;
      const rawZ =
        acc.z ?? (ev.accelerationIncludingGravity ? STANDARD_GRAVITY_MPS2 : 0);
      const ax = rawX / STANDARD_GRAVITY_MPS2;
      const ay = rawY / STANDARD_GRAVITY_MPS2;
      const az = ev.accelerationIncludingGravity
        ? rawZ / STANDARD_GRAVITY_MPS2
        : 1.0 + rawZ / STANDARD_GRAVITY_MPS2;
      if (!Number.isFinite(ax) || !Number.isFinite(ay) || !Number.isFinite(az)) {
        return;
      }
      pushSample({
        t_ms: Date.now(),
        ax,
        ay,
        az,
      });
    };

    if ("DeviceOrientationEvent" in win) {
      win.addEventListener("deviceorientation", onOrientation as (ev: never) => void);
      cleanups.push(() => {
        win.removeEventListener?.(
          "deviceorientation",
          onOrientation as (ev: never) => void
        );
      });
    }
    if ("DeviceMotionEvent" in win) {
      win.addEventListener("devicemotion", onMotion as (ev: never) => void);
      cleanups.push(() => {
        win.removeEventListener?.("devicemotion", onMotion as (ev: never) => void);
      });
    }
  }

  return {
    mode,
    isNativeSensor,
    getSamples: () => [...ring],
    getMetrics: () => computeImuMetrics(ring),
    pushSample,
    clear: () => {
      ring.length = 0;
      cachedOrientation = { pitch_deg: null, roll_deg: null };
    },
    stop: async () => {
      stopped = true;
      for (const fn of cleanups.splice(0, cleanups.length)) {
        try {
          await fn();
        } catch {
          // Ignore cleanup errors
        }
      }
    },
  };
}

let activeWakeLockSentinel: WakeLockSentinelLike | null = null;
let activeWakeLockSource: "capacitor" | "wakelock" | null = null;

/**
 * Request screen KeepAwake: Capacitor `KeepAwake` plugin -> browser `navigator.wakeLock`.
 */
export async function requestKeepAwake(
  env?: RuntimeBridgeEnv,
  onReleased?: () => void
): Promise<KeepAwakeResult> {
  const mode = getRuntimeMode(env);
  const win = resolveWindow(env);
  const nav = resolveNavigator(env);
  const keepAwakePlugin = win?.Capacitor?.Plugins?.KeepAwake;

  if (mode !== "pwa_browser" && keepAwakePlugin?.keepAwake) {
    try {
      await keepAwakePlugin.keepAwake();
      activeWakeLockSource = "capacitor";
      return { active: true, source: "capacitor" };
    } catch (err) {
      return {
        active: false,
        source: "unsupported",
        reason: err instanceof Error ? err.message : "Native KeepAwake failed",
      };
    }
  }

  if (nav?.wakeLock && typeof nav.wakeLock.request === "function") {
    try {
      const sentinel = await nav.wakeLock.request("screen");
      activeWakeLockSentinel = sentinel;
      activeWakeLockSource = "wakelock";
      sentinel.addEventListener?.("release", () => {
        if (activeWakeLockSentinel === sentinel) {
          activeWakeLockSentinel = null;
          activeWakeLockSource = null;
        }
        onReleased?.();
      });
      return { active: true, source: "wakelock" };
    } catch (err) {
      return {
        active: false,
        source: "unsupported",
        reason:
          err instanceof Error
            ? err.message
            : "Screen Wake Lock denied or unavailable",
      };
    }
  }

  return {
    active: false,
    source: "unsupported",
    reason: "Screen Wake Lock API not supported in this browser",
  };
}

/**
 * Release active native KeepAwake or browser Screen Wake Lock sentinel.
 */
export async function releaseKeepAwake(env?: RuntimeBridgeEnv): Promise<void> {
  const win = resolveWindow(env);
  const keepAwakePlugin = win?.Capacitor?.Plugins?.KeepAwake;

  if (activeWakeLockSource === "capacitor" && keepAwakePlugin?.allowSleep) {
    try {
      await keepAwakePlugin.allowSleep();
    } catch {
      // Ignore native release errors
    }
  }

  if (activeWakeLockSentinel) {
    try {
      await activeWakeLockSentinel.release();
    } catch {
      // Ignore browser wake lock release errors
    }
  }

  activeWakeLockSentinel = null;
  activeWakeLockSource = null;
}
