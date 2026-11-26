# Changelog

All notable changes to **TrailForge** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
