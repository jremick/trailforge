import { describe, expect, it } from "vitest";
import {
  buildElevationProfileSvg,
  buildTrackCourseSvg,
  computeTrackGradeDistribution,
  findNearestPointOnTrackCourse,
  formatBreakdownFactors,
  formatOverrideLabel,
  normalizeTrackInspector,
  sampleElevationAtDistance,
  sampleTrackCourseAtDistance,
} from "./inspectorHelpers";
import type { BaselineBreakdown, TrackInspectorDetail } from "./types";

const SAMPLE_BREAKDOWN: BaselineBreakdown = {
  formula_version: "baseline_v1",
  raw_score: 70.0,
  final_score: 75.0,
  grade: "Extreme",
  factors: {
    surface_tracktype: {
      points: 26,
      max_points: 35,
      surface_class: "rough_rock_mud_sand",
      tracktype: "grade4",
    },
    max_slope: {
      points: 30,
      max_points: 30,
      max_slope_deg: 21.98,
    },
    sustained_relief: {
      points: 12,
      max_points: 15,
      sustained_slope_deg: 14.5,
      elev_gain_m: 19.4,
      elev_loss_m: 2.1,
    },
    waterway_crossings: {
      points: 10,
      max_points: 12,
      unbridged_count: 1,
      perennial: true,
      is_bridged: false,
      waterways: ["Murray River"],
    },
    four_wd_access: {
      points: 8,
      max_points: 8,
      is_4wd_only: true,
      trail_class: "4wd_track",
      access_status: "public",
      has_gate_barrier: false,
    },
  },
  overrides: ["steep_rough_or_perennial_extreme_floor"],
  reasons: [
    "Surface rough rock mud sand (grade4) [26/35 pts]",
    "Max slope 22.0° [30/30 pts]",
    "1 perennial crossing (Murray River) [10/12 pts]",
  ],
  dem_tier: "ga_5m_lidar",
};

describe("Track Inspector explainable helpers", () => {
  it("formats all 5 baseline breakdown factor bars accurately", () => {
    const factors = formatBreakdownFactors(SAMPLE_BREAKDOWN);
    expect(factors).toHaveLength(5);
    expect(factors[0]).toMatchObject({
      key: "surface_tracktype",
      points: 26,
      maxPoints: 35,
    });
    expect(factors[1]).toMatchObject({
      key: "max_slope",
      points: 30,
      maxPoints: 30,
      pct: 100,
    });
    expect(factors[3]?.detail).toContain("perennial");
    expect(formatOverrideLabel("steep_rough_or_perennial_extreme_floor")).toContain(
      "Extreme"
    );
  });

  it("normalizes enriched TrackInspectorDetail payload cleanly", () => {
    const detail: TrackInspectorDetail = {
      track_id: "trk_davies_plain",
      name: "Davies Plain Track",
      alt_names: ["Tom Groggin Trail"],
      state: "VIC/NSW",
      grade: "Extreme",
      score: 75.0,
      total_length_m: 12450,
      min_elev_m: 605,
      max_elev_m: 1632,
      elev_gain_m: 1085,
      elev_loss_m: 62,
      max_slope_deg: 21.98,
      sustained_slope_deg: 14.5,
      waterway_crossing_count: 2,
      is_4wd_only: true,
      has_gate_barrier: false,
      access_status: "public",
      bbox: [148.08, -36.67, 148.15, -36.56],
      baseline_breakdown: SAMPLE_BREAKDOWN,
      elevation_profile: [
        { dist_m: 0, elev_m: 605, slope_deg: 4.0, grade: "Easy" },
        { dist_m: 4000, elev_m: 1050, slope_deg: 16.2, grade: "Hard" },
        { dist_m: 12450, elev_m: 1632, slope_deg: 21.98, grade: "Extreme" },
      ],
      waterway_crossings: [
        {
          name: "Murray River",
          waterway_type: "river",
          perennial: true,
          bridged: false,
          dist_m: 620,
        },
      ],
      capad_parks: [
        {
          name: "Alpine National Park",
          park_type: "National Park",
          type_abbr: "NP",
          state: "VIC",
          iucn: "II",
        },
      ],
    };

    const norm = normalizeTrackInspector(detail);
    expect(norm.grade).toBe("Extreme");
    expect(norm.score).toBe(75);
    expect(norm.totalLengthKm).toBeCloseTo(12.45, 2);
    expect(norm.minElevM).toBe(605);
    expect(norm.maxElevM).toBe(1632);
    expect(norm.parks).toHaveLength(1);
    expect(norm.profile).toHaveLength(3);
  });

  it("renders SVG elevation profile with all 4 grade colors, snow-line, crossings, and non-occluding steep zones", () => {
    const denseExtreme = Array.from({ length: 120 }, (_, i) => ({
      dist_m: 3400 + i * 10,
      elev_m: 900 + i * 3.5,
      slope_deg: i === 60 ? 21.9 : 18.6,
      grade: "Extreme",
    }));

    const res = buildElevationProfileSvg(
      [
        { dist_m: 0, elev_m: 610, slope_deg: 3.5, grade: "Easy" },
        { dist_m: 1000, elev_m: 680, slope_deg: 4.0, grade: "Easy" },
        { dist_m: 2000, elev_m: 780, slope_deg: 10.5, grade: "Medium" },
        { dist_m: 3000, elev_m: 860, slope_deg: 12.2, grade: "Hard" },
        ...denseExtreme,
      ],
      {
        snowLineElevationM: 1000,
        crossings: [
          {
            name: "Murray River",
            waterway_type: "river",
            perennial: true,
            bridged: false,
            dist_m: 450,
          },
        ],
      }
    );

    expect(res.hasSnowLineIntersection).toBe(true);
    expect(res.steepPointCount).toBe(120);
    expect(res.steepZoneCount).toBe(1);
    expect(res.svgHtml).toContain("<svg");
    expect(res.svgHtml).toContain("Snow-line 1000m");
    expect(res.svgHtml).toContain("Murray River");

    // All 4 canonical difficulty grade colors must render on elevation curve polylines
    expect(res.svgHtml).toContain('stroke="#22C55E"'); // Easy
    expect(res.svgHtml).toContain('stroke="#38BDF8"'); // Medium
    expect(res.svgHtml).toContain('stroke="#F59E0B"'); // Hard
    expect(res.svgHtml).toContain('stroke="#EF4444"'); // Extreme

    // Dense 120-pt steep run must emit only 1 peak marker + 1 crossing circle (never 120 overlapping dark circles)
    const circleMatches = res.svgHtml.match(/<circle /g) ?? [];
    expect(circleMatches.length).toBe(2);
    expect(res.svgHtml).toContain('fill="#EF4444" stroke="#F8FAFC"');
  });

  it("omits snow-line reference cleanly when threshold lies above track peak", () => {
    const res = buildElevationProfileSvg(
      [
        { dist_m: 0, elev_m: 610, slope_deg: 2.0, grade: "Easy" },
        { dist_m: 2000, elev_m: 780, slope_deg: 5.0, grade: "Easy" },
      ],
      { snowLineElevationM: 1400 }
    );
    expect(res.hasSnowLineIntersection).toBe(false);
    expect(res.svgHtml).not.toContain("Snow-line 1400m");
  });

  it("reconciles multi-tile track stats from segments/profile and builds interactive modal telemetry", () => {
    const detail: TrackInspectorDetail = {
      track_id: "tftrk_bgb",
      name: "Billy Goat Bluff Track",
      state: "VIC",
      grade: "Extreme",
      score: 98,
      // Stale flat-end single-tile header stats
      total_length_m: 7270,
      min_elev_m: 275,
      max_elev_m: 283,
      elev_gain_m: 1,
      elev_loss_m: 1044,
      max_slope_deg: 6.0,
      sustained_slope_deg: 21.9,
      waterway_crossing_count: 0,
      segments: [
        {
          segment_id: "s1",
          length_m: 9000,
          elev_min_m: 800,
          elev_max_m: 1450,
          elev_gain_m: 120,
          elev_loss_m: 650,
          max_slope_deg: 24.0,
          sustained_slope_deg: 21.9,
          grade: "Extreme",
        },
        {
          segment_id: "s2",
          length_m: 8800,
          elev_min_m: 230,
          elev_max_m: 800,
          elev_gain_m: 40,
          elev_loss_m: 570,
          max_slope_deg: 16.5,
          sustained_slope_deg: 11.2,
          grade: "Medium",
        },
      ],
      elevation_profile: [
        { dist_m: 0, elev_m: 1450, slope_deg: 24.0, grade: "Extreme", segment_id: "s1" },
        { dist_m: 9000, elev_m: 800, slope_deg: 16.5, grade: "Extreme", segment_id: "s1" },
        { dist_m: 17800, elev_m: 230, slope_deg: 6.0, grade: "Medium", segment_id: "s2" },
      ],
      waterway_crossings: [
        { name: "Wonnangatta River", waterway_type: "river", perennial: true, bridged: false, dist_m: 17400 },
      ],
    };

    const norm = normalizeTrackInspector(detail);
    expect(norm.totalLengthKm).toBeCloseTo(17.8, 1);
    expect(norm.minElevM).toBe(230);
    expect(norm.maxElevM).toBe(1450);
    expect(norm.maxSlopeDeg).toBe(24.0);
    expect(norm.waterwayCrossingCount).toBe(1);

    const modalChart = buildElevationProfileSvg(norm.profile, {
      detailed: true,
      snowLineElevationM: 1000,
      crossings: norm.crossings,
    });
    expect(modalChart.geometry).not.toBeNull();
    expect(modalChart.steepZones.length).toBeGreaterThanOrEqual(1);
    expect(modalChart.svgHtml).toContain('data-role="elev-scrub-hit"');
    expect(modalChart.svgHtml).toContain("tfElevGradModal");

    const mid = sampleElevationAtDistance(norm.profile, 4500);
    expect(mid).not.toBeNull();
    expect(mid?.elevM).toBeCloseTo(1125, 0);
    expect(mid?.grade).toBe("Extreme");
    expect(mid?.slopePct).toBeGreaterThan(25);

    const dist = computeTrackGradeDistribution(detail.segments ?? [], norm.profile);
    expect(dist.map((d) => d.grade)).toEqual(["Medium", "Extreme"]);
    expect(dist.reduce((acc, d) => acc + d.pct, 0)).toBeCloseTo(100, 0);
  });

  it("builds a North-up synchronized Track Course SVG with accurate bidirectional distance lookup", () => {
    const segments = [
      {
        segment_id: "seg_a",
        length_m: 1000,
        grade: "Medium",
        geometry: {
          type: "LineString",
          coordinates: [
            [147.08, -37.24],
            [147.09, -37.23],
          ],
        },
      },
      {
        segment_id: "seg_b",
        length_m: 1000,
        grade: "Extreme",
        geometry: {
          type: "LineString",
          coordinates: [
            [147.09, -37.23],
            [147.10, -37.22],
          ],
        },
      },
    ];
    const profile = [
      { dist_m: 0, elev_m: 1400, slope_deg: 9.0, grade: "Medium", segment_id: "seg_a" },
      { dist_m: 1000, elev_m: 1050, slope_deg: 18.5, grade: "Medium", segment_id: "seg_a" },
      { dist_m: 2000, elev_m: 620, slope_deg: 22.0, grade: "Extreme", segment_id: "seg_b" },
    ];

    const course = buildTrackCourseSvg(segments, profile, {
      width: 460,
      height: 340,
      crossings: [
        {
          name: "Wonnangatta River",
          waterway_type: "river",
          perennial: true,
          bridged: false,
          dist_m: 1850,
        },
      ],
    });

    expect(course.vertices.length).toBe(4);
    expect(course.svgHtml).toContain('data-role="course-cursor"');
    expect(course.svgHtml).toContain('data-role="course-scrub-hit"');
    expect(course.svgHtml).toContain("Wonnangatta River");
    expect(course.scaleBarLabel).toMatch(/(m|km)$/);

    // Forward lookup: elevation distM -> course SVG coordinate
    const midPt = sampleTrackCourseAtDistance(course.vertices, 1500);
    expect(midPt).not.toBeNull();
    expect(midPt!.lat).toBeCloseTo(-37.225, 3);
    expect(midPt!.lon).toBeCloseTo(147.095, 3);

    // Reverse lookup: exact & perpendicular-offset pointer on Track Course -> matching elevation distM
    const exactNearest = findNearestPointOnTrackCourse(
      course.vertices,
      midPt!.x,
      midPt!.y
    );
    expect(exactNearest).not.toBeNull();
    expect(exactNearest!.distM).toBeCloseTo(1500, 1);

    const perpNearest = findNearestPointOnTrackCourse(
      course.vertices,
      midPt!.x + 4,
      midPt!.y + 4
    );
    expect(perpNearest).not.toBeNull();
    expect(Math.abs(perpNearest!.distM - 1500)).toBeLessThan(15);

    // North-up check: higher latitude (-37.22) must have smaller SVG Y than southern start (-37.24)
    const startPt = sampleTrackCourseAtDistance(course.vertices, 0);
    const endPt = sampleTrackCourseAtDistance(course.vertices, 2000);
    expect(endPt!.y).toBeLessThan(startPt!.y);
  });
});
