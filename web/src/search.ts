import { fetchSearch } from "./api";
import { parseSearchCoordinate } from "./coords";
import { REGION_PRESETS, resolvePoiIconName } from "./presets";
import type {
  ApiSearchResponse,
  ApiSearchResultItem,
  UnifiedSearchItem,
} from "./types";

/**
 * Resolve strictly local/offline search candidates:
 * 1. Exact decimal lat,lon or MGRS coordinate via coords.ts
 * 2. Matching built-in VIC/NSW region or 4WD landmark presets
 */
export function resolveLocalSearch(query: string): UnifiedSearchItem[] {
  const trimmed = query.trim();
  if (!trimmed) return [];

  const items: UnifiedSearchItem[] = [];
  const parsed = parseSearchCoordinate(trimmed);

  if (parsed) {
    if (parsed.kind === "mgrs") {
      items.push({
        kind: "mgrs",
        id: `mgrs:${parsed.mgrs}`,
        title: parsed.formatted,
        subtitle: `MGRS ${parsed.gzd} (${parsed.precisionMeters} m) · ${parsed.lat.toFixed(4)}, ${parsed.lon.toFixed(4)}`,
        lat: parsed.lat,
        lon: parsed.lon,
      });
    } else {
      items.push({
        kind: "coordinate",
        id: `coord:${parsed.canonical}`,
        title: parsed.canonical,
        subtitle: "WGS84 Latitude, Longitude",
        lat: parsed.lat,
        lon: parsed.lon,
      });
    }
    return items;
  }

  const lower = trimmed.toLowerCase();
  for (const preset of REGION_PRESETS) {
    if (
      preset.id.toLowerCase().includes(lower) ||
      preset.label.toLowerCase().includes(lower) ||
      preset.shortLabel.toLowerCase().includes(lower)
    ) {
      items.push({
        kind: "preset",
        id: `preset:${preset.id}`,
        title: preset.label,
        subtitle: `${preset.stateTag} · Region preset`,
        lat: preset.center[1],
        lon: preset.center[0],
        bbox: preset.bbox,
        presetId: preset.id,
      });
    }
  }

  return items.slice(0, 4);
}

/**
 * Convert backend `/api/search` items into unified search entries.
 */
export function mapApiSearchResults(
  items: readonly ApiSearchResultItem[]
): UnifiedSearchItem[] {
  return items.map((item) => {
    const kind =
      item.kind === "track" || item.kind === "poi" || item.kind === "park"
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

export interface UnifiedSearchOptions {
  limit?: number;
  signal?: AbortSignal;
  remoteSearch?: (
    query: string,
    limit: number,
    signal?: AbortSignal
  ) => Promise<ApiSearchResponse>;
}

/**
 * Unified Search combining instant offline coordinate/MGRS + preset resolution
 * with live backend `/api/search?q=...` lookup for VIC/NSW tracks, camps, huts,
 * trigs, and CAPAD parks. Falls back gracefully if backend is offline.
 */
export async function runUnifiedSearch(
  query: string,
  options?: UnifiedSearchOptions
): Promise<{
  items: UnifiedSearchItem[];
  remoteError: boolean;
}> {
  const trimmed = query.trim();
  if (!trimmed) return { items: [], remoteError: false };

  const localItems = resolveLocalSearch(trimmed);

  // Exact coordinate/MGRS queries resolve 100% locally without hitting backend search
  const first = localItems[0];
  if (first && (first.kind === "coordinate" || first.kind === "mgrs")) {
    return { items: localItems, remoteError: false };
  }

  const limit = options?.limit ?? 12;
  const searchFn = options?.remoteSearch ?? fetchSearch;

  try {
    const remote = await searchFn(trimmed, limit, options?.signal);
    const mapped = mapApiSearchResults(remote.results);

    // Deduplicate preset matches when a real track/park with the same name exists
    const remoteNames = new Set(mapped.map((m) => m.title.toLowerCase()));
    const remainingLocal = localItems.filter(
      (loc) => !remoteNames.has(loc.title.toLowerCase())
    );

    return {
      items: [...mapped, ...remainingLocal].slice(0, limit),
      remoteError: false,
    };
  } catch (err) {
    if (options?.signal?.aborted) {
      throw err;
    }
    return {
      items: localItems,
      remoteError: true,
    };
  }
}
