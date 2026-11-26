# Changelog

All notable changes to **TrailForge** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
