import type { FeatureCollection } from "geojson";
import type {
  ApiHealthResponse,
  ApiSearchResponse,
  AuthMeResponse,
  AuthTokenResponse,
  ClosuresListResponse,
  ClosuresStatusResponse,
  ParkTenureItem,
  PoiItem,
  TileManifestResponse,
  TrackInspectorDetail,
  UserFavoriteItem,
  UserPinInput,
  UserPinItem,
  VehicleCapabilityClass,
  VehicleProfileInput,
  VehicleProfileItem,
} from "./types";

const AUTH_TOKEN_STORAGE_KEY = "trailforge.access_token";

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function getStoredAuthToken(): string | null {
  try {
    if (typeof window === "undefined" || !window.localStorage) return null;
    const token = window.localStorage.getItem(AUTH_TOKEN_STORAGE_KEY);
    return token && token.trim() ? token.trim() : null;
  } catch {
    return null;
  }
}

export function setStoredAuthToken(token: string | null): void {
  try {
    if (typeof window === "undefined" || !window.localStorage) return;
    if (token && token.trim()) {
      window.localStorage.setItem(AUTH_TOKEN_STORAGE_KEY, token.trim());
    } else {
      window.localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
    }
  } catch {
    // Ignore storage quota/private-mode errors
  }
}

function buildHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(extra ?? {}),
  };
  const token = getStoredAuthToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
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

async function requestJson<T>(
  url: string,
  options?: {
    method?: string;
    body?: unknown;
    signal?: AbortSignal;
  }
): Promise<T> {
  const hasBody = options?.body !== undefined;
  const res = await fetch(url, {
    method: options?.method ?? "GET",
    headers: buildHeaders(
      hasBody ? { "Content-Type": "application/json" } : undefined
    ),
    credentials: "same-origin",
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
  const res = await fetch("/api/health", {
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

export function fetchTileManifest(
  signal?: AbortSignal
): Promise<TileManifestResponse> {
  return requestJson<TileManifestResponse>("/api/tiles/manifest.json", {
    signal,
  });
}

export function fetchTracksGeoJson(
  signal?: AbortSignal
): Promise<FeatureCollection> {
  return requestJson<FeatureCollection>("/api/tracks/geojson?limit=2500", {
    signal,
  });
}

export function fetchPois(signal?: AbortSignal): Promise<PoiItem[]> {
  return requestJson<PoiItem[]>("/api/pois?limit=500", { signal });
}

export function fetchParks(signal?: AbortSignal): Promise<ParkTenureItem[]> {
  return requestJson<ParkTenureItem[]>("/api/parks?limit=200", { signal });
}

export function fetchTrackDetail(
  trackId: string,
  options?: {
    vehicleClass?: VehicleCapabilityClass | null;
    signal?: AbortSignal;
  }
): Promise<TrackInspectorDetail> {
  const params = new URLSearchParams();
  if (options?.vehicleClass) {
    params.set("vehicle_class", options.vehicleClass);
  }
  const qs = params.toString();
  const url = `/api/tracks/${encodeURIComponent(trackId)}${qs ? `?${qs}` : ""}`;
  return requestJson<TrackInspectorDetail>(url, { signal: options?.signal });
}

export function fetchSearch(
  query: string,
  limit = 12,
  signal?: AbortSignal
): Promise<ApiSearchResponse> {
  const params = new URLSearchParams({
    q: query,
    limit: String(limit),
  });
  return requestJson<ApiSearchResponse>(`/api/search?${params.toString()}`, {
    signal,
  });
}

/* ---------- Slice 04 Closures & Official Feeds ---------- */

export function fetchClosuresStatus(
  signal?: AbortSignal
): Promise<ClosuresStatusResponse> {
  return requestJson<ClosuresStatusResponse>("/api/closures/status", { signal });
}

export function fetchClosuresGeoJson(
  signal?: AbortSignal
): Promise<FeatureCollection> {
  return requestJson<FeatureCollection>("/api/closures/geojson", { signal });
}

export function fetchClosuresList(
  signal?: AbortSignal
): Promise<ClosuresListResponse> {
  return requestJson<ClosuresListResponse>("/api/closures?active_only=true", {
    signal,
  });
}

/* ---------- Slice 05 Auth, Vehicles, Favorites, Pins & Export ---------- */

export function fetchAuthMe(signal?: AbortSignal): Promise<AuthMeResponse> {
  return requestJson<AuthMeResponse>("/api/auth/me", { signal });
}

export async function registerAccount(payload: {
  email: string;
  display_name: string;
  password: string;
}): Promise<AuthTokenResponse> {
  const res = await requestJson<AuthTokenResponse>("/api/auth/register", {
    method: "POST",
    body: payload,
  });
  setStoredAuthToken(res.access_token);
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
  setStoredAuthToken(res.access_token);
  return res;
}

export async function logoutAccount(): Promise<void> {
  try {
    await requestJson<{ ok: boolean }>("/api/auth/logout", { method: "POST" });
  } finally {
    setStoredAuthToken(null);
  }
}

export async function fetchVehicles(
  signal?: AbortSignal
): Promise<VehicleProfileItem[]> {
  const data = await requestJson<
    VehicleProfileItem[] | { items: VehicleProfileItem[] }
  >("/api/vehicles", { signal });
  return Array.isArray(data) ? data : (data.items ?? []);
}

export function createVehicle(
  payload: VehicleProfileInput
): Promise<VehicleProfileItem> {
  return requestJson<VehicleProfileItem>("/api/vehicles", {
    method: "POST",
    body: payload,
  });
}

export function updateVehicle(
  id: string,
  payload: Partial<VehicleProfileInput>
): Promise<VehicleProfileItem> {
  return requestJson<VehicleProfileItem>(
    `/api/vehicles/${encodeURIComponent(id)}`,
    {
      method: "PATCH",
      body: payload,
    }
  );
}

export function deleteVehicle(id: string): Promise<void> {
  return requestJson<void>(`/api/vehicles/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export function activateVehicle(id: string): Promise<VehicleProfileItem> {
  return requestJson<VehicleProfileItem>(
    `/api/vehicles/${encodeURIComponent(id)}/activate`,
    { method: "POST" }
  );
}

export async function fetchFavorites(
  signal?: AbortSignal
): Promise<UserFavoriteItem[]> {
  const data = await requestJson<
    UserFavoriteItem[] | { items: UserFavoriteItem[] }
  >("/api/favorites", { signal });
  return Array.isArray(data) ? data : (data.items ?? []);
}

export function addFavorite(
  trackId: string,
  notes?: string | null
): Promise<UserFavoriteItem> {
  return requestJson<UserFavoriteItem>("/api/favorites", {
    method: "POST",
    body: { track_id: trackId, notes: notes ?? null },
  });
}

export function removeFavorite(trackId: string): Promise<void> {
  return requestJson<void>(
    `/api/favorites/${encodeURIComponent(trackId)}`,
    { method: "DELETE" }
  );
}

export async function fetchPins(signal?: AbortSignal): Promise<UserPinItem[]> {
  const data = await requestJson<UserPinItem[] | { items: UserPinItem[] }>(
    "/api/pins",
    { signal }
  );
  return Array.isArray(data) ? data : (data.items ?? []);
}

export function createPin(payload: UserPinInput): Promise<UserPinItem> {
  return requestJson<UserPinItem>("/api/pins", {
    method: "POST",
    body: payload,
  });
}

export function deletePin(id: string): Promise<void> {
  return requestJson<void>(`/api/pins/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export async function downloadAuthenticatedExport(
  url: string,
  fallbackFilename: string
): Promise<void> {
  const res = await fetch(url, {
    method: "GET",
    headers: buildHeaders({ Accept: "*/*" }),
    credentials: "same-origin",
  });
  if (!res.ok) {
    const detail = await parseErrorDetail(res);
    throw new ApiError(detail, res.status);
  }
  const blob = await res.blob();
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
