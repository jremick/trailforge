import { describe, expect, it, vi } from "vitest";
import {
  mapApiSearchResults,
  resolveLocalSearch,
  runUnifiedSearch,
} from "./search";
import type { ApiSearchResponse } from "./types";

describe("resolveLocalSearch (100% offline)", () => {
  it("resolves lat,lon coordinates locally without network calls", () => {
    const items = resolveLocalSearch("-36.615, 148.125");
    expect(items).toHaveLength(1);
    expect(items[0]?.kind).toBe("coordinate");
    expect(items[0]?.lat).toBe(-36.615);
    expect(items[0]?.lon).toBe(148.125);
  });

  it("resolves MGRS strings locally without network calls", () => {
    const items = resolveLocalSearch("56H KH 5100 6400");
    expect(items).toHaveLength(1);
    expect(items[0]?.kind).toBe("mgrs");
    expect(items[0]?.title).toBe("56H KH 5100 6400");
    expect(items[0]?.lat).toBeCloseTo(-33.734, 2);
    expect(items[0]?.lon).toBeCloseTo(150.311, 2);
  });

  it("matches curated VIC/NSW region presets offline", () => {
    const items = resolveLocalSearch("Blue Mountains");
    expect(items.some((i) => i.kind === "preset" && i.presetId === "blue-mountains")).toBe(
      true
    );
  });
});

describe("runUnifiedSearch", () => {
  it("short-circuits remote search for exact MGRS/lat,lon inputs", async () => {
    const remoteSearch = vi.fn();
    const res = await runUnifiedSearch("56HKH5100064000", { remoteSearch });
    expect(remoteSearch).not.toHaveBeenCalled();
    expect(res.remoteError).toBe(false);
    expect(res.items[0]?.kind).toBe("mgrs");
  });

  it("merges backend tracks, POIs, and CAPAD parks with icons and grades", async () => {
    const mockResponse: ApiSearchResponse = {
      query: "davies",
      total: 3,
      results: [
        {
          kind: "track",
          id: "trk_davies_01",
          name: "Davies Plain Track",
          state: "VIC/NSW",
          subtitle: "4wd track",
          lon: 148.125,
          lat: -36.615,
          bbox: [148.08, -36.67, 148.15, -36.56],
          grade: "Extreme",
        },
        {
          kind: "poi",
          id: "poi_hut_1",
          name: "Davies Plain Hut",
          state: "VIC",
          category: "hut",
          elev_m: 1615,
          lon: 148.126,
          lat: -36.662,
        },
        {
          kind: "park",
          id: "pk_1",
          name: "Alpine National Park",
          state: "VIC",
          subtitle: "National Park",
          lon: 148.11,
          lat: -36.64,
        },
      ],
    };

    const remoteSearch = vi.fn().mockResolvedValue(mockResponse);
    const { items, remoteError } = await runUnifiedSearch("davies", {
      remoteSearch,
    });

    expect(remoteError).toBe(false);
    expect(remoteSearch).toHaveBeenCalledWith("davies", 12, undefined);

    const track = items.find((i) => i.kind === "track");
    expect(track?.id).toBe("trk_davies_01");
    expect(track?.grade).toBe("Extreme");
    expect(track?.bbox).toEqual([148.08, -36.67, 148.15, -36.56]);

    const poi = items.find((i) => i.kind === "poi");
    expect(poi?.iconKey).toBe("hut");
    expect(poi?.subtitle).toContain("1615 m");
  });

  it("falls back gracefully to local presets when backend is offline", async () => {
    const remoteSearch = vi.fn().mockRejectedValue(new Error("Network down"));
    const { items, remoteError } = await runUnifiedSearch("Grampians", {
      remoteSearch,
    });
    expect(remoteError).toBe(true);
    expect(items.some((i) => i.presetId === "grampians")).toBe(true);
  });

  it("maps unknown POI types safely to fallback icon", () => {
    const mapped = mapApiSearchResults([
      {
        kind: "poi",
        id: "99",
        name: "Tom Groggin Campground",
        state: "NSW",
        subtitle: "campground",
        lon: 148.12,
        lat: -36.56,
      },
    ]);
    expect(mapped[0]?.iconKey).toBe("campground");
  });
});
