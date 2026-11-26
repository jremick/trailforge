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
from pathlib import Path
from typing import Final, Literal

import httpx
import numpy as np
from pydantic import BaseModel

from trailforge.ingestion.connectors.base import (
    ATTRIBUTION_GA_DEM,
    GDAL_STREAMING_ENV,
    USER_AGENT,
    BBox,
    DemTierLiteral,
    is_in_australia_coverage,
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

    with DemSampler(tif_path) as sampler:
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
        return env

    def _srtm_cog_url(self) -> str:
        if self.srtm_product == "hydro":
            return os.environ.get("TRAILFORGE_DEA_SRTM_HYDRO_URL", DEA_SRTM_1S_HYDRO_COG_URL)
        return os.environ.get("TRAILFORGE_DEA_SRTM_URL", DEA_SRTM_1S_SMOOTH_COG_URL)

    def _http_get_bytes(self, url: str, params: dict[str, str]) -> tuple[int, str, bytes]:
        owns_client = self._client is None
        http = (
            self._client
            if self._client is not None
            else httpx.Client(
                timeout=httpx.Timeout(self.timeout_s, connect=min(10.0, self.timeout_s)),
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
        )
        try:
            resp = http.get(url, params=params, headers={"User-Agent": USER_AGENT})
            return (resp.status_code, resp.headers.get("content-type", ""), resp.content)
        finally:
            if owns_client:
                http.close()

    def _fetch_5m_lidar_wcs(self, clip_bbox: BBox, work_dir: Path, out_tif: Path) -> bool:
        # Guard WCS pixel volume: only attempt 5m LiDAR on compact <= 1 deg tiles
        if clip_bbox.width > 1.0 or clip_bbox.height > 1.0:
            return False

        resx, resy, res_tag = wcs_resolution_for_bbox(clip_bbox)
        cache_path = (
            self.cache_dir / "au_elvis" / f"au_5m_{res_tag}_{clip_bbox.cache_hash()}.tif"
            if self.cache_dir is not None
            else None
        )
        if cache_path is not None and cache_path.is_file() and cache_path.stat().st_size > 0:
            shutil.copy2(cache_path, out_tif)
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
        try:
            status, content_type, body = self._http_get_bytes(GA_5M_LIDAR_WCS_URL, params)
        except Exception as exc:
            logger.warning("GA 5m LiDAR WCS failed (%s); falling back to SRTM 1s", exc)
            return False

        if (
            status != 200
            or "xml" in content_type.lower()
            or "html" in content_type.lower()
            or len(body) < 256
        ):
            return False

        work_dir.mkdir(parents=True, exist_ok=True)
        tmp_5m = work_dir / "dem_au_5m_tmp.tif"
        tmp_5m.write_bytes(body)
        try:
            stats = inspect_raster_coverage(
                tmp_5m,
                min_valid_ratio=self.min_valid_ratio,
                max_exact_zero_ratio=self.max_exact_zero_ratio,
            )
        except Exception as exc:
            logger.warning("GA 5m LiDAR GeoTIFF unreadable (%s); falling back to SRTM 1s", exc)
            tmp_5m.unlink(missing_ok=True)
            return False

        if not stats.accept_5m_lidar:
            logger.info(
                "GA 5m LiDAR rejected (valid=%.1f%%, zero=%.1f%%, cliff_ratio=%.1f%%); "
                "falling back to DEA SRTM 1s",
                stats.valid_frac * 100.0,
                stats.exact_zero_frac * 100.0,
                stats.zero_cliff_ratio * 100.0,
            )
            tmp_5m.unlink(missing_ok=True)
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
            status, content_type, body = self._http_get_bytes(GA_SRTM_1S_WCS_URL, params)
        except Exception as exc:
            logger.warning("GA SRTM 1s backup WCS failed: %s", exc)
            return False
        if (
            status != 200
            or "xml" in content_type.lower()
            or "html" in content_type.lower()
            or len(body) < 256
        ):
            return False
        out_tif.write_bytes(body)
        return True

    def _fetch_srtm_1s(self, clip_bbox: BBox, out_tif: Path) -> Path:
        cache_path = (
            self.cache_dir
            / "au_elvis"
            / f"au_srtm1s_{self.srtm_product}_{clip_bbox.cache_hash()}.tif"
            if self.cache_dir is not None
            else None
        )
        if cache_path is not None and cache_path.is_file() and cache_path.stat().st_size > 0:
            shutil.copy2(cache_path, out_tif)
            return out_tif

        vsicurl_cog = f"/vsicurl/{self._srtm_cog_url()}"
        cmd = [
            "gdal_translate",
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
            vsicurl_cog,
            str(out_tif),
        ]
        gdal_ok = False
        if shutil.which("gdal_translate") is not None:
            proc = subprocess.run(
                cmd, env=self._gdal_env(), capture_output=True, text=True, check=False
            )
            gdal_ok = proc.returncode == 0 and out_tif.is_file() and out_tif.stat().st_size > 0

        if not gdal_ok and not self._fetch_srtm_1s_wcs_backup(clip_bbox, out_tif):
            raise RuntimeError(
                "Both DEA SRTM 1s COG and GA SRTM 2024 WCS backup failed for "
                f"{clip_bbox.as_tuple()}"
            )

        stats = inspect_raster_coverage(out_tif)
        if stats.valid_frac < 0.05:
            out_tif.unlink(missing_ok=True)
            raise RuntimeError(
                f"Outside Australian DEM coverage (valid={stats.valid_frac * 100.0:.1f}%)"
            )

        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_cache = cache_path.with_suffix(".tif.tmp")
            shutil.copy2(out_tif, tmp_cache)
            tmp_cache.replace(cache_path)
        return out_tif

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

        work_dir.mkdir(parents=True, exist_ok=True)
        clip_bbox = bbox.round_enclosing(step_deg=0.001)
        if not is_in_australia_coverage(clip_bbox):
            raise RuntimeError(
                f"Requested bbox {clip_bbox.as_tuple()} is outside Australia DEM bounds"
            )

        out_tif = work_dir / "dem_raw.tif"
        if self.prefer_5m and self._fetch_5m_lidar_wcs(clip_bbox, work_dir, out_tif):
            return (out_tif, "5m-lidar")

        return (self._fetch_srtm_1s(clip_bbox, out_tif), "srtm-1s")
