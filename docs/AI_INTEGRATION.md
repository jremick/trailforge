# TrailForge AI & MCP Integration Guide

Connect **ChatGPT**, **Claude** (Web, Desktop, Code), **Google Antigravity (AGY)**, **Gemini** (Enterprise & CLI), **Cursor**, **VS Code**, and any other **Model Context Protocol (MCP)** client directly to your TrailForge instance for grounded Australian 4WD trail intelligence, interactive maps, linked elevation profiles, closure checks, rig suitability analysis, and multi-track route planning.

TrailForge embeds a **stateless Streamable HTTP MCP server** directly inside FastAPI alongside self-hosted **Personal Access Tokens (PATs)** and **OAuth 2.1 + PKCE + Dynamic Client Registration (`RFC 7591`)** — requiring **zero extra containers, zero Redis/brokers, zero headless Chromium, and zero third-party identity providers**.

> Replace `https://trails.example.com` throughout this guide with your own TrailForge public HTTPS domain (or `http://localhost:5173` for local Docker Compose development).

---

## 1. Endpoints, Auth Modes & 3-Tier Visual Experience

### 1.1 MCP & OAuth 2.1 Endpoint Topology

| Endpoint | Auth Policy | Best For |
| :--- | :--- | :--- |
| `https://trails.example.com/mcp` | **Hybrid:** Anonymous public access + optional `Bearer` (PAT or OAuth access token) | Instant zero-login browsing, PAT Bearer clients (Claude Code, Cursor, VS Code, Gemini CLI, Antigravity) |
| `https://trails.example.com/mcp/auth` | **Required `Bearer`:** Returns `HTTP 401` + `WWW-Authenticate` RFC 9728 metadata when anonymous | OAuth 2.1 sign-in flows (ChatGPT Web/Desktop, Claude.ai / Claude Desktop, Gemini Enterprise, Antigravity OAuth) |
| `https://trails.example.com/.well-known/oauth-protected-resource` | Public (`RFC 9728`) | Protected resource discovery (plus `/mcp` and `/mcp/auth` path-scoped variants) |
| `https://trails.example.com/.well-known/oauth-authorization-server` | Public (`RFC 8414`) | Self-hosted OAuth 2.1 Authorization Server metadata (`/api/oauth/*`) |

### 1.2 3-Tier Visual Output (Works in Every Client)

1. **Tier 1 — Interactive SEP-1865 MCP App (`ui://trailforge/explorer.html`):** Supported hosts (ChatGPT, Claude, Antigravity, MCP App-capable IDEs) render an interactive map + synchronized elevation profile (`explore`, `track`, `route`, `compare` modes) with live hover scrubbing, explainable grade breakdowns, closure/gate alerts, and rig suitability pills. If WebGL or external tiles are blocked/unavailable, it automatically renders a **zero-network interactive SVG/Canvas vector map** so the widget is never blank.
2. **Tier 2 — Pure-Python Pillow PNG Fallbacks (`ImageContent`):** `inspect_track`, `plan_route`, `compare_tracks`, and `check_area_conditions` also attach crisp dark-topo PNG charts (`< 120 KB` each, zero Chromium) for text/image-only clients (CLI tools, mobile chat, consumer clients).
3. **Tier 3 — Grounded Markdown + Web UI Deep Links:** Every tool includes concise Markdown tables, safety callouts, required attributions, GPX/GeoJSON & GridForge 300 DPI CLI handoffs, and clickable deep links directly back into your TrailForge web map (`?track=...&profile=1`, `?route=<track_id_1>,<track_id_2>&rig=<vehicle_class>`).

### 1.3 Available Tools & Safety Boundaries

- **Public Read & Planning Tools (Anonymous or Authenticated):** `search_trails`, `inspect_track`, `plan_route`, `compare_tracks`, `check_area_conditions`, `find_pois_near`, `export_route_file`, `list_regions_and_packs`, `open_trailforge_map`
- **Personal Read Tools (Requires `trailforge:read`):** `get_my_garage`, `list_my_saved_routes`, `list_my_trips_and_pins`
- **Private Write Tools (Requires `trailforge:write`):** `save_planned_route`, `save_personal_pin`, `toggle_favorite_track`
- **MCP Resources & Prompts:** `ui://trailforge/explorer.html`, `trailforge://grading-methodology`, `trailforge://vehicle-classes`, `trailforge://regions`, plus guided prompts (`plan-weekend-trip`, `pre-trip-safety-check`, `find-tracks-for-rig`).
- **Strict Privacy & Write Safety:**
  - Public tools never expose any user's `email`, `user_id`, private trips, personal pins, favorites, or saved routes.
  - Private trip summaries preserve TrailForge's mandatory `250 m` start/end home-zone privacy trimming.
  - **Zero MCP tools** can submit, edit, or vote on public community `condition_reports`, upload telemetry, or call `/api/admin/*` endpoints.

---

## 2. Authentication Options

### Option A: Anonymous Public Access (Zero Setup)
Point any MCP client at `https://trails.example.com/mcp`. All 9 public trail-intelligence, safety, route-planning, and GPX export tools work immediately without an account.

### Option B: Personal Access Tokens (`tf_pat_...`)
Best for CLI tools, IDEs, and local `stdio` bridges:

1. Sign in to `https://trails.example.com` and open **Account -> AI & MCP Access Tokens** (or call `POST /api/auth/tokens` with an active web session).
2. Choose a label (e.g. `Claude Code Laptop`) and scope:
   - `trailforge:read` — read your Vehicle Profiles (garage), saved routes, personal pins, favorites, and private trip summaries.
   - `trailforge:read trailforge:write` — also allow the AI to save planned routes, personal pins, and favorite tracks to your own account.
3. Copy the plaintext `tf_pat_...` token shown **once** (TrailForge stores only its SHA-256 hash).
4. Revoke any token instantly from the Account Panel; logging out or deactivating an account immediately invalidates all issued tokens.

### Option C: Self-Hosted OAuth 2.1 + PKCE + Dynamic Client Registration
Best for web and desktop AI clients (ChatGPT, Claude.ai, Gemini Enterprise):

1. Point the client to `https://trails.example.com/mcp/auth`.
2. TrailForge responds with `HTTP 401` and `WWW-Authenticate` pointing to `/.well-known/oauth-protected-resource/mcp/auth`.
3. The client discovers `/.well-known/oauth-authorization-server`, registers via `POST /api/oauth/register` (RFC 7591 DCR, strictly validating HTTPS or loopback redirect URIs), and opens `/api/oauth/authorize` in your browser with mandatory `PKCE S256`.
4. Sign in with your normal TrailForge account, review the requested scopes, and approve.
5. View or revoke connected AI apps at any time in **Account -> Connected AI Apps** (`/api/auth/oauth-grants`). Refresh tokens rotate on every use with automatic token-family revocation on replay.

---

## 3. Client Setup Guides

### 3.1 ChatGPT (Web & Desktop)

1. Open **ChatGPT -> Settings -> Apps & Connectors** (enable **Developer Mode** if custom MCP connectors are gated in your workspace).
2. Click **Create / Add Custom Connector** (MCP):
   - **Name:** `TrailForge`
   - **MCP Server URL (Anonymous public browsing):** `https://trails.example.com/mcp`
   - **MCP Server URL (Personal garage & saved routes via OAuth 2.1):** `https://trails.example.com/mcp/auth`
   - **Authentication:** `None` (for `/mcp`) or `OAuth` (for `/mcp/auth` — ChatGPT automatically completes RFC 9728/8414 discovery, RFC 7591 DCR, and PKCE `S256`).
3. Save and start asking questions such as:
   > *"Check closures and 72-hour rainfall around Davies Plain, then plan a Moderate route for a Touring 4WD and show the map and elevation profile."*

### 3.2 Claude (Web, Desktop & Claude Code)

#### Claude.ai & Claude Desktop (Remote Connector)
1. Open **Settings -> Connectors -> Add Custom Connector**.
2. Enter:
   - **Name:** `TrailForge`
   - **Remote MCP server URL:** `https://trails.example.com/mcp` *(anonymous)* or `https://trails.example.com/mcp/auth` *(OAuth 2.1 sign-in for your garage, saved routes, pins, and favorites)*.
3. Complete the browser consent prompt if using `/mcp/auth`.

#### Claude Code CLI
Add TrailForge directly over Streamable HTTP:

```bash
# Public anonymous access
claude mcp add --transport http trailforge https://trails.example.com/mcp

# Authenticated with a scoped Personal Access Token (export TRAILFORGE_API_TOKEN first)
claude mcp add --transport http trailforge https://trails.example.com/mcp \
  --header "Authorization: Bearer ${TRAILFORGE_API_TOKEN}"
```

#### Claude Desktop (`claude_desktop_config.json` via local `stdio` bridge)
If you prefer connecting via the local `trailforge-mcp` bridge:

```json
{
  "mcpServers": {
    "trailforge": {
      "command": "trailforge-mcp",
      "args": [],
      "env": {
        "TRAILFORGE_MCP_URL": "https://trails.example.com/mcp",
        "TRAILFORGE_API_TOKEN": "<optional-tf-pat-token>"
      }
    }
  }
}
```

### 3.3 Google Antigravity (AGY) Plugin & Live Side Panel (`aux_pane`)

TrailForge ships a ready-to-install Antigravity plugin template under [`deploy/ai/antigravity-plugin/`](../deploy/ai/antigravity-plugin/):

```bash
mkdir -p ~/.gemini/config/plugins
cp -R deploy/ai/antigravity-plugin ~/.gemini/config/plugins/trailforge
```

Update `~/.gemini/config/plugins/trailforge/mcp_config.json` to point to your instance:

```json
{
  "mcpServers": {
    "trailforge": {
      "url": "https://trails.example.com/mcp"
    }
  }
}
```

- Switch `url` to `https://trails.example.com/mcp/auth` for OAuth 2.1 sign-in, or use the `trailforge-mcp` `stdio` bridge with `${TRAILFORGE_API_TOKEN}` (see [`deploy/ai/antigravity-plugin/README.md`](../deploy/ai/antigravity-plugin/README.md) for `${PLUGIN_ROOT}` wrapper examples).
- Antigravity automatically exposes **`TrailForge Map`** in the side pane **+** menu via `open_trailforge_map` (`com.google.antigravity/ui` `aux_pane` entrypoint), updating the interactive map and linked elevation profile live as you chat.

### 3.4 Gemini (Enterprise & Gemini CLI)

#### Gemini Enterprise
Add a remote Streamable HTTP MCP tool connection pointing to:
- `https://trails.example.com/mcp` (anonymous public intelligence or PAT Bearer header), or
- `https://trails.example.com/mcp/auth` (OAuth 2.1 + PKCE + DCR).

#### Gemini CLI (`~/.gemini/settings.json`)
Add TrailForge under `mcpServers` using Streamable HTTP (`httpUrl`) or the `trailforge-mcp` `stdio` bridge:

```json
{
  "mcpServers": {
    "trailforge": {
      "httpUrl": "https://trails.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${TRAILFORGE_API_TOKEN}"
      }
    }
  }
}
```

*(Omit the `headers` block for anonymous public access, or set `"command": "trailforge-mcp"` with `TRAILFORGE_MCP_URL` for `stdio` transport.)*

### 3.5 Cursor

Create or edit `.cursor/mcp.json` (project-scoped) or `~/.cursor/mcp.json` (global):

```json
{
  "mcpServers": {
    "trailforge": {
      "url": "https://trails.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${env:TRAILFORGE_API_TOKEN}"
      }
    }
  }
}
```

*(Omit `"headers"` for anonymous public browsing, or replace `"url"` with `"command": "trailforge-mcp"` and `"env": {"TRAILFORGE_MCP_URL": "https://trails.example.com/mcp"}` for `stdio`.)*

### 3.6 VS Code (GitHub Copilot / Native MCP)

Add `.vscode/mcp.json` to your workspace (using VS Code's `inputs` prompt so tokens are never committed to Git):

```json
{
  "inputs": [
    {
      "type": "promptString",
      "id": "trailforge_api_token",
      "description": "Optional TrailForge Personal Access Token (leave blank for anonymous read-only)",
      "password": true
    }
  ],
  "servers": {
    "trailforge": {
      "type": "http",
      "url": "https://trails.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${input:trailforge_api_token}"
      }
    }
  }
}
```

*(For purely anonymous public usage, omit `inputs` and `headers`.)*

---

## 4. Local `stdio` Bridge (`trailforge-mcp`)

For MCP hosts that only support local `stdio` servers — or for offline local PostGIS development — TrailForge provides the `trailforge-mcp` CLI (`python -m trailforge.mcp.stdio_proxy`):

```bash
# Proxy stdio <-> remote or local Docker Compose Streamable HTTP endpoint
export TRAILFORGE_MCP_URL="https://trails.example.com/mcp"
export TRAILFORGE_API_TOKEN="<optional-tf-pat-token>"
trailforge-mcp

# Or run directly via Python module
python -m trailforge.mcp.stdio_proxy

# Direct in-process PostGIS mode (no HTTP server required; uses DATABASE_URL)
trailforge-mcp --direct
```

| Environment Variable | Default | Purpose |
| :--- | :--- | :--- |
| `TRAILFORGE_MCP_URL` | `http://localhost:5173/mcp` | Upstream Streamable HTTP MCP endpoint |
| `TRAILFORGE_API_TOKEN` | *(empty)* | Optional `tf_pat_...` Personal Access Token sent as `Authorization: Bearer ...` |

---

## 5. Self-Hosting & Edge Proxy Deployment

### 5.1 Environment Configuration

All 9 AI/MCP and OAuth 2.1 variables are documented in [`.env.example`](../.env.example) and [`deploy/railway/env.railway.example`](../deploy/railway/env.railway.example):

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TRAILFORGE_ENABLE_MCP` | `true` | Master kill switch for `/mcp`, `/mcp/auth`, and `/.well-known/oauth-*` (returns `404` when `false`) |
| `TRAILFORGE_MCP_AUTH_MODE` | `hybrid` | `hybrid` (anonymous `/mcp` + OAuth `/mcp/auth`), `oauth_required`, or `anonymous_only` |
| `TRAILFORGE_PUBLIC_BASE_URL` | `""` | Canonical public HTTPS origin (e.g. `https://trails.example.com`) for OAuth metadata & deep links — prevents internal hostnames from leaking |
| `TRAILFORGE_RATE_LIMIT_MCP_PER_MIN` | `60` | Dedicated sliding-window rate limit for `/mcp` and `/mcp/auth` (tracked under `route_group="mcp"` in Admin Console) |
| `TRAILFORGE_MCP_APP_HTML` | `""` | Optional local file path override for hot-reloading `ui://trailforge/explorer.html` during widget development |
| `TRAILFORGE_MCP_CSP_TILE_DOMAINS` | `""` | Comma-separated bare origins allowed in `_meta.ui.csp` (`connectDomains`/`resourceDomains`; auto-includes origin of `TILES_PUBLIC_BASE_URL`) |
| `TRAILFORGE_OAUTH_ACCESS_TOKEN_TTL_S` | `3600` | OAuth 2.1 access token lifetime in seconds (1 hour) |
| `TRAILFORGE_OAUTH_REFRESH_TOKEN_TTL_S` | `2592000` | OAuth 2.1 refresh token lifetime in seconds (30 days) |
| `TRAILFORGE_OAUTH_DCR_MAX_PER_IP_PER_DAY` | `20` | Daily per-IP cap on Dynamic Client Registration (`POST /api/oauth/register`) |

### 5.2 Cloudflare Pages + Railway Deployment

To expose `/api/*`, `/mcp`, `/mcp/*`, and `/.well-known/oauth-*` on the same origin (`https://trails.example.com`) behind Cloudflare Pages while keeping Railway origin lockdown (`X-TrailForge-Origin-Secret`) active:

1. **Mount the Pages Middleware during your Cloudflare Pages build** (Root directory = `web`):
   ```bash
   mkdir -p functions
   cp ../deploy/cloudflare/pages-proxy-worker.js functions/pages-proxy-worker.js
   cp ../deploy/cloudflare/pages-middleware.js functions/_middleware.js
   npm ci && npm run build
   ```
   - `pages-middleware.js` routes `/api`, `/api/*`, `/mcp`, `/mcp/*`, and `/.well-known/oauth-*` through `pages-proxy-worker.js` and calls `context.next()` for all static SPA assets (`/`, `/assets/*`, `/icons/*`, `/sw.js`).
   - `pages-proxy-worker.js` streams request and response bodies without buffering (preserving SSE frames and `206 Partial Content` slices), forwards `Mcp-Session-Id`, `MCP-Protocol-Version`, `Accept`, `Last-Event-ID`, `Authorization`, `Cookie`, and `CF-Connecting-IP`, injects `X-TrailForge-Origin-Secret`, preserves `Mcp-Session-Id` and `WWW-Authenticate` response headers, and rewrites internal `307` redirects back to `https://trails.example.com`.
2. **Set Environment Variables:**
   - **Cloudflare Pages:** `RAILWAY_API_ORIGIN=https://api.example.com` and `ORIGIN_SHARED_SECRET=<same-64-hex-secret>`
   - **Railway `api`:** `TRAILFORGE_PUBLIC_BASE_URL=https://trails.example.com`, `TRAILFORGE_ORIGIN_SHARED_SECRET=<same-64-hex-secret>`, and `TRAILFORGE_ENABLE_MCP=true`
3. **Cloudflare WAF / Bot Fight Mode Note:**
   If you enable aggressive Cloudflare Bot Fight Mode or Managed Challenges across the entire zone, add a WAF exception / skip rule for `/mcp`, `/mcp/*`, and `/.well-known/oauth-*` (while relying on TrailForge's built-in `TRAILFORGE_RATE_LIMIT_MCP_PER_MIN` and `TRAILFORGE_OAUTH_DCR_MAX_PER_IP_PER_DAY` limits) so automated AI clients (ChatGPT, Claude, Antigravity, Gemini) are not blocked by browser JS challenges.

### 5.3 Cloudflare R2 CORS Trade-Offs (`r2-cors.json` vs `r2-cors-mcp.json`)

SEP-1865 MCP Apps run inside sandboxed **opaque-origin (`null`) or unique-origin iframes** hosted by the AI client (ChatGPT, Claude, Antigravity). This creates an explicit choice for your public `PMTiles` R2 bucket (`https://tiles.example.com`):

| R2 CORS Profile | `AllowedOrigins` | MCP App Map Experience | Security & Cost Trade-Off |
| :--- | :--- | :--- | :--- |
| **[`deploy/cloudflare/r2-cors.json`](../deploy/cloudflare/r2-cors.json)** *(Default — strict)* | `["https://trails.example.com"]` | Zero-network **interactive SVG/Canvas vector map** (pan/zoom, grade colors, closures, POIs, synchronized elevation scrubbing) automatically activates inside MCP App iframes; main web UI uses full MapLibre `PMTiles`. | Prevents third-party websites from hotlinking your R2 `PMTiles` bucket; lowest read-operation exposure. |
| **[`deploy/cloudflare/r2-cors-mcp.json`](../deploy/cloudflare/r2-cors-mcp.json)** *(Opt-in wildcard read-only)* | `["*"]` (`GET`/`HEAD` only) | Full **live MapLibre GL JS 5** vector/terrain `PMTiles` inside MCP App iframes (ensure `TILES_PUBLIC_BASE_URL` or `TRAILFORGE_MCP_CSP_TILE_DOMAINS=https://tiles.example.com` is set). | Strictly read-only (`GET`/`HEAD` only — zero write/delete risk, `$0` R2 egress), but third-party sites could read your public tiles; monitor R2 Class B read quotas. |

> **Zero-Failure Guarantee:** Even with strict single-origin `r2-cors.json` or completely offline homelabs, TrailForge's MCP App automatically switches to its built-in interactive SVG/Canvas map renderer without errors.

### 5.4 Docker Compose (`web/nginx.conf`), Caddy v2 & Local Vite Dev

- **Docker Compose (`web/nginx.conf`):** Proxies `^/mcp(/|$)` and `^/\.well-known/oauth-` to `http://api:8000` with `proxy_buffering off`, `proxy_request_buffering off`, `gzip off` (on `/mcp`), HTTP/1.1 keepalive, and full MCP/OAuth header forwarding (`Mcp-Session-Id`, `MCP-Protocol-Version`, `Last-Event-ID`, `WWW-Authenticate`, `X-Forwarded-Host`).
- **Caddy v2 (`deploy/Caddyfile.example`):** Excludes `/mcp` and `/mcp/*` from `@compressible` (`encode`) and sets `flush_interval -1` plus `X-Forwarded-Host` so SSE frames flush immediately without gzip/zstd buffering.
- **Local Vite Dev (`web/vite.config.ts`):** Proxies `/api`, `/mcp`, and `/.well-known` to `http://localhost:8000` with `xfwd: true`.

---

## 6. Verification Smoke Commands

Verify your deployed or local MCP and OAuth discovery surface with `curl`:

```bash
# 1. Verify RFC 9728 Protected Resource Metadata
curl -fsS https://trails.example.com/.well-known/oauth-protected-resource/mcp/auth

# 2. Verify RFC 8414 Authorization Server Metadata
curl -fsS https://trails.example.com/.well-known/oauth-authorization-server

# 3. Verify /mcp/auth returns HTTP 401 with WWW-Authenticate discovery header
curl -sSI -X POST https://trails.example.com/mcp/auth \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}'

# 4. Verify anonymous Streamable HTTP initialize on /mcp
curl -fsS -X POST https://trails.example.com/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}'
```

---

## 7. Licensing, ODbL Separation & Required Attributions

- **Source Code License:** [`GPL-3.0-or-later`](../LICENSE).
- **Strict 3-Tier OSM ODbL Separation:** All AI authentication tables (`personal_access_tokens`, `oauth_clients`, `oauth_authorization_codes`, `oauth_tokens`) live in Tier C with zero OpenStreetMap geometry or tag columns.
- **Mandatory Data Attributions:** Every TrailForge MCP text summary, structured JSON payload (`attributions`), rendered PNG chart, and interactive MCP App footer preserves required attributions:
  - **OpenStreetMap contributors** (`ODbL` — Open Database License)
  - **Geoscience Australia (GA), Digital Earth Australia (DEA), DCCEEW CAPAD 2025, and Australian State & Territory mapping and emergency agencies** (DEECA/Vicmap, NSW Spatial Services, RFS/NPWS, VicEmergency, ACT ESA, QLD, WA, SA, NT under **`CC BY 4.0`**; Tasmania / The LIST under **`CC BY 3.0 AU`**)
  - **Open-Meteo** weather & rainfall data (**`CC BY 4.0`**)
