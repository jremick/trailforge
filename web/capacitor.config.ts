/**
 * TrailForge Capacitor 7 Native Mobile Shell Configuration.
 *
 * Self-contained TypeScript config (structurally compatible with `@capacitor/cli`
 * `CapacitorConfig`) so `npm run typecheck`, `npm run build`, and `npm test` run
 * 100% offline without requiring native Xcode/Android SDK packages to be installed.
 */

export interface TrailForgeCapacitorConfig {
  appId: string;
  appName: string;
  webDir: string;
  bundledWebRuntime: boolean;
  loggingBehavior?: "none" | "debug" | "production";
  server?: {
    androidScheme?: "http" | "https";
    iosScheme?: string;
    cleartext?: boolean;
    url?: string;
  };
  ios?: {
    contentInset?: "automatic" | "scrollableAxes" | "never" | "always";
    allowsLinkPreview?: boolean;
    preferredContentMode?: "recommended" | "mobile" | "desktop";
    backgroundColor?: string;
  };
  android?: {
    allowMixedContent?: boolean;
    captureInput?: boolean;
    webContentsDebuggingEnabled?: boolean;
    backgroundColor?: string;
  };
  plugins?: {
    SplashScreen?: {
      launchShowDuration?: number;
      launchAutoHide?: boolean;
      backgroundColor?: string;
      showSpinner?: boolean;
      androidScaleType?: string;
    };
    StatusBar?: {
      style?: "DARK" | "LIGHT" | "DEFAULT";
      backgroundColor?: string;
      overlaysWebView?: boolean;
    };
    BackgroundGeolocation?: {
      distanceFilter?: number;
      desiredAccuracy?: "HIGH" | "MEDIUM" | "LOW";
      stale?: boolean;
      backgroundTitle?: string;
      backgroundMessage?: string;
      stopOnTerminate?: boolean;
      startOnBoot?: boolean;
      intervalMs?: number;
    };
    Motion?: {
      sampleRateHz?: number;
      enableClinometer?: boolean;
    };
    KeepAwake?: {
      enabledByDefault?: boolean;
    };
  };
}

export const config: TrailForgeCapacitorConfig = {
  appId: "org.trailforge.app",
  appName: "TrailForge",
  webDir: "dist",
  bundledWebRuntime: false,
  loggingBehavior: "production",
  server: {
    androidScheme: "https",
    iosScheme: "capacitor",
    cleartext: false,
  },
  ios: {
    contentInset: "automatic",
    allowsLinkPreview: false,
    preferredContentMode: "mobile",
    backgroundColor: "#0E151B",
  },
  android: {
    allowMixedContent: false,
    captureInput: true,
    webContentsDebuggingEnabled: false,
    backgroundColor: "#0E151B",
  },
  plugins: {
    SplashScreen: {
      launchShowDuration: 1200,
      launchAutoHide: true,
      backgroundColor: "#0E151B",
      showSpinner: false,
      androidScaleType: "CENTER_CROP",
    },
    StatusBar: {
      style: "DARK",
      backgroundColor: "#0B1117",
      overlaysWebView: false,
    },
    BackgroundGeolocation: {
      distanceFilter: 5,
      desiredAccuracy: "HIGH",
      stale: false,
      backgroundTitle: "TrailForge 4WD Track Recording",
      backgroundMessage:
        "Background GPS & 50 Hz IMU logging active (mandatory >=250 m privacy trim enforced).",
      stopOnTerminate: false,
      startOnBoot: false,
      intervalMs: 1000,
    },
    Motion: {
      sampleRateHz: 50,
      enableClinometer: true,
    },
    KeepAwake: {
      enabledByDefault: false,
    },
  },
};

export default config;
