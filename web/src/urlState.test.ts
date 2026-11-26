import { describe, expect, it } from "vitest";
import {
  MAX_ROUTE_URL_TRACKS,
  buildAppUrlSearch,
  parseAppUrlState,
  parseRouteTrackIdsParam,
  parseVehicleClassParam,
} from "./urlState";

describe("Shareable deep-link URL state helpers", () => {
  it("parses valid track, region, map viewport coordinates, and profile flag", () => {
    const state = parseAppUrlState(
      "?track=tftrk_721db6efef73&region=australian-alps&lat=-37.4256&lon=147.0609&z=12.80&profile=1"
    );
    expect(state).toEqual({
      trackId: "tftrk_721db6efef73",
      regionId: "australian-alps",
      lat: -37.4256,
      lon: 147.0609,
      zoom: 12.8,
      profileOpen: true,
      routeTrackIds: [],
      rigClass: null,
    });
  });

  it("parses ordered ?route=<id1,id2,...>&rig=<vehicle_class> deep links", () => {
    const state = parseAppUrlState(
      "?route=tftrk_a1,tftrk_b2,tftrk_c3&rig=Touring%204WD&region=australian-alps"
    );
    expect(state.routeTrackIds).toEqual(["tftrk_a1", "tftrk_b2", "tftrk_c3"]);
    expect(state.rigClass).toBe("Touring 4WD");
    expect(state.regionId).toBe("australian-alps");
    expect(state.trackId).toBeNull();
    expect(state.profileOpen).toBe(false);
  });

  it("normalizes canonical vehicle classes flexibly and rejects invalid rig names", () => {
    expect(parseVehicleClassParam("AWD")).toBe("AWD");
    expect(parseVehicleClassParam("awd")).toBe("AWD");
    expect(parseVehicleClassParam("stock_4wd")).toBe("Stock 4WD");
    expect(parseVehicleClassParam("Stock-4WD")).toBe("Stock 4WD");
    expect(parseVehicleClassParam("touring   4wd")).toBe("Touring 4WD");
    expect(parseVehicleClassParam("Modified 4WD")).toBe("Modified 4WD");
    expect(parseVehicleClassParam("monster-truck")).toBeNull();
    expect(parseVehicleClassParam("<script>alert(1)</script>")).toBeNull();
    expect(parseVehicleClassParam("")).toBeNull();
  });

  it("validates route track IDs, preserves order, supports repeated route params, and caps at 30 legs", () => {
    const state = parseAppUrlState(
      "?route=tftrk_1,bad/id%20here,,tftrk_2&route=tftrk_3&rig=stock_4wd"
    );
    expect(state.routeTrackIds).toEqual(["tftrk_1", "tftrk_2", "tftrk_3"]);
    expect(state.rigClass).toBe("Stock 4WD");

    const many = Array.from({ length: 40 }, (_, i) => `tftrk_${i + 1}`).join(",");
    const capped = parseRouteTrackIdsParam([many]);
    expect(capped).toHaveLength(MAX_ROUTE_URL_TRACKS);
    expect(capped[0]).toBe("tftrk_1");
    expect(capped[MAX_ROUTE_URL_TRACKS - 1]).toBe(`tftrk_${MAX_ROUTE_URL_TRACKS}`);
  });

  it("rejects out-of-range coordinates, invalid track IDs, unknown regions, invalid rig, and orphan profile flags", () => {
    const state = parseAppUrlState(
      "?track=bad/id%20here&route=bad/one,also%20bad&rig=hovercraft&region=mars-crater&lat=199&lon=147&z=99&profile=1"
    );
    expect(state).toEqual({
      trackId: null,
      regionId: null,
      lat: null,
      lon: null,
      zoom: null,
      profileOpen: false,
      routeTrackIds: [],
      rigClass: null,
    });
  });

  it("builds clean deterministic query strings while preserving unrelated query params", () => {
    const qs = buildAppUrlSearch(
      {
        trackId: "tftrk_721db6efef73",
        regionId: "australian-alps",
        lat: -37.425576,
        lon: 147.060853,
        zoom: 12.843,
        profileOpen: true,
      },
      "?utm_source=club&lat=0&lon=0"
    );
    expect(qs).toBe(
      "?track=tftrk_721db6efef73&profile=1&region=australian-alps&lat=-37.4256&lon=147.0609&z=12.84&utm_source=club"
    );

    // Clearing track also clears profile flag
    const cleared = buildAppUrlSearch(
      {
        trackId: null,
        profileOpen: true,
        lat: -36.615,
        lon: 148.125,
        zoom: 11.4,
      },
      qs
    );
    expect(cleared).toBe("?lat=-36.6150&lon=148.1250&z=11.40&utm_source=club");
  });

  it("serializes and round-trips ?route and ?rig cleanly", () => {
    const qs = buildAppUrlSearch(
      {
        routeTrackIds: ["tftrk_davies", "tftrk_tom_groggin", "bad/id"],
        rigClass: "Touring 4WD",
        regionId: "australian-alps",
      },
      "?utm_campaign=mcp"
    );
    expect(qs).toBe(
      "?route=tftrk_davies,tftrk_tom_groggin&rig=Touring+4WD&region=australian-alps&utm_campaign=mcp"
    );

    const parsed = parseAppUrlState(qs);
    expect(parsed.routeTrackIds).toEqual(["tftrk_davies", "tftrk_tom_groggin"]);
    expect(parsed.rigClass).toBe("Touring 4WD");
    expect(parsed.regionId).toBe("australian-alps");
  });
});
