import type {
  AuthUser,
  LayerKey,
  LayerVisibilityMap,
  LayerVisibilityState,
  PublicUiConfigResponse,
  UserUiPreferencesResponse,
} from "./types";
import type { StorageLike } from "./sidebarHelpers";

export interface LayerDefinition {
  key: LayerKey;
  label: string;
  shortLabel: string;
  hint: string;
}

/**
 * Canonical default ordering for the left sidebar Map Layers list.
 */
export const CANONICAL_LAYER_ORDER: readonly LayerKey[] = [
  "contoursHillshade",
  "parksWaterways",
  "pois",
  "tracks",
  "steepHighlights",
  "hotspots",
  "closures",
  "reports",
  "unmapped",
  "snowLine",
] as const;

export const CANONICAL_LAYER_DEFINITIONS: readonly LayerDefinition[] = [
  {
    key: "contoursHillshade",
    label: "Contours & Hillshade",
    shortLabel: "Contours & Hillshade",
    hint: "20 m minor / 100 m index topo contours + Terrain-RGB shaded relief",
  },
  {
    key: "parksWaterways",
    label: "Parks & Waterways",
    shortLabel: "Parks & Waterways",
    hint: "CAPAD 2025 reserve boundaries & GA/OSM perennial/intermittent streams",
  },
  {
    key: "pois",
    label: "Outdoor POIs",
    shortLabel: "Outdoor POIs",
    hint: "Campgrounds, alpine huts, trigs, lookouts & access gates",
  },
  {
    key: "tracks",
    label: "4WD Tracks (Graded)",
    shortLabel: "4WD Tracks",
    hint: "Canonical 50–100 m segments colored Easy → Extreme (click to inspect)",
  },
  {
    key: "steepHighlights",
    label: "Steep Climbs (≥14°)",
    shortLabel: "Steep Climbs",
    hint: "Highlighted sustained & max gradient hazard pitches",
  },
  {
    key: "hotspots",
    label: "Obstacle Hotspots",
    shortLabel: "Obstacle Hotspots",
    hint: "Crowdsourced slow-crawl & high-roughness terrain pinch points",
  },
  {
    key: "closures",
    label: "Closures & Gates",
    shortLabel: "Closures & Gates",
    hint: "Official AU state seasonal closures, fire/park alerts & locked gates",
  },
  {
    key: "reports",
    label: "Hazard & Gate Reports",
    shortLabel: "Hazard & Gate Reports",
    hint: "Community trail condition reports & distinct locked-gate warnings",
  },
  {
    key: "unmapped",
    label: "Unmapped Track Spurs",
    shortLabel: "Unmapped Track Spurs",
    hint: "Anonymized off-network GPS traces discovered outside canonical roads",
  },
  {
    key: "snowLine",
    label: "Alpine Snow-Line Band",
    shortLabel: "Alpine Snow-Line Band",
    hint: "Configurable elevation highlight (>1,000 m default)",
  },
] as const;

export const LAYER_DEFINITION_MAP: Readonly<Record<LayerKey, LayerDefinition>> =
  Object.fromEntries(
    CANONICAL_LAYER_DEFINITIONS.map((def) => [def.key, def])
  ) as Readonly<Record<LayerKey, LayerDefinition>>;

const CANONICAL_KEY_SET: ReadonlySet<string> = new Set(CANONICAL_LAYER_ORDER);

export const DEFAULT_LAYER_VISIBILITY: Readonly<LayerVisibilityMap> = {
  contoursHillshade: true,
  parksWaterways: true,
  pois: true,
  tracks: true,
  steepHighlights: true,
  hotspots: true,
  closures: true,
  reports: true,
  unmapped: false,
  snowLine: false,
};

export const DEFAULT_PUBLIC_UI_CONFIG: PublicUiConfigResponse = {
  default_layer_order: [...CANONICAL_LAYER_ORDER],
  default_layer_visibility: { ...DEFAULT_LAYER_VISIBILITY },
  show_stack_telemetry_public: false,
};

export const USER_LAYER_PREFS_STORAGE_KEY = "trailforge.user_layer_prefs_v1";

export interface StoredUserLayerPrefs {
  layerOrder: LayerKey[] | null;
  layerVisibility: Partial<LayerVisibilityMap> | null;
}

export interface StorageWithRemove extends StorageLike {
  removeItem?(key: string): void;
}

function getDefaultStorage(): StorageWithRemove | null {
  try {
    if (typeof window !== "undefined" && window.localStorage) {
      return window.localStorage;
    }
  } catch (err) {
    console.warn("Unable to access browser localStorage for layer preferences:", err);
  }
  return null;
}

export function isCanonicalLayerKey(value: unknown): value is LayerKey {
  return typeof value === "string" && CANONICAL_KEY_SET.has(value);
}

/**
 * Normalize an arbitrary layer-order list so it contains all 10 canonical keys
 * with no duplicates and no unknown keys.
 */
export function normalizeLayerOrder(
  raw: readonly unknown[] | null | undefined,
  fallback: readonly LayerKey[] = CANONICAL_LAYER_ORDER
): LayerKey[] {
  const seen = new Set<LayerKey>();
  const out: LayerKey[] = [];
  if (Array.isArray(raw)) {
    for (const item of raw) {
      if (isCanonicalLayerKey(item) && !seen.has(item)) {
        seen.add(item);
        out.push(item);
      }
    }
  }
  for (const key of fallback) {
    if (isCanonicalLayerKey(key) && !seen.has(key)) {
      seen.add(key);
      out.push(key);
    }
  }
  for (const key of CANONICAL_LAYER_ORDER) {
    if (!seen.has(key)) {
      seen.add(key);
      out.push(key);
    }
  }
  return out;
}

/**
 * Normalize a full layer visibility map against a fallback visibility map.
 */
export function normalizeLayerVisibility(
  raw: Record<string, unknown> | null | undefined,
  fallback: Readonly<LayerVisibilityMap> = DEFAULT_LAYER_VISIBILITY
): LayerVisibilityMap {
  const out: LayerVisibilityMap = { ...DEFAULT_LAYER_VISIBILITY, ...fallback };
  if (raw && typeof raw === "object") {
    for (const [k, v] of Object.entries(raw)) {
      if (isCanonicalLayerKey(k) && typeof v === "boolean") {
        out[k] = v;
      }
    }
  }
  return out;
}

/**
 * Extract only valid boolean visibility overrides (returns null when empty).
 */
export function normalizePartialLayerVisibility(
  raw: Record<string, unknown> | null | undefined
): Partial<LayerVisibilityMap> | null {
  if (!raw || typeof raw !== "object") return null;
  const out: Partial<LayerVisibilityMap> = {};
  let count = 0;
  for (const [k, v] of Object.entries(raw)) {
    if (isCanonicalLayerKey(k) && typeof v === "boolean") {
      out[k] = v;
      count += 1;
    }
  }
  return count > 0 ? out : null;
}

export function normalizePublicUiConfig(raw: unknown): PublicUiConfigResponse {
  if (!raw || typeof raw !== "object") {
    return {
      default_layer_order: [...CANONICAL_LAYER_ORDER],
      default_layer_visibility: { ...DEFAULT_LAYER_VISIBILITY },
      show_stack_telemetry_public: false,
    };
  }
  const obj = raw as Record<string, unknown>;
  return {
    default_layer_order: normalizeLayerOrder(
      Array.isArray(obj.default_layer_order) ? obj.default_layer_order : null
    ),
    default_layer_visibility: normalizeLayerVisibility(
      obj.default_layer_visibility && typeof obj.default_layer_visibility === "object"
        ? (obj.default_layer_visibility as Record<string, unknown>)
        : null
    ),
    show_stack_telemetry_public:
      typeof obj.show_stack_telemetry_public === "boolean"
        ? obj.show_stack_telemetry_public
        : false,
  };
}

export function normalizeUserUiPreferences(
  raw: unknown
): UserUiPreferencesResponse {
  if (!raw || typeof raw !== "object") {
    return {
      layer_order: null,
      layer_visibility: null,
      has_custom_preferences: false,
    };
  }
  const obj = raw as Record<string, unknown>;
  const hasRawOrder =
    Array.isArray(obj.layer_order) &&
    obj.layer_order.some((item) => isCanonicalLayerKey(item));
  const layer_order = hasRawOrder
    ? normalizeLayerOrder(obj.layer_order as unknown[])
    : null;
  const layer_visibility = normalizePartialLayerVisibility(
    obj.layer_visibility && typeof obj.layer_visibility === "object"
      ? (obj.layer_visibility as Record<string, unknown>)
      : null
  );
  return {
    layer_order,
    layer_visibility,
    has_custom_preferences: Boolean(
      layer_order !== null || layer_visibility !== null
    ),
  };
}

/**
 * Move a single layer key up or down by one slot in a copy of the order array.
 */
export function moveLayerInOrder(
  order: readonly LayerKey[],
  key: LayerKey,
  direction: "up" | "down"
): LayerKey[] {
  const list = normalizeLayerOrder(order);
  const idx = list.indexOf(key);
  if (idx < 0) return list;
  const target = direction === "up" ? idx - 1 : idx + 1;
  if (target < 0 || target >= list.length) return list;
  const copy = [...list];
  const [moved] = copy.splice(idx, 1);
  copy.splice(target, 0, moved!);
  return copy;
}

/**
 * Move `sourceKey` to the current index of `targetKey` (for drag-and-drop reordering).
 */
export function reorderLayerKeys(
  order: readonly LayerKey[],
  sourceKey: LayerKey,
  targetKey: LayerKey
): LayerKey[] {
  const list = normalizeLayerOrder(order);
  if (sourceKey === targetKey) return list;
  const fromIdx = list.indexOf(sourceKey);
  const toIdx = list.indexOf(targetKey);
  if (fromIdx < 0 || toIdx < 0) return list;
  const copy = [...list];
  const [moved] = copy.splice(fromIdx, 1);
  copy.splice(toIdx, 0, moved!);
  return copy;
}

/**
 * Extract only the 10 boolean layer keys from a full `LayerVisibilityState`.
 */
export function extractLayerVisibilityMap(
  state: LayerVisibilityState
): LayerVisibilityMap {
  return {
    contoursHillshade: Boolean(state.contoursHillshade),
    parksWaterways: Boolean(state.parksWaterways),
    pois: Boolean(state.pois),
    tracks: Boolean(state.tracks),
    steepHighlights: Boolean(state.steepHighlights),
    hotspots: Boolean(state.hotspots),
    closures: Boolean(state.closures),
    reports: Boolean(state.reports),
    unmapped: Boolean(state.unmapped),
    snowLine: Boolean(state.snowLine),
  };
}

/**
 * Resolve effective sidebar layer order & visibility from:
 * 1. Instance defaults (admin DB config -> factory defaults)
 * 2. User preferences (account or localStorage overrides)
 */
export function resolveEffectiveLayerPreferences(
  instanceDefaults: PublicUiConfigResponse,
  userPrefs: StoredUserLayerPrefs | null | undefined,
  snowLineElevationM: number
): {
  layerOrder: LayerKey[];
  layerState: LayerVisibilityState;
  hasCustomPreferences: boolean;
} {
  const baseOrder = normalizeLayerOrder(instanceDefaults.default_layer_order);
  const baseVis = normalizeLayerVisibility(
    instanceDefaults.default_layer_visibility
  );

  const customOrder =
    userPrefs?.layerOrder && userPrefs.layerOrder.length > 0
      ? normalizeLayerOrder(userPrefs.layerOrder, baseOrder)
      : null;
  const customVis = normalizePartialLayerVisibility(
    userPrefs?.layerVisibility as Record<string, unknown> | null | undefined
  );

  const effectiveVis: LayerVisibilityMap = customVis
    ? { ...baseVis, ...customVis }
    : { ...baseVis };

  return {
    layerOrder: customOrder ?? baseOrder,
    layerState: {
      ...effectiveVis,
      snowLineElevationM,
    },
    hasCustomPreferences: Boolean(customOrder !== null || customVis !== null),
  };
}

/**
 * Stack & Offline Telemetry section visibility rule:
 * - Always visible for signed-in active members and admins.
 * - For anonymous / signed-out visitors, hidden by default unless enabled by hoster/admin.
 */
export function shouldShowStackTelemetrySection(
  user: Pick<AuthUser, "role" | "is_active"> | null | undefined,
  showStackTelemetryPublic: boolean
): boolean {
  if (
    user &&
    user.is_active !== false &&
    (user.role === "admin" || user.role === "member")
  ) {
    return true;
  }
  return Boolean(showStackTelemetryPublic);
}

export function loadStoredUserLayerPrefs(
  storage: StorageLike | null = getDefaultStorage()
): StoredUserLayerPrefs {
  if (!storage) {
    return { layerOrder: null, layerVisibility: null };
  }
  try {
    const raw = storage.getItem(USER_LAYER_PREFS_STORAGE_KEY);
    if (!raw || !raw.trim()) {
      return { layerOrder: null, layerVisibility: null };
    }
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const hasOrder =
      Array.isArray(parsed.layerOrder) &&
      parsed.layerOrder.some((item) => isCanonicalLayerKey(item));
    const layerOrder = hasOrder
      ? normalizeLayerOrder(parsed.layerOrder as unknown[])
      : null;
    const layerVisibility = normalizePartialLayerVisibility(
      parsed.layerVisibility && typeof parsed.layerVisibility === "object"
        ? (parsed.layerVisibility as Record<string, unknown>)
        : null
    );
    return { layerOrder, layerVisibility };
  } catch {
    return { layerOrder: null, layerVisibility: null };
  }
}

export function saveStoredUserLayerPrefs(
  prefs: StoredUserLayerPrefs,
  storage: StorageWithRemove | null = getDefaultStorage()
): void {
  if (!storage) return;
  try {
    const cleanOrder =
      prefs.layerOrder && prefs.layerOrder.length > 0
        ? normalizeLayerOrder(prefs.layerOrder)
        : null;
    const cleanVis = normalizePartialLayerVisibility(
      prefs.layerVisibility as Record<string, unknown> | null | undefined
    );
    if (!cleanOrder && !cleanVis) {
      if (typeof storage.removeItem === "function") {
        storage.removeItem(USER_LAYER_PREFS_STORAGE_KEY);
      } else {
        storage.setItem(USER_LAYER_PREFS_STORAGE_KEY, "");
      }
      return;
    }
    storage.setItem(
      USER_LAYER_PREFS_STORAGE_KEY,
      JSON.stringify({
        layerOrder: cleanOrder,
        layerVisibility: cleanVis,
      })
    );
  } catch (err) {
    console.warn("Unable to save map layer preferences to localStorage:", err);
  }
}

export function clearStoredUserLayerPrefs(
  storage: StorageWithRemove | null = getDefaultStorage()
): void {
  if (!storage) return;
  try {
    if (typeof storage.removeItem === "function") {
      storage.removeItem(USER_LAYER_PREFS_STORAGE_KEY);
    } else {
      storage.setItem(USER_LAYER_PREFS_STORAGE_KEY, "");
    }
  } catch (err) {
    console.warn("Unable to clear map layer preferences from localStorage:", err);
  }
}
