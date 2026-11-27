import type { Feature, FeatureCollection, Point } from "geojson";
import {
  buildOfflinePackTileCachePath,
  resolvePackAssetUrl,
} from "../adminHelpers";
import { DEFAULT_SNOWLINE_BANDS_M } from "../presets";
import type {
  ApiSearchResponse,
  ApiSearchResultItem,
  BBoxTuple,
  ClosureItem,
  ConditionReportItem,
  DownloadedPackRecord,
  OfflinePackFileEntry,
  OfflinePackManifest,
  OfflinePackRegionItem,
  OfflinePackSnapshot,
  PoiItem,
  TileManifestResponse,
  TrackInspectorDetail,
} from "../types";
import { verifySha256Hex } from "./rangeSlice";

export const OFFLINE_DB_NAME = "trailforge-offline-v1";
export const OFFLINE_DB_VERSION = 1;
export const PACKS_CACHE_NAME = "trailforge-packs-v1";
const ACTIVE_PACK_KEY = "trailforge.active_offline_pack";

export type OfflineStoreName =
  | "packs"
  | "snapshots"
  | "offline_pins"
  | "sync_queue";

export interface OfflineKvBackend {
  get<T>(store: OfflineStoreName, key: string): Promise<T | null>;
  getAll<T>(store: OfflineStoreName): Promise<T[]>;
  put<T>(store: OfflineStoreName, key: string, value: T): Promise<void>;
  delete(store: OfflineStoreName, key: string): Promise<void>;
}

/**
 * In-memory backend used automatically in Node/Vitest or environments without IndexedDB.
 */
export class MemoryOfflineBackend implements OfflineKvBackend {
  private readonly tables = new Map<OfflineStoreName, Map<string, unknown>>([
    ["packs", new Map()],
    ["snapshots", new Map()],
    ["offline_pins", new Map()],
    ["sync_queue", new Map()],
  ]);

  async get<T>(store: OfflineStoreName, key: string): Promise<T | null> {
    const val = this.tables.get(store)?.get(key);
    return val === undefined ? null : (structuredClone(val) as T);
  }

  async getAll<T>(store: OfflineStoreName): Promise<T[]> {
    const map = this.tables.get(store);
    if (!map) return [];
    return Array.from(map.values()).map((v) => structuredClone(v) as T);
  }

  async put<T>(store: OfflineStoreName, key: string, value: T): Promise<void> {
    this.tables.get(store)?.set(key, structuredClone(value));
  }

  async delete(store: OfflineStoreName, key: string): Promise<void> {
    this.tables.get(store)?.delete(key);
  }
}

class IndexedDbOfflineBackend implements OfflineKvBackend {
  private dbPromise: Promise<IDBDatabase> | null = null;

  constructor(
    private readonly name: string = OFFLINE_DB_NAME,
    private readonly stores: readonly OfflineStoreName[] = ["packs", "snapshots"]
  ) {}

  private open(): Promise<IDBDatabase> {
    if (this.dbPromise) return this.dbPromise;
    this.dbPromise = new Promise((resolve, reject) => {
      const req = indexedDB.open(this.name, OFFLINE_DB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        for (const name of this.stores) {
          if (!db.objectStoreNames.contains(name)) {
            db.createObjectStore(name);
          }
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error ?? new Error("IndexedDB open failed"));
    });
    return this.dbPromise;
  }

  async get<T>(store: OfflineStoreName, key: string): Promise<T | null> {
    const db = await this.open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(store, "readonly");
      const req = tx.objectStore(store).get(key);
      req.onsuccess = () =>
        resolve(req.result === undefined ? null : (req.result as T));
      req.onerror = () => reject(req.error);
    });
  }

  async getAll<T>(store: OfflineStoreName): Promise<T[]> {
    const db = await this.open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(store, "readonly");
      const req = tx.objectStore(store).getAll();
      req.onsuccess = () => resolve((req.result as T[]) ?? []);
      req.onerror = () => reject(req.error);
    });
  }

  async put<T>(store: OfflineStoreName, key: string, value: T): Promise<void> {
    const db = await this.open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(store, "readwrite");
      tx.objectStore(store).put(value, key);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  async delete(store: OfflineStoreName, key: string): Promise<void> {
    const db = await this.open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(store, "readwrite");
      tx.objectStore(store).delete(key);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }
}

let activeBackend: OfflineKvBackend | null = null;
const privateBackends = new Map<string, OfflineKvBackend>();
let memoryActivePackId: string | null = null;
// In-memory cache of deserialized offline pack snapshots so rapid search keystrokes and
// track clicks do not structured-clone multi-MB IndexedDB objects on every interaction.
const snapshotMemCache = new Map<string, OfflinePackSnapshot | null>();
const trackIndexMemCache = new Map<string, Map<string, TrackInspectorDetail>>();

function clearSnapshotMemCache(regionId?: string): void {
  if (regionId) {
    snapshotMemCache.delete(regionId);
    trackIndexMemCache.delete(regionId);
  } else {
    snapshotMemCache.clear();
    trackIndexMemCache.clear();
  }
}

function notifySwInvalidatePackCache(regionId?: string): void {
  try {
    if (
      typeof navigator !== "undefined" &&
      "serviceWorker" in navigator &&
      navigator.serviceWorker.controller
    ) {
      navigator.serviceWorker.controller.postMessage({
        type: "INVALIDATE_PACK_CACHE",
        regionId: regionId ?? null,
      });
    }
  } catch (err) {
    console.debug("Failed to notify service worker of pack cache invalidation:", err);
  }
}

async function loadSnapshotCached(
  regionId: string
): Promise<OfflinePackSnapshot | null> {
  if (snapshotMemCache.has(regionId)) {
    return snapshotMemCache.get(regionId) ?? null;
  }
  const snap = await getOfflineBackend().get<OfflinePackSnapshot>(
    "snapshots",
    regionId
  );
  snapshotMemCache.set(regionId, snap);
  trackIndexMemCache.delete(regionId);
  return snap;
}

function getTrackIndexForSnapshot(
  regionId: string,
  snap: OfflinePackSnapshot
): Map<string, TrackInspectorDetail> {
  let idx = trackIndexMemCache.get(regionId);
  if (!idx) {
    idx = new Map();
    for (const trk of snap.tracks ?? []) {
      idx.set(trk.track_id, trk);
    }
    trackIndexMemCache.set(regionId, idx);
  }
  return idx;
}

export function getOfflineBackend(): OfflineKvBackend {
  if (activeBackend) return activeBackend;
  if (typeof indexedDB !== "undefined") {
    activeBackend = new IndexedDbOfflineBackend();
  } else {
    activeBackend = new MemoryOfflineBackend();
  }
  return activeBackend;
}

export function getPrivateOfflineBackend(ownerId: string): OfflineKvBackend {
  if (!ownerId) throw new Error("A signed-in account is required for offline sync");
  let backend = privateBackends.get(ownerId);
  if (!backend) {
    // The old origin-wide queue and pins have no provable owner. Only account
    // databases are opened here, so old bundles cannot read new private work either.
    backend = typeof indexedDB === "undefined"
      ? new MemoryOfflineBackend()
      : new IndexedDbOfflineBackend(
          `trailforge-account-v1:${encodeURIComponent(ownerId)}`,
          ["sync_queue"]
        );
    privateBackends.set(ownerId, backend);
  }
  return backend;
}

export function setOfflineBackendForTesting(
  backend: OfflineKvBackend | null
): void {
  activeBackend = backend;
  privateBackends.clear();
  memoryActivePackId = null;
  clearSnapshotMemCache();
}

export function getActiveOfflinePackId(): string | null {
  try {
    if (typeof window !== "undefined" && window.localStorage) {
      const val = window.localStorage.getItem(ACTIVE_PACK_KEY);
      return val && val.trim() ? val.trim() : null;
    }
  } catch {
    // Ignore storage errors
  }
  return memoryActivePackId;
}

export function setActiveOfflinePackId(regionId: string | null): void {
  memoryActivePackId = regionId;
  try {
    if (typeof window !== "undefined" && window.localStorage) {
      if (regionId) {
        window.localStorage.setItem(ACTIVE_PACK_KEY, regionId);
      } else {
        window.localStorage.removeItem(ACTIVE_PACK_KEY);
      }
    }
  } catch {
    // Ignore storage errors
  }
}

export async function listDownloadedPacks(): Promise<DownloadedPackRecord[]> {
  const rows = await getOfflineBackend().getAll<DownloadedPackRecord>("packs");
  return rows.sort((a, b) =>
    (b.downloaded_at ?? "").localeCompare(a.downloaded_at ?? "")
  );
}

export async function getDownloadedPack(
  regionId: string
): Promise<DownloadedPackRecord | null> {
  return getOfflineBackend().get<DownloadedPackRecord>("packs", regionId);
}

export async function saveOfflinePackData(
  record: DownloadedPackRecord,
  snapshot: OfflinePackSnapshot
): Promise<void> {
  const backend = getOfflineBackend();
  await backend.put("packs", record.region_id, record);
  await backend.put("snapshots", record.region_id, snapshot);
  snapshotMemCache.set(record.region_id, snapshot);
  trackIndexMemCache.delete(record.region_id);
  notifySwInvalidatePackCache(record.region_id);
}

export async function removeOfflinePack(regionId: string): Promise<void> {
  const backend = getOfflineBackend();
  const existing = await getDownloadedPack(regionId);
  await backend.delete("packs", regionId);
  await backend.delete("snapshots", regionId);
  clearSnapshotMemCache(regionId);
  notifySwInvalidatePackCache(regionId);

  if (getActiveOfflinePackId() === regionId) {
    setActiveOfflinePackId(null);
  }

  if (typeof caches !== "undefined") {
    try {
      const cache = await caches.open(PACKS_CACHE_NAME);
      const urlsToDrop = [
        existing?.vector_pmtiles_url,
        existing?.terrain_pmtiles_url,
        `/api/packs/${encodeURIComponent(regionId)}/tiles/vector.pmtiles`,
        `/api/packs/${encodeURIComponent(regionId)}/tiles/terrain.pmtiles`,
      ].filter((u): u is string => Boolean(u));
      await Promise.all(urlsToDrop.map((u) => cache.delete(u)));
    } catch {
      // Ignore CacheStorage errors
    }
  }
}

function findManifestFileEntry(
  manifest: OfflinePackManifest,
  targetName: string
): OfflinePackFileEntry | null {
  const files = manifest.files;
  if (!files) return null;
  if (Array.isArray(files)) {
    return (
      files.find(
        (f) =>
          f.filename === targetName ||
          (f.path && f.path.endsWith(`/${targetName}`)) ||
          (f.url && f.url.endsWith(`/${targetName}`))
      ) ?? null
    );
  }
  if (targetName in files) return files[targetName] ?? null;
  for (const [k, v] of Object.entries(files)) {
    if (k.endsWith(targetName) || v.filename === targetName) return v;
  }
  return null;
}

export interface PackDownloadProgress {
  stage: string;
  pct: number;
}

/**
 * Download an offline regional pack from TrailForge API into IndexedDB + CacheStorage.
 * Stores full PMTiles archives in `trailforge-packs-v1` so `sw.js` serves HTTP 206
 * Range slices 100% offline.
 */
export async function downloadRegionalPackToDevice(
  region: OfflinePackRegionItem,
  onProgress?: (progress: PackDownloadProgress) => void
): Promise<DownloadedPackRecord> {
  const rid = encodeURIComponent(region.region_id);
  const manifestUrl = resolvePackAssetUrl(
    region.manifest_url,
    `/api/packs/${rid}/manifest.json`
  );
  const snapshotUrl = resolvePackAssetUrl(
    region.snapshot_url,
    `/api/packs/${rid}/snapshot.json`
  );

  onProgress?.({ stage: "Fetching pack manifest…", pct: 10 });
  const manifestRes = await fetch(manifestUrl, { cache: "no-store" });
  if (!manifestRes.ok) {
    throw new Error(`Manifest fetch failed (HTTP ${manifestRes.status})`);
  }
  const manifest = (await manifestRes.json()) as OfflinePackManifest;
  if (region.community_telemetry_policy && (
    manifest.community_telemetry_policy !== region.community_telemetry_policy ||
    (manifest.community_telemetry_revision ?? null) !== (region.community_telemetry_revision ?? null)
  )) {
    throw new Error("Pack contents have changed. Refresh the catalog and try the download again.");
  }

  onProgress?.({ stage: "Downloading offline track & POI snapshot…", pct: 32 });
  const snapRes = await fetch(snapshotUrl, { cache: "no-store" });
  if (!snapRes.ok) {
    throw new Error(`Snapshot fetch failed (HTTP ${snapRes.status})`);
  }
  const snapBuf = await snapRes.arrayBuffer();
  const snapEntry = findManifestFileEntry(manifest, "snapshot.json");
  const snapCheck = await verifySha256Hex(snapBuf, snapEntry?.sha256);
  if (snapCheck === "mismatch") {
    throw new Error("Offline snapshot SHA-256 verification failed");
  }
  const snapshot = JSON.parse(
    new TextDecoder().decode(snapBuf)
  ) as OfflinePackSnapshot;

  const vectorCachePath = buildOfflinePackTileCachePath(
    region.region_id,
    "vector"
  );
  const terrainCachePath = buildOfflinePackTileCachePath(
    region.region_id,
    "terrain"
  );
  const vectorUrl = resolvePackAssetUrl(
    region.vector_pmtiles_url ?? manifest.vector_pmtiles_url,
    vectorCachePath
  );
  const terrainUrl = resolvePackAssetUrl(
    region.terrain_pmtiles_url ?? manifest.terrain_pmtiles_url,
    terrainCachePath
  );

  let hasVector = false;
  let hasTerrain = false;
  let allVerified = snapCheck === "verified";
  let cachedBytes = snapBuf.byteLength;

  const cacheTile = async (
    tileUrl: string,
    cacheKeyPath: string,
    manifestFileName: string,
    stageLabel: string,
    pct: number
  ): Promise<boolean> => {
    onProgress?.({ stage: stageLabel, pct });
    const res = await fetch(tileUrl, { cache: "no-store" });
    if (!res.ok) return false;
    const buf = await res.arrayBuffer();
    if (buf.byteLength === 0) return false;
    const entry = findManifestFileEntry(manifest, manifestFileName);
    const check = await verifySha256Hex(buf, entry?.sha256);
    if (check === "mismatch") {
      allVerified = false;
      throw new Error(`${manifestFileName} SHA-256 verification failed`);
    }
    cachedBytes += buf.byteLength;

    if (typeof caches !== "undefined") {
      const cache = await caches.open(PACKS_CACHE_NAME);
      await cache.put(
        cacheKeyPath,
        new Response(buf, {
          status: 200,
          headers: {
            "Content-Type": "application/vnd.pmtiles",
            "Content-Length": String(buf.byteLength),
          },
        })
      );
    }
    return true;
  };

  try {
    hasVector = await cacheTile(
      vectorUrl,
      vectorCachePath,
      "vector.pmtiles",
      "Caching vector PMTiles for offline Range 206…",
      62
    );
  } catch (err) {
    if (err instanceof Error && err.message.includes("SHA-256")) throw err;
  }

  try {
    hasTerrain = await cacheTile(
      terrainUrl,
      terrainCachePath,
      "terrain.pmtiles",
      "Caching terrain hillshade PMTiles…",
      86
    );
  } catch (err) {
    if (err instanceof Error && err.message.includes("SHA-256")) throw err;
  }

  const counts = manifest.counts ?? {};
  const record: DownloadedPackRecord = {
    region_id: region.region_id,
    name: manifest.name ?? region.name,
    version: manifest.version ?? region.version ?? "1",
    downloaded_at: new Date().toISOString(),
    total_size_bytes: manifest.total_size_bytes ?? region.total_size_bytes ?? cachedBytes,
    track_count:
      counts.tracks ?? region.track_count ?? snapshot.tracks?.length ?? 0,
    segment_count: counts.segments ?? region.segment_count ?? 0,
    poi_count: counts.pois ?? region.poi_count ?? snapshot.pois?.length ?? 0,
    bbox: (manifest.bounds ?? manifest.bbox ?? region.bbox) as BBoxTuple,
    vector_pmtiles_url: hasVector ? vectorCachePath : null,
    terrain_pmtiles_url: hasTerrain ? terrainCachePath : null,
    has_vector_pmtiles: hasVector,
    has_terrain_pmtiles: hasTerrain,
    snowline_bands_m: manifest.snowline_bands_m ?? [...DEFAULT_SNOWLINE_BANDS_M],
    attributions:
      manifest.attributions ?? snapshot.attributions ?? region.attributions ?? [],
    sha256_verified: allVerified,
  };

  onProgress?.({ stage: "Saving pack to IndexedDB…", pct: 96 });
  await saveOfflinePackData(record, snapshot);
  onProgress?.({ stage: "Offline pack ready", pct: 100 });
  return record;
}

/* ---------- Offline Snapshot Query Helpers (used when offline or offline pack active) ---------- */

async function resolvePreferredPackAndSnapshot(
  regionId?: string | null
): Promise<{
  pack: DownloadedPackRecord;
  snapshot: OfflinePackSnapshot | null;
} | null> {
  const targetId = regionId ?? getActiveOfflinePackId();
  const backend = getOfflineBackend();

  if (targetId) {
    const pack = await backend.get<DownloadedPackRecord>("packs", targetId);
    if (pack) {
      const snapshot = await loadSnapshotCached(targetId);
      return { pack, snapshot };
    }
  }

  const all = await listDownloadedPacks();
  const first = all[0];
  if (!first) return null;
  const snapshot = await loadSnapshotCached(first.region_id);
  return { pack: first, snapshot };
}

export async function getOfflineTileManifest(
  regionId?: string | null
): Promise<TileManifestResponse | null> {
  const resolved = await resolvePreferredPackAndSnapshot(regionId);
  if (!resolved) return null;
  const { pack } = resolved;
  return {
    generated_at: pack.downloaded_at,
    bounds: pack.bbox,
    minzoom: 6,
    maxzoom: 15,
    vector_url:
      pack.vector_pmtiles_url ??
      `/api/packs/${encodeURIComponent(pack.region_id)}/tiles/vector.pmtiles`,
    terrain_url:
      pack.terrain_pmtiles_url ??
      `/api/packs/${encodeURIComponent(pack.region_id)}/tiles/terrain.pmtiles`,
    vector_available: pack.has_vector_pmtiles,
    terrain_available: pack.has_terrain_pmtiles,
    snowline_bands_m: pack.snowline_bands_m,
    attributions: pack.attributions,
  };
}

export async function getOfflineTrackDetail(
  trackId: string
): Promise<TrackInspectorDetail | null> {
  const activeId = getActiveOfflinePackId();

  if (activeId) {
    const activeSnap = await loadSnapshotCached(activeId);
    if (activeSnap) {
      const hit = getTrackIndexForSnapshot(activeId, activeSnap).get(trackId);
      if (hit) return hit;
    }
  }

  const packs = await listDownloadedPacks();
  for (const pack of packs) {
    if (pack.region_id === activeId) continue;
    const snap = await loadSnapshotCached(pack.region_id);
    if (!snap) continue;
    const hit = getTrackIndexForSnapshot(pack.region_id, snap).get(trackId);
    if (hit) return hit;
  }
  return null;
}

export async function getOfflineTracksGeoJson(
  regionId?: string | null
): Promise<FeatureCollection | null> {
  const resolved = await resolvePreferredPackAndSnapshot(regionId);
  const tracks = resolved?.snapshot?.tracks;
  if (!tracks || tracks.length === 0) return null;

  const features: Feature[] = [];
  for (const trk of tracks) {
    for (const seg of trk.segments ?? []) {
      const geom = (seg as unknown as { geometry?: Feature["geometry"] })
        .geometry;
      if (!geom) continue;
      const gradeObj = typeof seg.grade === "object" && seg.grade ? seg.grade : null;
      const effGrade =
        seg.effective_grade ??
        gradeObj?.effective_grade ??
        (typeof seg.grade === "string" ? seg.grade : null) ??
        trk.effective_grade ??
        trk.grade ??
        "Easy";
      features.push({
        type: "Feature",
        id: seg.segment_id,
        geometry: geom,
        properties: {
          segment_id: seg.segment_id,
          track_id: trk.track_id,
          track_name: trk.name,
          name: seg.name ?? trk.name,
          grade: effGrade,
          effective_grade: effGrade,
          max_slope_deg: seg.max_slope_deg ?? null,
          is_steep_highlight: Boolean(seg.is_steep_highlight),
          is_above_snowline: Boolean(seg.is_above_snowline),
          has_gate_barrier: Boolean(seg.has_gate_barrier),
          access_status: seg.access_status ?? "public",
        },
      });
    }
  }
  return { type: "FeatureCollection", features };
}

export async function getOfflinePois(
  regionId?: string | null
): Promise<PoiItem[] | null> {
  const resolved = await resolvePreferredPackAndSnapshot(regionId);
  return resolved?.snapshot?.pois ?? null;
}

function asFeatureCollection(raw: unknown): FeatureCollection | null {
  if (
    raw &&
    typeof raw === "object" &&
    (raw as { type?: unknown }).type === "FeatureCollection" &&
    Array.isArray((raw as { features?: unknown }).features)
  ) {
    return raw as FeatureCollection;
  }
  return null;
}

export async function getOfflineClosuresGeoJson(
  regionId?: string | null
): Promise<FeatureCollection | null> {
  const resolved = await resolvePreferredPackAndSnapshot(regionId);
  const raw = resolved?.snapshot?.closures;
  const fc = asFeatureCollection(raw);
  if (fc) return fc;
  if (Array.isArray(raw)) {
    const features: Feature[] = [];
    for (const item of raw as ClosureItem[]) {
      if (item.geometry && typeof item.geometry === "object") {
        features.push({
          type: "Feature",
          properties: {
            title: item.title,
            closure_type: item.closure_type,
            severity: item.severity,
            agency: item.agency ?? null,
            is_stale: Boolean(item.is_stale),
            track_id: item.affected_track_ids?.[0] ?? null,
          },
          geometry: item.geometry as Feature["geometry"],
        });
      }
    }
    return { type: "FeatureCollection", features };
  }
  return null;
}

export async function getOfflineReportsGeoJson(
  regionId?: string | null
): Promise<FeatureCollection | null> {
  const resolved = await resolvePreferredPackAndSnapshot(regionId);
  const raw = resolved?.snapshot?.reports;
  const fc = asFeatureCollection(raw);
  if (fc) return fc;
  if (Array.isArray(raw)) {
    const features: Feature<Point>[] = (raw as ConditionReportItem[])
      .filter((r) => Number.isFinite(r.lon) && Number.isFinite(r.lat))
      .map((r) => ({
        type: "Feature",
        id: String(r.id ?? r.report_id ?? ""),
        properties: { ...r },
        geometry: { type: "Point", coordinates: [r.lon, r.lat] },
      }));
    return { type: "FeatureCollection", features };
  }
  return null;
}

export async function getOfflineHotspotsGeoJson(
  regionId?: string | null
): Promise<FeatureCollection | null> {
  const resolved = await resolvePreferredPackAndSnapshot(regionId);
  const raw = resolved?.snapshot?.hotspots;
  const fc = asFeatureCollection(raw);
  if (fc) return fc;
  return null;
}

export async function searchOfflineSnapshots(
  query: string,
  limit = 12
): Promise<ApiSearchResponse | null> {
  const resolved = await resolvePreferredPackAndSnapshot();
  if (!resolved?.snapshot) return null;

  const q = query.trim().toLowerCase();
  if (!q) return { query, total: 0, results: [] };

  const results: ApiSearchResultItem[] = [];
  const snap = resolved.snapshot;

  for (const trk of snap.tracks ?? []) {
    const haystack = [trk.name, ...(trk.alt_names ?? []), trk.primary_park_name ?? ""]
      .join(" ")
      .toLowerCase();
    if (!haystack.includes(q)) continue;
    const bbox = trk.bbox ?? null;
    results.push({
      kind: "track",
      id: trk.track_id,
      name: trk.name,
      state: trk.state,
      subtitle: `${(trk.total_length_m / 1000).toFixed(1)} km · Offline pack`,
      bbox,
      lon: bbox ? (bbox[0] + bbox[2]) / 2 : null,
      lat: bbox ? (bbox[1] + bbox[3]) / 2 : null,
      grade: trk.effective_grade ?? trk.grade ?? null,
    });
  }

  for (const poi of snap.pois ?? []) {
    const haystack = `${poi.name} ${poi.poi_type ?? ""} ${poi.category ?? ""}`.toLowerCase();
    if (!haystack.includes(q)) continue;
    results.push({
      kind: "poi",
      id: String(poi.poi_id ?? poi.id ?? poi.poi_key ?? poi.name),
      name: poi.name,
      state: poi.state ?? null,
      category: poi.category ?? poi.poi_type ?? null,
      elev_m: poi.elevation_m ?? poi.elev_m ?? null,
      lon: poi.lon,
      lat: poi.lat,
    });
  }

  for (const park of snap.capad_parks ?? []) {
    if (!park.name.toLowerCase().includes(q)) continue;
    results.push({
      kind: "park",
      id: String(park.id ?? park.capad_external_id ?? park.name),
      name: park.name,
      state: park.state,
      subtitle: park.park_type,
    });
  }

  const sliced = results.slice(0, limit);
  return {
    query,
    total: results.length,
    results: sliced,
  };
}

export function formatByteSize(bytes: number | null | undefined): string {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes <= 0) {
    return "0 B";
  }
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}
