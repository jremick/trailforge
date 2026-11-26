import { describe, expect, it } from "vitest";
import {
  MIN_PRIVACY_TRIM_M,
  buildTripExportUrl,
  formatMatchConfidence,
  formatTrimmedDistance,
  formatTripVisibility,
  parseTelemetryJsonInput,
  validatePrivacyTrimMeters,
} from "./tripsHelpers";

describe("Slice 06 trip privacy buffer & visibility helpers", () => {
  it("enforces mandatory >= 250 m privacy trim bounds", () => {
    expect(MIN_PRIVACY_TRIM_M).toBe(250);
    expect(validatePrivacyTrimMeters(0).valid).toBe(false);
    expect(validatePrivacyTrimMeters(249).valid).toBe(false);
    expect(validatePrivacyTrimMeters(5001).valid).toBe(false);
    expect(validatePrivacyTrimMeters(Number.NaN).valid).toBe(false);

    expect(validatePrivacyTrimMeters(250)).toEqual({ valid: true, value: 250 });
    expect(validatePrivacyTrimMeters(500)).toEqual({ valid: true, value: 500 });
  });

  it("formats Private vs Anonymous Community visibility cleanly", () => {
    const priv = formatTripVisibility("private");
    expect(priv.tone).toBe("private");
    expect(priv.label).toContain("Private");

    const comm = formatTripVisibility("community_anonymous");
    expect(comm.tone).toBe("community");
    expect(comm.description).toContain("zero user identity");
  });

  it("formats HMM confidence percentages and privacy-trimmed distance", () => {
    expect(formatMatchConfidence(0.918)).toBe("92%");
    expect(formatMatchConfidence(null)).toBe("—");
    expect(formatTrimmedDistance(4710, 4210)).toBe("4.21 km (500 m trimmed)");
  });

  it("parses valid telemetry JSON inputs and rejects malformed payloads", () => {
    const okObj = parseTelemetryJsonInput(
      JSON.stringify({
        points: [
          { lon: 148.12, lat: -36.57, elev_m: 620 },
          { lon: 148.125, lat: -36.61, elev_m: 980 },
        ],
      })
    );
    expect(okObj.ok).toBe(true);
    if (okObj.ok) {
      expect(okObj.points).toHaveLength(2);
      expect(okObj.points[0]?.elev_m).toBe(620);
    }

    const okArr = parseTelemetryJsonInput(
      JSON.stringify([
        { lon: 148.12, lat: -36.57 },
        { lon: 148.125, lat: -36.61 },
      ])
    );
    expect(okArr.ok).toBe(true);

    expect(parseTelemetryJsonInput("not json").ok).toBe(false);
    expect(parseTelemetryJsonInput('{"points":[{"lon":148,"lat":-36}]}').ok).toBe(
      false
    );
    expect(
      parseTelemetryJsonInput(
        '[{"lon":999,"lat":-36},{"lon":148,"lat":-36.5}]'
      ).ok
    ).toBe(false);
  });

  it("builds owner GPX/GeoJSON trip export URLs", () => {
    expect(buildTripExportUrl("trip-abc", "gpx")).toBe(
      "/api/trips/trip-abc/export?format=gpx"
    );
    expect(buildTripExportUrl("trip-abc", "geojson")).toBe(
      "/api/trips/trip-abc/export?format=geojson"
    );
  });
});
