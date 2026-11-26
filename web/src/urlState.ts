import { getPresetById } from "./presets";

export interface AppUrlState {
  trackId: string | null;
  regionId: string | null;
  lat: number | null;
  lon: number | null;
  zoom: number | null;
  profileOpen: boolean;
}

const TRACK_ID_PATTERN = /^[A-Za-z0-9_.:-]{1,120}$/;

function parseFinite(raw: string | null, min: number, max: number): number | null {
  if (raw === null || raw.trim() === "") return null;
  const val = Number(raw);
  if (!Number.isFinite(val) || val < min || val > max) return null;
  return val;
}

/**
 * Parse bookmarkable/shareable TrailForge deep-link state from `window.location.search`.
 * Supports `?track=<id>&region=<preset>&lat=<deg>&lon=<deg>&z=<zoom>&profile=1`.
 */
export function parseAppUrlState(search: string): AppUrlState {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);

  const rawTrack = params.get("track")?.trim() ?? "";
  const trackId = rawTrack && TRACK_ID_PATTERN.test(rawTrack) ? rawTrack : null;

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
  const managedKeys = ["track", "region", "lat", "lon", "z", "profile"];
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

  const encoded = next.toString();
  return encoded ? `?${encoded}` : "";
}
