import { afterEach, describe, expect, it, vi } from "vitest";
import capacitorConfig from "../../capacitor.config";
import { uploadMobileTripSync } from "../api";
import {
  SIMULATED_HIGH_COUNTRY_WAYPOINTS,
  synthesizeSimulatedImuBurst,
} from "../offline/recorderEngine";
import type { ImuSample } from "../types";
import {
  CLINOMETER_CAUTION_DEG,
  CLINOMETER_HAZARD_DEG,
  STANDARD_GRAVITY_MPS2,
  type RuntimeBridgeEnv,
  classifyClinometerAlert,
  computeImuMetrics,
  computePitchRollDeg,
  getRuntimeMode,
  getRuntimeModeLabel,
  releaseKeepAwake,
  requestKeepAwake,
  runtimeModeToRecordingSource,
  startBackgroundLocationWatch,
  startHighRateImuCapture,
} from "./bridge";

describe("Capacitor 7 mobile shell config contract", () => {
  it("defines required TrailForge appId, appName, webDir, and plugin configs offline", () => {
    expect(capacitorConfig.appId).toBe("org.trailforge.app");
    expect(capacitorConfig.appName).toBe("TrailForge");
    expect(capacitorConfig.webDir).toBe("dist");
    expect(capacitorConfig.bundledWebRuntime).toBe(false);
    expect(capacitorConfig.plugins?.BackgroundGeolocation?.distanceFilter).toBeGreaterThan(0);
    expect(capacitorConfig.plugins?.Motion?.sampleRateHz).toBe(50);
    expect(capacitorConfig.plugins?.SplashScreen?.backgroundColor).toBe("#0E151B");
    expect(capacitorConfig.plugins?.StatusBar?.style).toBe("DARK");
  });
});

describe("Pure-TS 4WD clinometer & IMU signal math", () => {
  it("computes pitch and roll degrees accurately and scale-invariantly", () => {
    // Level ground in g and in m/s²
    expect(computePitchRollDeg(0, 0, 1.0)).toEqual({ pitch_deg: 0, roll_deg: 0 });
    expect(computePitchRollDeg(0, 0, STANDARD_GRAVITY_MPS2)).toEqual({
      pitch_deg: 0,
      roll_deg: 0,
    });

    // 30° nose-up climb (ay = sin 30°, az = cos 30°)
    const climb = computePitchRollDeg(0, Math.sin(Math.PI / 6), Math.cos(Math.PI / 6));
    expect(climb.pitch_deg).toBeCloseTo(30, 1);
    expect(climb.roll_deg).toBeCloseTo(0, 1);

    // -20° descent
    const descent = computePitchRollDeg(
      0,
      -Math.sin((20 * Math.PI) / 180),
      Math.cos((20 * Math.PI) / 180)
    );
    expect(descent.pitch_deg).toBeCloseTo(-20, 1);

    // 25° right side-slope roll
    const roll = computePitchRollDeg(
      Math.sin((25 * Math.PI) / 180),
      0,
      Math.cos((25 * Math.PI) / 180)
    );
    expect(roll.roll_deg).toBeCloseTo(25, 1);
    expect(roll.pitch_deg).toBeCloseTo(0, 1);

    // Zero / non-finite guard
    expect(computePitchRollDeg(0, 0, 0)).toEqual({ pitch_deg: 0, roll_deg: 0 });
    expect(computePitchRollDeg(Number.NaN, 0, 1)).toEqual({ pitch_deg: 0, roll_deg: 0 });
  });

  it("classifies 4WD clinometer caution (>=15°) and hazard (>=25°) thresholds symmetrically", () => {
    expect(CLINOMETER_CAUTION_DEG).toBe(15);
    expect(CLINOMETER_HAZARD_DEG).toBe(25);

    expect(classifyClinometerAlert(0, 0)).toBe("normal");
    expect(classifyClinometerAlert(14.9, -14.9)).toBe("normal");
    expect(classifyClinometerAlert(15.0, 2.0)).toBe("caution");
    expect(classifyClinometerAlert(4.0, -18.5)).toBe("caution");
    expect(classifyClinometerAlert(24.9, -24.9)).toBe("caution");
    expect(classifyClinometerAlert(25.0, 0)).toBe("hazard");
    expect(classifyClinometerAlert(-27.2, 5.0)).toBe("hazard");
    expect(classifyClinometerAlert(8.0, -26.4)).toBe("hazard");
    expect(classifyClinometerAlert(null, undefined)).toBe("normal");
  });

  it("distinguishes smooth sealed road, corrugated gravel, and rock-ledge shock spikes", () => {
    expect(computeImuMetrics([])).toEqual({
      sample_count: 0,
      roughness_rms: null,
      roughness_p95: null,
      shock_event_count: 0,
      max_pitch_deg: null,
      max_roll_deg: null,
      latest_pitch_deg: null,
      latest_roll_deg: null,
    });

    // Smooth sealed road (~0.01g noise)
    const smooth: ImuSample[] = Array.from({ length: 40 }, (_, i) => ({
      t_ms: i * 20,
      ax: 0,
      ay: 0,
      az: 1.0 + (i % 2 === 0 ? 0.01 : -0.01),
    }));
    const smoothMetrics = computeImuMetrics(smooth);
    expect(smoothMetrics.sample_count).toBe(40);
    expect(smoothMetrics.roughness_rms).toBeLessThan(0.03);
    expect(smoothMetrics.roughness_p95).toBeLessThan(0.03);
    expect(smoothMetrics.shock_event_count).toBe(0);

    // Corrugated gravel (~0.32g oscillation, zero severe shocks)
    const corrugated: ImuSample[] = Array.from({ length: 50 }, (_, i) => ({
      t_ms: i * 20,
      ax: 0.05,
      ay: 0.12,
      az: 1.0 + Math.sin(i * 0.9) * 0.35,
    }));
    const roughMetrics = computeImuMetrics(corrugated);
    expect(roughMetrics.roughness_rms!).toBeGreaterThan(0.18);
    expect(roughMetrics.roughness_p95!).toBeGreaterThan(roughMetrics.roughness_rms!);
    expect(roughMetrics.shock_event_count).toBe(0);

    // Rock-ledge shock spikes: two distinct impacts (first spans 2 consecutive 50 Hz samples)
    const shocks: ImuSample[] = [
      { t_ms: 0, ax: 0, ay: 0, az: 1.0, pitch_deg: 5.0, roll_deg: -4.0 },
      { t_ms: 20, ax: 0, ay: 0, az: 2.05, pitch_deg: 18.0, roll_deg: -16.0 },
      { t_ms: 40, ax: 0, ay: 0, az: 1.95, pitch_deg: 17.0, roll_deg: -15.0 },
      { t_ms: 60, ax: 0, ay: 0, az: 1.02, pitch_deg: 8.0, roll_deg: -6.0 },
      { t_ms: 80, ax: 0, ay: 0, az: 0.98, pitch_deg: 9.0, roll_deg: 7.0 },
      { t_ms: 100, ax: 0, ay: 0, az: 2.25, pitch_deg: 26.5, roll_deg: 21.0 },
      { t_ms: 120, ax: 0, ay: 0, az: 1.0, pitch_deg: 10.0, roll_deg: 4.0 },
    ];
    const shockMetrics = computeImuMetrics(shocks);
    expect(shockMetrics.shock_event_count).toBe(2);
    expect(shockMetrics.max_pitch_deg).toBeCloseTo(26.5, 1);
    expect(shockMetrics.max_roll_deg).toBeCloseTo(21.0, 1);
    expect(shockMetrics.latest_pitch_deg).toBeCloseTo(10.0, 1);
    expect(shockMetrics.latest_roll_deg).toBeCloseTo(4.0, 1);
  });

  it("synthesizes realistic High Country simulated drive IMU bursts with caution, hazard & shocks", () => {
    const allSamples: ImuSample[] = [];
    const severities = new Set<string>();
    const startMs = Date.parse("2026-11-26T00:00:00Z");

    SIMULATED_HIGH_COUNTRY_WAYPOINTS.forEach((wp, idx) => {
      severities.add(classifyClinometerAlert(wp.pitch_deg, wp.roll_deg));
      allSamples.push(...synthesizeSimulatedImuBurst(wp, startMs + idx * 25_000, 8));
    });

    const metrics = computeImuMetrics(allSamples);
    expect(severities.has("normal")).toBe(true);
    expect(severities.has("caution")).toBe(true);
    expect(severities.has("hazard")).toBe(true);
    expect(metrics.shock_event_count).toBeGreaterThanOrEqual(4);
    expect(metrics.max_pitch_deg!).toBeGreaterThanOrEqual(25);
    expect(metrics.max_roll_deg!).toBeGreaterThanOrEqual(15);
  });
});

describe("Native Capacitor vs Browser PWA runtime bridge", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("detects capacitor_ios, capacitor_android, and pwa_browser accurately", () => {
    expect(getRuntimeMode({ windowObj: undefined })).toBe("pwa_browser");
    expect(
      getRuntimeMode({
        windowObj: {
          Capacitor: { isNativePlatform: () => false, getPlatform: () => "web" },
        },
      })
    ).toBe("pwa_browser");

    const iosMode = getRuntimeMode({
      windowObj: {
        Capacitor: { isNativePlatform: () => true, getPlatform: () => "ios" },
      },
    });
    expect(iosMode).toBe("capacitor_ios");
    expect(runtimeModeToRecordingSource(iosMode)).toBe("capacitor_ios");
    expect(getRuntimeModeLabel(iosMode)).toContain("Native BG GPS");

    const androidMode = getRuntimeMode({
      windowObj: {
        Capacitor: { isNativePlatform: () => true, getPlatform: () => "android" },
      },
    });
    expect(androidMode).toBe("capacitor_android");
    expect(runtimeModeToRecordingSource(androidMode)).toBe("capacitor_android");
    expect(runtimeModeToRecordingSource("pwa_browser")).toBe("pwa");
    expect(getRuntimeModeLabel("pwa_browser")).toBe("PWA Mode");
  });

  it("routes background location watch to native Capacitor plugin when present and browser geolocation otherwise", async () => {
    const removeWatcher = vi.fn();
    let nativeCb: ((loc?: { longitude: number; latitude: number; altitude?: number; speed?: number; time?: number } | null) => void) | null = null;
    const addWatcher = vi.fn((_opts, cb) => {
      nativeCb = cb;
      return Promise.resolve("watch-ios-1");
    });

    const nativeEnv: RuntimeBridgeEnv = {
      windowObj: {
        Capacitor: {
          isNativePlatform: () => true,
          getPlatform: () => "ios",
          Plugins: {
            BackgroundGeolocation: { addWatcher, removeWatcher },
          },
        },
      },
    };

    const received: Array<{ lat: number; lon: number }> = [];
    const nativeHandle = await startBackgroundLocationWatch(
      { onLocation: (pt) => received.push({ lat: pt.lat, lon: pt.lon }) },
      nativeEnv
    );
    expect(nativeHandle.mode).toBe("capacitor_ios");
    expect(nativeHandle.isNativeBackground).toBe(true);
    nativeCb!({ longitude: 148.125, latitude: -36.615, altitude: 1120, speed: 4.5, time: 1700000000000 });
    expect(received).toEqual([{ lat: -36.615, lon: 148.125 }]);
    await nativeHandle.stop();
    expect(removeWatcher).toHaveBeenCalledWith({ id: "watch-ios-1" });

    // Browser PWA fallback
    const clearWatch = vi.fn();
    const watchPosition = vi.fn((success) => {
      success({
        coords: {
          longitude: 150.312,
          latitude: -33.732,
          altitude: 980,
          accuracy: 5,
          speed: 3.1,
        },
        timestamp: 1700000001000,
      });
      return 42;
    });
    const browserHandle = await startBackgroundLocationWatch(
      { onLocation: (pt) => received.push({ lat: pt.lat, lon: pt.lon }) },
      { windowObj: {}, navigatorObj: { geolocation: { watchPosition, clearWatch } } }
    );
    expect(browserHandle.mode).toBe("pwa_browser");
    expect(browserHandle.isNativeBackground).toBe(false);
    expect(received[1]).toEqual({ lat: -33.732, lon: 150.312 });
    await browserHandle.stop();
    expect(clearWatch).toHaveBeenCalledWith(42);
  });

  it("captures bounded IMU samples via native Capacitor Motion plugin and browser ring buffer", async () => {
    const removeListener = vi.fn();
    let accelCb: ((ev: Record<string, unknown>) => void) | null = null;
    const addListener = vi.fn((name: string, cb: (ev: Record<string, unknown>) => void) => {
      if (name === "accel") accelCb = cb;
      return Promise.resolve({ remove: removeListener });
    });

    const imu = await startHighRateImuCapture(
      { maxSamples: 12 },
      {
        windowObj: {
          Capacitor: {
            isNativePlatform: () => true,
            getPlatform: () => "android",
            Plugins: { Motion: { addListener } },
          },
        },
      }
    );

    expect(imu.isNativeSensor).toBe(true);
    expect(imu.mode).toBe("capacitor_android");

    for (let i = 0; i < 15; i += 1) {
      accelCb!({
        accelerationIncludingGravity: {
          x: 0,
          y: 0,
          z: STANDARD_GRAVITY_MPS2 * (i === 10 ? 2.1 : 1.0),
        },
      });
    }
    // Bounded ring buffer keeps latest 12 samples
    expect(imu.getSamples()).toHaveLength(12);
    expect(imu.getMetrics().shock_event_count).toBe(1);
    await imu.stop();
    expect(removeListener).toHaveBeenCalled();
  });

  it("bridges KeepAwake between native Capacitor plugin and browser Screen Wake Lock", async () => {
    const keepAwake = vi.fn();
    const allowSleep = vi.fn();
    const nativeEnv: RuntimeBridgeEnv = {
      windowObj: {
        Capacitor: {
          isNativePlatform: () => true,
          getPlatform: () => "ios",
          Plugins: { KeepAwake: { keepAwake, allowSleep } },
        },
      },
    };
    const nativeRes = await requestKeepAwake(nativeEnv);
    expect(nativeRes).toEqual({ active: true, source: "capacitor" });
    expect(keepAwake).toHaveBeenCalledTimes(1);
    await releaseKeepAwake(nativeEnv);
    expect(allowSleep).toHaveBeenCalledTimes(1);

    // Browser wakeLock fallback
    const release = vi.fn().mockResolvedValue(undefined);
    const wakeEnv: RuntimeBridgeEnv = {
      windowObj: {},
      navigatorObj: {
        wakeLock: {
          request: vi.fn().mockResolvedValue({ release }),
        },
      },
    };
    const webRes = await requestKeepAwake(wakeEnv);
    expect(webRes).toEqual({ active: true, source: "wakelock" });
    await releaseKeepAwake(wakeEnv);
    expect(release).toHaveBeenCalledTimes(1);

    // Unsupported environment degrades cleanly without throwing
    const unsupported = await requestKeepAwake({ windowObj: {}, navigatorObj: {} });
    expect(unsupported.active).toBe(false);
    expect(unsupported.source).toBe("unsupported");
  });

  it("uploads recorded sessions to /api/trips/mobile-sync with automatic /api/trips/upload fallback on 404", async () => {
    const mockTrip = {
      trip_id: "trip-1",
      name: "Davies Climb",
      visibility: "community_anonymous" as const,
      matched_segment_count: 5,
      imu_summary: {
        sample_count: 16,
        roughness_rms: 0.42,
        roughness_p95: 0.78,
        shock_event_count: 2,
        max_pitch_deg: 26.4,
        max_roll_deg: 20.2,
      },
    };

    const fetchOk = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(mockTrip), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      })
    );
    vi.stubGlobal("fetch", fetchOk);

    const synced = await uploadMobileTripSync({
      name: "Davies Climb",
      visibility: "community_anonymous",
      privacy_trim_m: 250,
      recording_source: "capacitor_ios",
      points: [
        { lon: 148.12, lat: -36.56, timestamp: "2026-11-26T00:00:00Z" },
        { lon: 148.13, lat: -36.58, timestamp: "2026-11-26T00:10:00Z" },
      ],
      imu_samples: [{ t_ms: 1000, ax: 0, ay: 0.2, az: 1.0, pitch_deg: 11.3, roll_deg: 0 }],
    });

    expect(synced.imu_summary?.shock_event_count).toBe(2);
    expect(fetchOk).toHaveBeenCalledWith(
      "/api/trips/mobile-sync",
      expect.objectContaining({ method: "POST" })
    );

    // 404 fallback to /api/trips/upload
    const fetchFallback = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "Not Found" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ trip_id: "trip-2", name: "Fallback", visibility: "private" }), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        })
      );
    vi.stubGlobal("fetch", fetchFallback);

    const res = await uploadMobileTripSync({
      name: "Fallback",
      visibility: "private",
      privacy_trim_m: 250,
      points: [
        { lon: 148.12, lat: -36.56, timestamp: "2026-11-26T00:00:00Z" },
        { lon: 148.13, lat: -36.58, timestamp: "2026-11-26T00:10:00Z" },
      ],
    });
    expect(res.trip_id).toBe("trip-2");
    expect(fetchFallback).toHaveBeenNthCalledWith(
      1,
      "/api/trips/mobile-sync",
      expect.objectContaining({ method: "POST" })
    );
    expect(fetchFallback).toHaveBeenNthCalledWith(
      2,
      "/api/trips/upload",
      expect.objectContaining({ method: "POST" })
    );
  });
});
