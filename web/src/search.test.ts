import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  MIN_CHARS_SEARCH_HINT,
  SEARCH_DEBOUNCE_MS,
  computeSearchClearTransition,
  computeSearchInputTransition,
  computeSearchResolvedTransition,
  computeSearchSubmitAction,
} from "./components/topbar";
import {
  MIN_REMOTE_SEARCH_LENGTH,
  SEARCH_CACHE_MAX_ENTRIES,
  SEARCH_CACHE_TTL_MS,
  clearSearchCache,
  formatAuPlaceSubtitle,
  getSearchCacheSize,
  hasCachedRemoteSearch,
  loadAuPlaces,
  mapApiSearchResults,
  matchAuPlaces,
  mergeUnifiedSearchResults,
  parseAuPlacesGeoJson,
  peekCachedUnifiedSearch,
  resetAuPlacesCache,
  resolveLocalSearch,
  runUnifiedSearch,
  setAuPlacesCache,
  shouldRunRemoteSearch,
  type AuPlaceInput,
} from "./search";
import type { ApiSearchResponse, UnifiedSearchItem } from "./types";

const SAMPLE_AU_PLACES: AuPlaceInput[] = [
  {
    id: "au-cap-brisbane",
    name: "Brisbane",
    place_class: "capital",
    state: "QLD",
    rank: 1,
    population: 2_500_000,
    lat: -27.4698,
    lon: 153.0251,
  },
  {
    id: "au-cap-melbourne",
    name: "Melbourne",
    place_class: "capital",
    state: "VIC",
    rank: 1,
    population: 5_000_000,
    lat: -37.8136,
    lon: 144.9631,
  },
  {
    id: "au-city-bendigo",
    name: "Bendigo",
    place_class: "city",
    state: "VIC",
    rank: 2,
    population: 100_000,
    lat: -36.757,
    lon: 144.2794,
  },
  {
    id: "au-state-vic",
    name: "Victoria",
    place_class: "state",
    state: "VIC",
    rank: 3,
    bbox: [140.96, -39.16, 149.98, -33.98],
    lat: -36.85,
    lon: 145.5,
  },
  {
    id: "au-state-nt",
    name: "Northern Territory",
    place_class: "state",
    state: "NT",
    rank: 3,
    bbox: [129.0, -26.0, 138.0, -10.9],
    lat: -19.5,
    lon: 133.4,
  },
  {
    id: "au-region-alps",
    name: "Australian Alps",
    place_class: "region",
    state: "VIC/NSW",
    rank: 5,
    bbox: [146.5, -37.5, 149.2, -35.5],
    lat: -36.6,
    lon: 148.1,
  },
  {
    id: "au-region-brindabellas",
    name: "Brindabellas",
    place_class: "region",
    state: "ACT/NSW",
    rank: 5,
    bbox: [148.6, -35.8, 149.0, -35.2],
    lat: -35.5,
    lon: 148.8,
  },
  {
    id: "au-town-bright",
    name: "Bright",
    place_class: "town",
    state: "VIC",
    rank: 6,
    population: 2500,
    region_hint: "Victorian High Country",
    lat: -36.728,
    lon: 146.961,
  },
  {
    id: "au-town-brighton",
    name: "Brighton",
    place_class: "town",
    state: "TAS",
    rank: 6,
    population: 4800,
    region_hint: "Greater Hobart",
    lat: -42.697,
    lon: 147.262,
  },
  {
    id: "au-town-dargo",
    name: "Dargo",
    place_class: "town",
    state: "VIC",
    rank: 6,
    population: 220,
    region_hint: "Victorian High Country",
    lat: -37.466,
    lon: 147.267,
  },
  {
    id: "au-locality-tom-groggin",
    name: "Tom Groggin",
    place_class: "locality",
    state: "NSW/VIC",
    rank: 8,
    region_hint: "Murray River · Kosciuszko gateway",
    lat: -36.551,
    lon: 148.128,
  },
];

beforeEach(() => {
  clearSearchCache();
  resetAuPlacesCache();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

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
    expect(
      items.some((i) => i.kind === "preset" && i.presetId === "blue-mountains")
    ).toBe(true);
  });

  it("resolves injected offline AU towns and regions with clean subtitles and bbox", () => {
    setAuPlacesCache(SAMPLE_AU_PLACES);

    const brightMatches = resolveLocalSearch("Bright");
    expect(brightMatches[0]?.kind).toBe("place");
    expect(brightMatches[0]?.title).toBe("Bright");
    expect(brightMatches[0]?.subtitle).toBe(
      "Town · VIC · Victorian High Country"
    );
    expect(brightMatches[0]?.lat).toBeCloseTo(-36.728, 3);
    expect(brightMatches[0]?.lon).toBeCloseTo(146.961, 3);

    const alpsMatches = resolveLocalSearch("Australian Alps");
    const regionItem = alpsMatches.find(
      (i) => i.kind === "place" && i.id === "au-region-alps"
    );
    expect(regionItem?.subtitle).toBe("Region · VIC/NSW");
    expect(regionItem?.bbox).toEqual([146.5, -37.5, 149.2, -35.5]);
  });
});

describe("AU places loading, parsing, matching, and ranking", () => {
  it("formats subtitles cleanly for towns, regions, capitals, states, and territories", () => {
    expect(
      formatAuPlaceSubtitle({
        name: "Bright",
        place_class: "town",
        state: "VIC",
        region_hint: "Victorian High Country",
      })
    ).toBe("Town · VIC · Victorian High Country");

    expect(
      formatAuPlaceSubtitle({
        name: "Australian Alps",
        place_class: "region",
        state: "VIC/NSW",
        region_hint: null,
      })
    ).toBe("Region · VIC/NSW");

    expect(
      formatAuPlaceSubtitle({
        name: "Northern Territory",
        place_class: "state",
        state: "NT",
        region_hint: null,
      })
    ).toBe("Territory · NT");
  });

  it("ranks exact > prefix > substring and higher-tier places first", () => {
    setAuPlacesCache(SAMPLE_AU_PLACES);

    // Exact match ("Bright") beats prefix match ("Brighton") even though Brighton has larger population
    const exactQuery = matchAuPlaces("Bright");
    expect(exactQuery.map((i) => i.title)).toEqual(["Bright", "Brighton"]);

    // Prefix matches sort by tier: Capital (Brisbane) > Region (Brindabellas) > Town (Brighton/Bright)
    const prefixQuery = matchAuPlaces("Bri");
    expect(prefixQuery[0]?.title).toBe("Brisbane");
    expect(prefixQuery[1]?.title).toBe("Brindabellas");
    expect(prefixQuery.slice(2).map((i) => i.title)).toContain("Bright");

    // Region hint matching surfaces gateway towns in Victorian High Country
    const hintQuery = matchAuPlaces("High Country");
    expect(hintQuery.map((i) => i.title)).toContain("Bright");
    expect(hintQuery.map((i) => i.title)).toContain("Dargo");
  });

  it("lazily fetches and caches /data/au_places.geojson and falls back gracefully on errors", async () => {
    const geojson = {
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          geometry: { type: "Point", coordinates: [146.961, -36.728] },
          properties: {
            id: "au-town-bright",
            name: "Bright",
            place_class: "town",
            state: "VIC",
            rank: 6,
            bbox: null,
            region_hint: "Victorian High Country",
          },
        },
        {
          type: "Feature",
          geometry: { type: "Point", coordinates: [148.1, -36.6] },
          properties: {
            id: "au-region-alps",
            name: "Australian Alps",
            place_class: "region",
            state: "VIC/NSW",
            rank: 5,
            bbox: "[146.5, -37.5, 149.2, -35.5]",
          },
        },
        // Invalid feature should be ignored safely
        {
          type: "Feature",
          geometry: { type: "Polygon", coordinates: [] },
          properties: { id: "bad", name: "Ignored" },
        },
      ],
    };

    const parsed = parseAuPlacesGeoJson(geojson);
    expect(parsed).toHaveLength(2);
    expect(parsed[1]?.bbox).toEqual([146.5, -37.5, 149.2, -35.5]);

    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => geojson,
    });

    const first = await loadAuPlaces({
      fetchImpl: fetchMock as unknown as typeof fetch,
    });
    const second = await loadAuPlaces({
      fetchImpl: fetchMock as unknown as typeof fetch,
    });
    expect(first).toHaveLength(2);
    expect(second).toHaveLength(2);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Graceful [] fallback when fetch fails
    resetAuPlacesCache();
    const failingFetch = vi.fn().mockRejectedValue(new Error("offline"));
    const fallback = await loadAuPlaces({
      fetchImpl: failingFetch as unknown as typeof fetch,
      force: true,
    });
    expect(fallback).toEqual([]);
  });
});

describe("mergeUnifiedSearchResults", () => {
  it("deduplicates same name+kind while prioritizing exact/strong tracks and preserving top towns", () => {
    setAuPlacesCache(SAMPLE_AU_PLACES);
    const localItems = resolveLocalSearch("Bright");

    const remoteItems: UnifiedSearchItem[] = [
      // Duplicate Nominatim place with same name+kind as local AU place
      {
        kind: "place",
        id: "nominatim:node:123",
        title: "Bright",
        subtitle: "Town · VIC · OSM place",
        lat: -36.728,
        lon: 146.961,
      },
      // Many prefix tracks that would otherwise push towns off a 10-item budget
      ...Array.from({ length: 10 }, (_, idx) => ({
        kind: "track" as const,
        id: `trk_bright_${idx + 1}`,
        title: `Bright Ridge Track ${idx + 1}`,
        subtitle: "4wd track · VIC",
        lat: -36.73,
        lon: 146.96,
        grade: "Hard",
      })),
    ];

    const merged = mergeUnifiedSearchResults("Bright", localItems, remoteItems, 10);
    expect(merged).toHaveLength(10);

    // "Bright" place should appear exactly once (curated local metadata preserved)
    const brightPlaces = merged.filter(
      (i) => i.kind === "place" && i.title.toLowerCase() === "bright"
    );
    expect(brightPlaces).toHaveLength(1);
    expect(brightPlaces[0]?.id).toBe("au-town-bright");
    expect(brightPlaces[0]?.subtitle).toBe("Town · VIC · Victorian High Country");

    // Prefix tracks remain strongly represented
    expect(merged.filter((i) => i.kind === "track").length).toBeGreaterThanOrEqual(8);
  });

  it("prioritizes exact baked tracks above exact places and reserves slots for prefix towns", () => {
    setAuPlacesCache(SAMPLE_AU_PLACES);
    const localBright = resolveLocalSearch("Bri");

    const prefixTracks: UnifiedSearchItem[] = Array.from({ length: 10 }, (_, i) => ({
      kind: "track",
      id: `trk_bri_${i}`,
      title: `Bridle Creek Track ${i + 1}`,
      subtitle: "4wd track · NSW",
      lat: -33.1,
      lon: 149.4,
      grade: "Medium",
    }));

    const mergedPrefix = mergeUnifiedSearchResults("Bri", localBright, prefixTracks, 10);
    expect(mergedPrefix).toHaveLength(10);
    expect(mergedPrefix[0]?.kind).toBe("track");
    // Top matching AU capital/region remain accessible within the 10-item budget
    expect(mergedPrefix.some((i) => i.id === "au-cap-brisbane")).toBe(true);
    expect(mergedPrefix.some((i) => i.id === "au-region-brindabellas")).toBe(true);

    // Exact track match ranks #1 even if a place has the same exact name
    const exactTrack: UnifiedSearchItem = {
      kind: "track",
      id: "trk_exact",
      title: "Bright",
      subtitle: "4wd track · VIC",
      lat: -36.73,
      lon: 146.96,
      grade: "Hard",
    };
    const mergedExact = mergeUnifiedSearchResults(
      "Bright",
      resolveLocalSearch("Bright"),
      [exactTrack],
      10
    );
    expect(mergedExact[0]?.id).toBe("trk_exact");
    expect(mergedExact[1]?.id).toBe("au-town-bright");
  });
});

describe("runUnifiedSearch guards and LRU cache", () => {
  it("enforces minimum 2-char remote guard and short-circuits coordinates/MGRS", async () => {
    setAuPlacesCache(SAMPLE_AU_PLACES);
    const remoteSearch = vi.fn();

    expect(MIN_REMOTE_SEARCH_LENGTH).toBe(2);
    expect(shouldRunRemoteSearch("")).toBe(false);
    expect(shouldRunRemoteSearch("b")).toBe(false);
    expect(shouldRunRemoteSearch("-36.615, 148.125")).toBe(false);
    expect(shouldRunRemoteSearch("56H KH 5100 6400")).toBe(false);
    expect(shouldRunRemoteSearch("br")).toBe(true);

    const oneChar = await runUnifiedSearch("b", { remoteSearch });
    expect(remoteSearch).not.toHaveBeenCalled();
    expect(oneChar.remoteError).toBe(false);
    expect(oneChar.remoteSkipped).toBe(true);
    expect(oneChar.items.some((i) => i.title === "Brisbane")).toBe(true);

    const mgrs = await runUnifiedSearch("56HKH5100064000", { remoteSearch });
    expect(remoteSearch).not.toHaveBeenCalled();
    expect(mgrs.remoteError).toBe(false);
    expect(mgrs.items[0]?.kind).toBe("mgrs");
  });

  it("caches remote search responses with normalized key, TTL expiry, LRU eviction, and clearSearchCache()", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));

    const mockResponse: ApiSearchResponse = {
      query: "davies",
      total: 1,
      results: [
        {
          kind: "track",
          id: "trk_davies_01",
          name: "Davies Plain Track",
          state: "VIC/NSW",
          subtitle: "4wd track",
          lon: 148.125,
          lat: -36.615,
          grade: "Extreme",
        },
      ],
    };

    const remoteSearch = vi.fn().mockResolvedValue(mockResponse);

    // First call populates cache
    const res1 = await runUnifiedSearch("  Davies ", { remoteSearch, limit: 10 });
    expect(remoteSearch).toHaveBeenCalledTimes(1);
    expect(res1.fromCache).toBe(false);
    expect(hasCachedRemoteSearch("davies", 10)).toBe(true);

    // Second call with different casing/whitespace hits cache in 0 ms
    const res2 = await runUnifiedSearch("DAVIES", { remoteSearch, limit: 10 });
    expect(remoteSearch).toHaveBeenCalledTimes(1);
    expect(res2.fromCache).toBe(true);
    expect(res2.items[0]?.id).toBe("trk_davies_01");

    // Synchronous peek helper returns merged results immediately
    const peeked = peekCachedUnifiedSearch("davies", { limit: 10 });
    expect(peeked?.[0]?.id).toBe("trk_davies_01");

    // Different limit uses a distinct cache key
    await runUnifiedSearch("davies", { remoteSearch, limit: 5 });
    expect(remoteSearch).toHaveBeenCalledTimes(2);

    // TTL expiration invalidates stale entry
    vi.advanceTimersByTime(SEARCH_CACHE_TTL_MS + 1);
    expect(hasCachedRemoteSearch("davies", 10)).toBe(false);
    const res3 = await runUnifiedSearch("davies", { remoteSearch, limit: 10 });
    expect(remoteSearch).toHaveBeenCalledTimes(3);
    expect(res3.fromCache).toBe(false);

    // clearSearchCache() invalidates all entries
    clearSearchCache();
    expect(getSearchCacheSize()).toBe(0);
    expect(hasCachedRemoteSearch("davies", 10)).toBe(false);

    // LRU capacity bound evicts oldest entries cleanly
    const lruRemote = vi.fn().mockImplementation(async (q: string) => ({
      query: q,
      total: 0,
      results: [],
    }));
    for (let i = 0; i < SEARCH_CACHE_MAX_ENTRIES + 5; i++) {
      await runUnifiedSearch(`track-${i}`, { remoteSearch: lruRemote, limit: 10 });
    }
    expect(getSearchCacheSize()).toBe(SEARCH_CACHE_MAX_ENTRIES);
    expect(hasCachedRemoteSearch("track-0", 10)).toBe(false);
    expect(hasCachedRemoteSearch(`track-${SEARCH_CACHE_MAX_ENTRIES + 4}`, 10)).toBe(
      true
    );
  });

  it("does not cache failed remote searches so recovery works immediately", async () => {
    const failing = vi.fn().mockRejectedValue(new Error("offline"));
    const first = await runUnifiedSearch("Grampians", {
      remoteSearch: failing,
      limit: 10,
    });
    expect(first.remoteError).toBe(true);
    expect(hasCachedRemoteSearch("Grampians", 10)).toBe(false);
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

  it("preserves AU geocoder place fallback results with bbox", () => {
    const mapped = mapApiSearchResults([
      {
        kind: "place",
        id: "nominatim:way:12345",
        name: "Billy Goat Bluff Track",
        state: "VIC",
        subtitle: "Track · Dargo · OSM place",
        category: "track",
        lon: 147.131,
        lat: -37.392,
        bbox: [147.11, -37.42, 147.16, -37.36],
      },
    ]);
    expect(mapped[0]?.kind).toBe("place");
    expect(mapped[0]?.iconKey).toBeNull();
    expect(mapped[0]?.bbox).toEqual([147.11, -37.42, 147.16, -37.36]);
    expect(mapped[0]?.subtitle).toContain("VIC");
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

describe("TopBar pure search state transitions", () => {
  it("uses 250ms debounce and keeps searching indicator + pending footer when instant local matches exist", () => {
    expect(SEARCH_DEBOUNCE_MS).toBe(250);
    setAuPlacesCache(SAMPLE_AU_PLACES);

    const localMatches = resolveLocalSearch("Bright");
    expect(localMatches.length).toBeGreaterThan(0);

    const partial = computeSearchInputTransition({
      rawValue: "Bright",
      localItems: localMatches,
      cachedItems: null,
    });

    expect(partial.isSearching).toBe(true);
    expect(partial.shouldScheduleRemote).toBe(true);
    expect(partial.menuMode).toBe("results");
    expect(partial.footerMode).toBe("searching");
    // Must NOT mark resultsQuery final for partial local-only results
    expect(partial.resultsQuery).toBe("");

    // Pressing Enter while partial local matches are showing waits for remote tracks
    const submitWhilePending = computeSearchSubmitAction({
      query: "Bright",
      resultsQuery: partial.resultsQuery,
      results: partial.results,
      activeIdx: partial.activeIdx,
    });
    expect(submitWhilePending.kind).toBe("execute_remote");

    // Once remote completes, searching clears, footer hides, and resultsQuery becomes final
    const finalItems: UnifiedSearchItem[] = [
      {
        kind: "track",
        id: "trk_bright_1",
        title: "Bright Spur Track",
        subtitle: "4wd track · VIC",
        lat: -36.73,
        lon: 146.96,
        grade: "Hard",
      },
      ...localMatches,
    ];
    const resolved = computeSearchResolvedTransition({
      query: "Bright",
      items: finalItems,
      remoteError: false,
    });
    expect(resolved.isSearching).toBe(false);
    expect(resolved.footerMode).toBe("none");
    expect(resolved.resultsQuery).toBe("Bright");
    expect(resolved.menuMode).toBe("results");

    // Pressing Enter after final results selects the highlighted item immediately
    const submitAfterFinal = computeSearchSubmitAction({
      query: "Bright",
      resultsQuery: resolved.resultsQuery,
      results: resolved.results,
      activeIdx: resolved.activeIdx,
    });
    expect(submitAfterFinal).toEqual({ kind: "select", item: finalItems[0] });
  });

  it("shows skeleton searching mode when no instant local matches exist", () => {
    const transition = computeSearchInputTransition({
      rawValue: "Billy Goat",
      localItems: [],
      cachedItems: null,
    });
    expect(transition.isSearching).toBe(true);
    expect(transition.shouldScheduleRemote).toBe(true);
    expect(transition.menuMode).toBe("searching");
    expect(transition.footerMode).toBe("none");
    expect(transition.resultsQuery).toBe("");
  });

  it("handles 1-char queries locally with hint and skips remote search", () => {
    expect(MIN_CHARS_SEARCH_HINT).toContain("2+");

    const noLocal = computeSearchInputTransition({
      rawValue: "z",
      localItems: [],
      cachedItems: null,
    });
    expect(noLocal.isSearching).toBe(false);
    expect(noLocal.shouldScheduleRemote).toBe(false);
    expect(noLocal.shouldCancelPending).toBe(true);
    expect(noLocal.menuMode).toBe("hint");
    expect(
      computeSearchSubmitAction({
        query: "z",
        resultsQuery: noLocal.resultsQuery,
        results: noLocal.results,
        activeIdx: noLocal.activeIdx,
      })
    ).toEqual({ kind: "hint" });

    setAuPlacesCache(SAMPLE_AU_PLACES);
    const localForB = resolveLocalSearch("b");
    const withLocal = computeSearchInputTransition({
      rawValue: "b",
      localItems: localForB,
      cachedItems: null,
    });
    expect(withLocal.isSearching).toBe(false);
    expect(withLocal.shouldScheduleRemote).toBe(false);
    expect(withLocal.menuMode).toBe("results");
    expect(withLocal.footerMode).toBe("min_chars_hint");
  });

  it("renders exact coordinates and cached queries instantaneously without spinner", () => {
    const coordLocal = resolveLocalSearch("-36.615, 148.125");
    const coordTransition = computeSearchInputTransition({
      rawValue: "-36.615, 148.125",
      localItems: coordLocal,
      cachedItems: null,
    });
    expect(coordTransition.isSearching).toBe(false);
    expect(coordTransition.shouldScheduleRemote).toBe(false);
    expect(coordTransition.resultsQuery).toBe("-36.615, 148.125");
    expect(coordTransition.footerMode).toBe("none");

    const cachedItem: UnifiedSearchItem = {
      kind: "track",
      id: "trk_cached",
      title: "Davies Plain Track",
      subtitle: "4wd track · VIC/NSW",
      lat: -36.615,
      lon: 148.125,
      grade: "Extreme",
    };
    const cacheHit = computeSearchInputTransition({
      rawValue: "davies",
      localItems: [],
      cachedItems: [cachedItem],
    });
    expect(cacheHit.isSearching).toBe(false);
    expect(cacheHit.shouldScheduleRemote).toBe(false);
    expect(cacheHit.resultsQuery).toBe("davies");
    expect(cacheHit.menuMode).toBe("results");
    expect(cacheHit.footerMode).toBe("none");

    const cleared = computeSearchClearTransition();
    expect(cleared.isSearching).toBe(false);
    expect(cleared.menuMode).toBe("hidden");
    expect(cleared.results).toEqual([]);
    expect(cleared.resultsQuery).toBe("");
  });
});
