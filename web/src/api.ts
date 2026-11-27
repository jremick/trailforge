import type { FeatureCollection } from "geojson";
import {
  DEFAULT_AUTH_CONFIG,
  getApiBaseUrl,
  normalizeAuthConfig,
  resolveApiUrl,
} from "./adminHelpers";
import {
  getActiveOfflinePackId,
  getOfflineClosuresGeoJson,
  getOfflineHotspotsGeoJson,
  getOfflinePois,
  getOfflineReportsGeoJson,
  getOfflineTileManifest,
  getOfflineTrackDetail,
  getOfflineTracksGeoJson,
  searchOfflineSnapshots,
} from "./offline/packStore";
import {
  DEFAULT_PUBLIC_UI_CONFIG,
  normalizePublicUiConfig,
  normalizeUserUiPreferences,
} from "./layerPrefsHelpers";
import type {
  AdminComputeActionResponse,
  AdminInviteCreateInput,
  AdminInviteItem,
  AdminStatsResponse,
  AdminUiSettingsResponse,
  AdminUiSettingsUpdateInput,
  AdminUserItem,
  AdminUserPatchInput,
  ApiHealthResponse,
  ApiSearchResponse,
  AuthConfigResponse,
  AuthMeResponse,
  AuthTokenResponse,
  ClosureDetailResponse,
  ClosuresListResponse,
  ClosuresStatusResponse,
  ConditionReportCreateInput,
  ConditionReportItem,
  MobileTripSyncPayload,
  OfflinePackManifest,
  OfflinePackRegionItem,
  OfflinePackSnapshot,
  OfflinePacksCatalogResponse,
  ParkTenureItem,
  PlannedRouteAnalysis,
  PoiItem,
  PublicUiConfigResponse,
  RainfallSummaryResponse,
  ReportVoteKind,
  RoutePlanRequest,
  SavedRouteCreateInput,
  SavedRouteItem,
  SavedRouteUpdateInput,
  TileManifestResponse,
  TrackInspectorDetail,
  TripItem,
  TripUploadJsonPayload,
  TripVisibility,
  UserFavoriteItem,
  UserPinInput,
  UserPinItem,
  UserUiPreferencesResponse,
  UserUiPreferencesUpdateInput,
  VehicleCapabilityClass,
  VehicleProfileInput,
  VehicleProfileItem,
} from "./types";

export { getApiBaseUrl, resolveApiUrl };
import { isNativeApp } from "./native/bridge";
import {
  slugifyExportFilename,
  triggerClientFileDownload,
} from "./offline/exporters";
import {
  buildRouteClientGeoJson,
  buildRouteClientGpx,
  planRouteFromOfflineStore,
} from "./routeHelpers";

export const AUTH_TOKEN_STORAGE_KEY = "trailforge.access_token";

export interface AccountRequestOptions {
  signal?: AbortSignal;
  ownerId?: string;
  anonymous?: boolean;
}

let sessionBearer: string | null = null;

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Native and cross-origin clients keep a bearer for the lifetime of the app page. */
export function usesSessionBearer(): boolean {
  if (isNativeApp()) return true;
  const apiBase = getApiBaseUrl();
  if (!apiBase) return false;
  const location = typeof window === "undefined" ? undefined : window.location;
  if (!location) return true;
  try {
    return new URL(apiBase, location.href).origin !== location.origin;
  } catch {
    return true;
  }
}

/** Remove a bearer saved by older releases when opening the app on any platform. */
export function clearLegacyStoredAuthToken(): void {
  try {
    if (typeof window !== "undefined") {
      window.localStorage?.removeItem(AUTH_TOKEN_STORAGE_KEY);
    }
  } catch {
    // Storage may be disabled by the browser.
  }
}

function acceptSessionToken(token: string): void {
  clearLegacyStoredAuthToken();
  sessionBearer = usesSessionBearer() ? token : null;
}

function accountOptions(options?: AccountRequestOptions | AbortSignal): AccountRequestOptions {
  return options && "aborted" in options ? { signal: options } : options ?? {};
}

function buildHeaders(
  extra?: Record<string, string>,
  options?: AccountRequestOptions
): Record<string, string> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(extra ?? {}),
  };
  if (options?.anonymous) return headers;
  if (sessionBearer && usesSessionBearer()) {
    headers.Authorization = `Bearer ${sessionBearer}`;
  }
  if (options?.ownerId) {
    headers["X-TrailForge-Account"] = options.ownerId;
  }
  return headers;
}

async function parseErrorDetail(res: Response): Promise<string> {
  try {
    const contentType = res.headers.get("content-type") ?? "";
    if (contentType.includes("application/json")) {
      const body = (await res.json()) as {
        detail?: unknown;
        message?: unknown;
      };
      if (typeof body.detail === "string") return body.detail;
      if (Array.isArray(body.detail)) {
        return body.detail
          .map((entry: unknown) => {
            if (entry && typeof entry === "object" && "msg" in entry) {
              return String((entry as { msg: unknown }).msg);
            }
            return String(entry);
          })
          .join("; ");
      }
      if (typeof body.message === "string") return body.message;
    }
    const text = await res.text();
    if (text.trim()) return text.slice(0, 200);
  } catch {
    // Ignore error body parse failures
  }
  return `HTTP ${res.status} ${res.statusText}`;
}

function fetchCredentialsMode(options?: AccountRequestOptions): RequestCredentials {
  return options?.anonymous || usesSessionBearer() ? "omit" : "include";
}

export async function requestJson<T>(
  url: string,
  options?: AccountRequestOptions & {
    method?: string;
    body?: unknown;
  },
  fetchImpl: typeof fetch = fetch
): Promise<T> {
  options?.signal?.throwIfAborted();
  const hasBody = options?.body !== undefined;
  const res = await fetchImpl(resolveApiUrl(url), {
    method: options?.method ?? "GET",
    headers: buildHeaders(
      hasBody ? { "Content-Type": "application/json" } : undefined,
      options
    ),
    credentials: fetchCredentialsMode(options),
    cache: "no-store",
    body: hasBody ? JSON.stringify(options.body) : undefined,
    signal: options?.signal,
  });

  if (!res.ok) {
    const detail = await parseErrorDetail(res);
    throw new ApiError(detail, res.status);
  }

  if (res.status === 204) {
    return undefined as T;
  }

  return (await res.json()) as T;
}

function isStructuredHealthPayload(value: unknown): value is ApiHealthResponse {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.status === "string" &&
    typeof candidate.db_ok === "boolean"
  );
}

/* ---------- Core Health & Slice 03 Tiles / Tracks ---------- */

export async function fetchHealth(
  signal?: AbortSignal
): Promise<ApiHealthResponse> {
  const res = await fetch(resolveApiUrl("/api/health"), {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
    signal,
  });

  if (res.ok) {
    return (await res.json()) as ApiHealthResponse;
  }

  // Backend returns HTTP 503 with a structured ApiHealthResponse when PostGIS is degraded.
  if (res.status === 503) {
    const contentType = res.headers.get("content-type") ?? "";
    if (contentType.includes("application/json")) {
      try {
        const body: unknown = await res.clone().json();
        if (isStructuredHealthPayload(body)) {
          return body;
        }
      } catch {
        // Fall through to generic ApiError extraction
      }
    }
  }

  const detail = await parseErrorDetail(res);
  throw new ApiError(detail, res.status);
}

function shouldPreferOfflinePack(): boolean {
  if (typeof navigator !== "undefined" && navigator.onLine === false) {
    return true;
  }
  return Boolean(getActiveOfflinePackId());
}

export async function fetchTileManifest(
  signal?: AbortSignal
): Promise<TileManifestResponse> {
  if (shouldPreferOfflinePack()) {
    const offline = await getOfflineTileManifest();
    if (offline) return offline;
  }
  try {
    return await requestJson<TileManifestResponse>("/api/tiles/manifest.json", {
      signal,
    });
  } catch (err) {
    if (signal?.aborted) throw err;
    const offline = await getOfflineTileManifest();
    if (offline) return offline;
    throw err;
  }
}

export async function fetchTracksGeoJson(
  signal?: AbortSignal
): Promise<FeatureCollection> {
  if (shouldPreferOfflinePack()) {
    const offline = await getOfflineTracksGeoJson();
    if (offline) return offline;
  }
  try {
    return await requestJson<FeatureCollection>("/api/tracks/geojson?limit=2500", {
      signal,
    });
  } catch (err) {
    if (signal?.aborted) throw err;
    const offline = await getOfflineTracksGeoJson();
    if (offline) return offline;
    throw err;
  }
}

export async function fetchPois(signal?: AbortSignal): Promise<PoiItem[]> {
  if (shouldPreferOfflinePack()) {
    const offline = await getOfflinePois();
    if (offline) return offline;
  }
  try {
    return await requestJson<PoiItem[]>("/api/pois?limit=500", { signal });
  } catch (err) {
    if (signal?.aborted) throw err;
    const offline = await getOfflinePois();
    if (offline) return offline;
    throw err;
  }
}

export function fetchParks(signal?: AbortSignal): Promise<ParkTenureItem[]> {
  return requestJson<ParkTenureItem[]>("/api/parks?limit=200", { signal });
}

export async function fetchTrackDetail(
  trackId: string,
  options?: AccountRequestOptions & {
    vehicleClass?: VehicleCapabilityClass | null;
  }
): Promise<TrackInspectorDetail> {
  options?.signal?.throwIfAborted();
  if (shouldPreferOfflinePack()) {
    const offline = await getOfflineTrackDetail(trackId);
    if (offline) return offline;
  }
  const params = new URLSearchParams();
  if (options?.vehicleClass) {
    params.set("vehicle_class", options.vehicleClass);
  }
  const qs = params.toString();
  const url = `/api/tracks/${encodeURIComponent(trackId)}${qs ? `?${qs}` : ""}`;
  try {
    return await requestJson<TrackInspectorDetail>(url, options);
  } catch (err) {
    if (options?.signal?.aborted) throw err;
    if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
      throw err;
    }
    const offline = await getOfflineTrackDetail(trackId);
    if (offline) return offline;
    throw err;
  }
}

export async function fetchSearch(
  query: string,
  limit = 12,
  signal?: AbortSignal
): Promise<ApiSearchResponse> {
  if (shouldPreferOfflinePack()) {
    const offline = await searchOfflineSnapshots(query, limit);
    if (offline) return offline;
  }
  const params = new URLSearchParams({
    q: query,
    limit: String(limit),
  });
  try {
    return await requestJson<ApiSearchResponse>(
      `/api/search?${params.toString()}`,
      { signal }
    );
  } catch (err) {
    if (signal?.aborted) throw err;
    const offline = await searchOfflineSnapshots(query, limit);
    if (offline) return offline;
    throw err;
  }
}

/* ---------- Slice 04 Closures & Official Feeds ---------- */

export function fetchClosuresStatus(
  signal?: AbortSignal
): Promise<ClosuresStatusResponse> {
  return requestJson<ClosuresStatusResponse>("/api/closures/status", { signal });
}

export async function fetchClosuresGeoJson(
  signal?: AbortSignal
): Promise<FeatureCollection> {
  if (shouldPreferOfflinePack()) {
    const offline = await getOfflineClosuresGeoJson();
    if (offline) return offline;
  }
  try {
    return await requestJson<FeatureCollection>("/api/closures/geojson", {
      signal,
    });
  } catch (err) {
    if (signal?.aborted) throw err;
    const offline = await getOfflineClosuresGeoJson();
    if (offline) return offline;
    throw err;
  }
}

export function fetchClosuresList(
  signal?: AbortSignal
): Promise<ClosuresListResponse> {
  return requestJson<ClosuresListResponse>("/api/closures?active_only=true", {
    signal,
  });
}

export function fetchClosureDetail(
  closureId: number | string,
  signal?: AbortSignal
): Promise<ClosureDetailResponse> {
  return requestJson<ClosureDetailResponse>(
    `/api/closures/${encodeURIComponent(String(closureId))}`,
    { signal }
  );
}

/* ---------- Public UI Defaults & User Layer Preferences ---------- */

export async function fetchPublicUiConfig(
  signal?: AbortSignal
): Promise<PublicUiConfigResponse> {
  try {
    const raw = await requestJson<PublicUiConfigResponse>("/api/ui-config", {
      signal,
    });
    return normalizePublicUiConfig(raw);
  } catch (err) {
    if (signal?.aborted) throw err;
    return {
      default_layer_order: [...DEFAULT_PUBLIC_UI_CONFIG.default_layer_order],
      default_layer_visibility: {
        ...DEFAULT_PUBLIC_UI_CONFIG.default_layer_visibility,
      },
      show_stack_telemetry_public:
        DEFAULT_PUBLIC_UI_CONFIG.show_stack_telemetry_public,
    };
  }
}

export async function fetchUserPreferences(
  options?: AccountRequestOptions | AbortSignal
): Promise<UserUiPreferencesResponse> {
  const raw = await requestJson<UserUiPreferencesResponse>(
    "/api/user/preferences",
    accountOptions(options)
  );
  return normalizeUserUiPreferences(raw);
}

export async function updateUserPreferences(
  payload: UserUiPreferencesUpdateInput,
  options?: AccountRequestOptions
): Promise<UserUiPreferencesResponse> {
  const raw = await requestJson<UserUiPreferencesResponse>(
    "/api/user/preferences",
    {
      method: "PUT",
      body: payload,
      ...options,
    }
  );
  return normalizeUserUiPreferences(raw);
}

export async function resetUserPreferences(
  options?: AccountRequestOptions
): Promise<UserUiPreferencesResponse> {
  const raw = await requestJson<UserUiPreferencesResponse>(
    "/api/user/preferences",
    { method: "DELETE", ...options }
  );
  return normalizeUserUiPreferences(raw);
}

/* ---------- Slice 05 Auth, Vehicles, Favorites, Pins & Export ---------- */

export async function fetchAuthConfig(
  signal?: AbortSignal
): Promise<AuthConfigResponse> {
  try {
    const raw = await requestJson<AuthConfigResponse>("/api/auth/config", {
      signal,
    });
    return normalizeAuthConfig(raw);
  } catch (err) {
    if (signal?.aborted) throw err;
    return { ...DEFAULT_AUTH_CONFIG };
  }
}

export async function fetchAuthMe(signal?: AbortSignal): Promise<AuthMeResponse> {
  const currentBearer = sessionBearer;
  try {
    const result = await requestJson<AuthMeResponse>("/api/auth/me", { signal });
    if (!result.authenticated && sessionBearer === currentBearer) {
      sessionBearer = null;
    }
    return result;
  } catch (err) {
    if (err instanceof ApiError && err.status === 403 && sessionBearer === currentBearer) {
      sessionBearer = null;
    }
    throw err;
  }
}

export async function registerAccount(payload: {
  email: string;
  display_name: string;
  password: string;
  invite_code?: string | null;
}): Promise<AuthTokenResponse> {
  const body: Record<string, string> = {
    email: payload.email,
    display_name: payload.display_name,
    password: payload.password,
  };
  const trimmedCode = payload.invite_code?.trim();
  if (trimmedCode) {
    body.invite_code = trimmedCode;
  }
  const res = await requestJson<AuthTokenResponse>("/api/auth/register", {
    method: "POST",
    body,
  });
  acceptSessionToken(res.access_token);
  return res;
}

export async function loginAccount(payload: {
  email: string;
  password: string;
}): Promise<AuthTokenResponse> {
  const res = await requestJson<AuthTokenResponse>("/api/auth/login", {
    method: "POST",
    body: payload,
  });
  acceptSessionToken(res.access_token);
  return res;
}

export async function logoutAccount(options?: AccountRequestOptions): Promise<void> {
  const currentBearer = sessionBearer;
  try {
    await requestJson<{ ok: boolean }>("/api/auth/logout", { method: "POST", ...options });
  } finally {
    if (sessionBearer === currentBearer) sessionBearer = null;
    clearLegacyStoredAuthToken();
  }
}

export async function fetchVehicles(
  options?: AccountRequestOptions | AbortSignal
): Promise<VehicleProfileItem[]> {
  const data = await requestJson<
    VehicleProfileItem[] | { items: VehicleProfileItem[] }
  >("/api/vehicles", accountOptions(options));
  return Array.isArray(data) ? data : (data.items ?? []);
}

export function createVehicle(
  payload: VehicleProfileInput,
  options?: AccountRequestOptions
): Promise<VehicleProfileItem> {
  return requestJson<VehicleProfileItem>("/api/vehicles", {
    method: "POST",
    body: payload,
    ...options,
  });
}

export function updateVehicle(
  id: string,
  payload: Partial<VehicleProfileInput>,
  options?: AccountRequestOptions
): Promise<VehicleProfileItem> {
  return requestJson<VehicleProfileItem>(
    `/api/vehicles/${encodeURIComponent(id)}`,
    {
      method: "PATCH",
      body: payload,
      ...options,
    }
  );
}

export function deleteVehicle(id: string, options?: AccountRequestOptions): Promise<void> {
  return requestJson<void>(`/api/vehicles/${encodeURIComponent(id)}`, {
    method: "DELETE",
    ...options,
  });
}

export function activateVehicle(
  id: string,
  options?: AccountRequestOptions
): Promise<VehicleProfileItem> {
  return requestJson<VehicleProfileItem>(
    `/api/vehicles/${encodeURIComponent(id)}/activate`,
    { method: "POST", ...options }
  );
}

export async function fetchFavorites(
  options?: AccountRequestOptions | AbortSignal
): Promise<UserFavoriteItem[]> {
  const data = await requestJson<
    UserFavoriteItem[] | { items: UserFavoriteItem[] }
  >("/api/favorites", accountOptions(options));
  return Array.isArray(data) ? data : (data.items ?? []);
}

export function addFavorite(
  trackId: string,
  notes?: string | null,
  options?: AccountRequestOptions
): Promise<UserFavoriteItem> {
  return requestJson<UserFavoriteItem>("/api/favorites", {
    method: "POST",
    body: { track_id: trackId, notes: notes ?? null },
    ...options,
  });
}

export function removeFavorite(trackId: string, options?: AccountRequestOptions): Promise<void> {
  return requestJson<void>(
    `/api/favorites/${encodeURIComponent(trackId)}`,
    { method: "DELETE", ...options }
  );
}

export async function fetchPins(options?: AccountRequestOptions | AbortSignal): Promise<UserPinItem[]> {
  const data = await requestJson<UserPinItem[] | { items: UserPinItem[] }>(
    "/api/pins",
    accountOptions(options)
  );
  return Array.isArray(data) ? data : (data.items ?? []);
}

export function createPin(payload: UserPinInput, options?: AccountRequestOptions): Promise<UserPinItem> {
  return requestJson<UserPinItem>("/api/pins", {
    method: "POST",
    body: payload,
    ...options,
  });
}

export function deletePin(id: string, options?: AccountRequestOptions): Promise<void> {
  return requestJson<void>(`/api/pins/${encodeURIComponent(id)}`, {
    method: "DELETE",
    ...options,
  });
}

/* ---------- Slice 06 Trips, Telemetry & Unmapped Candidates ---------- */

export async function fetchTrips(options?: AccountRequestOptions | AbortSignal): Promise<TripItem[]> {
  const data = await requestJson<TripItem[] | { items: TripItem[] }>(
    "/api/trips",
    accountOptions(options)
  );
  return Array.isArray(data) ? data : (data.items ?? []);
}

export async function uploadTripGpx(
  file: File,
  params: {
    name: string;
    vehicle_profile_id?: string | null;
    visibility: TripVisibility;
    privacy_trim_m: number;
  },
  options?: AccountRequestOptions
): Promise<TripItem> {
  options?.signal?.throwIfAborted();
  const form = new FormData();
  form.append("file", file, file.name);
  form.append("name", params.name);
  form.append("visibility", params.visibility);
  form.append("privacy_trim_m", String(params.privacy_trim_m));
  if (params.vehicle_profile_id) {
    form.append("vehicle_profile_id", params.vehicle_profile_id);
  }

  const res = await fetch(resolveApiUrl("/api/trips/upload"), {
    method: "POST",
    headers: buildHeaders(undefined, options),
    credentials: fetchCredentialsMode(options),
    body: form,
    signal: options?.signal,
  });
  if (!res.ok) {
    const detail = await parseErrorDetail(res);
    throw new ApiError(detail, res.status);
  }
  return (await res.json()) as TripItem;
}

export function uploadTripJson(
  payload: TripUploadJsonPayload,
  options?: AccountRequestOptions
): Promise<TripItem> {
  return requestJson<TripItem>("/api/trips/upload", {
    method: "POST",
    body: payload,
    ...options,
  });
}

/**
 * Upload a mobile/PWA recorded drive session (including high-rate `imu_samples`
 * and `recording_source`) via `POST /api/trips/mobile-sync`. Automatically falls
 * back to `POST /api/trips/upload` when talking to a backend without mobile-sync.
 */
export async function uploadMobileTripSync(
  payload: MobileTripSyncPayload,
  options?: AccountRequestOptions
): Promise<TripItem> {
  const body: MobileTripSyncPayload = {
    ...payload,
    client_session_id: payload.client_session_id ?? payload.client_uuid ?? null,
    recording_source: payload.recording_source ?? "pwa",
  };

  try {
    return await requestJson<TripItem>("/api/trips/mobile-sync", {
      method: "POST",
      body,
      ...options,
    });
  } catch (err) {
    if (err instanceof ApiError && (err.status === 404 || err.status === 405)) {
      options?.signal?.throwIfAborted();
      return uploadTripJson(payload, options);
    }
    throw err;
  }
}

export function updateTrip(
  tripId: string,
  patch: { name?: string; visibility?: TripVisibility },
  options?: AccountRequestOptions
): Promise<TripItem> {
  return requestJson<TripItem>(`/api/trips/${encodeURIComponent(tripId)}`, {
    method: "PATCH",
    body: patch,
    ...options,
  });
}

export function deleteTrip(tripId: string, options?: AccountRequestOptions): Promise<void> {
  return requestJson<void>(`/api/trips/${encodeURIComponent(tripId)}`, {
    method: "DELETE",
    ...options,
  });
}

export function fetchUnmappedCandidatesGeoJson(
  signal?: AbortSignal
): Promise<FeatureCollection> {
  return requestJson<FeatureCollection>("/api/telemetry/unmapped", { signal });
}

/* ---------- Slice 07 Live Crowdsourced Grading, Hotspots & Rainfall ---------- */

export async function fetchHotspotsGeoJson(
  signal?: AbortSignal
): Promise<FeatureCollection> {
  if (shouldPreferOfflinePack()) {
    const offline = await getOfflineHotspotsGeoJson();
    if (offline) return offline;
  }
  try {
    return await requestJson<FeatureCollection>(
      "/api/grading/hotspots/geojson",
      { signal }
    );
  } catch (err) {
    if (signal?.aborted) throw err;
    const offline = await getOfflineHotspotsGeoJson();
    if (offline) return offline;
    throw err;
  }
}

export function fetchRainfallSummary(
  signal?: AbortSignal
): Promise<RainfallSummaryResponse> {
  return requestJson<RainfallSummaryResponse>("/api/grading/rainfall", {
    signal,
  });
}

/* ---------- Slice 08 Community Condition Reports & Gate Warnings ---------- */

export async function fetchReportsGeoJson(
  options?: AccountRequestOptions | AbortSignal
): Promise<FeatureCollection> {
  const requestOptions = accountOptions(options);
  const { signal } = requestOptions;
  signal?.throwIfAborted();
  if (shouldPreferOfflinePack()) {
    const offline = await getOfflineReportsGeoJson();
    if (offline) return offline;
  }
  try {
    return await requestJson<FeatureCollection>(
      "/api/reports/geojson?include_resolved=false",
      requestOptions
    );
  } catch (err) {
    if (signal?.aborted) throw err;
    if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
      throw err;
    }
    const offline = await getOfflineReportsGeoJson();
    if (offline) return offline;
    throw err;
  }
}

export async function createConditionReport(
  input: ConditionReportCreateInput,
  options?: AccountRequestOptions
): Promise<ConditionReportItem> {
  options?.signal?.throwIfAborted();
  if (input.photo) {
    const form = new FormData();
    form.append("report_type", input.report_type);
    form.append("lon", String(input.lon));
    form.append("lat", String(input.lat));
    form.append("notes", input.notes);
    if (input.title) form.append("title", input.title);
    if (input.severity) form.append("severity", input.severity);
    if (input.track_id) form.append("track_id", input.track_id);
    if (input.segment_id) form.append("segment_id", input.segment_id);
    if (input.vehicle_class) form.append("vehicle_class", input.vehicle_class);
    if (input.client_uuid) form.append("client_uuid", input.client_uuid);
    form.append("photo", input.photo, input.photo.name);

    const res = await fetch(resolveApiUrl("/api/reports"), {
      method: "POST",
      headers: buildHeaders(undefined, options),
      credentials: fetchCredentialsMode(options),
      body: form,
      signal: options?.signal,
    });
    if (!res.ok) {
      const detail = await parseErrorDetail(res);
      throw new ApiError(detail, res.status);
    }
    return (await res.json()) as ConditionReportItem;
  }

  return requestJson<ConditionReportItem>("/api/reports", {
    method: "POST",
    body: {
      report_type: input.report_type,
      lon: input.lon,
      lat: input.lat,
      title: input.title ?? null,
      notes: input.notes,
      severity: input.severity ?? null,
      track_id: input.track_id ?? null,
      segment_id: input.segment_id ?? null,
      vehicle_class: input.vehicle_class ?? null,
      client_uuid: input.client_uuid ?? null,
    },
    ...options,
  });
}

/* ---------- Slice 09 Offline Regional Packs ---------- */

export function fetchPacksCatalog(
  signal?: AbortSignal
): Promise<OfflinePacksCatalogResponse> {
  return requestJson<OfflinePacksCatalogResponse>("/api/packs", { signal });
}

export function buildRegionPack(
  regionId: string,
  options?: AccountRequestOptions
): Promise<OfflinePackRegionItem> {
  return requestJson<OfflinePackRegionItem>(
    `/api/packs/${encodeURIComponent(regionId)}/build`,
    { method: "POST", ...options }
  );
}

export function fetchPackManifest(
  regionId: string,
  signal?: AbortSignal
): Promise<OfflinePackManifest> {
  return requestJson<OfflinePackManifest>(
    `/api/packs/${encodeURIComponent(regionId)}/manifest.json`,
    { signal }
  );
}

export function fetchPackSnapshot(
  regionId: string,
  signal?: AbortSignal
): Promise<OfflinePackSnapshot> {
  return requestJson<OfflinePackSnapshot>(
    `/api/packs/${encodeURIComponent(regionId)}/snapshot.json`,
    { signal }
  );
}

export function voteConditionReport(
  reportId: string | number,
  vote: ReportVoteKind,
  options?: AccountRequestOptions
): Promise<ConditionReportItem> {
  return requestJson<ConditionReportItem>(
    `/api/reports/${encodeURIComponent(String(reportId))}/vote`,
    {
      method: "POST",
      body: { vote },
      ...options,
    }
  );
}

export async function downloadAuthenticatedExport(
  url: string,
  fallbackFilename: string,
  options?: AccountRequestOptions
): Promise<void> {
  options?.signal?.throwIfAborted();
  const res = await fetch(resolveApiUrl(url), {
    method: "GET",
    headers: buildHeaders({ Accept: "*/*" }, options),
    credentials: fetchCredentialsMode(options),
    cache: "no-store",
    signal: options?.signal,
  });
  if (!res.ok) {
    const detail = await parseErrorDetail(res);
    throw new ApiError(detail, res.status);
  }
  const blob = await res.blob();
  options?.signal?.throwIfAborted();
  const disposition = res.headers.get("content-disposition") ?? "";
  const match = /filename="?([^";]+)"?/i.exec(disposition);
  const filename = match?.[1] ?? fallbackFilename;

  const objectUrl = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = objectUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

/* ---------- Phase A Interactive Multi-Track Route Planner & Saved Routes ---------- */

export async function planRoute(
  payload: RoutePlanRequest,
  options?: AccountRequestOptions | AbortSignal
): Promise<PlannedRouteAnalysis> {
  const requestOptions = accountOptions(options);
  const { signal } = requestOptions;
  signal?.throwIfAborted();
  if (shouldPreferOfflinePack()) {
    const offline = await planRouteFromOfflineStore(payload);
    signal?.throwIfAborted();
    if (offline) return offline;
  }
  try {
    return await requestJson<PlannedRouteAnalysis>("/api/routes/plan", {
      method: "POST",
      body: payload,
      ...requestOptions,
    });
  } catch (err) {
    if (signal?.aborted) throw err;
    if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
      throw err;
    }
    const offline = await planRouteFromOfflineStore(payload);
    signal?.throwIfAborted();
    if (offline) return offline;
    throw err;
  }
}

export async function exportPlannedRoute(
  payload: RoutePlanRequest,
  format: "gpx" | "geojson",
  offlineFallbackAnalysis?: PlannedRouteAnalysis | null,
  options?: AccountRequestOptions
): Promise<void> {
  const signal = options?.signal;
  signal?.throwIfAborted();
  const fallbackFilename = slugifyExportFilename(
    payload.name?.trim() || "trailforge-route",
    format
  );

  const exportOffline = (analysis: PlannedRouteAnalysis): void => {
    signal?.throwIfAborted();
    if (format === "gpx") {
      triggerClientFileDownload(
        buildRouteClientGpx(analysis),
        fallbackFilename,
        "application/gpx+xml"
      );
    } else {
      triggerClientFileDownload(
        JSON.stringify(buildRouteClientGeoJson(analysis), null, 2),
        fallbackFilename,
        "application/geo+json"
      );
    }
  };

  if (shouldPreferOfflinePack()) {
    const offline =
      offlineFallbackAnalysis ?? (await planRouteFromOfflineStore(payload));
    if (offline) {
      exportOffline(offline);
      return;
    }
  }

  try {
    const res = await fetch(
      resolveApiUrl(`/api/routes/plan/export?format=${encodeURIComponent(format)}`),
      {
        method: "POST",
        headers: buildHeaders(
          { "Content-Type": "application/json", Accept: "*/*" },
          options
        ),
        credentials: fetchCredentialsMode(options),
        body: JSON.stringify(payload),
        signal,
      }
    );
    if (!res.ok) {
      const detail = await parseErrorDetail(res);
      throw new ApiError(detail, res.status);
    }
    const blob = await res.blob();
    signal?.throwIfAborted();
    const disposition = res.headers.get("content-disposition") ?? "";
    const match = /filename="?([^";]+)"?/i.exec(disposition);
    const filename = match?.[1] ?? fallbackFilename;

    const objectUrl = URL.createObjectURL(blob);
    try {
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } finally {
      URL.revokeObjectURL(objectUrl);
    }
  } catch (err) {
    signal?.throwIfAborted();
    if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
      throw err;
    }
    const offline =
      offlineFallbackAnalysis ?? (await planRouteFromOfflineStore(payload));
    if (offline) {
      exportOffline(offline);
      return;
    }
    throw err;
  }
}

export async function listSavedRoutes(
  options?: AccountRequestOptions | AbortSignal
): Promise<SavedRouteItem[]> {
  const data = await requestJson<
    SavedRouteItem[] | { items?: SavedRouteItem[]; routes?: SavedRouteItem[] }
  >("/api/routes", accountOptions(options));
  if (Array.isArray(data)) return data;
  return data.items ?? data.routes ?? [];
}

export function saveRoute(
  payload: SavedRouteCreateInput,
  options?: AccountRequestOptions
): Promise<SavedRouteItem> {
  return requestJson<SavedRouteItem>("/api/routes", {
    method: "POST",
    body: payload,
    ...options,
  });
}

export function getSavedRoute(
  id: string,
  options?: AccountRequestOptions | AbortSignal
): Promise<SavedRouteItem> {
  return requestJson<SavedRouteItem>(
    `/api/routes/${encodeURIComponent(id)}`,
    accountOptions(options)
  );
}

export function updateSavedRoute(
  id: string,
  patch: SavedRouteUpdateInput,
  options?: AccountRequestOptions
): Promise<SavedRouteItem> {
  return requestJson<SavedRouteItem>(
    `/api/routes/${encodeURIComponent(id)}`,
    {
      method: "PATCH",
      body: patch,
      ...options,
    }
  );
}

export function deleteSavedRoute(id: string, options?: AccountRequestOptions): Promise<void> {
  return requestJson<void>(`/api/routes/${encodeURIComponent(id)}`, {
    method: "DELETE",
    ...options,
  });
}

export function getSavedRouteExportUrl(
  id: string,
  format: "gpx" | "geojson"
): string {
  return `/api/routes/${encodeURIComponent(id)}/export?format=${encodeURIComponent(format)}`;
}

/* ---------- v1.1.0 Admin Console: Traffic, Invites, Users & Compute Actions ---------- */

export function fetchAdminStats(
  options?: AccountRequestOptions | AbortSignal
): Promise<AdminStatsResponse> {
  return requestJson<AdminStatsResponse>("/api/admin/stats", accountOptions(options));
}

export async function fetchAdminUsers(
  options?: AccountRequestOptions | AbortSignal
): Promise<AdminUserItem[]> {
  const data = await requestJson<
    AdminUserItem[] | { items?: AdminUserItem[]; users?: AdminUserItem[] }
  >("/api/admin/users", accountOptions(options));
  if (Array.isArray(data)) return data;
  return data.items ?? data.users ?? [];
}

export function patchAdminUser(
  userId: string,
  patch: AdminUserPatchInput,
  options?: AccountRequestOptions
): Promise<AdminUserItem> {
  return requestJson<AdminUserItem>(
    `/api/admin/users/${encodeURIComponent(userId)}`,
    {
      method: "PATCH",
      body: patch,
      ...options,
    }
  );
}

export async function fetchAdminInvites(
  options?: AccountRequestOptions | AbortSignal
): Promise<AdminInviteItem[]> {
  const data = await requestJson<
    AdminInviteItem[] | { items?: AdminInviteItem[]; invites?: AdminInviteItem[] }
  >("/api/admin/invites", accountOptions(options));
  if (Array.isArray(data)) return data;
  return data.items ?? data.invites ?? [];
}

export function createAdminInvite(
  payload: AdminInviteCreateInput,
  options?: AccountRequestOptions
): Promise<AdminInviteItem> {
  return requestJson<AdminInviteItem>("/api/admin/invites", {
    method: "POST",
    body: payload,
    ...options,
  });
}

export function revokeAdminInvite(
  inviteId: string,
  options?: AccountRequestOptions
): Promise<AdminInviteItem | void> {
  return requestJson<AdminInviteItem | void>(
    `/api/admin/invites/${encodeURIComponent(inviteId)}`,
    { method: "DELETE", ...options }
  );
}

export function triggerAdminClosuresSync(options?: AccountRequestOptions): Promise<AdminComputeActionResponse> {
  return requestJson<AdminComputeActionResponse>("/api/closures/sync", {
    method: "POST",
    ...options,
  });
}

export function triggerAdminGradingRecompute(options?: AccountRequestOptions): Promise<AdminComputeActionResponse> {
  return requestJson<AdminComputeActionResponse>("/api/grading/recompute", {
    method: "POST",
    ...options,
  });
}

export function fetchAdminUiSettings(
  options?: AccountRequestOptions | AbortSignal
): Promise<AdminUiSettingsResponse> {
  return requestJson<AdminUiSettingsResponse>("/api/admin/ui-settings", accountOptions(options));
}

export function updateAdminUiSettings(
  payload: AdminUiSettingsUpdateInput,
  options?: AccountRequestOptions
): Promise<AdminUiSettingsResponse> {
  return requestJson<AdminUiSettingsResponse>("/api/admin/ui-settings", {
    method: "PUT",
    body: payload,
    ...options,
  });
}

export function resetAdminUiSettings(options?: AccountRequestOptions): Promise<AdminUiSettingsResponse> {
  return requestJson<AdminUiSettingsResponse>("/api/admin/ui-settings", {
    method: "DELETE",
    ...options,
  });
}
