// TrailForge Offline PWA Service Worker Shell (Slice 01)
const CACHE_NAME = "trailforge-shell-v1";
const PRECACHE_URLS = [
  "/",
  "/index.html",
  "/manifest.webmanifest",
  "/icon.svg",
  "/icons/campground.svg",
  "/icons/hut.svg",
  "/icons/trig.svg",
  "/icons/water_bore.svg",
  "/icons/fuel.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE_URLS))
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
            .filter((key) => key !== CACHE_NAME)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);

  // Never intercept cross-origin requests (external raster tiles fail gracefully in MapLibre)
  if (url.origin !== self.location.origin) return;

  // Live API & health endpoints are always network-only so offline state is accurate
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

  // Navigation requests: network-first with cached app-shell fallback for offline reloads
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put("/", copy));
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

  // Static app assets & icons: cache-first with background refresh
  event.respondWith(
    caches.match(request).then((cached) => {
      const networkFetch = fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => cached);

      return cached ?? networkFetch;
    })
  );
});
