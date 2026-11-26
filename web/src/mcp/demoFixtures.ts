import {
  DEFAULT_ATTRIBUTIONS,
  type CompactElevationPoint,
  type CompactSegmentGeom,
  type TrailForgeViewMode,
  type TrailForgeViewPayload,
} from "./types";

const DAVIES_SEGMENTS: CompactSegmentGeom[] = [
  {
    segment_id: "tfseg_dp_01",
    grade: "Medium",
    slope_deg: 9.2,
    length_m: 1450,
    is_steep: false,
    coords: [
      [148.092, -36.584],
      [148.101, -36.591],
      [148.109, -36.598],
    ],
  },
  {
    segment_id: "tfseg_dp_02",
    grade: "Hard",
    slope_deg: 16.4,
    length_m: 1620,
    is_steep: true,
    coords: [
      [148.109, -36.598],
      [148.118, -36.607],
      [148.126, -36.616],
    ],
  },
  {
    segment_id: "tfseg_dp_03",
    grade: "Extreme",
    slope_deg: 21.1,
    length_m: 1180,
    is_steep: true,
    coords: [
      [148.126, -36.616],
      [148.133, -36.625],
      [148.141, -36.633],
    ],
  },
  {
    segment_id: "tfseg_dp_04",
    grade: "Medium",
    slope_deg: 8.5,
    length_m: 1350,
    is_steep: false,
    coords: [
      [148.141, -36.633],
      [148.149, -36.641],
      [148.156, -36.648],
    ],
  },
];

const DAVIES_PROFILE: CompactElevationPoint[] = [
  { dist_m: 0, elev_m: 640, slope_deg: 5.1, grade: "Medium", segment_id: "tfseg_dp_01" },
  { dist_m: 720, elev_m: 745, slope_deg: 8.6, grade: "Medium", segment_id: "tfseg_dp_01" },
  { dist_m: 1450, elev_m: 870, slope_deg: 11.2, grade: "Medium", segment_id: "tfseg_dp_01" },
  { dist_m: 2260, elev_m: 1095, slope_deg: 16.4, grade: "Hard", segment_id: "tfseg_dp_02" },
  { dist_m: 3070, elev_m: 1310, slope_deg: 15.8, grade: "Hard", segment_id: "tfseg_dp_02" },
  { dist_m: 3660, elev_m: 1525, slope_deg: 21.1, grade: "Extreme", segment_id: "tfseg_dp_03" },
  { dist_m: 4250, elev_m: 1620, slope_deg: 14.5, grade: "Extreme", segment_id: "tfseg_dp_03" },
  { dist_m: 4920, elev_m: 1565, slope_deg: 7.9, grade: "Medium", segment_id: "tfseg_dp_04" },
  { dist_m: 5600, elev_m: 1490, slope_deg: 6.4, grade: "Medium", segment_id: "tfseg_dp_04" },
];

const TOM_GROGGIN_SEGMENTS: CompactSegmentGeom[] = [
  {
    segment_id: "tfseg_tg_01",
    grade: "Easy",
    slope_deg: 5.4,
    length_m: 1400,
    is_steep: false,
    coords: [
      [148.156, -36.648],
      [148.165, -36.642],
      [148.174, -36.635],
    ],
  },
  {
    segment_id: "tfseg_tg_02",
    grade: "Medium",
    slope_deg: 12.1,
    length_m: 1650,
    is_steep: false,
    coords: [
      [148.174, -36.635],
      [148.184, -36.628],
      [148.193, -36.619],
    ],
  },
  {
    segment_id: "tfseg_tg_03",
    grade: "Hard",
    slope_deg: 15.2,
    length_m: 1150,
    is_steep: true,
    coords: [
      [148.193, -36.619],
      [148.201, -36.611],
      [148.209, -36.604],
    ],
  },
];

const TOM_GROGGIN_PROFILE: CompactElevationPoint[] = [
  { dist_m: 0, elev_m: 1490, slope_deg: 5.4, grade: "Easy", segment_id: "tfseg_tg_01" },
  { dist_m: 1400, elev_m: 1320, slope_deg: 7.2, grade: "Easy", segment_id: "tfseg_tg_01" },
  { dist_m: 2250, elev_m: 1110, slope_deg: 12.1, grade: "Medium", segment_id: "tfseg_tg_02" },
  { dist_m: 3050, elev_m: 920, slope_deg: 11.4, grade: "Medium", segment_id: "tfseg_tg_02" },
  { dist_m: 3650, elev_m: 745, slope_deg: 15.2, grade: "Hard", segment_id: "tfseg_tg_03" },
  { dist_m: 4200, elev_m: 615, slope_deg: 8.8, grade: "Hard", segment_id: "tfseg_tg_03" },
];

const MOUNT_PINNIBAR_SEGMENTS: CompactSegmentGeom[] = [
  {
    segment_id: "tfseg_mp_01",
    grade: "Easy",
    slope_deg: 6.0,
    length_m: 2100,
    is_steep: false,
    coords: [
      [148.032, -36.535],
      [148.046, -36.544],
      [148.061, -36.553],
    ],
  },
  {
    segment_id: "tfseg_mp_02",
    grade: "Medium",
    slope_deg: 10.4,
    length_m: 1900,
    is_steep: false,
    coords: [
      [148.061, -36.553],
      [148.075, -36.564],
      [148.088, -36.574],
    ],
  },
];

const MOUNT_PINNIBAR_PROFILE: CompactElevationPoint[] = [
  { dist_m: 0, elev_m: 980, slope_deg: 5.5, grade: "Easy", segment_id: "tfseg_mp_01" },
  { dist_m: 1050, elev_m: 1140, slope_deg: 6.8, grade: "Easy", segment_id: "tfseg_mp_01" },
  { dist_m: 2100, elev_m: 1320, slope_deg: 8.9, grade: "Easy", segment_id: "tfseg_mp_01" },
  { dist_m: 3050, elev_m: 1560, slope_deg: 10.4, grade: "Medium", segment_id: "tfseg_mp_02" },
  { dist_m: 4000, elev_m: 1745, slope_deg: 9.7, grade: "Medium", segment_id: "tfseg_mp_02" },
];

export const DEMO_PAYLOADS: Record<TrailForgeViewMode, TrailForgeViewPayload> = {
  explore: {
    schema_version: "1.0",
    view_mode: "explore",
    title: "Australian Alps / High Country Trails",
    subtitle: "3 open & seasonal tracks · VIC / NSW border corridor",
    bbox: [148.02, -36.66, 148.22, -36.52],
    public_base_url: "https://trails.example.com",
    deep_link_url: "https://trails.example.com/?region=australian-alps",
    snowline_m: 1000,
    attributions: [...DEFAULT_ATTRIBUTIONS],
    rig_context: {
      capability_class: "Touring 4WD",
      vehicle_name: "Hilux SR5 Touring",
      has_snorkel: true,
    },
    explore: {
      tracks: [
        {
          track_id: "tftrk_davies_plain",
          name: "Davies Plain Track",
          state: "VIC",
          grade: "Extreme",
          score: 79,
          total_length_m: 5600,
          elev_gain_m: 980,
          max_slope_deg: 21.1,
          waterway_crossing_count: 1,
          is_4wd_only: true,
          closure_status: "seasonal",
          wet_risk_level: "damp",
          rain_72h_mm: 14.2,
          suitability: {
            rating: "Challenging",
            summary: "Steep 21° alpine climbs will test a Touring 4WD.",
          },
          segments: DAVIES_SEGMENTS,
        },
        {
          track_id: "tftrk_tom_groggin",
          name: "Tom Groggin River Track",
          state: "NSW",
          grade: "Hard",
          score: 64,
          total_length_m: 4200,
          elev_gain_m: 410,
          max_slope_deg: 15.2,
          waterway_crossing_count: 2,
          is_4wd_only: true,
          closure_status: "open",
          wet_risk_level: "damp",
          rain_72h_mm: 11.6,
          suitability: {
            rating: "Suited",
            summary: "Well matched to Touring 4WD with snorkel for Murray crossing.",
          },
          segments: TOM_GROGGIN_SEGMENTS,
        },
        {
          track_id: "tftrk_mt_pinnibar",
          name: "Mt Pinnibar Ridge Track",
          state: "VIC",
          grade: "Medium",
          score: 46,
          total_length_m: 4000,
          elev_gain_m: 765,
          max_slope_deg: 10.4,
          waterway_crossing_count: 0,
          is_4wd_only: false,
          closure_status: "open",
          wet_risk_level: "dry",
          rain_72h_mm: 2.8,
          suitability: {
            rating: "Suited",
            summary: "Ideal scenic alpine ridge climb for Touring 4WD.",
          },
          segments: MOUNT_PINNIBAR_SEGMENTS,
        },
      ],
      closures: [
        {
          closure_id: "cls_101",
          title: "Seasonal Winter Road Closure — Davies Plain Gate",
          agency: "Parks Victoria / DEECA",
          closure_type: "seasonal",
          severity: "warning",
          state: "VIC",
          source_url: "https://www.parks.vic.gov.au/",
          lon: 148.126,
          lat: -36.616,
        },
      ],
      reports: [
        {
          report_id: "rep_202",
          report_type: "fallen_tree",
          title: "Snow gum across upper spur",
          notes: "Passable via high-clearance bypass; chainsaw advised.",
          severity: "warning",
          confirm_count: 3,
          lon: 148.133,
          lat: -36.625,
        },
      ],
      pois: [
        {
          poi_id: "poi_hut_1",
          name: "Davies Plain Hut",
          category: "hut",
          state: "VIC",
          lon: 148.149,
          lat: -36.641,
          elev_m: 1550,
        },
        {
          poi_id: "poi_camp_1",
          name: "Tom Groggin Campground",
          category: "campground",
          state: "NSW",
          lon: 148.205,
          lat: -36.607,
          elev_m: 620,
        },
      ],
      rainfall_summary: {
        rain_24h_mm: 4.1,
        rain_72h_mm: 14.2,
        wet_risk_level: "damp",
      },
    },
  },

  track: {
    schema_version: "1.0",
    view_mode: "track",
    title: "Davies Plain Track",
    subtitle: "VIC · Alpine National Park · 5.6 km · Extreme (79/100)",
    bbox: [148.09, -36.65, 148.16, -36.58],
    public_base_url: "https://trails.example.com",
    deep_link_url: "https://trails.example.com/?track=tftrk_davies_plain&profile=1",
    snowline_m: 1000,
    attributions: [...DEFAULT_ATTRIBUTIONS],
    rig_context: {
      capability_class: "Touring 4WD",
      vehicle_name: "Hilux SR5 Touring",
      has_snorkel: true,
    },
    track: {
      summary: {
        track_id: "tftrk_davies_plain",
        name: "Davies Plain Track",
        state: "VIC",
        grade: "Extreme",
        score: 79,
        total_length_m: 5600,
        elev_gain_m: 980,
        elev_loss_m: 130,
        elev_min_m: 640,
        elev_max_m: 1620,
        max_slope_deg: 21.1,
        sustained_slope_deg: 13.8,
        waterway_crossing_count: 1,
        is_4wd_only: true,
        closure_status: "seasonal",
        wet_risk_level: "damp",
        rain_72h_mm: 14.2,
        suitability: {
          rating: "Challenging",
          summary: "Extreme 21.1° climb will challenge a Touring 4WD; recovery gear & dry weather advised.",
          reasons: [
            "Peak 21.1° climb over loose alpine shale",
            "Perennial crossing at Buckwong Creek (snorkel equipped)",
          ],
        },
      },
      segments: DAVIES_SEGMENTS,
      elevation_profile: DAVIES_PROFILE,
      crossings: [
        {
          name: "Buckwong Creek Ford",
          perennial: true,
          bridged: false,
          dist_m: 1450,
        },
      ],
      baseline_breakdown: {
        final_score: 79,
        dem_tier: "GA 5m LiDAR",
        overrides: ["steep_rough_or_perennial_extreme_floor"],
        factors: {
          surface_tracktype: {
            points: 26,
            max_points: 35,
            surface_class: "rough_rock_mud_sand",
            tracktype: "grade4",
          },
          max_slope: { points: 29, max_points: 30, max_slope_deg: 21.1 },
          sustained_relief: {
            points: 13,
            max_points: 15,
            sustained_slope_deg: 13.8,
            elev_gain_m: 980,
          },
          waterway_crossings: {
            points: 7,
            max_points: 12,
            unbridged_count: 1,
            perennial: true,
          },
          four_wd_access: {
            points: 4,
            max_points: 8,
            is_4wd_only: true,
            trail_class: "alpine_fire_trail",
          },
        },
      },
      live_summary: {
        live_grade: "Extreme",
        confidence_level: "medium",
        sample_count: 14,
        last_driven_at: "2026-11-24T04:15:00Z",
        avg_speed_kmh: 11.4,
        est_duration_min: 34,
        vehicle_class_split: {
          "Touring 4WD": 8,
          "Modified 4WD": 6,
        },
        wet_risk_level: "damp",
        rain_72h_mm: 14.2,
      },
      closures: [
        {
          closure_id: "cls_101",
          title: "Seasonal Winter Closure Window (June–October)",
          agency: "Parks Victoria / DEECA",
          closure_type: "seasonal",
          severity: "warning",
          state: "VIC",
          source_url: "https://www.parks.vic.gov.au/",
          lon: 148.126,
          lat: -36.616,
        },
      ],
      gate_warnings: [
        {
          report_id: "gw_12",
          report_type: "locked_gate",
          title: "Seasonal gate unlocked (confirmed 2d ago)",
          notes: "Gate open for summer season; keep shut behind convoy.",
          is_gate_warning: true,
          confirm_count: 4,
          lon: 148.109,
          lat: -36.598,
        },
      ],
      condition_reports: [
        {
          report_id: "rep_202",
          report_type: "washout",
          title: "Deep ruts on middle ridge",
          notes: "Pick high line on left bank after rain.",
          confirm_count: 3,
          lon: 148.133,
          lat: -36.625,
        },
      ],
      nearby_pois: [
        {
          poi_id: "poi_hut_1",
          name: "Davies Plain Hut",
          category: "hut",
          state: "VIC",
          lon: 148.149,
          lat: -36.641,
          elev_m: 1550,
          distance_m: 420,
        },
      ],
      parks: [
        { name: "Alpine National Park", park_type: "National Park", state: "VIC" },
      ],
      export_gpx_url: "https://trails.example.com/api/tracks/tftrk_davies_plain/export?format=gpx",
      export_geojson_url:
        "https://trails.example.com/api/tracks/tftrk_davies_plain/export?format=geojson",
    },
  },

  route: {
    schema_version: "1.0",
    view_mode: "route",
    title: "Davies Plain to Tom Groggin Traverse",
    subtitle: "2 stitched legs · 9.8 km · Touring 4WD",
    bbox: [148.09, -36.65, 148.21, -36.58],
    public_base_url: "https://trails.example.com",
    deep_link_url:
      "https://trails.example.com/?route=tftrk_davies_plain,tftrk_tom_groggin&rig=Touring+4WD",
    snowline_m: 1000,
    attributions: [...DEFAULT_ATTRIBUTIONS],
    rig_context: {
      capability_class: "Touring 4WD",
      vehicle_name: "Hilux SR5 Touring",
      has_snorkel: true,
    },
    route: {
      name: "Davies Plain to Tom Groggin Traverse",
      total_length_m: 9800,
      est_duration_min: 62,
      elev_gain_m: 980,
      elev_loss_m: 1005,
      elev_min_m: 615,
      elev_max_m: 1620,
      worst_grade: "Extreme",
      suitability: {
        rating: "Challenging",
        summary: "Challenging alpine traverse for Touring 4WD with steep climb & Murray River crossing.",
        reasons: [
          "Worst leg is Extreme (Davies Plain Track — 21.1° climb)",
          "3 waterway crossings including perennial Murray River ford",
        ],
      },
      safety_summary: {
        has_blocking_closure: false,
        closure_count: 1,
        gate_warning_count: 1,
        wet_risk_level: "damp",
        waterway_crossing_count: 3,
        perennial_crossing_count: 2,
        gap_count: 0,
        max_gap_m: 0,
      },
      legs: [
        {
          leg_index: 0,
          track_id: "tftrk_davies_plain",
          name: "Davies Plain Track",
          reversed: false,
          length_m: 5600,
          grade: "Extreme",
          gap_from_prev_m: 0,
          segments: DAVIES_SEGMENTS,
        },
        {
          leg_index: 1,
          track_id: "tftrk_tom_groggin",
          name: "Tom Groggin River Track",
          reversed: false,
          length_m: 4200,
          grade: "Hard",
          gap_from_prev_m: 0,
          segments: TOM_GROGGIN_SEGMENTS,
        },
      ],
      elevation_profile: [
        ...DAVIES_PROFILE,
        ...TOM_GROGGIN_PROFILE.slice(1).map((pt) => ({
          ...pt,
          dist_m: pt.dist_m + 5600,
        })),
      ],
      closures: [
        {
          closure_id: "cls_101",
          title: "Seasonal Closure Advisory — Alpine NP",
          agency: "Parks Victoria / DEECA",
          closure_type: "seasonal",
          severity: "warning",
          state: "VIC",
          source_url: "https://www.parks.vic.gov.au/",
          lon: 148.126,
          lat: -36.616,
        },
      ],
      gate_warnings: [
        {
          report_id: "gw_12",
          report_type: "locked_gate",
          title: "Davies Plain seasonal gate",
          notes: "Confirm open status with Parks Victoria prior to departure.",
          is_gate_warning: true,
          lon: 148.109,
          lat: -36.598,
        },
      ],
      waypoints: [
        { name: "Davies Plain Hut Camp", category: "camp", lon: 148.149, lat: -36.641 },
        { name: "Murray River Ford", category: "water", lon: 148.193, lat: -36.619 },
      ],
      nearby_pois: [
        {
          poi_id: "poi_camp_1",
          name: "Tom Groggin Campground",
          category: "campground",
          state: "NSW",
          lon: 148.205,
          lat: -36.607,
          elev_m: 620,
        },
      ],
      gridforge_cli:
        'gridforge render --bbox 148.09,-36.65,148.21,-36.58 --gpx davies-plain-traverse.gpx --dpi 300 --out davies-plain-map.pdf',
      export_gpx_url:
        "https://trails.example.com/api/routes/export?track_ids=tftrk_davies_plain,tftrk_tom_groggin&format=gpx",
    },
  },

  compare: {
    schema_version: "1.0",
    view_mode: "compare",
    title: "Compare Alpine Tracks (3)",
    subtitle: "Davies Plain vs Tom Groggin vs Mt Pinnibar · Touring 4WD",
    bbox: [148.02, -36.66, 148.22, -36.52],
    public_base_url: "https://trails.example.com",
    deep_link_url: "https://trails.example.com/?region=australian-alps",
    snowline_m: 1000,
    attributions: [...DEFAULT_ATTRIBUTIONS],
    rig_context: {
      capability_class: "Touring 4WD",
      vehicle_name: "Hilux SR5 Touring",
      has_snorkel: true,
    },
    compare: {
      tracks: [
        {
          summary: {
            track_id: "tftrk_davies_plain",
            name: "Davies Plain Track",
            state: "VIC",
            grade: "Extreme",
            score: 79,
            total_length_m: 5600,
            elev_gain_m: 980,
            max_slope_deg: 21.1,
            waterway_crossing_count: 1,
            suitability: { rating: "Challenging" },
          },
          segments: DAVIES_SEGMENTS,
          elevation_profile: DAVIES_PROFILE,
          color_hex: "#EF4444",
        },
        {
          summary: {
            track_id: "tftrk_tom_groggin",
            name: "Tom Groggin River Track",
            state: "NSW",
            grade: "Hard",
            score: 64,
            total_length_m: 4200,
            elev_gain_m: 410,
            max_slope_deg: 15.2,
            waterway_crossing_count: 2,
            suitability: { rating: "Suited" },
          },
          segments: TOM_GROGGIN_SEGMENTS,
          elevation_profile: TOM_GROGGIN_PROFILE,
          color_hex: "#38BDF8",
        },
        {
          summary: {
            track_id: "tftrk_mt_pinnibar",
            name: "Mt Pinnibar Ridge Track",
            state: "VIC",
            grade: "Medium",
            score: 46,
            total_length_m: 4000,
            elev_gain_m: 765,
            max_slope_deg: 10.4,
            waterway_crossing_count: 0,
            suitability: { rating: "Suited" },
          },
          segments: MOUNT_PINNIBAR_SEGMENTS,
          elevation_profile: MOUNT_PINNIBAR_PROFILE,
          color_hex: "#22C55E",
        },
      ],
    },
  },
};

export function getDemoPayload(modeRaw: string | null | undefined): TrailForgeViewPayload | null {
  if (!modeRaw) return null;
  const key = modeRaw.trim().toLowerCase();
  if (key === "explore" || key === "track" || key === "route" || key === "compare") {
    return DEMO_PAYLOADS[key];
  }
  return null;
}
