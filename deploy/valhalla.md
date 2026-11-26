# Optional Valhalla Map-Matching Sidecar Guide

TrailForge ships with a lightweight, zero-broker **in-PostGIS Hidden Markov Model (HMM)** matcher (`PostGISHMMMatcher`) enabled by default (`TRAILFORGE_MATCHER_BACKEND=postgis_hmm`). This keeps the entire stack lean (`~270–350 MiB` idle RAM) for 16 GB laptops, mini-PCs, and Raspberry Pi homelabs.

If you host TrailForge on a server with spare RAM (`>= 4 GB` free) and want full OpenStreetMap road/track graph map-matching via Valhalla's `/trace_attributes` Meili engine, you can opt into the **Valhalla sidecar** without changing any database schema, ingestion pipeline, or grading code.

---

## How TrailForge Preserves ODbL & Canonical Segment Identity

Valhalla matches raw GPS traces against OpenStreetMap ways, but TrailForge **never** writes OSM way IDs or OSM geometries into Tier C telemetry or grading tables:

1. TrailForge trims the mandatory `>= 250 m` start/end privacy buffer first (`EPSG:3577`).
2. `ValhallaTrackMatcher` calls `POST ${VALHALLA_URL}/trace_attributes` with off-road track costing (`costing="auto"`, `use_tracks=1.0`, `use_ferry=0.5`, `shape_match="map_snap"`).
3. TrailForge decodes Valhalla's Polyline6 matched shape and projects matched subtraces onto TrailForge's canonical 50–100 m `track_segments` (`tfseg_*`) in PostGIS.
4. Any Valhalla-unmatched or off-network spur (`>= 100 m`) is emitted cleanly to `unmapped_track_candidates`.
5. Public `telemetry_observations` rows continue linking exclusively via canonical `segment_id` and revocable keyed-HMAC `contribution_hash`.

---

## Starting TrailForge with the Valhalla Sidecar

Validate the merged Compose config first:

```bash
docker compose -f docker-compose.yml -f deploy/valhalla-compose.override.yml config -q
```

### Option A — Auto-Download Geofabrik Australia PBF on First Boot

```bash
VALHALLA_TILE_URLS=https://download.geofabrik.de/australia-oceania/australia-latest.osm.pbf \
docker compose -f docker-compose.yml -f deploy/valhalla-compose.override.yml up -d
```

For prebuilt GHCR images (plus optional Traefik HTTPS):

```bash
VALHALLA_TILE_URLS=https://download.geofabrik.de/australia-oceania/australia-latest.osm.pbf \
docker compose -f docker-compose.ghcr.yml -f deploy/valhalla-compose.override.yml up -d
```

### Option B — Use a Pre-Extracted Regional PBF (Faster & Lower RAM)

Extract a regional corridor (e.g. VIC/NSW Alps) with `osmium` and copy it into the `trailforge_valhalla_tiles` volume before starting `valhalla`:

```bash
docker volume create trailforge_valhalla_tiles
docker run --rm -v trailforge_valhalla_tiles:/custom_files -v "$PWD":/src alpine \
  cp /src/alps-corridor.osm.pbf /custom_files/alps-corridor.osm.pbf

docker compose -f docker-compose.yml -f deploy/valhalla-compose.override.yml up -d
```

---

## Configuration & Graceful Fallback

| Variable | Default (Core Stack) | Default (Valhalla Override) | Description |
| :--- | :--- | :--- | :--- |
| `TRAILFORGE_MATCHER_BACKEND` | `postgis_hmm` | `valhalla` | Selects `PostGISHMMMatcher` or `ValhallaTrackMatcher`. |
| `VALHALLA_URL` | `http://valhalla:8002` | `http://valhalla:8002` | Base URL of the Valhalla HTTP service. |
| `VALHALLA_TIMEOUT_S` | `10.0` | `10.0` | HTTP timeout (seconds) for `/trace_attributes`. |
| `VALHALLA_FALLBACK_TO_HMM` | `true` | `true` | Automatically fall back to `PostGISHMMMatcher` on timeout, 5xx, or connection error. |
| `VALHALLA_MEM_LIMIT` | *(n/a)* | `2g` | Container memory cap for the Valhalla sidecar (`4g` recommended for full Australia tile builds). |

Because `api` depends on `valhalla` with `condition: service_started` and `VALHALLA_FALLBACK_TO_HMM=true`, TrailForge stays 100% available while Valhalla builds routing tiles on first boot or restarts:

- When Valhalla answers `200 OK`: trip upload and `/api/trips/mobile-sync` report `matcher_backend: "valhalla"`.
- When Valhalla is warming up, down, or times out: TrailForge logs a structured warning, falls back seamlessly to `PostGISHMMMatcher`, and reports `matcher_backend: "postgis_hmm_fallback"`.

---

## Verification

Check Valhalla tile status from inside the Compose network:

```bash
docker compose -f docker-compose.yml -f deploy/valhalla-compose.override.yml \
  exec -T valhalla curl -fsS http://127.0.0.1:8002/status
```

Verify `api` health and inspect `matcher_backend` on a test upload to `POST /api/trips/upload` or `POST /api/trips/mobile-sync`.
