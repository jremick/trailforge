"""Waterway crossing detection and CAPAD protected area tenure lookup (EPSG:3577 metric)."""

from __future__ import annotations

import functools
from collections.abc import Mapping, Sequence
from typing import Any, Final

import numpy as np
from pydantic import BaseModel, Field
from pyproj import Transformer
from shapely import transform as shapely_transform_coords
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

from trailforge.ingestion.connectors.base import RawCapadFeature, RawWaterwayFeature

STORAGE_EPSG: Final[int] = 4326
METRIC_EPSG: Final[int] = 3577


@functools.lru_cache(maxsize=8)
def _get_transformer(src_epsg: int, dst_epsg: int) -> Transformer:
    return Transformer.from_crs(f"EPSG:{src_epsg}", f"EPSG:{dst_epsg}", always_xy=True)


def _project_geom(geom: BaseGeometry, transformer: Transformer) -> BaseGeometry:
    def _transform_arr(coords: np.ndarray) -> np.ndarray:
        if coords.size == 0:
            return coords
        xs, ys = transformer.transform(coords[:, 0], coords[:, 1])
        return np.column_stack((xs, ys))

    return shapely_transform_coords(geom, _transform_arr)


def _to_shapely(geom: BaseGeometry | Mapping[str, Any]) -> BaseGeometry:
    return shape(geom) if isinstance(geom, Mapping) else geom


class WaterwayCrossingDetail(BaseModel):
    """Details of a single waterway intersection along a canonical segment."""

    waterway_id: str
    name: str | None = None
    feature_type: str
    perenniality: str
    hierarchy: str | None = None
    crossing_point: tuple[float, float] | None = None
    is_perennial: bool = False
    is_major: bool = False


class CrossingSummary(BaseModel):
    """Aggregated waterway crossing statistics for a canonical segment."""

    total_count: int = 0
    unbridged_count: int = 0
    has_perennial_crossing: bool = False
    has_major_perennial_crossing: bool = False
    crossings: list[WaterwayCrossingDetail] = Field(default_factory=list)
    waterway_names: list[str] = Field(default_factory=list)


def count_waterway_crossings(
    geom_wgs84: BaseGeometry | Mapping[str, Any],
    waterways: Sequence[RawWaterwayFeature],
    *,
    is_bridged: bool = False,
    buffer_m: float = 1.0,
) -> CrossingSummary:
    """Intersect a WGS84 segment against GA/OSM waterways in EPSG:3577 meters.

    - Defensively ignores synthetic GA Surface Hydrology ``featuretype == 'Connector'`` rows.
    - When ``is_bridged=True``, records physical ``total_count`` intersections for transparency
      while setting ``unbridged_count = 0`` and ``has_perennial_crossing = False`` so bridged
      creek crossings do not incur off-road water-crossing grading penalties.
    """
    seg_wgs84 = _to_shapely(geom_wgs84)
    if seg_wgs84.is_empty or not waterways:
        return CrossingSummary()

    to_metric = _get_transformer(STORAGE_EPSG, METRIC_EPSG)
    to_wgs84 = _get_transformer(METRIC_EPSG, STORAGE_EPSG)

    seg_m = _project_geom(seg_wgs84, to_metric)
    seg_test_m = seg_m.buffer(buffer_m) if buffer_m > 0.0 else seg_m

    details: list[WaterwayCrossingDetail] = []
    names_seen: list[str] = []

    for ww in waterways:
        if ww.feature_type.strip().lower() == "connector":
            continue
        ww_wgs84 = ww.to_shapely()
        if ww_wgs84.is_empty:
            continue
        ww_m = _project_geom(ww_wgs84, to_metric)
        if not seg_test_m.intersects(ww_m):
            continue

        inter = seg_m.intersection(ww_m)
        if inter.is_empty:
            inter = seg_test_m.intersection(ww_m)

        pt_wgs84: tuple[float, float] | None = None
        if not inter.is_empty:
            centroid = inter.centroid if not isinstance(inter, Point) else inter
            lon, lat = to_wgs84.transform(centroid.x, centroid.y)
            pt_wgs84 = (round(float(lon), 6), round(float(lat), 6))

        is_perennial = ww.perenniality.strip().lower() == "perennial"
        is_major = (ww.hierarchy or "").strip().lower() == "major"
        details.append(
            WaterwayCrossingDetail(
                waterway_id=ww.source_feature_id,
                name=ww.name,
                feature_type=ww.feature_type,
                perenniality=ww.perenniality,
                hierarchy=ww.hierarchy,
                crossing_point=pt_wgs84,
                is_perennial=is_perennial,
                is_major=is_major,
            )
        )
        if ww.name and ww.name not in names_seen:
            names_seen.append(ww.name)

    total_count = len(details)
    if is_bridged:
        return CrossingSummary(
            total_count=total_count,
            unbridged_count=0,
            has_perennial_crossing=False,
            has_major_perennial_crossing=False,
            crossings=details,
            waterway_names=names_seen,
        )

    return CrossingSummary(
        total_count=total_count,
        unbridged_count=total_count,
        has_perennial_crossing=any(c.is_perennial for c in details),
        has_major_perennial_crossing=any(c.is_perennial and c.is_major for c in details),
        crossings=details,
        waterway_names=names_seen,
    )


def find_capad_tenure(
    geom_wgs84: BaseGeometry | Mapping[str, Any],
    parks: Sequence[RawCapadFeature],
) -> dict[str, Any] | None:
    """Resolve the dominant CAPAD 2025 protected area tenure for a WGS84 geometry.

    Checks midpoint coverage first (fast path), falling back to maximum intersection length in
    ``EPSG:3577`` if a segment crosses a border seam (e.g., Murray River between Alpine NP and
    Kosciuszko NP).
    """
    shp = _to_shapely(geom_wgs84)
    if shp.is_empty or not parks:
        return None

    midpoint = (
        shp.interpolate(0.5, normalized=True) if shp.geom_type == "LineString" else shp.centroid
    )

    def _to_dict(park: RawCapadFeature) -> dict[str, Any]:
        return {
            "source_feature_id": park.source_feature_id,
            "name": park.name,
            "park_type": park.park_type,
            "type_abbr": park.type_abbr,
            "iucn": park.iucn,
            "state": park.state,
        }

    # Fast path: midpoint strictly inside a park polygon
    for park in parks:
        pgeom = park.to_shapely()
        if pgeom.contains(midpoint):
            return _to_dict(park)

    # Border-seam / polygon fallback: choose park with largest metric intersection
    to_metric = _get_transformer(STORAGE_EPSG, METRIC_EPSG)
    shp_m = _project_geom(shp, to_metric)

    best_park: RawCapadFeature | None = None
    best_overlap = 0.0
    for park in parks:
        pgeom = park.to_shapely()
        if not pgeom.intersects(shp):
            continue
        pgeom_m = _project_geom(pgeom, to_metric)
        inter = shp_m.intersection(pgeom_m)
        measure = float(inter.length) if inter.length > 0.0 else float(inter.area)
        if measure > best_overlap or (best_park is None and not inter.is_empty):
            best_overlap = measure
            best_park = park

    return _to_dict(best_park) if best_park is not None else None
