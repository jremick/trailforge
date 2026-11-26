import { describe, expect, it } from "vitest";
import {
  OPEN_METEO_ATTRIBUTION,
  formatConfidenceBadge,
  formatDurationMinutes,
  formatHotspotReason,
  formatLastDriven,
  formatWetRiskBanner,
  normalizeVehicleClassSplit,
} from "./gradingHelpers";

describe("Slice 07 live grading, vehicle traffic split & rainfall helpers", () => {
  it("formats confidence badges with outlier-damped messaging for low/single samples", () => {
    const none = formatConfidenceBadge("none", 0);
    expect(none.tone).toBe("none");
    expect(none.label).toContain("No Live");

    const low = formatConfidenceBadge("low", 1);
    expect(low.tone).toBe("low");
    expect(low.detail).toContain("outlier-damped");

    const med = formatConfidenceBadge("medium", 5);
    expect(med.tone).toBe("medium");
    expect(med.label).toBe("Medium Confidence");

    const high = formatConfidenceBadge("high", 12);
    expect(high.tone).toBe("high");
    expect(high.detail).toContain("12");
  });

  it("formats 72h rainfall banners with Open-Meteo CC BY 4.0 attribution and DB saturated mapping", () => {
    const wet = formatWetRiskBanner({
      source: "Open-Meteo",
      rain_24h_mm: 8.4,
      rain_72h_mm: 24.5,
      wet_risk_level: "wet",
      wet_score_adjustment: 6.5,
      warning: "Unsealed clay tracks slippery; unbridged creek crossings elevated.",
      is_stale: false,
    });
    expect(wet).not.toBeNull();
    expect(wet?.tone).toBe("wet");
    expect(wet?.rain72hText).toBe("24.5 mm");
    expect(wet?.rain24hText).toBe("8.4 mm");
    expect(wet?.adjustmentText).toBe("+6.5 pts wet risk");
    expect(wet?.attribution).toBe(OPEN_METEO_ATTRIBUTION);

    const saturated = formatWetRiskBanner({
      rain_72h_mm: 48.2,
      wet_risk_level: "saturated",
      is_stale: true,
    });
    expect(saturated?.level).toBe("heavy_rain");
    expect(saturated?.label).toBe("Heavy Rain Warning");
    expect(saturated?.isStale).toBe(true);
    expect(saturated?.attribution).toContain("Open-Meteo");
  });

  it("normalizes vehicle capability traffic splits in canonical AWD -> Modified 4WD order", () => {
    const split = normalizeVehicleClassSplit({
      "Touring 4WD": { count: 6, pct: 60 },
      "Stock 4WD": { count: 3, pct: 30 },
      "Modified 4WD": { count: 1, pct: 10 },
    });
    expect(split.map((s) => s.key)).toEqual([
      "AWD",
      "Stock 4WD",
      "Touring 4WD",
      "Modified 4WD",
    ]);
    expect(split[0]?.count).toBe(0);
    expect(split[2]?.pct).toBe(60);

    // Also handles raw count maps cleanly
    const rawSplit = normalizeVehicleClassSplit({
      AWD: 1,
      "Stock 4WD": 3,
    });
    expect(rawSplit[0]?.pct).toBe(25);
    expect(rawSplit[1]?.pct).toBe(75);
    expect(normalizeVehicleClassSplit({})).toEqual([]);
  });

  it("formats durations, last-driven timestamps, and obstacle hotspot reasons", () => {
    expect(formatDurationMinutes(42)).toBe("42 min");
    expect(formatDurationMinutes(75)).toBe("1h 15m");
    expect(formatDurationMinutes(null, 1800)).toBe("30 min");
    expect(formatDurationMinutes(null, null)).toBe("—");

    const nowMs = Date.parse("2026-11-26T12:00:00Z");
    expect(formatLastDriven("2026-11-24T12:00:00Z", nowMs)).toBe(
      "2d ago (2026-11-24)"
    );
    expect(formatLastDriven(null)).toBe("Never logged");

    expect(formatHotspotReason("slow_crawl")).toContain("≤8 km/h");
    expect(formatHotspotReason("high_roughness")).toContain("roughness");
    expect(formatHotspotReason("custom_washout")).toBe("Custom Washout");
  });
});
