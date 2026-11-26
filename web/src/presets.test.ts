import { describe, expect, it } from "vitest";
import {
  DEFAULT_PRESET_ID,
  DIFFICULTY_META,
  OUTDOOR_POI_ICONS,
  REGION_PRESETS,
  REQUIRED_ATTRIBUTIONS,
  createInitialLayerState,
  escapeHtml,
  getDefaultPreset,
  getPresetById,
} from "./presets";

describe("VIC/NSW region presets", () => {
  it("includes the four core VIC/NSW regional presets with valid bounding boxes", () => {
    const requiredIds = ["australian-alps", "blue-mountains", "vic", "nsw"];
    for (const id of requiredIds) {
      const preset = getPresetById(id);
      expect(preset, `Missing preset ${id}`).toBeDefined();
      const [w, s, e, n] = preset!.bbox;
      expect(w).toBeLessThan(e);
      expect(s).toBeLessThan(n);

      const [lon, lat] = preset!.center;
      expect(lon).toBeGreaterThanOrEqual(w);
      expect(lon).toBeLessThanOrEqual(e);
      expect(lat).toBeGreaterThanOrEqual(s);
      expect(lat).toBeLessThanOrEqual(n);
    }
  });

  it("defaults to the VIC/NSW Australian Alps corridor", () => {
    const def = getDefaultPreset();
    expect(def.id).toBe(DEFAULT_PRESET_ID);
    expect(def.id).toBe("australian-alps");
    expect(def.utmZones).toContain("55H");
    expect(REGION_PRESETS.length).toBeGreaterThanOrEqual(8);
  });
});

describe("Layer defaults, difficulty legend, icons & required attributions", () => {
  it("defaults snow-line threshold to 1,000 m and enables tracks/steep/contours/POIs", () => {
    const layers = createInitialLayerState();
    expect(layers.tracks).toBe(true);
    expect(layers.steepHighlights).toBe(true);
    expect(layers.contoursHillshade).toBe(true);
    expect(layers.pois).toBe(true);
    expect(layers.snowLine).toBe(false);
    expect(layers.snowLineElevationM).toBe(1000);
  });

  it("defines all four explainable track difficulty grades", () => {
    expect(DIFFICULTY_META.map((d) => d.id)).toEqual([
      "easy",
      "medium",
      "hard",
      "extreme",
    ]);
  });

  it("preserves every required open-data attribution (ODbL & CC BY 4.0)", () => {
    const joined = REQUIRED_ATTRIBUTIONS.map(
      (a) => `${a.label} ${a.license} ${a.fullText}`
    ).join(" | ");

    expect(joined).toContain("OpenStreetMap");
    expect(joined).toContain("ODbL");
    expect(joined).toContain("Geoscience Australia");
    expect(joined).toContain("CAPAD");
    expect(joined).toContain("NSW");
    expect(joined).toContain("DEECA");
    expect(joined).toContain("Open-Meteo");
    expect(joined).toContain("CC BY 4.0");
  });

  it("references adapted outdoor SVG icons under /icons/", () => {
    expect(OUTDOOR_POI_ICONS.length).toBeGreaterThanOrEqual(10);
    for (const icon of OUTDOOR_POI_ICONS) {
      expect(icon.iconPath).toMatch(/^\/icons\/[a-z0-9_]+\.svg$/);
    }
  });

  it("escapes HTML special characters safely", () => {
    expect(escapeHtml(`<script>"a"&'b'</script>`)).toBe(
      "&lt;script&gt;&quot;a&quot;&amp;&#39;b&#39;&lt;/script&gt;"
    );
  });
});
