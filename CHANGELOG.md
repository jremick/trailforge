# Changelog

All notable changes to **TrailForge** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-11-27

Major feature, performance, and security hardening release (publishing `:v1.2.0`, `:v1.2`, and `:latest` multi-arch `linux/amd64` + `linux/arm64` GHCR images) introducing **native AI & Model Context Protocol (MCP) integration**, **self-hosted OAuth 2.1 + PKCE + Personal Access Tokens**, an **interactive SEP-1865 MCP App explorer**, **MapLibre GL JS v6**, **comprehensive PostGIS/API/PWA performance optimizations**, and **deep OWASP/STRIDE & container hardening** across `449` passing offline CI tests (`286` Python/PostGIS + `163` Vitest).

### Added

#### AI & Model Context Protocol (MCP) Server + Interactive MCP App
- **Stateless Streamable HTTP MCP Server (`src/trailforge/mcp/server.py`, `src/trailforge/mcp/tools.py`, `src/trailforge/mcp/resources.py`, `src/trailforge/mcp/prompts.py`):** Exposes **18 MCP tools** across track search, explainable DEM/telemetry grading, live Australian agency closures, CAPAD park alerts, rig suitability, multi-track route planning, GPX/GeoJSON export, offline pack inspection, and authenticated personal tools (garage vehicles, saved routes, condition reports), alongside `trailforge://` resources, curated 4WD planning prompts, and server-rendered Pillow PNG trail/elevation visuals (`src/trailforge/mcp/renderer.py`) with RGBA-on-RGB canvas compositing.
- **Interactive SEP-1865 MCP App (`ui://trailforge/explorer.html`, `src/trailforge/mcp/static/explorer.html`, `web/src/mcp/mcpApp.ts`):** Single-file interactive map & elevation explorer bundle (MapLibre GL JS v6 with Blob-URL bundled worker + zero-network SVG/Canvas fallback) for ChatGPT, Claude, Antigravity `aux_pane`, and MCP Apps-compatible hosts, plus ready-to-install Antigravity plugin templates (`deploy/ai/antigravity-plugin/plugin.json`, `deploy/ai/antigravity-plugin/mcp_config.json`), the `trailforge-mcp` local `stdio` bridge (`src/trailforge/mcp/stdio_proxy.py`), and setup guides (`docs/AI_INTEGRATION.md`, `docs/SPEC_AI_MCP.md`).
- **Self-Hosted Personal Access Tokens & OAuth 2.1 + PKCE + DCR (`src/trailforge/auth/tokens.py`, `src/trailforge/auth/oauth.py`, `src/trailforge/api/oauth.py`):** Tier C reversible Alembic migration `migrations/versions/0007_ai_mcp_auth.py` adding `personal_access_tokens` (`tf_pat_...` with scoped `trailforge:read`/`trailforge:write`), `oauth_clients` (RFC 7591 Dynamic Client Registration with daily per-IP caps), `oauth_authorization_codes` (mandatory PKCE `S256`), and `oauth_tokens` (rotating refresh tokens with reuse revocation), plus RFC 9728 Protected Resource Metadata (`/.well-known/oauth-protected-resource`), RFC 8414 Authorization Server Metadata (`/.well-known/oauth-authorization-server`), Account Panel PAT & OAuth grant management, and strict scope enforcement.

#### Database Performance Indexes & Secure Admin CLI
- **Performance & Search Indexes (`migrations/versions/0008_perf_and_security_indexes.py`):** Enables `pg_trgm` and adds trigram GIN search indexes on `tracks.name` and `pois.name`, plus targeted partial and composite indexes for active closures, canonical segment grades, live telemetry observations, and hotspot lookups (**35 base tables, 21 GIST indexes**; strict 3-tier OSM ODbL separation preserved).
- **Non-Leaking Admin Password CLI (`src/trailforge/auth/cli.py`):** Added `--password-stdin` and interactive `getpass` prompting to `create-admin` (avoiding plaintext password exposure in shell history or `ps` output).

### Changed & Performance
- **Database & API Hot-Path Optimization:** Eliminated N+1 queries across track listings, route planning, closure detail joins, and admin dashboards via batched SQLAlchemy queries and spatial joins; capped/streamed GPX & Overpass/XML ingestion parsers cleanly; vectorized 50 Hz IMU-to-GPS alignment (`src/trailforge/matching/imu.py`, `O(M + N log M)`); offloaded synchronous DB flushes, scrypt hashing, Pillow photo sanitization, and PNG rendering off the FastAPI asyncio event loop.
- **MapLibre GL JS v6 & Lean Web PWA (`web/package.json`, `web/src/map.ts`, `web/src/main.ts`, `web/src/mcp/mapLibreRenderer.ts`):** Upgraded `maplibre-gl` to `^6.19.0` with explicit Vite ES-module worker bundling (`maplibre-gl-worker.mjs?url` + `setWorkerUrl`) and Blob-URL worker bundling in the standalone MCP App; lazy-loaded heavy drawers (`RoutePlannerDrawer`, `PacksPanel`, `AccountPanel`, `AdminConsolePanel`) cutting initial JS bundle size by ~84 KB; upgraded the 50 Hz mobile IMU recorder (`web/src/offline/recorderEngine.ts`) to a typed-array ring buffer; switched offline pack SHA-256 verification (`web/src/offline/packStore.ts`) to streaming/zero-copy hashing; added `requestAnimationFrame` hover-popup throttling in `web/src/map.ts`; and bumped `web/public/sw.js` to v3 with bounded LRU cache pruning and `/mcp` passthrough.
- **Unprivileged Containers & Reverse-Proxy Hardening (`web/Dockerfile`, `web/nginx.conf`, `Dockerfile`, `deploy/Caddyfile.example`, `deploy/traefik-compose.override.yml`, `deploy/cloudflare/pages-proxy-worker.js`, `deploy/cloudflare/r2-cors-mcp.json`):** Migrated `trailforge-web` to `nginxinc/nginx-unprivileged:1.27-alpine` (`uid=101`, port `8080`), added SHA-256 checksum verification for `tippecanoe` and `go-pmtiles` in `Dockerfile`, pinned `uv sync --frozen`, added `/mcp`, `/mcp/*`, and `/.well-known/oauth-*` unbuffered proxy rules across Nginx, Caddy (with `17 MB` upload cap), Traefik, and Cloudflare Pages, and added `pip-audit` + `npm audit` vulnerability gates to `.github/workflows/ci.yml`.

### Security
- **Fail-Closed Proxy & Origin Boundaries (`src/trailforge/config.py`, `src/trailforge/api/middleware.py`, `src/trailforge/mcp/context.py`):** Defaulted `TRAILFORGE_TRUST_PROXY_HEADERS` to `false` in `Settings` (explicitly enabled in Compose/Railway templates), enforced `TRAILFORGE_TRUSTED_PROXY_CIDRS` peer validation with right-to-left `X-Forwarded-For` parsing, gated `CF-Connecting-IP` behind `TRAILFORGE_TRUST_CLOUDFLARE_HEADERS` (auto-enabled when `TRAILFORGE_ORIGIN_SHARED_SECRET` is set), partitioned the in-process LRU rate limiter across `api`/`auth`/`mcp`/`oauth_dcr` buckets so public floods cannot evict auth/DCR lockouts, rejected wildcard `CORS_ORIGINS=*`, and required `TRAILFORGE_PUBLIC_BASE_URL` in production when `TRAILFORGE_ENABLE_MCP=true`.
- **Auth, OAuth 2.1 & MCP Hardening:** Blocked PAT/OAuth Bearer tokens from invoking cookie logout (`POST /api/auth/logout`), removed the `invite_only` `TRAILFORGE_ADMIN_EMAIL` bypass so registration never skips invite validation or burns scrypt CPU on rejected requests, bound OAuth login CSRF nonces to `HttpOnly` cookies with same-origin enforcement and strict JSON parameter typing (`HTTP 400`), enabled strict MCP JSON Schema input validation (`validate_input=True`) with runtime bounds checks, downscaled & re-encoded uploaded report photos to `<= 2048 px` with full EXIF/PNG `tEXt` stripping (`src/trailforge/reports/photos.py`), and verified **0 Python CVEs** (`pip-audit`) and **0 npm high/critical CVEs** (`npm audit`).

### Upgrade Notes (`v1.1.x` -> `v1.2.0`)
- Run `alembic upgrade head` (automatic on Compose/Railway API boot) to apply `0007_ai_mcp_auth` and `0008_perf_and_security_indexes` cleanly on top of `0006_ui_preferences_and_settings`.
- In production with `TRAILFORGE_ENABLE_MCP=true`, set `TRAILFORGE_PUBLIC_BASE_URL=https://<your-domain>` (required by startup validation so OAuth 2.1 metadata and deep links never expose internal hostnames).
- Set `TRAILFORGE_TRUST_PROXY_HEADERS=true` (and `TRAILFORGE_TRUSTED_PROXY_CIDRS` / `TRAILFORGE_TRUST_CLOUDFLARE_HEADERS` as appropriate) whenever deploying behind Nginx, Caddy, Traefik, or Cloudflare; direct unproxied installs stay fail-closed (`false`).
- In `invite_only` mode, bootstrap your first admin via `python -m trailforge.auth.cli create-admin --email admin@example.com --password-stdin` (public `/api/auth/register` no longer bypasses invite codes for `TRAILFORGE_ADMIN_EMAIL`).
- Note that `trailforge-web` now listens on unprivileged port `8080` inside the container (`docker-compose.yml` and `docker-compose.ghcr.yml` already map `${WEB_PORT:-5173}:8080`).

### Verification Metrics (`v1.2.0`)
- **Offline CI Tests:** `449` automated offline tests passing (`286` Python/PostGIS unit, integration, MCP, OAuth 2.1 & security regression tests + `163` Vitest web tests across 20 files; `0` failures, `0` high/critical CVEs across `pip-audit` & `npm audit`).
- **Schema & Runtime:** Alembic `0008_perf_and_security_indexes` linear upgrade clean (**35 base tables, 21 GIST indexes**); unprivileged `trailforge-backend` (`uid=10001`) and `trailforge-web` (`uid=101`) containers verified end-to-end with live MapLibre GL JS v6 worker loading and HTTP `206` PMTiles range slicing.

---

## [1.1.6] - 2026-11-27

Map UX, incident inspection, and admin/user sidebar customization release (publishing `:v1.1.6`, `:v1.1`, and `:latest` multi-arch `linux/amd64` + `linux/arm64` GHCR images) on top of the deep-link URL state, topo mini-map, and linked elevation views shipped since `v1.1.5`.

### Added
- **Click-to-Inspect Emergency Incidents & Closures:** Added `GET /api/closures/{closure_id}` (`src/trailforge/api/closures.py`, `src/trailforge/feeds/queries.py`) returning HTML-cleaned agency descriptions, parsed incident metadata (headline, location, category, size, resources, CAPAD park, effective dates), geometry bbox, affected segment count, and up to 25 affected 4WD tracks. Added the right-hand `ClosureInspectorDrawer` (`web/src/components/closureInspector.ts`), selected-incident map outline highlight (`tf-closures-selected-outline`), overlapping-alert switcher, hover `"Click for details"` prompt, and `"Full Incident Details →"` jump links from the Track Inspector.
- **Admin Default Map Layers & Auto-Saved User Preferences:** Added Tier C Alembic migration `0006_ui_preferences_and_settings` (`users.ui_preferences` JSONB + singleton `instance_ui_settings` table, **31 base tables, 21 GIST indexes**), public `GET /api/ui-config`, authenticated `GET/PUT/DELETE /api/user/preferences`, and admin `GET/PUT/DELETE /api/admin/ui-settings`. Added drag-and-drop and `↑`/`↓` layer reordering + auto-saved toggle preferences (with **Reset defaults**) in the sidebar, plus a **Settings** tab in the Admin Console to configure instance-wide default layer order, default ON/OFF state, and public telemetry visibility.
- **Public Stack Telemetry Toggle:** Added `TRAILFORGE_SHOW_STACK_TELEMETRY_PUBLIC` (default `false`, configurable via env var or Admin Console) so **Stack & Offline Telemetry** stays hidden from signed-out visitors by default while remaining always visible to signed-in members and admins.

### Changed
- **Default Map Layer Ordering:** Updated the left-column default order to Contours & Hillshade → Parks & Waterways → Outdoor POIs → 4WD Tracks (Graded) → Steep Climbs (≥14°) → Obstacle Hotspots → Closures & Gates → Hazard & Gate Reports → Unmapped Track Spurs → Alpine Snow-Line Band (without changing MapLibre cartographic z-order).
- **Cleaner Sidebar Controls:** Removed `01`–`04` section numbering from sidebar headers, and hid the **Snow-Line Threshold** slider and **Track Difficulty Grade** legend whenever their corresponding map layers are switched OFF.

---

## [1.1.0] - 2026-11-26

Public-hosting and security-hardening release (publishing `:v1.1.0`, `:v1.1`, and `:latest` multi-arch `linux/amd64` + `linux/arm64` GHCR images) delivering **invite-only public access control & Admin Console**, **zero-Redis edge rate limiting & upload quotas**, **$0-egress Cloudflare R2 offload**, a **~$5–$10/mo Railway + Cloudflare deployment kit**, and **full remediation of all 13 pre-launch OWASP/STRIDE security audit findings** backed by `301+` automated offline CI tests.

### Added

#### Public Instance Access Control, Friend Invites & Admin API
- Tier C reversible Alembic migrations (`migrations/versions/0004_public_access_control.py` and `0005_user_token_version.py`, **30 base tables and 21 GIST indexes** with zero OSM ODbL impact) adding `role` (`'admin'`/`'member'`), `is_active`, `disabled_at`, `invited_by_code`, and `token_version` to `users`, plus new `invite_codes` and `usage_metrics_hourly` tables (`src/trailforge/db/models.py`, `tests/test_schema_postgis.py`).
- Public registration policies (`TRAILFORGE_REGISTRATION_MODE=open|invite_only|closed`), `TRAILFORGE_ADMIN_EMAIL` first-admin auto-bootstrap (allowed even in `invite_only` mode when no admin exists), `GET /api/auth/config` (`registration_mode`, `demo_banner`, `allow_anonymous_browsing`), immediate account suspension & session revocation across Bearer/cookie sessions and login, and `require_admin` dependency (`src/trailforge/auth/dependencies.py`, `src/trailforge/auth/invites.py`, `src/trailforge/api/auth.py`).
- Admin-only gating (`Depends(require_admin)`) on expensive compute endpoints (`POST /api/packs/{region_id}/build`, `POST /api/closures/sync`, `POST /api/grading/recompute`) while keeping all read/browse `GET` routes open to anonymous visitors, plus `PACKS_PUBLIC_BASE_URL` Cloudflare R2 pack URL emission in `src/trailforge/api/packs.py`.
- Admin management & telemetry API (`src/trailforge/api/admin.py`: `GET /api/admin/stats`, paginated `GET/PATCH /api/admin/users`, paginated `GET/POST/DELETE /api/admin/invites` with `FOR UPDATE` last-admin demotion/suspension `400` guard) and CLI (`python -m trailforge.auth.cli create-admin|promote|create-invite|list-invites`), verified by `tests/test_access_control.py`.

#### Zero-Redis Rate Limiting, Upload Disk Quotas & Cloudflare R2 Sync
- Zero-Redis in-process `UsageAndRateLimitMiddleware` (`src/trailforge/api/middleware.py`, wired in `src/trailforge/api/main.py`) extracting validated client IPs from `CF-Connecting-IP` / `X-Forwarded-For` (`TRAILFORGE_TRUST_PROXY_HEADERS=true`), enforcing configurable read (`TRAILFORGE_RATE_LIMIT_READ_PER_MIN`) and auth (`TRAILFORGE_RATE_LIMIT_AUTH_PER_MIN`) `O(1)` LRU sliding-window limits with `X-RateLimit-Limit` / `X-RateLimit-Remaining` and HTTP `429` + `Retry-After` (leaving `/api/health` unthrottled), and tracking rolling per-hour route-group telemetry flushed to `usage_metrics_hourly`.
- Storage & upload protections: global `/data/uploads` disk quota (`TRAILFORGE_UPLOADS_MAX_TOTAL_MB`, returning HTTP `507 Insufficient Storage` before writing photos in `src/trailforge/reports/photos.py`) and per-user daily trip & photo upload quotas (`TRAILFORGE_RATE_LIMIT_UPLOAD_PER_DAY` across `POST /api/trips/upload`, `POST /api/trips/mobile-sync`, and `POST /api/reports`).
- Zero-egress Cloudflare R2 offload (`TILES_PUBLIC_BASE_URL` in `src/trailforge/api/tiles.py`) and pure-Python AWS Signature Version 4 + `httpx` sync CLI (`python -m trailforge.packs.r2_sync`, zero `boto3` dependency, ETag/size skip checks, `--dry-run`), verified by `tests/test_edge_guard_r2.py`.

#### Railway + Cloudflare Cloud Deploy Kit & Compose Parity
- Railway Config-as-Code (`railway.toml`: Dockerfile build, `alembic upgrade head && exec uvicorn trailforge.api.main:app --host 0.0.0.0 --port ${PORT:-8000}`, `/api/health` healthcheck, `ON_FAILURE` restart policy) and complete ~$5–$10/mo hard-capped Railway + Cloudflare production playbook (`deploy/railway/README.md`, `deploy/railway/env.railway.example`) covering PostGIS (`imresamu/postgis:16-3.4`), `api`, `worker`, hard monthly billing limits, shared origin secret lock-down, R2 sync, and admin/invite bootstrap.
- Cloudflare Pages + R2 + WAF kit (`deploy/cloudflare/r2-cors.json` for HTTP `206` PMTiles CORS, `deploy/cloudflare/pages-proxy-worker.js` unbuffered `/api/*` Pages Function forwarding `CF-Connecting-IP`/`Range`/`Cookie`/`Authorization` + injecting `X-TrailForge-Origin-Secret` with `307 Location` rewriting, multi-`Set-Cookie` preservation, security headers, and safe 5xx error sanitization, and `deploy/cloudflare/waf-rules.md` covering Free-tier rate limits, Bot Fight Mode, Managed Challenge, origin lock-down, R2 cache rules, and cookie-free Cloudflare Web Analytics).
- Full public-hardening and R2 offload environment variable parity across `.env.example`, `docker-compose.yml`, and `docker-compose.ghcr.yml` (safe `open` local dev defaults with `invite_only` documented for public hosts), verified by `tests/test_cloud_deploy.py`.

#### Public Web UI, Absolute R2 PMTiles URLs & Admin Console
- Configurable `VITE_API_BASE_URL` and absolute `http://`/`https://` Cloudflare R2 `PMTiles` & offline pack URL support across `web/src/style.ts`, `web/src/map.ts`, `web/src/offline/packStore.ts`, `web/src/components/packsPanel.ts`, and `web/public/sw.js` (keeping relative `/api/tiles/...` paths 100% compatible).
- Startup `GET /api/auth/config` hydration, slim dismissible `TRAILFORGE_DEMO_BANNER` strip, `invite_only` (`TF-XXXX-XXXX` invite code input) and `closed` registration UX, friendly `429`/`403`/`507` error handling, and admin-only visibility for pack build/rebuild controls (`web/src/components/topbar.ts`, `accountPanel.ts`, `packsPanel.ts`).
- Slide-over **Admin Console** drawer (`web/src/components/adminPanel.ts`, `web/src/adminHelpers.ts`) exposing 24h/7d traffic & error/`429` stats, route breakdown, storage quota usage bar, one-click Closure Sync & Live Grade Recompute triggers, friend invite creation/copy/revocation, and user suspend/re-enable/role controls (`web/src/adminHelpers.test.ts`, `web/src/style.test.ts`, `web/src/security.test.ts` — `118/118` Vitest tests passing).

### Security (Pre-Launch OWASP/STRIDE Audit Remediation — 13 Findings Closed)
- **H-1 — Rate-Limiter Hardening & Origin Lock-Down (`src/trailforge/config.py`, `src/trailforge/api/middleware.py`, `deploy/cloudflare/pages-proxy-worker.js`):** Removed header-spoofable `testclient` rate-limit bypass (exempt only when raw ASGI peer is `testclient` with no `CF-Connecting-IP`/`X-Forwarded-For` headers), validated/canonicalized proxy IPs via `ipaddress.ip_address()`, normalized trailing slashes before matching `AUTH_RATE_LIMIT_PATHS`, replaced `O(N log N)` pruning with `O(1)` `OrderedDict` LRU eviction, and added constant-time `X-TrailForge-Origin-Secret` enforcement (`TRAILFORGE_ORIGIN_SHARED_SECRET` / `ORIGIN_SHARED_SECRET`) rejecting direct unauthenticated origin calls (`403`, exempting `/api/health` and `/healthz`).
- **H-2 — Fail-Closed Production Secret Guard (`src/trailforge/auth/security.py`, `src/trailforge/api/main.py`):** Added `validate_production_secrets(settings)` invoked at `create_app()` startup, raising `RuntimeError` immediately in production mode (`AUTH_COOKIE_SECURE=true`, `RAILWAY_ENVIRONMENT`, or `registration_mode != "open"`) if `AUTH_SECRET_KEY` or `TRAILFORGE_PRIVACY_SECRET` equals its dev default or has `< 32` characters.
- **H-3 — Streaming ASGI Body-Size Caps & Telemetry Array Limits (`src/trailforge/api/middleware.py`, `src/trailforge/api/trips.py`, `src/trailforge/matching/parser.py`):** Added pure-ASGI `BodySizeLimitMiddleware` checking both `Content-Length` and streamed `receive()` chunk byte counts (`16 MiB` `/api/trips/*`, `6 MiB` `/api/reports*`, `1 MiB` other `/api/*` -> HTTP `413 Payload Too Large` before FastAPI parses JSON/multipart), plus `50,000` GPS point and `100,000` IMU sample caps in Pydantic models and GPX/JSON parsers (`422`/`ValueError`).
- **M-1 — Report Photo EXIF Stripping & Per-User Quota (`pyproject.toml`, `src/trailforge/reports/photos.py`, `src/trailforge/api/reports.py`):** Promoted `pillow>=11.0,<14.0` to core dependencies, decoded and re-encoded uploaded report images via Pillow + `ImageOps.exif_transpose` (stripping all EXIF GPS/device/XMP/IPTC/tEXt metadata, rejecting corrupt/prefix-only fake images and `> 4096×4096` images with `422`), and enforced per-user daily upload quotas via `check_upload_quota`.
- **M-2 — Cloudflare Pages Proxy Redirect Origin Rewrite (`deploy/cloudflare/pages-proxy-worker.js`):** Rewrote `3xx`/`307` `Location` headers pointing at `RAILWAY_API_ORIGIN` (including protocol-relative redirects) back to the public client origin so internal `*.up.railway.app` hostnames are never exposed.
- **M-3 — Global Security Headers & MapLibre-Safe CSP (`src/trailforge/api/middleware.py`, `deploy/cloudflare/pages-proxy-worker.js`, `web/nginx.conf`, `web/public/_headers`):** Emitted `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy: camera=(), microphone=(), geolocation=(self)`, conditional `Strict-Transport-Security`, and a MapLibre/PMTiles/Service-Worker-compatible `Content-Security-Policy` across FastAPI, Pages proxy, Nginx (`server_tokens off`), and Cloudflare Pages `_headers`.
- **M-4 — Cookie-Only Same-Origin Web Auth & Server-Side `token_version` Revocation (`migrations/versions/0005_user_token_version.py`, `src/trailforge/auth/`, `src/trailforge/api/{auth,admin}.py`, `web/src/api.ts`):** Embedded `users.token_version` (`"tv"`) in HMAC tokens, incremented `token_version` on `POST /api/auth/logout` and admin role/`is_active` updates (immediately invalidating old tokens with `401`), and stopped persisting Bearer tokens in `window.localStorage` on same-origin web (relying strictly on `HttpOnly`, `SameSite=Lax` cookies while keeping Bearer only for cross-origin/native Capacitor).
- **M-5 — Cookie CSRF Guard (`src/trailforge/api/middleware.py`, `src/trailforge/api/main.py`):** Added pure-ASGI `CookieCSRFMiddleware` rejecting cross-site and sibling-subdomain (`Sec-Fetch-Site: cross-site`/`same-site` or untrusted `Origin`) mutating requests carrying `tf_session` without `Authorization: Bearer` (`403 Cross-site request blocked`).
- **M-6 — Invite-Before-Email Enumeration & Scrypt Timing-Oracle Fix (`src/trailforge/api/auth.py`):** Validated `invite_code` before email uniqueness in `invite_only` mode (without consuming invite uses on `409`) and added constant-work dummy `verify_password()` execution on missing/invalid email login.
- **L-1..L-4 — Spatial, Feed, Container & Docs Hardening:** Rejected `NaN`/`Inf` and out-of-range `bbox` values (`422` in `src/trailforge/api/tracks.py`); rejected `<!DOCTYPE`/`<!ENTITY` XML payloads, capped feed HTTP reads at `10 MiB`, and validated `^https?://` `source_url` in `src/trailforge/feeds/`; defaulted `Dockerfile` `ARG INSTALL_DEV=false` (`INSTALL_DEV=true` in dev Compose) with non-root `USER trailforge` (`10001:10001`) owning `/data` & `/app`; disabled `/docs`, `/redoc`, `/openapi.json` by default in production mode (`TRAILFORGE_ENABLE_DOCS`); added `FOR UPDATE` locking on last-admin updates, pagination on `/api/admin/{users,invites}`, `SAVEPOINT` + `IntegrityError` handling on concurrent report votes, and strict `/^\/api\/reports\/photos\/[A-Za-z0-9._-]+$/` photo URL filtering in `web/src/components/inspector.ts`.

### Verification Metrics (`v1.1.0`)
- **Offline CI Tests:** `301+` automated offline tests passing (`183` isolated Docker PostGIS tests + `118` Vitest web tests across 15 files + `156` host unit/deploy/security checks; `0` failures, `0` privacy hits), with `make test-integration` running in an isolated `docker compose run --rm --no-deps -T` container.
- **Schema & Container:** Alembic `0005_user_token_version` roundtrip & `alembic check` clean (**30 base tables, 21 GIST indexes**); backend container runs as non-root `trailforge` user (`10001:10001`).

---

## [1.0.1] - 2026-11-26

Patch release fixing 4 real-world Pan-Australia upstream agency endpoint quirks discovered during live `v1.0.0` verification so all 8 Australian states & territories (`VIC`, `NSW`, `ACT`, `TAS`, `QLD`, `WA`, `SA`, `NT`) return live HTTP `200` data cleanly out of the box.

### Fixed
- **NTG Road Centrelines (`src/trailforge/ingestion/connectors/au_states.py`):** Replaced nonexistent `OBJECTID` in `outFields` with the layer's actual `esriFieldTypeOID` field (`UFI`) on `https://gis-d.nt.gov.au/arcgis/rest/services/ROAD_CENTRELINES/FeatureServer/0/query` (eliminating ArcGIS `400 Unable to complete operation` errors) and added `UFI` fallback ID extraction so West MacDonnell and NT corridors return real NTG road tracks alongside GA Topo250K & NTLIS WFS POIs.
- **QLD Live Alerts (`qld_des_alerts` in `src/trailforge/feeds/base.py`, `src/trailforge/feeds/adapters_au.py`):** Switched default endpoint from the deprecated `https://parks.desi.qld.gov.au/park-alerts/rss.xml` URL (HTTP `404`) to Queensland Fire Department's live public GeoJSON feed (`https://www.fire.qld.gov.au/data/alerts/bushfireAlert.json`) with full QFD schema normalization (`UniqueID`, `WarningTitle`, `WarningLevel`, `Header`/`WarningText`, `EventType`, `WarningArea`, Polygon/Point geometry, and ISO timestamps) while retaining 100% backward compatibility with RSS/GeoJSON park alert fixtures.
- **WA Emergency Live Alerts (`wa_emergency` in `src/trailforge/feeds/base.py`, `src/trailforge/feeds/adapters_au.py`):** Switched default endpoint from the legacy SPA HTML URL (`https://www.emergency.wa.gov.au/data/message_DFES.json`) to Emergency WA's public v1 JSON API (`https://api.emergency.wa.gov.au/v1/warnings`) with `geo-source` `FeatureCollection` geometry extraction (preferring warning `Polygon`s over icon `Point`s), dict `location` fallback, `warning-type` severity mapping, non-fire `hazard_warning` classification, and direct `https://www.emergency.wa.gov.au/alerts/{id}` links.
- **NT Live Alerts (`nt_securent` in `src/trailforge/feeds/base.py`, `src/trailforge/feeds/adapters_au.py`):** Switched default endpoint from Cloudflare-gated `https://securent.nt.gov.au/alerts/feed` (HTTP `403`) to Northern Territory DIPL's official live public **Road Report NT** API (`https://roadreport.nt.gov.au/api/Obstruction/GetAll`), parsing live NT road closures, impassable tracks, flooding, and high-clearance 4WD restrictions into `LineString`/`Point` geometries while filtering out commercial heavy-vehicle axle-limit advisories and preserving compatibility with SecureNT & NTG `Hosted/Fire_Incidents` GeoJSON payloads.

### Testing
- Added offline unit tests in `tests/test_au_closure_feeds.py` and `tests/test_au_states_connectors.py` covering the exact live QFD GeoJSON, Emergency WA v1, Road Report NT JSON, and NTG `UFI` ArcGIS query parameter contracts.
- Expanded `@pytest.mark.live` smoke suites (`test_live_pan_au_closure_feeds_smoke` and `test_live_pan_au_connectors_smoke`) to verify all 6 Pan-AU states (`ACT`, `TAS`, `QLD`, `WA`, `SA`, `NT`) live alongside `VIC` & `NSW`.

---

## [1.0.0] - 2026-11-26

Inaugural stable GA release of **TrailForge**, graduating `v1.0.0-alpha.1` and `v1.0.0-beta.1` to production stability (publishing `:v1.0.0`, `:v1.0`, and `:latest` multi-arch `linux/amd64` + `linux/arm64` GHCR images) and delivering full **Pan-Australia 8-state support**, **11 official closure/emergency feeds**, an optional **Valhalla `/trace_attributes` sidecar**, **high-rate 50 Hz mobile IMU processing**, and a **Capacitor 7 mobile shell with live 4WD Clinometer** alongside the Interactive Multi-Track Route Planner, GridForge 300 DPI Strip Atlas handoff, zero-build GHCR + HTTPS deploy kit, and all 9 core vertical slices.

### Highlights Included in `v1.0.0` (Graduated from `alpha.1` & `beta.1`)
- **Core v1 Platform (Slices 01–09):** Strict 3-tier OSM ODbL-separated PostGIS schema, deterministic 50–100 m canonical track segmentation (`100%` ID preservation across re-imports), Day-1 DEM/OSM + recency-weighted vehicle-normalized live grading with Open-Meteo 72 h rainfall and obstacle hotspots, self-hosted accounts & AU 4WD rig suitability (`AWD`–`Modified 4WD`), mandatory `>= 250 m` privacy-trimmed telemetry & in-PostGIS HMM matcher, community hazard/gate reports with one-click OSM access fixes, MapLibre GL JS 5 PWA with HTTP `206` range-sliced `PMTiles` & alpine snow-line slider, and in-cab breadcrumb recorder with one-tap Backtrack.
- **Streaming Regional Bake, Route Planner & Production Deploy Kit:** Streaming `< 300 MB` RAM OSM PBF & live AU tiled bake CLI (`bake_cli`), 10k+ segment PostGIS GIST benchmark (`11,179` segments in `13.65 MiB` heap, `0.18–0.63 ms` scans), Interactive Multi-Track Route Planner (2-state DP leg orientation, combined 4-grade elevation SVG, saved `planned_routes`, GPX 1.1 / GeoJSON export, and 1-click GridForge 300 DPI Strip Atlas CLI handoff), zero-build `docker-compose.ghcr.yml`, turnkey Tailscale Serve / Caddy v2 / Traefik v3 HTTPS deploy kit (`deploy/`), and isolated `trailforge_test` PostGIS CI testing.

### Added Since `v1.0.0-beta.1`

#### Pan-Australia 8-State Connectors & 15-Region Pack Catalog
- Tier A `source_tracks_au_state` ORM model and reversible Alembic migration (`migrations/versions/0003_pan_au_source_tracks.py`, bringing the schema to 28 base tables and 21 GIST indexes) widening state/source/feed CHECK constraints cleanly across all 8 Australian states & territories while preserving strict 3-tier OSM ODbL separation and existing `source_tracks_vic` / `source_tracks_nsw` tables.
- All 6 remaining Australian state & territory open-data trail/POI connectors (`src/trailforge/ingestion/connectors/au_states.py`): `ACTTrailsConnector` (`ACTmapi/EPSDD`, `CC BY 4.0`), `TASTrailsConnector` (`the LIST/NRE Tas`, `CC BY 3.0 AU`), `QLDTrailsConnector` (`QSpatial/DESI/DNRM`, `CC BY 4.0`), `WATrailsConnector` (`DBCA/Landgate SLIP`, `CC BY 4.0`, strictly excluding any `CC BY-NC` records), `SATrailsConnector` (`DEW/DHUD/Location SA` + GA Topo250K, `CC BY 4.0`), and `NTTrailsConnector` (`NTLIS/DEPWS` + GA Topo250K, `CC BY 4.0`).
- `AutoAUStateTrailsRouter` automatically detecting all intersecting Australian states for any bounding box, dispatching cross-border connectors, merging results into pan-AU conflation/segmentation/live-bake pipelines, and returning dynamic agency attributions strictly for contributing states.
- Expanded offline pack catalog (`src/trailforge/packs/catalog.py`, `src/trailforge/api/packs.py`) to **15 Pan-Australia regions** (`7` iconic 4WD regions: `australian-alps`, `blue-mountains`, `tasmania-wilderness`, `far-north-qld`, `flinders-ranges`, `red-centre`, `perth-hills`; plus `8` statewide packs: `vic`, `nsw`, `act`, `tas`, `qld`, `wa`, `sa`, `nt`) exposing `category`, `states`, and dynamic per-state attributions (`tests/test_au_states_connectors.py`).

#### Pan-Australia Official Emergency & Park Closure Feeds
- 6 new official state/territory closure & emergency adapters (`src/trailforge/feeds/adapters_au.py`) alongside the 5 existing VIC/NSW/OSM feeds (11 adapters total): `ACTESAAlertsAdapter` (`act_esa`), `TasAlertAdapter` (`tas_alert`), `QLDParksAlertsAdapter` (`qld_des_alerts`), `EmergencyWAAdapter` (`wa_emergency`), `SAAlertAdapter` (`sa_cfs_parks`), and `NTFireParksAdapter` (`nt_securent`) with CAPAD park joins, official source URL preservation, and graceful outage `is_stale=True` retention.
- Lean opt-in state-scoped polling via `TRAILFORGE_ENABLED_STATES` (default `"VIC,NSW"`, `"VIC,NSW,ACT"`, or `"ALL"`), CLI `--states` option (`trailforge.feeds.cli`), and `?state=` single/comma-separated filtering + `available_states` (all 8 AU states) on `/api/closures` and `/api/closures/status` (`tests/test_au_closure_feeds.py`).

#### Pluggable Valhalla `TrackMatcher` Sidecar & High-Rate 50 Hz Mobile IMU
- Pluggable `ValhallaTrackMatcher` sidecar plugin and pure-Python Polyline6 encoder/decoder (`src/trailforge/matching/valhalla.py`, `src/trailforge/matching/factory.py`) implementing `TrackMatcher` via `POST {valhalla_url}/trace_attributes` (`use_tracks=1.0`, `map_snap`), projecting Valhalla-matched subtraces onto TrailForge's canonical 50–100 m PostGIS `track_segments` (preserving strict ODbL separation), extracting off-network spurs cleanly, and gracefully falling back to `PostGISHMMMatcher` (`postgis_hmm_fallback`) on HTTP error/timeout/connect failures.
- High-rate mobile IMU signal processor (`src/trailforge/matching/imu.py`) performing gravity baseline removal (`||a|| = sqrt(ax^2+ay^2+az^2)` in g units with sparse low-rate self-cancellation guard), computing `roughness_rms`, `roughness_p95`, debounced `shock_event_count` (`> 0.8g` dynamic / `> 1.8g` total), and `max_pitch_deg` / `max_roll_deg`, time-aligning IMU roughness onto trimmed GPS breadcrumbs and segments, and strictly discarding all IMU samples inside the mandatory `>= 250 m` start/end privacy buffer.
- Authenticated `POST /api/trips/mobile-sync` endpoint and optional `imu_samples` support on JSON `POST /api/trips/upload` (`src/trailforge/api/trips.py`), plus opt-in `deploy/valhalla-compose.override.yml` and `deploy/valhalla.md` keeping the default Compose stack lean (`tests/test_valhalla_imu.py`).

#### Capacitor 7 Mobile Shell, Live 4WD Clinometer & Pan-Australia Web UI
- Capacitor 7 mobile container configuration (`web/capacitor.config.ts`), native iOS/Android build guide & permission templates (`mobile/README.md`, `mobile/ios/Info.plist.snippet.xml`, `mobile/android/AndroidManifest.snippet.xml`), and Apple CarPlay (`CPMapTemplate`) / Android Auto (`NavigationTemplate`) architecture guide (`mobile/carplay-android-auto.md`).
- Zero-crash Capacitor/PWA runtime bridge (`web/src/native/bridge.ts`, `web/src/native/bridge.test.ts`) unifying native background geolocation, high-rate IMU capture, KeepAwake, and browser PWA fallbacks.
- Upgraded In-Cab Recorder with a live **4WD Clinometer** (`Pitch° / Roll°` with amber `>= 15°` and red `>= 25°` alerts), runtime mode badge, shock counter, simulated High Country clinometer/shock bursts, `POST /api/trips/mobile-sync` sync with offline IndexedDB queue fallback, plus Pan-Australia 15-region presets, `AU · 8 STATES` topbar pill, and **All / Iconic 4WD Regions / Statewide** pack category filters.

### Verification Metrics (`v1.0.0`)
- **Offline CI Tests:** `246+` automated offline tests passing (`148` isolated Docker PostGIS Python tests + `98` Vitest web tests across 13 files + host deploy-manifest parity checks; `0` failures, `0` worker races).
- **Schema & Lean Resource Footprint:** Alembic `0003_pan_au_source_tracks` roundtrip & `alembic check` clean (**28 base tables, 21 GIST indexes**); default Compose stack idle RAM **`~350 MiB`** (`< 400 MiB` budget) with optional Valhalla sidecar cleanly isolated in `deploy/valhalla-compose.override.yml`.

---

## [1.0.0-beta.1] - 2026-11-26

First public beta release of **TrailForge**, building on `v1.0.0-alpha.1` with a real-data streaming OSM PBF & Australian open-data regional bake pipeline, zero-build multi-arch GHCR & HTTPS deployment kit, isolated PostGIS test database, and an interactive multi-track Route Planner with GridForge 300 DPI Strip Atlas handoff.

### Added

#### Pillar 1 — Real-Data Regional Bake Pipeline & 10k-Segment Scale Benchmark
- Streaming `< 300 MB` RAM OSM PBF extractor (`src/trailforge/ingestion/pbf.py`) chaining `osmium extract --strategy=complete_ways` -> `osmium tags-filter` -> `osmium export -f geojsonseq` line-by-line into vehicular 4WD tracks, waterways, and outdoor POIs with shared-vertex gate propagation and ODbL provenance.
- Tiled regional & statewide open-data bake orchestrator and CLI (`src/trailforge/ingestion/live_bake.py`, `src/trailforge/ingestion/bake_cli.py`, plus `make bake-alps` and `make bake-blue-mountains`) supporting resumable tile manifests (`0.25°` default tiles), `GDAL_STREAMING_ENV`, live OSM PBF/Overpass + VIC DataVic WFS + NSW Spatial ArcGIS + GA Hydrology + CAPAD 2025 + GA 5 m LiDAR / DEA SRTM 1s DEM ingestion, closures, 72 h rainfall, PMTiles, and offline packs, plus `--fixtures` & `--dry-run` CI modes.
- 10,000+ segment synthetic scale benchmark and PostGIS `EXPLAIN (ANALYZE, FORMAT JSON)` GIST index auditor (`src/trailforge/ingestion/benchmark.py`, `tests/test_scale_bake.py`) plus a `@pytest.mark.live` real Australian corridor bake test.

#### Pillar 2 — Multi-Arch GHCR Release, Production HTTPS Deploy Kit & Isolated Test DB
- Multi-arch (`linux/amd64`, `linux/arm64`) GitHub Container Registry release workflow (`.github/workflows/release-images.yml`) publishing `trailforge-backend` (`INSTALL_DEV=false`) and `trailforge-web` with `GPL-3.0-or-later` OCI annotations, scoped GHA build caches, and a semver pre-release guard so tags like `v1.0.0-beta.1` never overwrite `:latest` or `:vMAJOR.MINOR`.
- Zero-build production/homelab Compose file (`docker-compose.ghcr.yml`) pulling prebuilt GHCR images + `imresamu/postgis:16-3.4`, binding `127.0.0.1:${WEB_PORT:-5173}:80` safely by default with full secrets/volumes/memory-cap parity (`db` 512m, `api` 512m, `worker` 512m, `web` 128m).
- Production HTTPS deployment kit (`deploy/README.md`, `deploy/Caddyfile.example`, `deploy/traefik-compose.override.yml`, `deploy/tailscale-serve.md`) preserving HTTP `Range` / `206 Partial Content` for PMTiles, enforcing `15 MB` GPX/photo upload caps, documenting why HTTPS is mandatory for in-cab PWA Service Workers / Geolocation / Screen Wake Lock / `AUTH_COOKIE_SECURE=true`, and covering PostGIS + `/data/uploads` + `/data/packs` backups and low-downtime upgrades.
- Isolated `trailforge_test` PostGIS integration test database in `Makefile` (`make test-integration`) and `tests/conftest.py` (auto-creating `trailforge_test` + PostGIS + Alembic migrations while syncing `DATABASE_URL` & `TEST_DATABASE_URL`) so tests never race `worker-1` or mutate seeded dev corridor data, backed by offline parity tests (`tests/test_deploy_config.py`).

#### Pillar 3 — Interactive Multi-Track Route Planner & GridForge 300 DPI Strip Atlas Handoff
- Tier C ODbL-separated `planned_routes` ORM model and Alembic migration (`migrations/versions/0002_planned_routes.py`, 27 base tables total; zero OSM geometries/tags — links strictly by canonical `track_ids` JSONB).
- Route planning engine & REST API (`src/trailforge/routes/planner.py`, `src/trailforge/api/routes.py`, `tests/test_route_planner.py`): `POST /api/routes/plan` with 2-state DP automatic leg orientation, `> 100 m` leg gap detection, combined distance/duration/elevation/slope/grade/rig-suitability/closures/gates/wet-risk/crossings/POIs summary, `POST /api/routes/plan/export?format=gpx|geojson` (GDAL-verified GPX 1.1 & RFC 7946 GeoJSON), authenticated saved-routes CRUD (`/api/routes`) with strict owner isolation, and pre-formatted `gridforge_cli` strip-atlas command.
- Interactive web **Route Planner** drawer, top-bar `Route` pill, `+ Route` Track Inspector button, stitched MapLibre route overlay (`#14b8a6` line + numbered `S` / `2..N` / `F` / `W` pins), combined 4-grade SVG elevation profile, offline pack fallback planner, one-click `Copy CLI` GridForge card, and drawer `overflow: clip` / `visibility` layout hardening (`web/src/components/routePlanner.ts`, `web/src/routeHelpers.ts`, `web/src/routeHelpers.test.ts`).

### Verification Metrics (`v1.0.0-beta.1`)
- **Offline CI Tests:** `207+` offline automated tests passing (`121` isolated Docker PostGIS Python tests + `86` Vitest web tests across 12 files + `4` host deploy-manifest parity tests; zero `worker-1` races and live dev corridor intact).
- **10k+ Segment Scale & GIST Benchmark:** `11,179` canonical segments (`150` tracks), **`13.65 MiB`** peak Python heap (`< 300 MB` budget), **`0.18–0.63 ms`** PostGIS GIST index scans (`0` Seq Scans on `track_segments`).
- **Live Real-Data AU Verification (`@pytest.mark.live`):** `4/4` passing across all 8 AU upstream agencies + real corridor bakes (Blue Mountains Narrow Neck & Australian Alps Tom Groggin: `592` real segments, `2.3 MiB` vector + `300 KiB` terrain PMTiles).

---

## [1.0.0-alpha.1] - 2026-11-26

First public alpha release of **TrailForge** — an open-source (`GPL-3.0-or-later`), self-hostable 4WD & off-road mapping, live track-grading, and trail-intelligence platform launching first for **Victoria & New South Wales (including ACT)**.

All 9 vertical slices across Checkpoints A, B, and C are implemented and verified end-to-end on the Australian Alps VIC/NSW corridor (`Davies Plain Track`, `Bucky Ridge Fire Trail`, `Pinch River Fire Trail`, `Tom Groggin Access Rd`, `Murray River` ford, and `Kosciuszko` / `Alpine National Park` tenure).

### Added

#### Slice 01 — Bootstrap Spatial Stack & Offline Test Harness
- Multi-arch (`linux/arm64` + `linux/amd64`) self-hosted Docker Compose stack (`PostGIS 16-3.4`, FastAPI `api`, background `worker`, Nginx + Vite/MapLibre GL JS `web`) with zero Redis/Kafka and zero PyQGIS (`~269 MiB` measured idle RSS across all 4 healthy containers).
- Versioned Alembic PostGIS schema migration (`0001_initial_spatial_schema`) enforcing strict 3-tier **OSM ODbL separation** (source tables -> canonical segments -> independent crowdsourced telemetry, live grades, closures, and condition reports).
- Deterministic synthetic Australian Alps corridor fixture harness (`tests/fixtures/`) with an autouse socket network guard ensuring 100% offline CI execution.
- Production-ready `.env.example` and `docker-compose.yml` passthrough for `AUTH_SECRET_KEY`, `TRAILFORGE_PRIVACY_SECRET`, `AUTH_COOKIE_SECURE`, and `TRAILFORGE_ENABLE_LIVE_FEEDS`.

#### Slice 02 — Canonical Track Network & Baseline Grading Pipeline
- Live & offline AU geospatial connectors for OpenStreetMap (`osm`), **DataVic / DEECA Vicmap WFS 2.0** (`tr_road`, `recweb_tracks`, `recweb_hut`, `recweb_site`, `foi_point`, `geomark_point`), **NSW Spatial Services** (`NSW_Transport_Theme` layers 5 & 9, `NSW_POI`, `SurveyMarkGDA2020`), **GA National Surface Hydrology**, **DCCEEW CAPAD 2025**, and **GA 5 m LiDAR / DEA SRTM 1s** elevation data.
- Geometry conflation and deterministic 50–100 m track segmentation preserving stable canonical `segment_id` values across re-imports (`100%` ID preservation across `v2` jittered re-ingestion: `358` exact + `86` spatial matches).
- DEM slope draping, waterway crossing intersection (with bridge exclusion), CAPAD park tenure attribution, and explainable Day-1 baseline grading (`Easy`, `Medium`, `Hard`, `Extreme`) exposing surface/tracktype, max/sustained slope degrees, crossings, and `4wd_only` tags.

#### Slice 03 — Interactive Vector Map & Track Inspector *(Checkpoint A)*
- Streaming Tippecanoe vector `PMTiles` and `rio-rgbify` Terrain-RGB `PMTiles` pipeline with HTTP `206 Partial Content` byte-range serving and ETag support.
- MapLibre GL JS 5 web application with color-coded difficulty styling, steep-grade highlight overlays, 3D hillshade/contours, configurable Alpine snow-line elevation band (`1,000 m` default), 18 outdoor SVG POI icons, unified search (tracks, POIs, `lat,lon`, MGRS), and an explainable **Track Inspector** with interactive elevation profile.

#### Slice 04 — Live VIC & NSW Closures & Incident Feeds
- Pluggable feed sync engine (`vic_mapshare_seasonal` via DEECA DataVic WFS `paim_vm_tr_road_closures`, `vic_emergency` GeoJSON, `nsw_rfs` `majorIncidents.json`, `nsw_npws` official RSS 2.0 park alerts joined to CAPAD boundaries, and `osm_access` private/gate propagation).
- Graceful upstream outage fallback retaining last-known closure state with explicit staleness indicators (`is_stale`) and official agency source URLs.

#### Slice 05 — Vehicle Profiles & Self-Hosted User Accounts
- Anonymous read-only browsing paired with lightweight self-hosted email/password accounts (zero external IdP), customizable **Vehicle Profiles** deterministically classified into `AWD`, `Stock 4WD`, `Touring 4WD`, or `Modified 4WD`, personalized rig suitability verdicts (`Suited`, `Challenging`, `High Risk`), saved favorite tracks, personal map pins, and GridForge-compatible **GPX 1.1 / GeoJSON** export.

#### Slice 06 — Privacy-Preserving Telemetry Ingestion & In-PostGIS HMM Matching *(Checkpoint B)*
- GPX 1.1 and Telemetry JSON (with optional IMU `roughness_rms`) upload API enforcing mandatory `>= 250 m` start/end privacy buffer trimming, revocable HMAC `contribution_hash` anonymization (zero `user_id` or `trip_id` on public `telemetry_observations`), instant `private` <-> `community_anonymous` toggle, and separate anonymized storage of off-network **unmapped track candidates**.
- Pluggable `TrackMatcher` protocol backed by a lightweight in-PostGIS **Hidden Markov Model (HMM)** matcher (`0.9739` mean match confidence across `145` Davies Plain segments; Valhalla/OSRM swappable).

#### Slice 07 — Explainable Crowdsourced Grading & 72 h Rainfall Engine
- Recency-weighted live grading combining static DEM/OSM baselines with vehicle-class-normalized crowdsourced speeds, durations, and IMU roughness, surfacing confidence levels, last-driven dates, vehicle-class traffic splits, and slow/rough obstacle hotspot markers.
- **Open-Meteo (CC BY 4.0)** 72-hour rainfall snapshot integration driving transparent wet-condition risk advisories and preventing single outlier traversals from skewing ratings.

#### Slice 08 — Community Condition Reports & Gate Warnings
- Structured hazard and access reports (`fallen_tree`, `bog_hole`, `washout`, `deep_water`, `locked_gate`, `private_property`, `snow_ice`, `track_clear`) with validated photo attachments, confirm/resolved voting, per-type half-life staleness decay, distinct locked-gate/private-property warning halos alongside official closures, and one-click **Suggest OSM Access Fix** / **Add OSM Note** links.

#### Slice 09 — Offline Regional Packs & In-Cab PWA Breadcrumb Recorder *(Checkpoint C)*
- Streaming disk-to-disk regional offline pack builder (`australian-alps`, `blue-mountains`, `vic`, `nsw`) emitting SHA-256 verified manifests, vector/terrain `PMTiles`, and offline track/POI/closure/report snapshots (`51.79 MB` measured peak heap vs `< 300 MB` guardrail).
- Offline-capable PWA Service Worker (Cache API HTTP `206` byte-range PMTiles serving), live in-cab GPS breadcrumb recorder with one-tap **Backtrack** bearing/distance guidance, Screen Wake Lock, optional Web `DeviceMotion` roughness capture, IndexedDB offline sync queue, and GPX/GeoJSON export.

#### Examples & Documentation
- Reproducible end-to-end API walkthrough (`examples/demo-walkthrough.sh`), Davies Plain GPX trace (`examples/davies_plain_drive.gpx`), IMU Telemetry JSON sample (`examples/telemetry_sample.json`), and GridForge companion strip-map handoff instructions (`examples/README.md`).

### Verification Metrics (`v1.0.0-alpha.1`)
- **Offline CI Tests:** `103` backend Python unit + PostGIS integration tests and `76` frontend Vitest tests (`179` offline automated checks passing with zero network access).
- **Live Upstream AU API Smoke Checks:** `3/3` `@pytest.mark.live` checks verified against live CAPAD 2025, DataVic WFS (`tr_road` & `paim_vm_tr_road_closures`), NSW Spatial Services, GA Surface Hydrology, VicEmergency, NSW RFS, NSW NPWS RSS 2.0, and Open-Meteo.
- **Resource Footprint:** `~269 MiB` idle RAM across all 4 Docker Compose services (`db` `85.1 MiB`, `api` `97.3 MiB`, `worker` `77.8 MiB`, `web` `8.5 MiB`); `51.79 MB` peak heap during offline pack builds (`< 300 MB` target).

### Alpha Scope & Next Milestones
- **Current Alpha Scope:** End-to-end verified on the synthetic/seeded VIC/NSW Australian Alps corridor with live upstream connector/feed smoke tests. Statewide VIC + NSW real-data pack bakes and multi-week live agency soak testing are tracked for `v1.0.0` GA.
- **Planned `v1.1+` Follow-Ons:** Native Capacitor/React Native background GPS shell, CarPlay/Android Auto projection, pan-Australia (QLD/WA/SA/TAS/NT) expansion, and optional Valhalla map-matching plugin.
