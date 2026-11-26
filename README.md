<div align="center">

# TrailForge

**Open-source, self-hostable 4WD & off-road mapping, live track-grading, multi-track route planning, and trail-intelligence platform — launching first for Victoria & New South Wales (including ACT).**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Release: v1.0.0-alpha.1](https://img.shields.io/badge/Release-v1.0.0--alpha.1-8b5cf6)](CHANGELOG.md)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![PostGIS 3.4 + PMTiles](https://img.shields.io/badge/PostGIS-3.4%20%2B%20PMTiles-336791?logo=postgresql&logoColor=white)](Dockerfile)
[![MapLibre GL JS 5](https://img.shields.io/badge/MapLibre%20GL%20JS-5-396CB2?logo=maplibre&logoColor=white)](web/package.json)
[![Multi-Arch](https://img.shields.io/badge/Docker-arm64%20%7C%20amd64-2496ED?logo=docker&logoColor=white)](docker-compose.ghcr.yml)
[![Tests](https://img.shields.io/badge/Tests-207%2B%20passing-2ea44f)](tests/)
[![Idle RAM](https://img.shields.io/badge/Idle%20RAM-~338%20MiB-f59e0b)](docker-compose.yml)
[![Sibling: GridForge](https://img.shields.io/badge/Sibling-GridForge-10b981)](https://github.com/jremick/gridforge)

Explainable 2-Stage Grading · Interactive Multi-Track Route Planner · 250 m Privacy-Trimmed HMM Telemetry · Live VIC & NSW Closures · Real-Data Regional Bake CLI · Streaming Offline Packs · Zero-Build GHCR & HTTPS Deploy Kit · GridForge 300 DPI Strip Atlas Handoff

</div>

---

![TrailForge VIC & NSW 4WD Map and Track Inspector](docs/images/trailforge-hero.png)

## What Makes TrailForge Different

| Capability | TrailForge | Proprietary 4WD Apps (Newtracs / OnX) |
| :--- | :--- | :--- |
| **Transparent Two-Stage Grading** | Combines a deterministic **Day-1 DEM/OSM baseline** (`surface`, `tracktype`, max/sustained slope degrees, waterway crossings, `4wd_only`) with recency-weighted, vehicle-class-normalized telemetry (`AWD`–`Modified 4WD`) and **72-hour rainfall** context — exposing exact factor breakdowns, sample counts, freshness, and obstacle hotspots. | Opaque black-box difficulty scores with no visibility into terrain vs. traffic weighting, vehicle capability bias, or wet-weather risk shifts. |
| **Interactive Multi-Track Route Planner** | Chain multiple 4WD tracks into a single stitched route with **2-state DP automatic leg orientation**, `> 100 m` gap detection, combined 4-grade elevation profile, worst-grade & rig suitability summary, closure/gate/wet-risk alerts, saved trip plans, and one-click **GridForge 300 DPI Strip Atlas** CLI handoff. | Disconnected track pages or cloud-locked route builders with no combined closure/suitability rollup or topo print handoff. |
| **Privacy-by-Design Telemetry** | Enforces mandatory **`≥ 250 m` start/end buffer trimming**, stores **zero `user_id` or `trip_id`** on public segment observations, supports anonymous read-only browsing, and provides instant **private ↔ anonymous community** HMAC revocation. | Mandatory cloud accounts with opaque location-history retention and limited control over contributed GPS traces. |
| **Official VIC & NSW Closures & CAPAD Park Alerts** | Pluggable ingesters for **VIC MapShareVic** seasonal closures, **VicEmergency** incidents, **NSW RFS** major incidents, **NSW NPWS** park alerts (joined to CAPAD 2025 boundaries), and OSM `barrier=gate` / `access=private` flags — retaining cached state with visible **outage staleness badges** when agencies go offline. | Manually curated or delayed closures disconnected from live Australian emergency and national park feeds. |
| **Real-Data Regional Bake & Offline PWA Packs** | Streaming **`< 300 MB` RAM** OSM PBF (`osmium`) + VIC/NSW open-data bake CLI (`bake_cli`) and regional offline packs (`australian-alps`, `blue-mountains`, `vic`, `nsw`) with SHA-256 manifests, Service Worker **HTTP `206` range-sliced** `PMTiles`, live GPS breadcrumb recorder, and one-tap **Backtrack**. | Paywalled offline downloads locked inside proprietary mobile silos with no self-hosted control. |
| **Zero-Build Self-Hosting & Production HTTPS Kit** | Multi-arch (`arm64` + `amd64`) prebuilt GHCR Compose (`docker-compose.ghcr.yml`) + turnkey **Tailscale Serve**, **Caddy v2**, and **Traefik v3** HTTPS guides (`deploy/`) with Range-safe `206` streaming, **zero Redis/Kafka**, and **zero PyQGIS**. | Cloud-only SaaS — cannot be self-hosted for 4WD clubs, SAR teams, or private homelabs. |
| **100% Open & ODbL-Clean Licensing** | `GPL-3.0-or-later` source code with strict **3-tier OSM ODbL table separation** isolating OpenStreetMap base geometries/tags from crowdsourced telemetry, live grades, closures, condition reports, and saved `planned_routes`. | Closed-source subscriptions and proprietary map/data lock-in. |

---

## Visual Feature Gallery

| **Interactive Multi-Track Route Planner & GridForge Handoff** | **Rig Suitability, Closures & Gate Warnings** |
| :---: | :---: |
| ![Interactive Multi-Track Route Planner and GridForge Strip Atlas Handoff](docs/images/trailforge-route-planner.png) | ![Rig Suitability, Closures and Gate Warnings](docs/images/trailforge-rig-closures.png) |
| *Stitched multi-track routes (`S → 2 → F`), combined 4-grade elevation SVG, closure/rig risk summary & 1-click GridForge 300 DPI CLI handoff* | *Rig Suitability (`AWD`–`Modified 4WD`), Official VIC/NSW Closures & Community Gate Warnings with One-Click OSM Fixes* |

| **Streaming Offline Packs (VIC & NSW)** | **In-Cab PWA Recorder & Backtrack** |
| :---: | :---: |
| ![Streaming VIC and NSW Offline Regional Packs](docs/images/trailforge-offline-packs.png) | ![In-Cab PWA Breadcrumb Recorder and Backtrack](docs/images/trailforge-recorder.png) |
| *Streaming VIC & NSW Offline Regional Packs (`australian-alps`, `blue-mountains`, `vic`, `nsw`) with SHA-256 Verification* | *In-Cab PWA Breadcrumb Recorder with One-Tap Backtrack, Screen Wake Lock & Offline GPX/GeoJSON Export* |

---

## 60-Second Self-Hosted Quickstart

### Option A: Zero-Build Production / Homelab Deploy (Prebuilt Multi-Arch GHCR)

Pull prebuilt `linux/arm64` + `linux/amd64` images directly from GitHub Container Registry — **no local Docker builds required**:

```bash
git clone https://github.com/jremick/trailforge.git
cd trailforge
cp .env.example .env
# Edit .env: set strong POSTGRES_PASSWORD, DATABASE_URL, AUTH_SECRET_KEY &
# TRAILFORGE_PRIVACY_SECRET (`openssl rand -hex 32`), CORS_ORIGINS, and AUTH_COOKIE_SECURE=true
docker compose -f docker-compose.ghcr.yml up -d
curl -fsS http://127.0.0.1:5173/api/health
```

`docker-compose.ghcr.yml` binds safely to `127.0.0.1:5173` by default for HTTPS reverse-proxy deployments (required on phones/tablets for PWA Service Worker offline packs, Geolocation API, Screen Wake Lock, and `AUTH_COOKIE_SECURE=true`). See the turnkey **[Production HTTPS Deploy Kit (`deploy/README.md`)](deploy/README.md)** for:
- **[Tailscale Serve (`deploy/tailscale-serve.md`)](deploy/tailscale-serve.md)** — zero-port-forward private Tailnet HTTPS for phones and in-cab tablets
- **[Caddy v2 (`deploy/Caddyfile.example`)](deploy/Caddyfile.example)** — automatic Let's Encrypt HTTPS with 15 MB upload cap and Range/HTTP `206` PMTiles passthrough
- **[Traefik v3 (`deploy/traefik-compose.override.yml`)](deploy/traefik-compose.override.yml)** — drop-in Compose override with Range-safe headers and upload limits
- **Backup & Upgrade Runbook** — PostGIS `pg_dump`, `/data/uploads` & `/data/packs` archives, and low-downtime image tag upgrades

### Option B: Local Build & Demo Corridor Quickstart (`docker compose up`)

Build locally on Apple Silicon (`arm64`) or Linux (`amd64`) and seed the offline Australian Alps demo corridor:

```bash
git clone https://github.com/jremick/trailforge.git
cd trailforge
cp .env.example .env
docker compose up -d --build --wait
make seed-corridor
```

Open **<http://localhost:5173>** in your browser (interactive OpenAPI docs at **<http://localhost:8000/docs>**, health check at `/api/health`).

### What `make seed-corridor` Loads (Australian Alps High Country Demo)

`make seed-corridor` runs the full 6-stage offline pipeline (`ingestion` → `feeds` → `seed_demo` → `grading` → `tiles` → `packs`) on the **VIC/NSW Davies Plain / Tom Groggin border corridor** (MGRS `55H FV`, `600–1,634 m` AHD):

- **Conflated Multi-Source Tracks:** `Davies Plain Track Lower`, `Davies Plain Ridge`, `Bucky Ridge Fire Trail`, and `Pinch River Fire Trail` (plus `Tom Groggin Access Rd`, `Poplars Fire Trail`, `Station Creek Private Spur`, and `Limestone Creek Bridged Link`) conflated across OSM, VIC Vicmap/RecWeb, and NSW Spatial Services into deterministic 50–100 m segments.
- **Terrain, Waterways & Snow-Line:** DEM-draped elevation profiles, steep-section highlights, unbridged **Murray River ford** crossing attribution, 20 m contours, Terrain-RGB hillshade, and an interactive **alpine snow-line slider** (`800–1,600 m`, default `1,000 m`).
- **Live VIC & NSW Closures & Gates:** MapShareVic winter seasonal closure on Davies Plain, VicEmergency fuel-reduction burn Watch & Act on Bucky Ridge, NSW NPWS Kosciuszko wet-conditions advisory joined to CAPAD boundaries, and an OSM locked gate on Station Creek Private Spur.
- **Live Telemetry, Rainfall & Hotspots:** Anonymized multi-rig passes (`Stock 4WD` / `Touring 4WD` / `Modified 4WD`), `18.5 mm` 72-hour rainfall wet-condition context, and 22 slow/rough escarpment obstacle hotspots.
- **Ready-to-Download Offline Pack:** Pre-built `australian-alps` offline pack (`vector.pmtiles`, `terrain.pmtiles`, `snapshot.json`, SHA-256 `manifest.json`, and `bundle.tar.gz`) ready to cache in-browser for zero-signal navigation.

> **Anonymous-first:** Browse maps, inspect tracks, plan multi-track routes, search VIC/NSW tracks/POIs/`lat,lon`/MGRS, switch Quick Rig classes (`AWD`–`Modified 4WD`), and export GPX/GeoJSON with zero sign-in. Register locally inside the **Rig & Account** drawer to save custom Vehicle Profiles, saved routes, favorites, personal pins, GPX/telemetry uploads, and timestamped hazard/gate reports. Stop the stack cleanly anytime with `make down`.

---

## Interactive Multi-Track Route Planner & GridForge Handoff

Plan multi-track 4WD trips directly in the map UI (**`Route`** pill in the top bar or **`+ Route`** in the Track Inspector) or via `/api/routes`:

- **Click-to-Chain Planning & Automatic Leg Orientation:** Click tracks on the map to append/reorder legs; a 2-state dynamic-programming optimizer automatically reverses legs when needed to minimize endpoint jumps and flags any `> 100 m` road-link gaps.
- **Unified Trip Intelligence:** Aggregates total distance, estimated travel time (using live telemetry speed or grade heuristics), elevation gain/loss, combined 4-grade SVG elevation profile, worst difficulty & active rig suitability verdict, intersecting official VIC/NSW closures, community locked-gate warnings, wet-weather risk, unbridged waterway crossings, and nearby huts/campsites.
- **Offline-Capable Planning & Saved Trips:** Works offline from downloaded regional pack snapshots, and authenticated users can save/update/reload personal route plans in Tier C `planned_routes` (strictly ODbL-separated — stores only canonical `track_ids` and user waypoints, zero OSM geometries/tags).
- **One-Click GridForge 300 DPI Strip Atlas Handoff:** Export stitched **GPX 1.1** (complete with `<trk>` trackpoints plus `<wpt>` pins for crossings, camps/huts, and custom waypoints) or **RFC 7946 GeoJSON**, and copy the pre-filled `gridforge render --strip-route ...` CLI command directly from the Route Planner drawer to print a true-scale 1:25,000 UTM/MGRS waterproof backup strip atlas in sibling project [GridForge](https://github.com/jremick/gridforge):

```bash
# Export a planned multi-track route (or click "⬇ GPX 1.1" + "Copy CLI" in the Route Planner drawer)
mkdir -p out
curl -fsSL -X POST "http://localhost:5173/api/routes/plan/export?format=gpx" \
  -H "Content-Type: application/json" \
  -d '{"name":"Davies Plain Loop","track_ids":["<track_id_1>","<track_id_2>"]}' \
  -o out/davies_plain_loop.gpx

# Render a 300 DPI true-scale UTM strip atlas + elevation profile + 100 mm calibration sheet in GridForge
docker run --rm -v "$(pwd)/out:/out" ghcr.io/jremick/gridforge:latest \
  gridforge render --strip-route /out/davies_plain_loop.gpx --scale 25000 --paper a4 \
    --dem-source au-elvis --au-overlay au-state-trails --slope-shading \
    --elevation-profile --calibration-page \
    --title "DAVIES PLAIN LOOP — VIC/NSW BACKUP STRIP ATLAS" \
    --output /out/davies_plain_loop_strip_atlas.pdf
```

---

## Real-Data Regional Bake CLI & Demo Cookbook

### Streaming Real-Data Regional & Statewide Bake (`bake_cli`)

Bake real Australian open-data networks (streaming Geofabrik `.osm.pbf` via `osmium` or Overpass + VIC DataVic WFS + NSW Spatial ArcGIS + GA Surface Hydrology + CAPAD 2025 + GA 5 m LiDAR / DEA SRTM 1s COG DEM) in memory-bounded `0.25°` spatial tiles with atomic resumable manifests (`< 300 MB` RAM):

```bash
# Convenience targets for Australian Alps & Blue Mountains (optional local PBF or bbox override)
make bake-alps BAKE_ARGS="--bbox 148.08,-36.56,148.15,-36.50"
make bake-blue-mountains BAKE_ARGS="--pbf /data/cache/australia-latest.osm.pbf"

# Full CLI with tiled resumable manifest, live closures, rainfall, PMTiles & offline pack build
docker compose exec -T api python -W error::RuntimeWarning -m trailforge.ingestion.bake_cli \
  --region vic \
  --pbf /data/cache/australia-latest.osm.pbf \
  --cache-dir /data/cache \
  --build-tiles --build-pack

# Offline dry-run & 10,000+ segment PostGIS GIST scale benchmark
docker compose exec -T api python -W error::RuntimeWarning -m trailforge.ingestion.bake_cli --fixtures tests/fixtures --dry-run
docker compose exec -T api python -W error::RuntimeWarning -m trailforge.ingestion.benchmark
```

### One-Command API & GPX Walkthrough

Explore the complete v1 API flow against your running stack using [`examples/demo-walkthrough.sh`](examples/demo-walkthrough.sh), [`examples/davies_plain_drive.gpx`](examples/davies_plain_drive.gpx), and [`examples/telemetry_sample.json`](examples/telemetry_sample.json) (see [`examples/README.md`](examples/README.md) for details):

```bash
./examples/demo-walkthrough.sh
```

### Individual Stage CLI Commands

Every stage is also scriptable headlessly inside the `api` container (zero external brokers):

```bash
# 1. Ingest & conflate OSM + VIC + NSW tracks into deterministic 50–100 m segments + Day-1 DEM grading
docker compose exec -T api python -m trailforge.ingestion.pipeline --fixtures tests/fixtures

# 2. Sync VIC/NSW official closures, emergency feeds, CAPAD park alerts & OSM gates (--fixtures or --live)
docker compose exec -T api python -m trailforge.feeds.cli --fixtures tests/fixtures/closures_sample.json
docker compose exec -T api python -m trailforge.feeds.cli --live

# 3. Recompute explainable crowdsourced live grades, hotspots & 72h rainfall (--offline-rain-72h or --live-rainfall)
docker compose exec -T api python -m trailforge.grading.cli --offline-rain-72h 18.5
docker compose exec -T api python -m trailforge.grading.cli --live-rainfall

# 4. Build vector & Terrain-RGB PMTiles (graded tracks, steep highlights, contours, snow-line bands, POIs)
docker compose exec -T api python -m trailforge.tiles.cli \
  --output-dir /data/tiles \
  --dem tests/fixtures/dem_alps_corridor.tif

# 5. Build streaming regional offline packs (australian-alps, blue-mountains, vic, nsw; < 300 MB RAM)
docker compose exec -T api python -m trailforge.packs.cli \
  --region australian-alps \
  --output-dir /data/packs \
  --dem tests/fixtures/dem_alps_corridor.tif
```

### v1 Regional Offline Pack Catalog

| Region ID | Scope | Type | MGRS / UTM Zones | WGS84 BBox (`w, s, e, n`) |
| :--- | :--- | :--- | :--- | :--- |
| **`australian-alps`** | Victorian High Country, Davies Plain, Tom Groggin, Alpine NP & Kosciuszko NP | Tactical | `55H` | `146.5, -37.6, 149.2, -35.4` |
| **`blue-mountains`** | Newnes Plateau, Wollemi, Kanangra-Boyd, Abercrombie & Blue Mountains | Tactical | `56H` | `149.7, -34.2, 150.9, -32.9` |
| **`vic`** | Statewide Victoria (Mallee, Grampians, Otways, High Country, East Gippsland) | State | `54H`, `55H` | `140.9, -39.2, 150.1, -33.9` |
| **`nsw`** | Statewide NSW & ACT (Snowy Mountains, Blue Mountains, Barrington Tops, Outback) | State | `54H`–`56J` | `140.9, -37.6, 153.7, -28.1` |

---

## Measured Performance, Accuracy & Memory Profile

Measured on an Apple Silicon **16 GB RAM** host running native `linux/arm64` Docker Compose (`imresamu/postgis:16-3.4`, Python `3.12`, GDAL, `osmium-tool`, Tippecanoe `2.79.0`, `go-pmtiles 1.31.2`, Nginx + MapLibre GL JS `5`):

| Metric | Measured Result | Guardrail / Target |
| :--- | :--- | :--- |
| **Combined stack idle RAM (`docker stats`, 4 healthy containers)** | **`~338 MiB`** total (`db` `149.8 MiB` · `api` `94.9 MiB` · `worker` `86.0 MiB` · `web` `7.7 MiB`) | 16 GB laptop / homelab friendly (`512m`/`512m`/`512m`/`128m` caps, zero Redis/Kafka) |
| **10,000+ segment scale benchmark (`benchmark.py`)** | **`11,179` canonical segments** (`150` tracks) in **`13.65 MiB`** peak Python heap (`138.49 MiB` RSS) | `< 300 MB` streaming RAM budget |
| **PostGIS `EXPLAIN ANALYZE` over `11,179` segments** | **`0.18–0.63 ms`** GIST index scans (`bbox` `0.291 ms` · HMM `ST_DWithin` `0.184 ms` · closure join `0.635 ms`; **`0` Seq Scans** on `track_segments`) | Indexed `ix_track_segments_geom_active` GIST lookups |
| **Offline pack builder peak Python heap** | **`51.79 MB`** (`australian-alps` PMTiles + snapshot + `bundle.tar.gz`) | `< 300 MB` streaming RAM budget |
| **Canonical segment-ID preservation (`v2` jittered re-import)** | **`100%`** (`358 exact + 86 spatial` = `444 / 444` preserved; `0` orphaned) | Deterministic identity across OSM/state updates |
| **In-PostGIS HMM map-matching confidence** | **`0.9739`** mean confidence across **`145`** Davies Plain segments | Pluggable `TrackMatcher` protocol (Valhalla-ready) |
| **Offline `PMTiles` serving (Nginx & Service Worker)** | **HTTP `206 Partial Content`** (`Accept-Ranges: bytes`, SHA-256 verified manifest) | Zero full-archive buffering in browser |
| **Automated offline test suite (`pytest` + Vitest)** | **`207+` offline tests passing** (`121` isolated Docker PostGIS + `86` Vitest web + `4` host deploy-manifest parity checks) | 100% offline CI gate on isolated `trailforge_test` DB |
| **Live upstream AU agency & real corridor bake tests (`@pytest.mark.live`)** | **`4/4` passing** across 8 live AU agencies (CAPAD 2025, DataVic WFS, NSW Spatial, GA Hydrology, VicEmergency, NSW RFS, NSW NPWS RSS 2.0, Open-Meteo) **plus real AU corridor bake** (Blue Mountains Narrow Neck & Alps Tom Groggin `592` real segments + `2.3 MiB` vector/`300 KiB` terrain PMTiles) | Real-data AU connector & bake verification |

---

## Architecture & Testing

```text
src/trailforge/
├── config.py              # Pydantic v2 settings, privacy/memory guardrails, auth & storage paths
├── db/                    # SQLAlchemy 2.0 + GeoAlchemy2 3-tier ODbL-separated PostGIS schema (27 tables)
├── auth/                  # Self-hosted scrypt password hashing & HMAC-SHA256 bearer/cookie auth
├── ingestion/             # OSM/VIC/NSW/GA/CAPAD connectors, streaming PBF, tiled live_bake CLI, conflation, DEM & benchmark
├── grading/               # Day-1 deterministic baseline + recency/rig-weighted live engine + Open-Meteo 72h rainfall
├── feeds/                 # Pluggable VIC MapShareVic, VicEmergency, NSW RFS, NSW NPWS & OSM gate sync worker
├── vehicles/              # Deterministic AU 4WD capability classifier (AWD -> Modified 4WD) & rig suitability
├── matching/              # Mandatory >=250 m privacy trimmer, GPX/JSON parser & pluggable in-PostGIS HMM matcher
├── reports/               # Structured condition & locked-gate reports, photo validation, confirm/resolve decay
├── routes/                # Multi-track route planner (2-state DP leg orientation, gap detection, GPX/GeoJSON & GridForge CLI)
├── tiles/                 # PostGIS + GDAL + Tippecanoe + rio-rgbify -> vector & Terrain-RGB PMTiles builder
├── packs/                 # Streaming VIC/NSW regional offline pack catalog, SHA-256 manifest & bundle builder
├── export/                # GridForge-compatible GPX 1.1 & RFC 7946 GeoJSON exporters
├── api/                   # FastAPI routers (tracks, routes, tiles, closures, auth, vehicles, trips, grading, reports, packs)
└── worker/                # Lightweight periodic background worker (feed sync, live grading, demo corridor seeder)
web/
├── src/                   # TypeScript + Vite + MapLibre GL JS 5 PWA (map, layers, search, grading, closures, reports, routes)
│   ├── components/        # Topbar, sidebar, track inspector, route planner drawer, rig/account drawer, packs manager, recorder
│   └── offline/           # IndexedDB + Cache Storage packStore, HTTP 206 range slicer, GPS recorder & deferred syncQueue
└── public/sw.js           # Offline-first Service Worker serving range-sliced PMTiles & cached pack assets
deploy/                    # Production HTTPS deploy kit (README checklist, Tailscale Serve, Caddy v2, Traefik v3 override)
migrations/                # Alembic spatial migrations (0001 initial schema + 0002 planned_routes, 20 GIST indexes)
assets/icons/              # 21 self-contained outdoor/topo/4WD SVG symbols (adapted from GridForge)
examples/                  # Runnable walkthrough script, Davies Plain GPX & sample telemetry JSON
tests/                     # 121+ offline Python/PostGIS tests (isolated trailforge_test DB) + synthetic fixtures + 4/4 live checks
```

```bash
# Lint & formatting checks (ruff)
make lint

# Offline host unit + deploy config parity tests (no DB required)
make test-unit

# Isolated PostGIS integration suite (auto-provisions & migrates trailforge_test without touching dev data or racing worker-1)
make test-integration

# Full offline Python suite (lint + host unit + isolated Docker PostGIS integration)
make test

# Web PWA TypeScript typecheck, production Vite build & 86 Vitest unit tests
make web-check

# Optional live upstream connector, agency feed & real AU corridor bake tests (network required)
docker compose exec -T api pytest -m live -v --override-ini="addopts="
```

See [`docs/PLAN.md`](docs/PLAN.md) for the vertical-slice roadmap, [`docs/SPEC_GIS_SCHEMA.md`](docs/SPEC_GIS_SCHEMA.md) for the GIS connector & 3-tier spatial schema specification, [`deploy/README.md`](deploy/README.md) for production self-hosting, [`AGENTS.md`](AGENTS.md) for engineering invariants, and [`CONTRIBUTING.md`](CONTRIBUTING.md) for contribution guidelines.

---

## Release Status & Roadmap (`v1.0.0-alpha.1` → `v1.0.0-beta.1` / `v1.0.0` GA)

- **Shipped in `v1.0.0-alpha.1`:** All **9 vertical slices** from [`docs/PLAN.md`](docs/PLAN.md) verified end-to-end across Checkpoints A, B, and C on the VIC/NSW Australian Alps corridor.
- **Completed in Phase A (Ready for `v1.0.0-beta.1`):**
  - Streaming OSM PBF + live VIC/NSW open-data tiled regional bake pipeline (`bake_cli`), real-data AU corridor bake verification (Blue Mountains & Tom Groggin), and `11,179`-segment PostGIS GIST scale benchmark (`13.65 MiB` heap, `0.18–0.63 ms` index scans).
  - Multi-arch GHCR release workflow (`.github/workflows/release-images.yml`), zero-build `docker-compose.ghcr.yml`, turnkey HTTPS deploy kit (`deploy/`), and isolated `trailforge_test` PostGIS integration test DB (`make test-integration`).
  - Interactive Multi-Track Route Planner (`planned_routes` migration, DP leg orientation, combined 4-grade elevation profile, saved routes CRUD, GPX/GeoJSON export, and 1-click GridForge 300 DPI Strip Atlas CLI handoff).
- **Next Milestones toward `v1.0.0` GA:** Full statewide Victoria (`vic`) & New South Wales (`nsw`) production PBF/DEM pack bakes and multi-week live agency soak testing across VIC/NSW feeds and Open-Meteo.
- **Planned `v1.1+` Follow-Ons:** Native mobile shell (Capacitor / React Native) for locked-screen iOS & Android background GPS + high-rate IMU roughness capture, Apple CarPlay & Android Auto projection, pan-Australia expansion (`QLD`, `WA`, `SA`, `TAS`, `NT`), and an optional Valhalla map-matching sidecar plugin behind the existing `TrackMatcher` protocol.

---

## Data Sources, Attributions & Licensing

| Dataset / Component | Scope | License & Attribution |
| :--- | :--- | :--- |
| **TrailForge Source Code** | Backend API, spatial pipelines, worker, web PWA | **GNU GPL v3.0 or later** ([LICENSE](LICENSE)) |
| **OpenStreetMap** | Roads, 4WD tracks, gates, access tags, POIs | **ODbL** — Map data © OpenStreetMap contributors (`openstreetmap.org/copyright`) |
| **Geoscience Australia / DEA** | Australia 5 m LiDAR & SRTM 1s DEM (`AHD`), Surface Hydrology | **CC BY 4.0** — © Commonwealth of Australia (Geoscience Australia / Digital Earth Australia) |
| **DCCEEW CAPAD 2025** | National Parks, Nature Reserves, IPAs | **CC BY 4.0** — © Commonwealth of Australia (DCCEEW) |
| **NSW Spatial Services (DCS)** | NSW fire trails, 4WD tracks, huts, POIs & trig stations | **CC BY 4.0** — © State of New South Wales (Spatial Services, DCS) |
| **Victoria DEECA / DTP Vicmap** | VIC fire/4WD tracks, recreation tracks, huts, sites & seasonal closures | **CC BY 4.0** — © State of Victoria (DEECA / DTP Vicmap) |
| **VicEmergency / NSW RFS / NSW NPWS** | Live emergency incidents & park closure alerts | Official state agency public feeds (attributed per feed with live source links) |
| **Open-Meteo** | Recent 72 h precipitation context (wet/dry weighting) | **CC BY 4.0** (non-commercial API; AGPLv3 self-hostable backend) |

TrailForge enforces strict **OSM ODbL separation** (isolating OpenStreetMap base tables from independent crowdsourced telemetry, live grades, closures, condition reports, and saved routes linked only by stable canonical segment/track IDs) and preserves all required attributions across the web UI footer, API responses, GPX/GeoJSON exports, and offline pack manifests.
