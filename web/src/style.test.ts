import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, fetchHealth } from "./api";
import { isWithinAustralia } from "./coords";
import {
  DEFAULT_SNOWLINE_BANDS_M,
  PAN_AU_PACK_PRESETS,
  createInitialLayerState,
  snapSnowlineBand,
} from "./presets";
import {
  SELECTED_CLOSURE_OUTLINE_LAYER_ID,
  SELECTED_TRACK_LAYER_IDS,
  SELECTED_TRACK_STEEP_LAYER_ID,
  buildOfflineGraticuleGeoJson,
  buildRegionalBoundsGeoJson,
  buildSelectedClosureFilter,
  buildSnowlineFilter,
  buildTrailForgeStyle,
  getTrackSelectionOpacities,
  poisToGeoJson,
  toPmtilesUrl,
  userPinsToGeoJson,
} from "./style";
import type { TileManifestResponse } from "./types";

const SAMPLE_MANIFEST: TileManifestResponse = {
  generated_at: "2026-11-26T00:00:00Z",
  bounds: [148.05, -36.68, 148.2, -36.55],
  minzoom: 8,
  maxzoom: 15,
  vector_url: "/api/tiles/trailforge_vector.pmtiles",
  terrain_url: "/api/tiles/trailforge_terrain.pmtiles",
  vector_available: true,
  terrain_available: true,
  layers: [
    "track_segments",
    "steep_sections",
    "waterways",
    "pois",
    "capad_parks",
    "contours",
    "snowline_bands",
  ],
  snowline_bands_m: [800, 900, 1000, 1100, 1200, 1400, 1600],
};

describe("MapLibre 5 PMTiles & offline fallback style builder", () => {
  it("resolves relative PMTiles URLs against origin and preserves absolute Cloudflare R2 URLs without prepending origin", () => {
    expect(
      toPmtilesUrl("/api/tiles/trailforge_vector.pmtiles", "http://localhost:5173")
    ).toBe("pmtiles://http://localhost:5173/api/tiles/trailforge_vector.pmtiles");
    expect(
      toPmtilesUrl(
        "https://tiles.example.com/trailforge_vector.pmtiles",
        "https://app.example.com"
      )
    ).toBe("pmtiles://https://tiles.example.com/trailforge_vector.pmtiles");
    expect(
      toPmtilesUrl(
        "http://192.0.2.10:9000/trailforge_terrain.pmtiles",
        "http://localhost:5173"
      )
    ).toBe("pmtiles://http://192.0.2.10:9000/trailforge_terrain.pmtiles");
    expect(
      toPmtilesUrl("https://tiles.example.org/alps.pmtiles")
    ).toBe("pmtiles://https://tiles.example.org/alps.pmtiles");
    expect(
      toPmtilesUrl("pmtiles://http://localhost:5173/a.pmtiles", "https://app.example.com")
    ).toBe("pmtiles://http://localhost:5173/a.pmtiles");
  });

  it("mounts absolute Cloudflare R2 PMTiles URLs directly without prepending app origin", () => {
    const r2Manifest: TileManifestResponse = {
      ...SAMPLE_MANIFEST,
      vector_url: "https://tiles.example.com/trailforge_vector.pmtiles",
      terrain_url: "https://tiles.example.com/trailforge_terrain.pmtiles",
      cdn_offload: true,
    };
    const style = buildTrailForgeStyle({
      initialBasemap: "offline-topo",
      layers: createInitialLayerState(),
      tileManifest: r2Manifest,
      origin: "https://app.example.com",
    });

    expect(style.sources["tf-vector"]).toEqual({
      type: "vector",
      url: "pmtiles://https://tiles.example.com/trailforge_vector.pmtiles",
    });
    expect(style.sources["tf-terrain-dem"]).toEqual({
      type: "raster-dem",
      url: "pmtiles://https://tiles.example.com/trailforge_terrain.pmtiles",
      encoding: "mapbox",
      tileSize: 256,
    });
  });

  it("snaps snow-line elevations to discrete manifest bands and builds MapLibre filter", () => {
    expect(snapSnowlineBand(1000, DEFAULT_SNOWLINE_BANDS_M)).toBe(1000);
    expect(snapSnowlineBand(1320, DEFAULT_SNOWLINE_BANDS_M)).toBe(1400);
    expect(snapSnowlineBand(700, DEFAULT_SNOWLINE_BANDS_M)).toBe(800);
    expect(buildSnowlineFilter(1200)).toEqual(["==", ["get", "elev_min_m"], 1200]);
  });

  it("mounts vector PMTiles layers and Terrain-RGB hillshade when manifest is available", () => {
    const layers = { ...createInitialLayerState(), snowLine: true, snowLineElevationM: 1200 };
    const style = buildTrailForgeStyle({
      initialBasemap: "offline-topo",
      layers,
      tileManifest: SAMPLE_MANIFEST,
      origin: "http://localhost:5173",
      selectedTrackId: "trk_davies",
    });

    expect(style.version).toBe(8);
    expect(style.sources["tf-vector"]).toEqual({
      type: "vector",
      url: "pmtiles://http://localhost:5173/api/tiles/trailforge_vector.pmtiles",
    });
    expect(style.sources["tf-terrain-dem"]).toEqual({
      type: "raster-dem",
      url: "pmtiles://http://localhost:5173/api/tiles/trailforge_terrain.pmtiles",
      encoding: "mapbox",
      tileSize: 256,
    });

    const byId = new Map(style.layers.map((l) => [l.id, l]));
    expect(byId.get("tf-hillshade")?.type).toBe("hillshade");
    expect(byId.get("tf-parks-fill")?.["source-layer" as keyof ReturnType<typeof byId.get>]).toBe(
      "capad_parks"
    );
    expect(byId.get("tf-snow-line-fill")).toMatchObject({
      source: "tf-vector",
      "source-layer": "snowline_bands",
      filter: ["==", ["get", "elev_min_m"], 1200],
      layout: { visibility: "visible" },
    });
    expect(byId.get("tf-contours-index")).toMatchObject({
      "source-layer": "contours",
    });
    expect(byId.get("tf-contours-labels")).toMatchObject({
      type: "symbol",
      "source-layer": "contours",
      layout: {
        "symbol-spacing": 520,
        "icon-padding": 28,
      },
      paint: {
        "icon-opacity": 0.78,
      },
    });
    expect(byId.get("tf-pois-layer")).toMatchObject({
      type: "circle",
      paint: {
        "circle-color": "rgba(241, 245, 249, 0.96)",
      },
    });
    expect(byId.get("tf-tracks-line")).toMatchObject({
      source: "tf-vector",
      "source-layer": "track_segments",
    });
    expect(byId.get("tf-steep-highlights")).toMatchObject({
      source: "tf-vector",
      "source-layer": "steep_sections",
    });
    expect(byId.get("tf-pois-icons")).toMatchObject({
      source: "tf-vector",
      "source-layer": "pois",
    });
    // Slice 04 closure hazard & Slice 05 personal pin layers mounted
    expect(byId.get("tf-closures-line")).toMatchObject({
      source: "tf-closures",
      layout: { visibility: "visible" },
    });
    expect(byId.get("tf-closures-polygon-fill")).toMatchObject({
      source: "tf-closures",
      layout: { visibility: "visible" },
    });
    expect(byId.get(SELECTED_CLOSURE_OUTLINE_LAYER_ID)).toMatchObject({
      source: "tf-closures",
      layout: { visibility: "visible" },
    });
    expect(buildSelectedClosureFilter(42)).toEqual([
      "all",
      ["in", ["geometry-type"], ["literal", ["Polygon", "LineString"]]],
      ["==", ["to-string", ["coalesce", ["get", "closure_id"], ""]], "42"],
    ]);
    // Slice 06 unmapped spurs (hidden by default) & Slice 08 reports/gate warnings
    expect(byId.get("tf-unmapped-line")).toMatchObject({
      source: "tf-unmapped",
      layout: { visibility: "none" },
    });
    expect(byId.get("tf-reports-gate-halo")).toMatchObject({
      source: "tf-reports",
      layout: { visibility: "visible" },
    });
    expect(byId.get("tf-reports-points")).toMatchObject({
      source: "tf-reports",
      layout: { visibility: "visible" },
    });
    expect(byId.get("tf-user-pins-dot")).toMatchObject({
      source: "tf-user-pins",
    });
    // Slice 07 obstacle hotspots & Slice 09 in-cab recorder + backtrack layers
    expect(byId.get("tf-hotspots-points")).toMatchObject({
      source: "tf-hotspots",
      layout: { visibility: "visible" },
    });
    expect(byId.get("tf-hotspots-line")).toMatchObject({
      source: "tf-hotspots",
      layout: { visibility: "visible" },
    });
    expect(byId.get("tf-recorded-track-line")).toMatchObject({
      source: "tf-recorded-track",
    });
    expect(byId.get("tf-backtrack-route-line")).toMatchObject({
      source: "tf-backtrack-route",
    });

    // Regression: ALL POI icons & point markers must render ABOVE every line layer
    const maxLineIdx = style.layers.reduce(
      (max, l, idx) => (l.type === "line" ? Math.max(max, idx) : max),
      -1
    );
    const layerIdx = (id: string) => style.layers.findIndex((l) => l.id === id);
    expect(layerIdx("tf-pois-layer")).toBeGreaterThan(maxLineIdx);
    expect(layerIdx("tf-pois-icons")).toBeGreaterThan(layerIdx("tf-pois-layer"));
    expect(layerIdx("tf-closures-points")).toBeGreaterThan(maxLineIdx);
    expect(layerIdx("tf-reports-points")).toBeGreaterThan(maxLineIdx);
    expect(layerIdx("tf-planned-route-pins-badge")).toBeGreaterThan(maxLineIdx);
    expect(layerIdx("tf-user-pins-dot")).toBeGreaterThan(maxLineIdx);
    expect(layerIdx("tf-search-pin-dot")).toBeGreaterThan(maxLineIdx);

    // Regression: selected track spotlight renders ABOVE unselected tracks & steep ticks
    // while receding unselected tracks and keeping waterways visually distinct from Medium tracks
    const baseTracksMaxIdx = Math.max(
      layerIdx("tf-tracks-casing"),
      layerIdx("tf-tracks-line"),
      layerIdx("tf-steep-highlights")
    );
    for (const selId of [...SELECTED_TRACK_LAYER_IDS, SELECTED_TRACK_STEEP_LAYER_ID]) {
      expect(layerIdx(selId)).toBeGreaterThan(baseTracksMaxIdx);
      expect(layerIdx(selId)).toBeLessThan(layerIdx("tf-pois-layer"));
    }
    const dimmed = getTrackSelectionOpacities(true);
    const normal = getTrackSelectionOpacities(false);
    expect(dimmed.line).toBeLessThan(normal.line);
    expect(dimmed.casing).toBeLessThan(normal.casing);
    expect((byId.get("tf-tracks-line")?.paint as Record<string, unknown>)["line-opacity"]).toBe(
      dimmed.line
    );
    expect(
      (byId.get("tf-tracks-selected-core")?.paint as Record<string, unknown>)["line-opacity"]
    ).toBe(1.0);
    expect(
      (byId.get("tf-waterways-perennial")?.paint as Record<string, unknown>)["line-color"]
    ).not.toBe("#38BDF8");
    expect((byId.get("tf-waterways-intermittent") as { minzoom?: number })?.minzoom).toBeGreaterThanOrEqual(12);
  });

  it("converts Slice 05 personal user pins into category-colored GeoJSON", () => {
    const fc = userPinsToGeoJson([
      {
        id: "pin-1",
        name: "Murray Crossing Camp",
        category: "camp",
        notes: "Flat riverside clearing",
        lon: 148.121,
        lat: -36.565,
      },
    ]);
    expect(fc.features).toHaveLength(1);
    expect(fc.features[0]?.properties).toMatchObject({
      id: "pin-1",
      name: "Murray Crossing Camp",
      category: "camp",
      color: "#22C55E",
    });
  });

  it("builds a clean GeoJSON fallback style when PMTiles manifest is unavailable", () => {
    const layers = createInitialLayerState();
    const style = buildTrailForgeStyle({
      initialBasemap: "offline-topo",
      layers,
    });

    expect(style.sources["tf-vector"]).toBeUndefined();
    expect(style.sources["tf-terrain-dem"]).toBeUndefined();
    expect(style.sources["tf-tracks"]).toBeDefined();
    expect(style.sources["tf-pois"]).toBeDefined();

    const byId = new Map(style.layers.map((l) => [l.id, l]));
    expect(byId.get("bg-dark-outdoors")?.type).toBe("background");
    expect(byId.get("basemap-osm-underlay")?.layout).toEqual({
      visibility: "none",
    });
    expect(byId.get("basemap-opentopo")?.layout).toEqual({
      visibility: "none",
    });
    expect(byId.get("basemap-satellite")?.layout).toEqual({
      visibility: "none",
    });
    expect(
      (byId.get("tf-snow-line-fill")?.layout as { visibility?: string })
        ?.visibility
    ).toBe("none");

    // OpenTopoMap basemap must not simultaneously load OSM raster tiles underneath
    const openTopoStyle = buildTrailForgeStyle({
      initialBasemap: "opentopo",
      layers,
    });
    const otById = new Map(openTopoStyle.layers.map((l) => [l.id, l]));
    expect(otById.get("basemap-opentopo")?.layout).toEqual({ visibility: "visible" });
    expect(otById.get("basemap-osm-underlay")?.layout).toEqual({ visibility: "none" });
  });

  it("converts fallback /api/pois items into GeoJSON with resolved outdoor icons", () => {
    const fc = poisToGeoJson([
      {
        id: 1,
        name: "Davies Plain Hut",
        poi_type: "hut",
        elev_m: 1615,
        lon: 148.126,
        lat: -36.662,
      },
      {
        id: 2,
        name: "Tom Groggin Campground",
        poi_type: "campground",
        elev_m: 612,
        lon: 148.1195,
        lat: -36.5602,
      },
    ]);
    expect(fc.features).toHaveLength(2);
    expect(fc.features[0]?.properties?.icon).toBe("hut");
    expect(fc.features[1]?.properties?.icon).toBe("campground");
  });

  it("generates non-empty offline graticule and corridor GeoJSON", () => {
    const graticule = buildOfflineGraticuleGeoJson();
    expect(graticule.features.length).toBeGreaterThan(25);

    const regions = buildRegionalBoundsGeoJson();
    expect(regions.features.length).toBe(3);
  });

  it("covers all 15 Pan-Australia preset centers inside Australia bounds", () => {
    expect(PAN_AU_PACK_PRESETS).toHaveLength(15);
    for (const preset of PAN_AU_PACK_PRESETS) {
      expect(
        isWithinAustralia(preset.center[1], preset.center[0]),
        `Center out of Australia bounds for ${preset.id}`
      ).toBe(true);
    }
    expect(isWithinAustralia(51.5, -0.12)).toBe(false);
  });
});

describe("fetchHealth (mocked offline)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns parsed /api/health payload when backend responds 200", async () => {
    const payload = {
      status: "ok",
      service: "trailforge-api",
      version: "1.1.0",
      db_ok: true,
      postgis_version: "3.4",
      schema_revision: "0001_initial",
    };

    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const res = await fetchHealth();
    expect(res).toEqual(payload);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/health",
      expect.objectContaining({ method: "GET" })
    );
  });

  it("returns structured degraded ApiHealthResponse on HTTP 503 when db_ok is false", async () => {
    const degradedPayload = {
      status: "degraded",
      service: "trailforge-api",
      version: "1.1.0",
      db_ok: false,
      postgis_version: null,
      schema_revision: null,
    };

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(degradedPayload), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        })
      )
    );

    const res = await fetchHealth();
    expect(res).toEqual(degradedPayload);
    expect(res.db_ok).toBe(false);
    expect(res.status).toBe("degraded");
  });

  it("throws ApiError on generic non-OK or offline proxy 503 responses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "DB unreachable" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        })
      )
    );

    await expect(fetchHealth()).rejects.toThrow(ApiError);

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ status: "offline", detail: "Network unreachable" }),
          {
            status: 503,
            headers: { "Content-Type": "application/json" },
          }
        )
      )
    );

    await expect(fetchHealth()).rejects.toThrow(ApiError);
  });
});
