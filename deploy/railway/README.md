# Deploying TrailForge on Railway + Cloudflare (~$5–$10/mo Hard-Capped)

This playbook walks through running a hardened, public-facing **TrailForge** instance on **Railway** (PostGIS + FastAPI API + background Worker) paired with **Cloudflare** (Pages frontend + Pages Function `/api/*` proxy + R2 `$0`-egress `PMTiles` & offline pack storage + Free WAF & Web Analytics).

Anonymous visitors can browse tracks, live difficulty grades, closures, elevation profiles, and route planning freely, while account registration stays locked to **`invite_only`** and expensive compute endpoints (`POST /api/packs/*/build`, `POST /api/closures/sync`, `POST /api/grading/recompute`, `/api/admin/*`) are strictly **admin-only**.

---

## Architecture & Cost Profile (~$5–$10/mo Hard-Capped)

| Component | Host / Tier | Est. Monthly Cost | Purpose |
| --- | --- | --- | --- |
| Frontend PWA + `/api/*` proxy | Cloudflare Pages + Functions (Free) | **$0** | MapLibre GL JS 6 PWA, Service Worker, same-origin API proxy |
| Vector/Terrain `PMTiles` & Offline Packs | Cloudflare R2 (Free: 10 GB, 10M reads, **$0 egress**) | **$0** | HTTP `206 Partial Content` tile streaming & `.tar.gz` downloads offloaded from Railway |
| Edge DDoS, Rate Limiting & Analytics | Cloudflare Free WAF + Web Analytics | **$0** | Edge IP throttling, Bot Fight Mode, cookie-free traffic stats |
| Service 1: `postgis` | Railway (`imresamu/postgis:16-3.4`, `/var/lib/postgresql/data`) | Included in Hobby | Lean PostgreSQL 16 + PostGIS 3.4 on private network |
| Service 2: `api` | Railway (`Dockerfile` / `railway.toml` or GHCR image, `/data`) | Included in Hobby | FastAPI + Alembic + in-process rate limiter + usage metrics |
| Service 3: `worker` | Railway (same backend image, `python -m trailforge.worker.main`) | ~$0–$5 overage | Background agency closure sync & live rainfall grading ticks |

> **Why R2 Offload Matters:** Serving large vector/terrain `PMTiles` and regional offline packs from Cloudflare R2 (`$0` egress) keeps Railway outbound bandwidth near zero — even during a traffic spike.

---

## Step 1: Set a Hard Monthly Usage Limit in Railway (First!)

Before deploying any services, lock in a hard billing ceiling so viral traffic or a misconfigured loop can **never** create a surprise bill:

1. Open **Railway Dashboard -> Workspace Settings -> Billing -> Usage Limits**.
2. Set **Hard Limit** to **`$10`** (or **`$15`** if you want extra headroom above the $5/mo Hobby plan credit). When reached, Railway automatically pauses workloads instead of billing uncapped overages.
3. Set **Email Alert (Soft Limit)** to **`$5`** so you receive an email the moment usage approaches your monthly credit.

---

## Step 2: Provision the 3 Railway Services

Create a new Railway project (e.g., `trailforge-prod`) and add the three services below inside the same project so they communicate over Railway's private internal network (`*.railway.internal`, `$0` egress).

### Service 1: `postgis` (PostgreSQL 16 + PostGIS 3.4)

1. Click **+ New -> Docker Image** and set image:
   ```text
   imresamu/postgis:16-3.4
   ```
   *(Multi-arch `linux/amd64` + `linux/arm64` build from the PostGIS maintainer.)*
2. Attach a **Persistent Volume** mounted at:
   ```text
   /var/lib/postgresql/data
   ```
3. Under **Settings -> Deploy -> Custom Start Command**, configure lean PostgreSQL memory flags matching `docker-compose.yml`:
   ```bash
   postgres -c shared_buffers=128MB -c effective_cache_size=256MB -c work_mem=8MB -c maintenance_work_mem=64MB -c max_connections=50
   ```
4. Under **Variables**, set:
   ```dotenv
   POSTGRES_DB=trailforge
   POSTGRES_USER=trailforge
   POSTGRES_PASSWORD=<generate-with-openssl-rand-hex-24>
   PGDATA=/var/lib/postgresql/data/pgdata
   ```
   *(Using a `pgdata` subdirectory avoids Railway volume `lost+found` initdb errors.)*
5. Leave **Public Networking disabled** — `postgis` should stay private to Railway's internal network.

### Service 2: `api` (FastAPI + Alembic + GIS API)

Choose either GitHub repo builds (recommended — automatically picks up [`railway.toml`](../../railway.toml)) or zero-build prebuilt GHCR images:

- **Option A — Build from GitHub Repo (`railway.toml`):**
  1. Click **+ New -> GitHub Repo** and select your TrailForge repository.
  2. Railway automatically detects [`railway.toml`](../../railway.toml), which configures:
     - `dockerfilePath = "Dockerfile"`
     - `startCommand = "sh -c 'alembic upgrade head && exec uvicorn trailforge.api.main:app --host 0.0.0.0 --port ${PORT:-8000}'"`
     - `healthcheckPath = "/api/health"` (`healthcheckTimeout = 120`, `restartPolicyType = "ON_FAILURE"`, `restartPolicyMaxRetries = 5`)
- **Option B — Prebuilt GHCR Image (Zero Build Minutes):**
  1. Click **+ New -> Docker Image** and set:
     ```text
     ghcr.io/jremick/trailforge-backend:latest
     ```
     *(Or pin a release tag such as `ghcr.io/jremick/trailforge-backend:v1.1.0`.)*
  2. Set **Custom Start Command**:
     ```bash
     sh -c 'alembic upgrade head && exec uvicorn trailforge.api.main:app --host 0.0.0.0 --port ${PORT:-8000}'
     ```
  3. Set **Healthcheck Path** to `/api/health` and **Healthcheck Timeout** to `120`.

For both options:
1. Attach a **Persistent Volume** mounted at `/data` (stores `/data/uploads`, `/data/tiles`, `/data/packs`, `/data/cache`).
2. Generate strong secrets locally:
   ```bash
   openssl rand -hex 32   # -> AUTH_SECRET_KEY
   openssl rand -hex 32   # -> TRAILFORGE_PRIVACY_SECRET
   openssl rand -hex 32   # -> TRAILFORGE_ORIGIN_SHARED_SECRET (also Cloudflare ORIGIN_SHARED_SECRET)
   ```
3. Paste the production variables from [`env.railway.example`](./env.railway.example) into `api -> Variables`, making sure you set:
   - `DATABASE_URL=postgresql+psycopg://${{postgis.POSTGRES_USER}}:${{postgis.POSTGRES_PASSWORD}}@${{postgis.RAILWAY_PRIVATE_DOMAIN}}:5432/${{postgis.POSTGRES_DB}}`
   - `TRAILFORGE_REGISTRATION_MODE=invite_only`
   - `TRAILFORGE_ADMIN_EMAIL=admin@example.com`
   - `AUTH_SECRET_KEY=<generated-64-hex>`
   - `TRAILFORGE_PRIVACY_SECRET=<generated-64-hex>`
   - `TRAILFORGE_ORIGIN_SHARED_SECRET=<generated-64-hex>` *(blocks direct unauthenticated `*.up.railway.app` access with HTTP 403)*
   - `TRAILFORGE_ENABLE_DOCS=false` *(disables public `/docs`, `/redoc`, and `/openapi.json` in production)*
   - `AUTH_COOKIE_SECURE=true`
   - `CORS_ORIGINS=https://trails.example.com` *(explicit origins only; wildcard `*` is rejected at startup)*
   - `TRAILFORGE_PUBLIC_BASE_URL=https://trails.example.com` *(required in production when `TRAILFORGE_ENABLE_MCP=true`)*
   - `TRAILFORGE_TRUST_PROXY_HEADERS=true` and `TRAILFORGE_TRUST_CLOUDFLARE_HEADERS=true` *(auto-enabled when `TRAILFORGE_ORIGIN_SHARED_SECRET` is set)*
   - `TILES_PUBLIC_BASE_URL=https://tiles.example.com`
   - `PACKS_PUBLIC_BASE_URL=https://tiles.example.com` *(or `https://packs.example.com`)*
   - `TRAILFORGE_DEMO_BANNER=...`
   - `TRAILFORGE_RATE_LIMIT_ENABLED=true`
   - `TRAILFORGE_UPLOADS_MAX_TOTAL_MB=500`
4. Under **Settings -> Networking**, generate a Railway domain (or attach a custom domain such as `api.example.com`). You will supply this URL as `RAILWAY_API_ORIGIN` in Cloudflare Pages.

### Service 3: `worker` (Background Closure Sync & Live Grading)

1. Add a third service using the **same GitHub repo or `ghcr.io/jremick/trailforge-backend:latest` image**.
2. Override **Settings -> Deploy -> Custom Start Command** to:
   ```bash
   python -m trailforge.worker.main
   ```
3. Clear/disable any HTTP **Healthcheck Path** on `worker` (it does not listen on `$PORT`).
4. Copy the shared variables from `api` (`DATABASE_URL`, `TRAILFORGE_PRIVACY_SECRET`, `TRAILFORGE_ENABLE_LIVE_FEEDS=true`, `TRAILFORGE_ENABLED_STATES`, `WORKER_POLL_SECONDS=60`, `PRIVACY_TRIM_METERS=250`, `GDAL_CACHEMAX=192`, `OMP_THREAD_LIMIT=1`).
5. A persistent volume on `worker` is **optional** when `PMTiles` and offline packs are built from `api`/CLI and served via Cloudflare R2.

---

## Step 3: Configure Cloudflare R2, Pages Proxy & WAF

### 3.1 Cloudflare R2 Bucket (`tiles.example.com`)

1. Create an R2 bucket (e.g., `trailforge-tiles`) in **Cloudflare Dashboard -> R2 Object Storage**.
2. Under **Bucket -> Settings -> Custom Domains**, connect `tiles.example.com` (enables Cloudflare CDN caching + `$0` egress).
3. Under **Bucket -> Settings -> CORS Policy**, paste [`deploy/cloudflare/r2-cors.json`](../cloudflare/r2-cors.json) and replace `https://trails.example.com` with your frontend domain. This exposes `Content-Range`, `Content-Length`, `Accept-Ranges`, and `ETag` required for browser MapLibre `PMTiles` HTTP `206 Partial Content` streaming.
4. Under **R2 -> Manage R2 API Tokens**, create an Object Read & Write token scoped to `trailforge-tiles` and set `TRAILFORGE_R2_ENDPOINT_URL`, `TRAILFORGE_R2_BUCKET`, `TRAILFORGE_R2_ACCESS_KEY_ID`, and `TRAILFORGE_R2_SECRET_ACCESS_KEY` on Railway `api`.

### 3.2 Cloudflare Pages + `/api/*` Proxy Function (`trails.example.com`)

1. Connect your TrailForge repository in **Cloudflare Dashboard -> Workers & Pages -> Create -> Pages**:
   - **Root directory:** `web`
   - **Build command:** `npm ci && npm run build`
   - **Build output directory:** `dist`
2. Place [`deploy/cloudflare/pages-proxy-worker.js`](../cloudflare/pages-proxy-worker.js) at `web/functions/api/[[path]].js` in your deployment branch (or copy it during your Pages build command: `mkdir -p functions/api && cp ../deploy/cloudflare/pages-proxy-worker.js functions/api/[[path]].js && npm ci && npm run build`).
3. Under **Pages Project -> Settings -> Environment Variables (Production)**, set:
   ```dotenv
   RAILWAY_API_ORIGIN=https://api.example.com
   ORIGIN_SHARED_SECRET=<same-64-hex-as-TRAILFORGE_ORIGIN_SHARED_SECRET>
   ```
   *(Replace `RAILWAY_API_ORIGIN` with your Railway `api` service HTTPS origin, with no trailing slash. `ORIGIN_SHARED_SECRET` is injected as `X-TrailForge-Origin-Secret` on every proxied request so direct hits to Railway are blocked with `403`.)*
4. Attach your custom domain `trails.example.com` to the Pages project. Same-origin `/api/*` requests now forward `CF-Connecting-IP`, `X-TrailForge-Origin-Secret`, `Cookie`, `Authorization`, and `Range` headers cleanly while preserving `Set-Cookie`, rewriting upstream `307` redirect `Location` headers back to `https://trails.example.com`, emitting global security response headers, and streaming unbuffered `206 Partial Content` slices.

### 3.3 Cloudflare WAF, Rate Limits, R2 Cache Rule & Web Analytics

Follow [`deploy/cloudflare/waf-rules.md`](../cloudflare/waf-rules.md) to enable:
- Per-IP edge rate limiting (`/api/auth/*`, `/api/trips/*`, `/api/reports`, `/api/*`)
- Bot Fight Mode + Managed Challenge custom rules
- Cache Rules for `tiles.example.com/*` (`*.pmtiles` with HTTP `Range` support)
- Free cookie-free **Cloudflare Web Analytics**

---

## Step 4: Bootstrap Data, R2 Tiles, Admin Account & Friend Invites

Verify the live deployment health first:

```bash
curl -fsS https://trails.example.com/api/health
curl -fsS https://trails.example.com/api/auth/config
```

Connect to the Railway `api` container (via `railway ssh --service api` or `railway run --service api`) to seed/bake tracks, sync tiles to R2, and issue invite codes:

### 4.1 Seed Demo Fixtures OR Bake Real Regional Data

- **Quick synthetic Australian Alps preview (offline-safe, seconds):**
  ```bash
  python -m trailforge.ingestion.pipeline --fixtures tests/fixtures
  python -m trailforge.feeds.cli --fixtures tests/fixtures/closures_sample.json
  python -m trailforge.grading.cli --offline-rain-72h 18.5
  python -m trailforge.tiles.cli --output-dir /data/tiles --dem tests/fixtures/dem_alps_corridor.tif
  python -m trailforge.packs.cli --region australian-alps --output-dir /data/packs --dem tests/fixtures/dem_alps_corridor.tif
  ```
- **Real Australian open-data regional bake (VIC/NSW/Pan-AU):**
  ```bash
  python -W error::RuntimeWarning -m trailforge.ingestion.bake_cli \
    --region australian-alps --cache-dir /data/cache --build-tiles --build-pack
  ```

### 4.2 Sync `PMTiles` & Offline Packs to Cloudflare R2

Preview uploads first with `--dry-run`, then sync live (pure-Python SigV4 uploader, zero `boto3`, skips unchanged files automatically):

```bash
python -W error::RuntimeWarning -m trailforge.packs.r2_sync \
  --tiles-dir /data/tiles --packs-dir /data/packs --dry-run

python -W error::RuntimeWarning -m trailforge.packs.r2_sync \
  --tiles-dir /data/tiles --packs-dir /data/packs
```

Verify R2 serves HTTP `206 Partial Content` with CORS headers:

```bash
curl -sSI \
  -H "Origin: https://trails.example.com" \
  -H "Range: bytes=0-127" \
  "https://tiles.example.com/trailforge_vector.pmtiles"
```

Confirm `HTTP/2 206`, `access-control-allow-origin: https://trails.example.com`, `content-range:`, `accept-ranges: bytes`, and `etag:` appear in the response.

### 4.3 Create Your First Admin Account & Friend Invite Codes

In `TRAILFORGE_REGISTRATION_MODE=invite_only`, public sign-up cannot bypass invite codes even when `TRAILFORGE_ADMIN_EMAIL` is set. Bootstrap your first admin directly via the **Admin CLI** (`railway ssh --service api`):

```bash
# Create the first admin account safely via stdin (avoids password exposure in argv/ps)
printf '%s\n' '<strong-admin-password>' | \
  python -W error::RuntimeWarning -m trailforge.auth.cli create-admin \
    --email admin@example.com \
    --name "Trail Admin" \
    --password-stdin

# Generate a multi-use friend invite code (valid for 30 days)
python -W error::RuntimeWarning -m trailforge.auth.cli create-invite \
  --label "4WD Club Mates" \
  --max-uses 10 \
  --expires-days 30

# Inspect active/used/revoked invite codes
python -W error::RuntimeWarning -m trailforge.auth.cli list-invites

# Promote an existing member account to admin if needed
python -W error::RuntimeWarning -m trailforge.auth.cli promote \
  --email co-admin@example.com
```

*(Alternatively, create a single-use bootstrap invite code with `create-invite --max-uses 1` and register through the Web UI using the email configured in `TRAILFORGE_ADMIN_EMAIL` to auto-promote the first admin.)*

Once signed in as an admin, open the **Admin** console drawer in the web UI to view 24h/7d traffic, error and `429` counts, `/data/uploads` quota usage, trigger closure/grade syncs, create/revoke friend invites, and suspend or promote accounts.

---

## Step 5: Post-Deploy Hardening Verification

```bash
# 1. Confirm rate-limit headers are emitted on public read endpoints
curl -sSI https://trails.example.com/api/tracks | grep -i x-ratelimit

# 2. Confirm anonymous visitors are blocked (401) from admin/compute endpoints
curl -sS -o /dev/null -w "%{http_code}\n" https://trails.example.com/api/admin/stats
curl -sS -o /dev/null -w "%{http_code}\n" -X POST https://trails.example.com/api/closures/sync

# 3. Confirm tile manifest points directly to your R2 custom domain
curl -fsS https://trails.example.com/api/tiles/manifest.json

# 4. Confirm direct Railway origin access (without X-TrailForge-Origin-Secret) returns 403
#    and OpenAPI docs are disabled (404) in production mode
curl -sS -o /dev/null -w "%{http_code}\n" https://api.example.com/api/tracks
curl -sS -o /dev/null -w "%{http_code}\n" https://trails.example.com/docs
```

---

## Licensing & Required Attributions

TrailForge is licensed under **`GPL-3.0-or-later`**. Public instances must preserve attribution notices for OpenStreetMap (`ODbL`), Geoscience Australia / DEA / CAPAD / state mapping agencies (`CC BY 4.0` / `CC BY 3.0 AU` for TAS), and Open-Meteo (`CC BY 4.0`).
