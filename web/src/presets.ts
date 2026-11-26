import type {
  AttributionItem,
  BasemapId,
  BBoxTuple,
  LayerVisibilityState,
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
export const DEFAULT_MAP_ZOOM = 10.2;

/**
 * Combined VIC + NSW + ACT launch bounding box [west, south, east, north].
 * Adapted from GridForge's verified Australian pack catalog.
 */
export const VIC_NSW_LAUNCH_BBOX: BBoxTuple = [140.95, -39.2, 153.68, -28.15];

/**
 * Curated VIC & NSW regional pack presets and 4WD High Country / escarpment landmarks.
 * Bounding boxes and coordinates adapted from sibling project GridForge.
 */
export const REGION_PRESETS: readonly RegionPreset[] = [
  {
    id: "australian-alps",
    label: "Australian Alps / High Country (VIC/NSW)",
    shortLabel: "Australian Alps",
    group: "region",
    stateTag: "VIC/NSW · 55H",
    utmZones: ["55H"],
    center: [148.125, -36.615],
    zoom: 10.2,
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
    id: "vic",
    label: "Victoria (Statewide)",
    shortLabel: "Victoria",
    group: "region",
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
    stateTag: "NSW/ACT · 54H–56J",
    utmZones: ["54H", "54J", "55H", "55J", "56H", "56J"],
    center: [147.3, -33.0],
    zoom: 6.0,
    bbox: [140.95, -37.55, 153.68, -28.15],
    description:
      "Full NSW & ACT including Blue Mountains, Brindabellas, Snowy Mountains & Outback.",
    estimatedPackHint: "~1.4 GB",
  },
  // Curated VIC/NSW 4WD landmarks
  {
    id: "davies-plain-corridor",
    label: "Davies Plain / Tom Groggin Border (VIC/NSW)",
    shortLabel: "Davies Plain",
    group: "landmark",
    stateTag: "VIC/NSW · 55H",
    utmZones: ["55H"],
    center: [148.125, -36.615],
    zoom: 12.2,
    bbox: [148.05, -36.68, 148.2, -36.55],
    description:
      "Murray River border crossing, steep alpine climbs & high-country huts (600–1,650 m).",
  },
  {
    id: "mt-bogong",
    label: "Mt Bogong / Alpine NP (VIC)",
    shortLabel: "Mt Bogong",
    group: "landmark",
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
    stateTag: "NSW · 55H",
    utmZones: ["55H"],
    center: [148.263, -36.456],
    zoom: 11.5,
    bbox: [148.08, -36.6, 148.45, -36.3],
    description: "Main Range, Alpine Way & Snowy Mountains alpine zone.",
  },
  {
    id: "brindabella-act",
    label: "Brindabella Range / Namadgi (ACT/NSW)",
    shortLabel: "Brindabellas",
    group: "landmark",
    stateTag: "ACT/NSW · 55H",
    utmZones: ["55H"],
    center: [148.88, -35.47],
    zoom: 10.5,
    bbox: [148.74, -35.94, 149.42, -35.1],
    description: "Brindabella fire trails, Coree & Namadgi National Park.",
  },
  {
    id: "grampians",
    label: "Grampians / Gariwerd (VIC)",
    shortLabel: "Grampians",
    group: "landmark",
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
    stateTag: "NSW · 56H",
    utmZones: ["56H"],
    center: [150.193, -35.276],
    zoom: 11.2,
    bbox: [149.95, -35.48, 150.45, -35.08],
    description: "South Coast fire trails, Clyde Valley & Morton National Park.",
  },
];

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
  { id: "osm", label: "OSM", hint: "OpenStreetMap Standard (online)" },
  { id: "satellite", label: "Imagery", hint: "Esri World Imagery (online)" },
  {
    id: "offline-topo",
    label: "Offline Grid",
    hint: "100% offline dark topographic graticule & corridor canvas",
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

export function createInitialLayerState(): LayerVisibilityState {
  return {
    tracks: true,
    steepHighlights: true,
    contoursHillshade: true,
    snowLine: false,
    snowLineElevationM: 1000,
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
];

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
];

export function escapeHtml(input: string): string {
  return input
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
