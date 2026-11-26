/**
 * TrailForge Cloudflare Pages Middleware (`web/functions/_middleware.js`).
 *
 * Drop-in Cloudflare Pages middleware wrapper that delegates same-origin
 * `/api/*`, `/mcp`, `/mcp/*`, and `/.well-known/oauth-*` requests to
 * `pages-proxy-worker.js` while letting Cloudflare Pages serve static SPA
 * assets (`index.html`, `/assets/*`, `/icons/*`, `/sw.js`, `/manifest.webmanifest`)
 * via `context.next()`.
 *
 * Setup during Cloudflare Pages build (Root directory = `web`):
 *   mkdir -p functions
 *   cp ../deploy/cloudflare/pages-proxy-worker.js functions/pages-proxy-worker.js
 *   cp ../deploy/cloudflare/pages-middleware.js functions/_middleware.js
 *   npm ci && npm run build
 *
 * Required Pages environment variables:
 *   RAILWAY_API_ORIGIN    = "https://api.example.com"
 *   ORIGIN_SHARED_SECRET  = "<same-secret-as-TRAILFORGE_ORIGIN_SHARED_SECRET>" (recommended)
 */

import { isProxiedPath, onRequest as proxyOnRequest } from "./pages-proxy-worker.js";

export async function onRequest(context) {
  const incomingUrl = new URL(context.request.url);

  if (isProxiedPath(incomingUrl.pathname)) {
    return proxyOnRequest(context);
  }

  if (typeof context.next === "function") {
    return context.next();
  }

  return new Response(JSON.stringify({ detail: "Not found." }), {
    status: 404,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}
