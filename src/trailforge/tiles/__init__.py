"""Vector and terrain-RGB PMTiles generation pipelines (Tippecanoe, rio-rgbify, pmtiles)."""

from __future__ import annotations

from typing import Any

from trailforge.tiles.builder import (
    DEFAULT_CONTOUR_INDEX_M,
    DEFAULT_CONTOUR_INTERVAL_M,
    DEFAULT_SNOWLINE_BANDS_M,
    MANIFEST_FILENAME,
    POI_CATEGORY_TO_ICON,
    TERRAIN_PMTILES_FILENAME,
    VECTOR_PMTILES_FILENAME,
    TileBuildResult,
    TileManifest,
    build_terrain_pmtiles,
    build_tiles,
    build_vector_pmtiles,
    export_postgis_vector_layers,
    generate_contours_geojson,
    generate_snowline_bands_geojson,
)

__all__ = [
    "DEFAULT_CONTOUR_INDEX_M",
    "DEFAULT_CONTOUR_INTERVAL_M",
    "DEFAULT_SNOWLINE_BANDS_M",
    "MANIFEST_FILENAME",
    "POI_CATEGORY_TO_ICON",
    "TERRAIN_PMTILES_FILENAME",
    "TileBuildResult",
    "TileManifest",
    "VECTOR_PMTILES_FILENAME",
    "build_terrain_pmtiles",
    "build_tiles",
    "build_vector_pmtiles",
    "export_postgis_vector_layers",
    "generate_contours_geojson",
    "generate_snowline_bands_geojson",
]


def __getattr__(name: str) -> Any:
    if name == "cli":
        from trailforge.tiles import cli

        return cli
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
