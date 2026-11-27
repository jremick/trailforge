# TrailForge Native Mobile Wrapper (Capacitor 7 — iOS & Android)

TrailForge ships a single TypeScript / Vite / MapLibre GL JS 5 application (`web/`) that runs:

- **In desktop & mobile web browsers** as an installable offline-first PWA (`pwa_browser`).
- **As a native iOS & Android app** wrapped with **Capacitor 7** (`capacitor_ios` / `capacitor_android`) for locked-screen background GPS recording, 50 Hz IMU corrugation & 4WD pitch/roll clinometer telemetry, native screen KeepAwake, and large offline Pan-Australia PMTiles pack storage.

The runtime abstraction lives in [`web/src/native/bridge.ts`](../web/src/native/bridge.ts) and gracefully degrades to standard Web APIs whenever native Capacitor plugins are absent, keeping CI tests and web builds 100% offline with zero native SDK dependencies.

---

## 1. Prerequisites

| Platform | Required Tooling |
| --- | --- |
| Shared | Node.js 20+, npm 10+, TrailForge backend (local homelab or HTTPS server) |
| iOS | macOS with Xcode 16+, iOS 16+ deployment target, CocoaPods or Swift Package Manager, Apple Developer account (for physical device testing) |
| Android | Android Studio Ladybug+ (2024.2+), JDK 17+, Android SDK Platform 34/35 (Android 14/15) |

---

## 2. Install Capacitor 7 & Native Plugins

From `web/` (do **not** commit generated `web/ios/` or `web/android/` build directories unless you maintain your own distribution fork):

```bash
cd web

# Build the production web bundle first (writes web/dist/)
npm ci
npm run typecheck
npm run build

# Install Capacitor 7 core, CLI, and platform packages
npm install @capacitor/core@^7 @capacitor/ios@^7 @capacitor/android@^7
npm install -D @capacitor/cli@^7

# Install native plugins bridged by web/src/native/bridge.ts
npm install \
  @capacitor-community/background-geolocation \
  @capacitor/motion \
  @capacitor-community/keep-awake \
  @capacitor/status-bar \
  @capacitor/splash-screen \
  @capacitor/filesystem
```

TrailForge's Capacitor configuration is already defined in [`web/capacitor.config.ts`](../web/capacitor.config.ts):

- `appId`: `org.trailforge.app`
- `appName`: `TrailForge`
- `webDir`: `dist`
- `bundledWebRuntime`: `false`

---

## 3. iOS Build Guide (Xcode + Locked-Screen Background GPS)

1. **Generate the Xcode project and sync the web bundle:**

   ```bash
   cd web
   npm run build
   npx cap add ios
   npx cap sync ios
   ```

2. **Apply required iOS permissions:**
   Merge every key from [`mobile/ios/Info.plist.snippet.xml`](./ios/Info.plist.snippet.xml) into `web/ios/App/App/Info.plist`:
   - `UIBackgroundModes` (`location`) — keeps GPS breadcrumb capture active while the screen is locked or TrailForge is backgrounded.
   - `NSLocationWhenInUseUsageDescription` & `NSLocationAlwaysAndWhenInUseUsageDescription` — explains remote 4WD tracking, Backtrack guidance, and mandatory `>= 250 m` privacy buffer trimming.
   - `NSMotionUsageDescription` — enables 50 Hz accelerometer roughness and 4WD pitch/roll clinometer capture.

3. **Enable Xcode Capabilities:**
   Open the project in Xcode:

   ```bash
   npx cap open ios
   ```

   - Select **App** target -> **Signing & Capabilities** -> **+ Capability** -> **Background Modes** -> tick **Location updates**.
   - Set **Deployment Target** to `iOS 16.0` or higher.
   - Select your signing Team and connect a physical iPhone or iPad (the iOS Simulator does not provide real 50 Hz accelerometer/clinometer hardware; desktop simulation is available directly via TrailForge's **Simulate Drive** button).

4. **Pointing at a self-hosted TrailForge API:**
   In production, configure your HTTPS TrailForge reverse proxy or set `VITE_API_BASE_URL` before `npm run build` (or configure `server.url` in `web/capacitor.config.ts` during LAN development).

   The native app keeps the session bearer in memory. Sign in again after relaunch to sync pending recordings for your account.

---

## 4. Android Build Guide (Android Studio + Foreground Location Service)

1. **Generate the Android Studio project and sync the web bundle:**

   ```bash
   cd web
   npm run build
   npx cap add android
   npx cap sync android
   ```

2. **Apply required Android permissions & Android 14+ Foreground Service declaration:**
   Merge [`mobile/android/AndroidManifest.snippet.xml`](./android/AndroidManifest.snippet.xml) into `web/android/app/src/main/AndroidManifest.xml`:
   - `ACCESS_FINE_LOCATION` + `ACCESS_COARSE_LOCATION` — high-accuracy GNSS breadcrumbs.
   - `ACCESS_BACKGROUND_LOCATION` — continuous tracking when screen is off (Android 10+ requires granting "Allow all the time" in App Settings after foreground grant).
   - `FOREGROUND_SERVICE` + `FOREGROUND_SERVICE_LOCATION` — required on Android 14+ (API 34+) to keep the GPS recorder alive with a persistent in-cab notification.
   - `WAKE_LOCK` — prevents CPU sleep during long outback climbs when KeepAwake is active.
   - `HIGH_SAMPLING_RATE_SENSORS` — unlocks 50 Hz accelerometer sampling on Android 12+ (API 31+) instead of the default 200 Hz cap error / throttled fallback.
   - `POST_NOTIFICATIONS` — displays the persistent Android 13+ foreground recording notification.

3. **Build & Run in Android Studio:**

   ```bash
   npx cap open android
   ```

   - Sync Gradle with JDK 17+.
   - Test on a physical Android device and set **Battery -> Unrestricted** for TrailForge so OEM battery managers do not throttle background GNSS polling on multi-hour High Country or Simpson Desert runs.

---

## 5. High-Rate 50 Hz IMU Capture, Clinometer & Privacy-First Sync

When recording via the **In-Cab Breadcrumb Recorder**:

1. **Runtime detection (`getRuntimeMode()`):** Automatically shows `Native BG GPS (iOS)` / `Native BG GPS (Android)` inside Capacitor or `PWA Mode` in browsers.
2. **50 Hz IMU ring buffer (`startHighRateImuCapture()`):**
   - Captures `(t_ms, ax, ay, az, pitch_deg, roll_deg)` samples in g units.
   - Computes live gravity-removed `roughness_rms`, `roughness_p95`, rock-ledge `shock_event_count` (peaks `> 0.8g` dynamic jolt or `> 1.8g` total), and `max_pitch_deg` / `max_roll_deg`.
   - Drives the live **4WD Clinometer** warning states: **Caution Amber** (`>= 15°`) and **Hazard Red** (`>= 25°`).
3. **Sync & Privacy Guardrails:**
   - Completed drives upload via `POST /api/trips/mobile-sync`. The recorder starts with private visibility and includes `confirm_community` when you select community sharing.
   - Signed-in drives with no connection queue in IndexedDB under the originating account and sync when that account reconnects. Signed-out recordings can be exported from the recorder. The app leaves older, unassigned queue entries and pins outside account sync.
   - Refresh open browser and installed PWA tabs after updating. Cookie-authenticated or identifiable browser session writes from an older bundle can receive HTTP 428 until it refreshes. Native and terminal bearer clients keep their existing authentication flow.
   - **Privacy by design:** Both GPS breadcrumbs **and** IMU samples falling within the mandatory start/end `>= 250 m` privacy trim buffer are discarded before any anonymized community telemetry is computed.

---

## 6. Offline Pan-Australia PMTiles Packaging on Mobile

TrailForge supports **15 Pan-Australia offline packs** (7 iconic 4WD regions + all 8 states & territories: `VIC`, `NSW`, `ACT`, `TAS`, `QLD`, `WA`, `SA`, `NT`):

1. **In-App Download (recommended):** Open **Offline Packs** -> filter by **All / Iconic 4WD Regions / Statewide** -> tap **Download for Offline**. Each pack stores SHA-256-verified vector PMTiles, Terrain-RGB hillshade PMTiles, graded tracks, POIs, CAPAD reserves, and agency closures locally and serves HTTP 206 byte-range slices offline with zero network calls.
2. **Pre-Bundling Large Regional Packs (`.tar.gz` / `.pmtiles`):**
   - Download the built `.tar.gz` bundle from `/api/packs/<region_id>/bundle.tar.gz` on Wi-Fi.
   - Extract `vector.pmtiles`, `terrain.pmtiles`, `manifest.json`, and `snapshot.json` into the native app's `Documents/trailforge-packs/<region_id>/` directory (iOS Files / Android App Storage) for air-gapped field use or in-dash projection (see [`carplay-android-auto.md`](./carplay-android-auto.md)).

---

## 7. In-Dash Projection (Apple CarPlay & Android Auto)

See [`mobile/carplay-android-auto.md`](./carplay-android-auto.md) for the `CPMapTemplate` (Apple CarPlay) and `NavigationTemplate` (Android Auto) architecture consuming TrailForge's local offline PMTiles, Backtrack breadcrumbs, and live Track Inspector metadata.

---

## 8. License & Attributions

- Code license: **GPL-3.0-or-later**.
- Native builds must retain TrailForge's open-data attributions in the UI footer and offline pack views (OpenStreetMap ODbL, Geoscience Australia / DEA CC BY 4.0, DCCEEW CAPAD 2025 CC BY 4.0, Open-Meteo CC BY 4.0, and contributing Australian state & territory mapping/emergency agencies).
