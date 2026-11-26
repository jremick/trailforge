import { describe, expect, it } from "vitest";
import {
  CONDITION_REPORT_TYPES,
  buildOsmFallbackLinks,
  formatReportFreshness,
  formatReportTypeLabel,
  isGateWarningType,
  partitionTrackReports,
  resolveReportOsmLinks,
} from "./reportsHelpers";
import type { ConditionReportItem } from "./types";

describe("Slice 08 community condition report helpers", () => {
  it("defines all 8 canonical trail condition report types", () => {
    const keys = CONDITION_REPORT_TYPES.map((t) => t.value);
    expect(keys).toEqual(
      expect.arrayContaining([
        "fallen_tree",
        "bog_hole",
        "washout",
        "deep_water",
        "locked_gate",
        "private_property",
        "snow_ice",
        "track_clear",
      ])
    );
    expect(keys).toHaveLength(8);
    expect(formatReportTypeLabel("locked_gate")).toBe("Locked Gate");
    expect(formatReportTypeLabel("private_property")).toBe("Private Property");
  });

  it("formats freshness decay scores into Fresh, Aging, Stale, and Resolved badges", () => {
    expect(formatReportFreshness(0.92, false, "active")).toEqual({
      label: "Fresh (92%)",
      tone: "fresh",
      pct: 92,
    });
    expect(formatReportFreshness(0.5, false, "active")).toEqual({
      label: "Aging (50%)",
      tone: "aging",
      pct: 50,
    });
    expect(formatReportFreshness(0.28, true, "stale")).toEqual({
      label: "Stale (28%)",
      tone: "stale",
      pct: 28,
    });
    expect(formatReportFreshness(0.9, false, "resolved")).toEqual({
      label: "Resolved",
      tone: "resolved",
      pct: 0,
    });
  });

  it("partitions locked_gate & private_property distinctly from general hazards without duplicates", () => {
    expect(isGateWarningType("locked_gate")).toBe(true);
    expect(isGateWarningType("private_property")).toBe(true);
    expect(isGateWarningType("fallen_tree")).toBe(false);

    const gateReport: ConditionReportItem = {
      id: "r1",
      report_type: "locked_gate",
      notes: "Padlocked steel farm gate across spur",
      lon: 148.118,
      lat: -36.6168,
      confirm_count: 2,
      resolved_count: 0,
      is_gate_warning: true,
    };
    const treeReport: ConditionReportItem = {
      id: "r2",
      report_type: "fallen_tree",
      notes: "Large snow gum blocking track; chainsaw needed",
      lon: 148.122,
      lat: -36.62,
      confirm_count: 1,
      resolved_count: 0,
    };

    const { gateWarnings, timelineReports } = partitionTrackReports(
      [gateReport, treeReport],
      [gateReport]
    );
    expect(gateWarnings).toHaveLength(1);
    expect(gateWarnings[0]?.id).toBe("r1");
    expect(timelineReports).toHaveLength(2);
  });

  it("generates and sanitizes one-click OpenStreetMap note & way edit links", () => {
    const withWay = buildOsmFallbackLinks(-36.6168, 148.118, 987654);
    expect(withWay.osmNoteUrl).toBe(
      "https://www.openstreetmap.org/note/new#map=16/-36.61680/148.11800"
    );
    expect(withWay.osmEditUrl).toBe(
      "https://www.openstreetmap.org/edit?way=987654#map=17/-36.61680/148.11800"
    );

    // Unsafe javascript: URLs must be rejected in favor of safe https OSM links
    const resolved = resolveReportOsmLinks({
      id: "r3",
      report_type: "private_property",
      notes: "Private signposted entry",
      lon: 148.118,
      lat: -36.6168,
      confirm_count: 1,
      resolved_count: 0,
      osm_note_url: "javascript:alert(1)",
      osm_edit_url: "https://www.openstreetmap.org/edit?way=111#map=17/-36.61680/148.11800",
    });
    expect(resolved.osmNoteUrl).toMatch(/^https:\/\/www\.openstreetmap\.org\/note\/new/);
    expect(resolved.osmEditUrl).toContain("way=111");
  });
});
