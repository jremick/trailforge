"""SEP-1865 MCP App resource (`ui://trailforge/explorer.html`) and reference resources."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from mcp import types
from mcp.server.lowlevel.helper_types import ReadResourceContents
from pydantic import AnyUrl

from trailforge.config import Settings
from trailforge.mcp.context import (
    get_mcp_app_html_path,
    get_mcp_csp_tile_domains,
    get_mcp_public_base_url,
    get_mcp_settings,
)
from trailforge.mcp.schemas import build_mcp_attributions
from trailforge.packs.catalog import list_pack_regions
from trailforge.vehicles.classifier import VEHICLE_CLASS_REFERENCE

MCP_APP_URI: Final[str] = "ui://trailforge/explorer.html"
MCP_APP_MIME_TYPE: Final[str] = "text/html;profile=mcp-app"
GRADING_METHODOLOGY_URI: Final[str] = "trailforge://grading-methodology"
VEHICLE_CLASSES_URI: Final[str] = "trailforge://vehicle-classes"
REGIONS_URI: Final[str] = "trailforge://regions"

MAX_MCP_APP_HTML_BYTES: Final[int] = 10 * 1024 * 1024  # 10 MiB SEP-1865 hard ceiling
_STATIC_EXPLORER_PATH: Final[Path] = Path(__file__).resolve().parent / "static" / "explorer.html"

_FALLBACK_EXPLORER_HTML: Final[str] = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>TrailForge Explorer</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0b1117;
    --panel: #111923;
    --border: #1e293b;
    --text: #f1f5f9;
    --muted: #94a3b8;
    --easy: #22c55e;
    --med: #38bdf8;
    --hard: #f59e0b;
    --ext: #ef4444;
  }
  body {
    margin: 0;
    font-family: system-ui, -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 14px;
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px;
  }
  h1 { margin: 0 0 6px; font-size: 16px; }
  p { margin: 0 0 10px; color: var(--muted); font-size: 13px; }
  pre {
    margin: 10px 0;
    padding: 10px;
    background: #070b10;
    border-radius: 6px;
    overflow: auto;
    font-size: 12px;
    max-height: 280px;
  }
  footer {
    margin-top: 12px;
    padding-top: 8px;
    border-top: 1px solid var(--border);
    font-size: 11px;
    color: var(--muted);
  }
</style>
</head>
<body>
  <div class="card">
    <h1 id="tf-title">TrailForge Explorer</h1>
    <p id="tf-subtitle">Awaiting TrailForge track, route, or area tool payload.</p>
    <div id="tf-summary"></div>
    <footer id="tf-attr">
      Map data © OpenStreetMap contributors (ODbL) · Elevation &amp; Hydrology
      © Commonwealth of Australia (GA/DEA CC BY 4.0) · State Agencies (CC BY 4.0) ·
      Rainfall by Open-Meteo.com (CC BY 4.0)
    </footer>
  </div>
  <script>
  (function () {
    function renderPayload(p) {
      if (!p || typeof p !== "object") return;
      var titleEl = document.getElementById("tf-title");
      var subEl = document.getElementById("tf-subtitle");
      var sumEl = document.getElementById("tf-summary");
      var attrEl = document.getElementById("tf-attr");
      if (titleEl && p.title) titleEl.textContent = String(p.title);
      if (subEl) {
        subEl.textContent = p.subtitle
          ? String(p.subtitle)
          : "View mode: " + String(p.view_mode || "explore");
      }
      if (sumEl) {
        sumEl.textContent = "";
        var pre = document.createElement("pre");
        pre.textContent = JSON.stringify(
          { view_mode: p.view_mode, bbox: p.bbox, deep_link_url: p.deep_link_url },
          null,
          2
        );
        sumEl.appendChild(pre);
      }
      if (attrEl && Array.isArray(p.attributions) && p.attributions.length) {
        attrEl.textContent = p.attributions.join(" · ");
      }
    }
    window.addEventListener("message", function (ev) {
      var msg = ev && ev.data;
      if (!msg || typeof msg !== "object") return;
      if (msg.method === "ui/notifications/tool-result" && msg.params) {
        var res = msg.params.result;
        var sc = msg.params.structuredContent || (res && res.structuredContent);
        if (sc) renderPayload(sc);
      }
    });
  })();
  </script>
</body>
</html>
"""

_GRADING_METHODOLOGY_MD: Final[str] = """# TrailForge Explainable Track Grading Methodology

TrailForge never surfaces an opaque difficulty score. Every canonical 50–100 m track segment
exposes both its **Day-1 deterministic baseline** and **live crowdsourced telemetry + 72 h
rainfall** context.

## 1. Difficulty Tiers (0–100 Score)

| Grade | Score | Color | Typical Terrain |
| :--- | :--- | :--- | :--- |
| **Easy** | `0–24.9` | `#22C55E` | Graded gravel, gentle grades (`< 10°`), bridged crossings |
| **Medium** | `25–49.9` | `#38BDF8` | Unsealed dirt/4WD tracks, `10–14°` climbs, shallow creeks |
| **Hard** | `50–74.9` | `#F59E0B` | Rough rocky/rutted tracks, `14–18°`, perennial crossings |
| **Extreme** | `75–100` | `#EF4444` | Severe escarpments (`≥ 18–20°`), rock shelves, deep rivers |

## 2. Deterministic Day-1 Baseline Factors (`baseline_v1`)

1. **Surface / Tracktype / Smoothness (0–35 pts):** Uses the worst known indicator across
   `surface_class` (`sealed=0`, `gravel_compacted=8`, `dirt_unsealed=16`,
   `rough_rock_mud_sand=26`), OSM `tracktype` (`grade1=0` to `grade5=35`), and `smoothness`.
2. **Maximum Slope from GA 5 m LiDAR / DEA SRTM 1s DEM (0–30 pts):**
   `< 6° = 0`, `6–10° = 6`, `10–14° = 14`, `14–18° = 22`, `≥ 18° = 30`.
3. **Sustained Slope & Elevation Relief (0–15 pts):** Sustained climb/descent angle and
   vertical gain/loss over the segment.
4. **Waterway Crossings (0–20 pts):** Intersections with GA Surface Hydrology & OSM
   waterways (bridged crossings do not add ford penalty; perennial unbridged rivers add
   elevated score).

### Explicit Safety Floor Overrides
- **Medium floor (`≥ 30`):** Any `4WD-only` track or unbridged perennial waterway crossing.
- **Hard floor (`≥ 55`):** Any segment with `max_slope_deg ≥ 18°`, or `4WD-only` with `≥ 14°`.
- **Extreme floor (`≥ 78`):** `max_slope_deg ≥ 20°` combined with rough surface or unbridged
  perennial river crossing.

## 3. Live Crowdsourced Telemetry & 72 h Rainfall Context

- **Privacy-Preserved Telemetry:** Opt-in GPS/IMU traces are automatically trimmed by at least
  `250 m` at start/end and stripped of all user identity before updating segment speeds,
  roughness, vehicle-class split, and obstacle hotspots.
- **Confidence Levels:** `baseline_only` (`0` samples), `low`, `medium`, `high` based on
  recency-weighted independent traversals.
- **72 h Open-Meteo Wet-Risk Shift:** `dry`, `damp`, `wet`, or `saturated` (`heavy_rain`).
  Wet or saturated unsealed clay/dirt tracks escalate vehicle suitability warnings and
  effective traction difficulty.

## 4. Data Attributions
- Map data © OpenStreetMap contributors (ODbL) — openstreetmap.org/copyright
- Elevation & Surface Hydrology: © Commonwealth of Australia
  (Geoscience Australia / Digital Earth Australia) (CC BY 4.0)
- Protected Areas (CAPAD 2025): © Commonwealth of Australia (DCCEEW) (CC BY 4.0)
- Official State Trail & Closure Feeds: State of Victoria (DEECA / VicEmergency),
  State of NSW (Spatial Services / RFS / NPWS), and Pan-AU state agencies
  (CC BY 4.0 / CC BY 3.0 AU)
- Weather & Rainfall data by Open-Meteo.com (CC BY 4.0)
"""


def build_mcp_app_csp(
    cfg: Settings | None = None,
    *,
    public_origin: str | None = None,
) -> dict[str, list[str]]:
    """Build SEP-1865 ``_meta.ui.csp`` containing only bare ``scheme://host[:port]`` origins."""
    origins = get_mcp_csp_tile_domains(cfg, public_origin=public_origin)
    return {
        "connectDomains": list(origins),
        "resourceDomains": list(origins),
    }


def build_mcp_app_meta(
    cfg: Settings | None = None,
    *,
    public_origin: str | None = None,
) -> dict[str, Any]:
    """Build SEP-1865 ``_meta`` object for ``ui://trailforge/explorer.html``."""
    return {
        "ui": {
            "prefersBorder": False,
            "csp": build_mcp_app_csp(cfg, public_origin=public_origin),
        }
    }


def load_mcp_app_html(cfg: Settings | None = None) -> str:
    """Load `ui://trailforge/explorer.html` from override path, static bundle, or fallback."""
    override_raw = get_mcp_app_html_path(cfg)
    if override_raw:
        override_path = Path(override_raw)
        if override_path.is_file():
            html = override_path.read_text(encoding="utf-8")
            if len(html.encode("utf-8")) >= MAX_MCP_APP_HTML_BYTES:
                raise ValueError("TRAILFORGE_MCP_APP_HTML exceeds SEP-1865 10 MiB limit")
            return html

    if _STATIC_EXPLORER_PATH.is_file():
        html = _STATIC_EXPLORER_PATH.read_text(encoding="utf-8")
        if len(html.encode("utf-8")) < MAX_MCP_APP_HTML_BYTES:
            return html

    return _FALLBACK_EXPLORER_HTML


def list_mcp_resources(
    cfg: Settings | None = None,
    *,
    public_origin: str | None = None,
) -> list[types.Resource]:
    """Return all MCP resources exposed by TrailForge (SEP-1865 UI + 3 reference resources)."""
    settings = cfg or get_mcp_settings()
    origin = public_origin or get_mcp_public_base_url(settings)
    app_meta = build_mcp_app_meta(settings, public_origin=origin)

    return [
        types.Resource(
            uri=AnyUrl(MCP_APP_URI),
            name="TrailForge Interactive Explorer",
            title="TrailForge Interactive Map & Elevation Explorer",
            description=(
                "Single-file SEP-1865 interactive 4WD map and synchronized elevation profile "
                "app supporting explore, track, route, and compare modes with zero-network SVG "
                "fallback."
            ),
            mimeType=MCP_APP_MIME_TYPE,
            _meta=app_meta,
        ),
        types.Resource(
            uri=AnyUrl(GRADING_METHODOLOGY_URI),
            name="TrailForge Grading Methodology",
            title="Explainable 4WD Track Grading Methodology",
            description=(
                "Day-1 DEM/OSM baseline scoring, safety floors, live telemetry blending, and "
                "72-hour rainfall wet-risk shifts."
            ),
            mimeType="text/markdown",
        ),
        types.Resource(
            uri=AnyUrl(VEHICLE_CLASSES_URI),
            name="TrailForge Vehicle Capability Classes",
            title="Canonical Australian 4WD Vehicle Capability Classes",
            description=(
                "Classification criteria for AWD, Stock 4WD, Touring 4WD, and Modified 4WD rigs."
            ),
            mimeType="application/json",
        ),
        types.Resource(
            uri=AnyUrl(REGIONS_URI),
            name="TrailForge Australian Pack Regions",
            title="15 Australian Regions, Bounding Boxes & Attributions",
            description=(
                "The 15 curated Australian 4WD regions and statewide packs with WGS84 bounding "
                "boxes, states, UTM zones, and official upstream attributions."
            ),
            mimeType="application/json",
        ),
    ]


def read_mcp_resource(
    uri: str | AnyUrl,
    cfg: Settings | None = None,
    *,
    public_origin: str | None = None,
) -> list[ReadResourceContents]:
    """Read a TrailForge MCP resource by URI with SEP-1865 MIME type and ``_meta``."""
    settings = cfg or get_mcp_settings()
    origin = public_origin or get_mcp_public_base_url(settings)
    uri_str = str(uri).strip()

    if uri_str == MCP_APP_URI:
        html = load_mcp_app_html(settings)
        return [
            ReadResourceContents(
                content=html,
                mime_type=MCP_APP_MIME_TYPE,
                meta=build_mcp_app_meta(settings, public_origin=origin),
            )
        ]

    if uri_str == GRADING_METHODOLOGY_URI:
        return [
            ReadResourceContents(
                content=_GRADING_METHODOLOGY_MD,
                mime_type="text/markdown",
            )
        ]

    if uri_str == VEHICLE_CLASSES_URI:
        payload = {
            "vehicle_classes": VEHICLE_CLASS_REFERENCE,
            "suitability_matrix": {
                "Easy": {
                    "AWD": "Suited",
                    "Stock 4WD": "Suited",
                    "Touring 4WD": "Suited",
                    "Modified 4WD": "Suited",
                },
                "Medium": {
                    "AWD": "Challenging",
                    "Stock 4WD": "Suited",
                    "Touring 4WD": "Suited",
                    "Modified 4WD": "Suited",
                },
                "Hard": {
                    "AWD": "High Risk",
                    "Stock 4WD": "Challenging",
                    "Touring 4WD": "Suited",
                    "Modified 4WD": "Suited",
                },
                "Extreme": {
                    "AWD": "High Risk",
                    "Stock 4WD": "High Risk",
                    "Touring 4WD": "Challenging",
                    "Modified 4WD": "Suited",
                },
            },
            "attributions": build_mcp_attributions(["VIC", "NSW"]),
        }
        return [
            ReadResourceContents(
                content=json.dumps(payload, indent=2),
                mime_type="application/json",
            )
        ]

    if uri_str == REGIONS_URI:
        regions = [
            {
                "region_id": r.region_id,
                "name": r.name,
                "description": r.description,
                "region_type": r.region_type,
                "category": r.category,
                "states": list(r.states),
                "bbox": list(r.bbox),
                "corridor_bbox": list(r.corridor_bbox) if r.corridor_bbox else None,
                "utm_zones": list(r.utm_zones),
                "estimated_size_hint": r.estimated_size_hint,
                "attributions": list(r.attributions),
            }
            for r in list_pack_regions()
        ]
        payload = {
            "count": len(regions),
            "regions": regions,
            "attributions": build_mcp_attributions(
                ["VIC", "NSW", "ACT", "TAS", "QLD", "WA", "SA", "NT"]
            ),
        }
        return [
            ReadResourceContents(
                content=json.dumps(payload, indent=2),
                mime_type="application/json",
            )
        ]

    raise ValueError(f"Unknown TrailForge MCP resource URI: {uri_str}")
