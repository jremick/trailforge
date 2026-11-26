import { describe, expect, it } from "vitest";
import {
  formatCoords,
  isValidMgrs,
  isWithinVicNswRegion,
  latLonToMgrs,
  looksLikeCoordOrMgrs,
  parseLatLon,
  parseMgrs,
  parseSearchCoordinate,
  resolveUtmZone,
} from "./coords";

describe("parseLatLon", () => {
  it("parses comma-separated signed decimal coordinates", () => {
    expect(parseLatLon("-36.615, 148.125")).toEqual({
      lat: -36.615,
      lon: 148.125,
    });
    expect(parseLatLon(" -33.732 , 150.312 ")).toEqual({
      lat: -33.732,
      lon: 150.312,
    });
  });

  it("parses space-separated decimals and hemisphere suffixes", () => {
    expect(parseLatLon("-36.456 148.263")).toEqual({
      lat: -36.456,
      lon: 148.263,
    });
    expect(parseLatLon("36.615°S, 148.125°E")).toEqual({
      lat: -36.615,
      lon: 148.125,
    });
    expect(parseLatLon("33.732S 150.312E")).toEqual({
      lat: -33.732,
      lon: 150.312,
    });
  });

  it("rejects out-of-range, conflicting sign, and non-coordinate strings", () => {
    expect(parseLatLon("")).toBeNull();
    expect(parseLatLon("Davies Plain Track")).toBeNull();
    expect(parseLatLon("4 4")).toBeNull();
    expect(parseLatLon("-91.0, 148.0")).toBeNull();
    expect(parseLatLon("-36.0, 181.0")).toBeNull();
    expect(parseLatLon("-36.5S, 148.1E")).toBeNull();
  });
});

describe("resolveUtmZone", () => {
  it("resolves VIC and NSW UTM zones and GZDs accurately", () => {
    // Grampians VIC -> 54H
    expect(resolveUtmZone(-37.145, 142.515)).toEqual({
      zone: 54,
      hemisphere: "S",
      epsg: 32754,
      gzd: "54H",
    });

    // Davies Plain / Australian Alps VIC/NSW -> 55H
    expect(resolveUtmZone(-36.615, 148.125)).toEqual({
      zone: 55,
      hemisphere: "S",
      epsg: 32755,
      gzd: "55H",
    });

    // Blue Mountains NSW -> 56H
    expect(resolveUtmZone(-33.732, 150.312)).toEqual({
      zone: 56,
      hemisphere: "S",
      epsg: 32756,
      gzd: "56H",
    });

    // Warrumbungles Northern NSW -> 55J
    expect(resolveUtmZone(-31.298, 149.005)).toEqual({
      zone: 55,
      hemisphere: "S",
      epsg: 32755,
      gzd: "55J",
    });
  });

  it("honors Norway and Svalbard MGRS zone exceptions", () => {
    expect(resolveUtmZone(60.39, 5.32).gzd).toBe("32V");
    expect(resolveUtmZone(78.22, 15.63).gzd).toBe("33X");
  });

  it("throws outside UTM latitude bounds", () => {
    expect(() => resolveUtmZone(-85, 148)).toThrow(RangeError);
    expect(() => resolveUtmZone(86, 148)).toThrow(RangeError);
  });
});

describe("MGRS encoding and decoding (100% offline)", () => {
  it("round-trips major VIC & NSW 4WD landmarks within 15 m at 10 m precision", () => {
    const landmarks = [
      { name: "Davies Plain Border", lat: -36.615, lon: 148.125, gzd: "55H" },
      { name: "Mt Kosciuszko", lat: -36.456, lon: 148.263, gzd: "55H" },
      { name: "Mt Bogong", lat: -36.733, lon: 147.306, gzd: "55H" },
      { name: "Blue Mountains Katoomba", lat: -33.732, lon: 150.312, gzd: "56H" },
      { name: "Grampians Halls Gap", lat: -37.145, lon: 142.515, gzd: "54H" },
      { name: "Warrumbungles NSW", lat: -31.298, lon: 149.005, gzd: "55J" },
      { name: "Canberra / Namadgi", lat: -35.3082, lon: 149.1244, gzd: "55H" },
    ];

    for (const pt of landmarks) {
      const mgrs10m = latLonToMgrs(pt.lat, pt.lon, 4, true);
      expect(mgrs10m.startsWith(pt.gzd)).toBe(true);

      const parsed = parseMgrs(mgrs10m);
      expect(parsed).not.toBeNull();
      expect(parsed!.gzd).toBe(pt.gzd);
      expect(parsed!.precisionMeters).toBe(10);
      // 10 m grid cell center is within ~0.00015° (~15 m)
      expect(Math.abs(parsed!.lat - pt.lat)).toBeLessThan(0.00015);
      expect(Math.abs(parsed!.lon - pt.lon)).toBeLessThan(0.00015);
    }
  });

  it("parses GridForge reference Blue Mountains MGRS string (case & spacing tolerant)", () => {
    const a = parseMgrs("56HKH5100064000");
    const b = parseMgrs("56h kh 510 640");
    expect(a).not.toBeNull();
    expect(b).not.toBeNull();
    expect(a!.zone).toBe(56);
    expect(a!.band).toBe("H");
    expect(a!.squareId).toBe("KH");
    expect(a!.precisionMeters).toBe(1);
    expect(b!.precisionMeters).toBe(100);
    // Katoomba Blue Mountains vicinity (-33.73, 150.31)
    expect(a!.lat).toBeCloseTo(-33.734, 2);
    expect(a!.lon).toBeCloseTo(150.311, 2);
    expect(Math.abs(a!.lat - b!.lat)).toBeLessThan(0.002);
    expect(Math.abs(a!.lon - b!.lon)).toBeLessThan(0.002);
  });

  it("supports Northern Hemisphere MGRS and coarse 100 km squares", () => {
    const dc = parseMgrs("18S UJ 2348 0647");
    expect(dc).not.toBeNull();
    expect(dc!.lat).toBeCloseTo(38.889, 2);
    expect(dc!.lon).toBeCloseTo(-77.035, 2);

    const coarse = parseMgrs("55H FV");
    expect(coarse).not.toBeNull();
    expect(coarse!.precisionMeters).toBe(100000);
    expect(coarse!.formatted).toBe("55H FV");
  });

  it("rejects malformed, odd-digit, polar UPS, invalid-column, and Svalbard-excluded MGRS", () => {
    expect(isValidMgrs("55H FV 123")).toBe(false); // odd digits
    expect(isValidMgrs("55I FV 1234 5678")).toBe(false); // I omitted in MGRS
    expect(isValidMgrs("55H OI 1234 5678")).toBe(false); // O/I omitted
    expect(isValidMgrs("A AA 1234 5678")).toBe(false); // polar UPS
    expect(isValidMgrs("32X AA 1000 1000")).toBe(false); // excluded Svalbard zone
    expect(isValidMgrs("55H KH 1000 1000")).toBe(false); // K invalid in zone 55 (A-H)
    expect(isValidMgrs("Davies Plain")).toBe(false);
  });
});

describe("parseSearchCoordinate & helpers", () => {
  it("dispatches lat/lon, MGRS, and free text appropriately", () => {
    const ll = parseSearchCoordinate("-36.615, 148.125");
    expect(ll?.kind).toBe("latlon");
    expect(ll?.lat).toBe(-36.615);

    const mg = parseSearchCoordinate("56H KH 5100 6400");
    expect(mg?.kind).toBe("mgrs");
    expect(looksLikeCoordOrMgrs("56H KH 5100 6400")).toBe(true);

    expect(parseSearchCoordinate("Tom Groggin Camp")).toBeNull();
    expect(looksLikeCoordOrMgrs("Tom Groggin Camp")).toBe(false);
  });

  it("checks VIC/NSW launch region bounds and formats coordinates", () => {
    expect(isWithinVicNswRegion(-36.615, 148.125)).toBe(true); // Alps
    expect(isWithinVicNswRegion(-33.732, 150.312)).toBe(true); // Blue Mountains
    expect(isWithinVicNswRegion(-31.95, 115.86)).toBe(false); // Perth WA
    expect(formatCoords(-36.615, 148.125, 3)).toBe("-36.615, 148.125");
  });
});
