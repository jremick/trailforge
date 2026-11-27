import { describe, expect, it } from "vitest";
import {
  buildCanonicalTrackCorridor,
  buildElevationProfileSvg,
  buildTrackCourseSvg,
  computeTrackGradeDistribution,
  findNearestPointOnTrackCourse,
  findNearestPointOnTrackCourseGeo,
  formatBreakdownFactors,
  formatOverrideLabel,
  normalizeTrackInspector,
  sampleElevationAtDistance,
  sampleTrackCourseAtDistance,
} from "./inspectorHelpers";
import type { BaselineBreakdown, SegmentSummaryItem, TrackInspectorDetail } from "./types";

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

    // Geographic nearest-point lookup (for live MapLibre mini-map hover)
    const geoNearest = findNearestPointOnTrackCourseGeo(
      course.vertices,
      147.095,
      -37.225
    );
    expect(geoNearest).not.toBeNull();
    expect(geoNearest!.distM).toBeCloseTo(1500, 1);
  });

  it("deduplicates overlapping multi-tile `_2` segments and chains monotonically with zero cursor flickering", () => {
    // Simulates Billy Goat Bluff multi-tile overlap: interleaved `_2` spatial duplicates without
    // elevation_profile_m + an out-of-order `seq=1` segment that geometrically belongs at the end.
    const segs = [
      {
        segment_id: "tfseg_b_2",
        segment_seq: 0,
        primary_source: "osm",
        length_m: 1000,
        grade: "Extreme",
        elev_min_m: 900,
        elev_max_m: 1100,
        elevation_profile_m: [],
        geometry: {
          type: "LineString",
          coordinates: [
            [147.04, -37.44],
            [147.05, -37.43],
          ],
        },
      },
      {
        segment_id: "tfseg_a",
        segment_seq: 0,
        primary_source: "osm",
        length_m: 1000,
        grade: "Medium",
        elev_min_m: 1100,
        elev_max_m: 1300,
        max_slope_deg: 10.0,
        elevation_profile_m: [
          [0, 1300],
          [500, 1200],
          [1000, 1100],
        ],
        geometry: {
          type: "LineString",
          coordinates: [
            [147.03, -37.45],
            [147.04, -37.44],
          ],
        },
      },
      // Out-of-order segment geometrically at the far northern end (should NOT create a drop spike in middle)
      {
        segment_id: "tfseg_c",
        segment_seq: 1,
        primary_source: "osm",
        length_m: 1000,
        grade: "Medium",
        elev_min_m: 500,
        elev_max_m: 900,
        max_slope_deg: 8.0,
        elevation_profile_m: [
          [0, 900],
          [500, 700],
          [1000, 500],
        ],
        geometry: {
          type: "LineString",
          coordinates: [
            [147.05, -37.43],
            [147.06, -37.42],
          ],
        },
      },
      {
        segment_id: "tfseg_b",
        segment_seq: 2,
        primary_source: "osm",
        length_m: 1000,
        grade: "Extreme",
        elev_min_m: 900,
        elev_max_m: 1100,
        max_slope_deg: 22.0,
        elevation_profile_m: [
          [0, 1100],
          [500, 1000],
          [1000, 900],
        ],
        geometry: {
          type: "LineString",
          coordinates: [
            [147.04, -37.44],
            [147.05, -37.43],
          ],
        },
      },
    ];

    const corridor = buildCanonicalTrackCorridor(segs, [], []);
    // Duplicate `tfseg_b_2` dropped; remaining 3 chained geometrically a -> b -> c!
    expect(corridor.rebuiltFromSegments).toBe(true);
    expect(corridor.segments.map((s) => s.segment_id)).toEqual([
      "tfseg_a",
      "tfseg_b",
      "tfseg_c",
    ]);

    // Elevation profile descends smoothly 1300 -> 500 with zero drop spike
    expect(corridor.profile[0]!.elev_m).toBe(1300);
    expect(corridor.profile[corridor.profile.length - 1]!.elev_m).toBe(500);
    for (let i = 1; i < corridor.profile.length; i += 1) {
      expect(corridor.profile[i]!.dist_m).toBeGreaterThanOrEqual(
        corridor.profile[i - 1]!.dist_m
      );
      expect(corridor.profile[i]!.elev_m).toBeLessThanOrEqual(
        corridor.profile[i - 1]!.elev_m
      );
    }

    // Scrubbing monotonically along distM moves strictly monotonically North-East (zero two-point flicker!)
    let prevLat = -90;
    let prevLon = -180;
    for (let d = 0; d <= 3000; d += 250) {
      const pt = sampleTrackCourseAtDistance(corridor.vertices, d);
      expect(pt).not.toBeNull();
      expect(pt!.lat).toBeGreaterThanOrEqual(prevLat - 1e-6);
      expect(pt!.lon).toBeGreaterThanOrEqual(prevLon - 1e-6);
      prevLat = pt!.lat;
      prevLon = pt!.lon;
    }
  });

  it("retains a closed loop, a signed spur and its crossing at the right location", () => {
    const a = [147.05, -37.43];
    const b = [147.052, -37.43];
    const c = [147.052, -37.428];
    const d = [147.05, -37.428];
    const spur = [147.054, -37.43];
    const legs = [[a, b], [b, c], [c, d], [d, a], [b, spur]];
    const segments = legs.map((coordinates, index): SegmentSummaryItem => ({
      segment_id: `test-${index}`,
      segment_seq: index,
      length_m: 200,
      grade: index === 4 ? "Hard" : "Easy",
      has_gate_barrier: index === 4,
      access_status: index === 4 ? "closed" : "public",
      max_slope_deg: index === 4 ? 22 : 1,
      elevation_profile_m: [[0, 200], [100, 240], [200, 200]],
      geometry: { type: "LineString", coordinates },
    }));
    const fallback = segments.flatMap((segment, index) => [
      { dist_m: index * 200, elev_m: 200, segment_id: segment.segment_id },
      { dist_m: (index + 1) * 200, elev_m: 200, segment_id: segment.segment_id },
    ]);
    const corridor = buildCanonicalTrackCorridor(segments, fallback, [
      { name: "Spur Creek", dist_m: 850, perennial: true },
    ]);
    expect(corridor.segments).toHaveLength(5);
    expect(corridor.segments).toEqual(expect.arrayContaining(segments));
    expect(new Set(corridor.vertices.map((v) => v.polylineId)).size).toBeGreaterThan(1);
    expect(corridor.vertices.at(-1)?.distM).toBeCloseTo(1000);
    expect(corridor.profile.at(-1)?.dist_m).toBeCloseTo(1000);

    const crossing = sampleTrackCourseAtDistance(
      corridor.vertices, corridor.crossings[0]!.dist_m!
    );
    expect(crossing?.grade).toBe("Hard");
    expect(crossing?.lon).toBeCloseTo(147.0525, 5);
    expect(corridor.profile.some((p) => p.slope_deg === 22)).toBe(true);
  });

  it("propagates segment-level 4WD flag, non-public access precedence, and finite sustained slope", () => {
    const detail: TrackInspectorDetail = {
      track_id: "trk_seg_overrides",
      name: "Segment Override Track",
      state: "VIC",
      grade: "Hard",
      score: 52.0,
      total_length_m: 1200,
      elev_gain_m: 0,
      waterway_crossing_count: 0,
      is_4wd_only: false,
      has_gate_barrier: false,
      access_status: "public",
      sustained_slope_deg: null,
      max_slope_deg: null,
      segments: [
        {
          segment_id: "seg_1",
          segment_seq: 0,
          length_m: 600,
          grade: "Medium",
          is_4wd_only: false,
          access_status: "public",
          sustained_slope_deg: Number.NaN,
        },
        {
          segment_id: "seg_2",
          segment_seq: 1,
          length_m: 600,
          grade: "Hard",
          is_4wd_only: true,
          access_status: "permit",
          sustained_slope_deg: 11.4,
        },
      ],
    };

    const norm = normalizeTrackInspector(detail);
    expect(norm.is4wdOnly).toBe(true);
    expect(norm.accessStatus).toBe("permit");
    expect(norm.sustainedSlopeDeg).toBeCloseTo(11.4, 2);
  });
});
