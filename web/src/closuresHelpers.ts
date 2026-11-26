import type {
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
