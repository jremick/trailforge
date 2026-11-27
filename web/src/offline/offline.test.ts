import { afterEach, describe, expect, it, vi } from "vitest";
import { buildPlannedRouteMapGeoJson, planRouteFromOfflineStore } from "../routeHelpers";
import type { OfflinePackSnapshot, RecordedBreadcrumbPoint } from "../types";
import {
  buildClientGeoJson,
  buildClientGpx,
  slugifyExportFilename,
} from "./exporters";
import {
  MemoryOfflineBackend,
  downloadRegionalPackToDevice,
  fnv1a32,
  getOfflineClosuresGeoJson,
  getOfflineHotspotsGeoJson,
  getOfflinePois,
  getOfflineReportsGeoJson,
  getOfflineTileManifest,
  getOfflineTrackDetail,
  getOfflineTracksGeoJson,
  packShardFilename,
  packShardStorageKey,
  removeOfflinePack,
  saveOfflinePackData,
  searchOfflineSnapshots,
  setActiveOfflinePackId,
  setOfflineBackendForTesting,
  shardIndexForTrack,
} from "./packStore";
import {
  computeSha256Hex,
  parseByteRange,
  sliceArrayBufferResponse,
  verifySha256Hex,
} from "./rangeSlice";
import {
  SIMULATED_HIGH_COUNTRY_WAYPOINTS,
  buildBacktrackGuidance,
  buildRecordedTrackGeoJson,
  computeBearingDeg,
  computeRecorderStats,
  computeRoughnessRms,
  formatCompassCardinal,
  haversineDistanceM,
} from "./recorderEngine";
import {
  deleteQueuedPin,
  enqueueOfflinePin,
  enqueueOfflineReport,
  enqueueOfflineTrip,
  flushSyncQueue,
  listSyncQueue,
  pinsFromSyncQueue,
} from "./syncQueue";

describe("Slice 09 HTTP 206 Range slicing & SHA-256 verification", () => {
  it("parses RFC 7233 byte ranges and returns valid 206 / 200 / 416 responses", async () => {
    const payload = new TextEncoder().encode("PMTiles-0123456789ABCDEF");
    expect(parseByteRange("bytes=0-6", payload.byteLength)).toEqual({
      status: 206,
      start: 0,
      end: 6,
    });
    expect(parseByteRange("bytes=-6", payload.byteLength)).toEqual({
      status: 206,
      start: payload.byteLength - 6,
      end: payload.byteLength - 1,
    });
    expect(parseByteRange("bytes=999-1000", payload.byteLength)).toEqual({
      status: 416,
    });

    const partial = sliceArrayBufferResponse(payload, "bytes=0-6", {
      contentType: "application/vnd.pmtiles",
    });
    expect(partial.status).toBe(206);
    expect(partial.headers.get("Content-Range")).toBe(
      `bytes 0-6/${payload.byteLength}`
    );
    expect(partial.headers.get("Content-Length")).toBe("7");
    expect(partial.headers.get("Accept-Ranges")).toBe("bytes");
    expect(await partial.text()).toBe("PMTiles");

    const headRes = sliceArrayBufferResponse(payload, "bytes=0-6", {
      method: "HEAD",
    });
    expect(headRes.status).toBe(206);
    expect(headRes.headers.get("Content-Length")).toBe("7");
    expect(await headRes.text()).toBe("");

    const full = sliceArrayBufferResponse(payload, null);
    expect(full.status).toBe(200);
    expect(await full.text()).toBe("PMTiles-0123456789ABCDEF");

    const invalid = sliceArrayBufferResponse(payload, "bytes=500-600");
    expect(invalid.status).toBe(416);
  });

  it("verifies SHA-256 checksums for downloaded offline pack artifacts", async () => {
    const data = new TextEncoder().encode("trailforge-offline-pack");
    const hex = await computeSha256Hex(data);
    expect(hex).toMatch(/^[0-9a-f]{64}$/);
    expect(await verifySha256Hex(data, hex)).toBe("verified");
    expect(await verifySha256Hex(data, "0".repeat(64))).toBe("mismatch");
    expect(await verifySha256Hex(data, null)).toBe("skipped");
  });
});

describe("Slice 09 Client-side GridForge-compatible GPX 1.1 & GeoJSON exporters", () => {
  const pts: RecordedBreadcrumbPoint[] = [
    {
      lon: 148.1195,
      lat: -36.5608,
      elev_m: 614.2,
      timestamp: "2026-11-26T01:00:00Z",
      speed_mps: 5.5,
      roughness_rms: 0.24,
    },
    {
      lon: 148.1254,
      lat: -36.5729,
      elev_m: 812.0,
      timestamp: "2026-11-26T01:05:00Z",
      speed_mps: 3.2,
      roughness_rms: 0.55,
    },
  ];

  it("builds valid XML-escaped GPX 1.1 with trackpoints, elevations, extensions, and waypoints", () => {
    const xml = buildClientGpx("Davies Plain & Tom <Groggin>", pts, {
      description: "High Country test",
      waypoints: [
        { name: "River Camp", lon: 148.1195, lat: -36.5608, elev_m: 614 },
      ],
    });
    expect(xml).toContain('<?xml version="1.0" encoding="UTF-8"?>');
    expect(xml).toContain('xmlns="http://www.topografix.com/GPX/1/1"');
    expect(xml).toContain("Davies Plain &amp; Tom &lt;Groggin&gt;");
    expect(xml).toContain('<wpt lat="-36.560800" lon="148.119500">');
    expect(xml).toContain('<trkpt lat="-36.572900" lon="148.125400">');
    expect(xml).toContain("<ele>812.0</ele>");
    expect(xml).toContain("<roughness_rms>0.5500</roughness_rms>");
  });

  it("builds RFC 7946 GeoJSON FeatureCollection with 3D LineString and waypoints", () => {
    const fc = buildClientGeoJson("Davies Plain Run", pts, {
      waypoints: [{ name: "Hut", lon: 148.126, lat: -36.662 }],
    });
    expect(fc.type).toBe("FeatureCollection");
    expect(fc.features).toHaveLength(2);
    expect(fc.features[0]?.geometry.type).toBe("LineString");
    expect(fc.features[1]?.geometry.type).toBe("Point");
    expect(slugifyExportFilename("Davies Plain / Run!", "gpx")).toBe(
      "davies-plain-run.gpx"
    );
  });
});

describe("Slice 09 In-Cab Breadcrumb Recorder & One-Tap Backtrack math", () => {
  it("computes bearing, compass cardinal, roughness RMS, and backtrack reversal accurately", () => {
    expect(Math.round(computeBearingDeg(-36.5, 148.0, -36.4, 148.0))).toBe(0);
    expect(Math.round(computeBearingDeg(-36.5, 148.0, -36.6, 148.0))).toBe(180);
    expect(formatCompassCardinal(0)).toBe("N");
    expect(formatCompassCardinal(90)).toBe("E");
    expect(formatCompassCardinal(225)).toBe("SW");

    const rms = computeRoughnessRms([4.903325, 4.903325]);
    expect(rms).toBeCloseTo(0.5, 3);

    const trail: RecordedBreadcrumbPoint[] = [
      { lon: 148.12, lat: -36.56, elev_m: 620, timestamp: "2026-11-26T00:00:00Z" },
      { lon: 148.12, lat: -36.57, elev_m: 750, timestamp: "2026-11-26T00:05:00Z" },
      { lon: 148.12, lat: -36.58, elev_m: 910, timestamp: "2026-11-26T00:10:00Z" },
    ];
    const stats = computeRecorderStats(trail, Date.parse("2026-11-26T00:10:00Z"));
    expect(stats.pointCount).toBe(3);
    expect(stats.distanceM).toBeGreaterThan(2100);
    expect(stats.durationS).toBe(600);

    const backtrack = buildBacktrackGuidance(trail);
    expect(backtrack.active).toBe(true);
    expect(backtrack.reversedPoints[0]?.lat).toBe(-36.58);
    expect(backtrack.reversedPoints[2]?.lat).toBe(-36.56);
    expect(backtrack.nextWaypointCardinal).toBe("N");
    expect(backtrack.startCardinal).toBe("N");
    expect(backtrack.returnDistanceM).toBeCloseTo(stats.distanceM, 0);

    const geo = buildRecordedTrackGeoJson(trail, true);
    expect(geo.liveLineFc.features).toHaveLength(1);
    expect(geo.backtrackLineFc.features).toHaveLength(1);
    expect(geo.backtrackLineFc.features[0]?.geometry.coordinates[0]).toEqual([
      148.12, -36.58,
    ]);
  });

  it("provides a simulated High Country track long enough to survive 250 m privacy trimming", () => {
    let dist = 0;
    for (let i = 1; i < SIMULATED_HIGH_COUNTRY_WAYPOINTS.length; i += 1) {
      const a = SIMULATED_HIGH_COUNTRY_WAYPOINTS[i - 1]!;
      const b = SIMULATED_HIGH_COUNTRY_WAYPOINTS[i]!;
      dist += haversineDistanceM(a.lat, a.lon, b.lat, b.lon);
    }
    expect(dist).toBeGreaterThan(1500);
  });
});

describe("Slice 09 Offline Pack Snapshot store & Sync Queue flush", () => {
  afterEach(() => {
    setOfflineBackendForTesting(null);
    vi.unstubAllGlobals();
  });

  it("rejects an offline pack manifest when it belongs to an older telemetry revision", async () => {
    const fetchManifest = vi.fn().mockResolvedValue(Response.json({
      region_id: "australian-alps",
      community_telemetry_policy: "test-policy",
      community_telemetry_revision: null,
    }));
    vi.stubGlobal("fetch", fetchManifest);
    await expect(downloadRegionalPackToDevice({
      region_id: "australian-alps",
      name: "Australian Alps",
      description: "Synthetic region",
      bbox: [148, -37, 149, -36],
      status: "ready",
      community_telemetry_policy: "test-policy",
      community_telemetry_revision: "22222222-2222-4222-8222-222222222222",
    })).rejects.toThrow("Pack contents have changed");
    expect(fetchManifest).toHaveBeenCalledOnce();
  });

  it("prefers region R2 PMTiles URLs over relative manifest URLs during pack download", async () => {
    setOfflineBackendForTesting(new MemoryOfflineBackend());
    const requestedUrls: string[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      requestedUrls.push(url);
      if (url.endsWith("/manifest.json")) {
        return Response.json({
          region_id: "australian-alps",
          version: "2026.11.27",
          generated_at: "2026-11-27T00:00:00Z",
          community_telemetry_policy: "static_v1",
          vector_available: true,
          terrain_available: true,
          vector_pmtiles_url: "/api/packs/australian-alps/tiles/vector.pmtiles",
          terrain_pmtiles_url: "/api/packs/australian-alps/tiles/terrain.pmtiles",
          bounds: [148, -37, 149, -36],
          files: [],
        });
      }
      if (url.endsWith("/snapshot.json")) {
        return Response.json({
          region_id: "australian-alps",
          version: "2026.11.27",
          generated_at: "2026-11-27T00:00:00Z",
          tracks: [],
          pois: [],
          parks: [],
        });
      }
      return new Response(new Uint8Array([1, 2, 3, 4]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    await downloadRegionalPackToDevice({
      region_id: "australian-alps",
      name: "Australian Alps",
      description: "Synthetic region",
      bbox: [148, -37, 149, -36],
      status: "ready",
      community_telemetry_policy: "static_v1",
      manifest_url: "https://tiles.example.test/packs/australian-alps/static_v1/manifest.json",
      snapshot_url: "https://tiles.example.test/packs/australian-alps/static_v1/snapshot.json",
      vector_pmtiles_url: "https://tiles.example.test/packs/australian-alps/static_v1/vector.pmtiles",
      terrain_pmtiles_url: "https://tiles.example.test/packs/australian-alps/static_v1/terrain.pmtiles",
    });

    expect(requestedUrls).toEqual([
      "https://tiles.example.test/packs/australian-alps/static_v1/manifest.json",
      "https://tiles.example.test/packs/australian-alps/static_v1/snapshot.json",
      "https://tiles.example.test/packs/australian-alps/static_v1/vector.pmtiles",
      "https://tiles.example.test/packs/australian-alps/static_v1/terrain.pmtiles",
    ]);
  });

  it("stores and queries offline pack snapshots (tracks, POIs, parks, closures, reports, hotspots)", async () => {
    setOfflineBackendForTesting(new MemoryOfflineBackend());

    const snapshot: OfflinePackSnapshot = {
      region_id: "australian-alps",
      version: "2026.11.26",
      tracks: [
        {
          track_id: "trk_davies",
          name: "Davies Plain Track",
          state: "VIC/NSW",
          total_length_m: 11300,
          elev_gain_m: 1050,
          waterway_crossing_count: 2,
          effective_grade: "Hard",
          live_grade: "Hard",
          confidence_level: "medium",
          sample_count: 4,
          bbox: [148.11, -36.67, 148.14, -36.56],
          segments: [
            {
              segment_id: "seg_1",
              name: "Davies Plain Lower",
              effective_grade: "Hard",
              is_steep_highlight: true,
              geometry: {
                type: "LineString",
                coordinates: [
                  [148.119, -36.561],
                  [148.122, -36.565],
                ],
              },
            } as unknown as NonNullable<
              NonNullable<OfflinePackSnapshot["tracks"]>[number]["segments"]
            >[number],
          ],
        },
      ],
      pois: [
        {
          id: 1,
          name: "Davies Plain Hut",
          poi_type: "hut",
          state: "VIC",
          elev_m: 1615,
          lon: 148.126,
          lat: -36.662,
        },
      ],
      capad_parks: [
        {
          id: 10,
          name: "Alpine National Park",
          park_type: "National Park",
          state: "VIC",
        },
      ],
      closures: { type: "FeatureCollection", features: [] },
      reports: [
        {
          id: "rep-1",
          report_type: "locked_gate",
          notes: "Seasonal padlock closed",
          lon: 148.121,
          lat: -36.563,
          confirm_count: 2,
          resolved_count: 0,
        },
      ],
      hotspots: {
        type: "FeatureCollection",
        features: [
          {
            type: "Feature",
            geometry: { type: "Point", coordinates: [148.124, -36.57] },
            properties: { segment_id: "seg_1", avg_speed_kmh: 5.2 },
          },
        ],
      },
    };

    await saveOfflinePackData(
      {
        region_id: "australian-alps",
        name: "Australian Alps",
        version: "2026.11.26",
        downloaded_at: "2026-11-26T02:00:00Z",
        total_size_bytes: 512000,
        track_count: 1,
        segment_count: 1,
        poi_count: 1,
        bbox: [146.5, -37.6, 149.2, -35.4],
        vector_pmtiles_url: "/api/packs/australian-alps/tiles/vector.pmtiles",
        terrain_pmtiles_url: "/api/packs/australian-alps/tiles/terrain.pmtiles",
        has_vector_pmtiles: true,
        has_terrain_pmtiles: true,
        snowline_bands_m: [800, 1000, 1200],
        attributions: ["© OpenStreetMap contributors (ODbL)"],
        sha256_verified: true,
      },
      snapshot
    );
    setActiveOfflinePackId("australian-alps");

    const manifest = await getOfflineTileManifest();
    expect(manifest?.vector_available).toBe(true);
    expect(manifest?.vector_url).toBe(
      "/api/packs/australian-alps/tiles/vector.pmtiles"
    );

    const detail = await getOfflineTrackDetail("trk_davies");
    expect(detail?.name).toBe("Davies Plain Track");
    expect(detail?.live_grade).toBe("Hard");

    const searchRes = await searchOfflineSnapshots("davies");
    expect(searchRes?.results.map((r) => r.name)).toEqual([
      "Davies Plain Track",
      "Davies Plain Hut",
    ]);

    const tracksFc = await getOfflineTracksGeoJson();
    expect(tracksFc?.features).toHaveLength(1);
    expect((await getOfflinePois())?.length).toBe(1);
    expect((await getOfflineClosuresGeoJson())?.type).toBe("FeatureCollection");
    expect((await getOfflineReportsGeoJson())?.features).toHaveLength(1);
    expect((await getOfflineHotspotsGeoJson())?.features).toHaveLength(1);

  });

  it("keeps ownerless legacy records and another account's pending data out of the queue and pin list", async () => {
    const legacy = new MemoryOfflineBackend();
    setOfflineBackendForTesting(legacy);
    await legacy.put("offline_pins", "offline-pin-legacy", {
      id: "offline-pin-legacy",
      name: "Legacy Camp",
      category: "camp",
      lon: 148.12,
      lat: -36.565,
    });
    await legacy.put("sync_queue", "report:legacy", {
      id: "report:legacy",
      kind: "report",
      created_at: "2026-11-26T02:05:00Z",
      label: "Legacy report",
      payload: { report_type: "fallen_tree", lon: 148.12, lat: -36.565, notes: "Legacy" },
    });

    const pin = await enqueueOfflinePin("owner-a", {
      name: "Creek Camp",
      category: "camp",
      lon: 148.12,
      lat: -36.565,
    });

    expect(await listSyncQueue("owner-b")).toEqual([]);
    await deleteQueuedPin("owner-b", pin.id);
    expect(pinsFromSyncQueue(await listSyncQueue("owner-a"))).toEqual([
      expect.objectContaining({ id: pin.id, name: "Creek Camp" }),
    ]);
    await deleteQueuedPin("owner-a", pin.id);
    expect(await listSyncQueue("owner-a")).toEqual([]);
    expect(await legacy.getAll("offline_pins")).toHaveLength(1);
    expect(await legacy.getAll("sync_queue")).toHaveLength(1);
  });

  it("queues offline trips, reports, and pins and flushes idempotently on reconnect", async () => {
    setOfflineBackendForTesting(new MemoryOfflineBackend());

    const owner = "owner-a";
    await enqueueOfflineTrip(owner, {
      name: "Alpine Climb",
      visibility: "community_anonymous",
      privacy_trim_m: 250,
      points: [
        { lon: 148.12, lat: -36.56, timestamp: "2026-11-26T01:00:00Z" },
        { lon: 148.13, lat: -36.57, timestamp: "2026-11-26T01:10:00Z" },
      ],
    });
    await enqueueOfflineReport(owner, {
      report_type: "fallen_tree",
      lon: 148.122,
      lat: -36.564,
      notes: "Tree across ridge",
    });
    await enqueueOfflinePin(
      owner,
      { name: "Camp Spot", category: "camp", lon: 148.121, lat: -36.562 }
    );

    expect(await listSyncQueue(owner)).toHaveLength(3);

    // An authentication failure retains both the current item and future writes.
    const syncTrip = vi.fn().mockResolvedValue(undefined);
    const syncPin = vi.fn().mockResolvedValue(undefined);
    const syncReportFail = vi
      .fn()
      .mockRejectedValue(Object.assign(new Error("Unauthorized"), { status: 401 }));

    const first = await flushSyncQueue(owner, new AbortController().signal, {
      syncTrip,
      syncReport: syncReportFail,
      syncPin,
    });
    expect(first.synced).toBe(1);
    expect(first.failed).toBe(1);
    expect(first.remaining).toBe(2);
    expect(first.authRequired).toBe(true);
    expect(syncTrip).toHaveBeenCalledTimes(1);
    expect(syncPin).not.toHaveBeenCalled();

    // Second flush after sign-in succeeds and empties the queue
    const second = await flushSyncQueue(owner, new AbortController().signal, {
      syncTrip,
      syncReport: vi.fn().mockResolvedValue(undefined),
      syncPin,
    });
    expect(second.synced).toBe(2);
    expect(second.remaining).toBe(0);
    expect(second.authRequired).toBe(false);
    expect(syncPin).toHaveBeenCalledWith(expect.objectContaining({ name: "Camp Spot" }));
  });

  it("stops dispatching an old account's queue when the session changes", async () => {
    setOfflineBackendForTesting(new MemoryOfflineBackend());
    const owner = "owner-a";
    const first = await enqueueOfflineReport(owner, {
      report_type: "fallen_tree", lon: 148.12, lat: -36.56, notes: "First report",
    });
    const second = await enqueueOfflinePin(owner, {
      name: "Ridge Camp", category: "camp", lon: 148.13, lat: -36.57,
    });
    const controller = new AbortController();
    const syncPin = vi.fn().mockResolvedValue(undefined);
    const syncReport = vi.fn().mockImplementation(async () => {
      controller.abort();
      throw controller.signal.reason;
    });

    await flushSyncQueue(owner, controller.signal, {
      syncTrip: vi.fn(), syncReport, syncPin,
    });
    expect(syncReport).toHaveBeenCalledOnce();
    expect(syncPin).not.toHaveBeenCalled();
    expect(await listSyncQueue(owner)).toEqual([first, second]);
    expect(await listSyncQueue("owner-b")).toEqual([]);
  });

  it("sets authRequired and halts sync on 409 account-changed conflicts but continues on other 409 errors", async () => {
    setOfflineBackendForTesting(new MemoryOfflineBackend());
    const owner = "owner-conflict";
    await enqueueOfflineReport(owner, {
      report_type: "fallen_tree",
      lon: 148.12,
      lat: -36.56,
      notes: "Stale session report",
    });
    await enqueueOfflinePin(owner, {
      name: "Follow-up Camp",
      category: "camp",
      lon: 148.13,
      lat: -36.57,
    });

    const syncPin = vi.fn().mockResolvedValue(undefined);
    const conflictRes = await flushSyncQueue(owner, new AbortController().signal, {
      syncTrip: vi.fn(),
      syncReport: vi.fn().mockRejectedValue(
        Object.assign(new Error("The signed-in account has changed"), { status: 409 })
      ),
      syncPin,
    });
    expect(conflictRes.authRequired).toBe(true);
    expect(conflictRes.failed).toBe(1);
    expect(conflictRes.remaining).toBe(2);
    expect(syncPin).not.toHaveBeenCalled();

    const otherConflictRes = await flushSyncQueue(owner, new AbortController().signal, {
      syncTrip: vi.fn(),
      syncReport: vi.fn().mockRejectedValue(
        Object.assign(new Error("Duplicate resource"), { status: 409 })
      ),
      syncPin,
    });
    expect(otherConflictRes.authRequired).toBe(false);
    expect(otherConflictRes.synced).toBe(1);
    expect(otherConflictRes.failed).toBe(1);
    expect(syncPin).toHaveBeenCalledOnce();
  });
});

async function gzipJson(payload: unknown): Promise<Uint8Array<ArrayBuffer>> {
  const raw = new TextEncoder().encode(JSON.stringify(payload));
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(raw);
      controller.close();
    },
  }).pipeThrough(
    new CompressionStream("gzip") as unknown as TransformStream<
      Uint8Array,
      Uint8Array
    >
  );
  const buf = await new Response(stream).arrayBuffer();
  return new Uint8Array(buf);
}

describe("sharded_v2 compact offline packs & IndexedDB v2 shard hydration", () => {
  afterEach(() => {
    setOfflineBackendForTesting(null);
    vi.unstubAllGlobals();
  });

  it("matches Python FNV-1a 32-bit reference test vectors exactly", () => {
    expect(fnv1a32("")).toBe(0x811c9dc5);
    expect(fnv1a32("a")).toBe(0xe40c292c);
    expect(fnv1a32("foobar")).toBe(0xbf9cf968);
    expect(packShardFilename(0)).toBe("tracks_00.json.gz");
    expect(packShardFilename(14)).toBe("tracks_14.json.gz");
  });

  it("downloads sharded_v2 packs with SHA-256 verification, hydrates full tracks from 1 shard, plans offline routes, and cleans up shards on remove", async () => {
    const backend = new MemoryOfflineBackend();
    setOfflineBackendForTesting(backend);

    const shardCount = 4;
    const trk1Id = "trk_davies_plain";
    const trk2Id = "trk_tom_groggin";
    const shardByIdx: Array<Record<string, unknown>> = Array.from(
      { length: shardCount },
      () => ({})
    );

    shardByIdx[shardIndexForTrack(trk1Id, shardCount)]![trk1Id] = {
      track_id: trk1Id,
      name: "Davies Plain Track",
      alt_names: ["Davies Plain"],
      state: "VIC",
      primary_source: "osm",
      has_osm_source: true,
      trail_class: "4wd_track",
      is_vehicular: true,
      total_length_m: 4200,
      elev_min_m: 920,
      elev_max_m: 1580,
      elev_gain_m: 660,
      elev_loss_m: 120,
      max_slope_deg: 21.5,
      sustained_slope_deg: 14.2,
      waterway_crossing_count: 1,
      is_4wd_only: true,
      has_gate_barrier: true,
      access_status: "seasonal",
      primary_park_id: 10,
      primary_park_name: "Alpine National Park",
      park_ids: [10],
      effective_grade: "Hard",
      score: 68.5,
      live_grade: null,
      live_score: null,
      confidence_level: "none",
      sample_count: 0,
      vehicle_class_split: {},
      rainfall_context: { wet_risk_level: "wet", rain_72h_mm: 22.0 },
      obstacle_hotspots: [],
      bbox: [148.11, -36.67, 148.13, -36.64],
      baseline_breakdown: {
        formula_version: "baseline_v1",
        final_score: 68.5,
        grade: "Hard",
      },
      elevation_profile: [
        { dist_m: 0, elev_m: 920, slope_deg: 8.0, grade: "Medium" },
        { dist_m: 2100, elev_m: 1280, slope_deg: 21.5, grade: "Hard" },
        { dist_m: 4200, elev_m: 1580, slope_deg: 12.0, grade: "Hard" },
      ],
      waterway_crossings: [
        {
          name: "Murray River",
          waterway_type: "river",
          perennial: true,
          bridged: false,
          dist_m: 400,
        },
      ],
      coords: [
        [148.119, -36.645],
        [148.122, -36.655],
        [148.126, -36.662],
      ],
      segment_count: 42,
      is_active: true,
      closure_status: "seasonal",
      closure_ids: [901],
    };

    shardByIdx[shardIndexForTrack(trk2Id, shardCount)]![trk2Id] = {
      track_id: trk2Id,
      name: "Tom Groggin Track",
      alt_names: [],
      state: "VIC/NSW",
      total_length_m: 3100,
      elev_min_m: 1560,
      elev_max_m: 1710,
      elev_gain_m: 150,
      elev_loss_m: 40,
      max_slope_deg: 15.0,
      waterway_crossing_count: 0,
      primary_park_id: 10,
      primary_park_name: "Alpine National Park",
      park_ids: [10],
      effective_grade: "Medium",
      score: 48.0,
      bbox: [148.126, -36.68, 148.14, -36.662],
      elevation_profile: [
        { dist_m: 0, elev_m: 1580, slope_deg: 9.0, grade: "Medium" },
        { dist_m: 3100, elev_m: 1710, slope_deg: 15.0, grade: "Medium" },
      ],
      waterway_crossings: [],
      coords: [
        [148.126, -36.662],
        [148.135, -36.675],
      ],
      segment_count: 31,
      is_active: true,
      closure_status: "open",
      closure_ids: [],
    };

    const snapshotBytes = new TextEncoder().encode(
      JSON.stringify({
        schema_version: "trailforge_pack_snapshot_v2",
        region_id: "australian-alps",
        version: "2026.11.28",
        shard_count: shardCount,
        shard_hash: "fnv1a32",
        attributions: ["© OpenStreetMap contributors (ODbL)"],
        tracks: [
          {
            track_id: trk1Id,
            name: "Davies Plain Track",
            alt_names: ["Davies Plain"],
            state: "VIC",
            total_length_m: 4200,
            effective_grade: "Hard",
            primary_park_name: "Alpine National Park",
            bbox: [148.11, -36.67, 148.13, -36.64],
          },
          {
            track_id: trk2Id,
            name: "Tom Groggin Track",
            alt_names: [],
            state: "VIC/NSW",
            total_length_m: 3100,
            effective_grade: "Medium",
            primary_park_name: "Alpine National Park",
            bbox: [148.126, -36.68, 148.14, -36.662],
          },
        ],
        pois: [
          {
            id: 1,
            name: "Davies Plain Hut",
            poi_type: "hut",
            state: "VIC",
            elev_m: 1615,
            lon: 148.1262,
            lat: -36.6621,
          },
          {
            id: 2,
            name: "Faraway Trig",
            poi_type: "trig",
            state: "NSW",
            lon: 149.1,
            lat: -35.8,
          },
        ],
        capad_parks: [
          {
            id: 10,
            name: "Alpine National Park",
            park_type: "National Park",
            state: "VIC",
          },
        ],
        closures: [
          {
            closure_id: 901,
            title: "Seasonal Winter Closure",
            closure_type: "seasonal",
            severity: "warning",
            agency: "Parks Victoria",
            affected_track_ids: [trk1Id],
          },
        ],
        reports: [
          {
            id: "rep-gate-1",
            track_id: trk1Id,
            report_type: "locked_gate",
            notes: "Winter gate locked",
            lon: 148.12,
            lat: -36.65,
            confirm_count: 3,
            resolved_count: 0,
            is_gate_warning: true,
          },
        ],
        hotspots: { type: "FeatureCollection", features: [] },
      })
    );

    const shardGzBytes: Array<Uint8Array<ArrayBuffer>> = [];
    const filesMap: Record<
      string,
      { path: string; size_bytes: number; sha256?: string }
    > = {
      "snapshot.json": {
        path: "snapshot.json",
        size_bytes: snapshotBytes.byteLength,
        sha256: (await computeSha256Hex(snapshotBytes)) ?? undefined,
      },
    };
    for (let i = 0; i < shardCount; i += 1) {
      const gz = await gzipJson({
        schema_version: "trailforge_pack_shard_v2",
        region_id: "australian-alps",
        shard_index: i,
        tracks: shardByIdx[i],
      });
      shardGzBytes.push(gz);
      const rel = `shards/${packShardFilename(i)}`;
      filesMap[rel] = {
        path: rel,
        size_bytes: gz.byteLength,
        sha256: (await computeSha256Hex(gz)) ?? undefined,
      };
    }

    const requestedUrls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        requestedUrls.push(url);
        if (url.endsWith("/manifest.json")) {
          return Response.json({
            region_id: "australian-alps",
            version: "2026.11.28",
            community_telemetry_policy: "static_v1",
            snapshot_format: "sharded_v2",
            shard_count: shardCount,
            shard_hash: "fnv1a32",
            shards_base_url: "/api/packs/australian-alps/shards",
            bounds: [146.5, -37.6, 149.2, -35.6],
            counts: { tracks: 2, segments: 73, pois: 2 },
            files: filesMap,
          });
        }
        if (url.endsWith("/snapshot.json")) {
          return new Response(snapshotBytes, { status: 200 });
        }
        const m = url.match(/\/shards\/tracks_(\d{2})\.json\.gz$/);
        if (m) {
          const idx = Number(m[1]);
          const shardData = shardGzBytes[idx];
          return new Response(shardData ? shardData.buffer : null, {
            status: 200,
            headers: { "Content-Type": "application/gzip" },
          });
        }
        return new Response(null, { status: 404 });
      })
    );

    const record = await downloadRegionalPackToDevice({
      region_id: "australian-alps",
      name: "Australian Alps",
      description: "Sharded Alps test",
      bbox: [146.5, -37.6, 149.2, -35.6],
      status: "ready",
      community_telemetry_policy: "static_v1",
      snapshot_format: "sharded_v2",
      shard_count: shardCount,
      manifest_url: "https://tiles.example.test/packs/australian-alps/static_v1/manifest.json",
      snapshot_url: "https://tiles.example.test/packs/australian-alps/static_v1/snapshot.json",
      shards_base_url: "https://tiles.example.test/packs/australian-alps/static_v1/shards",
    });

    expect(record.snapshot_format).toBe("sharded_v2");
    expect(record.shard_count).toBe(shardCount);
    expect(record.sha256_verified).toBe(true);
    expect(
      requestedUrls.filter((u) =>
        u.startsWith("https://tiles.example.test/packs/australian-alps/static_v1/shards/")
      )
    ).toHaveLength(shardCount);

    setActiveOfflinePackId("australian-alps");

    // Compact snapshot index supports offline search without decompressing shards
    const search = await searchOfflineSnapshots("Davies");
    expect(search?.results[0]?.id).toBe(trk1Id);
    expect(search?.results[0]?.grade).toBe("Hard");

    // Sharded packs serve vector map geometry via PMTiles, not monolithic GeoJSON
    expect(await getOfflineTracksGeoJson("australian-alps")).toBeNull();

    // Hydrates full TrackInspectorDetail from single shard + normalized snapshot tables
    const hydrated = await getOfflineTrackDetail(trk1Id);
    expect(hydrated).not.toBeNull();
    expect(hydrated?.primary_park?.name).toBe("Alpine National Park");
    expect(hydrated?.capad_parks).toHaveLength(1);
    expect(hydrated?.active_closures).toHaveLength(1);
    expect(hydrated?.active_closures?.[0]?.title).toBe("Seasonal Winter Closure");
    expect(hydrated?.community_gate_warnings).toHaveLength(1);
    expect(hydrated?.nearby_pois?.map((p) => p.name)).toEqual(["Davies Plain Hut"]);
    expect(hydrated?.nearby_pois?.[0]?.distance_m).toBeLessThan(100);
    expect(hydrated?.segments).toHaveLength(1);
    expect(hydrated?.elevation_profile).toHaveLength(3);

    // Offline multi-track route planning works end-to-end across hydrated sharded tracks
    const route = await planRouteFromOfflineStore({
      name: "Alps Sharded Traverse",
      track_ids: [trk1Id, trk2Id],
      vehicle_class: "Touring 4WD",
    });
    expect(route).not.toBeNull();
    expect(route?.total_length_m).toBe(7300);
    expect(route?.worst_grade).toBe("Hard");
    expect(route?.safety_summary.closure_count).toBe(1);
    expect(route?.safety_summary.gate_warning_count).toBe(1);
    expect(buildPlannedRouteMapGeoJson(route!).lineFc.features).toHaveLength(2);

    // Removing the pack deletes all shard blobs from pack_shards
    await removeOfflinePack("australian-alps");
    for (let i = 0; i < shardCount; i += 1) {
      expect(
        await backend.get("pack_shards", packShardStorageKey("australian-alps", i))
      ).toBeNull();
    }
  });

  it("rejects tampered sharded_v2 shards on SHA-256 mismatch and rolls back partial shards", async () => {
    const backend = new MemoryOfflineBackend();
    setOfflineBackendForTesting(backend);

    const snapshotBytes = new TextEncoder().encode(
      JSON.stringify({
        schema_version: "trailforge_pack_snapshot_v2",
        region_id: "australian-alps",
        shard_count: 2,
        tracks: [],
      })
    );
    const shard0 = await gzipJson({
      schema_version: "trailforge_pack_shard_v2",
      tracks: {},
    });
    const shard1 = await gzipJson({
      schema_version: "trailforge_pack_shard_v2",
      tracks: {},
    });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/manifest.json")) {
          return Response.json({
            region_id: "australian-alps",
            snapshot_format: "sharded_v2",
            shard_count: 2,
            files: {
              "snapshot.json": {
                path: "snapshot.json",
                size_bytes: snapshotBytes.byteLength,
                sha256: await computeSha256Hex(snapshotBytes),
              },
              "shards/tracks_00.json.gz": {
                path: "shards/tracks_00.json.gz",
                size_bytes: shard0.byteLength,
                sha256: await computeSha256Hex(shard0),
              },
              "shards/tracks_01.json.gz": {
                path: "shards/tracks_01.json.gz",
                size_bytes: shard1.byteLength,
                sha256: "f".repeat(64), // deliberate mismatch
              },
            },
          });
        }
        if (url.endsWith("/snapshot.json")) {
          return new Response(snapshotBytes, { status: 200 });
        }
        if (url.endsWith("tracks_00.json.gz")) {
          return new Response(shard0.buffer, { status: 200 });
        }
        if (url.endsWith("tracks_01.json.gz")) {
          return new Response(shard1.buffer, { status: 200 });
        }
        return new Response(null, { status: 404 });
      })
    );

    await expect(
      downloadRegionalPackToDevice({
        region_id: "australian-alps",
        name: "Australian Alps",
        description: "Tampered shard test",
        bbox: [146.5, -37.6, 149.2, -35.6],
        status: "ready",
      })
    ).rejects.toThrow("tracks_01.json.gz SHA-256 verification failed");

    // Rolled back: neither shard remains orphaned in pack_shards
    expect(await backend.get("pack_shards", "australian-alps:0")).toBeNull();
    expect(await backend.get("pack_shards", "australian-alps:1")).toBeNull();
  });
});
