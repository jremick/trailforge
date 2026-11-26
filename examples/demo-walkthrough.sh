#!/usr/bin/env bash
# TrailForge End-to-End Demo Walkthrough (v1.0.0)
#
# Exercises a running, corridor-seeded TrailForge stack without requiring `jq`:
#   1. Health & PostGIS/Alembic readiness check
#   2. Australian Alps corridor track listing & explainable Track Inspector breakdown
#   3. Official VIC & NSW closures + feed health status
#   4. Self-hosted account registration, Touring 4WD vehicle profile & rig suitability check
#   5. GPX upload (250 m privacy trim + HMM match + anonymous community contribution)
#      and Telemetry JSON upload (IMU roughness_rms + private logbook mode)
#   6. Community locked-gate report submission + one-click OSM access fix links
#   7. Live crowdsourced grading recompute & 72h rainfall context inspection
#   8. Offline regional pack catalog & SHA-256 manifest inspection
#   9. GridForge-compatible GPX 1.1 & GeoJSON export to ./out/
#
# Prerequisites:
#   docker compose up -d --build --wait
#   make seed-corridor
#
# Usage:
#   ./examples/demo-walkthrough.sh
#   BASE_URL=http://localhost:8000 ./examples/demo-walkthrough.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BASE_URL="${BASE_URL:-http://localhost:5173}"
BASE_URL="${BASE_URL%/}"
OUT_DIR="${OUT_DIR:-${REPO_ROOT}/out}"
TMP_DIR="$(mktemp -d)"

trap 'rm -rf "${TMP_DIR}"' EXIT
mkdir -p "${OUT_DIR}"

api_get_to() {
  local path="$1"
  local dest="$2"
  shift 2
  curl -fsS --connect-timeout 5 --max-time 30 "$@" "${BASE_URL}${path}" -o "${dest}"
}

api_post_json_to() {
  local path="$1"
  local body="$2"
  local dest="$3"
  shift 3
  curl -fsS --connect-timeout 5 --max-time 30 \
    -X POST \
    -H "Content-Type: application/json" \
    "$@" \
    -d "${body}" \
    "${BASE_URL}${path}" \
    -o "${dest}"
}

echo "======================================================================"
echo " TrailForge End-to-End Demo Walkthrough"
echo " Target : ${BASE_URL}"
echo " Output : ${OUT_DIR}"
echo "======================================================================"

# ------------------------------------------------------------------------------
# 1. Health Check
# ------------------------------------------------------------------------------
echo ""
echo "==> [1/9] API, PostGIS & Alembic Health Check"
if ! api_get_to "/api/health" "${TMP_DIR}/health.json"; then
  echo "ERROR: Could not reach ${BASE_URL}/api/health" >&2
  echo "Hint : Start and seed TrailForge with:" >&2
  echo "         docker compose up -d --build --wait && make seed-corridor" >&2
  echo "       Or override BASE_URL (e.g. BASE_URL=http://localhost:8000)." >&2
  exit 1
fi

python3 - "${TMP_DIR}/health.json" <<'PY'
import json, sys
from pathlib import Path
h = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(f"  status          : {h['status']} (db_ok={h['db_ok']})")
print(f"  service/version : {h['service']} v{h['version']}")
print(f"  postgis         : {h.get('postgis_version')}")
print(f"  schema_revision : {h.get('schema_revision')}")
assert h["status"] == "ok" and h["db_ok"] is True, "Backend health check not ok"
PY

# ------------------------------------------------------------------------------
# 2. Corridor Track Listing & Track Inspector Breakdown
# ------------------------------------------------------------------------------
echo ""
echo "==> [2/9] Australian Alps Corridor Tracks & Explainable Track Inspector"
api_get_to "/api/tracks" "${TMP_DIR}/tracks.json"
DAVIES_ID="$(python3 - "${TMP_DIR}/tracks.json" <<'PY'
import json, sys
from pathlib import Path
tracks = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert len(tracks) > 0, "No seeded tracks found — run `make seed-corridor` first."
print(f"  seeded corridor tracks: {len(tracks)}", file=sys.stderr)
chosen = None
for t in tracks:
    print(
        f"    - {t['name']} [{t['state']}] | grade={t.get('effective_grade')} "
        f"| {t['total_length_m']/1000:.2f} km | closure={t.get('closure_status')}",
        file=sys.stderr,
    )
    if "davies plain track lower" in t["name"].lower():
        chosen = t["track_id"]
if chosen is None:
    for t in tracks:
        if "davies plain" in t["name"].lower():
            chosen = t["track_id"]
            break
print(chosen or tracks[0]["track_id"])
PY
)"

api_get_to "/api/tracks/${DAVIES_ID}" "${TMP_DIR}/inspect.json"
python3 - "${TMP_DIR}/inspect.json" <<'PY'
import json, sys
from pathlib import Path
t = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(f"  inspected track : {t['name']} ({t['track_id']})")
print(f"  length / climb  : {t['total_length_m']/1000:.2f} km | +{t['elev_gain_m']:.0f} m | max slope {t.get('max_slope_deg')} deg")
print(f"  effective grade : {t.get('effective_grade')} (live={t.get('live_grade')}, samples={t.get('sample_count')}, confidence={t.get('confidence_level')})")
print(f"  water crossings : {t.get('waterway_crossing_count')} | nearby POIs: {len(t.get('nearby_pois', []))}")
park = (t.get("primary_park") or {}).get("name") or t.get("primary_park_name") or "Unassigned"
print(f"  CAPAD tenure    : {park}")
bd = t.get("baseline_breakdown") or {}
if bd:
    print(f"  baseline factors: {json.dumps(bd)[:160]}")
PY

# ------------------------------------------------------------------------------
# 3. Official VIC & NSW Closures & Feed Status
# ------------------------------------------------------------------------------
echo ""
echo "==> [3/9] VIC & NSW Closures and Agency Feed Health"
api_get_to "/api/closures/status" "${TMP_DIR}/closures_status.json"
api_get_to "/api/closures?active_only=true" "${TMP_DIR}/closures.json"
python3 - "${TMP_DIR}/closures_status.json" "${TMP_DIR}/closures.json" <<'PY'
import json, sys
from pathlib import Path
st = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
cl = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
print(f"  feed health     : {st.get('overall_status')} ({len(st.get('feeds', []))} configured feeds)")
items = cl.get("items", [])
print(f"  active closures : {len(items)}")
for c in items[:5]:
    print(f"    - [{c.get('state')}/{c.get('closure_type')}/{c.get('severity')}] {c.get('title')}")
PY

# ------------------------------------------------------------------------------
# 4. User Registration + Touring 4WD Vehicle Profile + Rig Suitability Check
# ------------------------------------------------------------------------------
echo ""
echo "==> [4/9] Self-Hosted Auth, Touring 4WD Vehicle Profile & Rig Suitability"
DEMO_TS="$(date +%s)-$$"
DEMO_EMAIL="demo-${DEMO_TS}@trailforge.local"
REGISTER_PAYLOAD="$(python3 -c 'import json,sys; print(json.dumps({"email": sys.argv[1], "display_name": "Alpine Demo Driver", "password": "TrailForge-Demo-2026!"}))' "${DEMO_EMAIL}")"
api_post_json_to "/api/auth/register" "${REGISTER_PAYLOAD}" "${TMP_DIR}/auth.json"
TOKEN="$(python3 -c 'import json,sys; from pathlib import Path; print(json.loads(Path(sys.argv[1]).read_text())["access_token"])' "${TMP_DIR}/auth.json")"
echo "  registered user : ${DEMO_EMAIL}"

VEHICLE_PAYLOAD='{
  "name": "Troopy Touring Rig",
  "drivetrain": "4wd_part_time",
  "low_range": true,
  "lift_mm": 50,
  "clearance_mm": 245,
  "tire_class": "AT",
  "front_locker": false,
  "rear_locker": true,
  "has_winch": true,
  "has_snorkel": true,
  "is_active": true
}'
api_post_json_to "/api/vehicles" "${VEHICLE_PAYLOAD}" "${TMP_DIR}/vehicle.json" -H "Authorization: Bearer ${TOKEN}"
VEHICLE_ID="$(python3 - "${TMP_DIR}/vehicle.json" <<'PY'
import json, sys
from pathlib import Path
v = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(f"  vehicle profile : {v['name']} -> class={v['capability_class']} (active={v['is_active']})", file=sys.stderr)
assert v["capability_class"] == "Touring 4WD", f"Unexpected class: {v['capability_class']}"
print(v["id"])
PY
)"

api_get_to "/api/tracks/${DAVIES_ID}" "${TMP_DIR}/suitability.json" -H "Authorization: Bearer ${TOKEN}"
python3 - "${TMP_DIR}/suitability.json" <<'PY'
import json, sys
from pathlib import Path
t = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
suit = t.get("suitability") or {}
print(f"  rig suitability : {suit.get('rating')} ({suit.get('vehicle_name') or suit.get('vehicle_class')})")
print(f"  guidance        : {suit.get('reason')}")
for w in (suit.get("warnings") or [])[:3]:
    print(f"    ! {w}")
assert suit.get("rating"), "Expected personalized suitability guidance"
PY

# ------------------------------------------------------------------------------
# 5. GPX Upload (250 m Privacy Trim + HMM) & Telemetry JSON Upload (Private Mode)
# ------------------------------------------------------------------------------
echo ""
echo "==> [5/9] GPX Upload (250 m Privacy Trim + HMM Match) & Private Telemetry JSON"
curl -fsS --connect-timeout 5 --max-time 60 \
  -X POST "${BASE_URL}/api/trips/upload" \
  -H "Authorization: Bearer ${TOKEN}" \
  -F "file=@${SCRIPT_DIR}/davies_plain_drive.gpx;type=application/gpx+xml" \
  -F "name=Davies Plain Demo Traverse (GPX)" \
  -F "vehicle_profile_id=${VEHICLE_ID}" \
  -F "visibility=community_anonymous" \
  -F "privacy_trim_m=250" \
  -o "${TMP_DIR}/gpx_upload.json"

python3 - "${TMP_DIR}/gpx_upload.json" <<'PY'
import json, sys
from pathlib import Path
r = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(f"  GPX trip_id     : {r['trip_id']} (visibility={r['visibility']})")
print(f"  privacy trim    : {r['privacy_trim_m']:.0f} m start/end -> raw {r['raw_length_m']:.0f} m trimmed to {r['trimmed_length_m']:.0f} m")
print(f"  HMM matching    : status={r['match_status']} | {r['matched_segment_count']} segments | avg_conf={r['avg_match_confidence']} | obs_published={r['observations_published']}")
assert r["trimmed_length_m"] < r["raw_length_m"], "Expected >= 250 m privacy trim reduction"
assert r["matched_segment_count"] > 0 and r["observations_published"] > 0
PY

curl -fsS --connect-timeout 5 --max-time 60 \
  -X POST "${BASE_URL}/api/trips/upload" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary "@${SCRIPT_DIR}/telemetry_sample.json" \
  -o "${TMP_DIR}/json_upload.json"

python3 - "${TMP_DIR}/json_upload.json" <<'PY'
import json, sys
from pathlib import Path
r = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(f"  JSON trip_id    : {r['trip_id']} (visibility={r['visibility']}, IMU roughness_rms included)")
print(f"  private guard   : matched={r['matched_segment_count']} segments | obs_published={r['observations_published']} (zero public telemetry leaked)")
assert r["visibility"] == "private" and r["observations_published"] == 0
PY

# ------------------------------------------------------------------------------
# 6. Locked-Gate Community Report & One-Click OSM Fix Links
# ------------------------------------------------------------------------------
echo ""
echo "==> [6/9] Community Locked-Gate Report & One-Click OSM Access Fix Links"
REPORT_PAYLOAD="$(python3 -c 'import json,sys; print(json.dumps({
  "report_type": "locked_gate",
  "lon": 148.1206,
  "lat": -36.5880,
  "track_id": sys.argv[1],
  "notes": "Demo walkthrough report: padlocked gate verified on spur entry; signage clearly posted."
}))' "${DAVIES_ID}")"
api_post_json_to "/api/reports" "${REPORT_PAYLOAD}" "${TMP_DIR}/report.json" -H "Authorization: Bearer ${TOKEN}"
python3 - "${TMP_DIR}/report.json" <<'PY'
import json, sys
from pathlib import Path
r = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(f"  report_id       : {r['id']} | type={r['report_type']} | gate_warning={r['is_gate_warning']} | freshness={r['freshness_score']}")
print(f"  OSM edit link   : {r['osm_edit_url']}")
print(f"  OSM note link   : {r['osm_note_url']}")
assert r["is_gate_warning"] is True
assert "openstreetmap.org" in r["osm_edit_url"] and "openstreetmap.org" in r["osm_note_url"]
PY

# ------------------------------------------------------------------------------
# 7. Live Grading Recompute & 72h Rainfall Inspection
# ------------------------------------------------------------------------------
echo ""
echo "==> [7/9] Explainable Live Grading Recompute & 72h Rainfall Context"
api_post_json_to "/api/grading/recompute" '{"rain_72h_mm": 18.5, "rain_24h_mm": 6.2}' "${TMP_DIR}/recompute.json"
api_get_to "/api/grading/rainfall" "${TMP_DIR}/rainfall.json"
api_get_to "/api/tracks/${DAVIES_ID}" "${TMP_DIR}/live_track.json" -H "Authorization: Bearer ${TOKEN}"
python3 - "${TMP_DIR}/recompute.json" "${TMP_DIR}/rainfall.json" "${TMP_DIR}/live_track.json" <<'PY'
import json, sys
from pathlib import Path
rec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
rain = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
trk = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
print(f"  recompute       : segments_evaluated={rec.get('segments_evaluated')} | with_telemetry={rec.get('segments_with_telemetry')} | hotspots={rec.get('obstacle_hotspots_count')} | rainfall_cells={rec.get('rainfall_cells_updated')}")
print(f"  rainfall feed   : {rain.get('attribution')}")
rc = trk.get("rainfall_context") or {}
print(f"  track rainfall  : 72h={rc.get('rain_72h_mm')} mm | wet_risk={rc.get('wet_risk_level')}")
print(f"  live breakdown  : grade={trk.get('live_grade')} | samples={trk.get('sample_count')} | confidence={trk.get('confidence_level')} | split={trk.get('vehicle_class_split')}")
print(f"  obstacle spots  : {len(trk.get('obstacle_hotspots', []))} rough/slow hotspot segments")
PY

# ------------------------------------------------------------------------------
# 8. Offline Regional Pack Manifest Inspection
# ------------------------------------------------------------------------------
echo ""
echo "==> [8/9] Offline Regional Pack Catalog & SHA-256 Manifest"
api_get_to "/api/packs" "${TMP_DIR}/packs.json"
api_get_to "/api/packs/australian-alps/manifest.json" "${TMP_DIR}/manifest.json"
python3 - "${TMP_DIR}/packs.json" "${TMP_DIR}/manifest.json" <<'PY'
import json, sys
from pathlib import Path
packs = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")).get("regions", [])
manifest = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
built = [p["region_id"] for p in packs if p.get("status") == "ready"]
print(f"  catalog regions : {len(packs)} configured | built={built}")
print(f"  alps manifest   : region={manifest.get('region_id')} | version={manifest.get('version')} | total_bytes={manifest.get('total_size_bytes')}")
for fname, meta in (manifest.get("files") or {}).items():
    sha = str(meta.get("sha256", ""))[:16]
    print(f"    - {fname}: {meta.get('size_bytes')} bytes (sha256={sha}...)")
PY

# ------------------------------------------------------------------------------
# 9. Export GridForge-Compatible GPX 1.1 & GeoJSON to ./out/
# ------------------------------------------------------------------------------
echo ""
echo "==> [9/9] Export GridForge-Compatible GPX 1.1 & GeoJSON to ${OUT_DIR}"
GPX_OUT="${OUT_DIR}/davies_plain_track.gpx"
GEOJSON_OUT="${OUT_DIR}/davies_plain_track.geojson"
api_get_to "/api/tracks/${DAVIES_ID}/export?format=gpx" "${GPX_OUT}"
api_get_to "/api/tracks/${DAVIES_ID}/export?format=geojson" "${GEOJSON_OUT}"

python3 - "${GPX_OUT}" "${GEOJSON_OUT}" <<'PY'
import json, sys, xml.etree.ElementTree as ET
from pathlib import Path
gpx_path, gj_path = Path(sys.argv[1]), Path(sys.argv[2])
root = ET.fromstring(gpx_path.read_text(encoding="utf-8"))
gj = json.loads(gj_path.read_text(encoding="utf-8"))
assert root.tag.endswith("gpx"), f"Unexpected GPX root: {root.tag}"
assert gj.get("type") == "FeatureCollection" and len(gj.get("features", [])) >= 1
print(f"  GPX exported    : {gpx_path} ({gpx_path.stat().st_size} bytes)")
print(f"  GeoJSON exported: {gj_path} ({gj_path.stat().st_size} bytes, {len(gj['features'])} features)")
PY

echo ""
echo "======================================================================"
echo " Walkthrough complete! Next: print waterproof backup maps in GridForge:"
echo "   gridforge render --strip-route ${GPX_OUT} --elevation-profile \\"
echo "     --calibration-page --scale 25000 --paper a4 --dem-source au-elvis \\"
echo "     --au-overlay au-state-trails --output ${OUT_DIR}/davies_plain_atlas.pdf"
echo "======================================================================"
