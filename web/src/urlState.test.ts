import { describe, expect, it } from "vitest";
import { buildAppUrlSearch, parseAppUrlState } from "./urlState";

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
    });
  });

  it("rejects out-of-range coordinates, invalid track IDs, unknown regions, and orphan profile flags", () => {
    const state = parseAppUrlState(
      "?track=bad/id%20here&region=mars-crater&lat=199&lon=147&z=99&profile=1"
    );
    expect(state).toEqual({
      trackId: null,
      regionId: null,
      lat: null,
      lon: null,
      zoom: null,
      profileOpen: false,
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
});
