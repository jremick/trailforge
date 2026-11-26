import type {
  ConfidenceLevel,
  RainfallContext,
  VehicleCapabilityClass,
  VehicleClassSplit,
  WetRiskLevel,
} from "./types";

export const OPEN_METEO_ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0)";

export const VEHICLE_SPLIT_ORDER: readonly {
  key: VehicleCapabilityClass;
  shortLabel: string;
  color: string;
}[] = [
  { key: "AWD", shortLabel: "AWD", color: "#22C55E" },
  { key: "Stock 4WD", shortLabel: "Stock 4WD", color: "#38BDF8" },
  { key: "Touring 4WD", shortLabel: "Touring 4WD", color: "#F59E0B" },
  { key: "Modified 4WD", shortLabel: "Modified 4WD", color: "#EF4444" },
];

export interface ConfidenceBadgeInfo {
  level: ConfidenceLevel;
  label: string;
  tone: "none" | "low" | "medium" | "high";
  detail: string;
}

export function formatConfidenceBadge(
  level: string | null | undefined,
  sampleCount = 0
): ConfidenceBadgeInfo {
  const norm = (level ?? "none").trim().toLowerCase();
  const count = Math.max(0, Math.round(Number(sampleCount) || 0));

  if (norm === "high") {
    return {
      level: "high",
      label: "High Confidence",
      tone: "high",
      detail: `${count} recency-weighted traversals`,
    };
  }
  if (norm === "medium") {
    return {
      level: "medium",
      label: "Medium Confidence",
      tone: "medium",
      detail: `${count} traversals (blended with terrain prior)`,
    };
  }
  if (norm === "low" || count > 0) {
    return {
      level: "low",
      label: "Low Confidence",
      tone: "low",
      detail:
        count === 1
          ? "1 traversal — outlier-damped against baseline prior"
          : `${count} traversals — outlier-damped against baseline prior`,
    };
  }
  return {
    level: "none",
    label: "No Live Telemetry",
    tone: "none",
    detail: "Showing deterministic terrain baseline until drivers log traversals",
  };
}

export interface WetRiskBannerInfo {
  level: WetRiskLevel;
  label: string;
  tone: "dry" | "damp" | "wet" | "heavy_rain";
  rain24hText: string;
  rain72hText: string;
  adjustmentText: string | null;
  warning: string | null;
  attribution: string;
  isStale: boolean;
}

export function formatWetRiskBanner(
  rainfall: RainfallContext | null | undefined
): WetRiskBannerInfo | null {
  if (!rainfall) return null;

  const rawLevel = (rainfall.wet_risk_level ?? "dry").trim().toLowerCase();
  const level: WetRiskLevel =
    rawLevel === "heavy_rain" || rawLevel === "saturated"
      ? "heavy_rain"
      : rawLevel === "wet" || rawLevel === "damp" || rawLevel === "dry"
        ? rawLevel
        : "dry";

  const labels: Record<WetRiskLevel, string> = {
    dry: "Dry Conditions",
    damp: "Damp Surface",
    wet: "Wet Track Risk",
    heavy_rain: "Heavy Rain Warning",
  };

  const r24 =
    typeof rainfall.rain_24h_mm === "number" && Number.isFinite(rainfall.rain_24h_mm)
      ? `${rainfall.rain_24h_mm.toFixed(1)} mm`
      : "0.0 mm";
  const r72 =
    typeof rainfall.rain_72h_mm === "number" && Number.isFinite(rainfall.rain_72h_mm)
      ? `${rainfall.rain_72h_mm.toFixed(1)} mm`
      : "0.0 mm";

  const delta =
    typeof rainfall.wet_score_adjustment === "number" &&
    Number.isFinite(rainfall.wet_score_adjustment) &&
    rainfall.wet_score_adjustment > 0
      ? `+${rainfall.wet_score_adjustment.toFixed(1)} pts wet risk`
      : null;

  const rawAttr = (rainfall.attribution ?? "").trim();
  const attribution =
    rawAttr && /open-meteo/i.test(rawAttr) ? rawAttr : OPEN_METEO_ATTRIBUTION;

  return {
    level,
    label: labels[level],
    tone: level,
    rain24hText: r24,
    rain72hText: r72,
    adjustmentText: delta,
    warning: rainfall.warning?.trim() || null,
    attribution,
    isStale: Boolean(rainfall.is_stale),
  };
}

export interface VehicleSplitSegment {
  key: VehicleCapabilityClass;
  shortLabel: string;
  color: string;
  count: number;
  pct: number;
}

export function normalizeVehicleClassSplit(
  split: VehicleClassSplit | null | undefined
): VehicleSplitSegment[] {
  if (!split || typeof split !== "object") return [];

  const counts = VEHICLE_SPLIT_ORDER.map((meta) => {
    const raw = split[meta.key];
    let count = 0;
    let pct: number | null = null;
    if (typeof raw === "number" && Number.isFinite(raw)) {
      count = Math.max(0, Math.round(raw));
    } else if (raw && typeof raw === "object") {
      count = Math.max(0, Math.round(Number(raw.count) || 0));
      if (typeof raw.pct === "number" && Number.isFinite(raw.pct)) {
        pct = raw.pct;
      }
    }
    return { ...meta, count, pct };
  });

  const totalCount = counts.reduce((sum, entry) => sum + entry.count, 0);
  if (totalCount <= 0) return [];

  return counts.map((entry) => {
    const computedPct =
      entry.pct !== null
        ? Math.max(0, Math.min(100, Math.round(entry.pct * 10) / 10))
        : Math.round((entry.count / totalCount) * 1000) / 10;
    return {
      key: entry.key,
      shortLabel: entry.shortLabel,
      color: entry.color,
      count: entry.count,
      pct: computedPct,
    };
  });
}

export function formatDurationMinutes(
  estMinutes: number | null | undefined,
  fallbackSeconds?: number | null
): string {
  let mins: number | null = null;
  if (typeof estMinutes === "number" && Number.isFinite(estMinutes) && estMinutes > 0) {
    mins = estMinutes;
  } else if (
    typeof fallbackSeconds === "number" &&
    Number.isFinite(fallbackSeconds) &&
    fallbackSeconds > 0
  ) {
    mins = fallbackSeconds / 60;
  }
  if (mins === null) return "—";
  if (mins < 1) return "<1 min";
  const rounded = Math.round(mins);
  if (rounded < 60) return `${rounded} min`;
  const hrs = Math.floor(rounded / 60);
  const rem = rounded % 60;
  return rem > 0 ? `${hrs}h ${rem}m` : `${hrs}h`;
}

export function formatLastDriven(
  iso: string | null | undefined,
  nowMs = Date.now()
): string {
  if (!iso) return "Never logged";
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return iso;
  const diffHours = Math.max(0, (nowMs - parsed) / 3_600_000);
  const datePart = new Date(parsed).toISOString().slice(0, 10);
  if (diffHours < 1) return `Just now (${datePart})`;
  if (diffHours < 24) return `${Math.round(diffHours)}h ago (${datePart})`;
  const days = Math.round(diffHours / 24);
  if (days <= 90) return `${days}d ago (${datePart})`;
  return datePart;
}

const HOTSPOT_REASON_LABELS: Record<string, string> = {
  slow_crawl: "Low-speed crawl (≤8 km/h)",
  high_roughness: "High surface roughness / washout",
  relative_slowdown: "Sharp slowdown vs track pace",
  wet_bog_risk: "Wet bog / mud slowdown",
  steep_obstacle: "Steep technical obstacle",
};

export function formatHotspotReason(reason: string): string {
  const key = reason.trim().toLowerCase();
  if (key in HOTSPOT_REASON_LABELS) {
    return HOTSPOT_REASON_LABELS[key]!;
  }
  return reason
    .replace(/_/g, " ")
    .replace(/\b\w/g, (ch) => ch.toUpperCase());
}
