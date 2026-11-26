"""Multi-track route planning engine and GridForge-compatible GPX/GeoJSON exporters."""

from trailforge.routes.export import (
    ROUTE_EXPORT_ATTRIBUTIONS,
    build_route_geojson,
    build_route_gpx,
)
from trailforge.routes.planner import (
    GRADE_SPEED_KMH,
    LEG_GAP_THRESHOLD_M,
    MAX_PROFILE_POINTS,
    MAX_ROUTE_TRACKS,
    OrientedLegTrack,
    PlannedRouteAnalysis,
    RouteElevationPoint,
    RouteLegGap,
    RouteLegSummary,
    RouteSafetySummary,
    RouteWaterwayCrossing,
    RouteWaypoint,
    TrackNotFoundError,
    build_gridforge_cli,
    plan_route,
    plan_route_with_geometry,
    slugify_route_name,
)

__all__ = [
    "GRADE_SPEED_KMH",
    "LEG_GAP_THRESHOLD_M",
    "MAX_PROFILE_POINTS",
    "MAX_ROUTE_TRACKS",
    "OrientedLegTrack",
    "PlannedRouteAnalysis",
    "ROUTE_EXPORT_ATTRIBUTIONS",
    "RouteElevationPoint",
    "RouteLegGap",
    "RouteLegSummary",
    "RouteSafetySummary",
    "RouteWaterwayCrossing",
    "RouteWaypoint",
    "TrackNotFoundError",
    "build_gridforge_cli",
    "build_route_geojson",
    "build_route_gpx",
    "plan_route",
    "plan_route_with_geometry",
    "slugify_route_name",
]
