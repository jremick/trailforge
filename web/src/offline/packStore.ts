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
  OfflinePackShard,
  OfflinePackShardTrack,
  OfflinePackSnapshot,
  OfflinePackTrackIndexEntry,
  ParkTenureItem,
  PoiItem,
  SegmentSummaryItem,
  TileManifestResponse,
  TrackInspectorDetail,
} from "../types";
import { verifySha256Hex } from "./rangeSlice";
import { haversineDistanceM } from "./recorderEngine";

export const OFFLINE_DB_NAME = "trailforge-offline-v1";
export const OFFLINE_DB_VERSION = 2;
export const PACKS_CACHE_NAME = "trailforge-packs-v1";
const ACTIVE_PACK_KEY = "trailforge.active_offline_pack";
const MAX_SHARD_CACHE_ENTRIES = 4;
const SHARD_DOWNLOAD_CONCURRENCY = 4;
const NEARBY_POI_RADIUS_M = 1500;
const MAX_NEARBY_POIS = 25;

export type OfflineStoreName =
  | "packs"
  | "snapshots"
  | "pack_shards"
  | "offline_pins"
  | "sync_queue";

/**
 * Deterministic FNV-1a 32-bit hash over UTF-8 bytes (matches Python backend ``fnv1a_32``).
 */
export function fnv1a32(value: string): number {
  const bytes = new TextEncoder().encode(value);
  let h = 0x811c9dc5;
  for (let i = 0; i < bytes.length; i += 1) {
    h ^= bytes[i]!;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

export function shardIndexForTrack(trackId: string, shardCount: number): number {
  if (shardCount <= 1) return 0;
  return fnv1a32(trackId) % shardCount;
}

export function packShardFilename(shardIndex: number): string {
  return `tracks_${String(shardIndex).padStart(2, "0")}.json.gz`;
}

export function packShardStorageKey(regionId: string, shardIndex: number): string {
  return `${regionId}:${shardIndex}`;
}

async function gunzipToJson<T>(bytes: Uint8Array): Promise<T> {
  if (typeof DecompressionStream === "undefined") {
    throw new Error("DecompressionStream('gzip') is not supported in this browser");
  }
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(bytes);
      controller.close();
    },
  }).pipeThrough(
    new DecompressionStream("gzip") as unknown as TransformStream<
      Uint8Array,
      Uint8Array
    >
  );
  const text = await new Response(stream).text();
  return JSON.parse(text) as T;
}

function isShardedSnapshot(snap: OfflinePackSnapshot | null | undefined): boolean {
  if (!snap) return false;
  return (
    snap.schema_version === "trailforge_pack_snapshot_v2" ||
    (typeof snap.shard_count === "number" && snap.shard_count > 0)
  );
}

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
    ["pack_shards", new Map()],
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
    private readonly stores: readonly OfflineStoreName[] = [
      "packs",
      "snapshots",
      "pack_shards",
    ]
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
const trackIndexMemCache = new Map<
  string,
  Map<string, TrackInspectorDetail | OfflinePackTrackIndexEntry>
>();
const shardMemCache = new Map<string, OfflinePackShard>();
const parkIndexMemCache = new Map<string, Map<number, ParkTenureItem>>();
const closureIndexMemCache = new Map<string, Map<number, ClosureItem>>();

function clearSnapshotMemCache(regionId?: string): void {
  if (regionId) {
    snapshotMemCache.delete(regionId);
    trackIndexMemCache.delete(regionId);
    parkIndexMemCache.delete(regionId);
    closureIndexMemCache.delete(regionId);
    const prefix = `${regionId}:`;
    for (const key of Array.from(shardMemCache.keys())) {
      if (key.startsWith(prefix)) shardMemCache.delete(key);
    }
  } else {
    snapshotMemCache.clear();
    trackIndexMemCache.clear();
    parkIndexMemCache.clear();
    closureIndexMemCache.clear();
    shardMemCache.clear();
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
  parkIndexMemCache.delete(regionId);
  closureIndexMemCache.delete(regionId);
  return snap;
}

function getTrackIndexForSnapshot(
  regionId: string,
  snap: OfflinePackSnapshot
): Map<string, TrackInspectorDetail | OfflinePackTrackIndexEntry> {
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

function getParkIndexForSnapshot(
  regionId: string,
  snap: OfflinePackSnapshot
): Map<number, ParkTenureItem> {
  let idx = parkIndexMemCache.get(regionId);
  if (!idx) {
    idx = new Map();
    for (const park of snap.capad_parks ?? []) {
      if (typeof park.id === "number") {
        idx.set(park.id, park);
      }
    }
    parkIndexMemCache.set(regionId, idx);
  }
  return idx;
}

function getClosureIndexForSnapshot(
  regionId: string,
  snap: OfflinePackSnapshot
): Map<number, ClosureItem> {
  let idx = closureIndexMemCache.get(regionId);
  if (!idx) {
    idx = new Map();
    if (Array.isArray(snap.closures)) {
      for (const item of snap.closures as ClosureItem[]) {
        const cid = item.closure_id ?? (typeof item.id === "number" ? item.id : null);
        if (typeof cid === "number") {
          idx.set(cid, item);
        }
      }
    }
    closureIndexMemCache.set(regionId, idx);
  }
  return idx;
}

async function loadShardCached(
  regionId: string,
  shardIndex: number
): Promise<OfflinePackShard | null> {
  const key = packShardStorageKey(regionId, shardIndex);
  const cached = shardMemCache.get(key);
  if (cached) {
    // Move to MRU position
    shardMemCache.delete(key);
    shardMemCache.set(key, cached);
    return cached;
  }

  const raw = await getOfflineBackend().get<Uint8Array | ArrayBuffer>(
    "pack_shards",
    key
  );
  if (!raw) return null;
  const bytes = raw instanceof Uint8Array ? raw : new Uint8Array(raw);
  const shard = await gunzipToJson<OfflinePackShard>(bytes);

  shardMemCache.set(key, shard);
  while (shardMemCache.size > MAX_SHARD_CACHE_ENTRIES) {
    const oldest = shardMemCache.keys().next().value;
    if (oldest !== undefined) shardMemCache.delete(oldest);
    else break;
  }
  return shard;
}

function computeNearbyPoisForShardedTrack(
  raw: OfflinePackShardTrack,
  pois: readonly PoiItem[] | undefined
): PoiItem[] {
  if (!pois || pois.length === 0) return [];
  const coords = raw.coords ?? [];
  let minLon = Infinity;
  let minLat = Infinity;
  let maxLon = -Infinity;
  let maxLat = -Infinity;

  if (coords.length > 0) {
    for (const pt of coords) {
      if (pt[0] < minLon) minLon = pt[0];
      if (pt[0] > maxLon) maxLon = pt[0];
      if (pt[1] < minLat) minLat = pt[1];
      if (pt[1] > maxLat) maxLat = pt[1];
    }
  } else if (raw.bbox && raw.bbox.length === 4) {
    [minLon, minLat, maxLon, maxLat] = raw.bbox;
  } else {
    return [];
  }

  // Conservative 0.02 deg (~2.2 km) bbox prefilter before exact haversine check
  const padDeg = 0.02;
  const minLonPad = minLon - padDeg;
  const maxLonPad = maxLon + padDeg;
  const minLatPad = minLat - padDeg;
  const maxLatPad = maxLat + padDeg;
  const samplePts: Array<[number, number]> =
    coords.length > 0
      ? coords
      : [[(minLon + maxLon) / 2, (minLat + maxLat) / 2]];

  const hits: Array<{ poi: PoiItem; distM: number }> = [];
  for (const poi of pois) {
    if (
      poi.lon < minLonPad ||
      poi.lon > maxLonPad ||
      poi.lat < minLatPad ||
      poi.lat > maxLatPad
    ) {
      continue;
    }
    let best = Infinity;
    for (const pt of samplePts) {
      const d = haversineDistanceM(poi.lat, poi.lon, pt[1], pt[0]);
      if (d < best) best = d;
    }
    if (best <= NEARBY_POI_RADIUS_M) {
      hits.push({ poi, distM: Math.round(best * 10) / 10 });
    }
  }

  hits.sort((a, b) => a.distM - b.distM || a.poi.name.localeCompare(b.poi.name));
  return hits.slice(0, MAX_NEARBY_POIS).map(({ poi, distM }) => ({
    ...poi,
    distance_m: distM,
  }));
}

function hydrateShardedTrack(
  regionId: string,
  snap: OfflinePackSnapshot,
  raw: OfflinePackShardTrack
): TrackInspectorDetail {
  const parkIndex = getParkIndexForSnapshot(regionId, snap);
  const closureIndex = getClosureIndexForSnapshot(regionId, snap);

  const primaryPark =
    typeof raw.primary_park_id === "number"
      ? (parkIndex.get(raw.primary_park_id) ?? null)
      : null;
  const capadParks: ParkTenureItem[] = [];
  const seenParkIds = new Set<number>();
  for (const pid of raw.park_ids ?? []) {
    if (seenParkIds.has(pid)) continue;
    const pk = parkIndex.get(pid);
    if (pk) {
      seenParkIds.add(pid);
      capadParks.push(pk);
    }
  }
  if (primaryPark && typeof primaryPark.id === "number" && !seenParkIds.has(primaryPark.id)) {
    capadParks.unshift(primaryPark);
  }
  capadParks.sort((a, b) => a.name.localeCompare(b.name));

  const activeClosures: ClosureItem[] = [];
  for (const cid of raw.closure_ids ?? []) {
    const c = closureIndex.get(cid);
    if (c) activeClosures.push(c);
  }

  const conditionReports: ConditionReportItem[] = [];
  const communityGateWarnings: ConditionReportItem[] = [];
  if (Array.isArray(snap.reports)) {
    for (const rep of snap.reports as ConditionReportItem[]) {
      if (rep.track_id !== raw.track_id) continue;
      conditionReports.push(rep);
      if (
        rep.is_gate_warning ||
        rep.report_type === "locked_gate" ||
        rep.report_type === "private_property"
      ) {
        communityGateWarnings.push(rep);
      }
    }
  }

  const corridorSegId = `${raw.track_id}:corridor`;
  const effGrade = raw.effective_grade ?? raw.grade ?? null;
  const elevationProfile = (raw.elevation_profile ?? []).map((p) => ({
    ...p,
    segment_id: p.segment_id ?? corridorSegId,
  }));
  const segments: SegmentSummaryItem[] =
    raw.coords && raw.coords.length >= 2
      ? [
          {
            segment_id: corridorSegId,
            segment_seq: 1,
            name: raw.name,
            length_m: raw.total_length_m,
            max_slope_deg: raw.max_slope_deg ?? null,
            sustained_slope_deg: raw.sustained_slope_deg ?? null,
            elev_min_m: raw.elev_min_m ?? null,
            elev_max_m: raw.elev_max_m ?? null,
            elev_gain_m: raw.elev_gain_m ?? 0,
            elev_loss_m: raw.elev_loss_m ?? 0,
            waterway_crossing_count: raw.waterway_crossing_count ?? 0,
            is_4wd_only: Boolean(raw.is_4wd_only),
            has_gate_barrier: Boolean(raw.has_gate_barrier),
            access_status: raw.access_status ?? "public",
            primary_source: raw.primary_source,
            elevation_profile_m: elevationProfile.map((p) => [p.dist_m, p.elev_m]),
            effective_grade: effGrade,
            grade: effGrade,
            score: raw.score ?? null,
            geometry: {
              type: "LineString",
              coordinates: raw.coords,
            },
          },
        ]
      : [];

  return {
    track_id: raw.track_id,
    name: raw.name,
    alt_names: raw.alt_names ?? [],
    state: raw.state,
    primary_source: raw.primary_source,
    trail_class: raw.trail_class,
    total_length_m: raw.total_length_m,
    elev_min_m: raw.elev_min_m ?? null,
    elev_max_m: raw.elev_max_m ?? null,
    min_elev_m: raw.elev_min_m ?? null,
    max_elev_m: raw.elev_max_m ?? null,
    elev_gain_m: raw.elev_gain_m ?? 0,
    elev_loss_m: raw.elev_loss_m ?? 0,
    max_slope_deg: raw.max_slope_deg ?? null,
    sustained_slope_deg: raw.sustained_slope_deg ?? null,
    waterway_crossing_count: raw.waterway_crossing_count ?? 0,
    is_4wd_only: Boolean(raw.is_4wd_only),
    has_gate_barrier: Boolean(raw.has_gate_barrier),
    access_status: raw.access_status ?? "public",
    primary_park_name: raw.primary_park_name ?? primaryPark?.name ?? null,
    primary_park: primaryPark,
    capad_parks: capadParks,
    effective_grade: effGrade,
    grade: effGrade,
    score: raw.score ?? null,
    live_grade: raw.live_grade ?? null,
    live_score: raw.live_score ?? null,
    confidence_level: raw.confidence_level ?? "none",
    sample_count: raw.sample_count ?? 0,
    last_driven_at: raw.last_driven_at ?? null,
    avg_speed_kmh: raw.avg_speed_kmh ?? null,
    est_duration_min: raw.est_duration_min ?? null,
    vehicle_class_split: raw.vehicle_class_split ?? {},
    live_breakdown: raw.live_breakdown ?? null,
    rainfall_context: raw.rainfall_context ?? null,
    obstacle_hotspots: raw.obstacle_hotspots ?? [],
    bbox: raw.bbox ?? null,
    baseline_breakdown: raw.baseline_breakdown ?? null,
    elevation_profile: elevationProfile,
    waterway_crossings: raw.waterway_crossings ?? [],
    segment_count: raw.segment_count ?? segments.length,
    closure_status: raw.closure_status ?? "open",
    active_closures: activeClosures,
    segments,
    nearby_pois: computeNearbyPoisForShardedTrack(raw, snap.pois),
    condition_reports: conditionReports,
    community_gate_warnings: communityGateWarnings,
  };
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
  const existingSnap = await backend.get<OfflinePackSnapshot>("snapshots", regionId);
  const shardCount = Math.max(
    existing?.shard_count ?? 0,
    existingSnap?.shard_count ?? 0
  );
  await backend.delete("packs", regionId);
  await backend.delete("snapshots", regionId);
  if (shardCount > 0) {
    await Promise.all(
      Array.from({ length: shardCount }, (_, idx) =>
        backend.delete("pack_shards", packShardStorageKey(regionId, idx))
      )
    );
  }
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

  const isSharded =
    manifest.snapshot_format === "sharded_v2" || isShardedSnapshot(snapshot);
  const shardCount = isSharded
    ? Math.max(0, Number(manifest.shard_count ?? snapshot.shard_count ?? 0))
    : 0;
  const backend = getOfflineBackend();
  const existingPack = await getDownloadedPack(region.region_id);

  let allVerified = snapCheck === "verified";
  let cachedBytes = snapBuf.byteLength;
  const writtenShardKeys: string[] = [];

  if (shardCount > 0) {
    const shardsBaseUrl = resolvePackAssetUrl(
      region.shards_base_url ?? manifest.shards_base_url,
      `/api/packs/${rid}/shards`
    ).replace(/\/+$/, "");

    let nextIdx = 0;
    let completed = 0;
    const workerCount = Math.min(SHARD_DOWNLOAD_CONCURRENCY, shardCount);

    const downloadWorker = async (): Promise<void> => {
      while (true) {
        const idx = nextIdx;
        nextIdx += 1;
        if (idx >= shardCount) return;

        const fname = packShardFilename(idx);
        const shardUrl = `${shardsBaseUrl}/${fname}`;
        const res = await fetch(shardUrl, { cache: "no-store" });
        if (!res.ok) {
          throw new Error(`Shard ${fname} fetch failed (HTTP ${res.status})`);
        }
        const buf = await res.arrayBuffer();
        const entry = findManifestFileEntry(manifest, `shards/${fname}`);
        const check = await verifySha256Hex(buf, entry?.sha256);
        if (check === "mismatch") {
          allVerified = false;
          throw new Error(`Shard ${fname} SHA-256 verification failed`);
        }
        if (check !== "verified") {
          allVerified = false;
        }
        const key = packShardStorageKey(region.region_id, idx);
        await backend.put("pack_shards", key, new Uint8Array(buf));
        writtenShardKeys.push(key);
        cachedBytes += buf.byteLength;
        completed += 1;
        const pct = Math.round(32 + (completed / shardCount) * 26);
        onProgress?.({
          stage: `Downloading track shards (${completed}/${shardCount})…`,
          pct,
        });
      }
    };

    try {
      await Promise.all(Array.from({ length: workerCount }, () => downloadWorker()));
    } catch (err) {
      await Promise.all(
        writtenShardKeys.map((k) => backend.delete("pack_shards", k))
      );
      clearSnapshotMemCache(region.region_id);
      throw err;
    }
  }

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
    if (err instanceof Error && err.message.includes("SHA-256")) {
      await Promise.all(
        writtenShardKeys.map((k) => backend.delete("pack_shards", k))
      );
      throw err;
    }
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
    if (err instanceof Error && err.message.includes("SHA-256")) {
      await Promise.all(
        writtenShardKeys.map((k) => backend.delete("pack_shards", k))
      );
      throw err;
    }
  }

  // Clean up any stale surplus shards if the region sharded count shrank on update
  const prevShardCount = existingPack?.shard_count ?? 0;
  if (prevShardCount > shardCount) {
    await Promise.all(
      Array.from({ length: prevShardCount - shardCount }, (_, offset) =>
        backend.delete(
          "pack_shards",
          packShardStorageKey(region.region_id, shardCount + offset)
        )
      )
    );
  }

  const counts = manifest.counts ?? {};
  const record: DownloadedPackRecord = {
    region_id: region.region_id,
    name: manifest.name ?? region.name,
    version: manifest.version ?? region.version ?? "1",
    downloaded_at: new Date().toISOString(),
    snapshot_format: shardCount > 0 ? "sharded_v2" : "inline_v1",
    shard_count: shardCount,
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
  clearSnapshotMemCache(region.region_id);
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

async function resolveTrackFromPackSnapshot(
  regionId: string,
  snap: OfflinePackSnapshot,
  trackId: string
): Promise<TrackInspectorDetail | null> {
  const indexHit = getTrackIndexForSnapshot(regionId, snap).get(trackId);
  if (!indexHit) return null;

  if (isShardedSnapshot(snap)) {
    const shardCount = Math.max(1, Number(snap.shard_count ?? 0));
    const shardIdx = shardIndexForTrack(trackId, shardCount);
    const shard = await loadShardCached(regionId, shardIdx);
    const shardTrack = shard?.tracks?.[trackId];
    if (!shardTrack) return null;
    return hydrateShardedTrack(regionId, snap, shardTrack);
  }

  return indexHit as TrackInspectorDetail;
}

export async function getOfflineTrackDetail(
  trackId: string
): Promise<TrackInspectorDetail | null> {
  const activeId = getActiveOfflinePackId();

  if (activeId) {
    const activeSnap = await loadSnapshotCached(activeId);
    if (activeSnap) {
      const hit = await resolveTrackFromPackSnapshot(activeId, activeSnap, trackId);
      if (hit) return hit;
    }
  }

  const packs = await listDownloadedPacks();
  for (const pack of packs) {
    if (pack.region_id === activeId) continue;
    const snap = await loadSnapshotCached(pack.region_id);
    if (!snap) continue;
    const hit = await resolveTrackFromPackSnapshot(pack.region_id, snap, trackId);
    if (hit) return hit;
  }
  return null;
}

export async function getOfflineTracksGeoJson(
  regionId?: string | null
): Promise<FeatureCollection | null> {
  const resolved = await resolvePreferredPackAndSnapshot(regionId);
  const snap = resolved?.snapshot;
  if (!snap || isShardedSnapshot(snap)) return null;
  const tracks = snap.tracks;
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
