import { describe, expect, it } from "vitest";
import {
  AUSTRALIA_BBOX,
  DEFAULT_PRESET_ID,
  DIFFICULTY_META,
  ICONIC_REGION_PACK_IDS,
  OUTDOOR_POI_ICONS,
  PAN_AU_PACK_PRESETS,
  REGION_PRESETS,
  REQUIRED_ATTRIBUTIONS,
  STATEWIDE_PACK_IDS,
  classifyPackCategory,
  createInitialLayerState,
  escapeHtml,
  filterPackRegions,
  getDefaultPreset,
  getPresetById,
} from "./presets";

describe("Pan-Australia 8-state & 7-iconic-region presets", () => {
  it("includes all 15 Pan-Australia offline pack presets (7 iconic regions + 8 states) with valid bboxes", () => {
    expect(ICONIC_REGION_PACK_IDS).toHaveLength(7);
    expect(STATEWIDE_PACK_IDS).toHaveLength(8);
    expect(PAN_AU_PACK_PRESETS).toHaveLength(15);

    const allPackIds = [...ICONIC_REGION_PACK_IDS, ...STATEWIDE_PACK_IDS];
    const [auW, auS, auE, auN] = AUSTRALIA_BBOX;

    for (const id of allPackIds) {
      const preset = getPresetById(id);
      expect(preset, `Missing Pan-AU preset ${id}`).toBeDefined();
      const [w, s, e, n] = preset!.bbox;
      expect(w).toBeLessThan(e);
      expect(s).toBeLessThan(n);

      // Center falls strictly inside preset bbox
      const [lon, lat] = preset!.center;
      expect(lon).toBeGreaterThanOrEqual(w);
      expect(lon).toBeLessThanOrEqual(e);
      expect(lat).toBeGreaterThanOrEqual(s);
      expect(lat).toBeLessThanOrEqual(n);

      // Entire preset falls inside Australia bounds
      expect(w).toBeGreaterThanOrEqual(auW - 0.1);
      expect(e).toBeLessThanOrEqual(auE + 0.1);
      expect(s).toBeGreaterThanOrEqual(auS - 0.1);
      expect(n).toBeLessThanOrEqual(auN + 0.1);
      expect(preset!.utmZones.length).toBeGreaterThan(0);
    }

    // Namadgi / Brindabellas ACT landmark is also present
    expect(getPresetById("brindabella-act")?.label).toContain("Namadgi");
  });

  it("classifies and filters packs across All, Iconic 4WD Regions, and Statewide", () => {
    const items = PAN_AU_PACK_PRESETS.map((p) => ({
      region_id: p.id,
      category: p.packCategory,
      region_type: p.packCategory === "state" ? "state" : "tactical",
    }));

    expect(filterPackRegions(items, "all")).toHaveLength(15);
    expect(filterPackRegions(items, "region")).toHaveLength(7);
    expect(filterPackRegions(items, "state")).toHaveLength(8);

    // Fallback classification when API omits category & region_type
    expect(classifyPackCategory({ region_id: "tas" })).toBe("state");
    expect(classifyPackCategory({ region_id: "wa" })).toBe("state");
    expect(classifyPackCategory({ region_id: "flinders-ranges" })).toBe("region");
    expect(classifyPackCategory({ region_id: "red-centre", region_type: "tactical" })).toBe(
      "region"
    );
    expect(classifyPackCategory({ region_id: "custom-state", category: "state" })).toBe(
      "state"
    );
  });

  it("defaults to the VIC/NSW Australian Alps corridor", () => {
    const def = getDefaultPreset();
    expect(def.id).toBe(DEFAULT_PRESET_ID);
    expect(def.id).toBe("australian-alps");
    expect(def.utmZones).toContain("55H");
    expect(REGION_PRESETS.length).toBeGreaterThanOrEqual(21);
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
