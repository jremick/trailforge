import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchTrackDetail } from "./api";
import {
  buildTrackExportUrl,
  buildUserExportUrl,
  classifyVehiclePreview,
  computeClientSuitability,
  getSuitabilityTone,
} from "./vehicleHelpers";

describe("Slice 05 deterministic Vehicle Capability preview classifier", () => {
  it("classifies AWD and high-range-only vehicles as AWD", () => {
    const res = classifyVehiclePreview({
      name: "Subaru Forester",
      drivetrain: "awd",
      low_range: false,
      lift_mm: 0,
      clearance_mm: 220,
      tire_class: "AT",
      front_locker: false,
      rear_locker: false,
      has_winch: false,
      has_snorkel: false,
    });
    expect(res.capability_class).toBe("AWD");
  });

  it("classifies factory low-range 4WD on HT tyres as Stock 4WD", () => {
    const res = classifyVehiclePreview({
      name: "Stock Prado",
      drivetrain: "4wd_full_time",
      low_range: true,
      lift_mm: 0,
      clearance_mm: 215,
      tire_class: "HT",
      front_locker: false,
      rear_locker: false,
      has_winch: false,
      has_snorkel: false,
    });
    expect(res.capability_class).toBe("Stock 4WD");
  });

  it("classifies lifted AT low-range 4WD as Touring 4WD", () => {
    const res = classifyVehiclePreview({
      name: "Hilux SR5 Touring",
      drivetrain: "4wd_part_time",
      low_range: true,
      lift_mm: 50,
      clearance_mm: 255,
      tire_class: "AT",
      front_locker: false,
      rear_locker: true,
      has_winch: false,
      has_snorkel: true,
    });
    expect(res.capability_class).toBe("Touring 4WD");
  });

  it("classifies lifted MT low-range 4WD with lockers + winch as Modified 4WD", () => {
    const res = classifyVehiclePreview({
      name: "79 Series Built",
      drivetrain: "4wd_part_time",
      low_range: true,
      lift_mm: 75,
      clearance_mm: 290,
      tire_class: "MT",
      front_locker: true,
      rear_locker: true,
      has_winch: true,
      has_snorkel: true,
    });
    expect(res.capability_class).toBe("Modified 4WD");
  });
  it("matches backend classifier.py boundaries across Touring 4WD and Modified 4WD", () => {
    const base4wd = {
      name: "Boundary Rig",
      drivetrain: "4wd_part_time" as const,
      low_range: true,
      lift_mm: 0,
      clearance_mm: 210,
      tire_class: "AT" as const,
      front_locker: false,
      rear_locker: false,
      has_winch: false,
      has_snorkel: false,
    };

    // Stock 4WD baseline (below 25 mm lift and 225 mm clearance, no extras)
    expect(classifyVehiclePreview(base4wd).capability_class).toBe("Stock 4WD");
    expect(
      classifyVehiclePreview({ ...base4wd, lift_mm: 24, clearance_mm: 224 })
        .capability_class
    ).toBe("Stock 4WD");
    // HT tires remain Stock 4WD even with lift
    expect(
      classifyVehiclePreview({ ...base4wd, tire_class: "HT", lift_mm: 50 })
        .capability_class
    ).toBe("Stock 4WD");

    // Touring 4WD exact lower thresholds: lift >= 25 OR clearance >= 225
    expect(
      classifyVehiclePreview({ ...base4wd, lift_mm: 25 }).capability_class
    ).toBe("Touring 4WD");
    expect(
      classifyVehiclePreview({ ...base4wd, clearance_mm: 225 }).capability_class
    ).toBe("Touring 4WD");

    // Modified 4WD branch 1: MT + lift >= 50 + (any locker OR winch)
    expect(
      classifyVehiclePreview({
        ...base4wd,
        tire_class: "MT",
        lift_mm: 50,
        has_winch: true,
      }).capability_class
    ).toBe("Modified 4WD");
    expect(
      classifyVehiclePreview({
        ...base4wd,
        tire_class: "MT",
        lift_mm: 50,
        rear_locker: true,
      }).capability_class
    ).toBe("Modified 4WD");

    // Modified 4WD branch 2: twin lockers + AT + (lift >= 50 OR clearance >= 250)
    expect(
      classifyVehiclePreview({
        ...base4wd,
        tire_class: "AT",
        clearance_mm: 250,
        front_locker: true,
        rear_locker: true,
      }).capability_class
    ).toBe("Modified 4WD");

    // Modified 4WD branch 3: MT + lift >= 75 + clearance >= 260 (even without lockers/winch)
    expect(
      classifyVehiclePreview({
        ...base4wd,
        tire_class: "MT",
        lift_mm: 75,
        clearance_mm: 260,
      }).capability_class
    ).toBe("Modified 4WD");
  });
});

describe("Rig suitability guidance & GPX/GeoJSON export URLs", () => {
  it("computes personalized rig suitability across grades and closure warnings", () => {
    expect(computeClientSuitability("Easy", "AWD").rating).toBe("Suited");
    expect(computeClientSuitability("Medium", "AWD").rating).toBe("Challenging");
    expect(computeClientSuitability("Hard", "AWD").rating).toBe(
      "High Risk for your rig"
    );
    expect(computeClientSuitability("Extreme", "Touring 4WD").rating).toBe(
      "Challenging"
    );
    expect(computeClientSuitability("Extreme", "Modified 4WD").rating).toBe(
      "Suited"
    );

    const seasonal = computeClientSuitability("Hard", "Touring 4WD", {
      vehicleName: "Hilux",
      closureStatus: "seasonal",
    });
    expect(seasonal.vehicle_name).toBe("Hilux");
    expect(seasonal.warnings?.[0]).toContain("Seasonal closure");

    expect(getSuitabilityTone("Suited")).toBe("suited");
    expect(getSuitabilityTone("Challenging")).toBe("challenging");
    expect(getSuitabilityTone("High Risk for your rig")).toBe("high-risk");
  });

  it("builds GridForge-compatible GPX and GeoJSON export URLs", () => {
    expect(buildTrackExportUrl("trk_davies 01", "gpx")).toBe(
      "/api/tracks/trk_davies%2001/export?format=gpx"
    );
    expect(buildTrackExportUrl("trk_davies", "geojson")).toBe(
      "/api/tracks/trk_davies/export?format=geojson"
    );
    expect(buildUserExportUrl("gpx")).toBe("/api/user/export?format=gpx");
    expect(buildUserExportUrl("geojson")).toBe(
      "/api/user/export?format=geojson"
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("passes ?vehicle_class= on anonymous fetchTrackDetail requests", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          track_id: "trk_1",
          name: "Davies Plain Track",
          state: "VIC",
          total_length_m: 1200,
          elev_gain_m: 120,
          waterway_crossing_count: 1,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      )
    );
    vi.stubGlobal("fetch", fetchMock);

    await fetchTrackDetail("trk_1", { vehicleClass: "Touring 4WD" });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/tracks/trk_1?vehicle_class=Touring+4WD",
      expect.objectContaining({ method: "GET" })
    );
  });
});
