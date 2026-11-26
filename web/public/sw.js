// TrailForge Offline PWA Service Worker (Slices 01–09)
// Supports offline SPA shell caching + HTTP 206 Partial Content Range slicing
// for downloaded regional PMTiles packs so MapLibre GL JS 5 renders 100% offline.

const SHELL_CACHE = "trailforge-shell-v2";
const PACKS_CACHE = "trailforge-packs-v1";
const ALLOWED_CACHES = new Set([SHELL_CACHE, PACKS_CACHE]);

const PRECACHE_URLS = [
  "/",
  "/index.html",
  "/manifest.webmanifest",
  "/icon.svg",
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
 * Serve cached PMTiles with exact HTTP 206 Partial Content byte slicing so
 * MapLibre GL JS 5 `pmtiles://` protocol renders offline regional packs seamlessly.
 */
async function respondFromCachedPackTile(request) {
  const cache = await caches.open(PACKS_CACHE);
  const url = new URL(request.url);
  const cached = (await cache.match(url.pathname)) || (await cache.match(request.url));
  if (!cached) {
    return null;
  }

  const blob = await cached.blob();
  const total = blob.size;
  const contentType =
    cached.headers.get("Content-Type") || "application/octet-stream";
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
  // - Cloudflare R2 absolute PMTiles URLs (e.g. https://tiles.example.com/*.pmtiles)
  //   stream HTTP 206 Range requests directly from R2 edge CDN; downloaded offline packs
  //   are cached under same-origin /api/packs/<region>/tiles/*.pmtiles below.
  if (url.origin !== self.location.origin) return;

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

  // Static app assets (/assets/*, /icons/*): cache-first with background refresh
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
