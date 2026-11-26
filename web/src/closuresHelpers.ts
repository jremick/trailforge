import type {
  BBoxTuple,
  ClosureDetailResponse,
  ClosuresStatusResponse,
  FeedStatusItem,
} from "./types";

export type ClosureTone = "critical" | "warning" | "info";

const CLOSURE_TYPE_LABELS: Record<string, string> = {
  closed: "Closed",
  road_closure: "Road Closure",
  park_closure: "Park Closure",
  seasonal: "Seasonal Closure",
  seasonal_closure: "Seasonal Closure",
  incident: "Emergency Incident",
  bushfire: "Bushfire Incident",
  flood: "Flood Warning",
  park_alert: "Park Alert",
  hazard_warning: "Hazard Advisory",
  locked_gate: "Locked Gate",
  private_gate: "Locked Gate",
  private_access: "Private Access",
};

export function formatClosureTypeLabel(closureType: string): string {
  const key = closureType.trim().toLowerCase();
  return (
    CLOSURE_TYPE_LABELS[key] ??
    key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())
  );
}

export function getClosureTone(
  closureType: string,
  severity?: string | null
): ClosureTone {
  const ct = closureType.trim().toLowerCase();
  const sev = (severity ?? "").trim().toLowerCase();

  if (
    sev === "critical" ||
    sev === "emergency" ||
    sev === "closed" ||
    ct === "closed" ||
    ct === "road_closure" ||
    ct === "park_closure" ||
    ct === "incident" ||
    ct === "bushfire" ||
    ct === "flood"
  ) {
    return "critical";
  }

  if (
    sev === "warning" ||
    sev === "watch_and_act" ||
    ct === "seasonal" ||
    ct === "seasonal_closure" ||
    ct === "locked_gate" ||
    ct === "private_gate" ||
    ct === "private_access"
  ) {
    return "warning";
  }

  return "info";
}

export function getClosureStatusBadge(status: string | null | undefined): {
  label: string;
  tone: ClosureTone | "open";
} {
  const norm = (status ?? "open").trim().toLowerCase();
  if (norm === "closed") return { label: "CLOSED", tone: "critical" };
  if (norm === "seasonal") return { label: "SEASONAL", tone: "warning" };
  if (norm === "gated") return { label: "GATED / PRIVATE", tone: "warning" };
  if (norm === "warning") return { label: "ALERT", tone: "warning" };
  if (norm === "info") return { label: "ADVISORY", tone: "info" };
  return { label: "OPEN", tone: "open" };
}

export function formatClosureDateRange(
  effectiveFrom?: string | null,
  effectiveTo?: string | null
): string {
  const fmt = (iso: string): string | null => {
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return null;
    return dt.toLocaleDateString("en-AU", {
      day: "numeric",
      month: "short",
      year: "numeric",
      timeZone: "UTC",
    });
  };

  const fromStr = effectiveFrom ? fmt(effectiveFrom) : null;
  const toStr = effectiveTo ? fmt(effectiveTo) : null;

  if (fromStr && toStr) return `${fromStr} – ${toStr}`;
  if (fromStr) return `From ${fromStr}`;
  if (toStr) return `Until ${toStr}`;
  return "";
}

export function formatRelativeTimestamp(
  iso?: string | null,
  nowMs: number = Date.now()
): string {
  if (!iso) return "Never";
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return "Unknown";
  const diffSec = Math.max(0, Math.floor((nowMs - ts) / 1000));
  if (diffSec < 60) return "Just now";
  const mins = Math.floor(diffSec / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 48) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

export interface FeedHealthSummary {
  kind: "healthy" | "degraded" | "stale" | "offline";
  label: string;
  staleFeeds: FeedStatusItem[];
  degradedFeeds: FeedStatusItem[];
  totalRecords: number;
  tooltip: string;
}

export function summarizeFeedStatus(
  status: ClosuresStatusResponse | null
): FeedHealthSummary {
  if (!status) {
    return {
      kind: "offline",
      label: "Feeds Offline",
      staleFeeds: [],
      degradedFeeds: [],
      totalRecords: 0,
      tooltip: "Closure feed status unavailable — check API connection",
    };
  }

  const feeds = status.feeds ?? [];
  const staleFeeds = feeds.filter((f) => f.is_stale);
  const degradedFeeds = feeds.filter(
    (f) => f.status === "degraded" || f.status === "failed"
  );
  const totalRecords = feeds.reduce((acc, f) => acc + (f.record_count ?? 0), 0);

  const overall = status.overall_status.toLowerCase();
  if (overall === "degraded" || degradedFeeds.length > 0) {
    const names = (degradedFeeds.length > 0 ? degradedFeeds : staleFeeds)
      .map((f) => f.display_name ?? f.feed_source)
      .join(", ");
    return {
      kind: "degraded",
      label: "Feeds Degraded",
      staleFeeds,
      degradedFeeds,
      totalRecords,
      tooltip: names
        ? `Upstream outage / stale cache: ${names}`
        : "One or more official VIC/NSW feeds degraded",
    };
  }

  if (overall === "stale" || staleFeeds.length > 0) {
    const names = staleFeeds
      .map((f) => f.display_name ?? f.feed_source)
      .join(", ");
    return {
      kind: "stale",
      label: "Feeds Stale",
      staleFeeds,
      degradedFeeds,
      totalRecords,
      tooltip: names
        ? `Cached closure data stale for: ${names}`
        : "One or more closure feeds are stale",
    };
  }

  return {
    kind: "healthy",
    label: `Feeds Live (${totalRecords})`,
    staleFeeds: [],
    degradedFeeds: [],
    totalRecords,
    tooltip: `${feeds.length} VIC/NSW official closure feeds fresh (${totalRecords} active records)`,
  };
}

/**
 * Only permit safe external `http://` or `https://` agency links without control chars.
 */
export function sanitizeExternalHttpUrl(
  url: string | null | undefined
): string | null {
  if (!url) return null;
  const trimmed = url.trim();
  if (!trimmed || /[\r\n\0]/.test(trimmed)) return null;
  return /^https?:\/\//i.test(trimmed) ? trimmed : null;
}

/**
 * Compute an [west, south, east, north] bounding box from any GeoJSON geometry.
 * Points receive a small visual padding (~1.5 km) so zooming frames the area cleanly.
 */
export function computeGeometryBBox(geometry: unknown): BBoxTuple | null {
  if (!geometry || typeof geometry !== "object") return null;
  const geom = geometry as {
    type?: unknown;
    coordinates?: unknown;
    geometries?: unknown;
  };

  let minLon = Infinity;
  let minLat = Infinity;
  let maxLon = -Infinity;
  let maxLat = -Infinity;

  const visitCoords = (node: unknown): void => {
    if (!Array.isArray(node)) return;
    if (
      node.length >= 2 &&
      typeof node[0] === "number" &&
      typeof node[1] === "number" &&
      Number.isFinite(node[0]) &&
      Number.isFinite(node[1])
    ) {
      const lon = node[0];
      const lat = node[1];
      if (lon < minLon) minLon = lon;
      if (lat < minLat) minLat = lat;
      if (lon > maxLon) maxLon = lon;
      if (lat > maxLat) maxLat = lat;
      return;
    }
    for (const child of node) {
      visitCoords(child);
    }
  };

  if (geom.type === "GeometryCollection" && Array.isArray(geom.geometries)) {
    for (const sub of geom.geometries) {
      const subBox = computeGeometryBBox(sub);
      if (subBox) {
        if (subBox[0] < minLon) minLon = subBox[0];
        if (subBox[1] < minLat) minLat = subBox[1];
        if (subBox[2] > maxLon) maxLon = subBox[2];
        if (subBox[3] > maxLat) maxLat = subBox[3];
      }
    }
  } else if (geom.coordinates !== undefined) {
    visitCoords(geom.coordinates);
  }

  if (
    !Number.isFinite(minLon) ||
    !Number.isFinite(minLat) ||
    !Number.isFinite(maxLon) ||
    !Number.isFinite(maxLat)
  ) {
    return null;
  }

  if (minLon === maxLon && minLat === maxLat) {
    const pad = 0.015;
    return [minLon - pad, minLat - pad, maxLon + pad, maxLat + pad];
  }

  return [minLon, minLat, maxLon, maxLat];
}

function asNonEmptyString(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

/**
 * Construct an immediate `ClosureDetailResponse` from MapLibre feature properties
 * so the Incident & Closure Details drawer opens instantly and works 100% offline.
 */
export function extractClosureSummaryFromProps(
  props: Record<string, unknown>,
  geometry?: unknown
): ClosureDetailResponse | null {
  const rawId = props.closure_id ?? props.id;
  const closureId =
    typeof rawId === "number" && Number.isFinite(rawId)
      ? rawId
      : typeof rawId === "string" && rawId.trim()
        ? rawId.trim()
        : null;
  const title = asNonEmptyString(props.title);
  if (closureId === null && !title) return null;

  const trackId = asNonEmptyString(props.track_id);
  const bbox = computeGeometryBBox(geometry);

  return {
    closure_id: closureId ?? title ?? "alert",
    feed_source: asNonEmptyString(props.feed_source),
    feed_display_name: asNonEmptyString(props.feed_display_name),
    agency: asNonEmptyString(props.agency),
    title: title ?? "Closure / Emergency Alert",
    subtitle: asNonEmptyString(props.subtitle),
    description: asNonEmptyString(props.description),
    closure_type: asNonEmptyString(props.closure_type) ?? "incident",
    severity: asNonEmptyString(props.severity) ?? "warning",
    status: asNonEmptyString(props.status) ?? "active",
    state: asNonEmptyString(props.state),
    effective_from: asNonEmptyString(props.effective_from),
    effective_to: asNonEmptyString(props.effective_to),
    source_url: sanitizeExternalHttpUrl(asNonEmptyString(props.source_url)),
    last_updated_at: asNonEmptyString(props.last_updated_at),
    is_stale: props.is_stale === true || props.is_stale === "true",
    capad_park_name: asNonEmptyString(props.capad_park_name),
    bbox,
    track_id: trackId,
    affected_track_ids: trackId ? [trackId] : [],
  };
}

export function formatClosureSeverityLabel(
  severity: string | null | undefined,
  rawSeverity?: string | null
): string {
  const raw = (rawSeverity ?? "").trim().toLowerCase();
  if (raw === "emergency") return "Emergency Warning";
  if (raw === "watch_and_act") return "Watch & Act";
  if (raw === "closed") return "Closed";
  if (raw === "advisory") return "Advice";

  const sev = (severity ?? "").trim().toLowerCase();
  if (sev === "critical") return "Critical";
  if (sev === "warning") return "Warning / Advisory";
  return "Information";
}

export interface ClosureMetadataRow {
  label: string;
  value: string;
  mono?: boolean;
}

const INCIDENT_DETAIL_LABELS: ReadonlyArray<readonly [string, string]> = [
  ["location", "Location"],
  ["category", "Category"],
  ["alert_level", "Alert Level"],
  ["incident_status", "Incident Status"],
  ["size", "Size / Area"],
  ["resources", "Resources"],
  ["council_area", "Council / Shire"],
  ["responsible_agency", "Lead Agency"],
];

export function buildClosureMetadataRows(
  detail: ClosureDetailResponse,
  nowMs: number = Date.now()
): ClosureMetadataRow[] {
  const rows: ClosureMetadataRow[] = [];
  const push = (label: string, value: string | null | undefined, mono = false) => {
    const clean = value?.trim();
    if (clean) rows.push({ label, value: clean, mono });
  };

  push("Agency", detail.agency);
  if (
    detail.feed_display_name &&
    detail.feed_display_name.toLowerCase() !== (detail.agency ?? "").toLowerCase()
  ) {
    push("Feed", detail.feed_display_name);
  }
  push("Alert Level", formatClosureSeverityLabel(detail.severity, detail.raw_severity));
  if (detail.status) {
    const st = detail.status.replace(/_/g, " ");
    push("Status", st.charAt(0).toUpperCase() + st.slice(1));
  }
  push("Park / Reserve", detail.capad_park_name);

  const meta = detail.incident_details ?? {};
  for (const [key, label] of INCIDENT_DETAIL_LABELS) {
    const val = meta[key];
    if (!val) continue;
    if (key === "alert_level" && val.toLowerCase() === formatClosureSeverityLabel(detail.severity, detail.raw_severity).toLowerCase()) {
      continue;
    }
    if (key === "incident_status" && detail.status && val.toLowerCase() === detail.status.toLowerCase()) {
      continue;
    }
    push(label, val);
  }

  const dateRange = formatClosureDateRange(detail.effective_from, detail.effective_to);
  if (dateRange) {
    push("Effective", dateRange, true);
  }
  if (detail.last_updated_at) {
    push("Last Updated", formatRelativeTimestamp(detail.last_updated_at, nowMs), true);
  }
  if (detail.external_id) {
    push("Reference ID", detail.external_id, true);
  }

  return rows;
}
