/**
 * TrailForge Cloudflare Pages Function — Same-Origin `/api/*` Reverse Proxy.
 *
 * Mount at `web/functions/api/[[path]].js` in Cloudflare Pages (or adapt into a
 * standalone Cloudflare Worker route for `https://trails.example.com/api/*`).
 *
 * Required Pages / Worker environment variable:
 *   RAILWAY_API_ORIGIN = "https://api.example.com"
 *
 * Behavior:
 *   - Proxies `/api/*` requests to `env.RAILWAY_API_ORIGIN` without buffering
 *     request or response bodies (preserving HTTP 206 Partial Content PMTiles
 *     range streaming and multi-MB GPX/photo/pack streams).
 *   - Forwards `CF-Connecting-IP`, `Range`, `If-Range`, `If-None-Match`,
 *     `Cookie`, and `Authorization` headers to FastAPI.
 *   - Preserves all upstream `Set-Cookie`, `Content-Range`, `Accept-Ranges`,
 *     `ETag`, `X-RateLimit-*`, and `Retry-After` response headers.
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

const SANITIZED_5XX_STATUSES = new Set([500, 502, 503, 504]);

function jsonError(status, detail) {
  return new Response(JSON.stringify({ detail }), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
    },
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

function buildUpstreamHeaders(request, incomingUrl) {
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

  return headers;
}

function buildClientResponseHeaders(upstreamHeaders) {
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

  return headers;
}

export async function onRequest(context) {
  const { request, env } = context;
  const incomingUrl = new URL(request.url);

  if (incomingUrl.pathname !== "/api" && !incomingUrl.pathname.startsWith("/api/")) {
    if (typeof context.next === "function") {
      return context.next();
    }
    return jsonError(404, "Not found.");
  }

  const upstreamOrigin = resolveUpstreamOrigin(env);
  if (!upstreamOrigin) {
    return jsonError(503, "API proxy origin is not configured.");
  }

  const upstreamUrl = new URL(incomingUrl.pathname + incomingUrl.search, upstreamOrigin);
  const method = request.method.toUpperCase();
  const hasBody = method !== "GET" && method !== "HEAD";

  const upstreamHeaders = buildUpstreamHeaders(request, incomingUrl);

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
    return jsonError(502, "Upstream TrailForge API is temporarily unavailable.");
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
    return jsonError(safeStatus, safeMessage);
  }

  const responseHeaders = buildClientResponseHeaders(upstreamResponse.headers);

  // Stream upstreamResponse.body directly without buffering so HTTP 206 Partial Content
  // PMTiles range slices and offline pack archives transfer at full edge speed.
  return new Response(upstreamResponse.body, {
    status: upstreamResponse.status,
    statusText: upstreamResponse.statusText,
    headers: responseHeaders,
  });
}
