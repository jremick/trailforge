"""Australia-wide DEM connector (GA 5 m LiDAR WCS + DEA SRTM 1s COG, vertical datum AHD).

Implements GridForge's coastal-water vs. missing-LiDAR cliff guard (`zero_cliff_ratio <= 0.15`)
and memory-bounded GDAL `/vsicurl/` streaming defaults (< 300 MB peak RAM). Never mixes
Tier 1 (5 m LiDAR) and Tier 2 (SRTM 1s) pixels inside a single elevation clip.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import tempfile
from contextlib import nullcontext
from pathlib import Path
from typing import Final, Literal

import httpx
import numpy as np
from pydantic import BaseModel

from trailforge.ingestion.connectors._cog_ranges import cog_range_path
from trailforge.ingestion.connectors.base import (
    ATTRIBUTION_GA_DEM,
    GDAL_STREAMING_ENV,
    IDENTITY_HTTP_HEADERS,
    BBox,
    DemTierLiteral,
    is_in_australia_coverage,
    iter_bounded_response,
    stream_upstream,
)

logger = logging.getLogger(__name__)

GA_5M_LIDAR_WCS_URL: Final[str] = (
    "https://services.ga.gov.au/gis/services/DEM_LiDAR_5m_2025/MapServer/WCSServer"
)
DEA_SRTM_1S_SMOOTH_COG_URL: Final[str] = (
    "https://dea-public-data.s3.ap-southeast-2.amazonaws.com/"
    "projects/elevation/ga_srtm_dem1sv1_0/dems1sv1_0.tif"
)
DEA_SRTM_1S_HYDRO_COG_URL: Final[str] = (
    "https://dea-public-data.s3.ap-southeast-2.amazonaws.com/"
    "projects/elevation/ga_srtm_dem1sv1_0/demh1sv1_0.tif"
)
GA_SRTM_1S_WCS_URL: Final[str] = (
    "https://services.ga.gov.au/gis/services/"
    "DEM_SRTM_1Second_Hydro_Enforced_2024/MapServer/WCSServer"
)
_MAX_WCS_GEOTIFF_BYTES: Final[int] = 128 * 1024 * 1024
_MAX_DEM_PIXELS: Final[int] = 32_000_000
_SRTM_PIXEL_DEG: Final[float] = 1.0 / 3600


class RasterCoverageStats(BaseModel):
    """Coverage & coastal-water vs. missing-LiDAR cliff statistics for a DEM raster."""

    valid_frac: float
    exact_zero_frac: float
    zero_cliff_ratio: float
    min_elev: float
    max_elev: float
    accept_5m_lidar: bool


def evaluate_raster_coverage_array(
    arr: np.ndarray,
    *,
    nodata: float | None = -9999.0,
    cliff_threshold_m: float = 15.0,
    min_valid_ratio: float = 0.95,
    max_exact_zero_ratio: float = 0.05,
) -> RasterCoverageStats:
    """Evaluate valid fraction, exact-zero fraction, and 0->cliff edge ratio on a 2D array.

    Distinguishes real coastal/estuary sea-level water (0.0 m adjacent to low shoreline,
    ``zero_cliff_ratio <= 0.15``) from GA WCS missing-LiDAR masks (0.0 m directly abutting
    inland >15 m ridges, ``zero_cliff_ratio ~ 0.95–1.00``).
    """
    if arr.ndim != 2 or arr.size == 0:
        return RasterCoverageStats(
            valid_frac=0.0,
            exact_zero_frac=1.0,
            zero_cliff_ratio=1.0,
            min_elev=0.0,
            max_elev=0.0,
            accept_5m_lidar=False,
        )

    rows, cols = arr.shape
    # Downsample to <= 256 x 256 for fast O(1)-memory inspection
    step_r = max(1, math.ceil(rows / 256))
    step_c = max(1, math.ceil(cols / 256))
    sample = np.asarray(arr[::step_r, ::step_c], dtype=np.float32)

    valid_mask = np.isfinite(sample) & (sample >= -500.0) & (sample <= 5000.0)
    if nodata is not None and math.isfinite(nodata):
        valid_mask &= np.abs(sample - np.float32(nodata)) >= 1e-3

    total = int(sample.size)
    valid_count = int(np.count_nonzero(valid_mask))
    if valid_count == 0:
        return RasterCoverageStats(
            valid_frac=0.0,
            exact_zero_frac=1.0,
            zero_cliff_ratio=1.0,
            min_elev=0.0,
            max_elev=0.0,
            accept_5m_lidar=False,
        )

    valid_vals = sample[valid_mask]
    min_elev = float(np.min(valid_vals))
    max_elev = float(np.max(valid_vals))

    zero_mask = valid_mask & (sample == 0.0)
    nonzero_mask = valid_mask & (sample != 0.0)
    exact_zero_count = int(np.count_nonzero(zero_mask))

    zero_cliff_ratio = 0.0
    if exact_zero_count == valid_count:
        zero_cliff_ratio = 1.0
    elif exact_zero_count > 0:
        b_edges = 0
        c_edges = 0
        # 4-neighbor transitions between exact 0.0 and valid non-zero pixels
        for z_a, nz_b, val_b in (
            (zero_mask[:-1, :], nonzero_mask[1:, :], sample[1:, :]),
            (zero_mask[1:, :], nonzero_mask[:-1, :], sample[:-1, :]),
            (zero_mask[:, :-1], nonzero_mask[:, 1:], sample[:, 1:]),
            (zero_mask[:, 1:], nonzero_mask[:, :-1], sample[:, :-1]),
        ):
            edge_mask = z_a & nz_b
            edge_n = int(np.count_nonzero(edge_mask))
            if edge_n > 0:
                b_edges += edge_n
                c_edges += int(np.count_nonzero(edge_mask & (val_b > cliff_threshold_m)))
        if b_edges > 0:
            zero_cliff_ratio = c_edges / b_edges

    valid_frac = valid_count / total
    exact_zero_frac = exact_zero_count / total
    coastal_water_ok = exact_zero_frac <= 0.45 and zero_cliff_ratio <= 0.15
    accept = (
        valid_frac >= min_valid_ratio
        and max_elev > min_elev + 0.1
        and (exact_zero_frac <= max_exact_zero_ratio or coastal_water_ok)
    )
    return RasterCoverageStats(
        valid_frac=valid_frac,
        exact_zero_frac=exact_zero_frac,
        zero_cliff_ratio=zero_cliff_ratio,
        min_elev=min_elev,
        max_elev=max_elev,
        accept_5m_lidar=accept,
    )


def inspect_raster_coverage(
    tif_path: Path,
    *,
    cliff_threshold_m: float = 15.0,
    min_valid_ratio: float = 0.95,
    max_exact_zero_ratio: float = 0.05,
) -> RasterCoverageStats:
    """Inspect a GeoTIFF DEM and apply the coastal-water vs. missing-LiDAR cliff guard."""
    from trailforge.ingestion.terrain import DemSampler

    if tif_path.stat().st_size > _MAX_WCS_GEOTIFF_BYTES:
        raise ValueError("DEM raster exceeds the file size limit")
    with DemSampler(tif_path, max_pixels=_MAX_DEM_PIXELS) as sampler:
        arr, nodata = sampler.read_array()
    return evaluate_raster_coverage_array(
        arr,
        nodata=nodata,
        cliff_threshold_m=cliff_threshold_m,
        min_valid_ratio=min_valid_ratio,
        max_exact_zero_ratio=max_exact_zero_ratio,
    )


def wcs_resolution_for_bbox(bbox: BBox) -> tuple[float, float, str]:
    """Select ~5 m WCS resolution for compact corridors and ~10 m for wider sheets."""
    if bbox.width <= 0.35 and bbox.height <= 0.35:
        return (0.000055, 0.000052, "5m")
    return (0.000110, 0.000104, "10m")


def _clip_dimensions(bbox: BBox, resx: float, resy: float) -> tuple[int, int]:
    # Include a pixel of rounding slack at each edge.
    return (math.ceil(bbox.width / resx) + 2, math.ceil(bbox.height / resy) + 2)


class AUElvisDEMConnector:
    """On-demand Australia-wide elevation connector (GA 5 m LiDAR WCS -> DEA SRTM 1s COG)."""

    name: str = "au-elvis"
    vertical_datum: str = "AHD"
    attribution: str = ATTRIBUTION_GA_DEM

    def __init__(
        self,
        *,
        cache_dir: Path | str | None = None,
        prefer_5m: bool = True,
        srtm_product: Literal["smoothed", "hydro"] = "smoothed",
        max_exact_zero_ratio: float = 0.05,
        min_valid_ratio: float = 0.95,
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.prefer_5m = prefer_5m
        self.srtm_product = srtm_product
        self.max_exact_zero_ratio = max_exact_zero_ratio
        self.min_valid_ratio = min_valid_ratio
        self.timeout_s = timeout_s
        self._client = client

    def _gdal_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for k, v in GDAL_STREAMING_ENV.items():
            env.setdefault(k, v)
        env["GDAL_HTTP_CONNECTTIMEOUT"] = str(min(10.0, self.timeout_s))
        env["GDAL_HTTP_TIMEOUT"] = str(self.timeout_s)
        env.update({
            "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
            "CPL_VSIL_CURL_USE_HEAD": "YES",
            "GDAL_HTTP_MULTIRANGE": "SERIAL",
            "GDAL_HTTP_MAX_RETRY": "0",
            "GDAL_HTTP_PROXY": "",
            "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1",
            "PROJ_NETWORK": "OFF",
        })
        return env

    def _srtm_cog_url(self) -> str:
        if self.srtm_product == "hydro":
            return os.environ.get("TRAILFORGE_DEA_SRTM_HYDRO_URL", DEA_SRTM_1S_HYDRO_COG_URL)
        return os.environ.get("TRAILFORGE_DEA_SRTM_URL", DEA_SRTM_1S_SMOOTH_COG_URL)

    def _download_wcs_raster(self, url: str, params: dict[str, str], dest: Path) -> bool:
        """Download a bounded WCS response to a temporary GeoTIFF for inspection."""
        owns_client = self._client is None
        http = (
            self._client
            if self._client is not None
            else httpx.Client(
                timeout=httpx.Timeout(self.timeout_s, connect=min(10.0, self.timeout_s)),
                headers=IDENTITY_HTTP_HEADERS,
            )
        )
        try:
            with stream_upstream(http, url, params=params) as resp:
                content_type = resp.headers.get("content-type", "").lower()
                if (
                    resp.status_code != 200 or "content-range" in resp.headers
                    or "xml" in content_type or "html" in content_type
                ):
                    return False
                with dest.open("wb") as file:
                    for chunk in iter_bounded_response(
                        resp, max_bytes=_MAX_WCS_GEOTIFF_BYTES, max_duration_s=self.timeout_s * 2
                    ):
                        file.write(chunk)
            if dest.stat().st_size < 256:
                return False
            with dest.open("rb") as file:
                return file.read(4) in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
        finally:
            if owns_client:
                http.close()

    def _fetch_5m_lidar_wcs(self, clip_bbox: BBox, work_dir: Path, out_tif: Path) -> bool:
        # Guard WCS pixel volume: only attempt 5m LiDAR on compact <= 1 deg tiles
        if clip_bbox.width > 1.0 or clip_bbox.height > 1.0:
            return False

        resx, resy, res_tag = wcs_resolution_for_bbox(clip_bbox)
        width, height = _clip_dimensions(clip_bbox, resx, resy)
        if width * height > _MAX_DEM_PIXELS:
            return False
        cache_path = (
            self.cache_dir / "au_elvis" / f"au_5m_{res_tag}_{clip_bbox.cache_hash()}.tif"
            if self.cache_dir is not None
            else None
        )
        if self._copy_cached_raster(cache_path, out_tif, expect_lidar=True):
            return True

        params = {
            "SERVICE": "WCS",
            "VERSION": "1.0.0",
            "REQUEST": "GetCoverage",
            "COVERAGE": "1",
            "CRS": "EPSG:4283",
            "BBOX": (
                f"{clip_bbox.min_x:.6f},{clip_bbox.min_y:.6f},"
                f"{clip_bbox.max_x:.6f},{clip_bbox.max_y:.6f}"
            ),
            "RESX": f"{resx:.6f}",
            "RESY": f"{resy:.6f}",
            "FORMAT": "GeoTIFF",
        }
        with tempfile.TemporaryDirectory(dir=work_dir, prefix="lidar-") as tmp_dir:
            tmp_5m = Path(tmp_dir) / "dem.tif"
            try:
                if not self._download_wcs_raster(GA_5M_LIDAR_WCS_URL, params, tmp_5m):
                    return False
                stats = inspect_raster_coverage(
                    tmp_5m,
                    min_valid_ratio=self.min_valid_ratio,
                    max_exact_zero_ratio=self.max_exact_zero_ratio,
                )
            except Exception as exc:
                logger.warning("GA 5m LiDAR WCS failed (%s); falling back to SRTM 1s", exc)
                return False

            if not stats.accept_5m_lidar:
                logger.info(
                    "GA 5m LiDAR rejected (valid=%.1f%%, zero=%.1f%%, cliff_ratio=%.1f%%); "
                    "falling back to DEA SRTM 1s",
                    stats.valid_frac * 100.0,
                    stats.exact_zero_frac * 100.0,
                    stats.zero_cliff_ratio * 100.0,
                )
                return False

            tmp_5m.replace(out_tif)
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_cache = cache_path.with_suffix(".tif.tmp")
            shutil.copy2(out_tif, tmp_cache)
            tmp_cache.replace(cache_path)
        return True

    def _fetch_srtm_1s_wcs_backup(self, clip_bbox: BBox, out_tif: Path) -> bool:
        params = {
            "SERVICE": "WCS",
            "VERSION": "1.0.0",
            "REQUEST": "GetCoverage",
            "COVERAGE": "1",
            "CRS": "EPSG:4326",
            "BBOX": (
                f"{clip_bbox.min_x:.6f},{clip_bbox.min_y:.6f},"
                f"{clip_bbox.max_x:.6f},{clip_bbox.max_y:.6f}"
            ),
            "RESX": "0.000277778",
            "RESY": "0.000277778",
            "FORMAT": "GeoTIFF",
        }
        try:
            return self._download_wcs_raster(GA_SRTM_1S_WCS_URL, params, out_tif)
        except Exception as exc:
            logger.warning("GA SRTM 1s backup WCS failed: %s", exc)
            return False

    def _copy_cached_raster(
        self, cache_path: Path | None, out_tif: Path, *, expect_lidar: bool = False
    ) -> bool:
        if cache_path is None or not cache_path.is_file():
            return False
        try:
            stats = inspect_raster_coverage(
                cache_path, min_valid_ratio=self.min_valid_ratio,
                max_exact_zero_ratio=self.max_exact_zero_ratio,
            )
        except Exception as exc:
            logger.warning("Ignoring invalid cached DEM %s (%s)", cache_path, exc)
            return False
        if (expect_lidar and not stats.accept_5m_lidar) or stats.valid_frac < 0.05:
            return False
        shutil.copy2(cache_path, out_tif)
        return True

    def _fetch_srtm_1s(self, clip_bbox: BBox, out_tif: Path) -> Path:
        cache_path = (
            self.cache_dir
            / "au_elvis"
            / f"au_srtm1s_{self.srtm_product}_{clip_bbox.cache_hash()}.tif"
            if self.cache_dir is not None
            else None
        )
        if self._copy_cached_raster(cache_path, out_tif):
            return out_tif

        width, height = _clip_dimensions(clip_bbox, _SRTM_PIXEL_DEG, _SRTM_PIXEL_DEG)
        with tempfile.TemporaryDirectory(dir=out_tif.parent, prefix="srtm-") as tmp_dir:
            tmp_tif = Path(tmp_dir) / "dem.tif"
            if (
                not self._fetch_srtm_1s_cog(clip_bbox, tmp_tif, width=width, height=height)
                and not self._fetch_srtm_1s_wcs_backup(clip_bbox, tmp_tif)
            ):
                raise RuntimeError(
                    "Both DEA SRTM 1s COG and GA SRTM 2024 WCS backup failed for "
                    f"{clip_bbox.as_tuple()}"
                )

            stats = inspect_raster_coverage(tmp_tif)
            if stats.valid_frac < 0.05:
                raise RuntimeError(
                    f"Outside Australian DEM coverage (valid={stats.valid_frac * 100.0:.1f}%)"
                )
            tmp_tif.replace(out_tif)

        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_cache = cache_path.with_suffix(".tif.tmp")
            shutil.copy2(out_tif, tmp_cache)
            tmp_cache.replace(cache_path)
        return out_tif

    def _fetch_srtm_1s_cog(
        self, clip_bbox: BBox, out_tif: Path, *, width: int, height: int
    ) -> bool:
        if shutil.which("gdal_translate") is None:
            return False
        cmd = [
            "gdal_translate",
            "-if",
            "GTiff",
            "-of",
            "GTiff",
            "-ot",
            "Float32",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "PREDICTOR=3",
            "-co",
            "TILED=YES",
            "-co",
            "BIGTIFF=IF_SAFER",
            "-projwin",
            f"{clip_bbox.min_x:.6f}",
            f"{clip_bbox.max_y:.6f}",
            f"{clip_bbox.max_x:.6f}",
            f"{clip_bbox.min_y:.6f}",
            "-projwin_srs",
            "EPSG:4326",
            "-outsize",
            str(width),
            str(height),
        ]
        timeout = self.timeout_s * 4
        try:
            http = (
                nullcontext(self._client)
                if self._client is not None
                else httpx.Client(
                    timeout=httpx.Timeout(self.timeout_s, connect=min(10.0, self.timeout_s)),
                    headers=IDENTITY_HTTP_HEADERS,
                )
            )
            with (
                http as client,
                cog_range_path(self._srtm_cog_url(), client, timeout_s=timeout) as source,
            ):
                proc = subprocess.run(
                    [*cmd, source.path, str(out_tif)], env=self._gdal_env(),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                    timeout=timeout,
                )
            if (
                not source.failed.is_set() and proc.returncode == 0 and out_tif.is_file()
                and 0 < out_tif.stat().st_size <= _MAX_WCS_GEOTIFF_BYTES
            ):
                return True
        except (
            httpx.InvalidURL, httpx.HTTPError, OSError, ValueError, subprocess.TimeoutExpired,
        ) as exc:
            logger.warning("DEA SRTM 1s COG request failed (%s)", type(exc).__name__)
        out_tif.unlink(missing_ok=True)
        return False

    def fetch_dem(
        self,
        bbox: BBox,
        work_dir: Path,
        *,
        local_dem: Path | str | None = None,
    ) -> tuple[Path, DemTierLiteral]:
        """Fetch GA 5 m LiDAR (if complete) or DEA SRTM 1s DEM (or use ``local_dem`` offline)."""
        if local_dem is not None:
            src = Path(local_dem).expanduser().resolve()
            if not src.is_file():
                raise FileNotFoundError(f"Local DEM GeoTIFF not found: {src}")
            return (src, "synthetic")

        clip_bbox = bbox.round_enclosing(step_deg=0.001)
        if not is_in_australia_coverage(clip_bbox):
            raise RuntimeError(
                f"Requested bbox {clip_bbox.as_tuple()} is outside Australia DEM bounds"
            )
        width, height = _clip_dimensions(clip_bbox, _SRTM_PIXEL_DEG, _SRTM_PIXEL_DEG)
        if clip_bbox.width <= 0 or clip_bbox.height <= 0 or width * height > _MAX_DEM_PIXELS:
            raise ValueError("Requested DEM clip exceeds the pixel limit. Use smaller bake tiles.")

        work_dir.mkdir(parents=True, exist_ok=True)
        out_tif = work_dir / "dem_raw.tif"
        if self.prefer_5m and self._fetch_5m_lidar_wcs(clip_bbox, work_dir, out_tif):
            return (out_tif, "5m-lidar")

        return (self._fetch_srtm_1s(clip_bbox, out_tif), "srtm-1s")
