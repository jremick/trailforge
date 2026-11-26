import type { FeatureCollection } from "geojson";
import type {
  ApiHealthResponse,
  ApiSearchResponse,
  ParkTenureItem,
  PoiItem,
  TileManifestResponse,
  TrackInspectorDetail,
} from "./types";

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
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
      if (typeof body.message === "string") return body.message;
    }
    const text = await res.text();
    if (text.trim()) return text.slice(0, 200);
  } catch {
    // Ignore error body parse failures
  }
  return `HTTP ${res.status} ${res.statusText}`;
}

async function requestJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
    signal,
  });

  if (!res.ok) {
    const detail = await parseErrorDetail(res);
    throw new ApiError(detail, res.status);
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
  return requestJson<TileManifestResponse>("/api/tiles/manifest.json", signal);
}

export function fetchTracksGeoJson(
  signal?: AbortSignal
): Promise<FeatureCollection> {
  return requestJson<FeatureCollection>("/api/tracks/geojson?limit=2500", signal);
}

export function fetchPois(signal?: AbortSignal): Promise<PoiItem[]> {
  return requestJson<PoiItem[]>("/api/pois?limit=500", signal);
}

export function fetchParks(signal?: AbortSignal): Promise<ParkTenureItem[]> {
  return requestJson<ParkTenureItem[]>("/api/parks?limit=200", signal);
}

export function fetchTrackDetail(
  trackId: string,
  signal?: AbortSignal
): Promise<TrackInspectorDetail> {
  return requestJson<TrackInspectorDetail>(
    `/api/tracks/${encodeURIComponent(trackId)}`,
    signal
  );
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
  return requestJson<ApiSearchResponse>(
    `/api/search?${params.toString()}`,
    signal
  );
}
