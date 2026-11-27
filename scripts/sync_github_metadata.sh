#!/usr/bin/env bash
# Idempotently sync GitHub repository homepage, description, topics, and verify
# OpenGraph social preview + public GHCR manifest availability.
#
# Usage:
#   ./scripts/sync_github_metadata.sh [owner/repo]
set -euo pipefail

REPO="${1:-jremick/trailforge}"
OWNER="${REPO%%/*}"

HOMEPAGE="https://mytrailforge.com"
DESCRIPTION="Live at mytrailforge.com — Open-source, self-hostable Australian 4WD mapping & live track-grading platform: explainable DEM+telemetry grades, 11 live AU closure feeds, multi-track route planner, 15 offline PMTiles packs, 50 Hz IMU & native AI/MCP server."

TOPICS=(
  4wd
  australia
  fastapi
  gis
  gpx
  maplibre
  mcp
  mgrs
  model-context-protocol
  oauth2
  offline-maps
  offroad
  openstreetmap
  overlanding
  pmtiles
  postgis
  pwa
  route-planning
  self-hosted
  topographic-maps
)

echo "Updating homepage (${HOMEPAGE}), description & ${#TOPICS[@]} topics on ${REPO}..."
TOPIC_ARGS=()
for t in "${TOPICS[@]}"; do
  TOPIC_ARGS+=(--add-topic "$t")
done

gh repo edit "${REPO}" \
  --homepage "${HOMEPAGE}" \
  --description "${DESCRIPTION}" \
  "${TOPIC_ARGS[@]}"

echo
echo "Checking repository metadata & Social Preview status..."
gh repo view "${REPO}" --json homepageUrl,description,repositoryTopics,usesCustomOpenGraphImage \
  --jq '{homepageUrl, description, topic_count: (.repositoryTopics | length), usesCustomOpenGraphImage}'

USES_CUSTOM_OG="$(gh repo view "${REPO}" --json usesCustomOpenGraphImage --jq '.usesCustomOpenGraphImage')"
if [ "${USES_CUSTOM_OG}" != "true" ]; then
  echo
  echo "WARNING: GitHub custom Social Preview image is not uploaded yet."
  echo "Upload docs/images/banner.png (1280x640) once at:"
  echo "  https://github.com/${REPO}/settings (Social preview -> Edit -> Upload an image)"
fi

echo
echo "Checking public GHCR pull access for multi-arch images..."
for IMG in "${OWNER}/trailforge-backend" "${OWNER}/trailforge-web"; do
  TOKEN="$(curl -fsSL "https://ghcr.io/token?scope=repository:${IMG}:pull" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")"
  HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json" \
    "https://ghcr.io/v2/${IMG}/manifests/latest")"

  if [ "${HTTP_CODE}" = "200" ]; then
    echo "GHCR public pull OK: ghcr.io/${IMG}:latest (HTTP ${HTTP_CODE})"
  else
    echo "ERROR: ghcr.io/${IMG}:latest returned HTTP ${HTTP_CODE} (check Package visibility -> Public)" >&2
    exit 1
  fi
done
