"""Deterministic Vehicle Capability Classification and personalized Track Suitability engine."""

from trailforge.vehicles.classifier import (
    VEHICLE_CLASS_REFERENCE,
    CapabilityClassLiteral,
    DrivetrainLiteral,
    SuitabilityRatingLiteral,
    TireClassLiteral,
    TrackSuitability,
    VehicleClassification,
    VehicleSpecs,
    classify_vehicle,
    evaluate_track_suitability,
    normalize_capability_class,
)

__all__ = [
    "VEHICLE_CLASS_REFERENCE",
    "CapabilityClassLiteral",
    "DrivetrainLiteral",
    "SuitabilityRatingLiteral",
    "TireClassLiteral",
    "TrackSuitability",
    "VehicleClassification",
    "VehicleSpecs",
    "classify_vehicle",
    "evaluate_track_suitability",
    "normalize_capability_class",
]
