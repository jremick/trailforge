import type {
  TelemetryPointInput,
  TripItem,
  TripVisibility,
} from "./types";

export const MIN_PRIVACY_TRIM_M = 250;
export const MAX_PRIVACY_TRIM_M = 5000;

export function getTripId(trip: TripItem): string {
  return String(trip.trip_id ?? trip.id ?? "");
}

export function clampPrivacyTrimMeters(raw: number): number {
  if (!Number.isFinite(raw)) return MIN_PRIVACY_TRIM_M;
  return Math.max(MIN_PRIVACY_TRIM_M, Math.min(MAX_PRIVACY_TRIM_M, Math.round(raw)));
}

export function validatePrivacyTrimMeters(raw: number): {
  valid: boolean;
  value: number;
  error?: string;
} {
  if (!Number.isFinite(raw)) {
    return {
      valid: false,
      value: MIN_PRIVACY_TRIM_M,
      error: "Privacy trim must be a number in meters.",
    };
  }
  if (raw < MIN_PRIVACY_TRIM_M) {
    return {
      valid: false,
      value: MIN_PRIVACY_TRIM_M,
      error: `Privacy trim must be at least ${MIN_PRIVACY_TRIM_M} m (start & end buffer enforced by design).`,
    };
  }
  if (raw > MAX_PRIVACY_TRIM_M) {
    return {
      valid: false,
      value: MAX_PRIVACY_TRIM_M,
      error: `Privacy trim cannot exceed ${MAX_PRIVACY_TRIM_M} m.`,
    };
  }
  return { valid: true, value: Math.round(raw) };
}

export function formatTripVisibility(visibility: TripVisibility): {
  label: string;
  shortLabel: string;
  tone: "private" | "community";
  description: string;
} {
  if (visibility === "community_anonymous") {
    return {
      label: "Anonymous Community Contribution",
      shortLabel: "Community Anon",
      tone: "community",
      description:
        "Trims ≥250 m from both ends and publishes anonymized segment traversals with zero user identity.",
    };
  }
  return {
    label: "Private Logbook Only",
    shortLabel: "Private Only",
    tone: "private",
    description:
      "Visible only to your account; zero observations published to community telemetry.",
  };
}

export function formatMatchConfidence(
  confidence: number | null | undefined
): string {
  if (typeof confidence !== "number" || !Number.isFinite(confidence)) {
    return "—";
  }
  const pct = confidence <= 1 ? confidence * 100 : confidence;
  return `${Math.round(Math.max(0, Math.min(100, pct)))}%`;
}

export function formatTrimmedDistance(
  rawLengthM?: number | null,
  trimmedLengthM?: number | null
): string {
  const raw = typeof rawLengthM === "number" && Number.isFinite(rawLengthM) ? rawLengthM : null;
  const trimmed =
    typeof trimmedLengthM === "number" && Number.isFinite(trimmedLengthM)
      ? trimmedLengthM
      : null;

  if (trimmed !== null && raw !== null) {
    const removed = Math.max(0, Math.round(raw - trimmed));
    return `${(trimmed / 1000).toFixed(2)} km (${removed} m trimmed)`;
  }
  if (trimmed !== null) return `${(trimmed / 1000).toFixed(2)} km`;
  if (raw !== null) return `${(raw / 1000).toFixed(2)} km raw`;
  return "—";
}

export function parseTelemetryJsonInput(
  rawText: string
):
  | { ok: true; points: TelemetryPointInput[] }
  | { ok: false; error: string } {
  const trimmed = rawText.trim();
  if (!trimmed) {
    return { ok: false, error: "Paste JSON telemetry points or select a GPX file." };
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return { ok: false, error: "Invalid JSON syntax." };
  }

  const list = Array.isArray(parsed)
    ? parsed
    : parsed && typeof parsed === "object" && Array.isArray((parsed as { points?: unknown }).points)
      ? (parsed as { points: unknown[] }).points
      : null;

  if (!list || list.length < 2) {
    return {
      ok: false,
      error: "Telemetry JSON must be an array or { points: [...] } with at least 2 points.",
    };
  }

  const points: TelemetryPointInput[] = [];
  for (let i = 0; i < list.length; i += 1) {
    const item = list[i];
    if (!item || typeof item !== "object") {
      return { ok: false, error: `Point #${i + 1} must be an object with lon & lat.` };
    }
    const obj = item as Record<string, unknown>;
    const lon = Number(obj.lon);
    const lat = Number(obj.lat);
    if (!Number.isFinite(lon) || !Number.isFinite(lat) || lat < -90 || lat > 90 || lon < -180 || lon > 180) {
      return { ok: false, error: `Point #${i + 1} has invalid lon/lat coordinates.` };
    }
    points.push({
      lon,
      lat,
      elev_m: typeof obj.elev_m === "number" ? obj.elev_m : null,
      timestamp: typeof obj.timestamp === "string" ? obj.timestamp : null,
      speed_mps: typeof obj.speed_mps === "number" ? obj.speed_mps : null,
      roughness_rms: typeof obj.roughness_rms === "number" ? obj.roughness_rms : null,
    });
  }

  return { ok: true, points };
}

export function buildTripExportUrl(
  tripId: string,
  format: "gpx" | "geojson"
): string {
  return `/api/trips/${encodeURIComponent(tripId)}/export?format=${format}`;
}
