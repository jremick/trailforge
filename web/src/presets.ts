import type {
  AttributionItem,
  BasemapId,
  BBoxTuple,
  LayerVisibilityState,
  OfflinePackRegionItem,
  PoiIconMeta,
  RegionPreset,
  TrackDifficulty,
} from "./types";

/**
 * Default launch viewport centered on the VIC/NSW Australian Alps border corridor
 * (Davies Plain / Tom Groggin / Murray River headwaters between Alpine NP & Kosciuszko NP).
 */
export const DEFAULT_PRESET_ID = "australian-alps";
export const DEFAULT_CENTER_LON = 148.125;
export const DEFAULT_CENTER_LAT = -36.615;
export const DEFAULT_MAP_ZOOM = 11.4;

/**
 * Default snow-line elevation threshold bands (meters AHD) matching
 * trailforge_vector.pmtiles `snowline_bands` layer (`elev_min_m`).
 */
export const DEFAULT_SNOWLINE_BANDS_M: readonly number[] = [
  800, 900, 1000, 1100, 1200, 1400, 1600,
];
export const DEFAULT_SNOWLINE_ELEVATION_M = 1000;

/**
 * Combined VIC + NSW + ACT launch bounding box [west, south, east, north].
 * Preserved for backward-compatible corridor checks.
 */
export const VIC_NSW_LAUNCH_BBOX: BBoxTuple = [140.95, -39.2, 153.68, -28.15];

/**
 * Full Pan-Australia bounding box across all 8 states & territories [west, south, east, north].
 */
export const AUSTRALIA_BBOX: BBoxTuple = [112.85, -43.75, 153.68, -10.0];

export type PackFilterCategory = "all" | "region" | "state";

export const ICONIC_REGION_PACK_IDS = [
  "australian-alps",
  "blue-mountains",
  "tasmania-wilderness",
  "far-north-qld",
  "flinders-ranges",
  "red-centre",
  "perth-hills",
] as const;

export const STATEWIDE_PACK_IDS = [
  "vic",
  "nsw",
  "act",
  "tas",
  "qld",
  "wa",
  "sa",
  "nt",
] as const;

const STATEWIDE_PACK_ID_SET: ReadonlySet<string> = new Set(STATEWIDE_PACK_IDS);

/**
 * Pan-Australia 15-region offline pack catalog (7 Iconic 4WD Regions + 8 Statewide)
 * plus curated 4WD corridors & landmarks (including Namadgi / Brindabellas ACT).
 * Bounding boxes and UTM zones adapted from sibling project GridForge.
 */
export const REGION_PRESETS: readonly RegionPreset[] = [
  // ---------- 7 Iconic 4WD Regions ----------
  {
    id: "australian-alps",
    label: "Australian Alps / High Country (VIC/NSW)",
    shortLabel: "Australian Alps",
    group: "region",
    packCategory: "region",
    states: ["VIC", "NSW"],
    stateTag: "VIC/NSW · 55H",
    utmZones: ["55H"],
    center: [148.125, -36.615],
    zoom: 11.4,
    bbox: [147.8, -37.1, 149.1, -35.8],
    description:
      "Davies Plain, Tom Groggin, Mt Kosciuszko, Alpine NP & Snowy Mountains 4WD corridor.",
    estimatedPackHint: "~65 MB",
  },
  {
    id: "blue-mountains",
    label: "Blue Mountains (NSW)",
    shortLabel: "Blue Mountains",
    group: "region",
    packCategory: "region",
    states: ["NSW"],
    stateTag: "NSW · 56H",
    utmZones: ["56H"],
    center: [150.312, -33.732],
    zoom: 10.6,
    bbox: [150.0, -34.05, 150.7, -33.4],
    description:
      "Katoomba, Wollemi, Newnes Plateau, Jamison & Grose Valleys, Kanangra-Boyd.",
    estimatedPackHint: "~35 MB",
  },
  {
    id: "tasmania-wilderness",
    label: "Tasmania Wilderness / West Coast (TAS)",
    shortLabel: "TAS Wilderness",
    group: "region",
    packCategory: "region",
    states: ["TAS"],
    stateTag: "TAS · 55G",
    utmZones: ["55G"],
    center: [145.65, -41.9],
    zoom: 8.8,
    bbox: [144.6, -43.0, 146.8, -40.8],
    description:
      "Tarkine, Balfour Track, West Coast beach runs, Cradle Mtn & Central Highlands.",
    estimatedPackHint: "~60 MB",
  },
  {
    id: "far-north-qld",
    label: "Cape York / Far North QLD (QLD)",
    shortLabel: "Cape York / FNQ",
    group: "region",
    packCategory: "region",
    states: ["QLD"],
    stateTag: "QLD · 54L–55L",
    utmZones: ["54L", "55K", "55L"],
    center: [144.2, -15.2],
    zoom: 7.0,
    bbox: [141.8, -17.6, 146.1, -10.7],
    description:
      "Old Telegraph Track, CREB Track, Bloomfield, Daintree & Atherton Tablelands.",
    estimatedPackHint: "~110 MB",
  },
  {
    id: "flinders-ranges",
    label: "Flinders Ranges (SA)",
    shortLabel: "Flinders Ranges",
    group: "region",
    packCategory: "region",
    states: ["SA"],
    stateTag: "SA · 54J",
    utmZones: ["54J"],
    center: [138.65, -31.45],
    zoom: 8.6,
    bbox: [137.8, -32.6, 139.6, -30.1],
    description:
      "Ikara-Flinders Ranges, Skytrek, Arkaroola, Parachilna & Brachina Gorge 4WD loops.",
    estimatedPackHint: "~55 MB",
  },
  {
    id: "red-centre",
    label: "Red Centre / MacDonnell Ranges (NT)",
    shortLabel: "Red Centre",
    group: "region",
    packCategory: "region",
    states: ["NT", "SA"],
    stateTag: "NT · 52J–53J",
    utmZones: ["52J", "53J"],
    center: [132.5, -24.1],
    zoom: 7.4,
    bbox: [130.4, -25.6, 134.8, -23.1],
    description:
      "Mereenie Loop, Boggy Hole, Finke Gorge, West MacDonnell Ranges & Uluru approaches.",
    estimatedPackHint: "~85 MB",
  },
  {
    id: "perth-hills",
    label: "Perth Hills / WA Southwest (WA)",
    shortLabel: "Perth Hills / SW",
    group: "region",
    packCategory: "region",
    states: ["WA"],
    stateTag: "WA · 50H",
    utmZones: ["50H"],
    center: [116.05, -32.45],
    zoom: 8.5,
    bbox: [115.0, -34.5, 116.8, -31.5],
    description:
      "Powerlines Track, Mundaring, Dwellingup, Darling Scarp & Southwest forest tracks.",
    estimatedPackHint: "~65 MB",
  },

  // ---------- 8 Australian States & Territories (Statewide) ----------
  {
    id: "vic",
    label: "Victoria (Statewide)",
    shortLabel: "Victoria",
    group: "region",
    packCategory: "state",
    states: ["VIC"],
    stateTag: "VIC · 54H–55H",
    utmZones: ["54H", "55H"],
    center: [145.5, -36.85],
    zoom: 6.7,
    bbox: [140.95, -39.2, 150.05, -33.95],
    description:
      "Full Victoria from Mallee & Grampians to Victorian High Country and East Gippsland.",
    estimatedPackHint: "~650 MB",
  },
  {
    id: "nsw",
    label: "New South Wales & ACT (Statewide)",
    shortLabel: "NSW & ACT",
    group: "region",
    packCategory: "state",
    states: ["NSW", "ACT"],
    stateTag: "NSW/ACT · 54H–56J",
    utmZones: ["54H", "54J", "55H", "55J", "56H", "56J"],
    center: [147.3, -33.0],
    zoom: 6.0,
    bbox: [140.95, -37.55, 153.68, -28.15],
    description:
      "Full NSW & ACT including Blue Mountains, Brindabellas, Snowy Mountains & Outback.",
    estimatedPackHint: "~1.4 GB",
  },
  {
    id: "act",
    label: "Australian Capital Territory (Statewide)",
    shortLabel: "ACT / Namadgi",
    group: "region",
    packCategory: "state",
    states: ["ACT"],
    stateTag: "ACT · 55H",
    utmZones: ["55H"],
    center: [149.02, -35.52],
    zoom: 9.8,
    bbox: [148.74, -35.94, 149.42, -35.1],
    description:
      "Namadgi National Park, Brindabella Range fire trails, Tidbinbilla & Cotter corridor.",
    estimatedPackHint: "~45 MB",
  },
  {
    id: "tas",
    label: "Tasmania (Statewide)",
    shortLabel: "Tasmania",
    group: "region",
    packCategory: "state",
    states: ["TAS"],
    stateTag: "TAS · 54G–55G",
    utmZones: ["54G", "55G"],
    center: [146.5, -42.0],
    zoom: 7.4,
    bbox: [143.8, -43.75, 148.55, -39.55],
    description:
      "Full Tasmania mainland, West Coast sand/mud tracks, Tarkine & alpine highlands.",
    estimatedPackHint: "~220 MB",
  },
  {
    id: "qld",
    label: "Queensland (Statewide)",
    shortLabel: "Queensland",
    group: "region",
    packCategory: "state",
    states: ["QLD"],
    stateTag: "QLD · 54J–56J",
    utmZones: ["54J", "54K", "54L", "55J", "55K", "55L", "56J"],
    center: [145.5, -20.5],
    zoom: 5.2,
    bbox: [137.95, -29.2, 153.6, -10.0],
    description:
      "Full Queensland from Glass House & K'gari to Cape York Peninsula & Channel Country.",
    estimatedPackHint: "~2.1 GB",
  },
  {
    id: "wa",
    label: "Western Australia (Statewide)",
    shortLabel: "Western Australia",
    group: "region",
    packCategory: "state",
    states: ["WA"],
    stateTag: "WA · 49J–52K",
    utmZones: ["49J", "50H", "50J", "50K", "51H", "51J", "51K", "52H", "52J", "52K"],
    center: [121.5, -25.0],
    zoom: 4.8,
    bbox: [112.85, -35.2, 129.05, -13.65],
    description:
      "Full Western Australia from Southwest forests to Pilbara gorges, Gibb River & Kimberley.",
    estimatedPackHint: "~2.8 GB",
  },
  {
    id: "sa",
    label: "South Australia (Statewide)",
    shortLabel: "South Australia",
    group: "region",
    packCategory: "state",
    states: ["SA"],
    stateTag: "SA · 52H–54J",
    utmZones: ["52H", "52J", "53H", "53J", "54H", "54J"],
    center: [135.8, -32.0],
    zoom: 5.6,
    bbox: [128.98, -38.1, 141.05, -25.95],
    description:
      "Full South Australia from Flinders Ranges & Border Track to Simpson Desert & Eyre Peninsula.",
    estimatedPackHint: "~900 MB",
  },
  {
    id: "nt",
    label: "Northern Territory (Statewide)",
    shortLabel: "Northern Territory",
    group: "region",
    packCategory: "state",
    states: ["NT"],
    stateTag: "NT · 52J–53L",
    utmZones: ["52J", "52K", "52L", "53J", "53K", "53L"],
    center: [133.4, -19.5],
    zoom: 5.4,
    bbox: [128.98, -26.05, 138.05, -10.9],
    description:
      "Full Northern Territory from Top End & Litchfield to West MacDonnell Ranges & Finke.",
    estimatedPackHint: "~1.2 GB",
  },

  // ---------- Curated 4WD Corridors & Landmarks ----------
  {
    id: "davies-plain-corridor",
    label: "Davies Plain / Tom Groggin Border (VIC/NSW)",
    shortLabel: "Davies Plain",
    group: "landmark",
    states: ["VIC", "NSW"],
    stateTag: "VIC/NSW · 55H",
    utmZones: ["55H"],
    center: [148.125, -36.615],
    zoom: 12.2,
    bbox: [148.05, -36.68, 148.2, -36.55],
    description:
      "Murray River border crossing, steep alpine climbs & high-country huts (600–1,650 m).",
  },
  {
    id: "brindabella-act",
    label: "Namadgi / Brindabellas (ACT/NSW)",
    shortLabel: "Namadgi / Brindabellas",
    group: "landmark",
    states: ["ACT", "NSW"],
    stateTag: "ACT/NSW · 55H",
    utmZones: ["55H"],
    center: [148.88, -35.47],
    zoom: 10.5,
    bbox: [148.74, -35.94, 149.42, -35.1],
    description: "Brindabella fire trails, Mt Coree, Two Sticks & Namadgi National Park.",
  },
  {
    id: "mt-bogong",
    label: "Mt Bogong / Alpine NP (VIC)",
    shortLabel: "Mt Bogong",
    group: "landmark",
    states: ["VIC"],
    stateTag: "VIC · 55H",
    utmZones: ["55H"],
    center: [147.306, -36.733],
    zoom: 11.5,
    bbox: [147.12, -36.88, 147.52, -36.58],
    description: "Victorian High Country ridges, alpine huts & seasonal tracks.",
  },
  {
    id: "mt-kosciuszko",
    label: "Mt Kosciuszko / Snowy Mtns (NSW)",
    shortLabel: "Kosciuszko",
    group: "landmark",
    states: ["NSW"],
    stateTag: "NSW · 55H",
    utmZones: ["55H"],
    center: [148.263, -36.456],
    zoom: 11.5,
    bbox: [148.08, -36.6, 148.45, -36.3],
    description: "Main Range, Alpine Way & Snowy Mountains alpine zone.",
  },
  {
    id: "grampians",
    label: "Grampians / Gariwerd (VIC)",
    shortLabel: "Grampians",
    group: "landmark",
    states: ["VIC"],
    stateTag: "VIC · 54H",
    utmZones: ["54H"],
    center: [142.515, -37.145],
    zoom: 11.0,
    bbox: [142.15, -37.55, 142.75, -36.85],
    description: "Sandstone escarpments, Halls Gap & forest 4WD tracks.",
  },
  {
    id: "budawangs",
    label: "Budawangs / Morton NP (NSW)",
    shortLabel: "Budawangs",
    group: "landmark",
    states: ["NSW"],
    stateTag: "NSW · 56H",
    utmZones: ["56H"],
    center: [150.193, -35.276],
    zoom: 11.2,
    bbox: [149.95, -35.48, 150.45, -35.08],
    description: "South Coast fire trails, Clyde Valley & Morton National Park.",
  },
];

export const PAN_AU_PACK_PRESETS: readonly RegionPreset[] =
  REGION_PRESETS.filter((preset) => preset.group === "region");

/**
 * Classify an offline pack entry into `"region"` (Iconic 4WD Region) or `"state"` (Statewide),
 * preferring backend `category` / `region_type` fields with a clean ID-based fallback.
 */
export function classifyPackCategory(
  pack: Pick<OfflinePackRegionItem, "region_id" | "category" | "region_type">
): "region" | "state" {
  const cat = (pack.category ?? "").trim().toLowerCase();
  if (cat === "state") return "state";
  if (cat === "region" || cat === "tactical") return "region";

  const rtype = (pack.region_type ?? "").trim().toLowerCase();
  if (rtype === "state") return "state";
  if (rtype === "tactical" || rtype === "region") return "region";

  const id = pack.region_id.trim().toLowerCase();
  return STATEWIDE_PACK_ID_SET.has(id) ? "state" : "region";
}

/**
 * Filter offline regional packs by `"all"`, `"region"` (Iconic 4WD Regions), or `"state"` (Statewide).
 */
export function filterPackRegions<
  T extends Pick<OfflinePackRegionItem, "region_id" | "category" | "region_type">,
>(regions: readonly T[], filter: PackFilterCategory): T[] {
  if (filter === "all") return [...regions];
  return regions.filter((r) => classifyPackCategory(r) === filter);
}

export function getPresetById(id: string): RegionPreset | undefined {
  return REGION_PRESETS.find((preset) => preset.id === id);
}

export function getDefaultPreset(): RegionPreset {
  return REGION_PRESETS[0]!;
}

export const BASEMAP_OPTIONS: readonly {
  id: BasemapId;
  label: string;
  hint: string;
}[] = [
  { id: "opentopo", label: "Topo", hint: "OpenTopoMap raster (online)" },
  {
    id: "vector",
    label: "Vector",
    hint: "Crisp OpenMapTiles vector basemap with labels floated above tracks & terrain",
  },
  { id: "osm", label: "OSM", hint: "OpenStreetMap Standard (online)" },
  { id: "satellite", label: "Imagery", hint: "Esri World Imagery (online)" },
  {
    id: "offline-topo",
    label: "Offline Topo",
    hint: "100% offline dark topo graticule, Terrain-RGB hillshade & vector PMTiles",
  },
];

export const DIFFICULTY_META: readonly {
  id: TrackDifficulty;
  label: string;
  color: string;
  hint: string;
}[] = [
  {
    id: "easy",
    label: "Easy",
    color: "#22C55E",
    hint: "2WD / AWD gravel & graded forest roads",
  },
  {
    id: "medium",
    label: "Medium",
    color: "#38BDF8",
    hint: "Stock 4WD, moderate climbs, shallow ruts/creeks",
  },
  {
    id: "hard",
    label: "Hard",
    color: "#F59E0B",
    hint: "Low-range 4WD, steep ≥14° grades, rocky/boggy sections",
  },
  {
    id: "extreme",
    label: "Extreme",
    color: "#EF4444",
    hint: "Modified 4WD, lockers/winch advised, severe climbs or crossings",
  },
];

export function getGradeColor(grade: string | null | undefined): string {
  const norm = (grade ?? "").trim().toLowerCase();
  if (norm === "easy") return "#22C55E";
  if (norm === "medium") return "#38BDF8";
  if (norm === "hard") return "#F59E0B";
  if (norm === "extreme") return "#EF4444";
  return "#94A3B8";
}

export function createInitialLayerState(): LayerVisibilityState {
  return {
    placesRegions: true,
    highways: true,
    tracks: true,
    steepHighlights: true,
    hotspots: true,
    closures: true,
    reports: true,
    unmapped: false,
    contoursHillshade: true,
    snowLine: false,
    snowLineElevationM: DEFAULT_SNOWLINE_ELEVATION_M,
    parksWaterways: true,
    pois: true,
  };
}

export const OUTDOOR_POI_ICONS: readonly PoiIconMeta[] = [
  { id: "campground", label: "Campground", iconPath: "/icons/campground.svg" },
  { id: "hut", label: "Alpine Hut", iconPath: "/icons/hut.svg" },
  { id: "trig", label: "Trig / Peak", iconPath: "/icons/trig.svg" },
  { id: "water_bore", label: "Water / Bore", iconPath: "/icons/water_bore.svg" },
  { id: "fuel", label: "Fuel", iconPath: "/icons/fuel.svg" },
  { id: "shelter", label: "Shelter", iconPath: "/icons/shelter.svg" },
  { id: "saddle", label: "Saddle / Pass", iconPath: "/icons/saddle.svg" },
  { id: "fire_station", label: "RFS / CFA", iconPath: "/icons/fire_station.svg" },
  { id: "helipad", label: "Helipad", iconPath: "/icons/helipad.svg" },
  { id: "homestead", label: "Homestead", iconPath: "/icons/homestead.svg" },
  { id: "airfield", label: "Airfield", iconPath: "/icons/airfield.svg" },
  { id: "hospital", label: "Hospital", iconPath: "/icons/hospital.svg" },
  { id: "police", label: "Police", iconPath: "/icons/police.svg" },
  { id: "comm_tower", label: "Comm Tower", iconPath: "/icons/comm_tower.svg" },
  { id: "obstacle_tower", label: "Gate / Barrier", iconPath: "/icons/obstacle_tower.svg" },
];

const POI_CATEGORY_TO_ICON: Record<string, string> = {
  campground: "campground",
  camp: "campground",
  campsite: "campground",
  hut: "hut",
  alpine_hut: "hut",
  shelter: "shelter",
  trig: "trig",
  survey_mark: "trig",
  peak: "trig",
  lookout: "saddle",
  saddle: "saddle",
  pass: "saddle",
  water: "water_bore",
  water_bore: "water_bore",
  bore: "water_bore",
  fuel: "fuel",
  gate: "obstacle_tower",
  obstacle_tower: "obstacle_tower",
  fire_station: "fire_station",
  helipad: "helipad",
  homestead: "homestead",
  airfield: "airfield",
  hospital: "hospital",
  police: "police",
  comm_tower: "comm_tower",
};

/**
 * Resolve a POI category or explicit icon name to an adapted SVG icon ID under `/icons/`.
 */
export function resolvePoiIconName(
  categoryOrIcon: string | null | undefined
): string {
  const key = (categoryOrIcon ?? "")
    .trim()
    .toLowerCase()
    .replace(/\.svg$/, "");
  if (key in POI_CATEGORY_TO_ICON) {
    return POI_CATEGORY_TO_ICON[key]!;
  }
  return "trig";
}

/**
 * Snap a numeric elevation threshold to the closest available snow-line band
 * in `snowline_bands` (default `[800, 900, 1000, 1100, 1200, 1400, 1600]`).
 */
export function snapSnowlineBand(
  elevationM: number,
  availableBands: readonly number[] = DEFAULT_SNOWLINE_BANDS_M
): number {
  if (availableBands.length === 0) return DEFAULT_SNOWLINE_ELEVATION_M;
  let best = availableBands[0]!;
  let bestDiff = Math.abs(elevationM - best);
  for (const band of availableBands) {
    const diff = Math.abs(elevationM - band);
    if (diff < bestDiff) {
      best = band;
      bestDiff = diff;
    }
  }
  return best;
}

/**
 * Required open-data attributions (AGENTS.md License & Data Hygiene).
 * Never strip these attributions from the UI.
 */
export const REQUIRED_ATTRIBUTIONS: readonly AttributionItem[] = [
  {
    id: "osm",
    label: "© OpenStreetMap contributors",
    license: "ODbL",
    fullText: "Map base data © OpenStreetMap contributors (ODbL)",
  },
  {
    id: "ga-dea",
    label: "GA / DEA",
    license: "CC BY 4.0",
    fullText:
      "Elevation & Hydrology © Commonwealth of Australia (Geoscience Australia / Digital Earth Australia) CC BY 4.0",
  },
  {
    id: "capad",
    label: "CAPAD 2025",
    license: "CC BY 4.0",
    fullText: "Protected Areas © DCCEEW CAPAD 2025 (CC BY 4.0)",
  },
  {
    id: "nsw-spatial",
    label: "NSW Spatial",
    license: "CC BY 4.0",
    fullText: "NSW Tracks & POIs © State of New South Wales (Spatial Services) CC BY 4.0",
  },
  {
    id: "vic-deeca",
    label: "VIC DEECA / Vicmap",
    license: "CC BY 4.0",
    fullText: "VIC Tracks & RecWeb © State of Victoria (DEECA / Vicmap) CC BY 4.0",
  },
  {
    id: "open-meteo",
    label: "Open-Meteo",
    license: "CC BY 4.0",
    fullText: "Rainfall & Weather © Open-Meteo (CC BY 4.0)",
  },
  {
    id: "openfreemap",
    label: "OpenFreeMap / OpenMapTiles",
    license: "CC BY 4.0 / ODbL",
    fullText:
      "Vector basemap tiles © OpenFreeMap © OpenMapTiles © OpenStreetMap contributors",
  },
  {
    id: "geonames",
    label: "GeoNames AU",
    license: "CC BY 4.0",
    fullText: "Australian place names & coordinates © GeoNames (CC BY 4.0)",
  },
];

export function escapeHtml(input: string): string {
  return input
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
