import type {
  PinCategory,
  TrackSuitability,
  VehicleCapabilityClass,
  VehicleProfileInput,
  VehicleProfileItem,
} from "./types";

export const VEHICLE_CLASSES: readonly VehicleCapabilityClass[] = [
  "AWD",
  "Stock 4WD",
  "Touring 4WD",
  "Modified 4WD",
];

export const VEHICLE_CLASS_META: Record<
  VehicleCapabilityClass,
  { short: string; summary: string; color: string }
> = {
  AWD: {
    short: "AWD",
    summary: "Soft-roader / AWD crossover without low-range transfer case",
    color: "#38BDF8",
  },
  "Stock 4WD": {
    short: "Stock 4WD",
    summary: "Factory low-range 4WD on HT/AT tyres with standard clearance",
    color: "#22C55E",
  },
  "Touring 4WD": {
    short: "Touring 4WD",
    summary: "Low-range 4WD with AT/MT tyres + lift/clearance or recovery gear",
    color: "#F59E0B",
  },
  "Modified 4WD": {
    short: "Mod 4WD",
    summary: "Lifted low-range 4WD with MT tyres, lockers & winch",
    color: "#A855F7",
  },
};

export const PIN_CATEGORY_OPTIONS: readonly {
  value: PinCategory;
  label: string;
  color: string;
}[] = [
  { value: "waypoint", label: "Waypoint", color: "#EC4899" },
  { value: "camp", label: "Camp", color: "#22C55E" },
  { value: "water", label: "Water", color: "#38BDF8" },
  { value: "hazard", label: "Hazard", color: "#EF4444" },
  { value: "recovery", label: "Recovery", color: "#F59E0B" },
  { value: "photo", label: "Photo", color: "#A855F7" },
];

export function getPinCategoryColor(category: string): string {
  const found = PIN_CATEGORY_OPTIONS.find((c) => c.value === category);
  return found?.color ?? "#EC4899";
}

export function getVehicleClearanceMm(v: VehicleProfileItem): number {
  return v.clearance_mm ?? v.ground_clearance_mm ?? 210;
}

export function getVehicleLowRange(v: VehicleProfileItem): boolean {
  return v.low_range ?? v.has_low_range ?? v.drivetrain !== "awd";
}

export function isVehicleActive(v: VehicleProfileItem): boolean {
  return Boolean(v.is_active ?? v.is_default);
}

/**
 * Deterministic client-side preview of TrailForge's Vehicle Capability Classifier
 * mirroring `src/trailforge/vehicles/classifier.py` 1:1 while editing a Vehicle Profile.
 */
export function classifyVehiclePreview(input: VehicleProfileInput): {
  capability_class: VehicleCapabilityClass;
  reasons: string[];
} {
  const reasons: string[] = [];

  if (input.drivetrain === "awd" || !input.low_range) {
    reasons.push(
      input.drivetrain === "awd"
        ? "AWD drivetrain without dedicated 4WD transfer case"
        : "No low-range gearing limits steep descent/climb control"
    );
    reasons.push(
      `${input.tire_class} tires with ${input.clearance_mm} mm ground clearance`
    );
    return { capability_class: "AWD", reasons };
  }

  reasons.push(
    input.drivetrain === "4wd_full_time"
      ? "Full-time dual-range 4WD"
      : "Part-time dual-range 4WD"
  );
  reasons.push(
    input.lift_mm > 0
      ? `${input.lift_mm} mm suspension lift (${input.clearance_mm} mm clearance)`
      : `Factory ride height (${input.clearance_mm} mm clearance)`
  );
  reasons.push(`${input.tire_class} tires`);

  const tractionNotes: string[] = [];
  if (input.front_locker && input.rear_locker) {
    tractionNotes.push("front & rear diff lockers");
  } else if (input.rear_locker) {
    tractionNotes.push("rear diff locker");
  } else if (input.front_locker) {
    tractionNotes.push("front diff locker");
  }
  if (input.has_winch) tractionNotes.push("recovery winch");
  if (input.has_snorkel) tractionNotes.push("snorkel");
  if (tractionNotes.length > 0) {
    reasons.push(`Equipped with ${tractionNotes.join(", ")}`);
  }

  const hasAnyLocker = input.front_locker || input.rear_locker;
  const hasTwinLockers = input.front_locker && input.rear_locker;
  const isAtOrMt = input.tire_class === "AT" || input.tire_class === "MT";

  const isModified =
    (input.tire_class === "MT" &&
      input.lift_mm >= 50 &&
      (hasAnyLocker || input.has_winch)) ||
    (hasTwinLockers &&
      isAtOrMt &&
      (input.lift_mm >= 50 || input.clearance_mm >= 250)) ||
    (input.tire_class === "MT" &&
      input.lift_mm >= 75 &&
      input.clearance_mm >= 260);

  if (isModified) {
    return { capability_class: "Modified 4WD", reasons };
  }

  const isTouring =
    isAtOrMt &&
    (input.lift_mm >= 25 ||
      input.clearance_mm >= 225 ||
      hasAnyLocker ||
      input.has_winch ||
      input.has_snorkel);

  if (isTouring) {
    return { capability_class: "Touring 4WD", reasons };
  }

  return { capability_class: "Stock 4WD", reasons };
}

const SUITABILITY_MATRIX: Record<
  string,
  Record<VehicleCapabilityClass, "Suited" | "Challenging" | "High Risk for your rig">
> = {
  easy: {
    AWD: "Suited",
    "Stock 4WD": "Suited",
    "Touring 4WD": "Suited",
    "Modified 4WD": "Suited",
  },
  medium: {
    AWD: "Challenging",
    "Stock 4WD": "Suited",
    "Touring 4WD": "Suited",
    "Modified 4WD": "Suited",
  },
  hard: {
    AWD: "High Risk for your rig",
    "Stock 4WD": "Challenging",
    "Touring 4WD": "Suited",
    "Modified 4WD": "Suited",
  },
  extreme: {
    AWD: "High Risk for your rig",
    "Stock 4WD": "High Risk for your rig",
    "Touring 4WD": "Challenging",
    "Modified 4WD": "Suited",
  },
};

/**
 * Deterministic client-side suitability fallback (matches backend baseline.py matrix)
 * used when inspecting tracks offline or before backend suitability attaches.
 */
export function computeClientSuitability(
  grade: string | null | undefined,
  vehicleClass: VehicleCapabilityClass,
  options?: {
    vehicleName?: string | null;
    closureStatus?: string | null;
  }
): TrackSuitability {
  const normGrade = (grade ?? "easy").trim().toLowerCase();
  const row = SUITABILITY_MATRIX[normGrade] ?? SUITABILITY_MATRIX.easy!;
  const rating = row[vehicleClass];
  const warnings: string[] = [];

  const closure = (options?.closureStatus ?? "open").toLowerCase();
  if (closure === "closed") {
    warnings.push("Track currently closed — check official closure notice.");
  } else if (closure === "seasonal") {
    warnings.push("Seasonal closure applies — confirm open dates before travel.");
  } else if (closure === "gated") {
    warnings.push("Locked gate or private property access restriction flagged.");
  }

  const labelGrade =
    normGrade.charAt(0).toUpperCase() + normGrade.slice(1);
  const reason =
    rating === "Suited"
      ? `${labelGrade} baseline terrain is well matched to ${vehicleClass} capability.`
      : rating === "Challenging"
        ? `${labelGrade} sections will test a ${vehicleClass}; carry recovery gear and check conditions.`
        : `${labelGrade} terrain exceeds recommended capability for ${vehicleClass}.`;

  return {
    vehicle_class: vehicleClass,
    vehicle_name: options?.vehicleName ?? null,
    rating,
    reason,
    warnings,
  };
}

export function getSuitabilityTone(
  rating: string
): "suited" | "challenging" | "high-risk" {
  const lower = rating.trim().toLowerCase();
  if (lower.includes("high risk")) return "high-risk";
  if (lower.includes("challenging")) return "challenging";
  return "suited";
}

export function buildTrackExportUrl(
  trackId: string,
  format: "gpx" | "geojson"
): string {
  return `/api/tracks/${encodeURIComponent(trackId)}/export?format=${format}`;
}

export function buildUserExportUrl(format: "gpx" | "geojson"): string {
  return `/api/user/export?format=${format}`;
}
