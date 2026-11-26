# Cloudflare Free-Tier WAF, Rate Limiting, R2 Cache & Analytics Recipe

This guide configures free Cloudflare edge protections in front of a public **TrailForge** instance (`https://trails.example.com` on Cloudflare Pages + `https://tiles.example.com` on Cloudflare R2 + Railway backend).

TrailForge's backend ships with a built-in, zero-Redis `UsageAndRateLimitMiddleware` (`TRAILFORGE_RATE_LIMIT_ENABLED=true`) keyed by `CF-Connecting-IP`. Layering Cloudflare edge rules in front absorbs floods, credential stuffing, and scraper spikes at the edge **before** requests ever reach Railway compute or incur egress.

---

## 1. Per-IP Edge Rate Limiting Rules

Configure under **Cloudflare Dashboard -> Security -> WAF -> Rate limiting rules**.

### Recommended 3-Bucket Thresholds

| Bucket | Scope Expression | Threshold | Period | Action |
| --- | --- | --- | --- | --- |
| **Auth protection** | `(http.request.uri.path starts_with "/api/auth/")` | `<= 10 req/min` per IP | `60s` | Block (`HTTP 429`) or Managed Challenge |
| **Uploads & reports** | `(http.request.uri.path starts_with "/api/trips/" or http.request.uri.path starts_with "/api/reports") and http.request.method in {"POST" "PUT" "PATCH" "DELETE"}` | `<= 20 req/min` per IP | `60s` | Block (`HTTP 429`) |
| **General API** | `(http.request.uri.path starts_with "/api/" and http.request.uri.path ne "/api/health")` | `<= 120 req/min` per IP | `60s` | Block (`HTTP 429`) |

> **Cloudflare Free Plan Note:** The Cloudflare Free plan includes **1 Custom Rate Limiting rule** (with a 10-second counting period; Pro/Business unlock additional rules and 60-second windows). On the Free plan:
> - Dedicate your **1 Cloudflare edge Rate Limiting rule** to `/api/auth/*` (e.g., `10 requests per 10 seconds` -> Block for 10s, or a broader `/api/*` ceiling of `20 requests per 10 seconds` ~= `120 req/min`).
> - Let TrailForge's built-in **in-process rate limiter** enforce all three fine-grained limits simultaneously using `CF-Connecting-IP`:
>   - `TRAILFORGE_RATE_LIMIT_AUTH_PER_MIN=12` (`/api/auth/login`, `/api/auth/register`)
>   - `TRAILFORGE_RATE_LIMIT_READ_PER_MIN=180` (general `/api/*`, excluding `/api/health`)
>   - `TRAILFORGE_RATE_LIMIT_UPLOAD_PER_DAY=25` (per-user daily cap on `/api/trips/upload` & `/api/trips/mobile-sync`)

---

## 2. Bot Fight Mode & Custom WAF Rules (Suspicious ASNs)

### 2.1 Enable Bot Fight Mode (Free Tier)

1. Open **Cloudflare Dashboard -> Security -> Bots**.
2. Enable **Bot Fight Mode** (automatically challenges known automated scrapers and headless credential-stuffing bots at the edge).
3. Leave `/api/health` reachable for uptime monitors if you use an external probe.

### 2.2 Managed Challenge for Suspicious ASNs & Automated Probes

Cloudflare Free includes **5 Custom WAF Rules** under **Security -> WAF -> Custom rules**.

#### Rule A: Managed Challenge for High-Noise Cloud / Scraper ASNs on Write & Auth Routes

Challenge automated cloud-host traffic attempting to hit authentication or upload endpoints while letting normal residential/mobile browsers browse maps without friction:

- **Rule name:** `TrailForge Challenge Suspicious ASNs on Auth/Write`
- **Expression (replace placeholder ASNs with abusive ASNs observed in Cloudflare Security Events):**
  ```text
  (
    (http.request.uri.path starts_with "/api/auth/"
      or http.request.uri.path starts_with "/api/trips/"
      or http.request.uri.path starts_with "/api/reports")
    and http.request.method in {"POST" "PUT" "PATCH" "DELETE"}
    and (
      ip.geoip.asnum in {64512 64513}
      or http.user_agent eq ""
      or lower(http.user_agent) contains "python-requests"
      or lower(http.user_agent) contains "scrapy"
    )
  )
  ```
- **Action:** `Managed Challenge`

#### Rule B: Optional Admin Endpoint Hardening

TrailForge enforces `Depends(require_admin)` in FastAPI on all `/api/admin/*` routes. For defense-in-depth, you can optionally challenge non-Australian traffic or restrict `/api/admin/*` by country or static IP (`192.0.2.0/24` example):

```text
(http.request.uri.path starts_with "/api/admin/" and not ip.geoip.country in {"AU"})
```

- **Action:** `Managed Challenge`

---

## 3. Cache Rules for R2 Custom Domain (`tiles.example.com/*`)

Serving `PMTiles` and regional offline packs through an R2 custom domain (`https://tiles.example.com`) delivers **$0 egress** and global edge caching for HTTP `206 Partial Content` byte-range slices.

### 3.1 Apply CORS Policy First

Apply [`r2-cors.json`](./r2-cors.json) to your R2 bucket (**R2 -> Bucket -> Settings -> CORS Policy**) so browsers on `https://trails.example.com` can send `Range` / `If-Range` / `If-None-Match` headers and read `Content-Range`, `Content-Length`, `Accept-Ranges`, and `ETag`.

### 3.2 Create the Edge Cache Rule

Open **Cloudflare Dashboard -> Caching -> Cache Rules -> Create rule**:

- **Rule name:** `TrailForge R2 PMTiles & Pack Cache`
- **Incoming requests match (Custom filter expression):**
  ```text
  (http.host eq "tiles.example.com" and (
    ends_with(http.request.uri.path, ".pmtiles")
    or ends_with(http.request.uri.path, ".tar.gz")
    or ends_with(http.request.uri.path, ".json")
  ))
  ```
- **Cache eligibility:** `Eligible for cache`
- **Edge TTL:** `Use cache-control header if present, bypass cache if not`
  *(TrailForge's `python -m trailforge.packs.r2_sync` sets `Cache-Control: public, max-age=86400` on `*.pmtiles` and `*.tar.gz`, and `Cache-Control: public, max-age=300` on `manifest.json` / `snapshot.json`.)*
- **Browser TTL:** `Respect origin`

### 3.3 Protect Binary `Range` / `206 Partial Content` Integrity

1. Keep **Compression / Auto Minify / Email Address Obfuscation / Rocket Loader** disabled for `tiles.example.com/*` (use a **Configuration Rule** matching `http.host eq "tiles.example.com"` if enabled zone-wide) so Cloudflare never mutates binary `PMTiles` byte ranges.
2. Verify edge caching and HTTP `206` byte-range responses after syncing tiles:
   ```bash
   curl -sSI \
     -H "Origin: https://trails.example.com" \
     -H "Range: bytes=0-127" \
     "https://tiles.example.com/trailforge_vector.pmtiles"
   ```
   Expected response headers:
   - `HTTP/2 206`
   - `accept-ranges: bytes`
   - `content-range: bytes 0-127/...`
   - `access-control-allow-origin: https://trails.example.com`
   - `etag: "..."`
   - `cf-cache-status: HIT` (on the second request)

---

## 4. Free Cookie-Free Cloudflare Web Analytics

Cloudflare Web Analytics provides real-user pageviews, referrers, countries, and Core Web Vitals for **$0** with **zero cookies and zero PII**, matching TrailForge's privacy-by-design architecture.

### Option A: One-Click Enable on Cloudflare Pages (Recommended)

1. Open **Cloudflare Dashboard -> Workers & Pages -> `trailforge` Pages project -> Metrics**.
2. Click **Enable Web Analytics**.
3. Cloudflare automatically injects the lightweight, cookie-free beacon on HTML pages served by Pages without modifying `web/` source files.

### Option B: Zone-Wide Automatic Setup

1. Open **Cloudflare Dashboard -> Analytics & Logs -> Web Analytics -> Add a site**.
2. Enter your proxied hostname (`trails.example.com`) and select **Automatic setup**.

Pair Cloudflare Web Analytics (frontend pageviews & edge hits) with TrailForge's built-in **Admin Console** (`GET /api/admin/stats`) to monitor 24h/7d API route counts, error rates, `429` rate-limit hits, active accounts/invites, and `/data/uploads` disk quota usage from one place.
