"""HTTP Range-request compliant PMTiles and manifest serving endpoints.

Serves generated vector (`trailforge_vector.pmtiles`) and terrain-RGB
(`trailforge_terrain.pmtiles`) archives directly from `TILES_DIR` with full
RFC 9110 `Range` (`206 Partial Content` / `416 Range Not Satisfiable`), `HEAD`,
and `ETag` support so MapLibre GL JS + `pmtiles` v4 can stream tiles cleanly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from trailforge.config import get_settings
from trailforge.ingestion.connectors.base import (
    ATTRIBUTION_CAPAD,
    ATTRIBUTION_GA_DEM,
    ATTRIBUTION_GA_HYDRO,
    ATTRIBUTION_NSW_SPATIAL,
    ATTRIBUTION_OSM,
    ATTRIBUTION_VIC_DEECA,
)
from trailforge.tiles.builder import (
    PUBLIC_VECTOR_PMTILES_FILENAME,
    STATIC_TILE_TELEMETRY_POLICY,
    VECTOR_LAYER_ORDER,
    VECTOR_PMTILES_FILENAME,
)

router = APIRouter(prefix="/api/tiles", tags=["tiles"])

ALLOWED_PMTILES: frozenset[str] = frozenset(
    {
        "trailforge_vector.pmtiles",
        "trailforge_terrain.pmtiles",
    }
)
DEFAULT_SNOWLINE_BANDS_M: list[int] = [800, 900, 1000, 1100, 1200, 1400, 1600]
DEFAULT_CORRIDOR_BOUNDS: list[float] = [148.05, -36.68, 148.20, -36.55]
DEFAULT_ATTRIBUTIONS: list[str] = [
    ATTRIBUTION_OSM,
    ATTRIBUTION_GA_DEM,
    ATTRIBUTION_GA_HYDRO,
    ATTRIBUTION_CAPAD,
    ATTRIBUTION_NSW_SPATIAL,
    ATTRIBUTION_VIC_DEECA,
]

_CHUNK_SIZE = 262_144
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def get_tiles_dir() -> Path:
    """Dependency returning the configured PMTiles directory (overridable in tests)."""
    return Path(get_settings().tiles_dir)


def _load_tiles_manifest(tiles_dir: Path) -> dict[str, Any]:
    try:
        payload = json.loads((tiles_dir / "manifest.json").read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _resolve_allowed_pmtiles(tiles_dir: Path, filename: str) -> Path:
    """Validate filename against strict allowlist and ensure no path traversal."""
    if filename not in ALLOWED_PMTILES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Requested PMTiles archive is not available",
        )
    base_dir = tiles_dir.resolve()
    target = (base_dir / filename).resolve()
    if target.parent != base_dir or not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PMTiles archive {filename!r} has not been generated yet",
        )
    if filename == VECTOR_PMTILES_FILENAME and (
        _load_tiles_manifest(base_dir).get("community_telemetry_policy")
        != STATIC_TILE_TELEMETRY_POLICY
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vector archive needs to be rebuilt")
    return target


def _compute_etag(file_path: Path) -> tuple[str, int]:
    st = file_path.stat()
    etag = f'"{st.st_size:x}-{st.st_mtime_ns:x}"'
    return etag, st.st_size


def _parse_byte_range(range_header: str, file_size: int) -> tuple[int, int]:
    """Parse single RFC 9110 `bytes=...` Range header against `file_size`.

    Raises HTTPException(416) for malformed, multi-range, or out-of-bounds ranges.
    """
    value = range_header.strip()
    if "," in value:
        raise HTTPException(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            detail="Multiple byte ranges are not supported",
            headers={"Content-Range": f"bytes */{file_size}"},
        )
    match = _RANGE_RE.match(value)
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            detail="Malformed Range header",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    start_s, end_s = match.group(1), match.group(2)
    if not start_s and not end_s:
        raise HTTPException(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            detail="Empty byte range",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    if file_size <= 0:
        raise HTTPException(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            detail="Empty archive",
            headers={"Content-Range": "bytes */0"},
        )

    if not start_s:
        # Suffix range: bytes=-N (last N bytes)
        suffix_len = int(end_s)
        if suffix_len <= 0:
            raise HTTPException(
                status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                detail="Zero-length suffix range",
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        start = max(0, file_size - suffix_len)
        end = file_size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else file_size - 1
        if start >= file_size or start > end:
            raise HTTPException(
                status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                detail="Requested range is out of bounds",
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        end = min(end, file_size - 1)

    return start, end


def _stream_file_slice(file_path: Path, start: int, length: int) -> Iterator[bytes]:
    with file_path.open("rb") as fh:
        fh.seek(start)
        remaining = length
        while remaining > 0:
            chunk = fh.read(min(_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@router.get("/manifest.json")
def get_tiles_manifest(
    request: Request,
    tiles_dir: Path = Depends(get_tiles_dir),
) -> JSONResponse:
    """Return active PMTiles archive availability, bounds, zooms, layers, and attributions."""
    cfg = getattr(getattr(request.app, "state", None), "custom_settings", None) or get_settings()
    public_base = cfg.tiles_public_base_url.strip().rstrip("/")
    cdn_offload = bool(public_base)
    configured_vector_basemap_url = cfg.vector_basemap_url.strip()

    vector_path = tiles_dir / "trailforge_vector.pmtiles"
    terrain_path = tiles_dir / "trailforge_terrain.pmtiles"
    disk_manifest = _load_tiles_manifest(tiles_dir)
    local_vector_current = (
        vector_path.is_file()
        and disk_manifest.get("community_telemetry_policy") == STATIC_TILE_TELEMETRY_POLICY
    )

    default_vector_url = (
        f"{public_base}/{PUBLIC_VECTOR_PMTILES_FILENAME}"
        if cdn_offload
        else "/api/tiles/trailforge_vector.pmtiles"
    )
    default_terrain_url = (
        f"{public_base}/trailforge_terrain.pmtiles"
        if cdn_offload
        else "/api/tiles/trailforge_terrain.pmtiles"
    )

    payload: dict[str, Any] = {
        "generated_at": None,
        "bounds": list(DEFAULT_CORRIDOR_BOUNDS),
        "minzoom": 8,
        "maxzoom": 15,
        "vector_url": default_vector_url,
        "terrain_url": default_terrain_url,
        "vector_basemap_url": configured_vector_basemap_url or None,
        "vector_available": vector_path.is_file() or cdn_offload,
        "terrain_available": terrain_path.is_file() or cdn_offload,
        "cdn_offload": cdn_offload,
        "layers": [],
        "snowline_bands_m": list(DEFAULT_SNOWLINE_BANDS_M),
        "attributions": list(DEFAULT_ATTRIBUTIONS),
    }

    payload.update(disk_manifest)

    # Always reflect live disk/CDN availability & canonical URLs
    payload["vector_url"] = default_vector_url
    payload["terrain_url"] = default_terrain_url
    if configured_vector_basemap_url:
        payload["vector_basemap_url"] = configured_vector_basemap_url
    else:
        disk_basemap = payload.get("vector_basemap_url")
        payload["vector_basemap_url"] = (
            disk_basemap.strip()
            if isinstance(disk_basemap, str) and disk_basemap.strip()
            else None
        )
    payload["vector_available"] = local_vector_current or cdn_offload
    payload["terrain_available"] = terrain_path.is_file() or cdn_offload
    payload["cdn_offload"] = cdn_offload
    if payload["vector_available"] and not payload.get("layers"):
        payload["layers"] = list(VECTOR_LAYER_ORDER)
    if not payload.get("snowline_bands_m"):
        payload["snowline_bands_m"] = list(DEFAULT_SNOWLINE_BANDS_M)
    if not payload.get("attributions"):
        payload["attributions"] = list(DEFAULT_ATTRIBUTIONS)

    return JSONResponse(
        content=payload,
        headers={"Cache-Control": "no-cache"},
    )


@router.head("/{filename}")
def head_pmtiles(
    filename: str,
    request: Request,
    tiles_dir: Path = Depends(get_tiles_dir),
) -> Response:
    """Return PMTiles archive metadata headers (`Content-Length`, `Accept-Ranges`, `ETag`)."""
    target = _resolve_allowed_pmtiles(tiles_dir, filename)
    etag, file_size = _compute_etag(target)

    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Cache-Control": "public, max-age=300",
        "Content-Type": "application/vnd.pmtiles",
        "Content-Length": str(file_size),
    }

    range_header = request.headers.get("range")
    if range_header:
        start, end = _parse_byte_range(range_header, file_size)
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        headers["Content-Length"] = str(end - start + 1)
        return Response(status_code=status.HTTP_206_PARTIAL_CONTENT, headers=headers)

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)

    return Response(status_code=status.HTTP_200_OK, headers=headers)


@router.get("/{filename}")
def get_pmtiles(
    filename: str,
    request: Request,
    tiles_dir: Path = Depends(get_tiles_dir),
) -> Response:
    """Stream full or RFC 9110 byte-range sliced PMTiles archive."""
    target = _resolve_allowed_pmtiles(tiles_dir, filename)
    etag, file_size = _compute_etag(target)

    base_headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Cache-Control": "public, max-age=300",
    }

    range_header = request.headers.get("range")
    if range_header:
        start, end = _parse_byte_range(range_header, file_size)
        slice_len = end - start + 1
        headers = {
            **base_headers,
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(slice_len),
        }
        return StreamingResponse(
            _stream_file_slice(target, start, slice_len),
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            media_type="application/vnd.pmtiles",
            headers=headers,
        )

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=base_headers)

    headers = {
        **base_headers,
        "Content-Length": str(file_size),
    }
    return StreamingResponse(
        _stream_file_slice(target, 0, file_size),
        status_code=status.HTTP_200_OK,
        media_type="application/vnd.pmtiles",
        headers=headers,
    )
