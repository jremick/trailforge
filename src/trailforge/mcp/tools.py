"""All 18 TrailForge MCP tools (9 public, 3 app-only, 3 personal read, 3 private write).

Strictly enforces:
  - Anonymous-first public trail intelligence (SC-2)
  - SEP-1865 UI metadata (`_meta.ui.resourceUri`, `visibility: ["app"]`) & Antigravity `aux_pane`
  - Pure-Pillow PNG fallbacks `< 120 KB` & `< 450 KB` total `CallToolResult` cap (SC-5)
  - Strict `user_id` ownership isolation, scope checks, zero PII leakage, zero condition-report
    writes, and zero admin operations (SC-8)
  - Mandatory OSM ODbL, GA/DEA, CAPAD, state agency & Open-Meteo attributions (SC-12)
"""

from __future__ import annotations

import base64
import json
import math
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final

from fastapi import HTTPException
from geoalchemy2.shape import from_shape
from mcp import types
from shapely.geometry import Point
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from trailforge.api.tracks import (
    TrackDetailResponse,
    get_track_detail,
    list_tracks,
)
from trailforge.db.models import (
    POI,
    CAPADPark,
    OfflinePack,
    PlannedRoute,
    RainfallSnapshot,
    Track,
    TrackSegment,
    TrackSegmentGrade,
    Trip,
    User,
    UserFavorite,
    UserPin,
    VehicleProfile,
)
from trailforge.feeds.queries import list_closures
from trailforge.grading.live import get_live_summaries_for_tracks
from trailforge.mcp.context import (
    SCOPE_READ,
    SCOPE_WRITE,
    McpAuthError,
    get_mcp_auth_context,
    get_mcp_auth_mode,
    get_mcp_public_base_url,
    get_mcp_settings,
    open_mcp_db_session,
    require_mcp_auth,
)
from trailforge.mcp.renderer import (
    DEFAULT_ATTRIBUTION_FOOTER,
    render_comparison_png,
    render_elevation_profile_png,
    render_map_card_png,
)
from trailforge.mcp.resources import MCP_APP_URI
from trailforge.mcp.schemas import (
    COMPARE_COLORS,
    DEFAULT_SIMPLIFY_TOLERANCE_DEG,
    LARGE_ROUTE_SIMPLIFY_TOLERANCE_DEG,
    CompactClosureItem,
    CompactCrossingItem,
    CompactElevationPoint,
    CompactLiveSummary,
    CompactParkRef,
    CompactPoiItem,
    CompactRainfallSummary,
    CompactReportItem,
    CompactSegmentGeom,
    CompactSuitability,
    CompactTrackCard,
    CompareTrackEntry,
    CompareViewData,
    ExploreViewData,
    RigContext,
    RouteLegData,
    RouteSafetySummaryData,
    RouteViewData,
    RouteWaypointData,
    TileConfig,
    TrackViewData,
    TrailForgeViewPayload,
    build_area_deep_link,
    build_mcp_attributions,
    build_route_deep_link,
    build_track_deep_link,
    compute_bbox_from_coords,
    downsample_compact_profile,
    enforce_call_tool_result_budget,
    format_attribution_markdown,
    simplify_linestring_coords,
)
from trailforge.packs.catalog import PACK_REGIONS, PackRegionSpec, list_pack_regions
from trailforge.reports.queries import list_reports
from trailforge.routes.export import build_route_geojson, build_route_gpx
from trailforge.routes.planner import (
    MAX_ROUTE_TRACKS,
    PlannedRouteAnalysis,
    RouteWaypoint,
    TrackNotFoundError,
    build_gridforge_cli,
    plan_route_with_geometry,
    slugify_route_name,
)
from trailforge.vehicles.classifier import (
    VALID_CAPABILITY_CLASSES,
    CapabilityClassLiteral,
    TrackSuitability,
    normalize_capability_class,
)

_VALID_GRADES: Final[tuple[str, ...]] = ("Easy", "Medium", "Hard", "Extreme")
_VALID_PIN_CATEGORIES: Final[tuple[str, ...]] = (
    "waypoint",
    "camp",
    "hazard",
    "photo",
    "water",
    "recovery",
)
_MAX_INLINE_EXPORT_BYTES: Final[int] = 120 * 1024  # 120 KB inline GPX/GeoJSON cap

PUBLIC_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "search_trails",
        "inspect_track",
        "plan_route",
        "compare_tracks",
        "check_area_conditions",
        "find_pois_near",
        "export_route_file",
        "list_regions_and_packs",
        "open_trailforge_map",
    }
)
APP_ONLY_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "app_get_track_detail",
        "app_plan_route_preview",
        "app_search_viewport",
    }
)
PERSONAL_READ_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "get_my_garage",
        "list_my_saved_routes",
        "list_my_trips_and_pins",
    }
)
PRIVATE_WRITE_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "save_planned_route",
        "save_personal_pin",
        "toggle_favorite_track",
    }
)
ALL_TOOL_NAMES: Final[frozenset[str]] = (
    PUBLIC_TOOL_NAMES | APP_ONLY_TOOL_NAMES | PERSONAL_READ_TOOL_NAMES | PRIVATE_WRITE_TOOL_NAMES
)

_READ_ONLY_ANNOTATIONS = types.ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_WRITE_ANNOTATIONS = types.ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)

_UI_META: Final[dict[str, Any]] = {"ui": {"resourceUri": MCP_APP_URI}}
_APP_ONLY_UI_META: Final[dict[str, Any]] = {
    "ui": {"resourceUri": MCP_APP_URI, "visibility": ["app"]}
}
_OPEN_MAP_META: Final[dict[str, Any]] = {
    "ui": {"resourceUri": MCP_APP_URI},
    "com.google.antigravity/ui": {"entrypoints": [{"type": "aux_pane", "title": "TrailForge Map"}]},
}


# =============================================================================
# Tool Catalog Definitions (All 18 Tools)
# =============================================================================


def _build_all_tool_definitions() -> list[types.Tool]:
    """Construct canonical MCP ``Tool`` metadata for all 18 TrailForge tools."""
    vehicle_class_schema: dict[str, Any] = {
        "type": "string",
        "enum": list(VALID_CAPABILITY_CLASSES),
        "description": (
            "Vehicle capability class override (AWD, Stock 4WD, Touring 4WD, Modified 4WD)."
        ),
    }
    bbox_schema: dict[str, Any] = {
        "type": "array",
        "items": {"type": "number"},
        "minItems": 4,
        "maxItems": 4,
        "description": "WGS84 bounding box [min_lon, min_lat, max_lon, max_lat].",
    }
    waypoint_schema: dict[str, Any] = {
        "type": "object",
        "required": ["name", "lon", "lat"],
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 120},
            "category": {"type": "string", "default": "waypoint"},
            "lon": {"type": "number", "minimum": -180, "maximum": 180},
            "lat": {"type": "number", "minimum": -90, "maximum": 90},
        },
    }

    return [
        # ---- A. Public Read & Planning Tools (9) ----
        types.Tool(
            name="search_trails",
            title="Search Australian 4WD Trails",
            description=(
                "Search canonical TrailForge 4WD tracks by name, state, region, bounding box, "
                "proximity, difficulty grade, open status, nearby POI types, and rig suitability."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Track name or keyword."},
                    "state": {
                        "type": "string",
                        "description": "Australian state code (e.g. VIC, NSW, ACT).",
                    },
                    "region": {
                        "type": "string",
                        "description": "Region ID or name (e.g. australian-alps, High Country).",
                    },
                    "bbox": bbox_schema,
                    "near_lat": {"type": "number", "minimum": -90, "maximum": 90},
                    "near_lon": {"type": "number", "minimum": -180, "maximum": 180},
                    "radius_km": {"type": "number", "minimum": 1, "maximum": 150, "default": 40},
                    "grades": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(_VALID_GRADES)},
                    },
                    "open_only": {"type": "boolean", "default": False},
                    "vehicle_class": vehicle_class_schema,
                    "poi_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Filter tracks with nearby POIs of these types (e.g. campground, hut)."
                        ),
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
                    "include_images": {"type": "boolean", "default": False},
                },
            },
            annotations=_READ_ONLY_ANNOTATIONS,
            _meta=_UI_META,
        ),
        types.Tool(
            name="inspect_track",
            title="Inspect 4WD Track Detail",
            description=(
                "Inspect a canonical 4WD track by track_id or track_name (fuzzy-resolves unique "
                "name or returns disambiguation list). Returns explainable grading breakdown, "
                "live telemetry, 72 h rainfall wet-risk, closures, gate warnings, crossings, "
                "nearby POIs, elevation profile, and PNG visuals."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "track_id": {"type": "string", "description": "Canonical TrailForge track_id."},
                    "track_name": {
                        "type": "string",
                        "description": "Track name (fuzzy-matched if track_id omitted).",
                    },
                    "vehicle_class": vehicle_class_schema,
                    "snowline_m": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 3000,
                        "default": 1000,
                    },
                    "include_images": {"type": "boolean", "default": True},
                },
            },
            annotations=_READ_ONLY_ANNOTATIONS,
            _meta=_UI_META,
        ),
        types.Tool(
            name="plan_route",
            title="Plan Multi-Track 4WD Route",
            description=(
                "Stitch 1..30 canonical tracks end-to-end with automatic orientation, leg gap "
                "detection, combined elevation profile, consolidated closures/gates/crossings, "
                "rig suitability, GridForge 300 DPI CLI, and PNG visuals."
            ),
            inputSchema={
                "type": "object",
                "required": ["track_ids"],
                "properties": {
                    "track_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": MAX_ROUTE_TRACKS,
                    },
                    "name": {"type": "string", "maxLength": 160},
                    "vehicle_class": vehicle_class_schema,
                    "waypoints": {"type": "array", "items": waypoint_schema, "maxItems": 100},
                    "snowline_m": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 3000,
                        "default": 1000,
                    },
                    "include_images": {"type": "boolean", "default": True},
                },
            },
            annotations=_READ_ONLY_ANNOTATIONS,
            _meta=_UI_META,
        ),
        types.Tool(
            name="compare_tracks",
            title="Compare 2–4 4WD Tracks",
            description=(
                "Compare 2 to 4 canonical tracks side-by-side on difficulty, slope, elevation "
                "profile, waterway crossings, closures, wet-risk, and rig suitability with an "
                "overlaid elevation comparison PNG."
            ),
            inputSchema={
                "type": "object",
                "required": ["track_ids"],
                "properties": {
                    "track_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 4,
                    },
                    "vehicle_class": vehicle_class_schema,
                    "include_images": {"type": "boolean", "default": True},
                },
            },
            annotations=_READ_ONLY_ANNOTATIONS,
            _meta=_UI_META,
        ),
        types.Tool(
            name="check_area_conditions",
            title="Check Area Closures, Hazards & Rainfall",
            description=(
                "Check active official agency closures, community condition reports and locked "
                "gate warnings, and 72-hour rainfall wet-risk for a state, region, park, bbox, "
                "or radius."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "state": {"type": "string"},
                    "region": {"type": "string"},
                    "park_name": {"type": "string"},
                    "bbox": bbox_schema,
                    "near_lat": {"type": "number", "minimum": -90, "maximum": 90},
                    "near_lon": {"type": "number", "minimum": -180, "maximum": 180},
                    "radius_km": {"type": "number", "minimum": 1, "maximum": 250, "default": 50},
                    "include_images": {"type": "boolean", "default": True},
                },
            },
            annotations=_READ_ONLY_ANNOTATIONS,
            _meta=_UI_META,
        ),
        types.Tool(
            name="find_pois_near",
            title="Find Backcountry POIs Nearby",
            description=(
                "Find campgrounds, alpine huts, water points, lookouts, trig stations, trailheads, "
                "or gates near a track, coordinate, or bounding box."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "track_id": {"type": "string"},
                    "lat": {"type": "number", "minimum": -90, "maximum": 90},
                    "lon": {"type": "number", "minimum": -180, "maximum": 180},
                    "radius_km": {"type": "number", "minimum": 0.5, "maximum": 50, "default": 10},
                    "bbox": bbox_schema,
                    "poi_types": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 15},
                },
            },
            annotations=_READ_ONLY_ANNOTATIONS,
            _meta=_UI_META,
        ),
        types.Tool(
            name="export_route_file",
            title="Export Route as GPX 1.1 or GeoJSON",
            description=(
                "Export 1..30 ordered canonical tracks as GridForge-compatible GPX 1.1 or RFC 7946 "
                "GeoJSON (returns inline content if <= 120 KB, direct export URL, and GridForge "
                "300 DPI strip-atlas CLI)."
            ),
            inputSchema={
                "type": "object",
                "required": ["track_ids"],
                "properties": {
                    "track_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": MAX_ROUTE_TRACKS,
                    },
                    "name": {"type": "string", "maxLength": 160},
                    "format": {"type": "string", "enum": ["gpx", "geojson"], "default": "gpx"},
                    "waypoints": {"type": "array", "items": waypoint_schema, "maxItems": 100},
                },
            },
            annotations=_READ_ONLY_ANNOTATIONS,
        ),
        types.Tool(
            name="list_regions_and_packs",
            title="List Australian Regions & Offline Packs",
            description=(
                "List TrailForge's 15 curated Australian 4WD regions and statewide offline packs "
                "with bounding boxes, states, UTM zones, build status, and attributions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "description": "Optional AU state filter (e.g. VIC, NSW).",
                    },
                },
            },
            annotations=_READ_ONLY_ANNOTATIONS,
        ),
        types.Tool(
            name="open_trailforge_map",
            title="Open TrailForge Interactive Map",
            description=(
                "Open the TrailForge interactive map explorer panel (accepts empty {} to open "
                "the default corridor, or optional region, track_id, or track_ids)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "region": {"type": "string"},
                    "track_id": {"type": "string"},
                    "track_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": MAX_ROUTE_TRACKS,
                    },
                },
            },
            annotations=_READ_ONLY_ANNOTATIONS,
            _meta=_OPEN_MAP_META,
        ),
        # ---- B. App-Only Interactive Tools (3, visibility: ["app"]) ----
        types.Tool(
            name="app_get_track_detail",
            title="App: Load Track Detail",
            description="App-only tool returning compact TrackViewData for ui://trailforge/explorer.html.",
            inputSchema={
                "type": "object",
                "required": ["track_id"],
                "properties": {
                    "track_id": {"type": "string"},
                    "vehicle_class": vehicle_class_schema,
                    "snowline_m": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 3000,
                        "default": 1000,
                    },
                },
            },
            annotations=_READ_ONLY_ANNOTATIONS,
            _meta=_APP_ONLY_UI_META,
        ),
        types.Tool(
            name="app_plan_route_preview",
            title="App: Plan Route Preview",
            description="App-only tool returning compact RouteViewData for ui://trailforge/explorer.html.",
            inputSchema={
                "type": "object",
                "required": ["track_ids"],
                "properties": {
                    "track_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": MAX_ROUTE_TRACKS,
                    },
                    "name": {"type": "string", "maxLength": 160},
                    "vehicle_class": vehicle_class_schema,
                },
            },
            annotations=_READ_ONLY_ANNOTATIONS,
            _meta=_APP_ONLY_UI_META,
        ),
        types.Tool(
            name="app_search_viewport",
            title="App: Search Map Viewport",
            description=(
                "App-only tool returning up to 40 lightweight tracks, closures, and POIs in a bbox."
            ),
            inputSchema={
                "type": "object",
                "required": ["bbox"],
                "properties": {
                    "bbox": bbox_schema,
                    "grades": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(_VALID_GRADES)},
                    },
                    "open_only": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 40, "default": 40},
                },
            },
            annotations=_READ_ONLY_ANNOTATIONS,
            _meta=_APP_ONLY_UI_META,
        ),
        # ---- C. Authenticated Personal Read Tools (3, require trailforge:read) ----
        types.Tool(
            name="get_my_garage",
            title="Get My Vehicle Garage",
            description=(
                "Return the signed-in user's saved Vehicle Profiles (drivetrain, lift, clearance, "
                "tires, lockers, winch, snorkel, capability class, and active default rig). "
                "Requires trailforge:read."
            ),
            inputSchema={"type": "object", "properties": {}},
            annotations=_READ_ONLY_ANNOTATIONS,
        ),
        types.Tool(
            name="list_my_saved_routes",
            title="List My Saved Planned Routes",
            description=(
                "List the signed-in user's saved planned routes with live grade, closure, "
                "wet-risk summary, and TrailForge deep links. Requires trailforge:read."
            ),
            inputSchema={"type": "object", "properties": {}},
            annotations=_READ_ONLY_ANNOTATIONS,
        ),
        types.Tool(
            name="list_my_trips_and_pins",
            title="List My Private Trips, Pins & Favorites",
            description=(
                "Return metadata summaries of the signed-in user's private trip logbook (never "
                "exposes raw home-zone coordinates), personal pins, and favorite tracks. "
                "Requires trailforge:read."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "include_trips": {"type": "boolean", "default": True},
                    "include_pins": {"type": "boolean", "default": True},
                    "include_favorites": {"type": "boolean", "default": True},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
            },
            annotations=_READ_ONLY_ANNOTATIONS,
        ),
        # ---- D. Authenticated Private Write Tools (3, require trailforge:write) ----
        types.Tool(
            name="save_planned_route",
            title="Save or Update Personal Planned Route",
            description=(
                "Save a new owned planned multi-track route (or update an existing owned route_id) "
                "for the signed-in user and return live route analysis, deep link, and interactive "
                "route view. Requires trailforge:write."
            ),
            inputSchema={
                "type": "object",
                "required": ["name", "track_ids"],
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 160},
                    "track_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": MAX_ROUTE_TRACKS,
                    },
                    "notes": {"type": "string", "maxLength": 2000},
                    "vehicle_class": vehicle_class_schema,
                    "waypoints": {"type": "array", "items": waypoint_schema, "maxItems": 100},
                    "route_id": {
                        "type": "string",
                        "description": "Optional owned PlannedRoute UUID to update.",
                    },
                },
            },
            annotations=_WRITE_ANNOTATIONS,
            _meta=_UI_META,
        ),
        types.Tool(
            name="save_personal_pin",
            title="Save Personal Map Pin",
            description=(
                "Save a private personal waypoint/camp/hazard/photo/water/recovery pin for the "
                "signed-in user. Requires trailforge:write."
            ),
            inputSchema={
                "type": "object",
                "required": ["title", "lat", "lon"],
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 120},
                    "lat": {"type": "number", "minimum": -90, "maximum": 90},
                    "lon": {"type": "number", "minimum": -180, "maximum": 180},
                    "category": {
                        "type": "string",
                        "enum": list(_VALID_PIN_CATEGORIES),
                        "default": "waypoint",
                    },
                    "notes": {"type": "string", "maxLength": 1000},
                },
            },
            annotations=_WRITE_ANNOTATIONS,
        ),
        types.Tool(
            name="toggle_favorite_track",
            title="Add or Remove Favorite Track",
            description=(
                "Add or remove a canonical track in the signed-in user's private favorites. "
                "Requires trailforge:write."
            ),
            inputSchema={
                "type": "object",
                "required": ["track_id", "action"],
                "properties": {
                    "track_id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "action": {"type": "string", "enum": ["add", "remove"]},
                    "notes": {"type": "string", "maxLength": 1000},
                },
            },
            annotations=_WRITE_ANNOTATIONS,
        ),
    ]


_ALL_TOOL_DEFINITIONS: Final[list[types.Tool]] = _build_all_tool_definitions()


def list_mcp_tools() -> list[types.Tool]:
    """Return active MCP tool catalog respecting ``TRAILFORGE_MCP_AUTH_MODE``."""
    auth_mode = get_mcp_auth_mode()
    if auth_mode == "anonymous_only":
        return [
            t
            for t in _ALL_TOOL_DEFINITIONS
            if t.name not in PERSONAL_READ_TOOL_NAMES and t.name not in PRIVATE_WRITE_TOOL_NAMES
        ]
    return list(_ALL_TOOL_DEFINITIONS)


# =============================================================================
# Shared Internal Helpers (Rig Context, Regions, Compact Converters, PNG Blocks)
# =============================================================================


def _error_result(message: str) -> types.CallToolResult:
    attrs = build_mcp_attributions(["VIC", "NSW"])
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=f"Error: {message}{format_attribution_markdown(attrs)}",
            )
        ],
        structuredContent={"error": message, "attributions": attrs},
        isError=True,
    )


def _png_image_content(png_bytes: bytes) -> types.ImageContent:
    return types.ImageContent(
        type="image",
        data=base64.b64encode(png_bytes).decode("ascii"),
        mimeType="image/png",
    )


def _build_tile_config(public_base_url: str) -> TileConfig | None:
    cfg = get_mcp_settings()
    tiles_base = str(getattr(cfg, "tiles_public_base_url", "") or "").strip().rstrip("/")
    if tiles_base:
        return TileConfig(
            vector_pmtiles_url=f"{tiles_base}/trailforge_vector.pmtiles",
            terrain_pmtiles_url=f"{tiles_base}/trailforge_terrain.pmtiles",
            basemap_raster_url=None,
        )
    return None


def _resolve_rig_context(
    db: Session,
    vehicle_class_arg: str | None,
) -> tuple[CapabilityClassLiteral | None, VehicleProfile | None, RigContext | None]:
    """Resolve rig capability from explicit override or signed-in user's default VehicleProfile."""
    if vehicle_class_arg is not None and str(vehicle_class_arg).strip():
        norm = normalize_capability_class(str(vehicle_class_arg))
        if norm is None:
            raise ValueError(
                "vehicle_class must be one of: AWD, Stock 4WD, Touring 4WD, Modified 4WD"
            )
        return norm, None, RigContext(capability_class=norm)

    auth_ctx = get_mcp_auth_context()
    if auth_ctx.is_authenticated and auth_ctx.user is not None and auth_ctx.has_scope(SCOPE_READ):
        profile = db.execute(
            select(VehicleProfile)
            .where(VehicleProfile.user_id == auth_ctx.user.id)
            .order_by(VehicleProfile.is_default.desc(), VehicleProfile.created_at.asc())
            .limit(1)
        ).scalar_one_or_none()
        if profile is not None:
            cap: CapabilityClassLiteral = profile.capability_class  # type: ignore[assignment]
            return (
                cap,
                profile,
                RigContext(
                    capability_class=cap,
                    vehicle_name=profile.name,
                    has_snorkel=bool(profile.has_snorkel),
                ),
            )
    return None, None, None


def _resolve_region(raw_region: str | None) -> PackRegionSpec | None:
    """Resolve a region ID or friendly name (e.g. 'High Country', 'australian-alps')."""
    if not raw_region or not raw_region.strip():
        return None
    key = raw_region.strip().lower()
    if key in PACK_REGIONS:
        return PACK_REGIONS[key]
    slug = key.replace(" ", "-")
    if slug in PACK_REGIONS:
        return PACK_REGIONS[slug]
    for spec in list_pack_regions():
        hay = f"{spec.region_id} {spec.name} {spec.description}".lower()
        if key in hay:
            return spec
    return None


def _parse_bbox_arg(raw_bbox: Any) -> tuple[float, float, float, float] | None:
    if raw_bbox is None:
        return None
    if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
        raise ValueError("bbox must be [min_lon, min_lat, max_lon, max_lat]")
    vals = [float(v) for v in raw_bbox]
    if not all(math.isfinite(v) for v in vals):
        raise ValueError("bbox coordinates must be finite numbers")
    min_lon, min_lat, max_lon, max_lat = vals
    if not (-180.0 <= min_lon < max_lon <= 180.0 and -90.0 <= min_lat < max_lat <= 90.0):
        raise ValueError(
            "bbox must satisfy -180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90"
        )
    return (min_lon, min_lat, max_lon, max_lat)


def _bbox_around_point(
    lat: float, lon: float, radius_km: float
) -> tuple[float, float, float, float]:
    if not (math.isfinite(lat) and math.isfinite(lon) and math.isfinite(radius_km)):
        raise ValueError("Coordinates and radius_km must be finite numbers")
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ValueError("Coordinates out of WGS84 range")
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * max(0.2, math.cos(math.radians(lat))))
    return (
        max(-180.0, lon - dlon),
        max(-90.0, lat - dlat),
        min(180.0, lon + dlon),
        min(90.0, lat + dlat),
    )


def _compact_suitability(suit: TrackSuitability | None) -> CompactSuitability | None:
    if suit is None:
        return None
    return CompactSuitability(
        rating=suit.rating,
        summary=suit.reason,
        reasons=list(suit.warnings),
        vehicle_class=suit.vehicle_class,
    )


def _compact_closure(raw: dict[str, Any]) -> CompactClosureItem:
    geom = raw.get("geometry")
    lon = raw.get("lon")
    lat = raw.get("lat")
    if (lon is None or lat is None) and isinstance(geom, dict):
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        if gtype == "Point" and isinstance(coords, list) and len(coords) >= 2:
            lon, lat = float(coords[0]), float(coords[1])
        elif gtype == "LineString" and isinstance(coords, list) and coords:
            mid = coords[len(coords) // 2]
            if isinstance(mid, list) and len(mid) >= 2:
                lon, lat = float(mid[0]), float(mid[1])
        elif gtype == "Polygon" and isinstance(coords, list) and coords and coords[0]:
            ring = coords[0]
            lon = sum(float(p[0]) for p in ring) / len(ring)
            lat = sum(float(p[1]) for p in ring) / len(ring)
    return CompactClosureItem(
        closure_id=int(raw.get("closure_id") or 0),
        title=str(raw.get("title") or "Official Closure"),
        agency=str(raw.get("agency") or raw.get("feed_source") or "Official Agency"),
        closure_type=str(raw.get("closure_type") or "closure"),
        severity=str(raw.get("severity") or "warning"),
        state=str(raw.get("state") or ""),
        status=str(raw.get("status") or "active"),
        effective_from=raw.get("effective_from"),
        effective_to=raw.get("effective_to"),
        source_url=raw.get("source_url"),
        lon=round(float(lon), 5) if lon is not None else None,
        lat=round(float(lat), 5) if lat is not None else None,
        affected_track_ids=list(raw.get("affected_track_ids") or []),
    )


def _compact_report(raw: dict[str, Any]) -> CompactReportItem:
    """Serialize condition/gate report strictly stripping any PII (zero email/user_id)."""
    return CompactReportItem(
        id=str(raw.get("id") or ""),
        report_type=str(raw.get("report_type") or "hazard"),
        report_type_label=str(raw.get("report_type_label") or raw.get("report_type") or "Hazard"),
        is_gate_warning=bool(raw.get("is_gate_warning", False)),
        severity=str(raw.get("severity") or "caution"),
        title=str(raw.get("title") or "Condition Report"),
        notes=str(raw.get("notes") or raw.get("description") or "")[:400],
        freshness_score=round(float(raw.get("freshness_score") or 1.0), 2),
        is_stale=bool(raw.get("is_stale", False)),
        observed_at=raw.get("observed_at"),
        track_id=raw.get("track_id"),
        track_name=raw.get("track_name"),
        lon=round(float(raw["lon"]), 5) if raw.get("lon") is not None else None,
        lat=round(float(raw["lat"]), 5) if raw.get("lat") is not None else None,
        osm_edit_url=raw.get("osm_edit_url"),
    )


def _compact_poi(poi: Any) -> CompactPoiItem:
    return CompactPoiItem(
        id=int(poi.id),
        name=str(poi.name),
        poi_type=str(poi.poi_type),
        state=str(poi.state),
        elev_m=round(float(poi.elev_m), 1) if poi.elev_m is not None else None,
        lon=round(float(poi.lon), 5),
        lat=round(float(poi.lat), 5),
        distance_m=round(float(poi.distance_m), 1) if poi.distance_m is not None else None,
        access_note=poi.access_note,
        capad_park_name=poi.capad_park_name,
    )


def _load_track_preview_coords(
    db: Session,
    track_ids: Sequence[str],
    *,
    tolerance_deg: float = 0.00025,
) -> dict[str, list[list[float]]]:
    """Load simplified polyline coordinates per track_id for explore map previews."""
    if not track_ids:
        return {}
    rows = db.execute(
        select(
            TrackSegment.track_id,
            func.ST_AsGeoJSON(TrackSegment.geom).label("geom_json"),
        )
        .where(TrackSegment.track_id.in_(list(track_ids)), TrackSegment.is_active.is_(True))
        .order_by(
            TrackSegment.track_id.asc(),
            TrackSegment.segment_seq.asc().nullslast(),
            TrackSegment.segment_id.asc(),
        )
    ).all()
    grouped: dict[str, list[list[float]]] = {}
    for tid, geom_json in rows:
        if not tid:
            continue
        gj = json.loads(geom_json)
        coords = gj.get("coordinates") or []
        buf = grouped.setdefault(tid, [])
        for pt in coords:
            if len(pt) >= 2 and (not buf or buf[-1][0] != pt[0] or buf[-1][1] != pt[1]):
                buf.append([float(pt[0]), float(pt[1])])
    return {
        tid: simplify_linestring_coords(pts, tolerance_deg=tolerance_deg, max_points=60)
        for tid, pts in grouped.items()
    }


def _load_compact_segments_by_track(
    db: Session,
    track_ids: Sequence[str],
    *,
    tolerance_deg: float = DEFAULT_SIMPLIFY_TOLERANCE_DEG,
) -> dict[str, list[CompactSegmentGeom]]:
    if not track_ids:
        return {}
    rows = db.execute(
        select(
            TrackSegment,
            func.ST_AsGeoJSON(TrackSegment.geom).label("geom_json"),
            TrackSegmentGrade.effective_grade,
        )
        .outerjoin(TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id)
        .where(TrackSegment.track_id.in_(list(track_ids)), TrackSegment.is_active.is_(True))
        .order_by(
            TrackSegment.track_id.asc(),
            TrackSegment.segment_seq.asc().nullslast(),
            TrackSegment.segment_id.asc(),
        )
    ).all()
    out: dict[str, list[CompactSegmentGeom]] = {tid: [] for tid in track_ids}
    for seg, geom_json, eff_grade in rows:
        if not seg.track_id or seg.track_id not in out:
            continue
        gj = json.loads(geom_json)
        coords = simplify_linestring_coords(
            gj.get("coordinates") or [],
            tolerance_deg=tolerance_deg,
            max_points=45,
        )
        out[seg.track_id].append(
            CompactSegmentGeom(
                segment_id=seg.segment_id,
                grade=eff_grade or "Medium",
                slope_deg=round(float(seg.max_slope_deg), 1)
                if seg.max_slope_deg is not None
                else None,
                length_m=round(float(seg.length_m or 0.0), 1),
                is_steep=bool(seg.is_steep_highlight or (seg.max_slope_deg or 0.0) >= 14.0),
                coords=coords,
            )
        )
    return out


def _detail_to_track_view_payload(
    detail: TrackDetailResponse,
    *,
    public_base_url: str,
    rig_ctx: RigContext | None,
    snowline_m: int,
    max_profile_points: int = 350,
    tolerance_deg: float = DEFAULT_SIMPLIFY_TOLERANCE_DEG,
) -> TrailForgeViewPayload:
    states = [s.strip() for s in detail.state.split("/") if s.strip()]
    attrs = build_mcp_attributions(states)
    deep_link = build_track_deep_link(
        public_base_url,
        detail.track_id,
        vehicle_class=rig_ctx.capability_class if rig_ctx else None,
    )

    rain_ctx = detail.rainfall_context if isinstance(detail.rainfall_context, dict) else {}
    wet_lvl = str(rain_ctx.get("wet_risk_level") or "dry")
    rain_72h = rain_ctx.get("rain_72h_mm")

    compact_segs: list[CompactSegmentGeom] = []
    for seg in detail.segments:
        raw_coords = (seg.geometry or {}).get("coordinates") or []
        eff_grade = (
            seg.grade.effective_grade
            if seg.grade is not None
            else (detail.effective_grade or "Medium")
        )
        compact_segs.append(
            CompactSegmentGeom(
                segment_id=seg.segment_id,
                grade=eff_grade,
                slope_deg=round(float(seg.max_slope_deg), 1)
                if seg.max_slope_deg is not None
                else None,
                length_m=round(float(seg.length_m), 1),
                is_steep=bool(seg.is_steep_highlight or (seg.max_slope_deg or 0.0) >= 14.0),
                coords=simplify_linestring_coords(
                    raw_coords, tolerance_deg=tolerance_deg, max_points=50
                ),
            )
        )

    raw_prof = [
        CompactElevationPoint(
            dist_m=round(float(p.dist_m), 1),
            elev_m=round(float(p.elev_m), 1),
            slope_deg=round(float(p.slope_deg), 1),
            grade=p.grade or detail.effective_grade,
            segment_id=p.segment_id,
        )
        for p in detail.elevation_profile
    ]
    prof = downsample_compact_profile(raw_prof, max_points=max_profile_points)

    summary = CompactTrackCard(
        track_id=detail.track_id,
        name=detail.name,
        state=detail.state,
        trail_class=detail.trail_class,
        grade=detail.effective_grade or detail.grade,
        score=detail.score,
        live_grade=detail.live_grade,
        total_length_m=round(float(detail.total_length_m or 0.0), 1),
        elev_min_m=round(float(detail.elev_min_m), 1) if detail.elev_min_m is not None else None,
        elev_max_m=round(float(detail.elev_max_m), 1) if detail.elev_max_m is not None else None,
        elev_gain_m=round(float(detail.elev_gain_m or 0.0), 1),
        elev_loss_m=round(float(detail.elev_loss_m or 0.0), 1),
        max_slope_deg=round(float(detail.max_slope_deg), 1)
        if detail.max_slope_deg is not None
        else None,
        waterway_crossing_count=int(detail.waterway_crossing_count or 0),
        closure_status=detail.closure_status,
        wet_risk_level=wet_lvl,
        primary_park_name=detail.primary_park_name,
        bbox=detail.bbox or compute_bbox_from_coords([s.coords for s in compact_segs]),
        suitability=_compact_suitability(detail.suitability),
        deep_link_url=deep_link,
    )

    vc_split: dict[str, int] = {}
    for k, v in (detail.vehicle_class_split or {}).items():
        if isinstance(v, dict):
            vc_split[str(k)] = int(v.get("count") or 0)
        elif isinstance(v, (int, float)):
            vc_split[str(k)] = int(v)

    live_summary = CompactLiveSummary(
        live_grade=detail.live_grade,
        confidence_level=detail.confidence_level,
        sample_count=int(detail.sample_count or 0),
        last_driven_at=detail.last_driven_at,
        avg_speed_kmh=detail.avg_speed_kmh,
        est_duration_min=detail.est_duration_min,
        vehicle_class_split=vc_split,
        wet_risk_level=wet_lvl,
        rain_72h_mm=round(float(rain_72h), 1) if rain_72h is not None else None,
    )

    crossings = [
        CompactCrossingItem(
            name=c.name,
            perennial=bool(c.perennial),
            bridged=bool(c.bridged),
            dist_m=round(float(c.dist_m), 1),
        )
        for c in detail.waterway_crossings
    ]
    parks = [
        CompactParkRef(name=p.name, park_type=p.park_type, state=p.state)
        for p in detail.capad_parks
    ]

    track_data = TrackViewData(
        summary=summary,
        segments=compact_segs,
        elevation_profile=prof,
        crossings=crossings,
        baseline_breakdown=detail.baseline_breakdown,
        live_summary=live_summary,
        closures=[_compact_closure(c) for c in detail.active_closures],
        gate_warnings=[_compact_report(r) for r in detail.community_gate_warnings],
        condition_reports=[_compact_report(r) for r in detail.condition_reports],
        nearby_pois=[_compact_poi(p) for p in detail.nearby_pois[:20]],
        parks=parks,
        export_gpx_url=f"{public_base_url}/api/tracks/{detail.track_id}/export?format=gpx",
        export_geojson_url=f"{public_base_url}/api/tracks/{detail.track_id}/export?format=geojson",
    )

    subtitle = (
        f"{detail.state} · {summary.grade or 'Unrated'} · {summary.total_length_m / 1000.0:.1f} km"
    )
    if detail.closure_status != "open":
        subtitle += f" · Status: {detail.closure_status.upper()}"

    return TrailForgeViewPayload(
        view_mode="track",
        title=detail.name,
        subtitle=subtitle,
        bbox=summary.bbox,
        public_base_url=public_base_url,
        deep_link_url=deep_link,
        snowline_m=snowline_m,
        attributions=attrs,
        rig_context=rig_ctx,
        tile_config=_build_tile_config(public_base_url),
        track=track_data,
    )


def _analysis_to_route_view_payload(
    db: Session,
    analysis: PlannedRouteAnalysis,
    *,
    public_base_url: str,
    rig_ctx: RigContext | None,
    snowline_m: int,
    saved_route_id: str | None = None,
) -> TrailForgeViewPayload:
    tol = (
        LARGE_ROUTE_SIMPLIFY_TOLERANCE_DEG
        if len(analysis.legs) > 15
        else DEFAULT_SIMPLIFY_TOLERANCE_DEG
    )
    segs_by_track = _load_compact_segments_by_track(
        db, list(dict.fromkeys(analysis.track_ids)), tolerance_deg=tol
    )

    route_legs: list[RouteLegData] = []
    for leg in analysis.legs:
        raw_segs = list(segs_by_track.get(leg.track_id) or [])
        if leg.reversed:
            raw_segs = [
                CompactSegmentGeom(
                    segment_id=s.segment_id,
                    grade=s.grade,
                    slope_deg=s.slope_deg,
                    length_m=s.length_m,
                    is_steep=s.is_steep,
                    coords=list(reversed(s.coords)),
                )
                for s in reversed(raw_segs)
            ]
        route_legs.append(
            RouteLegData(
                leg_index=leg.index,
                track_id=leg.track_id,
                name=leg.name,
                reversed=leg.reversed,
                length_m=leg.length_m,
                grade=leg.grade,
                gap_from_prev_m=leg.gap_from_prev_m,
                segments=raw_segs,
            )
        )

    raw_prof = [
        CompactElevationPoint(
            dist_m=round(float(p.dist_m), 1),
            elev_m=round(float(p.elev_m), 1),
            slope_deg=round(float(p.slope_deg), 1),
            grade=p.grade or analysis.worst_grade,
            segment_id=None,
        )
        for p in analysis.elevation_profile
    ]
    prof = downsample_compact_profile(raw_prof, max_points=400)

    perennial_count = sum(1 for c in analysis.waterway_crossings if c.perennial and not c.bridged)
    max_gap_m = max((g.gap_m for g in analysis.leg_gaps), default=0.0)
    safety = RouteSafetySummaryData(
        has_blocking_closure=analysis.safety_summary.has_blocking_closure,
        closure_count=analysis.safety_summary.closure_count,
        gate_warning_count=analysis.safety_summary.gate_warning_count,
        wet_risk_level=analysis.safety_summary.wet_risk_level,
        waterway_crossing_count=max(
            len(analysis.waterway_crossings), analysis.safety_summary.unbridged_crossings
        ),
        perennial_crossing_count=perennial_count,
        gap_count=len(analysis.leg_gaps),
        max_gap_m=round(float(max_gap_m), 1),
    )

    deep_link = build_route_deep_link(
        public_base_url,
        analysis.track_ids,
        vehicle_class=rig_ctx.capability_class if rig_ctx else None,
    )
    export_gpx_url = (
        f"{public_base_url}/api/routes/{saved_route_id}/export?format=gpx"
        if saved_route_id
        else f"{public_base_url}/api/routes/plan/export?format=gpx"
    )

    route_data = RouteViewData(
        name=analysis.name,
        saved_route_id=saved_route_id,
        total_length_m=analysis.total_length_m,
        est_duration_min=analysis.est_duration_min,
        elev_gain_m=analysis.elev_gain_m,
        elev_loss_m=analysis.elev_loss_m,
        elev_min_m=analysis.min_elev_m,
        elev_max_m=analysis.max_elev_m,
        worst_grade=analysis.worst_grade,
        suitability=_compact_suitability(analysis.suitability),
        safety_summary=safety,
        legs=route_legs,
        elevation_profile=prof,
        closures=[_compact_closure(c) for c in analysis.closures],
        gate_warnings=[_compact_report(r) for r in analysis.community_gate_warnings],
        waypoints=[
            RouteWaypointData(name=w.name, category=w.category, lon=w.lon, lat=w.lat)
            for w in analysis.waypoints
        ],
        nearby_pois=[_compact_poi(p) for p in analysis.nearby_pois[:25]],
        gridforge_cli=analysis.gridforge_cli,
        export_gpx_url=export_gpx_url,
    )

    attrs = build_mcp_attributions(analysis.states or ["VIC", "NSW"])
    subtitle = (
        f"{len(analysis.legs)} leg(s) · {analysis.total_length_m / 1000.0:.1f} km · "
        f"~{analysis.est_duration_min:.0f} min · Worst grade: {analysis.worst_grade}"
    )

    return TrailForgeViewPayload(
        view_mode="route",
        title=analysis.name,
        subtitle=subtitle,
        bbox=analysis.bbox,
        public_base_url=public_base_url,
        deep_link_url=deep_link,
        snowline_m=snowline_m,
        attributions=attrs,
        rig_context=rig_ctx,
        tile_config=_build_tile_config(public_base_url),
        route=route_data,
    )


def _fetch_rainfall_summary(
    db: Session, bbox: tuple[float, float, float, float] | None
) -> CompactRainfallSummary | None:
    stmt = select(RainfallSnapshot).order_by(RainfallSnapshot.fetched_at.desc()).limit(25)
    if bbox is not None:
        env = func.ST_MakeEnvelope(bbox[0], bbox[1], bbox[2], bbox[3], 4326)
        stmt = (
            select(RainfallSnapshot)
            .where(func.ST_Intersects(RainfallSnapshot.centroid_geom, env))
            .order_by(RainfallSnapshot.fetched_at.desc())
            .limit(25)
        )
    snaps = db.execute(stmt).scalars().all()
    if not snaps:
        return None
    wet_rank = {"dry": 0, "damp": 1, "wet": 2, "saturated": 3}
    worst = max(snaps, key=lambda s: (wet_rank.get(s.wet_risk_level, 0), s.rain_72h_mm))
    return CompactRainfallSummary(
        rain_24h_mm=round(float(max(s.rain_24h_mm for s in snaps)), 1),
        rain_72h_mm=round(float(max(s.rain_72h_mm for s in snaps)), 1),
        wet_risk_level=worst.wet_risk_level,
    )


def _query_pois(
    db: Session,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    near_point: tuple[float, float, float] | None = None,  # (lon, lat, radius_m)
    track_id: str | None = None,
    poi_types: Sequence[str] | None = None,
    limit: int = 15,
) -> list[CompactPoiItem]:
    norm_types = [t.strip().lower() for t in (poi_types or []) if t and t.strip()]

    if track_id:
        track_union_sq = (
            select(func.ST_Union(TrackSegment.geom).label("geom"))
            .where(TrackSegment.track_id == track_id, TrackSegment.is_active.is_(True))
            .subquery("mcp_poi_trk")
        )
        radius_m = near_point[2] if near_point else 10_000.0
        deg_pad = max(0.025, (radius_m / 111_000.0) * 1.6)
        dist_expr = func.ST_Distance(
            func.ST_Transform(POI.geom, 3577),
            func.ST_Transform(track_union_sq.c.geom, 3577),
        ).label("distance_m")
        stmt = (
            select(
                POI,
                func.ST_X(POI.geom).label("lon"),
                func.ST_Y(POI.geom).label("lat"),
                CAPADPark.name.label("park_name"),
                dist_expr,
            )
            .join(
                track_union_sq,
                func.ST_DWithin(
                    func.ST_Transform(POI.geom, 3577),
                    func.ST_Transform(track_union_sq.c.geom, 3577),
                    radius_m,
                ),
            )
            .outerjoin(CAPADPark, POI.capad_park_id == CAPADPark.id)
            .where(func.ST_DWithin(POI.geom, track_union_sq.c.geom, deg_pad))
        )
        if norm_types:
            stmt = stmt.where(POI.poi_type.in_(norm_types))
        stmt = stmt.order_by(dist_expr.asc(), POI.name.asc()).limit(limit)
        rows = db.execute(stmt).all()
        return [
            CompactPoiItem(
                id=poi.id,
                name=poi.name,
                poi_type=poi.poi_type,
                state=poi.state,
                elev_m=round(float(poi.elev_m), 1) if poi.elev_m is not None else None,
                lon=round(float(lon), 5),
                lat=round(float(lat), 5),
                distance_m=round(float(dist_m), 1),
                access_note=poi.access_note,
                capad_park_name=park_name,
            )
            for poi, lon, lat, park_name, dist_m in rows
        ]

    if near_point is not None:
        plon, plat, radius_m = near_point
        pt_4326 = func.ST_SetSRID(func.ST_MakePoint(plon, plat), 4326)
        pt_3577 = func.ST_Transform(pt_4326, 3577)
        dist_expr = func.ST_Distance(func.ST_Transform(POI.geom, 3577), pt_3577).label("distance_m")
        deg_pad = max(0.025, (radius_m / 111_000.0) * 1.6)
        stmt = (
            select(
                POI,
                func.ST_X(POI.geom).label("lon"),
                func.ST_Y(POI.geom).label("lat"),
                CAPADPark.name.label("park_name"),
                dist_expr,
            )
            .outerjoin(CAPADPark, POI.capad_park_id == CAPADPark.id)
            .where(
                func.ST_DWithin(POI.geom, pt_4326, deg_pad),
                func.ST_DWithin(func.ST_Transform(POI.geom, 3577), pt_3577, radius_m),
            )
        )
        if norm_types:
            stmt = stmt.where(POI.poi_type.in_(norm_types))
        stmt = stmt.order_by(dist_expr.asc(), POI.name.asc()).limit(limit)
        rows = db.execute(stmt).all()
        return [
            CompactPoiItem(
                id=poi.id,
                name=poi.name,
                poi_type=poi.poi_type,
                state=poi.state,
                elev_m=round(float(poi.elev_m), 1) if poi.elev_m is not None else None,
                lon=round(float(lon), 5),
                lat=round(float(lat), 5),
                distance_m=round(float(dist_m), 1),
                access_note=poi.access_note,
                capad_park_name=park_name,
            )
            for poi, lon, lat, park_name, dist_m in rows
        ]

    stmt = select(
        POI,
        func.ST_X(POI.geom).label("lon"),
        func.ST_Y(POI.geom).label("lat"),
        CAPADPark.name.label("park_name"),
    ).outerjoin(CAPADPark, POI.capad_park_id == CAPADPark.id)
    if bbox is not None:
        env = func.ST_MakeEnvelope(bbox[0], bbox[1], bbox[2], bbox[3], 4326)
        stmt = stmt.where(func.ST_Intersects(POI.geom, env))
    if norm_types:
        stmt = stmt.where(POI.poi_type.in_(norm_types))
    rows = db.execute(stmt.order_by(POI.name.asc()).limit(limit)).all()
    return [
        CompactPoiItem(
            id=poi.id,
            name=poi.name,
            poi_type=poi.poi_type,
            state=poi.state,
            elev_m=round(float(poi.elev_m), 1) if poi.elev_m is not None else None,
            lon=round(float(lon), 5),
            lat=round(float(lat), 5),
            access_note=poi.access_note,
            capad_park_name=park_name,
        )
        for poi, lon, lat, park_name in rows
    ]


# =============================================================================
# Individual Tool Implementations (All 18 Tools)
# =============================================================================


def _tool_search_trails(db: Session, args: dict[str, Any]) -> types.CallToolResult:
    public_base = get_mcp_public_base_url()
    query = str(args.get("query") or "").strip() or None
    state = str(args.get("state") or "").strip().upper() or None
    region_raw = str(args.get("region") or "").strip() or None
    region_spec = _resolve_region(region_raw)
    bbox = _parse_bbox_arg(args.get("bbox"))
    near_lat = args.get("near_lat")
    near_lon = args.get("near_lon")
    radius_km = float(args.get("radius_km") if args.get("radius_km") is not None else 40.0)
    radius_km = max(1.0, min(150.0, radius_km))
    grades_filter = [str(g).strip() for g in (args.get("grades") or []) if str(g).strip()]
    open_only = bool(args.get("open_only", False))
    poi_types = [str(p).strip().lower() for p in (args.get("poi_types") or []) if str(p).strip()]
    limit = max(1, min(25, int(args.get("limit") if args.get("limit") is not None else 10)))
    include_images = bool(args.get("include_images", False))

    cap_class, _profile, rig_ctx = _resolve_rig_context(db, args.get("vehicle_class"))
    auth_ctx = get_mcp_auth_context()
    current_user: User | None = (
        auth_ctx.user if (auth_ctx.is_authenticated and auth_ctx.has_scope(SCOPE_READ)) else None
    )

    if bbox is None and near_lat is not None and near_lon is not None:
        bbox = _bbox_around_point(float(near_lat), float(near_lon), radius_km)
    elif bbox is None and region_spec is not None:
        bbox = region_spec.bbox

    bbox_str = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}" if bbox is not None else None

    # Fetch headroom so post-filters (multi-grade, open_only, poi_types) still fill `limit`
    raw_tracks = list_tracks(
        state=state,
        grade=grades_filter[0] if len(grades_filter) == 1 else None,
        trail_class=None,
        bbox=bbox_str,
        q=query,
        vehicle_class=cap_class if args.get("vehicle_class") else None,
        include_inactive=False,
        limit=min(200, limit * 5),
        offset=0,
        current_user=current_user,
        db=db,
    )

    filtered = []
    allowed_grades = set(grades_filter)
    for trk in raw_tracks:
        if allowed_grades and (trk.effective_grade or trk.grade) not in allowed_grades:
            continue
        if open_only and trk.closure_status in ("closed", "seasonal"):
            continue
        filtered.append(trk)

    # Optional POI proximity filter (within 2.5 km of matching POI type)
    if poi_types and filtered:
        cand_ids = [t.track_id for t in filtered]
        matched_ids = set(
            db.execute(
                select(TrackSegment.track_id)
                .distinct()
                .join(
                    POI,
                    func.ST_DWithin(
                        func.ST_Transform(TrackSegment.geom, 3577),
                        func.ST_Transform(POI.geom, 3577),
                        2500.0,
                    ),
                )
                .where(
                    TrackSegment.track_id.in_(cand_ids),
                    TrackSegment.is_active.is_(True),
                    POI.poi_type.in_(poi_types),
                )
            )
            .scalars()
            .all()
        )
        filtered = [t for t in filtered if t.track_id in matched_ids]

    selected = filtered[:limit]
    sel_ids = [t.track_id for t in selected]
    previews = _load_track_preview_coords(db, sel_ids)
    live_map = get_live_summaries_for_tracks(db, sel_ids)

    cards: list[CompactTrackCard] = []
    states_seen: list[str] = []
    closures_by_id: dict[int, CompactClosureItem] = {}
    for trk in selected:
        for st_p in trk.state.split("/"):
            if st_p.strip() and st_p.strip() not in states_seen:
                states_seen.append(st_p.strip())
        live_s = live_map.get(trk.track_id, {})
        rain_ctx = live_s.get("rainfall_context") or {}
        wet_lvl = str(rain_ctx.get("wet_risk_level") or "dry")
        for c in trk.active_closures:
            cc = _compact_closure(c)
            closures_by_id.setdefault(cc.closure_id, cc)
        cards.append(
            CompactTrackCard(
                track_id=trk.track_id,
                name=trk.name,
                state=trk.state,
                trail_class=trk.trail_class,
                grade=trk.effective_grade or trk.grade,
                score=trk.score,
                live_grade=trk.live_grade,
                total_length_m=round(float(trk.total_length_m or 0.0), 1),
                elev_min_m=round(float(trk.elev_min_m), 1) if trk.elev_min_m is not None else None,
                elev_max_m=round(float(trk.elev_max_m), 1) if trk.elev_max_m is not None else None,
                elev_gain_m=round(float(trk.elev_gain_m or 0.0), 1),
                max_slope_deg=round(float(trk.max_slope_deg), 1)
                if trk.max_slope_deg is not None
                else None,
                waterway_crossing_count=int(trk.waterway_crossing_count or 0),
                closure_status=trk.closure_status,
                wet_risk_level=wet_lvl,
                primary_park_name=trk.primary_park_name,
                bbox=trk.bbox,
                suitability=_compact_suitability(trk.suitability),
                deep_link_url=build_track_deep_link(
                    public_base,
                    trk.track_id,
                    vehicle_class=rig_ctx.capability_class if rig_ctx else None,
                ),
                preview_coords=previews.get(trk.track_id, []),
            )
        )

    eff_bbox = (
        list(bbox)
        if bbox is not None
        else compute_bbox_from_coords([c.preview_coords for c in cards if c.preview_coords])
    )
    pois = _query_pois(
        db,
        bbox=(eff_bbox[0], eff_bbox[1], eff_bbox[2], eff_bbox[3]) if eff_bbox else None,
        poi_types=poi_types or None,
        limit=15,
    )
    rainfall_summary = _fetch_rainfall_summary(
        db, (eff_bbox[0], eff_bbox[1], eff_bbox[2], eff_bbox[3]) if eff_bbox else None
    )

    if state and state not in states_seen:
        states_seen.append(state)
    if region_spec:
        for st in region_spec.states:
            if st not in states_seen:
                states_seen.append(st)
    attrs = build_mcp_attributions(states_seen or ["VIC", "NSW"])

    area_label = region_spec.name if region_spec else (query or state or "TrailForge Network")
    deep_link = build_area_deep_link(
        public_base,
        region=region_spec.region_id if region_spec else None,
        bbox=eff_bbox,
    )
    payload = TrailForgeViewPayload(
        view_mode="explore",
        title=f"Trails — {area_label}",
        subtitle=f"{len(cards)} matching track(s)" + (" · Open only" if open_only else ""),
        bbox=eff_bbox,
        public_base_url=public_base,
        deep_link_url=deep_link,
        snowline_m=1000,
        attributions=attrs,
        rig_context=rig_ctx,
        tile_config=_build_tile_config(public_base),
        explore=ExploreViewData(
            tracks=cards,
            closures=list(closures_by_id.values()),
            reports=[],
            pois=pois,
            rainfall_summary=rainfall_summary,
        ),
    )

    lines = [f"### Trail Search: {area_label} ({len(cards)} found)"]
    if rig_ctx:
        rig_lbl = (
            f"{rig_ctx.vehicle_name} ({rig_ctx.capability_class})"
            if rig_ctx.vehicle_name
            else rig_ctx.capability_class
        )
        lines.append(f"**Active Rig:** {rig_lbl}")
    if cards:
        lines.append("")
        lines.append("| Track | ID | State | Grade | Length | Climb | Status | Suitability |")
        lines.append("| :--- | :--- | :--- | :--- | ---: | ---: | :--- | :--- |")
        for c in cards:
            suit_txt = c.suitability.rating if c.suitability else "—"
            lines.append(
                f"| {c.name} | `{c.track_id}` | {c.state} | {c.grade or 'Unrated'} | "
                f"{c.total_length_m / 1000.0:.1f} km | +{c.elev_gain_m:.0f} m | "
                f"{c.closure_status} | {suit_txt} |"
            )
    else:
        lines.append(
            "No tracks matched your filters. Try widening the region, bbox, or grade filter."
        )
    lines.append(f"\n[Open in TrailForge]({deep_link})")
    text = "\n".join(lines) + format_attribution_markdown(attrs)

    content: list[types.ContentBlock] = [types.TextContent(type="text", text=text)]
    if include_images:
        preview_segs = [
            CompactSegmentGeom(
                segment_id=c.track_id,
                grade=c.grade or "Medium",
                length_m=c.total_length_m,
                coords=c.preview_coords,
            )
            for c in cards
            if len(c.preview_coords) >= 2
        ]
        png = render_map_card_png(
            title=payload.title,
            subtitle=payload.subtitle,
            segments=preview_segs,
            bbox=eff_bbox,
            closures=list(closures_by_id.values()),
            pois=pois,
            show_endpoints=False,
            footer_text=DEFAULT_ATTRIBUTION_FOOTER,
        )
        content.append(_png_image_content(png))

    return types.CallToolResult(
        content=content,
        structuredContent=payload.model_dump(exclude_none=True),
        isError=False,
    )


def _resolve_track_id_or_disambiguate(
    db: Session, track_id: str | None, track_name: str | None
) -> tuple[str | None, list[dict[str, Any]]]:
    if track_id and track_id.strip():
        return track_id.strip(), []
    if not track_name or not track_name.strip():
        raise ValueError("Provide either 'track_id' or 'track_name'")

    term = track_name.strip()
    exact = (
        db.execute(
            select(Track)
            .where(Track.is_active.is_(True), func.lower(Track.name) == term.lower())
            .order_by(Track.track_id.asc())
        )
        .scalars()
        .all()
    )
    if len(exact) == 1:
        return exact[0].track_id, []

    pattern = f"%{term}%"
    matches = (
        db.execute(
            select(Track)
            .where(
                Track.is_active.is_(True),
                or_(
                    Track.name.ilike(pattern),
                    func.array_to_string(Track.alt_names, " ").ilike(pattern),
                ),
            )
            .order_by(func.length(Track.name).asc(), Track.name.asc())
            .limit(10)
        )
        .scalars()
        .all()
    )

    if len(matches) == 1:
        return matches[0].track_id, []
    if not matches:
        raise ValueError(f"No active track found matching {term!r}")

    return None, [
        {
            "track_id": t.track_id,
            "name": t.name,
            "state": t.state,
            "trail_class": t.trail_class,
            "total_length_m": round(float(t.total_length_m or 0.0), 1),
        }
        for t in matches
    ]


def _tool_inspect_track(
    db: Session, args: dict[str, Any], *, app_only: bool = False
) -> types.CallToolResult:
    public_base = get_mcp_public_base_url()
    snowline_m = int(args.get("snowline_m") if args.get("snowline_m") is not None else 1000)
    include_images = False if app_only else bool(args.get("include_images", True))

    cap_class, _profile, rig_ctx = _resolve_rig_context(db, args.get("vehicle_class"))
    auth_ctx = get_mcp_auth_context()
    current_user: User | None = (
        auth_ctx.user if (auth_ctx.is_authenticated and auth_ctx.has_scope(SCOPE_READ)) else None
    )

    resolved_id, candidates = _resolve_track_id_or_disambiguate(
        db,
        str(args.get("track_id") or "").strip() or None,
        str(args.get("track_name") or "").strip() or None,
    )
    if resolved_id is None:
        attrs = build_mcp_attributions(["VIC", "NSW"])
        lines = [
            f"### Multiple tracks matched `{args.get('track_name')}` — specify a `track_id`:",
            "",
        ]
        for cand in candidates:
            lines.append(
                f"- **{cand['name']}** (`{cand['track_id']}`) — {cand['state']}, "
                f"{cand['total_length_m'] / 1000.0:.1f} km"
            )
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text="\n".join(lines) + format_attribution_markdown(attrs),
                )
            ],
            structuredContent={
                "disambiguation": candidates,
                "attributions": attrs,
            },
            isError=False,
        )

    try:
        detail = get_track_detail(
            track_id=resolved_id,
            vehicle_class=cap_class if args.get("vehicle_class") else None,
            current_user=current_user,
            db=db,
        )
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from exc

    payload = _detail_to_track_view_payload(
        detail,
        public_base_url=public_base,
        rig_ctx=rig_ctx,
        snowline_m=snowline_m,
    )
    assert payload.track is not None
    s = payload.track.summary

    lines = [
        f"### {detail.name} (`{detail.track_id}`)",
        f"**Grade:** {s.grade or 'Unrated'}"
        + (f" ({s.score:.1f}/100)" if s.score is not None else "")
        + f" · **State:** {detail.state} · **Status:** {detail.closure_status.upper()}",
    ]
    if s.suitability:
        lines.append(
            f"**Rig Suitability ({s.suitability.vehicle_class}):** "
            f"{s.suitability.rating} — {s.suitability.summary}"
        )
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| :--- | :--- |")
    lines.append(
        f"| Length | {s.total_length_m / 1000.0:.2f} km ({detail.segment_count} segments) |"
    )
    elev_range = (
        f"{s.elev_min_m:.0f}–{s.elev_max_m:.0f} m"
        if s.elev_min_m is not None and s.elev_max_m is not None
        else "n/a"
    )
    lines.append(f"| Elevation | {elev_range} (+{s.elev_gain_m:.0f} m / -{s.elev_loss_m:.0f} m) |")
    lines.append(
        f"| Max Slope | {f'{s.max_slope_deg:.1f}°' if s.max_slope_deg is not None else 'n/a'} |"
    )
    unbridged_cnt = sum(1 for c in payload.track.crossings if not c.bridged)
    lines.append(
        f"| Waterway Crossings | {s.waterway_crossing_count} ({unbridged_cnt} unbridged) |"
    )
    live = payload.track.live_summary
    if live:
        rain_str = f", {live.rain_72h_mm:.1f} mm / 72 h" if live.rain_72h_mm is not None else ""
        lines.append(
            f"| Live Evidence & Wet Risk | {live.sample_count} sample(s) "
            f"({live.confidence_level}), wet-risk `{live.wet_risk_level}`{rain_str} |"
        )
    if detail.primary_park_name:
        lines.append(f"| Protected Area | {detail.primary_park_name} |")

    warnings: list[str] = []
    for cl in payload.track.closures:
        src = f" ([source]({cl.source_url}))" if cl.source_url else ""
        warnings.append(f"- **Closure ({cl.agency}):** {cl.title} [{cl.severity}]{src}")
    for gw in payload.track.gate_warnings:
        warnings.append(f"- **Gate / Access Warning:** {gw.title} — {gw.notes}")
    if s.suitability:
        for w in s.suitability.reasons:
            warnings.append(f"- **Rig Advisory:** {w}")
    if warnings:
        lines.append("\n**Warnings & Alerts:**")
        lines.extend(warnings[:8])

    lines.append(
        f"\n[Open Track in TrailForge]({payload.deep_link_url}) · "
        f"[GPX]({payload.track.export_gpx_url}) · [GeoJSON]({payload.track.export_geojson_url})"
    )
    text = "\n".join(lines) + format_attribution_markdown(payload.attributions)

    content: list[types.ContentBlock] = [types.TextContent(type="text", text=text)]
    if include_images:
        elev_png = render_elevation_profile_png(
            title=f"{detail.name} — Elevation Profile",
            points=payload.track.elevation_profile,
            grade=s.grade,
            total_length_m=s.total_length_m,
            elev_gain_m=s.elev_gain_m,
            elev_loss_m=s.elev_loss_m,
            max_slope_deg=s.max_slope_deg,
            snowline_m=snowline_m,
            crossings=payload.track.crossings,
            suitability_label=(
                f"{s.suitability.vehicle_class}: {s.suitability.rating}" if s.suitability else None
            ),
            footer_text=DEFAULT_ATTRIBUTION_FOOTER,
        )
        map_png = render_map_card_png(
            title=detail.name,
            subtitle=payload.subtitle,
            segments=payload.track.segments,
            bbox=payload.bbox,
            closures=payload.track.closures,
            reports=[*payload.track.gate_warnings, *payload.track.condition_reports],
            pois=payload.track.nearby_pois,
            show_endpoints=True,
            footer_text=DEFAULT_ATTRIBUTION_FOOTER,
        )
        content.append(_png_image_content(elev_png))
        content.append(_png_image_content(map_png))

    return types.CallToolResult(
        content=content,
        structuredContent=payload.model_dump(exclude_none=True),
        isError=False,
    )


def _parse_waypoints_arg(raw: Any) -> list[RouteWaypoint]:
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError("waypoints must be a list of {name, category?, lon, lat} objects")
    return [RouteWaypoint.model_validate(item) for item in raw]


def _tool_plan_route(
    db: Session,
    args: dict[str, Any],
    *,
    app_only: bool = False,
    saved_route_id: str | None = None,
) -> types.CallToolResult:
    public_base = get_mcp_public_base_url()
    track_ids = args.get("track_ids")
    if not isinstance(track_ids, list) or not track_ids:
        raise ValueError("track_ids must be a non-empty list of canonical track IDs (1..30)")
    name = str(args.get("name") or "").strip() or None
    snowline_m = int(args.get("snowline_m") if args.get("snowline_m") is not None else 1000)
    include_images = False if app_only else bool(args.get("include_images", True))
    waypoints = _parse_waypoints_arg(args.get("waypoints"))

    cap_class, profile, rig_ctx = _resolve_rig_context(db, args.get("vehicle_class"))
    try:
        analysis, _oriented = plan_route_with_geometry(
            db,
            track_ids,
            vehicle_class=cap_class if args.get("vehicle_class") else None,
            vehicle_profile=profile,
            waypoints=waypoints,
            name=name,
        )
    except TrackNotFoundError as exc:
        raise ValueError(str(exc)) from exc

    payload = _analysis_to_route_view_payload(
        db,
        analysis,
        public_base_url=public_base,
        rig_ctx=rig_ctx,
        snowline_m=snowline_m,
        saved_route_id=saved_route_id,
    )
    assert payload.route is not None
    r = payload.route
    ss = r.safety_summary

    lines = [
        f"### Route Plan: {r.name}",
        f"**Worst Grade:** {r.worst_grade} · **Length:** {r.total_length_m / 1000.0:.2f} km · "
        f"**Est. Time:** {r.est_duration_min:.0f} min · "
        f"**Climb/Drop:** +{r.elev_gain_m:.0f} m / -{r.elev_loss_m:.0f} m",
    ]
    if r.suitability:
        lines.append(
            f"**Rig Suitability ({r.suitability.vehicle_class}):** "
            f"{r.suitability.rating} — {r.suitability.summary}"
        )
    if saved_route_id:
        lines.append(f"**Saved Route ID:** `{saved_route_id}`")

    lines.append("")
    lines.append("| # | Leg | ID | Grade | Length | Dir | Gap |")
    lines.append("| ---: | :--- | :--- | :--- | ---: | :--- | ---: |")
    for leg in r.legs:
        dir_txt = "Rev" if leg.reversed else "Fwd"
        gap_txt = f"{leg.gap_from_prev_m:.0f} m" if leg.gap_from_prev_m > 0 else "—"
        lines.append(
            f"| {leg.leg_index + 1} | {leg.name} | `{leg.track_id}` | {leg.grade or '—'} | "
            f"{leg.length_m / 1000.0:.2f} km | {dir_txt} | {gap_txt} |"
        )

    if analysis.safety_summary.warnings:
        lines.append("\n**Safety & Conditions:**")
        for w in analysis.safety_summary.warnings[:8]:
            lines.append(f"- {w}")
    else:
        lines.append(
            f"\n**Safety:** No blocking closures detected (wet-risk `{ss.wet_risk_level}`)."
        )

    lines.append(f"\n**GridForge 300 DPI CLI:**\n```bash\n{r.gridforge_cli}\n```")
    lines.append(f"[Open Route in TrailForge]({payload.deep_link_url})")
    text = "\n".join(lines) + format_attribution_markdown(payload.attributions)

    content: list[types.ContentBlock] = [types.TextContent(type="text", text=text)]
    if include_images:
        all_segs = [seg for leg in r.legs for seg in leg.segments]
        map_png = render_map_card_png(
            title=r.name,
            subtitle=payload.subtitle,
            segments=all_segs,
            bbox=payload.bbox,
            closures=r.closures,
            reports=r.gate_warnings,
            pois=r.nearby_pois,
            show_endpoints=True,
            footer_text=DEFAULT_ATTRIBUTION_FOOTER,
        )
        content.append(_png_image_content(map_png))

        # Section 7.1: omit secondary PNG for very large >15-leg routes to stay well under 450 KB
        if len(r.legs) <= 15:
            route_crossings = [
                CompactCrossingItem(
                    name=c.name, perennial=c.perennial, bridged=c.bridged, dist_m=c.dist_m
                )
                for c in analysis.waterway_crossings
            ]
            elev_png = render_elevation_profile_png(
                title=f"{r.name} — Combined Elevation Profile",
                points=r.elevation_profile,
                grade=r.worst_grade,
                total_length_m=r.total_length_m,
                elev_gain_m=r.elev_gain_m,
                elev_loss_m=r.elev_loss_m,
                max_slope_deg=analysis.max_slope_deg,
                snowline_m=snowline_m,
                crossings=route_crossings,
                suitability_label=(
                    f"{r.suitability.vehicle_class}: {r.suitability.rating}"
                    if r.suitability
                    else None
                ),
                footer_text=DEFAULT_ATTRIBUTION_FOOTER,
            )
            content.append(_png_image_content(elev_png))

    return types.CallToolResult(
        content=content,
        structuredContent=payload.model_dump(exclude_none=True),
        isError=False,
    )


def _tool_compare_tracks(db: Session, args: dict[str, Any]) -> types.CallToolResult:
    public_base = get_mcp_public_base_url()
    raw_ids = args.get("track_ids")
    if not isinstance(raw_ids, list) or not (2 <= len(raw_ids) <= 4):
        raise ValueError("compare_tracks requires 2 to 4 canonical track_ids")
    track_ids = [str(t).strip() for t in raw_ids if str(t).strip()]
    if len(track_ids) < 2:
        raise ValueError("compare_tracks requires 2 to 4 non-blank track_ids")
    include_images = bool(args.get("include_images", True))

    cap_class, _profile, rig_ctx = _resolve_rig_context(db, args.get("vehicle_class"))
    auth_ctx = get_mcp_auth_context()
    current_user: User | None = (
        auth_ctx.user if (auth_ctx.is_authenticated and auth_ctx.has_scope(SCOPE_READ)) else None
    )

    entries: list[CompareTrackEntry] = []
    states_seen: list[str] = []
    all_coord_lists: list[list[list[float]]] = []
    for idx, tid in enumerate(track_ids):
        try:
            detail = get_track_detail(
                track_id=tid,
                vehicle_class=cap_class if args.get("vehicle_class") else None,
                current_user=current_user,
                db=db,
            )
        except HTTPException as exc:
            raise ValueError(f"Track {tid!r}: {exc.detail}") from exc

        for st_p in detail.state.split("/"):
            if st_p.strip() and st_p.strip() not in states_seen:
                states_seen.append(st_p.strip())
        single_payload = _detail_to_track_view_payload(
            detail,
            public_base_url=public_base,
            rig_ctx=rig_ctx,
            snowline_m=1000,
            max_profile_points=180,
            tolerance_deg=0.0002,
        )
        assert single_payload.track is not None
        for seg in single_payload.track.segments:
            if seg.coords:
                all_coord_lists.append(seg.coords)
        entries.append(
            CompareTrackEntry(
                summary=single_payload.track.summary,
                segments=single_payload.track.segments,
                elevation_profile=single_payload.track.elevation_profile,
                color_hex=COMPARE_COLORS[idx % len(COMPARE_COLORS)],
            )
        )

    attrs = build_mcp_attributions(states_seen or ["VIC", "NSW"])
    bbox = compute_bbox_from_coords(all_coord_lists)
    deep_link = build_route_deep_link(
        public_base,
        track_ids,
        vehicle_class=rig_ctx.capability_class if rig_ctx else None,
    )
    title = "Compare: " + " vs ".join(e.summary.name for e in entries)
    payload = TrailForgeViewPayload(
        view_mode="compare",
        title=title,
        subtitle=f"Side-by-side comparison of {len(entries)} tracks",
        bbox=bbox,
        public_base_url=public_base,
        deep_link_url=deep_link,
        snowline_m=1000,
        attributions=attrs,
        rig_context=rig_ctx,
        tile_config=_build_tile_config(public_base),
        compare=CompareViewData(tracks=entries),
    )

    lines = [f"### {title}", ""]
    lines.append(
        "| Track | Grade | Length | Climb | Max Slope | Crossings | Status | Suitability |"
    )
    lines.append("| :--- | :--- | ---: | ---: | ---: | ---: | :--- | :--- |")
    for e in entries:
        s = e.summary
        slope_s = f"{s.max_slope_deg:.1f}°" if s.max_slope_deg is not None else "n/a"
        suit_s = s.suitability.rating if s.suitability else "—"
        lines.append(
            f"| {s.name} (`{s.track_id}`) | {s.grade or '—'} | "
            f"{s.total_length_m / 1000.0:.2f} km | +{s.elev_gain_m:.0f} m | {slope_s} | "
            f"{s.waterway_crossing_count} | {s.closure_status} | {suit_s} |"
        )
    lines.append(f"\n[Open in TrailForge]({deep_link})")
    text = "\n".join(lines) + format_attribution_markdown(attrs)

    content: list[types.ContentBlock] = [types.TextContent(type="text", text=text)]
    if include_images:
        png = render_comparison_png(
            title=title,
            tracks=entries,
            snowline_m=1000,
            footer_text=DEFAULT_ATTRIBUTION_FOOTER,
        )
        content.append(_png_image_content(png))

    return types.CallToolResult(
        content=content,
        structuredContent=payload.model_dump(exclude_none=True),
        isError=False,
    )


def _tool_check_area_conditions(db: Session, args: dict[str, Any]) -> types.CallToolResult:
    public_base = get_mcp_public_base_url()
    state = str(args.get("state") or "").strip().upper() or None
    region_raw = str(args.get("region") or "").strip() or None
    park_name = str(args.get("park_name") or "").strip() or None
    region_spec = _resolve_region(region_raw)
    bbox = _parse_bbox_arg(args.get("bbox"))
    near_lat = args.get("near_lat")
    near_lon = args.get("near_lon")
    radius_km = max(
        1.0, min(250.0, float(args.get("radius_km") if args.get("radius_km") is not None else 50.0))
    )
    include_images = bool(args.get("include_images", True))

    if bbox is None and near_lat is not None and near_lon is not None:
        bbox = _bbox_around_point(float(near_lat), float(near_lon), radius_km)
    elif bbox is None and park_name:
        pk_row = db.execute(
            select(
                CAPADPark,
                func.ST_XMin(CAPADPark.geom),
                func.ST_YMin(CAPADPark.geom),
                func.ST_XMax(CAPADPark.geom),
                func.ST_YMax(CAPADPark.geom),
            )
            .where(CAPADPark.name.ilike(f"%{park_name}%"))
            .limit(1)
        ).first()
        if pk_row is not None and pk_row[1] is not None:
            bbox = (float(pk_row[1]), float(pk_row[2]), float(pk_row[3]), float(pk_row[4]))
            if state is None:
                state = pk_row[0].state
    elif bbox is None and region_spec is not None:
        bbox = region_spec.bbox

    state_filter = state
    if state_filter is None and region_spec is not None:
        state_filter = ",".join(region_spec.states)

    closures_resp = list_closures(db, active_only=True, state=state_filter)
    raw_closures = list(closures_resp.get("items") or [])

    compact_closures: list[CompactClosureItem] = []
    for item in raw_closures:
        cc = _compact_closure(item)
        if (
            park_name
            and (item.get("capad_park_name") or "").lower().find(park_name.lower()) < 0
            and bbox is None
        ):
            continue
        if (
            bbox is not None
            and cc.lon is not None
            and cc.lat is not None
            and not (bbox[0] <= cc.lon <= bbox[2] and bbox[1] <= cc.lat <= bbox[3])
        ):
            continue
        compact_closures.append(cc)

    raw_reports = list_reports(db, include_resolved=False, limit=60)
    compact_reports: list[CompactReportItem] = []
    for rep in raw_reports:
        cr = _compact_report(rep)
        if (
            bbox is not None
            and cr.lon is not None
            and cr.lat is not None
            and not (bbox[0] <= cr.lon <= bbox[2] and bbox[1] <= cr.lat <= bbox[3])
        ):
            continue
        compact_reports.append(cr)

    bbox_str = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}" if bbox is not None else None
    area_tracks = list_tracks(
        state=state,
        grade=None,
        trail_class=None,
        bbox=bbox_str,
        q=None,
        vehicle_class=None,
        include_inactive=False,
        limit=25,
        offset=0,
        current_user=None,
        db=db,
    )
    previews = _load_track_preview_coords(db, [t.track_id for t in area_tracks])
    track_cards = [
        CompactTrackCard(
            track_id=t.track_id,
            name=t.name,
            state=t.state,
            trail_class=t.trail_class,
            grade=t.effective_grade or t.grade,
            score=t.score,
            total_length_m=round(float(t.total_length_m or 0.0), 1),
            elev_gain_m=round(float(t.elev_gain_m or 0.0), 1),
            max_slope_deg=round(float(t.max_slope_deg), 1) if t.max_slope_deg is not None else None,
            waterway_crossing_count=int(t.waterway_crossing_count or 0),
            closure_status=t.closure_status,
            bbox=t.bbox,
            deep_link_url=build_track_deep_link(public_base, t.track_id),
            preview_coords=previews.get(t.track_id, []),
        )
        for t in area_tracks
    ]
    pois = _query_pois(db, bbox=bbox, limit=15)
    rainfall = _fetch_rainfall_summary(db, bbox)

    states_seen = (
        list(region_spec.states) if region_spec else ([state] if state else ["VIC", "NSW"])
    )
    attrs = build_mcp_attributions(states_seen)
    area_name = park_name or (region_spec.name if region_spec else (state or "Australian Corridor"))
    eff_bbox = (
        list(bbox)
        if bbox is not None
        else compute_bbox_from_coords([c.preview_coords for c in track_cards if c.preview_coords])
    )
    deep_link = build_area_deep_link(
        public_base,
        region=region_spec.region_id if region_spec else None,
        bbox=eff_bbox,
    )

    gate_count = sum(1 for r in compact_reports if r.is_gate_warning)
    subtitle = (
        f"{len(compact_closures)} active closure(s) · {len(compact_reports)} report(s) "
        f"({gate_count} gate warning(s))"
    )
    if rainfall:
        subtitle += (
            f" · Wet risk: {rainfall.wet_risk_level} ({rainfall.rain_72h_mm or 0:.1f} mm / 72 h)"
        )

    payload = TrailForgeViewPayload(
        view_mode="explore",
        title=f"Conditions — {area_name}",
        subtitle=subtitle,
        bbox=eff_bbox,
        public_base_url=public_base,
        deep_link_url=deep_link,
        snowline_m=1000,
        attributions=attrs,
        tile_config=_build_tile_config(public_base),
        explore=ExploreViewData(
            tracks=track_cards,
            closures=compact_closures,
            reports=compact_reports,
            pois=pois,
            rainfall_summary=rainfall,
        ),
    )

    lines = [f"### Area Conditions: {area_name}", f"**Summary:** {subtitle}"]
    if compact_closures:
        lines.append("\n**Active Official Closures & Incidents:**")
        for cl in compact_closures[:12]:
            src = f" ([official source]({cl.source_url}))" if cl.source_url else ""
            lines.append(
                f"- **{cl.title}** ({cl.agency}, `{cl.closure_type}` / `{cl.severity}`){src}"
            )
    else:
        lines.append("\n**Official Closures:** None active in queried area.")

    if compact_reports:
        lines.append("\n**Community Hazards & Locked Gates:**")
        for rep in compact_reports[:10]:
            tag = "[GATE]" if rep.is_gate_warning else f"[{rep.severity.upper()}]"
            trk = f" on {rep.track_name}" if rep.track_name else ""
            lines.append(f"- {tag} **{rep.title}**{trk} — {rep.notes[:140]}")

    lines.append(f"\n[Inspect Area in TrailForge]({deep_link})")
    text = "\n".join(lines) + format_attribution_markdown(attrs)

    content: list[types.ContentBlock] = [types.TextContent(type="text", text=text)]
    if include_images:
        segs = [
            CompactSegmentGeom(
                segment_id=c.track_id,
                grade=c.grade or "Medium",
                length_m=c.total_length_m,
                coords=c.preview_coords,
            )
            for c in track_cards
            if len(c.preview_coords) >= 2
        ]
        png = render_map_card_png(
            title=payload.title,
            subtitle=subtitle,
            segments=segs,
            bbox=eff_bbox,
            closures=compact_closures,
            reports=compact_reports,
            pois=pois,
            show_endpoints=False,
            footer_text=DEFAULT_ATTRIBUTION_FOOTER,
        )
        content.append(_png_image_content(png))

    return types.CallToolResult(
        content=content,
        structuredContent=payload.model_dump(exclude_none=True),
        isError=False,
    )


def _tool_find_pois_near(db: Session, args: dict[str, Any]) -> types.CallToolResult:
    public_base = get_mcp_public_base_url()
    track_id = str(args.get("track_id") or "").strip() or None
    lat = args.get("lat")
    lon = args.get("lon")
    radius_km = max(
        0.5, min(50.0, float(args.get("radius_km") if args.get("radius_km") is not None else 10.0))
    )
    bbox = _parse_bbox_arg(args.get("bbox"))
    poi_types = [str(p).strip().lower() for p in (args.get("poi_types") or []) if str(p).strip()]
    limit = max(1, min(30, int(args.get("limit") if args.get("limit") is not None else 15)))

    near_point: tuple[float, float, float] | None = None
    if lat is not None and lon is not None:
        near_point = (float(lon), float(lat), radius_km * 1000.0)
    elif track_id is not None:
        near_point = (0.0, 0.0, radius_km * 1000.0)

    pois = _query_pois(
        db,
        bbox=bbox,
        near_point=near_point,
        track_id=track_id,
        poi_types=poi_types or None,
        limit=limit,
    )
    states_seen = list(dict.fromkeys(p.state for p in pois if p.state)) or ["VIC", "NSW"]
    attrs = build_mcp_attributions(states_seen)

    eff_bbox = (
        list(bbox)
        if bbox is not None
        else compute_bbox_from_coords([[[p.lon, p.lat] for p in pois]])
    )
    deep_link = (
        build_track_deep_link(public_base, track_id)
        if track_id
        else build_area_deep_link(public_base, bbox=eff_bbox)
    )
    payload = TrailForgeViewPayload(
        view_mode="explore",
        title=f"Nearby POIs ({len(pois)} found)",
        subtitle=(f"Within {radius_km:.1f} km of {track_id}" if track_id else None),
        bbox=eff_bbox,
        public_base_url=public_base,
        deep_link_url=deep_link,
        snowline_m=1000,
        attributions=attrs,
        tile_config=_build_tile_config(public_base),
        explore=ExploreViewData(pois=pois),
    )

    lines = [f"### Backcountry POIs ({len(pois)} found)"]
    if pois:
        lines.append("")
        lines.append("| Name | Type | State | Elevation | Distance | Coordinates |")
        lines.append("| :--- | :--- | :--- | ---: | ---: | :--- |")
        for p in pois:
            elev_s = f"{p.elev_m:.0f} m" if p.elev_m is not None else "—"
            dist_s = f"{p.distance_m:.0f} m" if p.distance_m is not None else "—"
            lines.append(
                f"| {p.name} | {p.poi_type} | {p.state} | {elev_s} | {dist_s} | "
                f"`{p.lat:.5f}, {p.lon:.5f}` |"
            )
    else:
        lines.append("No POIs found in the specified area.")
    lines.append(f"\n[Open in TrailForge]({deep_link})")
    text = "\n".join(lines) + format_attribution_markdown(attrs)

    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent=payload.model_dump(exclude_none=True),
        isError=False,
    )


def _tool_export_route_file(db: Session, args: dict[str, Any]) -> types.CallToolResult:
    public_base = get_mcp_public_base_url()
    track_ids = args.get("track_ids")
    if not isinstance(track_ids, list) or not track_ids:
        raise ValueError("track_ids must be a non-empty list of canonical track IDs (1..30)")
    name = str(args.get("name") or "").strip() or None
    fmt = str(args.get("format") or "gpx").strip().lower()
    if fmt not in ("gpx", "geojson"):
        raise ValueError("format must be 'gpx' or 'geojson'")
    waypoints = _parse_waypoints_arg(args.get("waypoints"))

    try:
        analysis, oriented = plan_route_with_geometry(db, track_ids, waypoints=waypoints, name=name)
    except TrackNotFoundError as exc:
        raise ValueError(str(exc)) from exc

    slug = slugify_route_name(analysis.name)
    if fmt == "gpx":
        content_str = build_route_gpx(analysis, leg_tracks=oriented)
        filename = f"{slug}.gpx"
        media_type = "application/gpx+xml"
    else:
        gj = build_route_geojson(analysis, leg_tracks=oriented)
        content_str = json.dumps(gj, indent=2) + "\n"
        filename = f"{slug}.geojson"
        media_type = "application/geo+json"

    size_bytes = len(content_str.encode("utf-8"))
    inlined = size_bytes <= _MAX_INLINE_EXPORT_BYTES
    export_url = (
        f"{public_base}/api/tracks/{analysis.track_ids[0]}/export?format={fmt}"
        if len(analysis.track_ids) == 1
        else f"{public_base}/api/routes/plan/export?format={fmt}"
    )
    deep_link = build_route_deep_link(public_base, analysis.track_ids)
    attrs = build_mcp_attributions(analysis.states or ["VIC", "NSW"])

    structured: dict[str, Any] = {
        "filename": filename,
        "format": fmt,
        "media_type": media_type,
        "size_bytes": size_bytes,
        "inlined": inlined,
        "content": content_str if inlined else None,
        "export_url": export_url,
        "deep_link_url": deep_link,
        "gridforge_cli": analysis.gridforge_cli,
        "track_ids": analysis.track_ids,
        "attributions": attrs,
    }

    lines = [
        f"### Route Export: `{filename}` ({size_bytes:,} bytes)",
        f"- **Format:** `{fmt}` (`{media_type}`)",
        f"- **Direct Export Endpoint:** {export_url}",
        f"- **TrailForge Deep Link:** {deep_link}",
        f"- **GridForge 300 DPI Print CLI:**\n```bash\n{analysis.gridforge_cli}\n```",
    ]
    if inlined:
        fence = "xml" if fmt == "gpx" else "json"
        lines.append(f"\n```{fence}\n{content_str[:6000]}\n```")
        if len(content_str) > 6000:
            lines.append("*(Full file content returned in `structuredContent.content`.)*")
    else:
        lines.append(
            f"\nFile exceeds 120 KB inline threshold ({size_bytes:,} bytes); "
            f"fetch directly via `{export_url}`."
        )
    text = "\n".join(lines) + format_attribution_markdown(attrs)

    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent=structured,
        isError=False,
    )


def _tool_list_regions_and_packs(db: Session, args: dict[str, Any]) -> types.CallToolResult:
    state_filter = str(args.get("state") or "").strip().upper() or None
    regions = list_pack_regions()
    if state_filter:
        regions = [r for r in regions if state_filter in r.states]

    built_packs = {row.pack_id: row for row in db.execute(select(OfflinePack)).scalars().all()}
    items: list[dict[str, Any]] = []
    states_seen: list[str] = []
    for spec in regions:
        for st in spec.states:
            if st not in states_seen:
                states_seen.append(st)
        pack_row = built_packs.get(spec.region_id)
        items.append(
            {
                "region_id": spec.region_id,
                "name": spec.name,
                "description": spec.description,
                "category": spec.category,
                "region_type": spec.region_type,
                "states": list(spec.states),
                "bbox": list(spec.bbox),
                "utm_zones": list(spec.utm_zones),
                "estimated_size_hint": spec.estimated_size_hint,
                "pack_status": pack_row.status if pack_row else "not_built",
                "pack_size_bytes": int(pack_row.size_bytes) if pack_row else None,
                "attributions": list(spec.attributions),
            }
        )

    attrs = build_mcp_attributions(states_seen or ["VIC", "NSW"])
    lines = [
        f"### TrailForge Regions & Offline Packs ({len(items)} regions)",
        "",
        "| Region ID | Name | States | Size Hint | Pack Status |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for it in items:
        lines.append(
            f"| `{it['region_id']}` | {it['name']} | {', '.join(it['states'])} | "
            f"{it['estimated_size_hint']} | {it['pack_status']} |"
        )
    text = "\n".join(lines) + format_attribution_markdown(attrs)

    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent={"count": len(items), "regions": items, "attributions": attrs},
        isError=False,
    )


def _tool_open_trailforge_map(db: Session, args: dict[str, Any]) -> types.CallToolResult:
    track_ids = args.get("track_ids")
    track_id = str(args.get("track_id") or "").strip() or None
    region = str(args.get("region") or "").strip() or None

    if isinstance(track_ids, list) and track_ids:
        return _tool_plan_route(
            db,
            {"track_ids": track_ids, "include_images": False},
            app_only=False,
        )
    if track_id:
        return _tool_inspect_track(
            db,
            {"track_id": track_id, "include_images": False},
            app_only=False,
        )
    return _tool_search_trails(
        db,
        {
            "region": region or "australian-alps",
            "limit": 15,
            "include_images": False,
        },
    )


def _tool_app_search_viewport(db: Session, args: dict[str, Any]) -> types.CallToolResult:
    bbox = _parse_bbox_arg(args.get("bbox"))
    if bbox is None:
        raise ValueError("app_search_viewport requires bbox=[min_lon, min_lat, max_lon, max_lat]")
    grades = [str(g).strip() for g in (args.get("grades") or []) if str(g).strip()]
    open_only = bool(args.get("open_only", False))
    limit = max(1, min(40, int(args.get("limit") if args.get("limit") is not None else 40)))
    public_base = get_mcp_public_base_url()
    bbox_str = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"

    raw_tracks = list_tracks(
        state=None,
        grade=grades[0] if len(grades) == 1 else None,
        trail_class=None,
        bbox=bbox_str,
        q=None,
        vehicle_class=None,
        include_inactive=False,
        limit=min(100, limit * 2),
        offset=0,
        current_user=None,
        db=db,
    )
    allowed = set(grades)
    selected = [
        t
        for t in raw_tracks
        if (not allowed or (t.effective_grade or t.grade) in allowed)
        and (not open_only or t.closure_status not in ("closed", "seasonal"))
    ][:limit]
    previews = _load_track_preview_coords(db, [t.track_id for t in selected])

    cards = []
    closures_map: dict[int, CompactClosureItem] = {}
    states_seen: list[str] = []
    for t in selected:
        for st_p in t.state.split("/"):
            if st_p.strip() and st_p.strip() not in states_seen:
                states_seen.append(st_p.strip())
        for c in t.active_closures:
            cc = _compact_closure(c)
            closures_map.setdefault(cc.closure_id, cc)
        cards.append(
            CompactTrackCard(
                track_id=t.track_id,
                name=t.name,
                state=t.state,
                trail_class=t.trail_class,
                grade=t.effective_grade or t.grade,
                score=t.score,
                total_length_m=round(float(t.total_length_m or 0.0), 1),
                elev_gain_m=round(float(t.elev_gain_m or 0.0), 1),
                max_slope_deg=round(float(t.max_slope_deg), 1)
                if t.max_slope_deg is not None
                else None,
                waterway_crossing_count=int(t.waterway_crossing_count or 0),
                closure_status=t.closure_status,
                bbox=t.bbox,
                deep_link_url=build_track_deep_link(public_base, t.track_id),
                preview_coords=previews.get(t.track_id, []),
            )
        )

    pois = _query_pois(db, bbox=bbox, limit=25)
    attrs = build_mcp_attributions(states_seen or ["VIC", "NSW"])
    payload = TrailForgeViewPayload(
        view_mode="explore",
        title=f"Viewport ({len(cards)} tracks)",
        bbox=list(bbox),
        public_base_url=public_base,
        deep_link_url=build_area_deep_link(public_base, bbox=list(bbox)),
        snowline_m=1000,
        attributions=attrs,
        tile_config=_build_tile_config(public_base),
        explore=ExploreViewData(
            tracks=cards,
            closures=list(closures_map.values()),
            pois=pois,
        ),
    )
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=f"Loaded {len(cards)} tracks and {len(pois)} POIs in viewport."
                + format_attribution_markdown(attrs),
            )
        ],
        structuredContent=payload.model_dump(exclude_none=True),
        isError=False,
    )


# ---- C. Authenticated Personal Read Tools (3) ----


def _tool_get_my_garage(db: Session) -> types.CallToolResult:
    auth_ctx = require_mcp_auth(SCOPE_READ)
    assert auth_ctx.user is not None
    profiles = (
        db.execute(
            select(VehicleProfile)
            .where(VehicleProfile.user_id == auth_ctx.user.id)
            .order_by(VehicleProfile.is_default.desc(), VehicleProfile.created_at.asc())
        )
        .scalars()
        .all()
    )

    items = [
        {
            "id": str(p.id),
            "name": p.name,
            "drivetrain": p.drivetrain,
            "has_low_range": bool(p.has_low_range),
            "lift_mm": int(p.lift_mm),
            "ground_clearance_mm": int(p.ground_clearance_mm),
            "tire_class": p.tire_class,
            "front_locker": bool(p.front_locker),
            "rear_locker": bool(p.rear_locker),
            "has_winch": bool(p.has_winch),
            "has_snorkel": bool(p.has_snorkel),
            "capability_class": p.capability_class,
            "is_default": bool(p.is_default),
        }
        for p in profiles
    ]
    attrs = build_mcp_attributions(["VIC", "NSW"])
    lines = [f"### My Garage ({len(items)} vehicle profile(s))"]
    if items:
        lines.append("")
        lines.append("| Rig | Class | Drivetrain | Lift | Tires | Lockers / Recovery | Default |")
        lines.append("| :--- | :--- | :--- | ---: | :--- | :--- | :--- |")
        for it in items:
            gear = [
                lbl
                for flag, lbl in (
                    (it["front_locker"], "F-Lock"),
                    (it["rear_locker"], "R-Lock"),
                    (it["has_winch"], "Winch"),
                    (it["has_snorkel"], "Snorkel"),
                )
                if flag
            ]
            lines.append(
                f"| {it['name']} | {it['capability_class']} | {it['drivetrain']} | "
                f"{it['lift_mm']} mm | {it['tire_class']} | {', '.join(gear) or '—'} | "
                f"{'Yes' if it['is_default'] else 'No'} |"
            )
    else:
        lines.append("No saved vehicle profiles yet.")
    text = "\n".join(lines) + format_attribution_markdown(attrs)

    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent={"vehicles": items, "count": len(items), "attributions": attrs},
        isError=False,
    )


def _tool_list_my_saved_routes(db: Session) -> types.CallToolResult:
    auth_ctx = require_mcp_auth(SCOPE_READ)
    assert auth_ctx.user is not None
    public_base = get_mcp_public_base_url()

    routes = (
        db.execute(
            select(PlannedRoute)
            .where(PlannedRoute.user_id == auth_ctx.user.id)
            .order_by(PlannedRoute.updated_at.desc(), PlannedRoute.created_at.desc())
        )
        .scalars()
        .all()
    )

    active_profile = db.execute(
        select(VehicleProfile)
        .where(VehicleProfile.user_id == auth_ctx.user.id)
        .order_by(VehicleProfile.is_default.desc(), VehicleProfile.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()

    items: list[dict[str, Any]] = []
    for r in routes:
        tids = list(r.track_ids or [])
        wps = [
            RouteWaypoint.model_validate(w) for w in (r.waypoints_json or []) if isinstance(w, dict)
        ]
        deep_link = build_route_deep_link(public_base, tids, vehicle_class=r.vehicle_class)
        try:
            analysis, _ = plan_route_with_geometry(
                db,
                tids,
                vehicle_class=r.vehicle_class,
                vehicle_profile=active_profile,
                waypoints=wps,
                name=r.name,
            )
            items.append(
                {
                    "id": str(r.id),
                    "name": r.name,
                    "notes": r.notes,
                    "track_ids": tids,
                    "vehicle_class": r.vehicle_class,
                    "leg_count": len(analysis.legs),
                    "total_length_m": analysis.total_length_m,
                    "est_duration_min": analysis.est_duration_min,
                    "worst_grade": analysis.worst_grade,
                    "has_blocking_closure": analysis.safety_summary.has_blocking_closure,
                    "closure_count": analysis.safety_summary.closure_count,
                    "wet_risk_level": analysis.safety_summary.wet_risk_level,
                    "deep_link_url": deep_link,
                    "gridforge_cli": analysis.gridforge_cli,
                }
            )
        except ValueError:
            items.append(
                {
                    "id": str(r.id),
                    "name": r.name,
                    "notes": r.notes,
                    "track_ids": tids,
                    "vehicle_class": r.vehicle_class,
                    "leg_count": len(tids),
                    "deep_link_url": deep_link,
                    "gridforge_cli": build_gridforge_cli(r.name),
                }
            )

    attrs = build_mcp_attributions(["VIC", "NSW"])
    lines = [f"### My Saved Routes ({len(items)})"]
    if items:
        lines.append("")
        lines.append("| Route | Legs | Length | Grade | Closures | Wet Risk | Link |")
        lines.append("| :--- | ---: | ---: | :--- | :--- | :--- | :--- |")
        for it in items:
            len_s = f"{it['total_length_m'] / 1000.0:.1f} km" if "total_length_m" in it else "—"
            cl_s = "BLOCKED" if it.get("has_blocking_closure") else str(it.get("closure_count", 0))
            lines.append(
                f"| {it['name']} (`{it['id']}`) | {it['leg_count']} | {len_s} | "
                f"{it.get('worst_grade') or '—'} | {cl_s} | {it.get('wet_risk_level') or 'dry'} | "
                f"[Open]({it['deep_link_url']}) |"
            )
    else:
        lines.append("No saved routes yet.")
    text = "\n".join(lines) + format_attribution_markdown(attrs)

    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent={"routes": items, "count": len(items), "attributions": attrs},
        isError=False,
    )


def _tool_list_my_trips_and_pins(db: Session, args: dict[str, Any]) -> types.CallToolResult:
    auth_ctx = require_mcp_auth(SCOPE_READ)
    assert auth_ctx.user is not None
    include_trips = bool(args.get("include_trips", True))
    include_pins = bool(args.get("include_pins", True))
    include_favorites = bool(args.get("include_favorites", True))
    limit = max(1, min(100, int(args.get("limit") if args.get("limit") is not None else 20)))

    trips_out: list[dict[str, Any]] = []
    if include_trips:
        # Strictly metadata summaries only — NEVER select or return raw_geom / home-zone coords!
        trip_rows = (
            db.execute(
                select(Trip)
                .where(Trip.user_id == auth_ctx.user.id)
                .order_by(Trip.created_at.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        for t in trip_rows:
            trips_out.append(
                {
                    "id": str(t.id),
                    "title": t.title or "Recorded Trip",
                    "visibility": t.visibility,
                    "vehicle_capability_class": t.vehicle_capability_class,
                    "privacy_trim_m": round(float(t.privacy_trim_m), 1),
                    "raw_distance_m": round(float(t.raw_distance_m or 0.0), 1),
                    "trimmed_distance_m": round(float(t.trimmed_distance_m or 0.0), 1),
                    "duration_s": round(float(t.duration_s), 1)
                    if t.duration_s is not None
                    else None,
                    "started_at": t.started_at.isoformat() if t.started_at else None,
                    "ended_at": t.ended_at.isoformat() if t.ended_at else None,
                    "match_status": t.match_status,
                    "avg_match_confidence": (
                        round(float(t.avg_match_confidence), 3)
                        if t.avg_match_confidence is not None
                        else None
                    ),
                }
            )

    pins_out: list[dict[str, Any]] = []
    if include_pins:
        pin_rows = db.execute(
            select(
                UserPin,
                func.ST_X(UserPin.geom).label("lon"),
                func.ST_Y(UserPin.geom).label("lat"),
            )
            .where(UserPin.user_id == auth_ctx.user.id)
            .order_by(UserPin.created_at.desc())
            .limit(limit)
        ).all()
        for pin, lon, lat in pin_rows:
            pins_out.append(
                {
                    "id": str(pin.id),
                    "title": pin.title,
                    "category": pin.category,
                    "notes": pin.notes,
                    "lon": round(float(lon), 6),
                    "lat": round(float(lat), 6),
                    "elev_m": pin.elev_m,
                }
            )

    favs_out: list[dict[str, Any]] = []
    if include_favorites:
        public_base = get_mcp_public_base_url()
        fav_rows = db.execute(
            select(UserFavorite, Track)
            .join(Track, UserFavorite.track_id == Track.track_id)
            .where(UserFavorite.user_id == auth_ctx.user.id, UserFavorite.track_id.is_not(None))
            .order_by(UserFavorite.created_at.desc())
            .limit(limit)
        ).all()
        for fav, trk in fav_rows:
            favs_out.append(
                {
                    "track_id": trk.track_id,
                    "track_name": trk.name,
                    "state": trk.state,
                    "total_length_m": round(float(trk.total_length_m or 0.0), 1),
                    "notes": fav.notes,
                    "deep_link_url": build_track_deep_link(public_base, trk.track_id),
                }
            )

    attrs = build_mcp_attributions(["VIC", "NSW"])
    lines = [
        "### Personal Logbook & Saved Items",
        f"- **Trips:** {len(trips_out)} (metadata summaries only; home-zone coordinates omitted)",
        f"- **Personal Pins:** {len(pins_out)}",
        f"- **Favorite Tracks:** {len(favs_out)}",
    ]
    text = "\n".join(lines) + format_attribution_markdown(attrs)

    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent={
            "trips": trips_out,
            "pins": pins_out,
            "favorites": favs_out,
            "attributions": attrs,
        },
        isError=False,
    )


# ---- D. Authenticated Private Write Tools (3) ----


def _tool_save_planned_route(db: Session, args: dict[str, Any]) -> types.CallToolResult:
    auth_ctx = require_mcp_auth(SCOPE_WRITE)
    assert auth_ctx.user is not None

    name = str(args.get("name") or "").strip()
    if not name or len(name) > 160:
        raise ValueError("name must be 1..160 characters")
    track_ids = args.get("track_ids")
    if not isinstance(track_ids, list) or not (1 <= len(track_ids) <= MAX_ROUTE_TRACKS):
        raise ValueError(f"track_ids must contain 1..{MAX_ROUTE_TRACKS} canonical track IDs")
    cleaned_ids = [str(t).strip() for t in track_ids if str(t).strip()]
    if len(cleaned_ids) != len(track_ids):
        raise ValueError("track_ids cannot contain blank items")

    notes_raw = args.get("notes")
    notes = str(notes_raw).strip() if notes_raw is not None and str(notes_raw).strip() else None
    waypoints = _parse_waypoints_arg(args.get("waypoints"))

    vc_raw = args.get("vehicle_class")
    vc_norm: str | None = None
    if vc_raw is not None and str(vc_raw).strip():
        vc_norm = normalize_capability_class(str(vc_raw))
        if vc_norm is None:
            raise ValueError(
                "vehicle_class must be one of: AWD, Stock 4WD, Touring 4WD, Modified 4WD"
            )

    active_profile = db.execute(
        select(VehicleProfile)
        .where(VehicleProfile.user_id == auth_ctx.user.id)
        .order_by(VehicleProfile.is_default.desc(), VehicleProfile.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()

    # Validate route tracks & build analysis BEFORE persisting
    try:
        plan_route_with_geometry(
            db,
            cleaned_ids,
            vehicle_class=vc_norm,
            vehicle_profile=active_profile,
            waypoints=waypoints,
            name=name,
        )
    except TrackNotFoundError as exc:
        raise ValueError(str(exc)) from exc

    now = datetime.now(UTC)
    route_id_raw = str(args.get("route_id") or "").strip() or None
    if route_id_raw is not None:
        try:
            rid = uuid.UUID(route_id_raw)
        except ValueError as exc:
            raise ValueError("Planned route not found") from exc
        route = db.execute(
            select(PlannedRoute)
            .where(PlannedRoute.id == rid, PlannedRoute.user_id == auth_ctx.user.id)
            .limit(1)
        ).scalar_one_or_none()
        if route is None:
            raise ValueError("Planned route not found or not owned by caller")
        route.name = name
        route.notes = notes
        route.track_ids = cleaned_ids
        route.waypoints_json = [wp.model_dump() for wp in waypoints]
        route.vehicle_class = vc_norm
        route.updated_at = now
        db.commit()
        db.refresh(route)
    else:
        route = PlannedRoute(
            user_id=auth_ctx.user.id,
            name=name,
            notes=notes,
            track_ids=cleaned_ids,
            waypoints_json=[wp.model_dump() for wp in waypoints],
            vehicle_class=vc_norm,
            created_at=now,
            updated_at=now,
        )
        db.add(route)
        db.commit()
        db.refresh(route)

    return _tool_plan_route(
        db,
        {
            "track_ids": cleaned_ids,
            "name": name,
            "vehicle_class": vc_norm,
            "waypoints": [wp.model_dump() for wp in waypoints],
            "include_images": False,
        },
        saved_route_id=str(route.id),
    )


def _tool_save_personal_pin(db: Session, args: dict[str, Any]) -> types.CallToolResult:
    auth_ctx = require_mcp_auth(SCOPE_WRITE)
    assert auth_ctx.user is not None

    title = str(args.get("title") or "").strip()
    if not title or len(title) > 120:
        raise ValueError("title must be 1..120 characters")
    lat = float(args["lat"])
    lon = float(args["lon"])
    if not (
        math.isfinite(lat)
        and math.isfinite(lon)
        and -90.0 <= lat <= 90.0
        and -180.0 <= lon <= 180.0
    ):
        raise ValueError("lat/lon must be valid WGS84 coordinates")
    category = str(args.get("category") or "waypoint").strip().lower()
    if category not in _VALID_PIN_CATEGORIES:
        raise ValueError(f"category must be one of: {', '.join(_VALID_PIN_CATEGORIES)}")
    notes_raw = args.get("notes")
    notes = (
        str(notes_raw).strip()[:1000] if notes_raw is not None and str(notes_raw).strip() else None
    )

    pin = UserPin(
        user_id=auth_ctx.user.id,
        title=title,
        category=category,
        notes=notes,
        geom=from_shape(Point(lon, lat), srid=4326),
    )
    db.add(pin)
    db.commit()
    db.refresh(pin)

    attrs = build_mcp_attributions(["VIC", "NSW"])
    pin_dict = {
        "id": str(pin.id),
        "title": pin.title,
        "category": pin.category,
        "notes": pin.notes,
        "lat": round(lat, 6),
        "lon": round(lon, 6),
    }
    text = (
        f"Saved personal pin **{pin.title}** (`{pin.category}`) at `{lat:.5f}, {lon:.5f}` "
        f"(ID: `{pin.id}`)." + format_attribution_markdown(attrs)
    )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent={"pin": pin_dict, "attributions": attrs},
        isError=False,
    )


def _tool_toggle_favorite_track(db: Session, args: dict[str, Any]) -> types.CallToolResult:
    auth_ctx = require_mcp_auth(SCOPE_WRITE)
    assert auth_ctx.user is not None

    track_id = str(args.get("track_id") or "").strip()
    action = str(args.get("action") or "").strip().lower()
    if not track_id:
        raise ValueError("track_id is required")
    if action not in ("add", "remove"):
        raise ValueError("action must be 'add' or 'remove'")
    notes_raw = args.get("notes")
    notes = (
        str(notes_raw).strip()[:1000] if notes_raw is not None and str(notes_raw).strip() else None
    )

    trk = db.get(Track, track_id)
    if trk is None or not trk.is_active:
        raise ValueError(f"Track {track_id!r} not found")

    existing = db.execute(
        select(UserFavorite)
        .where(
            UserFavorite.user_id == auth_ctx.user.id,
            UserFavorite.track_id == track_id,
            UserFavorite.segment_id.is_(None),
        )
        .limit(1)
    ).scalar_one_or_none()

    if action == "add":
        if existing is not None:
            if notes is not None:
                existing.notes = notes
                db.commit()
        else:
            fav = UserFavorite(
                user_id=auth_ctx.user.id,
                track_id=track_id,
                segment_id=None,
                notes=notes,
            )
            db.add(fav)
            db.commit()
        is_favorite = True
        verb = "Added"
    else:
        if existing is not None:
            db.delete(existing)
            db.commit()
        is_favorite = False
        verb = "Removed"

    attrs = build_mcp_attributions([s.strip() for s in trk.state.split("/") if s.strip()])
    public_base = get_mcp_public_base_url()
    deep_link = build_track_deep_link(public_base, trk.track_id)
    text = (
        f"{verb} **{trk.name}** (`{trk.track_id}`) {'to' if is_favorite else 'from'} your "
        f"favorites. [Open Track]({deep_link})" + format_attribution_markdown(attrs)
    )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent={
            "track_id": trk.track_id,
            "track_name": trk.name,
            "is_favorite": is_favorite,
            "action": action,
            "deep_link_url": deep_link,
            "attributions": attrs,
        },
        isError=False,
    )


# =============================================================================
# Top-Level Dispatcher (Enforces Auth Mode, Validation & < 450 KB Budget)
# =============================================================================


def call_mcp_tool(name: str, arguments: dict[str, Any] | None = None) -> types.CallToolResult:
    """Execute one of the 18 TrailForge MCP tools and enforce `< 450 KB` budget."""
    args = dict(arguments or {})
    if name not in ALL_TOOL_NAMES:
        return _error_result(f"Unknown MCP tool: {name!r}")

    if get_mcp_auth_mode() == "anonymous_only" and (
        name in PERSONAL_READ_TOOL_NAMES or name in PRIVATE_WRITE_TOOL_NAMES
    ):
        return _error_result(
            f"Tool {name!r} is disabled when TRAILFORGE_MCP_AUTH_MODE=anonymous_only"
        )

    try:
        with open_mcp_db_session() as db:
            if name == "search_trails":
                res = _tool_search_trails(db, args)
            elif name == "inspect_track":
                res = _tool_inspect_track(db, args, app_only=False)
            elif name == "plan_route":
                res = _tool_plan_route(db, args, app_only=False)
            elif name == "compare_tracks":
                res = _tool_compare_tracks(db, args)
            elif name == "check_area_conditions":
                res = _tool_check_area_conditions(db, args)
            elif name == "find_pois_near":
                res = _tool_find_pois_near(db, args)
            elif name == "export_route_file":
                res = _tool_export_route_file(db, args)
            elif name == "list_regions_and_packs":
                res = _tool_list_regions_and_packs(db, args)
            elif name == "open_trailforge_map":
                res = _tool_open_trailforge_map(db, args)
            elif name == "app_get_track_detail":
                res = _tool_inspect_track(db, args, app_only=True)
            elif name == "app_plan_route_preview":
                res = _tool_plan_route(db, args, app_only=True)
            elif name == "app_search_viewport":
                res = _tool_app_search_viewport(db, args)
            elif name == "get_my_garage":
                res = _tool_get_my_garage(db)
            elif name == "list_my_saved_routes":
                res = _tool_list_my_saved_routes(db)
            elif name == "list_my_trips_and_pins":
                res = _tool_list_my_trips_and_pins(db, args)
            elif name == "save_planned_route":
                res = _tool_save_planned_route(db, args)
            elif name == "save_personal_pin":
                res = _tool_save_personal_pin(db, args)
            elif name == "toggle_favorite_track":
                res = _tool_toggle_favorite_track(db, args)
            else:
                return _error_result(f"Unsupported MCP tool: {name!r}")
    except (McpAuthError, ValueError, KeyError) as exc:
        return _error_result(str(exc))

    return enforce_call_tool_result_budget(res)
