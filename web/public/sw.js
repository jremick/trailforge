// TrailForge Offline PWA Service Worker (Slices 01–09)
// Supports offline SPA shell caching + HTTP 206 Partial Content Range slicing
// for downloaded regional PMTiles packs so MapLibre GL JS renders 100% offline.

const SHELL_CACHE = "trailforge-shell-v4";
const PACKS_CACHE = "trailforge-packs-v1";
const ALLOWED_CACHES = new Set([SHELL_CACHE, PACKS_CACHE]);
const MAX_MEM_PACK_BLOBS = 2;

let packsCachePromise = null;
// Small in-memory LRU cache of active PMTiles blobs so rapid MapLibre Range 206 pan/zoom
// requests don't re-open CacheStorage and re-materialize Response.blob() 50x per gesture.
const packBlobCache = new Map();

function getPacksCache() {
  if (!packsCachePromise) {
    packsCachePromise = caches.open(PACKS_CACHE).catch((err) => {
      packsCachePromise = null;
      throw err;
    });
  }
  return packsCachePromise;
}

function clearPackBlobCache(regionId) {
  if (!regionId) {
    packBlobCache.clear();
    return;
  }
  const prefix = `/api/packs/${encodeURIComponent(regionId)}/`;
  for (const key of packBlobCache.keys()) {
    if (key.startsWith(prefix)) {
      packBlobCache.delete(key);
    }
  }
}

const PRECACHE_URLS = [
  "/",
  "/index.html",
  "/manifest.webmanifest",
  "/icon.svg",
  "/data/au_places.geojson",
  "/data/au_highways.geojson",
  "/fonts/Noto%20Sans%20Regular/0-255.pbf",
  "/fonts/Noto%20Sans%20Regular/256-511.pbf",
  "/fonts/Noto%20Sans%20Regular/8192-8447.pbf",
  "/fonts/Noto%20Sans%20Medium/0-255.pbf",
  "/fonts/Noto%20Sans%20Medium/256-511.pbf",
  "/fonts/Noto%20Sans%20Medium/8192-8447.pbf",
  "/fonts/Noto%20Sans%20Bold/0-255.pbf",
  "/fonts/Noto%20Sans%20Bold/256-511.pbf",
  "/fonts/Noto%20Sans%20Bold/8192-8447.pbf",
  "/fonts/Noto%20Sans%20Italic/0-255.pbf",
  "/fonts/Noto%20Sans%20Italic/256-511.pbf",
  "/fonts/Noto%20Sans%20Italic/8192-8447.pbf",
  "/icons/airfield.svg",
  "/icons/campground.svg",
  "/icons/comm_tower.svg",
  "/icons/fire_station.svg",
  "/icons/fuel.svg",
  "/icons/helipad.svg",
  "/icons/homestead.svg",
  "/icons/hospital.svg",
  "/icons/hut.svg",
  "/icons/obstacle_tower.svg",
  "/icons/police.svg",
  "/icons/saddle.svg",
  "/icons/shelter.svg",
  "/icons/trig.svg",
  "/icons/water_bore.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) =>
        Promise.all(
          PRECACHE_URLS.map((url) => cache.add(url).catch(() => undefined))
        )
      )
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  packBlobCache.clear();
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => !ALLOWED_CACHES.has(key))
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("message", (event) => {
  const data = event.data;
  if (data && data.type === "INVALIDATE_PACK_CACHE") {
    clearPackBlobCache(typeof data.regionId === "string" ? data.regionId : null);
  }
});

/**
 * Parse RFC 7233 single byte range against known totalLength.
 */
function parseByteRange(rangeHeader, totalLength) {
  if (!Number.isFinite(totalLength) || totalLength <= 0) {
    return { status: 416 };
  }
  if (!rangeHeader || !rangeHeader.trim()) {
    return { status: 200, start: 0, end: totalLength - 1 };
  }
  const match = /^bytes=(\d*)-(\d*)$/i.exec(rangeHeader.trim());
  if (!match) return { status: 416 };

  const rawStart = match[1];
  const rawEnd = match[2];
  if (!rawStart && !rawEnd) return { status: 416 };

  let start;
  let end;
  if (!rawStart && rawEnd) {
    const suffix = Number(rawEnd);
    if (!Number.isInteger(suffix) || suffix <= 0) return { status: 416 };
    start = Math.max(0, totalLength - suffix);
    end = totalLength - 1;
  } else {
    start = Number(rawStart);
    end = rawEnd ? Number(rawEnd) : totalLength - 1;
    if (
      !Number.isInteger(start) ||
      !Number.isInteger(end) ||
      start < 0 ||
      start >= totalLength
    ) {
      return { status: 416 };
    }
    end = Math.min(end, totalLength - 1);
    if (end < start) return { status: 416 };
  }

  return { status: 206, start, end };
}

/**
 * Load a cached PMTiles Blob from SW memory LRU or CacheStorage.
 */
async function loadCachedPackBlob(request, pathname) {
  const memHit = packBlobCache.get(pathname);
  if (memHit) {
    // Refresh LRU position
    packBlobCache.delete(pathname);
    packBlobCache.set(pathname, memHit);
    return memHit;
  }

  const cache = await getPacksCache();
  const cached = (await cache.match(pathname)) || (await cache.match(request.url));
  if (!cached) {
    return null;
  }

  const blob = await cached.blob();
  const contentType =
    cached.headers.get("Content-Type") || "application/octet-stream";
  const entry = { blob, contentType };
  packBlobCache.set(pathname, entry);
  while (packBlobCache.size > MAX_MEM_PACK_BLOBS) {
    const oldestKey = packBlobCache.keys().next().value;
    if (oldestKey !== undefined) packBlobCache.delete(oldestKey);
    else break;
  }
  return entry;
}

/**
 * Serve cached PMTiles with exact HTTP 206 Partial Content byte slicing so
 * MapLibre `pmtiles://` protocol renders offline regional packs seamlessly.
 */
async function respondFromCachedPackTile(request) {
  const url = new URL(request.url);
  const entry = await loadCachedPackBlob(request, url.pathname);
  if (!entry) {
    return null;
  }

  const { blob, contentType } = entry;
  const total = blob.size;
  const rangeHeader = request.headers.get("Range");
  const isHead = request.method === "HEAD";

  const parsed = parseByteRange(rangeHeader, total);
  if (parsed.status === 416) {
    return new Response(null, {
      status: 416,
      headers: {
        "Accept-Ranges": "bytes",
        "Content-Range": `bytes */${total}`,
      },
    });
  }

  if (parsed.status === 200) {
    return new Response(isHead ? null : blob, {
      status: 200,
      headers: {
        "Content-Type": contentType,
        "Accept-Ranges": "bytes",
        "Content-Length": String(total),
        "X-TrailForge-Offline-Cache": "HIT",
      },
    });
  }

  const sliceLen = parsed.end - parsed.start + 1;
  const slicedBody = isHead ? null : blob.slice(parsed.start, parsed.end + 1);
  return new Response(slicedBody, {
    status: 206,
    statusText: "Partial Content",
    headers: {
      "Content-Type": contentType,
      "Accept-Ranges": "bytes",
      "Content-Range": `bytes ${parsed.start}-${parsed.end}/${total}`,
      "Content-Length": String(sliceLen),
      "X-TrailForge-Offline-Cache": "HIT",
    },
  });
}

function isPackPmtilesPath(pathname) {
  return (
    (pathname.startsWith("/api/packs/") && pathname.endsWith(".pmtiles")) ||
    (pathname.startsWith("/api/tiles/") && pathname.endsWith(".pmtiles"))
  );
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET" && request.method !== "HEAD") return;

  const url = new URL(request.url);

  // Never intercept cross-origin requests:
  // - External raster basemaps fail gracefully inside MapLibre
  // - Cloudflare R2 absolute PMTiles URLs stream HTTP 206 Range requests directly from R2 CDN
  if (url.origin !== self.location.origin) return;

  // Never intercept MCP Streamable HTTP / SSE or OAuth discovery metadata endpoints
  if (
    url.pathname === "/mcp" ||
    url.pathname.startsWith("/mcp/") ||
    url.pathname.startsWith("/.well-known/")
  ) {
    return;
  }

  // Offline regional PMTiles: serve Range 206 directly from PACKS_CACHE when downloaded!
  if (isPackPmtilesPath(url.pathname)) {
    event.respondWith(
      respondFromCachedPackTile(request).then((hit) => {
        if (hit) return hit;
        return fetch(request).catch(
          () =>
            new Response(
              JSON.stringify({
                status: "offline",
                detail: "PMTiles pack not cached offline",
              }),
              {
                status: 503,
                headers: { "Content-Type": "application/json" },
              }
            )
        );
      })
    );
    return;
  }

  // Live API & health endpoints are network-only so api.ts detects offline cleanly
  // and switches to IndexedDB offline pack snapshots.
  if (url.pathname.startsWith("/api/") || url.pathname === "/healthz") {
    event.respondWith(
      fetch(request).catch(
        () =>
          new Response(
            JSON.stringify({ status: "offline", detail: "Network unreachable" }),
            {
              status: 503,
              headers: { "Content-Type": "application/json" },
            }
          )
      )
    );
    return;
  }

  if (request.method !== "GET") return;

  // Navigation requests: network-first with cached app-shell fallback for offline reloads
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(SHELL_CACHE).then((cache) => cache.put("/", copy));
          }
          return response;
        })
        .catch(() =>
          caches
            .match("/index.html")
            .then((cached) => cached ?? caches.match("/"))
        )
    );
    return;
  }

  // Content-hashed Vite bundles (/assets/*) are immutable: pure Cache-First (no redundant fetch)
  if (url.pathname.startsWith("/assets/")) {
    event.respondWith(
      caches.match(request).then((cached) => {
        if (cached) return cached;
        return fetch(request).then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches
              .open(SHELL_CACHE)
              .then((cache) => cache.put(request, copy));
          }
          return response;
        });
      })
    );
    return;
  }

  // Other static app assets (/icons/*, /data/*, /fonts/*, /manifest.webmanifest): cache-first with background refresh
  event.respondWith(
    caches.match(request).then((cached) => {
      const networkFetch = fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches
              .open(SHELL_CACHE)
              .then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => cached);

      return cached ?? networkFetch;
    })
  );
});
