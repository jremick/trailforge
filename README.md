<div align="center">

# TrailForge

**Open-source, self-hostable 4WD & off-road mapping, live track-grading, and trail-intelligence platform — launching first for Victoria & New South Wales (including ACT).**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Release: v1.0.0-alpha.1](https://img.shields.io/badge/Release-v1.0.0--alpha.1-8b5cf6)](CHANGELOG.md)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![PostGIS 3.4 + PMTiles](https://img.shields.io/badge/PostGIS-3.4%20%2B%20PMTiles-336791?logo=postgresql&logoColor=white)](Dockerfile)
[![MapLibre GL JS 5](https://img.shields.io/badge/MapLibre%20GL%20JS-5-396CB2?logo=maplibre&logoColor=white)](web/package.json)
[![Multi-Arch](https://img.shields.io/badge/Docker-arm64%20%7C%20amd64-2496ED?logo=docker&logoColor=white)](docker-compose.yml)
[![Tests](https://img.shields.io/badge/Tests-179%2B%20passing-2ea44f)](tests/)
[![Idle RAM](https://img.shields.io/badge/Idle%20RAM-~269%20MiB-f59e0b)](docker-compose.yml)
[![Sibling: GridForge](https://img.shields.io/badge/Sibling-GridForge-10b981)](https://github.com/jremick/gridforge)

Explainable 2-Stage Grading · 250 m Privacy-Trimmed HMM Telemetry · Live VIC & NSW Closures · Alpine Snow-Line Band · Streaming Offline Packs · In-Cab PWA Backtrack Recorder · GridForge GPX/GeoJSON Handoff

</div>

---

![TrailForge VIC & NSW 4WD Map and Track Inspector](docs/images/trailforge-hero.png)

## What Makes TrailForge Different

| Capability | TrailForge | Proprietary 4WD Apps (Newtracs / OnX) |
| :--- | :--- | :--- |
| **Transparent Two-Stage Grading** | Combines a deterministic **Day-1 DEM/OSM baseline** (`surface`, `tracktype`, max/sustained slope degrees, waterway crossings, `4wd_only`) with recency-weighted, vehicle-class-normalized telemetry (`AWD`–`Modified 4WD`) and **72-hour rainfall** context — exposing exact factor breakdowns, sample counts, freshness, and obstacle hotspots. | Opaque black-box difficulty scores with no visibility into terrain vs. traffic weighting, vehicle capability bias, or wet-weather risk shifts. |
| **Privacy-by-Design Telemetry** | Enforces mandatory **`≥ 250 m` start/end buffer trimming**, stores **zero `user_id` or `trip_id`** on public segment observations, supports anonymous read-only browsing, and provides instant **private ↔ anonymous community** HMAC revocation. | Mandatory cloud accounts with opaque location-history retention and limited control over contributed GPS traces. |
| **Official VIC & NSW Closures & CAPAD Park Alerts** | Pluggable ingesters for **VIC MapShareVic** seasonal closures, **VicEmergency** incidents, **NSW RFS** major incidents, **NSW NPWS** park alerts (joined to CAPAD 2025 boundaries), and OSM `barrier=gate` / `access=private` flags — retaining cached state with visible **outage staleness badges** when agencies go offline. | Manually curated or delayed closures disconnected from live Australian emergency and national park feeds. |
| **Offline Regional Packs & In-Cab PWA Recorder** | Streaming **`< 300 MB` RAM** regional packs (`australian-alps`, `blue-mountains`, `vic`, `nsw`) with SHA-256 verification and Service Worker **HTTP `206` range-sliced** offline `PMTiles`, paired with a live GPS breadcrumb recorder, one-tap **Backtrack**, Screen Wake Lock, and deferred offline sync queue. | Paywalled offline downloads locked inside proprietary mobile silos with no self-hosted control. |
| **Built to Pair with GridForge** | One-click **GPX 1.1 & RFC 7946 GeoJSON** exports for tracks, personal pins, and recorded trips — plug directly into sibling project [GridForge](https://github.com/jremick/gridforge) for 300 DPI true-scale UTM/MGRS waterproof backup paper strip atlases. | Walled-garden route storage with limited or degraded topo print interoperability. |
| **16 GB Laptop / Homelab Friendly** | Single `docker compose up` across PostGIS 16/3.4 + FastAPI + Worker + Nginx PWA (**`~269 MiB`** combined idle RAM), **zero Redis/Kafka**, and **zero PyQGIS** dependency. | Cloud-only SaaS — cannot be self-hosted for 4WD clubs, SAR teams, or private homelabs. |
| **100% Open & ODbL-Clean Licensing** | `GPL-3.0-or-later` source code with strict **3-tier OSM ODbL table separation** isolating OpenStreetMap base geometries/tags from crowdsourced telemetry, live grades, closures, and condition reports. | Closed-source subscriptions and proprietary map/data lock-in. |

---

## Visual Feature Gallery

| **Rig Suitability, Closures & Gate Warnings** | **Streaming Offline Packs (VIC & NSW)** | **In-Cab PWA Recorder & Backtrack** |
| :---: | :---: | :---: |
| ![Rig Suitability, Closures and Gate Warnings](docs/images/trailforge-rig-closures.png) | ![Streaming VIC and NSW Offline Regional Packs](docs/images/trailforge-offline-packs.png) | ![In-Cab PWA Breadcrumb Recorder and Backtrack](docs/images/trailforge-recorder.png) |
| *Rig Suitability (`AWD`–`Modified 4WD`), Official VIC/NSW Closures & Community Gate Warnings with One-Click OSM Fixes* | *Streaming VIC & NSW Offline Regional Packs (`australian-alps`, `blue-mountains`, `vic`, `nsw`) with SHA-256 Verification* | *In-Cab PWA Breadcrumb Recorder with One-Tap Backtrack, Screen Wake Lock & Offline GPX/GeoJSON Export* |

---

## 60-Second Self-Hosted Quickstart (`docker compose up`)

Launch the full PostGIS + FastAPI + Worker + MapLibre GL JS 5 PWA stack locally on Apple Silicon (`arm64`) or Linux (`amd64`):

```bash
git clone https://github.com/jremick/trailforge.git
cd trailforge
cp .env.example .env
# For non-local / homelab deploys, set strong random secrets in .env:
#   AUTH_SECRET_KEY=$(openssl rand -hex 32)
#   TRAILFORGE_PRIVACY_SECRET=$(openssl rand -hex 32)
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

> **Anonymous-first:** Browse maps, inspect tracks, search VIC/NSW tracks/POIs/`lat,lon`/MGRS, switch Quick Rig classes (`AWD`–`Modified 4WD`), and export GPX/GeoJSON with zero sign-in. Register locally inside the **Rig & Account** drawer to save custom Vehicle Profiles, favorites, personal pins, GPX/telemetry uploads, and timestamped hazard/gate reports. Stop the stack cleanly anytime with `make down`.

---

## Demo Cookbook & CLI Examples

### One-Command API & GPX Walkthrough

Explore the complete v1 API flow against your running stack using [`examples/demo-walkthrough.sh`](examples/demo-walkthrough.sh), [`examples/davies_plain_drive.gpx`](examples/davies_plain_drive.gpx), and [`examples/telemetry_sample.json`](examples/telemetry_sample.json) (see [`examples/README.md`](examples/README.md) for details):

```bash
./examples/demo-walkthrough.sh
```

The walkthrough verifies `/api/health`, inspects corridor tracks and VIC/NSW closures, registers a local user with a `Touring 4WD` rig, checks personalized suitability, uploads a `≥ 250 m` privacy-trimmed GPX trace through the in-PostGIS HMM matcher, posts a locked-gate community report, inspects live grades & 72 h rainfall, validates the `australian-alps` pack manifest, and exports GridForge-ready GPX + GeoJSON into `./out/`.

### Pipeline & Worker CLI Commands

Every stage is scriptable headlessly inside the `api` container (zero external brokers):

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

### GridForge Companion Print Handoff (300 DPI Waterproof Backup Maps)

TrailForge deliberately omits PyQGIS so the interactive web/PWA stack stays lean; export any track, personal pins, or recorded trip as GPX 1.1 / GeoJSON and hand it straight to sibling project [GridForge](https://github.com/jremick/gridforge) for a true-scale 1:25,000 UTM/MGRS waterproof backup strip atlas:

```bash
# Export a track GPX from TrailForge (or click "⬇ GPX" in the Track Inspector / Recorder)
mkdir -p out
curl -fsSL "http://localhost:5173/api/tracks/<track_id>/export?format=gpx" -o out/davies_plain.gpx

# Render a 300 DPI true-scale UTM strip atlas + elevation profile + 100 mm calibration sheet in GridForge
docker run --rm -v "$(pwd)/out:/out" gridforge:dev \
  gridforge render --strip-route /out/davies_plain.gpx --scale 25000 --paper a4 \
    --dem-source au-elvis --au-overlay au-state-trails --slope-shading \
    --elevation-profile --calibration-page \
    --title "DAVIES PLAIN — VIC/NSW BACKUP STRIP ATLAS" \
    --output /out/davies_plain_strip_atlas.pdf
```

---

## Measured Performance, Accuracy & Memory Profile

Measured on an Apple Silicon **16 GB RAM** host running native `linux/arm64` Docker Compose (`imresamu/postgis:16-3.4`, Python `3.12`, GDAL, `osmium-tool`, Tippecanoe `2.79.0`, `go-pmtiles 1.31.2`, Nginx + MapLibre GL JS `5`):

| Metric | Measured Result | Guardrail / Target |
| :--- | :--- | :--- |
| **Combined stack idle RAM (`docker stats`, 4 healthy containers)** | **`~269 MiB`** (`268.6 MiB` total: `db` `85.1 MiB` · `api` `97.3 MiB` · `worker` `77.8 MiB` · `web` `8.5 MiB`) | 16 GB laptop / homelab friendly (zero Redis/Kafka) |
| **Offline pack builder peak Python heap** | **`51.79 MB`** (`australian-alps` PMTiles + snapshot + `bundle.tar.gz`) | `< 300 MB` streaming RAM budget |
| **Canonical segment-ID preservation (`v2` jittered re-import)** | **`100%`** (`358 exact + 86 spatial` = `444 / 444` preserved; `0` orphaned) | Deterministic identity across OSM/state updates |
| **In-PostGIS HMM map-matching confidence** | **`0.9739`** mean confidence across **`145`** Davies Plain segments | Pluggable `TrackMatcher` protocol (Valhalla-ready) |
| **Offline `PMTiles` serving (Nginx & Service Worker)** | **HTTP `206 Partial Content`** (`Accept-Ranges: bytes`, SHA-256 verified manifest) | Zero full-archive buffering in browser |
| **Automated offline test suite (`pytest` + Vitest)** | **`179` offline tests passing** (`103` Docker Python/PostGIS + `76` Vitest web) | 100% offline CI gate (`-m "not live"`) |
| **Live upstream agency smoke tests (`@pytest.mark.live`)** | **`3/3` passing** across 8 live AU agencies (CAPAD 2025, DataVic WFS `tr_road` & `paim_vm_tr_road_closures`, NSW Spatial `NSW_Transport_Theme`, GA Hydrology, VicEmergency, NSW RFS, NSW NPWS RSS 2.0, Open-Meteo) | Live upstream connector compatibility |

---

## Architecture & Testing

```text
src/trailforge/
├── config.py              # Pydantic v2 settings, privacy/memory guardrails, auth & storage paths
├── db/                    # SQLAlchemy 2.0 + GeoAlchemy2 3-tier ODbL-separated PostGIS schema
├── auth/                  # Self-hosted scrypt password hashing & HMAC-SHA256 bearer/cookie auth
├── ingestion/             # OSM/VIC/NSW/GA/CAPAD connectors, conflation, 50–100 m segmenter, DEM draping & crossings
├── grading/               # Day-1 deterministic baseline + recency/rig-weighted live engine + Open-Meteo 72h rainfall
├── feeds/                 # Pluggable VIC MapShareVic, VicEmergency, NSW RFS, NSW NPWS & OSM gate sync worker
├── vehicles/              # Deterministic AU 4WD capability classifier (AWD -> Modified 4WD) & rig suitability
├── matching/              # Mandatory >=250 m privacy trimmer, GPX/JSON parser & pluggable in-PostGIS HMM matcher
├── reports/               # Structured condition & locked-gate reports, photo validation, confirm/resolve decay
├── tiles/                 # PostGIS + GDAL + Tippecanoe + rio-rgbify -> vector & Terrain-RGB PMTiles builder
├── packs/                 # Streaming VIC/NSW regional offline pack catalog, SHA-256 manifest & bundle builder
├── export/                # GridForge-compatible GPX 1.1 & RFC 7946 GeoJSON exporters
├── api/                   # FastAPI routers (tracks, tiles, closures, auth, vehicles, trips, grading, reports, packs)
└── worker/                # Lightweight periodic background worker (feed sync, live grading, demo corridor seeder)
web/
├── src/                   # TypeScript + Vite + MapLibre GL JS 5 PWA (map, layers, search, grading, closures, reports)
│   ├── components/        # Topbar, sidebar, track inspector, rig/account drawer, offline packs manager, in-cab recorder
│   └── offline/           # IndexedDB + Cache Storage packStore, HTTP 206 range slicer, GPS recorder & deferred syncQueue
└── public/sw.js           # Offline-first Service Worker serving range-sliced PMTiles & cached pack assets
migrations/                # Alembic spatial migrations (GIST indexes, 3-tier ODbL tables)
assets/icons/              # 21 self-contained outdoor/topo/4WD SVG symbols (adapted from GridForge)
examples/                  # Runnable walkthrough script, Davies Plain GPX & sample telemetry JSON
tests/                     # 103 offline Python/PostGIS tests + deterministic synthetic VIC/NSW fixtures + 3/3 live checks
```

```bash
# Lint & formatting checks (ruff)
make lint

# Full offline Python test suite (lint + host unit + Docker PostGIS integration)
make test

# Web PWA TypeScript typecheck, production Vite build & 76 Vitest unit tests
make web-check

# Optional live upstream connector & agency feed smoke tests (network required)
docker compose exec -T api pytest -m live -v --override-ini="addopts="
```

See [`docs/PLAN.md`](docs/PLAN.md) for the vertical-slice roadmap, [`docs/SPEC_GIS_SCHEMA.md`](docs/SPEC_GIS_SCHEMA.md) for the GIS connector & 3-tier spatial schema specification, [`AGENTS.md`](AGENTS.md) for engineering invariants, and [`CONTRIBUTING.md`](CONTRIBUTING.md) for contribution guidelines.

---

## Alpha Release Status (`v1.0.0-alpha.1`) & Roadmap

- **Shipped in `v1.0.0-alpha.1`:** All **9 vertical slices** from [`docs/PLAN.md`](docs/PLAN.md) are implemented and verified end-to-end across Checkpoints A, B, and C on the VIC/NSW Australian Alps corridor — including deterministic DEM/OSM baseline grading, interactive `PMTiles` maps & alpine snow-line overlay, live VIC/NSW closure ingesters, Vehicle Profiles & rig suitability, 250 m privacy-trimmed HMM map-matching, explainable crowdsourced grading & 72 h rainfall, community condition & locked-gate reports with one-click OSM edit handoff, streaming offline regional packs, and the in-cab PWA backtrack recorder. See [`CHANGELOG.md`](CHANGELOG.md) for full release notes.
- **Next Milestones toward `v1.0.0` GA:** Full statewide Victoria & New South Wales production-scale PBF/DEM pack bakes (`vic` and `nsw`), extended live soak testing across upstream VIC/NSW government agency feeds and Open-Meteo, and hardened HTTPS reverse-proxy deployment templates.
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

TrailForge enforces strict **OSM ODbL separation** (isolating OpenStreetMap base tables from independent crowdsourced telemetry, live grades, closures, and condition reports linked only by stable canonical segment IDs) and preserves all required attributions across the web UI footer, API responses, GPX/GeoJSON exports, and offline pack manifests.
