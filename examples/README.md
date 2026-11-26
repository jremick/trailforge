# TrailForge Demo & Examples

Reproducible, 100% offline-friendly examples for exercising TrailForge's full Australian Alps corridor stack (PostGIS + FastAPI + PMTiles + MapLibre GL JS PWA) and handing off exported routes to sibling project **GridForge** for 300 DPI waterproof backup paper maps.

---

## Included Files

| File | Description |
| --- | --- |
| [`demo-walkthrough.sh`](demo-walkthrough.sh) | End-to-end executable walkthrough against a running TrailForge instance (`health`, track inspector, VIC/NSW closures, self-hosted auth + Touring 4WD rig suitability, 250 m privacy-trimmed GPX & Telemetry JSON HMM uploads, locked-gate community report + OSM fix links, live grading & 72 h rainfall, offline pack manifest, and GPX/GeoJSON export to `./out/`). |
| [`davies_plain_drive.gpx`](davies_plain_drive.gpx) | Realistic Davies Plain Traverse GPX 1.1 trace (Tom Groggin to Davies Plain Hut, 120 timestamped elevation points along the VIC/NSW Australian Alps corridor). Ready to upload via the Web UI **Account -> Trips** tab or `POST /api/trips/upload`. |
| [`telemetry_sample.json`](telemetry_sample.json) | Ready-to-upload JSON telemetry payload for `POST /api/trips/upload` (`Content-Type: application/json`) containing GPS coordinates, elevation, timestamps, speed (`speed_mps`), and synthetic IMU vibration roughness (`roughness_rms`) peaking along the steep Davies Plain escarpment climb. |

---

## Quick Start: Run the Full Walkthrough

### 1. Start and Seed TrailForge

```bash
cp .env.example .env
docker compose up -d --build --wait
make seed-corridor
```

### 2. Execute the End-to-End Walkthrough

```bash
./examples/demo-walkthrough.sh
```

Override the target URL or output directory if TrailForge runs on another host/port (for example via SSH port forwarding):

```bash
BASE_URL=http://localhost:5173 OUT_DIR=./out ./examples/demo-walkthrough.sh
```

Requires only `bash`, `curl`, and `python3` (no `jq` needed).

---

## Uploading Sample Traces via `curl`

After signing in and setting `TOKEN` (from `POST /api/auth/login` or `POST /api/auth/register`):

### Upload GPX as an Anonymous Community Contribution (250 m Privacy Trim)

```bash
curl -fsS -X POST "http://localhost:5173/api/trips/upload" \
  -H "Authorization: Bearer ${TOKEN}" \
  -F "file=@examples/davies_plain_drive.gpx" \
  -F "name=Davies Plain Community Traverse" \
  -F "visibility=community_anonymous" \
  -F "privacy_trim_m=250"
```

### Upload Telemetry JSON (with IMU `roughness_rms`) as a Private Logbook Entry

```bash
curl -fsS -X POST "http://localhost:5173/api/trips/upload" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary "@examples/telemetry_sample.json"
```

---

## Companion Workflow: Print Exported Tracks in GridForge

`demo-walkthrough.sh` exports GridForge-compatible GPX 1.1 (`out/davies_plain_track.gpx`) and GeoJSON (`out/davies_plain_track.geojson`) files containing track geometry and nearby huts/campgrounds/gates as waypoints.

Pass the exported GPX directly to **GridForge** for a true-scale 1:25,000 UTM multi-sheet waterproof strip atlas with elevation profile and calibration ruler:

```bash
gridforge render \
  --strip-route out/davies_plain_track.gpx \
  --elevation-profile \
  --calibration-page \
  --scale 25000 \
  --paper a4 \
  --dem-source au-elvis \
  --au-overlay au-state-trails \
  --au-overlay au-hydro \
  --au-overlay au-parks \
  --output out/davies_plain_strip_atlas.pdf
```
