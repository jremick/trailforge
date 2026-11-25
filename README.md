# TrailForge

**Open-source, self-hostable 4WD & off-road mapping, live track-grading, and trail-intelligence platform — launching first for Victoria & New South Wales (including ACT).**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Region: VIC & NSW](https://img.shields.io/badge/v1%20Coverage-VIC%20%26%20NSW%20%28AU%29-2ea44f)](docs/PLAN.md)
[![Sibling: GridForge](https://img.shields.io/badge/Sibling-GridForge-f59e0b)](https://github.com/jremick/gridforge)

TrailForge is an open-source alternative to proprietary off-road track-grading apps like Newtracs. It pairs **offline vector + topographic `PMTiles` maps** with a **transparent, two-stage track-grading engine** (deterministic Day-1 terrain/OSM baseline progressively refined by privacy-trimmed crowdsourced telemetry and recent rainfall context), **live official VIC & NSW closure/emergency feeds**, and **structured community trail reports**.

---

## Core Capabilities (v1 Scope — VIC & NSW)

- **Offline Vector & Topo Maps (`PMTiles` + MapLibre GL JS):** High-resolution off-road tracks, hillshade, contour lines, steep-section highlights, toggleable snow-line elevation band (>1,000 m default), and curated campsites, huts, and trig stations.
- **Canonical Multi-Source Track Network:** Conflates OpenStreetMap tracks with official Victorian (`tr_road`, `recweb_tracks`) and NSW (`NSW_Transport_Theme`) 4WD/fire trails into stable 50–100 m segments draped onto GA/DEA elevation data with automatic waterway-crossing detection.
- **Explainable Track Grading:**
  - *Day-1 Deterministic Baseline:* Surface/tracktype, max & sustained slope degrees, elevation gain, and water crossings.
  - *Crowdsourced Telemetry Layer:* Privacy-trimmed (250 m start/end buffer), anonymized GPX/telemetry traces map-matched via a pluggable in-PostGIS HMM matcher, normalized by **Vehicle Capability Class** (`AWD`, `Stock 4WD`, `Touring 4WD`, `Modified 4WD`), and contextualized with 72-hour rainfall.
- **Live VIC & NSW Closures & Incidents:** Pluggable ingesters for VIC MapShareVic seasonal road closures, VicEmergency incidents, NSW RFS major incidents, NSW NPWS alerts (joined to CAPAD parks), and OSM gate/private-access tags.
- **Structured Community Reports & Gate Warnings:** Timestamped hazard/access reports (`fallen_tree`, `bog_hole`, `washout`, `deep_water`, `locked_gate`, `private_property`, `snow_ice`, `track_clear`) with photos, confirm/resolve decay, and one-click OSM edit handoff.
- **Offline Regional Packs & In-Cab PWA Recorder:** Streaming `< 300 MB` RAM regional packs (`australian-alps`, `blue-mountains`, `vic`, `nsw`), live GPS breadcrumb/backtrack mode, offline pin dropping, deferred sync, and GPX/GeoJSON export (compatible with **GridForge** for 300 DPI waterproof backup paper maps).

---

## Architecture Overview

- **Backend:** Python 3.12 · FastAPI · Pydantic v2 · PostgreSQL + PostGIS · lightweight background worker
- **Spatial & Tile Pipeline:** GDAL · `osmium` · Tippecanoe · `rio-rgbify` → `PMTiles` (zero PyQGIS dependency)
- **Frontend:** TypeScript · Vite · MapLibre GL JS 5 offline-capable PWA
- **Deployment:** Self-hostable `docker compose up` (`arm64` & `amd64`), lean memory profile designed for 16 GB laptops and homelabs

See [`docs/PLAN.md`](docs/PLAN.md) for the full vertical-slice v1 roadmap and [`AGENTS.md`](AGENTS.md) for engineering conventions.

---

## Data Sources, Attributions & Licensing

| Dataset / Component | Scope | License & Attribution |
| :--- | :--- | :--- |
| **TrailForge Source Code** | Application, pipelines, web PWA | **GNU GPL v3.0 or later** ([LICENSE](LICENSE)) |
| **OpenStreetMap** | Roads, 4WD tracks, gates, access tags, POIs | **ODbL** — Map data © OpenStreetMap contributors (`openstreetmap.org/copyright`) |
| **Geoscience Australia / DEA** | Australia 5 m LiDAR & SRTM 1s DEM (`AHD`), Surface Hydrology | **CC BY 4.0** — © Commonwealth of Australia (Geoscience Australia / DEA) |
| **DCCEEW CAPAD 2025** | National Parks, Nature Reserves, IPAs | **CC BY 4.0** — © Commonwealth of Australia (DCCEEW) |
| **NSW Spatial Services (DCS)** | NSW fire trails, 4WD tracks, huts, POIs & trig stations | **CC BY 4.0** — © State of New South Wales (Spatial Services, DCS) |
| **Victoria DEECA / DTP Vicmap** | VIC fire/4WD tracks, recreation tracks, huts, sites & seasonal closures | **CC BY 4.0** — © State of Victoria (DEECA / DTP Vicmap) |
| **VicEmergency / NSW RFS / NSW NPWS** | Live incidents & park closure alerts | Official state agency public feeds (attributed per feed with source links) |
| **Open-Meteo** | Recent precipitation context (72 h wet/dry weighting) | **CC BY 4.0** (non-commercial API; AGPLv3 self-hostable backend) |
