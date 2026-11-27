"""DEM elevation sampling and canonical segment terrain draping (AHD meters).

Uses ``rasterio`` when installed (e.g., inside Docker with GDAL) and falls back cleanly to a
pure-Python + NumPy GeoTIFF reader (classic TIFF, uncompressed or DEFLATE, Predictor 1/3) so
offline tests run identically on hosts without native GDAL binaries.
"""

from __future__ import annotations

import functools
import math
import struct
import zlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal

import numpy as np
from pydantic import BaseModel, Field
from pyproj import Transformer
from shapely import transform as shapely_transform_coords
from shapely.geometry import LineString, shape
from shapely.geometry.base import BaseGeometry

METRIC_EPSG: Final[int] = 3577  # GDA94 / Australian Albers Equal Area (meters)
STORAGE_EPSG: Final[int] = 4326
DEFAULT_SNOW_LINE_M: Final[float] = 1000.0
STEEP_HIGHLIGHT_DEG: Final[float] = 14.0

_TIFF_TYPE_SIZES: Final[dict[int, tuple[int, str]]] = {
    1: (1, "B"),  # BYTE
    2: (1, "s"),  # ASCII
    3: (2, "H"),  # SHORT
    4: (4, "I"),  # LONG
    5: (8, "II"),  # RATIONAL
    7: (1, "B"),  # UNDEFINED
    8: (2, "h"),  # SSHORT
    9: (4, "i"),  # SLONG
    11: (4, "f"),  # FLOAT
    12: (8, "d"),  # DOUBLE
}


@functools.lru_cache(maxsize=16)
def _get_transformer(src_epsg: int, dst_epsg: int) -> Transformer:
    return Transformer.from_crs(f"EPSG:{src_epsg}", f"EPSG:{dst_epsg}", always_xy=True)


def _to_linestring(geom: BaseGeometry | Mapping[str, Any]) -> LineString:
    shp = shape(geom) if isinstance(geom, Mapping) else geom
    if isinstance(shp, LineString):
        return shp
    raise TypeError(f"Expected LineString geometry, got {shp.geom_type}")


def _decode_pure_python_geotiff(
    path: Path,
) -> tuple[np.ndarray, tuple[float, float, float, float], int, float | None]:
    """Decode classic GeoTIFF (uncompressed/DEFLATE, Predictor 1/3) using stdlib + NumPy.

    Returns:
        ``(data_2d_float32, (origin_x, dx, origin_y, dy_abs), crs_epsg, nodata)``
    """
    raw = path.read_bytes()
    if len(raw) < 8:
        raise ValueError(f"File too small to be a valid TIFF: {path}")

    byte_order = raw[:2]
    if byte_order == b"II":
        endian = "<"
    elif byte_order == b"MM":
        endian = ">"
    else:
        raise ValueError(f"Unsupported TIFF byte order {byte_order!r} in {path}")

    magic, ifd_offset = struct.unpack_from(f"{endian}HI", raw, 2)
    if magic != 42:
        raise ValueError(f"Unsupported TIFF magic {magic} (BigTIFF requires rasterio): {path}")

    num_entries = struct.unpack_from(f"{endian}H", raw, ifd_offset)[0]
    tags: dict[int, Any] = {}

    for idx in range(num_entries):
        entry_off = ifd_offset + 2 + idx * 12
        tag, dtype, count = struct.unpack_from(f"{endian}HHI", raw, entry_off)
        val_bytes = raw[entry_off + 8 : entry_off + 12]
        type_info = _TIFF_TYPE_SIZES.get(dtype)
        if type_info is None:
            continue
        unit_size, fmt_char = type_info
        total_bytes = unit_size * count
        if total_bytes <= 4:
            payload = val_bytes[:total_bytes]
        else:
            data_off = struct.unpack_from(f"{endian}I", val_bytes, 0)[0]
            payload = raw[data_off : data_off + total_bytes]

        if dtype == 2:
            tags[tag] = payload.rstrip(b"\x00").decode("ascii", errors="replace")
        elif fmt_char == "II":
            vals = struct.unpack_from(f"{endian}{count * 2}I", payload, 0)
            tags[tag] = [vals[2 * i] / vals[2 * i + 1] for i in range(count)]
        else:
            vals = struct.unpack_from(f"{endian}{count}{fmt_char}", payload, 0)
            tags[tag] = vals[0] if count == 1 else list(vals)

    cols = int(tags[256])
    rows = int(tags[257])
    bits_per_sample = int(tags.get(258, 32))
    compression = int(tags.get(259, 1))
    strip_offsets = tags[273]
    strip_counts = tags[279]
    if not isinstance(strip_offsets, list):
        strip_offsets = [int(strip_offsets)]
    if not isinstance(strip_counts, list):
        strip_counts = [int(strip_counts)]
    rows_per_strip = int(tags.get(278, rows))
    predictor = int(tags.get(317, 1))
    sample_format = int(tags.get(339, 1))

    if compression not in (1, 8, 32946):
        raise ValueError(
            f"Pure-Python GeoTIFF reader supports uncompressed/DEFLATE, got {compression}"
        )

    bytes_per_sample = bits_per_sample // 8
    arr = np.empty((rows, cols), dtype=np.float32)

    row_cursor = 0
    for s_off, s_cnt in zip(strip_offsets, strip_counts, strict=True):
        if row_cursor >= rows:
            break
        strip_rows = min(rows_per_strip, rows - row_cursor)
        chunk = raw[s_off : s_off + s_cnt]
        if compression in (8, 32946):
            chunk = zlib.decompress(chunk)

        row_byte_len = cols * bytes_per_sample
        if predictor == 3 and sample_format == 3 and bits_per_sample == 32:
            buf = np.frombuffer(chunk, dtype=np.uint8, count=strip_rows * row_byte_len).reshape(
                strip_rows, row_byte_len
            )
            # Undo horizontal byte-differencing per row, then re-interleave 4 IEEE big-endian planes
            undiff = (np.cumsum(buf, axis=1, dtype=np.uint32) & 0xFF).astype(np.uint8)
            planes = undiff.reshape(strip_rows, 4, cols).transpose(0, 2, 1)
            decoded = np.frombuffer(planes.tobytes(), dtype=">f4").reshape(strip_rows, cols)
            arr[row_cursor : row_cursor + strip_rows, :] = decoded.astype(np.float32, copy=False)
        elif predictor == 1:
            if sample_format == 3 and bits_per_sample == 32:
                np_dtype = f"{endian}f4"
            elif sample_format == 3 and bits_per_sample == 64:
                np_dtype = f"{endian}f8"
            elif sample_format == 2 and bits_per_sample == 16:
                np_dtype = f"{endian}i2"
            elif sample_format == 2 and bits_per_sample == 32:
                np_dtype = f"{endian}i4"
            elif sample_format == 1 and bits_per_sample == 16:
                np_dtype = f"{endian}u2"
            else:
                raise ValueError(
                    f"Unsupported sample_format={sample_format}, bits={bits_per_sample}"
                )
            decoded = np.frombuffer(chunk, dtype=np_dtype, count=strip_rows * cols).reshape(
                strip_rows, cols
            )
            arr[row_cursor : row_cursor + strip_rows, :] = decoded.astype(np.float32, copy=False)
        else:
            raise ValueError(f"Unsupported TIFF predictor={predictor} for format={sample_format}")

        row_cursor += strip_rows

    # Parse georeferencing (ModelPixelScaleTag + ModelTiepointTag)
    pixel_scale = tags.get(33550)
    tiepoints = tags.get(33922)
    if pixel_scale is None or tiepoints is None or len(pixel_scale) < 2 or len(tiepoints) < 6:
        raise ValueError(f"Missing GeoTIFF ModelPixelScale/ModelTiepoint tags in {path}")
    dx = float(pixel_scale[0])
    dy = abs(float(pixel_scale[1]))
    # Tiepoint (I, J, K, X, Y, Z) -> adjust to top-left corner (0, 0)
    origin_x = float(tiepoints[3]) - float(tiepoints[0]) * dx
    origin_y = float(tiepoints[4]) + float(tiepoints[1]) * dy

    # Parse GeoKeyDirectoryTag (34735) for EPSG code
    crs_epsg = 4326
    geokeys = tags.get(34735)
    if isinstance(geokeys, list) and len(geokeys) >= 4:
        num_keys = int(geokeys[3])
        for k_idx in range(num_keys):
            base = 4 + k_idx * 4
            if base + 3 >= len(geokeys):
                break
            key_id, tiff_tag_loc, _cnt, val_off = geokeys[base : base + 4]
            if tiff_tag_loc == 0 and key_id in (2048, 3072) and int(val_off) > 0:
                crs_epsg = int(val_off)

    nodata: float | None = None
    raw_nodata = tags.get(42113)
    if raw_nodata is not None:
        try:
            parsed_nd = float(str(raw_nodata).strip())
            if math.isfinite(parsed_nd):
                nodata = parsed_nd
        except ValueError:
            nodata = None

    return (arr, (origin_x, dx, origin_y, dy), crs_epsg, nodata)


def read_raster_bounds(path: Path | str) -> tuple[float, float, float, float]:
    """Read raster bounding box ``(min_x, min_y, max_x, max_y)`` without decoding pixels."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"DEM raster not found: {resolved}")
    try:
        import importlib

        rasterio = importlib.import_module("rasterio")

        with rasterio.open(resolved) as ds:
            b = ds.bounds
            return (float(b.left), float(b.bottom), float(b.right), float(b.top))
    except ImportError:
        with DemSampler(resolved) as sampler:
            return sampler.bounds


class DemSampler:
    """Reusable batch DEM elevation sampler with bilinear interpolation and nodata safety.

    Keeps the raster handle / decoded band open across an entire ingestion batch, automatically
    projects WGS84 (EPSG:4326) query points into the raster's native CRS, and uses ``rasterio``
    when installed with a transparent pure-Python GeoTIFF fallback otherwise.
    """

    def __init__(self, dem_path: Path | str, *, force_pure_python: bool = False) -> None:
        self.path = Path(dem_path).expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError(f"DEM GeoTIFF not found: {self.path}")

        self._rio_ds: Any = None
        self._data: np.ndarray | None = None
        self._closed = False
        self.backend: Literal["rasterio", "pure_python"] = "pure_python"

        use_rasterio = not force_pure_python
        if use_rasterio:
            try:
                import rasterio  # type: ignore[import-not-found]
            except ImportError:
                use_rasterio = False

        if use_rasterio:
            import rasterio  # type: ignore[import-not-found]

            ds = rasterio.open(self.path)
            self._rio_ds = ds
            self.backend = "rasterio"
            self.width: int = int(ds.width)
            self.height: int = int(ds.height)
            self.nodata: float | None = float(ds.nodata) if ds.nodata is not None else None
            t = ds.transform
            self._origin_x: float = float(t.c)
            self._dx: float = float(t.a)
            self._origin_y: float = float(t.f)
            self._dy: float = abs(float(t.e))
            self.crs_epsg: int = int(ds.crs.to_epsg() or 4326) if ds.crs is not None else 4326
            self._data = ds.read(1).astype(np.float32, copy=False)
        else:
            arr, (ox, dx, oy, dy), epsg, nodata = _decode_pure_python_geotiff(self.path)
            self.backend = "pure_python"
            self._data = arr
            self.height, self.width = int(arr.shape[0]), int(arr.shape[1])
            self._origin_x = ox
            self._dx = dx
            self._origin_y = oy
            self._dy = dy
            self.crs_epsg = epsg
            self.nodata = nodata

        self._to_raster_crs: Transformer | None = (
            _get_transformer(STORAGE_EPSG, self.crs_epsg) if self.crs_epsg != STORAGE_EPSG else None
        )

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """Raster bounding box ``(min_x, min_y, max_x, max_y)`` in raster CRS."""
        return (
            self._origin_x,
            self._origin_y - self.height * self._dy,
            self._origin_x + self.width * self._dx,
            self._origin_y,
        )

    def read_array(self) -> tuple[np.ndarray, float | None]:
        """Return ``(band1_float32_array, nodata)`` for coverage inspection."""
        if self._closed or self._data is None:
            raise RuntimeError("DemSampler is closed")
        return (self._data, self.nodata)

    def _is_valid_pixel(self, val: float) -> bool:
        if not math.isfinite(val):
            return False
        return not (self.nodata is not None and abs(val - self.nodata) < 1e-3)

    def sample_point(self, lon: float, lat: float) -> float | None:
        """Sample DEM elevation (meters) at WGS84 ``(lon, lat)`` using bilinear interpolation."""
        if self._closed or self._data is None:
            raise RuntimeError("DemSampler is closed")

        if self._to_raster_crs is not None:
            x, y = self._to_raster_crs.transform(lon, lat)
        else:
            x, y = lon, lat

        # Continuous pixel index relative to RasterPixelIsArea pixel centers (c + 0.5, r + 0.5)
        col_area = (x - self._origin_x) / self._dx
        row_area = (self._origin_y - y) / self._dy
        if col_area < 0.0 or col_area > self.width or row_area < 0.0 or row_area > self.height:
            return None

        col_f = max(0.0, min(self.width - 1.0, col_area - 0.5))
        row_f = max(0.0, min(self.height - 1.0, row_area - 0.5))

        c0 = int(math.floor(col_f))
        r0 = int(math.floor(row_f))
        c1 = min(self.width - 1, c0 + 1)
        r1 = min(self.height - 1, r0 + 1)
        fc = col_f - c0
        fr = row_f - r0

        corners = (
            (float(self._data[r0, c0]), (1.0 - fr) * (1.0 - fc)),
            (float(self._data[r0, c1]), (1.0 - fr) * fc),
            (float(self._data[r1, c0]), fr * (1.0 - fc)),
            (float(self._data[r1, c1]), fr * fc),
        )
        weighted_sum = 0.0
        total_w = 0.0
        for val, w in corners:
            if w > 0.0 and self._is_valid_pixel(val):
                weighted_sum += val * w
                total_w += w

        if total_w <= 0.0:
            # Exact-corner tie-break when bilinear weights on valid pixels were 0
            nearest_val = float(self._data[int(round(row_f)), int(round(col_f))])
            return nearest_val if self._is_valid_pixel(nearest_val) else None

        return weighted_sum / total_w

    def sample_points(self, coords: Sequence[tuple[float, float]]) -> list[float | None]:
        """Sample DEM elevation for a sequence of WGS84 ``(lon, lat)`` tuples."""
        return [self.sample_point(lon, lat) for lon, lat in coords]

    def close(self) -> None:
        if not self._closed:
            if self._rio_ds is not None:
                self._rio_ds.close()
                self._rio_ds = None
            self._data = None
            self._closed = True

    def __enter__(self) -> DemSampler:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class SegmentTerrainMetrics(BaseModel):
    """DEM-derived terrain, slope, relief, and elevation profile metrics for a canonical segment."""

    length_m: float = Field(..., ge=0.0)
    min_elev_m: float | None = None
    max_elev_m: float | None = None
    elev_start_m: float | None = None
    elev_end_m: float | None = None
    elev_gain_m: float = 0.0
    elev_loss_m: float = 0.0
    max_slope_deg: float | None = None
    sustained_slope_deg: float | None = None
    is_steep_highlight: bool = False
    is_above_snowline: bool = False
    elevation_profile: list[list[float]] = Field(default_factory=list)


def _compute_windowed_slopes(
    samples: list[tuple[float, float]], window_m: float
) -> list[float]:
    """Compute slope angles (degrees) across sample pairs separated by ``>= window_m``."""
    n = len(samples)
    if n < 2:
        return []
    total_d = samples[-1][0] - samples[0][0]
    if total_d <= 0.0:
        return []

    effective_win = min(window_m, total_d)
    slopes: list[float] = []
    j = 1
    for i in range(n - 1):
        d_i, z_i = samples[i]
        while j < n - 1 and (samples[j][0] - d_i) < effective_win - 1e-6:
            j += 1
        dd = samples[j][0] - d_i
        if dd >= effective_win - 1e-6 and dd > 0.0:
            dz = abs(samples[j][1] - z_i)
            slopes.append(math.degrees(math.atan2(dz, dd)))
    if not slopes:
        dz = abs(samples[-1][1] - samples[0][1])
        slopes.append(math.degrees(math.atan2(dz, total_d)))
    return slopes


def drape_segment_dem(
    geom_wgs84: BaseGeometry | Mapping[str, Any],
    sampler: DemSampler,
    *,
    sample_step_m: float = 10.0,
    snow_line_m: float = DEFAULT_SNOW_LINE_M,
) -> SegmentTerrainMetrics:
    """Drape a canonical WGS84 segment onto ``sampler`` and compute slope & relief metrics.

    Implements ``docs/SPEC_GIS_SCHEMA.md`` Section 3.4:
      - Samples elevation every ``sample_step_m`` (default 10 m, min 3 points: start/mid/end)
        along the ``EPSG:3577`` metric line.
      - ``max_slope_deg``: max local slope over rolling ``>= 20 m`` baseline (suppresses
        single-pixel DEM noise while accurately capturing steep >=18 deg climbs).
      - ``sustained_slope_deg``: max of end-to-end slope and 85th-percentile rolling ``>= 40 m``
        slope.
      - ``is_steep_highlight``: ``max_slope_deg >= 14.0``.
      - ``is_above_snowline``: ``max_elev_m >= snow_line_m`` (default 1,000 m).
    """
    line_wgs84 = _to_linestring(geom_wgs84)
    to_metric = _get_transformer(STORAGE_EPSG, METRIC_EPSG)
    to_wgs84 = _get_transformer(METRIC_EPSG, STORAGE_EPSG)

    def _to_albers_arr(coords: np.ndarray) -> np.ndarray:
        if coords.size == 0:
            return coords
        xs, ys = to_metric.transform(coords[:, 0], coords[:, 1])
        return np.column_stack((xs, ys))

    line_m = shapely_transform_coords(line_wgs84, _to_albers_arr)
    length_m = float(line_m.length)
    if length_m <= 0.0:
        return SegmentTerrainMetrics(length_m=0.0)

    step = max(2.0, float(sample_step_m))
    num_intervals = max(2, int(math.ceil(length_m / step)))
    distances = [length_m * i / num_intervals for i in range(num_intervals + 1)]

    sample_pts_wgs84: list[tuple[float, float]] = []
    for d in distances:
        pt_m = line_m.interpolate(d)
        lon, lat = to_wgs84.transform(pt_m.x, pt_m.y)
        sample_pts_wgs84.append((float(lon), float(lat)))

    elevs = sampler.sample_points(sample_pts_wgs84)
    valid_samples: list[tuple[float, float]] = [
        (d, z) for d, z in zip(distances, elevs, strict=True) if z is not None
    ]

    if not valid_samples:
        return SegmentTerrainMetrics(length_m=round(length_m, 2))

    z_vals = [z for _, z in valid_samples]
    min_elev = min(z_vals)
    max_elev = max(z_vals)
    elev_start = valid_samples[0][1]
    elev_end = valid_samples[-1][1]

    gain = 0.0
    loss = 0.0
    for idx in range(1, len(valid_samples)):
        delta = valid_samples[idx][1] - valid_samples[idx - 1][1]
        if delta > 0.0:
            gain += delta
        else:
            loss += -delta

    slopes_20 = _compute_windowed_slopes(valid_samples, window_m=20.0)
    max_slope = max(slopes_20) if slopes_20 else 0.0

    end_to_end_dd = valid_samples[-1][0] - valid_samples[0][0]
    end_to_end_slope = (
        math.degrees(math.atan2(abs(elev_end - elev_start), end_to_end_dd))
        if end_to_end_dd > 0.0
        else 0.0
    )
    slopes_40 = _compute_windowed_slopes(valid_samples, window_m=40.0)
    p85_slope = float(np.percentile(slopes_40, 85)) if slopes_40 else end_to_end_slope
    sustained_slope = max(end_to_end_slope, p85_slope)

    profile = [[round(d, 1), round(z, 2)] for d, z in valid_samples]

    return SegmentTerrainMetrics(
        length_m=round(length_m, 2),
        min_elev_m=round(min_elev, 2),
        max_elev_m=round(max_elev, 2),
        elev_start_m=round(elev_start, 2),
        elev_end_m=round(elev_end, 2),
        elev_gain_m=round(gain, 2),
        elev_loss_m=round(loss, 2),
        max_slope_deg=round(max_slope, 2),
        sustained_slope_deg=round(sustained_slope, 2),
        is_steep_highlight=max_slope >= STEEP_HIGHLIGHT_DEG,
        is_above_snowline=max_elev >= snow_line_m,
        elevation_profile=profile,
    )
