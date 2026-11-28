import type { Feature, FeatureCollection, Point } from "geojson";
import {
  buildOfflinePackTileCachePath,
  resolvePackAssetUrl,
} from "../adminHelpers";
import { DEFAULT_SNOWLINE_BANDS_M } from "../presets";
import type {
  ApiSearchResponse,
  ApiSearchResultItem,
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
export const OFFLINE_PACK_CHANGE_KEY = "trailforge.offline_pack_change";
const MAX_SHARD_CACHE_ENTRIES = 4;
const SHARD_DOWNLOAD_CONCURRENCY = 4;
// Matches MAX_SHARD_COUNT in the pack builder.
const MAX_PACK_SHARDS = 64;
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

function packSnapshotStorageKey(regionId: string, generation?: string): string {
  return generation ? `${regionId}:${generation}` : regionId;
}

export function packShardStorageKey(regionId: string, shardIndex: number, generation?: string): string {
  return `${packSnapshotStorageKey(regionId, generation)}:${shardIndex}`;
}

function packOperationKey(regionId: string): string {
  // This local key shares the snapshot store without changing legacy stores or records.
  return `!operation:${regionId}`;
}

function newPackGeneration(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
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

interface PackStorageState {
  installed: string | undefined;
  pending: string | undefined;
  snapshotKeys: string[];
  shardKeys: string[];
}

export interface OfflineKvBackend {
  get<T>(store: OfflineStoreName, key: string): Promise<T | null>;
  getAll<T>(store: OfflineStoreName): Promise<T[]>;
  put<T>(store: OfflineStoreName, key: string, value: T): Promise<void>;
  delete(store: OfflineStoreName, key: string): Promise<void>;
  inspectPackStorage(regionId: string): Promise<PackStorageState>;
  completePackOperation(
    regionId: string,
    operation: string,
    record?: DownloadedPackRecord | null
  ): Promise<{ previous: DownloadedPackRecord | null } | null>;
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

  async inspectPackStorage(regionId: string): Promise<PackStorageState> {
    const prefix = `${regionId}:`;
    const snapshots = this.tables.get("snapshots")!;
    const shards = this.tables.get("pack_shards")!;
    const record = this.tables.get("packs")!.get(regionId) as DownloadedPackRecord | undefined;
    return {
      installed: record?.storage_generation,
      pending: snapshots.get(packOperationKey(regionId)) as string | undefined,
      snapshotKeys: Array.from(snapshots.keys()).filter((key) => key.startsWith(prefix)),
      shardKeys: Array.from(shards.keys()).filter((key) => key.startsWith(prefix)),
    };
  }

  async completePackOperation(
    regionId: string,
    operation: string,
    record?: DownloadedPackRecord | null
  ): Promise<{ previous: DownloadedPackRecord | null } | null> {
    const snapshots = this.tables.get("snapshots")!;
    const packs = this.tables.get("packs")!;
    const opKey = packOperationKey(regionId);
    if (snapshots.get(opKey) !== operation) return null;
    const previous = (packs.get(regionId) as DownloadedPackRecord | undefined) ?? null;
    if (record) packs.set(regionId, structuredClone(record));
    else if (record === null) packs.delete(regionId);
    snapshots.delete(opKey);
    return { previous: structuredClone(previous) };
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
      tx.onabort = () => reject(tx.error ?? new Error("Offline storage write aborted"));
    });
  }

  async delete(store: OfflineStoreName, key: string): Promise<void> {
    const db = await this.open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(store, "readwrite");
      tx.objectStore(store).delete(key);
      tx.oncomplete = () => resolve();
      tx.onabort = () => reject(tx.error ?? new Error("Offline storage removal aborted"));
    });
  }

  async inspectPackStorage(regionId: string): Promise<PackStorageState> {
    const db = await this.open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(["packs", "snapshots", "pack_shards"], "readonly");
      const snapshots = tx.objectStore("snapshots");
      const prefix = `${regionId}:`;
      const namespaced = IDBKeyRange.bound(prefix, `${regionId};`, false, true);
      const record = tx.objectStore("packs").get(regionId);
      const pending = snapshots.get(packOperationKey(regionId));
      const snapshotKeys = snapshots.getAllKeys(namespaced);
      const shardKeys = tx.objectStore("pack_shards").getAllKeys(namespaced);
      tx.oncomplete = () => resolve({
        installed: (record.result as DownloadedPackRecord | undefined)?.storage_generation,
        pending: pending.result as string | undefined,
        snapshotKeys: snapshotKeys.result.filter((key) => typeof key === "string"),
        shardKeys: shardKeys.result.filter((key) => typeof key === "string"),
      });
      tx.onabort = () => reject(tx.error ?? new Error("Offline pack storage query aborted"));
    });
  }

  async completePackOperation(
    regionId: string,
    operation: string,
    record?: DownloadedPackRecord | null
  ): Promise<{ previous: DownloadedPackRecord | null } | null> {
    const db = await this.open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(["packs", "snapshots"], "readwrite");
      const snapshots = tx.objectStore("snapshots");
      const packs = tx.objectStore("packs");
      const opKey = packOperationKey(regionId);
      let result: { previous: DownloadedPackRecord | null } | null = null;
      const abortWith = (err: unknown): void => {
        try {
          tx.abort();
        } catch {
          // Transaction may already be aborting.
        }
        reject(err);
      };
      const pending = snapshots.get(opKey);
      pending.onsuccess = () => {
        try {
          if (pending.result !== operation) return;
          const current = packs.get(regionId);
          current.onsuccess = () => {
            try {
              if (record) packs.put(record, regionId);
              else if (record === null) packs.delete(regionId);
              snapshots.delete(opKey);
              result = { previous: current.result ?? null };
            } catch (err) {
              abortWith(err);
            }
          };
        } catch (err) {
          abortWith(err);
        }
      };
      tx.oncomplete = () => resolve(result);
      tx.onabort = () => reject(tx.error ?? new Error("Offline pack activation aborted"));
    });
  }
}

let activeBackend: OfflineKvBackend | null = null;
const privateBackends = new Map<string, OfflineKvBackend>();
let memoryActivePackId: string | null = null;
// In-memory cache of deserialized offline pack snapshots so rapid search keystrokes and
// track clicks do not structured-clone multi-MB IndexedDB objects on every interaction.
const snapshotMemCache = new Map<string, { key: string; snapshot: OfflinePackSnapshot }>();
const trackIndexMemCache = new WeakMap<
  OfflinePackSnapshot,
  Map<string, TrackInspectorDetail | OfflinePackTrackIndexEntry>
>();
const shardMemCache = new Map<string, OfflinePackShard>();
const parkIndexMemCache = new WeakMap<OfflinePackSnapshot, Map<number, ParkTenureItem>>();
const closureIndexMemCache = new WeakMap<OfflinePackSnapshot, Map<number, ClosureItem>>();

function clearSnapshotMemCache(regionId?: string): void {
  if (regionId) {
    snapshotMemCache.delete(regionId);
    const prefix = `${regionId}:`;
    for (const key of Array.from(shardMemCache.keys())) {
      if (key.startsWith(prefix)) shardMemCache.delete(key);
    }
  } else {
    snapshotMemCache.clear();
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
  pack: DownloadedPackRecord
): Promise<OfflinePackSnapshot | null> {
  const key = packSnapshotStorageKey(pack.region_id, pack.storage_generation);
  const cached = snapshotMemCache.get(pack.region_id);
  if (cached?.key === key) return cached.snapshot;
  const snapshot = await getOfflineBackend().get<OfflinePackSnapshot>("snapshots", key);
  if (snapshot) snapshotMemCache.set(pack.region_id, { key, snapshot });
  return snapshot;
}

function getTrackIndexForSnapshot(
  snap: OfflinePackSnapshot
): Map<string, TrackInspectorDetail | OfflinePackTrackIndexEntry> {
  let idx = trackIndexMemCache.get(snap);
  if (!idx) {
    idx = new Map();
    for (const trk of snap.tracks ?? []) {
      idx.set(trk.track_id, trk);
    }
    trackIndexMemCache.set(snap, idx);
  }
  return idx;
}

function getParkIndexForSnapshot(
  snap: OfflinePackSnapshot
): Map<number, ParkTenureItem> {
  let idx = parkIndexMemCache.get(snap);
  if (!idx) {
    idx = new Map();
    for (const park of snap.capad_parks ?? []) {
      if (typeof park.id === "number") {
        idx.set(park.id, park);
      }
    }
    parkIndexMemCache.set(snap, idx);
  }
  return idx;
}

function getClosureIndexForSnapshot(
  snap: OfflinePackSnapshot
): Map<number, ClosureItem> {
  let idx = closureIndexMemCache.get(snap);
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
    closureIndexMemCache.set(snap, idx);
  }
  return idx;
}

async function loadShardCached(
  pack: DownloadedPackRecord,
  shardIndex: number
): Promise<OfflinePackShard | null> {
  const key = packShardStorageKey(pack.region_id, shardIndex, pack.storage_generation);
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
  snap: OfflinePackSnapshot,
  raw: OfflinePackShardTrack
): TrackInspectorDetail {
  const parkIndex = getParkIndexForSnapshot(snap);
  const closureIndex = getClosureIndexForSnapshot(snap);

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

type PackStorage = Pick<DownloadedPackRecord,
  | "region_id"
  | "storage_generation"
  | "shard_count"
  | "vector_pmtiles_url"
  | "terrain_pmtiles_url"
>;

async function discardPackStorage(backend: OfflineKvBackend, storage: PackStorage): Promise<void> {
  const { region_id: regionId, storage_generation: generation } = storage;
  const snapshotKey = packSnapshotStorageKey(regionId, generation);
  const oldSnapshot = generation === undefined
    ? await backend.get<OfflinePackSnapshot>("snapshots", snapshotKey).catch(() => null)
    : null;
  const count = Math.min(MAX_PACK_SHARDS, Math.max(storage.shard_count ?? 0, oldSnapshot?.shard_count ?? 0));

  const deletes = [backend.delete("snapshots", snapshotKey)];
  for (let idx = 0; idx < count; idx += 1) {
    deletes.push(backend.delete("pack_shards", packShardStorageKey(regionId, idx, generation)));
  }
  if (typeof caches !== "undefined") {
    const paths = new Set([
      storage.vector_pmtiles_url ?? buildOfflinePackTileCachePath(regionId, "vector", generation),
      storage.terrain_pmtiles_url ?? buildOfflinePackTileCachePath(regionId, "terrain", generation),
    ]);
    deletes.push(caches.open(PACKS_CACHE_NAME).then(async (cache) => {
      await Promise.all(Array.from(paths, (path) => cache.delete(path)));
    }));
  }
  await Promise.allSettled(deletes);
}

async function reclaimAbandonedPackStorage(backend: OfflineKvBackend, regionId: string): Promise<void> {
  let cache: Cache | undefined;
  let tileKeys: readonly Request[] = [];
  if (typeof caches !== "undefined") {
    try {
      cache = await caches.open(PACKS_CACHE_NAME);
      tileKeys = await cache.keys();
    } catch {
      // IndexedDB cleanup can still run when CacheStorage is unavailable.
    }
  }

  // Read both protected generations atomically. A generation collected here
  // can never be installed later because its operation token is already stale.
  const state = await backend.inspectPackStorage(regionId).catch(() => null);
  if (!state) return;
  const abandoned = (generation: string): boolean =>
    /^[a-f0-9]{32}$/.test(generation) &&
    generation !== state.installed && generation !== state.pending;
  const prefix = `${regionId}:`;
  const drops: Promise<unknown>[] = [];
  for (const key of state.snapshotKeys) {
    if (abandoned(key.slice(prefix.length))) drops.push(backend.delete("snapshots", key));
  }
  for (const key of state.shardKeys) {
    const [generation] = key.slice(prefix.length).split(":", 1);
    if (generation && abandoned(generation)) drops.push(backend.delete("pack_shards", key));
  }
  const tilePrefix = `/api/packs/${encodeURIComponent(regionId)}/tiles/`;
  for (const key of tileKeys) {
    const { pathname } = new URL(key.url);
    if (!pathname.startsWith(tilePrefix)) continue;
    const match = /^([a-f0-9]{32})\/(vector|terrain)\.pmtiles$/.exec(pathname.slice(tilePrefix.length));
    if (match && abandoned(match[1]!)) drops.push(cache!.delete(key));
  }
  await Promise.allSettled(drops);
  if (drops.length > 0) notifySwInvalidatePackCache(regionId);
}

export function clearOfflinePackMemoryCache(regionId?: string): void {
  clearSnapshotMemCache(regionId);
}

function notifyPackChange(regionId: string, operation: string): void {
  clearSnapshotMemCache(regionId);
  try {
    if (typeof window !== "undefined") {
      window.dispatchEvent(new Event(OFFLINE_PACK_CHANGE_KEY));
      window.localStorage.setItem(OFFLINE_PACK_CHANGE_KEY, operation);
    }
  } catch {
    // Other tabs can still resolve the installed generation on their next read.
  }
}

export async function removeOfflinePack(regionId: string): Promise<void> {
  const backend = getOfflineBackend();
  const operation = newPackGeneration();
  await backend.put("snapshots", packOperationKey(regionId), operation);
  const committed = await backend.completePackOperation(regionId, operation, null);
  if (!committed) throw new Error("Another operation has taken over this offline pack");
  if (getActiveOfflinePackId() === regionId) setActiveOfflinePackId(null);
  notifyPackChange(regionId, operation);

  if (committed.previous) await discardPackStorage(backend, committed.previous);
  await reclaimAbandonedPackStorage(backend, regionId);
  notifySwInvalidatePackCache(regionId);
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
 * Download under fresh keys, then publish the installed pointer in one IndexedDB
 * transaction. A later download or removal can supersede the pending operation.
 */
export async function downloadRegionalPackToDevice(
  region: OfflinePackRegionItem,
  onProgress?: (progress: PackDownloadProgress) => void
): Promise<DownloadedPackRecord> {
  const regionId = region.region_id;
  const rid = encodeURIComponent(regionId);
  const generation = newPackGeneration();
  const backend = getOfflineBackend();
  const storage: PackStorage = {
    region_id: regionId,
    storage_generation: generation,
    shard_count: 0,
    vector_pmtiles_url: buildOfflinePackTileCachePath(regionId, "vector", generation),
    terrain_pmtiles_url: buildOfflinePackTileCachePath(regionId, "terrain", generation),
  };
  await backend.put("snapshots", packOperationKey(regionId), generation);

  let record: DownloadedPackRecord;
  let previous: DownloadedPackRecord | null;
  try {
    await reclaimAbandonedPackStorage(backend, regionId);
    const manifestUrl = resolvePackAssetUrl(region.manifest_url, `/api/packs/${rid}/manifest.json`);
    const snapshotUrl = resolvePackAssetUrl(region.snapshot_url, `/api/packs/${rid}/snapshot.json`);

    onProgress?.({ stage: "Fetching pack manifest…", pct: 10 });
    const manifestRes = await fetch(manifestUrl, { cache: "no-store" });
    if (!manifestRes.ok) throw new Error(`Manifest fetch failed (HTTP ${manifestRes.status})`);
    const manifest = (await manifestRes.json()) as OfflinePackManifest;
    if (manifest.region_id !== regionId || (region.community_telemetry_policy && (
      manifest.community_telemetry_policy !== region.community_telemetry_policy ||
      (manifest.community_telemetry_revision ?? null) !== (region.community_telemetry_revision ?? null)
    ))) {
      throw new Error("Pack contents have changed. Refresh the catalog and try the download again.");
    }

    let allVerified = true;
    let cachedBytes = 0;
    const verifyFile = async (name: string, bytes: ArrayBuffer): Promise<void> => {
      const entry = findManifestFileEntry(manifest, name);
      const check = await verifySha256Hex(bytes, entry?.sha256);
      if (check === "mismatch") throw new Error(`${name} SHA-256 verification failed`);
      if (entry?.size_bytes !== undefined && bytes.byteLength !== entry.size_bytes) {
        throw new Error(`${name} size does not match the pack manifest`);
      }
      allVerified &&= check === "verified";
      cachedBytes += bytes.byteLength;
    };

    onProgress?.({ stage: "Downloading offline track & POI snapshot…", pct: 32 });
    const snapRes = await fetch(snapshotUrl, { cache: "no-store" });
    if (!snapRes.ok) throw new Error(`Snapshot fetch failed (HTTP ${snapRes.status})`);
    const snapBuf = await snapRes.arrayBuffer();
    await verifyFile("snapshot.json", snapBuf);
    const snapshot = JSON.parse(new TextDecoder().decode(snapBuf)) as OfflinePackSnapshot;
    if (snapshot.region_id !== regionId || (
      manifest.version !== undefined && snapshot.version !== undefined &&
      manifest.version !== snapshot.version
    )) {
      throw new Error("Offline snapshot does not match the pack manifest");
    }
    const sharded = manifest.snapshot_format === "sharded_v2" || isShardedSnapshot(snapshot);
    const shardCount = sharded ? manifest.shard_count ?? snapshot.shard_count ?? 0 : 0;
    if (sharded) {
      if (!Number.isSafeInteger(shardCount) || shardCount < 1 || shardCount > MAX_PACK_SHARDS ||
        (snapshot.shard_count !== undefined && snapshot.shard_count !== shardCount)) {
        throw new Error("Offline snapshot shard count does not match the pack manifest");
      }
      storage.shard_count = shardCount;
      snapshot.shard_count = shardCount;
      const shardsBaseUrl = resolvePackAssetUrl(
        region.shards_base_url ?? manifest.shards_base_url,
        `/api/packs/${rid}/shards`
      ).replace(/\/+$/, "");
      const controller = new AbortController();
      let nextIdx = 0;
      let completed = 0;
      const downloadWorker = async (): Promise<void> => {
        try {
          while (!controller.signal.aborted && nextIdx < shardCount) {
            const idx = nextIdx++;
            const fname = packShardFilename(idx);
            const res = await fetch(`${shardsBaseUrl}/${fname}`, { cache: "no-store", signal: controller.signal });
            if (!res.ok) throw new Error(`Shard ${fname} fetch failed (HTTP ${res.status})`);
            const buf = await res.arrayBuffer();
            await verifyFile(`shards/${fname}`, buf);
            controller.signal.throwIfAborted();
            await backend.put("pack_shards", packShardStorageKey(regionId, idx, generation), new Uint8Array(buf));
            completed += 1;
            onProgress?.({
              stage: `Downloading track shards (${completed}/${shardCount})…`,
              pct: Math.round(32 + (completed / shardCount) * 26),
            });
          }
        } catch (err) {
          controller.abort(err);
        }
      };

      await Promise.all(Array.from(
        { length: Math.min(SHARD_DOWNLOAD_CONCURRENCY, shardCount) },
        () => downloadWorker()
      ));
      controller.signal.throwIfAborted();
    }

    const cacheTile = async (
      kind: "vector" | "terrain",
      declaredAvailable: boolean | undefined,
      offeredUrl: string | null | undefined,
      stage: string,
      pct: number
    ): Promise<string | null> => {
      const filename = `${kind}.pmtiles`;
      const declared = declaredAvailable === true || Boolean(findManifestFileEntry(manifest, filename));
      if (declaredAvailable === false && !declared) return null;
      if (declared && typeof caches === "undefined") {
        throw new Error("Offline tile storage is unavailable in this browser");
      }
      const livePath = buildOfflinePackTileCachePath(regionId, kind);
      const tileUrl = resolvePackAssetUrl(offeredUrl, livePath);
      onProgress?.({ stage, pct });
      const res = await fetch(tileUrl, { cache: "no-store" });
      if (res.headers.get("X-TrailForge-Offline-Cache") === "HIT") {
        throw new Error("Reload TrailForge before updating this offline pack");
      }
      if (!declared && (res.status === 404 || res.status === 410)) return null;
      if (res.status !== 200) throw new Error(`${filename} fetch failed (HTTP ${res.status})`);
      const buf = await res.arrayBuffer();
      if (buf.byteLength === 0) throw new Error(`${filename} is empty`);
      await verifyFile(filename, buf);

      if (typeof caches === "undefined") {
        throw new Error("Offline tile storage is unavailable in this browser");
      }
      const cache = await caches.open(PACKS_CACHE_NAME);
      const cachePath = buildOfflinePackTileCachePath(regionId, kind, generation);
      await cache.put(cachePath, new Response(buf, {
        headers: {
          "Content-Type": "application/vnd.pmtiles",
          "Content-Length": String(buf.byteLength),
        },
      }));
      return cachePath;
    };

    const vectorPath = await cacheTile(
      "vector", manifest.vector_available,
      region.vector_pmtiles_url ?? manifest.vector_pmtiles_url,
      "Caching vector PMTiles for offline Range 206…", 62
    );
    const terrainPath = await cacheTile(
      "terrain", manifest.terrain_available,
      region.terrain_pmtiles_url ?? manifest.terrain_pmtiles_url,
      "Caching terrain hillshade PMTiles…", 86
    );

    const counts = manifest.counts ?? {};
    record = {
      region_id: regionId,
      storage_generation: generation,
      name: manifest.name ?? region.name,
      version: manifest.version ?? region.version ?? "1",
      downloaded_at: new Date().toISOString(),
      snapshot_format: sharded ? "sharded_v2" : "inline_v1",
      shard_count: shardCount,
      total_size_bytes: manifest.total_size_bytes ?? region.total_size_bytes ?? cachedBytes,
      track_count: counts.tracks ?? region.track_count ?? snapshot.tracks?.length ?? 0,
      segment_count: counts.segments ?? region.segment_count ?? 0,
      poi_count: counts.pois ?? region.poi_count ?? snapshot.pois?.length ?? 0,
      bbox: manifest.bounds ?? manifest.bbox ?? region.bbox,
      vector_pmtiles_url: vectorPath,
      terrain_pmtiles_url: terrainPath,
      has_vector_pmtiles: vectorPath !== null,
      has_terrain_pmtiles: terrainPath !== null,
      snowline_bands_m: manifest.snowline_bands_m ?? [...DEFAULT_SNOWLINE_BANDS_M],
      attributions: manifest.attributions ?? snapshot.attributions ?? region.attributions ?? [],
      sha256_verified: allVerified,
    };

    onProgress?.({ stage: "Saving pack to IndexedDB…", pct: 96 });
    await backend.put("snapshots", packSnapshotStorageKey(regionId, generation), snapshot);
    const committed = await backend.completePackOperation(regionId, generation, record);
    if (!committed) throw new Error("Another operation has taken over this offline pack");
    previous = committed.previous;
  } catch (err) {
    await discardPackStorage(backend, storage);
    await backend.completePackOperation(regionId, generation).catch(() => undefined);
    throw err;
  }

  notifyPackChange(regionId, generation);
  if (previous) await discardPackStorage(backend, previous);
  notifySwInvalidatePackCache(regionId);
  try {
    onProgress?.({ stage: "Offline pack ready", pct: 100 });
  } catch {
    // The installed pointer already committed successfully.
  }
  return record;
}

/* ---------- Offline Snapshot Query Helpers (used when offline or offline pack active) ---------- */

async function resolvePreferredPack(regionId?: string | null): Promise<DownloadedPackRecord | null> {
  const targetId = regionId ?? getActiveOfflinePackId();
  const preferred = targetId ? await getDownloadedPack(targetId) : null;
  return preferred ?? (await listDownloadedPacks())[0] ?? null;
}

async function readPackSnapshot<T>(
  pack: DownloadedPackRecord,
  read: (pack: DownloadedPackRecord, snapshot: OfflinePackSnapshot) => Promise<T | null>
): Promise<T | null> {
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const snapshot = await loadSnapshotCached(pack);
    const result = snapshot ? await read(pack, snapshot) : null;
    if (result !== null) return result;

    // The previously selected generation may have been removed while the
    // snapshot or shard read was running. Retry using any newly installed one.
    const latest = await getDownloadedPack(pack.region_id);
    if (!latest || latest.storage_generation === pack.storage_generation) return null;
    pack = latest;
  }
  return null;
}

async function resolvePreferredPackAndSnapshot(regionId?: string | null): Promise<{
  pack: DownloadedPackRecord;
  snapshot: OfflinePackSnapshot;
} | null> {
  const pack = await resolvePreferredPack(regionId);
  return pack
    ? readPackSnapshot(pack, async (pack, snapshot) => ({ pack, snapshot }))
    : null;
}

export async function getOfflineTileManifest(
  regionId?: string | null
): Promise<TileManifestResponse | null> {
  const pack = await resolvePreferredPack(regionId);
  if (!pack) return null;
  const origin = typeof window === "undefined" ? "" : (window.location?.origin ?? "");
  const cacheUrl = (url: string): string => /^https?:\/\//.test(origin)
    ? new URL(url, origin).href
    : url;
  return {
    generated_at: pack.downloaded_at,
    bounds: pack.bbox,
    minzoom: 6,
    maxzoom: 15,
    vector_url: cacheUrl(pack.vector_pmtiles_url ?? buildOfflinePackTileCachePath(pack.region_id, "vector")),
    terrain_url: cacheUrl(pack.terrain_pmtiles_url ?? buildOfflinePackTileCachePath(pack.region_id, "terrain")),
    vector_available: pack.has_vector_pmtiles,
    terrain_available: pack.has_terrain_pmtiles,
    offline_pack: true,
    snowline_bands_m: pack.snowline_bands_m,
    attributions: pack.attributions,
  };
}

async function resolveTrackFromPackSnapshot(
  pack: DownloadedPackRecord,
  snap: OfflinePackSnapshot,
  trackId: string
): Promise<TrackInspectorDetail | null> {
  const indexHit = getTrackIndexForSnapshot(snap).get(trackId);
  if (!indexHit) return null;

  if (isShardedSnapshot(snap)) {
    const shardCount = Math.max(1, Number(snap.shard_count ?? 0));
    const shardIdx = shardIndexForTrack(trackId, shardCount);
    const shard = await loadShardCached(pack, shardIdx);
    const shardTrack = shard?.tracks?.[trackId];
    if (!shardTrack) return null;
    return hydrateShardedTrack(snap, shardTrack);
  }

  return indexHit as TrackInspectorDetail;
}

export async function getOfflineTrackDetail(
  trackId: string
): Promise<TrackInspectorDetail | null> {
  const activeId = getActiveOfflinePackId();
  const packs = await listDownloadedPacks();
  const activeIndex = packs.findIndex((pack) => pack.region_id === activeId);
  if (activeIndex > 0) packs.unshift(packs.splice(activeIndex, 1)[0]!);
  for (const pack of packs) {
    const hit = await readPackSnapshot(pack, (pack, snapshot) =>
      resolveTrackFromPackSnapshot(pack, snapshot, trackId)
    );
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
