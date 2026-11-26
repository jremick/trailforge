import type { ConditionReportItem, ConditionReportType } from "./types";

export interface ReportTypeMeta {
  value: ConditionReportType;
  label: string;
  color: string;
  isGateWarning: boolean;
  halfLifeDays: number;
}

export const CONDITION_REPORT_TYPES: readonly ReportTypeMeta[] = [
  {
    value: "locked_gate",
    label: "Locked Gate",
    color: "#EF4444",
    isGateWarning: true,
    halfLifeDays: 60,
  },
  {
    value: "private_property",
    label: "Private Property",
    color: "#F97316",
    isGateWarning: true,
    halfLifeDays: 90,
  },
  {
    value: "fallen_tree",
    label: "Fallen Tree",
    color: "#F59E0B",
    isGateWarning: false,
    halfLifeDays: 14,
  },
  {
    value: "bog_hole",
    label: "Bog Hole",
    color: "#D97706",
    isGateWarning: false,
    halfLifeDays: 21,
  },
  {
    value: "washout",
    label: "Washout",
    color: "#EF4444",
    isGateWarning: false,
    halfLifeDays: 30,
  },
  {
    value: "deep_water",
    label: "Deep Water",
    color: "#38BDF8",
    isGateWarning: false,
    halfLifeDays: 7,
  },
  {
    value: "snow_ice",
    label: "Snow / Ice",
    color: "#BAE6FD",
    isGateWarning: false,
    halfLifeDays: 5,
  },
  {
    value: "track_clear",
    label: "Track Clear",
    color: "#22C55E",
    isGateWarning: false,
    halfLifeDays: 14,
  },
];

const REPORT_TYPE_MAP: Record<string, ReportTypeMeta> = Object.fromEntries(
  CONDITION_REPORT_TYPES.map((t) => [t.value, t])
);

export function getReportId(report: ConditionReportItem): string {
  return String(report.report_id ?? report.id ?? "");
}

export function isGateWarningType(reportType: string): boolean {
  const norm = reportType.trim().toLowerCase();
  return norm === "locked_gate" || norm === "private_property";
}

export function formatReportTypeLabel(reportType: string): string {
  const norm = reportType.trim().toLowerCase();
  const meta = REPORT_TYPE_MAP[norm];
  if (meta) return meta.label;
  return norm.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function getReportTypeColor(reportType: string): string {
  const norm = reportType.trim().toLowerCase();
  return REPORT_TYPE_MAP[norm]?.color ?? "#F59E0B";
}

export function formatReportFreshness(
  freshnessScore?: number | null,
  isStale?: boolean | null,
  effectiveStatus?: string | null
): {
  label: string;
  tone: "fresh" | "aging" | "stale" | "resolved";
  pct: number;
} {
  if ((effectiveStatus ?? "").toLowerCase() === "resolved") {
    return { label: "Resolved", tone: "resolved", pct: 0 };
  }

  const score =
    typeof freshnessScore === "number" && Number.isFinite(freshnessScore)
      ? Math.max(0, Math.min(1, freshnessScore))
      : isStale
        ? 0.25
        : 0.9;
  const pct = Math.round(score * 100);

  if (isStale || score < 0.35) {
    return { label: `Stale (${pct}%)`, tone: "stale", pct };
  }
  if (score < 0.65) {
    return { label: `Aging (${pct}%)`, tone: "aging", pct };
  }
  return { label: `Fresh (${pct}%)`, tone: "fresh", pct };
}

/**
 * Partition condition reports into distinct locked-gate/private-property warnings
 * (surfaced alongside official closures) and the full trail condition timeline.
 */
export function partitionTrackReports(
  reports: readonly ConditionReportItem[] = [],
  explicitGateWarnings?: readonly ConditionReportItem[] | null
): {
  gateWarnings: ConditionReportItem[];
  timelineReports: ConditionReportItem[];
} {
  const seenGate = new Set<string>();
  const gateWarnings: ConditionReportItem[] = [];

  const addGate = (r: ConditionReportItem): void => {
    const key = getReportId(r) || `${r.report_type}:${r.lat}:${r.lon}`;
    if (seenGate.has(key)) return;
    seenGate.add(key);
    gateWarnings.push(r);
  };

  if (explicitGateWarnings && explicitGateWarnings.length > 0) {
    for (const r of explicitGateWarnings) addGate(r);
  }
  for (const r of reports) {
    if (r.is_gate_warning || isGateWarningType(r.report_type)) {
      addGate(r);
    }
  }

  // Ensure every gate warning also appears in timelineReports without duplicating IDs
  const seenTimeline = new Set<string>();
  const timelineReports: ConditionReportItem[] = [];
  for (const r of [...reports, ...gateWarnings]) {
    const key = getReportId(r) || `${r.report_type}:${r.lat}:${r.lon}:${r.notes}`;
    if (seenTimeline.has(key)) continue;
    seenTimeline.add(key);
    timelineReports.push(r);
  }

  return { gateWarnings, timelineReports };
}

/**
 * One-click OpenStreetMap fix/note URLs matching Slice 08 backend `decay.py`.
 */
export function buildOsmFallbackLinks(
  lat: number,
  lon: number,
  osmWayId?: number | string | null
): { osmNoteUrl: string; osmEditUrl: string } {
  const latStr = lat.toFixed(5);
  const lonStr = lon.toFixed(5);
  const osmNoteUrl = `https://www.openstreetmap.org/note/new#map=16/${latStr}/${lonStr}`;
  const osmEditUrl =
    osmWayId !== undefined && osmWayId !== null && String(osmWayId).trim()
      ? `https://www.openstreetmap.org/edit?way=${encodeURIComponent(String(osmWayId))}#map=17/${latStr}/${lonStr}`
      : `https://www.openstreetmap.org/edit#map=17/${latStr}/${lonStr}`;
  return { osmNoteUrl, osmEditUrl };
}

function isSafeHttpsUrl(url: string | null | undefined): url is string {
  return Boolean(url && /^https:\/\//i.test(url.trim()));
}

const SAFE_REPORT_PHOTO_URL_RE = /^\/api\/reports\/photos\/[A-Za-z0-9._-]+$/;

/**
 * Strictly validate a condition-report photo URL against the canonical local
 * API photo path (`/api/reports/photos/<filename>`), rejecting protocol-relative
 * (`//evil.com/...`), external `http(s)://`, `javascript:`, `data:`, and path
 * traversal (`..`) values.
 */
export function sanitizeReportPhotoUrl(
  url: string | null | undefined
): string | null {
  if (typeof url !== "string") return null;
  const trimmed = url.trim();
  if (!SAFE_REPORT_PHOTO_URL_RE.test(trimmed)) return null;
  const filename = trimmed.slice("/api/reports/photos/".length);
  if (filename === "." || filename === ".." || filename.includes("..")) {
    return null;
  }
  return trimmed;
}

export function resolveReportOsmLinks(report: ConditionReportItem): {
  osmNoteUrl: string;
  osmEditUrl: string;
} {
  const fallback = buildOsmFallbackLinks(report.lat, report.lon);
  return {
    osmNoteUrl: isSafeHttpsUrl(report.osm_note_url)
      ? report.osm_note_url.trim()
      : fallback.osmNoteUrl,
    osmEditUrl: isSafeHttpsUrl(report.osm_edit_url)
      ? report.osm_edit_url.trim()
      : fallback.osmEditUrl,
  };
}
