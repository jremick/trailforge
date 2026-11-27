"""REST API for Slice 09 Offline Regional Packs, Manifests, Snapshots, and Range-Capable PMTiles."""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterator
from datetime import UTC
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from trailforge.auth.dependencies import require_admin
from trailforge.config import get_settings
from trailforge.db.models import OfflinePack, User
from trailforge.db.session import get_db
from trailforge.packs.builder import (
    BUNDLE_FILENAME,
    MANIFEST_FILENAME,
    SNAPSHOT_FILENAME,
    TERRAIN_TILE_FILENAME,
    VECTOR_TILE_FILENAME,
    build_regional_pack,
    get_packs_dir,
    load_pack_manifest,
)
from trailforge.packs.catalog import (
    PACK_REGIONS,
    PackRegionSpec,
    list_pack_regions,
)

router = APIRouter(prefix="/api/packs", tags=["packs"])

ALLOWED_PACK_TILE_FILENAMES: Final[frozenset[str]] = frozenset(
    {
        VECTOR_TILE_FILENAME,
        TERRAIN_TILE_FILENAME,
    }
)
_CHUNK_SIZE: Final[int] = 262_144
_PACK_BUILD_LOCKS_GUARD = threading.Lock()
_PACK_BUILD_LOCKS: dict[str, threading.Lock] = {}


def _get_region_build_lock(region_id: str) -> threading.Lock:
    with _PACK_BUILD_LOCKS_GUARD:
        lock = _PACK_BUILD_LOCKS.get(region_id)
        if lock is None:
            lock = threading.Lock()
            _PACK_BUILD_LOCKS[region_id] = lock
        return lock
_RANGE_RE: Final[re.Pattern[str]] = re.compile(r"^bytes=(\d*)-(\d*)$")
_ALLOWED_DEM_ROOTS: Final[tuple[Path, ...]] = (
    Path("tests/fixtures").resolve(),
    Path("/data").resolve(),
)


class PackBuildRequest(BaseModel):
    """Optional request body for ``POST /api/packs/{region_id}/build``."""

    dem_path: str | None = Field(
        default=None,
        description="Optional DEM GeoTIFF path confined inside tests/fixtures or /data.",
    )
    version: str | None = Field(default=None, max_length=64)
    skip_tiles: bool = Field(default=False)


def get_packs_dir_dep() -> Path:
    """FastAPI dependency returning the configured offline packs directory."""
    return get_packs_dir()


def _has_unsafe_path_chars(value: str) -> bool:
    return not value or "\x00" in value or "/" in value or "\\" in value or ".." in value


def _validate_region_id(region_id: str) -> PackRegionSpec:
    """Strictly validate ``region_id`` against the 4-region catalog (rejects traversal)."""
    if _has_unsafe_path_chars(region_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid region_id",
        )
    key = region_id.strip().lower()
    region = PACK_REGIONS.get(key)
    if region is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown offline pack region: {region_id!r}",
        )
    return region


def _resolve_confined_dem_path(raw_path: str | None) -> Path | None:
    """Resolve optional ``dem_path`` strictly confined inside ``tests/fixtures`` or ``/data``."""
    if not raw_path:
        return None
    if "\x00" in raw_path:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid dem_path",
        )
    candidate = Path(raw_path).expanduser().resolve()
    if candidate.suffix.lower() not in (".tif", ".tiff"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="dem_path must be a .tif/.tiff file inside tests/fixtures or /data",
        )
    if not any(candidate.is_relative_to(root) for root in _ALLOWED_DEM_ROOTS):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="dem_path must resolve inside tests/fixtures or /data",
        )
    if not candidate.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"DEM file not found: {candidate.name}",
        )
    return candidate


def _resolve_pack_file(packs_dir: Path, region_id: str, filename: str) -> Path:
    """Confine a pack file read strictly inside ``<packs_dir>/<region_id>/``."""
    region = _validate_region_id(region_id)
    if _has_unsafe_path_chars(filename):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid pack filename",
        )
    region_dir = (packs_dir.resolve() / region.region_id).resolve()
    raw_target = region_dir / filename
    if raw_target.is_symlink():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Symlink pack files are forbidden",
        )
    target = raw_target.resolve()
    if not target.is_relative_to(region_dir):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Pack path escapes region directory",
        )
    if not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pack artifact {filename!r} has not been built for region {region.region_id!r}",
        )
    return target


def _resolve_pack_tile(packs_dir: Path, region_id: str, filename: str) -> Path:
    """Validate pack tile filename against strict PMTiles allowlist + path confinement."""
    _validate_region_id(region_id)
    if _has_unsafe_path_chars(filename):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid tile filename",
        )
    if filename not in ALLOWED_PACK_TILE_FILENAMES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Requested pack PMTiles archive is not allowed",
        )
    return _resolve_pack_file(packs_dir, region_id, filename)


def _compute_etag(file_path: Path) -> tuple[str, int]:
    st = file_path.stat()
    etag = f'"{st.st_size:x}-{st.st_mtime_ns:x}"'
    return etag, st.st_size


def _parse_byte_range(range_header: str, file_size: int) -> tuple[int, int]:
    """Parse single RFC 9110 ``bytes=...`` Range header against ``file_size``."""
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


def _get_packs_public_base_url() -> str:
    """Safely read optional public R2/CDN base URL for offline pack downloads."""
    cfg = get_settings()
    val = getattr(cfg, "packs_public_base_url", None)
    if val is not None:
        raw = str(val)
    else:
        raw = os.environ.get("PACKS_PUBLIC_BASE_URL") or os.environ.get(
            "TRAILFORGE_PACKS_PUBLIC_BASE_URL", ""
        )
    return raw.strip().rstrip("/")


def _build_region_summary(
    region: PackRegionSpec,
    packs_dir: Path,
    db_row: OfflinePack | None,
) -> dict[str, Any]:
    rid = region.region_id
    manifest = load_pack_manifest(packs_dir, rid)
    region_dir = packs_dir / rid
    bundle_path = region_dir / BUNDLE_FILENAME
    vector_path = region_dir / VECTOR_TILE_FILENAME
    terrain_path = region_dir / TERRAIN_TILE_FILENAME
    public_base = _get_packs_public_base_url()

    if manifest is not None:
        pack_status = "ready"
        version = manifest.version
        generated_at = manifest.generated_at
        total_size_bytes = manifest.total_size_bytes
        bundle_size_bytes = (
            int(bundle_path.stat().st_size) if bundle_path.is_file() else manifest.bundle_size_bytes
        )
        track_count = manifest.track_count
        segment_count = manifest.segment_count
        poi_count = manifest.poi_count
        vector_available = vector_path.is_file()
        terrain_available = terrain_path.is_file()
        bounds = manifest.bounds
        attributions = manifest.attributions
    elif public_base and db_row is not None and db_row.status == "ready":
        pack_status = "ready"
        version = db_row.version
        generated_at = (
            db_row.built_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            if db_row.built_at is not None
            else None
        )
        total_size_bytes = int(db_row.size_bytes or 0)
        bundle_size_bytes = 0
        track_count = 0
        segment_count = 0
        poi_count = 0
        vector_available = bool(db_row.vector_pmtiles_path)
        terrain_available = bool(db_row.terrain_pmtiles_path)
        bounds = list(region.bbox)
        attributions = (
            list(db_row.attributions) if db_row.attributions else list(region.attributions)
        )
    else:
        if db_row is not None and db_row.status in ("building", "failed"):
            pack_status = db_row.status
        else:
            pack_status = "not_built"
        version = db_row.version if db_row is not None and pack_status != "not_built" else None
        generated_at = (
            db_row.built_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            if db_row is not None and db_row.built_at is not None and pack_status != "not_built"
            else None
        )
        total_size_bytes = 0
        bundle_size_bytes = 0
        track_count = 0
        segment_count = 0
        poi_count = 0
        vector_available = False
        terrain_available = False
        bounds = list(region.bbox)
        attributions = list(region.attributions)

    if public_base:
        manifest_url = f"{public_base}/{rid}/{MANIFEST_FILENAME}"
        snapshot_url = f"{public_base}/{rid}/{SNAPSHOT_FILENAME}"
        bundle_url = f"{public_base}/{rid}/{BUNDLE_FILENAME}"
        vector_pmtiles_url = f"{public_base}/{rid}/{VECTOR_TILE_FILENAME}"
        terrain_pmtiles_url = f"{public_base}/{rid}/{TERRAIN_TILE_FILENAME}"
    else:
        manifest_url = f"/api/packs/{rid}/{MANIFEST_FILENAME}"
        snapshot_url = f"/api/packs/{rid}/{SNAPSHOT_FILENAME}"
        bundle_url = f"/api/packs/{rid}/{BUNDLE_FILENAME}"
        vector_pmtiles_url = f"/api/packs/{rid}/tiles/{VECTOR_TILE_FILENAME}"
        terrain_pmtiles_url = f"/api/packs/{rid}/tiles/{TERRAIN_TILE_FILENAME}"

    return {
        "region_id": rid,
        "name": region.name,
        "description": region.description,
        "region_type": region.region_type,
        "category": region.category,
        "states": list(region.states),
        "bbox": list(region.bbox),
        "bounds": bounds,
        "utm_zones": list(region.utm_zones),
        "estimated_size_hint": region.estimated_size_hint,
        "status": pack_status,
        "version": version,
        "generated_at": generated_at,
        "total_size_bytes": total_size_bytes,
        "bundle_size_bytes": bundle_size_bytes,
        "track_count": track_count,
        "segment_count": segment_count,
        "poi_count": poi_count,
        "vector_available": vector_available,
        "terrain_available": terrain_available,
        "manifest_url": manifest_url,
        "snapshot_url": snapshot_url,
        "bundle_url": bundle_url,
        "vector_pmtiles_url": vector_pmtiles_url,
        "terrain_pmtiles_url": terrain_pmtiles_url,
        "attributions": attributions,
    }


@router.get("")
def list_packs(
    packs_dir: Path = Depends(get_packs_dir_dep),
    db: Session = Depends(get_db),
) -> dict[str, list[dict[str, Any]]]:
    """List all 15 Pan-Australia offline packs with build status, sizes, and URLs."""
    rows_by_id = {
        row.pack_id: row for row in db.execute(select(OfflinePack)).scalars().all()
    }
    regions = [
        _build_region_summary(region, packs_dir, rows_by_id.get(region.region_id))
        for region in list_pack_regions()
    ]
    return {"regions": regions}


@router.post("/{region_id}/build")
def build_pack_endpoint(
    region_id: str,
    body: PackBuildRequest | None = None,
    _admin: User = Depends(require_admin),
    packs_dir: Path = Depends(get_packs_dir_dep),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Build or refresh a regional offline pack streaming disk-to-disk with per-region mutex."""
    region = _validate_region_id(region_id)
    req = body or PackBuildRequest()
    dem_path = _resolve_confined_dem_path(req.dem_path)

    build_lock = _get_region_build_lock(region.region_id)
    if not build_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A pack build for region '{region.region_id}' is already in progress",
        )
    try:
        result = build_regional_pack(
            db,
            region.region_id,
            packs_dir,
            dem_path=dem_path,
            version=req.version,
            skip_tiles=req.skip_tiles,
        )
        db_row = db.get(OfflinePack, region.region_id)
        summary = _build_region_summary(region, packs_dir, db_row)
        summary["manifest_sha256"] = result.manifest_sha256
        summary["manifest"] = result.manifest.model_dump(mode="json")
        return summary
    finally:
        build_lock.release()


@router.get("/{region_id}/manifest.json")
def get_pack_manifest_endpoint(
    region_id: str,
    packs_dir: Path = Depends(get_packs_dir_dep),
) -> JSONResponse:
    """Return a built regional pack's ``manifest.json`` with SHA-256 checksums & attributions."""
    manifest_path = _resolve_pack_file(packs_dir, region_id, MANIFEST_FILENAME)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    return JSONResponse(
        content=payload,
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/{region_id}/snapshot.json")
def get_pack_snapshot_endpoint(
    region_id: str,
    request: Request,
    packs_dir: Path = Depends(get_packs_dir_dep),
) -> Response:
    """Stream a built regional pack's compact offline ``snapshot.json`` with ETag/304 support."""
    snapshot_path = _resolve_pack_file(packs_dir, region_id, SNAPSHOT_FILENAME)
    etag, file_size = _compute_etag(snapshot_path)
    headers = {
        "ETag": etag,
        "Cache-Control": "public, max-age=300",
    }
    if_none_match = (request.headers.get("if-none-match") or "").strip()
    if if_none_match and (if_none_match == etag or if_none_match == f"W/{etag}"):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return StreamingResponse(
        _stream_file_slice(snapshot_path, 0, file_size),
        media_type="application/json",
        headers={
            **headers,
            "Content-Length": str(file_size),
        },
    )


@router.head("/{region_id}/tiles/{filename:path}")
def head_pack_pmtiles(
    region_id: str,
    filename: str,
    request: Request,
    packs_dir: Path = Depends(get_packs_dir_dep),
) -> Response:
    """Return Range-capable metadata headers for a regional pack's PMTiles archive."""
    target = _resolve_pack_tile(packs_dir, region_id, filename)
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


@router.get("/{region_id}/tiles/{filename:path}")
def get_pack_pmtiles(
    region_id: str,
    filename: str,
    request: Request,
    packs_dir: Path = Depends(get_packs_dir_dep),
) -> Response:
    """Stream full or RFC 9110 byte-range sliced regional pack PMTiles archive."""
    target = _resolve_pack_tile(packs_dir, region_id, filename)
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


@router.get("/{region_id}/bundle.tar.gz")
def get_pack_bundle_endpoint(
    region_id: str,
    packs_dir: Path = Depends(get_packs_dir_dep),
) -> StreamingResponse:
    """Stream a built regional pack's downloadable ``bundle.tar.gz`` archive."""
    region = _validate_region_id(region_id)
    bundle_path = _resolve_pack_file(packs_dir, region.region_id, BUNDLE_FILENAME)
    etag, file_size = _compute_etag(bundle_path)
    return StreamingResponse(
        _stream_file_slice(bundle_path, 0, file_size),
        media_type="application/gzip",
        headers={
            "Content-Length": str(file_size),
            "ETag": etag,
            "Content-Disposition": (
                f'attachment; filename="trailforge-pack-{region.region_id}.tar.gz"'
            ),
            "Cache-Control": "public, max-age=300",
        },
    )
