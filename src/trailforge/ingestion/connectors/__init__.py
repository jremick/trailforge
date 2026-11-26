"""Australian open-data and offline synthetic corridor connectors."""

from trailforge.ingestion.connectors.base import (
    ATTRIBUTION_CAPAD,
    ATTRIBUTION_GA_DEM,
    ATTRIBUTION_GA_HYDRO,
    ATTRIBUTION_NSW_SPATIAL,
    ATTRIBUTION_OPEN_METEO,
    ATTRIBUTION_OSM,
    ATTRIBUTION_STATE_FEEDS,
    ATTRIBUTION_VIC_DEECA,
    AU_COVERAGE_BBOX,
    CORRIDOR_ALPS_BBOX,
    GDAL_STREAMING_ENV,
    NSW_COVERAGE_BBOX,
    VIC_COVERAGE_BBOX,
    BBox,
    CorridorDataset,
    RawCapadFeature,
    RawPoiFeature,
    RawTrackFeature,
    RawWaterwayFeature,
    dedupe_poi_features,
    is_in_australia_coverage,
    is_in_nsw_coverage,
    is_in_vic_coverage,
    iter_spatial_tiles,
)
from trailforge.ingestion.connectors.capad import CAPADConnector
from trailforge.ingestion.connectors.dem import (
    AUElvisDEMConnector,
    RasterCoverageStats,
    evaluate_raster_coverage_array,
    inspect_raster_coverage,
)
from trailforge.ingestion.connectors.fixtures import load_corridor_fixtures
from trailforge.ingestion.connectors.hydro import GAHydroConnector
from trailforge.ingestion.connectors.nsw import NSWTrailsConnector
from trailforge.ingestion.connectors.osm import OSMConnector
from trailforge.ingestion.connectors.vic import VICTrailsConnector

__all__ = [
    "ATTRIBUTION_CAPAD",
    "ATTRIBUTION_GA_DEM",
    "ATTRIBUTION_GA_HYDRO",
    "ATTRIBUTION_NSW_SPATIAL",
    "ATTRIBUTION_OPEN_METEO",
    "ATTRIBUTION_OSM",
    "ATTRIBUTION_STATE_FEEDS",
    "ATTRIBUTION_VIC_DEECA",
    "AU_COVERAGE_BBOX",
    "AUElvisDEMConnector",
    "BBox",
    "CAPADConnector",
    "CORRIDOR_ALPS_BBOX",
    "CorridorDataset",
    "GAHydroConnector",
    "GDAL_STREAMING_ENV",
    "NSW_COVERAGE_BBOX",
    "NSWTrailsConnector",
    "OSMConnector",
    "RasterCoverageStats",
    "RawCapadFeature",
    "RawPoiFeature",
    "RawTrackFeature",
    "RawWaterwayFeature",
    "VIC_COVERAGE_BBOX",
    "VICTrailsConnector",
    "dedupe_poi_features",
    "evaluate_raster_coverage_array",
    "inspect_raster_coverage",
    "is_in_australia_coverage",
    "is_in_nsw_coverage",
    "is_in_vic_coverage",
    "iter_spatial_tiles",
    "load_corridor_fixtures",
]
