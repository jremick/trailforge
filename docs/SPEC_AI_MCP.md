# TrailForge AI & MCP Integration Specification

> **Status:** Locked v1.2 Contract Specification (Ready for Implementation)  
> **Code License:** `GPL-3.0-or-later`  
> **Standards:** Model Context Protocol (Streamable HTTP + `stdio`), MCP Apps (`SEP-1865` / `ext-apps`), OAuth 2.1 (`RFC 9728`, `RFC 8414`, `RFC 7591`, `RFC 7636` PKCE)  
> **Runtime Constraints:** Embedded in FastAPI (zero extra containers, zero Redis/brokers, zero headless Chromium, zero external IdP, strict 3-tier OSM ODbL separation)

---

## 1. Problem, Users & Experience

### 1.1 Problem
TrailForge already holds rich Pan-Australia 4WD trail intelligence—deterministic DEM/OSM grading, live crowdsourced telemetry, 72-hour rainfall context, 11 official agency closure feeds, community gate/hazard reports, vehicle capability suitability, and multi-track route planning. Today that intelligence is only accessible through the web map UI or raw developer REST endpoints. Users cannot ask their AI assistant ("Plan a 2-day moderate High Country route for my Touring 4WD with campsites and no closures, and show me the map and elevation profile") and get grounded answers paired with interactive maps and elevation charts.

### 1.2 Users
- **4WD & Off-Road Drivers (End Users):** Connect ChatGPT, Claude (Web, Desktop, Code), Antigravity, Gemini (Enterprise & CLI), Cursor, or VS Code to either a public TrailForge instance or their private homelab to ask natural-language trip-planning, safety, rig-suitability, and route-comparison questions—receiving grounded answers, interactive maps, elevation graphs, GPX exports, and deep links back into TrailForge.
- **Self-Hosters & Club Admins:** Enable or disable AI access via a single environment toggle, enforce dedicated rate limits within a lean (~350 MiB RAM / ~$5–$10/mo Railway) footprint, and monitor `/mcp` usage in the Admin Console.
- **Maintainers & Contributors:** Maintain one canonical MCP contract and one single-file MCP App widget bundle instead of fragmented per-vendor AI plugins.

### 1.3 Target Client Compatibility Matrix

| AI Client | Transport | Auth Modes Supported | Visual Experience |
| :--- | :--- | :--- | :--- |
| **Claude** (Web, Desktop, Code) | Streamable HTTP (`/mcp`, `/mcp/auth`) or `stdio` | Anonymous, OAuth 2.1 (Web/Desktop), PAT Bearer (Code/Desktop) | Tier 1 Interactive MCP App (`ui://`) + Tier 2 PNG + Tier 3 Deep Links |
| **ChatGPT** (Web, Desktop) | Streamable HTTP (`/mcp`, `/mcp/auth`) | Anonymous, OAuth 2.1 + PKCE + DCR | Tier 1 Interactive MCP App (`ui://`) + Tier 2 PNG + Tier 3 Deep Links |
| **Antigravity (AGY)** | Streamable HTTP or packaged local/remote plugin | Anonymous, PAT Bearer, OAuth 2.1 | Tier 1 Interactive MCP App inline **and** live-updating `aux_pane` side panel + `updateModelContext` + Tier 2 PNG + Tier 3 Deep Links |
| **Gemini** (Enterprise & CLI) | Streamable HTTP or `stdio` | Anonymous, PAT Bearer, OAuth 2.1 | Tier 1 MCP App (Enterprise) / Tier 2 PNG + Tier 3 Deep Links (CLI & consumer surfaces) |
| **Cursor / VS Code / Goose / Open WebUI** | Streamable HTTP or `stdio` | Anonymous, PAT Bearer | Tier 1 Interactive MCP App (where supported) + Tier 2 PNG + Tier 3 Deep Links |

---

## 2. Assumptions Recorded

1. Public instances run behind Cloudflare Pages (same-origin proxy to Railway `api`) with `TRAILFORGE_ORIGIN_SHARED_SECRET` enabled; self-hosted instances run behind `nginx` (Docker Compose), Caddy, Traefik, or Tailscale Serve.
2. TrailForge's existing `users`, `vehicle_profiles`, `planned_routes`, `user_pins`, `user_favorites`, and `trips` tables remain the single source of truth for user identity and personal data—no external IdP is introduced.
3. All offline CI tests must pass with zero external network access using existing synthetic VIC/NSW fixtures under `tests/fixtures/`.
4. All required upstream attributions (OSM `ODbL`, GA/DEA/CAPAD/state agencies `CC BY 4.0` / `CC BY 3.0 AU`, Open-Meteo `CC BY 4.0`) remain mandatory across text outputs, PNG renders, and interactive MCP App widgets.

---

## 3. Checkable Success Criteria

- **`SC-1` (Stateless Streamable HTTP MCP):** Sending standard MCP JSON-RPC `initialize`, `tools/list`, `resources/list`, `prompts/list`, and `tools/call` requests to `POST /mcp` succeeds without requiring server-side sticky sessions or Redis, and returns `404` when `TRAILFORGE_ENABLE_MCP=false`.
- **`SC-2` (Anonymous-First Public Intelligence):** An unauthenticated client calling `search_trails`, `inspect_track`, `plan_route`, `compare_tracks`, `check_area_conditions`, `find_pois_near`, `export_route_file`, `list_regions_and_packs`, or `open_trailforge_map` receives grounded responses from PostGIS with zero sign-in required.
- **`SC-3` (SEP-1865 MCP App Resource):** Reading `ui://trailforge/explorer.html` returns `mimeType="text/html;profile=mcp-app"`, valid self-contained HTML under `4 MiB` (strictly `< 10 MiB`), and `_meta.ui.csp` containing only bare origins derived from configured tile/public domains. Visual tools expose `_meta.ui.resourceUri = "ui://trailforge/explorer.html"`; `open_trailforge_map` also accepts empty `{}` arguments and exposes Antigravity `com.google.antigravity/ui` `aux_pane` entrypoint metadata.
- **`SC-4` (Hybrid Map & Linked Elevation Widget):** In automated widget tests and browser verification, the MCP App renders a synchronized map + elevation profile for all 4 modes (`explore`, `track`, `route`, `compare`), falls back cleanly to the zero-network interactive SVG/Canvas vector map when WebGL or external tile URLs are unavailable, syncs host light/dark CSS variables on `onhostcontextchanged`, and gates `openLink` / `updateModelContext` / `callServerTool` on `getHostCapabilities()`.
- **`SC-5` (Lightweight PNG Fallback Visuals):** `inspect_track`, `plan_route`, `compare_tracks`, and `check_area_conditions` (when `include_images=true`, the default) return valid PNG `ImageContent` blocks generated purely via Pillow (zero Chromium) with each PNG `< 120 KB`, total `CallToolResult` serialized size strictly `< 450 KB` (under the 1 MiB trajectory cap), non-blank rendered geometry/curves, and visible attribution text.
- **`SC-6` (Personal Access Tokens):** An authenticated user can create, list, and revoke scoped PATs (`trailforge:read` or `trailforge:read trailforge:write`) via `/api/auth/tokens` and the Account Panel UI. The plaintext `tf_pat_...` token is shown once, stored only as a SHA-256 hash, authenticates `/mcp` and `/mcp/auth`, enforces scope boundaries (`read`-only PAT cannot call write tools), and stops working immediately upon revocation, user suspension (`is_active=false`), or logout (`token_version` bump).
- **`SC-7` (Self-Hosted OAuth 2.1 + PKCE + DCR):** Unauthenticated requests to `/mcp/auth` return `HTTP 401` with `WWW-Authenticate: Bearer resource_metadata="<origin>/.well-known/oauth-protected-resource/mcp/auth", scope="trailforge:read trailforge:write"`. Full OAuth flow (`/.well-known/oauth-protected-resource` → `/.well-known/oauth-authorization-server` → `POST /api/oauth/register` → `GET/POST /api/oauth/authorize` with PKCE `S256` → `POST /api/oauth/token` → `refresh_token` rotation → `POST /api/oauth/revoke`) passes end-to-end offline. Replaying an already-used refresh token revokes the entire token family.
- **`SC-8` (Strict Privacy & Write Safety):** Public tools never return any user's `email`, `user_id`, private `trips`, `user_pins`, `user_favorites`, or `planned_routes`. Personal tools return only rows owned by the authenticated user. Write tools (`save_planned_route`, `save_personal_pin`, `toggle_favorite_track`) require `trailforge:write` scope and declare MCP `annotations: {readOnlyHint: false, destructiveHint: false}`. Zero MCP tools can create, modify, or vote on public `condition_reports` or invoke admin endpoints.
- **`SC-9` (Web UI Deep Links):** Opening TrailForge web UI with `?route=<track_id_1>,<track_id_2>&rig=<vehicle_class>` (in addition to existing `?track=...&region=...&lat=...&lon=...&z=...&profile=1`) loads the Route Planner with those ordered tracks and active rig class selected.
- **`SC-10` (Edge Proxy, Rate Limits & Admin Visibility):** `/mcp` and `/mcp/auth` enforce a dedicated sliding-window rate limit (`TRAILFORGE_RATE_LIMIT_MCP_PER_MIN`), a `1 MiB` streaming body limit, `X-TrailForge-Origin-Secret` when configured, and record hourly traffic under `route_group="mcp"` visible in `GET /api/admin/stats` and the Admin Console. Cloudflare Pages proxy, `nginx`, Vite dev proxy, and Caddy configs forward `/mcp`, `/mcp/*`, and `/.well-known/oauth-*` without response buffering.
- **`SC-11` (Local `stdio` Bridge & Antigravity Plugin):** Running the local `stdio` entrypoint bridges MCP over stdin/stdout to a local or remote TrailForge instance (with optional `TRAILFORGE_API_TOKEN`), and a ready-to-use Antigravity plugin template is documented and structurally validated.
- **`SC-12` (ODbL & Attribution Compliance):** All new DB tables reside in Tier C with zero OSM geometry/tag columns, and every MCP tool text summary, `structuredContent.attributions`, PNG chart footer, and MCP App footer preserves required data attributions.

---

## 4. Protocol, Transport & Endpoint Topology

### 4.1 HTTP Surface Mounted in FastAPI

| Path | Methods | Auth Policy | Rate-Limit Bucket / Group | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| `/mcp` | `POST`, `GET`, `DELETE`, `OPTIONS` | Hybrid: Anonymous allowed; optional `Bearer` (PAT, OAuth access token, or session token) | `mcp` / `mcp` | Zero-friction Streamable HTTP MCP endpoint for public browsing + optional Bearer auth |
| `/mcp/auth` | `POST`, `GET`, `DELETE`, `OPTIONS` | **Required** `Bearer` (OAuth access token or PAT); returns `401` + `WWW-Authenticate` when anonymous | `mcp` / `mcp` | OAuth-triggering Streamable HTTP MCP endpoint for ChatGPT Web, Claude.ai, and Gemini Enterprise sign-in flows |
| `/.well-known/oauth-protected-resource` | `GET`, `OPTIONS` | Public | `auth` / `auth` | RFC 9728 metadata for `/mcp` |
| `/.well-known/oauth-protected-resource/mcp` | `GET`, `OPTIONS` | Public | `auth` / `auth` | RFC 9728 path-scoped metadata for `/mcp` |
| `/.well-known/oauth-protected-resource/mcp/auth` | `GET`, `OPTIONS` | Public | `auth` / `auth` | RFC 9728 path-scoped metadata for `/mcp/auth` |
| `/.well-known/oauth-authorization-server` | `GET`, `OPTIONS` | Public | `auth` / `auth` | RFC 8414 OAuth 2.1 Authorization Server metadata |
| `/api/oauth/register` | `POST` | Public (DCR rate-limited) | `oauth_dcr` / `auth` | RFC 7591 Dynamic Client Registration |
| `/api/oauth/authorize` | `GET`, `POST` | Browser session (`tf_session` cookie) or inline login form; CSRF-protected | `auth` / `auth` | OAuth 2.1 Authorization & User Consent page (`X-Frame-Options: DENY`) |
| `/api/oauth/token` | `POST` | Client auth (`none` + PKCE, or `client_secret_post`) | `auth` / `auth` | Authorization code exchange & refresh-token rotation |
| `/api/oauth/revoke` | `POST` | Token or client credentials | `auth` / `auth` | RFC 7009 token revocation |
| `/api/auth/tokens` | `GET`, `POST` | Authenticated user (session cookie or full session token — NOT a PAT/OAuth token) | `api` / `auth` | List & mint Personal Access Tokens (`tf_pat_...`) |
| `/api/auth/tokens/{token_id}` | `DELETE` | Authenticated user | `api` / `auth` | Revoke an owned PAT |
| `/api/auth/oauth-grants` | `GET`, `DELETE /{client_id}` | Authenticated user | `api` / `auth` | List & revoke connected OAuth AI apps in Account Panel |

### 4.2 Transport & CORS Rules
- **Stateless Streamable HTTP:** Configure the MCP ASGI handler in stateless mode (`stateless_http=True`, `json_response=True` or SSE framing per `Accept` header) so each HTTP request is self-contained, requires zero sticky sessions, and adds near-zero idle RAM.
- **Auth Mode Configuration (`TRAILFORGE_MCP_AUTH_MODE`):**
  - `hybrid` *(default)*: `/mcp` allows anonymous + optional Bearer; `/mcp/auth` challenges unauthenticated requests with `401` + `WWW-Authenticate`.
  - `oauth_required`: Both `/mcp` and `/mcp/auth` challenge unauthenticated requests with `401` + `WWW-Authenticate`.
  - `anonymous_only`: `/mcp` runs public read-only tools only; personal/write tools are hidden; `/mcp/auth` returns `404`.
- **MCP & Discovery CORS:** `/mcp`, `/mcp/auth`, and `/.well-known/oauth-*` must answer `OPTIONS` preflight and emit `Access-Control-Expose-Headers: Mcp-Session-Id, WWW-Authenticate, X-RateLimit-Limit, X-RateLimit-Remaining`. Cross-origin requests to `/mcp*` and `/.well-known/*` do not rely on cookies (credentials are Bearer headers only), so `Access-Control-Allow-Origin` reflects safe origins (or `*` without `Allow-Credentials` on Bearer/public discovery endpoints) without weakening cookie CORS on `/api/*`.
- **Proxy Header Forwarding:** Proxies (`pages-proxy-worker.js`, `web/nginx.conf`, `web/vite.config.ts`) must forward `Authorization`, `Accept`, `Content-Type`, `Mcp-Session-Id`, `MCP-Protocol-Version`, and `Last-Event-ID`, preserve `WWW-Authenticate` and `Mcp-Session-Id` response headers, and disable response buffering/compression on `/mcp*`.

---

## 5. Authentication, OAuth 2.1 & Data Model (Tier C)

### 5.1 Scopes & Auth Principal Context
Every MCP request resolves into a typed `McpAuthContext` (passed via `contextvars.ContextVar` into MCP tool handlers):

```python
@dataclass(frozen=True, slots=True)
class McpAuthContext:
    user: User | None                   # None when anonymous
    auth_method: Literal["anonymous", "session", "pat", "oauth"]
    scopes: frozenset[str]              # subset of {"trailforge:read", "trailforge:write"}
    token_id: uuid.UUID | None = None   # PAT or OAuth token row ID for audit
    client_id: str | None = None        # OAuth client_id when auth_method == "oauth"
```

- **Scopes:**
  - `trailforge:read` — read user's Vehicle Profiles, saved routes, favorites, personal pins, and private trip summaries.
  - `trailforge:write` — create/update user's saved routes, personal pins, and favorites (implies `trailforge:read`).
  - REST routes explicitly opt in to delegated reads or saved-item writes. Detailed trip data, exports of private data, deletions, telemetry uploads, condition report writes, account state, and administrative operations require a full session. Existing PATs and OAuth grants follow these limits without reissuing them.
  - Existing user session tokens (`create_access_token`) carry full session privileges (`trailforge:read`, `trailforge:write`) and remain the only credential allowed to manage PATs (`/api/auth/tokens`) or OAuth grants (`/api/auth/oauth-grants`) so a leaked AI token cannot mint persistent replacement tokens.
- **Token Prefixes & Hashing:**
  - Personal Access Token: `tf_pat_<43-char-urlsafe-secret>` (256-bit entropy via `secrets.token_urlsafe(32)`).
  - OAuth Access Token: `tf_oat_<43-char-urlsafe-secret>` (TTL default `3600 s`).
  - OAuth Refresh Token: `tf_ort_<43-char-urlsafe-secret>` (TTL default `30 days`).
  - OAuth Authorization Code: `tf_oac_<43-char-urlsafe-secret>` (TTL `60 s`, single-use).
  - Only `SHA-256(token)` hex digests are stored in PostgreSQL; lookup uses indexed digest equality and constant-time comparisons for secrets.

### 5.2 Database Schema — Migration `0007_ai_mcp_auth` (Tier C, Zero OSM Data)
Revises `0005_user_token_version`. Adds four Tier C tables:

1. **`personal_access_tokens`**
   - `id` (`UUID`, PK, `gen_random_uuid()`)
   - `user_id` (`UUID`, FK → `users.id` `ON DELETE CASCADE`, indexed)
   - `name` (`String(80)`, NOT NULL)
   - `token_prefix` (`String(16)`, NOT NULL — display snippet e.g. `tf_pat_ab12cd`)
   - `token_hash` (`String(64)`, NOT NULL, UNIQUE)
   - `scopes` (`ARRAY(Text)`, NOT NULL, default `['trailforge:read']`)
   - `expires_at` (`DateTime(timezone=True)`, nullable)
   - `last_used_at` (`DateTime(timezone=True)`, nullable)
   - `revoked_at` (`DateTime(timezone=True)`, nullable)
   - `created_at` (`DateTime(timezone=True)`, NOT NULL, `now()`)
   - Constraints: max 25 active tokens per user enforced in API; scopes validated against `{"trailforge:read", "trailforge:write"}`.

2. **`oauth_clients`**
   - `client_id` (`String(64)`, PK — `tf_cli_<random>`)
   - `client_name` (`String(120)`, NOT NULL)
   - `client_secret_hash` (`String(64)`, nullable — NULL for public PKCE clients)
   - `redirect_uris` (`JSONB`, NOT NULL — list of validated URI strings, 1..10 items)
   - `grant_types` (`JSONB`, NOT NULL — subset of `["authorization_code", "refresh_token"]`)
   - `token_endpoint_auth_method` (`String(32)`, NOT NULL — `CHECK IN ('none','client_secret_post')`)
   - `registered_by_ip_hash` (`String(64)`, nullable — HMAC-hashed client IP for abuse telemetry)
   - `created_at` (`DateTime(timezone=True)`, NOT NULL, `now()`)
   - `last_used_at` (`DateTime(timezone=True)`, nullable)

3. **`oauth_authorization_codes`**
   - `code_hash` (`String(64)`, PK)
   - `client_id` (`String(64)`, FK → `oauth_clients.client_id` `ON DELETE CASCADE`, NOT NULL)
   - `user_id` (`UUID`, FK → `users.id` `ON DELETE CASCADE`, NOT NULL)
   - `redirect_uri` (`Text`, NOT NULL)
   - `scope` (`Text`, NOT NULL)
   - `code_challenge` (`String(128)`, NOT NULL)
   - `code_challenge_method` (`String(16)`, NOT NULL, `CHECK = 'S256'`)
   - `resource` (`Text`, nullable — RFC 8707 resource indicator)
   - `expires_at` (`DateTime(timezone=True)`, NOT NULL)
   - `consumed_at` (`DateTime(timezone=True)`, nullable)
   - `created_at` (`DateTime(timezone=True)`, NOT NULL, `now()`)

4. **`oauth_tokens`**
   - `id` (`UUID`, PK, `gen_random_uuid()`)
   - `token_family_id` (`UUID`, NOT NULL, indexed — links rotated refresh generations)
   - `client_id` (`String(64)`, FK → `oauth_clients.client_id` `ON DELETE CASCADE`, NOT NULL)
   - `user_id` (`UUID`, FK → `users.id` `ON DELETE CASCADE`, NOT NULL, indexed)
   - `user_token_version` (`Integer`, NOT NULL — must match `users.token_version` on every check)
   - `access_token_hash` (`String(64)`, NOT NULL, UNIQUE)
   - `refresh_token_hash` (`String(64)`, nullable, UNIQUE)
   - `scope` (`Text`, NOT NULL)
   - `resource` (`Text`, nullable)
   - `access_expires_at` (`DateTime(timezone=True)`, NOT NULL)
   - `refresh_expires_at` (`DateTime(timezone=True)`, nullable)
   - `revoked_at` (`DateTime(timezone=True)`, nullable)
   - `created_at` (`DateTime(timezone=True)`, NOT NULL, `now()`)

### 5.3 Mandatory OAuth 2.1 Security Invariants
1. **PKCE `S256` Mandatory:** Reject authorization requests without `code_challenge` or with `code_challenge_method != "S256"`.
2. **Strict Redirect URI Validation (No Open Redirects):**
   - Registration (`POST /api/oauth/register`) accepts ONLY:
     - `https://` URLs with a valid host and no fragment (`#`), OR
     - Loopback `http://localhost:<port>/...` / `http://127.0.0.1:<port>/...` / `http://[::1]:<port>/...` for local desktop/CLI MCP clients.
     - Rejects `javascript:`, `data:`, non-loopback `http://`, wildcards, and credentials in URI.
   - Authorize & Token endpoints require **exact byte-for-byte match** against a registered `redirect_uri`.
3. **Consent Page Hardening (`/api/oauth/authorize`):**
   - Renders server-side HTML with the escaped `client_name`, requested scopes, and user email. The read scope lists rigs, saved routes, pins, favorites, and trip summaries. The write scope lists saving routes, pins, and favorites. Signed-out visitors see a same-origin email and password form before consent.
   - Enforces `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`, and an HMAC-signed short-lived CSRF nonce bound to the user session on `POST /api/oauth/authorize`.
4. **Refresh Token Rotation & Reuse Detection:**
   - Exchanging a `refresh_token` atomically revokes the old `oauth_tokens` row and mints a new row sharing `token_family_id`.
   - Presenting an already-revoked `refresh_token` immediately revokes **all** tokens sharing that `token_family_id` and returns `400 invalid_grant`.
5. **DCR Abuse Bounds:**
   - Rate-limited via `TRAILFORGE_OAUTH_DCR_MAX_PER_IP_PER_DAY` (default `20`), validates client metadata lengths, and periodically prunes unused clients older than 90 days (or enforces a configurable table cap of `5000` clients).
6. **Public URL Resolution:**
   - Issuer and metadata URLs resolve from `TRAILFORGE_PUBLIC_BASE_URL` when configured, falling back to `X-Forwarded-Proto` + `X-Forwarded-Host` (when `trust_proxy_headers=True`) or request `Host`—never leaking internal `*.up.railway.app` hostnames.

---

## 6. MCP Tools, Resources & Prompts Contract

### 6.1 Tool Naming, Visibility & Annotations Summary

All model-visible tools return a `CallToolResult` containing:
- `content`: `[TextContent(type="text", text=<concise markdown digest with attributions>), *optional_png_ImageContent]`
- `structuredContent`: versioned JSON object matching Section 7 (consumed by the SEP-1865 MCP App and structured clients).

#### A. Public Read & Planning Tools (Anonymous + Authenticated)

| Tool Name | Arguments (JSON Schema Summary) | UI Resource / Entrypoint | Annotations | PNG Fallback (`include_images=true`) |
| :--- | :--- | :--- | :--- | :--- |
| `search_trails` | `query?: str`, `state?: str`, `region?: str`, `bbox?: [min_lon,min_lat,max_lon,max_lat]`, `near_lat?: float`, `near_lon?: float`, `radius_km?: float (1..150, default 40)`, `grades?: list["Easy"\|"Medium"\|"Hard"\|"Extreme"]`, `open_only?: bool (default false)`, `vehicle_class?: CapabilityClass`, `poi_types?: list[str]`, `limit?: int (1..25, default 10)`, `include_images?: bool (default false)` | `ui://trailforge/explorer.html` (`mode="explore"`) | `readOnlyHint: true`, `openWorldHint: false` | Area overview map PNG (when `include_images=true`) |
| `inspect_track` | `track_id?: str`, `track_name?: str` *(at least one required; fuzzy-resolves unique name or returns disambiguation list)*, `vehicle_class?: CapabilityClass`, `snowline_m?: int (default 1000)`, `include_images?: bool (default true)` | `ui://trailforge/explorer.html` (`mode="track"`) | `readOnlyHint: true`, `openWorldHint: false` | Elevation Profile PNG + Track Course Map PNG |
| `plan_route` | `track_ids: list[str] (1..30)`, `name?: str`, `vehicle_class?: CapabilityClass`, `waypoints?: list[{name, category?, lon, lat}]`, `snowline_m?: int (default 1000)`, `include_images?: bool (default true)` | `ui://trailforge/explorer.html` (`mode="route"`) | `readOnlyHint: true`, `openWorldHint: false` | Route Overview Map PNG + Combined Elevation Profile PNG |
| `compare_tracks` | `track_ids: list[str] (2..4)`, `vehicle_class?: CapabilityClass`, `include_images?: bool (default true)` | `ui://trailforge/explorer.html` (`mode="compare"`) | `readOnlyHint: true`, `openWorldHint: false` | Overlaid Elevation & KPI Comparison PNG |
| `check_area_conditions` | `state?: str`, `region?: str`, `park_name?: str`, `bbox?: [min_lon,min_lat,max_lon,max_lat]`, `near_lat?: float`, `near_lon?: float`, `radius_km?: float (default 50)`, `include_images?: bool (default true)` | `ui://trailforge/explorer.html` (`mode="explore"`) | `readOnlyHint: true`, `openWorldHint: false` | Area Closures & Hazards Map PNG |
| `find_pois_near` | `track_id?: str`, `lat?: float`, `lon?: float`, `radius_km?: float (0.5..50, default 10)`, `bbox?: [...]`, `poi_types?: list[str]`, `limit?: int (1..30, default 15)` | *(optional `explorer.html`)* | `readOnlyHint: true`, `openWorldHint: false` | None (text + structured list) |
| `export_route_file` | `track_ids: list[str] (1..30)`, `name?: str`, `format?: "gpx" \| "geojson" (default "gpx")`, `waypoints?: list[...]` | None | `readOnlyHint: true`, `openWorldHint: false` | None (returns GPX/GeoJSON text if `<= 120 KB`, direct export URL, and GridForge 300 DPI CLI) |
| `list_regions_and_packs` | `state?: str` | None | `readOnlyHint: true`, `openWorldHint: false` | None |
| `open_trailforge_map` | `region?: str`, `track_id?: str`, `track_ids?: list[str]` *(all optional; `{}` defaults to configured default corridor/regions)* | `ui://trailforge/explorer.html` + Antigravity `com.google.antigravity/ui` `entrypoints: [{"type": "aux_pane", "title": "TrailForge Map"}]` | `readOnlyHint: true`, `openWorldHint: false` | None |

> **Rig Resolution Rule:** Whenever `vehicle_class` is omitted and the caller is authenticated (`trailforge:read`), tools automatically resolve the user's active default `VehicleProfile` (including `has_snorkel` and rig name) using the same logic as `tracks.py` / `planner.py`. An explicit `vehicle_class` argument always overrides the saved profile for "what-if" questions.

#### B. App-Only Interactive Tools (`_meta.ui.visibility = ["app"]`)
Hidden from the LLM; callable exclusively by `ui://trailforge/explorer.html` via `app.callServerTool()` so clicking tracks, exploring map areas, or re-orienting a route inside the iframe stays fast, works under strict CSP without `connectDomains`, and does not consume LLM turns:
1. `app_get_track_detail(track_id: str, vehicle_class?: str, snowline_m?: int)` → returns compact `TrackViewData` (`structuredContent`).
2. `app_plan_route_preview(track_ids: list[str], name?: str, vehicle_class?: str)` → returns compact `RouteViewData` (`structuredContent`).
3. `app_search_viewport(bbox: list[float], grades?: list[str], open_only?: bool, limit?: int)` → returns up to 40 lightweight tracks + active closures + POIs within the widget viewport.

#### C. Authenticated Personal Read Tools (Require `trailforge:read`)
1. `get_my_garage()` — returns the signed-in user's Vehicle Profiles (name, drivetrain, lift, clearance, tires, lockers, winch, snorkel, `capability_class`, `is_default`).
2. `list_my_saved_routes()` — returns the user's saved `PlannedRoute` entries with live closure/grade/wet-risk summary and deep links.
3. `list_my_trips_and_pins(include_trips?: bool=true, include_pins?: bool=true, include_favorites?: bool=true, limit?: int=20)` — returns metadata summaries of the user's own private trip logbook (never raw home-zone coordinates), personal pins, and favorite tracks.

#### D. Authenticated Private Write Tools (Require `trailforge:write`)
All declare `annotations: {"readOnlyHint": false, "destructiveHint": false, "idempotentHint": false}`:
1. `save_planned_route(name: str, track_ids: list[str], notes?: str, vehicle_class?: str, waypoints?: list[...], route_id?: str)` — creates a new owned `PlannedRoute` (or updates `route_id` if owned by caller) and returns the saved detail + deep link + interactive `mode="route"` UI.
2. `save_personal_pin(title: str, lat: float, lon: float, category?: "waypoint"|"camp"|"hazard"|"photo"|"water"|"recovery", notes?: str)` — saves an owned `UserPin`.
3. `toggle_favorite_track(track_id: str, action: "add" | "remove", notes?: str)` — adds or removes an owned `UserFavorite`.

#### E. MCP Resources
- `ui://trailforge/explorer.html` (`mimeType: "text/html;profile=mcp-app"`, `_meta: {"ui": {"prefersBorder": false, "csp": {"connectDomains": [...], "resourceDomains": [...]}}}`)
- `trailforge://grading-methodology` (`mimeType: "text/markdown"`) — explains Day-1 DEM/OSM baseline factors, safety floors, live telemetry blending, and 72 h rainfall wet-risk shifts.
- `trailforge://vehicle-classes` (`mimeType: "application/json"`) — canonical `AWD`, `Stock 4WD`, `Touring 4WD`, `Modified 4WD` classification criteria.
- `trailforge://regions` (`mimeType: "application/json"`) — the 15 Australian pack regions, bounding boxes, states, and official data attributions.

#### F. MCP Prompts
- `plan-weekend-trip(region_or_area: str, days?: int=2, vehicle_class?: str, difficulty?: str, preferences?: str)` — guides the assistant to check closures/rainfall first, search candidate tracks with camps/huts, call `plan_route`, and present the map, elevation graph, safety briefing, and GPX/GridForge handoff.
- `pre-trip-safety-check(track_or_route: str, vehicle_class?: str)` — guides the assistant to inspect active agency closures, OSM/community locked gates, 72 h rainfall wet-risk, perennial water crossings, and rig suitability.
- `find-tracks-for-rig(region_or_area: str, vehicle_class?: str, max_difficulty?: str)` — guides the assistant to check `get_my_garage` (if signed in) or `vehicle_class`, find open suitable tracks, and compare top candidates with `compare_tracks`.

---

## 7. Locked `structuredContent` Wire Schema (Backend ↔ MCP App Contract)

Every visual tool populates `structuredContent` with the versioned `TrailForgeViewPayload` envelope below so the Python backend and TypeScript MCP App widget can be built independently without schema drift:

```typescript
export interface TrailForgeViewPayload {
  schema_version: "1.0";
  view_mode: "explore" | "track" | "route" | "compare";
  title: string;
  subtitle?: string | null;
  bbox: [number, number, number, number] | null; // [min_lon, min_lat, max_lon, max_lat]
  public_base_url: string;                        // e.g. "https://trails.example.com"
  deep_link_url: string;                          // e.g. "https://trails.example.com/?track=..."
  snowline_m: number;                             // default 1000
  attributions: string[];                         // mandatory OSM/GA/state/Open-Meteo notices
  rig_context?: {
    capability_class: "AWD" | "Stock 4WD" | "Touring 4WD" | "Modified 4WD";
    vehicle_name?: string | null;
    has_snorkel?: boolean | null;
  } | null;
  tile_config?: {
    vector_pmtiles_url?: string | null;
    terrain_pmtiles_url?: string | null;
    basemap_raster_url?: string | null;
  } | null;

  // Populated according to view_mode (nullable/omitted when unused):
  explore?: {
    tracks: CompactTrackCard[];
    closures: CompactClosureItem[];
    reports: CompactReportItem[];
    pois: CompactPoiItem[];
    rainfall_summary?: { rain_24h_mm?: number; rain_72h_mm?: number; wet_risk_level: string } | null;
  } | null;

  track?: {
    summary: CompactTrackCard;
    segments: CompactSegmentGeom[];               // simplified GeoJSON LineStrings with grade/slope/dist_m
    elevation_profile: CompactElevationPoint[];   // <= 350 points: {dist_m, elev_m, slope_deg, grade, segment_id}
    crossings: Array<{ name: string; perennial: boolean; bridged: boolean; dist_m: number }>;
    baseline_breakdown?: Record<string, unknown> | null;
    live_summary?: {
      live_grade?: string | null;
      confidence_level: string;
      sample_count: number;
      last_driven_at?: string | null;
      avg_speed_kmh?: number | null;
      est_duration_min?: number | null;
      vehicle_class_split?: Record<string, number>;
      wet_risk_level: string;
      rain_72h_mm?: number | null;
    } | null;
    closures: CompactClosureItem[];
    gate_warnings: CompactReportItem[];
    condition_reports: CompactReportItem[];
    nearby_pois: CompactPoiItem[];
    parks: Array<{ name: string; park_type: string; state: string }>;
    export_gpx_url: string;
    export_geojson_url: string;
  } | null;

  route?: {
    name: string;
    saved_route_id?: string | null;
    total_length_m: number;
    est_duration_min: number;
    elev_gain_m: number;
    elev_loss_m: number;
    elev_min_m?: number | null;
    elev_max_m?: number | null;
    worst_grade: string | null;
    suitability?: { rating: string; summary: string; reasons: string[] } | null;
    safety_summary: {
      has_blocking_closure: boolean;
      closure_count: number;
      gate_warning_count: number;
      wet_risk_level: string;
      waterway_crossing_count: number;
      perennial_crossing_count: number;
      gap_count: number;
      max_gap_m: number;
    };
    legs: Array<{
      leg_index: number;
      track_id: string;
      name: string;
      reversed: boolean;
      length_m: number;
      grade: string | null;
      gap_from_prev_m: number;
      segments: CompactSegmentGeom[];
    }>;
    elevation_profile: CompactElevationPoint[];   // <= 400 points across all stitched legs
    closures: CompactClosureItem[];
    gate_warnings: CompactReportItem[];
    waypoints: Array<{ name: string; category: string; lon: number; lat: number }>;
    nearby_pois: CompactPoiItem[];
    gridforge_cli: string;
    export_gpx_url: string;
  } | null;

  compare?: {
    tracks: Array<{
      summary: CompactTrackCard;
      segments: CompactSegmentGeom[];
      elevation_profile: CompactElevationPoint[]; // <= 180 points per track
      color_hex: string;
    }>;
  } | null;
}

export interface CompactSegmentGeom {
  segment_id: string;
  grade: "Easy" | "Medium" | "Hard" | "Extreme" | string;
  slope_deg: number | null;
  length_m: number;
  is_steep: boolean;
  coords: Array<[number, number]>; // [lon, lat] simplified to ~10-15 m tolerance
}

export interface CompactElevationPoint {
  dist_m: number;
  elev_m: number;
  slope_deg: number;
  grade: string | null;
  segment_id?: string | null;
}
```

### 7.1 Payload Size & Simplification Budget
- **Hard ceiling:** Serialized `CallToolResult` (text + `structuredContent` + optional base64 PNGs) must stay **`< 450 KB`** (safely under the `1 MiB` MCP App trajectory storage cap).
- Simplify segment coordinates (`shapely.simplify(tol=0.00012)` ≈ 12 m, rounded to 5 decimal places) and downsample elevation profiles uniformly (preserving start, end, min, max, and steepest peaks) to stay within point budgets.
- If a tool call returns a very large multi-track route (`> 15` legs), automatically increase coordinate simplification tolerance (e.g., `0.00025°` ≈ 25 m) and omit secondary PNGs before ever exceeding the budget.

---

## 8. 3-Tier Visual Rendering Specification

### 8.1 Tier 1 — Interactive SEP-1865 MCP App (`ui://trailforge/explorer.html`)
- **Build Target:** Standalone Vite config (`web/vite.mcp.config.ts`) using `@modelcontextprotocol/ext-apps` and `vite-plugin-singlefile`, compiling `web/mcp-app.html` → single self-contained HTML output loaded by the backend (with live-reload override via `TRAILFORGE_MCP_APP_HTML`).
- **SDK Lifecycle:**
  - Instantiate `new App({ name: "TrailForge Explorer", version }, {})`.
  - Register `ontoolinput`, `ontoolresult`, `ontoolcancelled`, and `onhostcontextchanged` **before** calling `await app.connect()`.
  - Apply host light/dark theme via `applyDocumentTheme` and `applyHostStyleVariables` on connect and every theme change, with crisp dark-topo CSS fallbacks.
  - Gate interactive host actions on `app.getHostCapabilities()`:
    - `"Open in TrailForge"` button → `app.openLink({ url: payload.deep_link_url })` (hidden if `openLinks` unsupported).
    - `"Send Selection to Chat"` / `"Add Track to Context"` button → `app.updateModelContext(...)` (hidden if `updateModelContext` unsupported, e.g., global view).
    - Clicking any track card/line in `explore` mode → `app.callServerTool({ name: "app_get_track_detail", arguments: { track_id } })` (gated on `serverTools`).
- **Hybrid Map Renderer (Never Blank):**
  1. **Primary Mode (MapLibre GL JS 5):** Activates when WebGL is available and either `tile_config.vector_pmtiles_url` or configured basemap tiles load cleanly within declared `_meta.ui.csp` (`connectDomains` / `resourceDomains`). Renders TrailForge's exact grade palette (`#22C55E` Easy, `#38BDF8` Medium, `#F59E0B` Hard, `#EF4444` Extreme), closure overlays, POIs, start/finish badges (`S`/`F`), and live hover cursor marker.
  2. **Automatic Zero-Network Fallback (Interactive SVG/Canvas Vector Map):** Activates immediately (without user error) when WebGL is unavailable, offline, or external tiles fail/are CSP-blocked. Reuses TrailForge's North-up projected track course renderer (`buildTrackCourseSvg` logic) enhanced with wheel/button zoom, drag pan, grade-colored segments, closure/gate badges, POI pins, scale bar, north arrow, and hover crosshair!
- **Synchronized Elevation Profile Graph:**
  - Reuses TrailForge's `buildElevationProfileSvg` visual grammar: grade-colored profile line & shaded area, dashed snow-line band, waterway crossing pins, steep (`≥14°`) zone highlights, and pointer/touch crosshair scrubbing.
  - Hovering/scrubbing the elevation profile moves the highlighted position dot on the map in lockstep—and hovering the map highlights the matching distance/elevation/slope on the graph.
- **Data Cards & Actions:**
  - Explainable grade breakdown bars, rig suitability pill, live telemetry freshness/rainfall chips, active closures & gate warnings with official source links, and copyable GridForge 300 DPI CLI + GPX link.
  - Always displays TrailForge's required attribution footer.

### 8.2 Tier 2 — Lightweight Server-Rendered PNG Fallbacks (`ImageContent`)
- Implemented in pure Python (`Pillow` + `math`/`numpy`, zero headless Chromium, zero Matplotlib/Cairo dependencies) with 2× supersampled anti-aliasing (`LANCZOS` downscale) and dark-topo styling matching TrailForge's palette:
  1. **Elevation Profile PNG (`960×420`):** Grade-colored terrain curve + translucent gradient fill, horizontal gridlines + altitude/distance axis labels, dashed snow-line marker, unbridged/bridged creek crossing ticks, steep-section callouts, header KPI strip (length, grade, gain/loss, max slope, rig suitability), and attribution footer.
  2. **Track / Route / Area Map Card PNG (`960×540`):** Equirectangular/Web-Mercator aspect-corrected projection with subtle graticule grid, grade-colored segments, closure/gate callouts, start (`S`) & finish (`F`) markers, nearby POI icons/labels (with simple greedy collision avoidance), scale bar (`km`), north arrow (`N`), legend, and attribution footer.
  3. **Track Comparison PNG (`960×460`):** Overlaid elevation curves (normalized or actual distance) for 2–4 tracks in distinct high-contrast colors + KPI summary strips.
- **Budget:** PNGs encoded with `optimize=True` (or quantized palette PNG when smaller), targeting `25–75 KB` per image.

### 8.3 Tier 3 — Grounded Text Digest & Web UI Deep Links
- Every tool's `TextContent` follows a concise, token-efficient Markdown structure: key verdict (grade, suitability, blocking closures/wet risk), tabular metrics, explicit warnings, direct TrailForge deep link, and attribution line.
- Extend `web/src/urlState.ts` and `web/src/main.ts` so `?route=<track_id_1>,<track_id_2>&rig=<vehicle_class>` opens the Route Planner drawer preloaded with those legs and rig class (complementing existing `?track=...&region=...&lat=...&lon=...&z=...&profile=1`).

---

## 9. Configuration, Edge Security, Proxy & Packaging

### 9.1 Environment Settings (`Settings` in `config.py`)

| Env Var | Default | Description |
| :--- | :--- | :--- |
| `TRAILFORGE_ENABLE_MCP` | `true` | Master toggle for `/mcp`, `/mcp/auth`, and OAuth discovery endpoints |
| `TRAILFORGE_MCP_AUTH_MODE` | `"hybrid"` | `"hybrid"` \| `"oauth_required"` \| `"anonymous_only"` |
| `TRAILFORGE_PUBLIC_BASE_URL` | `""` | Canonical public HTTPS origin (e.g. `https://trails.example.com`) for deep links & OAuth metadata |
| `TRAILFORGE_RATE_LIMIT_MCP_PER_MIN` | `60` | Per-IP (or per-authenticated-user) sliding-window request cap on `/mcp*` |
| `TRAILFORGE_MCP_APP_HTML` | `""` | Optional local file override for `ui://trailforge/explorer.html` hot reload |
| `TRAILFORGE_MCP_CSP_TILE_DOMAINS` | `""` | Comma-separated bare origins allowed in `_meta.ui.csp` `connectDomains`/`resourceDomains` (auto-includes origin of `TILES_PUBLIC_BASE_URL` when set) |
| `TRAILFORGE_OAUTH_ACCESS_TOKEN_TTL_S` | `3600` | OAuth 2.1 access token lifetime (seconds) |
| `TRAILFORGE_OAUTH_REFRESH_TOKEN_TTL_S` | `2592000` | OAuth 2.1 refresh token lifetime (30 days) |
| `TRAILFORGE_OAUTH_DCR_MAX_PER_IP_PER_DAY` | `20` | Daily cap on Dynamic Client Registration per IP |

### 9.2 Middleware & Edge Hardening
- **`ROUTE_GROUPS`:** Add `"mcp"` to `ROUTE_GROUPS` in `middleware.py` and classify `/mcp` and `/mcp/*` → `"mcp"`, `/.well-known/oauth-*` and `/api/oauth/*` → `"auth"`.
- **Body Limit:** Enforce `1 MiB` streaming body limit on `/mcp*` and `/api/oauth/*`.
- **Origin Secret:** Enforce `X-TrailForge-Origin-Secret` on `/mcp*`, `/.well-known/oauth-*`, and `/api/oauth/*` whenever `TRAILFORGE_ORIGIN_SHARED_SECRET` is set.
- **Cloudflare Pages Proxy:** Update `deploy/cloudflare/pages-proxy-worker.js` to proxy `/api/*`, `/mcp`, `/mcp/*`, and `/.well-known/oauth-*` (streaming without buffering, forwarding MCP/OAuth headers, preserving `WWW-Authenticate` and `Mcp-Session-Id`), and document Cloudflare Pages middleware mounting (copying the proxy into `<pages-project>/functions/_middleware.js` or thin wrappers).
- **Cloudflare R2 CORS (`deploy/cloudflare/r2-cors.json`):** Document optional `"*"` read-only (`GET`/`HEAD`) `AllowedOrigins` profile for public `PMTiles` buckets when enabling full MapLibre vector tile rendering inside opaque-origin MCP App iframes (while noting the zero-network SVG/Canvas fallback works even with strict single-origin R2 CORS).
- **Docker `nginx`, Vite dev proxy & Caddy:** Proxy `/mcp`, `/mcp/*`, and `/.well-known/oauth-*` to `api:8000` with `proxy_buffering off` (and exclude `/mcp*` from Caddy `encode` compression).

### 9.3 Local `stdio` Bridge & Antigravity Plugin Packaging
- **Python CLI Entrypoint:** `trailforge-mcp` (`python -m trailforge.mcp.stdio_proxy`) exposing a thin `stdio` MCP proxy forwarding to `TRAILFORGE_MCP_URL` (default `http://localhost:5173/mcp`) with optional `TRAILFORGE_API_TOKEN` Bearer header (or direct in-process PostGIS mode `--direct` for local dev).
- **Antigravity Plugin Template (`deploy/ai/antigravity-plugin/`):** Ready-to-copy `plugin.json` + `mcp_config.json` + SVG icon for instant installation into `~/.gemini/config/plugins/trailforge/`, plus copy-paste connection guides for ChatGPT, Claude, Antigravity, Gemini CLI/Enterprise, and Cursor in `docs/AI_INTEGRATION.md`.

---

## 10. Always / Ask First / Never Boundaries

### Always
- Keep all unit and integration CI tests **100% offline** using synthetic fixtures (`tests/fixtures/`).
- Preserve all upstream OSM (`ODbL`), GA/DEA/CAPAD/state (`CC BY 4.0` / `CC BY 3.0 AU`), and Open-Meteo (`CC BY 4.0`) attributions in every text, PNG, and MCP App output.
- Enforce strict `user_id` ownership checks on every personal read/write tool and scope checks (`trailforge:read` / `trailforge:write`) on PAT and OAuth tokens.
- Keep `CallToolResult` payloads strictly `< 450 KB` and idle server overhead minimal (zero Redis, zero headless Chromium).
- Keep all tracked files free of local paths, personal usernames, and real credentials.

### Ask First
- Before adding any new external runtime service (e.g., Redis, sidecar renderer, external OAuth provider).
- Before exposing any AI write tool that mutates shared community data (`condition_reports`, votes, telemetry ingestion) or admin operations.
- Before relaxing production security defaults (CORS on cookie routes, `X-TrailForge-Origin-Secret`, `AUTH_COOKIE_SECURE`).

### Never
- Never leak another user's private `trips`, `user_pins`, `user_favorites`, `planned_routes`, `email`, or raw home-zone coordinates through any MCP tool.
- Never store plaintext PATs, OAuth access/refresh tokens, authorization codes, or client secrets in the database.
- Never allow wildcard, `javascript:`, `data:`, or unvalidated non-loopback `http://` OAuth redirect URIs.
- Never store OSM geometries or raw OSM tag blobs in Tier C tables.

---

## 11. Out of Scope (v1.2)
- AI-submitted public community condition reports, votes, or GPX telemetry uploads.
- Server-side headless browser (Playwright/Chromium) map screenshot rendering.
- External third-party identity providers (Auth0/Clerk/Google OAuth login).
- Client ID Metadata Documents (CIMD) beyond standard RFC 7591 DCR (can be added additively later).
- Conversational voice / CarPlay / Android Auto assistant integration.

---

## 12. Verification & Test Seams (100% Offline CI)

1. **Auth & OAuth 2.1 Suite (`tests/test_ai_auth_oauth.py`):**
   - PAT lifecycle (mint once, hash storage, Bearer auth on `/mcp` & `/api/*`, read-only scope rejection on writes, revocation, suspension, logout `token_version` invalidation).
   - Full OAuth 2.1 + PKCE `S256` + DCR flow: discovery JSON validity (`/.well-known/*`), redirect URI injection/open-redirect rejection, bad PKCE verifier rejection, single-use auth code replay rejection, refresh rotation + token-family reuse revocation, RFC 7009 revoke, consent CSRF protection.
2. **MCP Server, Tools & Privacy Suite (`tests/test_mcp_server.py`):**
   - Streamable HTTP JSON-RPC over FastAPI `TestClient` against seeded synthetic Alps fixtures: `initialize`, `tools/list` (verifying `_meta.ui.resourceUri`, `annotations`, `visibility: ["app"]` separation), `resources/read` (`ui://trailforge/explorer.html` MIME + CSP origins), `prompts/get`.
   - Every public tool (`search_trails`, `inspect_track`, `plan_route`, `compare_tracks`, `check_area_conditions`, `find_pois_near`, `export_route_file`, `list_regions_and_packs`, `open_trailforge_map`), app-only tools, personal read tools, and private write tools.
   - Cross-user isolation test (User B cannot read/update User A's rigs, routes, pins, or trips), payload size budget (`< 450 KB`), and attribution presence checks.
3. **PNG Visual Renderer Suite (`tests/test_mcp_renderer.py`):**
   - Verifies Elevation Profile, Track/Route/Area Map Card, and Comparison PNGs decode cleanly via Pillow, meet dimensions and `< 120 KB` byte ceiling, contain non-uniform rendered pixels, and handle degenerate/empty profiles gracefully.
4. **Frontend Widget & Deep-Link Suite (`web/` Vitest + `tsc --noEmit` + Vite build):**
   - Unit tests for MCP App payload normalization, zero-network SVG/Canvas fallback rendering across all 4 `view_mode`s, capability gating (`openLinks`, `updateModelContext`, `serverTools`), theme CSS variable sync, and `?route=...&rig=...` URL deep-link parsing/serialization.
   - `npm --prefix web run build` AND `npm --prefix web run build:mcp` produce clean outputs with `explorer.html` well under `4 MiB`.
5. **Edge Security, Middleware & Proxy Suite (`tests/test_mcp_edge_security.py`):**
   - `/mcp` rate limiting (`429` + `Retry-After`), `1 MiB` body cap (`413`), `X-TrailForge-Origin-Secret` enforcement (`403`), `route_group="mcp"` metrics aggregation in `GET /api/admin/stats`, `TRAILFORGE_ENABLE_MCP=false` kill switch (`404`), and Cloudflare proxy JS tests for `/mcp` & `/.well-known/oauth-*` routing.

---

## 13. Open Decisions (Non-Blocking Defaults Applied)

- **`OD-1` (Default PNG inclusion on `search_trails`):** Default `include_images=false` on `search_trails` (to keep exploratory search turns ultra-fast and light) and `include_images=true` on `inspect_track`, `plan_route`, `compare_tracks`, and `check_area_conditions`. *Can be flipped cleanly without breaking callers.*
- **`OD-2` (Public R2 CORS policy):** Keep `deploy/cloudflare/r2-cors.json` origin-restricted by default and provide an explicit `r2-cors-mcp.json` / documented option for wildcard read-only `PMTiles` CORS for instances wanting live MapLibre tiles inside opaque-origin MCP App iframes.

---

## 14. Swarm Workstream & File-Ownership Map

To execute cleanly in parallel via `/swarm` with minimal merge friction, split work into **4 implementation streams + 1 integration/verification pass**:

| Workstream | Owns (Disjoint Paths) | Implements |
| :--- | :--- | :--- |
| **W1 — PAT & OAuth 2.1 Auth** | `migrations/versions/0007_ai_mcp_auth.py`, `src/trailforge/auth/tokens.py`, `src/trailforge/auth/oauth.py`, `src/trailforge/api/oauth.py`, `tests/test_ai_auth_oauth.py` | Tier C auth tables, PAT mint/verify/revoke, OAuth 2.1 + PKCE + DCR + consent HTML + `/.well-known/*`, Account Panel API hooks |
| **W2 — MCP Server, Tools & PNG Renderer** | `src/trailforge/mcp/*` (`server.py`, `tools.py`, `schemas.py`, `renderer.py`, `resources.py`, `prompts.py`, `context.py`, `stdio_proxy.py`), `tests/test_mcp_server.py`, `tests/test_mcp_renderer.py` | Stateless Streamable HTTP MCP server, all 18 tools (public, app-only, personal, private write), resources, prompts, Pillow PNG visual renderer, `stdio` CLI proxy |
| **W3 — MCP App Widget, Deep Links & Account UI** | `web/mcp-app.html`, `web/vite.mcp.config.ts`, `web/src/mcp/*`, `web/src/urlState.ts`, `web/src/urlState.test.ts`, `web/src/components/accountPanel.ts` (PAT/OAuth tokens section) | Single-file SEP-1865 interactive map + linked elevation app (`@modelcontextprotocol/ext-apps`, MapLibre + SVG/Canvas fallback), `?route=&rig=` URL deep links, PAT/Connected AI Apps UI |
| **W4 — Edge Proxy, Packaging & Docs** | `deploy/cloudflare/pages-proxy-worker.js`, `deploy/cloudflare/r2-cors.json`, `deploy/Caddyfile.example`, `deploy/ai/antigravity-plugin/*`, `web/nginx.conf`, `web/vite.config.ts`, `docs/AI_INTEGRATION.md`, `tests/test_mcp_edge_security.py` | Edge proxy routing for `/mcp*` & `/.well-known/oauth-*`, non-buffered streaming, Antigravity plugin template, client setup guides |
| **Coordinator / Integrator** | Shared seam files ONLY: `pyproject.toml`, `web/package.json`, `src/trailforge/config.py`, `src/trailforge/db/models.py`, `src/trailforge/auth/dependencies.py`, `src/trailforge/api/main.py`, `src/trailforge/api/middleware.py`, `src/trailforge/api/admin.py`, `web/src/main.ts`, `Dockerfile` | Wires W1–W4 together cleanly, builds `explorer.html` into backend assets, runs full offline Python + Vitest + live smoke verification |
