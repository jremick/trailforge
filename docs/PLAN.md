# TrailForge v1 Roadmap (Option B — Victoria & New South Wales)

## Overview
- **Goal:** Ship a self-hostable, open-source 4WD & off-road mapping, live track-grading, and trail-intelligence platform covering **Victoria & New South Wales (including ACT)**.
- **License:** `GPL-3.0-or-later`
- **Map-Matching Strategy:** Lightweight in-PostGIS Hidden Markov Model (HMM) matcher behind a pluggable matcher interface (Valhalla-ready for future deployments).
- **Riskiest Slice:** **Slice 02 — Canonical Track Network & Baseline Grading Pipeline**. Conflating OSM + VIC (`tr_road`, `recweb_tracks`) + NSW (`NSW_Transport_Theme`) into deduplicated 50–100 m segments with stable deterministic IDs, accurate DEM slope draping, and waterway-crossing attribution underpins every downstream feature (map-matching, telemetry aggregation, closures, reports, offline packs). Prove it first on a compact VIC/NSW High Country / Australian Alps border corridor before scaling statewide.
- **Ready Frontier:** **Slice 01** (unblocks **Slice 02** and **Slice 05** in parallel).

---

## Ordered Vertical Slices

### `01`: Bootstrap Spatial Stack & Test Harness
- **What to build:** Self-hostable Docker Compose stack (PostGIS, FastAPI API, background worker, Vite + MapLibre GL JS PWA shell), spatial database migrations with GIST indexes, and an offline synthetic test fixture harness so CI runs with zero network access.
- **Blocked by:** None (can start immediately)
- **Acceptance criteria:**
  - [ ] Single `docker compose up` starts PostGIS, API, worker, and web UI with a passing `/api/health` endpoint and lean idle memory guardrails.
  - [ ] Migrations create versioned spatial schemas for tracks, track segments, POIs, closures, vehicle profiles, trips, telemetry observations, and condition reports.
  - [ ] Automated unit and integration test suites pass completely offline against synthetic VIC/NSW fixtures.

### `02`: Canonical Track Network & Baseline Grading Pipeline *(Riskiest Slice)*
- **What to build:** Ingestion pipeline pulling OSM tracks plus VIC and NSW state 4WD/fire trails, conflating overlapping ways into canonical 50–100 m track segments with stable IDs, draping segments onto GA/DEA elevation data for gradient/relief, intersecting GA/OSM waterways for creek crossings, and computing an explainable Day-1 difficulty rating (`Easy` / `Medium` / `Hard` / `Extreme`).
- **Blocked by:** `01`
- **Acceptance criteria:**
  - [ ] Running ingestion on a VIC/NSW High Country corridor produces deduplicated 50–100 m segments with length, elevation profile, max/sustained grade, waterway-crossing count, and baseline difficulty.
  - [ ] Re-ingesting updated OSM/state snapshots preserves segment IDs for unchanged geometries so attached telemetry and reports are not orphaned.
  - [ ] Every graded segment exposes a structured breakdown of contributing factors (surface/tracktype, max slope degrees, crossings, `4wd_only` / access tags).

### `03`: Interactive Vector Map & Track Inspector
- **What to build:** `PMTiles` generation pipeline (graded tracks, steep-section highlights, POIs/camps/huts/trigs, terrain RGB hillshade/contours, configurable snow-line elevation band) and the MapLibre GL JS frontend with layer toggles, search, and a Track Inspector drawer.
- **Blocked by:** `02`
- **Acceptance criteria:**
  - [ ] Map renders color-coded difficulty tracks, highlighted steep grades, topo contours/hillshade, and toggleable snow-line (>1,000 m default) smoothly in the browser.
  - [ ] Selecting any track opens an inspector displaying distance, elevation profile, explainable grade breakdown, water crossings, POIs, and CAPAD park/forest tenure.
  - [ ] Search resolves VIC/NSW track names, camps, huts, `lat,lon`, and MGRS coordinates.

> **Checkpoint A (after `03`):** Demoable Day-1 off-road topo mapper for the VIC/NSW High Country — all offline tests pass, build is clean, and deterministic DEM/OSM grading is verified visually and numerically.

### `04`: Live VIC & NSW Closures and Incident Feeds
- **What to build:** Pluggable feed sync worker for VIC seasonal road closures (MapShareVic), VicEmergency incidents, NSW RFS major incidents, NSW NPWS park alerts (joined to CAPAD boundaries), plus OSM `access=private` / `barrier=gate` flags, propagating closure/warning status onto affected track segments.
- **Blocked by:** `02`
- **Acceptance criteria:**
  - [ ] Sync worker ingests VIC and NSW feeds into normalized closure/alert records linked to affected segments or park areas, preserving source URL and last-updated timestamp.
  - [ ] Closed, seasonally restricted, or gated tracks render with clear warning styling and official closure details in the Track Inspector.
  - [ ] Upstream agency outages fail gracefully, retaining last-known closure state with a visible staleness indicator.

### `05`: Vehicle Profiles & User Accounts
- **What to build:** Lightweight self-hostable auth (anonymous read-only browsing + optional signed-in accounts) and customizable Vehicle Profiles (drivetrain, lift/clearance, tire class `HT`/`AT`/`MT`, lockers, winch) classified into normalized Vehicle Capability Classes (`AWD`, `Stock 4WD`, `Touring 4WD`, `Modified 4WD`), plus saved favorites and pins.
- **Blocked by:** `01` *(can run in parallel with `02`–`04`)*
- **Acceptance criteria:**
  - [ ] Users can browse without an account or sign in to create, edit, and switch between Vehicle Profiles with deterministic capability classification.
  - [ ] Active Vehicle Profile displays personalized suitability guidance ("Suited", "Challenging", "High Risk for your rig") on tracks.
  - [ ] Saved favorite tracks and personal pins persist across sessions and export to GPX/GeoJSON.

### `06`: Privacy-Preserving Telemetry Ingestion & Map-Matching
- **What to build:** GPX and telemetry JSON upload API (GPS trace + optional IMU/accelerometer roughness + Vehicle Profile) enforcing automatic start/end privacy buffer trimming (default 250 m), stripping user identity from public aggregates, and snapping traces to canonical track segments via a pluggable in-PostGIS HMM matcher.
- **Blocked by:** `02`, `05`
- **Acceptance criteria:**
  - [ ] Uploaded GPX/telemetry traces trim start/end privacy zones and map-match onto canonical segments with confidence scores, recording anonymized speed, duration, roughness, vehicle class, and timestamp.
  - [ ] Traces that do not match known segments are stored separately as unmapped-track candidates rather than corrupting nearby segments.
  - [ ] Users can toggle any trip between private logbook only and anonymous community contribution.

> **Checkpoint B (after `06`):** Live official VIC/NSW closures, vehicle profiles, and real GPX/telemetry ingestion + privacy trimming verified end-to-end with green CI.

### `07`: Explainable Crowdsourced Grading & Rainfall Engine
- **What to build:** Dynamic aggregation job combining deterministic DEM/OSM baseline grades with recency-weighted crowdsourced segment traversals (normalized by Vehicle Capability Class) and recent 72-hour rainfall (Open-Meteo) to compute live difficulty, average completion time/speed, last-driven date, traffic split by vehicle class, and rough/slow obstacle hotspots.
- **Blocked by:** `06`
- **Acceptance criteria:**
  - [ ] Segments with community traversals surface live grade, confidence level, last-driven date, average speed/duration, vehicle-class traffic split, and slow/rough obstacle markers.
  - [ ] Recent rainfall adjusts wet-condition risk warnings transparently and prevents single outlier traversals from skewing ratings.
  - [ ] Track Inspector clearly explains both the static terrain baseline and live telemetry evidence behind the rating.

### `08`: Community Condition Reports & Gate Warnings
- **What to build:** Structured, timestamped trail condition reports (`fallen_tree`, `bog_hole`, `washout`, `deep_water`, `locked_gate`, `private_property`, `snow_ice`, `track_clear`) with optional photo attachments, vehicle-driven context, confirmation/resolved voting, automatic staleness decay, and one-click links to submit permanent gate/access fixes back to OpenStreetMap.
- **Blocked by:** `02`, `05`
- **Acceptance criteria:**
  - [ ] Signed-in users can submit structured hazard/gate reports with optional photos pinned to a track or coordinate, visible on the map and in the Track Inspector timeline.
  - [ ] Users can confirm or mark hazards resolved, dynamically decaying stale reports.
  - [ ] Locked-gate and private-property warnings surface distinctly alongside official closures during route inspection.

### `09`: Offline VIC & NSW Packs & In-Cab PWA Breadcrumb Recorder
- **What to build:** Streaming regional offline pack builder (`australian-alps`, `blue-mountains`, `vic`, `nsw`) outputting browser-downloadable offline bundles (vector `PMTiles` + terrain `PMTiles` + offline track/POI/closure/report snapshot), paired with a PWA in-cab trip recorder (live GPS breadcrumb trail, one-tap backtrack, Screen Wake Lock, optional Web DeviceMotion roughness capture, offline pins, and deferred sync on reconnect), plus GPX/GeoJSON export (compatible with GridForge print maps).
- **Blocked by:** `03`, `04`, `07`, `08`
- **Acceptance criteria:**
  - [ ] Users can download a regional offline pack and browse maps, inspect graded tracks, search POIs, and drop pins completely offline.
  - [ ] Trip recorder captures live GPS breadcrumbs with backtrack guidance, queues completed trips and hazard reports offline, and syncs automatically when online.
  - [ ] Planned routes and recorded trips export cleanly as GPX/GeoJSON (compatible with GridForge's strip-route/overlay workflow).

> **Checkpoint C (after `09` — v1 Release Gate):** Statewide VIC + NSW pack builds succeed within laptop/homelab memory guardrails, offline PWA mode verified end-to-end, full attribution & licensing audit complete, all tests green.

---

## Out of Scope for v1 (Planned v1.1+ Follow-Ons)
- Native mobile shell (Capacitor / React Native) for locked-screen iOS background GPS + high-rate IMU telemetry recording.
- Apple CarPlay / Android Auto projection.
- Expansion beyond VIC / NSW / ACT to QLD, WA, SA, TAS, NT, and international regions.
- Optional Valhalla map-matching sidecar plugin for dense mixed urban/rural deployments.
