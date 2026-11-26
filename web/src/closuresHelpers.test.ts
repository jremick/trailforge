import { describe, expect, it } from "vitest";
import {
  formatClosureDateRange,
  formatClosureTypeLabel,
  formatRelativeTimestamp,
  getClosureStatusBadge,
  getClosureTone,
  summarizeFeedStatus,
} from "./closuresHelpers";
import type { ClosuresStatusResponse } from "./types";

describe("Slice 04 closure labels, tones, and formatting", () => {
  it("formats canonical closure types cleanly", () => {
    expect(formatClosureTypeLabel("closed")).toBe("Closed");
    expect(formatClosureTypeLabel("seasonal")).toBe("Seasonal Closure");
    expect(formatClosureTypeLabel("park_alert")).toBe("Park Alert");
    expect(formatClosureTypeLabel("locked_gate")).toBe("Locked Gate");
    expect(formatClosureTypeLabel("private_access")).toBe("Private Access");
    expect(formatClosureTypeLabel("incident")).toBe("Emergency Incident");
  });

  it("maps closure types & severities to critical/warning/info tones", () => {
    expect(getClosureTone("closed", "critical")).toBe("critical");
    expect(getClosureTone("incident", "warning")).toBe("critical");
    expect(getClosureTone("seasonal", "warning")).toBe("warning");
    expect(getClosureTone("locked_gate", "warning")).toBe("warning");
    expect(getClosureTone("park_alert", "info")).toBe("info");
  });

  it("maps track closure_status badges accurately", () => {
    expect(getClosureStatusBadge("closed")).toEqual({
      label: "CLOSED",
      tone: "critical",
    });
    expect(getClosureStatusBadge("seasonal")).toEqual({
      label: "SEASONAL",
      tone: "warning",
    });
    expect(getClosureStatusBadge("gated")).toEqual({
      label: "GATED / PRIVATE",
      tone: "warning",
    });
    expect(getClosureStatusBadge("open")).toEqual({
      label: "OPEN",
      tone: "open",
    });
  });

  it("formats UTC closure date spans and relative timestamps", () => {
    const range = formatClosureDateRange(
      "2026-06-08T00:00:00Z",
      "2026-10-31T23:59:59Z"
    );
    expect(range).toContain("Jun");
    expect(range).toContain("Oct");
    expect(range).toContain("2026");

    const now = Date.parse("2026-11-26T12:00:00Z");
    expect(formatRelativeTimestamp("2026-11-26T11:45:00Z", now)).toBe("15m ago");
    expect(formatRelativeTimestamp("2026-11-26T08:00:00Z", now)).toBe("4h ago");
    expect(formatRelativeTimestamp(null, now)).toBe("Never");
  });
});

describe("summarizeFeedStatus (outage & staleness visibility)", () => {
  it("reports healthy live count when all feeds are fresh", () => {
    const status: ClosuresStatusResponse = {
      overall_status: "healthy",
      feeds: [
        {
          feed_source: "vic_mapshare_seasonal",
          display_name: "VIC MapShare Seasonal",
          state: "VIC",
          status: "ok",
          record_count: 1,
          is_stale: false,
        },
        {
          feed_source: "nsw_npws",
          display_name: "NSW NPWS Alerts",
          state: "NSW",
          status: "ok",
          record_count: 2,
          is_stale: false,
        },
      ],
    };

    const summary = summarizeFeedStatus(status);
    expect(summary.kind).toBe("healthy");
    expect(summary.label).toBe("Feeds Live (3)");
    expect(summary.staleFeeds).toHaveLength(0);
  });

  it("surfaces degraded agency outage with affected feed names", () => {
    const status: ClosuresStatusResponse = {
      overall_status: "degraded",
      feeds: [
        {
          feed_source: "vic_emergency",
          display_name: "VicEmergency",
          state: "VIC",
          status: "degraded",
          record_count: 1,
          consecutive_failures: 2,
          is_stale: true,
          last_error: "503 Service Unavailable",
        },
      ],
    };

    const summary = summarizeFeedStatus(status);
    expect(summary.kind).toBe("degraded");
    expect(summary.label).toBe("Feeds Degraded");
    expect(summary.degradedFeeds).toHaveLength(1);
    expect(summary.tooltip).toContain("VicEmergency");
  });

  it("handles stale and offline feed states gracefully", () => {
    const stale = summarizeFeedStatus({
      overall_status: "stale",
      feeds: [
        {
          feed_source: "nsw_rfs",
          display_name: "NSW RFS",
          status: "ok",
          is_stale: true,
          record_count: 0,
        },
      ],
    });
    expect(stale.kind).toBe("stale");
    expect(stale.label).toBe("Feeds Stale");
    expect(stale.tooltip).toContain("NSW RFS");

    const offline = summarizeFeedStatus(null);
    expect(offline.kind).toBe("offline");
    expect(offline.label).toBe("Feeds Offline");
  });
});
