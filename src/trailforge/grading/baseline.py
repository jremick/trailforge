"""Explainable Day-1 deterministic track baseline grading engine (Slice 02).

Implements the locked 0–100 scoring formula, explicit safety floor overrides, structured
``baseline_breakdown`` JSONB schema, and Vehicle Capability Class suitability matrix from
``docs/SPEC_GIS_SCHEMA.md`` Section 3.4.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, Field

from trailforge.ingestion.connectors.base import (
    AccessStatusLiteral,
    DemTierLiteral,
    SurfaceClassLiteral,
    TracktypeLiteral,
    TrailClassLiteral,
)

GradeLiteral = Literal["Easy", "Medium", "Hard", "Extreme"]
CapabilityClassLiteral = Literal["AWD", "Stock 4WD", "Touring 4WD", "Modified 4WD"]
WetRiskLiteral = Literal["dry", "damp", "wet", "saturated"]
SuitabilityLiteral = Literal["Suited", "Challenging", "High Risk"]

FORMULA_VERSION: Final[str] = "baseline_v1"

_SURFACE_POINTS: Final[dict[str, float]] = {
    "sealed": 0.0,
    "gravel_compacted": 8.0,
    "dirt_unsealed": 16.0,
    "rough_rock_mud_sand": 26.0,
}
_TRACKTYPE_POINTS: Final[dict[str, float]] = {
    "grade1": 0.0,
    "grade2": 8.0,
    "grade3": 16.0,
    "grade4": 26.0,
    "grade5": 35.0,
}
_SMOOTHNESS_POINTS: Final[dict[str, float]] = {
    "bad": 16.0,
    "very_bad": 26.0,
    "horrible": 26.0,
    "very_horrible": 35.0,
    "impassable": 35.0,
}

_SUITABILITY_MATRIX: Final[dict[GradeLiteral, dict[CapabilityClassLiteral, SuitabilityLiteral]]] = {
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
}


class SegmentGradeInput(BaseModel):
    """Deterministic static terrain, surface, crossing, and access inputs for one segment."""

    surface_class: SurfaceClassLiteral = "unknown"
    tracktype: TracktypeLiteral | None = None
    smoothness: str | None = None
    trail_class: TrailClassLiteral = "4wd_track"
    max_slope_deg: float | None = Field(default=None, ge=0.0, le=90.0)
    sustained_slope_deg: float | None = Field(default=None, ge=0.0, le=90.0)
    elev_gain_m: float = Field(default=0.0, ge=0.0)
    elev_loss_m: float = Field(default=0.0, ge=0.0)
    unbridged_crossing_count: int = Field(default=0, ge=0)
    has_perennial_crossing: bool = False
    has_major_perennial_crossing: bool = False
    waterway_names: list[str] = Field(default_factory=list)
    is_bridged: bool = False
    is_ford: bool = False
    is_4wd_only: bool = False
    is_4wd_recommended: bool = False
    access_status: AccessStatusLiteral = "public"
    has_gate_barrier: bool = False
    dem_tier: DemTierLiteral | None = None


class BaselineGradeResult(BaseModel):
    """Explainable Day-1 difficulty grade with score, safety floors, reasons, and JSONB payload."""

    grade: GradeLiteral
    score: float = Field(..., ge=0.0, le=100.0)
    raw_score: float = Field(..., ge=0.0, le=100.0)
    overrides: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    baseline_breakdown: dict[str, Any]


def score_to_grade(score: float) -> GradeLiteral:
    """Map numeric 0–100 score to canonical difficulty grade."""
    if score < 25.0:
        return "Easy"
    if score < 50.0:
        return "Medium"
    if score < 75.0:
        return "Hard"
    return "Extreme"


def _surface_factor(inp: SegmentGradeInput) -> float:
    if inp.surface_class == "unknown":
        if inp.trail_class in ("fire_trail", "management_trail", "4wd_track"):
            surface_pts = 16.0
        elif inp.trail_class == "unsealed_road":
            surface_pts = 8.0
        else:
            surface_pts = 0.0
    else:
        surface_pts = _SURFACE_POINTS.get(inp.surface_class, 0.0)

    tracktype_pts = _TRACKTYPE_POINTS.get(inp.tracktype or "", 0.0)
    smoothness_pts = _SMOOTHNESS_POINTS.get((inp.smoothness or "").strip().lower(), 0.0)
    return min(35.0, max(surface_pts, tracktype_pts, smoothness_pts))


def _max_slope_factor(max_slope_deg: float | None) -> float:
    if max_slope_deg is None or max_slope_deg < 6.0:
        return 0.0
    if max_slope_deg < 10.0:
        return 6.0
    if max_slope_deg < 14.0:
        return 14.0
    if max_slope_deg < 18.0:
        return 22.0
    return 30.0


def _sustained_relief_factor(
    sustained_slope_deg: float | None, elev_gain_m: float, elev_loss_m: float
) -> float:
    if sustained_slope_deg is None or sustained_slope_deg < 5.0:
        slope_pts = 0.0
    elif sustained_slope_deg < 9.0:
        slope_pts = 4.0
    elif sustained_slope_deg < 13.0:
        slope_pts = 8.0
    else:
        slope_pts = 12.0

    relief_bonus = 3.0 if max(elev_gain_m, elev_loss_m) >= 12.0 else 0.0
    return min(15.0, slope_pts + relief_bonus)


def _waterway_factor(inp: SegmentGradeInput) -> tuple[float, bool]:
    """Return ``(points, effective_perennial)`` for unbridged waterway crossings."""
    if inp.is_bridged:
        return (0.0, False)
    if inp.unbridged_crossing_count <= 0 and not inp.has_perennial_crossing:
        return (0.0, False)

    effective_perennial = (
        inp.has_perennial_crossing
        or inp.has_major_perennial_crossing
        or (inp.unbridged_crossing_count > 0 and inp.is_ford)
    )
    if inp.has_major_perennial_crossing:
        return (12.0, True)
    if effective_perennial:
        return (10.0, True)
    if inp.unbridged_crossing_count >= 2:
        return (8.0, False)
    return (6.0, False)


def _four_wd_access_factor(inp: SegmentGradeInput) -> float:
    candidates = [0.0]
    if inp.is_4wd_only:
        candidates.append(8.0)
    if inp.is_4wd_recommended:
        candidates.append(5.0)
    if inp.trail_class == "4wd_track":
        candidates.append(4.0)
    elif inp.trail_class in ("fire_trail", "management_trail"):
        candidates.append(3.0)
    return max(candidates)


def compute_baseline_grade(inp: SegmentGradeInput) -> BaselineGradeResult:
    """Compute an explainable Day-1 baseline difficulty grade and JSONB breakdown."""
    pts_surface = _surface_factor(inp)
    pts_max_slope = _max_slope_factor(inp.max_slope_deg)
    pts_sustained = _sustained_relief_factor(
        inp.sustained_slope_deg, inp.elev_gain_m, inp.elev_loss_m
    )
    pts_water, effective_perennial = _waterway_factor(inp)
    pts_4wd = _four_wd_access_factor(inp)

    raw_score = min(100.0, pts_surface + pts_max_slope + pts_sustained + pts_water + pts_4wd)
    final_score = raw_score
    overrides: list[str] = []

    max_slope = inp.max_slope_deg or 0.0
    if max_slope >= 20.0 and (pts_surface >= 26.0 or effective_perennial):
        if final_score < 75.0:
            final_score = 75.0
        overrides.append("steep_rough_or_perennial_extreme_floor")
    elif max_slope >= 18.0 or (inp.is_4wd_only and max_slope >= 14.0):
        if final_score < 50.0:
            final_score = 50.0
        overrides.append("steep_or_4wd_hard_floor")
    elif effective_perennial or inp.is_4wd_only:
        if final_score < 25.0:
            final_score = 25.0
        overrides.append("perennial_or_4wd_medium_floor")

    grade = score_to_grade(final_score)

    # Human-readable explainable reasons for Track Inspector UI
    reasons: list[str] = []
    if inp.tracktype or inp.surface_class != "unknown":
        surf_desc = inp.surface_class.replace("_", " ")
        tt_part = f" ({inp.tracktype})" if inp.tracktype else ""
        reasons.append(f"Surface {surf_desc}{tt_part} [{int(pts_surface)}/35 pts]")
    if inp.max_slope_deg is not None and inp.max_slope_deg >= 6.0:
        reasons.append(f"Max slope {inp.max_slope_deg:.1f}° [{int(pts_max_slope)}/30 pts]")
    if pts_sustained > 0:
        sust_s = f"{inp.sustained_slope_deg:.1f}°" if inp.sustained_slope_deg is not None else "n/a"
        reasons.append(
            f"Sustained grade {sust_s} / relief {max(inp.elev_gain_m, inp.elev_loss_m):.1f} m "
            f"[{int(pts_sustained)}/15 pts]"
        )
    if pts_water > 0:
        ww_label = ", ".join(inp.waterway_names) if inp.waterway_names else "unbridged watercourse"
        kind = "perennial" if effective_perennial else "intermittent"
        cnt = inp.unbridged_crossing_count or 1
        reasons.append(f"{cnt} {kind} crossing ({ww_label}) [{int(pts_water)}/12 pts]")
    elif inp.is_bridged:
        reasons.append("Bridged waterway crossing (0 water penalty)")
    if inp.is_4wd_only:
        reasons.append(f"Tagged 4wd_only=yes [{int(pts_4wd)}/8 pts]")
    elif inp.is_4wd_recommended:
        reasons.append(f"Tagged 4wd_only=recommended [{int(pts_4wd)}/8 pts]")
    if inp.has_gate_barrier or inp.access_status in ("private", "restricted", "closed"):
        reasons.append(
            f"Access warning: {inp.access_status}"
            + (" with gate barrier" if inp.has_gate_barrier else "")
        )
    if not reasons:
        reasons.append("Gentle grade on formed surface")

    breakdown: dict[str, Any] = {
        "formula_version": FORMULA_VERSION,
        "raw_score": round(raw_score, 1),
        "final_score": round(final_score, 1),
        "grade": grade,
        "factors": {
            "surface_tracktype": {
                "points": int(pts_surface),
                "max_points": 35,
                "surface_class": inp.surface_class,
                "tracktype": inp.tracktype,
                "smoothness": inp.smoothness,
            },
            "max_slope": {
                "points": int(pts_max_slope),
                "max_points": 30,
                "max_slope_deg": (
                    round(inp.max_slope_deg, 2) if inp.max_slope_deg is not None else None
                ),
            },
            "sustained_relief": {
                "points": int(pts_sustained),
                "max_points": 15,
                "sustained_slope_deg": (
                    round(inp.sustained_slope_deg, 2)
                    if inp.sustained_slope_deg is not None
                    else None
                ),
                "elev_gain_m": round(inp.elev_gain_m, 2),
                "elev_loss_m": round(inp.elev_loss_m, 2),
            },
            "waterway_crossings": {
                "points": int(pts_water),
                "max_points": 12,
                "unbridged_count": 0 if inp.is_bridged else inp.unbridged_crossing_count,
                "perennial": effective_perennial,
                "is_bridged": inp.is_bridged,
                "waterways": list(inp.waterway_names),
            },
            "four_wd_access": {
                "points": int(pts_4wd),
                "max_points": 8,
                "is_4wd_only": inp.is_4wd_only,
                "is_4wd_recommended": inp.is_4wd_recommended,
                "trail_class": inp.trail_class,
                "access_status": inp.access_status,
                "has_gate_barrier": inp.has_gate_barrier,
            },
        },
        "overrides": overrides,
        "reasons": reasons,
        "dem_tier": inp.dem_tier,
    }

    return BaselineGradeResult(
        grade=grade,
        score=round(final_score, 1),
        raw_score=round(raw_score, 1),
        overrides=overrides,
        reasons=reasons,
        baseline_breakdown=breakdown,
    )


def compute_vehicle_suitability(
    grade: GradeLiteral,
    capability_class: CapabilityClassLiteral,
    *,
    wet_risk_level: WetRiskLiteral = "dry",
    surface_class: SurfaceClassLiteral = "dirt_unsealed",
) -> SuitabilityLiteral:
    """Return personalized rig suitability guidance per ``docs/SPEC_GIS_SCHEMA.md`` Sec 3.4."""
    base = _SUITABILITY_MATRIX[grade][capability_class]
    if wet_risk_level in ("wet", "saturated") and surface_class != "sealed":
        if base == "Suited":
            return "Challenging"
        return "High Risk"
    return base
