# syntax=docker/dockerfile:1
# Multi-stage, multi-arch (linux/arm64 + linux/amd64) TrailForge backend & GIS worker image.
# Intentionally excludes PyQGIS (qgis.core): TrailForge serves interactive vector/terrain
# PMTiles via GDAL + osmium-tool + tippecanoe + rio-rgbify + pmtiles CLI.

ARG TIPPECANOE_VERSION=2.79.0
ARG PMTILES_VERSION=1.31.2

# ---------------------------------------------------------------------------
# Stage 1: Build tippecanoe from source (multi-arch arm64 & amd64)
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS tippecanoe-builder

ARG TIPPECANOE_VERSION
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    libsqlite3-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/tippecanoe
RUN curl -fsSL "https://github.com/felt/tippecanoe/archive/refs/tags/${TIPPECANOE_VERSION}.tar.gz" \
    | tar -xz --strip-components=1 \
    && make -j"$(nproc)" \
    && make install PREFIX=/usr/local

# ---------------------------------------------------------------------------
# Stage 2: Fetch official multi-arch go-pmtiles CLI binary
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS pmtiles-downloader

ARG PMTILES_VERSION
ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH}" in \
        arm64) PMTILES_ARCH="arm64" ;; \
        amd64) PMTILES_ARCH="x86_64" ;; \
        *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/protomaps/go-pmtiles/releases/download/v${PMTILES_VERSION}/go-pmtiles_${PMTILES_VERSION}_Linux_${PMTILES_ARCH}.tar.gz" \
        -o /tmp/pmtiles.tar.gz; \
    tar -xzf /tmp/pmtiles.tar.gz -C /usr/local/bin pmtiles; \
    chmod +x /usr/local/bin/pmtiles; \
    rm -f /tmp/pmtiles.tar.gz

# ---------------------------------------------------------------------------
# Stage 2b: Build single-file SEP-1865 MCP App HTML (`explorer.html`)
# ---------------------------------------------------------------------------
FROM node:22-slim AS mcp-app-builder

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build:mcp

# ---------------------------------------------------------------------------
# Stage 3: Lean Python 3.12 + GDAL + osmium + tippecanoe + rio-rgbify + pmtiles runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ARG INSTALL_DEV=false
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gdal-bin \
    libgdal32 \
    libgeos-c1v5 \
    libproj25 \
    libsqlite3-0 \
    osmium-tool \
    proj-data \
    zlib1g \
    && rm -rf /var/lib/apt/lists/*

# Install compiled tippecanoe & pmtiles CLIs
COPY --from=tippecanoe-builder /usr/local/bin/tippecanoe* /usr/local/bin/
COPY --from=tippecanoe-builder /usr/local/bin/tile-join /usr/local/bin/
COPY --from=pmtiles-downloader /usr/local/bin/pmtiles /usr/local/bin/pmtiles

# Create isolated virtualenv
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

# Install Python dependencies first for layer caching (incl. built SEP-1865 MCP App asset)
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
COPY --from=mcp-app-builder /web/dist-mcp/explorer.html ./src/trailforge/mcp/static/explorer.html
RUN pip install --no-cache-dir --upgrade pip \
    && if [ "${INSTALL_DEV}" = "true" ]; then \
        pip install --no-cache-dir ".[gis,dev]"; \
    else \
        pip install --no-cache-dir ".[gis]"; \
    fi

# Copy migrations, alembic config, and offline test suite
COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY tests/ ./tests/

# GDAL /vsicurl/ COG streaming & 16 GB laptop/homelab memory-bounded defaults
# Note: Leave PROJ_DATA unset so system gdal-bin uses /usr/share/proj while
# manylinux rasterio/pyproj wheels use their own bundled proj.db without mismatch.
ENV GDAL_CACHEMAX=192 \
    OMP_THREAD_LIMIT=1 \
    GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif,.vrt \
    VSI_CACHE=TRUE \
    VSI_CACHE_SIZE=67108864 \
    GDAL_HTTP_MAX_RETRY=3 \
    GDAL_HTTP_RETRY_DELAY=2 \
    TRAILFORGE_DATA_DIR=/data \
    TRAILFORGE_TILES_DIR=/data/tiles \
    TRAILFORGE_PACKS_DIR=/data/packs \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10001 trailforge \
    && useradd --system --uid 10001 --gid trailforge --home-dir /app --shell /usr/sbin/nologin trailforge \
    && mkdir -p /data/tiles /data/packs /data/cache /data/uploads \
    && chown -R trailforge:trailforge /data /app

USER trailforge

EXPOSE 8000

CMD ["uvicorn", "trailforge.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
