# syntax=docker/dockerfile:1
# Multi-stage, multi-arch (linux/arm64 + linux/amd64) TrailForge backend & GIS worker image.
# Intentionally excludes PyQGIS (qgis.core): TrailForge serves interactive vector/terrain
# PMTiles via GDAL + osmium-tool + tippecanoe + rio-rgbify + pmtiles CLI.

ARG TIPPECANOE_VERSION=2.79.0
ARG TIPPECANOE_SHA256=b0fd9df49b6efc988288ea48774822c6de19eb48428017f27ee0b3b01d44f05d
ARG PMTILES_VERSION=1.31.2
ARG PMTILES_SHA256_AMD64=3ed7dbf4ec2e6dfe5e25b6f70d1ffc932729f93c86db353bf514dd71010a312f
ARG PMTILES_SHA256_ARM64=f8bd47e7ea866863489cad588fbaf2f31f42e5821f7a03f009b3769f05801cb1

# ---------------------------------------------------------------------------
# Stage 1: Build tippecanoe from source (multi-arch arm64 & amd64)
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS tippecanoe-builder

ARG TIPPECANOE_VERSION
ARG TIPPECANOE_SHA256
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
        -o /tmp/tippecanoe.tar.gz \
    && echo "${TIPPECANOE_SHA256}  /tmp/tippecanoe.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/tippecanoe.tar.gz --strip-components=1 \
    && rm -f /tmp/tippecanoe.tar.gz \
    && make -j"$(nproc)" \
    && make install PREFIX=/usr/local

# ---------------------------------------------------------------------------
# Stage 2: Fetch official multi-arch go-pmtiles CLI binary
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS pmtiles-downloader

ARG PMTILES_VERSION
ARG PMTILES_SHA256_AMD64
ARG PMTILES_SHA256_ARM64
ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH}" in \
        arm64) PMTILES_ARCH="arm64"; PMTILES_SHA256="${PMTILES_SHA256_ARM64}" ;; \
        amd64) PMTILES_ARCH="x86_64"; PMTILES_SHA256="${PMTILES_SHA256_AMD64}" ;; \
        *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/protomaps/go-pmtiles/releases/download/v${PMTILES_VERSION}/go-pmtiles_${PMTILES_VERSION}_Linux_${PMTILES_ARCH}.tar.gz" \
        -o /tmp/pmtiles.tar.gz; \
    echo "${PMTILES_SHA256}  /tmp/pmtiles.tar.gz" | sha256sum -c -; \
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

# Install locked Python dependencies deterministically via uv (incl. built SEP-1865 MCP App asset)
COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/
COPY --from=mcp-app-builder /web/dist-mcp/explorer.html ./src/trailforge/mcp/static/explorer.html
RUN UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --frozen --no-cache --extra gis \
    $( [ "${INSTALL_DEV}" = "true" ] && echo "--extra dev" || echo "--no-dev" )

# Copy migrations, alembic config, and runtime offline fixtures (strip test code in prod builds)
COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY tests/ ./tests/
RUN if [ "${INSTALL_DEV}" != "true" ]; then \
        find tests -mindepth 1 -maxdepth 1 ! -name fixtures -exec rm -rf {} +; \
    fi

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
