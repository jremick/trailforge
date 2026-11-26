import { describe, expect, it } from "vitest";
import { createInitialLayerState } from "./presets";
import {
  addRouteTrackId,
  buildPlannedRouteMapGeoJson,
  buildRouteClientGeoJson,
  buildRouteClientGpx,
  buildRouteElevationProfileSvg,
  dedupeRouteTrackIds,
  extractAnalysisBBox,
  formatGridForgeCli,
  formatRouteSafetyBadge,
  moveRouteTrackDown,
  moveRouteTrackUp,
  planRouteOffline,
  removeRouteTrackAt,
  removeRouteTrackId,
  reorderRouteTracks,
  slugifyRouteName,
} from "./routeHelpers";
import { buildTrailForgeStyle } from "./style";
import type {
  PlannedRouteAnalysis,
  RouteSafetySummary,
  TrackInspectorDetail,
} from "./types";

function makeSegment(
  id: string,
  coords: number[][],
  elevMin: number,
  elevMax: number
) {
  return {
    segment_id: id,
    length_m: 1000,
    elev_min_m: elevMin,
    elev_max_m: elevMax,
    elev_gain_m: Math.max(0, elevMax - elevMin),
    elev_loss_m: Math.max(0, elevMin - elevMax),
    max_slope_deg: 14.5,
    sustained_slope_deg: 9.2,
    geometry: {
      type: "LineString",
      coordinates: coords,
    },
  };
}

const DAVIES_LOWER: TrackInspectorDetail = {
  track_id: "trk_davies_lower",
  name: "Davies Plain Track Lower",
  state: "VIC",
  grade: "Hard",
  effective_grade: "Hard",
  score: 64,
  total_length_m: 4200,
  min_elev_m: 610,
  max_elev_m: 1080,
  elev_gain_m: 470,
  elev_loss_m: 35,
  max_slope_deg: 16.8,
  sustained_slope_deg: 11.2,
  waterway_crossing_count: 1,
  closure_status: "open",
  bbox: [148.11, -36.6, 148.13, -36.56],
  rainfall_context: {
    wet_risk_level: "damp",
    rain_24h_mm: 4.0,
    rain_72h_mm: 9.5,
  },
  waterway_crossings: [
    {
      name: "Murray River",
      waterway_type: "river",
      perennial: true,
      bridged: false,
      dist_m: 500,
    },
  ],
  nearby_pois: [
    {
      id: 101,
      name: "Tom Groggin Campground",
      category: "campground",
      elev_m: 612,
      lon: 148.1195,
      lat: -36.5602,
    },
  ],
  capad_parks: [
    {
      id: 1,
      name: "Alpine National Park",
      park_type: "National Park",
      state: "VIC",
    },
  ],
  elevation_profile: [
    { dist_m: 0, elev_m: 610, slope_deg: 4.0, grade: "Easy" },
    { dist_m: 2100, elev_m: 820, slope_deg: 10.5, grade: "Medium" },
    { dist_m: 4200, elev_m: 1080, slope_deg: 16.8, grade: "Hard" },
  ],
  segments: [
    makeSegment(
      "seg_low_1",
      [
        [148.12, -36.56, 610],
        [148.125, -36.58, 840],
        [148.13, -36.6, 1080],
      ],
      610,
      1080
    ),
  ],
};

// Coordinates intentionally stored in reverse direction (south -> north ending at 148.13,-36.60)
// so offline planner must auto-orient it to connect cleanly to DAVIES_LOWER's south end!
const DAVIES_RIDGE_REVERSED: TrackInspectorDetail = {
  track_id: "trk_davies_ridge",
  name: "Davies Plain Ridge",
  state: "VIC",
  grade: "Extreme",
  effective_grade: "Extreme",
  score: 82,
  total_length_m: 5800,
  min_elev_m: 1080,
  max_elev_m: 1630,
  elev_gain_m: 560,
  elev_loss_m: 40,
  max_slope_deg: 21.4,
  sustained_slope_deg: 14.1,
  waterway_crossing_count: 1,
  closure_status: "seasonal",
  active_closures: [
    {
      id: 501,
      title: "Victorian High Country Seasonal Track Closure",
      closure_type: "seasonal",
      severity: "warning",
      agency: "Parks Victoria",
    },
  ],
  community_gate_warnings: [
    {
      id: "rep-gate-1",
      report_type: "locked_gate",
      title: "Padlocked Forestry Gate",
      notes: "Gate locked near ridge saddle",
      lon: 148.135,
      lat: -36.63,
      confirm_count: 3,
      resolved_count: 0,
      is_gate_warning: true,
    },
  ],
  rainfall_context: {
    wet_risk_level: "wet",
    rain_24h_mm: 18.2,
    rain_72h_mm: 34.0,
  },
  waterway_crossings: [
    {
      name: "Davies Creek",
      waterway_type: "stream",
      perennial: false,
      bridged: false,
      dist_m: 1200,
    },
  ],
  nearby_pois: [
    {
      id: 102,
      name: "Davies Plain Hut",
      category: "hut",
      elev_m: 1615,
      lon: 148.14,
      lat: -36.65,
    },
  ],
  capad_parks: [
    {
      id: 1,
      name: "Alpine National Park",
      park_type: "National Park",
      state: "VIC",
    },
  ],
  elevation_profile: [
    { dist_m: 0, elev_m: 1630, slope_deg: 12.0, grade: "Hard" },
    { dist_m: 2900, elev_m: 1360, slope_deg: 21.4, grade: "Extreme" },
    { dist_m: 5800, elev_m: 1080, slope_deg: 15.0, grade: "Hard" },
  ],
  segments: [
    makeSegment(
      "seg_ridge_1",
      [
        [148.14, -36.65, 1630],
        [148.135, -36.625, 1360],
        [148.1302, -36.6002, 1080],
      ],
      1080,
      1630
    ),
  ],
};

const DISTANT_TRACK: TrackInspectorDetail = {
  track_id: "trk_blue_mtns_far",
  name: "Narrow Neck Fire Trail",
  state: "NSW",
  grade: "Medium",
  effective_grade: "Medium",
  score: 41,
  total_length_m: 3100,
  min_elev_m: 890,
  max_elev_m: 1010,
  elev_gain_m: 140,
  elev_loss_m: 80,
  max_slope_deg: 10.5,
  waterway_crossing_count: 0,
  closure_status: "open",
  elevation_profile: [
    { dist_m: 0, elev_m: 890, slope_deg: 5.0, grade: "Easy" },
    { dist_m: 3100, elev_m: 1010, slope_deg: 10.5, grade: "Medium" },
  ],
  segments: [
    makeSegment(
      "seg_far_1",
      [
        [150.28, -33.74, 890],
        [150.26, -33.78, 1010],
      ],
      890,
      1010
    ),
  ],
};

describe("Route Planner leg management helpers", () => {
  it("adds, dedupes, enforces max leg limit, reorders, moves up/down, and removes tracks", () => {
    let legs: string[] = [];

    const first = addRouteTrackId(legs, "trk_a");
    expect(first.added).toBe(true);
    legs = first.trackIds;

    const dup = addRouteTrackId(legs, "trk_a");
    expect(dup.added).toBe(false);
    expect(dup.reason).toBe("duplicate");

    legs = addRouteTrackId(legs, "trk_b").trackIds;
    legs = addRouteTrackId(legs, "trk_c").trackIds;
    expect(legs).toEqual(["trk_a", "trk_b", "trk_c"]);

    // Move up / down boundaries & swaps
    expect(moveRouteTrackUp(legs, 0)).toEqual(["trk_a", "trk_b", "trk_c"]);
    expect(moveRouteTrackUp(legs, 2)).toEqual(["trk_a", "trk_c", "trk_b"]);
    expect(moveRouteTrackDown(legs, 0)).toEqual(["trk_b", "trk_a", "trk_c"]);
    expect(moveRouteTrackDown(legs, 2)).toEqual(["trk_a", "trk_b", "trk_c"]);

    // General reorder & remove
    expect(reorderRouteTracks(legs, 2, 0)).toEqual(["trk_c", "trk_a", "trk_b"]);
    expect(removeRouteTrackAt(legs, 1)).toEqual(["trk_a", "trk_c"]);
    expect(removeRouteTrackId(legs, "trk_a")).toEqual(["trk_b", "trk_c"]);

    // Dedupe preserves first occurrence & strips blank values
    expect(
      dedupeRouteTrackIds([" trk_a ", "", "trk_b", "trk_a", "trk_c", "trk_b"])
    ).toEqual(["trk_a", "trk_b", "trk_c"]);

    // Max legs guard
    const full = Array.from({ length: 30 }, (_, i) => `trk_${i}`);
    const over = addRouteTrackId(full, "trk_overflow");
    expect(over.added).toBe(false);
    expect(over.reason).toBe("limit");
  });
});

describe("Route safety status priority (closure > gate > wet > clear)", () => {
  const baseSummary: RouteSafetySummary = {
    has_blocking_closure: false,
    closure_count: 0,
    gate_warning_count: 0,
    active_report_count: 0,
    unbridged_crossings: 0,
    wet_risk_level: "dry",
    warnings: [],
  };

  it("prioritizes Blocked by Closure over gate and wet warnings", () => {
    const badge = formatRouteSafetyBadge({
      ...baseSummary,
      has_blocking_closure: true,
      closure_count: 1,
      gate_warning_count: 2,
      wet_risk_level: "heavy_rain",
    });
    expect(badge.kind).toBe("closure");
    expect(badge.label).toBe("Blocked by Closure");
    expect(badge.tone).toBe("critical");
  });

  it("prioritizes Gate Warning over Wet Caution when no closures exist", () => {
    const badge = formatRouteSafetyBadge({
      ...baseSummary,
      gate_warning_count: 1,
      wet_risk_level: "heavy_rain",
    });
    expect(badge.kind).toBe("gate");
    expect(badge.label).toBe("Gate Warning");
    expect(badge.tone).toBe("warning");
  });

  it("shows Wet Caution when wet or heavy_rain with no closures or gates", () => {
    const wetBadge = formatRouteSafetyBadge({
      ...baseSummary,
      wet_risk_level: "wet",
    });
    expect(wetBadge.kind).toBe("wet");
    expect(wetBadge.label).toBe("Wet Caution");
    expect(wetBadge.tone).toBe("warning");

    const heavyBadge = formatRouteSafetyBadge({
      ...baseSummary,
      wet_risk_level: "heavy_rain",
    });
    expect(heavyBadge.kind).toBe("wet");
    expect(heavyBadge.label).toBe("Wet Caution");
  });

  it("returns Clear when dry/damp with zero closures or gate warnings", () => {
    const badge = formatRouteSafetyBadge({
      ...baseSummary,
      wet_risk_level: "damp",
      unbridged_crossings: 2,
    });
    expect(badge.kind).toBe("clear");
    expect(badge.label).toBe("Clear");
    expect(badge.tone).toBe("clear");
    expect(badge.detail).toContain("2 unbridged crossings");
  });
});

describe("Combined Route Elevation Profile SVG builder", () => {
  it("renders all 4 grade colors, snow-line, crossing ticks, leg dividers, and unique gradient id", () => {
    const res = buildRouteElevationProfileSvg(
      [
        { dist_m: 0, elev_m: 610, slope_deg: 3.0, grade: "Easy", track_id: "t1" },
        { dist_m: 800, elev_m: 660, slope_deg: 3.6, grade: "Easy", track_id: "t1" },
        { dist_m: 1500, elev_m: 780, slope_deg: 8.5, grade: "Medium", track_id: "t1" },
        { dist_m: 3000, elev_m: 1050, slope_deg: 15.2, grade: "Hard", track_id: "t2" },
        { dist_m: 5000, elev_m: 1580, slope_deg: 21.6, grade: "Extreme", track_id: "t2" },
      ],
      {
        snowLineElevationM: 1200,
        crossings: [
          {
            name: "Murray River",
            waterway_type: "river",
            perennial: true,
            bridged: false,
            dist_m: 600,
          },
        ],
        legs: [
          {
            index: 0,
            track_id: "t1",
            name: "Lower Track",
            length_m: 2000,
            grade: "Medium",
            gate_warning_count: 0,
          },
          {
            index: 1,
            track_id: "t2",
            name: "Ridge Track",
            length_m: 3000,
            grade: "Extreme",
            gate_warning_count: 0,
          },
        ],
      }
    );

    expect(res.hasSnowLineIntersection).toBe(true);
    expect(res.svgHtml).toContain("tfRouteElevGrad");
    expect(res.svgHtml).not.toContain('id="tfElevGrad"');
    expect(res.svgHtml).toContain('stroke="#22C55E"'); // Easy
    expect(res.svgHtml).toContain('stroke="#38BDF8"'); // Medium
    expect(res.svgHtml).toContain('stroke="#F59E0B"'); // Hard
    expect(res.svgHtml).toContain('stroke="#EF4444"'); // Extreme
    expect(res.svgHtml).toContain("Snow-line 1200m");
    expect(res.svgHtml).toContain("Murray River");
    expect(res.svgHtml).toContain("tf-route-leg-divider");
    expect(res.svgHtml).toContain(">L2<");
  });
});

describe("GridForge Strip-Route CLI formatting", () => {
  it("formats ready-to-run GridForge docker command with sanitized slug and uppercase title", () => {
    expect(slugifyRouteName("Davies Plain & Tom Groggin Loop!")).toBe(
      "davies_plain_tom_groggin_loop"
    );

    const cli = formatGridForgeCli('Davies Plain "Alpine" Loop');
    expect(cli).toContain('docker run --rm -v "$(pwd)/out:/out" gridforge:dev gridforge render');
    expect(cli).toContain("--strip-route /out/davies_plain_alpine_loop.gpx");
    expect(cli).toContain("--scale 25000 --paper a4 --dem-source au-elvis");
    expect(cli).toContain("--au-overlay au-state-trails --elevation-profile --route-card --calibration-page");
    expect(cli).toContain('--title "DAVIES PLAIN ALPINE LOOP"');
    expect(cli).toContain("--output /out/davies_plain_alpine_loop_strip_atlas.pdf");
  });
});

describe("Offline Multi-Track Route Planner, Map GeoJSON & GPX/GeoJSON Export", () => {
  it("chains tracks locally, auto-orients reversed legs, aggregates safety/suitability, and exports GPX 1.1 & GeoJSON", () => {
    const analysis = planRouteOffline(
      [DAVIES_LOWER, DAVIES_RIDGE_REVERSED],
      {
        name: "Davies Plain Full Traverse",
        track_ids: ["trk_davies_lower", "trk_davies_ridge"],
        vehicle_class: "Stock 4WD",
        waypoints: [
          {
            name: "Ridge Lookout",
            category: "waypoint",
            lon: 148.136,
            lat: -36.632,
          },
        ],
      }
    );

    expect(analysis.offline_computed).toBe(true);
    expect(analysis.total_length_m).toBe(10000);
    expect(analysis.worst_grade).toBe("Extreme");
    expect(analysis.worst_score).toBe(82);
    expect(analysis.legs).toHaveLength(2);
    // Second leg was stored backwards and must be auto-reversed to keep gap < 100 m
    expect(analysis.legs[1]?.reversed).toBe(true);
    expect(analysis.legs[1]?.gap_from_prev_m ?? 999).toBeLessThan(100);
    expect(analysis.leg_gaps).toHaveLength(0);

    // Safety aggregation across both tracks
    expect(analysis.safety_summary.has_blocking_closure).toBe(true);
    expect(analysis.safety_summary.closure_count).toBe(1);
    expect(analysis.safety_summary.gate_warning_count).toBe(1);
    expect(analysis.safety_summary.wet_risk_level).toBe("wet");
    expect(analysis.safety_summary.unbridged_crossings).toBe(2);
    expect(analysis.capad_parks).toHaveLength(1); // deduplicated

    // Rig suitability reacts to vehicle_class
    expect(analysis.suitability?.rating).toBe("High Risk for your rig");

    const mod4wdAnalysis = planRouteOffline(
      [DAVIES_LOWER, DAVIES_RIDGE_REVERSED],
      {
        track_ids: ["trk_davies_lower", "trk_davies_ridge"],
        vehicle_class: "Modified 4WD",
      }
    );
    expect(mod4wdAnalysis.suitability?.rating).toBe("Suited");

    // Cumulative waterway crossings
    expect(analysis.waterway_crossings).toHaveLength(2);
    expect(analysis.waterway_crossings?.[0]?.dist_m).toBe(500);
    expect(analysis.waterway_crossings?.[1]?.dist_m).toBe(4200 + (5800 - 1200));

    // Client-side GPX 1.1 export (per-leg trkseg + waypoints + GPL-3.0 creator)
    const gpx = buildRouteClientGpx(analysis);
    expect(gpx).toContain('<gpx version="1.1" creator="TrailForge (GPL-3.0-or-later)"');
    expect(gpx).toContain("<name>Davies Plain Full Traverse</name>");
    expect((gpx.match(/<trkseg>/g) ?? []).length).toBe(2);
    expect(gpx).toContain("Ridge Lookout");
    expect(gpx).toContain("Tom Groggin Campground");
    expect(gpx).toContain("Padlocked Forestry Gate");

    // Client-side RFC 7946 GeoJSON export
    const geojson = buildRouteClientGeoJson(analysis);
    expect(geojson.type).toBe("FeatureCollection");
    const lineFeats = geojson.features.filter((f) => f.geometry.type === "LineString");
    const pointFeats = geojson.features.filter((f) => f.geometry.type === "Point");
    expect(lineFeats).toHaveLength(2);
    expect(pointFeats.length).toBeGreaterThanOrEqual(3); // 1 custom wpt + 2 POIs

    // MapLibre overlay normalizer emits start, leg-2, finish, and custom waypoint pins
    const { lineFc, pinsFc } = buildPlannedRouteMapGeoJson(analysis);
    expect(lineFc.features).toHaveLength(2);
    const pinKinds = pinsFc.features.map((f) => f.properties?.kind);
    expect(pinKinds).toEqual(["start", "leg", "finish", "waypoint"]);
    expect(extractAnalysisBBox(analysis)).not.toBeNull();
  });

  it("flags non-contiguous leg gaps > 100 m when chaining distant tracks", () => {
    const analysis = planRouteOffline([DAVIES_LOWER, DISTANT_TRACK], {
      track_ids: ["trk_davies_lower", "trk_blue_mtns_far"],
    });
    expect(analysis.leg_gaps).toHaveLength(1);
    expect(analysis.leg_gaps?.[0]?.gap_m ?? 0).toBeGreaterThan(100_000);
    expect(analysis.legs[1]?.gap_from_prev_m ?? 0).toBeGreaterThan(100_000);
  });

  it("normalizes MultiLineString and LineString backend geometries cleanly and mounts MapLibre layers", () => {
    const mlsAnalysis: PlannedRouteAnalysis = {
      name: "Test MultiLine",
      track_ids: ["t1", "t2"],
      total_length_m: 3000,
      est_duration_min: 15,
      elev_gain_m: 200,
      elev_loss_m: 100,
      worst_grade: "Medium",
      safety_summary: {
        has_blocking_closure: false,
        closure_count: 0,
        gate_warning_count: 0,
        active_report_count: 0,
        unbridged_crossings: 0,
        wet_risk_level: "dry",
        warnings: [],
      },
      legs: [
        {
          index: 0,
          track_id: "t1",
          name: "Leg One",
          length_m: 1500,
          grade: "Easy",
          gate_warning_count: 0,
        },
        {
          index: 1,
          track_id: "t2",
          name: "Leg Two",
          length_m: 1500,
          grade: "Medium",
          gate_warning_count: 0,
        },
      ],
      geometry: {
        type: "MultiLineString",
        coordinates: [
          [
            [148.1, -36.5],
            [148.11, -36.51],
          ],
          [
            [148.11, -36.51],
            [148.12, -36.52],
          ],
        ],
      },
      gridforge_cli: formatGridForgeCli("Test MultiLine"),
    };

    const { lineFc, pinsFc } = buildPlannedRouteMapGeoJson(mlsAnalysis);
    expect(lineFc.features).toHaveLength(2);
    expect(pinsFc.features.map((f) => f.properties?.label)).toEqual([
      "S",
      "2",
      "F",
    ]);

    const style = buildTrailForgeStyle({
      initialBasemap: "offline-topo",
      layers: createInitialLayerState(),
    });
    const byId = new Map(style.layers.map((l) => [l.id, l]));
    expect(style.sources["tf-planned-route"]).toBeDefined();
    expect(style.sources["tf-planned-route-pins"]).toBeDefined();
    expect(byId.get("tf-planned-route-casing")).toBeDefined();
    expect(byId.get("tf-planned-route-line")).toMatchObject({
      source: "tf-planned-route",
      paint: {
        "line-color": "#14b8a6",
      },
    });
  });
});
