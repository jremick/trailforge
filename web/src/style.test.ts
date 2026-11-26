import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, fetchHealth } from "./api";
import { createInitialLayerState } from "./presets";
import {
  buildOfflineGraticuleGeoJson,
  buildRegionalBoundsGeoJson,
  buildTrailForgeStyle,
} from "./style";

describe("MapLibre 5 offline-capable style builder", () => {
  it("builds a v8 style with offline graticule, corridors, and Slice 03 layers", () => {
    const layers = createInitialLayerState();
    const style = buildTrailForgeStyle({
      initialBasemap: "offline-topo",
      layers,
    });

    expect(style.version).toBe(8);
    expect(style.sources["offline-graticule"]).toBeDefined();
    expect(style.sources["offline-regions"]).toBeDefined();
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

    // Slice 03 layer slots honor initial visibility
    expect(
      (byId.get("tf-tracks-line")?.layout as { visibility?: string })
        ?.visibility
    ).toBe("visible");
    expect(
      (byId.get("tf-steep-highlights")?.layout as { visibility?: string })
        ?.visibility
    ).toBe("visible");
    expect(
      (byId.get("tf-contours-hillshade")?.layout as { visibility?: string })
        ?.visibility
    ).toBe("visible");
    expect(
      (byId.get("tf-snow-line-fill")?.layout as { visibility?: string })
        ?.visibility
    ).toBe("none");
    expect(
      (byId.get("tf-pois-layer")?.layout as { visibility?: string })?.visibility
    ).toBe("visible");
  });

  it("enables OpenTopo + OSM underlay when opentopo basemap is active", () => {
    const style = buildTrailForgeStyle({
      initialBasemap: "opentopo",
      layers: { ...createInitialLayerState(), snowLine: true },
    });
    const byId = new Map(style.layers.map((l) => [l.id, l]));
    expect(byId.get("basemap-opentopo")?.layout).toEqual({
      visibility: "visible",
    });
    expect(byId.get("basemap-osm-underlay")?.layout).toEqual({
      visibility: "visible",
    });
    expect(byId.get("tf-snow-line-fill")?.layout).toEqual({
      visibility: "visible",
    });
  });

  it("generates non-empty offline graticule and corridor GeoJSON", () => {
    const graticule = buildOfflineGraticuleGeoJson();
    expect(graticule.features.length).toBeGreaterThan(25);

    const regions = buildRegionalBoundsGeoJson();
    expect(regions.features.length).toBe(3);
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
      version: "0.1.0",
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
      version: "0.1.0",
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
