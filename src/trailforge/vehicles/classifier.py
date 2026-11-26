"""Deterministic Vehicle Capability Classifier and Track Suitability guidance (Slice 05).

Aligns strictly with docs/SPEC_GIS_SCHEMA.md Section 3.4 and
`src/trailforge/grading/baseline.py` (`compute_vehicle_suitability`).
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, Field

from trailforge.grading.baseline import (
    CapabilityClassLiteral,
    GradeLiteral,
    WetRiskLiteral,
    compute_vehicle_suitability,
)

DrivetrainLiteral = Literal["awd", "4wd_part_time", "4wd_full_time"]
TireClassLiteral = Literal["HT", "AT", "MT"]
SuitabilityRatingLiteral = Literal["Suited", "Challenging", "High Risk for your rig"]

VALID_CAPABILITY_CLASSES: Final[tuple[CapabilityClassLiteral, ...]] = (
    "AWD",
    "Stock 4WD",
    "Touring 4WD",
    "Modified 4WD",
)

_CLASS_LOOKUP: Final[dict[str, CapabilityClassLiteral]] = {
    c.lower(): c for c in VALID_CAPABILITY_CLASSES
}

VEHICLE_CLASS_REFERENCE: Final[list[dict[str, str]]] = [
    {
        "id": "AWD",
        "label": "AWD",
        "summary": "All-wheel drive SUV or soft-roader without a dual-range low transfer case.",
        "typical_setup": (
            "AWD drivetrain, road/HT or light AT tires, factory clearance (~180–210 mm)."
        ),
        "recommended_max_grade": "Easy",
    },
    {
        "id": "Stock 4WD",
        "label": "Stock 4WD",
        "summary": "Factory dual-range 4WD with low-range gearing and standard suspension/tires.",
        "typical_setup": (
            "Part-time or full-time 4WD with low range, factory clearance, HT/AT tires."
        ),
        "recommended_max_grade": "Medium",
    },
    {
        "id": "Touring 4WD",
        "label": "Touring 4WD",
        "summary": (
            "Equipped remote-touring 4WD ready for steep High Country tracks and river crossings."
        ),
        "typical_setup": "Dual-range 4WD, AT/MT tires, 25–50 mm lift or locker/winch/snorkel.",
        "recommended_max_grade": "Hard",
    },
    {
        "id": "Modified 4WD",
        "label": "Modified 4WD",
        "summary": (
            "Heavily built off-road rig set up for rocky escarpments, deep ruts, and extreme "
            "climbs."
        ),
        "typical_setup": (
            "Dual-range 4WD, 50+ mm lift, MT tires (or twin lockers + AT/MT), lockers/winch."
        ),
        "recommended_max_grade": "Extreme",
    },
]


class VehicleSpecs(BaseModel):
    """Normalized physical vehicle parameters used for deterministic capability classification."""

    drivetrain: DrivetrainLiteral
    low_range: bool = True
    lift_mm: int = Field(default=0, ge=0, le=300)
    clearance_mm: int = Field(default=210, ge=120, le=500)
    tire_class: TireClassLiteral = "AT"
    front_locker: bool = False
    rear_locker: bool = False
    has_winch: bool = False
    has_snorkel: bool = False


class VehicleClassification(BaseModel):
    """Deterministic classification output with transparent human-readable reasons."""

    capability_class: CapabilityClassLiteral
    classification_reasons: list[str]


class TrackSuitability(BaseModel):
    """Personalized rig suitability guidance returned on Track Inspector & Track List."""

    vehicle_class: CapabilityClassLiteral
    vehicle_name: str | None = None
    rating: SuitabilityRatingLiteral
    reason: str
    warnings: list[str] = Field(default_factory=list)


def normalize_capability_class(raw: str | None) -> CapabilityClassLiteral | None:
    """Validate and normalize a capability class query string."""
    if not raw:
        return None
    return _CLASS_LOOKUP.get(raw.strip().lower())


def classify_vehicle(specs: VehicleSpecs) -> VehicleClassification:
    """Deterministically classify vehicle hardware into one of 4 normalized AU 4WD classes."""
    reasons: list[str] = []

    if specs.drivetrain == "awd" or not specs.low_range:
        if specs.drivetrain == "awd":
            reasons.append("AWD drivetrain without dedicated 4WD transfer case")
        else:
            reasons.append("No low-range gearing limits steep descent/climb control")
        reasons.append(f"{specs.tire_class} tires with {specs.clearance_mm} mm ground clearance")
        return VehicleClassification(capability_class="AWD", classification_reasons=reasons)

    drivetrain_label = (
        "Full-time dual-range 4WD"
        if specs.drivetrain == "4wd_full_time"
        else "Part-time dual-range 4WD"
    )
    reasons.append(drivetrain_label)
    if specs.lift_mm > 0:
        reasons.append(f"{specs.lift_mm} mm suspension lift ({specs.clearance_mm} mm clearance)")
    else:
        reasons.append(f"Factory ride height ({specs.clearance_mm} mm clearance)")
    reasons.append(f"{specs.tire_class} tires")

    traction_notes: list[str] = []
    if specs.front_locker and specs.rear_locker:
        traction_notes.append("front & rear diff lockers")
    elif specs.rear_locker:
        traction_notes.append("rear diff locker")
    elif specs.front_locker:
        traction_notes.append("front diff locker")
    if specs.has_winch:
        traction_notes.append("recovery winch")
    if specs.has_snorkel:
        traction_notes.append("snorkel")
    if traction_notes:
        reasons.append("Equipped with " + ", ".join(traction_notes))

    has_any_locker = specs.front_locker or specs.rear_locker
    has_twin_lockers = specs.front_locker and specs.rear_locker

    is_modified = (
        (specs.tire_class == "MT" and specs.lift_mm >= 50 and (has_any_locker or specs.has_winch))
        or (
            has_twin_lockers
            and specs.tire_class in ("AT", "MT")
            and (specs.lift_mm >= 50 or specs.clearance_mm >= 250)
        )
        or (specs.tire_class == "MT" and specs.lift_mm >= 75 and specs.clearance_mm >= 260)
    )
    if is_modified:
        return VehicleClassification(
            capability_class="Modified 4WD", classification_reasons=reasons
        )

    is_touring = specs.tire_class in ("AT", "MT") and (
        specs.lift_mm >= 25
        or specs.clearance_mm >= 225
        or has_any_locker
        or specs.has_winch
        or specs.has_snorkel
    )
    if is_touring:
        return VehicleClassification(
            capability_class="Touring 4WD", classification_reasons=reasons
        )

    return VehicleClassification(capability_class="Stock 4WD", classification_reasons=reasons)


def evaluate_track_suitability(
    *,
    grade: GradeLiteral | None,
    capability_class: CapabilityClassLiteral,
    vehicle_name: str | None = None,
    max_slope_deg: float | None = None,
    waterway_crossing_count: int = 0,
    has_perennial_crossing: bool = False,
    is_4wd_only: bool = False,
    has_snorkel: bool | None = None,
    wet_risk_level: WetRiskLiteral = "dry",
    surface_class: str = "unknown",
    closure_status: str = "open",
) -> TrackSuitability:
    """Compute personalized rig suitability (Suited / Challenging / High Risk for your rig)."""
    eff_grade: GradeLiteral = grade if grade in ("Easy", "Medium", "Hard", "Extreme") else "Medium"
    base_rating = compute_vehicle_suitability(
        eff_grade,
        capability_class,
        wet_risk_level=wet_risk_level,
        surface_class=surface_class,  # type: ignore[arg-type]
    )

    warnings: list[str] = []
    final_rating: SuitabilityRatingLiteral = (
        "High Risk for your rig" if base_rating == "High Risk" else base_rating
    )

    wet_note = ""
    if wet_risk_level in ("wet", "saturated") and surface_class != "sealed":
        wet_note = f" Wet conditions ({wet_risk_level}) increase traction difficulty."
        warnings.append(
            f"Recent rainfall ({wet_risk_level}) elevates traction risk on unsealed terrain."
        )

    # Escalate AWD explicitly on 4wd_only tracks
    if capability_class == "AWD" and is_4wd_only:
        final_rating = "High Risk for your rig"
        warnings.append(
            "Track is designated 4WD-only; low-range gearing and high clearance required."
        )

    if max_slope_deg is not None and max_slope_deg >= 18.0:
        if capability_class in ("AWD", "Stock 4WD"):
            warnings.append(
                f"Steep {max_slope_deg:.1f}° climb/descent demands low-range control and traction."
            )
        else:
            warnings.append(f"Steep {max_slope_deg:.1f}° section — engage low range.")

    if waterway_crossing_count > 0:
        if has_perennial_crossing:
            if has_snorkel is False or capability_class in ("AWD", "Stock 4WD"):
                warnings.append(
                    "Unbridged perennial river crossing — check water depth; snorkel recommended."
                )
            else:
                warnings.append("Unbridged perennial river crossing — walk and check depth first.")
        elif capability_class == "AWD":
            warnings.append("Unbridged creek crossing ahead — check depth and entry/exit ruts.")

    if closure_status == "closed":
        warnings.append("Track currently has an active closure alert — do not enter.")
    elif closure_status == "seasonal":
        warnings.append("Seasonal road closure applies — verify open dates before travel.")
    elif closure_status == "gated":
        warnings.append("Private property or locked gate reported on this route.")

    if final_rating == "Suited":
        reason = f"{eff_grade} track is well matched to a {capability_class}.{wet_note}"
    elif final_rating == "Challenging":
        reason = (
            f"{eff_grade} track will challenge a {capability_class} — drive cautiously with "
            f"recovery gear.{wet_note}"
        )
    else:
        reason = (
            f"{eff_grade} track exceeds typical {capability_class} capability — high risk of "
            f"vehicle damage or getting stuck.{wet_note}"
        )

    return TrackSuitability(
        vehicle_class=capability_class,
        vehicle_name=vehicle_name,
        rating=final_rating,
        reason=reason.strip(),
        warnings=warnings,
    )
