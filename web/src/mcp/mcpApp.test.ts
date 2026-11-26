import type {
  McpUiHostCapabilities,
  McpUiHostContext,
  McpUiStyles,
} from "@modelcontextprotocol/ext-apps";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DEMO_PAYLOADS } from "./demoFixtures";
import {
  TrailForgeMcpApp,
  applyHostThemeContext,
  buildModelContextSummary,
  renderExplorerApp,
  resolveHostCapabilities,
  type ExplorerWidgetState,
  type McpAppBridge,
} from "./mcpApp";
import { buildVectorMapSvg } from "./svgMapRenderer";
import {
  COMPARE_FALLBACK_COLORS,
  DEFAULT_ATTRIBUTIONS,
  normalizeViewPayload,
  sanitizeExternalHttpUrl,
  sanitizeHexColor,
} from "./types";

interface FakeBridgeOptions {
  capabilities?: McpUiHostCapabilities;
  context?: McpUiHostContext;
  onConnectCheck?: (bridge: McpAppBridge) => void;
}

function createFakeBridge(options: FakeBridgeOptions = {}) {
  const eventsOrder: string[] = [];
  let connected = false;

  const bridge: McpAppBridge = {
    ontoolinput: undefined,
    ontoolresult: undefined,
    ontoolcancelled: undefined,
    onhostcontextchanged: undefined,
    connect: vi.fn(async () => {
      eventsOrder.push(
        `connect:handlers=${Boolean(bridge.ontoolinput && bridge.ontoolresult && bridge.ontoolcancelled && bridge.onhostcontextchanged)}`
      );
      options.onConnectCheck?.(bridge);
      connected = true;
    }),
    getHostCapabilities: vi.fn(() => (connected ? options.capabilities : undefined)),
    getHostContext: vi.fn(() => (connected ? options.context : undefined)),
    openLink: vi.fn(async () => ({})),
    updateModelContext: vi.fn(async () => ({})),
    callServerTool: vi.fn(async () => ({
      structuredContent: DEMO_PAYLOADS.track as unknown as Record<string, unknown>,
    })),
  };

  return { bridge, eventsOrder };
}

describe("SC-4: TrailForge MCP App payload normalization & URL hygiene", () => {
  it("normalizes all 4 view_modes and enforces default mandatory attributions", () => {
    for (const mode of ["explore", "track", "route", "compare"] as const) {
      const stripped = { ...DEMO_PAYLOADS[mode], attributions: [] };
      const normalized = normalizeViewPayload(stripped);
      expect(normalized).not.toBeNull();
      expect(normalized?.view_mode).toBe(mode);
      expect(normalized?.schema_version).toBe("1.0");
      expect(normalized?.attributions.length).toBeGreaterThanOrEqual(
        DEFAULT_ATTRIBUTIONS.length
      );
      expect(normalized?.attributions.join(" ")).toContain("OpenStreetMap");
      expect(normalized?.attributions.join(" ")).toContain("Open-Meteo");
    }
  });

  it("rejects unsafe non-http(s) URLs for external links", () => {
    expect(sanitizeExternalHttpUrl("https://trails.example.com/?track=t1")).toBe(
      "https://trails.example.com/?track=t1"
    );
    expect(sanitizeExternalHttpUrl("javascript:alert(1)")).toBeNull();
    expect(sanitizeExternalHttpUrl("data:text/html,<svg onload=alert(1)>")).toBeNull();
    expect(sanitizeExternalHttpUrl("file:///etc/passwd")).toBeNull();
  });

  it("rejects malicious color_hex values in compare mode across HTML, SVG map, and elevation chart", () => {
    expect(sanitizeHexColor("#22C55E")).toBe("#22C55E");
    expect(sanitizeHexColor("#abc")).toBe("#abc");
    expect(sanitizeHexColor('" onload="alert(1)', "#38BDF8")).toBe("#38BDF8");
    expect(
      sanitizeHexColor("red;background:url(javascript:alert(1))", "#F59E0B")
    ).toBe("#F59E0B");

    const poisonedCompare = structuredClone(DEMO_PAYLOADS.compare);
    poisonedCompare.compare!.tracks[0]!.color_hex = '" onload="alert(1)';
    poisonedCompare.compare!.tracks[1]!.color_hex =
      "red;background:url(javascript:alert(1))";

    const rendered = renderExplorerApp({
      status: "ready",
      statusMessage: null,
      payload: poisonedCompare,
      previousExplorePayload: null,
      theme: "dark",
      capabilities: {
        openLinks: true,
        updateModelContext: true,
        serverTools: true,
      },
      rendererMode: "svg",
      forceSvgFallback: true,
      standalonePreview: false,
      actionToast: null,
    });

    expect(rendered.html).not.toContain('onload="alert(1)');
    expect(rendered.html).not.toContain("javascript:alert(1)");
    expect(rendered.html).toContain(COMPARE_FALLBACK_COLORS[0]!);
    expect(rendered.html).toContain(COMPARE_FALLBACK_COLORS[1]!);
  });
});

describe("SC-4: Zero-network SVG fallback map & synced elevation rendering across all 4 modes", () => {
  const baseState = (
    overrides: Partial<ExplorerWidgetState>
  ): ExplorerWidgetState => ({
    status: "ready",
    statusMessage: null,
    payload: null,
    previousExplorePayload: null,
    theme: "dark",
    capabilities: {
      openLinks: true,
      updateModelContext: true,
      serverTools: true,
    },
    rendererMode: "svg",
    forceSvgFallback: true,
    standalonePreview: false,
    actionToast: null,
    ...overrides,
  });

  it("renders explore mode with grade palette, closures, POIs, north arrow, scale bar, and attribution footer", () => {
    const rendered = renderExplorerApp(
      baseState({ payload: DEMO_PAYLOADS.explore })
    );
    expect(rendered.vectorMap).not.toBeNull();
    expect(rendered.html).toContain('data-view-mode="explore"');
    expect(rendered.html).toContain("Vector SVG (Zero-Network)");
    expect(rendered.html).toContain("#EF4444"); // Extreme
    expect(rendered.html).toContain("#F59E0B"); // Hard
    expect(rendered.html).toContain("#38BDF8"); // Medium
    expect(rendered.html).toContain("#22C55E"); // Easy
    expect(rendered.html).toContain('data-role="north-arrow"');
    expect(rendered.html).toContain('data-role="scale-bar"');
    expect(rendered.html).toContain('data-marker-kind="closure"');
    expect(rendered.html).toContain('data-marker-kind="poi"');
    expect(rendered.html).toContain("Davies Plain Track");
    expect(rendered.html).toContain('data-role="attribution-footer"');
    expect(rendered.html).toContain("OpenStreetMap contributors");
  });

  it("renders track mode with synchronized elevation SVG, S/F markers, explainable breakdown, live telemetry, and snow-line", () => {
    const rendered = renderExplorerApp(
      baseState({ payload: DEMO_PAYLOADS.track })
    );
    expect(rendered.vectorMap?.vertices.length).toBeGreaterThan(2);
    expect(rendered.elevationGeometry).not.toBeNull();
    expect(rendered.html).toContain('data-view-mode="track"');
    expect(rendered.html).toContain('data-role="sf-markers"');
    expect(rendered.html).toContain('data-role="elevation-section"');
    expect(rendered.html).toContain("Snow-line 1000m");
    expect(rendered.html).toContain('data-role="grade-breakdown"');
    expect(rendered.html).toContain("Surface &amp; Tracktype");
    expect(rendered.html).toContain('data-role="live-telemetry-section"');
    expect(rendered.html).toContain("14 traversals");
    expect(rendered.html).toContain("Rain 72h: 14.2 mm");
    expect(rendered.html).toContain('data-role="attribution-footer"');
  });

  it("renders route mode with stitched legs, combined elevation profile, safety KPIs, and copyable GridForge CLI", () => {
    const rendered = renderExplorerApp(
      baseState({ payload: DEMO_PAYLOADS.route })
    );
    expect(rendered.vectorMap?.vertices.length).toBeGreaterThan(4);
    expect(rendered.elevationGeometry).not.toBeNull();
    expect(rendered.html).toContain('data-view-mode="route"');
    expect(rendered.html).toContain('data-role="route-legs-section"');
    expect(rendered.html).toContain("Davies Plain Track");
    expect(rendered.html).toContain("Tom Groggin River Track");
    expect(rendered.html).toContain('data-role="gridforge-cli"');
    expect(rendered.html).toContain("gridforge render");
    expect(rendered.html).toContain('data-role="attribution-footer"');
  });

  it("renders compare mode with overlaid multi-track elevation chart and side-by-side comparison table", () => {
    const rendered = renderExplorerApp(
      baseState({ payload: DEMO_PAYLOADS.compare })
    );
    expect(rendered.elevationGeometry).not.toBeNull();
    expect(rendered.html).toContain('data-view-mode="compare"');
    expect(rendered.html).toContain("tfm-compare-elev-svg");
    expect(rendered.html).toContain('data-role="compare-table-section"');
    expect(rendered.html).toContain("Mt Pinnibar Ridge Track");
    expect(rendered.html).toContain('data-role="attribution-footer"');
  });

  it("never renders a blank map even when segment geometries are empty", () => {
    const emptyExplore = normalizeViewPayload({
      view_mode: "explore",
      title: "Empty Area",
      explore: { tracks: [], closures: [], reports: [], pois: [] },
    })!;
    const svg = buildVectorMapSvg(emptyExplore, { theme: "light" });
    expect(svg.svgHtml).toContain("<svg");
    expect(svg.svgHtml).toContain('fill="#F1F5F9"');
    expect(svg.svgHtml).toContain('data-role="north-arrow"');
    expect(svg.svgHtml).toContain('data-role="scale-bar"');
  });
});

describe("SC-4: SEP-1865 lifecycle ordering, capability gating, and host theme sync", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("registers ontoolinput, ontoolresult, ontoolcancelled, and onhostcontextchanged BEFORE calling connect()", async () => {
    let handlersPresentAtConnect = false;
    const { bridge, eventsOrder } = createFakeBridge({
      capabilities: {
        openLinks: {},
        updateModelContext: { text: {} },
        serverTools: {},
      },
      context: { theme: "dark" },
      onConnectCheck: (b) => {
        handlersPresentAtConnect = Boolean(
          b.ontoolinput &&
            b.ontoolresult &&
            b.ontoolcancelled &&
            b.onhostcontextchanged
        );
      },
    });

    const app = new TrailForgeMcpApp({ bridge, forceSvgFallback: true });
    await app.start();

    expect(handlersPresentAtConnect).toBe(true);
    expect(app.wereHandlersRegisteredBeforeConnect()).toBe(true);
    expect(eventsOrder).toEqual(["connect:handlers=true"]);

    // Tool lifecycle transitions
    bridge.ontoolinput?.({ arguments: { track_name: "Davies Plain Track" } });
    expect(app.getState().status).toBe("loading");
    expect(app.getState().statusMessage).toContain("Davies Plain Track");

    bridge.ontoolresult?.({
      content: [{ type: "text", text: "Davies Plain Track summary" }],
      structuredContent: DEMO_PAYLOADS.track as unknown as Record<string, unknown>,
    });
    expect(app.getState().status).toBe("ready");
    expect(app.getState().payload?.view_mode).toBe("track");

    bridge.ontoolcancelled?.({ reason: "user stopped" });
    expect(app.getState().status).toBe("cancelled");
    expect(app.getState().statusMessage).toContain("user stopped");
  });

  it("gates Open in TrailForge, Add to AI Context, and interactive track drill-down on getHostCapabilities()", async () => {
    // 1. Restricted host (e.g. global view without openLinks / updateModelContext / serverTools)
    expect(resolveHostCapabilities(undefined)).toEqual({
      openLinks: false,
      updateModelContext: false,
      serverTools: false,
    });

    const restricted = createFakeBridge({ capabilities: {} });
    const restrictedApp = new TrailForgeMcpApp({
      bridge: restricted.bridge,
      forceSvgFallback: true,
    });
    await restrictedApp.start();
    restricted.bridge.ontoolresult?.({
      content: [],
      structuredContent: DEMO_PAYLOADS.explore as unknown as Record<string, unknown>,
    });

    const restrictedHtml = renderExplorerApp(restrictedApp.getState()).html;
    expect(restrictedHtml).not.toContain('data-action="open-trailforge"');
    expect(restrictedHtml).not.toContain('data-action="add-to-context"');
    expect(restrictedHtml).not.toContain("data-inspect-track=");

    // Calling gated methods when unsupported is a safe no-op
    await restrictedApp.openExternalUrl("https://trails.example.com");
    await restrictedApp.pushSelectionToModelContext();
    await restrictedApp.inspectTrackById("tftrk_davies_plain");
    expect(restricted.bridge.openLink).not.toHaveBeenCalled();
    expect(restricted.bridge.updateModelContext).not.toHaveBeenCalled();
    expect(restricted.bridge.callServerTool).not.toHaveBeenCalled();

    // 2. Full-capability host
    const full = createFakeBridge({
      capabilities: {
        openLinks: {},
        updateModelContext: { text: {} },
        serverTools: { listChanged: true },
      },
    });
    const fullApp = new TrailForgeMcpApp({
      bridge: full.bridge,
      forceSvgFallback: true,
    });
    await fullApp.start();
    full.bridge.ontoolresult?.({
      content: [],
      structuredContent: DEMO_PAYLOADS.explore as unknown as Record<string, unknown>,
    });

    const fullHtml = renderExplorerApp(fullApp.getState()).html;
    expect(fullHtml).toContain('data-action="open-trailforge"');
    expect(fullHtml).toContain('data-action="add-to-context"');
    expect(fullHtml).toContain('data-inspect-track="tftrk_davies_plain"');

    await fullApp.openExternalUrl(DEMO_PAYLOADS.explore.deep_link_url);
    expect(full.bridge.openLink).toHaveBeenCalledWith({
      url: DEMO_PAYLOADS.explore.deep_link_url,
    });

    await fullApp.pushSelectionToModelContext();
    expect(full.bridge.updateModelContext).toHaveBeenCalledTimes(1);
    expect(buildModelContextSummary(DEMO_PAYLOADS.explore)).toContain(
      "Davies Plain Track"
    );

    await fullApp.inspectTrackById("tftrk_davies_plain");
    expect(full.bridge.callServerTool).toHaveBeenCalledWith({
      name: "app_get_track_detail",
      arguments: {
        track_id: "tftrk_davies_plain",
        vehicle_class: "Touring 4WD",
      },
    });
    expect(fullApp.getState().payload?.view_mode).toBe("track");
    expect(fullApp.getState().previousExplorePayload?.view_mode).toBe("explore");
  });

  it("syncs host light/dark theme and CSS custom properties on connect and onhostcontextchanged", async () => {
    vi.stubGlobal("document", { documentElement: {} });
    const applyDocThemeMock = vi.fn();
    const applyVarsMock = vi.fn();
    const applyFontsMock = vi.fn();

    const { bridge } = createFakeBridge({
      capabilities: {},
      context: {
        theme: "dark",
        styles: {
          variables: {
            "--color-background-primary": "#0b1117",
            "--color-text-primary": "#e6edf3",
          } as unknown as McpUiStyles,
        },
      },
    });

    const app = new TrailForgeMcpApp({
      bridge,
      forceSvgFallback: true,
      themeHooks: {
        applyDocumentTheme: applyDocThemeMock,
        applyHostStyleVariables: applyVarsMock,
        applyHostFonts: applyFontsMock,
      },
    });
    await app.start();

    expect(applyDocThemeMock).toHaveBeenCalledWith("dark");
    expect(applyVarsMock).toHaveBeenCalledWith(
      {
        "--color-background-primary": "#0b1117",
        "--color-text-primary": "#e6edf3",
      },
      undefined
    );
    expect(app.getState().theme).toBe("dark");

    // Live theme switch from host
    bridge.onhostcontextchanged?.({
      theme: "light",
      styles: {
        variables: {
          "--color-background-primary": "#ffffff",
          "--color-text-primary": "#0f172a",
        } as unknown as McpUiStyles,
        css: {
          fonts: "@font-face { font-family: 'HostSans'; src: local('Arial'); }",
        },
      },
    });

    expect(applyDocThemeMock).toHaveBeenCalledWith("light");
    expect(applyVarsMock).toHaveBeenCalledWith(
      {
        "--color-background-primary": "#ffffff",
        "--color-text-primary": "#0f172a",
      },
      undefined
    );
    expect(applyFontsMock).toHaveBeenCalledTimes(1);
    expect(app.getState().theme).toBe("light");

    // Helper directly returns null on undefined context
    expect(applyHostThemeContext(undefined)).toBeNull();
  });

  it("boots standalone preview mode via ?demo=route&theme=light query params", async () => {
    const { bridge } = createFakeBridge();
    const app = new TrailForgeMcpApp({
      bridge,
      search: "?demo=route&theme=light",
      forceSvgFallback: true,
    });
    expect(app.getState().standalonePreview).toBe(true);
    expect(app.getState().payload?.view_mode).toBe("route");
    expect(app.getState().theme).toBe("light");

    const rendered = renderExplorerApp(app.getState());
    expect(rendered.html).toContain("Davies Plain to Tom Groggin Traverse");
    expect(rendered.html).toContain('data-action="open-trailforge"');
    expect(rendered.html).toContain('data-role="attribution-footer"');
  });
});
