/**
 * TrailForge Cloudflare Pages Function — Same-Origin `/api/*` Reverse Proxy.
 *
 * Mount at `web/functions/api/[[path]].js` in Cloudflare Pages (or adapt into a
 * standalone Cloudflare Worker route for `https://trails.example.com/api/*`).
 *
 * Required Pages / Worker environment variables:
 *   RAILWAY_API_ORIGIN    = "https://api.example.com"
 *   ORIGIN_SHARED_SECRET  = "<same-secret-as-TRAILFORGE_ORIGIN_SHARED_SECRET>" (recommended)
 *
 * Behavior:
 *   - Proxies `/api/*` requests to `env.RAILWAY_API_ORIGIN` without buffering
 *     request or response bodies (preserving HTTP 206 Partial Content PMTiles
 *     range streaming and multi-MB GPX/photo/pack streams).
 *   - Forwards `CF-Connecting-IP`, `Range`, `If-Range`, `If-None-Match`,
 *     `Cookie`, and `Authorization` headers to FastAPI, and injects
 *     `X-TrailForge-Origin-Secret` when `env.ORIGIN_SHARED_SECRET` is configured.
 *   - Preserves all upstream `Set-Cookie`, `Content-Range`, `Accept-Ranges`,
 *     `ETag`, `X-RateLimit-*`, and `Retry-After` response headers.
 *   - Rewrites upstream redirect `Location` headers (e.g. FastAPI trailing-slash
 *     `307` redirects) back to the public client origin so internal Railway
 *     hostnames (`*.up.railway.app`) are never leaked.
 *   - Adds baseline security response headers (`X-Content-Type-Options`,
 *     `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, and `HSTS`).
 *   - Strips internal upstream error pages/details on 5xx gateway faults so
 *     Railway hostnames or stack traces are never exposed to clients.
 */

const HOP_BY_HOP_REQUEST_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "host",
  "x-trailforge-origin-secret",
]);

const HOP_BY_HOP_RESPONSE_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "server",
  "x-powered-by",
]);

const EXPLICIT_FORWARD_HEADERS = [
  "cf-connecting-ip",
  "range",
  "if-range",
  "if-none-match",
  "if-modified-since",
  "cookie",
  "authorization",
];

const SECURITY_RESPONSE_HEADERS = [
  ["X-Content-Type-Options", "nosniff"],
  ["X-Frame-Options", "DENY"],
  ["Referrer-Policy", "strict-origin-when-cross-origin"],
  ["Permissions-Policy", "camera=(), microphone=(), geolocation=(self)"],
];

const HSTS_HEADER_VALUE = "max-age=31536000; includeSubDomains";

const SANITIZED_5XX_STATUSES = new Set([500, 502, 503, 504]);

function applySecurityHeaders(headers, incomingUrl) {
  for (const [name, value] of SECURITY_RESPONSE_HEADERS) {
    if (!headers.has(name)) {
      headers.set(name, value);
    }
  }
  if (incomingUrl?.protocol === "https:" && !headers.has("strict-transport-security")) {
    headers.set("Strict-Transport-Security", HSTS_HEADER_VALUE);
  }
}

function jsonError(status, detail, incomingUrl = null) {
  const headers = new Headers({
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  applySecurityHeaders(headers, incomingUrl);
  return new Response(JSON.stringify({ detail }), {
    status,
    headers,
  });
}

function resolveUpstreamOrigin(env) {
  const raw = typeof env?.RAILWAY_API_ORIGIN === "string" ? env.RAILWAY_API_ORIGIN.trim() : "";
  if (!raw) {
    return null;
  }
  try {
    const parsed = new URL(raw);
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
      return null;
    }
    return parsed.origin;
  } catch {
    return null;
  }
}

function rewriteRedirectLocation(location, upstreamOrigin, incomingUrl) {
  if (!location || !upstreamOrigin || !incomingUrl) {
    return location;
  }
  const trimmed = location.trim();
  if (!trimmed) {
    return location;
  }
  // Keep normal relative paths (starting with single '/') unchanged.
  if (trimmed.startsWith("/") && !trimmed.startsWith("//")) {
    return location;
  }
  try {
    const parsed = new URL(trimmed, upstreamOrigin);
    if (parsed.origin === upstreamOrigin) {
      return `${incomingUrl.origin}${parsed.pathname}${parsed.search}${parsed.hash}`;
    }
    return location;
  } catch {
    return location;
  }
}

function buildUpstreamHeaders(request, incomingUrl, env) {
  const headers = new Headers();

  for (const [key, value] of request.headers.entries()) {
    const lower = key.toLowerCase();
    if (HOP_BY_HOP_REQUEST_HEADERS.has(lower)) {
      continue;
    }
    headers.append(key, value);
  }

  for (const headerName of EXPLICIT_FORWARD_HEADERS) {
    const val = request.headers.get(headerName);
    if (val !== null) {
      headers.set(headerName, val);
    }
  }

  const clientIp = request.headers.get("CF-Connecting-IP");
  if (clientIp) {
    headers.set("CF-Connecting-IP", clientIp);
    headers.set("X-Real-IP", clientIp);
    const existingXff = request.headers.get("X-Forwarded-For");
    if (!existingXff) {
      headers.set("X-Forwarded-For", clientIp);
    }
  }

  headers.set("X-Forwarded-Proto", incomingUrl.protocol.replace(":", ""));
  headers.set("X-Forwarded-Host", incomingUrl.host);

  const originSecret =
    typeof env?.ORIGIN_SHARED_SECRET === "string" ? env.ORIGIN_SHARED_SECRET.trim() : "";
  if (originSecret) {
    headers.set("X-TrailForge-Origin-Secret", originSecret);
  }

  return headers;
}

function buildClientResponseHeaders(upstreamHeaders, { upstreamOrigin = null, incomingUrl = null } = {}) {
  // Constructing Headers from upstreamHeaders preserves multiple Set-Cookie entries
  // as well as Content-Range, Content-Length, Accept-Ranges, ETag, and rate-limit headers.
  const headers = new Headers(upstreamHeaders);

  for (const headerName of HOP_BY_HOP_RESPONSE_HEADERS) {
    headers.delete(headerName);
  }

  // Ensure multi-value Set-Cookie headers survive runtimes that separate getSetCookie().
  if (typeof upstreamHeaders.getSetCookie === "function") {
    const cookies = upstreamHeaders.getSetCookie();
    if (cookies.length > 0) {
      headers.delete("set-cookie");
      for (const cookie of cookies) {
        headers.append("Set-Cookie", cookie);
      }
    }
  }

  // Never leak internal Railway upstream origins in 3xx redirect Location headers.
  const rawLocation = headers.get("location");
  if (rawLocation && upstreamOrigin && incomingUrl) {
    const rewritten = rewriteRedirectLocation(rawLocation, upstreamOrigin, incomingUrl);
    if (rewritten !== rawLocation) {
      headers.set("Location", rewritten);
    }
  }

  applySecurityHeaders(headers, incomingUrl);

  return headers;
}

export async function onRequest(context) {
  const { request, env } = context;
  const incomingUrl = new URL(request.url);

  if (incomingUrl.pathname !== "/api" && !incomingUrl.pathname.startsWith("/api/")) {
    if (typeof context.next === "function") {
      return context.next();
    }
    return jsonError(404, "Not found.", incomingUrl);
  }

  const upstreamOrigin = resolveUpstreamOrigin(env);
  if (!upstreamOrigin) {
    return jsonError(503, "API proxy origin is not configured.", incomingUrl);
  }

  const upstreamUrl = new URL(incomingUrl.pathname + incomingUrl.search, upstreamOrigin);
  const method = request.method.toUpperCase();
  const hasBody = method !== "GET" && method !== "HEAD";

  const upstreamHeaders = buildUpstreamHeaders(request, incomingUrl, env);

  let upstreamResponse;
  try {
    upstreamResponse = await fetch(upstreamUrl.toString(), {
      method,
      headers: upstreamHeaders,
      body: hasBody ? request.body : undefined,
      redirect: "manual",
    });
  } catch {
    // Never expose internal Railway hostnames, DNS errors, or stack traces.
    return jsonError(502, "Upstream TrailForge API is temporarily unavailable.", incomingUrl);
  }

  // Sanitize unexpected gateway/server errors (500/502/503/504 or non-JSON 5xx)
  // while allowing intentional JSON responses like 507 Insufficient Storage through.
  const contentType = upstreamResponse.headers.get("content-type") || "";
  const isJson = contentType.toLowerCase().includes("application/json");
  if (
    SANITIZED_5XX_STATUSES.has(upstreamResponse.status) ||
    (upstreamResponse.status >= 500 && !isJson)
  ) {
    try {
      await upstreamResponse.body?.cancel?.();
    } catch {
      // Ignore stream cancellation errors.
    }
    const safeStatus = upstreamResponse.status >= 500 ? upstreamResponse.status : 502;
    const safeMessage =
      safeStatus === 503 || safeStatus === 504
        ? "TrailForge API is temporarily unavailable. Please retry shortly."
        : "An internal error occurred while contacting the TrailForge API.";
    return jsonError(safeStatus, safeMessage, incomingUrl);
  }

  const responseHeaders = buildClientResponseHeaders(upstreamResponse.headers, {
    upstreamOrigin,
    incomingUrl,
  });

  // Stream upstreamResponse.body directly without buffering so HTTP 206 Partial Content
  // PMTiles range slices and offline pack archives transfer at full edge speed.
  return new Response(upstreamResponse.body, {
    status: upstreamResponse.status,
    statusText: upstreamResponse.statusText,
    headers: responseHeaders,
  });
}
