# Contributing to TrailForge

Thank you for contributing to **TrailForge**! TrailForge is an open-source, self-hostable 4WD & off-road mapping, live track-grading, and trail-intelligence platform licensed under **GNU GPL v3.0 or later** (`GPL-3.0-or-later`).

---

## Development Environment

TrailForge targets **Python 3.12+**, **PostgreSQL 16 + PostGIS 3.4**, **GDAL / Tippecanoe / rio-rgbify**, and a **TypeScript + Vite + MapLibre GL JS 5** PWA frontend (`web/`).

### Quick Start

```bash
cp .env.example .env
docker compose up -d --build --wait
make seed-corridor
```

For local host Python tooling (linting and fast unit tests without Docker):

```bash
uv sync --group dev
```

For frontend development:

```bash
cd web
npm ci
npm run dev
```

---

## Offline CI & Verification Commands

All automated CI tests **must pass 100% offline** using the compact synthetic VIC/NSW fixtures under `tests/fixtures/` (an autouse pytest socket guard blocks outbound external hosts during offline runs).

```bash
# Python lint (ruff)
make lint
# equivalent: uv run ruff check src tests migrations

# Fast offline unit tests on host (no DB required)
make test-unit
# equivalent: uv run pytest -m "not live and not integration"

# Full offline unit + PostGIS integration tests inside Docker API container
make test-integration
# equivalent: docker compose exec -T api pytest -m "not live"

# All Python checks (lint + unit + PostGIS integration)
make test

# Verify Alembic schema matches SQLAlchemy models
docker compose exec -T api alembic check

# Frontend typecheck, production Vite build, and Vitest suite
make web-check
# equivalent: cd web && npm run typecheck && npm run build && npm test
```

### Optional Live Upstream Smoke Tests

Tests that contact real Australian government or weather APIs (CAPAD, DataVic WFS, NSW Spatial Services, GA Hydrology, VicEmergency, NSW RFS, NSW NPWS, Open-Meteo) **must** be marked with `@pytest.mark.live` so they are excluded from offline CI:

```bash
uv run pytest -m live -v -o addopts=
# or inside the Docker API container:
docker compose exec -T api pytest -m live -v -o addopts=
```

---

## Non-Negotiable Architecture & Data Hygiene Rules

1. **Strict OSM ODbL Separation:**
   - Keep OpenStreetMap geometries and tags isolated in dedicated source/base tables (`source_osm_ways` / base ingestion layer) and keep crowdsourced telemetry (`telemetry_observations`), computed difficulty ratings (`track_segment_grades`), closures (`closure_alerts`), and user condition reports (`condition_reports`) in independent relational/PostGIS tables linked only by stable `segment_id` foreign keys or spatial joins.
   - **Never** strip required dataset attributions: OpenStreetMap (`ODbL`), Geoscience Australia / DEA / DCCEEW CAPAD / NSW Spatial Services / DEECA DataVic (`CC BY 4.0`), and Open-Meteo (`CC BY 4.0`). Do not introduce `CC BY-NC` or proprietary scraped datasets.
2. **Zero PyQGIS (`qgis.core`):**
   - TrailForge serves interactive vector and terrain `PMTiles` to MapLibre GL JS and offline PWA storage. Leave 300 DPI print layout rendering to sibling project **GridForge** (maintain clean GPX 1.1 and GeoJSON exports instead).
3. **Stable Canonical Segment Identity:**
   - Canonical 50–100 m segments (`track_segments.segment_id`) must preserve deterministic IDs across OSM/state snapshot re-imports for unchanged geometries so historical telemetry, closures, and condition reports are never orphaned.
4. **Privacy by Design:**
   - Telemetry ingestion must strictly enforce automatic start/end buffer trimming (`>= 250 m` minimum, never disableable via API), store **zero** `user_id` or `trip_id` columns on public telemetry tables, support private-only trip logs, and allow instant HMAC-based revocation when switching a trip back to private.
5. **Pluggable Map-Matching:**
   - Keep map-matching behind the `TrackMatcher` protocol (`src/trailforge/matching/`) so external engines (e.g. Valhalla or OSRM) can plug in without altering ingestion, privacy trimming, or grading logic.
6. **Explainable Grading:**
   - Never surface an opaque difficulty score. Always expose both static deterministic baseline factors (surface/tracktype, slope degrees, water crossings, `4wd_only`) and live telemetry evidence (sample count, freshness, vehicle-class split, 72 h rainfall context).
7. **16 GB Laptop / Homelab Memory Guardrails:**
   - Keep idle Docker Compose memory lean, stream large PBF/DEM/pack operations disk-to-disk (`< 300 MB` peak RAM where practical), and avoid adding Redis, Kafka, or unnecessary broker services.

---

## Pull Request Checklist

Before opening a pull request:

1. `make lint`, `make test`, and `make web-check` all pass 100% offline.
2. Any database schema change includes a clean Alembic migration verified with `docker compose exec -T api alembic check` and `alembic upgrade head`.
3. New upstream API integrations include offline unit tests with mocked HTTP payloads and mark any live network checks with `@pytest.mark.live`.
4. Commit messages follow **Conventional Commits** (`feat(...)`, `fix(...)`, `docs(...)`, `test(...)`, `refactor(...)`, `chore(...)`).
5. All new code is compatible with `GPL-3.0-or-later`.
