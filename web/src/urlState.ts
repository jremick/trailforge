import { getPresetById } from "./presets";
import type { VehicleCapabilityClass } from "./types";

export interface AppUrlState {
  trackId: string | null;
  regionId: string | null;
  lat: number | null;
  lon: number | null;
  zoom: number | null;
  profileOpen: boolean;
  /** Ordered canonical track IDs for `?route=id1,id2,...` Route Planner deep links. */
  routeTrackIds?: string[];
  /** Canonical vehicle class for `?rig=<vehicle_class>` deep links. */
  rigClass?: VehicleCapabilityClass | null;
}

export interface ParsedAppUrlState extends AppUrlState {
  routeTrackIds: string[];
  rigClass: VehicleCapabilityClass | null;
}

export const MAX_ROUTE_URL_TRACKS = 30;

const TRACK_ID_PATTERN = /^[A-Za-z0-9_.:-]{1,120}$/;

const CANONICAL_RIG_MAP: Readonly<Record<string, VehicleCapabilityClass>> = {
  awd: "AWD",
  "stock 4wd": "Stock 4WD",
  "touring 4wd": "Touring 4WD",
  "modified 4wd": "Modified 4WD",
};

function parseFinite(raw: string | null, min: number, max: number): number | null {
  if (raw === null || raw.trim() === "") return null;
  const val = Number(raw);
  if (!Number.isFinite(val) || val < min || val > max) return null;
  return val;
}

/**
 * Validate and normalize a `?rig=` query parameter into one of TrailForge's
 * 4 canonical `VehicleCapabilityClass` values (`AWD`, `Stock 4WD`, `Touring 4WD`,
 * `Modified 4WD`), accepting space/underscore/hyphen separators case-insensitively.
 */
export function parseVehicleClassParam(
  raw: string | null | undefined
): VehicleCapabilityClass | null {
  if (!raw) return null;
  const normalized = raw
    .trim()
    .toLowerCase()
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ");
  return CANONICAL_RIG_MAP[normalized] ?? null;
}

/**
 * Validate and normalize ordered track IDs from `?route=id1,id2,...` (or repeated
 * `?route=id1&route=id2` params), rejecting malformed IDs and capping at 30 legs.
 */
export function parseRouteTrackIdsParam(
  rawValues: readonly string[]
): string[] {
  const result: string[] = [];
  for (const raw of rawValues) {
    if (!raw) continue;
    for (const part of raw.split(",")) {
      const candidate = part.trim();
      if (!candidate || !TRACK_ID_PATTERN.test(candidate)) continue;
      result.push(candidate);
      if (result.length >= MAX_ROUTE_URL_TRACKS) {
        return result;
      }
    }
  }
  return result;
}

/**
 * Parse bookmarkable/shareable TrailForge deep-link state from `window.location.search`.
 * Supports `?track=<id>&route=<id1,id2,...>&rig=<vehicle_class>&region=<preset>&lat=<deg>&lon=<deg>&z=<zoom>&profile=1`.
 */
export function parseAppUrlState(search: string): ParsedAppUrlState {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);

  const rawTrack = params.get("track")?.trim() ?? "";
  const trackId = rawTrack && TRACK_ID_PATTERN.test(rawTrack) ? rawTrack : null;

  const routeTrackIds = parseRouteTrackIdsParam(params.getAll("route"));
  const rigClass = parseVehicleClassParam(params.get("rig"));

  const rawRegion = params.get("region")?.trim() ?? "";
  const regionId = rawRegion && getPresetById(rawRegion) ? rawRegion : null;

  const parsedLat = parseFinite(params.get("lat"), -90, 90);
  const parsedLon = parseFinite(params.get("lon"), -180, 180);
  const hasCoords = parsedLat !== null && parsedLon !== null;

  const zoom = parseFinite(params.get("z"), 2, 20);
  const rawProfile = params.get("profile")?.trim().toLowerCase() ?? "";
  const profileOpen = Boolean(trackId) && (rawProfile === "1" || rawProfile === "true");

  return {
    trackId,
    regionId,
    lat: hasCoords ? parsedLat : null,
    lon: hasCoords ? parsedLon : null,
    zoom: hasCoords ? zoom : null,
    profileOpen,
    routeTrackIds,
    rigClass,
  };
}

/**
 * Serialize TrailForge view & selection state into a clean query string (including leading `?`,
 * or empty string when no state params are set), preserving unrelated query params.
 */
export function buildAppUrlSearch(
  state: Partial<AppUrlState>,
  existingSearch = ""
): string {
  const params = new URLSearchParams(
    existingSearch.startsWith("?") ? existingSearch.slice(1) : existingSearch
  );

  // Remove managed keys first so parameter ordering is clean & deterministic
  const managedKeys = [
    "track",
    "route",
    "rig",
    "region",
    "lat",
    "lon",
    "z",
    "profile",
  ];
  const preserved: Array<[string, string]> = [];
  params.forEach((value, key) => {
    if (!managedKeys.includes(key)) {
      preserved.push([key, value]);
    }
  });

  const next = new URLSearchParams();
  if (state.trackId && TRACK_ID_PATTERN.test(state.trackId)) {
    next.set("track", state.trackId);
    if (state.profileOpen) {
      next.set("profile", "1");
    }
  }
  if (Array.isArray(state.routeTrackIds) && state.routeTrackIds.length > 0) {
    const validIds = parseRouteTrackIdsParam(state.routeTrackIds);
    if (validIds.length > 0) {
      next.set("route", validIds.join(","));
    }
  }
  const normalizedRig = parseVehicleClassParam(state.rigClass ?? null);
  if (normalizedRig) {
    next.set("rig", normalizedRig);
  }
  if (state.regionId && getPresetById(state.regionId)) {
    next.set("region", state.regionId);
  }
  if (
    typeof state.lat === "number" &&
    Number.isFinite(state.lat) &&
    typeof state.lon === "number" &&
    Number.isFinite(state.lon)
  ) {
    next.set("lat", state.lat.toFixed(4));
    next.set("lon", state.lon.toFixed(4));
    if (typeof state.zoom === "number" && Number.isFinite(state.zoom)) {
      next.set("z", state.zoom.toFixed(2));
    }
  }

  for (const [k, v] of preserved) {
    next.append(k, v);
  }

  // Keep comma delimiters in `route` readable (RFC 3986 permits `,` in query values
  // and validated track IDs contain only [A-Za-z0-9_.:-]).
  const encoded = next
    .toString()
    .replace(/((?:^|&)route=[^&]*)/, (segment) =>
      segment.replace(/%2C/g, ",")
    );
  return encoded ? `?${encoded}` : "";
}
