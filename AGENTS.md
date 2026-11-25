# AGENTS.md — TrailForge Engineering Guide

## Project Mission
TrailForge is an open-source, self-hostable 4WD & off-road mapping, live track-grading, and trail-intelligence platform (an OSS alternative to Newtracs), launching first for **Victoria & New South Wales (including ACT)**.

## License & Data Hygiene
- **Code License:** `GPL-3.0-or-later` (matching sibling project GridForge).
- **OSM ODbL Separation:** Keep OpenStreetMap geometries/tags in dedicated source/base tables and keep crowdsourced telemetry, computed difficulty ratings, closures, and user condition reports in independent relational/PostGIS tables linked by stable segment IDs or spatial joins. Never strip required attributions (OSM ODbL, GA/DEA/CAPAD/NSW Spatial/DEECA CC BY 4.0, Open-Meteo CC BY 4.0).

## Read-Only Reference Repo (`GridForge`)
Sibling repo GridForge lives at `/Users/jarel/projects/oss-mapper`. **Treat `/Users/jarel/projects/oss-mapper` as strictly READ-ONLY** — never modify files inside it.

Reuse and adapt its verified Australian patterns rather than reinventing from scratch:
- `src/gridforge/sources/overlays_au_states.py` — NSW Spatial Services (`NSW_Transport_Theme` layers 5 & 9, `SurveyMarkGDA2020`, `NSW_POI`) and VIC DataVic/Vicmap WFS (`tr_road`, `recweb_tracks`, `recweb_hut`, `recweb_site`, `foi_point`, `geomark_point`).
- `src/gridforge/sources/overlays_au.py` — GA Surface Hydrology (`GAHydroSource`), DCCEEW CAPAD 2025 (`CAPADParksSource`), and paginated ArcGIS/WFS caching helpers.
- `src/gridforge/sources/dem_au_elvis.py` & `src/gridforge/pipeline/terrain.py` — GA 5 m LiDAR WCS + DEA SRTM 1s COG fetching (including coastal-water vs. missing-LiDAR cliff guard) and GDAL slope/hillshade/contour workflows.
- `src/gridforge/packs/catalog.py`, `builder.py`, `sources/osm_pbf.py` — VIC/NSW/ACT/High Country/Blue Mountains bounding boxes and `< 300 MB` RAM streaming pack builder.
- `src/gridforge/sources/overlay_user.py` — OGR-backed GPX/GeoJSON/CSV parsing.
- `assets/icons/` & `web/src/map.ts`, `presets.ts` — 18 outdoor SVG symbols, MapLibre GL JS 5 patterns, coordinate/MGRS parsers, and curated VIC/NSW presets.

## Hard Architectural Decisions (Locked for v1)
1. **No PyQGIS (`qgis.core`):** TrailForge serves interactive vector/terrain `PMTiles` to MapLibre GL JS and offline PWA storage. Leave 300 DPI print layout rendering to GridForge (support clean GPX/GeoJSON export to GridForge instead).
2. **Stack:** Python 3.12 + FastAPI + Pydantic v2 + PostgreSQL/PostGIS + GDAL/`osmium`/Tippecanoe/`rio-rgbify` + TypeScript/Vite/MapLibre GL JS 5 PWA + multi-arch (`arm64`/`amd64`) Docker Compose.
3. **Map-Matching:** Implement a lightweight **in-PostGIS Hidden Markov Model (HMM) matcher** behind a clean pluggable `TrackMatcher` protocol/interface so Valhalla or OSRM can plug in later without changing ingestion or grading logic.
4. **Stable Canonical Segment Identity (Slice 02 invariant):** Canonical 50–100 m track segments must preserve deterministic IDs across OSM/state re-imports for unchanged geometries so historical telemetry, closures, and condition reports remain attached.
5. **Privacy by Design:** Telemetry ingestion must enforce automatic start/end buffer trimming (default 250 m), strip user identity from all public segment aggregates, and support private-only trip logs alongside opt-in anonymous community contributions.
6. **Explainable Grading:** Never surface an opaque difficulty score. Every segment grade must expose both its deterministic baseline factors (surface/tracktype, slope degrees, waterway crossings) and live telemetry evidence (sample count, freshness, vehicle-class split, 72 h rainfall context).
7. **Homelab & Laptop Guardrails:** Keep idle memory lean (target 16 GB laptop / homelab hosts via `docker compose up`), stream large PBF/DEM operations disk-to-disk (`< 300 MB` peak RAM where practical), and avoid unnecessary broker services (no Redis/Kafka unless proven necessary).

## Testing & Verification Discipline
- All unit and integration CI tests must run **100% offline** using compact synthetic VIC/NSW fixtures under `tests/fixtures/`.
- Mark live upstream network checks (Overpass, DataVic WFS, NSW ArcGIS, DEA S3, MapShareVic, VicEmergency, NSW RFS/NPWS, Open-Meteo) with `@pytest.mark.live`.
- Follow the vertical-slice roadmap in `docs/PLAN.md` (and local `.jstack/plans/v1-roadmap/`). Prove every slice end-to-end and keep CI green before moving to the next slice, especially Checkpoints A, B, and C.
