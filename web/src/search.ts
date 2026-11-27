import { fetchSearch } from "./api";
import { parseSearchCoordinate } from "./coords";
import { REGION_PRESETS, resolvePoiIconName } from "./presets";
import type {
  ApiSearchResponse,
  ApiSearchResultItem,
  BBoxTuple,
  UnifiedSearchItem,
} from "./types";

export const MIN_REMOTE_SEARCH_LENGTH = 2;
export const SEARCH_CACHE_MAX_ENTRIES = 64;
export const SEARCH_CACHE_TTL_MS = 60_000;
export const AU_PLACES_DATA_URL = "/data/au_places.geojson";

const DEFAULT_SEARCH_LIMIT = 12;
const AU_PLACES_FAILURE_RETRY_MS = 30_000;

export type AuPlaceClass =
  | "state"
  | "region"
  | "capital"
  | "city"
  | "regional_hub"
  | "town"
  | "village"
  | "locality";

export interface AuPlaceEntry {
  id: string;
  name: string;
  place_class: AuPlaceClass | string;
  state: string;
  rank: number;
  minzoom?: number;
  maxzoom?: number | null;
  zoom_target?: number;
  bbox: BBoxTuple | null;
  population?: number | null;
  region_hint?: string | null;
  lat: number;
  lon: number;
}

export interface AuPlaceInput {
  id: string;
  name: string;
  place_class: AuPlaceClass | string;
  state: string;
  rank?: number;
  minzoom?: number;
  maxzoom?: number | null;
  zoom_target?: number;
  bbox?: BBoxTuple | null;
  population?: number | null;
  region_hint?: string | null;
  lat: number;
  lon: number;
}

export interface LocalSearchOptions {
  places?: readonly AuPlaceEntry[];
  maxResults?: number;
}

export interface LoadAuPlacesOptions {
  fetchImpl?: typeof fetch;
  url?: string;
  force?: boolean;
}

export interface UnifiedSearchOptions {
  limit?: number;
  signal?: AbortSignal;
  places?: readonly AuPlaceEntry[];
  bypassCache?: boolean;
  remoteSearch?: (
    query: string,
    limit: number,
    signal?: AbortSignal
  ) => Promise<ApiSearchResponse>;
}

export interface UnifiedSearchResult {
  items: UnifiedSearchItem[];
  remoteError: boolean;
  fromCache?: boolean;
  remoteSkipped?: boolean;
}

interface SearchCacheEntry {
  mappedRemote: UnifiedSearchItem[];
  expiresAt: number;
}

const PLACE_CLASS_DEFAULT_RANK: Record<string, number> = {
  capital: 1,
  city: 2,
  state: 3,
  regional_hub: 4,
  region: 5,
  town: 6,
  village: 7,
  locality: 8,
};

const PLACE_CLASS_LABELS: Record<string, string> = {
  state: "State",
  region: "Region",
  capital: "Capital",
  city: "City",
  regional_hub: "Regional Hub",
  town: "Town",
  village: "Village",
  locality: "Locality",
};

const remoteSearchCache = new Map<string, SearchCacheEntry>();

let cachedAuPlaces: readonly AuPlaceEntry[] | null = null;
let auPlacesLoadPromise: Promise<readonly AuPlaceEntry[]> | null = null;
let auPlacesLoadToken = 0;
let auPlacesLastFailedAt: number | null = null;

/**
 * Normalize query or title text for case-insensitive matching, deduplication,
 * and LRU cache keys.
 */
export function normalizeSearchText(input: string): string {
  return input.trim().toLowerCase().replace(/\s+/g, " ");
}

/**
 * Return true when the input parses cleanly as an exact lat,lon or MGRS coordinate.
 */
export function isExactCoordinateQuery(query: string): boolean {
  const trimmed = query.trim();
  if (!trimmed) return false;
  return parseSearchCoordinate(trimmed) !== null;
}

/**
 * Return true when a query should fire a remote `/api/search` lookup.
 * Skips empty strings, 1-character queries, and exact coordinate/MGRS queries.
 */
export function shouldRunRemoteSearch(query: string): boolean {
  const trimmed = query.trim();
  if (trimmed.length < MIN_REMOTE_SEARCH_LENGTH) return false;
  return !isExactCoordinateQuery(trimmed);
}

function buildSearchCacheKey(query: string, limit: number): string {
  return `${normalizeSearchText(query)}::${limit}`;
}

/**
 * Clear the in-memory client-side LRU response cache (for tests or refresh).
 */
export function clearSearchCache(): void {
  remoteSearchCache.clear();
}

/**
 * Return current entry count in the client-side LRU search cache.
 */
export function getSearchCacheSize(): number {
  return remoteSearchCache.size;
}

function getCachedRemoteItems(
  query: string,
  limit: number,
  nowMs = Date.now()
): UnifiedSearchItem[] | null {
  const key = buildSearchCacheKey(query, limit);
  const entry = remoteSearchCache.get(key);
  if (!entry) return null;

  if (nowMs >= entry.expiresAt) {
    remoteSearchCache.delete(key);
    return null;
  }

  // Refresh LRU recency
  remoteSearchCache.delete(key);
  remoteSearchCache.set(key, entry);
  return entry.mappedRemote;
}

function setCachedRemoteItems(
  query: string,
  limit: number,
  mappedRemote: UnifiedSearchItem[],
  nowMs = Date.now()
): void {
  const key = buildSearchCacheKey(query, limit);
  if (remoteSearchCache.has(key)) {
    remoteSearchCache.delete(key);
  }
  remoteSearchCache.set(key, {
    mappedRemote,
    expiresAt: nowMs + SEARCH_CACHE_TTL_MS,
  });

  while (remoteSearchCache.size > SEARCH_CACHE_MAX_ENTRIES) {
    const oldestKey = remoteSearchCache.keys().next().value;
    if (oldestKey === undefined) break;
    remoteSearchCache.delete(oldestKey);
  }
}

/**
 * Check whether a non-expired cached remote search response exists for the query.
 */
export function hasCachedRemoteSearch(
  query: string,
  limit = DEFAULT_SEARCH_LIMIT,
  nowMs = Date.now()
): boolean {
  if (!shouldRunRemoteSearch(query)) return false;
  const key = buildSearchCacheKey(query, limit);
  const entry = remoteSearchCache.get(key);
  if (!entry) return false;
  if (nowMs >= entry.expiresAt) {
    remoteSearchCache.delete(key);
    return false;
  }
  return true;
}

/**
 * Synchronously resolve merged search results when the remote query is already
 * present in the in-memory client LRU cache (0 ms instant backspacing/re-typing).
 */
export function peekCachedUnifiedSearch(
  query: string,
  options?: {
    limit?: number;
    places?: readonly AuPlaceEntry[];
    nowMs?: number;
  }
): UnifiedSearchItem[] | null {
  const trimmed = query.trim();
  if (!shouldRunRemoteSearch(trimmed)) return null;
  const limit = options?.limit ?? DEFAULT_SEARCH_LIMIT;
  const cachedRemote = getCachedRemoteItems(trimmed, limit, options?.nowMs);
  if (!cachedRemote) return null;
  const localItems = resolveLocalSearch(trimmed, { places: options?.places });
  return mergeUnifiedSearchResults(trimmed, localItems, cachedRemote, limit);
}

function parseBBox(raw: unknown): BBoxTuple | null {
  let candidate: unknown = raw;
  if (typeof candidate === "string") {
    try {
      candidate = JSON.parse(candidate);
    } catch {
      return null;
    }
  }
  if (!Array.isArray(candidate) || candidate.length !== 4) return null;
  const nums = candidate.map((v) => Number(v));
  if (nums.some((n) => !Number.isFinite(n))) return null;
  return [nums[0]!, nums[1]!, nums[2]!, nums[3]!];
}

function resolvePlaceRank(placeClass: string, rawRank?: unknown): number {
  if (typeof rawRank === "number" && Number.isFinite(rawRank) && rawRank > 0) {
    return rawRank;
  }
  const key = placeClass.trim().toLowerCase();
  return PLACE_CLASS_DEFAULT_RANK[key] ?? 8;
}

function normalizeAuPlaceInput(input: AuPlaceInput): AuPlaceEntry {
  const placeClass = input.place_class.trim().toLowerCase() || "town";
  return {
    id: input.id,
    name: input.name.trim(),
    place_class: placeClass,
    state: input.state.trim(),
    rank: resolvePlaceRank(placeClass, input.rank),
    minzoom: input.minzoom,
    maxzoom: input.maxzoom ?? null,
    zoom_target: input.zoom_target,
    bbox: input.bbox ?? null,
    population: input.population ?? null,
    region_hint: input.region_hint?.trim() || null,
    lat: input.lat,
    lon: input.lon,
  };
}

/**
 * Parse `/data/au_places.geojson` RFC 7946 FeatureCollection into normalized entries.
 */
export function parseAuPlacesGeoJson(raw: unknown): AuPlaceEntry[] {
  if (!raw || typeof raw !== "object") return [];
  const fc = raw as { features?: unknown };
  if (!Array.isArray(fc.features)) return [];

  const entries: AuPlaceEntry[] = [];
  for (let i = 0; i < fc.features.length; i++) {
    const feat = fc.features[i] as {
      geometry?: { type?: unknown; coordinates?: unknown };
      properties?: Record<string, unknown>;
    } | null;
    if (!feat || typeof feat !== "object") continue;
    if (feat.geometry?.type !== "Point") continue;
    if (!Array.isArray(feat.geometry.coordinates)) continue;
    const lon = Number(feat.geometry.coordinates[0]);
    const lat = Number(feat.geometry.coordinates[1]);
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue;

    const props = feat.properties ?? {};
    const name = typeof props.name === "string" ? props.name.trim() : "";
    if (!name) continue;

    const placeClass =
      typeof props.place_class === "string" && props.place_class.trim()
        ? props.place_class.trim().toLowerCase()
        : "town";
    const id =
      typeof props.id === "string" && props.id.trim()
        ? props.id.trim()
        : `au-place-${i}`;
    const state = typeof props.state === "string" ? props.state.trim() : "";
    const regionHint =
      typeof props.region_hint === "string" && props.region_hint.trim()
        ? props.region_hint.trim()
        : null;
    const population =
      typeof props.population === "number" && Number.isFinite(props.population)
        ? props.population
        : null;

    entries.push({
      id,
      name,
      place_class: placeClass,
      state,
      rank: resolvePlaceRank(placeClass, props.rank),
      minzoom:
        typeof props.minzoom === "number" && Number.isFinite(props.minzoom)
          ? props.minzoom
          : undefined,
      maxzoom:
        typeof props.maxzoom === "number" && Number.isFinite(props.maxzoom)
          ? props.maxzoom
          : null,
      zoom_target:
        typeof props.zoom_target === "number" &&
        Number.isFinite(props.zoom_target)
          ? props.zoom_target
          : undefined,
      bbox: parseBBox(props.bbox),
      population,
      region_hint: regionHint,
      lat,
      lon,
    });
  }

  return entries;
}

/**
 * Inject or clear the cached Pan-Australia places list (useful for unit tests).
 */
export function setAuPlacesCache(places: readonly AuPlaceInput[] | null): void {
  auPlacesLoadToken++;
  auPlacesLoadPromise = null;
  auPlacesLastFailedAt = null;
  if (places === null) {
    cachedAuPlaces = null;
    return;
  }
  cachedAuPlaces = places.map(normalizeAuPlaceInput);
}

/**
 * Reset the cached Pan-Australia places state so the next load fetches fresh data.
 */
export function resetAuPlacesCache(): void {
  setAuPlacesCache(null);
}

/**
 * Return already-loaded AU places synchronously without triggering network I/O.
 */
export function getLoadedAuPlaces(): readonly AuPlaceEntry[] {
  return cachedAuPlaces ?? [];
}

/**
 * Lazily fetch and cache `/data/au_places.geojson`.
 * Gracefully returns `[]` if unavailable or in Node/Vitest without a fetch mock.
 */
export async function loadAuPlaces(
  options?: LoadAuPlacesOptions
): Promise<readonly AuPlaceEntry[]> {
  if (!options?.force && cachedAuPlaces !== null) {
    return cachedAuPlaces;
  }
  if (!options?.force && auPlacesLoadPromise !== null) {
    return auPlacesLoadPromise;
  }
  if (
    !options?.force &&
    auPlacesLastFailedAt !== null &&
    Date.now() - auPlacesLastFailedAt < AU_PLACES_FAILURE_RETRY_MS
  ) {
    return [];
  }

  const fetcher =
    options?.fetchImpl ?? (typeof fetch === "function" ? fetch : undefined);
  if (!fetcher) {
    auPlacesLastFailedAt = Date.now();
    return [];
  }

  const url = options?.url ?? AU_PLACES_DATA_URL;
  const loadToken = ++auPlacesLoadToken;
  const promise = (async (): Promise<readonly AuPlaceEntry[]> => {
    try {
      const res = await fetcher(url, {
        method: "GET",
        headers: { Accept: "application/geo+json, application/json" },
      });
      if (!res || !res.ok) {
        if (auPlacesLoadToken === loadToken) {
          auPlacesLastFailedAt = Date.now();
        }
        return [];
      }
      const raw: unknown = await res.json();
      const parsed = parseAuPlacesGeoJson(raw);
      if (auPlacesLoadToken === loadToken) {
        cachedAuPlaces = parsed;
        auPlacesLastFailedAt = null;
      }
      return parsed;
    } catch {
      if (auPlacesLoadToken === loadToken) {
        auPlacesLastFailedAt = Date.now();
      }
      return [];
    } finally {
      if (auPlacesLoadToken === loadToken) {
        auPlacesLoadPromise = null;
      }
    }
  })();

  auPlacesLoadPromise = promise;
  return promise;
}

/**
 * Fire-and-forget warm preload of `/data/au_places.geojson` (e.g. on input focus).
 */
export function preloadAuPlaces(): Promise<readonly AuPlaceEntry[]> {
  return loadAuPlaces();
}

function formatPlaceClassLabel(placeClass: string, state: string): string {
  const key = placeClass.trim().toLowerCase();
  if (key === "state") {
    const upperState = state.trim().toUpperCase();
    if (upperState === "ACT" || upperState === "NT") {
      return "Territory";
    }
    return "State";
  }
  if (key in PLACE_CLASS_LABELS) {
    return PLACE_CLASS_LABELS[key]!;
  }
  return key
    .split(/[_\s]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

/**
 * Build a concise subtitle for a local AU place/region item
 * (e.g., `"Town · VIC · Victorian High Country"` or `"Region · VIC/NSW"`).
 */
export function formatAuPlaceSubtitle(
  place: Pick<AuPlaceEntry, "name" | "place_class" | "state" | "region_hint">
): string {
  const parts: string[] = [];
  const classLabel = formatPlaceClassLabel(place.place_class, place.state);
  if (classLabel) parts.push(classLabel);

  const state = place.state.trim();
  if (state) parts.push(state);

  const hint = place.region_hint?.trim() ?? "";
  if (
    hint &&
    normalizeSearchText(hint) !== normalizeSearchText(place.name) &&
    normalizeSearchText(hint) !== normalizeSearchText(state)
  ) {
    parts.push(hint);
  }

  return parts.join(" · ") || "Place";
}

export function auPlaceToSearchItem(place: AuPlaceEntry): UnifiedSearchItem {
  return {
    kind: "place",
    id: place.id,
    title: place.name,
    subtitle: formatAuPlaceSubtitle(place),
    lat: place.lat,
    lon: place.lon,
    bbox: place.bbox ?? null,
    iconKey: null,
  };
}

function hasWordPrefix(text: string, normQuery: string): boolean {
  if (!text || !normQuery) return false;
  const words = text.split(/[\s/\-·,()]+/);
  return words.some((word) => word.startsWith(normQuery));
}

function computeTitleMatchStrength(title: string, normQuery: string): number {
  const normTitle = normalizeSearchText(title);
  if (!normTitle || !normQuery) return 4;
  if (normTitle === normQuery) return 0;
  if (normTitle.startsWith(normQuery)) return 1;
  if (hasWordPrefix(normTitle, normQuery)) return 2;
  if (normTitle.includes(normQuery)) return 3;
  return 4;
}

function scoreAuPlaceMatch(place: AuPlaceEntry, normQuery: string): number {
  const nameNorm = normalizeSearchText(place.name);
  if (!nameNorm) return -1;

  // Keep 1-char suggestions strictly name-prefix so results stay focused
  if (normQuery.length < MIN_REMOTE_SEARCH_LENGTH) {
    return nameNorm.startsWith(normQuery) ? 2 : -1;
  }

  const stateNorm = normalizeSearchText(place.state);
  const hintNorm = normalizeSearchText(place.region_hint ?? "");
  const nameWithState = stateNorm ? `${nameNorm} ${stateNorm}` : nameNorm;

  if (nameNorm === normQuery) return 0;
  if (nameWithState === normQuery) return 1;
  if (nameNorm.startsWith(normQuery)) return 2;
  if (hasWordPrefix(nameNorm, normQuery)) return 3;
  if (nameNorm.includes(normQuery)) return 4;

  const tokens = normQuery.split(" ").filter(Boolean);
  if (tokens.length > 1) {
    const combined = `${nameNorm} ${stateNorm} ${hintNorm}`.trim();
    if (tokens.every((token) => combined.includes(token))) {
      const hasNameHit = tokens.some((token) => nameNorm.includes(token));
      return hasNameHit ? 5 : 6;
    }
  }

  if (hintNorm) {
    if (hintNorm.startsWith(normQuery) || hasWordPrefix(hintNorm, normQuery)) {
      return 6;
    }
    if (hintNorm.includes(normQuery)) {
      return 7;
    }
  }

  if (stateNorm) {
    const stateParts = stateNorm.split("/").filter(Boolean);
    if (stateNorm === normQuery || stateParts.includes(normQuery)) {
      return 8;
    }
  }

  return -1;
}

interface ScoredLocalCandidate {
  bucket: number;
  tierRank: number;
  population: number;
  titleLen: number;
  item: UnifiedSearchItem;
}

function scoreAuPlacesInternal(
  query: string,
  places: readonly AuPlaceEntry[],
  limit: number
): ScoredLocalCandidate[] {
  const normQuery = normalizeSearchText(query);
  if (!normQuery || places.length === 0) return [];

  const scored: ScoredLocalCandidate[] = [];
  for (const place of places) {
    const bucket = scoreAuPlaceMatch(place, normQuery);
    if (bucket < 0) continue;
    scored.push({
      bucket,
      tierRank: resolvePlaceRank(place.place_class, place.rank),
      population: place.population ?? 0,
      titleLen: place.name.length,
      item: auPlaceToSearchItem(place),
    });
  }

  scored.sort((a, b) => {
    if (a.bucket !== b.bucket) return a.bucket - b.bucket;
    if (a.tierRank !== b.tierRank) return a.tierRank - b.tierRank;
    if (a.population !== b.population) return b.population - a.population;
    if (a.titleLen !== b.titleLen) return a.titleLen - b.titleLen;
    return a.item.title.localeCompare(b.item.title);
  });

  return scored.slice(0, limit);
}

/**
 * Match query offline against Pan-Australia places & regions (`name`, `state`,
 * `region_hint`), ranking exact > prefix > substring and higher-tier places
 * (capital/city/state/regional_hub/region/town/village/locality) first.
 */
export function matchAuPlaces(
  query: string,
  places?: readonly AuPlaceEntry[],
  limit = 6
): UnifiedSearchItem[] {
  const source = places ?? cachedAuPlaces ?? [];
  return scoreAuPlacesInternal(query, source, limit).map((entry) => entry.item);
}

function scorePresetMatches(
  query: string,
  limit = 4
): ScoredLocalCandidate[] {
  const normQuery = normalizeSearchText(query);
  if (!normQuery) return [];

  const scored: ScoredLocalCandidate[] = [];
  for (const preset of REGION_PRESETS) {
    const idNorm = normalizeSearchText(preset.id.replace(/-/g, " "));
    const rawIdNorm = normalizeSearchText(preset.id);
    const labelNorm = normalizeSearchText(preset.label);
    const shortNorm = normalizeSearchText(preset.shortLabel);

    let bucket = -1;
    if (normQuery.length < MIN_REMOTE_SEARCH_LENGTH) {
      if (shortNorm.startsWith(normQuery) || labelNorm.startsWith(normQuery)) {
        bucket = 2;
      }
    } else if (
      shortNorm === normQuery ||
      labelNorm === normQuery ||
      idNorm === normQuery ||
      rawIdNorm === normQuery
    ) {
      bucket = 0;
    } else if (
      shortNorm.startsWith(normQuery) ||
      labelNorm.startsWith(normQuery) ||
      idNorm.startsWith(normQuery) ||
      rawIdNorm.startsWith(normQuery)
    ) {
      bucket = 2;
    } else if (
      hasWordPrefix(shortNorm, normQuery) ||
      hasWordPrefix(labelNorm, normQuery)
    ) {
      bucket = 3;
    } else if (
      labelNorm.includes(normQuery) ||
      shortNorm.includes(normQuery) ||
      rawIdNorm.includes(normQuery) ||
      idNorm.includes(normQuery)
    ) {
      bucket = 4;
    }

    if (bucket < 0) continue;

    scored.push({
      bucket,
      tierRank: preset.packCategory === "state" ? 5.2 : 5.5,
      population: 0,
      titleLen: preset.label.length,
      item: {
        kind: "preset",
        id: `preset:${preset.id}`,
        title: preset.label,
        subtitle: `${preset.stateTag} · Region preset`,
        lat: preset.center[1],
        lon: preset.center[0],
        bbox: preset.bbox,
        presetId: preset.id,
      },
    });
  }

  scored.sort((a, b) => {
    if (a.bucket !== b.bucket) return a.bucket - b.bucket;
    if (a.tierRank !== b.tierRank) return a.tierRank - b.tierRank;
    if (a.titleLen !== b.titleLen) return a.titleLen - b.titleLen;
    return a.item.title.localeCompare(b.item.title);
  });

  return scored.slice(0, limit);
}

/**
 * Resolve strictly local/offline search candidates:
 * 1. Exact decimal lat,lon or MGRS coordinate via coords.ts
 * 2. Matching built-in Pan-Australia region/landmark presets + offline AU places/regions
 */
export function resolveLocalSearch(
  query: string,
  options?: LocalSearchOptions
): UnifiedSearchItem[] {
  const trimmed = query.trim();
  if (!trimmed) return [];

  const parsed = parseSearchCoordinate(trimmed);
  if (parsed) {
    if (parsed.kind === "mgrs") {
      return [
        {
          kind: "mgrs",
          id: `mgrs:${parsed.mgrs}`,
          title: parsed.formatted,
          subtitle: `MGRS ${parsed.gzd} (${parsed.precisionMeters} m) · ${parsed.lat.toFixed(4)}, ${parsed.lon.toFixed(4)}`,
          lat: parsed.lat,
          lon: parsed.lon,
        },
      ];
    }
    return [
      {
        kind: "coordinate",
        id: `coord:${parsed.canonical}`,
        title: parsed.canonical,
        subtitle: "WGS84 Latitude, Longitude",
        lat: parsed.lat,
        lon: parsed.lon,
      },
    ];
  }

  const maxResults = options?.maxResults ?? 6;
  const placesSource = options?.places ?? cachedAuPlaces ?? [];
  const placeMatches = scoreAuPlacesInternal(trimmed, placesSource, maxResults);
  const presetMatches = scorePresetMatches(trimmed, Math.min(4, maxResults));

  const combined = [...placeMatches, ...presetMatches].sort((a, b) => {
    if (a.bucket !== b.bucket) return a.bucket - b.bucket;
    if (a.tierRank !== b.tierRank) return a.tierRank - b.tierRank;
    if (a.population !== b.population) return b.population - a.population;
    if (a.titleLen !== b.titleLen) return a.titleLen - b.titleLen;
    return a.item.title.localeCompare(b.item.title);
  });

  const seen = new Set<string>();
  const items: UnifiedSearchItem[] = [];
  for (const candidate of combined) {
    const key = `${normalizeSearchText(candidate.item.title)}::${candidate.item.kind}`;
    if (seen.has(key)) continue;
    seen.add(key);
    items.push(candidate.item);
    if (items.length >= maxResults) break;
  }

  return items;
}

/**
 * Convert backend `/api/search` items into unified search entries.
 */
export function mapApiSearchResults(
  items: readonly ApiSearchResultItem[]
): UnifiedSearchItem[] {
  return items.map((item) => {
    const kind =
      item.kind === "track" ||
      item.kind === "poi" ||
      item.kind === "park" ||
      item.kind === "place"
        ? item.kind
        : "poi";

    const parts: string[] = [];
    if (item.subtitle) parts.push(item.subtitle);
    else if (item.category) parts.push(item.category.replace(/_/g, " "));
    if (item.state) parts.push(item.state);
    if (typeof item.elev_m === "number" && Number.isFinite(item.elev_m)) {
      parts.push(`${Math.round(item.elev_m)} m`);
    }

    return {
      kind,
      id: item.id,
      title: item.name,
      subtitle: parts.join(" · ") || kind.toUpperCase(),
      lat: typeof item.lat === "number" ? item.lat : null,
      lon: typeof item.lon === "number" ? item.lon : null,
      bbox: item.bbox ?? null,
      grade: item.grade ?? null,
      iconKey:
        kind === "poi"
          ? resolvePoiIconName(item.category ?? item.subtitle)
          : null,
    };
  });
}

function dedupKey(item: UnifiedSearchItem): string {
  return `${normalizeSearchText(item.title)}::${item.kind}`;
}

/**
 * Cleanly merge instant local matches (presets + AU places) with remote `/api/search`
 * results:
 * - Deduplicates by normalized `name + kind` (preferring curated local AU place over
 *   a duplicate Nominatim place with the same name, and dropping presets shadowed by
 *   an exact-title remote track/park/POI).
 * - Prioritizes exact and strong baked tracks first while keeping top matching local
 *   AU towns/regions immediately visible within the result budget.
 */
export function mergeUnifiedSearchResults(
  query: string,
  localItems: readonly UnifiedSearchItem[],
  remoteItems: readonly UnifiedSearchItem[],
  limit = DEFAULT_SEARCH_LIMIT
): UnifiedSearchItem[] {
  const normQ = normalizeSearchText(query);
  const localPlaceTitles = new Set<string>();
  for (const item of localItems) {
    if (item.kind === "place") {
      localPlaceTitles.add(normalizeSearchText(item.title));
    }
  }

  const remoteCoreTitles = new Set<string>();
  const dedupedRemote: UnifiedSearchItem[] = [];
  const seenRemote = new Set<string>();

  for (const item of remoteItems) {
    const normTitle = normalizeSearchText(item.title);
    const key = `${normTitle}::${item.kind}`;
    if (seenRemote.has(key)) continue;
    // Prefer curated offline AU place metadata when remote Nominatim returns the same place
    if (item.kind === "place" && localPlaceTitles.has(normTitle)) continue;
    seenRemote.add(key);
    dedupedRemote.push(item);
    if (item.kind === "track" || item.kind === "park" || item.kind === "poi") {
      remoteCoreTitles.add(normTitle);
    }
  }

  const dedupedLocal: UnifiedSearchItem[] = [];
  const seenLocal = new Set<string>();
  for (const item of localItems) {
    const normTitle = normalizeSearchText(item.title);
    const key = `${normTitle}::${item.kind}`;
    if (seenLocal.has(key) || seenRemote.has(key)) continue;
    if (item.kind === "preset" && remoteCoreTitles.has(normTitle)) continue;
    seenLocal.add(key);
    dedupedLocal.push(item);
  }

  const exactTracks: UnifiedSearchItem[] = [];
  const exactOther: UnifiedSearchItem[] = [];
  const strongTracks: UnifiedSearchItem[] = [];
  const topLocalPlaces: UnifiedSearchItem[] = [];
  const remainingRemote: UnifiedSearchItem[] = [];
  const remainingLocal: UnifiedSearchItem[] = [];

  for (const item of dedupedRemote) {
    const strength = computeTitleMatchStrength(item.title, normQ);
    if (item.kind === "track" && strength === 0) {
      exactTracks.push(item);
    } else if (strength === 0) {
      exactOther.push(item);
    } else if (item.kind === "track" && strength <= 2) {
      strongTracks.push(item);
    } else {
      remainingRemote.push(item);
    }
  }

  for (const item of dedupedLocal) {
    const strength = computeTitleMatchStrength(item.title, normQ);
    if (item.kind === "place" && strength === 0) {
      exactOther.push(item);
    } else if (item.kind === "place" && strength <= 1) {
      topLocalPlaces.push(item);
    } else {
      remainingLocal.push(item);
    }
  }

  const merged: UnifiedSearchItem[] = [];
  const seenFinal = new Set<string>();
  const pushUnique = (item: UnifiedSearchItem): boolean => {
    if (merged.length >= limit) return false;
    const key = dedupKey(item);
    if (seenFinal.has(key)) return false;
    seenFinal.add(key);
    merged.push(item);
    return true;
  };

  for (const item of exactTracks) pushUnique(item);
  for (const item of exactOther) pushUnique(item);

  // Reserve up to 2 slots for top matching AU towns/regions when many prefix tracks match
  const reserveSlots =
    limit >= 5
      ? Math.min(2, topLocalPlaces.length)
      : limit >= 3
        ? Math.min(1, topLocalPlaces.length)
        : 0;

  const trackBudget = Math.max(0, limit - merged.length - reserveSlots);
  let strongIdx = 0;
  while (strongIdx < strongTracks.length && strongIdx < trackBudget) {
    pushUnique(strongTracks[strongIdx]!);
    strongIdx++;
  }

  for (const item of topLocalPlaces) {
    if (merged.length >= limit) break;
    pushUnique(item);
  }

  while (strongIdx < strongTracks.length && merged.length < limit) {
    pushUnique(strongTracks[strongIdx]!);
    strongIdx++;
  }

  for (const item of remainingRemote) {
    if (merged.length >= limit) break;
    pushUnique(item);
  }

  for (const item of remainingLocal) {
    if (merged.length >= limit) break;
    pushUnique(item);
  }

  return merged;
}

/**
 * Unified Search combining instant offline coordinate/MGRS + Pan-Australia
 * places/regions + presets with cached live backend `/api/search?q=...` lookup
 * for tracks, camps, huts, trigs, and CAPAD parks. Falls back gracefully if
 * backend is offline.
 */
export async function runUnifiedSearch(
  query: string,
  options?: UnifiedSearchOptions
): Promise<UnifiedSearchResult> {
  const trimmed = query.trim();
  if (!trimmed) {
    return {
      items: [],
      remoteError: false,
      fromCache: false,
      remoteSkipped: true,
    };
  }

  // Exact coordinate/MGRS queries resolve 100% locally without hitting places or backend
  if (isExactCoordinateQuery(trimmed)) {
    return {
      items: resolveLocalSearch(trimmed, { places: [] }),
      remoteError: false,
      fromCache: false,
      remoteSkipped: true,
    };
  }

  const limit = options?.limit ?? DEFAULT_SEARCH_LIMIT;
  const places =
    options?.places ??
    (cachedAuPlaces !== null ? cachedAuPlaces : await loadAuPlaces());
  const localItems = resolveLocalSearch(trimmed, { places });

  // Avoid firing remote `/api/search` on 1-character queries
  if (trimmed.length < MIN_REMOTE_SEARCH_LENGTH) {
    return {
      items: localItems.slice(0, limit),
      remoteError: false,
      fromCache: false,
      remoteSkipped: true,
    };
  }

  if (!options?.bypassCache) {
    const cachedRemote = getCachedRemoteItems(trimmed, limit);
    if (cachedRemote !== null) {
      return {
        items: mergeUnifiedSearchResults(trimmed, localItems, cachedRemote, limit),
        remoteError: false,
        fromCache: true,
        remoteSkipped: false,
      };
    }
  }

  const searchFn = options?.remoteSearch ?? fetchSearch;

  try {
    const remote = await searchFn(trimmed, limit, options?.signal);
    if (options?.signal?.aborted) {
      throw options.signal.reason ?? new Error("Search aborted");
    }
    const mapped = mapApiSearchResults(remote.results);
    setCachedRemoteItems(trimmed, limit, mapped);

    return {
      items: mergeUnifiedSearchResults(trimmed, localItems, mapped, limit),
      remoteError: false,
      fromCache: false,
      remoteSkipped: false,
    };
  } catch (err) {
    if (options?.signal?.aborted) {
      throw err;
    }
    return {
      items: localItems.slice(0, limit),
      remoteError: true,
      fromCache: false,
      remoteSkipped: false,
    };
  }
}
